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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import github, release_version
from app.models import (
    Alert,
    AlertSeverity,
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

        # Validate the weights sum to ~1.0.  This file decides what reaches
        # production main: weights summing >1.0 saturate confidence at 1.0 so
        # every candidate promotes (gate effectively disabled); <1.0 means
        # nothing can clear the threshold (gate stuck closed).  A human-edited
        # mistake here is silent and dangerous — fall back to the safe default
        # rubric rather than honour a mis-weighted policy.
        weight_sum = weights["audit"] + weights["test_erosion"] + weights["smoketest"]
        if abs(weight_sum - 1.0) > 0.01:
            raise ValueError(
                f"promotion_policy.yaml weights sum to {weight_sum:.4f}, "
                f"expected ~1.0"
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
    ``qualify_request`` path (holds the Project row lock) and the admin
    ``POST /qualify`` endpoint (holds no lock).  Both would otherwise pass the
    in-flight guard and each spawn an audit+smoketest pair (4 children, one
    orphaned pair) and clobber each other's ``last_qualification`` write.  To
    serialize them this function takes ``SELECT ... FOR UPDATE`` on the
    ``ReleaseVersion`` row and holds it across the in-flight check, the child
    spawns, and the ``last_qualification`` write.  The GitHub I/O that resolves
    ``candidate_sha`` runs BEFORE the lock to keep the locked window short.

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
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
        flag_modified(version, "last_qualification")
        session.add(version)
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
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
        flag_modified(version, "last_qualification")
        session.add(version)
        return {"status": "sha_error"}

    # 3. CI check on the candidate SHA (cheap, orchestrator-side).  Also done
    #    before the row lock — it is read-only external I/O.
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
        await session.execute(
            select(release_version.ReleaseVersion)
            .where(release_version.ReleaseVersion.id == version.id)
            .with_for_update()
        )
    ).scalars().first()
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
            "started_at": started_at,
        }
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
        flag_modified(version, "last_qualification")
        session.add(version)
        return {"status": "spawn_failed"}

    # 6. Stamp the pending qualification record.  smoketest_task_id is None
    #    when the smoketest subsystem is disabled — maybe_finalize_qualification
    #    and the reaper sweep both treat a None smoketest id as "no smoketest
    #    child to wait on".
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "smoketest_enabled": smoketest_enabled,
        "started_at": started_at,
        "audit_task_id": audit_task_id,
        "smoketest_task_id": smoketest_task_id,
    }
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
        title=f"Qualifier smoketest: candidate {candidate_sha[:8]}",
        description=(
            f"Qualify-time smoketest of release candidate {candidate_sha}"
        ),
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
    # Only status=="pass" counts toward the score — a "skip" (or any non-pass
    # status) contributes 0.  This is intentionally conservative: a skipped
    # qualifier test is unverified coverage, so it should pull the score down
    # rather than be treated as a pass.
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
    """Finalize qualification once the qualifier children have terminated.

    Invoked by the Phase 3 batch-3 completion handlers (not wired here) after
    the release_audit child — and the qualify-smoketest child, when the
    smoketest subsystem was enabled for this run (audit H3) — for ``version``
    have reached a terminal state.

    Reads the RELEASE_AUDIT_REPORT and (when the smoketest subsystem was
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

    The caller commits the session.  Returns a result dict.
    """
    qual = dict(version.last_qualification or {})
    candidate_sha = qual.get("candidate_sha")
    ci_green = bool(qual.get("ci_green"))
    audit_task_id = qual.get("audit_task_id")
    smoketest_task_id = qual.get("smoketest_task_id")
    # Audit H3: whether the qualifying run had the smoketest subsystem enabled.
    # Read from the recorded qualification (NOT live runtime_config) — the
    # config could have flipped between spawn and finalize, and the composite
    # must match the children that actually ran.  Legacy records that predate
    # the flag default to True (the old always-spawn behaviour).
    smoketest_enabled = bool(qual.get("smoketest_enabled", True))

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

    # Resolve credentials now so promote() can pass them to _record_promotion_success
    # for tagging.  promote() would resolve them itself if omitted, but resolving here
    # avoids a second round-trip and lets the tag share the same token.
    project = await session.get(Project, version.project_id)
    resolved_creds: tuple[str, str, str] | None = None
    if project is not None:
        resolved_creds = await _resolve_github_with_session(session, project)

    if resolved_creds is not None:
        _token, _owner, _repo = resolved_creds
        return await promote(
            session,
            version,
            candidate_sha,
            composite=composite,
            base_record=base_record,
            token=_token,
            owner=_owner,
            repo=_repo,
        )
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
       ``version.last_qualification``.  Load the recorded ``Task`` rows.  The
       qualifier-smoketest child is optional (audit H3): when the qualifying
       run recorded ``smoketest_enabled == False`` there is no smoketest child
       and only the audit child gates finalization.  If a required child is
       missing or has not yet reached a terminal state (``COMPLETED``,
       ``FAILED``, ``CANCELLED``), return ``None`` — a child is still running.

    4. When all required children are terminal, delegate to
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

    # 3. Check that all required children are terminal.  The audit child is
    #    always required; the qualifier-smoketest child is required only when
    #    the qualifying run had the smoketest subsystem enabled (audit H3) —
    #    legacy records that predate the flag default to "required".
    smoketest_required = bool(qual.get("smoketest_enabled", True))
    audit_task_id_raw = qual.get("audit_task_id")
    smoketest_task_id_raw = qual.get("smoketest_task_id")

    if not audit_task_id_raw or (smoketest_required and not smoketest_task_id_raw):
        logger.warning(
            "[RELEASE_QUALIFIER] maybe_finalize_qualification: version v%d "
            "has pending qualification but missing required child task ids "
            "(audit=%r smoke=%r smoketest_required=%s) — skipping",
            version.version_number,
            audit_task_id_raw,
            smoketest_task_id_raw,
            smoketest_required,
        )
        return None

    try:
        audit_task_id = uuid.UUID(str(audit_task_id_raw))
        smoketest_task_id = (
            uuid.UUID(str(smoketest_task_id_raw))
            if smoketest_required and smoketest_task_id_raw
            else None
        )
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
    smoketest_task: Task | None = (
        await session.get(Task, smoketest_task_id)
        if smoketest_task_id is not None
        else None
    )

    if audit_task is None or (smoketest_required and smoketest_task is None):
        logger.debug(
            "[RELEASE_QUALIFIER] maybe_finalize_qualification: version v%d "
            "required child tasks not yet persisted — skipping",
            version.version_number,
        )
        return None

    if audit_task.status not in _TERMINAL_STATUSES or (
        smoketest_required
        and smoketest_task is not None
        and smoketest_task.status not in _TERMINAL_STATUSES
    ):
        # A required child is still running — not time to finalize yet.
        return None

    # 4. All required children are terminal — finalize.
    logger.info(
        "[RELEASE_QUALIFIER] maybe_finalize_qualification: required children "
        "terminal for version v%d (audit=%s/%s smoke=%s) — finalizing",
        version.version_number,
        audit_task.id,
        audit_task.status,
        f"{smoketest_task.id}/{smoketest_task.status}"
        if smoketest_task is not None
        else "disabled",
    )
    return await _finalize_qualification(session, version)


