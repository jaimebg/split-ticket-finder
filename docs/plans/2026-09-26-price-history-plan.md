# Price History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each tracked route's price history, and make alerts explain why a price matters, with a second, trend-based trigger.

**Architecture:** `results/history.py` is pure. It computes statistics, the sparkline, the trend signal and all the text (history screen, alert, favourite line) from `(checked_at, price)` pairs and a `today` date. `db.py` reads the checks. `handlers/favorites.py` shows them, and `scheduler.py` decides and sends the alerts.

**Tech Stack:** Python 3.10+, python-telegram-bot 21, aiosqlite, pytest (`asyncio_mode = "auto"`), ruff.

**Spec:** `docs/plans/2026-09-26-price-history-design.md`

## Global Constraints

- Python 3.10 floor. No new dependencies. The suite stays offline.
- Config defaults: `PRICE_HISTORY_DAYS=30`, `SPARK_POINTS=20`, `ALERT_MIN_CHECKS=5`. Drop threshold: the existing `PRICE_DROP_THRESHOLD` (0.10).
- Sparkline bars: `▁▂▃▄▅▆▇█`. All prices equal, or a single price, gives `▄` for each point. No prices gives `""`.
- A single check sends at most one alert. A record drop updates the record. A trend alert never updates it.
- A favourite with a `NULL` record sets it from its first check and sends no alert.
- Every route and code string in Telegram HTML goes through `esc`. Every `callback_data` is ≤ 64 bytes.
- Branch `feat/price-history`. Commit per task. **Never push or merge to `main`.** `ruff check .` and `.venv/bin/pytest -q` are clean after every task. E402 is off, so don't write `# noqa: E402`.

## Review Focus

1. **Checks with a `NULL` price.** They are skipped everywhere: stats, trend and sparkline. They never raise. (Task 1)
2. **A favourite with exactly `ALERT_MIN_CHECKS` earlier checks, all on the same day.** The trend reason reads "lowest so far", not "lowest in 0 days". (Task 2)
3. **The record rule and the trend rule both fire.** One message is sent, naming both reasons. (Task 3)
4. **A history screen for a deleted favourite, or for one never checked.** The first says "That route is no longer tracked."; the second says "Not checked yet — …". Neither raises. (Task 3)
5. **HTML-hostile route codes in the list, the screen and the alert.** They are escaped. (Task 2)

---

### Task 1: Statistics, sparkline, trend signal and storage reads

**Files:**
- Create: `results/history.py`
- Modify: `config.py` (after the risk thresholds), `.env.example`, `db.py` (price checks section)
- Test: `tests/test_results_history.py` (new), `tests/test_db.py`

**Interfaces:**
- Produces:
  - `sparkline(prices: list[float]) -> str`
  - `@dataclass(frozen=True) PriceStats(count, first_day, last, previous, low, high, average, lowest_in_days, sparkline)`
  - `price_stats(checks: list[tuple[str, float | None]], *, today: date) -> PriceStats | None`
  - `@dataclass(frozen=True) TrendSignal(days: int, average: float, pct_below: int)`
  - `trend_signal(prior: list[tuple[str, float | None]], last: float, *, today: date) -> TrendSignal | None`
  - `db.get_price_checks(fav_id) -> list[tuple[str, float | None]]`, oldest first
  - `db.get_favorite(fav_id) -> dict | None`
  - `config.PRICE_HISTORY_DAYS`, `config.SPARK_POINTS`, `config.ALERT_MIN_CHECKS`

- [ ] **Step 1: Write the failing tests**

`tests/test_results_history.py`:

```python
"""results.history: numbers and text from a favourite's price checks."""
from __future__ import annotations

from datetime import date

import pytest

import config
from results.history import price_stats, sparkline, trend_signal

TODAY = date(2026, 10, 31)


def _c(day: str, price):
    return (f"{day}T08:00:00Z", price)


def test_sparkline_maps_the_range_onto_eight_bars():
    assert sparkline([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█"
    assert sparkline([10, 10, 10]) == "▄▄▄"
    assert sparkline([7]) == "▄"
    assert sparkline([]) == ""


def test_stats_skip_null_prices_and_report_the_basics():
    """Review Focus #1."""
    checks = [_c("2026-10-01", 800), _c("2026-10-02", None), _c("2026-10-20", 700),
              _c("2026-10-30", 650)]
    s = price_stats(checks, today=TODAY)
    assert (s.count, s.first_day, s.last, s.previous, s.low, s.high) == \
        (3, "2026-10-01", 650, 700, 650, 800)
    assert s.sparkline == sparkline([800, 700, 650])


def test_the_average_covers_the_window_and_falls_back_to_everything(monkeypatch):
    monkeypatch.setattr(config, "PRICE_HISTORY_DAYS", 30)
    checks = [_c("2026-09-01", 1000), _c("2026-10-20", 700), _c("2026-10-30", 500)]
    assert price_stats(checks, today=TODAY).average == 600          # 1000 is outside 30 days
    old = [_c("2026-08-01", 900), _c("2026-08-02", 700)]
    assert price_stats(old, today=TODAY).average == 800             # nothing in window: all


def test_lowest_in_days_counts_back_while_earlier_prices_are_not_lower():
    checks = [_c("2026-10-01", 600), _c("2026-10-10", 900), _c("2026-10-21", 800),
              _c("2026-10-30", 700)]
    assert price_stats(checks, today=TODAY).lowest_in_days == 21   # back to 10-10, not 10-01
    assert price_stats(checks[:1] + [_c("2026-10-30", 500)], today=TODAY).lowest_in_days == 30
    rising = [_c("2026-10-20", 500), _c("2026-10-30", 600)]
    assert price_stats(rising, today=TODAY).lowest_in_days is None
    assert price_stats([_c("2026-10-30", 500)], today=TODAY).lowest_in_days is None


def test_no_priced_checks_is_no_stats():
    assert price_stats([], today=TODAY) is None
    assert price_stats([_c("2026-10-01", None)], today=TODAY) is None


def test_sparkline_uses_the_last_points_only(monkeypatch):
    monkeypatch.setattr(config, "SPARK_POINTS", 3)
    checks = [_c(f"2026-10-{d:02d}", p) for d, p in ((1, 1), (2, 9), (3, 1), (4, 2), (5, 3))]
    assert price_stats(checks, today=TODAY).sparkline == sparkline([1, 2, 3])


@pytest.fixture
def five_prior():
    return [_c("2026-10-2%d" % d, p) for d, p in enumerate((800, 790, 810, 800, 805))]


def test_trend_fires_on_a_new_low_well_below_the_prior_average(five_prior):
    t = trend_signal(five_prior, 700, today=TODAY)
    assert t is not None
    assert t.average == pytest.approx(801)
    assert t.pct_below == 12
    assert t.days == 11                          # oldest prior check, 2026-10-20


def test_trend_needs_enough_history(five_prior, monkeypatch):
    monkeypatch.setattr(config, "ALERT_MIN_CHECKS", 6)
    assert trend_signal(five_prior, 700, today=TODAY) is None


def test_trend_needs_both_a_new_low_and_a_real_drop(five_prior):
    assert trend_signal(five_prior, 750, today=TODAY) is None     # new low, only 6% below
    spiky = five_prior + [_c("2026-10-29", 690)]
    assert trend_signal(spiky, 700, today=TODAY) is None          # not a new low


def test_trend_ignores_null_prior_checks(five_prior):
    assert trend_signal(five_prior + [_c("2026-10-30", None)], 700, today=TODAY) is not None
```

Append to `tests/test_db.py`:

```python
async def test_price_checks_come_back_oldest_first_even_on_a_tie(temp_db):
    fav = await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                       currency="EUR", price=None, check_dates=["2026-10-01"])
    for price in (700.0, None, 650.0):
        await db_module.add_price_check(fav, price, None)    # same second: id breaks the tie
    checks = await db_module.get_price_checks(fav)
    assert [p for _, p in checks] == [700.0, None, 650.0]
    assert (await db_module.get_favorite(fav))["id"] == fav
    assert await db_module.get_favorite(fav + 99) is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_history.py tests/test_db.py -q`
