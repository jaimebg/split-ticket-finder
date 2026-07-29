"""Favorites management handlers — save, list, and delete favorite routes."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import ORIGIN
from db import add_favorite, delete_favorite, get_favorites, get_search_by_id
from handlers.start import MAIN_MENU_KEYBOARD, owner_only_callback
from handlers.utils import esc, format_favorite, load_json_list

logger = logging.getLogger(__name__)


# ── Rendering ───────────────────────────────────────────────────────────────

async def _render_favorites(query, heading: str) -> None:
    """Show the favourites list, or the main menu when there are none."""
    favs = await get_favorites()

    if not favs:
        await query.edit_message_text(
            "No favorites saved yet.\n\n"
            "Run a search and tap <b>Track this route</b> to have the bot "
            "re-check its price and alert you when it drops.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    lines = [heading]
    buttons: list[list[InlineKeyboardButton]] = []

    for fav in favs:
        lines.append(format_favorite(fav, ORIGIN))
        buttons.append([
            InlineKeyboardButton(
                f"Delete {fav['hub']}->{fav['destination']}",
                callback_data=f"delfav_{fav['id']}",
            )
        ])

    buttons.append([InlineKeyboardButton("Back", callback_data="menu_main")])

    await query.edit_message_text(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Handlers ────────────────────────────────────────────────────────────────

@owner_only_callback
async def favorites_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all favorites with delete buttons."""
    query = update.callback_query
    await query.answer()
    await _render_favorites(query, "<b>Your favorites</b>\n")


@owner_only_callback
async def save_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track the best route of a past search.

    Callback data is ``savefav_{search_id}``. Everything else is read back from
    that stored search, so the favourite records the same trip shape and price
    the user actually saw — rather than re-deriving them from message text.
    """
    query = update.callback_query
    await query.answer()

    try:
        search_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid favorite data.", reply_markup=MAIN_MENU_KEYBOARD)
        return

    row = await get_search_by_id(search_id)
    if not row:
        await query.edit_message_text(
            "That search is no longer stored, so it can't be tracked.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    results = load_json_list(row.get("results"))
    if not results:
        await query.edit_message_text(
            "That search has no stored results to track.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    best = results[0]
    trip_days = row.get("trip_days") or 0
    # Track every date the search covered, so the scheduler can sample across
    # them instead of re-checking a single day forever.
    check_dates = [str(d) for d in load_json_list(row.get("dates"))] or [best["date"]]

    await add_favorite(
        origin=row.get("origin") or ORIGIN,
        hub=best["hub"],
        destination=best["dest"],
        adults=row.get("adults") or 1,
        currency=row.get("currency") or "EUR",
        price=best.get("total"),
        check_dates=check_dates,
        trip_days=trip_days,
    )

    trip_str = f"round-trip, {trip_days} days" if trip_days else "one-way"
    price_str = f" at {best['total']:,.0f} {row.get('currency') or 'EUR'}" if best.get("total") else ""
    await query.edit_message_text(
        f"Now tracking <b>{esc(row.get('origin') or ORIGIN)} -> {esc(best['hub'])} "
        f"-> {esc(best['dest'])}</b> ({trip_str}){price_str}.\n\n"
        f"Checking {len(check_dates)} date(s); you'll get an alert when the price drops.",
        parse_mode="HTML",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


@owner_only_callback
async def delete_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a favorite and refresh the list."""
    query = update.callback_query
    await query.answer()

    fav_id = int(query.data.split("_")[1])
    if not await delete_favorite(fav_id):
        await query.edit_message_text(
            "Favorite not found (may have been already deleted).",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    await _render_favorites(query, "<b>Your favorites</b> — deleted one.\n")


# ── Handler list builder ────────────────────────────────────────────────────

def get_favorites_handlers() -> list[CallbackQueryHandler]:
    """Return the list of CallbackQueryHandlers for favorites features."""
    return [
        CallbackQueryHandler(favorites_menu, pattern=r"^menu_favorites$"),
        CallbackQueryHandler(save_favorite, pattern=r"^savefav_\d+$"),
        CallbackQueryHandler(delete_fav, pattern=r"^delfav_\d+$"),
    ]
