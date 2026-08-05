# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: sweep_stuck_qualifications — defense-in-depth reaper

Split from test_release_qualifier.py."""
from _release_qualifier_testhelpers import *  # noqa: F403


def _patch_exact_promotion_pr(
    mocker,
    *,
    pr_number: int,
    candidate: str | None = None,
    merge_sha: str | None = None,
    merged: bool = False,
    discover: bool = False,
    base_ref: str = "main",
):
    """Patch one deterministic release PR with an exact head/base identity.

    ``candidate=None`` derives the SHA from ``create_branch`` after the normal
    promotion flow cuts the release branch.  Crash-recovery fixtures that make
    the PR discoverable before branch creation must pass ``candidate``.
    """
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=([{"number": pr_number}] if discover else [])),
    )

    async def _get_pull_request(_token, _owner, _repo, number, **_kwargs):
        from app import github as _github

        resolved_candidate = candidate
        if resolved_candidate is None:
            branch_call = _github.create_branch.call_args
            assert branch_call is not None, "candidate must be known before PR lookup"
            resolved_candidate = branch_call.args[4]
        return {
            "number": number,
            "state": "closed" if merged else "open",
            "merged": merged,
            "html_url": f"http://pr/{number}",
            "head": {"ref": "release-v1", "sha": resolved_candidate},
            "base": {"ref": base_ref, "sha": "base0001"},
            "merge_commit_sha": merge_sha if merged else None,
        }

    return mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_get_pull_request),
    )


@pytest.mark.asyncio
async def test_auto_stale_skips_when_head_unchanged(db_session, mocker):
    """When automain HEAD equals the not_promoted candidate SHA, the auto-stale
    gate short-circuits and no ledger rows are written."""
    from app.models import AuditFindingResolution

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    same_sha = "automn0" * 5 + "00000"
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": same_sha,
        "blocker": "below_threshold",
        "audit_findings": [
            {"key": "FINDING_NO_ADVANCE", "severity": "high", "summary": "unresolved"},
        ],
    }
    db_session.add(version)
    await db_session.commit()

    # HEAD matches candidate SHA — no advance.
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=same_sha))
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    ledger_rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert ledger_rows == []
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"


@pytest.mark.asyncio
async def test_auto_stale_skips_truncated_findings(db_session, mocker):
    """When audit_findings_truncated is True the auto-stale gate short-circuits
    — a partial finding list cannot safely be auto-staled."""
    from app.models import AuditFindingResolution

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    old_sha = "oldsha0" * 5 + "0000000"
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": old_sha,
        "blocker": "below_threshold",
        "audit_findings_truncated": True,
        "audit_findings": [
            {"key": "FINDING_TRUNC", "severity": "high", "summary": "truncated list"},
        ],
    }
    db_session.add(version)
    await db_session.commit()

    new_sha = "automn0" * 5 + "00000"
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=new_sha))
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    ledger_rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert ledger_rows == []


@pytest.mark.asyncio
async def test_auto_stale_idempotent_skips_existing_ledger_rows(db_session, mocker):
    """Auto-stale skips finding keys that already have a ledger row, preserving
    operator 'resolved' entries and avoiding redundant stale writes."""
    from app.models import AuditFindingResolution
    from app.release_readiness import apply_finding_resolution

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    old_sha = "oldsha0" * 5 + "0000000"
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": old_sha,
        "blocker": "below_threshold",
        "audit_findings": [
            {
                "key": "FINDING_RESOLVED",
                "severity": "high",
                "summary": "already resolved by operator",
            },
        ],
    }
    db_session.add(version)
    await db_session.commit()

    # Operator has already resolved this finding.
    await apply_finding_resolution(
        db_session, project.id, version.id, "FINDING_RESOLVED", "resolved"
    )
    await db_session.commit()

    new_sha = "automn0" * 5 + "00000"
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=new_sha))
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Re-drive fires (finding already resolved) → version moves to pending.
    # Finalized-only return semantics: the re-drive spawn is not counted.
    assert count == 0

    # Existing 'resolved' row was NOT overwritten by auto-stale.
    ledger_rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(ledger_rows) == 1
    assert ledger_rows[0].state == "resolved"


@pytest.mark.asyncio
async def test_sweep_skips_already_finalized_version(db_session):
    """A version whose last_qualification is not 'pending' is skipped — the
    sweep only acts on stuck pending records."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    # Mark it already finalized.
    version.last_qualification = {
        **version.last_qualification,
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0

    await db_session.refresh(version)
    # Unchanged.
    assert version.last_qualification["status"] == "not_promoted"


@pytest.mark.asyncio
async def test_sweep_is_idempotent(db_session):
    """Calling the sweep twice finalizes the stuck version once; the second
    call is a safe no-op that finalizes nothing."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

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
                    "test_erosion": {"deleted_test_files": []},
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

    first = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    second = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert first == 1
    # Second sweep finds nothing left to finalize.
    assert second == 0


@pytest.mark.asyncio
async def test_sweep_no_pending_versions_returns_zero(db_session):
    """With no pending qualifications anywhere, the sweep returns 0."""
    # An open version with no qualification at all.
    await _make_project_and_version(db_session)

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0


@pytest.mark.asyncio
async def test_finalize_retired_smoketest_is_absent_from_active_dimensions(db_session):
    """Audit H3: a smoketest-disabled qualification (no smoketest child, no
    SMOKETEST_RESULT) finalizes on audit + test_erosion alone — the smoketest
    dimension is dropped, no smoketest_startup_failed blocker fires, and a
    confident audit-only candidate reaches a dry_run (promotion off)."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 4,
                "test_count_delta": 2,
            },
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # No smoketest_startup_failed blocker — the dimension was dropped.
    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    # Perfect audit + perfect test_erosion renormalize to a 1.0 composite.
    assert result["confidence"] == pytest.approx(1.0)

    await db_session.refresh(version)
    rec = version.last_qualification
    assert rec["status"] == "dry_run"
    assert rec["smoketest_enabled"] is False
    assert "smoketest" not in rec["sub_scores"]


