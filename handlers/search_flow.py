"""Running a search and reporting it.

The guided conversation moved to ``handlers/search/`` in Layer 3a. What is
left is the engine-facing half: ``run_and_report``, shared by the builder
and by history reruns, and the two pure helpers the pre-flight summary
needs.

These stay here rather than moving with the conversation because
``tests/test_search_flow.py`` patches ``run_search`` and
``primary_provider`` as attributes of *this module*; a moved function
resolves those names in its new namespace and the patches would silently
stop taking effect. Layer 3b rewrites results and has to touch those tests
anyway -- the move belongs there.
"""
from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import FALLBACK_MAX_DATES, MAX_WINDOW_DAYS, SHORTLIST_SIZE, THROUGH_FARE_DATES
from db import save_search
from engine import run_search
from handlers.utils import esc, split_message
from models import SearchWindow
from providers.base import SupportsCalendar
from providers.registry import primary_provider
from search import format_results, itineraries_to_json, scan_to_json

logger = logging.getLogger(__name__)


def _oversized_window_message(start: str, end: str) -> str | None:
    """A user-facing message if *start*..*end* exceeds MAX_WINDOW_DAYS, else None.

    Checked in the date-collection steps themselves (review finding I6), so
    the conversation can re-prompt with a message naming the limit instead of
    reaching a full "Ready?" summary only to have ``run_search`` reject the
    window with a bare ``ValueError`` once the search actually starts. That
    ``ValueError`` is still caught distinctly in ``run_and_report`` below, as
    a second line of defence for whatever this early check doesn't cover
    (e.g. a history rerun of a search saved before this validation existed).
    """
    span = SearchWindow(start=start, end=end).days
    if span > MAX_WINDOW_DAYS:
        return (
            f"That's a <b>{span}-day</b> span ({start} to {end}), more than the "
            f"<b>{MAX_WINDOW_DAYS}-day</b> limit the search engine can cover in "
            "one request. Send a narrower range."
        )
    return None


def _estimate_queries(*, hubs: int, dests: int, dates: int, round_trip: bool) -> int:
    """Upper-bound query count shown before a search starts (review finding I4).

    Branches on whether the deployment's primary provider has a price
    calendar, exactly as ``engine.orchestrator.run_search`` branches its
    strategy -- the old formula (``hubs * dates * (1 + dests)``) described the
    grid pipeline this branch replaced, and quoted it even for a two-stage
    search that no longer issues one query per date at all.

    Two-stage (``isinstance(provider, SupportsCalendar)``): phase 0 prices
    every day of the window in one request per hub/destination pair
    (``H*(1+D)``, doubled for a round trip); phase 1 confirms at most
    ``SHORTLIST_SIZE`` candidates, each needing 2 legs one-way or 4
    round-trip; phase 2 prices a through-fare baseline for up to
    ``THROUGH_FARE_DATES`` dates per destination. Summed, this lands within a
    few requests of the real count (measured: 93 one-way / 190 round-trip for
    8 hubs x 3 destinations x 91 days) -- nothing like the grid formula's
    ~768 for the same inputs.

    Grid (no calendar): the old formula, but over the *sampled* date count --
    the fallback path only ever queries at most ``FALLBACK_MAX_DATES``
    distinct dates, however many the user actually picked (see
    ``engine/grid.py``).

    Both branches are upper bounds: real runs skip hubs and dates that turn
    out unreachable, and neither branch's phase 1/2 costs are owed at all
    when a phase has fewer real candidates than these caps assume.
    """
    if isinstance(primary_provider(), SupportsCalendar):
        phase0 = hubs * (1 + dests)
        if round_trip:
            phase0 *= 2
        legs_per_candidate = 4 if round_trip else 2
        phase1 = SHORTLIST_SIZE * legs_per_candidate
        phase2 = THROUGH_FARE_DATES * dests
        return phase0 + phase1 + phase2

    sampled_dates = min(dates, FALLBACK_MAX_DATES)
    n_queries = hubs * sampled_dates * (1 + dests)
    return n_queries * 2 if round_trip else n_queries


