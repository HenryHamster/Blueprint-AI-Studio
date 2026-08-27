# Shared test helpers extracted verbatim from test_release_qualifier.py (domain split).
# Star-imported by the test_release_qualifier__*.py bucket files; re-exports the
# complete original module namespace via __all__ so no test loses a symbol.
"""Tests for the release qualifier (automain LTS Phase 3).

Covers:
  * compute_composite — each hard blocker forces confidence 0.0; the
    policy-weighted sum; the promote threshold boundary; the audit-missing vs
    test_file_deleted blocker-label ordering; the smoketest-disabled mode
    (dimension dropped + weights renormalized — audit H3).
  * _load_policy — parses promotion_policy.yaml; falls back to hardcoded
    defaults on any read/parse failure (never raises), including a weight-sum
    that does not total ~1.0.
  * _score_audit / _score_smoketest — artifact → sub-score derivation,
    including the deleted-test-file and startup-failure hard-blocker sentinels.
  * run_qualification — phase + in-flight + already-dry-run guards (audit H1);
    the FOR UPDATE row-lock serialization (audit C3); the candidate_sha-pinned
    audit payload + main...<candidate_sha> diff (audit C4); the smoketest-skipped
    spawn path when smoketest_enabled is False (audit H3); the deferred
    binding-challenge spawn once audit findings exist.
  * _finalize_qualification — promote / dry-run / not-promoted decision paths;
    the smoketest-disabled drop-dimension path.
  * promote — success path (create release branch at candidate_sha, open +
    merge release-v{N} → main PR, advance version — audit C4); automain
    advancing is now harmless (no staleness guard); the stale-release-branch
    recreate path; the real-merge-SHA recording; the non-merged result →
    FAILED-promotion path; and the project_not_found → failure path.  Round 7:
    promote() does NOT commit a caller-supplied unrelated transaction; an
    already-merged PR returning {merged: False} is recorded as success not
    failure (disambiguated via the PR's own merged boolean); the early-return
    failure path does not expire() un-flushed mutations.
  * maybe_finalize_qualification — Round 7: opens its OWN dedicated session, so
    a caller's unrelated dirty work is never committed by promote(); concurrent
    (lock-serialized) finalize calls never double-promote.
  * sweep_stuck_qualifications — defense-in-depth reaper for qualifications
    stuck pending; the qualification-timeout force-finalize (audit H2); the
    per-version session isolation (audit H5).
"""

from __future__ import annotations
from datetime import timezone

import asyncio
import contextlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import func as sa_func
from sqlalchemy import select

from app import release_qualifier as rq
from app import release_scoring as rs
from app import release_version as rv
from app import github_refs
from app.release_commit_authorship import default_authorship_evidence_contract
from app.models import (
    Alert,
    Artifact,
    ArtifactType,
    Project,
    ProjectMessage,
    QualificationDimensionResult,
    QualificationVerdict,
    CompletionDebtEntry,
    CriterionEvidence,
    Task,
    TaskStatus,
    VersionAcceptanceCriterion,
)

# Fixed test policy for the compute_composite scoring-math tests.  Weights
# mirror promotion_policy.yaml; the threshold is pinned at 0.75 here so the
# threshold-boundary tests stay deterministic regardless of the shipped value.
_POLICY = {
    "weights": {"audit": 4 / 7, "test_erosion": 3 / 7},
    "threshold": 0.75,
}


def _shared_session_factory(db_session):
    """Return a session-factory that hands back the test's shared ``db_session``.

    Round 7: the release qualifier's finalize→promote chain
    (``maybe_finalize_qualification`` / ``sweep_stuck_qualifications`` →
    ``_finalize_in_dedicated_session``) opens its OWN session via an injected
    factory — in production that is ``AsyncSessionLocal`` (a genuine second DB
    connection), which fully decouples ``promote()``'s commits from the
    completion handler's transaction.

    Under the test harness the SQLite in-memory DB is a single
    ``StaticPool`` connection, so the "dedicated" session must BE the test
    session.  This factory yields ``db_session`` inside an async context
    manager whose ``__aexit__`` is a deliberate no-op — it does NOT close or
    roll back the session, so the test still owns its lifecycle and can run
    assertions after the function under test returns.

    Used as ``session_factory=_shared_session_factory(db_session)``.
    """

    @contextlib.asynccontextmanager
    async def _factory():
        # Yield the shared session; on exit do nothing (the test owns close()).
        yield db_session

    return _factory


# ===========================================================================
# _load_policy — read promotion_policy.yaml, fall back on failure
# ===========================================================================


def _point_policy_at(mocker, tmp_path, content: str | None):
    """Repoint rq._POLICY_PATH at a temp file (or a nonexistent path).

    Path instances are immutable, so the read_text method cannot be patched on
    the instance — instead the module attribute is swapped for a real Path that
    either holds ``content`` or does not exist (when ``content`` is None).  This
    exercises the genuine read_text + yaml.safe_load code path.

    ``_load_policy`` now resolves the live runtime_config rubric FIRST (it is
    seeded from the YAML at startup and is admin-tunable), so to exercise the
    YAML-file tier these tests must clear that in-memory override.  The
    runtime_config promotion values are temporarily set to None for the test.
    """
    # Clear the live runtime_config rubric so _load_policy falls through to the
    # YAML-file tier this helper is testing.
    mocker.patch.dict(
        rq._runtime_config,
        {"promotion_threshold": None, "promotion_weights": None},
        clear=False,
    )
    target = tmp_path / "promotion_policy.yaml"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    # else: leave the file absent so read_text raises FileNotFoundError.
    mocker.patch.object(rq, "_POLICY_PATH", target)
    return target


def _make_verified_finding(summary: str, file: str = "") -> dict:
    """Return a verified high-severity finding for reconciliation tests."""
    return {
        "severity": "high",
        "summary": summary,
        "file": file,
        "verification_note": "confirmed via full repo scan with supporting evidence",
    }


def _make_challenge_envelope(sha: str, verdicts: list) -> dict:
    return {"challenged_candidate_sha": sha, "verdicts": verdicts}


async def _open_hotfix_backfill_debt(db_session, project, candidate_sha: str):
    """Open a hotfix_deploy_pending_backfill debt row for ``project``, mirroring
    what ``_record_hotfix_audit_trail`` writes after an operator hotfix-deploy."""
    from app import completion_debt as cd

    opened = await cd.arecord_completion_debt(
        db_session,
        dedupe_key=f"hotfix_deploy:{project.id}:{candidate_sha}",
        kind=cd.KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
        title=f"Hotfix-deploy of release machinery at {candidate_sha[:8]} owes re-qual",
        project_id=project.id,
        source="hotfix_promote",
        detail={"candidate_sha": candidate_sha},
    )
    assert opened is True
    await db_session.commit()


async def _hotfix_backfill_row(db_session, project, candidate_sha: str):
    return (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key
                == f"hotfix_deploy:{project.id}:{candidate_sha}"
            )
        )
    ).scalar_one()


# ===========================================================================
# Shared fixtures for the DB-backed coordinator tests
# ===========================================================================


