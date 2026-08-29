from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from config import MAX_RETRIES, REQUEST_TIMEOUT, SOCS_COOKIE
from providers.base import (
    LegQuery,
    Offer,
    ProviderFetchError,
    ProviderParseError,
    Segment,
)

logger = logging.getLogger(__name__)


class ParseError(ProviderParseError):
    """Raised when a response could not be parsed as a flight listing.

    Distinguishes "Google returned something we don't understand" (layout
    change, consent wall, rate limiting) from the legitimate "this route has no
    flights on this date", which is an empty result list.
    """


class FetchError(ProviderFetchError):
    """Raised when the HTTP request failed after exhausting retries."""


# ============================================================
# Protobuf URL encoder
# ============================================================

def _varint(value):
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _field(fn, wt, data):
    return _varint((fn << 3) | wt) + data


def _str(fn, val):
    e = val.encode("utf-8")
    return _field(fn, 2, _varint(len(e)) + e)


def _bytes(fn, val):
    return _field(fn, 2, _varint(len(val)) + val)


def _var(fn, val):
    return _field(fn, 0, _varint(val))


def encode_tfs(from_apt, to_apt, date, adults=1, return_date=None):
    apt_from = _var(1, 1) + _str(2, from_apt)
    apt_to = _var(1, 1) + _str(2, to_apt)
    flt = _str(2, date) + _bytes(13, apt_from) + _bytes(14, apt_to)

    msg = bytearray()
    msg += _var(1, 28)
    if return_date:
        msg += _var(2, 1)                     # round-trip
    else:
        msg += _var(2, 2)                     # one-way
    msg += _bytes(3, bytes(flt))

    if return_date:
        apt_from_ret = _var(1, 1) + _str(2, to_apt)
        apt_to_ret = _var(1, 1) + _str(2, from_apt)
        flt_ret = _str(2, return_date) + _bytes(13, apt_from_ret) + _bytes(14, apt_to_ret)
        msg += _bytes(3, bytes(flt_ret))

    msg += _var(3, 1)
    msg += _var(8, 1)                         # economy
    msg += _var(9, 1)
    msg += _var(14, 1)
    pax = b""
    for _ in range(adults):
        pax += _var(1, 1)
    msg += _bytes(16, pax)
    msg += _bytes(18, _var(1, 1))
    return base64.b64encode(bytes(msg)).decode("utf-8").rstrip("=")


def build_url(from_apt, to_apt, date, adults=1, currency="EUR", return_date=None):
    tfs = encode_tfs(from_apt, to_apt, date, adults, return_date=return_date)
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&hl=es&curr={currency}"


# ============================================================
# Data classes
# ============================================================

@dataclass
class FlightResult:
    price: int
    airlines: list[str]
    stops: int
    duration: int               # total minutes
    segments: list[dict] = field(default_factory=list)

    @property
    def route_str(self):
        if not self.segments:
            return "?"
        parts = [s["from"] for s in self.segments] + [self.segments[-1]["to"]]
        return " -> ".join(parts)


# ============================================================
# Parser
# ============================================================

def _parse_offer(offer):
    try:
        fi = offer[0]
        if not isinstance(fi, list) or len(fi) < 3:
            return None

        airlines = fi[1] if isinstance(fi[1], list) else []
        raw_segs = fi[2] if isinstance(fi[2], list) else []

        segments = []
        for seg in raw_segs:
            if not isinstance(seg, list) or len(seg) < 12:
                continue
            s = {
                "from": seg[3] if isinstance(seg[3], str) else "?",
                "to": seg[6] if isinstance(seg[6], str) else "?",
                "dep_time": seg[8] if isinstance(seg[8], list) else [],
                "arr_time": seg[10] if isinstance(seg[10], list) else [],
                "duration": seg[11] if isinstance(seg[11], (int, float)) else 0,
            }
            if len(seg) > 17 and isinstance(seg[17], str):
                s["plane"] = seg[17]
            if len(seg) > 22 and isinstance(seg[22], list) and len(seg[22]) >= 2:
                s["flight"] = f"{seg[22][0]}{seg[22][1]}"
            segments.append(s)

        total_dur = fi[8] if len(fi) > 8 and isinstance(fi[8], (int, float)) else 0
        if not total_dur and segments:
            total_dur = sum(s["duration"] for s in segments)

        price = 0
        if isinstance(offer[1], list):
            p = offer[1][0] if isinstance(offer[1][0], list) else offer[1]
            if isinstance(p, list) and len(p) > 1 and isinstance(p[1], (int, float)):
                price = int(p[1])
        if price <= 0:
            return None

        return FlightResult(
            price=price,
            airlines=airlines,
            stops=max(0, len(segments) - 1),
            duration=int(total_dur),
            segments=segments,
        )
    except (IndexError, TypeError):
        return None


