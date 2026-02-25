"""Guided search conversation handler — walks the user through building a search."""

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
from handlers.start import MAIN_MENU_KEYBOARD, owner_only, owner_only_callback
from scraper import generate_dates
from search import format_results, routes_to_json, run_search

logger = logging.getLogger(__name__)

# ── Conversation states ──────────────────────────────────────────────────────
DEST, DATE_MODE, FIXED_DATES, RANGE_START, RANGE_END, RANGE_EVERY, HUBS, CUSTOM_HUBS, CONFIRM = range(9)


# ── Entry point ──────────────────────────────────────────────────────────────

@owner_only_callback
async def entry_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: user tapped 'Search flights' in main menu."""
    query = update.callback_query
    await query.answer()

    # Initialize user_data defaults
    context.user_data["origin"] = ORIGIN
    context.user_data["adults"] = 1
    context.user_data["currency"] = "EUR"
    context.user_data["destinations"] = {}
    context.user_data["dates"] = []
    context.user_data["hubs"] = {}

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
    raw = update.message.text.strip().upper()
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    if not codes:
        await update.message.reply_text("Please send at least one airport code.")
        return DEST

    # Store as {code: code} — we don't have names, use code as placeholder
    context.user_data["destinations"] = {c: c for c in codes}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Fixed dates", callback_data="datemode_fixed"),
            InlineKeyboardButton("Date range", callback_data="datemode_range"),
        ],
    ])
    await update.message.reply_text(
        f"Destinations: <b>{', '.join(codes)}</b>\n\n"
        "How do you want to specify dates?",
        parse_mode="HTML",
        reply_markup=keyboard,
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
        "Send the <b>start date</b> for the range.\n"
        "Format: <code>YYYY-MM-DD</code>",
        parse_mode="HTML",
    )
    return RANGE_START


# ── FIXED_DATES state ───────────────────────────────────────────────────────

@owner_only
async def fixed_dates_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends comma-separated dates."""
    raw = update.message.text.strip()
    dates = [d.strip() for d in raw.split(",") if d.strip()]
    if not dates:
        await update.message.reply_text("Please send at least one date.")
        return FIXED_DATES

    context.user_data["dates"] = dates
    return await _ask_hubs(update, context)


# ── RANGE_START / RANGE_END / RANGE_EVERY states ────────────────────────────

@owner_only
async def range_start_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends range start date."""
    context.user_data["range_start"] = update.message.text.strip()
    await update.message.reply_text(
        "Send the <b>end date</b> for the range.\n"
        "Format: <code>YYYY-MM-DD</code>",
        parse_mode="HTML",
    )
    return RANGE_END


@owner_only
async def range_end_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends range end date."""
    context.user_data["range_end"] = update.message.text.strip()

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
        "How often?",
        reply_markup=keyboard,
    )
    return RANGE_EVERY


@owner_only_callback
async def range_every_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picks the 'every N days' interval."""
    query = update.callback_query
    await query.answer()
    every = int(query.data.split("_")[1])

    start = context.user_data["range_start"]
    end = context.user_data["range_end"]
    dates = generate_dates(start, end, every)

    if not dates:
        await query.edit_message_text("No dates generated. Please try again with /start.")
        return ConversationHandler.END

    context.user_data["dates"] = dates
    # We need to send a new message since _ask_hubs expects a message context
    await query.edit_message_text(
        f"Generated <b>{len(dates)}</b> dates: {', '.join(dates[:5])}"
        + (f"... and {len(dates) - 5} more" if len(dates) > 5 else ""),
        parse_mode="HTML",
    )
    return await _ask_hubs_from_query(update, context)


# ── Hub selection helpers ────────────────────────────────────────────────────

def _hub_keyboard() -> InlineKeyboardMarkup:
    """Build the hub-selection keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("All hubs", callback_data="hubs_all")],
        [InlineKeyboardButton("Top 2 (MAD, BCN)", callback_data="hubs_top2")],
        [InlineKeyboardButton("Top 3 (MAD, BCN, LIS)", callback_data="hubs_top3")],
        [InlineKeyboardButton("Custom", callback_data="hubs_custom")],
    ])


