"""Tests for the grid-search fallback used when the primary provider has no
price calendar (Google, via ``GoogleProvider``, which implements
``FlightProvider`` but not ``SupportsCalendar``).

This is the pre-Layer-1 four-phase grid search from ``search.py:run_search``,
ported onto ``engine.fetch.LegFetcher`` and the new ``Itinerary`` type. It
does not touch ``search.py`` -- that file keeps working for its own callers
until Task 12 rewires them.

Reuses ``FakeProvider``/``_offer`` from ``tests/test_engine_fetch.py``, same
as ``tests/test_engine_drill.py`` does -- the same call-recording fake is
exactly what proves both the date-sampling and the phase-narrowing behaviour
this module exists to preserve.
"""
from __future__ import annotations

from decimal import Decimal

from engine.fetch import LegFetcher
from engine.grid import FALLBACK_MAX_DATES, _sample_explicit_dates, run_grid_search
from models import SearchWindow
from tests.test_engine_fetch import FakeProvider, _offer


def _fetcher(provider, **kw):
    return LegFetcher(provider, concurrency=4, delay=0, **kw)


def _seen(provider):
    """The (origin, dest, date) triples the fake provider actually saw."""
    return [(q.origin, q.dest, q.date) for q in provider.seen]


# ── Date sampling ────────────────────────────────────────────────────────────
# Ported from tests/test_scraper.py's generate_dates coverage, at the level
# this task actually consumes it: through run_grid_search's own sampling,
# not generate_dates directly.


async def test_a_90_day_window_is_sampled_at_or_under_the_cap():
    window = SearchWindow("2026-10-01", "2026-12-30")
    assert window.days == 91  # a "90-day window" in the task's own wording
    provider = FakeProvider()

    await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    dates_queried = {date for _, _, date in _seen(provider)}
    assert len(dates_queried) <= FALLBACK_MAX_DATES


async def test_a_5_day_window_yields_exactly_five_dates():
    window = SearchWindow("2026-10-01", "2026-10-05")
    assert window.days == 5
    provider = FakeProvider()

    await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    dates_queried = {date for _, _, date in _seen(provider)}
    assert dates_queried == {
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    }


# ── Four phases, in order, producing confirmed itineraries ─────────────────
# Adapted from test_search.py's test_round_trip_totals_include_all_four_legs
# and test_return_legs_are_queried_on_the_return_date: same scenario, ported
# onto LegQuery/LegFetcher/Itinerary/Decimal.


async def test_four_phases_run_in_order_and_produce_a_confirmed_itinerary():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],   # Phase 1
        ("MAD", "LPA", "2026-10-15"): [_offer("110")],   # Phase 1R
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],   # Phase 2
        ("NRT", "MAD", "2026-10-15"): [_offer("520")],   # Phase 2R
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=14, hub_names={"MAD": "Madrid"},
        dest_names={"NRT": "Tokyo"}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    # Each phase ran exactly once, and phase 1 (+1R) fully preceded phase 2
    # (+2R) in the call log -- the phases are awaited in sequence, not
    # launched concurrently, so this ordering is deterministic.
    assert _seen(provider) == [
        ("LPA", "MAD", "2026-10-01"),
        ("MAD", "LPA", "2026-10-15"),
        ("MAD", "NRT", "2026-10-01"),
        ("NRT", "MAD", "2026-10-15"),
    ]

    assert len(result) == 1
    itin = result[0]
    assert itin.confirmed is True
    assert itin.hub == "MAD"
    assert itin.return_date == "2026-10-15"


async def test_return_legs_are_queried_only_on_the_return_date():
    """Return legs are separate one-way searches on the return date, never a
    round-trip query and never on the outbound date."""
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        ("MAD", "LPA", "2026-10-15"): [_offer("100")],
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],
        ("NRT", "MAD", "2026-10-15"): [_offer("500")],
    })

    await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=14, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    seen = _seen(provider)
    assert ("MAD", "LPA", "2026-10-15") in seen
    assert ("NRT", "MAD", "2026-10-15") in seen
    assert ("MAD", "LPA", "2026-10-01") not in seen
    assert ("NRT", "MAD", "2026-10-01") not in seen


