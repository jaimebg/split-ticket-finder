"""Tests for the builder's glue layer (spec §6.2).

Two things live here that no other module can cover:

- The anchor lifecycle (``render_anchor``, 7 tests below). One message
  holds the draft and every sub-screen, edited in place. The two paths
  that can strand a user with no working panel -- the anchor being gone,
  and a refused delete of the user's typed echo -- both resend and
  re-anchor, and are the reason this file needs a fake bot at all.
- The handlers that wire draft.py/dates.py/places.py/hubs.py together:
  ``edit_field``'s field->screen/awaiting mapping, ``go``'s readiness
  gate, ``place_tap``'s MAX_DESTINATIONS cap, ``on_text``'s refused-delete
  path, and ``_load_ratings``'s cache key. These fail silently on a wrong
  mapping or a swallowed exception, so a review found them under-tested;
  a fake Update/context (``SimpleNamespace``, following
  ``tests/test_search_flow.py``'s "fake the bot, not the Telegram
  scaffolding" approach) stands in for real PTB objects.

Every other rule worth testing on its own lives in draft.py, dates.py,
places.py and hubs.py, which need no bot.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden
from telegram.ext import ConversationHandler

import handlers.start as start_module
from handlers.search import builder
from handlers.search.builder import render_anchor
from handlers.search.draft import (
    AWAIT_DEST,
    AWAIT_HUBS,
    MAX_DESTINATIONS,
    SCREEN_DATES,
    SCREEN_DEST,
    SCREEN_DRAFT,
    SCREEN_HUBS,
    SCREEN_TRIP,
    Button,
    SearchDraft,
)
from providers.base import Place, ProviderError, RatedPrice

_OWNER_ID = 918273645


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


# ── Fake Update/context scaffolding for the handlers below ──────────────────
#
# These handlers are plain async functions wrapped in @owner_only_callback /
# @owner_only, so a SimpleNamespace standing in for Update/context is enough
# -- no real telegram.Update/CallbackContext is built, matching
# tests/test_search_flow.py's "fake the bot, not the scaffolding" approach.


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(start_module, "OWNER_ID", _OWNER_ID)


class FakeQuery:
    """Just enough of telegram.CallbackQuery for the handlers under test."""

    def __init__(self, data: str):
        self.data = data
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class FakeMessage:
    """Just enough of telegram.Message for on_text."""

    def __init__(self, text: str, *, delete_error=None):
        self.text = text
        self.deleted = False
        self._delete_error = delete_error

    async def delete(self):
        if self._delete_error:
            raise self._delete_error
        self.deleted = True


class FakeApplication:
    """Records what create_task was handed without running it eagerly.

    A caller that wants to see the scheduled coroutine's side effects
    awaits ``scheduled[i]`` itself -- create_task is fire-and-forget in the
    real Application too, so nothing here should run it implicitly.
    """

    def __init__(self, bot):
        self.bot = bot
        self.scheduled: list = []

    def create_task(self, coro, update=None):
        self.scheduled.append(coro)
        return None


def _context(bot: FakeBot | None = None) -> SimpleNamespace:
    bot = bot or FakeBot()
    return SimpleNamespace(bot=bot, user_data={}, application=FakeApplication(bot))


def _cb_update(data: str, *, chat_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        callback_query=FakeQuery(data),
        effective_user=SimpleNamespace(id=_OWNER_ID),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _text_update(text: str, *, chat_id: int = 1, delete_error=None) -> SimpleNamespace:
    return SimpleNamespace(
        message=FakeMessage(text, delete_error=delete_error),
        effective_user=SimpleNamespace(id=_OWNER_ID),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def _draft(**kw) -> SearchDraft:
    base = {"origin": "LPA", "origin_name": "Gran Canaria"}
    base.update(kw)
    return SearchDraft(**base)


def _place(code: str, city: str) -> Place:
    return Place(code=code, name=f"{city} Airport", city=city, country="Testland",
                place_id=code.lower())


def _set_draft(context, draft: SearchDraft) -> None:
    context.user_data[builder._DRAFT] = draft


# ── SCREEN_HUBS: multi-select via name search (review finding 1) ────────────
#
# hubs_mod.render_hubs is a static preset grid; only places_mod.render_picker
# keeps a list of matches on screen across taps. _show must pick the picker
# whenever a hub search is active, exactly like it already does for
# SCREEN_DEST -- otherwise the first tap after a name search reverts to the
# grid and discards every other match, breaking multi-select for hubs only.


async def test_hubs_screen_shows_the_grid_with_no_search_active():
    context = _context()
    _set_draft(context, _draft(screen=SCREEN_HUBS, awaiting=AWAIT_HUBS))

    await builder._show(_cb_update("noop"), context)

    markup = context.bot.sends[0]["reply_markup"]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any(d.startswith("hp:") for d in datas), "presets are the default view"


async def test_hubs_screen_shows_the_picker_while_a_search_is_active():
    context = _context()
    _set_draft(context, _draft(screen=SCREEN_HUBS, awaiting=AWAIT_HUBS))
    context.user_data[builder._RESULTS] = [_place("LIS", "Lisbon"), _place("OPO", "Porto")]
    context.user_data[builder._TERM] = "Portugal"

    await builder._show(_cb_update("noop"), context)

    markup = context.bot.sends[0]["reply_markup"]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "p:hubs:LIS" in datas and "p:hubs:OPO" in datas
    assert not any(d.startswith("hp:") for d in datas), "the grid must not reappear"


async def test_picking_one_hub_match_keeps_the_rest_selectable():
    """The regression this finding exists to fix: picking LIS must not
    discard OPO from the still-open picker."""
    context = _context()
    _set_draft(context, _draft(screen=SCREEN_HUBS, awaiting=AWAIT_HUBS))
    context.user_data[builder._RESULTS] = [_place("LIS", "Lisbon"), _place("OPO", "Porto")]
    context.user_data[builder._TERM] = "Portugal"

    await builder.place_tap(_cb_update("p:hubs:LIS"), context)

    draft = builder._draft_of(context)
    assert draft.hub_codes == ("LIS",)
    assert draft.screen == SCREEN_HUBS
    markup = context.bot.sends[-1]["reply_markup"]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "p:hubs:OPO" in datas, "the second match must survive the first pick"


async def test_picking_a_hub_by_name_keeps_its_resolved_name():
    context = _context()
    _set_draft(context, _draft(screen=SCREEN_HUBS, awaiting=AWAIT_HUBS))
    context.user_data[builder._RESULTS] = [_place("ZRH", "Zurich")]
    context.user_data[builder._TERM] = "Zurich"

    await builder.place_tap(_cb_update("p:hubs:ZRH"), context)

    assert dict(builder._draft_of(context).hubs)["ZRH"] == "Zurich"


async def test_back_clears_the_search_so_hubs_reopens_on_the_grid():
    context = _context()
    _set_draft(context, _draft(screen=SCREEN_HUBS, awaiting=AWAIT_HUBS,
                               hubs=(("LIS", "Lisbon"),)))
    context.user_data[builder._RESULTS] = [_place("LIS", "Lisbon")]
    context.user_data[builder._TERM] = "Lisbon"

    await builder.back(_cb_update("back"), context)

    assert builder._RESULTS not in context.user_data
    assert builder._TERM not in context.user_data
    assert builder._draft_of(context).screen == SCREEN_DRAFT

    # Re-entering must land on the grid, not a stale picker. The anchor is
    # already set from back()'s own render, so this second render is an
    # edit, not a resend.
    await builder.edit_field(_cb_update("edit:hubs"), context)
    markup = context.bot.edits[-1]["reply_markup"]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any(d.startswith("hp:") for d in datas)


# ── edit_field's field -> screen/awaiting mapping ────────────────────────────


@pytest.mark.parametrize(("field", "expected_screen", "expected_awaiting"), [
    ("dest", SCREEN_DEST, AWAIT_DEST),
    ("hubs", SCREEN_HUBS, AWAIT_HUBS),
    ("trip", SCREEN_TRIP, None),
    ("dates", SCREEN_DATES, None),
])
async def test_edit_field_opens_the_matching_screen(field, expected_screen, expected_awaiting):
    context = _context()
    _set_draft(context, _draft())

    await builder.edit_field(_cb_update(f"edit:{field}"), context)

    draft = builder._draft_of(context)
    assert draft.screen == expected_screen
    assert draft.awaiting == expected_awaiting


# ── go's readiness gate ───────────────────────────────────────────────────────

_READY_DRAFT_KWARGS = {
    "destinations": (("NRT", "Tokyo"),),
    "hubs": (("MAD", "Madrid"),),
    "trip_days": 0,
    "window_start": "2026-09-10",
    "window_end": "2026-09-12",
}


async def test_go_alerts_the_missing_field_and_schedules_nothing():
    context = _context()
    missing_hubs = dict(_READY_DRAFT_KWARGS)
    missing_hubs["hubs"] = ()
    _set_draft(context, _draft(**missing_hubs))
    update = _cb_update("go")

    result = await builder.go(update, context)

    assert result == builder.BUILDING
    text, show_alert = update.callback_query.answers[0]
    assert show_alert is True
    assert "hubs" in text
    assert not context.application.scheduled, "an incomplete draft must not start a search"
    assert not update.callback_query.edits


async def test_go_schedules_run_and_report_with_the_draft_s_params(monkeypatch):
    calls = []

    async def fake_run_and_report(bot, chat_id, params):
        calls.append((bot, chat_id, params))

    monkeypatch.setattr(builder, "run_and_report", fake_run_and_report)

    context = _context()
    draft = _draft(**_READY_DRAFT_KWARGS)
    _set_draft(context, draft)
    update = _cb_update("go", chat_id=777)

    result = await builder.go(update, context)

    assert result == ConversationHandler.END
    assert update.callback_query.edits == ["On it — I'll message you when it's done."]
    assert len(context.application.scheduled) == 1

    await context.application.scheduled[0]
    bot, chat_id, params = calls[0]
    assert bot is context.application.bot
    assert chat_id == 777
    assert params == draft.to_params()
    assert context.user_data == {}, "the draft must not survive a launched search"


# ── place_tap's MAX_DESTINATIONS cap ─────────────────────────────────────────


async def test_place_tap_refuses_a_destination_past_the_cap():
    context = _context()
    ten = tuple((f"D{i:02d}", f"Dest {i}") for i in range(MAX_DESTINATIONS))
    _set_draft(context, _draft(destinations=ten))
    context.user_data[builder._RESULTS] = [_place("NEW", "Newcity")]
    update = _cb_update("p:dest:NEW")

    result = await builder.place_tap(update, context)

    assert result == builder.BUILDING
    text, show_alert = update.callback_query.answers[0]
    assert show_alert is True
    assert str(MAX_DESTINATIONS) in text
    assert builder._draft_of(context).destinations == ten, "the cap must block the add"
    assert not context.bot.edits and not context.bot.sends, "a refused tap must not re-render"


async def test_place_tap_adds_a_destination_under_the_cap():
    context = _context()
    nine = tuple((f"D{i:02d}", f"Dest {i}") for i in range(MAX_DESTINATIONS - 1))
    _set_draft(context, _draft(destinations=nine))
    context.user_data[builder._RESULTS] = [_place("NRT", "Tokyo")]
    update = _cb_update("p:dest:NRT")

    await builder.place_tap(update, context)

    assert builder._draft_of(context).dest_codes == (*[c for c, _ in nine], "NRT")


# ── on_text's refused-delete path ────────────────────────────────────────────


async def test_on_text_edits_the_anchor_when_the_delete_succeeds():
    context = _context()
    context.user_data[builder._ANCHOR] = 42
    _set_draft(context, _draft())

    await builder.on_text(_text_update("hello"), context)

    assert len(context.bot.edits) == 1
    assert not context.bot.sends
    assert context.user_data[builder._ANCHOR] == 42


async def test_on_text_forces_a_resend_when_the_delete_is_refused():
    context = _context()
    context.user_data[builder._ANCHOR] = 42
    _set_draft(context, _draft())
    update = _text_update("hello", delete_error=BadRequest("no rights"))

    await builder.on_text(update, context)

    assert len(context.bot.sends) == 1, "a refused delete must force a resend, not an edit"
    assert not context.bot.edits
    assert context.user_data[builder._ANCHOR] != 42


# ── _load_ratings's cache key and gating ─────────────────────────────────────


class _FakeCalendarProvider:
    """Enough of SupportsCalendar for isinstance() and one scripted call."""

    name = "fake-cal"

    def __init__(self, *, table=None, error=None):
        self.table = table or {}
        self.error = error
        self.calls: list = []

    async def price_calendar(self, query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.table

    async def search_leg(self, query):
        raise AssertionError("not called by these tests")

    async def aclose(self):
        return None


class _FakeNoCalendarProvider:
    """No price_calendar -- fails isinstance(_, SupportsCalendar)."""

    name = "fake-nocal"

    async def search_leg(self, query):
        raise AssertionError("not called by these tests")

    async def aclose(self):
        return None


async def test_load_ratings_does_nothing_without_a_destination(monkeypatch):
    provider = _FakeCalendarProvider(table={"2026-09-05": RatedPrice(Decimal("100"), "CHEAP")})
    monkeypatch.setattr(builder, "primary_provider", lambda: provider)
    context = _context()

    await builder._load_ratings(context, _draft())

    assert not provider.calls
    assert builder._RATINGS not in context.user_data


async def test_load_ratings_does_nothing_without_a_calendar_capable_provider(monkeypatch):
    monkeypatch.setattr(builder, "primary_provider", lambda: _FakeNoCalendarProvider())
    context = _context()

    await builder._load_ratings(context, _draft(destinations=(("NRT", "Tokyo"),)))

    assert builder._RATINGS not in context.user_data


# A month far enough in the future that it can never fall into
# month_rows's "past day" branch, whatever the real wall-clock date is when
# this suite runs -- unlike test_search_dates.py's pure functions, _today()
# here is not injectable.
_FUTURE_YEAR, _FUTURE_MONTH = 2030, 1
_FUTURE_RATED_DAY = f"{_FUTURE_YEAR:04d}-{_FUTURE_MONTH:02d}-05"


async def test_load_ratings_caches_under_the_key_dates_screen_reads(monkeypatch):
    provider = _FakeCalendarProvider(
        table={_FUTURE_RATED_DAY: RatedPrice(Decimal("100"), "CHEAP")}
    )
    monkeypatch.setattr(builder, "primary_provider", lambda: provider)
    context = _context()
    context.user_data[builder._MONTH] = (_FUTURE_YEAR, _FUTURE_MONTH)
    draft = _draft(destinations=(("NRT", "Tokyo"),))

    await builder._load_ratings(context, draft)

    dest = draft.dest_codes[0]
    key = f"{dest}:{_FUTURE_YEAR}-{_FUTURE_MONTH}"
    assert context.user_data[builder._RATINGS][key] == {_FUTURE_RATED_DAY: "CHEAP"}

    # _dates_screen must land on the exact same key -- the coloured cell is
    # the observable proof the two sides agree.
    _, rows = builder._dates_screen(context, draft)
    labels = [b.label for row in rows for b in row]
    assert any(lbl.startswith("🟢") and "5" in lbl for lbl in labels)


async def test_load_ratings_swallows_a_provider_error_and_renders_uncoloured(monkeypatch):
    provider = _FakeCalendarProvider(error=ProviderError("boom"))
    monkeypatch.setattr(builder, "primary_provider", lambda: provider)
    context = _context()
    context.user_data[builder._MONTH] = (_FUTURE_YEAR, _FUTURE_MONTH)
    draft = _draft(destinations=(("NRT", "Tokyo"),))

    await builder._load_ratings(context, draft)  # must not raise

    key = f"{draft.dest_codes[0]}:{_FUTURE_YEAR}-{_FUTURE_MONTH}"
    assert context.user_data[builder._RATINGS][key] == {}

    _, rows = builder._dates_screen(context, draft)
    labels = [b.label for row in rows for b in row]
    assert not any("🟢" in lbl or "🔴" in lbl or "🟡" in lbl for lbl in labels)
