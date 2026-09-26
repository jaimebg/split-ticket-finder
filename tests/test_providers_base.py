"""Tests for the provider-agnostic types and capability protocols."""
from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal

import pytest

from providers.base import (
    CalendarQuery,
    LegQuery,
    Offer,
    Place,
    ProviderError,
    ProviderFetchError,
    ProviderParseError,
    RatedPrice,
    Segment,
    SupportsCalendar,
    SupportsPlaces,
)


def _segment() -> Segment:
    return Segment(
        origin="LPA",
        dest="MAD",
        carrier="FR",
        carrier_name="Ryanair",
        flight_no="FR2012",
        duration=170,
        dep_local=datetime(2026, 10, 6, 8, 30),
        arr_local=datetime(2026, 10, 6, 12, 20),
    )


def test_offer_defaults_unknown_fields_to_none():
    """A provider that cannot report baggage must yield None, never zero.

    Rendering None as "0 bags included" would state a fare condition the bot
    never verified, on a project whose premise is that baggage erodes savings.
    """
    offer = Offer(
        price=Decimal("29"),
        currency="EUR",
        airlines=["Ryanair"],
        stops=0,
        duration=170,
        segments=[_segment()],
        provider="google",
    )
    assert offer.included_checked_bags is None
    assert offer.included_cabin_bags is None
    assert offer.checked_bag_price is None
    assert offer.booking_url is None
    assert offer.min_layover is None
    assert offer.pnr_count is None
    assert offer.requires_bag_recheck is None


def test_offer_price_is_decimal_not_float():
    offer = Offer(
        price=Decimal("174.303303"),
        currency="EUR",
        airlines=["Etihad"],
        stops=3,
        duration=2260,
        segments=[_segment()],
        provider="kiwi",
    )
    assert isinstance(offer.price, Decimal)
    assert offer.price * 4 == Decimal("697.213212")


def test_leg_query_defaults():
    q = LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    assert (q.adults, q.children, q.cabin, q.currency, q.limit) == (1, 0, "ECONOMY", "EUR", 5)
    assert q.max_stops is None and q.min_layover is None and q.exclude_carriers == ()


def test_calendar_query_defaults():
    q = CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
    assert (q.adults, q.cabin, q.currency) == (1, "ECONOMY", "EUR")


def test_error_hierarchy_lets_callers_catch_either_or_both():
    assert issubclass(ProviderFetchError, ProviderError)
    assert issubclass(ProviderParseError, ProviderError)
    assert not issubclass(ProviderParseError, ProviderFetchError)


def test_rated_price_and_place_are_plain_value_objects():
    rp = RatedPrice(price=Decimal("29"), rating="AVERAGE")
    assert rp.rating == "AVERAGE"
    p = Place(code="NRT", name="Narita International", city="Tokyo",
              country="Japan", place_id="Station:airport:NRT")
    assert p.place_id == "Station:airport:NRT"


class _CalendarOnly:
    async def price_calendar(self, query):
        return {}


class _PlacesOnly:
    async def resolve_place(self, term, limit=8):
        return []


def test_capability_protocols_are_detectable_at_runtime():
    """The engine picks its search strategy from these checks (spec 5.6)."""
    assert isinstance(_CalendarOnly(), SupportsCalendar)
    assert not isinstance(_CalendarOnly(), SupportsPlaces)
    assert isinstance(_PlacesOnly(), SupportsPlaces)
    assert not isinstance(_PlacesOnly(), SupportsCalendar)


