# Two-Stage Search Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the grid search with a two-stage engine that prices an entire date window via calendars, then spends real requests only on a ranked shortlist — cutting a round-trip search from ~640 requests covering 10 dates to ~170 covering 91.

**Architecture:** A new `engine/` package. Phase 0 fetches one price calendar per leg pair, covering the whole window at a cost independent of its length. Phase 0b ranks every (hub, destination, date) combination arithmetically with no requests. Phase 1 confirms only the top K after a diversity filter, sharing legs through a cache. Phase 2 prices a genuine single-ticket through-fare so the saving can be stated rather than claimed. When the primary provider has no calendar, the engine falls back to today's grid search unchanged.

**Tech Stack:** Python 3.10+, `httpx` (async, via the Layer 1 providers), `Decimal` for money, `pytest` + `pytest-asyncio`, `ruff`. No new dependencies.

**Spec:** `docs/plans/2026-08-29-multi-provider-search-design.md` (§5 specifies this layer; §7 the persistence changes)
**Layer 1 carry-forward:** `docs/plans/2026-08-29-layer-1-carry-forward.md` — read this too; it records constraints that are not visible in the code.

## Global Constraints

- **Python 3.10+**, matching `requires-python = ">=3.10"`. No `match` statements, no PEP 695 generics.
- **No new runtime dependencies.**
- **Every test runs offline** except the existing `network`-marked drift guard. Use recorded fixtures and fakes; never call a real provider from a test.
- **Money is `Decimal`, never `float`.** This layer removes the last `float`/`int` money in the codebase. Round only at render time.
- **`None` means "this provider cannot tell you". It never means zero.**
- **Empty list means "no results"; an exception means "broken".** Never collapse the two.
- **Calendar figures are estimates, not bookable prices.** Anything derived from phase 0 alone must carry `confirmed=False` and every surface showing it must say so (spec §5.3).
- **`min_layover is None` does NOT mean "direct".** Key on `stops == 0`. Google returns `None` on every offer including multi-stop ones (carry-forward).
- **`search_leg` may return fewer than `limit` offers.** Over-request when a specific count is needed (carry-forward).
- **`Offer` is unhashable and only shallowly frozen.** Never put offers in a `set`; key caches on `(origin, dest, date)` (carry-forward).
- **Google raises on `children`, non-economy `cabin`, and `min_layover`.** Any code building a `LegQuery` for an arbitrary provider must either not set them or handle `ProviderError` (carry-forward).
- Line length 100; `.venv/bin/ruff check .` must pass. **Never add `# noqa: E402` in a test file** (`tests/*` already ignores E402, so it trips `RUF100`).
- Use the repo venv: `.venv/bin/pytest`, `.venv/bin/ruff`.
- `pytest` stays green at every commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `models.py` | **Modify.** Add `Itinerary`, `Candidate`, `SearchWindow`, `Progress`, `CancelToken`, `SearchCancelled`. `Route` is removed at Task 12, not before. |
| `providers/base.py` | **Modify.** `Offer` gains `requires_bag_recheck`. |
| `providers/kiwi.py` | **Modify.** Map `Layover.isBaggageRecheck`, already fetched and discarded. |
| `engine/__init__.py` | **New.** Public API: `run_search`. |
| `engine/fetch.py` | **New.** Provider-backed bounded-concurrency fetcher, with cancel + progress. |
| `engine/scan.py` | **New.** Phase 0 (calendars) and phase 0b (ranking). |
| `engine/shortlist.py` | **New.** Diversity filter and leg deduplication. |
| `engine/drill.py` | **New.** Phase 1 (confirm) and phase 2 (through-fare). |
| `engine/grid.py` | **New.** The fallback grid search, moved from `search.py`. |
| `engine/orchestrator.py` | **New.** `run_search` — strategy selection and phase sequencing. |
| `search.py` | **Modify, then shrink.** Keeps `format_results`/`routes_to_json` (presentation, rewritten in Layer 3); orchestration moves out. |
| `scheduler.py` | **Modify.** Drops its duplicate discount maths, calls the engine. |
| `db.py` | **Modify.** Migrations from spec §7.1. |
| `handlers/history.py` | **Modify.** Reconstruct `Itinerary`, including legacy rows. |
| `handlers/search_flow.py` | **Modify.** Pass a window instead of a date list. |
| `config.py` | **Modify.** Engine knobs. |

### Why an `engine/` package

`search.py` is already ~400 lines holding orchestration, JSON serialization and Telegram formatting. This layer roughly doubles the orchestration and Layer 3 rewrites the formatting. Splitting now means Layer 3 edits presentation without touching search logic. The Layer 1 carry-forward's "don't split yet" applies to `providers/kiwi.py`, whose trigger is a fourth query — not to `search.py`, which this layer rewrites regardless.

### Cost model (spec §5.8)

```
phase 0   H·(1+D), doubled for round-trip          = 64   (H=8, D=3, RT)
phase 1   unique legs among K candidates, deduped  ≈ 90   (K=30)
phase 2   3 dates · D, doubled for round-trip      = 18
                                                     ----
                                                     ~172
```

Phase 0 is independent of window length up to 91 days. Phase 1 is the only term that varies with K.

---

## Task 1: `Offer.requires_bag_recheck`

Kiwi already fetches `Layover.isBaggageRecheck` and throws it away. It answers whether a self-transfer forces re-claiming and re-checking bags at the hub — the risk the README names as this project's top limitation.

**Files:**
- Modify: `providers/base.py`, `providers/kiwi.py`, `providers/google.py`
- Test: `tests/test_providers_base.py`, `tests/test_kiwi_provider.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Offer.requires_bag_recheck: bool | None`

- [ ] **Step 1: Write the failing tests**

In `tests/test_kiwi_provider.py`:

```python
async def test_search_leg_reports_baggage_recheck_on_self_transfer(kiwi_fixture):
    """isBaggageRecheck is already in the payload; it must reach the Offer."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="MAD", dest="NRT", date="2026-10-06")
    ))[0]
    # The recorded itinerary has isBaggageRecheck True on two of its layovers.
    assert first.requires_bag_recheck is True


async def test_search_leg_reports_no_recheck_when_no_layover_needs_one(kiwi_fixture):
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    ))[0]
    # A direct flight has no connection, so there is nothing to re-check.
    assert first.requires_bag_recheck is False
```

In `tests/test_google_provider.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_kiwi_provider.py -k recheck tests/test_google_provider.py -k recheck -v`
Expected: FAIL — `AttributeError: 'Offer' object has no attribute 'requires_bag_recheck'`

- [ ] **Step 3: Add the field**

In `providers/base.py`, add to `Offer` after `pnr_count`:

```python
    requires_bag_recheck: bool | None = None
```

and extend the `Offer` docstring's Optional-fields paragraph with:

```
    requires_bag_recheck is True when at least one connection forces the
    traveller to re-claim and re-check bags. It is meaningful only when
    stops > 0, and None when the provider cannot say.
```

- [ ] **Step 4: Map it in the Kiwi provider**

In `providers/kiwi.py`'s `_to_offer`, alongside the existing layover collection, track whether any layover demands a re-check. The layover dict already carries the key because `ONEWAY_QUERY` selects it:

```python
            layover = entry.get("layover")
            if layover and layover.get("duration") is not None:
                layovers.append(_minutes(layover["duration"]))
                if layover.get("isBaggageRecheck"):
                    bag_recheck = True
```

