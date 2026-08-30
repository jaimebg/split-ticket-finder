"""Tests for phase 1: confirming a shortlist against real offers.

Reuses ``FakeProvider``/``_offer`` from ``tests/test_engine_fetch.py`` rather
than redefining them -- the same fake, same call-recording behaviour, is
exactly what proves the dedup payoff (one call for a shared domestic leg)
and the re-pricing behaviour (offers, not estimates) that this module exists
to guarantee.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from engine.drill import confirm, through_fares
from engine.fetch import LegFetcher
from models import CancelToken, Candidate, Itinerary, SearchCancelled
from providers.base import ProviderError
from tests.test_engine_fetch import FakeProvider, _offer


def _cand(date, hub, dest, *, dom_price="100", onward_price="500",
          discount="0", return_date=""):
    """A candidate whose calendar estimate is deliberately far from the real offers."""
    return Candidate(date=date, return_date=return_date, hub=hub, dest=dest,
                     dom_price=Decimal(dom_price), onward_price=Decimal(onward_price),
                     discount=Decimal(discount))


def _offer_pnr(price, pnr_count):
    """An offer priced at ``price`` reporting a specific ``pnr_count``."""
    return dataclasses.replace(_offer(price), pnr_count=pnr_count)


def _itin(date, dest, total, *, hub="MAD", return_date=""):
    """An itinerary whose ``.total`` is exactly ``total`` -- via a single dom_out
    offer -- so tests can control cheapest-date ordering without caring about
    the rest of the itinerary's shape."""
    return Itinerary(
        date=date, return_date=return_date, hub=hub, hub_name=hub, dest=dest, dest_name=dest,
        discount=Decimal(0), dom_out=_offer(str(total)),
    )


def _fetcher(provider, **kw):
    return LegFetcher(provider, concurrency=4, delay=0, **kw)


async def test_confirm_builds_itineraries_from_real_offers_not_estimates():
    """A confirmed itinerary carries offers and prices from them, not the estimate."""
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
    })
    # Calendar estimate totals 600; the real offers total 340 -- deliberately
    # different, so a pass-through implementation would fail this.
    cand = _cand("2026-10-01", "MAD", "NRT", dom_price="100", onward_price="500")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=0,
        hub_names={"MAD": "Madrid"}, dest_names={"NRT": "Tokyo Narita"},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert len(result) == 1
    itin = result[0]
    assert itin.confirmed is True
    assert itin.total == Decimal("340.00")
    # Requirement 4: a confirmed itinerary must not also carry estimate fields.
    assert itin.est_dom_price is None
    assert itin.est_onward_price is None


async def test_confirm_picks_the_cheapest_offer_per_leg():
    """Multiple offers for one leg: the cheapest must win, regardless of order."""
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("90"), _offer("40"), _offer("60")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
    })
    cand = _cand("2026-10-01", "MAD", "NRT")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert result[0].dom_out.price == Decimal("40")


async def test_confirm_drops_a_candidate_missing_any_leg():
    """Unbookable is not cheap: a missing leg means no itinerary at all."""
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        # No offer for MAD -> NRT: this leg genuinely has no result.
    })
    cand = _cand("2026-10-01", "MAD", "NRT")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert result == []


