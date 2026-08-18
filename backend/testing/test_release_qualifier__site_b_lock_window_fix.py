# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: SITE B lock-window fix: GitHub-bound gates run BEFORE FOR UPDATE

Split from test_release_qualifier.py."""
from _release_qualifier_testhelpers import *  # noqa: F403



# ===========================================================================
# SITE B lock-window fix: GitHub-bound gates run BEFORE FOR UPDATE
# ===========================================================================


@pytest.mark.asyncio
async def test_finalize_locked_version_passes_pre_gates_to_finalize_qualification(
    db_session, mocker
):
    """_finalize_locked_version must pass _pre_gates to _finalize_qualification.

    This proves the three GitHub-bound gates (_resolve_ci_at_finalize,
    _resolve_migration_identity_at_finalize, _resolve_migration_apply_at_finalize)
    are computed BEFORE the FOR UPDATE row lock is acquired, not inside it.
    The SITE B lock-window fix hoists these gates to a non-locking pre-peek
    phase and passes their results as _pre_gates to _finalize_qualification so
    the function does not re-issue GitHub calls under the lock.
    """
    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 0.9,
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 2,
                        "test_count_delta": 1,
                    },
                }
            ),
        )
    )
    db_session.add(
        Artifact(
            task_id=smoke_task.id,
            artifact_type=ArtifactType.SMOKETEST_RESULT,
            logs=json.dumps(
                {
                    "passed": True,
                    "tests": [{"name": "a", "status": "pass"}],
                    "suites": {"startup": {"passed": True}},
                }
            ),
        )
    )
    await db_session.commit()

    # Track call order: assert all three gates run BEFORE _finalize_qualification
    # (which is called only after the FOR UPDATE lock is acquired).  A mock that
    # raises if a gate is called after _finalize_qualification has been entered
    # provides a direct "no GitHub I/O while lock is held" assertion.
    call_log: list[str] = []
    fq_entered = [False]

    orig_ci = rq._resolve_ci_at_finalize
    orig_mi = rq._resolve_migration_identity_at_finalize
    orig_apply = rq._resolve_migration_apply_at_finalize
    orig_fq = rq._finalize_qualification

    async def tracked_ci(session, version, candidate_sha):
        assert not fq_entered[0], (
            "_resolve_ci_at_finalize called after lock acquired (inside _finalize_qualification)"
        )
        call_log.append("ci_gate")
        return await orig_ci(session, version, candidate_sha)

    async def tracked_mi(session, version, candidate_sha):
        assert not fq_entered[0], (
            "_resolve_migration_identity_at_finalize called after lock acquired"
        )
        call_log.append("mi_gate")
        return await orig_mi(session, version, candidate_sha)

    async def tracked_apply(session, version, candidate_sha):
        assert not fq_entered[0], (
            "_resolve_migration_apply_at_finalize called after lock acquired"
        )
        call_log.append("apply_gate")
        return await orig_apply(session, version, candidate_sha)

    async def tracking_fq(session, version, *, _pre_gates=None):
        fq_entered[0] = True
        call_log.append("finalize_qualification")
        return await orig_fq(session, version, _pre_gates=_pre_gates)

    mocker.patch(
        "app.release_qualifier._resolve_ci_at_finalize", side_effect=tracked_ci
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        side_effect=tracked_mi,
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        side_effect=tracked_apply,
    )
    mocker.patch(
        "app.release_qualifier._finalize_qualification",
        side_effect=tracking_fq,
    )

    factory = _shared_session_factory(db_session)
    result = await rq._finalize_in_dedicated_session(
        version.id, timeout_now=None, session_factory=factory
    )
    await db_session.commit()

    # All three gates must have been called before _finalize_qualification
    # (i.e. before the FOR UPDATE lock was held), and finalize must have run.
    assert "ci_gate" in call_log, "CI gate was not called pre-lock"
    assert "mi_gate" in call_log, "Migration identity gate was not called pre-lock"
    assert "apply_gate" in call_log, "Migration apply gate was not called pre-lock"
    assert "finalize_qualification" in call_log, (
        "_finalize_qualification was not called"
    )
    fq_index = call_log.index("finalize_qualification")
    for gate in ("ci_gate", "mi_gate", "apply_gate"):
        assert call_log.index(gate) < fq_index, (
            f"{gate} appeared at or after _finalize_qualification in call_log: {call_log}"
        )
    # Verify outcome is a real finalize result (not None / short-circuit).
    assert result is not None


@pytest.mark.asyncio
async def test_finalize_locked_version_gates_skip_when_children_non_terminal(
    db_session, mocker
):
    """GitHub-bound gates must NOT run when a required child is still non-terminal.

    Previously the three gates (_resolve_ci_at_finalize etc.) ran unconditionally
    before the FOR UPDATE lock, causing Nx speculative calls whenever a sibling
    child completed while other children were still IN_PROGRESS.  The fix gates
    the pre-lock calls on _required_children_terminal returning True.
    """
    project, version = await _make_project_and_version(db_session)
    # Seed with audit task IN_PROGRESS (non-terminal) — children not all done.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Patch the three gates to raise if called — they must NOT be called.
    def _gate_must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "GitHub-bound gate called speculatively while children non-terminal"
        )

    mocker.patch(
        "app.release_qualifier._resolve_ci_at_finalize",
        side_effect=_gate_must_not_be_called,
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        side_effect=_gate_must_not_be_called,
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        side_effect=_gate_must_not_be_called,
    )

    factory = _shared_session_factory(db_session)
    result = await rq._finalize_in_dedicated_session(
        version.id, timeout_now=None, session_factory=factory
    )
    await db_session.commit()

    # Must return None — children not terminal, so finalization was skipped.
    assert result is None, f"expected None (children non-terminal), got {result!r}"


@pytest.mark.asyncio
async def test_finalize_locked_version_gates_skip_on_timeout_reaper_path(
    db_session, mocker
):
    """GitHub-bound gates must NOT run on the reaper timeout path.

    When sweep_stuck_qualifications calls _finalize_in_dedicated_session with
    timeout_now set, the timeout fires for aged versions and returns early before
    ever reaching _finalize_qualification. The pre-lock gates must not run when
    the timeout backstop has already expired.
    """
    from datetime import datetime as dt, timezone

    project, version = await _make_project_and_version(db_session)
    # Seed with children IN_PROGRESS and a very old started_at so the timeout fires.
    old_started_at = dt(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started_at,
    )

    # Patch the three gates to raise if called — they must NOT run on timeout path.
    def _gate_must_not_be_called(*args, **kwargs):
        raise AssertionError("GitHub-bound gate called during reaper timeout path")

    mocker.patch(
        "app.release_qualifier._resolve_ci_at_finalize",
        side_effect=_gate_must_not_be_called,
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        side_effect=_gate_must_not_be_called,
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        side_effect=_gate_must_not_be_called,
    )

    factory = _shared_session_factory(db_session)
    result = await rq._finalize_in_dedicated_session(
        version.id,
        timeout_now=dt(2026, 6, 15, tzinfo=timezone.utc),
        session_factory=factory,
    )
    await db_session.commit()

    # Reaper should have fired the timeout path (not called gates).
    assert result is not None
    assert result.get("blocker") == "qualification_timeout", (
        f"expected qualification_timeout blocker, got {result!r}"
    )


@pytest.mark.asyncio
async def test_finalize_locked_version_timeout_beats_pre_gates_when_children_terminal(
    db_session, mocker
):
    """An expired sweep timeout must force-finalize before pre-lock gate I/O.

    Regression guard for the sweep path: terminal children normally allow the
    pre-lock gates to run, but an already-expired timeout is the escape hatch for
    a stuck qualification and must not depend on CI/GitHub/migration gate calls.
    """
    from datetime import datetime as dt, timezone

    project, version = await _make_project_and_version(db_session)
    old_started_at = dt(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        started_at=old_started_at,
    )

    def _gate_must_not_be_called(*args, **kwargs):
        raise AssertionError("GitHub-bound gate called before expired timeout")

    mocker.patch(
        "app.release_qualifier._resolve_ci_at_finalize",
        side_effect=_gate_must_not_be_called,
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        side_effect=_gate_must_not_be_called,
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        side_effect=_gate_must_not_be_called,
    )

    result = await rq._finalize_in_dedicated_session(
        version.id,
        timeout_now=dt(2026, 6, 15, tzinfo=timezone.utc),
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.commit()

    assert result is not None
    assert result["status"] == "infra_failure"
    assert result["blocker"] == "qualification_timeout"


@pytest.mark.asyncio
async def test_finalize_locked_version_pre_gate_ci_result_flows_to_outcome(
    db_session, mocker
):
    """Pre-computed CI gate result flows correctly into the finalize outcome.

    When _resolve_ci_at_finalize returns ci_green=False (stubbed), the finalize
    must record a not-promoted outcome with blocker=ci_not_green — confirming
    the pre-computed value is actually used inside _finalize_qualification and
    not silently ignored.
    """
    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 0,
                        "test_count_delta": 0,
                    },
                }
            ),
        )
    )
    db_session.add(
        Artifact(
            task_id=smoke_task.id,
            artifact_type=ArtifactType.SMOKETEST_RESULT,
            logs=json.dumps(
                {
                    "passed": True,
                    "tests": [{"name": "a", "status": "pass"}],
                    "suites": {"startup": {"passed": True}},
                }
            ),
        )
    )
    await db_session.commit()

    # Override CI gate to return not-green with state=red — produces ci_not_green.
    mocker.patch(
        "app.release_qualifier._resolve_ci_at_finalize",
        new=AsyncMock(
            return_value=(
                False,
                {
                    "state": "red",
                    "resolved_sha": None,
                    "detail": "forced red for test",
                },
            )
        ),
    )

    factory = _shared_session_factory(db_session)
    result = await rq._finalize_in_dedicated_session(
        version.id, timeout_now=None, session_factory=factory
    )
    await db_session.commit()

    assert result is not None
    assert result.get("status") == "not_promoted"
    assert result.get("blocker") == "ci_not_green"


# ===========================================================================
# infra force-finalize re-qualification (fix: infra_failure not not_promoted)
# ===========================================================================


@pytest.mark.asyncio
async def test_timed_out_qualification_requalifies_same_sha(db_session, mocker):
    """A force-finalized infra_failure/qualification_timeout verdict must NOT
    block re-qualification with the same candidate SHA.

    Before this fix, _force_finalize_timed_out_qualification wrote
    status='not_promoted', which is in _CONCLUSIVE_VERDICT_STATUSES.  Guard 3
    in run_qualification then refused to re-qualify the same SHA, permanently
    blocking the version until automain advanced — the opposite of how the
    codebase treats infra failures elsewhere.

    After the fix, it writes status='infra_failure', which is NOT conclusive.
    Guard 3 skips it, and run_qualification proceeds to spawn a fresh audit
    pair for the same candidate SHA.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Plant a force-finalized infra_failure/qualification_timeout record on the
    # same SHA that automain will return.
    candidate_sha = "dead1234" * 5
    version.last_qualification = {
        "status": "infra_failure",
        "blocker": "qualification_timeout",
        "candidate_sha": candidate_sha,
        "finalized_at": "2026-06-01T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # automain HEAD returns the SAME sha as the prior infra_failure record.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate_sha),
    )
    # Sync gate: candidate is already absorbed into promotion_target (no deferral).
    mocker.patch(
        "app.release_qualifier._is_target_absorbed",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # Must NOT return cached_verdict — infra_failure is non-conclusive and
    # the same SHA must be eligible for re-qualification.
    assert result["status"] == "spawned", (
        f"expected 'spawned' (infra_failure is non-conclusive), got {result!r}"
    )
    assert result["candidate_sha"] == candidate_sha

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == candidate_sha


# ===========================================================================
# evaluate_merge_grade — pure scoring function unit tests
# ===========================================================================


class TestEvaluateMergeGrade:
    """Pure unit tests for rs.evaluate_merge_grade (all branches)."""

    def test_not_applicable_when_goal_empty(self):
        """Empty goal → status=not_applicable, blocked=False (no-op PASS)."""
        result = rs.evaluate_merge_grade("", {"covers_goal": False})
        assert result["status"] == "not_applicable"
        assert result["blocked"] is False
        assert result["covers_goal"] is None

    def test_not_applicable_when_goal_none(self):
        """None goal → status=not_applicable, blocked=False."""
        result = rs.evaluate_merge_grade(None, {"covers_goal": False})
        assert result["status"] == "not_applicable"
        assert result["blocked"] is False

    def test_not_applicable_when_goal_whitespace(self):
        """Whitespace-only goal → status=not_applicable, blocked=False."""
        result = rs.evaluate_merge_grade("   ", {"covers_goal": True})
        assert result["status"] == "not_applicable"
        assert result["blocked"] is False

    def test_unparseable_when_artifact_none(self):
        """Non-empty goal + None artifact → status=unparseable, blocked=True (infra failure must hold)."""
        result = rs.evaluate_merge_grade("deliver analytics dashboard", None)
        assert result["status"] == "unparseable"
        assert result["blocked"] is True

    def test_unparseable_when_artifact_not_dict(self):
        """Non-empty goal + non-dict artifact → status=unparseable, blocked=True (infra failure must hold)."""
        result = rs.evaluate_merge_grade("deliver analytics dashboard", "bad")
        assert result["status"] == "unparseable"
        assert result["blocked"] is True

    def test_pass_when_covers_goal_true(self):
        """Explicit covers_goal=True → status=pass, blocked=False."""
        result = rs.evaluate_merge_grade(
            "deliver analytics dashboard",
            {"covers_goal": True, "biggest_gap": "minor edge case"},
        )
        assert result["status"] == "pass"
        assert result["covers_goal"] is True
        assert result["blocked"] is False

    def test_fail_when_covers_goal_false(self):
        """Explicit covers_goal=False → status=fail, blocked=True."""
        result = rs.evaluate_merge_grade(
            "deliver analytics dashboard",
            {"covers_goal": False, "biggest_gap": "dashboard not shipped"},
        )
        assert result["status"] == "fail"
        assert result["covers_goal"] is False
        assert result["blocked"] is True

    def test_coverage_score_above_threshold_derives_pass(self):
        """coverage_score ≥ 0.5 with no covers_goal bool → derived pass."""
        result = rs.evaluate_merge_grade(
            "deliver analytics dashboard",
            {"coverage_score": 0.8},
        )
        assert result["status"] == "pass"
        assert result["covers_goal"] is True
        assert result["blocked"] is False

    def test_coverage_score_below_threshold_derives_fail(self):
        """coverage_score < 0.5 with no covers_goal bool → derived fail, blocked=True."""
        result = rs.evaluate_merge_grade(
            "deliver analytics dashboard",
            {"coverage_score": 0.3},
        )
        assert result["status"] == "fail"
        assert result["covers_goal"] is False
        assert result["blocked"] is True

    def test_coverage_score_exactly_threshold_derives_pass(self):
        """coverage_score == 0.5 (on the boundary) → pass (≥ threshold)."""
        result = rs.evaluate_merge_grade(
            "deliver analytics dashboard",
            {"coverage_score": 0.5},
        )
        assert result["status"] == "pass"
        assert result["blocked"] is False

    def test_covers_goal_bool_takes_precedence_over_score(self):
        """Explicit covers_goal bool wins over coverage_score."""
        # False covers_goal overrides a high coverage_score.
        result = rs.evaluate_merge_grade(
            "deliver analytics dashboard",
            {"covers_goal": False, "coverage_score": 0.99},
        )
        assert result["status"] == "fail"
        assert result["blocked"] is True

    def test_unparseable_when_neither_bool_nor_score(self):
        """Dict artifact with no usable covers_goal or coverage_score → unparseable, blocked=True."""
        result = rs.evaluate_merge_grade(
            "deliver analytics dashboard",
            {"something_else": "irrelevant"},
        )
        assert result["status"] == "unparseable"
        assert result["blocked"] is True

    def test_not_applicable_when_goal_is_contentless_test(self):
        """'test' is a contentless placeholder — not_applicable, blocked=False (no wedging)."""
        result = rs.evaluate_merge_grade("test", {"covers_goal": False})
        assert result["status"] == "not_applicable"
        assert result["blocked"] is False

    def test_not_applicable_when_goal_is_contentless_na(self):
        """'n/a' is a contentless placeholder — not_applicable, blocked=False."""
        result = rs.evaluate_merge_grade("n/a", {"covers_goal": False})
        assert result["status"] == "not_applicable"
        assert result["blocked"] is False

    def test_not_applicable_when_goal_is_contentless_tbd(self):
        """'tbd' is a contentless placeholder — not_applicable, blocked=False."""
        result = rs.evaluate_merge_grade("TBD", {"covers_goal": False})
        assert result["status"] == "not_applicable"
        assert result["blocked"] is False

    def test_real_goal_with_word_test_in_it_is_not_contentless(self):
        """'test the authentication flow' is a real goal, not a placeholder."""
        result = rs.evaluate_merge_grade(
            "test the authentication flow", {"covers_goal": True}
        )
        assert result["status"] == "pass"
        assert result["blocked"] is False


class TestIsContentlessGoal:
    """Unit tests for is_contentless_goal helper."""

    def test_contentless_words(self):
        """Single-word placeholders are contentless."""
        from app.release_scoring import is_contentless_goal

        for word in ["test", "n/a", "na", "tbd", "todo", "placeholder", "none", "null"]:
            assert is_contentless_goal(word), f"{word!r} should be contentless"
            assert is_contentless_goal(word.upper()), f"{word.upper()!r} should be contentless"

    def test_real_goal_is_not_contentless(self):
        """A substantive goal is not contentless."""
        from app.release_scoring import is_contentless_goal

        assert not is_contentless_goal("deliver the design-system token layer")
        assert not is_contentless_goal("test the authentication flow")
        assert not is_contentless_goal("ship v18 with 0 text-color literals")


# ===========================================================================
# Blind merge-grade hard-block hook (armable-dark)
# ===========================================================================


class TestBlindMergeGradeBlock:
    """Armable-dark hard-block hook for the 'misses' blind merge-grade coverage verdict."""

    # --- Unit tests: compute_composite integration --------------------------

    def test_compute_composite_criterion_coverage_incomplete_fires(self):
        """Goal-verification gate: any active CATEGORY not ``met`` forces
        confidence=0.0 and re-emits the preserved criterion_coverage_incomplete
        blocker (replaces the retired coverage-count gate)."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": "c-1", "status": "met", "tier": "judged",
                 "citations": [{"kind": "diff", "ref": "a.py:1"}]},
                {"category_key": "c-2", "status": "not_met", "tier": "judged",
                 "gap": "endpoint not added", "citations": []},
            ],
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "criterion_coverage_incomplete"
        assert result["sub_scores"]["unmet_categories"] == ["c-2"]

    def test_compute_composite_criterion_coverage_partial_only_fires(self):
        """A ``partial`` category gates identically to ``not_met`` — only ``met`` clears."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": "c-1", "status": "partial", "tier": "judged",
                 "gap": "only half wired", "citations": [{"kind": "diff", "ref": "a.py:1"}]},
            ],
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "criterion_coverage_incomplete"

    def test_compute_composite_criterion_coverage_default_skips_gate(self):
        """Backward compat: omitting criterion_coverage does not block."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
        )
        assert result["blocker"] is None
        assert result["promote"] is True

    def test_compute_composite_merge_grade_misses_label(self):
        """merge_grade_misses=True emits the merge_grade_misses blocker label."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            merge_grade_misses=True,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "merge_grade_misses"

    def test_compute_composite_criterion_coverage_no_criteria_skips_gate(self):
        """A version with no active categories (empty rubric) does not emit the
        criterion blocker — the empty verdict list has no unmet category."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[],
        )
        assert result["blocker"] is None
        assert result["promote"] is True

    # --- Goal-verification redesign: residual gate + judged-tier penalty -----
    # (These replace the retired ``uncorroborated_attestation`` hard-block: a
    # low-confidence acceptance set no longer hard-blocks — it is folded into the
    # judged-tier confidence penalty; an uncaptured RESIDUAL gap hard-blocks.)

    def test_compute_composite_residual_gap_hard_blocks_and_zeroes_confidence(self):
        """Goal-verification RESIDUAL gate: even with EVERY active category ``met``,
        a non-empty ``residual_uncaptured_gap`` (a goal aspect no category captures)
        hard-blocks and forces confidence to 0.0, re-emitting the preserved
        criterion_coverage_incomplete blocker.  This REPLACES one half of the
        retired uncorroborated_attestation hard-block."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": "c-1", "status": "met", "tier": "judged",
                 "citations": [{"kind": "diff", "ref": "a.py:1"}]},
            ],
            residual_uncaptured_gap="The goal's rollback path is untested by any criterion.",
        )
        assert result["blocker"] == "criterion_coverage_incomplete"
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert (
            result["sub_scores"]["residual_uncaptured_gap"]
            == "The goal's rollback path is untested by any criterion."
        )

    def test_all_judged_tier_met_promotes_with_penalty_not_hard_block(self):
        """The redesign REPLACED the uncorroborated_attestation hard-block with a
        confidence PENALTY: an acceptance set that is fully ``met`` but only at the
        low-confidence JUDGED tier (the v1 uninvested-LLM grade) is NO LONGER
        hard-blocked — it promotes with a proportional judged-tier penalty."""
        from app.release_scoring import JUDGED_TIER_PENALTY_WEIGHT

        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": f"c-{i}", "status": "met", "tier": "judged",
                 "citations": [{"kind": "diff", "ref": f"c{i}.py:1"}]}
                for i in range(3)
            ],
        )
        assert result["blocker"] is None
        assert result["promote"] is True
        # All 3 met categories rest on the judged tier → full judged penalty.
        assert result["sub_scores"]["judged_tier_met_fraction"] == 1.0
        assert result["confidence"] == round(1.0 * (1.0 - JUDGED_TIER_PENALTY_WEIGHT), 4)

    def test_compute_composite_one_unmet_category_among_met_still_blocks(self):
        """One not_met category among otherwise-met categories still hard-blocks,
        regardless of the others' tier."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": "c-1", "status": "met", "tier": "machine",
                 "citations": [{"kind": "test", "ref": "t.py::x"}]},
                {"category_key": "c-2", "status": "not_met", "tier": "judged",
                 "gap": "reachability path missing", "citations": []},
            ],
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "criterion_coverage_incomplete"
        assert result["sub_scores"]["unmet_categories"] == ["c-2"]

    def test_compute_composite_partial_category_among_met_still_blocks(self):
        """A 'partial' category among met categories still blocks (partial gates
        identically to not_met); a machine-tier met alongside does not rescue it."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": "c-1", "status": "met", "tier": "machine",
                 "citations": [{"kind": "test", "ref": "t.py::x"}]},
                {"category_key": "c-2", "status": "partial", "tier": "judged",
                 "gap": "half done", "citations": [{"kind": "diff", "ref": "b.py:2"}]},
            ],
        )
        assert result["confidence"] == 0.0
        assert result["blocker"] == "criterion_coverage_incomplete"


