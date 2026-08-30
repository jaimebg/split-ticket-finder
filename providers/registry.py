"""Provider selection.

Providers are singletons because each owns an httpx connection pool; building
a fresh one per search would re-do TLS for every request.
"""
from __future__ import annotations

from config import PRIMARY_PROVIDER, PROVIDERS, ConfigError
from providers.base import FlightProvider
from providers.google import GoogleProvider
from providers.kiwi import KiwiProvider

_BUILDERS = {
    "kiwi": KiwiProvider,
    "google": GoogleProvider,
}

_INSTANCES: dict[str, FlightProvider] = {}


def get_provider(name: str) -> FlightProvider:
    """Return the named provider, building it once and reusing it after."""
    key = name.strip().lower()
    if key not in _BUILDERS:
        raise ConfigError(
            f"unknown provider {name!r}; valid names are {', '.join(_BUILDERS)}"
        )
    if key not in _INSTANCES:
        _INSTANCES[key] = _BUILDERS[key]()
    return _INSTANCES[key]


def enabled_providers() -> dict[str, FlightProvider]:
    """Every configured provider, in preference order."""
    return {name: get_provider(name) for name in PROVIDERS}


def primary_provider() -> FlightProvider:
    """The provider that drives a search."""
    return get_provider(PRIMARY_PROVIDER)


async def close_all() -> None:
    """Release every held connection pool. Called on bot shutdown."""
    for provider in list(_INSTANCES.values()):
        await provider.aclose()
    _INSTANCES.clear()
