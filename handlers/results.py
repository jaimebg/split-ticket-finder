"""Running a search and presenting its results.

``run_and_report`` is shared by the builder and by history reruns.
``_estimate_queries`` gives the builder its pre-flight query count.
``_oversized_window_message`` is kept because tests still exercise it
directly.

``tests/test_results_run.py`` patches ``run_search`` and
``primary_provider`` as attributes of *this* module. Any function here must
look those names up through this module's globals, never through a local
import, or the patches silently stop taking effect.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import FALLBACK_MAX_DATES, MAX_WINDOW_DAYS, SHORTLIST_SIZE, THROUGH_FARE_DATES
from db import get_search_by_id, save_search, set_search_view
from engine import run_search
from engine.orchestrator import STRATEGY_GRID, STRATEGY_TWO_STAGE
from handlers.anchor import render_anchor
from handlers.search.draft import Button, Rows
from handlers.start import owner_only_callback
from handlers.utils import esc
from models import CancelToken, Progress, SearchCancelled, SearchWindow
from providers.base import SupportsCalendar
from providers.registry import primary_provider
from results import store, view
from results.filters import Filters
from search import scan_to_json

logger = logging.getLogger(__name__)

# The running searches' cancel tokens, in Application.bot_data under this key.
# Memory is the right place: a search in flight cannot survive a restart.
RUNS_KEY = "runs"

# Spec §6.6: at most one progress edit per this many seconds.
PROGRESS_INTERVAL = 3.0


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


def _expected_strategy() -> str:
    """The strategy run_search will pick, by the same capability check."""
    if isinstance(primary_provider(), SupportsCalendar):
        return STRATEGY_TWO_STAGE
    return STRATEGY_GRID


class ProgressMessage:
    """The live progress display: one message, at most one edit per interval.

    The engine's progress callback is synchronous and fires on every leg, so
    ``tick`` only records the latest value. A background loop sleeps for the
    interval, then renders whatever is latest, and skips the edit when the
    text has not changed (Telegram rejects an identical edit). Progress is
    cosmetic: a failed edit is logged and never reaches the search.
    """

    def __init__(self, bot, chat_id: int, message_id: int | None, *, strategy: str,
                 currency: str, cancel_data: str, sleep=asyncio.sleep):
        self._bot = bot
        self._chat_id = chat_id
        self.message_id = message_id
        self._strategy = strategy
        self._currency = currency
        self._rows: Rows = [[Button("✖ Cancel", cancel_data)]]
        self._sleep = sleep
        self._latest: Progress | None = None
        self._shown: str | None = None
        self._task: asyncio.Task | None = None

    def tick(self, progress: Progress) -> None:
        self._latest = progress

    async def flush(self) -> None:
        """Render the latest tick. Never raises: a failed edit is retried by
        the next flush, since ``_shown`` only moves on success."""
        text = view.progress_text(self._latest, self._strategy, self._currency)
        if text == self._shown:
            return
        try:
            self.message_id = await render_anchor(self._bot, self._chat_id,
                                                  self.message_id, text, self._rows)
        except TelegramError as exc:
            logger.info("Progress edit failed (%s); the search continues.", exc)
            return
        self._shown = text

    async def _loop(self, interval: float) -> None:
        while True:
            await self._sleep(interval)
            await self.flush()

    def start(self, interval: float) -> None:
        self._task = asyncio.create_task(self._loop(interval))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


async def run_and_report(bot, chat_id: int, params: dict, *, message_id: int | None = None,
                         runs: dict[str, CancelToken] | None = None,
                         interval: float = PROGRESS_INTERVAL) -> None:
    """Run a search in one live message: progress, then its results.

    *message_id* is the message to take over: the builder's anchor. None
    sends a fresh one, as history reruns do. *runs* is the registry the
    Cancel button looks tokens up in.

    Shared by the builder and history reruns, so both store the same fields,
    notably ``trip_days``, without which a rerun would silently change the
    trip shape. ``params["dates"]`` is the discrete list the user asked for:
    it is forwarded to ``run_search`` and also used to filter the returned
    itineraries (review finding C1). A result on a date nobody asked for
    must never be shown or stored, and a date the user did ask for must
    never be dropped.
    """
    dates = params["dates"]
    window = SearchWindow(start=min(dates), end=max(dates))
    currency = params["currency"]

    run_id = secrets.token_hex(4)
    cancel = CancelToken()
    if runs is not None:
        runs[run_id] = cancel

    progress = ProgressMessage(bot, chat_id, message_id, strategy=_expected_strategy(),
                               currency=currency, cancel_data=f"x:{run_id}")
    await progress.flush()
    progress.start(interval)

    failure: str | None = None
    try:
        result = await run_search(
            origin=params["origin"],
            destinations=params["destinations"],
            hubs=params["hubs"],
            window=window,
            trip_days=params.get("trip_days", 0),
            adults=params["adults"],
            currency=currency,
            dates=dates,
            cancel=cancel,
            on_progress=progress.tick,
        )
    except SearchCancelled:
        failure = "Search cancelled."
    except ValueError as exc:
        # run_search's ValueError (an oversized window on a history rerun) is
        # written for a human, so it is shown verbatim (review finding I6).
        logger.warning("Search rejected for %s: %s", params, exc)
        failure = f"Search failed: {esc(exc)}"
    except Exception:
        logger.exception("Search failed for %s", params)
        failure = "Search failed — check the bot logs for details."
    finally:
        await progress.stop()
        if runs is not None:
            runs.pop(run_id, None)

    if failure is not None:
        await render_anchor(bot, chat_id, progress.message_id, failure, view.MENU_ROWS)
        return

    requested_dates = set(dates)
    itineraries = [itin for itin in result.itineraries if itin.date in requested_dates]
    had_errors = bool(result.parse_errors or result.fetch_errors)
    if had_errors:
        logger.warning(
            "Search completed with %d parse failures and %d fetch failures for %s.",
            result.parse_errors, result.fetch_errors, params,
        )

    best = min(itineraries, key=lambda it: it.total) if itineraries else None
    search_id = await save_search(
        origin=params["origin"],
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
        best_route=(f"{params['origin']}->{best.hub}->{best.dest} {best.date}"
                    if best else None),
        through_fare=best.through_fare if best else None,
        results=store.serialize(itineraries) if itineraries else None,
        scan_json=json.loads(scan_to_json(result.scan)),
        strategy=result.strategy,
    )

    # Empty and broken must never look alike (review finding C2). A provider
    # that failed on every request also returns no itineraries; that gets its
    # own message, never the "No routes found" a genuinely empty search gets.
    if not itineraries:
        text = (
            "<b>Search incomplete</b> — "
            f"{result.parse_errors + result.fetch_errors} request(s) failed, "
            "so no results could be confirmed."
            if had_errors else "<b>No routes found.</b>"
        )
        await render_anchor(bot, chat_id, progress.message_id, text, view.MENU_ROWS)
        return

    row = await get_search_by_id(search_id)
    text, rows = view.summary(view.SearchMeta.from_row(row), store.load(row["results"]),
                              Filters(), 1)
    if had_errors:
        text += (
            "\n\n<i>Note: "
            f"{result.parse_errors} parse and {result.fetch_errors} fetch "
            "request(s) failed during this search — treat these results as "
            "incomplete, not a confirmed count.</i>"
        )
    await render_anchor(bot, chat_id, progress.message_id, text, rows)


@owner_only_callback
async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    token = context.application.bot_data.get(RUNS_KEY, {}).get(query.data[2:])
    if token is None:
        await query.answer("That search is no longer running.", show_alert=True)
        return
    token.cancel()
    await query.answer("Cancelling…")


_GONE = "That search is no longer stored."


def _view_state(row: dict) -> dict:
    try:
        state = json.loads(row.get("view_json") or "{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


async def _show(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                rows: Rows) -> None:
    await render_anchor(context.bot, update.effective_chat.id,
                        update.callback_query.message.message_id, text, rows)


@owner_only_callback
async def on_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every r:<id>:... button. Reads the search back from the database, so
    buttons keep working across restarts (spec §1)."""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) < 3 or not parts[1].isdigit():
        await query.answer()
        await _show(update, context, _GONE, view.MENU_ROWS)
        return
    search_id, action, args = int(parts[1]), parts[2], parts[3:]

    if action == "n":
        await query.answer()
        return

    row = await get_search_by_id(search_id)
    stored = store.load(row.get("results")) if row else None
    if not row or not stored.itineraries:
        await query.answer()
        await _show(update, context, _GONE, view.MENU_ROWS)
        return

    meta = view.SearchMeta.from_row(row)
    state = _view_state(row)
    filters = Filters.from_dict(state.get("filters"))
    page = state.get("page") if isinstance(state.get("page"), int) else 1

    try:
        if action == "p":
            page = int(args[0])
            await set_search_view(search_id, {"filters": filters.to_dict(), "page": page})
            text, rows = view.summary(meta, stored, filters, page)
        elif action == "d":
            index = int(args[0])
            if not 0 <= index < len(stored.itineraries):
                raise LookupError(index)
            text, rows = view.detail(meta, stored, index, page)
        elif action == "f" and not stored.detailed:
            text, rows = view.summary(meta, stored, Filters(), 1)
        elif action == "f":
            if args == ["clear"]:
                filters = Filters()
            elif len(args) == 2:
                filters = filters.with_setting(args[0], args[1])
            elif args:
                raise LookupError(args)
            if args:
                await set_search_view(search_id, {"filters": filters.to_dict(), "page": 1})
            text, rows = view.filters_screen(meta, stored, filters)
        else:
            raise LookupError(action)
    except (LookupError, ValueError):
        await query.answer()
        await _show(update, context, _GONE, view.MENU_ROWS)
        return

    await query.answer()
    await _show(update, context, text, rows)


def get_results_handlers() -> list[CallbackQueryHandler]:
    return [
        CallbackQueryHandler(on_results, pattern=r"^r:\d+:"),
        CallbackQueryHandler(on_cancel, pattern=r"^x:[0-9a-f]+$"),
    ]
