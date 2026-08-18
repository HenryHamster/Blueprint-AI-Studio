"""Cross-process orchestrator health authority regressions."""

from app.orchestrator_health_authority import project_background_leader_heartbeat


def test_fresh_background_leader_heartbeat_is_ok():
    health = project_background_leader_heartbeat(
        4.5, stale_after_seconds=90.0, leader_pid=123
    )

    assert health["status"] == "ok"
    assert health["authority"] == "background_leader_advisory_lock"
    assert health["heartbeat_age_seconds"] == 4.5
    assert health["cycle_age_seconds"] is None
    assert health["cycle_age_kind"] == "not_observable_cross_process"


def test_stale_background_leader_heartbeat_is_down():
    health = project_background_leader_heartbeat(
        91.0, stale_after_seconds=90.0, leader_pid=123
    )

    assert health["status"] == "down"
    assert health["reason"] == "background leader keepalive is stale"


def test_missing_background_leader_lock_is_down_without_fake_age():
    health = project_background_leader_heartbeat(None, stale_after_seconds=90.0)

    assert health["status"] == "down"
    assert health["heartbeat_age_seconds"] is None
    assert health["reason"] == "no process holds the background-leader advisory lock"