@pytest.mark.asyncio
async def test_maybe_finalize_smoketest_disabled_audit_only(db_session):
    """Audit H3: maybe_finalize_qualification finalizes a smoketest-disabled
    qualification once the SOLE audit child is terminal — it does not wedge
    waiting for a non-existent smoketest child."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    audit_task = await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {"deleted_test_files": []},
        },
    )

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Finalized on the audit child alone — no smoketest child to wait for.
    assert result is not None
    assert result["status"] == "dry_run"

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_smoke_status",
    [TaskStatus.IN_PROGRESS, TaskStatus.FAILED, TaskStatus.CANCELLED],
)
async def test_legacy_smoke_child_status_is_archival_and_never_gates(
    db_session, legacy_smoke_status
):
    """Legacy smoke configuration and IDs remain inspectable, but the retired
    smoke child cannot delay or fail finalization regardless of its status."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=legacy_smoke_status,
    )
    archived_smoke_id = str(smoke_task.id)
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "findings": [],
                    "test_erosion": {"deleted_test_files": []},
                }
            ),
        )
    )
    await db_session.commit()

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert result is not None
    assert result["status"] == "dry_run"
    assert result.get("blocker") is None

    await db_session.refresh(version)
    record = version.last_qualification
    assert record["status"] == "dry_run"
    # Preserve the historical record exactly for operator inspection.  The
    # active qualifier dimensions, rather than this archival flag, determine
    # whether finalization can proceed.
    assert record["smoketest_enabled"] is True
    assert record["smoketest_task_id"] == archived_smoke_id
    assert "smoketest" not in record["sub_scores"]

    rows = (
        (
            await db_session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id,
                    QualificationDimensionResult.dimension == "smoketest",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []

    await db_session.refresh(smoke_task)
    assert smoke_task.status == legacy_smoke_status


@pytest.mark.asyncio
async def test_maybe_finalize_env_preflight_only_does_not_require_smoketest_child(
    db_session,
):
    """Env preflight uses candidate evidence and must not require a smoke child.

    With the smoke lane disabled, an audit-only qualification must finalize
    even when env preflight is enabled. Missing preflight evidence should
    produce its normal fail-closed verdict instead of wedging in pending.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    audit_task = await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {"deleted_test_files": []},
        },
    )

    qual = dict(version.last_qualification or {})
    qual["env_preflight_enabled"] = True
    qual["env_preflight_evidence"] = None
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert result is not None
    assert result["status"] == "not_promoted"

    await db_session.refresh(version)
    rec = version.last_qualification
    assert rec["status"] == "not_promoted"
    assert rec["blocker"] == "boot_prereq_evidence_missing"
    assert rec["smoketest_task_id"] is None


@pytest.mark.asyncio
async def test_sweep_smoketest_disabled_audit_only(db_session):
    """Audit H3: the sweep finalizes a stuck smoketest-disabled qualification
    once its sole audit child is terminal — it does not require a smoketest
    child that never existed."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {"deleted_test_files": []},
        },
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


# ===========================================================================
# Audit H2 — qualification-timeout force-finalize
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_force_finalizes_timed_out_qualification(db_session, mocker):
    """Audit H2: a qualification pending longer than the qualification-timeout
    is force-finalized as a blocked one (blocker=qualification_timeout) EVEN
    when a child is still non-terminal — the escape hatch for a child that
    heartbeats-but-hangs.  Running children are CANCELLED and their VMs
    scheduled for teardown."""
    # Isolate this sweep/timeout test from the post-commit strategic re-trigger
    # (covered by its own tests); a resolved verdict would otherwise spawn a
    # real strategic_review whose branch-resolution emits extra alerts.
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    # Prevent the infra_failure redrive loop (runs after force-finalize in the
    # same sweep cycle) from overwriting the infra_failure status with error.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    # Prevent actual GCP VM teardown in test.
    mocker.patch(
        "app.release_qualifier_sweep.fire_and_forget_vm_delete",
    )
    from datetime import timedelta

    from app.time_utils import _utcnow
    from sqlmodel import select as _select

    project, version = await _make_project_and_version(db_session)

    # A current required audit child is still IN_PROGRESS, while an archival
    # legacy smoke child is also running. started_at is past the timeout.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,  # current required child hung
        smoke_status=TaskStatus.IN_PROGRESS,  # archival child, cleanup only
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1

    await db_session.refresh(version)
    # Force-finalized as an infra failure (not a conclusive verdict) so the
    # same SHA can be re-qualified once infrastructure recovers.
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    # Version stays open and returns to stabilize for the next strategic cycle.
    assert version.status == "open"
    assert version.phase == "stabilize"

    # The running smoke child was cancelled and its VM teardown scheduled.
    await db_session.refresh(smoke_task)
    assert smoke_task.status == TaskStatus.CANCELLED
    assert smoke_task.vm_stopped_at is not None

    # The running current audit child is cancelled as the cause of timeout.
    await db_session.refresh(audit_task)
    assert audit_task.status == TaskStatus.CANCELLED
    assert audit_task.vm_stopped_at is not None

    # An operator-visible Alert was raised.
    alerts = (
        (await db_session.execute(_select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "timed out" in alerts[0].title.lower()


@pytest.mark.asyncio
async def test_sweep_timeout_terminalizes_abandoned_children(db_session, mocker):
    """A qualification that times out with running children (audit, smoketest,
    challenge — and by extension merge_grade) cancels those children so the
    sweep can spawn a replacement without abandoned children consuming
    resources or completing against superseded attempt state."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.release_qualifier_sweep.fire_and_forget_vm_delete",
    )
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()

    # Seed with audit, smoke, and challenge all IN_PROGRESS.
    audit_task, smoke_task, challenge_task = await _seed_pending_qualification_with_challenge(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,
        smoke_status=TaskStatus.IN_PROGRESS,
        challenge_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"

    # Every running child was cancelled.
    await db_session.refresh(audit_task)
    assert audit_task.status == TaskStatus.CANCELLED
    assert audit_task.vm_stopped_at is not None

    await db_session.refresh(smoke_task)
    assert smoke_task.status == TaskStatus.CANCELLED
    assert smoke_task.vm_stopped_at is not None

    await db_session.refresh(challenge_task)
    assert challenge_task.status == TaskStatus.CANCELLED
    assert challenge_task.vm_stopped_at is not None


@pytest.mark.asyncio
async def test_sweep_spares_timed_out_qualification_while_pipeline_paused(
    db_session, mocker
):
    """PAUSE-AWARE 6h qualification-timeout backstop: a qualification whose raw
    wall-clock age exceeds the limit is NOT force-finalized when the excess is
    pipeline-pause time. While the pipeline is PAUSED the current audit/grade
    children cannot run, so the pending qualification is not "stuck" — force-finalizing
    it would send the version back to stabilize and re-fire the whole triple,
    which then times out again 6h later, re-spawning it against the newest
    candidate for the entire duration of the pause (the qualifier churn observed
    while paused).  Exercises the exact per-attempt ``pause_baseline`` subtraction
    path (baseline stamped at spawn, cumulative pause since then subtracted)."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.release_qualifier_sweep.fire_and_forget_vm_delete",
    )
    from datetime import timedelta

    from sqlalchemy.orm.attributes import flag_modified

    from app.models import DeploymentState, DeploymentStatus
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    # Started 7h ago with the required audit child still IN_PROGRESS, so the
    # wall-clock timeout backstop is the only possible finalizer.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,  # current required child hung
        smoke_status=TaskStatus.IN_PROGRESS,  # archival child, cleanup only
        started_at=old_started,
    )

    # ~5.5h of pipeline pause accumulated between spawn (baseline 5000s) and now
    # (cumulative 25000s), then resumed.  Active age ≈ 25200 - 20000 ≈ 1.4h < 6h.
    version.last_qualification["pause_baseline"] = 5000.0
    flag_modified(version, "last_qualification")
    db_session.add(version)
    deploy_state = DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,  # resumed — the pause window is closed
        total_manual_paused_seconds=25000.0,
        paused_at=None,
    )
    db_session.add(deploy_state)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    # Not swept: the pause-adjusted age is under the timeout.
    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    # Both the required audit child and archival smoke child remain untouched.
    await db_session.refresh(audit_task)
    assert audit_task.status == TaskStatus.IN_PROGRESS
    assert audit_task.vm_stopped_at is None
    await db_session.refresh(smoke_task)
    assert smoke_task.status == TaskStatus.IN_PROGRESS
    assert smoke_task.vm_stopped_at is None


@pytest.mark.asyncio
async def test_sweep_finalizes_timed_out_qualification_despite_brief_pause(
    db_session, mocker
):
    """MUTATION-PROOF companion to the pause-spare test: the pause-aware backstop
    must not degrade into a blanket disable.  A qualification 7h past its start
    with only a brief (30-min) pause still has ~6.5h of ACTIVE age above the 6h
    timeout, so it is force-finalized exactly as before.  Exercises the
    open-window fallback (legacy record carrying no per-attempt baseline —
    subtract only the currently-open pause window)."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.release_qualifier_sweep.fire_and_forget_vm_delete",
    )
    from datetime import timedelta

    from app.models import DeploymentState, DeploymentStatus
    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
    )
    # No pause_baseline on the record (legacy in-flight qual).  Pipeline is
    # currently paused, but only for 30 min — the open-window subtraction leaves
    # the active age ≈ 6.5h, still over the limit.
    deploy_state = DeploymentState(
        id=1,
        status=DeploymentStatus.PAUSED,
        paused_at=_utcnow() - timedelta(minutes=30),
        paused_kind="manual",
    )
    db_session.add(deploy_state)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    await db_session.refresh(smoke_task)
    assert smoke_task.status == TaskStatus.CANCELLED
    assert smoke_task.vm_stopped_at is not None


@pytest.mark.asyncio
async def test_sweep_timeout_partial_children_cancellation(db_session, mocker):
    """When some children are already terminal and others are running, only
    the running children are cancelled during force-finalize."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.release_qualifier_sweep.fire_and_forget_vm_delete",
    )
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()

    # Audit already COMPLETED, smoke IN_PROGRESS, challenge IN_PROGRESS.
    audit_task, smoke_task, challenge_task = await _seed_pending_qualification_with_challenge(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        challenge_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1

    # Running children (smoke, challenge) were cancelled.
    await db_session.refresh(smoke_task)
    assert smoke_task.status == TaskStatus.CANCELLED
    assert smoke_task.vm_stopped_at is not None

    await db_session.refresh(challenge_task)
    assert challenge_task.status == TaskStatus.CANCELLED
    assert challenge_task.vm_stopped_at is not None

    # Already-completed audit child is untouched.
    await db_session.refresh(audit_task)
    assert audit_task.status == TaskStatus.COMPLETED
    assert audit_task.vm_stopped_at is None


@pytest.mark.asyncio
async def test_sweep_crash_loop_cancels_children(db_session, mocker):
    """When the finalize path crash-loops past the threshold, the
    force-finalize cancels running children before stamping infra_failure."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    # Prevent the infra_failure redrive loop from re-spawning after
    # the crash-loop force-finalize in the same sweep cycle.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.release_qualifier_sweep.fire_and_forget_vm_delete",
    )

    project, version = await _make_project_and_version(db_session)

    # Seed a pending qual that an immediate crash (below) will force-finalize.
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
    )

    # Make _finalize_in_dedicated_session raise so the crash-streak logic
    # engages and, after _MAX_SWEEP_CRASH_STREAK attempts, force-finalizes.
    mocker.patch(
        "app.release_qualifier._finalize_in_dedicated_session",
        new=AsyncMock(side_effect=RuntimeError("simulated crash")),
    )

    for _ in range(rq._MAX_SWEEP_CRASH_STREAK):
        await rq.sweep_stuck_qualifications(
            db_session, session_factory=_shared_session_factory(db_session)
        )

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "sweep_finalize_crash_loop"
    assert version.phase == "stabilize"

    # The running smoke child was cancelled.
    await db_session.refresh(smoke_task)
    assert smoke_task.status == TaskStatus.CANCELLED
    assert smoke_task.vm_stopped_at is not None

    # The already-completed audit child is untouched.
    await db_session.refresh(audit_task)
    assert audit_task.status == TaskStatus.COMPLETED
    assert audit_task.vm_stopped_at is None


@pytest.mark.asyncio
async def test_timed_out_already_promoted_uses_qualification_superseded(db_session, mocker):
    """When a qualification times out but the candidate is already reachable
    from the promotion target (out-of-band force-promotion), record
    qualification_superseded blocker instead of the generic qualification_timeout.
    This allows the already-promoted exemption to engage on re-qualification."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.release_qualifier_sweep.fire_and_forget_vm_delete",
    )
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "abc123def456"

    # Seed a pending qualification that is well past the timeout.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
        candidate_sha=candidate_sha,
    )

    # Mock GitHub to report that the candidate is already on the promotion target.
    mocker.patch(
        "app.release_qualifier_sweep._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.release_promotion._candidate_already_on_main",
        new=AsyncMock(return_value=True),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1

    await db_session.refresh(version)
    # Should record qualification_superseded, not qualification_timeout.
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_superseded"
    assert "already reachable" in version.last_qualification["detail"]
    assert version.status == "open"
    assert version.phase == "stabilize"


@pytest.mark.asyncio
async def test_timed_out_not_promoted_uses_qualification_timeout(db_session, mocker):
    """When a qualification times out and the candidate is NOT reachable from
    the promotion target, record the generic qualification_timeout blocker."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "notpromoted123"

    # Seed a pending qualification that is well past the timeout.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
        candidate_sha=candidate_sha,
    )

    # Mock GitHub to report that the candidate is NOT on the promotion target.
    mocker.patch(
        "app.release_qualifier_sweep._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.release_promotion._candidate_already_on_main",
        new=AsyncMock(return_value=False),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1

    await db_session.refresh(version)
    # Should record the generic qualification_timeout blocker.
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    assert "qualifier child never reached" in version.last_qualification["detail"]
    assert version.status == "open"
    assert version.phase == "stabilize"


@pytest.mark.asyncio
async def test_timed_out_supersession_check_exception_falls_back_to_qualification_timeout(
    db_session, mocker
):
    """When _candidate_already_on_main raises an exception, the sweep falls back
    to qualification_timeout (fail-closed), never fabricating a supersession."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "deadbeef1234"

    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
        candidate_sha=candidate_sha,
    )

    mocker.patch(
        "app.release_qualifier_sweep._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.release_promotion._candidate_already_on_main",
        new=AsyncMock(side_effect=RuntimeError("github 503")),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    assert version.status == "open"
    assert version.phase == "stabilize"


@pytest.mark.asyncio
async def test_timed_out_cred_missing_falls_back_to_qualification_timeout(
    db_session, mocker
):
    """When _resolve_github_with_session returns None (no credentials), the sweep
    falls back to qualification_timeout without fabricating a supersession."""
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "nocreds999abc"

    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
        candidate_sha=candidate_sha,
    )

    mocker.patch(
        "app.release_qualifier_sweep._resolve_github_with_session",
        new=AsyncMock(return_value=None),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    assert version.status == "open"
    assert version.phase == "stabilize"


# ===========================================================================
# _maybe_reconcile_after_promotion_conflict — promotion-conflict self-heal
# ===========================================================================


@pytest.mark.asyncio
async def test_promotion_conflict_triggers_reconciliation(db_session, mocker):
    """A promotion that failed on a merge CONFLICT kicks off a main -> automain
    reconciliation so the version self-heals instead of dead-ending in
    stabilize until an operator notices."""
    project, version = await _make_project_and_version(db_session)
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    await rq._maybe_reconcile_after_promotion_conflict(
        version.id,
        {
            "status": "promotion_failed",
            "error": "merge_blocked",
            "conflict": True,
        },
        session_factory=_shared_session_factory(db_session),
    )

    recon_mock.assert_awaited_once()
    # Reconciliation runs for the conflicted version's own project.
    assert recon_mock.call_args.args[1] == project.id


@pytest.mark.asyncio
async def test_promotion_failure_without_conflict_does_not_reconcile(
    db_session, mocker
):
    """A non-conflict promotion failure (e.g. a 405 checks-block) must NOT
    trigger a reconciliation — there is no branch divergence to reconcile."""
    _project, version = await _make_project_and_version(db_session)
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    await rq._maybe_reconcile_after_promotion_conflict(
        version.id,
        {
            "status": "promotion_failed",
            "error": "merge_blocked",
            "conflict": False,
        },
        session_factory=_shared_session_factory(db_session),
    )

    recon_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_promotion_failure_result_does_not_reconcile(db_session, mocker):
    """A dry_run / not_promoted / None finalize result never triggers a
    reconciliation — only a conflicted promotion failure does."""
    _project, version = await _make_project_and_version(db_session)
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "merged"}),
    )

    for result in (
        {"status": "dry_run", "confidence": 0.9},
        {"status": "not_promoted", "blocker": "below_threshold"},
        None,
    ):
        await rq._maybe_reconcile_after_promotion_conflict(
            version.id,
            result,
            session_factory=_shared_session_factory(db_session),
        )

    recon_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_in_dedicated_session_reconciles_on_promotion_conflict(
    db_session, mocker
):
    """End-to-end wiring: when the finalize chain produces a conflicted
    promotion failure, _finalize_in_dedicated_session triggers the
    main -> automain reconciliation after its dedicated session has committed
    and closed."""
    _project, version = await _make_project_and_version(db_session)
    # Drive _finalize_locked_version to return a conflicted promotion failure.
    mocker.patch(
        "app.release_qualifier._finalize_locked_version",
        new=AsyncMock(
            return_value={
                "status": "promotion_failed",
                "error": "merge_blocked",
                "conflict": True,
            }
        ),
    )
    recon_mock = mocker.patch(
        "app.hotfix_backmerge.reconcile_main_into_automain",
        new=AsyncMock(return_value={"status": "conflict"}),
    )

    result = await rq._finalize_in_dedicated_session(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is not None
    assert result["status"] == "promotion_failed"
    recon_mock.assert_awaited_once()


# ===========================================================================
# _maybe_trigger_strategic_after_qualification — re-plan on a resolved verdict
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["not_promoted", "dry_run", "promoted", "error", "infra_failure"],
)
async def test_resolved_qualification_triggers_strategic_review(
    db_session, mocker, status
):
    """Every settled qualification verdict — a promote, any flavour of miss,
    or a terminal infra failure — re-triggers the strategic agent so it
    re-plans instead of leaving a dry frontier idle until the wall-clock
    backstop fires."""
    project, version = await _make_project_and_version(db_session)
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": status, "confidence": 0.8},
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_awaited_once()
    assert spawn_mock.call_args.args[0].id == project.id


@pytest.mark.asyncio
async def test_resolved_qualification_wakes_active_resident_without_spawn(
    db_session, mocker
):
    """A live Resident consumes the exact durable barrier receipt itself."""
    from app.models import ProjectMessage

    project, version = await _make_project_and_version(db_session)
    project.vision = {
        **(project.vision or {}),
        "_resident_strategic_session": {
            "session_id": "ss-qualification-test",
            "status": "active",
            "stop_requested": False,
        },
    }
    db_session.add(project)
    await db_session.commit()
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "not_promoted", "candidate_sha": "a" * 40},
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_not_awaited()
    receipt = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"release:{version.id}:qualification:not_promoted:{'a' * 40}",
            )
        )
    ).scalar_one()
    assert receipt.context["event_type"] == "release.qualification_resolved"
    assert receipt.context["receipt_kind"] == "qualification_barrier"
    assert receipt.context["qualification_status"] == "not_promoted"


