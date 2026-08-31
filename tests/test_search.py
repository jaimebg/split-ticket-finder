"""Tests for search.py: Telegram formatting and JSON storage of Itinerary results.

The actual searching (discount maths, phase narrowing, concurrency, scraper
failure tolerance) moved to engine/ in Tasks 9-11 and is covered there
(tests/test_engine_scan.py, tests/test_engine_drill.py, tests/test_engine_grid.py,
tests/test_engine_fetch.py, tests/test_engine_orchestrator.py). This file only
covers what remains in search.py: rendering and serializing Itinerary lists.
"""
from __future__ import annotations

import json
from decimal import Decimal

from engine.scan import CalendarGrid
from models import Itinerary, add_days
from providers.base import Offer, RatedPrice
from search import format_results, itineraries_to_json, scan_to_json


def _offer(price: str, *, booking_url: str | None = "https://book.example/x",
           requires_bag_recheck: bool | None = None) -> Offer:
    return Offer(
        price=Decimal(price),
        currency="EUR",
        airlines=["Iberia"],
        stops=0,
        duration=120,
        segments=[],
        provider="fake",
        booking_url=booking_url,
        requires_bag_recheck=requires_bag_recheck,
    )


def _confirmed_itin(**kw) -> Itinerary:
    base = {
        "date": "2026-09-01",
        "return_date": "",
        "hub": "MAD",
        "hub_name": "Madrid",
        "dest": "NRT",
        "dest_name": "Tokyo",
        "discount": Decimal("0.75"),
        "dom_out": _offer("100"),
        "onward_out": _offer("500"),
    }
    base.update(kw)
    return Itinerary(**base)


def _estimate_itin(**kw) -> Itinerary:
    base = {
        "date": "2026-09-01",
        "return_date": "",
        "hub": "MAD",
        "hub_name": "Madrid",
        "dest": "NRT",
        "dest_name": "Tokyo",
        "discount": Decimal("0.75"),
        "est_dom_price": Decimal("100"),
        "est_onward_price": Decimal("500"),
    }
    base.update(kw)
    return Itinerary(**base)


# ── No results ───────────────────────────────────────────────────────────────


def test_format_results_handles_no_itineraries():
    assert "No routes found" in format_results([], "LPA")


# ── Requirement: estimate labelling and no booking link ─────────────────────


def test_confirmed_itinerary_shows_a_booking_link():
    out = format_results([_confirmed_itin()], "LPA")
    assert "Estimate only" not in out
    assert "href=" in out


def test_unconfirmed_itinerary_is_labelled_an_estimate_with_no_booking_link():
    """An itinerary whose status is not STATUS_CONFIRMED must say so and must
    never show a booking link — phase 0's figures are cached calendar
    numbers, not a fare anyone could buy."""
    out = format_results([_estimate_itin()], "LPA")
    assert "Estimate only" in out
    assert "href=" not in out


def test_partially_confirmed_itinerary_is_also_labelled_an_estimate():
    """A round trip missing its return leg is STATUS_PARTIAL, not
    STATUS_CONFIRMED — it must be treated the same as a pure estimate."""
    itin = Itinerary(
        date="2026-09-01", return_date="2026-09-15", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100"), onward_out=_offer("500"),
        # dom_ret/onward_ret missing -> STATUS_PARTIAL despite two real offers.
    )
    assert not itin.confirmed
    out = format_results([itin], "LPA")
    assert "Estimate only" in out
    assert "href=" not in out


# ── Requirement: the savings block ───────────────────────────────────────────


def test_savings_block_renders_when_a_through_fare_exists():
    itin = _confirmed_itin().with_through_fare(Decimal("980"))
    out = format_results([itin], "LPA")

    assert "Through-fare" in out
    assert "You save" in out
    # dom_discounted (25) + onward (500) = 525 total; 980 - 525 = 455 saved.
    assert "455.00" in out
    assert "(46%)" in out


def test_savings_block_says_no_fare_available_when_through_fare_is_absent():
    out = format_results([_confirmed_itin()], "LPA")
    assert "no single-ticket fare available" in out
    assert "You save" not in out


