"""strategic_spawning: outcome evaluation and landing/deployment truth.

Covers the ``outcome_evaluation`` block, ``_build_recent_interventions``
(``on_automain`` / ``landed_on_integration`` / git-ancestry verification), the
``on_main`` + ``undeployed_directions`` deployment-state block, stranded-PR
credit discounting, and per-project integration-branch resolution.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import uuid
from datetime import timedelta

import pytest
from unittest.mock import AsyncMock, call
from sqlalchemy import select

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    Project,
    Task,
    TaskStatus,
)
from app.strategic_spawning import (
    _spawn_strategic_review,
    _build_recent_interventions,
)
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    _ensure_test_account_pool,
    _lts_split_active,
    _make_project,
    _patch_spawning_deps,
    _seed_legacy_cycles,
    _select_agent_tasks,
    _utcnow_naive,
)


async def _ensure_releaseversion_table(session):
    """Create releaseversion table idempotently."""
    from app.models import ReleaseVersion as _RV

    conn = await session.connection()
    await conn.run_sync(lambda c: _RV.__table__.create(c, checkfirst=True))
    await session.commit()


@pytest.mark.asyncio
async def test_outcome_evaluation_includes_success_evidence_and_retirement_condition(
    db_session,
):
    """outcome_evaluation in strategic payload includes success_evidence and retirement_condition when set."""
    long_evidence = "E" * 700
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev01",
                "status": "active",
                "text": "Direction with outcome criteria",
                "priority": "high",
                "context": {
                    "success_evidence": long_evidence,
                    "retirement_condition": "  retire when fully satisfied  ",
                },
                "research_history": [
                    {
                        "prs_merged": 2,
                        "prs_rejected": 0,
                        "tasks_created": 5,
                        "tasks_failed": 0,
                    }
                ],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]

    assert "outcome_evaluation" in payload
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    assert len(directions) > 0, "outcome_evaluation should have directions"
    outcome = next((o for o in directions if o["id"] == "d-oe-ev01"), None)
    assert outcome is not None, "Direction should be in outcome_evaluation"
    # success_evidence should be capped at 500 chars
    assert outcome.get("success_evidence") == "E" * 500
    # retirement_condition should be stripped and capped
    assert outcome.get("retirement_condition") == "retire when fully satisfied"


@pytest.mark.asyncio
async def test_outcome_evaluation_omits_fields_when_not_set(db_session):
    """outcome_evaluation omits success_evidence/retirement_condition when not in context."""
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev02",
                "status": "active",
                "text": "Direction without outcome criteria",
                "priority": "medium",
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    outcome = next((o for o in directions if o["id"] == "d-oe-ev02"), None)
    assert outcome is not None
    assert "success_evidence" not in outcome
    assert "retirement_condition" not in outcome


@pytest.mark.asyncio
async def test_outcome_evaluation_handles_empty_strings(db_session):
    """outcome_evaluation omits empty/whitespace-only success_evidence/retirement_condition."""
    project = _make_project(
        directions=[
            {
                "id": "d-oe-ev03",
                "status": "active",
                "text": "Direction with empty criteria",
                "priority": "low",
                "context": {
                    "success_evidence": "   ",  # whitespace only
                    "retirement_condition": "",  # empty
                },
                "research_history": [{"prs_merged": 1, "prs_rejected": 0}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    payload = tasks[0].payloads[0]
    outcome_eval = payload.get("outcome_evaluation", {})
    directions = outcome_eval.get("directions", [])

    outcome = next((o for o in directions if o["id"] == "d-oe-ev03"), None)
    assert outcome is not None
    assert "success_evidence" not in outcome
    assert "retirement_condition" not in outcome


# ── on_automain flag in _build_recent_interventions ──────────────────────────


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_when_unverified(db_session):
    """Root tasks (no parent) report on_automain=False when ancestry cannot be verified.

    When GitHub credentials are unavailable the ancestry check is skipped and the
    task is NOT assumed to have landed on the integration branch (fail-closed).
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Direct fix",
        description="No parent",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_true_when_rollup_merged(db_session):
    """Child task reports on_automain=True when its rollup parent's pr_merged=True."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 42},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Child fix",
        description="child",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    by_title = {m["pr_title"]: m for m in merges}
    # Child is on automain via the parent's pr_merged DB flag (fail-open for child
    # tasks is intentional — the flag is a DB-recorded rollup-merge event).
    assert by_title["Child fix"]["on_automain"] is True
    # Rollup parent is a root task with no credentials → ancestry unverifiable → False.
    assert by_title["Rollup parent"]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_when_rollup_not_merged(db_session):
    """Child task reports on_automain=False when its rollup parent is not yet merged."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Unmerged rollup",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        context={"rollup_pr_number": 99},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Staged fix",
        description="child merged into staging only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Staged fix"
    assert merges[0]["on_automain"] is False


# ── landed_on_integration field in _build_recent_interventions ───────────────


@pytest.mark.asyncio
async def test_merges_out_landed_on_integration_false(db_session):
    """merges[] entries expose landed_on_integration=False when the context key is
    explicitly False (merged to rollup staging branch, not integration branch)."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Rollup-only fix",
        description="merged to staging only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "landed_on_integration": False},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["landed_on_integration"] is False


@pytest.mark.asyncio
async def test_merges_out_landed_on_integration_true(db_session):
    """merges[] entries expose landed_on_integration=True when the context key is
    explicitly True (confirmed merge to the integration branch)."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Integration fix",
        description="landed on integration",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "landed_on_integration": True},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["landed_on_integration"] is True


@pytest.mark.asyncio
async def test_merges_out_landed_on_integration_legacy_none(db_session):
    """merges[] entries expose landed_on_integration=None for legacy rows that
    predate the landed_on_integration context key — backward compatible."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Legacy fix",
        description="old row without landed_on_integration key",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["landed_on_integration"] is None


@pytest.mark.asyncio
async def test_recent_merged_titles_landed_on_integration(db_session):
    """per_direction_delta recent_merged_titles entries expose landed_on_integration
    alongside on_automain and merge_verdict."""
    d_id = "d-test-loi"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[{"id": d_id, "text": "test direction", "status": "active"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task merged to rollup staging only (landed_on_integration=False)
    task = Task(
        project_id=project.id,
        title="Staging-only fix",
        description="merged to staging branch only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "direction_id": d_id,
            "landed_on_integration": False,
        },
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    delta = {d["direction_id"]: d for d in result["per_direction_delta"]}
    assert d_id in delta
    titles = delta[d_id].get("recent_merged_titles", [])
    assert len(titles) == 1
    assert titles[0]["landed_on_integration"] is False
    assert titles[0]["title"] == "Staging-only fix"


# ── on_main / undeployed_directions + deployment_state block ──────────────────


@pytest.mark.asyncio
async def test_on_main_mirrors_on_automain_without_lts_split(db_session):
    """With no LTS split (integration branch == promotion target), on_main equals
    on_automain and undeployed_directions is empty — there is no 'landed but not
    deployed' state when merges deploy directly."""
    # No LTS split: integration branch IS the promotion target. _make_project now
    # defaults to a split (automain/main), so request matching branches explicitly.
    project = _make_project(
        vision={"purpose": "test"},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 42},
    )
    db_session.add(parent)
    await db_session.flush()
    child = Task(
        project_id=project.id,
        title="Child fix",
        description="child",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True},
    )
    db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    # on_main == on_automain when there is no split.
    assert (
        by_title["Child fix"]["on_main"] == by_title["Child fix"]["on_automain"] is True
    )
    # No split → nothing can be "landed but not deployed".
    assert result["undeployed_directions"] == []


