# Layer 3b — Results View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the text dump a finished search produces with one live Telegram message: progress with Cancel, then a paged, filterable summary with a per-result detail view, all backed by SQLite so every button survives a restart.

**Architecture:** A new `results/` package holds the pure parts: `store.py` (full-fidelity v2 storage of itineraries plus readers for the two older shapes), `filters.py` (zero-request filters) and `view.py` (text and button specs for every screen). `handlers/results.py` owns `run_and_report`, the throttled progress message, and the `r:*` / `x:*` callbacks, and reads the search back from the database on every tap. The engine changes only to stamp a running best price on progress ticks.

**Tech Stack:** Python 3.10+, python-telegram-bot 21 (async), aiosqlite, pytest with `asyncio_mode = "auto"`, ruff.

**Spec:** `docs/plans/2026-09-26-layer-3b-results-design.md`. Read it before starting. Where this plan and the spec disagree, the plan wins and says why.

## Global Constraints

- Python 3.10 floor: CI runs 3.10–3.13. No `typing.Self`, no `ExceptionGroup`, no 3.11+ stdlib APIs.
- No new dependencies in `pyproject.toml`.
- The suite stays fully offline. No test may touch the network.
- Money is `Decimal` everywhere in Python. It becomes a float only at the existing `REAL` database columns (`best_price`, `through_fare`); inside the `results` JSON it is a string.
- Every provider- or user-supplied string interpolated into Telegram HTML goes through `handlers.utils.esc`.
- Every `callback_data` fits Telegram's 64-byte limit. Only the search id, an action letter and short arguments go in it.
- User-facing text is English.
- Work on branch `feat/results-view`. Commit after every task. **Never push or merge to `main`**: a push to `main` deploys to production.
- `ruff check .` and `pytest` are both clean at the end of every task.

## Review Focus

Inputs and failure modes the spec implies but no single feature test would naturally hit. Each has a test pinned in the owning task:

1. **A pre-3b search opened from history or an old button** (v1 or legacy `Route` rows): the summary renders as a labelled snapshot, has no Filters button, and a hand-crafted `r:<id>:f` callback falls back to the summary instead of crashing. (Task 7b)
2. **Filters that hide everything:** the summary says "No routes match these filters" and still offers the Filters button, so the user can clear them. (Task 5)
3. **A stored page number beyond the new page count** after a filter change: the view clamps to the last page. (Task 5)
4. **A round-trip whose return connection is unknown while the outbound is known:** the duration and buffer filters hide it, and do not evaluate only the outbound. (Task 4)
5. **A progress edit failing mid-search** (network hiccup, `RetryAfter`): the search still completes and renders its results. (Task 7a)

Also pinned: HTML-hostile provider strings in the detail view (Task 5) and a Cancel tap after the search finished (Task 7a).

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `handlers/results.py` | create (Task 1 move, grows in 7a/7b/8) | `run_and_report`, `ProgressMessage`, `on_results`, `on_cancel`, handler list |
| `handlers/search_flow.py` | delete (Task 1) | moved wholesale into `handlers/results.py` |
| `handlers/anchor.py` | create (Task 7a) | `markup`, `render_anchor`, moved out of the builder so `handlers/results.py` can use them without a circular import |
| `results/__init__.py` | create (Task 3) | package marker |
| `results/store.py` | create (Task 3) | `serialize`, `load`, `StoredResults`, `_itinerary_from_dict` (moved from history) |
| `results/filters.py` | create (Task 4) | `Filters`, `apply`, `carriers`, `journey_minutes` |
| `results/view.py` | create (Task 5) | `SearchMeta`, `summary`, `detail`, `filters_screen`, `progress_text`, `phase_label`, `savings_text`, `MENU_ROWS` |
| `models.py` | modify (Tasks 2, 6) | `ground_time`, `Itinerary.buffer_out/buffer_ret`, `Progress.best_confirmed` |
| `engine/orchestrator.py` | modify (Task 6) | `_BestStamp`; stamp best price on ticks |
| `db.py` | modify (Tasks 3, 7b) | `view_json` and `strategy` columns; `save_search(strategy=)`; `set_search_view` |
| `handlers/search/builder.py` | modify (Tasks 1, 7a) | import path; `go()` hands its anchor to `run_and_report` |
| `handlers/history.py` | modify (Tasks 1, 3, 7b) | imports; view opens the results summary |
| `handlers/favorites.py` | modify (Task 8) | legacy `savefav_` reads through `store.load` |
| `bot.py` | modify (Task 7b) | registers the results handlers |
| `search.py` | modify (Tasks 3, 9) | loses `itineraries_to_json` (3) and the formatter (9); keeps `scan_to_json` |
| `README.md` | modify (Task 9) | Features, Example output, Architecture |
| `tests/results_fixtures.py` | create (Task 2) | itinerary and offer builders shared by the 3b tests |
| `tests/test_results_run.py` | rename from `tests/test_search_flow.py` (Task 1) | `run_and_report` and pre-flight estimate tests |
| `tests/test_results_store.py` | create (Task 3) | |
| `tests/test_results_filters.py` | create (Task 4) | |
| `tests/test_results_view.py` | create (Task 5) | |
| `tests/test_results_handlers.py` | create (Tasks 7a, 7b, 8) | callbacks, progress, cancel |

---

### Task 1: Move `run_and_report` to `handlers/results.py` with no behaviour change

This is the move the 3a carry-forward warns about: `tests/test_search_flow.py` patches `run_search` and `primary_provider` as attributes of the module under test. Move the function and its tests together, so the patches keep taking effect.

**Files:**
- Create: `handlers/results.py` (the full content of `handlers/search_flow.py`)
- Delete: `handlers/search_flow.py`
- Rename: `tests/test_search_flow.py` → `tests/test_results_run.py`
- Modify: `handlers/search/builder.py:48`, `handlers/history.py:152`, and the docstrings that name the old module (`handlers/search/__init__.py`, `handlers/search/draft.py:15`, `tests/test_search_draft.py:7`, `tests/test_search_builder.py:16,161`, `tests/test_db.py:324`)

**Interfaces:**
- Produces: `handlers.results.run_and_report(bot, chat_id, params)`, `handlers.results._estimate_queries(...)`, `handlers.results._oversized_window_message(...)`, same signatures as today.

- [ ] **Step 1: Record the baseline test count**

Run: `.venv/bin/pytest --collect-only -q | tail -1`
Expected: a line like `N tests collected`. Write N down.

- [ ] **Step 2: Move the module and its tests**

```bash
git mv handlers/search_flow.py handlers/results.py
git mv tests/test_search_flow.py tests/test_results_run.py
sed -i '' 's/handlers\.search_flow/handlers.results/g; s/search_flow_module/results_module/g' tests/test_results_run.py
sed -i '' 's/from handlers\.search_flow import/from handlers.results import/' handlers/search/builder.py handlers/history.py
grep -rn "search_flow" --include='*.py' . | grep -v '^./.venv'
```

The `grep` lists the docstring mentions still left. Change each one to `handlers/results.py` or `tests/test_results_run.py` as appropriate. Then replace the module docstring at the top of `handlers/results.py` with:

```python
"""Running a search and presenting its results.

``run_and_report`` is shared by the builder and by history reruns.
``_estimate_queries`` gives the builder its pre-flight query count.
``_oversized_window_message`` is kept because tests still exercise it
directly.

``tests/test_results_run.py`` patches ``run_search`` and
``primary_provider`` as attributes of *this* module. Any function here must
look those names up through this module's globals, never through a local
import, or the patches silently stop taking effect.
"""
```

- [ ] **Step 3: Run the suite and compare the count**

Run: `.venv/bin/pytest -q && .venv/bin/pytest --collect-only -q | tail -1 && .venv/bin/ruff check .`
Expected: all pass, the count equals N, and ruff is clean.

- [ ] **Step 4: Prove the patches still take effect**

Temporarily point the `fake_engine` fixture at the wrong module:

```bash
sed -i '' 's/monkeypatch.setattr(results_module, "run_search", fake_run_search)/monkeypatch.setattr(__import__("engine"), "run_search", fake_run_search)/' tests/test_results_run.py
.venv/bin/pytest -q tests/test_results_run.py -k forwards_the_discrete_dates 2>&1 | tail -3
git checkout tests/test_results_run.py
```

Expected: the test FAILS, because the real engine runs instead of the fake and `calls` stays empty. After the `git checkout`, re-run it and confirm it PASSES.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: move run_and_report into handlers/results.py

