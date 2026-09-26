# Connection Risk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop pairing flights that can't connect, show every result's connection risk, and add a "night at the hub" search option.

**Architecture:**
- `engine/risk.py` classifies a self-transfer connection.
- `engine/pairing.py` picks, from offers already fetched, the cheapest pair in the best risk level. `confirm` and the grid use it instead of cheapest-per-leg.
- `SearchOptions.overnight` shifts only the domestic legs' dates (−1 day out, +1 day back), from calendar windows through candidates, legs and itineraries.
- The results view and filters read the same risk function.

**Tech Stack:** Python 3.10+, python-telegram-bot 21, aiosqlite, pytest (`asyncio_mode = "auto"`), ruff.

**Spec:** `docs/plans/2026-09-26-connection-risk-design.md`

## Global Constraints

- Python 3.10 floor. No new dependencies. The suite stays offline.
- Risk order, which is also the pairing preference: `LOW < MEDIUM < UNKNOWN < HIGH < IMPOSSIBLE`.
- Defaults: `RISK_HIGH_BELOW_HOURS = 2`, `RISK_MEDIUM_BELOW_HOURS = 4`. Valid only when `0 < high < medium`.
- An airport change is `HIGH`. A missing time is `UNKNOWN`. A negative gap is `IMPOSSIBLE`. A missing offer is not judged (`None`).
- The dates a user picks, `Itinerary.date` and `Itinerary.return_date` are always the onward (international) flight's dates. `overnight` moves only the domestic legs.
- Through-fares are priced on the onward dates.
- Every provider- or user-supplied string in Telegram HTML goes through `esc`. Every `callback_data` is ≤ 64 bytes.
- Branch `feat/connection-risk`. Commit per task. **Never push or merge to `main`.**
- `ruff check .` and `.venv/bin/pytest -q` are clean at the end of every task. E402 is off, so don't write `# noqa: E402`.

## Review Focus

1. **Every fetched pair is impossible.** Pairing still returns the cheapest impossible pair, the result is kept, and it is marked `⛔ impossible`, not dropped. (Task 2)
2. **A round trip whose outbound is safe but whose return is tight.** The risk is the return's `HIGH`, and the marker shows the return's gap, not the outbound's. (Task 5)
3. **Overnight on a round trip.** The domestic return flies the day *after* the onward return, not before. (Task 4)
4. **Stored v2 rows from before this change** (no `overnight` key) load with `overnight=False`, and old `view_json` without `hide_risky` still applies its other filters. (Tasks 3 and 5)
5. **"Hide risky" with no offers.** Estimates and partial results are hidden and counted, per 3b's unknown-never-passes rule. (Task 5)

---

## File Map

| File | Change |
|---|---|
| `config.py`, `.env.example` | two risk thresholds and their validation |
| `models.py` | `_self_transfer` → `self_transfer`; `Candidate`/`Itinerary.overnight`, `dom_date`, `dom_return_date` |
| `engine/risk.py` (new) | `Risk`, `RISK_LABELS`, `connection_risk`, `itinerary_risk` |
| `engine/pairing.py` (new) | `best_pair` |
| `engine/drill.py`, `engine/grid.py` | safe pairing; overnight dates |
| `engine/scan.py`, `engine/shortlist.py`, `engine/orchestrator.py` | overnight calendar windows, ranking and legs |
| `providers/base.py` | `SearchOptions.overnight` |
| `results/store.py` | stores `overnight` |
| `db.py` | `searches.overnight`, `favorites.overnight`; `save_search`/`add_favorite` param |
| `results/view.py`, `results/filters.py` | markers, detail risk and night lines, `hide_risky` |
| `handlers/search/options.py`, `draft.py`, `dates.py` | the toggle, the options line, the caption |
| `README.md` | Features |

---

### Task 1: Risk levels

**Files:**
- Modify: `config.py` (after `PRICE_DROP_THRESHOLD`; `validate`), `.env.example`, `models.py:38` (rename)
- Create: `engine/risk.py`
- Test: `tests/test_engine_risk.py` (new)

**Interfaces:**
- Produces:
  - `models.self_transfer(arriving, departing) -> timedelta | None`, replacing `_self_transfer`.
  - `engine.risk.Risk` (`IntEnum`: `LOW=0`, `MEDIUM=1`, `UNKNOWN=2`, `HIGH=3`, `IMPOSSIBLE=4`)
  - `RISK_LABELS: dict[Risk, str]`
  - `connection_risk(arriving: Offer | None, departing: Offer | None) -> Risk | None`
  - `itinerary_risk(itin: Itinerary) -> tuple[Risk, list[str]] | None`
  - `config.RISK_HIGH_BELOW_HOURS`, `config.RISK_MEDIUM_BELOW_HOURS` (floats)

- [ ] **Step 1: Write the failing tests**

`tests/test_engine_risk.py`:

