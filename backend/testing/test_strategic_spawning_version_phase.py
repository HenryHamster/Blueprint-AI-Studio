"""strategic_spawning: the version_phase block and its stall nudges.

Covers version-phase payload construction (build failures, aggregate pending
truth, promotion-target gating, bootstrap targets), the stabilize-stall and
build-stall strategic nudges, and the audit-finding hints they point at.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import uuid
from datetime import timedelta

import pytest
from unittest.mock import AsyncMock
from sqlalchemy import select

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
from app.models import (
    Task,
    TaskStatus,
)
from app.strategic_spawning import (
    _spawn_strategic_review,
)
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    _assert_generated_at_iso_utc,
    _ensure_test_account_pool,
    _lts_split_active,
    _make_project,
    _patch_spawning_deps,
    _select_agent_tasks,
    _utcnow_naive,
)


@pytest.mark.asyncio
async def test_version_phase_build_failure_surfaces_in_payload(
    db_session, mocker, _lts_split_active
):
    """When the outer version_phase block assembly fails, the payload must carry a
    compact failure-marker dict (lifecycle_signal='unavailable', signal_status=
    'build_failed') rather than silently omitting the block entirely.

    This is the regression test required by the acceptance gate:
    test_version_phase_build_failure_surfaces_in_payload must run green under a
    real merge-test verdict, and the PR must change non-test production code that
    the test exercises.
    """
    from app.strategic_payload_compaction import (
        _summarize_version_phase,
        _version_phase_floor,
    )

    # Patch current_open_version to raise so the outer try/except fires.
    mocker.patch(
        "app.release_version.current_open_version",
        side_effect=RuntimeError("simulated version_phase build failure"),
    )

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, (
        "version_phase must be present even when assembly fails — "
        "the failure-marker dict should be set, not None"
    )
    assert vp.get("lifecycle_signal") == "unavailable", (
        f"lifecycle_signal must be 'unavailable' on build failure; got {vp!r}"
    )
    assert vp.get("signal_status") == "build_failed", (
        f"signal_status must be 'build_failed'; got {vp.get('signal_status')!r}"
    )
    assert isinstance(vp.get("error"), str) and len(vp["error"]) > 0, (
        "error must be a non-empty bounded string"
    )
    assert len(vp["error"]) <= 240, "error must be bounded to 240 chars"
    assert vp.get("integration_branch") == "automain", (
        "integration_branch must be preserved in the failure marker"
    )
    assert vp.get("promotion_target") == "main", (
        "promotion_target must be preserved in the failure marker"
    )

    # Failure marker must survive both compaction paths so it is not silently
    # dropped under budget pressure.
    normal = _summarize_version_phase(vp)
    assert normal.get("lifecycle_signal") == "unavailable", (
        "lifecycle_signal must survive _summarize_version_phase"
    )
    assert normal.get("signal_status") == "build_failed"
    assert normal.get("integration_branch") == "automain"
    assert normal.get("promotion_target") == "main"

    floor_out = _version_phase_floor(vp)
    assert floor_out.get("lifecycle_signal") == "unavailable", (
        "lifecycle_signal must survive _version_phase_floor"
    )
    assert floor_out.get("signal_status") == "build_failed"


@pytest.mark.asyncio
async def test_version_phase_recent_versions_use_aggregate_pending_truth(
    db_session, mocker, _lts_split_active
):
    """A backend-confirmed promotion stays unshipped in the strategist payload
    while another required artifact is still unconfirmed."""
    from app import release_artifacts as artifact_registry
    from app.models import ReleaseDeployArtifact, ReleaseVersion
    from app.release_artifacts import (
        BACKEND_REVISION_ARTIFACT_KIND,
        ReleaseArtifactProvider,
    )

    await _ensure_releaseversion_table(db_session)

    backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
        BACKEND_REVISION_ARTIFACT_KIND
    ]

    async def _check_synthetic(_session, _version, _artifact, _now):
        return None

    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
            "synthetic_required": ReleaseArtifactProvider(
                artifact_kind="synthetic_required",
                check=_check_synthetic,
                required=True,
            ),
        },
        clear=True,
    )

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    closed = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=10),
        promoted_at=now - timedelta(days=2),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    open_v = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        qualification_attempts=0,
        last_qualification=None,
    )
    db_session.add_all([closed, open_v])
    await db_session.commit()
    await db_session.refresh(closed)

    db_session.add_all(
        [
            ReleaseDeployArtifact(
                version_id=closed.id,
                artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                serving_status="serving_confirmed",
                served_identifier="abc123def456",
                served_at=now - timedelta(days=2),
            ),
            ReleaseDeployArtifact(
                version_id=closed.id,
                artifact_kind="synthetic_required",
                serving_status="unconfirmed",
            ),
        ]
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )

    recent = (payload.get("version_phase") or {}).get("recent_versions") or []
    assert recent, "recent_versions should include the promoted version"
    latest = recent[0]
    artifacts = {entry["artifact_kind"]: entry for entry in latest["artifacts"]}
    assert latest["serving_status"] == "unconfirmed"
    assert latest["qualification_state"]["state"] == "promoted_unconfirmed"
    assert artifacts["synthetic_required"]["serving_status"] == "unconfirmed"

    suffix = payload.get("prompt_suffix", "")
    assert "NOT yet confirmed serving" in suffix, suffix
    assert "synthetic_required" in suffix, suffix


@pytest.mark.asyncio
async def test_version_phase_uses_promotion_target_gate(db_session, mocker):
    """Version-phase LTS context is active only when the resolver sees distinct branches."""
    await _ensure_releaseversion_table(db_session)

    _patch_spawning_deps(mocker)
    current_open_version = mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(return_value=None),
    )
    ensure_initial_release_version = mocker.patch(
        "app.release_version.ensure_initial_release_version",
        new=AsyncMock(return_value=None),
    )

    skipped_project = _make_project(
        integration_branch="trunk",
        promotion_target="trunk",
        vision={"purpose": "test purpose"},
    )
    db_session.add(skipped_project)
    await db_session.commit()
    await db_session.refresh(skipped_project)

    # integration == promotion: the version-phase LTS context is gated off
    # (the resolver fails safe instead of raising), so the review still spawns
    # but without invoking the version lifecycle. current_open_version may still
    # be probed once by best-effort enrichments orthogonal to this gate
    # (criterion-link conflict resolution calls it unconditionally); what must
    # stay closed when the branches are equal is the version bootstrap itself.
    assert await _spawn_strategic_review(skipped_project, db_session) is True
    # Every LTS-gated call site passes ``include_artifacts=True`` (all of them
    # sit under ``_integration_branch != _promotion_target`` in
    # strategic_spawning.py), so gate on that marker rather than a zero total —
    # the unconditional enrichment probe uses the default ``include_artifacts``.
    assert not any(
        c.kwargs.get("include_artifacts") for c in current_open_version.call_args_list
    )
    assert ensure_initial_release_version.call_count == 0

    current_open_version.reset_mock()
    ensure_initial_release_version.reset_mock()

    active_project = _make_project(
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
    db_session.add(active_project)
    await db_session.commit()
    await db_session.refresh(active_project)

    assert await _spawn_strategic_review(active_project, db_session) is True
    # The integration!=promotion gate performs at least one version-phase lookup;
    # compute_qualification_trigger_state may add a second call via release_readiness.
    assert current_open_version.call_count >= 1
    assert ensure_initial_release_version.call_args.kwargs["promotion_target"] == "main"


@pytest.mark.asyncio
async def test_version_phase_bootstrap_passes_custom_promotion_target(
    db_session, mocker
):
    """Bootstrap uses the project promotion target even when integration_branch is main."""
    await _ensure_releaseversion_table(db_session)

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(return_value=None),
    )
    ensure_initial_release_version = mocker.patch(
        "app.release_version.ensure_initial_release_version",
        new=AsyncMock(return_value=None),
    )

    project = _make_project(
        integration_branch="main",
        promotion_target="deploy",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _spawn_strategic_review(project, db_session) is True
    assert ensure_initial_release_version.call_count == 1
    assert (
        ensure_initial_release_version.call_args.kwargs["promotion_target"] == "deploy"
    )


# ── stabilize-stall strategic nudge ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_stabilize_stall_hint_fires_for_old_stabilize_version(
    db_session, mocker, _lts_split_active
):
    """An open stabilize-phase version older than _STABILIZE_STALL_AGE_DAYS with
    no qualification attempt gets a STALLED nudge in the strategic prompt_suffix
    and a stabilize_stall block in version_phase."""
    from datetime import timedelta
    from app.models import ReleaseVersion
    from app.release_qualifier import _STABILIZE_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Version stuck in stabilize well past the age threshold
    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        phase_changed_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        last_qualification=None,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "STALLED" in suffix, (
        f"Expected STALLED nudge in prompt_suffix; got: {suffix}"
    )
    assert "stabilize" in suffix.lower()
    assert "does NOT start automatically" in suffix
    assert "YOU must emit qualify_request" in suffix
    assert "fires AUTOMATICALLY" not in suffix

    vp = payload.get("version_phase")
    assert vp is not None
    stall_block = vp.get("stabilize_stall")
    assert stall_block is not None, (
        "stabilize_stall block should appear in version_phase"
    )
    assert stall_block.get("age_days", 0) >= _STABILIZE_STALL_AGE_DAYS


@pytest.mark.asyncio
async def test_stabilize_hint_points_at_carried_audit_findings(
    db_session, mocker, _lts_split_active
):
    """A stabilize version whose last_qualification carries audit findings gets
    a prompt_suffix that names the findings and tells the agent to target them
    with an audit-mode direction — and the steer points at
    version_phase.finding_resolution (the block that actually carries the
    finding keys + ledger states), not the stripped
    last_qualification.audit_findings list."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.85,
            "blocker": None,
            "sub_scores": {"audit": 0.74, "test_erosion": 1.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
                {"severity": "low", "summary": "Private cross-module import."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "finding_resolution" in suffix, f"Expected findings pointer; got: {suffix}"
    assert "last_qualification.audit_findings" not in suffix
    assert "Token baked into VM image" in suffix
    assert "recommended_mode='audit'" in suffix
    # The project has no active audit/fundamentals-mode direction, so the hint
    # must demand durable executable ownership without making creation itself
    # the goal.
    assert "Ensure one durable direction owns the outcome" in suffix
    assert "otherwise create one" in suffix
    assert "MUST create a new direction" not in suffix
    assert "does NOT count" in suffix

    vp = payload.get("version_phase")
    assert vp is not None
    carried = (vp.get("last_qualification") or {}).get("audit_findings")
    assert carried == [
        {"severity": "high", "summary": "Token baked into VM image."},
        {"severity": "low", "summary": "Private cross-module import."},
    ]
    # The finding-resolution block carries the carried finding's key + ledger
    # state even though the raw audit_findings list is stripped to a count by the
    # compact qualification serializer.
    fr = vp.get("finding_resolution")
    assert fr is not None, "finding_resolution block should carry the finding keys"
    assert fr["gating_total"] == 1
    gating = fr["findings"][0]
    assert gating["severity"] == "high"
    assert gating["state"] == "unresolved"
    assert gating["key"]
    # Audit (0.74) is the lowest active sub-score here, so it IS the binding
    # constraint and the hint should frame it that way.
    assert "the audit sub-score is the binding constraint on promotion" in suffix


@pytest.mark.asyncio
async def test_stabilize_hint_points_at_finding_resolution_block(
    db_session, mocker, _lts_split_active
):
    """When carried audit findings block re-qualification, the stabilize steer
    points the agent at version_phase.finding_resolution — the block that
    ACTUALLY carries the per-finding keys + ledger states — instead of
    version_phase.last_qualification.audit_findings, which the compact
    qualification serializer strips down to a count. The finding_resolution
    block also survives budget-trim compaction, so the pointer stays valid even
    when last_qualification.audit_findings has been removed from the payload."""
    from app.release_scoring import _audit_finding_key
    from app.strategic_spawning import _summarize_version_phase

    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "a" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # The gating (high) finding is left UNRESOLVED — no ledger row — so the
    # carried findings genuinely block re-qualification on the behind-head path.
    _patch_spawning_deps(mocker)
    _patch_candidate_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha="b" * 40
    )
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)

    # The steer points at the block that carries the finding keys, not the
    # stripped audit_findings list.
    assert "version_phase.finding_resolution" in suffix, (
        f"Expected finding_resolution pointer; got: {suffix}"
    )
    assert "version_phase.last_qualification.audit_findings" not in suffix
    assert "UNRESOLVED" in suffix
    assert "fix-first work" in suffix

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    vp = tasks[0].payloads[0].get("version_phase")
    assert vp is not None

    # The payload block carries each gating finding's key + resolution state.
    fr = vp.get("finding_resolution")
    assert fr is not None, "finding_resolution block must be present"
    assert fr["gating_total"] == 1
    assert fr["gating_resolved"] == 0
    by_key = {f["key"]: f for f in fr["findings"]}
    high_key = _audit_finding_key("Token baked into VM image.", None)
    assert high_key in by_key, f"finding keys missing from block: {list(by_key)}"
    assert by_key[high_key]["state"] == "unresolved"
    assert by_key[high_key]["severity"] == "high"

    # The compact version_phase serializer strips last_qualification.audit_findings
    # to a count but keeps the finding_resolution block — proving it is the durable
    # carrier of the keys the steer points at.
    compacted = _summarize_version_phase(vp)
    assert "audit_findings" not in (compacted.get("last_qualification") or {})
    assert compacted["finding_resolution"]["findings"]
    assert compacted["finding_resolution"]["findings"][0]["key"] == high_key


