"""Kiwi.com provider, over the GraphQL backend that serves kiwi.com itself.

The official Tequila API closed to new partners in May 2024, so this speaks to
api.skypicker.com directly. It needs no credentials but does require a valid
options.partner value, and it signals failure in a way that makes the error
handling here load-bearing:

  * An AppError arrives as HTTP 200 with __typename == "AppError". Anything
    branching on status codes alone will read a rejected partner key as
    success.
  * An unknown airport returns an *empty* result, not an error. That is a
    legitimate "no flights" and must not raise.

So: empty list means no flights, exception means broken, and never the reverse.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from config import (
    KIWI_PARTNER,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
)
from providers.base import LegQuery, Offer, ProviderFetchError, ProviderParseError, Segment

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.skypicker.com/umbrella/v2/graphql"
SITE_BASE = "https://www.kiwi.com"

HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "origin": SITE_BASE,
    "referer": f"{SITE_BASE}/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Rate limiting and transient server errors are worth retrying; nothing else is.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

ONEWAY_QUERY = """query OnewayItineraries($search: SearchOnewayInput, $filter: ItinerariesFilterInput, $options: ItinerariesOptionsInput) {
  onewayItineraries(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { message }
    ... on Itineraries { itineraries {
      id duration pnrCount
      price { amount }
      provider { name }
      bagsInfo { includedHandBags includedCheckedBags checkedBagTiers { tierPrice { amount } } }
      bookingOptions { edges { node { bookingUrl price { amount } } } }
      ... on ItineraryOneWay { sector { sectorSegments {
        layover { duration isStationChange isBaggageRecheck }
        segment { code duration
          carrier { code name }
          source { station { code name } localTime }
          destination { station { code name } localTime } } } } }
    } }
  }
}"""


def _place_id(code: str) -> str:
    """Kiwi place ids are deterministic, so a known IATA code needs no lookup."""
    return f"Station:airport:{code.strip().upper()}"


def _money(raw) -> Decimal:
    """Parse Kiwi's string prices. Junk raises rather than becoming zero."""
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderParseError(f"unparseable price {raw!r}") from exc


def _minutes(seconds) -> int:
    """Kiwi reports every duration in seconds; everything else here uses minutes."""
    try:
        return int(seconds) // 60
    except (TypeError, ValueError) as exc:
        raise ProviderParseError(f"unparseable duration {seconds!r}") from exc


def _booking_url(raw) -> str | None:
    """Booking links come back relative to the kiwi.com site root."""
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"{SITE_BASE}{raw}"


def _local_time(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _require(node: dict, *path: str):
    """Walk a path that the query asked for, raising if the shape changed."""
    cursor = node
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            raise ProviderParseError(f"missing expected field {'.'.join(path)!r}")
        cursor = cursor[key]
    return cursor


def build_client() -> httpx.AsyncClient:
    """One client per provider instance, so requests reuse connections."""
    return httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT)


