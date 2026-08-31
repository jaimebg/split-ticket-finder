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
many dates the grid fallback samples -- are Task 13's config knobs
(SHORTLIST_SIZE, MAX_PER_HUB, MAX_PER_DATE, THROUGH_FARE_DATES,
FALLBACK_MAX_DATES), read from config.py the same way DISCOUNT_AIRPORTS /
DOMESTIC_DISCOUNT already were -- imported by name so a test can monkeypatch
this module's own attribute, not config's. CROSS_CHECK_TOP_N is deliberately
left out of that list: it stays a plain module constant, not a deployment
setting.

A sixth config value, MAX_WINDOW_DAYS, is enforced rather than merely read:
``run_search`` rejects a window wider than it for a calendar-capable
provider, before phase 0 issues a single request (see ``run_search``'s own
docstring). It is not applied to the grid strategy, which never consults a
calendar and is not subject to the limit that value documents.

Concurrency and per-worker delay are also config knobs, chosen per call
rather than fixed once for the whole module: ``_budget`` reads
KIWI_CONCURRENCY/KIWI_DELAY for a ``SupportsCalendar`` provider and
MAX_CONCURRENCY/DEFAULT_DELAY for anything else (review finding I3 -- Kiwi
tolerates far more load than scraping Google does, and the two must not
share one budget). It is keyed on capability, the same way strategy
selection is, never on a provider's ``name`` string.

``run_search`` accepts optional ``concurrency``/``delay`` overrides (review
follow-up to I3) that flow through every fetcher a single call creates --
the primary phases' and, separately, the cross-check's. ``None`` (the
default) means "use ``_budget``'s per-provider config value", exactly the
behaviour before these parameters existed; a caller that supplies one
(a test, typically -- real pacing has no reason to differ per call in
production) gets it verbatim instead. This is what lets I3's behaviour be
asserted directly rather than merely endured: a test can pin that the real
per-provider default is used when nothing is passed, and separately run
every other test at ``delay=0`` without weakening -- or even touching --
the default itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from config import (
    DEFAULT_DELAY,
    DISCOUNT_AIRPORTS,
    DOMESTIC_DISCOUNT,
    FALLBACK_MAX_DATES,
    KIWI_CONCURRENCY,
    KIWI_DELAY,
    MAX_CONCURRENCY,
    MAX_PER_DATE,
    MAX_PER_HUB,
    MAX_WINDOW_DAYS,
    SHORTLIST_SIZE,
    THROUGH_FARE_DATES,
)
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
from providers.base import FlightProvider, ProviderError, SupportsCalendar
from providers.registry import enabled_providers, primary_provider

logger = logging.getLogger(__name__)

STRATEGY_TWO_STAGE = "two-stage"
STRATEGY_GRID = "grid"


