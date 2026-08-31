"""Tests for the price-tracking scheduler.

Task 12 rewired ``check_favorites`` to price favourites through
``engine.run_search`` instead of querying legs and computing
``dom_price * (1 - discount) + onward_price`` itself -- two implementations
of one formula was exactly the shape of the round-trip bug fixed in
e83a4d3. These tests fake the engine call, not the provider layer.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import db as db_module
import scheduler as scheduler_module
from models import Itinerary
from providers.base import Offer
from scheduler import _sample_dates, check_favorites


class FakeBot:
    """Records the alerts the scheduler tries to send."""

    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


def _offer(price: str) -> Offer:
    return Offer(price=Decimal(price), currency="EUR", airlines=["Iberia"],
                 stops=0, duration=120, segments=[], provider="fake")


def _itin(
    *, hub="MAD", dest="NRT", date="2026-09-01", return_date="",
    discount="0.5", dom_price="100", onward_price="500",
) -> Itinerary:
    """A confirmed itinerary with an arbitrary, engine-chosen discount.

    The discount (0.5) deliberately disagrees with config's own
    DOMESTIC_DISCOUNT (0.75): if the scheduler recomputed the total itself
    using config's rate, it would land on a different number than the one
    this itinerary's own ``.total`` reports.
    """
    return Itinerary(
        date=date, return_date=return_date, hub=hub, hub_name=hub, dest=dest,
        dest_name=dest, discount=Decimal(discount),
        dom_out=_offer(dom_price), onward_out=_offer(onward_price),
    )


@pytest.fixture
def fake_engine(monkeypatch):
    """Replace scheduler.run_search with a scripted, capture-everything fake."""
    calls: list[dict] = []
    state = {"itineraries": [_itin()]}

    async def fake_run_search(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(itineraries=state["itineraries"])

    monkeypatch.setattr(scheduler_module, "run_search", fake_run_search)
    return {"calls": calls, "state": state}


# ── Date sampling ───────────────────────────────────────────────────────────


def test_sample_dates_returns_all_when_under_the_cap():
    dates = ["2026-09-01", "2026-09-02"]
    assert _sample_dates(dates, max_n=5) == dates


def test_sample_dates_spreads_across_the_range():
    dates = [f"2026-09-{d:02d}" for d in range(1, 21)]
    sampled = _sample_dates(dates, max_n=5)

    assert len(sampled) == 5
    assert sampled[0] == "2026-09-01"
    assert all(d in dates for d in sampled)
    assert sampled == sorted(sampled)


def test_sample_dates_handles_empty_input():
    assert _sample_dates([], max_n=5) == []


# ── Task 12 requirement: the scheduler must not recompute a discount ────────


def test_scheduler_module_no_longer_imports_discount_config():
    """The discount formula now lives in exactly one place -- the engine.
    Two implementations of it is the shape of the bug fixed in e83a4d3."""
    assert not hasattr(scheduler_module, "DISCOUNT_AIRPORTS")
    assert not hasattr(scheduler_module, "DOMESTIC_DISCOUNT")


async def test_check_favorites_uses_the_engines_total_verbatim(temp_db, fake_engine):
    """dom_price=100 * (1 - 0.5) + onward_price=500 = 550 -- if the scheduler
    recomputed using config's DOMESTIC_DISCOUNT (0.75) it would record 525
    instead. Only 550 (the engine's own total) proves no local recompute."""
    fake_engine["state"]["itineraries"] = [
        _itin(discount="0.5", dom_price="100", onward_price="500")
    ]

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=None, check_dates=["2026-09-01"], trip_days=0,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    fav = (await db_module.get_favorites())[0]
    assert fav["last_price"] == pytest.approx(550.0)


async def test_round_trip_favorite_forwards_trip_days_to_the_engine(temp_db, fake_engine):
    """Regression guard for e83a4d3: a round-trip favourite must be replayed
    as round-trip, not one-way. The engine owns the actual round-trip
    pricing now (tested exhaustively in tests/test_engine_drill.py); the
    scheduler's own job is just to forward trip_days unchanged."""
    fake_engine["state"]["itineraries"] = [_itin(return_date="2026-09-15")]

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=1230.0, check_dates=["2026-09-01"], trip_days=14,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    assert fake_engine["calls"][0]["trip_days"] == 14


async def test_one_way_favorite_forwards_trip_days_zero(temp_db, fake_engine):
    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=["2026-09-01"], trip_days=0,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    assert fake_engine["calls"][0]["trip_days"] == 0


async def test_favorite_provider_is_resolved_and_forwarded(temp_db, fake_engine, monkeypatch):
    fake_provider = object()
    monkeypatch.setattr(scheduler_module, "get_provider", lambda name: fake_provider)

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=None, check_dates=["2026-09-01"], trip_days=0, provider="kiwi",
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    assert fake_engine["calls"][0]["provider"] is fake_provider


async def test_favorite_without_a_stored_provider_falls_back_to_the_primary(
    temp_db, fake_engine, monkeypatch
):
    """No recorded provider means there is no query shape to replay -- the
    scheduler must fall back to the deployment's primary provider *itself*,
    explicitly, rather than passing provider=None and relying on
    run_search's own default. Both land on the same provider today, but for
    different reasons: run_search's default is "no provider was given";
    this is "no provider was recorded, so use the primary" -- a decision
    that must be visible in scheduler.py, not an accident of a bare None
    happening to fall through correctly."""
    fake_primary = object()
    monkeypatch.setattr(scheduler_module, "primary_provider", lambda: fake_primary)

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=None, check_dates=["2026-09-01"], trip_days=0,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    assert fake_engine["calls"][0]["provider"] is fake_primary


# ── Alerting ─────────────────────────────────────────────────────────────────


async def test_genuine_price_drop_alerts_and_updates_the_record(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [
        _itin(discount="0", dom_price="50", onward_price="300")  # total 350
    ]

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=["2026-09-01"], trip_days=0,
    )

    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)

    assert len(bot.messages) == 1
    assert "Price drop" in bot.messages[0]
    assert "350" in bot.messages[0]

    fav = (await db_module.get_favorites())[0]
    assert fav["record_price"] == pytest.approx(350.0)


