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
from app.database import AsyncSessionLocal, resolve_async_engine
from app.failure_classification import (
    AGENT_TIMEOUT,
    PROVISION_FAILURE,
    RATE_LIMIT,
    classify_failure,
)
from app.acceptance_gate import (
    classify_merged_acceptance_context,
)
from app.landed_signal import landed_signal_predicate
from app.models import (
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)
from app.notification_dispatch import (
    dispatch_external_notification,  # noqa: F401 — re-exported for tests / compat patch point
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
    _candidate_already_on_main,
    _idempotent_pr_url,
    _lts_branch_names,
    _notify_operator,
    _record_promotion_failure,  # noqa: F401 — tests access via rq.*
    _record_promotion_success,
    _record_qualification_verdict,
    _recover_confidence_from_ci_unresolved,
    _relock_version_for_promotion_write,  # noqa: F401 — tests access via rq.*
    _resolve_github_with_session,
    promote,
)
from app.release_reconciliation import (
    _reconcile_if_deploy_failed_recovered,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_not_promoted_but_pr_merged,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_pending_auto_merge_landed,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_promotion_failed_landed,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_promotion_pending_timed_out,  # noqa: F401 — re-exported for back-compat
)
from app.release_qualifier_audit import (
    # Re-exports — release_qualifier stays the canonical import + patch surface
    # for the qualifier's audit/spawn and infra-failure seam.
    _MIN_INFRA_ERROR_STREAK,  # noqa: F401 — re-exported for callers/tests
    append_infra_failure_child_event,
    _enrich_infra_error,
    _handle_independent_blocker,  # noqa: F401 — re-exported for callers/tests
    _handle_infra_failure,
    _latest_artifact_logs,
    _select_lifecycle_audit,  # noqa: F401 — re-exported for callers/tests
    _spawn_qualifier_smoketest_task,
    _spawn_release_audit_challenge_task,
    _spawn_release_audit_task,
    get_infra_failure_stats,  # noqa: F401 — re-exported for callers/tests
    get_latest_qualification_audit_findings,  # noqa: F401 — re-exported for callers/tests
    get_latest_release_audit_challenge,  # noqa: F401 — re-exported for callers/tests
    get_latest_release_audit_report,  # noqa: F401 — re-exported for callers/tests
    resolve_lifecycle_audit_evidence,  # noqa: F401 — re-exported for callers/tests
)
from app.release_scoring import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the scoring layer (tests mock via ``app.release_qualifier.<name>``
    # and other modules import these from here). The implementations live in
    # release_scoring.py; noqa pins them so a ruff --fix sweep can't drop them
    # as "unused" and silently break downstream importers / patch points.
    _AUDIT_DEDUCTION_UNKNOWN,  # noqa: F401
    _AUDIT_DEDUCTIONS,  # noqa: F401
    _CI_GREEN,
    _CI_NO_CHECKS,  # noqa: F401
    _CI_PENDING,
    _CI_RED,
    _DEFAULT_POLICY,
    _FINDING_SUMMARY_MAX,
    _MAX_CARRIED_FINDINGS,  # noqa: F401
    _MAX_DELETED_TEST_FILES,  # noqa: F401 — re-exported for callers/tests
    _MAX_FAILED_SMOKE_TESTS,  # noqa: F401 — re-exported for callers/tests
    _MIN_TELEMETRY_SAMPLE,  # noqa: F401 — re-exported for callers/tests
    _SEVERITY_RANK,  # noqa: F401
    _SEVERITIES_REQUIRING_VERIFICATION,
    _audit_finding_key,  # noqa: F401 — re-exported for callers/tests
    _audit_findings_carry_meta,
    _audit_missing_reason,
    _clamp01,  # noqa: F401
    _classify_check_runs,
    _compute_audit_reconciliation,
    _compute_audit_challenge_synthesis,  # noqa: F401 — re-exported for callers/tests
    _compute_challenge_synthesis_from_binding_verdicts,
    _extract_audit_findings,
    _extract_excluded_audit_findings,
    _extract_smoketest_detail,
    _extract_test_erosion_detail,
    _finding_subject_is_authorized_divergence,
    _finding_is_verified,
    _fmt_sub_scores,
    _reconcile_audit_with_challenge,
    _score_audit,
    _score_smoketest,
    compute_challenged_vs_unchallenged_telemetry,  # noqa: F401 — re-exported for callers/tests
    compute_composite,
)
from app.acceptance_gate import summarize_version_acceptance_aggregate
from app.release_stall_alerts import (
    _AUTO_MERGE_STALL_SECONDS,  # noqa: F401 — re-exported for back-compat
    _BUILD_STALL_AGE_DAYS,  # noqa: F401 — re-exported for back-compat
    _BUILD_STALL_MERGED_TASKS,  # noqa: F401 — re-exported for back-compat
    _MIN_CALCIFICATION_ATTEMPTS,  # noqa: F401 — re-exported for back-compat
    _STABILIZE_STALL_AGE_DAYS,  # noqa: F401 — re-exported for back-compat
    _STABILIZE_STALL_MERGED_TASKS,  # noqa: F401 — re-exported for back-compat
    _check_version_auto_merge_stall,  # noqa: F401 — re-exported for tests
    _check_version_build_stall,  # noqa: F401 — re-exported for tests
    _check_version_calcification,  # noqa: F401 — re-exported for tests
    _check_version_ci_unresolved_stall,  # noqa: F401 — re-exported for tests
    _check_version_managed_check_wait_stall,  # noqa: F401 — re-exported for tests
    _check_version_promotion_readiness,  # noqa: F401 — re-exported for tests
    _check_version_stabilize_stall,  # noqa: F401 — re-exported for tests
    _is_build_stalled,  # noqa: F401 — re-exported for back-compat
)
from app.release_qualifier_sweep import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the reaper-sweep seam (tests access via ``rq.*`` / ``_rq.*``;
    # reaper_sweeps imports ``sweep_stuck_qualifications`` from here). The
    # implementations live in release_qualifier_sweep.py; ``_finalize_locked_version``
    # in this module calls the timeout helpers. noqa pins keep a ruff --fix
    # sweep from dropping the re-exports as "unused".
    _MAX_QUALIFICATION_DURATION_SECONDS,
    _MAX_SWEEP_CRASH_STREAK,  # noqa: F401 — re-exported for tests
    _force_finalize_sweep_crash_loop,  # noqa: F401 — re-exported for tests
    _force_finalize_timed_out_qualification,
    _parse_started_at,
    _redrive_deferred_unsynced_qualification,  # noqa: F401 — re-exported for tests
    sweep_stuck_qualifications,  # noqa: F401 — re-exported for reaper + tests
)
from app.migration_apply_gate import run_migration_apply_gate
from app.rollup_migration_validation import (
    check_qualification_migration_identity,
)
from app.release_readiness import (
    # Re-exports — release_qualifier is the canonical import surface for the
    # readiness layer (tests access via ``rq.*``; strategic_spawning and
    # admin_release_version import ``compute_promotion_readiness`` from here).
    # Implementations live in release_readiness.py; noqa pins them so a
    # ruff --fix sweep can't drop them as "unused" and silently break callers.
    _READINESS_CONFIDENCE_STDEV_MAX,  # noqa: F401 — re-exported for tests
    _READINESS_HISTORY_WINDOW,  # noqa: F401 — re-exported for tests
    _READINESS_MIN_CLEAN_STREAK,  # noqa: F401 — re-exported for tests
    _READINESS_MIN_VERDICTS,  # noqa: F401 — re-exported for tests
    _READINESS_MIN_WOULD_PROMOTE_RATE,  # noqa: F401 — re-exported for tests
    _READINESS_RECURRING_BLOCKER_MIN,  # noqa: F401 — re-exported for tests
    compute_promotion_readiness,  # noqa: F401 — re-exported for strategic_spawning + tests
    format_qualification_for_api,  # noqa: F401 — re-exported for tests
    format_verdict_for_api,  # noqa: F401 — re-exported for tests
    get_challenged_vs_unchallenged_telemetry,  # noqa: F401 — re-exported for callers/tests
)
from app.shared_state import (
    get_project_integration_branch,
    resolve_project_promotion_enabled,
    runtime_config as _runtime_config,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Conclusive qualification verdicts — when a candidate commit receives one of
# these verdicts, the outcome is settled and idempotent (relying on lucky
# re-rolls of a coin-flip margin is not sound). A re-run is only permitted
# when the integration branch advances to a new commit. Infra/error outcomes
# (infra_failure, error, etc.) are non-conclusive and always re-run, since they
# produced no real quality verdict.
_CONCLUSIVE_VERDICT_STATUSES: frozenset[str] = frozenset(
    {"promoted", "not_promoted", "dry_run"}
)

# ---------------------------------------------------------------------------
# Policy — the trusted core.  weights + threshold come from a human-owned file.
# The hardcoded fallback rubric (_DEFAULT_POLICY) and the scoring math live in
# app.release_scoring; _load_policy stays here because it reads _POLICY_PATH
# (patched in tests via app.release_qualifier._POLICY_PATH) off the filesystem.
# ---------------------------------------------------------------------------
# promotion_policy.yaml lives at backend/promotion_policy.yaml — two parents up
# from this file (backend/app/release_qualifier.py).
_POLICY_PATH = Path(__file__).resolve().parent.parent / "promotion_policy.yaml"


def _policy_from_runtime_config() -> dict[str, Any] | None:
    """Return the live promotion rubric from runtime_config, or None.

    The threshold + weights are seeded into runtime_config from
    promotion_policy.yaml at startup (shared_state._seed_promotion_policy) and
    are admin-tunable via PATCH /api/admin/settings, so calibrating the
    promotion bar no longer needs a redeploy.  This reads them at decision time.

    Returns the validated ``{"weights": {...}, "threshold": float}`` shape when
    runtime_config holds a usable rubric, else ``None`` so ``_load_policy``
    falls back to re-reading the YAML file / hardcoded defaults.  Validates the
    same invariants as the YAML loader (three float weights summing to ~1.0, a
    float threshold) — a malformed in-memory value must never reach the gate.
    """
    try:
        threshold_raw = _runtime_config.get("promotion_threshold")
        weights_raw = _runtime_config.get("promotion_weights")
        if threshold_raw is None or not isinstance(weights_raw, dict):
            return None
        weights = {
            "audit": float(weights_raw["audit"]),
            "test_erosion": float(weights_raw["test_erosion"]),
            "smoketest": float(weights_raw["smoketest"]),
        }
        threshold = float(threshold_raw)
        weight_sum = weights["audit"] + weights["test_erosion"] + weights["smoketest"]
        if abs(weight_sum - 1.0) > 0.01:
            raise ValueError(
                f"runtime_config promotion_weights sum to {weight_sum:.4f}, expected ~1.0"
            )
        return {"weights": weights, "threshold": threshold}
    except Exception as exc:  # noqa: BLE001 — fall back to the file/default on any failure
        logger.warning(
            "[RELEASE_QUALIFIER] runtime_config promotion rubric is unusable "
            "(%s); falling back to promotion_policy.yaml / defaults",
            exc,
        )
        return None


def _resolve_policy_with_provenance() -> tuple[dict[str, Any], str, str | None]:
    """Resolve the promotion rubric and return its active source + fallback reason.

    The policy payload itself keeps the legacy ``{"weights": ..., "threshold": ...}``
    shape. This helper layers operator-visible provenance on top so callers can tell
    whether the active rubric came from runtime config, the YAML file, or the
    hardcoded defaults after a YAML failure.
    """
    from_config = _policy_from_runtime_config()
    if from_config is not None:
        return from_config, "runtime_config", None

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

        return {"weights": weights, "threshold": threshold}, "yaml_file", None
    except Exception as exc:  # noqa: BLE001 — fall back on any failure, never raise
        error = f"{exc.__class__.__name__}: {exc}"
        logger.warning(
            "[RELEASE_QUALIFIER] failed to load promotion_policy.yaml (%s); "
            "falling back to hardcoded defaults",
            exc,
        )
        # Return a deep-ish copy so callers can't mutate the module constant.
        return (
            {
                "weights": dict(_DEFAULT_POLICY["weights"]),
                "threshold": _DEFAULT_POLICY["threshold"],
            },
            "hardcoded_defaults",
            error,
        )


def _load_policy() -> dict[str, Any]:
    """Resolve the promotion rubric: live runtime_config → YAML file → defaults.

    Resolution order:
      1. ``runtime_config['promotion_threshold'/'promotion_weights']`` — the
         live, admin-tunable rubric (seeded from the YAML at startup, then
         operator-overridable via PATCH /api/admin/settings with no redeploy).
      2. ``backend/promotion_policy.yaml`` — the on-disk file, when the
         runtime_config values are absent/unusable (e.g. a unit test importing
         the module without seeding).
      3. The hardcoded ``_DEFAULT_POLICY`` rubric.

    On ANY read or parse failure at the YAML tier — missing file, unreadable,
    malformed YAML, missing/non-numeric keys, PyYAML import failure — log a
    warning and return a copy of ``_DEFAULT_POLICY``.  Never raises: a broken
    policy source must not take down the qualifier (it would only fail-open the
    gate; the caller still decides whether to act on the composite).

    The returned dict always has the shape
    ``{"weights": {"audit": float, "test_erosion": float, "smoketest": float},
       "threshold": float}`` with every value coerced to float.
    """
    policy, _, _ = _resolve_policy_with_provenance()
    return policy


# Terminal task statuses — a qualifier child in any of these has finished
# (whether it succeeded, failed, was cancelled, or is BLOCKED on a precondition
# and so will not progress without intervention). BLOCKED is included so a
# precondition-blocked qualifier child does not read as perpetually in-flight.
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    }
)
_INFRA_FAILURE_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    }
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
    # The challenge child is required when the qualifying run recorded a
    # challenge_task_id — it must reach a terminal state before finalize can fire.
    required_keys = ["audit_task_id"]
    if qual.get("smoketest_enabled", True):
        required_keys.append("smoketest_task_id")
    if qual.get("challenge_task_id"):
        required_keys.append("challenge_task_id")

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


