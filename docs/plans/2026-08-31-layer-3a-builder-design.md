# Layer 3a — the search builder

Date: 2026-08-31. Branch: `feat/search-builder` (from `main` @ `b4d3c97`).
Spec: [multi-provider search design](2026-08-29-multi-provider-search-design.md) §6.2, §6.3, §6.4, §7.1
Binding notes: [layer 1](2026-08-29-layer-1-carry-forward.md) · [layer 2](2026-08-31-layer-2-carry-forward.md)

Layer 3 as §6 describes it is a builder, a place picker, a date picker, an
options screen, progress and cancel, result cards, and the §7.2 scheduler
consolidation. It lands in three stages:

| | Scope | Touches the engine? |
|---|---|---|
| **3a** (this doc) | hub-and-spoke builder, place autocomplete, month picker | no |
| 3b | progress and cancel, result cards, filters, pagination, detail view | no |
| 3c | passengers/cabin/currency/limits, `run_search` signature, favourites replay, §7.2 | yes |

The split is not arbitrary. 3c is the only stage that changes an engine
signature, and it is the stage the Layer 2 carry-forward warns will break a
Google-only deployment the moment a cabin selector exists. Isolating it means
the two stages that cannot break a search are not reviewed alongside the one
that can.

Each stage ships a bot that works. After 3a the input flow is new and the
results are today's unchanged text.

## What Layer 2 already built

Worth stating up front, because §6 reads as though none of it exists:

- `CancelToken`, `SearchCancelled`, `Progress` and `ProgressCallback` are in
  `models.py`. `run_search` already accepts `cancel=` and `on_progress=` and
  checks cancellation between every phase. §6.6 is wiring, not engine work — and
  it is 3b's wiring, not 3a's.
- §7.1's migrations are applied, except `place_cache`.
- Kiwi already implements `resolve_place` and `price_calendar`; Google
  implements neither.
- `handlers/search_flow.py` is *not* untested, contrary to the Layer 2
  carry-forward. `tests/test_search_flow.py` covers `run_and_report`,
  `_oversized_window_message` and `_estimate_queries` in 16 tests. What has no
  tests is the conversation layer — the eleven states, `_summary_text`,
  `_show_confirm` — which is exactly what this stage deletes.

Baseline: 362 tests, green.

## 1. Package layout

```
handlers/search/
  __init__.py   build_search_conversation()
  draft.py      SearchDraft and its rendering   — imports no telegram
  builder.py    anchor message, routing, Back
  places.py     autocomplete picker
  dates.py      month grid
  hubs.py       hub multi-select
```

`handlers/search_flow.py` keeps `run_and_report`, `_estimate_queries` and
`_oversized_window_message`; 3a takes only the conversation out of it. `bot.py`
changes one import line to take `build_search_conversation` from
`handlers.search`; `handlers/history.py` and `tests/test_search_flow.py` are
untouched.

Moving `run_and_report` in this stage would be free churn with a real cost. All
seven `monkeypatch.setattr` sites in `tests/test_search_flow.py` patch
attributes on the `handlers.search_flow` module object — `run_search` in three
of them, `primary_provider` in four. A function that moves resolves those names
in its new module's namespace, so every one of those patches would silently
stop taking effect and the tests would have to be rewritten to chase it. 3b
rewrites results and has to touch those tests anyway; the move belongs there,
with them.

## 2. The pure core

`draft.py` holds a frozen `SearchDraft` with `with_*` copy methods — the
`Itinerary.with_through_fare` idiom already in `models.py`. It owns every
decision worth testing:

| Member | Purpose |
|---|---|
| `screen` | which sub-screen is showing — **navigation state lives here, not in PTB** |
| `missing` | the fields blocking Search, so the button can be disabled with a reason |
| `to_params()` | the exact dict `run_and_report` already takes |
| `render(estimate=...)` | `(html_text, rows)`, where a row is `[(label, callback_data), ...]` |

`render()` returns button *tuples*, not `InlineKeyboardMarkup`; `builder.py`
has one four-line `_markup()`. The pre-flight query count is *passed in* rather
than computed: `_estimate_queries` still lives in `handlers/search_flow.py`, so
computing it inside `draft.py` would drag that module — and telegram with it —
into the import graph of the one file whose whole point is not needing a bot to
test. `builder.py` already imports telegram, so it makes the call and hands the
number over.

That keeps `draft.py` importable without telegram, so its tests need no
`Update`/`Context` scaffolding — the same reason `tests/test_search_flow.py`
tests `run_and_report` and not the handlers.

## 3. One conversation state