@pytest.mark.asyncio
async def test_promoted_qualification_defers_to_single_serving_receipt(
    db_session, mocker
):
    """A promotion does not spend one Resident turn before serving resolves."""
    from app.models import ProjectMessage

    project, version = await _make_project_and_version(db_session)
    project.vision = {
        **(project.vision or {}),
        "_resident_strategic_session": {
            "session_id": "ss-promotion-test",
            "status": "active",
            "stop_requested": False,
        },
    }
    db_session.add(project)
    await db_session.commit()
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "promoted", "candidate_sha": "c" * 40},
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_not_awaited()
    receipts = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert receipts == []


@pytest.mark.asyncio
async def test_resolved_qualification_is_inert_while_project_paused(db_session, mocker):
    """Pause records the outcome but blocks every new agent spawn."""
    from app.models import ProjectMessage

    project, version = await _make_project_and_version(db_session)
    project.paused = True
    db_session.add(project)
    await db_session.commit()
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "not_promoted", "candidate_sha": "b" * 40},
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_not_awaited()
    receipt = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    assert receipt.context["qualification_status"] == "not_promoted"


@pytest.mark.asyncio
async def test_run_qualification_does_not_spawn_or_call_github_while_paused(
    db_session, mocker
):
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project.paused = True
    db_session.add(project)
    await db_session.commit()
    resolver = mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )
    spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(side_effect=AssertionError("paused qualification spawned work")),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )

    assert result == {"status": "project_paused"}
    resolver.assert_not_awaited()
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"status": "pending_managed_check_wait"},  # promotion still pending
        {"status": "audit_rate_limit_respawned", "respawn_count": 1},  # transient
        {"status": "wrong_phase"},  # no-op
        None,  # read-only no-op finalize
    ],
)
async def test_unresolved_qualification_does_not_trigger_strategic(
    db_session, mocker, result
):
    """In-flight / transient / no-op finalize results must NOT re-trigger the
    strategic agent — only a settled verdict warrants a re-plan."""
    _project, version = await _make_project_and_version(db_session)
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        result,
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategic_trigger_skipped_when_autonomy_disabled(db_session, mocker):
    """A project with autonomy disabled is never auto-driven into a strategic
    cycle by a qualification verdict."""
    project, version = await _make_project_and_version(db_session)
    project.autonomous_enabled = False
    db_session.add(project)
    await db_session.commit()
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "not_promoted"},
        session_factory=_shared_session_factory(db_session),
    )

    spawn_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategic_trigger_swallows_spawn_failure(db_session, mocker):
    """The trigger is best-effort: a spawn failure (e.g. payload too large) is
    logged but never propagates to break the finalize chain."""
    _project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    )

    # Must not raise.
    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "not_promoted"},
        session_factory=_shared_session_factory(db_session),
    )


@pytest.mark.asyncio
async def test_strategic_trigger_queues_refresh_when_cycle_active(db_session, mocker):
    """When a strategic cycle is already active the spawn no-ops (returns False) —
    the verdict trigger must NOT be dropped. It coalesces into a durable refresh
    marker the autonomous loop consumes (regression for the 2026-06-02 dropped-
    trigger starvation: v3 promoted while a cycle ran and directions stayed 0)."""
    project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=False),  # a cycle is already active
    )

    await rq._maybe_trigger_strategic_after_qualification(
        version.id,
        {"status": "promoted"},
        session_factory=_shared_session_factory(db_session),
    )

    await db_session.refresh(project)
    from app.strategic_trigger import strategic_refresh_pending

    assert strategic_refresh_pending(project.vision) is True
    assert (
        "post_qualification:promoted"
        in (project.vision or {})["_strategic_refresh"]["reason"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finalize_result",
    [
        {"status": "not_promoted", "blocker": "below_threshold"},
        {"status": "infra_failure"},
    ],
)
async def test_finalize_in_dedicated_session_triggers_strategic_on_settled_outcome(
    db_session, mocker, finalize_result
):
    """End-to-end wiring: a finalize that resolves to a settled outcome drives
    _finalize_in_dedicated_session to re-trigger a strategic review after its
    dedicated session has committed and closed."""
    _project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.release_qualifier._finalize_locked_version",
        new=AsyncMock(return_value=finalize_result),
    )
    spawn_mock = mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )

    result = await rq._finalize_in_dedicated_session(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is not None and result["status"] == finalize_result["status"]
    spawn_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_does_not_time_out_a_fresh_qualification(db_session):
    """Audit H2: a qualification still within the timeout window with a
    running required audit child is NOT force-finalized — the timeout is a
    backstop, not an aggressive killer.  A retired smoke child is irrelevant."""
    project, version = await _make_project_and_version(db_session)

    # Fresh started_at (default = now), with the required audit child running.
    # The legacy smoke child is archival and must not be the reason this stays
    # pending.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,
        smoke_status=TaskStatus.CANCELLED,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 0
    await db_session.refresh(version)
    # Untouched — still pending, no timeout blocker.
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_sweep_timeout_takes_priority_over_terminal_children(db_session, mocker):
    """Audit H2: when a qualification is BOTH past the timeout AND has all
    children terminal, the timeout path wins — it is checked first and is
    child-state-independent (a stuck qualification this old should surface as
    a timeout, not silently finalize)."""
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)

    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        started_at=old_started,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["blocker"] == "qualification_timeout"


# ===========================================================================
# Audit H5 — per-version commit isolation in the sweep
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_commits_each_version_independently(db_session, mocker):
    """Audit H5: the sweep commits after EACH finalized version, so a failure
    finalizing one version cannot roll back another version's already-committed
    finalization.  Here version A finalizes cleanly; version B's finalize is
    forced to raise — A's finalized record must survive."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    # Two independent projects, each with a stuck pending qualification whose
    # children are both terminal.
    project_a, version_a = await _make_project_and_version(db_session)
    audit_a, smoke_a = await _seed_pending_qualification(
        db_session,
        project_a,
        version_a,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    db_session.add(
        Artifact(
            task_id=audit_a.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {"audit_score": 1.0, "test_erosion": {"deleted_test_files": []}, "findings": []}
            ),
        )
    )
    db_session.add(
        Artifact(
            task_id=smoke_a.id,
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

    project_b, version_b = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project_b,
        version_b,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    await db_session.commit()

    # Make _finalize_qualification blow up ONLY for version B.  version A
    # finalizes normally; the sweep processes each version in its own
    # try/except + commit, so whichever order the open-versions query returns
    # them in, A's commit lands and B's raise is isolated + rolled back.
    # Snapshot version B's id as a plain scalar: with the shared-session
    # factory, _finalize_locked_version's pre-lock rollback expires the test's
    # instances, and reading ``version_b.id`` inside the side-effect would
    # raise MissingGreenlet under real ORM expiry (masking the intended
    # RuntimeError and failing version A's finalize too).
    version_b_id = version_b.id
    real_finalize = rq._finalize_qualification

    async def _flaky_finalize(session, version, *, _pre_gates=None):
        if version.id == version_b_id:
            raise RuntimeError("simulated finalize failure for version B")
        return await real_finalize(session, version, _pre_gates=_pre_gates)

    mocker.patch.object(
        rq, "_finalize_qualification", new=AsyncMock(side_effect=_flaky_finalize)
    )

    # The sweep must not propagate version B's error — it isolates per version.
    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    # Exactly one version finalized (A); B's failure was caught and isolated.
    assert count == 1

    # CRITICAL (audit H5): version A's finalization is committed and durable —
    # B's raise did NOT roll it back.
    await db_session.refresh(version_a)
    assert version_a.last_qualification["status"] == "dry_run"

    # Version B stays pending — its finalize failed and rolled back cleanly.
    await db_session.refresh(version_b)
    assert version_b.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_sweep_finalize_crash_increments_streak(db_session, mocker):
    """A single finalize crash increments sweep_crash_streak by 1 but does NOT
    force-finalize — self-recovery fires only after _MAX_SWEEP_CRASH_STREAK
    consecutive crashes.  The version stays pending and will be retried."""
    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    mocker.patch.object(
        rq,
        "_finalize_qualification",
        new=AsyncMock(side_effect=RuntimeError("simulated finalize crash")),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    assert count == 0  # crash was caught; nothing finalized on this sweep

    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual is not None
    assert qual.get("status") == "pending"  # NOT force-finalized yet
    assert qual.get("sweep_crash_streak") == 1  # streak incremented once


@pytest.mark.asyncio
async def test_sweep_finalize_crash_loop_force_finalizes_and_alerts(db_session, mocker):
    """After _MAX_SWEEP_CRASH_STREAK consecutive finalize crashes the sweep
    force-finalizes the version as infra_failure/sweep_finalize_crash_loop, resets
    it to the stabilize phase, posts a WARNING Alert, and sends an operator
    notification — the crash-loop is never invisible to operators."""
    project, version = await _make_project_and_version(db_session)
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Simulate (MAX - 1) prior crashes already recorded.
    await db_session.refresh(version)
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    version.last_qualification = {
        **version.last_qualification,
        "sweep_crash_streak": rq._MAX_SWEEP_CRASH_STREAK - 1,
    }
    _flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch.object(
        rq,
        "_finalize_qualification",
        new=AsyncMock(side_effect=RuntimeError("persistent finalize crash")),
    )
    # Prevent the infra_failure redrive loop (runs after force-finalize in the
    # same sweep cycle) from overwriting the infra_failure status with error.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    # force-finalize path does not count as a normal finalization
    assert count == 0

    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual is not None
    assert qual["status"] == "infra_failure"
    assert qual["blocker"] == "sweep_finalize_crash_loop"
    assert qual.get("sweep_crash_streak") == 0  # reset after force-finalize
    assert version.phase == "stabilize"  # returned to stabilize for re-qualification

    # A WARNING Alert must be raised so the crash-loop is visible to operators.
    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "crash" in alerts[0].title.lower()

    # An operator ProjectMessage must be posted.
    messages = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert any("crash" in (m.content or "").lower() for m in messages)


# ===========================================================================
# Round 5 — R4-C1 / R4-H1: promote() idempotency on a crash-retry
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_idempotent_when_exact_receipt_already_landed(
    db_session, mocker
):
    """R4-C1: a retry with an exact merged release-PR receipt records success
    without opening or merging a second PR."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "alr3ady0" * 5
    merge_sha = "receipt1" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # The exact receipt's immutable merge commit is reachable from main.
    ancestor_mock = mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=lambda *_args, **_kwargs: True),
    )
    _patch_exact_promotion_pr(
        mocker,
        pr_number=1,
        candidate=candidate,
        merge_sha=merge_sha,
        merged=True,
        discover=True,
    )
    # These must NOT be called — the promotion already landed.
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Recorded as a SUCCESS — not a failure.
    assert result["status"] == "promoted"

    # The idempotency check ran; no PR was opened, nothing was re-merged.
    ancestor_mock.assert_awaited()
    branch_mock.assert_not_called()
    create_mock.assert_not_called()
    merge_mock.assert_not_called()
    # The release branch a prior attempt may have left behind is cleaned up.
    delete_mock.assert_awaited()

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha
    assert version.last_qualification["status"] == "promoted"
    # The record is flagged as an already-landed idempotent retry.
    assert version.last_qualification.get("already_landed") is True
    # The next version was still opened — the DB now matches GitHub.
    next_version = await rv.current_open_version(db_session, version.project_id)
    assert next_version is not None
    assert next_version.version_number == 2


