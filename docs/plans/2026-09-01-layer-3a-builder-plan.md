# Layer 3a — Search Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the eleven-state linear search conversation with a hub-and-spoke draft builder that has Back and Edit everywhere, resolves place names to airports without the user knowing an IATA code, and picks dates on a month grid instead of typed `YYYY-MM-DD` strings.

**Architecture:** A new `handlers/search/` package. A frozen `SearchDraft` holds every field *and* which sub-screen is showing, so `ConversationHandler` keeps a single `BUILDING` state and Back is a re-render rather than a transition. One anchor message is edited for every screen. `draft.py` and `dates.py` import no telegram, so the logic worth testing needs no `Update`/`Context` scaffolding. Nothing in `engine/`, `scheduler.py`, `search.py` or `handlers/history.py` is touched, and no persisted shape changes.

**Tech Stack:** Python 3.10+, `python-telegram-bot[ext]>=21.0`, `aiosqlite`, `pytest` + `pytest-asyncio`, `ruff`. No new dependencies.

**Spec:** `docs/plans/2026-08-31-layer-3a-builder-design.md` (this layer) · `docs/plans/2026-08-29-multi-provider-search-design.md` §6.2, §6.3, §6.4, §7.1
**Carry-forwards — read both; they record constraints not visible in the code:** `docs/plans/2026-08-29-layer-1-carry-forward.md` · `docs/plans/2026-08-31-layer-2-carry-forward.md`

## Global Constraints

- **Python 3.10+**, matching `requires-python = ">=3.10"`. No `match` statements, no PEP 695 generics.
- **No new runtime dependencies.**
- **Every test runs offline.** Fake providers and a temp SQLite file; never call a real provider or a real Telegram API.
- **`draft.py` and `dates.py` must not import `telegram`, directly or transitively.** That is the property that keeps their tests free of `Update`/`Context` scaffolding. `handlers/utils.py` is telegram-free and may be imported; `handlers/search_flow.py` is **not** and may not.
- **Never set `cabin`, `children` or `min_layover` on anything.** Google raises a bare `ProviderError` for all three, which aborts a phase and reports a misconfigured search as "no flights" (Layer 2 carry-forward). Layer 3c adds them together with the `ProviderError` handling that makes them safe.
- **Every interpolation of provider or user text goes through `esc()`** from `handlers/utils.py`. Place names, city names, country names and the user's own search term all reach Telegram HTML. `esc()` escapes quotes, so it is safe in both text and attribute position.
- **`Place.code` is validated against `^[A-Z]{3}$` before it is stored or used.** It flows into a `LegQuery`, a booking URL and a `searches` row; a provider is not a trusted source for it.
- **A `ProviderError` from a decoration must never break a screen.** The date grid renders uncoloured; the place picker degrades to typed codes.
- **All 16 existing `tests/test_search_flow.py` tests must pass with no edit.** They patch attributes on the `handlers.search_flow` module object, which is why `run_and_report`, `_estimate_queries` and `_oversized_window_message` stay in that file for this stage.
- **`engine/`, `scheduler.py`, `search.py`, `handlers/history.py`, `handlers/favorites.py` and `models.py` are not modified.**
- Line length 100; `.venv/bin/ruff check .` must pass. **Never add `# noqa: E402` in a test file** (`tests/*` already ignores E402, so it trips `RUF100`).
- Use the repo venv: `.venv/bin/pytest`, `.venv/bin/ruff`.
- `pytest` stays green at every commit. Baseline before Task 1: **362 passing**.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `handlers/search/__init__.py` | `build_search_conversation()`, the package's only export |
| `handlers/search/draft.py` | `SearchDraft`, screen constants, `Button`, draft-panel rendering. **No telegram.** |
| `handlers/search/dates.py` | Month grid, both selection modes, presets, window ceiling. **No telegram.** |
| `handlers/search/places.py` | Provider selection, cached place resolution, IATA validation, picker rendering |
| `handlers/search/hubs.py` | Hub multi-select and presets |
| `handlers/search/builder.py` | Anchor lifecycle, the single `BUILDING` state, routing, Back |
| `tests/test_search_draft.py` | `SearchDraft` |
| `tests/test_search_dates.py` | Month grid and selection rules |
| `tests/test_search_places.py` | Resolution, cache, paste short-circuit, degradation |
| `tests/test_search_hubs.py` | Hub toggles and presets |
| `tests/test_search_builder.py` | Anchor lifecycle, the two stranding paths |

**Modified:**

| File | Change |
|---|---|
| `config.py` | `PLACE_CACHE_TTL_HOURS` |
| `db.py` | `place_cache` table in `SCHEMA`; `get_cached_places` / `put_cached_places` |
| `tests/test_db.py` | Cache round-trip, miss, expiry, term normalisation |
| `bot.py` | One import line |
| `handlers/search_flow.py` | Conversation deleted; `run_and_report` and the two pure helpers stay |
| `README.md` | The Features and repo-structure sections |

---

## Task 1: Place cache

**Files:**
- Modify: `config.py` (after the `DB_PATH` block, around line 175)
- Modify: `db.py` — `SCHEMA`, plus a new accessor section
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `config.PLACE_CACHE_TTL_HOURS: int`
  - `db.normalize_term(term: str) -> str`
  - `async db.get_cached_places(term: str) -> list[dict] | None` — `None` on miss *or* expiry
  - `async db.put_cached_places(term: str, places: list[dict]) -> None`

`place_cache` is a new **table**, so it goes in `SCHEMA` and needs no `MIGRATIONS` entry — `init_db` runs `executescript(SCHEMA)` on every start, and that tuple exists only for columns added to tables that already exist.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
# ── place_cache (spec §7.1) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_place_cache_round_trips_a_term(tmp_db):
    places = [{"code": "NRT", "name": "Narita", "city": "Tokyo",
               "country": "Japan", "place_id": "Airport:NRT"}]

    await db.put_cached_places("Tokyo", places)

    assert await db.get_cached_places("Tokyo") == places


@pytest.mark.asyncio
async def test_place_cache_misses_an_unknown_term(tmp_db):
    assert await db.get_cached_places("Atlantis") is None


@pytest.mark.asyncio
async def test_place_cache_normalizes_case_and_whitespace(tmp_db):
    """'  TOKYO  ' and 'tokyo' are one search, not three cache entries."""
    places = [{"code": "NRT", "name": "Narita", "city": "Tokyo",
               "country": "Japan", "place_id": "Airport:NRT"}]

    await db.put_cached_places("  TOKYO  ", places)

    assert await db.get_cached_places("tokyo") == places
    assert await db.get_cached_places("Tokyo") == places


@pytest.mark.asyncio
async def test_place_cache_expires_past_the_ttl(tmp_db, monkeypatch):
    """An expired row reads as a miss, not as stale data.

    The TTL is read fresh inside the accessor so this monkeypatch takes
    effect -- the same reason engine/orchestrator.py's _discount() reads
    DOMESTIC_DISCOUNT through the module attribute rather than binding it
    at import.
    """
    await db.put_cached_places("Tokyo", [{"code": "NRT", "name": "Narita",
                                          "city": "Tokyo", "country": "Japan",
                                          "place_id": "Airport:NRT"}])
    monkeypatch.setattr(db, "PLACE_CACHE_TTL_HOURS", 0)

    assert await db.get_cached_places("Tokyo") is None


@pytest.mark.asyncio
async def test_place_cache_overwrites_rather_than_duplicating(tmp_db):
    """term is the primary key; a re-resolve replaces, it does not conflict."""
    await db.put_cached_places("Tokyo", [{"code": "NRT"}])
    await db.put_cached_places("Tokyo", [{"code": "HND"}])

    assert await db.get_cached_places("Tokyo") == [{"code": "HND"}]
```

Check the fixture name at the top of `tests/test_db.py` first — if the existing fixture that points `DB_PATH` at a temp file is not called `tmp_db`, use whatever it is called in all five tests.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -k place_cache -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'put_cached_places'`

- [ ] **Step 3: Add the config knob**

In `config.py`, immediately after the `DB_PATH` line:

```python
# Autocomplete results are cached so repeated terms cost no request (spec
# §7.1). The default is 30 days: airports do not move, so this TTL exists
# for renames and re-codings, not for freshness.
PLACE_CACHE_TTL_HOURS = _int_env("PLACE_CACHE_TTL_HOURS", 720, lo=1)
```

- [ ] **Step 4: Add the table and accessors**

In `db.py`, add to the end of the `SCHEMA` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS place_cache (
    term      TEXT PRIMARY KEY,   -- casefolded, whitespace-collapsed
    places    TEXT NOT NULL,      -- JSON list of place dicts
    cached_at TEXT NOT NULL
);
```

Change the config import at the top of `db.py`:

```python
from config import DB_PATH, PLACE_CACHE_TTL_HOURS
```

Add a new section at the end of `db.py`:

```python
# ── Place cache (spec §7.1) ──────────────────────────────────────────────────


def normalize_term(term: str) -> str:
    """The cache key for a free-text place search.

    Casefolded and whitespace-collapsed so "  TOKYO  ", "Tokyo" and "tokyo"
    are one entry rather than three. str.split() with no argument collapses
    runs of any whitespace, which also handles a pasted term containing a
    tab or a newline.
    """
    return " ".join(term.split()).casefold()


async def get_cached_places(term: str) -> list[dict] | None:
    """Cached places for *term*, or None if absent or past the TTL.

    Expiry reads as a miss rather than as stale data: the caller's only
    correct response to either is to ask the provider again.

    PLACE_CACHE_TTL_HOURS is read through this module's own global rather
    than captured at import, so a test can monkeypatch it -- the same
    reason engine/orchestrator.py's _discount() re-reads DOMESTIC_DISCOUNT.
    """
    key = normalize_term(term)
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT places, cached_at FROM place_cache WHERE term = ?", (key,)
        )
        row = await cursor.fetchone()

    if row is None:
        return None

    cached_at = datetime.strptime(row[1], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    age = datetime.now(timezone.utc) - cached_at
    if age > timedelta(hours=PLACE_CACHE_TTL_HOURS):
        return None

    return json.loads(row[0])


async def put_cached_places(term: str, places: list[dict]) -> None:
    """Cache *places* under *term*, replacing any existing entry."""
    async with _connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO place_cache (term, places, cached_at) "
            "VALUES (?, ?, ?)",
            (normalize_term(term), _json(places), _now()),
        )
        await db.commit()
```

Add `timedelta` to the existing `datetime` import at the top of `db.py`:

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 6: Run the full suite and the linter**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: 367 passed (362 + 5), ruff clean.

- [ ] **Step 7: Commit**

```bash
git add config.py db.py tests/test_db.py
git commit -m "feat: cache resolved places so autocomplete costs no repeat request

Spec §7.1. A new table rather than a migration: init_db runs
executescript(SCHEMA) on every start, and MIGRATIONS exists only for
columns added to tables that already exist.

Expiry reads as a miss rather than as stale data -- the caller's only
correct response to either is to ask the provider again. The TTL is read
through the module global so a test can patch it, the same reason
_discount() re-reads DOMESTIC_DISCOUNT."
```

---

## Task 2: `SearchDraft`

**Files:**
- Create: `handlers/search/__init__.py` (empty for now — Task 7 fills it)
- Create: `handlers/search/draft.py`
- Test: `tests/test_search_draft.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Button` — `NamedTuple("Button", [("label", str), ("data", str)])`
  - `Rows = list[list[Button]]`
  - Screen constants: `SCREEN_DRAFT`, `SCREEN_DEST`, `SCREEN_HUBS`, `SCREEN_DATES`, `SCREEN_TRIP`
  - Mode constants: `MODE_WINDOW = "window"`, `MODE_DAYS = "days"`
  - `AWAIT_DEST = "dest"`, `AWAIT_HUBS = "hubs"`, `AWAIT_TRIP_DAYS = "trip_days"`
  - `MAX_DESTINATIONS = 10`, `MAX_TRIP_DAYS = 180`
  - `SearchDraft` frozen dataclass with `with_(**kw)`, `dest_codes`, `hub_codes`, `effective_dates`, `missing`, `is_ready`, `to_params()`, `render(estimate=None) -> tuple[str, Rows]`

**Why the estimate is passed in.** `_estimate_queries` lives in `handlers/search_flow.py`, which imports telegram. Computing the estimate inside `draft.py` would pull telegram into the import graph of the one file whose whole point is not needing a bot to test. `builder.py` already imports telegram, so it makes the call and hands the number over.

**Why `trip_days` is `int | None`.** One-way is `trip_days == 0`, which is also the natural "unset" default. `None` means "not chosen yet", so `missing` can tell the two apart. `to_params()` collapses `None` to `0`, matching what `run_and_report` expects.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_draft.py`:

```python
"""Tests for the search draft — the pure core of the hub-and-spoke builder.

SearchDraft holds every field *and* which sub-screen is showing, which is
what lets ConversationHandler keep a single state and makes Back a
re-render rather than a transition. This module imports no telegram, so
none of these tests need Update/Context scaffolding -- the same reason
tests/test_search_flow.py tests run_and_report and not the handlers.
"""
from __future__ import annotations

import pytest

from handlers.search.draft import (
    MODE_DAYS,
    MODE_WINDOW,
    SCREEN_DATES,
    SCREEN_DRAFT,
    SearchDraft,
)


def _draft(**kw) -> SearchDraft:
    base = dict(origin="LPA", origin_name="Gran Canaria")
    base.update(kw)
    return SearchDraft(**base)


def _ready() -> SearchDraft:
    return _draft(
        destinations=(("NRT", "Tokyo Narita"),),
        hubs=(("MAD", "Madrid"),),
        trip_days=14,
        window_start="2026-10-01",
        window_end="2026-10-10",
    )


# ── missing / is_ready ───────────────────────────────────────────────────────


def test_a_fresh_draft_is_missing_everything_but_the_origin():
    assert _draft().missing == ("destination", "trip type", "dates", "hubs")


def test_a_complete_draft_is_ready():
    assert _ready().missing == ()
    assert _ready().is_ready


def test_one_way_is_chosen_not_merely_unset():
    """trip_days=0 is one-way; None is 'not asked yet'. Collapsing them
    would let a draft reach Search having never shown the trip screen."""
    assert "trip type" in _draft(trip_days=None).missing
    assert "trip type" not in _draft(trip_days=0).missing


def test_days_mode_needs_picked_days_not_a_window():
    d = _ready().with_(date_mode=MODE_DAYS, picked_days=())
    assert "dates" in d.missing

    d = d.with_(picked_days=("2026-10-03",))
    assert "dates" not in d.missing


# ── effective_dates ──────────────────────────────────────────────────────────


def test_window_mode_expands_to_every_day_inclusive():
    d = _draft(window_start="2026-10-01", window_end="2026-10-04")
    assert d.effective_dates == ["2026-10-01", "2026-10-02",
                                 "2026-10-03", "2026-10-04"]


def test_days_mode_uses_the_picked_list_sorted():
    d = _draft(date_mode=MODE_DAYS,
               picked_days=("2026-10-09", "2026-10-01", "2026-10-05"))
    assert d.effective_dates == ["2026-10-01", "2026-10-05", "2026-10-09"]


# ── to_params ────────────────────────────────────────────────────────────────


def test_to_params_matches_what_run_and_report_takes():
    """The keys are run_and_report's contract, not this class's preference.
    Changing one here silently changes what gets searched and stored."""
    params = _ready().to_params()

    assert set(params) == {"origin", "destinations", "dates", "hubs",
                           "adults", "currency", "trip_days"}
    assert params["origin"] == "LPA"
    assert params["destinations"] == {"NRT": "Tokyo Narita"}
    assert params["hubs"] == {"MAD": "Madrid"}
    assert params["trip_days"] == 14
    assert params["dates"][0] == "2026-10-01"
    assert params["dates"][-1] == "2026-10-10"


def test_to_params_reports_an_unchosen_trip_as_one_way():
    """run_and_report has no concept of 'not chosen'; 0 is its one-way."""
    assert _ready().with_(trip_days=None).to_params()["trip_days"] == 0


def test_to_params_carries_the_picked_days_in_days_mode():
    """This is what keeps run_and_report's C1 date filter load-bearing."""
    d = _ready().with_(date_mode=MODE_DAYS,
                       picked_days=("2026-10-03", "2026-10-11"))
    assert d.to_params()["dates"] == ["2026-10-03", "2026-10-11"]


# ── with_ ────────────────────────────────────────────────────────────────────


def test_with_returns_a_copy_and_leaves_the_original_alone():
    original = _draft()
    changed = original.with_(screen=SCREEN_DATES)

    assert changed.screen == SCREEN_DATES
    assert original.screen == SCREEN_DRAFT


# ── render ───────────────────────────────────────────────────────────────────


def test_render_escapes_place_names():
    """Provider text reaches Telegram HTML. An unescaped '<' makes Telegram
    reject the whole message; the Layer 2 carry-forward flags these
    interpolations as safe only by luck."""
    d = _ready().with_(destinations=(("XXX", "A<b>&B"),))
    text, _ = d.render()

    assert "A&lt;b&gt;&amp;B" in text
    assert "<b>&B" not in text


def test_render_shows_the_estimate_when_given_one():
    text, _ = _ready().render(estimate=170)
    assert "170" in text


def test_render_omits_the_estimate_line_when_not_ready():
    """A query count for an incomplete draft would be a made-up number."""
    text, _ = _draft().render(estimate=None)
    assert "requests" not in text


def test_render_names_every_missing_field():
    text, _ = _draft().render()
    for label in ("destination", "trip type", "dates", "hubs"):
        assert label in text


def test_render_always_offers_search_even_when_incomplete():
    """The button is always present; builder.py answers a premature tap
    with an alert naming what is missing. Telegram has no disabled button,
    and hiding it leaves the user with no visible way forward."""
    _, rows = _draft().render()
    data = [b.data for row in rows for b in row]
    assert "go" in data


def test_render_offers_an_edit_for_every_field():
    _, rows = _ready().render()
    data = [b.data for row in rows for b in row]
    for target in ("edit:dest", "edit:trip", "edit:dates", "edit:hubs"):
        assert target in data


def test_render_shows_the_read_only_passenger_footer():
    """Layer 3c makes this editable. Until then it is shown without an Edit
    affordance rather than hidden: the user should know what is being
    searched, and a dead button is worse than no button."""
    text, rows = _ready().render()
    data = [b.data for row in rows for b in row]

    assert "1 adult" in text and "Economy" in text and "EUR" in text
    assert not any(d.startswith("edit:who") for d in data)


def test_render_summarizes_long_hub_lists():
    d = _ready().with_(hubs=tuple((c, c) for c in
                                  ("MAD", "BCN", "LIS", "AGP", "SVQ", "VLC")))
    text, _ = d.render()
    assert "+3" in text


@pytest.mark.parametrize("mode,expected", [
    (MODE_WINDOW, "2026-10-01 → 2026-10-10"),
    (MODE_DAYS, "2 days"),
])
def test_render_describes_each_date_mode_in_its_own_terms(mode, expected):
    d = _ready().with_(date_mode=mode,
                       picked_days=("2026-10-03", "2026-10-11"))
    text, _ = d.render()
    assert expected in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_search_draft.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.search'`

- [ ] **Step 3: Create the package and the draft**

```bash
mkdir -p handlers/search
touch handlers/search/__init__.py
```

Create `handlers/search/draft.py`:

```python
"""The search draft: every field, plus which sub-screen is showing.

Spec §6.2. The old conversation was a linear chain of eleven
ConversationHandler states with no way back, so a typo at step six meant
/cancel and start over. Here the navigation state is *data* -- a field on
this frozen dataclass -- so ConversationHandler keeps a single state and
Back is a re-render rather than a transition. That is what makes "Back and
Edit exist everywhere" true by construction rather than by eleven
carefully maintained edges.

This module must not import telegram, directly or transitively. That is
what lets its tests run with no Update/Context scaffolding, and it is why
render() returns button tuples rather than an InlineKeyboardMarkup and
takes the query estimate as an argument rather than computing it (
_estimate_queries lives in handlers/search_flow.py, which does import
telegram).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import NamedTuple

from handlers.utils import esc
from models import SearchWindow

# ── Screens ──────────────────────────────────────────────────────────────────

SCREEN_DRAFT = "draft"
SCREEN_DEST = "dest"
SCREEN_HUBS = "hubs"
SCREEN_DATES = "dates"
SCREEN_TRIP = "trip"

# ── Date modes (spec §6.4) ───────────────────────────────────────────────────

MODE_WINDOW = "window"      # tap-start / tap-end; the engine prices every day
MODE_DAYS = "days"          # multi-select; the exact days the user asked for

# ── What typed text is for, when a screen is waiting for some ────────────────

AWAIT_DEST = "dest"
AWAIT_HUBS = "hubs"
AWAIT_TRIP_DAYS = "trip_days"

MAX_DESTINATIONS = 10
MAX_TRIP_DAYS = 180

# How many hub codes to name before collapsing the rest into "+N".
_HUBS_SHOWN = 3


class Button(NamedTuple):
    """One inline keyboard button, as data rather than a telegram object."""

    label: str
    data: str


Rows = list[list[Button]]


@dataclass(frozen=True)
class SearchDraft:
    """One in-progress search. Immutable; every change returns a copy.

    ``trip_days`` is ``int | None`` because one-way is 0, which is also the
    natural "unset" default -- ``None`` means "not chosen yet" so ``missing``
    can tell them apart. ``to_params`` collapses ``None`` to 0, which is
    what ``run_and_report`` means by one-way.

    ``destinations`` and ``hubs`` are tuples of ``(code, name)`` pairs
    rather than dicts so the dataclass stays hashable and genuinely frozen;
    ``to_params`` hands ``dict(...)`` to the engine, the shape it wants.
    """

    origin: str
    origin_name: str
    destinations: tuple[tuple[str, str], ...] = ()
    hubs: tuple[tuple[str, str], ...] = ()
    trip_days: int | None = None
    date_mode: str = MODE_WINDOW
    window_start: str | None = None
    window_end: str | None = None
    picked_days: tuple[str, ...] = ()
    adults: int = 1
    currency: str = "EUR"
    screen: str = SCREEN_DRAFT
    awaiting: str | None = None

    # ── Copying ──────────────────────────────────────────────────────────

    def with_(self, **changes) -> SearchDraft:
        """A copy with *changes* applied -- the Itinerary.with_* idiom."""
        return dataclasses.replace(self, **changes)

    # ── Derived views ────────────────────────────────────────────────────

    @property
    def dest_codes(self) -> tuple[str, ...]:
        return tuple(code for code, _ in self.destinations)

    @property
    def hub_codes(self) -> tuple[str, ...]:
        return tuple(code for code, _ in self.hubs)

    @property
    def has_dates(self) -> bool:
        if self.date_mode == MODE_DAYS:
            return bool(self.picked_days)
        return bool(self.window_start and self.window_end)

    @property
    def effective_dates(self) -> list[str]:
        """The date list handed to the engine.

        In days mode this is exactly what the user picked, which is what
        keeps run_and_report's C1 filter load-bearing. In window mode it is
        the full expanded span, so min()/max() still reconstruct the window
        and history_rerun needs no change (design doc §7).
        """
        if self.date_mode == MODE_DAYS:
            return sorted(self.picked_days)
        if not (self.window_start and self.window_end):
            return []
        return SearchWindow(start=self.window_start, end=self.window_end).dates()

    @property
    def missing(self) -> tuple[str, ...]:
        """Human labels for the fields still blocking a search."""
        gaps = []
        if not self.destinations:
            gaps.append("destination")
        if self.trip_days is None:
            gaps.append("trip type")
        if not self.has_dates:
            gaps.append("dates")
        if not self.hubs:
            gaps.append("hubs")
        return tuple(gaps)

    @property
    def is_ready(self) -> bool:
        return not self.missing

    # ── Handing off to the engine ────────────────────────────────────────

    def to_params(self) -> dict:
        """The exact dict run_and_report takes.

        These keys are that function's contract, not this class's
        preference -- Layer 3a deliberately changes no persisted shape, so
        history reruns and the scheduler keep working unexamined.
        """
        return {
            "origin": self.origin,
            "destinations": dict(self.destinations),
            "dates": self.effective_dates,
            "hubs": dict(self.hubs),
            "adults": self.adults,
            "currency": self.currency,
            "trip_days": self.trip_days or 0,
        }

    # ── Rendering ────────────────────────────────────────────────────────

    def _trip_line(self) -> str:
        if self.trip_days is None:
            return "<i>not set</i>"
        if self.trip_days == 0:
            return "One-way"
        return f"Round-trip · {self.trip_days} days"

    def _dates_line(self) -> str:
        if not self.has_dates:
            return "<i>not set</i>"
        if self.date_mode == MODE_DAYS:
            n = len(self.picked_days)
            return f"{n} day{'s' if n != 1 else ''} picked"
        return f"{self.window_start} → {self.window_end}"

    def _places_line(self, pairs: tuple[tuple[str, str], ...]) -> str:
        if not pairs:
            return "<i>not set</i>"
        codes = [code for code, _ in pairs]
        if len(codes) == 1:
            return f"{esc(codes[0])} · {esc(pairs[0][1])}"
        shown = " ".join(esc(c) for c in codes[:_HUBS_SHOWN])
        extra = len(codes) - _HUBS_SHOWN
        return f"{shown} +{extra}" if extra > 0 else shown

    def render(self, estimate: int | None = None) -> tuple[str, Rows]:
        """The draft panel: (Telegram HTML, button rows).

        Returns button *tuples* rather than an InlineKeyboardMarkup so this
        module stays telegram-free; builder.py converts.
        """
        lines = [
            "🔎 <b>Search draft</b>",
            "",
            f"<b>From</b>   {esc(self.origin)} · {esc(self.origin_name)}",
            f"<b>To</b>     {self._places_line(self.destinations)}",
            f"<b>Trip</b>   {self._trip_line()}",
            f"<b>Dates</b>  {self._dates_line()}",
            f"<b>Hubs</b>   {self._places_line(self.hubs)}",
            "",
            f"<i>{self.adults} adult{'s' if self.adults != 1 else ''} "
            f"· Economy · {esc(self.currency)}</i>",
        ]

        if self.missing:
            lines += ["", f"Still needed: <b>{', '.join(self.missing)}</b>"]
        elif estimate is not None:
            lines += ["", f"Up to <b>{estimate}</b> requests"]

        rows: Rows = [
            [Button("✏️ To", "edit:dest"), Button("✏️ Trip", "edit:trip")],
            [Button("✏️ Dates", "edit:dates"), Button("✏️ Hubs", "edit:hubs")],
            [Button("🔍 Search", "go"), Button("♻️ Reset", "reset")],
            [Button("⬅️ Menu", "menu_main")],
        ]
        return "\n".join(lines), rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_search_draft.py -v`