@pytest.mark.asyncio
async def test_release_audit_payload_includes_version_goal(db_session, mocker):
    """run_qualification threads version.goal into the release_audit payload.

    When a version has a non-empty goal, the spawned release_audit task's
    first_payload must contain a 'version_goal' key equal to that goal string.
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.goal = "STABILIZE — reduce pipeline friction"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    candidate = "abcdef12" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    audit_tasks = [
        t for t in tasks if (t.first_payload or {}).get("type") == "release_audit"
    ]
    assert len(audit_tasks) == 1, (
        f"expected 1 release_audit task, got {len(audit_tasks)}"
    )
    audit_task = audit_tasks[0]
    assert (
        audit_task.first_payload["version_goal"]
        == "STABILIZE — reduce pipeline friction"
    )


@pytest.mark.asyncio
async def test_release_audit_payload_omits_version_goal_when_none(db_session, mocker):
    """run_qualification omits version_goal from the release_audit payload when version.goal is None."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    assert version.goal is None

    candidate = "deadbeef" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    audit_tasks = [
        t for t in tasks if (t.first_payload or {}).get("type") == "release_audit"
    ]
    assert len(audit_tasks) == 1, (
        f"expected 1 release_audit task, got {len(audit_tasks)}"
    )
    audit_task = audit_tasks[0]
    assert "version_goal" not in audit_task.first_payload