`ConversationHandler` keeps exactly one state, `BUILDING`, holding every
`CallbackQueryHandler` plus one `MessageHandler` dispatching on
`draft.awaiting`. PTB handles entry, exit and text capture; *which screen you
are on is data*.

This is what makes §6.2's promise — Back and Edit everywhere by construction —
true rather than aspirational. Back is `draft.with_screen(DRAFT)` and a
re-render. There is no transition table to keep consistent, which is the thing
that made the old eleven-state chain unable to go backwards at all.

## 4. The anchor message

`user_data["anchor"]` holds one `message_id`, edited for every screen. One
`_render()` funnels all of it and absorbs `BadRequest: Message is not
modified` — the same failure §6.6 calls out for the progress message.

Free text breaks the single-panel illusion: when the user types a place name,
Telegram appends their message and the draft is no longer last on screen. The
handler therefore deletes the user's message after reading it. Both failure
paths resend and re-anchor rather than stranding the user:

- the edit fails because the anchor is gone
- the delete is refused (no rights, or the message is over 48h old)

These two paths are the only way this design can leave a user with no working
panel, so they are tested.

## 5. Place picker (§6.3)

Text in, up to eight toggle buttons, `Done`. Destinations and hubs are
multi-select; `MAX_DESTINATIONS = 10` is retained.

**Which provider answers.** Any enabled provider implementing `SupportsPlaces`,
primary first — not `primary_provider()`. With `PROVIDERS=("kiwi","google")`
and `PRIMARY_PROVIDER=google`, Kiwi still resolves names even though Google
drives the search. Only a genuinely Kiwi-less deployment loses autocomplete,
and there the screen degrades to the existing `parse_iata_codes` prompt.

This is the honest reading of two success criteria that otherwise conflict:
"no step of the conversation requires knowing an IATA code" and "disabling Kiwi
leaves a working Google-only bot". The first holds wherever a places-capable
provider is configured; the second is what the degradation is for.

**The paste path short-circuits the lookup.** Text that already parses as IATA
codes is accepted directly with no request. §6.3's power-user path, and it also
means a dead `places` endpoint never blocks a user who knows the code.

**Provider text is hostile until escaped.** `Place.name`, `.city`, `.country`
and the user's own search term all reach HTML. The Layer 2 carry-forward flags
these interpolations as safe only by luck; every one goes through `esc()`.
Separately, `Place.code` is validated against the IATA shape before storage —
it flows into a `LegQuery`, a booking URL and a `searches` row, and a provider
is not a trusted source for it.

**Cache (§7.1).** New table, keyed on the casefolded, whitespace-collapsed
term:

```sql
CREATE TABLE IF NOT EXISTS place_cache (
    term      TEXT PRIMARY KEY,
    places    TEXT NOT NULL,   -- JSON list
    cached_at TEXT NOT NULL
);
```

`init_db` runs `executescript(SCHEMA)` on every start, so this needs no
`MIGRATIONS` entry — that tuple exists only for columns added to tables that
already exist. `PLACE_CACHE_TTL_HOURS` defaults to 720: airports do not move,
and the TTL is there for renames, not freshness.

## 6. Date picker (§6.4)

`month_rows(year, month, *, mode, window, picked, ratings, today)` is a pure
function returning button rows. Every rule that matters is testable with no
bot: no past days, the `MAX_WINDOW_DAYS` ceiling refused *at the tap* rather
than at Ready, and the two modes.

The ceiling is re-checked here against `config.MAX_WINDOW_DAYS` rather than
reusing `_oversized_window_message`. That function returns a paragraph written
for a chat reply and lives in a telegram-importing module; a picker needs three
words under the grid. Two renderings of one limit, one source of truth for the
number.

**Two modes, because a window is not the only real question.** §6.4 says the
two-stage model means choosing a window, and that is the default: tap-start,
tap-end, plus `Next 30` / `Next 90` / `This month`. But "I can only fly the
3rd, the 10th or the 17th" is a real search the bot handles today, and a
window-only picker would drop it *and* turn `run_and_report`'s C1 date filter
into dead code that a later reader deletes without knowing what it guarded. A
`Pick days` toggle switches the same grid to multi-select.

Mode lives in the draft, so toggling is a re-render. Switching modes clears the
other mode's selection rather than translating it — a three-day pick is not a
window, and guessing which was meant is worse than asking again.

**Ratings.** One `price_calendar` for `origin → first destination`, when a
destination is set and a `SupportsCalendar` provider exists. Cells become
`🟢15` / `🟡15` / `🔴15`, plain `15` otherwise, cached in `user_data` per
`(dest, year-month)` so paging months does not re-request. The caption names
the destination and calls it a *direct-fare signal*, per §6.4 — it is the
through-fare shape, never a saving, and never the split. A `ProviderError`
renders the grid uncoloured with a one-line note: a decoration must not break
the picker.