# ── Narrowing: the whole point of the port ──────────────────────────────────
# Ported from test_search.py's test_unreachable_hubs_are_not_queried_for_onward_flights.


async def test_a_hub_unreachable_in_phase_1_is_never_queried_in_phase_2():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        # No LPA->BCN offer at all: BCN is unreachable from phase 1.
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],
        ("BCN", "NRT", "2026-10-01"): [_offer("400")],
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD", "BCN"],
        window=window, trip_days=0, hub_names={"MAD": "Madrid", "BCN": "Barcelona"},
        dest_names={"NRT": "Tokyo"}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )

    assert [itin.hub for itin in result] == ["MAD"]
    assert ("BCN", "NRT", "2026-10-01") not in _seen(provider)


async def test_an_empty_first_phase_means_no_onward_queries_at_all():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider()  # no answers anywhere

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert result == []
    # Only phase 1 ran; no onward query was ever attempted.
    assert all(origin == "LPA" for origin, _, _ in _seen(provider))


# ── Missing legs and combination assembly ───────────────────────────────────


async def test_a_combination_missing_the_onward_leg_produces_no_itinerary():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        # No MAD -> NRT offer: the onward leg genuinely has no result.
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert result == []


async def test_a_round_trip_missing_only_the_return_leg_produces_no_itinerary():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],
        ("MAD", "LPA", "2026-10-15"): [_offer("110")],
        # No NRT -> MAD return offer: the round trip is incomplete.
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=14, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert result == []


# ── Round-trip vs one-way leg counts ─────────────────────────────────────────
# Ported from test_search.py's test_round_trip_totals_include_all_four_legs.


async def test_round_trip_sums_all_four_legs():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        ("MAD", "LPA", "2026-10-15"): [_offer("110")],
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],
        ("NRT", "MAD", "2026-10-15"): [_offer("520")],
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=14, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert len(result) == 1
    itin = result[0]
    assert itin.dom_price == Decimal("210")     # 100 + 110
    assert itin.onward_price == Decimal("1020")  # 500 + 520
    assert itin.total == Decimal("1230.00")


async def test_one_way_uses_only_two_legs_and_skips_return_phases():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    assert len(result) == 1
    itin = result[0]
    assert itin.return_date == ""
    assert itin.dom_ret is None
    assert itin.onward_ret is None
    assert itin.total == Decimal("600.00")
    # No return-leg query of any kind was issued.
    assert all(dest != "LPA" for _, dest, _ in _seen(provider))


# ── Discount and sort order ──────────────────────────────────────────────────
# Ported from test_search.py's test_discount_applies_only_to_the_qualifying_hub
# and test_results_are_sorted_cheapest_first.


async def test_discount_applies_only_to_the_qualifying_hub():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        ("LPA", "LIS", "2026-10-01"): [_offer("100")],
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],
        ("LIS", "NRT", "2026-10-01"): [_offer("500")],
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD", "LIS"],
        window=window, trip_days=0, hub_names={"MAD": "Madrid", "LIS": "Lisboa"},
        dest_names={"NRT": "Tokyo"}, discount_airports={"MAD"},
        discount=Decimal("0.75"), adults=1, currency="EUR",
    )

    by_hub = {itin.hub: itin for itin in result}
    # MAD: 100 * (1 - 0.75) + 500 = 525
    assert by_hub["MAD"].dom_discounted == Decimal("25.00")
    assert by_hub["MAD"].total == Decimal("525.00")
    # LIS: no discount, so the full 100 counts
    assert by_hub["LIS"].dom_discounted == Decimal("100.00")
    assert by_hub["LIS"].total == Decimal("600.00")


