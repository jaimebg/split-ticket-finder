"""Provider-agnostic types, capability protocols and errors.

A provider is anything that can price a leg. Sources differ in what they can
answer -- Google Flights has no price-calendar and no place search, Kiwi has
both -- so capabilities are separate protocols rather than one interface full
of supports_x() flags. The engine asks isinstance(p, SupportsCalendar) and
chooses a search strategy from the answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

# ── Errors ───────────────────────────────────────────────────────────────────


class ProviderError(RuntimeError):
    """Base class for every provider failure."""


class ProviderFetchError(ProviderError):
    """The request failed after exhausting its retry budget."""


class ProviderParseError(ProviderError):
    """A response arrived but could not be understood.

    This is the important distinction in the whole layer: a schema change, a
    consent wall or a rejected partner key must never look like "this route has
    no flights", which is an empty list. Collapsing the two makes a broken
    provider indistinguishable from an unpopular route.
    """


# ── Value objects ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Segment:
    """One flight between two airports.

    dep_local/arr_local are Optional because providers differ in what they
    report: Kiwi gives full local timestamps, Google gives bare clock times
    that have to be reconstructed against the query date.

    Both are timezone-naive local times at their own airport, not a shared
    clock. Differencing dep_local/arr_local across two segments of a connection
    is meaningless -- it silently mixes two timezones. Layover length must come
    from the provider's own layover data, never be computed from these fields.
    """

    origin: str
    dest: str
    carrier: str                        # IATA carrier code, e.g. "FR"
    carrier_name: str
    flight_no: str                      # e.g. "FR2012"
    duration: int                       # minutes
    dep_local: datetime | None = None
    arr_local: datetime | None = None


@dataclass(frozen=True)
class Offer:
    """One bookable itinerary for a single leg.

    Every Optional field means "this provider cannot tell you", never zero.
    A Google-sourced Offer has included_checked_bags is None; a formatter must
    render that as "unknown" rather than "no bag included".

    min_layover is meaningful only when stops > 0; a direct flight has no
    connection to measure.

    frozen=True only guards attribute reassignment -- it does not make the
    dataclass hashable (list fields are unhashable) and it does not stop
    in-place mutation of the airlines/segments lists themselves. Offer cannot
    go in a set or be a dict key; deduping needs a derived key (e.g. a tuple of
    the fields that matter), not the Offer itself.
    """

    price: Decimal
    currency: str
    airlines: list[str]
    stops: int
    duration: int                       # minutes
    segments: list[Segment]
    provider: str
    booking_url: str | None = None
    included_cabin_bags: int | None = None
    included_checked_bags: int | None = None
    checked_bag_price: Decimal | None = None
    min_layover: int | None = None      # minutes
    pnr_count: int | None = None


@dataclass(frozen=True)
class LegQuery:
    """One origin->dest search on one date."""

    origin: str                         # IATA
    dest: str                           # IATA
    date: str                           # YYYY-MM-DD
    adults: int = 1
    children: int = 0
    cabin: str = "ECONOMY"
    currency: str = "EUR"
    limit: int = 5
    max_stops: int | None = None
    min_layover: int | None = None      # minutes
    exclude_carriers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CalendarQuery:
    """Cheapest price per day across a date window."""

    origin: str
    dest: str
    start: str                          # YYYY-MM-DD
    end: str                            # YYYY-MM-DD
    adults: int = 1
    children: int = 0
    cabin: str = "ECONOMY"
    currency: str = "EUR"


@dataclass(frozen=True)
class RatedPrice:
    """A calendar day's cheapest price, with the source's own cheap/expensive call."""

    price: Decimal
    rating: str                         # CHEAP | AVERAGE | EXPENSIVE | UNKNOWN


@dataclass(frozen=True)
class Place:
    """An airport resolved from free text."""

    code: str                           # IATA
    name: str
    city: str
    country: str
    place_id: str                       # provider-native id


# ── Protocols ────────────────────────────────────────────────────────────────
#
# Only the capability protocols are runtime_checkable, and they are
# methods-only on purpose: isinstance() against a Protocol carrying non-method
# members is not supported across all versions we target. FlightProvider keeps
# its `name` attribute and is used for typing only, never isinstance.


class FlightProvider(Protocol):
    """The one capability every provider must have."""

    name: str

    async def search_leg(self, query: LegQuery) -> list[Offer]:
        """Return offers for one leg, cheapest first.

        An empty list means the route genuinely has no flights. Anything
        wrong raises ProviderError.
        """
        ...

    async def aclose(self) -> None:
        """Release any held connection pool."""
        ...


@runtime_checkable
class SupportsCalendar(Protocol):
    """Can price a whole date window far more cheaply than day-by-day."""

    async def price_calendar(self, query: CalendarQuery) -> dict[str, RatedPrice]:
        """Map "YYYY-MM-DD" -> cheapest price. Missing days simply have no key."""
        ...


@runtime_checkable
class SupportsPlaces(Protocol):
    """Can turn free text into airports."""

    async def resolve_place(self, term: str, limit: int = 8) -> list[Place]:
        ...