@pytest.mark.asyncio
async def test_promote_open_pr_already_merged_recorded_as_success(db_session, mocker):
    """R4-H1: on a retry the release PR is still open, merge_pull_request runs
    again, GitHub returns 405 (already merged) → {"merged": False}.  Without the
    post-merge disambiguation this would be misclassified as merge_blocked.
    The exact PR resource then reports the expected head/base pair, merged=True,
    and its immutable merge commit, so promote() records SUCCESS instead."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "0penpr00" * 5
    merge_sha = "realmerg" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )

    merge_mock = None

    async def _is_ancestor(_token, _owner, _repo, ancestor, _target, **_kwargs):
        return ancestor == merge_sha

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    # merge returns 405-style {"merged": False} — the PR was already merged.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": False, "message": "Pull Request is not mergeable"}
        ),
    )

    async def _get_pull_request(_token, _owner, _repo, number, **_kwargs):
        merged_now = bool(merge_mock.call_count)
        return {
            "number": number,
            "state": "closed" if merged_now else "open",
            "merged": merged_now,
            "html_url": f"http://pr/{number}",
            "head": {"ref": "release-v1", "sha": candidate},
            "base": {"ref": "main", "sha": "base0001"},
            "merge_commit_sha": merge_sha if merged_now else None,
        }

    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    exact_pr_mock = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_get_pull_request),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # A merged=False result + PR-state merged=True = SUCCESS, not merge_blocked.
    assert result["status"] == "promoted"
    assert result["status"] != "promotion_failed"
    merge_mock.assert_awaited()  # the merge WAS attempted
    assert exact_pr_mock.await_count >= 3  # evidence pair plus exact receipt
    delete_mock.assert_awaited()  # branch cleaned up

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    # The exact PR receipt's merge commit becomes promoted_sha.
    assert version.promoted_sha == merge_sha
    assert version.last_qualification.get("already_landed") is True


@pytest.mark.asyncio
async def test_promote_pr_creation_fails_on_retry_but_candidate_merged(
    db_session, mocker
):
    """R4-H1: on a retry the prior PR merged then closed, so create_pull_request
    finds nothing open and raises.  Because the candidate is already on main,
    promote() re-checks idempotency and records SUCCESS rather than
    pr_creation_failed."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "prret000" * 5
    merge_sha = "mergedrt" * 5

    from fastapi import HTTPException

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    async def _ancestor(_token, _owner, _repo, ancestor, _target, **_kwargs):
        return ancestor == merge_sha

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_ancestor))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(side_effect=[[], [{"number": 8}]]),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 8,
                "state": "closed",
                "merged": True,
                "html_url": "http://pr/8",
                "head": {"ref": "release-v1", "sha": candidate},
                "base": {"ref": "main", "sha": "base0001"},
                "merge_commit_sha": merge_sha,
            }
        ),
    )
    # PR creation raises — the prior attempt's PR is already merged + closed.
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=422, detail="Failed to create pull request: head ref"
            )
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "sha": merge_sha}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # PR creation failed but the candidate is on main → SUCCESS.
    assert result["status"] == "promoted"
    create_mock.assert_awaited()
    # No merge was attempted — PR creation failed before the merge step.
    merge_mock.assert_not_called()
    delete_mock.assert_awaited()

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification.get("already_landed") is True


@pytest.mark.asyncio
async def test_promote_indeterminate_ancestry_does_not_false_succeed(
    db_session, mocker
):
    """R4-C1 safety: when the idempotency ancestry check is INDETERMINATE
    (is_ancestor_of returns None — a transient GitHub error), promote() must
    NOT treat that as 'already merged'.  It proceeds with a real promotion —
    a transient failure must never silently skip a merge."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "1nd3t000" * 5
    merge_sha = "freshmrg" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # is_ancestor_of returns None — the comparison could not be made.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=None))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "sha": merge_sha, "message": "Merged"}
        ),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 2, "html_url": "http://pr/2"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=2, candidate=candidate)
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    # A real promotion ran — the indeterminate check did NOT short-circuit it.
    branch_mock.assert_awaited()
    merge_mock.assert_awaited()


@pytest.mark.asyncio
async def test_promote_genuine_merge_block_still_fails(db_session, mocker):
    """R4-H1 boundary: a genuine non-merge (a real checks/reviews block — the PR
    genuinely did not merge) is still recorded as merge_blocked.  The Round-7
    concern-3 disambiguation only converts a non-merge to success when the PR
    actually merged; it must not paper over real failures."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "blocked0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # is_ancestor_of False — only the idempotency pre-check consults it.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 4, "html_url": "http://pr/4"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=4, candidate=candidate)
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": True,
                "message": "merge conflict",
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # A real merge block — recorded as a failure, not papered over.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"


# ===========================================================================
# No-CI repo promotion uses the same managed_check_wait path
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_no_ci_repo_enters_managed_check_wait(db_session, mocker):
    """A not-yet-merged no-CI promotion is retained for managed retry."""
    # Build a no-CI project (build_config.requires_ci_checks=False).
    project = Project(
        name="NoCiRepo",
        user_id="noci-user",
        github_owner="acme",
        github_repo="noci",
        integration_branch="automain",
        promotion_target="main",
        build_config={"requires_ci_checks": False},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    candidate = "noci0001" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 55, "html_url": "http://pr/55"}),
    )
    # The merge returns merged=False (no conflict) — GitHub is waiting on checks.
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": False, "conflict": False}),
    )
    delete_branch_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.91},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Release branch kept — the reconciler needs it.
    delete_branch_mock.assert_not_awaited()

    # Result is the one retryable managed_check_wait.
    assert result["status"] == "pending_managed_check_wait"
    assert result["managed_check_wait"] is True
    assert result["pr_number"] == 55
    assert result["pr_url"] == "http://pr/55"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["pr_number"] == 55
    # No terminal failure.
    assert "error" not in version.last_qualification


# ===========================================================================
# Round 5 — R4-H2: release-branch deletion after a successful promotion
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_deletes_release_branch_after_merge(db_session, mocker):
    """R4-H2: after a confirmed merge, promote() best-effort deletes the
    release-v{N} branch so dangling release branches do not accumulate on the
    repo forever."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "ca0d" * 10
    merge_sha = "delbr000" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 6, "html_url": "http://pr/6"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=6, candidate=candidate)
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "sha": merge_sha, "message": "Merged"}
        ),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    # The release-v1 branch was deleted exactly once after the merge.
    delete_mock.assert_called_once()
    assert delete_mock.call_args.args[3] == "release-v1"


@pytest.mark.asyncio
async def test_promote_branch_delete_failure_does_not_abort_promotion(
    db_session, mocker
):
    """R4-H2: a delete_branch failure is non-fatal — the promotion still
    succeeds (the merge already landed; a dangling branch is cosmetic)."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "beef" * 10
    merge_sha = "keptbr00" * 5

    from fastapi import HTTPException

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 10, "html_url": "http://pr/10"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=10, candidate=candidate)
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "sha": merge_sha, "message": "Merged"}
        ),
    )
    # delete_branch raises — must not abort the promotion.
    mocker.patch(
        "app.github.delete_branch",
        new=AsyncMock(side_effect=HTTPException(status_code=500, detail="boom")),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.95}
    )
    await db_session.commit()

    # The promotion succeeds despite the branch-delete failure.
    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2
    await db_session.refresh(version)
    assert version.status == "promoted"


# ===========================================================================
# Round 5 — R4-H3 / R4-C1: lock-safe re-check guards against double-promote
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_bails_if_version_already_promoted_under_relock(
    db_session, mocker
):
    """R4-H3 / R4-C1: promote() releases the ReleaseVersion row lock for its
    GitHub I/O, then re-acquires it for the final write and RE-CHECKS
    version.status == "open".  If a concurrent finalize / crash-retry already
    promoted the version while the GitHub I/O ran, the re-locked row reads
    status="promoted" and promote() bails WITHOUT opening a duplicate vN+1."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "racec0de" * 5
    merge_sha = "racemerg" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 11, "html_url": "http://pr/11"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=11, candidate=candidate)

    # Simulate a concurrent finalize winning the race: while promote()'s merge
    # I/O "runs", another path flips the version to promoted in the DB.  We
    # hook merge_pull_request to perform that out-of-band mutation via a direct
    # SQL UPDATE (no ORM identity-map interaction with promote()'s own
    # already-committed/expired instance).
    from sqlalchemy import update as _sa_update

    async def _merge_then_concurrent_promote(*args, **kwargs):
        # A parallel promotion lands first: mark the version promoted directly.
        await db_session.execute(
            _sa_update(rv.ReleaseVersion)
            .where(rv.ReleaseVersion.id == version.id)
            .values(status="promoted", promoted_sha="wonrace0" * 5)
        )
        await db_session.commit()
        return {"merged": True, "sha": merge_sha, "message": "Merged"}

    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(side_effect=_merge_then_concurrent_promote),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # promote() detected the version is already promoted under the re-lock and
    # bailed — it reports success-as-already-promoted, NOT a fresh promotion.
    assert result["status"] == "promoted"
    assert result.get("already_promoted") is True
    # CRITICAL: no duplicate vN+1 was opened — only the concurrent winner's
    # state stands.  current_open_version must not find a v2.
    open_v = await rv.current_open_version(db_session, _project.id)
    assert open_v is None or open_v.version_number != 2

    # All ReleaseVersion rows for the project: exactly one (v1, promoted) —
    # no duplicate v2 row was created by the losing promote() call.
    from sqlmodel import select as _select

    all_versions = (
        (
            await db_session.execute(
                _select(rv.ReleaseVersion).where(
                    rv.ReleaseVersion.project_id == _project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(all_versions) == 1
    assert all_versions[0].version_number == 1


# ===========================================================================
# Round 5 — R4 MEDIUM: qualification_attempts increment is lost-update-safe
# ===========================================================================


@pytest.mark.asyncio
async def test_qualification_attempts_increments_atomically_on_no_credentials(
    db_session,
):
    """R4 MEDIUM: the no_credentials early-return path increments
    qualification_attempts with an atomic SQL UPDATE (not a read-modify-write).
    Two sequential calls bump the counter to exactly 2 — the atomic increment
    is relative to the row's current DB value."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    project = Project(name="RQ-AtomicAttempts", user_id="rq-user")  # no repo
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="test setup")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    r1 = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    assert r1["status"] == "no_credentials"
    await db_session.refresh(version)
    assert version.qualification_attempts == 1

    r2 = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    assert r2["status"] == "no_credentials"
    await db_session.refresh(version)
    # The atomic increment bumped it relative to the DB value → 2, not 1.
    assert version.qualification_attempts == 2


@pytest.mark.asyncio
async def test_qualification_attempts_increments_atomically_on_sha_error(
    db_session, mocker
):
    """R4 MEDIUM: the sha_error early-return path also increments
    qualification_attempts with an atomic SQL UPDATE."""
    from fastapi import HTTPException
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain SHA snapshot fails — the sha_error path.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="no automain")),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "sha_error"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "error"
    assert version.last_qualification["error"] == "sha_error"
    # The atomic increment ran.
    assert version.qualification_attempts == 1


@pytest.mark.asyncio
async def test_sha_error_detail_includes_exception_class(db_session, mocker):
    """sha_error detail uses f"{type(exc).__name__}: {exc}" so that exceptions
    with an empty str() (e.g. timeout-class errors) still record a useful label."""

    class TimeoutError(Exception):
        def __str__(self):
            return ""  # empty str — the old format recorded an empty detail

    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=TimeoutError()),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "sha_error"
    await db_session.refresh(version)
    detail = version.last_qualification["detail"]
    # Must include the class name even when str(exc) is empty.
    assert "TimeoutError" in detail


# ===========================================================================
# Round 5 — R4 MEDIUM: status!="open" guard before promoting
# ===========================================================================


@pytest.mark.asyncio
async def test_maybe_finalize_skips_non_open_version(db_session):
    """R4 MEDIUM: maybe_finalize_qualification skips a version whose status is
    not 'open' (e.g. already promoted/abandoned) — defensive insurance against
    a future abandon path racing a late child completion."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Flip the version to a terminal status while last_qualification still
    # says 'pending' (a future abandon path could do this).
    version.status = "abandoned"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )

    # The status!="open" guard fired — finalize did not run.
    assert result is None
    await db_session.refresh(version)
    # last_qualification is untouched — still pending, never finalized.
    assert version.last_qualification["status"] == "pending"
    assert version.status == "abandoned"


@pytest.mark.asyncio
async def test_sweep_skips_non_open_version(db_session):
    """R4 MEDIUM: the sweep skips a non-open version even when it is past the
    qualification timeout — the timeout force-finalize must never flip a
    terminal (promoted/abandoned) version back to stabilize."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    project, version = await _make_project_and_version(db_session)
    # Past the timeout, one child still running — would normally force-finalize.
    old_started = (
        _utcnow() - timedelta(seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 600)
    ).isoformat()
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
        started_at=old_started,
    )

    # Flip to abandoned after seeding the (timed-out) pending qualification.
    version.status = "abandoned"
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The sweep's open-versions query never even returns an abandoned version,
    # and _sweep_one_version's status!="open" guard is the belt-and-suspenders
    # backstop — either way, nothing is finalized.
    assert count == 0
    await db_session.refresh(version)
    # The timed-out qualification was NOT force-finalized.
    assert version.last_qualification["status"] == "pending"
    assert version.status == "abandoned"


