"""Grid-search fallback for providers with no price calendar.

``GoogleProvider`` implements ``FlightProvider`` but not ``SupportsCalendar``
-- Google Flights has no price-calendar endpoint scraping can reach -- so on
a Google-only deployment phase 0 (``engine/scan.py``) has nothing to scan.
This module is what a Google-only deployment runs instead: the same
four-phase grid search ``search.py:run_search`` has always run, ported onto
``engine.fetch.LegFetcher`` and the ``Itinerary`` type so callers do not have
to branch on which era of type they get back.

This is a port, not a redesign. In particular it keeps the narrowing that is
the existing implementation's whole efficiency win: phase 2 (the onward leg)
only queries a (hub, date) pair that phase 1 (the domestic leg) proved has a
flight. A hub nothing reaches on any date is never queried for onward
flights at all.

Because there is no calendar, a wide window can only be *sampled* here, not
*covered* the way phase 0 covers every day of a window for the same one
request. This module picks at most ``max_dates`` evenly spaced departure
dates out of the window, however wide the window is -- a Google-only
deployment sees fewer distinct dates than a calendar-backed one would, and
that is a real limitation to be upfront about, not a bug to paper over.

The four phases, each one ``LegFetcher.fetch_many`` call, run in order:

  1.  origin -> hub          (the discounted leg)
  1R. hub -> origin          (round-trip only)
  2.  hub -> destination     (the international leg)
  2R. destination -> hub     (round-trip only)

Return legs are always queried as separate one-way searches, never as a
round-trip query -- the whole premise of a split ticket is that the legs are
booked separately, so a round-trip quote is not a price anyone could pay.
"""
from __future__ import annotations

from decimal import Decimal

from engine.fetch import LegFetcher
from engine.pairing import best_pair
from models import Itinerary, SearchWindow, add_days, generate_dates
from providers.base import Offer, SearchOptions

# Google has no calendar, so a wide window can only be sampled rather than
# covered. This caps how many distinct departure dates a fallback search
# ever queries, no matter how wide the requested window is.
FALLBACK_MAX_DATES = 12


