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
"""
from __future__ import annotations

from decimal import Decimal

from engine.fetch import LegFetcher
from engine.shortlist import legs_for
from models import Candidate, Itinerary
from providers.base import LegQuery, Offer

PHASE = "Phase 1"


def _cheapest(offers: list[Offer]) -> Offer:
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
            dom_out=_cheapest(dom_out_offers),
            dom_ret=_cheapest(dom_ret_offers) if dom_ret_offers else None,
            onward_out=_cheapest(onward_out_offers),
            onward_ret=_cheapest(onward_ret_offers) if onward_ret_offers else None,
        ))

    itineraries.sort(key=lambda itin: itin.total)
    return itineraries