async def _spawn_and_get_stabilize_suffix(project, db_session):
    """Helper: spawn a strategic review and return its prompt_suffix string."""
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    return tasks[0].payloads[0].get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_stabilize_hint_names_lower_dimension_when_audit_not_binding(
    db_session, mocker, _lts_split_active
):
    """When audit flagged findings but test_erosion is the LOWER sub-score,
    the hint must NOT claim audit is the binding constraint — it should point at
    test_erosion as the real limiting dimension."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.6,
            "blocker": None,
            "sub_scores": {"audit": 0.80, "test_erosion": 0.42},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)
    assert "test_erosion sub-score" in suffix
    assert "audit is not the limiting dimension" in suffix
    assert "the audit sub-score is the binding constraint on promotion" not in suffix


@pytest.mark.asyncio
async def test_version_phase_unavailable_marker_on_build_failure(
    db_session, mocker, _lts_split_active
):
    """When the version_phase block assembly encounters an exception, a minimal
    version_phase block with build_unavailable=True is included in the payload,
    so the strategist can distinguish 'build failed' from 'no version exists'."""
    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _patch_spawning_deps(mocker)
    # Force the version_phase block build to fail by mocking
    # current_open_version to raise an exception.
    mocker.patch(
        "app.release_version.current_open_version",
        new=AsyncMock(side_effect=RuntimeError("version_phase build boom")),
    )

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase")
    assert vp is not None, "version_phase block should be present even on build failure"
    assert (
        vp.get("build_unavailable") is True
    ), "version_phase should have build_unavailable=True marker"


def _patch_candidate_currency(mocker, *, resolved, head_sha):
    """Mock the GitHub resolution path used by _resolve_candidate_currency.

    ``resolved`` is the (token, owner, repo) tuple or None returned by
    ``_resolve_github_with_session``; ``head_sha`` is the integration-branch
    HEAD ``get_branch_sha`` reports.
    """
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=resolved),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=head_sha),
    )


def _make_stabilize_version_with_candidate(candidate_sha):
    from app.models import ReleaseVersion
    from app.release_scoring import _audit_finding_key

    now = _utcnow_naive()
    # Carried findings stamped with the deterministic content key the resolution
    # ledger joins against (release_scoring._audit_finding_key) so readiness can be
    # computed exactly as it is for real carried findings.
    _high = {"severity": "high", "summary": "Token baked into VM image."}
    _high["key"] = _audit_finding_key(_high["summary"], None)
    _low = {"severity": "low", "summary": "Private cross-module import."}
    _low["key"] = _audit_finding_key(_low["summary"], None)
    return ReleaseVersion(
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.85,
            "blocker": None,
            "sub_scores": {"audit": 0.74, "test_erosion": 1.0},
            "candidate_sha": candidate_sha,
            "audit_findings": [_high, _low],
        },
    )


async def _ensure_verdict_table(session):
    """Create qualificationverdict table idempotently (mirrors test_release_qualifier pattern)."""
    from app.models import QualificationVerdict as _QV

    conn = await session.connection()
    await conn.run_sync(lambda c: _QV.__table__.create(c, checkfirst=True))
    await session.commit()


async def _ensure_releaseversion_table(session):
    """Create releaseversion table idempotently."""
    from app.models import ReleaseVersion as _RV

    conn = await session.connection()
    await conn.run_sync(lambda c: _RV.__table__.create(c, checkfirst=True))
    await session.commit()


async def _ensure_audit_finding_resolution_table(session):
    """Create the AuditFindingResolution ledger table idempotently."""
    from app.models import AuditFindingResolution as _AFR

    conn = await session.connection()
    await conn.run_sync(lambda c: _AFR.__table__.create(c, checkfirst=True))
    await session.commit()


async def _resolve_finding_in_ledger(
    session, project_id, release_version_id, *, summary, severity, state="resolved"
):
    """Insert a resolution-ledger row for the carried finding keyed by summary."""
    from app.models import AuditFindingResolution
    from app.release_scoring import _audit_finding_key

    row = AuditFindingResolution(
        project_id=project_id,
        release_version_id=release_version_id,
        finding_key=_audit_finding_key(summary, None),
        severity=severity,
        state=state,
    )
    session.add(row)
    await session.commit()


@pytest.mark.asyncio
async def test_stabilize_carried_findings_marked_stale_when_candidate_behind_head(
    db_session, mocker, _lts_split_active
):
    """When the qualification's candidate commit is BEHIND the live integration
    HEAD AND the resolution ledger marks every gating finding resolved-or-stale,
    the carried findings are framed POSSIBLY-STALE, the agent is steered toward a
    fresh qualify_request (trigger MET), and they are NOT asserted as a current
    binding blocker nor escalated into a new audit direction."""
    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "a" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Mark the single gating (high) carried finding resolved in the ledger so the
    # pre-registered re-qualification trigger is MET and the re-qualify steer applies.
    await _resolve_finding_in_ledger(
        db_session,
        project.id,
        version.id,
        summary="Token baked into VM image.",
        severity="high",
        state="resolved",
    )

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha="b" * 40
    )
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)

    assert "POSSIBLY-STALE" in suffix
    assert "qualify_request" in suffix
    assert "trigger is MET" in suffix
    assert candidate[:8] in suffix
    # Must NOT assert the stale findings are a current binding blocker, and must
    # NOT churn a fresh remediation direction at a superseded snapshot.
    assert "the audit sub-score is the binding constraint on promotion" not in suffix
    assert "MUST create a new direction" not in suffix


@pytest.mark.asyncio
async def test_stabilize_hint_requalify_only_when_readiness_signaled(
    db_session, mocker, _lts_split_active
):
    """Trigger-based stabilize steering (the pre-registered re-qualification trigger).

    On the behind-head path the hint steers toward a fresh qualify_request ONLY
    when the finding-resolution ledger marks every gating high/medium finding
    resolved-or-stale. When a gating finding remains UNRESOLVED, the hint instead
    steers toward the outstanding fix-first work (and escalates to create a
    remediation direction) rather than a blanket re-qualification.
    """
    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)
    _patch_spawning_deps(mocker)
    _patch_candidate_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha="b" * 40
    )

    # ---- Scenario A: gating finding RESOLVED in the ledger → trigger MET ----
    ready_project = _make_project(is_self_repo=True, vision={"purpose": "p"})
    db_session.add(ready_project)
    await db_session.commit()
    await db_session.refresh(ready_project)

    ready_version = _make_stabilize_version_with_candidate("a" * 40)
    ready_version.project_id = ready_project.id
    db_session.add(ready_version)
    await db_session.commit()
    await db_session.refresh(ready_version)
    await _resolve_finding_in_ledger(
        db_session,
        ready_project.id,
        ready_version.id,
        summary="Token baked into VM image.",
        severity="high",
        state="resolved",
    )

    ready_suffix = await _spawn_and_get_stabilize_suffix(ready_project, db_session)
    # Re-qualify steer engaged; fix-first / escalation NOT engaged.
    assert "trigger is MET" in ready_suffix
    assert "PREFER emitting a fresh qualify_request" in ready_suffix
    assert "UNRESOLVED" not in ready_suffix
    assert "trigger is NOT met" not in ready_suffix
    assert "MUST create a new direction" not in ready_suffix

    # ---- Scenario B: gating finding UNRESOLVED (no ledger row) → trigger NOT met --
    notready_project = _make_project(is_self_repo=True, vision={"purpose": "p"})
    db_session.add(notready_project)
    await db_session.commit()
    await db_session.refresh(notready_project)

    notready_version = _make_stabilize_version_with_candidate("c" * 40)
    notready_version.project_id = notready_project.id
    db_session.add(notready_version)
    await db_session.commit()
    await db_session.refresh(notready_version)
    # No resolution-ledger row → the high gating finding reads as unresolved.

    notready_suffix = await _spawn_and_get_stabilize_suffix(
        notready_project, db_session
    )
    # Fix-first steer engaged; the blanket re-qualify trigger is explicitly NOT met.
    assert "trigger is NOT met" in notready_suffix
    assert "UNRESOLVED" in notready_suffix
    assert "fix-first work" in notready_suffix
    assert "trigger is MET" not in notready_suffix
    # The carried finding still needs fixing, so the prompt requires durable
    # ownership without assuming that a new direction is always necessary.
    assert "Ensure one durable direction owns the outcome" in notready_suffix
    assert "otherwise create one" in notready_suffix
    assert "MUST create a new direction" not in notready_suffix
    # The ledger only advances on an explicit, evidence-bearing declaration —
    # a merged PR does NOT credit carried findings (see
    # test_ci_completion.test_merge_auto_resolve_no_keys_writes_no_rows). Name
    # the op and where the finding_key comes from so the steer is actionable.
    assert "record_finding_resolution" in notready_suffix
    assert "version_phase.finding_resolution.findings[].key" in notready_suffix
    assert "merging a PR does not credit" in notready_suffix


@pytest.mark.asyncio
async def test_stabilize_carried_findings_live_when_candidate_equals_head(
    db_session, mocker, _lts_split_active
):
    """When the candidate commit equals the live HEAD, the findings are presented
    live with binding-constraint and durable-ownership framing, with no
    POSSIBLY-STALE wording."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "c" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha=candidate
    )
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)

    assert "POSSIBLY-STALE" not in suffix
    assert "the audit sub-score is the binding constraint on promotion" in suffix
    assert "Ensure one durable direction owns the outcome" in suffix
    assert "otherwise create one" in suffix
    assert "MUST create a new direction" not in suffix
    assert "Could not confirm" not in suffix