async def test_confirm_fetches_the_shared_domestic_leg_once_for_three_destinations():
    """The dedup payoff: one LPA->MAD call serves three destinations that day."""
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
        ("MAD", "JFK", "2026-10-01"): [_offer("250")],
        ("MAD", "LAX", "2026-10-01"): [_offer("400")],
    })
    cands = [
        _cand("2026-10-01", "MAD", "NRT"),
        _cand("2026-10-01", "MAD", "JFK"),
        _cand("2026-10-01", "MAD", "LAX"),
    ]

    result = await confirm(
        _fetcher(provider), cands, origin="LPA", trip_days=0,
        hub_names={"MAD": "Madrid"},
        dest_names={"NRT": "Tokyo Narita", "JFK": "New York JFK", "LAX": "Los Angeles"},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert len(result) == 3
    # 1 domestic leg + 3 onward legs = 4 calls total, not 3 domestic + 3 onward.
    assert len(provider.seen) == 4
    domestic_calls = [q for q in provider.seen
                       if (q.origin, q.dest, q.date) == ("LPA", "MAD", "2026-10-01")]
    assert len(domestic_calls) == 1


async def test_confirm_round_trip_fetches_and_sums_all_four_legs():
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
        ("MAD", "LPA", "2026-10-15"): [_offer("45")],
        ("NRT", "MAD", "2026-10-15"): [_offer("280")],
    })
    cand = _cand("2026-10-01", "MAD", "NRT", return_date="2026-10-15")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=14,
        hub_names={"MAD": "Madrid"}, dest_names={"NRT": "Tokyo Narita"},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert len(provider.seen) == 4
    assert len(result) == 1
    itin = result[0]
    assert itin.confirmed is True
    assert itin.dom_out.price == Decimal("40")
    assert itin.dom_ret.price == Decimal("45")
    assert itin.onward_out.price == Decimal("300")
    assert itin.onward_ret.price == Decimal("280")
    assert itin.total == Decimal("665.00")  # (40+45) + (300+280), no discount


async def test_confirm_round_trip_drops_candidate_missing_a_return_leg():
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
        ("MAD", "LPA", "2026-10-15"): [_offer("45")],
        # No return onward leg.
    })
    cand = _cand("2026-10-01", "MAD", "NRT", return_date="2026-10-15")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=14,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert result == []


async def test_confirm_carries_hub_and_dest_names_onto_the_itinerary():
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
    })
    cand = _cand("2026-10-01", "MAD", "NRT")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=0,
        hub_names={"MAD": "Madrid"}, dest_names={"NRT": "Tokyo Narita"},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert result[0].hub_name == "Madrid"
    assert result[0].dest_name == "Tokyo Narita"


async def test_confirm_applies_discount_only_for_hubs_in_discount_airports():
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
        ("LPA", "LIS", "2026-10-01"): [_offer("50")],
        ("LIS", "NRT", "2026-10-01"): [_offer("310")],
    })
    cands = [_cand("2026-10-01", "MAD", "NRT"), _cand("2026-10-01", "LIS", "NRT")]

    result = await confirm(
        _fetcher(provider), cands, origin="LPA", trip_days=0,
        hub_names={"MAD": "Madrid", "LIS": "Lisbon"}, dest_names={"NRT": "Tokyo Narita"},
        discount_airports={"MAD"}, discount=Decimal("0.75"), adults=1, currency="EUR",
    )

    by_hub = {itin.hub: itin for itin in result}
    assert by_hub["MAD"].discount == Decimal("0.75")
    assert by_hub["LIS"].discount == Decimal("0")


async def test_confirm_sorts_results_cheapest_first():
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("900")],   # expensive
        ("LPA", "LIS", "2026-10-01"): [_offer("30")],
        ("LIS", "JFK", "2026-10-01"): [_offer("100")],   # cheap
    })
    cands = [_cand("2026-10-01", "MAD", "NRT"), _cand("2026-10-01", "LIS", "JFK")]

    result = await confirm(
        _fetcher(provider), cands, origin="LPA", trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert [itin.total for itin in result] == sorted(itin.total for itin in result)
    assert result[0].dest == "JFK"


async def test_confirm_on_an_empty_candidate_list_makes_no_requests():
    provider = FakeProvider()

    result = await confirm(
        _fetcher(provider), [], origin="LPA", trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert result == []
    assert provider.seen == []


async def test_confirm_cancellation_propagates_as_search_cancelled():
    provider = FakeProvider(delay=0.01)
    token = CancelToken()
    token.cancel()
    cands = [_cand(f"2026-10-{d:02d}", "MAD", "NRT") for d in range(1, 6)]

    with pytest.raises(SearchCancelled):
        await confirm(
            _fetcher(provider, cancel=token), cands, origin="LPA", trip_days=0,
            hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
            adults=1, currency="EUR",
        )


async def test_confirm_one_way_ignores_a_populated_return_date_on_the_candidate():
    """trip_days is the authority on trip shape, mirroring legs_for: a candidate
    with a stray return_date is still built as one-way when trip_days == 0, so
    it is not wrongly dropped for "missing" return legs that were never fetched."""
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
    })
    cand = _cand("2026-10-01", "MAD", "NRT", return_date="2026-10-15")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert len(result) == 1
    assert result[0].confirmed is True
    assert result[0].return_date == ""
    assert result[0].dom_ret is None
    assert result[0].onward_ret is None


