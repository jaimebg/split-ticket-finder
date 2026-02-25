"""Background scheduler that periodically checks favorite routes for price drops."""
from __future__ import annotations

import asyncio
import json
import logging

from config import (
    ALERT_INTERVAL_HOURS,
    DISCOUNT_AIRPORTS,
    DOMESTIC_DISCOUNT,
    DEFAULT_DELAY,
    PRICE_DROP_THRESHOLD,
)
from db import add_price_check, get_favorites, update_favorite_price
from scraper import search

logger = logging.getLogger(__name__)


def _sample_dates(dates: list[str], max_n: int = 5) -> list[str]:
    """Return up to *max_n* evenly-spaced dates from the list."""
    if len(dates) <= max_n:
        return list(dates)
    step = len(dates) / max_n
    return [dates[int(i * step)] for i in range(max_n)]


async def check_favorites(bot, owner_chat_id: int) -> None:
    """Iterate all favorites and check current prices against records."""
    favorites = await get_favorites()
    if not favorites:
        logger.info("No favorites to check.")
        return

    for fav in favorites:
        fav_id = fav["id"]
        origin = fav["origin"]
        hub = fav["hub"]
        destination = fav["destination"]
        adults = fav["adults"]
        currency = fav["currency"]
        record_price = fav["record_price"]

        # Parse check_dates from JSON string
        try:
            all_dates = json.loads(fav["check_dates"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Favorite %d has invalid check_dates, skipping.", fav_id)
            continue

        if not all_dates:
            logger.warning("Favorite %d has no check_dates, skipping.", fav_id)
            continue

        sampled = _sample_dates(all_dates, max_n=5)
        best_price: float | None = None
        best_detail: dict | None = None

        for date in sampled:
            try:
                # Search domestic leg: origin -> hub
                dom_results = await search(origin, hub, date, adults, currency)
                if not dom_results:
                    await asyncio.sleep(DEFAULT_DELAY)
                    continue

                # Search international leg: hub -> destination
                intl_results = await search(hub, destination, date, adults, currency)
                if not intl_results:
                    await asyncio.sleep(DEFAULT_DELAY)
                    continue

                dom_price = dom_results[0].price
                intl_price = intl_results[0].price

                # Apply domestic discount if hub is in DISCOUNT_AIRPORTS
                if hub in DISCOUNT_AIRPORTS:
                    dom_discounted = dom_price * (1 - DOMESTIC_DISCOUNT)
                else:
                    dom_discounted = float(dom_price)

                total = dom_discounted + intl_price

                if best_price is None or total < best_price:
                    best_price = total
                    best_detail = {
                        "hub": hub,
                        "dest": destination,
                        "date": date,
                        "dom_price": dom_price,
                        "intl_price": intl_price,
                    }

            except Exception:
                logger.exception(
                    "Error checking favorite %d on date %s", fav_id, date
                )

            await asyncio.sleep(DEFAULT_DELAY)

        # After all dates checked for this favorite
        if best_price is None:
            logger.info("Favorite %d: no flights found in sampled dates.", fav_id)
            continue

        # Save price check record
        await add_price_check(fav_id, best_price, best_detail)

        # Compare against record price
        if (
            record_price is not None
            and best_price < record_price * (1 - PRICE_DROP_THRESHOLD)
        ):
            # Price drop detected — send alert and update record
            alert_msg = (
                f"Price drop alert! "
                f"{origin}->{hub}->{destination}: "
                f"{best_price:.0f} {currency} "
                f"(was {record_price:.0f})"
            )
            try:
                await bot.send_message(chat_id=owner_chat_id, text=alert_msg)
            except Exception:
                logger.exception("Failed to send price drop alert for favorite %d", fav_id)
            await update_favorite_price(fav_id, best_price, is_record=True)
            logger.info(
                "Favorite %d: price drop! %.0f -> %.0f %s",
                fav_id, record_price, best_price, currency,
            )
        else:
            # No significant drop — just update last price
            await update_favorite_price(fav_id, best_price, is_record=False)
            logger.info(
                "Favorite %d: checked, best=%.0f %s (record=%s)",
                fav_id, best_price, currency, record_price,
            )


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
