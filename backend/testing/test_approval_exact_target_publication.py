"""PostgreSQL proof for immutable approval-target publication.

The lifecycle and operator mutation run in independent sessions on purpose.
An identity-map-only test cannot prove that the exact reviewed target survived
the transaction boundary where the human approval claim is minted.
"""

import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.admin_approval_authority import (
    APPROVAL_HOLD_PROVENANCE_KEY,
    approval_merge_intent_writer,
    establish_independent_approval_hold,
)
from app.admin_task_approvals import (
    decide_operator_approval,
    execute_approval_followup,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.schemas import AgentStatusPayload
from app.shared_state import derive_task_branch
from app.task_lifecycle import (
    _adopt_exact_active_review,
    _process_post_commit,
    _publish_exact_review_refusal,
)


def _require_postgres(session) -> None:
    if resolve_dialect_name(session) != "postgresql":
        pytest.skip("exact publication authority requires PostgreSQL locks")


def _open_pr(*, head: str, base: str, branch: str, base_branch: str) -> dict:
    return {
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": head, "ref": branch},
        "base": {"sha": base, "ref": base_branch},
        "merge_commit_sha": "",
    }


def _mock_lifecycle_github(
    mocker,
    *,
    pr_snapshot: dict,
    scan_blocked: bool = False,
) -> None:
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value=pr_snapshot),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "failed_logs": "",
                "details": [],
            }
        ),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "README.md"}]),
    )
    mocker.patch("app.github.submit_pr_review", new=AsyncMock(return_value={"id": 1}))
    mocker.patch("app.github.create_pr_comment", new=AsyncMock())
    mocker.patch("app.github.update_check_run", new=AsyncMock())
    mocker.patch("app.github.merge_pull_request", new=AsyncMock())
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new=AsyncMock(
            return_value={
                "blocked": scan_blocked,
                "findings": [],
                "summary": "blocked" if scan_blocked else "clean",
                "report": "",
            }
        ),
    )
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.review_pipeline.broadcast_to_project", new=AsyncMock())
    mocker.patch("app.task_lifecycle._check_and_trigger_rollup", new=AsyncMock())


