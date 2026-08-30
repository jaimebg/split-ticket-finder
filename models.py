"""Domain types shared across providers and the search engine.

These describe the search domain, not any one data source. They lived in
scraper.py until the provider layer made that placement wrong: a Kiwi client
should not have to import a Route from the Google module.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from providers.base import Offer

# Money is rounded to cents only where a value is derived; never at input.
_CENTS = Decimal("0.01")


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


class SearchCancelled(RuntimeError):
    """Raised when a running search is cancelled by the user."""


class CancelToken:
    """A one-way flag the engine polls between requests.

    Cancellation has to be cooperative: a search is hundreds of awaits deep in
    a semaphore, and tearing that down mid-flight would leak connections. The
    engine checks this between legs instead.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise SearchCancelled("search cancelled")


@dataclass(frozen=True)
class Progress:
    """One progress tick, emitted as a phase advances."""

    phase: str
    done: int
    total: int
    best_total: Decimal | None = None

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0


ProgressCallback = Callable[[Progress], None]


@dataclass(frozen=True)
class SearchWindow:
    """An inclusive range of departure dates.

    The window replaces the old sampled date list: with a price calendar the
    cost of covering a range no longer scales with its length.
    """

    start: str                          # YYYY-MM-DD
    end: str                            # YYYY-MM-DD

    def dates(self) -> list[str]:
        first = datetime.strptime(self.start, "%Y-%m-%d")
        last = datetime.strptime(self.end, "%Y-%m-%d")
        if last < first:
            raise ValueError(f"window end {self.end} is before start {self.start}")
        span = (last - first).days
        return [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span + 1)]

    @property
    def days(self) -> int:
        return len(self.dates())


@dataclass(frozen=True)
class Candidate:
    """A (hub, destination, date) combination priced from calendars alone.

    These are cached cheapest-of-day figures. They rank the search space; they
    are never bookable, and phase 1 has to confirm one before it can be shown
    as a price the user could pay.
    """

    date: str
    return_date: str                    # "" for one-way
    hub: str
    dest: str
    dom_price: Decimal                  # undiscounted, both directions if round-trip
    onward_price: Decimal
    discount: Decimal                   # fraction taken off the domestic leg

    @property
    def dom_discounted(self) -> Decimal:
        return (self.dom_price * (Decimal(1) - self.discount)).quantize(_CENTS)

    @property
    def total(self) -> Decimal:
        return self.dom_discounted + self.onward_price


# Itinerary.status: confirmed alone cannot tell a display layer whether an
# itinerary has no real data at all or merely incomplete real data -- both
# report confirmed=False. These three values disambiguate that for callers,
# who should compare against the name rather than a bare string literal.
STATUS_ESTIMATE = "estimate"    # no real offer anywhere; every figure is a calendar guess
STATUS_CONFIRMED = "confirmed"  # every leg the trip shape needs is a real offer
STATUS_PARTIAL = "partial"      # some real offers, but not a complete itinerary


