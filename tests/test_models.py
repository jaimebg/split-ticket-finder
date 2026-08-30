"""Tests for the engine's value objects."""
from __future__ import annotations

from decimal import Decimal

import pytest

from models import (
    CancelToken,
    Candidate,
    Itinerary,
    Progress,
    SearchCancelled,
    SearchWindow,
)
from providers.base import Offer


def _offer(price: str, stops: int = 0, **kw) -> Offer:
    return Offer(
        price=Decimal(price), currency="EUR", airlines=["Ryanair"], stops=stops,
        duration=170, segments=[], provider="kiwi", **kw
    )


# ── SearchWindow ────────────────────────────────────────────────────────────


def test_window_lists_every_day_inclusive():
    w = SearchWindow(start="2026-10-01", end="2026-10-05")
    assert w.dates() == [
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    ]


def test_window_of_one_day_is_one_date():
    assert SearchWindow(start="2026-10-01", end="2026-10-01").dates() == ["2026-10-01"]


def test_window_rejects_end_before_start():
    with pytest.raises(ValueError):
        SearchWindow(start="2026-10-05", end="2026-10-01").dates()


def test_window_knows_its_length():
    assert SearchWindow(start="2026-10-01", end="2026-10-31").days == 31


# ── Candidate ───────────────────────────────────────────────────────────────


def test_candidate_is_an_estimate_and_sorts_by_total():
    a = Candidate(date="2026-10-01", return_date="", hub="MAD", dest="NRT",
                  dom_price=Decimal("100"), onward_price=Decimal("500"),
                  discount=Decimal("0.75"))
    b = Candidate(date="2026-10-02", return_date="", hub="BCN", dest="NRT",
                  dom_price=Decimal("80"), onward_price=Decimal("500"),
                  discount=Decimal("0"))
    # a: 100*0.25 + 500 = 525;  b: 80 + 500 = 580
    assert a.total == Decimal("525.00")
    assert b.total == Decimal("580")
    assert sorted([b, a], key=lambda c: c.total)[0] is a


def test_candidate_applies_no_discount_when_rate_is_zero():
    c = Candidate(date="2026-10-01", return_date="", hub="LIS", dest="NRT",
                  dom_price=Decimal("100"), onward_price=Decimal("500"),
                  discount=Decimal("0"))
    assert c.total == Decimal("600")


# ── Itinerary ───────────────────────────────────────────────────────────────


def test_itinerary_totals_are_decimal_and_discount_applies_to_domestic_only():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
    )
    assert it.dom_price == Decimal("148")
    assert it.dom_discounted == Decimal("37.00")
    assert it.onward_price == Decimal("575")
    assert it.total == Decimal("612.00")
    assert it.confirmed is True


def test_itinerary_one_way_with_both_legs_is_confirmed():
    """The stricter round-trip rule must not regress the one-way case."""
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
    )
    assert it.confirmed is True


def test_itinerary_round_trip_with_all_four_legs_is_confirmed():
    it = Itinerary(
        date="2026-10-01", return_date="2026-10-15", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100"), dom_ret=_offer("120"),
        onward_out=_offer("500"), onward_ret=_offer("480"),
    )
    assert it.confirmed is True


def test_itinerary_round_trip_sums_all_four_legs():
    it = Itinerary(
        date="2026-10-01", return_date="2026-10-15", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100"), dom_ret=_offer("120"),
        onward_out=_offer("500"), onward_ret=_offer("480"),
    )
    assert it.dom_price == Decimal("220")
    assert it.dom_discounted == Decimal("55.00")
    assert it.onward_price == Decimal("980")
    assert it.total == Decimal("1035.00")
    assert it.confirmed is True


def test_itinerary_round_trip_missing_dom_ret_is_unconfirmed():
    """Both outbound legs present is not enough for a round trip."""
    it = Itinerary(
        date="2026-10-01", return_date="2026-10-15", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100"), dom_ret=None,
        onward_out=_offer("500"), onward_ret=_offer("480"),
    )
    assert it.confirmed is False