async def test_small_price_change_does_not_alert(temp_db, fake_engine):
    """Below the 10% threshold, the record stands and no alert is sent."""
    fake_engine["state"]["itineraries"] = [
        _itin(discount="0", dom_price="100", onward_price="480")  # total 580
    ]

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=["2026-09-01"], trip_days=0,
    )

    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)

    assert bot.messages == []
    fav = (await db_module.get_favorites())[0]
    assert fav["record_price"] == pytest.approx(600.0)  # record unchanged
    assert fav["last_price"] == pytest.approx(580.0)     # but last price recorded


async def test_no_itineraries_found_is_skipped(temp_db, fake_engine):
    """The engine found nothing bookable for this favourite -- must not
    overwrite last_price with anything, and must not crash."""
    fake_engine["state"]["itineraries"] = []

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=["2026-09-01"], trip_days=0,
    )

    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)

    assert bot.messages == []
    fav = (await db_module.get_favorites())[0]
    assert fav["last_price"] == pytest.approx(600.0)  # untouched


async def test_favorite_with_no_dates_is_skipped(temp_db, fake_engine):
    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=[], trip_days=0,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    assert fake_engine["calls"] == []


async def test_engine_error_is_logged_and_does_not_abort_other_favorites(temp_db, monkeypatch):
    async def failing_run_search(**kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(scheduler_module, "run_search", failing_run_search)

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=["2026-09-01"], trip_days=0,
    )

    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)  # must not raise

    assert bot.messages == []
    fav = (await db_module.get_favorites())[0]
    assert fav["last_price"] == pytest.approx(600.0)  # untouched
