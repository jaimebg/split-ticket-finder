"""A tracked route's price history: numbers, the sparkline and all the text.

Pure. Everything is computed from ``(checked_at, price)`` pairs, oldest
first, and a ``today`` date passed in, so nothing here depends on the clock
or the database. Checks with no price (a run that found nothing) are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import config
from handlers.search.draft import Button, Rows
from handlers.utils import esc, load_json_list
from providers.base import SearchOptions

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


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _route(fav: dict, origin: str) -> str:
    return (f"{esc(fav.get('origin') or origin)} → {esc(fav['hub'])} → "
            f"{esc(fav['destination'])}")


def options_line(fav: dict) -> str:
    """Everything that changes the price besides the route, in one line."""
    opts = SearchOptions.from_mapping(fav)
    parts = [opts.party_label(), opts.currency]
    if opts.overnight:
        parts.append("🌙")
    if opts.max_stops is not None:
        parts.append("direct" if opts.max_stops == 0
                     else f"≤{opts.max_stops} stop{'s' if opts.max_stops > 1 else ''}")
    return " · ".join(parts)


def versus_average(stats: PriceStats | None) -> str | None:
    if stats is None or stats.count < 2 or stats.average <= 0:
        return None
    pct = round((stats.last - stats.average) / stats.average * 100)
    days = config.PRICE_HISTORY_DAYS
    if pct == 0:
        return f"at its {days}-day average"
    return f"{abs(pct)}% {'below' if pct < 0 else 'above'} its {days}-day average"


def history_screen(fav: dict, stats: PriceStats | None, *, origin: str) -> tuple[str, Rows]:
    trip = f"round-trip {fav['trip_days']}d" if fav.get("trip_days") else "one-way"
    dates = [str(d) for d in load_json_list(fav.get("check_dates"))]
    tracking = ", ".join(_day(d) for d in dates[:3]) + (f" (+{len(dates) - 3})" if len(dates) > 3 else "")
    cur = esc(SearchOptions.from_mapping(fav).currency)
    lines = [f"<b>{_route(fav, origin)}</b> · {trip}", esc(options_line(fav)),
             f"Tracking {tracking}" if tracking else "Tracking —", ""]
    if stats is None:
        lines.append(f"Not checked yet — the first check runs within "
                     f"{config.ALERT_INTERVAL_HOURS} hours.")
    else:
        lines.append(stats.sparkline)
        last = f"Last {stats.last:,.0f} {cur}"
        if stats.previous is not None and stats.previous != stats.last:
            delta = stats.last - stats.previous
            last += f" ({'−' if delta < 0 else '+'}{abs(delta):,.0f} since the previous check)"
        lines.append(last)
        lines.append(f"Lowest {stats.low:,.0f} · Highest {stats.high:,.0f} · "
                     f"{config.PRICE_HISTORY_DAYS}-day average {stats.average:,.0f}")
        if stats.lowest_in_days:
            lines.append(f"Lowest in {stats.lowest_in_days} days")
        lines.append(f"{stats.count} check{'s' if stats.count != 1 else ''} since "
                     f"{_day(stats.first_day)}")
    rows: Rows = [[Button("🗑 Delete", f"delfav_{fav['id']}"),
                   Button("◀ Favourites", "menu_favorites")]]
    return "\n".join(lines), rows


def alert_text(fav: dict, *, origin: str, last: float, date: str,
               record_before: float | None, record_drop: bool,
               trend: TrendSignal | None, spark: str) -> str:
    """One alert naming every reason that fired (record drop, trend, or both)."""
    cur = esc(SearchOptions.from_mapping(fav).currency)
    reasons = []
    if record_drop and record_before:
        pct = int((record_before - last) / record_before * 100)
        reasons.append(f"was {record_before:,.0f} (−{pct}%)")
    if trend is not None:
        since = f"lowest in {trend.days} days" if trend.days >= 1 else "lowest so far"
        reasons.append(f"{since}, {trend.pct_below}% below the average ({trend.average:,.0f})")
    lines = [f"📉 Price drop · {_route(fav, origin)} · {_day(date)}",
             esc(options_line(fav)),
             f"{last:,.0f} {cur} — {', '.join(reasons)}"]
    if spark:
        lines.append(spark)
    return "\n".join(lines)
