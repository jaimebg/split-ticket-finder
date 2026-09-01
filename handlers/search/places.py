"""Free text to airports (spec §6.3).

Which provider answers is the load-bearing decision. It is *any* enabled
provider implementing SupportsPlaces, primary first -- not
primary_provider(). With PROVIDERS=("kiwi","google") and
PRIMARY_PROVIDER=google, Kiwi still resolves names even though Google
drives the search. Only a genuinely Kiwi-less deployment loses
autocomplete, and there this screen degrades to typed codes.

That is the honest reading of two success criteria that otherwise
contradict: "no step of the conversation requires knowing an IATA code"
and "disabling Kiwi leaves a working Google-only bot". The first holds
wherever a places-capable provider is configured; the degradation is what
the second is for.
"""
from __future__ import annotations

import dataclasses
import re

from config import PRIMARY_PROVIDER
from db import get_cached_places, put_cached_places
from handlers.search.draft import Button, Rows, SearchDraft
from handlers.utils import ValidationError, esc, parse_iata_codes
from providers.base import FlightProvider, Place, SupportsPlaces
from providers.registry import enabled_providers

# Place.code reaches a LegQuery, a booking URL and a searches row. A
# provider is not a trusted source for it, so it is validated here.
IATA_RE = re.compile(r"^[A-Z]{3}$")

MAX_RESULTS = 8

PROMPT_WITH_SEARCH = (
    "Type a city or airport — <code>Tokyo</code>, <code>Narita</code>.\n"
    "Or paste codes directly: <code>NRT, HND</code>."
)

PROMPT_CODES_ONLY = (
    "Send airport codes separated by commas.\n"
    "IATA codes are exactly three letters, e.g. <code>MAD</code>, "
    "<code>JFK</code>.\n"
    "<i>Name search needs a provider that supports it; none is configured.</i>"
)

_FIELD_TITLES = {"dest": "Where to?", "hubs": "Which hubs?"}


def places_provider() -> FlightProvider | None:
    """The first enabled provider that can resolve names, primary first."""
    providers = enabled_providers()
    primary = providers.get(PRIMARY_PROVIDER)
    if isinstance(primary, SupportsPlaces):
        return primary
    for provider in providers.values():
        if isinstance(provider, SupportsPlaces):
            return provider
    return None


def try_parse_codes(text: str) -> list[str] | None:
    """The typed codes in *text*, or None if it is not a code list.

    §6.3's power-user path, and the reason a dead places endpoint never
    blocks someone who knows the code. A three-letter term is read as a
    code rather than a name; that is ambiguous in principle (RIO is both)
    and overwhelmingly a code in practice.
    """
    try:
        return parse_iata_codes(text)
    except ValidationError:
        return None


async def resolve(term: str, limit: int = MAX_RESULTS) -> list[Place]:
    """Places matching *term*, cached. Empty list if nothing can resolve.

    A ProviderError propagates rather than being swallowed: this project's
    central rule is that empty means "no results" and an exception means
    "broken", and the caller renders the two differently. A failure is
    never cached -- one bad minute must not look like a dead airport for
    the next thirty days.
    """
    cached = await get_cached_places(term)
    if cached is not None:
        return [Place(**row) for row in cached]

    provider = places_provider()
    if provider is None:
        return []

    found = await provider.resolve_place(term, limit=limit)
    valid = [p for p in found if IATA_RE.match(p.code)]
    await put_cached_places(term, [dataclasses.asdict(p) for p in valid])
    return valid


def _label(place: Place, selected: bool) -> str:
    mark = "✓" if selected else ""
    return f"{mark}{esc(place.code)} {esc(place.name)} · {esc(place.city)} ({esc(place.country)})"


def render_picker(
    draft: SearchDraft,
    *,
    field: str,
    results: list[Place],
    term: str,
    error: str | None = None,
) -> tuple[str, Rows]:
    """The picker screen for *field* ("dest" or "hubs")."""
    chosen = draft.dest_codes if field == "dest" else draft.hub_codes

    lines = [_FIELD_TITLES[field]]

    if error:
        lines.append(f"\n⚠️ {esc(error)}")
        lines.append(PROMPT_WITH_SEARCH)
    elif places_provider() is None:
        lines.append("\n" + PROMPT_CODES_ONLY)
    else:
        lines.append("\n" + PROMPT_WITH_SEARCH)

    if term and not results and not error:
        lines.append(f"\nNothing matched <b>{esc(term)}</b>.")

    if chosen:
        lines.append("\nSelected: <b>" + " ".join(esc(c) for c in chosen) + "</b>")

    rows: Rows = [
        [Button(_label(p, p.code in chosen), f"p:{field}:{esc(p.code)}")]
        for p in results[:MAX_RESULTS]
    ]
    rows.append([Button("⬅️ Done", "back")])
    return "\n".join(lines), rows