Moved with its tests in one commit, per the Layer 3a carry-forward: the
tests patch run_search and primary_provider on the module under test, so
moving one without the other would leave the patches silently inert."
```

---

### Task 2: Self-transfer buffers on `Itinerary`

**Files:**
- Modify: `models.py` (add `ground_time` after `_CENTS`; add two properties to `Itinerary` after `requires_bag_recheck`)
- Create: `tests/results_fixtures.py`
- Test: `tests/test_models.py` (append)

**Interfaces:**
- Produces:
  - `models.ground_time(landed: Segment, leaving: Segment) -> timedelta | None`
  - `Itinerary.buffer_out -> timedelta | None`
  - `Itinerary.buffer_ret -> timedelta | None`
  - `tests/results_fixtures.py` exports `seg`, `offer`, `one_way`, `standard_one_way`, `standard_round_trip`.

- [ ] **Step 1: Create the shared test builders**

`tests/results_fixtures.py`:

```python
"""Builders for itineraries with real offers, shared by the Layer 3b tests.

The standard one-way lands in Madrid at 10:00 and leaves at 13:00, a 3-hour
self-transfer. The standard round trip adds a return with the same 3-hour
gap. Totals with the default 75% discount:

    one-way     100 * 0.25 + 500             = 525.00
    round trip  (100 + 90) * 0.25 + 500 + 450 = 997.50
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from models import Itinerary
from providers.base import Offer, Segment


def seg(origin, dest, dep, arr, *, carrier="IB", name="Iberia", no=None,
        duration=120) -> Segment:
    return Segment(
        origin=origin, dest=dest, carrier=carrier, carrier_name=name,
        flight_no=no or f"{carrier}100", duration=duration,
        dep_local=datetime.fromisoformat(dep) if dep else None,
        arr_local=datetime.fromisoformat(arr) if arr else None,
    )


def offer(price, *segments, stops=None, duration=None,
          url="https://example.test/book", **kw) -> Offer:
    segs = list(segments)
    return Offer(
        price=Decimal(price), currency="EUR",
        airlines=sorted({s.carrier_name for s in segs}),
        stops=len(segs) - 1 if stops is None else stops,
        duration=sum(s.duration for s in segs) if duration is None else duration,
        segments=segs, provider="kiwi", booking_url=url, **kw,
    )


def dom_out():
    return offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00"))


def onward_out():
    return offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00",
                            carrier="JL", name="JAL", duration=780))


def onward_ret():
    return offer("450", seg("NRT", "MAD", "2026-10-15T11:00", "2026-10-15T18:00",
                            carrier="JL", name="JAL", duration=840))


def dom_ret():
    return offer("90", seg("MAD", "LPA", "2026-10-15T21:00", "2026-10-15T22:45",
                           duration=165))


def one_way(*, dom=None, onward=None, date="2026-10-01", hub="MAD", dest="NRT",
            discount="0.75", through_fare=None, **kw) -> Itinerary:
    return Itinerary(
        date=date, return_date="", hub=hub, hub_name="Madrid", dest=dest,
        dest_name="Tokyo", discount=Decimal(discount), dom_out=dom,
        onward_out=onward,
        through_fare=Decimal(through_fare) if through_fare is not None else None,
        **kw,
    )


def standard_one_way(**kw) -> Itinerary:
    return one_way(dom=dom_out(), onward=onward_out(), **kw)


def standard_round_trip(**kw) -> Itinerary:
    fields = {
        "date": "2026-10-01", "return_date": "2026-10-15", "hub": "MAD",
        "hub_name": "Madrid", "dest": "NRT", "dest_name": "Tokyo",
        "discount": Decimal("0.75"), "dom_out": dom_out(),
        "onward_out": onward_out(), "onward_ret": onward_ret(),
        "dom_ret": dom_ret(),
    }
    fields.update(kw)
    return Itinerary(**fields)
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_models.py`:

```python
# ── Self-transfer buffers (Layer 3b) ─────────────────────────────────────────

from datetime import timedelta  # noqa: E402

from models import ground_time  # noqa: E402
from tests.results_fixtures import (  # noqa: E402
    offer,
    one_way,
    seg,
    standard_one_way,
    standard_round_trip,
)


def test_buffer_out_is_the_time_on_the_ground_at_the_hub():
    assert standard_one_way().buffer_out == timedelta(hours=3)


def test_buffer_ret_is_measured_on_the_return_at_the_hub():
    assert standard_round_trip().buffer_ret == timedelta(hours=3)


def test_a_one_way_has_no_return_buffer():
    assert standard_one_way().buffer_ret is None


def test_an_airport_change_is_unknown_not_a_number():
    """Local times at two different airports are in different timezones;
    subtracting them is meaningless, so the buffer must be unknown."""
    itin = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00")),
        onward=offer("500", seg("TOJ", "NRT", "2026-10-01T13:00", "2026-10-02T09:00")),
    )
    assert itin.buffer_out is None


def test_a_missing_time_makes_the_buffer_unknown():
    itin = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", None)),
        onward=offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00")),
    )
    assert itin.buffer_out is None


def test_a_missing_offer_makes_the_buffer_unknown():
    assert one_way(dom=None, onward=standard_one_way().onward_out).buffer_out is None


def test_an_offer_without_segments_makes_the_buffer_unknown():
    itin = one_way(dom=offer("100"), onward=standard_one_way().onward_out)
    assert itin.buffer_out is None


def test_an_impossible_connection_is_a_negative_buffer_not_hidden():
    """The second ticket leaving before the first lands is a real,
    reportable problem. It must come back negative, not as None."""
    itin = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T14:00")),
        onward=offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00")),
    )
    assert itin.buffer_out == timedelta(hours=-1)


def test_ground_time_is_the_same_rule_for_a_layover_inside_one_ticket():
    landed = seg("MAD", "CDG", "2026-10-01T13:00", "2026-10-01T15:00")
    leaving = seg("CDG", "NRT", "2026-10-01T16:30", "2026-10-02T11:00")
    assert ground_time(landed, leaving) == timedelta(minutes=90)
```

- [ ] **Step 3: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_models.py -q -k "buffer or ground_time"`
Expected: FAIL with `ImportError: cannot import name 'ground_time'`.

- [ ] **Step 4: Implement**

In `models.py`, change the import line `from providers.base import Offer` to `from providers.base import Offer, Segment`. Then add after `_CENTS = Decimal("0.01")`:

```python
def ground_time(landed: Segment, leaving: Segment) -> timedelta | None:
    """Time between *landed* arriving and *leaving* departing, at one airport.

    Segment times are local to their own airport, so subtracting two of them
    is only meaningful when both are at the *same* airport: then they share a
    timezone. That is exactly the case for a connection. An airport change
    (arrive at one airport, leave from another) returns None, as does a
    missing time. A negative result is kept: it means the connection is
    impossible, which a caller must be able to report.
    """
    if landed.dest != leaving.origin:
        return None
    if landed.arr_local is None or leaving.dep_local is None:
        return None
    return leaving.dep_local - landed.arr_local


def _self_transfer(arriving: Offer | None, departing: Offer | None) -> timedelta | None:
    """The gap between two separately booked tickets, or None if unknown."""
    if arriving is None or departing is None:
        return None
    if not arriving.segments or not departing.segments:
        return None
    return ground_time(arriving.segments[-1], departing.segments[0])
```

Add to `Itinerary`, after `requires_bag_recheck`:

```python
    @property
    def buffer_out(self) -> timedelta | None:
        """Time at the hub between the domestic and onward outbound tickets."""
        return _self_transfer(self.dom_out, self.onward_out)

    @property
    def buffer_ret(self) -> timedelta | None:
        """Time at the hub between the onward and domestic return tickets."""
        return _self_transfer(self.onward_ret, self.dom_ret)
```

- [ ] **Step 5: Run the tests and the suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS, and ruff is clean.

- [ ] **Step 6: Commit**

```bash
git add models.py tests/results_fixtures.py tests/test_models.py
git commit -m "feat: measure the self-transfer buffer between tickets at the hub"
```

---

### Task 3: v2 result storage, and the two new `searches` columns

**Files:**
- Create: `results/__init__.py`, `results/store.py`
- Modify: `db.py` (schema, `MIGRATIONS`, `save_search`)
- Modify: `handlers/results.py` (`run_and_report` writes v2 and `strategy`)
- Modify: `handlers/history.py` (read through `store.load`; drop `_itinerary_from_dict`)
- Modify: `search.py` (delete `itineraries_to_json`)
- Modify: `tests/test_search.py` (delete the three `itineraries_to_json` tests), `tests/test_regressions.py`, `tests/test_results_run.py`, `tests/test_db.py`
- Test: `tests/test_results_store.py`

**Interfaces:**
- Consumes: `Itinerary`, `Offer`, `Segment`.
- Produces:
  - `results.store.serialize(itineraries: list[Itinerary]) -> dict`: the JSON-ready value `db.save_search(results=...)` encodes.
  - `results.store.load(raw: object) -> StoredResults`
  - `@dataclass(frozen=True) class StoredResults: itineraries: list[Itinerary]; detailed: bool`
  - `results.store._itinerary_from_dict(d: dict) -> Itinerary`, the v1 and legacy reader, moved unchanged from `handlers/history.py`.
  - `db.save_search(..., strategy: str | None = None)`, plus `searches.view_json` and `searches.strategy` columns.

- [ ] **Step 1: Write the failing store tests**

`tests/test_results_store.py`:

```python
"""results.store: the three shapes searches.results holds in real databases."""
from __future__ import annotations

import json
from decimal import Decimal

from results.store import StoredResults, load, serialize
from tests.results_fixtures import offer, one_way, standard_one_way, standard_round_trip


def _round_trip(itineraries):
    return load(json.dumps(serialize(itineraries)))


def test_v2_round_trip_is_exact():
    itins = [standard_one_way(through_fare="700"), standard_round_trip()]
    assert _round_trip(itins) == StoredResults(itineraries=itins, detailed=True)


def test_v2_keeps_money_as_exact_decimal_not_float():
    raw = serialize([one_way(dom=offer("0.10"), onward=offer("0.20"))])
    assert raw["itineraries"][0]["dom_out"]["price"] == "0.10"
    assert _round_trip([one_way(dom=offer("0.10"), onward=offer("0.20"))]) \
        .itineraries[0].dom_out.price == Decimal("0.10")


def test_v2_keeps_unknown_bags_unknown():
    """None means 'the provider could not say', never zero."""
    restored = _round_trip([standard_one_way()]).itineraries[0]
    assert restored.dom_out.included_checked_bags is None


def test_v2_keeps_an_estimate_an_estimate():
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    restored = _round_trip([estimate]).itineraries[0]
    assert restored.status == "estimate"
    assert restored.total == estimate.total


def test_v2_writes_the_version_and_readable_totals():
    raw = serialize([standard_one_way()])
    assert raw["v"] == 2
    assert raw["itineraries"][0]["total"] == "525.00"


def test_a_v1_list_loads_as_undetailed_estimates():
    v1 = [{"date": "2026-09-01", "return_date": "2026-09-15", "hub": "MAD",
           "hub_name": "Madrid", "dest": "NRT", "dest_name": "Tokyo",
           "discount": 0.75, "dom_price": 148.0, "dom_discounted": 37.0,
           "onward_price": 575.0, "total": 612.0, "status": "confirmed",
           "through_fare": 785.0, "savings": 173.0, "savings_pct": 22,
           "requires_bag_recheck": None, "providers": ["kiwi"]}]
    stored = load(json.dumps(v1))
    assert stored.detailed is False
    assert stored.itineraries[0].status == "estimate"
    assert stored.itineraries[0].return_date == "2026-09-15"
    assert stored.itineraries[0].total == Decimal("612.00")


def test_an_unreadable_entry_is_skipped_not_fatal():
    v1 = [{"hub": "MAD", "dest": "NRT", "date": "2026-09-01"},     # no prices at all
          {"date": "2026-09-01", "hub": "MAD", "dest": "NRT", "dom_price": 100.0,
           "onward_price": 500.0, "discount": 0.75}]
    stored = load(json.dumps(v1))
    assert len(stored.itineraries) == 1
    assert stored.itineraries[0].total == Decimal("525.00")


def test_nothing_stored_loads_as_empty():
    assert load(None) == StoredResults(itineraries=[], detailed=False)
    assert load("not json") == StoredResults(itineraries=[], detailed=False)
    assert load('{"v": 99}') == StoredResults(itineraries=[], detailed=False)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'results'`.

- [ ] **Step 3: Implement `results/store.py`**

`results/__init__.py`:

```python
"""Everything about a finished search that is not Telegram: storage,
filters, and the text of each screen."""
```

`results/store.py`: write the module below. Then cut `_itinerary_from_dict` **verbatim, docstring included** out of `handlers/history.py` and paste it where the comment marks. Adjust only the docstring's sentence about `format_results`: it now says the view labels such rows as estimates.

```python
"""What the ``searches.results`` column holds, and how to read it back.

Three shapes exist in real databases, and all three must keep loading:

- **v2** (Layer 3b on): ``{"v": 2, "itineraries": [...]}`` with every
  offer, segment, time and booking link, so a stored search can be paged,
  filtered and opened in detail after any number of restarts. Money is a
  string, so it comes back as the exact ``Decimal`` it was.
- **v1**: a bare list of prices and metadata, no offers. Loads as
  estimates.
- **The pre-engine ``Route`` shape**: also a bare list, with the old field
  names. Also loads as estimates.

``StoredResults.detailed`` is False for the last two. That is what turns
off filters and the detail view for them: there is nothing to filter on.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from models import Itinerary
from providers.base import Offer, Segment

logger = logging.getLogger(__name__)

VERSION = 2
_LEGS = ("dom_out", "dom_ret", "onward_out", "onward_ret")


@dataclass(frozen=True)
class StoredResults:
    itineraries: list[Itinerary]
    detailed: bool


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _segment_to_dict(s: Segment) -> dict:
    return {
        "origin": s.origin, "dest": s.dest, "carrier": s.carrier,
        "carrier_name": s.carrier_name, "flight_no": s.flight_no,
        "duration": s.duration, "dep_local": _time(s.dep_local),
        "arr_local": _time(s.arr_local),
    }


def _segment_from_dict(d: dict) -> Segment:
    return Segment(
        origin=d["origin"], dest=d["dest"], carrier=d["carrier"],
        carrier_name=d["carrier_name"], flight_no=d["flight_no"],
        duration=d["duration"], dep_local=_parse_time(d.get("dep_local")),
        arr_local=_parse_time(d.get("arr_local")),
    )


def _offer_to_dict(o: Offer | None) -> dict | None:
    if o is None:
        return None
    return {
        "price": _money(o.price), "currency": o.currency,
        "airlines": list(o.airlines), "stops": o.stops, "duration": o.duration,
        "segments": [_segment_to_dict(s) for s in o.segments],
        "provider": o.provider, "booking_url": o.booking_url,
        "included_cabin_bags": o.included_cabin_bags,
        "included_checked_bags": o.included_checked_bags,
        "checked_bag_price": _money(o.checked_bag_price),
        "min_layover": o.min_layover, "pnr_count": o.pnr_count,
        "requires_bag_recheck": o.requires_bag_recheck,
    }


def _offer_from_dict(d: dict | None) -> Offer | None:
    if d is None:
        return None
    return Offer(
        price=Decimal(d["price"]), currency=d["currency"],
        airlines=list(d["airlines"]), stops=d["stops"], duration=d["duration"],
        segments=[_segment_from_dict(s) for s in d["segments"]],
        provider=d["provider"], booking_url=d.get("booking_url"),
        included_cabin_bags=d.get("included_cabin_bags"),
        included_checked_bags=d.get("included_checked_bags"),
        checked_bag_price=_decimal(d.get("checked_bag_price")),
        min_layover=d.get("min_layover"), pnr_count=d.get("pnr_count"),
        requires_bag_recheck=d.get("requires_bag_recheck"),
    )


def _itinerary_to_dict(it: Itinerary) -> dict:
    d = {
        "date": it.date, "return_date": it.return_date, "hub": it.hub,
        "hub_name": it.hub_name, "dest": it.dest, "dest_name": it.dest_name,
        "discount": _money(it.discount),
        "est_dom_price": _money(it.est_dom_price),
        "est_onward_price": _money(it.est_onward_price),
        "through_fare": _money(it.through_fare),
        "providers": list(it.providers),
        # Derived, for anyone reading the column by hand. load() ignores them.
        "total": _money(it.total),
        "status": it.status,
    }
    for leg in _LEGS:
        d[leg] = _offer_to_dict(getattr(it, leg))
    return d


def _itinerary_from_v2(d: dict) -> Itinerary:
    return Itinerary(
        date=d["date"], return_date=d.get("return_date", ""), hub=d["hub"],
        hub_name=d.get("hub_name", d["hub"]), dest=d["dest"],
        dest_name=d.get("dest_name", d["dest"]),
        discount=Decimal(d["discount"]),
        est_dom_price=_decimal(d.get("est_dom_price")),
        est_onward_price=_decimal(d.get("est_onward_price")),
        through_fare=_decimal(d.get("through_fare")),
        providers=tuple(d.get("providers", ())),
        **{leg: _offer_from_dict(d.get(leg)) for leg in _LEGS},
    )


def serialize(itineraries: list[Itinerary]) -> dict:
    """The value for ``searches.results``; ``db.save_search`` JSON-encodes it."""
    return {"v": VERSION, "itineraries": [_itinerary_to_dict(it) for it in itineraries]}


# ── v1 and legacy reader ─────────────────────────────────────────────────────
# (paste _itinerary_from_dict from handlers/history.py here, verbatim)


def load(raw: object) -> StoredResults:
    """Read any shape ``searches.results`` has ever held. Never raises."""
    if raw is None:
        return StoredResults(itineraries=[], detailed=False)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return StoredResults(itineraries=[], detailed=False)

    if isinstance(data, dict) and data.get("v") == VERSION:
        return StoredResults(
            itineraries=_each(_itinerary_from_v2, data.get("itineraries")), detailed=True,
        )
    if isinstance(data, list):
        return StoredResults(itineraries=_each(_itinerary_from_dict, data), detailed=False)
    return StoredResults(itineraries=[], detailed=False)


def _each(reader, entries: object) -> list[Itinerary]:
    """Read every entry that parses; skip, and log, one that doesn't.

    A hand-edited or half-written row must not take the whole search down
    with it: the rest of its results are still worth showing.
    """
    if not isinstance(entries, list):
        return []
    itineraries = []
    for entry in entries:
        try:
            itineraries.append(reader(entry))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            logger.warning("Skipping an unreadable stored result (%s): %r", exc, entry)
    return itineraries
```

- [ ] **Step 4: Run the store tests**

Run: `.venv/bin/pytest tests/test_results_store.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing DB test**

In `tests/test_db.py`, find the `SEARCHES_NEW_COLUMNS` set and add `"view_json"` and `"strategy"` to it. Then append:

```python
async def test_save_search_records_the_strategy(temp_db):
    search_id = await db_module.save_search(
        origin="LPA", destinations=["NRT"], dates=["2026-10-01"], hubs=["MAD"],
        adults=1, currency="EUR", best_price=None, best_route=None, results=None,
        strategy="grid",
    )
    row = await db_module.get_search_by_id(search_id)
    assert row["strategy"] == "grid"
    assert row["view_json"] is None
```

Run: `.venv/bin/pytest tests/test_db.py -q`
Expected: FAIL. The migration test fails on the missing columns, and `save_search` rejects `strategy`.

- [ ] **Step 6: Implement the migrations and `strategy`**

In `db.py`:

1. In the `CREATE TABLE IF NOT EXISTS searches` statement, replace the last line `scan_json    TEXT               -- phase 0 calendar grid, JSON blob` with:

   ```sql
       scan_json    TEXT,              -- phase 0 calendar grid, JSON blob
       strategy     TEXT,              -- "two-stage" | "grid"; NULL = unknown (pre-3b)
       view_json    TEXT               -- results view state: filters and page; NULL = defaults
   ```

2. Append to `MIGRATIONS`:

   ```python
       # Layer 3b: the results view's per-search state, and which strategy
       # produced the numbers (the grid samples dates; the view says so).
       ("searches", "strategy", "TEXT"),
       ("searches", "view_json", "TEXT"),
   ```

3. In `save_search`, add the keyword parameter `strategy: str | None = None` after `scan_json`, add `strategy` to the column list and one `?` to `VALUES`, and add `strategy` as the last tuple element. Add a sentence to the docstring: `*strategy* is the engine's SearchResult.strategy.`

Run: `.venv/bin/pytest tests/test_db.py -q`
Expected: PASS.

