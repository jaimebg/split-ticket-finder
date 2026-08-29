# Multi-provider search — design

Date: 2026-08-29
Status: approved, not yet implemented
Supersedes parts of: `2026-02-25-telegram-bot-design.md`

---

## 1. Why

The bot works, but three of its properties are set by the fact that Google
Flights is its only data source, and none of them are inherent to the problem:

- **A search is slow and coarse.** Every (hub, destination, date) needs its own
  scrape, so a round-trip over 8 hubs, 3 destinations and 10 dates is ~640
  requests and about ten minutes. Ten dates is not a lot of dates.
- **The result set is thin.** Google's payload gives price, airlines, stop count
  and duration. It does not give baggage allowance, baggage prices, layover
  length or a booking link, so the README has to list "baggage is not modelled"
  and "leave a real buffer between legs" as limitations the user must handle.
- **There is one point of failure.** The parser reads undocumented array indices.
  When it breaks, the bot has nothing.

A second source removes all three at once, and Kiwi.com turns out to be an
unusually good fit: it is the operator that popularised virtual interlining, so
its data model already has first-class notions of self-transfer, separate
bookings and per-leg baggage — the exact concepts this project is built around.

## 2. Access research

Two routes were investigated on 2026-08-29.

**Tequila (the official B2B API) is not available.** Public self-serve signup
closed in May 2024; access is now invitation-only and requires a live travel
product or an established distribution use case. Not viable for a personal
project.

**Kiwi's own GraphQL backend is reachable.** `api.skypicker.com/umbrella/v2/graphql`
serves the kiwi.com frontend, needs no authentication, and has schema
introspection enabled. Every capability below was verified live against real
data before this design was written:

| Query | Verified behaviour |
|---|---|
| `itineraryPricesCalendar` | 91 days of `LPA→MAD` prices in **one request**, each with a cheap/average/expensive rating |
| `places` | `"Gran Canaria"` → `Station:airport:LPA`, city, country, GPS |
| `onewayItineraries` | price, exact local times, flight numbers, carriers, duration, baggage tiers, deep booking link, `pnrCount` |
| `ItinerariesFilterInput` | `maxStopsCount`, `stopoverTime`, `excludeCarriers`, `maxDuration`, `price` |

Also present and unused for now: `onewayOnePerCityItineraries`, `nomadItineraries`,
`itineraryPriceGraph`, `returnItineraryPricesCalendar`.

Three practical facts that shape the client:

- Place ids are deterministic (`Station:airport:LPA`), so a known IATA code needs
  no lookup. `places` is only needed for the autocomplete UX.
- `options.partner` is required; `"skypicker"` works.
- A long-haul through-fare query (`LPA→NRT`) returned `pnrCount: 3` — Kiwi was
  itself selling a three-ticket self-transfer. This matters for honesty: only
  `pnrCount == 1` is a genuine single-ticket through-fare, and that is the
  baseline savings must be measured against.

## 3. Scope

Three layers, built in order, each depending on the one before it.

1. Provider abstraction and the Kiwi client. Nothing user-visible.
2. Two-stage search engine and the richer result model.
3. Bot UX rebuild.

This document specifies all three so the interfaces between them are settled up
front. Each layer gets its own implementation plan and its own review checkpoint.

Explicitly out of scope: multi-city and nomad search, hotels, accounts or
multi-user support, and any paid API.

---

## 4. Layer 1 — Provider abstraction

### 4.1 Shape

```
providers/
  base.py       protocols, dataclasses, error taxonomy
  google.py     today's scraper.py logic, unchanged, behind an adapter
  kiwi.py       GraphQL client
  registry.py   name -> instance, driven by the PROVIDERS env var
```

`scraper.py` keeps its protobuf `tfs` encoder and its parser intact and moves
under `providers/google.py`. The existing tests continue to exercise that logic
directly; a thin new test covers the adapter mapping only.

### 4.2 Protocols

Capabilities genuinely differ between sources, so this is three runtime-checkable
protocols rather than one interface full of `supports_x()` booleans:

| Protocol | Method | Google | Kiwi |
|---|---|---|---|
| `FlightProvider` (required) | `search_leg(LegQuery) -> list[Offer]` | yes | yes |
| `SupportsCalendar` | `price_calendar(CalendarQuery) -> dict[str, RatedPrice]` | no | yes |
| `SupportsPlaces` | `resolve_place(term, limit) -> list[Place]` | no | yes |

The engine selects its strategy with `isinstance(provider, SupportsCalendar)`.
That is what lets a Google-only configuration keep running today's grid search
instead of failing, which is the fallback path required by §5.6.

### 4.3 Data model

`Offer` replaces `FlightResult` and is a strict superset:

```python
@dataclass(frozen=True)
class Segment:
    # Timestamps are Optional because sources differ: Kiwi gives full local
    # times, Google gives bare clock times that must be reconstructed against
    # the query date and can degrade to unknown.
    origin: str            # IATA
    dest: str
    carrier: str           # IATA carrier code
    carrier_name: str
    flight_no: str         # e.g. "FR2012"
    duration: int          # minutes
    dep_local: datetime | None = None
    arr_local: datetime | None = None

@dataclass(frozen=True)
class Offer:
    price: Decimal
    currency: str
    airlines: list[str]
    stops: int
    duration: int                      # minutes
    segments: list[Segment]
    provider: str
    booking_url: str | None = None
    included_cabin_bags: int | None = None
    included_checked_bags: int | None = None
    checked_bag_price: Decimal | None = None
    min_layover: int | None = None     # minutes
    pnr_count: int | None = None
```

**`None` means "this provider cannot tell you". It never means zero.** A
Google-sourced offer has `included_checked_bags is None`; the formatter must
render that as "unknown", never as "no bag included". Getting this wrong would
make the bot state a fare condition it has not verified.

### 4.4 Normalisation

Three places where the two sources genuinely disagree, owned by the adapters:

- Kiwi `duration` is **seconds**; Google's is minutes. Normalise to minutes.
- Kiwi prices are **strings** (`"29"`, `"34.99"`). Parse to `Decimal`, round only
  at render time. Float accumulation across four legs and a 75% discount is not
  acceptable for money.
- Kiwi `bookingUrl` is **relative** (`/en/booking/?…`). Prefix
  `https://www.kiwi.com`.

### 4.5 Errors

`ProviderError` → `ProviderFetchError` | `ProviderParseError`, replacing
`FetchError` / `ParseError` and keeping their meaning.

The Kiwi client raises `ProviderParseError` on a GraphQL `AppError` response
**and on any missing expected field**. Schema drift must never degrade into an
empty result list. This is the same principle the current parser already applies
to consent walls and layout changes, and it is why the codebase distinguishes
the two cases at all.

### 4.6 Configuration

New settings, following the existing `_*_env` validation helpers in `config.py`:

| Name | Default | Meaning |
|---|---|---|
| `PROVIDERS` | `kiwi,google` | Enabled providers, in preference order |
| `PRIMARY_PROVIDER` | `kiwi` | Drives search; must be in `PROVIDERS` |
| `KIWI_CONCURRENCY` | `8` | In-flight request cap for Kiwi |
| `KIWI_DELAY` | `0.3` | Per-worker delay, seconds |
| `KIWI_PARTNER` | `skypicker` | GraphQL `options.partner` |

Google keeps `MAX_CONCURRENCY` and `DEFAULT_DELAY` unchanged — scraping needs to
stay gentle, and the two sources should not share a budget.

`validate()` gains a check that `PRIMARY_PROVIDER` appears in `PROVIDERS`.

### 4.7 Drift guard

Introspection is open, so `tests/test_kiwi_schema.py` queries the live schema and
asserts every field this project depends on still exists. It is marked
`@pytest.mark.network` and deselected by default, so CI stays fully offline while
"did Kiwi change?" remains a single command.

Offline tests run against recorded JSON fixtures in `tests/fixtures/kiwi/`,
mirroring the existing `google_flights_lpa_mad.html` approach.

---

## 5. Layer 2 — Two-stage search engine

### 5.1 The idea

Today every candidate date costs a request, so the user is asked to sample dates
("every N days") and the search still costs hundreds of requests. The calendar
endpoint inverts this: date coverage becomes nearly free and only *confirmation*
costs requests. So the engine scans broadly, then spends its request budget on a
shortlist.

### 5.2 Phase 0 — scan

One calendar per hub (`origin→hub`) and one per hub×destination (`hub→dest`),
each covering the entire window. Round trips add the mirrored pair over the
shifted window.

Cost is `H·(1+D)` one-way, doubled for round-trip, and is **independent of window
length** up to 91 days.

### 5.3 Phase 0b — rank

Pure arithmetic over the cartesian product, no requests:

```
est(h, dest, d) = cal[origin→h][d] · (1 − discount) + cal[h→dest][d]
                + (round-trip: cal[h→origin][d+t] · (1 − discount) + cal[dest→h][d+t])
```

Every day in the window is ranked. These are Kiwi's cached cheapest-of-day
figures: they are **estimates, not bookable prices**, and every surface that
shows them must say so.

### 5.4 Phase 1 — drill down

Take the top K (default 30) candidates after a diversity filter — at most
`MAX_PER_HUB` (default 6) and `MAX_PER_DATE` (default 4) each, so the shortlist is not thirty variants of
the same Tuesday via Madrid — and issue real `search_leg` queries.

