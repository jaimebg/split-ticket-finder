# Layer 2 — carried forward

Date: 2026-08-31. Branch: `feat/two-stage-search`.
Spec: [multi-provider search design](2026-08-29-multi-provider-search-design.md) §5, §7 · Plan: [two-stage engine](2026-08-30-two-stage-engine-plan.md)
Layer 1's notes: [layer 1 carry-forward](2026-08-29-layer-1-carry-forward.md) — still binding.

Layer 2 (the two-stage engine) is complete and reviewed. This records what it
deliberately left undone and the decisions that bind Layer 3, so the next layer
starts from the record rather than rediscovering it.

## Decisions that bind Layer 3

- **Negative savings are a formatter concern, never an engine one.** `savings` goes negative
  when the airline's through-fare beats the split. Filtering or re-ranking in the engine was
  ruled out twice: it would make "every itinerary loses to the through-fare" indistinguishable
  from "no flights found", hiding real bookable answers behind the signal this system reserves
  for nothing-exists. Re-ranking is separately foreclosed — through-fares are priced *after*
  sorting. **`savings_pct` is persisted unguarded**, so a card renderer reading the stored field
  without checking the sign will print "You save -18%". The guard lives in the formatter, not
  the data.

- **`Itinerary.status` has three states and only two are reachable today.** `STATUS_PARTIAL`
  cannot arise from the engine (both `confirm` and `run_grid_search` drop incomplete
  candidates), and the current formatter collapses `partial` and `estimate` into one line —
  precisely the understating the three-state signal was added to prevent. Layer 3's result
  cards should distinguish them.

- **Empty means "no results"; an exception means "broken".** This law is now enforced at every
  engine boundary, and a total search failure has its own message ("Search incomplete") rather
  than a headline plus a caveat. A test asserts "No routes found" is *absent* in that case —
  do not weaken it.

- **A bare `ProviderError` aborts a phase; per-leg errors are counted.** Google raises the base
  class when it cannot express a query at all (children, non-economy cabin, `min_layover`), so
  swallowing it would report a misconfigured search as "no flights". `_cross_check` is the one
  bounded exception: a secondary-provider failure there is caught, because discarding a
  complete primary result for optional corroboration is worse.

- **Do not set `min_layover`, `children` or a non-`ECONOMY` `cabin` on a `LegQuery`** unless the
  user asked. The moment Layer 3's cabin selector does, a Google-only deployment starts
  aborting phases. Route to Kiwi or handle `ProviderError`.

- **Progress labels are unique but not user-facing prose.** `"Phase 1 (cross-check)"` arrives
  after `"Phase 2"` completes, which reads as going backwards. The engine's job was uniqueness;
  Layer 3 owns the wording and should rename them for humans.

- **`SearchResult.strategy` is read by nothing.** §5.6 requires telling the user a grid search
  *samples* rather than covers the window. That is not delivered to any user yet.

## Known gaps

- **The scheduler's grid path can still drop a window's end date.** It holds a window, not a
  discrete list, so it cannot pass `explicit_dates`; `engine/grid.py`'s window sampler may miss
  the final date. Strictly better than before Layer 2 (which truncated to 5 sampled points),
  but the same shape as the bug fixed for the guided flow. Fixing it means changing what the
  scheduler stores.

- **`favorites.cabin`, `children`, `max_stops`, `min_layover` are stored and never replayed.**
  Harmless while nothing sets non-defaults. The moment Layer 3 adds a cabin selector, replay
  them or the scheduler compares two different queries and reports the difference as a price
  movement — the `e83a4d3` bug class.

- **`handlers/search_flow.py` has no tests.** It is the single integration point between engine
  and bot, and four of the whole-branch review's findings lived in it. Layer 3 rewrites this
  file — write tests as it goes.

- **Unescaped interpolations are safe only by luck.** `origin`, `hub`, `dest` and `currency` go
  into HTML unescaped; they are validated IATA codes today. §6.3's place picker feeds free text
  into `origin`. `esc()` now escapes quotes — use it.

- **`Progress.best_total` is declared, forwarded, and never populated.** Either set it (the
  orchestrator knows the running best) or drop it.

- **`README` still advertises per-leg airlines, stops and durations**, which the current
  renderer dropped. Layer 3 rebuilds result cards — restore the data or fix the claim.

## Measured cost, for reference

8 hubs × 3 destinations, 91-day window, single provider:

| | one-way | round-trip |
|---|---|---|
| phase 0 calendars | 32 | 64 |
| phase 1 confirm | 58 | 120 |
| phase 2 through-fare | 3 | 6 |
| **total** | **93** | **190** |

Only the 3 cheapest distinct dates get a through-fare baseline, so most itineraries carry no
savings figure. That is `THROUGH_FARE_DATES`, not a bug.
