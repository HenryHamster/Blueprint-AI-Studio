import pytest

from app.models import Project, Task, TaskStatus
from app.spawn_calibration import (
    _assess_project_spawn_health,
    _calibrate_scope,
    _feedback_scope_override,
    _get_failure_coaching_suffix,
)


@pytest.mark.asyncio
async def test_over_budget_active_project_tasks_degrade_spawn_health(db_session, mocker, monkeypatch):
    """D14: queued/in-flight backlog blocks additional implementation dispatch."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    mocker.patch("app.compute.any_zone_available", return_value=True)
    project = Project(
        name="D14 backlog",
        user_id="u-d14",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.QUEUED,
    ]
    for idx, status in enumerate(statuses):
        db_session.add(Task(
            project_id=project.id,
            title=f"active task {idx}",
            payloads=[{"type": "create", "prompt": "work"}],
            status=status,
            created_by="ORCHESTRATOR",
        ))
    await db_session.commit()

    degraded, reason = await _assess_project_spawn_health(
        db_session,
        project.id,
        design_cap=2,
    )

    assert degraded is True
    assert reason is not None
    assert "active task backlog" in reason


# --- _get_failure_coaching_suffix ---

def _make_dfa(direction_id: str, counts: dict) -> dict:
    """Build a minimal design_doc with direction_first_attempt_feedback."""
    return {"direction_first_attempt_feedback": {direction_id: counts}}


def test_failure_coaching_no_design_doc():
    assert _get_failure_coaching_suffix("d1", {}) is None
    assert _get_failure_coaching_suffix("d1", None) is None


def test_failure_coaching_missing_direction_entry():
    doc = _make_dfa("other_dir", {"code_defect": 5, "total": 5, "last_updated": "2026-01-01"})
    assert _get_failure_coaching_suffix("d1", doc) is None


def test_failure_coaching_total_below_threshold():
    # Only 2 rejections — below the minimum of 3.
    doc = _make_dfa("d1", {"code_defect": 2, "total": 2, "last_updated": "2026-01-01"})
    assert _get_failure_coaching_suffix("d1", doc) is None


def test_failure_coaching_no_dominant_category():
    # 4 total across 3 categories — max share is 2/4 = 50%? No: 2+1+1=4, max=2/4=0.5 ≥ 0.4
    # Use a more evenly distributed case: 3 categories of 1 each = 3 total, max share 1/3 < 40%.
    doc = _make_dfa("d1", {
        "code_defect": 1,
        "scope_related": 1,
        "review_quality": 1,
        "total": 3,
        "last_updated": "2026-01-01",
    })
    assert _get_failure_coaching_suffix("d1", doc) is None


def test_failure_coaching_dominant_category_returned():
    # 4 code_defect out of 5 total = 80% — well above 40%.
    doc = _make_dfa("d1", {
        "code_defect": 4,
        "scope_related": 1,
        "total": 5,
        "last_updated": "2026-01-01",
    })
    result = _get_failure_coaching_suffix("d1", doc)
    assert result is not None
    assert "code_defect" in result
    assert "4/5" in result


def test_failure_coaching_exactly_at_threshold():
    # 2 out of 5 = 40% — exactly at threshold, should return coaching.
    doc = _make_dfa("d1", {
        "scope_related": 2,
        "code_defect": 1,
        "review_quality": 1,
        "other": 1,
        "total": 5,
        "last_updated": "2026-01-01",
    })
    result = _get_failure_coaching_suffix("d1", doc)
    assert result is not None
    assert "scope_related" in result


def test_failure_coaching_excludes_total_and_last_updated_from_counts():
    # If "total" and "last_updated" were not excluded they would skew the computation.
    doc = _make_dfa("d1", {
        "code_defect": 4,
        "total": 10,       # should be ignored
        "last_updated": "2026-01-01",  # should be ignored (non-numeric anyway)
    })
    result = _get_failure_coaching_suffix("d1", doc)
    # Only code_defect=4, total from cat_counts=4 >= 3, share=100% >= 40%
    assert result is not None
    assert "code_defect" in result
    assert "4/4" in result


# --- _feedback_scope_override and _calibrate_scope ---

def _make_project(design_doc: dict):
    """Minimal project-like object for scope-override tests (no DB needed)."""
    class _P:
        directions = []

    p = _P()
    p.design_doc = design_doc
    return p


def test_feedback_scope_override_downscopes_at_threshold():
    """Exactly 3 scope_related first-attempt rejections → downscope medium→small."""
    proj = _make_project({
        "direction_first_attempt_feedback": {
            "dir1": {"scope_related": 3, "total": 3, "last_updated": "2026-01-01"}
        }
    })
    scope, calibrated_from = _feedback_scope_override("medium", 1, {"id": "dir1"}, proj, None)
    assert scope == "small"
    assert calibrated_from == "medium"


def test_feedback_scope_override_no_downscope_below_threshold():
    """Only 2 scope_related rejections (below 3) → no override."""
    proj = _make_project({
        "direction_first_attempt_feedback": {
            "dir1": {"scope_related": 2, "total": 2, "last_updated": "2026-01-01"}
        }
    })
    assert _feedback_scope_override("medium", 1, {"id": "dir1"}, proj, None) is None


def test_feedback_scope_override_no_downscope_at_smallest_scope():
    """idx=0 (already at 'small') → downscope guard is skipped regardless of counts."""
    proj = _make_project({
        "direction_first_attempt_feedback": {
            "dir1": {"scope_related": 5, "total": 5, "last_updated": "2026-01-01"}
        }
    })
    # idx=0 means there is no smaller scope; the guard `if idx > 0` must prevent downscoping.
    assert _feedback_scope_override("small", 0, {"id": "dir1"}, proj, None) is None


def test_feedback_scope_override_retains_scope_on_strong_success_pattern():
    """success_count >= 5 for task_type/scope → retain proposed scope."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 5}})
    scope, calibrated_from = _feedback_scope_override("small", 0, {"id": "d2"}, proj, "create")
    assert scope == "small"
    assert calibrated_from is None


