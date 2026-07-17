"""Tests for resolve_dimension_states in qualification_state_resolver."""

import pytest

from app.qualification_state_resolver import resolve_dimension_states

_ALL_KEYS = {
    "audit", "test_erosion", "smoketest", "ci",
    "migration_identity", "migration_apply", "merge_grade",
    "challenge", "acceptance_window",
    "criterion_coverage", "debt_pressure", "goal_alignment",
    "capability_proof", "env_preflight",
}


def _by_key(states):
    return {s["key"]: s for s in states}


def test_none_last_qualification_returns_all_pending():
    result = resolve_dimension_states(None)
    assert len(result) == 14
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


def test_env_preflight_dark_by_default_is_not_applicable():
    # Slice 3 ships DARK: a finalized record with no env_preflight_enabled key
    # (the default, and every legacy record) renders the dimension not_applicable.
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
    assert d["env_preflight"]["status"] == "not_applicable"


def test_env_preflight_enabled_all_present_passed():
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {
            "audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8,
            "env_preflight": 1.0,
        },
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        "env_preflight_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["env_preflight"]["status"] == "passed"
    assert d["env_preflight"]["score"] == pytest.approx(1.0)


def test_env_preflight_boot_prereq_missing_blocker_is_failed():
    # The boot_prereq_missing blocker maps to the env_preflight dimension.
    qual = {
        "status": "not_promoted",
        "blocker": "boot_prereq_missing",
        "sub_scores": {
            "audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8,
            "env_preflight": 0.0,
            "env_preflight_missing": ["GITHUB_PAT_ENCRYPTION_KEY"],
        },
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        "env_preflight_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["env_preflight"]["status"] == "failed"
    assert d["env_preflight"]["reason"] == "boot_prereq_missing"


def test_env_preflight_enabled_no_manifest_is_unavailable():
    # Enabled but no env_preflight sub_score (candidate declared no manifest) →
    # unavailable, never a spurious block.
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        "env_preflight_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["env_preflight"]["status"] == "unavailable"


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


def _goal_alignment_qual(goal_alignment):
    """A clean, fully-passing qualification blob with ``goal_alignment`` injected.

    Nothing else in the blob can fail, so the goal_alignment dimension's status is
    attributable solely to the injected value.
    """
    return {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "smoketest_enabled": True,
        "goal_alignment": goal_alignment,
    }


def test_goal_alignment_unrecognized_dict_status_is_not_passed():
    """Finding #7: an unrecognized dict status must NOT fail open to 'passed'.

    v25's principle is "earned, not claimed" — a dimension may only read passed
    from a verdict it actually recognizes. This used to default to 'passed',
    claiming an alignment pass the audit never gave.
    """
    result = resolve_dimension_states(
        _goal_alignment_qual({"status": "who_knows", "grade": "?"})
    )
    d = _by_key(result)
    assert d["goal_alignment"]["status"] != "passed", (
        "an unrecognized goal_alignment verdict must never display as passed"
    )
    assert d["goal_alignment"]["status"] == "unavailable"
    assert d["goal_alignment"]["reason"] == "goal_alignment_unrecognized:who_knows"


def test_goal_alignment_empty_dict_is_not_passed():
    """A goal_alignment dict carrying no verdict key at all is unknown, not passed."""
    result = resolve_dimension_states(_goal_alignment_qual({"biggest_gap": "auth"}))
    d = _by_key(result)
    assert d["goal_alignment"]["status"] == "unavailable"
    assert d["goal_alignment"]["reason"] == "goal_alignment_unrecognized"


def test_goal_alignment_non_dict_truthy_is_not_passed():
    """Finding #7: a non-dict truthy value (a bare string / number the audit never
    promised) carries no readable verdict, so it must not claim a pass."""
    for junk in ("aligned-ish", 1, ["aligned"]):
        result = resolve_dimension_states(_goal_alignment_qual(junk))
        d = _by_key(result)
        assert d["goal_alignment"]["status"] != "passed", (
            f"non-dict goal_alignment {junk!r} must not fail open to passed"
        )
        assert d["goal_alignment"]["status"] == "unavailable"
        assert d["goal_alignment"]["reason"] == "goal_alignment_unrecognized"


def test_goal_alignment_real_audit_shape_shortfall_is_failed():
    """The REAL payload the release-audit agent emits is keyed on ``state``
    ({"state": "aligned"|"shortfall"|"not_applicable", "assessed_grade": ...}) —
    it carries neither ``status`` nor ``verdict``.

    Before the fix, every real verdict — a "shortfall" included — fell into the
    fail-open else branch and displayed as 'passed'. A shortfall must read failed.
    """
    result = resolve_dimension_states(
        _goal_alignment_qual(
            {
                "state": "shortfall",
                "assessed_grade": 0.4,
                "biggest_gap": "Auth not addressed",
            }
        )
    )
    d = _by_key(result)
    assert d["goal_alignment"]["status"] == "failed"
    assert d["goal_alignment"]["reason"] == "shortfall"


def test_goal_alignment_real_audit_shape_aligned_is_passed():
    """The earned pass: the audit's ``state="aligned"`` verdict reads passed."""
    result = resolve_dimension_states(
        _goal_alignment_qual({"state": "aligned", "assessed_grade": 0.95})
    )
    d = _by_key(result)
    assert d["goal_alignment"]["status"] == "passed"


def test_goal_alignment_real_audit_shape_not_applicable():
    """``state="not_applicable"`` (the goal cannot be assessed from this diff) is
    neither a pass nor a failure."""
    result = resolve_dimension_states(
        _goal_alignment_qual({"state": "not_applicable"})
    )
    d = _by_key(result)
    assert d["goal_alignment"]["status"] == "not_applicable"


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


def test_criterion_coverage_all_met_verdicts_passed_despite_low_counts():
    """Finding 9c04a643e3d336ec — align the resolver's coverage derivation with
    the redesigned category-verdict gate.

    The redesigned gate blocks on criterion_coverage_incomplete iff a category is
    not ``met`` (or a residual gap remains).  The legacy evidence-coverage COUNTS
    no longer feed that gate — attested/backfilled criteria are NOT counted as
    ``covered`` — so a version whose categories all grade ``met`` can still show
    covered < total.  Deriving the dimension from counts marked it ``failed``,
    contradicting the promotion the gate allowed.  With category_verdicts present
    and all ``met``, the dimension must resolve to ``passed``.
    """
    qual = {
        "status": "promoted",
        "blocker": None,
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "green"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        # Counts disagree (attested tiers not counted as covered) ...
        "criterion_coverage": {"total": 4, "covered": 2},
        # ... but every active category graded 'met' — the gate did NOT fire.
        "category_verdicts": [
            {"category_key": "c-1", "status": "met"},
            {"category_key": "c-2", "status": "met"},
        ],
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["criterion_coverage"]["status"] == "passed"
    assert d["criterion_coverage"]["score"] == 1.0
    assert d["criterion_coverage"]["reason"] is None


def test_criterion_coverage_not_met_verdict_failed_despite_full_counts():
    """The verdict-derived path also correctly FAILS: a not_met category resolves
    to ``failed`` even when the legacy counts show full coverage (covered==total).

    This exercises the case where a DIFFERENT blocker (e.g. ci) fired first, so the
    criterion_coverage dimension is derived from category_verdicts rather than the
    blocker map — the dimension must still reflect the true not_met category state.
    """
    qual = {
        "status": "not_promoted",
        "blocker": "ci_not_green",  # a different blocker won the ordered chain
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "red"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "criterion_coverage": {"total": 3, "covered": 3},
        "category_verdicts": [
            {"category_key": "c-1", "status": "met"},
            {"category_key": "c-2", "status": "not_met"},
        ],
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["criterion_coverage"]["status"] == "failed"
    assert d["criterion_coverage"]["reason"] == "criterion_coverage_incomplete"


def test_criterion_coverage_residual_gap_fails_despite_all_met():
    """A residual uncaptured gap fails the coverage dimension even when every
    category is ``met`` — mirroring the gate's residual_uncaptured_gap block."""
    qual = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
        "ci_resolution": {"state": "red"},
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
        "criterion_coverage": {"total": 1, "covered": 1},
        "category_verdicts": [{"category_key": "c-1", "status": "met"}],
        "residual_uncaptured_gap": "goal aspect no category captures",
        "smoketest_enabled": True,
    }
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["criterion_coverage"]["status"] == "failed"
    assert d["criterion_coverage"]["reason"] == "criterion_coverage_incomplete"


def test_criterion_coverage_no_verdicts_falls_back_to_counts():
    """When no category_verdicts are present (no rubric / ledger absent), the
    resolver falls back to the legacy evidence-coverage counts."""
    qual = {
        "status": "not_promoted",
        "blocker": "audit_missing",
        "sub_scores": {"audit": 0.0, "test_erosion": 0.0, "smoketest": 0.8},
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
    assert len(result) == 14
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
    assert len(result) == 14
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
    assert len(result) == 14
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
    assert len(result) == 14
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
    assert len(result) == 14
    d = {s["key"]: s for s in result}
    assert d["ci"]["status"] == "pending"


# ---------------------------------------------------------------------------
# proof_gate dimension — informational deployment-health signal
# ---------------------------------------------------------------------------


def _proof_gate_qual(**overrides):
    """A clean, fully-passing qualification blob with the capability-proof mode
    supplied via the REAL blob shape the release qualifier writes:
    ``capability_proof_detail={"mode": ...}``.  Callers override
    capability_proof_detail / blocker to exercise the proof_gate dimension."""
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
    }
    qual.update(overrides)
    return qual


def test_proof_gate_dimension_enforce_mode_passes():
    """The proof_gate dimension is 'passed' when the gate mode (read from the
    real capability_proof_detail.mode blob key) is 'enforce' and nothing blocks."""
    qual = _proof_gate_qual(capability_proof_detail={"mode": "enforce"})
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "passed"
    assert d["capability_proof"]["score"] is None
    assert d["capability_proof"]["reason"] is None


def test_proof_gate_dimension_enforce_mode_blocked_surfaces_failed():
    """In enforce mode, a real capability-proof gate block surfaces as a 'failed'
    dimension with the gate blocker reason — proving the blob-fallback resolver
    reads the mode from capability_proof_detail (not the phantom top-level key
    the release qualifier never writes) AND maps the gate blocker to the
    capability_proof dimension."""
    qual = _proof_gate_qual(
        status="not_promoted",
        blocker="capability_proof_gate_blocked",
        capability_proof_detail={"mode": "enforce", "unproven_count": 2},
    )
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "failed"
    assert d["capability_proof"]["reason"] == "capability_proof_gate_blocked"


def test_proof_gate_unavailable_when_off():
    """The proof_gate dimension is 'unavailable' when mode is 'off'."""
    qual = _proof_gate_qual(capability_proof_detail={"mode": "off"})
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "unavailable"
    assert d["capability_proof"]["reason"] == "off"


def test_proof_gate_passes_when_observe():
    """The proof_gate dimension is 'passed' (gate watching, not blocking) when
    mode is 'observe' — mirroring the ledger path's observe→passed semantics."""
    qual = _proof_gate_qual(capability_proof_detail={"mode": "observe"})
    result = resolve_dimension_states(qual)
    d = _by_key(result)
    assert d["capability_proof"]["status"] == "passed"
    assert d["capability_proof"]["reason"] is None


def test_proof_gate_unavailable_when_detail_missing():
    """The proof_gate dimension is 'unavailable' when capability_proof_detail is
    not present in the qualification dict (mode defaults to 'off')."""
    qual = _proof_gate_qual()  # no capability_proof_detail
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
    assert len(result) == 14
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
    assert len(result) == 14
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


# ---------------------------------------------------------------------------
# resolve_open_version_qualification_state — shared seam applies the ledger
# sub_score overlay so EVERY open-version surface tells one consistent story.
# ---------------------------------------------------------------------------


async def _make_project_and_version(db_session, last_qualification):
    from app.models import Project, ReleaseVersion

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification=last_qualification,
    )
    db_session.add(version)
    await db_session.flush()
    return project, version


# Neutral serving tuple that keeps the seam focused on the ledger overlay
# without needing loaded artifacts (version_serving_summary would otherwise run).
_NO_SERVING = ({"serving_status": None, "reason_code": None}, [])


@pytest.mark.asyncio
async def test_open_version_seam_applies_ledger_overlay(db_session):
    """resolve_open_version_qualification_state ITSELF overlays the ledger
    sub_scores onto the returned qualification_state, so an open-version surface
    that does NOT apply its own patch still tells the ledger-backed story.

    A blob-overwriting infra-failure re-qualification reset the audit sub_score
    to 0.0, while the QualificationDimensionResult ledger preserved the resolved
    pass at 0.95. The seam must surface the ledger score, not the stale blob."""
    from app.models import QualificationDimensionResult
    from app.qualification_state_resolver import (
        resolve_open_version_qualification_state,
    )

    project, version = await _make_project_and_version(
        db_session,
        {
            "status": "infra_failure",
            "candidate_sha": "sha-overwrite",
            # Stale/reset sub_scores left by the infra-failure blob overwrite.
            "sub_scores": {"audit": 0.0, "test_erosion": 0.0},
            "error": "vm_timeout",
        },
    )

    # Ledger preserved the resolved facts from before the infra-failure.
    for dim, status, sub_score in [
        ("audit", "passed", 0.95),
        ("test_erosion", "passed", 0.87),
        ("ci", "passed", None),
    ]:
        db_session.add(QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="sha-overwrite",
            dimension=dim,
            status=status,
            sub_score=sub_score,
            blocker=None,
        ))
    await db_session.flush()

    # audit_child / latest_verdict passed explicitly so the test isolates the
    # NEW ledger sub_score overlay from the other three enrichments.
    qual_state, _ = await resolve_open_version_qualification_state(
        db_session,
        version,
        project,
        serving=_NO_SERVING,
        audit_child=None,
        latest_verdict=None,
    )

    assert qual_state is not None
    assert qual_state["state"] == "infra_failure"
    # sub_scores reflect the LEDGER (0.95 / 0.87), NOT the stale blob (0.0 / 0.0).
    assert qual_state["sub_scores"]["audit"] == pytest.approx(0.95), (
        "seam must overlay the ledger audit score over the stale blob"
    )
    assert qual_state["sub_scores"]["test_erosion"] == pytest.approx(0.87), (
        "seam must overlay the ledger test_erosion score over the stale blob"
    )


