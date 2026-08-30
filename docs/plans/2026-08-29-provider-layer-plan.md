# Provider Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the existing Google Flights scraper and a new Kiwi.com GraphQL client behind one shared provider interface, so later work can search either source without knowing which it is talking to.

**Architecture:** A `providers/` package holds provider-agnostic types (`Offer`, `LegQuery`, `CalendarQuery`) plus three protocols. `FlightProvider` is the required one; `SupportsCalendar` and `SupportsPlaces` are optional capabilities the engine detects with `isinstance`, because Google can answer neither. Google's existing code moves in unchanged behind a thin adapter; Kiwi is new. Nothing user-visible changes in this layer — `search.py`, `scheduler.py` and the handlers keep working exactly as they do today.

**Tech Stack:** Python 3.10+, `httpx` (async), `Decimal` for money, `pytest` + `pytest-asyncio`, `ruff`. No new dependencies.

**Spec:** `docs/plans/2026-08-29-multi-provider-search-design.md`

## Global Constraints

- **Python 3.10+**, matching `requires-python = ">=3.10"`. No `match` statements, no PEP 695 generics.
- **No new runtime dependencies.** `httpx` is already a dependency; use it.
- **Every test in this plan runs offline** against `tests/fixtures/kiwi/*.json`. The only exception is Task 9, which is marked `network` and deselected by default.
- **Money is `Decimal`, never `float`.** Parse from string, round only at render time.
- **`None` means "this provider cannot tell you". It never means zero.** (spec §4.3)
- **Empty list means "no flights"; an exception means "broken".** Never collapse the two. (spec §4.5)
- **Line length 100**, ruff config already in `pyproject.toml`. Run `ruff check .` before every commit.
- **Never add `# noqa: E402` in a test file.** `pyproject.toml` already ignores E402 under `tests/*`, so the directive is itself an error (`RUF100`, and `RUF` is selected). Mid-file imports in tests need no suppression at all.
- **Use the venv:** `.venv/bin/pytest` and `.venv/bin/ruff`, already created with `pip install -e ".[dev]"`.
- Existing behaviour must not regress: `pytest` stays green at every commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `models.py` | **New.** `Route`, `fmt_dur`, `generate_dates`, `add_days` — domain types moved out of `scraper.py`, which is not where they belonged |
| `providers/__init__.py` | **New.** Public re-exports |
| `providers/base.py` | **New.** Dataclasses, protocols, error taxonomy |
| `providers/google.py` | **New** (`git mv` of `scraper.py`). Google-specific code plus `GoogleProvider` |
| `providers/kiwi.py` | **New.** `KiwiProvider` — GraphQL transport and three capabilities |
| `providers/registry.py` | **New.** Name → instance, driven by config |
| `config.py` | **Modify.** `PROVIDERS`, `PRIMARY_PROVIDER`, `KIWI_*`, `validate()` check |
| `pyproject.toml` | **Modify.** Register the `network` marker, deselect it by default |
| `.env.example` | **Modify.** Document the new settings |
| `tests/conftest.py` | **Modify.** Kiwi fixture loaders |

Fixtures are already recorded and committed in `tests/fixtures/kiwi/` (commit `91f0b57`).

### Verified API facts

These were confirmed against the live endpoint on 2026-08-29. Treat them as given; do not re-derive.

| Fact | Value |
|---|---|
| Endpoint | `https://api.skypicker.com/umbrella/v2/graphql?featureName=<op>` |
| Auth | None. `options.partner` is required and must be `skypicker` |
| Place id format | `Station:airport:LPA` — deterministic, no lookup needed |
| `Itinerary.duration`, `Segment.duration`, `Layover.duration` | **seconds** |
| `filter.stopoverTime` | **hours** — 3 → results with ≥180 min layovers. Seconds or minutes here silently return zero results |
| `filter.maxStopsCount`, `filter.excludeCarriers` | Work as named |
| `bookingUrl` | Relative (`/en/booking/?…`), needs `https://www.kiwi.com` prefix |
| Money | Strings (`"29"`, `"174.303303"`) |
| `AppError` | Arrives as **HTTP 200** with `__typename == "AppError"`. Branch on the payload, never the status code |
| Unknown airport | Returns an **empty** calendar, not an error |

---

## Task 1: Move domain types out of the scraper

`Route`, `fmt_dur`, `generate_dates` and `add_days` currently live in `scraper.py` but describe the search domain, not Google. If they stay, `providers/kiwi.py` would have to import a `Route` from the Google module. This task is a pure refactor: no behaviour changes and every existing test must still pass untouched.

**Files:**
- Create: `models.py`
- Modify: `scraper.py` (remove moved code, import back for now), `search.py:16-25`, `scheduler.py:17`, `handlers/history.py:15`
- Test: `tests/test_scraper.py` (update imports only)

**Interfaces:**
- Consumes: nothing
- Produces: `models.Route`, `models.fmt_dur(minutes: int) -> str`, `models.generate_dates(start: str, end: str, every: int) -> list[str]`, `models.add_days(date: str, days: int) -> str`

- [ ] **Step 1: Create `models.py` with the moved code**

Copy these four definitions verbatim out of `scraper.py` — `Route` (the dataclass), `fmt_dur`, `generate_dates`, `add_days` — into a new `models.py` with this header:

```python
"""Domain types shared across providers and the search engine.

These describe the search domain, not any one data source. They lived in
scraper.py until the provider layer made that placement wrong: a Kiwi client
should not have to import a Route from the Google module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
```

- [ ] **Step 2: Delete the four definitions from `scraper.py` and re-import them**

At the top of `scraper.py`, after the existing imports:

```python
from models import Route, add_days, fmt_dur, generate_dates  # noqa: F401
```

The `noqa` is deliberate and temporary — `scraper.py` no longer uses these itself, but re-exporting keeps every existing importer working until Task 3 moves the file. Task 3 deletes this line.

Then **delete `from datetime import datetime, timedelta` from `scraper.py`**. Only `generate_dates` and `add_days` used it, and both just left; leaving it is an `F401` failure. Task 3 adds it back when `_build_times` needs it. Everything else in the import block (`asyncio`, `base64`, `json`, `logging`, `random`, `re`, `dataclass`, `field`, `httpx`) is still in use by `FlightResult`, the encoder, the parser or the fetcher — leave those alone.

- [ ] **Step 3: Run the full suite to prove nothing changed**

Run: `pytest`
Expected: PASS, 71 tests. Anything else means the move dropped something.

- [ ] **Step 4: Point the real importers at `models`**

In `search.py`, `scheduler.py` and `handlers/history.py`, move `Route`, `add_days`, `fmt_dur` and `generate_dates` out of the `from scraper import (...)` list and into a new `from models import (...)` line. Leave the genuinely Google-specific imports (`build_client`, `build_url`, `search`, `FetchError`, `ParseError`, `FlightResult`) where they are.

`handlers/search_flow.py:29` imports `generate_dates` from `scraper` — change that one too.

- [ ] **Step 5: Run tests and lint**

Run: `pytest && ruff check .`
Expected: PASS, 71 tests, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add models.py scraper.py search.py scheduler.py handlers/history.py handlers/search_flow.py tests/test_scraper.py
git commit -m "refactor: move domain types out of scraper into models

Route, fmt_dur, generate_dates and add_days describe the search domain
rather than Google specifically. Leaving them in scraper.py would force
the incoming Kiwi provider to import a Route from the Google module.

