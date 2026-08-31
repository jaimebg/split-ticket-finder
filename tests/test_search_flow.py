"""Tests for the guided search flow's engine-facing wiring.

``run_and_report`` (shared by the guided flow and history reruns) is where
Layer 2's review found the branch's own invariants stop being enforced: the
discrete date list the user actually asked for gets silently widened into a
window (C1), and error counters are collected and thrown away (C2). These
tests fake ``engine.run_search`` (imported into ``handlers.search_flow``'s
own namespace) and a Telegram bot, the same "fake the engine call, not the
provider layer" approach ``tests/test_scheduler.py`` uses for
``check_favorites`` -- ``run_and_report`` is a plain async function, not a
decorated Telegram handler, so it can be called directly with no
``Update``/``Context`` scaffolding.

``_oversized_window_message`` and ``_estimate_queries`` (I6, I4) are tested
directly as the pure functions they are.
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

import db as db_module
import handlers.search_flow as search_flow_module
from config import FALLBACK_MAX_DATES, MAX_WINDOW_DAYS, SHORTLIST_SIZE, THROUGH_FARE_DATES
from handlers.search_flow import _oversized_window_message, run_and_report
from models import Itinerary
from providers.base import Offer


class FakeBot:
    """Records every message the handler tries to send."""

    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


class _FakeCalendarProvider:
    """Enough of SupportsCalendar for isinstance() -- never actually called."""

    name = "fake-cal"

    async def price_calendar(self, query):
        raise AssertionError("not called by these tests")

    async def search_leg(self, query):
        raise AssertionError("not called by these tests")

    async def aclose(self):
        return None


class _FakeNoCalendarProvider:
    """No price_calendar -- fails isinstance(_, SupportsCalendar)."""

    name = "fake-nocal"

    async def search_leg(self, query):
        raise AssertionError("not called by these tests")

    async def aclose(self):
        return None


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


# ── C2: broken must not look like empty ──────────────────────────────────────


async def test_a_provider_erroring_on_every_request_says_results_may_be_incomplete(
    temp_db, fake_engine,
):
    """When Kiwi 403s every calendar request, scan_calendars counts fetch
    errors and returns an empty grid -- the user must not be told the bare
    "<b>No routes found.</b>" that reads as a confirmed empty search."""
    fake_engine["state"]["itineraries"] = []
    fake_engine["state"]["parse_errors"] = 0
    fake_engine["state"]["fetch_errors"] = 32
    params = _base_params()

    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=params)

    sent_text = "\n".join(bot.messages)
    assert "No routes found" in sent_text  # still says this...
    assert "incomplete" in sent_text        # ...but qualified
    assert "32" in sent_text


async def test_a_clean_search_with_no_errors_says_nothing_extra(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(date="2026-09-01")]
    fake_engine["state"]["parse_errors"] = 0
    fake_engine["state"]["fetch_errors"] = 0
    params = _base_params(dates=["2026-09-01"])

    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=params)

    sent_text = "\n".join(bot.messages)
    assert "incomplete" not in sent_text


# ── I4: the pre-flight query estimate ────────────────────────────────────────


def test_estimate_queries_two_stage_matches_the_documented_formula(monkeypatch):
    monkeypatch.setattr(
        search_flow_module, "primary_provider", lambda: _FakeCalendarProvider(),
    )

    n = search_flow_module._estimate_queries(hubs=8, dests=3, dates=12, round_trip=True)

    phase0 = 8 * (1 + 3) * 2
    phase1 = SHORTLIST_SIZE * 4
    phase2 = THROUGH_FARE_DATES * 3
    assert n == phase0 + phase1 + phase2


def test_estimate_queries_two_stage_is_close_to_the_real_measured_count(monkeypatch):
    """README's measured end-to-end count for 8 hubs x 3 destinations x
    91 days, round-trip, is 190 requests. The old formula quoted 768 for the
    same inputs -- 4x too high, and inverted the branch's own headline
    claim. The new estimate must land within a small margin of the real
    figure, not the old grid-shaped one."""
    monkeypatch.setattr(
        search_flow_module, "primary_provider", lambda: _FakeCalendarProvider(),
    )

    n = search_flow_module._estimate_queries(hubs=8, dests=3, dates=14, round_trip=True)

    assert n < 250          # nowhere near the old formula's 768
    assert abs(n - 190) < 50  # in the neighbourhood of the real measured count


def test_estimate_queries_grid_uses_the_sampled_date_count_not_the_raw_one(monkeypatch):
    monkeypatch.setattr(
        search_flow_module, "primary_provider", lambda: _FakeNoCalendarProvider(),
    )

    n = search_flow_module._estimate_queries(hubs=8, dests=3, dates=50, round_trip=False)

    assert n == 8 * FALLBACK_MAX_DATES * (1 + 3)


def test_estimate_queries_grid_matches_the_old_formula_when_dates_fit_under_the_cap(
    monkeypatch,
):
    monkeypatch.setattr(
        search_flow_module, "primary_provider", lambda: _FakeNoCalendarProvider(),
    )

    n = search_flow_module._estimate_queries(hubs=8, dests=3, dates=5, round_trip=True)

    assert n == 8 * 5 * (1 + 3) * 2


# ── I6: an oversized date span is rejected before "Ready?", and again if it
# somehow still reaches run_search ──────────────────────────────────────────


def test_oversized_window_message_names_the_limit():
    msg = _oversized_window_message("2026-01-01", "2026-06-01")  # 152 days
    assert msg is not None
    assert str(MAX_WINDOW_DAYS) in msg


def test_window_within_the_limit_has_no_message():
    assert _oversized_window_message("2026-01-01", "2026-01-10") is None


def test_window_exactly_at_the_limit_has_no_message():
    from models import SearchWindow, add_days
    start = "2026-01-01"
    end = add_days(start, MAX_WINDOW_DAYS - 1)
    assert SearchWindow(start=start, end=end).days == MAX_WINDOW_DAYS
    assert _oversized_window_message(start, end) is None


async def test_run_and_report_surfaces_a_valueerrors_message_instead_of_the_generic_one(
    temp_db, monkeypatch,
):
    """A history rerun of a search saved before I6's early validation existed
    can still reach run_search with an oversized window. run_search's own
    ValueError message is written for a human -- it must reach the user
    verbatim, not the generic "check the bot logs" message."""
    human_message = (
        "window 2026-01-01 to 2026-06-01 covers 152 days, more than the "
        "91-day limit the price calendar supports (MAX_WINDOW_DAYS)"
    )

    async def raising_run_search(**kwargs):
        raise ValueError(human_message)

    monkeypatch.setattr(search_flow_module, "run_search", raising_run_search)
    bot = FakeBot()
    params = _base_params(dates=["2026-01-01", "2026-06-01"])

    await run_and_report(bot, chat_id=1, params=params)

    assert len(bot.messages) == 1
    assert "91-day limit" in bot.messages[0]
    assert "check the bot logs" not in bot.messages[0]


async def test_run_and_report_still_uses_the_generic_message_for_other_exceptions(
    temp_db, monkeypatch,
):
    """The distinct ValueError handling must not swallow real failures --
    anything else still gets the generic, log-pointing message."""
    async def raising_run_search(**kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(search_flow_module, "run_search", raising_run_search)
    bot = FakeBot()
    params = _base_params()

    await run_and_report(bot, chat_id=1, params=params)

    assert len(bot.messages) == 1
    assert "check the bot logs" in bot.messages[0]
