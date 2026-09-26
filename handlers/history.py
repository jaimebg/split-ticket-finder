"""Search history handlers — view past searches and rerun them."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

import handlers.results as results_module
from config import DEFAULT_HUBS, ORIGIN
from db import get_search_by_id, get_searches
from handlers.anchor import markup
from handlers.start import MAIN_MENU_KEYBOARD, owner_only_callback
from handlers.utils import esc, load_json_list
from results import view
from results.filters import Filters
from results.store import load

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

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
        dests = load_json_list(s.get("destinations"))
        dest_str = ",".join(str(d) for d in dests) or "?"
        date_part = s["created_at"][:10] if s.get("created_at") else "?"
        price_str = f"{s['best_price']:,.0f}" if s.get("best_price") else "N/A"
        trip_str = "RT" if (s.get("trip_days") or 0) else "OW"

        label = f"{date_part} | {s['origin']}->{dest_str} | {trip_str} | {price_str}"
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
        await query.edit_message_text("Search not found.", reply_markup=MAIN_MENU_KEYBOARD)
        return

    stored = load(row.get("results"))
    if not stored.itineraries:
        await query.edit_message_text(
            "No results stored for this search.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    state = results_module._view_state(row)
    text, rows = view.summary(view.SearchMeta.from_row(row), stored,
                              Filters.from_dict(state.get("filters")), 1)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup(rows),
                                  disable_web_page_preview=True)


@owner_only_callback
async def history_rerun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rerun a past search with the same parameters."""
    from handlers.results import RUNS_KEY, run_and_report

    query = update.callback_query
    await query.answer()

    search_id = int(query.data.split("_")[-1])
    row = await get_search_by_id(search_id)

    if not row:
        await query.edit_message_text("Search not found.", reply_markup=MAIN_MENU_KEYBOARD)
        return

    dest_codes = [str(c) for c in load_json_list(row.get("destinations"))]
    dates = [str(d) for d in load_json_list(row.get("dates"))]
    hub_codes = [str(c) for c in load_json_list(row.get("hubs"))]

    if not dest_codes or not dates or not hub_codes:
        await query.edit_message_text(
            "That search is missing parameters and can't be rerun.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    # trip_days has to be replayed too, or a round-trip search silently reruns
    # as one-way and the two results aren't comparable.
    params = {
        "origin": row.get("origin") or ORIGIN,
        "destinations": {c: c for c in dest_codes},
        "dates": dates,
        "hubs": {c: DEFAULT_HUBS.get(c, c) for c in hub_codes},
        "adults": row.get("adults") or 1,
        "currency": row.get("currency") or "EUR",
        "trip_days": row.get("trip_days") or 0,
    }

    trip_str = f"round-trip {params['trip_days']}d" if params["trip_days"] else "one-way"
    await query.edit_message_text(
        f"Rerunning <b>{esc(params['origin'])} -> {esc(','.join(dest_codes))}</b> "
        f"({trip_str}, {len(dates)} dates). I'll message you when done.",
        parse_mode="HTML",
    )

    context.application.create_task(
        run_and_report(context.application.bot, update.effective_chat.id, params,
                       runs=context.application.bot_data.setdefault(RUNS_KEY, {})),
        update=update,
    )


# ── Handler list builder ────────────────────────────────────────────────────

def get_history_handlers() -> list[CallbackQueryHandler]:
    """Return the list of CallbackQueryHandlers for history features."""
    return [
        CallbackQueryHandler(history_menu, pattern=r"^menu_history$"),
        CallbackQueryHandler(history_view, pattern=r"^hist_view_\d+$"),
        CallbackQueryHandler(history_rerun, pattern=r"^hist_rerun_\d+$"),
    ]