Expected: PASS, 18 tests.

- [ ] **Step 5: Verify the no-telegram property holds**

Run:
```bash
.venv/bin/python -c "
import sys, handlers.search.draft
assert 'telegram' not in sys.modules, sorted(m for m in sys.modules if 'telegram' in m)
print('draft.py is telegram-free')"
```
Expected: `draft.py is telegram-free`. If it fails, the printed module list names what pulled telegram in — fix the import rather than deleting the check.

- [ ] **Step 6: Run the full suite and the linter**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: 385 passed, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add handlers/search/__init__.py handlers/search/draft.py tests/test_search_draft.py
git commit -m "feat: add SearchDraft, the pure core of the hub-and-spoke builder

Spec §6.2. Navigation state is a field on the draft rather than a
ConversationHandler state, so Back is a re-render and 'Back and Edit
everywhere' is true by construction rather than by eleven maintained
edges.

trip_days is int | None because one-way is 0, which is also the natural
unset default; None means 'not chosen yet' so missing can tell them apart.

render() returns button tuples and takes the query estimate as an
argument, both so this module stays telegram-free and its tests need no
Update/Context scaffolding."
```

---

## Task 3: Month grid

**Files:**
- Create: `handlers/search/dates.py`
- Test: `tests/test_search_dates.py`

**Interfaces:**
- Consumes: `Button`, `Rows`, `SearchDraft`, `MODE_WINDOW`, `MODE_DAYS` from `handlers.search.draft`.
- Produces:
  - `month_rows(year, month, *, draft, ratings=None, today) -> Rows`
  - `apply_day_tap(draft, date, *, today) -> tuple[SearchDraft, str | None]` — the copy, plus an alert string when the tap was refused
  - `apply_preset(draft, preset, *, today) -> SearchDraft` for `preset in ("30", "90", "month")`
  - `shift_month(year, month, delta) -> tuple[int, int]`
  - `caption(draft, dest_code=None) -> str`
  - `RATING_MARKS: dict[str, str]`

**The window ceiling is re-checked here** against `config.MAX_WINDOW_DAYS` rather than reusing `_oversized_window_message`. That function returns a paragraph written for a chat reply and lives in a telegram-importing module; a picker needs three words under the grid. Two renderings of one limit, one source of truth for the number.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_dates.py`:

```python
"""Tests for the month grid (spec §6.4).

The grid is a pure function, so every rule that matters -- no past days,
the MAX_WINDOW_DAYS ceiling refused at the tap rather than at Ready, and
the two selection modes -- is testable with no bot. Like draft.py this
module imports no telegram.
"""
from __future__ import annotations

from config import MAX_WINDOW_DAYS
from handlers.search.dates import (
    RATING_MARKS,
    apply_day_tap,
    apply_preset,
    caption,
    month_rows,
    shift_month,
)
from handlers.search.draft import MODE_DAYS, MODE_WINDOW, SearchDraft

TODAY = "2026-10-15"


def _draft(**kw) -> SearchDraft:
    base = dict(origin="LPA", origin_name="Gran Canaria")
    base.update(kw)
    return SearchDraft(**base)


def _labels(rows) -> list[str]:
    return [b.label for row in rows for b in row]


def _data(rows) -> list[str]:
    return [b.data for row in rows for b in row]


# ── Grid shape ───────────────────────────────────────────────────────────────


def test_grid_lays_the_month_out_seven_wide():
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    day_rows = [r for r in rows if any(b.data.startswith("d:") for b in r)]

    assert day_rows
    assert all(len(r) == 7 for r in day_rows)


def test_grid_covers_every_day_of_the_month():
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    days = [b.data for b in [b for row in rows for b in row]
            if b.data.startswith("d:")]

    assert "d:2026-10-31" in days
    assert "d:2026-11-01" not in days


def test_grid_offers_month_navigation():
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    assert "m:2026-09" in _data(rows)
    assert "m:2026-11" in _data(rows)


# ── Past days ────────────────────────────────────────────────────────────────


def test_past_days_are_not_tappable():
    """A search for yesterday is not a search. The cell stays in place so
    the month keeps its shape, but it carries no date callback."""
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    days = [b.data for b in [b for row in rows for b in row]
            if b.data.startswith("d:")]

    assert "d:2026-10-14" not in days
    assert "d:2026-10-15" in days      # today itself is fair game


# ── Window mode ──────────────────────────────────────────────────────────────


def test_first_tap_sets_the_window_start():
    d, alert = apply_day_tap(_draft(), "2026-10-20", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-10-20", None)
    assert alert is None


def test_second_tap_sets_the_window_end():
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    d, alert = apply_day_tap(d, "2026-10-25", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-10-20", "2026-10-25")
    assert alert is None


def test_tapping_before_the_start_moves_the_start_rather_than_inverting():
    """An end before its start is not a window. Treating the earlier tap as
    a new start is what the user meant, and it beats an error."""
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    d, alert = apply_day_tap(d, "2026-10-16", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-10-16", None)
    assert alert is None


def test_tapping_a_third_time_starts_a_new_window():
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    d, _ = apply_day_tap(d, "2026-10-25", today=TODAY)
    d, _ = apply_day_tap(d, "2026-11-02", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-11-02", None)


def test_an_oversized_window_is_refused_at_the_tap():
    """Review finding I6 moved this check off the Ready screen. Here it
    moves onto the tap itself: the user learns the limit while looking at
    the calendar, not after building a whole search."""
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    too_far = "2027-06-01"

    after, alert = apply_day_tap(d, too_far, today=TODAY)

    assert after.window_end is None            # unchanged
    assert after.window_start == "2026-10-20"
    assert alert is not None
    assert str(MAX_WINDOW_DAYS) in alert


def test_a_window_exactly_at_the_limit_is_accepted():
    """Off-by-one guard: MAX_WINDOW_DAYS counts both endpoints, so a window
    of exactly that many days is legal and one day more is not. Getting
    this wrong costs the user a day of the window with no explanation."""
    from models import add_days

    start = "2026-10-20"
    d, _ = apply_day_tap(_draft(), start, today=TODAY)
    at_limit = add_days(start, MAX_WINDOW_DAYS - 1)

    after, alert = apply_day_tap(d, at_limit, today=TODAY)

    assert alert is None
    assert after.window_end == at_limit


def test_a_window_one_day_over_the_limit_is_refused():
    from models import add_days

    start = "2026-10-20"
    d, _ = apply_day_tap(_draft(), start, today=TODAY)
    over_limit = add_days(start, MAX_WINDOW_DAYS)

    after, alert = apply_day_tap(d, over_limit, today=TODAY)

    assert after.window_end is None
    assert alert is not None


# ── Days mode ────────────────────────────────────────────────────────────────


def test_days_mode_toggles_a_day_on_and_off():
    d = _draft(date_mode=MODE_DAYS)

    d, _ = apply_day_tap(d, "2026-10-20", today=TODAY)
    assert d.picked_days == ("2026-10-20",)

    d, _ = apply_day_tap(d, "2026-10-20", today=TODAY)
    assert d.picked_days == ()


def test_days_mode_keeps_picks_sorted():
    d = _draft(date_mode=MODE_DAYS)
    for day in ("2026-10-25", "2026-10-20", "2026-10-22"):
        d, _ = apply_day_tap(d, day, today=TODAY)

    assert d.picked_days == ("2026-10-20", "2026-10-22", "2026-10-25")


def test_days_mode_refuses_a_span_over_the_limit():
    """The engine's window ceiling applies to the picked days' span too --
    run_and_report derives a SearchWindow from min() and max()."""
    d = _draft(date_mode=MODE_DAYS, picked_days=("2026-10-20",))

    after, alert = apply_day_tap(d, "2027-06-01", today=TODAY)

    assert after.picked_days == ("2026-10-20",)
    assert alert is not None


# ── Presets ──────────────────────────────────────────────────────────────────


def test_next_30_sets_a_window_starting_today():
    d = apply_preset(_draft(), "30", today=TODAY)
    assert d.window_start == TODAY
    assert d.window_end == "2026-11-13"     # today + 29, inclusive of both


def test_next_90_stays_within_the_engine_limit():
    d = apply_preset(_draft(), "90", today=TODAY)
    from models import SearchWindow
    assert SearchWindow(start=d.window_start, end=d.window_end).days <= MAX_WINDOW_DAYS


def test_this_month_runs_from_today_to_the_month_end():
    d = apply_preset(_draft(), "month", today=TODAY)
    assert d.window_start == TODAY
    assert d.window_end == "2026-10-31"


def test_a_preset_switches_back_to_window_mode():
    """A preset is a window by definition; leaving the draft in days mode
    would show a window the search would then ignore."""
    d = apply_preset(_draft(date_mode=MODE_DAYS, picked_days=("2026-10-20",)),
                     "30", today=TODAY)
    assert d.date_mode == MODE_WINDOW


# ── Mode switching ───────────────────────────────────────────────────────────


def test_switching_modes_clears_the_other_modes_selection():
    """A three-day pick is not a window, and guessing which was meant is
    worse than asking again."""
    d = _draft(window_start="2026-10-01", window_end="2026-10-10")
    rows = month_rows(2026, 10, draft=d, today=TODAY)

    assert "dm:days" in _data(rows)


# ── Ratings (spec §6.4) ──────────────────────────────────────────────────────


def test_rated_days_carry_their_mark():
    ratings = {"2026-10-20": "CHEAP", "2026-10-21": "EXPENSIVE"}
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY, ratings=ratings)
    labels = _labels(rows)

    assert any(lbl.startswith(RATING_MARKS["CHEAP"]) and "20" in lbl
               for lbl in labels)
    assert any(lbl.startswith(RATING_MARKS["EXPENSIVE"]) and "21" in lbl
               for lbl in labels)


def test_an_unrated_grid_renders_plain_numbers():
    """§6.4: with no destination set the grid renders uncoloured rather
    than guessing. A ProviderError takes the same path."""
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY, ratings=None)
    labels = [lbl for lbl in _labels(rows) if lbl.strip().isdigit()]

    assert "20" in [lbl.strip() for lbl in labels]


def test_unknown_rating_is_treated_as_unrated():
    """RatedPrice.rating can be UNKNOWN; that must not render as a mark
    the legend does not explain."""
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY,
                      ratings={"2026-10-20": "UNKNOWN"})
    labels = [b.label for row in rows for b in row if b.data == "d:2026-10-20"]

    assert labels == ["20"]


# ── Caption ──────────────────────────────────────────────────────────────────


def test_caption_names_the_destination_and_calls_it_a_direct_fare_signal():
    """§6.4: the picker's colours are the through-fare shape, not the
    split. Saying so is the whole point of the label."""
    text = caption(_draft(), dest_code="NRT")

    assert "NRT" in text
    assert "direct-fare signal" in text
    assert "saving" not in text.lower()


def test_caption_without_a_destination_does_not_claim_a_signal():
    text = caption(_draft(), dest_code=None)
    assert "direct-fare signal" not in text


# ── Month arithmetic ─────────────────────────────────────────────────────────


def test_shift_month_wraps_the_year():
    assert shift_month(2026, 12, 1) == (2027, 1)
    assert shift_month(2026, 1, -1) == (2025, 12)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_search_dates.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.search.dates'`

