# Layer 3c — search options

Date: 2026-09-26. Branch: `feat/search-options` (from `main` @ `d45f8d2`).
Spec: [multi-provider search design](2026-08-29-multi-provider-search-design.md) §7
Binding notes: [layer 3a carry-forward](2026-09-01-layer-3a-carry-forward.md), "What 3c inherits" · [layer 3a design](2026-08-31-layer-3a-builder-design.md) §8 · [layer 3b design](2026-09-26-layer-3b-results-design.md)

Every search today is 1 adult, Economy, EUR, with no limits. The draft shows
that as a read-only footer. 3c makes it editable: passengers (adults and
children), cabin, currency, and two search-time limits (maximum stops,
minimum layover). It is the one stage of Layer 3 that changes an engine
signature.

## What already exists

- `LegQuery` and `CalendarQuery` already carry `adults`, `children`, `cabin`
  and `currency`. `LegQuery` also carries `max_stops` and `min_layover`.
- Kiwi sends all of them. Google sends adults and currency, applies
  `max_stops` client-side, and raises a bare `ProviderError` for children,
  a cabin other than Economy, and `min_layover`.
- `favorites` already has `cabin`, `children`, `max_stops` and `min_layover`
  columns. They are written with defaults and never replayed.
- §7.2 is done: `scheduler.py` already prices through `run_search`. What is
  left is the carry-forward's grid end-date problem (§6).

## Verified against the live APIs (2026-09-26)

| Question | Kiwi | Google |
|---|---|---|
| Price for 2 adults vs 1 | **total** (32 → 62 EUR; 2 adults + 1 child → 86) | **per person** (30 → 30) |
| Calendar price for 2 adults | **total** (32 → 62) | no calendar |
| Cabin enum (`CabinClassType`) | `ECONOMY`, `PREMIUM_ECONOMY`, `BUSINESS`, `FIRST_CLASS` | Economy only |

The price difference is the reason for §3's normalisation rule. Without it,
a two-adult search would compare a Kiwi total against a Google per-person
price in the cross-check. The same mismatch would reach the savings line
whenever a through-fare came from the other provider.

## 1. `SearchOptions`

```python
@dataclass(frozen=True)
class SearchOptions:
    adults: int = 1
    children: int = 0
    cabin: str = "ECONOMY"          # a CabinClassType value
    currency: str = "EUR"
    max_stops: int | None = None    # per leg; None = no limit
    min_layover: int | None = None  # minutes, inside a ticket; None = no minimum
```

It lives in `providers/base.py` beside `LegQuery`, which it feeds. It has two
methods, `leg_query(origin, dest, date)` and `calendar_query(origin, dest,
start, end)`, so every construction site builds its query from the options
instead of copying six fields by hand. The defaults equal today's behaviour,
so a caller passing nothing gets exactly today's search.

`min_layover` is a connection *inside* one ticket (the provider's stopover).
It is not the self-transfer gap between the two tickets, which 3b's filters
already cover.

**The engine signature.** `run_search`, `_run_two_stage`, `_run_grid`,
`_cross_check`, `scan_calendars`, `confirm`, `through_fares` and
`run_grid_search` all replace their `adults=` / `currency=` parameters with
`options: SearchOptions`. That covers every `LegQuery` and `CalendarQuery`
construction site: grid ×4, drill ×3, scan ×4. No site keeps a bare
`adults=` argument that could drift from the options.

## 2. What a provider can do

```python
@dataclass(frozen=True)
class Capabilities:
    cabins: frozenset[str]
    children: bool
    min_layover: bool

    def rejects(self, options: SearchOptions) -> str | None:
        """A human reason this provider cannot run *options*, or None."""
```

Each provider has a `capabilities` attribute:

- **Kiwi:** all four cabins, children, min_layover.
- **Google:** `{"ECONOMY"}`, no children, no min_layover.

There are two lines of defence:

1. **The builder offers only what the primary provider can do.** On a
   Google-only deployment the cabin row shows Economy alone, and the
   children and layover rows are absent.
2. **`run_and_report` checks before searching.** A history rerun or a
   favourite saved under another deployment can still carry an option the
   current provider cannot run. `primary.capabilities.rejects(options)` is
   called before `run_search`. A reason ends the search with "Your flight
   source can't search Business class." — never "No routes found." As a
   last resort, a bare `ProviderError` escaping the engine gets that same
   wording rather than "check the logs".

The cross-check already catches a secondary's `ProviderError` and keeps the
primary-only result (review finding I8). With options set, a Google secondary
behind a Kiwi primary quietly stops corroborating Business searches, which is
correct.

## 3. Price is always for the whole party

**Rule:** `Offer.price` and `RatedPrice.price` are the total for every
passenger in the query. Kiwi already returns that. The Google adapter
multiplies each offer's price by `query.adults` (Google cannot carry
children, so adults is the whole party). `checked_bag_price` is a price
per bag, not per party, and is not multiplied.

Because the rule holds at the provider boundary, the engine, the discount
arithmetic (a percentage, so it scales), the through-fare comparison and
the scheduler's drop threshold need no change.

