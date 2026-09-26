"""split_ticket_mcp.server through an in-memory MCP client. No network."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import split_ticket_mcp.server as server
from mcp import Client

from providers.base import ALL_CABINS, Capabilities, Place, ProviderError, RatedPrice
from tests.results_fixtures import offer, one_way, onward_out, seg, standard_one_way

D1 = str(date.today() + timedelta(days=10))
D2 = str(date.today() + timedelta(days=12))


class FakeCalendarProvider:
    name = "kiwi"
    capabilities = Capabilities(cabins=frozenset(ALL_CABINS), children=True, min_layover=True)

    def __init__(self, table=None, error=None):
        self.table, self.error, self.queries = table or {}, error, []

    async def price_calendar(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.table

    async def search_leg(self, query):
        return []

    async def resolve_place(self, term, limit=8):
        return [Place(code="NRT", name="Narita", city="Tokyo", country="Japan", place_id="x")]


@pytest.fixture
def engine(monkeypatch):
    state = {"itineraries": [], "errors": 0, "calls": [], "raise": None}

    async def fake_run_search(**kwargs):
        state["calls"].append(kwargs)
        if state["raise"]:
            raise state["raise"]
        return SimpleNamespace(itineraries=state["itineraries"], strategy="two-stage",
                               scan=None, parse_errors=state["errors"], fetch_errors=0)

    monkeypatch.setattr(server, "run_search", fake_run_search)
    monkeypatch.setattr(server, "primary_provider", lambda: FakeCalendarProvider())
    return state


async def _call(tool, args):
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, args)


def _error(result) -> str:
    assert result.is_error
    return " ".join(getattr(b, "text", "") for b in result.content)


async def test_the_server_offers_exactly_three_tools():
    async with Client(server.mcp) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
    assert names == ["find_airports", "price_calendar", "search_split_tickets"]


async def test_a_search_returns_structured_itineraries(engine):
    engine["itineraries"] = [standard_one_way(date=D1, through_fare=Decimal("700")),
                             standard_one_way(date=D2)]
    result = await _call("search_split_tickets", {"destinations": ["nrt"], "window_start": D1,
                                                  "window_end": D2, "cabin": "business",
                                                  "adults": 2, "limit": 1})
    assert not result.is_error
    out = result.structured_content
    assert out["routes_found"] == 2 and len(out["itineraries"]) == 1
    assert out["itineraries"][0]["total"] == 525.0
    call = engine["calls"][0]
    assert call["destinations"] == {"NRT": "NRT"}
    assert (call["options"].cabin, call["options"].adults) == ("BUSINESS", 2)


async def test_results_on_dates_nobody_asked_for_are_dropped(engine):
    """Review Focus #3."""
    engine["itineraries"] = [standard_one_way(date=D1),
                             standard_one_way(date=str(date.today() + timedelta(days=40)))]
    out = (await _call("search_split_tickets", {"destinations": ["NRT"], "window_start": D1,
                                                "window_end": D2})).structured_content
    assert out["routes_found"] == 1


async def test_hide_risky_can_hide_everything_without_erroring(engine):
    """Review Focus #4."""
    tight = one_way(dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T12:00")),
                    onward=onward_out(), date=D1)
    engine["itineraries"] = [tight]
    result = await _call("search_split_tickets", {"destinations": ["NRT"], "window_start": D1,
                                                  "window_end": D1, "hide_risky": True})
    assert not result.is_error
    assert result.structured_content["itineraries"] == []
    assert result.structured_content["hidden_as_risky"] == 1


async def test_failed_requests_are_reported(engine):
    engine["errors"] = 7
    out = (await _call("search_split_tickets", {"destinations": ["NRT"], "window_start": D1,
                                                "window_end": D1})).structured_content
    assert out["failed_requests"] == 7 and out["itineraries"] == []