@pytest.mark.asyncio
async def test_undeployed_directions_lists_integration_only_merges(db_session, mocker):
    """Under an LTS split, a merge reachable from the integration branch but NOT
    from the promotion target is surfaced in undeployed_directions, grouped by
    direction."""
    from unittest.mock import AsyncMock

    # Integration-branch ancestry True, promotion-target ancestry False.
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"merge_commit_sha": "deadbeefcafe"}),
    )
    # is_ancestor_of(token, owner, repo, sha, branch, project_id=...) — branch is a[4].
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=lambda *a, **k: a[4] == "automain"),
    )

    project = _make_project(
        integration_branch="automain",
        promotion_target="main",
        directions=[{"id": "d-x", "status": "active", "text": "warm pool reuse"}],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Wire warm pool ready signal",
        description="root merge on automain only",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 7, "direction_id": "d-x"},
    )
    db_session.add(task)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    merge = result["merges"][0]
    assert merge["on_automain"] is True
    assert merge["on_main"] is False
    undeployed = result["undeployed_directions"]
    assert len(undeployed) == 1
    assert undeployed[0]["direction_id"] == "d-x"
    assert undeployed[0]["direction_text"] == "warm pool reuse"
    assert "Wire warm pool ready signal" in undeployed[0]["merge_titles"]


@pytest.mark.asyncio
async def test_deployment_state_block_in_payload_when_split(db_session, mocker):
    """The strategic payload carries a deployment_state block under an LTS split:
    deployed SHA/status from DeploymentState, the promoted vs open version, and
    automain_ahead_of_main derived from un-promoted merged work."""
    from datetime import timedelta
    from app.models import ReleaseVersion, DeploymentState

    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)
    # The DeploymentState read is now gated on the deploy-level identity (not
    # is_self_repo): make this project the deploy identity so its live container
    # SHA/status is observable to the strategist.
    mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})

    project = _make_project(
        is_self_repo=True,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    from app.runtime_config_state import runtime_config

    mocker.patch.dict(
        runtime_config,
        {"promotion_enabled": True, "promotion_enabled_by_project": {str(project.id): False}},
    )

    now = _utcnow_naive()
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="promoted",
            opened_at=now - timedelta(days=10),
            promoted_at=now - timedelta(days=2),
            promoted_sha="promotedsha0001",
        )
    )
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=2,
            phase="build",
            status="open",
            opened_at=now - timedelta(hours=1),
        )
    )
    # A merged task during the open (un-promoted) version → ahead of main.
    db_session.add(
        Task(
            project_id=project.id,
            title="Merged during v2",
            description="x",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={"pr_merged": True},
            updated_at=now,
        )
    )
    # Live deployed state (self-repo container tracker).
    db_session.add(
        DeploymentState(
            id=1,
            current_sha="deadbeefcafe9999",
            current_revision="rev-123",
            status="STABLE",
        )
    )
    await db_session.commit()

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )

    ds = payload.get("deployment_state")
    assert ds is not None, "deployment_state block missing under an LTS split"
    assert ds["integration_branch"] == "automain"
    assert ds["promotion_target"] == "main"
    assert ds["promotion_enabled"] is False
    assert ds["promotion_enabled_source"] == "project_override"
    assert ds["deploy_identity"] == "acme/widget"
    assert ds["deploy_identity_matches_project"] is True
    assert ds["deployment_state_available"] is True
    # A build-phase version has not entered promotion yet, so it has no serving
    # observation rather than a failed/unconfirmed serving observation.
    assert ds["open_serving_status"] is None
    assert "promotion_enabled=False" in payload.get("prompt_suffix", "")
    assert "revise, split, replace, or park" in payload.get("prompt_suffix", "")
    assert ds["deployed_sha"] == "deadbeefcafe"  # DeploymentState.current_sha, 12-char
    assert ds["deploy_status"] == "STABLE"
    assert ds["promoted_version"] == 1
    assert ds["open_version"] == 2
    assert ds["open_status"] == "open"
    assert ds["tasks_merged_unpromoted"] >= 1
    assert ds["automain_ahead_of_main"] is True


@pytest.mark.asyncio
async def test_deployment_state_and_failed_directive_use_aggregate_failed_truth(
    db_session, mocker, _lts_split_active
):
    """A required non-backend artifact failure must keep the strategist in
    deploy-failed mode even when the backend artifact is serving_confirmed."""
    from app import release_artifacts as artifact_registry
    from app.models import ReleaseDeployArtifact, ReleaseVersion
    from app.release_artifacts import (
        BACKEND_REVISION_ARTIFACT_KIND,
        ReleaseArtifactProvider,
    )

    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)

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

    project = _make_project(
        is_self_repo=True,
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
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
        serving_status="serving_confirmed",
        served_sha="abc123def456",
    )
    current_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        phase="build",
        status="open",
        opened_at=now,
        phase_changed_at=now,
        last_qualification={"status": "not_promoted", "confidence": 0.75},
        qualification_attempts=1,
    )
    db_session.add_all([failed_version, current_version])
    await db_session.commit()
    await db_session.refresh(failed_version)

    db_session.add_all(
        [
            ReleaseDeployArtifact(
                version_id=failed_version.id,
                artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
                serving_status="serving_confirmed",
                served_identifier="abc123def456",
                served_at=now - timedelta(hours=3),
            ),
            ReleaseDeployArtifact(
                version_id=failed_version.id,
                artifact_kind="synthetic_required",
                serving_status="deploy_failed",
                reason_code="synthetic_deadline_elapsed",
            ),
        ]
    )
    await db_session.commit()

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )

    deployment_state = payload.get("deployment_state") or {}
    assert deployment_state["deployed_sha"] is None
    assert deployment_state["promoted_serving_status"] == "deploy_failed"
    assert (
        deployment_state["promoted_deploy_failure_reason"]
        == "synthetic_deadline_elapsed"
    )
    assert "synthetic_required" in (deployment_state["promoted_artifact_gap"] or "")

    suffix = payload.get("prompt_suffix", "")
    assert "deployment crisis" in suffix.lower(), suffix
    assert "synthetic_required" in suffix, suffix


