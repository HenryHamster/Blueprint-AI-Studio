"""Acceptance evidence for durable candidate-bound rehearsal dispatch."""

import asyncio
import ast
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from _release_qualifier_testhelpers import (
    _make_project_and_version,
    _shared_session_factory,
)
from sqlalchemy import select

from app.acceptance_criteria import normalize_evidence_contract
from app.models import ProjectMember, ReleasePrepromotionDispatch
from app.release_prepromotion_rehearsal import (
    PREPROMOTION_REHEARSAL_CHECKS,
    PREPROMOTION_REHEARSAL_CONTRACT_TYPE,
    PREPROMOTION_REHEARSAL_DEFERRED,
    PREPROMOTION_REHEARSAL_KIND,
    PREPROMOTION_REHEARSAL_WORKFLOW,
    PREPROMOTION_REHEARSAL_WORKFLOW_REF,
    apply_rehearsal_evidence,
    dispatch_pending_rehearsal_intents,
    dispatch_prepromotion_rehearsal,
    persisted_rehearsal_for_candidate,
    persisted_rehearsal_for_version,
    prepare_rehearsal_dispatch_intent,
    redrive_rehearsal_ready_qualification,
)
from app.routers import admin_release_version as release_router
from app.time_utils import _utcnow

_CANDIDATE = "a" * 40
_CATEGORY = {
    "category_key": "c-bootstrap",
    "text": "Trusted CI builds and smoke-tests the candidate before promotion.",
    "evidence_contract": {
        "type": PREPROMOTION_REHEARSAL_CONTRACT_TYPE,
        "predicate": {"required": True},
    },
}
_VERDICT = {
    "category_key": "c-bootstrap",
    "status": "partial",
    "tier": "judged",
    "gap": "no trusted CI receipt",
    "citations": [],
}


def test_disabled_host_rehearsal_shell_is_gone_and_fail_closed_stubs_remain():
    repo_root = Path(__file__).parents[2]
    blueprint_source = (repo_root / "scripts" / "blueprint.py").read_text(
        encoding="utf-8"
    )
    for retired_name in (
        "_request_release_rehearsal",
        "_post_prepromotion_rehearsal_receipt",
        "_read_open_rehearsal_version",
    ):
        assert f"def {retired_name}" not in blueprint_source
    assert "Host rehearsal is disabled" not in blueprint_source

    for relative_path in (
        "scripts/local_app_release.py",
        "scripts/bootstrap_local_app_release.py",
    ):
        tree = ast.parse((repo_root / relative_path).read_text(encoding="utf-8"))
        rehearse = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "rehearse"
        )
        assert isinstance(rehearse.body[-1], ast.Raise)
        assert "host rehearsal disabled" in ast.unparse(rehearse)

    release_source = (repo_root / "scripts" / "blueprint_release.py").read_text(
        encoding="utf-8"
    )
    assert "host rehearsal disabled" in release_source
    assert "--rehearse" in release_source


@pytest.mark.asyncio
async def test_permanent_dispatch_404_terminalizes_with_named_target(db_session, mocker):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()
    mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )
    response = MagicMock(status_code=404, text="workflow not found")
    mocker.patch("app.github._github_request", new=AsyncMock(return_value=response))

    assert await dispatch_pending_rehearsal_intents(limit=1) == 1

    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    assert row is not None
    assert row.status == "rejected"
    assert row.last_error == (
        "permanent 404: dispatch target absent "
        "acme/widget/actions/workflows/prepromotion-rehearsal.yml@automain"
    )


