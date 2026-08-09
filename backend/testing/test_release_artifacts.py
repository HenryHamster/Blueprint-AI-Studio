"""Unit tests for per-provider observability + the promotion_merge baseline.

These cover the deploy-observability refactor that replaces the per-project
``is_self_repo`` arming gate:

  * provider observability selection (deploy-identity project vs external
    GitHub project vs a credential-less project),
  * the baseline ``promotion_merge`` provider's confirm / merge-reverted /
    workflow-failed / indeterminate paths,
  * ``summarize_required_artifact_gap`` skipping vacuous (None-status) rows,
  * ``summarize_release_artifacts`` gating the backend legacy-scalar projection
    on observability so an external project never shows a phantom backend gap.

All pure / mocked — no DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app import release_artifacts as artifact_registry
from app.release_artifacts import (
    AGENT_IMAGE_ARTIFACT_KIND,
    AGENT_IMAGE_FLEET_SOURCE_UNOBSERVABLE_REASON,
    BACKEND_REVISION_ARTIFACT_KIND,
    PROMOTION_MERGE_ARTIFACT_KIND,
    _check_agent_image_serving,
    _check_promotion_merge_serving,
    observable_release_artifact_providers,
    summarize_release_artifacts,
    summarize_required_artifact_gap,
)

_SHA = "a" * 40


class _Proj:
    def __init__(self, github_owner=None, github_repo=None):
        self.id = 1
        self.project_id = 1
        self.github_owner = github_owner
        self.github_repo = github_repo


class _Version:
    def __init__(
        self,
        *,
        promoted_sha=_SHA,
        serving_status=None,
        served_sha=None,
        served_at=None,
        deploy_failure_reason=None,
        version_number=1,
        project_id=1,
        promoted_at=None,
        deploy_confirm_deadline=None,
    ):
        self.promoted_sha = promoted_sha
        self.serving_status = serving_status
        self.served_sha = served_sha
        self.served_at = served_at
        self.deploy_failure_reason = deploy_failure_reason
        self.version_number = version_number
        self.project_id = project_id
        self.promoted_at = promoted_at
        self.deploy_confirm_deadline = deploy_confirm_deadline


class _Artifact:
    def __init__(self, artifact_kind, serving_status, **kw):
        self.artifact_kind = artifact_kind
        self.serving_status = serving_status
        self.served_identifier = kw.get("served_identifier")
        self.served_at = kw.get("served_at")
        self.reason_code = kw.get("reason_code")
        self.confirmation_deadline = kw.get("confirmation_deadline")


class _DeployState:
    """Minimal DeploymentState stand-in for the pure (no-DB) provider tests."""

    def __init__(self, *, status, current_sha, deployed_at, halted_at=None):
        self.status = status
        self.current_sha = current_sha
        self.deployed_at = deployed_at
        self.halted_at = halted_at


class _DispatchSession:
    """Fake AsyncSession that routes DeploymentState lookups to a deploy-state
    stand-in and every other model lookup (e.g. Project) to the given project."""

    def __init__(self, project, ds):
        self._project = project
        self._ds = ds

    async def get(self, model, _pk):
        if model.__name__ == "DeploymentState":
            return self._ds
        return self._project


# ---------------------------------------------------------------------------
# Provider observability selection
# ---------------------------------------------------------------------------


def test_observable_providers_for_deploy_identity_includes_cloud_run():
    """The deploy-identity project sees ALL three providers: the merge baseline
    plus both Cloud Run providers (backend_revision + agent_image)."""
    project = _Proj(github_owner="acme", github_repo="widget")
    kinds = {
        p.artifact_kind
        for p in observable_release_artifact_providers(project, ("acme", "widget"))
    }
    assert kinds == {
        PROMOTION_MERGE_ARTIFACT_KIND,
        BACKEND_REVISION_ARTIFACT_KIND,
        AGENT_IMAGE_ARTIFACT_KIND,
    }


def test_observable_providers_for_external_github_project_is_merge_only():
    """An external GitHub project (not the deploy identity) sees ONLY the
    merge-level baseline — its Cloud Run signals are unobservable."""
    project = _Proj(github_owner="other", github_repo="proj")
    kinds = {
        p.artifact_kind
        for p in observable_release_artifact_providers(project, ("acme", "widget"))
    }
    assert kinds == {PROMOTION_MERGE_ARTIFACT_KIND}


def test_observable_providers_none_without_github_repo():
    """A credential-less project (no GitHub repo) has NO observable provider —
    so the promotion is never armed and never stranded waiting on a signal."""
    project = _Proj(github_owner=None, github_repo=None)
    assert observable_release_artifact_providers(project, ("acme", "widget")) == ()


@pytest.mark.asyncio
async def test_agent_image_unobservable_fleet_reason_distinct_from_not_yet_serving(
    mocker,
):
    """No source identity is distinct from an observed, lagging fleet."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    project = _Proj(github_owner="acme", github_repo="widget")
    version = _Version(
        promoted_at=now - timedelta(hours=2),
        deploy_confirm_deadline=now - timedelta(minutes=1),
    )
    version.id = 1
    deployment_state = _DeployState(
        status="stable",
        current_sha="b" * 40,
        deployed_at=now - timedelta(hours=1),
    )
    session = _DispatchSession(project, deployment_state)
    artifact = _Artifact(AGENT_IMAGE_ARTIFACT_KIND, "unconfirmed")

    mocker.patch(
        "app.release_artifacts.project_matches_deploy_identity", return_value=True
    )
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=True),
    )
    fleet = mocker.patch(
        "app.compute.fleet_serving_source_sha",
        new=AsyncMock(return_value=None),
    )

    decision = await _check_agent_image_serving(session, version, artifact, now)

    assert decision == (
        "confirmation_timed_out",
        AGENT_IMAGE_FLEET_SOURCE_UNOBSERVABLE_REASON,
        None,
    )

    fleet.return_value = False
    lagging_decision = await _check_agent_image_serving(
        session, version, artifact, now
    )
    assert lagging_decision == (
        "confirmation_timed_out",
        "agent_image_fleet_not_yet_serving",
        None,
    )


