"""Tests for the SQLite persistence layer, focused on Task 11's additive migrations.

Covers:
- fresh-database schema includes the new `searches`/`favorites` columns
- `init_db()` migrates an existing, populated, pre-`trip_days` database in place
  without losing rows (the real `flight_finder.db` is exactly this shape)
- `save_search` / `add_favorite` round-trip the new query-shape columns
- existing callers (old argument sets, no new kwargs) keep working
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import db as db_module
from db import (
    add_favorite,
    add_price_check,
    get_favorites,
    get_search_by_id,
    get_searches,
    init_db,
    save_search,
)

# The exact column list of the live `flight_finder.db` before this task, as
# verified against the tracked-working-copy database. A migration must carry a
# database in this shape forward without losing data.
OLD_SEARCHES_SCHEMA = """
CREATE TABLE searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    origin      TEXT    NOT NULL,
    destinations TEXT   NOT NULL,
    dates       TEXT    NOT NULL,
    hubs        TEXT    NOT NULL,
    adults      INTEGER NOT NULL DEFAULT 1,
    currency    TEXT    NOT NULL DEFAULT 'EUR',
    best_price  REAL,
    best_route  TEXT,
    results     TEXT
);
"""

OLD_FAVORITES_SCHEMA = """
CREATE TABLE favorites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    origin        TEXT    NOT NULL,
    hub           TEXT    NOT NULL,
    destination   TEXT    NOT NULL,
    adults        INTEGER NOT NULL DEFAULT 1,
    currency      TEXT    NOT NULL DEFAULT 'EUR',
    record_price  REAL,
    record_date   TEXT,
    last_price    REAL,
    last_checked  TEXT,
    check_dates   TEXT    NOT NULL
);
"""

OLD_PRICE_CHECKS_SCHEMA = """
CREATE TABLE price_checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    favorite_id  INTEGER NOT NULL REFERENCES favorites(id) ON DELETE CASCADE,
    checked_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    best_price   REAL,
    route_detail TEXT
);
"""

SEARCHES_NEW_COLUMNS = {"window_start", "window_end", "provider", "through_fare", "scan_json"}
FAVORITES_NEW_COLUMNS = {"provider", "cabin", "children", "max_stops", "min_layover"}


def _columns(path: str, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


# ── Fresh database ───────────────────────────────────────────────────────────


async def test_fresh_database_has_all_new_searches_columns(temp_db):
    columns = _columns(temp_db, "searches")
    assert columns >= SEARCHES_NEW_COLUMNS
    # trip_days (an earlier migration) must still be there too.
    assert "trip_days" in columns


async def test_fresh_database_has_all_new_favorites_columns(temp_db):
    columns = _columns(temp_db, "favorites")
    assert columns >= FAVORITES_NEW_COLUMNS
    assert "trip_days" in columns


# ── Migration of an existing, populated, old-schema database ────────────────


async def test_init_db_migrates_an_old_populated_database_without_losing_rows(
    tmp_path, monkeypatch
):
    """Reproduce the exact shape of the live flight_finder.db: pre-trip_days,
    pre-Task-11, with a real row in `searches`. init_db() must add the new
    columns without dropping or altering that row.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(OLD_SEARCHES_SCHEMA)
    conn.execute(OLD_FAVORITES_SCHEMA)
    conn.execute(OLD_PRICE_CHECKS_SCHEMA)
    conn.execute(
        """
        INSERT INTO searches
            (id, created_at, origin, destinations, dates, hubs, adults, currency,
             best_price, best_route, results)
        VALUES (1, '2026-01-01T00:00:00Z', 'LPA', '["JFK"]', '["2026-03-01"]',
                '["MAD"]', 2, 'EUR', 612.5, 'LPA->MAD->JFK 2026-03-01', '[]')
        """
    )
    conn.execute(
        """
        INSERT INTO favorites
            (id, created_at, origin, hub, destination, adults, currency,
             record_price, record_date, last_price, last_checked, check_dates)
        VALUES (1, '2026-01-01T00:00:00Z', 'LPA', 'MAD', 'JFK', 2, 'EUR',
                612.5, '2026-01-01T00:00:00Z', 612.5, '2026-01-01T00:00:00Z',
                '["2026-03-01"]')
        """
    )
    conn.commit()
    conn.close()

    # Sanity check: this really is the old shape, before any migration runs.
    assert "trip_days" not in _columns(str(path), "searches")
    assert not (SEARCHES_NEW_COLUMNS & _columns(str(path), "searches"))

    monkeypatch.setattr(db_module, "DB_PATH", str(path))

    for _ in range(3):  # idempotent: migrating an already-migrated db is a no-op
        await init_db()

        columns = _columns(str(path), "searches")
        assert columns >= SEARCHES_NEW_COLUMNS
        assert "trip_days" in columns

        fav_columns = _columns(str(path), "favorites")
        assert fav_columns >= FAVORITES_NEW_COLUMNS
        assert "trip_days" in fav_columns

        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("SELECT * FROM searches WHERE id = 1").fetchone()
        finally:
            conn.close()
        assert row is not None
        # The original row's data must survive untouched, and the new columns
        # must default to NULL (or the migration's stated default) rather than
        # wiping/reinterpreting existing values.
        as_dict = dict(zip(_ordered_columns(str(path), "searches"), row, strict=True))
        assert as_dict["origin"] == "LPA"
        assert as_dict["destinations"] == '["JFK"]'
        assert as_dict["dates"] == '["2026-03-01"]'
        assert as_dict["hubs"] == '["MAD"]'
        assert as_dict["adults"] == 2
        assert as_dict["currency"] == "EUR"
        assert as_dict["best_price"] == 612.5
        assert as_dict["best_route"] == "LPA->MAD->JFK 2026-03-01"
        assert as_dict["results"] == "[]"
        assert as_dict["trip_days"] == 0  # migration default, one-way
        for col in SEARCHES_NEW_COLUMNS:
            assert as_dict[col] is None


