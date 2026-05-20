"""Release qualifier — the automain → main promotion gate.

Phase 3 of the automain LTS design (docs/plans/automain-lts-plan-v1.md).

The qualifier is an **async coordinator**, not a blocking call.  It scores an
``automain`` release candidate against ``main`` (the last known-good LTS branch)
and, when confident, promotes it.  Confidence is *differential* (everything is
measured as the ``main...automain`` diff) and *composite* (CI + audit +
test-erosion + smoketest).

Two-phase flow:

  run_qualification(session, project_id, version)
      Snapshot the candidate SHA, check CI (cheap, orchestrator-side), spawn a
      ``release_audit`` agent task and a qualify-time ``smoketest`` agent task,
      stamp ``version.last_qualification = {status: "pending", ...}`` and bump
      ``qualification_attempts``.  Returns immediately — does NOT block on the
      children.

  _finalize_qualification(session, version)
      Invoked later (by Phase 3 batch-3 completion handlers — not wired here)
      once BOTH the release_audit and qualify-smoketest children for this
      version have terminated.  Reads the RELEASE_AUDIT_REPORT and
      SMOKETEST_RESULT artifacts, computes the composite, and either promotes
      or sets the version back to ``stabilize``.

Hard blockers (CI red, smoketest startup-suite failure, any deleted test file)
force confidence to 0.0 — those candidates never promote regardless of the
other sub-scores.

Ships dark: actual promotion runs only when ``runtime_config["promotion_enabled"]``
is True.  Otherwise the composite is computed and logged (dry-run) but no PR is
opened.

Public API: ``run_qualification``, ``maybe_finalize_qualification``,
``_finalize_qualification``, ``compute_composite``, ``_load_policy``,
``promote``.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import github, release_version
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import PayloadTooLargeError, validate_internal_payload
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy — the trusted core.  weights + threshold come from a human-owned file.
# ---------------------------------------------------------------------------

# Hardcoded fallback used when promotion_policy.yaml is missing, unreadable, or
# malformed.  MUST match the documented defaults in promotion_policy.yaml so a
# parse failure degrades to the intended rubric rather than a silent surprise.
_DEFAULT_POLICY: dict[str, Any] = {
    "weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
    "threshold": 0.75,
}

# promotion_policy.yaml lives at backend/promotion_policy.yaml — two parents up
# from this file (backend/app/release_qualifier.py).
_POLICY_PATH = Path(__file__).resolve().parent.parent / "promotion_policy.yaml"


def _load_policy() -> dict[str, Any]:
    """Read backend/promotion_policy.yaml; fall back to hardcoded defaults.

    On ANY read or parse failure — missing file, unreadable, malformed YAML,
    missing/non-numeric keys, PyYAML import failure — log a warning and return
    a copy of ``_DEFAULT_POLICY``.  Never raises: a broken policy file must not
    take down the qualifier (it would only fail-open the gate; the caller still
    decides whether to act on the composite).

    The returned dict always has the shape
    ``{"weights": {"audit": float, "test_erosion": float, "smoketest": float},
       "threshold": float}`` with every value coerced to float.
    """
    try:
        import yaml

        raw_text = _POLICY_PATH.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError(
                f"promotion_policy.yaml did not parse to a mapping: {type(parsed)}"
            )

        weights_raw = parsed.get("weights")
        if not isinstance(weights_raw, dict):
            raise ValueError("promotion_policy.yaml missing 'weights' mapping")

        # Coerce every weight + threshold to float; KeyError/TypeError/ValueError
        # here drop us into the fallback branch.
        weights = {
            "audit": float(weights_raw["audit"]),
            "test_erosion": float(weights_raw["test_erosion"]),
            "smoketest": float(weights_raw["smoketest"]),
        }
        threshold = float(parsed["threshold"])
        return {"weights": weights, "threshold": threshold}
    except Exception as exc:  # noqa: BLE001 — fall back on any failure, never raise
        logger.warning(
            "[RELEASE_QUALIFIER] failed to load promotion_policy.yaml (%s); "
            "falling back to hardcoded defaults",
            exc,
        )
        # Return a deep-ish copy so callers can't mutate the module constant.
        return {
            "weights": dict(_DEFAULT_POLICY["weights"]),
            "threshold": _DEFAULT_POLICY["threshold"],
        }


def _clamp01(value: float) -> float:
    """Clamp a numeric sub-score into [0.0, 1.0]."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


# ---------------------------------------------------------------------------
# Composite — pure scoring function.
# ---------------------------------------------------------------------------


