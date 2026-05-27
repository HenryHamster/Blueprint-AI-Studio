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
    Project,
    Task,
    TaskStatus,
)
from app.notification_dispatch import (
    post_version_auto_merge_stall_message,  # noqa: F401 — re-exported for tests
    post_version_build_stalled_message,  # noqa: F401 — re-exported for tests
    post_version_calcification_message,  # noqa: F401 — re-exported for tests
    post_version_ci_unresolved_stall_message,  # noqa: F401 — re-exported for tests
    post_version_promotion_readiness_message,  # noqa: F401 — re-exported for tests
    post_version_stabilize_stalled_message,  # noqa: F401 — re-exported for tests
)
from app.release_promotion import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for these symbols (tests access via ``rq.*``; other modules that
    # do ``from app.release_qualifier import promote`` keep working). The
    # implementations live in release_promotion.py; noqa pins them so a
    # ruff --fix sweep can't drop them as "unused" and silently break callers.
    _candidate_already_on_main,  # noqa: F401 — tests access via rq.*
    _idempotent_pr_url,  # noqa: F401 — tests access via rq.*
    _lts_branch_names,
    _notify_operator,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
    _record_promotion_failure,  # noqa: F401 — tests access via rq.*
    _record_promotion_success,  # noqa: F401 — tests access via rq.*
    _record_qualification_verdict,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
    _relock_version_for_promotion_write,  # noqa: F401 — tests access via rq.*
    _resolve_github_with_session,
    promote,  # noqa: F401 — re-exported for tests + release_qualifier_finalize (calls via rq.promote)
)
from app.release_qualifier_ci import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the qualifier-scoped CI resolvers.  _finalize_qualification
    # (still here) calls _resolve_ci_at_finalize via the module-global name so
    # tests that patch ``app.release_qualifier._resolve_ci_at_finalize`` (e.g.
    # test_agent.py) keep working; run_qualification calls _resolve_candidate_ci
    # via the same local name.  Tests also access these via ``rq.*``.  The
    # implementations live in release_qualifier_ci.py.
    _resolve_candidate_ci,
    _resolve_ci_at_finalize,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
)
from app.release_qualifier_infra import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the infra-failure branch (tests patch via
    # ``rq._MIN_INFRA_ERROR_STREAK`` / ``rq._handle_infra_failure``). The
    # implementations live in release_qualifier_infra.py.
    _MIN_INFRA_ERROR_STREAK,  # noqa: F401 — re-exported for tests + callers
    _handle_infra_failure,  # noqa: F401 — re-exported for tests + callers
)
from app.release_qualifier_io import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the qualifier I/O helpers.  run_qualification calls
    # _spawn_release_audit_task / _spawn_qualifier_smoketest_task via these
    # local symbols, _finalize_qualification uses _latest_artifact_logs, and
    # admin_digest imports get_latest_release_audit_report from here.  Tests
    # patch via ``rq.<name>``.  The implementations live in
    # release_qualifier_io.py; noqa pins them so a ruff --fix sweep can't drop
    # them as "unused" and silently break callers / patch sites.
    _latest_artifact_logs,  # noqa: F401 — re-exported for tests + callers
    _spawn_qualifier_smoketest_task,  # noqa: F401 — re-exported for tests + callers
    _spawn_release_audit_task,  # noqa: F401 — re-exported for tests + callers
    get_latest_release_audit_report,  # noqa: F401 — re-exported for admin_digest
)
from app.release_reconciliation import (
    _reconcile_if_pending_auto_merge_landed,  # noqa: F401 — re-exported for tests
    _reconcile_if_promotion_failed_landed,  # noqa: F401 — re-exported for tests
)
from app.release_qualifier_readiness import (
    _OUTCOME_LABELS,  # noqa: F401 — re-exported for back-compat
    _READINESS_CONFIDENCE_STDEV_MAX,  # noqa: F401 — re-exported for back-compat
    _READINESS_HISTORY_WINDOW,  # noqa: F401 — re-exported for back-compat
    _READINESS_MIN_CLEAN_STREAK,  # noqa: F401 — re-exported for back-compat
    _READINESS_MIN_VERDICTS,  # noqa: F401 — re-exported for back-compat
    _READINESS_MIN_WOULD_PROMOTE_RATE,  # noqa: F401 — re-exported for back-compat
    _READINESS_RECURRING_BLOCKER_MIN,  # noqa: F401 — re-exported for back-compat
    _empty_readiness,  # noqa: F401 — re-exported for back-compat
    compute_promotion_readiness,  # noqa: F401 — re-exported for back-compat
    format_qualification_for_api,  # noqa: F401 — re-exported for back-compat
    format_verdict_for_api,  # noqa: F401 — re-exported for back-compat
)
from app.release_scoring import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the scoring layer (tests mock via ``app.release_qualifier.<name>``
    # and other modules import these from here). The implementations live in
    # release_scoring.py; noqa pins them so a ruff --fix sweep can't drop them
    # as "unused" and silently break downstream importers / patch points.
    _AUDIT_DEDUCTION_UNKNOWN,  # noqa: F401
    _AUDIT_DEDUCTIONS,  # noqa: F401
    _CI_GREEN,  # noqa: F401 — re-exported for tests/back-compat
    _CI_NO_CHECKS,  # noqa: F401
    _CI_PENDING,  # noqa: F401 — re-exported for tests/back-compat
    _CI_RED,  # noqa: F401 — re-exported; release_qualifier_finalize reads via rq.*
    _DEFAULT_POLICY,
    _FINDING_SUMMARY_MAX,  # noqa: F401
    _MAX_CARRIED_FINDINGS,  # noqa: F401
    _SEVERITY_RANK,  # noqa: F401
    _clamp01,  # noqa: F401
    _classify_check_runs,  # noqa: F401 — re-exported for tests/back-compat
    _audit_missing_reason,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
    _extract_audit_findings,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
    _fmt_sub_scores,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
    _score_audit,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
    _score_smoketest,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
    compute_composite,  # noqa: F401 — re-exported; release_qualifier_finalize calls via rq.*
)
from app.release_stall_alerts import (
    _AUTO_MERGE_STALL_SECONDS,  # noqa: F401 — re-exported for back-compat
    _BUILD_STALL_AGE_DAYS,  # noqa: F401 — re-exported for back-compat
    _BUILD_STALL_MERGED_TASKS,  # noqa: F401 — re-exported for back-compat
    _STABILIZE_STALL_AGE_DAYS,  # noqa: F401 — re-exported for back-compat
    _STABILIZE_STALL_MERGED_TASKS,  # noqa: F401 — re-exported for back-compat
    _check_version_auto_merge_stall,  # noqa: F401 — re-exported for tests
    _check_version_build_stall,  # noqa: F401 — re-exported for tests
    _check_version_calcification,  # noqa: F401 — re-exported for tests
    _check_version_ci_unresolved_stall,  # noqa: F401 — re-exported for tests
    _check_version_promotion_readiness,  # noqa: F401 — re-exported for tests
    _check_version_stabilize_stall,  # noqa: F401 — re-exported for tests
    _is_build_stalled,  # noqa: F401 — re-exported for back-compat
    _MIN_CALCIFICATION_ATTEMPTS,  # noqa: F401 — re-exported for back-compat
)
from app.release_qualifier_finalize import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the finalize decision pipeline (tests call
    # ``rq._finalize_qualification`` and patch ``rq._BLOCKER_LABELS``).
    # ``_finalize_locked_version`` (still here) reaches _finalize_qualification
    # via the module-global lookup, so the re-export keeps test patches that
    # rebind ``rq._finalize_qualification`` effective.  The implementations
    # live in release_qualifier_finalize.py.
    _BLOCKER_LABELS,  # noqa: F401 — re-exported for tests + back-compat
    _finalize_qualification,  # noqa: F401 — re-exported for tests + back-compat
)
from app.release_qualifier_sweep import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the sweep machinery (reaper.py calls via
    # ``release_qualifier.sweep_stuck_qualifications``; tests patch via
    # ``rq.*``). The implementations live in release_qualifier_sweep.py.
    _MAX_QUALIFICATION_DURATION_SECONDS,  # noqa: F401 — used by _finalize_locked_version + re-exported
    _MAX_SWEEP_CRASH_STREAK,  # noqa: F401 — re-exported for tests/back-compat
    _force_finalize_sweep_crash_loop,  # noqa: F401 — re-exported for tests
    _force_finalize_timed_out_qualification,  # noqa: F401 — re-exported for tests
    _parse_started_at,  # noqa: F401 — re-exported for tests
    sweep_stuck_qualifications,  # noqa: F401 — re-exported; reaper.py calls via rq.*
)
from app.shared_state import (
    runtime_config as _runtime_config,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy — the trusted core.  weights + threshold come from a human-owned file.
# The hardcoded fallback rubric (_DEFAULT_POLICY) and the scoring math live in
# app.release_scoring; _load_policy stays here because it reads _POLICY_PATH
# (patched in tests via app.release_qualifier._POLICY_PATH) off the filesystem.
# ---------------------------------------------------------------------------

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


# Terminal task statuses — a qualifier child in any of these has finished
# (whether it succeeded, failed, or was cancelled).
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
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
# run_qualification — the coordinator (phase 1: spawn).
#
# The qualifier-scoped CI resolvers (_resolve_candidate_ci and
# _resolve_ci_at_finalize) live in app.release_qualifier_ci and are re-exported
# at the top of this file so module-global lookups + ``rq.*`` test patches keep
# working.
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

    # 2. Snapshot the candidate SHA — integration branch HEAD.  Audit C3: this
    #    GitHub round-trip is done BEFORE taking the ReleaseVersion row lock so
    #    the lock is not held across external I/O.
    integration_branch, promotion_target = _lts_branch_names(project)
    try:
        candidate_sha = await github.get_branch_sha(
            token, owner, repo, integration_branch
        )
    except Exception as exc:  # noqa: BLE001 — a missing branch / API error is recoverable
        logger.warning(
            "[RELEASE_QUALIFIER] could not snapshot %s SHA for %s/%s: %s",
            integration_branch,
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
    # Audit C4: the diff range is pinned to the exact snapshot commit
    # (promotion_target...<candidate_sha>) rather than the live integration
    # branch — the integration branch advances during the 10-30 min the
    # children take to run, and an un-pinned diff would audit a different
    # changeset than was qualified.
    diff_range = f"{promotion_target}...{candidate_sha}"
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
            base_branch=promotion_target,
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


# The qualifier I/O surface (_spawn_release_audit_task,
# _spawn_qualifier_smoketest_task, _latest_artifact_logs,
# get_latest_release_audit_report) lives in app.release_qualifier_io; the
# symbols are re-imported at the top of this module so callers and tests still
# reach them via app.release_qualifier.
#
# The audit/findings/smoketest scoring family (_score_audit,
# _extract_audit_findings, _score_smoketest and their _AUDIT_DEDUCTIONS /
# _MAX_CARRIED_FINDINGS / _SEVERITY_RANK constants) is pure and lives in
# app.release_scoring; the symbols are re-exported above so callers and tests
# still reach them via app.release_qualifier.


# ---------------------------------------------------------------------------
# _finalize_qualification — phase 2: collect + decide.
# Moved to release_qualifier_finalize.py; re-exported above so tests that call
# ``rq._finalize_qualification`` and patch ``rq._BLOCKER_LABELS`` keep working
# and ``_finalize_locked_version`` (still here) reaches the function via the
# module-global lookup.  Operator-notification rendering lives there too.
# ---------------------------------------------------------------------------


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
        # Skip the qualification timeout when the version is already awaiting a
        # promotion auto-merge.  Qualification completed and passed; the pending
        # clock now belongs to the promotion PR on main CI, not to qualifier
        # children.  The timeout backstop is only for qualifier children that
        # never reach a terminal state.  The auto-merge stall check (separate
        # lifecycle alert) handles the case where the PR is genuinely stuck.
        if qual.get("promotion_pending"):
            return None
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
# sweep_stuck_qualifications — moved to release_qualifier_sweep.py.
# Re-exported above via `from app.release_qualifier_sweep import ...`.
# ---------------------------------------------------------------------------
