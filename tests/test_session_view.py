"""The session and sessions views: donut, profile, legs, filters.

These are pure functions of already-fetched rows, which is why `ui` is kept
apart from `web` — rendering only reachable through a running server is
rendering nobody tests.
"""

from __future__ import annotations

from datetime import date

from apple_health import ui


def _hr(**seconds: int) -> dict:
    labels = ["Z1 <135", "Z2 135-159", "Z3 160-169", "Z4 170-177", "Z5 >=178"]
    secs = dict(zip(labels, [seconds.get(f"z{i}", 0) for i in range(1, 6)]))
    total = sum(secs.values()) or 1
    return {"zone_seconds": secs,
            "zone_percent": {k: v / total * 100 for k, v in secs.items()}}


# --- the zone donut ----------------------------------------------------------

def test_no_donut_without_zone_time():
    """A ring of nothing is worse than no ring."""
    assert ui.zone_donut({"zone_seconds": {}, "zone_percent": {}}) == ""


def test_each_zone_with_time_gets_a_slice():
    html = ui.zone_donut(_hr(z1=600, z2=1200, z3=600))
    for cls in ("slice z1", "slice z2", "slice z3"):
        assert cls in html
    # Z4 and Z5 had no time; they are absent, not drawn at zero length.
    assert "slice z4" not in html


def test_slices_carry_a_title_for_hover_and_screen_readers():
    """Identity is never colour alone — the ring is also a legend and a title."""
    html = ui.zone_donut(_hr(z1=600, z2=1200))
    assert "<title>" in html
    assert "Z2 135-159" in html


def test_the_legend_names_every_slice():
    html = ui.zone_donut(_hr(z1=600, z2=1200, z3=300))
    assert html.count('class="key"') == 3


def test_the_centre_carries_the_total_minutes():
    """A donut whose hole says nothing wastes where the eye lands first."""
    html = ui.zone_donut(_hr(z1=600, z2=600))     # 20 min
    assert ">20</text>" in html


def test_shares_are_of_recorded_time_only():
    html = ui.zone_donut(_hr(z1=900, z2=2700))    # 25 % / 75 %
    assert "<b>25 %</b>" in html and "<b>75 %</b>" in html


# --- legs vs markers ---------------------------------------------------------

def test_a_single_segment_is_not_shown_as_a_leg_table():
    """Every workout has one `HKWorkoutActivity`; only multisport has more.

    A one-row "Segments" table above a "61× segment" marker line made the page
    appear to claim one segment and sixty-one at once.
    """
    segs = [{"idx": 1, "activity": "Cycling", "started_at": "2026-09-02T11:28:56",
             "duration_s": 7000, "stats": {"HeartRate": {"avg": 132}}}]
    out = ui.segments_section(segs, None)
    assert "<h2>Legs" not in out


def test_two_segments_are_shown_as_legs_with_their_sport():
    segs = [{"idx": 1, "activity": "Swimming", "started_at": "2026-06-28T09:05:00",
             "duration_s": 1937, "stats": {}},
            {"idx": 2, "activity": "Cycling", "started_at": "2026-06-28T09:40:00",
             "duration_s": 4893, "stats": {}}]
    out = ui.segments_section(segs, None)
    assert "<h2>Legs" in out
    assert "Natation" in out and "Velo" in out


def test_a_lone_segments_measures_are_surfaced_rather_than_counted():
    """"9 mesure(s)" threw away running power, stride length and the rest —
    numbers held nowhere else in the record."""
    segs = [{"idx": 1, "activity": "Running", "started_at": "2026-09-03T18:48:37",
             "duration_s": 4229,
             "stats": {"HeartRate": {"avg": 158},
                       "RunningPower": {"avg": 288.4},
                       "RunningStrideLength": {"avg": 1.13}}}]
    out = ui.segments_section(segs, None)
    assert "Puissance" in out and "288.4" in out and "W" in out
    assert "Longueur de foulée" in out
    # Heart rate has its own section with the series behind it.
    assert "HeartRate" not in out


def test_marker_kinds_are_named_not_left_as_enums():
    out = ui.segments_section(None, [{"kind": "segment", "count": 61},
                                     {"kind": "motionPaused", "count": 3}])
    assert "61× repère auto" in out
    assert "3× pause auto" in out
    assert "motionPaused" not in out


def test_markers_say_they_are_not_the_legs():
    """The two things share Apple's name; the page must not."""
    out = ui.segments_section(None, [{"kind": "segment", "count": 61}])
    assert "legs" in out.lower()


# --- the sessions list -------------------------------------------------------

def _sessions() -> list[dict]:
    return [
        {"id": 1, "date": "2026-09-01", "activity": "Swimming", "duration_min": 60},
        {"id": 2, "date": "2026-09-05", "activity": "Running", "duration_min": 70},
        {"id": 3, "date": "2026-09-03", "activity": "Running", "duration_min": 40,
         "race": "2026-09-03-course"},
    ]


def test_sessions_are_listed_newest_first():
    """The question asked of this page is about the last few days."""
    ctx = {"coverage": {"observed_through": "2026-09-05T04:40:40+00:00"},
           "record": {}}
    html = ui.render_sessions(ctx, _sessions(), date(2026, 8, 1), date(2026, 9, 6))
    import re
    dates = re.findall(r'class="when">(\d{4}-\d\d-\d\d)<', html)
    assert dates == sorted(dates, reverse=True)


def test_filtering_by_sport_keeps_only_that_sport():
    ctx = {"coverage": {}, "record": {}}
    html = ui.render_sessions(ctx, _sessions(), date(2026, 8, 1), date(2026, 9, 6),
                              activity="Running")
    assert html.count('class="when">') == 2


def test_filter_counts_are_of_the_window_not_the_filtered_view():
    """A filter that renumbers itself gives no way to see what it removed."""
    ctx = {"coverage": {}, "record": {}}
    html = ui.render_sessions(ctx, _sessions(), date(2026, 8, 1), date(2026, 9, 6),
                              activity="Running")
    assert "tout (3)" in html
    assert "Natation (1)" in html


def test_a_race_is_flagged_and_filterable():
    ctx = {"coverage": {}, "record": {}}
    html = ui.render_sessions(ctx, _sessions(), date(2026, 8, 1), date(2026, 9, 6))
    assert 'class="flag race"' in html
    html = ui.render_sessions(ctx, _sessions(), date(2026, 8, 1), date(2026, 9, 6),
                              races_only=True)
    assert html.count('class="when">') == 1


def test_the_race_filter_is_offered_even_when_the_window_holds_none():
    """It widens the window to the whole record, so an empty count here is not
    an empty answer — it is the one filter that is not a subset of the page."""
    ctx = {"coverage": {}, "record": {}}
    plain = [s for s in _sessions() if not s.get("race")]
    html = ui.render_sessions(ctx, plain, date(2026, 8, 1), date(2026, 9, 6))
    assert "races=1" in html


def test_the_window_is_stated_in_words():
    """It was implied by two arrows and a pair of dates, so the page read as
    the whole record with an odd beginning."""
    ctx = {"coverage": {}, "record": {}}
    html = ui.render_sessions(ctx, _sessions(), date(2026, 8, 1), date(2026, 9, 6))
    assert "Fenêtre affichée" in html
    assert "37 jours" in html
