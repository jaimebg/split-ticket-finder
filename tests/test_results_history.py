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


from results.history import alert_text, history_screen, options_line, versus_average

FAV = {"id": 7, "origin": "LPA", "hub": "MAD", "destination": "NRT", "trip_days": 14,
       "adults": 2, "children": 0, "cabin": "BUSINESS", "currency": "EUR",
       "max_stops": 1, "min_layover": None, "overnight": 1,
       "check_dates": '["2026-10-01", "2026-10-03", "2026-10-08", "2026-10-10", "2026-10-12"]'}


def test_options_line_names_everything_that_changes_the_price():
    assert options_line(FAV) == "2 adults · Business · EUR · 🌙 · ≤1 stop"
    plain = {"adults": 1, "currency": "USD"}
    assert options_line(plain) == "1 adult · Economy · USD"


def test_versus_average():
    s = price_stats([_c("2026-10-20", 800), _c("2026-10-30", 704)], today=TODAY)  # avg 752
    assert versus_average(s) == "6% below its 30-day average"
    up = price_stats([_c("2026-10-20", 500), _c("2026-10-30", 600)], today=TODAY)
    assert versus_average(up) == "9% above its 30-day average"
    assert versus_average(price_stats([_c("2026-10-30", 600)], today=TODAY)) is None
    assert versus_average(None) is None


def test_the_history_screen():
    checks = [_c("2026-10-02", 790), _c("2026-10-19", 636), _c("2026-10-30", 612)]
    text, rows = history_screen(FAV, price_stats(checks, today=TODAY), origin="LPA")
    assert "LPA → MAD → NRT" in text and "round-trip 14d" in text
    assert "2 adults · Business · EUR · 🌙 · ≤1 stop" in text
    assert "Tracking 1 Oct, 3 Oct, 8 Oct (+2)" in text
    assert "Last 612 EUR (−24 since the previous check)" in text
    assert "Lowest 612 · Highest 790" in text
    assert "3 checks since 2 Oct" in text
    assert [b.data for row in rows for b in row] == ["delfav_7", "menu_favorites"]


def test_the_history_screen_before_any_check(monkeypatch):
    """Review Focus #4 (the never-checked half)."""
    monkeypatch.setattr(config, "ALERT_INTERVAL_HOURS", 6)
    text, _ = history_screen(FAV, None, origin="LPA")
    assert "Not checked yet — the first check runs within 6 hours." in text


def test_hostile_codes_are_escaped():
    """Review Focus #5."""
    bad = {**FAV, "hub": "<b>", "destination": "&x"}
    text, _ = history_screen(bad, None, origin="LPA")
    assert "<b>" not in text.replace("<b>LPA", "") and "&lt;b&gt;" in text
    msg = alert_text(bad, origin="LPA", last=612, date="2026-10-01", record_before=700,
                     record_drop=True, trend=None, spark="▃▁")
    assert "&lt;b&gt;" in msg and "&amp;x" in msg


def test_alert_text_for_each_trigger():
    """Review Focus #2 and #3."""
    from results.history import TrendSignal
    trend = TrendSignal(days=30, average=748, pct_below=18)
    both = alert_text(FAV, origin="LPA", last=612, date="2026-10-01", record_before=700,
                      record_drop=True, trend=trend, spark="▃▄▂▁")
    assert both.startswith("📉 Price drop · LPA → MAD → NRT · 1 Oct")
    assert "612 EUR — was 700 (−12%), lowest in 30 days, 18% below the average (748)" in both
    assert "2 adults · Business · EUR · 🌙 · ≤1 stop" in both and "▃▄▂▁" in both
    same_day = TrendSignal(days=0, average=748, pct_below=18)
    only_trend = alert_text(FAV, origin="LPA", last=612, date="2026-10-01", record_before=None,
                            record_drop=False, trend=same_day, spark="")
    assert "612 EUR — lowest so far, 18% below the average (748)" in only_trend


def test_trend_fires_on_the_crossing_not_on_every_cent_after_it(five_prior):
    """Once the previous check already sat low against the trend, a further
    cent off is not news: alert on the crossing, not on each new decimal."""
    after_alert = [*five_prior, _c("2026-10-30", 700)]
    assert trend_signal(after_alert, 699.99, today=TODAY) is None
    after_null = [*after_alert, _c("2026-10-30", None)]
    assert trend_signal(after_null, 699.99, today=TODAY) is None