- [ ] **Step 3: Write the month grid**

Create `handlers/search/dates.py`:

```python
"""The month grid (spec §6.4).

In the two-stage model the user chooses a *window*, not individual dates,
so window mode is the default: tap a start, tap an end. But "I can only fly
the 3rd, the 10th or the 17th" is a real search the bot handled before this
rewrite, and a window-only picker would drop it *and* turn
run_and_report's C1 date filter into dead code that a later reader deletes
without knowing what it guarded. A Pick-days toggle switches the same grid
to multi-select.

Mode lives in the draft, so toggling is a re-render. Switching modes clears
the other mode's selection rather than translating it -- a three-day pick
is not a window, and guessing which was meant is worse than asking again.

Like draft.py, this module imports no telegram.
"""
from __future__ import annotations

import calendar
from datetime import datetime

from config import MAX_WINDOW_DAYS
from handlers.search.draft import MODE_DAYS, MODE_WINDOW, Button, Rows, SearchDraft
from models import SearchWindow, add_days

_ISO = "%Y-%m-%d"

# §6.4's price ratings. A provider's UNKNOWN deliberately has no mark: a
# symbol the legend does not explain is worse than a plain number.
RATING_MARKS = {
    "CHEAP": "🟢",
    "AVERAGE": "🟡",
    "EXPENSIVE": "🔴",
}

_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")

_WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

# A Telegram inline keyboard needs a callback for every button. Cells that
# are not tappable get this, and the router answers it silently.
NOOP = "noop"


# ── Month arithmetic ─────────────────────────────────────────────────────────

def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """The month *delta* months from (year, month), wrapping the year."""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _span_days(a: str, b: str) -> int:
    """Inclusive day count between two ISO dates, in either order."""
    lo, hi = sorted((a, b))
    return SearchWindow(start=lo, end=hi).days


def _too_long(a: str, b: str) -> str | None:
    """An alert string if a..b exceeds the engine's ceiling, else None."""
    span = _span_days(a, b)
    if span > MAX_WINDOW_DAYS:
        return (f"That would be {span} days. The engine covers at most "
                f"{MAX_WINDOW_DAYS} in one search — pick a closer date.")
    return None


# ── Selection ────────────────────────────────────────────────────────────────

def apply_day_tap(
    draft: SearchDraft, date: str, *, today: str
) -> tuple[SearchDraft, str | None]:
    """Apply a tap on *date*. Returns the new draft and an alert, if refused.

    An alert means nothing changed: the caller shows it and re-renders the
    unchanged grid. Refusing at the tap rather than at Ready is the point --
    review finding I6 moved this check off the summary screen, and here it
    moves onto the calendar the user is already looking at.
    """
    if date < today:
        return draft, None

    if draft.date_mode == MODE_DAYS:
        picked = set(draft.picked_days)
        if date in picked:
            picked.discard(date)
            return draft.with_(picked_days=tuple(sorted(picked))), None
        if picked:
            alert = _too_long(min(min(picked), date), max(max(picked), date))
            if alert:
                return draft, alert
        picked.add(date)
        return draft.with_(picked_days=tuple(sorted(picked))), None

    start, end = draft.window_start, draft.window_end

    if start is None or end is not None:
        # No window yet, or a complete one: this tap starts a new window.
        return draft.with_(window_start=date, window_end=None), None

    if date < start:
        # An end before its start is not a window; the earlier tap is a new
        # start, which is what the user meant and beats an error.
        return draft.with_(window_start=date, window_end=None), None

    alert = _too_long(start, date)
    if alert:
        return draft, alert
    return draft.with_(window_end=date), None


def apply_preset(draft: SearchDraft, preset: str, *, today: str) -> SearchDraft:
    """Apply "30", "90" or "month". Always leaves the draft in window mode."""
    if preset == "month":
        first = datetime.strptime(today, _ISO)
        last_day = calendar.monthrange(first.year, first.month)[1]
        end = first.replace(day=last_day).strftime(_ISO)
    else:
        # Inclusive of both endpoints, so "next 30" is today plus 29.
        end = add_days(today, min(int(preset), MAX_WINDOW_DAYS) - 1)

    return draft.with_(
        date_mode=MODE_WINDOW,
        window_start=today,
        window_end=end,
        picked_days=(),
    )


def switch_mode(draft: SearchDraft, mode: str) -> SearchDraft:
    """Switch selection mode, clearing the other mode's selection."""
    if mode == MODE_DAYS:
        return draft.with_(date_mode=MODE_DAYS, window_start=None,
                           window_end=None)
    return draft.with_(date_mode=MODE_WINDOW, picked_days=())


# ── Rendering ────────────────────────────────────────────────────────────────

def _cell_label(
    date: str, day: int, draft: SearchDraft, ratings: dict[str, str] | None
) -> str:
    """The label for one tappable day."""
    if draft.date_mode == MODE_DAYS:
        if date in draft.picked_days:
            return f"✓{day}"
    else:
        start, end = draft.window_start, draft.window_end
        if date in (start, end):
            return f"[{day}]"
        if start and end and start < date < end:
            return f"·{day}"

    mark = (ratings or {}).get(date)
    if mark in RATING_MARKS:
        return f"{RATING_MARKS[mark]}{day}"
    return str(day)


def month_rows(
    year: int,
    month: int,
    *,
    draft: SearchDraft,
    today: str,
    ratings: dict[str, str] | None = None,
) -> Rows:
    """The full picker keyboard for one month.

    *ratings* maps an ISO date to a RatedPrice.rating string. None renders
    an uncoloured grid -- §6.4's behaviour when no destination is set, and
    also what a ProviderError falls back to. A decoration must not break
    the picker.
    """
    rows: Rows = [
        [Button(f"◀", f"m:{'%04d-%02d' % shift_month(year, month, -1)}"),
         Button(f"{_MONTH_NAMES[month - 1]} {year}", NOOP),
         Button("▶", f"m:{'%04d-%02d' % shift_month(year, month, 1)}")],
        [Button(d, NOOP) for d in _WEEKDAYS],
    ]

    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row: list[Button] = []
        for day in week:
            if day == 0:
                row.append(Button(" ", NOOP))
                continue
            date = f"{year:04d}-{month:02d}-{day:02d}"
            if date < today:
                row.append(Button("·", NOOP))
                continue
            row.append(Button(_cell_label(date, day, draft, ratings), f"d:{date}"))
        rows.append(row)

    rows.append([
        Button("Next 30", "dp:30"),
        Button("Next 90", "dp:90"),
        Button("This month", "dp:month"),
    ])

    toggle = (Button("Pick a range", f"dm:{MODE_WINDOW}")
              if draft.date_mode == MODE_DAYS
              else Button("Pick days", f"dm:{MODE_DAYS}"))
    rows.append([toggle, Button("Clear", "dclear"), Button("Done", "back")])

    return rows


def caption(draft: SearchDraft, dest_code: str | None = None) -> str:
    """The text above the grid.

    §6.4 requires the colours be labelled a *direct-fare signal* and the
    destination they came from be named: they are the through-fare shape
    for one destination, not the split, and not a saving. The results
    screen's strip is the version worth acting on.
    """
    if draft.date_mode == MODE_DAYS:
        n = len(draft.picked_days)
        state = f"{n} day{'s' if n != 1 else ''} selected" if n else "Tap the days you can fly."
    elif draft.window_start and draft.window_end:
        span = _span_days(draft.window_start, draft.window_end)
        state = f"{draft.window_start} → {draft.window_end} · {span} days"
    elif draft.window_start:
        state = f"{draft.window_start} → … tap the end date"
    else:
        state = "Tap a start date."

    lines = ["<b>When?</b>", state]
    if dest_code:
        lines.append(
            f"<i>Colours: direct-fare signal · {draft.origin}→{dest_code}. "
            "Not the split price.</i>"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_search_dates.py -v`
Expected: PASS, 25 tests.

- [ ] **Step 5: Verify the no-telegram property still holds**

Run:
```bash
.venv/bin/python -c "
import sys, handlers.search.dates
assert 'telegram' not in sys.modules, sorted(m for m in sys.modules if 'telegram' in m)
print('dates.py is telegram-free')"
```

- [ ] **Step 6: Run the full suite and the linter**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: 410 passed, ruff clean. Fix any `f`-string-without-placeholder warnings ruff raises on the `◀` button.

- [ ] **Step 7: Commit**

```bash
git add handlers/search/dates.py tests/test_search_dates.py
git commit -m "feat: add the month grid with window and multi-day selection

Spec §6.4. Window mode is the default the two-stage engine wants, but a
Pick-days toggle keeps the discrete-date search the bot handled before
this rewrite -- dropping it would also turn run_and_report's C1 date
filter into dead code a later reader deletes without knowing what it
guarded.

The MAX_WINDOW_DAYS ceiling is refused at the tap. Review finding I6 moved
that check off the Ready screen; this moves it onto the calendar the user
is already looking at. The limit is re-checked here rather than reusing
_oversized_window_message, which returns a chat paragraph and lives in a
telegram-importing module.

A provider's UNKNOWN rating deliberately gets no mark: a symbol the legend
does not explain is worse than a plain number."
```

---

## Task 4: Place picker

**Files:**
- Create: `handlers/search/places.py`
- Test: `tests/test_search_places.py`

**Interfaces:**
- Consumes: `Button`, `Rows`, `SearchDraft` from `handlers.search.draft`; `db.get_cached_places` / `db.put_cached_places` from Task 1.
- Produces:
  - `places_provider() -> FlightProvider | None`
  - `IATA_RE`
  - `try_parse_codes(text: str) -> list[str] | None`
  - `async resolve(term: str, limit: int = 8) -> list[Place]`
  - `render_picker(draft, *, field, results, term, error=None) -> tuple[str, Rows]` for `field in ("dest", "hubs")`
  - `PROMPT_WITH_SEARCH`, `PROMPT_CODES_ONLY`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_places.py`:

```python
"""Tests for the place picker (spec §6.3).

Free text in, up to eight toggle buttons out. The three things that decide
correctness here are which provider answers, whether provider text is
escaped before it reaches Telegram HTML, and whether a Kiwi-less
deployment still has a way to search at all.
"""
from __future__ import annotations

import pytest

import db as db_module
import handlers.search.places as places
from providers.base import Place, ProviderFetchError

NRT = Place(code="NRT", name="Narita", city="Tokyo", country="Japan",
            place_id="Airport:NRT")
HND = Place(code="HND", name="Haneda", city="Tokyo", country="Japan",
            place_id="Airport:HND")


class FakePlacesProvider:
    """A provider that can resolve names. Records every call."""

    name = "fake"

    def __init__(self, results=None, error=None):
        self._results = results if results is not None else [NRT, HND]
        self._error = error
        self.calls: list[str] = []

    async def resolve_place(self, term, limit=8):
        self.calls.append(term)
        if self._error:
            raise self._error
        return self._results[:limit]

    async def search_leg(self, query):
        return []

    async def aclose(self):
        return None


class FakePlainProvider:
    """A provider with no place search — Google's shape."""

    name = "plain"

    async def search_leg(self, query):
        return []

    async def aclose(self):
        return None


# ── Provider selection ───────────────────────────────────────────────────────


def test_places_provider_picks_a_capable_provider(monkeypatch):
    monkeypatch.setattr(places, "enabled_providers",
                        lambda: {"google": FakePlainProvider(),
                                 "kiwi": FakePlacesProvider()})

    assert isinstance(places.places_provider(), FakePlacesProvider)


def test_places_provider_does_not_require_the_primary_one(monkeypatch):
    """With PRIMARY_PROVIDER=google, Kiwi still resolves names even though
    Google drives the search. Otherwise the spec's 'no step requires an
    IATA code' and 'disabling Kiwi leaves a working bot' contradict."""
    monkeypatch.setattr(places, "enabled_providers",
                        lambda: {"google": FakePlainProvider(),
                                 "kiwi": FakePlacesProvider()})
    monkeypatch.setattr(places, "PRIMARY_PROVIDER", "google")

    assert places.places_provider() is not None


def test_places_provider_is_none_without_a_capable_provider(monkeypatch):
    monkeypatch.setattr(places, "enabled_providers",
                        lambda: {"google": FakePlainProvider()})

    assert places.places_provider() is None


# ── The paste short-circuit (§6.3) ───────────────────────────────────────────


def test_typed_codes_parse_without_any_lookup():
    assert places.try_parse_codes("JFK,LAX") == ["JFK", "LAX"]


def test_a_place_name_is_not_mistaken_for_codes():
    assert places.try_parse_codes("Tokyo") is None
    assert places.try_parse_codes("New York") is None