Initialise `bag_recheck = False` next to `layovers: list[int] = []`, and pass `requires_bag_recheck=bag_recheck` in the `Offer(...)` call. A direct flight has no layovers, so it stays `False` — that is correct, not unknown: Kiwi *can* tell us, and the answer is "no connection, nothing to re-check".

Google's adapter passes nothing, so it keeps the `None` default. Add one line to `GoogleProvider`'s docstring listing `requires_bag_recheck` among the fields it cannot report.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest -k recheck -v`
Expected: PASS, 3 tests.

- [ ] **Step 6: Extend the drift guard**

In `tests/test_kiwi_schema.py`, `Layover`'s `EXPECTED` entry already lists `isBaggageRecheck`, so no change is needed — confirm it does and say so in your report rather than editing.

- [ ] **Step 7: Run the full suite and lint**

Run: `.venv/bin/pytest && .venv/bin/ruff check .`
Expected: PASS, 147 tests.

- [ ] **Step 8: Commit**

```bash
git add providers/ tests/test_providers_base.py tests/test_kiwi_provider.py tests/test_google_provider.py
git commit -m "feat: surface whether a self-transfer forces a bag re-check

Kiwi already returned Layover.isBaggageRecheck and the mapper discarded
it. It answers whether a connection forces re-claiming and re-checking
bags at the hub, which is the risk this project's README names as its
top limitation.

Google cannot report it, so its offers carry None rather than False --
'we cannot tell you' is not 'no re-check needed'."
```

---

## Task 2: Engine value objects

**Files:**
- Modify: `models.py`
- Test: `tests/test_models.py` (new)

**Interfaces:**
- Consumes: `providers.base.Offer`
- Produces: `SearchWindow`, `Candidate`, `Itinerary`, `Progress`, `CancelToken`, `SearchCancelled`

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
"""Tests for the engine's value objects."""
from __future__ import annotations

from decimal import Decimal

import pytest

from models import (
    Candidate,
    CancelToken,
    Itinerary,
    Progress,
    SearchCancelled,
    SearchWindow,
)
from providers.base import Offer, Segment


def _offer(price: str, stops: int = 0, **kw) -> Offer:
    return Offer(
        price=Decimal(price), currency="EUR", airlines=["Ryanair"], stops=stops,
        duration=170, segments=[], provider="kiwi", **kw
    )


# ── SearchWindow ────────────────────────────────────────────────────────────


def test_window_lists_every_day_inclusive():
    w = SearchWindow(start="2026-10-01", end="2026-10-05")
    assert w.dates() == [
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    ]


def test_window_of_one_day_is_one_date():
    assert SearchWindow(start="2026-10-01", end="2026-10-01").dates() == ["2026-10-01"]


def test_window_rejects_end_before_start():
    with pytest.raises(ValueError):
        SearchWindow(start="2026-10-05", end="2026-10-01").dates()


def test_window_knows_its_length():
    assert SearchWindow(start="2026-10-01", end="2026-10-31").days == 31


# ── Candidate ───────────────────────────────────────────────────────────────


def test_candidate_is_an_estimate_and_sorts_by_total():
    a = Candidate(date="2026-10-01", return_date="", hub="MAD", dest="NRT",
                  dom_price=Decimal("100"), onward_price=Decimal("500"),
                  discount=Decimal("0.75"))
    b = Candidate(date="2026-10-02", return_date="", hub="BCN", dest="NRT",
                  dom_price=Decimal("80"), onward_price=Decimal("500"),
                  discount=Decimal("0"))
    # a: 100*0.25 + 500 = 525;  b: 80 + 500 = 580
    assert a.total == Decimal("525.00")
    assert b.total == Decimal("580")
    assert sorted([b, a], key=lambda c: c.total)[0] is a


def test_candidate_applies_no_discount_when_rate_is_zero():
    c = Candidate(date="2026-10-01", return_date="", hub="LIS", dest="NRT",
                  dom_price=Decimal("100"), onward_price=Decimal("500"),
                  discount=Decimal("0"))
    assert c.total == Decimal("600")


# ── Itinerary ───────────────────────────────────────────────────────────────


def test_itinerary_totals_are_decimal_and_discount_applies_to_domestic_only():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
    )
    assert it.dom_price == Decimal("148")
    assert it.dom_discounted == Decimal("37.00")
    assert it.onward_price == Decimal("575")
    assert it.total == Decimal("612.00")
    assert it.confirmed is True


def test_itinerary_round_trip_sums_all_four_legs():
    it = Itinerary(
        date="2026-10-01", return_date="2026-10-15", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100"), dom_ret=_offer("120"),
        onward_out=_offer("500"), onward_ret=_offer("480"),
    )
    assert it.dom_price == Decimal("220")
    assert it.dom_discounted == Decimal("55.00")
    assert it.onward_price == Decimal("980")
    assert it.total == Decimal("1035.00")


def test_itinerary_from_candidate_is_unconfirmed():
    """An estimate carries no offers and must never be presented as bookable."""
    c = Candidate(date="2026-10-01", return_date="", hub="MAD", dest="NRT",
                  dom_price=Decimal("148"), onward_price=Decimal("575"),
                  discount=Decimal("0.75"))
    it = Itinerary.from_candidate(c, hub_name="Madrid", dest_name="Tokyo")
    assert it.confirmed is False
    assert it.total == Decimal("612.00")
    assert it.dom_out is None


def test_itinerary_reports_the_worst_bag_recheck_across_legs():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100", requires_bag_recheck=False),
        onward_out=_offer("500", stops=2, requires_bag_recheck=True),
    )
    assert it.requires_bag_recheck is True


def test_itinerary_bag_recheck_is_unknown_when_any_leg_cannot_say():
    """One provider saying False and another saying nothing is not False."""
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100", requires_bag_recheck=False),
        onward_out=_offer("500", stops=1, requires_bag_recheck=None),
    )
    assert it.requires_bag_recheck is None


def test_itinerary_savings_against_a_through_fare():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
        through_fare=Decimal("980"),
    )
    assert it.savings == Decimal("368.00")
    assert it.savings_pct == 37


def test_itinerary_savings_is_none_without_a_through_fare():
    """No single-ticket fare exists is not a saving of zero."""
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
    )
    assert it.savings is None
    assert it.savings_pct is None


# ── Cancellation and progress ───────────────────────────────────────────────


def test_cancel_token_starts_uncancelled_and_raises_once_cancelled():
    token = CancelToken()
    assert token.cancelled is False
    token.raise_if_cancelled()          # must not raise
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(SearchCancelled):
        token.raise_if_cancelled()


def test_cancel_is_idempotent():
    token = CancelToken()
    token.cancel()
    token.cancel()
    assert token.cancelled is True


def test_progress_reports_a_fraction():
    p = Progress(phase="Phase 1", done=3, total=12)
    assert p.fraction == 0.25


def test_progress_fraction_is_zero_when_total_is_zero():
    assert Progress(phase="Phase 0", done=0, total=0).fraction == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'Candidate' from 'models'`

- [ ] **Step 3: Write the implementation**

Add to `models.py` (keep `Route` for now — Task 12 removes it):

