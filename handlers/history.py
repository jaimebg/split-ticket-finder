"""Search history handlers — view past searches and rerun them."""
from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import DEFAULT_HUBS, ORIGIN
from db import get_search_by_id, get_searches, save_search
from handlers.start import MAIN_MENU_KEYBOARD, owner_only_callback
from scraper import Route
from search import format_results, routes_to_json, run_search

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

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
            if len(block) > limit:
                chunks.append(block)
                current = ""
            else:
                current = block

    if current:
        chunks.append(current)
    return chunks


def _route_from_dict(d: dict) -> Route:
    """Reconstruct a Route dataclass instance from a stored dict."""
    return Route(
        date=d["date"],
        hub=d["hub"],
        hub_name=d.get("hub_name", d["hub"]),
        dest=d["dest"],
        dest_name=d.get("dest_name", d["dest"]),
        dom_price=d["dom_price"],
        dom_discounted=d["dom_discounted"],
        intl_price=d["intl_price"],
        total=d["total"],
        dom_airlines=d.get("dom_airlines", []),
        dom_stops=d.get("dom_stops", 0),
        dom_dur=d.get("dom_dur", 0),
        intl_airlines=d.get("intl_airlines", []),
        intl_stops=d.get("intl_stops", 0),
        intl_dur=d.get("intl_dur", 0),
    )


# ── Handlers ────────────────────────────────────────────────────────────────

@owner_only_callback
async def history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 10 searches with View/Rerun buttons."""
    query = update.callback_query
    await query.answer()

    searches = await get_searches(10)

    if not searches:
        await query.edit_message_text(
            "No search history yet.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    buttons: list[list[InlineKeyboardButton]] = []
    for s in searches:
        # Parse destinations from JSON
        try:
            dests = json.loads(s["destinations"])
        except (json.JSONDecodeError, TypeError):
            dests = []

        dest_str = ",".join(dests) if isinstance(dests, list) else str(dests)
        date_part = s["created_at"][:10] if s.get("created_at") else "?"
        price_str = f"{s['best_price']:,.0f}" if s.get("best_price") else "N/A"

        label = f"{date_part} | {s['origin']}->{dest_str} | {price_str}"

        buttons.append([
            InlineKeyboardButton(f"View: {label}", callback_data=f"hist_view_{s['id']}"),
            InlineKeyboardButton("Rerun", callback_data=f"hist_rerun_{s['id']}"),
        ])

    buttons.append([InlineKeyboardButton("Back", callback_data="menu_main")])

    await query.edit_message_text(
        "<b>Search history</b> (last 10):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@owner_only_callback
async def history_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View stored results for a past search."""
    query = update.callback_query
    await query.answer()

    search_id = int(query.data.split("_")[-1])
    row = await get_search_by_id(search_id)

    if not row:
        await query.edit_message_text(
            "Search not found.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # Parse stored results
    results_raw = row.get("results")
    if not results_raw:
        await query.edit_message_text(
            "No results stored for this search.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    try:
        result_dicts = json.loads(results_raw) if isinstance(results_raw, str) else results_raw
    except (json.JSONDecodeError, TypeError):
        await query.edit_message_text(
            "Could not parse stored results.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # Reconstruct Route objects
    routes = [_route_from_dict(d) for d in result_dicts]

    origin = row.get("origin", ORIGIN)
    currency = row.get("currency", "EUR")

    text = format_results(routes, origin, currency)

    # Split and send
    chunks = _split_message(text)

    # Edit the original message with the first chunk
    await query.edit_message_text(
        chunks[0],
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # Send remaining chunks as new messages
    for chunk in chunks[1:]:
        await query.message.reply_text(
            chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


@owner_only_callback
async def history_rerun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rerun a past search with the same parameters."""
    query = update.callback_query
    await query.answer()

    search_id = int(query.data.split("_")[-1])
    row = await get_search_by_id(search_id)

    if not row:
        await query.edit_message_text(
            "Search not found.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # Parse parameters from the stored search
    origin = row.get("origin", ORIGIN)
    currency = row.get("currency", "EUR")
    adults = row.get("adults", 1)

    try:
        dest_codes = json.loads(row["destinations"]) if isinstance(row["destinations"], str) else row["destinations"]
    except (json.JSONDecodeError, TypeError):
        dest_codes = []

    try:
        dates = json.loads(row["dates"]) if isinstance(row["dates"], str) else row["dates"]
    except (json.JSONDecodeError, TypeError):
        dates = []

    try:
        hub_codes = json.loads(row["hubs"]) if isinstance(row["hubs"], str) else row["hubs"]
    except (json.JSONDecodeError, TypeError):
        hub_codes = []

    # Reconstruct dicts: destinations as {code: code}, hubs as {code: name}
    all_known_hubs = dict(DEFAULT_HUBS)
    destinations = {c: c for c in dest_codes}
    hubs = {c: all_known_hubs.get(c, c) for c in hub_codes}

    await query.edit_message_text("On it, I'll message you when done.")

    chat_id = update.effective_chat.id
    bot = context.application.bot

    async def _rerun_task() -> None:
        try:
            routes = await run_search(
                origin=origin,
                destinations=destinations,
                dates=dates,
                hubs=hubs,
                adults=adults,
                currency=currency,
            )

            text = format_results(routes, origin, currency)
            chunks = _split_message(text)
            for chunk in chunks:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            # Save to DB
            results_data = json.loads(routes_to_json(routes)) if routes else None
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
                results=results_data,
            )

        except Exception:
            logger.exception("History rerun task failed")
            await bot.send_message(
                chat_id=chat_id,
                text="Rerun failed. Check bot logs for details.",
            )

    context.application.create_task(_rerun_task(), update=update)


# ── Handler list builder ────────────────────────────────────────────────────

def get_history_handlers() -> list[CallbackQueryHandler]:
    """Return the list of CallbackQueryHandlers for history features."""
    return [
        CallbackQueryHandler(history_menu, pattern=r"^menu_history$"),
        CallbackQueryHandler(history_view, pattern=r"^hist_view_\d+$"),
        CallbackQueryHandler(history_rerun, pattern=r"^hist_rerun_\d+$"),
    ]