async def _seed_project(db_session, *, name: str) -> Project:
    project = Project(
        name=name,
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.parametrize("peer_hold", [False, True], ids=["fresh", "peer-hold"])
@pytest.mark.asyncio
async def test_critical_approve_publishes_exact_target_before_operator_intent(
    db_session, mocker, peer_hold
):
    """APPROVE binds a previously empty original before FALSE creates intent."""

    _require_postgres(db_session)
    project = await _seed_project(db_session, name="Critical exact publication")
    original = Task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "create",
                "prompt": "change the critical path",
                "base_branch": "automain",
            }
        ],
        # Legacy rows stored a boolean; publication must keep supporting it.
        context={"touched_critical": True},
    )
    peer_message = "Peer failure owns this approval episode"
    if peer_hold:
        original.status = TaskStatus.FAILED
        original.latest_status_message = peer_message
        establish_independent_approval_hold(
            original,
            reason="peer hold",
            owner="peer worker",
        )
    db_session.add(original)
    await db_session.commit()
    await db_session.refresh(original)
    assert "review_loop" not in (original.context or {})

    pr_number = 7311
    branch = derive_task_branch(original.id, "create", "blueprint")
    head_sha = "critical-head-7311"
    base_sha = "critical-base-7311"
    target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    review = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "automain",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "automain",
                "original_task_id": str(original.id),
                "iteration": 1,
                "review_target": target,
            }
        },
    )
    db_session.add(review)
    db_session.add(
        Artifact(
            task_id=original.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        )
    )
    await db_session.commit()
    review_id = review.id
    original_id = original.id

    pr_snapshot = _open_pr(
        head=head_sha,
        base=base_sha,
        branch=branch,
        base_branch="automain",
    )
    _mock_lifecycle_github(mocker, pr_snapshot=pr_snapshot)
    payload = AgentStatusPayload(
        task_id=review_id,
        status=TaskStatus.COMPLETED,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        artifact_type=ArtifactType.PULL_REQUEST,
        verdict_json=json.dumps(
            {
                "verdict": "APPROVE",
                "feedback": "exact target is clean",
                "issues": [],
                "reviewed_head_sha": head_sha,
                "reviewed_base_sha": base_sha,
            }
        ),
    )
    await _process_post_commit(
        review_id,
        payload,
        _session_factory=AsyncSessionLocal,
    )

    await db_session.rollback()
    async with AsyncSessionLocal() as decision_session:
        source = await decision_session.get(Task, original_id)
        assert source is not None
        loop = dict((source.context or {}).get("review_loop") or {})
        assert source.requires_approval is True
        assert loop == {
            **loop,
            "project_id": str(project.id),
            "pr_number": pr_number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "branch": branch,
            "base_branch": "automain",
            "original_task_id": str(original_id),
            "review_target": target,
            "review_approved": True,
        }
        assert any(
            row.get("review_task_id") == str(review_id)
            for row in loop["review_history"]
        )
        assert loop["issue_ledger"]["open"] == []
        provenance = (source.context or {})[APPROVAL_HOLD_PROVENANCE_KEY]
        if peer_hold:
            assert source.status == TaskStatus.FAILED
            assert source.latest_status_message == peer_message
            assert provenance["owner"] == "peer worker"
            assert provenance["reason"] == "peer hold"
            return
        assert provenance["owner"] == "task_lifecycle"
        assert provenance["reason"] == "security_critical_files"

        decided, decision, _ = await decide_operator_approval(
            original_id,
            False,
            decision_session,
            modified_by="USER",
        )
        assert decided.requires_approval is True
        assert decision.followup_kind == "critical_merge"
        assert (decision.claim or {})["head_sha"] == head_sha
        assert (decision.claim or {})["base_sha"] == base_sha
        writer = approval_merge_intent_writer(
            decision,
            owner="owner",
            repo="repo",
            branch=branch,
            base_branch="automain",
        )
        ledger = await writer(
            decision_session,
            {
                **target,
                "head_ref": branch,
                "base_ref": "automain",
                "state": "open",
                "merged": False,
                "draft": False,
                "mergeable": True,
                "mergeable_state": "clean",
                "merge_commit_sha": "",
            },
        )
        assert ledger is not None
        assert ledger["state"] == "not_sent"
        assert ledger["head_sha"] == head_sha
        assert ledger["base_sha"] == base_sha


@pytest.mark.asyncio
async def test_scan_hold_release_adopts_exact_active_review_without_duplicate(
    db_session, mocker
):
    """A scan-gated source binds and later releases through one exact child."""

    _require_postgres(db_session)
    project = await _seed_project(db_session, name="Scan exact adoption")
    pr_number = 7312
    source = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "create",
                "prompt": "large staged change",
                "base_branch": "research/cycle-exact",
            }
        ],
        context={"scope": "large"},
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    assert "review_loop" not in (source.context or {})

    branch = derive_task_branch(source.id, "create", "blueprint")
    head_sha = "scan-head-7312"
    base_sha = "scan-base-7312"
    target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    existing_review = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "research/cycle-exact",
                "original_task_id": str(source.id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "research/cycle-exact",
                "original_task_id": str(source.id),
                "iteration": 1,
                "review_target": target,
            }
        },
    )
    db_session.add(existing_review)
    db_session.add(
        Artifact(
            task_id=source.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        )
    )
    await db_session.commit()
    source_id = source.id
    review_id = existing_review.id

    _mock_lifecycle_github(
        mocker,
        pr_snapshot=_open_pr(
            head=head_sha,
            base=base_sha,
            branch=branch,
            base_branch="research/cycle-exact",
        ),
        scan_blocked=True,
    )
    await _process_post_commit(
        source_id,
        AgentStatusPayload(
            task_id=source_id,
            status=TaskStatus.COMPLETED,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
            artifact_type=ArtifactType.PULL_REQUEST,
        ),
        _session_factory=AsyncSessionLocal,
    )

    await db_session.rollback()
    async with AsyncSessionLocal() as release_session:
        adopted_source = await release_session.get(Task, source_id)
        assert adopted_source is not None
        loop = dict((adopted_source.context or {}).get("review_loop") or {})
        assert adopted_source.requires_approval is True
        assert loop["review_target"] == target
        assert loop["head_sha"] == head_sha
        assert loop["base_sha"] == base_sha
        assert loop["review_history"] == []
        assert loop["issue_ledger"] == {"open": [], "resolved": []}

        _task, decision, _ = await decide_operator_approval(
            source_id,
            False,
            release_session,
            modified_by="USER",
        )
        assert decision.followup_kind == "review_spawn"
        assert (decision.claim or {})["head_sha"] == head_sha
        assert (decision.claim or {})["base_sha"] == base_sha
        released, changes = await execute_approval_followup(
            decision,
            release_session,
        )
        assert released.requires_approval is False
        assert changes["spawned_review"] == str(review_id)

    async with AsyncSessionLocal() as verify_session:
        reviews = (
            (
                await verify_session.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.id != source_id,
                        Task.payloads[0]["type"].as_string() == "review",
                    )
                )
            )
            .scalars()
            .all()
        )
        final_source = await verify_session.get(Task, source_id)
        assert final_source is not None
        assert len(reviews) == 1
        assert reviews[0].id == review_id
        assert ((final_source.context or {})["review_loop"])["review_target"] == target
        assert final_source.requires_approval is False


