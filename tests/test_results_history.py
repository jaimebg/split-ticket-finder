"""results.history: numbers and text from a favourite's price checks."""
from __future__ import annotations

from datetime import date

import pytest

import config
from results.history import price_stats, sparkline, trend_signal

TODAY = date(2026, 10, 31)


def _c(day: str, price):
    return (f"{day}T08:00:00Z", price)


def test_sparkline_maps_the_range_onto_eight_bars():
    assert sparkline([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█"
    assert sparkline([10, 10, 10]) == "▄▄▄"
    assert sparkline([7]) == "▄"
    assert sparkline([]) == ""


def test_stats_skip_null_prices_and_report_the_basics():
    """Review Focus #1."""
    checks = [_c("2026-10-01", 800), _c("2026-10-02", None), _c("2026-10-20", 700),
              _c("2026-10-30", 650)]
    s = price_stats(checks, today=TODAY)
    assert (s.count, s.first_day, s.last, s.previous, s.low, s.high) == \
        (3, "2026-10-01", 650, 700, 650, 800)
    assert s.sparkline == sparkline([800, 700, 650])


def test_the_average_covers_the_window_and_falls_back_to_everything(monkeypatch):
    monkeypatch.setattr(config, "PRICE_HISTORY_DAYS", 30)
    checks = [_c("2026-09-01", 1000), _c("2026-10-20", 700), _c("2026-10-30", 500)]
    assert price_stats(checks, today=TODAY).average == 600          # 1000 is outside 30 days
    old = [_c("2026-08-01", 900), _c("2026-08-02", 700)]
    assert price_stats(old, today=TODAY).average == 800             # nothing in window: all


def test_lowest_in_days_counts_back_while_earlier_prices_are_not_lower():
    checks = [_c("2026-10-01", 600), _c("2026-10-10", 900), _c("2026-10-21", 800),
              _c("2026-10-30", 700)]
    assert price_stats(checks, today=TODAY).lowest_in_days == 21   # back to 10-10, not 10-01
    assert price_stats([*checks[:1], _c("2026-10-30", 500)], today=TODAY).lowest_in_days == 30
    rising = [_c("2026-10-20", 500), _c("2026-10-30", 600)]
    assert price_stats(rising, today=TODAY).lowest_in_days is None
    assert price_stats([_c("2026-10-30", 500)], today=TODAY).lowest_in_days is None


def test_no_priced_checks_is_no_stats():
    assert price_stats([], today=TODAY) is None
    assert price_stats([_c("2026-10-01", None)], today=TODAY) is None


def test_sparkline_uses_the_last_points_only(monkeypatch):
    monkeypatch.setattr(config, "SPARK_POINTS", 3)
    checks = [_c(f"2026-10-{d:02d}", p) for d, p in ((1, 1), (2, 9), (3, 1), (4, 2), (5, 3))]
    assert price_stats(checks, today=TODAY).sparkline == sparkline([1, 2, 3])


@pytest.fixture
def five_prior():
    return [_c(f"2026-10-2{d}", p) for d, p in enumerate((800, 790, 810, 800, 805))]


def test_trend_fires_on_a_new_low_well_below_the_prior_average(five_prior):
    t = trend_signal(five_prior, 700, today=TODAY)
    assert t is not None
    assert t.average == pytest.approx(801)
    assert t.pct_below == 12
    assert t.days == 11                          # oldest prior check, 2026-10-20


def test_trend_needs_enough_history(five_prior, monkeypatch):
    monkeypatch.setattr(config, "ALERT_MIN_CHECKS", 6)
    assert trend_signal(five_prior, 700, today=TODAY) is None


def test_trend_needs_both_a_new_low_and_a_real_drop(five_prior):
    assert trend_signal(five_prior, 750, today=TODAY) is None     # new low, only 6% below
    spiky = [*five_prior, _c("2026-10-29", 690)]
    assert trend_signal(spiky, 700, today=TODAY) is None          # not a new low


def test_trend_ignores_null_prior_checks(five_prior):
    assert trend_signal([*five_prior, _c("2026-10-30", None)], 700, today=TODAY) is not None
