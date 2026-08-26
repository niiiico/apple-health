"""Tests for the heart-rate series providers.

The contract both renderers now depend on: same shape from either source, and
`None` reserved for *no series was recorded* — which is a different fact from
an empty one, and the renderers say different things about each.
"""

from __future__ import annotations

from apple_health.sources.hr_series import InboxSeries

UUID = "19DEA167-88E7-452F-AAE8-7F08A0F7CB3B"


def _write(inbox, uuid: str, rows: str) -> None:
    (inbox / f"hr-{uuid}.csv").write_text("time,bpm\n" + rows)


def test_reads_a_sidecar_as_epoch_bpm_pairs(tmp_path):
    _write(tmp_path, UUID, "2026-08-12T10:12:45Z,128\n2026-08-12T10:12:50Z,122\n")
    series = InboxSeries(tmp_path).series_for(UUID)
    assert series is not None
    assert [bpm for _, bpm in series] == [128.0, 122.0]
    # Five seconds apart, as the watch sampled them.
    assert series[1][0] - series[0][0] == 5.0


def test_a_missing_sidecar_is_none_not_empty(tmp_path):
    # The renderers print "Séries FC indisponibles" for None. An empty list
    # would instead render a zone table over nothing.
    assert InboxSeries(tmp_path).series_for(UUID) is None


def test_a_workout_without_a_uuid_has_no_series(tmp_path):
    # Full-export rows carry no HealthKit uuid, so there is nothing to look up.
    assert InboxSeries(tmp_path).series_for(None) is None


def test_header_only_sidecar_yields_an_empty_series(tmp_path):
    # Distinct from missing: the file exists and recorded nothing.
    _write(tmp_path, UUID, "")
    assert InboxSeries(tmp_path).series_for(UUID) == []


class _Fake:
    """A source with a fixed answer, for exercising the fallback order."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = 0

    def series_for(self, uuid):
        self.asked += 1
        return self.answer


def test_fallback_prefers_the_first_source_that_has_a_series():
    from apple_health.sources.hr_series import FallbackSeries

    primary, secondary = _Fake([(1.0, 120.0)]), _Fake([(2.0, 130.0)])
    assert FallbackSeries(primary, secondary).series_for("u") == [(1.0, 120.0)]
    assert secondary.asked == 0, "must not consult the fallback when the first answered"


def test_fallback_reaches_past_a_source_with_nothing():
    # The case that matters: a workout ah-pgsync has not reached yet still
    # renders from its sidecar instead of silently losing its zones.
    from apple_health.sources.hr_series import FallbackSeries

    assert FallbackSeries(_Fake(None), _Fake([(2.0, 130.0)])).series_for("u") == [(2.0, 130.0)]


def test_fallback_distinguishes_empty_from_missing():
    # An empty series is an answer: the file existed and recorded nothing.
    from apple_health.sources.hr_series import FallbackSeries

    secondary = _Fake([(2.0, 130.0)])
    assert FallbackSeries(_Fake([]), secondary).series_for("u") == []
    assert secondary.asked == 0


def test_no_source_has_it():
    from apple_health.sources.hr_series import FallbackSeries

    assert FallbackSeries(_Fake(None), _Fake(None)).series_for("u") is None
