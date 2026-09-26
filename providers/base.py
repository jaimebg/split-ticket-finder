"""Provider-agnostic types, capability protocols and errors.

A provider is anything that can price a leg. Sources differ in what they can
answer -- Google Flights has no price-calendar and no place search, Kiwi has
both -- so capabilities are separate protocols rather than one interface full
of supports_x() flags. The engine asks isinstance(p, SupportsCalendar) and
chooses a search strategy from the answer.
"""
from __future__ import annotations

import dataclasses
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

    requires_bag_recheck is True when at least one connection forces the
    traveller to re-claim and re-check bags. It is meaningful only when
    stops > 0, and None when the provider cannot say.

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
    requires_bag_recheck: bool | None = None


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


# ── Search options (Layer 3c) ────────────────────────────────────────────────

ALL_CABINS = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST_CLASS")
CABIN_LABELS = {
    "ECONOMY": "Economy",
    "PREMIUM_ECONOMY": "Premium economy",
    "BUSINESS": "Business",
    "FIRST_CLASS": "First",
}


@dataclass(frozen=True)
class SearchOptions:
    """What the user chose beyond route and dates, carried end to end.

    Every LegQuery and CalendarQuery the engine builds comes from here, so a
    choice can't reach one leg and miss another. The defaults are the search
    every caller ran before Layer 3c.

    ``min_layover`` is a connection *inside* one ticket (the provider's
    stopover), not the self-transfer gap between the two tickets.
    """

    adults: int = 1
    children: int = 0
    cabin: str = "ECONOMY"
    currency: str = "EUR"
    max_stops: int | None = None
    min_layover: int | None = None      # minutes

    @property
    def passengers(self) -> int:
        return self.adults + self.children

    @property
    def is_default_party(self) -> bool:
        return (self.adults, self.children, self.cabin) == (1, 0, "ECONOMY")

    def leg_query(self, origin: str, dest: str, date: str) -> LegQuery:
        return LegQuery(
            origin=origin, dest=dest, date=date, adults=self.adults,
            children=self.children, cabin=self.cabin, currency=self.currency,
            max_stops=self.max_stops, min_layover=self.min_layover,
        )

    def calendar_query(self, origin: str, dest: str, start: str, end: str) -> CalendarQuery:
        return CalendarQuery(
            origin=origin, dest=dest, start=start, end=end, adults=self.adults,
            children=self.children, cabin=self.cabin, currency=self.currency,
        )

    def without_limits(self) -> SearchOptions:
        """Same party, cabin and currency; no stop or layover limits."""
        return dataclasses.replace(self, max_stops=None, min_layover=None)

    def as_columns(self) -> dict:
        """The six fields under the column names ``searches`` and ``favorites`` use."""
        return {
            "adults": self.adults, "children": self.children, "cabin": self.cabin,
            "currency": self.currency, "max_stops": self.max_stops,
            "min_layover": self.min_layover,
        }

    @classmethod
    def from_mapping(cls, m) -> SearchOptions:
        """From a params dict or a database row. Missing or NULL is the default,
        so a row written before Layer 3c replays as the search it was."""
        def get(key, default):
            value = m.get(key)
            return default if value is None else value

        return cls(
            adults=get("adults", 1), children=get("children", 0),
            cabin=get("cabin", "ECONOMY"), currency=get("currency", "EUR"),
            max_stops=m.get("max_stops"), min_layover=m.get("min_layover"),
        )

    def party_label(self) -> str:
        people = f"{self.adults} adult{'s' if self.adults != 1 else ''}"
        if self.children:
            people += f", {self.children} child{'ren' if self.children != 1 else ''}"
        return f"{people} · {CABIN_LABELS.get(self.cabin, self.cabin)}"


@dataclass(frozen=True)
class Capabilities:
    """Which SearchOptions a provider can actually search."""

    cabins: frozenset[str]
    children: bool
    min_layover: bool

    def rejects(self, options: SearchOptions) -> str | None:
        """A sentence saying why this provider can't run *options*, or None."""
        if options.cabin not in self.cabins:
            label = CABIN_LABELS.get(options.cabin, options.cabin)
            return f"Your flight source can't search {label} class."
        if options.children and not self.children:
            return "Your flight source can't search with children."
        if options.min_layover is not None and not self.min_layover:
            return "Your flight source can't apply a minimum layover."
        return None


_UNRESTRICTED = Capabilities(cabins=frozenset(ALL_CABINS), children=True, min_layover=True)


def capabilities_of(provider: object) -> Capabilities:
    """The provider's declared capabilities. A provider that declares none is
    assumed capable; if it isn't, it raises ProviderError itself, which the
    caller also handles."""
    return getattr(provider, "capabilities", _UNRESTRICTED)


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
