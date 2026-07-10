"""Tests for resolve_dimension_states in qualification_state_resolver."""

import pytest

from app.qualification_state_resolver import resolve_dimension_states

_ALL_KEYS = {
    "audit", "test_erosion", "smoketest", "ci",
    "migration_identity", "migration_apply", "merge_grade",
    "criterion_coverage", "debt_pressure", "goal_alignment",
}


def _by_key(states):
    return {s["key"]: s for s in states}


def test_none_last_qualification_returns_all_pending():
    result = resolve_dimension_states(None)
    assert len(result) == 10
    d = _by_key(result)
    for key in _ALL_KEYS:
        assert d[key]["status"] == "pending", key


def test_empty_dict_returns_all_pending():
    result = resolve_dimension_states({})
    d = _by_key(result)
    for key in _ALL_KEYS:
        assert d[key]["status"] == "pending", key


def test_infra_failure_all_unavailable():
    qual = {"status": "infra_failure", "error": "VM timeout"}
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    for key in _ALL_KEYS:
        assert d[key]["status"] == "unavailable", key
        assert d[key]["reason"] == "VM timeout", key


def test_infra_failure_no_error_uses_sentinel():
    qual = {"status": "infra_failure"}
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["audit"]["status"] == "unavailable"
    assert d["audit"]["reason"] == "infra_failure"


def test_pending_no_promotion_pending_all_pending():
    qual = {"status": "pending", "candidate_sha": "abc123"}
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    for key in _ALL_KEYS:
        assert d[key]["status"] == "pending", key


def test_qualifying_all_pending():
    qual = {"status": "qualifying", "candidate_sha": "abc123"}
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    for key in _ALL_KEYS:
        assert d[key]["status"] == "pending", key


def test_pending_with_promotion_pending_not_all_pending():
    """When promotion_pending is set, derive per-dimension state instead."""
    qual = {
        "status": "pending",
        "promotion_pending": {"pr_number": 42, "enabled_at": "2026-01-01T00:00:00"},
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.95},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "pass",
        "merge_grade_coverage": 0.8,
        "criterion_coverage": {"total": 5, "covered": 5},
        "blocker": None,
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["audit"]["status"] == "passed"
    assert d["audit"]["score"] == pytest.approx(0.9)
    assert d["ci"]["status"] == "passed"
    assert d["migration_identity"]["status"] == "passed"


def test_finalized_smoketest_blocker():
    """blocker=smoketest_startup_failed → smoketest failed, others derived."""
    qual = {
        "status": "not_promoted",
        "blocker": "smoketest_startup_failed",
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.0},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "pass",
        "merge_grade_coverage": 0.7,
        "criterion_coverage": {"total": 3, "covered": 3},
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["smoketest"]["status"] == "failed"
    assert d["smoketest"]["reason"] == "smoketest_startup_failed"
    assert d["audit"]["status"] == "passed"
    assert d["audit"]["score"] == pytest.approx(0.9)
    assert d["test_erosion"]["status"] == "passed"
    assert d["test_erosion"]["score"] == pytest.approx(0.85)


def test_finalized_audit_passed_with_sub_score():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.95, "test_erosion": 0.90, "smoketest": 0.85},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "pass",
        "merge_grade_coverage": 0.9,
        "criterion_coverage": {"total": 4, "covered": 4},
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["audit"]["status"] == "passed"
    assert d["audit"]["score"] == pytest.approx(0.95)
    assert d["test_erosion"]["status"] == "passed"
    assert d["smoketest"]["status"] == "passed"