def _promotion_in_flight_result(
    version: release_version.ReleaseVersion,
) -> dict[str, Any] | None:
    """Return a promotion-blocking status dict, or None when not mid-promotion.

    The status echoes the blocking sub-state: ``promotion_in_flight`` (native
    GitHub auto-merge) or ``promotion_check_wait`` (the managed migration-
    carrying wait whose merge the reconciler retries after required checks pass).

    A version whose audit PASSED and now has an open promotion PR auto-merging
    (``last_qualification.status == "pending"`` + a ``promotion_pending``
    sub-key) is mid-promotion. Re-qualifying in this state would spawn a fresh
    audit/smoketest pair and clobber the promotion-pending record other parts of
    the system rely on as provenance that the promotion is still legitimately
    merging — so ``run_qualification`` refuses (no-op) just like the version-cut
    lever does (``abandon_current_version`` raises ``VersionCutBlockedByPromotion``
    on the same predicate). Reuses ``summarize_qualification_state`` so all the
    operator levers agree by construction.
    """
    qual_state = release_version.summarize_qualification_state(
        version.last_qualification
    )
    if (
        qual_state
        and qual_state.get("state") in release_version.PROMOTION_BLOCKING_STATES
    ):
        result = {
            # Echo the specific blocking sub-state (``promotion_in_flight`` for
            # native auto-merge, ``promotion_check_wait`` for the managed
            # migration-carrying wait) so callers can tell which hold fired.
            "status": qual_state["state"],
            "promotion_pr_number": qual_state.get("promotion_pr_number"),
            "promotion_pr_url": qual_state.get("promotion_pr_url"),
        }
        # Carry the additive age / auto-timeout freshness keys so the qualify 409
        # can surface elapsed age and near/past-timeout state (keys absent when
        # promotion_pending had no usable enabled_at).
        for key in release_version._PROMOTION_FRESHNESS_KEYS:
            if key in qual_state:
                result[key] = qual_state[key]
        return result
    return None


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
# These resolvers scope a stricter interpretation to the qualifier only — they
# pass ``treat_missing_as_success=False`` so an empty check-runs list is
# distinguishable, and resolve CI through the candidate's PR head when the
# candidate carries no checks of its own.  The shared helper's global
# fail-open behaviour is unchanged for every other caller.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# run_qualification — the coordinator (phase 1: spawn).
# ---------------------------------------------------------------------------


async def _is_target_absorbed(
    token: str,
    owner: str,
    repo: str,
    *,
    target_head: str,
    candidate_sha: str,
    project_id: str,
) -> bool | None:
    """Sync-gate check: is the promotion-target HEAD absorbed into the candidate?

    A thin wrapper over ``github.is_ancestor_of`` so the qualify-on-synced gate
    has its OWN mockable seam — ``is_ancestor_of`` is also used by ``promote()``
    (promote-idempotency: "is this candidate already on main?") and tested
    directly, so the gate must not share their mock. Returns True (absorbed /
    synced), False (not absorbed), or None (indeterminate compare).
    """
    return await github.is_ancestor_of(
        token, owner, repo, target_head, candidate_sha, project_id=project_id
    )


async def _check_lts_sync_gate(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    token: str,
    owner: str,
    repo: str,
    candidate_sha: str,
    integration_branch: str,
    promotion_target: str,
) -> dict[str, Any] | None:
    """Qualify-on-synced gate: don't score a candidate that is behind ``main``.

    Under an LTS split, a candidate snapshotted from the integration branch can
    be BEHIND the promotion target — ``main`` landed commits the integration
    branch has not back-merged yet. Scoring such a candidate wastes a full audit:
    the eventual ``release-vN -> main`` promotion merge can break CI on a
    *semantic* conflict and bounce the version back to stabilize. Qualifying only
    a SYNCED candidate (whose tree already contains ``main``) makes that break
    structurally impossible.

    Returns ``None`` to PROCEED — synced, an indeterminate compare, or the
    bounded fall-through fired — or a retryable ``{"status": "deferred_unsynced",
    ...}`` to DEFER. A deferral is NEVER dropped: the strategic ``qualify_request``
    / cadence re-emits and this re-gates each attempt, while ``first_deferred_at``
    (persisted on ``last_qualification``) bounds the wait so a wedged back-merge
    can never starve promotion.

    Degrade-safe: any GitHub error or indeterminate result PROCEEDS rather than
    blocks — the promotion bounce + back-merge CI-fix loop remain the backstop,
    and the gate must never become a wedge of its own.
    """
    # Promotion-target HEAD — one extra read; on any failure, proceed ungated.
    try:
        target_head = await github.get_branch_sha(token, owner, repo, promotion_target)
    except Exception as exc:  # noqa: BLE001 — a flaky read must not block qualification
        logger.warning(
            "[RELEASE_QUALIFIER] sync-gate: could not resolve %s HEAD (%s) — "
            "proceeding ungated",
            promotion_target,
            exc,
        )
        return None

    # Is the promotion target already absorbed into the candidate's tree?
    absorbed = await _is_target_absorbed(
        token,
        owner,
        repo,
        target_head=target_head,
        candidate_sha=candidate_sha,
        project_id=str(version.project_id),
    )
    if absorbed is not False:
        # True (synced) or None (indeterminate compare) → proceed. A later
        # normal last_qualification write replaces any prior deferral note.
        return None

    # absorbed is False → main has commits the candidate lacks → unsynced.
    prior = version.last_qualification or {}
    first = _utcnow()
    if prior.get("status") == "deferred_unsynced" and prior.get("first_deferred_at"):
        try:
            parsed = datetime.fromisoformat(
                str(prior["first_deferred_at"]).replace("Z", "+00:00")
            )
            first = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except (ValueError, TypeError):
            pass  # malformed — restart the clock from now (fail toward firing sooner)
    waited_min = max(0.0, (_utcnow() - first).total_seconds() / 60.0)
    fall_through_min = _runtime_config.get("qualify_unsynced_fall_through_minutes", 30)

    if fall_through_min and waited_min >= fall_through_min:
        # Bounded fall-through: a wedged back-merge must never starve promotion.
        logger.warning(
            "[RELEASE_QUALIFIER] sync-gate: v%d candidate %s has been behind %s "
            "for %.0fmin (>= %smin) — qualifying ANYWAY; the %s -> %s back-merge "
            "may be stuck",
            version.version_number,
            candidate_sha[:8],
            promotion_target,
            waited_min,
            fall_through_min,
            promotion_target,
            integration_branch,
        )
        await _notify_operator(
            session,
            version,
            "qualify_unsynced_fall_through",
            (
                f"v{version.version_number}: {integration_branch} has not absorbed "
                f"{promotion_target} for {waited_min:.0f}min, so the qualify-sync "
                f"gate's fall-through fired — scoring the candidate anyway. Check "
                f"the {promotion_target} -> {integration_branch} back-merge."
            ),
            extra={
                "candidate_sha": candidate_sha,
                "waited_minutes": round(waited_min),
                "fall_through_minutes": fall_through_min,
            },
        )
        return None

    # Defer (retryable — the re-emitted qualify_request / cadence re-enters).
    version.last_qualification = {
        "status": "deferred_unsynced",
        "candidate_sha": candidate_sha,
        "promotion_target": promotion_target,
        "first_deferred_at": first.isoformat(),
        "deferred_at": _utcnow().isoformat(),
        "reason": (
            f"{integration_branch} has not absorbed {promotion_target} "
            f"(a {promotion_target} -> {integration_branch} back-merge is "
            f"pending); deferring to avoid scoring a candidate that would fail "
            f"the promotion merge"
        ),
    }
    flag_modified(version, "last_qualification")
    session.add(version)
    logger.info(
        "[RELEASE_QUALIFIER] sync-gate: deferring qualification of v%d — "
        "candidate %s is behind %s (waited %.0f/%smin); retries when synced",
        version.version_number,
        candidate_sha[:8],
        promotion_target,
        waited_min,
        fall_through_min,
    )
    return {"status": "deferred_unsynced", "candidate_sha": candidate_sha}


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
      * a conclusive qualification verdict (promoted/not_promoted/dry_run) was
        reached for this candidate commit and ``automain`` has not advanced
        since ⇒ ``{"status": "cached_verdict", ...}``.  A conclusive verdict is
        settled and idempotent — relying on lucky re-rolls of an LLM audit at
        the margin is not sound. Once ``automain`` advances to a new commit,
        that unscored candidate triggers a fresh qualification.  Non-conclusive
        outcomes (infra_failure, error) always re-run when re-requested since
        they produced no real quality verdict.

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
         cached-verdict guards under the lock.
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
    ``"wrong_phase"`` / ``"already_pending"`` / ``"cached_verdict"`` when a
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

    # Cheap pre-lock promotion-in-flight check (no I/O — pure DB read).  A
    # version whose audit passed and now has an open promotion PR auto-merging
    # must not be re-qualified: doing so spawns a fresh heavy audit/smoketest
    # pair and clobbers the promotion-pending record.  Consistent with the
    # version-cut lever, which refuses in the same state.  Best-effort fast path
    # — the authoritative re-check happens under the row lock below.
    _promo = _promotion_in_flight_result(version)
    if _promo is not None:
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d has a promotion "
            "in flight — skipping (promotion_in_flight, pre-lock)",
            version.version_number,
        )
        return _promo

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

    # 3b. Qualify-on-synced gate (LTS split only).  Don't score a candidate that
    #     is behind the promotion target — it would fail the promotion merge and
    #     bounce the version back to stabilize, burning a full re-audit.  Bounded,
    #     degrade-safe, and never drops the request (see _check_lts_sync_gate).
    #     Runs pre-lock (read-only GitHub I/O) like the candidate snapshot + CI
    #     check above, so no row lock is held across the compare.
    if integration_branch != promotion_target:
        _sync_gate = await _check_lts_sync_gate(
            session,
            version,
            token=token,
            owner=owner,
            repo=repo,
            candidate_sha=candidate_sha,
            integration_branch=integration_branch,
            promotion_target=promotion_target,
        )
        if _sync_gate is not None:
            return _sync_gate

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

    # Guard 2b (under the lock): a promotion is in flight — the audit PASSED and
    # a promotion PR is open and auto-merging (last_qualification.status ==
    # "pending" + a promotion_pending sub-key, children already terminal).  This
    # is NOT "in flight" by the child-task definition above, and "pending" is not
    # a conclusive verdict, so neither Guard 2 nor Guard 3 catches it.  Spawning
    # a fresh audit/smoketest pair here and overwriting the promotion-pending
    # record would discard the provenance the rest of the system relies on to
    # know the promotion is still legitimately merging — so refuse (no-op),
    # consistent with the version-cut lever.
    _promo = _promotion_in_flight_result(version)
    if _promo is not None:
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d has a promotion "
            "in flight — skipping (promotion_in_flight)",
            version.version_number,
        )
        return _promo

    # Guard 3 (under the lock): a conclusive qualification verdict
    # (promoted/not_promoted/dry_run) for this candidate commit has been reached
    # and automain has not advanced. A conclusive verdict is settled and
    # idempotent — re-scoring identical code can yield a different (potentially
    # lucky) result, so that verdict is not re-rolled on the same commit.
    # Infra/error outcomes are non-conclusive; they must re-run when the
    # qualification is re-requested.  Once automain moves to a new commit, that
    # new candidate is unscored and a fresh qualification is always permitted.
    prior_qual = version.last_qualification or {}
    prior_status = prior_qual.get("status")
    if (
        prior_status in _CONCLUSIVE_VERDICT_STATUSES
        and prior_qual.get("candidate_sha") == candidate_sha
    ):
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d already has a "
            "conclusive %s verdict for candidate %s (automain unchanged) — "
            "skipping (cached verdict)",
            version.version_number,
            prior_status,
            candidate_sha[:8],
        )
        return {
            "status": "cached_verdict",
            "cached_verdict_status": prior_status,
            "candidate_sha": candidate_sha,
        }

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
    prior_infra_failure_signature = (
        prior_qual.get("infra_failure_signature")
        if prior_qual.get("status") == "infra_failure"
        else None
    )
    prior_infra_failure_candidate_sha = (
        prior_qual.get("candidate_sha")
        if prior_qual.get("status") == "infra_failure"
        else None
    )
    infra_attempt_history = (
        prior_qual.get("infra_attempt_history")
        if prior_qual.get("status") == "infra_failure"
        else []
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
    challenge_task_id: str | None = None
    audit_challenge_task_id: str | None = None
    audit_challenge_spawn_error: str | None = None
    audit_challenge_spawn_pending = True
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
        "challenge_task_id": challenge_task_id,
        "audit_challenge_task_id": audit_challenge_task_id,
        "audit_challenge_spawn_error": audit_challenge_spawn_error,
        "audit_challenge_spawn_pending": audit_challenge_spawn_pending,
        "smoketest_task_id": smoketest_task_id,
        "prior_infra_streak": prior_infra_streak,
        "previous_infra_failure_signature": prior_infra_failure_signature,
        "previous_infra_failure_candidate_sha": prior_infra_failure_candidate_sha,
        "infra_attempt_history": infra_attempt_history,
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
        "challenge_pending=%s smoketest_task=%s",
        version.version_number,
        project_id,
        candidate_sha[:8],
        ci_green,
        smoketest_enabled,
        audit_task_id,
        audit_challenge_spawn_pending,
        smoketest_task_id,
    )
    return {
        "status": "spawned",
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "ci_state": ci_state,
        "smoketest_enabled": smoketest_enabled,
        "audit_task_id": audit_task_id,
        "challenge_task_id": challenge_task_id,
        "audit_challenge_task_id": audit_challenge_task_id,
        "audit_challenge_spawn_error": audit_challenge_spawn_error,
        "audit_challenge_spawn_pending": audit_challenge_spawn_pending,
        "smoketest_task_id": smoketest_task_id,
    }