# ---------------------------------------------------------------------------
# summarize_required_artifact_gap — vacuous (None) rows are skipped
# ---------------------------------------------------------------------------


def test_gap_skips_none_status_required_rows():
    """An un-armed/legacy required row (serving_status None) is VACUOUS, not
    pending — it must not project a phantom 'waiting on required artifacts'."""
    evidence = [
        {
            "artifact_kind": BACKEND_REVISION_ARTIFACT_KIND,
            "required": True,
            "serving_status": None,
            "reason_code": None,
        },
        {
            "artifact_kind": AGENT_IMAGE_ARTIFACT_KIND,
            "required": True,
            "serving_status": None,
            "reason_code": None,
        },
    ]
    assert summarize_required_artifact_gap(evidence) is None


def test_gap_reports_armed_pending_required_row():
    """A genuinely pending (unconfirmed) required row STILL reports — the fix
    must not weaken the legitimate pending case."""
    evidence = [
        {
            "artifact_kind": BACKEND_REVISION_ARTIFACT_KIND,
            "required": True,
            "serving_status": None,  # vacuous → skipped
            "reason_code": None,
        },
        {
            "artifact_kind": PROMOTION_MERGE_ARTIFACT_KIND,
            "required": True,
            "serving_status": "unconfirmed",  # genuinely pending → reported
            "reason_code": None,
        },
    ]
    gap = summarize_required_artifact_gap(evidence)
    assert gap == "waiting on required artifacts: promotion_merge"