async def _ask_hubs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the hub-selection keyboard (from a message handler context)."""
    dates = context.user_data["dates"]
    await update.message.reply_text(
        f"Dates set ({len(dates)} total).\n\n"
        "Which hub airports?",
        reply_markup=_hub_keyboard(),
    )
    return HUBS


async def _ask_hubs_from_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the hub-selection keyboard (from a callback query context)."""
    query = update.callback_query
    dates = context.user_data["dates"]
    await query.message.reply_text(
        f"Dates set ({len(dates)} total).\n\n"
        "Which hub airports?",
        reply_markup=_hub_keyboard(),
    )
    return HUBS


# ── HUBS state ───────────────────────────────────────────────────────────────

@owner_only_callback
async def hubs_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """All hubs."""
    query = update.callback_query
    await query.answer()
    context.user_data["hubs"] = dict(DEFAULT_HUBS)
    return await _show_confirm(query, context)


@owner_only_callback
async def hubs_top2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Top 2: MAD, BCN."""
    query = update.callback_query
    await query.answer()
    context.user_data["hubs"] = {"MAD": "Madrid", "BCN": "Barcelona"}
    return await _show_confirm(query, context)


@owner_only_callback
async def hubs_top3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Top 3: MAD, BCN, LIS."""
    query = update.callback_query
    await query.answer()
    context.user_data["hubs"] = {"MAD": "Madrid", "BCN": "Barcelona", "LIS": "Lisboa"}
    return await _show_confirm(query, context)


@owner_only_callback
async def hubs_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Custom hubs — ask user to type codes."""
    query = update.callback_query
    await query.answer()
    all_codes = ", ".join(DEFAULT_HUBS.keys())
    await query.edit_message_text(
        "Send hub airport codes separated by commas.\n"
        f"Available: <code>{all_codes}</code>\n"
        "Or type any IATA codes.",
        parse_mode="HTML",
    )
    return CUSTOM_HUBS


# ── CUSTOM_HUBS state ───────────────────────────────────────────────────────

@owner_only
async def custom_hubs_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User types custom hub codes."""
    raw = update.message.text.strip().upper()
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    if not codes:
        await update.message.reply_text("Please send at least one hub code.")
        return CUSTOM_HUBS

    # Look up names from known hubs, fall back to code itself
    all_known = {**SPAIN_HUBS, **PORTUGAL_HUBS}
    hubs = {c: all_known.get(c, c) for c in codes}
    context.user_data["hubs"] = hubs

    return await _show_confirm_from_message(update, context)


# ── CONFIRM state helpers ────────────────────────────────────────────────────

async def _show_confirm(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display confirmation summary (from a callback query)."""
    ud = context.user_data
    origin = ud["origin"]
    dests = ud["destinations"]
    dates = ud["dates"]
    hubs = ud["hubs"]
    n_queries = len(hubs) * len(dates) + len(hubs) * len(dests) * len(dates)

    summary = (
        "<b>Search summary</b>\n\n"
        f"Origin: <code>{origin}</code>\n"
        f"Destinations: <code>{', '.join(dests.keys())}</code>\n"
        f"Dates: <b>{len(dates)}</b> ({dates[0]} to {dates[-1]})\n"
        f"Hubs: <code>{', '.join(hubs.keys())}</code>\n"
        f"~<b>{n_queries}</b> queries\n\n"
        "Ready?"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Start search", callback_data="search_go"),
            InlineKeyboardButton("Cancel", callback_data="search_cancel"),
        ],
    ])
    await query.edit_message_text(summary, parse_mode="HTML", reply_markup=keyboard)
    return CONFIRM


async def _show_confirm_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display confirmation summary (from a message handler)."""
    ud = context.user_data
    origin = ud["origin"]
    dests = ud["destinations"]
    dates = ud["dates"]
    hubs = ud["hubs"]
    n_queries = len(hubs) * len(dates) + len(hubs) * len(dests) * len(dates)

    summary = (
        "<b>Search summary</b>\n\n"
        f"Origin: <code>{origin}</code>\n"
        f"Destinations: <code>{', '.join(dests.keys())}</code>\n"
        f"Dates: <b>{len(dates)}</b> ({dates[0]} to {dates[-1]})\n"
        f"Hubs: <code>{', '.join(hubs.keys())}</code>\n"
        f"~<b>{n_queries}</b> queries\n\n"
        "Ready?"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Start search", callback_data="search_go"),
            InlineKeyboardButton("Cancel", callback_data="search_cancel"),
        ],
    ])
    await update.message.reply_text(summary, parse_mode="HTML", reply_markup=keyboard)
    return CONFIRM


