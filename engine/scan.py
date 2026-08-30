"""Phase 0: price an entire date window from calendars.

This is the capability the whole two-stage design rests on. One request
prices a whole window, so date coverage stops scaling with request count:
a 91-day window costs exactly what a one-day window costs, and the cost is
H*(1+D), doubled for a round trip.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from engine.fetch import report
from models import CancelToken, ProgressCallback, SearchWindow, add_days
from providers.base import (
    CalendarQuery,
    ProviderFetchError,
    ProviderParseError,
    RatedPrice,
    SupportsCalendar,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalendarGrid:
    """Every calendar a search needs, keyed for phase 0b's arithmetic."""

    out_dom: dict[str, dict[str, RatedPrice]]
    ret_dom: dict[str, dict[str, RatedPrice]]
    out_onward: dict[tuple[str, str], dict[str, RatedPrice]]
    ret_onward: dict[tuple[str, str], dict[str, RatedPrice]]


# One calendar request: which slot of the grid it belongs under, and the
# key (hub, or (hub, dest)) it should be filed at within that slot.
_CalendarJob = tuple[CalendarQuery, str, object]


def _build_jobs(
    *,
    origin: str,
    hubs: list[str],
    dests: list[str],
    window: SearchWindow,
    trip_days: int,
    adults: int,
    currency: str,
) -> list[_CalendarJob]:
    jobs: list[_CalendarJob] = []
    for hub in hubs:
        jobs.append((
            CalendarQuery(origin=origin, dest=hub, start=window.start, end=window.end,
                          adults=adults, currency=currency),
            "out_dom", hub,
        ))
        for dest in dests:
            jobs.append((
                CalendarQuery(origin=hub, dest=dest, start=window.start, end=window.end,
                              adults=adults, currency=currency),
                "out_onward", (hub, dest),
            ))

    if trip_days > 0:
        ret_start = add_days(window.start, trip_days)
        ret_end = add_days(window.end, trip_days)
        for hub in hubs:
            jobs.append((
                CalendarQuery(origin=hub, dest=origin, start=ret_start, end=ret_end,
                              adults=adults, currency=currency),
                "ret_dom", hub,
            ))
            for dest in dests:
                jobs.append((
                    CalendarQuery(origin=dest, dest=hub, start=ret_start, end=ret_end,
                                  adults=adults, currency=currency),
                    "ret_onward", (hub, dest),
                ))

    return jobs


async def scan_calendars(
    provider: SupportsCalendar,
    *,
    origin: str,
    hubs: list[str],
    dests: list[str],
    window: SearchWindow,
    trip_days: int,
    adults: int,
    currency: str,
    concurrency: int = 8,
    delay: float = 0.0,
    cancel: CancelToken | None = None,
    on_progress: ProgressCallback | None = None,
) -> CalendarGrid:
    """Price every leg pair's whole date window in one request each.

    Builds origin->hub and hub->dest calendars over ``window``, and for a
    round trip (trip_days > 0) also hub->origin and dest->hub calendars over
    ``window`` shifted forward by ``trip_days`` at both endpoints. Runs them
    under the same bounded-concurrency, cancellable, per-worker-delay
    discipline as LegFetcher, and reports progress through the same
    swallow-exceptions helper. A leg pair whose calendar cannot be fetched or
    parsed is counted and dropped -- absent from the grid -- rather than
    aborting the scan.
    """
    if cancel is not None:
        cancel.raise_if_cancelled()

    jobs = _build_jobs(
        origin=origin, hubs=hubs, dests=dests, window=window, trip_days=trip_days,
        adults=adults, currency=currency,
    )

    out_dom: dict[str, dict[str, RatedPrice]] = {}
    ret_dom: dict[str, dict[str, RatedPrice]] = {}
    out_onward: dict[tuple[str, str], dict[str, RatedPrice]] = {}
    ret_onward: dict[tuple[str, str], dict[str, RatedPrice]] = {}
    slots = {
        "out_dom": out_dom,
        "ret_dom": ret_dom,
        "out_onward": out_onward,
        "ret_onward": ret_onward,
    }

    if not jobs:
        return CalendarGrid(out_dom=out_dom, ret_dom=ret_dom,
                             out_onward=out_onward, ret_onward=ret_onward)

    total = len(jobs)
    phase = "Phase 0"
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(query: CalendarQuery) -> dict[str, RatedPrice]:
        async with semaphore:
            if cancel is not None:
                cancel.raise_if_cancelled()
            try:
                return await provider.price_calendar(query)
            except ProviderParseError as exc:
                logger.warning("Calendar parse failed for %s->%s: %s",
                               query.origin, query.dest, exc)
                return {}
            except ProviderFetchError as exc:
                logger.warning("Calendar fetch failed for %s->%s: %s",
                               query.origin, query.dest, exc)
                return {}
            finally:
                if delay:
                    await asyncio.sleep(delay)

    report(on_progress, phase, 0, total)

    tasks = [asyncio.create_task(_one(query)) for query, _, _ in jobs]
    done = 0
    try:
        for (_, slot_name, key), task in zip(jobs, tasks, strict=True):
            prices = await task
            done += 1
            if prices:
                slots[slot_name][key] = prices
            report(on_progress, phase, done, total)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return CalendarGrid(out_dom=out_dom, ret_dom=ret_dom,
                         out_onward=out_onward, ret_onward=ret_onward)