```python
"""engine.risk: how dangerous a self-transfer between two tickets is."""
from __future__ import annotations

import pytest

import config
from engine.risk import RISK_LABELS, Risk, connection_risk, itinerary_risk
from tests.results_fixtures import offer, one_way, seg, standard_round_trip


def _pair(arrive: str, depart: str, *, via_out="MAD", via_in="MAD"):
    first = offer("100", seg("LPA", via_out, "2026-10-01T07:00", arrive))
    second = offer("500", seg(via_in, "NRT", depart, "2026-10-02T09:00"))
    return first, second


@pytest.mark.parametrize(("arrive", "depart", "risk"), [
    ("2026-10-01T10:00", "2026-10-01T09:00", Risk.IMPOSSIBLE),
    ("2026-10-01T10:00", "2026-10-01T10:00", Risk.HIGH),       # 0h
    ("2026-10-01T10:00", "2026-10-01T11:59", Risk.HIGH),
    ("2026-10-01T10:00", "2026-10-01T12:00", Risk.MEDIUM),     # exactly 2h
    ("2026-10-01T10:00", "2026-10-01T13:59", Risk.MEDIUM),
    ("2026-10-01T10:00", "2026-10-01T14:00", Risk.LOW),        # exactly 4h
    ("2026-09-30T20:00", "2026-10-01T13:00", Risk.LOW),        # a night at the hub
])
def test_levels_and_their_boundaries(arrive, depart, risk):
    assert connection_risk(*_pair(arrive, depart)) is risk


def test_an_airport_change_is_high_even_with_hours_to_spare():
    first, second = _pair("2026-10-01T10:00", "2026-10-01T18:00", via_in="TOJ")
    assert connection_risk(first, second) is Risk.HIGH


def test_a_missing_time_is_unknown_not_high():
    first = offer("100", seg("LPA", "MAD", "2026-10-01T07:00", None))
    second = offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00"))
    assert connection_risk(first, second) is Risk.UNKNOWN
    assert connection_risk(offer("100"), second) is Risk.UNKNOWN     # no segments at all


def test_a_missing_offer_is_not_judged():
    assert connection_risk(None, offer("500")) is None


def test_thresholds_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "RISK_HIGH_BELOW_HOURS", 3.0)
    monkeypatch.setattr(config, "RISK_MEDIUM_BELOW_HOURS", 6.0)
    assert connection_risk(*_pair("2026-10-01T10:00", "2026-10-01T12:30")) is Risk.HIGH
    assert connection_risk(*_pair("2026-10-01T10:00", "2026-10-01T15:00")) is Risk.MEDIUM


def test_a_round_trip_takes_the_worse_direction_with_reasons():
    rt = standard_round_trip(
        dom_ret=offer("90", seg("MAD", "LPA", "2026-10-15T19:00", "2026-10-15T20:45")),
    )  # return: lands 18:00, domestic leaves 19:00 -> 1h
    risk, reasons = itinerary_risk(rt)
    assert risk is Risk.HIGH
    assert reasons == ["3h00m between tickets at MAD", "1h00m between tickets at MAD"]


def test_reasons_name_airport_changes_and_impossible_connections():
    first, second = _pair("2026-10-01T10:00", "2026-10-01T18:00", via_in="TOJ")
    change = one_way(dom=first, onward=second)
    assert itinerary_risk(change) == (Risk.HIGH, ["arrive MAD, depart TOJ"])
    first, second = _pair("2026-10-01T14:00", "2026-10-01T13:00")
    impossible = one_way(dom=first, onward=second)
    assert itinerary_risk(impossible) == (
        Risk.IMPOSSIBLE, ["the second ticket leaves before the first lands at MAD"])


def test_an_estimate_is_not_judged():
    assert itinerary_risk(one_way()) is None


def test_every_level_has_a_label():
    assert set(RISK_LABELS) == set(Risk)


def test_invalid_thresholds_are_a_config_error(monkeypatch):
    monkeypatch.setattr(config, "RISK_HIGH_BELOW_HOURS", 5.0)
    monkeypatch.setattr(config, "RISK_MEDIUM_BELOW_HOURS", 4.0)
    with pytest.raises(config.ConfigError, match="RISK_HIGH_BELOW_HOURS"):
        config.validate()
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_engine_risk.py -q`
Expected: collection error, `No module named 'engine.risk'`.

- [ ] **Step 3: Implement**

`config.py`, after `PRICE_DROP_THRESHOLD`:

```python
# Self-transfer risk (engine/risk.py): below HIGH hours between tickets a
# connection is high risk, below MEDIUM it is medium, otherwise low.
RISK_HIGH_BELOW_HOURS = _float_env("RISK_HIGH_BELOW_HOURS", 2.0, lo=0.0, hi=48.0)
RISK_MEDIUM_BELOW_HOURS = _float_env("RISK_MEDIUM_BELOW_HOURS", 4.0, lo=0.0, hi=72.0)
```

In `validate()`, before `if problems:`:

```python
    if not 0 < RISK_HIGH_BELOW_HOURS < RISK_MEDIUM_BELOW_HOURS:
        problems.append(
            f"RISK_HIGH_BELOW_HOURS ({RISK_HIGH_BELOW_HOURS}) must be above 0 and below "
            f"RISK_MEDIUM_BELOW_HOURS ({RISK_MEDIUM_BELOW_HOURS})"
        )
```

`validate` reads module globals, so the monkeypatched values in the test take effect. If `validate` refers to them through a local import, read them as `globals()["RISK_HIGH_BELOW_HOURS"]` instead.

`.env.example`, after `PRICE_DROP_THRESHOLD=0.10`:

```
# Connection risk between the two tickets, in hours: under HIGH is high risk,
# under MEDIUM is medium, otherwise low.
RISK_HIGH_BELOW_HOURS=2
RISK_MEDIUM_BELOW_HOURS=4
```

`models.py`: rename `_self_transfer` to `self_transfer`, in its definition and in the two property bodies.

`engine/risk.py`:

```python
"""How dangerous a self-transfer between two separately booked tickets is.

One function judges a connection, and both the engine (to pick which flights
to pair) and the results view (to label them) call it, so the two can never
disagree. Thresholds are read from config at call time.
"""
from __future__ import annotations

from datetime import timedelta
from enum import IntEnum

import config
from models import Itinerary, fmt_dur, ground_time
from providers.base import Offer


class Risk(IntEnum):
    """Lower is better. The order is also the pairing preference."""

    LOW = 0
    MEDIUM = 1
    UNKNOWN = 2
    HIGH = 3
    IMPOSSIBLE = 4


RISK_LABELS = {
    Risk.LOW: "Low", Risk.MEDIUM: "Medium", Risk.UNKNOWN: "Unknown",
    Risk.HIGH: "High", Risk.IMPOSSIBLE: "Impossible",
}


def connection_risk(arriving: Offer | None, departing: Offer | None) -> Risk | None:
    """The risk of catching *departing* after *arriving*, or None if either is missing."""
    if arriving is None or departing is None:
        return None
    if not arriving.segments or not departing.segments:
        return Risk.UNKNOWN
    landed, leaving = arriving.segments[-1], departing.segments[0]
    if landed.dest != leaving.origin:
        return Risk.HIGH                   # airport change: a transfer across town
    gap = ground_time(landed, leaving)
    if gap is None:
        return Risk.UNKNOWN
    if gap < timedelta(0):
        return Risk.IMPOSSIBLE
    if gap < timedelta(hours=config.RISK_HIGH_BELOW_HOURS):
        return Risk.HIGH
    if gap < timedelta(hours=config.RISK_MEDIUM_BELOW_HOURS):
        return Risk.MEDIUM
    return Risk.LOW


def _reason(risk: Risk, arriving: Offer, departing: Offer, hub: str) -> str:
    if risk is Risk.UNKNOWN:
        return f"times not reported at {hub}"
    landed, leaving = arriving.segments[-1], departing.segments[0]
    if landed.dest != leaving.origin:
        return f"arrive {landed.dest}, depart {leaving.origin}"
    if risk is Risk.IMPOSSIBLE:
        return f"the second ticket leaves before the first lands at {hub}"
    gap = ground_time(landed, leaving)
    return f"{fmt_dur(int(gap.total_seconds() // 60))} between tickets at {hub}"


def itinerary_risk(itin: Itinerary) -> tuple[Risk, list[str]] | None:
    """The worse of the outbound and return connections, with one reason per
    connection judged. None when neither connection could be judged."""
    connections = [(itin.dom_out, itin.onward_out)]
    if itin.return_date:
        connections.append((itin.onward_ret, itin.dom_ret))
    worst: Risk | None = None
    reasons: list[str] = []
    for arriving, departing in connections:
        risk = connection_risk(arriving, departing)
        if risk is None:
            continue
        reasons.append(_reason(risk, arriving, departing, itin.hub))
        worst = risk if worst is None else max(worst, risk)
    return None if worst is None else (worst, reasons)
```

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: classify the risk of each self-transfer connection"
```

---

### Task 2: Pair flights safe-first

**Files:**
- Create: `engine/pairing.py`
- Modify: `engine/drill.py` (`confirm`), `engine/grid.py` (combine)
- Test: `tests/test_engine_pairing.py` (new), `tests/test_engine_drill.py`, `tests/test_engine_grid.py`

**Interfaces:**
- Consumes: `connection_risk`, `Risk` (Task 1).
- Produces: `engine.pairing.best_pair(firsts, seconds, *, first_discount=Decimal(0), second_discount=Decimal(0)) -> tuple[Offer, Offer]`, where both lists are non-empty.

- [ ] **Step 1: Write the failing tests**

`tests/test_engine_pairing.py`:

```python
"""engine.pairing: the cheapest pair within the best risk level."""
from __future__ import annotations

from decimal import Decimal

from engine.pairing import best_pair
from tests.results_fixtures import offer, seg


def dom(price, arrive):
    return offer(price, seg("LPA", "MAD", "2026-10-01T06:00", arrive))


def onward(price, depart):
    return offer(price, seg("MAD", "NRT", depart, "2026-10-02T09:00"))


def test_a_cheaper_tight_pair_loses_to_a_safe_one():
    cheap_late = dom("20", "2026-10-01T12:00")      # 1h before the onward -> HIGH
    dear_early = dom("60", "2026-10-01T08:00")      # 5h before -> LOW
    assert best_pair([cheap_late, dear_early], [onward("500", "2026-10-01T13:00")]) == \
        (dear_early, onward("500", "2026-10-01T13:00"))


def test_cheapest_within_the_best_level():
    a, b = dom("40", "2026-10-01T07:00"), dom("30", "2026-10-01T07:30")
    first, _ = best_pair([a, b], [onward("500", "2026-10-01T13:00")])
    assert first is b


def test_all_impossible_still_returns_the_cheapest_impossible_pair():
    """Review Focus #1: nothing is dropped here; the view marks it impossible."""
    late1, late2 = dom("50", "2026-10-01T20:00"), dom("40", "2026-10-01T21:00")
    first, _ = best_pair([late1, late2], [onward("500", "2026-10-01T13:00")])
    assert first is late2


def test_unknown_times_fall_back_to_the_cheapest_pair():
    """Offers without segments (Google sometimes, test fakes) are all UNKNOWN,
    so pairing reduces to today's cheapest-per-leg."""
    assert best_pair([offer("40"), offer("30")], [offer("500"), offer("450")]) == \
        (offer("30"), offer("450"))


def test_the_discount_can_change_which_safe_pair_is_cheapest():
    """Within one risk level the pairs are not a free product: here each
    domestic flight is only LOW with one onward flight. Undiscounted, the
    cheap domestic + dear onward wins (40 + 545 = 585 < 600); at 75% off the
    domestic side, the dear domestic + cheap onward wins (25 + 500 = 525 <
    10 + 545 = 555)."""
    dear_dom = dom("100", "2026-10-01T07:00")       # LOW with both onwards
    cheap_dom = dom("40", "2026-10-01T11:30")       # 1.5h before 13:00 -> HIGH
    early = onward("500", "2026-10-01T13:00")
    late = onward("545", "2026-10-01T16:00")        # 4.5h after cheap_dom -> LOW
    assert best_pair([dear_dom, cheap_dom], [early, late]) == (cheap_dom, late)
    assert best_pair([dear_dom, cheap_dom], [early, late],
                     first_discount=Decimal("0.75")) == (dear_dom, early)
```

Append to `tests/test_engine_drill.py`:

```python
from tests.results_fixtures import offer as timed_offer
from tests.results_fixtures import seg


async def test_confirm_pairs_flights_that_can_connect():
    """The cheapest domestic flight lands after the onward leaves; confirm
    must pair the dearer one that makes the connection."""
    too_late = timed_offer("20", seg("LPA", "MAD", "2026-10-01T10:00", "2026-10-01T14:00"))
    in_time = timed_offer("45", seg("LPA", "MAD", "2026-10-01T06:00", "2026-10-01T08:00"))
    onward = timed_offer("300", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00"))
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [too_late, in_time],
        ("MAD", "NRT", "2026-10-01"): [onward],
    })

    [itin] = await confirm(
        _fetcher(provider), [_cand("2026-10-01", "MAD", "NRT")], origin="LPA",
        trip_days=0, hub_names={}, dest_names={}, discount_airports=set(),
        discount=Decimal(0), options=SearchOptions(),
    )

    assert itin.dom_out is in_time