async def _make_project_and_version(db_session, *, phase: str = "build"):
    """Create a self-repo project with an authorized GitHub repo + open v1.

    ``phase`` defaults to "build" (open_version's default).  Pass
    phase="stabilize" for tests that exercise run_qualification, whose phase
    guard only admits a stabilize-phase candidate.
    """
    project = Project(
        name="RQ-Test",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        # Distinct integration/promotion pair (the multi-branch LTS model these
        # promote tests intend — head=release-vN, base="main"). Without an
        # explicit pair a non-self repo resolves to main/main, which is the
        # single-branch case promote() now short-circuits as lts_inactive.
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    if phase != "build":
        rv.set_version_phase(version, phase, reasoning="test setup")
        db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


def _branch_sha_mock(
    *, automain: str = "automn0" * 5 + "00000", main: str = "main00" * 6 + "0000"
):
    """An AsyncMock for github.get_branch_sha keyed on the requested branch.

    After the audit-C4 fix promote() pins the PR to the candidate commit and
    no longer re-resolves automain (the staleness guard is gone) — its only
    get_branch_sha call is for "main" (the post-merge HEAD).  run_qualification
    still resolves "automain" to snapshot the candidate.  This mock answers
    both branches so the same helper serves run_qualification and promote
    tests; ``main`` lets a promote test assert promoted_sha is the new main
    HEAD (the real merge commit, not the candidate).
    """

    async def _impl(token, owner, repo, branch, project_id=None, *, live=False):
        if branch == "automain":
            return automain
        if branch == "main":
            return main
        raise AssertionError(f"unexpected branch {branch!r}")

    return AsyncMock(side_effect=_impl)


def _create_branch_mock():
    """An AsyncMock for github.create_branch that echoes the requested SHA.

    After the audit-C4 fix promote() creates a release-v{N} branch pinned to
    the candidate commit before opening the PR.  github.create_branch returns
    ``{"ref": ..., "object": {"sha": <sha>}}``; this mock echoes the SHA it was
    asked to create so promote()'s "branch already exists at a stale SHA"
    delete+recreate path is NOT triggered (the branch lands exactly where
    asked).  Tests that exercise the stale-branch path mock create_branch
    explicitly.
    """

    async def _impl(token, owner, repo, branch_name, sha):
        return {"ref": f"refs/heads/{branch_name}", "object": {"sha": sha}}

    return AsyncMock(side_effect=_impl)


def _patch_promote_github_defaults(mocker, *, already_on_main: bool = False):
    """Patch the GitHub helpers promote() calls that are NOT the focus of a test.

    promote() makes several GitHub calls beyond branch/PR/merge:

      * ``github.is_ancestor_of`` — receipt-merge reachability plus candidate
        ancestry corroboration.  Candidate ancestry alone is never promotion
        authority.  ``already_on_main=True`` also exposes a deterministic exact
        merged-PR receipt for compatibility with older fixture call sites.
      * ``github.delete_branch`` — the best-effort release-branch cleanup
        (R4-H2).  Defaulted to a no-op success so a test that does not
        specifically assert on branch deletion does not make a real network
        call.

    Returns ``(is_ancestor_mock, delete_branch_mock)`` so a test can assert on
    either when it cares. A test that specifically exercises exact receipt
    identity should patch the full PR resource itself.
    """
    receipt_sha = "receipt0" * 5

    async def _is_ancestor(_token, _owner, _repo, ancestor, _descendant, **_kwargs):
        # Exact receipt reachability is independent from whether the candidate
        # was already present before this promote() attempt.
        if ancestor == receipt_sha:
            return True
        return already_on_main

    is_ancestor_mock = mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_is_ancestor),
    )
    delete_branch_mock = mocker.patch(
        "app.github.delete_branch",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(
            return_value=([{"number": 9001}] if already_on_main else [])
        ),
    )

    async def _get_pull_request(_token, _owner, _repo, number, **_kwargs):
        from app import github as _github

        create_branch_call = getattr(_github.create_branch, "call_args", None)
        create_pr_call = getattr(_github.create_pull_request, "call_args", None)
        if create_branch_call is not None:
            head_ref = create_branch_call.args[3]
            candidate_sha = create_branch_call.args[4]
        else:
            head_ref = "release-v1"
            ancestry_call = is_ancestor_mock.call_args
            candidate_sha = ancestry_call.args[3] if ancestry_call is not None else "c" * 40
        base_ref = (
            create_pr_call.kwargs.get("base", "main")
            if create_pr_call is not None
            else "main"
        )
        merge_mock = _github.merge_pull_request
        merge_called = bool(getattr(merge_mock, "call_count", 0))
        merge_return = getattr(merge_mock, "return_value", None)
        merged_now = bool(
            already_on_main
            or (
                merge_called
                and isinstance(merge_return, dict)
                and merge_return.get("merged") is True
            )
        )
        return {
            "number": number,
            "state": "closed" if merged_now else "open",
            "merged": merged_now,
            "html_url": f"http://pr/{number}",
            "head": {"ref": head_ref, "sha": candidate_sha},
            "base": {"ref": base_ref, "sha": "base0001"},
            "merge_commit_sha": receipt_sha if merged_now else None,
        }

    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=_get_pull_request),
    )
    return is_ancestor_mock, delete_branch_mock


@pytest.fixture(autouse=True)
def _qualifier_ci_resolves_green(mocker, monkeypatch):
    """Default the qualifier's CI resolution to a real completed green build.

    Both ``run_qualification`` and ``_finalize_qualification`` resolve CI for
    the candidate commit through ``_resolve_candidate_ci``, which calls the
    GitHub check-runs + PR-for-commit + installation-token helpers.  The
    overwhelming majority of coordinator tests exercise guards, scoring,
    promote paths or notifications where CI is incidental and the candidate is
    assumed green.

    This autouse fixture patches those GitHub helpers so ``_resolve_candidate_ci``
    runs for real and resolves GREEN by default — the candidate commit reports
    a completed, all-passing build.  Tests that exercise the CI gate itself
    re-patch ``get_check_runs_cached`` / ``list_pull_requests_for_commit`` in
    the test body; the later patch wins.  Pure-function tests (compute_composite
    etc.) never reach these helpers, so the patches are harmless there.
    """
    monkeypatch.setenv(
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL",
        "postgresql+asyncpg://scratch-user@localhost/scratch-control",
    )
    # Qualifier fixtures use acme/widget and a synthetic token. Any GitHub
    # helper not explicitly mocked by the test must fail closed immediately,
    # never escape to the real network and consume the per-test timeout in
    # transport retries. Tests of a particular HTTP outcome re-patch this seam
    # in their body, after this autouse fixture.
    blocked_github_response = httpx.Response(
        401,
        json={"message": "GitHub network disabled in qualifier tests"},
        request=httpx.Request("GET", "https://api.github.invalid/test-blocked"),
    )
    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=blocked_github_response),
    )
    # Candidate authorship is a live GitHub gate in production. Coordinator
    # tests use synthetic SHAs and exercise its decision contract separately;
    # keep their unrelated finalize assertions deterministic with a clean
    # candidate-range measurement.
    def _synthetic_candidate_head(version):
        # The real ``candidate_authorship_range`` reads the ``candidate_sha``
        # COLUMN, which ``run_qualification`` stamps before it spawns any
        # child (before ``last_qualification['candidate_sha']`` is written at
        # the final pending-record stamp). Prefer the column so a spawn-time
        # authorship measurement sees the same candidate a finalize-time one
        # does; fall back to the qual dict for tests that seed a candidate via
        # ``last_qualification`` only.
        return (
            getattr(version, "candidate_sha", None)
            or (version.last_qualification or {}).get("candidate_sha")
            or "b" * 40
        )

    async def _clean_candidate_authorship(session, version):
        return {
            "state": "resolved",
            "count": 0,
            "offenders": [],
            "range": {"base_sha": "a" * 40, "head_sha": _synthetic_candidate_head(version)},
        }

    def _synthetic_candidate_range(version):
        return ("a" * 40, _synthetic_candidate_head(version))

    mocker.patch.object(
        rq,
        "_measure_candidate_operator_authorship",
        new=AsyncMock(side_effect=_clean_candidate_authorship),
    )
    mocker.patch.object(
        rq, "candidate_authorship_range", side_effect=_synthetic_candidate_range
    )
    mocker.patch(
        "app.release_commit_authorship.candidate_authorship_range",
        side_effect=_synthetic_candidate_range,
    )
    # Finalization may retire archival child VMs or trigger orphan inventory as
    # an incidental side effect. Keep qualifier tests outside the real GCE SDK;
    # compute-specific tests own these boundaries and qualifier tests can
    # re-patch them when the interaction itself is under test.
    mocker.patch("app.compute._list_vms_in_zone", return_value=[])
    mocker.patch("app.compute._delete_gcp", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.dispatch_workflow_run",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.release_prepromotion_rehearsal.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "success",
                "details": [],
                "failed_logs": "",
            }
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": "feedface" * 5},
                }
            ]
        ),
    )
    # Default the qualify-on-synced gate to SYNCED so the overwhelming majority
    # of run_qualification tests (which assume the candidate is promotable)
    # proceed without a real compare call. Patches the gate's OWN seam (not the
    # shared github.is_ancestor_of, which promote-idempotency + TestIsAncestorOf
    # depend on). Tests that exercise the gate re-patch it; the later patch wins.
    mocker.patch(
        "app.release_qualifier._is_target_absorbed",
        new=AsyncMock(return_value=True),
    )
    # Default the pre-merge Alembic dual-head guard to a PR that touches NO
    # migration files, so promote()'s enforce_premerge_migration_guard returns
    # ("ok") and the merge proceeds unchanged for the majority of promote tests
    # where migration safety is incidental. Tests that exercise the guard
    # re-patch get_pr_files (or the diff / version-listing helpers) in their own
    # body; the later patch wins.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[]),
    )
    # Default the finalize-time deterministic Alembic revision-identity gate to
    # CLEAN: _finalize_qualification lists the candidate's and the promotion
    # target's migration filenames via github.list_versions_at_ref and runs
    # find_content_divergent_revisions over them. Returning the SAME list for
    # every ref means no revision id diverges, so the gate is "ok" and the
    # finalize proceeds to the quality composite for the majority of tests where
    # migration safety is incidental. Tests exercising the gate re-patch this in
    # their own body (branch-aware / raising); the later patch wins.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0001_init.py", "0002_feature.py"]),
    )
    # The identity gate reads blob shas (list_version_shas_at_ref) to skip body
    # fetches for unchanged migrations. Default both refs to the SAME sha map so
    # every shared file is byte-identical ('unchanged') and the gate stays 'ok'
    # without any body fetch — mirrors the list_versions_at_ref default above.
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=AsyncMock(
            return_value={"0001_init.py": "sha1", "0002_feature.py": "sha2"}
        ),
    )
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value="\"\"\"test migration body\"\"\"\n"),
    )
    # Default the finalize-time scratch-DB migration-APPLY gate to CLEAN.
    # _finalize_qualification calls run_migration_apply_gate, which in production
    # reproduces the candidate's upgrade against production's recorded state on
    # an isolated Postgres scratch DB. The test session is SQLite, so the real
    # gate would 'defer' (non-Postgres bind) and HOLD every finalize as an infra
    # failure. Defaulting it to ("ok", None) keeps migration-apply safety
    # incidental for the majority of tests; tests exercising the gate re-patch
    # this in their own body (block / defer); the later patch wins.
    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(return_value=("ok", None)),
    )


