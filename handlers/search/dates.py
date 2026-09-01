"""The month grid (spec §6.4).

In the two-stage model the user chooses a *window*, not individual dates,
so window mode is the default: tap a start, tap an end. But "I can only fly
the 3rd, the 10th or the 17th" is a real search the bot handled before this
rewrite, and a window-only picker would drop it *and* turn
run_and_report's C1 date filter into dead code that a later reader deletes
without knowing what it guarded. A Pick-days toggle switches the same grid
to multi-select.

Mode lives in the draft, so toggling is a re-render. Switching modes clears
the other mode's selection rather than translating it -- a three-day pick
is not a window, and guessing which was meant is worse than asking again.

Like draft.py, this module imports no telegram.
"""
from __future__ import annotations

import calendar
from datetime import datetime

from config import MAX_WINDOW_DAYS
from handlers.search.draft import MODE_DAYS, MODE_WINDOW, Button, Rows, SearchDraft
from models import SearchWindow, add_days

_ISO = "%Y-%m-%d"

# §6.4's price ratings. A provider's UNKNOWN deliberately has no mark: a
# symbol the legend does not explain is worse than a plain number.
RATING_MARKS = {
    "CHEAP": "🟢",
    "AVERAGE": "🟡",
    "EXPENSIVE": "🔴",
}

_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")

_WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

# A Telegram inline keyboard needs a callback for every button. Cells that
# are not tappable get this, and the router answers it silently.
NOOP = "noop"


# ── Month arithmetic ─────────────────────────────────────────────────────────

def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """The month *delta* months from (year, month), wrapping the year."""
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _span_days(a: str, b: str) -> int:
    """Inclusive day count between two ISO dates, in either order."""
    lo, hi = sorted((a, b))
    return SearchWindow(start=lo, end=hi).days


def _too_long(a: str, b: str) -> str | None:
    """An alert string if a..b exceeds the engine's ceiling, else None."""
    span = _span_days(a, b)
    if span > MAX_WINDOW_DAYS:
        return (f"That would be {span} days. The engine covers at most "
                f"{MAX_WINDOW_DAYS} in one search — pick a closer date.")
    return None


# ── Selection ────────────────────────────────────────────────────────────────

def apply_day_tap(
    draft: SearchDraft, date: str, *, today: str
) -> tuple[SearchDraft, str | None]:
    """Apply a tap on *date*. Returns the new draft and an alert, if refused.

    An alert means nothing changed: the caller shows it and re-renders the
    unchanged grid. Refusing at the tap rather than at Ready is the point --
    review finding I6 moved this check off the summary screen, and here it
    moves onto the calendar the user is already looking at.
    """
    if date < today:
        return draft, None

    if draft.date_mode == MODE_DAYS:
        picked = set(draft.picked_days)
        if date in picked:
            picked.discard(date)
            return draft.with_(picked_days=tuple(sorted(picked))), None
        if picked:
            alert = _too_long(min(min(picked), date), max(max(picked), date))
            if alert:
                return draft, alert
        picked.add(date)
        return draft.with_(picked_days=tuple(sorted(picked))), None

    start, end = draft.window_start, draft.window_end

    if start is None or end is not None:
        # No window yet, or a complete one: this tap starts a new window.
        return draft.with_(window_start=date, window_end=None), None

    if date < start:
        # An end before its start is not a window; the earlier tap is a new
        # start, which is what the user meant and beats an error.
        return draft.with_(window_start=date, window_end=None), None

    alert = _too_long(start, date)
    if alert:
        return draft, alert
    return draft.with_(window_end=date), None


def apply_preset(draft: SearchDraft, preset: str, *, today: str) -> SearchDraft:
    """Apply "30", "90" or "month". Always leaves the draft in window mode."""
    if preset == "month":
        first = datetime.strptime(today, _ISO)
        last_day = calendar.monthrange(first.year, first.month)[1]
        end = first.replace(day=last_day).strftime(_ISO)
    else:
        # Inclusive of both endpoints, so "next 30" is today plus 29.
        end = add_days(today, min(int(preset), MAX_WINDOW_DAYS) - 1)

    return draft.with_(
        date_mode=MODE_WINDOW,
        window_start=today,
        window_end=end,
        picked_days=(),
    )


