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
from results.store import serialize
from tests.results_fixtures import standard_one_way

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
        "results": serialize([standard_one_way(date="2026-09-01")]),
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


async def test_save_favorite_tracks_the_cheapest_stored_result(temp_db, monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    search_id = await _save_search_with({
        "results": serialize([standard_one_way(date="2026-09-01", through_fare="900"),
                              standard_one_way(date="2026-09-02", hub="BCN",
                                               discount="1")]),
    })

    await save_favorite(_update(f"savefav_{search_id}"), None)

    fav = (await db_module.get_favorites())[0]
    assert fav["hub"] == "BCN"            # 500.00 beats 525.00
    assert fav["record_price"] == 500.0


async def test_save_favorite_stores_the_search_s_options(temp_db, monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    search_id = await _save_search_with({"adults": 2, "children": 1, "cabin": "BUSINESS",
                                         "max_stops": 0})
    await save_favorite(_update(f"savefav_{search_id}"), None)
    fav = (await db_module.get_favorites())[0]
    assert (fav["adults"], fav["children"], fav["cabin"], fav["max_stops"]) == \
        (2, 1, "BUSINESS", 0)


def test_the_favourite_line_shows_currency_options_and_average():
    from datetime import date

    from handlers.utils import format_favorite
    from results.history import price_stats

    fav = {"id": 1, "origin": "LPA", "hub": "MAD", "destination": "NRT", "trip_days": 0,
           "adults": 2, "cabin": "BUSINESS", "currency": "USD", "record_price": 700.0,
           "last_price": 704.0, "last_checked": "2026-10-30T08:00:00Z",
           "check_dates": '["2026-11-01"]'}
    stats = price_stats([("2026-10-20T08:00:00Z", 800.0), ("2026-10-30T08:00:00Z", 704.0)],
                        today=date(2026, 10, 31))
    text = format_favorite(fav, "LPA", stats)
    assert "2 adults · Business · USD" in text
    assert "6% below its 30-day average" in text
    assert "6% below" not in format_favorite(fav, "LPA")      # no stats, no line


class _Query:
    def __init__(self, data):
        self.data = data
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def _upd(data):
    return SimpleNamespace(callback_query=_Query(data), effective_user=SimpleNamespace(id=_OWNER_ID))


async def test_the_list_offers_history_per_favourite(temp_db, monkeypatch):
    from handlers.favorites import favorites_menu

    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    fav = await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                       currency="EUR", price=700.0, check_dates=["2026-10-01"])
    update = _upd("menu_favorites")
    await favorites_menu(update, None)
    text, kw = update.callback_query.edits[-1]
    data = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert f"fh:{fav}" in data and f"delfav_{fav}" in data
    assert "1 adult · Economy · EUR" in text


async def test_the_history_screen_and_a_deleted_favourite(temp_db, monkeypatch):
    """Review Focus #4 (the deleted half)."""
    from handlers.favorites import favorite_history

    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    fav = await db_module.add_favorite(origin="LPA", hub="MAD", destination="NRT", adults=1,
                                       currency="EUR", price=700.0, check_dates=["2026-10-01"])
    for price in (800.0, 700.0):
        await db_module.add_price_check(fav, price, None)

    update = _upd(f"fh:{fav}")
    await favorite_history(update, None)
    assert "Last 700 EUR (−100 since the previous check)" in update.callback_query.edits[-1][0]

    gone = _upd(f"fh:{fav + 50}")
    await favorite_history(gone, None)
    assert gone.callback_query.edits[-1][0] == "That route is no longer tracked."


async def test_every_favourite_gets_its_own_history_and_delete_buttons(temp_db, monkeypatch):
    from handlers.favorites import favorites_menu

    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)
    ids = [await db_module.add_favorite(origin="LPA", hub=hub, destination="NRT", adults=1,
                                        currency="EUR", price=700.0, check_dates=["2026-10-01"])
           for hub in ("MAD", "BCN", "LIS")]
    update = _upd("menu_favorites")
    await favorites_menu(update, None)
    data = {b.callback_data for row in update.callback_query.edits[-1][1]["reply_markup"].inline_keyboard
            for b in row}
    for fav in ids:
        assert f"fh:{fav}" in data and f"delfav_{fav}" in data