def test_format_results_through_fare_fallback_applies_only_when_itinerary_has_none():
    """The keyword through_fare is a fallback for a reconstructed row that
    carries none of its own; an itinerary that already has one keeps it."""
    with_own = _confirmed_itin().with_through_fare(Decimal("980"))
    out = format_results([with_own], "LPA", through_fare=Decimal("1"))
    # 1 would make the split look worse than the through-fare; the itinerary's
    # own 980 must win, so this must still render as a real saving.
    assert "455.00" in out

    without_own = _confirmed_itin()
    out2 = format_results([without_own], "LPA", through_fare=Decimal("980"))
    assert "455.00" in out2


# ── CRITICAL requirement: negative savings must never say "You save -N" ─────


def test_negative_savings_says_through_fare_is_cheaper_not_a_negative_saving():
    """savings can be negative when the through-fare beats the split
    (observed -11.25 in the pipeline). Rendering "You save -N" would
    recommend the more expensive option -- product-breaking, not cosmetic."""
    itin = _confirmed_itin().with_through_fare(Decimal("513.75"))  # total is 525
    out = format_results([itin], "LPA")

    assert "cheaper" in out
    assert "You save" not in out
    assert "-11.25" not in out
    assert "−11.25" not in out


def test_zero_savings_also_reads_as_the_through_fare_being_cheaper():
    """savings == 0 is a tie, not a saving -- must not render "You save 0"."""
    itin = _confirmed_itin().with_through_fare(Decimal("525"))  # exactly total
    out = format_results([itin], "LPA")

    assert "cheaper" in out
    assert "You save" not in out


# ── Requirement: bag re-check warning ────────────────────────────────────────


def test_bag_recheck_warning_shown_when_true():
    itin = _confirmed_itin(dom_out=_offer("100", requires_bag_recheck=True))
    out = format_results([itin], "LPA")
    assert "re-checking bags" in out


def test_bag_recheck_silent_when_unknown():
    """None means "cannot tell you", never "no" -- must not be rendered as
    a warning, and must not be rendered as reassurance either."""
    itin = _confirmed_itin(dom_out=_offer("100", requires_bag_recheck=None))
    out = format_results([itin], "LPA")
    assert "re-checking bags" not in out


def test_bag_recheck_silent_when_false():
    itin = _confirmed_itin(
        dom_out=_offer("100", requires_bag_recheck=False),
        onward_out=_offer("500", requires_bag_recheck=False),
    )
    assert itin.requires_bag_recheck is False
    out = format_results([itin], "LPA")
    assert "re-checking bags" not in out


# ── Discount tag and round-trip labelling ────────────────────────────────────


def test_discounted_hub_is_tagged():
    out = format_results([_confirmed_itin()], "LPA")
    assert "75% disc." in out


def test_non_discounted_hub_is_tagged_no_disc():
    itin = _confirmed_itin(discount=Decimal("0"))
    out = format_results([itin], "LPA")
    assert "no disc." in out


def test_round_trip_itinerary_renders_round_trip_label_and_both_dates():
    itin = _confirmed_itin(
        return_date="2026-09-15",
        dom_ret=_offer("110"),
        onward_ret=_offer("520"),
    )
    out = format_results([itin], "LPA")
    assert "Round-trip" in out
    assert "One-way" not in out
    assert "2026-09-01 — 2026-09-15" in out


# ── Requirement: the actionable discount reminder ────────────────────────────


def test_discount_reminder_shown_when_a_result_is_discounted():
    """Without this instruction the user does not actually receive the
    discount this product exists to exploit -- a through-ticket never
    applies it, only two separately booked tickets do."""
    out = format_results([_confirmed_itin()], "LPA")  # discount=0.75
    lowered = out.lower()
    assert "separate" in lowered
    assert "book" in lowered


def test_discount_reminder_omitted_when_nothing_shown_is_discounted():
    itin = _confirmed_itin(discount=Decimal("0"))
    out = format_results([itin], "LPA")
    assert "separate ticket" not in out.lower()


# ── Serialization ─────────────────────────────────────────────────────────────


def test_itineraries_to_json_caps_at_25_entries():
    itins = [_estimate_itin(date=f"2026-09-{d:02d}") for d in range(1, 40, 1)][:40]
    assert len(json.loads(itineraries_to_json(itins))) == 25