@pytest.mark.parametrize("race_winner", ["terminal_child", "replacement_source"])
@pytest.mark.asyncio
async def test_exact_review_adoption_rechecks_independent_race(db_session, race_winner):
    """A terminal child or replacement source wins without stale publication."""

    _require_postgres(db_session)
    project = await _seed_project(
        db_session,
        name=f"Exact adoption race {race_winner}",
    )
    project_id = project.id
    pr_number = 7313
    branch = "blueprint/exact-adoption-race"
    base_branch = "research/cycle-race"
    target = {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "base_sha": "race-base-7313",
        "head_sha": "race-head-7313",
    }
    source = Task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[{"type": "create", "base_branch": base_branch}],
        context={},
    )
    review = Task(
        project_id=project_id,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
            }
        ],
        context={"review_loop": {"review_target": target}},
    )
    db_session.add(source)
    db_session.add(review)
    await db_session.commit()
    source_id = source.id
    review_id = review.id

    async with AsyncSessionLocal() as winner:
        if race_winner == "terminal_child":
            winning_review = await winner.get(Task, review_id)
            assert winning_review is not None
            winning_review.status = TaskStatus.COMPLETED
            winner.add(winning_review)
        else:
            winning_source = await winner.get(Task, source_id)
            assert winning_source is not None
            replacement_target = {
                **target,
                "base_sha": "replacement-base",
                "head_sha": "replacement-head",
            }
            winning_source.context = {
                "replacement_sentinel": "preserve",
                "review_loop": {
                    "project_id": str(project_id),
                    "pr_number": pr_number,
                    "branch": branch,
                    "base_branch": base_branch,
                    "base_sha": replacement_target["base_sha"],
                    "head_sha": replacement_target["head_sha"],
                    "review_target": replacement_target,
                },
            }
            establish_independent_approval_hold(
                winning_source,
                reason="replacement hold",
                owner="race winner",
            )
            winner.add(winning_source)
        await winner.commit()

    assert not await _adopt_exact_active_review(
        db_session,
        project_id=project_id,
        pr_number=pr_number,
        original_task_id=source_id,
        review_task_id=review_id,
        target=target,
        branch=branch,
        base_branch=base_branch,
    )
    if race_winner == "terminal_child":
        assert await _publish_exact_review_refusal(
            db_session,
            project_id=project_id,
            pr_number=pr_number,
            original_task_id=source_id,
            target=None,
            branch=branch,
            base_branch=base_branch,
        )
    else:
        assert (
            await _publish_exact_review_refusal(
                db_session,
                project_id=project_id,
                pr_number=pr_number,
                original_task_id=source_id,
                target=target,
                branch=branch,
                base_branch=base_branch,
            )
            is None
        )

    await db_session.rollback()
    async with AsyncSessionLocal() as verify_session:
        fresh = await verify_session.get(Task, source_id)
        assert fresh is not None
        if race_winner == "terminal_child":
            assert "review_loop" not in (fresh.context or {})
            assert fresh.requires_approval is True
            assert (fresh.context or {})[APPROVAL_HOLD_PROVENANCE_KEY][
                "owner"
            ] == "task_lifecycle"
        else:
            loop = (fresh.context or {})["review_loop"]
            assert (fresh.context or {})["replacement_sentinel"] == "preserve"
            assert loop["head_sha"] == "replacement-head"
            assert loop["base_sha"] == "replacement-base"
            assert fresh.requires_approval is True