@pytest.mark.asyncio
async def test_stabilize_carried_findings_degrades_when_sha_unresolvable(
    db_session, mocker, _lts_split_active
):
    """When the integration HEAD can't be resolved (GitHub unavailable), the loop
    degrades to today's behavior but softens the currency assertion with a
    provisional caveat — and never crashes."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "d" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=None, head_sha="e" * 40)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)

    assert "POSSIBLY-STALE" not in suffix
    assert "the audit sub-score is the binding constraint on promotion" in suffix
    assert "Could not confirm" in suffix


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_staleness_includes_generated_at(
    db_session, mocker, _lts_split_active
):
    """When the qualification candidate is BEHIND integration HEAD, the spawned
    strategic_review payload must contain a payload_staleness block with stale=True,
    payload_compiled_sha == candidate_sha, active_candidate_sha == candidate_sha,
    and integration_head_sha == head_sha."""
    await _ensure_releaseversion_table(db_session)
    await _ensure_audit_finding_resolution_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "a" * 40
    head = "b" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=("tok", "owner", "repo"), head_sha=head)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, "payload_staleness must be present when currency resolved"
    assert staleness["stale"] is True, "stale must be True when candidate is behind HEAD"
    assert staleness["verdict"] == "stale", "verdict must be 'stale' when candidate is behind HEAD"
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    assert staleness["payload_compiled_sha"] == candidate
    assert staleness["payload_sha"] == candidate
    assert staleness["active_candidate_sha"] == candidate
    assert staleness["integration_head_sha"] == head
    _assert_generated_at_iso_utc(staleness)
    assert staleness["stale_reason"] == "payload_sha_differs_from_integration_head"


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_staleness_current_when_candidate_matches_head(
    db_session, mocker, _lts_split_active
):
    """When the qualification candidate equals the live HEAD, payload_staleness
    must be present with stale=False."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sha = "c" * 40
    version = _make_stabilize_version_with_candidate(sha)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=("tok", "owner", "repo"), head_sha=sha)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, "payload_staleness must be present when currency resolved"
    assert staleness["stale"] is False, "stale must be False when candidate equals HEAD"
    assert staleness["verdict"] == "fresh", "verdict must be 'fresh' when candidate matches HEAD"
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    assert staleness["payload_sha"] == sha
    assert staleness["payload_compiled_sha"] == sha
    assert staleness["active_candidate_sha"] == sha
    assert staleness["integration_head_sha"] == sha
    _assert_generated_at_iso_utc(staleness)
    assert staleness["stale_reason"] is None


