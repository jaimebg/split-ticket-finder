"""Guided search conversation handler — walks the user through building a search."""
from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import DEFAULT_HUBS, ORIGIN, PORTUGAL_HUBS, SPAIN_HUBS
from db import save_search
from engine import run_search
from handlers.start import MAIN_MENU_KEYBOARD, owner_only, owner_only_callback
from handlers.utils import (
    ValidationError,
    esc,
    parse_date,
    parse_date_list,
    parse_iata_codes,
    parse_positive_int,
    split_message,
)
from models import SearchWindow, generate_dates
from search import format_results, itineraries_to_json, scan_to_json

logger = logging.getLogger(__name__)

# ── Conversation states ──────────────────────────────────────────────────────
(DEST, TRIP_TYPE, TRIP_DAYS, DATE_MODE, FIXED_DATES,
 RANGE_START, RANGE_END, RANGE_EVERY, HUBS, CUSTOM_HUBS, CONFIRM) = range(11)

MAX_TRIP_DAYS = 180
MAX_DESTINATIONS = 10

DATE_MODE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Fixed dates", callback_data="datemode_fixed"),
        InlineKeyboardButton("Date range", callback_data="datemode_range"),
    ],
])


# ── Entry point ──────────────────────────────────────────────────────────────

@owner_only_callback
async def entry_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: user tapped 'Search flights' in main menu."""
    query = update.callback_query
    await query.answer()

    context.user_data.clear()
    context.user_data.update(
        origin=ORIGIN,
        adults=1,
        currency="EUR",
        destinations={},
        dates=[],
        hubs={},
        trip_days=0,
    )

    await query.edit_message_text(
        "Where do you want to fly?\n"
        "Send destination airport codes separated by commas.\n"
        "Example: <code>JFK, LAX, MIA</code>",
        parse_mode="HTML",
    )
    return DEST


# ── DEST state ───────────────────────────────────────────────────────────────

@owner_only
async def dest_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends destination codes."""
    try:
        codes = parse_iata_codes(update.message.text, field="destination code")
    except ValidationError as exc:
        await update.message.reply_text(str(exc), parse_mode="HTML")
        return DEST

    if len(codes) > MAX_DESTINATIONS:
        await update.message.reply_text(
            f"That's {len(codes)} destinations — the search would take hours. "
            f"Please send at most {MAX_DESTINATIONS}.",
        )
        return DEST

    # Names are unknown for arbitrary codes, so the code doubles as the label.
    context.user_data["destinations"] = {c: c for c in codes}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("One-way", callback_data="trip_oneway"),
            InlineKeyboardButton("Round-trip", callback_data="trip_roundtrip"),
        ],
    ])
    await update.message.reply_text(
        f"Destinations: <b>{esc(', '.join(codes))}</b>\n\nOne-way or round-trip?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return TRIP_TYPE


# ── TRIP_TYPE state ──────────────────────────────────────────────────────────

@owner_only_callback
async def trip_oneway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose one-way."""
    query = update.callback_query
    await query.answer()
    context.user_data["trip_days"] = 0

    await query.edit_message_text(
        "One-way selected.\n\nHow do you want to specify <b>departure</b> dates?",
        parse_mode="HTML",
        reply_markup=DATE_MODE_KEYBOARD,
    )
    return DATE_MODE


@owner_only_callback
async def trip_roundtrip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose round-trip — ask trip duration."""
    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("7 days", callback_data="tripdays_7"),
            InlineKeyboardButton("10 days", callback_data="tripdays_10"),
        ],
        [
            InlineKeyboardButton("14 days", callback_data="tripdays_14"),
            InlineKeyboardButton("21 days", callback_data="tripdays_21"),
        ],
        [InlineKeyboardButton("Custom", callback_data="tripdays_custom")],
    ])
    await query.edit_message_text(
        "Round-trip selected.\n\nHow long is the trip?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return TRIP_DAYS


# ── TRIP_DAYS state ─────────────────────────────────────────────────────────

@owner_only_callback
async def tripdays_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked a preset trip duration."""
    query = update.callback_query
    await query.answer()
    days = int(query.data.split("_")[1])
    context.user_data["trip_days"] = days

    await query.edit_message_text(
        f"Round-trip, <b>{days} days</b>.\n\n"
        "How do you want to specify <b>departure</b> dates?",
        parse_mode="HTML",
        reply_markup=DATE_MODE_KEYBOARD,
    )
    return DATE_MODE


@owner_only_callback
async def tripdays_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User wants to type a custom trip duration."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Send the trip duration in days (e.g. <code>12</code>).",
        parse_mode="HTML",
    )
    return TRIP_DAYS


@owner_only
async def tripdays_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed a custom number of days."""
    try:
        days = parse_positive_int(update.message.text, field="days", maximum=MAX_TRIP_DAYS)
    except ValidationError as exc:
        await update.message.reply_text(str(exc), parse_mode="HTML")
        return TRIP_DAYS

    context.user_data["trip_days"] = days
    await update.message.reply_text(
        f"Round-trip, <b>{days} days</b>.\n\n"
        "How do you want to specify <b>departure</b> dates?",
        parse_mode="HTML",
        reply_markup=DATE_MODE_KEYBOARD,
    )
    return DATE_MODE


# ── DATE_MODE state ──────────────────────────────────────────────────────────

@owner_only_callback
async def datemode_fixed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose 'Fixed dates'."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Send the travel dates separated by commas.\n"
        "Format: <code>YYYY-MM-DD</code>\n"
        "Example: <code>2026-03-15, 2026-03-22, 2026-04-01</code>",
        parse_mode="HTML",
    )
    return FIXED_DATES


