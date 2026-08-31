"""Presentation for search results: Telegram HTML rendering and JSON storage.

This is what remains of the pre-engine orchestrator once engine/ took over
actually searching (Tasks 9-11): a place to turn a list of ``Itinerary`` into
Telegram markup, and into the compact JSON blob the ``searches`` table
stores. Layer 3 rewrites presentation and will own this file next; until
then it stays intentionally plain.
"""
from __future__ import annotations

import json
from decimal import Decimal

from engine.scan import CalendarGrid
from handlers.utils import esc
from models import Itinerary
from providers.base import RatedPrice

_CENTS = Decimal("0.01")


# ── JSON serializers ─────────────────────────────────────────────────────────

def itineraries_to_json(itineraries: list[Itinerary]) -> str:
    """Serialize the top 25 itineraries to a compact JSON string for DB storage.

    Real ``Offer`` objects (booking links, exact flight numbers, segments)
    are not carried into storage — only the derived prices and metadata a
    redisplay needs. A row reloaded from this JSON is always rendered as an
    estimate (see ``handlers/history.py``'s reconstruction), which is
    honest: the numbers are a historical snapshot, not a fresh, bookable
    quote.
    """
    top = itineraries[:25]
    data = [
        {
            "date": it.date,
            "return_date": it.return_date,
            "hub": it.hub,
            "hub_name": it.hub_name,
            "dest": it.dest,
            "dest_name": it.dest_name,
            "discount": float(it.discount),
            "dom_price": float(it.dom_price),
            "dom_discounted": float(it.dom_discounted),
            "onward_price": float(it.onward_price),
            "total": float(it.total),
            "status": it.status,
            "through_fare": float(it.through_fare) if it.through_fare is not None else None,
            "savings": float(it.savings) if it.savings is not None else None,
            "savings_pct": it.savings_pct,
            "requires_bag_recheck": it.requires_bag_recheck,
            "providers": list(it.providers),
        }
        for it in top
    ]
    return json.dumps(data, ensure_ascii=False)


def scan_to_json(scan: CalendarGrid | None) -> str:
    """Serialize phase 0's calendar grid to a compact JSON string for DB storage.

    Task 11 added the ``searches.scan_json`` column specifically so a past
    search can be redisplayed without re-querying; this is what finally
    writes to it. Deliberately minimal -- a date -> price map per leg key,
    dropping each day's CHEAP/AVERAGE/EXPENSIVE rating and the grid's own
    error counters (already folded into the search-level totals a caller
    tracks separately). A domestic leg is keyed by hub alone; an onward leg
    by ``"hub|dest"``, since JSON object keys must be strings and the grid
    itself keys that side on a ``(hub, dest)`` tuple.

    ``scan`` is ``None`` for the grid-fallback strategy (no calendar was
    ever scanned) and serializes to the JSON literal ``"null"``, so
    ``json.loads(scan_to_json(scan))`` is always safe to call regardless of
    which strategy ran.
    """
    if scan is None:
        return "null"

    def _prices(table: dict[str, RatedPrice]) -> dict[str, float]:
        return {date: float(rated.price) for date, rated in table.items()}

    data = {
        "out_dom": {hub: _prices(table) for hub, table in scan.out_dom.items()},
        "ret_dom": {hub: _prices(table) for hub, table in scan.ret_dom.items()},
        "out_onward": {
            f"{hub}|{dest}": _prices(table)
            for (hub, dest), table in scan.out_onward.items()
        },
        "ret_onward": {
            f"{hub}|{dest}": _prices(table)
            for (hub, dest), table in scan.ret_onward.items()
        },
    }
    return json.dumps(data, ensure_ascii=False)


# ── Telegram formatter ───────────────────────────────────────────────────────

def _through_fare_for(itin: Itinerary, fallback: Decimal | None) -> Decimal | None:
    """The through-fare baseline to render for *itin*.

    Prefers the itinerary's own (attached by the engine, keyed to its exact
    destination and date); falls back to *fallback* only when the itinerary
    carries none at all — the case for a reconstructed legacy row, which
    never stored one.
    """
    return itin.through_fare if itin.through_fare is not None else fallback


def _savings_lines(
    itin: Itinerary, origin: str, currency: str, fallback: Decimal | None
) -> list[str]:
    """The savings block for one itinerary (spec §5.5).

    A through-fare that is cheaper than the split is not a saving — it is
    the opposite of the product's whole premise, and rendering it as one
    ("You save -11.25 EUR") would recommend the more expensive option. When
    the split does not beat the through-fare, this says so plainly instead.
    """
    through_fare = _through_fare_for(itin, fallback)
    if through_fare is None:
        return ["  Savings: no single-ticket fare available"]

    savings = (through_fare - itin.total).quantize(_CENTS)
    if savings <= 0:
        return [
            f"  Through-fare {origin}->{itin.dest}: {through_fare:,.2f} {currency}",
            "  The through-fare is cheaper — splitting is not worth it for this itinerary.",
        ]

    if itin.through_fare is not None:
        pct = itin.savings_pct
    else:
        pct = int((savings / through_fare) * 100) if through_fare > 0 else None
    pct_str = f" ({pct}%)" if pct is not None else ""

    return [
        f"  Through-fare {origin}->{itin.dest}   {through_fare:,.2f} {currency}",
        f"  Split via {itin.hub}          {itin.total:,.2f} {currency}",
        f"  You save               {savings:,.2f} {currency}{pct_str}",
    ]