```python
from decimal import Decimal
from typing import Callable

from providers.base import Offer

# Money is rounded to cents only where a value is derived; never at input.
_CENTS = Decimal("0.01")


class SearchCancelled(RuntimeError):
    """Raised when a running search is cancelled by the user."""


class CancelToken:
    """A one-way flag the engine polls between requests.

    Cancellation has to be cooperative: a search is hundreds of awaits deep in
    a semaphore, and tearing that down mid-flight would leak connections. The
    engine checks this between legs instead.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise SearchCancelled("search cancelled")


@dataclass(frozen=True)
class Progress:
    """One progress tick, emitted as a phase advances."""

    phase: str
    done: int
    total: int
    best_total: Decimal | None = None

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0


ProgressCallback = Callable[[Progress], None]


@dataclass(frozen=True)
class SearchWindow:
    """An inclusive range of departure dates.

    The window replaces the old sampled date list: with a price calendar the
    cost of covering a range no longer scales with its length.
    """

    start: str                          # YYYY-MM-DD
    end: str                            # YYYY-MM-DD

    def dates(self) -> list[str]:
        first = datetime.strptime(self.start, "%Y-%m-%d")
        last = datetime.strptime(self.end, "%Y-%m-%d")
        if last < first:
            raise ValueError(f"window end {self.end} is before start {self.start}")
        span = (last - first).days
        return [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span + 1)]

    @property
    def days(self) -> int:
        return len(self.dates())


@dataclass(frozen=True)
class Candidate:
    """A (hub, destination, date) combination priced from calendars alone.

    These are cached cheapest-of-day figures. They rank the search space; they
    are never bookable, and phase 1 has to confirm one before it can be shown
    as a price the user could pay.
    """

    date: str
    return_date: str                    # "" for one-way
    hub: str
    dest: str
    dom_price: Decimal                  # undiscounted, both directions if round-trip
    onward_price: Decimal
    discount: Decimal                   # fraction taken off the domestic leg

    @property
    def dom_discounted(self) -> Decimal:
        return (self.dom_price * (Decimal(1) - self.discount)).quantize(_CENTS)

    @property
    def total(self) -> Decimal:
        return self.dom_discounted + self.onward_price


@dataclass(frozen=True)
class Itinerary:
    """A split-ticket itinerary: a discounted leg plus an onward leg.

    Composes Offers rather than flattening them, so every field Layer 1 already
    parses -- exact times, flight numbers, baggage, booking links, layovers --
    stays reachable without this type growing a copy of each.

    ``confirmed`` is the load-bearing flag. False means the prices came from
    phase 0's calendars and are estimates; the offers are None and no booking
    link exists. Every surface showing an unconfirmed itinerary must say so.
    """

    date: str
    return_date: str                    # "" for one-way
    hub: str
    hub_name: str
    dest: str
    dest_name: str
    discount: Decimal
    dom_out: Offer | None = None
    dom_ret: Offer | None = None
    onward_out: Offer | None = None
    onward_ret: Offer | None = None
    # Populated only for an unconfirmed itinerary, where there are no offers.
    est_dom_price: Decimal | None = None
    est_onward_price: Decimal | None = None
    through_fare: Decimal | None = None
    providers: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.dom_out is not None and self.onward_out is not None

    @property
    def dom_price(self) -> Decimal:
        if not self.confirmed:
            return self.est_dom_price or Decimal(0)
        total = self.dom_out.price
        if self.dom_ret is not None:
            total += self.dom_ret.price
        return total

    @property
    def onward_price(self) -> Decimal:
        if not self.confirmed:
            return self.est_onward_price or Decimal(0)
        total = self.onward_out.price
        if self.onward_ret is not None:
            total += self.onward_ret.price
        return total

    @property
    def dom_discounted(self) -> Decimal:
        return (self.dom_price * (Decimal(1) - self.discount)).quantize(_CENTS)

    @property
    def total(self) -> Decimal:
        return self.dom_discounted + self.onward_price

    @property
    def legs(self) -> tuple[Offer, ...]:
        return tuple(o for o in (self.dom_out, self.dom_ret,
                                 self.onward_out, self.onward_ret) if o is not None)

    @property
    def requires_bag_recheck(self) -> bool | None:
        """True if any leg forces a bag re-check, None if any leg cannot say.

        Unknown wins over False: one provider reporting 'no' does not license
        claiming 'no' for a leg whose provider never answered.
        """
        answers = [o.requires_bag_recheck for o in self.legs]
        if not answers:
            return None
        if any(a is True for a in answers):
            return True
        if any(a is None for a in answers):
            return None
        return False

    @property
    def savings(self) -> Decimal | None:
        """How much this beats a genuine single-ticket through-fare by."""
        if self.through_fare is None:
            return None
        return (self.through_fare - self.total).quantize(_CENTS)

    @property
    def savings_pct(self) -> int | None:
        if self.through_fare is None or self.through_fare <= 0:
            return None
        return int((self.savings / self.through_fare) * 100)

    @classmethod
    def from_candidate(cls, candidate: Candidate, hub_name: str, dest_name: str) -> Itinerary:
        """Build an unconfirmed itinerary from a calendar-derived candidate."""
        return cls(
            date=candidate.date,
            return_date=candidate.return_date,
            hub=candidate.hub,
            hub_name=hub_name,
            dest=candidate.dest,
            dest_name=dest_name,
            discount=candidate.discount,
            est_dom_price=candidate.dom_price,
            est_onward_price=candidate.onward_price,
        )
```

Add `from typing import Callable` and `from decimal import Decimal` to the imports; `dataclass`, `field`, `datetime` and `timedelta` are already there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/pytest && .venv/bin/ruff check .`
Expected: PASS, 164 tests.

- [ ] **Step 6: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "feat: add the engine's value objects

Itinerary composes Offers rather than flattening them, so the per-leg
data Layer 1 already parses stays reachable without this type growing a
copy of every field.

Its confirmed flag is load-bearing: False means the prices came from
calendars and are estimates with no booking link behind them, which
every surface has to disclose.

requires_bag_recheck resolves unknown over False across legs -- one
provider reporting 'no' does not license claiming 'no' for a leg whose
provider never answered."
```

---

## Task 3: Provider-backed leg fetcher

`search.py`'s `_LegFetcher` calls the old module-level `scraper.search`. It becomes provider-backed and gains cancellation and progress.

**Files:**
- Create: `engine/__init__.py`, `engine/fetch.py`
- Test: `tests/test_engine_fetch.py`

