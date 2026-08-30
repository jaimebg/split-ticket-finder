"""Tests for the Kiwi GraphQL client, against recorded responses."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from providers.base import (
    CalendarQuery,
    LegQuery,
    Place,
    ProviderFetchError,
    ProviderParseError,
    SupportsCalendar,
    SupportsPlaces,
)
from providers.kiwi import (
    KiwiProvider,
    _booking_url,
    _minutes,
    _money,
    _place_id,
    _require,
)


def _provider(handler) -> KiwiProvider:
    """A KiwiProvider whose HTTP calls are served by *handler*."""
    transport = httpx.MockTransport(handler)
    return KiwiProvider(client=httpx.AsyncClient(transport=transport))


def test_provider_name():
    assert KiwiProvider().name == "kiwi"


async def test_execute_returns_the_root_node_on_success(kiwi_fixture):
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    node = await _provider(handler)._execute(
        "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
    )
    assert node["__typename"] == "ItineraryPricesCalendar"
    assert len(node["calendar"]) == 30


async def test_app_error_raises_parse_error_despite_http_200(kiwi_fixture):
    """AppError arrives as HTTP 200. Branching on status code would miss it."""
    payload = kiwi_fixture("app_error")

    def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderParseError, match="Partner not valid"):
        await _provider(handler)._execute(
            "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
        )


async def test_graphql_errors_field_raises_parse_error():
    """A GraphQL validation error means the schema moved under us."""
    def handler(request):
        return httpx.Response(200, json={
            "data": None,
            "errors": [{"message": "Field amount doesn't exist on Root"}],
        })

    with pytest.raises(ProviderParseError, match="amount"):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_missing_root_field_raises_parse_error():
    def handler(request):
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(ProviderParseError):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_non_json_body_raises_parse_error():
    def handler(request):
        return httpx.Response(200, text="<html>rate limited</html>")

    with pytest.raises(ProviderParseError):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_non_retryable_status_raises_fetch_error():
    def handler(request):
        return httpx.Response(403)

    with pytest.raises(ProviderFetchError, match="403"):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_retries_then_succeeds(monkeypatch, kiwi_fixture):
    import providers.kiwi as kiwi

    async def no_sleep(_):
        return None

    monkeypatch.setattr(kiwi.asyncio, "sleep", no_sleep)
    calls = {"n": 0}
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=payload)

    node = await _provider(handler)._execute(
        "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
    )
    assert calls["n"] == 3
    assert node["__typename"] == "ItineraryPricesCalendar"


async def test_gives_up_after_the_retry_budget(monkeypatch):
    import providers.kiwi as kiwi

    async def no_sleep(_):
        return None

    monkeypatch.setattr(kiwi.asyncio, "sleep", no_sleep)

    def handler(request):
        return httpx.Response(503)

    with pytest.raises(ProviderFetchError, match="giving up"):
        await _provider(handler)._execute("Op", "query {}", {}, "onewayItineraries")


async def test_operation_name_travels_as_the_feature_name_query_param(kiwi_fixture):
    seen = {}
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=payload)

    await _provider(handler)._execute(
        "PricesCalendar", "query {}", {}, "itineraryPricesCalendar"
    )
    assert "featureName=PricesCalendar" in seen["url"]


# ── Leg search ───────────────────────────────────────────────────────────────


def test_place_id_is_derived_not_looked_up():
    assert _place_id("lpa") == "Station:airport:LPA"


def test_money_parses_strings_to_decimal():
    assert _money("174.303303") == Decimal("174.303303")
    assert _money("29") == Decimal("29")


def test_money_rejects_junk_loudly():
    with pytest.raises(ProviderParseError):
        _money("not-a-price")
    with pytest.raises(ProviderParseError):
        _money(None)


def test_minutes_converts_from_seconds():
    """Kiwi reports every duration in seconds; the rest of the app uses minutes."""
    assert _minutes(10200) == 170
    assert _minutes(0) == 0


def test_booking_url_is_absolutised():
    assert _booking_url("/en/booking/?x=1") == "https://www.kiwi.com/en/booking/?x=1"
    assert _booking_url("https://www.kiwi.com/en/booking/?x=1") == (
        "https://www.kiwi.com/en/booking/?x=1"
    )
    assert _booking_url(None) is None
    assert _booking_url("") is None


async def test_search_leg_maps_a_direct_flight(kiwi_fixture):
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    offers = await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    )

    assert len(offers) == 5
    assert [o.price for o in offers] == [Decimal(x) for x in ("29", "41", "43", "44", "45")]

    first = offers[0]
    assert first.provider == "kiwi"
    assert first.currency == "EUR"
    assert first.duration == 170                     # 10200 seconds
    assert first.stops == 0
    assert first.pnr_count == 1
    assert first.airlines == ["Ryanair"]
    assert first.booking_url.startswith("https://www.kiwi.com/en/booking/")
    # A direct flight has no connection to measure.
    assert first.min_layover is None

    seg = first.segments[0]
    assert (seg.origin, seg.dest) == ("LPA", "MAD")
    assert seg.carrier == "FR"
    assert seg.carrier_name == "Ryanair"
    assert seg.flight_no == "FR2012"
    assert seg.dep_local == datetime(2026, 10, 6, 8, 30)
    assert seg.arr_local == datetime(2026, 10, 6, 12, 20)


async def test_search_leg_reports_baggage_as_known_zero_not_unknown(kiwi_fixture):
    """Kiwi CAN report baggage, so 0 included bags is a fact, not a gap."""
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    ))[0]

    assert first.included_checked_bags == 0
    assert first.included_cabin_bags == 0
    assert first.checked_bag_price == Decimal("34.99")   # cheapest tier


async def test_search_leg_maps_a_multi_segment_self_transfer(kiwi_fixture):
    """Four segments, three separate bookings, three layovers."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="MAD", dest="NRT", date="2026-10-06")
    ))[0]

    assert first.price == Decimal("552")
    assert len(first.segments) == 4
    assert first.stops == 3
    assert first.pnr_count == 3
    assert first.min_layover == 100                  # 6000 seconds, the shortest
    assert first.checked_bag_price == Decimal("174.303303")
    assert [s.origin for s in first.segments] == ["MAD", "AUH", "KUL", "KHH"]
    assert first.segments[-1].dest == "NRT"


