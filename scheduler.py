"""Background scheduler that periodically checks favorite routes for price drops."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

from config import ALERT_INTERVAL_HOURS, PRICE_DROP_THRESHOLD, SPARK_POINTS
from db import add_price_check, get_favorites, get_price_checks, update_favorite_price
from engine import run_search
from handlers.anchor import markup
from handlers.search.draft import Button
from models import SearchWindow, bookable_onward_dates
from providers.base import SearchOptions, capabilities_of
from providers.registry import get_provider, primary_provider
from results.history import alert_text, sparkline, trend_signal

logger = logging.getLogger(__name__)


async def check_favorites(bot, owner_chat_id: int) -> None:
    """Iterate all favorites and check current prices against records.

    Pricing is entirely the engine's job: this calls ``engine.run_search``
    with the favourite's own (single) hub and destination and reads the
    total off whichever itinerary comes back cheapest. It must never
    recompute ``dom_price * (1 - discount) + onward_price`` itself — two
    implementations of that one formula is exactly the shape of the
    round-trip bug fixed in e83a4d3.
    """
    favorites = await get_favorites()
    if not favorites:
        logger.info("No favorites to check.")
        return

    for fav in favorites:
        fav_id = fav["id"]
        origin = fav["origin"]
        hub = fav["hub"]
        destination = fav["destination"]
        options = SearchOptions.from_mapping(fav)
        currency = options.currency
        record_price = fav["record_price"]

        # A favourite saved from a round-trip search has a record price
        # covering all four legs. Re-pricing it as one-way would halve the
        # total and read as a price drop on every single cycle, so the trip
        # shape has to be replayed exactly as it was quoted.
        trip_days = fav.get("trip_days") or 0

        try:
            all_dates = json.loads(fav["check_dates"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Favorite %d has invalid check_dates, skipping.", fav_id)
            continue

        if not all_dates:
            logger.warning("Favorite %d has no check_dates, skipping.", fav_id)
            continue

        # The full tracked range, not a sample of it (review finding I5): the
        # old `_sample_dates(all_dates, max_n=5)` picked five evenly-spaced
        # indices that never included the last one, so a favourite tracked
        # over e.g. 2026-09-01..2026-11-30 had its final ~18 days never
        # re-priced -- a genuine drop there was undetectable, contradicting
        # handlers/favorites.py's own "Track every date the search covered".
        # That sampling saved requests only under the old grid engine, where
        # date coverage cost real queries; a price-calendar provider prices
        # the whole window for one request regardless of how wide it is, so
        # there is nothing left to save by sampling.
        # A night at the hub can't fly an onward flight today: the domestic
        # leg would be yesterday.
        all_dates = bookable_onward_dates(all_dates, options.overnight,
                                          date.today().isoformat())
        if not all_dates:
            logger.info("Favorite %d has no flyable dates left, skipping.", fav_id)
            continue
        window = SearchWindow(start=min(all_dates), end=max(all_dates))

        # The query shape a favourite's price was quoted under has to be
        # replayed exactly, same reasoning as trip_days above — a provider
        # named at save time is resolved back to that same provider.
        # No provider recorded means either this favourite predates Task
        # 11's provider column, or it was tracked from a stored search that
        # itself never got tagged. There is no query shape left to replay,
        # so this deliberately falls back to the deployment's primary
        # provider. That is an explicit decision made here, not an accident
        # of passing provider=None through to run_search and letting its
        # own default apply — the two happen to pick the same provider
        # today, but for different reasons, and only one of them is a
        # decision this module owns.
        provider_name = fav.get("provider")
        provider = get_provider(provider_name) if provider_name else primary_provider()

        # The options a price was quoted under must be replayed exactly. A
        # provider that can't express them would price a different query and
        # report the difference as a movement, so skip instead.
        refusal = capabilities_of(provider).rejects(options)
        if refusal is not None:
            logger.warning("Favorite %d skipped: %s", fav_id, refusal)
            continue

        try:
            result = await run_search(
                origin=origin,
                destinations={destination: destination},
                hubs={hub: hub},
                window=window,
                trip_days=trip_days,
                options=options,
                # The exact tracked dates: on a grid (calendar-less) provider
                # the engine would otherwise resample the window and could
                # drop the last date.
                dates=all_dates,
                provider=provider,
            )
        except Exception:
            logger.exception("Error checking favorite %d", fav_id)
            continue

        itineraries = result.itineraries
        if not itineraries:
            # Empty and broken must not be logged alike (review finding C2):
            # a provider erroring on every request also returns an empty
            # itinerary list here, and without this check that reads in the
            # logs exactly like a route that was genuinely searched and
            # came back empty.
            if result.parse_errors or result.fetch_errors:
                logger.warning(
                    "Favorite %d: %d parse and %d fetch failures — could not "
                    "fully check this route, not necessarily no flights.",
                    fav_id, result.parse_errors, result.fetch_errors,
                )
            else:
                logger.info("Favorite %d: no flights found in tracked dates.", fav_id)
            continue

        best = min(itineraries, key=lambda itin: itin.total)
        best_price = float(best.total)
        best_detail = {
            "hub": best.hub,
            "dest": best.dest,
            "date": best.date,
            "return_date": best.return_date,
            "trip_days": trip_days,
            "dom_price": float(best.dom_price),
            "onward_price": float(best.onward_price),
        }

        prior = await get_price_checks(fav_id)
        await add_price_check(fav_id, best_price, best_detail)

        if record_price is None:
            # A favourite saved without a price: its first check is its record.
            await update_favorite_price(fav_id, best_price, is_record=True)
            logger.info("Favorite %d: first check sets the record at %.2f %s",
                        fav_id, best_price, currency)
            continue

        record_drop = best_price < record_price * (1 - PRICE_DROP_THRESHOLD)
        trend = trend_signal(prior, best_price, today=date.today())
        if record_drop or trend is not None:
            prices = [p for _, p in prior if p is not None] + [best_price]
            text = alert_text(
                fav, origin=origin, last=best_price, date=best.date,
                record_before=record_price, record_drop=record_drop, trend=trend,
                spark=sparkline(prices[-SPARK_POINTS:]),
            )
            try:
                await bot.send_message(
                    chat_id=owner_chat_id, text=text, parse_mode="HTML",
                    reply_markup=markup([[Button("📈 History", f"fh:{fav_id}")]]),
                )
            except Exception:
                logger.exception("Failed to send price alert for favorite %d", fav_id)
            logger.info("Favorite %d: alert (record_drop=%s, trend=%s) at %.2f %s",
                        fav_id, record_drop, trend is not None, best_price, currency)

        # Only a record drop moves the record: it means "the best price we
        # alerted on", and a trend alert is about the recent average instead.
        await update_favorite_price(fav_id, best_price, is_record=record_drop)


async def scheduler_loop(bot, owner_chat_id: int) -> None:
    """Infinite loop: sleep, then check favorites. Never dies."""
    logger.info(
        "Scheduler started — checking every %d hour(s).", ALERT_INTERVAL_HOURS
    )
    while True:
        try:
            await asyncio.sleep(ALERT_INTERVAL_HOURS * 3600)
            logger.info("Scheduler: running price checks…")
            await check_favorites(bot, owner_chat_id)
        except asyncio.CancelledError:
            logger.info("Scheduler cancelled, exiting.")
            break
        except Exception:
            logger.exception("Scheduler: unhandled error (will retry next cycle).")