@pytest.mark.asyncio
async def test_spawn_strategic_review_payload_freshness_unknown_when_currency_indeterminate(
    db_session, mocker, _lts_split_active
):
    """When _resolve_candidate_currency returns None (GitHub unreachable or no
    candidate SHA), payload_staleness must be present with verdict='unknown' and
    a checked_at timestamp so the agent cannot mistake it for a confirmed-fresh result."""
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "d" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    # resolved=None means _resolve_github_with_session returned None (no credentials)
    # so _resolve_candidate_currency returns None -> _currency stays None.
    _patch_candidate_currency(mocker, resolved=None, head_sha="e" * 40)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, (
        "payload_staleness must be present even when currency is indeterminate"
    )
    assert staleness.get("verdict") == "unknown", (
        "verdict must be 'unknown' when currency could not be resolved"
    )
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    assert staleness.get("stale") is None or "stale" not in staleness, (
        "stale field must not be set on an unknown-verdict block"
    )


def _patch_candidate_currency_raises(mocker, exc, *, resolved=("tok", "owner", "repo")):
    """Mock the currency GitHub path so ``get_branch_sha`` raises ``exc``.

    Credential resolution succeeds (a transient GitHub failure only happens
    AFTER credentials resolve), and the bounded-retry sleep is neutralized so
    the api_unreachable classification is exercised without wall-clock delay.
    """
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=resolved),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=exc),
    )
    mocker.patch("app.strategic_spawning._CURRENCY_RETRY_DELAY_S", 0)


@pytest.mark.asyncio
async def test_payload_staleness_api_unreachable_verdict_distinct_from_unknown(
    db_session, mocker, _lts_split_active
):
    """A TRANSIENT GitHub failure (5xx) must surface payload_staleness.verdict=
    'api_unreachable' — DISTINCT from the 'unknown' verdict used for genuine local
    indeterminacy — after a bounded retry, and must NOT set ``stale``.

    Regression: an API outage at spawn time must not be reported as a stale/unknown
    payload, which would force the strategist to re-ground every load-bearing
    payload claim.  The endpoint must never crash on the transient failure.
    """
    from fastapi import HTTPException

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "f" * 40
    version = _make_stabilize_version_with_candidate(candidate)
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    # Credentials resolve, but the live branch-HEAD read hits a GitHub 5xx — a
    # transient reachability failure, NOT genuine local indeterminacy.
    _patch_candidate_currency_raises(
        mocker,
        HTTPException(status_code=502, detail="GitHub API error (HTTP 502)."),
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, (
        "payload_staleness must be present even when GitHub is unreachable"
    )
    assert staleness.get("verdict") == "api_unreachable", (
        "a transient GitHub 5xx must produce verdict='api_unreachable', "
        "distinct from the 'unknown' verdict used for genuine indeterminacy"
    )
    assert staleness.get("verdict") != "unknown", (
        "api_unreachable must NOT collapse into the genuine-indeterminacy verdict"
    )
    # An unreachable API is NOT a known-stale payload — never set stale.
    assert "stale" not in staleness or staleness.get("stale") is None, (
        "stale must not be set on an api_unreachable block"
    )
    assert "checked_at" in staleness, "checked_at timestamp must be present"
    _assert_generated_at_iso_utc(staleness)


@pytest.mark.asyncio
async def test_resolve_candidate_currency_typed_verdicts(db_session, mocker):
    """Unit coverage of the typed verdicts produced by _resolve_candidate_currency:
    fresh / stale (resolved), api_unreachable (transient GitHub/transport), and
    unknown (genuine local indeterminacy) each with the right shape."""
    from fastapi import HTTPException
    import httpx

    from app.strategic_spawning import _resolve_candidate_currency

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    candidate = "a" * 40
    mocker.patch("app.strategic_spawning._CURRENCY_RETRY_DELAY_S", 0)

    # Genuine indeterminacy: no candidate SHA on the record → unknown, no GitHub call.
    res = await _resolve_candidate_currency(db_session, project, {}, "main")
    assert res["verdict"] == "unknown"
    assert res["resolved"] is False
    assert res["reason"] == "no_candidate_sha"

    # Genuine indeterminacy: candidate present but repo credentials unavailable.
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=None),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "unknown"
    assert res["reason"] == "repo_credentials_unavailable"
    assert res["candidate_sha"] == candidate

    # Resolved fresh: candidate == HEAD.
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "fresh"
    assert res["resolved"] is True
    assert res["current"] is True
    assert res["head_sha"] == candidate

    # Resolved stale: candidate != HEAD.
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value="b" * 40))
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "stale"
    assert res["current"] is False

    # Transient GitHub 5xx → api_unreachable, distinct from unknown.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=HTTPException(status_code=502, detail="5xx")),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "api_unreachable"
    assert res["resolved"] is False
    assert res["candidate_sha"] == candidate

    # Transport error (timeout) → api_unreachable.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=httpx.ConnectError("boom")),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "api_unreachable"

    # Branch genuinely not found (404) is NOT transient → unknown, not api_unreachable.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail="not found")),
    )
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "unknown"
    assert res["reason"] == "branch_not_found"