def parse_flights(html):
    """Extract flight offers from a Google Flights results page.

    Returns an empty list when the page is a valid results page that simply has
    no offers for the requested route/date. Raises :class:`ParseError` when the
    page is not a results page at all — a consent wall, a rate-limit response,
    or a layout change on Google's side. Callers need that distinction: the
    first is a normal outcome, the second means the scraper is broken.
    """
    if not html or len(html) < 1000:
        raise ParseError(f"response too short to be a results page ({len(html or '')} bytes)")
    m = re.search(r'<script[^>]*class="ds:1"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        raise ParseError("no ds:1 payload found (consent wall, block, or layout change)")
    dm = re.search(r'data:(.*?)(?:,\s*sideChannel|\}\s*\)\s*;?\s*$)', m.group(1), re.DOTALL)
    if not dm:
        raise ParseError("ds:1 payload found but it carries no data: field")
    try:
        data = json.loads(dm.group(1).strip())
    except json.JSONDecodeError as exc:
        raise ParseError(f"ds:1 data field is not valid JSON: {exc}") from exc

    results = []
    for section in (
        lambda: data[2][0],    # "best" offers
        lambda: data[3][0],    # "other" offers
    ):
        try:
            offers = section()
            if isinstance(offers, list):
                for o in offers:
                    f = _parse_offer(o)
                    if f:
                        results.append(f)
        except (IndexError, TypeError):
            pass

    results.sort(key=lambda f: f.price)
    return results


# ============================================================
# Async scraper
# ============================================================

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Status codes worth retrying: rate limiting and transient server errors.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def build_client() -> httpx.AsyncClient:
    """Create the shared HTTP client.

    One client per search run means one connection pool, so the many requests a
    search issues reuse connections instead of re-doing TLS every time.
    """
    return httpx.AsyncClient(
        headers=HEADERS,
        cookies={"SOCS": SOCS_COOKIE},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )


async def fetch_html(
    from_apt,
    to_apt,
    date,
    adults=1,
    currency="EUR",
    return_date=None,
    client: httpx.AsyncClient | None = None,
):
    """Fetch a Google Flights results page, retrying transient failures.

    Raises :class:`FetchError` once the retry budget is exhausted. Pass *client*
    to reuse a connection pool across many calls; otherwise a throwaway client
    is created for this single request.
    """
    url = build_url(from_apt, to_apt, date, adults, currency, return_date=return_date)
    leg = f"{from_apt}->{to_apt} {date}"

    if client is None:
        async with build_client() as own_client:
            return await fetch_html(
                from_apt, to_apt, date, adults, currency, return_date, client=own_client
            )

    last_error = "unknown error"
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            # Exponential backoff with jitter, so parallel workers that hit a
            # rate limit together don't retry in lockstep.
            delay = 2**attempt + random.uniform(0, 1)
            logger.info("Retrying %s in %.1fs (attempt %d): %s", leg, delay, attempt + 1, last_error)
            await asyncio.sleep(delay)

        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        if response.status_code in RETRY_STATUS:
            last_error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise FetchError(f"{leg}: HTTP {response.status_code}")
        return response.text

    raise FetchError(f"{leg}: giving up after {MAX_RETRIES + 1} attempts ({last_error})")


async def search(
    from_apt,
    to_apt,
    date,
    adults=1,
    currency="EUR",
    return_date=None,
    client: httpx.AsyncClient | None = None,
):
    """Search flights and return parsed results, cheapest first.

    Propagates :class:`FetchError` and :class:`ParseError` so callers can tell a
    broken scraper from a route with no availability.
    """
    html = await fetch_html(
        from_apt, to_apt, date, adults, currency, return_date=return_date, client=client
    )
    return parse_flights(html)


# ============================================================
# Provider adapter
# ============================================================

def _build_times(date, raw_segments):
    """Attach dates to Google's bare [hour, minute] pairs.

    Google reports clock times with no date attached. Time only ever moves
    forward within one itinerary, so whenever a clock time is earlier than the
    one before it the itinerary has crossed midnight and the day advances.
    Returns one (departure, arrival) tuple per segment, with None where the
    payload had nothing usable.
    """
    cursor = datetime.strptime(date, "%Y-%m-%d")
    out = []
    for seg in raw_segments:
        pair = []
        for key in ("dep_time", "arr_time"):
            hm = seg.get(key)
            if not (
                isinstance(hm, list)
                and len(hm) >= 2
                and isinstance(hm[0], int)
                and isinstance(hm[1], int)
            ):
                pair.append(None)
                continue
            try:
                candidate = cursor.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            except ValueError:
                pair.append(None)
                continue
            if candidate < cursor:
                candidate += timedelta(days=1)
            cursor = candidate
            pair.append(candidate)
        out.append((pair[0], pair[1]))
    return out


def _to_offer(flight, query: LegQuery) -> Offer:
    """Map one parsed FlightResult onto the shared Offer type."""
    times = _build_times(query.date, flight.segments)
    segments = []
    for raw, (dep, arr) in zip(flight.segments, times, strict=True):
        flight_no = raw.get("flight") or ""
        carrier = flight_no[:2] if len(flight_no) >= 2 else ""
        segments.append(Segment(
            origin=raw.get("from", "?"),
            dest=raw.get("to", "?"),
            carrier=carrier,
            carrier_name=carrier,
            flight_no=flight_no,
            duration=int(raw.get("duration") or 0),
            dep_local=dep,
            arr_local=arr,
        ))

    return Offer(
        price=Decimal(str(flight.price)),
        currency=query.currency,
        airlines=list(flight.airlines),
        stops=flight.stops,
        duration=flight.duration,
        segments=segments,
        provider="google",
        # Everything below is structurally absent from Google's payload.
        # None means "unknown", and the formatter must say so.
    )


class GoogleProvider:
    """Google Flights behind the shared provider interface.

    Implements FlightProvider only: Google exposes no price calendar and no
    place search, which is exactly why those are separate protocols.
    """

    name = "google"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns_client = client is None

    async def _get_client(self):
        if self._client is None:
            self._client = build_client()
        return self._client

    async def search_leg(self, query: LegQuery) -> list[Offer]:
        client = await self._get_client()
        html = await fetch_html(
            query.origin,
            query.dest,
            query.date,
            query.adults,
            query.currency,
            client=client,
        )
        flights = parse_flights(html)
        return [_to_offer(f, query) for f in flights[: query.limit]]

    async def aclose(self):
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