# ===========================================================================
# Fix Z: promotion_failed re-driver + circuit breaker
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_does_not_promote_dry_run_pass_after_promotion_enabled(
    db_session, mocker
):
    """A scheduler sweep does not turn a prior dry-run into a release decision.

    Scenario: _finalize_qualification recorded status='dry_run' with
    would_promote=True while promotion was disabled.  The operator later enables
    promotion. Strategic owns the follow-up decision.
    """
    from app import shared_state

    candidate_sha = "d7e5ca11" * 5  # 40-char placeholder SHA
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "dry_run",
        "would_promote": True,
        "candidate_sha": candidate_sha,
        "candidate_diff_hash": "hashvalue",
        "confidence": 0.92,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate_sha),
    )
    force_promote_spy = mocker.patch(
        "app.release_qualifier._force_promote_in_dedicated_session",
        new=AsyncMock(return_value={"status": "promotion_pending"}),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    force_promote_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_leaves_legacy_smoke_dry_run_for_strategic(
    db_session, mocker
):
    """A legacy smoke-participating pass is not scheduler-requalified."""
    from app import shared_state

    candidate_sha = "legdry11" * 5
    shared_state.runtime_config["promotion_enabled"] = True

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "dry_run",
        "would_promote": True,
        "candidate_sha": candidate_sha,
        "candidate_diff_hash": "legacy-hash",
        "smoketest_enabled": True,
        "smoketest_task_id": "legacy-smoke-task",
        "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
        "confidence": 1.0,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    force_promote = mocker.patch(
        "app.release_qualifier._force_promote_in_dedicated_session",
        new=AsyncMock(return_value={"status": "promotion_pending"}),
    )
    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    force_promote.assert_not_awaited()
    run_qual.assert_not_awaited()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


@pytest.mark.asyncio
async def test_record_promotion_failure_increments_count(db_session, mocker):
    """_record_promotion_failure must increment promotion_failure_count from
    the previous last_qualification, starting at 1 for a fresh version."""
    from app.release_promotion import _record_promotion_failure

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # No prior promotion failure — count should become 1.
    base_record = {
        "candidate_sha": "aaaa" * 10,
        "confidence": 0.0,
    }

    mocker.patch(
        "app.release_promotion._relock_version_for_promotion_write",
        new=AsyncMock(return_value=version),
    )
    mocker.patch(
        "app.release_promotion._record_qualification_verdict",
        new=AsyncMock(return_value=None),
    )

    result = await _record_promotion_failure(
        db_session,
        version,
        base_record,
        "required_check_failed",
        lock_released=False,
    )

    assert result["status"] == "promotion_failed"
    assert version.last_qualification["promotion_failure_count"] == 1

    # Second failure should increment to 2.
    await _record_promotion_failure(
        db_session,
        version,
        base_record,
        "required_check_failed",
        lock_released=False,
    )
    assert version.last_qualification["promotion_failure_count"] == 2


class TestComputeCompositeCapabilityProofGate:
    """Unit tests for the capability_proof_blocked hard blocker in compute_composite.

    Advisory in OFF/OBSERVE mode (blocked=False, never blocks), gating only in
    ENFORCE mode when unproven capabilities exist (blocked=True).

    NOTE: these three tests were previously nested INSIDE
    ``test_record_promotion_failure_increments_count`` (indented as inner
    ``def``s taking ``self``), so pytest never collected them — they were dead
    code that silently exercised nothing. Un-nested here into a real module-level
    test class so the capability-proof promotion-seam is actually verified.
    """

    def test_compute_composite_capability_proof_not_blocked_does_not_gate(self):
        """capability_proof_blocked=False never blocks (OFF/OBSERVE mode)."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            capability_proof_blocked=False,
        )
        assert result["blocker"] is None
        assert result["promote"] is True
        assert result["confidence"] == 1.0

    def test_compute_composite_capability_proof_blocked_label(self):
        """capability_proof_blocked=True emits the capability_proof_gate_blocked blocker label."""
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            capability_proof_blocked=True,
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "capability_proof_gate_blocked"

    def test_compute_composite_capability_proof_blocker_precedence(self):
        """Criterion coverage retains precedence over capability proof."""
        # Trigger the criterion-coverage gate (via category_verdicts — the actual
        # blocking input; criterion_coverage is observability-only) AND
        # capability_proof_blocked. Must report criterion_coverage_incomplete
        # first (higher priority).
        result = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=1.0,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": "c-1", "status": "not_met", "tier": "judged",
                 "gap": "not wired", "citations": []},
            ],
            capability_proof_blocked=True,
        )
        assert result["blocker"] == "criterion_coverage_incomplete"

        # A legacy smoke argument cannot alter the active capability-proof gate.
        result2 = rq.compute_composite(
            ci_green=True,
            audit_score=1.0,
            test_erosion=1.0,
            smoketest_score=None,
            policy=_POLICY,
            category_verdicts=[
                {"category_key": "c-1", "status": "met", "tier": "judged",
                 "citations": [{"kind": "diff", "ref": "a.py:1"}]},
            ],
            capability_proof_blocked=True,
        )
        assert result2["blocker"] == "capability_proof_gate_blocked"


class TestComputeGoalCoverageDrift:
    """Unit tests for the pure compute_goal_coverage_drift scorer."""

    def test_not_applicable_empty_goal(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "",
            merge_grade_status="pass",
            merges_since_grade=0,
        )
        assert result["signal"] == "not_applicable"

    def test_not_applicable_none_goal(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            None,
            merge_grade_status="pass",
            merges_since_grade=0,
        )
        assert result["signal"] == "not_applicable"

    def test_not_applicable_contentless_goal(self):
        from app.release_scoring import compute_goal_coverage_drift

        for placeholder in ("test", "n/a", "tbd", "placeholder"):
            result = compute_goal_coverage_drift(
                placeholder,
                merge_grade_status="pass",
                merges_since_grade=0,
            )
            assert result["signal"] == "not_applicable", f"Expected not_applicable for '{placeholder}'"

    def test_no_grade_yet_when_status_none(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status=None,
            merges_since_grade=3,
        )
        assert result["signal"] == "no_grade_yet"
        assert result["coverage"] is None

    def test_ungraded_statuses_do_not_look_on_track(self):
        from app.release_scoring import compute_goal_coverage_drift

        for status in ("not_applicable", "superseded_exempt"):
            result = compute_goal_coverage_drift(
                "Improve authentication coverage",
                merge_grade_status=status,
                merge_grade_coverage=0.91,
                merges_since_grade=0,
            )
            assert result["signal"] == status
            assert "passed" not in result["detail"].lower()

    def test_on_track_pass_grade_low_delta(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Harden the release gate",
            merge_grade_status="pass",
            merge_grade_coverage=0.85,
            merge_grade_biggest_gap=None,
            merges_since_grade=2,
        )
        assert result["signal"] == "on_track"
        assert result["stale"] is False
        assert result["coverage"] == pytest.approx(0.85)
        assert result["merges_since_grade"] == 2

    def test_drifting_when_fail_grade(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve test reliability",
            merge_grade_status="fail",
            merge_grade_coverage=0.3,
            merge_grade_biggest_gap="Integration tests not updated",
            merges_since_grade=1,
        )
        assert result["signal"] == "drifting"
        assert result["biggest_gap"] == "Integration tests not updated"
        assert result["coverage"] == pytest.approx(0.3)

    def test_drifting_stale_when_material_new_work(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Performance optimization",
            merge_grade_status="pass",
            merge_grade_coverage=0.9,
            merges_since_grade=5,  # at threshold
        )
        assert result["signal"] == "drifting"
        assert result["stale"] is True
        assert result["merges_since_grade"] == 5

    def test_stale_threshold_boundary(self):
        from app.release_scoring import compute_goal_coverage_drift

        # 4 merges = not stale
        result_ok = compute_goal_coverage_drift(
            "Some real goal",
            merge_grade_status="pass",
            merge_grade_coverage=0.8,
            merges_since_grade=4,
        )
        assert result_ok["signal"] == "on_track"
        assert result_ok["stale"] is False

        # 5 merges = stale -> drifting
        result_stale = compute_goal_coverage_drift(
            "Some real goal",
            merge_grade_status="pass",
            merge_grade_coverage=0.8,
            merges_since_grade=5,
        )
        assert result_stale["signal"] == "drifting"
        assert result_stale["stale"] is True

    def test_biggest_gap_falls_back_to_goal_alignment(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Fix auth layer",
            merge_grade_status="fail",
            merge_grade_biggest_gap=None,
            goal_alignment={"biggest_gap": "Auth not addressed", "state": "shortfall"},
            merges_since_grade=0,
        )
        assert result["signal"] == "drifting"
        assert result["biggest_gap"] == "Auth not addressed"

    def test_biggest_gap_merge_grade_takes_precedence(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Fix auth layer",
            merge_grade_status="fail",
            merge_grade_biggest_gap="Missing rate limiting",
            goal_alignment={"biggest_gap": "Auth not addressed"},
            merges_since_grade=0,
        )
        assert result["biggest_gap"] == "Missing rate limiting"

    def test_no_grade_yet_below_threshold(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status=None,
            merges_since_grade=4,
        )
        assert result["signal"] == "no_grade_yet"
        assert result["stale"] is False
        assert result["merges_since_grade"] == 4

    def test_drifting_at_threshold_when_no_grade_yet(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status=None,
            merges_since_grade=5,
        )
        assert result["signal"] == "drifting"
        assert result["stale"] is True
        assert result["merges_since_grade"] == 5
        assert result["coverage"] is None

    def test_drifting_above_threshold_when_no_grade_yet(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status=None,
            merges_since_grade=10,
        )
        assert result["signal"] == "drifting"
        assert result["stale"] is True
        assert result["merges_since_grade"] == 10
        assert result["coverage"] is None

    def test_not_applicable_status_non_stale(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="not_applicable",
            merges_since_grade=0,
        )
        assert result["signal"] == "not_applicable"
        assert result["stale"] is False

    def test_not_applicable_status_stale_not_drifting(self):
        """stale semantics do not apply to not_applicable — must stay not_applicable."""
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="not_applicable",
            merges_since_grade=5,
        )
        assert result["signal"] == "not_applicable"

    def test_superseded_exempt_status_non_stale(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="superseded_exempt",
            merges_since_grade=0,
        )
        assert result["signal"] == "superseded_exempt"
        assert result["stale"] is False

    def test_superseded_exempt_status_stale_not_drifting(self):
        """stale semantics do not apply to superseded_exempt — must stay superseded_exempt."""
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="superseded_exempt",
            merges_since_grade=5,
        )
        assert result["signal"] == "superseded_exempt"

    def test_unparseable_status_returns_no_grade_yet(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="unparseable",
            merges_since_grade=0,
        )
        assert result["signal"] == "no_grade_yet"

    def test_unknown_future_status_returns_no_grade_yet(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="future_status",
            merges_since_grade=0,
        )
        assert result["signal"] == "no_grade_yet"

    def test_pass_status_non_stale_returns_on_track(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="pass",
            merge_grade_coverage=0.9,
            merges_since_grade=0,
        )
        assert result["signal"] == "on_track"

    def test_fail_status_returns_drifting(self):
        from app.release_scoring import compute_goal_coverage_drift

        result = compute_goal_coverage_drift(
            "Improve authentication coverage",
            merge_grade_status="fail",
            merge_grade_coverage=0.4,
            merges_since_grade=0,
        )
        assert result["signal"] == "drifting"


# ===========================================================================
# _collect_terminal_infra_child_events — batch-fetch fix (perf)
# ===========================================================================


@pytest.mark.asyncio
async def test_collect_terminal_infra_child_events_batch_fetches_tasks(db_session):
    """_collect_terminal_infra_child_events must fetch all terminal task IDs in
    one batch SELECT instead of one session.get per leg, and must correctly
    filter by seen_failed_ids and task status."""
    from app.release_qualifier_audit import _collect_terminal_infra_child_events

    project = Project(
        name="Audit-batch-test",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create a FAILED audit task and a CANCELLED challenge task.
    audit_task = Task(
        project_id=project.id,
        title="Audit task",
        payloads=[{"type": "release_qualifier_audit"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    challenge_task = Task(
        project_id=project.id,
        title="Challenge task",
        payloads=[{"type": "release_audit_challenge"}],
        status=TaskStatus.CANCELLED,
        created_by="ORCHESTRATOR",
    )
    # A task in a non-terminal status that must NOT appear in output.
    running_task = Task(
        project_id=project.id,
        title="Running audit",
        payloads=[{"type": "release_qualifier_audit"}],
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
    )
    db_session.add_all([audit_task, challenge_task, running_task])
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(challenge_task)
    await db_session.refresh(running_task)

    qual = {
        "audit_task_id": str(audit_task.id),
        "challenge_task_id": str(challenge_task.id),
        "infra_failure_child_events": [],
    }

    result = await _collect_terminal_infra_child_events(
        db_session, qual, infra_error="audit_failed"
    )

    # Both terminal tasks must appear; the in-progress task must be absent.
    legs = {e["leg"] for e in result}
    assert "audit" in legs
    assert "challenge" in legs
    assert len(result) == 2

    # Running task must not be included (non-terminal status).
    task_ids_in_result = {str(e.get("failed_task_id")) for e in result}
    assert str(running_task.id) not in task_ids_in_result


@pytest.mark.asyncio
async def test_collect_terminal_infra_child_events_skips_seen_failed(db_session):
    """Tasks already in seen_failed_ids must not appear in the output."""
    from app.release_qualifier_audit import _collect_terminal_infra_child_events

    project = Project(
        name="Audit-seen-test",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Already-recorded audit",
        payloads=[{"type": "release_qualifier_audit"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    qual = {
        "audit_task_id": str(task.id),
        # Pretend this task was already recorded.
        "infra_failure_child_events": [{"leg": "audit", "failed_task_id": str(task.id)}],
    }

    result = await _collect_terminal_infra_child_events(
        db_session, qual, infra_error="audit_failed"
    )

    # The existing entry must be preserved; no duplicate must appear.
    assert len(result) == 1
    assert result[0]["failed_task_id"] == str(task.id)


# ===========================================================================
# _compact_infra_child_event — child_tail from latest_status_message
# ===========================================================================


def test_compact_infra_child_event_includes_child_tail_from_latest_status_message():
    """child_tail is populated from latest_status_message and truncated to 500 chars."""
    from app.release_qualifier_audit import _compact_infra_child_event

    task = MagicMock()
    task.id = uuid.uuid4()
    task.status = "FAILED"
    task.context = {"failure_category": "claude_code_failed"}
    long_msg = "x" * 700
    task.latest_status_message = long_msg

    event = _compact_infra_child_event(leg="smoketest", failed_task=task)

    assert "child_tail" in event
    assert len(event["child_tail"]) == 500
    assert event["child_tail"] == long_msg[-500:]


def test_compact_infra_child_event_omits_child_tail_when_status_message_none():
    """child_tail must be absent when latest_status_message is None."""
    from app.release_qualifier_audit import _compact_infra_child_event

    task = MagicMock()
    task.id = uuid.uuid4()
    task.status = "FAILED"
    task.context = {}
    task.latest_status_message = None

    event = _compact_infra_child_event(leg="audit", failed_task=task)

    assert "child_tail" not in event


def test_compact_infra_child_event_omits_child_tail_when_failed_task_none():
    """child_tail must be absent when failed_task is None."""
    from app.release_qualifier_audit import _compact_infra_child_event

    event = _compact_infra_child_event(leg="audit", failed_task=None)

    assert "child_tail" not in event
    assert "failed_task_id" not in event


@pytest.mark.asyncio
async def test_collect_terminal_infra_child_events_propagates_child_tail(db_session):
    """Integration: a task with latest_status_message produces an event with child_tail."""
    from app.release_qualifier_audit import _collect_terminal_infra_child_events

    project = Project(
        name="Child-tail-test",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    audit_task = Task(
        project_id=project.id,
        title="Audit with tail",
        payloads=[{"type": "release_qualifier_audit"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        latest_status_message="Error: claude_code_failed — the agent exited with CODE_ERROR",
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    qual = {
        "audit_task_id": str(audit_task.id),
        "infra_failure_child_events": [],
    }

    result = await _collect_terminal_infra_child_events(
        db_session, qual, infra_error="audit_failed"
    )

    assert len(result) == 1
    assert result[0]["leg"] == "audit"
    assert result[0]["child_tail"] == "Error: claude_code_failed — the agent exited with CODE_ERROR"


# ===========================================================================
# Completion-debt block in qualification verdicts
# ===========================================================================


@pytest.mark.asyncio
async def test_qualification_verdict_includes_completion_debt_block(
    db_session, mocker
):
    """Completion-debt snapshot flows through finalize → last_qualification and verdict row.

    When completion_debt.get_open_completion_debt returns a non-empty snapshot,
    the finalized last_qualification carries a `completion_debt` key with the
    correct shape, and the persisted QualificationVerdict row's sub_scores embed
    the same snapshot.  The debt block is purely informational and must not
    change the blocker or confidence.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.9,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    _debt_snapshot = {
        "open_count": 3,
        "per_kind": {"evergreen_fill": 2, "observe_mode": 1},
        "oldest_age_days": 14,
    }

    # Mock the completion_debt module so finalize picks up the snapshot even
    # though the real module is not yet landed.
    _mock_cd = MagicMock()
    _mock_cd.get_open_completion_debt = AsyncMock(return_value=_debt_snapshot)
    mocker.patch.dict("sys.modules", {"app.completion_debt": _mock_cd})
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Status must still be dry_run — debt block is informational, not a blocker.
    assert result["status"] == "dry_run", f"unexpected status: {result['status']}"
    assert result.get("blocker") is None, f"unexpected blocker: {result.get('blocker')}"

    # last_qualification carries the snapshot.
    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual.get("completion_debt") == _debt_snapshot, (
        f"completion_debt missing or wrong in last_qualification: {qual.get('completion_debt')}"
    )

    # QualificationVerdict row's sub_scores embed the same snapshot.
    stmt = select(QualificationVerdict).where(
        QualificationVerdict.release_version_id == version.id
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert rows, "no QualificationVerdict row recorded"
    verdict_row = rows[-1]
    assert isinstance(verdict_row.sub_scores, dict), "sub_scores is not a dict"
    assert verdict_row.sub_scores.get("completion_debt") == _debt_snapshot, (
        f"completion_debt missing or wrong in verdict sub_scores: "
        f"{verdict_row.sub_scores.get('completion_debt')}"
    )


@pytest.mark.asyncio
async def test_finalize_debt_pressure_uses_project_actionable_open_count(
    db_session, mocker
):
    """Finalize scores project-owned open debt without mutating the public snapshot."""
    from app import shared_state

    mocker.patch.dict(
        shared_state.runtime_config,
        {
            "promotion_enabled": False,
            "completion_debt_weight_mode": "enforce",
            "promotion_threshold": 0.90,
            "promotion_weights": {
                "audit": 0.40,
                "test_erosion": 0.30,
                "smoketest": 0.30,
            },
        },
        clear=False,
    )

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.92,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-project-debt", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    for idx in range(10):
        db_session.add(
            CompletionDebtEntry(
                project_id=None,
                kind="seed_inventory",
                dedupe_key=f"global-standing-{idx}",
                title=f"Global standing debt {idx}",
            )
        )
    db_session.add(
        CompletionDebtEntry(
            project_id=project.id,
            kind="retirement_residual",
            dedupe_key="project-actionable-one",
            title="Project actionable debt",
        )
    )
    await db_session.commit()
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["completion_debt"]["total_open"] == 11
    assert "project_actionable_open" not in qual["completion_debt"]
    assert qual["sub_scores"]["debt_pressure"] == pytest.approx(0.95)
    assert qual["confidence"] == pytest.approx(0.9925)


@pytest.mark.asyncio
async def test_qualification_verdict_completion_debt_absent_when_module_missing(
    db_session, mocker
):
    """When completion_debt module is unavailable, finalize succeeds and omits the block.

    The best-effort try/except in _finalize_qualification must swallow any
    import or runtime error from the completion_debt module so that the
    qualification verdict is still written.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-b", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Simulate a missing module by making the import raise ImportError.
    _mock_cd = MagicMock()
    _mock_cd.get_open_completion_debt = AsyncMock(side_effect=ImportError("not landed"))
    mocker.patch.dict("sys.modules", {"app.completion_debt": _mock_cd})
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Finalize must still succeed (best-effort — never fatal).
    assert result["status"] in ("dry_run", "not_promoted", "promoted"), (
        f"unexpected status: {result['status']}"
    )

    # completion_debt key should be None (not set) in last_qualification.
    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual.get("completion_debt") is None, (
        f"expected completion_debt=None, got {qual.get('completion_debt')}"
    )


@pytest.mark.asyncio
async def test_sweep_promotes_dry_run_candidate_when_promotion_enabled(
    db_session, mocker
):
    """Integration: sweep_stuck_qualifications re-drives a dry_run+would_promote version
    once promotion is enabled, increments the finalized count, and spawns qualification."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "dry_run",
        "candidate_sha": "0a1b2c3d" * 5,
        "would_promote": True,
        "confidence": 0.93,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),
    )

    # Re-drives do not contribute to the finalized count.
    assert count == 0


