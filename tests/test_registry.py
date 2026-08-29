"""Tests for provider selection and configuration validation."""
from __future__ import annotations

import pytest

import config
from providers import registry
from providers.base import SupportsCalendar
from providers.google import GoogleProvider
from providers.kiwi import KiwiProvider


@pytest.fixture(autouse=True)
def _clear_registry():
    registry._INSTANCES.clear()
    yield
    registry._INSTANCES.clear()


def test_get_provider_returns_the_right_class():
    assert isinstance(registry.get_provider("kiwi"), KiwiProvider)
    assert isinstance(registry.get_provider("google"), GoogleProvider)


def test_get_provider_is_a_singleton_per_name():
    """One instance per provider, so its connection pool is actually reused."""
    assert registry.get_provider("kiwi") is registry.get_provider("kiwi")


def test_get_provider_rejects_an_unknown_name():
    with pytest.raises(config.ConfigError, match="nope"):
        registry.get_provider("nope")


def test_enabled_providers_follows_config_order(monkeypatch):
    monkeypatch.setattr(config, "PROVIDERS", ("google", "kiwi"))
    monkeypatch.setattr(registry, "PROVIDERS", ("google", "kiwi"))
    assert list(registry.enabled_providers()) == ["google", "kiwi"]


def test_primary_provider_uses_the_configured_name(monkeypatch):
    monkeypatch.setattr(registry, "PRIMARY_PROVIDER", "google")
    assert isinstance(registry.primary_provider(), GoogleProvider)


def test_only_kiwi_advertises_calendar_support():
    """This is the check the engine uses to decide on grid-search fallback."""
    assert isinstance(registry.get_provider("kiwi"), SupportsCalendar)
    assert not isinstance(registry.get_provider("google"), SupportsCalendar)


async def test_close_all_closes_providers_and_clears_instances_for_rebuild():
    """close_all() must both reach every provider's aclose() and clear
    _INSTANCES.

    Clearing connections but leaving stale instances behind would hand a dead
    client to the next get_provider() call after a shutdown/restart cycle
    within one process, so both effects are checked independently.
    """
    # Closing an already-empty registry is a no-op: no exception, nothing to
    # clear.
    await registry.close_all()
    assert registry._INSTANCES == {}

    # A stub proves aclose() actually reached it, rather than just assuming
    # "no exception raised" means the call happened.
    closed_names = []

    class StubProvider:
        name = "stub"

        async def search_leg(self, query):
            raise NotImplementedError

        async def aclose(self):
            closed_names.append(self.name)

    registry._INSTANCES["stub"] = StubProvider()
    first_kiwi = registry.get_provider("kiwi")

    await registry.close_all()

    assert closed_names == ["stub"]
    assert registry._INSTANCES == {}

    # _INSTANCES was cleared, not just emptied of closed connections: a fresh
    # get_provider() call must rebuild rather than hand back the old (now
    # closed) instance.
    second_kiwi = registry.get_provider("kiwi")
    assert second_kiwi is not first_kiwi


# ── Config parsing ───────────────────────────────────────────────────────────


def test_providers_env_parses_a_comma_list(monkeypatch):
    monkeypatch.setenv("PROVIDERS", " kiwi , google ")
    assert config._providers_env("PROVIDERS", ("kiwi",)) == ("kiwi", "google")


def test_providers_env_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("PROVIDERS", raising=False)
    assert config._providers_env("PROVIDERS", ("kiwi", "google")) == ("kiwi", "google")


def test_providers_env_rejects_an_unknown_name(monkeypatch):
    monkeypatch.setenv("PROVIDERS", "kiwi,banana")
    with pytest.raises(config.ConfigError, match="banana"):
        config._providers_env("PROVIDERS", ("kiwi",))


def test_validate_rejects_a_primary_outside_the_enabled_set(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "token")
    monkeypatch.setattr(config, "OWNER_ID", 1)
    monkeypatch.setattr(config, "PROVIDERS", ("google",))
    monkeypatch.setattr(config, "PRIMARY_PROVIDER", "kiwi")
    with pytest.raises(config.ConfigError, match="PRIMARY_PROVIDER"):
        config.validate()


def test_validate_accepts_a_consistent_provider_config(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "token")
    monkeypatch.setattr(config, "OWNER_ID", 1)
    monkeypatch.setattr(config, "PROVIDERS", ("kiwi", "google"))
    monkeypatch.setattr(config, "PRIMARY_PROVIDER", "kiwi")
    config.validate()
