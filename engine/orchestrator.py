"""Task 10: the orchestrator -- wires every engine phase into one callable.

Nothing before this module calls scan.py, shortlist.py, drill.py or grid.py
together. ``run_search`` is that call: it picks a strategy purely from what
the provider can do, runs that strategy's phases in order, attaches a
through-fare baseline, and cross-checks the cheapest results against a
second enabled provider when one exists.

Strategy selection is ``isinstance(provider, SupportsCalendar)`` --
Kiwi has a price calendar, Google does not -- never a name comparison. That
one check is the entire fallback the rest of the design leans on:

  two-stage (has a calendar):  scan -> rank -> diversify -> confirm
  grid (no calendar):          run_grid_search (its own four phases)

Both paths converge on the same finish: a through-fare baseline (so a
Google-only deployment still reports a saving whenever it can price a
single-PNR through-fare) and a cross-check tagging step (spec §5.7).

Five of this module's tuning knobs -- shortlist size, the per-hub/per-date
diversity caps, how many dates the through-fare baseline covers, and how
many dates the grid fallback samples -- are Task 13's config knobs, not
built yet. They are module-level constants with the plan's own defaults
until Task 13 wires them through as parameters; this module deliberately
does not add them to config.py itself.

The domestic-leg discount (DISCOUNT_AIRPORTS / DOMESTIC_DISCOUNT) already
exists in config.py, so it is read from there directly, the same way
search.py and scheduler.py already do -- imported by name so a test can
monkeypatch this module's own attribute, not config's.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from config import DISCOUNT_AIRPORTS, DOMESTIC_DISCOUNT
from engine.drill import PHASE as CONFIRM_PHASE
from engine.drill import PHASE_THROUGH_FARE, confirm, through_fares
from engine.fetch import LegFetcher
from engine.grid import run_grid_search
from engine.scan import CalendarGrid, rank_candidates, scan_calendars
from engine.shortlist import diversify
from models import (
    CancelToken,
    Candidate,
    Itinerary,
    Progress,
    ProgressCallback,
    SearchWindow,
)
from providers.base import FlightProvider, SupportsCalendar
from providers.registry import enabled_providers, primary_provider

STRATEGY_TWO_STAGE = "two-stage"
STRATEGY_GRID = "grid"

# Task 13's knobs (spec §5.4/§5.5/§5.6), taken as module defaults until then.
SHORTLIST_SIZE = 30
MAX_PER_HUB = 6
MAX_PER_DATE = 4
THROUGH_FARE_DATES = 3
FALLBACK_MAX_DATES = 12

# Not a Task-13 knob: LegFetcher/scan_calendars need *some* concurrency and
# per-worker delay, and run_search's own signature takes neither as a
# parameter. These match scan_calendars' own defaults.
_CONCURRENCY = 8
_DELAY = 0.0

# spec §5.7: only the cheapest few confirmed itineraries are cross-checked
# against a second enabled provider -- doing it for the whole shortlist
# would double the request count for diminishing benefit.
CROSS_CHECK_TOP_N = 3

# Review finding (Task 10): engine.drill and engine.grid each hardcode their
# own phase label at the LegFetcher.fetch_many call site, and neither module
# may be touched to parameterise it. Composed within one run_search call,
# two of those hardcoded labels collide with a label already emitted earlier
# in the same run:
#
#   - _run_grid calls run_grid_search (which itself reports its onward leg
#     as "Phase 2") and then through_fares, whose own hardcoded label is
#     also "Phase 2" -- a caller sees "Phase 2" finish, then restart at 0/M.
#   - _cross_check's confirm() call hardcodes "Phase 1" -- colliding with
#     whichever phase already used that label earlier in the same run
#     (two-stage's own confirm, or the grid fallback's first leg).
#
# Two-stage's own "Phase 0" / "Phase 1" / "Phase 2" never collide with each
# other (through_fares' "Phase 2" is only ever emitted once there, after
# confirm's "Phase 1" has already finished), so two-stage's own labels are
# left exactly as engine.drill/engine.scan report them -- unchanged, and
# still what tests/test_engine_orchestrator.py's
# test_progress_phases_arrive_in_order_and_end_complete pins.
#
# _PhaseRelabeler (below) is what relabels the two colliding calls, so every
# label a caller observes across one run_search call is unique.
GRID_THROUGH_FARE_PHASE = "Phase 2 (through-fare)"
CROSS_CHECK_PHASE = "Phase 1 (cross-check)"


class _PhaseRelabeler:
    """Wraps a caller's ``on_progress``, renaming one hardcoded phase label
    to a distinct one for whichever composed call is about to run.

    ``engine.drill.confirm``/``engine.drill.through_fares`` and
    ``engine.grid.run_grid_search`` each pass a fixed phase string straight
    to ``LegFetcher.fetch_many`` -- there is no parameter to give them a
    different one, and both modules are reviewed and out of this task's
    charter. When this orchestrator sequences two such calls behind one
    shared ``on_progress`` and both happen to use the same raw label, a
    caller keyed on phase name sees a completed phase "restart" at 0/M.

    ``retitle`` swaps the raw -> shown mapping used for the next call.
    Composed calls never overlap -- each one is awaited to completion (and
    so stops emitting ticks) before the next starts -- so one mapping
    active at a time is enough to keep every label unique across a whole
    ``run_search`` call. A raw label absent from the mapping passes through
    unchanged, which is what keeps two-stage's own "Phase 0"/"Phase 1"/
    "Phase 2" untouched wherever no relabelling is needed.
    """

    def __init__(self, on_progress: ProgressCallback | None):
        self._on_progress = on_progress
        self._relabel: dict[str, str] = {}

    def retitle(self, relabel: dict[str, str]) -> None:
        self._relabel = relabel

    def __call__(self, progress: Progress) -> None:
        if self._on_progress is None:
            return
        shown = self._relabel.get(progress.phase, progress.phase)
        if shown != progress.phase:
            progress = Progress(phase=shown, done=progress.done, total=progress.total,
                                 best_total=progress.best_total)
        self._on_progress(progress)


def _attach_through_fares(
    itineraries: list[Itinerary], fares: dict[tuple[str, str], Decimal]
) -> list[Itinerary]:
    """Attach each itinerary's through-fare baseline, keyed ``(dest, date)``.

    Shared by ``_run_two_stage`` and ``_run_grid``, which otherwise
    duplicated this verbatim (review finding, Minor).
    """
    return [itin.with_through_fare(fares.get((itin.dest, itin.date))) for itin in itineraries]


@dataclass(frozen=True)
class SearchResult:
    """Everything a caller needs to render or persist one search."""

    itineraries: list[Itinerary]
    strategy: str                  # "two-stage" | "grid"
    scan: CalendarGrid | None      # phase 0's grid, or None for "grid"
    parse_errors: int
    fetch_errors: int


def _discount() -> Decimal:
    """DOMESTIC_DISCOUNT as a Decimal, read fresh so a test's monkeypatch of
    this module's own attribute takes effect -- never a float in engine math."""
    return Decimal(str(DOMESTIC_DISCOUNT))


