"""Tests for the URL encoder, date helpers and the Google Flights parser."""
from __future__ import annotations

import base64

import pytest

from models import fmt_dur, generate_dates
from providers.google import (
    FlightResult,
    ParseError,
    _varint,
    build_url,
    encode_tfs,
    parse_flights,
)

# ── Protobuf varint encoder ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (16384, b"\x80\x80\x01"),
    ],
)
def test_varint_matches_protobuf_spec(value, expected):
    assert _varint(value) == expected


# ── tfs URL parameter ───────────────────────────────────────────────────────


def _decode_tfs(tfs: str) -> bytes:
    """Re-add the stripped base64 padding and decode."""
    return base64.b64decode(tfs + "=" * (-len(tfs) % 4))


def test_encode_tfs_embeds_both_airport_codes():
    raw = _decode_tfs(encode_tfs("LPA", "MAD", "2026-09-15"))
    assert b"LPA" in raw
    assert b"MAD" in raw
    assert b"2026-09-15" in raw


def test_encode_tfs_one_way_and_round_trip_differ():
    one_way = encode_tfs("LPA", "MAD", "2026-09-15")
    round_trip = encode_tfs("LPA", "MAD", "2026-09-15", return_date="2026-09-22")

    assert one_way != round_trip
    # The return date only appears in the round-trip payload.
    assert b"2026-09-22" not in _decode_tfs(one_way)
    assert b"2026-09-22" in _decode_tfs(round_trip)


def test_encode_tfs_round_trip_sets_trip_type_flag():
    # Field 2 carries the trip type: 1 = round-trip, 2 = one-way.
    assert _decode_tfs(encode_tfs("LPA", "MAD", "2026-09-15"))[2:4] == b"\x10\x02"
    assert (
        _decode_tfs(encode_tfs("LPA", "MAD", "2026-09-15", return_date="2026-09-22"))[2:4]
        == b"\x10\x01"
    )


def test_encode_tfs_encodes_one_passenger_entry_per_adult():
    solo = _decode_tfs(encode_tfs("LPA", "MAD", "2026-09-15", adults=1))
    family = _decode_tfs(encode_tfs("LPA", "MAD", "2026-09-15", adults=3))
    # Each adult adds a 2-byte entry (\x08\x01) to the passenger block (field 16).
    assert solo.count(b"\x08\x01") + 2 == family.count(b"\x08\x01")
    assert len(family) == len(solo) + 4


def test_build_url_carries_currency_and_tfs():
    url = build_url("LPA", "MAD", "2026-09-15", currency="USD")
    assert url.startswith("https://www.google.com/travel/flights/search?tfs=")
    assert "curr=USD" in url
    assert "=" not in url.split("tfs=")[1].split("&")[0]  # padding stripped


# ── Date helpers ────────────────────────────────────────────────────────────


def test_generate_dates_inclusive_with_step():
    assert generate_dates("2026-09-01", "2026-09-10", 3) == [
        "2026-09-01",
        "2026-09-04",
        "2026-09-07",
        "2026-09-10",
    ]


def test_generate_dates_single_day_range():
    assert generate_dates("2026-09-01", "2026-09-01", 7) == ["2026-09-01"]


def test_generate_dates_end_before_start_is_empty():
    assert generate_dates("2026-09-10", "2026-09-01", 3) == []


def test_generate_dates_rejects_bad_format():
    with pytest.raises(ValueError):
        generate_dates("tomorrow", "2026-09-01", 3)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "?"), (-5, "?"), (45, "0h45m"), (60, "1h00m"), (170, "2h50m")],
)
def test_fmt_dur(minutes, expected):
    assert fmt_dur(minutes) == expected


# ── Parser (pinned against a real captured response) ────────────────────────


def test_parse_flights_reads_real_capture(real_html):
    flights = parse_flights(real_html)

    assert len(flights) == 3
    assert [f.price for f in flights] == [37, 41, 56]
    assert flights[0].airlines == ["Ryanair"]
    assert flights[0].stops == 0
    assert flights[0].duration == 170
    assert flights[0].route_str == "LPA -> MAD"


def test_parse_flights_returns_results_sorted_by_price(real_html):
    prices = [f.price for f in parse_flights(real_html)]
    assert prices == sorted(prices)


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        ("", "too short"),
        ("too short", "too short"),
        ("<html>" + "x" * 2000 + "</html>", "no ds:1"),  # long enough, no ds:1 block
        (
            '<script class="ds:1">AF_initDataCallback({data:not json, sideChannel: {}});</script>'
            + "x" * 2000,
            "not valid JSON",
        ),
        ('<script class="ds:1">no data field here</script>' + "x" * 2000, "no data: field"),
    ],
)
def test_parse_flights_raises_on_non_results_page(html, reason):
    """A broken/blocked response must not be mistaken for 'no flights found'."""
    with pytest.raises(ParseError, match=reason):
        parse_flights(html)


def test_parse_flights_returns_empty_for_valid_page_without_offers():
    """A well-formed page with no offers is a normal outcome, not an error."""
    html = (
        "<html>" + "x" * 2000
        + '<script class="ds:1">AF_initDataCallback({data:[0,1,[[]],[[]]], sideChannel: {}});</script>'
        + "</html>"
    )
    assert parse_flights(html) == []


def test_route_str_without_segments():
    assert FlightResult(price=1, airlines=[], stops=0, duration=0).route_str == "?"


def test_route_str_chains_multi_leg_segments():
    flight = FlightResult(
        price=1,
        airlines=[],
        stops=1,
        duration=0,
        segments=[{"from": "LPA", "to": "MAD"}, {"from": "MAD", "to": "NRT"}],
    )
    assert flight.route_str == "LPA -> MAD -> NRT"
