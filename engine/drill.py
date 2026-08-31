"""Phase 1: confirm a shortlist of calendar candidates against real offers.

Phase 0 and 0b rank candidates from calendars alone: cached cheapest-of-day
figures that are never bookable. This module is what turns those estimates
into itineraries a user could actually buy -- by fetching real offers for
every leg the shortlist needs, and re-pricing every candidate *from those
offers*, never by trusting the calendar guess forward. A candidate whose
legs are not all present is dropped rather than shown at a partial price:
half an itinerary is not bookable, which is the same "unbookable, not
cheap" rule phase 0b applies to calendar gaps.

Task 6's ``legs_for`` already collapsed the shortlist to the unique
(origin, dest, date) triples it needs -- so the shared domestic leg is
fetched once no matter how many destinations share it -- and this module
fetches all of them in a single ``LegFetcher.fetch_many`` call, so the
concurrency cap applies across the whole phase and progress is one
monotonic count rather than one per candidate.

Phase 2 (``through_fares``, below) prices the honest baseline the rest of
this project has always claimed to beat: what the airline itself would
charge to fly the same route as a single, ordinary through-fare. That claim
is only honest when the offer priced is actually a single ticket. Kiwi
happily returns multi-PNR "self-transfer" itineraries -- effectively its own
split ticket -- stitched together into one search result; pricing one of
those as "the through-fare" would compare our split against another split
and call the difference a saving. So only an offer with ``pnr_count == 1``
counts, and Google's offers, which never report a PNR count at all, can
never qualify either -- ``None`` is not evidence of anything.
"""
from __future__ import annotations

from decimal import Decimal

from engine.fetch import LegFetcher
from engine.shortlist import legs_for
from models import Candidate, Itinerary
from providers.base import LegQuery, Offer

PHASE = "Phase 1"
PHASE_THROUGH_FARE = "Phase 2"


def cheapest(offers: list[Offer]) -> Offer:
    """The cheapest of a leg's offers -- ``search_leg`` documents cheapest-first,
    but this is picked explicitly rather than trusting that ordering."""
    return min(offers, key=lambda o: o.price)


async def confirm(
    fetcher: LegFetcher,
    candidates: list[Candidate],
    *,
    origin: str,
    trip_days: int,
    hub_names: dict[str, str],
    dest_names: dict[str, str],
    discount_airports: set[str],
    discount: Decimal,
    adults: int,
    currency: str,
) -> list[Itinerary]:
    """Confirm ``candidates`` against real offers, re-pricing from them.

    Builds ``LegQuery`` objects from ``legs_for`` and fetches them all in one
    ``fetch_many`` call, then assembles one ``Itinerary`` per candidate from
    the cached results, taking each leg's cheapest offer. A candidate whose
    legs are not all present -- domestic and onward outbound always,
    domestic and onward return too when ``trip_days > 0`` -- is dropped
    rather than emitted with a missing side reading as free.

    Only ``adults`` and ``currency`` are forwarded onto each ``LegQuery``;
    ``min_layover``, ``children`` and ``cabin`` are left at their defaults
    because this function accepts no such parameters, and ``GoogleProvider``
    raises a bare ``ProviderError`` -- aborting the whole phase -- for any of
    them being set, which would break a Google-only deployment.

    The domestic-leg discount applies only when ``hub in discount_airports``;
    every other candidate's domestic leg pays full price. Results are sorted
    cheapest (``.total``) first. An empty ``candidates`` list makes no
    requests and returns ``[]``. Cancellation propagates as
    ``SearchCancelled`` from the underlying ``fetch_many`` call.
    """
    legs = legs_for(candidates, origin=origin, trip_days=trip_days)
    queries = [
        LegQuery(origin=leg_origin, dest=leg_dest, date=leg_date,
                  adults=adults, currency=currency)
        for leg_origin, leg_dest, leg_date in legs
    ]
    offers_by_leg = await fetcher.fetch_many(queries, phase=PHASE)

    round_trip = trip_days > 0
    itineraries: list[Itinerary] = []

    for cand in candidates:
        dom_out_offers = offers_by_leg.get((origin, cand.hub, cand.date))
        onward_out_offers = offers_by_leg.get((cand.hub, cand.dest, cand.date))
        if dom_out_offers is None or onward_out_offers is None:
            continue

        dom_ret_offers: list[Offer] | None = None
        onward_ret_offers: list[Offer] | None = None
        if round_trip:
            dom_ret_offers = offers_by_leg.get((cand.hub, origin, cand.return_date))
            onward_ret_offers = offers_by_leg.get((cand.dest, cand.hub, cand.return_date))
            if dom_ret_offers is None or onward_ret_offers is None:
                continue

        rate = discount if cand.hub in discount_airports else Decimal(0)

        itineraries.append(Itinerary(
            date=cand.date,
            return_date=cand.return_date if round_trip else "",
            hub=cand.hub,
            hub_name=hub_names.get(cand.hub, cand.hub),
            dest=cand.dest,
            dest_name=dest_names.get(cand.dest, cand.dest),
            discount=rate,
            dom_out=cheapest(dom_out_offers),
            dom_ret=cheapest(dom_ret_offers) if dom_ret_offers else None,
            onward_out=cheapest(onward_out_offers),
            onward_ret=cheapest(onward_ret_offers) if onward_ret_offers else None,
        ))

    itineraries.sort(key=lambda itin: itin.total)
    return itineraries