def _check_cancel(cancel: CancelToken | None) -> None:
    if cancel is not None:
        cancel.raise_if_cancelled()


def _pick_secondary(provider: FlightProvider) -> FlightProvider | None:
    """The other enabled provider to cross-check against, if any (spec §5.7).

    "Enabled" is whatever providers.registry.enabled_providers() currently
    reports, not just the instance run_search was handed -- so a search run
    with an explicit provider still cross-checks against the deployment's
    real secondary when the deployment has one configured. Identity
    (``is``), not equality, is what excludes the provider already driving
    the primary search from being picked as its own secondary.
    """
    providers = enabled_providers()
    if len(providers) < 2:
        return None
    for candidate in providers.values():
        if candidate is not provider:
            return candidate
    return None


async def _cross_check(
    itineraries: list[Itinerary],
    provider: FlightProvider,
    secondary: FlightProvider | None,
    *,
    origin: str,
    trip_days: int,
    hub_names: dict[str, str],
    dest_names: dict[str, str],
    adults: int,
    currency: str,
    cancel: CancelToken | None,
    on_progress: ProgressCallback | None,
) -> tuple[list[Itinerary], int, int]:
    """Tag every itinerary with the provider(s) that priced it.

    Every itinerary gets the primary provider's name unconditionally. When a
    second provider is enabled, the cheapest ``CROSS_CHECK_TOP_N`` *already
    confirmed* itineraries are re-priced against it too -- one the secondary
    can also fully confirm gets both names; one it cannot (route it doesn't
    serve, a leg it fails to price) gets only the primary's. "Both priced
    it" is a claim this only makes when it is actually true, never merely
    attempted.

    An unconfirmed itinerary is skipped for cross-check purposes (there is
    nothing bookable yet to corroborate), but still gets tagged with the
    primary's name like every other result.
    """
    tagged = [itin.with_providers(provider.name) for itin in itineraries]
    if secondary is None or not tagged:
        return tagged, 0, 0

    _check_cancel(cancel)

    confirmed_indices = [i for i, itin in enumerate(tagged) if itin.confirmed]
    top_indices = confirmed_indices[:CROSS_CHECK_TOP_N]
    if not top_indices:
        return tagged, 0, 0

    candidates = [
        Candidate(date=tagged[i].date, return_date=tagged[i].return_date,
                  hub=tagged[i].hub, dest=tagged[i].dest, dom_price=tagged[i].dom_price,
                  onward_price=tagged[i].onward_price, discount=tagged[i].discount)
        for i in top_indices
    ]
    relabel = _PhaseRelabeler(on_progress)
    relabel.retitle({CONFIRM_PHASE: CROSS_CHECK_PHASE})
    fetcher = LegFetcher(secondary, concurrency=_CONCURRENCY, delay=_DELAY,
                          cancel=cancel, on_progress=relabel)
    rechecked = await confirm(
        fetcher, candidates, origin=origin, trip_days=trip_days,
        hub_names=hub_names, dest_names=dest_names,
        discount_airports=DISCOUNT_AIRPORTS, discount=_discount(),
        adults=adults, currency=currency,
    )
    both_confirmed = {
        (r.date, r.return_date, r.hub, r.dest) for r in rechecked if r.confirmed
    }

    result = list(tagged)
    for i in top_indices:
        itin = tagged[i]
        key = (itin.date, itin.return_date, itin.hub, itin.dest)
        if key in both_confirmed:
            result[i] = itin.with_providers(provider.name, secondary.name)

    return result, fetcher.parse_errors, fetcher.fetch_errors