def compute_composite(
    ci_green: bool,
    audit_score: float,
    test_erosion: float,
    smoketest_score: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Compute the composite promotion confidence for a release candidate.

    Hard blockers — each forces ``confidence = 0.0`` and ``promote = False``:
      * ``not ci_green``           — CI is not green on the candidate SHA.
      * ``smoketest_score`` is None — the smoketest never produced a usable
        score (treated as a startup-suite failure: caller passes None for that).
      * ``audit_score`` / ``test_erosion`` of None — a deleted test file (the
        caller signals an irrecoverable test-erosion blocker by passing None
        for ``test_erosion``).

    The three numeric inputs are clamped to [0,1] before weighting.  When no
    hard blocker fires, ``confidence`` is the policy-weighted sum of the three
    normalized sub-scores.  ``promote`` is True iff ``confidence >= threshold``.

    Returns ``{confidence, promote, sub_scores, blocker}`` where ``blocker`` is
    a short human-readable string when a hard blocker fired, else None.
    """
    weights = policy.get("weights") or _DEFAULT_POLICY["weights"]
    threshold = float(policy.get("threshold", _DEFAULT_POLICY["threshold"]))
    w_audit = float(weights.get("audit", 0.40))
    w_test = float(weights.get("test_erosion", 0.30))
    w_smoke = float(weights.get("smoketest", 0.30))

    # Hard blockers — checked before any weighting.  ``None`` is the sentinel
    # for "this dimension hit an irrecoverable blocker"; the caller passes None
    # for test_erosion on a deleted test file and None for smoketest on a
    # startup-suite failure.
    blocker: str | None = None
    if not ci_green:
        blocker = "ci_not_green"
    elif smoketest_score is None:
        blocker = "smoketest_startup_failed"
    elif test_erosion is None:
        blocker = "test_file_deleted"
    elif audit_score is None:
        # An audit that produced no usable score is a soft signal, but without
        # it the composite is meaningless — treat as a blocker so a missing
        # audit can never promote on smoketest + test_erosion alone.
        blocker = "audit_missing"

    # Sub-scores are always reported (clamped) for observability, even when a
    # blocker fired and the composite is forced to zero.
    sub_scores = {
        "audit": _clamp01(audit_score) if audit_score is not None else 0.0,
        "test_erosion": _clamp01(test_erosion) if test_erosion is not None else 0.0,
        "smoketest": _clamp01(smoketest_score) if smoketest_score is not None else 0.0,
    }

    if blocker is not None:
        return {
            "confidence": 0.0,
            "promote": False,
            "sub_scores": sub_scores,
            "blocker": blocker,
        }

    confidence = (
        w_audit * sub_scores["audit"]
        + w_test * sub_scores["test_erosion"]
        + w_smoke * sub_scores["smoketest"]
    )
    confidence = round(_clamp01(confidence), 4)
    return {
        "confidence": confidence,
        "promote": confidence >= threshold,
        "sub_scores": sub_scores,
        "blocker": None,
    }


# ---------------------------------------------------------------------------
# GitHub token resolution — project-level (the qualifier is not task-bound).
# ---------------------------------------------------------------------------


async def _resolve_github_with_session(
    session: AsyncSession,
    project: Project,
) -> tuple[str, str, str] | None:
    """Resolve ``(token, owner, repo)`` using the active session for fallbacks.

    Same resolution order as ``get_github_token_for_task``: App-level discovery,
    then the user's stored installation id, then their PAT.  The session is
    needed only for the latter two (they read ``UserIntegration``).
    """
    owner = project.github_owner
    repo = project.github_repo
    if not owner or not repo:
        logger.warning(
            "[RELEASE_QUALIFIER] project %s has no authorized GitHub repo",
            project.id,
        )
        return None

    token: str | None = None
    try:
        token = await github.get_repo_installation_token(owner, repo)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RELEASE_QUALIFIER] App-level token discovery failed for %s/%s",
            owner,
            repo,
        )

    if not token and project.user_id:
        from app.github import generate_installation_access_token
        from app.models import UserIntegration
        from app.secrets_manager import get_user_secret

        integration = (
            await session.execute(
                select(UserIntegration).where(
                    UserIntegration.user_id == project.user_id
                )
            )
        ).scalars().first()
        if integration and integration.github_installation_id:
            try:
                token = await generate_installation_access_token(
                    integration.github_installation_id, repositories=[repo]
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[RELEASE_QUALIFIER] stored installation token failed for "
                    "%s/%s",
                    owner,
                    repo,
                )
        if not token and integration and integration.github_pat:
            try:
                token = await get_user_secret(integration.github_pat)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[RELEASE_QUALIFIER] PAT retrieval failed for %s/%s",
                    owner,
                    repo,
                )

    if not token:
        logger.warning(
            "[RELEASE_QUALIFIER] no GitHub token available for %s/%s",
            owner,
            repo,
        )
        return None
    return token, owner, repo


# ---------------------------------------------------------------------------
# run_qualification — the coordinator (phase 1: spawn).
# ---------------------------------------------------------------------------


async def run_qualification(
    session: AsyncSession,
    project_id: uuid.UUID,
    version: release_version.ReleaseVersion,
) -> dict[str, Any]:
    """Kick off qualification for the open release ``version``.

    Steps (none of which block on the spawned children):
      1. Resolve the project's owner/repo/GitHub token.
      2. Snapshot the candidate SHA: ``automain`` HEAD.
      3. Check CI on that SHA via ``get_check_runs_cached`` (cheap, orchestrator
         side).  ``ci_green`` is recorded but not acted on here — the composite
         in ``_finalize_qualification`` applies it as a hard blocker.
      4. Spawn a ``release_audit`` task (clones ``main``, audits the
         ``main...automain`` diff against main's standards) and a qualify-time
         ``smoketest`` task (against ``automain``).  Both carry
         ``context["release_version_id"]``; the smoketest also carries
         ``context["qualifier_smoketest"] = True`` so its failure does not
         spawn a fix task.
      5. Stamp ``version.last_qualification = {status: "pending", ...}`` and
         bump ``version.qualification_attempts``.

    Returns a status dict.  ``status`` is ``"spawned"`` on success or an error
    code (``"no_credentials"``, ``"sha_error"``, ``"spawn_failed"``) otherwise.
    The caller commits the session.
    """
    project = await session.get(Project, project_id)
    if project is None:
        logger.warning(
            "[RELEASE_QUALIFIER] run_qualification: project %s not found",
            project_id,
        )
        return {"status": "project_not_found"}

    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        version.last_qualification = {
            "status": "error",
            "error": "no_credentials",
            "started_at": _utcnow().isoformat(),
        }
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
        flag_modified(version, "last_qualification")
        session.add(version)
        return {"status": "no_credentials"}
    token, owner, repo = resolved

    # 2. Snapshot the candidate SHA — automain HEAD.
    try:
        candidate_sha = await github.get_branch_sha(token, owner, repo, "automain")
    except Exception as exc:  # noqa: BLE001 — a missing branch / API error is recoverable
        logger.warning(
            "[RELEASE_QUALIFIER] could not snapshot automain SHA for %s/%s: %s",
            owner,
            repo,
            exc,
        )
        version.last_qualification = {
            "status": "error",
            "error": "sha_error",
            "detail": str(exc)[:300],
            "started_at": _utcnow().isoformat(),
        }
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
        flag_modified(version, "last_qualification")
        session.add(version)
        return {"status": "sha_error"}

    # 3. CI check on the candidate SHA (cheap, orchestrator-side).
    ci_green = False
    try:
        ci_result = await github.get_check_runs_cached(
            token, owner, repo, candidate_sha, project_id=str(project_id)
        )
        ci_green = ci_result.get("conclusion") == "success"
    except Exception as exc:  # noqa: BLE001 — CI fetch failure ⇒ treat as not-green
        logger.warning(
            "[RELEASE_QUALIFIER] CI check failed for %s@%s: %s",
            repo,
            candidate_sha[:8],
            exc,
        )

    # 4. Spawn the two heavy children.
    started_at = _utcnow().isoformat()
    # The LTS branch is always "main" (deploy.yml deploys from main) — NOT
    # integration_branch, which is the branch the pipeline builds ON (automain
    # post-Phase-1). The qualifier audits the main...automain diff and
    # promotes automain → main.
    diff_range = "main...automain"
    audit_task_id: str | None = None
    smoketest_task_id: str | None = None
    try:
        audit_task = await _spawn_release_audit_task(
            session,
            project_id,
            version_id=version.id,
            base_branch="main",
            diff_range=diff_range,
            candidate_sha=candidate_sha,
        )
        audit_task_id = str(audit_task.id)
        smoketest_task = await _spawn_qualifier_smoketest_task(
            session,
            project_id,
            version_id=version.id,
            candidate_sha=candidate_sha,
        )
        smoketest_task_id = str(smoketest_task.id)
    except Exception as exc:  # noqa: BLE001 — spawn failure must not raise out
        logger.warning(
            "[RELEASE_QUALIFIER] failed to spawn qualification children for "
            "version v%d: %s",
            version.version_number,
            exc,
            exc_info=True,
        )
        version.last_qualification = {
            "status": "error",
            "error": "spawn_failed",
            "detail": str(exc)[:300],
            "candidate_sha": candidate_sha,
            "ci_green": ci_green,
            "started_at": started_at,
        }
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
        flag_modified(version, "last_qualification")
        session.add(version)
        return {"status": "spawn_failed"}

    # 5. Stamp the pending qualification record.
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "started_at": started_at,
        "audit_task_id": audit_task_id,
        "smoketest_task_id": smoketest_task_id,
    }
    version.qualification_attempts = (version.qualification_attempts or 0) + 1
    flag_modified(version, "last_qualification")
    session.add(version)

    logger.info(
        "[RELEASE_QUALIFIER] qualification started for v%d project=%s "
        "candidate=%s ci_green=%s audit_task=%s smoketest_task=%s",
        version.version_number,
        project_id,
        candidate_sha[:8],
        ci_green,
        audit_task_id,
        smoketest_task_id,
    )
    return {
        "status": "spawned",
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "audit_task_id": audit_task_id,
        "smoketest_task_id": smoketest_task_id,
    }


async def _spawn_release_audit_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    base_branch: str,
    diff_range: str,
    candidate_sha: str,
) -> Task:
    """Create + queue a ``release_audit`` task for the qualifier.

    The audit clones ``base_branch`` (``main``) and reviews the ``diff_range``
    (``main...automain``) against main's standards — the differential audit at
    the heart of the qualifier (design doc §6 gotcha 2: it MUST run on main).
    Mirrors the ``_spawn_self_audit_task`` spawn pattern.
    """
    payload: dict[str, Any] = {
        "type": "release_audit",
        "base_branch": base_branch,
        "diff_range": diff_range,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get(
        "release_audit"
    )
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        "release_audit"
    )
    if type_effort:
        payload["effort"] = type_effort
    try:
        payload = validate_internal_payload(payload, "release_qualifier:release_audit")
    except PayloadTooLargeError:
        # release_audit payloads are tiny; a too-large error here is a real bug.
        raise

    task = Task(
        project_id=project_id,
        title="Release audit: main...automain diff",
        description=(
            f"Differential audit of {diff_range} against base_branch "
            f"standards for the release qualifier"
        ),
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "release_version_id": str(version_id),
            "candidate_sha": candidate_sha,
        },
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "RELEASE_QUALIFIER",
                "task_type": "release_audit",
                "diff_range": diff_range,
                "release_version_id": str(version_id),
            },
        )
    )
    return task


async def _spawn_qualifier_smoketest_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    candidate_sha: str,
) -> Task:
    """Create + queue a qualify-time ``smoketest`` task against ``automain``.

    The context flag ``qualifier_smoketest=True`` marks this as a
    pre-promotion qualifier input — distinct from the post-promotion
    production smoketest — so its failure does NOT spawn a fix task
    (design doc §6 gotcha 5).
    """
    payload: dict[str, Any] = {
        "type": "smoketest",
        "base_branch": "automain",
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get("smoketest")
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        "smoketest"
    )
    if type_effort:
        payload["effort"] = type_effort
    health_check_urls = _runtime_config.get("health_check_urls") or []
    if health_check_urls:
        payload["health_check_urls"] = health_check_urls
    try:
        payload = validate_internal_payload(
            payload, "release_qualifier:smoketest"
        )
    except PayloadTooLargeError:
        raise

    task = Task(
        project_id=project_id,
        title="Qualifier smoketest: automain candidate",
        description="Qualify-time smoketest of the automain release candidate",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "release_version_id": str(version_id),
            "qualifier_smoketest": True,
            "candidate_sha": candidate_sha,
        },
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "RELEASE_QUALIFIER",
                "task_type": "smoketest",
                "qualifier_smoketest": True,
                "release_version_id": str(version_id),
            },
        )
    )
    return task


# ---------------------------------------------------------------------------
# Artifact parsing — read the children's reports.
# ---------------------------------------------------------------------------


async def _latest_artifact_logs(
    session: AsyncSession,
    task_id: uuid.UUID,
    artifact_type: ArtifactType,
) -> dict[str, Any] | None:
    """Return the parsed JSON ``logs`` of the latest artifact of a type.

    Both RELEASE_AUDIT_REPORT and SMOKETEST_RESULT store a JSON document in
    the ``Artifact.logs`` column.  Returns None when no artifact exists or the
    logs are not parseable JSON.
    """
    import json

    artifact = (
        await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id == task_id,
                Artifact.artifact_type == artifact_type,
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if artifact is None or not artifact.logs:
        return None
    try:
        parsed = json.loads(artifact.logs)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning(
            "[RELEASE_QUALIFIER] artifact %s for task %s has unparseable logs",
            artifact_type,
            task_id,
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def _score_audit(report: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Derive ``(audit_score, test_erosion_score)`` from a RELEASE_AUDIT_REPORT.

    The agent emits ``{audit_score, test_erosion: {deleted_test_files,
    assertion_delta, test_count_delta}, findings}`` (agent/src/handlers/
    specialized.ts).

    Returns ``audit_score`` clamped to [0,1] (None when absent/unusable) and a
    ``test_erosion_score`` in [0,1] where 1.0 == no erosion.  ``test_erosion``
    is None — the hard-blocker sentinel — when the diff deleted a test file.
    A net reduction in tests or assertions applies a graded penalty.
    """
    if not isinstance(report, dict):
        return None, None

    raw_audit = report.get("audit_score")
    audit_score: float | None = (
        _clamp01(raw_audit)
        if isinstance(raw_audit, (int, float)) and not isinstance(raw_audit, bool)
        else None
    )

    erosion = report.get("test_erosion")
    if not isinstance(erosion, dict):
        # No test-erosion block — assume neutral (no erosion detected).
        return audit_score, 1.0

    deleted = erosion.get("deleted_test_files")
    if isinstance(deleted, list) and len(deleted) > 0:
        # Hard blocker — any deleted test file forces a non-promote.
        return audit_score, None

    # Graded penalty for net assertion / test-count reduction.  Each negative
    # delta knocks the score down; positive deltas leave it at the ceiling.
    score = 1.0
    test_delta = erosion.get("test_count_delta")
    if isinstance(test_delta, (int, float)) and test_delta < 0:
        # 0.10 penalty per net-removed test, capped so it can't go below 0.
        score -= min(0.5, abs(float(test_delta)) * 0.10)
    assertion_delta = erosion.get("assertion_delta")
    if isinstance(assertion_delta, (int, float)) and assertion_delta < 0:
        # 0.02 penalty per net-removed assertion, capped.
        score -= min(0.3, abs(float(assertion_delta)) * 0.02)
    return audit_score, _clamp01(score)


def _score_smoketest(result: dict[str, Any] | None) -> float | None:
    """Derive a ``smoketest_score`` in [0,1] from a SMOKETEST_RESULT.

    The smoketest emits ``{passed, tests: [{status}], suites: {startup:
    {passed}, ...}}`` (agent/src + routers/agent_status.py).

    Returns the pass ratio (passed tests / total tests).  Returns None — the
    hard-blocker sentinel — when the startup suite failed or no result is
    available, since a failed startup suite means the candidate can't even
    boot.
    """
    if not isinstance(result, dict):
        return None

    suites = result.get("suites")
    if isinstance(suites, dict):
        startup = suites.get("startup")
        if isinstance(startup, dict) and startup.get("passed") is False:
            # Startup suite failed — hard blocker.
            return None

    tests = result.get("tests")
    if not isinstance(tests, list) or not tests:
        # No tests ran: if the overall result claims passed, treat as a full
        # score; otherwise as zero (but not a hard blocker — startup is the
        # blocker, handled above).
        return 1.0 if result.get("passed") is True else 0.0

    total = len(tests)
    passed = sum(
        1 for t in tests if isinstance(t, dict) and t.get("status") == "pass"
    )
    return _clamp01(passed / total) if total else 0.0


# ---------------------------------------------------------------------------
# _finalize_qualification — the coordinator (phase 2: collect + decide).
# ---------------------------------------------------------------------------


async def _finalize_qualification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> dict[str, Any]:
    """Finalize qualification once both qualifier children have terminated.

    Invoked by the Phase 3 batch-3 completion handlers (not wired here) after
    BOTH the release_audit and qualify-smoketest children for ``version`` have
    reached a terminal state.

    Reads the RELEASE_AUDIT_REPORT and SMOKETEST_RESULT artifacts, computes the
    composite, then:
      * promote == True  and promotion_enabled ⇒ ``promote()``.
      * promote == True  but promotion_enabled is False ⇒ dry-run: log the
        composite, record it, leave the version open.
      * promote == False ⇒ record the blocker, keep the version open, set
        ``phase = "stabilize"``.

    The caller commits the session.  Returns a result dict.
    """
    qual = dict(version.last_qualification or {})
    candidate_sha = qual.get("candidate_sha")
    ci_green = bool(qual.get("ci_green"))
    audit_task_id = qual.get("audit_task_id")
    smoketest_task_id = qual.get("smoketest_task_id")

    policy = _load_policy()

    # --- Read the release_audit report -------------------------------------
    audit_report: dict[str, Any] | None = None
    if audit_task_id:
        try:
            audit_report = await _latest_artifact_logs(
                session,
                uuid.UUID(str(audit_task_id)),
                ArtifactType.RELEASE_AUDIT_REPORT,
            )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid audit_task_id %s on version v%d",
                audit_task_id,
                version.version_number,
            )
    audit_score, test_erosion_score = _score_audit(audit_report)

    # --- Read the smoketest result -----------------------------------------
    smoketest_result: dict[str, Any] | None = None
    if smoketest_task_id:
        try:
            smoketest_result = await _latest_artifact_logs(
                session,
                uuid.UUID(str(smoketest_task_id)),
                ArtifactType.SMOKETEST_RESULT,
            )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid smoketest_task_id %s on version v%d",
                smoketest_task_id,
                version.version_number,
            )
    smoketest_score = _score_smoketest(smoketest_result)

    # --- Compute the composite ---------------------------------------------
    composite = compute_composite(
        ci_green=ci_green,
        audit_score=audit_score,
        test_erosion=test_erosion_score,
        smoketest_score=smoketest_score,
        policy=policy,
    )

    finalized_at = _utcnow().isoformat()
    base_record: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "confidence": composite["confidence"],
        "sub_scores": composite["sub_scores"],
        "blocker": composite["blocker"],
        "threshold": policy.get("threshold"),
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": audit_task_id,
        "smoketest_task_id": smoketest_task_id,
    }

    promotion_enabled = bool(_runtime_config.get("promotion_enabled"))

    logger.info(
        "[RELEASE_QUALIFIER] v%d composite=%.4f promote=%s blocker=%s "
        "sub_scores=%s promotion_enabled=%s",
        version.version_number,
        composite["confidence"],
        composite["promote"],
        composite["blocker"],
        composite["sub_scores"],
        promotion_enabled,
    )

    # --- Decide -------------------------------------------------------------
    if not composite["promote"]:
        # Below threshold (or a hard blocker fired): keep the version open and
        # send it back to stabilize so the next strategic cycle targets the
        # blocker.  A failed qualifier only ever blocks — which is safe.
        record = {**base_record, "status": "not_promoted"}
        version.last_qualification = record
        flag_modified(version, "last_qualification")
        reason = (
            f"qualification v{version.version_number}: "
            f"confidence {composite['confidence']:.2f} < "
            f"{policy.get('threshold')}"
        )
        if composite["blocker"]:
            reason = (
                f"qualification v{version.version_number} blocked: "
                f"{composite['blocker']}"
            )
        release_version.set_version_phase(version, "stabilize", reasoning=reason)
        session.add(version)
        return {
            "status": "not_promoted",
            "confidence": composite["confidence"],
            "blocker": composite["blocker"],
        }

    if not promotion_enabled:
        # Dry-run: the candidate would promote, but Phase 3 ships dark.  Record
        # the composite and leave the version open — interim-manual promotion
        # continues elsewhere.
        record = {**base_record, "status": "dry_run", "would_promote": True}
        version.last_qualification = record
        flag_modified(version, "last_qualification")
        session.add(version)
        logger.info(
            "[RELEASE_QUALIFIER] v%d would promote (confidence=%.4f) but "
            "promotion_enabled=False — dry-run, not promoting",
            version.version_number,
            composite["confidence"],
        )
        return {
            "status": "dry_run",
            "confidence": composite["confidence"],
            "would_promote": True,
        }

    # --- Promote ------------------------------------------------------------
    if not candidate_sha:
        # Should not happen — run_qualification always records it — but guard
        # so a missing SHA degrades to stabilize rather than a crash.
        record = {**base_record, "status": "error", "error": "missing_candidate_sha"}
        version.last_qualification = record
        flag_modified(version, "last_qualification")
        release_version.set_version_phase(
            version,
            "stabilize",
            reasoning=f"qualification v{version.version_number}: missing SHA",
        )
        session.add(version)
        return {"status": "error", "error": "missing_candidate_sha"}

    return await promote(
        session,
        version,
        candidate_sha,
        composite=composite,
        base_record=base_record,
    )