# ── Background search task ───────────────────────────────────────────────────

async def run_and_report(bot, chat_id: int, params: dict) -> None:
    """Run a search, send the results, persist them, and offer to track them.

    Shared by the guided flow and by history reruns so both paths store the same
    fields — notably ``trip_days``, without which a rerun would silently change
    the trip shape. ``params["dates"]`` is the discrete date list the guided
    flow (or a history rerun) collected; the engine wants a contiguous
    ``SearchWindow``, so it is converted here, once, rather than pushed onto
    every caller.

    ``dates`` is also forwarded to ``run_search`` itself (as ``dates=``) and
    used again here to filter the returned itineraries down to
    ``date in set(dates)`` (review finding C1): the two-stage strategy prices
    every day of the window "for free", and the grid fallback resamples its
    own dates from the window when it isn't told otherwise -- neither of
    those is the same thing as "the exact dates the user asked for", and a
    result on a date nobody requested must never reach display or storage,
    just as a date the user did explicitly ask for must never be silently
    dropped.
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
            dates=dates,
        )
    except ValueError as exc:
        # A guided-flow date span is validated before this point (see
        # _oversized_window_message), but a history rerun of a search saved
        # before that validation existed can still reach run_search with an
        # oversized window. run_search's own ValueError message is already
        # written for a human (review finding I6) -- show it verbatim rather
        # than the generic "check the logs" message below, which would send
        # someone hunting for a bug that isn't there.
        logger.warning("Search rejected for %s: %s", params, exc)
        await bot.send_message(chat_id=chat_id, text=f"Search failed: {esc(exc)}")
        return
    except Exception:
        logger.exception("Search failed for %s", params)
        await bot.send_message(
            chat_id=chat_id,
            text="Search failed — check the bot logs for details.",
        )
        return

    requested_dates = set(dates)
    itineraries = [itin for itin in result.itineraries if itin.date in requested_dates]
    origin = params["origin"]
    currency = params["currency"]
    had_errors = bool(result.parse_errors or result.fetch_errors)

    # Empty and broken must never look alike (this project's central rule,
    # and the reason C2 was Critical). A provider that failed on every
    # request also returns an empty itinerary list; showing the same bold
    # "<b>No routes found.</b>" headline a genuinely empty search gets --
    # even with a corrective note in italics underneath -- states a false
    # fact first, in the part a skimming Telegram user actually reads, with
    # the correction relegated to fine print. That is the same
    # broken-looks-like-empty failure C2 exists to prevent, only softened.
    # A total failure (no itineraries at all) therefore gets its own
    # message instead of format_results ever running. A *partial* result
    # (some itineraries survived, alongside some errors) is genuinely
    # different -- there are real results to show, so they are shown, with
    # the note appended below them, where "the results above" is accurate.
    if not itineraries and had_errors:
        logger.warning(
            "Search completed with %d parse failures and %d fetch failures "
            "for %s — no results could be confirmed.",
            result.parse_errors, result.fetch_errors, params,
        )
        report_text = (
            "<b>Search incomplete</b> — "
            f"{result.parse_errors + result.fetch_errors} request(s) failed, "
            "so no results could be confirmed."
        )
    else:
        incomplete_notice = ""
        if had_errors:
            logger.warning(
                "Search completed with %d parse failures and %d fetch failures "
                "for %s — results may be incomplete.",
                result.parse_errors, result.fetch_errors, params,
            )
            incomplete_notice = (
                "\n\n<i>Note: "
                f"{result.parse_errors} parse and {result.fetch_errors} fetch "
                "request(s) failed during this search — treat the results "
                "above as incomplete, not a confirmed count.</i>"
            )
        report_text = format_results(itineraries, origin, currency) + incomplete_notice

    for chunk in split_message(report_text):
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
