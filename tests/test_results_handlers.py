"""handlers.results: the live message's progress, cancel, and callbacks."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import handlers.start as start_module
from handlers.results import RUNS_KEY, ProgressMessage, on_cancel
from models import CancelToken, Progress

_OWNER_ID = 4242


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)


class FakeBot:
    def __init__(self):
        self.log: list[tuple[str, dict]] = []
        self._next_id = 100

    async def edit_message_text(self, **kw):
        self.log.append(("edit", kw))

    async def send_message(self, **kw):
        self.log.append(("send", kw))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    @property
    def texts(self):
        return [kw["text"] for _, kw in self.log]


class FakeQuery:
    def __init__(self, data, message_id=42):
        self.data = data
        self.message = SimpleNamespace(message_id=message_id)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


def _update(data):
    return SimpleNamespace(
        callback_query=FakeQuery(data),
        effective_user=SimpleNamespace(id=_OWNER_ID),
        effective_chat=SimpleNamespace(id=1),
    )


def _context(bot=None, runs=None):
    bot = bot or FakeBot()
    return SimpleNamespace(bot=bot, application=SimpleNamespace(
        bot=bot, bot_data={RUNS_KEY: runs if runs is not None else {}}))


def _progress_message(bot, sleep=asyncio.sleep):
    return ProgressMessage(bot, chat_id=1, message_id=42, strategy="two-stage",
                           currency="EUR", cancel_data="x:ab12", sleep=sleep)


# ── ProgressMessage ─────────────────────────────────────────────────────────

async def test_a_failed_flush_is_retried_not_raised():
    from telegram.error import NetworkError

    class FlakyBot(FakeBot):
        fail = True

        async def edit_message_text(self, **kw):
            if self.fail:
                self.fail = False
                raise NetworkError("flaky")
            await super().edit_message_text(**kw)

    bot = FlakyBot()
    view = _progress_message(bot)
    view.tick(Progress(phase="Phase 0", done=1, total=4))
    await view.flush()          # fails quietly
    await view.flush()          # same text, but never shown, so it retries
    assert bot.texts == ["Scanning price calendars… 1/4"]


async def test_flush_skips_an_unchanged_text():
    bot = FakeBot()
    view = _progress_message(bot)
    view.tick(Progress(phase="Phase 0", done=1, total=4))
    await view.flush()
    view.tick(Progress(phase="Phase 0", done=1, total=4))
    await view.flush()
    assert bot.texts == ["Scanning price calendars… 1/4"]


async def test_the_loop_waits_the_interval_before_every_edit():
    """At most one edit per interval: the loop sleeps first, every time."""
    bot = FakeBot()
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError
        view.tick(Progress(phase="Phase 0", done=len(sleeps), total=4))

    view = _progress_message(bot, sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await view._loop(3.0)

    assert sleeps == [3.0, 3.0, 3.0]
    assert len(bot.texts) == 2


# ── Cancel ──────────────────────────────────────────────────────────────────

async def test_cancel_sets_the_running_search_s_token():
    token = CancelToken()
    update = _update("x:ab12")
    await on_cancel(update, _context(runs={"ab12": token}))
    assert token.cancelled
    assert update.callback_query.answers == [("Cancelling…", False)]


async def test_cancel_after_the_search_finished_says_so():
    update = _update("x:ab12")
    await on_cancel(update, _context(runs={}))
    assert update.callback_query.answers == [("That search is no longer running.", True)]


import json

import db as db_module
from handlers.results import on_results
from results.store import serialize
from tests.results_fixtures import standard_one_way


async def _saved(results, **kw) -> int:
    fields = {"origin": "LPA", "destinations": ["NRT"], "dates": ["2026-10-01"],
              "hubs": ["MAD"], "adults": 1, "currency": "EUR", "best_price": 525.0,
              "best_route": "x", "results": results, "trip_days": 0,
              "window_start": "2026-10-01", "window_end": "2026-10-01",
              "provider": "kiwi", "strategy": "two-stage"}
    fields.update(kw)
    return await db_module.save_search(**fields)


def _last(bot):
    _, kw = bot.log[-1]
    data = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    return kw["text"], data


async def _tap(data, bot=None):
    bot = bot or FakeBot()
    update = _update(data)
    await on_results(update, _context(bot))
    return bot, update


async def test_page_detail_and_back(temp_db):
    sid = await _saved(serialize([standard_one_way(date=f"2026-10-{d:02d}")
                                  for d in range(1, 8)]))
    bot, _ = await _tap(f"r:{sid}:p:2")
    text, data = _last(bot)
    assert "6. " in text
    assert f"r:{sid}:d:6" in data

    bot, _ = await _tap(f"r:{sid}:d:6", bot)
    text, data = _last(bot)
    assert "Ticket 1" in text
    assert f"r:{sid}:p:2" in data, "Back returns to the page the user was on"


async def test_setting_a_filter_persists_and_resets_to_page_one(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    await db_module.set_search_view(sid, {"filters": {}, "page": 3})

    bot, _ = await _tap(f"r:{sid}:f:s:0")

    row = await db_module.get_search_by_id(sid)
    assert json.loads(row["view_json"]) == {
        "filters": {"max_stops": 0, "max_hours": None, "min_buffer_hours": None,
                    "exclude": []},
        "page": 1,
    }
    assert "1 of 1 routes shown" in _last(bot)[0]


async def test_clear_resets_every_filter(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    await db_module.set_search_view(sid, {"filters": {"max_stops": 0}, "page": 1})
    await _tap(f"r:{sid}:f:clear")
    row = await db_module.get_search_by_id(sid)
    assert json.loads(row["view_json"])["filters"]["max_stops"] is None


async def test_a_deleted_search_says_it_is_gone(temp_db):
    bot, update = await _tap("r:999:p:1")
    assert _last(bot)[0] == "That search is no longer stored."
    assert update.callback_query.answers


async def test_an_out_of_range_index_or_bad_filter_is_treated_as_stale(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    for data in (f"r:{sid}:d:5", f"r:{sid}:f:s:9", f"r:{sid}:zz"):
        bot, _ = await _tap(data)
        assert _last(bot)[0] == "That search is no longer stored."


async def test_a_pre_3b_search_opens_as_a_snapshot_and_filters_fall_back(temp_db):
    """Review Focus #1: a v1 row reached from an old button or from history."""
    v1 = [{"date": "2026-10-01", "hub": "MAD", "hub_name": "Madrid", "dest": "NRT",
           "dest_name": "Tokyo", "discount": 0.75, "dom_price": 100.0,
           "onward_price": 500.0, "through_fare": None}]
    sid = await _saved(v1, strategy=None)

    bot, _ = await _tap(f"r:{sid}:p:1")
    text, data = _last(bot)
    assert "Historical snapshot" in text
    assert f"r:{sid}:f" not in data

    bot, _ = await _tap(f"r:{sid}:f", bot)
    assert "Historical snapshot" in _last(bot)[0], "no filter screen for a snapshot"