@pytest.mark.asyncio
async def test_deployment_state_absent_without_lts_split(db_session, mocker):
    """A single-branch project (integration branch == promotion target) emits no
    deployment_state block — there is no integration-vs-deployed distinction."""
    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)

    project = _make_project(
        is_self_repo=True,
        integration_branch="trunk",
        promotion_target="trunk",
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )
    assert "deployment_state" not in payload


async def _spawn_self_repo_with_deployment_state(
    db_session,
    mocker,
    *,
    status,
    rolled_back_from_sha=None,
    rollback_count=0,
    rollback_reason=None,
):
    """Spawn a self-repo strategic review under an LTS split with a seeded
    DeploymentState row. Returns (deployment_state block, prompt_suffix)."""
    from datetime import timedelta
    from app.models import ReleaseVersion, DeploymentState

    await _ensure_releaseversion_table(db_session)
    _patch_spawning_deps(mocker)
    # The DeploymentState read is now gated on the deploy-level identity (not
    # is_self_repo): make this project the deploy identity so its live container
    # state is observable to the strategist.
    mocker.patch.dict("os.environ", {"DEPLOY_REPO_IDENTITY": "acme/widget"})

    project = _make_project(
        is_self_repo=True,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        vision={"purpose": "test purpose"},
    )
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
            opened_at=now - timedelta(hours=1),
        )
    )
    db_session.add(
        DeploymentState(
            id=1,
            current_sha="deadbeefcafe9999",
            current_revision="rev-123",
            status=status,
            rolled_back_from_sha=rolled_back_from_sha,
            rollback_count=rollback_count,
            rollback_reason=rollback_reason,
        )
    )
    await db_session.commit()

    assert await _spawn_strategic_review(project, db_session) is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = next(
        t.payloads[0]
        for t in result.scalars().all()
        if t.first_payload.get("type") == "strategic_review"
    )
    return payload.get("deployment_state"), payload.get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_deployment_state_block_carries_rollback_provenance(db_session, mocker):
    """The deployment_state block surfaces the rollback provenance the operator
    UI already shows: rolled-back-from SHA (12-char), count, and reason."""
    ds, _suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="ROLLED_BACK",
        rolled_back_from_sha="badc0ffee0ddf00d",
        rollback_count=2,
        rollback_reason="health check failed after deploy",
    )
    assert ds is not None, "deployment_state block missing under an LTS split"
    assert ds["rolled_back_from_sha"] == "badc0ffee0dd"  # 12-char, like deployed_sha
    assert ds["rollback_count"] == 2
    assert ds["rollback_reason"] == "health check failed after deploy"


@pytest.mark.asyncio
async def test_stabilization_directive_fires_on_rolled_back(db_session, mocker):
    """A ROLLED_BACK deploy status injects a stabilization directive naming the
    reason + count and steering the strategist away from advancing focus."""
    _ds, suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="ROLLED_BACK",
        rolled_back_from_sha="badc0ffee0ddf00d",
        rollback_count=1,
        rollback_reason="health check failed after deploy",
    )
    assert "ROLLED_BACK" in suffix
    assert "STABILIZATION" in suffix
    assert "health check failed after deploy" in suffix
    assert "Consecutive rollbacks: 1" in suffix
    assert "badc0ffee0dd" in suffix
    assert "do NOT advance focus" in suffix


@pytest.mark.asyncio
async def test_stabilization_directive_fires_on_halted(db_session, mocker):
    """A HALTED deploy status injects the repeated-rollback-halt directive."""
    _ds, suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="HALTED",
        rolled_back_from_sha="badc0ffee0ddf00d",
        rollback_count=3,
        rollback_reason="repeated boot crash",
    )
    assert "HALTED" in suffix
    assert "STABILIZATION" in suffix
    assert "manual intervention" in suffix
    assert "Consecutive rollbacks: 3" in suffix
    assert "repeated-rollback halt" in suffix


@pytest.mark.asyncio
async def test_stabilization_directive_absent_when_stable(db_session, mocker):
    """A healthy (STABLE) deploy status emits no stabilization directive."""
    _ds, suffix = await _spawn_self_repo_with_deployment_state(
        db_session,
        mocker,
        status="STABLE",
    )
    assert "STABILIZATION" not in suffix
    assert "ROLLED_BACK" not in suffix
    assert "HALTED" not in suffix


@pytest.mark.asyncio
async def test_rolling_rates_exclude_rollup_children(db_session):
    """Rollup children (parent_task_id set) are not counted as merged in rolling_rates.

    A rollup child's pr_merged=True means its PR landed on the rollup staging branch,
    not the integration branch. Only the rollup parent (root task) should count
    toward the merged counter in rolling_rates when it lands on integration.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent — merged to integration branch
    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(parent)
    await db_session.flush()

    # Two rollup children — each merged to staging only
    for i in range(2):
        child = Task(
            project_id=project.id,
            title=f"Child fix {i}",
            description="child",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent.id,
            context={"pr_merged": True},
        )
        db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    rates = result["rolling_rates"]
    # Only the root parent counts as a merged task; the two children are staging-only
    assert rates["last_7d"]["merged"] == 1, (
        f"Expected 1 integration-landed merge (root task only), "
        f"got {rates['last_7d']['merged']}"
    )


@pytest.mark.asyncio
async def test_window_merge_rate_population_consistent_with_rollup_children(db_session):
    """Both numerator and denominator of merge_rate count only root tasks.

    When a rollup parent and its children all complete in the same window,
    the merge_rate denominator ('finished') must count only root tasks,
    consistent with the numerator ('merged'). If the denominator included
    rollup children, the rate would be artificially deflated: 1 root merged
    over 3 total (parent + 2 children) = 33% instead of 1 root over 1 root = 100%.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent — merged to integration branch
    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True},
    )
    db_session.add(parent)
    await db_session.flush()

    # Two rollup children — each merged to staging only
    for i in range(2):
        child = Task(
            project_id=project.id,
            title=f"Child fix {i}",
            description="child",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            parent_task_id=parent.id,
            context={"pr_merged": True},
        )
        db_session.add(child)
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session)
    rates = result["rolling_rates"]

    # merged should be 1 (root parent only)
    assert rates["last_7d"]["merged"] == 1, (
        f"Expected 1 merged (root task only), got {rates['last_7d']['merged']}"
    )
    # finished should also be 1 (root parent only), NOT 3 (parent + 2 children)
    assert rates["last_7d"]["finished"] == 1, (
        f"Expected 1 finished (root task only), got {rates['last_7d']['finished']}. "
        "Denominator must exclude rollup children for consistent merge_rate."
    )
    # merge_rate should be 1.0 (100%), NOT 0.333 (33%)
    assert rates["last_7d"]["merge_rate"] == 1.0, (
        f"Expected 100% merge_rate, got {rates['last_7d']['merge_rate'] * 100}%. "
        "Rate is deflated when denominator counts rollup children."
    )


