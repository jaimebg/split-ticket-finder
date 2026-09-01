"""Tests for the month grid (spec §6.4).

The grid is a pure function, so every rule that matters -- no past days,
the MAX_WINDOW_DAYS ceiling refused at the tap rather than at Ready, and
the two selection modes -- is testable with no bot. Like draft.py this
module imports no telegram.
"""
from __future__ import annotations

from config import MAX_WINDOW_DAYS
from handlers.search.dates import (
    RATING_MARKS,
    apply_day_tap,
    apply_preset,
    caption,
    month_rows,
    shift_month,
)
from handlers.search.draft import MODE_DAYS, MODE_WINDOW, SearchDraft

TODAY = "2026-10-15"


def _draft(**kw) -> SearchDraft:
    base = {"origin": "LPA", "origin_name": "Gran Canaria"}
    base.update(kw)
    return SearchDraft(**base)


def _labels(rows) -> list[str]:
    return [b.label for row in rows for b in row]


def _data(rows) -> list[str]:
    return [b.data for row in rows for b in row]


# ── Grid shape ───────────────────────────────────────────────────────────────


def test_grid_lays_the_month_out_seven_wide():
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    day_rows = [r for r in rows if any(b.data.startswith("d:") for b in r)]

    assert day_rows
    assert all(len(r) == 7 for r in day_rows)


def test_grid_covers_every_day_of_the_month():
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    days = [b.data for b in [b for row in rows for b in row]
            if b.data.startswith("d:")]

    assert "d:2026-10-31" in days
    assert "d:2026-11-01" not in days


def test_grid_offers_month_navigation():
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    assert "m:2026-09" in _data(rows)
    assert "m:2026-11" in _data(rows)


# ── Past days ────────────────────────────────────────────────────────────────


def test_past_days_are_not_tappable():
    """A search for yesterday is not a search. The cell stays in place so
    the month keeps its shape, but it carries no date callback."""
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY)
    days = [b.data for b in [b for row in rows for b in row]
            if b.data.startswith("d:")]

    assert "d:2026-10-14" not in days
    assert "d:2026-10-15" in days      # today itself is fair game


# ── Window mode ──────────────────────────────────────────────────────────────


