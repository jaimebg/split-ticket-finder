"""Configuration loaded from environment variables (see .env.example)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _float_env(name: str, default: float, *, lo: float, hi: float) -> float:
    """Read a float env var, falling back to *default* if unset or out of range."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _int_env(name: str, default: int, *, lo: int) -> int:
    """Read an int env var, falling back to *default* if unset."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {value}")
    return value


def _codes_env(name: str, default: set[str]) -> set[str]:
    """Read a comma-separated list of IATA codes into a set."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return set(default)
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


KNOWN_PROVIDERS = ("kiwi", "google")


def _providers_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated provider list, preserving order and de-duplicating."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return tuple(default)
    names = []
    for part in raw.split(","):
        cleaned = part.strip().lower()
        if not cleaned:
            continue
        if cleaned not in KNOWN_PROVIDERS:
            raise ConfigError(
                f"{name} lists unknown provider {cleaned!r}; "
                f"valid names are {', '.join(KNOWN_PROVIDERS)}"
            )
        if cleaned not in names:
            names.append(cleaned)
    if not names:
        raise ConfigError(f"{name} is set but lists no valid providers")
    return tuple(names)


# ── Telegram ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = _int_env("OWNER_ID", 0, lo=0)

# ── Origin ────────────────────────────────────────────────
# Airports whose residents qualify for Spain's extra-peninsular discount.
# Reference only — ORIGIN can be any IATA code; this documents the eligible set
# and is used to warn when the configured origin does not qualify.
ELIGIBLE_ORIGINS = {
    # Canary Islands
    "LPA": "Gran Canaria",
    "TFN": "Tenerife Norte",
    "TFS": "Tenerife Sur",
    "ACE": "Lanzarote",
    "FUE": "Fuerteventura",
    "SPC": "La Palma",
    "GMZ": "La Gomera",
    "VDE": "El Hierro",
    # Balearic Islands
    "PMI": "Mallorca",
    "IBZ": "Ibiza",
    "MAH": "Menorca",
    # Ceuta and Melilla
    "MLN": "Melilla",
}

ORIGIN = os.getenv("ORIGIN", "LPA").strip().upper()

# ── Hub dictionaries ─────────────────────────────────────
SPAIN_HUBS = {
    "MAD": "Madrid",
    "BCN": "Barcelona",
    "AGP": "Malaga",
    "SVQ": "Sevilla",
    "VLC": "Valencia",
    "BIO": "Bilbao",
}

PORTUGAL_HUBS = {
    "LIS": "Lisboa",
    "OPO": "Oporto",
}

DEFAULT_HUBS = {**SPAIN_HUBS, **PORTUGAL_HUBS}

# ── Discount rule ────────────────────────────────────────
# The engine models a discount that applies to only *part* of an itinerary, so
# splitting the trip can beat the through-fare.
#
# The flagship case is Spain's "descuento de residente": residents of the
# extra-peninsular territories — the Canary Islands, the Balearic Islands,
# Ceuta and Melilla — get 75% off domestic flights to the peninsula. An
# international through-fare never applies it, so booking the domestic leg
# separately can beat the through-fare outright.
#
# Both knobs are configurable, so the same engine covers any partial-itinerary
# discount (other island or remote-region subsidies, corporate or loyalty fares
# valid only on one carrier's domestic network, etc.).
#
#   DISCOUNT_AIRPORTS — hubs whose leg from ORIGIN qualifies for the discount
#   DOMESTIC_DISCOUNT — fraction taken off that leg (0.75 = you pay 25%)
DISCOUNT_AIRPORTS = _codes_env("DISCOUNT_AIRPORTS", set(SPAIN_HUBS))
DOMESTIC_DISCOUNT = _float_env("DOMESTIC_DISCOUNT", 0.75, lo=0.0, hi=1.0)

# ── Scraper settings ─────────────────────────────────────
# Google's cookie-consent cookie. Not a secret; it only suppresses the consent
# interstitial so the response contains flight data.
SOCS_COOKIE = os.getenv(
    "SOCS_COOKIE",
    "CAISHAgCEhJnd3NfMjAyNTA2MjEtMF9SQzIaAmVzIAEaBgiAyMC6Bg",
)
# Seconds to wait between requests issued by a single worker.
DEFAULT_DELAY = _float_env("DEFAULT_DELAY", 2.5, lo=0.0, hi=60.0)
# How many requests may be in flight at once. Keep this low: it is the main
# lever on how hard the scraper hits Google.
MAX_CONCURRENCY = _int_env("MAX_CONCURRENCY", 4, lo=1)
# Per-request timeout and retry budget.
REQUEST_TIMEOUT = _float_env("REQUEST_TIMEOUT", 20.0, lo=1.0, hi=120.0)
MAX_RETRIES = _int_env("MAX_RETRIES", 2, lo=0)

