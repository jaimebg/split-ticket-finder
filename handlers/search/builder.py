"""The hub-and-spoke builder (spec §6.2).

One anchor message holds the draft and every sub-screen, edited in place.
ConversationHandler keeps a single BUILDING state; which screen is showing
is a field on the draft, so Back is a re-render rather than a transition
and "Back and Edit exist everywhere" is true by construction.

Free text breaks the single-panel illusion -- Telegram appends the user's
message, so the panel is no longer last on screen. The handler therefore
deletes it after reading. Both failure paths (the anchor is gone, the
delete is refused) resend and re-anchor rather than stranding the user.
"""
from __future__ import annotations

import logging
from datetime import date as date_cls

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ELIGIBLE_ORIGINS, ORIGIN
from handlers.search import dates as dates_mod
from handlers.search import hubs as hubs_mod
from handlers.search import places as places_mod
from handlers.search.draft import (
    AWAIT_DEST,
    AWAIT_HUBS,
    AWAIT_TRIP_DAYS,
    MAX_DESTINATIONS,
    MAX_TRIP_DAYS,
    SCREEN_DATES,
    SCREEN_DEST,
    SCREEN_DRAFT,
    SCREEN_HUBS,
    SCREEN_TRIP,
    Button,
    Rows,
    SearchDraft,
)
from handlers.search_flow import _estimate_queries, run_and_report
from handlers.start import MAIN_MENU_KEYBOARD, owner_only, owner_only_callback
from handlers.utils import ValidationError, parse_positive_int
from providers.base import ProviderError, SupportsCalendar
from providers.registry import primary_provider

logger = logging.getLogger(__name__)

BUILDING = 0

_ANCHOR = "anchor_id"
_DRAFT = "draft"
_MONTH = "month"
_RATINGS = "ratings"
_RESULTS = "results"
_TERM = "term"

_TRIP_PRESETS = (7, 10, 14, 21)


# ── The anchor ───────────────────────────────────────────────────────────────

def _markup(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.label, callback_data=b.data) for b in row]
         for row in rows]
    )


async def render_anchor(bot, chat_id: int, message_id: int | None,
                        text: str, rows: Rows) -> int:
    """Show *text* in the anchor, returning the live message id.

    The returned id differs from *message_id* when a resend was needed.
    Callers must store it back, or every later edit targets a message that
    is no longer there.

    An identical edit is not an error: Telegram rejects it with
    "Message is not modified", which is a no-op, not a failure -- the same
    case §6.6 calls out for the progress message. Any other edit failure
    means the panel is unusable, so it is resent.
    """
    markup = _markup(rows)

    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                parse_mode="HTML", reply_markup=markup,
                disable_web_page_preview=True,
            )
            return message_id
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return message_id
            logger.info("Anchor %s unusable (%s) — resending.", message_id, exc)
        except Forbidden as exc:
            logger.info("Anchor %s forbidden (%s) — resending.", message_id, exc)

    message = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
        disable_web_page_preview=True,
    )
    return message.message_id


def _draft_of(context) -> SearchDraft:
    return context.user_data[_DRAFT]


def _store(context, draft: SearchDraft) -> None:
    context.user_data[_DRAFT] = draft


def _today() -> str:
    return date_cls.today().strftime("%Y-%m-%d")


