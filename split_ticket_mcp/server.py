"""A local MCP server: the split-ticket engine as tools for an AI assistant.

Read-only, no database. Each tool validates its input and then calls exactly
what the Telegram bot calls. Anything a person could get wrong comes back as
a ToolError with a sentence. stdout carries the protocol, so nothing here
prints and main() sends logging to stderr.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import date

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

import config
from engine import run_search
from handlers.search.places import places_provider
from models import SearchWindow, bookable_onward_dates
from providers.base import (
    ALL_CABINS,
    ProviderError,
    SearchOptions,
    SupportsCalendar,
    capabilities_of,
)
from providers.registry import primary_provider
from results.filters import Filters, apply
from split_ticket_mcp.mapping import ItineraryOut, itinerary_out

mcp = MCPServer(
    "split-ticket-finder",
    instructions=(
        "Find flights cheaper than the airline's own fare by splitting one "
        "journey into a discounted domestic ticket plus an onward ticket via a "
        "hub. Use find_airports to turn place names into IATA codes, "
        "price_calendar to explore dates cheaply, and search_split_tickets "
        "for the real search, which is expensive."
    ),
)

_IATA = re.compile(r"^[A-Z]{3}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
MAX_PASSENGERS = 9
MAX_DESTINATIONS = 10


def _code(value: str, what: str) -> str:
    code = value.strip().upper()
    if not _IATA.match(code):
        raise ToolError(f"{what} {value!r} is not a 3-letter IATA airport code.")
    return code


def _day(value: str, what: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{what} {value!r} is not a YYYY-MM-DD date.") from None


def _options(*, adults: int, children: int, cabin: str, currency: str,
             max_stops: int | None = None, min_layover: int | None = None,
             overnight: bool = False) -> SearchOptions:
    if adults < 1:
        raise ToolError("A search needs at least one adult.")
    if children < 0 or adults + children > MAX_PASSENGERS:
        raise ToolError(f"A search holds at most {MAX_PASSENGERS} passengers.")
    cabin = cabin.strip().upper()
    if cabin not in ALL_CABINS:
        raise ToolError(f"{cabin!r} is not a cabin; use one of {', '.join(ALL_CABINS)}.")
    currency = currency.strip().upper()
    if not _CURRENCY.match(currency):
        raise ToolError(f"{currency!r} is not a 3-letter currency code.")
    if max_stops is not None and not 0 <= max_stops <= 3:
        raise ToolError("max_stops must be between 0 and 3.")
    if min_layover is not None and not 0 <= min_layover <= 24 * 60:
        raise ToolError("min_layover_minutes must be between 0 and 1440.")
    options = SearchOptions(adults=adults, children=children, cabin=cabin, currency=currency,
                            max_stops=max_stops, min_layover=min_layover, overnight=overnight)
    refusal = capabilities_of(primary_provider()).rejects(options)
    if refusal is not None:
        raise ToolError(refusal)
    return options


# ── search_split_tickets ─────────────────────────────────────────────────────

class SearchOutput(BaseModel):
    strategy: str
    currency: str
    routes_found: int
    hidden_as_risky: int
    failed_requests: int
    itineraries: list[ItineraryOut]


@mcp.tool()
async def search_split_tickets(
    destinations: list[str],
    window_start: str,
    window_end: str,
    origin: str | None = None,
    hubs: list[str] | None = None,
    trip_days: int = 0,
    adults: int = 1,
    children: int = 0,
    cabin: str = "ECONOMY",
    currency: str = "EUR",
    max_stops: int | None = None,
    min_layover_minutes: int | None = None,
    overnight: bool = False,
    hide_risky: bool = False,
    limit: int = 10,
) -> SearchOutput:
    """Search split-ticket itineraries: a discounted domestic flight to a hub plus a
    separately booked onward flight, compared with the airline's single ticket.

    COST: one call sends roughly 90-190 requests to the flight sources and can
    take up to a minute. Call it deliberately, not in a loop; use
    price_calendar to explore dates first.

    Dates are the international (onward) flight's days. trip_days=0 is
    one-way; otherwise the return is that many days later. overnight=True
    flies the domestic leg the day before (and the day after on the return).
    Prices are totals for the whole party. A ticket with price null has no
    bookable offer yet. failed_requests > 0 means the result may be
    incomplete, not that no flights exist.
    """
    if not 1 <= len(destinations) <= MAX_DESTINATIONS:
        raise ToolError(f"Give between 1 and {MAX_DESTINATIONS} destinations.")
    dests = [_code(d, "Destination") for d in destinations]
    org = _code(origin or config.ORIGIN, "Origin")
    hub_names = ({c: config.DEFAULT_HUBS.get(c, c) for c in (_code(h, "Hub") for h in hubs)}
                 if hubs else dict(config.DEFAULT_HUBS))
    start, end = _day(window_start, "window_start"), _day(window_end, "window_end")
    if end < start:
        raise ToolError("The window ends before it starts.")
    if not 0 <= trip_days <= 180:
        raise ToolError("trip_days must be between 0 (one-way) and 180.")
    if not 1 <= limit <= 30:
        raise ToolError("limit must be between 1 and 30.")
    options = _options(adults=adults, children=children, cabin=cabin, currency=currency,
                       max_stops=max_stops, min_layover=min_layover_minutes,
                       overnight=overnight)

    today = date.today().isoformat()
    window_days = [d for d in SearchWindow(start.isoformat(), end.isoformat()).dates()
                   if d >= today]
    dates = bookable_onward_dates(window_days, overnight, today)
    if not dates:
        hint = (" With a night at the hub the earliest international flight is tomorrow."
                if overnight else "")
        raise ToolError(f"No searchable dates: the window is in the past.{hint}")
    window = SearchWindow(dates[0], dates[-1])
    if window.days > config.MAX_WINDOW_DAYS:
        raise ToolError(f"The window covers {window.days} days; the most one search can "
                        f"cover is {config.MAX_WINDOW_DAYS}.")

    try:
        result = await run_search(origin=org, destinations={d: d for d in dests},
                                  hubs=hub_names, window=window, trip_days=trip_days,
                                  options=options, dates=dates)
    except ProviderError:
        raise ToolError("Your flight source can't run this search with these options.") from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    wanted = set(dates)
    itineraries = [it for it in result.itineraries if it.date in wanted]
    if hide_risky:
        shown_pairs, hidden = apply(itineraries, Filters(hide_risky=True))
        shown = [it for _, it in shown_pairs]
    else:
        shown, hidden = itineraries, 0
    shown.sort(key=lambda it: it.total)
    return SearchOutput(
        strategy=result.strategy,
        currency=options.currency,
        routes_found=len(itineraries),
        hidden_as_risky=hidden,
        failed_requests=result.parse_errors + result.fetch_errors,
        itineraries=[itinerary_out(it) for it in shown[:limit]],
    )


# ── price_calendar ───────────────────────────────────────────────────────────

class CalendarDay(BaseModel):
    date: str
    price: float
    rating: str


class CalendarOutput(BaseModel):
    currency: str
    days: list[CalendarDay]


@mcp.tool()
async def price_calendar(
    origin: str,
    dest: str,
    start: str,
    end: str,
    adults: int = 1,
    children: int = 0,
    cabin: str = "ECONOMY",
    currency: str = "EUR",
    max_stops: int | None = None,
) -> CalendarOutput:
    """Cheapest price per day for one direct route (origin -> dest), for the
    whole party. One request, so it is the cheap way to explore dates before
    search_split_tickets. These are cached cheapest-of-day figures, not
    bookable fares."""
    org, dst = _code(origin, "Origin"), _code(dest, "Destination")
    first, last = _day(start, "start"), _day(end, "end")
    if last < first:
        raise ToolError("The window ends before it starts.")
    if (last - first).days + 1 > config.MAX_WINDOW_DAYS:
        raise ToolError(f"The most one calendar can cover is {config.MAX_WINDOW_DAYS} days.")
    options = _options(adults=adults, children=children, cabin=cabin, currency=currency,
                       max_stops=max_stops)
    provider = primary_provider()
    if not isinstance(provider, SupportsCalendar):
        raise ToolError("Your flight source has no price calendar.")
    try:
        table = await provider.price_calendar(
            options.calendar_query(org, dst, first.isoformat(), last.isoformat()))
    except ProviderError as exc:
        raise ToolError(f"The price calendar failed: {exc}") from None
    return CalendarOutput(
        currency=options.currency,
        days=[CalendarDay(date=d, price=float(r.price), rating=r.rating)
              for d, r in sorted(table.items())],
    )


# ── find_airports ────────────────────────────────────────────────────────────

class AirportOut(BaseModel):
    code: str
    name: str
    city: str
    country: str


class AirportsOutput(BaseModel):
    airports: list[AirportOut]


@mcp.tool()
async def find_airports(query: str) -> AirportsOutput:
    """Turn a city or airport name into IATA codes, e.g. "Tokio" -> NRT, HND."""
    term = query.strip()
    if not 2 <= len(term) <= 60:
        raise ToolError("The query must be at least 2 characters and at most 60.")
    provider = places_provider()
    if provider is None:
        raise ToolError("No configured flight source can look up airport names; "
                        "pass IATA codes instead.")
    try:
        places = await provider.resolve_place(term)
    except ProviderError as exc:
        raise ToolError(f"The airport lookup failed: {exc}") from None
    return AirportsOutput(airports=[AirportOut(code=p.code, name=p.name, city=p.city,
                                               country=p.country) for p in places])


def main() -> None:
    """Entry point for the split-ticket-mcp command: stdio transport."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()
