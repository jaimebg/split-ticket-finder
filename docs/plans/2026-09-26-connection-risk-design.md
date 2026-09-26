# Connection risk — safe pairing, risk levels, a night at the hub

Date: 2026-09-26. Branch: `feat/connection-risk` (from `main` @ `127644a`).
Builds on: [layer 3b design](2026-09-26-layer-3b-results-design.md) §5 (self-transfer buffer) · [layer 3c design](2026-09-26-layer-3c-options-design.md) (`SearchOptions`)

A split ticket is two contracts. If the first flight is late and you miss the
second, the second airline owes you nothing. That risk is the real price of
the saving, and today the bot both creates it and hides it:

- **It creates it.** `confirm` and the grid pick the cheapest offer for each
  ticket on its own (`engine/drill.py`, `engine/grid.py`), with no look at the
  times. The 3b review found impossible pairings are common. The second
  ticket often leaves before the first lands, because the cheapest flight of
  the day is paired with the cheapest flight of the day.
- **It hides it.** Since 3b the detail view shows the time between tickets,
  but nothing ranks by it, flags it in the summary, or lets you filter on it.

This subproject changes three things: which flights are paired, how risk is
shown, and a new "night at the hub" search option.

## 1. Risk levels

`engine/risk.py` is a pure module. The engine uses it to pair flights and the
view uses it to label them, so the two can't disagree.

```python
class Risk(IntEnum):          # lower is better; the order is the pairing preference
    LOW = 0
    MEDIUM = 1
    UNKNOWN = 2
    HIGH = 3
    IMPOSSIBLE = 4

def connection_risk(arriving: Offer | None, departing: Offer | None) -> Risk | None
def itinerary_risk(itin: Itinerary) -> tuple[Risk, list[str]] | None
```

