"""What a known-bad app build wrote, corrected before it is stored.

The deltas are the source for the delta era: a rebuild replays every file in
the archive, so repairing the database alone would have fixed these values only
until the next re-base. These tests pin the correction at the ingest, where the
replay goes through.

Both bugs are silent by construction — 79 % humidity and a pool swim are the
plausible readings of the wrong values — which is why they are pinned rather
than trusted to stay fixed.
"""

from __future__ import annotations

from apple_health.commands.pgsync import _build, _repair_conditions


# --- reading the build -------------------------------------------------------

def test_the_build_is_read_from_the_version_string():
    assert _build("1.0 (46)") == 46


def test_a_version_without_a_build_reads_as_old():
    """Builds up to 44 reported a bare "1.0" — the build number was itself one
    of the things missing, so None means old, not unknown."""
    assert _build("1.0") is None
    assert _build(None) is None


# --- humidity ----------------------------------------------------------------

def test_humidity_is_divided_by_a_hundred():
    """The app multiplied by 100, believing `HKUnit.percent()` is a fraction."""
    assert _repair_conditions({"weather_humidity_pct": 7900.0}, None) == {
        "weather_humidity_pct": 79.0}


def test_a_plausible_humidity_is_left_alone():
    assert _repair_conditions({"weather_humidity_pct": 79.0}, None) == {
        "weather_humidity_pct": 79.0}


def test_humidity_is_corrected_on_the_value_not_the_build():
    """No humidity is above 100, so this stays right for any future build that
    gets it wrong again — unlike the location, which needs the build."""
    assert _repair_conditions(
        {"weather_humidity_pct": 8300.0}, 99)["weather_humidity_pct"] == 83.0


# --- swimming location -------------------------------------------------------

def test_the_location_is_re_derived_from_the_lap_length():
    """The old label is discarded rather than flipped.

    `intValue == 1 ? "openWater" : "pool"` sent raw 2 (openWater) *and* raw 0
    (unknown) to "pool", so the label carries two meanings and flipping it
    would state open water about a swim HealthKit could not place.
    """
    assert _repair_conditions(
        {"swim_location": "openWater", "pool_length_m": 25.0}, None
    ) == {"swim_location": "pool", "pool_length_m": 25.0}
    assert _repair_conditions(
        {"swim_location": "pool", "pool_length_m": None}, None
    ) == {"swim_location": "openWater", "pool_length_m": None}


def test_an_ambiguous_pool_label_with_a_lap_length_stays_a_pool():
    """A blind flip made this open water — a pool swim with a 25 m lap length.

    This is the case the two repair paths would have disagreed on: migration 16
    reads the lap length and says pool, so a re-base replaying the same delta
    had to say pool too or the label would move on its own.
    """
    assert _repair_conditions(
        {"swim_location": "pool", "pool_length_m": 25.0}, None
    )["swim_location"] == "pool"


def test_a_trusted_build_is_taken_at_face_value():
    """Re-deriving a fixed build's output would discard what it correctly knew
    — including an open-water swim that happens to carry a lap length."""
    assert _repair_conditions(
        {"swim_location": "openWater", "pool_length_m": 25.0}, 46
    )["swim_location"] == "openWater"


def test_a_missing_location_stays_missing():
    """Absent is not unknown-and-guessed: nothing invents a location."""
    assert _repair_conditions({"swim_location": None, "pool_length_m": None},
                              None) == {"swim_location": None,
                                        "pool_length_m": None}


# --- everything else ---------------------------------------------------------

def test_other_conditions_are_untouched():
    """Only the two known-wrong fields are rewritten; the rest is the record."""
    original = {"weather_temp_c": 26.5, "elevation_ascended_m": 90.72,
                "avg_mets": 10.2, "pool_length_m": 25.0, "max_speed_kmh": 18.3}
    assert _repair_conditions(original, None) == original


def test_the_delta_is_not_mutated():
    """The caller's dict is the parsed delta; correcting it in place would make
    the file and what was stored disagree for anything reading it afterwards."""
    delta = {"weather_humidity_pct": 7900.0}
    _repair_conditions(delta, None)
    assert delta == {"weather_humidity_pct": 7900.0}