def test_itinerary_round_trip_missing_onward_ret_is_unconfirmed():
    it = Itinerary(
        date="2026-10-01", return_date="2026-10-15", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100"), dom_ret=_offer("120"),
        onward_out=_offer("500"), onward_ret=None,
    )
    assert it.confirmed is False


def test_itinerary_from_candidate_is_unconfirmed():
    """An estimate carries no offers and must never be presented as bookable."""
    c = Candidate(date="2026-10-01", return_date="", hub="MAD", dest="NRT",
                  dom_price=Decimal("148"), onward_price=Decimal("575"),
                  discount=Decimal("0.75"))
    it = Itinerary.from_candidate(c, hub_name="Madrid", dest_name="Tokyo")
    assert it.confirmed is False
    assert it.total == Decimal("612.00")
    assert it.dom_out is None


def test_itinerary_reports_the_worst_bag_recheck_across_legs():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100", requires_bag_recheck=False),
        onward_out=_offer("500", stops=2, requires_bag_recheck=True),
    )
    assert it.requires_bag_recheck is True


def test_itinerary_bag_recheck_is_unknown_when_any_leg_cannot_say():
    """One provider saying False and another saying nothing is not False."""
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("100", requires_bag_recheck=False),
        onward_out=_offer("500", stops=1, requires_bag_recheck=None),
    )
    assert it.requires_bag_recheck is None


def test_itinerary_bag_recheck_is_unknown_with_no_legs():
    """No legs to inspect is a genuine cannot-say, not a clean 'no'."""
    c = Candidate(date="2026-10-01", return_date="", hub="MAD", dest="NRT",
                  dom_price=Decimal("148"), onward_price=Decimal("575"),
                  discount=Decimal("0.75"))
    it = Itinerary.from_candidate(c, hub_name="Madrid", dest_name="Tokyo")
    assert it.requires_bag_recheck is None


def test_itinerary_savings_against_a_through_fare():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
        through_fare=Decimal("980"),
    )
    assert it.savings == Decimal("368.00")
    assert it.savings_pct == 37


def test_itinerary_savings_is_none_without_a_through_fare():
    """No single-ticket fare exists is not a saving of zero."""
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
    )
    assert it.savings is None
    assert it.savings_pct is None


def test_itinerary_savings_pct_is_none_when_through_fare_is_not_positive():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
        through_fare=Decimal("0"),
    )
    assert it.savings_pct is None


# ── with_through_fare / with_providers ──────────────────────────────────────


def test_with_through_fare_returns_a_new_object_and_leaves_original_untouched():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
    )
    updated = it.with_through_fare(Decimal("980"))
    assert updated is not it
    assert updated.through_fare == Decimal("980")
    assert it.through_fare is None


def test_with_providers_returns_a_new_object_and_leaves_original_untouched():
    it = Itinerary(
        date="2026-10-01", return_date="", hub="MAD", hub_name="Madrid",
        dest="NRT", dest_name="Tokyo", discount=Decimal("0.75"),
        dom_out=_offer("148"), onward_out=_offer("575"),
    )
    updated = it.with_providers("kiwi", "google")
    assert updated is not it
    assert updated.providers == ("kiwi", "google")
    assert it.providers == ()


# ── Cancellation and progress ───────────────────────────────────────────────


def test_cancel_token_starts_uncancelled_and_raises_once_cancelled():
    token = CancelToken()
    assert token.cancelled is False
    token.raise_if_cancelled()          # must not raise
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(SearchCancelled):
        token.raise_if_cancelled()


def test_cancel_is_idempotent():
    token = CancelToken()
    token.cancel()
    token.cancel()
    assert token.cancelled is True


def test_progress_reports_a_fraction():
    p = Progress(phase="Phase 1", done=3, total=12)
    assert p.fraction == 0.25


def test_progress_fraction_is_zero_when_total_is_zero():
    assert Progress(phase="Phase 0", done=0, total=0).fraction == 0.0