def switch_mode(draft: SearchDraft, mode: str) -> SearchDraft:
    """Switch selection mode, clearing the other mode's selection."""
    if mode == MODE_DAYS:
        return draft.with_(date_mode=MODE_DAYS, window_start=None,
                           window_end=None)
    return draft.with_(date_mode=MODE_WINDOW, picked_days=())


# ── Rendering ────────────────────────────────────────────────────────────────

def _cell_label(
    date: str, day: int, draft: SearchDraft, ratings: dict[str, str] | None
) -> str:
    """The label for one tappable day."""
    if draft.date_mode == MODE_DAYS:
        if date in draft.picked_days:
            return f"✓{day}"
    else:
        start, end = draft.window_start, draft.window_end
        if date in (start, end):
            return f"[{day}]"
        if start and end and start < date < end:
            return f"·{day}"

    mark = (ratings or {}).get(date)
    if mark in RATING_MARKS:
        return f"{RATING_MARKS[mark]}{day}"
    return str(day)


def month_rows(
    year: int,
    month: int,
    *,
    draft: SearchDraft,
    today: str,
    ratings: dict[str, str] | None = None,
) -> Rows:
    """The full picker keyboard for one month.

    *ratings* maps an ISO date to a RatedPrice.rating string. None renders
    an uncoloured grid -- §6.4's behaviour when no destination is set, and
    also what a ProviderError falls back to. A decoration must not break
    the picker.
    """
    prev_y, prev_m = shift_month(year, month, -1)
    next_y, next_m = shift_month(year, month, 1)
    rows: Rows = [
        [Button("◀", f"m:{prev_y:04d}-{prev_m:02d}"),
         Button(f"{_MONTH_NAMES[month - 1]} {year}", NOOP),
         Button("▶", f"m:{next_y:04d}-{next_m:02d}")],
        [Button(d, NOOP) for d in _WEEKDAYS],
    ]

    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row: list[Button] = []
        for day in week:
            if day == 0:
                row.append(Button(" ", NOOP))
                continue
            date = f"{year:04d}-{month:02d}-{day:02d}"
            if date < today:
                row.append(Button("·", NOOP))
                continue
            row.append(Button(_cell_label(date, day, draft, ratings), f"d:{date}"))
        rows.append(row)

    rows.append([
        Button("Next 30", "dp:30"),
        Button("Next 90", "dp:90"),
        Button("This month", "dp:month"),
    ])

    toggle = (Button("Pick a range", f"dm:{MODE_WINDOW}")
              if draft.date_mode == MODE_DAYS
              else Button("Pick days", f"dm:{MODE_DAYS}"))
    rows.append([toggle, Button("Clear", "dclear"), Button("Done", "back")])

    return rows


def caption(draft: SearchDraft, dest_code: str | None = None) -> str:
    """The text above the grid.

    §6.4 requires the colours be labelled a *direct-fare signal* and the
    destination they came from be named: they are the through-fare shape
    for one destination, not the split, and not a saving. The results
    screen's strip is the version worth acting on.
    """
    if draft.date_mode == MODE_DAYS:
        n = len(draft.picked_days)
        state = f"{n} day{'s' if n != 1 else ''} selected" if n else "Tap the days you can fly."
    elif draft.window_start and draft.window_end:
        span = _span_days(draft.window_start, draft.window_end)
        state = f"{draft.window_start} → {draft.window_end} · {span} days"
    elif draft.window_start:
        state = f"{draft.window_start} → … tap the end date"
    else:
        state = "Tap a start date."

    lines = ["<b>When?</b>", state]
    if dest_code:
        lines.append(
            f"<i>Colours: direct-fare signal · {draft.origin}→{dest_code}. "
            "Not the split price.</i>"
        )
    return "\n".join(lines)
