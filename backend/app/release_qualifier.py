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
before delegating — so the dedicated session's ``SELECT ... FOR UPDATE SKIP LOCKED`` on the
version row can never wait on a lock the caller still holds; if a peer holds it,
the finalize attempt skips and the next completion/sweep retries.

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

import contextlib
import hashlib
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
    ACCEPTANCE_AGGREGATE_BUCKETS,
    _UNDECLARED_REASON_KEYS,
    classify_merged_acceptance_context,
    classify_undeclared_reason,
)
from app.acceptance_criteria import get_version_coverage_summary
from app.landed_signal import landed_signal_predicate
from app.models import (
    ArtifactType,
    Project,
    QualificationDimensionResult,
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
    _merge_grade_misses_armed,
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
    _reconcile_if_ci_unresolved_now_green,  # noqa: F401 — re-exported for back-compat
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
    _handle_independent_blocker,  # re-exported for callers/tests
    _handle_infra_failure,
    _latest_artifact_logs,
    _select_lifecycle_audit,  # noqa: F401 — re-exported for callers/tests
    _spawn_qualifier_smoketest_task,
    _spawn_release_audit_challenge_task,
    _spawn_release_audit_task,
    _spawn_release_merge_grade_task,
    get_infra_failure_stats,  # noqa: F401 — re-exported for callers/tests
    get_latest_qualification_audit_findings,  # noqa: F401 — re-exported for callers/tests
    get_latest_release_audit_challenge,  # noqa: F401 — re-exported for callers/tests
    get_latest_release_audit_report,  # noqa: F401 — re-exported for callers/tests
    resolve_lifecycle_audit_evidence,  # noqa: F401 — re-exported for callers/tests
)
from app.ci_resolution import (
    _CI_GREEN,
    _CI_NO_CHECKS,
    _CI_PENDING,
    _CI_RED,
    _classify_check_runs,
    proceed_on_no_checks,
    requires_ci_checks,
)
from app.release_scoring import (
    # Re-exports — release_qualifier is the canonical import + test-patch
    # surface for the scoring layer (tests mock via ``app.release_qualifier.<name>``
    # and other modules import these from here). The implementations live in
    # release_scoring.py; noqa pins them so a ruff --fix sweep can't drop them
    # as "unused" and silently break downstream importers / patch points.
    _AUDIT_DEDUCTION_UNKNOWN,  # noqa: F401
    _AUDIT_DEDUCTIONS,  # noqa: F401
    _CHALLENGE_FINDING_MAX,  # noqa: F401 — re-exported for callers/tests
    _DEFAULT_POLICY,
    _FINDING_SUMMARY_MAX,
    _MAX_CARRIED_FINDINGS,  # noqa: F401
    _MAX_DELETED_TEST_FILES,  # noqa: F401 — re-exported for callers/tests
    _MAX_FAILED_SMOKE_TESTS,  # noqa: F401 — re-exported for callers/tests
    _MIN_TELEMETRY_SAMPLE,  # noqa: F401 — re-exported for callers/tests
    _SEVERITY_RANK,
    _SEVERITIES_REQUIRING_VERIFICATION,
    _audit_finding_key,  # noqa: F401 — re-exported for callers/tests
    _audit_findings_carry_meta,
    _audit_missing_reason,
    compute_enforce_confidence_preview,
    DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT,
    MAX_COMPLETION_DEBT_WEIGHT,
    _clamp01,  # noqa: F401
    coerce_completion_debt_weight,
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
    _score_completion_debt_pressure,
    _score_smoketest,
    compute_challenged_vs_unchallenged_telemetry,  # noqa: F401 — re-exported for callers/tests
    compute_composite,
    evaluate_merge_grade,
    is_contentless_goal,
)
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
    _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS,
    _MAX_QUALIFICATION_DURATION_SECONDS,
    _MAX_SWEEP_CRASH_STREAK,  # noqa: F401 — re-exported for tests
    _force_finalize_sweep_crash_loop,  # noqa: F401 — re-exported for tests
    _force_finalize_timed_out_qualification,
    _parse_started_at,
    _fire_initial_qualification_if_coverage_complete,  # noqa: F401 — re-exported for tests
    _reconcile_if_criterion_coverage_now_complete,  # noqa: F401 — re-exported for tests
    _redrive_deferred_unsynced_qualification,  # noqa: F401 — re-exported for tests
    _redrive_dry_run_candidate,  # noqa: F401 — re-exported for tests
    _redrive_not_promoted_qualification,  # noqa: F401 — re-exported for tests
    sweep_stuck_qualifications,  # noqa: F401 — re-exported for reaper + tests
)
from app.migration_apply_gate import run_migration_apply_gate
from app.rollup_migration_validation import (
    check_qualification_migration_identity,
)
from app.task_capabilities import WRITE_TASK_TYPES
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


def _is_audited_surface_path(path: str) -> bool:
    """True when a file is part of the code surface a release audit judges.

    Excludes the mechanically-regenerated artifacts an audit does NOT judge —
    emitter-inventory snapshots and other ``__fixtures__``/``__snapshots__``,
    the module-size limits config, CI workflow files, generated test-timing
    data, and docs.  A candidate that differs from a prior audited candidate
    ONLY in these excluded files has the SAME audited surface, so its audit
    verdict can be reused (Guard 3) instead of re-rolling the ~15min LLM audit.
    Everything else — app code AND test logic — is IN the surface, so a real
    change always re-audits.
    """
    p = path.lower()
    if p.startswith(".github/"):
        return False
    if "/__fixtures__/" in p or "/__snapshots__/" in p:
        return False
    if p.endswith("module_size_limits.json"):
        return False
    if p.endswith(".test_durations"):
        return False
    return not p.endswith(".md")


