"""Tests for shortlist diversity and leg deduplication."""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.shortlist import diversify, legs_for
from models import Candidate


def _c(date, hub, dest, total, return_date=""):
    """A candidate whose total is exactly `total` (no discount applied)."""
    return Candidate(date=date, return_date=return_date, hub=hub, dest=dest,
                     dom_price=Decimal(0), onward_price=Decimal(total),
                     discount=Decimal(0))


def test_diversify_keeps_order_and_respects_the_limit():
    cands = [_c("2026-10-01", "MAD", "NRT", 100 + i) for i in range(10)]
    # All same hub and date, so caps bite before the limit does.
    out = diversify(cands, limit=5, max_per_hub=3, max_per_date=3)
    assert len(out) == 3


def test_diversify_caps_per_hub():
    cands = [_c(f"2026-10-{d:02d}", "MAD", "NRT", 100 + d) for d in range(1, 11)]
    out = diversify(cands, limit=30, max_per_hub=4, max_per_date=99)
    assert len(out) == 4
    assert all(c.hub == "MAD" for c in out)


def test_diversify_caps_per_date():
    cands = [_c("2026-10-01", h, "NRT", 100 + i)
             for i, h in enumerate(["MAD", "BCN", "AGP", "SVQ", "VLC", "BIO"])]
    out = diversify(cands, limit=30, max_per_hub=99, max_per_date=2)
    assert len(out) == 2


def test_diversify_prefers_cheaper_candidates_within_a_cap():
    cands = [_c("2026-10-01", "MAD", "NRT", 100),
             _c("2026-10-02", "MAD", "NRT", 200),
             _c("2026-10-03", "MAD", "NRT", 300)]
    out = diversify(cands, limit=30, max_per_hub=2, max_per_date=99)
    assert [c.total for c in out] == [Decimal(100), Decimal(200)]


def test_diversify_spreads_across_hubs_rather_than_taking_one_cheap_cluster():
    """The point of the filter: not 30 variants of the same Tuesday via Madrid."""
    cheap_madrid = [_c(f"2026-10-{d:02d}", "MAD", "NRT", 100 + d) for d in range(1, 9)]
    pricier_bcn = [_c(f"2026-10-{d:02d}", "BCN", "NRT", 500 + d) for d in range(1, 9)]
    out = diversify(cheap_madrid + pricier_bcn, limit=6, max_per_hub=3, max_per_date=99)
    assert {c.hub for c in out} == {"MAD", "BCN"}


def test_diversify_on_an_empty_list():
    assert diversify([], limit=30, max_per_hub=6, max_per_date=4) == []


# ── Leg deduplication ───────────────────────────────────────────────────────


def test_legs_for_one_way_yields_two_legs_per_candidate():
    legs = legs_for([_c("2026-10-01", "MAD", "NRT", 600)], origin="LPA", trip_days=0)
    assert set(legs) == {("LPA", "MAD", "2026-10-01"), ("MAD", "NRT", "2026-10-01")}


def test_legs_for_deduplicates_the_shared_domestic_leg():
    """One LPA->MAD on the 1st serves every destination that day."""
    cands = [_c("2026-10-01", "MAD", "NRT", 600),
             _c("2026-10-01", "MAD", "JFK", 500),
             _c("2026-10-01", "MAD", "LAX", 700)]
    legs = legs_for(cands, origin="LPA", trip_days=0)
    assert legs.count(("LPA", "MAD", "2026-10-01")) == 1
    assert len(legs) == 4          # 1 domestic + 3 onward


def test_legs_for_round_trip_adds_the_mirrored_legs():
    cands = [_c("2026-10-01", "MAD", "NRT", 600, return_date="2026-10-15")]
    legs = legs_for(cands, origin="LPA", trip_days=14)
    assert set(legs) == {
        ("LPA", "MAD", "2026-10-01"), ("MAD", "NRT", "2026-10-01"),
        ("MAD", "LPA", "2026-10-15"), ("NRT", "MAD", "2026-10-15"),
    }


def test_legs_for_is_deterministic():
    cands = [_c("2026-10-02", "BCN", "JFK", 500), _c("2026-10-01", "MAD", "NRT", 600)]
    assert legs_for(cands, origin="LPA", trip_days=0) == \
           legs_for(cands, origin="LPA", trip_days=0)


def test_legs_for_no_candidates():
    assert legs_for([], origin="LPA", trip_days=0) == []


def test_legs_for_raises_when_trip_days_positive_but_return_date_empty():
    """trip_days and return_date are two sources of truth for the same fact;

    when they disagree, building a leg from an empty date would silently
    produce a malformed provider query rather than an error.
    """
    cands = [_c("2026-10-01", "MAD", "NRT", 600)]  # return_date="" (default)
    with pytest.raises(ValueError, match=r"2026-10-01.*MAD.*NRT"):
        legs_for(cands, origin="LPA", trip_days=14)


def test_legs_for_one_way_ignores_a_populated_return_date():
    """trip_days is the authority on trip shape: trip_days=0 means one-way legs
    only, even if the candidate happens to carry a return_date."""
    cands = [_c("2026-10-01", "MAD", "NRT", 600, return_date="2026-10-15")]
    legs = legs_for(cands, origin="LPA", trip_days=0)
    assert set(legs) == {("LPA", "MAD", "2026-10-01"), ("MAD", "NRT", "2026-10-01")}