Pure move, no behaviour change."
```

---

## Task 2: Provider types and protocols

**Files:**
- Create: `providers/__init__.py`, `providers/base.py`
- Test: `tests/test_providers_base.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Segment`, `Offer`, `LegQuery`, `CalendarQuery`, `RatedPrice`, `Place`, `FlightProvider`, `SupportsCalendar`, `SupportsPlaces`, `ProviderError`, `ProviderFetchError`, `ProviderParseError`

- [ ] **Step 1: Write the failing test**

Create `tests/test_providers_base.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_providers_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'providers'`

- [ ] **Step 3: Write the implementation**

Create `providers/__init__.py`:

```python
"""Flight data providers behind one shared interface."""
```

Create `providers/base.py`:

```python
"""Provider-agnostic types, capability protocols and errors.

A provider is anything that can price a leg. Sources differ in what they can
answer -- Google Flights has no price-calendar and no place search, Kiwi has
both -- so capabilities are separate protocols rather than one interface full
of supports_x() flags. The engine asks isinstance(p, SupportsCalendar) and
chooses a search strategy from the answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

# ── Errors ───────────────────────────────────────────────────────────────────


class ProviderError(RuntimeError):
    """Base class for every provider failure."""


class ProviderFetchError(ProviderError):
    """The request failed after exhausting its retry budget."""


class ProviderParseError(ProviderError):
    """A response arrived but could not be understood.

    This is the important distinction in the whole layer: a schema change, a
    consent wall or a rejected partner key must never look like "this route has
    no flights", which is an empty list. Collapsing the two makes a broken
    provider indistinguishable from an unpopular route.
    """


# ── Value objects ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Segment:
    """One flight between two airports.

    dep_local/arr_local are Optional because providers differ in what they
    report: Kiwi gives full local timestamps, Google gives bare clock times
    that have to be reconstructed against the query date.
    """

    origin: str
    dest: str
    carrier: str                        # IATA carrier code, e.g. "FR"
    carrier_name: str
    flight_no: str                      # e.g. "FR2012"
    duration: int                       # minutes
    dep_local: datetime | None = None
    arr_local: datetime | None = None


@dataclass(frozen=True)
class Offer:
    """One bookable itinerary for a single leg.

    Every Optional field means "this provider cannot tell you", never zero.
    A Google-sourced Offer has included_checked_bags is None; a formatter must
    render that as "unknown" rather than "no bag included".

    min_layover is meaningful only when stops > 0; a direct flight has no
    connection to measure.
    """

    price: Decimal
    currency: str
    airlines: list[str]
    stops: int
    duration: int                       # minutes
    segments: list[Segment]
    provider: str
    booking_url: str | None = None
    included_cabin_bags: int | None = None
    included_checked_bags: int | None = None
    checked_bag_price: Decimal | None = None
    min_layover: int | None = None      # minutes
    pnr_count: int | None = None


@dataclass(frozen=True)
class LegQuery:
    """One origin->dest search on one date."""

    origin: str                         # IATA
    dest: str                           # IATA
    date: str                           # YYYY-MM-DD
    adults: int = 1
    children: int = 0
    cabin: str = "ECONOMY"
    currency: str = "EUR"
    limit: int = 5
    max_stops: int | None = None
    min_layover: int | None = None      # minutes
    exclude_carriers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalendarQuery:
    """Cheapest price per day across a date window."""

    origin: str
    dest: str
    start: str                          # YYYY-MM-DD
    end: str                            # YYYY-MM-DD
    adults: int = 1
    children: int = 0
    cabin: str = "ECONOMY"
    currency: str = "EUR"


@dataclass(frozen=True)
class RatedPrice:
    """A calendar day's cheapest price, with the source's own cheap/expensive call."""

    price: Decimal
    rating: str                         # CHEAP | AVERAGE | EXPENSIVE | UNKNOWN


@dataclass(frozen=True)
class Place:
    """An airport resolved from free text."""

    code: str                           # IATA
    name: str
    city: str
    country: str
    place_id: str                       # provider-native id


# ── Protocols ────────────────────────────────────────────────────────────────
#
# Only the capability protocols are runtime_checkable, and they are
# methods-only on purpose: isinstance() against a Protocol carrying non-method
# members is not supported across all versions we target. FlightProvider keeps
# its `name` attribute and is used for typing only, never isinstance.


class FlightProvider(Protocol):
    """The one capability every provider must have."""

    name: str

    async def search_leg(self, query: LegQuery) -> list[Offer]:
        """Return offers for one leg, cheapest first.

        An empty list means the route genuinely has no flights. Anything
        wrong raises ProviderError.
        """
        ...

    async def aclose(self) -> None:
        """Release any held connection pool."""
        ...


@runtime_checkable
class SupportsCalendar(Protocol):
    """Can price a whole date window far more cheaply than day-by-day."""

    async def price_calendar(self, query: CalendarQuery) -> dict[str, RatedPrice]:
        """Map "YYYY-MM-DD" -> cheapest price. Missing days simply have no key."""
        ...


@runtime_checkable
class SupportsPlaces(Protocol):
    """Can turn free text into airports."""

    async def resolve_place(self, term: str, limit: int = 8) -> list[Place]:
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_providers_base.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `pytest && ruff check .`
Expected: PASS, 79 tests.

- [ ] **Step 6: Commit**

```bash
git add providers/__init__.py providers/base.py tests/test_providers_base.py
git commit -m "feat: add provider-agnostic types and capability protocols

Offer supersedes FlightResult and adds the fields Google cannot supply --
baggage, booking link, layover, PNR count -- as Optionals that mean
'unknown', never zero.

Capabilities are separate runtime-checkable protocols rather than
supports_x() flags on one interface, so the engine can detect at runtime
that Google has no calendar and fall back to grid search."
```

---

## Task 3: Move the Google scraper behind the interface

**Files:**
- Create: `providers/google.py` (via `git mv scraper.py providers/google.py`)
- Modify: `search.py`, `scheduler.py`, `handlers/history.py`, `tests/test_scraper.py`, `tests/test_search.py`, `tests/test_regressions.py` (imports)
- Test: `tests/test_google_provider.py`

**Interfaces:**
- Consumes: `providers.base.{Offer, Segment, LegQuery, FlightProvider, ProviderFetchError, ProviderParseError}`
- Produces: `providers.google.GoogleProvider` with `name = "google"`, `async search_leg(LegQuery) -> list[Offer]`, `async aclose() -> None`

- [ ] **Step 1: Move the file and rewire imports**

```bash
git mv scraper.py providers/google.py
```

In `providers/google.py`:
- Delete the temporary `from models import ...` re-export line added in Task 1.
- Add `from models import fmt_dur  # noqa: F401` **only if** something in the file still calls it; it does not, so add nothing.
- Rename `FetchError` → keep the class but make it subclass the shared one, and same for `ParseError`:

```python
from providers.base import (
    LegQuery,
    Offer,
    ProviderFetchError,
    ProviderParseError,
    Segment,
)


class ParseError(ProviderParseError):
    """Kept as a distinct name because the parser tests assert on it directly."""


class FetchError(ProviderFetchError):
    """Kept as a distinct name because the fetch tests assert on it directly."""
```

Delete the two old standalone class definitions and their docstring bodies, keeping the docstrings on the new subclasses.

Then update every importer of `scraper` to `providers.google`: `search.py`, `scheduler.py`, `handlers/history.py`, `tests/test_scraper.py`, `tests/test_search.py`, `tests/test_regressions.py`.

- [ ] **Step 2: Run the suite to prove the move is clean**

Run: `pytest`
Expected: PASS, 79 tests. No behaviour has changed yet.

- [ ] **Step 3: Write the failing adapter test**

Create `tests/test_google_provider.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_google_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'GoogleProvider'`

- [ ] **Step 5: Write the adapter**

Append to `providers/google.py`:

```python
# ============================================================
# Provider adapter
# ============================================================

def _build_times(date, raw_segments):
    """Attach dates to Google's bare [hour, minute] pairs.

    Google reports clock times with no date attached. Time only ever moves
    forward within one itinerary, so whenever a clock time is earlier than the
    one before it the itinerary has crossed midnight and the day advances.
    Returns one (departure, arrival) tuple per segment, with None where the
    payload had nothing usable.
    """
    cursor = datetime.strptime(date, "%Y-%m-%d")
    out = []
    for seg in raw_segments:
        pair = []
        for key in ("dep_time", "arr_time"):
            hm = seg.get(key)
            if not (
                isinstance(hm, list)
                and len(hm) >= 2
                and isinstance(hm[0], int)
                and isinstance(hm[1], int)
            ):
                pair.append(None)
                continue
            try:
                candidate = cursor.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            except ValueError:
                pair.append(None)
                continue
            if candidate < cursor:
                candidate += timedelta(days=1)
            cursor = candidate
            pair.append(candidate)
        out.append((pair[0], pair[1]))
    return out


def _to_offer(flight, query):
    """Map one parsed FlightResult onto the shared Offer type."""
    times = _build_times(query.date, flight.segments)
    segments = []
    for raw, (dep, arr) in zip(flight.segments, times, strict=True):
        flight_no = raw.get("flight") or ""
        carrier = flight_no[:2] if len(flight_no) >= 2 else ""
        segments.append(Segment(
            origin=raw.get("from", "?"),
            dest=raw.get("to", "?"),
            carrier=carrier,
            carrier_name=carrier,
            flight_no=flight_no,
            duration=int(raw.get("duration") or 0),
            dep_local=dep,
            arr_local=arr,
        ))

    return Offer(
        price=Decimal(str(flight.price)),
        currency=query.currency,
        airlines=list(flight.airlines),
        stops=flight.stops,
        duration=flight.duration,
        segments=segments,
        provider="google",
        # Everything below is structurally absent from Google's payload.
        # None means "unknown", and the formatter must say so.
    )


class GoogleProvider:
    """Google Flights behind the shared provider interface.

    Implements FlightProvider only: Google exposes no price calendar and no
    place search, which is exactly why those are separate protocols.
    """

    name = "google"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns_client = client is None

    async def _get_client(self):
        if self._client is None:
            self._client = build_client()
        return self._client

    async def search_leg(self, query):
        client = await self._get_client()
        html = await fetch_html(
            query.origin,
            query.dest,
            query.date,
            query.adults,
            query.currency,
            client=client,
        )
        flights = parse_flights(html)
        return [_to_offer(f, query) for f in flights[: query.limit]]

    async def aclose(self):
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
```

Add these to the file's imports — `datetime`/`timedelta` were removed in Task 1 once nothing used them, and `_build_times` needs them back:

```python
from datetime import datetime, timedelta
from decimal import Decimal

from providers.base import LegQuery, Offer, Segment
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_google_provider.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 7: Run the full suite and lint**

Run: `pytest && ruff check .`
Expected: PASS, 86 tests.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: move the Google scraper behind the provider interface

git mv scraper.py providers/google.py, with the protobuf tfs encoder and
the parser untouched, plus a GoogleProvider adapter mapping FlightResult
onto the shared Offer.

The adapter has one piece of real work: Google reports bare [hour, minute]
clock times with no date, so _build_times reconstructs timestamps against
the query date and advances a day whenever the clock goes backwards.

Fields Google structurally cannot report -- baggage, booking link, PNR
count -- stay None rather than defaulting to zero."
```

---

## Task 4: Kiwi transport and error handling

The riskiest part of the client is not parsing, it is telling failure modes apart. `AppError` arrives as HTTP 200. An unknown airport returns an empty result, not an error. This task builds only the transport and gets those distinctions right.

**Files:**
- Create: `providers/kiwi.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_kiwi_provider.py`

**Interfaces:**
- Consumes: `providers.base.{ProviderFetchError, ProviderParseError}`
- Produces: `providers.kiwi.KiwiProvider` with `name = "kiwi"`, `KiwiProvider._execute(operation: str, query: str, variables: dict, root: str) -> dict`, module constants `ENDPOINT`, `SITE_BASE`, `HEADERS`, `RETRY_STATUS`

- [ ] **Step 1: Add fixture loaders to conftest**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def kiwi_fixture():
    """Load a recorded Kiwi GraphQL response by name (no .json suffix)."""
    import json

    def _load(name: str) -> dict:
        return json.loads((FIXTURE_DIR / "kiwi" / f"{name}.json").read_text())

    return _load
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_kiwi_provider.py`:

```python
"""Tests for the Kiwi GraphQL client, against recorded responses."""
from __future__ import annotations

import httpx
import pytest

from providers.base import ProviderFetchError, ProviderParseError
from providers.kiwi import KiwiProvider


def _provider(handler) -> KiwiProvider:
    """A KiwiProvider whose HTTP calls are served by *handler*."""
    transport = httpx.MockTransport(handler)
    return KiwiProvider(client=httpx.AsyncClient(transport=transport))


def test_provider_name():
    assert KiwiProvider().name == "kiwi"


async def test_execute_returns_the_root_node_on_success(kiwi_fixture):
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    node = await _provider(handler)._execute(
        "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
    )
    assert node["__typename"] == "ItineraryPricesCalendar"
    assert len(node["calendar"]) == 30


async def test_app_error_raises_parse_error_despite_http_200(kiwi_fixture):
    """AppError arrives as HTTP 200. Branching on status code would miss it."""
    payload = kiwi_fixture("app_error")

    def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderParseError, match="Partner not valid"):
        await _provider(handler)._execute(
            "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
        )


async def test_graphql_errors_field_raises_parse_error():
    """A GraphQL validation error means the schema moved under us."""
    def handler(request):
        return httpx.Response(200, json={
            "data": None,
            "errors": [{"message": "Field amount doesn't exist on Root"}],
        })

    with pytest.raises(ProviderParseError, match="amount"):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_missing_root_field_raises_parse_error():
    def handler(request):
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(ProviderParseError):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_non_json_body_raises_parse_error():
    def handler(request):
        return httpx.Response(200, text="<html>rate limited</html>")

    with pytest.raises(ProviderParseError):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_non_retryable_status_raises_fetch_error():
    def handler(request):
        return httpx.Response(403)

    with pytest.raises(ProviderFetchError, match="403"):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_retries_then_succeeds(monkeypatch, kiwi_fixture):
    import providers.kiwi as kiwi

    async def no_sleep(_):
        return None

    monkeypatch.setattr(kiwi.asyncio, "sleep", no_sleep)
    calls = {"n": 0}
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=payload)

    node = await _provider(handler)._execute(
        "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
    )
    assert calls["n"] == 3
    assert node["__typename"] == "ItineraryPricesCalendar"


async def test_gives_up_after_the_retry_budget(monkeypatch):
    import providers.kiwi as kiwi

    async def no_sleep(_):
        return None

    monkeypatch.setattr(kiwi.asyncio, "sleep", no_sleep)

    def handler(request):
        return httpx.Response(503)

    with pytest.raises(ProviderFetchError, match="giving up"):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_operation_name_travels_as_the_feature_name_query_param(kiwi_fixture):
    seen = {}
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=payload)

    await _provider(handler)._execute(
        "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
    )
    assert "featureName=PricesCalendar" in seen["url"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_kiwi_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'providers.kiwi'`

- [ ] **Step 4: Write the transport**

Create `providers/kiwi.py`:

```python
"""Kiwi.com provider, over the GraphQL backend that serves kiwi.com itself.