## 7. Storage: nothing changes

`to_params()` emits exactly the dict `run_and_report` takes today.

| Mode | `dates` | `window_start` / `window_end` |
|---|---|---|
| days | the picked list | `min` / `max` of it |
| window | `window.dates()`, the full expanded span | the window |

So `min(dates)`/`max(dates)` still reconstruct the window, `history_rerun`
needs no change, and the C1 filter (`itin.date in requested_dates`) stays
load-bearing in days mode and correctly vacuous in window mode. No new column,
no `run_and_report` signature change, no engine call-site edit.

The cost is about a kilobyte of JSON for a 91-day window search. The benefit is
that this stage touches no persisted shape at all, so nothing in `db.py`,
`scheduler.py` or `handlers/history.py` has to be re-reasoned about.

**All 16 existing `test_search_flow.py` tests must pass untouched**, and with
`run_and_report` staying put (§1) that is a real constraint rather than a hope.
If one fails, that is a signal this section is wrong — not a test to update.

## 8. What 3a does not do

| | Lands in |
|---|---|
| Progress message, cancel button, phase relabelling for humans | 3b |
| Result cards, pagination, filters, detail view | 3b |
| The `savings_pct` sign guard in a card renderer | 3b |
| Splitting `STATUS_PARTIAL` from `STATUS_ESTIMATE` in display | 3b |
| `SearchResult.strategy` telling the user a grid search samples | 3b |
| `Progress.best_total` — populate it or delete it | 3b |
| README's per-leg airlines, stops and durations claim | 3b |
| Passengers, cabin, currency, bags, limits | 3c |
| The `run_search` signature and its four `LegQuery` call sites | 3c |
| `favorites` replay of cabin/children/max_stops/min_layover | 3c |
| §7.2 scheduler consolidation; the grid window end-date drop | 3c |

**The prohibition 3a inherits.** It must not set `cabin`, `children` or
`min_layover` on anything. Google raises a bare `ProviderError` for all three,
which aborts a phase and reports a misconfigured search as "no flights". The
draft therefore shows a read-only `1 adult · Economy · EUR` footer under the
query estimate — informative, with no Edit affordance to tap dead. 3c promotes
it to a real row *and* adds the `ProviderError` handling that makes it safe.
Those two must ship together, which is why they are one stage.

## 9. Testing

| File | Covers | Needs a bot? |
|---|---|---|
| `test_search_draft.py` | `SearchDraft`, `missing`, `to_params`, `render` | no |
| `test_search_dates.py` | month grid, both modes, window ceiling, past days | no |
| `test_search_places.py` | provider selection, paste short-circuit, `esc()`, IATA validation, cache hit/miss/expiry, no-places degradation | no |
| `test_search_builder.py` | anchor lifecycle, edit-fails-resend, delete-refused | fake bot |
| `test_db.py` (extended) | `place_cache` round-trip and TTL expiry | no |

Steps 1–5 of the writing order below are pure and land with their own tests.
Only the builder needs a fake bot, and only for the two paths that can strand a
user.

## 10. Writing order

Bottom-up, tests first per step:

1. `place_cache` table, `PLACE_CACHE_TTL_HOURS`, get/put accessors, TTL expiry
2. `draft.py` — the biggest unit, and what every screen depends on
3. `dates.py` — grid and mode rules, `ratings=None` by default so it is
   testable with no provider at all
4. `places.py`
5. `hubs.py`
6. `builder.py` — thin layer over all of the above, so it goes last
7. Wire-up: `__init__.py` exports `build_search_conversation`; `bot.py` takes it
   from `handlers.search`; delete the conversation half of
   `handlers/search_flow.py`; full suite green against the 362 baseline

Step 7 deletes the eleven states, `_summary_text`, `_show_confirm`,
`_ask_hubs`, `_hub_keyboard` and `build_search_conversation` from
`search_flow.py`, and nothing else. `run_and_report`, `_estimate_queries` and
`_oversized_window_message` stay exactly where they are.

## 11. Done when

- No step of the conversation requires typing an IATA code or a date, on a
  deployment with a places-capable provider
- A Kiwi-less deployment still completes a search through the typed-code path
- Back and Edit reach every field from every screen
- Both date modes work, and the window ceiling is refused at the tap
- `handlers/history.py`, `scheduler.py`, `search.py` and `engine/` are
  unmodified; `bot.py` changes only its import line; `db.py` gains only the
  `place_cache` table and its two accessors
- All 16 existing `test_search_flow.py` tests pass with no edit
- The suite is green and larger than 362