For one connection (the arriving ticket's last segment, then the departing
ticket's first):

| Risk | When |
|---|---|
| `IMPOSSIBLE` | the gap is negative: the second ticket leaves before the first lands |
| `HIGH` | gap < `RISK_HIGH_BELOW_HOURS` (default 2), **or** an airport change (the first ticket lands at one airport, the second leaves from another) |
| `MEDIUM` | gap < `RISK_MEDIUM_BELOW_HOURS` (default 4) |
| `LOW` | gap ≥ `RISK_MEDIUM_BELOW_HOURS`, which includes every night at the hub |
| `UNKNOWN` | a time is missing (Google sometimes doesn't report one) |
| `None` | either offer is missing (an estimate or partial result): there is no connection to judge |

The airport-change case is why this can't simply be `models.ground_time`:
that returns `None` for both an airport change and a missing time, which are
`HIGH` and `UNKNOWN` respectively.

`itinerary_risk` takes the worse of the outbound and return connections. It
returns the reasons as short lines, for example `"1h40 between tickets at
MAD"` or `"arrive MAD, depart TOJ"`. `None` means neither connection could be
judged.

Both thresholds are config knobs, validated as `0 < high < medium`.
`models._self_transfer` is renamed to `models.self_transfer` because the risk
module imports it.

## 2. Pairing: safe before cheap

`engine/pairing.py` has a single pure function:

```python
def best_pair(firsts: list[Offer], seconds: list[Offer], *,
              first_discount: Decimal = Decimal(0),
              second_discount: Decimal = Decimal(0)) -> tuple[Offer, Offer]
```

It considers every (first, second) pair. With up to 5 offers per leg that is
25 pairs, all already fetched, so this costs zero new requests. It returns
the cheapest pair **within the best risk level available**:

> `LOW` → `MEDIUM` → `UNKNOWN` → `HIGH` → `IMPOSSIBLE`.

Cheapest here means the combined price after the discount (the domestic
side's rate is passed in: `first_discount` outbound, `second_discount` on
the return), so pairing never leaves money on the table within a level.

This is the product decision, approved 2026-09-26. **A safer connection beats
a cheaper one.** A 40-minute self-transfer that saves €15 is not a saving.
The summary still orders results by total, so a safe pairing only replaces an
unsafe one *within the same route and date*, never across results.

`confirm` uses `best_pair` for the outbound pair (domestic out, onward out) and
the return pair (onward return, domestic return) independently. The grid's
combine step does the same. `cheapest()` stays for through-fares, which are a
single ticket.

## 3. A night at the hub

`SearchOptions` gains `overnight: bool = False`. When it is on:

- the domestic outbound flies **the day before** the onward flight, and
- the domestic return flies **the day after** the onward return.

**The dates the user picks are always the onward flight's dates.** That is
what `Itinerary.date` and `return_date` already mean in the summary and in
the 3b date filter. With `overnight` they stay the onward dates, and the
domestic dates move.

| Where | Change |
|---|---|
| `Candidate`, `Itinerary` | gain `overnight: bool = False` and the properties `dom_date` (`date − 1` if overnight) and `dom_return_date` (`return_date + 1` if overnight, `""` one-way) |
| `scan._build_jobs` | domestic calendar windows shift by −1 day (out) / +1 day (return); onward windows unchanged. Same request count; the window width is unchanged, so `MAX_WINDOW_DAYS` still holds |
| `rank_candidates` | reads `out_dom` at `date − 1` and `ret_dom` at `return + 1` when overnight |
| `drill.legs_for` / `confirm` | query and look up domestic legs on the shifted dates |
| `grid` | phases 1 and 1R query the shifted dates; combine looks them up there |
| through-fares | unchanged: priced on the onward dates |
| `results/store.py` | stores `overnight`; a missing key loads as `False` |

It costs no extra requests: the same queries, on different days. The
provider capabilities are unaffected, because every provider can search any
date.

**Display.**

- The detail shows the domestic flight's own date, and the line "🌙 1 night
  in Madrid before your flight — not included in the price".
- A round trip shows both nights.
- The summary row adds 🌙.
- The through-fare comparison keeps comparing like with like (the ticket
  prices). It says the night is extra rather than guessing a hotel price.

**Persistence.**

- `searches.overnight` and `favorites.overnight` are `INTEGER` columns; `NULL`
  reads as off.
- `SearchOptions.as_columns` and `from_mapping` include the field.
- `add_favorite` gains the parameter.

History, favourites and the scheduler replay it with no further change,
since they already go through `SearchOptions`.

**Builder.** The options screen gains a row `[🌙 Night at the hub: Off]`,
toggling via `o:o:1` / `o:o:0`. The draft's options line appends `· night at
hub`. The dates screen caption says "dates are the international flight's
day" when the option is on.

## 4. Display and filters

- **Summary row:** a risk marker after the status marker: `⚠️ 1h40` (HIGH),
  `⛔ impossible`, `❔ times unknown`. LOW and MEDIUM show nothing in the
  row, to keep it short. The detail always states the level.
- **Detail:** a "Connection risk: High" line with the reasons from
  `itinerary_risk`, plus a fixed reminder under any split: "Separate tickets:
  collect and re-check your bags, and the second airline won't wait if the
  first is late."
- **Filters (3b):** `Filters` gains `hide_risky: bool`, which hides `HIGH`,
  `IMPOSSIBLE` and `UNKNOWN` (unknown never passes, per 3b's rule). Its button
  row on the filters screen is `[Any risk] [Hide risky]`, with the callback key
  `r` (`r:<id>:f:r:1` / `r:<id>:f:r:any`). It counts toward the hidden line
  like any filter.

## 5. Testing

| File | Covers |
|---|---|
| `test_engine_risk.py` | every level boundary (exactly 2h, exactly 4h, negative, 0); airport change vs missing time; round trip takes the worse direction; reasons text; thresholds from config |
| `test_engine_pairing.py` | cheapest within the best level; a cheaper HIGH loses to a dearer LOW; ties; all-unknown falls back to cheapest; discount applied when comparing |
| `test_engine_drill.py` / `test_engine_grid.py` | confirm/grid use safe pairing; overnight shifts exactly the domestic legs' dates, both directions |
| `test_engine_scan.py` | overnight shifts the domestic calendar windows and `rank_candidates` lookups |
| `test_models.py` | `dom_date` / `dom_return_date` |
| `test_results_store.py` | `overnight` round trip; old v2 rows load it as `False` |
| `test_results_view.py` / `test_results_filters.py` | markers, the detail risk line, the night line, `hide_risky` |
| `test_search_options.py` / `test_search_draft.py` | the toggle and the options line |
| `test_db.py` | the two migrations; `add_favorite(overnight=)` |

## 6. Done when

- A search no longer shows an impossible connection when a possible pair was
  fetched for the same route and date.
- Every result states its connection risk, and "Hide risky" leaves only
  low- and medium-risk results, with a count of the rest.
- "Night at the hub" returns itineraries whose domestic flight is the day
  before (and the day after on the return), with the night called out,
  replayed by history, favourites and the scheduler.
- Full suite green; `ruff check .` clean.