```

Append to `tests/test_engine_grid.py`:

```python
from tests.results_fixtures import offer as timed_offer
from tests.results_fixtures import seg


async def test_the_grid_pairs_flights_that_can_connect():
    too_late = timed_offer("20", seg("LPA", "MAD", "2026-10-01T10:00", "2026-10-01T14:00"))
    in_time = timed_offer("45", seg("LPA", "MAD", "2026-10-01T06:00", "2026-10-01T08:00"))
    onward = timed_offer("300", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00"))
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [too_late, in_time],
        ("MAD", "NRT", "2026-10-01"): [onward],
    })

    [itin] = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=SearchWindow("2026-10-01", "2026-10-01"), trip_days=0,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        options=SearchOptions(),
    )

    assert itin.dom_out is in_time
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_engine_pairing.py tests/test_engine_drill.py tests/test_engine_grid.py -q`
Expected: the pairing module is missing, and the two engine tests FAIL because `too_late` is picked.

- [ ] **Step 3: Implement**

`engine/pairing.py`:

```python
"""Which two flights to pair across a self-transfer.

The provider returns several offers per leg; the old rule took the cheapest
of each on its own, which paired flights that cannot connect. This takes the
cheapest pair within the best risk level available (engine.risk): a safer
connection beats a cheaper one, because a missed self-transfer loses both
tickets. It only chooses among offers already fetched -- zero new requests.
"""
from __future__ import annotations

from decimal import Decimal

from engine.risk import connection_risk
from providers.base import Offer


def _paid(offer: Offer, discount: Decimal) -> Decimal:
    return offer.price * (Decimal(1) - discount)


def best_pair(firsts: list[Offer], seconds: list[Offer], *,
              first_discount: Decimal = Decimal(0),
              second_discount: Decimal = Decimal(0)) -> tuple[Offer, Offer]:
    """The (first, second) pair with the lowest risk, cheapest among equals.

    Both lists must be non-empty. Ties keep the providers' own order (cheapest
    first), so the choice is deterministic.
    """
    return min(
        ((a, b) for a in firsts for b in seconds),
        key=lambda pair: (connection_risk(*pair),
                          _paid(pair[0], first_discount) + _paid(pair[1], second_discount)),
    )
```

`engine/drill.py`, in `confirm`: add `from engine.pairing import best_pair`. Replace the block from `rate = discount if cand.hub in discount_airports else Decimal(0)` through the `itineraries.append(Itinerary(...))` call with:

```python
        rate = discount if cand.hub in discount_airports else Decimal(0)
        dom_out, onward_out = best_pair(dom_out_offers, onward_out_offers, first_discount=rate)
        dom_ret = onward_ret = None
        if round_trip:
            onward_ret, dom_ret = best_pair(onward_ret_offers, dom_ret_offers,
                                            second_discount=rate)

        itineraries.append(Itinerary(
            date=cand.date,
            return_date=cand.return_date if round_trip else "",
            hub=cand.hub,
            hub_name=hub_names.get(cand.hub, cand.hub),
            dest=cand.dest,
            dest_name=dest_names.get(cand.dest, cand.dest),
            discount=rate,
            dom_out=dom_out,
            dom_ret=dom_ret,
            onward_out=onward_out,
            onward_ret=onward_ret,
        ))
```

Also change the two `is None` checks on the looked-up offer lists to `if not dom_out_offers or not onward_out_offers:` and `if not dom_ret_offers or not onward_ret_offers:`. `best_pair` needs non-empty lists, and an empty list is no offer. Update `confirm`'s docstring: replace any sentence saying each leg takes its cheapest offer with "Each direction takes the cheapest pair in the best connection-risk level (engine.pairing)."

`engine/grid.py`, in the combine loop: import `best_pair`, move the `rate = ...` line above the `Itinerary(...)` construction, and build:

```python
            dom_o, onward_o = best_pair(dom_out_offers, onward_out_offers, first_discount=rate)
            onward_r = dom_r = None
            if round_trip:
                onward_r, dom_r = best_pair(onward_ret_offers, dom_ret_offers,
                                            second_discount=rate)
```

and pass `dom_out=dom_o, dom_ret=dom_r, onward_out=onward_o, onward_ret=onward_r`. Remove the `cheapest` import from `grid.py` if it is now unused. Update the docstring sentence "Each leg uses its cheapest offer." the same way.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS. Existing tests use offers without segments, which are all `UNKNOWN`, so they still get cheapest-per-leg.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: pair flights that can connect, cheapest within the safest level

Confirm and the grid used to take each leg's cheapest offer on its own,
often pairing a domestic flight that lands after the onward one leaves."
```

---

### Task 3: `overnight` in the data model and storage

**Files:**
- Modify: `models.py` (`Candidate`, `Itinerary`, `from_candidate`), `providers/base.py` (`SearchOptions`), `results/store.py`, `db.py`
- Test: `tests/test_models.py`, `tests/test_providers_base.py`, `tests/test_results_store.py`, `tests/test_db.py`, `tests/test_search_draft.py` (pinned param keys)

**Interfaces:**
- Produces:
  - `Candidate.overnight: bool = False` and `Itinerary.overnight: bool = False`, each with `.dom_date -> str` and `.dom_return_date -> str`.
  - `SearchOptions.overnight: bool = False`, included in `as_columns()` and `from_mapping()`.
  - `save_search(..., overnight=False)` and `add_favorite(..., overnight=False)`.
  - `searches.overnight` and `favorites.overnight` (`INTEGER`, `NULL` = off).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_models.py`:

```python
from decimal import Decimal as _D

from models import Candidate


def test_overnight_moves_only_the_domestic_dates():
    c = Candidate(date="2026-10-01", return_date="2026-10-15", hub="MAD", dest="NRT",
                  dom_price=_D(1), onward_price=_D(1), discount=_D(0), overnight=True)
    assert (c.dom_date, c.dom_return_date) == ("2026-09-30", "2026-10-16")
    plain = Candidate(date="2026-10-01", return_date="", hub="MAD", dest="NRT",
                      dom_price=_D(1), onward_price=_D(1), discount=_D(0))
    assert (plain.dom_date, plain.dom_return_date) == ("2026-10-01", "")