The results summary and detail label prices "total · 2 adults, 1 child"
whenever the party is not one adult, so a total is never read as a
per-person fare.

## 4. The builder

The read-only footer becomes a draft row:

```
✏️ Options   2 adults, 1 child · Business · USD · ≤1 stop
```

It opens an options screen. Each tap edits the draft and re-renders, as
every other screen does:

```
Passengers
  Adults    [−] 2 [+]
  Children  [−] 1 [+]
Cabin       [Economy] [• Business] [Premium] [First]
Currency    [EUR] [• USD] [GBP]
Max stops   [• Any] [Direct] [≤1] [≤2]
Min layover [• Any] [1h] [2h] [3h]
[⬅️ Back]
```

- Adults range 1–9, children 0–8, with at most 9 passengers in total
  (Kiwi's limit). A `+` that would break a bound answers with an alert and
  does not change the draft.
- Rows the primary provider cannot use are not drawn (§2).
- The query estimate is unchanged. Options do not change the request count.

`SearchDraft` gains the options fields. `to_params()` adds `children`,
`cabin`, `max_stops` and `min_layover` beside the existing `adults` and
`currency`. The callback prefix is `o:` (`o:a:+`, `o:c:BUSINESS`,
`o:cur:USD`, `o:s:1`, `o:l:120`), registered in the builder's single
`BUILDING` state like every other screen.

## 5. Persistence and replay

- **`searches`** gains `children`, `cabin`, `max_stops` and `min_layover`
  through `MIGRATIONS`. `NULL` reads as today's default. `run_and_report`
  writes them.
- **History rerun** replays all six options. Previously it replayed only
  `adults` and `currency`.
- **Favourites.** Both 3b track actions and the legacy `savefav_` pass the
  stored search's options to `add_favorite`, which already has the columns.
- **Scheduler** replays them: it builds `SearchOptions` from the favourite
  row (with `NULL` as the default) and checks the provider's capabilities
  first. A favourite whose options the current provider rejects is logged
  and skipped, never re-priced under a different query shape.

This closes the carry-forward item: a replayed favourite always compares
like with like.

## 6. The scheduler's exact dates

The scheduler passes `dates=all_dates` to `run_search`. On a grid (Google)
deployment the engine then searches the tracked dates themselves instead of
resampling the window, so the final date can no longer be dropped. The
two-stage strategy ignores `dates`, and its behaviour is unchanged.

## 7. Clean shutdown

`post_shutdown` cancels `bot_data["scheduler_task"]` and awaits it. This
removes the `Task was destroyed but it is pending!` error that every restart
currently logs, one per deploy now that deploys are automatic.

## 8. Display

- The summary header gains the party and cabin when they are not the
  defaults: `LPA → NRT · round-trip · 2 adults, 1 child · Business · 34 routes`.
- The detail's price line reads `total for 3 passengers` when the party is
  larger than one.
- `SearchMeta` reads the new columns, with `NULL` as the default.

## 9. Testing

| File | Covers |
|---|---|
| `test_providers_base.py` | `SearchOptions` query builders; `Capabilities.rejects` for each option and its wording |
| `test_google_provider.py` | price × adults; bag price untouched; `capabilities` |
| `test_kiwi_provider.py` | `capabilities`; options reach the query variables |
| `test_engine_*` | options reach every `LegQuery`/`CalendarQuery` (scan, drill, grid, through-fares, cross-check); a Business search's secondary `ProviderError` keeps the primary result |
| `test_search_draft.py` | the options row and screen; bounds; `to_params` |
| `test_search_builder.py` | `o:` callbacks; rows hidden for a Google primary |
| `test_results_run.py` | a rejected option ends with the capability message and saves nothing; options are persisted |
| `test_history` / favourites / scheduler | rerun, both track paths and the scheduler replay all options; scheduler passes `dates`; a rejected favourite is skipped |
| `test_db.py` | the four migrations |
| `test_bot.py` (new) | `post_shutdown` cancels the scheduler task |
| `test_kiwi_schema.py` (network) | `CabinClassType` still has the four values |

## 10. Writing order

1. `SearchOptions` and `Capabilities` in `providers/base.py`; both providers'
   `capabilities`; Google's price normalisation.
2. The engine takes `options` end to end. Behaviour is unchanged for the
   defaults, which the existing suite proves.
3. `searches` migrations; `run_and_report` writes and checks the options;
   history rerun replays them.
4. Favourites and scheduler replay, plus the scheduler's exact dates.
5. The draft fields, the options row and screen, and the `o:` callbacks.
6. The display (§8).
7. Clean shutdown.
8. README: Features and the "Notes on some decisions" price rule.

## 11. Done when

- A Kiwi deployment can search 2 adults + 1 child in Business, paid in USD,
  direct only. The prices shown are totals, and history, favourites and the
  scheduler all replay that same query.
- A Google-only deployment offers only what Google can do. A stored Business
  search rerun there says why it can't run, instead of "No routes found".
- A deploy restart logs no `Task was destroyed` error.
- Every 3c item in the 3a design §8 and the 3a carry-forward is closed.
- Full suite green; `ruff check .` clean.
