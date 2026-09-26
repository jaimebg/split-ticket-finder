"""results.view: every screen as text + button specs, with no bot."""
from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from models import Progress
from results.filters import Filters
from results.store import StoredResults
from results.view import (
    PAGE_SIZE,
    SearchMeta,
    detail,
    filters_screen,
    phase_label,
    progress_text,
    savings_text,
    summary,
)
from tests.results_fixtures import (
    offer,
    one_way,
    onward_out,
    seg,
    standard_one_way,
    standard_round_trip,
)

META = SearchMeta(
    search_id=7, origin="LPA", destinations=("NRT",), currency="EUR",
    round_trip=False, strategy="two-stage", sampled_dates=1, window_days=91,
    fallback_through_fare=None,
)


def _data(rows):
    return [b.data for row in rows for b in row]


def _many(n):
    return [standard_one_way(date=f"2026-10-{d:02d}") for d in range(1, n + 1)]


# ── Summary ─────────────────────────────────────────────────────────────────

def test_summary_pages_five_at_a_time_with_navigation():
    text, rows = summary(META, StoredResults(_many(12), True), Filters(), 2)
    assert "12 routes" in text
    assert "6. " in text and "10. " in text and "11. " not in text
    data = _data(rows)
    assert "r:7:d:5" in data                     # stored index, not screen position
    assert "r:7:p:1" in data and "r:7:p:3" in data
    assert "r:7:f" in data


def test_first_page_has_no_back_target():
    _, rows = summary(META, StoredResults(_many(12), True), Filters(), 1)
    assert "r:7:p:0" not in _data(rows)


def test_page_beyond_range_is_clamped():
    """Review Focus #3: a stored page 5 after filters shrink the list to one page."""
    text, _ = summary(META, StoredResults(_many(3), True), Filters(), 5)
    assert "1. " in text


def test_status_markers_distinguish_partial_from_estimate():
    partial = standard_round_trip(dom_ret=None)
    estimate = one_way(est_dom_price=Decimal("29"), est_onward_price=Decimal("500"))
    text, _ = summary(META, StoredResults([standard_one_way(), partial, estimate], True),
                      Filters(), 1)
    assert "partial" in text
    assert "est." in text


def test_savings_are_shown_and_a_cheaper_single_ticket_says_so():
    good = standard_one_way(through_fare="700")          # total 525
    assert "Save 175.00 EUR (25%)" in savings_text(good, "EUR", None)
    bad = standard_one_way(through_fare="500")
    line = savings_text(bad, "EUR", None)
    assert "-" not in line
    assert "single ticket is 25.00 EUR cheaper" in line


def test_savings_fall_back_to_the_row_through_fare_for_legacy_rows():
    assert savings_text(standard_one_way(), "EUR", Decimal("700")).startswith("Save")
    assert savings_text(standard_one_way(), "EUR", None) is None


def test_grid_line_says_the_window_was_sampled():
    meta = replace(META, strategy="grid", sampled_dates=12)
    text, _ = summary(meta, StoredResults(_many(1), True), Filters(), 1)
    assert "Sampled 12 of 91 days" in text


def test_hidden_line_counts_filtered_routes():
    text, rows = summary(META, StoredResults([standard_one_way(), one_way()], True),
                         Filters(max_stops=1), 1)
    assert "1 route hidden by filters" in text
    assert any(b.label.endswith("· 1") for row in rows for b in row)


def test_filters_that_hide_everything_still_offer_the_filters_button():
    """Review Focus #2."""
    text, rows = summary(META, StoredResults([standard_one_way()], True),
                         Filters(exclude=frozenset({"JL"})), 1)
    assert "No routes match these filters" in text
    assert "r:7:f" in _data(rows)


def test_undetailed_results_have_no_filters_and_say_why():
    text, rows = summary(META, StoredResults([standard_one_way()], False), Filters(), 1)
    assert "Historical snapshot" in text
    assert "r:7:f" not in _data(rows)


