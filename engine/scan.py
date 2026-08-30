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
    """Every calendar a search needs, keyed for phase 0b's arithmetic.

    parse_errors/fetch_errors count leg pairs whose calendar could not be
    read -- they travel with the grid rather than a separate return value,
    because the grid is what flows on to phase 0b and a search summary
    needs to say when its ranking was built from an incomplete grid.
    """

    out_dom: dict[str, dict[str, RatedPrice]]
    ret_dom: dict[str, dict[str, RatedPrice]]
    out_onward: dict[tuple[str, str], dict[str, RatedPrice]]
    ret_onward: dict[tuple[str, str], dict[str, RatedPrice]]
    parse_errors: int = 0
    fetch_errors: int = 0


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
    aborting the scan; the other leg pairs are unaffected.
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
    parse_errors = 0
    fetch_errors = 0

    if not jobs:
        return CalendarGrid(out_dom=out_dom, ret_dom=ret_dom, out_onward=out_onward,
                             ret_onward=ret_onward, parse_errors=parse_errors,
                             fetch_errors=fetch_errors)

    total = len(jobs)
    phase = "Phase 0"
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(query: CalendarQuery) -> dict[str, RatedPrice]:
        nonlocal parse_errors, fetch_errors
        async with semaphore:
            if cancel is not None:
                cancel.raise_if_cancelled()
            # Three kinds of failure reach this point, handled differently on
            # purpose -- mirrors LegFetcher._one:
            #
            # - ProviderParseError / ProviderFetchError: this leg pair's
            #   calendar failed -- count it, drop it, keep going. Other leg
            #   pairs may well succeed.
            # - A bare ProviderError: the provider cannot answer this *kind*
            #   of query at all. Every leg pair would fail the same way, so
            #   this is deliberately left uncaught here: it propagates out of
            #   scan_calendars and aborts the scan. Counting it like a
            #   per-leg failure would report a misconfigured search as "no
            #   flights found", which is exactly the broken-looks-like-empty
            #   failure this codebase exists to prevent. Do not add
            #   "except ProviderError" as a tidy-up.
            # - SearchCancelled: a user decision; propagates.
            try:
                return await provider.price_calendar(query)
            except ProviderParseError as exc:
                parse_errors += 1
                logger.warning("Calendar parse failed for %s->%s: %s",
                               query.origin, query.dest, exc)
                return {}
            except ProviderFetchError as exc:
                fetch_errors += 1
                logger.warning("Calendar fetch failed for %s->%s: %s",
                               query.origin, query.dest, exc)
                return {}
            finally:
                # Hold the slot for the delay, so the rate limit is per-worker.
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

    return CalendarGrid(out_dom=out_dom, ret_dom=ret_dom, out_onward=out_onward,
                         ret_onward=ret_onward, parse_errors=parse_errors,
                         fetch_errors=fetch_errors)
