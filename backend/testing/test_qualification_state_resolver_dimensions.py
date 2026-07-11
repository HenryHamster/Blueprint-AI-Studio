"""Tests for resolve_dimension_states in qualification_state_resolver."""

import pytest

from app.qualification_state_resolver import resolve_dimension_states

_ALL_KEYS = {
    "audit", "test_erosion", "smoketest", "ci",
    "migration_identity", "migration_apply", "merge_grade",
    "challenge", "acceptance_window",
    "criterion_coverage", "debt_pressure", "goal_alignment",
    "capability_proof",
}


def _by_key(states):
    return {s["key"]: s for s in states}


def test_none_last_qualification_returns_all_pending():
    result = resolve_dimension_states(None)
    assert len(result) == 13
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


def test_returns_all_13_dimensions():
    result = resolve_dimension_states(None)
    assert len(result) == 13
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

    # Add ledger rows for all 10 overlapping dimensions.  Use a variety of
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
        ("challenge", "passed", None, None),
        ("acceptance_window", "passed", 0.8, None),
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
    assert len(result) == 13
    d = {s["key"]: s for s in result}

    # 10 ledger-backed dimensions use ledger values (with status mapping).
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
    assert d["challenge"]["status"] == "passed"
    assert d["acceptance_window"]["status"] == "passed"
    assert d["acceptance_window"]["score"] == pytest.approx(0.8)

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
    assert len(result) == 13
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
    assert len(result) == 13
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
    assert len(result) == 13
    d = {s["key"]: s for s in result}
    assert d["ci"]["status"] == "pending"


# ---------------------------------------------------------------------------
# proof_gate dimension — informational deployment-health signal
# ---------------------------------------------------------------------------


def test_proof_gate_dimension_enforce_mode_passes():
    """The proof_gate dimension is 'passed' when capability_proof_gate_mode is
    'enforce', and stays purely informational (never a blocker)."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "pass",
        "criterion_coverage": {"total": 3, "covered": 3},
        "smoketest_enabled": True,
        "capability_proof_gate_mode": "enforce",
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "passed"
    assert d["capability_proof"]["score"] is None
    assert d["capability_proof"]["reason"] is None


def test_proof_gate_unavailable_when_off():
    """The proof_gate dimension is 'unavailable' when mode is 'off'."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "pass",
        "criterion_coverage": {"total": 3, "covered": 3},
        "smoketest_enabled": True,
        "capability_proof_gate_mode": "off",
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "unavailable"
    assert d["capability_proof"]["reason"] == "off"


def test_proof_gate_unavailable_when_observe():
    """The proof_gate dimension is 'unavailable' when mode is 'observe'."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "pass",
        "criterion_coverage": {"total": 3, "covered": 3},
        "smoketest_enabled": True,
        "capability_proof_gate_mode": "observe",
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "unavailable"
    assert d["capability_proof"]["reason"] == "observe"


def test_proof_gate_unavailable_when_mode_missing():
    """The proof_gate dimension is 'unavailable' when capability_proof_gate_mode
    is not present in the qualification dict (defaults to 'off')."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "merge_grade_status": "pass",
        "criterion_coverage": {"total": 3, "covered": 3},
        "smoketest_enabled": True,
        # capability_proof_gate_mode intentionally absent
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "unavailable"
    assert d["capability_proof"]["reason"] == "off"


@pytest.mark.asyncio
async def test_proof_gate_ledger_backed_path_works(db_session):
    """When a QualificationDimensionResult row exists for proof_gate, the
    ledger-backed path derives the dimension state from the ledger."""
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
            "sub_scores": {"audit": 0.9},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "criterion_coverage": {"total": 3, "covered": 3},
            "blocker": None,
            "smoketest_enabled": True,
            "capability_proof_gate_mode": "enforce",
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Ledger row says proof_gate is "passed" (enforce mode).
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc123",
        dimension="capability_proof",
        status="passed",
        sub_score=None,
        blocker=None,
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    assert len(result) == 13
    d = {s["key"]: s for s in result}
    assert d["capability_proof"]["status"] == "passed"