# ---------------------------------------------------------------------------
# _finalize_qualification — phase 2: collect + decide.
# The child-task spawn / artifact-read / infra-failure seam lives in
# app.release_qualifier_audit and is re-exported above for back-compat.
# ---------------------------------------------------------------------------

# Maximum number of times a RATE_LIMIT-classified release_audit task will be
# re-spawned per qualification run before falling through to _handle_infra_failure.
_MAX_AUDIT_RL_RESPAWNS = 2
_MAX_AUDIT_CHALLENGE_BINDING_RESPAWNS = 2
_AUDIT_CHALLENGE_BINDING_HOLDS: frozenset[str] = frozenset(
    {
        "audit_challenge_missing",
        "audit_challenge_binding_incomplete",
        "audit_challenge_uncertain",
        "audit_challenge_sha_mismatch",
        "audit_challenge_empty_verdicts",
    }
)

# Human-readable rendering of the compute_composite() hard-blocker codes.
_BLOCKER_LABELS: dict[str, str] = {
    "ci_not_green": "CI is not green on the candidate",
    "ci_unresolved": "CI state could not be evaluated (pending, no check-runs, or GitHub I/O degraded)",
    "smoketest_startup_failed": "the smoketest startup suite failed",
    "audit_missing": "the release audit produced no usable report",
    "test_file_deleted": "the diff deleted a test file",
    "migration_identity_collision": (
        "an Alembic revision-identity collision between the candidate and the "
        "deployed promotion target"
    ),
    "migration_apply_drift": (
        "the candidate's migration chain does not apply cleanly against "
        "production's recorded schema state (deploy-fatal drift)"
    ),
}