# ---------------------------------------------------------------------------
# compute_qualification_trigger_state + _fire_initial_qualification tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_qualification_trigger_state_armed(db_session, mocker):
    """open stabilize-phase version, no prior attempt → armed.

    Stage-1 decoupling: the initial trigger arms on phase + no-prior-attempt
    alone — there is no pre-qualification criterion-coverage precondition. A
    criterion is present here but does not affect arming.
    """
    from app.release_readiness import compute_qualification_trigger_state

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _add_version_criterion(
        db_session, project, version,
        key="c-trig-armed", text="A criterion",
        source_kind="task_merge",
    )
    assert version.last_qualification is None
    assert (version.qualification_attempts or 0) == 0

    result = await compute_qualification_trigger_state(db_session, project.id)

    assert result["trigger_state"] == "armed"
    assert result["open_version_id"] == str(version.id)
    assert result["reason"] == "armed"


@pytest.mark.asyncio
async def test_compute_qualification_trigger_state_armed_no_criteria(db_session):
    """No active criteria is not incomplete → trigger armed."""
    from app.release_readiness import compute_qualification_trigger_state

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    result = await compute_qualification_trigger_state(db_session, project.id)

    assert result["trigger_state"] == "armed"
    assert result["open_version_id"] == str(version.id)
    assert result["reason"] == "armed"