async def test_search_leg_returns_empty_list_when_there_are_no_itineraries():
    """No flights is a normal outcome and must not raise."""
    def handler(request):
        return httpx.Response(200, json={
            "data": {"onewayItineraries": {"__typename": "Itineraries", "itineraries": []}}
        })

    offers = await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="ZZZ", date="2026-10-06")
    )
    assert offers == []


async def test_search_leg_sends_filters_with_stopover_time_in_hours(kiwi_fixture):
    """stopoverTime is in HOURS. Seconds or minutes silently return nothing."""
    seen = {}
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).search_leg(LegQuery(
        origin="LPA", dest="MAD", date="2026-10-06",
        limit=3, max_stops=1, min_layover=180, exclude_carriers=("EY", "AK"),
    ))

    flt = seen["body"]["variables"]["filter"]
    assert flt["limit"] == 3
    assert flt["maxStopsCount"] == 1
    assert flt["stopoverTime"]["start"] == 3          # 180 minutes -> 3 hours
    assert flt["excludeCarriers"] == ["EY", "AK"]
    assert flt["transportTypes"] == ["FLIGHT"]


async def test_search_leg_sends_the_date_as_a_full_day_window(kiwi_fixture):
    seen = {}
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    )

    itin = seen["body"]["variables"]["search"]["itinerary"]
    assert itin["source"]["ids"] == ["Station:airport:LPA"]
    assert itin["destination"]["ids"] == ["Station:airport:MAD"]
    assert itin["outboundDepartureDate"] == {
        "start": "2026-10-06T00:00:00", "end": "2026-10-06T23:59:59",
    }


# ── Layover threshold enforcement (fix round 1, finding 1) ──────────────────
#
# filter.stopoverTime only has hour granularity, so the API can return
# connections shorter than what was asked for (e.g. min_layover=90 sends
# start=1, which admits 60-89 minute connections too). search_leg must
# enforce the exact minute threshold itself, after mapping.


