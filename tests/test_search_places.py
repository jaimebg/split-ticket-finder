"""Tests for the place picker (spec §6.3).

Free text in, up to eight toggle buttons out. The three things that decide
correctness here are which provider answers, whether provider text is
escaped before it reaches Telegram HTML, and whether a Kiwi-less
deployment still has a way to search at all.
"""
from __future__ import annotations

import pytest

import db as db_module
import handlers.search.places as places
from providers.base import Place, ProviderFetchError

NRT = Place(code="NRT", name="Narita", city="Tokyo", country="Japan",
            place_id="Airport:NRT")
HND = Place(code="HND", name="Haneda", city="Tokyo", country="Japan",
            place_id="Airport:HND")


class FakePlacesProvider:
    """A provider that can resolve names. Records every call."""

    name = "fake"

    def __init__(self, results=None, error=None):
        self._results = results if results is not None else [NRT, HND]
        self._error = error
        self.calls: list[str] = []

    async def resolve_place(self, term, limit=8):
        self.calls.append(term)
        if self._error:
            raise self._error
        return self._results[:limit]

    async def search_leg(self, query):
        return []

    async def aclose(self):
        return None


class FakePlainProvider:
    """A provider with no place search — Google's shape."""

    name = "plain"

    async def search_leg(self, query):
        return []

    async def aclose(self):
        return None


# ── Provider selection ───────────────────────────────────────────────────────


def test_places_provider_picks_a_capable_provider(monkeypatch):
    monkeypatch.setattr(places, "enabled_providers",
                        lambda: {"google": FakePlainProvider(),
                                 "kiwi": FakePlacesProvider()})

    assert isinstance(places.places_provider(), FakePlacesProvider)


def test_places_provider_does_not_require_the_primary_one(monkeypatch):
    """With PRIMARY_PROVIDER=google, Kiwi still resolves names even though
    Google drives the search. Otherwise the spec's 'no step requires an
    IATA code' and 'disabling Kiwi leaves a working bot' contradict."""
    monkeypatch.setattr(places, "enabled_providers",
                        lambda: {"google": FakePlainProvider(),
                                 "kiwi": FakePlacesProvider()})
    monkeypatch.setattr(places, "PRIMARY_PROVIDER", "google")

    assert places.places_provider() is not None


def test_places_provider_is_none_without_a_capable_provider(monkeypatch):
    monkeypatch.setattr(places, "enabled_providers",
                        lambda: {"google": FakePlainProvider()})

    assert places.places_provider() is None


# ── The paste short-circuit (§6.3) ───────────────────────────────────────────


def test_typed_codes_parse_without_any_lookup():
    assert places.try_parse_codes("JFK,LAX") == ["JFK", "LAX"]


def test_a_place_name_is_not_mistaken_for_codes():
    assert places.try_parse_codes("Tokyo") is None
    assert places.try_parse_codes("New York") is None


# ── Resolution and cache ─────────────────────────────────────────────────────


async def test_resolve_returns_places_and_caches_them(temp_db, monkeypatch):
    provider = FakePlacesProvider()
    monkeypatch.setattr(places, "places_provider", lambda: provider)

    first = await places.resolve("Tokyo")
    second = await places.resolve("Tokyo")

    assert [p.code for p in first] == ["NRT", "HND"]
    assert first == second
    assert provider.calls == ["Tokyo"]      # the second call hit the cache


async def test_resolve_normalizes_the_cache_key(temp_db, monkeypatch):
    provider = FakePlacesProvider()
    monkeypatch.setattr(places, "places_provider", lambda: provider)

    await places.resolve("Tokyo")
    await places.resolve("  TOKYO  ")

    assert provider.calls == ["Tokyo"]


async def test_resolve_drops_a_place_whose_code_is_not_iata(temp_db, monkeypatch):
    """Place.code flows into a LegQuery, a booking URL and a searches row.
    A provider is not a trusted source for it."""
    bad = Place(code="TOKYO-ALL", name="All airports", city="Tokyo",
                country="Japan", place_id="City:tokyo")
    monkeypatch.setattr(places, "places_provider",
                        lambda: FakePlacesProvider(results=[bad, NRT]))

    result = await places.resolve("Tokyo")

    assert [p.code for p in result] == ["NRT"]