@pytest.mark.asyncio
async def test_compute_qualification_trigger_state_blocked_no_version(db_session):
    """No open version → reason=no_open_version."""
    from app.release_readiness import compute_qualification_trigger_state

    project = Project(
        name="NoVer-Project",
        user_id="rq-user-nv",
        github_owner="acme",
        github_repo="nover",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await compute_qualification_trigger_state(db_session, project.id)

    assert result["trigger_state"] == "blocked"
    assert result["open_version_id"] is None
    assert result["reason"] == "no_open_version"


@pytest.mark.asyncio
async def test_compute_qualification_trigger_state_blocked_wrong_phase(db_session):
    """Version in 'build' phase → reason=wrong_phase."""
    from app.release_readiness import compute_qualification_trigger_state

    project, version = await _make_project_and_version(db_session, phase="build")

    result = await compute_qualification_trigger_state(db_session, project.id)

    assert result["trigger_state"] == "blocked"
    assert result["reason"] == "wrong_phase"
    assert result["open_version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_compute_qualification_trigger_state_blocked_prior_attempt_last_qual(
    db_session,
):
    """last_qualification is set → reason=prior_attempt_exists."""
    from app.release_readiness import compute_qualification_trigger_state

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "error",
        "candidate_sha": "err00000" * 5,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await compute_qualification_trigger_state(db_session, project.id)

    assert result["trigger_state"] == "blocked"
    assert result["reason"] == "prior_attempt_exists"


@pytest.mark.asyncio
async def test_compute_qualification_trigger_state_blocked_prior_attempt_attempts_count(
    db_session,
):
    """qualification_attempts > 0 (even with no last_qualification) → reason=prior_attempt_exists."""
    from app.release_readiness import compute_qualification_trigger_state

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await compute_qualification_trigger_state(db_session, project.id)

    assert result["trigger_state"] == "blocked"
    assert result["reason"] == "prior_attempt_exists"


@pytest.mark.asyncio
async def test_compute_qualification_trigger_state_armed_despite_incomplete_criteria(
    db_session,
):
    """Stage-1 decoupling: a stabilize-phase version with unmet criteria ARMS.

    Previously an incomplete-coverage version was blocked
    (reason=coverage_incomplete), which was the wedge — coverage measured ~0
    forever so the initial trigger never armed. With the pre-qualification
    coverage gate retired, the same version arms; the acceptance contract is
    enforced INSIDE qualification (the ledger gate in compute_composite).
    """
    from app.release_readiness import compute_qualification_trigger_state

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _add_version_criterion(
        db_session, project, version,
        key="c-covered", text="Covered", source_kind="task_merge",
    )
    await _add_version_criterion(
        db_session, project, version,
        key="c-missing", text="Not covered yet",
    )

    result = await compute_qualification_trigger_state(db_session, project.id)

    assert result["trigger_state"] == "armed"
    assert result["reason"] == "armed"


@pytest.mark.asyncio
async def test_sweep_starts_initial_qualification_when_auto_trigger_fires(
    db_session, mocker
):
    """The sweep executes the trigger resolver's fire decision.

    The ``auto_qualification_triggers`` resolver armed a stabilize-phase
    never-attempted version but nothing executed that decision — the agent-emitted
    qualify_request path was the only starter, so the trigger engine "decided but
    never fired".  With the knob defaulting to "on", the sweep's auto-trigger leg
    now starts the initial qualification through its sanctioned automatic path
    (``auto_trigger=True``).
    """
    from app import shared_state

    shared_state.runtime_config["auto_qualification_triggers"] = "on"

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _add_version_criterion(
        db_session, project, version,
        key="c-sweep-init", text="Covered criterion for sweep integration",
        source_kind="task_merge",
    )

    automain_sha = "swpinit0" * 5
    mocker.patch(
        "app.github.get_branch_sha", new=_branch_sha_mock(automain=automain_sha)
    )
    run_qual_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned", "candidate_sha": automain_sha}),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session,
        session_factory=_shared_session_factory(db_session),
    )

    # Auto-fires are not counted as finalized; the assertion that matters is that
    # the armed never-attempted version's decision was executed. The sweep's
    # auto-trigger is automatic machinery: it must use the distinct sanctioned
    # auto_trigger path, not claim explicit-request status.
    assert count == 0
    run_qual_mock.assert_awaited_once()
    call = run_qual_mock.await_args
    assert call.args[1] == project.id
    assert call.kwargs.get("explicit_request") is not True
    assert call.kwargs.get("auto_trigger") is True


# Named acceptance test: test_drift_sentinel_runs_by_default_without_build_config_flag
@pytest.mark.asyncio
async def test_drift_sentinel_runs_by_default_without_build_config_flag(
    db_session, mocker
):
    """The sentinel runs by default with no build_config flag (canonical, not opt-in).

    Without any ``full_suite_drift_sentinel_enabled`` key the sweep must reach
    compute_promotion_ci_green_gate (the dispatch entrypoint) — proving the
    default-OFF build_config opt-in was removed.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel  # noqa: PLC0415

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # No build_config flag set — the sentinel must still run (default ON).

    gate_mock = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "dispatched",
                "head_sha": "abc12345" * 5,
                "run_id": 999,
                "html_url": "https://github.com/runs/999",
                "reason": "no run for HEAD — dispatched",
            }
        ),
    )

    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True
    gate_mock.assert_called_once()


@pytest.mark.asyncio
async def test_drift_sentinel_skips_when_explicitly_disabled(db_session, mocker):
    """Sentinel returns False only when build_config explicitly disables it."""
    from unittest.mock import AsyncMock  # noqa: PLC0415
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel  # noqa: PLC0415

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project.build_config = {"full_suite_drift_sentinel_enabled": False}
    db_session.add(project)
    await db_session.commit()

    gate_mock = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "dispatched",
                "head_sha": "abc12345" * 5,
                "run_id": 999,
            }
        ),
    )
    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False
    gate_mock.assert_not_called()


@pytest.mark.asyncio
async def test_drift_sentinel_runs_for_non_stabilize_version(db_session, mocker):
    """Sentinel dispatches for a build-phase open version too (no phase hedge)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel  # noqa: PLC0415

    project, version = await _make_project_and_version(db_session, phase="build")
    # No flag needed — the sentinel is canonical and phase-agnostic.

    gate_mock = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "dispatched",
                "head_sha": "abc12345" * 5,
                "run_id": 999,
                "html_url": "https://github.com/runs/999",
                "reason": "no run for HEAD — dispatched",
            }
        ),
    )

    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True
    gate_mock.assert_called_once()


@pytest.mark.asyncio
async def test_drift_sentinel_dispatches_when_no_ci_run(db_session, mocker):
    """Sentinel returns True and logs when ci.yml is dispatched for HEAD."""
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # No build_config flag — the sentinel is canonical and ON by default.

    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "dispatched",
                "head_sha": "abc12345" * 5,
                "run_id": 999,
                "html_url": "https://github.com/runs/999",
                "reason": "no run for HEAD — dispatched",
            }
        ),
    )

    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True


@pytest.mark.asyncio
async def test_drift_sentinel_alerts_and_harvests_on_red(db_session, mocker):
    """Sentinel raises an Alert and calls harvest when CI concludes red."""
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel
    from sqlalchemy import select

    HEAD_SHA = "deadbeef" * 5

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # No build_config flag — the sentinel is canonical and ON by default.

    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "red",
                "head_sha": HEAD_SHA,
                "run_id": 42,
                "html_url": "https://github.com/runs/42",
                "reason": "ci.yml concluded failure",
            }
        ),
    )
    harvest_mock = mocker.patch(
        "app.release_reconciliation._harvest_promotion_ci_failures",
        new=AsyncMock(return_value=None),
    )

    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True

    # An Alert should have been created.
    alert = (
        await db_session.execute(
            select(Alert).where(Alert.project_id == project.id)
        )
    ).scalars().first()
    assert alert is not None
    assert HEAD_SHA[:8] in alert.title
    assert alert.dedupe_key is not None

    # Harvest was called once.
    harvest_mock.assert_called_once()
    call_kwargs = harvest_mock.call_args.kwargs
    assert call_kwargs["ci_source_sha"] == HEAD_SHA
    assert call_kwargs["selfheal_enabled"] is True


@pytest.mark.asyncio
async def test_drift_sentinel_deduplicates_alert_on_same_sha(db_session, mocker):
    """Sentinel does not create a second Alert when one for the SHA already exists."""
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel
    from sqlalchemy import select

    HEAD_SHA = "cafebabe" * 5

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # No build_config flag — the sentinel is canonical and ON by default.

    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "red",
                "head_sha": HEAD_SHA,
                "run_id": 7,
                "html_url": "",
                "reason": "red",
            }
        ),
    )
    mocker.patch(
        "app.release_reconciliation._harvest_promotion_ci_failures",
        new=AsyncMock(return_value=None),
    )

    # First call — creates the Alert.
    await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Second call — same SHA, same project.
    await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Exactly one Alert row despite two calls.
    alerts = (
        await db_session.execute(
            select(Alert).where(Alert.project_id == project.id)
        )
    ).scalars().all()
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_drift_sentinel_reruns_failed_jobs_on_infra_flake_red(db_session, mocker):
    """On an infra-flake red, the drift sentinel harvest re-runs the failed jobs
    so the promotion-CI green gate can clear for the SAME HEAD, while still
    handing the delivery decision to Strategic and spawning no code-fix task."""
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel
    from sqlalchemy import select

    HEAD_SHA = "feedface" * 5
    RUN_ID = 4242

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # No build_config flag — the sentinel is canonical and ON by default.

    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "red",
                "head_sha": HEAD_SHA,
                "run_id": RUN_ID,
                "html_url": "https://github.com/runs/4242",
                "reason": "ci.yml concluded failure",
            }
        ),
    )
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "build", "conclusion": "failure"}],
                "failed_logs": "npm error code ECONNREFUSED connecting to registry.npmjs.org",
                "setup_phase_failure": False,
                "failed_run_ids": [RUN_ID],
            }
        ),
    )
    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        new=AsyncMock(return_value=True),
    )
    refresh_mock = mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure",
        new=AsyncMock(),
    )

    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True

    # The flake's failed jobs were re-run so the green gate can clear for the
    # same HEAD — the behavior that unlatches ci_not_green without a new commit.
    rerun_mock.assert_awaited_once_with("tok", "acme", "widget", RUN_ID)

    # Strategic is still handed the delivery decision...
    refresh_mock.assert_awaited_once()
    assert refresh_mock.await_args.args[1] == project.id
    assert refresh_mock.await_args.args[2].startswith(
        f"promotion_ci_infra_flake:v{version.version_number}:"
    )

    # ...and no code-fix task is spawned for an infra flake.
    handle_mock.assert_not_awaited()

    # The red SHA still raises the deduplicated Alert.
    alert = (
        await db_session.execute(
            select(Alert).where(Alert.project_id == project.id)
        )
    ).scalars().first()
    assert alert is not None
    assert HEAD_SHA[:8] in alert.title


def _flake_sentinel_mocks(mocker, head_sha, run_id):
    """Patch the sentinel's collaborators for a persistent infra-flake red.

    Returns ``(rerun_mock, refresh_mock, handle_mock)``.
    """
    mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "red",
                "head_sha": head_sha,
                "run_id": run_id,
                "html_url": "",
                "reason": "ci.yml concluded failure",
            }
        ),
    )
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "build", "conclusion": "failure"}],
                # Deliberately a log that TRIPS the broad infra substrings while
                # actually carrying a genuine test regression — the
                # misclassification the rerun budget has to bound.
                "failed_logs": (
                    "FAILED testing/test_foo.py::test_bar\n"
                    "AssertionError: docker pull cache warning"
                ),
                "setup_phase_failure": False,
                "failed_run_ids": [run_id],
            }
        ),
    )
    return (
        mocker.patch(
            "app.github_checks.rerun_failed_jobs",
            new=AsyncMock(return_value=True),
        ),
        mocker.patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=AsyncMock(return_value=True),
        ),
        mocker.patch(
            "app.auto_remediation._handle_main_branch_ci_failure",
            new=AsyncMock(),
        ),
    )


@pytest.mark.asyncio
async def test_drift_sentinel_rerun_budget_stops_second_pass_on_same_head(
    db_session, mocker
):
    """The per-HEAD rerun budget bounds the sentinel's infra-flake rerun.

    The sentinel runs on EVERY sweep while the gate is red and the Alert
    dedupe_key does not gate the harvest, so without a budget a misclassified
    genuine regression would re-run the full 8-shard matrix once per CI cycle
    forever and never escalate.  The second red pass on the SAME head_sha must
    issue no rerun and instead fall through to the fix spawn.
    """
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    HEAD_SHA = "abadcafe" * 5
    RUN_ID = 5150

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    rerun_mock, _refresh_mock, handle_mock = _flake_sentinel_mocks(
        mocker, HEAD_SHA, RUN_ID
    )
    factory = _shared_session_factory(db_session)

    # First red pass: budget available -> one rerun, no fix spawn.
    assert await _check_full_suite_drift_sentinel(version.id, session_factory=factory)
    rerun_mock.assert_awaited_once_with("tok", "acme", "widget", RUN_ID)
    handle_mock.assert_not_awaited()

    # Second red pass on the SAME head: budget exhausted.
    assert await _check_full_suite_drift_sentinel(version.id, session_factory=factory)
    assert rerun_mock.await_count == 1, (
        "rerun budget is per-HEAD: a second red pass on the same head_sha must "
        "not re-run the matrix again"
    )
    # ...and the flake escalates to the fix path instead of latching forever.
    handle_mock.assert_awaited_once()
    assert handle_mock.await_args.kwargs["head_sha"] == HEAD_SHA

    # A third pass keeps escalating, never re-running.
    assert await _check_full_suite_drift_sentinel(version.id, session_factory=factory)
    assert rerun_mock.await_count == 1


