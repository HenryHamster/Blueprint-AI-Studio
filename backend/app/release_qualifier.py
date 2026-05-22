"""Release qualifier — the automain → main promotion gate.

Phase 3 of the automain LTS design (docs/plans/automain-lts-plan-v1.md).

The qualifier is an **async coordinator**, not a blocking call.  It scores an
``automain`` release candidate against ``main`` (the last known-good LTS branch)
and, when confident, promotes it.  Confidence is *differential* (everything is
measured as the ``main...automain`` diff) and *composite* (CI + audit +
test-erosion + smoketest).

Two-phase flow:

  run_qualification(session, project_id, version)
      Snapshot the candidate SHA, check CI (cheap, orchestrator-side), lock the
      ReleaseVersion row, spawn a ``release_audit`` agent task and — when the
      smoketest subsystem is enabled — a qualify-time ``smoketest`` agent task,
      stamp ``version.last_qualification = {status: "pending", ...}`` and bump
      ``qualification_attempts``.  Both child payloads carry the exact
      ``candidate_sha`` so the children check out the precise snapshot commit.
      Returns immediately — does NOT block on the children.

  _finalize_qualification(session, version)
      Invoked later (by Phase 3 batch-3 completion handlers — not wired here)
      once the release_audit child (and the qualify-smoketest child, when the
      subsystem was enabled) for this version have terminated.  Reads the
      RELEASE_AUDIT_REPORT and SMOKETEST_RESULT artifacts, computes the
      composite, and either promotes or sets the version back to ``stabilize``.

  promote(session, version, candidate_sha, ...)
      Creates a release branch pinned to ``candidate_sha``, opens a PR from it
      into ``main`` and merges it.  Promotes the EXACT qualified commit, so
      ``automain`` advancing during qualification is harmless.

Session ownership (the merge→main transaction boundary).  ``promote()`` merges
to production ``main``; it MUST never commit a caller's unrelated transaction.
``maybe_finalize_qualification`` therefore opens its OWN dedicated session (via
``AsyncSessionLocal``) for the whole finalize→promote chain — fully decoupled
from whatever session the task-completion handler / reaper passed.  Inside that
dedicated session ``promote()``'s intermediate ``commit()`` (which releases the
row lock before the 30-120s of GitHub I/O) only ever flushes qualifier state,
because the dedicated session has nothing else dirty.  A fresh session also
cannot self-deadlock: the completion handler holds a lock on the *Task* row, not
the ``ReleaseVersion`` row, and the reaper sweep takes no lock on the version
before delegating — so the dedicated session's ``SELECT ... FOR UPDATE`` on the
version row can never wait on a lock the caller still holds.

Hard blockers (CI red, smoketest startup-suite failure when the subsystem is
enabled, any deleted test file) force confidence to 0.0 — those candidates
never promote regardless of the other sub-scores.  When the smoketest
subsystem is disabled the smoketest dimension is dropped and the remaining
weights renormalize (see ``compute_composite``).

Ships dark: actual promotion runs only when ``runtime_config["promotion_enabled"]``
is True.  Otherwise the composite is computed and logged (dry-run) but no PR is
opened.

Public API: ``run_qualification``, ``maybe_finalize_qualification``,
``sweep_stuck_qualifications``, ``_finalize_qualification``,
``compute_composite``, ``_load_policy``, ``promote``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import github, release_version
from app.database import AsyncSessionLocal
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    QualificationVerdict,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.notification_dispatch import (
    post_event_message,
    post_version_build_stalled_message,
    post_version_calcification_message,
    post_version_promotion_readiness_message,
    post_version_stabilize_stalled_message,
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
    "threshold": 0.90,
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

        # Validate the weights sum to ~1.0.  This file decides what reaches
        # production main: weights summing >1.0 saturate confidence at 1.0 so
        # every candidate promotes (gate effectively disabled); <1.0 means
        # nothing can clear the threshold (gate stuck closed).  A human-edited
        # mistake here is silent and dangerous — fall back to the safe default
        # rubric rather than honour a mis-weighted policy.
        weight_sum = weights["audit"] + weights["test_erosion"] + weights["smoketest"]
        if abs(weight_sum - 1.0) > 0.01:
            raise ValueError(
                f"promotion_policy.yaml weights sum to {weight_sum:.4f}, expected ~1.0"
            )

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


# Terminal task statuses — a qualifier child in any of these has finished
# (whether it succeeded, failed, or was cancelled).
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# Audit H2: hard wall-clock bound on a single qualification.  A qualification
# spawns two heavy agent-VM children (release_audit + qualifier-smoketest),
# each of which may itself retry — but if a child heartbeats-but-hangs, gets
# stuck in a recovery/requeue loop, or otherwise never reaches a terminal
# state, the version wedges at last_qualification.status == "pending" forever
# with no signal.  sweep_stuck_qualifications force-finalizes any pending
# qualification older than this as a blocked one (blocker
# "qualification_timeout") and raises an Alert.  Six hours mirrors the
# EVALUATING wall-clock backstop in services.py — long enough for a genuinely
# progressing two-task pipeline (including retries) to finish, short enough
# that a wedged qualification surfaces within a useful operator window.
_MAX_QUALIFICATION_DURATION_SECONDS = 6 * 60 * 60

# Minimum consecutive hard-blocker failures before raising a calcification
# alert.  Conservative: three failures on the same blocker strongly suggest
# the issue is not transient and the version is genuinely stuck.
_MIN_CALCIFICATION_ATTEMPTS = 3

# Minimum consecutive qualifier infrastructure errors (FAILED/CANCELLED
# required child tasks) before raising a dedicated infra-failure alert.
# Mirrors _MIN_CALCIFICATION_ATTEMPTS: three consecutive infra failures
# strongly suggest the qualifier pipeline itself (agent VM / clone) is broken.
_MIN_INFRA_ERROR_STREAK = 3

# Build-phase calcification thresholds — the backstop for the OTHER end of the
# lifecycle from _check_version_calcification.  build→stabilize is entirely
# agent-driven (the strategic agent must emit a version_phase_decision); there
# is no cadence and no automatic backstop, so a conservative or distracted
# strategic agent can leave an open version accumulating merged tasks in the
# 'build' phase indefinitely, never reaching qualify/promote.  A build-phase
# version that has sat for this many days OR accumulated this many merged tasks
# since the phase started has likely stalled.  EITHER trigger fires the alert.
# These are deliberately CONSERVATIVE: the goal is to catch a genuine stall,
# not nag a normally-progressing build.
_BUILD_STALL_AGE_DAYS = 14
_BUILD_STALL_MERGED_TASKS = 40

# Stabilize-phase stall thresholds — the backstop for the stabilize→qualify
# edge of the lifecycle.  A version that enters stabilize but never gets
# qualified falls through both the build-stall guard (wrong phase) and the
# calcification guard (requires repeated hard-blocker failures).  These
# conservative thresholds catch a genuine stall without nagging a normally-
# progressing stabilize phase.  EITHER trigger fires the alert.
_STABILIZE_STALL_AGE_DAYS = 14
_STABILIZE_STALL_MERGED_TASKS = 40


def _is_build_stalled(age_days: int, merged_count: int) -> bool:
    """Return True iff a build-phase version's age OR merged-task count is past
    the conservative build-stall threshold.

    Shared by ``_check_version_calcification``'s build-phase sibling
    (``_check_version_build_stall``) and the strategic build-stall nudge so both
    judge a stall by the exact same rule.
    """
    return (
        age_days >= _BUILD_STALL_AGE_DAYS or merged_count >= _BUILD_STALL_MERGED_TASKS
    )


async def _qualification_in_flight(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Return True iff ``version`` has a qualification *actively running*.

    "In flight" = ``last_qualification.status == "pending"`` AND at least one
    recorded child task (audit / smoketest) is still non-terminal.  This is the
    in-flight guard for ``run_qualification``: it must distinguish a genuinely
    running qualification (do not re-spawn) from a ``pending`` record whose
    children have all already terminated (awaiting finalize — a fresh run is
    still permitted; the stale record is harmless and gets overwritten).

    The qualifier-smoketest child is optional (audit H3): when the qualifying
    run recorded ``smoketest_enabled == False`` the smoketest subsystem was
    disabled and ``smoketest_task_id`` is legitimately ``None`` — that absence
    is NOT a parse failure, the qualification simply has a single child.

    A ``pending`` record with a missing/unparseable *required* child task id
    is treated as in flight (conservative — the run that stamped it may still
    be spawning).
    """
    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        return False

    # The audit child is always required.  The smoketest child is required
    # only when the qualifying run had the smoketest subsystem enabled —
    # records that predate the smoketest_enabled flag default to "required"
    # so legacy pending records keep their original conservative behaviour.
    required_keys = ["audit_task_id"]
    if qual.get("smoketest_enabled", True):
        required_keys.append("smoketest_task_id")

    child_ids: list[uuid.UUID] = []
    parse_failed = False
    for key in required_keys:
        raw = qual.get(key)
        if not raw:
            parse_failed = True
            continue
        try:
            child_ids.append(uuid.UUID(str(raw)))
        except (ValueError, AttributeError):
            parse_failed = True

    if parse_failed:
        # A pending record we cannot fully resolve — assume the prior run is
        # still mid-spawn rather than risk a duplicate.
        return True

    for task_id in child_ids:
        task = await session.get(Task, task_id)
        if task is None:
            # The child row is not (yet) persisted — the spawning run is still
            # in progress; treat as in flight.
            return True
        if task.status not in _TERMINAL_STATUSES:
            return True
    # status == "pending" but every child already terminated → not in flight.
    return False


async def _increment_qualification_attempts(
    session: AsyncSession,
    version_id: uuid.UUID,
) -> None:
    """Atomically bump ``qualification_attempts`` on a ReleaseVersion row.

    Audit R4 MEDIUM — the ``no_credentials`` / ``sha_error`` early-return
    paths in ``run_qualification`` run BEFORE the ``ReleaseVersion`` FOR UPDATE
    lock is taken, so a read-modify-write
    (``version.qualification_attempts = (version.qualification_attempts or 0) + 1``)
    is vulnerable to a lost update: two concurrent callers (strategic
    ``qualify_request`` + admin ``POST /qualify``) both read the same value and
    write back the same ``+1``.

    This issues a single atomic SQL ``UPDATE ... SET qualification_attempts =
    qualification_attempts + 1`` — the increment happens in the database
    relative to the row's current value, so concurrent callers can never lose
    an increment.  ``COALESCE`` is used so a (defensively) NULL counter still
    increments cleanly to 1.  Dialect-agnostic: works on both Postgres and the
    SQLite test fixtures.

    The caller's later ``session.commit()`` persists this; the in-memory
    instance's ``qualification_attempts`` attribute is expired by the
    ``update()`` statement's session synchronization and lazily reloads on next
    access (the early-return callers do not read it back).
    """
    await session.execute(
        update(release_version.ReleaseVersion)
        .where(release_version.ReleaseVersion.id == version_id)
        .values(
            qualification_attempts=(
                sa_func.coalesce(
                    release_version.ReleaseVersion.qualification_attempts, 0
                )
                + 1
            )
        )
    )


# ---------------------------------------------------------------------------
# Composite — pure scoring function.
# ---------------------------------------------------------------------------


