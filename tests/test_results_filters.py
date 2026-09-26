"""results.filters: zero-request filters, where unknown never passes."""
from __future__ import annotations

from decimal import Decimal

import pytest

from results.filters import Filters, apply, carriers
from tests.results_fixtures import (
    offer,
    one_way,
    seg,
    standard_one_way,
    standard_round_trip,
)


def _shown(itins, filters):
    return [i for i, _ in apply(itins, filters)[0]]


def test_no_filters_shows_everything_including_estimates():
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    shown, hidden = apply([standard_one_way(), estimate], Filters())
    assert [i for i, _ in shown] == [0, 1]
    assert hidden == 0


def test_any_active_filter_hides_an_estimate():
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    shown, hidden = apply([estimate], Filters(max_stops=1))
    assert shown == []
    assert hidden == 1


def test_max_stops():
    one_stop = one_way(
        dom=standard_one_way().dom_out,
        onward=offer("450",
                     seg("MAD", "CDG", "2026-10-01T13:00", "2026-10-01T15:00"),
                     seg("CDG", "NRT", "2026-10-01T17:00", "2026-10-02T12:00")),
    )
    itins = [standard_one_way(), one_stop]
    assert _shown(itins, Filters(max_stops=0)) == [0]
    assert _shown(itins, Filters(max_stops=1)) == [0, 1]


def test_max_hours_counts_flying_plus_the_buffer():
    # 120 + 180 + 780 minutes = 18h exactly.
    assert _shown([standard_one_way()], Filters(max_hours=18)) == [0]
    assert _shown([standard_one_way()], Filters(max_hours=12)) == []


def test_min_buffer():
    assert _shown([standard_one_way()], Filters(min_buffer_hours=3)) == [0]
    assert _shown([standard_one_way()], Filters(min_buffer_hours=4)) == []


def test_exclude_a_carrier_on_any_segment():
    assert _shown([standard_one_way()], Filters(exclude=frozenset({"JL"}))) == []
    assert _shown([standard_one_way()], Filters(exclude=frozenset({"FR"}))) == [0]


def test_round_trip_with_an_unknown_return_connection_is_hidden():
    """Review Focus #4: the outbound buffer is known and fine, but the
    return's is not. Evaluating only the outbound would let it through."""
    rt = standard_round_trip(
        dom_ret=offer("90", seg("MAD", "LPA", None, "2026-10-15T22:45")),
    )
    assert _shown([rt], Filters(min_buffer_hours=2)) == []
    assert _shown([rt], Filters(max_hours=24)) == []
    assert _shown([rt], Filters(max_stops=1)) == [0]


def test_a_round_trip_missing_a_return_offer_is_hidden_by_any_filter():
    rt = standard_round_trip(dom_ret=None)
    assert _shown([rt], Filters(max_stops=1)) == []


def test_indices_refer_to_the_input_list_not_the_filtered_one():
    itins = [standard_one_way(), one_way(dom=None), standard_one_way(date="2026-10-02")]
    assert _shown(itins, Filters(max_stops=1)) == [0, 2]


def test_carriers_lists_every_code_once_sorted():
    assert carriers([standard_one_way(), standard_round_trip()]) == [
        ("IB", "Iberia"), ("JL", "JAL"),
    ]


def test_active_counts_each_set_filter_once():
    assert Filters().active == 0
    assert Filters(max_stops=0, exclude=frozenset({"FR", "IB"})).active == 2


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("s", "0", Filters(max_stops=0)),
        ("h", "18", Filters(max_hours=18)),
        ("b", "3", Filters(min_buffer_hours=3)),
        ("x", "FR", Filters(exclude=frozenset({"FR"}))),
    ],
)
def test_with_setting(key, value, expected):
    assert Filters().with_setting(key, value) == expected


def test_with_setting_any_clears_and_x_toggles_off():
    f = Filters(max_stops=0, exclude=frozenset({"FR"}))
    assert f.with_setting("s", "any").max_stops is None
    assert f.with_setting("x", "FR").exclude == frozenset()


@pytest.mark.parametrize(("key", "value"), [("s", "7"), ("q", "1"), ("h", "abc")])
def test_with_setting_rejects_what_no_button_sends(key, value):
    with pytest.raises(ValueError):
        Filters().with_setting(key, value)


def test_dict_round_trip_and_tolerance():
    f = Filters(max_stops=1, max_hours=24, min_buffer_hours=2, exclude=frozenset({"FR"}))
    assert Filters.from_dict(f.to_dict()) == f
    assert Filters.from_dict(None) == Filters()
    assert Filters.from_dict({"max_stops": 9, "exclude": [3, "FR"]}) == Filters(
        exclude=frozenset({"FR"}))