# ── Resolution and cache ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_returns_places_and_caches_them(tmp_db, monkeypatch):
    provider = FakePlacesProvider()
    monkeypatch.setattr(places, "places_provider", lambda: provider)

    first = await places.resolve("Tokyo")
    second = await places.resolve("Tokyo")

    assert [p.code for p in first] == ["NRT", "HND"]
    assert first == second
    assert provider.calls == ["Tokyo"]      # the second call hit the cache


@pytest.mark.asyncio
async def test_resolve_normalizes_the_cache_key(tmp_db, monkeypatch):
    provider = FakePlacesProvider()
    monkeypatch.setattr(places, "places_provider", lambda: provider)

    await places.resolve("Tokyo")
    await places.resolve("  TOKYO  ")

    assert provider.calls == ["Tokyo"]


@pytest.mark.asyncio
async def test_resolve_drops_a_place_whose_code_is_not_iata(tmp_db, monkeypatch):
    """Place.code flows into a LegQuery, a booking URL and a searches row.
    A provider is not a trusted source for it."""
    bad = Place(code="TOKYO-ALL", name="All airports", city="Tokyo",
                country="Japan", place_id="City:tokyo")
    monkeypatch.setattr(places, "places_provider",
                        lambda: FakePlacesProvider(results=[bad, NRT]))

    result = await places.resolve("Tokyo")

    assert [p.code for p in result] == ["NRT"]


@pytest.mark.asyncio
async def test_resolve_without_a_capable_provider_returns_empty(tmp_db, monkeypatch):
    monkeypatch.setattr(places, "places_provider", lambda: None)

    assert await places.resolve("Tokyo") == []


@pytest.mark.asyncio
async def test_resolve_does_not_cache_a_provider_failure(tmp_db, monkeypatch):
    """Caching an error would make one bad minute look like a dead airport
    for the next thirty days."""
    provider = FakePlacesProvider(error=ProviderFetchError("down"))
    monkeypatch.setattr(places, "places_provider", lambda: provider)

    with pytest.raises(ProviderFetchError):
        await places.resolve("Tokyo")

    assert await db_module.get_cached_places("Tokyo") is None


# ── Rendering ────────────────────────────────────────────────────────────────


def _draft():
    from handlers.search.draft import SearchDraft
    return SearchDraft(origin="LPA", origin_name="Gran Canaria")


def test_picker_escapes_provider_text():
    """The Layer 2 carry-forward flags these interpolations as safe only by
    luck. An unescaped '<' makes Telegram reject the whole message."""
    hostile = Place(code="XXX", name="A<b>", city="C&D", country='E"F',
                    place_id="x")
    text, rows = places.render_picker(_draft(), field="dest",
                                      results=[hostile], term="x")
    blob = text + "".join(b.label for row in rows for b in row)

    assert "<b>" not in blob
    assert "&lt;b&gt;" in blob


def test_picker_escapes_the_users_own_search_term():
    text, _ = places.render_picker(_draft(), field="dest", results=[],
                                   term="<script>")
    assert "<script>" not in text


def test_picker_marks_already_selected_places():
    draft = _draft().with_(destinations=(("NRT", "Narita"),))
    _, rows = places.render_picker(draft, field="dest",
                                   results=[NRT, HND], term="Tokyo")
    labels = {b.data: b.label for row in rows for b in row}

    assert labels["p:dest:NRT"].startswith("✓")
    assert not labels["p:dest:HND"].startswith("✓")


def test_picker_offers_done_and_back():
    _, rows = places.render_picker(_draft(), field="dest", results=[NRT],
                                   term="Tokyo")
    data = [b.data for row in rows for b in row]

    assert "back" in data


def test_picker_without_a_capable_provider_asks_for_codes(monkeypatch):
    """A Kiwi-less deployment keeps a working search — the second half of
    the two success criteria that would otherwise contradict."""
    monkeypatch.setattr(places, "places_provider", lambda: None)

    text, _ = places.render_picker(_draft(), field="dest", results=[], term="")

    assert "IATA" in text or "code" in text.lower()
    assert "MAD" in text          # a worked example, not a bare instruction


def test_picker_reports_an_error_without_losing_the_screen():
    text, rows = places.render_picker(_draft(), field="dest", results=[],
                                      term="Tokyo", error="Search unavailable")

    assert "Search unavailable" in text
    assert any(b.data == "back" for row in rows for b in row)
```

Add a `tmp_db` fixture to this file if `tests/conftest.py` does not already provide one — check `tests/conftest.py` first and reuse the existing name if it differs.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_search_places.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.search.places'`

- [ ] **Step 3: Write the picker**

Create `handlers/search/places.py`:

```python
"""Free text to airports (spec §6.3).

Which provider answers is the load-bearing decision. It is *any* enabled
provider implementing SupportsPlaces, primary first -- not
primary_provider(). With PROVIDERS=("kiwi","google") and
PRIMARY_PROVIDER=google, Kiwi still resolves names even though Google
drives the search. Only a genuinely Kiwi-less deployment loses
autocomplete, and there this screen degrades to typed codes.

That is the honest reading of two success criteria that otherwise
contradict: "no step of the conversation requires knowing an IATA code"
and "disabling Kiwi leaves a working Google-only bot". The first holds
wherever a places-capable provider is configured; the degradation is what
the second is for.
"""
from __future__ import annotations

import dataclasses
import re

from config import PRIMARY_PROVIDER
from db import get_cached_places, put_cached_places
from handlers.search.draft import Button, Rows, SearchDraft
from handlers.utils import ValidationError, esc, parse_iata_codes
from providers.base import FlightProvider, Place, SupportsPlaces
from providers.registry import enabled_providers

# Place.code reaches a LegQuery, a booking URL and a searches row. A
# provider is not a trusted source for it, so it is validated here.
IATA_RE = re.compile(r"^[A-Z]{3}$")

MAX_RESULTS = 8

PROMPT_WITH_SEARCH = (
    "Type a city or airport — <code>Tokyo</code>, <code>Narita</code>.\n"
    "Or paste codes directly: <code>NRT, HND</code>."
)

PROMPT_CODES_ONLY = (
    "Send airport codes separated by commas.\n"
    "IATA codes are exactly three letters, e.g. <code>MAD</code>, "
    "<code>JFK</code>.\n"
    "<i>Name search needs a provider that supports it; none is configured.</i>"
)

_FIELD_TITLES = {"dest": "Where to?", "hubs": "Which hubs?"}


def places_provider() -> FlightProvider | None:
    """The first enabled provider that can resolve names, primary first."""
    providers = enabled_providers()
    primary = providers.get(PRIMARY_PROVIDER)
    if isinstance(primary, SupportsPlaces):
        return primary
    for provider in providers.values():
        if isinstance(provider, SupportsPlaces):
            return provider
    return None


def try_parse_codes(text: str) -> list[str] | None:
    """The typed codes in *text*, or None if it is not a code list.

    §6.3's power-user path, and the reason a dead places endpoint never
    blocks someone who knows the code. A three-letter term is read as a
    code rather than a name; that is ambiguous in principle (RIO is both)
    and overwhelmingly a code in practice.
    """
    try:
        return parse_iata_codes(text)
    except ValidationError:
        return None


async def resolve(term: str, limit: int = MAX_RESULTS) -> list[Place]:
    """Places matching *term*, cached. Empty list if nothing can resolve.

    A ProviderError propagates rather than being swallowed: this project's
    central rule is that empty means "no results" and an exception means
    "broken", and the caller renders the two differently. A failure is
    never cached -- one bad minute must not look like a dead airport for
    the next thirty days.
    """
    cached = await get_cached_places(term)
    if cached is not None:
        return [Place(**row) for row in cached]

    provider = places_provider()
    if provider is None:
        return []

    found = await provider.resolve_place(term, limit=limit)
    valid = [p for p in found if IATA_RE.match(p.code)]
    await put_cached_places(term, [dataclasses.asdict(p) for p in valid])
    return valid


def _label(place: Place, selected: bool) -> str:
    mark = "✓" if selected else ""
    return f"{mark}{esc(place.code)} · {esc(place.city)} ({esc(place.country)})"


def render_picker(
    draft: SearchDraft,
    *,
    field: str,
    results: list[Place],
    term: str,
    error: str | None = None,
) -> tuple[str, Rows]:
    """The picker screen for *field* ("dest" or "hubs")."""
    chosen = draft.dest_codes if field == "dest" else draft.hub_codes

    lines = [f"<b>{_FIELD_TITLES[field]}</b>"]

    if error:
        lines.append(f"\n⚠️ {esc(error)}")
        lines.append(PROMPT_WITH_SEARCH)
    elif places_provider() is None:
        lines.append("\n" + PROMPT_CODES_ONLY)
    else:
        lines.append("\n" + PROMPT_WITH_SEARCH)

    if term and not results and not error:
        lines.append(f"\nNothing matched <b>{esc(term)}</b>.")

    if chosen:
        lines.append("\nSelected: <b>" + " ".join(esc(c) for c in chosen) + "</b>")

    rows: Rows = [
        [Button(_label(p, p.code in chosen), f"p:{field}:{esc(p.code)}")]
        for p in results[:MAX_RESULTS]
    ]
    rows.append([Button("⬅️ Done", "back")])
    return "\n".join(lines), rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_search_places.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Run the full suite and the linter**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: 427 passed, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add handlers/search/places.py tests/test_search_places.py
git commit -m "feat: resolve place names to airports, with a typed-code fallback

Spec §6.3. Autocomplete keys off any enabled SupportsPlaces provider
rather than the primary one, so a google-primary deployment still resolves
names through Kiwi. Only a genuinely Kiwi-less deployment degrades to
typed codes -- which is what reconciles 'no step requires an IATA code'
with 'disabling Kiwi leaves a working bot'.

Place.code is validated against the IATA shape before it is stored: it
reaches a LegQuery, a booking URL and a searches row, and a provider is
not a trusted source for it. Provider text is escaped; the Layer 2
carry-forward flags these interpolations as safe only by luck.

A ProviderError propagates rather than being swallowed, and is never
cached: empty means no results, an exception means broken, and one bad
minute must not look like a dead airport for thirty days."
```

---

## Task 5: Hub multi-select

**Files:**
- Create: `handlers/search/hubs.py`
- Test: `tests/test_search_hubs.py`

**Interfaces:**
- Consumes: `Button`, `Rows`, `SearchDraft` from `handlers.search.draft`.
- Produces:
  - `render_hubs(draft) -> tuple[str, Rows]`
  - `toggle_hub(draft, code) -> SearchDraft`
  - `apply_hub_preset(draft, preset) -> SearchDraft` for `preset in ("all", "top2", "top3")`
  - `add_typed_hubs(draft, codes) -> SearchDraft`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_hubs.py`:

```python
"""Tests for hub multi-select (spec §6.3).

Hubs are the search's cost driver -- phase 0 issues one calendar request
per hub per destination -- so the screen shows every known hub as a toggle
rather than hiding them behind presets.
"""
from __future__ import annotations

from config import DEFAULT_HUBS
from handlers.search.hubs import (
    add_typed_hubs,
    apply_hub_preset,
    render_hubs,
    toggle_hub,
)
from handlers.search.draft import SearchDraft


def _draft(**kw) -> SearchDraft:
    base = dict(origin="LPA", origin_name="Gran Canaria")
    base.update(kw)
    return SearchDraft(**base)


def test_toggling_a_hub_adds_then_removes_it():
    d = toggle_hub(_draft(), "MAD")
    assert d.hub_codes == ("MAD",)

    d = toggle_hub(d, "MAD")
    assert d.hub_codes == ()


def test_a_toggled_hub_carries_its_known_name():
    d = toggle_hub(_draft(), "MAD")
    assert dict(d.hubs)["MAD"] == "Madrid"


def test_presets_select_the_documented_sets():
    assert apply_hub_preset(_draft(), "top2").hub_codes == ("MAD", "BCN")
    assert apply_hub_preset(_draft(), "top3").hub_codes == ("MAD", "BCN", "LIS")
    assert set(apply_hub_preset(_draft(), "all").hub_codes) == set(DEFAULT_HUBS)


def test_a_preset_replaces_rather_than_appends():
    """Tapping 'Top 2' after picking six hubs must give two, not eight."""
    d = apply_hub_preset(_draft(), "all")
    assert apply_hub_preset(d, "top2").hub_codes == ("MAD", "BCN")


def test_typed_codes_are_added_with_a_known_name_where_there_is_one():
    d = add_typed_hubs(_draft(), ["MAD", "ZRH"])
    hubs = dict(d.hubs)

    assert hubs["MAD"] == "Madrid"
    assert hubs["ZRH"] == "ZRH"     # unknown: the code doubles as the label


def test_typed_codes_merge_with_what_is_already_selected():
    d = toggle_hub(_draft(), "MAD")
    d = add_typed_hubs(d, ["BCN"])

    assert set(d.hub_codes) == {"MAD", "BCN"}


