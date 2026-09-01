# Layer 3a — carried forward

Date: 2026-09-01. Branch: `feat/search-builder`.
Spec: [multi-provider search design](2026-08-29-multi-provider-search-design.md) §6.2, §6.3, §6.4, §7.1 · Design: [layer 3a builder design](2026-08-31-layer-3a-builder-design.md) §8 · Plan: [layer 3a builder plan](2026-09-01-layer-3a-builder-plan.md)
Earlier notes: [layer 1](2026-08-29-layer-1-carry-forward.md) · [layer 2](2026-08-31-layer-2-carry-forward.md) — both still binding.

Layer 3a (the hub-and-spoke draft builder, place autocomplete, month picker)
is complete and reviewed. It replaces the eleven-state linear conversation
with a single `BUILDING` state driven by `SearchDraft.screen`, resolves
place names without the user typing an IATA code when a places-capable
provider is enabled, and degrades cleanly to typed codes when it isn't. It
touches no engine code and changes no persisted shape. This records what it
deliberately left undone and the decisions that bind Layer 3b and 3c, so the
next stage starts from the record rather than rediscovering it.

## What 3b inherits

- **The `savings_pct` sign guard for card renderers.** The stored field is
  unguarded and goes negative when the through-fare beats the split (Layer
  2's rule: this is a formatter concern, not an engine one). A naive card
  reader that doesn't check the sign will print "You save -18%".
- **Splitting `STATUS_PARTIAL` from `STATUS_ESTIMATE` in display.** The
  current formatter still collapses them into one line, which is exactly the
  understating the three-state signal was added to prevent.
- **`SearchResult.strategy` telling the user a grid search *samples* rather
  than covers the window.** Read by nothing today. §5.6 requires it; no card
  delivers it.
- **`Progress.best_total`**, declared and forwarded since Layer 2, still
  never populated. Set it from the orchestrator's running best, or drop the
  field — don't ship a progress UI that reads it as always-zero.
- **The progress phase labels**, where `"Phase 1 (cross-check)"` arrives
  after `"Phase 2"` completes and reads as going backwards. The engine's job
  was uniqueness, not human-facing wording; 3b owns the rename.
- **README's per-leg airlines/stops/durations claim.** The current renderer
  dropped that data. Restore it or fix the claim — don't ship a results
  rewrite that leaves the mismatch standing.
- **Moving `run_and_report` out of `handlers/search_flow.py` together with
  its tests' monkeypatch targets.** `tests/test_search_flow.py` patches
  `run_search` and `primary_provider` as attributes of *this module*; a
  moved function resolves those names in its new namespace and the patches
  silently stop taking effect. Move the function and the tests in the same
  commit, or the suite goes green while testing nothing.

## What 3c inherits

- **The `run_search` signature and its four `LegQuery` call sites.** 3c is
  the only stage that changes an engine signature — that's the reason it's
  its own stage rather than folded into 3a or 3b.
- **The `ProviderError` handling that MUST ship in the same commit as any
  cabin selector.** Google raises a bare `ProviderError` on `children`,
  non-economy `cabin`, and `min_layover` — it can't express those queries at
  all. Left unhandled, that aborts a phase and reports a misconfigured
  search as "no flights," not as "this deployment can't do that." The draft
  therefore ships 3a with a read-only `1 adult · Economy · EUR` footer and no
  Edit affordance on it; 3c promotes it to a real row *and* adds the
  handling that makes doing so safe. Those two land together on purpose.
- **`favorites` replay of `cabin`/`children`/`max_stops`/`min_layover`.**
  Stored since Layer 2, never replayed. Harmless while nothing sets a
  non-default value; the moment 3c's cabin selector does, an unreplayed
  favorite compares two different queries and reports the difference as a
  price movement.
- **§7.2 scheduler consolidation and the scheduler's grid window end-date
  drop.** The scheduler holds a window, not a discrete list, so it can't
  pass `explicit_dates` and the grid sampler can miss the final date.
  Strictly better than pre-Layer-2 (which truncated to 5 sampled points),
  but the same shape as the bug already fixed for the guided flow.

## Decisions 3a made that bind later work

- **Place resolution keys off ANY enabled `SupportsPlaces` provider, primary
  first — not `primary_provider()`.** This is what reconciles "no step of
  the builder requires typing an IATA code" (true whenever *some* enabled
  provider does places) with "disabling Kiwi leaves a working Google-only
  bot" (true because the picker degrades to typed codes only when *no*
  enabled provider does places, regardless of which one is primary). Keying
  off `primary_provider()` instead would make the picker's availability
  depend on an unrelated deployment choice.
