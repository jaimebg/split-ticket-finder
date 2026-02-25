import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import SOCS_COOKIE


# ============================================================
# Protobuf URL encoder
# ============================================================

def _varint(value):
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _field(fn, wt, data):
    return _varint((fn << 3) | wt) + data


def _str(fn, val):
    e = val.encode("utf-8")
    return _field(fn, 2, _varint(len(e)) + e)


def _bytes(fn, val):
    return _field(fn, 2, _varint(len(val)) + val)


def _var(fn, val):
    return _field(fn, 0, _varint(val))


def encode_tfs(from_apt, to_apt, date, adults=1):
    apt_from = _var(1, 1) + _str(2, from_apt)
    apt_to = _var(1, 1) + _str(2, to_apt)
    flt = _str(2, date) + _bytes(13, apt_from) + _bytes(14, apt_to)

    msg = bytearray()
    msg += _var(1, 28)
    msg += _var(2, 2)                         # one-way
    msg += _bytes(3, bytes(flt))
    msg += _var(3, 1)
    msg += _var(8, 1)                         # economy
    msg += _var(9, 1)
    msg += _var(14, 1)
    pax = b""
    for _ in range(adults):
        pax += _var(1, 1)
    msg += _bytes(16, pax)
    msg += _bytes(18, _var(1, 1))
    return base64.b64encode(bytes(msg)).decode("utf-8").rstrip("=")


def build_url(from_apt, to_apt, date, adults=1, currency="EUR"):
    tfs = encode_tfs(from_apt, to_apt, date, adults)
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&hl=es&curr={currency}"


# ============================================================
# Data classes
# ============================================================

@dataclass
class FlightResult:
    price: int
    airlines: list[str]
    stops: int
    duration: int               # total minutes
    segments: list[dict] = field(default_factory=list)

    @property
    def route_str(self):
        if not self.segments:
            return "?"
        parts = [s["from"] for s in self.segments] + [self.segments[-1]["to"]]
        return " -> ".join(parts)


@dataclass
class Route:
    date: str
    hub: str
    hub_name: str
    dest: str
    dest_name: str
    dom_price: int
    dom_discounted: float
    intl_price: int
    total: float
    dom_airlines: list[str] = field(default_factory=list)
    dom_stops: int = 0
    dom_dur: int = 0
    intl_airlines: list[str] = field(default_factory=list)
    intl_stops: int = 0
    intl_dur: int = 0


# ============================================================
# Helpers
# ============================================================

def fmt_dur(m):
    if m <= 0:
        return "?"
    h, r = divmod(m, 60)
    return f"{h}h{r:02d}m"


def generate_dates(start, end, every):
    """Generate dates from start to end, every N days."""
    dates = []
    cur = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    while cur <= stop:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=every)
    return dates


# ============================================================
# Parser
# ============================================================

def _parse_offer(offer):
    try:
        fi = offer[0]
        if not isinstance(fi, list) or len(fi) < 3:
            return None

        airlines = fi[1] if isinstance(fi[1], list) else []
        raw_segs = fi[2] if isinstance(fi[2], list) else []

        segments = []
        for seg in raw_segs:
            if not isinstance(seg, list) or len(seg) < 12:
                continue
            s = {
                "from": seg[3] if isinstance(seg[3], str) else "?",
                "to": seg[6] if isinstance(seg[6], str) else "?",
                "dep_time": seg[8] if isinstance(seg[8], list) else [],
                "arr_time": seg[10] if isinstance(seg[10], list) else [],
                "duration": seg[11] if isinstance(seg[11], (int, float)) else 0,
            }
            if len(seg) > 17 and isinstance(seg[17], str):
                s["plane"] = seg[17]
            if len(seg) > 22 and isinstance(seg[22], list) and len(seg[22]) >= 2:
                s["flight"] = f"{seg[22][0]}{seg[22][1]}"
            segments.append(s)

        total_dur = fi[8] if len(fi) > 8 and isinstance(fi[8], (int, float)) else 0
        if not total_dur and segments:
            total_dur = sum(s["duration"] for s in segments)

        price = 0
        if isinstance(offer[1], list):
            p = offer[1][0] if isinstance(offer[1][0], list) else offer[1]
            if isinstance(p, list) and len(p) > 1 and isinstance(p[1], (int, float)):
                price = int(p[1])
        if price <= 0:
            return None

        return FlightResult(
            price=price,
            airlines=airlines,
            stops=max(0, len(segments) - 1),
            duration=int(total_dur),
            segments=segments,
        )
    except (IndexError, TypeError):
        return None


def parse_flights(html):
    if not html or len(html) < 1000:
        return []
    m = re.search(r'<script[^>]*class="ds:1"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return []
    dm = re.search(r'data:(.*?)(?:,\s*sideChannel|\}\s*\)\s*;?\s*$)', m.group(1), re.DOTALL)
    if not dm:
        return []
    try:
        data = json.loads(dm.group(1).strip())
    except json.JSONDecodeError:
        return []

    results = []
    for section in (
        lambda: data[2][0],    # "best" offers
        lambda: data[3][0],    # "other" offers
    ):
        try:
            offers = section()
            if isinstance(offers, list):
                for o in offers:
                    f = _parse_offer(o)
                    if f:
                        results.append(f)
        except (IndexError, TypeError):
            pass

    results.sort(key=lambda f: f.price)
    return results


# ============================================================
# Async scraper
# ============================================================

async def fetch_html(from_apt, to_apt, date, adults=1, currency="EUR"):
    """Fetch Google Flights HTML using curl as an async subprocess."""
    url = build_url(from_apt, to_apt, date, adults, currency)
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "--compressed",
        "-H", "accept: text/html,application/xhtml+xml,application/xml;q=0.9",
        "-H", "accept-language: es-ES,es;q=0.9,en;q=0.8",
        "-H", (
            "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "-H", f"cookie: SOCS={SOCS_COOKIE}",
        "--max-time", "20",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")


async def search(from_apt, to_apt, date, adults=1, currency="EUR"):
    """Search flights and return parsed results."""
    html = await fetch_html(from_apt, to_apt, date, adults, currency)
    return parse_flights(html)