Expected: collection error, `No module named 'results.history'`.

- [ ] **Step 3: Implement**

`config.py`, after `RISK_MEDIUM_BELOW_HOURS`:

```python
# Price history (results/history.py): the window the average and the trend
# alert look back over, how many points the sparkline shows, and how many
# earlier checks a trend alert needs before it can fire.
PRICE_HISTORY_DAYS = _int_env("PRICE_HISTORY_DAYS", 30, lo=1)
SPARK_POINTS = _int_env("SPARK_POINTS", 20, lo=2)
ALERT_MIN_CHECKS = _int_env("ALERT_MIN_CHECKS", 5, lo=1)
```

`.env.example`, after `RISK_MEDIUM_BELOW_HOURS=4`:

```
# Price history: average/trend window in days, sparkline length, and how many
# earlier checks a "lowest in N days" alert needs.
PRICE_HISTORY_DAYS=30
SPARK_POINTS=20
ALERT_MIN_CHECKS=5
```

`db.py`, at the end of the price checks section:

```python
async def get_price_checks(fav_id: int) -> list[tuple[str, float | None]]:
    """A favourite's checks as (checked_at, best_price), oldest first. Checks
    in the same second keep their insertion order (id breaks the tie)."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT checked_at, best_price FROM price_checks WHERE favorite_id = ? "
            "ORDER BY checked_at, id", (fav_id,))
        return [(row[0], row[1]) for row in await cursor.fetchall()]
```

And after `get_favorites`:

```python
async def get_favorite(fav_id: int) -> dict | None:
    """One favourite by id, or None."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM favorites WHERE id = ?", (fav_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
```

`results/history.py`:

```python
"""A tracked route's price history: numbers, the sparkline and all the text.

Pure. Everything is computed from ``(checked_at, price)`` pairs, oldest
first, and a ``today`` date passed in, so nothing here depends on the clock
or the database. Checks with no price (a run that found nothing) are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import config

_BARS = "▁▂▃▄▅▆▇█"


def sparkline(prices: list[float]) -> str:
    """Each price mapped linearly onto eight bars; a flat series sits mid-height."""
    if not prices:
        return ""
    low, high = min(prices), max(prices)
    if high == low:
        return _BARS[3] * len(prices)
    scale = (len(_BARS) - 1) / (high - low)
    return "".join(_BARS[round((p - low) * scale)] for p in prices)


def _priced(checks: list[tuple[str, float | None]]) -> list[tuple[date, float]]:
    return [(date.fromisoformat(ts[:10]), float(p)) for ts, p in checks if p is not None]


def _window(points: list[tuple[date, float]], today: date) -> list[tuple[date, float]]:
    recent = [(d, p) for d, p in points if (today - d).days <= config.PRICE_HISTORY_DAYS]
    return recent or points


@dataclass(frozen=True)
class PriceStats:
    count: int
    first_day: str
    last: float
    previous: float | None
    low: float
    high: float
    average: float
    lowest_in_days: int | None
    sparkline: str


def price_stats(checks: list[tuple[str, float | None]], *, today: date) -> PriceStats | None:
    points = _priced(checks)
    if not points:
        return None
    prices = [p for _, p in points]
    last = prices[-1]
    window = _window(points, today)

    lowest_in_days = None
    if len(points) >= 2 and prices[-2] >= last:
        i = len(points) - 2
        while i > 0 and prices[i - 1] >= last:
            i -= 1
        lowest_in_days = (today - points[i][0]).days

    return PriceStats(
        count=len(points),
        first_day=points[0][0].isoformat(),
        last=last,
        previous=prices[-2] if len(prices) >= 2 else None,
        low=min(prices),
        high=max(prices),
        average=sum(p for _, p in window) / len(window),
        lowest_in_days=lowest_in_days,
        sparkline=sparkline(prices[-config.SPARK_POINTS:]),
    )


@dataclass(frozen=True)
class TrendSignal:
    days: int            # how far back the new low reaches, in days
    average: float       # of the earlier checks in the window
    pct_below: int       # how far below that average, in whole percent


def trend_signal(prior: list[tuple[str, float | None]], last: float, *,
                 today: date) -> TrendSignal | None:
    """A 'low against the trend' alert, or None.

    Fires only with at least ALERT_MIN_CHECKS earlier priced checks, when
    *last* is strictly below every earlier price in the window and at least
    PRICE_DROP_THRESHOLD below their average. The average is over the
    earlier checks only, so the new low doesn't lower its own baseline.
    """
    points = _priced(prior)
    if len(points) < config.ALERT_MIN_CHECKS:
        return None
    window = _window(points, today)
    prices = [p for _, p in window]
    average = sum(prices) / len(prices)
    if not (last < min(prices) and last <= average * (1 - config.PRICE_DROP_THRESHOLD)):
        return None
    return TrendSignal(days=(today - window[0][0]).days, average=average,
                       pct_below=int((average - last) / average * 100))
```

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: price statistics, sparkline and trend signal from stored checks"
```

---

### Task 2: The text: favourite line, history screen, alert

**Files:**
- Modify: `results/history.py`, `handlers/utils.py` (`format_favorite`)
- Test: `tests/test_results_history.py`, `tests/test_handlers_favorites.py`

**Interfaces:**
- Consumes: `PriceStats`, `TrendSignal` (Task 1); `SearchOptions.from_mapping`, `.party_label()`.
- Produces:
  - `options_line(fav: dict) -> str`, e.g. `"2 adults · Business · EUR · 🌙 · ≤1 stop"`
  - `versus_average(stats: PriceStats | None) -> str | None`
  - `history_screen(fav: dict, stats: PriceStats | None, *, origin: str) -> tuple[str, Rows]`
  - `alert_text(fav: dict, *, origin: str, last: float, date: str, record_before: float | None, record_drop: bool, trend: TrendSignal | None, spark: str) -> str`
  - `format_favorite(fav, default_origin, stats=None)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_results_history.py`:

```python
from results.history import alert_text, history_screen, options_line, versus_average

FAV = {"id": 7, "origin": "LPA", "hub": "MAD", "destination": "NRT", "trip_days": 14,
       "adults": 2, "children": 0, "cabin": "BUSINESS", "currency": "EUR",
       "max_stops": 1, "min_layover": None, "overnight": 1,
       "check_dates": '["2026-10-01", "2026-10-03", "2026-10-08", "2026-10-10", "2026-10-12"]'}


def test_options_line_names_everything_that_changes_the_price():
    assert options_line(FAV) == "2 adults · Business · EUR · 🌙 · ≤1 stop"
    plain = {"adults": 1, "currency": "USD"}
    assert options_line(plain) == "1 adult · Economy · USD"


def test_versus_average():
    s = price_stats([_c("2026-10-20", 800), _c("2026-10-30", 704)], today=TODAY)  # avg 752
    assert versus_average(s) == "6% below its 30-day average"
    up = price_stats([_c("2026-10-20", 500), _c("2026-10-30", 600)], today=TODAY)
    assert versus_average(up) == "9% above its 30-day average"
    assert versus_average(price_stats([_c("2026-10-30", 600)], today=TODAY)) is None
    assert versus_average(None) is None


def test_the_history_screen():
    checks = [_c("2026-10-02", 790), _c("2026-10-19", 636), _c("2026-10-30", 612)]
    text, rows = history_screen(FAV, price_stats(checks, today=TODAY), origin="LPA")
    assert "LPA → MAD → NRT" in text and "round-trip 14d" in text
    assert "2 adults · Business · EUR · 🌙 · ≤1 stop" in text
    assert "Tracking 1 Oct, 3 Oct, 8 Oct (+2)" in text
    assert "Last 612 EUR (−24 since the previous check)" in text
    assert "Lowest 612 · Highest 790" in text
    assert "3 checks since 2 Oct" in text
    assert [b.data for row in rows for b in row] == ["delfav_7", "menu_favorites"]