Candidates share legs heavily: one `origin→MAD on the 6th` serves every
destination on that date. A leg-level cache keyed on `(origin, dest, date)`
therefore keeps real cost well below `4K`. Diversity and dedup are load-bearing,
not incidental: without them this phase is the whole cost of the search.

K defaults to 30 rather than 20 because §6.5 filters operate on the fetched set,
and filters need material to work on.

### 5.5 Phase 2 — through-fare baseline

One `origin→dest` query for each of the 3 cheapest distinct dates, restricted
to `pnr_count == 1`. This
produces the savings figure the project has always claimed and never shown:

```
Through-fare LPA→NRT   980 EUR
Split via MAD          612 EUR
You save               368 EUR (38%)
```

Where no single-ticket through-fare exists, the line reads "no single-ticket
fare available" rather than silently comparing against another split.

### 5.6 Fallback

When the primary provider does not implement `SupportsCalendar`, the engine runs
the existing grid search (phases 1/1R/2/2R) unchanged.

The window still drives the UI in this mode, so the engine expands it into
discrete dates with the existing `generate_dates(start, end, every)`, choosing
`every` so the sample stays under `FALLBACK_MAX_DATES` (default 12). The user is
told the search is sampling rather than covering the window, because in this mode
it genuinely is. A Google-only deployment therefore keeps working exactly as it
does today.

### 5.7 Cross-check

When two providers are enabled, the top 3 confirmed candidates are priced on both
and both figures are reported. Only the top 3, because doing it for all K doubles
the search cost for a diminishing benefit.

### 5.8 Expected cost

Round-trip, 8 hubs, 3 destinations:

```
phase 0   H·(1+D)·2                        = 64
phase 1   unique legs among K candidates   ≈ 90   (K=30, after dedup)
phase 2   3 dates · D · 2                  = 18
                                             ----
                                             ~172
```

| | today | two-stage |
|---|---|---|
| requests | ~640 | ~170 |
| dates covered | 10 | 91 |
| wall clock | ~10 min | under a minute |

Phase 1 is the only term that depends on K and the only one that is not exact,
because dedup savings depend on how the diversity filter spreads the shortlist.
The builder's pre-flight estimate (§6.2) computes this formula rather than
hardcoding a number.

---

## 6. Layer 3 — Bot UX

### 6.1 Problem

The current conversation is a linear chain (`DEST → TRIP_TYPE → TRIP_DAYS →
DATE_MODE → … → CONFIRM`). It has no Back at any step, so a typo at step six
means `/cancel` and start over. It requires IATA codes from memory and dates as
typed `YYYY-MM-DD` strings. Passengers, cabin class and currency are hardcoded to
`1 / economy / EUR` and cannot be changed at all. A ten-minute search reports no
progress and cannot be stopped.

### 6.2 Hub-and-spoke builder

One persistent draft message, edited in place. Each field opens a sub-state that
returns to the draft, so Back and Edit exist everywhere by construction.

```
🔎 Search draft

From      LPA · Gran Canaria
To        NRT · Tokyo Narita        [Edit]
Trip      Round-trip · 14 days      [Edit]
Window    2026-10-01 → 2026-12-15   [Edit]
Hubs      MAD BCN LIS +3            [Edit]
Who       1 adult · Economy · EUR   [Edit]
Limits    max 1 stop · ≥3h buffer   [Edit]

~170 requests · est. 55s
[ Search ]   [ Reset ]
```

`handlers/search_flow.py` is already the largest file in the repo at 593 lines
and this roughly doubles its responsibilities, so it splits:

```
handlers/search/
  __init__.py   build_search_conversation()
  builder.py    draft screen and routing
  places.py     autocomplete picker
  dates.py      month calendar keyboard
  options.py    passengers, cabin, currency, bags, limits
  progress.py   live progress message and cancel
  results.py    cards, filters, detail view
```

### 6.3 Place picker

Free text → Kiwi `places` → up to 8 inline buttons (`NRT · Tokyo Narita (Japan)`).
Destinations and hubs use multi-select with checkmark toggles and a Done button.

Pasting `JFK,LAX` still parses directly through the existing `parse_iata_codes`,
so the power-user path survives. Resolved terms are cached (§7.1).

### 6.4 Date picker

In the two-stage model the user chooses a **window**, not individual dates, so
this is a month grid with tap-start / tap-end plus presets (Next 30, Next 90, a
specific month).

Price ratings appear in two places, with different and clearly-labelled meanings:

- **On the picker**, once at least one destination is set: one `origin→dest`
  calendar request colours each day, using the **first** selected destination and
  naming it in the caption (`direct-fare signal · LPA→NRT`). With no destination
  set the grid renders uncoloured rather than guessing. Labelled as a
  *direct-fare signal* — it is the through-fare shape, not the split.
- **On the results screen**: a colour-coded strip covering the whole window,
  built from phase 0 data at no extra cost. These are real split estimates and
  are the version worth acting on.

### 6.5 Results

- Summary card: best price, savings vs through-fare (§5.5), window calendar strip.
- Paginated cards, 5 per page, `◀ 1/6 ▶`.
- Tap a card for detail: exact local times, flight numbers, layover length,
  baggage allowance and price, per-leg booking links, and a self-transfer warning
  quoting the actual buffer in hours.
- Filters — max stops, max total duration, minimum connection buffer, exclude
  carriers — re-filter the already-fetched set with **zero new requests**.
- Track is per-result, not best-only as today.

### 6.6 Progress and cancel

The engine emits phase events through a callback. The handler maintains one
message, throttled to at most one edit per 3 seconds and skipping edits whose
text has not changed, because Telegram rejects identical edits with
`BadRequest: Message is not modified`.

```
Searching… phase 2 of 4
Scanned 38/60 legs · best so far 612 EUR
[ Cancel ]
```

Cancel sets a token the leg fetcher checks between requests; the engine raises
`SearchCancelled`, which `run_and_report` catches and reports as a cancellation
rather than a failure.

---

## 7. Data model

### 7.1 Migrations

Applied through the existing `MIGRATIONS` tuple in `db.py`, which already handles
SQLite's lack of `ADD COLUMN IF NOT EXISTS`.

`searches` gains:

| Column | Why |
|---|---|
| `window_start`, `window_end` | The searched window; `dates` is kept and holds the expanded sample used in fallback mode (§5.6) |
| `provider` | Which source produced these numbers |
| `through_fare` | The §5.5 baseline, for redisplay |
| `scan_json` | Phase 0 grid, so history redisplay needs no requests |

`favorites` gains `provider`, `cabin`, `children`, `max_stops`, `min_layover`.
This is the same reasoning that made `trip_days` necessary: the scheduler must
replay the exact query shape a price was quoted under, or the next check
compares two different things and reports the difference as a price movement.

New table `place_cache (term TEXT PRIMARY KEY, places TEXT, cached_at TEXT)` with
a TTL, so autocomplete does not re-query for repeated terms.

### 7.2 Scheduler consolidation

`scheduler.py` currently recomputes the discount arithmetic independently of
`search.py`. Two implementations of one formula is precisely the shape of the
round-trip bug fixed in `e83a4d3`, so the scheduler moves onto the shared engine
and the duplicate maths is deleted. This is a correctness change, not tidying.

---

## 8. Testing

CI stays fully offline. New offline coverage:

- Kiwi response parsing against recorded fixtures, including the `AppError` path
- Seconds→minutes, `Decimal` money, relative-URL prefixing
- `None` vs `0` bag semantics (§4.3) — a Google offer must never render as
  "no checked bag included"
- Phase 0b ranking arithmetic, one-way and round-trip
- Diversity filter and leg dedup (§5.4)
- Fallback to grid search when the provider lacks `SupportsCalendar`
- Cancel token propagation
- Migration idempotence on an existing database

Plus the network-marked drift guard of §4.7, deselected by default.

Target: ~120 tests, up from 71.

## 9. Risks

1. **The endpoint is unofficial.** This is the same category as the existing
   Google Flights scraping, which the README already discloses. The Legal section
   extends to cover Kiwi; rate limiting and the single-user restriction stay. The
   drift guard is the mitigation, and loud failure (§4.5) is the safety net.
2. **Calendar figures are cached cheapest-of-day and not bookable.** Only the
   drilled-down shortlist is confirmed. Every surface showing an unconfirmed
   figure must label it an estimate.
3. **`options.partner` is an internal parameter** and is the likeliest single
   point of breakage. It is configurable via `KIWI_PARTNER` so it can be changed
   without a code edit.
4. **Layer 3 is a large rewrite of the conversation flow.** It lands last and
   behind the two layers it depends on, with a checkpoint before it starts.

## 10. Success criteria

- A round-trip search over 8 hubs, 3 destinations and a 90-day window completes
  in under two minutes and reports a best price for every day in the window.
- Results show baggage allowance and cost, exact times, flight numbers, layover
  length and working per-leg booking links.
- The bot reports savings against a genuine single-ticket through-fare, or says
  none exists.
- No step of the conversation requires knowing an IATA code or typing a date.
- Any search can be cancelled while running.
- Disabling Kiwi leaves a working Google-only bot.
- CI remains offline and green.
