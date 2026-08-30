"""Tests for the bounded-concurrency, cancellable leg fetcher."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from engine.fetch import LegFetcher
from models import CancelToken, Progress, SearchCancelled
from providers.base import LegQuery, Offer, ProviderFetchError, ProviderParseError


def _offer(price: str) -> Offer:
    return Offer(price=Decimal(price), currency="EUR", airlines=[], stops=0,
                 duration=100, segments=[], provider="fake")


class FakeProvider:
    """Records the queries it receives and replays scripted answers."""

    name = "fake"

    def __init__(self, answers=None, error=None, delay=0.0):
        self.answers = answers or {}
        self.error = error
        self.delay = delay
        self.seen: list[LegQuery] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def search_leg(self, query):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.seen.append(query)
            if self.error is not None:
                raise self.error
            return self.answers.get((query.origin, query.dest, query.date), [])
        finally:
            self.in_flight -= 1

    async def aclose(self):
        return None


def _q(origin, dest, date):
    return LegQuery(origin=origin, dest=dest, date=date)


async def test_fetches_every_query_and_keys_results_by_leg():
    provider = FakeProvider({("LPA", "MAD", "2026-10-01"): [_offer("29")]})
    fetcher = LegFetcher(provider, concurrency=4, delay=0)

    found = await fetcher.fetch_many(
        [_q("LPA", "MAD", "2026-10-01"), _q("LPA", "BCN", "2026-10-01")], phase="Phase 1"
    )

    assert set(found) == {("LPA", "MAD", "2026-10-01")}
    assert found[("LPA", "MAD", "2026-10-01")][0].price == Decimal("29")


async def test_empty_query_list_makes_no_calls():
    provider = FakeProvider()
    assert await LegFetcher(provider, concurrency=4, delay=0).fetch_many([], phase="p") == {}
    assert provider.seen == []


async def test_respects_the_concurrency_cap():
    provider = FakeProvider(delay=0.01)
    fetcher = LegFetcher(provider, concurrency=3, delay=0)
    await fetcher.fetch_many([_q("LPA", "MAD", f"2026-10-{d:02d}") for d in range(1, 13)],
                             phase="Phase 1")
    assert provider.peak_in_flight <= 3


async def test_provider_errors_are_counted_not_raised():
    """One broken leg must not abort a search of hundreds."""
    provider = FakeProvider(error=ProviderParseError("schema moved"))
    fetcher = LegFetcher(provider, concurrency=2, delay=0)

    found = await fetcher.fetch_many([_q("LPA", "MAD", "2026-10-01")], phase="Phase 1")

    assert found == {}
    assert fetcher.parse_errors == 1
    assert fetcher.fetch_errors == 0


async def test_fetch_errors_are_counted_separately():
    provider = FakeProvider(error=ProviderFetchError("timeout"))
    fetcher = LegFetcher(provider, concurrency=2, delay=0)
    await fetcher.fetch_many([_q("LPA", "MAD", "2026-10-01")], phase="Phase 1")
    assert fetcher.fetch_errors == 1
    assert fetcher.parse_errors == 0


async def test_cancellation_stops_the_run_and_raises():
    provider = FakeProvider(delay=0.01)
    token = CancelToken()
    fetcher = LegFetcher(provider, concurrency=2, delay=0, cancel=token)
    token.cancel()

    with pytest.raises(SearchCancelled):
        await fetcher.fetch_many(
            [_q("LPA", "MAD", f"2026-10-{d:02d}") for d in range(1, 21)], phase="Phase 1"
        )
    # Cancelled before any work began, so nothing was requested.
    assert provider.seen == []


async def test_progress_is_reported_and_ends_complete():
    provider = FakeProvider()
    ticks: list[Progress] = []
    fetcher = LegFetcher(provider, concurrency=2, delay=0, on_progress=ticks.append)

    await fetcher.fetch_many(
        [_q("LPA", "MAD", f"2026-10-{d:02d}") for d in range(1, 5)], phase="Phase 1"
    )

    assert ticks, "at least one progress tick"
    assert all(t.phase == "Phase 1" for t in ticks)
    assert all(t.total == 4 for t in ticks)
    assert ticks[-1].done == 4
    assert ticks[-1].fraction == 1.0


async def test_a_failing_progress_callback_does_not_break_the_search():
    """Progress is cosmetic; a broken UI callback must not lose a search."""
    def boom(_):
        raise RuntimeError("telegram is down")

    provider = FakeProvider({("LPA", "MAD", "2026-10-01"): [_offer("29")]})
    fetcher = LegFetcher(provider, concurrency=2, delay=0, on_progress=boom)

    found = await fetcher.fetch_many([_q("LPA", "MAD", "2026-10-01")], phase="Phase 1")
    assert found[("LPA", "MAD", "2026-10-01")][0].price == Decimal("29")