def test_first_tap_sets_the_window_start():
    d, alert = apply_day_tap(_draft(), "2026-10-20", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-10-20", None)
    assert alert is None


def test_second_tap_sets_the_window_end():
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    d, alert = apply_day_tap(d, "2026-10-25", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-10-20", "2026-10-25")
    assert alert is None


def test_tapping_before_the_start_moves_the_start_rather_than_inverting():
    """An end before its start is not a window. Treating the earlier tap as
    a new start is what the user meant, and it beats an error."""
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    d, alert = apply_day_tap(d, "2026-10-16", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-10-16", None)
    assert alert is None


def test_tapping_a_third_time_starts_a_new_window():
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    d, _ = apply_day_tap(d, "2026-10-25", today=TODAY)
    d, _ = apply_day_tap(d, "2026-11-02", today=TODAY)

    assert (d.window_start, d.window_end) == ("2026-11-02", None)


def test_an_oversized_window_is_refused_at_the_tap():
    """Review finding I6 moved this check off the Ready screen. Here it
    moves onto the tap itself: the user learns the limit while looking at
    the calendar, not after building a whole search."""
    d, _ = apply_day_tap(_draft(), "2026-10-20", today=TODAY)
    too_far = "2027-06-01"

    after, alert = apply_day_tap(d, too_far, today=TODAY)

    assert after.window_end is None            # unchanged
    assert after.window_start == "2026-10-20"
    assert alert is not None
    assert str(MAX_WINDOW_DAYS) in alert


def test_a_window_exactly_at_the_limit_is_accepted():
    """Off-by-one guard: MAX_WINDOW_DAYS counts both endpoints, so a window
    of exactly that many days is legal and one day more is not. Getting
    this wrong costs the user a day of the window with no explanation."""
    from models import add_days

    start = "2026-10-20"
    d, _ = apply_day_tap(_draft(), start, today=TODAY)
    at_limit = add_days(start, MAX_WINDOW_DAYS - 1)

    after, alert = apply_day_tap(d, at_limit, today=TODAY)

    assert alert is None
    assert after.window_end == at_limit


def test_a_window_one_day_over_the_limit_is_refused():
    from models import add_days

    start = "2026-10-20"
    d, _ = apply_day_tap(_draft(), start, today=TODAY)
    over_limit = add_days(start, MAX_WINDOW_DAYS)

    after, alert = apply_day_tap(d, over_limit, today=TODAY)

    assert after.window_end is None
    assert alert is not None


# ── Days mode ────────────────────────────────────────────────────────────────


def test_days_mode_toggles_a_day_on_and_off():
    d = _draft(date_mode=MODE_DAYS)

    d, _ = apply_day_tap(d, "2026-10-20", today=TODAY)
    assert d.picked_days == ("2026-10-20",)

    d, _ = apply_day_tap(d, "2026-10-20", today=TODAY)
    assert d.picked_days == ()


def test_days_mode_keeps_picks_sorted():
    d = _draft(date_mode=MODE_DAYS)
    for day in ("2026-10-25", "2026-10-20", "2026-10-22"):
        d, _ = apply_day_tap(d, day, today=TODAY)

    assert d.picked_days == ("2026-10-20", "2026-10-22", "2026-10-25")


def test_days_mode_refuses_a_span_over_the_limit():
    """The engine's window ceiling applies to the picked days' span too --
    run_and_report derives a SearchWindow from min() and max()."""
    d = _draft(date_mode=MODE_DAYS, picked_days=("2026-10-20",))

    after, alert = apply_day_tap(d, "2027-06-01", today=TODAY)

    assert after.picked_days == ("2026-10-20",)
    assert alert is not None


# ── Presets ──────────────────────────────────────────────────────────────────


def test_next_30_sets_a_window_starting_today():
    d = apply_preset(_draft(), "30", today=TODAY)
    assert d.window_start == TODAY
    assert d.window_end == "2026-11-13"     # today + 29, inclusive of both


def test_next_90_stays_within_the_engine_limit():
    d = apply_preset(_draft(), "90", today=TODAY)
    from models import SearchWindow
    assert SearchWindow(start=d.window_start, end=d.window_end).days <= MAX_WINDOW_DAYS


def test_this_month_runs_from_today_to_the_month_end():
    d = apply_preset(_draft(), "month", today=TODAY)
    assert d.window_start == TODAY
    assert d.window_end == "2026-10-31"


def test_a_preset_switches_back_to_window_mode():
    """A preset is a window by definition; leaving the draft in days mode
    would show a window the search would then ignore."""
    d = apply_preset(_draft(date_mode=MODE_DAYS, picked_days=("2026-10-20",)),
                     "30", today=TODAY)
    assert d.date_mode == MODE_WINDOW


# ── Mode switching ───────────────────────────────────────────────────────────


def test_switching_modes_clears_the_other_modes_selection():
    """A three-day pick is not a window, and guessing which was meant is
    worse than asking again."""
    d = _draft(window_start="2026-10-01", window_end="2026-10-10")
    rows = month_rows(2026, 10, draft=d, today=TODAY)

    assert "dm:days" in _data(rows)


# ── Ratings (spec §6.4) ──────────────────────────────────────────────────────


def test_rated_days_carry_their_mark():
    ratings = {"2026-10-20": "CHEAP", "2026-10-21": "EXPENSIVE"}
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY, ratings=ratings)
    labels = _labels(rows)

    assert any(lbl.startswith(RATING_MARKS["CHEAP"]) and "20" in lbl
               for lbl in labels)
    assert any(lbl.startswith(RATING_MARKS["EXPENSIVE"]) and "21" in lbl
               for lbl in labels)


def test_an_unrated_grid_renders_plain_numbers():
    """§6.4: with no destination set the grid renders uncoloured rather
    than guessing. A ProviderError takes the same path."""
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY, ratings=None)
    labels = [lbl for lbl in _labels(rows) if lbl.strip().isdigit()]

    assert "20" in [lbl.strip() for lbl in labels]


def test_unknown_rating_is_treated_as_unrated():
    """RatedPrice.rating can be UNKNOWN; that must not render as a mark
    the legend does not explain."""
    rows = month_rows(2026, 10, draft=_draft(), today=TODAY,
                      ratings={"2026-10-20": "UNKNOWN"})
    labels = [b.label for row in rows for b in row if b.data == "d:2026-10-20"]

    assert labels == ["20"]


# ── Caption ──────────────────────────────────────────────────────────────────


def test_caption_names_the_destination_and_calls_it_a_direct_fare_signal():
    """§6.4: the picker's colours are the through-fare shape, not the
    split. Saying so is the whole point of the label."""
    text = caption(_draft(), dest_code="NRT")

    assert "NRT" in text
    assert "direct-fare signal" in text
    assert "saving" not in text.lower()


def test_caption_without_a_destination_does_not_claim_a_signal():
    text = caption(_draft(), dest_code=None)
    assert "direct-fare signal" not in text


# ── Month arithmetic ─────────────────────────────────────────────────────────


def test_shift_month_wraps_the_year():
    assert shift_month(2026, 12, 1) == (2027, 1)
    assert shift_month(2026, 1, -1) == (2025, 12)
