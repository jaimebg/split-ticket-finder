"""Background scheduler that periodically checks favorite routes for price drops."""
from __future__ import annotations

import asyncio
import json
import logging

from config import ALERT_INTERVAL_HOURS, PRICE_DROP_THRESHOLD
from db import add_price_check, get_favorites, update_favorite_price
from engine import run_search
from models import SearchWindow
from providers.registry import get_provider, primary_provider

logger = logging.getLogger(__name__)


def _sample_dates(dates: list[str], max_n: int = 5) -> list[str]:
    """Return up to *max_n* evenly-spaced dates from the list."""
    if len(dates) <= max_n:
        return list(dates)
    step = len(dates) / max_n
    return [dates[int(i * step)] for i in range(max_n)]


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
        adults = fav["adults"]
        currency = fav["currency"]
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

        sampled = _sample_dates(all_dates, max_n=5)
        window = SearchWindow(start=min(sampled), end=max(sampled))

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

        try:
            result = await run_search(
                origin=origin,
                destinations={destination: destination},
                hubs={hub: hub},
                window=window,
                trip_days=trip_days,
                adults=adults,
                currency=currency,
                provider=provider,
            )
        except Exception:
            logger.exception("Error checking favorite %d", fav_id)
            continue

        itineraries = result.itineraries
        if not itineraries:
            logger.info("Favorite %d: no flights found in sampled dates.", fav_id)
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
                f"{best_price:.2f} {currency} "
                f"(was {record_price:.2f})"
            )
            try:
                await bot.send_message(chat_id=owner_chat_id, text=alert_msg)
            except Exception:
                logger.exception("Failed to send price drop alert for favorite %d", fav_id)
            await update_favorite_price(fav_id, best_price, is_record=True)
            logger.info(
                "Favorite %d: price drop! %.2f -> %.2f %s",
                fav_id, record_price, best_price, currency,
            )
        else:
            # No significant drop — just update last price
            await update_favorite_price(fav_id, best_price, is_record=False)
            logger.info(
                "Favorite %d: checked, best=%.2f %s (record=%s)",
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