def test_offer_is_frozen():
    offer = Offer(price=Decimal("29"), currency="EUR", airlines=[], stops=0,
                  duration=170, segments=[], provider="kiwi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        offer.price = Decimal("1")


# ── SearchOptions and Capabilities (Layer 3c) ───────────────────────────────

from providers.base import (
    ALL_CABINS,
    Capabilities,
    SearchOptions,
    capabilities_of,
)


def test_default_options_build_todays_queries():
    assert SearchOptions().leg_query("LPA", "MAD", "2026-10-01") == LegQuery(
        origin="LPA", dest="MAD", date="2026-10-01")
    assert SearchOptions().calendar_query("LPA", "MAD", "2026-10-01", "2026-10-31") == \
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")


def test_options_reach_the_queries():
    opts = SearchOptions(adults=2, children=1, cabin="BUSINESS", currency="USD",
                         max_stops=1, min_layover=90)
    q = opts.leg_query("LPA", "MAD", "2026-10-01")
    assert (q.adults, q.children, q.cabin, q.currency, q.max_stops, q.min_layover) == \
        (2, 1, "BUSINESS", "USD", 1, 90)
    c = opts.calendar_query("LPA", "MAD", "2026-10-01", "2026-10-31")
    assert (c.adults, c.children, c.cabin, c.currency) == (2, 1, "BUSINESS", "USD")


def test_without_limits_keeps_the_party_and_drops_the_limits():
    opts = SearchOptions(adults=2, cabin="BUSINESS", max_stops=0, min_layover=60)
    assert opts.without_limits() == SearchOptions(adults=2, cabin="BUSINESS")


def test_from_mapping_treats_null_as_the_default():
    row = {"adults": 2, "children": None, "cabin": None, "currency": "USD",
           "max_stops": None, "min_layover": 120, "unrelated": "x"}
    assert SearchOptions.from_mapping(row) == SearchOptions(
        adults=2, currency="USD", min_layover=120)
    assert SearchOptions.from_mapping({}) == SearchOptions()


def test_as_columns_round_trips_through_from_mapping():
    opts = SearchOptions(adults=3, children=2, cabin="FIRST_CLASS", currency="GBP",
                         max_stops=2, min_layover=180)
    assert SearchOptions.from_mapping(opts.as_columns()) == opts


def test_party_label():
    assert SearchOptions().party_label() == "1 adult · Economy"
    assert SearchOptions(adults=2, children=1, cabin="BUSINESS").party_label() == \
        "2 adults, 1 child · Business"
    assert SearchOptions(adults=1, children=2).party_label() == "1 adult, 2 children · Economy"
    assert SearchOptions().is_default_party
    assert not SearchOptions(adults=2).is_default_party
    assert SearchOptions(adults=2, children=1).passengers == 3


GOOGLE_LIKE = Capabilities(cabins=frozenset({"ECONOMY"}), children=False, min_layover=False)


def test_capabilities_reject_each_unsupported_option_in_words():
    assert GOOGLE_LIKE.rejects(SearchOptions()) is None
    assert GOOGLE_LIKE.rejects(SearchOptions(cabin="BUSINESS")) == \
        "Your flight source can't search Business class."
    assert GOOGLE_LIKE.rejects(SearchOptions(children=1)) == \
        "Your flight source can't search with children."
    assert GOOGLE_LIKE.rejects(SearchOptions(min_layover=60)) == \
        "Your flight source can't apply a minimum layover."


def test_a_provider_that_declares_nothing_is_assumed_capable():
    class Bare:
        name = "bare"

    caps = capabilities_of(Bare())
    assert caps.cabins == frozenset(ALL_CABINS)
    assert caps.rejects(SearchOptions(adults=2, children=1, cabin="FIRST_CLASS",
                                      min_layover=60)) is None


def test_calendar_queries_carry_the_search_limits():
    """Phase 0 ranks from calendars; without the limits it shortlists
    one-stop dates that a direct-only confirm then throws away."""
    c = SearchOptions(max_stops=0, min_layover=120).calendar_query(
        "MAD", "NRT", "2026-10-01", "2026-10-31")
    assert (c.max_stops, c.min_layover) == (0, 120)


def test_overnight_round_trips_through_the_columns():
    opts = SearchOptions(overnight=True)
    assert opts.as_columns()["overnight"] is True
    assert SearchOptions.from_mapping({"overnight": 1}) == opts
    assert SearchOptions.from_mapping({"overnight": None}).overnight is False