# ---------------------------------------------------------------------------
# maybe_finalize_qualification — task-completion hook (coordination glue).
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def maybe_finalize_qualification(
    session: AsyncSession,
    completed_task: Task,
) -> dict[str, Any] | None:
    """Called from a task-completion handler whenever ANY task terminates.

    This is the coordination glue between ``run_qualification`` (which spawns
    the two qualifier children and returns immediately) and
    ``_finalize_qualification`` (which computes the composite and decides).

    Fast path: if ``completed_task.context`` has no ``release_version_id``,
    return ``None`` immediately.  The overwhelming majority of tasks are not
    qualifier children and this must be a cheap no-op.

    When a ``release_version_id`` is present:

    1. Load the ``ReleaseVersion`` with ``SELECT ... FOR UPDATE``.  This lock
       is the critical race guard: the ``release_audit`` and qualifier
       ``smoketest`` children can terminate near-simultaneously, and without
       it two concurrent completion handlers could both finalize → a double
       promotion.

    2. If the version is None, or
       ``version.last_qualification.get("status") != "pending"``, return
       ``None``.  The version was already finalized (or was never mid-
       qualification).  The lock + this guard make a second call a safe no-op
       (idempotent).

    3. Read ``audit_task_id`` and ``smoketest_task_id`` from
       ``version.last_qualification``.  Load both ``Task`` rows.  If either
       is missing or has not yet reached a terminal state (``COMPLETED``,
       ``FAILED``, ``CANCELLED``), return ``None`` — the sibling child is
       still running.

    4. When BOTH children are terminal, delegate to
       ``_finalize_qualification(session, version)`` and return its result.

    The caller is responsible for committing the session.

    Returns ``None`` when this task is not a qualifier child, when the
    sibling is still running, or when the version is already finalized.
    Returns the ``_finalize_qualification`` result dict when finalization runs.
    """
    context = completed_task.context or {}
    version_id_raw = context.get("release_version_id")
    if not version_id_raw:
        # Fast path — not a qualifier child.  Must be O(1).
        return None

    # Parse the version id.
    try:
        version_id = uuid.UUID(str(version_id_raw))
    except (ValueError, AttributeError):
        logger.warning(
            "[RELEASE_QUALIFIER] maybe_finalize_qualification: invalid "
            "release_version_id %r on task %s — skipping",
            version_id_raw,
            completed_task.id,
        )
        return None

    # 1. Lock the version row to prevent a double-finalize race.
    version: release_version.ReleaseVersion | None = (
        await session.execute(
            select(release_version.ReleaseVersion)
            .where(release_version.ReleaseVersion.id == version_id)
            .with_for_update()
        )
    ).scalars().first()

    # 2. Guard: already finalized or never mid-qualification.
    if version is None:
        logger.debug(
            "[RELEASE_QUALIFIER] maybe_finalize_qualification: version %s "
            "not found — skipping",
            version_id,
        )
        return None

    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        # Either not yet started, or already finalized — idempotent no-op.
        return None

    # 3. Check that both children are terminal.
    audit_task_id_raw = qual.get("audit_task_id")
    smoketest_task_id_raw = qual.get("smoketest_task_id")

    if not audit_task_id_raw or not smoketest_task_id_raw:
        logger.warning(
            "[RELEASE_QUALIFIER] maybe_finalize_qualification: version v%d "
            "has pending qualification but missing child task ids — skipping",
            version.version_number,
        )
        return None

    try:
        audit_task_id = uuid.UUID(str(audit_task_id_raw))
        smoketest_task_id = uuid.UUID(str(smoketest_task_id_raw))
    except (ValueError, AttributeError):
        logger.warning(
            "[RELEASE_QUALIFIER] maybe_finalize_qualification: version v%d "
            "has unparseable child task ids (%r, %r) — skipping",
            version.version_number,
            audit_task_id_raw,
            smoketest_task_id_raw,
        )
        return None

    audit_task: Task | None = await session.get(Task, audit_task_id)
    smoketest_task: Task | None = await session.get(Task, smoketest_task_id)

    if audit_task is None or smoketest_task is None:
        logger.debug(
            "[RELEASE_QUALIFIER] maybe_finalize_qualification: version v%d "
            "child tasks not yet persisted — skipping",
            version.version_number,
        )
        return None

    if (
        audit_task.status not in _TERMINAL_STATUSES
        or smoketest_task.status not in _TERMINAL_STATUSES
    ):
        # Sibling is still running — not time to finalize yet.
        return None

    # 4. Both children are terminal — finalize.
    logger.info(
        "[RELEASE_QUALIFIER] maybe_finalize_qualification: both children "
        "terminal for version v%d (audit=%s/%s smoke=%s/%s) — finalizing",
        version.version_number,
        audit_task.id,
        audit_task.status,
        smoketest_task.id,
        smoketest_task.status,
    )
    return await _finalize_qualification(session, version)