@pytest.mark.asyncio
async def test_open_version_seam_no_ledger_matches_blob(db_session):
    """With NO ledger rows for the candidate, the seam's ledger overlay is a
    no-op: resolve_dimension_states_from_ledger falls back to the blob, so the
    returned sub_scores reflect the blob-derived scores (no spurious mutation)."""
    from app.qualification_state_resolver import (
        resolve_open_version_qualification_state,
    )

    project, version = await _make_project_and_version(
        db_session,
        {
            "status": "not_promoted",
            "candidate_sha": "sha-no-ledger",
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    # Deliberately NO ledger rows for this candidate_sha.

    qual_state, _ = await resolve_open_version_qualification_state(
        db_session,
        version,
        project,
        serving=_NO_SERVING,
        audit_child=None,
        latest_verdict=None,
    )

    assert qual_state is not None
    assert qual_state["state"] == "not_promoted"
    # Overlay derives from the blob (no ledger), so sub_scores mirror the blob.
    assert qual_state["sub_scores"]["audit"] == pytest.approx(0.9)
    assert qual_state["sub_scores"]["test_erosion"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_open_version_seam_none_state_untouched(db_session):
    """An unqualified open version (no verdict yet → summarize returns None) is
    returned as (None, ...); the ledger overlay does not fabricate a state."""
    from app.qualification_state_resolver import (
        resolve_open_version_qualification_state,
    )

    project, version = await _make_project_and_version(
        db_session,
        # No 'status' → summarize_qualification_state returns None (unqualified).
        {},
    )

    qual_state, _ = await resolve_open_version_qualification_state(
        db_session,
        version,
        project,
        serving=_NO_SERVING,
        audit_child=None,
        latest_verdict=None,
    )

    assert qual_state is None


@pytest.mark.asyncio
async def test_open_version_seam_threaded_dimension_states_equal_auto(db_session):
    """Threading a pre-resolved dimension_states (as the admin endpoint does to
    avoid a duplicate ledger query) yields the SAME overlaid qualification_state
    as letting the seam resolve the ledger itself."""
    from app.models import QualificationDimensionResult
    from app.qualification_state_resolver import (
        resolve_open_version_qualification_state,
        resolve_dimension_states_from_ledger,
    )

    project, version = await _make_project_and_version(
        db_session,
        {
            "status": "infra_failure",
            "candidate_sha": "sha-thread",
            "sub_scores": {"audit": 0.0},
            "error": "vm_timeout",
        },
    )
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-thread",
        dimension="audit",
        status="passed",
        sub_score=0.91,
        blocker=None,
    ))
    await db_session.flush()

    # (a) let the seam resolve the ledger itself
    auto_state, _ = await resolve_open_version_qualification_state(
        db_session,
        version,
        project,
        serving=_NO_SERVING,
        audit_child=None,
        latest_verdict=None,
    )

    # (b) thread a pre-resolved dimension_states in
    pre_resolved = await resolve_dimension_states_from_ledger(db_session, version)
    threaded_state, _ = await resolve_open_version_qualification_state(
        db_session,
        version,
        project,
        serving=_NO_SERVING,
        audit_child=None,
        latest_verdict=None,
        dimension_states=pre_resolved,
    )

    assert auto_state is not None and threaded_state is not None
    assert threaded_state["sub_scores"]["audit"] == pytest.approx(0.91)
    assert auto_state["sub_scores"] == threaded_state["sub_scores"], (
        "threading a pre-resolved dimension_states must match auto-resolution"
    )


# ---------------------------------------------------------------------------
# _strategic_recent_version_entry — the strategic recent-versions read surface
# must carry the same ledger sub_score overlay as the other open-version reads,
# instead of returning the stale last_qualification blob's sub_scores.
# ---------------------------------------------------------------------------


async def _reload_version_with_artifacts(db_session, version_id):
    """Re-fetch a version with deploy_artifacts eager-loaded, mirroring how the
    real strategic read path loads closed versions (include_artifacts=True), so
    version_serving_summary does not trip an async lazy-load."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import ReleaseVersion

    return (
        await db_session.execute(
            select(ReleaseVersion)
            .options(selectinload(ReleaseVersion.deploy_artifacts))
            .where(ReleaseVersion.id == version_id)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_strategic_recent_version_entry_applies_ledger_overlay(db_session):
    """_strategic_recent_version_entry routes qualification_state through the
    shared seam, so a stale/reset last_qualification blob is overlaid with the
    ledger-preserved per-dimension sub_scores it emits to the strategist."""
    from app.models import QualificationDimensionResult
    from app.strategic_spawning import _strategic_recent_version_entry

    project, version = await _make_project_and_version(
        db_session,
        {
            "status": "infra_failure",
            "candidate_sha": "sha-recent-overwrite",
            # Stale sub_scores left by a blob-overwriting infra failure.
            "sub_scores": {"audit": 0.0, "test_erosion": 0.0},
            "error": "vm_timeout",
        },
    )

    # Ledger preserved the resolved facts from before the infra failure.
    for dim, status, sub_score in [
        ("audit", "passed", 0.92),
        ("test_erosion", "passed", 0.81),
    ]:
        db_session.add(QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="sha-recent-overwrite",
            dimension=dim,
            status=status,
            sub_score=sub_score,
            blocker=None,
        ))
    await db_session.flush()

    version = await _reload_version_with_artifacts(db_session, version.id)
    entry = await _strategic_recent_version_entry(db_session, version, project)

    qual_state = entry["qualification_state"]
    assert qual_state is not None
    # sub_scores reflect the LEDGER (0.92 / 0.81), NOT the stale blob (0.0 / 0.0).
    assert qual_state["sub_scores"]["audit"] == pytest.approx(0.92), (
        "strategic recent-version entry must overlay the ledger audit score"
    )
    assert qual_state["sub_scores"]["test_erosion"] == pytest.approx(0.81), (
        "strategic recent-version entry must overlay the ledger test_erosion score"
    )


@pytest.mark.asyncio
async def test_strategic_recent_version_entry_no_ledger_matches_blob(db_session):
    """With NO ledger rows, the overlay is a no-op: the strategic entry's
    qualification_state sub_scores mirror the blob-derived scores unchanged."""
    from app.strategic_spawning import _strategic_recent_version_entry

    project, version = await _make_project_and_version(
        db_session,
        {
            "status": "not_promoted",
            "candidate_sha": "sha-recent-no-ledger",
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    # Deliberately NO ledger rows for this candidate_sha.

    version = await _reload_version_with_artifacts(db_session, version.id)
    entry = await _strategic_recent_version_entry(db_session, version, project)

    qual_state = entry["qualification_state"]
    assert qual_state is not None
    assert qual_state["sub_scores"]["audit"] == pytest.approx(0.9)
    assert qual_state["sub_scores"]["test_erosion"] == pytest.approx(0.85)

@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_mutable_dim_reused_currency_falls_back_to_blob_ci(db_session):
    """When a CI ledger row has currency='reused' (stale prior-attempt fact),
    resolve_dimension_states_from_ledger falls back to the blob-derived state
    for the ci dimension rather than surfacing the stale passed row.

    Scenario: Attempt-1 passed CI; the re-drive (Attempt-2) re-measured CI as
    blocking.  The ledger still holds the Attempt-1 row marked as
    reused_by=[Attempt-2].  The blob records the fresh Attempt-2 result:
    ci_resolution.state='red'.  The resolver must use the blob (blocked) for ci,
    not the stale ledger (passed).
    """
    import uuid
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    prior_attempt_id = uuid.uuid4()
    active_attempt_id = uuid.uuid4()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "sha-reused-ci",
            "qualification_attempt_id": str(active_attempt_id),
            # Fresh re-drive result: CI is now red.
            "ci_resolution": {"state": "red"},
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "blocker": "ci_not_green",
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Stale prior-attempt CI row: passed (from Attempt-1), now marked reused by Attempt-2.
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-reused-ci",
        dimension="ci",
        status="passed",
        sub_score=None,
        blocker=None,
        origin_attempt_id=prior_attempt_id,
        reused_by_attempt_ids=[str(active_attempt_id)],
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    d = {s["key"]: s for s in result}

    # The fresh blob state is ci_resolution.state='red' → ci is blocked/failed.
    assert d["ci"]["status"] == "failed", (
        "stale reused CI ledger row must not override fresh blob-derived failed state"
    )
    # Provenance is still recorded with currency=reused so callers know why.
    assert d["ci"]["provenance"]["currency"] == "reused"


@pytest.mark.asyncio
async def test_mutable_dim_current_currency_uses_ledger_ci(db_session):
    """When a CI ledger row has currency='current' (origin_attempt_id matches active),
    the ledger value IS used — this is the normal case."""
    import uuid
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    active_attempt_id = uuid.uuid4()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "sha-current-ci",
            "qualification_attempt_id": str(active_attempt_id),
            # Blob says red, but the CURRENT ledger row says passed —
            # the ledger is authoritative when currency='current'.
            "ci_resolution": {"state": "red"},
            "sub_scores": {"audit": 0.9},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Current CI row: origin_attempt_id == active_attempt_id → currency=current.
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-current-ci",
        dimension="ci",
        status="passed",
        sub_score=None,
        blocker=None,
        origin_attempt_id=active_attempt_id,
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    d = {s["key"]: s for s in result}

    # Ledger (current) wins over stale blob.
    assert d["ci"]["status"] == "passed", (
        "current CI ledger row must override the blob-derived state"
    )
    assert d["ci"]["provenance"]["currency"] == "current"


@pytest.mark.asyncio
async def test_qualifying_attempt_only_surfaces_terminal_dimension_facts(
    db_session,
):
    """Pending UI state is attempt-scoped, while same-attempt proof can advance.

    This covers the production v26 shape: a new durable attempt is active while
    the JSON projection can still carry an older attempt id, and the candidate's
    ledger contains a mixture of current, unavailable, and explicitly reused
    facts. Only terminal rows produced by the durable active attempt may replace
    the all-pending projection.
    """
    import uuid

    from app.models import Project, QualificationDimensionResult, ReleaseVersion
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Attempt scoped project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    stale_blob_attempt = uuid.uuid4()
    active_attempt = uuid.uuid4()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        active_qualification_attempt_id=active_attempt,
        last_qualification={
            "status": "qualifying",
            "candidate_sha": "sha-attempt-scoped",
            "qualification_attempt_id": str(stale_blob_attempt),
        },
    )
    db_session.add(version)
    await db_session.flush()

    db_session.add_all([
        # Audit deductions are valid scored evidence, not a hard failure. This
        # exact 0.96 shape previously rendered as a red failed dimension.
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="sha-attempt-scoped",
            dimension="audit",
            status="failed",
            sub_score=0.96,
            blocker=None,
            origin_attempt_id=active_attempt,
        ),
        # Nonterminal same-attempt facts must remain pending while qualification
        # is still running.
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="sha-attempt-scoped",
            dimension="ci",
            status="unavailable",
            blocker=None,
            origin_attempt_id=active_attempt,
        ),
        # An explicit reuse is provenance, not current-attempt terminal proof.
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha="sha-attempt-scoped",
            dimension="merge_grade",
            status="failed",
            sub_score=0.2,
            blocker="merge_grade_misses",
            origin_attempt_id=stale_blob_attempt,
            reused_by_attempt_ids=[str(active_attempt)],
        ),
    ])
    await db_session.flush()

    states = _by_key(await resolve_dimension_states_from_ledger(db_session, version))

    assert states["audit"]["status"] == "passed"
    assert states["audit"]["score"] == pytest.approx(0.96)
    assert states["audit"]["provenance"]["currency"] == "current"
    # CI ledger row status='unavailable' is skipped by the in-flight guard; blob status='qualifying' → pending.
    assert states["ci"]["status"] == "pending"
    assert states["ci"]["provenance"]["currency"] == "current"
    assert states["merge_grade"]["status"] == "pending"
    assert states["merge_grade"]["provenance"]["currency"] == "reused"


@pytest.mark.asyncio

async def test_finalized_attempt_does_not_hide_reused_terminal_failure(db_session):
    """The in-flight guard must not erase real terminal failures after finalize."""
    import uuid

    from app.models import Project, QualificationDimensionResult, ReleaseVersion
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Finalized attempt project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    prior_attempt = uuid.uuid4()
    active_attempt = uuid.uuid4()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        active_qualification_attempt_id=active_attempt,
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "sha-finalized-reuse",
            "qualification_attempt_id": str(active_attempt),
        },
    )
    db_session.add(version)
    await db_session.flush()
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-finalized-reuse",
        dimension="merge_grade",
        status="failed",
        sub_score=0.2,
        blocker="merge_grade_misses",
        origin_attempt_id=prior_attempt,
        reused_by_attempt_ids=[str(active_attempt)],
    ))
    await db_session.flush()

    states = _by_key(await resolve_dimension_states_from_ledger(db_session, version))
    assert states["merge_grade"]["status"] == "failed"
    assert states["merge_grade"]["reason"] == "merge_grade_misses"
    assert states["merge_grade"]["provenance"]["currency"] == "reused"


@pytest.mark.asyncio
async def test_qualifying_dimensions_fail_closed_without_started_at(db_session):
    """Missing generation identity must stay pending rather than claim stale proof."""
    import uuid

    from app.models import Project, ReleaseVersion
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Missing generation project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    active_attempt_id = uuid.uuid4()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        active_qualification_attempt_id=active_attempt_id,
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "sha-reused-mig-id",
            "qualification_attempt_id": str(active_attempt_id),
        },
    )
    db_session.add(version)
    await db_session.flush()
    states = _by_key(await resolve_dimension_states_from_ledger(db_session, version))
    # No ledger rows; blob has status="not_promoted" with no merge_grade_status → unavailable.
    assert states["merge_grade"]["status"] == "unavailable"

async def test_mutable_dim_reused_currency_falls_back_to_blob_migration_identity(db_session):
    """Parallel of the CI test: a reused migration_identity ledger row falls back
    to the blob-derived state (unavailable) rather than surfacing the stale passed."""
    import uuid
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    prior_attempt_id = uuid.uuid4()
    active_attempt_id = uuid.uuid4()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "sha-reused-mig-id",
            "qualification_attempt_id": str(active_attempt_id),
            # Fresh result: migration_safety is not 'ok' → resolves to unavailable/failed.
            "ci_resolution": {"state": "green"},
            "migration_safety": None,
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "sub_scores": {"audit": 0.9},
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-missing-generation",
        dimension="ci",
        status="passed",
    ))
    await db_session.flush()

    states = _by_key(await resolve_dimension_states_from_ledger(db_session, version))
    # Row with different candidate_sha is excluded; falls back to blob (ci_resolution=green).
    assert states["ci"]["status"] == "passed"

    # Stale prior-attempt migration_identity row: passed.
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-reused-mig-id",
        dimension="migration_identity",
        status="passed",
        sub_score=None,
        blocker=None,
        origin_attempt_id=prior_attempt_id,
        reused_by_attempt_ids=[str(active_attempt_id)],
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    d = {s["key"]: s for s in result}

    # Blob-derived: migration_safety=None → not_applicable or unavailable, NOT passed.
    assert d["migration_identity"]["status"] != "passed", (
        "stale reused migration_identity ledger row must not override blob-derived state"
    )
    assert d["migration_identity"]["provenance"]["currency"] == "reused"


@pytest.mark.asyncio
async def test_mutable_dim_reused_currency_falls_back_to_blob_migration_apply(db_session):
    """Parallel of the CI test: a reused migration_apply ledger row falls back
    to the blob-derived state rather than surfacing the stale passed."""
    import uuid
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    prior_attempt_id = uuid.uuid4()
    active_attempt_id = uuid.uuid4()

    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "not_promoted",
            "candidate_sha": "sha-reused-mig-apply",
            "qualification_attempt_id": str(active_attempt_id),
            # Fresh result: migration_apply_safety is 'unsafe' → fails.
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "unsafe",
            "merge_grade_status": "pass",
            "sub_scores": {"audit": 0.9},
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    # Stale prior-attempt migration_apply row: passed.
    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-reused-mig-apply",
        dimension="migration_apply",
        status="passed",
        sub_score=None,
        blocker=None,
        origin_attempt_id=prior_attempt_id,
        reused_by_attempt_ids=[str(active_attempt_id)],
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    d = {s["key"]: s for s in result}

    # Blob-derived: migration_apply_safety='unsafe' → failed, NOT passed.
    assert d["migration_apply"]["status"] != "passed", (
        "stale reused migration_apply ledger row must not override blob-derived state"
    )
    assert d["migration_apply"]["provenance"]["currency"] == "reused"


@pytest.mark.asyncio
async def test_ledger_attempt_in_flight_status_single_computation(db_session):
    """resolve_dimension_states_from_ledger computes status/attempt_in_flight once.
    Verifies the surviving computation (not the removed duplicate) correctly gates
    in-flight attempt filtering: when status='qualifying', rows are included; when
    status='promotion_pending', attempt_in_flight=False so rows are not skipped."""
    import uuid
    from app.models import Project, ReleaseVersion, QualificationDimensionResult
    from app.qualification_state_resolver import resolve_dimension_states_from_ledger

    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    attempt_id = uuid.uuid4()

    # promotion_pending=True means attempt_in_flight=False — rows must still be read.
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        last_qualification={
            "status": "qualifying",
            "promotion_pending": True,
            "candidate_sha": "sha-inflight-test",
            "qualification_attempt_id": str(attempt_id),
            "ci_resolution": {"state": "green"},
            "migration_safety": "ok",
            "migration_apply_safety": "ok",
            "merge_grade_status": "pass",
            "sub_scores": {"audit": 0.9, "test_erosion": 0.85, "smoketest": 0.8},
            "blocker": None,
            "smoketest_enabled": True,
        },
    )
    db_session.add(version)
    await db_session.flush()

    db_session.add(QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="sha-inflight-test",
        dimension="audit",
        status="passed",
        sub_score=0.99,
        blocker=None,
        origin_attempt_id=attempt_id,
    ))
    await db_session.flush()

    result = await resolve_dimension_states_from_ledger(db_session, version)
    d = {s["key"]: s for s in result}

    # Ledger row must be surfaced: attempt_in_flight=False (promotion_pending) so
    # no active-attempt filter applies, and the row's sub_score=0.99 is visible.
    assert d["audit"]["status"] == "passed"
    assert d["audit"]["score"] == pytest.approx(0.99)
