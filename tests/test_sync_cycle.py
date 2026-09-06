"""The order and the failure behaviour of one `ah-sync` cycle.

Every step is stubbed: what is under test is the chaining — which step runs,
after which, and what a failure in one does to the ones after it. Those are the
decisions that go wrong silently, and the ones a live run cannot demonstrate
without a real phone, a real iCloud folder and a real Postgres.
"""

from __future__ import annotations

import pytest

from apple_health.commands import sync


@pytest.fixture
def cycle(monkeypatch):
    """Stub every step, recording the order they ran in."""
    calls: list[str] = []

    def stub(name, rc=0):
        def run(argv=None):
            calls.append(name)
            return rc
        return run

    monkeypatch.setenv("APPLE_HEALTH_DSN", "postgresql://stub/stub")
    # icloud must report *something* on stdout or the cycle stops as "nothing new".
    def fetch(argv=None):
        calls.append("icloud")
        print("delta-20260905T044040Z-0027.json")
        return 0
    monkeypatch.setattr(sync.icloud, "main", fetch)
    monkeypatch.setattr(sync.healthsync, "main", stub("healthsync"))
    monkeypatch.setattr(sync.pgsync, "main", stub("pgsync"))
    monkeypatch.setattr(sync.routepoints, "main", stub("routepoints"))
    monkeypatch.setattr(sync.session_files, "main", stub("session_files"))
    monkeypatch.setattr(sync.vault_box, "main", stub("vault_box"))
    return calls


def test_route_points_are_loaded_every_cycle(cycle):
    """The step that was missing. Without it a new session has a route summary
    and no track, so the map and the elevation profile are simply absent until
    somebody remembers the command — which is how two days of riding had no map.
    """
    assert sync.main([]) == 0
    assert "routepoints" in cycle


def test_route_points_run_after_postgres(cycle):
    """`pgsync` is what creates the `routes` rows the points attach to; before
    it there is nothing to attach them to."""
    sync.main([])
    assert cycle.index("routepoints") > cycle.index("pgsync")


def test_the_whole_order(cycle):
    sync.main([])
    assert cycle == ["icloud", "healthsync", "pgsync", "routepoints",
                     "session_files", "vault_box"]


def test_a_postgres_failure_stops_before_route_points(cycle, monkeypatch):
    """Coverage would read stale, which is the failure this project exists to
    stop — there is no point drawing maps on top of it."""
    monkeypatch.setattr(sync.pgsync, "main", lambda argv=None: 1)
    assert sync.main([]) == 1
    assert "routepoints" not in cycle


def test_a_route_failure_does_not_block_the_rest(cycle, monkeypatch):
    """A GPX that will not parse costs a map, not a number: the workout, its
    distance and its heart rate are already in from the delta. Blocking the
    Vault brief over one would trade something that matters for something that
    does not."""
    def boom(argv=None):
        raise RuntimeError("mangled gpx")
    monkeypatch.setattr(sync.routepoints, "main", boom)
    assert sync.main([]) == 0
    assert "session_files" in cycle and "vault_box" in cycle


def test_a_route_failure_is_reported_not_swallowed(cycle, monkeypatch, capsys):
    """A step that quietly skips work looks identical to one that had none."""
    def boom(argv=None):
        raise RuntimeError("mangled gpx")
    monkeypatch.setattr(sync.routepoints, "main", boom)
    sync.main([])
    assert "route points failed" in capsys.readouterr().err


def test_nothing_new_runs_nothing(monkeypatch):
    """An empty fetch means an empty cycle — including the new step."""
    calls: list[str] = []
    monkeypatch.setattr(sync.icloud, "main", lambda argv=None: 0)
    monkeypatch.setattr(sync.routepoints, "main",
                        lambda argv=None: calls.append("routepoints"))
    assert sync.main([]) == 0
    assert calls == []


def test_without_a_dsn_the_postgres_half_is_skipped(cycle, monkeypatch):
    """The launchd plist carries the DSN; a hand run without it should still
    render and push rather than failing."""
    monkeypatch.delenv("APPLE_HEALTH_DSN", raising=False)
    assert sync.main([]) == 0
    assert "pgsync" not in cycle and "routepoints" not in cycle
    assert "session_files" in cycle
