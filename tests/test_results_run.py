"""Tests for the guided search flow's engine-facing wiring.

``run_and_report`` (shared by the guided flow and history reruns) is where
Layer 2's review found the branch's own invariants stop being enforced: the
discrete date list the user actually asked for gets silently widened into a
window (C1), and error counters are collected and thrown away (C2). These
tests fake ``engine.run_search`` (imported into ``handlers.results``'s
own namespace) and a Telegram bot, the same "fake the engine call, not the
provider layer" approach ``tests/test_scheduler.py`` uses for
``check_favorites`` -- ``run_and_report`` is a plain async function, not a
decorated Telegram handler, so it can be called directly with no
``Update``/``Context`` scaffolding.

``_oversized_window_message`` and ``_estimate_queries`` (I6, I4) are tested
directly as the pure functions they are.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest

import db as db_module
import handlers.results as results_module
from config import FALLBACK_MAX_DATES, MAX_WINDOW_DAYS, SHORTLIST_SIZE, THROUGH_FARE_DATES
from handlers.results import _oversized_window_message, run_and_report
from models import Itinerary
from providers.base import Offer
from results.store import load


class FakeBot:
    """Records every text the handler shows, edits and sends alike, in order."""

    def __init__(self, *, edit_errors=None):
        self.messages: list[str] = []
        self.markups: list = []
        self.edit_errors = list(edit_errors or [])
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, **kwargs):
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))


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
    """Replace handlers.results.run_search with a scripted fake."""
    calls: list[dict] = []
    state = {"itineraries": [], "parse_errors": 0, "fetch_errors": 0, "scan": None}

    async def fake_run_search(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            itineraries=state["itineraries"],
            parse_errors=state["parse_errors"],
            fetch_errors=state["fetch_errors"],
            scan=state["scan"],
            strategy="two-stage",
        )

    monkeypatch.setattr(results_module, "run_search", fake_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
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
    assert "1 Sep" in sent_text and "20 Sep" in sent_text
    for absent in ("5 Sep", "10 Sep", "15 Sep"):
        assert not re.search(rf"(?<!\d){absent}", sent_text)

    stored = (await db_module.get_searches(1))[0]
    stored_result_dates = {it.date for it in load(stored["results"]).itineraries}
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
    stored_result_dates = {it.date for it in load(stored["results"]).itineraries}
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

    assert "2 routes" in bot.messages[-1]
    stored = (await db_module.get_searches(1))[0]
    stored_result_dates = {it.date for it in load(stored["results"]).itineraries}
    assert stored_result_dates == {"2026-09-01", "2026-09-20"}


# ── C2: broken must not look like empty ──────────────────────────────────────


async def test_a_total_failure_gets_a_distinct_message_not_a_false_empty_headline(
    temp_db, fake_engine,
):
    """When Kiwi 403s every calendar request, scan_calendars counts fetch
    errors and returns an empty grid. The bold "<b>No routes found.</b>"
    headline must not appear at all here, even with a corrective note in
    italics underneath -- that states a false fact first, in the part a
    skimming Telegram user actually reads, with the correction relegated to
    fine print. That is the same broken-looks-like-empty failure C2 exists
    to prevent, just softened, and a test asserting "No routes found" is
    present would teach the next person that ordering is fine. The absence
    assertion below is what stops that regression, not the wording alone."""
    fake_engine["state"]["itineraries"] = []
    fake_engine["state"]["parse_errors"] = 0
    fake_engine["state"]["fetch_errors"] = 32
    params = _base_params()

    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=params)

    sent_text = "\n".join(bot.messages)
    assert "No routes found" not in sent_text  # the load-bearing assertion
    assert "Search incomplete" in sent_text
    assert "32" in sent_text


async def test_a_partial_result_still_shows_results_with_the_note_appended_below(
    temp_db, fake_engine,
):
    """A partial result (some itineraries survived alongside some errors) is
    genuinely different from a total failure: there are real results to
    show, so format_results still runs and the caveat is appended below
    them, where "the results above" is an accurate description -- this
    behaviour must stay exactly as it was."""
    fake_engine["state"]["itineraries"] = [_itin(date="2026-09-01")]
    fake_engine["state"]["parse_errors"] = 1
    fake_engine["state"]["fetch_errors"] = 0
    params = _base_params(dates=["2026-09-01"])

    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=params)

    sent_text = "\n".join(bot.messages)
    assert "1 Sep" in sent_text  # the result is still shown
    assert "incomplete" in sent_text
    assert "Search incomplete" not in sent_text  # that headline is total-failure only


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
        results_module, "primary_provider", lambda: _FakeCalendarProvider(),
    )

    n = results_module._estimate_queries(hubs=8, dests=3, dates=12, round_trip=True)

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
        results_module, "primary_provider", lambda: _FakeCalendarProvider(),
    )

    n = results_module._estimate_queries(hubs=8, dests=3, dates=14, round_trip=True)

    assert n < 250          # nowhere near the old formula's 768
    assert abs(n - 190) < 50  # in the neighbourhood of the real measured count


def test_estimate_queries_grid_uses_the_sampled_date_count_not_the_raw_one(monkeypatch):
    monkeypatch.setattr(
        results_module, "primary_provider", lambda: _FakeNoCalendarProvider(),
    )

    n = results_module._estimate_queries(hubs=8, dests=3, dates=50, round_trip=False)

    assert n == 8 * FALLBACK_MAX_DATES * (1 + 3)


def test_estimate_queries_grid_matches_the_old_formula_when_dates_fit_under_the_cap(
    monkeypatch,
):
    monkeypatch.setattr(
        results_module, "primary_provider", lambda: _FakeNoCalendarProvider(),
    )

    n = results_module._estimate_queries(hubs=8, dests=3, dates=5, round_trip=True)

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

    monkeypatch.setattr(results_module, "run_search", raising_run_search)
    bot = FakeBot()
    params = _base_params(dates=["2026-01-01", "2026-06-01"])

    await run_and_report(bot, chat_id=1, params=params)

    assert len(bot.messages) == 2
    assert "91-day limit" in bot.messages[-1]
    assert "check the bot logs" not in bot.messages[-1]


async def test_run_and_report_still_uses_the_generic_message_for_other_exceptions(
    temp_db, monkeypatch,
):
    """The distinct ValueError handling must not swallow real failures --
    anything else still gets the generic, log-pointing message."""
    async def raising_run_search(**kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(results_module, "run_search", raising_run_search)
    bot = FakeBot()
    params = _base_params()

    await run_and_report(bot, chat_id=1, params=params)

    assert len(bot.messages) == 2
    assert "check the bot logs" in bot.messages[-1]

from models import CancelToken, Progress


async def test_the_search_shows_a_cancel_button_while_it_runs(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(date="2026-09-01")]
    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=_base_params(dates=["2026-09-01"]))

    assert bot.messages[0] == "Starting search…"
    first = bot.markups[0].inline_keyboard[0][0]
    assert first.callback_data.startswith("x:")


async def test_results_replace_the_progress_message_in_place(temp_db, fake_engine):
    fake_engine["state"]["itineraries"] = [_itin(date="2026-09-01")]
    bot = FakeBot()
    await run_and_report(bot, chat_id=1, params=_base_params(dates=["2026-09-01"]),
                         message_id=42)

    assert bot.messages[0] == "Starting search…"
    assert "1 routes" in bot.messages[-1]
    buttons = [b.callback_data for row in bot.markups[-1].inline_keyboard for b in row]
    assert any(d.startswith("r:") and ":d:0" in d for d in buttons)


async def test_cancel_saves_nothing_and_says_so(temp_db, monkeypatch):
    async def cancelling_run_search(**kwargs):
        kwargs["cancel"].cancel()
        kwargs["cancel"].raise_if_cancelled()

    monkeypatch.setattr(results_module, "run_search", cancelling_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
    runs: dict[str, CancelToken] = {}
    bot = FakeBot()

    await run_and_report(bot, chat_id=1, params=_base_params(), runs=runs)

    assert bot.messages[-1] == "Search cancelled."
    assert await db_module.get_searches(1) == []
    assert runs == {}, "a finished run must leave the registry"


async def test_progress_ticks_reach_the_message(temp_db, monkeypatch):
    async def ticking_run_search(**kwargs):
        kwargs["on_progress"](Progress(phase="Phase 1", done=3, total=10,
                                       best_total=Decimal("612"), best_confirmed=True))
        await asyncio.sleep(0.05)
        return SimpleNamespace(itineraries=[], parse_errors=0, fetch_errors=0,
                               scan=None, strategy="two-stage")

    monkeypatch.setattr(results_module, "run_search", ticking_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
    bot = FakeBot()

    await run_and_report(bot, chat_id=1, params=_base_params(), interval=0.01)

    assert "Confirming flights… 3/10\nBest so far: 612 EUR" in bot.messages


async def test_a_failed_progress_edit_does_not_stop_the_search(temp_db, monkeypatch):
    """Review Focus #5."""
    from telegram.error import NetworkError

    async def ticking_run_search(**kwargs):
        kwargs["on_progress"](Progress(phase="Phase 0", done=1, total=4))
        await asyncio.sleep(0.05)
        return SimpleNamespace(itineraries=[_itin(date="2026-09-01")], parse_errors=0,
                               fetch_errors=0, scan=None, strategy="two-stage")

    monkeypatch.setattr(results_module, "run_search", ticking_run_search)
    monkeypatch.setattr(results_module, "primary_provider", lambda: _FakeCalendarProvider())
    bot = FakeBot(edit_errors=[NetworkError("flaky")])

    await run_and_report(bot, chat_id=1, params=_base_params(dates=["2026-09-01"]),
                         message_id=42, interval=0.01)

    assert "1 routes" in bot.messages[-1]