def test_gap_reports_failed_over_none():
    evidence = [
        {
            "artifact_kind": BACKEND_REVISION_ARTIFACT_KIND,
            "required": True,
            "serving_status": None,
            "reason_code": None,
        },
        {
            "artifact_kind": PROMOTION_MERGE_ARTIFACT_KIND,
            "required": True,
            "serving_status": "deploy_failed",
            "reason_code": "merge_reverted",
        },
    ]
    gap = summarize_required_artifact_gap(evidence)
    assert gap == "required artifacts failed: promotion_merge (reason=merge_reverted)"


# ---------------------------------------------------------------------------
# summarize_release_artifacts — observability-gated legacy scalar projection
# ---------------------------------------------------------------------------


def test_summarize_external_project_suppresses_backend_scalar(monkeypatch):
    """For an external project (no deploy identity) with a serving scalar but NO
    backend_revision row, the backend legacy-scalar projection is SUPPRESSED so
    it never invents a misleading backend_revision signal. The aggregate still
    falls back to the version scalar."""
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    version = _Version(serving_status="serving_confirmed", served_sha=_SHA)
    project = _Proj(github_owner="other", github_repo="proj")

    aggregate, evidence = summarize_release_artifacts(
        version, [], project=project
    )
    by_kind = {e["artifact_kind"]: e for e in evidence}
    # Backend projection suppressed → vacuous None (not the scalar).
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND]["serving_status"] is None
    # Aggregate still reflects the scalar via the final fallback.
    assert aggregate["serving_status"] == "serving_confirmed"


def test_summarize_legacy_caller_projects_backend_scalar():
    """With project=None (legacy callers) the backend legacy-scalar projection
    is unconditional — byte-for-byte preserved."""
    version = _Version(serving_status="unconfirmed")
    aggregate, evidence = summarize_release_artifacts(version, [])
    by_kind = {e["artifact_kind"]: e for e in evidence}
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND]["serving_status"] == "unconfirmed"
    assert aggregate["serving_status"] == "unconfirmed"


def test_summarize_deploy_identity_projects_backend_scalar(monkeypatch):
    """For the deploy-identity project the backend provider IS observable, so the
    legacy-scalar projection applies even without a row."""
    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    version = _Version(serving_status="serving_confirmed", served_sha=_SHA)
    project = _Proj(github_owner="acme", github_repo="widget")
    _aggregate, evidence = summarize_release_artifacts(
        version, [], project=project
    )
    by_kind = {e["artifact_kind"]: e for e in evidence}
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND]["serving_status"] == (
        "serving_confirmed"
    )


# ---------------------------------------------------------------------------
# Baseline promotion_merge provider
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, project):
        self._project = project

    async def get(self, _model, _pk):
        return self._project


def _patch_promotion_merge_github(
    mocker, *, ancestry, checks=None, resolve=("tok", "acme", "widget")
):
    from unittest.mock import AsyncMock

    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=resolve),
    )
    mocker.patch(
        "app.release_promotion._lts_branch_names",
        return_value=("automain", "main"),
    )
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=ancestry),
    )
    if checks is not None:
        mocker.patch(
            "app.github_checks.get_check_runs_cached",
            new=AsyncMock(return_value=checks),
        )


@pytest.mark.asyncio
async def test_promotion_merge_confirms_when_reachable_and_checks_green(mocker):
    _patch_promotion_merge_github(
        mocker,
        ancestry=True,
        checks={"status": "completed", "conclusion": "success"},
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    version = _Version(promoted_sha=_SHA)
    out = await _check_promotion_merge_serving(
        _FakeSession(project), version, _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None), _now()
    )
    assert out is not None
    assert out[0] == "serving_confirmed"
    assert out[1] == _SHA


@pytest.mark.asyncio
async def test_promotion_merge_confirms_with_no_checks(mocker):
    """No deploy/release workflow ran (no_checks) → still a merge-level confirm."""
    _patch_promotion_merge_github(
        mocker,
        ancestry=True,
        checks={"status": "no_checks", "conclusion": None},
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None), _now()
    )
    assert out[0] == "serving_confirmed"