@pytest.mark.asyncio
async def test_resolve_candidate_currency_retries_transient_failure(db_session, mocker):
    """The bounded retry must (a) make a SECOND ``get_branch_sha`` attempt after a
    transient blip and resolve fresh/stale when that retry succeeds, and (b) make
    exactly ``_CURRENCY_RETRY_ATTEMPTS`` attempts before classifying
    api_unreachable.  These assertions on ``call_count`` FAIL if the function
    only ever makes a single attempt — the core requested retry behavior."""
    from fastapi import HTTPException

    from app.strategic_spawning import (
        _CURRENCY_RETRY_ATTEMPTS,
        _resolve_candidate_currency,
    )

    # The retry must actually exercise more than one attempt for this to be a
    # meaningful guard; the production default is 2.
    assert _CURRENCY_RETRY_ATTEMPTS >= 2

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    candidate = "a" * 40
    head = "b" * 40
    # No wall-clock delay between the bounded retries.
    mocker.patch("app.strategic_spawning._CURRENCY_RETRY_DELAY_S", 0)
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )

    # (a) First attempt raises a transient 5xx, the retry succeeds with a HEAD
    # that differs from the candidate → resolves to 'stale' (NOT api_unreachable),
    # proving the transient failure was retried rather than classified.
    transient_then_ok = AsyncMock(
        side_effect=[HTTPException(status_code=502, detail="5xx"), head]
    )
    mocker.patch("app.github.get_branch_sha", new=transient_then_ok)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "stale", (
        "a transient first attempt followed by a successful retry must resolve, "
        "not classify api_unreachable"
    )
    assert res["resolved"] is True
    assert res["head_sha"] == head
    assert transient_then_ok.await_count == 2, (
        "the transient first attempt must be retried a second time before resolving"
    )

    # (a') Same, but the retry succeeds with HEAD == candidate → 'fresh'.
    transient_then_fresh = AsyncMock(
        side_effect=[HTTPException(status_code=503, detail="5xx"), candidate]
    )
    mocker.patch("app.github.get_branch_sha", new=transient_then_fresh)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "fresh"
    assert res["current"] is True
    assert transient_then_fresh.await_count == 2

    # (b) Every attempt fails transiently → api_unreachable only AFTER exhausting
    # the full bounded budget of attempts.
    always_transient = AsyncMock(
        side_effect=HTTPException(status_code=502, detail="5xx")
    )
    mocker.patch("app.github.get_branch_sha", new=always_transient)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "api_unreachable"
    assert always_transient.await_count == _CURRENCY_RETRY_ATTEMPTS, (
        "api_unreachable must only be reported after the full bounded retry budget "
        f"of {_CURRENCY_RETRY_ATTEMPTS} attempts is exhausted"
    )

    # (b') A 404 short-circuits — it is not transient, so it must NOT be retried.
    not_found = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="not found")
    )
    mocker.patch("app.github.get_branch_sha", new=not_found)
    res = await _resolve_candidate_currency(
        db_session, project, {"candidate_sha": candidate}, "main"
    )
    assert res["verdict"] == "unknown"
    assert res["reason"] == "branch_not_found"
    assert not_found.await_count == 1, (
        "a non-transient 404 must short-circuit without consuming a retry"
    )


@pytest.mark.asyncio
async def test_payload_staleness_resolves_true_verdict_without_carried_findings(
    db_session, mocker, _lts_split_active
):
    """payload_staleness must carry a real stale/fresh verdict even when the
    version has no carried audit findings (i.e. the stabilize+findings fast-path
    is skipped).  Without the fallback resolution, _currency stays None and the
    payload emits verdict='unknown' on every non-findings cycle."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "a" * 40
    head = "b" * 40
    now = _utcnow_naive()
    # Stabilize phase with a candidate SHA but NO audit_findings — the
    # stabilize+carried-findings guard is skipped, so only the fallback path runs.
    version = ReleaseVersion(
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.90,
            "blocker": None,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0},
            "candidate_sha": candidate,
            "audit_findings": [],
        },
    )
    version.project_id = project.id
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    _patch_candidate_currency(mocker, resolved=("tok", "owner", "repo"), head_sha=head)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    staleness = payload.get("payload_staleness")
    assert staleness is not None, "payload_staleness must be present"
    assert staleness.get("verdict") == "stale", (
        "verdict must be 'stale' (not 'unknown') when candidate SHA is behind HEAD "
        "even without carried audit findings — the fallback path must resolve currency"
    )
    assert staleness.get("stale") is True
    assert staleness.get("active_candidate_sha") == candidate
    assert staleness.get("integration_head_sha") == head
    _assert_generated_at_iso_utc(staleness)


@pytest.mark.asyncio
async def test_stabilize_hint_names_hard_blocker_as_binding_constraint(
    db_session, mocker, _lts_split_active
):
    """When a hard blocker fired, the hint must name the blocker as the binding
    constraint rather than asserting audit (or any numeric sub-score) is."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.0,
            "blocker": "ci_not_green",
            "sub_scores": {"audit": 0.0, "test_erosion": 0.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)
    assert "hard blocker (ci_not_green)" in suffix
    assert "the audit sub-score is the binding constraint on promotion" not in suffix


@pytest.mark.asyncio
async def test_stabilize_hint_excludes_disabled_smoketest_from_binding(
    db_session, mocker, _lts_split_active
):
    """A disabled smoketest dimension carries a 0.0 placeholder that must not be
    treated as the binding constraint — audit (the lowest REAL dimension) is."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.6,
            "blocker": None,
            "smoketest_enabled": False,
            "sub_scores": {"audit": 0.55, "test_erosion": 0.90, "smoketest": 0.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token baked into VM image."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    suffix = await _spawn_and_get_stabilize_suffix(project, db_session)
    # audit (0.55) is the lowest REAL dimension; the smoketest 0.0 placeholder
    # must not win the binding determination.
    assert "the audit sub-score is the binding constraint on promotion" in suffix
    assert "smoketest sub-score" not in suffix


@pytest.mark.asyncio
async def test_qualification_substate_hint_promotion_check_wait(
    db_session, mocker, _lts_split_active
):
    """status='pending' WITH promotion_pending (audit passed, promotion PR open)
    must be surfaced as PROMOTION_CHECK_WAIT — not read as 'audit still running'.
    Regression for incident 2026-06-02 where the agent waited on a promotion it
    should have been watching."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "pending",
            "promotion_pending": {
                "pr_number": 7357,
                "pr_url": "https://github.com/x/y/pull/7357",
                "enabled_at": (now - timedelta(minutes=30)).isoformat(),
            },
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_CHECK_WAIT" in suffix, f"Expected sub-state hint; got: {suffix}"
    assert "required checks" in suffix
    assert "exact base/head pair" in suffix
    assert "still running" in suffix  # the explicit "do NOT read as ..." clause

    vp = payload.get("version_phase") or {}
    qstate = vp.get("qualification_state", {})
    assert qstate.get("state") == "promotion_check_wait"
    # The additive freshness context flows through to the strategic payload.
    assert qstate.get("promotion_age_seconds", 0) >= 1700  # ~30 min
    assert "promotion_past_timeout" in qstate
    assert "promotion_timeout_seconds" in qstate


