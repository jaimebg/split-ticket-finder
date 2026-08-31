"""Tests for handlers/favorites.py's provider round-trip (Task 13, Part A).

Task 11 added a `provider` column to both `searches` and `favorites` so the
scheduler can replay a favourite's exact query shape rather than re-pricing
it under a different one (see scheduler.py's own comment, and the
round-trip regression fixed in e83a4d3: a round-trip favourite re-priced as
one-way read as a 50% crash on every cycle). `save_favorite` never forwarded
the stored search's provider into `add_favorite`, so the column was always
NULL for a tracked favourite regardless of what actually priced it -- these
tests pin the fix.
"""
from __future__ import annotations

from types import SimpleNamespace

import db as db_module
import handlers.start as start_module
from handlers.favorites import save_favorite

_OWNER_ID = 918273645


class FakeQuery:
    """Just enough of telegram.CallbackQuery for save_favorite to run."""

    def __init__(self, data: str):
        self.data = data
        self.answered = False
        self.edits: list[str] = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def _update(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        callback_query=FakeQuery(data),
        effective_user=SimpleNamespace(id=_OWNER_ID),
    )


async def _save_search_with(db_kwargs) -> int:
    defaults = {
        "origin": "LPA", "destinations": ["NRT"], "dates": ["2026-09-01"], "hubs": ["MAD"],
        "adults": 1, "currency": "EUR", "best_price": 505.0,
        "best_route": "LPA->MAD->NRT 2026-09-01",
        "results": [{"hub": "MAD", "dest": "NRT", "date": "2026-09-01", "total": 505.0}],
        "trip_days": 0,
    }
    defaults.update(db_kwargs)
    return await db_module.save_search(**defaults)


async def test_save_favorite_forwards_the_stored_search_s_provider(temp_db, monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)

    search_id = await _save_search_with({"provider": "kiwi"})

    await save_favorite(_update(f"savefav_{search_id}"), None)

    fav = (await db_module.get_favorites())[0]
    assert fav["provider"] == "kiwi", (
        "the favourite must record the same provider the price was quoted "
        "under, so the scheduler can replay that exact query shape"
    )


async def test_save_favorite_leaves_provider_unset_when_the_search_predates_it(
    temp_db, monkeypatch
):
    """A search saved before Task 11's migration (or otherwise missing a
    provider tag) has provider=None. There is nothing true to forward, so
    the favourite honestly records "unknown" rather than a guess -- the
    scheduler's own explicit fallback to the primary provider is what keeps
    an untagged favourite trackable."""
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)

    search_id = await _save_search_with({})  # provider omitted -> NULL

    await save_favorite(_update(f"savefav_{search_id}"), None)

    fav = (await db_module.get_favorites())[0]
    assert fav["provider"] is None