async def _show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Render whichever screen the draft says is current."""
    draft = _draft_of(context)
    chat_id = update.effective_chat.id

    if draft.screen == SCREEN_DATES:
        text, rows = _dates_screen(context, draft)
    elif draft.screen == SCREEN_HUBS:
        text, rows = hubs_mod.render_hubs(draft)
    elif draft.screen == SCREEN_DEST:
        text, rows = places_mod.render_picker(
            draft, field="dest",
            results=context.user_data.get(_RESULTS, []),
            term=context.user_data.get(_TERM, ""),
        )
    elif draft.screen == SCREEN_TRIP:
        text, rows = _trip_screen()
    else:
        estimate = _estimate_queries(
            hubs=len(draft.hubs), dests=len(draft.destinations),
            dates=len(draft.effective_dates), round_trip=bool(draft.trip_days),
        ) if draft.is_ready else None
        text, rows = draft.render(estimate=estimate)

    live = await render_anchor(context.bot, chat_id,
                               context.user_data.get(_ANCHOR), text, rows)
    context.user_data[_ANCHOR] = live
    return BUILDING


def _dates_screen(context, draft: SearchDraft) -> tuple[str, Rows]:
    year, month = context.user_data.get(_MONTH, _current_month())
    dest = draft.dest_codes[0] if draft.destinations else None
    ratings = context.user_data.get(_RATINGS, {}).get(f"{dest}:{year}-{month}")
    rows = dates_mod.month_rows(year, month, draft=draft, today=_today(),
                                ratings=ratings)
    return dates_mod.caption(draft, dest_code=dest if ratings else None), rows


def _current_month() -> tuple[int, int]:
    today = date_cls.today()
    return today.year, today.month


def _trip_screen() -> tuple[str, Rows]:
    rows: Rows = [
        [Button("One-way", "trip:0")],
        [Button(f"{d} days", f"trip:{d}") for d in _TRIP_PRESETS[:2]],
        [Button(f"{d} days", f"trip:{d}") for d in _TRIP_PRESETS[2:]],
        [Button("Custom…", "trip:custom")],
        [Button("⬅️ Back", "back")],
    ]
    return ("<b>One-way or round-trip?</b>\n\n"
            "For a round trip, pick how long the trip lasts.", rows)


# ── Entry and exit ───────────────────────────────────────────────────────────

@owner_only_callback
async def entry_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open a fresh draft, taking over the message the menu button was on."""
    query = update.callback_query
    await query.answer()

    context.user_data.clear()
    context.user_data[_ANCHOR] = query.message.message_id
    _store(context, SearchDraft(
        origin=ORIGIN,
        origin_name=ELIGIBLE_ORIGINS.get(ORIGIN, ORIGIN),
    ))
    return await _show(update, context)


@owner_only_callback
async def back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the draft from any sub-screen. The whole point of §6.2."""
    await update.callback_query.answer()
    context.user_data.pop(_RESULTS, None)
    context.user_data.pop(_TERM, None)
    _store(context, _draft_of(context).with_(screen=SCREEN_DRAFT, awaiting=None))
    return await _show(update, context)


@owner_only_callback
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("Draft cleared.")
    anchor = context.user_data.get(_ANCHOR)
    context.user_data.clear()
    context.user_data[_ANCHOR] = anchor
    _store(context, SearchDraft(
        origin=ORIGIN, origin_name=ELIGIBLE_ORIGINS.get(ORIGIN, ORIGIN),
    ))
    return await _show(update, context)


@owner_only_callback
async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open the sub-screen for one field."""
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]

    screens = {"dest": SCREEN_DEST, "trip": SCREEN_TRIP,
               "dates": SCREEN_DATES, "hubs": SCREEN_HUBS}
    awaiting = {"dest": AWAIT_DEST, "hubs": AWAIT_HUBS}

    draft = _draft_of(context).with_(screen=screens[field],
                                     awaiting=awaiting.get(field))

    if screens[field] == SCREEN_DATES:
        context.user_data[_MONTH] = _current_month()
        await _load_ratings(context, draft)

    _store(context, draft)
    return await _show(update, context)


async def _load_ratings(context, draft: SearchDraft) -> None:
    """Fetch the §6.4 direct-fare signal for the visible month, if possible.

    Silent on failure by design: the colours are a decoration and the grid
    renders uncoloured without them. Letting a ProviderError reach the user
    here would break a working picker over an optional hint.
    """
    if not draft.destinations:
        return
    provider = primary_provider()
    if not isinstance(provider, SupportsCalendar):
        return

    year, month = context.user_data.get(_MONTH, _current_month())
    dest = draft.dest_codes[0]
    key = f"{dest}:{year}-{month}"
    cache = context.user_data.setdefault(_RATINGS, {})
    if key in cache:
        return

    import calendar as _cal

    from providers.base import CalendarQuery

    last = _cal.monthrange(year, month)[1]
    try:
        table = await provider.price_calendar(CalendarQuery(
            origin=draft.origin, dest=dest,
            start=f"{year:04d}-{month:02d}-01",
            end=f"{year:04d}-{month:02d}-{last:02d}",
            adults=draft.adults, currency=draft.currency,
        ))
    except ProviderError as exc:
        logger.info("No date ratings for %s (%s) — rendering uncoloured.",
                    dest, exc)
        cache[key] = {}
        return

    cache[key] = {day: rated.rating for day, rated in table.items()}


# ── Dates ────────────────────────────────────────────────────────────────────

