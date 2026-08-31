"""Helpers shared across handlers: message chunking, validation, rendering."""
from __future__ import annotations

import html
import json
from datetime import date as date_cls
from datetime import datetime

# Telegram rejects messages over 4096 characters. Leave headroom so a chunk
# never lands exactly on the limit after HTML entities are expanded.
TELEGRAM_LIMIT = 4000

# Sanity bound on how far ahead a search may be scheduled.
MAX_DAYS_AHEAD = 365


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split *text* into chunks of at most *limit* chars on blank-line breaks.

    Formatted results are built as blocks separated by blank lines, so splitting
    there keeps each chunk readable. A single block longer than *limit* is
    emitted as-is rather than cut mid-tag, which would produce invalid HTML.
    """
    chunks: list[str] = []
    current = ""

    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
        if len(block) > limit:
            chunks.append(block)
            current = ""
        else:
            current = block

    if current:
        chunks.append(current)
    return chunks


def esc(value: object) -> str:
    """Escape *value* for Telegram's HTML parse mode -- safe in both text
    content and quoted attribute values (e.g. an href).

    Every piece of user- or provider-supplied text interpolated into a
    message or attribute must go through this: an unescaped "<" makes
    Telegram reject the whole message as malformed HTML, and an unescaped
    '"' inside an attribute -- a booking URL from a third-party provider,
    say -- can break out of it entirely and inject a new one. Quotes are
    escaped unconditionally rather than only in an "attribute" variant of
    this function: escaping them in text content is harmless (Telegram
    parses the entities back to literal characters), so one function that
    is always safe beats two where picking the wrong one silently reopens
    the same hole.
    """
    return html.escape(str(value), quote=True)


# ── Input validation ────────────────────────────────────────────────────────


class ValidationError(ValueError):
    """Raised with a user-facing message when input can't be accepted."""


def parse_date(raw: str, *, field: str = "date") -> str:
    """Validate a "YYYY-MM-DD" string, returning it normalised.

    Raises :class:`ValidationError` with a message meant to be shown to the
    user, so the conversation can re-prompt instead of dying on a traceback.
    """
    text = raw.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(
            f"<b>{esc(text)}</b> is not a valid {field}.\n"
            "Use the format <code>YYYY-MM-DD</code>, for example <code>2026-09-15</code>."
        ) from None

    today = date_cls.today()
    if parsed < today:
        raise ValidationError(
            f"<b>{text}</b> is in the past. Pick a {field} from <code>{today}</code> onwards."
        )
    if (parsed - today).days > MAX_DAYS_AHEAD:
        raise ValidationError(
            f"<b>{text}</b> is more than {MAX_DAYS_AHEAD} days away — "
            "airlines rarely publish fares that far ahead."
        )
    return text


def parse_date_list(raw: str) -> list[str]:
    """Validate a comma-separated list of dates, de-duplicated and sorted."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValidationError("Please send at least one date.")
    return sorted({parse_date(p) for p in parts})


def parse_iata_codes(raw: str, *, field: str = "airport code") -> list[str]:
    """Validate a comma-separated list of IATA codes, de-duplicated in order."""
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValidationError(f"Please send at least one {field}.")

    bad = [p for p in parts if not (len(p) == 3 and p.isalpha())]
    if bad:
        raise ValidationError(
            f"Not valid {field}s: <b>{esc(', '.join(bad))}</b>.\n"
            "IATA codes are exactly three letters, e.g. <code>MAD</code>, <code>JFK</code>."
        )

    seen: list[str] = []
    for code in parts:
        if code not in seen:
            seen.append(code)
    return seen


def parse_positive_int(raw: str, *, field: str, maximum: int) -> int:
    """Validate a positive integer within *maximum*."""
    text = raw.strip()
    try:
        value = int(text)
    except ValueError:
        raise ValidationError(
            f"<b>{esc(text)}</b> is not a number. Send a whole number of {field}."
        ) from None
    if value < 1:
        raise ValidationError(f"{field.capitalize()} must be at least 1.")
    if value > maximum:
        raise ValidationError(f"{field.capitalize()} must be at most {maximum}.")
    return value


# ── Rendering ───────────────────────────────────────────────────────────────


def load_json_list(value: object) -> list:
    """Decode a JSON list stored in a TEXT column, tolerating bad data."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def format_favorite(fav: dict, default_origin: str) -> str:
    """Render one favourite as an HTML block for the favourites list."""
    record_price = fav.get("record_price")
    last_price = fav.get("last_price")
    last_checked = fav.get("last_checked")
    trip_days = fav.get("trip_days") or 0

    record_str = f"{record_price:,.0f}" if record_price is not None else "N/A"
    last_str = f"{last_price:,.0f}" if last_price is not None else "N/A"
    checked_str = last_checked[:10] if last_checked else "never"
    trip_str = f"round-trip {trip_days}d" if trip_days else "one-way"

    dates = load_json_list(fav.get("check_dates"))
    dates_str = ", ".join(str(d) for d in dates[:3])
    if len(dates) > 3:
        dates_str += f" (+{len(dates) - 3})"

    origin = fav.get("origin") or default_origin
    return (
        f"<b>{esc(origin)} -> {esc(fav['hub'])} -> {esc(fav['destination'])}</b>"
        f" <i>({trip_str})</i>\n"
        f"  Record: {record_str} | Last: {last_str}\n"
        f"  Checked: {checked_str} | Dates: {esc(dates_str)}"
    )
