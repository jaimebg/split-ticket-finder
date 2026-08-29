"""Tests for the Google adapter: FlightResult -> Offer."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from providers.base import LegQuery, Offer
from providers.google import GoogleProvider, _build_times


def test_provider_name():
    assert GoogleProvider().name == "google"


def test_build_times_attaches_query_date_to_bare_clock_times():
    """Google reports [hour, minute] with no date at all."""
    raw = [{"dep_time": [8, 30], "arr_time": [12, 20]}]
    times = _build_times("2026-10-06", raw)
    assert times == [(datetime(2026, 10, 6, 8, 30), datetime(2026, 10, 6, 12, 20))]


def test_build_times_rolls_over_midnight():
    """A 21:10 departure arriving 00:55 lands on the next day, not the same one."""
    raw = [{"dep_time": [21, 10], "arr_time": [0, 55]}]
    times = _build_times("2026-10-06", raw)
    assert times == [(datetime(2026, 10, 6, 21, 10), datetime(2026, 10, 7, 0, 55))]


def test_build_times_rolls_over_across_multiple_segments():
    raw = [
        {"dep_time": [22, 0], "arr_time": [23, 30]},
        {"dep_time": [1, 15], "arr_time": [6, 45]},
    ]
    times = _build_times("2026-10-06", raw)
    assert times[0] == (datetime(2026, 10, 6, 22, 0), datetime(2026, 10, 6, 23, 30))
    assert times[1] == (datetime(2026, 10, 7, 1, 15), datetime(2026, 10, 7, 6, 45))


def test_build_times_yields_none_for_missing_or_malformed_times():
    raw = [{"dep_time": [], "arr_time": None}, {"dep_time": ["x", "y"], "arr_time": [9, 0]}]
    times = _build_times("2026-10-06", raw)
    assert times[0] == (None, None)
    assert times[1][0] is None
    assert times[1][1] == datetime(2026, 10, 6, 9, 0)


async def test_search_leg_maps_real_capture_to_offers(real_html, monkeypatch):
    """The adapter turns a real Google response into Offers with unknowns as None."""
    import providers.google as google

    async def fake_fetch(*args, **kwargs):
        return real_html

    monkeypatch.setattr(google, "fetch_html", fake_fetch)

    offers = await GoogleProvider().search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    )

    assert offers, "the real capture contains offers"
    assert all(isinstance(o, Offer) for o in offers)
    assert [o.price for o in offers] == sorted(o.price for o in offers)

    first = offers[0]
    assert first.provider == "google"
    assert isinstance(first.price, Decimal)
    assert first.currency == "EUR"
    # Everything Google structurally cannot report stays unknown.
    assert first.included_checked_bags is None
    assert first.included_cabin_bags is None
    assert first.checked_bag_price is None
    assert first.booking_url is None
    assert first.pnr_count is None


async def test_search_leg_respects_limit(real_html, monkeypatch):
    import providers.google as google

    async def fake_fetch(*args, **kwargs):
        return real_html

    monkeypatch.setattr(google, "fetch_html", fake_fetch)
    offers = await GoogleProvider().search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06", limit=1)
    )
    assert len(offers) == 1
