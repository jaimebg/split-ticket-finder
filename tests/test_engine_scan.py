"""Tests for phase 0: pricing a whole window from calendars."""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.scan import CalendarGrid, scan_calendars
from models import CancelToken, SearchCancelled, SearchWindow
from providers.base import ProviderError, ProviderFetchError, ProviderParseError, RatedPrice


class FakeCalendarProvider:
    name = "fake"

    def __init__(self, prices=None, errors=None):
        # prices: {(origin, dest): {date: "29"}}
        # errors: {(origin, dest): Exception} -- raised instead of returning a table
        self.prices = prices or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, str, str, str]] = []

    async def search_leg(self, query):
        return []

    async def price_calendar(self, query):
        self.calls.append((query.origin, query.dest, query.start, query.end))
        key = (query.origin, query.dest)
        if key in self.errors:
            raise self.errors[key]
        table = self.prices.get(key, {})
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


async def test_a_parse_error_drops_only_that_leg_and_is_counted():
    """A broken calendar is dropped and counted; the rest of the grid is intact."""
    provider = FakeCalendarProvider(
        prices={("MAD", "NRT"): {"2026-10-01": "575"}},
        errors={("LPA", "MAD"): ProviderParseError("schema moved")},
    )
    grid = await scan_calendars(
        provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
        window=WINDOW, trip_days=0, adults=1, currency="EUR",
    )
    assert grid.parse_errors == 1
    assert grid.fetch_errors == 0
    assert "MAD" not in grid.out_dom
    # the leg that succeeded is unaffected by the one that failed
    assert grid.out_onward[("MAD", "NRT")]["2026-10-01"].price == Decimal("575")


async def test_a_fetch_error_drops_only_that_leg_and_is_counted_separately():
    provider = FakeCalendarProvider(
        prices={("MAD", "NRT"): {"2026-10-01": "575"}},
        errors={("LPA", "MAD"): ProviderFetchError("timeout")},
    )
    grid = await scan_calendars(
        provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
        window=WINDOW, trip_days=0, adults=1, currency="EUR",
    )
    assert grid.fetch_errors == 1
    assert grid.parse_errors == 0
    assert "MAD" not in grid.out_dom
    assert grid.out_onward[("MAD", "NRT")]["2026-10-01"].price == Decimal("575")


async def test_a_clean_scan_leaves_both_error_counters_at_zero():
    provider = FakeCalendarProvider({("LPA", "MAD"): {"2026-10-01": "29"}})
    grid = await scan_calendars(
        provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
        window=WINDOW, trip_days=0, adults=1, currency="EUR",
    )
    assert grid.parse_errors == 0
    assert grid.fetch_errors == 0


async def test_a_bare_provider_error_propagates_and_is_not_counted():
    """Mirrors LegFetcher's three-way contract: a bare ProviderError means the
    provider cannot answer this *kind* of query at all, so every leg pair would
    fail identically -- it must abort the scan rather than being counted like a
    per-leg parse/fetch failure."""
    provider = FakeCalendarProvider(
        errors={("LPA", "MAD"): ProviderError("cannot express this query")},
    )
    with pytest.raises(ProviderError):
        await scan_calendars(
            provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
            window=WINDOW, trip_days=0, adults=1, currency="EUR",
        )
