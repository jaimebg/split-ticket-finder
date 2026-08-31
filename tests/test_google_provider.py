"""Tests for the Google adapter: FlightResult -> Offer."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from providers.base import LegQuery, Offer, ProviderError
from providers.google import FlightResult, GoogleProvider, _build_times


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


async def test_google_cannot_report_baggage_recheck(real_html, monkeypatch):
    """Google's payload has no layover data, so this must be unknown, not False."""
    import providers.google as google

    async def fake_fetch(*args, **kwargs):
        return real_html

    monkeypatch.setattr(google, "fetch_html", fake_fetch)
    offers = await GoogleProvider().search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    )
    assert offers[0].requires_bag_recheck is None


# ── Unsupported LegQuery fields (fix round 2, finding 1) ────────────────────
#
# Google honours max_stops and exclude_carriers client-side, and must raise
# rather than silently ignore or approximate children, non-ECONOMY cabin, and
# min_layover -- the query would otherwise mean something different depending
# on which provider answered it.


async def test_search_leg_drops_offers_exceeding_max_stops(monkeypatch):
    """max_stops is enforced client-side, before the limit slice."""
    import providers.google as google

    direct = FlightResult(
        price=100, airlines=["Iberia"], stops=0, duration=120,
        segments=[{
            "from": "LPA", "to": "MAD", "flight": "IB1234",
            "dep_time": [8, 0], "arr_time": [10, 0],
        }],
    )
    connecting = FlightResult(
        price=90, airlines=["Vueling"], stops=1, duration=300,
        segments=[
            {"from": "LPA", "to": "BCN", "flight": "VY1111",
             "dep_time": [8, 0], "arr_time": [10, 0]},
            {"from": "BCN", "to": "MAD", "flight": "VY2222",
             "dep_time": [12, 0], "arr_time": [13, 0]},
        ],
    )

    async def fake_fetch(*args, **kwargs):
        return "<html></html>"

    monkeypatch.setattr(google, "fetch_html", fake_fetch)
    monkeypatch.setattr(google, "parse_flights", lambda html: [connecting, direct])

    offers = await GoogleProvider().search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06", max_stops=0)
    )
    assert len(offers) == 1
    assert offers[0].stops == 0


async def test_search_leg_drops_offers_by_excluded_carrier(real_html, monkeypatch):
    """exclude_carriers matches Segment.carrier case-insensitively."""
    import providers.google as google

    async def fake_fetch(*args, **kwargs):
        return real_html

    monkeypatch.setattr(google, "fetch_html", fake_fetch)
    offers = await GoogleProvider().search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06", exclude_carriers=("fr",))
    )
    assert len(offers) == 2
    assert all(s.carrier.upper() != "FR" for o in offers for s in o.segments)


async def test_search_leg_raises_for_children():
    """Google cannot express a passenger count with children."""
    with pytest.raises(ProviderError, match="children"):
        await GoogleProvider().search_leg(
            LegQuery(origin="LPA", dest="MAD", date="2026-10-06", children=1)
        )


async def test_search_leg_raises_for_non_economy_cabin():
    """encode_tfs hardcodes economy; a different cabin would be silently wrong."""
    with pytest.raises(ProviderError, match="BUSINESS"):
        await GoogleProvider().search_leg(
            LegQuery(origin="LPA", dest="MAD", date="2026-10-06", cabin="BUSINESS")
        )


async def test_search_leg_raises_for_min_layover():
    """Google's per-airport local times cannot be differenced across a connection."""
    with pytest.raises(ProviderError, match="min_layover"):
        await GoogleProvider().search_leg(
            LegQuery(origin="LPA", dest="MAD", date="2026-10-06", min_layover=90)
        )