@owner_only_callback
async def datemode_range(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User chose 'Date range'."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Send the <b>start date</b> for the range.\nFormat: <code>YYYY-MM-DD</code>",
        parse_mode="HTML",
    )
    return RANGE_START


# ── FIXED_DATES state ───────────────────────────────────────────────────────

@owner_only
async def fixed_dates_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends comma-separated dates."""
    try:
        dates = parse_date_list(update.message.text)
    except ValidationError as exc:
        await update.message.reply_text(str(exc), parse_mode="HTML")
        return FIXED_DATES

    context.user_data["dates"] = dates
    return await _ask_hubs(update.message, context)


# ── RANGE_START / RANGE_END / RANGE_EVERY states ────────────────────────────

@owner_only
async def range_start_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends range start date."""
    try:
        start = parse_date(update.message.text, field="start date")
    except ValidationError as exc:
        await update.message.reply_text(str(exc), parse_mode="HTML")
        return RANGE_START

    context.user_data["range_start"] = start
    await update.message.reply_text(
        "Send the <b>end date</b> for the range.\nFormat: <code>YYYY-MM-DD</code>",
        parse_mode="HTML",
    )
    return RANGE_END


@owner_only
async def range_end_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends range end date."""
    try:
        end = parse_date(update.message.text, field="end date")
    except ValidationError as exc:
        await update.message.reply_text(str(exc), parse_mode="HTML")
        return RANGE_END

    start = context.user_data["range_start"]
    if end < start:
        await update.message.reply_text(
            f"The end date <b>{end}</b> is before the start date <b>{start}</b>.\n"
            "Send an end date on or after the start date.",
            parse_mode="HTML",
        )
        return RANGE_END

    context.user_data["range_end"] = end

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Every 3 days", callback_data="every_3"),
            InlineKeyboardButton("Every 5 days", callback_data="every_5"),
        ],
        [
            InlineKeyboardButton("Every 7 days", callback_data="every_7"),
            InlineKeyboardButton("Every 10 days", callback_data="every_10"),
        ],
    ])
    await update.message.reply_text(
        f"Range <b>{start}</b> to <b>{end}</b>.\n\n"
        "Sample a departure date every N days within the range:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return RANGE_EVERY


@owner_only_callback
async def range_every_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picks the 'every N days' interval."""
    query = update.callback_query
    await query.answer()
    every = int(query.data.split("_")[1])

    # Both endpoints were validated on the way in, so this cannot raise.
    dates = generate_dates(
        context.user_data["range_start"], context.user_data["range_end"], every
    )
    if not dates:
        await query.edit_message_text(
            "That range produced no dates. Start over with /start.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return ConversationHandler.END

    context.user_data["dates"] = dates
    shown = ", ".join(dates[:5])
    more = f"... and {len(dates) - 5} more" if len(dates) > 5 else ""
    await query.edit_message_text(
        f"Generated <b>{len(dates)}</b> dates: {shown}{more}",
        parse_mode="HTML",
    )
    return await _ask_hubs(query.message, context)


# ── Hub selection ────────────────────────────────────────────────────────────

def _hub_keyboard() -> InlineKeyboardMarkup:
    """Build the hub-selection keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("All hubs", callback_data="hubs_all")],
        [InlineKeyboardButton("Top 2 (MAD, BCN)", callback_data="hubs_top2")],
        [InlineKeyboardButton("Top 3 (MAD, BCN, LIS)", callback_data="hubs_top3")],
        [InlineKeyboardButton("Custom", callback_data="hubs_custom")],
    ])


async def _ask_hubs(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the hub-selection keyboard as a reply to *message*."""
    dates = context.user_data["dates"]
    await message.reply_text(
        f"Dates set ({len(dates)} total).\n\nWhich hub airports?",
        reply_markup=_hub_keyboard(),
    )
    return HUBS


@owner_only_callback
async def hubs_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked one of the preset hub sets."""
    query = update.callback_query
    await query.answer()

    presets = {
        "hubs_all": dict(DEFAULT_HUBS),
        "hubs_top2": {"MAD": "Madrid", "BCN": "Barcelona"},
        "hubs_top3": {"MAD": "Madrid", "BCN": "Barcelona", "LIS": "Lisboa"},
    }
    context.user_data["hubs"] = presets[query.data]
    return await _show_confirm(query.edit_message_text, context)


@owner_only_callback
async def hubs_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Custom hubs — ask user to type codes."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Send hub airport codes separated by commas.\n"
        f"Known hubs: <code>{', '.join(DEFAULT_HUBS)}</code>\n"
        "Or type any IATA codes.",
        parse_mode="HTML",
    )
    return CUSTOM_HUBS


@owner_only
async def custom_hubs_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User types custom hub codes."""
    try:
        codes = parse_iata_codes(update.message.text, field="hub code")
    except ValidationError as exc:
        await update.message.reply_text(str(exc), parse_mode="HTML")
        return CUSTOM_HUBS

    known = {**SPAIN_HUBS, **PORTUGAL_HUBS}
    context.user_data["hubs"] = {c: known.get(c, c) for c in codes}
    return await _show_confirm(update.message.reply_text, context)


# ── CONFIRM state ────────────────────────────────────────────────────────────

def _summary_text(user_data: dict) -> str:
    """Build the pre-flight summary shown before a search starts."""
    dests = user_data["destinations"]
    dates = user_data["dates"]
    hubs = user_data["hubs"]
    trip_days = user_data.get("trip_days", 0)

    # Phase 1 queries every hub/date; phase 2 every hub/dest/date. Round trips
    # double both. This is an upper bound — phase 2 skips unreachable hubs.
    n_queries = len(hubs) * len(dates) * (1 + len(dests))
    if trip_days:
        n_queries *= 2

    trip_label = f"Round-trip ({trip_days} days)" if trip_days else "One-way"
    return (
        "<b>Search summary</b>\n\n"
        f"Origin: <code>{esc(user_data['origin'])}</code>\n"
        f"Destinations: <code>{esc(', '.join(dests))}</code>\n"
        f"Trip: <b>{trip_label}</b>\n"
        f"Dates: <b>{len(dates)}</b> ({dates[0]} to {dates[-1]})\n"
        f"Hubs: <code>{esc(', '.join(hubs))}</code>\n"
        f"Up to <b>{n_queries}</b> queries\n\n"
        "Ready?"
    )


async def _show_confirm(send, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display the confirmation summary via *send* (an edit or reply callable)."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Start search", callback_data="search_go"),
            InlineKeyboardButton("Cancel", callback_data="search_cancel"),
        ],
    ])
    await send(_summary_text(context.user_data), parse_mode="HTML", reply_markup=keyboard)
    return CONFIRM