def test_the_history_screen_before_any_check(monkeypatch):
    """Review Focus #4 (the never-checked half)."""
    monkeypatch.setattr(config, "ALERT_INTERVAL_HOURS", 6)
    text, _ = history_screen(FAV, None, origin="LPA")
    assert "Not checked yet — the first check runs within 6 hours." in text


def test_hostile_codes_are_escaped():
    """Review Focus #5."""
    bad = {**FAV, "hub": "<b>", "destination": "&x"}
    text, _ = history_screen(bad, None, origin="LPA")
    assert "<b>" not in text.replace("<b>LPA", "") and "&lt;b&gt;" in text
    msg = alert_text(bad, origin="LPA", last=612, date="2026-10-01", record_before=700,
                     record_drop=True, trend=None, spark="▃▁")
    assert "&lt;b&gt;" in msg and "&amp;x" in msg


def test_alert_text_for_each_trigger():
    """Review Focus #2 and #3."""
    from results.history import TrendSignal
    trend = TrendSignal(days=30, average=748, pct_below=18)
    both = alert_text(FAV, origin="LPA", last=612, date="2026-10-01", record_before=700,
                      record_drop=True, trend=trend, spark="▃▄▂▁")
    assert both.startswith("📉 Price drop · LPA → MAD → NRT · 1 Oct")
    assert "612 EUR — was 700 (−12%), lowest in 30 days, 18% below the average (748)" in both
    assert "2 adults · Business · EUR · 🌙 · ≤1 stop" in both and "▃▄▂▁" in both
    same_day = TrendSignal(days=0, average=748, pct_below=18)
    only_trend = alert_text(FAV, origin="LPA", last=612, date="2026-10-01", record_before=None,
                            record_drop=False, trend=same_day, spark="")
    assert "612 EUR — lowest so far, 18% below the average (748)" in only_trend
```

Append to `tests/test_handlers_favorites.py`:

```python
def test_the_favourite_line_shows_currency_options_and_average():
    from datetime import date

    from handlers.utils import format_favorite
    from results.history import price_stats

    fav = {"id": 1, "origin": "LPA", "hub": "MAD", "destination": "NRT", "trip_days": 0,
           "adults": 2, "cabin": "BUSINESS", "currency": "USD", "record_price": 700.0,
           "last_price": 704.0, "last_checked": "2026-10-30T08:00:00Z",
           "check_dates": '["2026-11-01"]'}
    stats = price_stats([("2026-10-20T08:00:00Z", 800.0), ("2026-10-30T08:00:00Z", 704.0)],
                        today=date(2026, 10, 31))
    text = format_favorite(fav, "LPA", stats)
    assert "2 adults · Business · USD" in text
    assert "6% below its 30-day average" in text
    assert "6% below" not in format_favorite(fav, "LPA")      # no stats, no line
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_history.py tests/test_handlers_favorites.py -q`
Expected: an `ImportError` for the new names, and `format_favorite` rejects the third argument.

- [ ] **Step 3: Implement**

Append to `results/history.py` (add the imports at the top: `from handlers.search.draft import Button, Rows`, `from handlers.utils import esc, load_json_list`, `from providers.base import SearchOptions`):

```python
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _route(fav: dict, origin: str) -> str:
    return (f"{esc(fav.get('origin') or origin)} → {esc(fav['hub'])} → "
            f"{esc(fav['destination'])}")


def options_line(fav: dict) -> str:
    """Everything that changes the price besides the route, in one line."""
    opts = SearchOptions.from_mapping(fav)
    parts = [opts.party_label(), opts.currency]
    if opts.overnight:
        parts.append("🌙")
    if opts.max_stops is not None:
        parts.append("direct" if opts.max_stops == 0
                     else f"≤{opts.max_stops} stop{'s' if opts.max_stops > 1 else ''}")
    return " · ".join(parts)