# ===========================================================================
# Round 5 — R4-H5: timeout backstop survives an unparseable started_at
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_timeout_fires_when_started_at_unparseable(db_session, mocker):
    """R4-H5: when started_at is corrupt/unparseable, the qualification-timeout
    backstop must STILL fire — it falls back to version.updated_at as the
    timeout clock.  A bad timestamp string must never wedge a qualification
    forever."""
    # Isolate this sweep/timeout test from the post-commit strategic re-trigger
    # (covered by its own tests); a resolved verdict would otherwise spawn a
    # real strategic_review whose branch-resolution emits extra alerts.
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    # Prevent the infra_failure redrive loop (runs after force-finalize in the
    # same sweep cycle) from overwriting the infra_failure status with error.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from datetime import timedelta

    from app.models import ReleaseVersion as _RV
    from app.time_utils import _utcnow
    from sqlmodel import select as _select

    project, version = await _make_project_and_version(db_session)

    # Seed a pending qualification with a hung child and a CORRUPT started_at.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,  # hung child, never terminal
        started_at="not-a-real-timestamp",  # unparseable
    )

    # Force the row's updated_at far into the past so the updated_at fallback
    # clock exceeds the qualification timeout.  An explicit UPDATE bypasses the
    # onupdate hook (which would otherwise stamp it to now).
    old_ts = _utcnow() - timedelta(
        seconds=rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600
    )
    from sqlalchemy import update as _update

    await db_session.execute(
        _update(_RV).where(_RV.id == version.id).values(updated_at=old_ts)
    )
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The timeout fired via the updated_at fallback, even with a bad started_at.
    assert count == 1
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["blocker"] == "qualification_timeout"
    assert version.phase == "stabilize"

    # An operator-visible Alert was raised.
    from app.models import Alert as _Alert

    alerts = (
        (
            await db_session.execute(
                _select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_sweep_no_timeout_when_started_at_unparseable_but_row_fresh(
    db_session,
):
    """R4-H5: the unparseable-started_at fallback does NOT make the timeout
    over-aggressive — when updated_at is recent, a corrupt started_at does not
    cause a premature force-finalize.  The still-running required audit child
    keeps the version pending; the retired smoke child remains irrelevant."""
    project, version = await _make_project_and_version(db_session)

    # Corrupt started_at, but the row was just written (updated_at = now).  The
    # audit child is the active gate; a terminal legacy smoke child is archival.
    await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.IN_PROGRESS,
        smoke_status=TaskStatus.CANCELLED,
        started_at="garbage",
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # updated_at is fresh → the fallback clock is well within the timeout.
    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


# ===========================================================================
# Round 5 — github.is_ancestor_of: the idempotency primitive
# ===========================================================================


class TestIsAncestorOf:
    """github.is_ancestor_of wraps the compare API to answer 'is ancestor_sha
    reachable from descendant_ref'."""

    @pytest.mark.asyncio
    async def test_status_ahead_means_ancestor(self, mocker):
        """compare status 'ahead' → the descendant strictly contains the
        ancestor → True."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "ahead"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is True

    @pytest.mark.asyncio
    async def test_status_identical_means_ancestor(self, mocker):
        """compare status 'identical' (same commit) → True."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "identical"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is True

    @pytest.mark.asyncio
    async def test_status_diverged_means_not_ancestor(self, mocker):
        """compare status 'diverged' → the descendant does NOT contain the
        ancestor → False."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "diverged"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is False

    @pytest.mark.asyncio
    async def test_status_behind_means_not_ancestor(self, mocker):
        """compare status 'behind' → False."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"status": "behind"})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is False

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, mocker):
        """A non-200 response → None (indeterminate, NOT False) so the caller
        can distinguish 'definitely not an ancestor' from 'could not tell'."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 404
        resp.json = mocker.Mock(return_value={})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is None

    @pytest.mark.asyncio
    async def test_unparseable_body_returns_none(self, mocker):
        """An unparseable 200 body → None (indeterminate)."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(side_effect=ValueError("bad json"))
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.is_ancestor_of("tok", "o", "r", "cafe" * 10, "main")
        assert result is None


# ===========================================================================
# Round 7 — github.get_pull_request_merge_state: robust merged disambiguation
# ===========================================================================


class TestGetPullRequestMergeState:
    """github.get_pull_request_merge_state queries the PR resource itself for
    its authoritative ``merged`` boolean — the primitive that lets promote()
    distinguish an already-merged PR from a genuine merge block (concern 3)."""

    @pytest.mark.asyncio
    async def test_merged_true(self, mocker):
        """A PR whose resource reports merged=True → True."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"number": 7, "merged": True})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is True

    @pytest.mark.asyncio
    async def test_merged_false(self, mocker):
        """A PR whose resource reports merged=False → False (authoritative —
        the PR genuinely has not merged)."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"number": 7, "merged": False})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is False

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, mocker):
        """A non-200 (404 / API error) → None (indeterminate, NOT False) so the
        caller can fall back rather than misread a transient failure as
        'definitely not merged'."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 404
        resp.json = mocker.Mock(return_value={})
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is None

    @pytest.mark.asyncio
    async def test_unparseable_body_returns_none(self, mocker):
        """An unparseable 200 body → None (indeterminate)."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(side_effect=ValueError("bad json"))
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_merged_field_returns_none(self, mocker):
        """A 200 body with no boolean ``merged`` field → None (indeterminate) —
        the helper never guesses."""
        from app import github

        resp = mocker.Mock()
        resp.status_code = 200
        resp.json = mocker.Mock(return_value={"number": 7})  # no 'merged'
        mocker.patch("app.github._github_request", new=AsyncMock(return_value=resp))
        result = await github.get_pull_request_merge_state("tok", "o", "r", 7)
        assert result is None


# ===========================================================================
# Round 7 — promote(): merged=False disambiguation via an exact PR receipt
# (concern 3 — an already-merged PR must never be recorded as merge_blocked)
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_merged_false_but_pr_state_merged_is_success(db_session, mocker):
    """Concern 3: merge_pull_request returns {"merged": False} (GitHub's merge
    endpoint returns 405 once a PR is already merged), AND the eventually-
    PR then exposes an exact merged receipt for the expected head/base/candidate
    and immutable merge commit.  promote() records SUCCESS rather than
    misclassifying a genuinely landed promotion as merge_blocked."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "pr5tate0" * 5
    merge_sha = "pr5merg0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    async def _is_ancestor(_token, _owner, _repo, ancestor, _target, **_kwargs):
        return ancestor == merge_sha

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 31, "html_url": "http://pr/31"}),
    )
    # merge returns 405-style {"merged": False} — the PR was already merged.
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": False, "message": "not mergeable"}),
    )

    async def _get_pull_request(_token, _owner, _repo, number, **_kwargs):
        merged_now = bool(merge_mock.call_count)
        return {
            "number": number,
            "state": "closed" if merged_now else "open",
            "merged": merged_now,
            "html_url": f"http://pr/{number}",
            "head": {"ref": "release-v1", "sha": candidate},
            "base": {"ref": "main", "sha": "base0001"},
            "merge_commit_sha": merge_sha if merged_now else None,
        }

    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    exact_pr_mock = mocker.patch(
        "app.github.get_pull_request", new=AsyncMock(side_effect=_get_pull_request)
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # Recorded as a SUCCESS — NOT merge_blocked — on the exact receipt.
    assert result["status"] == "promoted"
    assert result["status"] != "promotion_failed"
    merge_mock.assert_awaited()  # the merge WAS attempted
    assert exact_pr_mock.await_count >= 3
    delete_mock.assert_awaited()  # branch cleaned up

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification.get("already_landed") is True
    # The immutable receipt merge commit becomes promoted_sha.
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_promote_merged_false_and_pr_state_not_merged_is_failure(
    db_session, mocker
):
    """Concern 3 boundary: merge_pull_request returns {"merged": False} AND the
    exact PR remains open and the merge reports a real conflict.  This is
    recorded as merge_blocked."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "n0tm3rg0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    # Pre-check ancestry False so promote() proceeds to the merge step.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 32, "html_url": "http://pr/32"}),
    )
    exact_pr_mock = _patch_exact_promotion_pr(
        mocker, pr_number=32, candidate=candidate
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": True,
                "message": "merge conflict",
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    # A genuine merge block (conflict=True) — recorded as a failure, not papered over.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"
    assert exact_pr_mock.await_count >= 3

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_promote_merged_false_without_exact_receipt_defers_despite_ancestry(
    db_session, mocker
):
    """Candidate ancestry cannot turn merged=False into promotion success.

    Without an exact merged PR receipt the promotion remains in the canonical
    managed waiter even when the candidate is reachable from main.
    """
    _project, version = await _make_project_and_version(db_session)
    candidate = "fa11bck0" * 5
    merge_sha = "fa11mrg0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    # Pre-check ancestry False (proceed to merge); the post-merge fallback
    # ancestry check returns True (the candidate did in fact land).
    ancestry_calls = {"n": 0}

    async def _ancestor(token, owner, repo, anc, desc, project_id=None):
        ancestry_calls["n"] += 1
        return ancestry_calls["n"] >= 2

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_ancestor))
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 33, "html_url": "http://pr/33"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=33, candidate=candidate)
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": False, "message": "not mergeable"}),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "pending_managed_check_wait"
    assert result["managed_check_wait"] is True
    # Ancestry corroboration ran, but could not supply promotion authority.
    assert ancestry_calls["n"] >= 2

    await db_session.refresh(version)
    assert version.status == "open"
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["pr_number"] == 33


# ===========================================================================
# Round 7 — concern 1: promote() must NOT commit a caller's unrelated
# transaction.  maybe_finalize_qualification opens its OWN dedicated session.
# ===========================================================================


@pytest.mark.asyncio
async def test_maybe_finalize_takes_no_caller_session(db_session, mocker):
    """Concern 1 (structural, both dialects): maybe_finalize_qualification has
    NO ``session`` parameter — it cannot, by construction, operate on or commit
    a caller's session.  It opens its OWN session via the injected factory; the
    factory IS invoked exactly once for a real qualifier child."""
    # Isolate the factory-count invariant from the post-commit strategic
    # re-trigger (covered by its own tests), which opens its OWN session via the
    # same factory — so the finalize chain's single dedicated session stays the
    # only factory call this assertion counts.
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    import inspect

    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    # The public signature must not accept a session — passing one is an error.
    params = list(inspect.signature(rq.maybe_finalize_qualification).parameters)
    assert "session" not in params
    assert params[0] == "completed_task"

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
                {"audit_score": 1.0, "test_erosion": {"deleted_test_files": []}, "findings": []}
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

    # Wrap the shared-session factory so we can assert it was the source of the
    # session the finalize chain ran on.
    factory_calls = {"n": 0}
    base_factory = _shared_session_factory(db_session)

    def _counting_factory():
        factory_calls["n"] += 1
        return base_factory()

    result = await rq.maybe_finalize_qualification(
        smoke_task, session_factory=_counting_factory
    )
    await db_session.commit()

    # The dedicated session was opened via the injected factory — exactly once.
    assert factory_calls["n"] == 1
    # And the finalize actually ran on it.
    assert result is not None
    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


