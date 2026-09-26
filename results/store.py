"""What the ``searches.results`` column holds, and how to read it back.

Three shapes exist in real databases, and all three must keep loading:

- **v2** (Layer 3b on): ``{"v": 2, "itineraries": [...]}`` with every
  offer, segment, time and booking link, so a stored search can be paged,
  filtered and opened in detail after any number of restarts. Money is a
  string, so it comes back as the exact ``Decimal`` it was.
- **v1**: a bare list of prices and metadata, no offers. Loads as
  estimates.
- **The pre-engine ``Route`` shape**: also a bare list, with the old field
  names. Also loads as estimates.

``StoredResults.detailed`` is False for the last two. That is what turns
off filters and the detail view for them: there is nothing to filter on.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from models import Itinerary
from providers.base import Offer, Segment

logger = logging.getLogger(__name__)

VERSION = 2
_LEGS = ("dom_out", "dom_ret", "onward_out", "onward_ret")


@dataclass(frozen=True)
class StoredResults:
    itineraries: list[Itinerary]
    detailed: bool


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _segment_to_dict(s: Segment) -> dict:
    return {
        "origin": s.origin, "dest": s.dest, "carrier": s.carrier,
        "carrier_name": s.carrier_name, "flight_no": s.flight_no,
        "duration": s.duration, "dep_local": _time(s.dep_local),
        "arr_local": _time(s.arr_local),
    }


def _segment_from_dict(d: dict) -> Segment:
    return Segment(
        origin=d["origin"], dest=d["dest"], carrier=d["carrier"],
        carrier_name=d["carrier_name"], flight_no=d["flight_no"],
        duration=d["duration"], dep_local=_parse_time(d.get("dep_local")),
        arr_local=_parse_time(d.get("arr_local")),
    )


def _offer_to_dict(o: Offer | None) -> dict | None:
    if o is None:
        return None
    return {
        "price": _money(o.price), "currency": o.currency,
        "airlines": list(o.airlines), "stops": o.stops, "duration": o.duration,
        "segments": [_segment_to_dict(s) for s in o.segments],
        "provider": o.provider, "booking_url": o.booking_url,
        "included_cabin_bags": o.included_cabin_bags,
        "included_checked_bags": o.included_checked_bags,
        "checked_bag_price": _money(o.checked_bag_price),
        "min_layover": o.min_layover, "pnr_count": o.pnr_count,
        "requires_bag_recheck": o.requires_bag_recheck,
    }


def _offer_from_dict(d: dict | None) -> Offer | None:
    if d is None:
        return None
    return Offer(
        price=Decimal(d["price"]), currency=d["currency"],
        airlines=list(d["airlines"]), stops=d["stops"], duration=d["duration"],
        segments=[_segment_from_dict(s) for s in d["segments"]],
        provider=d["provider"], booking_url=d.get("booking_url"),
        included_cabin_bags=d.get("included_cabin_bags"),
        included_checked_bags=d.get("included_checked_bags"),
        checked_bag_price=_decimal(d.get("checked_bag_price")),
        min_layover=d.get("min_layover"), pnr_count=d.get("pnr_count"),
        requires_bag_recheck=d.get("requires_bag_recheck"),
    )


def _itinerary_to_dict(it: Itinerary) -> dict:
    d = {
        "date": it.date, "return_date": it.return_date, "hub": it.hub,
        "hub_name": it.hub_name, "dest": it.dest, "dest_name": it.dest_name,
        "discount": _money(it.discount),
        "est_dom_price": _money(it.est_dom_price),
        "est_onward_price": _money(it.est_onward_price),
        "through_fare": _money(it.through_fare),
        "providers": list(it.providers),
        # Derived, for anyone reading the column by hand. load() ignores them.
        "total": _money(it.total),
        "status": it.status,
    }
    for leg in _LEGS:
        d[leg] = _offer_to_dict(getattr(it, leg))
    return d


def _itinerary_from_v2(d: dict) -> Itinerary:
    return Itinerary(
        date=d["date"], return_date=d.get("return_date", ""), hub=d["hub"],
        hub_name=d.get("hub_name", d["hub"]), dest=d["dest"],
        dest_name=d.get("dest_name", d["dest"]),
        discount=Decimal(d["discount"]),
        est_dom_price=_decimal(d.get("est_dom_price")),
        est_onward_price=_decimal(d.get("est_onward_price")),
        through_fare=_decimal(d.get("through_fare")),
        providers=tuple(d.get("providers", ())),
        **{leg: _offer_from_dict(d.get(leg)) for leg in _LEGS},
    )


def serialize(itineraries: list[Itinerary]) -> dict:
    """The value for ``searches.results``; ``db.save_search`` JSON-encodes it."""
    return {"v": VERSION, "itineraries": [_itinerary_to_dict(it) for it in itineraries]}


# ── v1 and legacy reader ─────────────────────────────────────────────────────

def _itinerary_from_dict(d: dict) -> Itinerary:
    """Reconstruct an ``Itinerary`` from a stored result dict.

    Handles both the shape ``search.itineraries_to_json`` now writes
    (``discount``/``onward_price``/``through_fare``) and the pre-engine
    ``Route`` shape it replaced (``dom_price``/``dom_discounted``/
    ``intl_price``, no ``discount`` field at all) — the two rows in the live
    ``flight_finder.db`` are this older shape, and must still load.

    Neither shape carries a real ``Offer``, so a row reloaded from storage
    always comes back unconfirmed (``est_dom_price``/``est_onward_price``
    only): the numbers are a historical snapshot, not a fresh, bookable
    quote, and the results view labels it an estimate accordingly.

    ``return_date`` matters beyond display: the view uses it to
    decide whether the stored prices are round-trip totals, so dropping it
    would render a round-trip search as one-way with round-trip prices.
    """
    dom_price = Decimal(str(d["dom_price"]))
    onward_price = Decimal(str(d.get("onward_price", d.get("intl_price", 0))))

    if "discount" in d:
        discount = Decimal(str(d["discount"]))
    elif dom_price:
        # Old Route rows never stored the discount fraction directly, only
        # both sides of it (dom_price, dom_discounted) — recover it from
        # those so the stored total still reproduces exactly.
        dom_discounted = Decimal(str(d.get("dom_discounted", dom_price)))
        discount = Decimal(1) - dom_discounted / dom_price
    else:
        discount = Decimal(0)

    through_fare_raw = d.get("through_fare")
    through_fare = Decimal(str(through_fare_raw)) if through_fare_raw is not None else None

    return Itinerary(
        date=d["date"],
        return_date=d.get("return_date", ""),
        hub=d["hub"],
        hub_name=d.get("hub_name", d["hub"]),
        dest=d["dest"],
        dest_name=d.get("dest_name", d["dest"]),
        discount=discount,
        est_dom_price=dom_price,
        est_onward_price=onward_price,
        through_fare=through_fare,
    )


def load(raw: object) -> StoredResults:
    """Read any shape ``searches.results`` has ever held. Never raises."""
    if raw is None:
        return StoredResults(itineraries=[], detailed=False)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return StoredResults(itineraries=[], detailed=False)

    if isinstance(data, dict) and data.get("v") == VERSION:
        return StoredResults(
            itineraries=_each(_itinerary_from_v2, data.get("itineraries")), detailed=True,
        )
    if isinstance(data, list):
        return StoredResults(itineraries=_each(_itinerary_from_dict, data), detailed=False)
    return StoredResults(itineraries=[], detailed=False)


def _each(reader, entries: object) -> list[Itinerary]:
    """Read every entry that parses; skip, and log, one that doesn't.

    A hand-edited or half-written row must not take the whole search down
    with it: the rest of its results are still worth showing.
    """
    if not isinstance(entries, list):
        return []
    itineraries = []
    for entry in entries:
        try:
            itineraries.append(reader(entry))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            logger.warning("Skipping an unreadable stored result (%s): %r", exc, entry)
    return itineraries
