"""Tests for the orchestrator: the single callable that wires every engine
phase together (Task 10).

Two capability-distinct fakes drive these tests:

- ``FakeCalendarProvider`` implements both ``search_leg`` and
  ``price_calendar`` -- i.e. ``SupportsCalendar`` -- so it selects the
  two-stage pipeline (scan -> rank -> diversify -> confirm -> through-fares).
- ``FakeLegProvider`` (imported from ``tests.test_engine_fetch``) implements
  only ``search_leg``, so ``isinstance(provider, SupportsCalendar)`` is
  False and it falls back to the grid search.

Both fakes record every call they receive, in order, on a *shared* list when
one is provided -- that shared ordering is what proves phases ran in the
right sequence, not just that each phase individually worked (already
covered by tests/test_engine_scan.py, tests/test_engine_drill.py and
tests/test_engine_grid.py).
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

import engine.orchestrator as orchestrator
from engine.orchestrator import SearchResult, run_search
from models import CancelToken, Progress, SearchCancelled, SearchWindow
from providers.base import ProviderFetchError, ProviderParseError, RatedPrice
from tests.test_engine_fetch import FakeProvider, _offer


def _offer_pnr(price: str):
    """A single-PNR offer -- the only kind through_fares() will count."""
    return dataclasses.replace(_offer(price), pnr_count=1)


class FakeCalendarProvider:
    """A provider with a price calendar -- selects the two-stage strategy.

    ``calendar_answers`` maps ``(origin, dest)`` to ``{date: "price"}``;
    ``leg_answers`` maps ``(origin, dest, date)`` to a list of ``Offer``.
    Every call is appended to ``self.calls`` as ``("cal", origin, dest)`` or
    ``("leg", origin, dest, date)``, in the order the provider actually saw
    them -- across both methods -- which is what lets a test prove that every
    calendar call precedes every leg call.
    """

    def __init__(self, name="primary", calendar_answers=None, leg_answers=None,
                 calendar_errors=None, leg_errors=None):
        self.name = name
        self.calendar_answers = calendar_answers or {}
        self.leg_answers = leg_answers or {}
        self.calendar_errors = calendar_errors or {}
        self.leg_errors = leg_errors or {}
        self.calls: list[tuple] = []

    async def price_calendar(self, query):
        self.calls.append(("cal", query.origin, query.dest))
        key = (query.origin, query.dest)
        if key in self.calendar_errors:
            raise self.calendar_errors[key]
        table = self.calendar_answers.get(key, {})
        return {d: RatedPrice(price=Decimal(p), rating="AVERAGE") for d, p in table.items()}

    async def search_leg(self, query):
        self.calls.append(("leg", query.origin, query.dest, query.date))
        key = (query.origin, query.dest, query.date)
        if key in self.leg_errors:
            raise self.leg_errors[key]
        return self.leg_answers.get(key, [])

    async def aclose(self):
        return None


def _neutral_discount(monkeypatch):
    """Zero out the domestic discount so itinerary totals are plain leg sums."""
    monkeypatch.setattr(orchestrator, "DISCOUNT_AIRPORTS", set())
    monkeypatch.setattr(orchestrator, "DOMESTIC_DISCOUNT", 0.0)


WINDOW = SearchWindow("2026-10-01", "2026-10-01")


# ── Strategy selection ───────────────────────────────────────────────────────


async def test_a_calendar_capable_provider_selects_two_stage(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeCalendarProvider()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert isinstance(result, SearchResult)
    assert result.strategy == "two-stage"


async def test_a_provider_without_a_calendar_selects_grid(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeProvider()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert result.strategy == "grid"


async def test_default_provider_comes_from_the_registry(monkeypatch):
    """provider=None defers to providers.registry.primary_provider()."""
    _neutral_discount(monkeypatch)
    provider = FakeCalendarProvider()
    monkeypatch.setattr(orchestrator, "primary_provider", lambda: provider)
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0,
    )

    assert result.strategy == "two-stage"
    assert result.itineraries == []


# ── Two-stage: phase order 0 -> 0b -> 1 -> 2 ────────────────────────────────


def _one_hub_scenario():
    """One hub, one destination, one date: exactly one candidate survives
    every phase, so the resulting call log is small and easy to assert on."""
    return FakeCalendarProvider(
        calendar_answers={
            ("LPA", "MAD"): {"2026-10-01": "29"},
            ("MAD", "NRT"): {"2026-10-01": "500"},
        },
        leg_answers={
            ("LPA", "MAD", "2026-10-01"): [_offer("25")],
            ("MAD", "NRT", "2026-10-01"): [_offer("480")],
            ("LPA", "NRT", "2026-10-01"): [_offer_pnr("700")],
        },
    )


async def test_two_stage_runs_calendar_calls_before_any_leg_call(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = _one_hub_scenario()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    kinds = [c[0] for c in provider.calls]
    # Every "cal" call (phase 0) precedes every "leg" call (phase 1 then 2).
    last_cal = max(i for i, k in enumerate(kinds) if k == "cal")
    first_leg = min(i for i, k in enumerate(kinds) if k == "leg")
    assert last_cal < first_leg

    assert result.scan is not None
    assert len(result.itineraries) == 1
    itin = result.itineraries[0]
    assert itin.confirmed is True
    assert itin.total == Decimal("505.00")  # 25 + 480, no discount
    assert itin.through_fare == Decimal("700")


async def test_diversify_narrows_the_shortlist_before_confirm_is_called(monkeypatch):
    """0b actually runs: more calendar candidates exist than confirm() ever
    sees a leg query for, because diversify's per-date cap keeps only
    MAX_PER_DATE of them."""
    _neutral_discount(monkeypatch)
    # 5 hubs all reachable on the same single date -- MAX_PER_DATE (4) must
    # drop exactly one of them before phase 1 ever issues a leg query.
    hubs = ["MAD", "BCN", "LIS", "OPO", "SVQ"]
    calendar_answers = {("LPA", hub): {"2026-10-01": "10"} for hub in hubs}
    calendar_answers.update({(hub, "NRT"): {"2026-10-01": "500"} for hub in hubs})
    leg_answers = {("LPA", hub, "2026-10-01"): [_offer("10")] for hub in hubs}
    leg_answers.update({(hub, "NRT", "2026-10-01"): [_offer("500")] for hub in hubs})
    provider = FakeCalendarProvider(calendar_answers=calendar_answers, leg_answers=leg_answers)
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"},
        hubs={h: h for h in hubs}, window=WINDOW, trip_days=0, provider=provider,
    )

    # Phase 1's domestic leg is LPA -> hub; phase 2's through-fare baseline is
    # also LPA -> NRT, so restrict to destinations that are actually hubs
    # (excluding the destination "NRT") to isolate phase 1's own leg calls.
    hubs_queried_for_legs = {
        c[2] for c in provider.calls if c[0] == "leg" and c[1] == "LPA" and c[2] in hubs
    }
    assert len(hubs_queried_for_legs) == orchestrator.MAX_PER_DATE == 4
    assert len(result.itineraries) == 4


# ── Progress ─────────────────────────────────────────────────────────────────


def _assert_progress_labels_well_formed(ticks: list[Progress], expected_order: list[str]) -> None:
    """Shared shape-of-progress assertion (review finding: phase-name
    collisions across composed calls).

    ``expected_order`` is the exact, deduplicated sequence of labels a
    caller should see, in first-appearance order -- asserting equality
    against it is what proves every label is unique (a raw label reused
    for an unrelated later segment would either be missing from this list,
    if it never got its own distinct name, or -- the actual bug -- appear
    only once here while its ``done`` sequence below secretly resets to 0
    partway through, which the monotonicity check below would catch).

    For each label: the ``done`` values across every tick carrying that
    label, in emission order, must never decrease (a label reused for a
    second, later segment would jump back down from a completed total to
    0 -- exactly the "phase restarts" bug), and the last tick for that
    label must be complete.
    """
    seen_order = list(dict.fromkeys(t.phase for t in ticks))
    assert seen_order == expected_order

    for phase in expected_order:
        phase_ticks = [t for t in ticks if t.phase == phase]
        dones = [t.done for t in phase_ticks]
        assert dones == sorted(dones), f"{phase!r} done went backwards: {dones}"
        assert phase_ticks[-1].done == phase_ticks[-1].total
        assert phase_ticks[-1].fraction == 1.0


async def test_progress_phases_arrive_in_order_and_end_complete(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = _one_hub_scenario()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    ticks: list[Progress] = []

    await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider, on_progress=ticks.append,
    )

    _assert_progress_labels_well_formed(ticks, ["Phase 0", "Phase 1", "Phase 2"])


async def test_grid_progress_labels_are_unique_and_monotonic(monkeypatch):
    """Review finding #1: run_grid_search reports its onward (hub->dest) leg
    as "Phase 2", and through_fares -- which _run_grid shares one LegFetcher
    with -- hardcodes "Phase 2" too. Without relabelling, a caller watching
    phase names would see "Phase 2" finish at the end of the grid search,
    then restart at 0/1 for the unrelated through-fare query."""
    _neutral_discount(monkeypatch)
    provider = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("25")],
        ("MAD", "NRT", "2026-10-01"): [_offer("480")],
        ("LPA", "NRT", "2026-10-01"): [_offer_pnr("700")],
    })
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    ticks: list[Progress] = []

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider, on_progress=ticks.append,
    )

    assert result.strategy == "grid"
    assert len(result.itineraries) == 1
    assert result.itineraries[0].through_fare == Decimal("700")

    _assert_progress_labels_well_formed(
        ticks, ["Phase 1", "Phase 2", orchestrator.GRID_THROUGH_FARE_PHASE]
    )


async def test_cross_check_progress_labels_are_unique_and_monotonic(monkeypatch):
    """Review finding #2: engine.drill.confirm() always reports "Phase 1",
    and _cross_check hands it the same on_progress a second time (after
    two-stage's own phase 1 already completed under that exact label).
    Without relabelling, a caller would see "Phase 1" finish, then restart
    at 0/N for the cross-check -- the same shape of bug as finding #1, on
    the two-stage path instead of the grid path."""
    _neutral_discount(monkeypatch)
    hubs = ["MAD", "BCN", "LIS", "SVQ"]
    prices = {"MAD": "10", "BCN": "20", "LIS": "30", "SVQ": "1000"}
    calendar_answers = {("LPA", h): {"2026-10-01": prices[h]} for h in hubs}
    calendar_answers.update({(h, "NRT"): {"2026-10-01": "500"} for h in hubs})
    leg_answers = {("LPA", h, "2026-10-01"): [_offer(prices[h])] for h in hubs}
    leg_answers.update({(h, "NRT", "2026-10-01"): [_offer("500")] for h in hubs})

    primary = FakeCalendarProvider(
        name="primary", calendar_answers=calendar_answers, leg_answers=leg_answers,
    )
    secondary = FakeProvider({
        **{("LPA", h, "2026-10-01"): [_offer(prices[h])] for h in hubs},
        **{(h, "NRT", "2026-10-01"): [_offer("500")] for h in hubs},
    })
    monkeypatch.setattr(
        orchestrator, "enabled_providers",
        lambda: {"primary": primary, "secondary": secondary},
    )
    ticks: list[Progress] = []

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={h: h for h in hubs},
        window=WINDOW, trip_days=0, provider=primary, on_progress=ticks.append,
    )

    assert len(result.itineraries) == 4
    top3 = result.itineraries[:3]
    assert all(set(i.providers) == {primary.name, secondary.name} for i in top3)

    _assert_progress_labels_well_formed(
        ticks, ["Phase 0", "Phase 1", "Phase 2", orchestrator.CROSS_CHECK_PHASE]
    )


# ── Cancellation between phases ─────────────────────────────────────────────


async def test_cancelling_after_phase_0_never_starts_phase_1(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = _one_hub_scenario()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    token = CancelToken()

    def cancel_after_phase_0(tick: Progress) -> None:
        if tick.phase == "Phase 0" and tick.done == tick.total:
            token.cancel()

    with pytest.raises(SearchCancelled):
        await run_search(
            origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
            window=WINDOW, trip_days=0, provider=provider, cancel=token,
            on_progress=cancel_after_phase_0,
        )

    # Phase 0 (calendar calls) genuinely ran; phase 1 (leg calls) never did.
    assert any(c[0] == "cal" for c in provider.calls)
    assert all(c[0] != "leg" for c in provider.calls)


async def test_cancelling_after_phase_1_never_starts_phase_2(monkeypatch):
    """Two-stage's own phase 1 -> phase 2 boundary, not just the phase 0
    boundary already covered above."""
    _neutral_discount(monkeypatch)
    provider = _one_hub_scenario()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    token = CancelToken()

    def cancel_after_phase_1(tick: Progress) -> None:
        if tick.phase == "Phase 1" and tick.done == tick.total:
            token.cancel()

    with pytest.raises(SearchCancelled):
        await run_search(
            origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
            window=WINDOW, trip_days=0, provider=provider, cancel=token,
            on_progress=cancel_after_phase_1,
        )

    # Phase 1's two legs ran; phase 2's through-fare query (LPA -> NRT) did not.
    assert ("leg", "LPA", "MAD", "2026-10-01") in provider.calls
    assert ("leg", "MAD", "NRT", "2026-10-01") in provider.calls
    assert ("leg", "LPA", "NRT", "2026-10-01") not in provider.calls


async def test_cancelling_after_phase_1_also_prevents_the_cross_check(monkeypatch):
    """Cancellation must stop the cross-check just as it stops any other
    phase -- the secondary provider must never be queried either."""
    _neutral_discount(monkeypatch)
    primary = _one_hub_scenario()
    secondary = FakeProvider({
        ("LPA", "MAD", "2026-10-01"): [_offer("25")],
        ("MAD", "NRT", "2026-10-01"): [_offer("480")],
    })
    monkeypatch.setattr(
        orchestrator, "enabled_providers", lambda: {"primary": primary, "secondary": secondary},
    )
    token = CancelToken()

    def cancel_after_phase_1(tick: Progress) -> None:
        if tick.phase == "Phase 1" and tick.done == tick.total:
            token.cancel()

    with pytest.raises(SearchCancelled):
        await run_search(
            origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
            window=WINDOW, trip_days=0, provider=primary, cancel=token,
            on_progress=cancel_after_phase_1,
        )

    assert secondary.seen == []


async def test_pre_cancelled_token_raises_before_any_call(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = _one_hub_scenario()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    token = CancelToken()
    token.cancel()

    with pytest.raises(SearchCancelled):
        await run_search(
            origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
            window=WINDOW, trip_days=0, provider=provider, cancel=token,
        )

    assert provider.calls == []


async def test_grid_strategy_cancellation_between_phase_1_and_2_stops_phase_2(monkeypatch):
    """The grid fallback has phases too; cancelling right after phase 1
    (origin -> hub) must stop phase 2 (hub -> dest) from ever starting."""
    _neutral_discount(monkeypatch)
    provider = FakeProvider({("LPA", "MAD", "2026-10-01"): [_offer("100")]})
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})
    token = CancelToken()

    def cancel_after_phase_1(tick: Progress) -> None:
        if tick.phase == "Phase 1" and tick.done == tick.total:
            token.cancel()

    with pytest.raises(SearchCancelled):
        await run_search(
            origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
            window=WINDOW, trip_days=0, provider=provider, cancel=token,
            on_progress=cancel_after_phase_1,
        )

    assert ("LPA", "MAD", "2026-10-01") in [
        (q.origin, q.dest, q.date) for q in provider.seen
    ]
    assert not any(q.origin == "MAD" and q.dest == "NRT" for q in provider.seen)


# ── Error aggregation ────────────────────────────────────────────────────────


async def test_error_counters_from_scan_and_confirm_are_aggregated(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeCalendarProvider(
        calendar_answers={("MAD", "NRT"): {"2026-10-01": "500"}},
        calendar_errors={("LPA", "MAD"): ProviderParseError("boom")},
        leg_answers={},
    )
    # Nothing survives phase 0 (LPA->MAD errored, so no candidate exists),
    # but the scan's own error counter must still surface on the result.
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert result.parse_errors == 1
    assert result.itineraries == []


async def test_confirm_phase_errors_are_also_aggregated(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeCalendarProvider(
        calendar_answers={
            ("LPA", "MAD"): {"2026-10-01": "29"},
            ("MAD", "NRT"): {"2026-10-01": "500"},
        },
        leg_errors={("LPA", "MAD", "2026-10-01"): ProviderFetchError("timeout")},
    )
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert result.fetch_errors == 1
    assert result.itineraries == []  # the one candidate lost its domestic leg


async def test_error_counters_from_two_phases_are_summed(monkeypatch):
    """Each test above isolates one phase's contribution. This one induces
    a parse error in phase 0 (scan) *and* a separate one in phase 1
    (confirm) in the same run, and asserts the total -- proving the
    aggregation is genuine addition, not just "whichever phase happened to
    run last" or "only the first error counted"."""
    _neutral_discount(monkeypatch)
    provider = FakeCalendarProvider(
        calendar_answers={
            ("LPA", "BCN"): {"2026-10-01": "10"},
            ("BCN", "NRT"): {"2026-10-01": "500"},
        },
        # Phase 0: LPA->MAD's calendar can't be read, so MAD never becomes a
        # candidate at all -- BCN is unaffected and still reaches phase 1.
        calendar_errors={("LPA", "MAD"): ProviderParseError("scan boom")},
        # Phase 1: BCN's own domestic leg then fails too.
        leg_errors={("LPA", "BCN", "2026-10-01"): ProviderParseError("confirm boom")},
    )
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"},
        hubs={"MAD": "Madrid", "BCN": "Barcelona"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert result.parse_errors == 2  # 1 from the scan, 1 from confirm
    assert result.itineraries == []  # BCN also lost its domestic leg


# ── Cross-check (spec §5.7) ──────────────────────────────────────────────────


async def test_cross_check_tags_only_the_top_three_with_both_providers(monkeypatch):
    _neutral_discount(monkeypatch)
    hubs = ["MAD", "BCN", "LIS", "SVQ"]
    prices = {"MAD": "10", "BCN": "20", "LIS": "30", "SVQ": "1000"}
    calendar_answers = {("LPA", h): {"2026-10-01": prices[h]} for h in hubs}
    calendar_answers.update({(h, "NRT"): {"2026-10-01": "500"} for h in hubs})
    leg_answers = {("LPA", h, "2026-10-01"): [_offer(prices[h])] for h in hubs}
    leg_answers.update({(h, "NRT", "2026-10-01"): [_offer("500")] for h in hubs})

    primary = FakeCalendarProvider(
        name="primary", calendar_answers=calendar_answers, leg_answers=leg_answers,
    )
    # Secondary answers the same legs (so it can fully confirm each one) --
    # it never needs a price calendar; only search_leg is used on it.
    secondary = FakeProvider({
        **{("LPA", h, "2026-10-01"): [_offer(prices[h])] for h in hubs},
        **{(h, "NRT", "2026-10-01"): [_offer("500")] for h in hubs},
    })
    monkeypatch.setattr(
        orchestrator, "enabled_providers",
        lambda: {"primary": primary, "secondary": secondary},
    )

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={h: h for h in hubs},
        window=WINDOW, trip_days=0, provider=primary,
    )

    assert len(result.itineraries) == 4
    # Cheapest-first: MAD (510), BCN (520), LIS (530), SVQ (1500).
    assert [i.hub for i in result.itineraries] == ["MAD", "BCN", "LIS", "SVQ"]
    top3, rest = result.itineraries[:3], result.itineraries[3:]
    assert all(set(i.providers) == {primary.name, secondary.name} for i in top3)
    assert all(i.providers == (primary.name,) for i in rest)

    # The secondary was only ever asked about the top-3 hubs, never SVQ.
    assert all(q.origin != "SVQ" and q.dest != "SVQ" for q in secondary.seen)


async def test_cross_check_errors_reach_search_result(monkeypatch):
    """The cross-check's own LegFetcher errors are unverified today (every
    existing test either has no secondary or a secondary that fully
    succeeds). Here the secondary fails every query it is asked, and the
    resulting fetch_errors must still surface on SearchResult -- not just
    get logged and dropped -- exactly as confirm's phase-1 errors already
    do (test_confirm_phase_errors_are_also_aggregated)."""
    _neutral_discount(monkeypatch)
    primary = _one_hub_scenario()
    secondary = FakeProvider(error=ProviderFetchError("cross-check boom"))
    monkeypatch.setattr(
        orchestrator, "enabled_providers", lambda: {"primary": primary, "secondary": secondary},
    )

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=primary,
    )

    # One confirmed itinerary, one-way: legs_for gives it 2 legs
    # (LPA->MAD, MAD->NRT), both of which fail against the secondary.
    assert result.fetch_errors == 2
    assert len(result.itineraries) == 1
    # The secondary never managed to confirm it, so only primary's tag stands.
    assert result.itineraries[0].providers == (primary.name,)