@pytest.mark.asyncio
async def test_qualification_substate_hint_promotion_failed(
    db_session, mocker, _lts_split_active
):
    """status='promotion_failed' (passed audit, merge failed) must be surfaced as
    PROMOTION_FAILED with the failure reason and a 'fresh qualify' directive —
    not left to read as a still-pending audit."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={
            "status": "promotion_failed",
            "error": "pr_ci_red",
            "detail": "PR #7357 CI conclusively failed",
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_FAILED" in suffix, f"Expected sub-state hint; got: {suffix}"
    assert "pr_ci_red" in suffix
    assert "fresh qualify" in suffix.lower()

    vp = payload.get("version_phase") or {}
    assert vp.get("qualification_state", {}).get("state") == "promotion_failed"


@pytest.mark.asyncio
async def test_qualification_substate_hint_absent_while_auditing(
    db_session, mocker, _lts_split_active
):
    """Bare status='pending' (audit genuinely scoring) must NOT emit the
    promotion sub-state hint — the agent SHOULD wait in that case."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "pending"},
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    payload = tasks[0].payloads[0]
    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_CHECK_WAIT" not in suffix
    assert "PROMOTION_FAILED" not in suffix
    vp = payload.get("version_phase") or {}
    assert vp.get("qualification_state", {}).get("state") == "qualifying"


@pytest.mark.asyncio
async def test_qualification_substate_hint_suppressed_when_verdict_supersedes_pending(
    db_session, mocker, _lts_split_active
):
    """When a finalized QualificationVerdict (terminal_status='promoted') exists
    and last_qualification still says 'pending'+'promotion_pending', the resolver
    substitutes the effective status to 'promoted'.  The prompt-suffix hint must
    use the resolver-applied qualification_state (NOT the raw last_qualification)
    so PROMOTION_CHECK_WAIT is absent — the promotion already resolved."""
    from app.models import QualificationVerdict, ReleaseVersion
    from sqlalchemy.orm.attributes import flag_modified

    await _ensure_releaseversion_table(db_session)
    await _ensure_verdict_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    sha = "f" * 40
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=sha,
            confidence=0.92,
            terminal_status="promoted",
            would_promote=True,
            promotion_enabled=False,
            finalized_at=now,
        )
    )
    # Stale last_qualification: still shows pending+promotion_pending even though
    # the verdict is already finalized.
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": sha,
        "confidence": 0.92,
        "promotion_pending": {
            "pr_number": 9002,
            "pr_url": "https://github.com/x/y/pull/9002",
            "enabled_at": now.isoformat(),
        },
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    assert await _spawn_strategic_review(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "PROMOTION_CHECK_WAIT" not in suffix, (
        "prompt-suffix must NOT assert PROMOTION_CHECK_WAIT when the verdict "
        f"has already finalized as promoted; got suffix: {suffix!r}"
    )
    # The version_phase qualification_state must reflect the resolved outcome
    vp = payload.get("version_phase") or {}
    qs = vp.get("qualification_state", {})
    assert qs.get("state") == "promoted", (
        f"qualification_state must be resolver-applied 'promoted', got: {qs}"
    )


@pytest.mark.asyncio
async def test_stabilize_findings_hint_softens_when_remediation_direction_exists(
    db_session, mocker, _lts_split_active
):
    """When an active audit-mode direction already exists, the hint must NOT
    demand a new one — it should instead tell the agent to confirm the existing
    direction targets the findings."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
        directions=[
            {
                "id": "d-existing",
                "text": "Fix the release audit findings",
                "status": "active",
                "priority": "high",
                "recommended_mode": "audit",
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        qualification_attempts=2,
        last_qualification={
            "status": "not_promoted",
            "confidence": 0.6971,
            "blocker": None,
            "sub_scores": {"audit": 0.47, "test_erosion": 1.0},
            "audit_findings": [
                {"severity": "high", "summary": "Token TTL rollout gap."},
            ],
        },
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")
    assert "Token TTL rollout gap" in suffix
    assert "already have an active audit/fundamentals-mode direction" in suffix
    assert "MUST create a new direction" not in suffix, (
        f"Should not demand a new direction when one exists; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_fires_on_fresh_version(
    db_session, mocker, _lts_split_active
):
    """A version that just opened from a promotion (version_number > 1, never
    qualified) gets a prompt_suffix asserting the prior version promoted and
    directing the strategist to author the new version's goal via new_version_goal."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
        goal=None,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" in suffix, (
        f"Expected post-promotion handoff hint; got: {suffix}"
    )
    assert "intent.current_focus" in suffix
    assert "v2" in suffix and "v1" in suffix
    # The hint must ASSERT the prior version promoted and FORBID re-promoting it,
    # then direct the strategist to author the new version goal via new_version_goal.
    assert "PROMOTED" in suffix
    assert "do NOT" in suffix
    assert "new_version_goal" in suffix
    # Setup and retirement are independent — the hint must signal they can run in parallel.
    assert "concurrent" in suffix
    assert "parallel" in suffix


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_grounds_in_prior_promoted_record(
    db_session, mocker, _lts_split_active
):
    """When the prior version's promoted record is readable, the handoff hint
    states the concrete promotion — its date and final confidence — so the agent
    gets a positive 'it shipped' signal rather than only a 'stale text' warning,
    and is explicitly forbidden from re-qualifying the already-shipped version."""
    from datetime import timedelta
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    promoted_at = now - timedelta(hours=3)
    prior = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=promoted_at,
        last_qualification={"status": "promoted", "confidence": 0.93},
    )
    db_session.add(prior)
    fresh = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
        goal="Promote v1: re-requesting qualification now that findings are fixed",
    )
    db_session.add(fresh)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert "Version handoff" in suffix
    # Grounded in the real promoted record: concrete date + final verdict.
    assert promoted_at.date().isoformat() in suffix, (
        f"Expected the prior version's promotion date in the hint; got: {suffix}"
    )
    assert "confidence 0.93" in suffix
    # And the binding instruction: do not re-qualify the already-shipped version.
    assert "re-qualify v1" in suffix