def test_summary_escapes_hostile_hub_codes():
    text, _ = summary(META, StoredResults([standard_one_way(hub="<b>")], True), Filters(), 1)
    assert "<b>" not in text.replace("<b>LPA", "").replace("<b>525", "")
    assert "&lt;b&gt;" in text


# ── Detail ──────────────────────────────────────────────────────────────────

def test_detail_shows_each_ticket_the_connection_and_links():
    stored = StoredResults([standard_one_way(through_fare="700")], True)
    text, rows = detail(META, stored, 0, 1)
    assert "IB100" in text and "JL100" in text
    assert "07:00" in text and "13:00" in text
    assert "3h00m between tickets" in text
    assert "25.00 EUR (100.00 EUR before 75% discount)" in text
    assert text.count('href="https://example.test/book"') == 2
    assert "checked unknown" in text        # None bags are unknown, never "none"
    assert "Save 175.00 EUR" in text
    assert _data(rows) == ["r:7:t:0", "r:7:T:0", "r:7:p:1"]


def test_detail_of_a_round_trip_has_four_tickets_and_two_connections():
    text, _ = detail(META, StoredResults([standard_round_trip()], True), 0, 1)
    for n in (1, 2, 3, 4):
        assert f"Ticket {n}" in text
    assert text.count("⏱") == 2          # one connection line per direction


def test_detail_flags_an_airport_change_and_an_impossible_connection():
    change = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00")),
        onward=offer("500", seg("TOJ", "NRT", "2026-10-01T13:00", "2026-10-02T09:00")),
    )
    text, _ = detail(META, StoredResults([change], True), 0, 1)
    assert "Airport change" in text

    impossible = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T14:00")),
        onward=offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00")),
    )
    text, _ = detail(META, StoredResults([impossible], True), 0, 1)
    assert "leaves before the first lands" in text


def test_detail_of_a_partial_names_the_missing_leg_and_links_nothing_for_it():
    text, _ = detail(META, StoredResults([one_way(dom=standard_one_way().dom_out)], True), 0, 1)
    assert "no flight chosen yet" in text
    assert text.count("href=") == 1


def test_detail_warns_about_bag_recheck_only_when_true():
    recheck = one_way(dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00",
                                            "2026-10-01T10:00"),
                                requires_bag_recheck=True),
                      onward=onward_out())
    assert "You must collect and re-check bags" in detail(META, StoredResults([recheck], True), 0, 1)[0]
    assert "You must collect and re-check bags" not in detail(META, StoredResults([standard_one_way()], True), 0, 1)[0]


def test_detail_of_an_undetailed_row_is_a_labelled_snapshot():
    text, _ = detail(META, StoredResults([standard_one_way()], False), 0, 1)
    assert "Historical snapshot" in text
    assert "href=" not in text


def test_detail_escapes_hostile_provider_strings():
    hostile = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00",
                             name="<script>", no="<i>1"), url='x" onclick="y'),
        onward=standard_one_way().onward_out,
    )
    text, _ = detail(META, StoredResults([hostile], True), 0, 1)
    assert "<script>" not in text and "<i>1" not in text
    assert 'onclick="y"' not in text


# ── Filters screen ──────────────────────────────────────────────────────────

def test_filters_screen_marks_the_current_choice_and_lists_carriers():
    stored = StoredResults([standard_one_way()], True)
    text, rows = filters_screen(META, stored, Filters(max_stops=0, exclude=frozenset({"JL"})))
    assert "0 of 1 routes shown" in text
    labels = {b.data: b.label for row in rows for b in row}
    assert labels["r:7:f:s:0"].startswith("•")
    assert not labels["r:7:f:s:any"].startswith("•")
    assert labels["r:7:f:x:JL"].startswith("✗")
    assert labels["r:7:f:x:IB"].startswith("✓")
    assert "r:7:f:clear" in labels and "r:7:p:1" in labels