# ---------------------------------------------------------------------------
# sweep_stuck_qualifications — defense-in-depth reaper.
# ---------------------------------------------------------------------------


def _parse_started_at(qual: dict[str, Any]) -> datetime | None:
    """Parse ``last_qualification["started_at"]`` to a datetime, or None.

    ``started_at`` is an ISO-8601 string stamped by ``run_qualification``.
    Returns None when it is missing or unparseable so the caller can decide
    whether to treat the age as unknown (never time it out on a bad value).
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
            title=(
                f"Release qualification v{version.version_number} timed out"
            ),
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


async def sweep_stuck_qualifications(session: AsyncSession) -> int:
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
        ``CANCELLED``) — drives the version through
        ``maybe_finalize_qualification``, which re-checks every guard (row
        lock, still-pending, children-terminal) and delegates to
        ``_finalize_qualification``.  The qualifier-smoketest child is optional
        (audit H3): when the qualifying run recorded ``smoketest_enabled ==
        False`` only the audit child gates this branch.

      * **Pending longer than ``_MAX_QUALIFICATION_DURATION_SECONDS``** (audit
        H2) — force-finalizes the qualification as blocked (blocker
        ``qualification_timeout``) regardless of child state and raises an
        Alert.  This is the escape hatch for a child that heartbeats-but-hangs
        and so never reaches a terminal state.

    Audit H5: EACH version's finalize is wrapped in its own try/except and
    followed by ``await session.commit()`` on success.  Without the per-version
    commit, version B's iteration raising would unwind version A's already-
    landed work — and when version A's finalize promoted (a real GitHub merge
    landed on ``main``), the rolled-back ``status="promoted"`` + N+1 row would
    leave the DB inconsistent with GitHub.  Committing after each successful
    finalize makes a landed promotion durable before the next version is
    touched.

    Idempotent and safe to call repeatedly: a version already finalized (or
    not actually ready) is skipped by ``maybe_finalize_qualification``'s own
    guards.  Returns the number of versions that were actually finalized this
    sweep.
    """
    # Only ``open`` versions can be mid-qualification — a promoted/abandoned
    # version is terminal.  This keeps the scan bounded (≈ one open version per
    # project) and avoids a JSONB-path WHERE clause that differs across the
    # Postgres / SQLite-test dialects; the pending filter is applied in Python.
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
    # Snapshot just the IDs up front (audit H5).  The per-version commit /
    # rollback below expires every loaded instance, so iterating the ORM
    # objects directly would trip a MissingGreenlet on a later version's
    # expired-attribute access.  _sweep_one_version re-fetches each version
    # under its own FOR UPDATE lock, so the id is all that is needed here.
    version_ids = [v.id for v in open_versions]

    now = _utcnow()
    finalized = 0
    for version_id in version_ids:
        # Audit H5: each version's finalize is isolated in its own
        # try/except + commit — a failure on one version must NOT roll back
        # another version's already-committed finalization (a real GitHub
        # merge may have landed for it).
        try:
            did_finalize = await _sweep_one_version(session, version_id, now)
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: failed to finalize stuck "
                "qualification for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] sweep: rollback failed for version %s",
                    version_id,
                )
            continue

        if did_finalize:
            # Audit H5: commit this version's finalize before touching the
            # next one, so a landed promotion is durable immediately.
            try:
                await session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] sweep: commit failed after "
                    "finalizing version %s",
                    version_id,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] sweep: rollback failed for "
                        "version %s",
                        version_id,
                    )
                continue
            finalized += 1

    if finalized:
        logger.info(
            "[RELEASE_QUALIFIER] sweep finalized %d stuck qualification(s)",
            finalized,
        )
    return finalized


