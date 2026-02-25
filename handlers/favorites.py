"""Favorites management handlers — save, list, and delete favorite routes."""

import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import ORIGIN
from db import add_favorite, delete_favorite, get_favorites
from handlers.start import MAIN_MENU_KEYBOARD, owner_only_callback

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    """Try to extract the price from the 'Best route' message text.

    Expected format: "Best route: 123 EUR via MAD to NRT on 2026-03-15"
    or with thousand separators: "Best route: 1,234 EUR ..."
    """
    match = re.search(r"Best route:\s*\*?\*?([0-9,.]+)", text)
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


# ── Handlers ────────────────────────────────────────────────────────────────

@owner_only_callback
async def favorites_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all favorites with delete buttons."""
    query = update.callback_query
    await query.answer()

    favs = await get_favorites()

    if not favs:
        await query.edit_message_text(
            "No favorites saved yet.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    lines: list[str] = ["<b>Your favorites</b>\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    for fav in favs:
        origin = fav.get("origin", ORIGIN)
        hub = fav["hub"]
        dest = fav["destination"]

        record_price = fav.get("record_price")
        last_price = fav.get("last_price")
        last_checked = fav.get("last_checked")

        record_str = f"{record_price:,.0f}" if record_price is not None else "N/A"
        last_str = f"{last_price:,.0f}" if last_price is not None else "N/A"
        checked_str = last_checked[:10] if last_checked else "never"

        # Parse check_dates for display
        try:
            dates = json.loads(fav["check_dates"]) if isinstance(fav["check_dates"], str) else fav["check_dates"]
        except (json.JSONDecodeError, TypeError):
            dates = []
        dates_str = ", ".join(dates[:3])
        if len(dates) > 3:
            dates_str += f" (+{len(dates) - 3})"

        lines.append(
            f"<b>{origin} -> {hub} -> {dest}</b>\n"
            f"  Record: {record_str} | Last: {last_str}\n"
            f"  Checked: {checked_str} | Dates: {dates_str}"
        )

        buttons.append([
            InlineKeyboardButton(
                f"Delete {hub}->{dest}",
                callback_data=f"delfav_{fav['id']}",
            )
        ])

    buttons.append([InlineKeyboardButton("Back", callback_data="menu_main")])

    await query.edit_message_text(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@owner_only_callback
async def save_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a route as favorite from the 'Save best as favorite' button.

    Callback data format: savefav_{hub}_{dest}_{date}
    """
    query = update.callback_query
    await query.answer()

    # Parse callback data
    parts = query.data.split("_")
    # savefav_MAD_NRT_2026-03-15 -> ["savefav", "MAD", "NRT", "2026-03-15"]
    if len(parts) < 4:
        await query.edit_message_text("Invalid favorite data.")
        return

    hub = parts[1]
    dest = parts[2]
    date = parts[3]

    # Try to extract price from the message text
    price = _parse_price(query.message.text or "")

    await add_favorite(
        origin=ORIGIN,
        hub=hub,
        destination=dest,
        adults=1,
        currency="EUR",
        price=price,
        check_dates=[date],
    )

    price_str = f" ({price:,.0f} EUR)" if price is not None else ""
    await query.edit_message_text(
        f"Saved favorite: {ORIGIN} -> {hub} -> {dest} on {date}{price_str}",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


@owner_only_callback
async def delete_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a favorite and refresh the list."""
    query = update.callback_query
    await query.answer()

    # Parse fav_id from callback data: delfav_{id}
    fav_id = int(query.data.split("_")[1])
    deleted = await delete_favorite(fav_id)

    if not deleted:
        await query.edit_message_text(
            "Favorite not found (may have been already deleted).",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # Refresh the favorites list
    favs = await get_favorites()

    if not favs:
        await query.edit_message_text(
            "Favorite deleted. No favorites remaining.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # Rebuild the list (same logic as favorites_menu)
    lines: list[str] = ["<b>Your favorites</b> (deleted one)\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    for fav in favs:
        origin = fav.get("origin", ORIGIN)
        hub_code = fav["hub"]
        dest_code = fav["destination"]

        record_price = fav.get("record_price")
        last_price = fav.get("last_price")
        last_checked = fav.get("last_checked")

        record_str = f"{record_price:,.0f}" if record_price is not None else "N/A"
        last_str = f"{last_price:,.0f}" if last_price is not None else "N/A"
        checked_str = last_checked[:10] if last_checked else "never"

        try:
            dates = json.loads(fav["check_dates"]) if isinstance(fav["check_dates"], str) else fav["check_dates"]
        except (json.JSONDecodeError, TypeError):
            dates = []
        dates_str = ", ".join(dates[:3])
        if len(dates) > 3:
            dates_str += f" (+{len(dates) - 3})"

        lines.append(
            f"<b>{origin} -> {hub_code} -> {dest_code}</b>\n"
            f"  Record: {record_str} | Last: {last_str}\n"
            f"  Checked: {checked_str} | Dates: {dates_str}"
        )

        buttons.append([
            InlineKeyboardButton(
                f"Delete {hub_code}->{dest_code}",
                callback_data=f"delfav_{fav['id']}",
            )
        ])

    buttons.append([InlineKeyboardButton("Back", callback_data="menu_main")])

    await query.edit_message_text(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Handler list builder ────────────────────────────────────────────────────

def get_favorites_handlers() -> list[CallbackQueryHandler]:
    """Return the list of CallbackQueryHandlers for favorites features."""
    return [
        CallbackQueryHandler(favorites_menu, pattern=r"^menu_favorites$"),
        CallbackQueryHandler(save_favorite, pattern=r"^savefav_"),
        CallbackQueryHandler(delete_fav, pattern=r"^delfav_\d+$"),
    ]
