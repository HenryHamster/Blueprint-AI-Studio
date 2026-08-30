"""Release qualifier — the automain → main promotion gate.

The qualifier is an asynchronous coordinator. ``run_qualification`` snapshots
one candidate SHA, resolves CI, and spawns only the active audit, challenge,
merge-grade, and evidence children. ``_finalize_qualification`` composes those
active results with deterministic migration, environment, and promotion-PR
gates, then either promotes the exact qualified commit or records the blocker.

Qualifier VM smoke is retired. Historical ``smoketest_enabled`` and
``smoketest_task_id`` values remain readable as archival metadata, but no
current attempt spawns, waits for, respawns, reads an artifact from, scores,
ledgers, or blocks on that lane. Ordinary post-promotion deployment smoke is a
separate task-lifecycle check and remains available.

``maybe_finalize_qualification`` owns a dedicated database session for the
finalize-to-promote chain so GitHub I/O and the promotion commit cannot flush a
caller transaction. Actual promotion remains dark unless
``runtime_config["promotion_enabled"]`` is true.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Collection

from sqlalchemy import func as sa_func
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import github, release_version
from app import release_qualification_deferral as qualification_deferral
from app import release_task_admission
from app.database import AsyncSessionLocal, resolve_async_engine
from app.failure_reason import exception_reason
from app.failure_classification import (
    ADMIN_CANCEL,
    AGENT_TIMEOUT,
    PROVISION_FAILURE,
    RATE_LIMIT,
    classify_failure,
)
from app.acceptance_gate import (
    ACCEPTANCE_AGGREGATE_BUCKETS,
    ACCEPTANCE_RULE_VERSION,
    ACCEPTANCE_TEST_CORROBORATED_KEY,
    CODE_ACCEPTANCE_VERDICT_KEY,
    WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY,
    _UNDECLARED_REASON_KEYS,
    _UNSATISFIED_REASON_KEYS,
    _is_test_path,
    classify_merged_acceptance_context,
    classify_undeclared_reason,
    classify_unsatisfied_reason,
    effective_acceptance_verdict,
    record_code_acceptance_verdict,
    verdict_rule_version,
)
from app.acceptance_criteria import (
    WALKTHROUGH_E2E_FLOW_MARKERS,
    WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS,
    get_criteria_for_version,
    get_version_coverage_summary,
    normalize_evidence_contract,
)
from app.acceptance_provenance import PR_CHANGED_FILES_STAMP_SKIPPED_KEY

# Per-(sha, category) goal-verdict ledger (goal-verification redesign).  A SIBLING
# module owns this file; it may not be present in every checkout during the
# staged rollout of the redesign, so the import is guarded exactly like the
# best-effort ``app.completion_debt`` snapshot below.  When absent, the qualifier
# falls back to the pre-redesign path (no per-category gate, holistic merge-grade
# only) rather than failing to import.  The integrator's tree carries the module.
try:  # pragma: no cover - exercised once the sibling ledger module lands
    from app import category_verdict_ledger as _category_ledger
except Exception:  # noqa: BLE001 - sibling module not yet landed in this checkout
    _category_ledger = None  # type: ignore[assignment]

from app.landed_signal import LANDED_MERGE_SHA_KEY
from app.landed_signal import is_verified_merge, landed_signal_predicate
from app.models import (
    ArtifactType,
    DeploymentState,
    Project,
    QualificationDimensionResult,
    Task,
    TaskStatus,
)
from app.task_locking import AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
from app.release_policy_projection import (
    promotion_policy_projection,
    qualification_attempt_policy_projection,  # noqa: F401 — re-exported for callers/tests
    qualification_attempt_policy_record_fields,
)
from app.release_models import CriterionEvidence
from app.release_authority_lock import lock_project_then_version
from app.release_qualification_authority import (
    _apply_ci_unresolved_promotion_observation,
    _authorship_receipt_from_measurement,
    _candidate_already_on_main_at_finalize,
    _count_project_actionable_open_completion_debt,
    _deployed_env_presence_snapshot,
    _lock_exact_qualification_authority,
    _measure_candidate_operator_authorship,
    _measured_authorship_receipt_for_audit_spawn,
    _observe_ci_unresolved_promotion_pending,
    qualification_authority_fingerprint,
    qualification_finalize_authority_fingerprint,
    qualification_project_observation_fingerprint,
)
from app.release_qualification_observation_protocol import (
    ALREADY_ON_MAIN,
    CI_RECOVERY,
    FinalizeObservationRequired,
    extend_finalize_observation_packet,
)
from app.release_commit_authorship import (
    authorship_category_evidence,
    authorship_receipt_for_measurement,
    candidate_authorship_range,
    count_non_bot_authored_commits,  # noqa: F401 — authority-module patch surface
    resolve_candidate_base_sha,  # noqa: F401 — authority-module patch surface
)
from app.notification_dispatch import (
    dispatch_external_notification,  # noqa: F401 — re-exported for tests / compat patch point
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
    _candidate_already_on_main,  # noqa: F401 — authority-module patch surface
    _idempotent_pr_url,
    _lts_branch_names,
    _merge_grade_misses_armed,
    _notify_operator,
    _record_promotion_failure,
    _record_promotion_success,
    _record_qualification_verdict,
    _recover_confidence_from_ci_unresolved,
    _relock_version_for_promotion_write,  # noqa: F401 — tests access via rq.*
    _resolve_exact_promotion_receipt,
    _resolve_github_with_session,
    promote,
)
from app.release_reconciliation import (
    _reconcile_if_deploy_failed_recovered,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_not_promoted_but_pr_merged,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_promotion_failed_landed,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_promotion_pending_timed_out,  # noqa: F401 — re-exported for back-compat
)
from app.release_qualifier_audit import (
    # Re-exports — release_qualifier stays the canonical import + patch surface
    # for the qualifier's audit/spawn and infra-failure seam.
    _MIN_INFRA_ERROR_STREAK,  # noqa: F401 — re-exported for callers/tests
    _build_audit_leg_proof,
    append_infra_failure_child_event,
    dispatch_postcommit_infra_notification,
    _enrich_infra_error,
    _handle_independent_blocker,  # re-exported for callers/tests
    _handle_infra_failure,
    _latest_artifact_logs,
    _select_lifecycle_audit,  # noqa: F401 — re-exported for callers/tests
    _spawn_release_audit_challenge_task,
    _spawn_release_audit_task,
    _spawn_release_merge_grade_task,
    _spawn_direction_merge_grade_task,  # noqa: F401 — re-exported for callers/tests
    _spawn_qualifier_walkthrough_task,
    record_direction_merge_grade,  # noqa: F401 — re-exported for callers/tests
    get_latest_qualification_audit_findings,  # noqa: F401 — re-exported for callers/tests
    resolve_lifecycle_audit_evidence,  # noqa: F401 — re-exported for callers/tests
    snapshot_stranded_approved_prs,
    required_children_terminal as _required_children_terminal,
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
    PROMOTION_CONFIDENCE_THRESHOLD,  # noqa: F401 — canonical default for tests/callers
    _FINDING_SUMMARY_MAX,
    _MAX_CARRIED_FINDINGS,  # noqa: F401
    _MAX_DELETED_TEST_FILES,  # noqa: F401 — re-exported for callers/tests
    _MIN_TELEMETRY_SAMPLE,  # noqa: F401 — re-exported for callers/tests
    _SEVERITY_RANK,
    _SEVERITIES_REQUIRING_VERIFICATION,
    _audit_finding_key,  # noqa: F401 — re-exported for callers/tests
    _audit_findings_carry_meta,
    _audit_missing_reason,
    blocker_execution_site,
    blocker_execution_site_label,
    compute_enforce_confidence_preview,
    DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT,
    MAX_COMPLETION_DEBT_WEIGHT,
    _clamp01,  # noqa: F401
    coerce_completion_debt_weight,
    _compute_audit_reconciliation,
    _compute_challenge_synthesis_from_binding_verdicts,
    _extract_audit_findings,
    _extract_excluded_audit_findings,
    _extract_test_erosion_detail,
    _finding_subject_is_authorized_divergence,
    _finding_is_verified,
    _fmt_sub_scores,
    _reconcile_audit_with_challenge,
    _score_audit,
    _score_completion_debt_pressure,
    _score_env_preflight,
    compute_challenged_vs_unchallenged_telemetry,  # noqa: F401 — re-exported for callers/tests
    compute_composite,
    evaluate_candidate_operator_authorship_gate,
    evaluate_merge_grade,
    is_contentless_goal,
    promotion_threshold_for_policy,  # noqa: F401 — canonical enforcement helper
)
from app.release_stall_alerts import (
    _BUILD_STALL_AGE_DAYS,  # noqa: F401 — re-exported for back-compat
    _BUILD_STALL_MERGED_TASKS,  # noqa: F401 — re-exported for back-compat
    _MIN_CALCIFICATION_ATTEMPTS,  # noqa: F401 — re-exported for back-compat
    _STABILIZE_STALL_AGE_DAYS,  # noqa: F401 — re-exported for back-compat
    _STABILIZE_STALL_MERGED_TASKS,  # noqa: F401 — re-exported for back-compat
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
    _reconcile_hotfix_audit_trail,  # noqa: F401
    sweep_stuck_qualifications,  # noqa: F401 — re-exported for reaper + tests
)
from app.migration_apply_gate import (
    MIGRATION_APPLY_SCRATCH_ENV,
    preflight_migration_apply_scratch,
    run_migration_apply_gate,
)
from app.env_preflight_evidence import sanitize_env_preflight_artifact
from app.qualification_dimensions import (
    LEDGER_DIMENSION_KEYS,
    MUTABLE_DIMENSION_KEYS,
    PROMOTION_REQUIRED_DIMENSIONS,
)
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

# Re-exports — release_qualifier is the canonical import + test-patch
# surface for the audited-surface hashing / scoped-delta-reuse seam (tests
# access via ``rq.*`` / ``_rq.*``; release_hotfix_promotion imports
# ``compute_audited_surface_hash`` from here). Implementations live in
# release_qualifier_audit_surface.py; noqa pins keep a ruff --fix sweep from
# dropping the re-exports as "unused".
from app.release_qualifier_audit_surface import (
    _CONCLUSIVE_VERDICT_STATUSES,  # noqa: F401 — re-exported for callers/tests
    _audit_detail_is_delta_reusable,  # noqa: F401 — re-exported for callers/tests
    _audit_detail_surface_map,  # noqa: F401 — re-exported for callers/tests
    _audit_dimension_detail,  # noqa: F401 — re-exported for callers/tests
    _audit_finding_identity,  # noqa: F401 — re-exported for callers/tests
    _audit_finding_path,  # noqa: F401 — re-exported for callers/tests
    _audit_report_confirms_goal_category_range,  # noqa: F401 — re-exported for callers/tests
    _audit_report_confirms_incremental_base,  # noqa: F401 — re-exported for callers/tests
    _audit_report_raw_findings,  # noqa: F401 — re-exported for callers/tests
    _audited_surface_hash_from_map,  # noqa: F401 — re-exported for callers/tests
    _carried_finding_as_report_item,  # noqa: F401 — re-exported for callers/tests
    _changed_audited_surface_paths,  # noqa: F401 — re-exported for callers/tests
    _incremental_audit_base_sha,  # noqa: F401 — re-exported for callers/tests
    _is_audited_surface_path,  # noqa: F401 — re-exported for callers/tests
    _merge_scoped_audit_report_with_prior_fact,  # noqa: F401 — re-exported for callers/tests
    _merge_scoped_test_erosion_with_prior_fact,  # noqa: F401 — re-exported for callers/tests
    _promotion_failed_reuse_eligible,  # noqa: F401 — re-exported for callers/tests
    _reviewed_audit_paths,  # noqa: F401 — re-exported for callers/tests
    audited_surface_path_map,  # noqa: F401 — re-exported for callers/tests
    compute_audited_surface_hash,  # noqa: F401 — re-exported for callers/tests
)

# Size governance: this module's cap in backend/testing/module_size_limits.json.
# Bumped 10300 -> 10800 (tight governing raise) when the authorship evidence
# contract and reconciliation hard-deadline logic brought the module to 10549
# lines, past the historically governing 10500. The raise keeps only a small
# explicit margin (~250 lines, <3% of the cap) — NOT an unbounded generic bump,
# which reintroduces the ~2k dead-headroom value the governance test rejects.
# Keep the JSON entry in sync with this constant — the governance test in
# test_release_qualifier.py cross-checks them, so an over-loosened cap regresses
# red instead of silently widening headroom past the point the guardrail fires.
_MODULE_SIZE_GOVERNANCE_CAP: int = 10900

# Product probes are opt-in parts of a qualification attempt's decision
# contract. Keep the set closed: silently dropping an unknown value would let a
# caller believe an important/costly check ran when the verdict ignored it.
_SUPPORTED_QUALIFICATION_PROBES: frozenset[str] = frozenset({"walkthrough_e2e"})

# Report-contract ``invalid`` reasons from _evaluate_walkthrough_e2e_report:
# the REPORT itself violates the deployed qualifier's evidence contract (malformed
# entries, duplicate raw keys, or a schema/legacy-shape mismatch) rather than
# failing provenance or the runner/harness lane.  These are DEPLOYED-side
# failures — the validator runs in the serving process — so they block as
# ``walkthrough_e2e_contract_failed`` instead of holding as an infra failure.
# The remaining ``invalid`` reasons (``report_missing``, ``candidate_sha_mismatch``,
# ``exercised_engine_sha_mismatch``, ``results_missing``) stay infra-shaped.
_WALKTHROUGH_REPORT_CONTRACT_INVALID_REASONS: frozenset[str] = frozenset(
    {"result_schema_mismatch", "result_key_duplicate", "result_entry_invalid"}
)

# Frozen output contract of scripts/walkthrough/v26-live-walkthrough.cjs.  The
# first three raw rows must stay coherent with the meta-criterion gate in
# acceptance_criteria (the rubric-resolved required sub-criteria, which for the
# v26 rubric are c-dfdc9d82 and c-f52fd975): the fourth row is their
# conjunction/meta result.  The workday slug is a supported legacy alias
# for the registered meta criterion, so those two raw rows intentionally
# collapse to one canonical evidence key.  No other raw or canonical duplicate
# is part of the contract.
#
# THESE CONSTANTS ARE OBSERVATIONAL — they describe what the current
# v26-output harness writes.  They are NOT a blanket key contract on a
# decision-bearing report: a report that emits NONE of the legacy SUBSTANTIVE
# keys (a harness re-pointed to the OPEN version's rubric) is accepted and its
# keys are validated against the ACTIVE rubric downstream, while a report that
# DOES emit a legacy substantive key must still carry the complete v26 set (a
# partial legacy report is truncated).  The canonical meta key c-5214996f is
# NOT a legacy signal for this rule: it participates in both the retired v26
# report and the open rubric, so a report carrying the meta row alongside fresh
# substantive keys is an open-rubric report.  The report validator
# (_evaluate_walkthrough_e2e_report) and the canonical-key resolver
# (_walkthrough_e2e_canonical_evidence_keys) fail a genuinely malformed report
# (empty results, invalid entries, duplicate raw keys, an incomplete legacy set,
# or an unexpected canonical collision).  Which criteria a report may name is
# resolved at request time (see _resolve_walkthrough_e2e_evidence_contract) and
# re-validated at finalize time against the OPEN version's ACTIVE criteria, so a
# rubric that re-mints its criteria under fresh keys is not silently refused just
# because the harness no longer emits the legacy keys.
_WALKTHROUGH_E2E_EXPECTED_RAW_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "c-dfdc9d82",
        "c-f52fd975",
        "c-workday-session-lifecycle",
        "c-5214996f",
    }
)
# The SUBSTANTIVE subset of the legacy report contract — the raw rows that carry
# actual flow evidence.  The meta key c-5214996f is deliberately excluded: it is
# the canonical meta criterion that participates in BOTH the retired v26 report
# and the open rubric, so a report that carries the meta row alongside fresh
# open-rubric keys is an OPEN-rubric report, not a partial legacy report.  The
# partial-legacy rule below triggers on these keys only; the COMPLETE four-row
# contract is still required once a report uses any of them.
_WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_RAW_KEYS: frozenset[str] = frozenset(
    {
        "c-dfdc9d82",
        "c-f52fd975",
        "c-workday-session-lifecycle",
    }
)
_WALKTHROUGH_E2E_EXPECTED_CANONICAL_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {"c-dfdc9d82", "c-f52fd975", "c-5214996f"}
)
_WALKTHROUGH_E2E_ALLOWED_CANONICAL_COLLISION: frozenset[str] = frozenset(
    {"c-workday-session-lifecycle", "c-5214996f"}
)

# The flow markers (``WALKTHROUGH_E2E_FLOW_MARKERS``) and legacy harness keys
# (``WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS``) live in acceptance_criteria so the
# evidence-contract resolver here and the meta-criterion gate there share one
# source of truth.  See ``_resolve_walkthrough_e2e_evidence_contract`` below.


def _normalize_qualification_probes(raw: Any) -> frozenset[str]:
    """Return a strict, bounded product-probe selection."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError("requested_probes must be a collection of probe names")
    normalized: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("requested_probes entries must be strings")
        probe = item.strip().lower()
        if not probe:
            raise ValueError("requested_probes entries must not be blank")
        if probe not in _SUPPORTED_QUALIFICATION_PROBES:
            raise ValueError(f"unsupported qualification probe: {probe[:80]}")
        if probe in normalized:
            raise ValueError(f"duplicate qualification probe: {probe}")
        normalized.add(probe)
    if len(normalized) > len(_SUPPORTED_QUALIFICATION_PROBES):
        raise ValueError("requested_probes contains too many entries")
    return frozenset(normalized)


def _resolve_active_criterion_keys(rows: list[Any]) -> frozenset[str]:
    """Resolve the ACTIVE criterion keys from a version's criterion rows.

    Mirrors the finalize-time active-criterion resolution (live_criterion_rows
    filtered to ``status == "active"``) so the request-time probe gate and the
    finalize-time report-key gate read the rubric the same way.
    """
    return frozenset(
        str(row.criterion_key)
        for row in rows
        if row.status == "active" and row.criterion_key
    )


def _resolve_walkthrough_e2e_evidence_contract(
    rows: list[Any],
) -> frozenset[str]:
    """Resolve the ACTIVE criteria the walkthrough_e2e probe can produce
    evidence for in the open version's rubric.

    A criterion is walkthrough-provable when its text names one of the flows the
    live harness exercises (semantic scenario match against
    ``WALKTHROUGH_E2E_FLOW_MARKERS``) or it carries one of the harness's legacy
    canonical keys (``WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS`` — a rubric that
    reuses the v26 criteria verbatim).  Only ACTIVE criteria count: a retracted
    or superseded criterion cannot earn evidence.  The request-time
    compatibility gate reads this resolved contract so it refuses the probe only
    when the open rubric genuinely has no criterion the live walkthrough could
    produce evidence for, instead of pinning the decision to a retired version's
    key set.
    """
    resolved: set[str] = set()
    for row in rows:
        if row.status != "active" or not row.criterion_key:
            continue
        text = str(row.text or "").lower()
        if any(
            marker in text
            for flow_markers in WALKTHROUGH_E2E_FLOW_MARKERS
            for marker in flow_markers
        ) or row.criterion_key in WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS:
            resolved.add(row.criterion_key)
    return frozenset(resolved)


async def _drop_incompatible_product_probes(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    requested_probes: frozenset[str],
) -> tuple[frozenset[str], dict[str, Any] | None]:
    """Drop product probes the open version's ACTIVE rubric cannot satisfy.

    Returns ``(effective_probes, drop_info)``.  ``effective_probes`` is the
    requested selection with every structurally-impossible probe removed;
    ``drop_info`` is None when nothing was dropped, else a dict naming the
    dropped probe, its declared evidence keys, and the open rubric's active
    keys.

    The walkthrough_e2e probe's evidence contract is resolved at request time
    against the OPEN version's ACTIVE criteria
    (:func:`_resolve_walkthrough_e2e_evidence_contract`) rather than a
    module-level constant naming one frozen rubric.  When the open rubric has
    no ACTIVE criterion the probe could produce live-stack evidence for, even a
    flawless run would still fail (``walkthrough_e2e_report_unknown_criteria``)
    only AFTER a full qualification was spent.  The request-time gate therefore
    DROPS the probe and lets the qualification continue with the remainder
    (probe-free when it was the only probe) instead of refusing the whole
    qualification — an incompatible probe must never silently prevent a
    qualification from spawning at all.  A rubric that re-mints its walkthrough
    criteria under fresh keys is NOT dropped: the resolved contract finds the
    live criteria by scenario text, so the strategist guidance that requests the
    probe to clear ``criterion_coverage_incomplete`` can actually take effect.
    """
    if "walkthrough_e2e" not in requested_probes:
        return requested_probes, None
    rows = await get_criteria_for_version(session, version.id)
    active_keys = _resolve_active_criterion_keys(rows)
    walkthrough_keys = _resolve_walkthrough_e2e_evidence_contract(rows)
    if walkthrough_keys:
        return requested_probes, None
    return (
        requested_probes - frozenset({"walkthrough_e2e"}),
        {
            "probe": "walkthrough_e2e",
            # Declared harness contract for observability only — the drop
            # decision above read the RESOLVED contract (empty for this rubric).
            "canonical_keys": sorted(
                _WALKTHROUGH_E2E_EXPECTED_CANONICAL_EVIDENCE_KEYS
            ),
            "active_keys": sorted(active_keys),
        },
    )


def _qualification_probe_contract_is_reusable(
    prior_qual: dict[str, Any], current_probes: frozenset[str]
) -> bool:
    """Whether a conclusive record satisfies the current probe contract."""
    try:
        prior_probes = _normalize_qualification_probes(
            prior_qual.get("requested_probes")
        )
    except ValueError:
        return False
    if prior_probes != current_probes:
        return False
    if "walkthrough_e2e" not in current_probes:
        return True
    result = prior_qual.get("walkthrough_e2e_result")
    candidate_sha = str(prior_qual.get("candidate_sha") or "").strip()
    if (
        isinstance(result, dict)
        and result.get("status") in {"passed", "failed"}
        and candidate_sha
        and result.get("candidate_sha") == candidate_sha
        and result.get("exercised_engine_sha") == candidate_sha
    ):
        return True
    # A walkthrough evidence-CONTRACT failure
    # (blocker="walkthrough_e2e_contract_failed") records a DETERMINISTIC
    # deployed-side verdict: the serving validator rejected the report's
    # structure or key resolution, and re-running the SAME candidate would
    # reproduce the same rejection (the validator is deployed, not re-rolled).
    # Treating the record as reusable lets run_qualification's cached-verdict
    # guard make a same-candidate re-drive a cheap no-op (no VM spawn, no
    # cap advance) while a candidate advance — a conforming fix landing on
    # automain — or a goal/rubric change (contract_fingerprint drift) still
    # spawns a fresh qualification.  The walkthrough-contract sweep re-driver
    # depends on this no-op for its unchanged-candidate sweep passes.
    return bool(
        prior_qual.get("status") == "not_promoted"
        and prior_qual.get("blocker") == "walkthrough_e2e_contract_failed"
    )

# Explicit enumeration of every dimension written by _dual_write_dimension_results.
# This constant is asserted == LEDGER_DIMENSION_KEYS at module load so any drift
# between the spec list and the registry is caught at import time (no DB needed),
# rather than only when _dual_write_dimension_results executes against a real session.
_DUAL_WRITE_SPEC_DIMENSIONS: frozenset[str] = frozenset({
    "ci",
    "audit",
    "test_erosion",

    "challenge",
    "migration_identity",
    "migration_apply",
    "merge_grade",
    "acceptance_window",
    "debt_pressure",
    "capability_proof",
    "env_preflight",
})
assert _DUAL_WRITE_SPEC_DIMENSIONS == LEDGER_DIMENSION_KEYS, (
    f"_DUAL_WRITE_SPEC_DIMENSIONS ({_DUAL_WRITE_SPEC_DIMENSIONS}) != "
    f"LEDGER_DIMENSION_KEYS ({LEDGER_DIMENSION_KEYS}); "
    f"extra: {_DUAL_WRITE_SPEC_DIMENSIONS - LEDGER_DIMENSION_KEYS}, "
    f"missing: {LEDGER_DIMENSION_KEYS - _DUAL_WRITE_SPEC_DIMENSIONS}"
)

_NEVER_GRADED_GAP = (
    "No merge-grade verdict exists for this candidate — the acceptance contract "
    "was never graded (a merge-grade runs only for a version with a usable, "
    "non-contentless goal)."
)

_RUBRIC_PROVENANCE_VERSION = 1
_MAX_RUBRIC_DIFF_KEYS = 20

def _canonicalize_rubric(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    canonical: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("criterion_key") or item.get("category_key") or "").strip()
        if not key or key in seen:
            continue
        text = " ".join(str(item.get("text") or "").split())
        entry: dict[str, Any] = {"criterion_key": key, "text": text}
        contract = normalize_evidence_contract(item.get("evidence_contract"))
        if contract is not None:
            entry["evidence_contract"] = contract
        canonical.append(entry)
        seen.add(key)
    return canonical

