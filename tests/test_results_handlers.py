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