# ===========================================================================
# Round 8 — promote(): pending-CI block → enable auto-merge, no failure verdict
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_pending_ci_enters_managed_check_wait_no_failure(db_session, mocker):
    """When merge_pull_request returns merged=False with no conflict flag
    (GitHub 405 — required checks still pending), promote() must NOT record a
    failure verdict. Instead Blueprint retains the PR in managed_check_wait and
    retries after checks pass. last_qualification stays "pending"."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "p3nd1ng0" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # PR not yet merged (pending CI, not a conflict).
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 99, "html_url": "http://pr/99"}),
    )
    # 405-style: not merged, no conflict flag — pending required checks.
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": False, "message": "required checks pending"}
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9657},
        base_record={"confidence": 0.9657, "sub_scores": {"ci": 1.0}},
    )
    await db_session.commit()

    # Pending CI → managed retry, NOT a promotion failure.
    assert result["status"] == "pending_managed_check_wait"
    assert result["managed_check_wait"] is True

    # No failure recorded — status is not promotion_failed.
    await db_session.refresh(version)
    lq = version.last_qualification or {}
    assert lq.get("status") != "promotion_failed"
    assert "error" not in lq

    # Durable promotion_pending sub-state is now written so the reconciler can
    # distinguish this version (qualification PASSED) from a hung qualifier child.
    pending = lq.get("promotion_pending") or {}
    assert pending.get("pr_number") == 99
    assert pending.get("pr_url") == "http://pr/99"
    assert pending.get("managed_check_wait") is True
    assert "enabled_at" in pending

    # The spawn-time "pending" record carried NO confidence (the composite is
    # only computed at finalize), so promote() must merge base_record's composite
    # into the durable promotion_pending record.  Otherwise the later reconcile
    # path records the promotion at "confidence 0.00" (the v6 prod regression).
    assert lq.get("confidence") == 0.9657
    assert lq.get("sub_scores") == {"ci": 1.0}
    # The merge must NOT clobber the "pending" status the substate relies on.
    assert lq.get("status") == "pending"


@pytest.mark.asyncio
async def test_promote_does_not_commit_caller_unrelated_transaction(db_session, mocker):
    """Concern 1 (behavioral): promote()'s intermediate commit (which releases
    the row lock before its GitHub I/O) must NOT commit a caller's UNRELATED
    open transaction — the completion handler's terminal-status / TaskHistory /
    Artifact / remediation work.

    The fix is that the finalize→promote chain runs on its OWN dedicated
    session, so promote()'s commit only ever flushes qualifier state.  This
    test proves it: a genuinely-separate caller session holds an uncommitted
    unrelated Task; after the dedicated-session promote chain runs (intermediate
    commit included), the caller's Task is STILL uncommitted — rolling the
    caller session back discards it.

    Requires genuinely independent DB transactions (a real second connection),
    which the SQLite in-memory StaticPool harness cannot provide — so it is
    skipped there and runs under Postgres (the test-backend.sh default + CI)."""
    if db_session.bind.dialect.name == "sqlite":
        pytest.skip(
            "needs two independent DB transactions — SQLite in-memory shares "
            "one connection; runs under Postgres"
        )

    from app import shared_state
    from app.database import AsyncSessionLocal

    shared_state.runtime_config["promotion_enabled"] = True

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
                    # A clean RELEASE_AUDIT_REPORT carries an explicit empty
                    # findings list — the real agent always emits the key.  A
                    # report that omits it is the malformed `no_findings_key`
                    # shape _score_audit scores as UNMEASURED, which hard-blocks
                    # on `audit_missing` before promotion is ever considered.
                    "findings": [],
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 5,
                        "test_count_delta": 2,
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

    # promote() will run a full merge — patch the GitHub surface.
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main="caller00" * 5),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 50, "html_url": "http://pr/50"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=50)
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )
    # The full maybe_finalize_qualification wrapper fires post-promote hooks that
    # make real GitHub calls; stub them so the test does not hang to the 30s
    # timeout on the network-isolated shard.
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.release_qualifier._maybe_reconcile_after_promotion_conflict",
        new=AsyncMock(return_value=None),
    )

    # A genuinely separate "caller" session with UNRELATED uncommitted work —
    # this stands in for the task-completion handler's open transaction.
    caller_session = AsyncSessionLocal()
    try:
        unrelated = Task(
            project_id=project.id,
            title="UNRELATED-caller-work",
            payloads=[{"type": "fix"}],
            vm_instance_id="vm-unrelated-caller",
            status=TaskStatus.IN_PROGRESS,
        )
        caller_session.add(unrelated)
        await caller_session.flush()  # in the caller txn, NOT committed
        unrelated_id = unrelated.id

        # The merge hook fires DURING promote() — at that point promote() has
        # already issued its intermediate commit.  If that commit had run on
        # the caller's session it would have committed `unrelated`.  Assert the
        # caller's row is still invisible to a fresh connection at this moment.
        async def _merge_and_probe(*args, **kwargs):
            async with AsyncSessionLocal() as probe:
                leaked = await probe.get(Task, unrelated_id)
            assert leaked is None, (
                "promote()'s intermediate commit leaked the caller's "
                "unrelated transaction"
            )
            return {"merged": True, "sha": "caller00" * 5, "message": "Merged"}

        mocker.patch(
            "app.github.merge_pull_request",
            new=AsyncMock(side_effect=_merge_and_probe),
        )

        # Run the real finalize→promote chain on its own dedicated session.
        result = await rq.maybe_finalize_qualification(smoke_task)
        assert result["status"] == "promoted"

        # The dedicated-session promotion committed qualifier state...
        async with AsyncSessionLocal() as verify:
            v = await verify.get(rv.ReleaseVersion, version.id)
            assert v is not None
            assert v.status == "promoted"
            # ...but the caller's unrelated Task is STILL uncommitted.
            leaked_after = await verify.get(Task, unrelated_id)
            assert leaked_after is None, (
                "the caller's unrelated transaction was committed by the finalize chain"
            )

        # The caller still owns its transaction: rolling back discards the
        # unrelated work cleanly (it was never committed by promote()).
        await caller_session.rollback()
        async with AsyncSessionLocal() as final:
            assert await final.get(Task, unrelated_id) is None
    finally:
        await caller_session.close()


@pytest.mark.asyncio
async def test_concurrent_finalize_does_not_double_promote(db_session, mocker):
    """Concern 4(d): two finalize attempts on the same promote-eligible version
    must promote it exactly ONCE.

    The dedicated session FOR UPDATE-locks the version row at the top of the
    critical section, so two concurrent callers are serialized — concurrent
    becomes sequential.  The first finalizes (promote → status='promoted'); the
    second, on acquiring the lock, sees ``last_qualification.status`` is no
    longer 'pending' (and ``status`` no longer 'open') and bails.  Driving the
    finalize path twice in sequence is exactly that serialized resolution: the
    structural guard must yield exactly one promotion — one v2 row, merge
    called once."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

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
                    "findings": [],
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 8,
                        "test_count_delta": 3,
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

    merge_sha = "dbl3prom" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 60, "html_url": "http://pr/60"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    factory = _shared_session_factory(db_session)

    # First finalize — promotes v1, opens v2.
    first = await rq.maybe_finalize_qualification(smoke_task, session_factory=factory)
    await db_session.commit()
    assert first is not None
    assert first["status"] == "promoted"

    # Second finalize (the serialized "concurrent" caller) — the version is now
    # promoted; the under-lock guard must make this a no-op.
    second = await rq.maybe_finalize_qualification(smoke_task, session_factory=factory)
    await db_session.commit()
    # No second finalize ran — the pending guard short-circuited it.
    assert second is None

    # The merge was attempted exactly ONCE — no double-merge.
    assert merge_mock.await_count == 1

    # Exactly TWO ReleaseVersion rows exist for the project: v1 (promoted) and
    # the single v2 the first finalize opened — NO duplicate v2.
    from sqlmodel import select as _select

    all_versions = (
        (
            await db_session.execute(
                _select(rv.ReleaseVersion).where(
                    rv.ReleaseVersion.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(all_versions) == 2
    numbers = sorted(v.version_number for v in all_versions)
    assert numbers == [1, 2]
    v1 = next(v for v in all_versions if v.version_number == 1)
    assert v1.status == "promoted"


@pytest.mark.asyncio
async def test_overlapping_promote_does_not_double_promote(db_session, mocker):
    """Round 9 (concurrency, genuine overlap): the SECOND finalize is launched
    from INSIDE the first finalize's merge hook — a true overlapping promote,
    not the post-promotion sequential case ``test_concurrent_finalize_does_not_
    double_promote`` covers.

    promote() issues its intermediate commit (releasing the ReleaseVersion row
    lock) BEFORE its GitHub I/O, with ``last_qualification.status`` still
    'pending'.  So a concurrent caller that fires mid-merge genuinely passes the
    pending guard and enters its own promote() — the pending guard is NOT what
    serializes this.  The real safety net is the exact merged-PR receipt plus
    the ``status != "open"`` re-check in ``_record_promotion_success`` /
    ``_record_promotion_failure``.

    This mirrors the mid-merge-callback pattern of
    ``test_promote_does_not_commit_caller_unrelated_transaction``: the merge
    hook reentrantly runs the second finalize on its OWN genuinely-independent
    dedicated session.  Like that test it needs two real independent DB
    transactions (the first finalize's intermediate commit must release a row
    lock the second can then acquire), which the single-connection SQLite
    in-memory harness cannot provide — so it is skipped there and runs under
    Postgres (the test-backend.sh default + CI).  The dedicated sessions are
    built from the test engine (``db_session.bind``) rather than the global
    ``app.database.AsyncSessionLocal`` — that engine's pool is loop-bound and
    reused across pytest-asyncio's per-test event loops, so opening sessions on
    it here would trip an 'attached to a different loop' error; the test engine
    is the one already on this test's loop.  Despite the genuine overlap the
    result must be exactly ONE merge call and exactly ONE v2 ReleaseVersion
    row."""
    if db_session.bind.dialect.name == "sqlite":
        pytest.skip(
            "needs two independent DB transactions for a genuine overlapping "
            "promote — SQLite in-memory shares one connection; runs under "
            "Postgres"
        )

    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    # A dedicated-session factory bound to the SAME engine the test fixture
    # uses (db_session.bind) — genuinely independent transactions / row locks,
    # but on this test's event loop (avoiding the global AsyncSessionLocal
    # cross-loop pool trap).  Each session it yields IS closed on exit, so the
    # finalize chain owns its own lifecycle exactly as it would in production.
    _test_engine = db_session.bind

    @asynccontextmanager
    async def _dedicated_factory():
        async with _AsyncSession(_test_engine, expire_on_commit=False) as s:
            yield s

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
                    # See the sibling promote test: a clean report must carry an
                    # explicit empty findings list, else _score_audit reads it as
                    # UNMEASURED and compute_composite hard-blocks `audit_missing`.
                    "findings": [],
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 8,
                        "test_count_delta": 3,
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

    merge_sha = "0v3rlap0" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    # The exact release PR is open before the first merge lands and exposes an
    # immutable merged receipt afterwards.  The SECOND finalize, launched from
    # inside the merge hook, discovers that receipt and does not submit another
    # merge.
    merge_landed = {"done": False}

    async def _is_ancestor(token, owner, repo, anc, desc, project_id=None):
        return merge_landed["done"]

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    create_branch_mock = mocker.patch(
        "app.github.create_branch", new=_create_branch_mock()
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 70, "html_url": "http://pr/70"}),
    )

    async def _find_release_pr(*_args, **_kwargs):
        return [{"number": 70}] if merge_landed["done"] else []

    async def _get_release_pr(_token, _owner, _repo, number, **_kwargs):
        branch_call = create_branch_mock.call_args
        assert branch_call is not None
        candidate = branch_call.args[4]
        return {
            "number": number,
            "state": "closed" if merge_landed["done"] else "open",
            "merged": merge_landed["done"],
            "html_url": f"http://pr/{number}",
            "head": {"ref": "release-v1", "sha": candidate},
            "base": {"ref": "main", "sha": "base0001"},
            "merge_commit_sha": merge_sha if merge_landed["done"] else None,
        }

    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(side_effect=_find_release_pr),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_get_release_pr),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    second_result = {}

    async def _merge_then_reentrant_finalize(*args, **kwargs):
        # The first promote() is mid-merge here — its intermediate commit has
        # already run, so the ReleaseVersion row lock is released and
        # last_qualification is still 'pending'.  Launch the
        # genuinely-overlapping second finalize NOW, on its OWN independent
        # dedicated session (a real second connection from the test engine, NOT
        # the shared test session) so it is a true concurrent promote and not a
        # reentrant commit on this transaction.  The merge hook is THIS
        # function, so we expose the exact receipt BEFORE the reentrant call.
        # The inner finalize resolves that receipt and takes the no-merge
        # idempotent-success path, avoiding a recursive merge.
        if not merge_landed["done"]:
            merge_landed["done"] = True
            second_result["value"] = await rq.maybe_finalize_qualification(
                smoke_task, session_factory=_dedicated_factory
            )
        return {"merged": True, "sha": merge_sha, "message": "Merged"}

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(side_effect=_merge_then_reentrant_finalize),
    )

    # The first finalize runs on its own dedicated session (test-engine factory)
    # — fully independent of this test's db_session.
    first = await rq.maybe_finalize_qualification(
        smoke_task, session_factory=_dedicated_factory
    )

    # The first finalize promoted v1.
    assert first is not None
    assert first["status"] == "promoted"

    # The genuinely-overlapping second finalize ran its own promote() but the
    # idempotency pre-check short-circuited it — it recorded success WITHOUT a
    # second merge (status 'promoted', possibly flagged already_promoted /
    # already_landed depending on which call won the status!="open" re-check).
    assert second_result["value"] is not None
    assert second_result["value"]["status"] == "promoted"

    # merge_pull_request was called EXACTLY once despite the genuine overlap —
    # the second promote() took the exact-receipt idempotent path.
    assert merge_mock.await_count == 1

    # Exactly ONE v2 ReleaseVersion row — no duplicate from the overlapping
    # promote.  Read through a fresh dedicated connection: the committed
    # cross-session state is what matters (db_session would not see the
    # separate-connection commits without an expire/re-read).
    from sqlmodel import select as _select

    async with _dedicated_factory() as verify:
        all_versions = (
            (
                await verify.execute(
                    _select(rv.ReleaseVersion).where(
                        rv.ReleaseVersion.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(all_versions) == 2
        assert sorted(v.version_number for v in all_versions) == [1, 2]
        v1 = next(v for v in all_versions if v.version_number == 1)
        assert v1.status == "promoted"
        # v1's terminal record is the promoted record — never overwritten with
        # a contradictory promotion_failed.
        assert v1.last_qualification["status"] == "promoted"


@pytest.mark.asyncio
async def test_record_promotion_failure_does_not_overwrite_promoted_version(
    db_session, mocker
):
    """Round 9 (data integrity): when two finalize/promote calls race and one
    PROMOTES successfully while the loser's merge fails transiently, the loser's
    _record_promotion_failure re-locks the row, sees status='promoted', and must
    NOT overwrite last_qualification with a {status:'promotion_failed'} record —
    that would leave a self-contradictory terminal state (version.status=
    'promoted' yet last_qualification.status='promotion_failed').

    Mirrors ``test_promote_bails_if_version_already_promoted_under_relock`` (the
    SUCCESS-path counterpart) but drives the FAILURE path: the merge hook flips
    the version to 'promoted' out-of-band (a parallel promotion landing first)
    AND returns {merged: False} so the losing promote() routes to
    _record_promotion_failure.  With the guard in place the loser bails as
    already-promoted and the promoted record stands intact."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "l0s3rrac" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # is_ancestor_of False — the idempotency pre-check does not short-circuit;
    # this promote() proceeds to open + merge the PR (and the merge fails).
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 80, "html_url": "http://pr/80"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=80, candidate=candidate)
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    # The merge hook: a PARALLEL promotion lands first (direct SQL UPDATE flips
    # the row to 'promoted' with a real promoted record), then this call's own
    # merge returns merged=False — a transient block.  This promote() then runs
    # _record_promotion_failure, which must see status='promoted' under the
    # re-lock and bail.
    from sqlalchemy import update as _sa_update

    winner_record = {
        "status": "promoted",
        "candidate_sha": candidate,
        "promoted_sha": "winn3rsh" * 5,
        "pr_url": "http://pr/winner",
    }

    async def _merge_loses_race(*args, **kwargs):
        await db_session.execute(
            _sa_update(rv.ReleaseVersion)
            .where(rv.ReleaseVersion.id == version.id)
            .values(
                status="promoted",
                promoted_sha="winn3rsh" * 5,
                last_qualification=winner_record,
            )
        )
        await db_session.commit()
        return {"merged": False, "conflict": True, "message": "merge conflict"}

    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(side_effect=_merge_loses_race),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # The losing promote() detected the version is already promoted under the
    # re-lock and bailed as already-promoted — it did NOT record a failure.
    assert result["status"] != "promotion_failed"
    assert result.get("already_promoted") is True

    await db_session.refresh(version)
    # CRITICAL: the terminal state is self-consistent — the promoted record set
    # by the winner stands, NOT a contradictory promotion_failed record.
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification["status"] != "promotion_failed"
    # The winner's record is intact — not clobbered by the loser.
    assert version.last_qualification["promoted_sha"] == "winn3rsh" * 5
    assert version.last_qualification["pr_url"] == "http://pr/winner"
    assert "error" not in version.last_qualification


@pytest.mark.asyncio
async def test_record_promotion_failure_still_records_on_open_version(
    db_session, mocker
):
    """Round 9 (data integrity — boundary): the new status!='open' guard in
    _record_promotion_failure must NOT suppress a genuine failure record when
    the version is legitimately still 'open'.  A real merge block on an
    un-raced version is still recorded as promotion_failed and the version goes
    back to stabilize — the guard converts only the already-promoted race, not
    real failures."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "0p3nfa1l" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 81, "html_url": "http://pr/81"}),
    )
    _patch_exact_promotion_pr(mocker, pr_number=81, candidate=candidate)
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": True,
                "message": "merge conflict",
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # The version was open — a real merge conflict is recorded as a failure.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "merge_blocked"


# ===========================================================================
# Round 7 — concern 4: _relock_version_for_promotion_write must not expire()
# un-flushed in-memory mutations on the early-return (lock-not-released) path.
# ===========================================================================


@pytest.mark.asyncio
async def test_relock_lock_not_released_preserves_unflushed_mutation(db_session):
    """Concern 4: on an early-return failure path BEFORE promote()'s
    intermediate commit, the version row is still locked by this transaction.
    _relock_version_for_promotion_write(lock_released=False) must return the
    LIVE instance WITHOUT session.expire() — an expire here would silently
    discard un-flushed in-memory mutations on the version."""
    _project, version = await _make_project_and_version(db_session)

    # Mutate the in-memory instance but do NOT flush/commit — exactly the state
    # an early-return failure path could be in.
    version.last_qualification = {"status": "pending", "sentinel": "unflushed"}
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(version, "last_qualification")

    resolved = await rq._relock_version_for_promotion_write(
        db_session, version, lock_released=False
    )

    # The SAME live instance is returned, and its un-flushed mutation survives —
    # it was NOT discarded by an expire().
    assert resolved is version
    assert resolved.last_qualification == {
        "status": "pending",
        "sentinel": "unflushed",
    }


@pytest.mark.asyncio
async def test_relock_lock_released_rereads_authoritative_db_state(db_session):
    """Concern 4 counterpart: when the lock HAS been released (lock_released=
    True — promote()'s intermediate commit ran + the unlocked GitHub I/O is
    done), the helper re-reads + re-locks the row from AUTHORITATIVE DB state.

    A concurrent finalize / crash-retry could have changed the row while the
    lock was released; the re-read must reflect that committed change rather
    than the now-stale in-memory instance.  This is what makes the
    ``status != "open"`` re-check in _record_promotion_success able to detect a
    concurrent promotion.  Simulated with a direct SQL UPDATE (a stand-in for
    the concurrent writer) — the re-locked instance must reflect it."""
    from sqlalchemy import update as _sa_update

    _project, version = await _make_project_and_version(db_session)

    # Commit a known starting state; the in-memory instance is clean (exactly
    # promote()'s state right after its intermediate commit).
    version.status = "open"
    db_session.add(version)
    await db_session.commit()
    assert version.status == "open"

    # A concurrent writer flips the row in the DB out-of-band (direct SQL — no
    # ORM identity-map interaction), exactly as a parallel promotion would.
    await db_session.execute(
        _sa_update(rv.ReleaseVersion)
        .where(rv.ReleaseVersion.id == version.id)
        .values(status="promoted")
    )
    await db_session.commit()

    resolved = await rq._relock_version_for_promotion_write(
        db_session, version, lock_released=True
    )

    # The re-read reflects the authoritative committed DB state ("promoted"),
    # not the now-stale in-memory instance — so the under-relock status re-check
    # can correctly detect the concurrent promotion.
    assert resolved is not None
    assert resolved.status == "promoted"


# ===========================================================================
# Round 7 — concern 5: dangling-branch cleanup + labeled pr_url
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_pr_missing_number_cleans_up_release_branch(db_session, mocker):
    """Concern 5: when create_pull_request returns a PR with no number, the
    release-v{N} branch (already created) cannot be merged — promote() must
    best-effort delete it so it does not dangle, then record the failure."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    # PR object with NO 'number' field (and not an empty_diff).
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"html_url": "http://pr/nonumber"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )

    result = await rq.promote(
        db_session, version, "n0num000" * 5, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "pr_missing_number"
    # No merge attempted — there is no PR number to merge.
    merge_mock.assert_not_called()
    # The dangling release-v1 branch was cleaned up.
    delete_mock.assert_awaited()
    assert any(c.args[3] == "release-v1" for c in delete_mock.call_args_list)

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_promote_idempotent_success_records_meaningful_pr_url(db_session, mocker):
    """Exact-receipt and exact-empty-diff successes both record useful provenance."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "1d3mp0t0" * 5
    merge_sha = "urlmerge" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    _patch_exact_promotion_pr(
        mocker,
        pr_number=91,
        candidate=candidate,
        merge_sha=merge_sha,
        merged=True,
        discover=True,
    )

    # Case A: an exact merged receipt records the authoritative PR URL.
    result_a = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate, "pr_url": "http://pr/prior"},
    )
    await db_session.commit()
    assert result_a["status"] == "promoted"
    await db_session.refresh(version)
    assert version.last_qualification["pr_url"] == "http://pr/91"
    assert version.promoted_sha == merge_sha

    # Case B: an exact empty diff records an explicit label, not a blank URL.
    _project_b, version_b = await _make_project_and_version(db_session)
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=candidate),
    )
    result_b = await rq.promote(
        db_session,
        version_b,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()
    assert result_b["status"] == "promoted"
    await db_session.refresh(version_b)
    recorded = version_b.last_qualification["pr_url"]
    assert recorded != ""
    assert "empty diff" in recorded.lower()


class TestFmtSubScores:
    """_fmt_sub_scores renders a composite's sub-scores for operator messages."""

    def test_ignores_archival_smoke_sub_score(self):
        out = rq._fmt_sub_scores(
            {"audit": 0.95, "test_erosion": 1.0, "smoketest": 0.8}
        )
        assert out == "audit 0.95 · test 1.00"
        assert "smoke" not in out

    def test_empty_or_none_returns_na(self):
        assert rq._fmt_sub_scores(None) == "n/a"
        assert rq._fmt_sub_scores({}) == "n/a"


@pytest.mark.asyncio
async def test_finalize_dry_run_posts_operator_message(db_session, mocker):
    """A dry-run verdict (promotion disabled) posts a release_dry_run message."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
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
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    msgs = await _messages_for(db_session, project.id, "release_dry_run")
    assert len(msgs) == 1
    assert msgs[0].from_type == "system"
    assert "would promote" in msgs[0].content
    assert msgs[0].context["version_number"] == version.version_number


@pytest.mark.asyncio
async def test_finalize_blocked_posts_operator_message(db_session):
    """A hard blocker posts a release_blocked message naming the blocker."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": ["testing/test_gone.py"],
                "assertion_delta": 0,
                "test_count_delta": -2,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_gone.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; deletion is genuine erosion.",
                }
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    msgs = await _messages_for(db_session, project.id, "release_blocked")
    assert len(msgs) == 1
    assert "blocked from promotion" in msgs[0].content
    assert "deleted a test file" in msgs[0].content
    assert msgs[0].context["blocker"] == "test_file_deleted"