def _rubric_fingerprint(snapshot: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        _canonicalize_rubric(snapshot),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def _qualification_contract_fingerprint(
    version_goal: str,
    rubric_fingerprint: str,
    *,
    promotion_policy: dict[str, Any] | None = None,
    capability_proof_mode: str = "off",
    env_preflight_enabled: bool = False,
    requested_probes: Collection[str] | None = None,
) -> str:
    """Bind reusable goal evidence to the exact launch-time contract."""
    policy = promotion_policy or {}
    raw_weights = policy.get("weights")
    normalized_weights = {
        str(key): float(value)
        for key, value in sorted(
            (raw_weights.items() if isinstance(raw_weights, dict) else [])
        )
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    raw_threshold = policy.get("threshold")
    encoded = json.dumps(
        {
            "version_goal": " ".join(str(version_goal or "").split()),
            "rubric_fingerprint": str(rubric_fingerprint or ""),
            "promotion_policy": {
                "threshold": (
                    float(raw_threshold)
                    if isinstance(raw_threshold, (int, float))
                    and not isinstance(raw_threshold, bool)
                    else None
                ),
                "weights": normalized_weights,
            },
            "capability_proof_mode": str(capability_proof_mode or "off")
            .strip()
            .lower(),
            "env_preflight_enabled": bool(env_preflight_enabled),
            "requested_probes": sorted(
                {
                    str(probe).strip()
                    for probe in (requested_probes or [])
                    if str(probe).strip()
                }
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def _qualification_attempt_id(
    version: release_version.ReleaseVersion,
    qual: dict[str, Any] | None = None,
) -> str | None:
    raw = (qual or {}).get("qualification_attempt_id")
    if raw is None:
        raw = (version.last_qualification or {}).get("qualification_attempt_id")
    return str(raw) if raw not in (None, "") else None

def _rubric_provenance(
    snapshot: list[dict[str, str]],
    *,
    source: str,
    qualification_attempt_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": _RUBRIC_PROVENANCE_VERSION,
        "source": source,
        "fingerprint": _rubric_fingerprint(snapshot),
        "criteria": snapshot,
        "qualification_attempt_id": qualification_attempt_id,
    }

def _rubric_detail_provenance(detail: Any) -> dict[str, Any] | None:
    if not isinstance(detail, dict):
        return None
    nested = detail.get("rubric_provenance")
    if isinstance(nested, dict):
        return nested
    if isinstance(detail.get("fingerprint"), str) and isinstance(
        detail.get("criteria"), list
    ):
        return detail
    return None

def _rubric_dimension_reusable(
    detail: Any,
    *,
    fingerprint: str,
    qualification_attempt_id: str | None,
) -> bool:
    provenance = _rubric_detail_provenance(detail)
    if provenance is None or provenance.get("fingerprint") != fingerprint:
        return False
    # A rubric-bound fact proves a particular qualification attempt, not merely
    # a rubric's contents.  Older projections may not yet carry the first-class
    # attempt id; fail closed in that rolling state rather than letting a
    # same-SHA re-drive silently borrow a prior blind grade.
    stored_attempt_id = provenance.get("qualification_attempt_id")
    if qualification_attempt_id is None or stored_attempt_id in (None, ""):
        return False
    return stored_attempt_id == qualification_attempt_id

async def _snapshot_qualification_rubric(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows = await get_criteria_for_version(session, version.id)
    active = _canonicalize_rubric(
        [
            {
                "criterion_key": row.criterion_key,
                "text": row.text,
                "evidence_contract": row.evidence_contract,
            }
            for row in rows
            if row.status == "active"
        ]
    )
    lifecycle = [
        {
            "criterion_key": str(row.criterion_key),
            "status": str(row.status),
        }
        for row in rows
        if row.criterion_key
    ]
    return active, lifecycle

async def _resolve_launched_rubric(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    qual: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, str | None]:
    if isinstance(qual.get("rubric_snapshot"), list):
        snapshot = _canonicalize_rubric(qual["rubric_snapshot"])
        fingerprint = _rubric_fingerprint(snapshot)
        stored = qual.get("rubric_fingerprint")
        if stored not in (None, fingerprint):
            return None, None, "stored_rubric_fingerprint_mismatch"
        return (
            snapshot,
            _rubric_provenance(
                snapshot,
                source="attempt_projection",
                qualification_attempt_id=_qualification_attempt_id(version, qual),
            ),
            None,
        )

    task_id = qual.get("merge_grade_task_id")
    if task_id:
        try:
            task = await session.get(Task, uuid.UUID(str(task_id)))
        except (TypeError, ValueError, AttributeError):
            task = None
        payload = task.first_payload if task is not None else None
        if isinstance(payload, dict) and isinstance(payload.get("categories"), list):
            snapshot = _canonicalize_rubric(payload["categories"])
            return (
                snapshot,
                _rubric_provenance(
                    snapshot,
                    source="legacy_merge_grade_task_payload",
                    qualification_attempt_id=_qualification_attempt_id(version, qual),
                ),
                None,
            )
        return None, None, "merge_grade_task_payload_has_no_rubric"

    return None, None, "pending_attempt_has_no_rubric_snapshot_or_grade_payload"

def _bounded_rubric_drift(
    launched: list[dict[str, str]],
    live: list[dict[str, str]],
    launched_lifecycle: Any,
    live_lifecycle: list[dict[str, str]],
) -> dict[str, Any] | None:
    launched_by_key = {row["criterion_key"]: row["text"] for row in launched}
    live_by_key = {row["criterion_key"]: row["text"] for row in live}
    prior_status = {
        str(row.get("criterion_key")): str(row.get("status"))
        for row in (launched_lifecycle or [])
        if isinstance(row, dict) and row.get("criterion_key")
    }
    live_status = {
        str(row.get("criterion_key")): str(row.get("status"))
        for row in live_lifecycle
        if row.get("criterion_key")
    }
    added = [key for key in live_by_key if key not in launched_by_key]
    removed = [key for key in launched_by_key if key not in live_by_key]
    changed = [
        key
        for key in launched_by_key.keys() & live_by_key.keys()
        if launched_by_key[key] != live_by_key[key]
    ]
    if not added and not removed and not changed:
        return None
    changes: list[dict[str, str]] = []
    for key in added:
        kind = (
            "reinstated"
            if prior_status.get(key) in {"retracted", "superseded"}
            else "added"
        )
        changes.append({"criterion_key": key, "change": kind})
    for key in removed:
        status = live_status.get(key)
        kind = status if status in {"retracted", "superseded"} else "removed"
        changes.append({"criterion_key": key, "change": kind})
    changes.extend(
        {"criterion_key": key, "change": "changed"} for key in changed
    )
    bounded_changes = changes[:_MAX_RUBRIC_DIFF_KEYS]
    return {
        "added_keys": added[:_MAX_RUBRIC_DIFF_KEYS],
        "removed_keys": removed[:_MAX_RUBRIC_DIFF_KEYS],
        "changed_keys": changed[:_MAX_RUBRIC_DIFF_KEYS],
        "changes": bounded_changes,
        "total_changes": len(changes),
        "truncated": len(changes) > len(bounded_changes),
        "launched_fingerprint": _rubric_fingerprint(launched),
        "live_fingerprint": _rubric_fingerprint(live),
    }

def _never_graded_category_verdicts(active_keys: set[str]) -> list[dict[str, Any]]:
    """Fail-CLOSED verdicts for a candidate that was NEVER graded.

    ``compute_composite`` SKIPS the per-category gate when ``category_verdicts`` is
    ``None`` — that is deliberate (it preserves the no-criteria path).  But a version
    that HAS active acceptance criteria and simply never produced a merge-grade for
    this candidate would hit the same ``None`` and thus promote over an acceptance
    contract that was never checked at all.  That is reachable whenever no merge-grade
    ran — most notably a version whose ``goal`` is empty/contentless (see
    ``_merge_grade_goal_usable``), which spawns no merge-grade child.

    Synthesising ``not_met`` for every active key mirrors
    ``category_verdict_ledger.is_candidate_fully_met`` (an ACTIVE criterion with no
    verdict → NOT met), so the gate and the sweep can never disagree, and it carries
    the reason in ``gap`` rather than failing silently.
    """
    return [
        {
            "category_key": key,
            "status": "not_met",
            "tier": "judged",
            "gap": _NEVER_GRADED_GAP,
            "citations": [],
        }
        for key in sorted(k for k in active_keys if k)
    ]

def _coerce_ledger_category_verdict(v: Any) -> dict[str, Any]:
    """Coerce a category-verdict row read back from the ledger into a plain dict.

    ``compute_composite``'s goal-verification gate reads ``status``/``tier`` via
    ``dict.get``; the sibling ledger may hand back either dicts or ORM rows, so
    this normalises both to ``{category_key, status, tier}`` (the only fields the
    gate consumes).  Keeps the gate pure and shape-agnostic to the ledger's
    internal representation.
    """
    if isinstance(v, dict):
        return v
    return {
        "category_key": getattr(v, "category_key", None),
        "status": getattr(v, "status", None),
        "tier": getattr(v, "tier", "judged"),
        "gap": getattr(v, "gap", None),
        "grader_run_id": (
            str(getattr(v, "grader_run_id", ""))
            if getattr(v, "grader_run_id", None)
            else None
        ),
    }

def _fail_closed_category_verdicts(
    active_keys: set[str | None],
) -> list[dict[str, Any]] | None:
    """Build a fail-CLOSED per-category gate input from the active criterion keys.

    ``compute_composite``'s goal-verification gate BLOCKS when any category's
    ``status`` is not ``"met"`` and SKIPS entirely when handed ``None``.  When the
    per-category verdicts cannot be read back (ledger read raised, or returned no
    row for any active criterion), returning ``None`` would silently SKIP the gate
    and let an unverified candidate promote — the fail-open this closes.  Instead,
    synthesise one ``"unverified"`` verdict per active criterion so the gate blocks
    with ``criterion_coverage_incomplete`` (unverified != verified).  Returns
    ``None`` only when there are genuinely no active criterion keys to gate on.
    """
    verdicts = [
        {
            "category_key": k,
            "status": "unverified",
            "tier": "unverified",
        }
        for k in sorted(k for k in active_keys if k)
    ]
    return verdicts or None

# ---------------------------------------------------------------------------
# Policy — the trusted core.  weights + threshold come from a human-owned file.
# The hardcoded fallback rubric (_DEFAULT_POLICY) and the scoring math live in
# app.release_scoring; _load_policy stays here because it reads _POLICY_PATH
# (patched in tests via app.release_qualifier._POLICY_PATH) off the filesystem.
# ---------------------------------------------------------------------------
# promotion_policy.yaml lives at backend/promotion_policy.yaml — two parents up
# from this file (backend/app/release_qualifier.py).
_POLICY_PATH = Path(__file__).resolve().parent.parent / "promotion_policy.yaml"


def _finite_policy_float(value: Any, *, label: str) -> float:
    """Coerce one policy number while rejecting JSON-unsafe NaN/Infinity."""

    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _normalize_active_promotion_weights(weights_raw: Any) -> dict[str, float]:
    """Accept current two-key and legacy three-key policies deterministically."""
    from decimal import Decimal

    if not isinstance(weights_raw, dict):
        raise ValueError("promotion weights must be a mapping")
    audit = _finite_policy_float(weights_raw["audit"], label="audit weight")
    erosion = _finite_policy_float(
        weights_raw["test_erosion"], label="test_erosion weight"
    )
    smoke = _finite_policy_float(
        weights_raw.get("smoketest", 0.0), label="smoketest weight"
    )
    if audit < 0 or erosion < 0 or smoke < 0:
        raise ValueError("promotion weights must be non-negative")
    declared_sum = audit + erosion + smoke
    if abs(declared_sum - 1.0) > 0.01:
        raise ValueError(f"promotion weights sum to {declared_sum:.4f}, expected ~1.0")
    audit_decimal = Decimal(str(weights_raw["audit"]))
    erosion_decimal = Decimal(str(weights_raw["test_erosion"]))
    active_sum = audit_decimal + erosion_decimal
    if active_sum <= 0:
        raise ValueError("active promotion weights must be positive")
    result = {
        "audit": float(audit_decimal / active_sum),
        "test_erosion": float(erosion_decimal / active_sum),
    }
    debt = coerce_completion_debt_weight(weights_raw)
    if debt > 0.0:
        result["completion_debt"] = debt
    return result


def _policy_from_runtime_config() -> dict[str, Any] | None:
    """Return the live promotion rubric from runtime_config, or None.

    The threshold + weights are seeded into runtime_config from
    promotion_policy.yaml at startup (shared_state._seed_promotion_policy) and
    are admin-tunable via PATCH /api/admin/settings, so calibrating the
    promotion bar no longer needs a redeploy.  This reads them at decision time.

    Returns the validated ``{"weights": {...}, "threshold": float}`` shape when
    runtime_config holds a usable rubric, else ``None`` so ``_load_policy``
    falls back to re-reading the YAML file / hardcoded defaults. Current
    two-key and archival three-key rubrics are normalized through the same
    helper, so the retired smoke weight can never reach the active gate.
    """
    try:
        threshold_raw = _runtime_config.get("promotion_threshold")
        weights_raw = _runtime_config.get("promotion_weights")
        if threshold_raw is None or not isinstance(weights_raw, dict):
            return None
        weights = _normalize_active_promotion_weights(weights_raw)
        threshold = _finite_policy_float(
            threshold_raw, label="runtime promotion threshold"
        )
        result: dict[str, Any] = {"weights": weights, "threshold": threshold}
        for _k in ("merge_grade_block_threshold",):
            _v = _runtime_config.get(_k)
            if _v is not None:
                with contextlib.suppress(TypeError, ValueError):
                    result[_k] = _finite_policy_float(_v, label=_k)
        # Slice 3 (design 245212a4): dark-by-default deployed-side env-preflight
        # judge flag.  Threaded through the rubric so a fleet-level runtime_config
        # arm reaches _finalize_qualification's policy fallback.
        _epf = _runtime_config.get("env_preflight_enabled")
        if _epf is not None:
            result["env_preflight_enabled"] = bool(_epf)
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
            weights = _normalize_active_promotion_weights(weights_raw)
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
            _finite_policy_float(
                threshold_raw, label="project promotion threshold"
            )
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
# ``completion_debt_weight_mode`` is a real runtime_config key graduated to
# "enforce" (three-state adjudication, decision #7); only "enforce" lets the
# configured completion_debt weight reach the composite blend. An operator may
# still pin "off"/"observe" per deployment to roll back.
_COMPLETION_DEBT_WEIGHT_MODES: frozenset[str] = frozenset({"off", "observe", "enforce"})

def _resolve_completion_debt_weight_mode() -> str:
    """Return the live ``completion_debt_weight_mode`` (shipped default "enforce").

    Any unrecognised / malformed value degrades to the conservative "observe"
    fallback so a garbage knob can never silently arm the enforce blend — the
    intended "enforce" state must be an explicit (or the shipped-default) value,
    never a side effect of parsing junk.
    """
    mode = str(_runtime_config.get("completion_debt_weight_mode", "enforce")).strip().lower()
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


def current_effective_policy_projection(
    project: Project | None = None,
) -> dict[str, Any]:
    """Resolve and project the policy the qualifier would enforce right now."""

    policy, source, fallback_error = _resolve_policy_with_provenance(project)
    return promotion_policy_projection(
        policy,
        source=source,
        fallback_error=fallback_error,
    )
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

        # Normalize current two-key and archival three-key policies through one
        # path; KeyError/TypeError/ValueError here drops into the safe fallback.
        weights = _normalize_active_promotion_weights(weights_raw)
        threshold = _finite_policy_float(
            parsed["threshold"], label="YAML promotion threshold"
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
                    result[_k] = _finite_policy_float(parsed[_k], label=_k)
        # Slice 3 (design 245212a4): dark-by-default deployed-side env-preflight
        # judge flag from the on-disk rubric (default False when absent).
        if "env_preflight_enabled" in parsed:
            result["env_preflight_enabled"] = bool(parsed["env_preflight_enabled"])
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
    ``{"weights": {"audit": float, "test_erosion": float},
       "threshold": float}`` with every value coerced to float. An OPTIONAL
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

def _legacy_smoke_tainted_verdict(qual: dict[str, Any]) -> bool:
    """Return whether a conclusive verdict depended on retired qualifier smoke."""
    if qual.get("status") not in _CONCLUSIVE_VERDICT_STATUSES:
        return False
    sub_scores = qual.get("sub_scores")
    resolved = qual.get("resolved_dimensions")
    return bool(
        qual.get("smoketest_enabled") is True
        or (isinstance(sub_scores, dict) and "smoketest" in sub_scores)
        or (isinstance(resolved, dict) and "smoketest" in resolved)
        or qual.get("blocker") == "smoketest_startup_failed"
    )


def _legacy_smoke_infra_lineage(qual: dict[str, Any]) -> bool:
    def _row_is_smoke(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        if str(row.get("leg") or row.get("failing_leg") or "").lower() == "smoketest":
            return True
        signature = str(row.get("signature") or row.get("error") or "").lower()
        if signature.startswith("smoketest"):
            return True
        children = row.get("child_failures")
        return isinstance(children, list) and any(_row_is_smoke(child) for child in children)

    failing_dimension = str(qual.get("failing_dimension") or "").lower()
    if failing_dimension:
        return failing_dimension == "smoketest"
    direct = str(qual.get("infra_failure_signature") or qual.get("error") or "").lower()
    if direct.startswith("smoketest"):
        return True
    history = qual.get("infra_attempt_history")
    if isinstance(history, list) and history:
        # The final row is the current attempt. Earlier smoke failures must not
        # erase retry/backoff for a newer audit or challenge failure.
        return _row_is_smoke(history[-1])
    events = qual.get("infra_failure_child_events")
    return bool(
        isinstance(events, list)
        and events
        and all(_row_is_smoke(row) for row in events)
    )


async def _qualification_in_flight(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Return whether an active non-smoke qualifier child is still running."""
    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        return False
    required_keys = ["audit_task_id"]
    if qual.get("challenge_task_id"):
        required_keys.append("challenge_task_id")
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
    # The walkthrough_e2e probe is optional and decision-bearing: a MISSING,
    # unparseable, or deleted child is NOT in-flight work — the finalize path
    # holds an absent requested probe as an infra failure (walkthrough_e2e_task_
    # missing), so there is nothing live left to wait for.  Only a present,
    # parseable child id joins the required set (a live running probe still
    # blocks re-entry); a deleted walkthrough row is skipped in the missing-row
    # check below for the same reason.
    walkthrough_child_id: uuid.UUID | None = None
    if "walkthrough_e2e" in set(qual.get("requested_probes") or []):
        _wt_raw = qual.get("walkthrough_e2e_task_id")
        if _wt_raw:
            try:
                walkthrough_child_id = uuid.UUID(str(_wt_raw))
                child_ids.append(walkthrough_child_id)
            except (ValueError, AttributeError):
                pass
    if parse_failed:
        return True
    rows = (
        await session.execute(select(Task.id, Task.status).where(Task.id.in_(child_ids)))
    ).all()
    found_ids = {row.id for row in rows}
    for task_id in child_ids:
        if task_id == walkthrough_child_id:
            # A deleted walkthrough row cannot be in-flight work.
            continue
        if task_id not in found_ids:
            return True
    return any(row.status not in _TERMINAL_STATUSES for row in rows)

def _promotion_pending_result(
    version: release_version.ReleaseVersion,
    *,
    latest_verdict: Any = None,
) -> dict[str, Any] | None:
    """Return a promotion-blocking status dict, or None when not mid-promotion.

    The sole blocking sub-state is ``promotion_check_wait``: Blueprint owns the
    persisted promotion PR and retries its exact target after required checks pass.
    Re-qualifying while that state is active would overwrite the durable
    promotion-pending provenance, so every operator path uses this same guard.
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
            "status": qual_state["state"],
            "promotion_pr_number": qual_state.get("promotion_pr_number"),
            "promotion_pr_url": qual_state.get("promotion_pr_url"),
        }
        for key in release_version._PROMOTION_FRESHNESS_KEYS:
            if key in qual_state:
                result[key] = qual_state[key]
        return result
    return None


def _coerce_qualification_attempt_id(value: Any) -> uuid.UUID | None:
    """Return a UUID attempt identity, accepting legacy string projections."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _qualification_attempt_for_record(
    version: release_version.ReleaseVersion,
    record: dict[str, Any],
) -> uuid.UUID:
    """Resolve identity, deriving a stable UUID5 for partial legacy backfills."""
    explicit = _coerce_qualification_attempt_id(
        record.get("qualification_attempt_id")
    ) or _coerce_qualification_attempt_id(
        getattr(version, "active_qualification_attempt_id", None)
    )
    if explicit is not None:
        return explicit
    legacy_key = ":".join(
        (
            str(version.id),
            str(record.get("candidate_sha") or "unknown"),
            str(record.get("started_at") or "unknown"),
        )
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, f"blueprint:qualification:{legacy_key}")

async def _record_attempt_projection_atomic(
    session: AsyncSession,
    version_id: uuid.UUID,
    *,
    attempt_id: uuid.UUID,
    retry_episode: int,
    is_new_attempt: bool,
) -> None:
    """Atomically publish identity and distinct lifetime/retry accounting."""
    values: dict[str, Any] = {
        "active_qualification_attempt_id": attempt_id,
        "qualification_retry_episode": max(0, int(retry_episode)),
    }
    if is_new_attempt:
        values["qualification_attempts"] = (
            sa_func.coalesce(release_version.ReleaseVersion.qualification_attempts, 0)
            + 1
        )
    await session.execute(
        update(release_version.ReleaseVersion)
        .where(release_version.ReleaseVersion.id == version_id)
        .values(**values)
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


def _ci_dimension_status(
    *,
    ci_green: bool,
    ci_resolution: dict[str, Any],
    ci_required: bool,
) -> str:
    """Map the effective CI policy to the persisted dimension status.

    A topology-safe ``no_checks`` result is a deliberate non-blocking outcome,
    not a failed or unresolved CI result.  Keep this mapping beside the
    finalize policy so the eager and comprehensive ledger writers cannot drift
    from ``compute_composite``'s blocker semantics.
    """
    if ci_green or (
        not ci_required and ci_resolution.get("state") == _CI_NO_CHECKS
    ):
        return "passed"
    return "blocked"

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
    requested_probes: frozenset[str] = frozenset(),
    record_deferral: bool = True,
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
        # Return a pure notification intent.  The caller publishes it only
        # after the exact Project -> Version authority CAS and only if this
        # request actually reaches launch, so a superseded GitHub observation
        # cannot leak a stale durable operator message.
        return {
            "_proceed_after_sync_fall_through": True,
            "_notification_intent": {
                "event": "qualify_unsynced_fall_through",
                "detail": (
                    f"v{version.version_number}: {integration_branch} has not "
                    f"absorbed {promotion_target} for {waited_min:.0f}min, so "
                    "the qualify-sync gate's fall-through fired — scoring the "
                    f"candidate anyway. Check the {promotion_target} -> "
                    f"{integration_branch} back-merge."
                ),
                "extra": {
                    "candidate_sha": candidate_sha,
                    "waited_minutes": round(waited_min),
                    "fall_through_minutes": fall_through_min,
                },
            },
        }

    # Defer (retryable — the re-emitted qualify_request / cadence re-enters).
    deferral_record = {
        "status": "deferred_unsynced",
        "candidate_sha": candidate_sha,
        "requested_probes": sorted(requested_probes),
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
    if record_deferral:
        version.last_qualification = deferral_record
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
    return {
        "status": "deferred_unsynced",
        "candidate_sha": candidate_sha,
        "requested_probes": sorted(requested_probes),
        **(
            {"_deferral_record": deferral_record}
            if not record_deferral
            else {}
        ),
    }


async def run_qualification(
    session: AsyncSession,
    project_id: uuid.UUID,
    version: release_version.ReleaseVersion,
    *,
    session_factory=None,
    allow_synchronous_reuse_promote: bool = False,
    qualification_attempt_id: uuid.UUID | str | None = None,
    expected_phase_changed_at: datetime | None = None,
    expected_authority_fingerprint: str | None = None,
    exact_authority: bool | None = None,
    retry_episode: int = 0,
    retry_episode_cap: int | None = None,
    requested_probes: list[str] | tuple[str, ...] | set[str] | None = None,
    explicit_request: bool = False,
    auto_trigger: bool = False,
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
    pass the in-flight guard and each spawn duplicate expensive children (
    with one orphaned set) and clobber each other's
    ``last_qualification`` write.  So this function takes its OWN
    ``SELECT ... FOR UPDATE`` on the ``ReleaseVersion`` row and holds it across
    the in-flight check, the child spawns, and the ``last_qualification``
    write — that ReleaseVersion lock (not the strategic path's Project lock) is
    what serializes the race.  The GitHub I/O that resolves ``candidate_sha``
    runs BEFORE the lock to keep the locked window short; the bounded rehearsal
    dispatch is the intentional exception because it must be serialized by the
    version lock.

    Steps (none of which block on the spawned children):
      1. Resolve the project's owner/repo/GitHub token.
      2. Snapshot the candidate SHA: ``automain`` HEAD (full 40-char SHA).
      3. Check CI on that SHA via ``get_check_runs_cached`` (cheap, orchestrator
         side).  ``ci_green`` is recorded but not acted on here — the composite
         in ``_finalize_qualification`` applies it as a hard blocker.
      4. Lock the ``ReleaseVersion`` row, then re-check the in-flight /
         cached-verdict guards under the lock and ensure the candidate rehearsal
         is dispatched exactly once.
      5. Spawn one holistic final review, plus only the product probes
         explicitly selected by the qualification requester. Their
         payloads carry the exact ``candidate_sha`` so every gate evaluates the
         snapshotted tree rather than a later integration-branch head.
      6. Stamp ``version.last_qualification = {status: "pending", ...}`` and
         bump ``version.qualification_attempts``.

    Returns a status dict.  ``status`` is ``"spawned"`` on success;
    ``"wrong_phase"`` / ``"already_pending"`` / ``"cached_verdict"`` when a
    guard fired (no side effects); or an error code (``"project_not_found"``,
    ``"no_credentials"``, ``"sha_error"``, ``"spawn_failed"``).  The caller
    commits the session.
    """
    continued_attempt_id = _coerce_qualification_attempt_id(
        qualification_attempt_id
    )
    attempt_id = continued_attempt_id or uuid.uuid4()
    is_new_attempt = continued_attempt_id is None
    retry_episode = max(0, int(retry_episode or 0))
    prior_qual = version.last_qualification or {}
    if exact_authority is None:
        # Preserve exact semantics for older callers that supplied a concrete
        # epoch, while allowing a durable intent to explicitly bind the valid
        # NULL bootstrap epoch.
        exact_authority = (
            expected_phase_changed_at is not None
            or expected_authority_fingerprint is not None
        )
    authority_phase_changed_at = (
        expected_phase_changed_at
        if exact_authority
        else version.phase_changed_at
    )
    authority_fingerprint = (
        expected_authority_fingerprint
        or qualification_authority_fingerprint(version)
    )
    raw_requested_probes: Any = requested_probes
    if raw_requested_probes is None and continued_attempt_id is not None:
        raw_requested_probes = prior_qual.get("requested_probes")
    try:
        effective_requested_probes = _normalize_qualification_probes(
            raw_requested_probes
        )
    except ValueError as exc:
        logger.warning(
            "[RELEASE_QUALIFIER] refusing invalid product-probe selection for "
            "v%d: %s",
            version.version_number,
            exc,
        )
        return {
            "status": "invalid_requested_probes",
            "detail": str(exc)[:200],
        }
    _requested_probes_before_compatibility = effective_requested_probes

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

    # Fresh qualification is an ownership decision, not a branch-head side
    # effect.  Exactly two sanctioned sources may START a fresh review, and each
    # is distinct so the guard keeps meaning:
    #
    #   * ``explicit_request=True`` — Strategic's qualify_request and the
    #     operator endpoint: a human (or a caller acting for one) decided this
    #     candidate deserves a review right now.
    #   * ``auto_trigger=True`` — the sweep's sanctioned auto-trigger: the
    #     trigger resolver (``qualification_triggers.resolve_qualification_trigger``)
    #     fired a bounded rule (initial fire, rubric redrive, candidate/dry-run
    #     advance, acceptance-grade re-grade) gated behind the
    #     ``auto_qualification_triggers`` knob.  The sweep must NOT masquerade
    #     as an explicit caller — it is automatic machinery with its own,
    #     narrower authority.
    #
    # Any other automatic caller — the bespoke error/infra re-drivers, stale
    # reconcilers — may only CONTINUE an already-requested attempt by carrying
    # its immutable attempt id; they never start a fresh review.
    if (
        not explicit_request
        and not auto_trigger
        and continued_attempt_id is None
    ):
        logger.info(
            "[RELEASE_QUALIFIER] v%d ignored automatic fresh qualification; "
            "Strategic or an operator must request a candidate review",
            version.version_number,
        )
        return {"status": "explicit_request_required"}

    # Drop a product probe the open version's ACTIVE rubric cannot satisfy
    # BEFORE any qualification work (credential resolution, candidate SHA
    # resolution, locking, or child spawn) is spent.  The walkthrough_e2e
    # probe's canonical evidence keys are pinned to criteria the open rubric
    # must actually carry; when none of them is active the probe is structurally
    # impossible and would only fail (contained, but expensive) after a full
    # qualification.  Dropping it and continuing with the remainder — probe-free
    # when it was the only probe — replaces both the old silent late blackout
    # AND the request-time refusal that blocked the whole qualification.  The
    # drop is recorded durably on the spawned record under the existing
    # ``qualify_request_blocked`` / ``qualify_request_blocked_at`` keys so the
    # strategist can see why the requested probe never ran.
    effective_requested_probes, _probe_drop = (
        await _drop_incompatible_product_probes(
            session, version, effective_requested_probes
        )
    )
    _probe_drop_record: dict[str, Any] | None = None
    _probe_drop_blocked_at: str | None = None
    if _probe_drop is not None:
        _probe_drop_record = {
            "blocker": "incompatible_probe",
            "probe": _probe_drop["probe"],
            "canonical_evidence_keys": _probe_drop["canonical_keys"],
            "active_rubric_keys": _probe_drop["active_keys"],
        }
        _probe_drop_blocked_at = _utcnow().isoformat()
        logger.info(
            "[RELEASE_QUALIFIER] v%d dropped incompatible product probe '%s' "
            "before spawning: canonical evidence keys %s share no ACTIVE "
            "criterion in the open rubric (active keys %s) — continuing "
            "probe-free with %s",
            version.version_number,
            _probe_drop["probe"],
            _probe_drop["canonical_keys"],
            _probe_drop["active_keys"],
            sorted(effective_requested_probes),
        )

    # Promotion policy is a cheap capability fact.  A disabled project must not
    # burn a release audit merely to rediscover that delivery is unavailable.
    # Lock the version before recording that blocker: an already-running
    # qualification or managed promotion owns last_qualification and must never
    # be overwritten by a concurrent configuration observation.
    if not resolve_project_promotion_enabled(project_id):
        locked_version, authority_block = (
            await _lock_exact_qualification_authority(
                session,
                project_id=project_id,
                version_id=version.id,
                version_number=version.version_number,
                expected_phase_changed_at=authority_phase_changed_at,
                expected_authority_fingerprint=authority_fingerprint,
            )
        )
        if authority_block is not None:
            return authority_block
        version = locked_version
        if version.status != "open":
            return {"status": "version_not_open"}
        if version.phase != "stabilize":
            return {"status": "wrong_phase"}
        if await _qualification_in_flight(session, version):
            return {"status": "already_pending"}
        from app.qualification_state_resolver import resolve_newest_qualification_verdict

        latest_verdict = await resolve_newest_qualification_verdict(session, version)
        promotion_pending = _promotion_pending_result(
            version, latest_verdict=latest_verdict
        )
        if promotion_pending is not None:
            return promotion_pending
        blocked_at = _utcnow().isoformat()
        version.last_qualification = {
            **(version.last_qualification or {}),
            "status": "external_blocker",
            "blocker": "promotion_disabled",
            "blocker_class": "external_delivery",
            "detail": (
                "Promotion is disabled for this project; qualification was not "
                "started and no review agents were spawned."
            ),
            "requested_at": blocked_at,
        }
        flag_modified(version, "last_qualification")
        session.add(version)
        return {
            "status": "external_blocker",
            "blocker": "promotion_disabled",
            "blocker_class": "external_delivery",
        }

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

    # Cheap pre-lock promotion-pending check (no I/O beyond verdict fetch).
    # A version whose audit passed and now has a managed promotion pending
    # must not be re-qualified: doing so spawns fresh expensive qualifier
    # children and clobbers the promotion-pending record.  Consistent with the
    # version-cut lever, which refuses in the same state.  Best-effort fast path
    # — the authoritative re-check happens under the row lock below.
    # Fetch the newest verdict so stale-pending+finalized-verdict is not
    # misclassified as still-blocking.
    from app.qualification_state_resolver import resolve_newest_qualification_verdict

    _latest_verdict_prelock = await resolve_newest_qualification_verdict(
        session, version
    )
    _promo = _promotion_pending_result(version, latest_verdict=_latest_verdict_prelock)
    if _promo is not None:
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d has a promotion "
            "pending — skipping (promotion_check_wait, pre-lock)",
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
    if project.paused:
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: project %s is paused — "
            "qualification spawning remains stopped",
            project_id,
        )
        return {"status": "project_paused"}
    project_observation_fingerprint = (
        qualification_project_observation_fingerprint(project)
    )

    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        version, authority_block = await _lock_exact_qualification_authority(
            session,
            project_id=project_id,
            version_id=version.id,
            version_number=version.version_number,
            expected_phase_changed_at=authority_phase_changed_at,
            expected_authority_fingerprint=authority_fingerprint,
            expected_project_observation_fingerprint=(
                project_observation_fingerprint
            ),
        )
        if authority_block is not None:
            return authority_block
        version.last_qualification = {
            "status": "error",
            "error": "no_credentials",
            "started_at": _utcnow().isoformat(),
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": retry_episode,
            "retry_episode_cap": retry_episode_cap,
            "requested_probes": sorted(effective_requested_probes),
        }
        flag_modified(version, "last_qualification")
        session.add(version)
        # Audit R4 MEDIUM: increment the counter with an atomic SQL UPDATE
        # rather than a read-modify-write.  This early-return path runs BEFORE
        # the ReleaseVersion FOR UPDATE lock, so two concurrent callers
        # (strategic qualify_request + admin POST /qualify) could otherwise
        # both read the same attempts value and write back the same +1,
        # losing one increment.
        await _record_attempt_projection_atomic(
            session,
            version.id,
            attempt_id=attempt_id,
            retry_episode=retry_episode,
            is_new_attempt=is_new_attempt,
        )
        return {
            "status": "no_credentials",
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": retry_episode,
            "requested_probes": sorted(effective_requested_probes),
        }
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
        version, authority_block = await _lock_exact_qualification_authority(
            session,
            project_id=project_id,
            version_id=version.id,
            version_number=version.version_number,
            expected_phase_changed_at=authority_phase_changed_at,
            expected_authority_fingerprint=authority_fingerprint,
            expected_project_observation_fingerprint=(
                project_observation_fingerprint
            ),
        )
        if authority_block is not None:
            return authority_block
        version.last_qualification = {
            "status": "error",
            "error": "sha_error",
            "detail": exception_reason(exc),
            "started_at": _utcnow().isoformat(),
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": retry_episode,
            "retry_episode_cap": retry_episode_cap,
            "requested_probes": sorted(effective_requested_probes),
        }
        flag_modified(version, "last_qualification")
        session.add(version)
        # Audit R4 MEDIUM: atomic SQL increment — see the no_credentials path
        # above.  This early return is also pre-lock, so a read-modify-write
        # would be vulnerable to a lost update under concurrent callers.
        await _record_attempt_projection_atomic(
            session,
            version.id,
            attempt_id=attempt_id,
            retry_episode=retry_episode,
            is_new_attempt=is_new_attempt,
        )
        return {
            "status": "sha_error",
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": retry_episode,
            "requested_probes": sorted(effective_requested_probes),
        }

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
        and not _legacy_smoke_tainted_verdict(_pre_lock_qual)
        and _qualification_probe_contract_is_reusable(
            _pre_lock_qual, effective_requested_probes
        )
    )
    candidate_tree_entries = (
        None
        if _same_sha_conclusive
        else await github.list_repo_tree_paths_at_ref(
            token, owner, repo, candidate_sha, project_id=str(project_id)
        )
    )
    candidate_audited_surface = audited_surface_path_map(candidate_tree_entries)
    candidate_diff_hash = compute_audited_surface_hash(candidate_tree_entries)

    # All candidate GitHub content evidence is gathered before the authoritative
    # ReleaseVersion lock. The locked section below consumes only these immutable
    # candidate-bound values and rechecks phase/in-flight/dispatch intent before
    # spawning or writing.
    try:
        _pre_mi_verdict, _pre_mi_detail = await check_qualification_migration_identity(
            token,
            owner,
            repo,
            candidate_sha,
            promotion_target,
            project_id=str(project_id),
        )
    except Exception as exc:  # noqa: BLE001
        _pre_mi_verdict = "defer"
        _pre_mi_detail = f"migration-identity pre-spawn check error: {exc}"

    env_preflight_evidence: dict[str, Any] | None = None
    try:
        env_preflight_evidence = await _build_env_preflight_evidence_for_candidate(
            project, token, owner, repo, candidate_sha
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[RELEASE_QUALIFIER] failed to build env-preflight evidence for "
            "v%d candidate %s — evidence unavailable",
            version.version_number,
            candidate_sha[:8],
            exc_info=True,
        )

    # 3b. Qualify-on-synced gate (LTS split only).  Don't score a candidate that
    #     is behind the promotion target — it would fail the promotion merge and
    #     bounce the version back to stabilize, burning a full re-audit.  Bounded,
    #     degrade-safe, and never drops the request (see _check_lts_sync_gate).
    #     Runs pre-lock (read-only GitHub I/O) like the candidate snapshot + CI
    #     check above, so no row lock is held across the compare.
    _sync_fall_through_notification: dict[str, Any] | None = None
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
            requested_probes=effective_requested_probes,
            record_deferral=False,
        )
        if _sync_gate is not None and _sync_gate.get(
            "_proceed_after_sync_fall_through"
        ):
            _intent = _sync_gate.get("_notification_intent")
            if isinstance(_intent, dict):
                _sync_fall_through_notification = _intent
            _sync_gate = None
        if _sync_gate is not None:
            deferral_record = _sync_gate.pop("_deferral_record", None)
            version, authority_block = (
                await _lock_exact_qualification_authority(
                    session,
                    project_id=project_id,
                    version_id=version.id,
                    version_number=version.version_number,
                    expected_phase_changed_at=authority_phase_changed_at,
                    expected_authority_fingerprint=authority_fingerprint,
                    expected_project_observation_fingerprint=(
                        project_observation_fingerprint
                    ),
                )
            )
            if authority_block is not None:
                return authority_block
            if isinstance(deferral_record, dict):
                version.last_qualification = deferral_record
                flag_modified(version, "last_qualification")
                session.add(version)
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
    #     candidate_diff_hash is truthy AND == the prior verdict's hash.
    #     Lock/session discipline (critical): the promote runs in a DEDICATED
    #     AsyncSessionLocal session, NEVER this caller's — otherwise promote()'s
    #     intermediate commit (which it issues to free the row lock before its
    #     30-120s of GitHub I/O) would prematurely commit the caller's unrelated
    #     dirty work.  This runs BEFORE this function's own FOR UPDATE lock (step
    #     4 below): the dedicated session must be free to SELECT..FOR UPDATE the
    #     version row, which this caller has NOT yet locked. A capped record is
    #     escalated below; otherwise the dedicated retry is used.
    if (_pre_lock_qual.get("status") == "promotion_failed" and
            int(_pre_lock_qual.get("promotion_failure_count") or 0) >=
            _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS):
        if exact_authority:
            version, authority_block = await _lock_exact_qualification_authority(
                session,
                project_id=project_id,
                version_id=version.id,
                version_number=version.version_number,
                expected_phase_changed_at=expected_phase_changed_at,
                expected_authority_fingerprint=expected_authority_fingerprint,
            )
            if authority_block is not None:
                return authority_block
        else:
            from app.release_qualifier_redrive import _escalate_promotion_failure_loop_in_dedicated_session
            await _escalate_promotion_failure_loop_in_dedicated_session(version.id, session_factory=session_factory)
        return {"status": "promotion_failed_requalify_cap", "candidate_sha": candidate_sha}

    if (
        not effective_requested_probes
        and _qualification_probe_contract_is_reusable(
            _pre_lock_qual, effective_requested_probes
        )
        and _promotion_failed_reuse_eligible(_pre_lock_qual, candidate_diff_hash)
        and int(_pre_lock_qual.get("promotion_failure_count") or 0)
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
            if exact_authority:
                version, authority_block = await _lock_exact_qualification_authority(
                    session,
                    project_id=project_id,
                    version_id=version.id,
                    version_number=version.version_number,
                    expected_phase_changed_at=expected_phase_changed_at,
                    expected_authority_fingerprint=expected_authority_fingerprint,
                )
                if authority_block is not None:
                    return authority_block
            return {
                "status": "promotion_failed_reuse_deferred",
                "candidate_sha": candidate_sha,
            }
        _reuse_result = await _reattempt_promotion_failed_in_dedicated_session(
            version.id,
            candidate_sha,
            candidate_diff_hash,
            session_factory=session_factory,
            expected_phase_changed_at=authority_phase_changed_at,
            expected_authority_fingerprint=authority_fingerprint,
            expected_project_observation_fingerprint=(
                project_observation_fingerprint
            ),
            enforce_phase_epoch=True,
        )
        if _reuse_result is not None:
            if _reuse_result.get("status") in {
                "superseded_phase_epoch",
                "superseded_qualification_state",
                "superseded_project_observation_state",
            }:
                return _reuse_result
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

    # Candidate authorship can require GitHub ancestry/history requests.  Build
    # the immutable receipt before taking Project/ReleaseVersion authority;
    # the relock below fences both the source version and repository identity
    # before any child or qualification projection consumes it.
    (
        _prelock_audit_authorship_receipt,
        _prelock_audit_authorship_range,
    ) = await _measured_authorship_receipt_for_audit_spawn(
        session,
        version,
        candidate_sha_override=candidate_sha,
    )

    # 4. Project -> ReleaseVersion serialization prevents duplicate children;
    #    populate_existing also defeats a caller's stale cached version row.
    locked_version, authority_block = await _lock_exact_qualification_authority(
        session,
        project_id=project_id,
        version_id=version.id,
        version_number=version.version_number,
        expected_phase_changed_at=authority_phase_changed_at,
        expected_authority_fingerprint=authority_fingerprint,
        expected_project_observation_fingerprint=project_observation_fingerprint,
    )
    if authority_block is not None:
        return authority_block
    # From here on, mutate the freshly-locked row (its attributes are the
    # post-lock DB truth) rather than the caller's possibly-stale instance.
    version = locked_version

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

    # Guard 2b (under the lock): a promotion is pending — the audit PASSED and
    # a managed promotion owns the PR (last_qualification.status ==
    # "pending" + a promotion_pending sub-key, children already terminal).  This
    # is NOT "in flight" by the child-task definition above, and "pending" is not
    # a conclusive verdict, so neither Guard 2 nor Guard 3 catches it.  Spawning
    # fresh qualifier children here and overwriting the promotion-pending
    # record would discard the provenance the rest of the system relies on to
    # know the promotion is still legitimately pending — so refuse (no-op),
    # consistent with the version-cut lever.
    # Re-fetch the verdict under the lock (the pre-lock fetch may be stale if a
    # concurrent promotion finalized between the pre-lock check and here).
    _latest_verdict_locked = await resolve_newest_qualification_verdict(
        session, version
    )
    _promo = _promotion_pending_result(version, latest_verdict=_latest_verdict_locked)
    if _promo is not None:
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: version v%d has a promotion "
            "pending — skipping (promotion_check_wait)",
            version.version_number,
        )
        return _promo

    # The pre-lock probe check is only a cost-saving hint.  Criteria may change
    # while repository/provider observations run, so recompute the decision
    # from the rubric snapshot protected by the qualification authority lock.
    # A continued attempt inherits its selection from the freshly locked
    # projection; a fresh request reuses the strictly normalized input.
    prior_qual = version.last_qualification or {}
    _authoritative_probe_request = _requested_probes_before_compatibility
    if requested_probes is None and continued_attempt_id is not None:
        try:
            _authoritative_probe_request = _normalize_qualification_probes(
                prior_qual.get("requested_probes")
            )
        except ValueError as exc:
            return {
                "status": "invalid_requested_probes",
                "detail": str(exc)[:200],
            }
    effective_requested_probes, _probe_drop = (
        await _drop_incompatible_product_probes(
            session,
            version,
            _authoritative_probe_request,
        )
    )
    _probe_drop_record = None
    _probe_drop_blocked_at = None
    if _probe_drop is not None:
        _probe_drop_record = {
            "blocker": "incompatible_probe",
            "probe": _probe_drop["probe"],
            "canonical_evidence_keys": _probe_drop["canonical_keys"],
            "active_rubric_keys": _probe_drop["active_keys"],
        }
        _probe_drop_blocked_at = _utcnow().isoformat()

    # Stamp the candidate this attempt actually evaluates.  ``candidate_sha``
    # is re-resolved live from integration-branch HEAD on EVERY call (see the
    # snapshot above), including a CONTINUED attempt, and every other consumer
    # in this run — the diff range, the dimension rows, and
    # ``last_qualification['candidate_sha']`` — uses that fresh value.  Pinning
    # the column only on a new attempt would let a continued attempt whose
    # candidate advanced measure base..STALE_head in the hard authorship gate
    # while the promotion candidate is base..NEW_head, so the write is
    # unconditional and no gate can disagree with another.
    #
    # It is written AFTER the skip guards above (wrong_phase / already_pending
    # / promotion_check_wait) on purpose: those paths do not evaluate this
    # candidate at all, and re-pointing the column on a promotion_check_wait
    # would move the authorship range out from under an already-pending
    # promotion.
    version.candidate_sha = candidate_sha
    session.add(version)

    active_criteria = [
        row
        for row in await get_criteria_for_version(session, version.id)
        if row.status == "active"
    ]
    from app.release_prepromotion_rehearsal import (  # noqa: PLC0415
        normalize_rehearsal_evidence_contract,
        prepare_rehearsal_dispatch_intent,
    )

    rehearsal_required = any(
        normalize_rehearsal_evidence_contract(row.evidence_contract) is not None
        for row in active_criteria
    )
    rehearsal_dispatch_ok = True
    rehearsal_dispatch: dict[str, Any] | None = None
    if rehearsal_required:
        rehearsal_dispatch_ok, rehearsal_dispatch = (
            await prepare_rehearsal_dispatch_intent(session, version, candidate_sha)
        )
    if rehearsal_required and not rehearsal_dispatch_ok:
        version.last_qualification = {
            **(version.last_qualification or {}),
            "status": "rehearsal_dispatch_pending",
            "blocker": "prepromotion_rehearsal_dispatch_pending",
            "candidate_sha": candidate_sha,
            "prepromotion_rehearsal_dispatch": rehearsal_dispatch,
            "detail": "durable rehearsal outbox intent awaits dispatch/reconciliation",
            "started_at": _utcnow().isoformat(),
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": retry_episode,
            "retry_episode_cap": retry_episode_cap,
            "requested_probes": sorted(effective_requested_probes),
        }
        flag_modified(version, "last_qualification")
        session.add(version)
        return {
            "status": "rehearsal_dispatch_pending",
            "blocker": "prepromotion_rehearsal_dispatch_pending",
            "candidate_sha": candidate_sha,
            "dispatch": rehearsal_dispatch,
        }

    # Snapshot the merge-state signal under the version lock that owns the
    # qualification launch; promotion scoring stays unchanged.
    stranded_approved_prs = await snapshot_stranded_approved_prs(
        session, version.project_id
    )

    # Snapshot the live decision contract while the version row is locked.
    # Candidate code facts may remain reusable across goal edits, but a cached
    # holistic verdict is valid only for this exact goal + rubric pair.
    version_goal = (version.goal or "").strip()
    _merge_grade_goal_usable = bool(version_goal) and not is_contentless_goal(
        version_goal
    )
    rubric_snapshot, rubric_lifecycle_snapshot = (
        await _snapshot_qualification_rubric(session, version)
    )
    rubric_provenance = _rubric_provenance(
        rubric_snapshot,
        source="qualification_launch",
        qualification_attempt_id=str(attempt_id),
    )
    rubric_fingerprint = rubric_provenance["fingerprint"]
    (
        _spawn_policy_snapshot,
        _spawn_policy_source,
        _spawn_policy_fallback_error,
    ) = _resolve_policy_with_provenance(project)
    _spawn_policy_projection = promotion_policy_projection(
        _spawn_policy_snapshot,
        source=_spawn_policy_source,
        fallback_error=_spawn_policy_fallback_error,
    )
    _spawn_capability_proof_mode = str(
        _runtime_config.get("capability_proof_gate_mode", "off") or "off"
    ).strip().lower()
    _env_preflight_build = (project.build_config or {}).get(
        "env_preflight_enabled"
    )
    if _env_preflight_build is not None:
        env_preflight_enabled = bool(_env_preflight_build)
    elif _runtime_config.get("env_preflight_enabled") is not None:
        env_preflight_enabled = bool(_runtime_config.get("env_preflight_enabled"))
    else:
        env_preflight_enabled = bool(
            _spawn_policy_snapshot.get("env_preflight_enabled", False)
        )
    contract_fingerprint = _qualification_contract_fingerprint(
        version_goal,
        rubric_fingerprint,
        promotion_policy=_spawn_policy_snapshot,
        capability_proof_mode=_spawn_capability_proof_mode,
        env_preflight_enabled=env_preflight_enabled,
        requested_probes=effective_requested_probes,
    )

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
    # Product probes are decision-selected, not globally armed. A retry of the
    # same qualification attempt inherits the frozen selection; a new request
    # with no selection runs no product probe. The selection was strictly
    # normalized before any GitHub I/O above.
    if continued_attempt_id is not None:
        projected_attempt_id = _coerce_qualification_attempt_id(
            prior_qual.get("qualification_attempt_id")
        ) or _coerce_qualification_attempt_id(
            version.active_qualification_attempt_id
        )
        if (
            projected_attempt_id is not None
            and projected_attempt_id != continued_attempt_id
        ):
            logger.info(
                "[RELEASE_QUALIFIER] refusing stale retry attempt %s for v%d; "
                "active attempt is %s",
                continued_attempt_id,
                version.version_number,
                projected_attempt_id,
            )
            return {
                "status": "stale_qualification_attempt",
                "qualification_attempt_id": str(continued_attempt_id),
                "active_qualification_attempt_id": str(projected_attempt_id),
            }
    prior_status = prior_qual.get("status")
    ci_unresolved_recovered_same_sha = (
        prior_status == "not_promoted"
        and prior_qual.get("blocker") == "ci_unresolved"
        and prior_qual.get("candidate_sha") == candidate_sha
        and ci_green
    )
    # Goal-verification recovery — the acceptance-contract safety valve, updated
    # in LOCKSTEP with the redesigned gate.  A same-SHA not_promoted verdict whose
    # blocker was ``criterion_coverage_incomplete`` (now set in release_scoring
    # when any active CATEGORY is not ``met`` OR the residual gap is non-empty) may
    # re-run once the ledger shows the candidate is FULLY MET again: every active
    # category met AND the residual clean.  This is the deadlock this valve exists
    # to break — retracting/superseding an uncoverable criterion, or a re-grade
    # flipping a category to ``met``, must let the qualifier re-audit and clear the
    # gate WITHOUT a SHA change or operator action.  ``is_candidate_fully_met``
    # subsumes the old ``total - covered - attested - backfilled <= 0`` arithmetic
    # AND the residual leg in one sha-keyed read.  Mirrors the ``ci_unresolved``
    # narrow exception: relaxes caching for THIS blocker, on the SAME SHA only.
    # Retiring qualifier VM smoke invalidates every conclusive verdict that
    # participated in that gate.  This includes a historical passing dry_run:
    # it must be re-qualified under the current two-dimension policy instead of
    # being reused on the same SHA (or an identical audited surface).
    legacy_smoke_tainted_verdict = _legacy_smoke_tainted_verdict(prior_qual)
    criterion_coverage_recovered_same_sha = False
    if (
        prior_status == "not_promoted"
        and prior_qual.get("blocker") == "criterion_coverage_incomplete"
        and prior_qual.get("candidate_sha") == candidate_sha
    ):
        if _category_ledger is None:
            # Sibling ledger not present in this checkout — cannot re-evaluate the
            # per-category gate, so leave the verdict cached (safe: no wrong re-run).
            criterion_coverage_recovered_same_sha = False
        else:
            try:
                criterion_coverage_recovered_same_sha = (
                    await _category_ledger.is_candidate_fully_met(
                        session, version.id, candidate_sha
                    )
                )
            except Exception:  # noqa: BLE001 — a ledger lookup failure stays cached (safe: no wrong re-run)
                logger.warning(
                    "[RELEASE_QUALIFIER] run_qualification: goal-verification "
                    "recovery check failed for v%d — leaving the not_promoted "
                    "verdict cached",
                    version.version_number,
                )
                criterion_coverage_recovered_same_sha = False    # Content-hash reuse: a newer commit whose AUDITED SURFACE is byte-identical
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
        and not effective_requested_probes
        and _qualification_probe_contract_is_reusable(
            prior_qual, effective_requested_probes
        )
    )
    if (
        prior_status in _CONCLUSIVE_VERDICT_STATUSES
        and (
            prior_qual.get("candidate_sha") == candidate_sha
            or _same_audited_surface
        )
        and not ci_unresolved_recovered_same_sha
        and not criterion_coverage_recovered_same_sha
        and not legacy_smoke_tainted_verdict
        and prior_qual.get("contract_fingerprint") == contract_fingerprint
        and _qualification_probe_contract_is_reusable(
            prior_qual, effective_requested_probes
        )
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
    # tasks) before the current pending run — it resets to 0 only when a prior
    # finalization wrote a non-infra_failure status.  It accumulates across
    # advancing candidate SHAs so that repeated cancellations on a moving
    # automain still trip the escalation cap and fire an operator Alert.
    _legacy_smoke_infra = _legacy_smoke_infra_lineage(prior_qual)
    prior_infra_failure_candidate_sha = (
        prior_qual.get("candidate_sha")
        if prior_qual.get("status") == "infra_failure" and not _legacy_smoke_infra
        else None
    )
    prior_infra_streak: int = (
        int(prior_qual.get("consecutive_infra_errors") or 0)
        if prior_qual.get("status") == "infra_failure" and not _legacy_smoke_infra
        else 0
    )
    prior_infra_failure_signature = (
        prior_qual.get("infra_failure_signature")
        if prior_qual.get("status") == "infra_failure" and not _legacy_smoke_infra
        else None
    )
    infra_attempt_history = (
        prior_qual.get("infra_attempt_history")
        if prior_qual.get("status") == "infra_failure" and not _legacy_smoke_infra
        else []
    )
    infra_attempt_history_total = (
        prior_qual.get("infra_attempt_history_total")
        if prior_qual.get("status") == "infra_failure" and not _legacy_smoke_infra
        else 0
    )
    prior_same_signature_streak = (
        prior_qual.get("same_signature_streak")
        if prior_qual.get("status") == "infra_failure" and not _legacy_smoke_infra
        else 0
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

    if _sync_fall_through_notification is not None:
        await _notify_operator(
            session,
            version,
            str(_sync_fall_through_notification["event"]),
            str(_sync_fall_through_notification["detail"]),
            extra=dict(_sync_fall_through_notification.get("extra") or {}),
        )

    # 5. Spawn the heavy children.
    started_at = _utcnow().isoformat()
    from app import pause_clock  # noqa: PLC0415 — local import avoids load cycle

    # Pause baseline captured at spawn so the 6h qualification-timeout backstop
    # excludes pipeline-pause time (see _qualification_timeout_age_seconds).
    _pause_baseline = await pause_clock.current_baseline(session)
    # Audit C4: the diff range is pinned to the exact snapshot commit
    # (promotion_target...<candidate_sha>) rather than the live integration
    # branch — the integration branch advances during the 10-30 min the
    # children take to run, and an un-pinned diff would audit a different
    # changeset than was qualified.
    diff_range = f"{promotion_target}...{candidate_sha}"
    # Qualifier VM smoketests are retired. Preserve explicit archival fields in
    # qualification records, but never launch or score this legacy dimension.
    smoketest_enabled = False
    # A live-stack walkthrough is expensive and application-specific. It runs
    # only when Strategic (or another explicit caller) selected it for this
    # qualification decision; no fleet/project flag silently adds one.
    walkthrough_e2e_requested = "walkthrough_e2e" in effective_requested_probes
    # Slice 3 (design 245212a4): the DEPLOYED-side env-preflight judge ships
    # DARK — env_preflight_enabled defaults False.  Resolve with the same source
    # precedence as the promotion rubric: per-project build_config override, then
    # the fleet runtime_config flag, then the promotion_policy.yaml default, then
    # False.  Record it on the pending qual so _finalize_qualification scores
    # against the flag state in force when the run STARTED, not a value that
    # flipped mid-run.  When False the judge is fully inert (no boot_prereq_missing
    # blocker, no env_preflight sub_score, default promotion behaviour unchanged).
    audit_task_id: str | None = None
    challenge_task_id: str | None = None
    audit_challenge_task_id: str | None = None
    audit_challenge_spawn_error: str | None = None
    audit_challenge_spawn_pending = True
    smoketest_task_id: str | None = None
    walkthrough_e2e_task_id: str | None = None
    merge_grade_task_id: str | None = None
    # Blind merge-grade gate (dir d-c0fe439f): grade the merged diffs against the
    # frozen goal.  The goal is the ONLY narrative the grader sees — the spawn
    # helper enforces blindness to running_update.  When the goal is empty or a
    # contentless placeholder (e.g. "test", "n/a") there is nothing to grade
    # against, so no child is spawned and the finalize gate is a no-op PASS (see
    # _finalize_qualification).  Contentless == empty for this purpose.
    # Cross-SHA audit reuse is keyed by the audit dimension's own durable
    # per-path surface identity, not by the mutable last_qualification blob.
    # A byte-identical surface copies the resolved audit/test facts to this
    # candidate with explicit reuse provenance. A changed surface retains the
    # prior candidate only as an agent-verified incremental-base hint.
    prior_audit_fact = (
        await _load_latest_prior_audit_dimension(session, version, candidate_sha)
        if candidate_audited_surface
        else None
    )
    prior_audit_candidate_sha: str | None = None
    changed_audited_paths: list[str] = []
    if prior_audit_fact is not None:
        prior_surface = _audit_detail_surface_map(prior_audit_fact.detail)
        prior_surface_hash = (
            _audited_surface_hash_from_map(prior_surface) if prior_surface else None
        )
        source_sha = str(prior_audit_fact.candidate_sha or "")
        source_challenge = await _load_dimension_result(
            session, version, source_sha, "challenge"
        )
        prior_fact_confirmed = prior_audit_fact.status != "failed" or (
            source_challenge is not None and source_challenge.status == "passed"
        )
        if (
            prior_surface
            and prior_surface_hash == prior_audit_fact.audited_surface_hash
            and source_sha
            and prior_fact_confirmed
            and _audit_detail_is_delta_reusable(prior_audit_fact.detail)
        ):
            changed_audited_paths = _changed_audited_surface_paths(
                prior_surface, candidate_audited_surface
            )
            prior_audit_candidate_sha = source_sha
            if not changed_audited_paths:
                source_erosion = await _load_dimension_result(
                    session, version, source_sha, "test_erosion"
                )
                if (
                    source_erosion is not None
                    and source_erosion.status in _LEDGER_RESOLVED_STATUSES
                ):
                    await _carry_dimension_fact_to_candidate(
                        session, version, prior_audit_fact, candidate_sha, attempt_id
                    )
                    await _carry_dimension_fact_to_candidate(
                        session, version, source_erosion, candidate_sha, attempt_id
                    )
                    if (
                        source_challenge is not None
                        and source_challenge.status in _LEDGER_RESOLVED_STATUSES
                    ):
                        await _carry_dimension_fact_to_candidate(
                            session,
                            version,
                            source_challenge,
                            candidate_sha,
                            attempt_id,
                        )
                    logger.info(
                        "[RELEASE_QUALIFIER] v%d candidate %s reused audit surface "
                        "verbatim from candidate %s (0 audited paths changed)",
                        version.version_number,
                        candidate_sha[:8],
                        source_sha[:8],
                    )
                else:
                    # An audit child also owns test erosion. Without both facts,
                    # the task must run so finalize cannot compose a partial child.
                    prior_audit_candidate_sha = None
            else:
                logger.info(
                    "[RELEASE_QUALIFIER] v%d candidate %s delta-scoping audit from "
                    "%s across %d changed audited path(s)",
                    version.version_number,
                    candidate_sha[:8],
                    source_sha[:8],
                    len(changed_audited_paths),
                )
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
        if dim not in LEDGER_DIMENSION_KEYS:
            continue
        # Mutable gates are re-measured on every same-SHA re-drive. Their old
        # ledger facts stay durable, but must not be composed or marked reused:
        # finalize writes the current measurement back to that one row.
        if dim in MUTABLE_DIMENSION_KEYS:
            continue
        if row.status in _LEDGER_RESOLVED_STATUSES:
            if dim == "merge_grade" and not _rubric_dimension_reusable(
                row.detail,
                fingerprint=rubric_fingerprint,
                qualification_attempt_id=str(attempt_id),
            ):
                logger.info(
                    "[RELEASE_QUALIFIER] v%d candidate %s refusing merge-grade "
                    "reuse: stored attempt/rubric provenance does not match "
                    "the newly launched rubric %s",
                    version.version_number,
                    candidate_sha[:8],
                    rubric_fingerprint[:12],
                )
                continue
            resolved_dims[dim] = {
                "status": row.status,
                "sub_score": row.sub_score,
                "blocker": row.blocker,
                "detail": row.detail,
                "origin_attempt_id": (
                    str(row.origin_attempt_id) if row.origin_attempt_id else None
                ),
                "reused_by_attempt_id": str(attempt_id),
                "producing_task_id": (
                    str(row.producing_task_id) if row.producing_task_id else None
                ),
            }
    # Audit-challenge parity on reuse: a stored audit fact with status "failed"
    # carries a finding-driven DEDUCTION that is only trustworthy once the
    # audit-challenge gate CONFIRMED the underlying findings.  The audit
    # dimension is upserted to the ledger EARLY (before the challenge gate runs
    # in _finalize_qualification), so a prior attempt that HELD on a
    # missing/uncertain/SHA-mismatched challenge leaves audit="failed" while the
    # "challenge" dimension is never written "passed" (its dual-write is only
    # reached on a terminal finalize).  Reusing that audit verbatim would
    # compose an unconfirmed deduction and silently drop the challenge gate the
    # fresh-run path enforces.  Force a fresh audit+challenge re-measurement
    # instead — drop audit+test_erosion from the reuse set so BOTH the spawn
    # decision below AND the finalize composition route through the same gate.
    #
    # A stored audit fact with status "blocked" (sub_score None == the
    # ``audit_missing`` hard-blocker sentinel) is the SECOND way a reused audit
    # fact composes an un-re-measurable verdict, and it is strictly worse: the
    # dimension carries NO usable report at all, so reusing it verbatim replays
    # the identical ``audit_missing`` block on every permitted same-sha re-drive
    # while ``_audit_needed`` stays False — the audit child is never re-spawned,
    # so nothing can ever produce the report that would clear it.  That is a
    # fail-closed wedge with no recovery valve short of new work landing (which
    # moves the sha and abandons the ledger row).  A re-drive is only reached
    # when some caller judged the candidate worth re-measuring, so the ONLY
    # useful thing to do with a blocked audit fact is re-measure it.  Drop it
    # from the reuse set for the same reason, and by the same mechanism, as the
    # unconfirmed-deduction case above.
    _reused_audit_status = resolved_dims.get("audit", {}).get("status")
    _force_fresh_audit_reason: str | None = None
    if (
        _reused_audit_status == "failed"
        and resolved_dims.get("challenge", {}).get("status") != "passed"
    ):
        _force_fresh_audit_reason = (
            "reused a 'failed' audit whose challenge gate never resolved "
            "'passed' — composing the unconfirmed deduction would silently drop "
            "the challenge gate the fresh-run path enforces"
        )
    elif _reused_audit_status == "blocked":
        _force_fresh_audit_reason = (
            "reused a 'blocked' audit fact (no usable report) — replaying it "
            "would re-block on audit_missing forever, since the audit child is "
            "not re-spawned while the fact is reused"
        )
    if _force_fresh_audit_reason:
        logger.info(
            "[RELEASE_QUALIFIER] v%d candidate %s %s — forcing a fresh "
            "audit+challenge re-measurement",
            version.version_number,
            candidate_sha[:8],
            _force_fresh_audit_reason,
        )
        resolved_dims.pop("audit", None)
        resolved_dims.pop("test_erosion", None)
    _reused_audit_detail = resolved_dims.get("audit", {}).get("detail")
    _holistic_contract_required = _merge_grade_goal_usable or bool(rubric_snapshot)
    _holistic_contract_reusable = not _holistic_contract_required or (
        isinstance(_reused_audit_detail, dict)
        and _reused_audit_detail.get("holistic_contract_fingerprint")
        == contract_fingerprint
    )
    if not _holistic_contract_reusable:
        logger.info(
            "[RELEASE_QUALIFIER] v%d candidate %s refusing holistic audit "
            "reuse because the goal/rubric contract changed; retaining its "
            "code-quality and test facts",
            version.version_number,
            candidate_sha[:8],
        )
        resolved_dims.pop("challenge", None)
    await _mark_dimensions_reused_by_attempt(
        session,
        [row for dim, row in prior_dim_results.items() if dim in resolved_dims],
        attempt_id,
    )
    # Pre-spawn deterministic migration-identity collision gate: cheap file-list
    # comparison identical to the finalize-time gate, but run BEFORE incrementing
    # qualification_attempts and BEFORE spawning expensive audit/merge-grade
    # children.  A duplicate-revision or dual-head alembic chain is
    # DETERMINISTIC: re-running the full pipeline cannot change the verdict, so
    # burning an expensive attempt first is pure waste.  Skipped when the
    # migration_identity dimension is already resolved for this candidate_sha from
    # a prior finalize (the dimension was verified clean or blocked then; either
    # way the live fetch is redundant).  Under the FOR UPDATE lock — safe to write.
    if "migration_identity" not in resolved_dims:
        if _pre_mi_verdict == "block":
            logger.info(
                "[RELEASE_QUALIFIER] run_qualification: v%d candidate %s blocked "
                "pre-spawn by migration-identity collision — NOT incrementing "
                "qualification_attempts: %s",
                version.version_number,
                candidate_sha[:8],
                _pre_mi_detail,
            )
            await _upsert_dimension_result(
                session,
                version,
                candidate_sha,
                "migration_identity",
                status="blocked",
                sub_score=None,
                blocker="migration_identity_collision",
                detail={
                    "verdict": "block",
                    "detail": _pre_mi_detail,
                    "execution_site": "pre_spawn",
                },
                origin_attempt_id=attempt_id,
            )
            version.last_qualification = {
                "status": "not_promoted",
                "blocker": "migration_identity_collision",
                "candidate_sha": candidate_sha,
                "prepromotion_rehearsal_dispatch": rehearsal_dispatch,
                "qualification_attempt_id": str(attempt_id),
                "retry_episode": retry_episode,
                "retry_episode_cap": retry_episode_cap,
                "detail": _pre_mi_detail,
                "started_at": started_at,
            }
            flag_modified(version, "last_qualification")
            session.add(version)
            return {
                "status": "migration_identity_collision",
                "blocker": "migration_identity_collision",
                "detail": _pre_mi_detail,
                "qualification_attempt_id": str(attempt_id),
            }
        if _pre_mi_verdict == "defer":
            logger.info(
                "[RELEASE_QUALIFIER] run_qualification: v%d candidate %s "
                "migration-identity pre-spawn check deferred — NOT incrementing "
                "qualification_attempts: %s",
                version.version_number,
                candidate_sha[:8],
                _pre_mi_detail,
            )
            return {
                "status": "migration_identity_deferred",
                "detail": _pre_mi_detail,
                "qualification_attempt_id": str(attempt_id),
            }
    # The one holistic review owns code quality, test erosion, goal alignment,
    # and acceptance-category evidence.  There is no parallel merge-grade leg.
    _audit_needed = not (
        "audit" in resolved_dims and "test_erosion" in resolved_dims
    ) or not _holistic_contract_reusable
    # The walkthrough_e2e child is an opt-in decision-bearing product probe.
    _walkthrough_e2e_needed = walkthrough_e2e_requested
    _merge_grade_needed = False
    incremental_audit_base_sha = _incremental_audit_base_sha(
        prior_qual,
        candidate_sha,
        prior_audit_candidate_sha=(
            prior_audit_candidate_sha if changed_audited_paths else None
        ),
    )
    if resolved_dims:
        logger.info(
            "[RELEASE_QUALIFIER] run_qualification: v%d candidate %s has %d "
            "already-resolved dimension(s) from a prior finalize — "
            "audit_needed=%s merge_grade_needed=%s",
            version.version_number,
            candidate_sha[:8],
            len(resolved_dims),
            _audit_needed,

            _merge_grade_needed,
        )
    # The ReleaseVersion row is locked here, so lifetime/new-attempt accounting
    # and the active projection advance atomically with the pending/error write.
    version.active_qualification_attempt_id = attempt_id
    version.qualification_retry_episode = retry_episode
    if is_new_attempt:
        version.qualification_attempts = (version.qualification_attempts or 0) + 1
    # Attempt contract: freeze promotion policy and capability-proof mode BEFORE
    # the first child-spawn await so a mid-flight operator/config change cannot
    # compose children spawned under one contract with a verdict computed under
    # another.  Legacy records that predate the freeze fall back to live values.
    # The spawn block is ATOMIC: if a later spawn fails, every child row already
    # created in this block is cancelled so a partial spawn cannot orphan a
    # QUEUED/PROVISIONING child of an attempt that is going terminal.
    walkthrough_e2e_task: Task | None = None
    audit_task: Task | None = None
    try:
        # Spawn the walkthrough_e2e child FIRST: its execution-site check fails
        # closed (QualifierWalkthroughSiteUnavailableError) before any child row
        # is created, so a site failure cannot orphan the QUEUED release_audit
        # child that follows. The walkthrough spawn does not depend on the audit
        # child's existence, so fail-before-allocating is safe here.
        if _walkthrough_e2e_needed:
            walkthrough_e2e_task = await _spawn_qualifier_walkthrough_task(
                session,
                project_id,
                version_id=version.id,
                candidate_sha=candidate_sha,
                qualification_attempt_id=attempt_id,
            )
            walkthrough_e2e_task_id = str(walkthrough_e2e_task.id)
        if _audit_needed:
            audit_task = await _spawn_release_audit_task(
                session,
                project_id,
                version_id=version.id,
                base_branch=promotion_target,
                diff_range=diff_range,
                candidate_sha=candidate_sha,
                qualification_attempt_id=attempt_id,
                audit_base_sha=incremental_audit_base_sha,
                version_goal=version.goal,
                categories=[
                    {
                        "category_key": c.get("criterion_key"),
                        "text": c.get("text"),
                        **(
                            {"evidence_contract": c["evidence_contract"]}
                            if isinstance(c.get("evidence_contract"), dict)
                            else {}
                        ),
                    }
                    for c in rubric_snapshot
                    if c.get("criterion_key")
                ],
                rubric_fingerprint=rubric_fingerprint,
                candidate_authorship_receipt=(
                    _prelock_audit_authorship_receipt
                ),
                candidate_authorship_measured_range=(
                    _prelock_audit_authorship_range
                ),
            )
            audit_task_id = str(audit_task.id)
    except Exception as exc:  # noqa: BLE001 — spawn failure must not raise out
        # Make the spawn block atomic: cancel any child row created before the
        # failure so a partial spawn cannot leave a phantom QUEUED/PROVISIONING
        # child of an attempt that is going terminal as spawn_failed.
        _spawn_cleanup_at = _utcnow()
        for _spawned_child in (walkthrough_e2e_task, audit_task):
            if _spawned_child is None or _spawned_child.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.BLOCKED,
            ):
                continue
            _spawned_child.status = TaskStatus.CANCELLED
            _spawned_child.vm_stopped_at = _spawn_cleanup_at
            session.add(_spawned_child)
        logger.warning(
            "[RELEASE_QUALIFIER] failed to spawn qualification children for "
            "version v%d: %s",
            version.version_number,
            exc,
            exc_info=True,
        )
        # Stamp the serving engine sha on the spawn_failed record so the
        # error-redrive sweep can detect a later execution-site capability
        # change: release_qualifier_redrive._walkthrough_execution_site_changed
        # compares this against the current serving sha to re-arm a capped
        # walkthrough spawn_failed exactly once after a deploy.  Lazy import
        # mirrors the redrive module's idiom (app.deployed_sha is a lightweight
        # env-reader; no load-order coupling here).
        from app.deployed_sha import get_serving_sha  # noqa: PLC0415

        version.last_qualification = {
            "status": "error",
            "error": "spawn_failed",
            "engine_sha": get_serving_sha(),
            "detail": str(exc)[:300],
            "candidate_sha": candidate_sha,
            "prepromotion_rehearsal_dispatch": rehearsal_dispatch,
            "ci_green": ci_green,
            "ci_state": ci_state,
            "started_at": started_at,
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": retry_episode,
            "retry_episode_cap": retry_episode_cap,
            "requested_probes": sorted(effective_requested_probes),
            # This is still a counted qualification attempt. Preserve the
            # launch-time decision contract even when child spawning fails so
            # Strategic can distinguish this attempt from current live policy.
            "promotion_policy_snapshot": _spawn_policy_snapshot,
            "promotion_policy_source": _spawn_policy_source,
            "promotion_policy_fallback_error": _spawn_policy_fallback_error,
            "promotion_policy_hash": _spawn_policy_projection["policy_hash"],
        }
        # Read-modify-write is safe here (unlike the no_credentials / sha_error
        # early returns, which run pre-lock and use the atomic
        # _record_attempt_projection_atomic): this site runs UNDER the
        # ReleaseVersion FOR UPDATE row lock taken above, so no concurrent
        # caller can interleave a lost update.
        flag_modified(version, "last_qualification")
        session.add(version)
        # Redrive counter stamping is an exact-result CAS.  Return the durable
        # attempt identity so the sweep can prove it is stamping this failure,
        # rather than a newer qualification projection for the same version.
        return {
            "status": "spawn_failed",
            "qualification_attempt_id": str(attempt_id),
        }

    audit_challenge_spawn_pending = _audit_needed


    # 6. Stamp the pending qualification record.  smoketest_task_id is None
    #    when the smoketest subsystem is disabled — maybe_finalize_qualification
    #    and the reaper sweep both treat a None smoketest id as "no smoketest
    #    child to wait on".  prior_infra_streak carries the infra-failure streak
    #    from the previous infra_failure finalization (0 when the previous
    #    finalization was a quality verdict / dry-run / non-infra outcome).
    version.last_qualification = {
        "status": "pending",
        "qualification_attempt_id": str(attempt_id),
        "retry_episode": retry_episode,
        "retry_episode_cap": retry_episode_cap,
        "candidate_sha": candidate_sha,
        "prepromotion_rehearsal_dispatch": rehearsal_dispatch,
        "candidate_diff_hash": candidate_diff_hash,
        "stranded_approved_prs": stranded_approved_prs,
        "audited_surface_paths": candidate_audited_surface or None,
        "audit_base_sha": incremental_audit_base_sha,
        "prior_audit_candidate_sha": (
            prior_audit_candidate_sha if changed_audited_paths else None
        ),
        "changed_audited_paths": changed_audited_paths or None,
        "ci_green": ci_green,
        "ci_state": ci_state,
        "smoketest_enabled": smoketest_enabled,
        # Slice 3 (design 245212a4): dark-by-default deployed-side env-preflight
        # judge flag, captured at spawn so finalize scores against the in-force state.
        "env_preflight_enabled": env_preflight_enabled,
        # Candidate-side env-preflight evidence artifact, produced unconditionally
        # (decoupled from the smoketest child).  Pure-content presence-only shape
        # — names and scopes only, never values.  None when the evidence could not
        # be fetched from the candidate tree (caught and handled in finalize).
        "env_preflight_evidence": env_preflight_evidence,
        "started_at": started_at,
        "pause_baseline": _pause_baseline,
        "audit_task_id": audit_task_id,
        "challenge_task_id": challenge_task_id,
        "audit_challenge_task_id": audit_challenge_task_id,
        "audit_challenge_spawn_error": audit_challenge_spawn_error,
        "audit_challenge_spawn_pending": audit_challenge_spawn_pending,
        "smoketest_task_id": smoketest_task_id,
        # Explicitly requested decision-bearing product probe. It is a required
        # child for this attempt and finalization consumes its same-SHA result.
        "requested_probes": sorted(effective_requested_probes),
        # Durable record of a product probe dropped at request time because the
        # open version's ACTIVE rubric shares no canonical evidence key with it
        # (blocker="incompatible_probe").  None when no probe was dropped.  The
        # strategist reads this alongside requested_probes to see why the
        # requested probe never ran.
        "qualify_request_blocked": _probe_drop_record,
        "qualify_request_blocked_at": _probe_drop_blocked_at,
        "walkthrough_e2e_task_id": walkthrough_e2e_task_id,
        "merge_grade_task_id": merge_grade_task_id,
        "rubric_snapshot": rubric_snapshot,
        "rubric_fingerprint": rubric_fingerprint,
        "contract_fingerprint": contract_fingerprint,
        "rubric_lifecycle_snapshot": rubric_lifecycle_snapshot,
        "rubric_provenance": rubric_provenance,
        "prior_infra_streak": prior_infra_streak,
        "previous_infra_failure_signature": prior_infra_failure_signature,
        "previous_infra_failure_candidate_sha": prior_infra_failure_candidate_sha,
        "infra_attempt_history": infra_attempt_history,
        "infra_attempt_history_total": infra_attempt_history_total,
        "prior_same_signature_streak": prior_same_signature_streak,
        "prior_superseded": prior_superseded,
        # Per-rubric-episode re-drive counter, carried forward from the prior
        # rubric hold (0 when the prior outcome was not a rubric hold).  The
        # rubric re-driver (_redrive_rubric_changed_qualification) increments
        # this POST-commit after each successful relaunch so the cap advances
        # across the spawn→finalize→relaunch cycle; stamping it here keeps the
        # pending record carrying the accumulated count even if that post-commit
        # write is lost to a crash.
        "rubric_redrive_count": int(prior_qual.get("rubric_redrive_count") or 0),
        # Per-contract-episode re-drive counter, carried forward from the prior
        # walkthrough contract-failure verdict (0 when the prior outcome was not
        # a contract failure).  The walkthrough-contract re-driver
        # (_redrive_walkthrough_contract_qualification) increments this
        # POST-commit after each successful re-entry so the cap advances across
        # the spawn→finalize→re-drive cycle; stamping it here keeps the pending
        # record carrying the accumulated count even if that post-commit write
        # is lost to a crash.
        "walkthrough_contract_redrive_count": int(
            prior_qual.get("walkthrough_contract_redrive_count") or 0
        ),
        "keyless_requalification_consumed_at": prior_qual.get(
            "keyless_requalification_consumed_at"
        ),
        # Slice 4 (design 245212a4): OWNER-armed one-shot acknowledgment that the
        # missing boot prerequisite will be bound in the same deploy revision.
        # Carried forward from the prior verdict (base_record REPLACES
        # last_qualification, so an armed ack must survive re-qualification) so it
        # is still armed when this finalize scores env-preflight.  Consumed by
        # _finalize_qualification only once it actually overrides a
        # boot_prereq_missing block.
        "ack_env_will_be_bound": prior_qual.get("ack_env_will_be_bound"),
        # Dimensions already resolved (passed/failed/blocked) for this
        # candidate_sha from a prior finalize — carried forward from the
        # per-dimension ledger so _finalize_qualification can compose from
        # them without re-running their gates.
        "resolved_dimensions": resolved_dims if resolved_dims else None,
        # Attempt contract: freeze the version goal, promotion policy, and
        # capability-proof mode at spawn so mid-flight configuration changes
        # cannot compose children spawned under one contract with a verdict
        # computed under another.
        "version_goal": version_goal,
        "promotion_policy_snapshot": _spawn_policy_snapshot,
        "promotion_policy_source": _spawn_policy_source,
        "promotion_policy_fallback_error": _spawn_policy_fallback_error,
        "promotion_policy_hash": _spawn_policy_projection["policy_hash"],
        "capability_proof_mode": _spawn_capability_proof_mode,
    }
    # Read-modify-write is safe here (unlike the no_credentials / sha_error
    # early returns, which run pre-lock and use the atomic
    # _record_attempt_projection_atomic): this site runs UNDER the
    # ReleaseVersion FOR UPDATE row lock taken above, so no concurrent caller
    # can interleave a lost update.
    flag_modified(version, "last_qualification")
    session.add(version)

    logger.info(
        "[RELEASE_QUALIFIER] qualification started for v%d project=%s "
        "candidate=%s ci_green=%s audit_task=%s challenge_pending=%s",
        version.version_number,
        project_id,
        candidate_sha[:8],
        ci_green,
        audit_task_id,
        audit_challenge_spawn_pending,
    )
    return {
        "status": "spawned",
        "qualification_attempt_id": str(attempt_id),
        "retry_episode": retry_episode,
        "candidate_sha": candidate_sha,
        "stranded_approved_prs": stranded_approved_prs,
        "ci_green": ci_green,
        "ci_state": ci_state,
        "smoketest_enabled": smoketest_enabled,
        "env_preflight_enabled": env_preflight_enabled,
        "audit_task_id": audit_task_id,
        "challenge_task_id": challenge_task_id,
        "audit_challenge_task_id": audit_challenge_task_id,
        "audit_challenge_spawn_error": audit_challenge_spawn_error,
        "audit_challenge_spawn_pending": audit_challenge_spawn_pending,
        "smoketest_task_id": smoketest_task_id,
        "requested_probes": sorted(effective_requested_probes),
        "walkthrough_e2e_task_id": walkthrough_e2e_task_id,
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


def _evaluate_walkthrough_e2e_report(
    report: Any, candidate_sha: str | None
) -> dict[str, Any]:
    """Validate and classify a decision-bearing walkthrough report.

    A negative criterion is a valid quality result (``failed``). Structural or
    provenance defects are ``invalid`` and therefore infrastructure failures,
    never quality verdicts. The exercised live stack must identify the exact
    candidate; checking only the report's requested SHA would accept a harness
    that accidentally exercised another checkout.
    """
    expected_sha = str(candidate_sha or "").strip()
    if not isinstance(report, dict):
        return {"status": "invalid", "reason": "report_missing"}
    report_sha = str(report.get("git_sha") or "").strip()
    if not report_sha or report_sha != expected_sha:
        return {
            "status": "invalid",
            "reason": "candidate_sha_mismatch",
            "candidate_sha": report_sha or None,
        }
    exercised_sha = str(report.get("exercised_engine_sha") or "").strip()
    if not exercised_sha or exercised_sha != expected_sha:
        return {
            "status": "invalid",
            "reason": "exercised_engine_sha_mismatch",
            "candidate_sha": report_sha,
            "exercised_engine_sha": exercised_sha or None,
        }
    results = report.get("results")
    if not isinstance(results, list):
        return {
            "status": "invalid",
            "reason": "results_missing",
            "candidate_sha": report_sha,
            "exercised_engine_sha": exercised_sha,
        }

    normalized_results: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for entry in results:
        if not isinstance(entry, dict):
            return {
                "status": "invalid",
                "reason": "result_entry_invalid",
                "candidate_sha": report_sha,
                "exercised_engine_sha": exercised_sha,
            }
        key = str(entry.get("criterion_key") or "").strip()
        notes = entry.get("notes")
        if (
            not key
            or len(key) > 120
            or type(entry.get("passed")) is not bool
            or (notes is not None and not isinstance(notes, str))
        ):
            return {
                "status": "invalid",
                "reason": "result_entry_invalid",
                "candidate_sha": report_sha,
                "exercised_engine_sha": exercised_sha,
            }
        if key in seen_keys:
            return {
                "status": "invalid",
                "reason": "result_key_duplicate",
                "candidate_sha": report_sha,
                "exercised_engine_sha": exercised_sha,
                "criterion_key": key,
            }
        seen_keys.add(key)
        normalized_results.append(
            {
                "criterion_key": key,
                "passed": entry["passed"],
                "notes": notes[:500] if notes is not None else None,
            }
        )

    if not seen_keys:
        # An empty results list is a truncated report — the harness must emit at
        # least one flow outcome.
        return {
            "status": "invalid",
            "reason": "result_schema_mismatch",
            "candidate_sha": report_sha,
            "exercised_engine_sha": exercised_sha,
        }
    if (
        seen_keys & _WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_RAW_KEYS
        and seen_keys != _WALKTHROUGH_E2E_EXPECTED_RAW_RESULT_KEYS
    ):
        # A report that uses ANY of the legacy SUBSTANTIVE harness keys must
        # carry the COMPLETE v26 output contract (a partial legacy report is
        # truncated).  A report that uses NONE of the substantive legacy keys is
        # authored against the OPEN version's rubric — the harness was re-pointed
        # to emit that rubric's fresh criterion keys — so its keys are validated
        # downstream by the finalize-time gate against the ACTIVE rubric, not
        # against the retired v26 set here.  The canonical meta key c-5214996f
        # is deliberately NOT a legacy signal: it participates in both the
        # retired report and the open rubric, so a report carrying the meta row
        # alongside fresh substantive keys is an open-rubric report.
        return {
            "status": "invalid",
            "reason": "result_schema_mismatch",
            "candidate_sha": report_sha,
            "exercised_engine_sha": exercised_sha,
            "missing_criterion_keys": sorted(
                _WALKTHROUGH_E2E_EXPECTED_RAW_RESULT_KEYS - seen_keys
            ),
            "unexpected_criterion_keys": sorted(
                seen_keys - _WALKTHROUGH_E2E_EXPECTED_RAW_RESULT_KEYS
            ),
        }

    failed_keys = [
        row["criterion_key"] for row in normalized_results if not row["passed"]
    ]
    return {
        "status": "failed" if failed_keys else "passed",
        "candidate_sha": report_sha,
        "exercised_engine_sha": exercised_sha,
        "result_count": len(normalized_results),
        "failed_criterion_keys": failed_keys[:50],
        "results": normalized_results,
    }


def _walkthrough_e2e_canonical_evidence_keys(
    normalized_results: list[dict[str, Any]],
) -> frozenset[str]:
    """Resolve a report's raw criterion keys to its unique evidence keys.

    Each raw key is resolved through the legacy alias map
    (:func:`app.acceptance_criteria._resolve_walkthrough_criterion_key`) — e.g.
    ``c-workday-session-lifecycle`` → ``c-5214996f`` — so a report that still
    emits a legacy slug maps onto the same canonical evidence key as the
    explicit meta row.  That workday slug / meta row is the only intentional
    many-to-one pair; keeping the exception exact prevents a later alias-table
    edit from silently merging two otherwise independent probe outcomes.  Any
    OTHER pair of raw keys collapsing onto one canonical key is real malformed
    evidence and raises.  WHICH canonical keys a report may name is NOT
    validated here — the caller checks the resolved keys against the open
    version's ACTIVE rubric rather than a frozen constant.
    """
    from app.acceptance_criteria import _resolve_walkthrough_criterion_key

    canonical_groups: dict[str, set[str]] = {}
    for row in normalized_results:
        raw_key = row["criterion_key"]
        canonical_groups.setdefault(
            _resolve_walkthrough_criterion_key(raw_key), set()
        ).add(raw_key)
    duplicate_groups = {
        frozenset(raw_keys)
        for raw_keys in canonical_groups.values()
        if len(raw_keys) > 1
    }
    # Raise only on a collision that is NOT the sole allowed workday/meta pair.
    # A report with no collisions at all (e.g. fresh open-rubric keys, or a
    # legacy report that omitted the meta row) must NOT raise — the previous
    # ``!= {allowed}`` test treated the empty set as unexpected and refused any
    # report that did not carry the v26 workday slug + meta row.
    if duplicate_groups - {_WALKTHROUGH_E2E_ALLOWED_CANONICAL_COLLISION}:
        raise ValueError("unexpected canonical criterion collision")
    return frozenset(canonical_groups)


def _apply_walkthrough_decision(
    composite: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """Fold a valid requested walkthrough into the promotion decision."""
    passed = result.get("status") == "passed"
    updated = {
        **composite,
        "sub_scores": {
            **(composite.get("sub_scores") or {}),
            "walkthrough_e2e": 1.0 if passed else 0.0,
        },
    }
    if passed:
        return updated
    failing_gates = list(composite.get("failing_gates") or [])
    if "walkthrough_e2e_failed" not in failing_gates:
        failing_gates.append("walkthrough_e2e_failed")
    updated["failing_gates"] = failing_gates
    updated["confidence_ceiling"] = composite.get("confidence_ceiling", 0.0)
    updated["sub_scores"]["failing_gates"] = failing_gates
    return {
        **updated,
        "promote": False,
        "blocker": "walkthrough_e2e_failed",
    }


async def _handle_walkthrough_contract_failure(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    qual: dict[str, Any],
    *,
    error: str,
    walkthrough_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize a walkthrough probe whose REPORT violates the evidence contract.

    Report-key validation — schema mismatch, duplicate/invalid entries, canonical
    collisions, and unknown criteria — is performed by the DEPLOYED qualifier
    against the DEPLOYED rubric.  No fix on automain can change how the
    currently-serving validator parses the report, so this is a hard blocker
    classified ``EXECUTION_SITE_DEPLOYED`` (inert until this version itself
    promotes) — NOT the transient, non-conclusive infra hold the runner/harness
    failures use.  Unlike ``_handle_infra_failure`` it records a durable
    ``not_promoted`` verdict, writes the walkthrough dimension ledger row as
    ``blocked`` (a resolved fact, never the transient ``unavailable``), and does
    not touch the infra-error streak or raise an infra crash-loop Alert.
    """
    finalized_at = _utcnow().isoformat()
    candidate_sha = qual.get("candidate_sha")
    sub_scores: dict[str, Any] = {
        "walkthrough_e2e": 0.0,
        "failing_gates": ["walkthrough_e2e_contract_failed"],
        "confidence_ceiling": 0.0,
    }
    # Stamp the serving engine sha on the record so observability (and any
    # future site-change detection) can see WHICH deployed validator produced
    # this contract failure — a new serving sha means the validator may have
    # changed.  Lazy import mirrors the spawn_failed record's idiom
    # (app.deployed_sha is a lightweight env-reader; no load-order coupling).
    from app.deployed_sha import get_serving_sha  # noqa: PLC0415

    record: dict[str, Any] = {
        **qualification_attempt_policy_record_fields(qual),
        "qualification_attempt_id": qual.get("qualification_attempt_id"),
        "retry_episode": qual.get("retry_episode", 0),
        "retry_episode_cap": qual.get("retry_episode_cap"),
        "candidate_sha": candidate_sha,
        "candidate_diff_hash": qual.get("candidate_diff_hash"),
        "ci_green": qual.get("ci_green"),
        "ci_resolution": qual.get("ci_resolution"),
        "smoketest_enabled": bool(qual.get("smoketest_enabled", False)),
        "engine_sha": get_serving_sha(),
        "status": "not_promoted",
        "blocker": "walkthrough_e2e_contract_failed",
        # Persist the execution site directly so every downstream surface
        # (agent_strategic_fetch reads the stored value first) sees DEPLOYED
        # even for records whose blocker is not in BLOCKER_EXECUTION_SITE.
        "execution_site": blocker_execution_site("walkthrough_e2e_contract_failed"),
        # Observability: which exact contract check failed
        # (walkthrough_e2e_report_<reason>).
        "error": error,
        "confidence": 0.0,
        "sub_scores": sub_scores,
        "failing_gates": ["walkthrough_e2e_contract_failed"],
        "confidence_ceiling": 0.0,
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": qual.get("audit_task_id"),
        "audit_challenge_task_id": qual.get("audit_challenge_task_id"),
        "audit_challenge_spawn_error": qual.get("audit_challenge_spawn_error"),
        "smoketest_task_id": qual.get("smoketest_task_id"),
        "requested_probes": qual.get("requested_probes") or [],
        "walkthrough_e2e_task_id": qual.get("walkthrough_e2e_task_id"),
        "walkthrough_e2e_result": walkthrough_result,
        # Carry the one-shot keyless re-qualification marker forward so the
        # sweep's exactly-once bound survives the overwrite of last_qualification.
        "keyless_requalification_consumed_at": qual.get(
            "keyless_requalification_consumed_at"
        ),
        # Carry the per-contract-episode re-drive counter forward so the
        # sweep's walkthrough-contract re-driver cap
        # (_redrive_walkthrough_contract_qualification) survives the
        # spawn→finalize→re-drive overwrite of last_qualification.
        "walkthrough_contract_redrive_count": qual.get(
            "walkthrough_contract_redrive_count"
        ),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number}: walkthrough_e2e report "
            f"violates the deployed qualifier's evidence contract ({error}) — "
            "deployed-side blocker, returning to stabilize"
        ),
    )
    session.add(version)

    # The walkthrough dimension ledger row: a contract failure is a RESOLVED
    # (blocked) fact, never the transient 'unavailable' an infra hold records.
    if candidate_sha:
        await _upsert_dimension_result(
            session,
            version,
            candidate_sha,
            "walkthrough_e2e",
            status="blocked",
            sub_score=0.0,
            blocker="walkthrough_e2e_contract_failed",
            detail={"error": error},
            origin_attempt_id=qual.get("qualification_attempt_id"),
        )

    # Compose already-resolved dimension facts from the per-dimension ledger into
    # the record so a passed audit/CI/etc. stays visible in the verdict even
    # though the probe leg's contract failure ends finalize early (mirrors
    # _handle_infra_failure's Phase 1b read).  Best-effort — never fatal.
    resolved_dimensions: dict[str, Any] = {}
    if candidate_sha:
        try:
            _prior = await _load_prior_dimension_results(
                session, version, candidate_sha
            )
            for _dim, _row in _prior.items():
                if _row.status in _LEDGER_RESOLVED_STATUSES:
                    resolved_dimensions[_dim] = {
                        "status": _row.status,
                        "sub_score": _row.sub_score,
                        "blocker": _row.blocker,
                    }
        except Exception:  # noqa: BLE001 — read is best-effort, never fatal
            resolved_dimensions = {}
    if resolved_dimensions:
        record["resolved_dimensions"] = resolved_dimensions
        flag_modified(version, "last_qualification")

    promotion_enabled = resolve_project_promotion_enabled(version.project_id)
    await _record_qualification_verdict(
        session,
        version,
        record,
        terminal_status="not_promoted",
        would_promote=False,
        promotion_enabled=promotion_enabled,
    )

    blocker_label = _BLOCKER_LABELS.get(
        "walkthrough_e2e_contract_failed", "walkthrough_e2e_contract_failed"
    )
    _site_phrase = blocker_execution_site_label("walkthrough_e2e_contract_failed")
    if _site_phrase:
        blocker_label = f"{blocker_label} ({_site_phrase})"
    await _notify_operator(
        session,
        version,
        "release_blocked",
        (
            f"v{version.version_number} blocked from promotion — {blocker_label} "
            f"({error}). Not promoted; staying in stabilize."
        ),
        extra={
            "blocker": "walkthrough_e2e_contract_failed",
            "execution_site": blocker_execution_site(
                "walkthrough_e2e_contract_failed"
            ),
            "error": error,
        },
    )
    logger.warning(
        "[RELEASE_QUALIFIER] v%d walkthrough_e2e contract failure: %s — "
        "verdict=not_promoted (deployed-side blocker, not infra)",
        version.version_number,
        error,
    )
    return {
        "status": "not_promoted",
        "blocker": "walkthrough_e2e_contract_failed",
        "execution_site": blocker_execution_site("walkthrough_e2e_contract_failed"),
        "error": error,
    }


def _advance_qualification_retry_episode(
    version: release_version.ReleaseVersion,
    record: dict[str, Any],
    *,
    cap: int,
) -> dict[str, Any]:
    episode = int(record.get("retry_episode") or 0) + 1
    version.qualification_retry_episode = episode
    return {
        **record,
        "retry_episode": episode,
        "retry_episode_cap": cap,
    }

# Human-readable rendering of the compute_composite() hard-blocker codes.
_BLOCKER_LABELS: dict[str, str] = {
    "ci_not_green": "CI is not green on the candidate",
    "ci_unresolved": "CI state could not be evaluated (pending, no check-runs, or GitHub I/O degraded)",

    "audit_missing": "the release audit produced no usable report",
    "walkthrough_e2e_failed": (
        "the explicitly requested live-stack walkthrough failed one or more "
        "acceptance criteria"
    ),
    "walkthrough_e2e_contract_failed": (
        "the live-stack walkthrough report does not satisfy the DEPLOYED "
        "qualifier's evidence contract (report-key validation failed: schema "
        "mismatch, duplicate/invalid entries, canonical collisions, or keys "
        "that do not resolve to the open version's ACTIVE rubric) — a fix on "
        "automain is inert until this version itself promotes"
    ),
    "test_file_deleted": "the diff deleted a test file",
    "migration_identity_collision": (
        "an Alembic revision-identity collision between the candidate and the "
        "deployed promotion target"
    ),
    "migration_apply_drift": (
        "the candidate's migration chain does not apply cleanly against "
        "production's recorded schema state (deploy-fatal drift)"
    ),
    "candidate_operator_authorship_nonzero": (
        "the candidate's complete base-to-head range recorded more "
        "operator-authored commits than its contracted repair budget"
    ),
    "candidate_operator_authorship_evidence_missing": (
        "the candidate has no valid complete-range operator-authorship "
        "measurement; the gate fails closed until that evidence is resolved"
    ),
    "criterion_coverage_incomplete": (
        "the blind merge-grade found the merged diff does not yet satisfy the "
        "version goal: at least one active acceptance-criterion CATEGORY is not "
        "met (or is only partially met), or the grader named a goal aspect that "
        "no category captures (residual gap) — the categories must all grade "
        "'met' with cited diff evidence, and the residual must be clean, to clear"
    ),
    "merge_grade_misses": (
        "the merged diffs 'misses' the version goal per the blind merge-grade "
        "and the merge_grade_block_threshold is armed"
    ),
    "boot_prereq_missing": (
        "a boot/env prerequisite the candidate declares is absent from the "
        "deploy-target environment (presence-only): bind the missing secret(s) "
        "in the deploy target before promotion"
    ),
    "boot_prereq_evidence_missing": (
        "the candidate produced no boot/env-prerequisite evidence, so the boot "
        "gate cannot be certified — ensure the evidence producer runs on the "
        "candidate tree"
    ),
    # NOTE: ``uncorroborated_attestation`` was retired with the attestation gate
    # it labeled (replaced by the per-category goal-verification gate, whose
    # block surfaces as ``criterion_coverage_incomplete``).  It has no emitter
    # anywhere in the backend; a label for a blocker nothing can emit is dead
    # operator-facing vocabulary, so it is intentionally absent here.  Do not
    # re-add a label without an emitter — see test_blocker_labels_have_emitters.
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

def _migration_apply_timeout_detail(detail: str | None) -> bool:
    """Return whether detail starts with one stable migration watchdog code."""

    if not detail:
        return False
    code = detail.partition(":")[0].strip()
    return code.startswith("migration_apply_") and code.endswith("_timeout")

def _migration_apply_infra_error(detail: str | None) -> str:
    """Preserve stable timeout codes; retain the legacy code for other defers."""

    if _migration_apply_timeout_detail(detail):
        return str(detail)
    return (
        f"migration_apply_check_unavailable: {detail}"
        if detail
        else "migration_apply_check_unavailable"
    )

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
        scratch_preflight = preflight_migration_apply_scratch(
            os.getenv(MIGRATION_APPLY_SCRATCH_ENV)
        )
        if scratch_preflight.status == "absent":
            # Config-absent: record as "skipped" so a skipped gate can never read
            # as a passed gate.  Not "defer" (transient infra) and not "ok" (gate
            # actually ran & passed) — this is a permanent config choice meaning
            # the gate was never attempted.
            return ("skipped", "scratch_database_not_configured")
        if scratch_preflight.status != "ready" or scratch_preflight.target is None:
            return ("defer", scratch_preflight.detail)
        prod_engine = resolve_async_engine(session)
        scratch_db_url = scratch_preflight.target

        try:
            verdict, detail = await run_migration_apply_gate(
                prod_engine=prod_engine,
                gh_token=token,
                owner=owner,
                repo=repo,
                candidate_ref=candidate_sha,
                project_id=str(version.project_id),
                scratch_db_url=scratch_db_url,
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
        if _migration_apply_timeout_detail(detail):
            # Fresh credentials cannot repair a wedged import, lock, or database
            # stage. The next qualification cycle may retry; this finalize must
            # not blindly double the watchdog budget.
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
            scratch_db_url=scratch_db_url,
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
        "acceptance_window_declared_unsatisfied_reasons": dict, # split of the declared_unsatisfied bucket by reason (sums to buckets["declared_unsatisfied"])
        "acceptance_window_undeclared_task_ids": list,  # deterministic capped IDs in the undeclared bucket
        "acceptance_window_declared_unsatisfied_task_ids": list,  # deterministic capped IDs in the declared-unsatisfied bucket
        "acceptance_window_task_records": list,  # one durable audit record per write task
        "acceptance_window_bucket_task_id_cap": int,  # shared cap for the two identity-evidence lists
        "acceptance_window_inferred_test_cochange": int,  # verified merges credited via inferred_test_cochange
        "acceptance_window_rederived_count": int,  # write-task verdicts re-derived under the current rule this drive (each task counted once)
        "acceptance_window_rederived_upgrades": int,  # re-derived verdicts that strictly improved (satisfied/proven minted)
        "acceptance_window_rederived_downgrades": int,  # re-derived verdicts that strictly worsened
        "acceptance_window_rule_version": int,  # the acceptance rule version this drive judged under
        "acceptance_window_stale_version_counts": dict,  # retired rule version -> recorded rows stamped under it (legacy pre-stamp rows counted under 0)
        "acceptance_window_rederived_task_ids": list,  # task ids actually re-derived this drive (capped)
      }

    The nested breakdowns are purely additive: ``acceptance_window_buckets``
    keys the 7 canonical ``ACCEPTANCE_AGGREGATE_BUCKETS`` and makes the fraction
    auditable (verified == proven + satisfied_unproven + inferred_test_cochange),
    while ``acceptance_window_undeclared_reasons`` explains why the remaining
    undeclared merges did not infer coverage and
    ``acceptance_window_declared_unsatisfied_reasons`` explains WHY each
    explicitly declared-but-unsatisfied verdict failed (its persisted per-verdict
    reason, previously discarded at aggregation). No existing key changes type or
    value.
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
    # undeclared, and for each declared-but-unsatisfied verdict WHY it failed.
    # All breakdowns are additive observability — the scalar ``verified`` is
    # derived from the three verified buckets so its value and the invariant
    # contract are byte-for-byte unchanged from the prior ``sum(...)``.
    buckets: dict[str, int] = dict.fromkeys(ACCEPTANCE_AGGREGATE_BUCKETS, 0)
    undeclared_reasons: dict[str, int] = dict.fromkeys(_UNDECLARED_REASON_KEYS, 0)
    unsatisfied_reasons: dict[str, int] = dict.fromkeys(_UNSATISFIED_REASON_KEYS, 0)
    # Retain enough task identity to audit an unhealed bucket without making the
    # qualification payload unbounded.  IDs are sorted after classification so
    # database row order cannot change the observable result.
    _BUCKET_TASK_ID_CAP = 100
    undeclared_task_ids: list[str] = []
    declared_unsatisfied_task_ids: list[str] = []
    task_records: list[dict[str, Any]] = []
    # Re-derivation pass (rung 3 of design doc c50453ef): a verdict frozen under
    # a RETIRED acceptance rule is re-derived from the task's persisted merge
    # record before classification, so an evidence-rule improvement landing
    # mid-version re-scores already-merged work. Current-rule stamps and the
    # declared-fail-safe rows pass through verbatim (see
    # ``acceptance_gate.effective_acceptance_verdict``); undeclared rows keep no
    # verdict key so the inferred-test-cochange path still applies. The re-derived
    # verdict is written back onto the session-bound task so the correction is
    # DURABLE for every reader, not a per-drive throwaway.
    # Re-derivation audit (design doc c50453ef §5): the window reports how much
    # already-merged work was re-scored by the current rule THIS drive, in which
    # direction, and which retired rule versions produced the stale population —
    # so score movement is inspectable instead of a silently-shifting aggregate.
    # A task is counted exactly ONCE per drive even though the corroboration pass
    # and the classification pass both walk every write-task row.
    _REDERIVED_TASK_ID_CAP = 100
    _rederived_seen: set[str] = set()
    _stale_seen: set[str] = set()
    _rederived_task_ids: list[str] = []
    _stale_version_counts: dict[int, int] = {}
    _rederived_upgrades = 0
    _rederived_downgrades = 0

    def _verdict_rank(verdict: dict) -> int:
        """Ordinal rank of a verdict for upgrade/downgrade direction: 0
        unsatisfied, 1 satisfied-unproven, 2 proven."""
        if verdict.get("proven") is True:
            return 2
        if verdict.get("satisfied") is True:
            return 1
        return 0

    def _effective_context(task: Task) -> dict:
        nonlocal _rederived_upgrades, _rederived_downgrades
        ctx = dict(task.context or {})
        frozen = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
        if frozen is not None and not isinstance(frozen, dict):
            frozen = None
        # Audit the RETIRED-version population BEFORE re-derivation: every
        # recorded row stamped under a rule other than the current one is counted
        # once under its own version, so the stale population's size and vintage
        # are visible even when a row cannot be re-derived (its payload no longer
        # declares evidence, so ``effective_acceptance_verdict`` returns the
        # frozen row verbatim).
        if isinstance(frozen, dict):
            _version = verdict_rule_version(frozen)
            if _version != ACCEPTANCE_RULE_VERSION:
                _task_id = str(task.id)
                if _task_id not in _stale_seen:
                    _stale_seen.add(_task_id)
                    _stale_version_counts[_version] = (
                        _stale_version_counts.get(_version, 0) + 1
                    )
        effective = effective_acceptance_verdict(task)
        if effective is not None:
            ctx[CODE_ACCEPTANCE_VERDICT_KEY] = effective
            # Durability: re-derivation recomputes the corrected verdict on every
            # drive and every other reader (task_outcomes, direction_outcomes,
            # acceptance_criteria, metrics_integrity_sweep, capability_proof)
            # reads the RECORDED row — so a non-frozen re-derivation is stamped
            # back onto ``task.context`` and made durable by the enclosing flow's
            # next commit. Idempotent: once re-stamped under the CURRENT rule,
            # later drives return the frozen dict verbatim and skip this write.
            if effective is not frozen:
                task.context = ctx
                session.add(task)
                # Count the re-derivation exactly once per task per drive. The
                # direction is the strict rank movement between the retired-rule
                # verdict and the current-rule verdict, so upgrades AND downgrades
                # are both visible — re-derivation is honest, not monotonic.
                _task_id = str(task.id)
                if _task_id not in _rederived_seen:
                    _rederived_seen.add(_task_id)
                    _rederived_task_ids.append(_task_id)
                    _frozen_rank = (
                        _verdict_rank(frozen) if isinstance(frozen, dict) else 0
                    )
                    _effective_rank = _verdict_rank(effective)
                    if _effective_rank > _frozen_rank:
                        _rederived_upgrades += 1
                    elif _effective_rank < _frozen_rank:
                        _rederived_downgrades += 1
        return ctx

    # Corroboration pass (the fix for "the acceptance window mints ~0 proven
    # coverage"): soft named_test verdicts (satisfied, unproven) recorded on a
    # non-CI-monitor landing path carry no corroboration marker, so a declared
    # test ABSENT from the PR's diff but PRESENT in the merged tree is capped at
    # soft credit forever. Corroborate each such verdict against the stored
    # merge commit and re-record — a test the backend confirms exists at the
    # merge commit then mints machine-proven coverage. CONSERVATIVE: only a
    # confirmed presence (True) stamps the marker and re-records; a corroborated
    # absence or indeterminate result leaves the recorded soft verdict untouched,
    # so this additive observability pass never downgrades a previous
    # qualification. Best-effort and bounded per drive — corroboration is GitHub
    # I/O, so the pass never blocks the aggregation on a slow/absent tree.
    #
    # The cap bounds ATTEMPTS, not successes: a window where every candidate
    # corroborates indeterminate (no GitHub creds, unavailable tree) must still
    # issue at most ``_WINDOW_CORROBORATION_CAP`` round-trips per drive. And
    # because a non-True result stamps no corroboration marker, the attempt is
    # additionally recorded DURABLY under
    # :data:`WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY` keyed by the merge sha that
    # was read — otherwise the same tasks would be re-read in full on every
    # subsequent qualifier drive, forever. Keying by sha (not a bare boolean)
    # keeps a later landing on a NEW merge commit eligible for a fresh attempt.
    _window_project = await session.get(Project, version.project_id)
    _WINDOW_CORROBORATION_CAP = 50
    _corroboration_attempts = 0
    for task in rows_write:
        if _corroboration_attempts >= _WINDOW_CORROBORATION_CAP:
            break
        _ctx = _effective_context(task)
        _verdict = _ctx.get(CODE_ACCEPTANCE_VERDICT_KEY)
        if not (
            isinstance(_verdict, dict)
            and _verdict.get("kind") == "named_test"
            and _verdict.get("satisfied") is True
            and _verdict.get("proven") is not True
            and ACCEPTANCE_TEST_CORROBORATED_KEY not in _ctx
        ):
            continue
        _sha = (task.context or {}).get(LANDED_MERGE_SHA_KEY)
        if not isinstance(_sha, str) or not _sha:
            continue
        _sha = _sha[:64]
        if (task.context or {}).get(WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY) == _sha:
            # Already attempted against this exact merge commit on an earlier
            # drive and it did not produce a definitive result. The read is
            # deterministic for an immutable commit, so retrying spends I/O for
            # no new information.
            continue
        from app.acceptance_gate import corroborate_acceptance_test_at_merge

        _corroboration_attempts += 1
        try:
            _result = await corroborate_acceptance_test_at_merge(
                task,
                session,
                project=_window_project,
                commit_sha=_sha,
            )
        except Exception:  # noqa: BLE001 — window corroboration must never abort
            logger.warning(
                "[RELEASE_QUALIFIER] acceptance-window corroboration failed for "
                "task %s",
                getattr(task, "id", None),
                exc_info=True,
            )
            # An UNEXPECTED raise (the helper swallows its own I/O failures) is
            # not durably marked: leave the task eligible so a transient fault
            # cannot permanently deny it proven credit. The per-drive cap above
            # already bounds the cost.
            continue
        _new_ctx = {
            **(task.context or {}),
            WINDOW_CORROBORATION_ATTEMPTED_SHA_KEY: _sha,
        }
        if _result is True:
            # CONSERVATIVE: only a confirmed presence stamps the corroboration
            # marker and re-records. A corroborated absence (False) or an
            # indeterminate result (None) leaves the recorded soft verdict
            # untouched — it is only marked as attempted.
            _new_ctx[ACCEPTANCE_TEST_CORROBORATED_KEY] = True
            task.context = _new_ctx
            record_code_acceptance_verdict(task)
        else:
            task.context = _new_ctx
        session.add(task)

    for task in rows_write:
        ctx = _effective_context(task)
        bucket = classify_merged_acceptance_context(ctx)
        buckets[bucket] += 1
        if bucket == "undeclared":
            undeclared_reasons[classify_undeclared_reason(ctx)] += 1
            undeclared_task_ids.append(str(task.id))
        elif bucket == "declared_unsatisfied":
            unsatisfied_reasons[
                classify_unsatisfied_reason(ctx.get(CODE_ACCEPTANCE_VERDICT_KEY))
            ] += 1
            declared_unsatisfied_task_ids.append(str(task.id))
        task_records.append(
            {
                "task_id": str(task.id),
                "bucket": bucket,
                "credited": bucket
                in {
                    "proven",
                    "satisfied_unproven",
                    "inferred_test_cochange",
                },
                PR_CHANGED_FILES_STAMP_SKIPPED_KEY: (
                    ctx.get(PR_CHANGED_FILES_STAMP_SKIPPED_KEY)
                    if isinstance(ctx.get(PR_CHANGED_FILES_STAMP_SKIPPED_KEY), str)
                    else None
                ),
            }
        )
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
        if classify_merged_acceptance_context(_effective_context(task)) == "inferred_test_cochange"
    ][:_INFERRED_TASK_ID_CAP]
    fraction = (verified / total) if total > 0 else None
    return {
        "acceptance_window_total": total,
        "acceptance_window_verified": verified,
        "acceptance_window_fraction": fraction,
        "acceptance_window_write_total": total,
        "acceptance_window_total_all_merges": total_all_merges,
        # Additive nested breakdowns (None-safe on legacy rows that lack them):
        # per-bucket counts over the write-task window, and the undeclared /
        # declared-unsatisfied reason splits.
        # Sum(buckets) == total; sum(undeclared_reasons) == buckets["undeclared"];
        # sum(unsatisfied_reasons) == buckets["declared_unsatisfied"].
        "acceptance_window_buckets": buckets,
        "acceptance_window_undeclared_reasons": undeclared_reasons,
        "acceptance_window_declared_unsatisfied_reasons": unsatisfied_reasons,
        # Deterministic, bounded identity evidence for the two buckets that
        # cannot be diagnosed from counts alone.  ``*_total`` and
        # ``*_truncated`` make an omitted tail unambiguous rather than looking
        # like an empty bucket.
        "acceptance_window_undeclared_task_ids": sorted(undeclared_task_ids)[
            :_BUCKET_TASK_ID_CAP
        ],
        "acceptance_window_undeclared_task_ids_total": len(undeclared_task_ids),
        "acceptance_window_undeclared_task_ids_truncated": len(undeclared_task_ids)
        > _BUCKET_TASK_ID_CAP,
        "acceptance_window_declared_unsatisfied_task_ids": sorted(
            declared_unsatisfied_task_ids
        )[:_BUCKET_TASK_ID_CAP],
        "acceptance_window_declared_unsatisfied_task_ids_total": len(
            declared_unsatisfied_task_ids
        ),
        "acceptance_window_declared_unsatisfied_task_ids_truncated": len(
            declared_unsatisfied_task_ids
        )
        > _BUCKET_TASK_ID_CAP,
        # Full per-task window enumeration: unlike the capped bucket identity
        # lists above, this is the durable record used to audit every write task
        # as either credited or carrying a stamp-skip cause.
        "acceptance_window_task_records": task_records,
        "acceptance_window_bucket_task_id_cap": _BUCKET_TASK_ID_CAP,
        # Additive audit breakdown: how many verified merges were credited via the
        # inferred test-cochange path (undeclared merge, CI-verified, test+prod
        # co-change) rather than an explicit proven/satisfied declaration. Consumed
        # by acceptance_criteria.record_acceptance_window_coverage_evidence for the
        # evidence detail; no score impact. Equals buckets["inferred_test_cochange"].
        "acceptance_window_inferred_test_cochange": inferred_test_cochange,
        # Per-merge audit trail: task IDs of the inferred_test_cochange merges so
        # operators can trace which specific merges were counted without SQL.
        "acceptance_window_inferred_test_cochange_task_ids": inferred_task_ids,
        # Re-derivation audit (design doc c50453ef §5): how much already-merged
        # work was re-scored by the CURRENT rule this drive, in which direction,
        # and which retired rule versions produced the stale population. All keys
        # are additive observability — the scalar ``verified`` and the bucket
        # counts above are unchanged. ``rederived_count`` == len(rederived_task_ids)
        # before the cap; upgrades + downgrades can be less than the count because
        # a re-derivation that preserves the rank (e.g. a satisfied->satisfied
        # reason change) is still a re-derivation.
        "acceptance_window_rederived_count": len(_rederived_seen),
        "acceptance_window_rederived_upgrades": _rederived_upgrades,
        "acceptance_window_rederived_downgrades": _rederived_downgrades,
        "acceptance_window_rule_version": ACCEPTANCE_RULE_VERSION,
        "acceptance_window_stale_version_counts": dict(
            sorted(_stale_version_counts.items())
        ),
        "acceptance_window_rederived_task_ids": _rederived_task_ids[
            :_REDERIVED_TASK_ID_CAP
        ],
    }

async def _collect_verified_merge_test_evidence(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> dict[str, list[str]]:
    """Gather the candidate's VERIFIED-merged test + production-seam file paths.

    Feeds the v2 machine-tier upgrade in ``evaluate_merge_grade``: a merge-grade
    ``{"kind": "test"}`` citation earns the machine tier only when the cited test
    is among these ``test_files`` (a test that shipped in a merge gated by a REAL
    CI/test verdict — ``landed_signal.is_verified_merge``) AND semantically maps to
    one of these ``seam_files``.  Restricting to verified merges is how "ran green
    on the candidate SHA" is machine-confirmed without re-running any test at grade
    time: the qualifier has no live test-runner, and a verified merge is the
    standing machine evidence that the shipped test ran green under real CI.

    Scans the SAME version window as ``_compute_acceptance_window_aggregate`` — all
    COMPLETED, landed tasks that completed at/after ``version.opened_at`` — and, for
    those whose merge is ``verified``, splits each task's ``_pr_changed_files`` into
    test vs non-test (production seam) paths.  Returns
    ``{"test_files": [...], "seam_files": [...]}`` (deduplicated, sorted).
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
    test_files: set[str] = set()
    seam_files: set[str] = set()
    for task in rows:
        if not is_verified_merge(task):
            continue
        changed = (task.context or {}).get("_pr_changed_files")
        if not isinstance(changed, list):
            continue
        for p in changed:
            if not isinstance(p, str) or not p.strip():
                continue
            if _is_test_path(p):
                test_files.add(p.strip())
            else:
                seam_files.add(p.strip())
    return {"test_files": sorted(test_files), "seam_files": sorted(seam_files)}


async def _guard_ci_unresolved_promotion_pending(
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
    marker write after a release PR was created. A
    subsequent sweep re-evaluated CI as ci_unresolved (transient, not red) and
    wrote not_promoted — clobbering a managed promotion.

    This guard runs UNDER the version row lock (caller holds it from
    _finalize_locked_version) before the not_promoted write. GitHub I/O here is
    quick (ancestry + open-PR fetch), consistent with _resolve_ci_at_finalize's
    existing pattern of doing brief API calls under the lock.

    (a) Candidate already on main — the PR merged while promotion_pending was
        being committed and the commit rolled back. Record success instead.
    (b) Open release PR still exists — restore the managed promotion_pending
        sub-state so the reconciler can revalidate and retry it rather than
        writing a spurious not_promoted.

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

    receipt = await _resolve_exact_promotion_receipt(
        token,
        owner,
        repo,
        candidate_sha,
        version,
        project=project,
    )
    if receipt is not None:
        logger.info(
            "[RELEASE_QUALIFIER] ci_unresolved guard: candidate %s for v%d is "
            "bound to an exact promotion receipt — recording success instead "
            "of not_promoted",
            candidate_sha[:8],
            version.version_number,
        )
        receipt_pr_number = receipt.get("pr_number")
        if receipt_pr_number is not None:
            from app.rollup_migration_validation import (
                _validate_migration_chain_post_merge,
            )

            migration_status, migration_reason = (
                await _validate_migration_chain_post_merge(
                    token,
                    owner,
                    repo,
                    int(receipt_pr_number),
                    receipt["merge_commit_sha"],
                )
            )
            if migration_status == "defer":
                return {
                    "status": "pending",
                    "reason": "promotion_receipt_validation_deferred",
                }
            if migration_status == "block":
                return await _record_promotion_failure(
                    session,
                    version,
                    _recover_confidence_from_ci_unresolved(base_record),
                    "migration_guard_blocked",
                    detail=migration_reason,
                    pr_url=receipt.get("pr_url"),
                    lock_released=False,
                )
        return await _record_promotion_success(
            session,
            version,
            candidate_sha,
            _recover_confidence_from_ci_unresolved(base_record),
            pr_url=receipt.get("pr_url") or _idempotent_pr_url(base_record),
            token=token,
            owner=owner,
            repo=repo,
            empty_diff=bool(receipt.get("empty_diff")),
            promoted_sha=receipt["merge_commit_sha"],
            already_landed=True,
            lock_released=False,
        )

    # Candidate not yet on main — check whether its release PR is still open.
    # If so, restore the one canonical managed waiter so the reconciler checks
    # CI, revalidates the exact target, and retries instead of recording a miss.
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
                and (pr.get("head") or {}).get("sha") == candidate_sha
                and (pr.get("base") or {}).get("ref") == promotion_target
                and pr.get("state", "open") == "open"
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
    logger.info(
        "[RELEASE_QUALIFIER] ci_unresolved guard: open release PR #%s for v%d "
        "found — restoring managed_check_wait instead of writing not_promoted",
        pr_number,
        version.version_number,
    )

    promotion_pending: dict[str, Any] = {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "managed_check_wait": True,
        "enabled_at": _utcnow().isoformat(),
        "restored_by_ci_unresolved_guard": True,
    }

    q = dict(version.last_qualification or {})
    q.update(_recover_confidence_from_ci_unresolved(base_record))
    q["status"] = "pending"
    q["promotion_pending"] = promotion_pending
    version.last_qualification = q
    flag_modified(version, "last_qualification")
    session.add(version)
    return {
        "status": "pending_managed_check_wait",
        "pr_number": pr_number,
        "pr_url": pr_url,
        "managed_check_wait": True,
    }


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
    origin_attempt_id: uuid.UUID | str | None = None,
    reused_by_attempt_ids: list[str] | None = None,
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
    _origin_uuid = _coerce_qualification_attempt_id(origin_attempt_id)

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
                # Preserve an older producer only when the start path explicitly
                # marked this fact as reused by the active attempt.  Otherwise a
                # resolved write is a genuine recomputation with a new origin.
                if (
                    dimension not in MUTABLE_DIMENSION_KEYS
                    and existing.status in _LEDGER_RESOLVED_STATUSES
                    and _origin_uuid is not None
                    and existing.origin_attempt_id != _origin_uuid
                    and str(_origin_uuid)
                    in {str(value) for value in (existing.reused_by_attempt_ids or [])}
                ):
                    return
                # Never downgrade a resolved fact (passed/failed/blocked) to a
                # non-resolved status (e.g. "unavailable" from an infra failure
                # in a different gate or a failed _load_prior_dimension_results).
                # _record_infra_dimension_unavailable has a matching guard at its
                # call site; this primitive-level guard is defense-in-depth.
                if (
                    existing.status in _LEDGER_RESOLVED_STATUSES
                    and status not in _LEDGER_RESOLVED_STATUSES
                ):
                    logger.debug(
                        "[RELEASE_QUALIFIER] skipping downgrade of resolved dimension "
                        "'%s' on v%s/%s from '%s' to '%s'",
                        dimension,
                        getattr(version, "version_number", "?"),
                        candidate_sha[:8] if candidate_sha else "?",
                        existing.status,
                        status,
                    )
                    return
                existing.status = status
                existing.sub_score = _sub_score
                existing.blocker = blocker
                existing.audited_surface_hash = audited_surface_hash
                existing.detail = detail
                existing.producing_task_id = _producing_uuid
                if _origin_uuid is not None:
                    existing.origin_attempt_id = _origin_uuid
                if reused_by_attempt_ids is not None:
                    existing.reused_by_attempt_ids = reused_by_attempt_ids
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
                        origin_attempt_id=_origin_uuid,
                        reused_by_attempt_ids=reused_by_attempt_ids,
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
# reached a real pass/fail/blocked outcome.  ``unavailable`` is deliberately
# ambiguous at the storage layer: it can mean an explicitly non-applicable gate
# or a transient infrastructure failure.  Promotion therefore must not treat the
# status alone as terminal; it may accept only a known explicit exemption.
# Re-finalize still RE-RUNS infra-held dimensions while REUSING the ones that
# already truly passed/failed/blocked.
_LEDGER_RESOLVED_STATUSES: frozenset[str] = frozenset({"passed", "failed", "blocked"})

_PROMOTION_EXPLICIT_NON_APPLICABLE_STATUSES: dict[str, frozenset[str]] = {
    # With no gradeable goal there is no merge-grade gate.  The two exemption
    # statuses are also explicit, durable evidence that the holistic grade is
    # not applicable to promotion for this candidate.
    "merge_grade": frozenset(
        {"not_applicable", "superseded_exempt", "already_on_main_exempt"}
    ),
    # When no scratch DB is configured the apply gate is permanently skipped
    # (not transiently deferred).  Record as an explicit non-applicable so
    # the promotion ledger-completeness check does not treat the missing
    # gate as an infra hold that blocks the release indefinitely.
    "migration_apply": frozenset({"scratch_database_not_configured"}),
}

def _ledger_fact_satisfies_promotion(
    dimension: str,
    row: QualificationDimensionResult,
) -> bool:
    """Whether one ledger row is durable terminal proof for promotion.

    A transient infra hold is recorded as ``status="unavailable"`` with details
    such as ``infra_error`` or a migration ``verdict="defer"``.  Those facts
    remain incomplete.  Only a dimension-specific, explicit non-applicable
    marker may satisfy the same-tree invariant without a pass/fail/blocked
    outcome.
    """
    if row.status in _LEDGER_RESOLVED_STATUSES:
        return True
    if row.status != "unavailable" or not isinstance(row.detail, dict):
        return False
    explicit_status = row.detail.get("status")
    return explicit_status in _PROMOTION_EXPLICIT_NON_APPLICABLE_STATUSES.get(
        dimension, frozenset()
    )

# Gates that must every one have a terminal fact at the SAME candidate_sha before
# a promotion may compose from the ledger — the informational dimensions
# (challenge / acceptance_window / debt_pressure) never gate promotion and are
# excluded. Retired smoke rows are archival and never enter this active set.
# Sourced from the canonical dimension registry (see qualification_dimensions).
_PROMOTION_REQUIRED_DIMENSIONS: tuple[str, ...] = PROMOTION_REQUIRED_DIMENSIONS

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
    # Old smoke rows remain queryable in storage but never compose into a new
    # attempt, satisfy completeness, or suppress an active child spawn.
    return {r.dimension: r for r in rows if r.dimension in LEDGER_DIMENSION_KEYS}

async def _load_dimension_result(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
    dimension: str,
) -> QualificationDimensionResult | None:
    """Load one durable dimension fact at an exact candidate SHA."""
    if not candidate_sha:
        return None
    try:
        return (
            await session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id,
                    QualificationDimensionResult.candidate_sha == candidate_sha,
                    QualificationDimensionResult.dimension == dimension,
                )
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 -- reuse is best-effort
        logger.warning(
            "[RELEASE_QUALIFIER] failed to load %s dimension for v%s/%s "
            "(non-fatal -- falling back to full audit): %s",
            dimension,
            getattr(version, "version_number", "?"),
            candidate_sha[:8],
            exc,
        )
        return None

async def _load_latest_prior_audit_dimension(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
) -> QualificationDimensionResult | None:
    """Return the newest complete cross-SHA audit fact usable as a delta base."""
    try:
        rows = (
            (
                await session.execute(
                    select(QualificationDimensionResult)
                    .where(
                        QualificationDimensionResult.release_version_id == version.id,
                        QualificationDimensionResult.dimension == "audit",
                        QualificationDimensionResult.candidate_sha.is_not(None),
                        QualificationDimensionResult.candidate_sha != candidate_sha,
                        QualificationDimensionResult.status.in_(("passed", "failed")),
                    )
                    .order_by(QualificationDimensionResult.computed_at.desc())
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001 -- reuse is best-effort
        logger.warning(
            "[RELEASE_QUALIFIER] failed to load prior audit dimension for v%s "
            "(non-fatal -- falling back to full audit): %s",
            getattr(version, "version_number", "?"),
            exc,
        )
        return None
    return next(
        (
            row
            for row in rows
            if row.audited_surface_hash
            and row.candidate_sha
            and _audit_detail_is_delta_reusable(row.detail)
        ),
        None,
    )

async def _carry_dimension_fact_to_candidate(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    source: QualificationDimensionResult,
    candidate_sha: str,
    attempt_id: uuid.UUID,
) -> None:
    """Copy a reusable fact to a new candidate while preserving ownership."""
    reused = [
        str(value)
        for value in (source.reused_by_attempt_ids or [])
        if _coerce_qualification_attempt_id(value) is not None
    ]
    attempt_text = str(attempt_id)
    if attempt_text not in reused:
        reused.append(attempt_text)
    await _upsert_dimension_result(
        session,
        version,
        candidate_sha,
        source.dimension,
        status=source.status,
        sub_score=source.sub_score,
        blocker=source.blocker,
        detail=source.detail,
        producing_task_id=source.producing_task_id,
        audited_surface_hash=source.audited_surface_hash,
        origin_attempt_id=source.origin_attempt_id,
        reused_by_attempt_ids=reused,
    )

async def _mark_dimensions_reused_by_attempt(
    session: AsyncSession,
    rows: list[QualificationDimensionResult],
    attempt_id: uuid.UUID,
) -> None:
    """Best-effort append reuse provenance without changing fact ownership."""
    if not rows:
        return
    try:
        async with session.begin_nested():
            attempt_text = str(attempt_id)
            for row in rows:
                reused = [
                    str(value)
                    for value in (row.reused_by_attempt_ids or [])
                    if _coerce_qualification_attempt_id(value) is not None
                ]
                if attempt_text not in reused:
                    row.reused_by_attempt_ids = [*reused, attempt_text]
                    session.add(row)
            await session.flush()
    except Exception as exc:  # noqa: BLE001 -- ledger provenance is failure-isolated
        logger.warning(
            "[RELEASE_QUALIFIER] failed to mark dimension reuse for attempt %s "
            "(non-fatal -- additive ledger provenance only): %s",
            attempt_id,
            exc,
        )

async def _record_infra_dimension_unavailable(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
    dimension: str,
    *,
    infra_error: str,
    signature: str | None = None,
    origin_attempt_id: uuid.UUID | str | None = None,
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
        if existing is not None and existing.status in _LEDGER_RESOLVED_STATUSES:
            # Already resolved (passed/failed/blocked) — an infra failure in a
            # different gate must never downgrade a fact that truly settled.
            logger.debug(
                "[RELEASE_QUALIFIER] skipping infra-unavailable write for dimension "
                "'%s' on v%s/%s — already resolved as '%s'",
                dimension,
                getattr(version, "version_number", "?"),
                candidate_sha[:8] if candidate_sha else "?",
                existing.status,
            )
            return 0
        if existing is not None and isinstance(existing.detail, dict):
            prior = existing.detail.get("consecutive_infra_errors")
            if isinstance(prior, int) and not isinstance(prior, bool):
                prior_streak = prior
        elif existing is None:
            # No row for this candidate SHA yet — carry the streak forward from
            # the most recent unavailable row for this dimension at ANY SHA so
            # that the cap still fires on a moving automain (new SHA each cycle).
            prior_row = (
                await session.execute(
                    select(QualificationDimensionResult)
                    .where(
                        QualificationDimensionResult.release_version_id == version.id,
                        QualificationDimensionResult.dimension == dimension,
                        QualificationDimensionResult.status == "unavailable",
                    )
                    .order_by(QualificationDimensionResult.computed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if prior_row is not None and isinstance(prior_row.detail, dict):
                prior = prior_row.detail.get("consecutive_infra_errors")
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
        origin_attempt_id=origin_attempt_id,
    )
    return new_streak

async def _promotion_dimension_ledger_complete(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> tuple[bool, list[str]]:
    """Is there a full set of durable terminal facts at ONE candidate_sha?

    The batch model's invariant — every gate scored the SAME candidate tree —
    carried onto the per-dimension ledger: a promotion may only compose from the
    ledger when every required gating dimension has a real outcome or an explicit
    non-applicable fact (not transiently unavailable, pending, or missing) at
    ``candidate_sha``.  A required dimension whose only fact is at a DIFFERENT
    candidate_sha does not count (``_load_prior_dimension_results`` is already
    sha-scoped).  Returns ``(complete, missing_dimensions)``.
    """
    required = list(_PROMOTION_REQUIRED_DIMENSIONS)

    prior = await _load_prior_dimension_results(session, version, candidate_sha)
    missing = [
        dim
        for dim in required
        if dim not in prior or not _ledger_fact_satisfies_promotion(dim, prior[dim])
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
    dimension has resolved (CI, audit, test-erosion, challenge, both
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
    ci_resolution = base_record.get("ci_resolution")
    if not isinstance(ci_resolution, dict):
        ci_resolution = {}
    ci_required = bool(base_record.get("ci_required", True))


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
        if verdict in ("defer", "skipped"):
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

    # env_preflight ledger status (Slice 3, design 245212a4): mirror the deployed-
    # side judge semantics used by the state resolver.  DARK by default → the judge
    # is inert, so no manifest is scored and the row is ``unavailable``.  When the
    # judge is enabled: a live boot-prereq block (boot_prereq_missing /
    # boot_prereq_evidence_missing) surfaces via the composite blocker as
    # ``blocked``; a recorded positive sub_score is ``passed``; a None score (no
    # candidate manifest seen) is ``unavailable``.  env_preflight is MUTABLE — it is
    # re-measured live every drive and its reused ledger row is never composed, so
    # this spec always upserts the freshly-measured fact.
    _epf_enabled = bool(base_record.get("env_preflight_enabled"))
    _epf_blocker = _own_blocker({"boot_prereq_missing", "boot_prereq_evidence_missing"})
    _epf_score = _score("env_preflight")
    if _epf_blocker is not None:
        _epf_status = "blocked"
    elif _epf_enabled and _epf_score is not None and _epf_score > 0.0:
        _epf_status = "passed"
    else:
        _epf_status = "unavailable"

    specs: list[dict[str, Any]] = [
        {
            "dimension": "ci",
            "status": _ci_dimension_status(
                ci_green=ci_green,
                ci_resolution=ci_resolution,
                ci_required=ci_required,
            ),
            "sub_score": None,
            "blocker": _own_blocker({"ci_not_green", "ci_unresolved"}),
            "detail": ci_resolution,
            "producing_task_id": None,
        },
        {
            "dimension": "audit",
            "status": audit_status,
            "sub_score": audit_score,
            "blocker": _own_blocker({"audit_missing"}),
            "audited_surface_hash": base_record.get("candidate_diff_hash"),
            "detail": _audit_dimension_detail(
                surface_paths=base_record.get("audited_surface_paths"),
                audit_findings=base_record.get("audit_findings") or [],
                excluded_audit_findings=base_record.get("excluded_audit_findings")
                or [],
                findings_meta=base_record,
                audit_missing_reason=base_record.get("audit_missing_reason"),
                audit_scope=base_record.get("audit_scope"),
                holistic_review=base_record.get("holistic_review"),
                holistic_contract_fingerprint=base_record.get(
                    "contract_fingerprint"
                ),
            ),
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
                **(
                    {"status": base_record.get("migration_apply_safety_detail")}
                    if base_record.get("migration_apply_safety") == "skipped"
                    and base_record.get("migration_apply_safety_detail")
                    else {}
                ),
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
        {
            "dimension": "env_preflight",
            "status": _epf_status,
            # Presence-only sub_score, recorded only when the judge is enabled so a
            # dark run carries no score into the ledger.
            "sub_score": _epf_score if _epf_enabled else None,
            "blocker": _epf_blocker,
            # Slice 3 verdict blob: {required:[{name,scope}], missing:[names],
            # deploy_target_scope} — presence-only, None when the judge was disabled
            # or no candidate manifest was seen.
            "detail": base_record.get("env_preflight_verdict"),
            "producing_task_id": None,
        },
    ]

    # Fail-fast: every dimension in the canonical ledger-backed registry must have a
    # spec entry here.  A future ledger dimension added to the registry but omitted
    # from the specs list would silently never be persisted — this assertion ensures
    # the dual-write writer stays synchronised with LEDGER_DIMENSION_KEYS.
    spec_dimension_set = {s["dimension"] for s in specs}
    assert spec_dimension_set == LEDGER_DIMENSION_KEYS, (
        f"dual-write spec dimensions ({spec_dimension_set}) != "
        f"LEDGER_DIMENSION_KEYS ({LEDGER_DIMENSION_KEYS}); "
        f"extra: {spec_dimension_set - LEDGER_DIMENSION_KEYS}, "
        f"missing: {LEDGER_DIMENSION_KEYS - spec_dimension_set}"
    )

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
        if (
            spec["dimension"] in prior_resolved
            and spec["dimension"] not in MUTABLE_DIMENSION_KEYS
        ):
            # Already resolved (passed/failed/blocked) for this candidate tree in
            # an earlier finalize — reuse the stored fact, do NOT re-record or
            # overwrite it.  This is the read-side composition: a re-check after a
            # transient failure re-runs ONLY the still-unavailable dimensions and
            # never discards the ones that already passed for the same tree.
            continue
        _detail = spec["detail"]
        # Tag a blocked dimension's fact with WHERE its check runs (deployed-side
        # inert-until-promote vs candidate-side clearable by an automain fix), so
        # the per-dimension ledger view carries the same execution-site signal as
        # last_qualification.  Wrap non-dict details so we never clobber them.
        _spec_site = blocker_execution_site(spec["blocker"])
        if _spec_site is not None:
            if isinstance(_detail, dict):
                _detail = {**_detail, "execution_site": _spec_site}
            else:
                _detail = {"execution_site": _spec_site, "detail": _detail}
        await _upsert_dimension_result(
            session,
            version,
            candidate_sha,
            spec["dimension"],
            status=spec["status"],
            sub_score=spec["sub_score"],
            blocker=spec["blocker"],
            detail=_detail,
            producing_task_id=spec["producing_task_id"],
            audited_surface_hash=spec.get("audited_surface_hash"),
            origin_attempt_id=base_record.get("qualification_attempt_id"),
        )

async def _build_env_preflight_evidence_for_candidate(
    project: Project,
    token: str,
    owner: str,
    repo: str,
    candidate_sha: str,
) -> dict[str, Any] | None:
    """Build env-preflight evidence from the candidate tree via the GitHub API.

    Reads ``backend/app/required_runtime_env.py`` and
    ``backend/testing/smoketest.yaml`` from the candidate SHA through the GitHub
    contents API (cheap, no VM needed) and produces the same pure-content artifact
    as :func:`build_env_preflight_evidence`.

    Returns ``None`` when either source file is missing or unreadable — the
    caller treats this as "evidence unavailable", which triggers
    ``boot_prereq_evidence_missing`` when the env-preflight judge is enabled.

    Uses the in-memory content cache (10-minute TTL) so repeated reads of the
    same file at the same ref on the same orchestrator avoid redundant API calls.
    """
    from app.env_preflight_evidence import build_env_preflight_from_source

    required_runtime_py = await github.get_file_text_at_ref(
        "backend/app/required_runtime_env.py",
        token=token,
        owner=owner,
        repo=repo,
        ref=candidate_sha,
        project_id=str(project.id),
        use_content_cache=True,
    )
    smoketest_yaml = await github.get_file_text_at_ref(
        "backend/testing/smoketest.yaml",
        token=token,
        owner=owner,
        repo=repo,
        ref=candidate_sha,
        project_id=str(project.id),
        use_content_cache=True,
    )
    return build_env_preflight_from_source(
        required_runtime_env_py_source=required_runtime_py,
        smoketest_yaml_source=smoketest_yaml,
    )

async def _record_rubric_provenance_outcome(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    qual: dict[str, Any],
    *,
    status: str,
    rubric_provenance: dict[str, Any] | None,
    rubric_drift: dict[str, Any] | None = None,
    provenance_error: str | None = None,
    policy: dict[str, Any] | None = None,
    blocker: str | None = None,
) -> dict[str, Any]:
    """Hold an attempt whose launched acceptance contract cannot finalize."""
    finalized_at = _utcnow().isoformat()
    verdict_status = "rubric_unknown" if status == "rubric_provenance_unknown" else status
    record: dict[str, Any] = {
        **qualification_attempt_policy_record_fields(qual),
        "status": status,
        "candidate_sha": qual.get("candidate_sha"),
        "candidate_diff_hash": qual.get("candidate_diff_hash"),
        "audited_surface_paths": qual.get("audited_surface_paths"),
        "qualification_attempt_id": qual.get("qualification_attempt_id"),
        "confidence": 0.0,
        "sub_scores": {},
        "blocker": blocker or status,
        "threshold": (policy or {}).get("threshold"),
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": qual.get("audit_task_id"),
        "smoketest_task_id": qual.get("smoketest_task_id"),
        "merge_grade_task_id": qual.get("merge_grade_task_id"),
        "requested_probes": qual.get("requested_probes") or [],
        # Durable record of a product probe dropped at request time
        # (blocker="incompatible_probe"), carried forward from the pending record.
        "qualify_request_blocked": qual.get("qualify_request_blocked"),
        "qualify_request_blocked_at": qual.get("qualify_request_blocked_at"),
        "walkthrough_e2e_task_id": qual.get("walkthrough_e2e_task_id"),
        "walkthrough_e2e_result": qual.get("walkthrough_e2e_result"),
        "rubric_snapshot": (
            rubric_provenance.get("criteria") if rubric_provenance else None
        ),
        "rubric_fingerprint": (
            rubric_provenance.get("fingerprint") if rubric_provenance else None
        ),
        "rubric_provenance": rubric_provenance,
        "rubric_drift": rubric_drift,
        "rubric_provenance_error": provenance_error,
        "version_goal": qual.get("version_goal"),
        "contract_fingerprint": qual.get("contract_fingerprint"),
        # Deliberately absent: this attempt never gates on live categories and
        # never fabricates verdicts for categories outside its launched rubric.
        "category_verdicts": None,
        "residual_uncaptured_gap": None,
        "merge_grade": {
            "status": status,
            "blocked": True,
            "rubric_provenance": rubric_provenance,
            "rubric_drift": rubric_drift,
            "rubric_provenance_error": provenance_error,
        },
        "merge_grade_status": verdict_status,
        "merge_grade_coverage": None,
        "merge_grade_biggest_gap": None,
        # Per-rubric-episode re-drive counter, carried forward so the rubric
        # re-driver (_redrive_rubric_changed_qualification) can enforce its
        # bounded relaunch cap across the spawn→finalize→relaunch cycle.  Resets
        # to 0 naturally when a NON-rubric verdict lands (that record carries no
        # counter).
        "rubric_redrive_count": int(qual.get("rubric_redrive_count") or 0),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number}: goal/rubric decision "
            "contract was superseded while review was in flight"
            if blocker == "qualification_contract_superseded"
            else (
                f"qualification v{version.version_number}: acceptance rubric changed "
                "while the blind grade was in flight; launch a fresh grade"
                if status == "rubric_changed"
                else f"qualification v{version.version_number}: launched rubric provenance unknown"
            )
        ),
    )
    session.add(version)
    # Deliberately NO _record_qualification_verdict here (mirrors
    # _handle_infra_failure: "HOLD, no verdict recorded").  A rubric_changed /
    # rubric_provenance_unknown attempt measured nothing under the final
    # rubric — it must never be scored, ledgered as an immutable
    # QualificationVerdict row, or counted as a failed attempt in the readiness
    # aggregation.  last_qualification itself IS the hold's durable record; the
    # operator is notified below.
    await _notify_operator(
        session,
        version,
        (
            "release_rubric_changed"
            if status == "rubric_changed"
            else "release_rubric_unknown"
        ),
        (
            f"v{version.version_number} was not promoted because its goal/rubric "
            "decision contract changed while review was in flight. Strategic can "
            "decide whether to request a fresh qualification."
            if blocker == "qualification_contract_superseded"
            else (
                f"v{version.version_number} was not promoted because its acceptance "
                "rubric changed while the blind merge-grade was in flight. A fresh "
                "grade will use the new rubric."
                if status == "rubric_changed"
                else f"v{version.version_number} was held because the pending grade's "
                "rubric provenance could not be reconstructed. A fresh grade is required."
            )
        ),
        extra={
            "blocker": blocker or status,
            "rubric_drift": rubric_drift,
            "rubric_provenance_error": provenance_error,
        },
    )
    return {
        "status": status,
        "blocker": blocker or status,
        "rubric_drift": rubric_drift,
        "rubric_provenance_error": provenance_error,
    }

async def _finalize_qualification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    _pre_gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Finalize qualification once the active qualifier children have terminated.

    Invoked by ``_finalize_locked_version`` after the active audit, challenge,
    and merge-grade children for ``version`` have reached a terminal state.
    Archived qualifier-smoke metadata is retained on old records but is never
    loaded, scored, or used as a finalization prerequisite.

    Re-resolves CI for the candidate commit (``_resolve_ci_at_finalize``)
    rather than trusting the boolean frozen at spawn, reads the active child
    artifacts, computes the composite, then:
      * promote == True  and promotion_enabled ⇒ ``promote()``.
      * promote == True  but promotion_enabled is False ⇒ dry-run: log the
        composite, record it, leave the version open.
      * promote == False ⇒ record the blocker, keep the version open, set
        ``phase = "stabilize"``.


    Runs on whatever ``session`` it is given.  Non-promote paths leave their
    mutations for the caller to commit.  The promote path delegates to
    ``promote()``; successful promotion commits its canonical terminal state
    before post-commit follow-ups, while the caller's trailing commit remains a
    harmless boundary for the other paths.  Returns a result dict.
    """
    qual = dict(version.last_qualification or {})
    if _pre_gates is not None:
        qual["_finalize_observation_protocol"] = bool(
            _pre_gates.get("_observation_protocol")
        )
        if "hotfix_scope_preview" in _pre_gates:
            qual["_hotfix_scope_preview_observation"] = _pre_gates[
                "hotfix_scope_preview"
            ]
    attempt_id = _qualification_attempt_for_record(version, qual)
    qual["qualification_attempt_id"] = str(attempt_id)
    version.active_qualification_attempt_id = attempt_id
    if "retry_episode" not in qual:
        qual["retry_episode"] = int(
            getattr(version, "qualification_retry_episode", 0) or 0
        )
    # Attempt contract: read values frozen at spawn so mid-flight configuration
    # changes do not compose children spawned under one contract with a verdict
    # computed under another.  Legacy records that predate the freeze fall back
    # to the live values so existing finalizes continue to work.
    _frozen_version_goal: str = (
        str(qual["version_goal"]).strip()
        if "version_goal" in qual
        else (version.goal or "").strip()
    )
    _frozen_capability_proof_mode: str | None = (
        str(qual["capability_proof_mode"]).strip().lower()
        if "capability_proof_mode" in qual
        else None
    )
    candidate_sha = qual.get("candidate_sha")

    async def _finalize_respawn_authorship_receipt():
        if _pre_gates is not None:
            return _authorship_receipt_from_measurement(
                version,
                _pre_gates["candidate_authorship_measurement"],
                candidate_sha_override=candidate_sha,
            )
        # Direct unit-test compatibility.  Production finalization only enters
        # with a complete unlocked gate packet.
        return await _measured_authorship_receipt_for_audit_spawn(
            session,
            version,
            candidate_sha_override=candidate_sha,
        )

    audit_task_id = qual.get("audit_task_id")
    challenge_task_id = qual.get("challenge_task_id")
    audit_challenge_task_id = qual.get("audit_challenge_task_id") or challenge_task_id
    # Archival compatibility only. Preserve the attempt-time flag and task id on
    # terminal records, but never load or gate on that old child. New attempts
    # stamp false/None; legacy attempts retain their original metadata.
    smoketest_task_id = qual.get("smoketest_task_id")
    merge_grade_task_id = qual.get("merge_grade_task_id")
    try:
        requested_probes = _normalize_qualification_probes(
            qual.get("requested_probes")
        )
    except ValueError:
        return await _handle_infra_failure(
            session, version, qual, "qualification_probe_contract_invalid"
        )
    walkthrough_e2e_task_id = qual.get("walkthrough_e2e_task_id")
    # Computed by the requested-probe leg (which runs AFTER dimension
    # composition, see below) and folded into the promotion decision by
    # _apply_walkthrough_decision.  None when no probe was requested or the
    # probe evaluation has not run yet.
    walkthrough_e2e_result: dict[str, Any] | None = None
    smoketest_enabled = bool(qual.get("smoketest_enabled", False))
    # Dimensions already resolved (passed/failed/blocked) for this candidate_sha
    # from a prior finalize, carried forward by run_qualification from the
    # per-dimension ledger.  When a dimension is present here its gate evaluation
    # is composed from the ledger rather than re-run, and its child task may not
    # have been spawned (the spawn coordinator skipped it).
    # Defensive compatibility for attempts written before mutable dimensions
    # were excluded at spawn: never let a persisted legacy reuse marker suppress
    # the current CI/migration measurement during finalize.
    resolved_dims: dict[str, dict[str, Any]] = {
        dim: fact
        for dim, fact in (qual.get("resolved_dimensions") or {}).items()
        if dim in LEDGER_DIMENSION_KEYS and dim not in MUTABLE_DIMENSION_KEYS
    }

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

    ci_state = ci_resolution.get("state", _CI_RED)
    _policy_project = await session.get(Project, version.project_id)
    ci_required = True
    if _pre_gates is not None and "ci_required" in _pre_gates:
        ci_required = bool(_pre_gates["ci_required"])
    elif ci_state == _CI_NO_CHECKS:
        ci_required = _ci_required_for_finalize(_policy_project, ci_resolution)

    # Eager-write CI dimension so this fact survives a later gate's infra failure
    # that returns before _dual_write_dimension_results (line 5065).
    if candidate_sha:
        await _upsert_dimension_result(
            session, version, candidate_sha, "ci",
            status=_ci_dimension_status(
                ci_green=ci_green,
                ci_resolution=ci_resolution,
                ci_required=ci_required,
            ),
            detail=ci_resolution if isinstance(ci_resolution, dict) else None,
            origin_attempt_id=attempt_id,
        )

    # Prefer the promotion policy frozen at spawn; fall back to the live policy
    # for legacy records that predate the attempt-contract freeze.
    policy = qual.get("promotion_policy_snapshot")
    policy_source = qual.get("promotion_policy_source")
    policy_fallback_error = qual.get("promotion_policy_fallback_error")
    if not isinstance(policy, dict) or not policy:
        policy, policy_source, policy_fallback_error = _resolve_policy_with_provenance(
            _policy_project
        )
    attempt_policy_projection = promotion_policy_projection(
        policy,
        source=(str(policy_source) if policy_source else "legacy_snapshot"),
        fallback_error=(
            str(policy_fallback_error) if policy_fallback_error else None
        ),
    )

    # Derive whether enforce mode was active at spawn from the frozen (or fallback)
    # policy.  _apply_completion_debt_weight_mode baked the freeze-time mode into
    # weights.completion_debt, so a present positive value means enforce was active
    # when the policy was captured.  Used below for debt-scoring fallback (line 6393)
    # and the enforce preview gate (line 6584) so mid-flight knob changes cannot
    # omit the fail-closed debt score or produce metadata under the wrong contract.
    _frozen_cd_weight: float = float(
        (policy.get("weights") or {}).get("completion_debt", 0.0) or 0.0
    )
    _frozen_cd_was_enforce: bool = _frozen_cd_weight > 0.0

    # Resolve env_preflight_enabled EARLY — before the child-terminal-status
    # checks and artifact reads — so both can reference it.  The flag was
    # captured on the pending record at spawn; legacy records predating it
    # default to the policy fallback.
    env_preflight_enabled = bool(
        qual.get("env_preflight_enabled", policy.get("env_preflight_enabled", False))
    )

    # Resolve the launch contract before checking child infrastructure state.
    # Rubric drift is authoritative for this attempt: a lifecycle/text change
    # must be recorded even if a required child also failed while it ran.
    (
        launched_rubric,
        rubric_provenance,
        rubric_provenance_error,
    ) = await _resolve_launched_rubric(session, version, qual)
    if launched_rubric is None or rubric_provenance is None:
        return await _record_rubric_provenance_outcome(
            session,
            version,
            qual,
            status="rubric_provenance_unknown",
            rubric_provenance=None,
            provenance_error=rubric_provenance_error,
            policy=policy,
        )

    live_criterion_rows = await get_criteria_for_version(session, version.id)
    live_rubric = _canonicalize_rubric(
        [
            {
                "criterion_key": row.criterion_key,
                "text": row.text,
                "evidence_contract": row.evidence_contract,
            }
            for row in live_criterion_rows
            if row.status == "active"
        ]
    )
    live_lifecycle = [
        {"criterion_key": str(row.criterion_key), "status": str(row.status)}
        for row in live_criterion_rows
        if row.criterion_key
    ]
    rubric_drift = _bounded_rubric_drift(
        launched_rubric,
        live_rubric,
        qual.get("rubric_lifecycle_snapshot"),
        live_lifecycle,
    )
    if rubric_drift is not None:
        logger.info(
            "[RELEASE_QUALIFIER] v%d candidate %s rubric changed while the "
            "blind merge-grade was in flight: %s",
            version.version_number,
            (candidate_sha or "")[:8],
            rubric_drift,
        )
        return await _record_rubric_provenance_outcome(
            session,
            version,
            qual,
            status="rubric_changed",
            rubric_provenance=rubric_provenance,
            rubric_drift=rubric_drift,
            policy=policy,
        )

    live_goal = (version.goal or "").strip()
    live_contract_fingerprint = _qualification_contract_fingerprint(
        live_goal,
        _rubric_fingerprint(live_rubric),
        promotion_policy=qual.get("promotion_policy_snapshot"),
        capability_proof_mode=str(qual.get("capability_proof_mode") or "off"),
        env_preflight_enabled=bool(qual.get("env_preflight_enabled", False)),
        requested_probes=qual.get("requested_probes") or [],
    )
    launched_contract_fingerprint = qual.get("contract_fingerprint") or (
        _qualification_contract_fingerprint(
            str(qual.get("version_goal") or ""),
            str(qual.get("rubric_fingerprint") or ""),
            promotion_policy=qual.get("promotion_policy_snapshot"),
            capability_proof_mode=str(qual.get("capability_proof_mode") or "off"),
            env_preflight_enabled=bool(qual.get("env_preflight_enabled", False)),
            requested_probes=qual.get("requested_probes") or [],
        )
    )
    if live_contract_fingerprint != launched_contract_fingerprint:
        logger.info(
            "[RELEASE_QUALIFIER] v%d candidate %s goal/rubric contract changed "
            "while review was in flight; marking the attempt superseded",
            version.version_number,
            (candidate_sha or "")[:8],
        )
        return await _record_rubric_provenance_outcome(
            session,
            version,
            qual,
            status="rubric_changed",
            rubric_provenance=rubric_provenance,
            rubric_drift={
                "launched_contract_fingerprint": launched_contract_fingerprint,
                "live_contract_fingerprint": live_contract_fingerprint,
                "goal_changed": live_goal != str(qual.get("version_goal") or "").strip(),
            },
            policy=policy,
            blocker="qualification_contract_superseded",
        )

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
                    (
                        _respawn_authorship_receipt,
                        _respawn_authorship_range,
                    ) = await _finalize_respawn_authorship_receipt()
                    new_audit_task = await _spawn_release_audit_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=new_base_branch,
                        diff_range=new_diff_range,
                        candidate_sha=candidate_sha,
                        qualification_attempt_id=attempt_id,
                        audit_base_sha=fp.get("audit_base_sha"),
                        version_goal=_frozen_version_goal,
                        categories=fp.get("categories"),
                        rubric_fingerprint=fp.get("rubric_fingerprint"),
                        candidate_authorship_receipt=_respawn_authorship_receipt,
                        candidate_authorship_measured_range=_respawn_authorship_range,
                    )
                    new_count = rl_respawn_count + 1
                    updated_qual = {
                        **qual,
                        "audit_task_id": str(new_audit_task.id),
                        "audit_rl_respawn_count": new_count,
                    }
                    updated_qual = _advance_qualification_retry_episode(
                        version, updated_qual, cap=_MAX_AUDIT_RL_RESPAWNS
                    )
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
                    (
                        _respawn_authorship_receipt,
                        _respawn_authorship_range,
                    ) = await _finalize_respawn_authorship_receipt()
                    new_audit_task = await _spawn_release_audit_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=new_base_branch,
                        diff_range=new_diff_range,
                        candidate_sha=candidate_sha,
                        qualification_attempt_id=attempt_id,
                        audit_base_sha=fp.get("audit_base_sha"),
                        version_goal=_frozen_version_goal,
                        categories=fp.get("categories"),
                        rubric_fingerprint=fp.get("rubric_fingerprint"),
                        candidate_authorship_receipt=_respawn_authorship_receipt,
                        candidate_authorship_measured_range=_respawn_authorship_range,
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
                    updated_qual = _advance_qualification_retry_episode(
                        version, updated_qual, cap=_MAX_AUDIT_RL_RESPAWNS
                    )
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
                # ADMIN_CANCEL on a CANCELLED audit child (reaper cascade, admin
                # action, dedup, force-finalize) is a transient external
                # interruption — candidate quality was never evaluated.  Respawn
                # the same candidate_sha, parallel to RATE_LIMIT / infra respawn.
                admin_cancel_respawn_count = qual.get(
                    "audit_admin_cancel_respawn_count", 0
                )
                if (
                    _audit_task_obj.status == TaskStatus.CANCELLED
                    and _audit_explicit_cat == ADMIN_CANCEL
                    and admin_cancel_respawn_count < _MAX_AUDIT_RL_RESPAWNS
                ):
                    fp = _audit_task_obj.first_payload or {}
                    _project = await session.get(Project, version.project_id)
                    _integration_branch = get_project_integration_branch(_project)
                    new_base_branch = fp.get("base_branch", "") or _integration_branch
                    new_diff_range = (
                        fp.get("diff_range", "")
                        or f"{_integration_branch}...{candidate_sha}"
                    )
                    (
                        _respawn_authorship_receipt,
                        _respawn_authorship_range,
                    ) = await _finalize_respawn_authorship_receipt()
                    new_audit_task = await _spawn_release_audit_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=new_base_branch,
                        diff_range=new_diff_range,
                        candidate_sha=candidate_sha,
                        qualification_attempt_id=attempt_id,
                        audit_base_sha=fp.get("audit_base_sha"),
                        version_goal=_frozen_version_goal,
                        categories=fp.get("categories"),
                        rubric_fingerprint=fp.get("rubric_fingerprint"),
                        candidate_authorship_receipt=_respawn_authorship_receipt,
                        candidate_authorship_measured_range=_respawn_authorship_range,
                    )
                    new_count = admin_cancel_respawn_count + 1
                    updated_qual = append_infra_failure_child_event(
                        qual,
                        leg="audit",
                        failed_task=_audit_task_obj,
                        replacement_task_id=str(new_audit_task.id),
                        label="audit_admin_cancel_respawned",
                        respawn_count=new_count,
                    )
                    updated_qual = {
                        **updated_qual,
                        "audit_task_id": str(new_audit_task.id),
                        "audit_admin_cancel_respawn_count": new_count,
                    }
                    updated_qual = _advance_qualification_retry_episode(
                        version, updated_qual, cap=_MAX_AUDIT_RL_RESPAWNS
                    )
                    version.last_qualification = updated_qual
                    flag_modified(version, "last_qualification")
                    session.add(version)
                    await session.flush()
                    logger.info(
                        "[RELEASE_QUALIFIER] v%d audit task %s was admin-cancelled; "
                        "re-spawned as %s (respawn %d/%d)",
                        version.version_number,
                        audit_task_id,
                        new_audit_task.id,
                        new_count,
                        _MAX_AUDIT_RL_RESPAWNS,
                    )
                    return {
                        "status": "audit_admin_cancel_respawned",
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
                if (
                    _audit_task_obj.status == TaskStatus.CANCELLED
                    and _audit_explicit_cat == ADMIN_CANCEL
                    and admin_cancel_respawn_count >= _MAX_AUDIT_RL_RESPAWNS
                ):
                    infra_label = "audit_admin_cancel_respawn_cap_exceeded"
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
                # An ADMIN_CANCEL child is an external operator/reaper supersession,
                # not a pipeline infrastructure failure.  Return a neutral hold so the
                # attempt counter and consecutive_infra_errors streak are NOT advanced.
                if (
                    (_merge_grade_task_obj.context or {}).get("failure_category")
                    == ADMIN_CANCEL
                ):
                    logger.info(
                        "[RELEASE_QUALIFIER] v%d merge-grade task %s was "
                        "ADMIN_CANCEL'd; returning neutral hold (no infra-failure "
                        "penalty)",
                        version.version_number,
                        merge_grade_task_id,
                    )
                    return {"status": "merge_grade_admin_cancel_hold"}
                _merge_explicit_cat = (_merge_grade_task_obj.context or {}).get(
                    "failure_category", ""
                ) or (_merge_grade_task_obj.context or {}).get("failure_class", "")
                merge_grade_infra_respawn_count = qual.get(
                    "merge_grade_infra_respawn_count", 0
                )
                # --- Merge-grade PROVISION_FAILURE / AGENT_TIMEOUT: bounded respawn ---
                if (
                    _merge_grade_task_obj.status == TaskStatus.FAILED
                    and _merge_explicit_cat in (PROVISION_FAILURE, AGENT_TIMEOUT)
                    and merge_grade_infra_respawn_count < _MAX_AUDIT_RL_RESPAWNS
                ):
                    _merge_fp = _merge_grade_task_obj.first_payload or {}
                    _merge_project = await session.get(
                        Project, version.project_id
                    )
                    _merge_int_branch = get_project_integration_branch(
                        _merge_project
                    )
                    _merge_base = (
                        _merge_fp.get("base_branch", "") or _merge_int_branch
                    )
                    _merge_range = (
                        _merge_fp.get("diff_range", "")
                        or f"{_merge_int_branch}...{candidate_sha}"
                    )
                    _merge_new_task = await _spawn_release_merge_grade_task(
                        session,
                        version.project_id,
                        version_id=version.id,
                        base_branch=_merge_base,
                        diff_range=_merge_range,
                        candidate_sha=candidate_sha,
                        qualification_attempt_id=attempt_id,
                        version_goal=(
                            _merge_fp.get("version_goal")
                            or _frozen_version_goal
                        ),
                        merged_pr_numbers=list(
                            _merge_fp.get("merged_prs") or []
                        ),
                        categories=_merge_fp.get("categories"),
                        rubric_fingerprint=_merge_fp.get("rubric_fingerprint"),
                    )
                    _merge_new_count = merge_grade_infra_respawn_count + 1
                    _merge_updated_qual = append_infra_failure_child_event(
                        qual,
                        leg="merge_grade",
                        failed_task=_merge_grade_task_obj,
                        replacement_task_id=str(_merge_new_task.id),
                        label="merge_grade_infra_respawned",
                        respawn_count=_merge_new_count,
                    )
                    _merge_updated_qual = {
                        **_merge_updated_qual,
                        "merge_grade_task_id": str(_merge_new_task.id),
                        "merge_grade_infra_respawn_count": _merge_new_count,
                    }
                    _merge_updated_qual = _advance_qualification_retry_episode(
                        version,
                        _merge_updated_qual,
                        cap=_MAX_AUDIT_RL_RESPAWNS,
                    )
                    version.last_qualification = _merge_updated_qual
                    flag_modified(version, "last_qualification")
                    session.add(version)
                    await session.flush()
                    logger.info(
                        "[RELEASE_QUALIFIER] v%d merge-grade task %s failed "
                        "with %s; re-spawned as %s (respawn %d/%d)",
                        version.version_number,
                        merge_grade_task_id,
                        _merge_explicit_cat,
                        _merge_new_task.id,
                        _merge_new_count,
                        _MAX_AUDIT_RL_RESPAWNS,
                    )
                    return {
                        "status": "merge_grade_infra_respawned",
                        "respawn_count": _merge_new_count,
                    }
                _merge_infra_label = _enrich_infra_error(
                    f"merge_grade_task_{_merge_grade_task_obj.status.lower()}",
                    _merge_grade_task_obj,
                )
                if (
                    _merge_grade_task_obj.status == TaskStatus.FAILED
                    and _merge_explicit_cat in (PROVISION_FAILURE, AGENT_TIMEOUT)
                    and merge_grade_infra_respawn_count >= _MAX_AUDIT_RL_RESPAWNS
                ):
                    _merge_infra_label = (
                        "merge_grade_infra_respawn_cap_exceeded"
                    )
                return await _handle_infra_failure(
                    session,
                    version,
                    qual,
                    _merge_infra_label,
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
    _candidate_surface_paths = qual.get("audited_surface_paths")
    _resolved_audit_detail: dict[str, Any] = {}
    _prior_delta_audit_row = None
    _prior_delta_erosion_row = None
    _prior_delta_sha = str(qual.get("prior_audit_candidate_sha") or "").strip()
    if _prior_delta_sha and not _audit_and_erosion_resolved:
        _prior_delta_audit_row = await _load_dimension_result(
            session, version, _prior_delta_sha, "audit"
        )
        _prior_delta_erosion_row = await _load_dimension_result(
            session, version, _prior_delta_sha, "test_erosion"
        )
    if _audit_and_erosion_resolved:
        audit_report: dict[str, Any] | None = None
        # A fresh holistic child may accompany reused code-quality/test facts
        # when only the goal/rubric contract changed. Its report supplies goal
        # and category evidence; the durable dimension facts below remain the
        # source of truth for code quality and test erosion.
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
            if (
                audit_report is None
                and _audit_task_obj is not None
                and _audit_task_obj.status == TaskStatus.COMPLETED
            ):
                return await _handle_infra_failure(
                    session, version, qual, "holistic_review_report_missing"
                )
        audit_score = resolved_dims["audit"].get("sub_score")
        test_erosion_score = resolved_dims["test_erosion"].get("sub_score")
        _resolved_audit_detail_raw = resolved_dims["audit"].get("detail")
        if isinstance(_resolved_audit_detail_raw, dict):
            _resolved_audit_detail = _resolved_audit_detail_raw
        # A resolved test_erosion fact with status "blocked" carries the
        # hard-blocker sentinel (sub_score None == test_file_deleted); it must
        # propagate as None so compute_composite re-raises the block, never
        # coerce to a clean 1.0 (the same-SHA ledger-reuse fail-open).  Only a
        # non-blocked reuse whose score is genuinely absent falls back to the
        # neutral 1.0.
        if (
            test_erosion_score is None
            and resolved_dims["test_erosion"].get("status") != "blocked"
        ):
            test_erosion_score = 1.0
        audit_findings = list(_resolved_audit_detail.get("audit_findings") or [])
        excluded_audit_findings = list(
            _resolved_audit_detail.get("excluded_audit_findings") or []
        )
        _resolved_erosion_detail = resolved_dims["test_erosion"].get("detail")
        test_erosion_detail = (
            dict(_resolved_erosion_detail)
            if isinstance(_resolved_erosion_detail, dict)
            else {}
        )
        audit_missing_reason = _resolved_audit_detail.get("audit_missing_reason")
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

        if _prior_delta_audit_row is not None:
            audit_report, _ = _merge_scoped_audit_report_with_prior_fact(
                audit_report,
                audit_report,
                _prior_delta_audit_row.detail,
                expected_base_sha=_prior_delta_sha,
                candidate_surface_paths=qual.get("audited_surface_paths"),
            )
        audit_score, test_erosion_score = _score_audit(
            audit_report, candidate_surface_paths=_candidate_surface_paths
        )
        test_erosion_detail = _extract_test_erosion_detail(audit_report)
        if (
            _prior_delta_erosion_row is not None
            and isinstance(audit_report, dict)
            and _audit_report_confirms_incremental_base(audit_report, _prior_delta_sha)
        ):
            test_erosion_score, test_erosion_detail = (
                _merge_scoped_test_erosion_with_prior_fact(
                    audit_report,
                    _prior_delta_erosion_row.detail,
                    prior_blocked=_prior_delta_erosion_row.status == "blocked",
                )
            )
        # Carry the findings (not just the score) into last_qualification so the
        # strategic agent can target them — otherwise the audit sub-score, usually
        # the binding constraint, is opaque to the loop.
        audit_findings = _extract_audit_findings(
            audit_report, candidate_surface_paths=_candidate_surface_paths
        )
        # Carry the higher-severity findings the verification gate EXCLUDED from
        # scoring — for display only, marked unverified — so an audit whose
        # sub-score was inflated by dropped findings is rendered honestly instead of
        # reading as clean. Never confirmed blockers.  Use original report (the
        # unverified-primary gap is independent of the challenge reconciliation).
        excluded_audit_findings = _extract_excluded_audit_findings(
            audit_report, candidate_surface_paths=_candidate_surface_paths
        )
        # Carry the specific test-erosion detail (deleted files / negative deltas)
        # so the agent can target it when test-erosion is the binding constraint,
        # the same way it sees audit_findings.
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

    # Goal and acceptance-category grading has no scoped carry-forward, so the
    # release_audit report MUST have graded the full version delta the
    # promotion would carry.  The agent echoes the range it actually graded
    # into audit_scope.goal_category_diff_range (pinned at spawn to
    # base_branch...candidate_sha); a present echo that disagrees means a
    # narrowed goal/category window — hold rather than score a narrowed grade.
    if isinstance(audit_report, dict) and candidate_sha:
        _expected_goal_category_range = (
            f"{_lts_branch_names(_policy_project)[1]}...{candidate_sha}"
        )
        if not _audit_report_confirms_goal_category_range(
            audit_report, _expected_goal_category_range
        ):
            _echoed_goal_category_range = (audit_report.get("audit_scope") or {}).get(
                "goal_category_diff_range"
            )
            logger.warning(
                "[RELEASE_QUALIFIER] v%d candidate %s: release_audit report "
                "echoed goal_category_diff_range %r but expected the full "
                "version delta %r — holding",
                version.version_number,
                str(candidate_sha)[:8],
                _echoed_goal_category_range,
                _expected_goal_category_range,
            )
            return await _handle_infra_failure(
                session, version, qual, "audit_goal_category_range_narrowed"
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
        detail=_audit_dimension_detail(
            surface_paths=qual.get("audited_surface_paths"),
            audit_findings=audit_findings,
            excluded_audit_findings=excluded_audit_findings,
            findings_meta=_audit_findings_carry_meta(
                audit_report, candidate_surface_paths=_candidate_surface_paths
            ),
            audit_missing_reason=audit_missing_reason,
            audit_scope=(audit_report or {}).get("audit_scope"),
            holistic_review=audit_report if isinstance(audit_report, dict) else None,
            holistic_contract_fingerprint=qual.get("contract_fingerprint"),
        ),
        producing_task_id=audit_task_id,
        audited_surface_hash=qual.get("candidate_diff_hash"),
        origin_attempt_id=attempt_id,
    )
    _te_status = "blocked" if test_erosion_score is None else "passed"
    await _upsert_dimension_result(
        session, version, candidate_sha, "test_erosion",
        status=_te_status,
        sub_score=test_erosion_score,
        detail=test_erosion_detail,
        producing_task_id=audit_task_id,
        origin_attempt_id=attempt_id,
    )


    # Candidate-side env-preflight evidence is produced directly and does not
    # depend on a smoketest task or artifact.
    _env_preflight_raw = qual.get("env_preflight_evidence")
    env_preflight = sanitize_env_preflight_artifact(_env_preflight_raw)
    # DEPLOYED-side env-preflight judge (Slice 3, design 245212a4), DARK by
    # default.  The flag was resolved above (before the smoketest result read)
    # so the deployment-time checks in this section (env_preflight_score,
    # env_preflight_missing, ack_override) can reference it.  When the flag is
    # False every downstream use is inert (score/missing ignored, no
    # boot_prereq_missing blocker, no sub_score, no verdict blob).
    env_preflight_score, env_preflight_missing, env_preflight_scope = (
        _score_env_preflight(
            env_preflight, _deployed_env_presence_snapshot(env_preflight)
        )
    )
    # Presence-only verdict blob — recorded ONLY when the judge is enabled, so a
    # dark run leaves last_qualification byte-for-byte unchanged.  ``required`` is
    # the candidate-declared [{name, scope}] list; ``missing`` is NAMES only.
    env_preflight_verdict: dict[str, Any] | None = None
    if env_preflight_enabled and isinstance(env_preflight, dict):
        env_preflight_verdict = {
            "required": env_preflight.get("required_env") or [],
            "missing": env_preflight_missing,
            "deploy_target_scope": env_preflight_scope,
        }

    # Slice 4 (design 245212a4): OWNER-armed ack that the missing boot
    # prerequisite will be bound in the same deploy revision.  The override
    # applies ONLY when the env-preflight judge is enabled AND it actually found
    # a missing prerequisite (otherwise there is nothing to override and the ack
    # stays armed for a later run).  It suppresses exactly the boot_prereq_missing
    # block inside compute_composite and NO other gate.  Presence-only: the ack
    # marker carries who/when, never any env value.
    _env_ack = qual.get("ack_env_will_be_bound")
    env_preflight_ack_override = bool(
        _env_ack and env_preflight_enabled and env_preflight_missing
    )
    env_preflight_ack_applied: dict[str, Any] | None = None
    if env_preflight_ack_override and isinstance(_env_ack, dict):
        env_preflight_ack_applied = {
            "actor": _env_ack.get("actor"),
            "acknowledged_at": _env_ack.get("acknowledged_at"),
            "overridden_blocker": "boot_prereq_missing",
            # Presence-only: NAMES of the acknowledged-missing prerequisites,
            # never any value.
            "missing": [str(n) for n in env_preflight_missing],
        }

    primary_findings = _primary_findings_for_challenge_retry(audit_report)

    # If the qualifying run intentionally deferred the challenge spawn, do the
    # first binding spawn now that the audit report exists.  This is the point
    # where the initial release_audit_challenge finally has the audit findings
    # needed to copy primary summaries verbatim into the prompt.
    if (
            bool(qual.get("audit_challenge_spawn_pending"))
            and not challenge_task_id
            # A second opinion is exceptional: only verified, deductible
            # consequential findings justify another LLM review.  A clean or
            # low-severity primary review finalizes without a challenger.
            and bool(primary_findings)
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
                qualification_attempt_id=attempt_id,
                primary_audit_task_id=audit_task_id,
                primary_findings=primary_findings,
                version_goal=_frozen_version_goal,
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
        # are caught before artifact reads (consistent with other active children).
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
                    _chal_explicit_cat = (_challenge_task_obj.context or {}).get(
                        "failure_category", ""
                    ) or (_challenge_task_obj.context or {}).get("failure_class", "")
                    challenge_infra_respawn_count = qual.get(
                        "challenge_infra_respawn_count", 0
                    )
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
                                        qualification_attempt_id=attempt_id,
                                        primary_audit_task_id=audit_task_id,
                                        primary_findings=_chal_primary_findings,
                                        version_goal=_frozen_version_goal,
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
                                version.last_qualification = _advance_qualification_retry_episode(
                                    version, version.last_qualification, cap=_MAX_AUDIT_RL_RESPAWNS
                                )
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
                    # ADMIN_CANCEL on a CANCELLED challenge child — respawn
                    # the same candidate, parallel to PROVISION_FAILURE/AGENT_TIMEOUT.
                    challenge_admin_cancel_respawn_count = qual.get(
                        "challenge_admin_cancel_respawn_count", 0
                    )
                    if (
                        _challenge_task_obj.status == TaskStatus.CANCELLED
                        and _chal_explicit_cat == ADMIN_CANCEL
                        and challenge_admin_cancel_respawn_count < _MAX_AUDIT_RL_RESPAWNS
                    ):
                        _chal_payload = _challenge_task_obj.first_payload or {}
                        _audit_payload = (
                            _audit_task_obj.first_payload
                            if _audit_task_obj is not None
                            else {}
                        )
                        _chal_project2 = await session.get(Project, version.project_id)
                        _, _chal_promo_target2 = (
                            _lts_branch_names(_chal_project2)
                            if _chal_project2 is not None
                            else ("", "main")
                        )
                        _chal_base_branch2 = (
                            _chal_payload.get("base_branch")
                            or _audit_payload.get("base_branch")
                            or _chal_promo_target2
                        )
                        _chal_diff_range2 = (
                            _chal_payload.get("diff_range")
                            or _audit_payload.get("diff_range")
                            or f"{_chal_base_branch2}...{candidate_sha}"
                        )
                        _chal_primary_findings2 = _primary_findings_for_challenge_retry(
                            audit_report
                        )
                        if _chal_primary_findings2:
                            from app.payload_schemas import PayloadTooLargeError

                            try:
                                _new_challenge_task2 = (
                                    await _spawn_release_audit_challenge_task(
                                        session,
                                        version.project_id,
                                        version_id=version.id,
                                        base_branch=_chal_base_branch2,
                                        diff_range=_chal_diff_range2,
                                        candidate_sha=candidate_sha,
                                        qualification_attempt_id=attempt_id,
                                        primary_audit_task_id=audit_task_id,
                                        primary_findings=_chal_primary_findings2,
                                        version_goal=_frozen_version_goal,
                                    )
                                )
                            except PayloadTooLargeError as _chal_exc2:
                                logger.warning(
                                    "[RELEASE_QUALIFIER] v%d challenge admin-cancel respawn "
                                    "skipped — primary_findings payload too large "
                                    "(%s); routing to infra_failure",
                                    version.version_number,
                                    _chal_exc2,
                                )
                            else:
                                _chal_ac_new_count = challenge_admin_cancel_respawn_count + 1
                                _chal_ac_updated_qual = append_infra_failure_child_event(
                                    qual,
                                    leg="challenge",
                                    failed_task=_challenge_task_obj,
                                    replacement_task_id=str(_new_challenge_task2.id),
                                    label="challenge_admin_cancel_respawned",
                                    respawn_count=_chal_ac_new_count,
                                )
                                version.last_qualification = {
                                    **_chal_ac_updated_qual,
                                    "status": "pending",
                                    "challenge_task_id": str(_new_challenge_task2.id),
                                    "audit_challenge_task_id": str(
                                        _new_challenge_task2.id
                                    ),
                                    "challenge_admin_cancel_respawn_count": _chal_ac_new_count,
                                }
                                version.last_qualification = _advance_qualification_retry_episode(
                                    version,
                                    version.last_qualification,
                                    cap=_MAX_AUDIT_RL_RESPAWNS,
                                )
                                flag_modified(version, "last_qualification")
                                session.add(version)
                                await session.flush()
                                logger.info(
                                    "[RELEASE_QUALIFIER] v%d challenge task %s was "
                                    "admin-cancelled; re-spawned as %s (respawn %d/%d)",
                                    version.version_number,
                                    challenge_task_id,
                                    _new_challenge_task2.id,
                                    _chal_ac_new_count,
                                    _MAX_AUDIT_RL_RESPAWNS,
                                )
                                return {
                                    "status": "challenge_admin_cancel_respawned",
                                    "respawn_count": _chal_ac_new_count,
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
                    if (
                        _challenge_task_obj.status == TaskStatus.CANCELLED
                        and _chal_explicit_cat == ADMIN_CANCEL
                        and challenge_admin_cancel_respawn_count >= _MAX_AUDIT_RL_RESPAWNS
                    ):
                        _challenge_infra_label = "challenge_admin_cancel_respawn_cap_exceeded"
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
                    qualification_attempt_id=attempt_id,
                    primary_audit_task_id=audit_task_id,
                    primary_findings=primary_findings,
                    version_goal=_frozen_version_goal,
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
                version.last_qualification = _advance_qualification_retry_episode(
                    version, version.last_qualification, cap=_MAX_AUDIT_CHALLENGE_BINDING_RESPAWNS
                )
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
                        qualification_attempt_id=attempt_id,
                        primary_audit_task_id=audit_task_id,
                        primary_findings=primary_findings,
                        version_goal=_frozen_version_goal,
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
                version.last_qualification = _advance_qualification_retry_episode(
                    version, version.last_qualification, cap=_MAX_AUDIT_CHALLENGE_BINDING_RESPAWNS
                )
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
        _audit_report_for_excluded = audit_report
        prior_audit_candidate_sha = qual.get("prior_audit_candidate_sha")
        if prior_audit_candidate_sha:
            prior_audit_row = await _load_dimension_result(
                session,
                version,
                str(prior_audit_candidate_sha),
                "audit",
            )
            if prior_audit_row is not None:
                (
                    _audit_report_scored,
                    _audit_report_for_excluded,
                ) = _merge_scoped_audit_report_with_prior_fact(
                    _audit_report_scored,
                    audit_report,
                    prior_audit_row.detail,
                    expected_base_sha=str(prior_audit_candidate_sha),
                    candidate_surface_paths=qual.get("audited_surface_paths"),
                )
        audit_score, _delta_test_erosion_score = _score_audit(
            _audit_report_scored,
            candidate_surface_paths=_candidate_surface_paths,
        )
        if prior_audit_candidate_sha:
            prior_erosion_row = await _load_dimension_result(
                session, version, str(prior_audit_candidate_sha), "test_erosion"
            )
            if (
                prior_erosion_row is not None
                and isinstance(audit_report, dict)
                and _audit_report_confirms_incremental_base(
                    audit_report, str(prior_audit_candidate_sha)
                )
            ):
                test_erosion_score, test_erosion_detail = (
                    _merge_scoped_test_erosion_with_prior_fact(
                        audit_report,
                        prior_erosion_row.detail,
                        prior_blocked=prior_erosion_row.status == "blocked",
                    )
                )
            else:
                test_erosion_score = _delta_test_erosion_score
        else:
            test_erosion_score = _delta_test_erosion_score
        # Carry the confirmed (actionable) findings into last_qualification so the
        # strategic agent can target them.  Use reconciled report when available so
        # refuted findings are not surfaced as actionable blockers.
        audit_findings = _extract_audit_findings(
            _audit_report_scored,
            candidate_surface_paths=_candidate_surface_paths,
        )
        excluded_audit_findings = _extract_excluded_audit_findings(
            _audit_report_for_excluded,
            candidate_surface_paths=_candidate_surface_paths,
        )
        # Pre-cap totals + truncation flags for both carried lists. Verified meta is
        # computed on the reconciled report (matches audit_findings just above);
        # excluded meta on the original report (matches excluded_audit_findings,
        # which is derived from audit_report — the unverified-primary gap is
        # independent of challenge reconciliation).
        _verified_findings_meta = _audit_findings_carry_meta(
            _audit_report_scored,
            candidate_surface_paths=_candidate_surface_paths,
        )
        _excluded_findings_meta = _audit_findings_carry_meta(
            _audit_report_for_excluded,
            candidate_surface_paths=_candidate_surface_paths,
        )
        # Build the reconciliation record on the RECONCILED report so
        # findings_derived_score agrees with the applied audit_score.  The challenge
        # synthesis is derived from the binding verdict artifact (new gate) — kept
        # separate from excluded_findings_count (verification-gate drops) so they
        # are never double-counted.
        audit_reconciliation = {
            **_compute_audit_reconciliation(
                _audit_report_scored,
                candidate_surface_paths=_candidate_surface_paths,
            ),
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
        # Refresh the per-dimension audit ledger row with the POST-challenge-
        # reconciliation score (finding 7c6c3428ff6afee2).  The eager upsert
        # earlier in finalize ran BEFORE the challenge gate and therefore stored
        # the PRE-reconciliation audit_score; reconciliation has now dropped
        # challenger-refuted findings and re-scored the report, so overwrite that
        # stale fact.  Without this the ledger permanently freezes the
        # pre-reconciliation value (the later _dual_write_dimension_results skips
        # audit because its row is already RESOLVED), and the read-side
        # sub_scores overlay — patch_qualification_state_from_dimension_states —
        # then clobbers the reconciled sub_scores.audit with that stale value.
        # Both statuses are RESOLVED, so the primitive's downgrade guard permits
        # this update.
        if audit_score is None:
            _reconciled_audit_status = "blocked"
        elif audit_score < 1.0:
            _reconciled_audit_status = "failed"
        else:
            _reconciled_audit_status = "passed"
        await _upsert_dimension_result(
            session, version, candidate_sha, "audit",
            status=_reconciled_audit_status,
            sub_score=audit_score,
            detail=_audit_dimension_detail(
                surface_paths=qual.get("audited_surface_paths"),
                audit_findings=audit_findings,
                excluded_audit_findings=excluded_audit_findings,
                findings_meta={
                    **_verified_findings_meta,
                    **_excluded_findings_meta,
                },
                audit_missing_reason=audit_missing_reason,
                holistic_review=audit_report if isinstance(audit_report, dict) else None,
                holistic_contract_fingerprint=qual.get("contract_fingerprint"),
                audit_scope=(audit_report or {}).get("audit_scope"),
            ),
            producing_task_id=audit_task_id,
            audited_surface_hash=qual.get("candidate_diff_hash"),
            origin_attempt_id=attempt_id,
        )
    else:
        # audit+test_erosion resolved from ledger — reconciliation fields are
        # not meaningful (no challenge ran, no audit report exists).
        _verified_findings_meta = {
            "audit_findings_total": _resolved_audit_detail.get(
                "audit_findings_total", len(audit_findings)
            ),
            "audit_findings_truncated": bool(
                _resolved_audit_detail.get("audit_findings_truncated")
            ),
        }
        _excluded_findings_meta = {
            "excluded_audit_findings_total": _resolved_audit_detail.get(
                "excluded_audit_findings_total",
                len(excluded_audit_findings),
            ),
            "excluded_audit_findings_truncated": bool(
                _resolved_audit_detail.get("excluded_audit_findings_truncated")
            ),
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
    # Phase 1b (read-side cutover): if a prior finalize already resolved this
    # dimension at the same candidate_sha, reuse the stored verdict and skip the
    # live GitHub fetch — only 'passed'/'blocked' rows appear in resolved_dims
    # (_LEDGER_RESOLVED_STATUSES excludes 'unavailable'), so a prior defer never
    # short-circuits the live gate.
    if "migration_identity" in resolved_dims:
        _mi_ledger = resolved_dims["migration_identity"]
        migration_verdict = "block" if _mi_ledger["status"] == "blocked" else "pass"
        migration_detail = (_mi_ledger.get("detail") or {}).get("detail")
        logger.info(
            "[RELEASE_QUALIFIER] v%d candidate %s reusing ledger "
            "migration_identity=%s (skip live gate)",
            version.version_number,
            (candidate_sha or "")[:8],
            _mi_ledger["status"],
        )
    elif _pre_gates is not None:
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
            f"migration_identity_check_unavailable: {migration_detail}"
            if migration_detail
            else "migration_identity_check_unavailable",
        )
    migration_collision = migration_verdict == "block"
    # Eager-write migration_identity so a later gate's infra failure cannot
    # discard this verdict before _dual_write_dimension_results is reached.
    await _upsert_dimension_result(
        session,
        version,
        candidate_sha,
        "migration_identity",
        status="blocked" if migration_collision else "passed",
        sub_score=None,
        blocker="migration_identity_collision" if migration_collision else None,
        detail={
            "verdict": migration_verdict,
            "detail": migration_detail,
            "execution_site": "eager_write",
        },
        origin_attempt_id=attempt_id,
    )

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
    # Phase 1b (read-side cutover): same pattern as migration_identity above.
    if "migration_apply" in resolved_dims:
        _ma_ledger = resolved_dims["migration_apply"]
        apply_verdict = "block" if _ma_ledger["status"] == "blocked" else "pass"
        apply_detail = (_ma_ledger.get("detail") or {}).get("detail")
        logger.info(
            "[RELEASE_QUALIFIER] v%d candidate %s reusing ledger "
            "migration_apply=%s (skip live gate)",
            version.version_number,
            (candidate_sha or "")[:8],
            _ma_ledger["status"],
        )
    elif _pre_gates is not None:
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

            _migration_apply_infra_error(apply_detail),
        )
    migration_apply_blocked = apply_verdict == "block"
    if apply_verdict == "skipped":
        # Gate was never attempted (scratch DB not configured).  Record as
        # "unavailable" so a skipped gate can never read as a passed gate.
        _ma_status = "unavailable"
        _ma_blocker = None
    else:
        _ma_status = "blocked" if migration_apply_blocked else "passed"
        _ma_blocker = "migration_apply_drift" if migration_apply_blocked else None
    # Eager-write migration_apply so a later gate's infra failure cannot
    # discard this verdict before _dual_write_dimension_results is reached.
    _ma_detail: dict[str, Any] = {
        "verdict": apply_verdict,
        "detail": apply_detail,
        "execution_site": "eager_write",
    }
    if apply_verdict == "skipped" and apply_detail:
        # Include an explicit status so _ledger_fact_satisfies_promotion can
        # recognise this as a permanent non-applicable outcome rather than a
        # transient infra hold — keeps the ledger-completeness check from
        # stalling promotion on a deliberately skipped gate.
        _ma_detail["status"] = apply_detail
    await _upsert_dimension_result(
        session,
        version,
        candidate_sha,
        "migration_apply",
        status=_ma_status,
        sub_score=None,
        blocker=_ma_blocker,
        detail=_ma_detail,
        origin_attempt_id=attempt_id,
    )

    # Keep today's coverage summary for observability; gate on launched rubric.
    criterion_coverage = await get_version_coverage_summary(session, version.id)
    active_categories: list[dict[str, Any]] = [
        {
            "category_key": c.get("criterion_key"),
            "text": c.get("text"),
            **(
                {"evidence_contract": c["evidence_contract"]}
                if isinstance(c.get("evidence_contract"), dict)
                else {}
            ),
        }
        for c in launched_rubric
        if c.get("criterion_key")
    ]
    from app.release_prepromotion_rehearsal import (  # noqa: PLC0415
        validate_prepromotion_gradeability,
    )

    # Every active category can hard-block promotion.  Validate its evidence
    # producer phase before composing any category verdicts, so a newly added
    # criterion cannot introduce a producer/gate cycle that only wedges a
    # release attempt.
    validate_prepromotion_gradeability(active_categories)

    # Measure and grade the candidate's complete base-to-head range.  The
    # previously promoted serving receipt is intentionally not consulted here:
    # it is immutable while this candidate is blocked and would make the hard
    # gate impossible to clear by changing the candidate.
    authorship_measurement = (
        _pre_gates.get("candidate_authorship_measurement")
        if _pre_gates is not None
        else None
    )
    if authorship_measurement is None:
        authorship_measurement = await _measure_candidate_operator_authorship(
            session, version
        )
    candidate_base_sha, candidate_head_sha = candidate_authorship_range(version)
    if candidate_base_sha is None:
        # The version has no stored base_sha (write-once, best-effort at open
        # time). ``_measure_candidate_operator_authorship`` derives one from
        # the promotion target and reports it in the measurement's own range
        # -- trust that derived value here so the receipt is validated
        # against the SAME range that was actually measured, instead of
        # recomputing a still-null base and invalidating the receipt.
        _measured_range = (
            authorship_measurement.get("range")
            if isinstance(authorship_measurement, dict)
            else None
        )
        if isinstance(_measured_range, dict) and isinstance(
            _measured_range.get("base_sha"), str
        ) and _measured_range.get("head_sha") == candidate_head_sha:
            candidate_base_sha = _measured_range["base_sha"]
    candidate_authorship_receipt = authorship_receipt_for_measurement(
        authorship_measurement,
        candidate_base_sha,
        candidate_head_sha,
    )
    candidate_operator_authorship_gate = evaluate_candidate_operator_authorship_gate(
        version,
        authorship_measurement,
        active_categories,
        resolved_range=(candidate_base_sha, candidate_head_sha),
    )
    authorship_evidence = authorship_category_evidence(
        candidate_authorship_receipt,
        candidate_base_sha,
        candidate_head_sha,
        active_categories,
    )
    from app.release_prepromotion_rehearsal import (  # noqa: PLC0415
        apply_rehearsal_evidence,
        persisted_rehearsal_for_version,
        rehearsal_category_evidence,
    )
    prepromotion_rehearsal = await persisted_rehearsal_for_version(
        session,
        version,
        candidate_sha,
    )
    rehearsal_evidence = rehearsal_category_evidence(
        prepromotion_rehearsal,
        candidate_sha,
        active_categories,
    )
    authoritative_category_evidence = [
        *authorship_evidence,
        *rehearsal_evidence,
    ]
    _merge_grade_goal_usable = bool(_frozen_version_goal) and not is_contentless_goal(
        _frozen_version_goal
    )

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
    if (_merge_grade_goal_usable or bool(active_categories)) and "merge_grade" not in resolved_dims:
        # The primary release review is holistic.  It supplies both defect/test
        # evidence and the formerly separate blind merge-grade contract, so one
        # candidate decision consumes one LLM task rather than a fixed fan-out.
        # ``evaluate_merge_grade`` is also reached when the goal is empty/
        # contentless but an ACTIVE acceptance-criteria rubric exists: the rubric
        # is a gradeable contract on its own (the audit ships ``categories`` even
        # without a goal string), and its per-category verdicts must be graded and
        # gated instead of fail-closing the whole contract as never-graded.
        merge_grade_artifact: dict[str, Any] | None = (
            audit_report
            if isinstance(audit_report, dict)
            else (
                _resolved_audit_detail.get("holistic_review")
                if _resolved_audit_detail.get("holistic_contract_fingerprint")
                == qual.get("contract_fingerprint")
                else None
            )
        )
        # v2 machine tier: only gather the (bounded) verified-merge test evidence
        # when there is an ACTIVE rubric to grade categories against — otherwise
        # the per-category grade (and thus the tier) is never computed.
        _verified_merge_evidence = (
            await _collect_verified_merge_test_evidence(session, version)
            if active_categories
            else None
        )
        merge_grade_cert = evaluate_merge_grade(
            _frozen_version_goal,
            merge_grade_artifact,
            active_categories or None,
            _verified_merge_evidence,
            authoritative_category_evidence,
        )
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
        # skipped or waved through, exactly like the other active legs.
        if merge_grade_cert["status"] == "unparseable":
            label = (
                "holistic_review_report_missing"
                if merge_grade_artifact is None
                else "holistic_review_goal_grade_unparseable"
            )
            return await _handle_infra_failure(session, version, qual, label)
        # The grader produced a usable holistic verdict but CANNOT emit the
        # per-category section (stale agent image predating the per-category
        # protocol).  HOLD — never promote over an ungraded acceptance contract —
        # but do NOT fabricate a ``not_met`` verdict for every active criterion:
        # that would report a false reason ("graded, not met" when nothing was
        # graded) and, because the ledger is sha-KEYED, freeze the fabricated
        # failure against this candidate permanently.  Holding leaves the ledger
        # clean, so the SAME candidate re-qualifies honestly the moment a grader
        # that supports the protocol reaches the fleet.
        if merge_grade_cert["status"] == "category_grade_unsupported":
            logger.warning(
                "[RELEASE_QUALIFIER] v%d candidate %s: the release-audit/merge-grade "
                "agent returned no `category_verdicts` section while %d acceptance "
                "criteria are ACTIVE — the grader predates the per-category "
                "protocol (or the goal-less rubric contract). HOLDING (not "
                "fabricating not_met verdicts). Rebake the agent image from a ref "
                "carrying agent/src/handlers/audits.ts / merge-grade.ts per-category "
                "output.",
                version.version_number,
                (candidate_sha or "")[:8],
                len(active_categories or []),
            )
            return await _handle_infra_failure(
                session, version, qual, "merge_grade_grader_lacks_category_verdicts"
            )

    merge_grade_cert = {
        **merge_grade_cert,
        "rubric_provenance": rubric_provenance,
    }

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
    if (
        merge_grade_misses
        and _pre_gates is not None
        and _pre_gates.get("_observation_protocol")
        and ALREADY_ON_MAIN not in _pre_gates
    ):
        raise FinalizeObservationRequired(ALREADY_ON_MAIN)
    candidate_already_on_main = bool(
        _pre_gates.get(ALREADY_ON_MAIN)
    ) if _pre_gates is not None else await _candidate_already_on_main_at_finalize(
        session, version, candidate_sha
    )
    if merge_grade_misses and candidate_already_on_main:
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
    if _merge_grade_goal_usable or "merge_grade" in resolved_dims:
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
            detail=merge_grade_cert,
            producing_task_id=audit_task_id or merge_grade_task_id,
            origin_attempt_id=attempt_id,
        )

    # --- Goal-verification per-category ledger (redesign) -------------------
    # The acceptance criteria ARE the merge-grade's categories.  Persist this
    # candidate's per-category verdicts + residual into the sha-keyed ledger (the
    # verdict cache), then read the gate inputs back FROM the ledger so the gate
    # is a pure read that survives a re-drive (on a re-drive the merge_grade child
    # is not re-spawned; its verdicts are already in the ledger).
    #
    # Finding 85fa667a4d7fcae6 — merge-grade EXEMPTIONS (superseded_exempt /
    # already_on_main_exempt) exempt ONLY the HOLISTIC merge_grade_misses gate
    # (cleared above).  They do NOT exempt this per-category coverage gate: a
    # candidate whose ACTIVE acceptance criteria grade ``not_met`` must still
    # block on ``criterion_coverage_incomplete``, exactly as main's retired
    # unconditional coverage block did.  Skipping grading on the exempt path
    # (the prior ``not _merge_grade_exempt`` guard) silently loosened coverage —
    # a not_met candidate promoted just because its release was superseded or it
    # had already landed on the promotion target.  We now grade + persist + gate
    # on the SAME exempt candidates.  When the sibling ledger module is absent,
    # or there is no active rubric, the gate is still skipped (fallback to the
    # pre-redesign holistic-only path).
    _merge_grade_exempt = merge_grade_cert.get("status") in (
        "superseded_exempt",
        "already_on_main_exempt",
    )
    _gate_category_verdicts: list[dict[str, Any]] | None = None
    _gate_residual: str | None = None
    if _category_ledger is not None and active_categories:
        # The CURRENTLY-ACTIVE criterion keys.  A retracted / superseded criterion
        # keeps its (possibly ``not_met``) CategoryVerdict row in the ledger — the
        # row is never cleared — but it is no longer part of the acceptance
        # contract, so it must NOT gate.  Both the ledger read-back (FIX 1) and the
        # fail-closed in-memory fallback (FIX 2) below filter to this set, mirroring
        # ``category_verdict_ledger.is_candidate_fully_met`` (the sweep / reuse
        # guard) so the gate and the sweep never disagree.
        _active_keys = {
            c.get("category_key")
            for c in active_categories
            if c.get("category_key")
        }
        # The goal-verification gate only APPLIES when a merge-grade grade exists
        # for this candidate (fresh child this finalize, or a grade reused from
        # the per-dimension ledger on a re-drive).  When no merge-grade ran (e.g.
        # the version has no gradeable goal), the per-category verdicts are
        # legitimately absent and the gate stays inert — criteria are governed by
        # the evidence-coverage path, not this gate — so an empty/unreadable
        # read-back falls back to None (skip) rather than fail-closed.  Fail-closed
        # only guards the case where a grade DID run but its verdicts cannot be
        # trusted (absent for active keys, or the read raised).
        _merge_grade_ran = bool(merge_grade_task_id) or ("merge_grade" in resolved_dims)
        _expected_grader_run_id = merge_grade_task_id or resolved_dims.get(
            "merge_grade", {}
        ).get("producing_task_id")
        if _expected_grader_run_id is not None:
            _expected_grader_run_id = str(_expected_grader_run_id)
        _fresh_category_verdicts = merge_grade_cert.get("category_verdicts")
        if _fresh_category_verdicts is not None:
            _fresh_category_verdicts = apply_rehearsal_evidence(
                _fresh_category_verdicts,
                active_categories,
                prepromotion_rehearsal,
                candidate_sha,
            )
            # Fresh grade this finalize: write each category verdict + the residual.
            try:
                for _v in _fresh_category_verdicts:
                    _machine_provenance = _v.get("evidence_provenance")
                    await _category_ledger.upsert_category_verdict(
                        session,
                        version_id=version.id,
                        candidate_sha=candidate_sha,
                        category_key=_v.get("category_key"),
                        status=_v.get("status"),
                        tier=_v.get("tier", "judged"),
                        gap=_v.get("gap"),
                        citations=_v.get("citations") or [],
                        grader_run_id=(
                            None
                            if isinstance(_machine_provenance, dict)
                            else merge_grade_task_id
                        ),
                        evidence_provenance=_machine_provenance,
                        qualification_attempt_id=attempt_id,
                        project_id=version.project_id,
                    )
                await _category_ledger.upsert_residual(
                    session,
                    version.id,
                    candidate_sha,
                    merge_grade_cert.get("residual_uncaptured_gap"),
                    qualification_attempt_id=attempt_id,
                    project_id=version.project_id,
                )
            except Exception:  # noqa: BLE001 — ledger write failure must not crash finalize
                logger.warning(
                    "[RELEASE_QUALIFIER] v%d failed to persist per-category "
                    "goal-verification verdicts to the ledger — gating on the "
                    "freshly-computed in-memory verdicts (fail-closed) for this "
                    "finalize",
                    version.version_number,
                )
        if _fresh_category_verdicts is not None:
            # Gate on fresh evidence directly; this also fails closed if the
            # best-effort ledger write hiccupped or older same-SHA rows exist.
            # Fail-open state 1/3: the write failed AND the freshly-computed
            # verdicts do not cover any currently-active criterion (e.g. the
            # grader only graded now-retracted categories).  Absent evidence for
            # every active category is UNVERIFIED, not clean — fail closed rather
            # than skip the gate (None).
            _gate_category_verdicts = [
                cv
                for cv in (
                    _coerce_ledger_category_verdict(_v)
                    for _v in (_fresh_category_verdicts or [])
                )
                if cv.get("category_key") in _active_keys
            ] or _fail_closed_category_verdicts(_active_keys)
            _gate_residual = merge_grade_cert.get("residual_uncaptured_gap")
        else:
            # Read the gate inputs back FROM the ledger (canonical source; covers
            # the re-drive path where no fresh artifact was re-evaluated).
            try:
                _ledger_verdicts = await _category_ledger.get_category_verdicts(
                    session, version.id, candidate_sha,
                    qualification_attempt_id=attempt_id,
                )
                # FIX 1 — filter to the CURRENTLY-ACTIVE criterion keys.  The ledger
                # returns EVERY row for the (version, sha) pair, including a
                # retracted criterion's stale ``not_met`` row, which would otherwise
                # permanently re-block this gate even though the sweep's
                # ``is_candidate_fully_met`` (which filters the same way) reports the
                # candidate FULLY MET — a permanent finalize wedge + re-qual loop.
                # Fail-open state 2/3: the read-back succeeded but returned NO
                # row for any currently-active criterion.  When a merge-grade
                # grade ran for this candidate, that means active criteria were
                # never graded into the ledger — UNVERIFIED, not clean — so fail
                # closed rather than skip the gate (None).  When no grade ran the
                # gate is inert (evidence-coverage path governs), so skip.
                _gate_category_verdicts = [
                    cv
                    for cv in (
                        _coerce_ledger_category_verdict(_v)
                        for _v in (_ledger_verdicts or [])
                        if _expected_grader_run_id is None
                        or str(
                            _v.get("grader_run_id")
                            if isinstance(_v, dict)
                            else getattr(_v, "grader_run_id", None)
                        )
                        == _expected_grader_run_id
                    )
                    if cv.get("category_key") in _active_keys
                ] or (
                    _fail_closed_category_verdicts(_active_keys)
                    if _merge_grade_ran
                    else None
                )
                _gate_category_verdicts = apply_rehearsal_evidence(
                    _gate_category_verdicts,
                    active_categories,
                    prepromotion_rehearsal,
                    candidate_sha,
                )
                for _v in _gate_category_verdicts or []:
                    _machine_provenance = _v.get("evidence_provenance")
                    if not isinstance(_machine_provenance, dict):
                        continue
                    await _category_ledger.upsert_category_verdict(
                        session,
                        version_id=version.id,
                        candidate_sha=candidate_sha,
                        category_key=_v.get("category_key"),
                        status=_v.get("status"),
                        tier=_v.get("tier", "machine"),
                        gap=_v.get("gap"),
                        citations=_v.get("citations") or [],
                        grader_run_id=None,
                        evidence_provenance=_machine_provenance,
                        qualification_attempt_id=attempt_id,
                        project_id=version.project_id,
                    )
                _gate_residual = await _category_ledger.get_residual(
                    session, version.id, candidate_sha,
                    qualification_attempt_id=attempt_id,
                )
            except Exception:  # noqa: BLE001 — a read failure fails CLOSED (below)
                # Fail-open state 3/3: the ledger read itself raised, so the
                # verdicts are UNREADABLE.  When a grade ran for this candidate,
                # skipping the gate (None) would let a candidate promote on a
                # transient DB error masking the goal-verification result — fail
                # closed so an unreadable gate input blocks instead of promotes.
                # (No grade ran → gate inert → skip, matching the no-goal path.)
                logger.warning(
                    "[RELEASE_QUALIFIER] v%d failed to read per-category verdicts back "
                    "from the ledger — %s the goal-verification gate",
                    version.version_number,
                    "failing closed on" if _merge_grade_ran else "skipping (no grade ran)",
                )
                _gate_category_verdicts = (
                    _fail_closed_category_verdicts(_active_keys)
                    if _merge_grade_ran
                    else None
                )
                _gate_residual = None

        if _gate_category_verdicts is None:
            # FIX 3 — fail CLOSED on a NEVER-GRADED candidate.  We are inside the
            # guard, so this version HAS active acceptance criteria and is not
            # merge-grade exempt — yet not one active category carries a verdict for
            # this candidate.  A ``None`` gate input SKIPS the category gate, so
            # leaving it would promote over an acceptance contract that was never
            # graded at all.
            #
            # This is reachable whenever no merge-grade ran for the candidate — most
            # notably a version whose ``goal`` is empty/contentless (see
            # ``_merge_grade_goal_usable``), which spawns no merge-grade child and so
            # produces no verdicts.  Blanking the goal is a real operator lever, and
            # before the coverage gate was retired the pre-qualification coverage
            # precondition happened to hold such a version back; it no longer does.
            #
            # Fail closed, mirroring ``category_verdict_ledger.is_candidate_fully_met``
            # (an ACTIVE criterion with no verdict → NOT met), so the gate and the
            # sweep cannot disagree.  The gap text carries WHY.
            _gate_category_verdicts = _never_graded_category_verdicts(_active_keys) or None
            if _gate_category_verdicts:
                logger.warning(
                    "[RELEASE_QUALIFIER] v%d has %d ACTIVE acceptance criteria but no "
                    "merge-grade verdict for candidate %s — failing the category gate "
                    "CLOSED rather than promoting over an ungraded acceptance contract",
                    version.version_number,
                    len(_gate_category_verdicts),
                    (candidate_sha or "?")[:8],
                )

    # --- Capability-proof gate dimension (informational in OFF/OBSERVE, gating in ENFORCE) ---
    # Reflects whether the capability proof gate is active and — in ENFORCE
    # mode — whether all working capabilities carry machine-verified proof.
    # The dimension is always recorded in the ledger, but only blocks promotion
    # in ENFORCE mode when unproven capabilities exist.
    # Delegate to the shared IO-free primitive so this release-qualification
    # enforcement point derives from the SAME tiered proof map and exemption set
    # the spawn-time resolver and blocked-resume sweep use — one source of truth
    # for "unproven working capability blocks promotion" instead of an inline
    # re-implementation that can drift.
    from app.capability_registry import (
        capability_proof_gate_release_status as _cp_release_status,
    )

    # Prefer the mode frozen at spawn; fall back to live config for legacy records.
    _capability_proof_mode = _frozen_capability_proof_mode or str(
        _runtime_config.get("capability_proof_gate_mode", "off") or "off"
    ).strip().lower()
    capability_proof_blocked, capability_proof_detail = _cp_release_status(
        getattr(_policy_project, "problem_model", None),
        _capability_proof_mode,
        # Finding #1: pass the durable, platform-only proof ledger so the enforce
        # gate corroborates each "cleared" capability against machine-verified
        # evidence instead of trusting agent-writable problem_model evidence_refs
        # / proof_exempt claims.
        getattr(_policy_project, "capability_proof_records", None),
    )

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
    elif _frozen_cd_was_enforce:
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
        smoketest_score=None,  # legacy signature; qualifier smoke is inert
        policy=policy,
        smoketest_enabled=False,
        ci_state=ci_state,
        ci_required=ci_required,
        migration_collision=migration_collision,
        migration_apply_blocked=migration_apply_blocked,
        # Goal-verification redesign: the per-category verdicts + residual (read
        # from the sha-keyed ledger) drive the gate INSTEAD of the coverage-count
        # summary.  Both are None when there is no active rubric / the ledger is
        # absent / the grade was exempted — preserving the holistic-only path.
        category_verdicts=_gate_category_verdicts,
        residual_uncaptured_gap=_gate_residual,
        merge_grade_misses=merge_grade_misses,
        merge_grade_exempt=_merge_grade_exempt,
        debt_pressure_score=_debt_pressure_score,
        capability_proof_blocked=capability_proof_blocked,
        # Slice 3 (design 245212a4): DARK-by-default deployed-side env-preflight
        # judge.  Score/missing are ignored entirely when env_preflight_enabled
        # is False, preserving byte-for-byte default promotion behaviour.
        env_preflight_enabled=env_preflight_enabled,
        env_preflight_score=env_preflight_score,
        env_preflight_missing=env_preflight_missing,
        # Slice 4: OWNER-armed one-shot override of the boot_prereq_missing block
        # (scoped to that blocker only — see _env_ack above).
        env_preflight_ack_override=env_preflight_ack_override,
        candidate_operator_authorship_gate=candidate_operator_authorship_gate,
    )

    # --- Explicit product probe: decision-bearing walkthrough --------------
    # Runs AFTER the release_audit report was read and AFTER the composite was
    # composed, so a requested-but-not-passed probe fails ONLY its own dimension
    # (walkthrough_e2e → promote=False / blocker=walkthrough_e2e_failed via
    # _apply_walkthrough_decision below) instead of discarding the
    # already-computed audit / challenge / merge-grade verdicts — which are
    # already eager-written to the per-dimension ledger, so they survive an
    # infra hold too.
    #
    # INFRA-SHAPED probe failures — a missing/invalid/non-terminal/infra-terminal
    # child, a provenance-broken report (missing report / SHA mismatch / missing
    # results), or missing ingested evidence — route to _handle_infra_failure.
    # An agent-VM or clone failure in the probe must not be booked as a
    # conclusive quality judgment against the candidate: the infra_failure record
    # is non-conclusive and the release-qualifier re-driver re-runs the attempt
    # (spawning a fresh site-pinned child).
    #
    # REPORT-CONTRACT failures — the report itself violates the DEPLOYED
    # qualifier's evidence contract (schema mismatch, duplicate/invalid entries,
    # canonical collisions, or keys that do not resolve to the open version's
    # ACTIVE rubric) — are DEPLOYED-side hard blockers, not transient holds: the
    # validator runs in the serving process, so no fix on automain changes how
    # the report is parsed until this version itself promotes.  They book a
    # not_promoted verdict with blocker=walkthrough_e2e_contract_failed /
    # execution_site=deployed via _handle_walkthrough_contract_failure.
    #
    # A GENUINE criterion failure (report valid, every key resolves to an ACTIVE
    # criterion, one or more criteria failed) stays a QUALITY verdict —
    # promote=False, blocker=walkthrough_e2e_failed — folded into the
    # walkthrough_e2e dimension alone.
    if "walkthrough_e2e" in requested_probes:
        _walkthrough_reason: str | None = None
        if not walkthrough_e2e_task_id:
            _walkthrough_reason = "walkthrough_e2e_task_missing"
        else:
            try:
                _walkthrough_task_obj = await session.get(
                    Task, uuid.UUID(str(walkthrough_e2e_task_id))
                )
            except (ValueError, TypeError):
                _walkthrough_reason = "walkthrough_e2e_task_id_invalid"
                _walkthrough_task_obj = None
            if _walkthrough_reason is None:
                if _walkthrough_task_obj is None:
                    _walkthrough_reason = "walkthrough_e2e_task_missing"
                elif (
                    _walkthrough_task_obj.status
                    in _INFRA_FAILURE_TERMINAL_STATUSES
                ):
                    _walkthrough_reason = _enrich_infra_error(
                        f"walkthrough_e2e_task_{_walkthrough_task_obj.status.lower()}",
                        _walkthrough_task_obj,
                    )
                elif _walkthrough_task_obj.status != TaskStatus.COMPLETED:
                    _walkthrough_reason = "walkthrough_e2e_task_not_terminal"

        if _walkthrough_reason is not None:
            # Infra-shaped probe failure: the walkthrough lane itself broke
            # (missing/invalid/non-terminal/infra-terminal child), not the
            # candidate.  Hold the release via the non-conclusive infra_failure
            # path so the probe is re-driven rather than booked as a quality
            # verdict.
            return await _handle_infra_failure(
                session,
                version,
                qual,
                _walkthrough_reason,
            )

        walkthrough_report = await _latest_artifact_logs(
            session,
            uuid.UUID(str(walkthrough_e2e_task_id)),
            ArtifactType.WALKTHROUGH_E2E_REPORT,
        )
        walkthrough_e2e_result = _evaluate_walkthrough_e2e_report(
            walkthrough_report, candidate_sha
        )
        if walkthrough_e2e_result.get("status") == "invalid":
            _walkthrough_invalid_reason = str(
                walkthrough_e2e_result.get("reason") or "invalid"
            )[:80]
            if _walkthrough_invalid_reason in _WALKTHROUGH_REPORT_CONTRACT_INVALID_REASONS:
                # Report-contract defect: the report itself violates the
                # DEPLOYED qualifier's evidence contract (schema mismatch,
                # duplicate raw keys, malformed entries) — a deployed-side
                # blocker, not a transient infra hold.  No fix on automain can
                # change how the serving validator parses the report.
                return await _handle_walkthrough_contract_failure(
                    session,
                    version,
                    qual,
                    error="walkthrough_e2e_report_" + _walkthrough_invalid_reason,
                    walkthrough_result=walkthrough_e2e_result,
                )
            # Provenance / runner-lane defect (missing report, candidate or
            # exercised-engine SHA mismatch, missing results): the harness did
            # not produce usable evidence.  Infrastructure failure, not a
            # quality finding.
            return await _handle_infra_failure(
                session,
                version,
                qual,
                "walkthrough_e2e_report_" + _walkthrough_invalid_reason,
            )
        # The report may use supported legacy aliases, but every reported key
        # must resolve to an ACTIVE criterion in the open version's rubric.
        # Unknown keys are malformed evidence, not a pass.
        from app.acceptance_criteria import WALKTHROUGH_E2E_EVIDENCE_KIND

        active_criterion_keys = _resolve_active_criterion_keys(
            live_criterion_rows
        )
        normalized_results = walkthrough_e2e_result.get("results") or []
        try:
            resolved_report_keys = _walkthrough_e2e_canonical_evidence_keys(
                normalized_results
            )
        except ValueError:
            # Canonical collision in the report — a report-contract defect the
            # DEPLOYED validator resolves.  Deployed-side blocker, not infra.
            return await _handle_walkthrough_contract_failure(
                session,
                version,
                qual,
                error="walkthrough_e2e_report_duplicate_criteria",
                walkthrough_result=walkthrough_e2e_result,
            )
        if resolved_report_keys - active_criterion_keys:
            # The report keys do not resolve to the open version's ACTIVE
            # rubric — the harness posted against criteria this version does not
            # carry.  Report-contract defect, resolved by the DEPLOYED validator
            # against the DEPLOYED rubric: deployed-side blocker, not infra.
            return await _handle_walkthrough_contract_failure(
                session,
                version,
                qual,
                error="walkthrough_e2e_report_unknown_criteria",
                walkthrough_result=walkthrough_e2e_result,
            )
        if walkthrough_e2e_result.get("status") == "passed":
            # A passing probe may promote only after its covered-tier evidence
            # was durably ingested.  The agent posts it before terminal status
            # and the backend completion handler is an idempotent backstop; this
            # read proves that seam instead of trusting a best-effort HTTP call.
            active_criteria_by_key = {
                str(row.criterion_key): row
                for row in live_criterion_rows
                if row.status == "active" and row.criterion_key
            }
            expected_evidence = {
                f"walkthrough_e2e:{attempt_id}:{key}": active_criteria_by_key[
                    key
                ].id
                for key in resolved_report_keys
            }
            observed_evidence = (
                await session.execute(
                    select(CriterionEvidence).where(
                        CriterionEvidence.project_id
                        == version.project_id,
                        CriterionEvidence.source_kind
                        == WALKTHROUGH_E2E_EVIDENCE_KIND,
                        CriterionEvidence.source_id.in_(
                            set(expected_evidence)
                        ),
                    )
                )
            ).scalars().all()
            observed_by_source: dict[str, list[CriterionEvidence]] = {}
            for evidence in observed_evidence:
                if evidence.source_id:
                    observed_by_source.setdefault(
                        evidence.source_id, []
                    ).append(evidence)
            if any(
                len(observed_by_source.get(source_id, [])) != 1
                or observed_by_source[source_id][0].criterion_id
                != criterion_id
                for source_id, criterion_id
                in expected_evidence.items()
            ):
                # The probe passed but the covered-tier evidence never landed —
                # the ingestion seam broke.  Infrastructure failure, not a
                # quality finding: hold and re-drive so the evidence can land.
                return await _handle_infra_failure(
                    session,
                    version,
                    qual,
                    "walkthrough_e2e_evidence_missing",
                )
        # A GENUINE criterion failure (report valid, every key resolves to an
        # ACTIVE criterion, one or more criteria failed) is the only
        # requested-but-not-passed outcome that is a QUALITY verdict.  It is
        # folded into the walkthrough_e2e dimension as a hard blocker
        # (promote=False / blocker=walkthrough_e2e_failed) by
        # _apply_walkthrough_decision below; a passing probe folds in as a 1.0
        # sub-score.

    # --- Acceptance-window counts (additive evidence, no score impact) -----
    # Reuses the same verified-prs-merged predicate from direction_outcomes so
    # the release qualification surfaces the same proven-vs-delivered signal as
    # the strategic and direction layers. Merged into sub_scores as additive
    # keys; the active audit/test-erosion weights are unchanged.
    acceptance_window = await _compute_acceptance_window_aggregate(session, version)
    extended_sub_scores = dict(composite["sub_scores"])
    extended_sub_scores.update(acceptance_window)
    extended_sub_scores["stranded_approved_prs"] = qual.get(
        "stranded_approved_prs"
    )
    if walkthrough_e2e_result is not None:
        composite = _apply_walkthrough_decision(
            composite, walkthrough_e2e_result
        )
        walkthrough_score = composite["sub_scores"]["walkthrough_e2e"]
        extended_sub_scores["walkthrough_e2e"] = walkthrough_score
        extended_sub_scores["failing_gates"] = composite.get("failing_gates") or []
        extended_sub_scores["confidence_ceiling"] = composite.get(
            "confidence_ceiling"
        )

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
        "ci_required": ci_required,
        "ci_resolution": ci_resolution,
        "smoketest_enabled": smoketest_enabled,
        "confidence": composite["confidence"],
        "sub_scores": extended_sub_scores,
        "failing_gates": composite.get("failing_gates") or [],
        "confidence_ceiling": composite.get("confidence_ceiling"),
        "required_sub_scores": composite.get("required_sub_scores") or {},
        # Persist the candidate-range gate result so CI-unresolved recovery
        # reuses the same decision without a fresh database or GitHub read.
        "candidate_operator_authorship_gate": candidate_operator_authorship_gate,
        "stranded_approved_prs": qual.get("stranded_approved_prs"),
        "blocker": composite["blocker"],
        # Execution-site of the responsible check (dir: tag promotion blockers
        # inert-until-promote). "deployed" = a fix on automain is inert until this
        # version itself promotes; "candidate" = an automain fix clears it next
        # qualification; None when there is no blocker or it is unclassified.
        "execution_site": blocker_execution_site(composite["blocker"]),
        "threshold": policy.get("threshold"),
        # Preserve the exact launch-time decision contract when replacing the
        # pending JSONB record with its terminal result. These additive JSONB
        # fields need no migration and keep current-vs-attempt truth separable.
        "promotion_policy_snapshot": policy,
        "promotion_policy_source": attempt_policy_projection["source"],
        "promotion_policy_fallback_error": attempt_policy_projection[
            "fallback_error"
        ],
        "promotion_policy_hash": attempt_policy_projection["policy_hash"],
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
        "requested_probes": sorted(requested_probes),
        # Durable record of a product probe dropped at request time
        # (blocker="incompatible_probe"), carried forward from the pending record
        # so the verdict keeps the drop trace.
        "qualify_request_blocked": qual.get("qualify_request_blocked"),
        "qualify_request_blocked_at": qual.get("qualify_request_blocked_at"),
        "walkthrough_e2e_task_id": walkthrough_e2e_task_id,
        "walkthrough_e2e_result": walkthrough_e2e_result,
        "audit_findings": audit_findings,
        "holistic_review": (
            audit_report
            if isinstance(audit_report, dict)
            else (
                _resolved_audit_detail.get("holistic_review")
                if _resolved_audit_detail.get("holistic_contract_fingerprint")
                == qual.get("contract_fingerprint")
                else None
            )
        ),
        # Agent-authored, backend-preserved explanation of delta selection or
        # full fallback. Kept schema-free and shaped for later dimension detail.
        "audit_scope": (
            (audit_report or {}).get("audit_scope")
            or (
                _resolved_audit_detail.get("audit_scope")
                if _audit_and_erosion_resolved
                else None
            )
        ),
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

        # Candidate-side env-preflight evidence (Slice 2, design 245212a4):
        # pure-content {required_env, startup_check_names, manifest_source}
        # introspected from the candidate tree. Stored UNCHANGED — no gating,
        # never read by compute_composite or the promotion decision. None when
        # candidate-side evidence could not be built.
        "env_preflight": env_preflight,
        # Slice 3 (design 245212a4): the deployed-side judge flag + verdict blob.
        # env_preflight_enabled is read back by the state resolver to render the
        # env_preflight dimension (not_applicable when dark).  env_preflight_verdict
        # is {required:[{name,scope}], missing:[names], deploy_target_scope} —
        # presence-only, None when the judge was disabled or no manifest was seen.
        "env_preflight_enabled": env_preflight_enabled,
        "env_preflight_verdict": env_preflight_verdict,
        # Slice 4 (design 245212a4): OWNER-armed one-shot ack that the missing
        # boot prerequisite will be bound in the same deploy revision.
        #   * ack_env_will_be_bound — the ARMED marker.  Carried forward UNCHANGED
        #     while it has not yet overridden a block (so it stays armed across
        #     re-qualification), and CLEARED to None the run it actually overrides
        #     one — a one-shot bypass, never sticky.
        #   * env_preflight_ack_applied — audit record of the applied override
        #     (actor / acknowledged_at / overridden_blocker + presence-only missing
        #     NAMES); None when no override was applied this run.  Presence-only: no
        #     env VALUE is ever recorded.
        "ack_env_will_be_bound": (
            None if env_preflight_ack_override else qual.get("ack_env_will_be_bound")
        ),
        "env_preflight_ack_applied": env_preflight_ack_applied,
        "audit_missing_reason": audit_missing_reason,
        "audit_reconciliation": audit_reconciliation,
        "migration_safety": migration_verdict,
        "migration_safety_detail": migration_detail,
        "migration_apply_safety": apply_verdict,
        "migration_apply_safety_detail": apply_detail,
        "criterion_coverage": criterion_coverage,
        # Attempt-scoped acceptance contract (coverage above remains live).
        "qualification_attempt_id": qual.get("qualification_attempt_id"),
        "rubric_snapshot": launched_rubric,
        "rubric_fingerprint": rubric_provenance.get("fingerprint"),
        "contract_fingerprint": qual.get("contract_fingerprint"),
        "version_goal": qual.get("version_goal"),
        "rubric_provenance": rubric_provenance,
        "rubric_drift": None,
        # Goal-verification redesign: the per-category verdicts + residual that
        # drove the gate this finalize (sha-keyed; None when no rubric / ledger
        # absent / exempt).  Persisted so the CI-unresolved-recovery recompute in
        # release_promotion can re-run the SAME gate without a DB round-trip.
        "category_verdicts": _gate_category_verdicts,
        "residual_uncaptured_gap": _gate_residual,
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
        "prepromotion_rehearsal": prepromotion_rehearsal,
    }

    # Enforce-mode confidence preview — only in observe/off mode, only when no
    # hard blocker fired and debt pressure was actually scored.  Records what
    # enforce would have done to confidence so the operator can graduate the
    # completion_debt_weight_mode knob with accumulated evidence rather than
    # flying blind.  Has no effect on the live composite.
    if (
        not _frozen_cd_was_enforce
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

    # --- Discharge hotfix-deploy backfill debt on a passing normal gate ------
    # The operator hotfix-deploy channel opens a hotfix_deploy_pending_backfill
    # completion-debt row (see _record_hotfix_audit_trail) recording that a
    # fast-tracked release-machinery promotion bypassed the audit/scoring gate
    # and is owed re-qualification by the NORMAL gate.  This IS that gate: when
    # the composite clears the audit/scoring bar (promote=True) the owed
    # re-qualification has demonstrably happened, so resolve any open
    # hotfix-deploy backfill debt for this project — otherwise the row is
    # write-only and drags the debt-pressure sub-score down forever.  Fire on
    # gate-pass (composite.promote), NOT on merge-land: a pass is robust to a
    # descendant SHA and to a later dry-run/merge-conflict, and it mirrors the
    # "gate cleared" semantics the debt records.  Best-effort — never raises,
    # never alters the decision below.
    if composite["promote"]:
        try:
            from app.completion_debt import aresolve_hotfix_deploy_backfill_debts

            _discharged = await aresolve_hotfix_deploy_backfill_debts(
                session,
                version.project_id,
                reason=(
                    f"re-qualified by normal gate at "
                    f"{(candidate_sha or '')[:8]} (v{version.version_number})"
                ),
            )
            if _discharged:
                logger.info(
                    "[RELEASE_QUALIFIER] v%d discharged %d hotfix-deploy backfill "
                    "debt row(s) on passing normal gate",
                    version.version_number,
                    _discharged,
                )
        except Exception:  # noqa: BLE001 — discharge must never break finalize
            logger.warning(
                "[RELEASE_QUALIFIER] v%d failed to discharge hotfix-deploy "
                "backfill debt",
                version.version_number,
                exc_info=True,
            )

    # --- Decide -------------------------------------------------------------
    if not composite["promote"]:
        # Monotonic ledger guard (incident 2026-06-09): a ci_unresolved blocker
        # is transient — CI was pending or unresolvable, not red. Before writing
        # not_promoted for this reason, verify the candidate is not already on
        # main and that there is no open release PR still awaiting managed
        # reconciliation. Either condition means a promotion is pending and this not_promoted
        # would be a spurious miss.
        if composite.get("blocker") == "ci_unresolved":
            if _pre_gates is not None:
                if (
                    _pre_gates.get("_observation_protocol")
                    and CI_RECOVERY not in _pre_gates
                ):
                    raise FinalizeObservationRequired(CI_RECOVERY)
                _guard_result = await _apply_ci_unresolved_promotion_observation(
                    session,
                    version,
                    candidate_sha,
                    base_record,
                    _pre_gates.get(
                        "ci_recovery_observation",
                        # Legacy/direct-test packets predate the unlocked
                        # observation field.  Missing evidence must stay a
                        # conservative no-op, never trigger provider I/O while
                        # the finalize authority lock is held.
                        {"state": "unavailable", "reason": "not_observed"},
                    ),
                )
                if _guard_result is not None:
                    return _guard_result
            else:
                _guard_project = await session.get(Project, version.project_id)
                if _guard_project is not None:
                    _guard_resolved = await _resolve_github_with_session(
                        session, _guard_project
                    )
                    if _guard_resolved is not None:
                        _gtoken, _gowner, _grepo = _guard_resolved
                        _guard_result = (
                            await _guard_ci_unresolved_promotion_pending(
                                session,
                                version,
                                candidate_sha,
                                base_record,
                                _gtoken,
                                _gowner,
                                _grepo,
                                _guard_project,
                            )
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
            # The env-preflight blocker carries the specific missing var NAMES so
            # the operator sees WHICH prerequisite is absent, not just the generic
            # label.  Presence-only: names only, never values.
            if (
                composite["blocker"] == "boot_prereq_missing"
                and env_preflight_missing
            ):
                blocker_label = (
                    f"{blocker_label} — missing: {', '.join(env_preflight_missing)}"
                )
            # Tag the blocker with WHERE its check runs so the operator sees
            # whether a fix on automain takes effect pre-promotion (candidate-side)
            # or is inert until this version itself promotes (deployed-side).
            _site_phrase = blocker_execution_site_label(composite["blocker"])
            if _site_phrase:
                blocker_label = f"{blocker_label} ({_site_phrase})"
            notify_event = "release_blocked"
            notify_summary = (
                f"v{version.version_number} blocked from promotion — "
                f"{blocker_label}. Failing gates: "
                f"{', '.join(composite.get('failing_gates') or [])}. "
                f"Confidence ceiling: {composite.get('confidence_ceiling')}. "
                f"Required sub-scores: {composite.get('required_sub_scores') or 'none'}. "
                "Not promoted; staying in stabilize."
            )
        else:
            notify_event = "release_below_threshold"
            notify_summary = (
                f"v{version.version_number} qualified at confidence "
                f"{composite['confidence']:.2f}, below the "
                f"{policy.get('threshold')} promotion bar — not promoted, "
                f"staying in stabilize. Sub-scores: {scores_str}. "
                f"Confidence ceiling: {composite.get('confidence_ceiling')}; "
                f"required sub-scores: {composite.get('required_sub_scores') or 'none'}."
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
                "failing_gates": composite.get("failing_gates") or [],
                "confidence_ceiling": composite.get("confidence_ceiling"),
                "execution_site": blocker_execution_site(composite["blocker"]),
            },
        )
        return {
            "status": "not_promoted",
            "confidence": composite["confidence"],
            "blocker": composite["blocker"],
            "execution_site": blocker_execution_site(composite["blocker"]),
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
    # durable terminal fact at THIS candidate_sha — the batch model's "everything
    # scored the same tree" guarantee carried onto the ledger.  The dual-write above
    # populated exactly these rows for the candidate moments ago, so the common
    # path is complete; a populated-but-incomplete ledger (a required dimension
    # missing or stranded at a different sha) means the facts do NOT agree on one
    # tree, so HOLD as an infra failure rather than promote on a split verdict.
    # An entirely empty ledger is also incomplete.  Promotion must fail closed on
    # every attempt: otherwise suppressing all first-attempt writes bypasses the
    # same-tree invariant precisely when there is no durable proof at all.
    _ledger_complete, _missing_dims = await _promotion_dimension_ledger_complete(
        session, version, candidate_sha
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
        logger.warning(
            "[RELEASE_QUALIFIER] v%d per-dimension ledger empty at promote "
            "(candidate=%s, attempts=%d) — dual-write did not land; holding "
            "rather than promoting without durable same-tree facts",
            version.version_number,
            (candidate_sha or "")[:8],
            version.qualification_attempts or 0,
        )
        return await _handle_infra_failure(
            session, version, qual, "qualification_dimension_ledger_empty"
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
# failure). Excludes the managed promotion-pending, transient
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
    """Deliver a resolved qualification barrier to the strategic owner.

    A version cut deliberately lets the direction frontier go dry — the
    strategic agent winds its active directions down to a paused residual while
    it waits to qualify ("do not open the next feature wave until vN promotes").
    When qualification then resolves the agent must re-plan: open the next wave
    on a promote, or work the blocker on a miss. But nothing was re-triggering
    it. With every direction paused, no tactical research spawns, so the
    strategic cadence counter never advances and the project sits idle with free
    VMs until the wall-clock backstop eventually fires (up to
    ``strategic_max_interval_minutes`` later). A live Resident receives a durable
    exact receipt. Without one, the legacy one-shot strategic refresh remains the
    compatibility path.

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
            qualification_status = str(result.get("status") or "unknown")
            qualification = dict(version.last_qualification or {})

            from app.strategic_resident_session import (
                read_resident_session,
                resident_session_is_active,
            )

            resident_active = resident_session_is_active(
                read_resident_session(project.vision)
            )
            # Promotion is not the final delivery boundary: every promoted
            # version arms the promotion-merge serving provider.  Waking an
            # expensive Resident here and again when serving resolves spends a
            # second turn without a second decision.  The serving reconciler
            # emits the consolidated success/failure receipt instead.
            if qualification_status == "promoted" and (
                resident_active or project.paused
            ):
                logger.info(
                    "[RELEASE_QUALIFIER] v%d promoted — deferring the strategic "
                    "receipt until serving resolves",
                    version.version_number,
                )
                return

            from app.notification_dispatch import post_event_message

            await post_event_message(
                session,
                project.id,
                event_type="release.qualification_resolved",
                summary=(
                    f"Qualification resolved for v{version.version_number}: "
                    f"{qualification_status}."
                ),
                context={
                    "release_version_id": str(version.id),
                    "version_number": version.version_number,
                    "qualification_status": qualification_status,
                    "candidate_sha": result.get("candidate_sha")
                    or qualification.get("candidate_sha"),
                    "promoted_sha": version.promoted_sha,
                    "serving_status": version.serving_status,
                    "receipt_kind": "qualification_barrier",
                    "resident_wake_barrier": True,
                },
                idempotency_key=(
                    f"release:{version.id}:qualification:{qualification_status}:"
                    f"{result.get('candidate_sha') or qualification.get('candidate_sha') or 'none'}"
                ),
            )
            if project.paused:
                # Preserve the exact outcome for the next session, but never
                # create new agent work while the operator pause is active.
                await session.commit()
                return
            if resident_active:
                await session.commit()
                logger.info(
                    "[RELEASE_QUALIFIER] qualification resolved (%s) for v%d — "
                    "delivered an exact receipt to the active Resident",
                    qualification_status,
                    version.version_number,
                )
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
    expected_attempt_id: uuid.UUID | str | None = None,
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
    promotions is the exact-receipt machinery (an exactly identified landed PR
    routes straight to success without a second merge) PLUS the
    ``version.status != "open"``
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
            result = await _finalize_locked_version(
                session,
                version_id,
                timeout_now,
                expected_attempt_id=expected_attempt_id,
            )
        except release_task_admission.ReleaseTaskAdmissionClosed as exc:
            await session.rollback()
            result = await qualification_deferral.persist_finalization_deferred(
                session,
                version_id,
                exc.project_id,
                exc.reason,
                expected_attempt_id=expected_attempt_id,
            )
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
    *,
    expected_phase_changed_at: datetime | None = None,
    expected_authority_fingerprint: str | None = None,
    expected_project_observation_fingerprint: str | None = None,
    enforce_phase_epoch: bool = False,
) -> dict[str, Any] | None:
    """Lock + re-promote a ``promotion_failed`` version on an open dedicated session.

    The body of ``_reattempt_promotion_failed_in_dedicated_session`` (which owns
    the session + its rollback-on-error contract).  Re-verifies the reuse gate
    AUTHORITATIVELY under a ``SELECT ... FOR UPDATE`` on the version row (the
    pre-lock check in ``run_qualification`` may be stale), reconstructs the
    ``composite`` / ``base_record`` from the PASSING fields the promotion_failed
    record carries, and re-enters ``promote()`` on the CURRENT ``candidate_sha``
    WITHOUT re-auditing.  Successful promotion commits its canonical terminal
    state before post-commit follow-ups; the trailing dedicated-session commit
    remains the boundary for failure and no-op paths.  Returns the ``promote()`` result, or
    ``None`` for every no-op guard so ``run_qualification`` falls through to the
    normal re-audit path.
    """
    version = await lock_project_then_version(session, version_id)
    if version is None:
        return None
    locked_project = await session.get(
        Project, version.project_id, populate_existing=True
    )
    if locked_project is None:
        return None
    if (
        expected_project_observation_fingerprint is not None
        and qualification_project_observation_fingerprint(locked_project)
        != expected_project_observation_fingerprint
    ):
        return {"status": "superseded_project_observation_state"}
    # Only an OPEN, stabilize-phase version can be re-promoted.  A concurrent
    # finalize/promote may have flipped it to promoted/abandoned or moved it out
    # of stabilize between the pre-lock read and this lock — bail to re-audit.
    if version.status != "open" or version.phase != "stabilize":
        return None
    if (
        enforce_phase_epoch
        and version.phase_changed_at != expected_phase_changed_at
    ):
        return {"status": "superseded_phase_epoch"}
    if (
        expected_authority_fingerprint is not None
        and qualification_authority_fingerprint(version)
        != expected_authority_fingerprint
    ):
        return {"status": "superseded_qualification_state"}
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
        from app.release_qualifier_redrive import _escalate_promotion_failure_loop
        await _escalate_promotion_failure_loop(session, version)
        return {"status": "promotion_failed_requalify_cap", "candidate_sha": candidate_sha}

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
    expected_phase_changed_at: datetime | None = None,
    expected_authority_fingerprint: str | None = None,
    expected_project_observation_fingerprint: str | None = None,
    enforce_phase_epoch: bool = False,
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
    PRE-LOCK section — before it takes its own release-authority locks — and its
    caller commits the prior redrive session before opening this dedicated
    Project-then-Version session.  The fresh session therefore never waits on a
    lock retained by the same logical caller.  The strong Project lock also
    serializes exact version cuts and other promotion writers in canonical order.

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
                session,
                version_id,
                candidate_sha,
                candidate_diff_hash,
                expected_phase_changed_at=expected_phase_changed_at,
                expected_authority_fingerprint=expected_authority_fingerprint,
                expected_project_observation_fingerprint=(
                    expected_project_observation_fingerprint
                ),
                enforce_phase_epoch=enforce_phase_epoch,
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
    if result is not None and result.get("status") not in {
        "superseded_phase_epoch",
        "superseded_qualification_state",
        "superseded_project_observation_state",
    }:
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
        "promotion_check_wait",
        "ci_not_green",
    }
)


async def _force_promote_locked(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str,
    candidate_diff_hash: str | None,
    actor: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Lock + FORCE-promote an open version on a dedicated session (no audit).

    Body of ``_force_promote_in_dedicated_session``.  Takes ``SELECT..FOR UPDATE``
    on the version row, refuses when the version is not open or a promotion is
    already in flight (Guard 2b / the Fix-3 claim), builds a FORCED
    composite/base_record (no audit score — marked ``forced_promote``), and
    re-enters ``promote()`` on the current candidate, mirroring
    ``_finalize_locked_version``'s commit discipline.
    """
    version = await lock_project_then_version(session, version_id)
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
    _promo = _promotion_pending_result(version, latest_verdict=_latest_verdict_force)
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
        "forced_promote_reason": reason,
        "started_at": now.isoformat(),
        "finalized_at": now.isoformat(),
    }
    logger.warning(
        "[RELEASE_QUALIFIER] FORCE-PROMOTE: v%d cutting release branch + opening "
        "promotion PR at candidate %s (audit gate SKIPPED; CI still gates the "
        "merge) — actor=%s reason=%s",
        version.version_number,
        candidate_sha[:8],
        actor,
        reason,
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
    reason: str | None = None,
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
                session, version_id, candidate_sha, candidate_diff_hash, actor, reason
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
    reason: str | None = None,
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
            return {"status": "sha_error", "detail": exception_reason(exc)}
    if not candidate_sha:
        return {"status": "sha_error", "detail": "empty candidate sha"}
    # Normalize the operator's ref to a CANONICAL full 40-hex sha before any
    # consumer keys off it.  ``git log`` prints an abbreviated sha, so that is
    # what a human types into ``--candidate-sha`` — and GitHub's ref-create API
    # answers anything shorter with a 422 "Object does not exist" four layers
    # deeper, which surfaced as a bare ``release_branch_failed`` with no reason
    # (incident 2026-08-23).  RESOLVE rather than reject: the engine already
    # holds the token, owner and repo, so it must not hand back a lookup it can
    # perform itself.  A full sha returns unchanged with NO network call, so an
    # already-canonical candidate behaves exactly as it did before.
    try:
        candidate_sha = await github.resolve_commit_sha(
            token, owner, repo, candidate_sha, project_id=str(project_id)
        )
    except Exception as exc:  # noqa: BLE001 — an unresolvable ref IS an operator error; report it precisely
        return {"status": "sha_error", "detail": exception_reason(exc)}
    candidate_diff_hash = compute_audited_surface_hash(
        await github.list_repo_tree_paths_at_ref(
            token, owner, repo, candidate_sha, project_id=str(project_id)
        )
    )
    # Explicitly resolve CI for the candidate before opening the promotion PR.
    # Both bypass channels skip the AUDIT gate only, not CI; relying on branch
    # protection alone fails open when protection is absent or misconfigured.
    ci_green, ci_resolution = await _resolve_ci_at_finalize(session, version, candidate_sha)
    ci_required = _ci_required_for_finalize(project, ci_resolution)
    if ci_required and not ci_green:
        logger.warning(
            "[RELEASE_QUALIFIER] FORCE-PROMOTE blocked: project %s v%d candidate %s "
            "CI is not green (state=%s) — actor=%s reason=%s",
            project_id,
            version.version_number,
            candidate_sha[:8],
            ci_resolution.get("state"),
            actor,
            reason,
        )
        return {"status": "ci_not_green", "ci_resolution": ci_resolution}
    logger.warning(
        "[RELEASE_QUALIFIER] FORCE-PROMOTE requested: project %s v%d candidate %s "
        "actor=%s reason=%s — SKIPPING the audit/qualify gate (CI still gates the "
        "merge)",
        project_id,
        version.version_number,
        candidate_sha[:8],
        actor,
        reason,
    )
    return await _force_promote_in_dedicated_session(
        version.id,
        candidate_sha,
        candidate_diff_hash,
        actor,
        reason=reason,
        session_factory=session_factory,
    )



def _qualification_timeout_age_seconds(
    version: release_version.ReleaseVersion,
    qual: dict[str, Any],
    timeout_now: datetime,
    *,
    warn_on_fallback: bool,
    deploy_state: DeploymentState | None = None,
) -> float | None:
    """Return pending qualification age in seconds, or None if no clock exists.

    PAUSE-AWARE when ``deploy_state`` is supplied: pipeline-pause time is not
    counted toward the 6h timeout. A qualification whose active audit/grade
    children cannot run *because the pipeline is paused* is not "stuck" — force-
    finalizing it as ``qualification_timeout`` sends the version back to
    stabilize and re-fires the whole triple, which then times out again 6h
    later, re-spawning the triple against the newest candidate for the entire
    duration of the pause. The per-attempt ``pause_baseline`` (stamped at spawn
    in run_qualification) makes the subtraction exact across pause windows;
    without it (legacy pending records) the open pause window is still excluded.
    ``deploy_state`` is only ever threaded from the reaper finalize path, where
    ``timeout_now`` == ``_utcnow()``, so the helper's own clock agrees with it.
    """
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
    if deploy_state is not None:
        from app import pause_clock  # noqa: PLC0415 — local import avoids load cycle

        elapsed = pause_clock.active_elapsed_seconds(
            deploy_state, timeout_clock, qual.get("pause_baseline")
        )
    else:
        elapsed = (timeout_now - timeout_clock).total_seconds()
    return max(
        0.0,
        elapsed
        - qualification_deferral.deferred_timeout_seconds(
            qual, now=timeout_now, deploy_state=deploy_state
        ),
    )


async def _finalize_locked_version(
    session: AsyncSession,
    version_id: uuid.UUID,
    timeout_now: datetime | None,
    *,
    expected_attempt_id: uuid.UUID | str | None = None,
    _observation_packet: dict[str, Any] | None = None,
    _observation_round: int = 0,
) -> dict[str, Any] | None:
    """Lock + finalize one version within an already-open dedicated session.

    The body of ``_finalize_in_dedicated_session`` (which owns the session and
    its rollback-on-error contract).  Split out so the locking + decision logic
    reads as a flat sequence.  Commits the dedicated session on every path that
    actually wrote something; returns ``None`` for the read-only no-op paths.

    GitHub-bound gates run from a non-locking candidate peek; only the decision
    write holds ``ReleaseVersion FOR UPDATE``.  This avoids long ShareLock waits
    while their rate-limit/retry I/O is in flight.
    """
    # Peek the immutable candidate; all guards are re-verified under lock.
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
    # Cheap no-op fast path before the external gates.
    if peek is None or peek.status != "open":
        return None
    peek_qual = peek.last_qualification or {}
    if peek_qual.get("status") != "pending":
        return None
    peek_project_id = peek.project_id
    expected_attempt_uuid = _coerce_qualification_attempt_id(expected_attempt_id)
    active_attempt_uuid = _coerce_qualification_attempt_id(
        peek_qual.get("qualification_attempt_id")
    ) or _coerce_qualification_attempt_id(peek.active_qualification_attempt_id)
    if (
        expected_attempt_uuid is not None
        and active_attempt_uuid is not None
        and expected_attempt_uuid != active_attempt_uuid
    ):
        logger.info(
            "[RELEASE_QUALIFIER] finalize ignored completion from stale attempt "
            "%s; v%d active attempt is %s",
            expected_attempt_uuid,
            peek.version_number,
            active_attempt_uuid,
        )
        return None
    peek_candidate_sha: str | None = peek_qual.get("candidate_sha")
    # Pause-aware timeout: read the deploy state once so the 6h backstop excludes
    # pipeline-pause time at both the pre-lock peek and the under-lock check.
    _deploy_state = await session.get(DeploymentState, 1)
    pre_timeout_age_seconds = (
        _qualification_timeout_age_seconds(
            peek,
            peek_qual,
            timeout_now,
            warn_on_fallback=False,
            deploy_state=_deploy_state,
        )
        if timeout_now is not None and not peek_qual.get("promotion_pending")
        else None
    )
    pre_timeout_expired = (
        pre_timeout_age_seconds is not None
        and pre_timeout_age_seconds > _MAX_QUALIFICATION_DURATION_SECONDS
    )
    early_admission = await release_task_admission.peek_release_task_admission(
        session, peek_project_id
    )

    # Run external gates only for an admitted, terminal-child unlocked peek.
    # A rare readiness race resolves the gates later under the decision lock.
    _pre_gates: dict[str, Any] | None = (
        dict(_observation_packet) if _observation_packet is not None else None
    )
    if (
        _pre_gates is None
        and early_admission
        == release_task_admission.AUTOMATIC_TASK_CREATION_ALLOWED
        and not pre_timeout_expired
        and await _required_children_terminal(session, peek)
    ):
        pre_gate_project = await session.get(Project, peek_project_id)
        if pre_gate_project is None:
            return None
        pre_version_authority_fingerprint = (
            qualification_finalize_authority_fingerprint(peek)
        )
        pre_project_observation_fingerprint = (
            qualification_project_observation_fingerprint(pre_gate_project)
        )
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
        pre_authorship_measurement = await _measure_candidate_operator_authorship(
            session, peek
        )
        _pre_gates = {
            "_observation_protocol": True,
            "ci_green": pre_ci_green,
            "ci_resolution": pre_ci_resolution,
            "migration_verdict": pre_migration_verdict,
            "migration_detail": pre_migration_detail,
            "apply_verdict": pre_apply_verdict,
            "apply_detail": pre_apply_detail,
            "candidate_authorship_measurement": pre_authorship_measurement,
            "version_authority_fingerprint": (
                pre_version_authority_fingerprint
            ),
            "project_observation_fingerprint": (
                pre_project_observation_fingerprint
            ),
        }
        if pre_ci_resolution.get("state") == _CI_NO_CHECKS:
            _pre_gates["ci_required"] = _ci_required_for_finalize(
                pre_gate_project, pre_ci_resolution
            )
        # Clear any failed-closed DBAPI transaction before taking row locks.
        await session.rollback()

    # Project contention fails closed for a later sweep, never crash budget.
    admission_decision = await release_task_admission.finalize_admission_decision(
        session, peek_project_id, skip_locked=True
    )
    if admission_decision == AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE:
        return None

    # Force a fresh authoritative read under the Version lock.
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

    # ``rollback()`` expires ORM state.  Keep the immutable display value as a
    # scalar so the diagnostic-only exits below never lazy-load ``version``
    # after ending their transaction.
    locked_version_number = version.version_number

    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        # Idempotent no-op after a lock-serialized non-promote finalization.
        # Overlapping promotion is fenced by its receipt/status re-checks.
        return None

    locked_attempt_uuid = _coerce_qualification_attempt_id(
        qual.get("qualification_attempt_id")
    ) or _coerce_qualification_attempt_id(version.active_qualification_attempt_id)
    if not qualification_deferral.finalization_snapshot_is_current(
        expected_attempt_uuid, active_attempt_uuid, peek_candidate_sha,
        locked_attempt_uuid, qual.get("candidate_sha"), _pre_gates is not None
    ):
        return None
    if _pre_gates is not None:
        locked_project = await session.get(
            Project, version.project_id, populate_existing=True
        )
        if (
            locked_project is None
            or qualification_finalize_authority_fingerprint(version)
            != _pre_gates["version_authority_fingerprint"]
            or qualification_project_observation_fingerprint(locked_project)
            != _pre_gates["project_observation_fingerprint"]
        ):
            await session.rollback()
            logger.info(
                "[RELEASE_QUALIFIER] finalize: v%d external gate packet was "
                "superseded before apply",
                locked_version_number,
            )
            return None

    _deploy_state_locked = await session.get(
        DeploymentState, 1, populate_existing=True
    )
    deferred, resumed_from_admission_deferral, children_terminal_after_deferral = (
        await qualification_deferral.prepare_finalization_admission(
            session,
            version,
            admission_decision,
            _required_children_terminal,
            _deploy_state_locked,
        )
    )
    if deferred is not None:
        return deferred
    qual = version.last_qualification or {}

    # --- Path 1: age-bound qualification timeout (reaper sweep only) --------
    # Checked first and child-state-independent (audit H2).
    if timeout_now is not None:
        # Skip the qualification timeout when the version is already awaiting a
        # managed promotion. Qualification completed and passed; the pending
        # clock now belongs to the promotion PR on main CI, not to qualifier
        # children.  The timeout backstop is only for qualifier children that
        # never reach a terminal state. The managed-wait stall check (separate
        # lifecycle alert) handles the case where the PR is genuinely stuck.
        if qual.get("promotion_pending"):
            return None
        age_seconds = _qualification_timeout_age_seconds(
            version,
            qual,
            timeout_now,
            warn_on_fallback=True,
            deploy_state=_deploy_state_locked,
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
    if children_terminal_after_deferral is None:
        children_terminal_after_deferral = await _required_children_terminal(
            session, version
        )
    if not children_terminal_after_deferral:
        if resumed_from_admission_deferral:
            await session.commit()
        return None
    if _pre_gates is None:
        # Readiness can flip after the unlocked peek (for example, the sibling
        # child commits terminal while this finalizer waits for authority).
        # Never fall back to provider/subprocess gates under Project/Version
        # locks.  Persist only a bounded admission-resume transition, if one
        # occurred, and let the completion event or scheduled sweep re-enter
        # through the normal unlocked observation path.
        if resumed_from_admission_deferral:
            await session.commit()
        else:
            await session.rollback()
        logger.info(
            "[RELEASE_QUALIFIER] finalize: v%d became ready after the "
            "unlocked gate peek; deferring one cycle for a complete gate packet",
            locked_version_number,
        )
        return None

    # Finalize on the dedicated session.  _finalize_qualification may call
    # promote(), whose intermediate commit + re-lock are now safe: this session
    # holds only qualifier state.  Pass the pre-computed gate results so the
    # function does NOT re-run the GitHub-bound gates under the row lock.
    try:
        result = await _finalize_qualification(
            session, version, _pre_gates=_pre_gates
        )
    except FinalizeObservationRequired as exc:
        if _observation_round >= 3:
            await session.rollback()
            return {"status": "pending", "reason": "observation_retry_exhausted"}
        extended = await extend_finalize_observation_packet(
            session, version_id, _pre_gates, exc.kind
        )
        if extended is None:
            return None
        return await _finalize_locked_version(
            session,
            version_id,
            timeout_now,
            expected_attempt_id=expected_attempt_id,
            _observation_packet=extended,
            _observation_round=_observation_round + 1,
        )
    await session.commit()
    dispatch_postcommit_infra_notification(result)
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
         and merge-grade children can terminate
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
        version_id,
        session_factory=session_factory,
        expected_attempt_id=context.get("qualification_attempt_id"),
    )


# ── Deferred re-export: break the release_qualifier ⟷ release_hotfix_promotion
# circular import ─────────────────────────────────────────────────────────────
# release_hotfix_promotion imports many symbols from THIS module at its own top
# level (_FORCE_PROMOTE_NOOP_STATUSES, promote, compute_audited_surface_hash,
# …). If we imported it at the top of release_qualifier, rhp would run while
# this module was only partially initialized and fail to find those names. By
# importing it at the very bottom — after every symbol rhp needs is defined —
# rhp sees a fully-initialized release_qualifier. The two names remain
# module-level attributes so `release_qualifier.hotfix_promote_current_version`
# / `.check_hotfix_scope_preview` keep working for admin_hotfix_promote.
from app.release_hotfix_promotion import (  # noqa: E402
    _record_hotfix_audit_trail as _record_hotfix_audit_trail,
    check_hotfix_scope_preview as check_hotfix_scope_preview,
    hotfix_promote_current_version as hotfix_promote_current_version,
)