@pytest.mark.asyncio
async def test_active_counts_include_old_inflight_tasks_outside_lookback(db_session):
    """Current in-flight work is counted even when the task predates the lookback window."""
    project = _make_project(
        vision={"purpose": "test"},
        directions=[{"id": "d-old", "status": "active", "text": "long-lived lane"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_started = _utcnow_naive() - timedelta(days=35)
    db_session.add(
        Task(
            project_id=project.id,
            title="Long running task",
            description="still in flight",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            created_at=old_started,
            updated_at=old_started,
            context={"direction_id": "d-old", "scope": "large"},
        )
    )
    await db_session.commit()

    result = await _build_recent_interventions(project, db_session, lookback_days=21)
    assert result["active_by_direction"] == {"d-old": 1}
    assert result["active_by_scope"]["large"] == 1


# ── git-ancestry landing verification in _build_recent_interventions ──────────


@pytest.mark.asyncio
async def test_on_automain_false_when_commit_not_ancestor(db_session, mocker):
    """pr_merged=True but commit is NOT an ancestor of automain → on_automain=False.

    The ancestry check returns False and there are no _pr_changed_files for the
    content-aware fallback, so the task is correctly reported as NOT landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Stranded fix",
        description="pr_merged=True but not on automain",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 201},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "stranded-sha",
                "head": {"sha": "head-sha"},
            }
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Stranded fix"
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_true_when_commit_is_ancestor(db_session, mocker):
    """Commit IS a git-ancestor of automain → on_automain=True via ancestry check."""
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Landed fix",
        description="merge SHA is a real ancestor",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 202},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "landed-sha", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Landed fix"
    assert merges[0]["on_automain"] is True


@pytest.mark.asyncio
async def test_on_automain_false_when_ancestry_api_indeterminate(db_session, mocker):
    """is_ancestor_of returning None (API error / diverged) → on_automain=False.

    Degrade-safe convention: indeterminate result must NOT be treated as landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Indeterminate fix",
        description="ancestry API returned None",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 203},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "some-sha", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=None),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "Indeterminate fix"
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_child_verified_via_parent_rollup_pr(db_session, mocker):
    """Rollup child: ancestry is verified against the PARENT's rollup PR, not the child's own.

    A rollup child's own PR merges into the rollup staging branch, so its merge
    commit is never an ancestor of the integration branch once the rollup PR
    squash-merges. The child is on automain iff the parent's rollup PR landed —
    verifying the child's own PR would (wrongly) report on_automain=False.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 500},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Rollup child fix",
        description="child merged into staging branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "pr_number": 999},
    )
    db_session.add(child)
    await db_session.commit()

    def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        return {
            999: {"merge_commit_sha": "child-merge-sha", "head": {"sha": "child-head"}},
            500: {
                "merge_commit_sha": "rollup-merge-sha",
                "head": {"sha": "rollup-head"},
            },
        }[pr_num]

    def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        # The child's own merge commit lives on the staging branch (not an
        # ancestor); only the rollup PR's squash commit is on the integration
        # branch.
        return sha == "rollup-merge-sha"

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    # Child is on automain because the PARENT's rollup PR #500 squash-landed —
    # not because the child's own PR #999 (staging-only) is an ancestor.
    assert by_title["Rollup child fix"]["on_automain"] is True
    assert by_title["Rollup parent"]["on_automain"] is True


@pytest.mark.asyncio
async def test_on_automain_child_false_when_parent_rollup_pr_not_ancestor(
    db_session, mocker
):
    """Rollup child: on_automain=False when the parent's rollup PR has not landed.

    The rollup PR's merge commit is not an ancestor of the integration branch
    (rollup merged into a staging branch that itself never reached automain), so
    the child is correctly reported as NOT landed.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Stranded rollup parent",
        description="rollup",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "rollup_pr_number": 501},
    )
    db_session.add(parent)
    await db_session.flush()

    child = Task(
        project_id=project.id,
        title="Stranded rollup child",
        description="child whose rollup never landed",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        parent_task_id=parent.id,
        context={"pr_merged": True, "pr_number": 998},
    )
    db_session.add(child)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "rollup-merge-sha",
                "head": {"sha": "rollup-head"},
            }
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    by_title = {m["pr_title"]: m for m in result["merges"]}
    assert by_title["Stranded rollup child"]["on_automain"] is False
    assert by_title["Stranded rollup parent"]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_no_pr_number(db_session, mocker):
    """Root task with no pr_number reports on_automain=False even with credentials.

    When credentials resolve but the task has no pr_number or rollup_pr_number in
    its context, no ancestry check can run for it.  The task must NOT be assumed
    landed (fail-closed), so on_automain must be False.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="No-PR root fix",
        description="merged but no pr_number",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        # pr_merged=True but no pr_number/rollup_pr_number → ancestry check skipped
        context={"pr_merged": True},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "No-PR root fix"
    # No pr_number → ancestry check skipped → unverifiable → fail-closed = False
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_on_automain_false_for_root_task_ancestry_api_exception(
    db_session, mocker
):
    """Root task reports on_automain=False when the ancestry API call raises an exception.

    An exception during the ancestry check leaves the task absent from
    _automain_verified.  The fallback must NOT assume landed (fail-closed).
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="API-error root fix",
        description="ancestry API raises",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 777},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "abc123", "head": {"sha": "head-sha"}}
        ),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=RuntimeError("GitHub API unreachable")),
    )

    result = await _build_recent_interventions(project, db_session)
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["pr_title"] == "API-error root fix"
    # API exception → task absent from _automain_verified → fail-closed = False
    assert merges[0]["on_automain"] is False


