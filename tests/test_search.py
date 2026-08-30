"""Tests for the search orchestrator: discount maths, concurrency, formatting."""
from __future__ import annotations

import asyncio

import pytest

import search as search_module
from models import Route, add_days
from providers.google import FetchError, FlightResult, ParseError
from search import format_results, routes_to_json, run_search


def _flight(price: int, airline: str = "Iberia") -> FlightResult:
    return FlightResult(price=price, airlines=[airline], stops=0, duration=120)


@pytest.fixture
def fake_legs(monkeypatch):
    """Replace the network layer with a scripted price table.

    Returns the dict of recorded calls so tests can assert on which legs were
    queried, and accepts a {(from, to): price} table to drive results.
    """
    prices: dict[tuple[str, str], int] = {}
    calls: list[tuple[str, str, str]] = []
    in_flight = {"now": 0, "peak": 0}

    async def fake_search(from_apt, to_apt, date, adults=1, currency="EUR", **kwargs):
        calls.append((from_apt, to_apt, date))
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        try:
            await asyncio.sleep(0)  # yield, so overlap is observable
            price = prices.get((from_apt, to_apt))
            if price is None:
                return []
            return [_flight(price)]
        finally:
            in_flight["now"] -= 1

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(search_module, "search", fake_search)
    monkeypatch.setattr(search_module, "build_client", lambda: FakeClient())
    return {"prices": prices, "calls": calls, "in_flight": in_flight}


# ── Discount maths ──────────────────────────────────────────────────────────


async def test_discount_applies_only_to_the_qualifying_hub(fake_legs, monkeypatch):
    """MAD qualifies for the discount; LIS (Portugal) does not."""
    monkeypatch.setattr(search_module, "DISCOUNT_AIRPORTS", {"MAD"})
    monkeypatch.setattr(search_module, "DOMESTIC_DISCOUNT", 0.75)

    fake_legs["prices"].update({
        ("LPA", "MAD"): 100,
        ("LPA", "LIS"): 100,
        ("MAD", "NRT"): 500,
        ("LIS", "NRT"): 500,
    })

    routes = await run_search(
        origin="LPA",
        destinations={"NRT": "Tokyo"},
        dates=["2026-09-01"],
        hubs={"MAD": "Madrid", "LIS": "Lisboa"},
        delay=0,
    )

    by_hub = {r.hub: r for r in routes}
    # MAD: 100 * (1 - 0.75) + 500 = 525
    assert by_hub["MAD"].dom_discounted == pytest.approx(25.0)
    assert by_hub["MAD"].total == pytest.approx(525.0)
    # LIS: no discount, so the full 100 counts
    assert by_hub["LIS"].dom_discounted == pytest.approx(100.0)
    assert by_hub["LIS"].total == pytest.approx(600.0)


async def test_results_are_sorted_cheapest_first(fake_legs, monkeypatch):
    monkeypatch.setattr(search_module, "DISCOUNT_AIRPORTS", set())
    fake_legs["prices"].update({
        ("LPA", "MAD"): 100,
        ("LPA", "BCN"): 50,
        ("MAD", "NRT"): 500,
        ("BCN", "NRT"): 700,
    })

    routes = await run_search(
        origin="LPA",
        destinations={"NRT": "Tokyo"},
        dates=["2026-09-01"],
        hubs={"MAD": "Madrid", "BCN": "Barcelona"},
        delay=0,
    )

    totals = [r.total for r in routes]
    assert totals == sorted(totals)
    assert routes[0].hub == "MAD"  # 600 beats BCN's 750


async def test_round_trip_totals_include_all_four_legs(fake_legs, monkeypatch):
    monkeypatch.setattr(search_module, "DISCOUNT_AIRPORTS", set())
    fake_legs["prices"].update({
        ("LPA", "MAD"): 100,   # outbound domestic
        ("MAD", "LPA"): 110,   # return domestic
        ("MAD", "NRT"): 500,   # outbound international
        ("NRT", "MAD"): 520,   # return international
    })

    routes = await run_search(
        origin="LPA",
        destinations={"NRT": "Tokyo"},
        dates=["2026-09-01"],
        hubs={"MAD": "Madrid"},
        delay=0,
        trip_days=14,
    )

    assert len(routes) == 1
    route = routes[0]
    assert route.dom_price == 210      # 100 + 110
    assert route.intl_price == 1020    # 500 + 520
    assert route.total == pytest.approx(1230.0)
    assert route.return_date == "2026-09-15"


async def test_return_legs_are_queried_on_the_return_date(fake_legs):
    fake_legs["prices"].update({
        ("LPA", "MAD"): 100, ("MAD", "LPA"): 100,
        ("MAD", "NRT"): 500, ("NRT", "MAD"): 500,
    })

    await run_search(
        origin="LPA",
        destinations={"NRT": "Tokyo"},
        dates=["2026-09-01"],
        hubs={"MAD": "Madrid"},
        delay=0,
        trip_days=14,
    )

    calls = fake_legs["calls"]
    assert ("MAD", "LPA", "2026-09-15") in calls
    assert ("NRT", "MAD", "2026-09-15") in calls
    # Return legs are searched as separate one-ways, on the return date only.
    assert ("MAD", "LPA", "2026-09-01") not in calls


