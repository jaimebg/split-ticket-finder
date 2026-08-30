"""Regression tests for bugs found during the pre-release audit.

Each test fails against the code as it was before the fix, so they pin the
behaviour rather than just exercising it.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import db as db_module
from handlers.history import _route_from_dict
from handlers.utils import ValidationError, esc, parse_date, parse_date_list, split_message
from models import Route, generate_dates
from search import format_results, routes_to_json


def _days_from_now(days: int) -> str:
    """A valid future date, so these tests don't rot as the calendar moves."""
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def _round_trip_route() -> Route:
    return Route(
        date="2026-09-01",
        hub="MAD",
        hub_name="Madrid",
        dest="NRT",
        dest_name="NRT",
        dom_price=100,
        dom_discounted=25.0,
        intl_price=400,
        total=425.0,
        return_date="2026-09-15",
    )


# ── Bug 1: an invalid date killed the conversation with no feedback ──────────


def test_invalid_date_raises_a_user_facing_error():
    """The flow must be able to re-prompt instead of dying inside generate_dates."""
    with pytest.raises(ValidationError) as exc:
        parse_date("tomorrow", field="start date")
    assert "not a valid start date" in str(exc.value)
    assert "YYYY-MM-DD" in str(exc.value)


def test_past_dates_are_rejected():
    with pytest.raises(ValidationError, match="in the past"):
        parse_date("2020-01-01")


def test_dates_too_far_ahead_are_rejected():
    far = _days_from_now(400)
    with pytest.raises(ValidationError, match="days away"):
        parse_date(far)


def test_validated_dates_never_blow_up_generate_dates():
    """parse_date is the guard that makes the later generate_dates call safe."""
    start = parse_date(_days_from_now(30))
    end = parse_date(_days_from_now(39))
    assert generate_dates(start, end, 3) == [
        _days_from_now(30), _days_from_now(33), _days_from_now(36), _days_from_now(39),
    ]


def test_date_list_is_deduplicated_and_sorted():
    near, far = _days_from_now(10), _days_from_now(60)
    assert parse_date_list(f"{far}, {near}, {far}") == [near, far]


# ── Bug 2: viewing a stored round-trip rendered it as one-way ───────────────


def test_stored_round_trip_survives_the_json_round_trip():
    stored = json.loads(routes_to_json([_round_trip_route()]))[0]
    assert stored["return_date"] == "2026-09-15"

    restored = _route_from_dict(stored)
    assert restored.return_date == "2026-09-15", "return_date must survive storage"


def test_stored_round_trip_still_renders_as_round_trip():
    stored = json.loads(routes_to_json([_round_trip_route()]))[0]
    rendered = format_results([_route_from_dict(stored)], "LPA")

    assert "Round-trip" in rendered
    assert "One-way" not in rendered
    # Both legs of the date range must show, not just the outbound.
    assert "2026-09-01 — 2026-09-15" in rendered


# ── Bug 3: trip_days was not persisted, causing false price-drop alerts ─────


async def test_searches_round_trip_shape_is_persisted(temp_db):
    search_id = await db_module.save_search(
        origin="LPA",
        destinations=["NRT"],
        dates=["2026-09-01"],
        hubs=["MAD"],
        adults=1,
        currency="EUR",
        best_price=425.0,
        best_route="LPA->MAD->NRT 2026-09-01",
        results=[{"total": 425.0}],
        trip_days=14,
    )
    row = await db_module.get_search_by_id(search_id)
    assert row["trip_days"] == 14, "a rerun would otherwise silently become one-way"


async def test_favorites_round_trip_shape_is_persisted(temp_db):
    await db_module.add_favorite(
        origin="LPA",
        hub="MAD",
        destination="NRT",
        adults=1,
        currency="EUR",
        price=425.0,
        check_dates=["2026-09-01"],
        trip_days=14,
    )
    fav = (await db_module.get_favorites())[0]
    assert fav["trip_days"] == 14
    assert fav["record_price"] == 425.0


async def test_migration_adds_trip_days_to_a_pre_existing_database(tmp_path, monkeypatch):
    """A database created before trip_days existed must upgrade in place."""
    import aiosqlite

    path = tmp_path / "legacy.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(path))

    # Recreate the original schema, without trip_days.
    async with aiosqlite.connect(str(path)) as legacy:
        await legacy.execute(
            """
            CREATE TABLE favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                origin TEXT NOT NULL, hub TEXT NOT NULL, destination TEXT NOT NULL,
                adults INTEGER NOT NULL DEFAULT 1, currency TEXT NOT NULL DEFAULT 'EUR',
                record_price REAL, record_date TEXT, last_price REAL, last_checked TEXT,
                check_dates TEXT NOT NULL
            )
            """
        )
        await legacy.execute(
            "INSERT INTO favorites (origin, hub, destination, check_dates) "
            "VALUES ('LPA','MAD','NRT','[\"2026-09-01\"]')"
        )
        await legacy.commit()

    await db_module.init_db()

    fav = (await db_module.get_favorites())[0]
    assert fav["trip_days"] == 0, "existing rows default to one-way"
    assert fav["hub"] == "MAD", "existing data must be preserved"


async def test_init_db_is_idempotent(temp_db):
    """Running migrations twice must not fail on an already-migrated database."""
    await db_module.init_db()
    await db_module.init_db()


# ── Bug 4: user input was interpolated into HTML unescaped ─────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<b>bold", "&lt;b&gt;bold"),
        ("a & b", "a &amp; b"),
        ("MAD", "MAD"),
    ],
)
def test_user_input_is_escaped_for_html(raw, expected):
    assert esc(raw) == expected


# ── Message splitting ───────────────────────────────────────────────────────


def test_split_message_keeps_chunks_within_the_limit():
    text = "\n\n".join(f"block {i} " + "x" * 500 for i in range(20))
    chunks = split_message(text, limit=1000)

    assert all(len(c) <= 1000 for c in chunks)
    # No content may be dropped.
    assert sum(c.count("block ") for c in chunks) == 20


def test_split_message_emits_oversized_block_whole():
    """An oversized block is passed through rather than cut mid-HTML-tag."""
    huge = "y" * 1500
    chunks = split_message(f"small\n\n{huge}", limit=1000)
    assert huge in chunks


def test_split_message_short_text_is_one_chunk():
    assert split_message("just one block") == ["just one block"]