def _receipt(
    candidate_sha: str = _CANDIDATE,
    *,
    project_id: str = "11111111-1111-4111-8111-111111111111",
    version_id: str = "22222222-2222-4222-8222-222222222222",
    request_id: str = "33333333-3333-4333-8333-333333333333",
) -> dict:
    return {
        "kind": PREPROMOTION_REHEARSAL_KIND,
        "schema_version": 1,
        "source": "github_actions",
        "project_id": project_id,
        "release_version_id": version_id,
        "request_id": request_id,
        "candidate_sha": candidate_sha,
        "repository": "acme/widget",
        "workflow_file": PREPROMOTION_REHEARSAL_WORKFLOW,
        "workflow_ref": PREPROMOTION_REHEARSAL_WORKFLOW_REF,
        "workflow_head_sha": "b" * 40,
        "run_id": 42,
        "run_attempt": 1,
        "result": "passed",
        "checks": list(PREPROMOTION_REHEARSAL_CHECKS),
        "deferred": list(PREPROMOTION_REHEARSAL_DEFERRED),
        "completed_at": "2026-08-11T12:00:00+00:00",
    }


def test_valid_candidate_receipt_grades_rehearsal_category_met():
    result = apply_rehearsal_evidence([_VERDICT], [_CATEGORY], _receipt(), _CANDIDATE)
    assert result is not None
    assert result[0]["status"] == "met"
    assert result[0]["tier"] == "machine"


def test_machine_receipt_inserts_verdict_when_grader_omits_category():
    result = apply_rehearsal_evidence(None, [_CATEGORY], _receipt(), _CANDIDATE)
    assert result is not None
    assert result == [
        {
            "category_key": "c-bootstrap",
            "text": _CATEGORY["text"],
            "status": "met",
            "tier": "machine",
            "gap": None,
            "citations": [
                {
                    "kind": "test",
                    "ref": "github-actions:42:33333333-3333-4333-8333-333333333333",
                }
            ],
            "evidence_provenance": {
                "kind": "machine",
                "source_kind": "trusted_ci_workflow",
                "request_id": "33333333-3333-4333-8333-333333333333",
                "run_id": 42,
                "candidate_sha": _CANDIDATE,
            },
        }
    ]


def test_missing_receipt_inserts_fail_closed_verdict_when_grader_omits_category():
    result = apply_rehearsal_evidence([], [_CATEGORY], None, _CANDIDATE)
    assert result is not None
    assert len(result) == 1
    assert result[0]["category_key"] == "c-bootstrap"
    assert result[0]["status"] == "not_met"
    assert result[0]["evidence_provenance"]["source_kind"] == (
        "release_record_validation"
    )


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda value: value.update(extra="forged"),),
        (lambda value: value.update(checks=["candidate_checkout_exact_sha"]),),
        (lambda value: value.update(workflow_ref="release-v32"),),
        (lambda value: value.update(run_id=True),),
        (lambda value: value.update(completed_at="not-a-timestamp"),),
    ],
)
def test_receipt_schema_rejects_extra_or_malformed_evidence(mutation):
    receipt = _receipt()
    mutation(receipt)
    assert persisted_rehearsal_for_candidate(receipt, _CANDIDATE) is None


def test_missing_or_wrong_candidate_receipt_fails_closed():
    assert persisted_rehearsal_for_candidate(_receipt("c" * 40), _CANDIDATE) is None
    result = apply_rehearsal_evidence([_VERDICT], [_CATEGORY], None, _CANDIDATE)
    assert result is not None
    assert result[0]["status"] == "not_met"


