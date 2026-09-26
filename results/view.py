"""Telegram screens for a search: progress, summary, detail and filters.

Pure. Every function takes stored data and view state and returns
``(text, rows)``: Telegram HTML plus button specs (``Button``, plain data).
Nothing here calls Telegram or the database. ``handlers/results.py`` does
both, so every screen is tested without a bot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from config import ORIGIN
from engine.orchestrator import CROSS_CHECK_PHASE, GRID_THROUGH_FARE_PHASE, STRATEGY_GRID
from handlers.search.draft import Button, Rows
from handlers.utils import esc, load_json_list
from models import (
    STATUS_ESTIMATE,
    STATUS_PARTIAL,
    Itinerary,
    Progress,
    fmt_dur,
    ground_time,
)
from providers.base import Offer
from results.filters import BUFFER_CHOICES, HOURS_CHOICES, Filters, apply, carriers
from results.store import StoredResults

PAGE_SIZE = 5
_CENTS = Decimal("0.01")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MARKERS = {STATUS_PARTIAL: " · partial", STATUS_ESTIMATE: " · est."}
MAX_CARRIER_BUTTONS = 16

MENU_ROWS: Rows = [[Button("🔍 New search", "menu_search"), Button("🏠 Menu", "menu_main")]]


@dataclass(frozen=True)
class SearchMeta:
    """What the screens need from the ``searches`` row besides its results."""

    search_id: int
    origin: str
    destinations: tuple[str, ...]
    currency: str
    round_trip: bool
    strategy: str | None
    sampled_dates: int
    window_days: int | None
    fallback_through_fare: Decimal | None

    @classmethod
    def from_row(cls, row: dict) -> SearchMeta:
        start, end = row.get("window_start"), row.get("window_end")
        window_days = None
        if start and end:
            span = datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")
            window_days = span.days + 1
        fare = row.get("through_fare")
        return cls(
            search_id=row["id"],
            origin=row.get("origin") or ORIGIN,
            destinations=tuple(str(d) for d in load_json_list(row.get("destinations"))),
            currency=row.get("currency") or "EUR",
            round_trip=bool(row.get("trip_days")),
            strategy=row.get("strategy"),
            sampled_dates=len(load_json_list(row.get("dates"))),
            window_days=window_days,
            fallback_through_fare=Decimal(str(fare)) if fare is not None else None,
        )


# ── Small formatters ─────────────────────────────────────────────────────────

def _money(amount: Decimal, currency: str, places: int = 0) -> str:
    return f"{amount:,.{places}f} {esc(currency)}"


def _day(date: str) -> str:
    """'2026-09-04' -> '4 Sep'. Fixed month names: never locale-dependent."""
    d = datetime.strptime(date, "%Y-%m-%d")
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _dates(itin: Itinerary) -> str:
    return f"{_day(itin.date)} → {_day(itin.return_date)}" if itin.return_date else _day(itin.date)


def _gap(delta: timedelta) -> str:
    return fmt_dur(int(delta.total_seconds() // 60))


def _fallback_fare(meta: SearchMeta, stored: StoredResults) -> Decimal | None:
    """The row-level through-fare, only for rows that stored no per-trip fare.

    ``searches.through_fare`` is the fare for the *best* itinerary's
    destination and date. A detailed (v2) itinerary carries its own fare or
    none at all; borrowing the best's would quote a saving against a fare
    never priced for that trip. Only legacy rows, which stored no per-trip
    fare, fall back to it.
    """
    return None if stored.detailed else meta.fallback_through_fare


def savings_text(itin: Itinerary, currency: str, fallback: Decimal | None) -> str | None:
    """One line comparing *itin* with the single through-ticket, or None.

    The sign guard lives here and only here. When the single ticket is
    cheaper, the line says so instead of printing a negative saving: that
    would recommend the more expensive option.
    """
    fare = itin.through_fare if itin.through_fare is not None else fallback
    if fare is None or fare <= 0:
        return None
    savings = (fare - itin.total).quantize(_CENTS)
    if savings < 0:
        return (f"The single ticket is {_money(-savings, currency, 2)} cheaper — "
                "splitting doesn't pay here.")
    if savings == 0:
        return "Same price as the single ticket — splitting gains nothing."
    pct = int(savings / fare * 100)
    return (f"Save {_money(savings, currency, 2)} ({pct}%) vs the single ticket "
            f"at {_money(fare, currency, 2)}")


# ── Summary ──────────────────────────────────────────────────────────────────

def summary(meta: SearchMeta, stored: StoredResults, filters: Filters,
            page: int) -> tuple[str, Rows]:
    active = filters if stored.detailed else Filters()
    shown, hidden = apply(stored.itineraries, active)
    shown.sort(key=lambda pair: pair[1].total)
    pages = max(1, math.ceil(len(shown) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    sid = meta.search_id

    trip = "round-trip" if meta.round_trip else "one-way"
    dests = ", ".join(esc(d) for d in meta.destinations) or "?"
    parts = [f"<b>{esc(meta.origin)} → {dests}</b> · {trip} · "
             f"{len(stored.itineraries)} routes"]
    if shown:
        best = shown[0][1]
        head = f"Best <b>{_money(best.total, meta.currency)}</b>"
        saving = savings_text(best, meta.currency, _fallback_fare(meta, stored))
        parts.append(f"{head}\n{saving}" if saving else head)
    if meta.strategy == STRATEGY_GRID and meta.window_days:
        parts.append(f"<i>Sampled {meta.sampled_dates} of {meta.window_days} days — "
                     "not every day was priced.</i>")
    if hidden:
        parts.append(f"<i>{hidden} route{'s' if hidden != 1 else ''} hidden by filters.</i>")
    if not stored.detailed:
        parts.append("<i>Historical snapshot — flight details were not stored "
                     "for this search.</i>")

    start = (page - 1) * PAGE_SIZE
    chunk = shown[start:start + PAGE_SIZE]
    if chunk:
        parts.append("\n".join(
            f"{start + n}. <b>{_money(it.total, meta.currency)}</b>  {_dates(it)}  "
            f"via {esc(it.hub)}{_MARKERS.get(it.status, '')}"
            for n, (_, it) in enumerate(chunk, 1)
        ))
    else:
        parts.append("No routes match these filters.")

    rows: Rows = []
    if chunk:
        rows.append([Button(str(start + n), f"r:{sid}:d:{i}")
                     for n, (i, _) in enumerate(chunk, 1)])
    if pages > 1:
        noop = f"r:{sid}:n"
        rows.append([
            Button("◀", f"r:{sid}:p:{page - 1}" if page > 1 else noop),
            Button(f"{page}/{pages}", noop),
            Button("▶", f"r:{sid}:p:{page + 1}" if page < pages else noop),
        ])
    bottom: list[Button] = []
    if stored.detailed:
        label = f"⚙️ Filters · {active.active}" if active.active else "⚙️ Filters"
        bottom.append(Button(label, f"r:{sid}:f"))
    rows.append(bottom + list(MENU_ROWS[0]))
    return "\n\n".join(parts), rows


# ── Detail ───────────────────────────────────────────────────────────────────

def _bags(o: Offer) -> str:
    cabin = ("cabin unknown" if o.included_cabin_bags is None
             else f"{o.included_cabin_bags} cabin")
    if o.included_checked_bags is None:
        checked = "checked unknown"
    elif o.included_checked_bags == 0 and o.checked_bag_price is not None:
        checked = f"checked +{_money(o.checked_bag_price, o.currency, 2)}"
    else:
        checked = f"{o.included_checked_bags} checked"
    return f"{cabin}, {checked}"


def _ticket(number: int, o: Offer | None, discount: Decimal, currency: str,
            *, domestic: bool) -> list[str]:
    if o is None:
        return [f"<b>Ticket {number}</b> · price from calendar — no flight chosen yet"]
    head = [f"<b>Ticket {number}</b>"]
    if o.segments:
        head.append(f"{esc(o.segments[0].origin)} → {esc(o.segments[-1].dest)}")
    if domestic and discount > 0:
        paid = (o.price * (Decimal(1) - discount)).quantize(_CENTS)
        head.append(f"{_money(paid, currency, 2)} ({_money(o.price, currency, 2)} "
                    f"before {int(discount * 100)}% discount)")
    else:
        head.append(_money(o.price, currency, 2))
    lines = [" · ".join(head)]

    for n, s in enumerate(o.segments):
        if n:
            layover = ground_time(o.segments[n - 1], s)
            lines.append(f"  layover {esc(s.origin)} {_gap(layover)}" if layover is not None
                         else f"  change at {esc(s.origin)}")
        when = ""
        if s.dep_local is not None and s.arr_local is not None:
            when = (f" · {_day(s.dep_local.strftime('%Y-%m-%d'))} "
                    f"{s.dep_local:%H:%M} → {s.arr_local:%H:%M}")
        lines.append(f"  {esc(s.flight_no)} {esc(s.carrier_name)}{when} · {fmt_dur(s.duration)}")

    stops = "direct" if o.stops == 0 else f"{o.stops} stop{'s' if o.stops > 1 else ''}"
    lines.append(f"  {stops} · {fmt_dur(o.duration)} · bags: {_bags(o)}")
    if o.booking_url:
        lines.append(f'  <a href="{esc(o.booking_url)}">Book this ticket</a>')
    return lines


def _connection(hub: str, arriving: Offer | None, buffer: timedelta | None,
                departing: Offer | None) -> str | None:
    if arriving is None or departing is None:
        return None
    if buffer is not None:
        if buffer < timedelta(0):
            return (f"⚠️ At {esc(hub)} the second ticket leaves before the first lands "
                    "— this connection is impossible.")
        return f"⏱ {_gap(buffer)} between tickets at {esc(hub)}"
    if arriving.segments and departing.segments \
            and arriving.segments[-1].dest != departing.segments[0].origin:
        return (f"⚠️ Airport change: arrive {esc(arriving.segments[-1].dest)}, "
                f"depart {esc(departing.segments[0].origin)}")
    return f"⏱ Time between tickets at {esc(hub)}: unknown"


def detail(meta: SearchMeta, stored: StoredResults, index: int,
           page: int) -> tuple[str, Rows]:
    """The detail for ``stored.itineraries[index]``. The caller validates *index*."""
    itin = stored.itineraries[index]
    cur = meta.currency
    trip = "round-trip" if itin.return_date else "one-way"
    parts = [
        f"<b>{_money(itin.total, cur, 2)}</b> · {trip}{_MARKERS.get(itin.status, '')}\n"
        f"{esc(meta.origin)} → {esc(itin.hub)} ({esc(itin.hub_name)}) → "
        f"{esc(itin.dest)} ({esc(itin.dest_name)})\n{_dates(itin)}",
    ]

    if not stored.detailed or itin.status == STATUS_ESTIMATE:
        parts.append(
            f"Domestic {_money(itin.dom_price, cur, 2)} → "
            f"{_money(itin.dom_discounted, cur, 2)} after discount · "
            f"onward {_money(itin.onward_price, cur, 2)}"
        )
    if not stored.detailed:
        parts.append("<i>Historical snapshot — flight details were not stored "
                     "for this search.</i>")
    else:
        directions = [("Outbound", 1, itin.dom_out, itin.buffer_out, itin.onward_out, True)]
        if itin.return_date:
            directions.append(("Return", 3, itin.onward_ret, itin.buffer_ret, itin.dom_ret, False))
        for label, first_no, first, buffer, second, first_is_domestic in directions:
            block = [f"<u>{label}</u>"]
            block += _ticket(first_no, first, itin.discount, cur, domestic=first_is_domestic)
            link = _connection(itin.hub, first, buffer, second)
            if link:
                block.append(link)
            block += _ticket(first_no + 1, second, itin.discount, cur,
                             domestic=not first_is_domestic)
            parts.append("\n".join(block))

    if itin.requires_bag_recheck is True:
        parts.append("⚠️ You must collect and re-check bags between tickets.")
    saving = savings_text(itin, cur, _fallback_fare(meta, stored))
    if saving:
        parts.append(saving)
    if itin.discount > 0:
        parts.append("<i>Book each ticket separately — only a separate domestic "
                     "ticket gets the discount.</i>")

    sid = meta.search_id
    rows: Rows = [
        [Button("⭐ Track this trip", f"r:{sid}:t:{index}"),
         Button("📈 Track route", f"r:{sid}:T:{index}")],
        [Button("◀ Back", f"r:{sid}:p:{page}")],
    ]
    return "\n\n".join(parts), rows


# ── Filters screen ───────────────────────────────────────────────────────────

def filters_screen(meta: SearchMeta, stored: StoredResults,
                   filters: Filters) -> tuple[str, Rows]:
    shown, _ = apply(stored.itineraries, filters)
    sid = meta.search_id
    noop = f"r:{sid}:n"
    text = (f"<b>Filters</b> — {len(shown)} of {len(stored.itineraries)} routes shown\n\n"
            "<i>A route the filters can't check (an estimate, or missing flight "
            "times) is hidden while any filter is on.</i>")

    def opt(key: str, value: int | None, current: int | None, label: str) -> Button:
        mark = "• " if current == value else ""
        return Button(f"{mark}{label}", f"r:{sid}:f:{key}:{'any' if value is None else value}")

    rows: Rows = [
        [Button("Stops per ticket", noop)],
        [opt("s", None, filters.max_stops, "Any"), opt("s", 0, filters.max_stops, "Direct"),
         opt("s", 1, filters.max_stops, "≤1 stop")],
        [Button("Max journey time (each way)", noop)],
        [opt("h", None, filters.max_hours, "Any")]
        + [opt("h", h, filters.max_hours, f"{h}h") for h in HOURS_CHOICES],
        [Button("Min time between tickets", noop)],
        [opt("b", None, filters.min_buffer_hours, "Any")]
        + [opt("b", b, filters.min_buffer_hours, f"{b}h") for b in BUFFER_CHOICES],
    ]
    codes = carriers(stored.itineraries)[:MAX_CARRIER_BUTTONS]
    if codes:
        rows.append([Button("Airlines — tap to exclude", noop)])
        buttons = [Button(f"{'✗' if code in filters.exclude else '✓'} {esc(code)}",
                          f"r:{sid}:f:x:{code}") for code, _ in codes]
        rows += [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([Button("Clear", f"r:{sid}:f:clear"), Button("◀ Results", f"r:{sid}:p:1")])
    return text, rows


# ── Progress ─────────────────────────────────────────────────────────────────

_TWO_STAGE_LABELS = {
    "Phase 0": "Scanning price calendars",
    "Phase 1": "Confirming flights",
    "Phase 2": "Pricing the single-ticket fare",
}
_GRID_LABELS = {
    "Phase 1": "Searching domestic flights",
    "Phase 1R": "Searching domestic return flights",
    "Phase 2": "Searching onward flights",
    "Phase 2R": "Searching onward return flights",
    GRID_THROUGH_FARE_PHASE: "Pricing the single-ticket fare",
}


def phase_label(phase: str, strategy: str) -> str:
    """A human step name. "Phase 1" means different work in each strategy,
    so the strategy picks the table. An unknown label passes through as-is."""
    if phase == CROSS_CHECK_PHASE:
        return "Cross-checking with a second source"
    table = _GRID_LABELS if strategy == STRATEGY_GRID else _TWO_STAGE_LABELS
    return table.get(phase, phase)


def progress_text(progress: Progress | None, strategy: str, currency: str) -> str:
    if progress is None:
        return "Starting search…"
    lines = [f"{esc(phase_label(progress.phase, strategy))}… {progress.done}/{progress.total}"]
    if progress.best_total is not None:
        confirmed = progress.best_confirmed
        lines.append(f"Best so far: {_money(progress.best_total, currency)}"
                     f"{'' if confirmed else ' (est.)'}")
    return "\n".join(lines)