@pytest.mark.asyncio
async def test_promotion_merge_deploy_failed_when_reverted(mocker):
    """Promoted SHA no longer reachable from the target head → merge reverted."""
    _patch_promotion_merge_github(mocker, ancestry=False)
    project = _Proj(github_owner="acme", github_repo="widget")
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None), _now()
    )
    assert out == ("deploy_failed", "merge_reverted", None)


@pytest.mark.asyncio
async def test_promotion_merge_deploy_failed_on_workflow_failure(mocker):
    _patch_promotion_merge_github(
        mocker,
        ancestry=True,
        checks={"status": "completed", "conclusion": "failure"},
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None), _now()
    )
    assert out == ("deploy_failed", "deploy_workflow_failed", None)


@pytest.mark.asyncio
async def test_promotion_merge_defers_while_checks_in_progress(mocker):
    _patch_promotion_merge_github(
        mocker,
        ancestry=True,
        checks={"status": "in_progress", "conclusion": None},
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None), _now()
    )
    assert out is None


@pytest.mark.asyncio
async def test_promotion_merge_indeterminate_ancestry_waits(mocker):
    """An indeterminate ancestry check (API error → None) defers rather than
    manufacturing a failure — the confirmation window eventually times out."""
    _patch_promotion_merge_github(mocker, ancestry=None)
    project = _Proj(github_owner="acme", github_repo="widget")
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None), _now()
    )
    assert out is None


@pytest.mark.asyncio
async def test_promotion_merge_no_promoted_sha_returns_none(mocker):
    project = _Proj(github_owner="acme", github_repo="widget")
    out = await _check_promotion_merge_serving(
        _FakeSession(project),
        _Version(promoted_sha=None),
        _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None),
        _now(),
    )
    assert out is None


@pytest.mark.asyncio
async def test_promotion_merge_check_runs_failure_degrades_to_confirm(mocker):
    """A check-runs lookup error must not manufacture a failure — the merge
    persisted (ancestry True), so degrade to the merge-level confirmation."""
    from unittest.mock import AsyncMock

    _patch_promotion_merge_github(mocker, ancestry=True)
    mocker.patch(
        "app.github_checks.get_check_runs_cached",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None), _now()
    )
    assert out[0] == "serving_confirmed"


def _now():
    from datetime import datetime

    return datetime(2026, 6, 10, 12, 0, 0)


def _past_deadline():
    """A deadline that has already elapsed relative to _now()."""
    from datetime import datetime

    return datetime(2026, 6, 10, 11, 0, 0)  # 1 hour before _now()


def _future_deadline():
    """A deadline that has not yet elapsed relative to _now()."""
    from datetime import datetime

    return datetime(2026, 6, 10, 13, 0, 0)  # 1 hour after _now()


@pytest.mark.asyncio
async def test_promotion_merge_times_out_on_indeterminate_ancestry_after_deadline(mocker):
    """When the ancestry check is indeterminate (API error) and the deadline has
    elapsed, return confirmation_timed_out rather than deferring indefinitely."""
    _patch_promotion_merge_github(mocker, ancestry=None)
    project = _Proj(github_owner="acme", github_repo="widget")
    artifact = _Artifact(
        PROMOTION_MERGE_ARTIFACT_KIND, None, confirmation_deadline=_past_deadline()
    )
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), artifact, _now()
    )
    assert out == ("confirmation_timed_out", "promotion_merge_indeterminate", None)


@pytest.mark.asyncio
async def test_promotion_merge_times_out_on_no_credentials_after_deadline(mocker):
    """No GitHub credentials + elapsed deadline → confirmation_timed_out, not None."""
    from unittest.mock import AsyncMock

    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=None),
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    artifact = _Artifact(
        PROMOTION_MERGE_ARTIFACT_KIND, None, confirmation_deadline=_past_deadline()
    )
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), artifact, _now()
    )
    assert out == ("confirmation_timed_out", "promotion_merge_indeterminate", None)