def test_itineraries_to_json_round_trips_key_fields():
    itin = _confirmed_itin().with_through_fare(Decimal("980"))
    stored = json.loads(itineraries_to_json([itin]))[0]

    assert stored["date"] == "2026-09-01"
    assert stored["hub"] == "MAD"
    assert stored["dest"] == "NRT"
    assert stored["total"] == 525.0
    assert stored["through_fare"] == 980.0
    assert stored["status"] == "confirmed"


def test_itineraries_to_json_records_no_through_fare_as_none_not_zero():
    stored = json.loads(itineraries_to_json([_confirmed_itin()]))[0]
    assert stored["through_fare"] is None
    assert stored["savings"] is None


# ── scan_to_json (Task 12 follow-up: wire the phase-0 calendar grid) ────────


def _rated(price: str, rating: str = "AVERAGE") -> RatedPrice:
    return RatedPrice(price=Decimal(price), rating=rating)


def test_scan_to_json_serializes_none_as_the_json_null_literal():
    """The grid strategy sets scan=None; this must stay loads()-safe so a
    caller never has to branch on strategy before storing it."""
    assert scan_to_json(None) == "null"
    assert json.loads(scan_to_json(None)) is None


def test_scan_to_json_round_trips_dates_to_prices_per_leg_key():
    grid = CalendarGrid(
        out_dom={"MAD": {"2026-09-01": _rated("50")}},
        ret_dom={"MAD": {"2026-09-15": _rated("55")}},
        out_onward={("MAD", "NRT"): {"2026-09-01": _rated("500")}},
        ret_onward={("MAD", "NRT"): {"2026-09-15": _rated("520")}},
    )

    stored = json.loads(scan_to_json(grid))

    assert stored["out_dom"]["MAD"]["2026-09-01"] == 50.0
    assert stored["ret_dom"]["MAD"]["2026-09-15"] == 55.0
    # (hub, dest) tuple keys become "hub|dest" strings -- JSON object keys
    # must be strings, and the grid itself keys the onward side on a tuple.
    assert stored["out_onward"]["MAD|NRT"]["2026-09-01"] == 500.0
    assert stored["ret_onward"]["MAD|NRT"]["2026-09-15"] == 520.0


async def test_scan_to_json_persists_and_reloads_through_the_real_db(temp_db):
    """Task 11 added searches.scan_json specifically so history can
    redisplay a past search without re-querying -- this proves a search
    actually writes it and reads back the identical structure. Uses
    temp_db (a throwaway file), never the real flight_finder.db."""
    import db as db_module

    grid = CalendarGrid(
        out_dom={"MAD": {"2026-09-01": _rated("50")}},
        ret_dom={},
        out_onward={("MAD", "NRT"): {"2026-09-01": _rated("500")}},
        ret_onward={},
    )

    search_id = await db_module.save_search(
        origin="LPA", destinations=["NRT"], dates=["2026-09-01"], hubs=["MAD"],
        adults=1, currency="EUR", best_price=525.0,
        best_route="LPA->MAD->NRT 2026-09-01",
        results=[{"total": 525.0}],
        scan_json=json.loads(scan_to_json(grid)),
    )

    row = await db_module.get_search_by_id(search_id)
    reloaded = json.loads(row["scan_json"])

    assert reloaded["out_dom"]["MAD"]["2026-09-01"] == 50.0
    assert reloaded["out_onward"]["MAD|NRT"]["2026-09-01"] == 500.0


# ── Telegram length limits ───────────────────────────────────────────────────


def test_format_output_stays_within_telegram_limits_for_a_large_search():
    """Each block must be individually sendable after splitting."""
    from handlers.utils import split_message

    itins = [
        _confirmed_itin(date=f"2026-09-{d:02d}", hub=hub, dest=dest)
        for d in range(1, 15)
        for hub in ("MAD", "BCN", "AGP")
        for dest in ("NRT", "JFK")
    ]
    chunks = split_message(format_results(itins, "LPA"))
    assert all(len(c) <= 4096 for c in chunks)


def test_add_days_matches_models_helper():
    assert add_days("2026-09-01", 14) == "2026-09-15"
    assert add_days("2026-12-25", 10) == "2027-01-04"
