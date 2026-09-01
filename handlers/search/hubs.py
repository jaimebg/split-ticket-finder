"""Hub multi-select (spec §6.3).

Hubs are the search's cost driver -- phase 0 issues one calendar request
per hub per destination, so eight hubs cost four times what two do. The
screen therefore shows every known hub as a toggle rather than hiding them
behind presets, and the draft's request estimate updates as they change.
"""
from __future__ import annotations

from config import DEFAULT_HUBS, PORTUGAL_HUBS, SPAIN_HUBS
from handlers.search.draft import Button, Rows, SearchDraft
from handlers.utils import esc

_KNOWN = {**SPAIN_HUBS, **PORTUGAL_HUBS}

_PRESETS = {
    "all": tuple(DEFAULT_HUBS),
    "top2": ("MAD", "BCN"),
    "top3": ("MAD", "BCN", "LIS"),
}

_PER_ROW = 3


def _named(codes) -> tuple[tuple[str, str], ...]:
    """Pair each code with its known name, or itself when unknown."""
    return tuple((code, _KNOWN.get(code, code)) for code in codes)


def toggle_hub(
    draft: SearchDraft, code: str, name: str | None = None
) -> SearchDraft:
    """Add *code* if absent, remove it if present.

    When *name* is given, use it for that code instead of looking it up in
    _KNOWN. When *name* is None, use the _KNOWN name if available, or the
    code itself as the label if unknown.

    Existing selections keep their previous names when toggled off and back on.
    """
    current = dict(draft.hubs)
    if code in current:
        # Toggle off: remove from selection
        del current[code]
    else:
        # Toggle on: add with the given or looked-up name
        if name is not None:
            current[code] = name
        else:
            current[code] = _KNOWN.get(code, code)
    return draft.with_(hubs=tuple(sorted(current.items())))


def apply_hub_preset(draft: SearchDraft, preset: str) -> SearchDraft:
    """Replace the selection with a preset set.

    Replaces rather than appends: tapping "Top 2" after picking six hubs
    must give two, not eight.
    """
    return draft.with_(hubs=_named(_PRESETS[preset]))


def add_typed_hubs(draft: SearchDraft, codes: list[str]) -> SearchDraft:
    """Merge typed codes into the selection, preserving order and uniqueness."""
    current = dict(draft.hubs)
    for code in codes:
        if code not in current:
            current[code] = _KNOWN.get(code, code)
    return draft.with_(hubs=tuple(sorted(current.items())))


def render_hubs(draft: SearchDraft) -> tuple[str, Rows]:
    """The hub screen: (Telegram HTML, button rows)."""
    chosen = set(draft.hub_codes)

    lines = [
        "<b>Which hubs?</b>",
        "",
        "A hub is where the discounted domestic leg ends and the onward "
        "flight begins.",
    ]
    if chosen:
        lines.append(
            f"\nSelected: <b>{' '.join(esc(c) for c in draft.hub_codes)}</b> "
            f"({len(chosen)})"
        )
    else:
        lines.append("\n<i>Pick at least one hub, or use a preset.</i>")
    lines.append(
        "\nYou can also send codes directly: <code>MAD, BCN, ZRH</code>."
    )

    codes = list(DEFAULT_HUBS)
    rows: Rows = []
    for i in range(0, len(codes), _PER_ROW):
        rows.append([
            Button(f"{'✓' if c in chosen else ''}{c}", f"h:{c}")
            for c in codes[i:i + _PER_ROW]
        ])

    rows.append([
        Button("All", "hp:all"),
        Button("Top 2", "hp:top2"),
        Button("Top 3", "hp:top3"),
    ])
    rows.append([Button("⬅️ Done", "back")])
    return "\n".join(lines), rows