@owner_only_callback
async def confirm_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User confirmed — launch search in background."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("On it, I'll message you when done.")

    ud = context.user_data
    params = {
        "origin": ud["origin"],
        "destinations": dict(ud["destinations"]),
        "dates": list(ud["dates"]),
        "hubs": dict(ud["hubs"]),
        "adults": ud["adults"],
        "currency": ud["currency"],
        "trip_days": ud.get("trip_days", 0),
    }
    context.application.create_task(
        run_and_report(context.application.bot, update.effective_chat.id, params),
        update=update,
    )
    return ConversationHandler.END


@owner_only_callback
async def confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User cancelled the search."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Search cancelled.", reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


# ── Background search task ───────────────────────────────────────────────────

async def run_and_report(bot, chat_id: int, params: dict) -> None:
    """Run a search, send the results, persist them, and offer to track them.

    Shared by the guided flow and by history reruns so both paths store the same
    fields — notably ``trip_days``, without which a rerun would silently change
    the trip shape. ``params["dates"]`` is the discrete date list the guided
    flow (or a history rerun) collected; the engine wants a contiguous
    ``SearchWindow``, so it is converted here, once, rather than pushed onto
    every caller.
    """
    dates = params["dates"]
    window = SearchWindow(start=min(dates), end=max(dates))

    try:
        result = await run_search(
            origin=params["origin"],
            destinations=params["destinations"],
            hubs=params["hubs"],
            window=window,
            trip_days=params.get("trip_days", 0),
            adults=params["adults"],
            currency=params["currency"],
        )
    except Exception:
        logger.exception("Search failed for %s", params)
        await bot.send_message(
            chat_id=chat_id,
            text="Search failed — check the bot logs for details.",
        )
        return

    itineraries = result.itineraries
    origin = params["origin"]
    currency = params["currency"]

    for chunk in split_message(format_results(itineraries, origin, currency)):
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    results_data = json.loads(itineraries_to_json(itineraries)) if itineraries else None
    # scan_to_json(None) is the JSON literal "null" -> json.loads gives None,
    # so this is safe for both strategies without branching on result.scan here.
    scan_data = json.loads(scan_to_json(result.scan))
    best = itineraries[0] if itineraries else None
    search_id = await save_search(
        origin=origin,
        destinations=list(params["destinations"]),
        dates=dates,
        hubs=list(params["hubs"]),
        adults=params["adults"],
        currency=currency,
        trip_days=params.get("trip_days", 0),
        window_start=window.start,
        window_end=window.end,
        provider=best.providers[0] if best and best.providers else None,
        best_price=float(best.total) if best else None,
        best_route=f"{origin}->{best.hub}->{best.dest} {best.date}" if best else None,
        through_fare=best.through_fare if best else None,
        results=results_data,
        scan_json=scan_data,
    )

    if not best:
        return

    # The button carries only the search id; the handler reads price and trip
    # shape back from the stored row.
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Track this route", callback_data=f"savefav_{search_id}")],
    ])
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"Best route: <b>{best.total:,.2f} {currency}</b> "
            f"via {esc(best.hub)} to {esc(best.dest)} on {best.date}"
        ),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ── Fallback ─────────────────────────────────────────────────────────────────