def test_typed_codes_do_not_duplicate_an_existing_hub():
    d = toggle_hub(_draft(), "MAD")
    d = add_typed_hubs(d, ["MAD"])

    assert d.hub_codes == ("MAD",)


def test_render_marks_selected_hubs():
    d = toggle_hub(_draft(), "MAD")
    _, rows = render_hubs(d)
    labels = {b.data: b.label for row in rows for b in row}

    assert labels["h:MAD"].startswith("✓")
    assert not labels["h:BCN"].startswith("✓")


def test_render_offers_every_known_hub():
    _, rows = render_hubs(_draft())
    data = {b.data for row in rows for b in row}

    for code in DEFAULT_HUBS:
        assert f"h:{code}" in data


def test_render_offers_the_presets_and_done():
    _, rows = render_hubs(_draft())
    data = {b.data for row in rows for b in row}

    assert {"hp:all", "hp:top2", "hp:top3", "back"} <= data


def test_render_warns_when_no_hub_is_selected():
    text, _ = render_hubs(_draft())
    assert "at least one" in text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_search_hubs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.search.hubs'`

- [ ] **Step 3: Write the hub screen**

Create `handlers/search/hubs.py`:

```python
"""Hub multi-select (spec §6.3).

Hubs are the search's cost driver -- phase 0 issues one calendar request
per hub per destination, so eight hubs cost four times what two do. The
screen therefore shows every known hub as a toggle rather than hiding them
behind presets, and the draft's request estimate updates as they change.
"""
from __future__ import annotations

from config import DEFAULT_HUBS, PORTUGAL_HUBS, SPAIN_HUBS
from handlers.search.draft import Button, Rows, SearchDraft
from handlers.utils import esc

_KNOWN = {**SPAIN_HUBS, **PORTUGAL_HUBS}

_PRESETS = {
    "all": tuple(DEFAULT_HUBS),
    "top2": ("MAD", "BCN"),
    "top3": ("MAD", "BCN", "LIS"),
}

_PER_ROW = 3


def _named(codes) -> tuple[tuple[str, str], ...]:
    """Pair each code with its known name, or itself when unknown."""
    return tuple((code, _KNOWN.get(code, code)) for code in codes)


def toggle_hub(draft: SearchDraft, code: str) -> SearchDraft:
    """Add *code* if absent, remove it if present."""
    current = list(draft.hub_codes)
    if code in current:
        current.remove(code)
    else:
        current.append(code)
    return draft.with_(hubs=_named(current))


def apply_hub_preset(draft: SearchDraft, preset: str) -> SearchDraft:
    """Replace the selection with a preset set.

    Replaces rather than appends: tapping "Top 2" after picking six hubs
    must give two, not eight.
    """
    return draft.with_(hubs=_named(_PRESETS[preset]))


def add_typed_hubs(draft: SearchDraft, codes: list[str]) -> SearchDraft:
    """Merge typed codes into the selection, preserving order and uniqueness."""
    current = list(draft.hub_codes)
    for code in codes:
        if code not in current:
            current.append(code)
    return draft.with_(hubs=_named(current))


def render_hubs(draft: SearchDraft) -> tuple[str, Rows]:
    """The hub screen: (Telegram HTML, button rows)."""
    chosen = set(draft.hub_codes)

    lines = [
        "<b>Which hubs?</b>",
        "",
        "A hub is where the discounted domestic leg ends and the onward "
        "flight begins.",
    ]
    if chosen:
        lines.append(
            f"\nSelected: <b>{' '.join(esc(c) for c in draft.hub_codes)}</b> "
            f"({len(chosen)})"
        )
    else:
        lines.append("\n<i>Pick at least one hub, or use a preset.</i>")
    lines.append(
        "\nYou can also send codes directly: <code>MAD, BCN, ZRH</code>."
    )

    codes = list(DEFAULT_HUBS)
    rows: Rows = []
    for i in range(0, len(codes), _PER_ROW):
        rows.append([
            Button(f"{'✓' if c in chosen else ''}{c}", f"h:{c}")
            for c in codes[i:i + _PER_ROW]
        ])

    rows.append([
        Button("All", "hp:all"),
        Button("Top 2", "hp:top2"),
        Button("Top 3", "hp:top3"),
    ])
    rows.append([Button("⬅️ Done", "back")])
    return "\n".join(lines), rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_search_hubs.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the full suite and the linter**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: 438 passed, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add handlers/search/hubs.py tests/test_search_hubs.py
git commit -m "feat: add hub multi-select with presets and typed codes

Spec §6.3. Every known hub is a toggle rather than hidden behind presets:
hubs are the search's cost driver -- phase 0 issues one calendar request
per hub per destination -- so the choice deserves to be visible.

A preset replaces the selection rather than appending to it: tapping
'Top 2' after picking six must give two, not eight."
```

---

## Task 6: The builder

**Files:**
- Create: `handlers/search/builder.py`
- Test: `tests/test_search_builder.py`

**Interfaces:**
- Consumes: everything from Tasks 2–5, plus `run_and_report` and `_estimate_queries` from `handlers.search_flow`.
- Produces:
  - `BUILDING` (the single conversation state)
  - `build_search_conversation() -> ConversationHandler`
  - `async render_anchor(bot, chat_id, message_id, text, rows) -> int` — returns the live message id, which differs from the input when a resend was needed

**The two stranding paths.** This design can leave the user with no working panel in exactly two ways: the anchor edit fails because the message is gone, and deleting the user's typed echo is refused. Both resend and re-anchor. They are the only reason this task needs a fake bot at all.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_builder.py`:

```python
"""Tests for the builder's anchor lifecycle (spec §6.2).

One message holds the draft and every sub-screen, edited in place. The two
paths that can strand a user with no working panel -- the anchor being
gone, and a refused delete of the user's typed echo -- both resend and
re-anchor, and are the reason this file needs a fake bot at all. Every
other rule worth testing lives in draft.py, dates.py, places.py and
hubs.py, which need no bot.
"""
from __future__ import annotations

import pytest
from telegram.error import BadRequest, Forbidden

from handlers.search.builder import render_anchor
from handlers.search.draft import Button


class FakeBot:
    """Records edits and sends; can be told to fail either."""

    def __init__(self, *, edit_error=None):
        self.edit_error = edit_error
        self.edits: list[dict] = []
        self.sends: list[dict] = []
        self._next_id = 500

    async def edit_message_text(self, **kw):
        self.edits.append(kw)
        if self.edit_error:
            raise self.edit_error
        return None

    async def send_message(self, **kw):
        self.sends.append(kw)
        self._next_id += 1
        return type("Msg", (), {"message_id": self._next_id})()


ROWS = [[Button("Search", "go")]]


@pytest.mark.asyncio
async def test_render_edits_the_anchor_in_place():
    bot = FakeBot()

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live == 42
    assert len(bot.edits) == 1
    assert not bot.sends


@pytest.mark.asyncio
async def test_an_unchanged_edit_is_not_an_error():
    """Telegram rejects an identical edit with 'Message is not modified'.
    Tapping a toggle twice must not look like a crash -- the same failure
    §6.6 calls out for the progress message."""
    bot = FakeBot(edit_error=BadRequest("Message is not modified"))

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live == 42
    assert not bot.sends


@pytest.mark.asyncio
async def test_a_missing_anchor_is_resent_and_re_anchored():
    """If the user deleted the panel, editing it fails forever. Resending
    is the only way back; returning the new id is how the caller keeps
    editing the right message."""
    bot = FakeBot(edit_error=BadRequest("Message to edit not found"))

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live != 42
    assert len(bot.sends) == 1
    assert bot.sends[0]["text"] == "draft"


@pytest.mark.asyncio
async def test_a_forbidden_edit_is_resent_too():
    bot = FakeBot(edit_error=Forbidden("no rights"))

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live != 42
    assert len(bot.sends) == 1


@pytest.mark.asyncio
async def test_render_sends_a_first_anchor_when_there_is_none():
    bot = FakeBot()

    live = await render_anchor(bot, chat_id=1, message_id=None,
                               text="draft", rows=ROWS)

    assert live == 501
    assert not bot.edits


@pytest.mark.asyncio
async def test_render_uses_html_and_suppresses_link_previews():
    bot = FakeBot()

    await render_anchor(bot, chat_id=1, message_id=42, text="a <b>b</b>",
                        rows=ROWS)

    assert bot.edits[0]["parse_mode"] == "HTML"
    assert bot.edits[0]["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_rows_become_a_real_inline_keyboard():
    bot = FakeBot()

    await render_anchor(bot, chat_id=1, message_id=42, text="draft",
                        rows=[[Button("Search", "go"), Button("Reset", "reset")]])

    markup = bot.edits[0]["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "Search"
    assert markup.inline_keyboard[0][1].callback_data == "reset"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_search_builder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.search.builder'`

- [ ] **Step 3: Write the builder**

Create `handlers/search/builder.py`:

