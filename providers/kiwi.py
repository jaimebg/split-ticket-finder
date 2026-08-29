"""Kiwi.com provider, over the GraphQL backend that serves kiwi.com itself.

The official Tequila API closed to new partners in May 2024, so this speaks to
api.skypicker.com directly. It needs no credentials but does require a valid
options.partner value, and it signals failure in a way that makes the error
handling here load-bearing:

  * An AppError arrives as HTTP 200 with __typename == "AppError". Anything
    branching on status codes alone will read a rejected partner key as
    success.
  * An unknown airport returns an *empty* result, not an error. That is a
    legitimate "no flights" and must not raise.

So: empty list means no flights, exception means broken, and never the reverse.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

from config import (
    KIWI_PARTNER,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
)
from providers.base import ProviderFetchError, ProviderParseError

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.skypicker.com/umbrella/v2/graphql"
SITE_BASE = "https://www.kiwi.com"

HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "origin": SITE_BASE,
    "referer": f"{SITE_BASE}/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Rate limiting and transient server errors are worth retrying; nothing else is.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def build_client() -> httpx.AsyncClient:
    """One client per provider instance, so requests reuse connections."""
    return httpx.AsyncClient(headers=HEADERS, timeout=REQUEST_TIMEOUT)


class KiwiProvider:
    """Kiwi.com behind the shared provider interface.

    Implements FlightProvider, SupportsCalendar and SupportsPlaces.
    """

    name = "kiwi"

    def __init__(self, client: httpx.AsyncClient | None = None, partner: str | None = None):
        self._client = client
        self._owns_client = client is None
        self._partner = partner or KIWI_PARTNER

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client()
        return self._client

    def _options(self, currency: str, extra: dict | None = None) -> dict:
        options = {
            "partner": self._partner,
            "currency": currency.lower(),
            "locale": "en",
        }
        if extra:
            options.update(extra)
        return options

    async def _execute(self, operation: str, query: str, variables: dict, root: str) -> dict:
        """POST one GraphQL operation and return its root node.

        Raises ProviderFetchError when the request never landed, and
        ProviderParseError when something came back that we cannot trust --
        including an AppError, which is an HTTP 200.
        """
        client = await self._get_client()
        url = f"{ENDPOINT}?featureName={operation}"
        body = {"query": query, "variables": variables}
        last_error = "unknown error"

        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                # Exponential backoff with jitter, so parallel workers that hit
                # a rate limit together do not retry in lockstep.
                delay = 2**attempt + random.uniform(0, 1)
                logger.info(
                    "Retrying %s in %.1fs (attempt %d): %s",
                    operation, delay, attempt + 1, last_error,
                )
                await asyncio.sleep(delay)

            try:
                response = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                raise ProviderFetchError(f"{operation}: HTTP {response.status_code}")

            return self._unwrap(operation, response, root)

        raise ProviderFetchError(
            f"{operation}: giving up after {MAX_RETRIES + 1} attempts ({last_error})"
        )

    @staticmethod
    def _unwrap(operation: str, response: httpx.Response, root: str) -> dict:
        """Pull the root node out of a 200 response, or explain why we cannot."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderParseError(
                f"{operation}: response body is not JSON ({exc})"
            ) from exc

        if payload.get("errors"):
            # GraphQL validation failed, which means the schema moved under us.
            messages = "; ".join(
                str(e.get("message", e)) for e in payload["errors"][:3]
            )
            raise ProviderParseError(f"{operation}: GraphQL errors: {messages}")

        node = (payload.get("data") or {}).get(root)
        if node is None:
            raise ProviderParseError(f"{operation}: response carries no {root!r} field")

        if node.get("__typename") == "AppError":
            raise ProviderParseError(
                f"{operation}: {node.get('message', 'AppError with no message')}"
            )

        return node

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