def versus_average(stats: PriceStats | None) -> str | None:
    if stats is None or stats.count < 2 or stats.average <= 0:
        return None
    pct = round((stats.last - stats.average) / stats.average * 100)
    days = config.PRICE_HISTORY_DAYS
    if pct == 0:
        return f"at its {days}-day average"
    return f"{abs(pct)}% {'below' if pct < 0 else 'above'} its {days}-day average"


def history_screen(fav: dict, stats: PriceStats | None, *, origin: str) -> tuple[str, Rows]:
    trip = f"round-trip {fav['trip_days']}d" if fav.get("trip_days") else "one-way"
    dates = [str(d) for d in load_json_list(fav.get("check_dates"))]
    tracking = ", ".join(_day(d) for d in dates[:3]) + (f" (+{len(dates) - 3})" if len(dates) > 3 else "")
    cur = esc(SearchOptions.from_mapping(fav).currency)
    lines = [f"<b>{_route(fav, origin)}</b> · {trip}", esc(options_line(fav)),
             f"Tracking {tracking}" if tracking else "Tracking —", ""]
    if stats is None:
        lines.append(f"Not checked yet — the first check runs within "
                     f"{config.ALERT_INTERVAL_HOURS} hours.")
    else:
        lines.append(stats.sparkline)
        last = f"Last {stats.last:,.0f} {cur}"
        if stats.previous is not None and stats.previous != stats.last:
            delta = stats.last - stats.previous
            last += f" ({'−' if delta < 0 else '+'}{abs(delta):,.0f} since the previous check)"
        lines.append(last)
        lines.append(f"Lowest {stats.low:,.0f} · Highest {stats.high:,.0f} · "
                     f"{config.PRICE_HISTORY_DAYS}-day average {stats.average:,.0f}")
        if stats.lowest_in_days:
            lines.append(f"Lowest in {stats.lowest_in_days} days")
        lines.append(f"{stats.count} check{'s' if stats.count != 1 else ''} since "
                     f"{_day(stats.first_day)}")
    rows: Rows = [[Button("🗑 Delete", f"delfav_{fav['id']}"),
                   Button("◀ Favourites", "menu_favorites")]]
    return "\n".join(lines), rows


def alert_text(fav: dict, *, origin: str, last: float, date: str,
               record_before: float | None, record_drop: bool,
               trend: TrendSignal | None, spark: str) -> str:
    """One alert naming every reason that fired (record drop, trend, or both)."""
    cur = esc(SearchOptions.from_mapping(fav).currency)
    reasons = []
    if record_drop and record_before:
        pct = int((record_before - last) / record_before * 100)
        reasons.append(f"was {record_before:,.0f} (−{pct}%)")
    if trend is not None:
        since = f"lowest in {trend.days} days" if trend.days >= 1 else "lowest so far"
        reasons.append(f"{since}, {trend.pct_below}% below the average ({trend.average:,.0f})")
    lines = [f"📉 Price drop · {_route(fav, origin)} · {_day(date)}",
             esc(options_line(fav)),
             f"{last:,.0f} {cur} — {', '.join(reasons)}"]
    if spark:
        lines.append(spark)
    return "\n".join(lines)
```

The price line has no markup so it reads cleanly in a notification preview.

`handlers/utils.py`, `format_favorite(fav: dict, default_origin: str, stats=None) -> str`:
- add, as a new second line of the block, `f"  {esc(options_line(fav))}"`;
- after the Record/Last line, when `versus_average(stats)` is not `None`, add `f"  {versus_average(stats)}"`;
- import `options_line` and `versus_average` inside the function (`from results.history import ...`), because `results.history` imports `handlers.utils` at module level and a top-level import would be circular.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: history screen, favourite line and alert text"
```

---

### Task 3: Wire it up: the favourites list, the history screen, the alerts

