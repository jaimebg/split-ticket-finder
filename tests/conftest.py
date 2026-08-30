"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def real_html() -> str:
    """A trimmed capture of a real Google Flights response (LPA -> MAD)."""
    return (FIXTURE_DIR / "google_flights_lpa_mad.html").read_text()


@pytest.fixture
async def temp_db(tmp_path, monkeypatch):
    """Point the db module at a throwaway SQLite file and initialise it."""
    import db

    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    await db.init_db()
    return str(path)


@pytest.fixture
def kiwi_fixture():
    """Load a recorded Kiwi GraphQL response by name (no .json suffix)."""
    import json

    def _load(name: str) -> dict:
        return json.loads((FIXTURE_DIR / "kiwi" / f"{name}.json").read_text())

    return _load