- [ ] **Step 7: Write v2 from `run_and_report`**

In `handlers/results.py`:
- Replace `from search import format_results, itineraries_to_json, scan_to_json` with `from search import format_results, scan_to_json` and `from results import store`.
- Replace `results_data = json.loads(itineraries_to_json(itineraries)) if itineraries else None` with `results_data = store.serialize(itineraries) if itineraries else None`.
- Add `strategy=result.strategy,` to the `save_search(...)` call.

In `tests/test_results_run.py`:
- Add `strategy="two-stage"` to the `SimpleNamespace(...)` the `fake_engine` fixture returns.
- Replace each of the three `{r["date"] for r in json.loads(stored["results"])}` with `{it.date for it in load(stored["results"]).itineraries}`, and add `from results.store import load` to the imports.

- [ ] **Step 8: Read through `store.load` in history, and delete the v1 writer**

In `handlers/history.py`: remove `_itinerary_from_dict` (it now lives in `results/store.py`) and the now-unused `Decimal` import only if nothing else uses it. In `history_view`, replace

```python
    result_dicts = load_json_list(row.get("results"))
    if not result_dicts:
```

with

```python
    itineraries = load(row.get("results")).itineraries
    if not itineraries:
```

and delete the line `itineraries = [_itinerary_from_dict(d) for d in result_dicts]`. Add `from results.store import load`.

In `search.py`: delete `itineraries_to_json` and the now-unused `import json`.

In `tests/test_search.py`: delete `test_itineraries_to_json_caps_at_25_entries`, `test_itineraries_to_json_round_trips_key_fields` and `test_itineraries_to_json_records_no_through_fare_as_none_not_zero`, and remove `itineraries_to_json` from its import. The store tests above replace them.

In `tests/test_regressions.py`:
- Change `from handlers.history import _itinerary_from_dict` to `from results.store import _itinerary_from_dict, load, serialize`.
- Remove `itineraries_to_json` from the `from search import` line.
- Rewrite the two "Bug 2" tests so they go through the v2 path:

```python
def test_stored_round_trip_survives_the_json_round_trip():
    restored = load(json.dumps(serialize([_round_trip_itinerary()]))).itineraries[0]
    assert restored.return_date == "2026-09-15", "return_date must survive storage"


def test_stored_round_trip_still_renders_as_round_trip():
    restored = load(json.dumps(serialize([_round_trip_itinerary()]))).itineraries[0]
    rendered = format_results([restored], "LPA")

    assert "Round-trip" in rendered
    assert "One-way" not in rendered
    assert "2026-09-01 — 2026-09-15" in rendered
```

`handlers/favorites.py` still reads `results` with `load_json_list`. With v2 that returns `[]`, so the legacy "Track this route" button would say "no stored results". Fix it now, so nothing is broken between tasks. In `save_favorite`, replace

```python
    results = load_json_list(row.get("results"))
    if not results:
```

with

```python
    itineraries = load(row.get("results")).itineraries
    if not itineraries:
```

and replace the use of `best = results[0]` and its dict lookups with an `Itinerary`:

```python
    best = min(itineraries, key=lambda it: it.total)
    ...
    check_dates = [str(d) for d in load_json_list(row.get("dates"))] or [best.date]
    await add_favorite(
        origin=row.get("origin") or ORIGIN,
        hub=best.hub,
        destination=best.dest,
        adults=row.get("adults") or 1,
        currency=row.get("currency") or "EUR",
        price=float(best.total),
        check_dates=check_dates,
        trip_days=trip_days,
        provider=row.get("provider"),
    )
    ...
    price_str = f" at {best.total:,.0f} {row.get('currency') or 'EUR'}"
```

In the confirmation message, change `best['hub']` / `best['dest']` to `best.hub` / `best.dest`. Keep the existing comment block above `provider=`. Add `from results.store import load`.

In `tests/test_handlers_favorites.py`, the `_save_search_with` defaults store a hand-made dict with no prices, which no reader can turn into an itinerary. Replace that `"results"` default with a real v2 value, and add a test:

```python
from results.store import serialize  # add to the imports
from tests.results_fixtures import standard_one_way  # add to the imports

        # in _save_search_with's defaults:
        "results": serialize([standard_one_way(date="2026-09-01")]),


async def test_save_favorite_tracks_the_cheapest_stored_result(temp_db, monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    search_id = await _save_search_with({
        "results": serialize([standard_one_way(date="2026-09-01", through_fare="900"),
                              standard_one_way(date="2026-09-02", hub="BCN",
                                               discount="1")]),
    })

    await save_favorite(_update(f"savefav_{search_id}"), None)

    fav = (await db_module.get_favorites())[0]
    assert fav["hub"] == "BCN"            # 500.00 beats 525.00
    assert fav["record_price"] == 500.0
```

Then run `grep -n '"results":' tests/test_handlers_favorites.py` and convert any other hand-made results dict in that file the same way.

- [ ] **Step 9: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS, and ruff is clean.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: store full itineraries (results v2) and the search strategy

The results column now keeps every offer, segment and booking link, so a
stored search can be paged, filtered and opened after a restart. v1 and
the pre-engine Route shape still load, as estimates."
```

---

### Task 4: Filters over the stored results

**Files:**
- Create: `results/filters.py`
- Test: `tests/test_results_filters.py`

**Interfaces:**
- Consumes: `Itinerary.buffer_out/buffer_ret` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class Filters(max_stops: int | None = None, max_hours: int | None = None, min_buffer_hours: int | None = None, exclude: frozenset[str] = frozenset())`
  - `Filters.active -> int`
  - `Filters.to_dict() -> dict`
  - `Filters.from_dict(d: object) -> Filters`
  - `Filters.with_setting(key: str, value: str) -> Filters`, which raises `ValueError` on an unknown key or value. Keys: `s` stops, `h` hours, `b` buffer, `x` toggle a carrier.
  - `apply(itineraries: list[Itinerary], filters: Filters) -> tuple[list[tuple[int, Itinerary]], int]`, returning `(index in the input, itinerary)` pairs in input order, and the hidden count.
  - `carriers(itineraries) -> list[tuple[str, str]]`, the sorted `(code, name)` pairs.
  - `journey_minutes(first, buffer, second) -> int | None`
  - Constants `STOPS_CHOICES = (0, 1)`, `HOURS_CHOICES = (12, 18, 24)`, `BUFFER_CHOICES = (2, 3, 4)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_results_filters.py`:

```python
"""results.filters: zero-request filters, where unknown never passes."""
from __future__ import annotations

from decimal import Decimal

import pytest

from results.filters import Filters, apply, carriers
from tests.results_fixtures import (
    offer,
    one_way,
    seg,
    standard_one_way,
    standard_round_trip,
)


def _shown(itins, filters):
    return [i for i, _ in apply(itins, filters)[0]]


def test_no_filters_shows_everything_including_estimates():
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    shown, hidden = apply([standard_one_way(), estimate], Filters())
    assert [i for i, _ in shown] == [0, 1]
    assert hidden == 0


def test_any_active_filter_hides_an_estimate():
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    shown, hidden = apply([estimate], Filters(max_stops=1))
    assert shown == []
    assert hidden == 1


def test_max_stops():
    one_stop = one_way(
        dom=standard_one_way().dom_out,
        onward=offer("450",
                     seg("MAD", "CDG", "2026-10-01T13:00", "2026-10-01T15:00"),
                     seg("CDG", "NRT", "2026-10-01T17:00", "2026-10-02T12:00")),
    )
    itins = [standard_one_way(), one_stop]
    assert _shown(itins, Filters(max_stops=0)) == [0]
    assert _shown(itins, Filters(max_stops=1)) == [0, 1]


def test_max_hours_counts_flying_plus_the_buffer():
    # 120 + 180 + 780 minutes = 18h exactly.
    assert _shown([standard_one_way()], Filters(max_hours=18)) == [0]
    assert _shown([standard_one_way()], Filters(max_hours=12)) == []


def test_min_buffer():
    assert _shown([standard_one_way()], Filters(min_buffer_hours=3)) == [0]
    assert _shown([standard_one_way()], Filters(min_buffer_hours=4)) == []


def test_exclude_a_carrier_on_any_segment():
    assert _shown([standard_one_way()], Filters(exclude=frozenset({"JL"}))) == []
    assert _shown([standard_one_way()], Filters(exclude=frozenset({"FR"}))) == [0]


def test_round_trip_with_an_unknown_return_connection_is_hidden():
    """Review Focus #4: the outbound buffer is known and fine, but the
    return's is not. Evaluating only the outbound would let it through."""
    rt = standard_round_trip(
        dom_ret=offer("90", seg("MAD", "LPA", None, "2026-10-15T22:45")),
    )
    assert _shown([rt], Filters(min_buffer_hours=2)) == []
    assert _shown([rt], Filters(max_hours=24)) == []
    assert _shown([rt], Filters(max_stops=1)) == [0]


def test_a_round_trip_missing_a_return_offer_is_hidden_by_any_filter():
    rt = standard_round_trip(dom_ret=None)
    assert _shown([rt], Filters(max_stops=1)) == []


def test_indices_refer_to_the_input_list_not_the_filtered_one():
    itins = [standard_one_way(), one_way(dom=None), standard_one_way(date="2026-10-02")]
    assert _shown(itins, Filters(max_stops=1)) == [0, 2]


def test_carriers_lists_every_code_once_sorted():
    assert carriers([standard_one_way(), standard_round_trip()]) == [
        ("IB", "Iberia"), ("JL", "JAL"),
    ]


def test_active_counts_each_set_filter_once():
    assert Filters().active == 0
    assert Filters(max_stops=0, exclude=frozenset({"FR", "IB"})).active == 2


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("s", "0", Filters(max_stops=0)),
        ("h", "18", Filters(max_hours=18)),
        ("b", "3", Filters(min_buffer_hours=3)),
        ("x", "FR", Filters(exclude=frozenset({"FR"}))),
    ],
)
def test_with_setting(key, value, expected):
    assert Filters().with_setting(key, value) == expected


def test_with_setting_any_clears_and_x_toggles_off():
    f = Filters(max_stops=0, exclude=frozenset({"FR"}))
    assert f.with_setting("s", "any").max_stops is None
    assert f.with_setting("x", "FR").exclude == frozenset()


@pytest.mark.parametrize(("key", "value"), [("s", "7"), ("q", "1"), ("h", "abc")])
def test_with_setting_rejects_what_no_button_sends(key, value):
    with pytest.raises(ValueError):
        Filters().with_setting(key, value)


def test_dict_round_trip_and_tolerance():
    f = Filters(max_stops=1, max_hours=24, min_buffer_hours=2, exclude=frozenset({"FR"}))
    assert Filters.from_dict(f.to_dict()) == f
    assert Filters.from_dict(None) == Filters()
    assert Filters.from_dict({"max_stops": 9, "exclude": [3, "FR"]}) == Filters(
        exclude=frozenset({"FR"}))
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_filters.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'results.filters'`.

- [ ] **Step 3: Implement `results/filters.py`**

```python
"""Filters over an already-fetched result set. Zero new requests (spec §6.5).

**Unknown never passes.** When any filter is active, an itinerary that
filter cannot evaluate is hidden: an estimate or partial with no offer for a
leg, a connection whose times were not reported, an offer with no segment
list. The alternative would show a "direct only" list with flights nobody
checked. The view reports how many routes are hidden, so hidden never reads
as "none exist".
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from models import Itinerary
from providers.base import Offer

STOPS_CHOICES = (0, 1)
HOURS_CHOICES = (12, 18, 24)
BUFFER_CHOICES = (2, 3, 4)

# Callback key -> (field, allowed values). "x" toggles a carrier instead.
_SETTINGS = {
    "s": ("max_stops", STOPS_CHOICES),
    "h": ("max_hours", HOURS_CHOICES),
    "b": ("min_buffer_hours", BUFFER_CHOICES),
}


def _choice(value: object, allowed: tuple[int, ...]) -> int | None:
    # type() rather than isinstance(): True is an int, and must not pass as 1.
    return value if type(value) is int and value in allowed else None


@dataclass(frozen=True)
class Filters:
    max_stops: int | None = None
    max_hours: int | None = None
    min_buffer_hours: int | None = None
    exclude: frozenset[str] = frozenset()

    @property
    def active(self) -> int:
        set_values = (self.max_stops, self.max_hours, self.min_buffer_hours)
        return sum(v is not None for v in set_values) + (1 if self.exclude else 0)

    def to_dict(self) -> dict:
        return {
            "max_stops": self.max_stops, "max_hours": self.max_hours,
            "min_buffer_hours": self.min_buffer_hours,
            "exclude": sorted(self.exclude),
        }

    @classmethod
    def from_dict(cls, d: object) -> Filters:
        """Tolerant: anything a button could not have produced is dropped."""
        if not isinstance(d, dict):
            return cls()
        exclude = d.get("exclude") if isinstance(d.get("exclude"), list) else []
        return cls(
            max_stops=_choice(d.get("max_stops"), STOPS_CHOICES),
            max_hours=_choice(d.get("max_hours"), HOURS_CHOICES),
            min_buffer_hours=_choice(d.get("min_buffer_hours"), BUFFER_CHOICES),
            exclude=frozenset(c for c in exclude if isinstance(c, str)),
        )

    def with_setting(self, key: str, value: str) -> Filters:
        """Apply one filter button. ValueError for anything no button sends."""
        if key == "x":
            return replace(self, exclude=self.exclude ^ {value})
        if key not in _SETTINGS:
            raise ValueError(f"unknown filter key {key!r}")
        field_name, allowed = _SETTINGS[key]
        if value == "any":
            return replace(self, **{field_name: None})
        number = int(value)  # ValueError for a non-number, which is what we want
        if number not in allowed:
            raise ValueError(f"{value!r} is not an option for {key!r}")
        return replace(self, **{field_name: number})


def _required_offers(itin: Itinerary) -> list[Offer | None]:
    legs = [itin.dom_out, itin.onward_out]
    if itin.return_date:
        legs += [itin.onward_ret, itin.dom_ret]
    return legs


def _directions(itin: Itinerary) -> list[tuple[Offer | None, timedelta | None, Offer | None]]:
    directions = [(itin.dom_out, itin.buffer_out, itin.onward_out)]
    if itin.return_date:
        directions.append((itin.onward_ret, itin.buffer_ret, itin.dom_ret))
    return directions


def journey_minutes(first: Offer | None, buffer: timedelta | None,
                    second: Offer | None) -> int | None:
    """One direction, door to door: both tickets' own durations plus the gap.

    Each offer's ``duration`` already covers its own layovers. Only the gap
    between the two tickets is computed, and that is a same-airport
    subtraction (see ``models.ground_time``), so no cross-timezone
    arithmetic happens anywhere.
    """
    if first is None or second is None or buffer is None:
        return None
    return first.duration + int(buffer.total_seconds() // 60) + second.duration


def _passes(itin: Itinerary, f: Filters) -> bool:
    if not f.active:
        return True
    offers = _required_offers(itin)
    if any(o is None for o in offers):
        return False
    if f.max_stops is not None and any(o.stops > f.max_stops for o in offers):
        return False
    if f.max_hours is not None:
        for first, buffer, second in _directions(itin):
            minutes = journey_minutes(first, buffer, second)
            if minutes is None or minutes > f.max_hours * 60:
                return False
    if f.min_buffer_hours is not None:
        needed = timedelta(hours=f.min_buffer_hours)
        for _, buffer, _ in _directions(itin):
            if buffer is None or buffer < needed:
                return False
    if f.exclude:
        for o in offers:
            if not o.segments or any(s.carrier in f.exclude for s in o.segments):
                return False
    return True


def apply(itineraries: list[Itinerary],
          filters: Filters) -> tuple[list[tuple[int, Itinerary]], int]:
    """``(index, itinerary)`` for each one shown, in input order, and how many
    were hidden. The index is into *itineraries*, never a screen position."""
    shown = [(i, it) for i, it in enumerate(itineraries) if _passes(it, filters)]
    return shown, len(itineraries) - len(shown)


def carriers(itineraries: list[Itinerary]) -> list[tuple[str, str]]:
    """Every ``(code, name)`` flown by any segment of any stored itinerary."""
    seen: dict[str, str] = {}
    for it in itineraries:
        for o in it.legs:
            for s in o.segments:
                seen.setdefault(s.carrier, s.carrier_name)
    return sorted(seen.items())
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_results_filters.py -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add results/filters.py tests/test_results_filters.py
git commit -m "feat: zero-request result filters where unknown never passes"
```