# ===========================================================================
# run_qualification — qualify-on-synced gate (LTS split)
# ===========================================================================


async def _audit_tasks(db_session, project_id):
    rows = (
        (await db_session.execute(select(Task).where(Task.project_id == project_id)))
        .scalars()
        .all()
    )
    return [t for t in rows if t.first_payload.get("type") == "release_audit"]


def _promotion_failed_record(
    *,
    candidate_sha: str,
    surface_hash: str,
    promotion_failure_count: int = 1,
    confidence: float = 0.96,
    threshold: float = 0.90,
):
    """A representative ``promotion_failed`` last_qualification record.

    A promotion_failed verdict is the PASSING would-promote base_record PLUS the
    promotion-outcome keys (status/error/promotion_failure_count) — see
    ``_record_promotion_failure``.  Carries ``candidate_diff_hash`` so the
    passing-audit reuse gate can compare it to the live audited surface.
    """
    return {
        "status": "promotion_failed",
        "error": "ci_red",
        "promotion_failure_count": promotion_failure_count,
        "candidate_sha": candidate_sha,
        "candidate_diff_hash": surface_hash,
        "ci_green": True,
        "ci_resolution": {"state": "green", "ci_green": True},
        "confidence": confidence,
        "sub_scores": {"audit": 1.0, "test_erosion": 1.0},
        "blocker": None,
        "threshold": threshold,
        "smoketest_enabled": False,
        "started_at": "2026-07-01T00:00:00",
        "finalized_at": "2026-07-01T00:05:00",
        "migration_safety": "ok",
        "migration_apply_safety": "ok",
    }


# ===========================================================================
# _finalize_qualification — phase 2: collect + decide
# ===========================================================================


async def _seed_qualification_children(
    db_session,
    project,
    version,
    *,
    audit_logs: dict | None,
    smoke_logs: dict | None,
    challenge_verdicts: list[dict] | None = None,
    ci_green: bool = True,
    version_goal: str | None = None,
    seed_merge_grade: bool = False,
    merge_grade_logs: dict | None = None,
    declare_authorship_budget: bool = True,
):
    """Create the qualifier child tasks + their report artifacts, and stamp
    version.last_qualification with their ids (pending state).

    ``challenge_verdicts`` — when supplied, a RELEASE_AUDIT_CHALLENGE artifact
    is created for a new challenge task with those verdicts so the fail-closed
    audit-challenge gate can resolve them.  When None, no challenge artifact is
    created; tests with NO verified higher-severity findings proceed without one
    (the gate is inert), while tests with verified findings that want to reach
    scoring/promotion must pass ``challenge_verdicts`` or the gate will hold.
    """
    _candidate_sha = "feed" * 10

    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": _candidate_sha},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="vm-smoke",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    if audit_logs is not None:
        # Normalise to the real RELEASE_AUDIT_REPORT contract: a clean audit
        # emits an explicit ``findings: []`` (the agent always includes the key).
        # A fixture that omits the key entirely is the malformed ``no_findings_key``
        # shape that _score_audit now scores as unmeasured — inject the empty list
        # so "clean audit" fixtures keep meaning clean, while fixtures that supply
        # findings/issues are left untouched.
        _audit_logs = dict(audit_logs)
        # An empty-dict report is intentionally left as-is (it exercises the
        # audit_report_empty_dict infra-failure path); only a NON-empty report
        # missing the findings section gets the empty-list normalization.
        if _audit_logs and "findings" not in _audit_logs and "issues" not in _audit_logs:
            _audit_logs["findings"] = []
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(_audit_logs),
            )
        )
    if smoke_logs is not None:
        db_session.add(
            Artifact(
                task_id=smoke_task.id,
                artifact_type=ArtifactType.SMOKETEST_RESULT,
                logs=json.dumps(smoke_logs),
            )
        )

    challenge_task_id: str | None = None
    if challenge_verdicts is not None:
        challenge_task = Task(
            project_id=project.id,
            title="audit-challenge",
            payloads=[{"type": "release_audit_challenge", "base_branch": "main"}],
            vm_instance_id="vm-challenge",
            status=TaskStatus.COMPLETED,
            context={"candidate_sha": _candidate_sha},
        )
        db_session.add(challenge_task)
        await db_session.flush()
        db_session.add(
            Artifact(
                task_id=challenge_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_CHALLENGE,
                logs=json.dumps(
                    {
                        "challenged_candidate_sha": _candidate_sha,
                        "verdicts": challenge_verdicts,
                    }
                ),
            )
        )
        await db_session.flush()
        await db_session.refresh(challenge_task)
        challenge_task_id = str(challenge_task.id)

    merge_grade_task_id: str | None = None
    merge_grade_task: Task | None = None
    if seed_merge_grade:
        merge_grade_task = Task(
            project_id=project.id,
            title="merge-grade",
            payloads=[{"type": "release_merge_grade", "base_branch": "main"}],
            vm_instance_id="vm-merge-grade",
            status=TaskStatus.COMPLETED,
            context={"candidate_sha": _candidate_sha},
        )
        db_session.add(merge_grade_task)
        await db_session.flush()
        if merge_grade_logs is not None:
            db_session.add(
                Artifact(
                    task_id=merge_grade_task.id,
                    artifact_type=ArtifactType.RELEASE_MERGE_GRADE,
                    logs=json.dumps(merge_grade_logs),
                )
            )
        await db_session.flush()
        await db_session.refresh(merge_grade_task)
        merge_grade_task_id = str(merge_grade_task.id)

    if version_goal is not None:
        version.goal = version_goal

    if declare_authorship_budget:
        await _declare_test_authorship_budget(db_session, project, version)

    rubric_snapshot, rubric_lifecycle = await rq._snapshot_qualification_rubric(
        db_session, version
    )
    rubric_fingerprint = rq._rubric_fingerprint(rubric_snapshot)
    promotion_policy = rq._load_policy(project)
    # This generic fixture models the ordinary non-preflight path. Tests for the
    # optional preflight gate explicitly opt in and restamp the frozen contract.
    env_preflight_enabled = False
    if merge_grade_task is not None:
        merge_grade_task.payloads = [
            {
                **(merge_grade_task.first_payload or {}),
                "categories": [
                    {"category_key": row["criterion_key"], "text": row["text"]}
                    for row in rubric_snapshot
                ],
                "rubric_fingerprint": rubric_fingerprint,
            }
        ]
        db_session.add(merge_grade_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _candidate_sha,
        "ci_green": ci_green,
        "smoketest_enabled": True,
        "env_preflight_enabled": env_preflight_enabled,
        "started_at": "2026-05-19T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
        "challenge_task_id": challenge_task_id,
        "merge_grade_task_id": merge_grade_task_id,
        "rubric_snapshot": rubric_snapshot,
        "rubric_fingerprint": rubric_fingerprint,
        "rubric_lifecycle_snapshot": rubric_lifecycle,
        "version_goal": version.goal or "",
        "promotion_policy_snapshot": promotion_policy,
        "capability_proof_mode": rq._runtime_config.get(
            "capability_proof_gate_mode", "off"
        ),
        "contract_fingerprint": rq._qualification_contract_fingerprint(
            version.goal or "",
            rubric_fingerprint,
            promotion_policy=promotion_policy,
            capability_proof_mode=rq._runtime_config.get(
                "capability_proof_gate_mode", "off"
            ),
            env_preflight_enabled=env_preflight_enabled,
            requested_probes=[],
        ),
        "requested_probes": [],
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task


async def _add_version_criterion(
    db_session,
    project,
    version,
    *,
    key: str,
    text: str,
    source_kind: str | None = None,
    sync_pending_rubric: bool = True,
):
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=key,
        text=text,
        created_by="test",
    )
    db_session.add(criterion)
    await db_session.flush()
    if source_kind is not None:
        detail = {"verdict": "merge-test"}
        if source_kind == "task_merge":
            detail["proven"] = True
        db_session.add(
            CriterionEvidence(
                project_id=project.id,
                criterion_id=criterion.id,
                source_kind=source_kind,
                source_id=f"merge-test-{key}",
                detail=detail,
            )
        )
    await db_session.commit()
    await db_session.refresh(criterion)
    if sync_pending_rubric and (version.last_qualification or {}).get("status") == "pending":
        snapshot, lifecycle = await rq._snapshot_qualification_rubric(
            db_session, version
        )
        fingerprint = rq._rubric_fingerprint(snapshot)
        qual = {
            **(version.last_qualification or {}),
            "rubric_snapshot": snapshot,
            "rubric_fingerprint": fingerprint,
            "rubric_lifecycle_snapshot": lifecycle,
        }
        qual = _with_current_qualification_contract(project, version, qual)
        merge_grade_id = qual.get("merge_grade_task_id")
        if merge_grade_id:
            merge_grade_task = await db_session.get(Task, uuid.UUID(str(merge_grade_id)))
            if merge_grade_task is not None:
                merge_grade_task.payloads = [
                    {
                        **(merge_grade_task.first_payload or {}),
                        "categories": [
                            {"category_key": row["criterion_key"], "text": row["text"]}
                            for row in snapshot
                        ],
                        "rubric_fingerprint": fingerprint,
                    }
                ]
                db_session.add(merge_grade_task)
        version.last_qualification = qual
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
    return criterion