async def _run_two_stage(
    provider: FlightProvider,
    *,
    origin: str,
    destinations: dict[str, str],
    hubs: dict[str, str],
    window: SearchWindow,
    trip_days: int,
    adults: int,
    currency: str,
    cancel: CancelToken | None,
    on_progress: ProgressCallback | None,
) -> tuple[list[Itinerary], CalendarGrid, int, int]:
    """Phase 0 -> 0b -> 1 -> 2, cancellable between each."""
    _check_cancel(cancel)
    grid = await scan_calendars(
        provider, origin=origin, hubs=list(hubs), dests=list(destinations),
        window=window, trip_days=trip_days, adults=adults, currency=currency,
        concurrency=_CONCURRENCY, delay=_DELAY, cancel=cancel, on_progress=on_progress,
    )

    _check_cancel(cancel)
    candidates = rank_candidates(
        grid, window=window, trip_days=trip_days,
        discount_airports=DISCOUNT_AIRPORTS, discount=_discount(),
    )
    shortlist = diversify(
        candidates, limit=SHORTLIST_SIZE, max_per_hub=MAX_PER_HUB, max_per_date=MAX_PER_DATE,
    )

    _check_cancel(cancel)
    fetcher = LegFetcher(provider, concurrency=_CONCURRENCY, delay=_DELAY,
                          cancel=cancel, on_progress=on_progress)
    itineraries = await confirm(
        fetcher, shortlist, origin=origin, trip_days=trip_days,
        hub_names=hubs, dest_names=destinations,
        discount_airports=DISCOUNT_AIRPORTS, discount=_discount(),
        adults=adults, currency=currency,
    )

    _check_cancel(cancel)
    fares = await through_fares(
        fetcher, itineraries, origin=origin, trip_days=trip_days,
        dates_limit=THROUGH_FARE_DATES, adults=adults, currency=currency,
    )
    itineraries = _attach_through_fares(itineraries, fares)

    return (itineraries, grid,
            grid.parse_errors + fetcher.parse_errors,
            grid.fetch_errors + fetcher.fetch_errors)


