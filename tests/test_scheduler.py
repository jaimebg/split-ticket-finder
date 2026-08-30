"""Tests for the price-tracking scheduler."""
from __future__ import annotations

import pytest

import db as db_module
import scheduler as scheduler_module
from providers.google import FlightResult
from scheduler import _sample_dates, check_favorites


class FakeBot:
    """Records the alerts the scheduler tries to send."""

    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


@pytest.fixture
def fake_prices(monkeypatch):
    """Stub the scraper with a {(from, to): price} table and record queries."""
    prices: dict[tuple[str, str], int] = {}
    calls: list[tuple[str, str, str]] = []

    async def fake_search(from_apt, to_apt, date, adults=1, currency="EUR", **kwargs):
        calls.append((from_apt, to_apt, date))
        price = prices.get((from_apt, to_apt))
        if price is None:
            return []
        return [FlightResult(price=price, airlines=["Iberia"], stops=0, duration=120)]

    monkeypatch.setattr(scheduler_module, "search", fake_search)
    monkeypatch.setattr(scheduler_module, "DEFAULT_DELAY", 0)
    monkeypatch.setattr(scheduler_module, "DISCOUNT_AIRPORTS", set())
    return {"prices": prices, "calls": calls}


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


# ── The false price-drop alert ──────────────────────────────────────────────


async def test_round_trip_favorite_is_repriced_as_round_trip(temp_db, fake_prices):
    """The regression: re-pricing a round-trip favourite as one-way halved the
    total and fired a price-drop alert on every cycle."""
    fake_prices["prices"].update({
        ("LPA", "MAD"): 100,   # outbound domestic
        ("MAD", "NRT"): 500,   # outbound international
        ("NRT", "MAD"): 520,   # return international
        ("MAD", "LPA"): 110,   # return domestic
    })
    record_price = 100 + 500 + 520 + 110  # 1230, what the user was quoted

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=float(record_price), check_dates=["2026-09-01"], trip_days=14,
    )

    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)

    assert bot.messages == [], f"unexpected price-drop alert: {bot.messages}"

    # All four legs must have been queried, the return ones on the return date.
    calls = fake_prices["calls"]
    assert ("MAD", "LPA", "2026-09-15") in calls
    assert ("NRT", "MAD", "2026-09-15") in calls

    fav = (await db_module.get_favorites())[0]
    assert fav["last_price"] == pytest.approx(float(record_price))
    assert fav["record_price"] == pytest.approx(float(record_price))


async def test_one_way_favorite_queries_only_outbound_legs(temp_db, fake_prices):
    fake_prices["prices"].update({("LPA", "MAD"): 100, ("MAD", "NRT"): 500})

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=["2026-09-01"], trip_days=0,
    )

    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)

    assert bot.messages == []
    assert fake_prices["calls"] == [
        ("LPA", "MAD", "2026-09-01"),
        ("MAD", "NRT", "2026-09-01"),
    ]


async def test_genuine_price_drop_alerts_and_updates_the_record(temp_db, fake_prices):
    fake_prices["prices"].update({("LPA", "MAD"): 50, ("MAD", "NRT"): 300})

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


async def test_small_price_change_does_not_alert(temp_db, fake_prices):
    """Below the 10% threshold, the record stands and no alert is sent."""
    fake_prices["prices"].update({("LPA", "MAD"): 100, ("MAD", "NRT"): 480})

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


async def test_incomplete_itinerary_is_skipped(temp_db, fake_prices):
    """A missing leg makes the trip unbookable, so it must not produce a price."""
    fake_prices["prices"].update({("LPA", "MAD"): 100})  # no onward flight

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=["2026-09-01"], trip_days=0,
    )

    bot = FakeBot()
    await check_favorites(bot, owner_chat_id=1)

    assert bot.messages == []
    fav = (await db_module.get_favorites())[0]
    assert fav["last_price"] == pytest.approx(600.0)  # untouched


async def test_discount_is_applied_to_the_qualifying_hub(temp_db, fake_prices, monkeypatch):
    monkeypatch.setattr(scheduler_module, "DISCOUNT_AIRPORTS", {"MAD"})
    monkeypatch.setattr(scheduler_module, "DOMESTIC_DISCOUNT", 0.75)
    fake_prices["prices"].update({("LPA", "MAD"): 100, ("MAD", "NRT"): 500})

    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=None, check_dates=["2026-09-01"], trip_days=0,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)

    fav = (await db_module.get_favorites())[0]
    # 100 * (1 - 0.75) + 500 = 525
    assert fav["last_price"] == pytest.approx(525.0)


async def test_favorite_with_no_dates_is_skipped(temp_db, fake_prices):
    await db_module.add_favorite(
        origin="LPA", hub="MAD", destination="NRT", adults=1, currency="EUR",
        price=600.0, check_dates=[], trip_days=0,
    )

    await check_favorites(FakeBot(), owner_chat_id=1)
    assert fake_prices["calls"] == []