async def _seed_walkthrough_compatible_rubric(db_session, project, version):
    """Seed the retired-v26 canonical keys so the walkthrough_e2e probe's
    evidence contract intersects the open version's ACTIVE rubric (the
    request-time compatibility gate in run_qualification)."""
    for _wt_key in ("c-dfdc9d82", "c-f52fd975", "c-5214996f"):
        await _add_version_criterion(
            db_session,
            project,
            version,
            key=_wt_key,
            text=f"walkthrough {_wt_key}",
        )


def _with_current_qualification_contract(project, version, record=None):
    """Return a pending qualification record with a self-consistent contract.

    Tests that intentionally model spawn-time settings must update the contract
    fingerprint alongside those frozen fields; otherwise finalize correctly
    treats the fixture mutation as mid-flight contract drift.
    """
    qual = dict(record if record is not None else (version.last_qualification or {}))
    rubric_snapshot = qual.get("rubric_snapshot") or []
    rubric_fingerprint = rq._rubric_fingerprint(rubric_snapshot)
    policy = qual.get("promotion_policy_snapshot")
    if not isinstance(policy, dict):
        policy = rq._load_policy(project)
    capability_mode = str(
        qual.get("capability_proof_mode")
        or rq._runtime_config.get("capability_proof_gate_mode", "off")
        or "off"
    ).strip().lower()
    version_goal = str(qual.get("version_goal", version.goal or ""))
    requested_probes = list(qual.get("requested_probes") or [])
    env_preflight_enabled = bool(qual.get("env_preflight_enabled", False))
    return {
        **qual,
        "rubric_fingerprint": rubric_fingerprint,
        "version_goal": version_goal,
        "promotion_policy_snapshot": policy,
        "capability_proof_mode": capability_mode,
        "env_preflight_enabled": env_preflight_enabled,
        "requested_probes": requested_probes,
        "contract_fingerprint": rq._qualification_contract_fingerprint(
            version_goal,
            rubric_fingerprint,
            promotion_policy=policy,
            capability_proof_mode=capability_mode,
            env_preflight_enabled=env_preflight_enabled,
            requested_probes=requested_probes,
        ),
    }


# ---------------------------------------------------------------------------
# Phase 1: compose qualification from the per-dimension ledger.
#
# Read-side composition + per-dimension infra isolation + the same-tree
# promotion invariant.  These exercise the production seams added when finalize
# began reading/writing per-(candidate_sha, dimension) facts instead of one
# atomic pass/fail: a single gate's infra failure isolates to its own dimension
# (the others survive), a re-finalize reuses already-resolved dimensions, and a
# promotion still requires a full terminal set at ONE candidate_sha.
# ---------------------------------------------------------------------------

_LEDGER_SHA_A = "a1b2c3d4" * 5  # 40-char candidate SHA
_LEDGER_SHA_B = "b0b0b0b0" * 5  # a DIFFERENT candidate tree


async def _seed_dimension_row(
    db_session,
    project,
    version,
    dimension,
    *,
    status,
    candidate_sha=_LEDGER_SHA_A,
    sub_score=None,
    blocker=None,
    detail=None,
    origin_attempt_id=None,
):
    row = QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=candidate_sha,
        dimension=dimension,
        status=status,
        sub_score=sub_score,
        blocker=blocker,
        detail=detail,
        origin_attempt_id=origin_attempt_id,
    )
    db_session.add(row)
    return row