@pytest.mark.asyncio
async def test_finalize_below_threshold_posts_operator_message(db_session):
    """A non-blocked candidate scoring under the threshold posts a
    release_below_threshold message carrying the sub-scores."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    # 2 high + 1 low findings → derived audit 0.68, erosion 1.0, smoketest 0.5
    # (half-passing) → composite 0.722, well under the 0.90 threshold, but no
    # hard blocker (audit present, no test deletion, CI green).
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.5,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [
                {"severity": "low", "summary": "Stale comment in release_scoring."},
                {
                    "severity": "high",
                    "summary": "Unverified retry path.",
                    "verification_note": "Read app/release_promotion.py lines 40-60: retry loop swallows transient errors with no telemetry, confirmed in diff.",
                },
                {
                    "severity": "high",
                    "summary": "Missing null check.",
                    "verification_note": "Read app/release_scoring.py lines 120-135: payload.audit may be None and is dereferenced without guard, confirmed in diff.",
                },
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "fail"},
            ],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[
            {
                "severity": "high",
                "summary": "Unverified retry path.",
                "file": "",
                "verdict": "confirmed",
                "reason": "Independently confirmed: retry loop swallows transient errors with no telemetry.",
            },
            {
                "severity": "high",
                "summary": "Missing null check.",
                "file": "",
                "verdict": "confirmed",
                "reason": "Independently confirmed: None dereference on unguarded payload.audit access.",
            },
        ],
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] is None
    msgs = await _messages_for(db_session, project.id, "release_below_threshold")
    assert len(msgs) == 1
    assert "below the" in msgs[0].content
    assert "Sub-scores:" in msgs[0].content


@pytest.mark.asyncio
async def test_promote_success_posts_operator_message(db_session, mocker):
    """A successful promotion posts a release_promoted message."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 10,
                "test_count_delta": 4,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 55, "html_url": "http://pr/55"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "promoted"
    msgs = await _messages_for(db_session, project.id, "release_promoted")
    assert len(msgs) == 1
    assert "promoted" in msgs[0].content.lower()
    assert msgs[0].context["next_version_number"] == 2