@pytest.mark.asyncio
async def test_recent_merged_titles_carry_tristate_merge_verdict(db_session, mocker):
    """Each recent_merged_titles entry carries a tri-state merge_verdict.

    on_automain is fail-closed (collapses verified-not-landed and unverifiable
    into False); merge_verdict keeps them apart so cycle-start merge-accounting
    can flag only the genuine not-landed conflict. Two merged tasks on the same
    active direction: one ancestry-verified landed, one ancestry-verified NOT
    landed — both have pr_number, so both are verifiable.
    """
    direction_id = "dir-merge-acct"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    landed = Task(
        project_id=project.id,
        title="Landed on automain",
        description="ancestor of integration branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 301, "direction_id": direction_id},
    )
    stranded = Task(
        project_id=project.id,
        title="Badge says merged but stranded",
        description="merge commit NOT an ancestor",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 302, "direction_id": direction_id},
    )
    db_session.add_all([landed, stranded])
    await db_session.commit()

    def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        return {
            301: {"merge_commit_sha": "landed-sha", "head": {"sha": "h1"}},
            302: {"merge_commit_sha": "stranded-sha", "head": {"sha": "h2"}},
        }[pr_num]

    def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        return sha == "landed-sha"

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )
    verdicts = {t["title"]: t["merge_verdict"] for t in delta["recent_merged_titles"]}
    assert verdicts["Landed on automain"] == "verified_landed"
    assert verdicts["Badge says merged but stranded"] == "verified_not_landed"