**Files:**
- Modify: `handlers/favorites.py` (list buttons and stats; `favorite_history`; handler registration), `scheduler.py`
- Test: `tests/test_handlers_favorites.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: everything from Tasks 1–2; `handlers.anchor.markup`; `db.get_price_checks`, `db.get_favorite`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_handlers_favorites.py`:

```python
class _Query:
    def __init__(self, data):
        self.data = data
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def _upd(data):
    return SimpleNamespace(callback_query=_Query(data), effective_user=SimpleNamespace(id=_OWNER_ID))


async def test_the_list_offers_history_per_favourite(temp_db, monkeypatch):
    from handlers.favorites import favorites_menu

    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    fav = await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                       currency="EUR", price=700.0, check_dates=["2026-10-01"])
    update = _upd("menu_favorites")
    await favorites_menu(update, None)
    text, kw = update.callback_query.edits[-1]
    data = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert f"fh:{fav}" in data and f"delfav_{fav}" in data
    assert "1 adult · Economy · EUR" in text


async def test_the_history_screen_and_a_deleted_favourite(temp_db, monkeypatch):
    """Review Focus #4 (the deleted half)."""
    from handlers.favorites import favorite_history

    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    fav = await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                       currency="EUR", price=700.0, check_dates=["2026-10-01"])
    for price in (800.0, 700.0):
        await db_module.add_price_check(fav, price, None)

    update = _upd(f"fh:{fav}")
    await favorite_history(update, None)
    assert "Last 700 EUR (−100 since the previous check)" in update.callback_query.edits[-1][0]

    gone = _upd(f"fh:{fav + 50}")
    await favorite_history(gone, None)
    assert gone.callback_query.edits[-1][0] == "That route is no longer tracked."
```

Append to `tests/test_scheduler.py`:

```python
class MarkupBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.markups = []

    async def send_message(self, chat_id, text, **kwargs):
        await super().send_message(chat_id, text, **kwargs)
        self.markups.append(kwargs.get("reply_markup"))


async def test_the_first_check_sets_a_missing_record_without_alerting(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(discount="0", dom_price="50", onward_price="300")]
    await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                 currency="EUR", price=None, check_dates=["2026-09-01"])
    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)
    assert bot.messages == []
    assert (await db_module.get_favorites())[0]["record_price"] == pytest.approx(350.0)


async def _favourite_with_history(prices, record=700.0):
    fav = await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                       currency="EUR", price=record, check_dates=["2026-09-01"])
    for p in prices:
        await db_module.add_price_check(fav, p, None)
    return fav


async def test_a_new_low_against_the_trend_alerts_and_keeps_the_record(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(discount="0", dom_price="100", onward_price="600")]
    fav = await _favourite_with_history((800, 790, 810, 800, 805))
    bot = MarkupBot()

    await check_favorites(bot, owner_chat_id=1)

    assert len(bot.messages) == 1
    assert "lowest" in bot.messages[0] and "below the average (801)" in bot.messages[0]
    assert bot.markups[0].inline_keyboard[0][0].callback_data == f"fh:{fav}"
    assert (await db_module.get_favorites())[0]["record_price"] == pytest.approx(700.0)


async def test_no_trend_alert_without_enough_history(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(discount="0", dom_price="100", onward_price="600")]
    await _favourite_with_history((800, 790, 810, 800))
    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)
    assert bot.messages == []


async def test_both_triggers_send_one_alert_naming_both(temp_db, fake_engine):
    """Review Focus #3."""
    fake_engine["state"]["itineraries"] = [_itin(discount="0", dom_price="100", onward_price="500")]
    await _favourite_with_history((800, 790, 810, 800, 805), record=700.0)
    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)
    assert len(bot.messages) == 1
    assert "was 700" in bot.messages[0] and "below the average" in bot.messages[0]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_handlers_favorites.py tests/test_scheduler.py -q`
Expected: FAIL. There is no `favorite_history` and no `fh:` button, the record stays `NULL`, and there are no trend alerts.

- [ ] **Step 3: Implement**

