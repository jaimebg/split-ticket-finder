"""The options screen: passengers, cabin, currency and search limits.

Pure, like draft.py and dates.py: it takes a draft and the provider's
Capabilities and returns text + button specs, or a new draft. The builder
does the Telegram part. Only what the provider can honour is drawn, and a
tap it can't honour is refused with the provider's own reason.
"""
from __future__ import annotations

from handlers.search.draft import Button, Rows, SearchDraft
from providers.base import CABIN_LABELS, Capabilities, SearchOptions

MAX_PASSENGERS = 9
CURRENCIES = ("EUR", "USD", "GBP")
STOP_CHOICES = (0, 1, 2)
LAYOVER_CHOICES = (60, 120, 180)      # minutes
_FULL = "Nine passengers is the most one search can hold."
_STALE = "That option is out of date."


def _mark(label: str, on: bool) -> str:
    return f"• {label}" if on else label


def _stops_label(n: int) -> str:
    return "Direct" if n == 0 else f"≤{n} stop{'s' if n > 1 else ''}"


def render(draft: SearchDraft, caps: Capabilities) -> tuple[str, Rows]:
    text = (f"<b>Search options</b>\n\n{draft.options.party_label()} · {draft.currency}"
            "\n\n<i>Prices are always the total for everyone travelling.</i>")
    rows: Rows = [[Button("Adults", "o:n"), Button("−", "o:a:-"),
                   Button(str(draft.adults), "o:n"), Button("+", "o:a:+")]]
    if caps.children:
        rows.append([Button("Children", "o:n"), Button("−", "o:k:-"),
                     Button(str(draft.children), "o:n"), Button("+", "o:k:+")])
    cabins = [c for c in CABIN_LABELS if c in caps.cabins]
    if len(cabins) > 1:
        rows.append([Button(_mark(CABIN_LABELS[c], draft.cabin == c), f"o:c:{c}")
                     for c in cabins])
    rows.append([Button(_mark(c, draft.currency == c), f"o:cur:{c}") for c in CURRENCIES])
    rows.append([Button(_mark("Any stops", draft.max_stops is None), "o:s:any")]
                + [Button(_mark(_stops_label(n), draft.max_stops == n), f"o:s:{n}")
                   for n in STOP_CHOICES])
    if caps.min_layover:
        rows.append([Button(_mark("Any layover", draft.min_layover is None), "o:l:any")]
                    + [Button(_mark(f"≥{m // 60}h layover", draft.min_layover == m),
                              f"o:l:{m}") for m in LAYOVER_CHOICES])
    rows.append([Button("⬅️ Back", "back")])
    return text, rows


def _passengers(draft: SearchDraft, key: str, sign: str,
                caps: Capabilities) -> SearchDraft | str:
    if sign not in ("+", "-"):
        return _STALE
    step = 1 if sign == "+" else -1
    adults, children = draft.adults, draft.children
    if key == "a":
        if adults + step < 1:
            return "At least one adult travels."
        adults += step
    else:
        if step > 0 and not caps.children:
            return caps.rejects(SearchOptions(children=1))
        if children + step < 0:
            return "There are no children to remove."
        children += step
    if adults + children > MAX_PASSENGERS:
        return _FULL
    return draft.with_(adults=adults, children=children)


def _choice(value: str, choices: tuple[int, ...]) -> int | str | None:
    """"any" -> None, a listed number -> int, anything else -> _STALE."""
    if value == "any":
        return None
    if value.isdigit() and int(value) in choices:
        return int(value)
    return _STALE


def apply(draft: SearchDraft, data: str, caps: Capabilities) -> SearchDraft | str:
    """The draft after one tap, or the reason the tap is refused."""
    parts = data.split(":")
    if len(parts) != 3:
        return _STALE
    _, key, value = parts

    if key in ("a", "k"):
        return _passengers(draft, key, value, caps)
    if key == "c" and value in CABIN_LABELS:
        new = draft.with_(cabin=value)
    elif key == "cur" and value in CURRENCIES:
        new = draft.with_(currency=value)
    elif key == "s":
        stops = _choice(value, STOP_CHOICES)
        if stops == _STALE:
            return _STALE
        new = draft.with_(max_stops=stops)
    elif key == "l":
        layover = _choice(value, LAYOVER_CHOICES)
        if layover == _STALE:
            return _STALE
        new = draft.with_(min_layover=layover)
    else:
        return _STALE

    refusal = caps.rejects(new.options)
    return refusal if refusal is not None else new
