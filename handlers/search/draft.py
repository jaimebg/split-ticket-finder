"""The search draft: every field, plus which sub-screen is showing.

Spec §6.2. The old conversation was a linear chain of eleven
ConversationHandler states with no way back, so a typo at step six meant
/cancel and start over. Here the navigation state is *data* -- a field on
this frozen dataclass -- so ConversationHandler keeps a single state and
Back is a re-render rather than a transition. That is what makes "Back and
Edit exist everywhere" true by construction rather than by eleven
carefully maintained edges.

This module must not import telegram, directly or transitively. That is
what lets its tests run with no Update/Context scaffolding, and it is why
render() returns button tuples rather than an InlineKeyboardMarkup and
takes the query estimate as an argument rather than computing it (
_estimate_queries lives in handlers/search_flow.py, which does import
telegram).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import NamedTuple

from handlers.utils import esc
from models import SearchWindow

# ── Screens ──────────────────────────────────────────────────────────────────

SCREEN_DRAFT = "draft"
SCREEN_DEST = "dest"
SCREEN_HUBS = "hubs"
SCREEN_DATES = "dates"
SCREEN_TRIP = "trip"

# ── Date modes (spec §6.4) ───────────────────────────────────────────────────

MODE_WINDOW = "window"      # tap-start / tap-end; the engine prices every day
MODE_DAYS = "days"          # multi-select; the exact days the user asked for

# ── What typed text is for, when a screen is waiting for some ────────────────

AWAIT_DEST = "dest"
AWAIT_HUBS = "hubs"
AWAIT_TRIP_DAYS = "trip_days"

MAX_DESTINATIONS = 10
MAX_TRIP_DAYS = 180

# How many hub codes to name before collapsing the rest into "+N".
_HUBS_SHOWN = 3


class Button(NamedTuple):
    """One inline keyboard button, as data rather than a telegram object."""

    label: str
    data: str


Rows = list[list[Button]]


@dataclass(frozen=True)
class SearchDraft:
    """One in-progress search. Immutable; every change returns a copy.

    ``trip_days`` is ``int | None`` because one-way is 0, which is also the
    natural "unset" default -- ``None`` means "not chosen yet" so ``missing``
    can tell them apart. ``to_params`` collapses ``None`` to 0, which is
    what ``run_and_report`` means by one-way.

    ``destinations`` and ``hubs`` are tuples of ``(code, name)`` pairs
    rather than dicts so the dataclass stays hashable and genuinely frozen;
    ``to_params`` hands ``dict(...)`` to the engine, the shape it wants.
    """

    origin: str
    origin_name: str
    destinations: tuple[tuple[str, str], ...] = ()
    hubs: tuple[tuple[str, str], ...] = ()
    trip_days: int | None = None
    date_mode: str = MODE_WINDOW
    window_start: str | None = None
    window_end: str | None = None
    picked_days: tuple[str, ...] = ()
    adults: int = 1
    currency: str = "EUR"
    screen: str = SCREEN_DRAFT
    awaiting: str | None = None

    # ── Copying ──────────────────────────────────────────────────────────

    def with_(self, **changes) -> SearchDraft:
        """A copy with *changes* applied -- the Itinerary.with_* idiom."""
        return dataclasses.replace(self, **changes)

    # ── Derived views ────────────────────────────────────────────────────

    @property
    def dest_codes(self) -> tuple[str, ...]:
        return tuple(code for code, _ in self.destinations)

    @property
    def hub_codes(self) -> tuple[str, ...]:
        return tuple(code for code, _ in self.hubs)

    @property
    def has_dates(self) -> bool:
        if self.date_mode == MODE_DAYS:
            return bool(self.picked_days)
        return bool(self.window_start and self.window_end)

    @property
    def effective_dates(self) -> list[str]:
        """The date list handed to the engine.

        In days mode this is exactly what the user picked, which is what
        keeps run_and_report's C1 filter load-bearing. In window mode it is
        the full expanded span, so min()/max() still reconstruct the window
        and history_rerun needs no change (design doc §7).
        """
        if self.date_mode == MODE_DAYS:
            return sorted(self.picked_days)
        if not (self.window_start and self.window_end):
            return []
        return SearchWindow(start=self.window_start, end=self.window_end).dates()

    @property
    def missing(self) -> tuple[str, ...]:
        """Human labels for the fields still blocking a search."""
        gaps = []
        if not self.destinations:
            gaps.append("destination")
        if self.trip_days is None:
            gaps.append("trip type")
        if not self.has_dates:
            gaps.append("dates")
        if not self.hubs:
            gaps.append("hubs")
        return tuple(gaps)

    @property
    def is_ready(self) -> bool:
        return not self.missing

    # ── Handing off to the engine ────────────────────────────────────────

    def to_params(self) -> dict:
        """The exact dict run_and_report takes.

        These keys are that function's contract, not this class's
        preference -- Layer 3a deliberately changes no persisted shape, so
        history reruns and the scheduler keep working unexamined.
        """
        return {
            "origin": self.origin,
            "destinations": dict(self.destinations),
            "dates": self.effective_dates,
            "hubs": dict(self.hubs),
            "adults": self.adults,
            "currency": self.currency,
            "trip_days": self.trip_days or 0,
        }

    # ── Rendering ────────────────────────────────────────────────────────

    def _trip_line(self) -> str:
        if self.trip_days is None:
            return "<i>not set</i>"
        if self.trip_days == 0:
            return "One-way"
        return f"Round-trip · {self.trip_days} days"

    def _dates_line(self) -> str:
        if not self.has_dates:
            return "<i>not set</i>"
        if self.date_mode == MODE_DAYS:
            n = len(self.picked_days)
            return f"{n} day{'s' if n != 1 else ''} picked"
        return f"{self.window_start} → {self.window_end}"

    def _places_line(self, pairs: tuple[tuple[str, str], ...]) -> str:
        if not pairs:
            return "<i>not set</i>"
        codes = [code for code, _ in pairs]
        if len(codes) == 1:
            return f"{esc(codes[0])} · {esc(pairs[0][1])}"
        shown = " ".join(esc(c) for c in codes[:_HUBS_SHOWN])
        extra = len(codes) - _HUBS_SHOWN
        return f"{shown} +{extra}" if extra > 0 else shown

    def render(self, estimate: int | None = None) -> tuple[str, Rows]:
        """The draft panel: (Telegram HTML, button rows).

        Returns button *tuples* rather than an InlineKeyboardMarkup so this
        module stays telegram-free; builder.py converts.
        """
        lines = [
            "🔎 <b>Search draft</b>",
            "",
            f"<b>From</b>   {esc(self.origin)} · {esc(self.origin_name)}",
            f"<b>To</b>     {self._places_line(self.destinations)}",
            f"<b>Trip</b>   {self._trip_line()}",
            f"<b>Dates</b>  {self._dates_line()}",
            f"<b>Hubs</b>   {self._places_line(self.hubs)}",
            "",
            f"<i>{self.adults} adult{'s' if self.adults != 1 else ''} "
            f"· Economy · {esc(self.currency)}</i>",
        ]

        if self.missing:
            lines += ["", f"Still needed: <b>{', '.join(self.missing)}</b>"]
        elif estimate is not None:
            lines += ["", f"Up to <b>{estimate}</b> requests"]

        rows: Rows = [
            [Button("✏️ To", "edit:dest"), Button("✏️ Trip", "edit:trip")],
            [Button("✏️ Dates", "edit:dates"), Button("✏️ Hubs", "edit:hubs")],
            [Button("🔍 Search", "go"), Button("♻️ Reset", "reset")],
            [Button("⬅️ Menu", "menu_main")],
        ]
        return "\n".join(lines), rows