async def test_confirm_never_sets_min_layover_children_or_non_economy_cabin():
    """Phase 1 must work on a Google-only deployment: Google raises a bare
    ProviderError for min_layover, children, or a non-ECONOMY cabin, so
    confirm() must never set them unless the caller supplied them -- and
    confirm() takes no such parameters at all."""
    class _AssertingProvider(FakeProvider):
        async def search_leg(self, query):
            if query.min_layover is not None or query.children or query.cabin != "ECONOMY":
                raise ProviderError("would abort a Google-only deployment")
            return await super().search_leg(query)

    provider = _AssertingProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
    })
    cand = _cand("2026-10-01", "MAD", "NRT")

    result = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert len(result) == 1


# ── Phase 2: through_fares ───────────────────────────────────────────────────
#
# Only a single-PNR offer (pnr_count == 1) can substantiate a through-fare
# claim. Kiwi itself sells multi-PNR "self-transfer" itineraries -- a real
# LPA->NRT query came back with pnrCount: 3 -- and comparing our split against
# one of those is comparing a split against another split, not a genuine
# saving. Google cannot report pnr_count at all, so None must not be treated
# as 1 either.


async def test_through_fares_queries_only_the_three_cheapest_distinct_dates():
    """Four distinct dates, one of them ("2026-10-04") priced far above the
    others, and one date ("2026-10-01") shared by two destinations. Only the
    three cheapest dates should generate any query at all, and every
    (destination, date) pair on those dates gets exactly one query."""
    provider = FakeProvider({
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("50", 1)],
        ("LPA", "JFK", "2026-10-01"): [_offer_pnr("60", 1)],
        ("LPA", "NRT", "2026-10-02"): [_offer_pnr("100", 1)],
        ("LPA", "NRT", "2026-10-03"): [_offer_pnr("150", 1)],
        ("LPA", "NRT", "2026-10-04"): [_offer_pnr("999", 1)],
    })
    itineraries = [
        _itin("2026-10-01", "NRT", "50"),
        _itin("2026-10-01", "JFK", "60"),
        _itin("2026-10-02", "NRT", "100"),
        _itin("2026-10-03", "NRT", "150"),
        _itin("2026-10-04", "NRT", "999"),  # most expensive date -- must be excluded
    ]

    result = await through_fares(
        _fetcher(provider), itineraries, origin="LPA", trip_days=0,
        adults=1, currency="EUR",
    )

    # 4 pairs on the 3 cheapest dates: (NRT,01) (JFK,01) (NRT,02) (NRT,03).
    assert len(provider.seen) == 4
    assert all(q.date != "2026-10-04" for q in provider.seen)
    assert result == {
        ("NRT", "2026-10-01"): Decimal("50"),
        ("JFK", "2026-10-01"): Decimal("60"),
        ("NRT", "2026-10-02"): Decimal("100"),
        ("NRT", "2026-10-03"): Decimal("150"),
    }


async def test_through_fares_skips_a_cheaper_multi_pnr_offer():
    """The cheapest offer overall has pnr_count == 3 (Kiwi's own self-transfer)
    and is cheaper than the single-PNR alternative. A naive "cheapest wins"
    implementation would report 300, not 400."""
    provider = FakeProvider({
        ("LPA", "NRT", "2026-10-01"): [
            _offer_pnr("300", 3),
            _offer_pnr("400", 1),
        ],
    })
    itineraries = [_itin("2026-10-01", "NRT", "500")]

    result = await through_fares(
        _fetcher(provider), itineraries, origin="LPA", trip_days=0,
        adults=1, currency="EUR",
    )

    assert result == {("NRT", "2026-10-01"): Decimal("400")}