def test_audit_missing_blocker_also_marks_test_erosion_unavailable():
    """When blocker=audit_missing, both audit and test_erosion should be unavailable."""
    qual = {
        "status": "not_promoted",
        "blocker": "audit_missing",
        "sub_scores": {"audit": 0.0, "test_erosion": 0.0, "smoketest": 0.85},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["audit"]["status"] == "failed"
    assert d["audit"]["reason"] == "audit_missing"
    assert d["test_erosion"]["status"] == "unavailable"
    assert d["test_erosion"]["reason"] == "audit_missing"


def test_ci_not_green_blocker():
    qual = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "red"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["ci"]["status"] == "failed"
    assert d["ci"]["reason"] == "ci_not_green"
    assert d["audit"]["status"] == "passed"


def test_migration_identity_pass():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["migration_identity"]["status"] == "passed"
    assert d["migration_apply"]["status"] == "passed"


def test_migration_apply_none_is_not_applicable():
    """When migration_apply_safety is absent, it should be not_applicable."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        # migration_apply_safety intentionally absent
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["migration_apply"]["status"] == "not_applicable"


def test_smoketest_disabled_not_applicable():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.0},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": False,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["smoketest"]["status"] == "not_applicable"


def test_goal_alignment_unavailable_when_absent():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        # goal_alignment intentionally absent
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["goal_alignment"]["status"] == "unavailable"


def test_goal_alignment_passed_when_present():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        "goal_alignment": {"status": "pass", "grade": "A"},
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["goal_alignment"]["status"] == "passed"


def test_criterion_coverage_incomplete():
    qual = {
        "status": "not_promoted",
        "blocker": "criterion_coverage_incomplete",
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "criterion_coverage": {"total": 5, "covered": 3},
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["criterion_coverage"]["status"] == "failed"
    assert d["criterion_coverage"]["reason"] == "criterion_coverage_incomplete"


def test_criterion_coverage_zero_total_not_applicable():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "criterion_coverage": {"total": 0, "covered": 0},
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["criterion_coverage"]["status"] == "not_applicable"


def test_debt_pressure_when_present():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {
            "audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8, "debt_pressure": 0.7
        },
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["debt_pressure"]["status"] == "passed"
    assert d["debt_pressure"]["score"] == pytest.approx(0.7)


def test_returns_all_10_dimensions():
    result = resolve_dimension_states(None)
    assert len(result) == 10
    keys = {s["key"] for s in result}
    assert keys == _ALL_KEYS


def test_merge_grade_not_applicable():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "not_applicable",
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["merge_grade"]["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# resolve_dimension_states_from_ledger — ledger-backed dimension state reader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_dimension_state_reads_ledger_when_populated(db_session):
    """When ledger rows exist for the version's candidate SHA, the operator-facing
    dimension states are derived from the ledger for the 8 overlapping dimensions.
    The 2 blob-only dimensions (criterion_coverage, goal_alignment) stay derived
    from last_qualification."""
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "abc123",
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "merge_grade_coverage": 0.85,
            "criterion_coverage": {"total": 3, "covered": 3},
            "debt_pressure": 0.6,
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Add ledger rows for all 8 overlapping dimensions.  Use a variety of
    # statuses and scores that DIFFER from the blob to prove the ledger is read.
    ledger_dims = [
        ("ci", "blocked", None, "ci_not_green"),
        ("audit", "passed", 0.95, None),
        ("test_erosion", "passed", 0.82, None),
        ("smoketest", "unavailable", None, None),
        ("migration_identity", "passed", None, None),
        ("migration_apply", "passed", None, None),
        ("merge_grade", "failed", 0.5, "merge_grade_misses"),
        ("debt_pressure", "passed", 0.7, None),
    ]
    for dim, status, sub_score, blocker in ledger_dims:
        db_session.add(QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="abc123",
            dimension=dim,
            status=status,
            sub_score=sub_score,
            blocker=blocker,
        ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    assert len(result) == 10
    d = {s["key"]: s for s in result}

    # 8 ledger-backed dimensions use ledger values (with status mapping).
    assert d["ci"]["status"] == "failed", "blocked → failed mapping"
    assert d["ci"]["reason"] == "ci_not_green"
    assert d["audit"]["status"] == "passed"
    assert d["audit"]["score"] == pytest.approx(0.95)
    assert d["test_erosion"]["status"] == "passed"
    assert d["test_erosion"]["score"] == pytest.approx(0.82)
    assert d["smoketest"]["status"] == "unavailable"
    assert d["migration_identity"]["status"] == "passed"
    assert d["migration_apply"]["status"] == "passed"
    assert d["merge_grade"]["status"] == "failed"
    assert d["merge_grade"]["reason"] == "merge_grade_misses"
    assert d["merge_grade"]["score"] == pytest.approx(0.5)
    assert d["debt_pressure"]["status"] == "passed"
    assert d["debt_pressure"]["score"] == pytest.approx(0.7)

    # 2 blob-only dimensions preserved from last_qualification.
    assert d["criterion_coverage"]["status"] == "passed"
    assert d["goal_alignment"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_ledger_empty_falls_back_to_blob(db_session):
    """When no QualificationDimensionResult rows exist for the version's
    candidate_sha, resolve_dimension_states_from_ledger falls back to
    the synchronous resolve_dimension_states(last_qualification)."""
    from app.models import Project, ReleaseVersion
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "abc123",
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "merge_grade_coverage": 0.85,
            "criterion_coverage": {"total": 4, "covered": 4},
            "goal_alignment": {"status": "pass"},
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Deliberately NO ledger rows.

    result = await resolve_dimension_states_from_ledger(db_session, version)
    assert len(result) == 10
    d = {s["key"]: s for s in result}

    # All derived from blob — same as calling resolve_dimension_states directly.
    assert d["ci"]["status"] == "passed"
    assert d["audit"]["status"] == "passed"
    assert d["audit"]["score"] == pytest.approx(0.9)
    assert d["smoketest"]["status"] == "passed"
    assert d["migration_identity"]["status"] == "passed"
    assert d["migration_apply"]["status"] == "passed"
    assert d["merge_grade"]["status"] == "passed"
    assert d["criterion_coverage"]["status"] == "passed"
    assert d["goal_alignment"]["status"] == "passed"


@pytest.mark.asyncio
async def test_ledger_different_candidate_sha_falls_back(db_session):
    """When ledger rows exist but for a DIFFERENT candidate_sha than the current
    last_qualification, resolve_dimension_states_from_ledger falls back to the blob."""
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "current-sha",
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Ledger rows exist but for a DIFFERENT candidate SHA.
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="old-sha",
        dimension="ci",
        status="blocked",
        blocker="ci_not_green",
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    assert len(result) == 10
    d = {s["key"]: s for s in result}

    # CI is "passed" from blob (ci_resolution.state=green), NOT "failed" from ledger.
    assert d["ci"]["status"] == "passed"


@pytest.mark.asyncio
async def test_ledger_no_candidate_sha_falls_back(db_session):
    """When last_qualification has no candidate_sha, the ledger is skipped."""
    from app.models import Project, ReleaseVersion
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={"status": "pending"},
    )
    db_session.add(version)
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    assert len(result) == 10
    d = {s["key"]: s for s in result}
    assert d["ci"]["status"] == "pending"