def _cheapest_single_pnr(offers: list[Offer] | None) -> Offer | None:
    """The cheapest offer among ``offers`` that is a genuine single ticket.

    ``None`` (the leg had no results at all) and "no offer has pnr_count == 1"
    are the same outcome here: no through-fare can be substantiated. Note
    ``o.pnr_count == 1`` rather than ``!= None`` -- a bare ``!=`` would treat
    Google's ``None`` as passing, when it is exactly the case this guards
    against.
    """
    if not offers:
        return None
    single_pnr = [o for o in offers if o.pnr_count == 1]
    if not single_pnr:
        return None
    return cheapest(single_pnr)


async def through_fares(
    fetcher: LegFetcher,
    itineraries: list[Itinerary],
    *,
    origin: str,
    trip_days: int,
    dates_limit: int = 3,
    adults: int,
    currency: str,
) -> dict[tuple[str, str], Decimal]:
    """Price the through-fare baseline for the cheapest itineraries, honestly.

    This is a baseline, not a second search: it prices only the
    ``dates_limit`` cheapest *distinct* dates among ``itineraries`` (ranked by
    each date's cheapest itinerary total), and only the (destination, date)
    pairs that actually occur on those dates -- one query per pair, fetched
    in a single ``fetch_many`` call under ``PHASE_THROUGH_FARE``.

    Only an offer with ``pnr_count == 1`` counts as a through-fare -- see the
    module docstring. A pair with no qualifying offer is simply absent from
    the result; it is never recorded as a saving of zero. A round trip needs
    both the outbound (``origin`` -> dest on ``date``) and return (dest ->
    ``origin`` on the itinerary's ``return_date``) legs to each independently
    qualify -- a single-PNR outbound paired with a multi-PNR return yields no
    entry.

    ``trip_days`` is the authority on trip shape, exactly as in ``confirm``:
    ``trip_days > 0`` means round-trip, using each itinerary's own
    ``return_date`` for the return leg's query date. An empty ``itineraries``
    list makes no requests and returns ``{}``.
    """
    round_trip = trip_days > 0

    ordered = sorted(itineraries, key=lambda itin: itin.total)
    selected_dates: list[str] = []
    for itin in ordered:
        if itin.date not in selected_dates:
            selected_dates.append(itin.date)
        if len(selected_dates) >= dates_limit:
            break
    dates = set(selected_dates)

    pairs: dict[tuple[str, str], str] = {}  # (dest, date) -> return_date
    for itin in itineraries:
        if itin.date in dates:
            pairs.setdefault((itin.dest, itin.date), itin.return_date)

    queries: list[LegQuery] = []
    for (dest, date), return_date in pairs.items():
        queries.append(LegQuery(origin=origin, dest=dest, date=date,
                                  adults=adults, currency=currency))
        if round_trip:
            queries.append(LegQuery(origin=dest, dest=origin, date=return_date,
                                      adults=adults, currency=currency))

    offers_by_leg = await fetcher.fetch_many(queries, phase=PHASE_THROUGH_FARE)

    fares: dict[tuple[str, str], Decimal] = {}
    for (dest, date), return_date in pairs.items():
        out_offer = _cheapest_single_pnr(offers_by_leg.get((origin, dest, date)))
        if out_offer is None:
            continue
        if not round_trip:
            fares[(dest, date)] = out_offer.price
            continue
        ret_offer = _cheapest_single_pnr(offers_by_leg.get((dest, origin, return_date)))
        if ret_offer is None:
            continue
        fares[(dest, date)] = out_offer.price + ret_offer.price

    return fares
