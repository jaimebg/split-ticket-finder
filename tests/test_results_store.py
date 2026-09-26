"""results.store: the three shapes searches.results holds in real databases."""
from __future__ import annotations

import json
from decimal import Decimal

from results.store import StoredResults, load, serialize
from tests.results_fixtures import offer, one_way, standard_one_way, standard_round_trip


def _round_trip(itineraries):
    return load(json.dumps(serialize(itineraries)))


def test_v2_round_trip_is_exact():
    itins = [standard_one_way(through_fare="700"), standard_round_trip()]
    assert _round_trip(itins) == StoredResults(itineraries=itins, detailed=True)


def test_v2_keeps_money_as_exact_decimal_not_float():
    raw = serialize([one_way(dom=offer("0.10"), onward=offer("0.20"))])
    assert raw["itineraries"][0]["dom_out"]["price"] == "0.10"
    assert _round_trip([one_way(dom=offer("0.10"), onward=offer("0.20"))]) \
        .itineraries[0].dom_out.price == Decimal("0.10")


def test_v2_keeps_unknown_bags_unknown():
    """None means 'the provider could not say', never zero."""
    restored = _round_trip([standard_one_way()]).itineraries[0]
    assert restored.dom_out.included_checked_bags is None


def test_v2_keeps_an_estimate_an_estimate():
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    restored = _round_trip([estimate]).itineraries[0]
    assert restored.status == "estimate"
    assert restored.total == estimate.total


def test_v2_writes_the_version_and_readable_totals():
    raw = serialize([standard_one_way()])
    assert raw["v"] == 2
    assert raw["itineraries"][0]["total"] == "525.00"


def test_a_v1_list_loads_as_undetailed_estimates():
    v1 = [{"date": "2026-09-01", "return_date": "2026-09-15", "hub": "MAD",
           "hub_name": "Madrid", "dest": "NRT", "dest_name": "Tokyo",
           "discount": 0.75, "dom_price": 148.0, "dom_discounted": 37.0,
           "onward_price": 575.0, "total": 612.0, "status": "confirmed",
           "through_fare": 785.0, "savings": 173.0, "savings_pct": 22,
           "requires_bag_recheck": None, "providers": ["kiwi"]}]
    stored = load(json.dumps(v1))
    assert stored.detailed is False
    assert stored.itineraries[0].status == "estimate"
    assert stored.itineraries[0].return_date == "2026-09-15"
    assert stored.itineraries[0].total == Decimal("612.00")


def test_an_unreadable_entry_is_skipped_not_fatal():
    v1 = [{"hub": "MAD", "dest": "NRT", "date": "2026-09-01"},     # no prices at all
          {"date": "2026-09-01", "hub": "MAD", "dest": "NRT", "dom_price": 100.0,
           "onward_price": 500.0, "discount": 0.75}]
    stored = load(json.dumps(v1))
    assert len(stored.itineraries) == 1
    assert stored.itineraries[0].total == Decimal("525.00")


def test_nothing_stored_loads_as_empty():
    assert load(None) == StoredResults(itineraries=[], detailed=False)
    assert load("not json") == StoredResults(itineraries=[], detailed=False)
    assert load('{"v": 99}') == StoredResults(itineraries=[], detailed=False)