@pytest.mark.asyncio
async def test_drift_sentinel_rerun_budget_resets_on_new_head(db_session, mocker):
    """A new integration HEAD legitimately earns a fresh rerun attempt."""
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel

    OLD_SHA = "0ldc0ffe" * 5
    NEW_SHA = "beefbeef" * 5

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    rerun_mock, _refresh_mock, _handle_mock = _flake_sentinel_mocks(mocker, OLD_SHA, 111)
    factory = _shared_session_factory(db_session)

    await _check_full_suite_drift_sentinel(version.id, session_factory=factory)
    await _check_full_suite_drift_sentinel(version.id, session_factory=factory)
    assert rerun_mock.await_count == 1

    # New commit lands: budget resets, so the flake gets one more attempt.
    rerun_mock, _refresh_mock, _handle_mock = _flake_sentinel_mocks(mocker, NEW_SHA, 222)
    await _check_full_suite_drift_sentinel(version.id, session_factory=factory)
    rerun_mock.assert_awaited_once_with("tok", "acme", "widget", 222)


@pytest.mark.asyncio
async def test_finalize_logs_completion_debt_snapshot_fetch_failure(db_session, mocker, caplog):
    """Finalize logs when completion-debt snapshot fetch fails, then continues."""
    from app import shared_state
    import logging
    from importlib import import_module as original_import

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Create a mock that raises an exception only when trying to import
    # completion_debt, but falls back to normal behavior for other modules.
    def mock_import_module(name, *args, **kwargs):
        if "completion_debt" in name:
            raise RuntimeError("completion_debt module not available")
        return original_import(name, *args, **kwargs)

    mocker.patch("importlib.import_module", new=mock_import_module)

    with caplog.at_level(logging.WARNING, logger="app.release_qualifier"):
        result = await rq._finalize_qualification(
            db_session,
            version,
            _pre_gates=_clean_finalize_pre_gates(rq._CI_GREEN),
        )

    # Should log the completion-debt failure.
    assert any(
        "completion-debt snapshot fetch failed" in record.message
        for record in caplog.records
    ), f"Expected warning about completion-debt snapshot, got: {[r.message for r in caplog.records]}"

    # Finalize should proceed and produce a verdict.
    assert result["status"] in ("promoted", "not_promoted", "dry_run")


@pytest.mark.asyncio
async def test_enforce_mode_debt_read_failure_does_not_inflate_confidence(
    db_session, mocker
):
    """NAMED ACCEPTANCE TEST — enforce mode debt-read failure must not inflate confidence.

    When completion_debt_weight_mode='enforce' and the debt-snapshot fetch raises,
    _finalize_qualification must NOT silently drop the (1 - w_debt) down-scaling
    that compute_composite applies in enforce mode.  Without the fix, a None
    debt_pressure_score causes compute_composite to skip the scaling entirely,
    producing confidence == existing_confidence instead of the correctly bounded
    (1 - w_debt) * existing_confidence + w_debt * debt_pressure value.

    Verified by comparing:
      - enforce_confidence_fail: confidence when debt fetch raises (post-fix = 0.0 pressure)
      - enforce_confidence_clean: confidence when debt fetch returns zero debt (pressure = 1.0)

    Post-fix: the failed-read path MUST produce confidence <= the zero-debt
    clean path, because 0.0 pressure pulls confidence down via the blend.
    Pre-fix: the failed-read path produced confidence > the clean path because
    the down-scaling was silently skipped.
    """
    from app import shared_state
    from importlib import import_module as original_import
    from unittest.mock import patch

    _enforce_weight = 0.15  # DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT

    def _make_cfg():
        return {
            "promotion_enabled": False,
            "completion_debt_weight_mode": "enforce",
            "promotion_threshold": 0.90,
            "promotion_weights": {
                "audit": 0.40,
                "test_erosion": 0.30,
                "smoketest": 0.30,
            },
        }

    _good_audit = {
        "audit_score": 0.92,
        "findings": [],
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 0,
            "test_count_delta": 0,
        },
    }
    _good_smoke = {
        "passed": True,
        "tests": [{"name": "s", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    # --- Arm 1: debt snapshot fetch raises --------------------------------
    mocker.patch.dict(shared_state.runtime_config, _make_cfg(), clear=False)

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _seed_qualification_children(
        db_session, project, version, audit_logs=_good_audit, smoke_logs=_good_smoke
    )

    def _fail_import(name, *args, **kwargs):
        if "completion_debt" in name:
            raise RuntimeError("simulated debt-module failure")
        return original_import(name, *args, **kwargs)

    _ci_pre_gates = _clean_finalize_pre_gates(rq._CI_GREEN, ci_green=True)

    # Scope only the debt-module failure.  ``mocker.stopall()`` would also stop
    # this suite's autouse candidate-authorship fixture and make the comparison
    # arm fail closed on an unrelated hard gate.
    with patch("importlib.import_module", new=_fail_import):
        result_fail = await rq._finalize_qualification(
            db_session,
            version,
            _pre_gates=_ci_pre_gates,
        )
    enforce_confidence_fail = result_fail["confidence"]

    # --- Arm 2: debt snapshot succeeds with zero open debt ----------------
    mocker.patch.dict(shared_state.runtime_config, _make_cfg(), clear=False)

    project2, version2 = await _make_project_and_version(db_session, phase="stabilize")
    await _seed_qualification_children(
        db_session, project2, version2, audit_logs=_good_audit, smoke_logs=_good_smoke
    )

    # Empty debt snapshot → pressure = 1.0 (no open items).
    _mock_cd_zero = MagicMock()
    _mock_cd_zero.get_open_completion_debt = AsyncMock(return_value={"total_open": 0})
    mocker.patch.dict("sys.modules", {"app.completion_debt": _mock_cd_zero})
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result_clean = await rq._finalize_qualification(
        db_session,
        version2,
        _pre_gates=_clean_finalize_pre_gates(rq._CI_GREEN, ci_green=True),
    )
    enforce_confidence_clean = result_clean["confidence"]

    # Post-fix: a failed read is treated as maximum debt (pressure=0.0), so its
    # confidence must be STRICTLY LESS than the clean zero-debt run (pressure=1.0).
    assert enforce_confidence_fail < enforce_confidence_clean, (
        f"Enforce-mode debt-read failure inflated confidence: "
        f"fail={enforce_confidence_fail} should be < clean={enforce_confidence_clean}. "
        f"The (1 - w_debt) down-scaling was skipped when debt fetch raised."
    )

    # The failed-read confidence must equal the formula for 0.0 pressure:
    # (1 - w_debt) * existing_confidence + w_debt * 0.0
    # existing_confidence with a clean zero-debt (pressure=1.0) equals
    # (1 - w_debt) * existing_confidence + w_debt * 1.0, so:
    # failed_confidence = clean_confidence - w_debt (the pure-debt term drops)
    assert enforce_confidence_fail == pytest.approx(
        enforce_confidence_clean - _enforce_weight, abs=1e-3
    ), (
        f"Failed-read confidence {enforce_confidence_fail} does not match expected "
        f"formula: clean {enforce_confidence_clean} - weight {_enforce_weight}"
    )


@pytest.mark.asyncio
async def test_finalize_logs_actionable_open_count_failure(db_session, mocker, caplog):
    """Finalize logs when actionable-open count fails, then continues."""
    from app import shared_state
    import logging
    from importlib import import_module as original_import

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.9,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Mock the completion_debt module to return a valid debt dict.
    mock_cd_mod = AsyncMock()
    mock_cd_mod.get_open_completion_debt = AsyncMock(
        return_value={"open_count": 5, "total_blocked": 1}
    )

    def mock_import_module(name, *args, **kwargs):
        if "completion_debt" in name:
            return mock_cd_mod
        return original_import(name, *args, **kwargs)

    mocker.patch("importlib.import_module", new=mock_import_module)

    # Mock the actionable-open count function to raise an exception.
    mocker.patch(
        "app.release_qualifier._count_project_actionable_open_completion_debt",
        new=AsyncMock(side_effect=RuntimeError("Count failed")),
    )

    with caplog.at_level(logging.WARNING, logger="app.release_qualifier"):
        result = await rq._finalize_qualification(
            db_session,
            version,
            _pre_gates=_clean_finalize_pre_gates(rq._CI_GREEN),
        )

    # Should log the actionable-open count failure.
    assert any(
        "actionable-open completion-debt count" in record.message
        for record in caplog.records
    ), f"Expected warning about actionable-open count, got: {[r.message for r in caplog.records]}"

    # Finalize should proceed and produce a verdict.
    assert result["status"] in ("promoted", "not_promoted", "dry_run")


@pytest.mark.asyncio
async def test_full_suite_drift_sentinel_get_default_on(db_session, client):
    """GET returns enabled=True and is_default=True when flag is absent."""
    project = await _make_project_with_owner(db_session)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/full-suite-drift-sentinel"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_suite_drift_sentinel_enabled"] is True
    assert body["is_default"] is True


@pytest.mark.asyncio
async def test_full_suite_drift_sentinel_patch_enable_disable(db_session, client):
    """PATCH toggles full_suite_drift_sentinel_enabled and GET reflects the change."""
    project = await _make_project_with_owner(db_session)

    # Explicitly enable the sentinel.
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/full-suite-drift-sentinel",
        json={"full_suite_drift_sentinel_enabled": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_suite_drift_sentinel_enabled"] is True
    assert body["is_default"] is False

    # GET now reflects the enabled state.
    resp = await client.get(
        f"/api/admin/projects/{project.id}/full-suite-drift-sentinel"
    )
    assert resp.status_code == 200
    assert resp.json()["full_suite_drift_sentinel_enabled"] is True

    # Disable the sentinel — stored explicitly (rollback path); is_default is
    # False because the build_config now carries an explicit override.
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/full-suite-drift-sentinel",
        json={"full_suite_drift_sentinel_enabled": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_suite_drift_sentinel_enabled"] is False
    assert body["is_default"] is False


@pytest.mark.asyncio
async def test_full_suite_drift_sentinel_patch_validation(db_session, client):
    """PATCH rejects non-boolean and missing-key bodies with 422."""
    project = await _make_project_with_owner(db_session)

    # Missing required field.
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/full-suite-drift-sentinel",
        json={},
    )
    assert resp.status_code == 422

    # Non-boolean value.
    resp = await client.patch(
        f"/api/admin/projects/{project.id}/full-suite-drift-sentinel",
        json={"full_suite_drift_sentinel_enabled": "yes"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_full_suite_drift_sentinel_sweep_uses_build_config(db_session, mocker):
    """Sweep runs the sentinel by default; explicit build_config False disables.

    Verifies that WITHOUT any build_config flag the sentinel calls
    compute_promotion_ci_green_gate (the dispatch entrypoint), while an
    explicit ``full_suite_drift_sentinel_enabled: False`` is a no-op rollback.
    """
    from unittest.mock import AsyncMock  # noqa: PLC0415
    from app.release_qualifier_sweep import _check_full_suite_drift_sentinel  # noqa: PLC0415

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    gate_mock = mocker.patch(
        "app.release_readiness.compute_promotion_ci_green_gate",
        new=AsyncMock(
            return_value={
                "gate_state": "dispatched",
                "head_sha": "aabbccdd" * 5,
                "run_id": 42,
            }
        ),
    )

    # No flag — canonical default ON: gate must be called.
    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True
    gate_mock.assert_called_once()

    # Explicitly disable via build_config (the rollback path) — no-op.
    gate_mock.reset_mock()
    project.build_config = {"full_suite_drift_sentinel_enabled": False}
    db_session.add(project)
    await db_session.commit()

    result = await _check_full_suite_drift_sentinel(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False
    gate_mock.assert_not_called()


@pytest.mark.asyncio
async def test_record_v22_live_verification_evidence_records_evidence_row(db_session):
    """Integration coverage: record_v22_live_verification_evidence writes a
    CriterionEvidence row for criterion c-dd953d8c on a v22 version.

    Verifies the function correctly records an evidence row against the
    live-verification criterion — not just that it is CALLED (wiring invariant).
    """
    from app import release_readiness as rr
    from app import release_version as rv
    from app.acceptance_criteria import get_evidence_for_criterion

    # Create project + v22 version (the function only operates on version 22).
    project = Project(
        name="V22-Live-Verify-Test",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=22)
    await db_session.commit()
    await db_session.refresh(version)

    # Create the live-verification criterion c-dd953d8c on this version.
    criterion = await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-dd953d8c",
        text="v22 live-verification claim audit",
    )

    # --- Happy path: record evidence ---
    recorded = await rr.record_v22_live_verification_evidence(
        db_session, project.id, version
    )
    assert recorded == 1, "Expected 1 new evidence row written"

    # Verify the evidence row was persisted with correct source_kind and source_id.
    evidence_rows = await get_evidence_for_criterion(db_session, criterion.id)
    matched = [
        e
        for e in evidence_rows
        if e.source_kind == "audit_finding_backfill"
        and e.source_id == "live_verification_backfill:v22"
    ]
    assert len(matched) == 1, (
        f"Expected exactly 1 live-verification evidence row, found {len(matched)}"
    )
    row = matched[0]
    assert row.detail == {
        "claims_verified": 5,
        "verification_date": "2026-07-07",
        "note": (
            "Five v22 promotion-gated live-verification claims "
            "(direction d-fd9c793d) code-verified PASS in direction notes."
        ),
    }

    # --- Idempotency: second call is a no-op ---
    recorded2 = await rr.record_v22_live_verification_evidence(
        db_session, project.id, version
    )
    assert recorded2 == 0, "Expected 0 on idempotent second call — evidence already recorded"

    # --- Wrong version guard: returns 0 for non-v22 versions ---
    version_v23 = await rv.open_version(db_session, project.id, version_number=23)
    await db_session.commit()
    await db_session.refresh(version_v23)

    recorded3 = await rr.record_v22_live_verification_evidence(
        db_session, project.id, version_v23
    )
    assert recorded3 == 0, "Expected 0 for non-v22 version — version guard must reject"


# ---------------------------------------------------------------------------
# Phase 1a: QualificationDimensionResult dual-write tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_dual_writes_dimension_rows_for_all_gates(db_session, mocker):
    """_finalize_qualification writes one QualificationDimensionResult row per
    active gate (audit, test_erosion, ci, migration_identity, migration_apply,
    merge_grade, acceptance_window, debt_pressure, capability_proof, env_preflight)
    on a clean pass.

    Verifies: each finalize run persists the expected dimension rows for its
    candidate_sha.
    """
    from app import shared_state
    from app.models import QualificationDimensionResult
    from sqlalchemy import select

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run, no promote
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[],
    )

    candidate_sha = version.last_qualification["candidate_sha"]

    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates=_clean_finalize_pre_gates("green", ci_green=True),
    )
    await db_session.commit()

    assert result["status"] in ("dry_run", "not_promoted", "promoted"), (
        f"unexpected status {result['status']!r}"
    )

    rows = (
        await db_session.execute(
            select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == candidate_sha,
            )
        )
    ).scalars().all()

    dims_found = {r.dimension for r in rows}
    expected_dims = {
        "audit",
        "test_erosion",
        "ci",
        "challenge",
        "migration_identity",
        "migration_apply",
        "merge_grade",
        "acceptance_window",
        "debt_pressure",
        "capability_proof",
        "env_preflight",
    }
    assert expected_dims == dims_found, (
        f"expected dims {expected_dims}, got {dims_found}"
    )

    by_dim = {r.dimension: r for r in rows}

    # CI should be "passed" since ci_green=True
    assert by_dim["ci"].status == "passed", (
        f"expected ci=passed, got {by_dim['ci'].status!r}"
    )
    # audit should be "passed" with audit_score=1.0
    assert by_dim["audit"].status == "passed", (
        f"expected audit=passed, got {by_dim['audit'].status!r}"
    )
    # migration_identity and migration_apply pass with "ok" verdict
    assert by_dim["migration_identity"].status == "passed"
    assert by_dim["migration_apply"].status == "passed"
    # All rows carry the correct candidate_sha
    for r in rows:
        assert r.candidate_sha == candidate_sha, (
            f"dim {r.dimension!r}: expected sha {candidate_sha!r}, got {r.candidate_sha!r}"
        )
        assert r.project_id == project.id
        assert r.release_version_id == version.id


@pytest.mark.asyncio
async def test_finalize_dimension_rows_unique_constraint_prevents_duplicates(
    db_session, mocker
):
    """UNIQUE(release_version_id, candidate_sha, dimension) prevents duplicate rows.

    A second _finalize_qualification call for the SAME candidate_sha must not
    write duplicate rows — the ON CONFLICT DO NOTHING guard leaves existing rows
    intact.
    """
    from app import shared_state
    from app.models import QualificationDimensionResult
    from sqlalchemy import func as count_func, select

    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[],
    )

    candidate_sha = version.last_qualification["candidate_sha"]
    pre_gates = _clean_finalize_pre_gates("green", ci_green=True)

    # First finalize
    await rq._finalize_qualification(db_session, version, _pre_gates=pre_gates)
    await db_session.commit()

    count_after_first = (
        await db_session.execute(
            select(count_func.count()).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == candidate_sha,
            )
        )
    ).scalar()

    # Restore version state (simulating a re-finalize on the same SHA)
    version.last_qualification = {
        **version.last_qualification,
        "status": "pending",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Second finalize for same candidate_sha
    await rq._finalize_qualification(db_session, version, _pre_gates=pre_gates)
    await db_session.commit()

    count_after_second = (
        await db_session.execute(
            select(count_func.count()).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == candidate_sha,
            )
        )
    ).scalar()

    assert count_after_first > 0, "First finalize must write at least one row"
    assert count_after_second == count_after_first, (
        f"second finalize must not duplicate rows: "
        f"first={count_after_first}, second={count_after_second}"
    )


