"""Serialization of phase 0's calendar grid for the ``searches`` table.

Result rendering moved to ``results/view.py`` and result storage to
``results/store.py`` in Layer 3b.
"""
from __future__ import annotations

import json

from engine.scan import CalendarGrid
from providers.base import RatedPrice

# ── JSON serializers ─────────────────────────────────────────────────────────

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