@pytest.mark.asyncio
async def test_promotion_merge_times_out_on_in_progress_checks_after_deadline(mocker):
    """In-flight checks + elapsed deadline → confirmation_timed_out rather than defer."""
    _patch_promotion_merge_github(
        mocker,
        ancestry=True,
        checks={"status": "in_progress", "conclusion": None},
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    artifact = _Artifact(
        PROMOTION_MERGE_ARTIFACT_KIND, None, confirmation_deadline=_past_deadline()
    )
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), artifact, _now()
    )
    assert out == ("confirmation_timed_out", "promotion_merge_indeterminate", None)


@pytest.mark.asyncio
async def test_promotion_merge_still_defers_within_deadline(mocker):
    """Within the deadline, indeterminate checks still return None (keep waiting)."""
    _patch_promotion_merge_github(mocker, ancestry=None)
    project = _Proj(github_owner="acme", github_repo="widget")
    artifact = _Artifact(
        PROMOTION_MERGE_ARTIFACT_KIND, None, confirmation_deadline=_future_deadline()
    )
    out = await _check_promotion_merge_serving(
        _FakeSession(project), _Version(), artifact, _now()
    )
    assert out is None


@pytest.mark.asyncio
async def test_promotion_merge_deadline_from_version_when_artifact_has_none(mocker):
    """Falls back to version.deploy_confirm_deadline when artifact has no deadline."""
    _patch_promotion_merge_github(mocker, ancestry=None)
    project = _Proj(github_owner="acme", github_repo="widget")
    # artifact has no deadline; version has a past deadline
    artifact = _Artifact(PROMOTION_MERGE_ARTIFACT_KIND, None)
    version = _Version(deploy_confirm_deadline=_past_deadline())
    out = await _check_promotion_merge_serving(_FakeSession(project), version, artifact, _now())
    assert out == ("confirmation_timed_out", "promotion_merge_indeterminate", None)


# ---------------------------------------------------------------------------
# _effective_confirm_deadline — the shared promoted_at backstop
# ---------------------------------------------------------------------------


def test_effective_confirm_deadline_backstops_null_from_promoted_at():
    from app.release_promotion import _DEPLOY_CONFIRM_WINDOW_SECONDS

    promoted_at = _now()
    window = timedelta(seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS)
    # No stamped deadline anywhere → backstop off promoted_at + window.
    assert artifact_registry._effective_confirm_deadline(None, None, promoted_at) == (
        promoted_at + window
    )
    # Artifact deadline wins over a NULL version deadline.
    artifact_dl = _now() + timedelta(hours=1)
    assert artifact_registry._effective_confirm_deadline(
        artifact_dl, None, promoted_at
    ) == artifact_dl
    # Version deadline wins over a NULL artifact deadline.
    version_dl = _now() + timedelta(hours=2)
    assert artifact_registry._effective_confirm_deadline(
        None, version_dl, promoted_at
    ) == version_dl
    # No promoted_at to derive a window from → None (the degenerate case).
    assert artifact_registry._effective_confirm_deadline(None, None, None) is None


