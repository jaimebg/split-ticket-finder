# Layer 3b — progress, results and filters

Date: 2026-09-26. Branch: `feat/results-view` (from `main` @ `e1bbcd9`).
Spec: [multi-provider search design](2026-08-29-multi-provider-search-design.md) §6.5, §6.6
Design: [layer 3a builder design](2026-08-31-layer-3a-builder-design.md) §8
Binding notes: [layer 1](2026-08-29-layer-1-carry-forward.md) · [layer 2](2026-08-31-layer-2-carry-forward.md) · [layer 3a](2026-09-01-layer-3a-carry-forward.md)

3a replaced how a search is *asked for*. 3b replaces what happens after the
user taps Search: a progress message with a Cancel button, then results the
user can page through, filter and open, all in that one message. It touches
no engine signature. Its only engine change is populating the
`Progress.best_total` field Layer 2 declared and never set.

After 3b the bot shows results as an interactive view instead of a text dump.
3c then adds passengers, cabin and currency.

## What already exists

- `run_search` accepts `cancel=` and `on_progress=`; `CancelToken` and
  `SearchCancelled` are in `models.py`, and the engine checks the token
  between every request (`engine/fetch.py`). §6.6 is wiring.
- `Itinerary` composes the real `Offer`s, so segments, flight numbers, local
  times, bags and booking links are all reachable in memory.
- **They are not reachable after storage.** `search.itineraries_to_json`
  keeps only prices and metadata, and `handlers/history.py` rebuilds every
  stored row as an estimate. That is the one data problem 3b has to solve.

## 1. Where result state lives

Every button press reads the search back from SQLite. Nothing about a
finished search lives in memory.

The reason is deployment. Every green push to `main` now restarts the bot, so
an in-memory results cache would turn the buttons of any search older than
the last deploy into dead ones. Reading from the database also makes history
the same view as a fresh search, with nothing to reconcile.

Callback data carries only the search id and the action, so it always fits
Telegram's 64-byte limit:

| Callback | Action |
|---|---|
| `r:<id>:p:<n>` | Show page *n* of the summary |
| `r:<id>:d:<i>` | Show the detail for result *i* (see below for what *i* indexes) |
| `r:<id>:f` | Open the filters screen |
| `r:<id>:f:<key>:<value>` | Set one filter, re-render the filters screen |
| `r:<id>:f:clear` | Reset every filter |
| `r:<id>:t:<i>` | Track result *i*, its exact dates |
| `r:<id>:T:<i>` | Track result *i*'s route across the whole window |
| `x:<run>` | Cancel a running search (§3) |

`<i>` is the result's index in the **stored, unfiltered** list, never a
position on screen, so a filter change between two taps cannot make a button
open a different result than the one it was drawn for.

## 2. Storage

### 2.1 Results, version 2

`searches.results` stays one JSON value but becomes an object:

```json
{"v": 2, "itineraries": [ ... ]}
```

Each itinerary keeps every field version 1 has, and adds its offers:
`dom_out`, `dom_ret`, `onward_out`, `onward_ret`, each an `Offer` dict or
`null`, with segments as nested dicts and datetimes as ISO strings. Money is
written as a string, not a float, so a round trip through storage is exact
`Decimal`, not approximately equal.

Every itinerary the engine returned is stored, not the first 25. That is at
most `SHORTLIST_SIZE` (default 30) at a few kilobytes each.

`results/store.py` owns both directions:

- `dump(itineraries) -> str`
- `load(raw) -> StoredResults`, which reads all three shapes that exist:
  - **v2** comes back as full `Itinerary` objects with their offers, and
    `status` is whatever it was when stored.
  - **v1**, a bare list of the current shape, comes back as estimates, as
    today.
  - **The pre-engine `Route` shape** also comes back as estimates. The two
    rows in the live database are this shape, and they must keep loading.

  `StoredResults.detailed` is False for the last two, which is what disables
  filters and the detail view for them (§6).

`_itinerary_from_dict` moves from `handlers/history.py` into `store.py`
unchanged, as the v1 and legacy branch.

### 2.2 The view state

One migration, through the existing `MIGRATIONS` tuple:

```
("searches", "view_json", "TEXT")
```

It holds the active filters and the last page shown, for example
`{"filters": {"max_stops": 1, "exclude": ["FR"]}, "page": 2}`. `NULL` means no
filters, page 1. It is per search, and this bot has one user, so there is no
per-viewer state to separate.

## 3. Progress and cancel

`run_and_report` gains the message it should turn into progress: the
builder's anchor, which today it edits to "On it" and abandons. From there
the same message is the progress display, then the summary.

**Progress text.** The engine's phase labels are for tests and logs. The
view maps them to steps a person can follow:

| Engine label | Shown |
|---|---|
| `Phase 0` | Scanning price calendars |
| `Phase 1` | Confirming flights |
| `Phase 2` (two-stage) | Pricing the single-ticket fare |
| `Phase 1 (cross-check)` | Cross-checking with a second source |
| `Phase 1`, `1R`, `2`, `2R` (grid) | Searching domestic / onward legs |
| `Phase 2 (through-fare)` | Pricing the single-ticket fare |

With the cross-check shown in words rather than as a phase number, it no
longer reads as going backwards. An unknown label is shown as-is, so a new
engine phase degrades to readable text rather than an error. There is no
"step 2 of 4": the handler would have to predict the orchestrator's strategy
and cross-check choice to count steps, and a wrong count is worse than none.

```
Confirming flights… 38/60
Best so far: 612 EUR
[ Cancel ]
```

**`best_total`.** The orchestrator knows its running best at two points: the
cheapest ranked candidate after phase 0b (an estimate), and the cheapest
confirmed itinerary after phase 1. `_PhaseRelabeler`, which already wraps
every tick the caller sees, gains a `best` it stamps onto each tick. The view
labels the phase 0b figure `est.` and drops the label once a confirmed best
replaces it. Before phase 0b nothing is known, so the line is absent, never a
zero.

**Throttle.** At most one edit per 3 seconds, and none when the text has not
changed, since Telegram rejects an identical edit with
`BadRequest: Message is not modified`. The final tick of each phase is
throttled like any other. Only the transition to results is always sent. A
failed progress edit is logged and ignored; `engine.fetch.report` already
guarantees a callback that raises cannot abort the search.

**Cancel.** Each run gets a short random id and a `CancelToken`, kept in
`bot_data["runs"][run_id]` for the life of the task. A running search cannot
survive a restart anyway, so memory is the right place. `x:<run>` sets the
token. The engine raises `SearchCancelled` at its next check.
`run_and_report` catches that, edits the message to "Search cancelled." with
the main menu, and saves nothing. A tap on the Cancel button of a run that
already finished, or that died in a restart, answers "That search is no
longer running."

History reruns have no anchor, so they send a fresh message and use it the
same way.

## 4. The views

All rendering is pure: `results/view.py` takes stored results, the view
state and the search row's metadata, and returns `(text, keyboard)`. No
Telegram calls, no database, so every screen is testable without a bot.

### 4.1 Summary

```
LPA → NRT · round-trip · 34 routes
Best 612 EUR · save 173 EUR (22%)
Sampled 12 of 91 days — not every day was priced.      ← grid only
2 routes hidden by filters.                             ← when filtering

1. 612 EUR   4 Sep → 18 Sep   via MAD
2. 634 EUR  11 Sep → 25 Sep   via BCN   partial
3. 648 EUR   4 Sep → 18 Sep   via LIS   est.
4. …

[1] [2] [3] [4] [5]
[◀] [1/7] [▶]
[Filters · 2] [New search] [Menu]
```

- 5 results per page. The number buttons open the detail.
- **Status markers:** a confirmed result has none. `partial` means some legs
  are real offers and some are not. `est.` means calendar prices only. These
  are the two states the old formatter collapsed into one.