def compute_audited_surface_hash(tree_entries: list[dict] | None) -> str | None:
    """Content hash of the audited code surface at a candidate ref.

    Built from the git blob SHAs of every tracked file EXCEPT the mechanically-
    regenerated artifacts a code audit does not judge (see
    ``_is_audited_surface_path``).  Two candidates whose audited surface is
    byte-identical hash identically, so the qualifier can REUSE a prior audit
    verdict for the newer commit (Guard 3) instead of re-rolling the audit — the
    "fixing CI / regenerating a snapshot must not re-run the whole qualification"
    lever (Henry, 2026-07-02).  Cheap: the whole tree (path + blob SHA) comes
    from ONE ``list_repo_tree_paths_at_ref`` call, no per-file reads.  Returns
    None when the tree is unavailable — fail-open: no reuse, the normal
    SHA-keyed cache path applies.
    """
    if not tree_entries:
        return None
    relevant = sorted(
        (str(e["path"]), str(e["sha"]))
        for e in tree_entries
        if isinstance(e, dict)
        and e.get("sha")
        and e.get("path")
        and _is_audited_surface_path(str(e["path"]))
    )
    if not relevant:
        return None
    h = hashlib.sha256()
    for path, sha in relevant:
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(sha.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _incremental_audit_base_sha(
    prior_qualification: dict[str, Any] | None,
    candidate_sha: str,
) -> str | None:
    """Return the prior fully-cleared audit SHA eligible as a delta baseline.

    Incremental review is safe only after an audit left no verified *or*
    unverified findings. A version may still be ``not_promoted`` for an
    independent gate, so status alone is deliberately not a proxy for audit
    cleanliness. Missing legacy fields fail closed to a full audit.

    The provenance shape is also suitable for Phase 1's
    ``QualificationDimensionResult.detail`` JSON. This branch intentionally
    stores it in ``last_qualification`` until that ledger is backmerged.
    """
    if not isinstance(prior_qualification, dict):
        return None
    if prior_qualification.get("status") not in {
        *_CONCLUSIVE_VERDICT_STATUSES,
        "promotion_failed",
    }:
        return None
    prior_sha = str(prior_qualification.get("candidate_sha") or "").strip()
    if not prior_sha or prior_sha == candidate_sha:
        return None
    sub_scores = prior_qualification.get("sub_scores")
    if not isinstance(sub_scores, dict) or not isinstance(
        sub_scores.get("audit"), (int, float)
    ):
        return None
    if prior_qualification.get("audit_findings"):
        return None
    if prior_qualification.get("excluded_audit_findings"):
        return None
    if prior_qualification.get("audit_findings_total") not in (0, None):
        return None
    if prior_qualification.get("excluded_audit_findings_total") not in (0, None):
        return None
    return prior_sha


def _promotion_failed_reuse_eligible(
    qual: dict[str, Any] | None, candidate_diff_hash: str | None
) -> bool:
    """True when a ``promotion_failed`` verdict can be re-used to RE-PROMOTE.

    A ``promotion_failed`` verdict ALWAYS means the audit PASSED and only the
    PROMOTION step failed (PR CI red / staleness / merge) — ``promote()`` is
    only ever reached from ``_finalize_qualification`` for a would-promote
    composite.  When the harvested fix landed ENTIRELY in an audit-EXCLUDED path
    (CI workflow / test fixture / config), the live audited-surface hash is
    byte-identical to the passing candidate, so that audit verdict still holds:
    the CURRENT candidate can be re-promoted WITHOUT re-running the ~15min audit
    (which also re-rolls a non-deterministic score — the v21 treadmill, passed
    0.9657 → CI-failed → re-qualified 0.6171).  Henry, 2026-07-02.

    Strictly gated (any miss ⇒ fall through to the normal re-audit — a CHANGED
    surface, i.e. an app-code fix, MUST re-audit): the prior status is
    ``promotion_failed`` AND the LIVE ``candidate_diff_hash`` is truthy AND it
    equals the prior verdict's ``candidate_diff_hash``.  The cap
    (``_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS``) is enforced separately by the
    caller so a persistently-red candidate cannot re-promote forever.
    """
    if not isinstance(qual, dict):
        return False
    if qual.get("status") != "promotion_failed":
        return False
    prior_hash = qual.get("candidate_diff_hash")
    return bool(candidate_diff_hash and prior_hash and candidate_diff_hash == prior_hash)


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
        # completion_debt is an OPTIONAL, SEPARATE blend factor — NOT part of the
        # three-core-weight sum above.  Read + preserve it (default 0.0, clamped
        # to [0, 0.5]) so the mode gate in _resolve_policy_with_provenance can
        # turn the debt-pressure signal on; absent => 0.0 = the legacy behaviour.
        _cd = coerce_completion_debt_weight(weights_raw)
        if _cd > 0.0:
            weights["completion_debt"] = _cd
        result: dict[str, Any] = {"weights": weights, "threshold": threshold}
        for _k in ("merge_grade_block_threshold",):
            _v = _runtime_config.get(_k)
            if _v is not None:
                with contextlib.suppress(TypeError, ValueError):
                    result[_k] = float(_v)
        return result
    except Exception as exc:  # noqa: BLE001 — fall back to the file/default on any failure
        logger.warning(
            "[RELEASE_QUALIFIER] runtime_config promotion rubric is unusable "
            "(%s); falling back to promotion_policy.yaml / defaults",
            exc,
        )
        return None


# A project's per-project promotion threshold is clamped to this floor so a
# project can never set a near-zero bar that silently disables its own gate.
_SAFE_PROMOTION_THRESHOLD_FLOOR = 0.5
# The audit dimension must carry real weight in any per-project rubric.
# compute_composite only hard-blocks a MISSING audit, so a rubric that zeroes the
# audit weight would let a terrible-but-present audit promote — reject it.
_MIN_AUDIT_WEIGHT = 0.2
# Margin by which the threshold is clamped above (1 - audit_weight) so a
# zero-audit candidate strictly fails the composite (audit can always veto).
_AUDIT_VETO_MARGIN = 0.05


def _policy_from_build_config(project: Project | None) -> dict[str, Any] | None:
    """Resolve a per-project promotion rubric from ``Project.build_config``.

    A project may LEGITIMATELY promote at a different bar than the fleet default
    (an external research/witness repo vs Blueprint's trusted self-improvement
    core), so ``build_config`` carries an optional per-project override on the
    SAME schemaless JSONB seam that already holds ``requires_ci_checks`` /
    ``has_ci`` — no new settings surface, no migration.  Returns
    ``{"weights", "threshold"}`` or ``None`` when the project sets no override
    (fall through to the fleet rubric).  Never raises.

    Safety: the threshold is clamped to ``_SAFE_PROMOTION_THRESHOLD_FLOOR`` so a
    project cannot set a near-zero bar; weights must sum ~1.0 (mirrors the YAML
    validation) or the override is ignored and the fleet rubric applies.
    """
    bc = (project.build_config or {}) if project is not None else {}
    threshold_raw = bc.get("promotion_threshold")
    weights_raw = bc.get("promotion_weights")
    if threshold_raw is None and weights_raw is None:
        return None
    try:
        if weights_raw is not None:
            weights = {
                "audit": float(weights_raw["audit"]),
                "test_erosion": float(weights_raw["test_erosion"]),
                "smoketest": float(weights_raw["smoketest"]),
            }
            wsum = weights["audit"] + weights["test_erosion"] + weights["smoketest"]
            if abs(wsum - 1.0) > 0.01:
                raise ValueError(
                    f"build_config promotion_weights sum to {wsum:.4f}, expected ~1.0"
                )
            # Optional per-project completion_debt blend factor (separate from the
            # three-core-weight sum; default 0.0, clamped to [0, 0.5]).
            _cd = coerce_completion_debt_weight(weights_raw)
            if _cd > 0.0:
                weights["completion_debt"] = _cd
            if weights["audit"] < _MIN_AUDIT_WEIGHT:
                raise ValueError(
                    f"build_config promotion_weights audit={weights['audit']:.3f} "
                    f"below the {_MIN_AUDIT_WEIGHT} floor — the audit dimension "
                    f"must carry real weight"
                )
        else:
            weights = dict(_DEFAULT_POLICY["weights"])
        threshold = (
            float(threshold_raw)
            if threshold_raw is not None
            else float(_DEFAULT_POLICY["threshold"])
        )
        # Audit-veto coupling (round-2 review finding B): the audit-weight floor
        # and the threshold floor are independent, so at their joint minimum
        # (audit_weight=0.2, threshold=0.5) a candidate with a PRESENT-but-terrible
        # audit (score 0.0 — no audit_missing hard block) still promotes
        # (0.2·0 + test_erosion + smoketest = 0.8 ≥ 0.5).  A zero-audit candidate
        # scores at most (1 - audit_weight), so CLAMP the threshold up to strictly
        # exceed that — the audit dimension can then ALWAYS veto a promotion, for
        # any per-project weights.  (Clamping up rather than rejecting keeps the
        # override usable; it only ever makes the gate stricter.)
        _audit_veto_floor = 1.0 - weights["audit"] + _AUDIT_VETO_MARGIN
        threshold = max(
            threshold, _SAFE_PROMOTION_THRESHOLD_FLOOR, _audit_veto_floor
        )
        return {"weights": weights, "threshold": threshold}
    except Exception as exc:  # noqa: BLE001 — a bad per-project override falls through
        logger.warning(
            "[RELEASE_QUALIFIER] project %s build_config promotion rubric invalid "
            "(%s) — falling back to the fleet rubric",
            getattr(project, "id", "?"),
            exc,
        )
        return None


# The operator-facing graduation gate for the completion-debt pressure signal.
# ``completion_debt_weight_mode`` is a real runtime_config key (default
# "observe"); only "enforce" lets the configured completion_debt weight reach
# the composite blend.
_COMPLETION_DEBT_WEIGHT_MODES: frozenset[str] = frozenset({"off", "observe", "enforce"})


def _resolve_completion_debt_weight_mode() -> str:
    """Return the live ``completion_debt_weight_mode`` (default "observe").

    Any unrecognised / unset value degrades to the safe "observe" default so a
    malformed knob can never silently arm the enforce blend.
    """
    mode = str(_runtime_config.get("completion_debt_weight_mode", "observe")).strip().lower()
    return mode if mode in _COMPLETION_DEBT_WEIGHT_MODES else "observe"


def _apply_completion_debt_weight_mode(policy: dict[str, Any]) -> dict[str, Any]:
    """Mode-gate a resolved policy's ``weights["completion_debt"]`` blend factor.

    Centralises Defect 2's graduation gate so ``compute_composite`` stays pure:
    the returned policy already carries the *effective* completion_debt weight.

      * "observe" / "off"  → the effective weight is forced to 0.0 and the key is
        DROPPED, so ``weights`` is the legacy three-key shape and the debt-
        pressure score is recorded in sub_scores but never blended (the shipped
        observe behaviour, backward compatible).
      * "enforce"          → the configured completion_debt weight is applied; if
        the operator graduated but left it unset/zero, fall back to
        ``DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT`` so graduation actually changes
        the composite.  The effective weight is clamped to
        ``[0, MAX_COMPLETION_DEBT_WEIGHT]``.

    Returns a shallow copy with a fresh ``weights`` dict — the caller's input is
    never mutated.
    """
    weights = dict(policy.get("weights") or {})
    configured = coerce_completion_debt_weight(weights)
    mode = _resolve_completion_debt_weight_mode()
    if mode == "enforce":
        effective = (
            configured
            if configured > 0.0
            else DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT
        )
        effective = max(0.0, min(effective, MAX_COMPLETION_DEBT_WEIGHT))
    else:
        effective = 0.0
    if effective > 0.0:
        weights["completion_debt"] = effective
    else:
        weights.pop("completion_debt", None)
    gated = dict(policy)
    gated["weights"] = weights
    return gated


def _resolve_policy_with_provenance(
    project: Project | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    """Resolve the promotion rubric and return its active source + fallback reason.

    The policy payload itself keeps the legacy ``{"weights": ..., "threshold": ...}``
    shape. This helper layers operator-visible provenance on top so callers can tell
    whether the active rubric came from the project's build_config, runtime config,
    the YAML file, or the hardcoded defaults after a YAML failure.

    The final step mode-gates ``weights["completion_debt"]`` via
    ``_apply_completion_debt_weight_mode`` so every consumer (the qualifier and
    the admin provenance surface) sees the same effective, graduation-gated blend
    factor.
    """
    fleet_policy, fleet_source, fleet_error = _resolve_fleet_policy_with_provenance()
    overrides = _policy_from_build_config(project)
    if overrides is not None:
        # Overlay ONLY the per-project threshold/weights onto the fleet policy so
        # fleet-only keys (e.g. merge_grade_block_threshold) are PRESERVED — a
        # per-project bar must not silently disarm the merge-grade goal-coverage
        # blocker that the runtime_config / yaml tiers carry.
        merged = dict(fleet_policy)
        merged["weights"] = overrides["weights"]
        merged["threshold"] = overrides["threshold"]
        return _apply_completion_debt_weight_mode(merged), "build_config", fleet_error
    return _apply_completion_debt_weight_mode(fleet_policy), fleet_source, fleet_error


def _resolve_fleet_policy_with_provenance() -> tuple[dict[str, Any], str, str | None]:
    """Resolve the FLEET promotion rubric (no per-project override): live
    ``runtime_config`` → ``promotion_policy.yaml`` → hardcoded defaults."""
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

        # Optional completion_debt blend factor from the on-disk rubric (separate
        # from the three-core-weight sum; default 0.0, clamped to [0, 0.5]).
        _cd = coerce_completion_debt_weight(weights_raw)
        if _cd > 0.0:
            weights["completion_debt"] = _cd

        result: dict[str, Any] = {"weights": weights, "threshold": threshold}
        for _k in ("merge_grade_block_threshold",):
            if _k in parsed:
                with contextlib.suppress(TypeError, ValueError):
                    result[_k] = float(parsed[_k])
        return result, "yaml_file", None
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


def _load_policy(project: Project | None = None) -> dict[str, Any]:
    """Resolve the promotion rubric: project build_config → runtime_config → YAML → defaults.

    Resolution order:
      0. ``project.build_config['promotion_threshold'/'promotion_weights']`` — an
         optional per-project override (clamped to a safety floor), for a repo
         that legitimately promotes at a different bar than the fleet default.
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
       "threshold": float}`` with every value coerced to float.  An OPTIONAL
    ``weights["completion_debt"]`` blend factor is present only when the operator
    has graduated ``completion_debt_weight_mode`` to "enforce" (mode-gated in
    ``_resolve_policy_with_provenance``); in observe/off mode the key is absent
    and the three-core-weight shape is preserved unchanged.
    """
    policy, _, _ = _resolve_policy_with_provenance(project)
    return policy


def _resolve_configured_completion_debt_weight(
    project: Project | None = None,
) -> float:
    """Resolve the completion_debt blend weight through the SAME provenance chain
    the enforce blend reads (build_config > runtime_config > yaml), BEFORE the
    observe/off mode gate drops it.

    ``_apply_completion_debt_weight_mode``'s enforce branch reads its configured
    weight via ``coerce_completion_debt_weight(policy["weights"])`` on the fully
    resolved (pre-gate) policy — which honours a per-project build_config override
    and the on-disk YAML rubric, not just runtime_config.  The observe-mode
    ``debt_enforce_confidence_preview`` must feed ``compute_enforce_confidence_preview``
    that SAME configured weight; reading ``_runtime_config["promotion_weights"]``
    alone would show operators a preview derived from a different weight than
    enforce would actually apply (finding: preview/enforce weight-source drift).

    Mirrors the build_config-vs-fleet selection in ``_resolve_policy_with_provenance``
    exactly: a valid build_config override supplies the weight, otherwise the fleet
    (runtime_config → yaml → defaults) rubric does.
    """
    overrides = _policy_from_build_config(project)
    if overrides is not None:
        return coerce_completion_debt_weight(overrides["weights"])
    fleet_policy, _, _ = _resolve_fleet_policy_with_provenance()
    return coerce_completion_debt_weight(fleet_policy["weights"])


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
    # The blind merge-grade child is required whenever it was spawned (a
    # non-empty version goal) — finalize must wait for it like the audit child.
    if qual.get("merge_grade_task_id"):
        required_keys.append("merge_grade_task_id")

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

    rows = (
        await session.execute(
            select(Task.id, Task.status).where(Task.id.in_(child_ids))
        )
    ).all()
    found_ids = {r.id for r in rows}
    if any(cid not in found_ids for cid in child_ids):
        # A child row is not (yet) persisted — the spawning run is still
        # in progress; treat as in flight.
        return True
    # status == "pending" but every child already terminated → not in flight.
    return any(r.status not in _TERMINAL_STATUSES for r in rows)


def _promotion_in_flight_result(
    version: release_version.ReleaseVersion,
    *,
    latest_verdict: Any = None,
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

    When ``latest_verdict`` is passed, applies finalized-verdict substitution
    (via :func:`~app.qualification_state_resolver.apply_verdict_substitution`)
    before classifying so stale-pending records with an already-finalized verdict
    row are not misclassified as still-blocking.
    """
    from app.qualification_state_resolver import apply_verdict_substitution

    qual = (
        version.last_qualification
        if isinstance(version.last_qualification, dict)
        else {}
    )
    effective_qual, _ = apply_verdict_substitution(qual, latest_verdict)
    qual_state = release_version.summarize_qualification_state(effective_qual)
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

    if not prs:
        # ``list_pull_requests_for_commit`` collapses API failures and genuine
        # empty results to ``[]``. Re-check the raw response so a transient
        # ``commits/{sha}/pulls`` failure stays strict instead of self-clearing
        # as definitive ``no_checks``.
        try:
            response = await github._github_request(  # type: ignore[attr-defined]
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/commits/{candidate_sha}/pulls?per_page=100",
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                project_id=project_id,
            )
        except Exception as exc:  # noqa: BLE001 — raw PR lookup failure → pending
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
        if response.status_code != 200:
            logger.warning(
                "[RELEASE_QUALIFIER] PR-for-commit lookup failed for %s/%s@%s: "
                "HTTP %s",
                owner,
                repo,
                candidate_sha[:8],
                response.status_code,
            )
            return {
                "state": _CI_PENDING,
                "ci_green": False,
                "resolved_sha": None,
                "detail": f"pr-for-commit lookup failed: HTTP {response.status_code}",
            }
        try:
            prs = response.json()
        except (ValueError, TypeError) as exc:
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
        if not isinstance(prs, list):
            return {
                "state": _CI_PENDING,
                "ci_green": False,
                "resolved_sha": None,
                "detail": "pr-for-commit lookup returned a non-list body",
            }

    # Prefer merged PRs; fall back to every associated PR when none carry merged_at.
    merged_prs = [pr for pr in prs if isinstance(pr, dict) and pr.get("merged_at")]
    use_prs = merged_prs or [pr for pr in prs if isinstance(pr, dict)]
    all_head_shas: list[str] = []
    for pr in use_prs:
        head = pr.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if head_sha and head_sha != candidate_sha and head_sha not in all_head_shas:
            all_head_shas.append(head_sha)

    # Cap the sweep to avoid exhausting API quota on repos with many PRs.
    head_shas = all_head_shas[:_PR_HEAD_SWEEP_MAX]
    truncated = len(all_head_shas) > len(head_shas)

    # Log a warning if we had to truncate the PR head sweep.
    if truncated:
        logger.warning(
            "[RELEASE_QUALIFIER] CI resolution: commit %s@%s/%s is associated with "
            "%d PR heads; checking at most %d PR heads (truncated)",
            candidate_sha[:8],
            owner,
            repo,
            len(all_head_shas),
            _PR_HEAD_SWEEP_MAX,
        )

    if not head_shas:
        return {
            "state": _CI_NO_CHECKS,
            "ci_green": False,
            "resolved_sha": None,
            "detail": "no check-runs on candidate and no associated PR heads",
        }

    # Exhaustive RED-preferring sweep: inspect every PR head before deciding.
    # A red on ANY head must win over a green on another head regardless of the
    # order GitHub returns them — return green only when at least one head is
    # green AND no associated head is red.
    first_red_sha: str | None = None
    first_green_sha: str | None = None
    saw_pending = False
    saw_error = False
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
            saw_error = True
            continue
        pr_state = _classify_check_runs(pr_checks)
        if pr_state == _CI_RED and first_red_sha is None:
            first_red_sha = head_sha
        elif pr_state == _CI_GREEN and first_green_sha is None:
            first_green_sha = head_sha
        elif pr_state == _CI_PENDING:
            saw_pending = True
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
    if saw_error and not saw_pending:
        return {
            "state": _CI_PENDING,
            "ci_green": False,
            "resolved_sha": None,
            "detail": "PR head check-runs fetch failed",
        }
    if saw_pending:
        return {
            "state": _CI_PENDING,
            "ci_green": False,
            "resolved_sha": None,
            "detail": "PR head check-runs still pending or unavailable",
        }
    return {
        "state": _CI_NO_CHECKS,
        "ci_green": False,
        "resolved_sha": None,
        "detail": "no check-runs on candidate or associated PR heads",
    }


async def _annotate_absent_ci_integration_topology(
    *,
    token: str,
    owner: str,
    repo: str,
    project: Project,
    candidate_sha: str,
    ci_resolution: dict[str, Any],
) -> dict[str, Any]:
    """Add finalize-time topology provenance for a definitive no-checks result.

    The scorer still receives a plain ``ci_required`` boolean.  This provenance
    lets the finalizer relax that boolean only when the absent candidate-level
    CI belongs to the configured integration branch; comparison failures are
    recorded as indeterminate and remain strict.
    """
    if ci_resolution.get("state") != _CI_NO_CHECKS:
        return ci_resolution

    integration_branch, promotion_target = _lts_branch_names(project)
    topology: dict[str, Any] = {
        "integration_branch": integration_branch,
        "promotion_target": promotion_target,
        "candidate_reachable_from_integration": None,
    }
    if integration_branch == promotion_target:
        topology["detail"] = "integration branch equals promotion target"
        return {**ci_resolution, "integration_branch_topology": topology}
    if requires_ci_checks(project):
        topology["detail"] = "project requires candidate CI checks"
        return {**ci_resolution, "integration_branch_topology": topology}

    try:
        reachable = await github.is_ancestor_of(
            token,
            owner,
            repo,
            candidate_sha,
            integration_branch,
            project_id=str(project.id),
        )
    except Exception as exc:  # noqa: BLE001 — topology-unavailable stays strict
        logger.warning(
            "[RELEASE_QUALIFIER] finalize CI topology check failed for %s/%s@%s "
            "against %s: %s",
            owner,
            repo,
            candidate_sha[:8],
            integration_branch,
            exc,
        )
        topology["detail"] = f"integration topology check failed: {exc}"
        return {**ci_resolution, "integration_branch_topology": topology}

    topology["candidate_reachable_from_integration"] = reachable
    if reachable is True:
        topology["detail"] = "candidate is reachable from integration branch"
    elif reachable is False:
        topology["detail"] = "candidate is not reachable from integration branch"
    else:
        topology["detail"] = "integration topology check was indeterminate"
    return {**ci_resolution, "integration_branch_topology": topology}


def _ci_required_for_finalize(
    project: Project | None,
    ci_resolution: dict[str, Any],
) -> bool:
    """Return whether the finalize scorer must hard-block unresolved CI."""
    if project is None:
        return True
    if ci_resolution.get("state") != _CI_NO_CHECKS:
        return True
    if requires_ci_checks(project):
        return True

    integration_branch, promotion_target = _lts_branch_names(project)
    if integration_branch == promotion_target:
        return True

    topology = ci_resolution.get("integration_branch_topology")
    if not isinstance(topology, dict):
        return True
    if topology.get("candidate_reachable_from_integration") is not True:
        return True
    return not proceed_on_no_checks(project, integration_branch)


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
        ci_resolution = await _annotate_absent_ci_integration_topology(
            token=token,
            owner=owner,
            repo=repo,
            project=project,
            candidate_sha=candidate_sha,
            ci_resolution=ci_resolution,
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
        if "integration_branch_topology" in ci_resolution:
            resolution_provenance["integration_branch_topology"] = ci_resolution[
                "integration_branch_topology"
            ]
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
    *,
    session_factory=None,
    allow_synchronous_reuse_promote: bool = False,
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

    # Cheap pre-lock promotion-in-flight check (no I/O beyond verdict fetch).
    # A version whose audit passed and now has an open promotion PR auto-merging
    # must not be re-qualified: doing so spawns a fresh heavy audit/smoketest
    # pair and clobbers the promotion-pending record.  Consistent with the
    # version-cut lever, which refuses in the same state.  Best-effort fast path
    # — the authoritative re-check happens under the row lock below.
    # Fetch the newest verdict so stale-pending+finalized-verdict is not
    # misclassified as still-blocking.
    from app.qualification_state_resolver import resolve_newest_qualification_verdict

    _latest_verdict_prelock = await resolve_newest_qualification_verdict(
        session, version
    )
    _promo = _promotion_in_flight_result(version, latest_verdict=_latest_verdict_prelock)
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
            token, owner, repo, integration_branch, live=True
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
            "detail": f"{type(exc).__name__}: {exc}"[:300],
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

    # 3a. Content hash of the audited surface at the candidate (cheap — ONE tree
    #     call, no per-file reads).  Lets Guard 3 reuse a prior conclusive verdict
    #     when a newer commit differs ONLY in CI/fixture/config files: fixing CI
    #     or regenerating a snapshot must NOT re-run the whole ~15min audit
    #     (Henry, 2026-07-02).  Fail-open: list_repo_tree_paths_at_ref returns
    #     None on any error, so candidate_diff_hash is None → no reuse, the
    #     SHA-keyed cache path applies unchanged.
    #
    #     Skip the tree call entirely when a SAME-SHA conclusive verdict already
    #     stands: Guard 3 will serve that from the SHA cache and never reach the
    #     cross-SHA reuse branch, so the hash (needed only for cross-SHA reuse +
    #     storage on a fresh spawn) would be a wasted API call every strategic
    #     cycle a version sits not_promoted.  The pre-lock read may be stale; a
    #     wrong skip only forgoes reuse (candidate_diff_hash=None → fail-open),
    #     never causes a wrong reuse.
    _pre_lock_qual = version.last_qualification or {}
    _same_sha_conclusive = (
        _pre_lock_qual.get("status") in _CONCLUSIVE_VERDICT_STATUSES
        and _pre_lock_qual.get("candidate_sha") == candidate_sha
    )
    candidate_diff_hash = (
        None
        if _same_sha_conclusive
        else compute_audited_surface_hash(
            await github.list_repo_tree_paths_at_ref(
                token, owner, repo, candidate_sha, project_id=str(project_id)
            )
        )
    )

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

    # 3c. Passing-audit reuse across a promotion_failed → RE-PROMOTE without
    #     re-auditing (Henry, 2026-07-02).  A promotion_failed verdict ALWAYS
    #     means the audit PASSED and only the promotion step failed (PR CI red /
    #     staleness / merge).  When the harvested fix landed ENTIRELY in an
    #     audit-EXCLUDED path (CI workflow / test fixture / config), the live
    #     audited-surface hash equals the passing candidate's, so the audit still
    #     holds — re-attempt the promotion on the CURRENT candidate (it carries
    #     the fix; its surface == the passed one by hypothesis) WITHOUT re-running
    #     the ~15min audit or re-rolling the non-deterministic score (the v21
    #     treadmill).  Strictly gated: prior status promotion_failed AND the live
    #     candidate_diff_hash is truthy AND == the prior verdict's hash AND the
    #     promotion-failure re-qualify cap is NOT exhausted.  ANY miss (a CHANGED
    #     surface = an app-code fix, or the cap reached) FALLS THROUGH to the
    #     normal re-audit below — fail-safe.
    #
    #     Lock/session discipline (critical): the promote runs in a DEDICATED
    #     AsyncSessionLocal session, NEVER this caller's — otherwise promote()'s
    #     intermediate commit (which it issues to free the row lock before its
    #     30-120s of GitHub I/O) would prematurely commit the caller's unrelated
    #     dirty work.  This runs BEFORE this function's own FOR UPDATE lock (step
    #     4 below): the dedicated session must be free to SELECT..FOR UPDATE the
    #     version row, which this caller has NOT yet locked, so there is no
    #     self-deadlock.  The dedicated helper re-verifies the gate + cap under
    #     its OWN row lock (authoritative — the pre-lock read may be stale) and
    #     reconstructs composite/base_record from the passing fields the
    #     promotion_failed record carries; a None return (state changed, cap hit,
    #     candidate already on main, lock contended) falls through to re-audit.
    if _promotion_failed_reuse_eligible(_pre_lock_qual, candidate_diff_hash) and (
        int(_pre_lock_qual.get("promotion_failure_count") or 0)
        < _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS
    ):
        if not allow_synchronous_reuse_promote:
            # DEFER the reuse-promote to the sweep re-driver (review finding 1).
            # promote() → _record_promotion_success synchronously activates staged
            # directions via a nested UPDATE on a SEPARATE connection.  Run from a
            # caller that holds row locks — notably the STRATEGIC transaction, which
            # may hold the very Direction/Project rows that UPDATE needs — the
            # await-suspended caller would hang forever (Postgres can't see the
            # cross-connection wait cycle, so its deadlock detector never fires).
            # ONLY the promotion_failed sweep re-driver runs OUTSIDE any lock-holding
            # request AND re-drives promotion_failed every cycle, so it is the sole
            # caller that sets allow_synchronous_reuse_promote.  Return a no-op
            # deferral here (NOT a re-audit — that would re-introduce the treadmill);
            # the sweep performs the promote safely on its next pass.
            logger.info(
                "[RELEASE_QUALIFIER] run_qualification: v%d is promotion_failed with "
                "an unchanged audited surface — DEFERRING the passing-audit "
                "reuse-promote to the sweep re-driver (a synchronous promote inside "
                "this lock-holding caller could hang on staged-direction activation)",
                version.version_number,
            )
            return {
                "status": "promotion_failed_reuse_deferred",
                "candidate_sha": candidate_sha,
            }
        _reuse_result = await _reattempt_promotion_failed_in_dedicated_session(
            version.id,
            candidate_sha,
            candidate_diff_hash,
            session_factory=session_factory,
        )
        if _reuse_result is not None:
            logger.info(
                "[RELEASE_QUALIFIER] run_qualification: v%d reused the PASSING "
                "audit of a promotion_failed candidate (audited surface "
                "unchanged) and re-attempted promotion on %s WITHOUT "
                "re-auditing — result=%s",
                version.version_number,
                candidate_sha[:8],
                _reuse_result.get("status"),
            )
            return {**_reuse_result, "reused_audit": True}

    # 4. Take the ReleaseVersion row lock (audit C3).  The strategic
    #    qualify_request path and the admin POST /qualify endpoint can both
    #    reach here concurrently; without serializing them they each pass the
    #    in-flight guard and spawn a duplicate audit+smoketest pair, and the
    #    later last_qualification write clobbers the earlier one.  FOR UPDATE
    #    mirrors maybe_finalize_qualification's pattern — held across the
    #    in-flight check, the spawns, and the last_qualification write.  The
    #    second caller blocks here until the first commits, then sees the
    #    pending record and short-circuits at the in-flight guard below.
    #
    #    ``populate_existing=True`` (+ an explicit ``expire`` for callers that
    #    pre-loaded the row) is the correctness linchpin: when ``version`` was
    #    already cached in this session's identity map — notably the strategic
    #    ``qualify_request`` path, which loads the open version via
    #    ``current_open_version(session, ...)`` and may then hold a copy that
    #    PRE-DATES a ``promote()`` committed in another session — a bare
    #    ``with_for_update()`` SELECT acquires the DB lock but returns that SAME
    #    cached instance WITHOUT refreshing its attributes, so the under-lock
    #    guards below (phase / in-flight / promotion-in-flight / cached-verdict)
    #    would read STALE ``last_qualification``.  That is how a re-qualify slips
    #    past the promotion-in-flight guard while a promotion PR is genuinely open
    #    in ``promotion_check_wait`` and spawns a redundant audit + merge-grade.
    #    Forcing the loaded row to overwrite the cached attributes makes the
    #    under-lock guards authoritative regardless of caller staleness (mirrors
    #    the ``populate_existing`` pattern the managed-check-wait reconciler
    #    helpers already use).  ``populate_existing`` overwrites the attributes of
    #    an instance already present in the identity map, so it is sufficient on
    #    its own — ``version`` is not mutated before this point, so no dirty write
    #    is lost.
    locked_version = (
        (
            await session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version.id)
                .with_for_update()
                .execution_options(populate_existing=True)
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
    # Re-fetch the verdict under the lock (the pre-lock fetch may be stale if a
    # concurrent promotion finalized between the pre-lock check and here).
    _latest_verdict_locked = await resolve_newest_qualification_verdict(
        session, version
    )
    _promo = _promotion_in_flight_result(version, latest_verdict=_latest_verdict_locked)
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
    # ``ci_unresolved`` is the narrow transient exception: a same-SHA
    # not_promoted verdict may re-run once the fresh CI snapshot is green.
    # ``criterion_coverage_incomplete`` is the second narrow exception: a
    # same-SHA not_promoted verdict may re-run once the live coverage summary
    # shows the coverage gate would no longer fire (a retracted/superseded
    # criterion cleared the wedge) — the acceptance-contract safety valve.
    prior_qual = version.last_qualification or {}
    prior_status = prior_qual.get("status")
    ci_unresolved_recovered_same_sha = (
        prior_status == "not_promoted"
        and prior_qual.get("blocker") == "ci_unresolved"
        and prior_qual.get("candidate_sha") == candidate_sha
        and ci_green
    )
    # Criterion-coverage recovery — the acceptance-contract safety valve.  A
    # same-SHA not_promoted verdict whose blocker was
    # ``criterion_coverage_incomplete`` (set in release_scoring when active
    # criteria are uncovered) may re-run once the LIVE coverage summary shows the
    # gate would no longer fire: every active criterion covered, or ZERO active
    # criteria remain after a retraction/supersession cleared an uncoverable one.
    # This is the deadlock this direction exists to break — retracting an
    # uncoverable criterion must let the qualifier re-audit and clear the gate
    # WITHOUT a SHA change or operator action.  Mirrors the ``ci_unresolved``
    # narrow exception exactly: it relaxes caching for THIS blocker only and
    # only on the SAME SHA (a different SHA already re-runs via the normal cache
    # miss), and every downstream gate still applies to the fresh re-audit.
    criterion_coverage_recovered_same_sha = False
    if (
        prior_status == "not_promoted"
        and prior_qual.get("blocker") == "criterion_coverage_incomplete"
        and prior_qual.get("candidate_sha") == candidate_sha
    ):
        try:
            _coverage = await get_version_coverage_summary(session, version.id)
            _total = int(_coverage.get("total") or 0)
            _covered = int(_coverage.get("covered") or 0)
            _attested = int(_coverage.get("attested") or 0)
            # Mirror the release_scoring gate exactly: it fires when
            # ``total > 0 and (total - covered - attested) > 0``, so the gate is
            # CLEAR when no active criteria remain (total == 0), every active one
            # is machine-covered, or the remaining ones are at least covered by a
            # non-expired operator attestation (which clears the blocker but
            # incurs the confidence penalty rather than wedging not_promoted).
            criterion_coverage_recovered_same_sha = (
                _total == 0 or (_total - _covered - _attested) <= 0
            )
        except Exception:  # noqa: BLE001 — a coverage lookup failure stays cached (safe: no wrong re-run)
            logger.warning(
                "[RELEASE_QUALIFIER] run_qualification: coverage-recovery check "
                "failed for v%d — leaving the not_promoted verdict cached",
                version.version_number,
            )
            criterion_coverage_recovered_same_sha = False
    # Content-hash reuse: a newer commit whose AUDITED SURFACE is byte-identical
    # to the prior conclusive candidate (differs only in excluded CI/fixture/
    # config files) carries the same verdict — reuse it instead of re-rolling the
    # audit.  This is the "fixing CI / regenerating a snapshot must not re-run the
    # whole qualification" lever; it never wedges a not_promoted (real findings
    # are app-code, so fixing them changes the hash and re-audits).
    # Content-hash CROSS-SHA reuse is valid ONLY for a verdict whose outcome is
    # fully determined by the AUDITED SURFACE.  That holds for promoted / dry_run
    # (a would-promote decision driven by audit + test-erosion), but NOT for
    # not_promoted: a not_promoted can be blocked on state OUTSIDE the audited-
    # surface hash — CI (.github/), operator criterion attestation, live
    # migration-apply drift, an operator-lowered merge-grade threshold — any of
    # which is fixable WITHOUT changing the hash.  Reusing such a verdict across
    # SHAs would wedge the version not_promoted even after the real fix lands
    # (round-2 review finding A, generalizing the CI-only wedge round 1 found).
    # So restrict cross-SHA reuse to non-not_promoted conclusive verdicts; a
    # not_promoted only reuses on the SAME SHA, so its externally-determined
    # blocker is re-evaluated on any HEAD advance.
    _prior_diff_hash = prior_qual.get("candidate_diff_hash")
    _same_audited_surface = bool(
        candidate_diff_hash
        and _prior_diff_hash
        and candidate_diff_hash == _prior_diff_hash
        and prior_status != "not_promoted"
    )
    if (
        prior_status in _CONCLUSIVE_VERDICT_STATUSES
        and (
            prior_qual.get("candidate_sha") == candidate_sha
            or _same_audited_surface
        )
        and not ci_unresolved_recovered_same_sha
        and not criterion_coverage_recovered_same_sha
    ):
        _reuse_reason = (
            "automain unchanged"
            if prior_qual.get("candidate_sha") == candidate_sha
            else "audited surface unchanged (CI/fixture-only diff)"
        )
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d already has a "
            "conclusive %s verdict for candidate %s (%s) — skipping (cached "
            "verdict)",
            version.version_number,
            prior_status,
            candidate_sha[:8],
            _reuse_reason,
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
    # finalization wrote a non-infra_failure status OR when the candidate SHA
    # changes (a new commit should not inherit infra failures from prior SHAs).
    prior_infra_failure_candidate_sha = (
        prior_qual.get("candidate_sha")
        if prior_qual.get("status") == "infra_failure"
        else None
    )
    prior_infra_streak: int = (
        int(prior_qual.get("consecutive_infra_errors") or 0)
        if (
            prior_qual.get("status") == "infra_failure"
            and prior_infra_failure_candidate_sha == candidate_sha
        )
        else 0
    )
    prior_infra_failure_signature = (
        prior_qual.get("infra_failure_signature")
        if prior_qual.get("status") == "infra_failure"
        else None
    )
    infra_attempt_history = (
        prior_qual.get("infra_attempt_history")
        if prior_qual.get("status") == "infra_failure"
        else []
    )
    # Carry forward the prior-superseded flag: when the reconciler previously
    # wrote blocker='qualification_superseded' (force-promote out-of-band), the
    # new qualification's diff is stabilization-only and grading it against the
    # frozen goal is meaningless.  Sticky: once set it survives across infra-
    # failure retry cycles until a genuine new version cycle clears it.
    prior_superseded: bool = bool(
        prior_qual.get("prior_superseded")
        or prior_qual.get("blocker") == "qualification_superseded"
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
    # Per-project override on the build_config seam (a project may or may not
    # have a smoke suite), falling back to the fleet-global runtime_config flag.
    smoketest_enabled = bool(
        (project.build_config or {}).get(
            "smoketest_enabled", _runtime_config.get("smoketest_enabled")
        )
    )
    audit_task_id: str | None = None
    challenge_task_id: str | None = None
    audit_challenge_task_id: str | None = None
    audit_challenge_spawn_error: str | None = None
    audit_challenge_spawn_pending = True
    smoketest_task_id: str | None = None
    merge_grade_task_id: str | None = None
    # Blind merge-grade gate (dir d-c0fe439f): grade the merged diffs against the
    # frozen goal.  The goal is the ONLY narrative the grader sees — the spawn
    # helper enforces blindness to running_update.  When the goal is empty or a
    # contentless placeholder (e.g. "test", "n/a") there is nothing to grade
    # against, so no child is spawned and the finalize gate is a no-op PASS (see
    # _finalize_qualification).  Contentless == empty for this purpose.
    version_goal = (version.goal or "").strip()
    _merge_grade_goal_usable = bool(version_goal) and not is_contentless_goal(version_goal)
    # Consult the per-dimension fact ledger before spawning children (Phase 1
    # ledger reuse).  On a re-drive at the same candidate_sha, dimensions that
    # already resolved (passed/failed/blocked) in a prior finalize are carried
    # forward from the ledger — their child tasks are NOT re-spawned, and only
    # the dimensions still unavailable/pending are re-run.  This is the piece
    # that flattens the "attempt 12→13" re-audit cost when a single gate
    # (e.g. merge-grade) hit a transient infra failure.
    prior_dim_results = await _load_prior_dimension_results(
        session, version, candidate_sha
    )
    resolved_dims: dict[str, dict[str, Any]] = {}
    for dim, row in prior_dim_results.items():
        if row.status in _LEDGER_RESOLVED_STATUSES:
            resolved_dims[dim] = {
                "status": row.status,
                "sub_score": row.sub_score,
                "blocker": row.blocker,
                "detail": row.detail,
            }
    # A child task is needed only when NOT ALL of its produced dimensions are
    # already resolved.  audit task → {audit, test_erosion}; smoketest task →
    # {smoketest}; merge_grade task → {merge_grade}.
    _audit_needed = not (
        "audit" in resolved_dims and "test_erosion" in resolved_dims
    )
    _smoketest_needed = smoketest_enabled and "smoketest" not in resolved_dims
    _merge_grade_needed = (
        _merge_grade_goal_usable and "merge_grade" not in resolved_dims
    )
    if resolved_dims:
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: v%d candidate %s has %d "
            "already-resolved dimension(s) from a prior finalize — "
            "audit_needed=%s smoketest_needed=%s merge_grade_needed=%s",
            version.version_number,
            candidate_sha[:8],
            len(resolved_dims),
            _audit_needed,
            _smoketest_needed,
            _merge_grade_needed,
        )
    try:
        if _audit_needed:
            audit_task = await _spawn_release_audit_task(
                session,
                project_id,
                version_id=version.id,
                base_branch=promotion_target,
                diff_range=diff_range,
                candidate_sha=candidate_sha,
                audit_base_sha=_incremental_audit_base_sha(prior_qual, candidate_sha),
                version_goal=version.goal,
            )
            audit_task_id = str(audit_task.id)
        if _merge_grade_needed:
            merged_pr_numbers = await _merged_pr_numbers_for_version(session, version)
            merge_grade_task = await _spawn_release_merge_grade_task(
                session,
                project_id,
                version_id=version.id,
                base_branch=promotion_target,
                diff_range=diff_range,
                candidate_sha=candidate_sha,
                version_goal=version_goal,
                merged_pr_numbers=merged_pr_numbers,
            )
            merge_grade_task_id = str(merge_grade_task.id)
        if _smoketest_needed:
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

    audit_challenge_spawn_pending = _audit_needed

    # 6. Stamp the pending qualification record.  smoketest_task_id is None
    #    when the smoketest subsystem is disabled — maybe_finalize_qualification
    #    and the reaper sweep both treat a None smoketest id as "no smoketest
    #    child to wait on".  prior_infra_streak carries the infra-failure streak
    #    from the previous infra_failure finalization (0 when the previous
    #    finalization was a quality verdict / dry-run / non-infra outcome).
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "candidate_diff_hash": candidate_diff_hash,
        "audit_base_sha": _incremental_audit_base_sha(prior_qual, candidate_sha),
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
        "merge_grade_task_id": merge_grade_task_id,
        "prior_infra_streak": prior_infra_streak,
        "previous_infra_failure_signature": prior_infra_failure_signature,
        "previous_infra_failure_candidate_sha": prior_infra_failure_candidate_sha,
        "infra_attempt_history": infra_attempt_history,
        "prior_superseded": prior_superseded,
        "keyless_requalification_consumed_at": prior_qual.get(
            "keyless_requalification_consumed_at"
        ),
        # Dimensions already resolved (passed/failed/blocked) for this
        # candidate_sha from a prior finalize — carried forward from the
        # per-dimension ledger so _finalize_qualification can compose from
        # them without re-running their gates.
        "resolved_dimensions": resolved_dims if resolved_dims else None,
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
        "merge_grade_task_id": merge_grade_task_id,
    }


# ---------------------------------------------------------------------------
# _finalize_qualification — phase 2: collect + decide.
# The child-task spawn / artifact-read / infra-failure seam lives in
# app.release_qualifier_audit and is re-exported above for back-compat.
# ---------------------------------------------------------------------------

# Maximum number of PR heads to check in the CI resolution fallback path.
# When a commit is associated with more than this many PRs, we cap the sweep
# to avoid exhausting API quota and slowing the CI polling cycle.
_PR_HEAD_SWEEP_MAX = 8

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
    "criterion_coverage_incomplete": (
        "one or more version acceptance criteria remain uncovered; stuck "
        "criteria (no direction mapping, or all mapped directions retired) need "
        "direction remapping or operator attestation to clear"
    ),
    "merge_grade_misses": (
        "the merged diffs 'misses' the version goal per the blind merge-grade "
        "and the merge_grade_block_threshold is armed"
    ),
    "uncorroborated_attestation": (
        "all acceptance criteria rely solely on operator attestation — zero "
        "machine-verified evidence; promotion is blocked until at least one "
        "criterion carries machine-verified evidence (named_test, "
        "reachability, or verified task_merge)"
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
        try:
            verdict, detail = await check_qualification_migration_identity(
                token,
                owner,
                repo,
                candidate_sha,
                promotion_target,
                project_id=str(version.project_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RELEASE_QUALIFIER] finalize migration-identity gate failed for "
                "v%d (attempt 1, retrying): %s",
                version.version_number,
                exc,
            )
            verdict, detail = "defer", f"migration-identity gate error: {exc}"
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-identity gate for v%d "
            "candidate %s vs %s: %s (%s)",
            version.version_number,
            candidate_sha[:8],
            promotion_target,
            verdict,
            detail or "no divergence",
        )
        if verdict != "defer":
            return verdict, detail
        # First attempt deferred — retry once with freshly-resolved credentials.
        # _resolve_github_with_session returns a fresh token if the cache was
        # invalidated by a 401 during the first attempt.
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-identity gate deferred for v%d "
            "(attempt 1); retrying with fresh credentials",
            version.version_number,
        )
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return verdict, detail
        token, owner, repo = resolved
        verdict, detail = await check_qualification_migration_identity(
            token,
            owner,
            repo,
            candidate_sha,
            promotion_target,
            project_id=str(version.project_id),
        )
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-identity gate retry for v%d "
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
        try:
            verdict, detail = await run_migration_apply_gate(
                prod_engine=prod_engine,
                gh_token=token,
                owner=owner,
                repo=repo,
                candidate_ref=candidate_sha,
                project_id=str(version.project_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RELEASE_QUALIFIER] finalize migration-apply gate failed for "
                "v%d (attempt 1, retrying): %s",
                version.version_number,
                exc,
            )
            verdict, detail = "defer", f"migration-apply gate error: {exc}"
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-apply gate for v%d "
            "candidate %s: %s (%s)",
            version.version_number,
            candidate_sha[:8],
            verdict,
            detail or "applies cleanly",
        )
        if verdict != "defer":
            return verdict, detail
        # First attempt deferred — retry once with freshly-resolved credentials.
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-apply gate deferred for v%d "
            "(attempt 1); retrying with fresh credentials",
            version.version_number,
        )
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return verdict, detail
        token, owner, repo = resolved
        verdict, detail = await run_migration_apply_gate(
            prod_engine=prod_engine,
            gh_token=token,
            owner=owner,
            repo=repo,
            candidate_ref=candidate_sha,
            project_id=str(version.project_id),
        )
        logger.info(
            "[RELEASE_QUALIFIER] finalize migration-apply gate retry for v%d "
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
    """Count write-task merges in this version's window with explicit verified acceptance.

    Queries all COMPLETED tasks with the landed-signal predicate that completed
    at or after ``version.opened_at`` (the version window lower bound). The
    denominator and numerator are both restricted to WRITE_TASK_TYPES so the
    fraction reflects the population that can actually declare acceptance evidence
    (non-write tasks such as research/review/plan/audit can never enter the
    ``proven`` / ``satisfied_unproven`` / ``inferred_test_cochange`` buckets). A
    verified merge is one whose acceptance context lands in any of those three
    buckets — including undeclared merges that co-changed a test and a
    production file under genuine CI (``inferred_test_cochange``). A None fraction means no
    write-task merges landed in the window — distinct from a genuine 0-coverage
    failure.

    Returns:
      {
        "acceptance_window_total": int,           # write-task merges in the window
        "acceptance_window_verified": int,         # write-task merges with proven/satisfied acceptance
        "acceptance_window_fraction": float|None,  # None when write-task total == 0
        "acceptance_window_write_total": int,      # same as acceptance_window_total (explicit alias)
        "acceptance_window_total_all_merges": int, # all landed merges regardless of type (observability)
        "acceptance_window_buckets": dict,         # per-bucket counts over the write-task window (sums to write_total)
        "acceptance_window_undeclared_reasons": dict, # split of the undeclared bucket by reason (sums to buckets["undeclared"])
        "acceptance_window_inferred_test_cochange": int,  # verified merges credited via inferred_test_cochange
      }

    The two nested breakdowns are purely additive: ``acceptance_window_buckets``
    keys the 7 canonical ``ACCEPTANCE_AGGREGATE_BUCKETS`` and makes the fraction
    auditable (verified == proven + satisfied_unproven + inferred_test_cochange),
    while ``acceptance_window_undeclared_reasons`` explains why the remaining
    undeclared merges did not infer coverage. No existing key changes type or value.
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

    total_all_merges = len(rows)
    rows_write = [
        task
        for task in rows
        if (task.first_payload or {}).get("type") in WRITE_TASK_TYPES
    ]
    total = len(rows_write)
    # Single classification pass: tally every write-task merge into its acceptance
    # bucket, and for each undeclared merge additionally record WHY it stayed
    # undeclared. Both breakdowns are additive observability — the scalar
    # ``verified`` is derived from the three verified buckets so its value and the
    # invariant contract are byte-for-byte unchanged from the prior ``sum(...)``.
    buckets: dict[str, int] = dict.fromkeys(ACCEPTANCE_AGGREGATE_BUCKETS, 0)
    undeclared_reasons: dict[str, int] = dict.fromkeys(_UNDECLARED_REASON_KEYS, 0)
    for task in rows_write:
        ctx = task.context or {}
        bucket = classify_merged_acceptance_context(ctx)
        buckets[bucket] += 1
        if bucket == "undeclared":
            undeclared_reasons[classify_undeclared_reason(ctx)] += 1
    verified = (
        buckets["proven"]
        + buckets["satisfied_unproven"]
        + buckets["inferred_test_cochange"]
    )
    inferred_test_cochange = buckets["inferred_test_cochange"]
    # Per-task audit trail for inferred_test_cochange merges, capped so the
    # evidence detail stays bounded in storage regardless of window size.
    _INFERRED_TASK_ID_CAP = 100
    inferred_task_ids: list[str] = [
        str(task.id)
        for task in rows_write
        if classify_merged_acceptance_context(task.context or {}) == "inferred_test_cochange"
    ][:_INFERRED_TASK_ID_CAP]
    fraction = (verified / total) if total > 0 else None
    return {
        "acceptance_window_total": total,
        "acceptance_window_verified": verified,
        "acceptance_window_fraction": fraction,
        "acceptance_window_write_total": total,
        "acceptance_window_total_all_merges": total_all_merges,
        # Additive nested breakdowns (None-safe on legacy rows that lack them):
        # per-bucket counts over the write-task window, and the undeclared reason
        # split. Sum(buckets) == total; sum(undeclared_reasons) == buckets["undeclared"].
        "acceptance_window_buckets": buckets,
        "acceptance_window_undeclared_reasons": undeclared_reasons,
        # Additive audit breakdown: how many verified merges were credited via the
        # inferred test-cochange path (undeclared merge, CI-verified, test+prod
        # co-change) rather than an explicit proven/satisfied declaration. Consumed
        # by acceptance_criteria.record_acceptance_window_coverage_evidence for the
        # evidence detail; no score impact. Equals buckets["inferred_test_cochange"].
        "acceptance_window_inferred_test_cochange": inferred_test_cochange,
        # Per-merge audit trail: task IDs of the inferred_test_cochange merges so
        # operators can trace which specific merges were counted without SQL.
        "acceptance_window_inferred_test_cochange_task_ids": inferred_task_ids,
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
                token, owner, repo, promotion_target, live=True
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


async def _merged_pr_numbers_for_version(
    session: AsyncSession,
    version_obj: release_version.ReleaseVersion,
) -> list[int]:
    """Return the PR numbers merged into this version's acceptance window.

    Reuses the SAME landed-merge predicate the blind merge-grade path uses
    (``landed_signal_predicate`` + the version's opened_at window) so the blind
    merge-grade child grades exactly the diffs that landed in this version.
    Pure DB read — no GitHub I/O; the agent fetches each PR's diff itself.

    Blind by construction: this discovers merged PRs from the task ledger only;
    it never reads ``Project.running_update``.  Returns a sorted, de-duplicated
    list; empty when the version has no opened_at or no landed merges yet.
    """
    if version_obj.opened_at is None:
        return []
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == version_obj.project_id,
                    Task.status == TaskStatus.COMPLETED,
                    landed_signal_predicate(session),
                    Task.updated_at >= version_obj.opened_at,
                )
            )
        )
        .scalars()
        .all()
    )
    pr_numbers: list[int] = []
    for task in rows:
        if release_version._task_completed_at(task) < version_obj.opened_at:
            continue
        pn = (task.first_payload or {}).get("pr_number")
        if isinstance(pn, int) and pn > 0 and pn not in pr_numbers:
            pr_numbers.append(pn)
    return sorted(pr_numbers)