# ---------------------------------------------------------------------------
# Supersession reclassification — NULL deadline must time out, not defer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ancestry", "deployed_at_delta", "promotion_age", "expected"),
    [
        # Past the derived window + ancestry indeterminate (compare API
        # unavailable) → the soft ancestry timeout, never an infinite defer.
        (None, timedelta(minutes=5), timedelta(hours=2), ("confirmation_timed_out", "backend_revision_ancestry_indeterminate", None)),
        # Past the derived window + determinately NOT a descendant, and the live
        # revision predates the promotion → the honest no_new_revision timeout,
        # substantiated via the deploy-workflow run listing (no deploy.yml run
        # for the promoted SHA → never dispatched).
        (False, timedelta(minutes=-5), timedelta(hours=2), ("confirmation_timed_out", "deploy_workflow_never_dispatched", None)),
        # Within the derived window → still defers; the backstop bounds the
        # wait, it does not make the reclassifier eager.
        (None, timedelta(minutes=5), timedelta(minutes=30), None),
    ],
)
async def test_supersession_null_deadline_times_out_not_defers(
    mocker, ancestry, deployed_at_delta, promotion_age, expected
):
    """A STABLE sha_mismatch whose version has NO stamped confirmation deadline
    must still time out once the promoted_at-backed window elapses instead of
    deferring forever — the supersession reclassifier applies the same backstop
    as the sibling serving providers.  The within-window case is the control:
    the backstop bounds the wait, it never makes the reclassifier eager."""
    from unittest.mock import AsyncMock

    from app.models import DeploymentStatus
    from app.release_artifacts import _check_backend_revision_serving

    promoted_at = _now() - promotion_age
    version = _Version(
        promoted_sha=_SHA,
        promoted_at=promoted_at,
        deploy_confirm_deadline=None,
    )
    ds = _DeployState(
        status=DeploymentStatus.STABLE,
        current_sha="b" * 40,
        deployed_at=promoted_at + deployed_at_delta,
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    session = _DispatchSession(project=project, ds=ds)
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=ancestry))
    # No deploy.yml run for the promoted SHA — the predates-promotion timeout
    # is substantiated as never-dispatched rather than the bare no_new_revision.
    mocker.patch(
        "app.github.get_workflow_run_for_sha", new=AsyncMock(return_value=None)
    )

    out = await _check_backend_revision_serving(
        session,
        version,
        _Artifact(BACKEND_REVISION_ARTIFACT_KIND, None),
        _now(),
    )
    assert out == expected


# ---------------------------------------------------------------------------
# no_new_revision substantiation via the deploy workflow run listing
# ---------------------------------------------------------------------------


def _predates_promotion_stale_mismatch(mocker):
    """Shared setup for the no_new_revision substantiation tests: a STABLE
    revision predating the promotion, determinately NOT a descendant, past the
    derived confirmation deadline, with GitHub creds resolved."""
    from unittest.mock import AsyncMock

    from app.models import DeploymentStatus

    promoted_at = _now() - timedelta(hours=2)
    version = _Version(
        promoted_sha=_SHA,
        promoted_at=promoted_at,
        deploy_confirm_deadline=None,
    )
    ds = _DeployState(
        status=DeploymentStatus.STABLE,
        current_sha="b" * 40,
        deployed_at=promoted_at - timedelta(minutes=5),  # predates promotion
    )
    project = _Proj(github_owner="acme", github_repo="widget")
    session = _DispatchSession(project=project, ds=ds)
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    return session, version