@pytest.mark.asyncio
async def test_exact_review_adoption_rejects_new_rollup_pr_before_loop_rearm(
    db_session,
):
    """A durable replacement rollup marker fences an old exact review child."""

    _require_postgres(db_session)
    project = await _seed_project(db_session, name="Rollup marker publication race")
    project_id = project.id
    old_pr_number = 7320
    replacement_pr_number = 7321
    branch = "research/rollup-publication-race-staging"
    base_branch = "research/rollup-publication-race-base"
    target = {
        "project_id": str(project_id),
        "pr_number": old_pr_number,
        "base_sha": "rollup-race-base-7320",
        "head_sha": "rollup-race-head-7320",
    }
    old_loop = {
        "project_id": str(project_id),
        "pr_number": old_pr_number,
        "branch": branch,
        "base_branch": base_branch,
        "base_sha": target["base_sha"],
        "head_sha": target["head_sha"],
        "review_target": target,
    }
    source = Task(
        project_id=project_id,
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        payloads=[{"type": "research", "base_branch": base_branch}],
        context={
            "rollup_pr_number": old_pr_number,
            "review_loop": old_loop,
        },
    )
    review = Task(
        project_id=project_id,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "review",
                "pr_number": old_pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "original_task_id": None,
            }
        ],
        context={"review_loop": {"review_target": target}},
    )
    db_session.add(source)
    db_session.add(review)
    await db_session.flush()
    source_id = source.id
    review_id = review.id
    review.payloads = [
        {
            **review.first_payload,
            "original_task_id": str(source_id),
        }
    ]
    db_session.add(review)
    await db_session.commit()

    # The rollup writer publishes its durable replacement marker first. Its
    # nested review-loop rearm is intentionally still pending in this window.
    async with AsyncSessionLocal() as winner:
        winning_source = await winner.get(Task, source_id)
        assert winning_source is not None
        winning_source.context = {
            **(winning_source.context or {}),
            "rollup_pr_number": replacement_pr_number,
            "replacement_sentinel": "preserve",
        }
        winner.add(winning_source)
        await winner.commit()

    assert not await _adopt_exact_active_review(
        db_session,
        project_id=project_id,
        pr_number=old_pr_number,
        original_task_id=source_id,
        review_task_id=review_id,
        target=target,
        branch=branch,
        base_branch=base_branch,
    )

    await db_session.rollback()
    async with AsyncSessionLocal() as verify_session:
        fresh_source = await verify_session.get(Task, source_id)
        fresh_review = await verify_session.get(Task, review_id)
        assert fresh_source is not None
        assert fresh_review is not None
        assert (fresh_source.context or {})["rollup_pr_number"] == (
            replacement_pr_number
        )
        assert (fresh_source.context or {})["replacement_sentinel"] == "preserve"
        assert (fresh_source.context or {})["review_loop"] == old_loop
        assert fresh_review.status == TaskStatus.QUEUED