def test_an_itinerary_from_an_overnight_candidate_stays_overnight():
    from models import Itinerary
    c = Candidate(date="2026-10-01", return_date="", hub="MAD", dest="NRT",
                  dom_price=_D(1), onward_price=_D(1), discount=_D(0), overnight=True)
    itin = Itinerary.from_candidate(c, "Madrid", "Tokyo")
    assert itin.overnight and itin.dom_date == "2026-09-30"
```

Append to `tests/test_providers_base.py`:

```python
def test_overnight_round_trips_through_the_columns():
    opts = SearchOptions(overnight=True)
    assert opts.as_columns()["overnight"] is True
    assert SearchOptions.from_mapping({"overnight": 1}) == opts
    assert SearchOptions.from_mapping({"overnight": None}).overnight is False
```

Append to `tests/test_results_store.py`:

```python
def test_overnight_survives_storage_and_old_rows_default_to_off():
    """Review Focus #4."""
    night = standard_one_way(overnight=True)
    assert _round_trip([night]).itineraries[0].overnight is True
    raw = serialize([standard_one_way()])
    del raw["itineraries"][0]["overnight"]
    assert load(json.dumps(raw)).itineraries[0].overnight is False
```

Append to `tests/test_db.py`, after adding `"overnight"` to `SEARCHES_NEW_COLUMNS` (and `FAVORITES_NEW_COLUMNS`, if that set exists for the favourites migration):

```python
async def test_overnight_is_stored_for_searches_and_favourites(temp_db):
    sid = await db_module.save_search(
        origin="LPA", destinations=["NRT"], dates=["2026-10-01"], hubs=["MAD"],
        adults=1, currency="EUR", best_price=None, best_route=None, results=None,
        overnight=True,
    )
    assert (await db_module.get_search_by_id(sid))["overnight"] == 1
    await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                 currency="EUR", price=None, check_dates=["2026-10-01"],
                                 overnight=True)
    assert (await db_module.get_favorites())[0]["overnight"] == 1
```

In `tests/test_search_draft.py`, `test_to_params_matches_what_run_and_report_takes`: add `"overnight"` to the expected key set. This is the deliberate contract change.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_models.py tests/test_providers_base.py tests/test_results_store.py tests/test_db.py tests/test_search_draft.py -q`
Expected: FAIL on the unknown `overnight` field and the missing columns.

- [ ] **Step 3: Implement**

`models.py`:
- `Candidate`: add `overnight: bool = False` after `discount`, plus:

  ```python
      @property
      def dom_date(self) -> str:
          """The domestic outbound's day: the day before the onward flight overnight."""
          return add_days(self.date, -1) if self.overnight else self.date

      @property
      def dom_return_date(self) -> str:
          """The domestic return's day: the day after the onward return overnight."""
          if not self.return_date:
              return ""
          return add_days(self.return_date, 1) if self.overnight else self.return_date
  ```

- `Itinerary`: add `overnight: bool = False` after `providers`, and the same two properties.
- `from_candidate`: pass `overnight=candidate.overnight`.

`providers/base.py`, `SearchOptions`:
- Add the field `overnight: bool = False` after `min_layover`, with the comment `# domestic legs a day early (out) / late (back): a night at the hub`.
- `as_columns`: add `"overnight": self.overnight`.
- `from_mapping`: add `overnight=bool(m.get("overnight") or False)`.

`results/store.py`: in `_itinerary_to_dict` add `"overnight": it.overnight`. In `_itinerary_from_v2` add `overnight=bool(d.get("overnight", False))`.

`db.py`:
- `searches` `CREATE TABLE`: after `min_layover`, add `overnight INTEGER -- 1 = a night at the hub; NULL = off`, fixing the trailing commas.
- `favorites` `CREATE TABLE`: add `overnight INTEGER,` after `min_layover`.
- `MIGRATIONS`: append `("searches", "overnight", "INTEGER")` and `("favorites", "overnight", "INTEGER")`.
- `save_search`: add `overnight: bool = False`, insert `int(overnight)` into the column list, placeholders and values.
- `add_favorite`: add `overnight: bool = False` and insert `int(overnight)` the same way. Read the function's `INSERT` and extend it in the same pattern its other columns follow.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS. `run_and_report`, history and favourites already splat `as_columns()`, so `overnight` flows through them with no further edit.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: model and store the night-at-the-hub option"
```

---

### Task 4: The engine honours `overnight`

**Files:**
- Modify: `engine/scan.py` (`_build_jobs`, `rank_candidates`), `engine/shortlist.py` (`legs_for`), `engine/drill.py` (`confirm` lookups), `engine/grid.py`, `engine/orchestrator.py` (`_run_two_stage`)
- Test: `tests/test_engine_scan.py`, `tests/test_engine_drill.py`, `tests/test_engine_grid.py`, `tests/test_engine_orchestrator.py`

**Interfaces:**
- Consumes: `Candidate.dom_date` / `dom_return_date`, `SearchOptions.overnight` (Task 3).
- Produces: `rank_candidates(..., overnight: bool = False)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_scan.py`:

```python
async def test_overnight_shifts_only_the_domestic_calendar_windows():
    provider = FakeCalendarProvider()
    await scan_calendars(provider, origin="LPA", hubs=["MAD"], dests=["NRT"],
                         window=WINDOW, trip_days=14,
                         options=SearchOptions(overnight=True))
    assert ("LPA", "MAD", "2026-09-30", "2026-10-02") in provider.calls    # a day early
    assert ("MAD", "NRT", "2026-10-01", "2026-10-03") in provider.calls    # unchanged
    assert ("MAD", "LPA", "2026-10-16", "2026-10-18") in provider.calls    # a day late
    assert ("NRT", "MAD", "2026-10-15", "2026-10-17") in provider.calls    # unchanged