@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel inside the conversation."""
    context.user_data.clear()
    await update.message.reply_text("Search cancelled.", reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


# ── Builder ──────────────────────────────────────────────────────────────────

def build_search_conversation() -> ConversationHandler:
    """Construct and return the search ConversationHandler."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(entry_search, pattern="^menu_search$")],
        states={
            DEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dest_input),
            ],
            TRIP_TYPE: [
                CallbackQueryHandler(trip_oneway, pattern="^trip_oneway$"),
                CallbackQueryHandler(trip_roundtrip, pattern="^trip_roundtrip$"),
            ],
            TRIP_DAYS: [
                CallbackQueryHandler(tripdays_preset, pattern=r"^tripdays_\d+$"),
                CallbackQueryHandler(tripdays_custom_prompt, pattern="^tripdays_custom$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, tripdays_custom_input),
            ],
            DATE_MODE: [
                CallbackQueryHandler(datemode_fixed, pattern="^datemode_fixed$"),
                CallbackQueryHandler(datemode_range, pattern="^datemode_range$"),
            ],
            FIXED_DATES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fixed_dates_input),
            ],
            RANGE_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, range_start_input),
            ],
            RANGE_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, range_end_input),
            ],
            RANGE_EVERY: [
                CallbackQueryHandler(range_every_input, pattern=r"^every_\d+$"),
            ],
            HUBS: [
                CallbackQueryHandler(hubs_preset, pattern=r"^hubs_(all|top2|top3)$"),
                CallbackQueryHandler(hubs_custom, pattern="^hubs_custom$"),
            ],
            CUSTOM_HUBS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_hubs_input),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_go, pattern="^search_go$"),
                CallbackQueryHandler(confirm_cancel, pattern="^search_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