```python
"""The hub-and-spoke builder (spec §6.2).

One anchor message holds the draft and every sub-screen, edited in place.
ConversationHandler keeps a single BUILDING state; which screen is showing
is a field on the draft, so Back is a re-render rather than a transition
and "Back and Edit exist everywhere" is true by construction.

Free text breaks the single-panel illusion -- Telegram appends the user's
message, so the panel is no longer last on screen. The handler therefore
deletes it after reading. Both failure paths (the anchor is gone, the
delete is refused) resend and re-anchor rather than stranding the user.
"""
from __future__ import annotations

import logging
from datetime import date as date_cls

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import DEFAULT_HUBS, ELIGIBLE_ORIGINS, ORIGIN
from handlers.search import dates as dates_mod
from handlers.search import hubs as hubs_mod
from handlers.search import places as places_mod
from handlers.search.draft import (
    AWAIT_DEST,
    AWAIT_HUBS,
    AWAIT_TRIP_DAYS,
    MAX_DESTINATIONS,
    MAX_TRIP_DAYS,
    SCREEN_DATES,
    SCREEN_DEST,
    SCREEN_DRAFT,
    SCREEN_HUBS,
    SCREEN_TRIP,
    Button,
    Rows,
    SearchDraft,
)
from handlers.search_flow import _estimate_queries, run_and_report
from handlers.start import MAIN_MENU_KEYBOARD, owner_only, owner_only_callback
from handlers.utils import ValidationError, parse_positive_int
from providers.base import ProviderError, SupportsCalendar
from providers.registry import primary_provider

logger = logging.getLogger(__name__)

BUILDING = 0

_ANCHOR = "anchor_id"
_DRAFT = "draft"
_MONTH = "month"
_RATINGS = "ratings"
_RESULTS = "results"
_TERM = "term"

_TRIP_PRESETS = (7, 10, 14, 21)


# ── The anchor ───────────────────────────────────────────────────────────────

def _markup(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.label, callback_data=b.data) for b in row]
         for row in rows]
    )


async def render_anchor(bot, chat_id: int, message_id: int | None,
                        text: str, rows: Rows) -> int:
    """Show *text* in the anchor, returning the live message id.

    The returned id differs from *message_id* when a resend was needed.
    Callers must store it back, or every later edit targets a message that
    is no longer there.

    An identical edit is not an error: Telegram rejects it with
    "Message is not modified", which is a no-op, not a failure -- the same
    case §6.6 calls out for the progress message. Any other edit failure
    means the panel is unusable, so it is resent.
    """
    markup = _markup(rows)

    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode="HTML", reply_markup=markup,
                disable_web_page_preview=True,
            )
            return message_id
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return message_id
            logger.info("Anchor %s unusable (%s) — resending.", message_id, exc)
        except Forbidden as exc:
            logger.info("Anchor %s forbidden (%s) — resending.", message_id, exc)

    message = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
        disable_web_page_preview=True,
    )
    return message.message_id


def _draft_of(context) -> SearchDraft:
    return context.user_data[_DRAFT]


def _store(context, draft: SearchDraft) -> None:
    context.user_data[_DRAFT] = draft


def _today() -> str:
    return date_cls.today().strftime("%Y-%m-%d")


async def _show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Render whichever screen the draft says is current."""
    draft = _draft_of(context)
    chat_id = update.effective_chat.id

    if draft.screen == SCREEN_DATES:
        text, rows = _dates_screen(context, draft)
    elif draft.screen == SCREEN_HUBS:
        text, rows = hubs_mod.render_hubs(draft)
    elif draft.screen == SCREEN_DEST:
        text, rows = places_mod.render_picker(
            draft, field="dest",
            results=context.user_data.get(_RESULTS, []),
            term=context.user_data.get(_TERM, ""),
        )
    elif draft.screen == SCREEN_TRIP:
        text, rows = _trip_screen()
    else:
        estimate = _estimate_queries(
            hubs=len(draft.hubs), dests=len(draft.destinations),
            dates=len(draft.effective_dates), round_trip=bool(draft.trip_days),
        ) if draft.is_ready else None
        text, rows = draft.render(estimate=estimate)

    live = await render_anchor(context.bot, chat_id,
                               context.user_data.get(_ANCHOR), text, rows)
    context.user_data[_ANCHOR] = live
    return BUILDING


def _dates_screen(context, draft: SearchDraft) -> tuple[str, Rows]:
    year, month = context.user_data.get(_MONTH, _current_month())
    dest = draft.dest_codes[0] if draft.destinations else None
    ratings = context.user_data.get(_RATINGS, {}).get(f"{dest}:{year}-{month}")
    rows = dates_mod.month_rows(year, month, draft=draft, today=_today(),
                                ratings=ratings)
    return dates_mod.caption(draft, dest_code=dest if ratings else None), rows


def _current_month() -> tuple[int, int]:
    today = date_cls.today()
    return today.year, today.month


def _trip_screen() -> tuple[str, Rows]:
    rows: Rows = [
        [Button("One-way", "trip:0")],
        [Button(f"{d} days", f"trip:{d}") for d in _TRIP_PRESETS[:2]],
        [Button(f"{d} days", f"trip:{d}") for d in _TRIP_PRESETS[2:]],
        [Button("Custom…", "trip:custom")],
        [Button("⬅️ Back", "back")],
    ]
    return ("<b>One-way or round-trip?</b>\n\n"
            "For a round trip, pick how long the trip lasts.", rows)


# ── Entry and exit ───────────────────────────────────────────────────────────

@owner_only_callback
async def entry_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open a fresh draft, taking over the message the menu button was on."""
    query = update.callback_query
    await query.answer()

    context.user_data.clear()
    context.user_data[_ANCHOR] = query.message.message_id
    _store(context, SearchDraft(
        origin=ORIGIN,
        origin_name=ELIGIBLE_ORIGINS.get(ORIGIN, ORIGIN),
    ))
    return await _show(update, context)


@owner_only_callback
async def back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the draft from any sub-screen. The whole point of §6.2."""
    await update.callback_query.answer()
    context.user_data.pop(_RESULTS, None)
    context.user_data.pop(_TERM, None)
    _store(context, _draft_of(context).with_(screen=SCREEN_DRAFT, awaiting=None))
    return await _show(update, context)


@owner_only_callback
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("Draft cleared.")
    anchor = context.user_data.get(_ANCHOR)
    context.user_data.clear()
    context.user_data[_ANCHOR] = anchor
    _store(context, SearchDraft(
        origin=ORIGIN, origin_name=ELIGIBLE_ORIGINS.get(ORIGIN, ORIGIN),
    ))
    return await _show(update, context)


@owner_only_callback
async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open the sub-screen for one field."""
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]

    screens = {"dest": SCREEN_DEST, "trip": SCREEN_TRIP,
               "dates": SCREEN_DATES, "hubs": SCREEN_HUBS}
    awaiting = {"dest": AWAIT_DEST, "hubs": AWAIT_HUBS}

    draft = _draft_of(context).with_(screen=screens[field],
                                     awaiting=awaiting.get(field))

    if screens[field] == SCREEN_DATES:
        context.user_data[_MONTH] = _current_month()
        await _load_ratings(context, draft)

    _store(context, draft)
    return await _show(update, context)


async def _load_ratings(context, draft: SearchDraft) -> None:
    """Fetch the §6.4 direct-fare signal for the visible month, if possible.

    Silent on failure by design: the colours are a decoration and the grid
    renders uncoloured without them. Letting a ProviderError reach the user
    here would break a working picker over an optional hint.
    """
    if not draft.destinations:
        return
    provider = primary_provider()
    if not isinstance(provider, SupportsCalendar):
        return

    year, month = context.user_data.get(_MONTH, _current_month())
    dest = draft.dest_codes[0]
    key = f"{dest}:{year}-{month}"
    cache = context.user_data.setdefault(_RATINGS, {})
    if key in cache:
        return

    import calendar as _cal

    from providers.base import CalendarQuery

    last = _cal.monthrange(year, month)[1]
    try:
        table = await provider.price_calendar(CalendarQuery(
            origin=draft.origin, dest=dest,
            start=f"{year:04d}-{month:02d}-01",
            end=f"{year:04d}-{month:02d}-{last:02d}",
            adults=draft.adults, currency=draft.currency,
        ))
    except ProviderError as exc:
        logger.info("No date ratings for %s (%s) — rendering uncoloured.",
                    dest, exc)
        cache[key] = {}
        return

    cache[key] = {day: rated.rating for day, rated in table.items()}


# ── Dates ────────────────────────────────────────────────────────────────────

@owner_only_callback
async def date_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    date = query.data.split(":", 1)[1]

    draft, alert = dates_mod.apply_day_tap(_draft_of(context), date,
                                           today=_today())
    await query.answer(alert or "", show_alert=bool(alert))
    if alert:
        return BUILDING

    _store(context, draft)
    return await _show(update, context)


@owner_only_callback
async def month_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    year, month = (int(p) for p in query.data.split(":", 1)[1].split("-"))
    context.user_data[_MONTH] = (year, month)
    await _load_ratings(context, _draft_of(context))
    return await _show(update, context)


@owner_only_callback
async def date_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    preset = query.data.split(":", 1)[1]
    _store(context, dates_mod.apply_preset(_draft_of(context), preset,
                                           today=_today()))
    return await _show(update, context)


@owner_only_callback
async def date_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    _store(context, dates_mod.switch_mode(_draft_of(context), mode))
    return await _show(update, context)


@owner_only_callback
async def date_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _store(context, _draft_of(context).with_(window_start=None, window_end=None,
                                             picked_days=()))
    return await _show(update, context)


# ── Trip shape ───────────────────────────────────────────────────────────────

@owner_only_callback
async def trip_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "custom":
        _store(context, _draft_of(context).with_(awaiting=AWAIT_TRIP_DAYS))
        text = (f"<b>How long is the trip?</b>\n\nSend a number of days "
                f"(1–{MAX_TRIP_DAYS}).")
        rows: Rows = [[Button("⬅️ Back", "back")]]
        live = await render_anchor(context.bot, update.effective_chat.id,
                                   context.user_data.get(_ANCHOR), text, rows)
        context.user_data[_ANCHOR] = live
        return BUILDING

    _store(context, _draft_of(context).with_(trip_days=int(choice),
                                             screen=SCREEN_DRAFT, awaiting=None))
    return await _show(update, context)


# ── Hubs ─────────────────────────────────────────────────────────────────────

@owner_only_callback
async def hub_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _store(context, hubs_mod.toggle_hub(_draft_of(context),
                                        query.data.split(":", 1)[1]))
    return await _show(update, context)


@owner_only_callback
async def hub_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _store(context, hubs_mod.apply_hub_preset(_draft_of(context),
                                              query.data.split(":", 1)[1]))
    return await _show(update, context)


# ── Places ───────────────────────────────────────────────────────────────────

@owner_only_callback
async def place_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle one resolved place into or out of the draft."""
    query = update.callback_query
    _, field, code = query.data.split(":", 2)
    draft = _draft_of(context)

    results = {p.code: p for p in context.user_data.get(_RESULTS, [])}
    place = results.get(code)
    name = f"{place.city}" if place else code

    if field == "dest":
        current = list(draft.destinations)
        if code in draft.dest_codes:
            current = [(c, n) for c, n in current if c != code]
        elif len(current) >= MAX_DESTINATIONS:
            await query.answer(
                f"At most {MAX_DESTINATIONS} destinations — the search would "
                "take hours.", show_alert=True)
            return BUILDING
        else:
            current.append((code, name))
        draft = draft.with_(destinations=tuple(current))
    else:
        draft = hubs_mod.toggle_hub(draft, code)

    await query.answer()
    _store(context, draft)
    return await _show(update, context)


# ── Typed text ───────────────────────────────────────────────────────────────

@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Route a typed message to whichever screen is waiting for one.

    The user's message is deleted so the anchor stays last on screen. A
    refused delete is not fatal: _show resends the anchor, which puts the
    panel back in front of them.
    """
    text = update.message.text
    draft = _draft_of(context)

    try:
        await update.message.delete()
    except (BadRequest, Forbidden, TelegramError) as exc:
        # Telegram refuses after 48h or without delete rights. The panel is
        # now above the user's message, so force a resend rather than an edit.
        logger.info("Could not delete the user's message (%s) — re-anchoring.",
                    exc)
        context.user_data[_ANCHOR] = None

    if draft.awaiting == AWAIT_TRIP_DAYS:
        return await _handle_trip_text(update, context, text)
    if draft.awaiting in (AWAIT_DEST, AWAIT_HUBS):
        return await _handle_place_text(update, context, text, draft)
    return await _show(update, context)


async def _handle_trip_text(update, context, text: str) -> int:
    try:
        days = parse_positive_int(text, field="days", maximum=MAX_TRIP_DAYS)
    except ValidationError as exc:
        await update.effective_chat.send_message(str(exc), parse_mode="HTML")
        return BUILDING

    _store(context, _draft_of(context).with_(trip_days=days,
                                             screen=SCREEN_DRAFT, awaiting=None))
    return await _show(update, context)


async def _handle_place_text(update, context, text: str, draft) -> int:
    field = "dest" if draft.awaiting == AWAIT_DEST else "hubs"

    codes = places_mod.try_parse_codes(text)
    if codes is not None:
        # §6.3's power-user path: typed codes are accepted with no request.
        if field == "dest":
            known = dict(draft.destinations)
            merged = list(draft.destinations)
            for code in codes:
                if code not in known:
                    merged.append((code, code))
            _store(context, draft.with_(destinations=tuple(merged[:MAX_DESTINATIONS])))
        else:
            _store(context, hubs_mod.add_typed_hubs(draft, codes))
        context.user_data.pop(_RESULTS, None)
        return await _show(update, context)

    context.user_data[_TERM] = text
    error = None
    try:
        results = await places_mod.resolve(text)
    except ProviderError as exc:
        logger.warning("Place lookup failed for %r: %s", text, exc)
        results, error = [], "Name search is unavailable right now."

    context.user_data[_RESULTS] = results
    body, rows = places_mod.render_picker(draft, field=field, results=results,
                                          term=text, error=error)
    live = await render_anchor(context.bot, update.effective_chat.id,
                               context.user_data.get(_ANCHOR), body, rows)
    context.user_data[_ANCHOR] = live
    return BUILDING


# ── Launching the search ─────────────────────────────────────────────────────

@owner_only_callback
async def go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the search, or say what is still missing."""
    query = update.callback_query
    draft = _draft_of(context)

    if not draft.is_ready:
        await query.answer(f"Still needed: {', '.join(draft.missing)}",
                           show_alert=True)
        return BUILDING

    await query.answer()
    await query.edit_message_text("On it — I'll message you when it's done.")

    context.application.create_task(
        run_and_report(context.application.bot, update.effective_chat.id,
                       draft.to_params()),
        update=update,
    )
    context.user_data.clear()
    return ConversationHandler.END


@owner_only_callback
async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A padding or header cell. Telegram needs an answer or it spins."""
    await update.callback_query.answer()
    return BUILDING


@owner_only_callback
async def to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Welcome to Flight Finder!\n"
                                  "Use the menu below to get started.",
                                  reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Search cancelled.",
                                    reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


# ── Builder ──────────────────────────────────────────────────────────────────

def build_search_conversation() -> ConversationHandler:
    """The search conversation.

    One state. Every screen is a re-render of the same anchor from the same
    draft, so there is no transition table to keep consistent -- which is
    what made the old eleven-state chain unable to go backwards at all.
    """
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(entry_search, pattern="^menu_search$")],
        states={
            BUILDING: [
                CallbackQueryHandler(edit_field, pattern=r"^edit:"),
                CallbackQueryHandler(date_tap, pattern=r"^d:\d{4}-\d{2}-\d{2}$"),
                CallbackQueryHandler(month_nav, pattern=r"^m:\d{4}-\d{1,2}$"),
                CallbackQueryHandler(date_preset, pattern=r"^dp:"),
                CallbackQueryHandler(date_mode, pattern=r"^dm:"),
                CallbackQueryHandler(date_clear, pattern="^dclear$"),
                CallbackQueryHandler(trip_choice, pattern=r"^trip:"),
                CallbackQueryHandler(hub_tap, pattern=r"^h:[A-Z]{3}$"),
                CallbackQueryHandler(hub_preset, pattern=r"^hp:"),
                CallbackQueryHandler(place_tap, pattern=r"^p:(dest|hubs):[A-Z]{3}$"),
                CallbackQueryHandler(back, pattern="^back$"),
                CallbackQueryHandler(reset, pattern="^reset$"),
                CallbackQueryHandler(go, pattern="^go$"),
                CallbackQueryHandler(to_menu, pattern="^menu_main$"),
                CallbackQueryHandler(noop, pattern="^noop$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_search_builder.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Run the full suite and the linter**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: 445 passed, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add handlers/search/builder.py tests/test_search_builder.py
git commit -m "feat: add the hub-and-spoke builder over a single anchor message

Spec §6.2. ConversationHandler keeps one BUILDING state; which screen is
showing is a field on the draft, so Back is a re-render rather than a
transition and there is no edge table to keep consistent -- which is what
made the old eleven-state chain unable to go backwards at all.

render_anchor returns the live message id because it may have resent. The
two paths that can strand a user -- the anchor being gone, and a refused
delete of the user's typed echo -- both resend and re-anchor, and are what
the fake bot in the tests exists to cover.

The §6.4 date ratings are fetched with ProviderError swallowed: colours
are a decoration and the grid renders uncoloured without them. Breaking a
working picker over an optional hint would be the wrong trade."
```

