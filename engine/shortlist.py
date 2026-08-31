"""Diversity filter and leg deduplication between phase 0 and phase 1.

Phase 0 scans broadly and ranks for free: a 91-day window over 8 hubs x 3
destinations produces 2,184 candidates for zero further requests. Phase 1
has to confirm candidates against real offers, and that is where the budget
actually gets spent -- so what phase 1 confirms matters as much as how many.

Two problems would otherwise burn that budget straight back:

- Taking the top N candidates by price alone gives thirty variants of the
  same cheap Tuesday via Madrid. The user gets one option dressed up as
  thirty, and every one of those confirmations was redundant.
  ``diversify`` fixes this with per-hub and per-date caps.

- Confirming a shortlist leg by leg without dedup re-fetches the same
  domestic leg once per destination that shares it: one LPA->MAD on a given
  day serves every destination priced out of Madrid that day. Naively that
  is O(candidates * legs-per-candidate) requests -- with a shortlist of 30
  round-trip candidates, 4K requests. ``legs_for`` collapses the shared legs
  so each unique (origin, dest, date) triple is fetched once.
"""
from __future__ import annotations

from models import Candidate

from .fetch import LegKey


def diversify(
    candidates: list[Candidate],
    *,
    limit: int,
    max_per_hub: int,
    max_per_date: int,
) -> list[Candidate]:
    """Select a shortlist that is cheap *and* varied, not just cheap.

    ``candidates`` must already be sorted cheapest-first (as
    ``rank_candidates`` returns them). This function walks that order and
    never re-sorts it: taking candidates in the given order, subject only to
    the per-hub and per-date caps, is what makes it prefer cheaper
    candidates within each cap. Re-sorting the input here would silently
    break that guarantee for every caller.

    A candidate is kept only if neither its hub nor its date has hit its
    cap yet. Selection stops once ``limit`` candidates have been kept.
    """
    hub_counts: dict[str, int] = {}
    date_counts: dict[str, int] = {}
    out: list[Candidate] = []

    for cand in candidates:
        if len(out) >= limit:
            break
        if hub_counts.get(cand.hub, 0) >= max_per_hub:
            continue
        if date_counts.get(cand.date, 0) >= max_per_date:
            continue
        out.append(cand)
        hub_counts[cand.hub] = hub_counts.get(cand.hub, 0) + 1
        date_counts[cand.date] = date_counts.get(cand.date, 0) + 1

    return out


def legs_for(
    candidates: list[Candidate],
    *,
    origin: str,
    trip_days: int,
) -> list[LegKey]:
    """Return the unique legs phase 1 needs to confirm ``candidates``.

    Many candidates share a domestic leg -- every destination priced out of
    the same hub on the same day needs the identical ``origin -> hub``
    query. Deduplicating that leg is what keeps phase 1's request count
    proportional to the shortlist's diversity rather than its size.

    Legs are collected through a ``dict`` used as an ordered set rather than
    a ``set``: ``Offer`` is unhashable and ``Candidate`` is treated the same
    way, so nothing here is ever hashed as a value -- only ``LegKey`` tuples
    are, as dict keys. A plain ``set`` would also make the output order vary
    run to run, and that order is the request order: it has to stay
    deterministic for the resulting request count to be reproducible and a
    cost regression to be spottable.

    For a one-way candidate (``trip_days == 0``), two legs are added: the
    domestic ``origin -> hub`` leg and the onward ``hub -> dest`` leg, both
    on ``date``. For a round trip, two mirrored legs are added on top of
    those: ``hub -> origin`` and ``dest -> hub``, both on ``return_date``.

    ``trip_days`` and ``return_date`` are two sources of truth for the same
    fact, and phase 0b keeps them consistent -- but this is a public
    function Task 7 calls, so that consistency is checked rather than
    assumed. ``trip_days`` is the authority on trip *shape*; a candidate's
    own ``return_date`` is the authority on *which* date, once shape says a
    return date is expected. If ``trip_days > 0`` and a candidate's
    ``return_date`` is empty, that candidate is inconsistent with the shape
    it was asked to be built for, and there is no date to build a correct
    mirrored leg from -- silently building one from ``""`` would produce a
    malformed provider query rather than a shrunk shortlist, which is the
    broken-looks-like-empty failure mode this project avoids elsewhere.
    This raises ``ValueError`` instead. The converse (``trip_days == 0``
    with a populated ``return_date``) is not an error: ``trip_days`` says
    the trip is one-way, so the unused ``return_date`` is simply ignored
    and only one-way legs are emitted.
    """
    legs: dict[LegKey, None] = {}

    for cand in candidates:
        legs[(origin, cand.hub, cand.date)] = None
        legs[(cand.hub, cand.dest, cand.date)] = None
        if trip_days > 0:
            if not cand.return_date:
                raise ValueError(
                    f"candidate {cand.date} {cand.hub}->{cand.dest} has trip_days="
                    f"{trip_days} but no return_date"
                )
            legs[(cand.hub, origin, cand.return_date)] = None
            legs[(cand.dest, cand.hub, cand.return_date)] = None

    return list(legs)
