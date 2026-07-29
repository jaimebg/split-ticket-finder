"""Async search orchestrator — ties scraper + config into a multi-phase search."""
from __future__ import annotations

import asyncio
import json
import logging

from config import (
    DEFAULT_DELAY,
    DISCOUNT_AIRPORTS,
    DOMESTIC_DISCOUNT,
    MAX_CONCURRENCY,
)
from scraper import (
    FetchError,
    FlightResult,
    ParseError,
    Route,
    add_days,
    build_client,
    build_url,
    fmt_dur,
    search,
)

logger = logging.getLogger(__name__)

# A leg is one origin->destination query on one date.
Leg = tuple[str, str, str]


class _LegFetcher:
    """Runs many leg queries with bounded concurrency and a per-worker delay.

    A search issues hundreds of requests. Running them one at a time (the
    original design) took tens of minutes; running them all at once would get
    the scraper blocked. This caps in-flight requests at MAX_CONCURRENCY and
    still spaces out each worker's requests by DEFAULT_DELAY, so throughput
    scales with the cap while the request rate stays predictable.
    """

    def __init__(self, client, *, delay: float, concurrency: int):
        self._client = client
        self._delay = delay
        self._concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self.parse_errors = 0
        self.fetch_errors = 0

    async def _one(self, from_apt, to_apt, date, adults, currency):
        async with self._semaphore:
            try:
                flights = await search(
                    from_apt, to_apt, date, adults, currency, client=self._client
                )
            except ParseError as exc:
                # Layout change, consent wall or rate limiting — not "no flights".
                self.parse_errors += 1
                logger.warning("Parse failed for %s->%s %s: %s", from_apt, to_apt, date, exc)
                return []
            except FetchError as exc:
                self.fetch_errors += 1
                logger.warning("Fetch failed for %s->%s %s: %s", from_apt, to_apt, date, exc)
                return []
            except Exception:
                self.fetch_errors += 1
                logger.exception("Unexpected error for %s->%s %s", from_apt, to_apt, date)
                return []
            finally:
                # Hold the slot for the delay so the rate limit is per-worker.
                if self._delay:
                    await asyncio.sleep(self._delay)
            return flights

    async def run(
        self, legs: list[Leg], adults: int, currency: str, phase: str
    ) -> dict[Leg, list[FlightResult]]:
        """Fetch every leg concurrently, returning only those with results."""
        if not legs:
            return {}

        logger.info("%s: %d queries (concurrency %d)", phase, len(legs), self._concurrency)
        results = await asyncio.gather(
            *(self._one(f, t, d, adults, currency) for f, t, d in legs)
        )

        found: dict[Leg, list[FlightResult]] = {}
        for leg, flights in zip(legs, results, strict=True):
            if flights:
                found[leg] = flights[:3]
        logger.info("%s done: %d/%d legs with flights", phase, len(found), len(legs))
        return found


# ── Multi-phase search ──────────────────────────────────────────────────────