- **Window mode stores `dates` as the full expanded span rather than adding
  a `date_mode` column.** That is what kept this layer free of any persisted
  -shape change, so `db.py`, `scheduler.py` and `handlers/history.py` were
  never re-reasoned about. About 1 KB of JSON per window search buys that.
  Don't "optimize" this into a compact range representation without
  reopening those three files.
- **Navigation state lives on `SearchDraft.screen`, not in
  `ConversationHandler`, which keeps ONE state.** Adding a screen means
  adding a `SCREEN_*` constant and a branch in `_show` — never a new PTB
  state. Do not reintroduce per-screen states; that was the eleven-state
  design this layer replaced.
- **`toggle_hub` takes an optional `name`** so a hub found by name search
  keeps its city; that string becomes `Itinerary.hub_name` on every result
  card. Any new way of adding a hub to the draft (3b's filters, say) should
  thread a name through the same way rather than falling back to a bare code.
- **`handlers/search/__init__.py`'s re-export of `build_search_conversation`
  is lazy by design (PEP 562 `__getattr__`), not an oversight.** An eager
  `from handlers.search.builder import build_search_conversation` at module
  level pulls `telegram`/`telegram.ext` into `sys.modules` on *any* import
  reaching into the package — including `import handlers.search.draft` —
  because Python always runs a package's `__init__.py` before a submodule
  import completes. That silently destroys the telegram-free boundary
  `draft.py` and `dates.py` are built around: the reason `draft.py` returns
  button tuples instead of `InlineKeyboardMarkup`, and takes the query
  estimate as an argument instead of computing it. This isn't hypothetical —
  it happened once, in Task 7's wire-up commit, and was caught only because
  Task 8's manual verification step re-ran the check by hand; nothing in the
  suite would have caught it otherwise.
  `tests/test_search_draft.py::test_the_draft_and_dates_modules_stay_telegram_free`
  now pins it — it shells out to a subprocess (an in-process check is
  useless once any other test module has already imported telegram) and
  fails if the re-export goes eager again. Anyone touching
  `handlers/search/__init__.py` should keep the `__getattr__` form and keep
  that test passing, not "simplify" it to a plain import.

## Known gaps

- **`_oversized_window_message` in `handlers/search_flow.py` is unreachable
  from production.** Both of its call sites went with the old conversation
  in Task 7, and `dates.py`'s `_too_long` reimplements the same ceiling
  check for the new one. It survives only because three tests in
  `tests/test_search_flow.py` call it directly — those tests could not be
  edited without destroying the evidence that Task 7's deletion took the
  conversation and nothing else. Delete the function in 3b, with those
  tests, when `run_and_report` moves (see "What 3b inherits" above).
- **`_load_ratings` caches per `(dest, year-month)` in `user_data` with no
  eviction**, so a long month-paging session accumulates entries for the
  life of the conversation. It also caches `{}` on `ProviderError` with no
  retry, so a transient provider outage means that destination/month shows
  no colours again until the draft is reset — not just until the outage
  clears.
- **Ratings use only the FIRST destination**, per §6.4, which is silent
  about what a multi-destination draft should show. Picking a destination to
  privilege (or blending) is an open design question, not a bug.
- **`try_parse_codes` reads any three-letter input as an IATA code**, so
  someone typing a three-letter city name (e.g. a name that happens to be
  three letters) gets a code lookup instead of a places search. No
  disambiguation exists between "this looks like a code" and "this is a
  short name."
- **`go()` and `to_menu()` call `query.edit_message_text` directly rather
  than through `render_anchor`**, inconsistent with the module's resend
  discipline elsewhere. Safe today only because a callback query always
  originates from the live anchor message; would need revisiting if a
  future screen could be reached from a stale or forwarded message.
- **The escaping tests inject hostile characters only into `Place.name`**;
  `city` and `country` escaping is unasserted, though `_label` does escape
  all four fields. The code path is believed safe — the test coverage isn't
  proof of it.
- **`apply_preset`'s "month" branch does not cap against `MAX_WINDOW_DAYS`**
  the way its "30"/"90" branches do. Unreachable at the current default of
  91 days, since no calendar month exceeds 31 days — reachable only if an
  operator lowers the window limit below roughly 31.

## Measured cost, for reference

`handlers/search_flow.py`: 764 lines (Layer 2 end state) → 245 lines after
Task 7 deleted the conversation → 251 lines after this task's docstring
correction (net +6 lines for an accurate paragraph, no code changed).

Suite: 362 tests (Layer 2 baseline) → 474 after Layer 3a's seven build tasks
→ 475 after this task added the subprocess-based pinning test for the
telegram-free boundary. Ruff clean throughout.
