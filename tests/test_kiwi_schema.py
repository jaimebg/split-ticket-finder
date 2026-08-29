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
    "AppError": {"message"},
    "ItineraryOneWay": {
        "id", "duration", "pnrCount", "price", "provider",
        "bagsInfo", "bookingOptions", "sector",
    },
    "ItineraryProvider": {"name"},
    "ItineraryBagsInfo": {"includedHandBags", "includedCheckedBags", "checkedBagTiers"},
    "BaggageTier": {"tierPrice"},
    "Money": {"amount"},
    "Sector": {"sectorSegments"},
    "SectorSegment": {"segment", "layover"},
    "Layover": {"duration", "isStationChange", "isBaggageRecheck"},
    "Segment": {"code", "duration", "carrier", "source", "destination"},
    "Carrier": {"code", "name"},
    "Stop": {"station", "localTime"},
    "Station": {"code", "name", "city"},
    "BookingOptionConnection": {"edges"},
    "BookingOptionEdge": {"node"},
    "BookingOption": {"bookingUrl", "price"},
    "PriceCalendarItem": {"date", "ratedPrice"},
    "RatedPrice": {"price", "rating"},
    "ItineraryPricesCalendar": {"currency", "calendar"},
    "Currency": {"code"},
    "Itineraries": {"itineraries"},
    "PlaceConnection": {"edges"},
    "PlaceEdge": {"node"},
    "Place": {"id", "legacyId", "name"},
    "City": {"name", "country"},
    "Country": {"name"},
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


def test_a_real_oneway_request_still_returns_itineraries():
    """End-to-end smoke test for search_leg's query.

    The field-level introspection above only covers output types. Every input
    type SearchOnewayInput/ItinerariesFilterInput/ItinerariesOptionsInput
    declare is otherwise unguarded, and a removed input field breaks the whole
    query -- taking search_leg fully dark -- rather than just one field of the
    response. Firing the real query end to end catches that far more cheaply
    than introspecting input types (which would need `inputFields`, not
    `fields`). This also exercises stopoverTime and excludeCarriers, which
    carry the hours filter and the design's named single point of breakage
    (options.partner) respectively.
    """
    from datetime import date, timedelta

    from providers.kiwi import ONEWAY_QUERY

    travel_date = date.today() + timedelta(days=30)
    variables = {
        "search": {
            "itinerary": {
                "source": {"ids": ["Station:airport:LPA"]},
                "destination": {"ids": ["Station:airport:MAD"]},
                "outboundDepartureDate": {
                    "start": f"{travel_date}T00:00:00",
                    "end": f"{travel_date}T23:59:59",
                },
            },
            "passengers": {"adults": 1, "children": 0},
            "cabinClass": {"cabinClass": "ECONOMY"},
        },
        "filter": {
            "limit": 5,
            "transportTypes": ["FLIGHT"],
            "maxStopsCount": 3,
            "stopoverTime": {"start": 1, "end": 48},
            "excludeCarriers": ["ZZ"],
        },
        "options": {
            "partner": "skypicker", "currency": "eur", "locale": "en", "sortBy": "PRICE",
        },
    }
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        response = client.post(
            f"{ENDPOINT}?featureName=DriftGuard",
            json={"query": ONEWAY_QUERY, "variables": variables},
        )
    node = (response.json().get("data") or {}).get("onewayItineraries")
    assert node is not None, "oneway query returned no data"
    assert node.get("__typename") == "Itineraries", node
    assert node["itineraries"], "itineraries came back empty for a route that has flights"


def test_a_real_places_request_still_returns_airports():
    """End-to-end smoke test for resolve_place's query.

    Covers PlacesFilterInput.onlyTypes, unguarded by the field introspection
    above, the same way the oneway smoke test covers its own inputs.
    """
    from providers.kiwi import PLACES_QUERY

    variables = {
        "search": {"term": "Tokyo"},
        "filter": {"onlyTypes": ["AIRPORT"]},
        "options": {"locale": "en"},
        "first": 5,
    }
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        response = client.post(
            f"{ENDPOINT}?featureName=DriftGuard",
            json={"query": PLACES_QUERY, "variables": variables},
        )
    node = (response.json().get("data") or {}).get("places")
    assert node is not None, "places query returned no data"
    assert node.get("__typename") == "PlaceConnection", node
    assert node["edges"], "places came back empty for a well-known city"
