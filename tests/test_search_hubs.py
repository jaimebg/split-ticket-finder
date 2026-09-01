"""Tests for hub multi-select (spec §6.3).

Hubs are the search's cost driver -- phase 0 issues one calendar request
per hub per destination -- so the screen shows every known hub as a toggle
rather than hiding them behind presets.
"""
from __future__ import annotations

from config import DEFAULT_HUBS
from handlers.search.draft import SearchDraft
from handlers.search.hubs import (
    add_typed_hubs,
    apply_hub_preset,
    render_hubs,
    toggle_hub,
)


def _draft(**kw) -> SearchDraft:
    base = {"origin": "LPA", "origin_name": "Gran Canaria"}
    base.update(kw)
    return SearchDraft(**base)


def test_toggling_a_hub_adds_then_removes_it():
    d = toggle_hub(_draft(), "MAD")
    assert d.hub_codes == ("MAD",)

    d = toggle_hub(d, "MAD")
    assert d.hub_codes == ()


def test_a_toggled_hub_carries_its_known_name():
    d = toggle_hub(_draft(), "MAD")
    assert dict(d.hubs)["MAD"] == "Madrid"


def test_toggling_with_explicit_name_stores_that_name():
    """New code added via toggle_hub with explicit name uses that name."""
    d = toggle_hub(_draft(), "ZRH", name="Zurich")
    assert dict(d.hubs)["ZRH"] == "Zurich"


def test_toggling_known_code_without_name_uses_known_name():
    """Known code toggled without explicit name gets its _KNOWN name."""
    d = toggle_hub(_draft(), "MAD", name=None)
    assert dict(d.hubs)["MAD"] == "Madrid"


def test_toggling_unknown_code_without_name_doubles_as_label():
    """Unknown code toggled without explicit name uses code as label."""
    d = toggle_hub(_draft(), "ZRH", name=None)
    assert dict(d.hubs)["ZRH"] == "ZRH"


def test_existing_selection_keeps_previous_name_when_other_hubs_toggle():
    """When toggling other hubs, existing selections preserve their names."""
    d = toggle_hub(_draft(), "ZRH", name="Zurich")
    assert dict(d.hubs)["ZRH"] == "Zurich"

    # Toggle another hub on: ZRH should still be "Zurich", not re-derived
    d = toggle_hub(d, "MAD")
    hubs = dict(d.hubs)
    assert hubs["ZRH"] == "Zurich"
    assert hubs["MAD"] == "Madrid"


def test_presets_select_the_documented_sets():
    assert apply_hub_preset(_draft(), "top2").hub_codes == ("MAD", "BCN")
    assert apply_hub_preset(_draft(), "top3").hub_codes == ("MAD", "BCN", "LIS")
    assert set(apply_hub_preset(_draft(), "all").hub_codes) == set(DEFAULT_HUBS)


def test_a_preset_replaces_rather_than_appends():
    """Tapping 'Top 2' after picking six hubs must give two, not eight."""
    d = apply_hub_preset(_draft(), "all")
    assert apply_hub_preset(d, "top2").hub_codes == ("MAD", "BCN")


def test_typed_codes_are_added_with_a_known_name_where_there_is_one():
    d = add_typed_hubs(_draft(), ["MAD", "ZRH"])
    hubs = dict(d.hubs)

    assert hubs["MAD"] == "Madrid"
    assert hubs["ZRH"] == "ZRH"     # unknown: the code doubles as the label


def test_typed_codes_merge_with_what_is_already_selected():
    d = toggle_hub(_draft(), "MAD")
    d = add_typed_hubs(d, ["BCN"])

    assert set(d.hub_codes) == {"MAD", "BCN"}


def test_typed_codes_do_not_duplicate_an_existing_hub():
    d = toggle_hub(_draft(), "MAD")
    d = add_typed_hubs(d, ["MAD"])

    assert d.hub_codes == ("MAD",)


def test_toggle_preserves_insertion_order():
    """Toggling MAD, then BCN, then LIS yields exactly ("MAD", "BCN", "LIS")."""
    d = _draft()
    d = toggle_hub(d, "MAD")
    d = toggle_hub(d, "BCN")
    d = toggle_hub(d, "LIS")
    assert d.hub_codes == ("MAD", "BCN", "LIS")


def test_typed_codes_preserve_insertion_order():
    """Selecting ZRH first and typing MAD yields exactly ("ZRH", "MAD")."""
    d = toggle_hub(_draft(), "ZRH", name="Zurich")
    d = add_typed_hubs(d, ["MAD"])
    assert d.hub_codes == ("ZRH", "MAD")


def test_preset_and_individual_toggles_yield_same_order():
    """Tapping 'Top 3' and toggling same hubs individually produce same tuple."""
    # Path 1: use preset
    d_preset = apply_hub_preset(_draft(), "top3")

    # Path 2: toggle individually
    d_toggle = _draft()
    d_toggle = toggle_hub(d_toggle, "MAD")
    d_toggle = toggle_hub(d_toggle, "BCN")
    d_toggle = toggle_hub(d_toggle, "LIS")

    assert d_preset.hub_codes == d_toggle.hub_codes
    assert d_preset.hub_codes == ("MAD", "BCN", "LIS")


def test_render_marks_selected_hubs():
    d = toggle_hub(_draft(), "MAD")
    _, rows = render_hubs(d)
    labels = {b.data: b.label for row in rows for b in row}

    assert labels["h:MAD"].startswith("✓")
    assert not labels["h:BCN"].startswith("✓")


def test_render_offers_every_known_hub():
    _, rows = render_hubs(_draft())
    data = {b.data for row in rows for b in row}

    for code in DEFAULT_HUBS:
        assert f"h:{code}" in data


def test_render_offers_the_presets_and_done():
    _, rows = render_hubs(_draft())
    data = {b.data for row in rows for b in row}

    assert {"hp:all", "hp:top2", "hp:top3", "back"} <= data


def test_render_warns_when_no_hub_is_selected():
    text, _ = render_hubs(_draft())
    assert "at least one" in text.lower()