**Interfaces:**
- Consumes: `providers.base.{FlightProvider, LegQuery, Offer, ProviderError}`, `models.{CancelToken, Progress, ProgressCallback, SearchCancelled}`
- Produces: `engine.fetch.LegFetcher(provider, *, concurrency, delay, cancel=None, on_progress=None)` with `async fetch_many(queries: list[LegQuery], phase: str) -> dict[tuple[str, str, str], list[Offer]]`, and counters `parse_errors` / `fetch_errors`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_fetch.py`:

```python
"""Tests for the bounded-concurrency, cancellable leg fetcher."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from engine.fetch import LegFetcher
from models import CancelToken, Progress, SearchCancelled
from providers.base import LegQuery, Offer, ProviderFetchError, ProviderParseError


def _offer(price: str) -> Offer:
    return Offer(price=Decimal(price), currency="EUR", airlines=[], stops=0,
                 duration=100, segments=[], provider="fake")


class FakeProvider:
    """Records the queries it receives and replays scripted answers."""

    name = "fake"

    def __init__(self, answers=None, error=None, delay=0.0):
        self.answers = answers or {}
        self.error = error
        self.delay = delay
        self.seen: list[LegQuery] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def search_leg(self, query):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.seen.append(query)
            if self.error is not None:
                raise self.error
            return self.answers.get((query.origin, query.dest, query.date), [])
        finally:
            self.in_flight -= 1

    async def aclose(self):
        return None


def _q(origin, dest, date):
    return LegQuery(origin=origin, dest=dest, date=date)


async def test_fetches_every_query_and_keys_results_by_leg():
    provider = FakeProvider({("LPA", "MAD", "2026-10-01"): [_offer("29")]})
    fetcher = LegFetcher(provider, concurrency=4, delay=0)

    found = await fetcher.fetch_many(
        [_q("LPA", "MAD", "2026-10-01"), _q("LPA", "BCN", "2026-10-01")], phase="Phase 1"
    )

    assert set(found) == {("LPA", "MAD", "2026-10-01")}
    assert found[("LPA", "MAD", "2026-10-01")][0].price == Decimal("29")


async def test_empty_query_list_makes_no_calls():
    provider = FakeProvider()
    assert await LegFetcher(provider, concurrency=4, delay=0).fetch_many([], phase="p") == {}
    assert provider.seen == []


async def test_respects_the_concurrency_cap():
    provider = FakeProvider(delay=0.01)
    fetcher = LegFetcher(provider, concurrency=3, delay=0)
    await fetcher.fetch_many([_q("LPA", "MAD", f"2026-10-{d:02d}") for d in range(1, 13)],
                             phase="Phase 1")
    assert provider.peak_in_flight <= 3


async def test_provider_errors_are_counted_not_raised():
    """One broken leg must not abort a search of hundreds."""
    provider = FakeProvider(error=ProviderParseError("schema moved"))
    fetcher = LegFetcher(provider, concurrency=2, delay=0)

    found = await fetcher.fetch_many([_q("LPA", "MAD", "2026-10-01")], phase="Phase 1")

    assert found == {}
    assert fetcher.parse_errors == 1
    assert fetcher.fetch_errors == 0


async def test_fetch_errors_are_counted_separately():
    provider = FakeProvider(error=ProviderFetchError("timeout"))
    fetcher = LegFetcher(provider, concurrency=2, delay=0)
    await fetcher.fetch_many([_q("LPA", "MAD", "2026-10-01")], phase="Phase 1")
    assert fetcher.fetch_errors == 1
    assert fetcher.parse_errors == 0


async def test_cancellation_stops_the_run_and_raises():
    provider = FakeProvider(delay=0.01)
    token = CancelToken()
    fetcher = LegFetcher(provider, concurrency=2, delay=0, cancel=token)
    token.cancel()

    with pytest.raises(SearchCancelled):
        await fetcher.fetch_many(
            [_q("LPA", "MAD", f"2026-10-{d:02d}") for d in range(1, 21)], phase="Phase 1"
        )
    # Cancelled before any work began, so nothing was requested.
    assert provider.seen == []


async def test_progress_is_reported_and_ends_complete():
    provider = FakeProvider()
    ticks: list[Progress] = []
    fetcher = LegFetcher(provider, concurrency=2, delay=0, on_progress=ticks.append)

    await fetcher.fetch_many(
        [_q("LPA", "MAD", f"2026-10-{d:02d}") for d in range(1, 5)], phase="Phase 1"
    )

    assert ticks, "at least one progress tick"
    assert all(t.phase == "Phase 1" for t in ticks)
    assert all(t.total == 4 for t in ticks)
    assert ticks[-1].done == 4
    assert ticks[-1].fraction == 1.0


async def test_a_failing_progress_callback_does_not_break_the_search():
    """Progress is cosmetic; a broken UI callback must not lose a search."""
    def boom(_):
        raise RuntimeError("telegram is down")

    provider = FakeProvider({("LPA", "MAD", "2026-10-01"): [_offer("29")]})
    fetcher = LegFetcher(provider, concurrency=2, delay=0, on_progress=boom)

    found = await fetcher.fetch_many([_q("LPA", "MAD", "2026-10-01")], phase="Phase 1")
    assert found[("LPA", "MAD", "2026-10-01")][0].price == Decimal("29")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_engine_fetch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine'`

- [ ] **Step 3: Write the implementation**

Create `engine/__init__.py`:

```python
"""The split-ticket search engine."""
```

Create `engine/fetch.py`:

```python
"""Bounded-concurrency leg fetching, with cancellation and progress.

A search issues hundreds of requests. Running them serially took tens of
minutes; running them all at once gets a provider to block us. This caps
in-flight requests and still spaces out each worker's own requests, so
throughput scales with the cap while the request rate stays predictable.

One broken leg must not abort a search of hundreds, so provider errors are
counted and the leg is dropped. Cancellation is the one exception: it is a
user decision, and it propagates.
"""
from __future__ import annotations

import asyncio
import logging

from models import CancelToken, Progress, ProgressCallback
from providers.base import (
    FlightProvider,
    LegQuery,
    Offer,
    ProviderFetchError,
    ProviderParseError,
)

logger = logging.getLogger(__name__)

# A leg is one origin->dest query on one date.
LegKey = tuple[str, str, str]


class LegFetcher:
    """Runs many leg queries against one provider under a concurrency cap."""

    def __init__(
        self,
        provider: FlightProvider,
        *,
        concurrency: int,
        delay: float,
        cancel: CancelToken | None = None,
        on_progress: ProgressCallback | None = None,
    ):
        self._provider = provider
        self._concurrency = concurrency
        self._delay = delay
        self._cancel = cancel
        self._on_progress = on_progress
        self._semaphore = asyncio.Semaphore(concurrency)
        self.parse_errors = 0
        self.fetch_errors = 0

    def _report(self, phase: str, done: int, total: int) -> None:
        """Emit a progress tick. Never let a UI callback break a search."""
        if self._on_progress is None:
            return
        try:
            self._on_progress(Progress(phase=phase, done=done, total=total))
        except Exception:
            logger.exception("Progress callback failed; continuing search.")

    async def _one(self, query: LegQuery) -> list[Offer]:
        async with self._semaphore:
            if self._cancel is not None:
                self._cancel.raise_if_cancelled()
            try:
                return await self._provider.search_leg(query)
            except ProviderParseError as exc:
                self.parse_errors += 1
                logger.warning("Parse failed for %s->%s %s: %s",
                               query.origin, query.dest, query.date, exc)
                return []
            except ProviderFetchError as exc:
                self.fetch_errors += 1
                logger.warning("Fetch failed for %s->%s %s: %s",
                               query.origin, query.dest, query.date, exc)
                return []
            finally:
                # Hold the slot for the delay, so the rate limit is per-worker.
                if self._delay:
                    await asyncio.sleep(self._delay)

    async def fetch_many(
        self, queries: list[LegQuery], phase: str
    ) -> dict[LegKey, list[Offer]]:
        """Fetch every query concurrently, returning only legs with results."""
        if not queries:
            return {}
        if self._cancel is not None:
            self._cancel.raise_if_cancelled()

        total = len(queries)
        logger.info("%s: %d queries (concurrency %d)", phase, total, self._concurrency)
        self._report(phase, 0, total)

        found: dict[LegKey, list[Offer]] = {}
        done = 0
        tasks = [asyncio.create_task(self._one(q)) for q in queries]
        try:
            for query, task in zip(queries, tasks, strict=True):
                offers = await task
                done += 1
                if offers:
                    found[(query.origin, query.dest, query.date)] = offers
                self._report(phase, done, total)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        logger.info("%s done: %d/%d legs with results", phase, len(found), total)
        return found
```

Note the `except BaseException` block: `SearchCancelled` propagates out of a worker, and without cancelling the siblings their connections leak and pytest reports un-awaited tasks.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_engine_fetch.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/pytest && .venv/bin/ruff check .`
Expected: PASS, 172 tests.

- [ ] **Step 6: Commit**

```bash
git add engine/ tests/test_engine_fetch.py
git commit -m "feat: add the provider-backed leg fetcher

Replaces search.py's scraper-bound fetcher with one that takes any
FlightProvider, and adds the two hooks Layer 3 needs: a cancel token
polled between legs, and a progress callback.

Cancellation is cooperative because a search is hundreds of awaits deep
inside a semaphore; tearing that down mid-flight leaks connections. When
it does fire, sibling tasks are cancelled and drained rather than left
running.

A failing progress callback is swallowed -- progress is cosmetic and a
broken UI must not lose a completed search."
```

---

## Task 4: Phase 0 — calendar scan

**Files:**
- Create: `engine/scan.py`
- Test: `tests/test_engine_scan.py`

**Interfaces:**
- Consumes: `providers.base.{SupportsCalendar, CalendarQuery, RatedPrice}`, `models.{SearchWindow, CancelToken, Progress}`
- Produces: `engine.scan.scan_calendars(provider, *, origin, hubs, dests, window, trip_days, adults, currency, cancel=None, on_progress=None) -> CalendarGrid`, and the `CalendarGrid` dataclass with `.out_dom`, `.ret_dom`, `.out_onward`, `.ret_onward` mappings

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_scan.py`:

```python
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
    kw = dict(origin="LPA", hubs=["MAD", "BCN"], dests=["NRT"],
              trip_days=0, adults=1, currency="EUR")
    await scan_calendars(short, window=SearchWindow("2026-10-01", "2026-10-03"), **kw)
    await scan_calendars(long, window=SearchWindow("2026-10-01", "2026-12-30"), **kw)
    assert len(short.calls) == len(long.calls)


async def test_grid_exposes_prices_by_leg_and_date():
    provider = FakeCalendarProvider({
        ("LPA", "MAD"): {"2026-10-01": "29", "2026-10-02": "48"},
        ("MAD", "NRT"): {"2026-10-01": "575"},
    })
    grid = await scan_calendars(
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
    grid = await scan_calendars(
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_engine_scan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.scan'`

- [ ] **Step 3: Write the implementation**

Create `engine/scan.py`. It builds the four calendar sets, runs them under the same concurrency discipline as `LegFetcher`, and returns them in a `CalendarGrid`:

```python
"""Phase 0: price an entire date window from calendars.

