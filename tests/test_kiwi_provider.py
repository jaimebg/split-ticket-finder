"""Tests for the Kiwi GraphQL client, against recorded responses."""
from __future__ import annotations

import httpx
import pytest

from providers.base import ProviderFetchError, ProviderParseError
from providers.kiwi import KiwiProvider


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