async def _sweep_one_version(
    session: AsyncSession,
    version_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Finalize one stuck pending qualification.  Returns True iff it finalized.

    Helper for ``sweep_stuck_qualifications`` — each call is wrapped by the
    caller in its own try/except + commit (audit H5).  Two paths, in priority
    order:

      1. Age-bound timeout (audit H2): if the qualification has been pending
         longer than ``_MAX_QUALIFICATION_DURATION_SECONDS``, force-finalize it
         as a ``qualification_timeout`` blocker regardless of child state.
      2. All required children terminal: drive it through
         ``maybe_finalize_qualification``.

    Takes ``SELECT ... FOR UPDATE`` on the version row up front so the
    child-state-independent timeout path can't race a concurrent
    ``maybe_finalize_qualification`` from a completion handler, and so the
    qualification is re-read from authoritative locked state (a completion
    handler may have finalized it since the caller's unlocked scan).  Path 2
    calls ``maybe_finalize_qualification``, which re-locks the same row — a
    no-op within this transaction since the lock is already held.
    """
    # Lock the version row (audit H2 race safety) and read its qualification
    # from the locked instance.
    version = (
        await session.execute(
            select(release_version.ReleaseVersion)
            .where(release_version.ReleaseVersion.id == version_id)
            .with_for_update()
        )
    ).scalars().first()
    if version is None:
        return False
    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        # Not pending (never started, or already finalized under the lock by a
        # completion handler) — nothing to do.
        return False

    # Path 1 — age-bound timeout (checked first, child-state-independent).
    started_at = _parse_started_at(qual)
    if started_at is not None:
        age_seconds = (now - started_at).total_seconds()
        if age_seconds > _MAX_QUALIFICATION_DURATION_SECONDS:
            await _force_finalize_timed_out_qualification(
                session, version, age_seconds
            )
            return True

    # Path 2 — all required children terminal.  The audit child is always
    # required; the qualifier-smoketest child is required only when the
    # qualifying run had the smoketest subsystem enabled (audit H3) — legacy
    # records that predate the flag default to "required".
    smoketest_required = bool(qual.get("smoketest_enabled", True))
    audit_id_raw = qual.get("audit_task_id")
    smoke_id_raw = qual.get("smoketest_task_id")
    if not audit_id_raw or (smoketest_required and not smoke_id_raw):
        return False
    try:
        audit_id = uuid.UUID(str(audit_id_raw))
        smoke_id = (
            uuid.UUID(str(smoke_id_raw))
            if smoketest_required and smoke_id_raw
            else None
        )
    except (ValueError, AttributeError):
        logger.warning(
            "[RELEASE_QUALIFIER] sweep: version v%d has unparseable child "
            "task ids (%r, %r) — skipping",
            version.version_number,
            audit_id_raw,
            smoke_id_raw,
        )
        return False

    audit_task = await session.get(Task, audit_id)
    smoke_task = (
        await session.get(Task, smoke_id) if smoke_id is not None else None
    )
    if audit_task is None or (smoketest_required and smoke_task is None):
        return False
    if audit_task.status not in _TERMINAL_STATUSES or (
        smoke_task is not None and smoke_task.status not in _TERMINAL_STATUSES
    ):
        return False

    # All required children terminal but the version is still pending — stuck.
    # Drive it through maybe_finalize_qualification, which re-locks the row and
    # re-validates every guard before finalizing.  Pass the terminal audit
    # child as the trigger task (it carries release_version_id in context,
    # exactly as the normal completion path would).
    logger.info(
        "[RELEASE_QUALIFIER] sweep: version v%d stuck pending with required "
        "children terminal — finalizing",
        version.version_number,
    )
    result = await maybe_finalize_qualification(session, audit_task)
    return result is not None


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

    Because the PR is pinned to ``candidate_sha`` there is NO staleness guard:
    ``automain`` moving on after qualification is expected and harmless — the
    release branch is frozen at the scored commit regardless.

    CRITICAL: ``github.merge_pull_request`` does NOT enforce CI and does not
    raise on a 405/409 — it returns ``{"merged": False}``.  This function
    checks ``result.get("merged")`` and treats a non-merged result as a FAILED
    promotion: it does NOT retry blindly.

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

    ``token``, ``owner``, ``repo`` may be pre-resolved by the caller
    (``_finalize_qualification`` already holds them); if omitted they are
    resolved here from the project row.

    The caller commits the session.  Returns a result dict.
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
        return _record_promotion_failure(
            session, version, base_record, "project_not_found"
        )

    if not token or not owner or not repo:
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return _record_promotion_failure(
                session, version, base_record, "no_credentials"
            )
        token, owner, repo = resolved

    # 0. Create the release branch pinned to the qualified candidate commit
    #    (audit C4).  Deterministic name so a retried promotion is idempotent.
    #    create_branch returns the existing ref on a 422; if that existing ref
    #    points at a different SHA (a stale branch from a failed prior attempt)
    #    delete + recreate it so the PR always carries exactly candidate_sha.
    release_branch = f"release-v{version.version_number}"
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
        return _record_promotion_failure(
            session,
            version,
            base_record,
            "release_branch_failed",
            detail=str(exc)[:300],
        )

    # 1. Open the release-branch → main PR.  The release branch is frozen at
    #    candidate_sha, so this PR promotes exactly the qualified commit.
    try:
        pr = await github.create_pull_request(
            token,
            owner,
            repo,
            title=(
                f"Release v{version.version_number}: promote "
                f"{release_branch} → main"
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
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    empty_diff: bool = False,
    promoted_sha: str | None = None,
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

    After the version is marked promoted, creates a git tag
    ``v{version.version_number}`` on the promoted SHA.  A tag-creation failure
    is non-fatal — it is wrapped in try/except and only logs a warning.  The
    merge already succeeded; the tag is cosmetic convenience.
    """
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
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    version.status = "promoted"
    version.promoted_at = now
    version.promoted_sha = effective_sha
    session.add(version)

    # Open the next version (build phase) — base_sha is the freshly-promoted SHA
    # (the real main HEAD after the merge, not the now-superseded candidate).
    next_version = await release_version.open_version(
        session,
        version.project_id,
        version_number=version.version_number + 1,
        base_sha=effective_sha,
    )
    logger.info(
        "[RELEASE_QUALIFIER] v%d promoted (sha=%s); opened v%d",
        version.version_number,
        effective_sha[:8],
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

    return {
        "status": "promoted",
        "promoted_sha": effective_sha,
        "next_version_number": next_version.version_number,
    }