def test_overnight_ranking_pairs_the_domestic_day_before():
    grid = _grid(out_dom={"MAD": {"2026-09-30": "29"}},
                 out_onward={("MAD", "NRT"): {"2026-10-01": "575"}})
    assert rank_candidates(grid, window=WINDOW, trip_days=0,
                           discount_airports=set(), discount=Decimal(0)) == []
    [c] = rank_candidates(grid, window=WINDOW, trip_days=0, discount_airports=set(),
                          discount=Decimal(0), overnight=True)
    assert (c.date, c.dom_date, c.overnight) == ("2026-10-01", "2026-09-30", True)
```

Append to `tests/test_engine_drill.py`:

```python
async def test_confirm_queries_the_domestic_legs_on_their_shifted_days():
    """Review Focus #3: out a day early, back a day late."""
    provider = FakeProvider({
        ("LPA", "MAD", "2026-09-30"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
        ("NRT", "MAD", "2026-10-15"): [_offer("280")],
        ("MAD", "LPA", "2026-10-16"): [_offer("35")],
    })
    cand = dataclasses.replace(_cand("2026-10-01", "MAD", "NRT", return_date="2026-10-15"),
                               overnight=True)

    [itin] = await confirm(
        _fetcher(provider), [cand], origin="LPA", trip_days=14, hub_names={},
        dest_names={}, discount_airports=set(), discount=Decimal(0),
        options=SearchOptions(overnight=True),
    )

    assert itin.overnight
    assert itin.total == Decimal("655.00")
    assert {(q.origin, q.dest, q.date) for q in provider.seen} == {
        ("LPA", "MAD", "2026-09-30"), ("MAD", "NRT", "2026-10-01"),
        ("NRT", "MAD", "2026-10-15"), ("MAD", "LPA", "2026-10-16"),
    }
```

Append to `tests/test_engine_grid.py`:

```python
async def test_the_grid_flies_the_domestic_legs_a_day_early_and_late():
    provider = FakeProvider({
        ("LPA", "MAD", "2026-09-30"): [_offer("40")],
        ("MAD", "NRT", "2026-10-01"): [_offer("300")],
        ("NRT", "MAD", "2026-10-08"): [_offer("280")],
        ("MAD", "LPA", "2026-10-09"): [_offer("35")],
    })

    [itin] = await run_grid_search(
        _fetcher(provider), origin="LPA", dests=["NRT"], hubs=["MAD"],
        window=SearchWindow("2026-10-01", "2026-10-01"), trip_days=7,
        hub_names={}, dest_names={}, discount_airports=set(), discount=Decimal(0),
        options=SearchOptions(overnight=True),
    )

    assert (itin.date, itin.return_date, itin.overnight) == ("2026-10-01", "2026-10-08", True)
    assert ("LPA", "MAD", "2026-09-30") in _seen(provider)
    assert ("MAD", "LPA", "2026-10-09") in _seen(provider)
```

Append to `tests/test_engine_orchestrator.py`:

```python
async def test_overnight_end_to_end_two_stage(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeCalendarProvider(
        calendar_answers={("LPA", "MAD"): {"2026-09-30": "29"},
                          ("MAD", "NRT"): {"2026-10-01": "500"}},
        leg_answers={("LPA", "MAD", "2026-09-30"): [_offer("25")],
                     ("MAD", "NRT", "2026-10-01"): [_offer("480")],
                     ("LPA", "NRT", "2026-10-01"): [_offer_pnr("700")]},
    )
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(origin="LPA", destinations={"NRT": "Tokyo"},
                              hubs={"MAD": "Madrid"}, window=WINDOW, trip_days=0,
                              provider=provider, options=SearchOptions(overnight=True))

    [itin] = result.itineraries
    assert (itin.dom_date, itin.date, itin.overnight) == ("2026-09-30", "2026-10-01", True)
    assert itin.through_fare == Decimal("700")      # priced on the onward date
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_engine_scan.py tests/test_engine_drill.py tests/test_engine_grid.py tests/test_engine_orchestrator.py -q`
Expected: FAIL. The windows aren't shifted, `rank_candidates` rejects `overnight=`, and the legs are queried on the onward dates.

- [ ] **Step 3: Implement**

`engine/scan.py`:
- `_build_jobs`, before building jobs:

  ```python
      # A night at the hub: the domestic legs fly a day before (out) and a day
      # after (back) the onward flights, so only their windows move.
      dom_shift = -1 if options.overnight else 0
  ```

  - The outbound domestic job uses `options.calendar_query(origin, hub, add_days(window.start, dom_shift), add_days(window.end, dom_shift))`.
  - The return domestic job uses `options.calendar_query(hub, origin, add_days(ret_start, -dom_shift), add_days(ret_end, -dom_shift))`.
  - The onward jobs are unchanged.
- `rank_candidates`: add the keyword `overnight: bool = False`. Inside the loop:

  ```python
              out_dom = out_dom_prices.get(add_days(date, -1) if overnight else date)
  ```

  and on the return, `ret_dom = ret_dom_prices.get(add_days(return_date, 1) if overnight else return_date)`. Pass `overnight=overnight` to `Candidate(...)`. Add one docstring sentence: "With ``overnight``, a candidate's domestic legs are read a day before (out) and a day after (back) its onward dates."

`engine/shortlist.py`, `legs_for`: use `cand.dom_date` for the `(origin, cand.hub, …)` leg and `cand.dom_return_date` for the `(cand.hub, origin, …)` leg. Onward legs are unchanged.

`engine/drill.py`, `confirm`: look up `(origin, cand.hub, cand.dom_date)` and `(cand.hub, origin, cand.dom_return_date)`, and pass `overnight=cand.overnight` to `Itinerary(...)`.

`engine/orchestrator.py`, `_run_two_stage`: pass `overnight=options.overnight` to `rank_candidates(...)`.

`engine/grid.py`: sampled `dates` stay onward dates. Replace phases 1–2R and the combine header with:

```python
    shift = -1 if options.overnight else 0     # domestic day relative to the onward day

    # ── Phase 1: outbound domestic leg (origin -> hubs) ─────────────────────
    phase1_queries = [
        options.leg_query(origin, hub, add_days(date, shift))
        for hub in hubs
        for date in dates
    ]
    dom_out = await fetcher.fetch_many(phase1_queries, phase="Phase 1")
    if not dom_out:
        return []
    # (hub, onward date) pairs phase 1 proved reachable.
    reachable = [(hub, add_days(dom_date, -shift)) for (_, hub, dom_date) in dom_out]

    # ── Phase 1R: return domestic leg (hubs -> origin), a day late overnight ─
    dom_ret: dict[tuple[str, str, str], list[Offer]] = {}
    if round_trip:
        phase1r_queries = [
            options.leg_query(hub, origin, add_days(date, trip_days - shift))
            for (hub, date) in reachable
        ]
        dom_ret = await fetcher.fetch_many(phase1r_queries, phase="Phase 1R")

    # ── Phase 2: outbound onward leg (hubs -> destinations) ─────────────────
    phase2_queries = [
        options.leg_query(hub, dest, date)
        for (hub, date) in reachable
        for dest in dests
    ]
    onward_out = await fetcher.fetch_many(phase2_queries, phase="Phase 2")
```

Keep phase 2R as it is: onward returns are on `add_days(date, trip_days)`. The combine loop becomes `for (hub, date) in reachable:` with `dom_out_offers = dom_out[(origin, hub, add_days(date, shift))]`, and on the return `dom_ret.get((hub, origin, add_days(return_date, -shift)))`. Pass `overnight=options.overnight` to `Itinerary(...)`. Keep the existing comment block about "only (hub, date) pairs phase 1 proved reachable" beside the phase 2 queries.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: a night at the hub moves the domestic flights a day early and late"
```

---

### Task 5: Show risk and the night; filter risky results

**Files:**
- Modify: `results/view.py`, `results/filters.py`
- Test: `tests/test_results_view.py`, `tests/test_results_filters.py`, `tests/test_results_handlers.py` (pinned `view_json` dict)

**Interfaces:**
- Consumes: `itinerary_risk`, `connection_risk`, `Risk`, `RISK_LABELS` (Task 1); `Itinerary.overnight` (Task 3).
- Produces: `Filters.hide_risky: bool = False`, the filter key `r` (`"1"` / `"any"`), and the view markers.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_results_view.py`:

```python
def _tight_return():
    return standard_round_trip(
        dom_ret=offer("90", seg("MAD", "LPA", "2026-10-15T19:00", "2026-10-15T20:45")))


def test_the_summary_marks_a_tight_connection_with_its_own_gap():
    """Review Focus #2: the outbound has 3h, the return 1h; the 1h is shown."""
    meta = replace(META, round_trip=True)
    text, _ = summary(meta, StoredResults([_tight_return()], True), Filters(), 1)
    assert "⚠️ 1h00m" in text
    assert "3h00m" not in text


def test_impossible_and_unknown_are_marked_and_low_is_not():
    impossible = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T14:00")),
        onward=onward_out())
    unknown = one_way(dom=offer("100"), onward=offer("500"))
    text, _ = summary(META, StoredResults([impossible, unknown, standard_one_way()], True),
                      Filters(), 1)
    assert "⛔ impossible" in text and "❔ times unknown" in text
    assert text.count("⚠️") == 0


def test_the_detail_states_the_risk_and_the_separate_ticket_reminder():
    text, _ = detail(META, StoredResults([standard_one_way()], True), 0, 1)
    assert "Connection risk: Medium" in text
    assert "3h00m between tickets at MAD" in text
    assert "the second airline won't wait" in text


def test_a_night_at_the_hub_is_called_out_and_not_priced():
    night = standard_one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-09-30T18:00", "2026-09-30T21:00")),
        overnight=True)
    text, _ = summary(META, StoredResults([night], True), Filters(), 1)
    assert "🌙" in text
    detail_text, _ = detail(META, StoredResults([night], True), 0, 1)
    assert "1 night in Madrid before your flight — not included in the price" in detail_text
    assert "Connection risk: Low" in detail_text