This is the capability the whole two-stage design rests on. One request
prices a whole window, so date coverage stops scaling with request count:
a 91-day window costs exactly what a one-day window costs, and the cost is
H*(1+D), doubled for a round trip.
"""
```

Provide:

```python
@dataclass(frozen=True)
class CalendarGrid:
    """Every calendar a search needs, keyed for phase 0b's arithmetic."""

    out_dom: dict[str, dict[str, RatedPrice]]                    # hub -> date -> price
    ret_dom: dict[str, dict[str, RatedPrice]]                    # hub -> return date -> price
    out_onward: dict[tuple[str, str], dict[str, RatedPrice]]     # (hub,dest) -> date -> price
    ret_onward: dict[tuple[str, str], dict[str, RatedPrice]]     # (hub,dest) -> return date -> price
```

`scan_calendars` must:
- build `CalendarQuery` objects for `origin→hub` per hub, and `hub→dest` per hub×dest, over `window`
- for `trip_days > 0`, add `hub→origin` and `dest→hub` over the window shifted by `trip_days` (use `models.add_days` on both endpoints)
- run them with bounded concurrency (`asyncio.Semaphore`), checking `cancel.raise_if_cancelled()` before starting and inside each worker
- emit progress ticks with phase `"Phase 0"` through the same swallow-exceptions helper as `LegFetcher` — factor that helper into `engine/fetch.py` as a module-level `report(on_progress, phase, done, total)` and import it here rather than duplicating it
- let `ProviderParseError`/`ProviderFetchError` count and drop the leg, exactly as `LegFetcher` does; a hub with no calendar is simply absent from the grid

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_engine_scan.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/pytest && .venv/bin/ruff check .`
Expected: PASS, 178 tests.

- [ ] **Step 6: Commit**

```bash
git add engine/scan.py engine/fetch.py tests/test_engine_scan.py
git commit -m "feat: add phase 0, the calendar scan

One request per leg pair prices the entire window, so coverage stops
scaling with request count: 91 days costs what 3 days costs. That is the
inversion the whole two-stage design is built on.

A hub with no calendar data is absent from the grid rather than an
error -- no flights is data."
```

---

## Task 5: Phase 0b — ranking

**Files:**
- Modify: `engine/scan.py`
- Test: `tests/test_engine_scan.py`

**Interfaces:**
- Consumes: `CalendarGrid`, `models.{Candidate, SearchWindow}`, `config.{DISCOUNT_AIRPORTS, DOMESTIC_DISCOUNT}`
- Produces: `engine.scan.rank_candidates(grid, *, window, trip_days, discount_airports, discount) -> list[Candidate]`, sorted cheapest first

- [ ] **Step 1: Write the failing test**

Append to `tests/test_engine_scan.py`:

```python
from engine.scan import rank_candidates
from models import Candidate


def _grid(out_dom=None, out_onward=None, ret_dom=None, ret_onward=None):
    def rated(table):
        return {d: RatedPrice(price=Decimal(p), rating="AVERAGE") for d, p in table.items()}
    return CalendarGrid(
        out_dom={k: rated(v) for k, v in (out_dom or {}).items()},
        ret_dom={k: rated(v) for k, v in (ret_dom or {}).items()},
        out_onward={k: rated(v) for k, v in (out_onward or {}).items()},
        ret_onward={k: rated(v) for k, v in (ret_onward or {}).items()},
    )


def test_ranking_applies_the_discount_only_to_the_domestic_leg():
    grid = _grid(out_dom={"MAD": {"2026-10-01": "148"}},
                 out_onward={("MAD", "NRT"): {"2026-10-01": "575"}})
    ranked = rank_candidates(grid, window=WINDOW, trip_days=0,
                             discount_airports={"MAD"}, discount=Decimal("0.75"))
    assert len(ranked) == 1
    assert ranked[0].total == Decimal("612.00")     # 148*0.25 + 575


def test_ranking_skips_the_discount_for_a_hub_outside_the_scheme():
    grid = _grid(out_dom={"LIS": {"2026-10-01": "148"}},
                 out_onward={("LIS", "NRT"): {"2026-10-01": "575"}})
    ranked = rank_candidates(grid, window=WINDOW, trip_days=0,
                             discount_airports={"MAD"}, discount=Decimal("0.75"))
    assert ranked[0].total == Decimal("723")        # 148 + 575, no discount


def test_ranking_covers_every_day_in_the_window():
    grid = _grid(out_dom={"MAD": {"2026-10-01": "29", "2026-10-02": "48", "2026-10-03": "45"}},
                 out_onward={("MAD", "NRT"): {"2026-10-01": "575", "2026-10-02": "500",
                                              "2026-10-03": "600"}})
    ranked = rank_candidates(grid, window=WINDOW, trip_days=0,
                             discount_airports={"MAD"}, discount=Decimal("0.75"))
    assert {c.date for c in ranked} == {"2026-10-01", "2026-10-02", "2026-10-03"}


def test_ranking_is_sorted_cheapest_first():
    grid = _grid(out_dom={"MAD": {"2026-10-01": "29", "2026-10-02": "200"}},
                 out_onward={("MAD", "NRT"): {"2026-10-01": "575", "2026-10-02": "400"}})
    ranked = rank_candidates(grid, window=WINDOW, trip_days=0,
                             discount_airports={"MAD"}, discount=Decimal("0.75"))
    assert [c.total for c in ranked] == sorted(c.total for c in ranked)


def test_a_date_missing_from_either_leg_produces_no_candidate():
    """Half an itinerary is not an itinerary."""
    grid = _grid(out_dom={"MAD": {"2026-10-01": "29", "2026-10-02": "48"}},
                 out_onward={("MAD", "NRT"): {"2026-10-01": "575"}})
    ranked = rank_candidates(grid, window=WINDOW, trip_days=0,
                             discount_airports={"MAD"}, discount=Decimal("0.75"))
    assert [c.date for c in ranked] == ["2026-10-01"]


def test_round_trip_requires_all_four_legs_and_sums_them():
    grid = _grid(
        out_dom={"MAD": {"2026-10-01": "100"}},
        out_onward={("MAD", "NRT"): {"2026-10-01": "500"}},
        ret_dom={"MAD": {"2026-10-15": "120"}},
        ret_onward={("MAD", "NRT"): {"2026-10-15": "480"}},
    )
    ranked = rank_candidates(grid, window=WINDOW, trip_days=14,
                             discount_airports={"MAD"}, discount=Decimal("0.75"))
    assert len(ranked) == 1
    c = ranked[0]
    assert c.return_date == "2026-10-15"
    assert c.dom_price == Decimal("220")
    assert c.onward_price == Decimal("980")
    assert c.total == Decimal("1035.00")


def test_round_trip_drops_a_date_whose_return_leg_is_missing():
    grid = _grid(
        out_dom={"MAD": {"2026-10-01": "100"}},
        out_onward={("MAD", "NRT"): {"2026-10-01": "500"}},
        ret_dom={"MAD": {"2026-10-15": "120"}},
        ret_onward={},                                  # no return onward leg
    )
    assert rank_candidates(grid, window=WINDOW, trip_days=14,
                           discount_airports={"MAD"}, discount=Decimal("0.75")) == []


def test_empty_grid_ranks_to_nothing():
    assert rank_candidates(_grid(), window=WINDOW, trip_days=0,
                           discount_airports={"MAD"}, discount=Decimal("0.75")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_engine_scan.py -k rank -v`
