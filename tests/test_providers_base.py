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