async def test_through_fares_pair_absent_when_no_single_pnr_offer_exists():
    """Every offer for this leg is multi-PNR -- the pair must not appear at
    all, not be recorded as a saving of zero."""
    provider = FakeProvider({
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("300", 3), _offer_pnr("350", 2)],
    })
    itineraries = [_itin("2026-10-01", "NRT", "500")]

    result = await through_fares(
        _fetcher(provider), itineraries, origin="LPA", trip_days=0,
        adults=1, currency="EUR",
    )

    assert result == {}


async def test_through_fares_pnr_count_none_does_not_qualify():
    """A Google-style offer reports pnr_count=None. None is not 1, so it
    cannot substantiate a through-fare claim, no matter how cheap it is."""
    provider = FakeProvider({
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("200", None)],
    })
    itineraries = [_itin("2026-10-01", "NRT", "500")]

    result = await through_fares(
        _fetcher(provider), itineraries, origin="LPA", trip_days=0,
        adults=1, currency="EUR",
    )

    assert result == {}


async def test_through_fares_round_trip_sums_outbound_and_return():
    provider = FakeProvider({
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("400", 1)],
        ("NRT", "LPA", "2026-10-15"): [_offer_pnr("380", 1)],
    })
    itineraries = [_itin("2026-10-01", "NRT", "500", return_date="2026-10-15")]

    result = await through_fares(
        _fetcher(provider), itineraries, origin="LPA", trip_days=14,
        adults=1, currency="EUR",
    )

    assert result == {("NRT", "2026-10-01"): Decimal("780")}


async def test_through_fares_round_trip_non_qualifying_return_yields_no_entry():
    """A single-PNR outbound paired with a multi-PNR return is not a
    through-fare: both directions must qualify independently."""
    provider = FakeProvider({
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("400", 1)],
        ("NRT", "LPA", "2026-10-15"): [_offer_pnr("380", 3)],
    })
    itineraries = [_itin("2026-10-01", "NRT", "500", return_date="2026-10-15")]

    result = await through_fares(
        _fetcher(provider), itineraries, origin="LPA", trip_days=14,
        adults=1, currency="EUR",
    )

    assert result == {}


async def test_through_fares_on_an_empty_itinerary_list_makes_no_requests():
    provider = FakeProvider()

    result = await through_fares(
        _fetcher(provider), [], origin="LPA", trip_days=0,
        adults=1, currency="EUR",
    )

    assert result == {}
    assert provider.seen == []


async def test_through_fares_never_sets_min_layover_children_or_non_economy_cabin():
    """Google raises a bare ProviderError for any of these -- aborting the
    whole phase by design -- so through_fares must never set them."""
    class _AssertingProvider(FakeProvider):
        async def search_leg(self, query):
            if query.min_layover is not None or query.children or query.cabin != "ECONOMY":
                raise ProviderError("would abort a Google-only deployment")
            return await super().search_leg(query)

    provider = _AssertingProvider({
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("400", 1)],
    })
    itineraries = [_itin("2026-10-01", "NRT", "500")]

    result = await through_fares(
        _fetcher(provider), itineraries, origin="LPA", trip_days=0,
        adults=1, currency="EUR",
    )

    assert result == {("NRT", "2026-10-01"): Decimal("400")}


async def test_through_fares_feeds_itinerary_savings_and_absent_pair_stays_none():
    """The mapping through_fares produces attaches cleanly via
    with_through_fare: a priced pair reports real savings/savings_pct, and an
    itinerary for an absent pair reports None for both -- never zero."""
    provider = FakeProvider({
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("980", 1)],
        # No offer at all for the second destination/date.
    })
    priced = _itin("2026-10-01", "NRT", "612")
    unpriced = _itin("2026-10-02", "JFK", "612")

    fares = await through_fares(
        _fetcher(provider), [priced, unpriced], origin="LPA", trip_days=0,
        adults=1, currency="EUR",
    )

    priced_itin = priced.with_through_fare(fares.get((priced.dest, priced.date)))
    unpriced_itin = unpriced.with_through_fare(fares.get((unpriced.dest, unpriced.date)))

    assert priced_itin.savings == Decimal("368.00")
    assert priced_itin.savings_pct == 37
    assert unpriced_itin.savings is None
    assert unpriced_itin.savings_pct is None