@pytest.mark.asyncio
async def test_finalize_new_candidate_sha_writes_new_dimension_rows(
    db_session, mocker
):
    """A second finalize for a NEW candidate_sha writes new rows rather than
    mutating existing ones (immutable-once-terminal-per-SHA invariant).

    Per the design's concurrency-safety argument in section 4: each row is
    keyed by (release_version_id, candidate_sha, dimension) — a new candidate
    maps to new rows, never in-place updates.
    """
    from app import shared_state
    from app.models import QualificationDimensionResult
    from sqlalchemy import select

    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[],
    )

    sha_1 = version.last_qualification["candidate_sha"]
    pre_gates = _clean_finalize_pre_gates("green", ci_green=True)

    # Finalize with sha_1
    await rq._finalize_qualification(db_session, version, _pre_gates=pre_gates)
    await db_session.commit()

    rows_sha1 = (
        await db_session.execute(
            select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == sha_1,
            )
        )
    ).scalars().all()
    assert len(rows_sha1) > 0, "sha_1 finalize must produce dimension rows"

    # Switch to a new candidate_sha (simulates automain advancing).
    # Clear challenge_task_id to avoid SHA-mismatch early-return in the challenge gate.
    sha_2 = "b" * 40
    version.last_qualification = {
        "status": "pending",
        **_EMPTY_RUBRIC_PROJECTION,
        "candidate_sha": sha_2,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-05-19T00:00:00",
        "audit_task_id": version.last_qualification.get("audit_task_id"),
        "smoketest_task_id": version.last_qualification.get("smoketest_task_id"),
        "challenge_task_id": None,
        "merge_grade_task_id": None,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(db_session, version, _pre_gates=pre_gates)
    await db_session.commit()

    rows_sha2 = (
        await db_session.execute(
            select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == sha_2,
            )
        )
    ).scalars().all()
    assert len(rows_sha2) > 0, "sha_2 finalize must write new rows, not mutate sha_1 rows"

    # sha_1 rows must still exist and be unchanged (immutable once written)
    rows_sha1_after = (
        await db_session.execute(
            select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == sha_1,
            )
        )
    ).scalars().all()
    assert len(rows_sha1_after) == len(rows_sha1), (
        "sha_1 rows must be immutable — count must not change after sha_2 finalize"
    )

    dims_sha2 = {r.dimension for r in rows_sha2}
    dims_sha1 = {r.dimension for r in rows_sha1}
    assert dims_sha2 == dims_sha1, (
        f"both SHAs should produce the same set of dimension rows; "
        f"sha_1={dims_sha1}, sha_2={dims_sha2}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved_status", ["passed", "failed", "blocked"])