The official Tequila API closed to new partners in May 2024, so this speaks to
api.skypicker.com directly. It needs no credentials but does require a valid
options.partner value, and it signals failure in a way that makes the error
handling here load-bearing:

  * An AppError arrives as HTTP 200 with __typename == "AppError". Anything
    branching on status codes alone will read a rejected partner key as
    success.
  * An unknown airport returns an *empty* result, not an error. That is a
    legitimate "no flights" and must not raise.

So: empty list means no flights, exception means broken, and never the reverse.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

from config import (
    KIWI_PARTNER,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
)
from providers.base import ProviderFetchError, ProviderParseError

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.skypicker.com/umbrella/v2/graphql"
SITE_BASE = "https://www.kiwi.com"

HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "origin": SITE_BASE,
    "referer": f"{SITE_BASE}/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Rate limiting and transient server errors are worth retrying; nothing else is.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def build_client() -> httpx.AsyncClient:
    """One client per provider instance, so requests reuse connections."""
    return httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT)


class KiwiProvider:
    """Kiwi.com behind the shared provider interface.

    Implements FlightProvider, SupportsCalendar and SupportsPlaces.
    """

    name = "kiwi"

    def __init__(self, client: httpx.AsyncClient | None = None, partner: str | None = None):
        self._client = client
        self._owns_client = client is None
        self._partner = partner or KIWI_PARTNER

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client()
        return self._client

    def _options(self, currency: str, extra: dict | None = None) -> dict:
        options = {
            "partner": self._partner,
            "currency": currency.lower(),
            "locale": "en",
        }
        if extra:
            options.update(extra)
        return options

    async def _execute(self, operation: str, query: str, variables: dict, root: str) -> dict:
        """POST one GraphQL operation and return its root node.

        Raises ProviderFetchError when the request never landed, and
        ProviderParseError when something came back that we cannot trust --
        including an AppError, which is an HTTP 200.
        """
        client = await self._get_client()
        url = f"{ENDPOINT}?featureName={operation}"
        body = {"query": query, "variables": variables}
        last_error = "unknown error"

        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                # Exponential backoff with jitter, so parallel workers that hit
                # a rate limit together do not retry in lockstep.
                delay = 2**attempt + random.uniform(0, 1)
                logger.info(
                    "Retrying %s in %.1fs (attempt %d): %s",
                    operation, delay, attempt + 1, last_error,
                )
                await asyncio.sleep(delay)

            try:
                response = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                raise ProviderFetchError(f"{operation}: HTTP {response.status_code}")

            return self._unwrap(operation, response, root)

        raise ProviderFetchError(
            f"{operation}: giving up after {MAX_RETRIES + 1} attempts ({last_error})"
        )

    @staticmethod
    def _unwrap(operation: str, response: httpx.Response, root: str) -> dict:
        """Pull the root node out of a 200 response, or explain why we cannot."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderParseError(
                f"{operation}: response body is not JSON ({exc})"
            ) from exc

        if payload.get("errors"):
            # GraphQL validation failed, which means the schema moved under us.
            messages = "; ".join(
                str(e.get("message", e)) for e in payload["errors"][:3]
            )
            raise ProviderParseError(f"{operation}: GraphQL errors: {messages}")

        node = (payload.get("data") or {}).get(root)
        if node is None:
            raise ProviderParseError(f"{operation}: response carries no {root!r} field")

        if node.get("__typename") == "AppError":
            raise ProviderParseError(
                f"{operation}: {node.get('message', 'AppError with no message')}"
            )

        return node

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
```

- [ ] **Step 5: Add the config values this imports**

In `config.py`, in the scraper settings block:

```python
# ── Kiwi provider ────────────────────────────────────────
# The GraphQL backend requires a partner identifier. It is not a secret, and
# an invalid one fails loudly with AppError("Partner not valid.") rather than
# degrading quietly, so it is safe to make configurable.
KIWI_PARTNER = os.getenv("KIWI_PARTNER", "skypicker").strip() or "skypicker"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_kiwi_provider.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 7: Run the full suite and lint**

Run: `pytest && ruff check .`
Expected: PASS, 96 tests.

- [ ] **Step 8: Commit**

```bash
git add providers/kiwi.py config.py tests/conftest.py tests/test_kiwi_provider.py
git commit -m "feat: add Kiwi GraphQL transport with explicit failure modes

The endpoint signals failure in two ways that defeat naive handling: an
AppError arrives as HTTP 200 with __typename set, and an unknown airport
returns an empty result rather than an error.

So _unwrap branches on the payload, never the status code, and raises
ProviderParseError for AppError, GraphQL errors and non-JSON bodies alike
-- while an empty-but-valid response is left alone to mean 'no flights'.

Retries reuse the same backoff-with-jitter approach as the Google client."
```

---

## Task 5: Kiwi leg search

**Files:**
- Modify: `providers/kiwi.py`
- Test: `tests/test_kiwi_provider.py`

**Interfaces:**
- Consumes: `KiwiProvider._execute`, `providers.base.{LegQuery, Offer, Segment}`
- Produces: `KiwiProvider.search_leg(LegQuery) -> list[Offer]`, helpers `_money(raw) -> Decimal`, `_minutes(seconds) -> int`, `_booking_url(raw) -> str | None`, `_local_time(raw) -> datetime | None`, `_place_id(code) -> str`, module constant `ONEWAY_QUERY`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_kiwi_provider.py`:

```python
# ── Leg search ───────────────────────────────────────────────────────────────

from datetime import datetime
from decimal import Decimal

from providers.base import LegQuery
from providers.kiwi import _booking_url, _minutes, _money, _place_id


def test_place_id_is_derived_not_looked_up():
    assert _place_id("lpa") == "Station:airport:LPA"


def test_money_parses_strings_to_decimal():
    assert _money("174.303303") == Decimal("174.303303")
    assert _money("29") == Decimal("29")


def test_money_rejects_junk_loudly():
    with pytest.raises(ProviderParseError):
        _money("not-a-price")
    with pytest.raises(ProviderParseError):
        _money(None)


def test_minutes_converts_from_seconds():
    """Kiwi reports every duration in seconds; the rest of the app uses minutes."""
    assert _minutes(10200) == 170
    assert _minutes(0) == 0


def test_booking_url_is_absolutised():
    assert _booking_url("/en/booking/?x=1") == "https://www.kiwi.com/en/booking/?x=1"
    assert _booking_url("https://www.kiwi.com/en/booking/?x=1") == (
        "https://www.kiwi.com/en/booking/?x=1"
    )
    assert _booking_url(None) is None
    assert _booking_url("") is None


async def test_search_leg_maps_a_direct_flight(kiwi_fixture):
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    offers = await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    )

    assert len(offers) == 5
    assert [o.price for o in offers] == [Decimal(x) for x in ("29", "41", "43", "44", "45")]

    first = offers[0]
    assert first.provider == "kiwi"
    assert first.currency == "EUR"
    assert first.duration == 170                     # 10200 seconds
    assert first.stops == 0
    assert first.pnr_count == 1
    assert first.airlines == ["Ryanair"]
    assert first.booking_url.startswith("https://www.kiwi.com/en/booking/")
    # A direct flight has no connection to measure.
    assert first.min_layover is None

    seg = first.segments[0]
    assert (seg.origin, seg.dest) == ("LPA", "MAD")
    assert seg.carrier == "FR"
    assert seg.carrier_name == "Ryanair"
    assert seg.flight_no == "FR2012"
    assert seg.dep_local == datetime(2026, 10, 6, 8, 30)
    assert seg.arr_local == datetime(2026, 10, 6, 12, 20)


async def test_search_leg_reports_baggage_as_known_zero_not_unknown(kiwi_fixture):
    """Kiwi CAN report baggage, so 0 included bags is a fact, not a gap."""
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    ))[0]

    assert first.included_checked_bags == 0
    assert first.included_cabin_bags == 0
    assert first.checked_bag_price == Decimal("34.99")   # cheapest tier


async def test_search_leg_maps_a_multi_segment_self_transfer(kiwi_fixture):
    """Four segments, three separate bookings, three layovers."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="MAD", dest="NRT", date="2026-10-06")
    ))[0]

    assert first.price == Decimal("552")
    assert len(first.segments) == 4
    assert first.stops == 3
    assert first.pnr_count == 3
    assert first.min_layover == 100                  # 6000 seconds, the shortest
    assert first.checked_bag_price == Decimal("174.303303")
    assert [s.origin for s in first.segments] == ["MAD", "AUH", "KUL", "KHH"]
    assert first.segments[-1].dest == "NRT"