---

### Task 5: The screens (`results/view.py`)

**Files:**
- Create: `results/view.py`
- Test: `tests/test_results_view.py`

**Interfaces:**
- Consumes: `StoredResults` (Task 3); `Filters`, `apply`, `carriers`, `journey_minutes`, the choice constants (Task 4); `Button`, `Rows` from `handlers.search.draft`; `STRATEGY_GRID`, `GRID_THROUGH_FARE_PHASE`, `CROSS_CHECK_PHASE` from `engine.orchestrator`; `Progress` (its `best_confirmed` field arrives in Task 6, so this task reads it with `getattr(progress, "best_confirmed", False)` and Task 6 replaces that with a plain attribute read).
- Produces:
  - `@dataclass(frozen=True) class SearchMeta` with `search_id, origin, destinations, currency, round_trip, strategy, sampled_dates, window_days, fallback_through_fare`, and `SearchMeta.from_row(row: dict) -> SearchMeta`
  - `summary(meta, stored, filters, page) -> tuple[str, Rows]`
  - `detail(meta, stored, index, page) -> tuple[str, Rows]`
  - `filters_screen(meta, stored, filters) -> tuple[str, Rows]`
  - `progress_text(progress: Progress | None, strategy: str, currency: str) -> str`
  - `phase_label(phase: str, strategy: str) -> str`
  - `savings_text(itin, currency, fallback) -> str | None`
  - `MENU_ROWS: Rows`
  - `PAGE_SIZE = 5`
  - Callback formats as in spec §1: `r:<id>:p:<n>`, `r:<id>:d:<i>`, `r:<id>:f`, `r:<id>:f:<key>:<value>`, `r:<id>:f:clear`, `r:<id>:t:<i>`, `r:<id>:T:<i>`, `r:<id>:n` (no-op).

This plan adds `r:<id>:n`, which the spec's table does not list. It is a no-op callback for the page counter and the filter section headings: Telegram buttons need callback data, and a heading must answer without doing anything.

- [ ] **Step 1: Write the failing tests**

`tests/test_results_view.py`:

```python
"""results.view: every screen as text + button specs, with no bot."""
from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from models import Progress
from results.filters import Filters
from results.store import StoredResults
from results.view import (
    PAGE_SIZE,
    SearchMeta,
    detail,
    filters_screen,
    phase_label,
    progress_text,
    savings_text,
    summary,
)
from tests.results_fixtures import (
    offer,
    one_way,
    onward_out,
    seg,
    standard_one_way,
    standard_round_trip,
)

META = SearchMeta(
    search_id=7, origin="LPA", destinations=("NRT",), currency="EUR",
    round_trip=False, strategy="two-stage", sampled_dates=1, window_days=91,
    fallback_through_fare=None,
)


def _data(rows):
    return [b.data for row in rows for b in row]


def _many(n):
    return [standard_one_way(date=f"2026-10-{d:02d}") for d in range(1, n + 1)]


# ── Summary ─────────────────────────────────────────────────────────────────

def test_summary_pages_five_at_a_time_with_navigation():
    text, rows = summary(META, StoredResults(_many(12), True), Filters(), 2)
    assert "12 routes" in text
    assert "6. " in text and "10. " in text and "11. " not in text
    data = _data(rows)
    assert "r:7:d:5" in data                     # stored index, not screen position
    assert "r:7:p:1" in data and "r:7:p:3" in data
    assert "r:7:f" in data


def test_first_page_has_no_back_target():
    _, rows = summary(META, StoredResults(_many(12), True), Filters(), 1)
    assert "r:7:p:0" not in _data(rows)


def test_page_beyond_range_is_clamped():
    """Review Focus #3: a stored page 5 after filters shrink the list to one page."""
    text, _ = summary(META, StoredResults(_many(3), True), Filters(), 5)
    assert "1. " in text


def test_status_markers_distinguish_partial_from_estimate():
    partial = standard_round_trip(dom_ret=None)
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    text, _ = summary(META, StoredResults([standard_one_way(), partial, estimate], True),
                      Filters(), 1)
    assert "partial" in text
    assert "est." in text


def test_savings_are_shown_and_a_cheaper_single_ticket_says_so():
    good = standard_one_way(through_fare="700")          # total 525
    assert "Save 175.00 EUR (25%)" in savings_text(good, "EUR", None)
    bad = standard_one_way(through_fare="500")
    line = savings_text(bad, "EUR", None)
    assert "-" not in line
    assert "single ticket is 25.00 EUR cheaper" in line


def test_savings_fall_back_to_the_row_through_fare_for_legacy_rows():
    assert savings_text(standard_one_way(), "EUR", Decimal("700")).startswith("Save")
    assert savings_text(standard_one_way(), "EUR", None) is None


def test_grid_line_says_the_window_was_sampled():
    meta = replace(META, strategy="grid", sampled_dates=12)
    text, _ = summary(meta, StoredResults(_many(1), True), Filters(), 1)
    assert "Sampled 12 of 91 days" in text


def test_hidden_line_counts_filtered_routes():
    text, rows = summary(META, StoredResults([standard_one_way(), one_way()], True),
                         Filters(max_stops=1), 1)
    assert "1 route hidden by filters" in text
    assert any(b.label.endswith("· 1") for row in rows for b in row)


def test_filters_that_hide_everything_still_offer_the_filters_button():
    """Review Focus #2."""
    text, rows = summary(META, StoredResults([standard_one_way()], True),
                         Filters(exclude=frozenset({"JL"})), 1)
    assert "No routes match these filters" in text
    assert "r:7:f" in _data(rows)


def test_undetailed_results_have_no_filters_and_say_why():
    text, rows = summary(META, StoredResults([standard_one_way()], False), Filters(), 1)
    assert "Historical snapshot" in text
    assert "r:7:f" not in _data(rows)


def test_summary_escapes_hostile_hub_codes():
    text, _ = summary(META, StoredResults([standard_one_way(hub="<b>")], True), Filters(), 1)
    assert "<b>" not in text.replace("<b>LPA", "").replace("<b>525", "")
    assert "&lt;b&gt;" in text


# ── Detail ──────────────────────────────────────────────────────────────────

def test_detail_shows_each_ticket_the_connection_and_links():
    stored = StoredResults([standard_one_way(through_fare="700")], True)
    text, rows = detail(META, stored, 0, 1)
    assert "IB100" in text and "JL100" in text
    assert "07:00" in text and "13:00" in text
    assert "3h00m between tickets" in text
    assert "25.00 EUR (100.00 EUR before 75% discount)" in text
    assert text.count('href="https://example.test/book"') == 2
    assert "checked unknown" in text        # None bags are unknown, never "none"
    assert "Save 175.00 EUR" in text
    assert _data(rows) == ["r:7:t:0", "r:7:T:0", "r:7:p:1"]


def test_detail_of_a_round_trip_has_four_tickets_and_two_connections():
    text, _ = detail(META, StoredResults([standard_round_trip()], True), 0, 1)
    for n in (1, 2, 3, 4):
        assert f"Ticket {n}" in text
    assert text.count("between tickets") == 2


def test_detail_flags_an_airport_change_and_an_impossible_connection():
    change = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00")),
        onward=offer("500", seg("TOJ", "NRT", "2026-10-01T13:00", "2026-10-02T09:00")),
    )
    text, _ = detail(META, StoredResults([change], True), 0, 1)
    assert "Airport change" in text

    impossible = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T14:00")),
        onward=offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00")),
    )
    text, _ = detail(META, StoredResults([impossible], True), 0, 1)
    assert "leaves before the first lands" in text


def test_detail_of_a_partial_names_the_missing_leg_and_links_nothing_for_it():
    text, _ = detail(META, StoredResults([one_way(dom=standard_one_way().dom_out)], True), 0, 1)
    assert "no flight chosen yet" in text
    assert text.count("href=") == 1


def test_detail_warns_about_bag_recheck_only_when_true():
    recheck = one_way(dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00",
                                            "2026-10-01T10:00"),
                                requires_bag_recheck=True),
                      onward=onward_out())
    assert "re-check" in detail(META, StoredResults([recheck], True), 0, 1)[0]
    assert "re-check" not in detail(META, StoredResults([standard_one_way()], True), 0, 1)[0]


def test_detail_of_an_undetailed_row_is_a_labelled_snapshot():
    text, _ = detail(META, StoredResults([standard_one_way()], False), 0, 1)
    assert "Historical snapshot" in text
    assert "href=" not in text


def test_detail_escapes_hostile_provider_strings():
    hostile = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00",
                             name="<script>", no="<i>1"), url='x" onclick="y'),
        onward=standard_one_way().onward_out,
    )
    text, _ = detail(META, StoredResults([hostile], True), 0, 1)
    assert "<script>" not in text and "<i>1" not in text
    assert 'onclick="y"' not in text


# ── Filters screen ──────────────────────────────────────────────────────────

def test_filters_screen_marks_the_current_choice_and_lists_carriers():
    stored = StoredResults([standard_one_way()], True)
    text, rows = filters_screen(META, stored, Filters(max_stops=0, exclude=frozenset({"JL"})))
    assert "0 of 1 routes shown" in text
    labels = {b.data: b.label for row in rows for b in row}
    assert labels["r:7:f:s:0"].startswith("•")
    assert not labels["r:7:f:s:any"].startswith("•")
    assert labels["r:7:f:x:JL"].startswith("✗")
    assert labels["r:7:f:x:IB"].startswith("✓")
    assert "r:7:f:clear" in labels and "r:7:p:1" in labels


def test_every_callback_fits_telegrams_64_bytes():
    meta = replace(META, search_id=10**9)
    stored = StoredResults(_many(12), True)
    for text_rows in (summary(meta, stored, Filters(), 2), detail(meta, stored, 11, 3),
                      filters_screen(meta, stored, Filters())):
        assert all(len(d.encode()) <= 64 for d in _data(text_rows[1]))


# ── Progress ────────────────────────────────────────────────────────────────

def test_phase_labels_depend_on_strategy_and_cross_check_reads_forward():
    assert phase_label("Phase 1", "two-stage") == "Confirming flights"
    assert phase_label("Phase 1", "grid") == "Searching domestic flights"
    assert phase_label("Phase 2", "two-stage") == "Pricing the single-ticket fare"
    assert phase_label("Phase 1 (cross-check)", "two-stage") == "Cross-checking with a second source"
    assert phase_label("Phase 9", "two-stage") == "Phase 9"


def test_progress_text_before_any_tick_and_with_a_best_price():
    assert progress_text(None, "two-stage", "EUR") == "Starting search…"
    est = Progress(phase="Phase 1", done=3, total=10, best_total=Decimal("612"))
    assert progress_text(est, "two-stage", "EUR") == (
        "Confirming flights… 3/10\nBest so far: 612 EUR (est.)")
    none_yet = Progress(phase="Phase 0", done=1, total=4)
    assert "Best so far" not in progress_text(none_yet, "two-stage", "EUR")


# ── SearchMeta ──────────────────────────────────────────────────────────────

def test_search_meta_from_a_row():
    row = {"id": 3, "origin": "LPA", "destinations": '["NRT", "KIX"]',
           "currency": "EUR", "trip_days": 14, "strategy": "grid",
           "dates": '["2026-10-01", "2026-10-05"]', "window_start": "2026-10-01",
           "window_end": "2026-12-30", "through_fare": 785.0}
    meta = SearchMeta.from_row(row)
    assert meta.destinations == ("NRT", "KIX")
    assert meta.round_trip is True
    assert meta.sampled_dates == 2
    assert meta.window_days == 91
    assert meta.fallback_through_fare == Decimal("785.0")


def test_page_size_is_five():
    assert PAGE_SIZE == 5
    assert re.search(r"5\. ", summary(META, StoredResults(_many(5), True), Filters(), 1)[0])
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_view.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'results.view'`.

- [ ] **Step 3: Implement `results/view.py`**

