# Layer 3c — Search Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a search choose passengers, cabin, currency, max stops and min layover, and carry that choice end to end: the engine, storage, reruns, favourites and the scheduler.

**Architecture:** One frozen `SearchOptions` value (in `providers/base.py`) replaces the loose `adults=` / `currency=` parameters across the engine and builds every `LegQuery` / `CalendarQuery`. Each provider declares `Capabilities`. The builder offers only what the primary provider can do, and `run_and_report` and the scheduler check again before searching. `Offer.price` is normalised to the whole party's total at the provider boundary: Google multiplies by adults, Kiwi already returns totals.

**Tech Stack:** Python 3.10+, python-telegram-bot 21, aiosqlite, pytest (`asyncio_mode = "auto"`), ruff.

**Spec:** `docs/plans/2026-09-26-layer-3c-options-design.md`

## Global Constraints

- Python 3.10 floor. No new dependencies. The suite stays offline, except the `network`-marked drift guard.
- `Offer.price` and `RatedPrice.price` are the total for every passenger in the query (spec §3).
- Cabin values are Kiwi's `CabinClassType`: `ECONOMY`, `PREMIUM_ECONOMY`, `BUSINESS`, `FIRST_CLASS`.
- Passenger bounds: adults 1–9, children 0–8, adults + children ≤ 9.
- The defaults (`SearchOptions()`) must reproduce today's search exactly: 1 adult, 0 children, Economy, EUR, no limits.
- Every provider- or user-supplied string in Telegram HTML goes through `handlers.utils.esc`. Every `callback_data` is ≤ 64 bytes.
- Branch `feat/search-options`. Commit per task. **Never push or merge to `main`**: pushing to `main` deploys.
- `ruff check .` and `.venv/bin/pytest -q` are clean at the end of every task. E402 is not enabled in this repo's ruff config, so never write `# noqa: E402`.

## Review Focus

1. **A history rerun of a Business search on a Google-only deployment.** It must end with "Your flight source can't search Business class." and save nothing. (Task 3)
2. **A favourite whose options the current provider rejects.** The scheduler logs it and skips it; it never re-prices under a different query shape and never raises. (Task 4)
3. **A 2-adult search where the through-fare comes from Google** as cross-check or grid provider. Google prices must be ×2 before any comparison. (Task 1 pins the provider rule; Task 2 pins that the engine hands `adults=2` to every query.)
4. **`+` at a passenger bound** (9 adults, or 8 + 1 child). The draft is unchanged and an alert explains why. (Task 5)
5. **Old `searches` / `favorites` rows with `NULL` in the new columns.** They replay as today's defaults. (Tasks 3 and 4)

---

## File Map

| File | Change |
|---|---|
| `providers/base.py` | `SearchOptions`, `Capabilities`, `CABIN_LABELS`, `ALL_CABINS`, `capabilities_of` |
| `providers/kiwi.py`, `providers/google.py` | `capabilities`; Google price × adults |
| `engine/scan.py`, `engine/drill.py`, `engine/grid.py`, `engine/orchestrator.py` | `options: SearchOptions` replaces `adults` / `currency` |
| `db.py` | `searches` gains `children`, `cabin`, `max_stops`, `min_layover`; `save_search` takes them |
| `handlers/results.py` | build options from params; capability check; `ProviderError` wording; persist options; track actions pass options |
| `handlers/history.py` | rerun replays all options |
| `handlers/favorites.py` | legacy `savefav_` passes options |
| `scheduler.py` | replays options, checks capabilities, passes `dates` |
| `handlers/search/draft.py` | options fields, `options` property, `SCREEN_OPTIONS`, options row |
| `handlers/search/options.py` (new) | pure options screen and tap logic |
| `handlers/search/builder.py` | `edit:opts`, `o:` callback, `SCREEN_OPTIONS` branch |
| `results/view.py` | party and cabin in the header, "total for N passengers" |
| `bot.py` | `post_shutdown` cancels the scheduler task |
| `tests/test_kiwi_schema.py` | `CabinClassType` values (network) |
| `README.md` | Features; the price rule |

---

### Task 1: `SearchOptions`, `Capabilities`, and whole-party prices

**Files:**
- Modify: `providers/base.py` (after `LegQuery`)
- Modify: `providers/kiwi.py:157-165`, `providers/google.py:400-480`
- Test: `tests/test_providers_base.py`, `tests/test_google_provider.py`, `tests/test_kiwi_provider.py`

**Interfaces:**
- Produces:
  - `SearchOptions(adults=1, children=0, cabin="ECONOMY", currency="EUR", max_stops=None, min_layover=None)`, frozen.
  - `.leg_query(origin, dest, date) -> LegQuery`
  - `.calendar_query(origin, dest, start, end) -> CalendarQuery`
  - `.without_limits() -> SearchOptions`
  - `.as_columns() -> dict` (the six fields, keyed by the column names `db` uses)
  - `SearchOptions.from_mapping(m) -> SearchOptions`, where a missing or `None` value means the default.
  - `.passengers -> int`
  - `.party_label() -> str`, e.g. `"2 adults, 1 child · Business"`
  - `.is_default_party -> bool`
  - `Capabilities(cabins: frozenset[str], children: bool, min_layover: bool)` with `.rejects(options) -> str | None`
  - `ALL_CABINS: tuple[str, ...]` and `CABIN_LABELS: dict[str, str]`
  - `capabilities_of(provider) -> Capabilities`: the provider's declared `capabilities`, or a permissive default when it declares none (test fakes).
  - `KiwiProvider.capabilities` and `GoogleProvider.capabilities`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_providers_base.py`:

```python
# ── SearchOptions and Capabilities (Layer 3c) ───────────────────────────────

from providers.base import (
    ALL_CABINS,
    Capabilities,
    CalendarQuery,
    LegQuery,
    SearchOptions,
    capabilities_of,
)


def test_default_options_build_todays_queries():
    assert SearchOptions().leg_query("LPA", "MAD", "2026-10-01") == LegQuery(
        origin="LPA", dest="MAD", date="2026-10-01")
    assert SearchOptions().calendar_query("LPA", "MAD", "2026-10-01", "2026-10-31") == \
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")


def test_options_reach_the_queries():
    opts = SearchOptions(adults=2, children=1, cabin="BUSINESS", currency="USD",
                         max_stops=1, min_layover=90)
    q = opts.leg_query("LPA", "MAD", "2026-10-01")
    assert (q.adults, q.children, q.cabin, q.currency, q.max_stops, q.min_layover) == \
        (2, 1, "BUSINESS", "USD", 1, 90)
    c = opts.calendar_query("LPA", "MAD", "2026-10-01", "2026-10-31")
    assert (c.adults, c.children, c.cabin, c.currency) == (2, 1, "BUSINESS", "USD")


def test_without_limits_keeps_the_party_and_drops_the_limits():
    opts = SearchOptions(adults=2, cabin="BUSINESS", max_stops=0, min_layover=60)
    assert opts.without_limits() == SearchOptions(adults=2, cabin="BUSINESS")


def test_from_mapping_treats_null_as_the_default():
    row = {"adults": 2, "children": None, "cabin": None, "currency": "USD",
           "max_stops": None, "min_layover": 120, "unrelated": "x"}
    assert SearchOptions.from_mapping(row) == SearchOptions(
        adults=2, currency="USD", min_layover=120)
    assert SearchOptions.from_mapping({}) == SearchOptions()


