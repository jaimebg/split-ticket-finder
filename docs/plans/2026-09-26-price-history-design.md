# Price history for tracked routes

Date: 2026-09-26. Branch: `feat/price-history` (from `main` @ `f0f70f5`).

Every scheduler check is already stored in `price_checks` (`favorite_id`,
`checked_at`, `best_price`, `route_detail`), but nothing ever reads it back.
The favourites list shows only the record and the last price. The alert knows
only one rule: at least 10% below the record. This subproject turns the stored
history into something to look at, and makes the alerts say why a price
matters.

## 1. Statistics (`results/history.py`, pure)

```python
@dataclass(frozen=True)
class PriceStats:
    count: int
    last: float
    previous: float | None       # the check before the last
    low: float
    high: float
    average: float               # over the last PRICE_HISTORY_DAYS
    lowest_in_days: int | None   # the last price is the lowest in this many days; None if not
    sparkline: str               # the last SPARK_POINTS prices, ▁▂▃▄▅▆▇█

def price_stats(checks: list[tuple[str, float]], *, today: date) -> PriceStats | None
def sparkline(prices: list[float]) -> str
```

- **Input.** `checks` holds `(checked_at ISO, best_price)` pairs, oldest first.
  Checks whose price is `NULL` are skipped. No priced check at all returns
  `None`.
- **`average`** covers the checks within `PRICE_HISTORY_DAYS` (default 30) of
  `today`. If none fall in that window, it covers all checks.
- **`lowest_in_days`** is the age in days of the oldest check that the last
  price is still at or below. If the last price is the lowest in the whole
  history, it is the age of the first check. If the previous check was lower,
  it is `None`.
- **`sparkline`** maps each price linearly onto 8 bars. Every price equal
  gives a flat row of `▄`. A single point gives `▄`. An empty list gives `""`.
- **`today` is a parameter**, so the tests are not tied to the calendar.

Config: `PRICE_HISTORY_DAYS=30`, `SPARK_POINTS=20`, `ALERT_MIN_CHECKS=5`.

## 2. Storage

`db.get_price_checks(fav_id: int) -> list[tuple[str, float | None]]` returns
the checks oldest first. Its query is ordered by `checked_at` and then `id`,
because several checks can share a second.

## 3. Favourites list and the history screen

- **List line.** Each favourite's block gains:
  - the currency;
  - the trip options (`options.party_label()`, `· 🌙` when overnight, and
    `· ≤1 stop` when limited), which also closes the 3c/risk deferred minor
    that two favourites differing only in options looked identical;
  - with at least 2 checks, a line such as `12% below its 30-day average` or
    `5% above …`.
- **Buttons.** Each favourite gets **[📈 History]** (`fh:<id>`) next to
  Delete.
- **History screen** (`fh:<id>`, rendered by a pure `history_screen(fav, stats)`
  in `results/history.py`):

  ```
  LPA → MAD → NRT · round-trip 14d
  2 adults · Business · EUR · 🌙
  Tracking 1 Oct, 3 Oct, 8 Oct (+4)

  ▃▄▄▂▁▁▃▅▄▂▁
  Last 612 EUR (−24 since the previous check)
  Lowest 598 · Highest 790 · 30-day average 701
  Lowest in 12 days
  34 checks since 2 Sep

  [🗑 Delete] [◀ Favourites]
  ```

  With no checks yet it says "Not checked yet — the first check runs within
  N hours." A deleted favourite answers "That route is no longer tracked."

## 4. Smarter alerts

**Triggers.** One check sends at most one alert. It is sent when either of
these holds:

1. **Record drop** (today's rule): `last < record × (1 − PRICE_DROP_THRESHOLD)`.
2. **Low against the trend** (new): at least `ALERT_MIN_CHECKS` earlier checks,
   **and** the last price is the lowest in `PRICE_HISTORY_DAYS`, **and**
   `last ≤ average × (1 − PRICE_DROP_THRESHOLD)`. The average here is computed
   over the earlier checks only, so the new low doesn't drag down its own
   baseline. It fires on the crossing only: if the previous check already
   met the condition, a further drop is not re-alerted.

A record drop also updates the record, as today. A trend alert does not change
the record, and neither does anything else, so the record keeps meaning "the
best price we alerted on".

**The record fix.** A favourite whose `record_price` is `NULL` sets its record
from its first check and sends no alert. Today such a favourite never alerts,
ever.

**Text.** Plain HTML, escaped:

```
📉 LPA → MAD → NRT · 1 Oct
2 adults · Business · 🌙
612 EUR — lowest in 30 days, 18% below the average (748)
▃▄▄▂▁▁▃▅▄▂▁
```

The reason line names the trigger that fired:

- **record drop:** `was 700 (−13%)`
- **trend:** `lowest in N days, X% below the average (Y)`
- **both:** the record wording followed by the trend wording.

The message carries an inline **[📈 History]** button (`fh:<id>`). The
scheduler builds it with `handlers.anchor.markup`.

## 5. Testing

| File | Covers |
|---|---|
| `test_results_history.py` | `sparkline` (empty, one point, flat, a ramp); `price_stats` (NULL skipped, window average with fallback, `lowest_in_days` at the whole history / partial / `None`, `previous`); `history_screen` (full, no checks, HTML-hostile route codes) |
| `test_db.py` | `get_price_checks` order, including a tie on `checked_at` |
| `test_scheduler.py` | the record fix; the trend trigger fires only with ≥ `ALERT_MIN_CHECKS` and both conditions; the average excludes the new check; one alert when both triggers hold; alert text names the reason; the history button's callback data |
| `test_handlers_favorites.py` | the list line (currency, options, vs-average); `fh:` renders the screen; `fh:` for a deleted favourite |

## 6. Done when

- Every favourite shows its currency, options and position against its
  average, and opens a history screen with a mini chart and statistics.
- An alert says why a price matters, and links to the history.
- A favourite saved without a price starts alerting after its first check.
- Full suite green; `ruff check .` clean.