def test_every_callback_fits_telegrams_64_bytes():
    meta = replace(META, search_id=10**9)
    stored = StoredResults(_many(12), True)
    for text_rows in (summary(meta, stored, Filters(), 2), detail(meta, stored, 11, 3),
                      filters_screen(meta, stored, Filters())):
        assert all(len(d.encode()) <= 64 for d in _data(text_rows[1]))


# ── Progress ────────────────────────────────────────────────────────────────

def test_phase_labels_depend_on_strategy_and_cross_check_reads_forward():
    assert phase_label("Phase 1", "two-stage") == "Confirming flights"
    assert phase_label("Phase 1", "grid") == "Searching domestic flights"
    assert phase_label("Phase 2", "two-stage") == "Pricing the single-ticket fare"
    assert phase_label("Phase 1 (cross-check)", "two-stage") == "Cross-checking with a second source"
    assert phase_label("Phase 9", "two-stage") == "Phase 9"


def test_progress_text_before_any_tick_and_with_a_best_price():
    assert progress_text(None, "two-stage", "EUR") == "Starting search…"
    est = Progress(phase="Phase 1", done=3, total=10, best_total=Decimal("612"))
    assert progress_text(est, "two-stage", "EUR") == (
        "Confirming flights… 3/10\nBest so far: 612 EUR (est.)")
    none_yet = Progress(phase="Phase 0", done=1, total=4)
    assert "Best so far" not in progress_text(none_yet, "two-stage", "EUR")


# ── SearchMeta ──────────────────────────────────────────────────────────────

def test_search_meta_from_a_row():
    row = {"id": 3, "origin": "LPA", "destinations": '["NRT", "KIX"]',
           "currency": "EUR", "trip_days": 14, "strategy": "grid",
           "dates": '["2026-10-01", "2026-10-05"]', "window_start": "2026-10-01",
           "window_end": "2026-12-30", "through_fare": 785.0}
    meta = SearchMeta.from_row(row)
    assert meta.destinations == ("NRT", "KIX")
    assert meta.round_trip is True
    assert meta.sampled_dates == 2
    assert meta.window_days == 91
    assert meta.fallback_through_fare == Decimal("785.0")


def test_page_size_is_five():
    assert PAGE_SIZE == 5
    assert re.search(r"5\. ", summary(META, StoredResults(_many(5), True), Filters(), 1)[0])


# ── Ported from the deleted format_results tests (Layer 3b, Task 9) ─────────

def test_detail_reminds_to_book_separately_only_when_discounted():
    discounted = detail(META, StoredResults([standard_one_way()], True), 0, 1)[0]
    assert "Book each ticket separately" in discounted
    plain = detail(META, StoredResults([standard_one_way(discount="0")], True), 0, 1)[0]
    assert "Book each ticket separately" not in plain


def test_bag_recheck_silent_when_a_provider_says_no():
    no_recheck = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T10:00"),
                  requires_bag_recheck=False),
        onward=offer("500", seg("MAD", "NRT", "2026-10-01T13:00", "2026-10-02T09:00"),
                     requires_bag_recheck=False),
    )
    assert "You must collect and re-check bags" not in detail(META, StoredResults([no_recheck], True), 0, 1)[0]


def test_equal_prices_are_not_called_a_saving():
    line = savings_text(standard_one_way(through_fare="525"), "EUR", None)
    assert "Save" not in line and "-" not in line


def test_every_screen_fits_one_telegram_message():
    """Telegram rejects messages over 4096 characters; each screen is one
    message edited in place, so there is no splitting to fall back on."""
    many_hops = standard_round_trip(
        onward_out=offer("500", *[seg("MAD" if i == 0 else f"X{i}", "NRT" if i == 3 else f"X{i + 1}",
                                      f"2026-10-01T{10 + i:02d}:00", f"2026-10-01T{10 + i:02d}:50",
                                      name="A" * 40) for i in range(4)]),
    )
    big = StoredResults([many_hops, *_many(40)], True)
    for text, _ in (summary(META, big, Filters(), 3), detail(META, big, 0, 1),
                    filters_screen(META, big, Filters())):
        assert len(text) <= 4000


