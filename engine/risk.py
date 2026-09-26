"""How dangerous a self-transfer between two separately booked tickets is.

One function judges a connection, and both the engine (to pick which flights
to pair) and the results view (to label them) call it, so the two can never
disagree. Thresholds are read from config at call time.
"""
from __future__ import annotations

from datetime import timedelta
from enum import IntEnum

import config
from models import Itinerary, fmt_dur, ground_time
from providers.base import Offer


class Risk(IntEnum):
    """Lower is better. The order is also the pairing preference."""

    LOW = 0
    MEDIUM = 1
    UNKNOWN = 2
    HIGH = 3
    IMPOSSIBLE = 4


RISK_LABELS = {
    Risk.LOW: "Low", Risk.MEDIUM: "Medium", Risk.UNKNOWN: "Unknown",
    Risk.HIGH: "High", Risk.IMPOSSIBLE: "Impossible",
}


def connection_risk(arriving: Offer | None, departing: Offer | None) -> Risk | None:
    """The risk of catching *departing* after *arriving*, or None if either is missing."""
    if arriving is None or departing is None:
        return None
    if not arriving.segments or not departing.segments:
        return Risk.UNKNOWN
    landed, leaving = arriving.segments[-1], departing.segments[0]
    if landed.dest != leaving.origin:
        return Risk.HIGH                   # airport change: a transfer across town
    gap = ground_time(landed, leaving)
    if gap is None:
        return Risk.UNKNOWN
    if gap < timedelta(0):
        return Risk.IMPOSSIBLE
    if gap < timedelta(hours=config.RISK_HIGH_BELOW_HOURS):
        return Risk.HIGH
    if gap < timedelta(hours=config.RISK_MEDIUM_BELOW_HOURS):
        return Risk.MEDIUM
    return Risk.LOW


def _reason(risk: Risk, arriving: Offer, departing: Offer, hub: str) -> str:
    if risk is Risk.UNKNOWN:
        return f"times not reported at {hub}"
    landed, leaving = arriving.segments[-1], departing.segments[0]
    if landed.dest != leaving.origin:
        return f"arrive {landed.dest}, depart {leaving.origin}"
    if risk is Risk.IMPOSSIBLE:
        return f"the second ticket leaves before the first lands at {hub}"
    gap = ground_time(landed, leaving)
    return f"{fmt_dur(int(gap.total_seconds() // 60))} between tickets at {hub}"


def itinerary_risk(itin: Itinerary) -> tuple[Risk, list[str]] | None:
    """The worse of the outbound and return connections, with one reason per
    connection judged. None when neither connection could be judged."""
    connections = [(itin.dom_out, itin.onward_out)]
    if itin.return_date:
        connections.append((itin.onward_ret, itin.dom_ret))
    worst: Risk | None = None
    reasons: list[str] = []
    for arriving, departing in connections:
        risk = connection_risk(arriving, departing)
        if risk is None:
            continue
        reasons.append(_reason(risk, arriving, departing, itin.hub))
        worst = risk if worst is None else max(worst, risk)
    return None if worst is None else (worst, reasons)