def test_as_columns_round_trips_through_from_mapping():
    opts = SearchOptions(adults=3, children=2, cabin="FIRST_CLASS", currency="GBP",
                         max_stops=2, min_layover=180)
    assert SearchOptions.from_mapping(opts.as_columns()) == opts


def test_party_label():
    assert SearchOptions().party_label() == "1 adult · Economy"
    assert SearchOptions(adults=2, children=1, cabin="BUSINESS").party_label() == \
        "2 adults, 1 child · Business"
    assert SearchOptions(adults=1, children=2).party_label() == "1 adult, 2 children · Economy"
    assert SearchOptions().is_default_party
    assert not SearchOptions(adults=2).is_default_party
    assert SearchOptions(adults=2, children=1).passengers == 3


GOOGLE_LIKE = Capabilities(cabins=frozenset({"ECONOMY"}), children=False, min_layover=False)


def test_capabilities_reject_each_unsupported_option_in_words():
    assert GOOGLE_LIKE.rejects(SearchOptions()) is None
    assert GOOGLE_LIKE.rejects(SearchOptions(cabin="BUSINESS")) == \
        "Your flight source can't search Business class."
    assert GOOGLE_LIKE.rejects(SearchOptions(children=1)) == \
        "Your flight source can't search with children."
    assert GOOGLE_LIKE.rejects(SearchOptions(min_layover=60)) == \
        "Your flight source can't apply a minimum layover."


def test_a_provider_that_declares_nothing_is_assumed_capable():
    class Bare:
        name = "bare"

    caps = capabilities_of(Bare())
    assert caps.cabins == frozenset(ALL_CABINS)
    assert caps.rejects(SearchOptions(adults=2, children=1, cabin="FIRST_CLASS",
                                      min_layover=60)) is None
```

Append to `tests/test_google_provider.py`:

```python
async def test_prices_are_totals_for_the_whole_party(real_html, monkeypatch):
    """Google quotes per person (verified live 2026-09-26: 30 EUR for 1 and
    for 2 adults). Every Offer.price must be the party's total, as Kiwi's
    already is, or a two-adult cross-check compares totals with per-person
    prices."""
    import providers.google as google

    async def fake_fetch(*args, **kwargs):
        return real_html

    monkeypatch.setattr(google, "fetch_html", fake_fetch)
    one = await GoogleProvider().search_leg(LegQuery(origin="LPA", dest="MAD",
                                                     date="2026-10-06", adults=1))
    two = await GoogleProvider().search_leg(LegQuery(origin="LPA", dest="MAD",
                                                     date="2026-10-06", adults=2))
    assert [o.price * 2 for o in one] == [o.price for o in two]
    assert [o.checked_bag_price for o in one] == [o.checked_bag_price for o in two]


def test_google_declares_what_it_cannot_do():
    caps = GoogleProvider().capabilities
    assert caps.cabins == frozenset({"ECONOMY"})
    assert caps.children is False and caps.min_layover is False
```

Append to `tests/test_kiwi_provider.py`:

```python
def test_kiwi_declares_every_cabin_children_and_layover():
    from providers.base import ALL_CABINS
    from providers.kiwi import KiwiProvider

    caps = KiwiProvider().capabilities
    assert caps.cabins == frozenset(ALL_CABINS)
    assert caps.children and caps.min_layover
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_providers_base.py tests/test_google_provider.py tests/test_kiwi_provider.py -q`
Expected: FAIL. `SearchOptions` can't be imported (collection error), and the provider tests fail on `capabilities`.

- [ ] **Step 3: Implement**

In `providers/base.py`, after `CalendarQuery`:

```python
# ── Search options (Layer 3c) ────────────────────────────────────────────────

ALL_CABINS = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST_CLASS")
CABIN_LABELS = {
    "ECONOMY": "Economy",
    "PREMIUM_ECONOMY": "Premium economy",
    "BUSINESS": "Business",
    "FIRST_CLASS": "First",
}


@dataclass(frozen=True)
class SearchOptions:
    """What the user chose beyond route and dates, carried end to end.

    Every LegQuery and CalendarQuery the engine builds comes from here, so a
    choice can't reach one leg and miss another. The defaults are the search
    every caller ran before Layer 3c.

    ``min_layover`` is a connection *inside* one ticket (the provider's
    stopover), not the self-transfer gap between the two tickets.
    """

    adults: int = 1
    children: int = 0
    cabin: str = "ECONOMY"
    currency: str = "EUR"
    max_stops: int | None = None
    min_layover: int | None = None      # minutes

    @property
    def passengers(self) -> int:
        return self.adults + self.children

    @property
    def is_default_party(self) -> bool:
        return (self.adults, self.children, self.cabin) == (1, 0, "ECONOMY")

    def leg_query(self, origin: str, dest: str, date: str) -> LegQuery:
        return LegQuery(
            origin=origin, dest=dest, date=date, adults=self.adults,
            children=self.children, cabin=self.cabin, currency=self.currency,
            max_stops=self.max_stops, min_layover=self.min_layover,
        )

    def calendar_query(self, origin: str, dest: str, start: str, end: str) -> CalendarQuery:
        return CalendarQuery(
            origin=origin, dest=dest, start=start, end=end, adults=self.adults,
            children=self.children, cabin=self.cabin, currency=self.currency,
        )

    def without_limits(self) -> SearchOptions:
        """Same party, cabin and currency; no stop or layover limits."""
        return dataclasses.replace(self, max_stops=None, min_layover=None)

    def as_columns(self) -> dict:
        """The six fields under the column names ``searches`` and ``favorites`` use."""
        return {
            "adults": self.adults, "children": self.children, "cabin": self.cabin,
            "currency": self.currency, "max_stops": self.max_stops,
            "min_layover": self.min_layover,
        }

    @classmethod
    def from_mapping(cls, m) -> SearchOptions:
        """From a params dict or a database row. Missing or NULL is the default,
        so a row written before Layer 3c replays as the search it was."""
        def get(key, default):
            value = m.get(key)
            return default if value is None else value

        return cls(
            adults=get("adults", 1), children=get("children", 0),
            cabin=get("cabin", "ECONOMY"), currency=get("currency", "EUR"),
            max_stops=m.get("max_stops"), min_layover=m.get("min_layover"),
        )

    def party_label(self) -> str:
        people = f"{self.adults} adult{'s' if self.adults != 1 else ''}"
        if self.children:
            people += f", {self.children} child{'ren' if self.children != 1 else ''}"
        return f"{people} · {CABIN_LABELS.get(self.cabin, self.cabin)}"


@dataclass(frozen=True)
class Capabilities:
    """Which SearchOptions a provider can actually search."""

    cabins: frozenset[str]
    children: bool
    min_layover: bool

    def rejects(self, options: SearchOptions) -> str | None:
        """A sentence saying why this provider can't run *options*, or None."""
        if options.cabin not in self.cabins:
            label = CABIN_LABELS.get(options.cabin, options.cabin)
            return f"Your flight source can't search {label} class."
        if options.children and not self.children:
            return "Your flight source can't search with children."
        if options.min_layover is not None and not self.min_layover:
            return "Your flight source can't apply a minimum layover."
        return None