# ---------------------------------------------------------------------------
# promote — open + merge the automain → main PR.
# ---------------------------------------------------------------------------


async def promote(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
    *,
    composite: dict[str, Any] | None = None,
    base_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Promote the ``automain`` candidate to ``main``.

    Opens a PR ``automain → main`` and merges it with ``merge_method="merge"``
    (preserves integration history; branch protection on ``main`` runs CI as
    the final gate).

    CRITICAL: ``github.merge_pull_request`` does NOT enforce CI and does not
    raise on a 405/409 — it returns ``{"merged": False}``.  This function
    checks ``result.get("merged")`` and treats a non-merged result as a FAILED
    promotion: it does NOT retry blindly.

    On success: ``version.status = "promoted"``, stamps ``promoted_at`` /
    ``promoted_sha``, opens the next version (``build`` phase) via
    ``release_version.open_version``.

    On failure / non-merge: records the outcome in ``version.last_qualification``,
    keeps the version open, and sets ``phase = "stabilize"``.

    The caller commits the session.  Returns a result dict.
    """
    base_record = dict(base_record or {})
    project = await session.get(Project, version.project_id)
    if project is None:
        logger.warning(
            "[RELEASE_QUALIFIER] promote: project %s not found",
            version.project_id,
        )
        return {"status": "error", "error": "project_not_found"}

    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        return _record_promotion_failure(
            session, version, base_record, "no_credentials"
        )
    token, owner, repo = resolved

    # 1. Open the automain → main PR.
    try:
        pr = await github.create_pull_request(
            token,
            owner,
            repo,
            title=f"Release v{version.version_number}: promote automain → main",
            head="automain",
            base="main",
            body=(
                f"Automated promotion of release v{version.version_number}.\n\n"
                f"Candidate SHA: `{candidate_sha}`\n"
                f"Composite confidence: "
                f"{(composite or {}).get('confidence', 'n/a')}\n\n"
                f"Opened by the release qualifier "
                f"(backend/app/release_qualifier.py)."
            ),
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a PR-creation failure blocks, never raises
        logger.warning(
            "[RELEASE_QUALIFIER] failed to open promotion PR for v%d: %s",
            version.version_number,
            exc,
        )
        return _record_promotion_failure(
            session,
            version,
            base_record,
            "pr_creation_failed",
            detail=str(exc)[:300],
        )

    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")

    # An empty diff (automain == main) is a degenerate but valid outcome:
    # create_pull_request returns {"empty_diff": True, "merged": True}.  Nothing
    # to promote — treat as a no-op success and still advance the version.
    if pr.get("empty_diff"):
        logger.info(
            "[RELEASE_QUALIFIER] v%d promotion: automain == main (empty diff)",
            version.version_number,
        )
        return await _record_promotion_success(
            session, version, candidate_sha, base_record, pr_url, empty_diff=True
        )

    if not pr_number:
        return _record_promotion_failure(
            session, version, base_record, "pr_missing_number"
        )

    # 2. Merge the PR — merge_method="merge" preserves integration history.
    #    merge_pull_request does NOT raise on 405/409; it returns merged=False.
    try:
        merge_result = await github.merge_pull_request(
            token,
            owner,
            repo,
            pr_number,
            merge_method="merge",
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a 5xx raises HTTPException; treat as failure
        logger.warning(
            "[RELEASE_QUALIFIER] merge raised for promotion PR #%s (v%d): %s",
            pr_number,
            version.version_number,
            exc,
        )
        return _record_promotion_failure(
            session,
            version,
            base_record,
            "merge_error",
            detail=str(exc)[:300],
            pr_url=pr_url,
        )

    if not merge_result.get("merged"):
        # Non-merged result (405 = checks/reviews block, 409 = conflict).  This
        # is a FAILED promotion — do NOT retry blindly.  Branch protection is
        # the real gate; the version goes back to stabilize.
        msg = merge_result.get("message", "merge not completed")
        logger.warning(
            "[RELEASE_QUALIFIER] promotion PR #%s for v%d did not merge: %s",
            pr_number,
            version.version_number,
            msg,
        )
        return _record_promotion_failure(
            session,
            version,
            base_record,
            "merge_blocked",
            detail=str(msg)[:300],
            pr_url=pr_url,
            conflict=bool(merge_result.get("conflict")),
        )

    # 3. Success — promote the version and open the next one.
    logger.info(
        "[RELEASE_QUALIFIER] v%d promoted: automain → main merged via PR #%s",
        version.version_number,
        pr_number,
    )
    return await _record_promotion_success(
        session, version, candidate_sha, base_record, pr_url
    )


def _record_promotion_failure(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    base_record: dict[str, Any],
    error: str,
    *,
    detail: str | None = None,
    pr_url: str | None = None,
    conflict: bool = False,
) -> dict[str, Any]:
    """Record a failed promotion: keep the version open, set phase=stabilize.

    A failed promotion only ever *blocks* — the version stays open and goes
    back to stabilize so the next strategic cycle works the blocker.  ``main``
    is untouched, so this is always safe.
    """
    record = {
        **base_record,
        "status": "promotion_failed",
        "error": error,
    }
    if detail:
        record["detail"] = detail
    if pr_url:
        record["pr_url"] = pr_url
    if conflict:
        record["conflict"] = True
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"promotion v{version.version_number} failed ({error}); "
            f"returning to stabilize"
        ),
    )
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] promotion of v%d failed (%s) — version stays open, "
        "phase=stabilize",
        version.version_number,
        error,
    )
    return {"status": "promotion_failed", "error": error}


async def _record_promotion_success(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
    base_record: dict[str, Any],
    pr_url: str,
    *,
    empty_diff: bool = False,
) -> dict[str, Any]:
    """Record a successful promotion and open the next version.

    Marks ``version`` ``status="promoted"``, stamps ``promoted_at`` /
    ``promoted_sha``, then opens version N+1 in the ``build`` phase with its
    ``base_sha`` set to the just-promoted SHA.
    """
    now = _utcnow()
    record = {
        **base_record,
        "status": "promoted",
        "promoted_sha": candidate_sha,
        "pr_url": pr_url,
    }
    if empty_diff:
        record["empty_diff"] = True
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    version.status = "promoted"
    version.promoted_at = now
    version.promoted_sha = candidate_sha
    session.add(version)

    # Open the next version (build phase) — base_sha is the freshly-promoted SHA.
    next_version = await release_version.open_version(
        session,
        version.project_id,
        version_number=version.version_number + 1,
        base_sha=candidate_sha,
    )
    logger.info(
        "[RELEASE_QUALIFIER] v%d promoted (sha=%s); opened v%d",
        version.version_number,
        candidate_sha[:8],
        next_version.version_number,
    )
    return {
        "status": "promoted",
        "promoted_sha": candidate_sha,
        "next_version_number": next_version.version_number,
    }
