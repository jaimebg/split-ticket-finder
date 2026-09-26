"""Engine itineraries as models an assistant can read.

Pure: no MCP, no I/O. The rules match the Telegram view. Savings keep their
sign (negative when the single ticket is cheaper). Unknown bags stay unknown.
A ticket with no real offer carries no price and nothing bookable, so an
assistant can't present it as a fare you could buy.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from engine.risk import itinerary_risk
from models import Itinerary
from providers.base import Offer
from results.view import bags_text

_CENTS = Decimal("0.01")


class TicketOut(BaseModel):
    role: str
    price: float | None = None           # before the discount
    paid: float | None = None            # after it; only domestic tickets differ
    from_airport: str | None = None
    to_airport: str | None = None
    stops: int | None = None
    duration_minutes: int | None = None
    flights: list[str] = []
    departs: str | None = None           # local ISO datetime, first segment
    arrives: str | None = None           # local ISO datetime, last segment
    booking_url: str | None = None
    bags: str | None = None


class ItineraryOut(BaseModel):
    total: float
    status: str
    date: str
    return_date: str
    dom_date: str
    dom_return_date: str
    hub: str
    hub_name: str
    dest: str
    dest_name: str
    overnight: bool
    domestic_discount_pct: int
    through_fare: float | None
    savings: float | None
    risk: str | None
    risk_reasons: list[str]
    tickets: list[TicketOut]


def _ticket(role: str, offer: Offer | None, discount: Decimal) -> TicketOut:
    if offer is None:
        return TicketOut(role=role)
    first, last = (offer.segments[0], offer.segments[-1]) if offer.segments else (None, None)
    paid = (offer.price * (Decimal(1) - discount)).quantize(_CENTS)
    return TicketOut(
        role=role,
        price=float(offer.price),
        paid=float(paid),
        from_airport=first.origin if first else None,
        to_airport=last.dest if last else None,
        stops=offer.stops,
        duration_minutes=offer.duration,
        flights=[s.flight_no for s in offer.segments],
        departs=first.dep_local.isoformat() if first and first.dep_local else None,
        arrives=last.arr_local.isoformat() if last and last.arr_local else None,
        booking_url=offer.booking_url,
        bags=bags_text(offer),
    )


def itinerary_out(itin: Itinerary) -> ItineraryOut:
    judged = itinerary_risk(itin)
    tickets = [
        _ticket("domestic out", itin.dom_out, itin.discount),
        _ticket("onward out", itin.onward_out, Decimal(0)),
    ]
    if itin.return_date:
        tickets += [
            _ticket("onward return", itin.onward_ret, Decimal(0)),
            _ticket("domestic return", itin.dom_ret, itin.discount),
        ]
    return ItineraryOut(
        total=float(itin.total),
        status=itin.status,
        date=itin.date,
        return_date=itin.return_date,
        dom_date=itin.dom_date,
        dom_return_date=itin.dom_return_date,
        hub=itin.hub,
        hub_name=itin.hub_name,
        dest=itin.dest,
        dest_name=itin.dest_name,
        overnight=itin.overnight,
        domestic_discount_pct=int(itin.discount * 100),
        through_fare=float(itin.through_fare) if itin.through_fare is not None else None,
        savings=float(itin.savings) if itin.savings is not None else None,
        risk=judged[0].name.lower() if judged else None,
        risk_reasons=judged[1] if judged else [],
        tickets=tickets,
    )
