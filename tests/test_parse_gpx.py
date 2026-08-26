"""Tests for the GPX route summariser."""

from __future__ import annotations

import textwrap

from apple_health.sources import gpx

# Two points ~111 m apart in latitude (0.001 deg), 60 s apart, +5 m elevation.
SAMPLE = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1" creator="Apple Health Export"
         xmlns="http://www.topografix.com/GPX/1/1">
      <trk><name>Route</name><trkseg>
        <trkpt lon="139.700000" lat="35.700000"><ele>10.0</ele><time>2024-03-01T07:00:00Z</time></trkpt>
        <trkpt lon="139.700000" lat="35.701000"><ele>15.0</ele><time>2024-03-01T07:01:00Z</time></trkpt>
      </trkseg></trk>
    </gpx>
    """)


def test_summarise(tmp_path):
    p = tmp_path / "route_2024-03-01_7.00am.gpx"
    p.write_text(SAMPLE, encoding="utf-8")
    s = gpx.summarise_gpx(p)

    assert s.n_points == 2
    assert 0.10 < s.distance_km < 0.12          # ~0.111 km
    assert abs(s.duration_min - 1.0) < 1e-6      # 60 s
    assert abs(s.elev_gain_m - 5.0) < 1e-6
    assert s.min_lat == 35.700000 and s.max_lat == 35.701000
    assert s.avg_speed_kmh is not None and s.avg_speed_kmh > 6  # ~6.7 km/h


def test_parse_routes_dir(tmp_path):
    (tmp_path / "a.gpx").write_text(SAMPLE, encoding="utf-8")
    (tmp_path / "b.gpx").write_text(SAMPLE, encoding="utf-8")
    summaries = gpx.parse_routes(tmp_path, progress_every=0)
    assert len(summaries) == 2
