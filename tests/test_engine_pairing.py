"""engine.pairing: the cheapest pair within the best risk level."""
from __future__ import annotations

from decimal import Decimal

from engine.pairing import best_pair
from tests.results_fixtures import offer, seg


def dom(price, arrive):
    return offer(price, seg("LPA", "MAD", "2026-10-01T06:00", arrive))


def onward(price, depart):
    return offer(price, seg("MAD", "NRT", depart, "2026-10-02T09:00"))


def test_a_cheaper_tight_pair_loses_to_a_safe_one():
    cheap_late = dom("20", "2026-10-01T12:00")      # 1h before the onward -> HIGH
    dear_early = dom("60", "2026-10-01T08:00")      # 5h before -> LOW
    assert best_pair([cheap_late, dear_early], [onward("500", "2026-10-01T13:00")]) == \
        (dear_early, onward("500", "2026-10-01T13:00"))


def test_cheapest_within_the_best_level():
    a, b = dom("40", "2026-10-01T07:00"), dom("30", "2026-10-01T07:30")
    first, _ = best_pair([a, b], [onward("500", "2026-10-01T13:00")])
    assert first is b


def test_all_impossible_still_returns_the_cheapest_impossible_pair():
    """Review Focus #1: nothing is dropped here; the view marks it impossible."""
    late1, late2 = dom("50", "2026-10-01T20:00"), dom("40", "2026-10-01T21:00")
    first, _ = best_pair([late1, late2], [onward("500", "2026-10-01T13:00")])
    assert first is late2


def test_unknown_times_fall_back_to_the_cheapest_pair():
    """Offers without segments (Google sometimes, test fakes) are all UNKNOWN,
    so pairing reduces to today's cheapest-per-leg."""
    assert best_pair([offer("40"), offer("30")], [offer("500"), offer("450")]) == \
        (offer("30"), offer("450"))


def test_the_discount_can_change_which_safe_pair_is_cheapest():
    """Within one risk level the pairs are not a free product: here each
    domestic flight is only LOW with one onward flight. Undiscounted, the
    cheap domestic + dear onward wins (40 + 545 = 585 < 600); at 75% off the
    domestic side, the dear domestic + cheap onward wins (25 + 500 = 525 <
    10 + 545 = 555)."""
    dear_dom = dom("100", "2026-10-01T07:00")       # LOW with both onwards
    cheap_dom = dom("40", "2026-10-01T11:30")       # 1.5h before 13:00 -> HIGH
    early = onward("500", "2026-10-01T13:00")
    late = onward("545", "2026-10-01T16:00")        # 4.5h after cheap_dom -> LOW
    assert best_pair([dear_dom, cheap_dom], [early, late]) == (cheap_dom, late)
    assert best_pair([dear_dom, cheap_dom], [early, late],
                     first_discount=Decimal("0.75")) == (dear_dom, early)