def test_receipt_rejects_cross_record_and_repository_replay():
    receipt = _receipt()
    expected = {
        "project_id": receipt["project_id"],
        "release_version_id": receipt["release_version_id"],
        "request_id": receipt["request_id"],
        "repository": receipt["repository"],
    }
    assert (
        persisted_rehearsal_for_candidate(receipt, _CANDIDATE, **expected)
        is not None
    )
    for key, wrong in (
        ("project_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("release_version_id", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ("request_id", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        ("repository", "other/repository"),
    ):
        mismatch = {**expected, key: wrong}
        assert (
            persisted_rehearsal_for_candidate(receipt, _CANDIDATE, **mismatch)
            is None
        )


def test_central_contract_normalizer_accepts_only_closed_ci_shape():
    assert (
        normalize_evidence_contract(_CATEGORY["evidence_contract"])
        == (_CATEGORY["evidence_contract"])
    )
    assert (
        normalize_evidence_contract(
            {
                "type": PREPROMOTION_REHEARSAL_CONTRACT_TYPE,
                "predicate": {"required": False},
            }
        )
        is None
    )
    assert (
        normalize_evidence_contract(
            {
                "type": PREPROMOTION_REHEARSAL_CONTRACT_TYPE,
                "predicate": {"required": True},
                "extra": "unsupported",
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_dispatch_uses_integration_workflow_and_exact_candidate(mocker):
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run_outcome",
        new=AsyncMock(return_value="accepted"),
    )
    request_id = "11111111-1111-1111-1111-111111111111"
    assert (
        await dispatch_prepromotion_rehearsal(
            "token", "owner", "repo", _CANDIDATE, request_id, project_id="project"
        )
        == "accepted"
    )
    assert dispatch.await_args.args[:5] == (
        "token",
        "owner",
        "repo",
        PREPROMOTION_REHEARSAL_WORKFLOW,
        "automain",
    )
    assert dispatch.await_args.kwargs["inputs"] == {
        "candidate_sha": _CANDIDATE,
        "request_id": request_id,
    }


def test_trusted_workflow_keeps_candidate_execution_in_ephemeral_containers():
    workflow = (
        Path(__file__).parents[2]
        / ".github"
        / "workflows"
        / "prepromotion-rehearsal.yml"
    ).read_text(encoding="utf-8")
    assert "release-v" not in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "ref: ${{ inputs.candidate_sha }}" in workflow
    assert 'git -C trusted rev-parse HEAD)" = "${GITHUB_SHA}"' in workflow
    assert "prepromotion-rehearsal-${{ inputs.request_id }}" in workflow
    assert "secrets." not in workflow
    assert "id-token: write" not in workflow
    assert "Authenticate to Google Cloud" not in workflow
    assert "upload-artifact" not in workflow
    assert "npm ci\n" not in workflow.replace("          RUN npm ci\n", "")
    assert "working-directory: ./admin-ui" not in workflow
    assert "--network none" in workflow
    assert "--read-only" in workflow
    assert "--cap-drop ALL" in workflow
    assert "--security-opt no-new-privileges" in workflow
    assert "--pids-limit 256" in workflow
    assert "--entrypoint /usr/local/bin/python" in workflow
    assert "actions/checkout@v4" not in workflow
    assert workflow.count(
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
    ) == 2
    assert "${{ inputs.request_id }}" not in "\n".join(
        line for line in workflow.splitlines() if line.lstrip().startswith("run:")
    )


@pytest.mark.asyncio
async def test_intent_is_durable_and_not_projected_from_last_qualification(db_session):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    ready, first = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    version.last_qualification = {"status": "unrelated replacement"}
    ready_again, second = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()
    rows = (
        (
            await db_session.execute(
                select(ReleasePrepromotionDispatch).where(
                    ReleasePrepromotionDispatch.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert ready is ready_again is False
    assert first["request_id"] == second["request_id"]
    assert len(rows) == 1
    assert rows[0].project_id == project.id


@pytest.mark.asyncio
async def test_version_read_requires_observed_matching_outbox(db_session):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    version.prepromotion_rehearsal_receipt = _receipt(
        project_id=str(project.id),
        version_id=str(version.id),
        request_id=projection["request_id"],
    )
    await db_session.commit()

    assert (
        await persisted_rehearsal_for_version(db_session, version, _CANDIDATE)
        is None
    )
    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    row.status = "observed"
    row.workflow_run_id = 42
    db_session.add(row)
    await db_session.commit()
    assert (
        await persisted_rehearsal_for_version(db_session, version, _CANDIDATE)
        is not None
    )

    row.workflow_run_id = 99
    db_session.add(row)
    await db_session.commit()
    ready, _ = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    assert ready is False
    row.workflow_run_id = 42
    db_session.add(row)
    await db_session.commit()

    project.github_repo = "renamed-widget"
    db_session.add(project)
    await db_session.commit()
    assert (
        await persisted_rehearsal_for_version(db_session, version, _CANDIDATE)
        is None
    )
    ready, _ = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    assert ready is False
    assert row.status == "observed"
    ready, _ = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE, rearm_terminal=True
    )
    assert ready is False
    assert row.status == "claimed"
    assert row.workflow_run_id is None
    assert version.prepromotion_rehearsal_receipt is None


@pytest.mark.asyncio
async def test_ambiguous_dispatch_reconciles_before_retry(db_session, mocker):
    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()
    mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.dispatch_prepromotion_rehearsal",
        new=AsyncMock(return_value="ambiguous"),
    )
    lookup = mocker.patch(
        "app.release_prepromotion_rehearsal.github.get_workflow_run_for_dispatch_request",
        new=AsyncMock(
            return_value={
                "state": "found",
                "run_id": 42,
                "status": "completed",
                "conclusion": "success",
                "head_sha": "b" * 40,
                "head_branch": PREPROMOTION_REHEARSAL_WORKFLOW_REF,
                "event": "workflow_dispatch",
                "path": ".github/workflows/prepromotion-rehearsal.yml",
                "run_attempt": 1,
                "created_at": "2026-08-11T11:00:00Z",
                "updated_at": "2026-08-11T11:05:00Z",
            }
        ),
    )

    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    await db_session.refresh(row)
    assert row.status == "uncertain"
    # A fresh uncertain POST is never automatically retried.
    assert await dispatch_pending_rehearsal_intents(limit=1) == 0
    assert dispatch.await_count == 1

    claimed_at = row.claimed_at
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    # GitHub timestamps are second-resolution while claimed_at carries DB
    # microseconds. A run created in the same wall-clock second is valid.
    same_second = claimed_at.replace(microsecond=0)
    lookup.return_value["created_at"] = same_second.isoformat()
    lookup.return_value["updated_at"] = (same_second + timedelta(seconds=2)).isoformat()
    row.next_retry_at = _utcnow() - timedelta(seconds=1)
    db_session.add(row)
    await db_session.commit()
    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    await db_session.refresh(row)
    assert row.status == "observed"
    assert row.workflow_run_id == 42
    await db_session.refresh(version)
    assert (
        persisted_rehearsal_for_candidate(
            version.prepromotion_rehearsal_receipt, _CANDIDATE
        )
        is not None
    )
    assert dispatch.await_count == 1
    lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_attempting_claim_lease_prevents_concurrent_reclaim(db_session, mocker):
    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()

    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()

    async def _blocked_resolver(*_args, **_kwargs):
        resolver_started.set()
        await release_resolver.wait()
        return ("token", "acme", "widget")

    mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(side_effect=_blocked_resolver),
    )
    mocker.patch(
        "app.release_prepromotion_rehearsal.dispatch_prepromotion_rehearsal",
        new=AsyncMock(return_value="accepted"),
    )

    first = asyncio.create_task(dispatch_pending_rehearsal_intents(limit=1))
    await resolver_started.wait()
    assert await dispatch_pending_rehearsal_intents(limit=1) == 0
    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    await db_session.refresh(row)
    assert row.status == "attempting"
    assert row.dispatch_attempts == 1
    assert row.next_retry_at is not None and row.next_retry_at > _utcnow()

    release_resolver.set()
    assert await first == 1
    await db_session.refresh(row)
    assert row.status == "dispatched"
    assert row.dispatch_attempts == 1


@pytest.mark.asyncio
async def test_dispatcher_hard_wall_includes_session_entry(mocker):
    from app import release_prepromotion_rehearsal as rehearsal

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingContext:
        async def __aenter__(self):
            entered.set()
            await release.wait()

        async def __aexit__(self, *_args):
            return None

    mocker.patch.object(rehearsal, "PREPROMOTION_RECONCILE_MAX_SECONDS", 0.02)
    started = asyncio.get_running_loop().time()
    assert await rehearsal.dispatch_pending_rehearsal_intents(
        session_factory=lambda: BlockingContext(), limit=1
    ) == 0
    assert entered.is_set()
    assert asyncio.get_running_loop().time() - started < 0.2


@pytest.mark.asyncio
async def test_repository_flip_during_lookup_cannot_misattribute_receipt(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()
    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    row.status = "attempting"
    row.dispatch_attempts = 1
    row.next_retry_at = _utcnow() - timedelta(seconds=1)
    db_session.add(row)
    await db_session.commit()
    mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )

    async def _lookup(*_args, **_kwargs):
        project.github_repo = "renamed-widget"
        db_session.add(project)
        await db_session.commit()
        claimed = row.claimed_at.replace(tzinfo=timezone.utc)
        return {
            "state": "found",
            "run_id": 91,
            "status": "completed",
            "conclusion": "success",
            "head_sha": "b" * 40,
            "head_branch": PREPROMOTION_REHEARSAL_WORKFLOW_REF,
            "event": "workflow_dispatch",
            "path": ".github/workflows/prepromotion-rehearsal.yml",
            "run_attempt": 1,
            "created_at": claimed.isoformat(),
            "updated_at": (claimed + timedelta(seconds=1)).isoformat(),
        }

    mocker.patch(
        "app.release_prepromotion_rehearsal.github.get_workflow_run_for_dispatch_request",
        new=AsyncMock(side_effect=_lookup),
    )
    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    await db_session.refresh(row)
    await db_session.refresh(version)
    assert row.status == "uncertain"
    assert version.prepromotion_rehearsal_receipt is None


@pytest.mark.asyncio
async def test_credential_failures_exhaust_bounded_attempt_budget(db_session, mocker):
    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()
    resolver = mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(return_value=None),
    )
    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    for _ in range(10):
        row.next_retry_at = _utcnow() - timedelta(seconds=1)
        db_session.add(row)
        await db_session.commit()
        assert await dispatch_pending_rehearsal_intents(limit=1) == 1
        await db_session.refresh(row)
    row.next_retry_at = _utcnow() - timedelta(seconds=1)
    db_session.add(row)
    await db_session.commit()
    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    await db_session.refresh(row)
    assert row.status == "rejected"
    assert row.dispatch_attempts == 10
    assert resolver.await_count == 10


@pytest.mark.asyncio
async def test_due_observation_is_not_starved_by_fresh_claims(db_session, mocker):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    due = ReleasePrepromotionDispatch(
        release_version_id=version.id,
        project_id=project.id,
        candidate_sha=_CANDIDATE,
        workflow_ref=PREPROMOTION_REHEARSAL_WORKFLOW_REF,
        status="dispatched",
        dispatch_attempts=1,
        next_retry_at=_utcnow() - timedelta(minutes=5),
    )
    db_session.add(due)
    for index in range(3):
        db_session.add(
            ReleasePrepromotionDispatch(
                release_version_id=version.id,
                project_id=project.id,
                candidate_sha=f"{index + 1:x}" * 40,
                workflow_ref=PREPROMOTION_REHEARSAL_WORKFLOW_REF,
                status="claimed",
                claimed_at=_utcnow() + timedelta(seconds=index),
            )
        )
    db_session.add(version)
    await db_session.commit()
    mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(return_value=("token", "acme", "widget")),
    )
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.get_workflow_run_for_dispatch_request",
        new=AsyncMock(return_value={"state": "not_found"}),
    )
    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    await db_session.refresh(due)
    assert due.status == "uncertain"
    assert due.dispatch_attempts == 2


@pytest.mark.asyncio
async def test_prior_post_not_found_and_rejected_never_redispatch(db_session, mocker):
    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()
    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    row.status = "attempting"
    row.dispatch_attempts = 1
    row.next_retry_at = _utcnow() - timedelta(seconds=1)
    db_session.add(row)
    await db_session.commit()
    mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.get_workflow_run_for_dispatch_request",
        new=AsyncMock(return_value={"state": "not_found"}),
    )
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.dispatch_prepromotion_rehearsal",
        new=AsyncMock(return_value="accepted"),
    )

    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    await db_session.refresh(row)
    assert row.status == "uncertain"
    dispatch.assert_not_awaited()

    row.status = "rejected"
    row.attempted_at = _utcnow() - timedelta(days=1)
    db_session.add(row)
    await db_session.commit()
    assert await dispatch_pending_rehearsal_intents(limit=1) == 0
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_preseeded_receipt_cannot_terminalize_claim_without_observed_run(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    version.prepromotion_rehearsal_receipt = _receipt(
        project_id=str(project.id),
        version_id=str(version.id),
        request_id=projection["request_id"],
    )
    db_session.add(version)
    await db_session.commit()
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.dispatch_prepromotion_rehearsal",
        new=AsyncMock(return_value="accepted"),
    )
    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    assert row.status == "claimed"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_terminalizes_stale_candidate_without_github(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": "d" * 40}
    stale = ReleasePrepromotionDispatch(
        release_version_id=version.id,
        project_id=project.id,
        candidate_sha=_CANDIDATE,
        workflow_ref=PREPROMOTION_REHEARSAL_WORKFLOW_REF,
        status="claimed",
    )
    db_session.add(version)
    db_session.add(stale)
    await db_session.commit()
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.dispatch_prepromotion_rehearsal",
        new=AsyncMock(return_value="accepted"),
    )

    assert await dispatch_pending_rehearsal_intents(limit=1) == 1
    await db_session.refresh(stale)
    assert stale.status == "rejected"
    assert "no longer active" in stale.last_error
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_prefix_does_not_starve_current_candidate_across_bounded_sweeps(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    base_time = _utcnow() - timedelta(minutes=10)
    for index in range(4):
        db_session.add(
            ReleasePrepromotionDispatch(
                release_version_id=version.id,
                project_id=project.id,
                candidate_sha=f"{index + 1:x}" * 40,
                workflow_ref=PREPROMOTION_REHEARSAL_WORKFLOW_REF,
                status="claimed",
                claimed_at=base_time + timedelta(seconds=index),
            )
        )
    current = ReleasePrepromotionDispatch(
        release_version_id=version.id,
        project_id=project.id,
        candidate_sha=_CANDIDATE,
        workflow_ref=PREPROMOTION_REHEARSAL_WORKFLOW_REF,
        status="claimed",
        claimed_at=base_time + timedelta(seconds=5),
    )
    db_session.add(version)
    db_session.add(current)
    await db_session.commit()
    mocker.patch(
        "app.release_prepromotion_rehearsal._resolve_dispatch_github_context",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.dispatch_prepromotion_rehearsal",
        new=AsyncMock(return_value="accepted"),
    )

    assert await dispatch_pending_rehearsal_intents(limit=3) == 3
    dispatch.assert_not_awaited()
    assert await dispatch_pending_rehearsal_intents(limit=3) == 2
    dispatch.assert_awaited_once()
    await db_session.refresh(current)
    assert current.status == "dispatched"


@pytest.mark.asyncio
async def test_admin_request_commits_and_explicitly_rearms_rejected_intent(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {"candidate_sha": _CANDIDATE}
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id=project.user_id,
            role="owner",
        )
    )
    db_session.add(version)
    await db_session.commit()
    first = await release_router.request_prepromotion_rehearsal(
        project.id,
        version.version_number,
        db_session,
        {"user_id": project.user_id},
    )
    request_id = first["dispatch"]["request_id"]
    row = await db_session.get(ReleasePrepromotionDispatch, request_id)
    assert row is not None
    row.status = "rejected"
    row.dispatch_attempts = 3
    row.last_error = "workflow temporarily unavailable"
    db_session.add(row)
    await db_session.commit()

    second = await release_router.request_prepromotion_rehearsal(
        project.id,
        version.version_number,
        db_session,
        {"user_id": project.user_id},
    )
    assert second["dispatch"]["request_id"] == request_id
    await db_session.refresh(row)
    assert row.status == "claimed"
    assert row.dispatch_attempts == 0
    assert row.last_error is None


