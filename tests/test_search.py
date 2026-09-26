"""Tests for search.py: serialization of phase 0's calendar grid.

The actual searching (discount maths, phase narrowing, concurrency, scraper
failure tolerance) moved to engine/ in Tasks 9-11 and is covered there
(tests/test_engine_scan.py, tests/test_engine_drill.py, tests/test_engine_grid.py,
tests/test_engine_fetch.py, tests/test_engine_orchestrator.py). This file only
covers what remains in search.py. Result rendering and storage moved to
results/ in Layer 3b (tests/test_results_view.py, tests/test_results_store.py).
"""
from __future__ import annotations

import json
from decimal import Decimal

from engine.scan import CalendarGrid
from models import add_days
from providers.base import RatedPrice
from search import scan_to_json

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


def test_add_days_matches_models_helper():
    assert add_days("2026-09-01", 14) == "2026-09-15"
    assert add_days("2026-12-25", 10) == "2027-01-04"