@pytest.mark.asyncio
async def test_post_promote_handoff_respects_existing_fresh_goal(
    db_session, mocker, _lts_split_active
):
    """A fresh post-promotion version can already have its goal authored
    by the time the strategic review spawns. The handoff suffix must not carry
    stale null-goal instructions that contradict version_phase."""
    from datetime import timedelta
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    promoted_at = now - timedelta(hours=2)
    prior = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=4),
        promoted_at=promoted_at,
        last_qualification={"status": "promoted", "confidence": 0.96},
        serving_status="serving_confirmed",
        served_sha="abc123def456",
        served_at=promoted_at,
    )
    fresh_goal = "Make lifecycle context recovery reliable after promotion."
    fresh = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
        goal=fresh_goal,
    )
    db_session.add_all([prior, fresh])
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    vp = payload.get("version_phase") or {}
    assert vp.get("goal") == fresh_goal

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" in suffix
    assert fresh_goal in suffix or vp.get("goal") == fresh_goal
    assert "goal field on v2 is null" not in suffix
    assert "review v2's existing `version_phase.goal`" in suffix
    assert "keep it if sound" in suffix


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_self_clears_after_first_qualification(
    db_session, mocker, _lts_split_active
):
    """Once a version has been qualified at least once, the handoff hint must
    no longer fire — the agent should be reading real verdict data, not
    refreshing the carried narrative."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "not_promoted", "confidence": 0.85},
        qualification_attempts=1,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" not in suffix, (
        f"Handoff hint must self-clear once a verdict exists; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_hint_skipped_for_bootstrap_version(
    db_session, mocker, _lts_split_active
):
    """The bootstrap version (v1) didn't open from any prior promotion, so
    the handoff hint must NOT fire even when last_qualification is null."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "Version handoff" not in suffix, (
        f"v1 has no prior promotion; handoff hint must not fire. Got: {suffix}"
    )


@pytest.mark.asyncio
async def test_post_promote_handoff_unknown_serving_status_is_not_live(
    db_session, mocker, _lts_split_active
):
    """If the prior promoted record cannot be read, the handoff must fail
    closed instead of implying the previous version is live."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now,
            phase_changed_at=now,
            last_qualification=None,
            qualification_attempts=0,
            goal="carry-over promotion narrative",
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.release_version.recent_closed_versions",
        side_effect=RuntimeError("lookup failed"),
    )

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert "Version handoff" in suffix
    assert "is now live" not in suffix
    assert "serving evidence is unknown or unreadable" in suffix
    assert (
        "Verify lifecycle/deploy state before treating the previous version as live"
        in suffix
    )
    assert "serving_confirmed" in suffix


@pytest.mark.asyncio
async def test_failed_deploy_directive_renders_after_next_version_qualification_started(
    db_session, mocker, _lts_split_active
):
    """Failed-deploy directive must keep rendering even after the next version
    has begun qualification — until a subsequent version is promoted and confirmed
    serving. This ensures the recovery priority remains visible to the strategist
    even when the next version is building/qualifying."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # v1: promoted but deployment failed
    prior_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(hours=3),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="deploy_failed",
        deploy_failure_reason="Cloud Run deployment timed out",
    )
    db_session.add(prior_version)
    # v2: open and already started qualification (condition that used to clear handoff)
    current_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "not_promoted", "confidence": 0.75},
        qualification_attempts=1,  # Has started qualifying
    )
    db_session.add(current_version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    # The post-promotion handoff must have self-cleared (fresh version has qualified)
    assert "Version handoff" not in suffix, (
        f"Post-promotion handoff should self-clear after first qualification; got: {suffix}"
    )
    # But the failed-deploy directive must be present
    assert "deployment crisis" in suffix.lower() or "failed deploy" in suffix.lower(), (
        f"Failed-deploy directive must render even after next version has qualified; got: {suffix}"
    )
    assert "v1" in suffix, f"Directive must reference the failed v1; got: {suffix}"
    assert "deploy_failed" in suffix or "DID NOT confirm serving" in suffix, (
        f"Directive must reference deployment failure; got: {suffix}"
    )
    assert "Cloud Run deployment timed out" in suffix, (
        f"Directive must include failure reason; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_failed_deploy_directive_clears_when_newer_version_serving_confirmed(
    db_session, mocker, _lts_split_active
):
    """Failed-deploy directive must clear once a subsequent version is promoted
    AND confirmed serving. This prevents the recovery priority from lingering
    after it has actually been resolved by a newer promotion."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # v1: promoted but deployment failed
    failed_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=10),
        promoted_at=now - timedelta(days=5),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="deploy_failed",
        deploy_failure_reason="Cloud Run crashed on startup",
    )
    db_session.add(failed_version)
    # v2: promoted and serving confirmed (recovery successful)
    recovery_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(days=1),
        last_qualification={"status": "promoted", "confidence": 0.95},
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    db_session.add(recovery_version)
    # v3: open and ready for next iteration
    current_version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification=None,
        qualification_attempts=0,
    )
    db_session.add(current_version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    # The failed-deploy directive must NOT be present because v2 is the latest
    # promoted version and it is serving_confirmed (not deploy_failed)
    assert (
        "deployment crisis" not in suffix.lower() and "deploy_failed" not in suffix
    ), (
        f"Failed-deploy directive must clear when recovery is confirmed serving; got: {suffix}"
    )


@pytest.mark.asyncio
async def test_failed_deploy_directive_text_states_same_sha_recovery_clears(
    db_session, mocker, _lts_split_active
):
    """The crisis directive text must correctly state that confirming THIS deploy
    serving (a recovery of the SAME promoted SHA) clears it — not only a
    subsequent promotion. The old wording wrongly implied a same-SHA recovery
    could never clear it."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    failed_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(hours=3),
        last_qualification={"status": "promoted", "confidence": 0.93},
        serving_status="deploy_failed",
        deploy_failure_reason="boot crash, recovered late",
    )
    db_session.add(failed_version)
    # An open v2 — the directive only fires while a version is open.
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now,
            phase_changed_at=now,
            last_qualification=None,
            qualification_attempts=0,
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert "deployment crisis" in suffix.lower(), f"directive missing; got: {suffix}"
    # The corrected wording: same-SHA recovery clears it; no subsequent version
    # is required.
    assert "SAME promoted SHA" in suffix, (
        f"directive must state same-SHA recovery clears it; got: {suffix}"
    )
    assert "do NOT need to cut and promote a subsequent" in suffix, (
        f"directive must not imply only a subsequent version clears it; got: {suffix}"
    )
    # The misleading old phrasing must be gone.
    assert (
        "remains active until a subsequent version is promoted AND confirmed serving"
        not in suffix
    )


@pytest.mark.asyncio
async def test_failed_deploy_directive_clears_on_same_sha_recovery(
    db_session, mocker, _lts_split_active
):
    """A same-SHA recovery (the single promoted version's serving_status flips
    deploy_failed -> serving_confirmed, no subsequent version) clears the crisis
    directive — the directive is computed at view time from serving_status."""
    from app.models import ReleaseVersion

    await _ensure_releaseversion_table(db_session)

    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    recovered_version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="promoted",
        opened_at=now - timedelta(days=5),
        promoted_at=now - timedelta(hours=6),
        last_qualification={"status": "promoted", "confidence": 0.93},
        # Recovery flipped the SAME promoted SHA back to serving_confirmed.
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    db_session.add(recovered_version)
    # An open v2 so the directive WOULD fire if v1 were still deploy_failed —
    # making the "directive cleared" assertion meaningful rather than vacuous.
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now,
            phase_changed_at=now,
            last_qualification=None,
            qualification_attempts=0,
        )
    )
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    suffix = tasks[0].payloads[0].get("prompt_suffix", "")

    assert (
        "deployment crisis" not in suffix.lower() and "deploy_failed" not in suffix
    ), f"Crisis directive must clear on a same-SHA recovery; got: {suffix}"