@pytest.mark.asyncio
async def test_proof_gate_ledger_unavailable_when_not_enforce(db_session):
    """When the ledger row for proof_gate says 'unavailable', the UI shows it."""
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
            "sub_scores": {"audit": 0.9},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "criterion_coverage": {"total": 3, "covered": 3},
            "blocker": None,
            "smoketest_enabled": True,
            "capability_proof_gate_mode": "off",
        },
    )
    db_session.add(version)
    await db_session.flush()

    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc123",
        dimension="capability_proof",
        status="unavailable",
        sub_score=None,
        blocker=None,
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    d = {s["key"]: s for s in result}
    assert d["capability_proof"]["status"] == "unavailable"


# ---------------------------------------------------------------------------
# challenge + acceptance_window — blob-derived and ledger-backed
# ---------------------------------------------------------------------------


def test_challenge_blob_passed_when_verdicts_present():
    """challenge dimension is 'passed' when challenge_verdicts is a non-empty dict."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        "challenge_verdicts": {"verdict": "pass", "some_key": True},
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["challenge"]["status"] == "passed"
    assert d["challenge"]["score"] is None
    assert d["challenge"]["reason"] is None


def test_challenge_blob_unavailable_when_no_verdicts():
    """challenge dimension is 'unavailable' when challenge_verdicts is absent."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        # challenge_verdicts intentionally absent
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["challenge"]["status"] == "unavailable"