def compute_composite(
    ci_green: bool,
    audit_score: float,
    test_erosion: float,
    smoketest_score: float,
    policy: dict[str, Any],
    *,
    smoketest_enabled: bool = True,
) -> dict[str, Any]:
    """Compute the composite promotion confidence for a release candidate.

    Hard blockers — each forces ``confidence = 0.0`` and ``promote = False``.
    They are evaluated in this order, and ``blocker`` reports the first match:
      * ``not ci_green``           — CI is not green on the candidate SHA
        (``"ci_not_green"``).
      * ``smoketest_score`` is None — the smoketest never produced a usable
        score, treated as a startup-suite failure
        (``"smoketest_startup_failed"``).  ONLY checked when
        ``smoketest_enabled`` is True (see below).
      * ``audit_score`` is None     — the release_audit report is missing or
        unusable (``"audit_missing"``).  Checked before test-erosion because a
        missing audit report nulls both inputs.
      * ``test_erosion`` is None    — the diff deleted a test file
        (``"test_file_deleted"``); a real deletion, only meaningful when the
        audit report itself was present.

    ``smoketest_enabled`` (audit H3): the qualifier-smoketest subsystem is
    gated by ``runtime_config["smoketest_enabled"]``, which defaults False.
    When the qualifying run had it disabled it spawned NO qualifier-smoketest
    child and there is no SMOKETEST_RESULT artifact — so ``smoketest_score``
    is unconditionally None.  Treating that None as the
    ``smoketest_startup_failed`` hard blocker would make EVERY candidate fail
    while the subsystem is off.  Therefore, when ``smoketest_enabled`` is
    False this function: (a) skips the smoketest hard blocker, (b) drops the
    smoketest dimension from the weighted sum, and (c) renormalizes the
    remaining ``audit`` + ``test_erosion`` weights so they sum to 1.0.  When
    True it behaves exactly as the design-doc rubric specifies.

    The numeric inputs are clamped to [0,1] before weighting.  When no hard
    blocker fires, ``confidence`` is the policy-weighted sum of the active
    sub-scores.  ``promote`` is True iff ``confidence >= threshold``.

    Returns ``{confidence, promote, sub_scores, blocker}`` where ``blocker`` is
    a short human-readable string when a hard blocker fired, else None.
    """
    weights = policy.get("weights") or _DEFAULT_POLICY["weights"]
    threshold = float(policy.get("threshold", _DEFAULT_POLICY["threshold"]))
    w_audit = float(weights.get("audit", 0.40))
    w_test = float(weights.get("test_erosion", 0.30))
    w_smoke = float(weights.get("smoketest", 0.30))

    if not smoketest_enabled:
        # Audit H3: drop the smoketest dimension and renormalize audit +
        # test_erosion so the two remaining weights sum to 1.0.  A degenerate
        # policy where audit + test_erosion both round to 0 falls back to the
        # documented 0.40/0.30 default split so confidence is never NaN.
        audit_test_sum = w_audit + w_test
        if audit_test_sum > 0:
            w_audit = w_audit / audit_test_sum
            w_test = w_test / audit_test_sum
        else:
            default_sum = (
                _DEFAULT_POLICY["weights"]["audit"]
                + _DEFAULT_POLICY["weights"]["test_erosion"]
            )
            w_audit = _DEFAULT_POLICY["weights"]["audit"] / default_sum
            w_test = _DEFAULT_POLICY["weights"]["test_erosion"] / default_sum
        w_smoke = 0.0

    # Hard blockers — checked before any weighting.  ``None`` is the sentinel
    # for "this dimension hit an irrecoverable blocker"; the caller passes None
    # for test_erosion on a deleted test file and None for smoketest on a
    # startup-suite failure.
    #
    # Ordering matters when several inputs are None at once.  A FAILED
    # release_audit posts no RELEASE_AUDIT_REPORT, so _score_audit(None)
    # returns (audit_score=None, test_erosion=None) — BOTH None even though no
    # test file was actually deleted.  ``audit_score is None`` must therefore
    # be checked BEFORE ``test_erosion is None`` so an audit that never ran is
    # reported as "audit_missing" rather than mislabelled "test_file_deleted":
    # a None test_erosion only genuinely means a deleted test file when the
    # audit report itself was present (audit_score not None).
    blocker: str | None = None
    if not ci_green:
        blocker = "ci_not_green"
    elif smoketest_enabled and smoketest_score is None:
        # Only a hard blocker when the smoketest subsystem was actually
        # enabled — a disabled subsystem legitimately produces no score.
        blocker = "smoketest_startup_failed"
    elif audit_score is None:
        # An audit that produced no usable score is a soft signal, but without
        # it the composite is meaningless — treat as a blocker so a missing
        # audit can never promote on smoketest + test_erosion alone.  Reported
        # ahead of test_file_deleted because a missing report nulls both.
        blocker = "audit_missing"
    elif test_erosion is None:
        blocker = "test_file_deleted"

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

    # When the smoketest dimension is dropped its weight is 0.0, so the term
    # contributes nothing regardless of the (unused) smoketest sub-score.
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
            (
                await session.execute(
                    select(UserIntegration).where(
                        UserIntegration.user_id == project.user_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if integration and integration.github_installation_id:
            try:
                token = await generate_installation_access_token(
                    integration.github_installation_id, repositories=[repo]
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[RELEASE_QUALIFIER] stored installation token failed for %s/%s",
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
# Qualifier-scoped CI resolution — the real-green-build gate.
#
# The qualifier must NOT treat the CI dimension as green unless a real,
# completed, green build exists for the exact candidate commit.  Two project
# realities make a naive check unreliable:
#
#   * CI (.github/workflows/ci.yml) runs only on ``pull_request`` events —
#     there is no push trigger.  A freshly-merged ``automain`` HEAD commit
#     therefore carries NO check-runs of its own; the check-runs that actually
#     verified the change live on the head commit of the PR that introduced it.
#   * the shared ``github.get_check_runs`` helper fails OPEN on an empty
#     check-runs list (so the orchestrator's CI-wait does not hang on no-CI
#     repos).  Inheriting that here would let a candidate score CI-green purely
#     from the ABSENCE of any signal.
#
# This resolver scopes a stricter interpretation to the qualifier only — it
# passes ``treat_missing_as_success=False`` so an empty check-runs list is
# distinguishable, and resolves CI through the candidate's PR head when the
# candidate carries no checks of its own.  The shared helper's global
# fail-open behaviour is unchanged for every other caller.
# ---------------------------------------------------------------------------

# CI resolution states for a qualifier candidate commit.
_CI_GREEN = "green"  # a real completed, all-passing build exists
_CI_RED = "red"  # a completed build with at least one failure
_CI_PENDING = "pending"  # checks in progress, or no check-runs found anywhere
_CI_NO_CHECKS = "no_checks"  # the commit itself carries no check-runs


def _classify_check_runs(result: dict[str, Any] | None) -> str:
    """Map a strict ``get_check_runs`` result to a qualifier CI state."""
    if not isinstance(result, dict):
        return _CI_PENDING
    status = result.get("status")
    if status == "no_checks":
        return _CI_NO_CHECKS
    if status == "completed":
        conclusion = result.get("conclusion")
        if conclusion == "success":
            return _CI_GREEN
        if conclusion == "failure":
            return _CI_RED
        return _CI_PENDING
    return _CI_PENDING


async def _resolve_candidate_ci(
    token: str,
    owner: str,
    repo: str,
    candidate_sha: str,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Resolve the *real* CI verdict for a release-qualifier candidate commit.

    Resolution order:
      1. Inspect check-runs on ``candidate_sha`` itself, in strict mode (an
         empty list is NOT a pass).  A completed green/red build here is
         authoritative — return it.  Checks still in progress → pending.
      2. When the candidate carries no check-runs of its own, look up the PR(s)
         that introduced it (``commits/{sha}/pulls``) and evaluate check-runs
         on the PR head commit — that is where ``pull_request``-triggered CI
         actually ran.

    Returns ``{"state": str, "ci_green": bool, "resolved_sha": str | None,
    "detail": str}`` — ``ci_green`` is True ONLY for the ``green`` state.
    Never raises: any GitHub I/O failure degrades to the ``pending`` state.
    """
    if not candidate_sha:
        return {
            "state": _CI_PENDING,
            "ci_green": False,
            "resolved_sha": None,
            "detail": "no candidate sha",
        }

    # --- Step 1: check-runs on the candidate commit itself -----------------
    try:
        direct = await github.get_check_runs_cached(
            token,
            owner,
            repo,
            candidate_sha,
            project_id=project_id,
            treat_missing_as_success=False,
        )
    except Exception as exc:  # noqa: BLE001 — CI fetch failure → not-green/pending
        logger.warning(
            "[RELEASE_QUALIFIER] CI check-runs fetch failed for %s/%s@%s: %s",
            owner,
            repo,
            candidate_sha[:8],
            exc,
        )
        return {
            "state": _CI_PENDING,
            "ci_green": False,
            "resolved_sha": candidate_sha,
            "detail": f"candidate check-runs fetch failed: {exc}",
        }

    direct_state = _classify_check_runs(direct)
    if direct_state in (_CI_GREEN, _CI_RED):
        return {
            "state": direct_state,
            "ci_green": direct_state == _CI_GREEN,
            "resolved_sha": candidate_sha,
            "detail": f"candidate commit check-runs: {direct_state}",
        }
    if direct_state == _CI_PENDING:
        return {
            "state": _CI_PENDING,
            "ci_green": False,
            "resolved_sha": candidate_sha,
            "detail": "check-runs in progress on the candidate commit",
        }

    # --- Step 2: resolve through the PR that introduced the candidate ------
    # direct_state == _CI_NO_CHECKS: the candidate has no check-runs of its own.
    try:
        prs = await github.list_pull_requests_for_commit(
            token, owner, repo, candidate_sha, project_id=project_id
        )
    except Exception as exc:  # noqa: BLE001 — PR lookup failure → not-green/pending
        logger.warning(
            "[RELEASE_QUALIFIER] PR-for-commit lookup failed for %s/%s@%s: %s",
            owner,
            repo,
            candidate_sha[:8],
            exc,
        )
        return {
            "state": _CI_PENDING,
            "ci_green": False,
            "resolved_sha": None,
            "detail": f"pr-for-commit lookup failed: {exc}",
        }

    # Prefer merged PRs; fall back to every associated PR when none carry merged_at.
    merged_prs = [pr for pr in prs if isinstance(pr, dict) and pr.get("merged_at")]
    use_prs = merged_prs or [pr for pr in prs if isinstance(pr, dict)]
    head_shas: list[str] = []
    for pr in use_prs:
        head = pr.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if head_sha and head_sha != candidate_sha and head_sha not in head_shas:
            head_shas.append(head_sha)

    if not head_shas:
        return {
            "state": _CI_PENDING,
            "ci_green": False,
            "resolved_sha": None,
            "detail": "no check-runs on candidate and no PR head with CI",
        }

    # Exhaustive RED-preferring sweep: inspect every PR head before deciding.
    # A red on ANY head must win over a green on another head regardless of the
    # order GitHub returns them — return green only when at least one head is
    # green AND no associated head is red.
    first_red_sha: str | None = None
    first_green_sha: str | None = None
    for head_sha in head_shas:
        try:
            pr_checks = await github.get_check_runs_cached(
                token,
                owner,
                repo,
                head_sha,
                project_id=project_id,
                treat_missing_as_success=False,
            )
        except Exception as exc:  # noqa: BLE001 — keep trying remaining PR heads
            logger.warning(
                "[RELEASE_QUALIFIER] CI check-runs fetch failed for PR head "
                "%s/%s@%s: %s",
                owner,
                repo,
                head_sha[:8],
                exc,
            )
            continue
        pr_state = _classify_check_runs(pr_checks)
        if pr_state == _CI_RED and first_red_sha is None:
            first_red_sha = head_sha
        elif pr_state == _CI_GREEN and first_green_sha is None:
            first_green_sha = head_sha
        # pending / no_checks — keep inspecting remaining heads.

    if first_red_sha is not None:
        return {
            "state": _CI_RED,
            "ci_green": False,
            "resolved_sha": first_red_sha,
            "detail": "PR head check-runs failed",
        }
    if first_green_sha is not None:
        return {
            "state": _CI_GREEN,
            "ci_green": True,
            "resolved_sha": first_green_sha,
            "detail": "PR head check-runs passed",
        }
    return {
        "state": _CI_PENDING,
        "ci_green": False,
        "resolved_sha": None,
        "detail": "PR head check-runs still pending / absent",
    }


# ---------------------------------------------------------------------------
# run_qualification — the coordinator (phase 1: spawn).
# ---------------------------------------------------------------------------


async def run_qualification(
    session: AsyncSession,
    project_id: uuid.UUID,
    version: release_version.ReleaseVersion,
) -> dict[str, Any]:
    """Kick off qualification for the open release ``version``.

    This is the single chokepoint for starting a qualification — both the
    strategic ``qualify_request`` path and the admin ``/qualify`` endpoint
    funnel through here.  It is **self-defending**: callers need no guard of
    their own.  Before spawning anything it early-returns when:

      * ``version.phase != "stabilize"`` ⇒ ``{"status": "wrong_phase"}``.
        Qualification only ever runs on a stabilize-phase candidate.
      * a qualification is already in flight ⇒ ``{"status": "already_pending"}``.
        "In flight" means ``last_qualification.status == "pending"`` AND at
        least one recorded child task is still non-terminal.  Re-emitting
        ``qualify_request`` every strategic cycle or double-clicking the admin
        button must NOT spawn a second pair of heavy agent VMs or orphan the
        prior children.  (A ``pending`` record whose children have all
        terminated is *not* in flight — it is awaiting finalize — so a fresh
        run is still allowed; ``_finalize_qualification``/the reaper handle
        that stale record.)
      * the prior qualification finalized as a ``dry_run`` and ``automain``
        has not advanced since ⇒ ``{"status": "already_dry_run"}``.  In the
        shipped-dark default (``promotion_enabled=False``) a passing candidate
        records ``status="dry_run"`` and the version stays open + stabilize.
        Without this guard the strategic agent re-emits ``qualify_request``
        every cycle and re-spawns two agent VMs for an identical, already-
        scored commit.  Once ``automain`` moves, the candidate is new and a
        fresh qualification is allowed.

    Concurrency (audit C3): two callers can race here — the strategic
    ``qualify_request`` path and the admin ``POST /qualify`` endpoint.  Neither
    caller holds a lock that serializes THIS function: the strategic path holds
    the *Project* row lock (``lock_project_then_task`` in the post-commit
    chain) but that does NOT cover the ``ReleaseVersion`` row, and the admin
    endpoint holds no lock at all.  Without serialization both callers would
    pass the in-flight guard and each spawn an audit+smoketest pair (4
    children, one orphaned pair) and clobber each other's
    ``last_qualification`` write.  So this function takes its OWN
    ``SELECT ... FOR UPDATE`` on the ``ReleaseVersion`` row and holds it across
    the in-flight check, the child spawns, and the ``last_qualification``
    write — that ReleaseVersion lock (not the strategic path's Project lock) is
    what serializes the race.  The GitHub I/O that resolves ``candidate_sha``
    runs BEFORE the lock to keep the locked window short.

    Steps (none of which block on the spawned children):
      1. Resolve the project's owner/repo/GitHub token.
      2. Snapshot the candidate SHA: ``automain`` HEAD (full 40-char SHA).
      3. Check CI on that SHA via ``get_check_runs_cached`` (cheap, orchestrator
         side).  ``ci_green`` is recorded but not acted on here — the composite
         in ``_finalize_qualification`` applies it as a hard blocker.
      4. Lock the ``ReleaseVersion`` row, then re-check the in-flight /
         already-dry-run guards under the lock.
      5. Spawn a ``release_audit`` task (clones ``main``, audits the
         ``main...<candidate_sha>`` diff against main's standards) and — only
         when ``smoketest_enabled`` is True — a qualify-time ``smoketest`` task.
         Both child payloads carry the exact ``candidate_sha`` (audit C4) so
         the children check out the precise commit that was snapshotted rather
         than whatever ``automain`` drifts to by their runtime; both carry
         ``context["release_version_id"]``; the smoketest also carries
         ``context["qualifier_smoketest"] = True`` so its failure does not
         spawn a fix task.
      6. Stamp ``version.last_qualification = {status: "pending", ...}`` and
         bump ``version.qualification_attempts``.

    Returns a status dict.  ``status`` is ``"spawned"`` on success;
    ``"wrong_phase"`` / ``"already_pending"`` / ``"already_dry_run"`` when a
    guard fired (no side effects); or an error code (``"project_not_found"``,
    ``"no_credentials"``, ``"sha_error"``, ``"spawn_failed"``).  The caller
    commits the session.
    """
    # Guard 1: only qualify a stabilize-phase candidate.  Cheap, no I/O — do
    # it before resolving credentials so a wrong-phase call is a pure no-op.
    # The phase is re-checked under the row lock further down (after the
    # GitHub I/O) so a concurrent phase flip can't slip a spawn through.
    if version.phase != "stabilize":
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d is in "
            "phase=%s, not stabilize — skipping (wrong_phase)",
            version.version_number,
            version.phase,
        )
        return {"status": "wrong_phase"}

    # Cheap pre-lock in-flight check (no I/O — pure DB reads).  This is a
    # best-effort fast path so the common case (a re-emitted qualify_request
    # while a qualification is already running) short-circuits BEFORE the three
    # GitHub round-trips below.  It is NOT the correctness guard — a concurrent
    # caller could still pass this and the GitHub I/O simultaneously; the
    # authoritative in-flight re-check happens under the row lock (audit C3).
    if await _qualification_in_flight(session, version):
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d already has a "
            "qualification in flight — skipping (already_pending, pre-lock)",
            version.version_number,
        )
        return {"status": "already_pending"}

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
        flag_modified(version, "last_qualification")
        session.add(version)
        # Audit R4 MEDIUM: increment the counter with an atomic SQL UPDATE
        # rather than a read-modify-write.  This early-return path runs BEFORE
        # the ReleaseVersion FOR UPDATE lock, so two concurrent callers
        # (strategic qualify_request + admin POST /qualify) could otherwise
        # both read the same attempts value and write back the same +1,
        # losing one increment.
        await _increment_qualification_attempts(session, version.id)
        return {"status": "no_credentials"}
    token, owner, repo = resolved

    # 2. Snapshot the candidate SHA — automain HEAD.  Audit C3: this GitHub
    #    round-trip is done BEFORE taking the ReleaseVersion row lock so the
    #    lock is not held across external I/O.
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
        flag_modified(version, "last_qualification")
        session.add(version)
        # Audit R4 MEDIUM: atomic SQL increment — see the no_credentials path
        # above.  This early return is also pre-lock, so a read-modify-write
        # would be vulnerable to a lost update under concurrent callers.
        await _increment_qualification_attempts(session, version.id)
        return {"status": "sha_error"}

    # 3. CI check on the candidate SHA (cheap, orchestrator-side).  Done before
    #    the row lock — it is read-only external I/O.  This resolves CI through
    #    the candidate's PR head when the candidate carries no check-runs of its
    #    own (CI runs only on pull_request events), and never fails green purely
    #    from the absence of a signal.  ``ci_green`` recorded here is only a
    #    spawn-time snapshot — _finalize_qualification RE-RESOLVES CI and
    #    applies it as the composite hard blocker, so a candidate whose checks
    #    are merely pending now is not frozen not-green forever.
    #    _resolve_candidate_ci never raises.
    ci_resolution = await _resolve_candidate_ci(
        token, owner, repo, candidate_sha, project_id=str(project_id)
    )
    ci_green = ci_resolution["ci_green"]
    ci_state = ci_resolution["state"]

    # 4. Take the ReleaseVersion row lock (audit C3).  The strategic
    #    qualify_request path and the admin POST /qualify endpoint can both
    #    reach here concurrently; without serializing them they each pass the
    #    in-flight guard and spawn a duplicate audit+smoketest pair, and the
    #    later last_qualification write clobbers the earlier one.  FOR UPDATE
    #    mirrors maybe_finalize_qualification's pattern — held across the
    #    in-flight check, the spawns, and the last_qualification write.  The
    #    second caller blocks here until the first commits, then sees the
    #    pending record and short-circuits at the in-flight guard below.
    locked_version = (
        (
            await session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version.id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if locked_version is None:
        logger.warning(
            "[RELEASE_QUALIFIER] run_qualification: version row %s vanished "
            "before lock — skipping",
            version.id,
        )
        return {"status": "version_not_found"}
    # From here on, mutate the freshly-locked row (its attributes are the
    # post-lock DB truth) rather than the caller's possibly-stale instance.
    version = locked_version

    # Re-check the phase under the lock — a concurrent strategic cycle may
    # have flipped it out of stabilize between the cheap guard and the lock.
    if version.phase != "stabilize":
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d phase changed "
            "to %s before lock — skipping (wrong_phase)",
            version.version_number,
            version.phase,
        )
        return {"status": "wrong_phase"}

    # Guard 2 (under the lock): a qualification already in flight (pending +
    # a live child).  Re-emitting qualify_request every strategic cycle or
    # double-clicking the admin button must NOT spawn a second pair of heavy
    # agent VMs or orphan the prior children.
    if await _qualification_in_flight(session, version):
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d already has a "
            "qualification in flight — skipping (already_pending)",
            version.version_number,
        )
        return {"status": "already_pending"}

    # Guard 3 (under the lock — audit H1): the prior qualification finalized as
    # a dry_run and automain has not advanced.  In the shipped-dark default
    # (promotion_enabled=False) a passing candidate records status="dry_run"
    # and the version stays open + stabilize; the strategic agent then re-emits
    # qualify_request every cycle.  Re-qualifying the same already-scored
    # commit just burns two more agent VMs, so short-circuit until automain
    # actually moves.
    prior_qual = version.last_qualification or {}
    if (
        prior_qual.get("status") == "dry_run"
        and prior_qual.get("candidate_sha") == candidate_sha
    ):
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d already has a "
            "dry_run for candidate %s (automain unchanged) — skipping "
            "(already_dry_run)",
            version.version_number,
            candidate_sha[:8],
        )
        return {"status": "already_dry_run"}

    # Carry forward the infra-failure streak so _finalize_qualification can
    # detect a persistently broken qualifier pipeline.  The streak is the count
    # of consecutive infrastructure failures (FAILED/CANCELLED required child
    # tasks) before the current pending run — it resets to 0 whenever a prior
    # finalization wrote a non-infra_failure status.
    prior_infra_streak: int = (
        int(prior_qual.get("consecutive_infra_errors") or 0)
        if prior_qual.get("status") == "infra_failure"
        else 0
    )

    # 5. Spawn the heavy children.
    started_at = _utcnow().isoformat()
    # The LTS branch is always "main" (deploy.yml deploys from main) — NOT
    # integration_branch, which is the branch the pipeline builds ON (automain
    # post-Phase-1).  Audit C4: the diff range is pinned to the exact snapshot
    # commit (main...<candidate_sha>) rather than the live automain branch —
    # automain advances during the 10-30 min the children take to run, and an
    # un-pinned diff would audit a different changeset than was qualified.
    diff_range = f"main...{candidate_sha}"
    # Audit H3: the qualifier-smoketest child is spawned only when the
    # smoketest subsystem is enabled.  smoketest_enabled defaults False (the
    # subsystem is deferred), in which case no qualifier-smoketest task is
    # spawned and compute_composite drops the smoketest dimension entirely —
    # see _finalize_qualification / compute_composite.  When True, behave as
    # the design doc specifies (audit + test-erosion + smoketest).
    smoketest_enabled = bool(_runtime_config.get("smoketest_enabled"))
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
        if smoketest_enabled:
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
            "ci_state": ci_state,
            "started_at": started_at,
        }
        # Read-modify-write is safe here (unlike the no_credentials / sha_error
        # early returns, which run pre-lock and use the atomic
        # _increment_qualification_attempts): this site runs UNDER the
        # ReleaseVersion FOR UPDATE row lock taken above, so no concurrent
        # caller can interleave a lost update.
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
        flag_modified(version, "last_qualification")
        session.add(version)
        return {"status": "spawn_failed"}

    # 6. Stamp the pending qualification record.  smoketest_task_id is None
    #    when the smoketest subsystem is disabled — maybe_finalize_qualification
    #    and the reaper sweep both treat a None smoketest id as "no smoketest
    #    child to wait on".  prior_infra_streak carries the infra-failure streak
    #    from the previous infra_failure finalization (0 when the previous
    #    finalization was a quality verdict / dry-run / non-infra outcome).
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "ci_state": ci_state,
        "smoketest_enabled": smoketest_enabled,
        "started_at": started_at,
        "audit_task_id": audit_task_id,
        "smoketest_task_id": smoketest_task_id,
        "prior_infra_streak": prior_infra_streak,
    }
    # Read-modify-write is safe here (unlike the no_credentials / sha_error
    # early returns, which run pre-lock and use the atomic
    # _increment_qualification_attempts): this site runs UNDER the
    # ReleaseVersion FOR UPDATE row lock taken above, so no concurrent caller
    # can interleave a lost update.
    version.qualification_attempts = (version.qualification_attempts or 0) + 1
    flag_modified(version, "last_qualification")
    session.add(version)

    logger.info(
        "[RELEASE_QUALIFIER] qualification started for v%d project=%s "
        "candidate=%s ci_green=%s smoketest_enabled=%s audit_task=%s "
        "smoketest_task=%s",
        version.version_number,
        project_id,
        candidate_sha[:8],
        ci_green,
        smoketest_enabled,
        audit_task_id,
        smoketest_task_id,
    )
    return {
        "status": "spawned",
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "ci_state": ci_state,
        "smoketest_enabled": smoketest_enabled,
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
    (``main...<candidate_sha>`` — pinned to the exact snapshot commit, audit
    C4) against main's standards — the differential audit at the heart of the
    qualifier (design doc §6 gotcha 2: it MUST run on main).  Mirrors the
    ``_spawn_self_audit_task`` spawn pattern.

    The payload carries ``candidate_sha`` (the full 40-char SHA) so the agent's
    ``handleReleaseAudit`` checks out exactly that commit rather than whatever
    ``automain`` has drifted to by the task's runtime (audit C4 contract — the
    payload field name is exactly ``candidate_sha``).
    """
    payload: dict[str, Any] = {
        "type": "release_audit",
        "base_branch": base_branch,
        "diff_range": diff_range,
        # Audit C4: pin the audit to the exact qualified commit.  The agent
        # reads this from the payload and checks out this SHA.
        "candidate_sha": candidate_sha,
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
        title=f"Release audit: main...{candidate_sha[:8]} diff",
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
    """Create + queue a qualify-time ``smoketest`` task on the candidate commit.

    The context flag ``qualifier_smoketest=True`` marks this as a
    pre-promotion qualifier input — distinct from the post-promotion
    production smoketest — so its failure does NOT spawn a fix task
    (design doc §6 gotcha 5).

    The payload carries ``candidate_sha`` (the full 40-char SHA, audit C4) so
    the agent's qualifier-smoketest handler checks out exactly that commit
    after cloning ``automain`` — the candidate commit is reachable from the
    ``automain`` branch at spawn time, and pinning to the SHA means the
    smoketest scores the same commit the audit does even though ``automain``
    keeps advancing while the task is queued (audit C4 contract — the payload
    field name is exactly ``candidate_sha``).
    """
    payload: dict[str, Any] = {
        "type": "smoketest",
        "base_branch": "automain",
        # Audit C4: pin the smoketest to the exact qualified commit.  The agent
        # clones automain then checks out this SHA.
        "candidate_sha": candidate_sha,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get("smoketest")
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get("smoketest")
    if type_effort:
        payload["effort"] = type_effort
    health_check_urls = _runtime_config.get("health_check_urls") or []
    if health_check_urls:
        payload["health_check_urls"] = health_check_urls
    try:
        payload = validate_internal_payload(payload, "release_qualifier:smoketest")
    except PayloadTooLargeError:
        raise

    task = Task(
        project_id=project_id,
        title=f"Qualifier smoketest: candidate {candidate_sha[:8]}",
        description=(f"Qualify-time smoketest of release candidate {candidate_sha}"),
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
        (
            await session.execute(
                select(Artifact)
                .where(
                    Artifact.task_id == task_id,
                    Artifact.artifact_type == artifact_type,
                )
                .order_by(Artifact.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
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
    # Only status=="pass" counts toward the score — a "skip" (or any non-pass
    # status) contributes 0.  This is intentionally conservative: a skipped
    # qualifier test is unverified coverage, so it should pull the score down
    # rather than be treated as a pass.
    passed = sum(1 for t in tests if isinstance(t, dict) and t.get("status") == "pass")
    return _clamp01(passed / total) if total else 0.0


# ---------------------------------------------------------------------------
# _finalize_qualification — the coordinator (phase 2: collect + decide).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Operator notifications — the calibration instrument.  Every qualifier verdict
# posts a system ProjectMessage so the operator sees promotions, blocks and
# below-threshold near-misses without polling, and can tune promotion_policy.yaml
# from real outcomes.  Best-effort: a messaging failure never affects a release.
# ---------------------------------------------------------------------------

# Human-readable rendering of the compute_composite() hard-blocker codes.
_BLOCKER_LABELS: dict[str, str] = {
    "ci_not_green": "CI is not green on the candidate",
    "smoketest_startup_failed": "the smoketest startup suite failed",
    "audit_missing": "the release audit produced no usable report",
    "test_file_deleted": "the diff deleted a test file",
}


def _fmt_sub_scores(
    sub_scores: dict[str, Any] | None,
    *,
    smoketest_enabled: bool = True,
) -> str:
    """Render sub-scores as ``audit 0.95 · test 1.00 · smoke 0.80`` for messages.

    The smoketest term is omitted when the subsystem was disabled for the run —
    its sub-score is a meaningless 0.0 placeholder in that case.
    """
    if not sub_scores:
        return "n/a"
    parts = [
        f"audit {float(sub_scores.get('audit', 0.0)):.2f}",
        f"test {float(sub_scores.get('test_erosion', 0.0)):.2f}",
    ]
    if smoketest_enabled:
        parts.append(f"smoke {float(sub_scores.get('smoketest', 0.0)):.2f}")
    return " · ".join(parts)


async def _notify_operator(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    event_type: str,
    summary: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort operator notification for a release-qualifier verdict.

    Posts a system ``ProjectMessage`` so the operator sees every promotion
    outcome without polling.  Does not commit — the message rides the caller's
    commit, the same one that persists ``last_qualification``.

    A messaging failure must NEVER abort or fail qualification/promotion: the
    verdict is already decided and recorded by the time this runs.  Mirrors the
    best-effort ``create_tag`` pattern — wrapped in try/except, logs a warning
    and never propagates.
    """
    try:
        context: dict[str, Any] = {
            "version_number": version.version_number,
            "version_id": str(version.id),
        }
        if extra:
            context.update(extra)
        await post_event_message(
            session,
            version.project_id,
            event_type,
            summary,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001 — notifications must never break a release
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post operator notification %s "
            "(non-fatal): %s",
            event_type,
            exc,
        )


def _parse_iso_dt(raw: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a datetime, or None.

    Used to coerce the ``started_at`` / ``finalized_at`` ISO strings carried in
    a qualification ``base_record`` into datetime columns on the verdict-history
    row.  Returns None on a missing / non-string / unparseable value — recording
    is best-effort, so a bad timestamp must never raise.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _record_qualification_verdict(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    base_record: dict[str, Any],
    *,
    terminal_status: str,
    would_promote: bool,
    promotion_enabled: bool,
) -> None:
    """Best-effort: append ONE immutable ``QualificationVerdict`` history row.

    Phase 4 (calibration) durable evidence trail.  ``last_qualification`` is
    OVERWRITTEN on every finalize, so this appends a never-overwritten per-verdict
    row that ``compute_promotion_readiness`` reads across versions to reconstruct
    the dry-run track record.  Purely additive — ``last_qualification`` is still
    written exactly as before; this does not touch it.

    CRITICAL: recording must NEVER break or roll back a qualification/promotion.
    The verdict INSERT runs inside a SAVEPOINT (``begin_nested``) wrapped in
    try/except.  Before opening the savepoint the caller's already-staged
    mutations (``last_qualification`` etc.) are flushed into the OUTER
    transaction, so the savepoint contains ONLY the verdict INSERT — a rollback
    of that savepoint can never drop the qualification's own write.  Any failure
    (a malformed row, or — until the sibling Alembic migration lands — a missing
    ``qualificationverdict`` table) surfaces inside the savepoint, is rolled back
    to the savepoint leaving the outer transaction intact, and is logged and
    swallowed.  Purely additive: ``last_qualification`` is written exactly as
    before and the surrounding qualification/promotion is unaffected.
    """
    try:
        sub_scores = base_record.get("sub_scores") or {}
        if not isinstance(sub_scores, dict):
            sub_scores = {}
        # Embed CI resolution provenance in sub_scores.
        ci_resolution = base_record.get("ci_resolution")
        if isinstance(ci_resolution, dict):
            sub_scores = dict(sub_scores)  # make a copy to avoid mutating base_record
            sub_scores["ci_resolution"] = {
                "state": ci_resolution.get("state"),
                "resolved_sha": ci_resolution.get("resolved_sha"),
                "detail": ci_resolution.get("detail"),
            }

        confidence = base_record.get("confidence")
        threshold = base_record.get("threshold")
        verdict = QualificationVerdict(
            project_id=version.project_id,
            release_version_id=version.id,
            candidate_sha=base_record.get("candidate_sha"),
            confidence=(
                float(confidence)
                if isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                else 0.0
            ),
            sub_scores=sub_scores if sub_scores else None,
            blocker=base_record.get("blocker"),
            would_promote=bool(would_promote),
            promotion_enabled=bool(promotion_enabled),
            terminal_status=terminal_status,
            threshold=(
                float(threshold)
                if isinstance(threshold, (int, float))
                and not isinstance(threshold, bool)
                else None
            ),
            started_at=_parse_iso_dt(base_record.get("started_at")),
            finalized_at=_parse_iso_dt(base_record.get("finalized_at")),
        )
        # Flush the caller's staged mutations into the outer transaction first,
        # so the savepoint below isolates ONLY the verdict INSERT.
        await session.flush()
        async with session.begin_nested():
            session.add(verdict)
            await session.flush()
    except Exception as exc:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.warning(
            "[RELEASE_QUALIFIER] failed to record qualification verdict for v%s "
            "(non-fatal — additive history only): %s",
            getattr(version, "version_number", "?"),
            exc,
        )


async def _handle_infra_failure(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    qual: dict[str, Any],
    infra_error: str,
) -> dict[str, Any]:
    """Finalize a qualifier run that ended due to an infrastructure failure.

    Called when a required qualifier child (release_audit or qualifier
    smoketest) terminated as FAILED or CANCELLED rather than COMPLETED —
    indicating the qualifier pipeline itself broke (agent VM crash, timeout,
    clone failure), NOT a quality finding about the release candidate.

    Distinct from a quality verdict in three ways:
      1. Does NOT write a ``QualificationVerdict`` history row — no quality
         judgement was rendered so the verdict history stays clean.
      2. Returns the version to ``stabilize`` for a clean re-qualification,
         exactly like an error path (``missing_candidate_sha``), but with a
         clearly labelled ``infra_failure`` status.
      3. Posts a ``release_infra_failure`` operator notification that
         explicitly names this as an infrastructure failure — not a quality
         blocker — so the operator can distinguish pipeline noise from real
         candidate problems.

    Consecutive infra-failure backstop: after ``_MIN_INFRA_ERROR_STREAK``
    consecutive infrastructure failures the regular calcification guard is
    silent (it only triggers on ``not_promoted`` with a quality blocker).
    This backstop raises a separate Alert so a persistently broken qualifier
    pipeline surfaces to the operator.
    """
    finalized_at = _utcnow().isoformat()
    # Consecutive infra-failure streak: one more than whatever the previous
    # qualifying run reported (carried forward via prior_infra_streak stamped
    # by run_qualification; resets to 0 when a non-infra finalization wrote
    # a different status).
    prior_streak = int(qual.get("prior_infra_streak") or 0)
    current_streak = prior_streak + 1

    record: dict[str, Any] = {
        "candidate_sha": qual.get("candidate_sha"),
        "ci_green": qual.get("ci_green"),
        "smoketest_enabled": qual.get("smoketest_enabled", True),
        "status": "infra_failure",
        "error": infra_error,
        "consecutive_infra_errors": current_streak,
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": qual.get("audit_task_id"),
        "smoketest_task_id": qual.get("smoketest_task_id"),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number}: infrastructure failure "
            f"({infra_error}) — not a quality finding, returning to stabilize"
        ),
    )
    session.add(version)

    # Consecutive infra-error backstop alert.  The calcification guard never
    # fires for infra_failure records (it requires not_promoted + a quality
    # blocker), so a persistently broken qualifier pipeline would be invisible
    # without this sibling backstop.  Alert at multiples of the streak
    # threshold so an intermittently broken pipeline does not spam the operator.
    if (
        current_streak >= _MIN_INFRA_ERROR_STREAK
        and current_streak % _MIN_INFRA_ERROR_STREAK == 0
    ):
        session.add(
            Alert(
                project_id=version.project_id,
                severity=AlertSeverity.WARNING,
                title=(
                    f"Release qualifier v{version.version_number} infrastructure "
                    f"failure: {current_streak} consecutive errors"
                ),
                message=(
                    f"The release qualifier for v{version.version_number} has "
                    f"failed {current_streak} consecutive time(s) due to "
                    f"infrastructure errors ({infra_error!r}) — the qualifier "
                    f"agent VM or clone pipeline is broken. This is NOT a "
                    f"quality finding about the release candidate. Inspect the "
                    f"release_audit / qualifier-smoketest agent infrastructure "
                    f"and re-queue to retry once the issue is resolved."
                ),
            )
        )
        logger.warning(
            "[RELEASE_QUALIFIER] infra-failure backstop: v%d has %d consecutive "
            "infrastructure errors (error=%s) — alert raised",
            version.version_number,
            current_streak,
            infra_error,
        )

    await _notify_operator(
        session,
        version,
        "release_infra_failure",
        (
            f"v{version.version_number} qualifier infrastructure failure — "
            f"{infra_error.replace('_', ' ')} (agent VM / clone failure, "
            f"not a quality finding). Returned to stabilize for "
            f"re-qualification. Consecutive infra errors: {current_streak}."
        ),
        extra={
            "error": infra_error,
            "consecutive_infra_errors": current_streak,
        },
    )

    logger.warning(
        "[RELEASE_QUALIFIER] v%d infra failure: %s (streak=%d) — "
        "no verdict recorded, version returned to stabilize",
        version.version_number,
        infra_error,
        current_streak,
    )
    return {
        "status": "infra_failure",
        "error": infra_error,
        "consecutive_infra_errors": current_streak,
    }


async def _resolve_ci_at_finalize(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> tuple[bool, dict[str, Any]]:
    """Re-resolve the candidate commit's CI verdict at finalize time.

    ``run_qualification`` snapshots ``ci_green`` once, at spawn.  Reusing that
    frozen boolean at finalize is a livelock: if the candidate's checks were
    merely in progress when qualification started, ``ci_green`` was recorded
    False, finalize would keep blocking on it, the version returns to stabilize
    and the SAME commit is re-qualified and re-blocked indefinitely.  So
    finalize re-evaluates CI here against the candidate commit rather than
    trusting the spawn-time value — once the build completes green this
    re-resolution lets the CI dimension pass, while a still-pending build
    simply stays not-green WITHOUT a permanent dead-end.

    Returns (ci_green, ci_resolution) where ci_green is True ONLY when a real
    completed green build exists for the candidate, and ci_resolution contains
    the full provenance (state, resolved_sha, detail).  Any credential-resolution
    or GitHub I/O failure degrades gracefully to (False, degenerate_resolution)
    for THIS finalize attempt — it never crashes the finalize.
    """
    degenerate_resolution = {
        "state": _CI_PENDING,
        "resolved_sha": None,
        "detail": "unresolved",
    }
    if not candidate_sha:
        return False, degenerate_resolution
    try:
        project = await session.get(Project, version.project_id)
        if project is None:
            logger.warning(
                "[RELEASE_QUALIFIER] finalize CI re-resolution: project %s for "
                "version v%d not found — treating CI as not-green",
                version.project_id,
                version.version_number,
            )
            return False, degenerate_resolution
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            logger.warning(
                "[RELEASE_QUALIFIER] finalize CI re-resolution: no GitHub "
                "credentials for version v%d — treating CI as not-green",
                version.version_number,
            )
            return False, degenerate_resolution
        token, owner, repo = resolved
        ci_resolution = await _resolve_candidate_ci(
            token, owner, repo, candidate_sha, project_id=str(version.project_id)
        )
        logger.info(
            "[RELEASE_QUALIFIER] finalize CI re-resolution for v%d candidate "
            "%s: state=%s ci_green=%s (%s)",
            version.version_number,
            candidate_sha[:8],
            ci_resolution["state"],
            ci_resolution["ci_green"],
            ci_resolution["detail"],
        )
        # Extract only the provenance fields, excluding ci_green boolean.
        resolution_provenance = {
            "state": ci_resolution.get("state"),
            "resolved_sha": ci_resolution.get("resolved_sha"),
            "detail": ci_resolution.get("detail"),
        }
        return bool(ci_resolution["ci_green"]), resolution_provenance
    except Exception as exc:  # noqa: BLE001 — finalize CI failure → not-green
        logger.warning(
            "[RELEASE_QUALIFIER] finalize CI re-resolution failed for version "
            "v%d (treating CI as not-green for this attempt): %s",
            version.version_number,
            exc,
        )
        return False, degenerate_resolution


async def _finalize_qualification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> dict[str, Any]:
    """Finalize qualification once the qualifier children have terminated.

    Invoked by the Phase 3 batch-3 completion handlers (not wired here) after
    the release_audit child — and the qualify-smoketest child, when the
    smoketest subsystem was enabled for this run (audit H3) — for ``version``
    have reached a terminal state.

    Re-resolves CI for the candidate commit (``_resolve_ci_at_finalize``)
    rather than trusting the boolean frozen at spawn, reads the
    RELEASE_AUDIT_REPORT and (when the smoketest subsystem was
    enabled) SMOKETEST_RESULT artifacts, computes the composite, then:
      * promote == True  and promotion_enabled ⇒ ``promote()``.
      * promote == True  but promotion_enabled is False ⇒ dry-run: log the
        composite, record it, leave the version open.
      * promote == False ⇒ record the blocker, keep the version open, set
        ``phase = "stabilize"``.

    Audit H3: when the qualifying run recorded ``smoketest_enabled == False``
    the smoketest dimension is dropped — no SMOKETEST_RESULT is read and
    ``compute_composite`` renormalizes the audit + test-erosion weights with no
    smoketest hard blocker.

    Runs on whatever ``session`` it is given and does NOT commit it (the
    non-promote paths just mutate ``version.last_qualification``); the promote
    path delegates to ``promote()``, which issues its own intermediate commit.
    The caller (``_finalize_in_dedicated_session``) commits the dedicated
    session afterwards.  Returns a result dict.
    """
    qual = dict(version.last_qualification or {})
    candidate_sha = qual.get("candidate_sha")
    audit_task_id = qual.get("audit_task_id")
    smoketest_task_id = qual.get("smoketest_task_id")
    # Audit H3: whether the qualifying run had the smoketest subsystem enabled.
    # Read from the recorded qualification (NOT live runtime_config) — the
    # config could have flipped between spawn and finalize, and the composite
    # must match the children that actually ran.  Legacy records that predate
    # the flag default to True (the old always-spawn behaviour).
    smoketest_enabled = bool(qual.get("smoketest_enabled", True))

    # Re-resolve CI for the candidate commit NOW rather than reusing the
    # boolean frozen at spawn — a candidate whose checks were merely pending
    # when run_qualification ran recorded ci_green=False, and trusting that
    # stale value would re-block the same commit forever.  Re-resolution lets a
    # now-completed green build pass and keeps a still-pending build not-green
    # without a permanent dead-end.  Graceful: a credential / GitHub failure
    # degrades to not-green for this attempt (never crashes the finalize).
    ci_green, ci_resolution = await _resolve_ci_at_finalize(
        session, version, candidate_sha
    )

    policy = _load_policy()

    # --- Infra-failure detection: check required child terminal statuses ------
    # A FAILED or CANCELLED required child means the qualifier pipeline itself
    # broke (agent VM crash, timeout, clone failure) — NOT a quality finding.
    # Route these to _handle_infra_failure BEFORE reading artifacts so the
    # quality-verdict path only sees COMPLETED children.  A COMPLETED child
    # whose artifact is absent or unparseable still routes through the normal
    # compute_composite path (audit_missing quality blocker — the real report
    # was expected but absent, which is a candidate-scoring concern, not infra).
    if audit_task_id:
        try:
            _audit_task_obj = await session.get(Task, uuid.UUID(str(audit_task_id)))
            if _audit_task_obj is not None and _audit_task_obj.status in (
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                return await _handle_infra_failure(
                    session,
                    version,
                    qual,
                    f"audit_task_{_audit_task_obj.status.lower()}",
                )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid audit_task_id %s on version v%d "
                "(skipping infra-failure check)",
                audit_task_id,
                version.version_number,
            )

    if smoketest_enabled and smoketest_task_id:
        try:
            _smoke_task_obj = await session.get(Task, uuid.UUID(str(smoketest_task_id)))
            if _smoke_task_obj is not None and _smoke_task_obj.status in (
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                return await _handle_infra_failure(
                    session,
                    version,
                    qual,
                    f"smoketest_task_{_smoke_task_obj.status.lower()}",
                )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid smoketest_task_id %s on version v%d "
                "(skipping infra-failure check)",
                smoketest_task_id,
                version.version_number,
            )

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
    # Audit H3: only when the smoketest subsystem was enabled — a disabled run
    # spawned no qualifier-smoketest child, so there is no SMOKETEST_RESULT to
    # read and smoketest_score stays None (compute_composite drops the
    # dimension rather than hard-blocking on it).
    smoketest_result: dict[str, Any] | None = None
    if smoketest_enabled and smoketest_task_id:
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
        smoketest_enabled=smoketest_enabled,
    )

    finalized_at = _utcnow().isoformat()
    base_record: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "ci_resolution": ci_resolution,
        "smoketest_enabled": smoketest_enabled,
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
        await _record_qualification_verdict(
            session,
            version,
            record,
            terminal_status="not_promoted",
            would_promote=False,
            promotion_enabled=promotion_enabled,
        )
        scores_str = _fmt_sub_scores(
            composite["sub_scores"], smoketest_enabled=smoketest_enabled
        )
        if composite["blocker"]:
            blocker_label = _BLOCKER_LABELS.get(
                composite["blocker"], composite["blocker"]
            )
            notify_event = "release_blocked"
            notify_summary = (
                f"v{version.version_number} blocked from promotion — "
                f"{blocker_label}. Not promoted; staying in stabilize."
            )
        else:
            notify_event = "release_below_threshold"
            notify_summary = (
                f"v{version.version_number} qualified at confidence "
                f"{composite['confidence']:.2f}, below the "
                f"{policy.get('threshold')} promotion bar — not promoted, "
                f"staying in stabilize. Sub-scores: {scores_str}."
            )
        await _notify_operator(
            session,
            version,
            notify_event,
            notify_summary,
            extra={
                "confidence": composite["confidence"],
                "threshold": policy.get("threshold"),
                "sub_scores": composite["sub_scores"],
                "blocker": composite["blocker"],
            },
        )
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
        await _record_qualification_verdict(
            session,
            version,
            record,
            terminal_status="dry_run",
            would_promote=True,
            promotion_enabled=False,
        )
        logger.info(
            "[RELEASE_QUALIFIER] v%d would promote (confidence=%.4f) but "
            "promotion_enabled=False — dry-run, not promoting",
            version.version_number,
            composite["confidence"],
        )
        await _notify_operator(
            session,
            version,
            "release_dry_run",
            (
                f"v{version.version_number} would promote (confidence "
                f"{composite['confidence']:.2f}) but promotion is disabled — "
                f"dry-run only, main not advanced."
            ),
            extra={
                "confidence": composite["confidence"],
                "threshold": policy.get("threshold"),
                "sub_scores": composite["sub_scores"],
            },
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
        await _notify_operator(
            session,
            version,
            "release_error",
            (
                f"v{version.version_number} qualification could not complete "
                f"— candidate SHA missing. Returned to stabilize."
            ),
            extra={"error": "missing_candidate_sha"},
        )
        return {"status": "error", "error": "missing_candidate_sha"}

    # Delegate to promote().  Credentials are NOT pre-resolved here on purpose
    # (concern 2): credential resolution is GitHub I/O, and at this point the
    # dedicated session still holds the ReleaseVersion FOR UPDATE lock.
    # promote() releases that lock via its intermediate commit and then resolves
    # the token unlocked — so no GitHub round-trip ever runs under the row lock.
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


async def _required_children_terminal(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Return True iff every required qualifier child of ``version`` is terminal.

    The audit child is always required; the qualifier-smoketest child is
    required only when the qualifying run had the smoketest subsystem enabled
    (audit H3) — legacy records that predate the ``smoketest_enabled`` flag
    default to "required".  Returns False (not ready) when a required child id
    is missing/unparseable, the row is not yet persisted, or any required child
    is still non-terminal — i.e. a conservative "not ready to finalize" answer.
    """
    qual = version.last_qualification or {}
    smoketest_required = bool(qual.get("smoketest_enabled", True))
    audit_task_id_raw = qual.get("audit_task_id")
    smoketest_task_id_raw = qual.get("smoketest_task_id")

    if not audit_task_id_raw or (smoketest_required and not smoketest_task_id_raw):
        logger.warning(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d has pending "
            "qualification but missing required child task ids "
            "(audit=%r smoke=%r smoketest_required=%s) — not ready",
            version.version_number,
            audit_task_id_raw,
            smoketest_task_id_raw,
            smoketest_required,
        )
        return False

    try:
        audit_task_id = uuid.UUID(str(audit_task_id_raw))
        smoketest_task_id = (
            uuid.UUID(str(smoketest_task_id_raw))
            if smoketest_required and smoketest_task_id_raw
            else None
        )
    except (ValueError, AttributeError):
        logger.warning(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d has unparseable "
            "child task ids (%r, %r) — not ready",
            version.version_number,
            audit_task_id_raw,
            smoketest_task_id_raw,
        )
        return False

    audit_task: Task | None = await session.get(Task, audit_task_id)
    smoketest_task: Task | None = (
        await session.get(Task, smoketest_task_id)
        if smoketest_task_id is not None
        else None
    )

    if audit_task is None or (smoketest_required and smoketest_task is None):
        logger.debug(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d required child "
            "tasks not yet persisted — not ready",
            version.version_number,
        )
        return False

    if audit_task.status not in _TERMINAL_STATUSES or (
        smoketest_required
        and smoketest_task is not None
        and smoketest_task.status not in _TERMINAL_STATUSES
    ):
        # A required child is still running — not time to finalize yet.
        return False

    logger.info(
        "[RELEASE_QUALIFIER] finalize-gate: required children terminal for "
        "version v%d (audit=%s/%s smoke=%s)",
        version.version_number,
        audit_task.id,
        audit_task.status,
        f"{smoketest_task.id}/{smoketest_task.status}"
        if smoketest_task is not None
        else "disabled",
    )
    return True


async def _maybe_reconcile_after_promotion_conflict(
    version_id: uuid.UUID,
    result: dict[str, Any] | None,
    *,
    session_factory=None,
) -> None:
    """Best-effort: when a promotion failed on a merge CONFLICT, kick off a
    ``main → automain`` reconciliation so the version self-heals.

    A promotion conflict (``automain → main`` will not merge) always means
    ``main`` advanced past ``automain`` with conflicting content — a back-merge
    that should have happened did not.  Triggering the reconciler resolves
    ``main`` into ``automain`` (raising a conflict-resolution plan when the
    merge itself conflicts); once ``automain`` absorbs ``main`` the next
    qualify cycle promotes cleanly instead of dead-ending in stabilize.

    Runs AFTER the dedicated finalize session has committed and closed — never
    under the ``ReleaseVersion`` row lock, and never inside the dedicated
    session whose commit semantics ``promote()`` depends on.  A failure here
    never propagates: the promotion-failure record is already committed, and
    the periodic branch reconciler is the backstop if this best-effort call
    does not land.  ``session_factory`` is threaded through for test injection.
    """
    if not result or result.get("status") != "promotion_failed":
        return
    if not result.get("conflict"):
        return
    try:
        # Lazy import — release_qualifier <-> hotfix_backmerge is otherwise circular.
        from app import hotfix_backmerge

        factory = session_factory or AsyncSessionLocal
        async with factory() as session:
            version = await session.get(release_version.ReleaseVersion, version_id)
            if version is None:
                return
            recon = await hotfix_backmerge.reconcile_main_into_automain(
                session, version.project_id
            )
            await session.commit()
            logger.info(
                "[RELEASE_QUALIFIER] promotion of v%d conflicted — triggered a "
                "main -> automain reconciliation: %s",
                version.version_number,
                recon.get("status"),
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; the periodic reconciler backstops
        logger.warning(
            "[RELEASE_QUALIFIER] post-promotion-conflict reconciliation failed "
            "for version %s (non-fatal — periodic reconciler will retry): %s",
            version_id,
            exc,
        )


async def _finalize_in_dedicated_session(
    version_id: uuid.UUID,
    *,
    session_factory=None,
    timeout_now: datetime | None = None,
) -> dict[str, Any] | None:
    """Run the finalize→promote chain for ``version_id`` on a DEDICATED session.

    This is the single chokepoint through which BOTH the task-completion path
    (``maybe_finalize_qualification``) and the reaper sweep
    (``sweep_stuck_qualifications``) reach ``_finalize_qualification`` /
    ``promote()``.

    **Why a dedicated session.**  ``promote()`` merges to production ``main``
    and issues an intermediate ``session.commit()`` to release the
    ``ReleaseVersion`` row lock before its 30-120s of GitHub I/O.  If that
    commit ran on a caller's session it would prematurely commit the caller's
    UNRELATED dirty work (a completed task's terminal status, its TaskHistory /
    Artifacts / remediation+regression records) — destroying the completion
    handler's atomicity.  So this function opens its OWN session via
    ``AsyncSessionLocal``; the finalize chain has nothing else dirty in it, so
    ``promote()``'s commit only ever flushes qualifier state.  ``session_factory``
    is injectable for tests (defaults to ``AsyncSessionLocal``).

    **Why it cannot self-deadlock.**  The dedicated session takes
    ``SELECT ... FOR UPDATE`` on the ``ReleaseVersion`` row.  The
    task-completion caller holds a lock on the *Task* row, not the
    ``ReleaseVersion`` row; the reaper sweep takes NO lock on the version row
    before calling here.  So a fresh session can never block on a version-row
    lock held by the same logical caller — concern (c).

    **Why concurrent calls cannot double-promote (concern d).**  This function
    ``SELECT ... FOR UPDATE``-locks the version row at the very top and holds it
    across the readiness check and the decision.  For the NON-promote outcomes
    (``dry_run`` / ``not_promoted``) the lock is held until commit, so a second
    concurrent call blocks until the first commits, then re-reads
    ``last_qualification.status``, sees it is no longer ``"pending"``, and bails
    at the pending guard — the pending guard alone serializes those paths.

    The PROMOTE path is different and the pending guard is NOT what protects it:
    ``promote()`` issues an intermediate ``commit()`` that releases this row
    lock BEFORE its 30-120s of GitHub I/O, and at that commit
    ``last_qualification.status`` is still ``"pending"`` (it is only moved to
    ``promoted`` / ``promotion_failed`` at the very end).  So a genuinely
    concurrent second caller can acquire the freed lock, pass the pending guard,
    and enter its own ``promote()``.  What actually serializes two overlapping
    promotions is the idempotency machinery (``_candidate_already_on_main`` /
    ``_promotion_pr_landed`` — an already-landed candidate routes straight to
    success without a second merge) PLUS the ``version.status != "open"``
    re-check that BOTH ``_record_promotion_success`` and
    ``_record_promotion_failure`` perform under the re-lock before the final
    state write (the loser sees ``status="promoted"`` and bails as
    already-promoted, opening no duplicate vN+1 and writing no contradictory
    ``last_qualification``).

    Returns the ``_finalize_qualification`` / ``_force_finalize_*`` result dict
    when a finalize ran, or ``None`` when the version was not ready / already
    finalized / gone.

    ``timeout_now`` — when set (the reaper sweep passes ``_utcnow()``), the
    age-bound qualification-timeout backstop (audit H2) is evaluated under the
    lock and takes priority over the children-terminal path.  When ``None``
    (the normal task-completion path) only the children-terminal path applies.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            result = await _finalize_locked_version(session, version_id, timeout_now)
        except Exception:
            # Roll back this dedicated session's partial work before unwinding.
            # In production each dedicated session is independent and the
            # ``async with`` __aexit__ would roll back anyway; the explicit
            # rollback makes the contract obvious and leaves the session clean
            # for any caller (e.g. the sweep) that reuses the same factory for
            # the next version.
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] finalize: rollback failed for version %s",
                    version_id,
                )
            raise

    # The dedicated finalize session has now committed + closed.  When the
    # finalize ran a promotion that failed on a merge CONFLICT, kick off a
    # main -> automain reconciliation HERE — outside the dedicated session and
    # the ReleaseVersion row lock — so the version self-heals instead of
    # dead-ending in stabilize (automain/LTS promotion-conflict recovery).
    # Best-effort: a failure never propagates and never alters ``result``.
    await _maybe_reconcile_after_promotion_conflict(
        version_id, result, session_factory=session_factory
    )
    return result


async def _finalize_locked_version(
    session: AsyncSession,
    version_id: uuid.UUID,
    timeout_now: datetime | None,
) -> dict[str, Any] | None:
    """Lock + finalize one version within an already-open dedicated session.

    The body of ``_finalize_in_dedicated_session`` (which owns the session and
    its rollback-on-error contract).  Split out so the locking + decision logic
    reads as a flat sequence.  Commits the dedicated session on every path that
    actually wrote something; returns ``None`` for the read-only no-op paths.
    """
    # Lock the version row — the single serialization point for the whole
    # finalize→promote critical section (concern d).
    version: release_version.ReleaseVersion | None = (
        (
            await session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version_id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )

    if version is None:
        logger.debug(
            "[RELEASE_QUALIFIER] finalize: version %s not found — skipping",
            version_id,
        )
        return None

    # Defensive (audit R4 MEDIUM): only an ``open`` version can be
    # mid-qualification — a ``promoted`` / ``abandoned`` version is terminal.
    # Checked under the FOR UPDATE lock, so authoritative.
    if version.status != "open":
        logger.info(
            "[RELEASE_QUALIFIER] finalize: version v%d is status=%s (not "
            "open) — skipping finalize",
            version.version_number,
            version.status,
        )
        return None

    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        # Not yet started, or already finalized (possibly by the call that held
        # this lock just before us) — idempotent no-op.  For the non-promote
        # outcomes (dry_run / not_promoted) this guard fully serializes
        # concurrent finalize calls: the loser blocked on the row lock and sees
        # the winner's non-pending status here.  It does NOT cover an
        # overlapping promote — promote() commits with the status still
        # "pending", so a concurrent caller can pass this guard mid-promotion;
        # the promote path is instead serialized by the idempotency checks +
        # the status!="open" re-check in _record_promotion_{success,failure}
        # (see this module's _finalize_locked_version docstring).
        return None

    # --- Path 1: age-bound qualification timeout (reaper sweep only) --------
    # Checked first and child-state-independent (audit H2).
    if timeout_now is not None:
        started_at = _parse_started_at(qual)
        timeout_clock = started_at
        if timeout_clock is None:
            # updated_at is non-null + onupdate-stamped; the others are further
            # fallbacks.  A corrupt started_at must never defeat the backstop
            # (audit R4-H5).
            timeout_clock = (
                version.updated_at or version.phase_changed_at or version.opened_at
            )
            if timeout_clock is not None:
                logger.warning(
                    "[RELEASE_QUALIFIER] finalize: version v%d has a "
                    "missing/unparseable started_at (%r) — using updated_at "
                    "as the timeout clock so the backstop still fires",
                    version.version_number,
                    qual.get("started_at"),
                )
        if timeout_clock is not None:
            age_seconds = (timeout_now - timeout_clock).total_seconds()
            if age_seconds > _MAX_QUALIFICATION_DURATION_SECONDS:
                await _force_finalize_timed_out_qualification(
                    session, version, age_seconds
                )
                await session.commit()
                return {
                    "status": "not_promoted",
                    "blocker": "qualification_timeout",
                }

    # --- Path 2: all required children terminal -----------------------------
    if not await _required_children_terminal(session, version):
        return None

    # Finalize on the dedicated session.  _finalize_qualification may call
    # promote(), whose intermediate commit + re-lock are now safe: this session
    # holds only qualifier state.
    result = await _finalize_qualification(session, version)
    await session.commit()
    return result


async def maybe_finalize_qualification(
    completed_task: Task,
    *,
    session_factory=None,
) -> dict[str, Any] | None:
    """Called from a task-completion handler whenever ANY task terminates.

    This is the coordination glue between ``run_qualification`` (which spawns
    the two qualifier children and returns immediately) and
    ``_finalize_qualification`` (which computes the composite and decides).

    Fast path: if ``completed_task.context`` has no ``release_version_id``,
    return ``None`` immediately.  The overwhelming majority of tasks are not
    qualifier children and this must be a cheap O(1) no-op — it does no I/O and
    opens no session.

    When a ``release_version_id`` is present, this delegates to
    ``_finalize_in_dedicated_session``, which:

      1. Opens its OWN dedicated DB session (``AsyncSessionLocal``) — fully
         decoupled from the caller's session.  This is the fix for the
         transaction-coupling defect: ``promote()``'s intermediate
         ``session.commit()`` (which releases the row lock before its GitHub
         I/O) would otherwise commit the completion handler's unrelated dirty
         work (the completed task's terminal status, TaskHistory, Artifacts,
         remediation/regression records).  On the dedicated session that
         commit only ever flushes qualifier state.

      2. Takes ``SELECT ... FOR UPDATE`` on the ``ReleaseVersion`` row — the
         serialization point for the non-promote paths.  The ``release_audit``
         and qualifier ``smoketest`` children can terminate
         near-simultaneously, and a reaper sweep can run concurrently; for a
         ``dry_run`` / ``not_promoted`` outcome the lock makes a second finalize
         attempt block until the first commits, then bail at the pending guard.
         A PROMOTE is the exception: ``promote()`` releases this lock via an
         intermediate commit (with the status still ``"pending"``) before its
         GitHub I/O, so a concurrent caller can pass the pending guard
         mid-promotion — overlapping promotions are instead serialized by the
         idempotency checks plus the ``status != "open"`` re-check in
         ``_record_promotion_success`` / ``_record_promotion_failure``.  A
         fresh session cannot self-deadlock: the caller holds the *Task* row
         lock, not this ``ReleaseVersion`` row lock.

      3. No-ops when the version is gone, not ``open``, or no longer
         ``last_qualification.status == "pending"`` (idempotent), or when a
         required qualifier child is still non-terminal.

      4. Otherwise runs ``_finalize_qualification`` (→ ``promote()`` when the
         composite clears + ``promotion_enabled``) and commits the dedicated
         session.

    The caller's session is NEVER touched — neither read nor committed.
    ``session_factory`` is injectable for tests (defaults to
    ``AsyncSessionLocal``).

    Returns ``None`` when this task is not a qualifier child, when a sibling is
    still running, or when the version is already finalized.  Returns the
    ``_finalize_qualification`` result dict when finalization runs.
    """
    context = completed_task.context or {}
    version_id_raw = context.get("release_version_id")
    if not version_id_raw:
        # Fast path — not a qualifier child.  Must be O(1): no session, no I/O.
        return None

    # Parse the version id (a stable identifier — captured before any session
    # is opened so the dedicated session re-loads it under its own lock).
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

    return await _finalize_in_dedicated_session(
        version_id, session_factory=session_factory
    )


# ---------------------------------------------------------------------------
# sweep_stuck_qualifications — defense-in-depth reaper.
# ---------------------------------------------------------------------------


def _parse_started_at(qual: dict[str, Any]) -> datetime | None:
    """Parse ``last_qualification["started_at"]`` to a datetime, or None.

    ``started_at`` is an ISO-8601 string stamped by ``run_qualification``.
    Returns None when it is missing or unparseable.

    Audit R4-H5: a None return must NOT defeat the qualification-timeout
    backstop — ``_finalize_locked_version`` falls back to a real row timestamp
    (``version.updated_at``) when this returns None, so a corrupt
    ``started_at`` string can never wedge a qualification forever.  The
    fallback lives in the caller (not here) because this helper has no access
    to the ``ReleaseVersion`` row.
    """
    raw = qual.get("started_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _force_finalize_timed_out_qualification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    age_seconds: float,
) -> None:
    """Force-finalize a qualification that has been ``pending`` too long.

    Audit H2: a qualifier child that heartbeats-but-hangs (or is stuck in a
    recovery/requeue loop) never reaches a terminal state, so neither
    ``maybe_finalize_qualification`` nor the both-children-terminal branch of
    the sweep ever fires and the version wedges at ``pending`` forever.  This
    is the unconditional escape hatch — called regardless of child state once
    the qualification exceeds ``_MAX_QUALIFICATION_DURATION_SECONDS``.

    Records ``last_qualification.status = "not_promoted"`` with
    ``blocker = "qualification_timeout"``, sends the version back to the
    ``stabilize`` phase (so the next strategic cycle re-targets it), and raises
    an ``Alert`` so the stuck qualification is operator-visible.  The version
    stays ``open`` — a timed-out qualification only ever blocks, never
    promotes, which is always safe.  The caller commits the session.
    """
    qual = dict(version.last_qualification or {})
    age_minutes = age_seconds / 60.0
    detail = (
        f"qualification pending {age_minutes:.0f} min "
        f"(> {_MAX_QUALIFICATION_DURATION_SECONDS // 60} min limit) — a "
        f"qualifier child never reached a terminal state"
    )
    record = {
        **qual,
        "status": "not_promoted",
        "blocker": "qualification_timeout",
        "detail": detail,
        "finalized_at": _utcnow().isoformat(),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number} timed out "
            f"({age_minutes:.0f} min) — returning to stabilize"
        ),
    )
    session.add(version)

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=(f"Release qualification v{version.version_number} timed out"),
            message=(
                f"Qualification for release v{version.version_number} was "
                f"stuck pending for {age_minutes:.0f} minutes — a qualifier "
                f"child (release_audit / qualifier-smoketest) never reached a "
                f"terminal state. Force-finalized as a blocked qualification "
                f"(blocker=qualification_timeout); the version returns to the "
                f"stabilize phase for the next strategic cycle."
            ),
        )
    )
    logger.warning(
        "[RELEASE_QUALIFIER] sweep: version v%d qualification timed out "
        "(pending %.0f min) — force-finalized as qualification_timeout",
        version.version_number,
        age_minutes,
    )


async def _check_version_calcification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Post a deduplicated calcification alert when a version is stuck.

    A version calcifies when it has accumulated ``_MIN_CALCIFICATION_ATTEMPTS``
    or more qualification attempts in the stabilize phase, all failing on the
    same hard blocker, with no indication it will self-heal.

    CRITICAL: a version whose last qualification is ``status="dry_run"`` is a
    PASSING candidate held back only by ``promotion_enabled=False`` — that is
    healthy operation and must NOT trigger this alert.  Only repeated hard-blocker
    failures (``status="not_promoted"`` with a non-None blocker) qualify.

    Deduplication: compares the version's durable
    ``calcification_alerted_blocker`` column against the current blocker.  If
    they match, the alert has already been posted for this stall and is
    skipped; a new alert is posted only when the blocker changes or the field
    is NULL.  Storing the dedup anchor on the version (rather than relying on
    Alert-row existence) keeps the once-per-stall guarantee even after an
    operator clears alerts.

    Does NOT commit the session — the caller must commit after a ``True`` return.
    """
    if version.status != "open" or version.phase != "stabilize":
        return False

    attempts = version.qualification_attempts or 0
    if attempts < _MIN_CALCIFICATION_ATTEMPTS:
        return False

    qual = version.last_qualification or {}
    qual_status = qual.get("status")
    blocker = qual.get("blocker")

    # A dry-run hold is healthy — the version passed and is waiting for
    # promotion_enabled.  Do not alert.
    if qual_status == "dry_run":
        return False

    # Only alert for hard-blocker failures (not below-threshold near-misses,
    # which have blocker=None and may self-correct without intervention).
    if qual_status != "not_promoted" or not blocker:
        return False

    # Durable dedup: the version remembers the blocker it last alerted on.
    # Same blocker → already notified for this stall; skip.
    if version.calcification_alerted_blocker == blocker:
        return False

    alert_title = f"Release v{version.version_number} calcifying: {blocker}"

    age_days: int = 0
    ref_time = version.phase_changed_at or version.opened_at
    if ref_time is not None:
        age_days = max(0, (_utcnow() - ref_time).days)

    blocker_label = _BLOCKER_LABELS.get(blocker, blocker)
    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=alert_title,
            message=(
                f"Release v{version.version_number} has been stuck in the "
                f"stabilize phase for {age_days} day(s) with {attempts} "
                f"qualification attempt(s), each blocked by: {blocker_label}. "
                f"The integration trunk may be drifting from the deployed "
                f"branch. Resolve the blocker to restore promotion."
            ),
        )
    )
    await post_version_calcification_message(
        session,
        version.project_id,
        version_number=version.version_number,
        version_id=str(version.id),
        qualification_attempts=attempts,
        blocker=blocker,
        blocker_label=blocker_label,
        age_days=age_days,
    )
    # Record the durable dedup anchor so subsequent sweeps don't re-alert for
    # the same stall (survives an operator clearing the Alert row).
    version.calcification_alerted_blocker = blocker
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] calcification alert for v%d: %d attempts "
        "blocked by %s over %d day(s)",
        version.version_number,
        attempts,
        blocker,
        age_days,
    )
    return True


async def _check_version_build_stall(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Post a deduplicated build-stall alert when a version sits too long in
    the ``build`` phase without advancing to ``stabilize``.

    This is the sibling backstop to ``_check_version_calcification``: that guard
    covers a version repeatedly failing qualification in the ``stabilize`` phase,
    while this one covers the OTHER end of the lifecycle.  ``build → stabilize``
    is entirely agent-driven (the strategic agent must emit a
    ``version_phase_decision``); there is no cadence or automatic backstop, so a
    conservative or distracted strategic agent can leave an open version
    accumulating merged tasks in ``build`` for many cycles, never reaching
    qualify/promote — the lifecycle silently failing to advance.

    Fires when an open ``build``-phase version's phase age (days since the build
    phase started) OR its merged-task count since the phase started crosses the
    conservative ``_is_build_stalled`` threshold.  Phase age derives from
    ``phase_changed_at`` falling back to ``opened_at`` (a freshly-opened build
    version has ``phase_changed_at = None``) — exactly as
    ``_check_version_calcification`` derives its age.  The merged-task count uses
    ``release_version.count_merged_since`` from that same build-phase-start
    reference time.

    Deduplication: compares the version's durable ``build_stall_alerted_at``
    column against the current build-phase-start reference time.  If they match,
    the alert has already been posted for this stall and is skipped; a new alert
    is posted only when the reference differs (or the field is NULL).  Because a
    version can move ``build → stabilize → build`` again (each transition
    re-stamps ``phase_changed_at``), storing the reference time lets the dedup
    reset naturally for a fresh build phase while never re-firing for the same
    stall.

    All messaging is best-effort wrapped so it can NEVER break the qualifier
    sweep.  Does NOT commit the session — the caller must commit after a ``True``
    return.
    """
    if version.status != "open" or version.phase != "build":
        return False

    ref_time = version.phase_changed_at or version.opened_at
    if ref_time is None:
        return False

    age_days = max(0, (_utcnow() - ref_time).days)

    try:
        merged_count = await release_version.count_merged_since(
            session, version.project_id, ref_time
        )
    except Exception as exc:  # noqa: BLE001 — a count failure must not break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] build-stall merged-task count failed for v%d "
            "(non-fatal): %s",
            version.version_number,
            exc,
        )
        merged_count = 0

    if not _is_build_stalled(age_days, merged_count):
        return False

    # Durable dedup: the version remembers the build-phase reference time it
    # last alerted on.  Same reference → already notified for this stall; skip.
    if version.build_stall_alerted_at == ref_time:
        return False

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Release v{version.version_number} stalled in build phase",
            message=(
                f"Release v{version.version_number} has been in the build phase "
                f"for {age_days} day(s) with {merged_count} merged task(s) and "
                f"has not advanced to stabilize. build→stabilize is agent-driven "
                f"with no automatic cadence — the strategic agent may need to "
                f"emit a version_phase_decision to move it to stabilize so it can "
                f"progress toward qualification and promotion."
            ),
        )
    )
    # post_version_build_stalled_message is itself defensive, but wrap the whole
    # messaging block so a transient failure can never abort the sweep.
    try:
        await post_version_build_stalled_message(
            session,
            version.project_id,
            version_number=version.version_number,
            version_id=str(version.id),
            age_days=age_days,
            tasks_merged=merged_count,
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post build-stall message for v%d "
            "(non-fatal): %s",
            version.version_number,
            exc,
        )

    # Record the durable dedup anchor so subsequent sweeps don't re-alert for
    # the same stall (survives an operator clearing the Alert row).
    version.build_stall_alerted_at = ref_time
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] build-stall alert for v%d: %d day(s), %d merged "
        "task(s) in build phase without advancing",
        version.version_number,
        age_days,
        merged_count,
    )
    return True


async def _check_version_stabilize_stall(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Post a deduplicated stabilize-stall alert when a version sits too long
    in the ``stabilize`` phase without making progress toward qualification.

    This is the third lifecycle backstop alongside ``_check_version_build_stall``
    and ``_check_version_calcification``:
      * build-stall: ``build`` phase too long without advancing to stabilize.
      * stabilize-stall (this): ``stabilize`` phase too long without a
        qualify_request — covers zero-attempt or soft-near-miss stalls that the
        calcification guard ignores (it requires repeated hard-blocker failures).
      * calcification: repeated hard-blocker failures blocking the qualify step.

    After this backstop lands, no lifecycle transition (build→stabilize,
    stabilize→qualify, qualify→promote) can stall unseen.

    Fires when an open stabilize-phase version's phase age OR its merged-task
    count crosses the conservative threshold AND neither the calcification guard
    (hard-blocker stall already covered) nor a dry-run hold (healthy pass
    waiting for promotion_enabled) applies.

    Deduplication: compares the version's durable ``stabilize_stall_alerted_at``
    column against the current stabilize-phase reference time.  Same reference →
    already notified for this stall; new reference (phase re-entered, so
    phase_changed_at re-stamped) → fresh stall can alert again.

    All messaging is best-effort wrapped so it can NEVER break the qualifier
    sweep.  Does NOT commit the session — the caller must commit after a True return.
    """
    if version.status != "open" or version.phase != "stabilize":
        return False

    qual = version.last_qualification or {}
    qual_status = qual.get("status")

    # A dry-run hold is a passing candidate waiting for promotion_enabled —
    # healthy, not stalled.
    if qual_status == "dry_run":
        return False

    # A version with repeated hard-blocker failures is covered by the
    # calcification alert — don't double-alert on the same stall.
    if qual_status == "not_promoted" and qual.get("blocker"):
        return False

    ref_time = version.phase_changed_at or version.opened_at
    if ref_time is None:
        return False

    age_days = max(0, (_utcnow() - ref_time).days)

    try:
        merged_count = await release_version.count_merged_since(
            session, version.project_id, ref_time
        )
    except Exception as exc:  # noqa: BLE001 — a count failure must not break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] stabilize-stall merged-task count failed for v%d "
            "(non-fatal): %s",
            version.version_number,
            exc,
        )
        merged_count = 0

    if not (
        age_days >= _STABILIZE_STALL_AGE_DAYS
        or merged_count >= _STABILIZE_STALL_MERGED_TASKS
    ):
        return False

    # Durable dedup: the version remembers the stabilize-phase reference time it
    # last alerted on.  Same reference → already notified for this stall; skip.
    if version.stabilize_stall_alerted_at == ref_time:
        return False

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Release v{version.version_number} stalled in stabilize phase",
            message=(
                f"Release v{version.version_number} has been in the stabilize "
                f"phase for {age_days} day(s) with {merged_count} merged task(s) "
                f"but has not been qualified. stabilize→qualify requires a "
                f"qualify_request from the strategic agent — consider requesting "
                f"qualification now or addressing what is preventing readiness."
            ),
        )
    )
    try:
        await post_version_stabilize_stalled_message(
            session,
            version.project_id,
            version_number=version.version_number,
            version_id=str(version.id),
            age_days=age_days,
            tasks_merged=merged_count,
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post stabilize-stall message for v%d "
            "(non-fatal): %s",
            version.version_number,
            exc,
        )

    # Record the durable dedup anchor so subsequent sweeps don't re-alert for
    # the same stall (survives an operator clearing the Alert row).
    version.stabilize_stall_alerted_at = ref_time
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] stabilize-stall alert for v%d: %d day(s), %d merged "
        "task(s) in stabilize phase without qualifying",
        version.version_number,
        age_days,
        merged_count,
    )
    return True


async def _check_version_promotion_readiness(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Notify operator once when the dry-run track record first reaches readiness.

    Fires exactly once per not-ready → ready transition while promotion is
    disabled; resets the dedup flag when readiness lapses so recovery can
    notify again.  Messaging is best-effort — failure never breaks the sweep.
    Does NOT commit — caller commits on True return.
    """
    if version.status != "open":
        return False
    if bool(_runtime_config.get("promotion_enabled")):
        return False  # promotion already on → operator acted

    try:
        readiness = await compute_promotion_readiness(session, version.project_id)
    except Exception as exc:  # noqa: BLE001 — failure must not break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] promotion-readiness check failed for v%d "
            "(non-fatal): %s",
            version.version_number,
            exc,
        )
        return False

    if not readiness.get("ready"):
        # Reset dedup flag so the next not-ready→ready recovery can notify.
        # Return True when we actually clear the flag so the caller commits the
        # reset — otherwise the sweep (which only commits on a True return) rolls
        # it back on session close, leaving the flag True and suppressing the
        # next ready re-notification.
        if version.promotion_ready_notified:
            version.promotion_ready_notified = False
            session.add(version)
            return True
        return False

    if version.promotion_ready_notified:
        return False  # already notified for this ready window

    try:
        await post_version_promotion_readiness_message(
            session,
            version.project_id,
            version_number=version.version_number,
            version_id=str(version.id),
            verdicts_evaluated=readiness["verdicts_evaluated"],
            would_promote_rate=readiness["would_promote_rate"],
            consecutive_clean_passes=readiness["consecutive_clean_passes"],
            reason=readiness["reason"],
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post promotion-readiness message "
            "for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )
        return False

    version.promotion_ready_notified = True
    session.add(version)
    logger.info(
        "[RELEASE_QUALIFIER] promotion-readiness notification sent for v%d: "
        "%d clean passes, rate %.2f, %d verdict(s)",
        version.version_number,
        readiness["consecutive_clean_passes"],
        readiness["would_promote_rate"],
        readiness["verdicts_evaluated"],
    )
    return True


async def sweep_stuck_qualifications(
    session: AsyncSession,
    *,
    session_factory=None,
) -> int:
    """Finalize qualifications stuck ``pending`` past the normal completion path.

    ``maybe_finalize_qualification`` is the normal path: a child terminating
    fires a completion handler that finalizes the version once its sibling is
    also terminal.  But if a child dies *without* its handler running — handler
    exception, orchestrator restart between the child's terminal write and the
    handler, a child that was force-cancelled out of band — the version is left
    with ``last_qualification.status == "pending"`` forever even though its
    children have finished.

    This sweep is the defense-in-depth reaper for that case.  For every
    ``ReleaseVersion`` whose ``last_qualification.status == "pending"`` it
    handles two situations:

      * **Both required children terminal** (``COMPLETED`` / ``FAILED`` /
        ``CANCELLED``) — finalizes the version.  The qualifier-smoketest child
        is optional (audit H3): when the qualifying run recorded
        ``smoketest_enabled == False`` only the audit child gates this branch.

      * **Pending longer than ``_MAX_QUALIFICATION_DURATION_SECONDS``** (audit
        H2) — force-finalizes the qualification as blocked (blocker
        ``qualification_timeout``) regardless of child state and raises an
        Alert.  This is the escape hatch for a child that heartbeats-but-hangs
        and so never reaches a terminal state.

    **Per-version session isolation (replaces the old per-version commit —
    audit H5).**  ``session`` is used ONLY for the read-only scan that lists the
    candidate version ids; the sweep never writes through it.  Each version is
    then finalized via ``_finalize_in_dedicated_session``, which opens its OWN
    session, locks that one version, finalizes it, and commits — in complete
    isolation.  A failure (or a real GitHub merge) on version B therefore
    cannot roll back version A's already-committed finalization: they were
    never in the same transaction.  This is strictly stronger than the old
    "commit after each version on the shared session" pattern and removes the
    shared-session rollback/expire bookkeeping entirely.

    Idempotent and safe to call repeatedly: a version already finalized (or not
    actually ready) is no-op'd by ``_finalize_in_dedicated_session``'s own
    under-lock guards.  Returns the number of versions actually finalized.
    """
    # Only ``open`` versions can be mid-qualification — a promoted/abandoned
    # version is terminal.  This keeps the scan bounded (≈ one open version per
    # project) and avoids a JSONB-path WHERE clause that differs across the
    # Postgres / SQLite-test dialects; the pending filter is applied inside the
    # per-version dedicated session under the row lock.
    open_versions = list(
        (
            await session.execute(
                select(release_version.ReleaseVersion).where(
                    release_version.ReleaseVersion.status == "open"
                )
            )
        )
        .scalars()
        .all()
    )
    # Snapshot just the IDs — each version is re-fetched under its own
    # FOR UPDATE lock in its own dedicated session, so the id is all that is
    # carried out of the scan.
    version_ids = [v.id for v in open_versions]

    now = _utcnow()
    finalized = 0
    for version_id in version_ids:
        # Each version finalizes in its own dedicated session (concern d + the
        # audit-H5 isolation guarantee): a failure here is contained and cannot
        # touch another version's already-committed work.
        try:
            result = await _finalize_in_dedicated_session(
                version_id,
                session_factory=session_factory,
                timeout_now=now,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: failed to finalize stuck "
                "qualification for version %s",
                version_id,
            )
            continue
        if result is not None:
            finalized += 1

    if finalized:
        logger.info(
            "[RELEASE_QUALIFIER] sweep finalized %d stuck qualification(s)",
            finalized,
        )

    # Lifecycle-stall checks: alert when an open version is stuck at either end
    # of the lifecycle — calcifying in stabilize (repeated hard-blocker
    # failures) or stalled in build (sitting too long without advancing).  The
    # two checks are mutually exclusive per version (each early-returns on the
    # wrong phase) but both run so whichever applies fires.  Each version gets
    # its own dedicated session (same isolation pattern as the finalize loop
    # above) so a messaging failure on one version can't affect another.
    factory = session_factory or AsyncSessionLocal
    for version in open_versions:
        try:
            async with factory() as alert_session:
                v = await alert_session.get(release_version.ReleaseVersion, version.id)
                if v is None:
                    continue
                alerted = await _check_version_calcification(alert_session, v)
                alerted = await _check_version_build_stall(alert_session, v) or alerted
                alerted = (
                    await _check_version_stabilize_stall(alert_session, v) or alerted
                )
                alerted = (
                    await _check_version_promotion_readiness(alert_session, v)
                    or alerted
                )
                if alerted:
                    await alert_session.commit()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] lifecycle-stall check failed for version %s",
                version.id,
            )

    return finalized


# ---------------------------------------------------------------------------
# promote — open + merge the automain → main PR.
# ---------------------------------------------------------------------------


async def _candidate_already_on_main(
    token: str,
    owner: str,
    repo: str,
    candidate_sha: str,
    version: release_version.ReleaseVersion,
) -> bool:
    """Return True iff ``candidate_sha`` is already reachable from ``main``.

    Audit R4-C1 / R4-H1 — the single idempotency primitive for ``promote()``.
    A promotion is a multi-step GitHub operation (create release branch, open
    PR, merge).  If the process crashes between the merge LANDING on ``main``
    and the DB commit that records ``status="promoted"``, the next finalize /
    sweep re-enters ``promote()`` with ``last_qualification`` still ``pending``.
    Without this check the retry would (a) re-merge a closed/merged PR, or
    (b) hit a 405 "already merged" and misclassify a SUCCESSFUL promotion as a
    failure — leaving the DB and GitHub permanently divergent.

    This wraps ``github.is_ancestor_of(candidate_sha, "main")``.  It answers
    the question that covers BOTH retry sub-cases uniformly: "has this exact
    candidate commit already landed on main?".

    Returns ``False`` (NOT True) when the ancestry check could not be made
    (``is_ancestor_of`` returned ``None`` — API error, ref missing): a
    transient GitHub failure must never be read as "already promoted", which
    would skip a real merge.  A genuinely-already-merged candidate will be
    re-detected on the next retry once GitHub is reachable again.
    """
    try:
        ancestry = await github.is_ancestor_of(
            token,
            owner,
            repo,
            candidate_sha,
            "main",
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a check failure must not abort promotion
        logger.warning(
            "[RELEASE_QUALIFIER] promote: idempotency ancestry check raised "
            "for v%d candidate %s (%s) — treating as not-yet-merged",
            version.version_number,
            (candidate_sha or "")[:8],
            exc,
        )
        return False
    if ancestry is None:
        # Indeterminate — degrade to "not merged" so a real merge still runs.
        logger.warning(
            "[RELEASE_QUALIFIER] promote: idempotency ancestry check for v%d "
            "candidate %s was indeterminate — treating as not-yet-merged",
            version.version_number,
            (candidate_sha or "")[:8],
        )
        return False
    return ancestry


async def _promotion_pr_landed(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    candidate_sha: str,
    version: release_version.ReleaseVersion,
) -> bool:
    """Return True iff promotion PR ``pr_number`` has in fact already merged.

    Concern 3 — ``github.merge_pull_request`` returns ``{"merged": False}`` for
    BOTH a genuine merge block (failing checks / required reviews / conflict)
    AND an already-merged PR (GitHub's merge endpoint returns 405 once a PR has
    already been merged).  ``promote()`` must NOT record an already-merged PR as
    a ``merge_blocked`` failure — that permanently diverges the DB from GitHub.

    This disambiguates with TWO signals, strongest first:

      1. **The PR's own ``merged`` boolean** (``get_pull_request_merge_state``).
         This is a property of the PR resource itself; GitHub flips it to
         ``true`` synchronously the moment a merge completes.  It is therefore
         the AUTHORITATIVE answer and — crucially — is NOT subject to the
         eventual-consistency lag the ``GET /compare`` ancestry API can exhibit
         immediately after a merge.  A genuinely-successful promotion that the
         ancestry API would still report as ``diverged`` for a few seconds is
         correctly seen as merged here, so a real success is never misrecorded
         as ``merge_blocked``.

      2. **Ancestry fallback** (``_candidate_already_on_main``).  Used ONLY when
         the PR-state query is indeterminate (API error, PR not found).  Better
         than nothing: a crash-retry against a PR that merged-then-was-GC'd may
         no longer be queryable, but the candidate is still on ``main``.

    Returns ``True`` only on a positive merge signal from either tier; ``False``
    when both are negative or indeterminate — so a genuine merge block (the PR
    is open + unmerged, the candidate is not on main) is still correctly
    reported as not-landed and recorded as the failure it is.
    """
    try:
        pr_merged = await github.get_pull_request_merge_state(
            token, owner, repo, pr_number, project_id=str(version.project_id)
        )
    except Exception as exc:  # noqa: BLE001 — a state check must not abort promotion
        logger.warning(
            "[RELEASE_QUALIFIER] promote: PR-state merge check raised for "
            "PR #%s (v%d) (%s) — falling back to the ancestry check",
            pr_number,
            version.version_number,
            exc,
        )
        pr_merged = None

    if pr_merged is True:
        logger.info(
            "[RELEASE_QUALIFIER] promote: PR #%s for v%d reports merged=True on "
            "its own resource — the promotion landed",
            pr_number,
            version.version_number,
        )
        return True
    if pr_merged is False:
        # The PR resource itself says it is NOT merged — this is authoritative.
        # A merged=False merge result on a definitively-unmerged PR is a real
        # merge block; do not consult the lagging ancestry API to second-guess
        # it.
        return False

    # pr_merged is None — the PR state could not be determined.  Fall back to
    # the ancestry check (eventually-consistent, but the best remaining signal).
    logger.warning(
        "[RELEASE_QUALIFIER] promote: PR #%s for v%d merge state "
        "indeterminate — falling back to the candidate-on-main ancestry check",
        pr_number,
        version.version_number,
    )
    if not candidate_sha:
        return False
    return await _candidate_already_on_main(token, owner, repo, candidate_sha, version)


def _idempotent_pr_url(base_record: dict[str, Any] | None) -> str:
    """Resolve the ``pr_url`` to record on an idempotent-success path.

    Concern 5 — the idempotency success paths (candidate already on ``main``;
    PR-creation failed on a retry because the prior attempt's PR already merged)
    have no live PR object of their own to read an ``html_url`` from.  Recording
    a bare ``""`` is misleading — it reads like "there was no PR".

    When the caller threaded the original promotion attempt's PR URL through
    ``base_record["pr_url"]`` (a prior ``promotion_failed`` record on the same
    version carries it), reuse that real URL.  Otherwise return an explicit
    sentinel string so the recorded value is unambiguous rather than blank.
    """
    carried = (base_record or {}).get("pr_url")
    if isinstance(carried, str) and carried:
        return carried
    return "(already merged — no PR opened by this attempt)"


async def _relock_version_for_promotion_write(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    lock_released: bool,
) -> release_version.ReleaseVersion | None:
    """Resolve the version row, holding ``SELECT ... FOR UPDATE``, for the final write.

    Audit R4-H3 — ``promote()`` releases the ``ReleaseVersion`` row lock (via an
    intermediate ``session.commit()``) before its 30-120s of GitHub I/O so a
    concurrent ``run_qualification`` / reaper sweep / strategic completion is
    not stalled behind network calls.  Every terminal state write in
    ``promote()`` therefore goes through this helper.

    Returns the version instance with the row ``FOR UPDATE``-locked, or ``None``
    if the row vanished (project deleted mid-promotion) — the caller treats
    ``None`` as "nothing to write".

    ``lock_released`` makes the two cases EXPLICIT — this is the fix for the
    early-return ``expire()`` defect (concern 4):

      * ``lock_released=True`` — ``promote()`` already issued its intermediate
        commit.  The row lock is gone and the in-memory instance may be stale,
        so the row must be RE-READ and RE-LOCKED.  ``session.expire`` BEFORE the
        ``populate_existing`` SELECT mirrors the canonical re-lock pattern
        (``ci_completion._relock_merge_task`` / ``rollup_management._relock_parent``):
        ``populate_existing=True`` alone has been observed not to reliably
        refresh an instance still in the identity map on async sessions.  An
        ``expire`` here is safe: a just-committed instance has no un-flushed
        mutations to lose (the commit flushed them).

      * ``lock_released=False`` — an early-return failure path BEFORE the
        intermediate commit.  In ``promote()`` only the ``project_not_found``
        return reaches here (it is detected before the commit).  The row is
        STILL locked by this very transaction, and the live instance is already
        the authoritative locked row.  Calling ``session.expire`` here would
        DISCARD any un-flushed in-memory mutation on the version — the bug this
        parameter removes.  So on this path the helper does NOT expire and does
        NOT re-SELECT; it just confirms the row still exists and returns the
        live, already-locked instance for the caller to mutate.
    """
    # Snapshot the PK up front.  In the lock_released=True branch the
    # ``session.expire(version)`` below expires EVERY attribute on the instance
    # (including ``id``); reading ``version.id`` after that point triggers a
    # sync lazy-load, which under the async session raises MissingGreenlet
    # ("greenlet_spawn has not been called").  The reaper-sweep finalize path
    # hit this on every cycle — a passing-but-merge-conflicted promotion could
    # never record its verdict, so the version stuck at status="pending" and the
    # sweep crash-looped.  Holding the id in a local avoids the reload entirely.
    version_id = version.id

    if not lock_released:
        # The row is still locked by this transaction; the live instance is
        # authoritative.  A bare existence check (no expire — would drop
        # pending mutations; no re-SELECT — the lock is already held).
        still_exists = (
            await session.execute(
                select(release_version.ReleaseVersion.id).where(
                    release_version.ReleaseVersion.id == version_id
                )
            )
        ).first()
        if still_exists is None:
            logger.warning(
                "[RELEASE_QUALIFIER] promote: version row %s vanished before "
                "the final state write — skipping",
                version_id,
            )
            return None
        return version

    # lock_released=True — re-read + re-lock from authoritative DB state.
    session.expire(version)
    relocked = (
        (
            await session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if relocked is None:
        logger.warning(
            "[RELEASE_QUALIFIER] promote: version row %s vanished before the "
            "final state write — skipping",
            version_id,
        )
    return relocked


async def promote(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
    *,
    composite: dict[str, Any] | None = None,
    base_record: dict[str, Any] | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Promote the qualified candidate commit to ``main``.

    Audit C4: the qualifier children score the exact ``candidate_sha`` snapshot
    (their payloads are pinned to it), and ``automain`` legitimately keeps
    advancing during the 10-30 min they run.  Promotion therefore targets the
    EXACT ``candidate_sha`` — NOT live ``automain``:

      1. Create a release branch ``release-v{version_number}`` at
         ``candidate_sha``.  ``github.create_branch`` is idempotent (a 422
         "already exists" returns the existing ref); if a stale branch from a
         prior attempt points elsewhere it is deleted and recreated so the PR
         always carries exactly the qualified commit.
      2. Open a PR ``release-v{version_number} → main``.
      3. Merge it with ``merge_method="merge"`` (preserves integration
         history; branch protection on ``main`` runs CI as the final gate).
      4. Best-effort delete the ``release-v{version_number}`` branch (audit
         R4-H2 — dangling release branches must not accumulate forever).

    Because the PR is pinned to ``candidate_sha`` there is NO staleness guard:
    ``automain`` moving on after qualification is expected and harmless — the
    release branch is frozen at the scored commit regardless.

    **Idempotency (audit R4-C1 / R4-H1).**  A promotion is multi-step; a crash
    or retry between the merge landing on ``main`` and the DB commit would
    otherwise re-merge a closed PR or misclassify an already-merged PR (405) as
    a failure — diverging the DB from GitHub permanently.  ``promote()``
    therefore runs ONE ancestry check (``_candidate_already_on_main`` →
    ``github.is_ancestor_of``) (a) at the very start — if ``candidate_sha`` is
    already reachable from ``main`` the promotion already landed, so route
    straight to success — and (b) again before recording any non-merged result
    as a failure, so an open-but-already-merged PR is recorded as the success
    it actually is.

    **Lock safety (audit R4-H3).**  ``promote()``'s GitHub I/O (branch create,
    optional delete+recreate, PR create, merge, SHA re-resolve, tag, branch
    delete) is 30-120s of network calls.  The caller
    (``maybe_finalize_qualification`` / the sweep) holds a ``FOR UPDATE`` lock
    on the ``ReleaseVersion`` row; holding it across that I/O stalls every
    concurrent qualifier path.  So ``promote()`` issues an intermediate
    ``session.commit()`` to RELEASE that lock before the heavy I/O, runs the
    I/O unlocked, then RE-acquires ``FOR UPDATE`` (via
    ``_relock_version_for_promotion_write``) only for the final state write —
    re-checking ``version.status == "open"`` under the re-lock.  The idempotency
    check PLUS that re-check together keep a crash-retry or a concurrent
    finalize from double-promoting: if a parallel run already promoted, the
    re-locked row reads ``status="promoted"`` and this call bails as
    already-landed instead of opening a duplicate vN+1.

    CRITICAL: ``github.merge_pull_request`` does NOT enforce CI and does not
    raise on a 405/409 — it returns ``{"merged": False}``.  This function
    checks ``result.get("merged")`` and treats a non-merged result as a FAILED
    promotion (after the idempotency re-check): it does NOT retry blindly.

    On success: ``version.status = "promoted"``, stamps ``promoted_at`` /
    ``promoted_sha``, opens the next version (``build`` phase) via
    ``release_version.open_version``.  The recorded ``promoted_sha`` and the
    ``v{N}`` tag point at the REAL merge commit — ``merge_method="merge"``
    creates a new commit on ``main`` of which ``candidate_sha`` is only a
    parent, so the new ``main`` HEAD is re-resolved after the merge and used
    for ``promoted_sha``.  ``candidate_sha`` stays recorded as what was scored.
    The empty-diff branch keeps ``candidate_sha`` (release branch == main).

    On failure / non-merge: records the outcome in ``version.last_qualification``,
    keeps the version open, and sets ``phase = "stabilize"``.

    ``token``, ``owner``, ``repo`` are optional; when omitted (the normal
    finalize path — concern 2: ``_finalize_qualification`` deliberately does NOT
    pre-resolve them, since credential resolution is GitHub I/O) they are
    resolved here AFTER the intermediate commit has released the row lock, so no
    token round-trip ever runs under the lock.

    Operates on the ``session`` it is given — which the finalize chain runs as a
    DEDICATED session, so the intermediate commit only ever flushes qualifier
    state.  ``promote()`` issues that one intermediate commit itself; the
    caller (``_finalize_in_dedicated_session``) commits the final state write.
    Returns a result dict.
    """
    base_record = dict(base_record or {})
    project = await session.get(Project, version.project_id)
    if project is None:
        logger.warning(
            "[RELEASE_QUALIFIER] promote: project %s not found",
            version.project_id,
        )
        # Record a promotion failure so the version reaches a terminal
        # stabilize state.  A bare error return here would leave
        # last_qualification stuck at status="pending", and every later
        # _finalize_qualification call would re-loop on it forever.
        # This runs BEFORE the intermediate commit, so the version row is still
        # locked by this transaction — write directly (lock_released=False).
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "project_not_found",
            lock_released=False,
        )

    release_branch = f"release-v{version.version_number}"

    # --- Release the ReleaseVersion row lock BEFORE any GitHub I/O ----------
    # Audit R4-H3 + concern 2: the caller-supplied dedicated session holds a
    # FOR UPDATE lock on this version row (taken by _finalize_in_dedicated_session).
    # Token resolution AND the idempotency ancestry check are both GitHub I/O —
    # they must NOT run under that row lock, or every concurrent qualifier path
    # stalls behind the network.  So the intermediate commit happens HERE,
    # before token resolution and before the idempotency check — the lock is
    # released first, then all I/O runs unlocked.  The final state write
    # re-acquires the lock via _relock_version_for_promotion_write.
    #
    # The dedicated session has ONLY qualifier state dirty (concern 1), so this
    # commit can never prematurely commit a caller's unrelated transaction.
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — a commit failure here is recoverable
        logger.warning(
            "[RELEASE_QUALIFIER] promote: failed to commit before releasing "
            "the row lock for v%d (%s) — recording failure",
            version.version_number,
            exc,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] promote: rollback after pre-I/O commit "
                "failure also failed for v%d",
                version.version_number,
            )
        # The rollback released the lock — the failure record must re-lock.
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "lock_release_failed",
            detail=str(exc)[:300],
            lock_released=True,
        )

    # The row lock is now released — all the I/O below runs unlocked.
    if not token or not owner or not repo:
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return await _record_promotion_failure(
                session,
                version,
                base_record,
                "no_credentials",
                lock_released=True,
            )
        token, owner, repo = resolved

    # --- Idempotency pre-check (audit R4-C1 / R4-H1) ------------------------
    # If candidate_sha is ALREADY reachable from main, a prior promotion
    # attempt landed the merge but crashed before the DB commit.  Route
    # straight to the success path (treat as already-landed) — do NOT re-merge
    # and do NOT record a failure.  This is a read-only GitHub call and now
    # runs unlocked (the intermediate commit above released the row lock).
    if candidate_sha and await _candidate_already_on_main(
        token, owner, repo, candidate_sha, version
    ):
        logger.info(
            "[RELEASE_QUALIFIER] promote: candidate %s for v%d is already "
            "reachable from main — a prior promotion landed; recording success "
            "without re-merging (idempotent retry)",
            candidate_sha[:8],
            version.version_number,
        )
        # Best-effort: clean up the release branch a prior attempt may have
        # left behind (audit R4-H2).
        await _delete_release_branch_best_effort(
            token, owner, repo, release_branch, version
        )
        return await _record_promotion_success(
            session,
            version,
            candidate_sha,
            base_record,
            # Concern 5: this idempotent-success path has no PR object of its
            # own.  Carry forward a pr_url the caller threaded through
            # base_record (the original promotion attempt's PR) when present;
            # otherwise label it explicitly rather than recording a bare "".
            pr_url=_idempotent_pr_url(base_record),
            token=token,
            owner=owner,
            repo=repo,
            already_landed=True,
        )

    # 0. Create the release branch pinned to the qualified candidate commit
    #    (audit C4).  Deterministic name so a retried promotion is idempotent.
    #    create_branch returns the existing ref on a 422; if that existing ref
    #    points at a different SHA (a stale branch from a failed prior attempt)
    #    delete + recreate it so the PR always carries exactly candidate_sha.
    try:
        branch_ref = await github.create_branch(
            token, owner, repo, release_branch, candidate_sha
        )
        existing_sha = (branch_ref or {}).get("object", {}).get("sha")
        if existing_sha and existing_sha != candidate_sha:
            logger.warning(
                "[RELEASE_QUALIFIER] promote: release branch %s already exists "
                "at %s (stale) — recreating at candidate %s for v%d",
                release_branch,
                existing_sha[:8],
                candidate_sha[:8],
                version.version_number,
            )
            await github.delete_branch(token, owner, repo, release_branch)
            await github.create_branch(
                token, owner, repo, release_branch, candidate_sha
            )
    except Exception as exc:  # noqa: BLE001 — a branch-creation failure blocks, never raises
        logger.warning(
            "[RELEASE_QUALIFIER] promote: failed to create release branch %s "
            "at %s for v%d: %s",
            release_branch,
            (candidate_sha or "")[:8],
            version.version_number,
            exc,
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "release_branch_failed",
            detail=str(exc)[:300],
            lock_released=True,
        )

    # 1. Open the release-branch → main PR.  The release branch is frozen at
    #    candidate_sha, so this PR promotes exactly the qualified commit.
    try:
        pr = await github.create_pull_request(
            token,
            owner,
            repo,
            title=(
                f"Release v{version.version_number}: promote {release_branch} → main"
            ),
            head=release_branch,
            base="main",
            body=(
                f"Automated promotion of release v{version.version_number}.\n\n"
                f"Candidate SHA: `{candidate_sha}`\n"
                f"Release branch: `{release_branch}` (pinned to the qualified "
                f"commit)\n"
                f"Composite confidence: "
                f"{(composite or {}).get('confidence', 'n/a')}\n\n"
                f"Opened by the release qualifier "
                f"(backend/app/release_qualifier.py)."
            ),
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a PR-creation failure blocks, never raises
        # Audit R4-H1: a PR-creation failure on a RETRY is the classic
        # divergence trap — the prior attempt's PR merged then closed, so
        # create_pull_request finds nothing open and raises.  Re-run the
        # idempotency check before recording a failure: if the candidate is
        # already on main, this is a successful prior promotion, not a failure.
        if candidate_sha and await _candidate_already_on_main(
            token, owner, repo, candidate_sha, version
        ):
            logger.info(
                "[RELEASE_QUALIFIER] promote: PR creation failed for v%d but "
                "candidate %s is already on main — a prior promotion landed; "
                "recording success (idempotent retry)",
                version.version_number,
                candidate_sha[:8],
            )
            await _delete_release_branch_best_effort(
                token, owner, repo, release_branch, version
            )
            return await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                # Concern 5: no live PR object here (creation failed); carry a
                # caller-threaded pr_url through if present, else label it.
                pr_url=_idempotent_pr_url(base_record),
                token=token,
                owner=owner,
                repo=repo,
                already_landed=True,
            )
        logger.warning(
            "[RELEASE_QUALIFIER] failed to open promotion PR for v%d: %s",
            version.version_number,
            exc,
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "pr_creation_failed",
            detail=str(exc)[:300],
            lock_released=True,
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
        await _delete_release_branch_best_effort(
            token, owner, repo, release_branch, version
        )
        return await _record_promotion_success(
            session,
            version,
            candidate_sha,
            base_record,
            pr_url,
            token=token,
            owner=owner,
            repo=repo,
            empty_diff=True,
        )

    if not pr_number:
        # Concern 5: a PR with no number cannot be merged — but create_branch
        # already ran, so the release-v{N} branch exists.  Clean it up
        # best-effort before recording the failure so it does not dangle.
        await _delete_release_branch_best_effort(
            token, owner, repo, release_branch, version
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "pr_missing_number",
            pr_url=pr_url,
            lock_released=True,
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
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "merge_error",
            detail=str(exc)[:300],
            pr_url=pr_url,
            lock_released=True,
        )

    if not merge_result.get("merged"):
        # Concern 3 — disambiguate a merged=False result.  merge_pull_request
        # collapses TWO different outcomes onto {"merged": False}: a genuine
        # merge block (failing checks / required reviews / conflict) AND an
        # already-merged PR (GitHub's merge endpoint returns 405 once a PR is
        # already merged).  Recording an already-merged PR as merge_blocked
        # would permanently diverge the DB from GitHub.
        #
        # _promotion_pr_landed asks the PR resource itself for its
        # authoritative ``merged`` boolean — which flips synchronously on a
        # merge and is NOT subject to the compare API's eventual-consistency
        # lag that could make a genuinely-successful promotion look like a
        # merge_blocked failure right after the merge.  It falls back to the
        # ancestry check only when the PR state is indeterminate.
        if await _promotion_pr_landed(
            token, owner, repo, pr_number, candidate_sha, version
        ):
            logger.info(
                "[RELEASE_QUALIFIER] promote: PR #%s for v%d returned "
                "merged=False but the PR is in fact merged — recording success "
                "(idempotent retry / eventual-consistency-safe)",
                pr_number,
                version.version_number,
            )
            await _delete_release_branch_best_effort(
                token, owner, repo, release_branch, version
            )
            merged_sha_retry: str | None = None
            try:
                merged_sha_retry = await github.get_branch_sha(
                    token, owner, repo, "main"
                )
            except Exception:  # noqa: BLE001 — best-effort; degrade to candidate
                logger.warning(
                    "[RELEASE_QUALIFIER] promote: could not re-resolve main "
                    "HEAD after detecting an already-merged PR #%s for v%d — "
                    "recording candidate_sha",
                    pr_number,
                    version.version_number,
                )
            return await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                pr_url,
                token=token,
                owner=owner,
                repo=repo,
                promoted_sha=merged_sha_retry or candidate_sha,
                already_landed=True,
            )
        # Distinguish a real merge conflict (GitHub 409 → conflict=True) from
        # pending required checks (GitHub 405 → conflict=False/None).  A real
        # conflict cannot self-heal and must fail immediately; a pending-CI
        # block WILL clear once checks pass, so arrange for GitHub to complete
        # the merge automatically rather than recording a spurious failure.
        if merge_result.get("conflict"):
            msg = merge_result.get("message", "merge not completed")
            logger.warning(
                "[RELEASE_QUALIFIER] promotion PR #%s for v%d did not merge "
                "(real conflict): %s",
                pr_number,
                version.version_number,
                msg,
            )
            return await _record_promotion_failure(
                session,
                version,
                base_record,
                "merge_blocked",
                detail=str(msg)[:300],
                pr_url=pr_url,
                conflict=True,
                lock_released=True,
            )

        # CI still pending (GitHub 405, no conflict) — arrange for GitHub to
        # complete the merge once required checks pass.  Don't record a failure
        # verdict; last_qualification stays "pending" so the reaper re-checks
        # and _candidate_already_on_main records idempotent success when the
        # auto-merge lands.
        msg = merge_result.get("message", "merge not completed")
        try:
            am_result = await github.enable_auto_merge(
                token,
                owner,
                repo,
                pr_number,
                merge_method="MERGE",
                project_id=str(version.project_id),
            )
        except Exception as exc:  # noqa: BLE001
            am_result = {"enabled": False, "message": str(exc)[:200]}
        logger.info(
            "[RELEASE_QUALIFIER] promote: PR #%s for v%d pending CI — "
            "auto-merge %s (%s). Qualification stays pending; reaper confirms "
            "when auto-merge lands.",
            pr_number,
            version.version_number,
            "enabled" if am_result.get("enabled") else "not confirmed",
            am_result.get("message", ""),
        )
        return {
            "status": "pending_auto_merge",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "auto_merge_enabled": bool(am_result.get("enabled")),
        }

    # 3. Success — resolve the REAL merged commit.  merge_method="merge"
    #    creates a NEW merge commit on main; candidate_sha is only one of its
    #    parents, not main HEAD.  github.merge_pull_request does not return the
    #    merge SHA, so re-resolve main HEAD — that commit IS the merge result.
    #    A resolution failure here is non-fatal: the merge already landed, so
    #    degrade to candidate_sha (a known parent) rather than fail a
    #    successful promotion.
    merged_sha: str | None = None
    try:
        merged_sha = await github.get_branch_sha(token, owner, repo, "main")
    except Exception as exc:  # noqa: BLE001 — merge already succeeded; SHA read is best-effort
        logger.warning(
            "[RELEASE_QUALIFIER] promote: merged PR #%s for v%d but could not "
            "resolve the new main HEAD (%s) — recording candidate_sha instead",
            pr_number,
            version.version_number,
            exc,
        )

    # 4. Best-effort: delete the release branch now that the merge has landed
    #    (audit R4-H2 — release-v{N} branches must not accumulate forever).
    await _delete_release_branch_best_effort(
        token, owner, repo, release_branch, version
    )

    logger.info(
        "[RELEASE_QUALIFIER] v%d promoted: automain → main merged via PR #%s "
        "(merge commit=%s)",
        version.version_number,
        pr_number,
        (merged_sha or candidate_sha)[:8],
    )
    return await _record_promotion_success(
        session,
        version,
        candidate_sha,
        base_record,
        pr_url,
        token=token,
        owner=owner,
        repo=repo,
        promoted_sha=merged_sha or candidate_sha,
    )


async def _delete_release_branch_best_effort(
    token: str,
    owner: str,
    repo: str,
    release_branch: str,
    version: release_version.ReleaseVersion,
) -> None:
    """Delete the ``release-v{N}`` branch after a promotion — best-effort.

    Audit R4-H2: the release branch is a throwaway pointer at the qualified
    commit; once the promotion PR has merged (or the candidate is confirmed on
    main) it serves no purpose.  Left behind, ``release-v1``, ``release-v2``, …
    accumulate on the repo forever.

    A delete failure must NEVER fail or abort the promotion — the merge has
    already landed; a dangling branch is cosmetic clutter, not a correctness
    problem.  Same best-effort contract as the ``v{N}`` tag creation: wrapped
    in try/except, logs a warning only.
    """
    try:
        deleted = await github.delete_branch(token, owner, repo, release_branch)
        if deleted:
            logger.info(
                "[RELEASE_QUALIFIER] promote: deleted release branch %s for v%d",
                release_branch,
                version.version_number,
            )
        else:
            # delete_branch returns False (not raises) on a non-204 — e.g. the
            # branch is already gone.  Not an error; just note it.
            logger.info(
                "[RELEASE_QUALIFIER] promote: release branch %s for v%d not "
                "deleted (already gone or non-204) — non-fatal",
                release_branch,
                version.version_number,
            )
    except Exception as exc:  # noqa: BLE001 — branch delete failure must not abort promotion
        logger.warning(
            "[RELEASE_QUALIFIER] promote: failed to delete release branch %s "
            "for v%d (non-fatal): %s",
            release_branch,
            version.version_number,
            exc,
        )


async def _record_promotion_failure(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    base_record: dict[str, Any],
    error: str,
    *,
    detail: str | None = None,
    pr_url: str | None = None,
    conflict: bool = False,
    lock_released: bool,
) -> dict[str, Any]:
    """Record a failed promotion: keep the version open, set phase=stabilize.

    A failed promotion only ever *blocks* — the version stays open and goes
    back to stabilize so the next strategic cycle works the blocker.  ``main``
    is untouched, so this is always safe.

    Audit R4-H3: resolves the ``ReleaseVersion`` row (``FOR UPDATE``-locked) via
    ``_relock_version_for_promotion_write`` before writing.  ``lock_released``
    tells that helper whether ``promote()`` has already issued its intermediate
    commit: ``True`` ⇒ re-read + re-lock from the DB; ``False`` ⇒ an early-return
    path before the commit, the row is still locked by this transaction so the
    live instance is used directly without an ``expire()`` that would drop
    pending mutations (concern 4).

    Round 9 (data-integrity): re-checks ``version.status == "open"`` under the
    re-lock — the SAME guard ``_record_promotion_success`` already has.  Two
    finalize/promote calls can race (the completion-handler finalize and the
    reaper-sweep finalize run in separate concurrent transactions); if one
    promotes successfully while the loser fails transiently, the loser must NOT
    overwrite ``last_qualification`` with ``{status: "promotion_failed", ...}``
    on a row that already reads ``status="promoted"`` — that would leave a
    self-contradictory terminal record.  When the re-locked row is no longer
    ``open`` this returns an ``already_promoted``-style result WITHOUT touching
    ``last_qualification``, exactly mirroring ``_record_promotion_success``.
    """
    locked = await _relock_version_for_promotion_write(
        session, version, lock_released=lock_released
    )
    if locked is None:
        # Row vanished (project deleted mid-promotion) — nothing to record.
        return {"status": "promotion_failed", "error": error, "conflict": conflict}
    version = locked
    # Re-check under the re-lock (Round 9 — data integrity): a concurrent
    # finalize / crash-retry may have already PROMOTED this version while this
    # call's GitHub I/O ran unlocked.  An already-promoted (or abandoned)
    # version is terminal — overwriting last_qualification with a
    # promotion_failed record here would contradict version.status="promoted".
    # Bail to a result that reflects the already-recorded state, leaving
    # last_qualification untouched.  Mirrors _record_promotion_success.
    if version.status != "open":
        logger.info(
            "[RELEASE_QUALIFIER] promote: v%d is already status=%s under the "
            "re-lock when recording a '%s' failure — a concurrent "
            "finalize/retry already promoted it; not overwriting "
            "last_qualification (idempotent)",
            version.version_number,
            version.status,
            error,
        )
        existing = version.last_qualification or {}
        return {
            "status": existing.get("status", version.status),
            "promoted_sha": existing.get("promoted_sha") or version.promoted_sha,
            "next_version_number": None,
            "already_promoted": True,
        }
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
    # Defensive (audit R4 MEDIUM): only flip phase on a still-open version.  An
    # already-promoted/abandoned version is terminal — set_version_phase would
    # be a meaningless write (and the version should never reach here promoted).
    if version.status == "open":
        release_version.set_version_phase(
            version,
            "stabilize",
            reasoning=(
                f"promotion v{version.version_number} failed ({error}); "
                f"returning to stabilize"
            ),
        )
    session.add(version)
    # Phase 4: durable verdict-history row.  A promotion_failed candidate DID
    # clear the qualifier (would_promote=True) — the failure is in the merge
    # infrastructure, not the score — and promotion only ever runs when enabled.
    await _record_qualification_verdict(
        session,
        version,
        record,
        terminal_status="promotion_failed",
        would_promote=True,
        promotion_enabled=True,
    )
    logger.warning(
        "[RELEASE_QUALIFIER] promotion of v%d failed (%s) — version stays open, "
        "phase=stabilize",
        version.version_number,
        error,
    )
    failure_detail = f": {detail}" if detail else ""
    await _notify_operator(
        session,
        version,
        "release_promotion_failed",
        (
            f"Promotion of v{version.version_number} failed — it cleared the "
            f"qualifier but the automain → main merge failed "
            f"({error}{failure_detail}). main was NOT advanced; the version "
            f"stays open and returns to stabilize."
        ),
        extra={
            "error": error,
            "detail": detail,
            "pr_url": pr_url,
            "conflict": conflict,
        },
    )
    return {"status": "promotion_failed", "error": error, "conflict": conflict}


async def _record_promotion_success(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
    base_record: dict[str, Any],
    pr_url: str,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    empty_diff: bool = False,
    promoted_sha: str | None = None,
    already_landed: bool = False,
    lock_released: bool = True,
) -> dict[str, Any]:
    """Record a successful promotion and open the next version.

    Marks ``version`` ``status="promoted"``, stamps ``promoted_at`` /
    ``promoted_sha``, then opens version N+1 in the ``build`` phase with its
    ``base_sha`` set to the just-promoted SHA.

    ``promoted_sha`` is the commit that actually became ``main`` HEAD.  For a
    real merge that is the NEW merge commit ``merge_method="merge"`` produced
    (``candidate_sha`` — automain HEAD — is only a *parent* of it), so
    ``promote()`` re-resolves ``main`` HEAD after the merge and passes it here.
    When omitted it defaults to ``candidate_sha`` — correct for the empty-diff
    branch, where ``automain == main`` and nothing new was committed.  The
    version row, the next version's ``base_sha`` and the ``v{N}`` tag all use
    this ``promoted_sha``.

    Audit R4-H3 / R4-C1: resolves the ``ReleaseVersion`` row (``FOR UPDATE``-
    locked) via ``_relock_version_for_promotion_write`` and RE-CHECKS
    ``version.status == "open"`` under that lock.  If the row already reads
    ``status="promoted"`` a concurrent finalize / a crash-retry already promoted
    this version — bail as already-landed WITHOUT opening a duplicate vN+1,
    instead of double-promoting.  Every ``promote()`` success path reaches here
    AFTER the intermediate commit, so ``lock_released`` defaults to ``True``
    (re-read + re-lock from the DB).  ``already_landed`` is True when
    ``promote()`` reached here via an idempotency path (the merge was already on
    ``main``); it only affects logging.

    After the version is marked promoted, creates a git tag
    ``v{version.version_number}`` on the promoted SHA.  A tag-creation failure
    is non-fatal — it is wrapped in try/except and only logs a warning.  The
    merge already succeeded; the tag is cosmetic convenience.
    """
    locked = await _relock_version_for_promotion_write(
        session, version, lock_released=lock_released
    )
    if locked is None:
        # Row vanished — the merge landed on GitHub but the version row is
        # gone (project deleted mid-promotion).  Nothing left to write.
        return {
            "status": "promoted",
            "promoted_sha": promoted_sha or candidate_sha,
            "next_version_number": None,
        }
    version = locked

    # Re-check under the re-lock (audit R4-C1 / R4-H3): a concurrent finalize
    # or a crash-retry may have already promoted this version while promote()'s
    # GitHub I/O ran unlocked.  An already-promoted (or abandoned) version is
    # terminal — re-promoting would open a duplicate vN+1.  Bail to a success
    # return that reflects the already-recorded state.
    if version.status != "open":
        logger.info(
            "[RELEASE_QUALIFIER] promote: v%d is already status=%s under the "
            "re-lock — a concurrent finalize/retry already promoted it; not "
            "re-promoting (idempotent)",
            version.version_number,
            version.status,
        )
        existing = version.last_qualification or {}
        return {
            "status": "promoted",
            "promoted_sha": existing.get("promoted_sha")
            or version.promoted_sha
            or promoted_sha
            or candidate_sha,
            "next_version_number": None,
            "already_promoted": True,
        }

    # The commit that is now main HEAD.  Falls back to candidate_sha for the
    # empty-diff path (automain == main, so they are the same commit).
    effective_sha = promoted_sha or candidate_sha
    now = _utcnow()
    record = {
        **base_record,
        "status": "promoted",
        "candidate_sha": candidate_sha,
        "promoted_sha": effective_sha,
        "pr_url": pr_url,
    }
    if empty_diff:
        record["empty_diff"] = True
    if already_landed:
        record["already_landed"] = True
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    version.status = "promoted"
    version.promoted_at = now
    version.promoted_sha = effective_sha
    session.add(version)
    # Phase 4: durable verdict-history row for a successful promotion.  This
    # only ever runs with promotion enabled (the dry_run branch returns earlier).
    await _record_qualification_verdict(
        session,
        version,
        record,
        terminal_status="promoted",
        would_promote=True,
        promotion_enabled=True,
    )

    # Open the next version (build phase) — base_sha is the freshly-promoted SHA
    # (the real main HEAD after the merge, not the now-superseded candidate).
    # goal snapshots the project's current focus (see release_version.open_version).
    _next_goal: str | None = None
    try:
        from app.problem_model import parse_problem_model

        # _record_promotion_success has no `project` in scope — fetch it.  (Bug:
        # the prior code referenced an undefined `project`, so the NameError was
        # always swallowed here and every promotion opened v(N+1) with goal=None
        # instead of snapshotting current_focus.)
        _project = await session.get(Project, version.project_id)
        if _project is not None:
            _next_goal = (
                parse_problem_model(_project.problem_model).intent.current_focus
                or None
            )
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] could not read current_focus for next version goal"
        )
    next_version = await release_version.open_version(
        session,
        version.project_id,
        version_number=version.version_number + 1,
        base_sha=effective_sha,
        goal=_next_goal,
    )
    logger.info(
        "[RELEASE_QUALIFIER] v%d promoted (sha=%s%s); opened v%d",
        version.version_number,
        effective_sha[:8],
        " — already-landed retry" if already_landed else "",
        next_version.version_number,
    )

    # Best-effort: create a git tag v{version_number} on the promoted SHA.
    # A tag-creation failure must NEVER abort or fail the promotion — the merge
    # already succeeded and the tag is cosmetic convenience only.
    if token and owner and repo:
        tag_name = f"v{version.version_number}"
        try:
            await github.create_tag(
                token,
                owner,
                repo,
                tag_name,
                effective_sha,
                message=f"Release {tag_name} (automain → main promotion)",
                project_id=str(version.project_id),
            )
            logger.info(
                "[RELEASE_QUALIFIER] created release tag %s on %s",
                tag_name,
                effective_sha[:8],
            )
        except Exception as exc:  # noqa: BLE001 — tag failure must not abort promotion
            logger.warning(
                "[RELEASE_QUALIFIER] failed to create release tag %s (non-fatal): %s",
                tag_name,
                exc,
            )

    await _notify_operator(
        session,
        version,
        "release_promoted",
        (
            f"Released v{version.version_number} — automain → main promoted "
            f"(confidence {float(base_record.get('confidence') or 0.0):.2f}). "
            f"Production deploy is running; v{next_version.version_number} is "
            f"now open for build."
        ),
        extra={
            "confidence": base_record.get("confidence"),
            "sub_scores": base_record.get("sub_scores"),
            "promoted_sha": effective_sha,
            "pr_url": pr_url,
            "next_version_number": next_version.version_number,
        },
    )
    return {
        "status": "promoted",
        "promoted_sha": effective_sha,
        "next_version_number": next_version.version_number,
    }


# ---------------------------------------------------------------------------
# Public display helpers — stable API for the operator lifecycle panel.
# ---------------------------------------------------------------------------

# Maps the internal last_qualification status code to a human-readable label
# for the version-lifecycle panel.
_OUTCOME_LABELS: dict[str, str] = {
    "pending": "In progress",
    "dry_run": "Dry-run (would promote)",
    "promoted": "Promoted",
    "not_promoted": "Below threshold",
    "error": "Error",
}


def format_qualification_for_api(qual: dict[str, Any]) -> dict[str, Any]:
    """Return an operator-facing summary of a ``last_qualification`` record.

    Normalizes the raw qualification dict stored on ``ReleaseVersion`` into
    a stable shape suitable for the version-lifecycle panel.  Adds a
    human-readable ``outcome`` label alongside the raw ``status`` code.

    Safe to call with an empty dict — all fields default to ``None``.
    """
    status = qual.get("status")
    return {
        "status": status,
        "outcome": _OUTCOME_LABELS.get(status, status),
        "confidence": qual.get("confidence"),
        "threshold": qual.get("threshold"),
        "sub_scores": qual.get("sub_scores"),
        "blocker": qual.get("blocker"),
        "started_at": qual.get("started_at"),
        "finalized_at": qual.get("finalized_at"),
    }


def format_verdict_for_api(verdict: QualificationVerdict) -> dict[str, Any]:
    """Return an operator-facing dict for a single QualificationVerdict row.

    Serializes the ORM row into a stable, JSON-safe shape for the
    qualification-verdict history endpoint.  Adds a human-readable ``outcome``
    label alongside the raw ``terminal_status`` code so the UI can render
    both without a second lookup.
    """
    return {
        "id": str(verdict.id),
        "release_version_id": str(verdict.release_version_id),
        "candidate_sha": verdict.candidate_sha,
        "confidence": verdict.confidence,
        "sub_scores": verdict.sub_scores,
        "blocker": verdict.blocker,
        "would_promote": verdict.would_promote,
        "promotion_enabled": verdict.promotion_enabled,
        "terminal_status": verdict.terminal_status,
        "outcome": _OUTCOME_LABELS.get(
            verdict.terminal_status, verdict.terminal_status
        ),
        "threshold": verdict.threshold,
        "started_at": verdict.started_at.isoformat() if verdict.started_at else None,
        "finalized_at": verdict.finalized_at.isoformat()
        if verdict.finalized_at
        else None,
        "created_at": verdict.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Promotion-readiness aggregation — the calibration evidence (design Phase 4).
# ---------------------------------------------------------------------------
#
# These constants gate the human-facing recommendation only.  Per design
# decision D2 the recommendation NEVER auto-flips runtime_config["promotion_
# enabled"] — it summarizes the durable dry-run track record so an operator can
# answer "is the qualifier trustworthy enough to enable autonomous promotion
# yet?" from real data and flip the switch themselves.  Kept deliberately
# CONSERVATIVE: signalling ready prematurely is the costly error (it invites a
# human to hand the qualifier the keys to production main).

# How many most-recent dry-run-regime verdicts to consider.
_READINESS_HISTORY_WINDOW = 50
# Minimum number of evaluated dry-run-regime verdicts before any "ready" signal.
# A handful of passes is not yet a track record.
_READINESS_MIN_VERDICTS = 5
# Minimum run of the MOST-RECENT consecutive clean would-promote verdicts (no
# blocker) — recent stability matters more than a good-then-bad history.
_READINESS_MIN_CLEAN_STREAK = 3
# Minimum would-promote rate across the evaluated window.
_READINESS_MIN_WOULD_PROMOTE_RATE = 0.80
# A blocker code seen at least this many times in the window is "recurring" —
# the qualifier keeps tripping on the same hard problem, so NOT ready.
_READINESS_RECURRING_BLOCKER_MIN = 2
# Confidence-spread ceiling for the advisory "stable" flag (evidence only — not
# a hard readiness gate).
_READINESS_CONFIDENCE_STDEV_MAX = 0.15


def _empty_readiness(thresholds: dict[str, Any]) -> dict[str, Any]:
    """The not-ready summary returned when there is no verdict history yet."""
    return {
        "ready": False,
        "reason": "no qualification verdicts recorded yet — nothing to evaluate",
        "verdicts_evaluated": 0,
        "would_promote_count": 0,
        "would_promote_rate": 0.0,
        "consecutive_clean_passes": 0,
        "confidence": {
            "mean": None,
            "stdev": None,
            "min": None,
            "max": None,
            "stable": False,
        },
        "recurring_blockers": {},
        "blocker_counts": {},
        "thresholds": thresholds,
    }


async def compute_promotion_readiness(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Summarize the dry-run qualification track record into a readiness verdict.

    Reads the durable :class:`QualificationVerdict` history (the most recent
    ``_READINESS_HISTORY_WINDOW`` verdicts recorded while promotion was DISABLED
    — the dry-run regime, where every finalize lands as ``dry_run`` /
    ``not_promoted``) and returns a conservative ``ready`` / not-ready
    recommendation plus the supporting evidence.

    EVIDENCE + RECOMMENDATION ONLY (design decision D2): this NEVER flips
    ``promotion_enabled``.  ``ready=True`` requires ALL of:

      * at least ``_READINESS_MIN_VERDICTS`` evaluated verdicts, AND
      * a most-recent consecutive-clean would-promote streak of at least
        ``_READINESS_MIN_CLEAN_STREAK``, AND
      * a would-promote rate >= ``_READINESS_MIN_WOULD_PROMOTE_RATE``, AND
      * NO recurring hard blocker (a blocker code seen
        >= ``_READINESS_RECURRING_BLOCKER_MIN`` times in the window).

    Returns a dict with ``ready``, a human-readable ``reason``, the raw evidence
    (``verdicts_evaluated``, ``would_promote_count``, ``would_promote_rate``,
    ``consecutive_clean_passes``, a ``confidence`` distribution + advisory
    ``stable`` flag, ``blocker_counts`` and the subset that is ``recurring_
    blockers``) and the ``thresholds`` used so the recommendation is auditable.
    """
    thresholds: dict[str, Any] = {
        "min_verdicts": _READINESS_MIN_VERDICTS,
        "min_clean_streak": _READINESS_MIN_CLEAN_STREAK,
        "min_would_promote_rate": _READINESS_MIN_WOULD_PROMOTE_RATE,
        "recurring_blocker_min": _READINESS_RECURRING_BLOCKER_MIN,
        "history_window": _READINESS_HISTORY_WINDOW,
    }

    # Only the dry-run regime (promotion_enabled is False) is evidence for
    # whether to flip the switch — verdicts recorded after promotion was enabled
    # are post-decision and excluded.  Newest-first so the leading run is the
    # most-recent consecutive-clean streak.
    stmt = (
        select(QualificationVerdict)
        .where(
            QualificationVerdict.project_id == project_id,
            QualificationVerdict.promotion_enabled.is_(False),
        )
        .order_by(QualificationVerdict.created_at.desc())
        .limit(_READINESS_HISTORY_WINDOW)
    )
    verdicts = list((await session.execute(stmt)).scalars().all())

    evaluated = len(verdicts)
    if evaluated == 0:
        return _empty_readiness(thresholds)

    would_promote_count = sum(1 for v in verdicts if v.would_promote)
    would_promote_rate = round(would_promote_count / evaluated, 4)

    # Leading run of clean would-promote passes (would_promote AND no blocker).
    consecutive_clean = 0
    for v in verdicts:
        if v.would_promote and not v.blocker:
            consecutive_clean += 1
        else:
            break

    # Blocker frequency across the window (only non-promote verdicts carry one).
    blocker_counts: dict[str, int] = {}
    for v in verdicts:
        if v.blocker:
            blocker_counts[v.blocker] = blocker_counts.get(v.blocker, 0) + 1
    recurring_blockers = {
        code: n
        for code, n in blocker_counts.items()
        if n >= _READINESS_RECURRING_BLOCKER_MIN
    }

    # Confidence distribution + an advisory stability measure (population stdev).
    confidences = [float(v.confidence) for v in verdicts]
    conf_mean = round(sum(confidences) / evaluated, 4)
    conf_min = round(min(confidences), 4)
    conf_max = round(max(confidences), 4)
    if evaluated > 1:
        variance = sum((c - conf_mean) ** 2 for c in confidences) / evaluated
        conf_stdev = round(variance**0.5, 4)
    else:
        conf_stdev = 0.0
    conf_stable = conf_stdev <= _READINESS_CONFIDENCE_STDEV_MAX

    reasons: list[str] = []
    if evaluated < _READINESS_MIN_VERDICTS:
        reasons.append(
            f"only {evaluated} dry-run verdict(s) evaluated "
            f"(need >= {_READINESS_MIN_VERDICTS})"
        )
    if consecutive_clean < _READINESS_MIN_CLEAN_STREAK:
        reasons.append(
            f"consecutive-clean streak is {consecutive_clean} "
            f"(need >= {_READINESS_MIN_CLEAN_STREAK})"
        )
    if would_promote_rate < _READINESS_MIN_WOULD_PROMOTE_RATE:
        reasons.append(
            f"would-promote rate is {would_promote_rate:.2f} "
            f"(need >= {_READINESS_MIN_WOULD_PROMOTE_RATE:.2f})"
        )
    if recurring_blockers:
        formatted = ", ".join(
            f"{code}x{n}" for code, n in sorted(recurring_blockers.items())
        )
        reasons.append(f"recurring hard blocker(s): {formatted}")

    ready = not reasons
    if ready:
        reason = (
            f"qualifier looks trustworthy: {would_promote_count}/{evaluated} "
            f"would-promote (rate {would_promote_rate:.2f}), {consecutive_clean} "
            f"consecutive clean passes, no recurring blockers"
        )
    else:
        reason = "; ".join(reasons)

    return {
        "ready": ready,
        "reason": reason,
        "verdicts_evaluated": evaluated,
        "would_promote_count": would_promote_count,
        "would_promote_rate": would_promote_rate,
        "consecutive_clean_passes": consecutive_clean,
        "confidence": {
            "mean": conf_mean,
            "stdev": conf_stdev,
            "min": conf_min,
            "max": conf_max,
            "stable": conf_stable,
        },
        "recurring_blockers": recurring_blockers,
        "blocker_counts": blocker_counts,
        "thresholds": thresholds,
    }