@pytest.mark.asyncio
async def test_stabilize_stall_hint_suppressed_for_dry_run(
    db_session, mocker, _lts_split_active
):
    """A dry-run hold (passing candidate, promotion_enabled=False) is healthy
    and must NOT trigger the stalled nudge."""
    from datetime import timedelta
    from app.models import ReleaseVersion
    from app.release_qualifier import _STABILIZE_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        phase="stabilize",
        status="open",
        opened_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        phase_changed_at=now - timedelta(days=_STABILIZE_STALL_AGE_DAYS + 5),
        last_qualification={"status": "dry_run", "candidate_sha": "abc123"},
    )
    db_session.add(version)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "STALLED" not in suffix, (
        "dry_run hold should suppress the stabilize-stall nudge"
    )
    vp = payload.get("version_phase") or {}
    assert "stabilize_stall" not in vp, (
        "stabilize_stall block must not appear for dry_run hold"
    )


# ── build-stall strategic nudge ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_stall_nudge_counts_build_phase_merges_not_version_lifetime(
    db_session, mocker, _lts_split_active
):
    """A version that cycled build→stabilize→build must NOT fire the build-stall
    nudge prematurely based on the inflated version-lifetime merge count.

    Setup: version re-entered the build phase recently (phase_changed_at = now).
    There are many pre-phase-change tasks (version-lifetime count > threshold)
    but zero post-phase-change tasks (phase-scoped count = 0).  The nudge must
    NOT fire because neither age nor phase-scoped count exceed the threshold.
    """
    from app.models import ReleaseVersion
    from app.release_qualifier import _BUILD_STALL_MERGED_TASKS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # Version just re-entered build phase (age = 0 days)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        phase="build",
        status="open",
        opened_at=now - timedelta(days=60),
        phase_changed_at=now,  # just re-entered build from stabilize
    )
    db_session.add(version)
    await db_session.commit()

    # Seed tasks merged BEFORE the phase transition (pre-phase-change, version-lifetime)
    pre_phase_count = _BUILD_STALL_MERGED_TASKS + 5
    for i in range(pre_phase_count):
        t = Task(
            project_id=project.id,
            title=f"Pre-phase task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-old"},
        )
        t.updated_at = now - timedelta(days=30 + i)
        db_session.add(t)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "BUILD — STALLED" not in suffix, (
        "build-stall nudge must NOT fire when phase-scoped count is 0 "
        f"(only version-lifetime count {pre_phase_count} exceeds threshold); got: {suffix}"
    )
    vp = payload.get("version_phase") or {}
    assert "build_stall" not in vp, (
        "build_stall block must not appear when phase-scoped merge count is below threshold"
    )


@pytest.mark.asyncio
async def test_build_stall_nudge_tasks_merged_field_is_phase_scoped(
    db_session, mocker, _lts_split_active
):
    """When the build-stall nudge fires, version_phase['build_stall']['tasks_merged']
    must equal only the post-phase_changed_at merge count, not the version-lifetime count.
    """
    from app.models import ReleaseVersion
    from app.release_qualifier import _BUILD_STALL_AGE_DAYS

    await _ensure_releaseversion_table(db_session)

    project = _make_project(
        is_self_repo=True,
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow_naive()
    # Version re-entered build phase past the age threshold
    phase_entry = now - timedelta(days=_BUILD_STALL_AGE_DAYS + 2)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=6,
        phase="build",
        status="open",
        opened_at=now - timedelta(days=60),
        phase_changed_at=phase_entry,
    )
    db_session.add(version)
    await db_session.commit()

    # Tasks merged BEFORE phase re-entry (should NOT be counted by nudge)
    for i in range(10):
        t = Task(
            project_id=project.id,
            title=f"Pre-entry task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-old"},
        )
        t.updated_at = phase_entry - timedelta(days=1 + i)
        db_session.add(t)

    # Tasks merged AFTER phase re-entry (should be counted)
    post_phase_count = 3
    for i in range(post_phase_count):
        t = Task(
            project_id=project.id,
            title=f"Post-entry task {i}",
            description="test",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True, "direction_id": "d-new"},
        )
        t.updated_at = phase_entry + timedelta(hours=1 + i)
        db_session.add(t)
    await db_session.commit()

    _patch_spawning_deps(mocker)
    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    payload = tasks[0].payloads[0]

    # Nudge fires because age threshold is exceeded
    suffix = payload.get("prompt_suffix", "")
    assert "BUILD — STALLED" in suffix, (
        f"Expected build-stall nudge to fire on age; got: {suffix}"
    )

    vp = payload.get("version_phase") or {}
    stall_block = vp.get("build_stall")
    assert stall_block is not None, "build_stall block must appear in version_phase"
    reported_merged = stall_block.get("tasks_merged")
    assert reported_merged == post_phase_count, (
        f"build_stall.tasks_merged should be phase-scoped count {post_phase_count}, "
        f"not version-lifetime count; got {reported_merged}"
    )


# ── outcome_evaluation read path for success_evidence/retirement_condition ────




# ── goal-authoring compliance ledger in the version_phase block ──────────────


async def _spawn_and_get_version_phase(project, db_session):
    """Spawn one strategic_review and return its version_phase payload block."""
    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = [
        t
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    ]
    assert tasks, "Expected a strategic_review task"
    return tasks[0].payloads[0].get("version_phase")


async def _make_open_version_with_omissions(
    db_session, project, *, goal, cycles, last_cycle=None
):
    from app.models import ReleaseVersion

    now = _utcnow_naive()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        goal=goal,
        goal_omission_cycles=cycles,
        goal_omission_last_cycle=last_cycle,
    )
    db_session.add(version)
    await db_session.commit()
    return version


@pytest.mark.asyncio
async def test_version_phase_names_the_unanswered_goal_streak(
    db_session, mocker, _lts_split_active
):
    """A re-ask that has already been ignored N times must SAY so.

    ``goal_needed`` alone is an identical request every cycle. Carrying the
    streak lets the strategist see that this exact output was requested and not
    delivered N times, instead of reading a first-time ask forever.
    """
    await _ensure_releaseversion_table(db_session)
    project = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _make_open_version_with_omissions(
        db_session, project, goal=None, cycles=2, last_cycle="cycle-2"
    )

    _patch_spawning_deps(mocker)
    vp = await _spawn_and_get_version_phase(project, db_session)

    assert vp is not None
    assert vp.get("goal_needed") is True
    assert vp.get("goal_omission_cycles") == 2, (
        "The open ask must carry how many completed cycles have already missed "
        f"it; got version_phase={vp!r}"
    )


@pytest.mark.asyncio
async def test_version_phase_omits_goal_streak_when_there_is_nothing_to_report(
    db_session, mocker, _lts_split_active
):
    """No streak and no open ask both mean: do not spend payload budget on it."""
    await _ensure_releaseversion_table(db_session)

    first_ask = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(first_ask)
    await db_session.commit()
    await db_session.refresh(first_ask)
    await _make_open_version_with_omissions(
        db_session, first_ask, goal=None, cycles=0
    )

    _patch_spawning_deps(mocker)
    vp = await _spawn_and_get_version_phase(first_ask, db_session)
    assert vp is not None
    assert vp.get("goal_needed") is True
    assert "goal_omission_cycles" not in vp, (
        "A first-time ask has no history to report; emitting a zero would be noise."
    )

    answered = _make_project(is_self_repo=True, vision={"purpose": "test purpose"})
    db_session.add(answered)
    await db_session.commit()
    await db_session.refresh(answered)
    await _make_open_version_with_omissions(
        db_session, answered, goal="An objective already authored.", cycles=3
    )

    vp_answered = await _spawn_and_get_version_phase(answered, db_session)
    assert vp_answered is not None
    assert "goal_needed" not in vp_answered
    assert "goal_omission_cycles" not in vp_answered, (
        "The ask is closed; a stale streak must not keep being re-reported."
    )