# ── Providers ────────────────────────────────────────────
# Which sources to use, in preference order. PRIMARY drives search; the others
# are available for cross-checking. A Google-only configuration is supported
# and falls back to grid search, because Google has no price calendar.
PROVIDERS = _providers_env("PROVIDERS", ("kiwi", "google"))
PRIMARY_PROVIDER = os.getenv("PRIMARY_PROVIDER", "kiwi").strip().lower() or "kiwi"

# Kiwi tolerates far more load than scraping Google does, so it gets its own
# budget rather than sharing MAX_CONCURRENCY / DEFAULT_DELAY.
KIWI_CONCURRENCY = _int_env("KIWI_CONCURRENCY", 8, lo=1)
KIWI_DELAY = _float_env("KIWI_DELAY", 0.3, lo=0.0, hi=60.0)

# ── Kiwi provider ────────────────────────────────────────
# The GraphQL backend requires a partner identifier. It is not a secret, and
# an invalid one fails loudly with AppError("Partner not valid.") rather than
# degrading quietly, so it is safe to make configurable.
KIWI_PARTNER = os.getenv("KIWI_PARTNER", "skypicker").strip() or "skypicker"

# ── Database ─────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "flight_finder.db")

# ── Alert config ─────────────────────────────────────────
ALERT_INTERVAL_HOURS = _int_env("ALERT_INTERVAL_HOURS", 6, lo=1)
PRICE_DROP_THRESHOLD = _float_env("PRICE_DROP_THRESHOLD", 0.10, lo=0.0, hi=1.0)

# ── Engine tuning ────────────────────────────────────────
# The two-stage engine's own knobs. Defaults are the numbers the engine was
# measured against end to end, single provider, 8 hubs x 3 destinations,
# 91-day window: 93 requests one-way, 190 round-trip (14 days) -- phase 0
# (32 / 64) + phase 1 (58 / 120) + phase 2 (3 / 6). Changing one changes
# that request-count story, so do it deliberately.

# K — how many of phase 0's ranked candidates get confirmed against real
# offers in phase 1. Raising it finds more at the cost of more requests.
SHORTLIST_SIZE = _int_env("SHORTLIST_SIZE", 30, lo=1)
# Diversity caps applied to that shortlist, so one unusually cheap hub or
# date can't crowd out every other option.
MAX_PER_HUB = _int_env("MAX_PER_HUB", 6, lo=1)
MAX_PER_DATE = _int_env("MAX_PER_DATE", 4, lo=1)
# Distinct dates priced for the through-fare (single-ticket) baseline. Only
# a confirmed itinerary landing on one of those dates gets a savings figure;
# raising this shows one on more results, at the cost of more requests.
THROUGH_FARE_DATES = _int_env("THROUGH_FARE_DATES", 3, lo=1)
# Dates sampled by the grid fallback, for a provider with no price calendar.
FALLBACK_MAX_DATES = _int_env("FALLBACK_MAX_DATES", 12, lo=1)
# Upper bound on a search window, in days — the verified limit of Kiwi's
# price-calendar endpoint (see providers/kiwi.py). A window wider than this
# is more than the calendar the engine relies on can actually answer.
MAX_WINDOW_DAYS = _int_env("MAX_WINDOW_DAYS", 91, lo=1)


def validate() -> None:
    """Fail fast with an actionable message if required config is missing.

    Called from the bot entry point before the Telegram Application is built,
    so a misconfigured deployment errors out immediately instead of surfacing
    an opaque library error (or, worse, starting a bot nobody can talk to).
    """
    problems = []
    if not BOT_TOKEN:
        problems.append(
            "BOT_TOKEN is not set — create a bot with @BotFather and put the token in .env"
        )
    if not OWNER_ID:
        problems.append(
            "OWNER_ID is not set — message @userinfobot to get your numeric Telegram id. "
            "Without it the bot would reject every user, including you."
        )
    if PRIMARY_PROVIDER not in PROVIDERS:
        problems.append(
            f"PRIMARY_PROVIDER is {PRIMARY_PROVIDER!r} but PROVIDERS is "
            f"{', '.join(PROVIDERS)} — the primary provider must be enabled"
        )
    # A diversity cap larger than the shortlist it filters can never actually
    # trigger — every candidate already fits under it — so it silently does
    # nothing. That reads as a broken filter, not a harmless no-op.
    if MAX_PER_HUB > SHORTLIST_SIZE:
        problems.append(
            f"MAX_PER_HUB ({MAX_PER_HUB}) is greater than SHORTLIST_SIZE ({SHORTLIST_SIZE}) "
            "— it would never actually cap anything"
        )
    if MAX_PER_DATE > SHORTLIST_SIZE:
        problems.append(
            f"MAX_PER_DATE ({MAX_PER_DATE}) is greater than SHORTLIST_SIZE ({SHORTLIST_SIZE}) "
            "— it would never actually cap anything"
        )
    if problems:
        raise ConfigError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
            + "\n\nSee .env.example for the full list of settings."
        )
