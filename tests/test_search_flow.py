"""Tests for the guided search flow's engine-facing wiring.

``run_and_report`` (shared by the guided flow and history reruns) is where
Layer 2's review found the branch's own invariants stop being enforced: the
discrete date list the user actually asked for gets silently widened into a
window (C1). These tests fake ``engine.run_search`` (imported into
``handlers.search_flow``'s own namespace) and a Telegram bot, the same "fake
the engine call, not the provider layer" approach ``tests/test_scheduler.py``
uses for ``check_favorites`` -- ``run_and_report`` is a plain async function,
not a decorated Telegram handler, so it can be called directly with no
``Update``/``Context`` scaffolding.
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

import db as db_module
import handlers.search_flow as search_flow_module
from handlers.search_flow import run_and_report
from models import Itinerary
from providers.base import Offer


class FakeBot:
    """Records every message the handler tries to send."""

    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


def _offer(price: str) -> Offer:
    return Offer(price=Decimal(price), currency="EUR", airlines=["Iberia"],
                 stops=0, duration=120, segments=[], provider="fake")


def _itin(*, date, hub="MAD", dest="NRT", return_date="",
          dom_price="100", onward_price="500") -> Itinerary:
    return Itinerary(
        date=date, return_date=return_date, hub=hub, hub_name=hub, dest=dest,
        dest_name=dest, discount=Decimal("0"),
        dom_out=_offer(dom_price), onward_out=_offer(onward_price),
    )


@pytest.fixture
def fake_engine(monkeypatch):
    """Replace handlers.search_flow.run_search with a scripted fake."""
    calls: list[dict] = []
    state = {"itineraries": [], "parse_errors": 0, "fetch_errors": 0, "scan": None}

    async def fake_run_search(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            itineraries=state["itineraries"],
            parse_errors=state["parse_errors"],
            fetch_errors=state["fetch_errors"],
            scan=state["scan"],
        )

    monkeypatch.setattr(search_flow_module, "run_search", fake_run_search)
    return {"calls": calls, "state": state}


def _base_params(**overrides) -> dict:
    params = {
        "origin": "LPA",
        "destinations": {"NRT": "NRT"},
        "dates": ["2026-09-01", "2026-09-20"],
        "hubs": {"MAD": "Madrid"},
        "adults": 1,
        "currency": "EUR",
        "trip_days": 0,
    }
    params.update(overrides)
    return params


# ── C1: the discrete date list stays authoritative ──────────────────────────


async def test_run_and_report_forwards_the_discrete_dates_to_run_search(temp_db, fake_engine):
    """run_search needs the explicit list too (so the grid path can be told
    to search it directly instead of resampling its own from the window)."""
    params = _base_params()
    await run_and_report(FakeBot(), chat_id=1, params=params)
    assert fake_engine["calls"][0]["dates"] == params["dates"]


async def test_fixed_date_search_returns_results_only_on_requested_dates(
    temp_db, fake_engine,
):
    """The fake engine stands in for a real calendar-backed one, which prices
    every day between the user's two chosen dates for free -- exactly what
    lets phase 0 cover a whole window in one request. Only the two dates the
    user actually asked for may reach the user or storage."""
    fake_engine["state"]["itineraries"] = [
        _itin(date=d) for d in
        ["2026-09-01", "2026-09-05", "2026-09-10", "2026-09-15", "2026-09-20"]
    ]
    params = _base_params(dates=["2026-09-01", "2026-09-20"])

    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=params)

    sent_text = "\n".join(bot.messages)
    assert "2026-09-01" in sent_text
    assert "2026-09-20" in sent_text
    assert "2026-09-05" not in sent_text
    assert "2026-09-10" not in sent_text
    assert "2026-09-15" not in sent_text

    stored = (await db_module.get_searches(1))[0]
    stored_result_dates = {r["date"] for r in json.loads(stored["results"])}
    assert stored_result_dates == {"2026-09-01", "2026-09-20"}


async def test_two_date_selection_19_days_apart_does_not_return_an_in_between_date(
    temp_db, fake_engine,
):
    """The concrete scenario named in the review: 2026-09-01 and 2026-09-20,
    19 days apart. Neither a date the calendar covered "for free" nor one a
    window-based grid resample invented may surface -- and the user's own
    second choice, 2026-09-20, must not be dropped either."""
    fake_engine["state"]["itineraries"] = [
        _itin(date="2026-09-01"),
        _itin(date="2026-09-09"),  # an in-between date nobody asked for
        _itin(date="2026-09-20"),
    ]
    params = _base_params(dates=["2026-09-01", "2026-09-20"])

    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=params)

    stored = (await db_module.get_searches(1))[0]
    stored_result_dates = {r["date"] for r in json.loads(stored["results"])}
    assert stored_result_dates == {"2026-09-01", "2026-09-20"}
    assert "2026-09-09" not in stored_result_dates


async def test_a_date_the_user_asked_for_is_never_dropped_by_filtering(temp_db, fake_engine):
    """The other direction of the same invariant: every requested date the
    engine actually returned a result for must survive the filter."""
    fake_engine["state"]["itineraries"] = [
        _itin(date="2026-09-01"), _itin(date="2026-09-20"),
    ]
    params = _base_params(dates=["2026-09-01", "2026-09-20"])

    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=params)

    best_message = bot.messages[-1]
    assert "2026-09-01" in best_message or "2026-09-20" in best_message  # a "Track" offer exists
    stored = (await db_module.get_searches(1))[0]
    stored_result_dates = {r["date"] for r in json.loads(stored["results"])}
    assert stored_result_dates == {"2026-09-01", "2026-09-20"}