async def test_search_leg_drops_offers_under_the_exact_minute_threshold(kiwi_fixture):
    """The API filter only guarantees whole hours; search_leg enforces minutes.

    The fixture holds three itineraries with min_layover 100, 125 and 80
    minutes (prices 552, 566, 573 respectively).
    """
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")

    def handler(request):
        return httpx.Response(200, json=payload)

    kept = await _provider(handler).search_leg(LegQuery(
        origin="MAD", dest="NRT", date="2026-10-06", min_layover=90,
    ))
    # The 80-minute connection is dropped; the 100- and 125-minute ones stay.
    assert [o.min_layover for o in kept] == [100, 125]
    assert all(o.min_layover >= 90 for o in kept)

    dropped = await _provider(handler).search_leg(LegQuery(
        origin="MAD", dest="NRT", date="2026-10-06", min_layover=200,
    ))
    assert dropped == []


async def test_search_leg_keeps_direct_flights_regardless_of_min_layover(kiwi_fixture):
    """A direct flight has no connection, so it trivially satisfies any minimum."""
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    offers = await _provider(handler).search_leg(LegQuery(
        origin="LPA", dest="MAD", date="2026-10-06", min_layover=180,
    ))
    assert len(offers) == 5
    assert all(o.min_layover is None for o in offers)


# ── Layover shape invariant (fix round 2, finding 2) ────────────────────────
#
# A layover shaped {} or {"duration": null} is silently skipped when building
# `layovers`, so a fully-degraded set of layovers would leave min_layover as
# None -- which the filter above would then read as "direct flight" and keep,
# letting a multi-stop self-transfer straight through a min_layover request.
# _to_offer must instead notice the count mismatch (len(layovers) must equal
# len(segments) - 1) and raise, rather than let the filter be silently
# disabled by degraded data.


async def test_search_leg_raises_when_every_layover_is_malformed(kiwi_fixture):
    """All layovers shaped {"duration": null} must raise, not silently pass the filter."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")
    itinerary = payload["data"]["onewayItineraries"]["itineraries"][0]
    for entry in itinerary["sector"]["sectorSegments"]:
        if entry.get("layover") is not None:
            entry["layover"] = {"duration": None}

    def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderParseError, match="layover count mismatch"):
        await _provider(handler).search_leg(LegQuery(
            origin="MAD", dest="NRT", date="2026-10-06", min_layover=180,
        ))


# ── Baggage re-check (bag-recheck task) ──────────────────────────────────────
#
# ONEWAY_QUERY already selects Layover.isBaggageRecheck; it answers whether a
# self-transfer forces the traveller to re-claim and re-check bags at the hub.


async def test_search_leg_reports_baggage_recheck_on_self_transfer(kiwi_fixture):
    """isBaggageRecheck is already in the payload; it must reach the Offer."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="MAD", dest="NRT", date="2026-10-06")
    ))[0]
    # The recorded itinerary has isBaggageRecheck True on two of its layovers.
    assert first.requires_bag_recheck is True


async def test_search_leg_reports_no_recheck_when_no_layover_needs_one(kiwi_fixture):
    payload = kiwi_fixture("oneway_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="LPA", dest="MAD", date="2026-10-06")
    ))[0]
    # A direct flight has no connection, so there is nothing to re-check.
    assert first.requires_bag_recheck is False


# ── Baggage re-check aggregation (fix round 1, finding 1) ───────────────────
#
# isBaggageRecheck is a nullable Boolean in Kiwi's schema, so a layover can
# report null (or omit the field) as well as True/False. Collapsing "unknown"
# into False would under-warn about a self-transfer hazard -- the exact
# defect this task exists to fix, reproduced one level down. The aggregation
# rule, in priority order: True beats None (unknown) beats False; no
# layovers at all (a direct flight) is False.


async def test_search_leg_reports_unknown_recheck_when_its_only_layover_is_null(
    kiwi_fixture,
):
    """A lone layover reporting null must yield None, not the False default."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")
    itinerary = payload["data"]["onewayItineraries"]["itineraries"][0]
    segments = itinerary["sector"]["sectorSegments"]
    itinerary["sector"]["sectorSegments"] = segments[:2]
    segments[1]["layover"]["isBaggageRecheck"] = None

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="MAD", dest="NRT", date="2026-10-06")
    ))[0]
    assert first.requires_bag_recheck is None


async def test_search_leg_lets_a_confirmed_recheck_win_over_an_unknown_one(
    kiwi_fixture,
):
    """One True and one null layover must yield True: a confirmed hazard wins."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")
    itinerary = payload["data"]["onewayItineraries"]["itineraries"][0]
    segments = itinerary["sector"]["sectorSegments"]
    itinerary["sector"]["sectorSegments"] = segments[:3]
    segments[1]["layover"]["isBaggageRecheck"] = None
    # segments[2]["layover"]["isBaggageRecheck"] is already True in the fixture.

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="MAD", dest="NRT", date="2026-10-06")
    ))[0]
    assert first.requires_bag_recheck is True


