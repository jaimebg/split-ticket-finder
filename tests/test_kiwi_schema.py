"""Drift guard: asserts the live Kiwi schema still has the fields we read.

The client depends on undocumented response shapes. Introspection is open, so
this turns "did Kiwi change?" into one command. It is the only test that
touches the network and is deselected by default:

    pytest -m network
"""
from __future__ import annotations

import httpx
import pytest

from providers.kiwi import ENDPOINT, HEADERS

pytestmark = pytest.mark.network

# Every type we read from, and the fields we read off it.
EXPECTED = {
    "RootQuery": {"onewayItineraries", "itineraryPricesCalendar", "places"},
    "ItineraryOneWay": {
        "id", "duration", "pnrCount", "price", "provider",
        "bagsInfo", "bookingOptions", "sector",
    },
    "ItineraryBagsInfo": {"includedHandBags", "includedCheckedBags", "checkedBagTiers"},
    "BaggageTier": {"tierPrice"},
    "SectorSegment": {"segment", "layover"},
    "Layover": {"duration", "isStationChange", "isBaggageRecheck"},
    "Segment": {"code", "duration", "carrier", "source", "destination"},
    "Stop": {"station", "localTime"},
    "Station": {"code", "name", "city"},
    "PriceCalendarItem": {"date", "ratedPrice"},
    "ItineraryPricesCalendar": {"currency", "calendar"},
    "PlaceConnection": {"edges"},
}

INTROSPECT = """query Drift($name: String!) {
  __type(name: $name) { name fields { name } }
}"""


def _fields(client: httpx.Client, type_name: str) -> set[str]:
    response = client.post(
        f"{ENDPOINT}?featureName=DriftGuard",
        json={"query": INTROSPECT, "variables": {"name": type_name}},
    )
    response.raise_for_status()
    node = (response.json().get("data") or {}).get("__type")
    assert node is not None, f"type {type_name!r} no longer exists"
    return {f["name"] for f in (node.get("fields") or [])}


@pytest.mark.parametrize(("type_name", "expected"), sorted(EXPECTED.items()))
def test_expected_fields_still_exist(type_name, expected):
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        actual = _fields(client, type_name)
    missing = expected - actual
    assert not missing, f"{type_name} lost fields the client reads: {sorted(missing)}"


def test_a_real_calendar_request_still_returns_prices():
    """End-to-end smoke test: the whole path still yields usable data."""
    from datetime import date, timedelta

    from providers.kiwi import CALENDAR_QUERY

    start = date.today() + timedelta(days=30)
    end = start + timedelta(days=10)
    variables = {
        "search": {
            "source": {"ids": ["Station:airport:LPA"]},
            "destination": {"ids": ["Station:airport:MAD"]},
            "dates": {"start": f"{start}T00:00:00", "end": f"{end}T00:00:00"},
            "passengers": {"adults": 1},
        },
        "filter": {"transportTypes": ["FLIGHT"]},
        "options": {"partner": "skypicker", "currency": "eur", "locale": "en"},
    }
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        response = client.post(
            f"{ENDPOINT}?featureName=DriftGuard",
            json={"query": CALENDAR_QUERY, "variables": variables},
        )
    node = (response.json().get("data") or {}).get("itineraryPricesCalendar")
    assert node is not None, "calendar query returned no data"
    assert node.get("__typename") == "ItineraryPricesCalendar", node
    assert node["calendar"], "calendar came back empty for a route that has flights"
