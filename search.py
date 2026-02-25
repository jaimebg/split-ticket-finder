"""Async search orchestrator — ties scraper + config into a multi-phase search."""
from __future__ import annotations

import asyncio
import json
import logging

from config import DISCOUNT_AIRPORTS, DOMESTIC_DISCOUNT, DEFAULT_DELAY
from scraper import FlightResult, Route, build_url, fmt_dur, search

logger = logging.getLogger(__name__)


# ── Phase 1-2-3 search ──────────────────────────────────────────────────────

async def run_search(
    origin: str,
    destinations: dict[str, str],
    dates: list[str],
    hubs: dict[str, str],
    adults: int = 1,
    currency: str = "EUR",
    delay: float = DEFAULT_DELAY,
) -> list[Route]:
    """Run the full 3-phase search and return routes sorted by total price.

    Parameters
    ----------
    origin : str          – IATA code of departure airport (e.g. "LPA").
    destinations : dict   – {code: name} of final destinations.
    dates : list[str]     – Travel dates in "YYYY-MM-DD" format.
    hubs : dict           – {code: name} of hub airports to route through.
    adults : int          – Number of adult passengers.
    currency : str        – Currency code for prices.
    delay : float         – Seconds to sleep between HTTP requests.

    Returns
    -------
    list[Route] sorted by ascending total price.
    """

    # ── Phase 1: Domestic (origin -> hubs) ───────────────────────────────
    logger.info("Phase 1: searching %s -> %d hubs x %d dates",
                origin, len(hubs), len(dates))

    dom_cache: dict[tuple[str, str], list[FlightResult]] = {}

    for hub in hubs:
        for date in dates:
            try:
                flights = await search(origin, hub, date, adults, currency)
            except Exception:
                logger.exception("Phase 1 error: %s->%s %s", origin, hub, date)
                flights = []

            if flights:
                dom_cache[(hub, date)] = flights[:3]
                best = flights[0]
                logger.info("  %s->%s %s: %d flights, best %d %s",
                            origin, hub, date, len(flights),
                            best.price, currency)
            else:
                logger.info("  %s->%s %s: no flights", origin, hub, date)

            await asyncio.sleep(delay)

    hubs_found = {hub for hub, _ in dom_cache}
    logger.info("Phase 1 done: %d hub/date combos, %d hubs with flights",
                len(dom_cache), len(hubs_found))

    # ── Phase 2: International (hub -> destinations) ─────────────────────
    logger.info("Phase 2: searching %d hubs -> %d destinations x %d dates",
                len(hubs_found), len(destinations), len(dates))

    intl_cache: dict[tuple[str, str, str], list[FlightResult]] = {}

    for hub in hubs_found:
        for dest in destinations:
            for date in dates:
                if (hub, date) not in dom_cache:
                    continue
                try:
                    flights = await search(hub, dest, date, adults, currency)
                except Exception:
                    logger.exception("Phase 2 error: %s->%s %s", hub, dest, date)
                    flights = []

                if flights:
                    intl_cache[(hub, dest, date)] = flights[:3]
                    best = flights[0]
                    logger.info("  %s->%s %s: %d flights, best %d %s",
                                hub, dest, date, len(flights),
                                best.price, currency)
                else:
                    logger.info("  %s->%s %s: no flights", hub, dest, date)

                await asyncio.sleep(delay)

    logger.info("Phase 2 done: %d international combos", len(intl_cache))

    # ── Phase 3: Combine ─────────────────────────────────────────────────
    routes: list[Route] = []

    for (hub, date), doms in dom_cache.items():
        for dest, dest_name in destinations.items():
            intls = intl_cache.get((hub, dest, date))
            if not intls:
                continue
            dom = doms[0]
            is_discounted = hub in DISCOUNT_AIRPORTS
            discount = DOMESTIC_DISCOUNT if is_discounted else 0
            dom_discounted = dom.price * (1 - discount)

            for intl in intls[:2]:
                routes.append(Route(
                    date=date,
                    hub=hub,
                    hub_name=hubs.get(hub, hub),
                    dest=dest,
                    dest_name=dest_name,
                    dom_price=dom.price,
                    dom_discounted=dom_discounted,
                    intl_price=intl.price,
                    total=dom_discounted + intl.price,
                    dom_airlines=dom.airlines,
                    dom_stops=dom.stops,
                    dom_dur=dom.duration,
                    intl_airlines=intl.airlines,
                    intl_stops=intl.stops,
                    intl_dur=intl.duration,
                ))

    routes.sort(key=lambda r: r.total)
    logger.info("Phase 3 done: %d combined routes", len(routes))
    return routes


# ── JSON serializer ──────────────────────────────────────────────────────────

def routes_to_json(routes: list[Route]) -> str:
    """Serialize the top 25 routes to a compact JSON string for DB storage."""
    top = routes[:25]
    data = [
        {
            "date": r.date,
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
    parts: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────
    best = routes[0]
    parts.append(
        f"<b>Found {len(routes)} routes</b>\n"
        f"Best: <b>{best.total:,.0f} {currency}</b> "
        f"({origin}->{best.hub}->{best.dest} on {best.date})"
    )

    # ── Top 10 ───────────────────────────────────────────────────────────
    top_n = min(10, len(routes))
    lines = [f"<b>Top {top_n} cheapest routes:</b>"]

    for i, r in enumerate(routes[:top_n], 1):
        tag = f"{disc_pct}% disc." if r.hub in DISCOUNT_AIRPORTS else "no disc."
        lines.append(
            f"\n<b>#{i}</b> <code>{r.total:,.0f} {currency}</code>\n"
            f"  {r.date} | {origin} -> {r.hub} ({r.hub_name}) -> {r.dest} ({r.dest_name})\n"
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
        url1 = build_url(origin, r.hub, r.date, currency=currency)
        url2 = build_url(r.hub, r.dest, r.date, currency=currency)
        link_lines.append(
            f"  #{i} {origin}->{r.hub}->{r.dest} {r.date}\n"
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
