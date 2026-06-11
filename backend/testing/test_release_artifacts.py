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

import pytest

from app import release_artifacts as artifact_registry
from app.release_artifacts import (
    AGENT_IMAGE_ARTIFACT_KIND,
    BACKEND_REVISION_ARTIFACT_KIND,
    PROMOTION_MERGE_ARTIFACT_KIND,
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


# The promotion_merge provider must be registered REQUIRED + merge-observable so
# every promotion (any GitHub project) gets armed and gated on it.
def test_promotion_merge_registered_required_and_merge_observable():
    provider = artifact_registry._ARTIFACT_PROVIDERS[PROMOTION_MERGE_ARTIFACT_KIND]
    assert provider.required is True
    # Observable for any GitHub project, regardless of deploy identity.
    assert provider.observable(_Proj(github_owner="x", github_repo="y"), None) is True
    assert provider.observable(_Proj(), None) is False