def _budget(
    provider: FlightProvider,
    *,
    concurrency: int | None = None,
    delay: float | None = None,
) -> tuple[int, float]:
    """The (concurrency, delay) a fetcher against *provider* should use.

    Review finding I3: this used to be a flat ``_CONCURRENCY = 8`` /
    ``_DELAY = 0.0`` regardless of provider, which left KIWI_CONCURRENCY,
    KIWI_DELAY, MAX_CONCURRENCY and DEFAULT_DELAY dead config -- and ran a
    Google-only deployment's scraper at 8-wide with zero spacing, where
    Layer 1 used 4-wide with a 2.5s delay specifically to avoid getting
    blocked. Keyed on ``isinstance(provider, SupportsCalendar)``, the same
    capability check strategy selection uses -- not on ``provider.name`` --
    since that is what actually distinguishes "tolerates load" (Kiwi's API)
    from "must be throttled" (scraping Google) in this codebase, and stays
    correct even if a future calendar-capable provider isn't named "kiwi".

    ``concurrency``/``delay``, independently, override the per-provider
    config default when given (review follow-up to I3): the engine reaching
    into config and imposing it on every caller with no way to say
    otherwise made I3's own pacing untestable except by genuinely sleeping
    through it. ``None`` for either (the default) keeps that value
    config-derived exactly as before -- this is an added seam, not a
    weakened default.
    """
    default_concurrency, default_delay = (
        (KIWI_CONCURRENCY, KIWI_DELAY) if isinstance(provider, SupportsCalendar)
        else (MAX_CONCURRENCY, DEFAULT_DELAY)
    )
    return (
        default_concurrency if concurrency is None else concurrency,
        default_delay if delay is None else delay,
    )

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
    concurrency: int | None = None,
    delay: float | None = None,
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

    A bare ``ProviderError`` from the secondary's ``confirm`` call (review
    finding I8) is caught here, logged, and treated as "the secondary could
    not corroborate anything" -- unlike the primary search, where the same
    exception is deliberately left to propagate and abort the phase (see
    ``engine/fetch.py``'s and ``engine/scan.py``'s own comments). The
    difference is what each call is *for*: the primary's result is the
    search, and a misconfigured query there must not silently look like "no
    flights found". The secondary's result here is optional corroboration of
    an already-complete, already-bookable primary result -- discarding that
    result because a second, non-essential provider cannot answer this kind
    of query at all would throw away real, confirmed itineraries for a
    capability gap that is not the primary's problem.
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
    resolved_concurrency, resolved_delay = _budget(secondary, concurrency=concurrency, delay=delay)
    fetcher = LegFetcher(secondary, concurrency=resolved_concurrency, delay=resolved_delay,
                          cancel=cancel, on_progress=relabel)
    try:
        rechecked = await confirm(
            fetcher, candidates, origin=origin, trip_days=trip_days,
            hub_names=hub_names, dest_names=dest_names,
            discount_airports=DISCOUNT_AIRPORTS, discount=_discount(),
            adults=adults, currency=currency,
        )
    except ProviderError:
        logger.warning(
            "Cross-check against %s aborted (provider cannot answer this kind "
            "of query); keeping the primary-only result.", secondary.name,
        )
        return tagged, fetcher.parse_errors, fetcher.fetch_errors

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
    concurrency: int | None = None,
    delay: float | None = None,
) -> tuple[list[Itinerary], CalendarGrid, int, int]:
    """Phase 0 -> 0b -> 1 -> 2, cancellable between each."""
    resolved_concurrency, resolved_delay = _budget(provider, concurrency=concurrency, delay=delay)
    _check_cancel(cancel)
    grid = await scan_calendars(
        provider, origin=origin, hubs=list(hubs), dests=list(destinations),
        window=window, trip_days=trip_days, adults=adults, currency=currency,
        concurrency=resolved_concurrency, delay=resolved_delay, cancel=cancel,
        on_progress=on_progress,
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
    fetcher = LegFetcher(provider, concurrency=resolved_concurrency, delay=resolved_delay,
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
    dates: list[str] | None = None,
    concurrency: int | None = None,
    delay: float | None = None,
) -> tuple[list[Itinerary], None, int, int]:
    """The four-phase grid fallback, plus the through-fare baseline.

    ``run_grid_search`` and ``through_fares`` share one ``LegFetcher`` (so
    their error counters accumulate on one object, as in ``_run_two_stage``)
    behind a ``_PhaseRelabeler``: ``run_grid_search`` itself reports its
    onward leg as "Phase 2", so ``through_fares``' own hardcoded "Phase 2"
    is relabelled to ``GRID_THROUGH_FARE_PHASE`` before it runs -- otherwise
    a caller would see "Phase 2" finish, then restart at 0/M.

    ``dates``, when given, is forwarded to ``run_grid_search`` as
    ``explicit_dates`` -- see C1 in the Layer 2 review: a caller with a
    discrete list of dates it actually wants searched (rather than an
    arbitrary window to sample from) must have the grid path search those
    dates, not silently resample its own from the window instead.
    """
    resolved_concurrency, resolved_delay = _budget(provider, concurrency=concurrency, delay=delay)
    _check_cancel(cancel)
    relabel = _PhaseRelabeler(on_progress)
    fetcher = LegFetcher(provider, concurrency=resolved_concurrency, delay=resolved_delay,
                          cancel=cancel, on_progress=relabel)
    itineraries = await run_grid_search(
        fetcher, origin=origin, dests=list(destinations), hubs=list(hubs),
        window=window, trip_days=trip_days, hub_names=hubs, dest_names=destinations,
        discount_airports=DISCOUNT_AIRPORTS, discount=_discount(),
        adults=adults, currency=currency, max_dates=FALLBACK_MAX_DATES,
        explicit_dates=dates,
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
    dates: list[str] | None = None,
    concurrency: int | None = None,
    delay: float | None = None,
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

    ``dates``, when given, is a caller's discrete list of departure dates it
    actually wants searched -- distinct from ``window``, which both
    strategies need regardless (the two-stage calendar covers a whole window
    for one request; the grid fallback needs *some* range to sample from
    when it has no such discrete list). It is used only by the grid
    fallback, forwarded as ``run_grid_search``'s ``explicit_dates`` so those
    dates are queried directly instead of being resampled from ``window``
    (review finding C1). The two-stage strategy ignores it: it already prices
    every day in ``window`` for the same one calendar request, so there is
    nothing to resample away in the first place -- a caller that only wants
    a subset of those days back must filter the returned itineraries itself.

    ``concurrency``/``delay``, independently, override ``_budget``'s
    per-provider config default for every fetcher this call creates --
    the primary phases' and the cross-check's alike (review follow-up to
    I3). ``None`` (the default) means "use the config value for whichever
    provider each fetcher actually talks to"; this is the seam that lets a
    test assert I3's real per-provider default is used when nothing is
    passed, and separately run at ``delay=0`` everywhere else, without
    weakening the default itself.

    Cancellation is checked between every phase (see ``_run_two_stage`` /
    ``_run_grid`` / ``_cross_check``); a token cancelled after one phase
    completes raises ``SearchCancelled`` before the next phase issues a
    single request. An empty result -- no candidate anywhere, or no leg
    reachable from ``origin`` at all -- returns ``itineraries == []``
    cleanly; only a genuinely broken provider raises.

    A ``window`` wider than ``MAX_WINDOW_DAYS`` raises ``ValueError`` when
    the chosen provider has a calendar -- that is a verified physical limit
    of the endpoint phase 0 relies on, and a wider request there does not
    error, it silently prices fewer days than asked for. The grid strategy
    never consults a calendar, so it is exempt from that check entirely.
    """
    if provider is None:
        provider = primary_provider()

    # MAX_WINDOW_DAYS is a verified physical limit of the price-calendar
    # endpoint the two-stage strategy depends on (Kiwi's, confirmed against
    # the live API): a wider window doesn't error there, it silently returns
    # fewer days than asked for -- a search that looks complete and quietly
    # isn't. Checked here, before phase 0 issues a single request, so an
    # oversized window fails loudly instead. The grid strategy never
    # consults a calendar, so the limit is not physically binding there --
    # it already bounds its own request count via FALLBACK_MAX_DATES no
    # matter how wide the window is -- so it is deliberately exempt rather
    # than rejected for a reason that only applies to the other path.
    if isinstance(provider, SupportsCalendar) and window.days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"window {window.start} to {window.end} covers {window.days} days, "
            f"more than the {MAX_WINDOW_DAYS}-day limit the price calendar "
            f"supports (MAX_WINDOW_DAYS)"
        )

    secondary = _pick_secondary(provider)

    if isinstance(provider, SupportsCalendar):
        strategy = STRATEGY_TWO_STAGE
        itineraries, scan, parse_errors, fetch_errors = await _run_two_stage(
            provider, origin=origin, destinations=destinations, hubs=hubs,
            window=window, trip_days=trip_days, adults=adults, currency=currency,
            cancel=cancel, on_progress=on_progress, concurrency=concurrency, delay=delay,
        )
    else:
        strategy = STRATEGY_GRID
        itineraries, scan, parse_errors, fetch_errors = await _run_grid(
            provider, origin=origin, destinations=destinations, hubs=hubs,
            window=window, trip_days=trip_days, adults=adults, currency=currency,
            cancel=cancel, on_progress=on_progress, dates=dates,
            concurrency=concurrency, delay=delay,
        )

    tagged, xc_parse_errors, xc_fetch_errors = await _cross_check(
        itineraries, provider, secondary, origin=origin, trip_days=trip_days,
        hub_names=hubs, dest_names=destinations, adults=adults, currency=currency,
        cancel=cancel, on_progress=on_progress, concurrency=concurrency, delay=delay,
    )

    return SearchResult(
        itineraries=tagged,
        strategy=strategy,
        scan=scan,
        parse_errors=parse_errors + xc_parse_errors,
        fetch_errors=fetch_errors + xc_fetch_errors,
    )