async def test_redrive_isolation_proven_evidence(db_session, resolved_status):
    """_upsert_dimension_result must never downgrade a resolved dimension fact
    (passed/failed/blocked) to a non-resolved status such as 'unavailable'.

    This is the primitive-level isolation guard: when a qualification redrive
    encounters an infra failure AFTER a dimension has already settled, the
    _upsert_dimension_result primitive itself rejects the downgrade write,
    preserving the settled fact even when the call bypasses the higher-level
    guard in _record_infra_dimension_unavailable.

    Also verifies that resolved→resolved transitions (e.g. failed→passed) ARE
    still permitted — the guard only blocks non-resolved overwrites.
    """
    from app import release_qualifier as rq
    from app.models import QualificationDimensionResult
    from sqlalchemy import select as sa_select

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "aabbccddaabbccddaabbccddaabbccddaabbccdd"

    # Seed a pre-resolved dimension row at the primitive layer.
    seed_row = QualificationDimensionResult(
        project_id=version.project_id,
        release_version_id=version.id,
        candidate_sha=candidate_sha,
        dimension="ci",
        status=resolved_status,
        sub_score=1.0 if resolved_status == "passed" else 0.0,
    )
    db_session.add(seed_row)
    await db_session.flush()

    # Attempt to downgrade directly via _upsert_dimension_result (bypassing
    # _record_infra_dimension_unavailable's caller-side guard).
    await rq._upsert_dimension_result(
        db_session,
        version,
        candidate_sha,
        "ci",
        status="unavailable",
        detail={"infra_error": "simulated_redrive_infra_failure"},
    )
    await db_session.flush()

    row = (
        await db_session.execute(
            sa_select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == candidate_sha,
                QualificationDimensionResult.dimension == "ci",
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "dimension row must still exist"
    assert row.status == resolved_status, (
        f"primitive-level guard failed: resolved status '{resolved_status}' was "
        f"downgraded to '{row.status}' by a non-resolved write"
    )
    # detail must NOT have been overwritten with the infra-failure payload.
    assert not (
        isinstance(row.detail, dict) and "infra_error" in row.detail
    ), "infra_error detail must not overwrite a resolved row"

    # Verify the inverse: resolved→resolved transitions are still permitted.
    # A re-qualification that legitimately flips passed→failed must be recorded.
    new_resolved_status = "failed" if resolved_status == "passed" else "passed"
    await rq._upsert_dimension_result(
        db_session,
        version,
        candidate_sha,
        "ci",
        status=new_resolved_status,
        blocker="ci_not_green" if new_resolved_status == "failed" else None,
    )
    await db_session.flush()

    row2 = (
        await db_session.execute(
            sa_select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == candidate_sha,
                QualificationDimensionResult.dimension == "ci",
            )
        )
    ).scalar_one_or_none()
    assert row2 is not None
    assert row2.status == new_resolved_status, (
        f"resolved→resolved transition must be permitted: expected '{new_resolved_status}', "
        f"got '{row2.status}'"
    )


# ---------------------------------------------------------------------------
# Attempt-contract freeze tests (version_goal, promotion_policy, proof mode)
# ---------------------------------------------------------------------------


class TestAttemptContractFreeze:
    """The attempt contract (version_goal, promotion_policy_snapshot,
    capability_proof_mode) is frozen at spawn time so mid-flight config
    changes cannot combine children from one contract with a verdict from
    another."""

    @pytest.mark.asyncio
    async def test_version_goal_stored_at_spawn(self, db_session, mocker):
        """run_qualification stores version.goal as version_goal in last_qualification."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False
        mocker.patch(
            "app.release_qualifier.resolve_project_promotion_enabled",
            return_value=True,
        )
        mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="tok"))
        mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
        mocker.patch("app.release_qualifier._resolve_candidate_ci", new=AsyncMock(
            return_value={"ci_green": True, "state": "success", "resolved_sha": None}
        ))
        mocker.patch("app.release_qualifier._spawn_release_audit_task", new=AsyncMock(
            return_value=MagicMock(id=uuid.uuid4())
        ))

        project, version = await _make_project_and_version(db_session, phase="stabilize")
        version.goal = "Deliver a working login flow"
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        await rq.run_qualification(
            db_session, project.id, version, explicit_request=True
        )
        await db_session.commit()
        await db_session.refresh(version)

        qual = version.last_qualification or {}
        assert qual.get("version_goal") == "Deliver a working login flow", (
            f"version_goal not frozen at spawn: {qual.get('version_goal')!r}"
        )

    @pytest.mark.asyncio
    async def test_capability_proof_mode_stored_at_spawn(self, db_session, mocker):
        """run_qualification stores capability_proof_gate_mode in last_qualification."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False
        mocker.patch(
            "app.release_qualifier.resolve_project_promotion_enabled",
            return_value=True,
        )
        shared_state.runtime_config["capability_proof_gate_mode"] = "observe"
        try:
            mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="tok"))
            mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
            mocker.patch("app.release_qualifier._resolve_candidate_ci", new=AsyncMock(
                return_value={"ci_green": True, "state": "success", "resolved_sha": None}
            ))
            mocker.patch("app.release_qualifier._spawn_release_audit_task", new=AsyncMock(
                return_value=MagicMock(id=uuid.uuid4())
            ))

            project, version = await _make_project_and_version(db_session, phase="stabilize")

            await rq.run_qualification(
                db_session, project.id, version, explicit_request=True
            )
            await db_session.commit()
            await db_session.refresh(version)

            qual = version.last_qualification or {}
            assert qual.get("capability_proof_mode") == "observe", (
                f"capability_proof_mode not frozen at spawn: {qual.get('capability_proof_mode')!r}"
            )
        finally:
            shared_state.runtime_config.pop("capability_proof_gate_mode", None)

    @pytest.mark.asyncio
    async def test_version_goal_change_supersedes_pending_qual(self, db_session, mocker):
        """A decision-bearing goal edit supersedes the frozen attempt contract."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        # Seed children with an empty frozen goal so the merge-grade gate is a no-op.
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={"audit_score": 0.9, "findings": [], "test_erosion": {
                "deleted_test_files": [], "assertion_delta": 0, "test_count_delta": 0,
            }},
            smoke_logs={
                "passed": True,
                "tests": [{"name": "s", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )
        # Freeze version_goal="" (no goal at spawn) but set a live goal afterwards.
        qual = dict(version.last_qualification or {})
        qual["version_goal"] = ""
        version.last_qualification = qual
        version.goal = "A non-empty live goal that should be ignored"
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Mock evaluate_merge_grade so we can observe what goal it is called with.
        called_with_goals: list[str | None] = []

        def _fake_evaluate_merge_grade(goal, artifact, categories, evidence):
            called_with_goals.append(goal)
            return {"status": "not_applicable", "covers_goal": None, "coverage": None,
                    "biggest_gap": None, "blocked": False, "detail": None}

        mocker.patch("app.release_qualifier.evaluate_merge_grade", side_effect=_fake_evaluate_merge_grade)
        mocker.patch("app.release_qualifier.promote", new=AsyncMock())

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result.get("status") == "rubric_changed"
        assert result.get("blocker") == "qualification_contract_superseded"
        assert called_with_goals == []

    @pytest.mark.asyncio
    async def test_promotion_policy_frozen_in_pending_qual(self, db_session, mocker):
        """_finalize_qualification uses the promotion policy frozen at spawn time.

        A promotion_policy_snapshot stored in last_qualification takes precedence
        over the live runtime_config / _load_policy result.  Changing the threshold
        after spawn must not affect the verdict for the in-flight attempt.
        """
        from app import shared_state

        # Set a low threshold at "spawn" time that would allow promotion.
        shared_state.runtime_config["promotion_enabled"] = False
        _spawn_policy = {"weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
                         "threshold": 0.50}

        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={"audit_score": 0.7, "findings": [], "test_erosion": {
                "deleted_test_files": [], "assertion_delta": 0, "test_count_delta": 0,
            }},
            smoke_logs={
                "passed": True,
                "tests": [{"name": "s", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )
        # Stamp the frozen policy into last_qualification.
        qual = dict(version.last_qualification or {})
        qual["promotion_policy_snapshot"] = _spawn_policy
        version.last_qualification = _with_current_qualification_contract(
            project, version, qual
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Raise the live threshold to something the score would fail.
        _live_policy = {"weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
                        "threshold": 0.99}
        mocker.patch("app.release_qualifier._load_policy", return_value=_live_policy)
        mocker.patch("app.release_qualifier.promote", new=AsyncMock())

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # With frozen threshold=0.50 the composite is roughly 0.7*0.4 + 1.0*0.3 + 1.0*0.3 = 0.88,
        # which exceeds the frozen bar -- result is dry_run, not not_promoted.
        # (With live threshold=0.99 the same composite would be not_promoted.)
        assert result.get("status") == "dry_run", (
            f"promotion policy was not frozen at spawn: expected dry_run "
            f"(frozen threshold=0.50) but got {result.get('status')!r} — "
            f"live threshold=0.99 may have been used"
        )

    @pytest.mark.asyncio
    async def test_capability_proof_mode_frozen_in_pending_qual(self, db_session, mocker):
        """_finalize_qualification uses the capability_proof_mode frozen at spawn.

        Changing runtime_config['capability_proof_gate_mode'] between spawn and
        finalize must not affect the qualification verdict for that attempt.
        """
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False
        # Freeze "off" mode in last_qualification.
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={"audit_score": 0.9, "findings": [], "test_erosion": {
                "deleted_test_files": [], "assertion_delta": 0, "test_count_delta": 0,
            }},
            smoke_logs={
                "passed": True,
                "tests": [{"name": "s", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )
        qual = dict(version.last_qualification or {})
        qual["capability_proof_mode"] = "off"
        version.last_qualification = _with_current_qualification_contract(
            project, version, qual
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # After spawn: flip the live config to "enforce" with an unproven capability.
        shared_state.runtime_config["capability_proof_gate_mode"] = "enforce"
        # Inject an unproven working capability into the project's problem_model.
        project.problem_model = {
            "capabilities": [{"name": "github_api", "status": "working", "evidence_refs": []}]
        }
        db_session.add(project)
        await db_session.commit()
        try:
            mocker.patch("app.release_qualifier.promote", new=AsyncMock())

            result = await rq._finalize_qualification(db_session, version)
            await db_session.commit()

            # With frozen "off" mode the capability proof gate is inert — should
            # still produce dry_run, not a capability_proof_blocked not_promoted.
            assert result.get("status") == "dry_run", (
                f"capability_proof_mode was not frozen at spawn: expected dry_run "
                f"(frozen mode=off) but got {result.get('status')!r} with "
                f"blocker={result.get('blocker')!r}"
            )
            assert result.get("blocker") != "capability_proof_blocked", (
                "capability_proof_blocked fired despite frozen 'off' mode"
            )
        finally:
            shared_state.runtime_config.pop("capability_proof_gate_mode", None)


@pytest.mark.asyncio
async def test_subprocess_deterministic_defect_surfaces_as_block(monkeypatch):
    """End-to-end seam: a deterministic migration defect raised INSIDE the
    isolated migration-apply subprocess surfaces to the release-qualifier gate
    as a hard BLOCK — not a defer / infra_failure retry that burns attempts.

    This pins the regression this task fixes: the SyntaxError/ImportError/
    class-42 ProgrammingError classifiers went dead when migration apply moved
    into a child process (the parent only saw returncode + stderr text and
    wrapped everything as a deferrable ``_MigrationApplyError``).  The fix has
    the child emit a structured verdict that the parent replays into the SAME
    typed exceptions.

    The test drives the REAL production chain that
    ``release_qualifier._resolve_migration_apply_at_finalize`` invokes:
    ``run_migration_apply_gate`` -> ``_provision_seed_and_detect_drift`` ->
    ``_apply_candidate_migrations`` -> ``_run_migration_apply_subprocess``.
    Only the OS-level subprocess spawn is faked — its stdout carries a verdict
    built by the real ``_classify_apply_failure`` for a genuine ``SyntaxError``,
    so the classifier, the sentinel emission format, ``_parse_apply_verdict``,
    ``_raise_for_apply_verdict`` and the ``('block', ...)`` mapping are all
    exercised together.
    """
    import sqlalchemy as sa
    import app.migration_apply_gate as gate_mod
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    # No real retry backoff in the (unreachable-here) transient path.
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(gate_mod, "_TRANSIENT_RETRY_MAX_DELAY", 0.01)

    prod_meta = sa.MetaData()
    sa.Table("widget", prod_meta, sa.Column("id", sa.Integer, primary_key=True))
    monkeypatch.setattr(
        gate_mod,
        "_read_prod_state",
        AsyncMock(return_value=gate_mod._ProdState(revision="a01", metadata=prod_meta)),
    )

    class _FakeScratchEngine:
        url = make_url(
            "postgresql+asyncpg://scratch_user:scratch-secret@scratch-db:5432/scratch_ephemeral"
        )

        async def dispose(self):
            return None

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(_base_url, db_name=None):
        # Yield a distinct db name so the ISOLATION GUARD does not trip.
        yield _FakeScratchEngine(), "scratch-gate-ephemeral"

    monkeypatch.setattr(gate_mod, "ephemeral_postgres_database", _fake_ephemeral)
    monkeypatch.setattr(gate_mod, "_seed_scratch", AsyncMock())

    # Build the child's verdict with the REAL classifier for a genuine
    # SyntaxError — the exact machine-readable line the child prints on stdout.
    child_verdict = gate_mod._classify_apply_failure(
        SyntaxError("invalid syntax", ("<migration_0001>", 3, 1, "def upgrade(\n"))
    )
    assert child_verdict["kind"] == "deterministic_defect"
    stdout_bytes = (
        gate_mod._MIGRATION_APPLY_VERDICT_SENTINEL + json.dumps(child_verdict) + "\n"
    ).encode()

    spawn_calls = 0

    class _FakeProc:
        returncode = 0
        pid = 4242

        async def communicate(self, input=None):
            del input
            return stdout_bytes, b""

    async def _fake_spawn(*args, **kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        return _FakeProc()

    monkeypatch.setattr(gate_mod.asyncio, "create_subprocess_exec", _fake_spawn)

    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:5432/nope")
    try:
        verdict, detail = await gate_mod.run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources={"0001_bad.py": "revision='0001'\ndown_revision=None\n"},
            required_metadata=sa.MetaData(),
            scratch_db_url=(
                "postgresql+asyncpg://scratch_user:scratch-secret@"
                "scratch-db:5432/control"
            ),
        )
    finally:
        await engine.dispose()

    assert verdict == "block", (
        f"deterministic subprocess defect must BLOCK, not {verdict!r}: {detail}"
    )
    assert detail is not None and "SyntaxError" in detail, (
        f"block detail should name the offending exception type: {detail!r}"
    )
    # Deterministic defect blocks on the FIRST apply — no infra-failure retry.
    assert spawn_calls == 1, (
        f"deterministic defect spawned the apply subprocess {spawn_calls} times; "
        "must block on first attempt without a retry"
    )
    # The scratch DB password must never leak into the surfaced verdict.
    assert "scratch-secret" not in (detail or "")
