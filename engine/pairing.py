"""Which two flights to pair across a self-transfer.

The provider returns several offers per leg; the old rule took the cheapest
of each on its own, which paired flights that cannot connect. This takes the
cheapest pair within the best risk level available (engine.risk): a safer
connection beats a cheaper one, because a missed self-transfer loses both
tickets. It only chooses among offers already fetched -- zero new requests.
"""
from __future__ import annotations

from decimal import Decimal

from engine.risk import connection_risk
from providers.base import Offer


def _paid(offer: Offer, discount: Decimal) -> Decimal:
    return offer.price * (Decimal(1) - discount)


def best_pair(firsts: list[Offer], seconds: list[Offer], *,
              first_discount: Decimal = Decimal(0),
              second_discount: Decimal = Decimal(0)) -> tuple[Offer, Offer]:
    """The (first, second) pair with the lowest risk, cheapest among equals.

    Both lists must be non-empty. Ties keep the providers' own order (cheapest
    first), so the choice is deterministic.
    """
    return min(
        ((a, b) for a in firsts for b in seconds),
        key=lambda pair: (connection_risk(*pair),
                          _paid(pair[0], first_discount) + _paid(pair[1], second_discount)),
    )