_UNRESTRICTED = Capabilities(cabins=frozenset(ALL_CABINS), children=True, min_layover=True)


def capabilities_of(provider: object) -> Capabilities:
    """The provider's declared capabilities. A provider that declares none is
    assumed capable; if it isn't, it raises ProviderError itself, which the
    caller also handles."""
    return getattr(provider, "capabilities", _UNRESTRICTED)
```

Add `import dataclasses` to `providers/base.py` if it isn't already imported.

In `providers/kiwi.py`, change the base import to include `ALL_CABINS, Capabilities`, and add to `KiwiProvider` under `name = "kiwi"`:

```python
    capabilities = Capabilities(cabins=frozenset(ALL_CABINS), children=True,
                                min_layover=True)
```

In `providers/google.py`, change the base import to include `Capabilities`, and add under `name = "google"`:

```python
    # encode_tfs hardcodes economy and adults only, and Google's per-airport
    # local times can't be differenced across a connection.
    capabilities = Capabilities(cabins=frozenset({"ECONOMY"}), children=False,
                                min_layover=False)
```

In `GoogleProvider.search_leg`, right after `offers = [_to_offer(f, query) for f in flights]`:

```python
        # Google quotes per person; every Offer.price is the party's total
        # (Kiwi's already are). Children can't reach here, so adults is the
        # whole party. checked_bag_price is per bag and stays as it is.
        if query.adults > 1:
            offers = [dataclasses.replace(o, price=o.price * query.adults) for o in offers]
```

Add `import dataclasses` to `providers/google.py` if missing.

- [ ] **Step 4: Run the tests and the suite**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add providers tests/test_providers_base.py tests/test_google_provider.py tests/test_kiwi_provider.py
git commit -m "feat: SearchOptions, provider capabilities, and whole-party prices

Google quotes per person and Kiwi per party (both verified live); every
Offer.price is now the party's total."
```

---

### Task 2: The engine takes `options` end to end