@pytest.mark.asyncio
async def test_promote_failure_posts_operator_message(db_session, mocker):
    """A failed promotion (non-merged merge result) posts a
    release_promotion_failed message."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 92, "html_url": "http://pr/92"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": True,
                "message": "merge conflict",
            }
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.95},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    msgs = await _messages_for(db_session, project.id, "release_promotion_failed")
    assert len(msgs) == 1
    assert "merge failed" in msgs[0].content
    assert msgs[0].context["error"] == "merge_blocked"


@pytest.mark.asyncio
async def test_notify_operator_swallows_failure(db_session, mocker):
    """A messaging failure inside _notify_operator must never propagate — the
    release verdict is already decided by the time it runs."""
    _project, version = await _make_project_and_version(db_session)
    mocker.patch(
        "app.release_promotion.post_event_message",
        new=AsyncMock(side_effect=RuntimeError("messaging is down")),
    )
    # Must return normally despite post_event_message raising.
    await rq._notify_operator(db_session, version, "release_promoted", "summary text")


# ===========================================================================
# Calcification alerting — _check_version_calcification
# ===========================================================================


@pytest.mark.asyncio
async def test_calcification_alert_fires_for_repeated_hard_blockers(db_session):
    """A version in stabilize with >= _MIN_CALCIFICATION_ATTEMPTS hard-blocker
    failures posts one Alert and one release_calcifying message."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "candidate_sha": "abc" * 13 + "a",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert alerted is True
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "calcifying" in alerts[0].title
    assert "ci_not_green" in alerts[0].title
    assert "CI is not green" in alerts[0].message
    # Execution-site tag: ci_not_green is candidate-side, so the alert tells the
    # operator an automain fix clears it next qualification (dir: tag promotion
    # blockers inert-until-promote).
    assert "CANDIDATE-side" in alerts[0].message

    msgs = await _messages_for(db_session, project.id, "release_calcifying")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_calcification_alert_not_fired_for_dry_run_hold(db_session):
    """A version in the dry-run hold (passed qualifier but promotion disabled)
    must NOT trigger a calcification alert — that is healthy, not calcification."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS + 5
    version.last_qualification = {
        "status": "dry_run",
        "would_promote": True,
        "confidence": 0.95,
        "blocker": None,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_calcification_alert_deduplicated(db_session):
    """A second call with the same version+blocker is a no-op — the alert is
    posted only once per distinct stall."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "audit_missing",
        "candidate_sha": "def" * 13 + "d",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_calcification(db_session, version)
    await db_session.commit()
    second = await rq._check_version_calcification(db_session, version)
    await db_session.commit()

    assert first is True
    assert second is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_calcification_alert_refires_on_blocker_change(db_session):
    """When the blocker changes (e.g. ci_not_green → audit_missing) a new alert
    is posted — the old one is for a different stall."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "candidate_sha": "aaa" * 13 + "b",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await rq._check_version_calcification(db_session, version)
    await db_session.commit()
    assert first is True

    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "audit_missing",
        "candidate_sha": "bbb" * 13 + "b",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    second = await rq._check_version_calcification(db_session, version)
    await db_session.commit()
    assert second is True

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_legacy_smoke_deadlock_escalates_when_episode_at_cap(db_session):
    """Legacy-smoke infra_failure with retry_episode >= retry_episode_cap fires
    an operator Alert and halts re-drive — the unconditional deadlock backstop.

    This is the acceptance evidence for the fix: the deployed v26 qualifier
    burned 64 attempts / retry_episode=8 (cap=3) with
    smoketest_task_failed_claude_code_failed errors but no operator escalation,
    because _legacy_smoke_infra_lineage zeroes consecutive_infra_errors and the
    streak cap never fires.  The backstop adds an episode-based override for the
    smoke lineage so a stuck qualifier does alert.
    """
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Set up infra_failure with legacy-smoke lineage: infra_failure_signature
    # starts with "smoketest" so _legacy_smoke_infra_lineage returns True,
    # zeroing the streak.  retry_episode=3 at cap=3 → deadlock backstop fires.
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "smoke123" * 5,
        "infra_failure_signature": "smoketest_task_failed_claude_code_failed",
        "error": "smoketest_task_failed_claude_code_failed",
        "retry_episode": 3,
        "retry_episode_cap": 3,
        "consecutive_infra_errors": 0,  # zeroed by legacy-smoke path
    }
    _flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is False, (
        "legacy-smoke infra_failure at episode cap must NOT re-drive — "
        "deadlock backstop must halt it"
    )

    # An operator Alert must have been raised.
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) >= 1, "deadlock backstop must raise an operator Alert"
    assert any("infra-failure" in a.title.lower() for a in alerts), (
        f"Alert title must mention infra-failure; got: {[a.title for a in alerts]}"
    )

    # infra_failure_loop_escalated flag must be set to prevent duplicate alerts.
    await db_session.refresh(version)
    assert version.last_qualification.get("infra_failure_loop_escalated") is True


@pytest.mark.asyncio
async def test_legacy_smoke_redrive_allowed_when_episode_below_cap(db_session, mocker):
    """Legacy-smoke infra_failure with retry_episode < retry_episode_cap still
    re-drives normally — deadlock backstop only fires at/above cap.
    """
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    _project, version = await _make_project_and_version(db_session, phase="stabilize")

    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "smoke456" * 5,
        "infra_failure_signature": "smoketest_task_failed_claude_code_failed",
        "error": "smoketest_task_failed_claude_code_failed",
        "retry_episode": 2,  # below cap=3 — backstop must not fire
        "retry_episode_cap": 3,
        "consecutive_infra_errors": 0,
    }
    _flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is True, (
        "legacy-smoke infra_failure below episode cap must still re-drive"
    )
    mock_run_qual.assert_called_once()


@pytest.mark.asyncio
async def test_walkthrough_infra_failure_rearms_once_after_site_change(db_session, mocker):
    """A CAPPED walkthrough_e2e infra failure re-arms EXACTLY once when the
    execution-site capability changed — the engine revision that recorded the
    streak is no longer serving — so the newly-repaired dispatch site gets one
    genuine attempt before the cap re-applies.

    This is the acceptance evidence for the fix: the capped V27 walkthrough
    failure cannot automatically resume after the supported-site repair lands
    (the redriver halts permanently at ``_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS``).
    Because every deploy stamps a new ``DEPLOY_SHA``, a capped record whose
    ``engine_sha`` differs from the current serving sha means the repair may
    have taken effect.  The re-arm is one-shot: once a fresh capped failure is
    recorded under the CURRENT engine, the cap re-applies and halts again.
    """
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    old_engine = "a" * 40
    new_engine = "b" * 40
    mocker.patch("app.deployed_sha.get_serving_sha", return_value=new_engine)

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "ca11face" * 5,
        "error": "walkthrough_e2e_task_failed_candidate_stack_boot_failed",
        "infra_failure_signature": (
            "walkthrough_e2e:unknown:"
            "walkthrough_e2e_task_failed_candidate_stack_boot_failed"
        ),
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        "engine_sha": old_engine,
        "infra_failure_loop_escalated": True,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True, (
        "capped walkthrough infra failure must re-arm once after the "
        "execution-site capability change (record engine no longer serving)"
    )
    mock_run_qual.assert_called_once()

    # ONE-SHOT: after a fresh capped failure recorded under the CURRENT serving
    # engine, the re-arm must NOT fire again — the cap re-applies and halts.
    await db_session.refresh(version)
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "ca11face" * 5,
        "error": "walkthrough_e2e_task_failed_candidate_stack_boot_failed",
        "infra_failure_signature": (
            "walkthrough_e2e:unknown:"
            "walkthrough_e2e_task_failed_candidate_stack_boot_failed"
        ),
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS + 1,
        "engine_sha": new_engine,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result_again = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result_again is False, (
        "capped walkthrough infra failure must NOT re-arm a second time once "
        "the record's engine matches the current serving engine"
    )
    assert mock_run_qual.call_count == 1


@pytest.mark.asyncio
async def test_walkthrough_infra_failure_cap_holds_without_site_change(db_session, mocker):
    """A CAPPED walkthrough_e2e infra failure does NOT re-arm when the engine
    that recorded the streak is still serving — the operator Alert + halt path
    fires exactly as before (no capability change, no re-arm)."""
    from app.release_qualifier_redrive import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    same_engine = "c" * 40
    mocker.patch("app.deployed_sha.get_serving_sha", return_value=same_engine)

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "beefcafe" * 5,
        "error": "walkthrough_e2e_task_failed_candidate_stack_boot_failed",
        "infra_failure_signature": (
            "walkthrough_e2e:unknown:"
            "walkthrough_e2e_task_failed_candidate_stack_boot_failed"
        ),
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        "engine_sha": same_engine,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False, (
        "capped walkthrough infra failure with the SAME engine still serving "
        "must halt (cap holds — no re-arm)"
    )
    mock_run_qual.assert_not_called()

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        "capped walkthrough infra failure (no site change) must raise one "
        "operator Alert"
    )
    await db_session.refresh(version)
    assert version.last_qualification.get("infra_failure_loop_escalated") is True


@pytest.mark.asyncio
async def test_walkthrough_spawn_failed_below_cap_is_redriven(db_session, mocker):
    """A walkthrough spawn_failed BELOW the error-redrive cap is re-driven by
    the sweep even when the engine that recorded it is still serving — the
    execution-site outage may have already cleared (a site-proven runner came
    online), and the bounded re-drive costs one attempt."""
    from app.release_qualifier_redrive import _redrive_error_qualification

    same_engine = "c" * 40
    mocker.patch("app.deployed_sha.get_serving_sha", return_value=same_engine)

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "error",
        "error": "spawn_failed",
        "detail": "cannot dispatch qualifier walkthrough_e2e: no eligible runner",
        "candidate_sha": "beefcafe" * 5,
        "engine_sha": same_engine,
        "requested_probes": ["walkthrough_e2e"],
        "error_redrive_count": 1,  # below _MAX_ERROR_REDRIVE_ATTEMPTS
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_error_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True, (
        "walkthrough spawn_failed below cap must re-drive regardless of site "
        "change — the outage may have already cleared"
    )
    mock_run_qual.assert_called_once()


@pytest.mark.asyncio
async def test_walkthrough_spawn_failed_cap_holds_without_site_change(db_session, mocker):
    """A CAPPED walkthrough spawn_failed does NOT re-arm when the engine that
    recorded it is still serving — the site-change re-arm only fires when a
    deploy landed a NEW serving engine sha (no capability change, no re-arm)."""
    from app.release_qualifier_redrive import (
        _MAX_ERROR_REDRIVE_ATTEMPTS,
        _redrive_error_qualification,
    )

    same_engine = "d" * 40
    mocker.patch("app.deployed_sha.get_serving_sha", return_value=same_engine)

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "error",
        "error": "spawn_failed",
        "detail": "cannot dispatch qualifier walkthrough_e2e: no eligible runner",
        "candidate_sha": "f00dcafe" * 5,
        "engine_sha": same_engine,
        "requested_probes": ["walkthrough_e2e"],
        "error_redrive_count": _MAX_ERROR_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_error_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is False, (
        "capped walkthrough spawn_failed with the SAME engine still serving "
        "must halt (cap holds — no re-arm)"
    )
    mock_run_qual.assert_not_called()


@pytest.mark.asyncio
async def test_calcification_not_fired_below_attempt_threshold(db_session):
    """Fewer than _MIN_CALCIFICATION_ATTEMPTS hard-blocker failures do not
    trigger an alert — the stall may still be transient."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS - 1
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    alerted = await rq._check_version_calcification(db_session, version)
    assert alerted is False
    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_sweep_triggers_calcification_check(db_session, mocker):
    """The sweep runs the calcification check after finalizing stuck versions —
    a version that already has enough hard-blocker failures gets an alert."""
    from app.models import Alert as _Alert

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # automain HEAD matches the candidate SHA — run_qualification returns
    # cached_verdict (no-op), preserving the not_promoted status so the
    # calcification check can fire.
    same_sha = "automn0" * 5 + "00000"
    version.qualification_attempts = rq._MIN_CALCIFICATION_ATTEMPTS
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "candidate_sha": same_sha,
    }
    db_session.add(version)
    await db_session.commit()

    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=same_sha))

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    alerts = (
        (
            await db_session.execute(
                select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "calcifying" in alerts[0].title
