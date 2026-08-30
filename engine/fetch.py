"""Bounded-concurrency leg fetching, with cancellation and progress.

A search issues hundreds of requests. Running them serially took tens of
minutes; running them all at once gets a provider to block us. This caps
in-flight requests and still spaces out each worker's own requests, so
throughput scales with the cap while the request rate stays predictable.

One broken leg must not abort a search of hundreds, so provider errors are
counted and the leg is dropped. Cancellation is the one exception: it is a
user decision, and it propagates.
"""
from __future__ import annotations

import asyncio
import logging

from models import CancelToken, Progress, ProgressCallback
from providers.base import (
    FlightProvider,
    LegQuery,
    Offer,
    ProviderFetchError,
    ProviderParseError,
)

logger = logging.getLogger(__name__)

# A leg is one origin->dest query on one date.
LegKey = tuple[str, str, str]


def report(
    on_progress: ProgressCallback | None, phase: str, done: int, total: int
) -> None:
    """Emit a progress tick, swallowing anything the callback throws.

    Progress is cosmetic. A UI callback that fails -- a Telegram edit hitting a
    rate limit, say -- must never cost a search that has already done its work.
    Phase 0 imports this rather than duplicating it.
    """
    if on_progress is None:
        return
    try:
        on_progress(Progress(phase=phase, done=done, total=total))
    except Exception:
        logger.exception("Progress callback failed; continuing search.")


class LegFetcher:
    """Runs many leg queries against one provider under a concurrency cap."""

    def __init__(
        self,
        provider: FlightProvider,
        *,
        concurrency: int,
        delay: float,
        cancel: CancelToken | None = None,
        on_progress: ProgressCallback | None = None,
    ):
        self._provider = provider
        self._concurrency = concurrency
        self._delay = delay
        self._cancel = cancel
        self._on_progress = on_progress
        self._semaphore = asyncio.Semaphore(concurrency)
        self.parse_errors = 0
        self.fetch_errors = 0

    def _report(self, phase: str, done: int, total: int) -> None:
        report(self._on_progress, phase, done, total)

    async def _one(self, query: LegQuery) -> list[Offer]:
        async with self._semaphore:
            if self._cancel is not None:
                self._cancel.raise_if_cancelled()
            try:
                return await self._provider.search_leg(query)
            except ProviderParseError as exc:
                self.parse_errors += 1
                logger.warning("Parse failed for %s->%s %s: %s",
                               query.origin, query.dest, query.date, exc)
                return []
            except ProviderFetchError as exc:
                self.fetch_errors += 1
                logger.warning("Fetch failed for %s->%s %s: %s",
                               query.origin, query.dest, query.date, exc)
                return []
            finally:
                # Hold the slot for the delay, so the rate limit is per-worker.
                if self._delay:
                    await asyncio.sleep(self._delay)

    async def fetch_many(
        self, queries: list[LegQuery], phase: str
    ) -> dict[LegKey, list[Offer]]:
        """Fetch every query concurrently, returning only legs with results."""
        if not queries:
            return {}
        if self._cancel is not None:
            self._cancel.raise_if_cancelled()

        total = len(queries)
        logger.info("%s: %d queries (concurrency %d)", phase, total, self._concurrency)
        self._report(phase, 0, total)

        found: dict[LegKey, list[Offer]] = {}
        done = 0
        tasks = [asyncio.create_task(self._one(q)) for q in queries]
        try:
            for query, task in zip(queries, tasks, strict=True):
                offers = await task
                done += 1
                if offers:
                    found[(query.origin, query.dest, query.date)] = offers
                self._report(phase, done, total)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        logger.info("%s done: %d/%d legs with results", phase, len(found), total)
        return found