# ── Phase narrowing and concurrency ─────────────────────────────────────────


async def test_unreachable_hubs_are_not_queried_for_onward_flights(fake_legs):
    """Phase 2 must skip hubs phase 1 found no flights to."""
    fake_legs["prices"].update({
        ("LPA", "MAD"): 100,
        ("MAD", "NRT"): 500,
        # No LPA->BCN, so BCN is unreachable.
        ("BCN", "NRT"): 400,
    })

    routes = await run_search(
        origin="LPA",
        destinations={"NRT": "Tokyo"},
        dates=["2026-09-01"],
        hubs={"MAD": "Madrid", "BCN": "Barcelona"},
        delay=0,
    )

    assert [r.hub for r in routes] == ["MAD"]
    assert ("BCN", "NRT", "2026-09-01") not in fake_legs["calls"]


async def test_empty_first_phase_short_circuits_the_search(fake_legs):
    routes = await run_search(
        origin="LPA",
        destinations={"NRT": "Tokyo"},
        dates=["2026-09-01"],
        hubs={"MAD": "Madrid"},
        delay=0,
    )
    assert routes == []
    # Only phase 1 ran; no onward queries were attempted.
    assert all(call[0] == "LPA" for call in fake_legs["calls"])


async def test_requests_run_concurrently_up_to_the_cap(fake_legs):
    dates = [f"2026-09-{d:02d}" for d in range(1, 11)]
    fake_legs["prices"][("LPA", "MAD")] = 100

    await run_search(
        origin="LPA",
        destinations={},
        dates=dates,
        hubs={"MAD": "Madrid"},
        delay=0,
        concurrency=4,
    )

    peak = fake_legs["in_flight"]["peak"]
    assert peak > 1, "the whole point of the rewrite is that legs overlap"
    assert peak <= 4, f"concurrency cap exceeded: {peak} requests in flight"


async def test_scraper_failures_do_not_abort_the_whole_search(monkeypatch):
    """A blocked or unparseable leg is logged and skipped, not fatal."""
    async def flaky_search(from_apt, to_apt, date, adults=1, currency="EUR", **kwargs):
        if to_apt == "BCN":
            raise ParseError("layout changed")
        if to_apt == "AGP":
            raise FetchError("HTTP 429")
        if (from_apt, to_apt) == ("LPA", "MAD"):
            return [_flight(100)]
        if (from_apt, to_apt) == ("MAD", "NRT"):
            return [_flight(500)]
        return []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(search_module, "search", flaky_search)
    monkeypatch.setattr(search_module, "build_client", lambda: FakeClient())
    monkeypatch.setattr(search_module, "DISCOUNT_AIRPORTS", set())

    routes = await run_search(
        origin="LPA",
        destinations={"NRT": "Tokyo"},
        dates=["2026-09-01"],
        hubs={"MAD": "Madrid", "BCN": "Barcelona", "AGP": "Malaga"},
        delay=0,
    )

    assert [r.hub for r in routes] == ["MAD"]


# ── Serialization and formatting ─────────────────────────────────────────────


def _route(**kw) -> Route:
    base = {
        "date": "2026-09-01", "hub": "MAD", "hub_name": "Madrid",
        "dest": "NRT", "dest_name": "Tokyo",
        "dom_price": 100, "dom_discounted": 25.0, "intl_price": 500, "total": 525.0,
        "dom_airlines": ["Iberia"], "intl_airlines": ["ANA"],
    }
    base.update(kw)
    return Route(**base)


def test_routes_to_json_caps_at_25_entries():
    import json
    routes = [_route(total=float(i)) for i in range(40)]
    assert len(json.loads(routes_to_json(routes))) == 25


def test_format_results_handles_no_routes():
    assert "No routes found" in format_results([], "LPA")


def test_format_results_includes_booking_links_and_discount_note(monkeypatch):
    monkeypatch.setattr(search_module, "DISCOUNT_AIRPORTS", {"MAD"})
    monkeypatch.setattr(search_module, "DOMESTIC_DISCOUNT", 0.75)

    out = format_results([_route()], "LPA", "EUR")

    assert "One-way" in out
    assert "525 EUR" in out
    assert "75% disc." in out
    assert "https://www.google.com/travel/flights/search?tfs=" in out
    assert "Book legs separately" in out


def test_format_results_marks_non_discounted_hubs(monkeypatch):
    monkeypatch.setattr(search_module, "DISCOUNT_AIRPORTS", {"MAD"})
    out = format_results([_route(hub="LIS", hub_name="Lisboa")], "LPA")
    assert "no disc." in out


def test_format_output_stays_within_telegram_limits_for_a_large_search():
    """Each block must be individually sendable after splitting."""
    from handlers.utils import split_message

    routes = [
        _route(date=f"2026-09-{d:02d}", hub=hub, dest=dest)
        for d in range(1, 15)
        for hub in ("MAD", "BCN", "AGP")
        for dest in ("NRT", "JFK")
    ]
    chunks = split_message(format_results(routes, "LPA"))
    assert all(len(c) <= 4096 for c in chunks)


def test_add_days_matches_models_helper():
    assert add_days("2026-09-01", 14) == "2026-09-15"
    assert add_days("2026-12-25", 10) == "2027-01-04"
