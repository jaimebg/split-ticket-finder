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
    if problems:
        raise ConfigError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
            + "\n\nSee .env.example for the full list of settings."
        )
