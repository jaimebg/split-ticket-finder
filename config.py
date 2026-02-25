import os

from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# ── Origin ────────────────────────────────────────────────
ORIGIN = os.getenv("ORIGIN", "LPA")

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

# ── Discount settings ────────────────────────────────────
DISCOUNT_AIRPORTS = set(SPAIN_HUBS.keys())
DOMESTIC_DISCOUNT = float(os.getenv("DOMESTIC_DISCOUNT", "0.85"))

# ── Scraper settings ─────────────────────────────────────
SOCS_COOKIE = os.getenv(
    "SOCS_COOKIE",
    "CAISHAgCEhJnd3NfMjAyNTA2MjEtMF9SQzIaAmVzIAEaBgiAyMC6Bg",
)
DEFAULT_DELAY = float(os.getenv("DEFAULT_DELAY", "2.5"))

# ── Alert config ─────────────────────────────────────────
ALERT_INTERVAL_MINUTES = int(os.getenv("ALERT_INTERVAL_MINUTES", "60"))