```

Append to `tests/test_results_filters.py`:

```python
from tests.results_fixtures import onward_out


def test_hide_risky_keeps_low_and_medium_only():
    tight = one_way(dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T12:00")),
                    onward=onward_out())                     # 1h -> HIGH
    unknown = one_way(dom=offer("100"), onward=offer("500"))
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    shown, hidden = apply([standard_one_way(), tight, unknown, estimate],
                          Filters(hide_risky=True))
    assert [i for i, _ in shown] == [0]
    assert hidden == 3          # Review Focus #5: the estimate is hidden and counted


def test_hide_risky_setting_and_persistence():
    assert Filters().with_setting("r", "1") == Filters(hide_risky=True)
    assert Filters(hide_risky=True).with_setting("r", "any") == Filters()
    assert Filters(hide_risky=True).active == 1
    assert Filters.from_dict(Filters(hide_risky=True).to_dict()).hide_risky is True
    assert Filters.from_dict({"max_stops": 0}) == Filters(max_stops=0)   # pre-risk view_json
```

In `tests/test_results_handlers.py`, `test_setting_a_filter_persists_and_resets_to_page_one`: add `"hide_risky": False` to the expected `filters` dict. This is the deliberate shape change.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_view.py tests/test_results_filters.py tests/test_results_handlers.py -q`
Expected: FAIL. There are no markers, no risk line, and `hide_risky` is unknown.

- [ ] **Step 3: Implement `results/filters.py`**

- Add the field `hide_risky: bool = False` last.
- `active`: add `+ (1 if self.hide_risky else 0)`.
- `to_dict`: add `"hide_risky": self.hide_risky`.
- `from_dict`: add `hide_risky=d.get("hide_risky") is True`.
- `with_setting`, before the `_SETTINGS` lookup:

  ```python
          if key == "r":
              if value not in ("1", "any"):
                  raise ValueError(f"{value!r} is not an option for 'r'")
              return replace(self, hide_risky=value == "1")
  ```

- `_passes`, after the exclude check:

  ```python
      if f.hide_risky:
          judged = itinerary_risk(itin)
          if judged is None or judged[0] >= Risk.UNKNOWN:
              return False
  ```