@owner_only_callback
async def date_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    date = query.data.split(":", 1)[1]

    draft, alert = dates_mod.apply_day_tap(_draft_of(context), date,
                                           today=_today())
    await query.answer(alert or "", show_alert=bool(alert))
    if alert:
        return BUILDING

    _store(context, draft)
    return await _show(update, context)


@owner_only_callback
async def month_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    year, month = (int(p) for p in query.data.split(":", 1)[1].split("-"))
    context.user_data[_MONTH] = (year, month)
    await _load_ratings(context, _draft_of(context))
    return await _show(update, context)


@owner_only_callback
async def date_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    preset = query.data.split(":", 1)[1]
    _store(context, dates_mod.apply_preset(_draft_of(context), preset,
                                           today=_today()))
    return await _show(update, context)


@owner_only_callback
async def date_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    _store(context, dates_mod.switch_mode(_draft_of(context), mode))
    return await _show(update, context)


@owner_only_callback
async def date_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _store(context, _draft_of(context).with_(window_start=None, window_end=None,
                                             picked_days=()))
    return await _show(update, context)


# ── Trip shape ───────────────────────────────────────────────────────────────

@owner_only_callback
async def trip_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "custom":
        _store(context, _draft_of(context).with_(awaiting=AWAIT_TRIP_DAYS))
        text = (f"<b>How long is the trip?</b>\n\nSend a number of days "
                f"(1–{MAX_TRIP_DAYS}).")
        rows: Rows = [[Button("⬅️ Back", "back")]]
        live = await render_anchor(context.bot, update.effective_chat.id,
                                   context.user_data.get(_ANCHOR), text, rows)
        context.user_data[_ANCHOR] = live
        return BUILDING

    _store(context, _draft_of(context).with_(trip_days=int(choice),
                                             screen=SCREEN_DRAFT, awaiting=None))
    return await _show(update, context)


# ── Hubs ─────────────────────────────────────────────────────────────────────

@owner_only_callback
async def hub_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _store(context, hubs_mod.toggle_hub(_draft_of(context),
                                        query.data.split(":", 1)[1]))
    return await _show(update, context)


@owner_only_callback
async def hub_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _store(context, hubs_mod.apply_hub_preset(_draft_of(context),
                                              query.data.split(":", 1)[1]))
    return await _show(update, context)


# ── Places ───────────────────────────────────────────────────────────────────

@owner_only_callback
async def place_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle one resolved place into or out of the draft."""
    query = update.callback_query
    _, field, code = query.data.split(":", 2)
    draft = _draft_of(context)

    results = {p.code: p for p in context.user_data.get(_RESULTS, [])}
    place = results.get(code)
    name = place.city if place else code

    if field == "dest":
        current = list(draft.destinations)
        if code in draft.dest_codes:
            current = [(c, n) for c, n in current if c != code]
        elif len(current) >= MAX_DESTINATIONS:
            await query.answer(
                f"At most {MAX_DESTINATIONS} destinations — the search would "
                "take hours.", show_alert=True)
            return BUILDING
        else:
            current.append((code, name))
        draft = draft.with_(destinations=tuple(current))
    else:
        # A hub found by name search keeps its resolved name; otherwise
        # toggle_hub falls back to the known-hub table, or the code itself.
        draft = hubs_mod.toggle_hub(draft, code, name=name)

    await query.answer()
    _store(context, draft)
    return await _show(update, context)


# ── Typed text ───────────────────────────────────────────────────────────────

@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Route a typed message to whichever screen is waiting for one.

    The user's message is deleted so the anchor stays last on screen. A
    refused delete is not fatal: _show resends the anchor, which puts the
    panel back in front of them.
    """
    text = update.message.text
    draft = _draft_of(context)

    try:
        await update.message.delete()
    except (BadRequest, Forbidden, TelegramError) as exc:
        # Telegram refuses after 48h or without delete rights. The panel is
        # now above the user's message, so force a resend rather than an edit.
        logger.info("Could not delete the user's message (%s) — re-anchoring.",
                    exc)
        context.user_data[_ANCHOR] = None

    if draft.awaiting == AWAIT_TRIP_DAYS:
        return await _handle_trip_text(update, context, text)
    if draft.awaiting in (AWAIT_DEST, AWAIT_HUBS):
        return await _handle_place_text(update, context, text, draft)
    return await _show(update, context)


