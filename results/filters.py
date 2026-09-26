"""Filters over an already-fetched result set. Zero new requests (spec §6.5).

**Unknown never passes.** When any filter is active, an itinerary that
filter cannot evaluate is hidden: an estimate or partial with no offer for a
leg, a connection whose times were not reported, an offer with no segment
list. The alternative would show a "direct only" list with flights nobody
checked. The view reports how many routes are hidden, so hidden never reads
as "none exist".
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from models import Itinerary
from providers.base import Offer

STOPS_CHOICES = (0, 1)
HOURS_CHOICES = (12, 18, 24)
BUFFER_CHOICES = (2, 3, 4)

# Callback key -> (field, allowed values). "x" toggles a carrier instead.
_SETTINGS = {
    "s": ("max_stops", STOPS_CHOICES),
    "h": ("max_hours", HOURS_CHOICES),
    "b": ("min_buffer_hours", BUFFER_CHOICES),
}


def _choice(value: object, allowed: tuple[int, ...]) -> int | None:
    # type() rather than isinstance(): True is an int, and must not pass as 1.
    return value if type(value) is int and value in allowed else None


@dataclass(frozen=True)
class Filters:
    max_stops: int | None = None
    max_hours: int | None = None
    min_buffer_hours: int | None = None
    exclude: frozenset[str] = frozenset()

    @property
    def active(self) -> int:
        set_values = (self.max_stops, self.max_hours, self.min_buffer_hours)
        return sum(v is not None for v in set_values) + (1 if self.exclude else 0)

    def to_dict(self) -> dict:
        return {
            "max_stops": self.max_stops, "max_hours": self.max_hours,
            "min_buffer_hours": self.min_buffer_hours,
            "exclude": sorted(self.exclude),
        }

    @classmethod
    def from_dict(cls, d: object) -> Filters:
        """Tolerant: anything a button could not have produced is dropped."""
        if not isinstance(d, dict):
            return cls()
        exclude = d.get("exclude") if isinstance(d.get("exclude"), list) else []
        return cls(
            max_stops=_choice(d.get("max_stops"), STOPS_CHOICES),
            max_hours=_choice(d.get("max_hours"), HOURS_CHOICES),
            min_buffer_hours=_choice(d.get("min_buffer_hours"), BUFFER_CHOICES),
            exclude=frozenset(c for c in exclude if isinstance(c, str)),
        )

    def with_setting(self, key: str, value: str) -> Filters:
        """Apply one filter button. ValueError for anything no button sends."""
        if key == "x":
            return replace(self, exclude=self.exclude ^ {value})
        if key not in _SETTINGS:
            raise ValueError(f"unknown filter key {key!r}")
        field_name, allowed = _SETTINGS[key]
        if value == "any":
            return replace(self, **{field_name: None})
        number = int(value)  # ValueError for a non-number, which is what we want
        if number not in allowed:
            raise ValueError(f"{value!r} is not an option for {key!r}")
        return replace(self, **{field_name: number})


def _required_offers(itin: Itinerary) -> list[Offer | None]:
    legs = [itin.dom_out, itin.onward_out]
    if itin.return_date:
        legs += [itin.onward_ret, itin.dom_ret]
    return legs


def _directions(itin: Itinerary) -> list[tuple[Offer | None, timedelta | None, Offer | None]]:
    directions = [(itin.dom_out, itin.buffer_out, itin.onward_out)]
    if itin.return_date:
        directions.append((itin.onward_ret, itin.buffer_ret, itin.dom_ret))
    return directions


def journey_minutes(first: Offer | None, buffer: timedelta | None,
                    second: Offer | None) -> int | None:
    """One direction, door to door: both tickets' own durations plus the gap.

    Each offer's ``duration`` already covers its own layovers. Only the gap
    between the two tickets is computed, and that is a same-airport
    subtraction (see ``models.ground_time``), so no cross-timezone
    arithmetic happens anywhere.
    """
    if first is None or second is None or buffer is None:
        return None
    if buffer < timedelta(0):
        # The second ticket leaves before the first lands: there is no
        # journey to time. Adding the negative gap would understate it.
        return None
    return first.duration + int(buffer.total_seconds() // 60) + second.duration


def _passes(itin: Itinerary, f: Filters) -> bool:
    if not f.active:
        return True
    offers = _required_offers(itin)
    if any(o is None for o in offers):
        return False
    if f.max_stops is not None and any(o.stops > f.max_stops for o in offers):
        return False
    if f.max_hours is not None:
        for first, buffer, second in _directions(itin):
            minutes = journey_minutes(first, buffer, second)
            if minutes is None or minutes > f.max_hours * 60:
                return False
    if f.min_buffer_hours is not None:
        needed = timedelta(hours=f.min_buffer_hours)
        for _, buffer, _ in _directions(itin):
            if buffer is None or buffer < needed:
                return False
    if f.exclude:
        for o in offers:
            if not o.segments or any(s.carrier in f.exclude for s in o.segments):
                return False
    return True


def apply(itineraries: list[Itinerary],
          filters: Filters) -> tuple[list[tuple[int, Itinerary]], int]:
    """``(index, itinerary)`` for each one shown, in input order, and how many
    were hidden. The index is into *itineraries*, never a screen position."""
    shown = [(i, it) for i, it in enumerate(itineraries) if _passes(it, filters)]
    return shown, len(itineraries) - len(shown)


def carriers(itineraries: list[Itinerary]) -> list[tuple[str, str]]:
    """Every ``(code, name)`` flown by any segment of any stored itinerary."""
    seen: dict[str, str] = {}
    for it in itineraries:
        for o in it.legs:
            for s in o.segments:
                seen.setdefault(s.carrier, s.carrier_name)
    return sorted(seen.items())