def _ordered_columns(path: str, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cur.fetchall()]
    finally:
        conn.close()


# ── save_search round-trips the new columns ──────────────────────────────────


async def test_save_search_round_trips_window_and_scan_json(temp_db):
    scan_blob = {"MAD": {"2026-03-01": 300.0}, "BCN": {"2026-03-01": 280.0}}
    search_id = await save_search(
        origin="LPA",
        destinations=["JFK"],
        dates=["2026-03-01", "2026-03-02"],
        hubs=["MAD", "BCN"],
        adults=1,
        currency="EUR",
        best_price=612.5,
        best_route="LPA->MAD->JFK 2026-03-01",
        results=None,
        window_start="2026-03-01",
        window_end="2026-03-10",
        provider="kiwi",
        through_fare=Decimal("980.00"),
        scan_json=scan_blob,
    )

    row = await get_search_by_id(search_id)
    assert row["window_start"] == "2026-03-01"
    assert row["window_end"] == "2026-03-10"
    assert row["provider"] == "kiwi"
    # through_fare is stored as REAL (see db.py docstring); reconstructing via
    # str() avoids binary float noise and recovers the exact Decimal.
    assert Decimal(str(row["through_fare"])) == Decimal("980.00")
    assert json.loads(row["scan_json"]) == scan_blob


async def test_save_search_defaults_new_columns_to_none(temp_db):
    """Callers that don't know about the new fields (Task 12 hasn't wired them
    in yet) must keep working, and must not get fabricated values back.
    """
    search_id = await save_search(
        origin="LPA",
        destinations=["JFK"],
        dates=["2026-03-01"],
        hubs=["MAD"],
        adults=1,
        currency="EUR",
        best_price=612.5,
        best_route="LPA->MAD->JFK 2026-03-01",
        results=None,
    )

    row = await get_search_by_id(search_id)
    assert row["window_start"] is None
    assert row["window_end"] is None
    assert row["provider"] is None
    assert row["through_fare"] is None
    assert row["scan_json"] is None
    assert row["trip_days"] == 0