def _booking_links(itin: Itinerary) -> list[str]:
    """Booking links for a confirmed itinerary's legs, labelled in trip order.

    Never called for an unconfirmed itinerary — ``format_results`` gates on
    ``itin.confirmed`` before reaching here: Phase 0's figures are cached
    cheapest-of-day calendar numbers, not fares anyone could actually book,
    and an itinerary missing a leg (``STATUS_PARTIAL``) is not bookable as a
    whole either, even though some of its legs have real offers.
    """
    labelled = [
        ("Domestic out", itin.dom_out),
        ("Onward out", itin.onward_out),
        ("Domestic return", itin.dom_ret),
        ("Onward return", itin.onward_ret),
    ]
    return [
        f'<a href="{esc(offer.booking_url)}">{label}</a>'
        for label, offer in labelled
        if offer is not None and offer.booking_url
    ]


def _itinerary_block(
    i: int, itin: Itinerary, origin: str, currency: str, fallback_through_fare: Decimal | None
) -> str:
    tag = f"{int(itin.discount * 100)}% disc." if itin.discount > 0 else "no disc."
    date_str = f"{itin.date} — {itin.return_date}" if itin.return_date else itin.date
    price_note = " (round-trip)" if itin.return_date else ""

    lines = [
        f"\n<b>#{i}</b> <code>{itin.total:,.2f} {currency}</code>{price_note}",
        f"  {date_str} | {origin} -> {itin.hub} ({esc(itin.hub_name)}) -> "
        f"{itin.dest} ({esc(itin.dest_name)})",
        f"  <i>Domestic leg:</i> {itin.dom_price:,.2f} {currency} ({tag}) -> "
        f"{itin.dom_discounted:,.2f} {currency}",
        f"  <i>Onward leg:</i> {itin.onward_price:,.2f} {currency}",
    ]

    if not itin.confirmed:
        # STATUS_ESTIMATE or STATUS_PARTIAL: never quote a bookable fare here.
        lines.append("  <i>Estimate only — not yet confirmed against a bookable fare.</i>")
    else:
        links = _booking_links(itin)
        if links:
            lines.append("  " + " | ".join(links))

    if itin.requires_bag_recheck is True:
        # None means "unknown" and must stay silent; only a confirmed "yes" warns.
        lines.append("  Warning: this itinerary requires re-checking bags between tickets.")

    lines.extend(_savings_lines(itin, origin, currency, fallback_through_fare))

    return "\n".join(lines)


def format_results(
    itineraries: list[Itinerary],
    origin: str,
    currency: str = "EUR",
    *,
    through_fare: Decimal | None = None,
) -> str:
    """Build Telegram-friendly HTML text with top results and summaries.

    ``through_fare`` is a fallback single-ticket baseline used only for an
    itinerary that carries none of its own — an itinerary the engine priced
    always carries its own, keyed to its exact destination and date, which
    takes precedence.

    The returned string uses HTML tags (<b>, <i>, <code>, <a>) compatible
    with python-telegram-bot's HTML parse mode. No single line exceeds 4096
    chars so the caller can split safely on double-newlines if the total is
    too long.
    """
    if not itineraries:
        return "<b>No routes found.</b>"

    roundtrip = bool(itineraries[0].return_date)
    trip_label = "Round-trip" if roundtrip else "One-way"
    parts: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────
    best = itineraries[0]
    date_info = f"{best.date} — {best.return_date}" if roundtrip else best.date
    parts.append(
        f"<b>{trip_label} · Found {len(itineraries)} routes</b>\n"
        f"Best: <b>{best.total:,.2f} {currency}</b> "
        f"({origin}->{best.hub}->{best.dest} on {date_info})"
    )

    # ── Top 10 ───────────────────────────────────────────────────────────
    top_n = min(10, len(itineraries))
    lines = [f"<b>Top {top_n} cheapest routes:</b>"]
    for i, itin in enumerate(itineraries[:top_n], 1):
        lines.append(_itinerary_block(i, itin, origin, currency, through_fare))
    parts.append("\n".join(lines))

    # ── Best per hub (itineraries are sorted by total; first per hub = best) ─
    seen_hubs: set[str] = set()
    hub_lines: list[str] = []
    for itin in itineraries:
        if itin.hub not in seen_hubs:
            seen_hubs.add(itin.hub)
            hub_lines.append(
                f"  {itin.hub} ({esc(itin.hub_name)}): <b>{itin.total:,.2f} {currency}</b>"
                f" on {itin.date} -> {itin.dest}"
            )
    if hub_lines:
        parts.append("<b>Best price per hub:</b>\n" + "\n".join(hub_lines))

    # ── Best per date (itineraries sorted by total; first per date = best) ──
    seen_dates: set[str] = set()
    date_lines: list[str] = []
    for itin in itineraries:
        if itin.date not in seen_dates:
            seen_dates.add(itin.date)
            date_lines.append(
                f"  {itin.date}: <b>{itin.total:,.2f} {currency}</b>"
                f" via {itin.hub} -> {itin.dest}"
            )
    if date_lines:
        parts.append("<b>Best price per date:</b>\n" + "\n".join(date_lines))

    # ── Reminder ─────────────────────────────────────────────────────────
    # The whole product exists to exploit this: a through-ticket never gets
    # the domestic-leg discount, only two separately booked tickets do.
    # Shown only when a displayed result actually carries one — no point
    # reminding the user about a discount none of the shown routes qualify
    # for, and the rate can differ per itinerary so no single percentage is
    # quoted here.
    if any(itin.discount > 0 for itin in itineraries[:top_n]):
        parts.append(
            "<i>Book each leg on its own, separate ticket — that is what lets the "
            "discounted domestic leg above actually receive its discount. A single "
            "through-fare ticket does not qualify for it.</i>"
        )

    return "\n\n".join(parts)