- Import `from engine.risk import Risk, itinerary_risk`.

- [ ] **Step 4: Implement `results/view.py`**

- Import `from engine.risk import RISK_LABELS, Risk, connection_risk, itinerary_risk`.
- Add:

  ```python
  def _risk_marker(itin: Itinerary) -> str:
      """A short summary-row marker; LOW and MEDIUM stay quiet to keep rows short."""
      judged = itinerary_risk(itin)
      if judged is None:
          return ""
      risk, _ = judged
      if risk is Risk.IMPOSSIBLE:
          return " · ⛔ impossible"
      if risk is Risk.UNKNOWN:
          return " · ❔ times unknown"
      if risk is Risk.HIGH:
          connections = [(itin.dom_out, itin.onward_out, itin.buffer_out)]
          if itin.return_date:
              connections.append((itin.onward_ret, itin.dom_ret, itin.buffer_ret))
          for arriving, departing, gap in connections:
              if connection_risk(arriving, departing) is Risk.HIGH:
                  return f" · ⚠️ {_gap(gap)}" if gap is not None else " · ⚠️ airport change"
      return ""
  ```

- In `summary`'s row f-string, after `{_MARKERS.get(it.status, '')}`, append `{_risk_marker(it)}{' · 🌙' if it.overnight else ''}`.
- In `detail`, inside the `else:` (detailed) branch, after the direction blocks loop:

  ```python
          judged = itinerary_risk(itin)
          if judged is not None:
              risk, reasons = judged
              parts.append(
                  f"<b>Connection risk: {RISK_LABELS[risk]}</b> — {esc('; '.join(reasons))}\n"
                  "<i>Separate tickets: collect and re-check your bags, and the second "
                  "airline won't wait if the first is late.</i>")
          if itin.overnight:
              night = f"🌙 1 night in {esc(itin.hub_name)} before your flight"
              if itin.return_date:
                  night += ", and 1 on the way back"
              parts.append(night + " — not included in the price.")
  ```

- In `filters_screen`, before the airlines rows:

  ```python
      rows.append([Button("Connection risk", noop)])
      rows.append([Button(_mark_opt("Any risk", not filters.hide_risky), f"r:{sid}:f:r:any"),
                   Button(_mark_opt("Hide risky", filters.hide_risky), f"r:{sid}:f:r:1")])
  ```

  and define next to `opt`:

  ```python
      def _mark_opt(label: str, on: bool) -> str:
          return f"• {label}" if on else label
  ```

- [ ] **Step 5: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS. If an existing detail test asserts an exact paragraph list or count that the new risk line changes, update that assertion and record a ruling.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: show connection risk and nights at the hub; filter risky results"
```

---

### Task 6: The toggle in the builder

**Files:**
- Modify: `handlers/search/draft.py` (field, `options`, options line), `handlers/search/options.py` (row, `o:o:` key), `handlers/search/dates.py` (caption)
- Test: `tests/test_search_options.py`, `tests/test_search_draft.py`, `tests/test_search_dates.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_search_options.py`:

```python
def test_the_night_at_the_hub_toggle():
    on = apply(_draft(), "o:o:1", KIWI)
    assert on.overnight is True
    assert apply(on, "o:o:0", KIWI).overnight is False
    assert isinstance(apply(_draft(), "o:o:yes", KIWI), str)
    _, rows = render(on, GOOGLE)            # every provider can search any date
    assert "o:o:0" in _data(rows)
```

Append to `tests/test_search_draft.py`:

```python
def test_the_options_line_names_the_night_at_the_hub():
    d = SearchDraft(origin="LPA", origin_name="Gran Canaria", overnight=True)
    assert d.options.overnight is True
    assert "night at hub" in d.render()[0]
```

Append to `tests/test_search_dates.py`:

```python
def test_the_caption_says_which_day_the_dates_mean_overnight():
    from handlers.search.dates import caption
    from handlers.search.draft import SearchDraft

    text = caption(SearchDraft(origin="LPA", origin_name="GC", overnight=True))
    assert "international flight" in text
    assert "international flight" not in caption(SearchDraft(origin="LPA", origin_name="GC"))
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_search_options.py tests/test_search_draft.py tests/test_search_dates.py -q`
Expected: FAIL. `SearchDraft` has no `overnight` field.

- [ ] **Step 3: Implement**

`handlers/search/draft.py`:
- Add the field `overnight: bool = False` after `min_layover`.
- `options`: pass `overnight=self.overnight`.
- `_options_line`: before `return`, add `if self.overnight: parts.append("night at hub")`.

`handlers/search/options.py`:
- In `render`, before the Back row:

  ```python
      rows.append([Button(_mark("🌙 Night at the hub", draft.overnight),
                          "o:o:0" if draft.overnight else "o:o:1")])
  ```

- In `apply`, add a branch before the final `else`:

  ```python
      elif key == "o" and value in ("0", "1"):
          new = draft.with_(overnight=value == "1")
  ```

`handlers/search/dates.py`, `caption`: before `return "\n".join(lines)`:

```python
    if draft.overnight:
        lines.append("<i>🌙 These are the international flight's dates; the domestic "
                     "flight is the day before.</i>")
```

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: a night-at-the-hub toggle in the search options"
```

---

### Task 7: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update Features**

After the "Filters" bullet, add:

```markdown
- **Connection risk** — a self-transfer is two contracts: miss the second
  flight and its airline owes you nothing. Each result is rated low, medium,
  high or impossible from the time between tickets (an airport change counts
  as high), and the pairing itself prefers a connection you can make over a
  cheaper one you can't. "Hide risky" leaves only low and medium.
- **Night at the hub** — an option that flies the domestic leg the day before
  the international one (and the day after on the way back), for a
  stress-free connection. Same request count; the night is called out, not
  priced.
```

In the "Filters" bullet, change "stops, total journey time, minimum time between tickets and excluded airlines" to "stops, total journey time, minimum time between tickets, connection risk and excluded airlines".

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: connection risk and the night-at-the-hub option"
```