@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.FAILED, TaskStatus.CANCELLED],
    ids=lambda status: status.value,
)
@pytest.mark.asyncio
async def test_initial_review_publication_rejects_status_only_terminal_race(
    db_session, mocker, terminal_status
):
    """A post-proof terminal source cannot publish an initial review child."""

    _require_postgres(db_session)
    project = await _seed_project(
        db_session,
        name=f"Initial publication {terminal_status.value} race",
    )
    project_id = project.id
    pr_number = 7314
    source = Task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "create",
                "prompt": "large staged status race",
                "base_branch": "research/status-race",
            }
        ],
        context={"scope": "large"},
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    source_id = source.id
    branch = derive_task_branch(source_id, "create", "blueprint")
    target = {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "base_sha": "initial-race-base-7314",
        "head_sha": "initial-race-head-7314",
    }
    db_session.add(
        Artifact(
            task_id=source_id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        )
    )
    await db_session.commit()

    _mock_lifecycle_github(
        mocker,
        pr_snapshot=_open_pr(
            head=str(target["head_sha"]),
            base=str(target["base_sha"]),
            branch=branch,
            base_branch="research/status-race",
        ),
    )

    async def _race_at_publication(
        source_task, task_type, source_session, *args, **kwargs
    ):
        assert task_type == "review"
        assert kwargs["review_target"] == target
        async with AsyncSessionLocal() as peer_session:
            raced_source = await peer_session.get(Task, source_id)
            assert raced_source is not None
            raced_source.status = terminal_status
            raced_source.requires_approval = False
            raced_source.latest_status_message = "peer terminal status wins"
            peer_session.add(raced_source)
            await peer_session.commit()

        authoritative_source = await kwargs["source_spawn_authority"](source_session)
        if authoritative_source is None:
            return None, False

        # Model the canonical atomic child/publication boundary. Before the
        # fix, the freshly loaded terminal row still reaches this commit.
        review = Task(
            project_id=project_id,
            parent_task_id=source_id,
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            payloads=[
                {
                    "type": "review",
                    "pr_number": pr_number,
                    "branch": branch,
                    "base_branch": "research/status-race",
                    "original_task_id": str(source_id),
                }
            ],
            context={"review_loop": {"review_target": target}},
        )
        source_session.add(review)
        await source_session.flush([review])
        await kwargs["source_spawn_publication"](authoritative_source, source_session)
        await source_session.commit()
        return review, True

    mocker.patch(
        "app.task_lifecycle._spawn_pipeline_task",
        new=AsyncMock(side_effect=_race_at_publication),
    )
    await _process_post_commit(
        source_id,
        AgentStatusPayload(
            task_id=source_id,
            status=TaskStatus.COMPLETED,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
            artifact_type=ArtifactType.PULL_REQUEST,
        ),
        _session_factory=AsyncSessionLocal,
    )

    await db_session.rollback()
    async with AsyncSessionLocal() as verify_session:
        fresh = await verify_session.get(Task, source_id)
        assert fresh is not None
        reviews = (
            (
                await verify_session.execute(
                    select(Task).where(
                        Task.project_id == project_id,
                        Task.id != source_id,
                        Task.payloads[0]["type"].as_string() == "review",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert reviews == []
        assert fresh.status == terminal_status
        assert fresh.latest_status_message == "peer terminal status wins"
        assert fresh.requires_approval is False
        assert "review_target" not in dict(
            (fresh.context or {}).get("review_loop") or {}
        )


@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.FAILED, TaskStatus.CANCELLED],
    ids=lambda status: status.value,
)
@pytest.mark.parametrize(
    "refusal_has_target", [False, True], ids=["no-target", "exact"]
)
@pytest.mark.asyncio
async def test_adoption_and_refusal_reject_terminal_source_status(
    db_session, terminal_status, refusal_has_target
):
    """Fresh Task authority rejects status-only terminal adoption/refusal."""

    _require_postgres(db_session)
    project = await _seed_project(
        db_session,
        name=f"Terminal adoption refusal {terminal_status.value}",
    )
    project_id = project.id
    pr_number = 7315
    branch = "blueprint/terminal-publication-race"
    base_branch = "research/terminal-publication-race"
    target = {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "base_sha": "terminal-base-7315",
        "head_sha": "terminal-head-7315",
    }
    source = Task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[{"type": "create", "base_branch": base_branch}],
        context={},
    )
    review = Task(
        project_id=project_id,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
            }
        ],
        context={"review_loop": {"review_target": target}},
    )
    db_session.add_all([source, review])
    await db_session.commit()
    source_id = source.id
    review_id = review.id

    async with AsyncSessionLocal() as peer_session:
        raced_source = await peer_session.get(Task, source_id)
        assert raced_source is not None
        raced_source.status = terminal_status
        raced_source.latest_status_message = "terminal source authority"
        peer_session.add(raced_source)
        await peer_session.commit()

    assert not await _adopt_exact_active_review(
        db_session,
        project_id=project_id,
        pr_number=pr_number,
        original_task_id=source_id,
        review_task_id=review_id,
        target=target,
        branch=branch,
        base_branch=base_branch,
    )
    assert (
        await _publish_exact_review_refusal(
            db_session,
            project_id=project_id,
            pr_number=pr_number,
            original_task_id=source_id,
            target=target if refusal_has_target else None,
            branch=branch,
            base_branch=base_branch,
        )
        is None
    )

    await db_session.rollback()
    async with AsyncSessionLocal() as verify_session:
        fresh = await verify_session.get(Task, source_id)
        assert fresh is not None
        assert fresh.status == terminal_status
        assert fresh.latest_status_message == "terminal source authority"
        assert fresh.requires_approval is False
        assert "review_loop" not in (fresh.context or {})