async def test_results_are_sorted_cheapest_first():
    window = SearchWindow("2026-10-01", "2026-10-01")
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("100")],
        ("LPA", "BCN", "2026-10-01"): [_offer("50")],
        ("MAD", "NRT", "2026-10-01"): [_offer("500")],
        ("BCN", "NRT", "2026-10-01"): [_offer("700")],
    })

    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD", "BCN"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )

    totals = [itin.total for itin in result]
    assert totals == sorted(totals)
    assert result[0].hub == "MAD"  # 600 beats BCN's 750


# ── Empty inputs ─────────────────────────────────────────────────────────────


async def test_empty_destinations_makes_no_requests():
    provider = FakeProvider()
    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=[], hubs=["MAD"],
        window=SearchWindow("2026-10-01", "2026-10-01"), trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )
    assert result == []
    assert provider.seen == []


async def test_empty_hubs_makes_no_requests():
    provider = FakeProvider()
    result = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=[],
        window=SearchWindow("2026-10-01", "2026-10-01"), trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        adults=1, currency="EUR",
    )
    assert result == []
    assert provider.seen == []


# ── explicit_dates (review finding C1) ──────────────────────────────────────
#
# A caller with a discrete list of dates it actually wants searched (the
# guided search flow's own list, say) must have the grid path search those
# dates directly, not silently resample its own from the window instead --
# a window-derived sample can drop dates the caller explicitly asked for.


async def test_explicit_dates_are_searched_even_when_a_window_sample_would_miss_them():
    """The concrete C1 scenario: two dates 19 days apart. A window-derived
    sample at the default cap steps past the window's own end date and never
    queries it (10 evenly-spaced dates over a 20-day window, none of them
    day 20). Passing the two dates explicitly must guarantee both are hit."""
    window = SearchWindow("2026-09-01", "2026-09-20")
    provider = FakeProvider()

    # Without explicit_dates, the window sample misses day 20 -- pinning the
    # regression this fix closes.
    without_explicit = FakeProvider()
    await run_grid_search(
        _fetcher(without_explicit), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
    )
    assert "2026-09-20" not in {date for _, _, date in _seen(without_explicit)}

    await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
        explicit_dates=["2026-09-01", "2026-09-20"],
    )

    dates_queried = {date for _, _, date in _seen(provider)}
    assert dates_queried == {"2026-09-01", "2026-09-20"}


async def test_explicit_dates_beyond_the_cap_are_sampled_but_keep_both_extremes():
    window = SearchWindow("2026-09-01", "2026-09-20")
    dates = [f"2026-09-{d:02d}" for d in range(1, 21)]  # 20 explicit dates
    provider = FakeProvider()

    await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=window, trip_days=0, hub_names={}, dest_names={},
        discount_airports=set(), discount=Decimal(0), adults=1, currency="EUR",
        max_dates=5, explicit_dates=dates,
    )

    dates_queried = {date for _, _, date in _seen(provider)}
    assert len(dates_queried) <= 5
    assert "2026-09-01" in dates_queried
    assert "2026-09-20" in dates_queried
    assert dates_queried <= set(dates)


def test_sample_explicit_dates_keeps_first_and_last_and_dedupes():
    dates = [f"2026-09-{d:02d}" for d in range(1, 21)] + ["2026-09-01"]  # dup
    sampled = _sample_explicit_dates(dates, 5)

    assert len(sampled) == 5
    assert sampled[0] == "2026-09-01"
    assert sampled[-1] == "2026-09-20"
    assert sampled == sorted(sampled)
    assert set(sampled) <= set(dates)


def test_sample_explicit_dates_returns_all_deduped_when_under_the_cap():
    dates = ["2026-09-05", "2026-09-01", "2026-09-01"]
    assert _sample_explicit_dates(dates, 5) == ["2026-09-01", "2026-09-05"]
