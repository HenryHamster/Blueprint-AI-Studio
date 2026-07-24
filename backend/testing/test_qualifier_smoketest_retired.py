"""Durable invariants for the retired qualifier VM-smoketest lane."""

from pathlib import Path

import pytest

from app.ci_resolution import _CI_PENDING, _CI_RED
from app.qualification_dimensions import LEDGER_DIMENSION_KEYS, UI_DIMENSIONS
from app.release_qualifier import _normalize_active_promotion_weights
from app.release_scoring import compute_composite

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
AGENT_HANDLER = Path(__file__).resolve().parents[2] / "agent" / "src" / "handlers" / "specialized.ts"


def _score(*, ci_green: bool = True, ci_state: str = "green", smoke=None):
    return compute_composite(
        ci_green=ci_green,
        ci_state=ci_state,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=smoke,
        smoketest_enabled=True,
        policy={"weights": {"audit": 0.4, "test_erosion": 0.3, "smoketest": 0.3}, "threshold": 0.9},
    )


def test_no_production_qualifier_smoketest_creator_exists():
    creators = []
    for path in APP_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if '"qualifier_smoketest": True' in source or "def _spawn_qualifier_smoketest_task" in source:
            creators.append(path.name)
    assert creators == []


def test_qualifier_never_spawns_respawns_reads_or_ledgers_smoke():
    source = (APP_ROOT / "release_qualifier.py").read_text(encoding="utf-8")
    assert "_spawn_qualifier_smoketest_task" not in source
    assert "ArtifactType.SMOKETEST_RESULT" not in source
    assert '"dimension": "smoketest"' not in source
    assert "smoketest_infra_respawn" not in source
    assert "smoketest_admin_cancel_respawn" not in source


def test_smoke_is_absent_from_current_score_and_dimension_registry():
    low = _score(smoke=0.0)
    missing = _score(smoke=None)
    high = _score(smoke=1.0)
    assert low == missing == high
    assert "smoketest" not in high["sub_scores"]
    assert "smoketest" not in LEDGER_DIMENSION_KEYS
    assert "smoketest" not in {key for key, _label in UI_DIMENSIONS}


def test_legacy_three_key_policy_matches_new_two_key_policy():
    legacy = _normalize_active_promotion_weights(
        {"audit": 0.4, "test_erosion": 0.3, "smoketest": 0.3}
    )
    current = _normalize_active_promotion_weights(
        {"audit": 4 / 7, "test_erosion": 3 / 7}
    )
    assert legacy == pytest.approx(current)


def test_ci_red_and_pending_remain_hard_blockers():
    red = _score(ci_green=False, ci_state=_CI_RED)
    pending = _score(ci_green=False, ci_state=_CI_PENDING)
    assert red["blocker"] == "ci_not_green"
    assert pending["blocker"] == "ci_unresolved"
    assert not red["promote"]
    assert not pending["promote"]


def test_ordinary_post_promotion_smoke_handler_remains():
    lifecycle = (APP_ROOT / "task_lifecycle.py").read_text(encoding="utf-8")
    assert "post-promotion" in lifecycle
    assert 'get("smoketest_enabled")' in lifecycle
    # The agent tree is not present in the backend-only Docker test context;
    # gate the handler-side assertions on its availability (matches the
    # convention used across test_ci_compliance_gate / test_efficiency_snapshot).
    if not AGENT_HANDLER.exists():
        pytest.skip("agent/src not available in this test environment")
    agent = AGENT_HANDLER.read_text(encoding="utf-8")
    assert "export async function handleSmoketest" in agent
    assert "qualifierSmoketest" not in agent
