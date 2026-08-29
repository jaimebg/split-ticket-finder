# Layer 1 — carried forward

Date: 2026-08-29. Branch: `feat/multi-provider-search`.
Spec: [multi-provider search design](2026-08-29-multi-provider-search-design.md) · Plan: [provider layer](2026-08-29-provider-layer-plan.md)

Layer 1 (the provider abstraction) is complete and reviewed. What follows is what
the nine task reviews and the whole-branch review deliberately left undone, so
Layer 2 starts from the record rather than rediscovering it.

## Deferred findings

- **T1** — tests/test_search.py:20 `test_add_days_matches_scraper_helper` names a helper that now lives in models, not scraper — rename in a later task.
- **T1** — models.py dropped the `# ==== Helpers ====` banner comment from scraper.py. Definitions themselves are byte-identical; only surrounding scaffolding differs.
- **T2** — providers/base.py:214-238 — `frozen=True` on Offer guards attribute reassignment but not in-place mutation of the `airlines`/`segments` lists, and Offer is unhashable. Docstrings and tests do not overclaim, so no dishonesty; worth a one-line docstring caveat before a later task assumes Offer can go in a set. Changing to tuple would be spec drift (field types are prescribed verbatim for downstream kwarg compatibility).
- **T3** — providers/google.py:140 — `duration=int(raw.get("duration") or 0)` and `origin/dest` falling back to "?" brush against "unknown is never zero", but Segment.duration/origin/dest are non-Optional in the Task 2 schema so None is not legal there. Structural, inherited verbatim from the brief, not an implementer choice.
- **T3** — providers/google.py:134-143 — Segment.carrier_name is set to the bare IATA code because Google's per-segment payload has nothing better. Offer.airlines is unaffected (it comes from FlightResult.airlines, which does carry real names). Kiwi will populate carrier_name properly, so the two providers will differ at segment level.
- **T4** — providers/kiwi.py:93,109-151 — `variables` never appears in raised messages, so a log line reads "PricesCalendar: HTTP 403" without saying which route/date failed. _execute is generic and cannot know which keys are meaningful; call sites in Tasks 5-7 are the right place to add route context.
- **T4** — providers/kiwi.py:109-111 — the `except httpx.HTTPError` network-failure retry branch has no covering test (all 10 tests exercise status-code and payload branches). Sound by inspection, untested.
- **T4** — providers/kiwi.py:94 — `last_error = "unknown error"` is unreachable; MAX_RETRIES is floored at 0 so the loop always runs once and overwrites it. Dead initialization.
- **T5** — providers/kiwi.py — "OnewayItineraries: response carries no itineraries" and sibling messages carry no route/date context, hard to diagnose from logs alone. Same theme as the Task 4 minor.
- **T5** — providers/kiwi.py `_local_time` catches only ValueError; a non-string truthy value would raise an uncaught TypeError instead of ProviderParseError, unlike _money/_minutes.
- **T5** — providers/kiwi.py — a layover present as `{}` (no duration key) is silently treated as "no layover" rather than raising for schema drift, distinct from the documented `layover: null` case.
- **T5** — providers/kiwi.py ONEWAY_QUERY fetches `provider { name }` which _to_offer never reads. Harmless over-fetch, verbatim from the brief.
- **T6** — _local_time / layover-{} observations from Task 5 still stand, unchanged by this task.
- **T7** — no test for the missing-`edges` raise path in resolve_place, unlike the equivalents now covered for `itineraries` (Task 5) and `calendar` (Task 6). Code path correct on inspection. This is the THIRD instance of the same pattern; I ruled it in-scope twice but Minors do not enter the fix loop, so it goes to the final review for triage. If the final review touches anything here, close this too.
- **T7** — providers/kiwi.py:437-439 — if the query ever stopped populating `code` on every node, resolve_place would skip all of them and return [] , indistinguishable from "no matches". Narrow (the AIRPORT-only server filter makes it unlikely, and _unwrap catches the wholesale shape changes first) but a genuine silent-empty blind spot. Verbatim from the brief.
- **T8** — providers/registry.py `_BUILDERS` and config.py `KNOWN_PROVIDERS` are two hand-maintained lists of valid provider names that must stay in sync. As specified in the brief, but a latent drift risk — worth a shared constant when Layer 2 adds anything.
- **T8** — tests/test_registry.py StubProvider defines a search_leg never exercised by the close_all test; could be trimmed to name + aclose.
- **T9** — AppError.message is read by _unwrap for all three operations but has no EXPECTED entry — genuinely uncovered, though the access uses .get(key, default) so a rename degrades the error text rather than breaking silently.

## Decisions that bind Layer 2

- **Do not split `providers/kiwi.py` yet.** At ~450 lines against `google.py`'s ~425 it was
  assessed as proportionate: three query constants, each sitting immediately above its single
  caller. Splitting now is a pure refactor touching every import across the suite for no
  functional payoff. **The trigger is the next query added to that file, not the last one.**

- **Trim the queries before shrinking the drift guard, not after.** Several fields are
  selected but never read (`provider{name}`, booking-option `price`, `currency{code}`,
  `legacyId`, itinerary `id`). The guard covers them deliberately: a removed *selected* field
  invalidates the whole query and takes the provider dark, which is worse than a mapping gap.
  Shrinking the guard first would remove live protection.

- **`Layover.isStationChange` and `isBaggageRecheck` are selected but unused — start using
  them, don't trim them.** They say whether a self-transfer forces re-claiming and re-checking
  bags at the hub. That is the risk the README names as this project's top limitation, and
  Kiwi already answers it for free.

- **The `min_layover is None` predicate is Kiwi-local.** Kiwi's offers carry per-connection
  layovers, so `None` genuinely means "direct". Google's offers have `min_layover is None` on
  *every* offer including multi-stop ones. The engine keys on `stops == 0`; any new
  buffer-filtering code must do the same or it will pass every multi-stop Google itinerary.

- **`search_leg` can return fewer than `limit` offers.** `filter.limit` is applied server-side
  before the client-side exact-minute filter, so a caller needing K results must over-request.

- **`Offer` is unhashable and only shallowly frozen.** The leg cache and dedup in the two-stage
  search cannot put offers in a `set`; key on `(origin, dest, date)` instead.

- **Google raises rather than silently ignoring what it cannot express** (`children`,
  non-economy `cabin`, `min_layover`). A Layer 2 builder that sets those fields must either
  route to Kiwi or handle `ProviderError`.