def test_feedback_scope_override_no_retain_weak_success_pattern():
    """success_count < 5 → success-pattern guard does not fire; returns None."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 4}})
    assert _feedback_scope_override("small", 0, {"id": "d2"}, proj, "create") is None


def test_feedback_scope_override_no_retain_missing_task_type():
    """task_type=None → success-pattern check is skipped entirely."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 10}})
    # Without task_type the playbook key cannot be formed, so no retention override.
    assert _feedback_scope_override("small", 0, {"id": "d2"}, proj, None) is None


def test_calibrate_scope_downscopes_via_failure_feedback_coldstart():
    """_calibrate_scope propagates failure-feedback downscope when history is sparse."""
    proj = _make_project({
        "direction_first_attempt_feedback": {
            "dir3": {"scope_related": 4, "total": 4, "last_updated": "2026-01-01"}
        }
    })
    direction = {"id": "dir3", "title": "T", "research_history": []}
    scope, calibrated_from = _calibrate_scope("medium", direction, proj, "create")
    assert scope == "small"
    assert calibrated_from == "medium"


def test_calibrate_scope_success_pattern_retained_coldstart():
    """_calibrate_scope returns the success-pattern scope intact when evidence is strong."""
    proj = _make_project({"playbook/success-create-small": {"success_count": 7}})
    direction = {"id": "dir4", "title": "T", "research_history": []}
    # With no failure feedback and strong success-pattern evidence, scope is retained.
    scope, calibrated_from = _calibrate_scope("small", direction, proj, "create")
    assert scope == "small"
    assert calibrated_from is None


def test_feedback_scope_override_retains_scope_on_direction_affinity():
    """2+ direction-local successes retain scope even when global success_count < 5."""
    proj = _make_project({
        "playbook/success-create-small": {
            "success_count": 3,  # below global threshold of 5
            "direction_successes": {"dir-local": 2},
        }
    })
    scope, calibrated_from = _feedback_scope_override(
        "small", 0, {"id": "dir-local"}, proj, "create"
    )
    assert scope == "small"
    assert calibrated_from is None