---

## Task 7: Wire it up and retire the old conversation

**Files:**
- Modify: `handlers/search/__init__.py`
- Modify: `bot.py` (the `from handlers.search_flow import build_search_conversation` line)
- Modify: `handlers/search_flow.py` — delete the conversation, keep the rest
- Modify: `README.md`

**Interfaces:**
- Consumes: `build_search_conversation` from `handlers.search.builder`.
- Produces: `handlers.search.build_search_conversation`.

**What is deleted from `handlers/search_flow.py`:** the eleven state constants, `DATE_MODE_KEYBOARD`, `MAX_TRIP_DAYS`, `MAX_DESTINATIONS`, `entry_search`, `dest_input`, `trip_oneway`, `trip_roundtrip`, `tripdays_preset`, `tripdays_custom_prompt`, `tripdays_custom_input`, `datemode_fixed`, `datemode_range`, `fixed_dates_input`, `range_start_input`, `range_end_input`, `range_every_input`, `_hub_keyboard`, `_ask_hubs`, `hubs_preset`, `hubs_custom`, `custom_hubs_input`, `_summary_text`, `_show_confirm`, `confirm_go`, `confirm_cancel`, `cancel_command`, `build_search_conversation`.

**What stays, and why:** `run_and_report`, `_estimate_queries`, `_oversized_window_message`. All seven `monkeypatch.setattr` sites in `tests/test_search_flow.py` patch attributes on this module object — `run_search` in three, `primary_provider` in four. A moved function resolves those names in its new module's namespace, so the patches would silently stop taking effect. Layer 3b rewrites results and has to touch those tests anyway; the move belongs there, with them.

Remove any import that only the deleted code used. After the deletion the file should still import: `json`, `logging`, `db.save_search`, `engine.run_search`, `handlers.utils` (`esc`, `split_message`), `models.SearchWindow`, `providers.base.SupportsCalendar`, `providers.registry.primary_provider`, `search` (`format_results`, `itineraries_to_json`, `scan_to_json`), and from `config`: `FALLBACK_MAX_DATES`, `MAX_WINDOW_DAYS`, `SHORTLIST_SIZE`, `THROUGH_FARE_DATES`. Let ruff tell you what is now unused.

- [ ] **Step 1: Export the builder**

Write `handlers/search/__init__.py`:

```python
"""The guided search conversation (spec §6.2-§6.4).

Split out of handlers/search_flow.py, which had grown to 764 lines and
held four of the whole-branch review's findings. Layer 3b moves
run_and_report here too, alongside the results rewrite that has to touch
its tests anyway.
"""
from handlers.search.builder import build_search_conversation

__all__ = ["build_search_conversation"]
```

- [ ] **Step 2: Point `bot.py` at it**

In `bot.py`, change:

```python
from handlers.search_flow import build_search_conversation
```

to:

```python
from handlers.search import build_search_conversation
```

- [ ] **Step 3: Verify the whole suite still passes before deleting anything**

Run: `.venv/bin/pytest -q`
Expected: 445 passed. Both conversations exist at this point; only the new one is registered.

- [ ] **Step 4: Delete the old conversation**

Delete every name listed under "What is deleted" above from `handlers/search_flow.py`, then update its module docstring:

```python
"""Running a search and reporting it.

The guided conversation moved to ``handlers/search/`` in Layer 3a. What is
left is the engine-facing half: ``run_and_report``, shared by the builder
and by history reruns, and the two pure helpers the pre-flight summary
needs.

These stay here rather than moving with the conversation because
``tests/test_search_flow.py`` patches ``run_search`` and
``primary_provider`` as attributes of *this module*; a moved function
resolves those names in its new namespace and the patches would silently
stop taking effect. Layer 3b rewrites results and has to touch those tests
anyway -- the move belongs there.
"""
```

- [ ] **Step 5: Verify nothing referenced the deleted code**

Run:
```bash
.venv/bin/ruff check . && \
grep -rn "search_flow" --include=*.py . | grep -v "^./.venv" | grep -v "^./tests/test_search_flow.py"
```
Expected: ruff clean. The grep should show only `handlers/search/builder.py`'s import of `_estimate_queries` and `run_and_report`, and `handlers/history.py`'s deferred `from handlers.search_flow import run_and_report`.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: 445 passed. **All 16 `tests/test_search_flow.py` tests must pass with no edit.** If one fails, the deletion took something it should not have — restore it rather than editing the test.

- [ ] **Step 7: Update the README**

In the Features section, replace the first two bullets:

```markdown
- **Guided search** — a single draft message you edit in place: pick
  destinations, trip shape, dates and hubs in any order, with Back and Edit
  on every field and a query-count estimate before anything is fetched.
- **Place search** — type a city or airport name and pick from the matches;
  no IATA code needed. Pasting codes still works. Falls back to codes when
  no configured provider can resolve names.
- **Date picker** — a month grid. Choose a window (the engine prices every
  day in it) or tap individual days. Where the provider has a price
  calendar, days are marked with a direct-fare signal for your first
  destination.
```

In the repo-structure block, replace the `search_flow.py` line:

```
  search_flow.py        run_and_report: run a search, report it, persist it
  search/               the guided search conversation
    draft.py            SearchDraft: fields, screen state, draft rendering
    builder.py          anchor message, single-state routing, Back
    places.py           name-to-airport autocomplete with a typed-code fallback
    dates.py            month grid: window and multi-day selection
    hubs.py             hub multi-select and presets
```

- [ ] **Step 8: Commit**

```bash
git add handlers/search/__init__.py bot.py handlers/search_flow.py README.md
git commit -m "feat: retire the linear search conversation for the draft builder

The eleven-state chain had no Back at any step, so a typo at step six meant
/cancel and start over; it required IATA codes from memory and dates as
typed YYYY-MM-DD strings. All of that is now a draft you edit in place.

run_and_report and the two pure helpers stay in search_flow.py. All 16 of
its tests pass with no edit -- which is the check that this deletion took
only the conversation."
```

---

## Task 8: Manual verification and the carry-forward

**Files:**
- Create: `docs/plans/2026-09-01-layer-3a-carry-forward.md`

- [ ] **Step 1: Verify the module boundaries actually hold**

Run:
```bash
.venv/bin/python -c "
import sys
import handlers.search.draft, handlers.search.dates
bad = sorted(m for m in sys.modules if m.startswith('telegram'))
assert not bad, bad
print('draft.py and dates.py are telegram-free')"
```

- [ ] **Step 2: Verify a Google-only deployment still builds a search**

Run:
```bash
PROVIDERS=google PRIMARY_PROVIDER=google .venv/bin/python -c "
import handlers.search.places as p
assert p.places_provider() is None
text, rows = p.render_picker(
    __import__('handlers.search.draft', fromlist=['SearchDraft']).SearchDraft(
        origin='LPA', origin_name='Gran Canaria'),
    field='dest', results=[], term='')
assert 'MAD' in text, text
print('google-only degrades to typed codes')"
```

- [ ] **Step 3: Confirm no forbidden field is ever set**

Run:
```bash
grep -rn "cabin\|children\|min_layover" handlers/search/ || echo "clean: no cabin/children/min_layover in the builder"
```
Expected: `clean: ...`. Google raises a bare `ProviderError` on all three; Layer 3c adds them with the handling that makes them safe.

- [ ] **Step 4: Full suite and linter one more time**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: 445 passed, ruff clean.

- [ ] **Step 5: Write the carry-forward**

Create `docs/plans/2026-09-01-layer-3a-carry-forward.md` recording, at minimum:

- **What 3b inherits:** the `savings_pct` sign guard for card renderers; splitting `STATUS_PARTIAL` from `STATUS_ESTIMATE` in display; `SearchResult.strategy` telling the user a grid search samples; `Progress.best_total` — populate or delete; the progress phase labels (`"Phase 1 (cross-check)"` arrives after `"Phase 2"`); README's per-leg airlines/stops/durations claim; moving `run_and_report` out of `search_flow.py` together with its tests' monkeypatch targets.
- **What 3c inherits:** the `run_search` signature and its four `LegQuery` call sites; the `ProviderError` handling that must ship in the same commit as any cabin selector; `favorites` replay of cabin/children/max_stops/min_layover; §7.2 scheduler consolidation and the grid window end-date drop.
- **Decisions 3a made that bind later work:** place resolution keys off any `SupportsPlaces` provider rather than the primary one; window mode stores `dates` as the full expanded span rather than adding a `date_mode` column, which is what kept this stage free of persisted-shape changes; navigation state lives on the draft rather than in `ConversationHandler`, so adding a screen means adding a `SCREEN_*` constant and a branch in `_show`, never a new state.
- **Known gaps:** `_load_ratings` fetches one month at a time and caches per `(dest, month)` in `user_data`, so a long paging session accumulates entries that are never evicted; the ratings use only the *first* destination, per §6.4, which is silent about what a multi-destination draft should show; `try_parse_codes` reads any three-letter term as a code, so a user searching for a three-letter city name gets a code lookup instead.

- [ ] **Step 6: Commit**

```bash
git add docs/plans/2026-09-01-layer-3a-carry-forward.md
git commit -m "docs: record what Layer 3a deferred and what binds 3b and 3c"
```

---

## Self-Review

**Spec coverage.**

| Spec requirement | Task |
|---|---|
| §6.2 hub-and-spoke draft, edited in place | 2, 6 |
| §6.2 Back and Edit everywhere | 2 (`screen` on the draft), 6 (`back`) |
| §6.2 pre-flight request estimate | 2 (`render(estimate=)`), 6 (`_show`) |
| §6.2 the `handlers/search/` package split | 1–7 |
| §6.3 free text → provider `places` → up to 8 buttons | 4 |
| §6.3 multi-select with checkmarks and Done | 4, 5 |
| §6.3 pasted `JFK,LAX` still parses directly | 4 (`try_parse_codes`) |
| §6.3 resolved terms are cached | 1, 4 |
| §6.4 month grid, tap-start / tap-end | 3 |
| §6.4 presets (Next 30, Next 90, a month) | 3 |
| §6.4 picker ratings from one `origin→dest` calendar request | 3 (render), 6 (`_load_ratings`) |
| §6.4 uncoloured with no destination set | 3 |
| §6.4 labelled a *direct-fare signal*, naming the destination | 3 (`caption`) |
| §7.1 `place_cache` with a TTL | 1 |
| Discrete-date search preserved (design doc §6) | 3 (`MODE_DAYS`) |
| Google-only degradation | 4, 8 |
| §6.5 results, §6.6 progress and cancel | **deferred to 3b by design** — design doc §8 |

**Placeholder scan.** No `TBD`/`TODO`. Every code step carries the actual code. Two steps deliberately say "check the existing fixture name first" (Task 1 Step 1, Task 4 Step 1) — that is an instruction to read a specific file, not a deferred decision.

**Type consistency.** `Button`/`Rows` are defined once in Task 2 and imported by Tasks 3–6. `SearchDraft.with_` is used consistently, never `replace`. Callback-data prefixes are used identically in the renderers and the router patterns: `edit:`, `d:`, `m:`, `dp:`, `dm:`, `dclear`, `trip:`, `h:`, `hp:`, `p:<field>:`, `back`, `reset`, `go`, `menu_main`, `noop`. `render_anchor` returns `int` and every caller stores it back into `_ANCHOR`.

**One gap found and closed during review:** Task 3's `switch_mode` is used by Task 6's `date_mode` handler but was not in the original interface list; it is now declared in `dates.py` and covered by `test_a_preset_switches_back_to_window_mode` and the mode-toggle test.

**Test count math.** 362 baseline → 367 (T1) → 385 (T2) → 410 (T3) → 427 (T4) → 438 (T5) → 445 (T6). These are the expected values in each task's verification step; if a run lands elsewhere, reconcile before moving on rather than adjusting the number.