async def test_search_leg_returns_empty_list_when_there_are_no_itineraries():
    """No flights is a normal outcome and must not raise."""
    def handler(request):
        return httpx.Response(200, json={
            "data": {"onewayItineraries": {"__typename": "Itineraries", "itineraries": []}}
        })

    offers = await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="ZZZ", date="2026-10-06")
    )
    assert offers == []


async def test_search_leg_sends_filters_with_stopover_time_in_hours(kiwi_fixture):
    """stopoverTime is in HOURS. Seconds or minutes silently return nothing."""
    seen = {}
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).search_leg(LegQuery(
        origin="LPA", dest="MAD", date="2026-10-06",
        limit=3, max_stops=1, min_layover=180, exclude_carriers=("EY", "AK"),
    ))

    flt = seen["body"]["variables"]["filter"]
    assert flt["limit"] == 3
    assert flt["maxStopsCount"] == 1
    assert flt["stopoverTime"]["start"] == 3          # 180 minutes -> 3 hours
    assert flt["excludeCarriers"] == ["EY", "AK"]
    assert flt["transportTypes"] == ["FLIGHT"]


async def test_search_leg_sends_the_date_as_a_full_day_window(kiwi_fixture):
    seen = {}
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    )

    itin = seen["body"]["variables"]["search"]["itinerary"]
    assert itin["source"]["ids"] == ["Station:airport:LPA"]
    assert itin["destination"]["ids"] == ["Station:airport:MAD"]
    assert itin["outboundDepartureDate"] == {
        "start": "2026-10-06T00:00:00", "end": "2026-10-06T23:59:59",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kiwi_provider.py -v`
Expected: FAIL — `ImportError: cannot import name '_minutes'`

- [ ] **Step 3: Write the implementation**

Add to `providers/kiwi.py` — imports first:

```python
from datetime import datetime
from decimal import Decimal, InvalidOperation

from providers.base import LegQuery, Offer, Segment
```

Then the query constant and helpers:

```python
ONEWAY_QUERY = """query OnewayItineraries($search: SearchOnewayInput, $filter: ItinerariesFilterInput, $options: ItinerariesOptionsInput) {
  onewayItineraries(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { message }
    ... on Itineraries { itineraries {
      id duration pnrCount
      price { amount }
      provider { name }
      bagsInfo { includedHandBags includedCheckedBags checkedBagTiers { tierPrice { amount } } }
      bookingOptions { edges { node { bookingUrl price { amount } } } }
      ... on ItineraryOneWay { sector { sectorSegments {
        layover { duration isStationChange isBaggageRecheck }
        segment { code duration
          carrier { code name }
          source { station { code name } localTime }
          destination { station { code name } localTime } } } } }
    } }
  }
}"""


def _place_id(code: str) -> str:
    """Kiwi place ids are deterministic, so a known IATA code needs no lookup."""
    return f"Station:airport:{code.strip().upper()}"


def _money(raw) -> Decimal:
    """Parse Kiwi's string prices. Junk raises rather than becoming zero."""
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderParseError(f"unparseable price {raw!r}") from exc


def _minutes(seconds) -> int:
    """Kiwi reports every duration in seconds; everything else here uses minutes."""
    try:
        return int(seconds) // 60
    except (TypeError, ValueError) as exc:
        raise ProviderParseError(f"unparseable duration {seconds!r}") from exc


def _booking_url(raw) -> str | None:
    """Booking links come back relative to the kiwi.com site root."""
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"{SITE_BASE}{raw}"


def _local_time(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _require(node: dict, *path: str):
    """Walk a path that the query asked for, raising if the shape changed."""
    cursor = node
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            raise ProviderParseError(f"missing expected field {'.'.join(path)!r}")
        cursor = cursor[key]
    return cursor
```

Then the mapping and the method, as members of `KiwiProvider`:

```python
    def _leg_filter(self, query: LegQuery) -> dict:
        flt: dict = {"limit": query.limit, "transportTypes": ["FLIGHT"]}
        if query.max_stops is not None:
            flt["maxStopsCount"] = query.max_stops
        if query.min_layover is not None:
            # stopoverTime is expressed in HOURS. Passing seconds or minutes
            # here does not error -- it silently matches nothing.
            flt["stopoverTime"] = {"start": max(0, query.min_layover // 60), "end": 48}
        if query.exclude_carriers:
            flt["excludeCarriers"] = list(query.exclude_carriers)
        return flt

    def _to_offer(self, raw: dict, query: LegQuery) -> Offer:
        sector_segments = _require(raw, "sector", "sectorSegments")

        segments: list[Segment] = []
        layovers: list[int] = []
        for entry in sector_segments:
            seg = _require(entry, "segment")
            carrier = _require(seg, "carrier")
            code = carrier.get("code") or ""
            segments.append(Segment(
                origin=_require(seg, "source", "station", "code"),
                dest=_require(seg, "destination", "station", "code"),
                carrier=code,
                carrier_name=carrier.get("name") or code,
                flight_no=f"{code}{seg.get('code') or ''}",
                duration=_minutes(seg.get("duration") or 0),
                dep_local=_local_time((seg.get("source") or {}).get("localTime")),
                arr_local=_local_time((seg.get("destination") or {}).get("localTime")),
            ))
            layover = entry.get("layover")
            if layover and layover.get("duration") is not None:
                layovers.append(_minutes(layover["duration"]))

        bags = raw.get("bagsInfo") or {}
        tiers = bags.get("checkedBagTiers") or []
        checked_bag_price = None
        if tiers:
            checked_bag_price = _money(_require(tiers[0], "tierPrice", "amount"))

        edges = (raw.get("bookingOptions") or {}).get("edges") or []
        booking_url = _booking_url(
            (edges[0].get("node") or {}).get("bookingUrl") if edges else None
        )

        return Offer(
            price=_money(_require(raw, "price", "amount")),
            currency=query.currency.upper(),
            airlines=list(dict.fromkeys(s.carrier_name for s in segments)),
            stops=max(0, len(segments) - 1),
            duration=_minutes(raw.get("duration") or 0),
            segments=segments,
            provider=self.name,
            booking_url=booking_url,
            included_cabin_bags=bags.get("includedHandBags"),
            included_checked_bags=bags.get("includedCheckedBags"),
            checked_bag_price=checked_bag_price,
            min_layover=min(layovers) if layovers else None,
            pnr_count=raw.get("pnrCount"),
        )

    async def search_leg(self, query: LegQuery) -> list[Offer]:
        variables = {
            "search": {
                "itinerary": {
                    "source": {"ids": [_place_id(query.origin)]},
                    "destination": {"ids": [_place_id(query.dest)]},
                    "outboundDepartureDate": {
                        "start": f"{query.date}T00:00:00",
                        "end": f"{query.date}T23:59:59",
                    },
                },
                "passengers": {"adults": query.adults, "children": query.children},
                "cabinClass": {"cabinClass": query.cabin},
            },
            "filter": self._leg_filter(query),
            "options": self._options(query.currency, {"sortBy": "PRICE"}),
        }
        node = await self._execute(
            "OnewayItineraries", ONEWAY_QUERY, variables, "onewayItineraries"
        )
        itineraries = node.get("itineraries")
        if itineraries is None:
            raise ProviderParseError("OnewayItineraries: response carries no itineraries")
        offers = [self._to_offer(raw, query) for raw in itineraries]
        offers.sort(key=lambda o: o.price)
        return offers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kiwi_provider.py -v`
Expected: PASS, 21 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `pytest && ruff check .`
Expected: PASS, 107 tests.

- [ ] **Step 6: Commit**

```bash
git add providers/kiwi.py tests/test_kiwi_provider.py
git commit -m "feat: add Kiwi leg search with unit normalisation

Maps onewayItineraries onto Offer, including the fields Google cannot
supply: baggage allowance and price, layover length, PNR count and a
booking link.

Three unit traps handled explicitly, all verified against the live API:
durations are seconds, prices are strings needing Decimal, and
filter.stopoverTime is in HOURS -- passing minutes or seconds there does
not error, it just silently matches nothing."
```

---

## Task 6: Kiwi price calendar

This is the capability the whole redesign rests on: one request prices a 91-day window.

**Files:**
- Modify: `providers/kiwi.py`
- Test: `tests/test_kiwi_provider.py`

**Interfaces:**
- Consumes: `KiwiProvider._execute`, `providers.base.{CalendarQuery, RatedPrice}`
- Produces: `KiwiProvider.price_calendar(CalendarQuery) -> dict[str, RatedPrice]`, module constant `CALENDAR_QUERY`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_kiwi_provider.py`:

```python
# ── Price calendar ───────────────────────────────────────────────────────────

from providers.base import CalendarQuery, SupportsCalendar


def test_kiwi_advertises_the_calendar_capability():
    assert isinstance(KiwiProvider(), SupportsCalendar)


async def test_price_calendar_maps_a_month(kiwi_fixture):
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    prices = await _provider(handler).price_calendar(
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
    )

    assert len(prices) == 30
    # Keys are plain dates, not the timestamps the API returns.
    assert "2026-10-01" in prices
    assert prices["2026-10-01"].price == Decimal("29")
    assert prices["2026-10-01"].rating == "AVERAGE"
    assert all(isinstance(v.price, Decimal) for v in prices.values())


async def test_price_calendar_returns_empty_for_an_unknown_airport(kiwi_fixture):
    """An unknown airport yields an empty calendar, which is data, not an error."""
    payload = kiwi_fixture("calendar_empty")

    def handler(request):
        return httpx.Response(200, json=payload)

    prices = await _provider(handler).price_calendar(
        CalendarQuery(origin="ZZZ", dest="MAD", start="2026-10-01", end="2026-10-05")
    )
    assert prices == {}


async def test_price_calendar_sends_a_datetime_window(kiwi_fixture):
    """Plain YYYY-MM-DD is rejected by the API; it wants DateTime."""
    seen = {}
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).price_calendar(
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
    )

    search = seen["body"]["variables"]["search"]
    assert search["dates"] == {
        "start": "2026-10-01T00:00:00", "end": "2026-10-31T00:00:00",
    }
    assert search["source"]["ids"] == ["Station:airport:LPA"]


async def test_price_calendar_tolerates_a_day_with_no_price(kiwi_fixture):
    """A null ratedPrice is a day with no flights, so it is simply absent."""
    payload = kiwi_fixture("calendar_lpa_mad")
    payload["data"]["itineraryPricesCalendar"]["calendar"][0]["ratedPrice"] = None

    def handler(request):
        return httpx.Response(200, json=payload)

    prices = await _provider(handler).price_calendar(
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
    )
    assert len(prices) == 29
    assert "2026-10-01" not in prices
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kiwi_provider.py -k calendar -v`
Expected: FAIL — `AttributeError: 'KiwiProvider' object has no attribute 'price_calendar'`

- [ ] **Step 3: Write the implementation**

Add `CalendarQuery, RatedPrice` to the `providers.base` import in `providers/kiwi.py`, then add the query constant:

```python
CALENDAR_QUERY = """query PricesCalendar($search: SearchPricesCalendarInput, $filter: ItinerariesFilterInput, $options: ItinerariesOptionsInput) {
  itineraryPricesCalendar(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { message }
    ... on ItineraryPricesCalendar {
      currency { code }
      calendar { date ratedPrice { price { amount } rating } }
    }
  }
}"""
```

And the method on `KiwiProvider`:

```python
    async def price_calendar(self, query: CalendarQuery) -> dict[str, RatedPrice]:
        """Price every day in a window with one request.

        This is the capability the two-stage search is built on: a 91-day
        window costs exactly the same as a one-day window. Days with no
        flights are absent from the result rather than present with a zero.
        """
        variables = {
            "search": {
                "source": {"ids": [_place_id(query.origin)]},
                "destination": {"ids": [_place_id(query.dest)]},
                # The API rejects bare YYYY-MM-DD here; it wants DateTime.
                "dates": {
                    "start": f"{query.start}T00:00:00",
                    "end": f"{query.end}T00:00:00",
                },
                "passengers": {"adults": query.adults, "children": query.children},
                "cabinClass": {"cabinClass": query.cabin},
            },
            "filter": {"transportTypes": ["FLIGHT"]},
            "options": self._options(query.currency),
        }
        node = await self._execute(
            "PricesCalendar", CALENDAR_QUERY, variables, "itineraryPricesCalendar"
        )

        calendar = node.get("calendar")
        if calendar is None:
            raise ProviderParseError("PricesCalendar: response carries no calendar")

        prices: dict[str, RatedPrice] = {}
        for item in calendar:
            rated = item.get("ratedPrice")
            if not rated:
                continue
            raw_date = item.get("date") or ""
            prices[raw_date[:10]] = RatedPrice(
                price=_money(_require(rated, "price", "amount")),
                rating=rated.get("rating") or "UNKNOWN",
            )
        return prices
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kiwi_provider.py -v`
Expected: PASS, 26 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `pytest && ruff check .`
Expected: PASS, 112 tests.

- [ ] **Step 6: Commit**

```bash
git add providers/kiwi.py tests/test_kiwi_provider.py
git commit -m "feat: add Kiwi price calendar

One request prices an entire window -- 91 days costs the same as one --
which is what the two-stage search in the design is built on.

Days with no flights are absent from the mapping rather than present as
zero, and an unknown airport yields an empty calendar rather than an
error, so callers can treat emptiness as data."
```

---

## Task 7: Kiwi place search

**Files:**
- Modify: `providers/kiwi.py`
- Test: `tests/test_kiwi_provider.py`

**Interfaces:**
- Consumes: `KiwiProvider._execute`, `providers.base.Place`
- Produces: `KiwiProvider.resolve_place(term: str, limit: int = 8) -> list[Place]`, module constant `PLACES_QUERY`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_kiwi_provider.py`:

```python
# ── Place search ─────────────────────────────────────────────────────────────

from providers.base import Place, SupportsPlaces


def test_kiwi_advertises_the_places_capability():
    assert isinstance(KiwiProvider(), SupportsPlaces)


async def test_resolve_place_returns_airports(kiwi_fixture):
    payload = kiwi_fixture("places_tokyo")

    def handler(request):
        return httpx.Response(200, json=payload)

    places = await _provider(handler).resolve_place("Tokyo")

    assert all(isinstance(p, Place) for p in places)
    assert [p.code for p in places] == ["NRT", "HND", "TJH"]
    assert places[0].name == "Narita International"
    assert places[0].city == "Tokyo"
    assert places[0].country == "Japan"
    assert places[0].place_id == "Station:airport:NRT"


async def test_resolve_place_handles_a_single_match(kiwi_fixture):
    payload = kiwi_fixture("places_gran_canaria")

    def handler(request):
        return httpx.Response(200, json=payload)

    places = await _provider(handler).resolve_place("Gran Canaria")
    assert len(places) == 1
    assert places[0].code == "LPA"


async def test_resolve_place_returns_empty_for_no_match():
    def handler(request):
        return httpx.Response(200, json={
            "data": {"places": {"__typename": "PlaceConnection", "edges": []}}
        })

    assert await _provider(handler).resolve_place("qqqqqq") == []


async def test_resolve_place_passes_term_and_limit(kiwi_fixture):
    seen = {}
    payload = kiwi_fixture("places_tokyo")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).resolve_place("Tokyo", limit=3)
    variables = seen["body"]["variables"]
    assert variables["search"]["term"] == "Tokyo"
    assert variables["first"] == 3
    assert variables["filter"]["onlyTypes"] == ["AIRPORT"]


async def test_resolve_place_skips_nodes_without_an_iata_code(kiwi_fixture):
    """Non-airport nodes have no code and are not selectable origins."""
    payload = kiwi_fixture("places_tokyo")
    payload["data"]["places"]["edges"][1]["node"]["code"] = None

    def handler(request):
        return httpx.Response(200, json=payload)

    places = await _provider(handler).resolve_place("Tokyo")
    assert [p.code for p in places] == ["NRT", "TJH"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kiwi_provider.py -k place -v`
Expected: FAIL — `AttributeError: 'KiwiProvider' object has no attribute 'resolve_place'`

- [ ] **Step 3: Write the implementation**

Add `Place` to the `providers.base` import, then:

```python
PLACES_QUERY = """query Places($search: PlacesSearchInput, $filter: PlacesFilterInput, $options: PlacesOptionsInput, $first: Int) {
  places(search: $search, filter: $filter, options: $options, first: $first) {
    __typename
    ... on AppError { message }
    ... on PlaceConnection { edges { node {
      __typename id legacyId name
      ... on Station { code city { name country { name } } } } } }
  }
}"""
```

And the method:

```python
    async def resolve_place(self, term: str, limit: int = 8) -> list[Place]:
        """Turn free text into airports, so the bot never demands an IATA code."""
        variables = {
            "search": {"term": term},
            "filter": {"onlyTypes": ["AIRPORT"]},
            "options": {"locale": "en"},
            "first": limit,
        }
        node = await self._execute("Places", PLACES_QUERY, variables, "places")

        edges = node.get("edges")
        if edges is None:
            raise ProviderParseError("Places: response carries no edges")

        places: list[Place] = []
        for edge in edges:
            item = edge.get("node") or {}
            code = item.get("code")
            if not code:
                # Not an airport station -- nothing bookable to select.
                continue
            city = item.get("city") or {}
            country = city.get("country") or {}
            places.append(Place(
                code=code,
                name=item.get("name") or code,
                city=city.get("name") or "",
                country=country.get("name") or "",
                place_id=item.get("id") or _place_id(code),
            ))
        return places
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kiwi_provider.py -v`
Expected: PASS, 32 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `pytest && ruff check .`
Expected: PASS, 118 tests.

- [ ] **Step 6: Commit**

```bash
git add providers/kiwi.py tests/test_kiwi_provider.py
git commit -m "feat: add Kiwi place search

Turns free text into airports, which is what lets the bot stop demanding
that the user knows IATA codes from memory.

Nodes without an IATA code are skipped rather than surfaced: they are not
airports and cannot be used as an origin or destination."
```

---

## Task 8: Configuration and registry

**Files:**
- Create: `providers/registry.py`
- Modify: `config.py`, `.env.example`, `bot.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `providers.google.GoogleProvider`, `providers.kiwi.KiwiProvider`, `config.{PROVIDERS, PRIMARY_PROVIDER}`
- Produces: `providers.registry.{get_provider, enabled_providers, primary_provider, close_all}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry.py`:

```python
"""Tests for provider selection and configuration validation."""
from __future__ import annotations

import pytest

import config
from providers import registry
from providers.base import SupportsCalendar
from providers.google import GoogleProvider
from providers.kiwi import KiwiProvider


@pytest.fixture(autouse=True)
def _clear_registry():
    registry._INSTANCES.clear()
    yield
    registry._INSTANCES.clear()


def test_get_provider_returns_the_right_class():
    assert isinstance(registry.get_provider("kiwi"), KiwiProvider)
    assert isinstance(registry.get_provider("google"), GoogleProvider)


def test_get_provider_is_a_singleton_per_name():
    """One instance per provider, so its connection pool is actually reused."""
    assert registry.get_provider("kiwi") is registry.get_provider("kiwi")


def test_get_provider_rejects_an_unknown_name():
    with pytest.raises(config.ConfigError, match="nope"):
        registry.get_provider("nope")


def test_enabled_providers_follows_config_order(monkeypatch):
    monkeypatch.setattr(config, "PROVIDERS", ("google", "kiwi"))
    monkeypatch.setattr(registry, "PROVIDERS", ("google", "kiwi"))
    assert list(registry.enabled_providers()) == ["google", "kiwi"]


def test_primary_provider_uses_the_configured_name(monkeypatch):
    monkeypatch.setattr(registry, "PRIMARY_PROVIDER", "google")
    assert isinstance(registry.primary_provider(), GoogleProvider)


def test_only_kiwi_advertises_calendar_support():
    """This is the check the engine uses to decide on grid-search fallback."""
    assert isinstance(registry.get_provider("kiwi"), SupportsCalendar)
    assert not isinstance(registry.get_provider("google"), SupportsCalendar)


# ── Config parsing ───────────────────────────────────────────────────────────


def test_providers_env_parses_a_comma_list(monkeypatch):
    monkeypatch.setenv("PROVIDERS", " kiwi , google ")
    assert config._providers_env("PROVIDERS", ("kiwi",)) == ("kiwi", "google")


def test_providers_env_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("PROVIDERS", raising=False)
    assert config._providers_env("PROVIDERS", ("kiwi", "google")) == ("kiwi", "google")


def test_providers_env_rejects_an_unknown_name(monkeypatch):
    monkeypatch.setenv("PROVIDERS", "kiwi,banana")
    with pytest.raises(config.ConfigError, match="banana"):
        config._providers_env("PROVIDERS", ("kiwi",))


def test_validate_rejects_a_primary_outside_the_enabled_set(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "token")
    monkeypatch.setattr(config, "OWNER_ID", 1)
    monkeypatch.setattr(config, "PROVIDERS", ("google",))
    monkeypatch.setattr(config, "PRIMARY_PROVIDER", "kiwi")
    with pytest.raises(config.ConfigError, match="PRIMARY_PROVIDER"):
        config.validate()


def test_validate_accepts_a_consistent_provider_config(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "token")
    monkeypatch.setattr(config, "OWNER_ID", 1)
    monkeypatch.setattr(config, "PROVIDERS", ("kiwi", "google"))
    monkeypatch.setattr(config, "PRIMARY_PROVIDER", "kiwi")
    config.validate()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'providers.registry'`

- [ ] **Step 3: Add the config values**

In `config.py`, add the parser next to the other `_*_env` helpers:

```python
KNOWN_PROVIDERS = ("kiwi", "google")


def _providers_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated provider list, preserving order and de-duplicating."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return tuple(default)
    names = []
    for part in raw.split(","):
        cleaned = part.strip().lower()
        if not cleaned:
            continue
        if cleaned not in KNOWN_PROVIDERS:
            raise ConfigError(
                f"{name} lists unknown provider {cleaned!r}; "
                f"valid names are {', '.join(KNOWN_PROVIDERS)}"
            )
        if cleaned not in names:
            names.append(cleaned)
    if not names:
        raise ConfigError(f"{name} is set but lists no valid providers")
    return tuple(names)
```

Then, in a new section after the scraper settings:

```python
# ── Providers ────────────────────────────────────────────
# Which sources to use, in preference order. PRIMARY drives search; the others
# are available for cross-checking. A Google-only configuration is supported
# and falls back to grid search, because Google has no price calendar.
PROVIDERS = _providers_env("PROVIDERS", ("kiwi", "google"))
PRIMARY_PROVIDER = os.getenv("PRIMARY_PROVIDER", "kiwi").strip().lower() or "kiwi"

# Kiwi tolerates far more load than scraping Google does, so it gets its own
# budget rather than sharing MAX_CONCURRENCY / DEFAULT_DELAY.
KIWI_CONCURRENCY = _int_env("KIWI_CONCURRENCY", 8, lo=1)
KIWI_DELAY = _float_env("KIWI_DELAY", 0.3, lo=0.0, hi=60.0)
```

In `validate()`, before the `if problems:` block:

```python
    if PRIMARY_PROVIDER not in PROVIDERS:
        problems.append(
            f"PRIMARY_PROVIDER is {PRIMARY_PROVIDER!r} but PROVIDERS is "
            f"{', '.join(PROVIDERS)} — the primary provider must be enabled"
        )
```

- [ ] **Step 4: Write the registry**

Create `providers/registry.py`:

```python
"""Provider selection.

Providers are singletons because each owns an httpx connection pool; building
a fresh one per search would re-do TLS for every request.
"""
from __future__ import annotations

from config import PRIMARY_PROVIDER, PROVIDERS, ConfigError
from providers.base import FlightProvider
from providers.google import GoogleProvider
from providers.kiwi import KiwiProvider

_BUILDERS = {
    "kiwi": KiwiProvider,
    "google": GoogleProvider,
}

_INSTANCES: dict[str, FlightProvider] = {}


def get_provider(name: str) -> FlightProvider:
    """Return the named provider, building it once and reusing it after."""
    key = name.strip().lower()
    if key not in _BUILDERS:
        raise ConfigError(
            f"unknown provider {name!r}; valid names are {', '.join(_BUILDERS)}"
        )
    if key not in _INSTANCES:
        _INSTANCES[key] = _BUILDERS[key]()
    return _INSTANCES[key]


def enabled_providers() -> dict[str, FlightProvider]:
    """Every configured provider, in preference order."""
    return {name: get_provider(name) for name in PROVIDERS}


def primary_provider() -> FlightProvider:
    """The provider that drives a search."""
    return get_provider(PRIMARY_PROVIDER)


async def close_all() -> None:
    """Release every held connection pool. Called on bot shutdown."""
    for provider in list(_INSTANCES.values()):
        await provider.aclose()
    _INSTANCES.clear()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_registry.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 6: Close providers on shutdown**

In `bot.py`, add after `post_init`:

```python
async def post_shutdown(application: Application) -> None:
    """Release provider connection pools on the way out."""
    from providers.registry import close_all

    await close_all()
    logger.info("Provider connections closed.")
```

and wire it in `main()`:

```python
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
```

- [ ] **Step 7: Document the new settings**

Append to `.env.example`:

```bash
# ── Providers ────────────────────────────────────────────
# Which flight sources to use, in preference order. Valid names: kiwi, google.
# A google-only setup works but falls back to the slower grid search, since
# Google exposes no price calendar.
PROVIDERS=kiwi,google
# Which one drives a search. Must appear in PROVIDERS.
PRIMARY_PROVIDER=kiwi

# Kiwi tolerates far more load than scraping Google does, so it has its own
# concurrency budget.
KIWI_CONCURRENCY=8
KIWI_DELAY=0.3
# Partner identifier required by the Kiwi GraphQL endpoint. Not a secret.
# An invalid value fails loudly rather than degrading quietly.
KIWI_PARTNER=skypicker
```

- [ ] **Step 8: Run the full suite and lint**

Run: `pytest && ruff check .`
Expected: PASS, 130 tests.

- [ ] **Step 9: Commit**

```bash
git add providers/registry.py config.py .env.example bot.py tests/test_registry.py
git commit -m "feat: add provider registry and configuration

Providers are singletons so each keeps one httpx connection pool rather
than re-doing TLS per request, and bot shutdown now closes them.

validate() rejects a PRIMARY_PROVIDER outside PROVIDERS, which would
otherwise surface as a confusing runtime lookup failure rather than a
startup error."
```

---

## Task 9: Live schema drift guard

The parser depends on undocumented response shapes. Introspection is open, so "did Kiwi change?" can be a single command instead of a debugging session. This test hits the network and is deselected by default so CI stays offline.

**Files:**
- Create: `tests/test_kiwi_schema.py`
- Modify: `pyproject.toml`, `README.md`
- Test: itself

**Interfaces:**
- Consumes: `providers.kiwi.{ENDPOINT, HEADERS}`
- Produces: nothing — this task is verification only

- [ ] **Step 1: Register the marker and deselect it by default**

In `pyproject.toml`, under `[tool.pytest.ini_options]`:

```toml
addopts = "-q --strict-markers -m 'not network'"
markers = [
    "network: hits the live Kiwi API; deselected by default, run with -m network",
]
```

`--strict-markers` is already set, so an unregistered marker would error rather than silently pass.

- [ ] **Step 2: Write the test**

Create `tests/test_kiwi_schema.py`:

```python
"""Drift guard: asserts the live Kiwi schema still has the fields we read.

The client depends on undocumented response shapes. Introspection is open, so
this turns "did Kiwi change?" into one command. It is the only test that
touches the network and is deselected by default:

    pytest -m network
"""
from __future__ import annotations

import httpx
import pytest

from providers.kiwi import ENDPOINT, HEADERS

pytestmark = pytest.mark.network

# Every type we read from, and the fields we read off it.
EXPECTED = {
    "RootQuery": {"onewayItineraries", "itineraryPricesCalendar", "places"},
    "ItineraryOneWay": {
        "id", "duration", "pnrCount", "price", "provider",
        "bagsInfo", "bookingOptions", "sector",
    },
    "ItineraryBagsInfo": {"includedHandBags", "includedCheckedBags", "checkedBagTiers"},
    "BaggageTier": {"tierPrice"},
    "SectorSegment": {"segment", "layover"},
    "Layover": {"duration", "isStationChange", "isBaggageRecheck"},
    "Segment": {"code", "duration", "carrier", "source", "destination"},
    "Stop": {"station", "localTime"},
    "Station": {"code", "name", "city"},
    "PriceCalendarItem": {"date", "ratedPrice"},
    "ItineraryPricesCalendar": {"currency", "calendar"},
    "PlaceConnection": {"edges"},
}

INTROSPECT = """query Drift($name: String!) {
  __type(name: $name) { name fields { name } }
}"""


def _fields(client: httpx.Client, type_name: str) -> set[str]:
    response = client.post(
        f"{ENDPOINT}?featureName=DriftGuard",
        json={"query": INTROSPECT, "variables": {"name": type_name}},
    )
    response.raise_for_status()
    node = (response.json().get("data") or {}).get("__type")
    assert node is not None, f"type {type_name!r} no longer exists"
    return {f["name"] for f in (node.get("fields") or [])}


@pytest.mark.parametrize(("type_name", "expected"), sorted(EXPECTED.items()))
def test_expected_fields_still_exist(type_name, expected):
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        actual = _fields(client, type_name)
    missing = expected - actual
    assert not missing, f"{type_name} lost fields the client reads: {sorted(missing)}"


def test_a_real_calendar_request_still_returns_prices():
    """End-to-end smoke test: the whole path still yields usable data."""
    from datetime import date, timedelta

    from providers.kiwi import CALENDAR_QUERY

    start = date.today() + timedelta(days=30)
    end = start + timedelta(days=10)
    variables = {
        "search": {
            "source": {"ids": ["Station:airport:LPA"]},
            "destination": {"ids": ["Station:airport:MAD"]},
            "dates": {"start": f"{start}T00:00:00", "end": f"{end}T00:00:00"},
            "passengers": {"adults": 1},
        },
        "filter": {"transportTypes": ["FLIGHT"]},
        "options": {"partner": "skypicker", "currency": "eur", "locale": "en"},
    }
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        response = client.post(
            f"{ENDPOINT}?featureName=DriftGuard",
            json={"query": CALENDAR_QUERY, "variables": variables},
        )
    node = (response.json().get("data") or {}).get("itineraryPricesCalendar")
    assert node is not None, "calendar query returned no data"
    assert node.get("__typename") == "ItineraryPricesCalendar", node
    assert node["calendar"], "calendar came back empty for a route that has flights"
```

- [ ] **Step 3: Verify it is deselected by default**

Run: `pytest`
Expected: PASS, 130 tests. The network tests are **not** among them.

- [ ] **Step 4: Run the drift guard explicitly**

Run: `pytest -m network -v`
Expected: PASS, 13 tests. If any fail, the client needs updating before anything else proceeds.

- [ ] **Step 5: Document it**

In `README.md`, under Development, replace the test lines with:

```markdown
```bash
pip install -e ".[dev]"

pytest              # offline tests only
pytest -m network   # drift guard: checks the live Kiwi schema
ruff check .        # lint
```

The parser depends on undocumented response shapes from both sources. The
Google parser is pinned against a recorded HTML capture; the Kiwi client is
pinned against recorded JSON, plus a network-marked drift guard that
introspects the live schema and fails if a field the client reads has moved.
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_kiwi_schema.py pyproject.toml README.md
git commit -m "test: add live schema drift guard for Kiwi

Introspection is open on the endpoint, so 'did Kiwi change?' can be one
command rather than a debugging session. Asserts every type and field the
client reads, plus one end-to-end calendar request.

Marked network and deselected by default, so CI stays fully offline."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §4.1 package shape | 2, 3, 4, 8 |
| §4.2 three protocols | 2 (defined), 8 (isinstance verified) |
| §4.3 `Offer`, None ≠ zero | 2, 3, 5 |
| §4.4 seconds→minutes, Decimal, relative URL | 5 |
| §4.5 error taxonomy, loud failure | 2, 4 |
| §4.6 configuration | 4 (`KIWI_PARTNER`), 8 (rest) |
| §4.7 drift guard | 9 |
| §5–§7 | Out of scope — layers 2 and 3 |

**Deviation from the spec, deliberate:** §4.3 types `Segment.dep_local` / `arr_local` as `datetime`. They are `datetime | None` here, because Google supplies bare clock times with no date and a malformed pair must degrade to "unknown" rather than to a wrong timestamp. This matches the None-means-unknown rule the spec itself sets. Patch the spec when this lands.

**Type consistency check:** `LegQuery`/`CalendarQuery` field names are identical across Tasks 2, 5 and 6. `_place_id`, `_money`, `_minutes`, `_booking_url`, `_local_time`, `_require` are defined once in Task 5 and reused in 6 and 7. `GoogleProvider.name == "google"` and `KiwiProvider.name == "kiwi"` match the `_BUILDERS` keys in Task 8 and the `KNOWN_PROVIDERS` tuple in `config.py`.

**Test count ladder:** 71 → 79 (T2) → 86 (T3) → 96 (T4) → 107 (T5) → 112 (T6) → 118 (T7) → 130 (T8), plus 13 network-only in T9. If a task's count does not land where stated, something was missed.