async def _candidate_already_on_main_at_finalize(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> bool:
    """Best-effort: is ``candidate_sha`` already reachable from the promotion target?

    A thin finalize-time wrapper over ``_candidate_already_on_main`` (resolves the
    project's GitHub credentials first) used ONLY to exempt an already-landed
    candidate from the restored hard merge-grade gate — see the exemption in
    ``_finalize_qualification``.

    Fail-CLOSED to ``False`` (keep the gate armed) on any credential / GitHub /
    single-branch-repo condition: a transient failure must never wave a failing
    merge-grade through, and a genuinely-landed candidate is re-detected on the
    next finalize / sweep retry once GitHub is reachable.
    """
    if not candidate_sha:
        return False
    try:
        project = await session.get(Project, version.project_id)
        if project is None:
            return False
        integration_branch, promotion_target = _lts_branch_names(project)
        if integration_branch == promotion_target:
            # Single-branch repo (LTS inactive): "already on main" is not a
            # meaningful distinction — nothing to exempt.
            return False
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return False
        token, owner, repo = resolved
        return await _candidate_already_on_main(
            token, owner, repo, candidate_sha, version, project=project
        )
    except Exception as exc:  # noqa: BLE001 — a check failure keeps the gate armed
        logger.warning(
            "[RELEASE_QUALIFIER] merge-grade already-on-main exemption check "
            "failed for v%d candidate %s (%s) — keeping the gate armed",
            version.version_number,
            (candidate_sha or "")[:8],
            exc,
        )
        return False


async def _count_project_actionable_open_completion_debt(
    session: AsyncSession, project_id: Any
) -> int:
    """Count open completion-debt rows owned by the qualified project only."""
    if project_id is None:
        return 0
    from app.models import CompletionDebtEntry

    stmt = (
        select(sa_func.count())
        .select_from(CompletionDebtEntry)
        .where(
            CompletionDebtEntry.project_id == project_id,
            CompletionDebtEntry.status == "open",
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _upsert_dimension_result(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
    dimension: str,
    *,
    status: str,
    sub_score: float | None = None,
    blocker: str | None = None,
    detail: Any | None = None,
    producing_task_id: Any | None = None,
    audited_surface_hash: str | None = None,
) -> None:
    """Best-effort UPSERT of ONE per-(candidate_sha, dimension) qualification fact.

    Phase 1 dual-write (design doc "From Batch Attempts to a Per-Dimension Fact
    Ledger", section 3).  SELECT-then-INSERT/UPDATE on the unique
    ``(release_version_id, candidate_sha, dimension)`` key so a later re-check of
    a single gate re-records ONLY its own fact (latest write wins) without
    touching the other dimensions that already passed for the same candidate
    tree.  SELECT-then-write (not a dialect-specific ON CONFLICT upsert) keeps
    the helper portable across the SQLite test gate and the Postgres runtime.

    CRITICAL: recording must NEVER break or roll back a qualification/promotion
    (mirrors ``_record_qualification_verdict``).  The read+write runs inside a
    SAVEPOINT (``begin_nested``) wrapped in try/except; any failure — a malformed
    row, or (until the sibling migration lands) a missing table — is rolled back
    to the savepoint leaving the OUTER transaction intact, logged, and swallowed.
    Purely additive: no existing write or decision is affected.  The caller is
    expected to have flushed its own staged mutations into the outer transaction
    first, so this savepoint isolates only the ledger write.
    """
    # A per-tree fact is only meaningful with a candidate SHA (the upsert key
    # relies on it).  Skip silently otherwise — additive, best-effort.
    if not candidate_sha:
        return

    _producing_uuid: uuid.UUID | None = None
    if producing_task_id:
        try:
            _producing_uuid = uuid.UUID(str(producing_task_id))
        except (ValueError, TypeError, AttributeError):
            _producing_uuid = None

    _sub_score: float | None = None
    if isinstance(sub_score, (int, float)) and not isinstance(sub_score, bool):
        _sub_score = float(sub_score)

    try:
        async with session.begin_nested():
            existing = (
                await session.execute(
                    select(QualificationDimensionResult).where(
                        QualificationDimensionResult.release_version_id == version.id,
                        QualificationDimensionResult.candidate_sha == candidate_sha,
                        QualificationDimensionResult.dimension == dimension,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.status = status
                existing.sub_score = _sub_score
                existing.blocker = blocker
                existing.audited_surface_hash = audited_surface_hash
                existing.detail = detail
                existing.producing_task_id = _producing_uuid
                existing.computed_at = _utcnow()
                session.add(existing)
            else:
                session.add(
                    QualificationDimensionResult(
                        project_id=version.project_id,
                        release_version_id=version.id,
                        candidate_sha=candidate_sha,
                        dimension=dimension,
                        status=status,
                        sub_score=_sub_score,
                        blocker=blocker,
                        audited_surface_hash=audited_surface_hash,
                        detail=detail,
                        producing_task_id=_producing_uuid,
                    )
                )
            await session.flush()
    except Exception as exc:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.warning(
            "[RELEASE_QUALIFIER] failed to upsert dimension result '%s' for v%s "
            "(non-fatal — additive ledger only): %s",
            dimension,
            getattr(version, "version_number", "?"),
            exc,
        )


# Per-dimension ledger status vocabulary (design doc "From Batch Attempts to a
# Per-Dimension Fact Ledger", section 3).  A dimension is RESOLVED when its gate
# reached a real pass/fail/blocked outcome; it is TERMINAL (for the same-tree
# promotion invariant) when it additionally reached a legitimate not-applicable
# 'unavailable' (e.g. smoketest disabled, merge-grade exempt).  'pending' and a
# missing row are neither.  An infra failure records 'unavailable' — RESOLVED is
# deliberately narrower than TERMINAL so a re-finalize RE-RUNS an infra-held
# dimension while REUSING the ones that already truly passed/failed/blocked.
_LEDGER_RESOLVED_STATUSES: frozenset[str] = frozenset({"passed", "failed", "blocked"})
_LEDGER_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"passed", "failed", "blocked", "unavailable"}
)

# Gates that must every one have a terminal fact at the SAME candidate_sha before
# a promotion may compose from the ledger — the informational dimensions
# (challenge / acceptance_window / debt_pressure) never gate promotion and are
# excluded.  smoketest is required only when the run had it enabled (audit H3).
_PROMOTION_REQUIRED_DIMENSIONS: tuple[str, ...] = (
    "ci",
    "audit",
    "test_erosion",
    "migration_identity",
    "migration_apply",
    "merge_grade",
)


async def _load_prior_dimension_results(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> dict[str, QualificationDimensionResult]:
    """Return the per-dimension ledger rows already recorded for this candidate.

    Read side of the per-dimension fact ledger (Phase 1).  Keyed by dimension,
    scoped to ``(release_version_id, candidate_sha)`` so facts from a DIFFERENT
    candidate tree are never mixed in — preserving the batch model's "everything
    scored the same tree" guarantee.  Best-effort: any read failure (including a
    missing table before the sibling migration lands) degrades to an empty map so
    finalize continues exactly as before.
    """
    if not candidate_sha:
        return {}
    try:
        rows = (
            (
                await session.execute(
                    select(QualificationDimensionResult).where(
                        QualificationDimensionResult.release_version_id == version.id,
                        QualificationDimensionResult.candidate_sha == candidate_sha,
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001 — read is best-effort, never fatal
        logger.warning(
            "[RELEASE_QUALIFIER] failed to load prior dimension ledger for v%s "
            "(non-fatal — composing from live gates): %s",
            getattr(version, "version_number", "?"),
            exc,
        )
        return {}
    return {r.dimension: r for r in rows}


async def _record_infra_dimension_unavailable(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
    dimension: str,
    *,
    infra_error: str,
    signature: str | None = None,
) -> int:
    """Mark ONE dimension 'unavailable' in the ledger on an infrastructure failure.

    Per-dimension failure isolation (Phase 1): an infra failure in a single gate
    records ONLY that ``(candidate_sha, dimension)`` fact as unavailable — the
    other dimensions that already resolved for the same candidate tree keep their
    rows untouched (the upsert only ever writes this one dimension's row).  The
    consecutive-infra-error streak is tracked PER-DIMENSION in the row's ``detail``
    JSONB, so one gate's flakiness cannot burn another gate's retry budget.

    Best-effort — mirrors ``_upsert_dimension_result`` and never breaks finalize.
    Returns the new per-dimension streak (0 when the write is skipped, e.g. no
    candidate SHA).
    """
    if not candidate_sha:
        return 0
    prior_streak = 0
    try:
        existing = (
            await session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id,
                    QualificationDimensionResult.candidate_sha == candidate_sha,
                    QualificationDimensionResult.dimension == dimension,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and isinstance(existing.detail, dict):
            prior = existing.detail.get("consecutive_infra_errors")
            if isinstance(prior, int) and not isinstance(prior, bool):
                prior_streak = prior
    except Exception as exc:  # noqa: BLE001 — best-effort read of the prior streak
        logger.warning(
            "[RELEASE_QUALIFIER] failed to read prior infra streak for dimension "
            "'%s' on v%s (non-fatal): %s",
            dimension,
            getattr(version, "version_number", "?"),
            exc,
        )
        prior_streak = 0
    new_streak = prior_streak + 1
    await _upsert_dimension_result(
        session,
        version,
        candidate_sha,
        dimension,
        status="unavailable",
        blocker=None,
        detail={
            "infra_error": infra_error,
            "infra_failure_signature": signature,
            "consecutive_infra_errors": new_streak,
        },
    )
    return new_streak


async def _promotion_dimension_ledger_complete(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
    *,
    smoketest_enabled: bool = True,
) -> tuple[bool, list[str]]:
    """Is there a full set of terminal dimension facts at ONE candidate_sha?

    The batch model's invariant — every gate scored the SAME candidate tree —
    carried onto the per-dimension ledger: a promotion may only compose from the
    ledger when every required gating dimension has a TERMINAL row (not pending,
    not missing) at ``candidate_sha``.  A required dimension whose only fact is at
    a DIFFERENT candidate_sha does not count (``_load_prior_dimension_results`` is
    already sha-scoped).  Returns ``(complete, missing_dimensions)``.
    """
    required = list(_PROMOTION_REQUIRED_DIMENSIONS)
    if smoketest_enabled:
        required.append("smoketest")
    prior = await _load_prior_dimension_results(session, version, candidate_sha)
    missing = [
        dim
        for dim in required
        if dim not in prior or prior[dim].status not in _LEDGER_TERMINAL_STATUSES
    ]
    return (not missing, missing)


async def _dual_write_dimension_results(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    base_record: dict[str, Any],
    composite: dict[str, Any],
) -> None:
    """Dual-write every resolved qualification dimension as its own ledger row.

    Phase 1 of the per-dimension fact ledger (design doc "From Batch Attempts to
    a Per-Dimension Fact Ledger").  Called from ``_finalize_qualification`` once
    the composite ``base_record`` is fully assembled — i.e. after EVERY gate /
    dimension has resolved (CI, audit, test-erosion, smoketest, challenge, both
    migration gates, merge-grade, acceptance-window, debt-pressure,
    capability-proof).  Deriving
    the per-dimension facts from the already-assembled ``base_record`` +
    ``composite`` keeps the write in ONE additive place that cannot alter control
    flow, rather than threading a mutation through the ~1300-line finalize body
    (all three terminal paths — not_promoted / dry_run / promote — flow through
    this single point).

    ADDITIVE to the composite / ``last_qualification`` / ``QualificationVerdict``
    writes — it changes no decision.  READ-SIDE COMPOSITION (Phase 1): a
    dimension that already reached a RESOLVED (passed/failed/blocked) fact at this
    same ``candidate_sha`` in an earlier finalize is REUSED verbatim — its stored
    row is left untouched rather than overwritten with the freshly recomputed
    value.  So a finalize re-run after a transient infra failure re-records ONLY
    the dimensions that were still unavailable, and the gates that already passed
    for this candidate tree are neither re-run into the ledger nor discarded.
    Each written row goes through best-effort ``_upsert_dimension_result``.
    """
    candidate_sha = base_record.get("candidate_sha")
    if not candidate_sha:
        # No candidate tree → no meaningful per-tree fact; skip the whole block.
        return

    # Read side: dimensions already RESOLVED for this candidate tree are reused
    # verbatim (skip re-recording), isolating a re-check to the still-unresolved
    # gates.  Best-effort — an empty map degrades to "write every dimension".
    prior_resolved = {
        dim: row
        for dim, row in (
            await _load_prior_dimension_results(session, version, candidate_sha)
        ).items()
        if row.status in _LEDGER_RESOLVED_STATUSES
    }

    sub_scores = base_record.get("sub_scores") or {}
    blocker = composite.get("blocker")
    ci_green = bool(base_record.get("ci_green"))
    smoketest_enabled = bool(base_record.get("smoketest_enabled", True))

    def _score(key: str) -> float | None:
        v = sub_scores.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return None

    def _own_blocker(codes: set[str]) -> str | None:
        return blocker if blocker in codes else None

    def _mig_status(verdict: Any) -> str:
        if verdict == "block":
            return "blocked"
        if verdict == "defer":
            return "unavailable"
        if verdict in ("ok", "pass"):
            return "passed"
        return "unavailable"

    audit_score = _score("audit")
    if blocker == "audit_missing":
        audit_status = "blocked"
    elif audit_score is not None and audit_score < 1.0:
        audit_status = "failed"
    else:
        audit_status = "passed"

    if not smoketest_enabled:
        smoketest_status = "unavailable"
    elif blocker == "smoketest_startup_failed":
        smoketest_status = "blocked"
    else:
        smoketest_status = "passed"

    mg_status_raw = base_record.get("merge_grade_status")
    if mg_status_raw == "pass":
        merge_grade_status = "passed"
    elif mg_status_raw in ("fail", "misses"):
        merge_grade_status = "failed"
    else:
        merge_grade_status = "unavailable"
    mg_coverage = base_record.get("merge_grade_coverage")

    challenge_verdicts = base_record.get("challenge_verdicts")
    acc_fraction = sub_scores.get("acceptance_window_fraction")
    debt_score = _score("debt_pressure")

    # Capability-proof gate ledger status: OFF → unavailable (gate not active),
    # OBSERVE → passed (watching), ENFORCE → same as the composite blocker.
    _cp_detail = base_record.get("capability_proof_detail") or {}
    _cp_mode = _cp_detail.get("mode", "off")
    if _cp_mode == "off":
        _cp_status = "unavailable"
    elif _cp_mode == "observe":
        _cp_status = "passed"
    else:  # enforce
        _cp_status = "blocked" if blocker == "capability_proof_gate_blocked" else "passed"

    specs: list[dict[str, Any]] = [
        {
            "dimension": "ci",
            "status": "passed" if ci_green else "blocked",
            "sub_score": None,
            "blocker": _own_blocker({"ci_not_green", "ci_unresolved"}),
            "detail": base_record.get("ci_resolution"),
            "producing_task_id": None,
        },
        {
            "dimension": "audit",
            "status": audit_status,
            "sub_score": audit_score,
            "blocker": _own_blocker({"audit_missing"}),
            "audited_surface_hash": base_record.get("candidate_diff_hash"),
            "detail": {
                "audit_missing_reason": base_record.get("audit_missing_reason"),
                "audit_findings_total": base_record.get("audit_findings_total"),
                "excluded_audit_findings_total": base_record.get("excluded_audit_findings_total"),
            },
            "producing_task_id": base_record.get("audit_task_id"),
        },
        {
            "dimension": "test_erosion",
            "status": "blocked" if blocker == "test_file_deleted" else "passed",
            "sub_score": _score("test_erosion"),
            "blocker": _own_blocker({"test_file_deleted"}),
            "detail": base_record.get("test_erosion_detail"),
            "producing_task_id": base_record.get("audit_task_id"),
        },
        {
            "dimension": "smoketest",
            "status": smoketest_status,
            "sub_score": _score("smoketest") if smoketest_enabled else None,
            "blocker": _own_blocker({"smoketest_startup_failed"}),
            "detail": base_record.get("smoketest_detail"),
            "producing_task_id": base_record.get("smoketest_task_id"),
        },
        {
            "dimension": "challenge",
            "status": "passed" if challenge_verdicts else "unavailable",
            "sub_score": None,
            "blocker": None,
            "detail": {
                "verdicts": challenge_verdicts,
                "challenger_refuted_audit_findings": base_record.get("challenger_refuted_audit_findings"),
            },
            "producing_task_id": base_record.get("audit_challenge_task_id"),
        },
        {
            "dimension": "migration_identity",
            "status": _mig_status(base_record.get("migration_safety")),
            "sub_score": None,
            "blocker": _own_blocker({"migration_identity_collision"}),
            "detail": {
                "verdict": base_record.get("migration_safety"),
                "detail": base_record.get("migration_safety_detail"),
            },
            "producing_task_id": None,
        },
        {
            "dimension": "migration_apply",
            "status": _mig_status(base_record.get("migration_apply_safety")),
            "sub_score": None,
            "blocker": _own_blocker({"migration_apply_drift"}),
            "detail": {
                "verdict": base_record.get("migration_apply_safety"),
                "detail": base_record.get("migration_apply_safety_detail"),
            },
            "producing_task_id": None,
        },
        {
            "dimension": "merge_grade",
            "status": merge_grade_status,
            "sub_score": (
                float(mg_coverage)
                if isinstance(mg_coverage, (int, float)) and not isinstance(mg_coverage, bool)
                else None
            ),
            "blocker": _own_blocker({"merge_grade_misses"}),
            "detail": base_record.get("merge_grade"),
            "producing_task_id": base_record.get("merge_grade_task_id"),
        },
        {
            "dimension": "acceptance_window",
            "status": "passed" if acc_fraction is not None else "unavailable",
            "sub_score": (
                float(acc_fraction)
                if isinstance(acc_fraction, (int, float)) and not isinstance(acc_fraction, bool)
                else None
            ),
            "blocker": None,
            "detail": {
                k: v for k, v in sub_scores.items() if str(k).startswith("acceptance_window")
            },
            "producing_task_id": None,
        },
        {
            "dimension": "debt_pressure",
            "status": "passed" if debt_score is not None else "unavailable",
            "sub_score": debt_score,
            "blocker": None,
            "detail": base_record.get("completion_debt"),
            "producing_task_id": None,
        },
        {
            "dimension": "capability_proof",
            "status": _cp_status,
            "sub_score": None,
            "blocker": _own_blocker({"capability_proof_gate_blocked"}),
            "detail": _cp_detail,
            "producing_task_id": None,
        },
    ]

    try:
        await session.flush()
    except Exception as exc:  # noqa: BLE001 — best-effort; never break finalize
        logger.warning(
            "[RELEASE_QUALIFIER] v%s dimension-ledger pre-flush failed (%s); "
            "skipping additive dual-write",
            getattr(version, "version_number", "?"),
            exc,
        )
        return

    for spec in specs:
        if spec["dimension"] in prior_resolved:
            # Already resolved (passed/failed/blocked) for this candidate tree in
            # an earlier finalize — reuse the stored fact, do NOT re-record or
            # overwrite it.  This is the read-side composition: a re-check after a
            # transient failure re-runs ONLY the still-unavailable dimensions and
            # never discards the ones that already passed for the same tree.
            continue
        await _upsert_dimension_result(
            session,
            version,
            candidate_sha,
            spec["dimension"],
            status=spec["status"],
            sub_score=spec["sub_score"],
            blocker=spec["blocker"],
            detail=spec["detail"],
            producing_task_id=spec["producing_task_id"],
            audited_surface_hash=spec.get("audited_surface_hash"),
        )


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
    merge_grade_task_id = qual.get("merge_grade_task_id")
    # Audit H3: whether the qualifying run had the smoketest subsystem enabled.
    # Read from the recorded qualification (NOT live runtime_config) — the
    # config could have flipped between spawn and finalize, and the composite
    # must match the children that actually ran.  Legacy records that predate
    # the flag default to True (the old always-spawn behaviour).
    smoketest_enabled = bool(qual.get("smoketest_enabled", True))
    # Dimensions already resolved (passed/failed/blocked) for this candidate_sha
    # from a prior finalize, carried forward by run_qualification from the
    # per-dimension ledger.  When a dimension is present here its gate evaluation
    # is composed from the ledger rather than re-run, and its child task may not
    # have been spawned (the spawn coordinator skipped it).
    resolved_dims: dict[str, dict[str, Any]] = qual.get("resolved_dimensions") or {}

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

    # Eager-write CI dimension so this fact survives a later gate's infra failure
    # that returns before _dual_write_dimension_results (line 5065).
    if candidate_sha:
        await _upsert_dimension_result(
            session, version, candidate_sha, "ci",
            status="passed" if ci_green else "blocked",
            detail=ci_resolution if isinstance(ci_resolution, dict) else None,
        )

    _policy_project = await session.get(Project, version.project_id)
    policy = _load_policy(_policy_project)

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
                        version_goal=version.goal,
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
                        version_goal=version.goal,
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

    # --- Blind merge-grade child: required-child terminal-status gate -------
    # The merge-grade child (spawned only when the version goal is non-empty) is
    # a REQUIRED child: a FAILED/CANCELLED/BLOCKED merge-grade means the grade
    # never produced a usable verdict, so the gate cannot be skipped — route to
    # _handle_infra_failure (HOLD, no verdict recorded) exactly like the audit /
    # smoketest legs.
    _merge_grade_task_obj: Task | None = None
    if merge_grade_task_id:
        try:
            _merge_grade_task_obj = await session.get(
                Task, uuid.UUID(str(merge_grade_task_id))
            )
            if (
                _merge_grade_task_obj is not None
                and _merge_grade_task_obj.status in _INFRA_FAILURE_TERMINAL_STATUSES
            ):
                return await _handle_infra_failure(
                    session,
                    version,
                    qual,
                    _enrich_infra_error(
                        f"merge_grade_task_{_merge_grade_task_obj.status.lower()}",
                        _merge_grade_task_obj,
                    ),
                )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid merge_grade_task_id %s on version "
                "v%d (skipping infra-failure check)",
                merge_grade_task_id,
                version.version_number,
            )

    # --- Read the release_audit report -------------------------------------
    # When both audit and test_erosion dimensions are already resolved from a
    # prior finalize at this candidate_sha (carried forward from the ledger),
    # compose from the durable facts rather than reading artifacts — the audit
    # child was not re-spawned for this re-drive.
    _audit_and_erosion_resolved = (
        "audit" in resolved_dims and "test_erosion" in resolved_dims
    )
    if _audit_and_erosion_resolved:
        audit_report = None
        audit_score = resolved_dims["audit"].get("sub_score")
        test_erosion_score = resolved_dims["test_erosion"].get("sub_score")
        if test_erosion_score is None:
            test_erosion_score = 1.0
        audit_findings: list[dict[str, Any]] = []
        excluded_audit_findings: list[dict[str, Any]] = []
        test_erosion_detail: dict[str, Any] = {}
        audit_missing_reason: str | None = None
    else:
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

    # Persist audit and test_erosion to the per-dimension ledger immediately
    # since both have now been resolved (from the ledger or live).  An infra
    # failure in a later gate must not discard them — otherwise the re-drive
    # re-spawns the audit child (~15 min) even though its results are intact.
    # _upsert_dimension_result handles a None candidate_sha with a silent no-op.
    if audit_score is None:
        _audit_status = "blocked"
    elif audit_score < 1.0:
        _audit_status = "failed"
    else:
        _audit_status = "passed"
    await _upsert_dimension_result(
        session, version, candidate_sha, "audit",
        status=_audit_status,
        sub_score=audit_score,
        producing_task_id=audit_task_id,
    )
    _te_status = "blocked" if test_erosion_score is None else "passed"
    await _upsert_dimension_result(
        session, version, candidate_sha, "test_erosion",
        status=_te_status,
        sub_score=test_erosion_score,
        producing_task_id=audit_task_id,
    )

    # --- Read the smoketest result -----------------------------------------
    # Audit H3: only when the smoketest subsystem was enabled — a disabled run
    # spawned no qualifier-smoketest child, so there is no SMOKETEST_RESULT to
    # read and smoketest_score stays None (compute_composite drops the
    # dimension rather than hard-blocking on it).  When the smoketest dimension
    # is already resolved from a prior finalize at this candidate_sha, compose
    # from the ledger (the smoketest child was not re-spawned).
    if "smoketest" in resolved_dims:
        smoketest_result = None
        _smoke_score = resolved_dims["smoketest"].get("sub_score")
        smoketest_score = _smoke_score if _smoke_score is not None else 1.0
        smoketest_detail = resolved_dims["smoketest"].get("detail") or {}
    else:
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

    # Persist smoketest to the per-dimension ledger immediately so it
    # survives a later-gate infra failure before _dual_write_dimension_results.
    # _upsert_dimension_result handles a None candidate_sha with a silent no-op.
    if not smoketest_enabled:
        _smoke_status = "unavailable"
    elif smoketest_score is None:
        _smoke_status = "blocked"
    else:
        _smoke_status = "passed"
    await _upsert_dimension_result(
        session, version, candidate_sha, "smoketest",
        status=_smoke_status,
        sub_score=smoketest_score,
        producing_task_id=smoketest_task_id,
    )
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
                version_goal=version.goal,
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
                                        version_goal=version.goal,
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
                    version_goal=version.goal,
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
                        version_goal=version.goal,
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

    # Score using the reconciled report (refuted findings removed when challenge
    # ran).  When audit+test_erosion are already resolved from a prior finalize
    # at this candidate_sha, skip re-scoring — the carried-forward scores and
    # empty findings lists from the ledger are authoritative, and re-scoring
    # against a None report would overwrite them with a default 1.0.
    if not _audit_and_erosion_resolved:
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
    else:
        # audit+test_erosion resolved from ledger — reconciliation fields are
        # not meaningful (no challenge ran, no audit report exists).
        _verified_findings_meta = {
            "audit_findings_total": 0,
            "audit_findings_truncated": False,
        }
        _excluded_findings_meta = {
            "excluded_audit_findings_total": 0,
            "excluded_audit_findings_truncated": False,
        }
        audit_reconciliation: dict[str, Any] = {}
        challenger_refuted_audit_findings = []
        _challenge_binding_record = []
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

    # --- Blind merge-grade artifact validation (dir d-c0fe439f) -------------
    # An uninvested release_merge_grade agent graded the merged diffs against the
    # frozen version.goal (blind to running_update).  Since ae0664174 the grade
    # is a HARD promotion blocker when armed: a "misses" verdict forces
    # merge_grade_misses below (blocker="merge_grade_misses") whenever the
    # operator has raised merge_grade_block_threshold above 0.0 — it is no longer
    # observability-only.  The canonical version acceptance-criteria coverage
    # gate is a separate, additional hard gate.  Semantics:
    #   * version.goal empty/unset → the gate is a no-op PASS (nothing to grade
    #     against); no child was spawned, merge_grade_task_id is None.
    #   * goal non-empty but the required grade is MISSING / UNPARSEABLE → HOLD
    #     (infra_failure): required artifacts cannot be skipped or waved through.
    #   * merge_grade dimension already resolved from a prior finalize at this
    #     candidate_sha → compose from the ledger (the child was not re-spawned).
    if "merge_grade" in resolved_dims:
        _mg_resolved = resolved_dims["merge_grade"]
        _mg_status = _mg_resolved["status"]
        merge_grade_cert = {
            "status": (
                "pass" if _mg_status == "passed" else (
                    "fail" if _mg_status == "failed" else _mg_status
                )
            ),
            "covers_goal": None,
            "coverage": _mg_resolved.get("sub_score"),
            "biggest_gap": None,
            "blocked": _mg_status == "failed",
            "detail": None,
        }
    else:
        merge_grade_cert: dict[str, Any] = {
            "status": "not_applicable",
            "covers_goal": None,
            "coverage": None,
            "biggest_gap": None,
            "blocked": False,
            "detail": None,
        }
    if merge_grade_task_id and "merge_grade" not in resolved_dims:
        merge_grade_artifact: dict[str, Any] | None = None
        try:
            merge_grade_artifact = await _latest_artifact_logs(
                session,
                uuid.UUID(str(merge_grade_task_id)),
                ArtifactType.RELEASE_MERGE_GRADE,
            )
        except (ValueError, TypeError):
            logger.warning(
                "[RELEASE_QUALIFIER] invalid merge_grade_task_id %s on version v%d",
                merge_grade_task_id,
                version.version_number,
            )
        merge_grade_cert = evaluate_merge_grade(version.goal, merge_grade_artifact)
        if qual.get("prior_superseded") and merge_grade_cert["status"] == "fail":
            merge_grade_cert = {
                **merge_grade_cert,
                "status": "superseded_exempt",
                "blocked": False,
            }
        # A spawned merge-grade child MUST produce a usable goal-coverage verdict
        # to finalize.  A COMPLETED child with an absent artifact, or a present
        # but malformed artifact, is an infrastructure failure (the agent ran but
        # emitted no usable output) — route to a HOLD so the gate cannot be
        # skipped or waved through, exactly like the audit/smoketest legs.
        if merge_grade_cert["status"] == "unparseable":
            label = (
                "merge_grade_report_missing"
                if merge_grade_artifact is None
                else "merge_grade_report_unparseable"
            )
            return await _handle_infra_failure(session, version, qual, label)

    ci_state = ci_resolution.get("state", _CI_RED)
    ci_required = True
    if _pre_gates is not None and "ci_required" in _pre_gates:
        ci_required = bool(_pre_gates["ci_required"])
    elif ci_state == _CI_NO_CHECKS:
        _proj_for_ci_gate = await session.get(Project, version.project_id)
        ci_required = _ci_required_for_finalize(_proj_for_ci_gate, ci_resolution)
    criterion_coverage = await get_version_coverage_summary(session, version.id)
    merge_grade_misses = _merge_grade_misses_armed(
        {
            "merge_grade_status": merge_grade_cert["status"],
            "merge_grade": merge_grade_cert,
        },
        policy,
    )
    # Already-on-main ancestry exemption (finding 0).  The restored hard
    # merge-grade gate (ae0664174) can otherwise wedge an out-of-band
    # force-promoted candidate: once it has landed on the promotion target
    # directly, a "misses" verdict would keep producing not_promoted /
    # merge_grade_misses every finalize, and NO reconciler clears that blocker
    # (the prior_superseded exemption above is only set on the version-cut path,
    # never on a bare force-merge).  A candidate already reachable from the
    # promotion target is past the point where blocking promotion is meaningful,
    # so exempt the grade.  Guarded by ``merge_grade_misses`` so the ancestry
    # GitHub read only runs in the exact wedge case (armed gate + failing grade),
    # never on the common unarmed / passing-grade paths.
    if merge_grade_misses and await _candidate_already_on_main_at_finalize(
        session, version, candidate_sha
    ):
        logger.info(
            "[RELEASE_QUALIFIER] v%d candidate %s failed the blind merge-grade "
            "but is already on the promotion target — exempting "
            "merge_grade_misses to avoid a not_promoted wedge",
            version.version_number,
            (candidate_sha or "")[:8],
        )
        merge_grade_misses = False
        merge_grade_cert = {
            **merge_grade_cert,
            "status": "already_on_main_exempt",
            "blocked": False,
        }
    # Eagerly persist merge_grade to the per-dimension ledger so a later-gate
    # infra failure does not discard the result before _dual_write_dimension_results.
    if merge_grade_task_id or "merge_grade" in resolved_dims:
        _mg_cert_status = merge_grade_cert.get("status", "not_applicable")
        if _mg_cert_status == "pass":
            _mg_ledger_status = "passed"
        elif _mg_cert_status in ("fail", "misses"):
            _mg_ledger_status = "failed"
        elif _mg_cert_status in ("superseded_exempt", "already_on_main_exempt"):
            _mg_ledger_status = "passed"
        else:
            _mg_ledger_status = "unavailable"
        _mg_coverage = merge_grade_cert.get("coverage")
        await _upsert_dimension_result(
            session, version, candidate_sha, "merge_grade",
            status=_mg_ledger_status,
            sub_score=_mg_coverage if isinstance(_mg_coverage, (int, float)) and not isinstance(_mg_coverage, bool) else None,
            producing_task_id=merge_grade_task_id,
        )
    # --- Capability-proof gate dimension (informational in OFF/OBSERVE, gating in ENFORCE) ---
    # Reflects whether the capability proof gate is active and — in ENFORCE
    # mode — whether all working capabilities carry machine-verified proof.
    # The dimension is always recorded in the ledger, but only blocks promotion
    # in ENFORCE mode when unproven capabilities exist.
    _capability_proof_mode = str(
        _runtime_config.get("capability_proof_gate_mode", "off") or "off"
    ).strip().lower()
    capability_proof_blocked = False
    capability_proof_detail: dict[str, Any] = {"mode": _capability_proof_mode}
    if _capability_proof_mode == "enforce":
        from app.capability_registry import working_capability_proof_status as _wcp_status

        _pm = getattr(_policy_project, "problem_model", None)
        if _pm:
            _proof_map = _wcp_status(_pm)
            _unproven = [
                n for n, s in _proof_map.items() if s != "proven"
            ]
            capability_proof_detail["total_working"] = len(_proof_map)
            capability_proof_detail["unproven_count"] = len(_unproven)
            capability_proof_detail["unproven"] = _unproven
            if _unproven:
                capability_proof_blocked = True
        else:
            capability_proof_detail["error"] = "no problem_model"

    # --- Completion-debt snapshot (additive, informational, best-effort) ------
    # Fetched before compute_composite so the pressure score can be passed in as
    # an observe-mode sub-score signal.  Never a blocker.  Best-effort: if the
    # completion_debt module is not yet landed or raises, finalize continues
    # with completion_debt=None (pressure score = None → omitted from sub_scores).
    _completion_debt: dict[str, Any] | None = None
    try:
        from importlib import import_module

        _cd_mod = import_module("app.completion_debt")
        _completion_debt = await _cd_mod.get_open_completion_debt(
            session, version.project_id
        )
    except Exception as exc:  # noqa: BLE001 — best-effort debt snapshot, fail-open
        # Keep finalize fail-open, but make the signal loss OBSERVABLE: without
        # this log a debt-ledger query failure would silently yield
        # debt_pressure_score=None, so in enforce mode compute_composite skips
        # the (1 - w_debt) down-scaling and the candidate scores HIGHER on the
        # exact attempt where the debt signal failed, with no trace.
        logger.warning(
            "[RELEASE_QUALIFIER] v%d completion-debt snapshot fetch failed "
            "(%s); continuing without the debt-pressure signal",
            version.version_number,
            exc,
        )
    _project_actionable_open: int | None = None
    if _completion_debt is not None:
        try:
            _project_actionable_open = (
                await _count_project_actionable_open_completion_debt(
                    session, version.project_id
                )
            )
        except Exception as exc:  # noqa: BLE001 — best-effort count, fail-open
            logger.warning(
                "[RELEASE_QUALIFIER] v%d actionable-open completion-debt count "
                "failed (%s); continuing without the actionable-open enrichment",
                version.version_number,
                exc,
            )
    _completion_debt_scoring_snapshot: dict[str, Any] | None = None
    if _completion_debt is not None:
        _completion_debt_scoring_snapshot = dict(_completion_debt)
        if _project_actionable_open is not None:
            _completion_debt_scoring_snapshot["project_actionable_open"] = (
                _project_actionable_open
            )
    if _completion_debt_scoring_snapshot is not None:
        _debt_pressure_score: float | None = _score_completion_debt_pressure(
            _completion_debt_scoring_snapshot
        )
    elif _resolve_completion_debt_weight_mode() == "enforce":
        # In enforce mode a failed debt-snapshot read must NOT inflate confidence
        # by silently dropping the (1 - w_debt) down-scaling inside
        # compute_composite.  Use 0.0 (maximum-debt worst case) so the composite
        # is still bounded correctly instead of being quietly over-scored.
        _debt_pressure_score = 0.0
    else:
        _debt_pressure_score = None

    composite = compute_composite(
        ci_green=ci_green,
        audit_score=audit_score,
        test_erosion=test_erosion_score,
        smoketest_score=smoketest_score,
        policy=policy,
        smoketest_enabled=smoketest_enabled,
        ci_state=ci_state,
        ci_required=ci_required,
        migration_collision=migration_collision,
        migration_apply_blocked=migration_apply_blocked,
        criterion_coverage=criterion_coverage,
        merge_grade_misses=merge_grade_misses,
        debt_pressure_score=_debt_pressure_score,
        capability_proof_blocked=capability_proof_blocked,
    )

    # --- Acceptance-window counts (additive evidence, no score impact) -----
    # Reuses the same verified-prs-merged predicate from direction_outcomes so
    # the release qualification surfaces the same proven-vs-delivered signal as
    # the strategic and direction layers. Merged into sub_scores as additive
    # keys; the existing audit/test_erosion/smoketest weights are unchanged.
    acceptance_window = await _compute_acceptance_window_aggregate(session, version)
    extended_sub_scores = dict(composite["sub_scores"])
    extended_sub_scores.update(acceptance_window)

    # --- Dual-write handled by _dual_write_dimension_results after base_record
    # is fully assembled (below), where all dimensions including challenge are
    # derived from the assembled composite.  The Phase 1a block that wrote
    # mid-finalize variables as raw rows was subsumed by the comprehensive
    # per-dimension write at the end of this function.

    finalized_at = _utcnow().isoformat()
    base_record: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        # Carry the audited-surface hash from the pending record into the verdict
        # so the NEXT qualify's Guard 3 can reuse this verdict for a commit that
        # differs only in CI/fixture/config files (content-hash reuse).
        "candidate_diff_hash": qual.get("candidate_diff_hash"),
        "ci_green": ci_green,
        "ci_resolution": ci_resolution,
        "smoketest_enabled": smoketest_enabled,
        "confidence": composite["confidence"],
        "sub_scores": extended_sub_scores,
        "blocker": composite["blocker"],
        "threshold": policy.get("threshold"),
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        # One-shot keyless re-qualification marker (finding 1).  The sweep's
        # same-SHA keyless re-drive (release_qualifier_sweep._redrive_not_promoted_
        # qualification) bounds itself to exactly-once by checking this marker on
        # the not_promoted record.  base_record REPLACES last_qualification, so it
        # must carry the marker forward from the pending record (run_qualification
        # seeds it from the prior verdict) — otherwise the exactly-once bound is
        # lost and heavy re-qualification can loop on an unchanged HEAD.
        "keyless_requalification_consumed_at": qual.get(
            "keyless_requalification_consumed_at"
        ),
        "audit_task_id": audit_task_id,
        "challenge_task_id": challenge_task_id,
        # Parsed challenge artifact — observable on last_qualification.
        "challenge_verdicts": challenge_verdicts,
        "audit_challenge_task_id": audit_challenge_task_id,
        "audit_challenge_spawn_error": qual.get("audit_challenge_spawn_error"),
        "smoketest_task_id": smoketest_task_id,
        "audit_findings": audit_findings,
        # Agent-authored, backend-preserved explanation of delta selection or
        # full fallback. Kept schema-free and shaped for later dimension detail.
        "audit_scope": (audit_report or {}).get("audit_scope"),
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
        "criterion_coverage": criterion_coverage,
        # Blind merge-grade goal-coverage verdict (dir d-c0fe439f). Mirrored into
        # last_qualification AND persisted on QualificationVerdict so an operator
        # can see the single biggest gap toward the goal.
        "merge_grade_task_id": merge_grade_task_id,
        "merge_grade_status": merge_grade_cert["status"],
        "merge_grade_coverage": merge_grade_cert["coverage"],
        "merge_grade_biggest_gap": merge_grade_cert["biggest_gap"],
        "merge_grade": merge_grade_cert,
        "merge_grade_misses": merge_grade_misses,
        # Goal-alignment grade emitted by the release_audit agent when
        # version.goal is set. None when no goal is configured or the audit
        # predates this field.
        "goal_alignment": (audit_report or {}).get("goal_alignment"),
        # Open completion-debt snapshot at qualification time (additive, informational).
        # None when the ledger module is not yet available or returned no data.
        "completion_debt": _completion_debt,
        # Capability-proof gate dimension (informational in OFF/OBSERVE,
        # gating in ENFORCE).  Recorded in the per-dimension fact ledger.
        "capability_proof_blocked": capability_proof_blocked,
        "capability_proof_detail": capability_proof_detail,
    }

    # Enforce-mode confidence preview — only in observe/off mode, only when no
    # hard blocker fired and debt pressure was actually scored.  Records what
    # enforce would have done to confidence so the operator can graduate the
    # completion_debt_weight_mode knob with accumulated evidence rather than
    # flying blind.  Has no effect on the live composite.
    if (
        _resolve_completion_debt_weight_mode() != "enforce"
        and composite["blocker"] is None
        and _debt_pressure_score is not None
    ):
        # Resolve the configured weight through the FULL provenance chain the
        # enforce blend uses (build_config > runtime_config > yaml), not
        # runtime_config alone — otherwise the preview an operator graduates the
        # knob on could be derived from a different weight than enforce applies.
        _configured_cd_weight = _resolve_configured_completion_debt_weight(
            _policy_project
        )
        _preview = compute_enforce_confidence_preview(
            composite["confidence"],
            _debt_pressure_score,
            configured_weight=_configured_cd_weight,
        )
        base_record["debt_enforce_confidence_preview"] = _preview["enforce_confidence"]
        base_record["debt_enforce_weight_preview"] = _preview["effective_weight"]

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

    # --- Per-dimension fact ledger dual-write (Phase 1, purely additive) -----
    # Every gate/dimension has now resolved into base_record + composite.  Write
    # each as its own (candidate_sha, dimension) ledger row ALONGSIDE the
    # existing last_qualification / QualificationVerdict writes so a later
    # single-gate re-check can re-record only its own fact without discarding the
    # dimensions that already passed for this tree.  Best-effort — never breaks
    # or rolls back the qualification.  Runs before the decision branch so all
    # three terminal paths (not_promoted / dry_run / promote) are covered once.
    await _dual_write_dimension_results(session, version, base_record, composite)

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

    # Same-tree promotion invariant (Phase 1): a promotion composes from the
    # per-dimension ledger only when every required gating dimension has a
    # TERMINAL fact at THIS candidate_sha — the batch model's "everything scored
    # the same tree" guarantee carried onto the ledger.  The dual-write above
    # populated exactly these rows for the candidate moments ago, so the common
    # path is complete; a populated-but-incomplete ledger (a required dimension
    # missing or stranded at a different sha) means the facts do NOT agree on one
    # tree, so HOLD as an infra failure rather than promote on a split verdict.
    # Fail OPEN when the ledger is entirely empty for this candidate (the
    # best-effort dual-write did not land) so a transient ledger-write failure
    # never blocks an otherwise-qualified promotion.
    _ledger_complete, _missing_dims = await _promotion_dimension_ledger_complete(
        session, version, candidate_sha, smoketest_enabled=smoketest_enabled
    )
    if not _ledger_complete:
        _prior_any = await _load_prior_dimension_results(
            session, version, candidate_sha
        )
        if _prior_any:
            logger.warning(
                "[RELEASE_QUALIFIER] v%d ledger incomplete at promote "
                "(candidate=%s missing/split dimensions=%s) — holding rather than "
                "promoting on facts that do not agree on one tree",
                version.version_number,
                (candidate_sha or "")[:8],
                _missing_dims,
            )
            return await _handle_infra_failure(
                session, version, qual, "qualification_dimension_ledger_incomplete"
            )
        # Completely empty ledger.  Distinguish first qualification from a re-drive
        # using qualification_attempts: on a re-drive (attempts > 1) the ledger was
        # populated on a prior successful finalize but is now empty — the current
        # _dual_write_dimension_results silently failed (SAVEPOINT swallowed the
        # write).  Hold rather than fail-open so the same-tree invariant is not
        # silently bypassed on a re-check.
        if (version.qualification_attempts or 0) > 1:
            logger.warning(
                "[RELEASE_QUALIFIER] v%d per-dimension ledger empty at promote "
                "(candidate=%s, attempts=%d) — dual-write silently failed; "
                "holding rather than promoting on the live composite",
                version.version_number,
                (candidate_sha or "")[:8],
                version.qualification_attempts,
            )
            return await _handle_infra_failure(
                session, version, qual, "qualification_dimension_ledger_empty"
            )
        logger.warning(
            "[RELEASE_QUALIFIER] v%d per-dimension ledger empty at promote "
            "(candidate=%s) — best-effort dual-write did not land; promoting on "
            "the live composite (fail-open)",
            version.version_number,
            (candidate_sha or "")[:8],
        )

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

    When one or more dimensions are already resolved via the per-dimension
    ledger (``resolved_dimensions``), their corresponding child task was not
    spawned — the function skips the terminal check for those dimensions since
    the ledger carries the durable fact forward from the prior finalize.
    """
    qual = version.last_qualification or {}

    # Dimensions already resolved via the per-dimension ledger, carried forward
    # from a prior finalize at this candidate_sha.  When present, the
    # corresponding child task was NOT re-spawned, so skip requiring it.
    resolved_dims: dict[str, dict[str, Any]] = qual.get("resolved_dimensions") or {}
    audit_resolved = "audit" in resolved_dims and "test_erosion" in resolved_dims
    smoketest_resolved = "smoketest" in resolved_dims
    merge_grade_resolved = "merge_grade" in resolved_dims

    smoketest_required = bool(qual.get("smoketest_enabled", True))
    audit_task_id_raw = qual.get("audit_task_id")
    challenge_task_id_raw = qual.get("challenge_task_id")
    smoketest_task_id_raw = qual.get("smoketest_task_id")
    challenge_task_id_raw = qual.get("challenge_task_id")
    challenge_required = bool(challenge_task_id_raw)
    merge_grade_task_id_raw = qual.get("merge_grade_task_id")
    merge_grade_required = bool(merge_grade_task_id_raw)

    # When a dimension is already resolved via the ledger, its child task id
    # is legitimately None (the spawn coordinator skipped it).  Skip requiring
    # it rather than returning False for a "missing" task id.
    if (not audit_task_id_raw and not audit_resolved) or (
        smoketest_required
        and not smoketest_task_id_raw
        and not smoketest_resolved
    ):
        logger.warning(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d has pending "
            "qualification but missing required child task ids "
            "(audit=%r smoke=%r smoketest_required=%s audit_resolved=%s "
            "smoketest_resolved=%s) — not ready",
            version.version_number,
            audit_task_id_raw,
            smoketest_task_id_raw,
            smoketest_required,
            audit_resolved,
            smoketest_resolved,
        )
        return False

    try:
        audit_task_id = (
            uuid.UUID(str(audit_task_id_raw))
            if audit_task_id_raw is not None
            else None
        )
        smoketest_task_id = (
            uuid.UUID(str(smoketest_task_id_raw))
            if smoketest_required and smoketest_task_id_raw
            else None
        )
        challenge_task_id = (
            uuid.UUID(str(challenge_task_id_raw)) if challenge_required else None
        )
        merge_grade_task_id = (
            uuid.UUID(str(merge_grade_task_id_raw)) if merge_grade_required else None
        )
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d has unparseable "
            "child task ids (audit=%r challenge=%r smoke=%r merge_grade=%r) — "
            "not ready",
            version.version_number,
            audit_task_id_raw,
            challenge_task_id_raw,
            smoketest_task_id_raw,
            merge_grade_task_id_raw,
        )
        return False

    audit_task: Task | None = (
        await session.get(Task, audit_task_id)
        if audit_task_id is not None
        else None
    )
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
    merge_grade_task: Task | None = (
        await session.get(Task, merge_grade_task_id)
        if merge_grade_task_id is not None
        else None
    )

    if (
        (not audit_resolved and audit_task is None)
        or (smoketest_required and not smoketest_resolved and smoketest_task is None)
        or (challenge_required and challenge_task is None)
        or (merge_grade_required and not merge_grade_resolved and merge_grade_task is None)
    ):
        logger.debug(
            "[RELEASE_QUALIFIER] finalize-gate: version v%d required child "
            "tasks not yet persisted — not ready",
            version.version_number,
        )
        return False

    if (
        (not audit_resolved and audit_task is not None and audit_task.status not in _TERMINAL_STATUSES)
        or (
            smoketest_required
            and not smoketest_resolved
            and smoketest_task is not None
            and smoketest_task.status not in _TERMINAL_STATUSES
        )
        or (
            challenge_required
            and challenge_task is not None
            and challenge_task.status not in _TERMINAL_STATUSES
        )
        or (
            merge_grade_required
            and not merge_grade_resolved
            and merge_grade_task is not None
            and merge_grade_task.status not in _TERMINAL_STATUSES
        )
    ):
        # A required child is still running — not time to finalize yet.
        return False

    logger.info(
        "[RELEASE_QUALIFIER] finalize-gate: required children terminal for "
        "version v%d (audit=%s/%s smoke=%s challenge=%s)",
        version.version_number,
        audit_task.id if audit_task is not None else "resolved",
        audit_task.status if audit_task is not None else "N/A",
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

            spawned = await _spawn_strategic_review(
                project,
                session,
                apply_spawn_budget_guard=True,
            )
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


async def _reattempt_promotion_failed_locked(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str,
    candidate_diff_hash: str,
) -> dict[str, Any] | None:
    """Lock + re-promote a ``promotion_failed`` version on an open dedicated session.

    The body of ``_reattempt_promotion_failed_in_dedicated_session`` (which owns
    the session + its rollback-on-error contract).  Re-verifies the reuse gate
    AUTHORITATIVELY under a ``SELECT ... FOR UPDATE`` on the version row (the
    pre-lock check in ``run_qualification`` may be stale), reconstructs the
    ``composite`` / ``base_record`` from the PASSING fields the promotion_failed
    record carries, and re-enters ``promote()`` on the CURRENT ``candidate_sha``
    WITHOUT re-auditing.  Commits the dedicated session on the promote path
    (``promote()`` issues its own intermediate commit but the FINAL state write
    in ``_record_promotion_{success,failure}`` is committed by the caller —
    mirrors ``_finalize_locked_version``).  Returns the ``promote()`` result, or
    ``None`` for every no-op guard so ``run_qualification`` falls through to the
    normal re-audit path.
    """
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
        return None
    # Only an OPEN, stabilize-phase version can be re-promoted.  A concurrent
    # finalize/promote may have flipped it to promoted/abandoned or moved it out
    # of stabilize between the pre-lock read and this lock — bail to re-audit.
    if version.status != "open" or version.phase != "stabilize":
        return None
    qual = version.last_qualification or {}
    # Authoritative re-verify of the reuse gate under the lock: a concurrent
    # re-attempt may have already advanced the state (e.g. a new promotion_failed
    # with a different hash, or a promoted verdict).
    if not _promotion_failed_reuse_eligible(qual, candidate_diff_hash):
        return None
    # Cap re-check under the lock — a concurrent re-attempt may have bumped the
    # promotion_failure_count to the cap since the pre-lock read.  Never
    # re-promote a persistently-red candidate forever (mirrors the sweep
    # re-driver's circuit breaker).
    if (
        int(qual.get("promotion_failure_count") or 0)
        >= _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS
    ):
        return None

    # Reconstruct the composite promote() needs (it only reads confidence, for
    # the PR body) from the passing verdict — do NOT re-score.
    composite = {
        "confidence": qual.get("confidence"),
        "promote": True,
        "blocker": qual.get("blocker"),
        "sub_scores": qual.get("sub_scores"),
    }
    # Reconstruct base_record from the promotion_failed record, which is exactly
    # the passing base_record PLUS the promotion-outcome keys.  Strip those
    # outcome keys, and pin candidate_sha + candidate_diff_hash to the CURRENT
    # candidate (its audited surface equals the passed one by hypothesis, so the
    # reused audit findings/scores are valid for it, but the promotion + the
    # recorded verdict must target the commit that carries the fix).
    _outcome_keys = {
        "status",
        "error",
        "detail",
        "conflict",
        "pr_url",
        "promotion_pending",
        "promotion_failure_count",
        "promotion_loop_escalated",
    }
    base_record = {k: v for k, v in qual.items() if k not in _outcome_keys}
    base_record["candidate_sha"] = candidate_sha
    base_record["candidate_diff_hash"] = candidate_diff_hash

    result = await promote(
        session,
        version,
        candidate_sha,
        composite=composite,
        base_record=base_record,
    )
    await session.commit()
    return result


async def _reattempt_promotion_failed_in_dedicated_session(
    version_id: uuid.UUID,
    candidate_sha: str,
    candidate_diff_hash: str,
    *,
    session_factory=None,
) -> dict[str, Any] | None:
    """Re-attempt promotion of a ``promotion_failed`` version WITHOUT re-auditing.

    The passing-audit-reuse chokepoint for ``run_qualification``'s promotion_failed
    fast-promote path (Henry, 2026-07-02).  Mirrors
    ``_finalize_in_dedicated_session``'s discipline exactly:

    **Why a dedicated session.**  ``promote()`` issues an intermediate
    ``session.commit()`` to release the ``ReleaseVersion`` row lock before its
    30-120s of GitHub I/O.  ``run_qualification``'s caller session may hold
    UNRELATED dirty work (the strategic path's post-commit chain); committing it
    mid-run would destroy that caller's atomicity.  So this opens its OWN session
    via ``AsyncSessionLocal`` (injectable for tests), whose only dirty state is
    the qualifier's own promote write.

    **Why it cannot self-deadlock.**  ``run_qualification`` calls this from its
    PRE-LOCK section — before it takes its own FOR UPDATE on the version row — so
    the fresh session's ``SELECT ... FOR UPDATE`` never blocks on a lock held by
    the same logical caller.  The strategic caller's Project lock is
    ``FOR NO KEY UPDATE`` (``lock_project_then_task``), which does NOT conflict
    with the ``FOR KEY SHARE`` an ``open_version`` FK-insert takes on the Project
    row, and the promote chain never UPDATEs the Project row — so the dedicated
    promote cannot deadlock against a concurrent strategic cycle either.

    **Idempotency / no double-promote.**  Reuses ``promote()``'s existing
    machinery: the ``_candidate_already_on_main`` ancestry check routes an
    already-landed candidate straight to success, the Fix-3 under-lock
    ``promotion_pending`` claim fences concurrent re-qualification, and the
    ``version.status != "open"`` re-check in ``_record_promotion_{success,failure}``
    makes an overlapping promote bail.  A repeat failure records ``promotion_failed``
    and increments ``promotion_failure_count`` exactly as today — the cap
    eventually stops it.

    Returns the ``promote()`` result dict, or ``None`` when the under-lock
    re-verify no-oped (so ``run_qualification`` falls through to a normal
    re-audit).
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            result = await _reattempt_promotion_failed_locked(
                session, version_id, candidate_sha, candidate_diff_hash
            )
        except Exception:
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reuse-promote: rollback failed for "
                    "version %s",
                    version_id,
                )
            raise

    # The dedicated session has now committed + closed.  Run the same post-commit
    # hooks the finalize path runs (outside the session + row lock, best-effort):
    # a merge-conflict failure triggers a main → automain reconciliation, and any
    # resolved outcome re-triggers the strategic agent so it re-plans.  Only when
    # a promote actually ran (result is not None) — a no-op reuse leaves the
    # version exactly as it was.
    if result is not None:
        await _maybe_reconcile_after_promotion_conflict(
            version_id, result, session_factory=session_factory
        )
        await _maybe_trigger_strategic_after_qualification(
            version_id, result, session_factory=session_factory
        )
    return result


# ---------------------------------------------------------------------------
# Operator FORCE-PROMOTE — skip the audit gate, still open the PR + wait for CI.
# ---------------------------------------------------------------------------
# Statuses returned by force_promote_current_version / _force_promote_locked that
# mean promote() never ran (a pre-promote no-op guard) — the post-commit hooks
# and any "promotion attempted" messaging must skip these.
_FORCE_PROMOTE_NOOP_STATUSES: frozenset[str] = frozenset(
    {
        "no_open_version",
        "project_not_found",
        "no_credentials",
        "sha_error",
        "version_not_found",
        "not_open",
        "promotion_in_flight",
    }
)


async def _force_promote_locked(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str,
    candidate_diff_hash: str | None,
    actor: str | None,
) -> dict[str, Any]:
    """Lock + FORCE-promote an open version on a dedicated session (no audit).

    Body of ``_force_promote_in_dedicated_session``.  Takes ``SELECT..FOR UPDATE``
    on the version row, refuses when the version is not open or a promotion is
    already in flight (Guard 2b / the Fix-3 claim), builds a FORCED
    composite/base_record (no audit score — marked ``forced_promote``), and
    re-enters ``promote()`` on the current candidate, mirroring
    ``_finalize_locked_version``'s commit discipline.
    """
    version = (
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
        return {"status": "version_not_found"}
    if version.status != "open":
        return {"status": "not_open", "version_status": version.status}
    # Refuse when a promotion is already in flight (an audit-passed PR open + the
    # Fix-3 claim, or a concurrent force-promote) — mirrors the /qualify + the
    # version-cut levers so every lever over an in-flight promotion agrees.
    # Apply finalized-verdict substitution so a stale-pending+finalized-verdict
    # is not misclassified as still-blocking.
    from app.qualification_state_resolver import resolve_newest_qualification_verdict

    _latest_verdict_force = await resolve_newest_qualification_verdict(session, version)
    _promo = _promotion_in_flight_result(version, latest_verdict=_latest_verdict_force)
    if _promo is not None:
        return _promo
    now = _utcnow()
    # Forced composite: NO audit score.  promote() only reads ``confidence`` for
    # the PR body (renders 'n/a' when absent), so omit it.
    composite: dict[str, Any] = {"promote": True, "forced_promote": True}
    base_record: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "candidate_diff_hash": candidate_diff_hash,
        "confidence": None,
        "forced_promote": True,
        "forced_promote_by": actor,
        "forced_promote_at": now.isoformat(),
        "started_at": now.isoformat(),
        "finalized_at": now.isoformat(),
    }
    logger.warning(
        "[RELEASE_QUALIFIER] FORCE-PROMOTE: v%d cutting release branch + opening "
        "promotion PR at candidate %s (audit gate SKIPPED; CI still gates the "
        "merge) — actor=%s",
        version.version_number,
        candidate_sha[:8],
        actor,
    )
    result = await promote(
        session,
        version,
        candidate_sha,
        composite=composite,
        base_record=base_record,
    )
    await session.commit()
    return result


async def _force_promote_in_dedicated_session(
    version_id: uuid.UUID,
    candidate_sha: str,
    candidate_diff_hash: str | None,
    actor: str | None,
    *,
    session_factory=None,
) -> dict[str, Any]:
    """Run FORCE-promote on a DEDICATED session (mirrors the reuse-promote path).

    A dedicated ``AsyncSessionLocal`` is required because ``promote()`` issues an
    intermediate lock-releasing commit — running it on the request session would
    prematurely commit unrelated work.  Runs the same post-commit hooks the
    finalize/reuse paths run, but only when a promotion actually attempted.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            result = await _force_promote_locked(
                session, version_id, candidate_sha, candidate_diff_hash, actor
            )
        except Exception:
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] force-promote: rollback failed for "
                    "version %s",
                    version_id,
                )
            raise

    if result.get("status") not in _FORCE_PROMOTE_NOOP_STATUSES:
        await _maybe_reconcile_after_promotion_conflict(
            version_id, result, session_factory=session_factory
        )
        await _maybe_trigger_strategic_after_qualification(
            version_id, result, session_factory=session_factory
        )
    return result


async def force_promote_current_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_sha: str | None = None,
    actor: str | None = None,
    session_factory=None,
) -> dict[str, Any]:
    """Operator FORCE-PROMOTE the project's open version.

    SKIPS the audit / qualification gate, but still cuts the release branch,
    opens the ``release-vN → promotion-target`` PR, and lets the NORMAL CI-wait /
    managed-check-wait machinery merge it once the promotion target's required
    checks (branch-protection CI) pass — 'force' skips the AUDIT, not CI.

    Resolves the candidate (an explicit SHA, else the current integration-branch
    HEAD) + credentials on the request session (pre-lock GitHub I/O), then
    re-enters ``promote()`` in a DEDICATED session (``promote()`` releases the row
    lock mid-flight).  Refuses when a promotion is already in flight.  Returns the
    ``promote()`` result, or a no-op guard status.
    """
    version = await release_version.current_open_version(session, project_id)
    if version is None:
        return {"status": "no_open_version"}
    project = await session.get(Project, project_id)
    if project is None:
        return {"status": "project_not_found"}
    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        return {"status": "no_credentials"}
    token, owner, repo = resolved
    integration_branch, _promotion_target = _lts_branch_names(project)
    if not candidate_sha:
        try:
            candidate_sha = await github.get_branch_sha(
                token, owner, repo, integration_branch, live=True
            )
        except Exception as exc:  # noqa: BLE001 — a missing branch / API error is recoverable
            return {"status": "sha_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not candidate_sha:
        return {"status": "sha_error", "detail": "empty candidate sha"}
    candidate_diff_hash = compute_audited_surface_hash(
        await github.list_repo_tree_paths_at_ref(
            token, owner, repo, candidate_sha, project_id=str(project_id)
        )
    )
    logger.warning(
        "[RELEASE_QUALIFIER] FORCE-PROMOTE requested: project %s v%d candidate %s "
        "actor=%s — SKIPPING the audit/qualify gate (CI still gates the merge)",
        project_id,
        version.version_number,
        candidate_sha[:8],
        actor,
    )
    return await _force_promote_in_dedicated_session(
        version.id,
        candidate_sha,
        candidate_diff_hash,
        actor,
        session_factory=session_factory,
    )


def _qualification_timeout_age_seconds(
    version: release_version.ReleaseVersion,
    qual: dict[str, Any],
    timeout_now: datetime,
    *,
    warn_on_fallback: bool,
) -> float | None:
    """Return pending qualification age in seconds, or None if no clock exists."""
    timeout_clock = _parse_started_at(qual)
    if timeout_clock is None:
        timeout_clock = (
            version.updated_at or version.phase_changed_at or version.opened_at
        )
        if timeout_clock is not None and warn_on_fallback:
            logger.warning(
                "[RELEASE_QUALIFIER] finalize: version v%d has a "
                "missing/unparseable started_at (%r) — using updated_at "
                "as the timeout clock so the backstop still fires",
                version.version_number,
                qual.get("started_at"),
            )
    if timeout_clock is None:
        return None
    return (timeout_now - timeout_clock).total_seconds()


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
    pre_timeout_age_seconds = (
        _qualification_timeout_age_seconds(
            peek,
            peek_qual,
            timeout_now,
            warn_on_fallback=False,
        )
        if timeout_now is not None and not peek_qual.get("promotion_pending")
        else None
    )
    pre_timeout_expired = (
        pre_timeout_age_seconds is not None
        and pre_timeout_age_seconds > _MAX_QUALIFICATION_DURATION_SECONDS
    )

    # ── Run the three GitHub-bound gates BEFORE acquiring the FOR UPDATE ──
    # Guard: only run when all required children are already terminal according
    # to the non-locking peek.
    # Without this guard the gates ran speculatively on every qualifier child
    # completion (Nx amplification) and on every reaper sweep iteration for
    # versions whose children were still running — a DB/GitHub load regression.
    #
    # When _pre_gates is None (children not yet terminal, timeout already
    # expired, or a readiness race),
    # _finalize_qualification will either be skipped entirely (Path 2 returns
    # early) or run with _pre_gates=None and call the gates itself under the
    # lock — acceptable because that path is rare (race between sibling
    # completion and this check) and the reaper sweep is infrequent.
    _pre_gates: dict[str, Any] | None = None
    if not pre_timeout_expired and await _required_children_terminal(session, peek):
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
        (
            pre_apply_verdict,
            pre_apply_detail,
        ) = await _resolve_migration_apply_at_finalize(
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
        if pre_ci_resolution.get("state") == _CI_NO_CHECKS:
            _pre_proj_for_ci_gate = await session.get(Project, peek.project_id)
            _pre_gates["ci_required"] = _ci_required_for_finalize(
                _pre_proj_for_ci_gate, pre_ci_resolution
            )
        # The pre-lock phase is read-only and intentionally best-effort. Some
        # helpers fail closed by swallowing exceptions, but a swallowed DBAPI
        # error still leaves the SQLAlchemy transaction aborted; without this
        # rollback the later FOR UPDATE/commit surfaces as PendingRollbackError
        # and the sweep misclassifies the qualification as a finalizer crash.
        await session.rollback()

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
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )

    if version is None:
        logger.debug(
            "[RELEASE_QUALIFIER] finalize: version %s not found or locked — skipping",
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
        age_seconds = _qualification_timeout_age_seconds(
            version,
            qual,
            timeout_now,
            warn_on_fallback=True,
        )
        if (
            age_seconds is not None
            and age_seconds > _MAX_QUALIFICATION_DURATION_SECONDS
        ):
            await _force_finalize_timed_out_qualification(
                session, version, age_seconds
            )
            await session.commit()
            return {
                "status": "infra_failure",
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

      2. Takes ``SELECT ... FOR UPDATE SKIP LOCKED`` on the ``ReleaseVersion`` row — the
         serialization point for the non-promote paths.  The ``release_audit``
         and qualifier ``smoketest`` children can terminate
         near-simultaneously, and a reaper sweep can run concurrently; for a
         ``dry_run`` / ``not_promoted`` outcome a peer that cannot acquire the
         lock skips; a later retry bails at the pending guard once the first
         finalize commits.
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