async def test_resolve_without_a_capable_provider_returns_empty(temp_db, monkeypatch):
    monkeypatch.setattr(places, "places_provider", lambda: None)

    assert await places.resolve("Tokyo") == []


async def test_resolve_does_not_cache_a_provider_failure(temp_db, monkeypatch):
    """Caching an error would make one bad minute look like a dead airport
    for the next thirty days."""
    provider = FakePlacesProvider(error=ProviderFetchError("down"))
    monkeypatch.setattr(places, "places_provider", lambda: provider)

    with pytest.raises(ProviderFetchError):
        await places.resolve("Tokyo")

    assert await db_module.get_cached_places("Tokyo") is None


# ── Rendering ────────────────────────────────────────────────────────────────


def _draft():
    from handlers.search.draft import SearchDraft
    return SearchDraft(origin="LPA", origin_name="Gran Canaria")


def test_picker_escapes_provider_text():
    """The Layer 2 carry-forward flags these interpolations as safe only by
    luck. An unescaped '<' makes Telegram reject the whole message."""
    hostile = Place(code="XXX", name="A<b>&B", city="Tokyo", country="Japan",
                    place_id="x")
    _, rows = places.render_picker(_draft(), field="dest",
                                   results=[hostile], term="x")
    labels = "".join(b.label for row in rows for b in row)

    assert "A<b>&B" not in labels
    assert "A&lt;b&gt;&amp;B" in labels


def test_picker_renders_heading_as_bold():
    """Field titles are bolded to match other screens in the package."""
    text, _ = places.render_picker(_draft(), field="dest", results=[], term="")
    assert text.startswith("<b>")


def test_picker_escapes_the_users_own_search_term():
    text, _ = places.render_picker(_draft(), field="dest", results=[],
                                   term="<script>")
    assert "<script>" not in text


def test_picker_marks_already_selected_places():
    draft = _draft().with_(destinations=(("NRT", "Narita"),))
    _, rows = places.render_picker(draft, field="dest",
                                   results=[NRT, HND], term="Tokyo")
    labels = {b.data: b.label for row in rows for b in row}

    assert labels["p:dest:NRT"].startswith("✓")
    assert not labels["p:dest:HND"].startswith("✓")


def test_picker_offers_done_and_back():
    _, rows = places.render_picker(_draft(), field="dest", results=[NRT],
                                   term="Tokyo")
    data = [b.data for row in rows for b in row]

    assert "back" in data


def test_picker_reports_no_matches_when_a_search_actually_ran(monkeypatch):
    monkeypatch.setattr(places, "places_provider", lambda: FakePlacesProvider())

    text, _ = places.render_picker(_draft(), field="dest", results=[], term="Zzzqx")

    assert "Nothing matched" in text


def test_picker_omits_nothing_matched_without_a_capable_provider(monkeypatch):
    """FIX 4: no places provider configured means no search ever ran --
    asserting "Nothing matched" would claim a result that was never
    obtained, the same empty-vs-unavailable collapse FIX 2 covers for the
    price calendar."""
    monkeypatch.setattr(places, "places_provider", lambda: None)

    text, _ = places.render_picker(_draft(), field="dest", results=[], term="Tokyo")

    assert "Nothing matched" not in text


def test_picker_without_a_capable_provider_asks_for_codes(monkeypatch):
    """A Kiwi-less deployment keeps a working search — the second half of
    the two success criteria that would otherwise contradict."""
    monkeypatch.setattr(places, "places_provider", lambda: None)

    text, _ = places.render_picker(_draft(), field="dest", results=[], term="")

    assert "IATA" in text or "code" in text.lower()
    assert "MAD" in text          # a worked example, not a bare instruction


def test_picker_reports_an_error_without_losing_the_screen():
    text, rows = places.render_picker(_draft(), field="dest", results=[],
                                      term="Tokyo", error="Search unavailable")

    assert "Search unavailable" in text
    assert any(b.data == "back" for row in rows for b in row)