@pytest.mark.parametrize(("args", "sentence"), [
    ({"destinations": ["TOKYO"]}, "is not a 3-letter IATA airport code"),
    ({"destinations": []}, "between 1 and 10 destinations"),
    ({"window_start": "01/10/2026"}, "is not a YYYY-MM-DD date"),
    ({"window_start": D2, "window_end": D1}, "ends before it starts"),
    ({"adults": 0}, "at least one adult"),
    ({"adults": 8, "children": 2}, "at most 9 passengers"),
    ({"cabin": "COACH"}, "is not a cabin"),
    ({"limit": 99}, "limit must be between 1 and 30"),
])
async def test_bad_input_is_a_sentence_not_a_search(engine, args, sentence):
    base = {"destinations": ["NRT"], "window_start": D1, "window_end": D2}
    text = _error(await _call("search_split_tickets", {**base, **args}))
    assert sentence in text
    assert engine["calls"] == []


async def test_a_past_window_or_today_overnight_is_refused(engine):
    """Review Focus #1."""
    past = str(date.today() - timedelta(days=5))
    assert "No searchable dates" in _error(await _call(
        "search_split_tickets",
        {"destinations": ["NRT"], "window_start": past, "window_end": past}))
    today = str(date.today())
    text = _error(await _call("search_split_tickets", {"destinations": ["NRT"],
                                                       "window_start": today,
                                                       "window_end": today,
                                                       "overnight": True}))
    assert "earliest international flight is tomorrow" in text
    assert engine["calls"] == []


async def test_an_option_the_source_cannot_search_is_refused(engine, monkeypatch):
    class GoogleLike(FakeCalendarProvider):
        capabilities = Capabilities(cabins=frozenset({"ECONOMY"}), children=False,
                                    min_layover=False)

    monkeypatch.setattr(server, "primary_provider", lambda: GoogleLike())
    text = _error(await _call("search_split_tickets", {"destinations": ["NRT"], "window_start": D1,
                                                       "window_end": D1, "cabin": "BUSINESS"}))
    assert "can't search Business class" in text


@pytest.mark.parametrize(("exc", "sentence"), [
    (ProviderError("cabin"), "can't run this search with these options"),
    (ValueError("window covers 120 days, more than the 91-day limit"), "91-day limit"),
])
async def test_engine_failures_become_sentences(engine, exc, sentence):
    """Review Focus #2."""
    engine["raise"] = exc
    text = _error(await _call("search_split_tickets", {"destinations": ["NRT"],
                                                       "window_start": D1, "window_end": D1}))
    assert sentence in text


async def test_the_price_calendar(monkeypatch):
    provider = FakeCalendarProvider(table={
        D2: RatedPrice(price=Decimal("48"), rating="AVERAGE"),
        D1: RatedPrice(price=Decimal("29"), rating="CHEAP")})
    monkeypatch.setattr(server, "primary_provider", lambda: provider)
    result = await _call("price_calendar", {"origin": "LPA", "dest": "MAD", "start": D1,
                                            "end": D2, "max_stops": 0})
    days = result.structured_content["days"]
    assert [d["date"] for d in days] == [D1, D2] and days[0]["price"] == 29.0
    assert provider.queries[0].max_stops == 0


async def test_no_calendar_is_a_sentence(monkeypatch):
    class NoCalendar:
        name = "google"

        async def search_leg(self, query):
            return []

    monkeypatch.setattr(server, "primary_provider", lambda: NoCalendar())
    text = _error(await _call("price_calendar", {"origin": "LPA", "dest": "MAD",
                                                 "start": D1, "end": D2}))
    assert "has no price calendar" in text


async def test_find_airports_and_its_errors(monkeypatch):
    monkeypatch.setattr(server, "places_provider", lambda: FakeCalendarProvider())
    out = (await _call("find_airports", {"query": "Tokio"})).structured_content
    assert out["airports"][0]["code"] == "NRT"
    assert "at least 2 characters" in _error(await _call("find_airports", {"query": "T"}))
    monkeypatch.setattr(server, "places_provider", lambda: None)
    assert "pass IATA codes instead" in _error(await _call("find_airports", {"query": "Tokio"}))