```python
"""Telegram screens for a search: progress, summary, detail and filters.

Pure. Every function takes stored data and view state and returns
``(text, rows)``: Telegram HTML plus button specs (``Button``, plain data).
Nothing here calls Telegram or the database. ``handlers/results.py`` does
both, so every screen is tested without a bot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from config import ORIGIN
from engine.orchestrator import CROSS_CHECK_PHASE, GRID_THROUGH_FARE_PHASE, STRATEGY_GRID
from handlers.search.draft import Button, Rows
from handlers.utils import esc, load_json_list
from models import (
    STATUS_ESTIMATE,
    STATUS_PARTIAL,
    Itinerary,
    Progress,
    fmt_dur,
    ground_time,
)
from providers.base import Offer
from results.filters import BUFFER_CHOICES, HOURS_CHOICES, Filters, apply, carriers
from results.store import StoredResults

PAGE_SIZE = 5
_CENTS = Decimal("0.01")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MARKERS = {STATUS_PARTIAL: " · partial", STATUS_ESTIMATE: " · est."}
MAX_CARRIER_BUTTONS = 16

MENU_ROWS: Rows = [[Button("🔍 New search", "menu_search"), Button("🏠 Menu", "menu_main")]]


@dataclass(frozen=True)
class SearchMeta:
    """What the screens need from the ``searches`` row besides its results."""

    search_id: int
    origin: str
    destinations: tuple[str, ...]
    currency: str
    round_trip: bool
    strategy: str | None
    sampled_dates: int
    window_days: int | None
    fallback_through_fare: Decimal | None

    @classmethod
    def from_row(cls, row: dict) -> SearchMeta:
        start, end = row.get("window_start"), row.get("window_end")
        window_days = None
        if start and end:
            span = datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")
            window_days = span.days + 1
        fare = row.get("through_fare")
        return cls(
            search_id=row["id"],
            origin=row.get("origin") or ORIGIN,
            destinations=tuple(str(d) for d in load_json_list(row.get("destinations"))),
            currency=row.get("currency") or "EUR",
            round_trip=bool(row.get("trip_days")),
            strategy=row.get("strategy"),
            sampled_dates=len(load_json_list(row.get("dates"))),
            window_days=window_days,
            fallback_through_fare=Decimal(str(fare)) if fare is not None else None,
        )


# ── Small formatters ─────────────────────────────────────────────────────────

def _money(amount: Decimal, currency: str, places: int = 0) -> str:
    return f"{amount:,.{places}f} {esc(currency)}"


def _day(date: str) -> str:
    """'2026-09-04' -> '4 Sep'. Fixed month names: never locale-dependent."""
    d = datetime.strptime(date, "%Y-%m-%d")
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _dates(itin: Itinerary) -> str:
    return f"{_day(itin.date)} → {_day(itin.return_date)}" if itin.return_date else _day(itin.date)


def _gap(delta: timedelta) -> str:
    return fmt_dur(int(delta.total_seconds() // 60))


def savings_text(itin: Itinerary, currency: str, fallback: Decimal | None) -> str | None:
    """One line comparing *itin* with the single through-ticket, or None.

    The sign guard lives here and only here. When the single ticket is
    cheaper, the line says so instead of printing a negative saving: that
    would recommend the more expensive option.
    """
    fare = itin.through_fare if itin.through_fare is not None else fallback
    if fare is None or fare <= 0:
        return None
    savings = (fare - itin.total).quantize(_CENTS)
    if savings < 0:
        return (f"The single ticket is {_money(-savings, currency, 2)} cheaper — "
                "splitting doesn't pay here.")
    if savings == 0:
        return "Same price as the single ticket — splitting gains nothing."
    pct = int(savings / fare * 100)
    return (f"Save {_money(savings, currency, 2)} ({pct}%) vs the single ticket "
            f"at {_money(fare, currency, 2)}")


# ── Summary ──────────────────────────────────────────────────────────────────

def summary(meta: SearchMeta, stored: StoredResults, filters: Filters,
            page: int) -> tuple[str, Rows]:
    active = filters if stored.detailed else Filters()
    shown, hidden = apply(stored.itineraries, active)
    shown.sort(key=lambda pair: pair[1].total)
    pages = max(1, math.ceil(len(shown) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    sid = meta.search_id

    trip = "round-trip" if meta.round_trip else "one-way"
    dests = ", ".join(esc(d) for d in meta.destinations) or "?"
    parts = [f"<b>{esc(meta.origin)} → {dests}</b> · {trip} · "
             f"{len(stored.itineraries)} routes"]
    if shown:
        best = shown[0][1]
        head = f"Best <b>{_money(best.total, meta.currency)}</b>"
        saving = savings_text(best, meta.currency, meta.fallback_through_fare)
        parts.append(f"{head}\n{saving}" if saving else head)
    if meta.strategy == STRATEGY_GRID and meta.window_days:
        parts.append(f"<i>Sampled {meta.sampled_dates} of {meta.window_days} days — "
                     "not every day was priced.</i>")
    if hidden:
        parts.append(f"<i>{hidden} route{'s' if hidden != 1 else ''} hidden by filters.</i>")
    if not stored.detailed:
        parts.append("<i>Historical snapshot — flight details were not stored "
                     "for this search.</i>")

    start = (page - 1) * PAGE_SIZE
    chunk = shown[start:start + PAGE_SIZE]
    if chunk:
        parts.append("\n".join(
            f"{start + n}. <b>{_money(it.total, meta.currency)}</b>  {_dates(it)}  "
            f"via {esc(it.hub)}{_MARKERS.get(it.status, '')}"
            for n, (_, it) in enumerate(chunk, 1)
        ))
    else:
        parts.append("No routes match these filters.")

    rows: Rows = []
    if chunk:
        rows.append([Button(str(start + n), f"r:{sid}:d:{i}")
                     for n, (i, _) in enumerate(chunk, 1)])
    if pages > 1:
        noop = f"r:{sid}:n"
        rows.append([
            Button("◀", f"r:{sid}:p:{page - 1}" if page > 1 else noop),
            Button(f"{page}/{pages}", noop),
            Button("▶", f"r:{sid}:p:{page + 1}" if page < pages else noop),
        ])
    bottom: list[Button] = []
    if stored.detailed:
        label = f"⚙️ Filters · {active.active}" if active.active else "⚙️ Filters"
        bottom.append(Button(label, f"r:{sid}:f"))
    rows.append(bottom + list(MENU_ROWS[0]))
    return "\n\n".join(parts), rows


# ── Detail ───────────────────────────────────────────────────────────────────

def _bags(o: Offer) -> str:
    cabin = ("cabin unknown" if o.included_cabin_bags is None
             else f"{o.included_cabin_bags} cabin")
    if o.included_checked_bags is None:
        checked = "checked unknown"
    elif o.included_checked_bags == 0 and o.checked_bag_price is not None:
        checked = f"checked +{_money(o.checked_bag_price, o.currency, 2)}"
    else:
        checked = f"{o.included_checked_bags} checked"
    return f"{cabin}, {checked}"


def _ticket(number: int, o: Offer | None, discount: Decimal, currency: str,
            *, domestic: bool) -> list[str]:
    if o is None:
        return [f"<b>Ticket {number}</b> · price from calendar — no flight chosen yet"]
    head = [f"<b>Ticket {number}</b>"]
    if o.segments:
        head.append(f"{esc(o.segments[0].origin)} → {esc(o.segments[-1].dest)}")
    if domestic and discount > 0:
        paid = (o.price * (Decimal(1) - discount)).quantize(_CENTS)
        head.append(f"{_money(paid, currency, 2)} ({_money(o.price, currency, 2)} "
                    f"before {int(discount * 100)}% discount)")
    else:
        head.append(_money(o.price, currency, 2))
    lines = [" · ".join(head)]

    for n, s in enumerate(o.segments):
        if n:
            layover = ground_time(o.segments[n - 1], s)
            lines.append(f"  layover {esc(s.origin)} {_gap(layover)}" if layover is not None
                         else f"  change at {esc(s.origin)}")
        when = ""
        if s.dep_local is not None and s.arr_local is not None:
            when = (f" · {_day(s.dep_local.strftime('%Y-%m-%d'))} "
                    f"{s.dep_local:%H:%M} → {s.arr_local:%H:%M}")
        lines.append(f"  {esc(s.flight_no)} {esc(s.carrier_name)}{when} · {fmt_dur(s.duration)}")

    stops = "direct" if o.stops == 0 else f"{o.stops} stop{'s' if o.stops > 1 else ''}"
    lines.append(f"  {stops} · {fmt_dur(o.duration)} · bags: {_bags(o)}")
    if o.booking_url:
        lines.append(f'  <a href="{esc(o.booking_url)}">Book this ticket</a>')
    return lines


def _connection(hub: str, arriving: Offer | None, buffer: timedelta | None,
                departing: Offer | None) -> str | None:
    if arriving is None or departing is None:
        return None
    if buffer is not None:
        if buffer < timedelta(0):
            return (f"⚠️ At {esc(hub)} the second ticket leaves before the first lands "
                    "— this connection is impossible.")
        return f"⏱ {_gap(buffer)} between tickets at {esc(hub)}"
    if arriving.segments and departing.segments \
            and arriving.segments[-1].dest != departing.segments[0].origin:
        return (f"⚠️ Airport change: arrive {esc(arriving.segments[-1].dest)}, "
                f"depart {esc(departing.segments[0].origin)}")
    return f"⏱ Time between tickets at {esc(hub)}: unknown"


def detail(meta: SearchMeta, stored: StoredResults, index: int,
           page: int) -> tuple[str, Rows]:
    """The detail for ``stored.itineraries[index]``. The caller validates *index*."""
    itin = stored.itineraries[index]
    cur = meta.currency
    trip = "round-trip" if itin.return_date else "one-way"
    parts = [
        f"<b>{_money(itin.total, cur, 2)}</b> · {trip}{_MARKERS.get(itin.status, '')}\n"
        f"{esc(meta.origin)} → {esc(itin.hub)} ({esc(itin.hub_name)}) → "
        f"{esc(itin.dest)} ({esc(itin.dest_name)})\n{_dates(itin)}",
    ]

    if not stored.detailed or itin.status == STATUS_ESTIMATE:
        parts.append(
            f"Domestic {_money(itin.dom_price, cur, 2)} → "
            f"{_money(itin.dom_discounted, cur, 2)} after discount · "
            f"onward {_money(itin.onward_price, cur, 2)}"
        )
    if not stored.detailed:
        parts.append("<i>Historical snapshot — flight details were not stored "
                     "for this search.</i>")
    else:
        directions = [("Outbound", 1, itin.dom_out, itin.buffer_out, itin.onward_out, True)]
        if itin.return_date:
            directions.append(("Return", 3, itin.onward_ret, itin.buffer_ret, itin.dom_ret, False))
        for label, first_no, first, buffer, second, first_is_domestic in directions:
            block = [f"<u>{label}</u>"]
            block += _ticket(first_no, first, itin.discount, cur, domestic=first_is_domestic)
            link = _connection(itin.hub, first, buffer, second)
            if link:
                block.append(link)
            block += _ticket(first_no + 1, second, itin.discount, cur,
                             domestic=not first_is_domestic)
            parts.append("\n".join(block))

    if itin.requires_bag_recheck is True:
        parts.append("⚠️ You must collect and re-check bags between tickets.")
    saving = savings_text(itin, cur, meta.fallback_through_fare)
    if saving:
        parts.append(saving)
    if itin.discount > 0:
        parts.append("<i>Book each ticket separately — only a separate domestic "
                     "ticket gets the discount.</i>")

    sid = meta.search_id
    rows: Rows = [
        [Button("⭐ Track this trip", f"r:{sid}:t:{index}"),
         Button("📈 Track route", f"r:{sid}:T:{index}")],
        [Button("◀ Back", f"r:{sid}:p:{page}")],
    ]
    return "\n\n".join(parts), rows


# ── Filters screen ───────────────────────────────────────────────────────────

def filters_screen(meta: SearchMeta, stored: StoredResults,
                   filters: Filters) -> tuple[str, Rows]:
    shown, _ = apply(stored.itineraries, filters)
    sid = meta.search_id
    noop = f"r:{sid}:n"
    text = (f"<b>Filters</b> — {len(shown)} of {len(stored.itineraries)} routes shown\n\n"
            "<i>A route the filters can't check (an estimate, or missing flight "
            "times) is hidden while any filter is on.</i>")

    def opt(key: str, value: int | None, current: int | None, label: str) -> Button:
        mark = "• " if current == value else ""
        return Button(f"{mark}{label}", f"r:{sid}:f:{key}:{'any' if value is None else value}")

    rows: Rows = [
        [Button("Stops per ticket", noop)],
        [opt("s", None, filters.max_stops, "Any"), opt("s", 0, filters.max_stops, "Direct"),
         opt("s", 1, filters.max_stops, "≤1 stop")],
        [Button("Max journey time (each way)", noop)],
        [opt("h", None, filters.max_hours, "Any")]
        + [opt("h", h, filters.max_hours, f"{h}h") for h in HOURS_CHOICES],
        [Button("Min time between tickets", noop)],
        [opt("b", None, filters.min_buffer_hours, "Any")]
        + [opt("b", b, filters.min_buffer_hours, f"{b}h") for b in BUFFER_CHOICES],
    ]
    codes = carriers(stored.itineraries)[:MAX_CARRIER_BUTTONS]
    if codes:
        rows.append([Button("Airlines — tap to exclude", noop)])
        buttons = [Button(f"{'✗' if code in filters.exclude else '✓'} {esc(code)}",
                          f"r:{sid}:f:x:{code}") for code, _ in codes]
        rows += [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([Button("Clear", f"r:{sid}:f:clear"), Button("◀ Results", f"r:{sid}:p:1")])
    return text, rows


# ── Progress ─────────────────────────────────────────────────────────────────

_TWO_STAGE_LABELS = {
    "Phase 0": "Scanning price calendars",
    "Phase 1": "Confirming flights",
    "Phase 2": "Pricing the single-ticket fare",
}
_GRID_LABELS = {
    "Phase 1": "Searching domestic flights",
    "Phase 1R": "Searching domestic return flights",
    "Phase 2": "Searching onward flights",
    "Phase 2R": "Searching onward return flights",
    GRID_THROUGH_FARE_PHASE: "Pricing the single-ticket fare",
}


def phase_label(phase: str, strategy: str) -> str:
    """A human step name. "Phase 1" means different work in each strategy,
    so the strategy picks the table. An unknown label passes through as-is."""
    if phase == CROSS_CHECK_PHASE:
        return "Cross-checking with a second source"
    table = _GRID_LABELS if strategy == STRATEGY_GRID else _TWO_STAGE_LABELS
    return table.get(phase, phase)


def progress_text(progress: Progress | None, strategy: str, currency: str) -> str:
    if progress is None:
        return "Starting search…"
    lines = [f"{esc(phase_label(progress.phase, strategy))}… {progress.done}/{progress.total}"]
    if progress.best_total is not None:
        confirmed = getattr(progress, "best_confirmed", False)  # Task 6 makes this a field
        lines.append(f"Best so far: {_money(progress.best_total, currency)}"
                     f"{'' if confirmed else ' (est.)'}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_results_view.py -q && .venv/bin/ruff check .`