# ── CONFIRM state handlers ───────────────────────────────────────────────────

@owner_only_callback
async def confirm_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User confirmed — launch search in background."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("On it, I'll message you when done.")

    chat_id = update.effective_chat.id
    user_data = dict(context.user_data)  # snapshot
    context.application.create_task(
        _run_search_task(context.application.bot, chat_id, user_data),
        update=update,
    )
    return ConversationHandler.END


@owner_only_callback
async def confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User cancelled the search."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Search cancelled.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    return ConversationHandler.END


# ── Background search task ───────────────────────────────────────────────────

def _split_message(text: str, limit: int = 4000) -> list[str]:
    """Split text on double-newline boundaries into chunks up to *limit* chars."""
    blocks = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single block exceeds the limit, send it as-is
            if len(block) > limit:
                chunks.append(block)
                current = ""
            else:
                current = block

    if current:
        chunks.append(current)
    return chunks


async def _run_search_task(bot, chat_id: int, user_data: dict) -> None:
    """Execute the search pipeline in the background and send results."""
    try:
        origin = user_data["origin"]
        destinations = user_data["destinations"]
        dates = user_data["dates"]
        hubs = user_data["hubs"]
        adults = user_data["adults"]
        currency = user_data["currency"]

        # 1. Run the search
        routes = await run_search(
            origin=origin,
            destinations=destinations,
            dates=dates,
            hubs=hubs,
            adults=adults,
            currency=currency,
        )

        # 2. Format results
        text = format_results(routes, origin, currency)

        # 3. Split and send
        chunks = _split_message(text)
        for chunk in chunks:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        # 4. Save to DB
        results_json = routes_to_json(routes)
        best_price = routes[0].total if routes else None
        best_route = (
            f"{origin}->{routes[0].hub}->{routes[0].dest} {routes[0].date}"
            if routes else None
        )
        await save_search(
            origin=origin,
            destinations=list(destinations.keys()),
            dates=dates,
            hubs=list(hubs.keys()),
            adults=adults,
            currency=currency,
            best_price=best_price,
            best_route=best_route,
            results=results_json,
        )

        # 5. Offer to save best as favorite
        if routes:
            best = routes[0]
            callback_data = f"savefav_{best.hub}_{best.dest}_{best.date}"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Save best as favorite",
                    callback_data=callback_data,
                )],
            ])
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Best route: <b>{best.total:,.0f} {currency}</b> "
                    f"via {best.hub} to {best.dest} on {best.date}"
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

    except Exception:
        logger.exception("Background search task failed")
        await bot.send_message(
            chat_id=chat_id,
            text="Search failed. Check bot logs for details.",
        )


# ── Fallback ─────────────────────────────────────────────────────────────────

@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel inside the conversation."""
    await update.message.reply_text(
        "Search cancelled.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    return ConversationHandler.END


# ── Builder ──────────────────────────────────────────────────────────────────

def build_search_conversation() -> ConversationHandler:
    """Construct and return the search ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_search, pattern="^menu_search$"),
        ],
        states={
            DEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dest_input),
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
                CallbackQueryHandler(hubs_all, pattern="^hubs_all$"),
                CallbackQueryHandler(hubs_top2, pattern="^hubs_top2$"),
                CallbackQueryHandler(hubs_top3, pattern="^hubs_top3$"),
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
        fallbacks=[
            CommandHandler("cancel", cancel_command),
        ],
    )