async def _dimension_rows_by_dim(db_session, version):
    rows = (
        (
            await db_session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.dimension: r for r in rows}


# Fully-controlled gate inputs for finalize reuse-path tests: CI green, both
# migration gates OK.  Keeps the composite dependent ONLY on the resolved-
# dimension facts under test (audit / test_erosion / smoketest), never on live
# GitHub reads.
_REUSE_PATH_PRE_GATES = {
    "ci_green": True,
    "ci_resolution": {"state": "green"},
    "ci_required": True,
    "migration_verdict": "ok",
    "migration_detail": {},
    "apply_verdict": "ok",
    "apply_detail": {},
}


async def _retract_default_authorship_criterion(db_session, version) -> None:
    """Retract the product-default authorship criterion ``open_version`` seeds.

    Many qualifier tests exercise behavior that is orthogonal to the acceptance
    rubric and were written against a version with an EMPTY live rubric.  The
    default ``c-operator-authorship`` policy row is real product behavior, so it
    is retracted explicitly (the same terminal status the criteria API uses)
    rather than skipped — the version keeps a consistent live rubric and the
    test keeps asserting only what it is about.
    """
    rows = (
        (
            await db_session.execute(
                select(VersionAcceptanceCriterion).where(
                    VersionAcceptanceCriterion.release_version_id == version.id,
                    VersionAcceptanceCriterion.criterion_key
                    == "c-operator-authorship",
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "retracted"
        db_session.add(row)
    await db_session.flush()


async def _delete_default_authorship_criterion(db_session, version) -> None:
    """Remove the seeded authorship criterion so the version has NO criteria.

    ``_retract_default_authorship_criterion`` keeps a terminal row, which still
    appears in the coverage projection.  A handful of tests assert the
    genuinely-empty-rubric shape (``total`` 0, ``criteria`` []); for those the
    row must not exist at all, which is the state a version had before the
    product started seeding a default criterion at open time.
    """
    rows = (
        (
            await db_session.execute(
                select(VersionAcceptanceCriterion).where(
                    VersionAcceptanceCriterion.release_version_id == version.id,
                    VersionAcceptanceCriterion.criterion_key
                    == "c-operator-authorship",
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await db_session.delete(row)
    await db_session.flush()


async def _declare_test_authorship_budget(db_session, project, version) -> None:
    """Give promotion-path fixtures the explicit authorship policy production requires."""
    existing_authorship = (
        await db_session.execute(
            select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.release_version_id == version.id,
                VersionAcceptanceCriterion.criterion_key
                == "c-test-operator-authorship",
            )
        )
    ).scalar_one_or_none()
    if existing_authorship is not None:
        return
    db_session.add(
        VersionAcceptanceCriterion(
            project_id=project.id,
            release_version_id=version.id,
            criterion_key="c-test-operator-authorship",
            text="test fixture declares candidate authorship budget",
            status="active",
            created_by="test",
            evidence_contract=default_authorship_evidence_contract(),
            evidence_contract_updated_by="test",
        )
    )
    await db_session.flush()


async def _live_rubric_projection(db_session, version) -> dict:
    """Launched-rubric projection matching the version's CURRENT active rubric.

    ``open_version`` seeds a default operator-authorship criterion, so a
    version's live rubric is never empty.  Stamping a hardcoded empty
    projection makes ``_finalize_qualification`` short-circuit at the
    bounded rubric-drift guard (status ``rubric_changed``) — and, once a
    contract fingerprint is present, at ``qualification_contract_superseded``
    — before the behavior under test is ever reached.  Mirror what
    ``run_qualification`` actually stamps instead.
    """
    snapshot, lifecycle = await rq._snapshot_qualification_rubric(db_session, version)
    return {
        "rubric_snapshot": snapshot,
        "rubric_fingerprint": rq._rubric_fingerprint(snapshot),
        "rubric_lifecycle_snapshot": lifecycle,
    }


def _clean_finalize_pre_gates(
    ci_state: str,
    *,
    ci_green: bool = False,
    ci_required: bool = True,
    include_ci_required: bool = True,
    integration_branch_topology: dict | None = None,
) -> dict:
    ci_resolution: dict = {
        "state": ci_state,
        "resolved_sha": None,
        "detail": f"test {ci_state}",
    }
    if integration_branch_topology is not None:
        ci_resolution["integration_branch_topology"] = integration_branch_topology
    pre_gates = {
        "ci_green": ci_green,
        "ci_resolution": ci_resolution,
        "migration_verdict": "ok",
        "migration_detail": None,
        "apply_verdict": "ok",
        "apply_detail": None,
    }
    if include_ci_required:
        pre_gates["ci_required"] = ci_required
    return pre_gates


async def _seed_relaxed_ci_clean_qualification(db_session):
    project, version = await _make_project_and_version(db_session)
    project.build_config = {
        "requires_ci_checks": False,
        "has_ci": True,
        "ci_required_branches": ["main"],
    }
    db_session.add(project)
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
    return project, version


async def _seed_green_finalize_candidate(db_session, project, version):
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
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    # NOTE: this fixture used to seed a criterion carrying task_merge evidence,
    # purely so the RETIRED coverage gate ("covered ⇒ pass") would not block these
    # CI-composite tests.  Coverage evidence no longer satisfies the acceptance
    # contract — the per-category grade does — so an ungraded criterion would now
    # (correctly) fail the gate closed and these tests would never reach the CI
    # composite they exist to exercise.  A version with NO criteria has no
    # acceptance contract to grade, which is exactly the clean baseline these
    # tests want.
    await db_session.refresh(version)


def _patch_finalize_ci_test_incidental_gates(mocker):
    """Keep non-CI finalize gates clean so tests reach the CI composite."""
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=[]),
    )
    # The migration-identity gate reads blob shas; empty ⇒ no shared migrations ⇒
    # 'ok', keeping migration safety incidental for the CI-composite tests.
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=AsyncMock(return_value={}),
    )
    # When list_pull_requests_for_commit returns [], the code makes a raw
    # _github_request to verify the result is not a transient API failure.
    # Return 200+[] so _resolve_candidate_ci reaches _CI_NO_CHECKS rather
    # than degrading to _CI_PENDING on the fake "gh-token" 401.
    _mock_pr_resp = MagicMock()
    _mock_pr_resp.status_code = 200
    _mock_pr_resp.json.return_value = []
    mocker.patch("app.github._github_request", new=AsyncMock(return_value=_mock_pr_resp))


async def _seed_completed_challenge_without_artifact(db_session, project, version):
    challenge_task = Task(
        project_id=project.id,
        title="audit-challenge",
        payloads=[{"type": "release_audit_challenge", "base_branch": "main"}],
        vm_instance_id="vm-challenge",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": version.last_qualification["candidate_sha"]},
    )
    db_session.add(challenge_task)
    await db_session.flush()
    await db_session.refresh(challenge_task)
    version.last_qualification = {
        **version.last_qualification,
        "challenge_task_id": str(challenge_task.id),
        "audit_challenge_task_id": str(challenge_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return challenge_task


async def _attach_audit_challenge(
    db_session,
    project,
    version,
    *,
    logs: dict | None = None,
    raw_logs: str | None = None,
    status: TaskStatus = TaskStatus.COMPLETED,
):
    task = Task(
        project_id=project.id,
        title="audit challenge",
        payloads=[{"type": "release_audit_challenge", "base_branch": "main"}],
        vm_instance_id="vm-audit-challenge",
        status=status,
        context={
            "release_version_id": str(version.id),
            "candidate_sha": version.last_qualification["candidate_sha"],
            "primary_audit_task_id": version.last_qualification["audit_task_id"],
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    if logs is not None or raw_logs is not None:
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_CHALLENGE,
                logs=json.dumps(logs) if logs is not None else raw_logs,
            )
        )
    version.last_qualification = {
        **version.last_qualification,
        "audit_challenge_task_id": str(task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return task


def _versions_by_ref(*, main_list: list[str], candidate_list: list[str]) -> AsyncMock:
    """Branch-keyed ``list_version_shas_at_ref``: deployed promotion target vs
    candidate, as ``{filename: blob sha}`` maps (the identity gate reads shas and
    derives the filename list from the keys).

    A ``ref == "main"`` request answers with the deployed list and everything
    else (the candidate SHA) answers with the candidate list. ``ref`` is the 4th
    positional arg of ``list_version_shas_at_ref``. Each filename gets a distinct
    name-derived sha; collision/identity divergence is detected from the filenames
    (keys), so the sha values only need to be present.
    """

    async def _side(token, owner, repo, ref, project_id=None):
        names = list(main_list) if ref == "main" else list(candidate_list)
        return {n: f"sha-{n}" for n in names}

    return AsyncMock(side_effect=_side)


# ===========================================================================
# Audit challenge reconciliation — fail-closed gate
# ===========================================================================

_CANDIDATE_SHA = "abcd1234" * 5  # 40-char hex


async def _seed_challenge_artifact(
    db_session,
    project,
    version,
    *,
    audit_task,
    verdicts: list[dict],
    candidate_sha: str = _CANDIDATE_SHA,
) -> None:
    """Create a challenge task + RELEASE_AUDIT_CHALLENGE artifact and stamp
    version.last_qualification with the challenge_task_id."""
    challenge_task = Task(
        project_id=project.id,
        title="audit-challenge",
        payloads=[{"type": "release_audit_challenge", "base_branch": "main"}],
        vm_instance_id="vm-challenge",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(challenge_task)
    await db_session.flush()

    artifact_payload = {
        "challenged_candidate_sha": candidate_sha,
        "verdicts": verdicts,
    }
    db_session.add(
        Artifact(
            task_id=challenge_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_CHALLENGE,
            logs=json.dumps(artifact_payload),
        )
    )

    # Stamp the challenge task id onto last_qualification.
    version.last_qualification = {
        **(version.last_qualification or {}),
        "challenge_task_id": str(challenge_task.id),
        "candidate_sha": candidate_sha,
    }
    # Also update the audit task's candidate_sha to match.
    audit_task.context = {**(audit_task.context or {}), "candidate_sha": candidate_sha}

    db_session.add(version)
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(version)


# ── Operator hotfix-deploy channel ────────────────────────────────────────────


def _hotfix_changed_files_mock(files):
    """AsyncMock for github_refs.get_changed_files_with_patches_between_refs.

    ``files`` is a list of (filename, patch) tuples; returns the
    ``[{"filename":.., "patch":..}, ..]`` shape the real compare helper yields.
    """

    async def _impl(token, owner, repo, base_ref, head_ref, project_id=None):
        return [{"filename": name, "patch": patch} for name, patch in files]

    return AsyncMock(side_effect=_impl)


def _patch_hotfix_promote_github(mocker, *, changed_files):
    """Patch every GitHub helper the hotfix-promote path touches (green defaults).

    Mirrors the force-promote test setup but adds the diff/compare mock the
    hotfix scope check depends on.  ``changed_files`` is a list of
    (filename, patch) tuples describing the candidate diff.
    """
    candidate = "facec0de" * 5  # 40-char candidate sha
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate, main=merge_sha),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    mocker.patch(
        "app.github_refs.get_changed_files_with_patches_between_refs",
        new=_hotfix_changed_files_mock(changed_files),
    )
    _patch_promote_github_defaults(mocker)
    create_branch_mock = mocker.patch(
        "app.github.create_branch", new=_create_branch_mock()
    )
    create_pr_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))
    mocker.patch("app.release_qualifier._spawn_release_audit_task", new=AsyncMock())
    return create_branch_mock, create_pr_mock


# ===========================================================================
# _finalize_qualification — infra-failure detection (FAILED/CANCELLED children)
# ===========================================================================


async def _seed_infra_failure_children(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus,
    smoke_status: TaskStatus = TaskStatus.COMPLETED,
    smoketest_enabled: bool = True,
    prior_infra_streak: int = 0,
) -> tuple[Task, Task | None]:
    """Create qualifier child tasks in requested statuses (no artifacts).

    Use for infra-failure scenarios where children did not produce artifacts.
    Stamps version.last_qualification as pending pointing to the children.
    """
    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-infra",
        status=audit_status,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    smoke_task: Task | None = None
    if smoketest_enabled:
        smoke_task = Task(
            project_id=project.id,
            title="smoke",
            payloads=[{"type": "smoketest", "base_branch": "automain"}],
            vm_instance_id="vm-smoke-infra",
            status=smoke_status,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    if smoke_task is not None:
        await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        **(await _live_rubric_projection(db_session, version)),
        "candidate_sha": "dead" * 10,
        "ci_green": True,
        "smoketest_enabled": smoketest_enabled,
        "started_at": "2026-05-19T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id) if smoke_task else None,
        "prior_infra_streak": prior_infra_streak,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task


async def _infra_verdicts_for_version(
    db_session, version
) -> list[QualificationVerdict]:
    return (
        (
            await db_session.execute(
                select(QualificationVerdict)
                .where(QualificationVerdict.release_version_id == version.id)
                .order_by(QualificationVerdict.created_at)
            )
        )
        .scalars()
        .all()
    )


# ===========================================================================
# maybe_finalize_qualification — task-completion coordination glue
# ===========================================================================


async def _make_task_with_context(
    db_session,
    project,
    *,
    status: TaskStatus,
    context: dict,
) -> Task:
    """Create a minimal Task with the given status and context."""
    task = Task(
        project_id=project.id,
        title="test-task",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id=f"vm-{id(context)}",
        status=status,
        context=context,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _seed_pending_qualification(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus = TaskStatus.COMPLETED,
    smoke_status: TaskStatus = TaskStatus.COMPLETED,
    started_at: str | None = None,
    candidate_sha: str | None = None,
) -> tuple[Task, Task]:
    """Create the two child tasks in the requested statuses and stamp
    version.last_qualification as 'pending' pointing to them.

    Both child tasks carry ``context["release_version_id"]`` so that
    maybe_finalize_qualification recognises them as qualifier children —
    matching the real spawn path in run_qualification.

    ``started_at`` defaults to *now* so the qualification is well within the
    qualification-timeout window — sweep tests for the both-children-terminal
    path must not accidentally trip the age-bound timeout.  Pass an explicit
    old timestamp to exercise the timeout path.

    ``candidate_sha`` defaults to a dummy value; override for testing specific SHAs.
    """
    from app.time_utils import _utcnow

    attempt_id = uuid.uuid4()
    attempt_text = str(attempt_id)

    audit_task = Task(
        project_id=project.id,
        title="audit-child",
        payloads=[{
            "type": "release_audit",
            "base_branch": "main",
            "qualification_attempt_id": attempt_text,
        }],
        vm_instance_id="vm-audit-child",
        status=audit_status,
        context={
            "release_version_id": str(version.id),
            "qualification_attempt_id": attempt_text,
        },
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke-child",
        payloads=[{
            "type": "smoketest",
            "base_branch": "automain",
            "qualification_attempt_id": attempt_text,
        }],
        vm_instance_id="vm-smoke-child",
        status=smoke_status,
        context={
            "release_version_id": str(version.id),
            "qualification_attempt_id": attempt_text,
        },
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    await _declare_test_authorship_budget(db_session, project, version)
    version.last_qualification = {
        "status": "pending",
        **(await _live_rubric_projection(db_session, version)),
        "qualification_attempt_id": attempt_text,
        "candidate_sha": candidate_sha or "abcd" * 10,
        "ci_green": True,
        "smoketest_enabled": True,
        "env_preflight_enabled": False,
        "started_at": started_at or _utcnow().isoformat(),
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    version.active_qualification_attempt_id = attempt_id
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task


async def _seed_merge_grade_child(
    db_session,
    project,
    version,
    *,
    status: TaskStatus,
) -> Task:
    """Attach a blind merge-grade child task in ``status`` to the pending
    qualification and stamp its id into ``version.last_qualification``.

    Mirrors the real run_qualification spawn: the merge-grade child carries the
    active attempt id and ``release_version_id`` so the finalize gate and the
    in-flight guards recognise it as a required qualifier child.
    """
    qual = dict(version.last_qualification or {})
    attempt_text = qual.get("qualification_attempt_id") or str(uuid.uuid4())
    merge_grade_task = Task(
        project_id=project.id,
        title="merge-grade-child",
        payloads=[{
            "type": "release_merge_grade",
            "base_branch": "main",
            "qualification_attempt_id": attempt_text,
        }],
        vm_instance_id="vm-merge-grade-child",
        status=status,
        context={
            "release_version_id": str(version.id),
            "qualification_attempt_id": attempt_text,
        },
    )
    db_session.add(merge_grade_task)
    await db_session.commit()
    await db_session.refresh(merge_grade_task)

    qual["merge_grade_task_id"] = str(merge_grade_task.id)
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return merge_grade_task


async def _finalize_with_smoketest_logs(db_session, smoke_logs: dict):
    """Drive a full qualification finalize with the given SMOKETEST_RESULT logs.

    Returns (result_dict, version) after committing. Mirrors
    test_maybe_finalize_both_children_terminal_calls_finalize's seeding so the
    only variable is the smoketest artifact body.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session, project, version,
        audit_status=TaskStatus.COMPLETED, smoke_status=TaskStatus.COMPLETED,
    )

    # _resolve_launched_rubric returns early (no env_preflight written) when
    # last_qualification has no rubric_snapshot. Patch the fields in so
    # finalization runs the full path, mirroring _seed_qualification_children.
    rubric_snapshot, rubric_lifecycle = await rq._snapshot_qualification_rubric(
        db_session, version
    )
    rubric_fingerprint = rq._rubric_fingerprint(rubric_snapshot)
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": True,
        "rubric_snapshot": rubric_snapshot,
        "rubric_fingerprint": rubric_fingerprint,
        "rubric_lifecycle_snapshot": rubric_lifecycle,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

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
            logs=json.dumps(smoke_logs),
        )
    )
    await db_session.commit()

    result = await rq.maybe_finalize_qualification(
        smoke_task, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    await db_session.refresh(version)
    # Snapshot last_qualification now: a later finalize commits and would expire
    # this version's attributes, making a deferred lazy-load fail.
    return result, dict(version.last_qualification)


async def _seed_pending_qualification_with_challenge(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus = TaskStatus.COMPLETED,
    smoke_status: TaskStatus = TaskStatus.COMPLETED,
    challenge_status: TaskStatus = TaskStatus.COMPLETED,
    started_at: str | None = None,
) -> tuple[Task, Task, Task]:
    """Like _seed_pending_qualification but also creates a challenge child task
    and stamps challenge_task_id in last_qualification."""
    from app.time_utils import _utcnow

    attempt_id = uuid.uuid4()
    attempt_text = str(attempt_id)

    audit_task = Task(
        project_id=project.id,
        title="audit-child",
        payloads=[{"type": "release_audit", "base_branch": "main", "qualification_attempt_id": attempt_text}],
        vm_instance_id="vm-audit-child-ch",
        status=audit_status,
        context={"release_version_id": str(version.id), "qualification_attempt_id": attempt_text},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke-child",
        payloads=[{"type": "smoketest", "base_branch": "automain", "qualification_attempt_id": attempt_text}],
        vm_instance_id="vm-smoke-child-ch",
        status=smoke_status,
        context={"release_version_id": str(version.id), "qualification_attempt_id": attempt_text},
    )
    challenge_task = Task(
        project_id=project.id,
        title="challenge-child",
        payloads=[{"type": "release_audit_challenge", "base_branch": "main", "qualification_attempt_id": attempt_text}],
        vm_instance_id="vm-challenge-child",
        status=challenge_status,
        context={"release_version_id": str(version.id), "qualification_attempt_id": attempt_text},
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    db_session.add(challenge_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)
    await db_session.refresh(challenge_task)

    version.last_qualification = {
        "status": "pending",
        **(await _live_rubric_projection(db_session, version)),
        "qualification_attempt_id": attempt_text,
        "candidate_sha": "abcd" * 10,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": started_at or _utcnow().isoformat(),
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
        "challenge_task_id": str(challenge_task.id),
        "audit_challenge_task_id": str(challenge_task.id),
    }
    version.active_qualification_attempt_id = attempt_id
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task, challenge_task


async def _seed_redrive_common_dimensions(db_session, project, version, sha):
    """Seed the non-audit gates as resolved 'passed' for a re-drive at ``sha``."""
    for dim in ("test_erosion", "smoketest", "ci", "migration_identity",
                "migration_apply"):
        await _seed_dimension_row(
            db_session, project, version, dim, status="passed", candidate_sha=sha,
        )


# ===========================================================================
# Audit H3 — smoketest-disabled finalization (audit-only qualification)
# ===========================================================================


async def _seed_audit_only_pending_qualification(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus = TaskStatus.COMPLETED,
    audit_logs: dict | None = None,
    started_at: str | None = None,
) -> Task:
    """Seed a smoketest-disabled pending qualification — only a release_audit
    child, smoketest_enabled=False, smoketest_task_id None — matching what
    run_qualification produces when runtime_config['smoketest_enabled'] is
    False."""
    from app.time_utils import _utcnow

    attempt_id = uuid.uuid4()
    attempt_text = str(attempt_id)

    audit_task = Task(
        project_id=project.id,
        title="audit-child",
        payloads=[{"type": "release_audit", "base_branch": "main", "qualification_attempt_id": attempt_text}],
        vm_instance_id="vm-audit-only",
        status=audit_status,
        context={"release_version_id": str(version.id), "qualification_attempt_id": attempt_text},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    if audit_logs is not None:
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(audit_logs),
            )
        )

    await _declare_test_authorship_budget(db_session, project, version)
    version.last_qualification = {
        "status": "pending",
        **(await _live_rubric_projection(db_session, version)),
        "qualification_attempt_id": attempt_text,
        "candidate_sha": "abcd" * 10,
        "ci_green": True,
        "smoketest_enabled": False,
        "env_preflight_enabled": False,
        "started_at": started_at or _utcnow().isoformat(),
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
    }
    version.active_qualification_attempt_id = attempt_id
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task


# ===========================================================================
# Operator notifications — the qualifier posts a ProjectMessage at every verdict
# (promoted / blocked / below-threshold / dry-run / promotion-failed) so the
# operator sees release outcomes without polling.
# ===========================================================================


async def _messages_for(db_session, project_id, event_type=None):
    """ProjectMessage rows for a project, optionally filtered by the context
    event_type the qualifier stamps."""
    result = await db_session.execute(
        select(ProjectMessage).where(ProjectMessage.project_id == project_id)
    )
    rows = list(result.scalars().all())
    if event_type is not None:
        rows = [m for m in rows if (m.context or {}).get("event_type") == event_type]
    return rows


# ===========================================================================
# Build-phase stall alerting — _check_version_build_stall
# ===========================================================================


async def _make_merged_task(db_session, project_id, *, updated_at):
    """Create a COMPLETED + pr_merged task so count_merged_since counts it."""
    task = Task(
        project_id=project_id,
        title="merged-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        updated_at=updated_at,
    )
    db_session.add(task)
    await db_session.commit()
    return task


# ===========================================================================
# QualificationVerdict recording — the durable per-verdict history (Phase 4)
# ===========================================================================


async def _ensure_verdict_table(db_session):
    """Create the ``qualificationverdict`` table on the test DB if absent.

    The Alembic migration for this table is authored by a sibling task and is
    not yet applied, so on the Postgres CI engine (whose schema is built from
    migrations) the table does not exist.  Create it idempotently so the
    verdict-history and readiness tests have a real table to exercise; on SQLite
    the per-test ``create_all`` already built it and ``checkfirst`` makes this a
    no-op there (and once the real migration lands).
    """
    from app.models import QualificationVerdict as _QV

    conn = await db_session.connection()
    await conn.run_sync(lambda c: _QV.__table__.create(c, checkfirst=True))
    await db_session.commit()


async def _verdict_rows(db_session, project_id):
    """Return all QualificationVerdict rows for a project, oldest-first."""
    rows = (
        (
            await db_session.execute(
                select(QualificationVerdict)
                .where(QualificationVerdict.project_id == project_id)
                .order_by(QualificationVerdict.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ===========================================================================
# compute_promotion_readiness — the calibration aggregation (Phase 4)
# ===========================================================================


async def _seed_verdict(
    db_session,
    project_id,
    version_id,
    *,
    seq: int,
    would_promote: bool,
    blocker: str | None = None,
    confidence: float = 0.95,
    promotion_enabled: bool = False,
):
    """Insert ONE QualificationVerdict row with a deterministic created_at.

    ``seq`` orders rows in time (higher seq = more recent) so the newest-first
    consecutive-clean-streak walk in compute_promotion_readiness is
    deterministic regardless of insert timing.
    """
    from datetime import timedelta

    from app.time_utils import _utcnow

    row = QualificationVerdict(
        project_id=project_id,
        release_version_id=version_id,
        candidate_sha="ab" * 20,
        confidence=confidence,
        sub_scores={"audit": 1.0, "test_erosion": 1.0, "smoketest": 0.9},
        blocker=blocker,
        would_promote=would_promote,
        promotion_enabled=promotion_enabled,
        terminal_status="dry_run" if would_promote else "not_promoted",
        threshold=0.9,
        created_at=_utcnow() + timedelta(minutes=seq),
    )
    db_session.add(row)
    await db_session.commit()
    return row


# ---------------------------------------------------------------------------
# Acceptance aggregate scoring — the qualifier scores on proven-vs-merged, not
# just merged. Declared-but-unsatisfied acceptance evidence in the open version
# raises an advisory caveat without hard-flipping readiness; undeclared/legacy
# and satisfied-only windows are unaffected.
# ---------------------------------------------------------------------------


async def _seed_landed_acceptance_task(
    db_session,
    project_id,
    *,
    satisfied: bool | None,
    proven: bool = True,
    kind: str = "named_test",
):
    """Insert ONE landed (merged-on-integration) COMPLETED task for a project.

    ``satisfied`` controls the durable code-acceptance verdict stamped on
    ``task.context``: ``True`` with ``proven=True`` → strict proven, ``True``
    with ``proven=False`` → satisfied_unproven, ``False`` →
    declared-but-unsatisfied, ``None`` → no verdict key at all.
    """
    from app.time_utils import _utcnow

    ctx: dict = {"pr_merged": True, "landed_on_integration": True}
    if satisfied is not None:
        ctx["code_acceptance_verdict"] = {
            "satisfied": satisfied,
            "proven": proven,
            "kind": kind,
        }
    task = Task(
        project_id=project_id,
        title="acceptance-landed-task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
        updated_at=_utcnow(),
    )
    db_session.add(task)
    await db_session.commit()
    return task


async def _seed_clean_streak(db_session, project, version, *, n: int = 6):
    """Seed ``n`` clean, high-confidence would-promote verdicts (ready streak)."""
    for i in range(n):
        await _seed_verdict(
            db_session,
            project.id,
            version.id,
            seq=i,
            would_promote=True,
            confidence=0.95,
        )


_RETIRED_READINESS_ACCEPTANCE_KEYS = {
    "acceptance_proven",
    "acceptance_satisfied_unproven",
    "acceptance_declared_unsatisfied",
    "acceptance_undeclared",
    "acceptance_merged_unverified_degraded",
    "acceptance_malformed",
    "acceptance_unproven_present",
    "acceptance_caveat",
    "acceptance_aggregate",
}


def _assert_retired_acceptance_keys_absent(out: dict):
    for key in _RETIRED_READINESS_ACCEPTANCE_KEYS:
        assert key not in out


async def _seed_promoted_verdict(
    db_session,
    project_id,
    version_id,
    *,
    seq: int,
    post_promotion_outcome: str | None,
):
    """Insert ONE promoted QualificationVerdict with a resolved (or NULL/unconfirmed)
    post-promotion serving outcome, for ground-truth precision tests."""
    from datetime import timedelta

    from app.time_utils import _utcnow

    row = QualificationVerdict(
        project_id=project_id,
        release_version_id=version_id,
        candidate_sha="cd" * 20,
        confidence=0.95,
        sub_scores={"audit": 1.0},
        blocker=None,
        would_promote=True,
        promotion_enabled=True,
        terminal_status="promoted",
        threshold=0.9,
        post_promotion_outcome=post_promotion_outcome,
        created_at=_utcnow() + timedelta(minutes=seq),
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _add_release_audit_task(
    db_session,
    project,
    *,
    version_id,
    candidate_sha,
    created_at=None,
    report=None,
    status=TaskStatus.COMPLETED,
):
    """Insert a release_audit Task scoped to a version (+ optional report)."""
    kwargs = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    task = Task(
        project_id=project.id,
        title="release audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        status=status,
        context={
            "release_version_id": str(version_id),
            "candidate_sha": candidate_sha,
        },
        **kwargs,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    if report is not None:
        db_session.add(
            Artifact(
                task_id=task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(report),
            )
        )
        await db_session.commit()
    return task


# ===========================================================================
# Per-project branch resolution — _lts_branch_names must use project columns
# ===========================================================================


async def _make_project_and_version_custom_branches(
    db_session,
    *,
    integration_branch: str,
    promotion_target: str,
    phase: str = "stabilize",
):
    """Create a project with non-default branch columns and an open version."""
    project = Project(
        name="custom-branch-project",
        user_id="cb-user",
        github_owner="acme",
        github_repo="custom",
        integration_branch=integration_branch,
        promotion_target=promotion_target,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from app import release_version as _rv

    version = await _rv.open_version(db_session, project.id, version_number=1)
    if phase != "build":
        _rv.set_version_phase(version, phase, reasoning="test setup")
        db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


# ---------------------------------------------------------------------------
# Per-attempt evidence mirroring onto QualificationVerdict (durable history).
#
# release_qualifier overwrites ReleaseVersion.last_qualification on every
# re-qualification, so the richer per-attempt evidence (migration-safety
# verdicts, findings, gate detail, promotion-failure context) was lost there.
# These tests assert that evidence is durably mirrored onto the immutable
# QualificationVerdict row and survives a subsequent last_qualification
# overwrite.
# ---------------------------------------------------------------------------


async def _load_only_verdict(db_session, version):
    """Return the single QualificationVerdict row for ``version`` (asserts one)."""
    rows = (
        (
            await db_session.execute(
                select(QualificationVerdict)
                .where(QualificationVerdict.release_version_id == version.id)
                .order_by(QualificationVerdict.created_at)
            )
        )
        .scalars()
        .all()
    )
    return rows


# --------------------------------------------------------------------------- #
# Audit-finding resolution ledger + re-qualification readiness signal
# (release_readiness.apply_finding_resolution / compute_requalification_readiness)
# --------------------------------------------------------------------------- #


def _carried_finding(severity: str, summary: str, file: str | None = None) -> dict:
    """Build a carried-finding dict shaped like _extract_audit_findings output.

    Stamps the same deterministic content ``key`` the real extractor attaches,
    so the readiness compute can join it against the ledger.
    """
    from app.release_scoring import _audit_finding_key

    item: dict = {"severity": severity, "summary": summary}
    if file:
        item["file"] = file
    item["key"] = _audit_finding_key(summary, file)
    return item


async def _set_last_qualification_findings(
    db_session, version, findings, *, truncated: bool = False
):
    """Attach carried audit findings to a version's last_qualification blob."""
    version.last_qualification = {
        "status": "pending",
        "audit_findings": findings,
        "audit_findings_truncated": truncated,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)


# ===========================================================================
# full-suite-drift-sentinel admin endpoint
# ===========================================================================


async def _make_project_with_owner(db_session, owner_id: str = "test-user-123"):
    """Create a project with the given user as owner (ProjectMember row included)."""
    from app.models import ProjectMember  # noqa: PLC0415

    project = Project(
        name="Sentinel-Test",
        user_id=owner_id,
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role="owner")
    db_session.add(member)
    await db_session.commit()
    return project


__all__ = [
    'annotations',
    'timezone',
    'asyncio',
    'contextlib',
    'json',
    'uuid',
    'AsyncMock',
    'MagicMock',
    'pytest',
    'sa_func',
    'select',
    'rq',
    'rs',
    'rv',
    'github_refs',
    'Alert',
    'Artifact',
    'ArtifactType',
    'Project',
    'ProjectMessage',
    'QualificationDimensionResult',
    'QualificationVerdict',
    'CompletionDebtEntry',
    'CriterionEvidence',
    'Task',
    'TaskStatus',
    'VersionAcceptanceCriterion',
    '_POLICY',
    '_shared_session_factory',
    '_point_policy_at',
    '_make_verified_finding',
    '_make_challenge_envelope',
    '_open_hotfix_backfill_debt',
    '_hotfix_backfill_row',
    '_make_project_and_version',
    '_branch_sha_mock',
    '_create_branch_mock',
    '_patch_promote_github_defaults',
    '_qualifier_ci_resolves_green',
    '_audit_tasks',
    '_promotion_failed_record',
    '_seed_qualification_children',
    '_seed_walkthrough_compatible_rubric',
    '_add_version_criterion',
    '_with_current_qualification_contract',
    '_LEDGER_SHA_A',
    '_LEDGER_SHA_B',
    '_seed_dimension_row',
    '_dimension_rows_by_dim',
    '_REUSE_PATH_PRE_GATES',
    '_live_rubric_projection',
    '_retract_default_authorship_criterion',
    '_delete_default_authorship_criterion',
    '_clean_finalize_pre_gates',
    '_seed_relaxed_ci_clean_qualification',
    '_seed_green_finalize_candidate',
    '_patch_finalize_ci_test_incidental_gates',
    '_seed_completed_challenge_without_artifact',
    '_attach_audit_challenge',
    '_versions_by_ref',
    '_CANDIDATE_SHA',
    '_seed_challenge_artifact',
    '_hotfix_changed_files_mock',
    '_patch_hotfix_promote_github',
    '_seed_infra_failure_children',
    '_infra_verdicts_for_version',
    '_make_task_with_context',
    '_seed_pending_qualification',
    '_seed_merge_grade_child',
    '_finalize_with_smoketest_logs',
    '_seed_pending_qualification_with_challenge',
    '_seed_redrive_common_dimensions',
    '_seed_audit_only_pending_qualification',
    '_messages_for',
    '_make_merged_task',
    '_ensure_verdict_table',
    '_verdict_rows',
    '_seed_verdict',
    '_seed_landed_acceptance_task',
    '_seed_clean_streak',
    '_RETIRED_READINESS_ACCEPTANCE_KEYS',
    '_assert_retired_acceptance_keys_absent',
    '_seed_promoted_verdict',
    '_add_release_audit_task',
    '_make_project_and_version_custom_branches',
    '_load_only_verdict',
    '_carried_finding',
    '_set_last_qualification_findings',
    '_make_project_with_owner',
]