Expected: FAIL — `ImportError: cannot import name 'rank_candidates'`

- [ ] **Step 3: Write the implementation**

Add `rank_candidates` to `engine/scan.py`. It iterates hub × dest × date over the window and emits a `Candidate` only when **every** leg the trip shape needs has a price on the right date. Round trips look up return legs at `add_days(date, trip_days)`. The discount rate is `discount` when `hub in discount_airports`, else `Decimal(0)`. Sort by `.total` ascending before returning.

Document in the docstring that these are estimates from cached cheapest-of-day figures, and that phase 1 confirms them.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_engine_scan.py -v`
Expected: PASS, 14 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/pytest && .venv/bin/ruff check .`
Expected: PASS, 186 tests.

- [ ] **Step 6: Commit**

```bash
git add engine/scan.py tests/test_engine_scan.py
git commit -m "feat: add phase 0b, ranking the whole window arithmetically

Ranks every hub x destination x date combination in the window with zero
requests, from the calendars phase 0 already fetched.

A candidate is emitted only when every leg its trip shape needs has a
price on the right date -- half an itinerary is not an itinerary, and a
round trip missing its return leg is not cheap, it is unbookable."
```

---

## Task 6: Diversity filter and leg deduplication

Without these, phase 1 is the entire cost of the search. They are load-bearing, not incidental.

**Files:**
- Create: `engine/shortlist.py`
- Test: `tests/test_engine_shortlist.py`

**Interfaces:**
- Consumes: `models.Candidate`
- Produces: `engine.shortlist.diversify(candidates, *, limit, max_per_hub, max_per_date) -> list[Candidate]` and `engine.shortlist.legs_for(candidates, *, origin, trip_days) -> list[LegKey]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_shortlist.py`:

```python
"""Tests for shortlist diversity and leg deduplication."""
from __future__ import annotations

from decimal import Decimal

from engine.shortlist import diversify, legs_for
from models import Candidate


def _c(date, hub, dest, total, return_date=""):
    """A candidate whose total is exactly `total` (no discount applied)."""
    return Candidate(date=date, return_date=return_date, hub=hub, dest=dest,
                     dom_price=Decimal(0), onward_price=Decimal(total),
                     discount=Decimal(0))


def test_diversify_keeps_order_and_respects_the_limit():
    cands = [_c("2026-10-01", "MAD", "NRT", 100 + i) for i in range(10)]
    # All same hub and date, so caps bite before the limit does.
    out = diversify(cands, limit=5, max_per_hub=3, max_per_date=3)
    assert len(out) == 3


def test_diversify_caps_per_hub():
    cands = [_c(f"2026-10-{d:02d}", "MAD", "NRT", 100 + d) for d in range(1, 11)]
    out = diversify(cands, limit=30, max_per_hub=4, max_per_date=99)
    assert len(out) == 4
    assert all(c.hub == "MAD" for c in out)


def test_diversify_caps_per_date():
    cands = [_c("2026-10-01", h, "NRT", 100 + i)
             for i, h in enumerate(["MAD", "BCN", "AGP", "SVQ", "VLC", "BIO"])]
    out = diversify(cands, limit=30, max_per_hub=99, max_per_date=2)
    assert len(out) == 2


def test_diversify_prefers_cheaper_candidates_within_a_cap():
    cands = [_c("2026-10-01", "MAD", "NRT", 100),
             _c("2026-10-02", "MAD", "NRT", 200),
             _c("2026-10-03", "MAD", "NRT", 300)]
    out = diversify(cands, limit=30, max_per_hub=2, max_per_date=99)
    assert [c.total for c in out] == [Decimal(100), Decimal(200)]


def test_diversify_spreads_across_hubs_rather_than_taking_one_cheap_cluster():
    """The point of the filter: not 30 variants of the same Tuesday via Madrid."""
    cheap_madrid = [_c(f"2026-10-{d:02d}", "MAD", "NRT", 100 + d) for d in range(1, 9)]
    pricier_bcn = [_c(f"2026-10-{d:02d}", "BCN", "NRT", 500 + d) for d in range(1, 9)]
    out = diversify(cheap_madrid + pricier_bcn, limit=6, max_per_hub=3, max_per_date=99)
    assert {c.hub for c in out} == {"MAD", "BCN"}


def test_diversify_on_an_empty_list():
    assert diversify([], limit=30, max_per_hub=6, max_per_date=4) == []


# ── Leg deduplication ───────────────────────────────────────────────────────


def test_legs_for_one_way_yields_two_legs_per_candidate():
    legs = legs_for([_c("2026-10-01", "MAD", "NRT", 600)], origin="LPA", trip_days=0)
    assert set(legs) == {("LPA", "MAD", "2026-10-01"), ("MAD", "NRT", "2026-10-01")}


def test_legs_for_deduplicates_the_shared_domestic_leg():
    """One LPA->MAD on the 1st serves every destination that day."""
    cands = [_c("2026-10-01", "MAD", "NRT", 600),
             _c("2026-10-01", "MAD", "JFK", 500),
             _c("2026-10-01", "MAD", "LAX", 700)]
    legs = legs_for(cands, origin="LPA", trip_days=0)
    assert legs.count(("LPA", "MAD", "2026-10-01")) == 1
    assert len(legs) == 4          # 1 domestic + 3 onward


def test_legs_for_round_trip_adds_the_mirrored_legs():
    cands = [_c("2026-10-01", "MAD", "NRT", 600, return_date="2026-10-15")]
    legs = legs_for(cands, origin="LPA", trip_days=14)
    assert set(legs) == {
        ("LPA", "MAD", "2026-10-01"), ("MAD", "NRT", "2026-10-01"),
        ("MAD", "LPA", "2026-10-15"), ("NRT", "MAD", "2026-10-15"),
    }


def test_legs_for_is_deterministic():
    cands = [_c("2026-10-02", "BCN", "JFK", 500), _c("2026-10-01", "MAD", "NRT", 600)]
    assert legs_for(cands, origin="LPA", trip_days=0) == \
           legs_for(cands, origin="LPA", trip_days=0)


def test_legs_for_no_candidates():
    assert legs_for([], origin="LPA", trip_days=0) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_engine_shortlist.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.shortlist'`

