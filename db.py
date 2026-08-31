"""SQLite database layer for flight_finder (async via aiosqlite)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import aiosqlite

from config import DB_PATH

# ── Schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    origin      TEXT    NOT NULL,
    destinations TEXT   NOT NULL,   -- JSON list
    dates       TEXT    NOT NULL,   -- JSON list; kept for the grid fallback (§5.6)
    hubs        TEXT    NOT NULL,   -- JSON list
    adults      INTEGER NOT NULL DEFAULT 1,
    currency    TEXT    NOT NULL DEFAULT 'EUR',
    trip_days   INTEGER NOT NULL DEFAULT 0,  -- 0 = one-way
    window_start TEXT,              -- searched window start (ISO date); NULL = no window
    window_end   TEXT,              -- searched window end (ISO date); NULL = no window
    provider     TEXT,              -- which source produced these numbers; NULL = unknown
    best_price  REAL,
    best_route  TEXT,
    through_fare REAL,              -- single-ticket baseline (Decimal, stored as REAL)
    results     TEXT,               -- JSON blob
    scan_json    TEXT               -- phase 0 calendar grid, JSON blob
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
    provider      TEXT,             -- which source the price was quoted from; NULL = unknown
    cabin         TEXT    NOT NULL DEFAULT 'ECONOMY',
    children      INTEGER NOT NULL DEFAULT 0,
    max_stops     INTEGER,          -- NULL = no limit applied
    min_layover   INTEGER,          -- minutes; NULL = no minimum applied
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
# EXISTS", so each is applied only when absent from the live table. Each spec
# here must match the column definition in SCHEMA above exactly, so a fresh
# install and a migrated install end up with identical tables.
MIGRATIONS = (
    ("searches", "trip_days", "INTEGER NOT NULL DEFAULT 0"),
    ("favorites", "trip_days", "INTEGER NOT NULL DEFAULT 0"),
    # Task 11: the two-stage engine's window search and its extra outputs.
    ("searches", "window_start", "TEXT"),
    ("searches", "window_end", "TEXT"),
    ("searches", "provider", "TEXT"),
    ("searches", "through_fare", "REAL"),
    ("searches", "scan_json", "TEXT"),
    # Task 11: the query shape a favourite's price was quoted under, so the
    # scheduler can replay it exactly (same reasoning as trip_days above).
    ("favorites", "provider", "TEXT"),
    ("favorites", "cabin", "TEXT NOT NULL DEFAULT 'ECONOMY'"),
    ("favorites", "children", "INTEGER NOT NULL DEFAULT 0"),
    ("favorites", "max_stops", "INTEGER"),
    ("favorites", "min_layover", "INTEGER"),
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
    window_start: str | None = None,
    window_end: str | None = None,
    provider: str | None = None,
    through_fare: Decimal | None = None,
    scan_json: object | None = None,
) -> int:
    """Insert a completed search and return its row id.

    *window_start*/*window_end* record the searched window (the two-stage
    engine's calendar search); *dates* is kept alongside it because the grid
    fallback for calendar-less providers still expands a window into discrete
    sampled dates, and history needs to know which ones. *provider* names the
    source those numbers came from. *through_fare* is the single-ticket
    baseline: it is `Decimal` in Python (this codebase's money type) but
    stored as `REAL`, matching the existing `best_price` column — SQLite has
    no native `Decimal`, so it is converted to `float` here at the boundary
    and must be reconstructed via `Decimal(str(value))` on read, which is
    exact for the 2-decimal-place amounts this column holds. *scan_json* is
    phase 0's calendar grid, stored so a past search can be redisplayed
    without re-querying.
    """
    async with _connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO searches
                (origin, destinations, dates, hubs, adults, currency, trip_days,
                 window_start, window_end, provider,
                 best_price, best_route, through_fare, results, scan_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                origin,
                _json(destinations),
                _json(dates),
                _json(hubs),
                adults,
                currency,
                trip_days,
                window_start,
                window_end,
                provider,
                best_price,
                best_route,
                float(through_fare) if through_fare is not None else None,
                _json(results) if results is not None else None,
                _json(scan_json) if scan_json is not None else None,
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
    provider: str | None = None,
    cabin: str = "ECONOMY",
    children: int = 0,
    max_stops: int | None = None,
    min_layover: int | None = None,
) -> int:
    """Add a route to favorites and return its row id.

    *trip_days*, *provider*, *cabin*, *children*, *max_stops* and
    *min_layover* together describe the query shape the *price* was quoted
    under. Of these, ``scheduler.check_favorites`` actually reads back and
    replays only *trip_days* and *provider* today (review finding I7) — the
    same reasoning that made *trip_days* necessary: a round-trip favourite
    re-priced as one-way looks like a 50% crash, every cycle.

    *cabin*, *children*, *max_stops* and *min_layover* are stored but not yet
    replayed: nothing in the guided flow sets them away from their defaults
    yet, so there is no live bug, but they are reserved for Layer 3 (whoever
    adds a cabin or stops selector must also teach the scheduler to read
    these columns back, or reintroduce the round-trip-shaped bug fixed in
    e83a4d3 for whichever of them they wire up). The columns stay regardless
    -- do not drop them. The defaults here match `providers.base.LegQuery`'s
    own defaults, so a favourite added without specifying them replays as
    the unqualified search it came from.
    """
    now = _now()
    async with _connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO favorites
                (origin, hub, destination, adults, currency, trip_days,
                 provider, cabin, children, max_stops, min_layover,
                 record_price, record_date, last_price, last_checked, check_dates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                origin,
                hub,
                destination,
                adults,
                currency,
                trip_days,
                provider,
                cabin,
                children,
                max_stops,
                min_layover,
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