async def test_search_leg_reports_no_recheck_when_every_layover_says_so(kiwi_fixture):
    """Every layover explicitly reporting False must yield False, not None."""
    payload = kiwi_fixture("oneway_mad_nrt_multisegment")
    itinerary = payload["data"]["onewayItineraries"]["itineraries"][0]
    for entry in itinerary["sector"]["sectorSegments"]:
        if entry.get("layover") is not None:
            entry["layover"]["isBaggageRecheck"] = False

    def handler(request):
        return httpx.Response(200, json=payload)

    first = (await _provider(handler).search_leg(
        LegQuery(origin="MAD", dest="NRT", date="2026-10-06")
    ))[0]
    assert first.requires_bag_recheck is False


# ── Broken vs. empty (fix round 1, finding 2) ────────────────────────────────
#
# "Empty list means no flights, exception means broken" is the whole point of
# this error taxonomy. These two tests cover the raise paths that were
# previously untested: a missing (not merely empty) itineraries key, and
# _require's own guard.


async def test_search_leg_raises_when_itineraries_key_is_entirely_missing():
    """Missing itineraries is a schema change, not an empty result -- must raise."""
    def handler(request):
        return httpx.Response(200, json={
            "data": {"onewayItineraries": {"__typename": "Itineraries"}}
        })

    with pytest.raises(ProviderParseError):
        await _provider(handler).search_leg(
            LegQuery(origin="LPA", dest="ZZZ", date="2026-10-06")
        )


def test_require_raises_and_names_the_missing_path():
    with pytest.raises(ProviderParseError, match=r"sector\.sectorSegments"):
        _require({"sector": {}}, "sector", "sectorSegments")


# ── Price calendar ───────────────────────────────────────────────────────────


def test_kiwi_advertises_the_calendar_capability():
    assert isinstance(KiwiProvider(), SupportsCalendar)


async def test_price_calendar_maps_a_month(kiwi_fixture):
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        return httpx.Response(200, json=payload)

    prices = await _provider(handler).price_calendar(
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
    )

    assert len(prices) == 30
    # Keys are plain dates, not the timestamps the API returns.
    assert "2026-10-01" in prices
    assert prices["2026-10-01"].price == Decimal("29")
    assert prices["2026-10-01"].rating == "AVERAGE"
    assert all(isinstance(v.price, Decimal) for v in prices.values())


async def test_price_calendar_returns_empty_for_an_unknown_airport(kiwi_fixture):
    """An unknown airport yields an empty calendar, which is data, not an error."""
    payload = kiwi_fixture("calendar_empty")

    def handler(request):
        return httpx.Response(200, json=payload)

    prices = await _provider(handler).price_calendar(
        CalendarQuery(origin="ZZZ", dest="MAD", start="2026-10-01", end="2026-10-05")
    )
    assert prices == {}


async def test_price_calendar_sends_a_datetime_window(kiwi_fixture):
    """Plain YYYY-MM-DD is rejected by the API; it wants DateTime."""
    seen = {}
    payload = kiwi_fixture("calendar_lpa_mad")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).price_calendar(
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
    )

    search = seen["body"]["variables"]["search"]
    assert search["dates"] == {
        "start": "2026-10-01T00:00:00", "end": "2026-10-31T00:00:00",
    }
    assert search["source"]["ids"] == ["Station:airport:LPA"]


async def test_price_calendar_tolerates_a_day_with_no_price(kiwi_fixture):
    """A null ratedPrice is a day with no flights, so it is simply absent."""
    payload = kiwi_fixture("calendar_lpa_mad")
    payload["data"]["itineraryPricesCalendar"]["calendar"][0]["ratedPrice"] = None

    def handler(request):
        return httpx.Response(200, json=payload)

    prices = await _provider(handler).price_calendar(
        CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
    )
    assert len(prices) == 29
    assert "2026-10-01" not in prices