`handlers/favorites.py`:
- `_render_favorites`: for each favourite compute `stats = price_stats(await get_price_checks(fav["id"]), today=date.today())`, call `format_favorite(fav, ORIGIN, stats)`, and make its button row:

  ```python
          buttons.append([
              InlineKeyboardButton("📈 History", callback_data=f"fh:{fav['id']}"),
              InlineKeyboardButton(f"Delete {fav['hub']}->{fav['destination']}",
                                   callback_data=f"delfav_{fav['id']}"),
          ])
  ```

- New handler:

  ```python
  @owner_only_callback
  async def favorite_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
      """One favourite's price history: sparkline, statistics, delete."""
      query = update.callback_query
      await query.answer()
      fav = await get_favorite(int(query.data.split(":")[1]))
      if fav is None:
          await query.edit_message_text("That route is no longer tracked.",
                                        reply_markup=MAIN_MENU_KEYBOARD)
          return
      stats = price_stats(await get_price_checks(fav["id"]), today=date.today())
      text, rows = history_screen(fav, stats, origin=ORIGIN)
      await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup(rows))
  ```

- Register `CallbackQueryHandler(favorite_history, pattern=r"^fh:\d+$")` in `get_favorites_handlers`.
- Imports: `from datetime import date`, `get_favorite, get_price_checks` from `db`, `from handlers.anchor import markup`, `from results.history import history_screen, price_stats`.

`scheduler.py`, replacing everything from `# Save price check record` to the end of the per-favourite loop body:

```python
        prior = await get_price_checks(fav_id)
        await add_price_check(fav_id, best_price, best_detail)

        if record_price is None:
            # A favourite saved without a price: its first check is its record.
            await update_favorite_price(fav_id, best_price, is_record=True)
            logger.info("Favorite %d: first check sets the record at %.2f %s",
                        fav_id, best_price, currency)
            continue

        record_drop = best_price < record_price * (1 - PRICE_DROP_THRESHOLD)
        trend = trend_signal(prior, best_price, today=date.today())
        if record_drop or trend is not None:
            prices = [p for _, p in prior if p is not None] + [best_price]
            text = alert_text(
                fav, origin=origin, last=best_price, date=best.date,
                record_before=record_price, record_drop=record_drop, trend=trend,
                spark=sparkline(prices[-SPARK_POINTS:]),
            )
            try:
                await bot.send_message(
                    chat_id=owner_chat_id, text=text, parse_mode="HTML",
                    reply_markup=markup([[Button("📈 History", f"fh:{fav_id}")]]),
                )
            except Exception:
                logger.exception("Failed to send price alert for favorite %d", fav_id)
            logger.info("Favorite %d: alert (record_drop=%s, trend=%s) at %.2f %s",
                        fav_id, record_drop, trend is not None, best_price, currency)

        # Only a record drop moves the record: it means "the best price we
        # alerted on", and a trend alert is about the recent average instead.
        await update_favorite_price(fav_id, best_price, is_record=record_drop)
```

Imports: `get_price_checks` from `db`, `from config import SPARK_POINTS` (beside the existing config import), `from handlers.anchor import markup`, `from handlers.search.draft import Button`, and `from results.history import alert_text, sparkline, trend_signal`. Keep the existing `test_genuine_price_drop_alerts_and_updates_the_record` passing: its message must still contain "Price drop" and "350", which `alert_text`'s header and price line provide.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: price history screen and trend-aware alerts

A favourite saved without a price now sets its record on the first
check instead of never alerting."
```

---

### Task 4: README

- [ ] **Step 1: Update Features**

In `README.md`, replace the "Price tracking" bullet with:

```markdown
- **Price tracking** — track any result, its exact dates or its route across
  the whole window. A scheduler re-prices it every few hours and keeps the
  history: each favourite shows a sparkline, its lowest, highest and 30-day
  average, and how today compares. Alerts fire on a drop below the record or
  on a new low well under the recent average, and say which — with a button
  to the full history.
```

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: price history and trend alerts"
```
