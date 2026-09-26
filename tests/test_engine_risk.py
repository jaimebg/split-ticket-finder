"""engine.risk: how dangerous a self-transfer between two tickets is."""
from __future__ import annotations

import pytest

import config
from engine.risk import RISK_LABELS, Risk, connection_risk, itinerary_risk
from tests.results_fixtures import offer, one_way, seg, standard_round_trip


def _pair(arrive: str, depart: str, *, via_out="MAD", via_in="MAD"):
    first = offer("100", seg("LPA", via_out, "2026-10-01T07:00", arrive))
    second = offer("500", seg(via_in, "NRT", depart, "2026-10-02T09:00"))
    return first, second


@pytest.mark.parametrize(("arrive", "depart", "risk"), [
    ("2026-10-01T10:00", "2026-10-01T09:00", Risk.IMPOSSIBLE),
    ("2026-10-01T10:00", "2026-10-01T10:00", Risk.HIGH),       # 0h
    ("2026-10-01T10:00", "2026-10-01T11:59", Risk.HIGH),
    ("2026-10-01T10:00", "2026-10-01T12:00", Risk.MEDIUM),     # exactly 2h
    ("2026-10-01T10:00", "2026-10-01T13:59", Risk.MEDIUM),
    ("2026-10-01T10:00", "2026-10-01T14:00", Risk.LOW),        # exactly 4h
    ("2026-09-30T20:00", "2026-10-01T13:00", Risk.LOW),        # a night at the hub
])
def test_levels_and_their_boundaries(arrive, depart, risk):
    assert connection_risk(*_pair(arrive, depart)) is risk


def test_an_airport_change_is_high_even_with_hours_to_spare():
    first, second = _pair("2026-10-01T10:00", "2026-10-01T18:00", via_in="TOJ")
    assert connection_risk(first, second) is Risk.HIGH


def test_a_missing_time_is_unknown_not_high():
    first = offer("100", seg("LPA", "MAD", "2026-10-01T07:00", None))
    second = offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00"))
    assert connection_risk(first, second) is Risk.UNKNOWN
    assert connection_risk(offer("100"), second) is Risk.UNKNOWN     # no segments at all


def test_a_missing_offer_is_not_judged():
    assert connection_risk(None, offer("500")) is None


def test_thresholds_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "RISK_HIGH_BELOW_HOURS", 3.0)
    monkeypatch.setattr(config, "RISK_MEDIUM_BELOW_HOURS", 6.0)
    assert connection_risk(*_pair("2026-10-01T10:00", "2026-10-01T12:30")) is Risk.HIGH
    assert connection_risk(*_pair("2026-10-01T10:00", "2026-10-01T15:00")) is Risk.MEDIUM


def test_a_round_trip_takes_the_worse_direction_with_reasons():
    rt = standard_round_trip(
        dom_ret=offer("90", seg("MAD", "LPA", "2026-10-15T19:00", "2026-10-15T20:45")),
    )  # return: lands 18:00, domestic leaves 19:00 -> 1h
    risk, reasons = itinerary_risk(rt)
    assert risk is Risk.HIGH
    assert reasons == ["3h00m between tickets at MAD", "1h00m between tickets at MAD"]


def test_reasons_name_airport_changes_and_impossible_connections():
    first, second = _pair("2026-10-01T10:00", "2026-10-01T18:00", via_in="TOJ")
    change = one_way(dom=first, onward=second)
    assert itinerary_risk(change) == (Risk.HIGH, ["arrive MAD, depart TOJ"])
    first, second = _pair("2026-10-01T14:00", "2026-10-01T13:00")
    impossible = one_way(dom=first, onward=second)
    assert itinerary_risk(impossible) == (
        Risk.IMPOSSIBLE, ["the second ticket leaves before the first lands at MAD"])


def test_an_estimate_is_not_judged():
    assert itinerary_risk(one_way()) is None


def test_every_level_has_a_label():
    assert set(RISK_LABELS) == set(Risk)


def test_invalid_thresholds_are_a_config_error(monkeypatch):
    monkeypatch.setattr(config, "RISK_HIGH_BELOW_HOURS", 5.0)
    monkeypatch.setattr(config, "RISK_MEDIUM_BELOW_HOURS", 4.0)
    with pytest.raises(config.ConfigError, match="RISK_HIGH_BELOW_HOURS"):
        config.validate()