async def run_search(
    origin: str,
    destinations: dict[str, str],
    dates: list[str],
    hubs: dict[str, str],
    adults: int = 1,
    currency: str = "EUR",
    delay: float = DEFAULT_DELAY,
    trip_days: int = 0,
    concurrency: int = MAX_CONCURRENCY,
) -> list[Route]:
    """Search split-ticket itineraries and return them sorted by total price.

    Runs in phases, because each phase narrows the next: only hubs reachable
    from *origin* are worth querying for onward flights, which cuts the query
    count substantially versus a full cross product.

      1.  origin -> hub            (the discounted leg)
      1R. hub -> origin            (round-trip only)
      2.  hub -> destination       (the international leg)
      2R. destination -> hub       (round-trip only)

    Return legs are queried as separate one-way searches rather than as a
    round-trip query, because the whole point is to book the legs separately.

    Parameters
    ----------
    origin : str          – IATA code of departure airport (e.g. "LPA").
    destinations : dict   – {code: name} of final destinations.
    dates : list[str]     – Travel dates in "YYYY-MM-DD" format.
    hubs : dict           – {code: name} of hub airports to route through.
    adults : int          – Number of adult passengers.
    currency : str        – Currency code for prices.
    delay : float         – Seconds each worker waits between its requests.
    trip_days : int       – Trip duration in days (0 = one-way).
    concurrency : int     – Maximum requests in flight at once.

    Returns
    -------
    list[Route] sorted by ascending total price.
    """
    roundtrip = trip_days > 0
    label = f"round-trip ({trip_days}d)" if roundtrip else "one-way"
    logger.info(
        "Search [%s]: %s -> %d hubs -> %d destinations over %d dates",
        label, origin, len(hubs), len(destinations), len(dates),
    )

    async with build_client() as client:
        fetcher = _LegFetcher(client, delay=delay, concurrency=concurrency)

        # ── Phase 1: outbound discounted leg (origin -> hubs) ──────────────
        dom_found = await fetcher.run(
            [(origin, hub, date) for hub in hubs for date in dates],
            adults, currency, "Phase 1 (origin -> hubs)",
        )
        # Re-key by (hub, date); the origin is constant across the phase.
        dom_cache = {(hub, date): f for (_, hub, date), f in dom_found.items()}

        hubs_found = {hub for hub, _ in dom_cache}
        if not hubs_found:
            logger.warning("No hub is reachable from %s on any requested date.", origin)
            return []

        # ── Phase 1R: return discounted leg (hubs -> origin) ───────────────
        dom_ret_cache: dict[tuple[str, str], list[FlightResult]] = {}
        if roundtrip:
            ret_legs = [
                (hub, origin, add_days(date, trip_days)) for hub, date in dom_cache
            ]
            ret_found = await fetcher.run(
                ret_legs, adults, currency, "Phase 1R (hubs -> origin)"
            )
            # Map back from the return date to the outbound date it belongs to.
            dom_ret_cache = {
                (hub, date): ret_found[(hub, origin, add_days(date, trip_days))]
                for hub, date in dom_cache
                if (hub, origin, add_days(date, trip_days)) in ret_found
            }

        # ── Phase 2: outbound international leg (hubs -> destinations) ─────
        intl_cache = await fetcher.run(
            [
                (hub, dest, date)
                for hub, date in dom_cache
                for dest in destinations
            ],
            adults, currency, "Phase 2 (hubs -> destinations)",
        )

        # ── Phase 2R: return international leg (destinations -> hubs) ──────
        intl_ret_cache: dict[tuple[str, str, str], list[FlightResult]] = {}
        if roundtrip:
            ret_found = await fetcher.run(
                [
                    (dest, hub, add_days(date, trip_days))
                    for hub, dest, date in intl_cache
                ],
                adults, currency, "Phase 2R (destinations -> hubs)",
            )
            intl_ret_cache = {
                (hub, dest, date): ret_found[(dest, hub, add_days(date, trip_days))]
                for hub, dest, date in intl_cache
                if (dest, hub, add_days(date, trip_days)) in ret_found
            }

    if fetcher.parse_errors or fetcher.fetch_errors:
        logger.warning(
            "Search completed with %d parse failures and %d fetch failures — "
            "results may be incomplete.",
            fetcher.parse_errors, fetcher.fetch_errors,
        )

    # ── Combine ──────────────────────────────────────────────────────────
    routes: list[Route] = []

    for (hub, date), doms in dom_cache.items():
        for dest, dest_name in destinations.items():
            intls = intl_cache.get((hub, dest, date))
            if not intls:
                continue

            dom_out = doms[0]
            is_discounted = hub in DISCOUNT_AIRPORTS
            discount = DOMESTIC_DISCOUNT if is_discounted else 0

            if roundtrip:
                dom_rets = dom_ret_cache.get((hub, date))
                intl_rets = intl_ret_cache.get((hub, dest, date))
                if not dom_rets or not intl_rets:
                    continue
                dom_price = dom_out.price + dom_rets[0].price
                ret = add_days(date, trip_days)
            else:
                dom_price = dom_out.price
                ret = ""

            dom_discounted = dom_price * (1 - discount)

            for intl_out in intls[:2]:
                intl_price = (
                    intl_out.price + intl_rets[0].price if roundtrip else intl_out.price
                )

                routes.append(Route(
                    date=date,
                    hub=hub,
                    hub_name=hubs.get(hub, hub),
                    dest=dest,
                    dest_name=dest_name,
                    dom_price=dom_price,
                    dom_discounted=dom_discounted,
                    intl_price=intl_price,
                    total=dom_discounted + intl_price,
                    return_date=ret,
                    dom_airlines=dom_out.airlines,
                    dom_stops=dom_out.stops,
                    dom_dur=dom_out.duration,
                    intl_airlines=intl_out.airlines,
                    intl_stops=intl_out.stops,
                    intl_dur=intl_out.duration,
                ))

    routes.sort(key=lambda r: r.total)
    logger.info("Combine done: %d routes", len(routes))
    return routes


# ── JSON serializer ──────────────────────────────────────────────────────────

def routes_to_json(routes: list[Route]) -> str:
    """Serialize the top 25 routes to a compact JSON string for DB storage."""
    top = routes[:25]
    data = [
        {
            "date": r.date,
            "return_date": r.return_date,
            "hub": r.hub,
            "hub_name": r.hub_name,
            "dest": r.dest,
            "dest_name": r.dest_name,
            "dom_price": r.dom_price,
            "dom_discounted": round(r.dom_discounted, 2),
            "intl_price": r.intl_price,
            "total": round(r.total, 2),
            "dom_airlines": r.dom_airlines,
            "intl_airlines": r.intl_airlines,
            "dom_stops": r.dom_stops,
            "dom_dur": r.dom_dur,
            "intl_stops": r.intl_stops,
            "intl_dur": r.intl_dur,
        }
        for r in top
    ]
    return json.dumps(data, ensure_ascii=False)