async def test_noop_only_answers(temp_db):
    bot, update = await _tap("r:1:n")
    assert bot.log == []
    assert update.callback_query.answers == [("", False)]


async def test_history_view_opens_the_results_summary(temp_db):
    from handlers.history import history_view

    edits = []

    class Query(FakeQuery):
        async def edit_message_text(self, text, **kw):
            edits.append((text, kw))

    sid = await _saved(serialize([standard_one_way()]))
    update = SimpleNamespace(callback_query=Query(f"hist_view_{sid}"),
                             effective_user=SimpleNamespace(id=_OWNER_ID),
                             effective_chat=SimpleNamespace(id=1))
    await history_view(update, _context())

    text, kw = edits[-1]
    assert "1 routes" in text
    data = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert f"r:{sid}:d:0" in data


async def test_track_this_trip_watches_its_exact_date(temp_db):
    sid = await _saved(serialize([standard_one_way(), standard_one_way(date="2026-10-05",
                                                                     hub="BCN")]),
                       dates=["2026-10-01", "2026-10-05"])
    bot, update = await _tap(f"r:{sid}:t:1")

    fav = (await db_module.get_favorites())[0]
    assert fav["hub"] == "BCN"
    assert json.loads(fav["check_dates"]) == ["2026-10-05"]
    assert fav["record_price"] == 525.0
    assert fav["provider"] == "kiwi"
    assert update.callback_query.answers == [("Tracking this trip.", False)]
    assert bot.log == [], "the detail stays on screen"


async def test_track_route_watches_every_searched_date(temp_db):
    sid = await _saved(serialize([standard_one_way()]),
                       dates=["2026-10-01", "2026-10-05"])
    await _tap(f"r:{sid}:T:0")
    fav = (await db_module.get_favorites())[0]
    assert json.loads(fav["check_dates"]) == ["2026-10-01", "2026-10-05"]


async def test_tracking_a_stale_index_changes_nothing(temp_db):
    sid = await _saved(serialize([standard_one_way()]))
    await _tap(f"r:{sid}:t:9")
    assert await db_module.get_favorites() == []
