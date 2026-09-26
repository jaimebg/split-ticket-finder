"""handlers.search.options: the options screen, as pure data."""
from __future__ import annotations

import pytest

from handlers.search.draft import SearchDraft
from handlers.search.options import CURRENCIES, apply, render
from providers.base import ALL_CABINS, Capabilities

KIWI = Capabilities(cabins=frozenset(ALL_CABINS), children=True, min_layover=True)
GOOGLE = Capabilities(cabins=frozenset({"ECONOMY"}), children=False, min_layover=False)


def _draft(**kw):
    return SearchDraft(origin="LPA", origin_name="Gran Canaria", **kw)


def _data(rows):
    return [b.data for row in rows for b in row]


@pytest.mark.parametrize(("data", "field", "value"), [
    ("o:a:+", "adults", 2), ("o:k:+", "children", 1), ("o:c:BUSINESS", "cabin", "BUSINESS"),
    ("o:cur:USD", "currency", "USD"), ("o:s:0", "max_stops", 0), ("o:s:any", "max_stops", None),
    ("o:l:120", "min_layover", 120), ("o:l:any", "min_layover", None),
])
def test_each_tap_sets_its_field(data, field, value):
    assert getattr(apply(_draft(), data, KIWI), field) == value


def test_minus_never_goes_below_one_adult_or_zero_children():
    assert apply(_draft(), "o:a:-", KIWI) == "At least one adult travels."
    assert apply(_draft(), "o:k:-", KIWI) == "There are no children to remove."


def test_nine_passengers_is_the_ceiling():
    """Review Focus #4."""
    assert apply(_draft(adults=9), "o:a:+", KIWI) == "Nine passengers is the most one search can hold."
    assert apply(_draft(adults=8, children=1), "o:k:+", KIWI) == \
        "Nine passengers is the most one search can hold."


def test_a_tap_the_provider_cannot_honour_is_refused():
    assert apply(_draft(), "o:c:BUSINESS", GOOGLE) == "Your flight source can't search Business class."
    assert apply(_draft(), "o:k:+", GOOGLE) == "Your flight source can't search with children."
    assert apply(_draft(), "o:l:60", GOOGLE) == "Your flight source can't apply a minimum layover."


def test_garbage_is_refused_not_raised():
    for data in ("o:", "o:zz:1", "o:s:7", "o:cur:XXX", "o:c:COACH", "o:l:5"):
        assert isinstance(apply(_draft(), data, KIWI), str)


def test_render_marks_choices_and_hides_what_google_cannot_do():
    _, rows = render(_draft(cabin="BUSINESS"), KIWI)
    labels = {b.data: b.label for row in rows for b in row}
    assert labels["o:c:BUSINESS"].startswith("•")
    assert "o:k:+" in labels and "o:l:60" in labels
    assert set(CURRENCIES) <= {d.split(":")[2] for d in labels if d.startswith("o:cur:")}

    _, g_rows = render(_draft(), GOOGLE)
    g_data = _data(g_rows)
    assert "o:c:BUSINESS" not in g_data
    assert "o:k:+" not in g_data and "o:l:60" not in g_data
    assert "back" in g_data


def test_every_callback_fits_64_bytes():
    _, rows = render(_draft(), KIWI)
    assert all(len(d.encode()) <= 64 for d in _data(rows))
