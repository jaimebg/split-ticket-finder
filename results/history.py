"""A tracked route's price history: numbers, the sparkline and all the text.

Pure. Everything is computed from ``(checked_at, price)`` pairs, oldest
first, and a ``today`` date passed in, so nothing here depends on the clock
or the database. Checks with no price (a run that found nothing) are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import config

_BARS = "▁▂▃▄▅▆▇█"


def sparkline(prices: list[float]) -> str:
    """Each price mapped linearly onto eight bars; a flat series sits mid-height."""
    if not prices:
        return ""
    low, high = min(prices), max(prices)
    if high == low:
        return _BARS[3] * len(prices)
    scale = (len(_BARS) - 1) / (high - low)
    return "".join(_BARS[round((p - low) * scale)] for p in prices)


def _priced(checks: list[tuple[str, float | None]]) -> list[tuple[date, float]]:
    return [(date.fromisoformat(ts[:10]), float(p)) for ts, p in checks if p is not None]


def _window(points: list[tuple[date, float]], today: date) -> list[tuple[date, float]]:
    recent = [(d, p) for d, p in points if (today - d).days <= config.PRICE_HISTORY_DAYS]
    return recent or points


@dataclass(frozen=True)
class PriceStats:
    count: int
    first_day: str
    last: float
    previous: float | None
    low: float
    high: float
    average: float
    lowest_in_days: int | None
    sparkline: str


def price_stats(checks: list[tuple[str, float | None]], *, today: date) -> PriceStats | None:
    points = _priced(checks)
    if not points:
        return None
    prices = [p for _, p in points]
    last = prices[-1]
    window = _window(points, today)

    lowest_in_days = None
    if len(points) >= 2 and prices[-2] >= last:
        i = len(points) - 2
        while i > 0 and prices[i - 1] >= last:
            i -= 1
        lowest_in_days = (today - points[i][0]).days

    return PriceStats(
        count=len(points),
        first_day=points[0][0].isoformat(),
        last=last,
        previous=prices[-2] if len(prices) >= 2 else None,
        low=min(prices),
        high=max(prices),
        average=sum(p for _, p in window) / len(window),
        lowest_in_days=lowest_in_days,
        sparkline=sparkline(prices[-config.SPARK_POINTS:]),
    )


@dataclass(frozen=True)
class TrendSignal:
    days: int            # how far back the new low reaches, in days
    average: float       # of the earlier checks in the window
    pct_below: int       # how far below that average, in whole percent


def trend_signal(prior: list[tuple[str, float | None]], last: float, *,
                 today: date) -> TrendSignal | None:
    """A 'low against the trend' alert, or None.

    Fires only with at least ALERT_MIN_CHECKS earlier priced checks, when
    *last* is strictly below every earlier price in the window and at least
    PRICE_DROP_THRESHOLD below their average. The average is over the
    earlier checks only, so the new low doesn't lower its own baseline.
    """
    points = _priced(prior)
    if len(points) < config.ALERT_MIN_CHECKS:
        return None
    window = _window(points, today)
    prices = [p for _, p in window]
    average = sum(prices) / len(prices)
    if not (last < min(prices) and last <= average * (1 - config.PRICE_DROP_THRESHOLD)):
        return None
    return TrendSignal(days=(today - window[0][0]).days, average=average,
                       pct_below=int((average - last) / average * 100))