class KiwiProvider:
    """Kiwi.com behind the shared provider interface.

    Implements FlightProvider, SupportsCalendar and SupportsPlaces.
    """

    name = "kiwi"

    def __init__(self, client: httpx.AsyncClient | None = None, partner: str | None = None):
        self._client = client
        self._owns_client = client is None
        self._partner = partner or KIWI_PARTNER

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client()
        return self._client

    def _options(self, currency: str, extra: dict | None = None) -> dict:
        options = {
            "partner": self._partner,
            "currency": currency.lower(),
            "locale": "en",
        }
        if extra:
            options.update(extra)
        return options

    async def _execute(self, operation: str, query: str, variables: dict, root: str) -> dict:
        """POST one GraphQL operation and return its root node.

        Raises ProviderFetchError when the request never landed, and
        ProviderParseError when something came back that we cannot trust --
        including an AppError, which is an HTTP 200.
        """
        client = await self._get_client()
        url = f"{ENDPOINT}?featureName={operation}"
        body = {"query": query, "variables": variables}
        last_error = "unknown error"

        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                # Exponential backoff with jitter, so parallel workers that hit
                # a rate limit together do not retry in lockstep.
                delay = 2**attempt + random.uniform(0, 1)
                logger.info(
                    "Retrying %s in %.1fs (attempt %d): %s",
                    operation, delay, attempt + 1, last_error,
                )
                await asyncio.sleep(delay)

            try:
                response = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                raise ProviderFetchError(f"{operation}: HTTP {response.status_code}")

            return self._unwrap(operation, response, root)

        raise ProviderFetchError(
            f"{operation}: giving up after {MAX_RETRIES + 1} attempts ({last_error})"
        )

    @staticmethod
    def _unwrap(operation: str, response: httpx.Response, root: str) -> dict:
        """Pull the root node out of a 200 response, or explain why we cannot."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderParseError(
                f"{operation}: response body is not JSON ({exc})"
            ) from exc

        if payload.get("errors"):
            # GraphQL validation failed, which means the schema moved under us.
            messages = "; ".join(
                str(e.get("message", e)) for e in payload["errors"][:3]
            )
            raise ProviderParseError(f"{operation}: GraphQL errors: {messages}")

        node = (payload.get("data") or {}).get(root)
        if node is None:
            raise ProviderParseError(f"{operation}: response carries no {root!r} field")

        if node.get("__typename") == "AppError":
            raise ProviderParseError(
                f"{operation}: {node.get('message', 'AppError with no message')}"
            )

        return node

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _leg_filter(self, query: LegQuery) -> dict:
        flt: dict = {"limit": query.limit, "transportTypes": ["FLIGHT"]}
        if query.max_stops is not None:
            flt["maxStopsCount"] = query.max_stops
        if query.min_layover is not None:
            # stopoverTime is expressed in HOURS. Passing seconds or minutes
            # here does not error -- it silently matches nothing.
            flt["stopoverTime"] = {"start": max(0, query.min_layover // 60), "end": 48}
        if query.exclude_carriers:
            flt["excludeCarriers"] = list(query.exclude_carriers)
        return flt

    def _to_offer(self, raw: dict, query: LegQuery) -> Offer:
        sector_segments = _require(raw, "sector", "sectorSegments")

        segments: list[Segment] = []
        layovers: list[int] = []
        for entry in sector_segments:
            seg = _require(entry, "segment")
            carrier = _require(seg, "carrier")
            code = carrier.get("code") or ""
            segments.append(Segment(
                origin=_require(seg, "source", "station", "code"),
                dest=_require(seg, "destination", "station", "code"),
                carrier=code,
                carrier_name=carrier.get("name") or code,
                flight_no=f"{code}{seg.get('code') or ''}",
                duration=_minutes(seg.get("duration") or 0),
                dep_local=_local_time((seg.get("source") or {}).get("localTime")),
                arr_local=_local_time((seg.get("destination") or {}).get("localTime")),
            ))
            layover = entry.get("layover")
            if layover and layover.get("duration") is not None:
                layovers.append(_minutes(layover["duration"]))

        bags = raw.get("bagsInfo") or {}
        tiers = bags.get("checkedBagTiers") or []
        checked_bag_price = None
        if tiers:
            checked_bag_price = _money(_require(tiers[0], "tierPrice", "amount"))

        edges = (raw.get("bookingOptions") or {}).get("edges") or []
        booking_url = _booking_url(
            (edges[0].get("node") or {}).get("bookingUrl") if edges else None
        )

        return Offer(
            price=_money(_require(raw, "price", "amount")),
            currency=query.currency.upper(),
            airlines=list(dict.fromkeys(s.carrier_name for s in segments)),
            stops=max(0, len(segments) - 1),
            duration=_minutes(raw.get("duration") or 0),
            segments=segments,
            provider=self.name,
            booking_url=booking_url,
            included_cabin_bags=bags.get("includedHandBags"),
            included_checked_bags=bags.get("includedCheckedBags"),
            checked_bag_price=checked_bag_price,
            min_layover=min(layovers) if layovers else None,
            pnr_count=raw.get("pnrCount"),
        )

    async def search_leg(self, query: LegQuery) -> list[Offer]:
        variables = {
            "search": {
                "itinerary": {
                    "source": {"ids": [_place_id(query.origin)]},
                    "destination": {"ids": [_place_id(query.dest)]},
                    "outboundDepartureDate": {
                        "start": f"{query.date}T00:00:00",
                        "end": f"{query.date}T23:59:59",
                    },
                },
                "passengers": {"adults": query.adults, "children": query.children},
                "cabinClass": {"cabinClass": query.cabin},
            },
            "filter": self._leg_filter(query),
            "options": self._options(query.currency, {"sortBy": "PRICE"}),
        }
        node = await self._execute(
            "OnewayItineraries", ONEWAY_QUERY, variables, "onewayItineraries"
        )
        itineraries = node.get("itineraries")
        if itineraries is None:
            raise ProviderParseError("OnewayItineraries: response carries no itineraries")
        offers = [self._to_offer(raw, query) for raw in itineraries]
        offers.sort(key=lambda o: o.price)
        return offers