def _sample_dates(window: SearchWindow, max_dates: int) -> list[str]:
    """Expand *window* to at most *max_dates* evenly spaced dates.

    ``generate_dates`` steps by a fixed number of days starting from
    ``window.start``. Stepping by ``ceil(total_days / max_dates)`` is the
    smallest integer step guaranteed to keep the resulting sample at or
    under the cap for any window length, while sampling every day (step 1)
    whenever the window already fits within the cap.
    """
    total_days = window.days
    every = 1 if total_days <= max_dates else -(-total_days // max_dates)
    return generate_dates(window.start, window.end, every)


def _sample_explicit_dates(dates: list[str], max_dates: int) -> list[str]:
    """Evenly sample at most *max_dates* dates out of an explicit list.

    Unlike ``_sample_dates``, every date here was asked for directly by a
    caller that already knows exactly which dates it wants -- so this always
    keeps the first and last date (a window-based ``every`` step can silently
    drop either end, which is the C1 regression this function exists to avoid
    for the grid path), sampling only the interior when there are more than
    *max_dates* of them. Returns them sorted, de-duplicated.
    """
    ordered = sorted(set(dates))
    if len(ordered) <= max_dates:
        return ordered
    if max_dates <= 1:
        return [ordered[0]]
    step = (len(ordered) - 1) / (max_dates - 1)
    indices = sorted({round(i * step) for i in range(max_dates)})
    return [ordered[i] for i in indices]


async def run_grid_search(
    fetcher: LegFetcher,
    *,
    origin: str,
    dests: list[str],
    hubs: list[str],
    window: SearchWindow,
    trip_days: int,
    hub_names: dict[str, str],
    dest_names: dict[str, str],
    discount_airports: set[str],
    discount: Decimal,
    options: SearchOptions,
    max_dates: int = FALLBACK_MAX_DATES,
    explicit_dates: list[str] | None = None,
) -> list[Itinerary]:
    """Run the four-phase grid search and return confirmed itineraries.

    ``dests`` and ``hubs`` are IATA codes; ``hub_names``/``dest_names`` supply
    display names the same way ``engine.drill.confirm`` does. An empty
    ``dests`` or ``hubs`` makes no requests at all and returns ``[]`` --
    there is nothing to search, so there is nothing worth phase 1 querying
    either.

    ``explicit_dates``, when given, is queried directly (sampled down to
    *max_dates* only if it has more entries than that) instead of evenly
    sampling ``window`` -- a caller that already knows exactly which dates it
    wants (the guided search flow's discrete date list, say) must have those
    dates actually searched, not silently replaced by a window-derived sample
    that can miss the very dates that were asked for (see C1 in the Layer 2
    review: a 20-day window sampled at the default cap dropped the window's
    own end date). ``None`` (the default) preserves the old window-sampling
    behaviour for callers with no discrete list of their own, such as the
    scheduler's price-tracking re-checks.

    Every returned ``Itinerary`` is built only from combinations where every
    leg the trip shape needs (domestic and onward outbound always, both
    returns too when ``trip_days > 0``) actually has an offer; a combination
    missing any leg is dropped rather than shown at a partial price, so
    every result's ``.confirmed`` is ``True``. Each direction takes the
    cheapest pair in the best connection-risk level (engine.pairing). The domestic-leg discount applies only when
    ``hub in discount_airports``. Results are sorted cheapest (``.total``)
    first.

    Every option in ``options`` is forwarded onto each ``LegQuery``. An option
    the provider can't express is refused before the search starts (see
    ``providers.base.Capabilities``), so it never reaches this phase.
    """
    if not dests or not hubs:
        return []

    round_trip = trip_days > 0
    dates = (
        _sample_explicit_dates(explicit_dates, max_dates)
        if explicit_dates
        else _sample_dates(window, max_dates)
    )

    shift = -1 if options.overnight else 0     # domestic day relative to the onward day

    # ── Phase 1: outbound domestic leg (origin -> hubs) ─────────────────────
    phase1_queries = [
        options.leg_query(origin, hub, add_days(date, shift))
        for hub in hubs
        for date in dates
    ]
    dom_out = await fetcher.fetch_many(phase1_queries, phase="Phase 1")
    if not dom_out:
        return []
    # (hub, onward date) pairs phase 1 proved reachable.
    reachable = [(hub, add_days(dom_date, -shift)) for (_, hub, dom_date) in dom_out]

    # ── Phase 1R: return domestic leg (hubs -> origin), a day late overnight ─
    dom_ret: dict[tuple[str, str, str], list[Offer]] = {}
    if round_trip:
        phase1r_queries = [
            options.leg_query(hub, origin, add_days(date, trip_days - shift))
            for (hub, date) in reachable
        ]
        dom_ret = await fetcher.fetch_many(phase1r_queries, phase="Phase 1R")

    # ── Phase 2: outbound onward leg (hubs -> destinations) ─────────────────
    # Only (hub, date) pairs phase 1 proved reachable are queried here -- a
    # hub with no flights from origin on any sampled date never appears in
    # reachable, so it is never queried for onward flights either.
    phase2_queries = [
        options.leg_query(hub, dest, date)
        for (hub, date) in reachable
        for dest in dests
    ]
    onward_out = await fetcher.fetch_many(phase2_queries, phase="Phase 2")

    # ── Phase 2R: return onward leg (destinations -> hubs) ──────────────────
    onward_ret: dict[tuple[str, str, str], list[Offer]] = {}
    if round_trip:
        phase2r_queries = [
            options.leg_query(dest, hub, add_days(date, trip_days))
            for (hub, dest, date) in onward_out
        ]
        onward_ret = await fetcher.fetch_many(phase2r_queries, phase="Phase 2R")

    # ── Combine ──────────────────────────────────────────────────────────
    itineraries: list[Itinerary] = []
    for (hub, date) in reachable:
        dom_out_offers = dom_out[(origin, hub, add_days(date, shift))]
        for dest in dests:
            onward_out_offers = onward_out.get((hub, dest, date))
            if not onward_out_offers:
                continue

            return_date = add_days(date, trip_days) if round_trip else ""
            dom_ret_offers: list[Offer] | None = None
            onward_ret_offers: list[Offer] | None = None
            if round_trip:
                dom_ret_offers = dom_ret.get((hub, origin, add_days(return_date, -shift)))
                onward_ret_offers = onward_ret.get((dest, hub, return_date))
                if not dom_ret_offers or not onward_ret_offers:
                    continue

            rate = discount if hub in discount_airports else Decimal(0)
            dom_o, onward_o = best_pair(dom_out_offers, onward_out_offers, first_discount=rate)
            onward_r = dom_r = None
            if round_trip:
                onward_r, dom_r = best_pair(onward_ret_offers, dom_ret_offers,
                                            second_discount=rate)

            itineraries.append(Itinerary(
                date=date,
                return_date=return_date,
                hub=hub,
                hub_name=hub_names.get(hub, hub),
                dest=dest,
                dest_name=dest_names.get(dest, dest),
                discount=rate,
                dom_out=dom_o,
                dom_ret=dom_r,
                onward_out=onward_o,
                onward_ret=onward_r,
                overnight=options.overnight,
            ))

    itineraries.sort(key=lambda itin: itin.total)
    return itineraries
