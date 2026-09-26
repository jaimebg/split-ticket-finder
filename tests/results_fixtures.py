"""Builders for itineraries with real offers, shared by the Layer 3b tests.

The standard one-way lands in Madrid at 10:00 and leaves at 13:00, a 3-hour
self-transfer. The standard round trip adds a return with the same 3-hour
gap. Totals with the default 75% discount:

    one-way     100 * 0.25 + 500             = 525.00
    round trip  (100 + 90) * 0.25 + 500 + 450 = 997.50
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from models import Itinerary
from providers.base import Offer, Segment


def seg(origin, dest, dep, arr, *, carrier="IB", name="Iberia", no=None,
        duration=120) -> Segment:
    return Segment(
        origin=origin, dest=dest, carrier=carrier, carrier_name=name,
        flight_no=no or f"{carrier}100", duration=duration,
        dep_local=datetime.fromisoformat(dep) if dep else None,
        arr_local=datetime.fromisoformat(arr) if arr else None,
    )


def offer(price, *segments, stops=None, duration=None,
          url="https://example.test/book", **kw) -> Offer:
    segs = list(segments)
    return Offer(
        price=Decimal(price), currency="EUR",
        airlines=sorted({s.carrier_name for s in segs}),
        stops=len(segs) - 1 if stops is None else stops,
        duration=sum(s.duration for s in segs) if duration is None else duration,
        segments=segs, provider="kiwi", booking_url=url, **kw,
    )


def dom_out():
    return offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00"))


def onward_out():
    return offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00",
                            carrier="JL", name="JAL", duration=780))


def onward_ret():
    return offer("450", seg("NRT", "MAD", "2026-10-15T11:00", "2026-10-15T18:00",
                            carrier="JL", name="JAL", duration=840))


def dom_ret():
    return offer("90", seg("MAD", "LPA", "2026-10-15T21:00", "2026-10-15T22:45",
                           duration=165))


def one_way(*, dom=None, onward=None, date="2026-10-01", hub="MAD", dest="NRT",
            discount="0.75", through_fare=None, **kw) -> Itinerary:
    return Itinerary(
        date=date, return_date="", hub=hub, hub_name="Madrid", dest=dest,
        dest_name="Tokyo", discount=Decimal(discount), dom_out=dom,
        onward_out=onward,
        through_fare=Decimal(through_fare) if through_fare is not None else None,
        **kw,
    )


def standard_one_way(**kw) -> Itinerary:
    return one_way(dom=dom_out(), onward=onward_out(), **kw)


def standard_round_trip(**kw) -> Itinerary:
    fields = {
        "date": "2026-10-01", "return_date": "2026-10-15", "hub": "MAD",
        "hub_name": "Madrid", "dest": "NRT", "dest_name": "Tokyo",
        "discount": Decimal("0.75"), "dom_out": dom_out(),
        "onward_out": onward_out(), "onward_ret": onward_ret(),
        "dom_ret": dom_ret(),
    }
    fields.update(kw)
    return Itinerary(**fields)
