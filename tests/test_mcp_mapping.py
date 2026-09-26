"""split_ticket_mcp.mapping: engine itineraries as LLM-readable models."""
from __future__ import annotations

from decimal import Decimal

from split_ticket_mcp.mapping import itinerary_out
from tests.results_fixtures import (
    offer,
    one_way,
    onward_out,
    seg,
    standard_one_way,
    standard_round_trip,
)


def test_a_confirmed_one_way():
    out = itinerary_out(standard_one_way(through_fare=Decimal("700")))
    assert out.status == "confirmed"
    assert out.total == 525.0
    assert out.savings == 175.0
    assert out.domestic_discount_pct == 75
    assert out.risk == "medium" and out.risk_reasons == ["3h00m between tickets at MAD"]
    dom, onward = out.tickets
    assert (dom.role, dom.price, dom.paid) == ("domestic out", 100.0, 25.0)
    assert (onward.role, onward.price, onward.paid) == ("onward out", 500.0, 500.0)
    assert dom.flights == ["IB100"] and dom.departs == "2026-10-01T07:00:00"
    assert dom.booking_url == "https://example.test/book"
    assert dom.bags == "cabin unknown, checked unknown"      # unknown stays unknown


def test_a_cheaper_single_ticket_is_a_negative_saving():
    assert itinerary_out(standard_one_way(through_fare=Decimal("500"))).savings == -25.0


def test_a_round_trip_lists_four_tickets_in_travel_order():
    roles = [t.role for t in itinerary_out(standard_round_trip()).tickets]
    assert roles == ["domestic out", "onward out", "onward return", "domestic return"]


def test_a_missing_offer_is_listed_with_nothing_bookable():
    partial = one_way(dom=standard_one_way().dom_out)
    out = itinerary_out(partial)
    assert out.status == "partial"
    missing = out.tickets[1]
    assert (missing.price, missing.paid, missing.booking_url, missing.flights) == (None, None, None, [])


def test_an_estimate_has_no_risk_and_no_bookable_ticket():
    est = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    out = itinerary_out(est)
    assert out.status == "estimate" and out.risk is None
    assert all(t.booking_url is None for t in out.tickets)


def test_overnight_dates():
    night = one_way(dom=offer("100", seg("LPA", "MAD", "2026-09-30T18:00", "2026-09-30T21:00")),
                    onward=onward_out(), overnight=True)
    out = itinerary_out(night)
    assert (out.date, out.dom_date, out.overnight) == ("2026-10-01", "2026-09-30", True)