@pytest.mark.asyncio
async def test_recent_merged_titles_pending_in_open_rollup_verdict(
    db_session, mocker
):
    """PRs merged into a still-open rollup branch get verdict pending_in_open_rollup.

    When get_pull_request returns state=open (no merge_commit_sha), the task
    is in a staging branch waiting for the rollup to squash-merge. This must
    NOT be classified as verified_not_landed (a real accounting conflict) and
    must NOT trigger the stranded-discount path.
    """
    direction_id = "dir-pending-rollup"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "pending rollup dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Impl merged into open rollup branch",
        description="PR is still open — rollup not yet squash-merged",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 555, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mark_stranded_mock = mocker.patch(
        "app.strategic_spawning.mark_stranded_non_landing"
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open"}),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )
    titles = delta["recent_merged_titles"]
    assert len(titles) == 1
    entry = titles[0]
    assert entry["title"] == "Impl merged into open rollup branch"
    assert entry["merge_verdict"] == "pending_in_open_rollup"
    assert entry["on_automain"] is False

    assert delta["recent_merge_verdict_counts"]["pending_in_open_rollup"] == 1
    assert delta["recent_merge_verdict_counts"]["verified_not_landed"] == 0
    assert "recent_not_landed_titles" not in delta

    mark_stranded_mock.assert_not_called()

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("stranded_discount_applied") is None


@pytest.mark.parametrize(
    ("messages", "expected_verdict", "expected_not_landed_titles"),
    [
        (
            ["Rollup squash\n\nIncluded work:\n- Honor rollup squash bodies"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
        (
            ["Rollup squash\n\nIncluded PRs: #9635, #9639"],
            "verified_landed",
            None,
        ),
        (
            ["Rollup squash\n\nIncluded PRs: #1111"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
        (None, "unverifiable", None),
    ],
    ids=["title-only-not-rescued", "pr-evidence", "absent", "scan-failure"],
)
@pytest.mark.asyncio
async def test_rollup_squash_body_evidence_prevents_not_landed_verdict(
    db_session, mocker, messages, expected_verdict, expected_not_landed_titles
):
    """Recent rollup squash bodies can upgrade false ancestry before accounting."""
    direction_id = "dir-rollup-evidence"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Honor rollup squash bodies",
        description="merge commit was rewritten by a rollup squash",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 9635, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "rewritten-child-sha",
                "head": {"sha": "head-sha"},
            }
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    commit_messages_mock = mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=messages),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )

    assert delta["recent_merged_titles"] == [
        {
            "title": "Honor rollup squash bodies",
            "on_automain": expected_verdict == "verified_landed",
            "merge_verdict": expected_verdict,
            "landed_on_integration": None,
        }
    ]
    if expected_not_landed_titles:
        assert delta["recent_not_landed_titles"] == expected_not_landed_titles
    else:
        assert "recent_not_landed_titles" not in delta
    assert delta["recent_merge_verdict_counts"][expected_verdict] == 1
    assert sum(delta["recent_merge_verdict_counts"].values()) == 1
    commit_messages_mock.assert_any_await(
        "tok", "owner", "repo", "automain", project_id=str(project.id)
    )
    expected_scan_count = 2 if expected_verdict == "verified_landed" else 1
    assert commit_messages_mock.await_count == expected_scan_count


@pytest.mark.parametrize(
    ("messages", "expected_verdict", "expected_not_landed_titles"),
    [
        (
            ["Rollup squash\n\nIncluded PRs: #9635, #9639"],
            "verified_landed",
            None,
        ),
        (
            ["Rollup squash\n\nIncluded work:\n- Honor rollup squash bodies"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
        (
            ["Rollup squash\n\nIncluded PRs: #1111"],
            "verified_not_landed",
            ["Honor rollup squash bodies"],
        ),
    ],
    ids=["pr-match-rescued", "title-only-not-rescued", "wrong-pr-not-rescued"],
)
@pytest.mark.asyncio
async def test_squash_rescue_requires_pr_number_evidence(
    db_session, mocker, messages, expected_verdict, expected_not_landed_titles
):
    """PR-number references are primary rescue evidence; title-only matches do not rescue.

    When a task has a PR number, only a PR number match in the commit messages
    can rescue it from a false NOT-landed verdict. Title substring matches are
    not sufficient for rescue when PR numbers are available.
    """
    direction_id = "dir-pr-primary-evidence"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "pr-number-primary test", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Honor rollup squash bodies",
        description="task with pr number for rescue evidence",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 9635, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "merge_commit_sha": "rewritten-child-sha",
                "head": {"sha": "head-sha"},
            }
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    commit_messages_mock = mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=messages),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )

    assert delta["recent_merged_titles"] == [
        {
            "title": "Honor rollup squash bodies",
            "on_automain": expected_verdict == "verified_landed",
            "merge_verdict": expected_verdict,
            "landed_on_integration": None,
        }
    ]
    if expected_not_landed_titles:
        assert delta["recent_not_landed_titles"] == expected_not_landed_titles
    else:
        assert "recent_not_landed_titles" not in delta
    assert delta["recent_merge_verdict_counts"][expected_verdict] == 1
    assert sum(delta["recent_merge_verdict_counts"].values()) == 1
    commit_messages_mock.assert_any_await(
        "tok", "owner", "repo", "automain", project_id=str(project.id)
    )


# ── stranded-PR credit discount persisted by cycle-start reconciliation ────────


@pytest.mark.asyncio
async def test_verified_not_landed_persists_stranded_discount(db_session, mocker):
    """verified_not_landed tasks get stranded_prs_merged incremented and stranded_discount_applied stamped.

    When _build_recent_interventions classifies a task as verified_not_landed
    (ancestry check returns False), it must call mark_stranded_non_landing and
    discount_stranded_pr_from_direction_credit so the direction credit gate stops
    crediting the stranded merge.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-stranded-discount"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "stranded discount test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 1,
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Stranded non-landing task",
        description="merge commit NOT an ancestor of integration branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "pr_number": 501,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha", "head": {"sha": "h1"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    await _build_recent_interventions(project, db_session)

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("stranded_discount_applied") is True, (
        "stranded_discount_applied must be stamped after cycle-start reconciliation"
    )
    assert ctx.get("landed_on_integration") is False, (
        "mark_stranded_non_landing must stamp landed_on_integration=False"
    )

    await db_session.refresh(project)
    dirs = project.directions or []
    the_dir = next(d for d in dirs if d.get("id") == direction_id)
    history = the_dir.get("research_history", [])
    entry = next(h for h in history if h.get("task_id") == research_task_id)
    assert entry.get("stranded_prs_merged") == 1, (
        "stranded_prs_merged must be incremented to 1 for the matching research_history entry"
    )


@pytest.mark.asyncio
async def test_verified_not_landed_discount_survives_projection_reload(
    db_session, mocker
):
    """The stranded-discount path works on tasks loaded through the function's own projection.

    Regression (2026-07-11 outage): tasks created in the test session stay fully
    loaded in the identity map, so the sibling test above never exercised the
    tasks_in_window load_only(..., raiseload=True) projection. When that
    projection omitted Task.project_id, discount_stranded_pr_from_direction_credit
    raised InvalidRequestError and aborted the entire per-project spawn cycle.
    Expunging the task forces _build_recent_interventions to re-load it through
    its real projection, pinning both the projection contents and the discount.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-stranded-projection"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "stranded discount projection test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 1,
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Stranded non-landing task (projection reload)",
        description="merge commit NOT an ancestor of integration branch",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "pr_number": 503,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id
    # Drop the fully-loaded instance so the tasks_in_window query re-loads it
    # under its load_only(..., raiseload=True) projection — as in production.
    db_session.expunge(task)

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha3", "head": {"sha": "h3"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    # Must not raise, and the discount must actually be applied — a projection
    # missing project_id would silently skip it (best-effort) or, pre-fix, abort
    # the whole spawn cycle.
    await _build_recent_interventions(project, db_session)

    reloaded = await db_session.get(Task, task_id)
    ctx = reloaded.context or {}
    assert ctx.get("stranded_discount_applied") is True, (
        "stranded_discount_applied must be stamped even when the task was loaded "
        "through the tasks_in_window raiseload projection"
    )

    await db_session.refresh(project)
    the_dir = next(d for d in project.directions or [] if d.get("id") == direction_id)
    entry = next(
        h
        for h in the_dir.get("research_history", [])
        if h.get("task_id") == research_task_id
    )
    assert entry.get("stranded_prs_merged") == 1


@pytest.mark.asyncio
async def test_stranded_discount_failure_does_not_abort_interventions_build(
    db_session, mocker, real_orm_expiry
):
    """One failing stranded-PR discount must not poison the spawn cycle.

    discount_stranded_pr_from_direction_credit's best-effort except path calls
    session.rollback() on the session it is handed; a rollback expires every
    loaded instance regardless of expire_on_commit=False. Run inline on the
    shared session (pre-fix), one failed discount made the next iteration's
    ``(t.context or {})`` read — and the caller's later ``project`` reads —
    raise MissingGreenlet and abort the whole spawn cycle. Post-fix each
    queued mark-stranded + discount pair runs on its OWN fresh session, so a
    failure is fully contained: the build completes, the remaining pairs still
    strand + discount, and the shared session's instances stay live.

    Uses the ``real_orm_expiry`` fixture: the suite-wide ``InstanceState._expire``
    no-op (backend/conftest.py) makes rollback-expiry impossible by default,
    so without it this test would pass vacuously against the broken code.
    """
    from sqlalchemy import inspect as sa_inspect

    dir_a, dir_b = "dir-strand-fail-a", "dir-strand-fail-b"
    rt_a, rt_b = str(uuid.uuid4()), str(uuid.uuid4())
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": d_id,
                "text": f"stranded failure isolation {d_id}",
                "status": "active",
                "research_history": [
                    {"task_id": rt, "prs_merged": 1, "verified_prs_merged": 1}
                ],
            }
            for d_id, rt in ((dir_a, rt_a), (dir_b, rt_b))
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    tasks = []
    for i, (d_id, rt) in enumerate(((dir_a, rt_a), (dir_b, rt_b))):
        t = Task(
            project_id=project.id,
            title=f"Stranded non-landing task {d_id}",
            description="merge commit NOT an ancestor of integration branch",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            context={
                "pr_merged": True,
                "pr_number": 600 + i,
                "direction_id": d_id,
                "research_task_id": rt,
            },
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.commit()
    task_id_a, task_id_b = (t.id for t in tasks)
    for t in tasks:
        db_session.expunge(t)

    # Self-check that real_orm_expiry actually restored genuine expiry
    # semantics — otherwise the whole test is vacuous (the suite-wide no-op
    # would make the pre-fix shared-session rollback harmless in tests while
    # it aborts spawn cycles in production).
    await db_session.refresh(project)  # ensure an active transaction to roll back
    await db_session.rollback()
    assert sa_inspect(project).expired, (
        "real_orm_expiry fixture did not restore InstanceState._expire — "
        "rollback-expiry coverage would be vacuous"
    )
    await db_session.refresh(project)  # re-load for the build below

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha9", "head": {"sha": "h9"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    # Pair 1's discount dies inside its own try (directions write blows up) →
    # its best-effort handler logs + rolls back the session it was handed.
    # Pair 2's discount must still go through the REAL locked write.
    from app.task_locking import with_directions_locked as _real_wdl

    _wdl_calls = {"n": 0}

    async def _flaky_wdl(session, project_id, mutate_fn, **kwargs):
        _wdl_calls["n"] += 1
        if _wdl_calls["n"] == 1:
            raise RuntimeError("directions write exploded")
        return await _real_wdl(session, project_id, mutate_fn, **kwargs)

    mocker.patch(
        "app.direction_outcomes.with_directions_locked", side_effect=_flaky_wdl
    )

    result = await _build_recent_interventions(project, db_session)

    # The build completed and BOTH directions were fully processed.
    assert result is not None
    assert _wdl_calls["n"] >= 2, "second pair's discount never ran"
    delta_by_id = {e["direction_id"]: e for e in result["per_direction_delta"]}
    assert set(delta_by_id) >= {dir_a, dir_b}
    for d_id in (dir_a, dir_b):
        assert delta_by_id[d_id].get("recent_merged_titles"), (
            f"direction {d_id} lost its merge data — the loop must survive a "
            "failed stranded-PR discount"
        )

    # F2 guard: the caller (_spawn_strategic_review → source_ref_validation)
    # keeps reading `project` on the shared session after the build. With real
    # expiry active, a shared-session rollback would have expired it and this
    # read would raise MissingGreenlet.
    assert isinstance(project.directions, list)

    from conftest import TestingSessionLocal

    async with TestingSessionLocal() as fresh:
        # Pair 1 (dir_a): its private session rolled back — nothing persisted;
        # it re-strands next cycle and the idempotency stamp must be absent.
        reloaded_a = await fresh.get(Task, task_id_a)
        ctx_a = reloaded_a.context or {}
        assert ctx_a.get("stranded_discount_applied") is None
        assert ctx_a.get("landed_on_integration") is None
        # Pair 2 (dir_b): fully stranded + discounted in its own session
        # despite pair 1's failure.
        reloaded_b = await fresh.get(Task, task_id_b)
        ctx_b = reloaded_b.context or {}
        assert ctx_b.get("landed_on_integration") is False
        assert "pr_merged" not in ctx_b
        assert ctx_b.get("stranded_discount_applied") is True
        fresh_project = await fresh.get(Project, project.id)
        dir_b_entry = next(
            d for d in fresh_project.directions or [] if d.get("id") == dir_b
        )
        hist_b = next(
            h
            for h in dir_b_entry.get("research_history", [])
            if h.get("task_id") == rt_b
        )
        assert hist_b.get("stranded_prs_merged") == 1


@pytest.mark.asyncio
async def test_verified_not_landed_stranded_discount_idempotent(db_session, mocker):
    """A task already carrying stranded_discount_applied=True is not double-counted.

    If a task was already processed by rollup verification (stranded_discount_applied=True
    in context), the cycle-start reconciliation must NOT increment stranded_prs_merged again.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-stranded-idem"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "idempotency test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 1,
                        "stranded_prs_merged": 1,  # already discounted once
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Already stranded and discounted",
        description="discount was already applied by rollup verification",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "pr_number": 502,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
            "landed_on_integration": False,
            "stranded_discount_applied": True,  # already stamped
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"merge_commit_sha": "stranded-sha2", "head": {"sha": "h2"}}
        ),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    await _build_recent_interventions(project, db_session)

    await db_session.refresh(project)
    dirs = project.directions or []
    the_dir = next(d for d in dirs if d.get("id") == direction_id)
    history = the_dir.get("research_history", [])
    entry = next(h for h in history if h.get("task_id") == research_task_id)
    assert entry.get("stranded_prs_merged") == 1, (
        "stranded_prs_merged must not be incremented again when stranded_discount_applied=True"
    )


@pytest.mark.asyncio
async def test_unverifiable_verdict_does_not_apply_stranded_discount(
    db_session, mocker
):
    """Tasks with unverifiable verdict must not trigger the stranded-PR credit discount.

    When ancestry cannot be determined (no GitHub credentials or API error),
    the task gets verdict 'unverifiable' and must NOT be marked stranded.
    """
    research_task_id = str(uuid.uuid4())
    direction_id = "dir-unverifiable"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {
                "id": direction_id,
                "text": "unverifiable test dir",
                "status": "active",
                "research_history": [
                    {
                        "task_id": research_task_id,
                        "prs_merged": 1,
                        "verified_prs_merged": 0,
                    }
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No pr_number → ancestry check is skipped → verdict is "unverifiable"
    task = Task(
        project_id=project.id,
        title="Unverifiable merge",
        description="no pr_number so ancestry check is skipped",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={
            "pr_merged": True,
            "direction_id": direction_id,
            "research_task_id": research_task_id,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Even if GitHub creds are present, no pr_number → unverifiable
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages", new=AsyncMock(return_value=[])
    )

    await _build_recent_interventions(project, db_session)

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("stranded_discount_applied") is None, (
        "unverifiable tasks must not have stranded_discount_applied stamped"
    )
    assert ctx.get("landed_on_integration") is None, (
        "unverifiable tasks must not have landed_on_integration stamped"
    )

    await db_session.refresh(project)
    dirs = project.directions or []
    the_dir = next(d for d in dirs if d.get("id") == direction_id)
    history = the_dir.get("research_history", [])
    entry = next(h for h in history if h.get("task_id") == research_task_id)
    assert entry.get("stranded_prs_merged") is None, (
        "stranded_prs_merged must not be written for unverifiable tasks"
    )


@pytest.mark.asyncio
async def test_rollup_squash_body_evidence_applies_to_promotion_target(
    db_session, mocker
):
    """Squash-body evidence on the promotion target marks rescued work deployed."""
    direction_id = "dir-rollup-main-evidence"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Promoted rollup squash body",
        description="merge commit was rewritten by a promoted rollup squash",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 9635, "direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"merge_commit_sha": "rewritten-child-sha"}),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    commit_messages_mock = mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(
            side_effect=[
                ["Rollup squash\n\nIncluded PRs: #9635"],
                ["Promoted rollup\n\nIncluded PRs: #9635"],
            ]
        ),
    )

    result = await _build_recent_interventions(project, db_session)

    merge = result["merges"][0]
    assert merge["on_automain"] is True
    assert merge["on_main"] is True
    assert result["undeployed_directions"] == []
    assert commit_messages_mock.await_args_list == [
        call("tok", "owner", "repo", "automain", project_id=str(project.id)),
        call("tok", "owner", "repo", "main", project_id=str(project.id)),
    ]


@pytest.mark.asyncio
async def test_recent_merged_titles_cap_preserves_full_merge_accounting_counts(
    db_session, mocker
):
    """The visible merge sample stays capped, but reconciliation metadata keeps
    exact verdict counts and a conflict-title sample from the full window."""

    from app.strategic_spawning import _RECON_LIST_CAP, _RECENT_MERGED_TITLES_CAP

    direction_id = "dir-merge-cap"
    project = _make_project(
        vision={"purpose": "test"},
        directions=[
            {"id": direction_id, "text": "merge accounting dir", "status": "active"}
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    base_time = _utcnow_naive()
    tasks = []
    for i in range(_RECENT_MERGED_TITLES_CAP + 3):
        tasks.append(
            Task(
                project_id=project.id,
                title=f"{'landed' if i < _RECENT_MERGED_TITLES_CAP else 'stranded'} {i}",
                description="merge accounting sample",
                payloads=[{"type": "create"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                context={
                    "pr_merged": True,
                    "pr_number": 400 + i,
                    "direction_id": direction_id,
                },
                created_at=base_time - timedelta(minutes=i + 1),
                updated_at=base_time - timedelta(minutes=i + 1),
            )
        )
    db_session.add_all(tasks)
    await db_session.commit()

    def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        return {
            "merge_commit_sha": (
                f"landed-sha-{pr_num}"
                if pr_num < 400 + _RECENT_MERGED_TITLES_CAP
                else f"stranded-sha-{pr_num}"
            ),
            "head": {"sha": f"h-{pr_num}"},
        }

    def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        return sha.startswith("landed-sha-")

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )
    mocker.patch(
        "app.github.list_recent_commit_messages",
        new=AsyncMock(return_value=[]),
    )

    result = await _build_recent_interventions(project, db_session)
    delta = next(
        d for d in result["per_direction_delta"] if d["direction_id"] == direction_id
    )
    assert len(delta["recent_merged_titles"]) == _RECENT_MERGED_TITLES_CAP
    assert {t["merge_verdict"] for t in delta["recent_merged_titles"]} == {
        "verified_landed"
    }
    assert delta["recent_merge_verdict_counts"] == {
        "verified_landed": _RECENT_MERGED_TITLES_CAP,
        "verified_not_landed": 3,
        "unverifiable": 0,
        "pending_in_open_rollup": 0,
    }
    assert delta["recent_not_landed_titles"] == [
        "stranded 10",
        "stranded 11",
        "stranded 12",
    ]
    assert len(delta["recent_not_landed_titles"]) <= _RECON_LIST_CAP


@pytest.mark.asyncio
async def test_ancestry_verification_runs_with_no_open_transaction(db_session, mocker):
    """Regression: GitHub ancestry I/O must NOT run inside an open DB transaction.

    Root cause of the strategic-cycle outage: ``_build_recent_interventions``
    made sequential, awaited GitHub calls (get_pull_request + is_ancestor_of)
    while the read transaction on ``session`` stayed open. For a large merged
    set the loop exceeded Postgres'
    ``idle_in_transaction_session_timeout`` (60s), the server killed the
    connection, and the next query raised ``InterfaceError: connection is
    closed`` — silently swallowed, so no strategic task ever spawned.

    This test asserts the fix invariant directly: at the moment EACH GitHub
    client method is invoked, ``session.in_transaction()`` is False (the read
    txn was committed/released before any network I/O). It also confirms the
    verified ancestry results still flow through correctly after the connection
    is re-acquired for the post-loop children query.
    """
    project = _make_project(vision={"purpose": "test"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Landed fix",
        description="merge SHA is a real ancestor",
        payloads=[{"type": "create"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context={"pr_merged": True, "pr_number": 777},
    )
    db_session.add(task)
    await db_session.commit()

    # Captured txn-state at each GitHub call site. Every entry MUST be False —
    # any True means network I/O ran inside an open transaction (the bug).
    txn_states: list[bool] = []

    async def _pr_side_effect(token, owner, repo, pr_num, project_id=None):
        txn_states.append(db_session.in_transaction())
        return {"merge_commit_sha": "landed-sha", "head": {"sha": "head-sha"}}

    async def _anc_side_effect(token, owner, repo, sha, ref, project_id=None):
        txn_states.append(db_session.in_transaction())
        return True

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_pr_side_effect),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_anc_side_effect),
    )

    result = await _build_recent_interventions(project, db_session)

    # The GitHub client was actually exercised (guards against a vacuous pass if
    # the verification path were skipped) ...
    assert txn_states, "expected at least one GitHub call to be made"
    # ... and NONE of those calls happened inside an open DB transaction.
    assert all(state is False for state in txn_states), (
        "GitHub ancestry I/O ran while a DB transaction was open — this is the "
        f"idle-in-transaction bug. txn states at call sites: {txn_states}"
    )

    # Behavior preserved: the verified result still lands on the merge entry.
    merges = result["merges"]
    assert len(merges) == 1
    assert merges[0]["on_automain"] is True


# ── Per-project integration_branch resolution (column-based) ─────────────────


class TestGetProjectIntegrationBranch:
    """get_project_integration_branch() — the canonical column-based resolver.

    The resolver fails SAFE and never raises, so it is safe to call from read
    endpoints, background recovery loops, and the protected_branches property.
    Self-repos map an unset integration branch to automain; non-self repos with
    an unset integration branch fall back to main. Refusing to land work on the
    promotion target is enforced at the merge/rollup boundary, not here.
    """

    def test_self_repo_without_integration_branch_resolves_to_automain(self):
        """Self-repos preserve the documented automain fallback when unset."""
        project = _make_project(
            integration_branch=None,
            promotion_target=None,
            is_self_repo=True,
            github_repo="BlueprintAIStudio",
        )
        assert _shared_state.get_project_integration_branch(project) == "automain"

    def test_non_self_repo_without_integration_branch_falls_back_to_main(self):
        """External repos with an unset integration branch fall back to main."""
        project = _make_project(integration_branch=None, is_self_repo=False)
        assert _shared_state.get_project_integration_branch(project) == "main"

    def test_column_override_used(self):
        """Project with integration_branch column set uses that value."""
        project = _make_project(integration_branch="feature-trunk")
        assert _shared_state.get_project_integration_branch(project) == "feature-trunk"

    def test_column_override_ignores_global(self):
        """Column value wins regardless of global state."""
        project = _make_project(integration_branch="my-branch")
        assert _shared_state.get_project_integration_branch(project) == "my-branch"

    def test_two_projects_resolve_independently(self):
        """Two projects with different column values each get their own value."""
        project_a = _make_project(integration_branch="trunk-a")
        project_b = _make_project(integration_branch="trunk-b")
        assert _shared_state.get_project_integration_branch(project_a) == "trunk-a"
        assert _shared_state.get_project_integration_branch(project_b) == "trunk-b"

    def test_project_with_override_and_project_without_are_independent(self):
        """A configured project is unaffected by another project's unset column."""
        project_with = _make_project(integration_branch="custom-trunk")
        project_without = _make_project(integration_branch=None, is_self_repo=False)
        assert (
            _shared_state.get_project_integration_branch(project_with) == "custom-trunk"
        )
        assert _shared_state.get_project_integration_branch(project_without) == "main"

    def test_same_branch_as_promotion_target_returns_configured_value(self):
        """The resolver returns the configured value even when it equals promotion.

        Refusing to land integration work on the promotion target is enforced at
        the merge/rollup boundary; the hot-path resolver must not raise.
        """
        project = _make_project(
            integration_branch="release", promotion_target="release"
        )
        assert _shared_state.get_project_integration_branch(project) == "release"


# ── _annotate_capability_proof_status: proof status annotation ────────────────


