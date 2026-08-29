"""Tests for the workout-metadata conversions.

Apple writes these with units that do not match their key names, and every
mistake here is silent: a wrong temperature is still a plausible temperature.
That is why each conversion is tested against the literal strings the export
actually contains.
"""

from __future__ import annotations

import pytest

from apple_health.commands.enrich import (
    humidity_pct, length_m, speed_kmh, temperature_c,
)


# --- temperature -------------------------------------------------------------

def test_fahrenheit_is_converted():
    """The export on this device writes degF, and the key does not say so.

    Reading "79 degF" as Celsius puts a warm morning at 79 °C — still a number,
    still plottable, and wrong by fifty degrees.
    """
    assert temperature_c("79 degF") == pytest.approx(26.11, abs=0.01)


def test_celsius_passes_through():
    assert temperature_c("21 degC") == pytest.approx(21.0)


def test_a_bare_number_is_taken_as_celsius():
    assert temperature_c("18") == pytest.approx(18.0)


def test_an_unknown_unit_is_refused_rather_than_guessed():
    """Better no temperature than a wrong one presented with confidence."""
    assert temperature_c("300 rankine") is None


def test_missing_temperature_is_none_not_zero():
    """Zero is a temperature. Absent is not."""
    assert temperature_c(None) is None
    assert temperature_c("") is None


# --- humidity ----------------------------------------------------------------

def test_humidity_is_scaled_from_apples_hundredths():
    """Apple writes "6100 %" for 61 %. Taken literally it is 6100 % humidity."""
    assert humidity_pct("6100 %") == pytest.approx(61.0)


def test_a_plausible_humidity_is_left_alone():
    assert humidity_pct("61 %") == pytest.approx(61.0)


# --- length ------------------------------------------------------------------

def test_centimetres_become_metres():
    """Elevation is written in centimetres despite reading like metres."""
    assert length_m("12345 cm") == pytest.approx(123.45)


def test_yards_become_metres():
    """A 25 yd pool is not a 25 m pool, and the swim splits depend on which."""
    assert length_m("25 yd") == pytest.approx(22.86)


def test_metres_pass_through():
    assert length_m("25 m") == pytest.approx(25.0)


def test_an_unknown_length_unit_is_refused():
    assert length_m("40 furlongs") is None


# --- speed -------------------------------------------------------------------

def test_metres_per_second_become_kmh():
    assert speed_kmh("10 m/s") == pytest.approx(36.0)


def test_an_unknown_speed_unit_is_refused():
    assert speed_kmh("5 knots") is None