Expected: PASS. If `test_summary_escapes_hostile_hub_codes` fails because of how it strips the legitimate `<b>` tags, fix the test's stripping, not the escaping. The point is that the hostile hub code shows up as `&lt;b&gt;`.

- [ ] **Step 5: Commit**

```bash
git add results/view.py tests/test_results_view.py
git commit -m "feat: results screens — summary, detail, filters and progress text"
```

---

### Task 6: A running best price on progress ticks

**Files:**
- Modify: `models.py` (`Progress` gains `best_confirmed`)
- Modify: `engine/orchestrator.py` (`_BestStamp`; `_PhaseRelabeler` keeps every field; stamping in `_run_two_stage`, `_run_grid` and `run_search`)
- Modify: `results/view.py` (replace the `getattr` with `progress.best_confirmed`)
- Test: `tests/test_engine_orchestrator.py` (append)

**Interfaces:**
- Produces: `Progress.best_confirmed: bool = False`. Every tick a caller of `run_search(on_progress=...)` receives carries the best known total so far: `None` before ranking, the cheapest shortlisted estimate after phase 0b, and the cheapest confirmed total once confirmed itineraries exist.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_orchestrator.py`:

```python
# ── Running best price on progress ticks (Layer 3b) ─────────────────────────


def _by_phase(ticks):
    phases: dict[str, list[Progress]] = {}
    for t in ticks:
        phases.setdefault(t.phase, []).append(t)
    return phases


async def test_best_total_is_absent_then_estimated_then_confirmed(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = _one_hub_scenario()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    ticks: list[Progress] = []

    await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider, on_progress=ticks.append,
    )

    phases = _by_phase(ticks)
    assert all(t.best_total is None for t in phases["Phase 0"])
    # Calendar prices 29 + 500: an estimate, until phase 1 confirms 25 + 480.
    assert all(t.best_total == Decimal("529") and not t.best_confirmed
               for t in phases["Phase 1"])
    assert all(t.best_total == Decimal("505") and t.best_confirmed
               for t in phases["Phase 2"])


async def test_grid_best_total_is_confirmed_once_the_legs_are_in(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("25")],
        ("MAD", "NRT", "2026-10-01"): [_offer("480")],
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("700")],
    })
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    ticks: list[Progress] = []

    await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider, on_progress=ticks.append,
    )

    phases = _by_phase(ticks)
    assert all(t.best_total is None for t in phases["Phase 1"])
    assert all(t.best_total == Decimal("505") and t.best_confirmed
               for t in phases[orchestrator.GRID_THROUGH_FARE_PHASE])


async def test_relabelled_ticks_keep_the_best_price(monkeypatch):
    """_PhaseRelabeler rebuilds a Progress when it renames a phase; it must
    not drop the stamped best on the way."""
    _neutral_discount(monkeypatch)
    ticks: list[Progress] = []
    relabel = orchestrator._PhaseRelabeler(ticks.append)
    relabel.retitle({"Phase 1": "Phase 1 (cross-check)"})

    relabel(Progress(phase="Phase 1", done=1, total=2, best_total=Decimal("9"),
                     best_confirmed=True))

    assert ticks == [Progress(phase="Phase 1 (cross-check)", done=1, total=2,
                              best_total=Decimal("9"), best_confirmed=True)]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_engine_orchestrator.py -q -k "best"`
Expected: FAIL with `TypeError: Progress.__init__() got an unexpected keyword argument 'best_confirmed'`.

- [ ] **Step 3: Implement**

In `models.py`, add a field to `Progress`, after `best_total`:

```python
    # False while best_total is only a calendar estimate (phase 0b's cheapest
    # candidate); True once it is the cheapest confirmed itinerary.
    best_confirmed: bool = False
```

In `engine/orchestrator.py`:

1. Add `import dataclasses` to the imports.
2. In `_PhaseRelabeler.__call__`, replace the `Progress(phase=shown, done=..., total=..., best_total=...)` construction with `progress = dataclasses.replace(progress, phase=shown)`.
3. Add after `_PhaseRelabeler`:

```python
class _BestStamp:
    """Stamps the running best total onto every tick a caller sees.

    Sits innermost, directly around the caller's ``on_progress``: every
    relabeller wraps it, so whatever phase name a tick ends up with, it
    reaches the caller carrying the current best. The strategies call
    ``set`` at the two points a best becomes known: after phase 0b ranks
    the calendars (an estimate) and once confirmed itineraries exist.
    """

    def __init__(self, on_progress: ProgressCallback):
        self._on_progress = on_progress
        self._best: Decimal | None = None
        self._confirmed = False

    def set(self, total: Decimal, *, confirmed: bool) -> None:
        self._best = total
        self._confirmed = confirmed

    def set_confirmed_from(self, itineraries: list[Itinerary]) -> None:
        totals = [it.total for it in itineraries if it.confirmed]
        if totals:
            self.set(min(totals), confirmed=True)

    def __call__(self, progress: Progress) -> None:
        self._on_progress(dataclasses.replace(
            progress, best_total=self._best, best_confirmed=self._confirmed,
        ))
```

4. `_run_two_stage` and `_run_grid` each gain a keyword parameter `best: _BestStamp | None = None`.
   - In `_run_two_stage`, right after `shortlist = diversify(...)`:

     ```python
         if best is not None and shortlist:
             best.set(min(c.total for c in shortlist), confirmed=False)
     ```

     and right after `itineraries = await confirm(...)`:

     ```python
         if best is not None:
             best.set_confirmed_from(itineraries)
     ```

   - In `_run_grid`, right after `itineraries = await run_grid_search(...)`:

     ```python
         if best is not None:
             best.set_confirmed_from(itineraries)
     ```

5. In `run_search`, before `secondary = _pick_secondary(provider)`:

   ```python
       best = _BestStamp(on_progress) if on_progress is not None else None
       on_progress = best if best is not None else on_progress
   ```

   and pass `best=best` to both `_run_two_stage(...)` and `_run_grid(...)`. `_cross_check` already receives `on_progress`, which is now the stamp, so cross-check ticks carry the confirmed best with no further change.

In `results/view.py`, replace `confirmed = getattr(progress, "best_confirmed", False)  # Task 6 makes this a field` with `confirmed = progress.best_confirmed`.

- [ ] **Step 4: Run the orchestrator tests and the suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS, including the existing `test_progress_phases_arrive_in_order_and_end_complete` and the label tests.

- [ ] **Step 5: Commit**

```bash
git add models.py engine/orchestrator.py results/view.py tests/test_engine_orchestrator.py
git commit -m "feat: stamp the running best price on progress ticks

Progress.best_total was declared in Layer 2 and never set. It now carries
phase 0b's cheapest estimate, then the cheapest confirmed total."
```

---

### Task 7a: The live message — progress, cancel, results in place

**Files:**
- Create: `handlers/anchor.py` (move `_markup` → `markup` and `render_anchor` out of `handlers/search/builder.py`)
- Modify: `handlers/search/builder.py` (import from `handlers.anchor`; `go()` passes its anchor and the run registry)
- Modify: `handlers/results.py` (`ProgressMessage`, new `run_and_report`, `on_cancel`, `RUNS_KEY`)
- Modify: `tests/test_results_run.py` (fake bot records edits too; results assertions read the summary)
- Modify: `tests/test_search_builder.py` (`FakeApplication.bot_data`; the `go()` tests)
- Test: `tests/test_results_handlers.py` (create)

**Interfaces:**
- Consumes: `results.view.progress_text`, `summary`, `SearchMeta`, `MENU_ROWS`; `results.store.serialize`, `load`; `results.filters.Filters`; `db.get_search_by_id`.
- Produces:
  - `handlers.anchor.markup(rows: Rows) -> InlineKeyboardMarkup`
  - `handlers.anchor.render_anchor(bot, chat_id, message_id, text, rows) -> int`, unchanged behaviour.
  - `handlers.results.RUNS_KEY = "runs"`
  - `handlers.results.run_and_report(bot, chat_id, params, *, message_id: int | None = None, runs: dict[str, CancelToken] | None = None, interval: float = PROGRESS_INTERVAL) -> None`
  - `handlers.results.PROGRESS_INTERVAL = 3.0`
  - `handlers.results.ProgressMessage`
  - `handlers.results.on_cancel(update, context)`

- [ ] **Step 1: Move `render_anchor` to `handlers/anchor.py`**

Create `handlers/anchor.py` from `builder.py`'s `_markup` (renamed `markup`) and `render_anchor`. Move the function bodies and docstrings verbatim, with this module docstring:

```python
"""One message edited in place, resent when it can't be edited.

Shared by the search builder and the results view: both keep a single live
message per flow rather than sending a new one per step.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from handlers.search.draft import Rows

logger = logging.getLogger(__name__)
```

In `builder.py`, delete both functions and add `from handlers.anchor import markup as _markup, render_anchor`. The tests import `render_anchor` from `builder`, and this keeps that name bound there.

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 2: Write the failing `run_and_report` tests**

In `tests/test_results_run.py`, replace `FakeBot` with a version that records edits and sends in one ordered list and returns message ids:

```python
class FakeBot:
    """Records every text the handler shows, edits and sends alike, in order."""

    def __init__(self, *, edit_errors=None):
        self.messages: list[str] = []
        self.markups: list = []
        self.edit_errors = list(edit_errors or [])
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, **kwargs):
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
```

In the `fake_engine` fixture, also pin the strategy lookup, so no test builds a real provider:

```python
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
```

Update the three C1 tests. The rendered dates are now `1 Sep`-style, so assert on the text with a lookbehind:

```python
    sent_text = "\n".join(bot.messages)
    assert "1 Sep" in sent_text and "20 Sep" in sent_text
    for absent in ("5 Sep", "10 Sep", "15 Sep"):
        assert not re.search(rf"(?<!\d){absent}", sent_text)
```

In `test_a_date_the_user_asked_for_is_never_dropped_by_filtering`, replace the `best_message` lines with `assert "2 routes" in bot.messages[-1]`. Add `import re`.

In the two `ValueError` / generic-exception tests, `len(bot.messages) == 1` becomes `== 2` ("Starting search…", then the failure), and the assertions read `bot.messages[-1]`.

Append the new behaviour tests:

```python
from models import CancelToken, Progress, SearchCancelled  # noqa: E402


async def test_the_search_shows_a_cancel_button_while_it_runs(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(date="2026-09-01")]
    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=_base_params(dates=["2026-09-01"]))

    assert bot.messages[0] == "Starting search…"
    first = bot.markups[0].inline_keyboard[0][0]
    assert first.callback_data.startswith("x:")


async def test_results_replace_the_progress_message_in_place(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(date="2026-09-01")]
    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=_base_params(dates=["2026-09-01"]),
                         message_id=42)

    assert bot.messages[0] == "Starting search…"
    assert "1 routes" in bot.messages[-1]
    buttons = [b.callback_data for row in bot.markups[-1].inline_keyboard for b in row]
    assert any(d.startswith("r:") and ":d:0" in d for d in buttons)


async def test_cancel_saves_nothing_and_says_so(temp_db, monkeypatch):
    async def cancelling_run_search(**kwargs):
        kwargs["cancel"].cancel()
        kwargs["cancel"].raise_if_cancelled()

    monkeypatch.setattr(results_module, "run_search", cancelling_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
    runs: dict[str, CancelToken] = {}
    bot = FakeBot()

    await run_and_report(bot, chat_id=1, params=_base_params(), runs=runs)

    assert bot.messages[-1] == "Search cancelled."
    assert await db_module.get_searches(1) == []
    assert runs == {}, "a finished run must leave the registry"


async def test_progress_ticks_reach_the_message(temp_db, monkeypatch):
    async def ticking_run_search(**kwargs):
        kwargs["on_progress"](Progress(phase="Phase 1", done=3, total=10,
                                       best_total=Decimal("612"), best_confirmed=True))
        await asyncio.sleep(0.05)
        return SimpleNamespace(itineraries=[], parse_errors=0, fetch_errors=0,
                               scan=None, strategy="two-stage")

    monkeypatch.setattr(results_module, "run_search", ticking_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
    bot = FakeBot()

    await run_and_report(bot, chat_id=1, params=_base_params(), interval=0.01)

    assert "Confirming flights… 3/10\nBest so far: 612 EUR" in bot.messages


async def test_a_failed_progress_edit_does_not_stop_the_search(temp_db, monkeypatch):
    """Review Focus #5."""
    from telegram.error import NetworkError

    async def ticking_run_search(**kwargs):
        kwargs["on_progress"](Progress(phase="Phase 0", done=1, total=4))
        await asyncio.sleep(0.05)
        return SimpleNamespace(itineraries=[_itin(date="2026-09-01")], parse_errors=0,
                               fetch_errors=0, scan=None, strategy="two-stage")

    monkeypatch.setattr(results_module, "run_search", ticking_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
    bot = FakeBot(edit_errors=[NetworkError("flaky")])

    await run_and_report(bot, chat_id=1, params=_base_params(dates=["2026-09-01"]),
                         message_id=42, interval=0.01)

    assert "1 routes" in bot.messages[-1]
```

Add `import asyncio` to the imports.

- [ ] **Step 3: Write the failing `ProgressMessage` and `on_cancel` tests**

`tests/test_results_handlers.py`:

```python
"""handlers.results: the live message's progress, cancel, and callbacks."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import handlers.start as start_module
from handlers.results import RUNS_KEY, ProgressMessage, on_cancel
from models import CancelToken, Progress

_OWNER_ID = 4242


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)


class FakeBot:
    def __init__(self):
        self.log: list[tuple[str, dict]] = []
        self._next_id = 100

    async def edit_message_text(self, **kw):
        self.log.append(("edit", kw))

    async def send_message(self, **kw):
        self.log.append(("send", kw))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    @property
    def texts(self):
        return [kw["text"] for _, kw in self.log]


class FakeQuery:
    def __init__(self, data, message_id=42):
        self.data = data
        self.message = SimpleNamespace(message_id=message_id)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


def _update(data):
    return SimpleNamespace(
        callback_query=FakeQuery(data),
        effective_user=SimpleNamespace(id=_OWNER_ID),
        effective_chat=SimpleNamespace(id=1),
    )


def _context(bot=None, runs=None):
    bot = bot or FakeBot()
    return SimpleNamespace(bot=bot, application=SimpleNamespace(
        bot=bot, bot_data={RUNS_KEY: runs if runs is not None else {}}))


def _progress_message(bot, sleep=asyncio.sleep):
    return ProgressMessage(bot, chat_id=1, message_id=42, strategy="two-stage",
                           currency="EUR", cancel_data="x:ab12", sleep=sleep)


# ── ProgressMessage ─────────────────────────────────────────────────────────

async def test_a_failed_flush_is_retried_not_raised():
    from telegram.error import NetworkError

    class FlakyBot(FakeBot):
        fail = True

        async def edit_message_text(self, **kw):
            if self.fail:
                self.fail = False
                raise NetworkError("flaky")
            await super().edit_message_text(**kw)

    bot = FlakyBot()
    view = _progress_message(bot)
    view.tick(Progress(phase="Phase 0", done=1, total=4))
    await view.flush()          # fails quietly
    await view.flush()          # same text, but never shown, so it retries
    assert bot.texts == ["Scanning price calendars… 1/4"]


async def test_flush_skips_an_unchanged_text():
    bot = FakeBot()
    view = _progress_message(bot)
    view.tick(Progress(phase="Phase 0", done=1, total=4))
    await view.flush()
    view.tick(Progress(phase="Phase 0", done=1, total=4))
    await view.flush()
    assert bot.texts == ["Scanning price calendars… 1/4"]


async def test_the_loop_waits_the_interval_before_every_edit():
    """At most one edit per interval: the loop sleeps first, every time."""
    bot = FakeBot()
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError
        view.tick(Progress(phase="Phase 0", done=len(sleeps), total=4))

    view = _progress_message(bot, sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await view._loop(3.0)

    assert sleeps == [3.0, 3.0, 3.0]
    assert len(bot.texts) == 2


# ── Cancel ──────────────────────────────────────────────────────────────────

async def test_cancel_sets_the_running_search_s_token():
    token = CancelToken()
    update = _update("x:ab12")
    await on_cancel(update, _context(runs={"ab12": token}))
    assert token.cancelled
    assert update.callback_query.answers == [("Cancelling…", False)]


async def test_cancel_after_the_search_finished_says_so():
    update = _update("x:ab12")
    await on_cancel(update, _context(runs={}))
    assert update.callback_query.answers == [("That search is no longer running.", True)]
```

- [ ] **Step 4: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_run.py tests/test_results_handlers.py -q`
Expected: FAIL. `ProgressMessage`, `RUNS_KEY` and `on_cancel` don't exist yet, and `run_and_report` takes no `message_id`.

- [ ] **Step 5: Implement in `handlers/results.py`**

Replace the imports and the whole `run_and_report` (keep `_oversized_window_message` and `_estimate_queries` untouched) with:

```python
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import FALLBACK_MAX_DATES, MAX_WINDOW_DAYS, SHORTLIST_SIZE, THROUGH_FARE_DATES
from db import get_search_by_id, save_search
from engine import run_search
from engine.orchestrator import STRATEGY_GRID, STRATEGY_TWO_STAGE
from handlers.anchor import render_anchor
from handlers.search.draft import Button, Rows
from handlers.start import owner_only_callback
from handlers.utils import esc
from models import CancelToken, Progress, SearchCancelled, SearchWindow
from providers.base import SupportsCalendar
from providers.registry import primary_provider
from results import store
from results import view
from results.filters import Filters
from search import scan_to_json

logger = logging.getLogger(__name__)

# The running searches' cancel tokens, in Application.bot_data under this key.
# Memory is the right place: a search in flight cannot survive a restart.
RUNS_KEY = "runs"

# Spec §6.6: at most one progress edit per this many seconds.
PROGRESS_INTERVAL = 3.0
```

(`_oversized_window_message` and `_estimate_queries` stay as they are, between the imports and the code below.)

```python
def _expected_strategy() -> str:
    """The strategy run_search will pick, by the same capability check."""
    if isinstance(primary_provider(), SupportsCalendar):
        return STRATEGY_TWO_STAGE
    return STRATEGY_GRID


class ProgressMessage:
    """The live progress display: one message, at most one edit per interval.

    The engine's progress callback is synchronous and fires on every leg, so
    ``tick`` only records the latest value. A background loop sleeps for the
    interval, then renders whatever is latest, and skips the edit when the
    text has not changed (Telegram rejects an identical edit). Progress is
    cosmetic: a failed edit is logged and never reaches the search.
    """

    def __init__(self, bot, chat_id: int, message_id: int | None, *, strategy: str,
                 currency: str, cancel_data: str, sleep=asyncio.sleep):
        self._bot = bot
        self._chat_id = chat_id
        self.message_id = message_id
        self._strategy = strategy
        self._currency = currency
        self._rows: Rows = [[Button("✖ Cancel", cancel_data)]]
        self._sleep = sleep
        self._latest: Progress | None = None
        self._shown: str | None = None
        self._task: asyncio.Task | None = None

    def tick(self, progress: Progress) -> None:
        self._latest = progress

    async def flush(self) -> None:
        """Render the latest tick. Never raises: a failed edit is retried by
        the next flush, since ``_shown`` only moves on success."""
        text = view.progress_text(self._latest, self._strategy, self._currency)
        if text == self._shown:
            return
        try:
            self.message_id = await render_anchor(self._bot, self._chat_id,
                                                  self.message_id, text, self._rows)
        except TelegramError as exc:
            logger.info("Progress edit failed (%s); the search continues.", exc)
            return
        self._shown = text

    async def _loop(self, interval: float) -> None:
        while True:
            await self._sleep(interval)
            await self.flush()

    def start(self, interval: float) -> None:
        self._task = asyncio.create_task(self._loop(interval))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


async def run_and_report(bot, chat_id: int, params: dict, *, message_id: int | None = None,
                         runs: dict[str, CancelToken] | None = None,
                         interval: float = PROGRESS_INTERVAL) -> None:
    """Run a search in one live message: progress, then its results.

    *message_id* is the message to take over: the builder's anchor. None
    sends a fresh one, as history reruns do. *runs* is the registry the
    Cancel button looks tokens up in.

    Shared by the builder and history reruns, so both store the same fields,
    notably ``trip_days``, without which a rerun would silently change the
    trip shape. ``params["dates"]`` is the discrete list the user asked for:
    it is forwarded to ``run_search`` and also used to filter the returned
    itineraries (review finding C1). A result on a date nobody asked for
    must never be shown or stored, and a date the user did ask for must
    never be dropped.
    """
    dates = params["dates"]
    window = SearchWindow(start=min(dates), end=max(dates))
    currency = params["currency"]

    run_id = secrets.token_hex(4)
    cancel = CancelToken()
    if runs is not None:
        runs[run_id] = cancel

    progress = ProgressMessage(bot, chat_id, message_id, strategy=_expected_strategy(),
                               currency=currency, cancel_data=f"x:{run_id}")
    await progress.flush()
    progress.start(interval)

    failure: str | None = None
    try:
        result = await run_search(
            origin=params["origin"],
            destinations=params["destinations"],
            hubs=params["hubs"],
            window=window,
            trip_days=params.get("trip_days", 0),
            adults=params["adults"],
            currency=currency,
            dates=dates,
            cancel=cancel,
            on_progress=progress.tick,
        )
    except SearchCancelled:
        failure = "Search cancelled."
    except ValueError as exc:
        # run_search's ValueError (an oversized window on a history rerun) is
        # written for a human, so it is shown verbatim (review finding I6).
        logger.warning("Search rejected for %s: %s", params, exc)
        failure = f"Search failed: {esc(exc)}"
    except Exception:
        logger.exception("Search failed for %s", params)
        failure = "Search failed — check the bot logs for details."
    finally:
        await progress.stop()
        if runs is not None:
            runs.pop(run_id, None)

    if failure is not None:
        await render_anchor(bot, chat_id, progress.message_id, failure, view.MENU_ROWS)
        return

    requested_dates = set(dates)
    itineraries = [itin for itin in result.itineraries if itin.date in requested_dates]
    had_errors = bool(result.parse_errors or result.fetch_errors)
    if had_errors:
        logger.warning(
            "Search completed with %d parse failures and %d fetch failures for %s.",
            result.parse_errors, result.fetch_errors, params,
        )

    best = min(itineraries, key=lambda it: it.total) if itineraries else None
    search_id = await save_search(
        origin=params["origin"],
        destinations=list(params["destinations"]),
        dates=dates,
        hubs=list(params["hubs"]),
        adults=params["adults"],
        currency=currency,
        trip_days=params.get("trip_days", 0),
        window_start=window.start,
        window_end=window.end,
        provider=best.providers[0] if best and best.providers else None,
        best_price=float(best.total) if best else None,
        best_route=(f"{params['origin']}->{best.hub}->{best.dest} {best.date}"
                    if best else None),
        through_fare=best.through_fare if best else None,
        results=store.serialize(itineraries) if itineraries else None,
        scan_json=json.loads(scan_to_json(result.scan)),
        strategy=result.strategy,
    )

    # Empty and broken must never look alike (review finding C2). A provider
    # that failed on every request also returns no itineraries; that gets its
    # own message, never the "No routes found" a genuinely empty search gets.
    if not itineraries:
        text = (
            "<b>Search incomplete</b> — "
            f"{result.parse_errors + result.fetch_errors} request(s) failed, "
            "so no results could be confirmed."
            if had_errors else "<b>No routes found.</b>"
        )
        await render_anchor(bot, chat_id, progress.message_id, text, view.MENU_ROWS)
        return

    row = await get_search_by_id(search_id)
    text, rows = view.summary(view.SearchMeta.from_row(row), store.load(row["results"]),
                              Filters(), 1)
    if had_errors:
        text += (
            "\n\n<i>Note: "
            f"{result.parse_errors} parse and {result.fetch_errors} fetch "
            "request(s) failed during this search — treat these results as "
            "incomplete, not a confirmed count.</i>"
        )
    await render_anchor(bot, chat_id, progress.message_id, text, rows)


@owner_only_callback
async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    token = context.application.bot_data.get(RUNS_KEY, {}).get(query.data[2:])
    if token is None:
        await query.answer("That search is no longer running.", show_alert=True)
        return
    token.cancel()
    await query.answer("Cancelling…")
```

Keep the `import json` line: `scan_to_json` is still decoded with `json.loads`.

- [ ] **Step 6: Hand the builder's anchor to the search**

In `handlers/search/builder.py`, add `from handlers.results import RUNS_KEY` beside the existing `handlers.results` import. Replace the body of `go()` from `await query.answer()` to the end with:

```python
    await query.answer()
    # The anchor becomes the search's live message: progress, then results.
    context.application.create_task(
        run_and_report(context.application.bot, update.effective_chat.id,
                       draft.to_params(), message_id=context.user_data.get(_ANCHOR),
                       runs=context.application.bot_data.setdefault(RUNS_KEY, {})),
        update=update,
    )
    context.user_data.clear()
    return ConversationHandler.END
```

Remove any imports this leaves unused (ruff will list them).

In `tests/test_search_builder.py`:
- Give `FakeApplication.__init__` the line `self.bot_data: dict = {}`.
- In `test_go_schedules_run_and_report_with_the_draft_s_params`, make the fake accept `**kwargs` and record them. Set `context.user_data[builder._ANCHOR] = 42` before calling `go`. Replace the `edits == ["On it …"]` assertion with `assert update.callback_query.edits == []`, and add `assert kwargs["message_id"] == 42` and `assert kwargs["runs"] is context.application.bot_data["runs"]`.
- Delete `test_go_still_schedules_the_search_when_the_edit_fails`: `go()` no longer edits anything, so there is no edit left to fail.

- [ ] **Step 7: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS, and ruff is clean.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: run a search in one live message with progress and Cancel

The builder's anchor becomes the progress message, throttled to one edit
per 3 seconds, with a Cancel button wired to the engine's CancelToken.
When the search ends, the same message becomes the paged summary."
```

---

### Task 7b: Results callbacks, history, and wiring

**Files:**
- Modify: `db.py` (`set_search_view`)
- Modify: `handlers/results.py` (`on_results`, `get_results_handlers`)
- Modify: `handlers/history.py` (`history_view` opens the summary; `history_rerun` passes `runs`)
- Modify: `bot.py` (register the handlers)
- Test: `tests/test_results_handlers.py` (append), `tests/test_db.py` (append)

**Interfaces:**
- Produces:
  - `db.set_search_view(search_id: int, view: dict) -> None`
  - `handlers.results.on_results(update, context)`
  - `handlers.results.get_results_handlers() -> list[CallbackQueryHandler]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
async def test_set_search_view_round_trips(temp_db):
    search_id = await db_module.save_search(
        origin="LPA", destinations=["NRT"], dates=["2026-10-01"], hubs=["MAD"],
        adults=1, currency="EUR", best_price=None, best_route=None, results=None,
    )
    await db_module.set_search_view(search_id, {"filters": {"max_stops": 0}, "page": 2})
    row = await db_module.get_search_by_id(search_id)
    assert json.loads(row["view_json"]) == {"filters": {"max_stops": 0}, "page": 2}
```

Append to `tests/test_results_handlers.py`:

```python
import json  # noqa: E402

import db as db_module  # noqa: E402
from handlers.results import on_results  # noqa: E402
from results.store import serialize  # noqa: E402
from tests.results_fixtures import standard_one_way  # noqa: E402


async def _saved(results, **kw) -> int:
    fields = dict(origin="LPA", destinations=["NRT"], dates=["2026-10-01"],
                  hubs=["MAD"], adults=1, currency="EUR", best_price=525.0,
                  best_route="x", results=results, trip_days=0,
                  window_start="2026-10-01", window_end="2026-10-01",
                  provider="kiwi", strategy="two-stage")
    fields.update(kw)
    return await db_module.save_search(**fields)


def _last(bot):
    kind, kw = bot.log[-1]
    data = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    return kw["text"], data


async def _tap(data, bot=None):
    bot = bot or FakeBot()
    update = _update(data)
    await on_results(update, _context(bot))
    return bot, update


async def test_page_detail_and_back(temp_db):
    sid = await _saved(serialize([standard_one_way(date=f"2026-10-{d:02d}")
                                  for d in range(1, 8)]))
    bot, _ = await _tap(f"r:{sid}:p:2")
    text, data = _last(bot)
    assert "6. " in text
    assert f"r:{sid}:d:6" in data

    bot, _ = await _tap(f"r:{sid}:d:6", bot)
    text, data = _last(bot)
    assert "Ticket 1" in text
    assert f"r:{sid}:p:2" in data, "Back returns to the page the user was on"


async def test_setting_a_filter_persists_and_resets_to_page_one(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    await db_module.set_search_view(sid, {"filters": {}, "page": 3})

    bot, _ = await _tap(f"r:{sid}:f:s:0")

    row = await db_module.get_search_by_id(sid)
    assert json.loads(row["view_json"]) == {
        "filters": {"max_stops": 0, "max_hours": None, "min_buffer_hours": None,
                    "exclude": []},
        "page": 1,
    }
    assert "1 of 1 routes shown" in _last(bot)[0]


async def test_clear_resets_every_filter(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    await db_module.set_search_view(sid, {"filters": {"max_stops": 0}, "page": 1})
    await _tap(f"r:{sid}:f:clear")
    row = await db_module.get_search_by_id(sid)
    assert json.loads(row["view_json"])["filters"]["max_stops"] is None


async def test_a_deleted_search_says_it_is_gone(temp_db):
    bot, update = await _tap("r:999:p:1")
    assert _last(bot)[0] == "That search is no longer stored."
    assert update.callback_query.answers


async def test_an_out_of_range_index_or_bad_filter_is_treated_as_stale(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    for data in (f"r:{sid}:d:5", f"r:{sid}:f:s:9", f"r:{sid}:zz"):
        bot, _ = await _tap(data)
        assert _last(bot)[0] == "That search is no longer stored."


async def test_a_pre_3b_search_opens_as_a_snapshot_and_filters_fall_back(temp_db):
    """Review Focus #1: a v1 row reached from an old button or from history."""
    v1 = [{"date": "2026-10-01", "hub": "MAD", "hub_name": "Madrid", "dest": "NRT",
           "dest_name": "Tokyo", "discount": 0.75, "dom_price": 100.0,
           "onward_price": 500.0, "through_fare": None}]
    sid = await _saved(v1, strategy=None)

    bot, _ = await _tap(f"r:{sid}:p:1")
    text, data = _last(bot)
    assert "Historical snapshot" in text
    assert f"r:{sid}:f" not in data

    bot, _ = await _tap(f"r:{sid}:f", bot)
    assert "Historical snapshot" in _last(bot)[0], "no filter screen for a snapshot"


async def test_noop_only_answers(temp_db):
    bot, update = await _tap("r:1:n")
    assert bot.log == []
    assert update.callback_query.answers == [("", False)]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_handlers.py tests/test_db.py -q`
Expected: FAIL, because `set_search_view` and `on_results` don't exist.

- [ ] **Step 3: Implement**

`db.py`, after `get_search_by_id`:

```python
async def set_search_view(search_id: int, view: dict) -> None:
    """Store the results view state (filters, page) for one search."""
    async with _connect() as db:
        await db.execute("UPDATE searches SET view_json = ? WHERE id = ?",
                         (_json(view), search_id))
        await db.commit()
```

`handlers/results.py`: extend the `db` import to `from db import get_search_by_id, save_search, set_search_view` (`import json` is already there), then:

```python
_GONE = "That search is no longer stored."


def _view_state(row: dict) -> dict:
    try:
        state = json.loads(row.get("view_json") or "{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


async def _show(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                rows: Rows) -> None:
    await render_anchor(context.bot, update.effective_chat.id,
                        update.callback_query.message.message_id, text, rows)


@owner_only_callback
async def on_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every r:<id>:... button. Reads the search back from the database, so
    buttons keep working across restarts (spec §1)."""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) < 3 or not parts[1].isdigit():
        await query.answer()
        await _show(update, context, _GONE, view.MENU_ROWS)
        return
    search_id, action, args = int(parts[1]), parts[2], parts[3:]

    if action == "n":
        await query.answer()
        return

    row = await get_search_by_id(search_id)
    stored = store.load(row.get("results")) if row else None
    if not row or not stored.itineraries:
        await query.answer()
        await _show(update, context, _GONE, view.MENU_ROWS)
        return

    meta = view.SearchMeta.from_row(row)
    state = _view_state(row)
    filters = Filters.from_dict(state.get("filters"))
    page = state.get("page") if isinstance(state.get("page"), int) else 1

    try:
        if action == "p":
            page = int(args[0])
            await set_search_view(search_id, {"filters": filters.to_dict(), "page": page})
            text, rows = view.summary(meta, stored, filters, page)
        elif action == "d":
            index = int(args[0])
            if not 0 <= index < len(stored.itineraries):
                raise LookupError(index)
            text, rows = view.detail(meta, stored, index, page)
        elif action == "f" and not stored.detailed:
            text, rows = view.summary(meta, stored, Filters(), 1)
        elif action == "f":
            if args == ["clear"]:
                filters = Filters()
            elif len(args) == 2:
                filters = filters.with_setting(args[0], args[1])
            elif args:
                raise LookupError(args)
            if args:
                await set_search_view(search_id, {"filters": filters.to_dict(), "page": 1})
            text, rows = view.filters_screen(meta, stored, filters)
        else:
            raise LookupError(action)
    except (LookupError, ValueError):
        await query.answer()
        await _show(update, context, _GONE, view.MENU_ROWS)
        return

    await query.answer()
    await _show(update, context, text, rows)


def get_results_handlers() -> list[CallbackQueryHandler]:
    return [
        CallbackQueryHandler(on_results, pattern=r"^r:\d+:"),
        CallbackQueryHandler(on_cancel, pattern=r"^x:[0-9a-f]+$"),
    ]
```

`handlers/history.py`:
- In `history_view`, after the empty check, replace the `format_results` / `split_message` rendering (everything from `row_through_fare = ...` to the end of the function) with:

```python
    state = results_module._view_state(row)
    text, rows = view.summary(view.SearchMeta.from_row(row), stored,
                              Filters.from_dict(state.get("filters")), 1)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup(rows),
                                  disable_web_page_preview=True)
```

  where `stored = load(row.get("results"))` replaces the `itineraries = load(...).itineraries` line from Task 3, and the empty check becomes `if not stored.itineraries:`. Use imports `import handlers.results as results_module`, `from handlers.anchor import markup`, `from results import view`, `from results.filters import Filters`. Remove `from search import format_results` and any imports this leaves unused.

- In `history_rerun`, replace `from handlers.results import run_and_report` with `from handlers.results import RUNS_KEY, run_and_report`, and pass `runs=context.application.bot_data.setdefault(RUNS_KEY, {})` to `run_and_report`. It stays `message_id=None`: a rerun gets a fresh live message and leaves the history list where it is (spec §3).

`bot.py`: import `from handlers.results import get_results_handlers`, and add a block between the favorites handlers and the main-menu fallback:

```python
    # ── 3b. Results view and Cancel ────────────────────────────────
    for handler in get_results_handlers():
        app.add_handler(handler)
```

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: page, filter and open results from any stored search"
```

---

### Task 8: Track a result from its detail

**Files:**
- Modify: `handlers/results.py` (`t` / `T` actions in `on_results`)
- Test: `tests/test_results_handlers.py` (append)

**Interfaces:**
- Consumes: `db.add_favorite(origin, hub, destination, adults, currency, price, check_dates, trip_days=0, provider=None, ...)`, `db.get_favorites()`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_track_this_trip_watches_its_exact_date(temp_db):
    sid = await _saved(serialize([standard_one_way(), standard_one_way(date="2026-10-05",
                                                                     hub="BCN")]),
                       dates=["2026-10-01", "2026-10-05"])
    bot, update = await _tap(f"r:{sid}:t:1")

    fav = (await db_module.get_favorites())[0]
    assert fav["hub"] == "BCN"
    assert json.loads(fav["check_dates"]) == ["2026-10-05"]
    assert fav["record_price"] == 525.0
    assert fav["provider"] == "kiwi"
    assert update.callback_query.answers == [("Tracking this trip.", False)]
    assert bot.log == [], "the detail stays on screen"


async def test_track_route_watches_every_searched_date(temp_db):
    sid = await _saved(serialize([standard_one_way()]),
                       dates=["2026-10-01", "2026-10-05"])
    await _tap(f"r:{sid}:T:0")
    fav = (await db_module.get_favorites())[0]
    assert json.loads(fav["check_dates"]) == ["2026-10-01", "2026-10-05"]


async def test_tracking_a_stale_index_changes_nothing(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    await _tap(f"r:{sid}:t:9")
    assert await db_module.get_favorites() == []
```

Run: `.venv/bin/pytest tests/test_results_handlers.py -q -k track`
Expected: FAIL. The action falls through to the stale message and no favourite is written.

- [ ] **Step 2: Implement**

In `handlers/results.py`, add `add_favorite` to the `db` import and `from handlers.utils import esc, load_json_list`. In `on_results`, add a branch before the final `else`:

```python
        elif action in ("t", "T"):
            index = int(args[0])
            if not 0 <= index < len(stored.itineraries):
                raise LookupError(index)
            itin = stored.itineraries[index]
            searched = [str(d) for d in load_json_list(row.get("dates"))]
            await add_favorite(
                origin=meta.origin, hub=itin.hub, destination=itin.dest,
                adults=row.get("adults") or 1, currency=meta.currency,
                price=float(itin.total),
                check_dates=[itin.date] if action == "t" else (searched or [itin.date]),
                trip_days=row.get("trip_days") or 0,
                # The provider that priced *this* itinerary, so the scheduler
                # replays the same query shape (see add_favorite's docstring).
                provider=itin.providers[0] if itin.providers else row.get("provider"),
            )
            await query.answer("Tracking this trip." if action == "t"
                               else "Tracking this route.")
            return
```

- [ ] **Step 3: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add handlers/results.py tests/test_results_handlers.py
git commit -m "feat: track any result, its exact trip or its whole route"
```

---

### Task 9: Delete the old formatter and update the README

**Files:**
- Modify: `search.py` (keep only `scan_to_json`)
- Modify: `tests/test_search.py` (keep only the `scan_to_json` tests)
- Modify: `tests/test_regressions.py` (render through `results.view`)
- Modify: `README.md`

- [ ] **Step 1: Find every remaining caller**

Run: `grep -rn "format_results\|_itinerary_block\|_savings_lines\|_booking_links\|_through_fare_for" --include='*.py' . | grep -v .venv`
Expected: hits only in `search.py`, `tests/test_search.py` and `tests/test_regressions.py`.

- [ ] **Step 2: Port the regression tests to the view**

In `tests/test_regressions.py`, replace `from search import format_results` with:

```python
from results.filters import Filters
from results.store import StoredResults
from results.view import SearchMeta, summary

_META = SearchMeta(search_id=1, origin="LPA", destinations=("NRT",), currency="EUR",
                   round_trip=True, strategy="two-stage", sampled_dates=1,
                   window_days=15, fallback_through_fare=None)
```

`test_stored_round_trip_still_renders_as_round_trip`:

```python
def test_stored_round_trip_still_renders_as_round_trip():
    restored = load(json.dumps(serialize([_round_trip_itinerary()]))).itineraries[0]
    text, _ = summary(_META, StoredResults([restored], True), Filters(), 1)
    assert "round-trip" in text
    assert "1 Sep → 15 Sep" in text
```

In `test_legacy_route_shaped_row_loads_without_crashing`, replace the final three lines with:

```python
    text, _ = summary(replace(_META, round_trip=False), StoredResults([itin], False),
                      Filters(), 1)
    assert "est." in text
    assert "Historical snapshot" in text
    assert "href=" not in text
```

and add `from dataclasses import replace`.

- [ ] **Step 3: Delete the formatter**

In `search.py`, delete `_through_fare_for`, `_savings_lines`, `_booking_links`, `_itinerary_block` and `format_results`, plus the now-unused imports (`Decimal`, `esc`, `Itinerary`, `_CENTS`). Replace the module docstring with:

```python
"""Serialization of phase 0's calendar grid for the ``searches`` table.

Result rendering moved to ``results/view.py`` and result storage to
``results/store.py`` in Layer 3b.
"""
```

In `tests/test_search.py`, delete every test that calls `format_results`, along with any helpers only those tests used. Keep the `scan_to_json` tests. Behaviour those tests pinned now lives in `tests/test_results_view.py`:

| Deleted test covered | Now covered by |
|---|---|
| no booking links on an estimate | `test_detail_of_an_undetailed_row_is_a_labelled_snapshot`, `test_detail_of_a_partial_names_the_missing_leg_and_links_nothing_for_it` |
| negative savings wording | `test_savings_are_shown_and_a_cheaper_single_ticket_says_so` |
| through-fare fallback | `test_savings_fall_back_to_the_row_through_fare_for_legacy_rows` |
| bag re-check only when True | `test_detail_warns_about_bag_recheck_only_when_true` |
| HTML escaping | `test_summary_escapes_hostile_hub_codes`, `test_detail_escapes_hostile_provider_strings` |

If a deleted test pins something that is in none of these rows, port it to `tests/test_results_view.py` before deleting it.

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 4: Update the README**

In `README.md`:

1. Under **Features**, replace the "Ranked results" bullet with:

   ```markdown
   - **Live results** — the search runs in one message: progress with a
     Cancel button, then a paged summary. Open any result for each ticket's
     flights, local times, bags, the time between tickets, and a booking
     link per ticket.
   - **Filters** — stops, total journey time, minimum time between tickets
     and excluded airlines, applied to results already fetched: zero new
     requests. A route the filters can't check is hidden and counted, never
     silently dropped.
   ```

   Also change the "Price tracking" bullet's first sentence to: "Track any result — its exact dates, or its route across the whole window — and a background scheduler re-prices it every few hours".

2. Replace the **Example output** code block and the paragraph after it with a summary and a detail in the new format. Generate both from the fixtures rather than typing them by hand:

   ```bash
   .venv/bin/python -c "
   from results.view import SearchMeta, summary, detail
   from results.store import StoredResults
   from results.filters import Filters
   from tests.results_fixtures import standard_round_trip
   m = SearchMeta(1,'LPA',('NRT',),'EUR',True,'two-stage',1,91,None)
   s = StoredResults([standard_round_trip(through_fare='1180')], True)
   print(summary(m, s, Filters(), 1)[0]); print('---'); print(detail(m, s, 0, 1)[0])"
   ```

   Strip the HTML tags from the output for the README. Keep the paragraph explaining that savings come from actually pricing the through-fare, and that the bag warning appears only when a provider confirms it.

3. In **Architecture**, replace the `search.py` line and add the new modules:

   ```
   results/
     store.py              the searches.results column: full-fidelity v2, and readers for older rows
     filters.py            zero-request filters; unknown never passes
     view.py               summary, detail, filters and progress screens -- text + buttons, no Telegram calls
   search.py             phase 0 calendar grid serialization
   ```

   Replace `search_flow.py        run_and_report: run a search, report it, persist it` with `results.py            run_and_report, the live progress message, the results callbacks`, and add `anchor.py             one message edited in place, resent when it can't be`.

- [ ] **Step 5: Final verification**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check . && git status --short`
Expected: all pass, ruff is clean, and only the files from this task are modified.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove the text-dump formatter; document the results view"
```