async def test_price_calendar_raises_when_a_priced_day_has_no_date(kiwi_fixture):
    """A priced day with no date is broken data, not a normal absence -- it must raise.

    Contrast with test_price_calendar_tolerates_a_day_with_no_price above: a
    missing *price* is a normal absence and is skipped silently, but a real
    price with no date can't be keyed at all, so silently dropping it would
    lose a priced day from the calendar.
    """
    payload = kiwi_fixture("calendar_lpa_mad")
    payload["data"]["itineraryPricesCalendar"]["calendar"][0]["date"] = None

    def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderParseError):
        await _provider(handler).price_calendar(
            CalendarQuery(origin="LPA", dest="MAD", start="2026-10-01", end="2026-10-31")
        )


async def test_price_calendar_raises_when_calendar_key_is_entirely_missing():
    """Missing calendar (not merely empty) is a schema change, not an empty result."""
    def handler(request):
        return httpx.Response(200, json={
            "data": {"itineraryPricesCalendar": {"__typename": "ItineraryPricesCalendar"}}
        })

    with pytest.raises(ProviderParseError):
        await _provider(handler).price_calendar(
            CalendarQuery(origin="LPA", dest="ZZZ", start="2026-10-01", end="2026-10-05")
        )


# ── Place search ─────────────────────────────────────────────────────────────


def test_kiwi_advertises_the_places_capability():
    assert isinstance(KiwiProvider(), SupportsPlaces)


async def test_resolve_place_returns_airports(kiwi_fixture):
    payload = kiwi_fixture("places_tokyo")

    def handler(request):
        return httpx.Response(200, json=payload)

    places = await _provider(handler).resolve_place("Tokyo")

    assert all(isinstance(p, Place) for p in places)
    assert [p.code for p in places] == ["NRT", "HND", "TJH"]
    assert places[0].name == "Narita International"
    assert places[0].city == "Tokyo"
    assert places[0].country == "Japan"
    assert places[0].place_id == "Station:airport:NRT"


async def test_resolve_place_handles_a_single_match(kiwi_fixture):
    payload = kiwi_fixture("places_gran_canaria")

    def handler(request):
        return httpx.Response(200, json=payload)

    places = await _provider(handler).resolve_place("Gran Canaria")
    assert len(places) == 1
    assert places[0].code == "LPA"


async def test_resolve_place_returns_empty_for_no_match():
    def handler(request):
        return httpx.Response(200, json={
            "data": {"places": {"__typename": "PlaceConnection", "edges": []}}
        })

    assert await _provider(handler).resolve_place("qqqqqq") == []


async def test_resolve_place_raises_when_edges_key_is_entirely_missing():
    """Missing edges (not merely empty) is a schema change, not an empty result.

    The equivalents for onewayItineraries and itineraryPricesCalendar are
    test_search_leg_raises_when_itineraries_key_is_entirely_missing and
    test_price_calendar_raises_when_calendar_key_is_entirely_missing; this is
    the third instance of the same pattern for `places`.
    """
    def handler(request):
        return httpx.Response(200, json={
            "data": {"places": {"__typename": "PlaceConnection"}}
        })

    with pytest.raises(ProviderParseError):
        await _provider(handler).resolve_place("Tokyo")


async def test_resolve_place_passes_term_and_limit(kiwi_fixture):
    seen = {}
    payload = kiwi_fixture("places_tokyo")

    def handler(request):
        import json as _json
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=payload)

    await _provider(handler).resolve_place("Tokyo", limit=3)
    variables = seen["body"]["variables"]
    assert variables["search"]["term"] == "Tokyo"
    assert variables["first"] == 3
    assert variables["filter"]["onlyTypes"] == ["AIRPORT"]


async def test_resolve_place_skips_nodes_without_an_iata_code(kiwi_fixture):
    """Non-airport nodes have no code and are not selectable origins."""
    payload = kiwi_fixture("places_tokyo")
    payload["data"]["places"]["edges"][1]["node"]["code"] = None

    def handler(request):
        return httpx.Response(200, json=payload)

    places = await _provider(handler).resolve_place("Tokyo")
    assert [p.code for p in places] == ["NRT", "TJH"]