@pytest.mark.asyncio
async def test_observed_receipt_redrives_parked_qualification_once(db_session, mocker):
    from app import release_qualifier

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "rehearsal_dispatch_pending",
        "candidate_sha": _CANDIDATE,
        "qualification_attempt_id": "33333333-3333-4333-8333-333333333333",
        "retry_episode": 2,
        "retry_episode_cap": 5,
        "requested_probes": ["walkthrough_e2e", "product_smoke"],
    }
    _, projection = await prepare_rehearsal_dispatch_intent(
        db_session, version, _CANDIDATE
    )
    await db_session.commit()
    run = mocker.patch.object(
        release_qualifier,
        "run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    factory = _shared_session_factory(db_session)

    assert not await redrive_rehearsal_ready_qualification(
        version.id, session_factory=factory
    )
    run.assert_not_awaited()

    row = await db_session.get(ReleasePrepromotionDispatch, projection["request_id"])
    row.status = "observed"
    row.workflow_run_id = 42
    version.prepromotion_rehearsal_receipt = _receipt(
        project_id=str(project.id),
        version_id=str(version.id),
        request_id=str(row.id),
    )
    db_session.add_all([row, version])
    await db_session.commit()

    parked = dict(version.last_qualification or {})
    parked.pop("qualification_attempt_id", None)
    version.last_qualification = parked
    db_session.add(version)
    await db_session.commit()
    assert not await redrive_rehearsal_ready_qualification(
        version.id, session_factory=factory
    )
    run.assert_not_awaited()

    version.last_qualification = {
        **parked,
        "qualification_attempt_id": "not-a-uuid",
    }
    db_session.add(version)
    await db_session.commit()
    assert not await redrive_rehearsal_ready_qualification(
        version.id, session_factory=factory
    )
    run.assert_not_awaited()

    version.last_qualification = {
        **parked,
        "qualification_attempt_id": "33333333-3333-4333-8333-333333333333",
    }
    db_session.add(version)
    await db_session.commit()

    async def _spawn(_session, _project_id, locked_version, **_kwargs):
        locked_version.last_qualification = {
            "status": "pending",
            "candidate_sha": _CANDIDATE,
        }
        db_session.add(locked_version)
        return {"status": "spawned"}

    run.side_effect = _spawn
    assert await redrive_rehearsal_ready_qualification(
        version.id, session_factory=factory
    )
    assert run.await_count == 1
    assert run.await_args.kwargs["auto_trigger"] is False
    assert str(run.await_args.kwargs["qualification_attempt_id"]) == (
        "33333333-3333-4333-8333-333333333333"
    )
    assert run.await_args.kwargs["retry_episode"] == 2
    assert run.await_args.kwargs["retry_episode_cap"] == 5
    assert run.await_args.kwargs["requested_probes"] == [
        "walkthrough_e2e",
        "product_smoke",
    ]
    assert not await redrive_rehearsal_ready_qualification(
        version.id, session_factory=factory
    )
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_reaper_dispatches_rehearsal_intents_before_qualification_sweep(mocker):
    """Step 9 runs the durable outbox first and preserves cancellation."""
    from app import reaper_sweeps, release_qualifier

    session = object()
    session_factory = object()
    dispatch = mocker.patch(
        "app.release_prepromotion_rehearsal.dispatch_pending_rehearsal_intents",
        new=AsyncMock(return_value=1),
    )
    qualify = mocker.patch.object(
        release_qualifier,
        "sweep_stuck_qualifications",
        new=AsyncMock(return_value=2),
    )

    assert (
        await reaper_sweeps.sweep_stuck_qualifications(
            session, session_factory=session_factory
        )
        == 2
    )
    dispatch.assert_awaited_once_with(session_factory=session_factory)
    qualify.assert_awaited_once_with(session, session_factory=session_factory)

    dispatch.reset_mock(side_effect=True)
    qualify.reset_mock()
    dispatch.side_effect = asyncio.CancelledError("shutdown")
    with pytest.raises(asyncio.CancelledError, match="shutdown"):
        await reaper_sweeps.sweep_stuck_qualifications(
            session, session_factory=session_factory
        )
    qualify.assert_not_awaited()
