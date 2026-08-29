"""Domain types shared across providers and the search engine.

These describe the search domain, not any one data source. They lived in
scraper.py until the provider layer made that placement wrong: a Kiwi client
should not have to import a Route from the Google module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class Route:
    date: str
    hub: str
    hub_name: str
    dest: str
    dest_name: str
    dom_price: int
    dom_discounted: float
    intl_price: int
    total: float
    return_date: str = ""
    dom_airlines: list[str] = field(default_factory=list)
    dom_stops: int = 0
    dom_dur: int = 0
    intl_airlines: list[str] = field(default_factory=list)
    intl_stops: int = 0
    intl_dur: int = 0


def fmt_dur(m):
    if m <= 0:
        return "?"
    h, r = divmod(m, 60)
    return f"{h}h{r:02d}m"


def generate_dates(start, end, every):
    """Generate dates from start to end, every N days."""
    dates = []
    cur = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    while cur <= stop:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=every)
    return dates


def add_days(date, days):
    """Return *date* shifted by *days*, both as "YYYY-MM-DD" strings."""
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
