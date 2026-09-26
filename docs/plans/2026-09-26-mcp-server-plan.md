# MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local stdio MCP server, `split-ticket-mcp`, that exposes three read-only tools: `search_split_tickets`, `price_calendar` and `find_airports`.

**Architecture:** `split_ticket_mcp/mapping.py` turns engine `Itinerary` objects into Pydantic models an LLM can read; it is pure and testable without MCP. `split_ticket_mcp/server.py` holds an `MCPServer` with three async tools. Each one validates its input, then calls exactly what the bot calls (`engine.run_search`, the provider's `price_calendar` / `resolve_place`, `results.filters`). `main()` routes logging to stderr and runs stdio.

**Tech Stack:** Python 3.10+, `mcp` 2.2 (`mcp.server.MCPServer`, `mcp.Client`, `mcp.server.mcpserver.exceptions.ToolError`), Pydantic (it ships with `mcp`), pytest (`asyncio_mode = "auto"`).

**Spec:** `docs/plans/2026-09-26-mcp-server-design.md`

## Global Constraints

- `mcp>=2.2,<3` goes only in the new `mcp` extra and in `dev`, **never** in core `dependencies`. The bot's production install must not change.
- stdout is the protocol channel. Nothing in the package prints, and `main()` sends logging to stderr.
- Every failure a person could cause raises `ToolError(<sentence>)`, never a bare exception.
- A ticket with no real offer is listed with `price`/`paid` and every flight field `null`. Per-ticket estimates don't exist: the engine's estimate prices sum both directions of a side. This is a deliberate narrowing of spec §2, which said "price taken from the calendar estimate".
- The tools use no database: no favourites, no history, no place cache.
- Branch `feat/mcp-server`. Commit per task. **Never push or merge to `main`.** `ruff check .` and `.venv/bin/pytest -q` are clean after every task. E402 is off, so don't write `# noqa: E402`.

## Review Focus

1. **A window that starts in the past, or starts today with `overnight`.** The past days are dropped. When nothing is left, the tool errors with a sentence, and nothing is searched. (Task 2)
2. **Engine failures.** A bare `ProviderError` escaping the engine, or `run_search`'s own `ValueError` for an oversized window, becomes a `ToolError` with a sentence, not a stack trace. (Task 2)
3. **Results on dates nobody asked for.** The engine can return itineraries outside the searched dates (3b's C1 rule). They are filtered out before counting and mapping. (Task 2)
4. **`hide_risky` removing everything.** The tool returns `itineraries: []` with `hidden_as_risky` = the count, not an error. (Task 2)
5. **Importing the package prints nothing to stdout.** It is checked in a subprocess. (Task 3)

---

### Task 1: Packaging and the output mapping

**Files:**
- Modify: `pyproject.toml`
- Create: `split_ticket_mcp/__init__.py`, `split_ticket_mcp/mapping.py`
- Test: `tests/test_mcp_mapping.py` (new), `tests/test_deploy_artifacts.py`

**Interfaces:**
- Produces:
  - `TicketOut`, `ItineraryOut` (Pydantic)
  - `itinerary_out(itin: Itinerary) -> ItineraryOut`

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_mapping.py`:

```python
"""split_ticket_mcp.mapping: engine itineraries as LLM-readable models."""
from __future__ import annotations

from decimal import Decimal

from split_ticket_mcp.mapping import itinerary_out
from tests.results_fixtures import offer, one_way, onward_out, seg, standard_one_way, standard_round_trip


def test_a_confirmed_one_way():
    out = itinerary_out(standard_one_way(through_fare=Decimal("700")))
    assert out.status == "confirmed"
    assert out.total == 525.0
    assert out.savings == 175.0
    assert out.domestic_discount_pct == 75
    assert out.risk == "medium" and out.risk_reasons == ["3h00m between tickets at MAD"]
    dom, onward = out.tickets
    assert (dom.role, dom.price, dom.paid) == ("domestic out", 100.0, 25.0)
    assert (onward.role, onward.price, onward.paid) == ("onward out", 500.0, 500.0)
    assert dom.flights == ["IB100"] and dom.departs == "2026-10-01T07:00:00"
    assert dom.booking_url == "https://example.test/book"
    assert dom.bags == "cabin unknown, checked unknown"      # unknown stays unknown


def test_a_cheaper_single_ticket_is_a_negative_saving():
    assert itinerary_out(standard_one_way(through_fare=Decimal("500"))).savings == -25.0


def test_a_round_trip_lists_four_tickets_in_travel_order():
    roles = [t.role for t in itinerary_out(standard_round_trip()).tickets]
    assert roles == ["domestic out", "onward out", "onward return", "domestic return"]


def test_a_missing_offer_is_listed_with_nothing_bookable():
    partial = one_way(dom=standard_one_way().dom_out)
    out = itinerary_out(partial)
    assert out.status == "partial"
    missing = out.tickets[1]
    assert (missing.price, missing.paid, missing.booking_url, missing.flights) == (None, None, None, [])


def test_an_estimate_has_no_risk_and_no_bookable_ticket():
    est = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    out = itinerary_out(est)
    assert out.status == "estimate" and out.risk is None
    assert all(t.booking_url is None for t in out.tickets)


def test_overnight_dates():
    night = one_way(dom=offer("100", seg("LPA", "MAD", "2026-09-30T18:00", "2026-09-30T21:00")),
                    onward=onward_out(), overnight=True)
    out = itinerary_out(night)
    assert (out.date, out.dom_date, out.overnight) == ("2026-10-01", "2026-09-30", True)
```

Append to `tests/test_deploy_artifacts.py`:

```python
def test_the_mcp_server_is_an_optional_extra_with_a_console_script():
    """The bot's production install (pip install -e .) must not pull mcp."""
    import re

    text = (DEPLOY.parent / "pyproject.toml").read_text()
    core = re.search(r"^dependencies = \[(.*?)\]", text, re.S | re.M).group(1)
    assert "mcp" not in core
    assert re.search(r'^mcp = \["mcp>=2\.2,<3"\]', text, re.M)
    assert 'split-ticket-mcp = "split_ticket_mcp.server:main"' in text
    assert '"split_ticket_mcp*"' in text
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_mcp_mapping.py tests/test_deploy_artifacts.py -q`
Expected: collection error `No module named 'split_ticket_mcp'`, and the artifact test fails.

- [ ] **Step 3: Implement**

`pyproject.toml`:
- In `[project.optional-dependencies]`, add `"mcp>=2.2,<3",` to `dev`, and a new line `mcp = ["mcp>=2.2,<3"]`.
- Add:

  ```toml
  [project.scripts]
  split-ticket-mcp = "split_ticket_mcp.server:main"
  ```

- In `[tool.setuptools.packages.find]`, extend `include` with `"split_ticket_mcp*"`.

Then run `.venv/bin/pip install -q -e ".[dev]"`.

`split_ticket_mcp/__init__.py`:

```python
"""A local MCP server exposing the split-ticket engine as read-only tools."""
```

`split_ticket_mcp/mapping.py`:

```python
"""Engine itineraries as models an assistant can read.

Pure: no MCP, no I/O. The rules match the Telegram view. Savings keep their
sign (negative when the single ticket is cheaper). Unknown bags stay unknown.
A ticket with no real offer carries no price and nothing bookable, so an
assistant can't present it as a fare you could buy.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from engine.risk import itinerary_risk
from models import Itinerary
from providers.base import Offer
from results.view import bags_text

_CENTS = Decimal("0.01")


class TicketOut(BaseModel):
    role: str
    price: float | None = None           # before the discount
    paid: float | None = None            # after it; only domestic tickets differ
    from_airport: str | None = None
    to_airport: str | None = None
    stops: int | None = None
    duration_minutes: int | None = None
    flights: list[str] = []
    departs: str | None = None           # local ISO datetime, first segment
    arrives: str | None = None           # local ISO datetime, last segment
    booking_url: str | None = None
    bags: str | None = None


class ItineraryOut(BaseModel):
    total: float
    status: str
    date: str
    return_date: str
    dom_date: str
    dom_return_date: str
    hub: str
    hub_name: str
    dest: str
    dest_name: str
    overnight: bool
    domestic_discount_pct: int
    through_fare: float | None
    savings: float | None
    risk: str | None
    risk_reasons: list[str]
    tickets: list[TicketOut]


def _ticket(role: str, offer: Offer | None, discount: Decimal) -> TicketOut:
    if offer is None:
        return TicketOut(role=role)
    first, last = (offer.segments[0], offer.segments[-1]) if offer.segments else (None, None)
    paid = (offer.price * (Decimal(1) - discount)).quantize(_CENTS)
    return TicketOut(
        role=role,
        price=float(offer.price),
        paid=float(paid),
        from_airport=first.origin if first else None,
        to_airport=last.dest if last else None,
        stops=offer.stops,
        duration_minutes=offer.duration,
        flights=[s.flight_no for s in offer.segments],
        departs=first.dep_local.isoformat() if first and first.dep_local else None,
        arrives=last.arr_local.isoformat() if last and last.arr_local else None,
        booking_url=offer.booking_url,
        bags=bags_text(offer),
    )


def itinerary_out(itin: Itinerary) -> ItineraryOut:
    judged = itinerary_risk(itin)
    tickets = [
        _ticket("domestic out", itin.dom_out, itin.discount),
        _ticket("onward out", itin.onward_out, Decimal(0)),
    ]
    if itin.return_date:
        tickets += [
            _ticket("onward return", itin.onward_ret, Decimal(0)),
            _ticket("domestic return", itin.dom_ret, itin.discount),
        ]
    return ItineraryOut(
        total=float(itin.total),
        status=itin.status,
        date=itin.date,
        return_date=itin.return_date,
        dom_date=itin.dom_date,
        dom_return_date=itin.dom_return_date,
        hub=itin.hub,
        hub_name=itin.hub_name,
        dest=itin.dest,
        dest_name=itin.dest_name,
        overnight=itin.overnight,
        domestic_discount_pct=int(itin.discount * 100),
        through_fare=float(itin.through_fare) if itin.through_fare is not None else None,
        savings=float(itin.savings) if itin.savings is not None else None,
        risk=judged[0].name.lower() if judged else None,
        risk_reasons=judged[1] if judged else [],
        tickets=tickets,
    )
```

Before writing `mapping.py`, rename `_bags` to `bags_text` in `results/view.py` (the definition and its one caller in `_ticket`). The mapping reuses it so the bag wording matches the Telegram detail exactly, and nothing imports a private name.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: package the MCP server as an optional extra; map itineraries for it"
```

---

### Task 2: The server and its three tools

**Files:**
- Create: `split_ticket_mcp/server.py`
- Test: `tests/test_mcp_server.py` (new)

**Interfaces:**
- Consumes: `itinerary_out`, `ItineraryOut` (Task 1); `engine.run_search`; `providers.registry.primary_provider`; `handlers.search.places.places_provider`; `results.filters.Filters`, `apply`; `models.SearchWindow`, `bookable_onward_dates`; `providers.base.SearchOptions`, `ALL_CABINS`, `SupportsCalendar`, `ProviderError`, `capabilities_of`.
- Produces: `split_ticket_mcp.server.mcp` (the `MCPServer`) and `main()`.

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_server.py`:

```python
"""split_ticket_mcp.server through an in-memory MCP client. No network."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from mcp import Client

import split_ticket_mcp.server as server
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
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -q`
Expected: collection error `No module named 'split_ticket_mcp.server'`.

- [ ] **Step 3: Implement `split_ticket_mcp/server.py`**

```python
"""A local MCP server: the split-ticket engine as tools for an AI assistant.

Read-only, no database. Each tool validates its input and then calls exactly
what the Telegram bot calls. Anything a person could get wrong comes back as
a ToolError with a sentence. stdout carries the protocol, so nothing here
prints and main() sends logging to stderr.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import date

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

import config
from engine import run_search
from handlers.search.places import places_provider
from models import SearchWindow, bookable_onward_dates
from providers.base import (
    ALL_CABINS,
    ProviderError,
    SearchOptions,
    SupportsCalendar,
    capabilities_of,
)
from providers.registry import primary_provider
from results.filters import Filters, apply
from split_ticket_mcp.mapping import ItineraryOut, itinerary_out

mcp = MCPServer(
    "split-ticket-finder",
    instructions=(
        "Find flights cheaper than the airline's own fare by splitting one "
        "journey into a discounted domestic ticket plus an onward ticket via a "
        "hub. Use find_airports to turn place names into IATA codes, "
        "price_calendar to explore dates cheaply, and search_split_tickets "
        "for the real search, which is expensive."
    ),
)

_IATA = re.compile(r"^[A-Z]{3}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
MAX_PASSENGERS = 9
MAX_DESTINATIONS = 10


def _code(value: str, what: str) -> str:
    code = value.strip().upper()
    if not _IATA.match(code):
        raise ToolError(f"{what} {value!r} is not a 3-letter IATA airport code.")
    return code


def _day(value: str, what: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{what} {value!r} is not a YYYY-MM-DD date.") from None


def _options(*, adults: int, children: int, cabin: str, currency: str,
             max_stops: int | None = None, min_layover: int | None = None,
             overnight: bool = False) -> SearchOptions:
    if adults < 1:
        raise ToolError("A search needs at least one adult.")
    if children < 0 or adults + children > MAX_PASSENGERS:
        raise ToolError(f"A search holds at most {MAX_PASSENGERS} passengers.")
    cabin = cabin.strip().upper()
    if cabin not in ALL_CABINS:
        raise ToolError(f"{cabin!r} is not a cabin; use one of {', '.join(ALL_CABINS)}.")
    currency = currency.strip().upper()
    if not _CURRENCY.match(currency):
        raise ToolError(f"{currency!r} is not a 3-letter currency code.")
    if max_stops is not None and not 0 <= max_stops <= 3:
        raise ToolError("max_stops must be between 0 and 3.")
    if min_layover is not None and not 0 <= min_layover <= 24 * 60:
        raise ToolError("min_layover_minutes must be between 0 and 1440.")
    options = SearchOptions(adults=adults, children=children, cabin=cabin, currency=currency,
                            max_stops=max_stops, min_layover=min_layover, overnight=overnight)
    refusal = capabilities_of(primary_provider()).rejects(options)
    if refusal is not None:
        raise ToolError(refusal)
    return options


# ── search_split_tickets ─────────────────────────────────────────────────────

class SearchOutput(BaseModel):
    strategy: str
    currency: str
    routes_found: int
    hidden_as_risky: int
    failed_requests: int
    itineraries: list[ItineraryOut]


@mcp.tool()
async def search_split_tickets(
    destinations: list[str],
    window_start: str,
    window_end: str,
    origin: str | None = None,
    hubs: list[str] | None = None,
    trip_days: int = 0,
    adults: int = 1,
    children: int = 0,
    cabin: str = "ECONOMY",
    currency: str = "EUR",
    max_stops: int | None = None,
    min_layover_minutes: int | None = None,
    overnight: bool = False,
    hide_risky: bool = False,
    limit: int = 10,
) -> SearchOutput:
    """Search split-ticket itineraries: a discounted domestic flight to a hub plus a
    separately booked onward flight, compared with the airline's single ticket.

    COST: one call sends roughly 90-190 requests to the flight sources and can
    take up to a minute. Call it deliberately, not in a loop; use
    price_calendar to explore dates first.

    Dates are the international (onward) flight's days. trip_days=0 is
    one-way; otherwise the return is that many days later. overnight=True
    flies the domestic leg the day before (and the day after on the return).
    Prices are totals for the whole party. A ticket with price null has no
    bookable offer yet. failed_requests > 0 means the result may be
    incomplete, not that no flights exist.
    """
    if not 1 <= len(destinations) <= MAX_DESTINATIONS:
        raise ToolError(f"Give between 1 and {MAX_DESTINATIONS} destinations.")
    dests = [_code(d, "Destination") for d in destinations]
    org = _code(origin or config.ORIGIN, "Origin")
    hub_names = ({c: config.DEFAULT_HUBS.get(c, c) for c in (_code(h, "Hub") for h in hubs)}
                 if hubs else dict(config.DEFAULT_HUBS))
    start, end = _day(window_start, "window_start"), _day(window_end, "window_end")
    if end < start:
        raise ToolError("The window ends before it starts.")
    if not 0 <= trip_days <= 180:
        raise ToolError("trip_days must be between 0 (one-way) and 180.")
    if not 1 <= limit <= 30:
        raise ToolError("limit must be between 1 and 30.")
    options = _options(adults=adults, children=children, cabin=cabin, currency=currency,
                       max_stops=max_stops, min_layover=min_layover_minutes,
                       overnight=overnight)

    today = date.today().isoformat()
    window_days = [d for d in SearchWindow(start.isoformat(), end.isoformat()).dates()
                   if d >= today]
    dates = bookable_onward_dates(window_days, overnight, today)
    if not dates:
        hint = (" With a night at the hub the earliest international flight is tomorrow."
                if overnight else "")
        raise ToolError(f"No searchable dates: the window is in the past.{hint}")
    window = SearchWindow(dates[0], dates[-1])
    if window.days > config.MAX_WINDOW_DAYS:
        raise ToolError(f"The window covers {window.days} days; the most one search can "
                        f"cover is {config.MAX_WINDOW_DAYS}.")

    try:
        result = await run_search(origin=org, destinations={d: d for d in dests},
                                  hubs=hub_names, window=window, trip_days=trip_days,
                                  options=options, dates=dates)
    except ProviderError:
        raise ToolError("Your flight source can't run this search with these options.") from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    wanted = set(dates)
    itineraries = [it for it in result.itineraries if it.date in wanted]
    if hide_risky:
        shown_pairs, hidden = apply(itineraries, Filters(hide_risky=True))
        shown = [it for _, it in shown_pairs]
    else:
        shown, hidden = itineraries, 0
    shown.sort(key=lambda it: it.total)
    return SearchOutput(
        strategy=result.strategy,
        currency=options.currency,
        routes_found=len(itineraries),
        hidden_as_risky=hidden,
        failed_requests=result.parse_errors + result.fetch_errors,
        itineraries=[itinerary_out(it) for it in shown[:limit]],
    )


# ── price_calendar ───────────────────────────────────────────────────────────

class CalendarDay(BaseModel):
    date: str
    price: float
    rating: str


class CalendarOutput(BaseModel):
    currency: str
    days: list[CalendarDay]


@mcp.tool()
async def price_calendar(
    origin: str,
    dest: str,
    start: str,
    end: str,
    adults: int = 1,
    children: int = 0,
    cabin: str = "ECONOMY",
    currency: str = "EUR",
    max_stops: int | None = None,
) -> CalendarOutput:
    """Cheapest price per day for one direct route (origin -> dest), for the
    whole party. One request, so it is the cheap way to explore dates before
    search_split_tickets. These are cached cheapest-of-day figures, not
    bookable fares."""
    org, dst = _code(origin, "Origin"), _code(dest, "Destination")
    first, last = _day(start, "start"), _day(end, "end")
    if last < first:
        raise ToolError("The window ends before it starts.")
    if (last - first).days + 1 > config.MAX_WINDOW_DAYS:
        raise ToolError(f"The most one calendar can cover is {config.MAX_WINDOW_DAYS} days.")
    options = _options(adults=adults, children=children, cabin=cabin, currency=currency,
                       max_stops=max_stops)
    provider = primary_provider()
    if not isinstance(provider, SupportsCalendar):
        raise ToolError("Your flight source has no price calendar.")
    try:
        table = await provider.price_calendar(
            options.calendar_query(org, dst, first.isoformat(), last.isoformat()))
    except ProviderError as exc:
        raise ToolError(f"The price calendar failed: {exc}") from None
    return CalendarOutput(
        currency=options.currency,
        days=[CalendarDay(date=d, price=float(r.price), rating=r.rating)
              for d, r in sorted(table.items())],
    )


# ── find_airports ────────────────────────────────────────────────────────────

class AirportOut(BaseModel):
    code: str
    name: str
    city: str
    country: str


class AirportsOutput(BaseModel):
    airports: list[AirportOut]


@mcp.tool()
async def find_airports(query: str) -> AirportsOutput:
    """Turn a city or airport name into IATA codes, e.g. "Tokio" -> NRT, HND."""
    term = query.strip()
    if not 2 <= len(term) <= 60:
        raise ToolError("The query must be at least 2 characters and at most 60.")
    provider = places_provider()
    if provider is None:
        raise ToolError("No configured flight source can look up airport names; "
                        "pass IATA codes instead.")
    try:
        places = await provider.resolve_place(term)
    except ProviderError as exc:
        raise ToolError(f"The airport lookup failed: {exc}") from None
    return AirportsOutput(airports=[AirportOut(code=p.code, name=p.name, city=p.city,
                                               country=p.country) for p in places])


def main() -> None:
    """Entry point for the split-ticket-mcp command: stdio transport."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()
```

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS. If `test_bad_input_is_a_sentence_not_a_search[args1]` (`destinations: []`) is rejected by the SDK's argument validation before the tool runs, the error text differs. In that case assert on `is_error` alone for that case and record a ruling.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: MCP tools — search_split_tickets, price_calendar, find_airports"
```

---

### Task 3: stdio hygiene and README

**Files:**
- Test: `tests/test_mcp_stdio.py` (new)
- Modify: `README.md`

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_stdio.py`:

```python
"""stdout is the MCP protocol channel: nothing else may write to it."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import split_ticket_mcp.server as server

ROOT = Path(__file__).resolve().parent.parent


def test_importing_the_server_prints_nothing():
    """Review Focus #5."""
    done = subprocess.run([sys.executable, "-c", "import split_ticket_mcp.server"],
                          cwd=ROOT, capture_output=True, text=True, check=True)
    assert done.stdout == ""


def test_main_logs_to_stderr_and_runs_stdio(monkeypatch):
    ran = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: ran.append((a, k)))
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        server.main()
        streams = [getattr(h, "stream", None) for h in root.handlers]
        assert sys.stderr in streams and sys.stdout not in streams
    finally:
        root.handlers[:] = saved
    assert ran == [((), {})]


def test_the_readme_documents_the_setup():
    text = (ROOT / "README.md").read_text()
    assert 'pip install -e ".[mcp]"' in text
    assert "claude mcp add split-tickets" in text
    assert '"mcpServers"' in text
```

Run: `.venv/bin/pytest tests/test_mcp_stdio.py -q`
Expected: the README test FAILS. The first two should already PASS, because Task 2 wrote `main()` and the import path. They are guards; record that they passed on the first run.

- [ ] **Step 2: Add the README section**

In `README.md`, add a section before `## Deployment`:

````markdown
## Use it from an AI assistant (MCP)

The engine is also a local [Model Context Protocol](https://modelcontextprotocol.io)
server, so Claude Code, Claude Desktop or any MCP client can search split
tickets for you. It runs on your machine over stdio, is read-only, and needs
no bot token.

```bash
pip install -e ".[mcp]"
claude mcp add split-tickets -- /path/to/split-ticket-finder/.venv/bin/split-ticket-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "split-tickets": {
      "command": "/path/to/split-ticket-finder/.venv/bin/split-ticket-mcp",
      "cwd": "/path/to/split-ticket-finder",
      "env": { "ORIGIN": "LPA", "PROVIDERS": "kiwi,google" }
    }
  }
}
```

`cwd` lets it read your `.env`; `env` overrides individual settings. Three tools:

| Tool | What it does | Cost |
|---|---|---|
| `find_airports` | "Tokio" → NRT, HND | 1 request |
| `price_calendar` | cheapest price per day for one route | 1 request |
| `search_split_tickets` | the full search, with legs, links, connection risk and savings | ~90–190 requests |

Try: *"Find me the cheapest way from Gran Canaria to Tokyo in late October
for two adults, and avoid tight connections."*
````

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs: use the engine from an AI assistant over MCP"
```
