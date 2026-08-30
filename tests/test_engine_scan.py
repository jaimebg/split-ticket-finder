"""Tests for phase 0: pricing a whole window from calendars."""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.scan import CalendarGrid, scan_calendars
from models import CancelToken, SearchCancelled, SearchWindow
from providers.base import RatedPrice


class FakeCalendarProvider:
    name = "fake"

    def __init__(self, prices=None):
        # prices: {(origin, dest): {date: "29"}}
        self.prices = prices or {}
        self.calls: list[tuple[str, str, str, str]] = []

    async def search_leg(self, query):
        return []

    async def price_calendar(self, query):
        self.calls.append((query.origin, query.dest, query.start, query.end))
        table = self.prices.get((query.origin, query.dest), {})
        return {d: RatedPrice(price=Decimal(p), rating="AVERAGE") for d, p in table.items()}

    async def aclose(self):
        return None


WINDOW = SearchWindow(start="2026-10-01", end="2026-10-03")


async def test_one_way_issues_one_calendar_per_leg_pair():
    """H + H*D requests, independent of how long the window is."""
    provider = FakeCalendarProvider()
    await scan_calendars(
        provider, origin="LPA", hubs=["MAD", "BCN"], dests=["NRT", "JFK"],
        window=WINDOW, trip_days=0, adults=1, currency="EUR",
    )
    # 2 hubs (origin->hub) + 2 hubs x 2 dests (hub->dest) = 6
    assert len(provider.calls) == 6
    assert ("LPA", "MAD", "2026-10-01", "2026-10-03") in provider.calls
    assert ("MAD", "NRT", "2026-10-01", "2026-10-03") in provider.calls


async def test_round_trip_doubles_the_calls_over_a_shifted_window():
    provider = FakeCalendarProvider()
    await scan_calendars(
        provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
        window=WINDOW, trip_days=14, adults=1, currency="EUR",
    )
    # outbound: LPA->MAD, MAD->NRT.  return: MAD->LPA, NRT->MAD, shifted 14 days.
    assert len(provider.calls) == 4
    assert ("MAD", "LPA", "2026-10-15", "2026-10-17") in provider.calls
    assert ("NRT", "MAD", "2026-10-15", "2026-10-17") in provider.calls


async def test_request_count_does_not_grow_with_window_length():
    """The whole premise: 91 days costs what 3 days costs."""
    short = FakeCalendarProvider()
    long = FakeCalendarProvider()
    kw = {"origin": "LPA", "hubs": ["MAD", "BCN"], "dests": ["NRT"],
          "trip_days": 0, "adults": 1, "currency": "EUR"}
    await scan_calendars(short, window=SearchWindow("2026-10-01", "2026-10-03"), **kw)
    await scan_calendars(long, window=SearchWindow("2026-10-01", "2026-12-30"), **kw)
    assert len(short.calls) == len(long.calls)


async def test_grid_exposes_prices_by_leg_and_date():
    provider = FakeCalendarProvider({
        ("LPA", "MAD"): {"2026-10-01": "29", "2026-10-02": "48"},
        ("MAD", "NRT"): {"2026-10-01": "575"},
    })
    grid: CalendarGrid = await scan_calendars(
        provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
        window=WINDOW, trip_days=0, adults=1, currency="EUR",
    )
    assert grid.out_dom["MAD"]["2026-10-01"].price == Decimal("29")
    assert grid.out_onward[("MAD", "NRT")]["2026-10-01"].price == Decimal("575")
    assert grid.ret_dom == {}
    assert grid.ret_onward == {}


async def test_a_hub_with_no_calendar_data_is_simply_absent():
    """No flights is data, not an error."""
    provider = FakeCalendarProvider({("LPA", "MAD"): {"2026-10-01": "29"}})
    grid: CalendarGrid = await scan_calendars(
        provider, origin="LPA", hubs=["MAD", "BCN"], dests=["NRT"],
        window=WINDOW, trip_days=0, adults=1, currency="EUR",
    )
    assert "MAD" in grid.out_dom
    assert grid.out_dom.get("BCN", {}) == {}


async def test_scan_is_cancellable():
    provider = FakeCalendarProvider()
    token = CancelToken()
    token.cancel()
    with pytest.raises(SearchCancelled):
        await scan_calendars(
            provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
            window=WINDOW, trip_days=0, adults=1, currency="EUR", cancel=token,
        )
    assert provider.calls == []