async def test_cross_check_with_fewer_than_three_confirmed_itineraries(monkeypatch):
    """Only 2 confirmed itineraries exist -- fewer than CROSS_CHECK_TOP_N
    (3). ``confirmed_indices[:CROSS_CHECK_TOP_N]`` slices this correctly
    (Python slicing past the end of a list is not an error), but nothing
    pinned that before; this does."""
    _neutral_discount(monkeypatch)
    hubs = ["MAD", "BCN"]
    prices = {"MAD": "10", "BCN": "20"}
    calendar_answers = {("LPA", h): {"2026-10-01": prices[h]} for h in hubs}
    calendar_answers.update({(h, "NRT"): {"2026-10-01": "500"} for h in hubs})
    leg_answers = {("LPA", h, "2026-10-01"): [_offer(prices[h])] for h in hubs}
    leg_answers.update({(h, "NRT", "2026-10-01"): [_offer("500")] for h in hubs})

    primary = FakeCalendarProvider(
        name="primary", calendar_answers=calendar_answers, leg_answers=leg_answers,
    )
    secondary = FakeProvider({
        **{("LPA", h, "2026-10-01"): [_offer(prices[h])] for h in hubs},
        **{(h, "NRT", "2026-10-01"): [_offer("500")] for h in hubs},
    })
    monkeypatch.setattr(
        orchestrator, "enabled_providers",
        lambda: {"primary": primary, "secondary": secondary},
    )

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={h: h for h in hubs},
        window=WINDOW, trip_days=0, provider=primary,
    )

    assert len(result.itineraries) == 2
    assert all(set(i.providers) == {primary.name, secondary.name} for i in result.itineraries)


async def test_a_single_enabled_provider_means_no_cross_check(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = _one_hub_scenario()
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert len(result.itineraries) == 1
    assert result.itineraries[0].providers == (provider.name,)


# ── Empty results ────────────────────────────────────────────────────────────


async def test_two_stage_with_no_candidates_returns_cleanly(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeCalendarProvider()  # no calendar data anywhere
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert result.itineraries == []
    assert result.strategy == "two-stage"
    assert result.parse_errors == 0
    assert result.fetch_errors == 0
    # Only the calendar phase ran; nothing downstream had anything to do.
    assert all(c[0] == "cal" for c in provider.calls)


async def test_grid_with_no_results_returns_cleanly(monkeypatch):
    _neutral_discount(monkeypatch)
    provider = FakeProvider()  # no answers anywhere
    monkeypatch.setattr(orchestrator, "enabled_providers", lambda: {"p": provider})

    result = await run_search(
        origin="LPA", destinations={"NRT": "Tokyo"}, hubs={"MAD": "Madrid"},
        window=WINDOW, trip_days=0, provider=provider,
    )

    assert result.itineraries == []
    assert result.strategy == "grid"
    assert result.scan is None