@pytest.mark.asyncio
async def test_no_new_revision_substantiated_when_deploy_workflow_never_dispatched(
    mocker,
):
    """A promotion whose deploy never happened no longer resolves to a bare
    no_new_revision: the deploy-workflow run listing for the promoted SHA shows
    NO deploy.yml run, so the timeout is substantiated as
    deploy_workflow_never_dispatched — distinct from 'ran and failed' and from
    'ran and the revision is stale'."""
    from unittest.mock import AsyncMock

    from app.release_artifacts import _check_backend_revision_serving

    session, version = _predates_promotion_stale_mismatch(mocker)
    # No deploy.yml run exists for the promoted SHA — the deploy workflow was
    # never dispatched.
    mocker.patch(
        "app.github.get_workflow_run_for_sha", new=AsyncMock(return_value=None)
    )

    out = await _check_backend_revision_serving(
        session,
        version,
        _Artifact(BACKEND_REVISION_ARTIFACT_KIND, None),
        _now(),
    )
    assert out == (
        "confirmation_timed_out",
        "deploy_workflow_never_dispatched",
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "expected_reason"),
    [
        # Completed non-success → the deploy workflow ran and failed.
        (
            {"run_id": 11, "status": "completed", "conclusion": "failure", "html_url": "u"},
            "deploy_workflow_failed",
        ),
        (
            {"run_id": 12, "status": "completed", "conclusion": "cancelled", "html_url": "u"},
            "deploy_workflow_failed",
        ),
        # Completed success → the pipeline shipped but the promoted revision
        # never served (stale revision).
        (
            {"run_id": 13, "status": "completed", "conclusion": "success", "html_url": "u"},
            "deploy_workflow_succeeded_stale_revision",
        ),
        # Still in flight past the deadline → the deploy is stuck in the pipeline.
        (
            {"run_id": 14, "status": "in_progress", "conclusion": None, "html_url": "u"},
            "deploy_workflow_in_progress",
        ),
    ],
)
async def test_no_new_revision_substantiated_by_deploy_workflow_conclusion(
    mocker, run, expected_reason
):
    """The deploy-workflow run conclusion names the specific gate behind the
    no_new_revision timeout: failed, succeeded-but-stale, or still-in-progress."""
    from unittest.mock import AsyncMock

    from app.release_artifacts import _check_backend_revision_serving

    session, version = _predates_promotion_stale_mismatch(mocker)
    mocker.patch(
        "app.github.get_workflow_run_for_sha", new=AsyncMock(return_value=run)
    )

    out = await _check_backend_revision_serving(
        session,
        version,
        _Artifact(BACKEND_REVISION_ARTIFACT_KIND, None),
        _now(),
    )
    assert out == ("confirmation_timed_out", expected_reason, None)


@pytest.mark.asyncio
async def test_no_new_revision_keeps_backstop_when_deploy_workflow_unavailable(mocker):
    """A credential/API failure on the deploy-workflow lookup keeps the honest
    no_new_revision backstop — never fabricate a specific gate from a failed
    lookup (conservative: report, never silently confirm)."""
    from unittest.mock import AsyncMock

    from app.release_artifacts import _check_backend_revision_serving

    session, version = _predates_promotion_stale_mismatch(mocker)
    # The reclassifier resolves credentials once to run the ancestry check;
    # the substantiation's own credential resolution then fails (e.g. a
    # transient gap), so the deploy-workflow lookup cannot run — keep the
    # honest no_new_revision backstop rather than fabricating a specific gate.
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(side_effect=[("tok", "acme", "widget"), None]),
    )
    mocker.patch(
        "app.github.get_workflow_run_for_sha",
        new=AsyncMock(side_effect=AssertionError("must not be called without creds")),
    )

    out = await _check_backend_revision_serving(
        session,
        version,
        _Artifact(BACKEND_REVISION_ARTIFACT_KIND, None),
        _now(),
    )
    assert out == ("confirmation_timed_out", "no_new_revision", None)


@pytest.mark.asyncio
async def test_no_new_revision_keeps_backstop_when_workflow_lookup_raises(mocker):
    """An exception from the deploy-workflow lookup keeps no_new_revision."""
    from unittest.mock import AsyncMock

    from app.release_artifacts import _check_backend_revision_serving

    session, version = _predates_promotion_stale_mismatch(mocker)
    mocker.patch(
        "app.github.get_workflow_run_for_sha",
        new=AsyncMock(side_effect=RuntimeError("github exploded")),
    )

    out = await _check_backend_revision_serving(
        session,
        version,
        _Artifact(BACKEND_REVISION_ARTIFACT_KIND, None),
        _now(),
    )
    assert out == ("confirmation_timed_out", "no_new_revision", None)


