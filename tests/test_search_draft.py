"""Tests for the search draft — the pure core of the hub-and-spoke builder.

SearchDraft holds every field *and* which sub-screen is showing, which is
what lets ConversationHandler keep a single state and makes Back a
re-render rather than a transition. This module imports no telegram, so
none of these tests need Update/Context scaffolding -- the same reason
tests/test_search_flow.py tests run_and_report and not the handlers.
"""
from __future__ import annotations

import pytest

from handlers.search.draft import (
    MODE_DAYS,
    MODE_WINDOW,
    SCREEN_DATES,
    SCREEN_DRAFT,
    SearchDraft,
)


def _draft(**kw) -> SearchDraft:
    base = {"origin": "LPA", "origin_name": "Gran Canaria"}
    base.update(kw)
    return SearchDraft(**base)


def _ready() -> SearchDraft:
    return _draft(
        destinations=(("NRT", "Tokyo Narita"),),
        hubs=(("MAD", "Madrid"),),
        trip_days=14,
        window_start="2026-10-01",
        window_end="2026-10-10",
    )


# ── missing / is_ready ───────────────────────────────────────────────────────


def test_a_fresh_draft_is_missing_everything_but_the_origin():
    assert _draft().missing == ("destination", "trip type", "dates", "hubs")


def test_a_complete_draft_is_ready():
    assert _ready().missing == ()
    assert _ready().is_ready


def test_one_way_is_chosen_not_merely_unset():
    """trip_days=0 is one-way; None is 'not asked yet'. Collapsing them
    would let a draft reach Search having never shown the trip screen."""
    assert "trip type" in _draft(trip_days=None).missing
    assert "trip type" not in _draft(trip_days=0).missing


def test_days_mode_needs_picked_days_not_a_window():
    d = _ready().with_(date_mode=MODE_DAYS, picked_days=())
    assert "dates" in d.missing

    d = d.with_(picked_days=("2026-10-03",))
    assert "dates" not in d.missing


# ── effective_dates ──────────────────────────────────────────────────────────


def test_window_mode_expands_to_every_day_inclusive():
    d = _draft(window_start="2026-10-01", window_end="2026-10-04")
    assert d.effective_dates == ["2026-10-01", "2026-10-02",
                                 "2026-10-03", "2026-10-04"]


def test_days_mode_uses_the_picked_list_sorted():
    d = _draft(date_mode=MODE_DAYS,
               picked_days=("2026-10-09", "2026-10-01", "2026-10-05"))
    assert d.effective_dates == ["2026-10-01", "2026-10-05", "2026-10-09"]


# ── to_params ────────────────────────────────────────────────────────────────


def test_to_params_matches_what_run_and_report_takes():
    """The keys are run_and_report's contract, not this class's preference.
    Changing one here silently changes what gets searched and stored."""
    params = _ready().to_params()

    assert set(params) == {"origin", "destinations", "dates", "hubs",
                           "adults", "currency", "trip_days"}
    assert params["origin"] == "LPA"
    assert params["destinations"] == {"NRT": "Tokyo Narita"}
    assert params["hubs"] == {"MAD": "Madrid"}
    assert params["trip_days"] == 14
    assert params["dates"][0] == "2026-10-01"
    assert params["dates"][-1] == "2026-10-10"


def test_to_params_reports_an_unchosen_trip_as_one_way():
    """run_and_report has no concept of 'not chosen'; 0 is its one-way."""
    assert _ready().with_(trip_days=None).to_params()["trip_days"] == 0


def test_to_params_carries_the_picked_days_in_days_mode():
    """This is what keeps run_and_report's C1 date filter load-bearing."""
    d = _ready().with_(date_mode=MODE_DAYS,
                       picked_days=("2026-10-03", "2026-10-11"))
    assert d.to_params()["dates"] == ["2026-10-03", "2026-10-11"]


# ── with_ ────────────────────────────────────────────────────────────────────


def test_with_returns_a_copy_and_leaves_the_original_alone():
    original = _draft()
    changed = original.with_(screen=SCREEN_DATES)

    assert changed.screen == SCREEN_DATES
    assert original.screen == SCREEN_DRAFT


# ── render ───────────────────────────────────────────────────────────────────


def test_render_escapes_place_names():
    """Provider text reaches Telegram HTML. An unescaped '<' makes Telegram
    reject the whole message; the Layer 2 carry-forward flags these
    interpolations as safe only by luck."""
    d = _ready().with_(destinations=(("XXX", "A<b>&B"),))
    text, _ = d.render()

    assert "A&lt;b&gt;&amp;B" in text
    assert "<b>&B" not in text


def test_render_shows_the_estimate_when_given_one():
    text, _ = _ready().render(estimate=170)
    assert "170" in text


def test_render_omits_the_estimate_line_when_not_ready():
    """A query count for an incomplete draft would be a made-up number."""
    text, _ = _draft().render(estimate=None)
    assert "requests" not in text


def test_render_names_every_missing_field():
    text, _ = _draft().render()
    for label in ("destination", "trip type", "dates", "hubs"):
        assert label in text


def test_render_always_offers_search_even_when_incomplete():
    """The button is always present; builder.py answers a premature tap
    with an alert naming what is missing. Telegram has no disabled button,
    and hiding it leaves the user with no visible way forward."""
    _, rows = _draft().render()
    data = [b.data for row in rows for b in row]
    assert "go" in data


def test_render_offers_an_edit_for_every_field():
    _, rows = _ready().render()
    data = [b.data for row in rows for b in row]
    for target in ("edit:dest", "edit:trip", "edit:dates", "edit:hubs"):
        assert target in data


def test_render_shows_the_read_only_passenger_footer():
    """Layer 3c makes this editable. Until then it is shown without an Edit
    affordance rather than hidden: the user should know what is being
    searched, and a dead button is worse than no button."""
    text, rows = _ready().render()
    data = [b.data for row in rows for b in row]

    assert "1 adult" in text and "Economy" in text and "EUR" in text
    assert not any(d.startswith("edit:who") for d in data)


def test_render_summarizes_long_hub_lists():
    d = _ready().with_(hubs=tuple((c, c) for c in
                                  ("MAD", "BCN", "LIS", "AGP", "SVQ", "VLC")))
    text, _ = d.render()
    assert "+3" in text


@pytest.mark.parametrize("mode,expected", [
    (MODE_WINDOW, "2026-10-01 → 2026-10-10"),
    (MODE_DAYS, "2 days"),
])
def test_render_describes_each_date_mode_in_its_own_terms(mode, expected):
    d = _ready().with_(date_mode=mode,
                       picked_days=("2026-10-03", "2026-10-11"))
    text, _ = d.render()
    assert expected in text


def test_the_draft_and_dates_modules_stay_telegram_free():
    """The boundary that lets these two modules be tested without a bot.

    Run in a subprocess because the parent pytest process has already
    imported telegram via other test modules, so an in-process
    sys.modules check would always pass and assert nothing.
    """
    import subprocess
    import sys
    code = (
        "import sys, handlers.search.draft, handlers.search.dates; "
        "bad = sorted(m for m in sys.modules if m.startswith('telegram')); "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