def test_feedback_scope_override_no_retain_on_single_direction_success():
    """Only 1 direction-local success (below threshold of 2) does not retain scope."""
    proj = _make_project({
        "playbook/success-create-small": {
            "success_count": 3,
            "direction_successes": {"dir-local": 1},
        }
    })
    assert _feedback_scope_override("small", 0, {"id": "dir-local"}, proj, "create") is None


def test_feedback_scope_override_ignores_malformed_direction_successes():
    """Malformed persisted direction_successes does not crash and is treated as empty."""
    proj = _make_project({
        "playbook/success-create-small": {
            "success_count": 3,
            "direction_successes": "bad",
        }
    })
    assert _feedback_scope_override("small", 0, {"id": "dir-local"}, proj, "create") is None


# --- _check_pre_spawn_budget (pre-spawn scope/budget gate) ---
# These tests target the function added in sibling cluster 1/2 (spawn_calibration.py).
# Local imports inside each test body follow the tdd-cluster-test-pattern playbook note
# so ImportError before the sibling lands yields XFAIL rather than a module-load error.


@pytest.mark.xfail(strict=False, reason="sibling cluster 1/2 (spawn_calibration.py) not yet merged")
def test_pre_spawn_budget_small_5_files_upgrades_to_medium():
    """small scope with 5 related files exceeds small-scope limit → auto-upgrade to medium."""
    from app.spawn_calibration import _check_pre_spawn_budget  # noqa: PLC0415
    files = [f"mod/file_{i}.py" for i in range(5)]
    final_scope, warning, requires_approval = _check_pre_spawn_budget("small", files)
    assert final_scope == "medium"
    assert warning is not None
    assert requires_approval is False


@pytest.mark.xfail(strict=False, reason="sibling cluster 1/2 (spawn_calibration.py) not yet merged")
def test_pre_spawn_budget_medium_12_files_upgrades_to_large():
    """medium scope with 12 related files exceeds MEDIUM_ANALYSIS_PLAN_FILE_LIMIT → auto-upgrade to large."""
    from app.spawn_calibration import _check_pre_spawn_budget  # noqa: PLC0415
    files = [f"mod/file_{i}.py" for i in range(12)]
    final_scope, warning, requires_approval = _check_pre_spawn_budget("medium", files)
    assert final_scope == "large"
    assert warning is not None
    assert requires_approval is False


@pytest.mark.xfail(strict=False, reason="sibling cluster 1/2 (spawn_calibration.py) not yet merged")
def test_pre_spawn_budget_large_30_files_requires_approval():
    """large scope with 30 related files exceeds large-scope limit → requires_approval=True."""
    from app.spawn_calibration import _check_pre_spawn_budget  # noqa: PLC0415
    files = [f"mod/file_{i}.py" for i in range(30)]
    final_scope, warning, requires_approval = _check_pre_spawn_budget("large", files)
    assert final_scope == "large"  # no higher scope tier
    assert requires_approval is True


@pytest.mark.xfail(strict=False, reason="sibling cluster 1/2 (spawn_calibration.py) not yet merged")
def test_pre_spawn_budget_within_limits_passes_through_unchanged():
    """Findings within their scope's file-count limit pass through with no upgrade or warning."""
    from app.spawn_calibration import _check_pre_spawn_budget  # noqa: PLC0415
    # small with 2 files: within limit (2 is not > 2)
    scope, warning, requires_approval = _check_pre_spawn_budget("small", ["a.py", "b.py"])
    assert scope == "small"
    assert warning is None
    assert requires_approval is False
    # medium with 6 files: within limit (6 ≤ MEDIUM_ANALYSIS_PLAN_FILE_LIMIT=10)
    scope, warning, requires_approval = _check_pre_spawn_budget("medium", [f"f{i}.py" for i in range(6)])
    assert scope == "medium"
    assert warning is None
    assert requires_approval is False