- **Savings line:** shown only when the best result has a through-fare. When
  the through-fare is cheaper than the split, it says so ("the single ticket
  is 18 EUR cheaper") rather than printing a negative percentage. That is the
  `savings_pct` sign guard, and it lives in one helper that the detail view
  uses too.
- **Grid line:** shown when the search ran on the grid strategy, from a new
  `searches.strategy` value. See §4.4.
- **Hidden line:** shown whenever a filter removes anything, so that results
  hidden by a filter are never presented as results that don't exist.
- The "book each leg separately" reminder moves from the summary into the
  detail view, next to the booking links it applies to.

### 4.2 Detail

For each leg that has an offer: carrier and flight numbers, local departure
and arrival, stops with each layover, duration, cabin and checked bags (with
"unknown" wherever a provider could not say, never "none"), and the leg's
booking link. Then the connection between the two tickets (§5), the bag
re-check warning, the through-fare comparison, and:

```
[Track this trip] [Track route]
[◀ Back]
```

A leg without an offer (partial or estimate) says "price from calendar — no
flight chosen yet" in place of its flight details.

For a stored row that is not detailed, the view shows the prices it has and
"Historical snapshot — flight details were not stored for this search."

### 4.3 Filters

```
Filters — 29 of 34 routes shown

Stops per leg:     [Any] [Direct] [≤1]
Total duration:    [Any] [12h] [18h] [24h]
Time between tickets: [Any] [2h] [3h] [4h]
Exclude airlines:  [✓ IB] [✓ UX] [✗ FR] [✓ VY]

[Clear] [◀ Results]
```

The airline row lists only carriers present in the stored results. A filter
tap writes `view_json` and re-renders this screen. "Results" returns to page
1, because the old page number may no longer exist.

### 4.4 `searches.strategy`

A second migration, `("searches", "strategy", "TEXT")`, written from
`SearchResult.strategy`. This is the first reader §5.6 requires. For the grid
line it also needs the sampled date count, which is `len(dates)`, and the
window length, from `window_start` and `window_end`. All three are already on
the row.

## 5. Filters and the connection between tickets

`results/filters.py` is pure: `apply(itineraries, filters) -> (shown, hidden_count)`.

**Unknown never passes.** When a filter is active, an itinerary it cannot
evaluate is hidden and counted in the "hidden" line. This covers estimates
and partials that have no offer for a leg, and connections whose times are
unknown. The alternative, letting unknowns through, would show a "direct
only" list containing flights nobody checked. With no filters active,
everything is shown.

| Filter | Passes when |
|---|---|
| Stops per leg | every leg's `stops` ≤ n |
| Total duration | each direction, first departure to last arrival with the buffer included, ≤ n hours |
| Time between tickets | every self-transfer buffer ≥ n hours |
| Exclude airlines | no segment of any leg flown by an excluded carrier |

**The self-transfer buffer** is new, and subproject 3 (connection risk) will
build on it. `Itinerary` gains:

- `buffer_out`: the time from the domestic outbound's last arrival to the
  onward outbound's first departure.
- `buffer_ret`: the time from the onward return's last arrival to the
  domestic return's first departure.

Each is a `timedelta`, or `None` when unknown.

This is the one place differencing two `Segment` local times is valid. The
`Segment` docstring forbids it across airports, because the two times are in
different timezones. Here both times are at the **same** airport: the hub.
So the property first checks that the arrival airport equals the departure
airport, and returns `None` when they differ, meaning an airport change
within a city, which is itself a risk subproject 3 will flag. It also
returns `None` when either time is missing (Google's reconstructed clock
times count as present only when the provider filled them in) and when
either offer is absent.

Total duration uses the same same-airport rule for the buffer, and each
offer's own `duration` for the flying parts, so no cross-timezone arithmetic
is ever done.

## 6. Tracking from a result

Tracking moves from "the best route" to any result, from the detail view:

- **Track this trip** stores `check_dates=[date]` and that result's total as
  the record price. The scheduler watches exactly that departure.
- **Track route** stores the search's full date list, which is today's
  behaviour, now for any hub and destination rather than only the best one.

Both go through `add_favorite` with the stored `provider`, exactly as
`save_favorite` does now. The legacy `savefav_<id>` callback stays
registered, because buttons already sent in old chats still carry it. It
keeps tracking the best route.

## 7. Moving `run_and_report`

`run_and_report`, its tests, `_estimate_queries` and
`_oversized_window_message` move from `handlers/search_flow.py` into
`handlers/results.py` in **one commit**, together with every
`monkeypatch.setattr("handlers.search_flow.…")` target in
`tests/test_search_flow.py`. That file becomes `tests/test_results_run.py`.
The move lands before any behaviour change, with the suite green and the
test count unchanged, so a later red test is a behaviour change rather than a
lost patch target.

After the move, one deliberately broken patch target is checked by hand to
confirm the tests actually fail, and then reverted. This is the safeguard the
3a carry-forward asks for.

`search.format_results` and `_itinerary_block` are deleted once nothing
calls them. `search.py` keeps `scan_to_json`, and `itineraries_to_json`
becomes `results.store.dump`.

## 8. Package layout

```
results/
  __init__.py
  store.py        v2 dump/load; v1 and legacy readers
  filters.py      apply(); buffer and duration helpers
  view.py         summary, detail, filters, progress text — pure
handlers/
  results.py      run_and_report; r:* and x:* callbacks; progress throttle
```

`results/` sits beside `engine/` and not under `handlers/` because nothing in
it knows about Telegram's objects, only about text and button specs, so the
web view in subproject 6 can reuse `store` and `filters` unchanged.

`view.py` returns keyboards as plain lists of `(label, callback_data)` rows.
`handlers/results.py` converts them to `InlineKeyboardMarkup`. That keeps
`view.py` importable without `python-telegram-bot`.

## 9. Error handling

| Situation | Behaviour |
|---|---|
| Callback for a deleted search | "That search is no longer stored." plus the menu |
| Callback index out of range (stale button after a DB edit) | Same message; never an exception |
| Edit fails with `BadRequest` or `Forbidden` (message too old or deleted) | Send the view as a new message; the builder's anchor pattern already does this |
| `Message is not modified` | Ignored |
| Search failed / incomplete / empty | Unchanged from today's three messages, rendered into the live message instead of a new one |
| Cancel after the search finished | "That search is no longer running." |

## 10. Testing

| File | Covers | Needs a bot? |
|---|---|---|
| `test_results_store.py` | v2 round trip is exact (`Decimal`, datetimes, None bags); v1 and legacy load as estimates; `detailed` flag | no |
| `test_results_filters.py` | each filter; unknown-never-passes; hidden count; `buffer_out`/`buffer_ret` including airport change and missing times | no |
| `test_results_view.py` | pagination edges; status markers; savings sign guard; grid line; hidden line; detail for full, partial, estimate and non-detailed rows; progress label mapping | no |
| `test_models.py` (extended) | the two buffer properties | no |
| `test_engine_orchestrator.py` (extended) | `best_total` absent before 0b, estimate after, confirmed after phase 1 | no |
| `test_results_run.py` (moved) | everything `test_search_flow.py` covers today, plus cancel and throttle | fake bot |
| `test_results_handlers.py` | callbacks: page, detail, filter set and clear, both track actions, stale id, cancel after finish | fake bot |
| `test_db.py` (extended) | the `view_json` and `strategy` migrations | no |

## 11. Writing order

Bottom-up, tests first for each step:

1. The move (§7). Behaviour unchanged, suite green, same test count.
2. `Itinerary.buffer_out` / `buffer_ret`.
3. `results/store.py` and the two migrations. `run_and_report` writes v2 and
   `strategy`; `history.py` reads through `store.load`.
4. `results/filters.py`.
5. `results/view.py`.
6. `best_total` in the orchestrator.
7. `handlers/results.py`: progress, cancel, the live message, the callbacks.
   The builder passes its anchor, and `history.py` opens the view.
8. Tracking from a result.
9. Delete the dead formatter; correct the README's Features and Example
   output sections to describe what is now shown.

## 12. Done when

- A search started from the builder shows live progress in the same message,
  can be cancelled, and becomes a paged summary in that message.
- Every result opens a detail with per-leg flights, times, bags and booking
  links. The README's per-leg claim is true again.
- Filters change what is shown with zero new requests, and say how many
  routes they hide.
- A search from before the last restart still pages, filters and opens
  details. A pre-3b row opens as a labelled snapshot.
- Every 3b item in the 3a carry-forward and 3a design §8 is closed.
- Full suite green; `ruff check .` clean.