# ── Telegram formatter ───────────────────────────────────────────────────────

def _stops_label(n: int) -> str:
    if n == 0:
        return "direct"
    return f"{n} stop{'s' if n != 1 else ''}"


def format_results(
    routes: list[Route],
    origin: str,
    currency: str = "EUR",
) -> str:
    """Build Telegram-friendly HTML text with top results and summaries.

    The returned string uses HTML tags (<b>, <i>, <code>) compatible with
    python-telegram-bot's HTML parse mode.  No single line exceeds 4096 chars
    so the caller can split safely on double-newlines if the total is too long.
    """
    if not routes:
        return "<b>No routes found.</b>"

    disc_pct = int(DOMESTIC_DISCOUNT * 100)
    roundtrip = bool(routes[0].return_date)
    trip_label = "Round-trip" if roundtrip else "One-way"
    parts: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────
    best = routes[0]
    date_info = best.date
    if roundtrip:
        date_info = f"{best.date} — {best.return_date}"
    parts.append(
        f"<b>{trip_label} · Found {len(routes)} routes</b>\n"
        f"Best: <b>{best.total:,.0f} {currency}</b> "
        f"({origin}->{best.hub}->{best.dest} on {date_info})"
    )

    # ── Top 10 ───────────────────────────────────────────────────────────
    top_n = min(10, len(routes))
    lines = [f"<b>Top {top_n} cheapest routes:</b>"]

    for i, r in enumerate(routes[:top_n], 1):
        tag = f"{disc_pct}% disc." if r.hub in DISCOUNT_AIRPORTS else "no disc."
        date_str = f"{r.date} — {r.return_date}" if r.return_date else r.date
        price_note = " (round-trip)" if r.return_date else ""
        lines.append(
            f"\n<b>#{i}</b> <code>{r.total:,.0f} {currency}</code>{price_note}\n"
            f"  {date_str} | {origin} -> {r.hub} ({r.hub_name}) -> {r.dest} ({r.dest_name})\n"
            f"  <i>Leg 1:</i> {r.dom_price} {currency} ({tag}) -> {r.dom_discounted:.0f} {currency}"
            f" | {', '.join(r.dom_airlines)} | {_stops_label(r.dom_stops)} | {fmt_dur(r.dom_dur)}\n"
            f"  <i>Leg 2:</i> {r.intl_price} {currency}"
            f" | {', '.join(r.intl_airlines)} | {_stops_label(r.intl_stops)} | {fmt_dur(r.intl_dur)}"
        )
    parts.append("\n".join(lines))

    # ── Best per hub (routes are sorted by total; first per hub = best) ─
    seen_hubs: set[str] = set()
    hub_lines: list[str] = []
    for r in routes:
        if r.hub not in seen_hubs:
            seen_hubs.add(r.hub)
            hub_lines.append(
                f"  {r.hub} ({r.hub_name}): <b>{r.total:,.0f} {currency}</b>"
                f" on {r.date} -> {r.dest}"
            )
    if hub_lines:
        parts.append("<b>Best price per hub:</b>\n" + "\n".join(hub_lines))

    # ── Best per date (routes sorted by total; first per date = best) ──
    seen_dates: set[str] = set()
    date_lines: list[str] = []
    for r in routes:
        if r.date not in seen_dates:
            seen_dates.add(r.date)
            date_lines.append(
                f"  {r.date}: <b>{r.total:,.0f} {currency}</b>"
                f" via {r.hub} -> {r.dest}"
            )
    if date_lines:
        parts.append("<b>Best price per date:</b>\n" + "\n".join(date_lines))

    # ── Google Flights links (top 3) ─────────────────────────────────────
    link_n = min(3, len(routes))
    link_lines: list[str] = []
    for i, r in enumerate(routes[:link_n], 1):
        ret = r.return_date or None
        url1 = build_url(origin, r.hub, r.date, currency=currency, return_date=ret)
        url2 = build_url(r.hub, r.dest, r.date, currency=currency, return_date=ret)
        date_str = f"{r.date} — {r.return_date}" if r.return_date else r.date
        link_lines.append(
            f"  #{i} {origin}->{r.hub}->{r.dest} {date_str}\n"
            f"    <a href=\"{url1}\">Leg 1</a> | <a href=\"{url2}\">Leg 2</a>"
        )
    if link_lines:
        parts.append("<b>Google Flights links:</b>\n" + "\n".join(link_lines))

    # ── Reminder ─────────────────────────────────────────────────────────
    parts.append(
        f"<i>Book legs separately to apply the {disc_pct}% Canary Islands"
        " resident discount on Spanish domestic flights.</i>"
    )

    return "\n\n".join(parts)