def _primary_findings_for_challenge_retry(
    audit_report: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Return deductible primary audit findings for a targeted challenge retry."""
    if not isinstance(audit_report, dict):
        return []
    findings = audit_report.get("findings")
    if not isinstance(findings, list):
        findings = audit_report.get("issues")
    if not isinstance(findings, list):
        return []

    primary_findings: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or finding.get("level") or "").lower()
        if severity not in _SEVERITIES_REQUIRING_VERIFICATION:
            continue
        if not _finding_is_verified(finding, severity):
            continue
        if _finding_subject_is_authorized_divergence(finding):
            continue
        summary = str(
            finding.get("summary")
            or finding.get("description")
            or finding.get("title")
            or finding.get("text")
            or ""
        ).strip()[:_FINDING_SUMMARY_MAX]
        if not summary:
            continue
        primary_findings.append(
            {
                "severity": severity,
                "summary": summary,
                # Cap file path: audit artifacts are external task output and
                # must be treated as untrusted. A very long or numerous file
                # field can push validate_internal_payload over PayloadTooLargeError.
                "file": str(
                    finding.get("file") or finding.get("file_path") or ""
                ).strip()[:_FINDING_SUMMARY_MAX],
            }
        )
    # Sort by severity rank (most severe first). Do not cap this binding list:
    # reconciliation requires a verdict for every verified deductible finding,
    # so the challenge prompt must cover the same domain. Payload size is still
    # guarded by validate_internal_payload at spawn time.
    primary_findings.sort(key=lambda d: _SEVERITY_RANK.get(d["severity"], 99))
    return primary_findings


def _audit_task_matches_candidate(task: Task | None, candidate_sha: str | None) -> bool:
    """Return True when the completed audit task is pinned to the candidate."""
    if task is None or task.status != TaskStatus.COMPLETED or not candidate_sha:
        return False
    context_sha = str((task.context or {}).get("candidate_sha") or "").strip()
    if context_sha:
        return context_sha == candidate_sha
    payload_sha = str((task.first_payload or {}).get("candidate_sha") or "").strip()
    return bool(payload_sha and payload_sha == candidate_sha)


def _build_audit_leg_proof(
    *,
    candidate_sha: str | None,
    audit_task_id: str | None,
    audit_task_candidate_sha: str | None,
    challenge_task_id: str | None,
    challenged_candidate_sha: str | None,
    binding_expected_count: int,
    binding_record_count: int,
    reconciliation_status: str | None,
    reconciliation_reason: str | None,
    legacy_advisory_only: bool,
) -> dict[str, Any]:
    """Derive the machine-readable audit-leg proof snapshot for a verdict."""
    required = binding_expected_count > 0 and not legacy_advisory_only
    binding_complete = (
        binding_expected_count > 0 and binding_record_count == binding_expected_count
    )
    audit_sha_pinned = bool(
        candidate_sha
        and audit_task_candidate_sha
        and audit_task_candidate_sha == candidate_sha
    )
    challenge_sha_pinned = bool(
        candidate_sha
        and challenged_candidate_sha
        and challenged_candidate_sha == candidate_sha
    )
    reconciliation_success = (
        reconciliation_status == "available" and reconciliation_reason is None
    )
    if not required:
        state = "not_required"
    elif (
        audit_sha_pinned
        and challenge_sha_pinned
        and binding_complete
        and reconciliation_success
    ):
        state = "proven"
    else:
        state = "unproven"
    return {
        "state": state,
        "required": state == "proven",
        "candidate_sha": candidate_sha,
        "audit_task_id": audit_task_id,
        "audit_task_candidate_sha": audit_task_candidate_sha,
        "challenge_task_id": challenge_task_id,
        "challenged_candidate_sha": challenged_candidate_sha,
        "binding_expected_count": binding_expected_count,
        "binding_record_count": binding_record_count,
        "binding_complete": binding_complete,
        "reconciliation_status": reconciliation_status,
        "reconciliation_reason": reconciliation_reason,
    }


async def _resolve_migration_identity_at_finalize(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> tuple[str, str | None]:
    """Run the deterministic Alembic revision-identity gate at finalize time.

    Resolves the project's GitHub credentials and branch names, then delegates
    to :func:`check_qualification_migration_identity`, which lists the
    candidate's migration filenames (at ``candidate_sha``) and the deployed
    promotion target's current migration filenames (at the promotion-target
    branch from ``_lts_branch_names``) and runs the existing
    ``find_content_divergent_revisions`` detector over the two.

    Returns the ``("ok"|"block"|"defer", detail)`` verdict:
      * ``"ok"``    — no collision; qualification proceeds normally.
      * ``"block"`` — a confirmed collision; the caller turns this into the
        ``migration_identity_collision`` hard blocker (confidence forced to 0).
      * ``"defer"`` — the candidate's / target's migration LIST could not be
        fetched; the caller HOLDS the release for this attempt rather than waving
        it through (a fetch error is not "no collision").

    The gate is deliberately scoped to migration-LIST fetch failures. A MISSING
    ``candidate_sha``, a vanished project, or unresolved GitHub credentials
    return ``("ok", None)`` so the gate stays inert for those edges — they are
    ALREADY handled upstream (the promote path's missing-SHA guard; the
    finalize-time CI re-resolution, which degrades to the ``ci_unresolved`` hard
    blocker on a missing project / credentials). Letting the gate hold on those
    too would only double-handle a case CI already blocks, never wave it through.
    """
    if not candidate_sha:
        # The check cannot run without a candidate ref; the promote path's
        # existing missing-SHA guard handles this case (it returns an error and
        # stays in stabilize), so do not perturb it here.
        return ("ok", None)
    try:
        project = await session.get(Project, version.project_id)
        if project is None:
            # A vanished project is already a ci_unresolved hard blocker via the
            # finalize CI re-resolution — keep the gate inert rather than convert
            # that into an infra failure.
            return ("ok", None)
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            # No credentials → CI re-resolution already degrades to not-green
            # (ci_unresolved blocker); the gate stays inert (CI blocks anyway).
            return ("ok", None)
        token, owner, repo = resolved
        _integration_branch, promotion_target = _lts_branch_names(project)
        verdict, detail = await check_qualification_migration_identity(
            token,
            owner,
            repo,
            candidate_sha,
            promotion_target,
            project_id=str(version.project_id),
        )
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-identity gate for v%d "
            "candidate %s vs %s: %s (%s)",
            version.version_number,
            candidate_sha[:8],
            promotion_target,
            verdict,
            detail or "no divergence",
        )
        return verdict, detail
    except Exception as exc:  # noqa: BLE001 — any failure HOLDS (never waves through)
        logger.warning(
            "[RELEASE_QUALIFIER] finalize migration-identity gate failed for "
            "v%d (holding this attempt): %s",
            version.version_number,
            exc,
        )
        return ("defer", f"migration-identity gate error: {exc}")


async def _resolve_migration_apply_at_finalize(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> tuple[str, str | None]:
    """Run the scratch-DB migration-apply gate at finalize time.

    Distinct from :func:`_resolve_migration_identity_at_finalize` (which
    compares migration FILENAMES for revision-id collisions): this dimension
    reproduces the deployed upgrade against an isolated scratch DB seeded to
    production's REAL recorded state (the live ``alembic_version`` stamp plus the
    tables actually present) and asserts the resulting schema satisfies every
    required object.  It catches the live-DB-stamped-ahead-of-real-schema class
    (the v5 computejob renumber-skip) that the filename check structurally
    cannot see.

    Resolves the project's GitHub credentials and promotion-target branch, gets
    the production engine via :func:`resolve_async_engine`, then delegates to
    :func:`run_migration_apply_gate` — which owns ALL scratch-DB orchestration
    (provision, seed, apply, drift-check).  This helper only adapts the qualifier
    context to that engine's keyword contract; it does NOT reimplement it.

    Returns the ``("ok"|"block"|"defer", detail)`` verdict:
      * ``"ok"``    — the candidate applies cleanly; qualification proceeds.
      * ``"block"`` — the resulting schema is missing a required object; the
        caller turns this into the ``migration_apply_drift`` hard blocker
        (confidence forced to 0).
      * ``"defer"`` — production's state could not be read, the candidate's
        migrations could not be fetched, or the scratch DB could not be
        provisioned; the caller HOLDS the release rather than waving it through.

    Mirrors the identity gate's inert edges: a MISSING ``candidate_sha``, a
    vanished project, or unresolved GitHub credentials return ``("ok", None)``
    so the gate stays inert for cases CI already blocks (the missing-SHA guard;
    the finalize CI re-resolution degrading to ``ci_unresolved``).
    """
    if not candidate_sha:
        return ("ok", None)
    try:
        project = await session.get(Project, version.project_id)
        if project is None:
            return ("ok", None)
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return ("ok", None)
        token, owner, repo = resolved
        prod_engine = resolve_async_engine(session)
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            gh_token=token,
            owner=owner,
            repo=repo,
            candidate_ref=candidate_sha,
            project_id=str(version.project_id),
        )
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-apply gate for v%d "
            "candidate %s: %s (%s)",
            version.version_number,
            candidate_sha[:8],
            verdict,
            detail or "applies cleanly",
        )
        return verdict, detail
    except Exception as exc:  # noqa: BLE001 — any failure HOLDS (never waves through)
        logger.warning(
            "[RELEASE_QUALIFIER] finalize migration-apply gate failed for "
            "v%d (holding this attempt): %s",
            version.version_number,
            exc,
        )
        return ("defer", f"migration-apply gate error: {exc}")


async def _compute_acceptance_window_aggregate(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> dict[str, Any]:
    """Count merged tasks in this version's window with explicit verified acceptance.

    Queries all COMPLETED tasks with the landed-signal predicate that completed
    at or after ``version.opened_at`` (the version window lower bound), then
    in-Python applies the canonical acceptance-bucket classifier: a merge is
    acceptance-verified only when it lands in the ``proven`` or
    ``satisfied_unproven`` buckets. Undeclared (legacy), degraded, explicitly
    unsatisfied, and malformed rows do not count as verified.

    Returns:
      {
        "acceptance_window_total": int,    # all landed merges in the window
        "acceptance_window_verified": int, # merges with explicit declared-and-verified acceptance
        "acceptance_window_fraction": float | None,  # None when total == 0
      }
    """

    since = version.opened_at
    stmt = select(Task).where(
        Task.project_id == version.project_id,
        Task.status == TaskStatus.COMPLETED,
        landed_signal_predicate(session),
        Task.updated_at >= since,
    )
    rows = [
        task
        for task in (await session.execute(stmt)).scalars().all()
        if release_version._task_completed_at(task) >= since
    ]

    total = len(rows)
    verified = sum(
        1
        for task in rows
        if classify_merged_acceptance_context(task.context or {})
        in {"proven", "satisfied_unproven"}
    )
    fraction = (verified / total) if total > 0 else None
    return {
        "acceptance_window_total": total,
        "acceptance_window_verified": verified,
        "acceptance_window_fraction": fraction,
    }


async def _guard_ci_unresolved_promotion_in_flight(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
    base_record: dict[str, Any],
    token: str,
    owner: str,
    repo: str,
    project: Any,
) -> dict[str, Any] | None:
    """Guard: before writing not_promoted for ci_unresolved, check for in-flight.

    Incident 2026-06-09: a transient DB error rolled back the promotion_pending
    marker write after a release PR was created and auto-merge was armed. A
    subsequent sweep re-evaluated CI as ci_unresolved (transient, not red) and
    wrote not_promoted — clobbering a promotion in flight.

    This guard runs UNDER the version row lock (caller holds it from
    _finalize_locked_version) before the not_promoted write. GitHub I/O here is
    quick (ancestry + open-PR fetch), consistent with _resolve_ci_at_finalize's
    existing pattern of doing brief API calls under the lock.

    (a) Candidate already on main — the PR merged while promotion_pending was
        being committed and the commit rolled back. Record success instead.
    (b) Open release PR still exists — auto-merge is armed or pending CI.
        Restore the promotion_pending sub-state so the reaper can confirm the
        merge later rather than writing a spurious not_promoted.

    Returns a result dict if the guard handled the situation (caller should
    return early), or None to proceed with the normal not_promoted write.
    """
    if not candidate_sha:
        return None

    integration_branch, promotion_target = _lts_branch_names(project)
    if integration_branch == promotion_target:
        logger.debug(
            "[RELEASE_QUALIFIER] ci_unresolved guard: v%d integration branch "
            "(%s) is the promotion target — single-branch repo, LTS inactive; "
            "nothing to guard",
            version.version_number,
            promotion_target,
        )
        return None

    if await _candidate_already_on_main(
        token, owner, repo, candidate_sha, version, project=project
    ):
        logger.info(
            "[RELEASE_QUALIFIER] ci_unresolved guard: candidate %s for v%d is "
            "already on main — recording promotion success instead of not_promoted",
            candidate_sha[:8],
            version.version_number,
        )
        promoted_sha: str | None = None
        try:
            promoted_sha = await github.get_branch_sha(
                token, owner, repo, promotion_target
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RELEASE_QUALIFIER] ci_unresolved guard: could not resolve %s "
                "HEAD SHA for v%d (%s) — using candidate_sha as promoted_sha",
                promotion_target,
                version.version_number,
                exc,
            )
        return await _record_promotion_success(
            session,
            version,
            candidate_sha,
            _recover_confidence_from_ci_unresolved(base_record),
            pr_url=_idempotent_pr_url(base_record),
            token=token,
            owner=owner,
            repo=repo,
            promoted_sha=promoted_sha,
            already_landed=True,
            lock_released=False,
        )

    # Candidate not yet on main — check whether the release PR is still open
    # (auto-merge armed, PR CI still pending). If so, restore promotion_pending
    # so the reaper confirms the merge rather than recording a spurious miss.
    release_branch = f"release-v{version.version_number}"
    try:
        open_prs = await github.get_open_pull_requests(
            token, owner, repo, base=promotion_target
        )
        open_release_pr = next(
            (
                pr
                for pr in open_prs
                if (pr.get("head") or {}).get("ref") == release_branch
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[RELEASE_QUALIFIER] ci_unresolved guard: could not check for open "
            "release PR for v%d (%s) — proceeding with not_promoted",
            version.version_number,
            exc,
        )
        return None

    if open_release_pr is None:
        return None

    pr_number = open_release_pr.get("number")
    pr_url = open_release_pr.get("html_url") or _idempotent_pr_url(base_record)
    auto_merge_enabled = open_release_pr.get("auto_merge") is not None
    logger.info(
        "[RELEASE_QUALIFIER] ci_unresolved guard: open release PR #%s for v%d "
        "found — restoring promotion_pending instead of writing not_promoted "
        "(auto_merge=%s)",
        pr_number,
        version.version_number,
        auto_merge_enabled,
    )
    # Detect whether the open release PR carries Alembic migrations.  The
    # fail-safe default is True (migration-carrying): if the file-list fetch
    # fails we must NOT route to the native-auto-merge path that bypasses
    # Blueprint's re-entry guard.
    from app.rollup_migration_validation import pr_touches_migrations

    is_migration_pr = True
    try:
        is_migration_pr = await pr_touches_migrations(token, owner, repo, pr_number)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[RELEASE_QUALIFIER] ci_unresolved guard: could not determine "
            "migration status for PR #%s (v%d) (%s) — defaulting to "
            "migration_carrying=True",
            pr_number,
            version.version_number,
            exc,
        )

    promotion_pending: dict[str, Any] = {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "auto_merge_enabled": auto_merge_enabled,
        "enabled_at": _utcnow().isoformat(),
        "restored_by_ci_unresolved_guard": True,
    }
    if is_migration_pr:
        promotion_pending["migration_carrying"] = True
        promotion_pending["managed_check_wait"] = True
        promotion_pending["why_not_native_automerge"] = "migration_guard_deferred"

    q = dict(version.last_qualification or {})
    q.update(_recover_confidence_from_ci_unresolved(base_record))
    q["status"] = "pending"
    q["promotion_pending"] = promotion_pending
    version.last_qualification = q
    flag_modified(version, "last_qualification")
    session.add(version)
    result_status = (
        "pending_managed_check_wait" if is_migration_pr else "pending_auto_merge"
    )
    return {
        "status": result_status,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "auto_merge_enabled": auto_merge_enabled,
        "managed_check_wait": is_migration_pr,
    }


async def _finalize_qualification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    _pre_gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize qualification once the qualifier children have terminated.

    Invoked by ``_finalize_locked_version`` after the release_audit child — and
    the qualify-smoketest child, when the smoketest subsystem was enabled for
    this run (audit H3) — for ``version`` have reached a terminal state.

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
    challenge_task_id = qual.get("challenge_task_id")
    audit_challenge_task_id = qual.get("audit_challenge_task_id") or challenge_task_id
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
    # Pre-computed by _finalize_locked_version before the FOR UPDATE lock was
    # acquired (SITE B lock-window fix); fall back to calling inline when not
    # provided (direct test calls, timeout path, etc.).
    if _pre_gates is not None:
        ci_green = bool(_pre_gates["ci_green"])
        ci_resolution = _pre_gates["ci_resolution"]
    else:
        ci_green, ci_resolution = await _resolve_ci_at_finalize(
            session, version, candidate_sha
        )

    policy = _load_policy()

    # --- Infra-failure detection: check required child terminal statuses ------
    # A FAILED, CANCELLED, or BLOCKED required child means the qualifier pipeline
    # itself broke (agent VM crash, timeout, clone failure, blocked precondition)
    # — NOT a quality finding.
    # Route these to _handle_infra_failure BEFORE reading artifacts so the
    # quality-verdict path only sees COMPLETED children.  A COMPLETED child
    # whose artifact is absent or unparseable is also an infrastructure failure
    # (the agent ran but emitted no usable output) — checked after artifact
    # reading below.
    _audit_task_obj: Task | None = None
    if audit_task_id:
        try:
            _audit_task_obj = await session.get(Task, uuid.UUID(str(audit_task_id)))
            if (
                _audit_task_obj is not None
                and _audit_task_obj.status in _INFRA_FAILURE_TERMINAL_STATUSES
            ):
                # A RATE_LIMIT-classified FAILED audit is a transient account-credit
                # failure, not a pipeline infrastructure failure.  Re-spawn the audit
                # child with the same candidate_sha so the same snapshot is scored,
                # and record the respawn count in last_qualification so the cap is
                # visible to the operator.  Non-transient FAILED/CANCELLED tasks and
                # exhausted respawns fall through to _handle_infra_failure as before.
                rl_respawn_count = qual.get("audit_rl_respawn_count", 0)
                if (
                    _audit_task_obj.status == TaskStatus.FAILED
                    and classify_failure(_audit_task_obj) == RATE_LIMIT
                    and rl_respawn_count < _MAX_AUDIT_RL_RESPAWNS
                ):
                    fp = _audit_task_obj.first_payload or {}
                    _project = await session.get(Project, version.project_id)
                    _integration_branch = get_project_integration_branch(_project)
                    new_base_branch = fp.get("base_branch", "") or _integration_branch
                    new_diff_range = (
                        fp.get("diff_range", "")
                        or f"{_integration_branch}...{candidate_sha}"
                    )
                    new_audit_task = await _spawn_release_audit_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=new_base_branch,
                        diff_range=new_diff_range,
                        candidate_sha=candidate_sha,
                    )
                    new_count = rl_respawn_count + 1
                    updated_qual = {
                        **qual,
                        "audit_task_id": str(new_audit_task.id),
                        "audit_rl_respawn_count": new_count,
                    }
                    version.last_qualification = updated_qual
                    flag_modified(version, "last_qualification")
                    session.add(version)
                    await session.flush()
                    logger.info(
                        "[RELEASE_QUALIFIER] v%d audit task %s was rate-limited; "
                        "re-spawned as %s (respawn %d/%d)",
                        version.version_number,
                        audit_task_id,
                        new_audit_task.id,
                        new_count,
                        _MAX_AUDIT_RL_RESPAWNS,
                    )
                    return {
                        "status": "audit_rate_limit_respawned",
                        "respawn_count": new_count,
                    }
                # Transient infra-failure classes (VM provision or agent timeout)
                # get bounded same-candidate respawns, parallel to RATE_LIMIT above.
                # Check the explicitly persisted failure_category rather than
                # classify_failure() to avoid false-positive AGENT_TIMEOUT matches
                # on tasks with empty status messages that were never explicitly
                # classified by the reaper or agent_status.
                infra_respawn_count = qual.get("audit_infra_respawn_count", 0)
                _audit_explicit_cat = (_audit_task_obj.context or {}).get(
                    "failure_category", ""
                ) or (_audit_task_obj.context or {}).get("failure_class", "")
                if (
                    _audit_task_obj.status == TaskStatus.FAILED
                    and _audit_explicit_cat in (PROVISION_FAILURE, AGENT_TIMEOUT)
                    and infra_respawn_count < _MAX_AUDIT_RL_RESPAWNS
                ):
                    fp = _audit_task_obj.first_payload or {}
                    _project = await session.get(Project, version.project_id)
                    _integration_branch = get_project_integration_branch(_project)
                    new_base_branch = fp.get("base_branch", "") or _integration_branch
                    new_diff_range = (
                        fp.get("diff_range", "")
                        or f"{_integration_branch}...{candidate_sha}"
                    )
                    new_audit_task = await _spawn_release_audit_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=new_base_branch,
                        diff_range=new_diff_range,
                        candidate_sha=candidate_sha,
                    )
                    new_count = infra_respawn_count + 1
                    updated_qual = append_infra_failure_child_event(
                        qual,
                        leg="audit",
                        failed_task=_audit_task_obj,
                        replacement_task_id=str(new_audit_task.id),
                        label="audit_infra_respawned",
                        respawn_count=new_count,
                    )
                    updated_qual = {
                        **updated_qual,
                        "audit_task_id": str(new_audit_task.id),
                        "audit_infra_respawn_count": new_count,
                    }
                    version.last_qualification = updated_qual
                    flag_modified(version, "last_qualification")
                    session.add(version)
                    await session.flush()
                    logger.info(
                        "[RELEASE_QUALIFIER] v%d audit task %s had infra failure (%s); "
                        "re-spawned as %s (respawn %d/%d)",
                        version.version_number,
                        audit_task_id,
                        _audit_explicit_cat,
                        new_audit_task.id,
                        new_count,
                        _MAX_AUDIT_RL_RESPAWNS,
                    )
                    return {
                        "status": "audit_infra_respawned",
                        "respawn_count": new_count,
                    }
                infra_label = _enrich_infra_error(
                    f"audit_task_{_audit_task_obj.status.lower()}",
                    _audit_task_obj,
                )
                if (
                    _audit_task_obj.status == TaskStatus.FAILED
                    and classify_failure(_audit_task_obj) == RATE_LIMIT
                    and rl_respawn_count >= _MAX_AUDIT_RL_RESPAWNS
                ):
                    infra_label = "audit_rate_limit_respawn_cap_exceeded"
                if (
                    _audit_task_obj.status == TaskStatus.FAILED
                    and _audit_explicit_cat in (PROVISION_FAILURE, AGENT_TIMEOUT)
                    and infra_respawn_count >= _MAX_AUDIT_RL_RESPAWNS
                ):
                    infra_label = "audit_infra_respawn_cap_exceeded"
                return await _handle_infra_failure(
                    session,
                    version,
                    qual,
                    infra_label,
                )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid audit_task_id %s on version v%d "
                "(skipping infra-failure check)",
                audit_task_id,
                version.version_number,
            )

    _smoke_task_obj: Task | None = None
    if smoketest_enabled and smoketest_task_id:
        try:
            _smoke_task_obj = await session.get(Task, uuid.UUID(str(smoketest_task_id)))
            if (
                _smoke_task_obj is not None
                and _smoke_task_obj.status in _INFRA_FAILURE_TERMINAL_STATUSES
            ):
                return await _handle_infra_failure(
                    session,
                    version,
                    qual,
                    _enrich_infra_error(
                        f"smoketest_task_{_smoke_task_obj.status.lower()}",
                        _smoke_task_obj,
                    ),
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
    # Defense-in-depth: a COMPLETED audit task whose RELEASE_AUDIT_REPORT
    # artifact is absent or unparseable is an infrastructure failure — the
    # agent ran but produced no usable output.  Route to _handle_infra_failure
    # so the operator notification is "release_infra_failure" (not
    # "release_blocked / audit_missing"), and no QualificationVerdict row is
    # written.
    if (
        audit_report is None
        and audit_task_id
        and _audit_task_obj is not None
        and _audit_task_obj.status == TaskStatus.COMPLETED
    ):
        return await _handle_infra_failure(
            session, version, qual, "audit_report_missing"
        )

    audit_score, test_erosion_score = _score_audit(audit_report)
    # Carry the findings (not just the score) into last_qualification so the
    # strategic agent can target them — otherwise the audit sub-score, usually
    # the binding constraint, is opaque to the loop.
    audit_findings = _extract_audit_findings(audit_report)
    # Carry the higher-severity findings the verification gate EXCLUDED from
    # scoring — for display only, marked unverified — so an audit whose
    # sub-score was inflated by dropped findings is rendered honestly instead of
    # reading as clean. Never confirmed blockers.  Use original report (the
    # unverified-primary gap is independent of the challenge reconciliation).
    excluded_audit_findings = _extract_excluded_audit_findings(audit_report)
    # Carry the specific test-erosion detail (deleted files / negative deltas)
    # so the agent can target it when test-erosion is the binding constraint,
    # the same way it sees audit_findings.
    test_erosion_detail = _extract_test_erosion_detail(audit_report)
    audit_missing_reason = _audit_missing_reason(audit_report)
    if audit_missing_reason == "parseable_but_unscoreable":
        return await _handle_infra_failure(
            session, version, qual, "audit_report_parseable_but_unscoreable"
        )
    # An empty artifact dict has no scoring signal at all — _score_audit()
    # returns a silent clean 1.0 because no deductions ever apply.
    if isinstance(audit_report, dict) and not audit_report:
        return await _handle_infra_failure(
            session, version, qual, "audit_report_empty_dict"
        )
    # A "findings" key whose value is not a list is a malformed artifact shape.
    # _score_audit() falls through to the "issues" fallback; if that is also
    # absent or not a list, derived=1.0 with no deductions — treat as infra_failure.
    if isinstance(audit_report, dict):
        _findings_raw = audit_report.get("findings")
        if (
            _findings_raw is not None
            and not isinstance(_findings_raw, list)
            and not isinstance(audit_report.get("issues"), list)
        ):
            return await _handle_infra_failure(
                session, version, qual, "audit_report_findings_wrong_type"
            )

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
    if (
        smoketest_enabled
        and smoketest_task_id
        and _smoke_task_obj is not None
        and _smoke_task_obj.status == TaskStatus.COMPLETED
        and smoketest_result is None
    ):
        return await _handle_infra_failure(
            session, version, qual, "smoketest_report_missing"
        )
    smoketest_score = _score_smoketest(smoketest_result)
    # Carry the specific smoketest-failure detail (failed suite / named tests)
    # so the agent can target it when smoketest is the binding constraint.
    smoketest_detail = _extract_smoketest_detail(smoketest_result)
    primary_findings = _primary_findings_for_challenge_retry(audit_report)

    # If the qualifying run intentionally deferred the challenge spawn, do the
    # first binding spawn now that the audit report exists.  This is the point
    # where the initial release_audit_challenge finally has the audit findings
    # needed to copy primary summaries verbatim into the prompt.
    if (
        bool(qual.get("audit_challenge_spawn_pending"))
        and not challenge_task_id
        and _audit_task_matches_candidate(_audit_task_obj, candidate_sha)
    ):
        audit_payload = (
            _audit_task_obj.first_payload if _audit_task_obj is not None else {}
        )
        project = await session.get(Project, version.project_id)
        _integration_branch, promotion_target = (
            _lts_branch_names(project) if project is not None else ("", "main")
        )
        challenge_base_branch = audit_payload.get("base_branch") or promotion_target
        challenge_diff_range = (
            audit_payload.get("diff_range")
            or f"{challenge_base_branch}...{candidate_sha}"
        )
        from app.payload_schemas import PayloadTooLargeError

        try:
            challenge_task = await _spawn_release_audit_challenge_task(
                session,
                version.project_id,
                version_id=version.id,
                base_branch=challenge_base_branch,
                diff_range=challenge_diff_range,
                candidate_sha=candidate_sha,
                primary_audit_task_id=audit_task_id,
                primary_findings=primary_findings,
            )
        except PayloadTooLargeError as _spawn_exc:
            logger.warning(
                "[RELEASE_QUALIFIER] v%d initial challenge spawn skipped "
                "— primary_findings payload too large (%s); routing to "
                "infra_failure so finalize can record state",
                version.version_number,
                _spawn_exc,
            )
            return await _handle_infra_failure(
                session, version, qual, "audit_challenge_spawn_payload_too_large"
            )
        new_respawn_count = int(qual.get("audit_challenge_respawn_count") or 0) + 1
        challenge_task_id = str(challenge_task.id)
        audit_challenge_task_id = challenge_task_id
        version.last_qualification = {
            **qual,
            "status": "pending",
            "challenge_task_id": challenge_task_id,
            "audit_challenge_task_id": challenge_task_id,
            "audit_challenge_spawn_pending": False,
            "audit_challenge_respawn_count": new_respawn_count,
            "audit_challenge_retry_reason": (
                "challenge_initial_primary_findings"
                if primary_findings
                else "challenge_initial_no_primary_findings"
            ),
            "audit_challenge_retry_primary_findings_count": len(primary_findings),
        }
        flag_modified(version, "last_qualification")
        session.add(version)
        await session.flush()
        logger.info(
            "[RELEASE_QUALIFIER] v%d initial challenge spawned as %s with %d "
            "primary findings",
            version.version_number,
            challenge_task_id,
            len(primary_findings),
        )
        return {
            "status": "audit_challenge_respawned",
            "reason": (
                "challenge_initial_primary_findings"
                if primary_findings
                else "challenge_initial_no_primary_findings"
            ),
            "respawn_count": new_respawn_count,
            "challenge_task_id": challenge_task_id,
        }

    _audit_report_scored = audit_report
    challenge_verdicts: dict[str, Any] | None = None
    challenger_refuted_audit_findings: list[dict[str, Any]] = []
    _challenge_binding_record: list[dict[str, Any]] = []
    # --- Read the audit-challenge verdict -------------------------------------
    # The new binding challenge gate applies when this qualification actually
    # ran the binding challenge flow.  Legacy advisory-only challenge records
    # (``audit_challenge_task_id`` without ``challenge_task_id``) remain
    # observable via audit_reconciliation but do not hard-gate qualification.
    #
    # If neither task id is present, this is the new binding flow with a missing
    # challenge artifact, which MUST hold the version.
    if challenge_task_id or not audit_challenge_task_id:
        # Load the challenge task object early so non-completed terminal states
        # are caught before artifact reads (consistent with audit/smoketest).
        _challenge_task_obj: Task | None = None
        if challenge_task_id:
            try:
                _challenge_task_obj = await session.get(
                    Task, uuid.UUID(str(challenge_task_id))
                )
                if (
                    _challenge_task_obj is not None
                    and _challenge_task_obj.status in _INFRA_FAILURE_TERMINAL_STATUSES
                ):
                    challenge_infra_respawn_count = qual.get(
                        "challenge_infra_respawn_count", 0
                    )
                    _chal_explicit_cat = (_challenge_task_obj.context or {}).get(
                        "failure_category", ""
                    ) or (_challenge_task_obj.context or {}).get("failure_class", "")
                    if (
                        _challenge_task_obj.status == TaskStatus.FAILED
                        and _chal_explicit_cat in (PROVISION_FAILURE, AGENT_TIMEOUT)
                        and challenge_infra_respawn_count < _MAX_AUDIT_RL_RESPAWNS
                    ):
                        _chal_payload = _challenge_task_obj.first_payload or {}
                        _audit_payload = (
                            _audit_task_obj.first_payload
                            if _audit_task_obj is not None
                            else {}
                        )
                        _chal_project = await session.get(Project, version.project_id)
                        _chal_int_branch, _chal_promo_target = (
                            _lts_branch_names(_chal_project)
                            if _chal_project is not None
                            else ("", "main")
                        )
                        _chal_base_branch = (
                            _chal_payload.get("base_branch")
                            or _audit_payload.get("base_branch")
                            or _chal_promo_target
                        )
                        _chal_diff_range = (
                            _chal_payload.get("diff_range")
                            or _audit_payload.get("diff_range")
                            or f"{_chal_base_branch}...{candidate_sha}"
                        )
                        _chal_primary_findings = _primary_findings_for_challenge_retry(
                            audit_report
                        )
                        if _chal_primary_findings:
                            from app.payload_schemas import PayloadTooLargeError

                            try:
                                _new_challenge_task = (
                                    await _spawn_release_audit_challenge_task(
                                        session,
                                        version.project_id,
                                        version_id=version.id,
                                        base_branch=_chal_base_branch,
                                        diff_range=_chal_diff_range,
                                        candidate_sha=candidate_sha,
                                        primary_audit_task_id=audit_task_id,
                                        primary_findings=_chal_primary_findings,
                                    )
                                )
                            except PayloadTooLargeError as _chal_exc:
                                logger.warning(
                                    "[RELEASE_QUALIFIER] v%d challenge infra respawn "
                                    "skipped — primary_findings payload too large "
                                    "(%s); routing to infra_failure",
                                    version.version_number,
                                    _chal_exc,
                                )
                            else:
                                _chal_new_count = challenge_infra_respawn_count + 1
                                updated_qual = append_infra_failure_child_event(
                                    qual,
                                    leg="challenge",
                                    failed_task=_challenge_task_obj,
                                    replacement_task_id=str(_new_challenge_task.id),
                                    label="challenge_infra_respawned",
                                    respawn_count=_chal_new_count,
                                )
                                version.last_qualification = {
                                    **updated_qual,
                                    "status": "pending",
                                    "challenge_task_id": str(_new_challenge_task.id),
                                    "audit_challenge_task_id": str(
                                        _new_challenge_task.id
                                    ),
                                    "challenge_infra_respawn_count": _chal_new_count,
                                }
                                flag_modified(version, "last_qualification")
                                session.add(version)
                                await session.flush()
                                logger.info(
                                    "[RELEASE_QUALIFIER] v%d challenge task %s had "
                                    "infra failure (%s); re-spawned as %s "
                                    "(respawn %d/%d)",
                                    version.version_number,
                                    challenge_task_id,
                                    _chal_explicit_cat,
                                    _new_challenge_task.id,
                                    _chal_new_count,
                                    _MAX_AUDIT_RL_RESPAWNS,
                                )
                                return {
                                    "status": "challenge_infra_respawned",
                                    "respawn_count": _chal_new_count,
                                }
                    _challenge_infra_label = _enrich_infra_error(
                        f"audit_challenge_task_{_challenge_task_obj.status.lower()}",
                        _challenge_task_obj,
                    )
                    if (
                        _challenge_task_obj.status == TaskStatus.FAILED
                        and _chal_explicit_cat in (PROVISION_FAILURE, AGENT_TIMEOUT)
                        and challenge_infra_respawn_count >= _MAX_AUDIT_RL_RESPAWNS
                    ):
                        _challenge_infra_label = "challenge_infra_respawn_cap_exceeded"
                    return await _handle_infra_failure(
                        session,
                        version,
                        qual,
                        _challenge_infra_label,
                    )
            except (ValueError, TypeError):
                logger.warning(
                    "[RELEASE_QUALIFIER] invalid challenge_task_id %s on version v%d "
                    "(skipping infra-failure check)",
                    challenge_task_id,
                    version.version_number,
                )

        # The challenge child now binds to the audit findings after the audit
        # report exists.  Its per-finding verdicts (confirmed /
        # refuted_false_positive / uncertain) are BINDING: a verified
        # higher-severity finding may only deduct after the challenger confirms
        # it.  A missing or uncertain verdict for any such finding HOLDS this
        # attempt (fail-closed) so the version returns to stabilize for
        # re-evaluation rather than falsely promoting or silently dropping the
        # deduction.  The artifact is still read best-effort; a missing challenge
        # with NO deductible findings proceeds normally (no hold needed).
        try:
            challenge_verdicts = await _latest_artifact_logs(
                session,
                uuid.UUID(str(challenge_task_id)),
                ArtifactType.RELEASE_AUDIT_CHALLENGE,
            )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid challenge_task_id %s on version v%d",
                challenge_task_id,
                version.version_number,
            )

        # Legacy fallback: if an older challenge task completed without
        # primary_findings in its payload and produced no artifact, respawn it
        # immediately rather than incurring a wasted reconciliation-failure
        # round-trip.  Only fires on the first pass (respawn_count == 0) and
        # only when no artifact was produced (empty-verdicts and SHA-mismatch
        # cases have an artifact and are handled by the reconciliation path
        # below with their own distinct reason codes).
        if (
            challenge_verdicts is None
            and _challenge_task_obj is not None
            and _challenge_task_obj.status == TaskStatus.COMPLETED
            and not ((_challenge_task_obj.first_payload or {}).get("primary_findings"))
            and primary_findings
            and int(qual.get("audit_challenge_respawn_count") or 0) == 0
            and _audit_task_matches_candidate(_audit_task_obj, candidate_sha)
        ):
            _early_payload = _challenge_task_obj.first_payload or {}
            _early_audit_payload = (
                _audit_task_obj.first_payload if _audit_task_obj is not None else {}
            )
            _early_project = await session.get(Project, version.project_id)
            _early_int_branch, _early_promo_target = (
                _lts_branch_names(_early_project)
                if _early_project is not None
                else ("", "main")
            )
            _early_base_branch = (
                _early_payload.get("base_branch")
                or _early_audit_payload.get("base_branch")
                or _early_promo_target
            )
            _early_diff_range = (
                _early_payload.get("diff_range")
                or _early_audit_payload.get("diff_range")
                or f"{_early_base_branch}...{candidate_sha}"
            )
            from app.payload_schemas import PayloadTooLargeError

            try:
                _early_retry_task = await _spawn_release_audit_challenge_task(
                    session,
                    version.project_id,
                    version_id=version.id,
                    base_branch=_early_base_branch,
                    diff_range=_early_diff_range,
                    candidate_sha=candidate_sha,
                    primary_audit_task_id=audit_task_id,
                    primary_findings=primary_findings,
                )
            except PayloadTooLargeError as _early_exc:
                logger.warning(
                    "[RELEASE_QUALIFIER] v%d initial challenge retry skipped "
                    "— primary_findings payload too large (%s); proceeding to "
                    "reconciliation",
                    version.version_number,
                    _early_exc,
                )
            else:
                version.last_qualification = {
                    **qual,
                    "status": "pending",
                    "challenge_task_id": str(_early_retry_task.id),
                    "audit_challenge_task_id": str(_early_retry_task.id),
                    "audit_challenge_spawn_pending": False,
                    "audit_challenge_respawn_count": 1,
                    "audit_challenge_retry_reason": "challenge_initial_no_primary_findings",
                    "audit_challenge_previous_task_id": challenge_task_id,
                    "audit_challenge_retry_primary_findings_count": len(
                        primary_findings
                    ),
                }
                flag_modified(version, "last_qualification")
                session.add(version)
                await session.flush()
                logger.info(
                    "[RELEASE_QUALIFIER] v%d initial challenge %s had no "
                    "primary_findings; re-spawned as %s with %d findings",
                    version.version_number,
                    challenge_task_id,
                    _early_retry_task.id,
                    len(primary_findings),
                )
                return {
                    "status": "audit_challenge_respawned",
                    "reason": "challenge_initial_no_primary_findings",
                    "respawn_count": 1,
                    "challenge_task_id": str(_early_retry_task.id),
                }

        # --- Challenge reconciliation: fail-closed audit challenge gate -----------
        # Each verified higher-severity finding must have a challenger-confirmed
        # verdict to deduct.  Challenger-refuted findings are excluded from scoring
        # and recorded separately (distinct from excluded_audit_findings which tracks
        # the unverified-primary substantiation gap — do not fold the two concepts).
        # A missing/uncertain/SHA-mismatched challenge for any deductible finding
        # HOLDS the attempt via _handle_infra_failure (same pattern as the migration
        # defer path: no verdict recorded, version returns to stabilize).
        (
            _challenge_hold,
            _audit_report_scored,
            challenger_refuted_audit_findings,
            _challenge_binding_record,
        ) = _reconcile_audit_with_challenge(
            audit_report, challenge_verdicts, candidate_sha
        )
        if _challenge_hold:
            logger.warning(
                "[RELEASE_QUALIFIER] v%d challenge reconciliation HOLD: %s — "
                "returning to stabilize for re-evaluation",
                version.version_number,
                _challenge_hold,
            )
            challenge_respawn_count = int(
                qual.get("audit_challenge_respawn_count") or 0
            )
            primary_findings = _primary_findings_for_challenge_retry(audit_report)
            if (
                _challenge_hold in _AUDIT_CHALLENGE_BINDING_HOLDS
                and challenge_respawn_count < _MAX_AUDIT_CHALLENGE_BINDING_RESPAWNS
                and _audit_task_matches_candidate(_audit_task_obj, candidate_sha)
                and primary_findings
            ):
                # Reuse the task object loaded during the terminal-status check.
                challenge_task_obj: Task | None = _challenge_task_obj
                challenge_payload = (
                    challenge_task_obj.first_payload
                    if challenge_task_obj is not None
                    else {}
                )
                audit_payload = (
                    _audit_task_obj.first_payload if _audit_task_obj is not None else {}
                )
                project = await session.get(Project, version.project_id)
                _integration_branch, promotion_target = (
                    _lts_branch_names(project) if project is not None else ("", "main")
                )
                retry_base_branch = (
                    challenge_payload.get("base_branch")
                    or audit_payload.get("base_branch")
                    or promotion_target
                )
                retry_diff_range = (
                    challenge_payload.get("diff_range")
                    or audit_payload.get("diff_range")
                    or f"{retry_base_branch}...{candidate_sha}"
                )
                from app.payload_schemas import PayloadTooLargeError

                try:
                    retry_task = await _spawn_release_audit_challenge_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=retry_base_branch,
                        diff_range=retry_diff_range,
                        candidate_sha=candidate_sha,
                        primary_audit_task_id=audit_task_id,
                        primary_findings=primary_findings,
                    )
                except PayloadTooLargeError as _spawn_exc:
                    logger.warning(
                        "[RELEASE_QUALIFIER] v%d challenge retry spawn skipped "
                        "— primary_findings payload too large (%s); "
                        "routing to infra_failure so finalize can record state",
                        version.version_number,
                        _spawn_exc,
                    )
                    return await _handle_infra_failure(
                        session, version, qual, _challenge_hold
                    )
                new_respawn_count = challenge_respawn_count + 1
                # When the hold was a SHA mismatch, record the SHA the challenge
                # reported so operators can see what candidate it actually ran on
                # vs what this qualification expected.
                _sha_mismatch_observed = (
                    str(
                        (challenge_verdicts or {}).get("challenged_candidate_sha") or ""
                    )
                    or None
                    if _challenge_hold == "audit_challenge_sha_mismatch"
                    else None
                )
                version.last_qualification = {
                    **qual,
                    "status": "pending",
                    "challenge_task_id": str(retry_task.id),
                    "audit_challenge_task_id": str(retry_task.id),
                    "audit_challenge_respawn_count": new_respawn_count,
                    "audit_challenge_retry_reason": _challenge_hold,
                    "audit_challenge_previous_task_id": challenge_task_id,
                    "audit_challenge_retry_primary_findings_count": len(
                        primary_findings
                    ),
                    **(
                        {
                            "audit_challenge_sha_mismatch_observed_sha": _sha_mismatch_observed
                        }
                        if _sha_mismatch_observed
                        else {}
                    ),
                }
                flag_modified(version, "last_qualification")
                session.add(version)
                await session.flush()
                logger.info(
                    "[RELEASE_QUALIFIER] v%d challenge hold %s; re-spawned "
                    "challenge as %s with %d primary findings (respawn %d/%d)",
                    version.version_number,
                    _challenge_hold,
                    retry_task.id,
                    len(primary_findings),
                    new_respawn_count,
                    _MAX_AUDIT_CHALLENGE_BINDING_RESPAWNS,
                )
                return {
                    "status": "audit_challenge_respawned",
                    "reason": _challenge_hold,
                    "respawn_count": new_respawn_count,
                    "challenge_task_id": str(retry_task.id),
                }
            if _challenge_hold == "audit_challenge_independent_blocker":
                return await _handle_independent_blocker(
                    session, version, qual, _challenge_binding_record
                )
            return await _handle_infra_failure(session, version, qual, _challenge_hold)

    # Score using the reconciled report (refuted findings removed when challenge ran).
    audit_score, test_erosion_score = _score_audit(_audit_report_scored)
    # Carry the confirmed (actionable) findings into last_qualification so the
    # strategic agent can target them.  Use reconciled report when available so
    # refuted findings are not surfaced as actionable blockers.
    audit_findings = _extract_audit_findings(_audit_report_scored)
    # Pre-cap totals + truncation flags for both carried lists. Verified meta is
    # computed on the reconciled report (matches audit_findings just above);
    # excluded meta on the original report (matches excluded_audit_findings,
    # which is derived from audit_report — the unverified-primary gap is
    # independent of challenge reconciliation).
    _verified_findings_meta = _audit_findings_carry_meta(_audit_report_scored)
    _excluded_findings_meta = _audit_findings_carry_meta(audit_report)
    # Build the reconciliation record on the RECONCILED report so
    # findings_derived_score agrees with the applied audit_score.  The challenge
    # synthesis is derived from the binding verdict artifact (new gate) — kept
    # separate from excluded_findings_count (verification-gate drops) so they
    # are never double-counted.
    audit_reconciliation = {
        **_compute_audit_reconciliation(_audit_report_scored),
        "audit_challenge": _compute_challenge_synthesis_from_binding_verdicts(
            audit_report,
            challenge_verdicts,
            candidate_sha=candidate_sha,
            challenge_reason=None,
        ),
        "challenge_excluded_findings_count": len(challenger_refuted_audit_findings),
        "challenge_binding_record": _challenge_binding_record,
    }
    audit_challenge = audit_reconciliation.get("audit_challenge")
    audit_task_candidate_sha = None
    if _audit_task_obj is not None:
        audit_task_candidate_sha = (
            str(
                (_audit_task_obj.context or {}).get("candidate_sha")
                or (_audit_task_obj.first_payload or {}).get("candidate_sha")
                or ""
            ).strip()
            or None
        )
    audit_reconciliation["audit_leg_proof"] = _build_audit_leg_proof(
        candidate_sha=candidate_sha,
        audit_task_id=str(audit_task_id) if audit_task_id else None,
        audit_task_candidate_sha=audit_task_candidate_sha,
        challenge_task_id=str(challenge_task_id) if challenge_task_id else None,
        challenged_candidate_sha=(
            str(audit_challenge.get("challenged_candidate_sha") or "").strip() or None
            if isinstance(audit_challenge, dict)
            else None
        ),
        binding_expected_count=len(primary_findings),
        binding_record_count=len(_challenge_binding_record),
        reconciliation_status=(
            str(audit_challenge.get("status") or "").strip() or None
            if isinstance(audit_challenge, dict)
            else None
        ),
        reconciliation_reason=(
            str(audit_challenge.get("reason") or "").strip() or None
            if isinstance(audit_challenge, dict)
            else None
        ),
        legacy_advisory_only=bool(audit_challenge_task_id)
        and not bool(challenge_task_id),
    )

    # --- Deterministic Alembic revision-identity safety gate ----------------
    # A revision-identity collision between the candidate and the deployed
    # promotion target (the back-merge renumber class, prod incident
    # 2026-06-04) must be a HARD, clearly-attributed qualification blocker — not
    # an opaque, score-eroding audit deduction the spawned auditor might miss.
    # Reuse the existing deterministic detector + GitHub fetch helpers; a fetch
    # failure DEFERS (holds) the release rather than waving it through. Routed
    # through _handle_infra_failure so a persistent inability to verify migration
    # identity surfaces the same way as any other finalize-time infra failure
    # (no verdict recorded, returned to stabilize, retried by the next cycle).
    # Pre-computed by _finalize_locked_version pre-lock (SITE B lock-window fix).
    if _pre_gates is not None:
        migration_verdict = _pre_gates["migration_verdict"]
        migration_detail = _pre_gates["migration_detail"]
    else:
        (
            migration_verdict,
            migration_detail,
        ) = await _resolve_migration_identity_at_finalize(
            session, version, candidate_sha
        )
    if migration_verdict == "defer":
        return await _handle_infra_failure(
            session,
            version,
            qual,
            "migration_identity_check_unavailable",
        )
    migration_collision = migration_verdict == "block"

    # --- Deterministic scratch-DB migration-APPLY safety gate ---------------
    # Complementary to the filename-identity check above: replay the candidate's
    # migration chain the deploy way against production's REAL recorded schema
    # state (live alembic_version stamp + present tables) on an isolated scratch
    # DB, and HARD-block when the resulting schema is missing a required object
    # (the v5 computejob renumber-skip class the filename check cannot see).  A
    # 'defer' verdict (prod state unreadable, candidate fetch failed, scratch DB
    # unprovisionable) HOLDS the release via _handle_infra_failure exactly like
    # the identity gate's defer path — never waved through.
    # Thread the exact SHA snapped into last_qualification through this gate;
    # do not re-derive it from any live checkout state.
    # Pre-computed by _finalize_locked_version pre-lock (SITE B lock-window fix).
    if _pre_gates is not None:
        apply_verdict = _pre_gates["apply_verdict"]
        apply_detail = _pre_gates["apply_detail"]
    else:
        apply_verdict, apply_detail = await _resolve_migration_apply_at_finalize(
            session, version, candidate_sha
        )
    if apply_verdict == "defer":
        return await _handle_infra_failure(
            session,
            version,
            qual,
            "migration_apply_check_unavailable",
        )
    migration_apply_blocked = apply_verdict == "block"

    # --- Compute the composite ---------------------------------------------
    composite = compute_composite(
        ci_green=ci_green,
        audit_score=audit_score,
        test_erosion=test_erosion_score,
        smoketest_score=smoketest_score,
        policy=policy,
        smoketest_enabled=smoketest_enabled,
        ci_state=ci_resolution.get("state", _CI_RED),
        migration_collision=migration_collision,
        migration_apply_blocked=migration_apply_blocked,
    )

    # --- Acceptance-window aggregate (additive evidence, no score impact) --
    # Reuses the same verified-prs-merged predicate from direction_outcomes so
    # the release qualification surfaces the same proven-vs-delivered signal as
    # the strategic and direction layers. Merged into sub_scores as additive
    # keys; the existing audit/test_erosion/smoketest weights are unchanged.
    acceptance_aggregate = await _compute_acceptance_window_aggregate(session, version)
    extended_sub_scores = dict(composite["sub_scores"])
    extended_sub_scores.update(acceptance_aggregate)

    finalized_at = _utcnow().isoformat()
    acceptance_aggregate = None
    if version.opened_at is not None:
        merged_rows = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == version.project_id,
                        Task.status == TaskStatus.COMPLETED,
                        landed_signal_predicate(session),
                        Task.updated_at >= version.opened_at,
                    )
                    .order_by(Task.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        merged_rows = [
            task
            for task in merged_rows
            if release_version._task_completed_at(task) >= version.opened_at
        ]
        acceptance_aggregate = summarize_version_acceptance_aggregate(merged_rows)
    base_record: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "ci_green": ci_green,
        "ci_resolution": ci_resolution,
        "smoketest_enabled": smoketest_enabled,
        "confidence": composite["confidence"],
        "sub_scores": extended_sub_scores,
        "blocker": composite["blocker"],
        "threshold": policy.get("threshold"),
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": audit_task_id,
        "challenge_task_id": challenge_task_id,
        # Parsed challenge artifact — observable on last_qualification.
        "challenge_verdicts": challenge_verdicts,
        "audit_challenge_task_id": audit_challenge_task_id,
        "audit_challenge_spawn_error": qual.get("audit_challenge_spawn_error"),
        "smoketest_task_id": smoketest_task_id,
        "audit_findings": audit_findings,
        "excluded_audit_findings": excluded_audit_findings,
        # Pre-cap totals + truncation flags so consumers can tell whether the
        # carried lists above are complete or were clipped at _MAX_CARRIED_FINDINGS.
        # Verified-list meta tracks the reconciled report (matches audit_findings);
        # excluded-list meta tracks the original report (matches excluded_audit_findings).
        "audit_findings_total": _verified_findings_meta["audit_findings_total"],
        "audit_findings_truncated": _verified_findings_meta["audit_findings_truncated"],
        "excluded_audit_findings_total": _excluded_findings_meta[
            "excluded_audit_findings_total"
        ],
        "excluded_audit_findings_truncated": _excluded_findings_meta[
            "excluded_audit_findings_truncated"
        ],
        # Challenger-confirmed false positives: findings the challenge marked
        # refuted_false_positive with an auditable reason — DISTINCT from
        # excluded_audit_findings (which tracks the unverified-primary gap).
        "challenger_refuted_audit_findings": challenger_refuted_audit_findings,
        "test_erosion_detail": test_erosion_detail,
        "smoketest_detail": smoketest_detail,
        "audit_missing_reason": audit_missing_reason,
        "audit_reconciliation": audit_reconciliation,
        "migration_safety": migration_verdict,
        "migration_safety_detail": migration_detail,
        "migration_apply_safety": apply_verdict,
        "migration_apply_safety_detail": apply_detail,
        "acceptance_aggregate": acceptance_aggregate,
    }

    # Per-project override (falls back to the fleet-wide promotion_enabled when
    # this project has no explicit override) — additive, behavior unchanged for
    # projects without one.
    promotion_enabled = resolve_project_promotion_enabled(version.project_id)

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
        # Monotonic ledger guard (incident 2026-06-09): a ci_unresolved blocker
        # is transient — CI was pending or unresolvable, not red. Before writing
        # not_promoted for this reason, verify the candidate is not already on
        # main and that there is no open release PR whose auto-merge is still
        # armed. Either condition means a promotion is in flight and this not_promoted
        # would be a spurious miss.
        if composite.get("blocker") == "ci_unresolved":
            _guard_project = await session.get(Project, version.project_id)
            if _guard_project is not None:
                _guard_resolved = await _resolve_github_with_session(
                    session, _guard_project
                )
                if _guard_resolved is not None:
                    _gtoken, _gowner, _grepo = _guard_resolved
                    _guard_result = await _guard_ci_unresolved_promotion_in_flight(
                        session,
                        version,
                        candidate_sha,
                        base_record,
                        _gtoken,
                        _gowner,
                        _grepo,
                        _guard_project,
                    )
                    if _guard_result is not None:
                        return _guard_result

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
            # The migration-identity blocker carries the specific diverging
            # revision so the operator sees WHICH revision collides, not just the
            # generic blocker label.
            if (
                composite["blocker"] == "migration_identity_collision"
                and migration_detail
            ):
                blocker_label = f"{blocker_label} — {migration_detail}"
            # The migration-apply blocker carries the specific missing object so
            # the operator sees WHICH required table/column the candidate's chain
            # would fail to create, not just the generic blocker label.
            if composite["blocker"] == "migration_apply_drift" and apply_detail:
                blocker_label = f"{blocker_label} — {apply_detail}"
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
    challenge_task_id_raw = qual.get("challenge_task_id")
    smoketest_task_id_raw = qual.get("smoketest_task_id")
    challenge_task_id_raw = qual.get("challenge_task_id")
    challenge_required = bool(challenge_task_id_raw)

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
        challenge_task_id = (
            uuid.UUID(str(challenge_task_id_raw)) if challenge_required else None
        )
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d has unparseable "
            "child task ids (audit=%r challenge=%r smoke=%r) — not ready",
            version.version_number,
            audit_task_id_raw,
            challenge_task_id_raw,
            smoketest_task_id_raw,
        )
        return False

    audit_task: Task | None = await session.get(Task, audit_task_id)
    smoketest_task: Task | None = (
        await session.get(Task, smoketest_task_id)
        if smoketest_task_id is not None
        else None
    )
    challenge_task: Task | None = (
        await session.get(Task, challenge_task_id)
        if challenge_task_id is not None
        else None
    )

    if (
        audit_task is None
        or (smoketest_required and smoketest_task is None)
        or (challenge_required and challenge_task is None)
    ):
        logger.debug(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d required child "
            "tasks not yet persisted — not ready",
            version.version_number,
        )
        return False

    if (
        audit_task.status not in _TERMINAL_STATUSES
        or (
            smoketest_required
            and smoketest_task is not None
            and smoketest_task.status not in _TERMINAL_STATUSES
        )
        or (
            challenge_required
            and challenge_task is not None
            and challenge_task.status not in _TERMINAL_STATUSES
        )
    ):
        # A required child is still running — not time to finalize yet.
        return False

    logger.info(
        "[RELEASE_QUALIFIER] finalize-gate: required children terminal for "
        "version v%d (audit=%s/%s smoke=%s challenge=%s)",
        version.version_number,
        audit_task.id,
        audit_task.status,
        f"{smoketest_task.id}/{smoketest_task.status}"
        if smoketest_task is not None
        else "disabled",
        f"{challenge_task.id}/{challenge_task.status}"
        if challenge_task is not None
        else "not_spawned",
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


# Qualification outcomes after which the strategic agent should re-plan: a
# settled verdict (a promote, any flavour of miss, or a terminal infra
# failure). Excludes the in-flight (``pending_auto_merge``), transient
# (``audit_rate_limit_respawned``) and read-only no-op (``None``) states,
# which do not represent a settled cut.
_STRATEGIC_RETRIGGER_STATUSES: frozenset[str] = frozenset(
    {
        "promoted",
        "promotion_failed",
        "not_promoted",
        "dry_run",
        "error",
        "infra_failure",
    }
)


async def _maybe_trigger_strategic_after_qualification(
    version_id: uuid.UUID,
    result: dict[str, Any] | None,
    *,
    session_factory=None,
) -> None:
    """Best-effort: once a qualification has RESOLVED, kick a strategic review.

    A version cut deliberately lets the direction frontier go dry — the
    strategic agent winds its active directions down to a paused residual while
    it waits to qualify ("do not open the next feature wave until vN promotes").
    When qualification then resolves the agent must re-plan: open the next wave
    on a promote, or work the blocker on a miss. But nothing was re-triggering
    it. With every direction paused, no tactical research spawns, so the
    strategic cadence counter never advances and the project sits idle with free
    VMs until the wall-clock backstop eventually fires (up to
    ``strategic_max_interval_minutes`` later). Spawning a ``strategic_review``
    here closes that gap immediately, for both promote and miss outcomes.

    Runs AFTER the dedicated finalize session committed + closed (mirrors
    ``_maybe_reconcile_after_promotion_conflict``): its own session, never under
    the ``ReleaseVersion`` row lock. Idempotent — ``_spawn_strategic_review``'s
    ``_has_active_strategic_task`` guard makes a cycle already in flight a no-op,
    so this can neither pile up strategic tasks nor loop. A failure never
    propagates; the wall-clock backstop in ``_spawn_research_task`` retries if
    this best-effort call does not land. ``session_factory`` is threaded through
    for test injection.
    """
    if not isinstance(result, dict):
        return
    if result.get("status") not in _STRATEGIC_RETRIGGER_STATUSES:
        return
    # Local import so this helper does not depend on a module-level
    # `PayloadTooLargeError` import. On the integration branch release_qualifier.py
    # is split into sub-modules and does NOT import this name at module scope, so a
    # back-merge that grafts this function onto that structure would otherwise hit a
    # NameError at the `except` below. payload_schemas defines it on both branches.
    from app.payload_schemas import PayloadTooLargeError

    try:
        factory = session_factory or AsyncSessionLocal
        async with factory() as session:
            version = await session.get(release_version.ReleaseVersion, version_id)
            if version is None:
                return
            project = await session.get(Project, version.project_id)
            if project is None or not project.autonomous_enabled:
                return
            # Lazy import — release_qualifier <-> strategic_spawning is circular.
            from app.strategic_spawning import _spawn_strategic_review

            spawned = await _spawn_strategic_review(project, session)
            if spawned:
                await session.commit()
                logger.info(
                    "[RELEASE_QUALIFIER] qualification resolved (%s) for v%d — "
                    "triggered a strategic review for project %s",
                    result.get("status"),
                    version.version_number,
                    project.id,
                )
            else:
                # A strategic cycle is already active, so the spawn no-op'd.
                # DON'T drop the trigger (incident 2026-06-02: a dropped
                # post-promotion refresh let the pipeline idle with 0 directions
                # until the 30-min strategic_spawn_cooldown elapsed). Coalesce it
                # into a durable refresh marker; the autonomous loop fires it as
                # soon as the active cycle clears, bypassing the cooldown.
                from app.strategic_trigger import request_strategic_refresh

                await request_strategic_refresh(
                    session,
                    project.id,
                    f"post_qualification:{result.get('status')}",
                )
                await session.commit()
                logger.info(
                    "[RELEASE_QUALIFIER] qualification resolved (%s) for v%d but a "
                    "strategic cycle is active — queued a coalesced refresh for "
                    "project %s",
                    result.get("status"),
                    version.version_number,
                    project.id,
                )
    except PayloadTooLargeError as exc:
        # The strategic payload outgrew the cap; the autonomous loop surfaces a
        # dedicated alert for this case and the wall-clock backstop retries.
        logger.warning(
            "[RELEASE_QUALIFIER] post-qualification strategic trigger skipped "
            "for version %s — payload too large (%s)",
            version_id,
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; wall-clock backstop retries
        logger.warning(
            "[RELEASE_QUALIFIER] post-qualification strategic trigger failed for "
            "version %s (non-fatal — wall-clock backstop will retry): %s",
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
    # And whenever the qualification RESOLVED (promote or miss), re-trigger the
    # strategic agent so it re-plans — otherwise a project whose frontier went
    # dry during the cut can sit idle until the wall-clock backstop fires.
    await _maybe_trigger_strategic_after_qualification(
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

    Lock-window fix (SITE B): the three GitHub-bound finalize gates
    (_resolve_ci_at_finalize, _resolve_migration_identity_at_finalize,
    _resolve_migration_apply_at_finalize) issue GitHub API calls that can take
    10-120 s under rate-limit/retry.  Running them UNDER the ReleaseVersion
    FOR UPDATE lock caused 22 s+ ShareLock waits on every concurrent qualifier
    finalize / reconcile call.  Fix: non-locking peek to read the immutable
    candidate_sha, run the three gates pre-lock, THEN acquire FOR UPDATE for
    the decision write only — mirroring the _relock_version_for_promotion_write
    pattern already used on the promote sub-path.
    """
    # ── Pre-lock peek: read stable fields for the GitHub-bound gate calls ──
    # A non-locking read suffices here: candidate_sha is immutable once set and
    # the status/qual-status checks below are re-verified authoritatively under
    # the FOR UPDATE lock that follows.  This peek exists ONLY to obtain the
    # candidate_sha so the three gates can run before the lock is held.
    peek: release_version.ReleaseVersion | None = (
        (
            await session.execute(
                select(release_version.ReleaseVersion).where(
                    release_version.ReleaseVersion.id == version_id
                )
            )
        )
        .scalars()
        .first()
    )
    # Fast-path skip for the two cheapest no-ops (status / qual-status).
    # These are re-checked under the lock below — this only avoids the
    # expensive gate calls when clearly not warranted.
    if peek is None or peek.status != "open":
        return None
    peek_qual = peek.last_qualification or {}
    if peek_qual.get("status") != "pending":
        return None
    peek_candidate_sha: str | None = peek_qual.get("candidate_sha")

    # ── Run the three GitHub-bound gates BEFORE acquiring the FOR UPDATE ──
    # Guard: only run when (a) this is not a timeout-only reaper call (timeout_now
    # is None means task-completion hook — children may be terminal) and (b) all
    # required children are already terminal according to the non-locking peek.
    # Without this guard the gates ran speculatively on every qualifier child
    # completion (Nx amplification) and on every reaper sweep iteration for
    # versions whose children were still running — a DB/GitHub load regression.
    #
    # When _pre_gates is None (children not yet terminal, or timeout path),
    # _finalize_qualification will either be skipped entirely (Path 2 returns
    # early) or run with _pre_gates=None and call the gates itself under the
    # lock — acceptable because that path is rare (race between sibling
    # completion and this check) and the reaper sweep is infrequent.
    _pre_gates: dict[str, Any] | None = None
    if timeout_now is None and await _required_children_terminal(session, peek):
        # All three are pinned to the immutable candidate_sha and are read-only
        # against GitHub; their results are valid for the entire finalize attempt.
        pre_ci_green, pre_ci_resolution = await _resolve_ci_at_finalize(
            session, peek, peek_candidate_sha
        )
        (
            pre_migration_verdict,
            pre_migration_detail,
        ) = await _resolve_migration_identity_at_finalize(
            session, peek, peek_candidate_sha
        )
        pre_apply_verdict, pre_apply_detail = await _resolve_migration_apply_at_finalize(
            session, peek, peek_candidate_sha
        )
        _pre_gates = {
            "ci_green": pre_ci_green,
            "ci_resolution": pre_ci_resolution,
            "migration_verdict": pre_migration_verdict,
            "migration_detail": pre_migration_detail,
            "apply_verdict": pre_apply_verdict,
            "apply_detail": pre_apply_detail,
        }

    # Expire the peek instance so the FOR UPDATE select below forces a fresh
    # read from the DB (mirrors _relock_version_for_promotion_write pattern —
    # populate_existing=True alone is not reliable on async sessions per
    # concern C1 in release_promotion.py).
    session.expire(peek)

    # ── Acquire the FOR UPDATE lock — decision write only ──
    # Lock the version row — the single serialization point for the whole
    # finalize→promote critical section (concern d).
    version: release_version.ReleaseVersion | None = (
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
    # holds only qualifier state.  Pass the pre-computed gate results so the
    # function does NOT re-run the GitHub-bound gates under the row lock.
    result = await _finalize_qualification(session, version, _pre_gates=_pre_gates)
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