@dataclass(frozen=True)
class Itinerary:
    """A split-ticket itinerary: a discounted leg plus an onward leg.

    Composes Offers rather than flattening them, so every field Layer 1 already
    parses -- exact times, flight numbers, baggage, booking links, layovers --
    stays reachable without this type growing a copy of each.

    ``confirmed`` is the load-bearing flag. False means the prices came from
    phase 0's calendars and are estimates; the offers are None and no booking
    link exists. Every surface showing an unconfirmed itinerary must say so.
    """

    date: str
    return_date: str                    # "" for one-way
    hub: str
    hub_name: str
    dest: str
    dest_name: str
    discount: Decimal
    dom_out: Offer | None = None
    dom_ret: Offer | None = None
    onward_out: Offer | None = None
    onward_ret: Offer | None = None
    # Populated only for an unconfirmed itinerary, where there are no offers.
    est_dom_price: Decimal | None = None
    est_onward_price: Decimal | None = None
    through_fare: Decimal | None = None
    providers: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        """Every leg the trip shape requires has a real offer behind it.

        A round trip (return_date set) needs all four legs; a one-way needs
        only the two outbound legs. A round trip missing dom_ret or
        onward_ret is not confirmed, even though both outbound offers exist
        -- reporting True there would pair a real-looking total with a
        return flight the traveller still has to buy.
        """
        if self.dom_out is None or self.onward_out is None:
            return False
        if self.return_date:
            return self.dom_ret is not None and self.onward_ret is not None
        return True

    @property
    def dom_price(self) -> Decimal:
        """Sum of the real domestic offers, or the estimate when there are none.

        Keyed on offer presence, not on ``confirmed`` -- a round trip missing
        only its return leg is still unconfirmed, but the outbound leg it
        does have is a real price and must count, not fall through to an
        unset estimate and read as free.

        Invariant this relies on: a side with no offer always has its
        estimate set too (``from_candidate`` sets both together). Nothing
        today constructs a side with neither -- if a future caller ever
        does, this falls through to ``Decimal(0)``, which reads as free
        rather than as missing. Guard that at construction time, not here.
        """
        if self.dom_out is not None:
            total = self.dom_out.price
            if self.dom_ret is not None:
                total += self.dom_ret.price
            return total
        return self.est_dom_price if self.est_dom_price is not None else Decimal(0)

    @property
    def onward_price(self) -> Decimal:
        """Sum of the real onward offers, or the estimate when there are none.

        Same fallback-to-zero caveat as ``dom_price`` above: relies on a
        missing offer always coming with its estimate set.
        """
        if self.onward_out is not None:
            total = self.onward_out.price
            if self.onward_ret is not None:
                total += self.onward_ret.price
            return total
        return self.est_onward_price if self.est_onward_price is not None else Decimal(0)

    @property
    def dom_discounted(self) -> Decimal:
        return (self.dom_price * (Decimal(1) - self.discount)).quantize(_CENTS)

    @property
    def total(self) -> Decimal:
        return self.dom_discounted + self.onward_price

    @property
    def legs(self) -> tuple[Offer, ...]:
        return tuple(o for o in (self.dom_out, self.dom_ret,
                                 self.onward_out, self.onward_ret) if o is not None)

    @property
    def status(self) -> str:
        """One of STATUS_ESTIMATE, STATUS_CONFIRMED, STATUS_PARTIAL.

        ``confirmed`` alone cannot distinguish "no real data" from
        "incomplete real data" -- both report False. A display layer needs
        that distinction: a total built from zero real offers is a calendar
        guess, but a total missing only one leg out of four is mostly real
        money, and stamping both as "estimated" alike either overstates the
        uncertainty of the second or, if a caller instead trusts any
        non-zero total, overstates the confidence of the first.
        """
        if not self.legs:
            return STATUS_ESTIMATE
        if self.confirmed:
            return STATUS_CONFIRMED
        return STATUS_PARTIAL

    @property
    def is_estimate(self) -> bool:
        """Convenience for the single most common check a display layer makes."""
        return self.status == STATUS_ESTIMATE

    @property
    def requires_bag_recheck(self) -> bool | None:
        """True if any leg forces a bag re-check, None if any leg cannot say.

        Unknown wins over False: one provider reporting 'no' does not license
        claiming 'no' for a leg whose provider never answered.
        """
        answers = [o.requires_bag_recheck for o in self.legs]
        if not answers:
            return None
        if any(a is True for a in answers):
            return True
        if any(a is None for a in answers):
            return None
        return False

    @property
    def savings(self) -> Decimal | None:
        """How much this beats a genuine single-ticket through-fare by."""
        if self.through_fare is None:
            return None
        return (self.through_fare - self.total).quantize(_CENTS)

    @property
    def savings_pct(self) -> int | None:
        if self.through_fare is None or self.through_fare <= 0:
            return None
        return int((self.savings / self.through_fare) * 100)

    def with_through_fare(self, fare: Decimal | None) -> Itinerary:
        """Return a copy carrying a through-fare baseline (phase 2 sets this)."""
        return dataclasses.replace(self, through_fare=fare)

    def with_providers(self, *names: str) -> Itinerary:
        """Return a copy naming the providers that priced it (cross-check sets this)."""
        return dataclasses.replace(self, providers=tuple(names))

    @classmethod
    def from_candidate(cls, candidate: Candidate, hub_name: str, dest_name: str) -> Itinerary:
        """Build an unconfirmed itinerary from a calendar-derived candidate."""
        return cls(
            date=candidate.date,
            return_date=candidate.return_date,
            hub=candidate.hub,
            hub_name=hub_name,
            dest=candidate.dest,
            dest_name=dest_name,
            discount=candidate.discount,
            est_dom_price=candidate.dom_price,
            est_onward_price=candidate.onward_price,
        )


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