@pytest.mark.asyncio
async def test_redetection_tick_cannot_latch_deploy_failed(mocker):
    """The periodic re-detection tick (app.main._reconcile_deploy_state_health_tick)
    must stamp DeploymentState.deployed_at with this process's actual boot time,
    not the tick's fire time — otherwise a tick that only now notices (or
    finally commits, after an earlier degraded write) a revision that has been
    serving since BEFORE the promotion was stamped fabricates a deployed_at
    that postdates promoted_at. That flips the supersession reclassifier's
    "predates promotion, still waiting" branch into "wrong deploy after
    promotion", latching a healthy in-flight promotion to deploy_failed."""
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel

    import app.main as _main
    from app.models import DeploymentState, DeploymentStatus
    from app.release_artifacts import _check_backend_revision_serving

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.main.AsyncSessionLocal", Session)

    try:
        # This process actually started serving the current revision 10
        # minutes BEFORE the promotion was stamped — the promoted deploy has
        # not landed yet, the old revision is still live.
        promoted_at = _now()
        real_boot_time = promoted_at - timedelta(minutes=10)
        mocker.patch.object(_main, "_process_boot_time", real_boot_time)

        # Seed a stale singleton (a prior degraded boot commit never landed)
        # and let the health tick fire long AFTER boot — simulating a tick
        # that only now notices the drift, well past the promotion.
        async with Session() as session:
            session.add(
                DeploymentState(
                    id=1,
                    current_revision="old-rev",
                    current_sha="old-sha",
                    last_known_good_revision="old-rev",
                    last_known_good_sha="old-sha",
                    status=DeploymentStatus.STABLE,
                )
            )
            await session.commit()

        mocker.patch.object(_main, "_current_revision", "new-rev")
        mocker.patch.object(_main, "_current_sha", "unknown")
        mocker.patch.object(
            _main, "resolve_deployed_sha", new=AsyncMock(return_value="cccccccccc")
        )

        reconciled = await _main._reconcile_deploy_state_health_tick()
        assert reconciled is True

        async with Session() as session:
            ds = await session.get(DeploymentState, 1)
            assert ds is not None
            # The bug this test guards against: deployed_at stamped with
            # "now" (well after promoted_at) instead of the real boot time.
            assert ds.deployed_at == real_boot_time
            assert ds.deployed_at < promoted_at

        # Feed the reconciled row into the actual serving-confirmation
        # consumer: a STABLE sha_mismatch (observed revision diverged, not a
        # descendant of the promoted commit) whose deployed_at PREDATES the
        # promotion must defer/time out, never latch deploy_failed.
        version = _Version(
            promoted_sha=_SHA,
            promoted_at=promoted_at,
            deploy_confirm_deadline=None,
        )
        project = _Proj(github_owner="acme", github_repo="widget")
        session_dispatch = _DispatchSession(project=project, ds=ds)
        mocker.patch(
            "app.release_promotion._resolve_github_with_session",
            new=AsyncMock(return_value=("tok", "acme", "widget")),
        )
        mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
        # No deploy.yml run for the promoted SHA — the timeout is substantiated
        # as never-dispatched rather than the bare no_new_revision backstop.
        mocker.patch(
            "app.github.get_workflow_run_for_sha", new=AsyncMock(return_value=None)
        )

        out = await _check_backend_revision_serving(
            session_dispatch,
            version,
            _Artifact(BACKEND_REVISION_ARTIFACT_KIND, None),
            promoted_at + timedelta(hours=2),  # well past the derived deadline
        )
        assert out is not None
        assert out[0] != "deploy_failed"
        assert out == (
            "confirmation_timed_out",
            "deploy_workflow_never_dispatched",
            None,
        )
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)
        await engine.dispose()


# The promotion_merge provider must be registered REQUIRED + merge-observable so
# every promotion (any GitHub project) gets armed and gated on it.
def test_promotion_merge_registered_required_and_merge_observable():
    provider = artifact_registry._ARTIFACT_PROVIDERS[PROMOTION_MERGE_ARTIFACT_KIND]
    assert provider.required is True
    # Observable for any GitHub project, regardless of deploy identity.
    assert provider.observable(_Proj(github_owner="x", github_repo="y"), None) is True
    assert provider.observable(_Proj(), None) is False