- [ ] **Step 3: Write the implementation**

Create `engine/shortlist.py`.

`diversify` walks candidates in the order given (already cheapest-first from `rank_candidates`), keeping one if neither its hub count nor its date count has hit its cap, stopping at `limit`. Because input order is by price, this naturally prefers cheaper candidates within each cap.

`legs_for` returns leg keys in deterministic order with duplicates removed — build with a `dict` used as an ordered set (never a `set`, per the carry-forward: `Offer` is unhashable, and determinism matters for test stability). For `trip_days > 0`, add `(hub, origin, return_date)` and `(dest, hub, return_date)`.

Document in the module docstring why both functions exist: without them phase 1 costs `4K` requests and returns thirty near-identical itineraries.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_engine_shortlist.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the full suite and lint**

Run: `.venv/bin/pytest && .venv/bin/ruff check .`
Expected: PASS, 197 tests.

- [ ] **Step 6: Commit**

```bash
git add engine/shortlist.py tests/test_engine_shortlist.py
git commit -m "feat: add shortlist diversity and leg deduplication

Both are load-bearing rather than incidental. Without the diversity caps
a search returns thirty variants of the same Tuesday via Madrid; without
dedup phase 1 costs 4K requests, since one LPA->MAD on the 1st serves
every destination that day.

Leg keys are collected through an ordered dict rather than a set: Offer
is unhashable, and deterministic ordering keeps the request count
reproducible."
```

---

## Task 7: Phase 1 — confirm the shortlist

**Files:**
- Create: `engine/drill.py`
- Test: `tests/test_engine_drill.py`

**Interfaces:**
- Consumes: `engine.fetch.LegFetcher`, `engine.shortlist.legs_for`, `models.{Candidate, Itinerary}`
- Produces: `engine.drill.confirm(fetcher, candidates, *, origin, trip_days, hub_names, dest_names, discount_airports, discount, adults, currency) -> list[Itinerary]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_drill.py` covering:

```python
async def test_confirm_builds_itineraries_from_real_offers():
    """A confirmed itinerary carries offers and prices from them, not the estimate."""
```
- a candidate whose legs all return offers yields `confirmed is True`, `total` computed from the offers (deliberately different from the candidate estimate, proving it re-prices rather than trusting phase 0)
- a candidate missing any leg yields **no** itinerary (unbookable, not cheap)
- the shared domestic leg is fetched **once** for three destinations on the same date (assert the fake provider's call count)
- round-trip candidates fetch and sum all four legs
- `hub_names`/`dest_names` are carried onto the itinerary
- the discount applies only when `hub in discount_airports`
- results are sorted cheapest first
- an empty candidate list makes no requests and returns `[]`
- cancellation propagates as `SearchCancelled`

Reuse the `FakeProvider` pattern from `tests/test_engine_fetch.py`; import it from there rather than redefining it, or move it to `tests/conftest.py` as a fixture factory if that reads better — say which you chose in your report.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_engine_drill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.drill'`

- [ ] **Step 3: Write the implementation**

`confirm` builds `LegQuery` objects from `legs_for(candidates, ...)`, fetches them all in **one** `fetch_many` call (so the concurrency cap applies across the whole phase and progress is a single monotonic count), then assembles an `Itinerary` per candidate from the cached results, taking each leg's cheapest offer. Candidates whose legs are not all present are dropped.

**Do not set `min_layover`, `children` or a non-economy `cabin` on the `LegQuery`** unless the caller supplied them — Google raises on all three (carry-forward), and phase 1 must work for a Google-only deployment.

- [ ] **Step 4-6: verify, full suite, commit** as in earlier tasks. Expected after this task: 206 tests.

---

## Task 8: Phase 2 — through-fare baseline

**Files:**
- Modify: `engine/drill.py`
- Test: `tests/test_engine_drill.py`

**Interfaces:**
- Produces: `engine.drill.through_fares(fetcher, itineraries, *, origin, trip_days, dates_limit=3, adults, currency) -> dict[tuple[str, str], Decimal]` keyed by `(dest, date)`

- [ ] **Step 1: Write the failing test**

Cover:
- one query per (destination, date) for the 3 cheapest **distinct** dates, no more
- **only `pnr_count == 1` offers count.** A response whose cheapest offer has `pnr_count == 3` (Kiwi selling its own self-transfer) must be skipped in favour of the cheapest single-PNR offer, and if none exists the pair is absent from the mapping
- an absent pair leaves `Itinerary.savings` as `None`, and the formatter must say "no single-ticket fare available" rather than implying a saving of zero
- `pnr_count is None` (Google, which cannot report it) is **not** treated as 1 — an unknown PNR count cannot substantiate a through-fare claim
- round-trip through-fares sum outbound and return

Use the recorded `oneway_mad_nrt_multisegment.json` shape as the model for a multi-PNR response; the real LPA→NRT through-fare came back with `pnrCount: 3`, which is exactly the case this guards against.

- [ ] **Step 2-6: fail, implement, verify, full suite, commit.** Expected after this task: 214 tests.

Commit message should record why `pnr_count == 1` is the honest baseline: comparing a split against another split is not a saving.

---

## Task 9: Grid-search fallback

**Files:**
- Create: `engine/grid.py`
- Modify: `search.py` (move `run_search`'s body out)
- Test: `tests/test_engine_grid.py`

**Interfaces:**
- Produces: `engine.grid.run_grid_search(fetcher, *, origin, dests, hubs, window, trip_days, ...) -> list[Itinerary]`

- [ ] **Step 1: Write the failing test**

Cover:
- the window is expanded to discrete dates via `generate_dates`, with `every` chosen so the sample stays at or under `FALLBACK_MAX_DATES` (default 12) — assert a 90-day window produces ≤ 12 dates and a 5-day window produces 5
- the four existing phases still run in order and produce `Itinerary` objects with `confirmed=True`
- a hub unreachable in phase 1 is never queried in phase 2 (the existing narrowing behaviour must survive the port)
- results are sorted cheapest first

Port the existing `tests/test_search.py` phase assertions rather than inventing new ones where they already cover this; note in your report which you moved and which you added.

- [ ] **Step 2-6.** Expected after this task: ~224 tests.

The commit must state that Google-only deployments keep working and are told they are sampling, not covering, the window.

---

## Task 10: Orchestrator

**Files:**
- Create: `engine/orchestrator.py`
- Modify: `engine/__init__.py`
- Test: `tests/test_engine_orchestrator.py`

**Interfaces:**
- Produces: `engine.run_search(*, origin, destinations, hubs, window, trip_days, adults, currency, provider=None, cancel=None, on_progress=None) -> SearchResult` where `SearchResult` carries `.itineraries`, `.strategy` (`"two-stage"` / `"grid"`), `.scan` (the `CalendarGrid`, or `None`), `.parse_errors`, `.fetch_errors`

- [ ] **Step 1: Write the failing test**

Cover:
- a provider implementing `SupportsCalendar` selects `"two-stage"`; one that does not selects `"grid"` — assert on `SearchResult.strategy`, since this is the switch the whole fallback rests on
- the phases run in order 0 → 0b → 1 → 2 for two-stage
- progress phases are reported in order and the final tick of each phase is complete
- cancellation between phases raises `SearchCancelled` and does not start the next phase
- error counters from every phase are aggregated onto the result
- when both providers are enabled, the top 3 confirmed itineraries carry both providers in `.providers` (spec §5.7) and the rest carry one
- an empty result (no candidates at all) returns cleanly with `itineraries == []` rather than raising

- [ ] **Step 2-6.** Expected after this task: ~236 tests.

---

## Task 11: Persistence

**Files:**
- Modify: `db.py`
- Test: `tests/test_db.py` (extend)

**Interfaces:**
- Produces: migrations for `searches` (`window_start`, `window_end`, `provider`, `through_fare`, `scan_json`) and `favorites` (`provider`, `cabin`, `children`, `max_stops`, `min_layover`), per spec §7.1

- [ ] **Step 1: Write the failing test**

Cover:
- every new column exists after `init_db()` on a fresh database
- **`init_db()` is idempotent on an existing populated database** — build one at the old schema, insert a row, migrate, assert the row survives and the new columns are present. The live `flight_finder.db` has real rows, so this is not hypothetical
- `save_search` round-trips a window and a `scan_json` blob
- `add_favorite` round-trips the new query-shape columns

- [ ] **Step 2-6.** Expected after this task: ~244 tests.

The commit must repeat the reasoning from the `trip_days` fix: the scheduler has to replay the exact query shape a price was quoted under, or it compares two different things and reports the difference as a price movement.

---

## Task 12: Wire the bot to the engine

This is the integration task. It is the one that can break the running bot, so it comes after everything it depends on is proven.

**Files:**
- Modify: `search.py`, `handlers/search_flow.py`, `handlers/history.py`, `scheduler.py`, `models.py` (remove `Route`)
- Test: `tests/test_search.py`, `tests/test_scheduler.py`, `tests/test_regressions.py`

**Interfaces:**
- Consumes: `engine.run_search`
- Produces: `search.format_results(itineraries, origin, currency, *, through_fare=None)` and `search.itineraries_to_json`

- [ ] **Step 1: Write the failing tests**

Cover:
- `format_results` labels an unconfirmed itinerary as an estimate and never shows it a booking link
- it renders the savings block from spec §5.5 when a through-fare exists, and "no single-ticket fare available" when it does not
- it warns when `requires_bag_recheck is True`, and says nothing when it is `None` (unknown is not "no")
- **legacy rows still render**: a stored result dict in the old `Route` shape (the field names in `handlers/history.py:_route_from_dict`) loads into an `Itinerary` with `confirmed=False` and no crash. Build the fixture from the two real rows in `flight_finder.db` if they are representative; otherwise hand-write one and say so
- `scheduler.check_favorites` computes its total through the engine, not its own arithmetic — assert by monkeypatching the engine and checking the scheduler does not recompute a discount itself

- [ ] **Step 2: Delete the duplicate discount maths**

`scheduler.py` currently recomputes `dom_price * (1 - discount) + intl_price` independently of the engine. Two implementations of one formula is the shape of the round-trip bug fixed in `e83a4d3`. Remove the scheduler's copy and call the engine.

- [ ] **Step 3: Remove `Route`**

Once `handlers/history.py` reconstructs `Itinerary` and `search.py` no longer references it, delete `Route` from `models.py`. Grep first; a stale importer is a runtime failure the tests may not reach.

- [ ] **Step 4-7: verify, full suite, lint, commit.** Expected after this task: ~256 tests.

- [ ] **Step 8: Manual smoke check**

Run `.venv/bin/python -c "import bot"` to prove the whole import graph still resolves, and report the output. Do **not** start the bot — it needs a real token and would begin polling.

---

## Task 13: Configuration and documentation

**Files:**
- Modify: `config.py`, `.env.example`, `README.md`
- Test: `tests/test_registry.py` (config validation)

- [ ] **Step 1: Add the engine knobs**

Following `config.py`'s existing helper style:

| Name | Default | Meaning |
|---|---|---|
| `SHORTLIST_SIZE` | `30` | K — candidates confirmed in phase 1 |
| `MAX_PER_HUB` | `6` | Diversity cap per hub |
| `MAX_PER_DATE` | `4` | Diversity cap per date |
| `THROUGH_FARE_DATES` | `3` | Distinct dates priced for the baseline |
| `FALLBACK_MAX_DATES` | `12` | Sampled dates when no calendar is available |
| `MAX_WINDOW_DAYS` | `91` | Upper bound on a search window |

`validate()` gains a check that `MAX_PER_HUB` and `MAX_PER_DATE` are each at most `SHORTLIST_SIZE` — caps larger than the shortlist silently do nothing, which reads as a broken filter.

- [ ] **Step 2: Document them** in `.env.example` with defaults and one-line explanations, matching the file's tone.

- [ ] **Step 3: Update the README**

The "How it works" section and its mermaid diagram describe the grid search. Rewrite for two-stage: the scan/rank/confirm/baseline phases, the request-count comparison from spec §5.8, and the fact that the search now covers every day in a window rather than sampling. Update the "Example output" block to include the savings lines. Keep the existing careful prose; do not assert a test count.

- [ ] **Step 4-6: verify, full suite, commit.**

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §5.1 the idea | 4, 5, 7 |
| §5.2 phase 0 scan | 4 |
| §5.3 phase 0b rank | 5 |
| §5.4 phase 1 drill-down, diversity, dedup | 6, 7 |
| §5.5 phase 2 through-fare | 8 |
| §5.6 fallback | 9 |
| §5.7 cross-check | 10 |
| §5.8 cost model | 13 (README), asserted in 4 |
| §7.1 migrations | 11 |
| §7.2 scheduler consolidation | 12 |
| §6 (Layer 3 UX) | Out of scope — the engine provides the progress/cancel hooks in Task 3 |

**Deviations from the spec, deliberate:**
1. §7.1 says the window "replaces" the sampled date list. The `dates` column is **kept**, because §5.6's fallback still needs discrete dates. Task 11's migration is additive.
2. The spec does not say what becomes of `Route`. It is replaced by `Itinerary` (Task 2) and deleted (Task 12), because flattening per-leg baggage, times, links and layovers into `Route`'s shape would mean roughly twenty more fields. Legacy stored rows still render, as unconfirmed.
3. `Offer` gains `requires_bag_recheck` (Task 1), which §4.3 does not list. Justified by the Layer 1 carry-forward: the field is already fetched and discarded, and it answers the risk the README names as the project's top limitation.

**Type consistency:** `Candidate` and `Itinerary` both expose `.total` as `Decimal`, and `Itinerary.from_candidate` is the only bridge between them. `LegKey` is `tuple[str, str, str]` in both `engine/fetch.py` and `engine/shortlist.py` — import it from `fetch` rather than redefining. `CalendarGrid`'s four mappings are keyed `hub` for domestic and `(hub, dest)` for onward, consistently in Tasks 4 and 5.

**Test-count ladder:** 144 → 147 (T1) → 164 (T2) → 172 (T3) → 178 (T4) → 186 (T5) → 197 (T6) → ~206 (T7) → ~214 (T8) → ~224 (T9) → ~236 (T10) → ~244 (T11) → ~256 (T12). Tasks 7-13 give estimates because their test lists are specified by behaviour rather than as literal code; treat a materially lower count as a signal that cases were missed, and trust `pytest` over these numbers.
