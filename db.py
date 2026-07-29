"""SQLite database layer for flight_finder (async via aiosqlite)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite

from config import DB_PATH

# ── Schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    origin      TEXT    NOT NULL,
    destinations TEXT   NOT NULL,   -- JSON list
    dates       TEXT    NOT NULL,   -- JSON list
    hubs        TEXT    NOT NULL,   -- JSON list
    adults      INTEGER NOT NULL DEFAULT 1,
    currency    TEXT    NOT NULL DEFAULT 'EUR',
    trip_days   INTEGER NOT NULL DEFAULT 0,  -- 0 = one-way
    best_price  REAL,
    best_route  TEXT,
    results     TEXT                -- JSON blob
);

CREATE TABLE IF NOT EXISTS favorites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    origin        TEXT    NOT NULL,
    hub           TEXT    NOT NULL,
    destination   TEXT    NOT NULL,
    adults        INTEGER NOT NULL DEFAULT 1,
    currency      TEXT    NOT NULL DEFAULT 'EUR',
    trip_days     INTEGER NOT NULL DEFAULT 0,  -- 0 = one-way
    record_price  REAL,
    record_date   TEXT,
    last_price    REAL,
    last_checked  TEXT,
    check_dates   TEXT    NOT NULL  -- JSON list
);

CREATE TABLE IF NOT EXISTS price_checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    favorite_id  INTEGER NOT NULL REFERENCES favorites(id) ON DELETE CASCADE,
    checked_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    best_price   REAL,
    route_detail TEXT                -- JSON blob
);
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    """Return current UTC timestamp as ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(obj: object) -> str:
    """Serialize an object to a compact JSON string."""
    return json.dumps(obj, ensure_ascii=False)


# ── Init ─────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _connect():
    """Open a connection with pragmas (foreign keys, WAL) enabled."""
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")
    try:
        yield db
    finally:
        await db.close()


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is applied only when absent from the live table.
MIGRATIONS = (
    ("searches", "trip_days", "INTEGER NOT NULL DEFAULT 0"),
    ("favorites", "trip_days", "INTEGER NOT NULL DEFAULT 0"),
)


async def _existing_columns(db, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def init_db() -> None:
    """Create tables if they don't exist, then apply pending migrations."""
    async with _connect() as db:
        await db.executescript(SCHEMA)

        for table, column, spec in MIGRATIONS:
            if column not in await _existing_columns(db, table):
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")

        await db.commit()


# ── Searches ─────────────────────────────────────────────────────────────────

async def save_search(
    origin: str,
    destinations: list[str],
    dates: list[str],
    hubs: list[str],
    adults: int,
    currency: str,
    best_price: float | None,
    best_route: str | None,
    results: object | None,
    trip_days: int = 0,
) -> int:
    """Insert a completed search and return its row id."""
    async with _connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO searches
                (origin, destinations, dates, hubs, adults, currency, trip_days,
                 best_price, best_route, results)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                origin,
                _json(destinations),
                _json(dates),
                _json(hubs),
                adults,
                currency,
                trip_days,
                best_price,
                best_route,
                _json(results) if results is not None else None,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_searches(limit: int = 10) -> list[dict]:
    """Return the last *limit* searches, newest first."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM searches ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_search_by_id(search_id: int) -> dict | None:
    """Return a single search by id, or None."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM searches WHERE id = ?", (search_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# ── Favorites ────────────────────────────────────────────────────────────────

async def add_favorite(
    origin: str,
    hub: str,
    destination: str,
    adults: int,
    currency: str,
    price: float | None,
    check_dates: list[str],
    trip_days: int = 0,
) -> int:
    """Add a route to favorites and return its row id.

    *trip_days* must match the trip shape the *price* was quoted for, otherwise
    the scheduler would re-price a round-trip favourite as one-way and read the
    difference as a price drop.
    """
    now = _now()
    async with _connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO favorites
                (origin, hub, destination, adults, currency, trip_days,
                 record_price, record_date, last_price, last_checked, check_dates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                origin,
                hub,
                destination,
                adults,
                currency,
                trip_days,
                price,
                now if price is not None else None,
                price,
                now if price is not None else None,
                _json(check_dates),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_favorites() -> list[dict]:
    """Return all favorites, newest first."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM favorites ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def delete_favorite(fav_id: int) -> bool:
    """Delete a favorite by id. Returns True if a row was deleted."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM favorites WHERE id = ?", (fav_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_favorite_price(
    fav_id: int,
    price: float,
    is_record: bool = False,
) -> None:
    """Update last_price (and optionally record_price) for a favorite."""
    now = _now()
    async with _connect() as db:
        if is_record:
            await db.execute(
                """
                UPDATE favorites
                   SET last_price    = ?,
                       last_checked  = ?,
                       record_price  = ?,
                       record_date   = ?
                 WHERE id = ?
                """,
                (price, now, price, now, fav_id),
            )
        else:
            await db.execute(
                """
                UPDATE favorites
                   SET last_price   = ?,
                       last_checked = ?
                 WHERE id = ?
                """,
                (price, now, fav_id),
            )
        await db.commit()


# ── Price checks ─────────────────────────────────────────────────────────────

async def add_price_check(
    fav_id: int,
    price: float | None,
    route_detail: object | None,
) -> int:
    """Log a price-check event for a favorite and return its row id."""
    async with _connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO price_checks (favorite_id, best_price, route_detail)
            VALUES (?, ?, ?)
            """,
            (
                fav_id,
                price,
                _json(route_detail) if route_detail is not None else None,
            ),
        )
        await db.commit()
        return cursor.lastrowid