**Files:**
- Modify: `engine/scan.py` (`_build_jobs`, `scan_calendars`), `engine/drill.py` (`confirm`, `through_fares`), `engine/grid.py` (`run_grid_search`), `engine/orchestrator.py` (`run_search`, `_run_two_stage`, `_run_grid`, `_cross_check`)
- Modify: `handlers/results.py`, `scheduler.py` (the `run_search` callers keep today's behaviour via `options=`)
- Modify tests: `tests/test_engine_scan.py`, `tests/test_engine_drill.py`, `tests/test_engine_grid.py`
- Test: `tests/test_engine_orchestrator.py` (append)

**Interfaces:**
- Consumes: `SearchOptions` (Task 1).
- Produces:
  - `run_search(..., options: SearchOptions | None = None, ...)`, where `None` means `SearchOptions()`. The `adults=` and `currency=` parameters are gone.
  - `scan_calendars`, `confirm`, `through_fares` and `run_grid_search` each take `options: SearchOptions` in place of `adults` and `currency`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_orchestrator.py`:

```python
# ── SearchOptions reach every query (Layer 3c) ──────────────────────────────

from providers.base import SearchOptions


class RecordingCalendarProvider(FakeCalendarProvider):
    """Keeps every query object, not just its route."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.queries: list = []

    async def price_calendar(self, query):
        self.queries.append(query)
        return await super().price_calendar(query)

    async def search_leg(self, query):
        self.queries.append(query)
        return await super().search_leg(query)


BUSINESS_PAIR = SearchOptions(adults=2, children=1, cabin="BUSINESS", currency="USD",
                              max_stops=1, min_layover=60)


async def test_options_reach_every_calendar_and_leg_query(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = RecordingCalendarProvider(
        calendar_answers={
            ("LPA", "MAD"): {"2026-10-01": "29"}, ("MAD", "NRT"): {"2026-10-01": "500"},
            ("MAD", "LPA"): {"2026-10-08": "29"}, ("NRT", "MAD"): {"2026-10-08": "500"},
        },
        leg_answers={
            ("LPA", "MAD", "2026-10-01"): [_offer("25")],
            ("MAD", "NRT", "2026-10-01"): [_offer("480")],
            ("MAD", "LPA", "2026-10-08"): [_offer("25")],
            ("NRT", "MAD", "2026-10-08"): [_offer("480")],
        },
    )
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    await run_search(origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
                     window=WINDOW, trip_days=7, provider=provider, options=BUSINESS_PAIR)

    kinds = {type(q).__name__ for q in provider.queries}
    assert kinds == {"CalendarQuery", "LegQuery"}
    for q in provider.queries:
        assert (q.adults, q.children, q.cabin, q.currency) == (2, 1, "BUSINESS", "USD"), q


async def test_through_fares_keep_the_party_but_drop_the_limits(monkeypatch):
    """A split always connects at the hub. A direct-only through-fare would
    almost never exist, so the baseline is priced for the same party, cabin
    and currency but without the stop/layover limits."""
    _neutral_discount(monkeypatch)
    provider = RecordingCalendarProvider(
        calendar_answers={("LPA", "MAD"): {"2026-10-01": "29"},
                          ("MAD", "NRT"): {"2026-10-01": "500"}},
        leg_answers={("LPA", "MAD", "2026-10-01"): [_offer("25")],
                     ("MAD", "NRT", "2026-10-01"): [_offer("480")],
                     ("LPA", "NRT", "2026-10-01"): [_offer_pnr("700")]},
    )
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    await run_search(origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
                     window=WINDOW, trip_days=0, provider=provider, options=BUSINESS_PAIR)

    legs = [q for q in provider.queries if type(q).__name__ == "LegQuery"]
    through = [q for q in legs if (q.origin, q.dest) == ("LPA", "NRT")]
    split = [q for q in legs if (q.origin, q.dest) != ("LPA", "NRT")]
    assert through and all(q.max_stops is None and q.min_layover is None for q in through)
    assert through[0].cabin == "BUSINESS" and through[0].adults == 2
    assert split and all(q.max_stops == 1 and q.min_layover == 60 for q in split)


async def test_grid_options_reach_every_leg_query(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("25")],
        ("MAD", "NRT", "2026-10-01"): [_offer("480")],
    })
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    await run_search(origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
                     window=WINDOW, trip_days=0, provider=provider,
                     options=SearchOptions(adults=3, currency="GBP"))

    assert provider.seen
    assert all((q.adults, q.currency) == (3, "GBP") for q in provider.seen)


async def test_no_options_is_todays_search(monkeypatch):
    _neutral_discount(monkeypatch)
    scenario = _one_hub_scenario()
    provider = RecordingCalendarProvider(calendar_answers=scenario.calendar_answers,
                                         leg_answers=scenario.leg_answers)
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    await run_search(origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
                     window=WINDOW, trip_days=0, provider=provider)

    assert all((q.adults, q.children, q.cabin, q.currency) == (1, 0, "ECONOMY", "EUR")
               for q in provider.queries)
```

Then make the existing engine tests pass `options`:

```bash
sed -i '' 's/adults=1, currency="EUR"/options=SearchOptions()/g' tests/test_engine_scan.py tests/test_engine_drill.py tests/test_engine_grid.py
sed -i '' 's/"adults": 1, "currency": "EUR"/"options": SearchOptions()/' tests/test_engine_scan.py
for f in tests/test_engine_scan.py tests/test_engine_drill.py tests/test_engine_grid.py; do
  grep -q "SearchOptions" "$f" && sed -i '' '0,/^from providers.base import/s//from providers.base import SearchOptions\nfrom providers.base import/' "$f"
done
.venv/bin/ruff check --fix -q tests/
```

If `sed`'s `0,/re/` form isn't available (BSD sed), add `from providers.base import SearchOptions` beside the file's other `providers.base` import by hand. `ruff --fix` merges and sorts it.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_engine_orchestrator.py tests/test_engine_scan.py tests/test_engine_drill.py tests/test_engine_grid.py -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'options'`.

- [ ] **Step 3: Implement**

The rule is the same at every site: the phase function's `adults: int, currency: str` parameters become `options: SearchOptions`. Each `LegQuery(origin=a, dest=b, date=d, adults=adults, currency=currency)` becomes `options.leg_query(a, b, d)`, and each `CalendarQuery(origin=a, dest=b, start=s, end=e, adults=adults, currency=currency)` becomes `options.calendar_query(a, b, s, e)`. Add `from providers.base import SearchOptions` (or extend the existing `providers.base` import) in each module.

- `engine/scan.py`: `_build_jobs` (four `CalendarQuery` sites) and `scan_calendars`, which passes `options=options` to `_build_jobs`.
- `engine/drill.py`: in `confirm`, `queries = [options.leg_query(o, d, dt) for o, d, dt in legs]`. In `through_fares`, the two queries use `options.without_limits().leg_query(...)`, because of the test above: a through-fare is never limited by stops or layover. Update both docstrings' "Only ``adults`` and ``currency`` are forwarded" sentence to "Every option in ``options`` is forwarded" (in `through_fares`: "…except the stop and layover limits").
- `engine/grid.py`: the four `LegQuery` sites and the same docstring sentence.
- `engine/orchestrator.py`: `run_search` replaces `adults: int = 1, currency: str = "EUR"` with `options: SearchOptions | None = None` and starts with `options = options if options is not None else SearchOptions()`. `_run_two_stage`, `_run_grid` and `_cross_check` take `options: SearchOptions` and forward it to `scan_calendars`, `confirm`, `through_fares` and `run_grid_search` in place of `adults=adults, currency=currency`. Update the `run_search` docstring with one sentence: "``options`` carries passengers, cabin, currency and search limits to every query; ``None`` is ``SearchOptions()``, today's defaults."

Then the two production callers:

- `handlers/results.py`, in `run_and_report`: replace `adults=params["adults"], currency=currency,` in the `run_search(...)` call with `options=SearchOptions.from_mapping(params),` and add `from providers.base import SearchOptions, SupportsCalendar` (extending the existing import).
- `scheduler.py`: replace `adults=adults, currency=currency,` in its `run_search(...)` call with `options=SearchOptions(adults=adults, currency=currency),`. Task 4 replaces this with the full replay. Add `from providers.base import SearchOptions`.

- [ ] **Step 4: Run everything**

Run: `grep -rn "adults=adults\|currency=currency" engine/ ; .venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: the `grep` prints nothing, and everything passes.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: the engine carries SearchOptions to every query

Through-fares keep the party, cabin and currency but not the stop and
layover limits: the split always connects at the hub, so a limited
baseline would almost never exist."
```

---

### Task 3: Persist, check and replay options in searches

**Files:**
- Modify: `db.py` (schema, `MIGRATIONS`, `save_search`)
- Modify: `handlers/results.py` (`run_and_report`)
- Modify: `handlers/history.py` (`history_rerun`)
- Test: `tests/test_db.py`, `tests/test_results_run.py`, `tests/test_results_handlers.py`

**Interfaces:**
- Consumes: `SearchOptions.from_mapping`, `.as_columns`, `capabilities_of` (Task 1); `run_search(options=)` (Task 2).
- Produces: `save_search(..., children=0, cabin="ECONOMY", max_stops=None, min_layover=None)`, plus `searches.children`, `searches.cabin`, `searches.max_stops` and `searches.min_layover`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_db.py`, add `"children", "cabin", "max_stops", "min_layover"` to `SEARCHES_NEW_COLUMNS`. The migration test asserts every new column is `NULL` on an old row, which is correct because `NULL` reads as the default. Then append:

```python
async def test_save_search_records_the_options(temp_db):
    search_id = await db_module.save_search(
        origin="LPA", destinations=["NRT"], dates=["2026-10-01"], hubs=["MAD"],
        adults=2, currency="USD", best_price=None, best_route=None, results=None,
        children=1, cabin="BUSINESS", max_stops=1, min_layover=90,
    )
    row = await db_module.get_search_by_id(search_id)
    assert (row["children"], row["cabin"], row["max_stops"], row["min_layover"]) == \
        (1, "BUSINESS", 1, 90)
```

Append to `tests/test_results_run.py`:

```python
class _GoogleLike(_FakeCalendarProvider):
    from providers.base import Capabilities
    capabilities = Capabilities(cabins=frozenset({"ECONOMY"}), children=False,
                                min_layover=False)


async def test_an_option_the_source_cannot_search_is_refused_before_searching(
    temp_db, fake_engine, monkeypatch,
):
    """Review Focus #1: a rerun of a Business search on a Google-only
    deployment says why it can't run -- never "No routes found"."""
    monkeypatch.setattr(results_module, "primary_provider", lambda: _GoogleLike())
    bot = FakeBot()

    await run_and_report(bot, chat_id=1, params=_base_params(cabin="BUSINESS"))

    assert bot.messages[-1] == "Your flight source can't search Business class."
    assert fake_engine["calls"] == []
    assert await db_module.get_searches(1) == []


async def test_a_provider_error_escaping_the_engine_is_explained(temp_db, monkeypatch):
    from providers.base import ProviderError

    async def raising_run_search(**kwargs):
        raise ProviderError("Google cannot express cabin 'BUSINESS'")

    monkeypatch.setattr(results_module, "run_search", raising_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
    bot = FakeBot()

    await run_and_report(bot, chat_id=1, params=_base_params())

    assert "can't run this search" in bot.messages[-1]
    assert "check the bot logs" not in bot.messages[-1]


async def test_the_options_are_searched_and_stored(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(date="2026-09-01")]
    params = _base_params(dates=["2026-09-01"], adults=2, children=1, cabin="BUSINESS",
                          currency="USD", max_stops=1, min_layover=60)

    await run_and_report(FakeBot(), chat_id=1, params=params)

    assert fake_engine["calls"][0]["options"] == SearchOptions(
        adults=2, children=1, cabin="BUSINESS", currency="USD", max_stops=1, min_layover=60)
    row = (await db_module.get_searches(1))[0]
    assert SearchOptions.from_mapping(row) == fake_engine["calls"][0]["options"]
```

Add `from providers.base import SearchOptions` to that file's imports.

Append to `tests/test_results_handlers.py`:

```python
async def test_history_rerun_replays_every_option(temp_db):
    from handlers.history import history_rerun
    from providers.base import SearchOptions

    sid = await _saved(None, adults=2, children=1, cabin="BUSINESS", currency="USD",
                       max_stops=1, min_layover=60)
    scheduled = []
    ctx = _context()
    ctx.application.create_task = lambda coro, update=None: scheduled.append(coro)

    class Query(FakeQuery):
        async def edit_message_text(self, text, **kw):
            pass

    update = SimpleNamespace(callback_query=Query(f"hist_rerun_{sid}"),
                             effective_user=SimpleNamespace(id=_OWNER_ID),
                             effective_chat=SimpleNamespace(id=1))
    await history_rerun(update, ctx)

    coro = scheduled[0]
    params = coro.cr_frame.f_locals["params"]
    coro.close()
    assert SearchOptions.from_mapping(params) == SearchOptions(
        adults=2, children=1, cabin="BUSINESS", currency="USD", max_stops=1, min_layover=60)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_db.py tests/test_results_run.py tests/test_results_handlers.py -q`
Expected: FAIL. The new columns are missing, the capability check doesn't exist, and the rerun drops the options.

- [ ] **Step 3: Implement**

`db.py`:
1. In the `searches` `CREATE TABLE`, after `view_json    TEXT               -- results view state...`, add `,` and:

   ```sql
       children     INTEGER,           -- NULL = 0 (pre-3c)
       cabin        TEXT,              -- CabinClassType; NULL = ECONOMY
       max_stops    INTEGER,           -- NULL = no limit
       min_layover  INTEGER            -- minutes; NULL = no minimum
   ```

   Keep the comma rules valid: the new last line has no trailing comma.
2. Append to `MIGRATIONS`:

   ```python
       # Layer 3c: the rest of the query shape, so reruns replay it exactly.
       # No SQL defaults: NULL means "the default", read by SearchOptions.
       ("searches", "children", "INTEGER"),
       ("searches", "cabin", "TEXT"),
       ("searches", "max_stops", "INTEGER"),
       ("searches", "min_layover", "INTEGER"),
   ```

3. `save_search` gains `children: int = 0, cabin: str = "ECONOMY", max_stops: int | None = None, min_layover: int | None = None` after `strategy`. Add them to the `INSERT` column list, with four more `?` and four more tuple values. Add a docstring sentence: `*children*, *cabin*, *max_stops* and *min_layover* complete the query shape (see SearchOptions).`

`handlers/results.py`, in `run_and_report`:
- Right after `currency = params["currency"]`, build the options, and refuse before creating the run or any message beyond one:

  ```python
      options = SearchOptions.from_mapping(params)
      refusal = capabilities_of(primary_provider()).rejects(options)
      if refusal is not None:
          # Checked here, not only in the builder: a history rerun or an old
          # button can carry an option this deployment's provider can't run.
          await render_anchor(bot, chat_id, message_id, refusal, view.MENU_ROWS)
          return
  ```

- Pass `options=options` to `run_search` (replacing Task 2's `SearchOptions.from_mapping(params)`).
- Add an `except ProviderError:` clause **before** `except ValueError`:

  ```python
      except ProviderError:
          # A bare ProviderError means the provider can't express this query
          # at all. It is a capability gap, not a failure, and must never
          # read as "No routes found".
          logger.warning("Provider cannot run the search %s", params, exc_info=True)
          failure = "Your flight source can't run this search with these options."
  ```

- In `save_search(...)`, replace `adults=params["adults"],` and `currency=currency,` with `**options.as_columns(),`.
- Imports: `from providers.base import ProviderError, SearchOptions, SupportsCalendar, capabilities_of`.

`handlers/history.py`, in `history_rerun`: replace the `"adults": ...` and `"currency": ...` lines of `params` with `**SearchOptions.from_mapping(row).as_columns(),` and add `from providers.base import SearchOptions`.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: store, check and replay search options

A search the provider can't express is refused up front with a reason,
never reported as no routes. History reruns replay every option."
```

---

### Task 4: Favourites and the scheduler replay options; exact dates

**Files:**
- Modify: `handlers/results.py` (`t`/`T` track branch), `handlers/favorites.py` (`save_favorite`), `scheduler.py`
- Test: `tests/test_results_handlers.py`, `tests/test_handlers_favorites.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `SearchOptions.from_mapping`, `.as_columns`, `capabilities_of`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_results_handlers.py`:

```python
async def test_tracking_stores_the_search_s_options(temp_db):
    sid = await _saved(serialize([standard_one_way()]), adults=2, children=1,
                       cabin="BUSINESS", currency="USD", max_stops=1, min_layover=60)
    await _tap(f"r:{sid}:t:0")
    fav = (await db_module.get_favorites())[0]
    assert (fav["adults"], fav["children"], fav["cabin"], fav["currency"],
            fav["max_stops"], fav["min_layover"]) == (2, 1, "BUSINESS", "USD", 1, 60)
```

Append to `tests/test_handlers_favorites.py`:

```python
async def test_save_favorite_stores_the_search_s_options(temp_db, monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    search_id = await _save_search_with({"adults": 2, "children": 1, "cabin": "BUSINESS",
                                         "max_stops": 0})
    await save_favorite(_update(f"savefav_{search_id}"), None)
    fav = (await db_module.get_favorites())[0]
    assert (fav["adults"], fav["children"], fav["cabin"], fav["max_stops"]) == \
        (2, 1, "BUSINESS", 0)
```

Append to `tests/test_scheduler.py`:

```python
from providers.base import Capabilities, SearchOptions


async def test_the_scheduler_replays_every_option_and_the_exact_dates(temp_db, fake_engine):
    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=2, currency="USD",
        price=None, check_dates=["2026-09-01", "2026-09-20"], trip_days=0,
        children=1, cabin="BUSINESS", max_stops=1, min_layover=60,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    call = fake_engine["calls"][0]
    assert call["options"] == SearchOptions(adults=2, children=1, cabin="BUSINESS",
                                            currency="USD", max_stops=1, min_layover=60)
    assert call["dates"] == ["2026-09-01", "2026-09-20"]


async def test_a_favourite_its_provider_cannot_search_is_skipped(
    temp_db, fake_engine, monkeypatch,
):
    """Review Focus #2: never re-price under a different query shape."""
    class GoogleLike:
        name = "google"
        capabilities = Capabilities(cabins=frozenset({"ECONOMY"}), children=False,
                                    min_layover=False)

    monkeypatch.setattr(scheduler_module, "get_provider", lambda name: GoogleLike())
    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=None, check_dates=["2026-09-01"], trip_days=0, provider="google",
        cabin="BUSINESS",
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    assert fake_engine["calls"] == []


async def test_a_pre_3c_favourite_replays_as_the_defaults(temp_db, fake_engine):
    """Review Focus #5: NULL option columns mean today's search."""
    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=None, check_dates=["2026-09-01"], trip_days=0,
    )
    await check_favorites(FakeBot(), owner_chat_id=1)
    assert fake_engine["calls"][0]["options"] == SearchOptions()
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_handlers.py tests/test_handlers_favorites.py tests/test_scheduler.py -q`
Expected: FAIL. The favourites store default options, and the scheduler passes neither `options` nor `dates` as expected.

- [ ] **Step 3: Implement**

`handlers/results.py`, in the `t`/`T` branch of `on_results`: replace `adults=row.get("adults") or 1, currency=meta.currency,` with `**SearchOptions.from_mapping(row).as_columns(),`.

`handlers/favorites.py`, in `save_favorite`: replace `adults=row.get("adults") or 1,` and `currency=row.get("currency") or "EUR",` with `**SearchOptions.from_mapping(row).as_columns(),` and add `from providers.base import SearchOptions`.

`scheduler.py`:
- Delete `adults = fav["adults"]` and replace `currency = fav["currency"]` with `options = SearchOptions.from_mapping(fav)` and `currency = options.currency` (the alert text still uses `currency`).
- After `provider = get_provider(...) if provider_name else primary_provider()`:

  ```python
          # The options a price was quoted under must be replayed exactly. A
          # provider that can't express them would price a different query
          # and report the difference as a movement, so skip instead.
          refusal = capabilities_of(provider).rejects(options)
          if refusal is not None:
              logger.warning("Favorite %d skipped: %s", fav_id, refusal)
              continue
  ```

- In the `run_search(...)` call, replace Task 2's `options=SearchOptions(adults=adults, currency=currency),` with `options=options,` and add `dates=all_dates,`. Add a comment above `dates=`: `# The exact tracked dates: on a grid (calendar-less) provider the engine would otherwise resample the window and could drop the last date.`
- Import `capabilities_of` beside `SearchOptions`.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: favourites and the scheduler replay the full query shape

The scheduler now passes the exact tracked dates and skips a favourite
its provider can't express rather than pricing a different query."
```

---

### Task 5: The options screen in the builder

**Files:**
- Modify: `handlers/search/draft.py` (fields, `options` property, `SCREEN_OPTIONS`, options row, `to_params`)
- Create: `handlers/search/options.py`
- Modify: `handlers/search/builder.py` (`edit_field`, `_show`, `option_tap`, handler registration)
- Test: `tests/test_search_draft.py`, `tests/test_search_options.py` (new), `tests/test_search_builder.py`

**Interfaces:**
- Consumes: `SearchOptions`, `Capabilities`, `CABIN_LABELS`, `ALL_CABINS`, `capabilities_of`.
- Produces:
  - `SearchDraft` gains `children: int = 0`, `cabin: str = "ECONOMY"`, `max_stops: int | None = None` and `min_layover: int | None = None`.
  - `SearchDraft.options -> SearchOptions`
  - `SCREEN_OPTIONS = "options"`
  - `options.render(draft, caps) -> tuple[str, Rows]`
  - `options.apply(draft, data: str, caps) -> SearchDraft | str`, which returns an error string when the tap is refused.
  - Callbacks: `o:a:+`, `o:a:-`, `o:k:+`, `o:k:-`, `o:c:<CABIN>`, `o:cur:<CODE>`, `o:s:<any|0|1|2>`, `o:l:<any|60|120|180>`.

- [ ] **Step 1: Write the failing tests**

`tests/test_search_options.py`:

```python
"""handlers.search.options: the options screen, as pure data."""
from __future__ import annotations

import pytest

from handlers.search.draft import SearchDraft
from handlers.search.options import CURRENCIES, apply, render
from providers.base import ALL_CABINS, Capabilities

KIWI = Capabilities(cabins=frozenset(ALL_CABINS), children=True, min_layover=True)
GOOGLE = Capabilities(cabins=frozenset({"ECONOMY"}), children=False, min_layover=False)


def _draft(**kw):
    return SearchDraft(origin="LPA", origin_name="Gran Canaria", **kw)


def _data(rows):
    return [b.data for row in rows for b in row]


@pytest.mark.parametrize(("data", "field", "value"), [
    ("o:a:+", "adults", 2), ("o:k:+", "children", 1), ("o:c:BUSINESS", "cabin", "BUSINESS"),
    ("o:cur:USD", "currency", "USD"), ("o:s:0", "max_stops", 0), ("o:s:any", "max_stops", None),
    ("o:l:120", "min_layover", 120), ("o:l:any", "min_layover", None),
])
def test_each_tap_sets_its_field(data, field, value):
    assert getattr(apply(_draft(), data, KIWI), field) == value


def test_minus_never_goes_below_one_adult_or_zero_children():
    assert apply(_draft(), "o:a:-", KIWI) == "At least one adult travels."
    assert apply(_draft(), "o:k:-", KIWI) == "There are no children to remove."


def test_nine_passengers_is_the_ceiling():
    """Review Focus #4."""
    assert apply(_draft(adults=9), "o:a:+", KIWI) == "Nine passengers is the most one search can hold."
    assert apply(_draft(adults=8, children=1), "o:k:+", KIWI) == \
        "Nine passengers is the most one search can hold."


def test_a_tap_the_provider_cannot_honour_is_refused():
    assert apply(_draft(), "o:c:BUSINESS", GOOGLE) == "Your flight source can't search Business class."
    assert apply(_draft(), "o:k:+", GOOGLE) == "Your flight source can't search with children."
    assert apply(_draft(), "o:l:60", GOOGLE) == "Your flight source can't apply a minimum layover."


def test_garbage_is_refused_not_raised():
    for data in ("o:", "o:zz:1", "o:s:7", "o:cur:XXX", "o:c:COACH", "o:l:5"):
        assert isinstance(apply(_draft(), data, KIWI), str)


def test_render_marks_choices_and_hides_what_google_cannot_do():
    text, rows = render(_draft(cabin="BUSINESS"), KIWI)
    labels = {b.data: b.label for row in rows for b in row}
    assert labels["o:c:BUSINESS"].startswith("•")
    assert "o:k:+" in labels and "o:l:60" in labels
    assert set(CURRENCIES) <= {d.split(":")[2] for d in labels if d.startswith("o:cur:")}

    _, g_rows = render(_draft(), GOOGLE)
    g_data = _data(g_rows)
    assert "o:c:BUSINESS" not in g_data
    assert "o:k:+" not in g_data and "o:l:60" not in g_data
    assert "back" in g_data


def test_every_callback_fits_64_bytes():
    _, rows = render(_draft(), KIWI)
    assert all(len(d.encode()) <= 64 for d in _data(rows))
```

Append to `tests/test_search_draft.py`:

```python
from providers.base import SearchOptions


def test_the_draft_carries_its_options_to_the_engine():
    d = SearchDraft(origin="LPA", origin_name="Gran Canaria", adults=2, children=1,
                    cabin="BUSINESS", currency="USD", max_stops=1, min_layover=60)
    assert d.options == SearchOptions(adults=2, children=1, cabin="BUSINESS",
                                      currency="USD", max_stops=1, min_layover=60)
    assert SearchOptions.from_mapping(d.to_params()) == d.options


def test_the_options_row_replaces_the_read_only_footer():
    d = SearchDraft(origin="LPA", origin_name="Gran Canaria", adults=2, cabin="BUSINESS",
                    currency="USD", max_stops=1)
    text, rows = d.render()
    assert "2 adults · Business · USD · ≤1 stop" in text
    assert "edit:opts" in [b.data for row in rows for b in row]
```

Append to `tests/test_search_builder.py`:

```python
async def test_an_options_tap_edits_the_draft_and_redraws(monkeypatch):
    from providers.base import ALL_CABINS, Capabilities

    class Kiwi:
        name = "kiwi"
        capabilities = Capabilities(cabins=frozenset(ALL_CABINS), children=True,
                                    min_layover=True)

    monkeypatch.setattr(builder, "primary_provider", lambda: Kiwi())
    context = _context()
    _set_draft(context, _draft(screen=builder.SCREEN_OPTIONS))
    update = _cb_update("o:c:BUSINESS")

    await builder.option_tap(update, context)

    assert context.user_data[builder._DRAFT].cabin == "BUSINESS"
    assert context.bot.edits or context.bot.sends


async def test_a_refused_options_tap_alerts_and_keeps_the_draft(monkeypatch):
    from providers.base import Capabilities

    class GoogleLike:
        name = "google"
        capabilities = Capabilities(cabins=frozenset({"ECONOMY"}), children=False,
                                    min_layover=False)

    monkeypatch.setattr(builder, "primary_provider", lambda: GoogleLike())
    context = _context()
    draft = _draft(screen=builder.SCREEN_OPTIONS)
    _set_draft(context, draft)
    update = _cb_update("o:c:BUSINESS")

    await builder.option_tap(update, context)

    assert context.user_data[builder._DRAFT] == draft
    assert update.callback_query.answers == [
        ("Your flight source can't search Business class.", True)]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_search_options.py tests/test_search_draft.py tests/test_search_builder.py -q`
Expected: FAIL. There is no `handlers.search.options` module, and `SearchDraft` has no `children` field.

- [ ] **Step 3: Implement `handlers/search/options.py`**

```python
"""The options screen: passengers, cabin, currency and search limits.

Pure, like draft.py and dates.py: it takes a draft and the provider's
Capabilities and returns text + button specs, or a new draft. The builder
does the Telegram part. Only what the provider can honour is drawn, and a
tap it can't honour is refused with the provider's own reason.
"""
from __future__ import annotations

from handlers.search.draft import Button, Rows, SearchDraft
from providers.base import CABIN_LABELS, Capabilities, SearchOptions

MAX_PASSENGERS = 9
CURRENCIES = ("EUR", "USD", "GBP")
STOP_CHOICES = (0, 1, 2)
LAYOVER_CHOICES = (60, 120, 180)      # minutes
_FULL = "Nine passengers is the most one search can hold."
_STALE = "That option is out of date."


def _mark(label: str, on: bool) -> str:
    return f"• {label}" if on else label


def _stops_label(n: int) -> str:
    return "Direct" if n == 0 else f"≤{n} stop{'s' if n > 1 else ''}"


def render(draft: SearchDraft, caps: Capabilities) -> tuple[str, Rows]:
    text = (f"<b>Search options</b>\n\n{draft.options.party_label()} · {draft.currency}"
            "\n\n<i>Prices are always the total for everyone travelling.</i>")
    rows: Rows = [[Button("Adults", "o:n"), Button("−", "o:a:-"),
                   Button(str(draft.adults), "o:n"), Button("+", "o:a:+")]]
    if caps.children:
        rows.append([Button("Children", "o:n"), Button("−", "o:k:-"),
                     Button(str(draft.children), "o:n"), Button("+", "o:k:+")])
    cabins = [c for c in CABIN_LABELS if c in caps.cabins]
    if len(cabins) > 1:
        rows.append([Button(_mark(CABIN_LABELS[c], draft.cabin == c), f"o:c:{c}")
                     for c in cabins])
    rows.append([Button(_mark(c, draft.currency == c), f"o:cur:{c}") for c in CURRENCIES])
    rows.append([Button(_mark("Any stops", draft.max_stops is None), "o:s:any")]
                + [Button(_mark(_stops_label(n), draft.max_stops == n), f"o:s:{n}")
                   for n in STOP_CHOICES])
    if caps.min_layover:
        rows.append([Button(_mark("Any layover", draft.min_layover is None), "o:l:any")]
                    + [Button(_mark(f"≥{m // 60}h layover", draft.min_layover == m),
                              f"o:l:{m}") for m in LAYOVER_CHOICES])
    rows.append([Button("⬅️ Back", "back")])
    return text, rows


def _passengers(draft: SearchDraft, key: str, sign: str,
                caps: Capabilities) -> SearchDraft | str:
    if sign not in ("+", "-"):
        return _STALE
    step = 1 if sign == "+" else -1
    adults, children = draft.adults, draft.children
    if key == "a":
        if adults + step < 1:
            return "At least one adult travels."
        adults += step
    else:
        if step > 0 and not caps.children:
            return caps.rejects(SearchOptions(children=1))
        if children + step < 0:
            return "There are no children to remove."
        children += step
    if adults + children > MAX_PASSENGERS:
        return _FULL
    return draft.with_(adults=adults, children=children)


def _choice(value: str, choices: tuple[int, ...]) -> int | None | str:
    """"any" -> None, a listed number -> int, anything else -> _STALE."""
    if value == "any":
        return None
    if value.isdigit() and int(value) in choices:
        return int(value)
    return _STALE


def apply(draft: SearchDraft, data: str, caps: Capabilities) -> SearchDraft | str:
    """The draft after one tap, or the reason the tap is refused."""
    parts = data.split(":")
    if len(parts) != 3:
        return _STALE
    _, key, value = parts

    if key in ("a", "k"):
        return _passengers(draft, key, value, caps)
    if key == "c" and value in CABIN_LABELS:
        new = draft.with_(cabin=value)
    elif key == "cur" and value in CURRENCIES:
        new = draft.with_(currency=value)
    elif key == "s":
        stops = _choice(value, STOP_CHOICES)
        if stops == _STALE:
            return _STALE
        new = draft.with_(max_stops=stops)
    elif key == "l":
        layover = _choice(value, LAYOVER_CHOICES)
        if layover == _STALE:
            return _STALE
        new = draft.with_(min_layover=layover)
    else:
        return _STALE

    refusal = caps.rejects(new.options)
    return refusal if refusal is not None else new
```

The screen stays open after a tap: every options tap re-renders the options screen. `back` returns to the draft, as on every other screen.

- [ ] **Step 4: Implement the draft and builder changes**

`handlers/search/draft.py`:
- Add `SCREEN_OPTIONS = "options"` with the other screens.
- Add the fields after `currency`: `children: int = 0`, `cabin: str = "ECONOMY"`, `max_stops: int | None = None`, `min_layover: int | None = None`.
- Add the property:

  ```python
      @property
      def options(self) -> SearchOptions:
          return SearchOptions(adults=self.adults, children=self.children, cabin=self.cabin,
                               currency=self.currency, max_stops=self.max_stops,
                               min_layover=self.min_layover)
  ```

- `to_params`: replace the `"adults"` and `"currency"` lines with `**self.options.as_columns(),`.
- In `render`, replace the `<i>… · Economy · …</i>` footer lines with:

  ```python
              f"<b>Options</b> {esc(self._options_line())}",
  ```

  and add:

  ```python
      def _options_line(self) -> str:
          parts = [self.options.party_label(), self.currency]
          if self.max_stops is not None:
              parts.append("direct" if self.max_stops == 0
                           else f"≤{self.max_stops} stop{'s' if self.max_stops > 1 else ''}")
          if self.min_layover is not None:
              parts.append(f"layover ≥{self.min_layover // 60}h")
          return " · ".join(parts)
  ```

- Add the button row `[Button("✏️ Options", "edit:opts")]` above the Search/Reset row.
- Import `SearchOptions` from `providers.base`. This module stays telegram-free, and `providers.base` imports no Telegram.

`handlers/search/builder.py`:
- Import `from handlers.search import options as options_mod`, add `SCREEN_OPTIONS` to the draft import, and `from providers.base import capabilities_of`.
- `edit_field`: add `"opts": SCREEN_OPTIONS` to `screens`.
- `_show`: add a branch before the final `else`:

  ```python
      elif draft.screen == SCREEN_OPTIONS:
          text, rows = options_mod.render(draft, capabilities_of(primary_provider()))
  ```

- New handler:

  ```python
  @owner_only_callback
  async def option_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
      """One tap on the options screen. A refused tap alerts and changes nothing."""
      query = update.callback_query
      if query.data == "o:n":
          await query.answer()
          return BUILDING
      result = options_mod.apply(_draft_of(context), query.data,
                                 capabilities_of(primary_provider()))
      if isinstance(result, str):
          await query.answer(result, show_alert=True)
          return BUILDING
      await query.answer()
      _store(context, result)
      return await _show(update, context)
  ```

- Register `CallbackQueryHandler(option_tap, pattern=r"^o:")` in the `BUILDING` state list.

- [ ] **Step 5: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS. If an existing draft test asserted the old `Economy` footer text, update it to the new options line and record the change in the ledger as a ruling.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: choose passengers, cabin, currency and limits in the builder"
```

---

### Task 6: Show the party in the results

**Files:**
- Modify: `results/view.py` (`SearchMeta.options`, the summary header, the detail price line)
- Test: `tests/test_results_view.py`

**Interfaces:**
- Produces: `SearchMeta.options: SearchOptions = SearchOptions()`, a new last field with a default, so existing constructions keep working. `from_row` reads it with `SearchOptions.from_mapping(row)`.

- [ ] **Step 1: Write the failing tests**

```python
from providers.base import SearchOptions


def test_the_summary_names_a_non_default_party():
    meta = replace(META, options=SearchOptions(adults=2, children=1, cabin="BUSINESS"))
    text, _ = summary(meta, StoredResults(_many(1), True), Filters(), 1)
    assert "2 adults, 1 child · Business" in text
    assert "adult" not in summary(META, StoredResults(_many(1), True), Filters(), 1)[0]


def test_the_detail_says_the_price_is_for_everyone():
    meta = replace(META, options=SearchOptions(adults=2, children=1))
    assert "total for 3 passengers" in detail(meta, StoredResults(_many(1), True), 0, 1)[0]
    assert "passengers" not in detail(META, StoredResults(_many(1), True), 0, 1)[0]


def test_search_meta_reads_the_options():
    row = {"id": 1, "adults": 2, "children": None, "cabin": "BUSINESS", "currency": "USD"}
    assert SearchMeta.from_row(row).options == SearchOptions(adults=2, cabin="BUSINESS",
                                                             currency="USD")
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_results_view.py -q`
Expected: FAIL: `SearchMeta` has no field `options`.

- [ ] **Step 3: Implement**

In `results/view.py`:
- Add the `SearchMeta` field `options: SearchOptions = SearchOptions()` last. In `from_row`, pass `options=SearchOptions.from_mapping(row)`.
- In `summary`, change the header so that after `{trip}` it inserts ` · {esc(meta.options.party_label())}` when `not meta.options.is_default_party`.
- In `detail`, after `{trip}{marker}` in the first line, append ` · total for {n} passengers` when `meta.options.passengers > 1`.
- Import `SearchOptions` from `providers.base`.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add results/view.py tests/test_results_view.py
git commit -m "feat: results say who the price is for"
```

---

### Task 7: Cancel the scheduler on shutdown

**Files:**
- Modify: `bot.py` (`post_shutdown`)
- Test: `tests/test_bot.py` (new)

- [ ] **Step 1: Write the failing test**

`tests/test_bot.py`:

```python
"""bot.post_shutdown: a clean exit on every deploy restart."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bot as bot_module


async def test_post_shutdown_cancels_the_scheduler_task(monkeypatch):
    """Without this, every restart logs 'Task was destroyed but it is
    pending!' -- once per deploy, now that deploys are automatic."""
    import providers.registry as registry

    async def no_providers():
        return None

    monkeypatch.setattr(registry, "close_all", no_providers)
    task = asyncio.create_task(asyncio.sleep(3600))
    app = SimpleNamespace(bot_data={"scheduler_task": task})

    await bot_module.post_shutdown(app)

    assert task.cancelled()


async def test_post_shutdown_without_a_scheduler_is_fine(monkeypatch):
    import providers.registry as registry

    async def no_providers():
        return None

    monkeypatch.setattr(registry, "close_all", no_providers)
    await bot_module.post_shutdown(SimpleNamespace(bot_data={}))
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/pytest tests/test_bot.py -q`
Expected: the first test FAILS (`task.cancelled()` is False).

- [ ] **Step 3: Implement**

In `bot.py`, `post_shutdown` becomes:

```python
async def post_shutdown(application: Application) -> None:
    """Stop the scheduler and release provider connection pools on the way out."""
    from providers.registry import close_all

    task = application.bot_data.get("scheduler_task")
    if task is not None:
        # Cancelled and awaited, so shutdown never leaves it pending: that
        # is what logged "Task was destroyed but it is pending!" on every
        # restart.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("Scheduler stopped.")

    await close_all()
    logger.info("Provider connections closed.")
```

Add `import contextlib` to `bot.py`.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_bot.py
git commit -m "fix: cancel the scheduler task on shutdown"
```

---

### Task 8: Drift guard and README

**Files:**
- Modify: `tests/test_kiwi_schema.py` (network-marked)
- Modify: `README.md`

- [ ] **Step 1: Add the drift guard**

Append to `tests/test_kiwi_schema.py`:

```python
def test_cabin_class_values_are_still_the_four_we_send():
    """SearchOptions.cabin is sent verbatim as CabinClassType."""
    from providers.base import ALL_CABINS

    q = 'query { __type(name: "CabinClassType") { enumValues { name } } }'
    response = httpx.post(ENDPOINT, headers=HEADERS, json={"query": q}, timeout=30)
    values = {v["name"] for v in response.json()["data"]["__type"]["enumValues"]}
    assert set(ALL_CABINS) <= values
```

Run: `.venv/bin/pytest -m network tests/test_kiwi_schema.py -q -k cabin`
Expected: PASS against the live API. If the network is unavailable, say so in the ledger. The default suite deselects this test either way.

- [ ] **Step 2: Update the README**

1. Under **Features**, after the "Guided search" bullet, add:

   ```markdown
   - **Search options** — adults and children, cabin (Economy to First),
     currency, and search-time limits on stops and layover. The builder only
     offers what the configured flight source can actually search; anything
     else is refused with a reason, never reported as "no flights".
   ```

2. Under **Notes on some decisions**, add a section:

   ```markdown
   **Every price is for the whole party.** The two sources disagree: Kiwi
   quotes the total for every passenger, Google quotes per person (verified
   against both live). Mixing them would make a two-adult cross-check compare
   a total with a single fare. So the rule lives at the provider boundary:
   every `Offer.price` is the party's total, and the Google adapter multiplies
   by the number of adults. Nothing downstream — the discount arithmetic, the
   savings line, the scheduler's drop threshold — has to know.
   ```

3. In **Setup**'s configuration sentence, nothing changes. Options are chosen per search, not configured.

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_kiwi_schema.py README.md
git commit -m "docs: search options and the whole-party price rule"
```