@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.FAILED, TaskStatus.CANCELLED],
    ids=lambda status: status.value,
)
@pytest.mark.asyncio
async def test_critical_approve_rejects_status_only_terminal_race(
    db_session, mocker, terminal_status
):
    """Critical APPROVE cannot recreate a hold or EVALUATING status after loss."""

    _require_postgres(db_session)
    project = await _seed_project(
        db_session,
        name=f"Critical status race {terminal_status.value}",
    )
    original = Task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "create",
                "prompt": "critical status race",
                "base_branch": "automain",
            }
        ],
        context={"touched_critical": True},
    )
    db_session.add(original)
    await db_session.commit()
    await db_session.refresh(original)
    original_id = original.id
    pr_number = 7316
    branch = derive_task_branch(original_id, "create", "blueprint")
    head_sha = "critical-race-head-7316"
    base_sha = "critical-race-base-7316"
    target = {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    review = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "automain",
                "original_task_id": str(original_id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "automain",
                "original_task_id": str(original_id),
                "iteration": 1,
                "review_target": target,
            }
        },
    )
    db_session.add(review)
    db_session.add(
        Artifact(
            task_id=original_id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        )
    )
    await db_session.commit()
    review_id = review.id

    _mock_lifecycle_github(
        mocker,
        pr_snapshot=_open_pr(
            head=head_sha,
            base=base_sha,
            branch=branch,
            base_branch="automain",
        ),
    )
    import app.task_lifecycle as lifecycle

    real_lock = lifecycle._lock_exact_review_original
    race_committed = False

    async def _race_then_lock(*args, **kwargs):
        nonlocal race_committed
        if not race_committed:
            async with AsyncSessionLocal() as peer_session:
                raced_original = await peer_session.get(Task, original_id)
                assert raced_original is not None
                raced_original.status = terminal_status
                raced_original.requires_approval = False
                raced_original.latest_status_message = "peer terminal status wins"
                peer_session.add(raced_original)
                await peer_session.commit()
            race_committed = True
        return await real_lock(*args, **kwargs)

    mocker.patch(
        "app.task_lifecycle._lock_exact_review_original",
        new=_race_then_lock,
    )
    await _process_post_commit(
        review_id,
        AgentStatusPayload(
            task_id=review_id,
            status=TaskStatus.COMPLETED,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
            artifact_type=ArtifactType.PULL_REQUEST,
            verdict_json=json.dumps(
                {
                    "verdict": "APPROVE",
                    "feedback": "exact target is clean",
                    "issues": [],
                    "reviewed_head_sha": head_sha,
                    "reviewed_base_sha": base_sha,
                }
            ),
        ),
        _session_factory=AsyncSessionLocal,
    )

    assert race_committed is True
    await db_session.rollback()
    async with AsyncSessionLocal() as verify_session:
        fresh = await verify_session.get(Task, original_id)
        assert fresh is not None
        assert fresh.status == terminal_status
        assert fresh.latest_status_message == "peer terminal status wins"
        assert fresh.requires_approval is False
        assert "review_loop" not in (fresh.context or {})
        assert (fresh.context or {}).get("touched_critical") is True