def test_acceptance_window_blob_passed_with_fraction():
    """acceptance_window dimension is 'passed' with score when sub_scores contains acceptance_window_fraction."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {
            "audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8,
            "acceptance_window_fraction": 0.75,
        },
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["acceptance_window"]["status"] == "passed"
    assert d["acceptance_window"]["score"] == pytest.approx(0.75)


def test_acceptance_window_blob_unavailable_when_absent():
    """acceptance_window dimension is 'unavailable' when acceptance_window_fraction is absent."""
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        # acceptance_window_fraction intentionally absent from sub_scores
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["acceptance_window"]["status"] == "unavailable"
    assert d["acceptance_window"]["score"] is None


@pytest.mark.asyncio
async def test_challenge_and_acceptance_window_surface_from_ledger(db_session):
    """challenge and acceptance_window ledger rows are read back by
    resolve_dimension_states_from_ledger and override the blob-derived state."""
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    # Blob has no challenge_verdicts (→ unavailable) and no acceptance_window_fraction
    # (→ unavailable) — the ledger rows below must override both.
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "sha-ledger-test",
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "criterion_coverage": {"total": 2, "covered": 2},
            "blocker": None,
            "smoketest_enabled": True,
            # challenge_verdicts absent → blob would produce "unavailable"
            # sub_scores.acceptance_window_fraction absent → blob would produce "unavailable"
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Ledger says both challenge and acceptance_window are "passed", which
    # contradicts the blob-derived "unavailable" — proves the ledger wins.
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-ledger-test",
        dimension="challenge",
        status="passed",
        sub_score=None,
        blocker=None,
    ))
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-ledger-test",
        dimension="acceptance_window",
        status="passed",
        sub_score=0.88,
        blocker=None,
    ))
    # Also add a CI row so the query returns rows for this candidate_sha,
    # triggering the ledger overlay path (not the fall-back-to-blob path).
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-ledger-test",
        dimension="ci",
        status="passed",
        sub_score=None,
        blocker=None,
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    assert len(result) == 13
    d = {s["key"]: s for s in result}

    # Blob would have returned "unavailable" for both; ledger overrides to "passed".
    assert d["challenge"]["status"] == "passed", (
        "challenge ledger row should override blob-derived unavailable"
    )
    assert d["acceptance_window"]["status"] == "passed", (
        "acceptance_window ledger row should override blob-derived unavailable"
    )
    assert d["acceptance_window"]["score"] == pytest.approx(0.88)

    # Blob-only dimensions are unaffected.
    assert d["criterion_coverage"]["status"] == "passed"
    assert d["goal_alignment"]["status"] == "unavailable"


# ---------------------------------------------------------------------------
# patch_qualification_state_from_dimension_states — read-surface consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_surface_prefers_ledger_over_stale_blob(db_session):
    """When the ledger has resolved dimension facts for the current candidate_sha
    and the last_qualification blob carries stale sub_scores (e.g. after an
    infra-failure overwrite resets the blob while the ledger preserves resolved
    results), patch_qualification_state_from_dimension_states ensures that the
    qualification_state sub_scores match the ledger-backed dimension_states scores.

    This exercises the Phase 1b read-cutover seam: one read returns one story.
    """
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import (
        resolve_dimension_states_from_ledger,
        patch_qualification_state_from_dimension_states,
    )

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    # Blob simulates an infra-failure overwrite: sub_scores are stale/reset
    # (audit=0.0) while the ledger preserved the successful pass at 0.95.
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "infra_failure",
            "candidate_sha": "sha-stale-blob",
            "sub_scores": {"audit": 0.0, "test_erosion": 0.0},
            "error": "vm_timeout",
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Ledger preserved the resolved facts from before the infra-failure.
    for dim, status, sub_score in [
        ("audit", "passed", 0.95),
        ("test_erosion", "passed", 0.87),
        ("ci", "passed", None),
    ]:
        db_session.add(QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="sha-stale-blob",
            dimension=dim,
            status=status,
            sub_score=sub_score,
            blocker=None,
        ))
    await db_session.flush()

    # Stale blob-derived qualification_state (as returned by summarize_qualification_state).
    stale_qual_state = {
        "state": "infra_failure",
        "summary": "transient infra failure",
        "sub_scores": {"audit": 0.0, "test_erosion": 0.0},
    }

    # Read the ledger-backed dimension_states.
    dimension_states = await resolve_dimension_states_from_ledger(db_session, version)
    d = {s["key"]: s for s in dimension_states}

    # Ledger correctly overrides the stale blob for audit and test_erosion.
    assert d["audit"]["status"] == "passed"
    assert d["audit"]["score"] == pytest.approx(0.95)
    assert d["test_erosion"]["status"] == "passed"
    assert d["test_erosion"]["score"] == pytest.approx(0.87)

    # Patch the stale qualification_state with the ledger-backed scores.
    patched = patch_qualification_state_from_dimension_states(stale_qual_state, dimension_states)

    # After patching, sub_scores reflect the ledger — not the stale blob.
    assert patched is not None
    assert patched["sub_scores"]["audit"] == pytest.approx(0.95), (
        "qualification_state.sub_scores should prefer ledger over stale blob"
    )
    assert patched["sub_scores"]["test_erosion"] == pytest.approx(0.87), (
        "qualification_state.sub_scores should prefer ledger over stale blob"
    )

    # Original is not mutated.
    assert stale_qual_state["sub_scores"]["audit"] == pytest.approx(0.0), (
        "patch must not mutate the original qualification_state"
    )

    # Guard: None input returns None.
    assert patch_qualification_state_from_dimension_states(None, dimension_states) is None

    # Guard: dimensions with no score do not zero out existing sub_scores keys.
    patched_no_score = patch_qualification_state_from_dimension_states(
        {"sub_scores": {"audit": 0.9}},
        [{"key": "audit", "label": "Audit", "status": "passed", "score": None, "reason": None}],
    )
    assert patched_no_score is not None
    assert patched_no_score["sub_scores"]["audit"] == pytest.approx(0.9), (
        "None score must not overwrite existing sub_scores value"
    )
