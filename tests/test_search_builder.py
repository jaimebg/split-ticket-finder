"""Tests for the builder's anchor lifecycle (spec §6.2).

One message holds the draft and every sub-screen, edited in place. The two
paths that can strand a user with no working panel -- the anchor being
gone, and a refused delete of the user's typed echo -- both resend and
re-anchor, and are the reason this file needs a fake bot at all. Every
other rule worth testing lives in draft.py, dates.py, places.py and
hubs.py, which need no bot.
"""
from __future__ import annotations

from telegram.error import BadRequest, Forbidden

from handlers.search.builder import render_anchor
from handlers.search.draft import Button


class FakeBot:
    """Records edits and sends; can be told to fail either."""

    def __init__(self, *, edit_error=None):
        self.edit_error = edit_error
        self.edits: list[dict] = []
        self.sends: list[dict] = []
        self._next_id = 500

    async def edit_message_text(self, **kw):
        self.edits.append(kw)
        if self.edit_error:
            raise self.edit_error
        return None

    async def send_message(self, **kw):
        self.sends.append(kw)
        self._next_id += 1
        return type("Msg", (), {"message_id": self._next_id})()


ROWS = [[Button("Search", "go")]]


async def test_render_edits_the_anchor_in_place():
    bot = FakeBot()

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live == 42
    assert len(bot.edits) == 1
    assert not bot.sends


async def test_an_unchanged_edit_is_not_an_error():
    """Telegram rejects an identical edit with 'Message is not modified'.
    Tapping a toggle twice must not look like a crash -- the same failure
    §6.6 calls out for the progress message."""
    bot = FakeBot(edit_error=BadRequest("Message is not modified"))

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live == 42
    assert not bot.sends


async def test_a_missing_anchor_is_resent_and_re_anchored():
    """If the user deleted the panel, editing it fails forever. Resending
    is the only way back; returning the new id is how the caller keeps
    editing the right message."""
    bot = FakeBot(edit_error=BadRequest("Message to edit not found"))

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live != 42
    assert len(bot.sends) == 1
    assert bot.sends[0]["text"] == "draft"


async def test_a_forbidden_edit_is_resent_too():
    bot = FakeBot(edit_error=Forbidden("no rights"))

    live = await render_anchor(bot, chat_id=1, message_id=42,
                               text="draft", rows=ROWS)

    assert live != 42
    assert len(bot.sends) == 1


async def test_render_sends_a_first_anchor_when_there_is_none():
    bot = FakeBot()

    live = await render_anchor(bot, chat_id=1, message_id=None,
                               text="draft", rows=ROWS)

    assert live == 501
    assert not bot.edits


async def test_render_uses_html_and_suppresses_link_previews():
    bot = FakeBot()

    await render_anchor(bot, chat_id=1, message_id=42, text="a <b>b</b>",
                        rows=ROWS)

    assert bot.edits[0]["parse_mode"] == "HTML"
    assert bot.edits[0]["disable_web_page_preview"] is True


async def test_rows_become_a_real_inline_keyboard():
    bot = FakeBot()

    await render_anchor(bot, chat_id=1, message_id=42, text="draft",
                        rows=[[Button("Search", "go"), Button("Reset", "reset")]])

    markup = bot.edits[0]["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "Search"
    assert markup.inline_keyboard[0][1].callback_data == "reset"