async def _handle_trip_text(update, context, text: str) -> int:
    try:
        days = parse_positive_int(text, field="days", maximum=MAX_TRIP_DAYS)
    except ValidationError as exc:
        await update.effective_chat.send_message(str(exc), parse_mode="HTML")
        return BUILDING

    _store(context, _draft_of(context).with_(trip_days=days,
                                             screen=SCREEN_DRAFT, awaiting=None))
    return await _show(update, context)


async def _handle_place_text(update, context, text: str, draft) -> int:
    field = "dest" if draft.awaiting == AWAIT_DEST else "hubs"

    codes = places_mod.try_parse_codes(text)
    if codes is not None:
        # §6.3's power-user path: typed codes are accepted with no request.
        if field == "dest":
            known = dict(draft.destinations)
            merged = list(draft.destinations)
            for code in codes:
                if code not in known:
                    merged.append((code, code))
            _store(context, draft.with_(destinations=tuple(merged[:MAX_DESTINATIONS])))
        else:
            _store(context, hubs_mod.add_typed_hubs(draft, codes))
        context.user_data.pop(_RESULTS, None)
        return await _show(update, context)

    context.user_data[_TERM] = text
    error = None
    try:
        results = await places_mod.resolve(text)
    except ProviderError as exc:
        logger.warning("Place lookup failed for %r: %s", text, exc)
        results, error = [], "Name search is unavailable right now."

    context.user_data[_RESULTS] = results
    body, rows = places_mod.render_picker(draft, field=field, results=results,
                                          term=text, error=error)
    live = await render_anchor(context.bot, update.effective_chat.id,
                               context.user_data.get(_ANCHOR), body, rows)
    context.user_data[_ANCHOR] = live
    return BUILDING


# ── Launching the search ─────────────────────────────────────────────────────

@owner_only_callback
async def go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the search, or say what is still missing."""
    query = update.callback_query
    draft = _draft_of(context)

    if not draft.is_ready:
        await query.answer(f"Still needed: {', '.join(draft.missing)}",
                           show_alert=True)
        return BUILDING

    await query.answer()
    await query.edit_message_text("On it — I'll message you when it's done.")

    context.application.create_task(
        run_and_report(context.application.bot, update.effective_chat.id,
                       draft.to_params()),
        update=update,
    )
    context.user_data.clear()
    return ConversationHandler.END


@owner_only_callback
async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A padding or header cell. Telegram needs an answer or it spins."""
    await update.callback_query.answer()
    return BUILDING


@owner_only_callback
async def to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Welcome to Flight Finder!\n"
                                  "Use the menu below to get started.",
                                  reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Search cancelled.",
                                    reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


# ── Builder ──────────────────────────────────────────────────────────────────

def build_search_conversation() -> ConversationHandler:
    """The search conversation.

    One state. Every screen is a re-render of the same anchor from the same
    draft, so there is no transition table to keep consistent -- which is
    what made the old eleven-state chain unable to go backwards at all.
    """
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(entry_search, pattern="^menu_search$")],
        states={
            BUILDING: [
                CallbackQueryHandler(edit_field, pattern=r"^edit:"),
                CallbackQueryHandler(date_tap, pattern=r"^d:\d{4}-\d{2}-\d{2}$"),
                CallbackQueryHandler(month_nav, pattern=r"^m:\d{4}-\d{1,2}$"),
                CallbackQueryHandler(date_preset, pattern=r"^dp:"),
                CallbackQueryHandler(date_mode, pattern=r"^dm:"),
                CallbackQueryHandler(date_clear, pattern="^dclear$"),
                CallbackQueryHandler(trip_choice, pattern=r"^trip:"),
                CallbackQueryHandler(hub_tap, pattern=r"^h:[A-Z]{3}$"),
                CallbackQueryHandler(hub_preset, pattern=r"^hp:"),
                CallbackQueryHandler(place_tap, pattern=r"^p:(dest|hubs):[A-Z]{3}$"),
                CallbackQueryHandler(back, pattern="^back$"),
                CallbackQueryHandler(reset, pattern="^reset$"),
                CallbackQueryHandler(go, pattern="^go$"),
                CallbackQueryHandler(to_menu, pattern="^menu_main$"),
                # NOOP is imported from dates.py rather than hardcoded here so
                # the padding-cell callback data and this router pattern can
                # never drift apart.
                CallbackQueryHandler(noop, pattern=f"^{dates_mod.NOOP}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