async def _run_grid(
    provider: FlightProvider,
    *,
    origin: str,
    destinations: dict[str, str],
    hubs: dict[str, str],
    window: SearchWindow,
    trip_days: int,
    adults: int,
    currency: str,
    cancel: CancelToken | None,
    on_progress: ProgressCallback | None,
) -> tuple[list[Itinerary], None, int, int]:
    """The four-phase grid fallback, plus the through-fare baseline.

    ``run_grid_search`` and ``through_fares`` share one ``LegFetcher`` (so
    their error counters accumulate on one object, as in ``_run_two_stage``)
    behind a ``_PhaseRelabeler``: ``run_grid_search`` itself reports its
    onward leg as "Phase 2", so ``through_fares``' own hardcoded "Phase 2"
    is relabelled to ``GRID_THROUGH_FARE_PHASE`` before it runs -- otherwise
    a caller would see "Phase 2" finish, then restart at 0/M.
    """
    _check_cancel(cancel)
    relabel = _PhaseRelabeler(on_progress)
    fetcher = LegFetcher(provider, concurrency=_CONCURRENCY, delay=_DELAY,
                          cancel=cancel, on_progress=relabel)
    itineraries = await run_grid_search(
        fetcher, origin=origin, dests=list(destinations), hubs=list(hubs),
        window=window, trip_days=trip_days, hub_names=hubs, dest_names=destinations,
        discount_airports=DISCOUNT_AIRPORTS, discount=_discount(),
        adults=adults, currency=currency, max_dates=FALLBACK_MAX_DATES,
    )

    _check_cancel(cancel)
    relabel.retitle({PHASE_THROUGH_FARE: GRID_THROUGH_FARE_PHASE})
    fares = await through_fares(
        fetcher, itineraries, origin=origin, trip_days=trip_days,
        dates_limit=THROUGH_FARE_DATES, adults=adults, currency=currency,
    )
    itineraries = _attach_through_fares(itineraries, fares)

    return itineraries, None, fetcher.parse_errors, fetcher.fetch_errors


async def run_search(
    *,
    origin: str,
    destinations: dict[str, str],
    hubs: dict[str, str],
    window: SearchWindow,
    trip_days: int,
    adults: int = 1,
    currency: str = "EUR",
    provider: FlightProvider | None = None,
    cancel: CancelToken | None = None,
    on_progress: ProgressCallback | None = None,
) -> SearchResult:
    """Run a full split-ticket search and return every phase's output as one result.

    ``destinations`` and ``hubs`` are ``{code: name}`` dicts, the shape every
    caller and config already holds them in; the phase functions this calls
    want plain code lists (``list(hubs)``, ``list(destinations)``) for the
    search itself and the dicts themselves (``hubs``, ``destinations``) for
    display names -- that adaptation happens here, once, rather than pushed
    onto callers.

    ``provider`` defaults to ``providers.registry.primary_provider()``.
    Strategy is chosen by capability alone:
    ``isinstance(provider, SupportsCalendar)`` runs the two-stage pipeline;
    anything else runs the grid fallback. Both finish with a through-fare
    baseline and a cross-check against a second enabled provider, if one
    exists (spec §5.7).

    Cancellation is checked between every phase (see ``_run_two_stage`` /
    ``_run_grid`` / ``_cross_check``); a token cancelled after one phase
    completes raises ``SearchCancelled`` before the next phase issues a
    single request. An empty result -- no candidate anywhere, or no leg
    reachable from ``origin`` at all -- returns ``itineraries == []``
    cleanly; only a genuinely broken provider raises.
    """
    if provider is None:
        provider = primary_provider()

    secondary = _pick_secondary(provider)

    if isinstance(provider, SupportsCalendar):
        strategy = STRATEGY_TWO_STAGE
        itineraries, scan, parse_errors, fetch_errors = await _run_two_stage(
            provider, origin=origin, destinations=destinations, hubs=hubs,
            window=window, trip_days=trip_days, adults=adults, currency=currency,
            cancel=cancel, on_progress=on_progress,
        )
    else:
        strategy = STRATEGY_GRID
        itineraries, scan, parse_errors, fetch_errors = await _run_grid(
            provider, origin=origin, destinations=destinations, hubs=hubs,
            window=window, trip_days=trip_days, adults=adults, currency=currency,
            cancel=cancel, on_progress=on_progress,
        )

    tagged, xc_parse_errors, xc_fetch_errors = await _cross_check(
        itineraries, provider, secondary, origin=origin, trip_days=trip_days,
        hub_names=hubs, dest_names=destinations, adults=adults, currency=currency,
        cancel=cancel, on_progress=on_progress,
    )

    return SearchResult(
        itineraries=tagged,
        strategy=strategy,
        scan=scan,
        parse_errors=parse_errors + xc_parse_errors,
        fetch_errors=fetch_errors + xc_fetch_errors,
    )