# ── Final review fixes ──────────────────────────────────────────────────────

def test_a_v2_result_without_its_own_through_fare_claims_no_saving():
    """The row's through_fare belongs to the best itinerary's destination and
    date. Borrowing it for another trip would quote a saving against a fare
    that was never priced for that trip."""
    meta = replace(META, fallback_through_fare=Decimal("800"))
    other = standard_one_way(date="2026-10-20", dest="KIX")
    stored = StoredResults([standard_one_way(through_fare="800"), other], True)

    assert "Save" not in detail(meta, stored, 1, 1)[0]
    only_other = summary(meta, stored, Filters(), 1)
    assert "Save 275.00" in only_other[0]          # the best's own fare still counts
    hidden_best = StoredResults([other], True)
    assert "Save" not in summary(meta, hidden_best, Filters(), 1)[0]


def test_the_row_fare_still_backs_an_undetailed_legacy_row():
    meta = replace(META, fallback_through_fare=Decimal("800"))
    assert "Save" in detail(meta, StoredResults([standard_one_way()], False), 0, 1)[0]


from providers.base import SearchOptions


def test_the_summary_names_a_non_default_party():
    meta = replace(META, options=SearchOptions(adults=2, children=1, cabin="BUSINESS"))
    text, _ = summary(meta, StoredResults(_many(1), True), Filters(), 1)
    assert "2 adults, 1 child · Business" in text
    assert "adult" not in summary(META, StoredResults(_many(1), True), Filters(), 1)[0]


def test_the_detail_says_the_price_is_for_everyone():
    meta = replace(META, options=SearchOptions(adults=2, children=1))
    assert "total for 3 passengers" in detail(meta, StoredResults(_many(1), True), 0, 1)[0]
    assert "passengers" not in detail(META, StoredResults(_many(1), True), 0, 1)[0]


def test_search_meta_reads_the_options():
    row = {"id": 1, "adults": 2, "children": None, "cabin": "BUSINESS", "currency": "USD"}
    assert SearchMeta.from_row(row).options == SearchOptions(adults=2, cabin="BUSINESS",
                                                             currency="USD")


def _tight_return():
    return standard_round_trip(
        dom_ret=offer("90", seg("MAD", "LPA", "2026-10-15T19:00", "2026-10-15T20:45")))


def test_the_summary_marks_a_tight_connection_with_its_own_gap():
    """Review Focus #2: the outbound has 3h, the return 1h; the 1h is shown."""
    meta = replace(META, round_trip=True)
    text, _ = summary(meta, StoredResults([_tight_return()], True), Filters(), 1)
    assert "⚠️ 1h00m" in text
    assert "3h00m" not in text


def test_impossible_and_unknown_are_marked_and_low_is_not():
    impossible = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-10-01T07:00", "2026-10-01T14:00")),
        onward=onward_out())
    unknown = one_way(dom=offer("100"), onward=offer("500"))
    text, _ = summary(META, StoredResults([impossible, unknown, standard_one_way()], True),
                      Filters(), 1)
    assert "⛔ impossible" in text and "❔ times unknown" in text
    assert text.count("⚠️") == 0


def test_the_detail_states_the_risk_and_the_separate_ticket_reminder():
    text, _ = detail(META, StoredResults([standard_one_way()], True), 0, 1)
    assert "Connection risk: Medium" in text
    assert "3h00m between tickets at MAD" in text
    assert "the second airline won't wait" in text


def test_a_night_at_the_hub_is_called_out_and_not_priced():
    night = one_way(
        dom=offer("100", seg("LPA", "MAD", "2026-09-30T18:00", "2026-09-30T21:00")),
        onward=onward_out(), overnight=True)
    text, _ = summary(META, StoredResults([night], True), Filters(), 1)
    assert "🌙" in text
    detail_text, _ = detail(META, StoredResults([night], True), 0, 1)
    assert "1 night in Madrid before your flight — not included in the price" in detail_text
    assert "Connection risk: Low" in detail_text
