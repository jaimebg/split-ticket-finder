"""Search history handlers — view past searches and rerun them."""
from __future__ import annotations

import logging
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import DEFAULT_HUBS, ORIGIN
from db import get_search_by_id, get_searches
from handlers.start import MAIN_MENU_KEYBOARD, owner_only_callback
from handlers.utils import esc, load_json_list, split_message
from models import Itinerary

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _itinerary_from_dict(d: dict) -> Itinerary:
    """Reconstruct an ``Itinerary`` from a stored result dict.

    Handles both the shape ``search.itineraries_to_json`` now writes
    (``discount``/``onward_price``/``through_fare``) and the pre-engine
    ``Route`` shape it replaced (``dom_price``/``dom_discounted``/
    ``intl_price``, no ``discount`` field at all) — the two rows in the live
    ``flight_finder.db`` are this older shape, and must still load.

    Neither shape carries a real ``Offer``, so a row reloaded from storage
    always comes back unconfirmed (``est_dom_price``/``est_onward_price``
    only): the numbers are a historical snapshot, not a fresh, bookable
    quote, and ``format_results`` labels it an estimate accordingly.

    ``return_date`` matters beyond display: ``format_results`` uses it to
    decide whether the stored prices are round-trip totals, so dropping it
    would render a round-trip search as one-way with round-trip prices.
    """
    dom_price = Decimal(str(d["dom_price"]))
    onward_price = Decimal(str(d.get("onward_price", d.get("intl_price", 0))))

    if "discount" in d:
        discount = Decimal(str(d["discount"]))
    elif dom_price:
        # Old Route rows never stored the discount fraction directly, only
        # both sides of it (dom_price, dom_discounted) — recover it from
        # those so the stored total still reproduces exactly.
        dom_discounted = Decimal(str(d.get("dom_discounted", dom_price)))
        discount = Decimal(1) - dom_discounted / dom_price
    else:
        discount = Decimal(0)

    through_fare_raw = d.get("through_fare")
    through_fare = Decimal(str(through_fare_raw)) if through_fare_raw is not None else None

    return Itinerary(
        date=d["date"],
        return_date=d.get("return_date", ""),
        hub=d["hub"],
        hub_name=d.get("hub_name", d["hub"]),
        dest=d["dest"],
        dest_name=d.get("dest_name", d["dest"]),
        discount=discount,
        est_dom_price=dom_price,
        est_onward_price=onward_price,
        through_fare=through_fare,
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
    from search import format_results

    query = update.callback_query
    await query.answer()

    search_id = int(query.data.split("_")[-1])
    row = await get_search_by_id(search_id)

    if not row:
        await query.edit_message_text("Search not found.", reply_markup=MAIN_MENU_KEYBOARD)
        return

    result_dicts = load_json_list(row.get("results"))
    if not result_dicts:
        await query.edit_message_text(
            "No results stored for this search.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    itineraries = [_itinerary_from_dict(d) for d in result_dicts]
    row_through_fare = row.get("through_fare")
    through_fare = Decimal(str(row_through_fare)) if row_through_fare is not None else None
    text = format_results(
        itineraries, row.get("origin") or ORIGIN, row.get("currency") or "EUR",
        through_fare=through_fare,
    )

    chunks = split_message(text)
    await query.edit_message_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await query.message.reply_text(
            chunk, parse_mode="HTML", disable_web_page_preview=True
        )


@owner_only_callback
async def history_rerun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rerun a past search with the same parameters."""
    from handlers.search_flow import run_and_report

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
        run_and_report(context.application.bot, update.effective_chat.id, params),
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