@pytest.mark.asyncio
async def test_no_target_operator_false_preserves_hold_without_claim(db_session):
    """A source with no exact base/head cannot mint review-spawn authority."""

    _require_postgres(db_session)
    project = await _seed_project(db_session, name="No-target operator refusal")
    source = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "create",
                "prompt": "review target unavailable",
                "branch": "blueprint/no-target",
                "base_branch": "research/no-target",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 7317,
                "branch": "blueprint/no-target",
                "base_branch": "research/no-target",
            }
        },
    )
    establish_independent_approval_hold(
        source,
        reason="review spawn refused: target unavailable",
        owner="task_lifecycle",
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    source_id = source.id
    db_session.add(
        Artifact(
            task_id=source_id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/7317",
        )
    )
    await db_session.commit()
    original_context = copy.deepcopy(source.context or {})

    with pytest.raises(HTTPException) as exc_info:
        await decide_operator_approval(
            source_id,
            False,
            db_session,
            modified_by="USER",
        )
    assert exc_info.value.status_code == 409
    await db_session.rollback()

    async with AsyncSessionLocal() as verify_session:
        fresh = await verify_session.get(Task, source_id)
        assert fresh is not None
        assert fresh.status == TaskStatus.COMPLETED
        assert fresh.requires_approval is True
        assert fresh.context == original_context


@pytest.mark.parametrize(
    "race_winner",
    [TaskStatus.FAILED, TaskStatus.CANCELLED, "replacement"],
    ids=["failed", "cancelled", "replacement"],
)
@pytest.mark.asyncio
async def test_generic_approval_gate_preserves_fresh_terminal_or_replacement(
    db_session, mocker, race_winner
):
    """A completed old review cannot revive or relabel a newer source episode."""

    _require_postgres(db_session)
    project = await _seed_project(db_session, name=f"Generic gate {race_winner}")
    project_id = project.id
    pr_number = 7318
    branch = "blueprint/generic-approval-race"
    base_branch = "research/generic-approval-race"
    target = {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "base_sha": "generic-base-7318",
        "head_sha": "generic-head-7318",
    }
    original = Task(
        project_id=project_id,
        status=TaskStatus.EVALUATING,
        created_by="ORCHESTRATOR",
        payloads=[{"type": "create", "base_branch": base_branch}],
        context={
            "review_loop": {
                **target,
                "review_target": target,
                "branch": branch,
                "base_branch": base_branch,
            }
        },
    )
    establish_independent_approval_hold(
        original,
        reason="security scan hold",
        owner="security_scan",
    )
    db_session.add(original)
    await db_session.commit()
    original_id = original.id
    review = Task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "original_task_id": str(original_id),
            }
        ],
        context={
            "review_loop": {
                **target,
                "review_target": target,
                "branch": branch,
                "base_branch": base_branch,
                "original_task_id": str(original_id),
            }
        },
    )
    db_session.add(review)
    await db_session.commit()
    review_id = review.id

    _mock_lifecycle_github(
        mocker,
        pr_snapshot=_open_pr(
            head=str(target["head_sha"]),
            base=str(target["base_sha"]),
            branch=branch,
            base_branch=base_branch,
        ),
    )
    race_committed = False
    replacement_target = {
        **target,
        "head_sha": "replacement-head-7318",
        "base_sha": "replacement-base-7318",
    }

    async def _race_before_gate(*_args, **_kwargs):
        nonlocal race_committed
        if not race_committed:
            async with AsyncSessionLocal() as peer_session:
                peer = await peer_session.get(Task, original_id)
                assert peer is not None
                if race_winner == "replacement":
                    peer.context = {
                        **(peer.context or {}),
                        "replacement_sentinel": "preserve",
                        "review_loop": {
                            **dict((peer.context or {}).get("review_loop") or {}),
                            **replacement_target,
                            "review_target": replacement_target,
                        },
                    }
                else:
                    peer.status = race_winner
                peer.latest_status_message = "peer terminal/replacement authority wins"
                peer_session.add(peer)
                await peer_session.commit()
            race_committed = True
        return [{"filename": "README.md"}]

    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(side_effect=_race_before_gate),
    )
    await _process_post_commit(
        review_id,
        AgentStatusPayload(
            task_id=review_id,
            status=TaskStatus.COMPLETED,
            pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
            artifact_type=ArtifactType.PULL_REQUEST,
            verdict_json=json.dumps(
                {
                    "verdict": "APPROVE",
                    "feedback": "exact target is clean",
                    "issues": [],
                    "reviewed_head_sha": target["head_sha"],
                    "reviewed_base_sha": target["base_sha"],
                }
            ),
        ),
        _session_factory=AsyncSessionLocal,
    )

    assert race_committed is True
    await db_session.rollback()
    async with AsyncSessionLocal() as verify_session:
        fresh = await verify_session.get(Task, original_id)
        assert fresh is not None
        assert fresh.latest_status_message == "peer terminal/replacement authority wins"
        assert fresh.requires_approval is True
        if race_winner == "replacement":
            assert fresh.status == TaskStatus.EVALUATING
            assert (fresh.context or {})["replacement_sentinel"] == "preserve"
            assert ((fresh.context or {})["review_loop"])["review_target"] == (
                replacement_target
            )
        else:
            assert fresh.status == race_winner