async def test_get_searches_includes_new_columns(temp_db):
    await save_search(
        origin="LPA",
        destinations=["JFK"],
        dates=["2026-03-01"],
        hubs=["MAD"],
        adults=1,
        currency="EUR",
        best_price=612.5,
        best_route="LPA->MAD->JFK 2026-03-01",
        results=None,
        provider="google",
    )
    rows = await get_searches()
    assert rows[0]["provider"] == "google"


# ── add_favorite round-trips the new query-shape columns ────────────────────


async def test_add_favorite_round_trips_query_shape_columns(temp_db):
    fav_id = await add_favorite(
        origin="LPA",
        hub="MAD",
        destination="JFK",
        adults=2,
        currency="EUR",
        price=612.5,
        check_dates=["2026-03-01"],
        trip_days=7,
        provider="kiwi",
        cabin="BUSINESS",
        children=1,
        max_stops=1,
        min_layover=90,
    )

    favorites = await get_favorites()
    fav = next(f for f in favorites if f["id"] == fav_id)
    assert fav["provider"] == "kiwi"
    assert fav["cabin"] == "BUSINESS"
    assert fav["children"] == 1
    assert fav["max_stops"] == 1
    assert fav["min_layover"] == 90
    assert fav["trip_days"] == 7


async def test_add_favorite_defaults_query_shape_columns(temp_db):
    """Old callers (handlers/favorites.py) don't pass the new query-shape
    kwargs. Defaults must match LegQuery's own defaults (ECONOMY, 0 children,
    no stop/layover constraint) so a replay under these defaults reproduces
    the same query shape an unqualified search already used.
    """
    fav_id = await add_favorite(
        origin="LPA",
        hub="MAD",
        destination="JFK",
        adults=1,
        currency="EUR",
        price=612.5,
        check_dates=["2026-03-01"],
    )

    favorites = await get_favorites()
    fav = next(f for f in favorites if f["id"] == fav_id)
    assert fav["provider"] is None
    assert fav["cabin"] == "ECONOMY"
    assert fav["children"] == 0
    assert fav["max_stops"] is None
    assert fav["min_layover"] is None


# ── Existing callers keep working (regression) ───────────────────────────────


async def test_save_search_old_call_shape_still_works(temp_db):
    """Exactly the call shape handlers/search_flow.py uses today."""
    search_id = await save_search(
        origin="LPA",
        destinations=["JFK"],
        dates=["2026-03-01"],
        hubs=["MAD"],
        adults=2,
        currency="EUR",
        trip_days=0,
        best_price=612.5,
        best_route="LPA->MAD->JFK 2026-03-01",
        results=[{"total": 612.5}],
    )
    row = await get_search_by_id(search_id)
    assert row["origin"] == "LPA"
    assert row["best_price"] == 612.5


async def test_add_favorite_old_call_shape_still_works(temp_db):
    """Exactly the call shape handlers/favorites.py uses today."""
    fav_id = await add_favorite(
        origin="LPA",
        hub="MAD",
        destination="JFK",
        adults=1,
        currency="EUR",
        price=612.5,
        check_dates=["2026-03-01"],
        trip_days=7,
    )
    favorites = await get_favorites()
    fav = next(f for f in favorites if f["id"] == fav_id)
    assert fav["trip_days"] == 7


async def test_add_price_check_still_works(temp_db):
    fav_id = await add_favorite(
        origin="LPA",
        hub="MAD",
        destination="JFK",
        adults=1,
        currency="EUR",
        price=612.5,
        check_dates=["2026-03-01"],
    )
    check_id = await add_price_check(fav_id, 590.0, {"route": "detail"})
    assert check_id is not None


async def test_init_db_is_idempotent_on_a_fresh_database(temp_db):
    """Calling init_db() again on an already-fresh, already-migrated database
    must not raise (no duplicate-column errors).
    """
    await init_db()
    await init_db()
