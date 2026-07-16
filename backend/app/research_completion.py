"""Research completion handler and supporting functions.

Extracted from app.task_completion to keep that module focused on plan/security/perf
completion handlers. Contains the research findings parser, context builder,
and the large _handle_research_completion handler.
"""

import asyncio
import contextlib
import json
import logging
import os
import random
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Dict, List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.context_bounds import bound_deferred_findings
from app.depends_on_provenance import remember_structural_depends_on
from app.findings_index import (  # noqa: F401 — re-exported for callers/tests
    lookup_findings_by_source_ref,
    query_findings_index,
    _shelve_findings_for_next_cycle,
)
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)
from app.cochange_decomposition import (
    auto_decompose_findings_by_cochange,
    cluster_files_by_cochange as _cluster_files_by_cochange,  # noqa: F401 — re-exported for tests
)
from app.compute_experiment_contract import (
    _author_compute_launch_contract,
    _stamp_container_image_pin_status,
    _validate_compute_launch_contract,
    resolve_compute_spend_approval,
    resolve_compute_spend_threshold,
)
from app.direction_metrics import (  # noqa: F401 — re-exported for callers/tests
    _HISTORY_AGG_DEFAULTS,
    _DECOMP_AGG_DEFAULTS,
    _fold_evicted_into_aggregate,
    build_evicted_cycle_snapshot,
    _compute_momentum_factor,
    _compute_acceptance_rate,
    _compute_productivity_score,
)
from app.direction_outcomes import credit_gate_merged_count
from app.failure_classification import (
    CO_DEFERRED_SHELVED,
    EXTERNAL_API,
    EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA,
    GRADER_CAUSE_REFUSAL,
    GRADER_TIMEOUT_CAUSES,
    GRADER_TRANSIENT_INFRA_CAUSES,
    INFRA_CLASSES,
    classify_external_api_subtype,
    classify_failure,
)
from app.finding_deduplication import (
    build_dedup_state,
    canonical_finding_body as _canonical_finding_body,
    extract_recent_terminal_finding_history,
    deduplicate_findings,
    extract_retired_failed_approaches,
    extract_archived_diagnoses,
    finding_hash as _finding_hash,
    is_duplicate as _is_duplicate,  # noqa: F401 — re-exported for callers/tests
    need_matches_task_lineage,
    normalize_prerequisite,
    resolve_prerequisite_task,
)
from app.task_blocked import NEED_REF_MAX_LEN
from app.task_evaluators import (
    ADVERSARIAL_PARSE_CHALLENGER_FAILED,
    ADVERSARIAL_PARSE_NO_HASH_MATCHES,
    ADVERSARIAL_WHOLESALE_NON_RESPONSE_OUTCOMES,
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    FINDING_CHALLENGE_VERDICT_KEY,
    build_adversarial_challenge_manifest,
    spawn_finding_challenge,
)
from app.capability_registry import (
    annotate_capability_proof_status,
    capability_need_blocked_by_proof_gate,
    capability_need_proof_status,
    off_registry_capability_refs,
    registry_capability_evidence_refs_map,
    registry_capability_names,
    registry_capability_vocabulary,
)
from app.payload_schemas import (
    normalize_acceptance_evidence,
    validate_internal_payload,
)
from app.playbook_learning import (
    RECENT_SUCCESSES_TASK_TYPES,
    summarize_recent_successes,
)
from app.research_context import (  # noqa: F401 — re-exported for callers/tests
    _parse_research_context,
    _parse_eviction_counts,
    _format_evicted_failed,
    _format_eviction_body,
    _build_research_context,
)
from app.research_direction_state import (  # noqa: F401 — re-exported for callers/tests
    RETIRED_DIRECTION_STATUSES,
    _compute_saturation,
    _detect_dedup_deadlock,
    apply_post_spawn_state,
    direction_saturation_refractory_minutes,
)
from app.direction_refractory import stamp_direction_refractory
from app.direction_mode import (
    normalized_recommended_mode as _normalized_recommended_mode,
)
from app.source_ref_validation import (
    validate_source_refs,
    resolve_branch_verification_context,
    summarize_broken_refs,
    summarize_ungrounded_claims,  # noqa: F401 — re-exported for callers/tests
    resolve_head_commit,
    cited_paths_changed_since,
    finding_cited_paths,
    classify_reproduction,
    _has_validation_backend,
    CITATION_STATE_ABSENT,
    CITATION_STATE_PRESENT,
    CITATION_STATE_INDETERMINATE,
)
from app.reproduction_scoring import record_target_defect_reproduction
from app.shared_state import (
    runtime_config as _runtime_config,
    DEFAULT_TASK_SCOPE,
    scope_to_effort,
    get_project_integration_branch as _get_project_integration_branch,
)
from app.spawn_calibration import (  # noqa: F401 — re-exported for tests/callers
    _SCOPE_ORDER,
    SCOPE_FILE_BUDGETS,
    MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
    _DEGRADED_SPAWN_CAP,
    _assess_spawn_health,
    _assess_project_spawn_health,
    _maybe_cold_start_upgrade_medium,
    _calibrate_scope,
    _poor_decomp_history,
    predict_spawn_success,
    check_scope_budget_mismatch,
    SPEND_BUDGET_APPROVAL_HOLD_REASON,
)
from app.task_locking import (
    with_design_doc_locked,
    with_directions_locked,
    with_problem_model_locked,
    with_vision_locked,
)
from app.plan_completion import (
    _validate_plan_dependencies,
    _break_dependency_cycles,
    _log_file_overlaps,
    TASK_PRIORITY_MAP,
)
from app.time_utils import _utcnow
from app.research_provenance_validation import (  # noqa: F401 — re-exported for callers/tests
    VERIFICATION_BASIS_TIERS,
    VALID_VERIFICATION_BASES,
    _HIGH_EVIDENCE_BASES,
    _DOWNGRADED_VERIFICATION_BASIS,
    _reconcile_verification_basis,
    _staleness_provenance_warning,
    _merge_provenance_warnings,
    recompute_launch_staleness,
    _summarize_ungrounded_claims_grounded,
    _count_total_symbol_refs,
    _file_source_present_count,
)
from app.research_scope_calibration import (  # noqa: F401 — re-exported for callers/tests
    MEDIUM_EXPLORATION_FLOOR,
    _avg_medium_expansion_ratio,
    _build_preventive_context,
    _calibrate_finding_scope,
    _cochange_decomposition_suppressed,
    _compute_prompt_telemetry,
    _outcome_complexity_threshold,
    _resolve_compute_spend_threshold,
)

logger = logging.getLogger(__name__)

# Task types whose effort is driven by the calibrated scope rather than the
# type-level task_type_efforts config default. These are the "implementation"
# types where cost should scale with code-change size: create, fix, refactor,
# plan spawn implementation subtasks at scope-derived effort; qa and migrate
# ship real code/schema changes that scale from trivial (one test/one column) to
# large (test restructuring/multi-table refactoring), so they benefit from the
# same scaling. deps and docs are omitted — dependency updates and docs tend to
# be lightweight and consistent across scope, so their type-level effort is kept.
_SCOPE_AWARE_EFFORT_TYPES = frozenset(
    {"create", "fix", "refactor", "plan", "qa", "migrate"}
)

# Idempotency guard for squash-merge ancestry fallback: once a parent task's
# PR is confirmed as squash-landed (commit message reference found), skip
# re-checking on subsequent cycles — the landing is permanent.
_SQUASH_LANDED_CONFIRMED_PARENT_IDS: set[str] = set()

# Max memo tasks spawned per research cycle for dropped findings. Independent
# of the implementation-task spawn cap (_DEGRADED_SPAWN_CAP / batch cap): memos
# are read-only and cheap, so a flood of dropped findings is bounded here rather
# than stampeding memo creation.
MEMO_SPAWN_CAP_PER_CYCLE = 5

# Per-cycle ceiling on implementation tasks spawned from one research cycle's
# findings. This is a flat backlog guard, NOT a capacity throttle: real capacity
# is enforced where it actually binds — the dispatcher only starts as many VMs as
# there are free slots (orchestrator's max_concurrent_vms gate), and
# _assess_project_spawn_health clamps spawning to _DEGRADED_SPAWN_CAP when the
# fleet is genuinely degraded (zones in cooldown, no accounts, per-project backlog
# over budget, high recent failure rate). A spawned child is just a QUEUED row
# that costs nothing until dispatched, so throttling spawn on instantaneous fleet
# load only suppresses healthy research yield and pushes findings into lossy
# deferral. Findings beyond this ceiling are deferred to the next cycle.
_MAX_FINDINGS_PER_CYCLE = 15

# Trailing cap for the durable spawn-count idempotency ledger persisted in
# project.vision. Only the most recent IDs can plausibly be replayed by a
# post-commit re-run, so an unbounded list would grow forever inside the
# JSONB for no dedup benefit. Matches the sibling durable-history caps
# (rejected_proposals=30, *_DURABLE_MAX=200) — see the review note.
_RESEARCH_SPAWN_COUNTED_IDS_MAX = 200

# Drop reason stamped on the memo a reproduce-on-HEAD rejection spawns. Distinct
# from the capacity/quality drop reasons (low_actionability / degradation_gate)
# so strategic review can tell "the cited code is unambiguously gone" — the
# strongest truthfulness signal — apart from a merely deferred finding.
_REPRODUCE_REJECTED_DROP_REASON = "reproduce_on_head_absent"
_CRITERION_EVIDENCE_REJECTED_DROP_REASON = "criterion_evidence_missing"

_FINDING_CHALLENGE_HISTORY_KEY = "finding_challenge_records"
_FINDING_CHALLENGE_RECORD_KEY = "finding_challenge"
_LOW_ACTIONABILITY_REFINEMENT_REASON = "low_actionability_refinement"
_RESEARCH_FINDING_CHALLENGE_FLAG = "research_finding_challenge_enabled"
_RESEARCH_FINDING_INDETERMINATE_CHALLENGE_FLAG = (
    "research_finding_challenge_indeterminate_dispatch_enabled"
)

_RESEARCH_SPAWN_GATE_CHILD_FIELDS = (
    "research_spawn_gate_challenged",
    "research_spawn_gate_verdict",
)

_RESEARCH_SPAWN_GATE_PARENT_COUNT_FIELDS = (
    "research_spawn_gate_challenged_count",
    "research_spawn_gate_refuted_count",
    "research_spawn_gate_survived_count",
    # Decision-quality breakdown of the inclusive `survived`/`refuted` buckets:
    # an explicit challenger pass vs a fail-open (passed but no explicit verdict)
    # pass vs an unresolved/no-verdict outcome. `survived` stays the inclusive
    # passed-bucket (= explicit_pass + fail_open) and `refuted` stays the
    # inclusive passed=False bucket (= explicit-refutation + unresolved).
    "research_spawn_gate_explicit_pass_count",
    "research_spawn_gate_fail_open_count",
    "research_spawn_gate_unresolved_count",
    # Compact, capped, deduped sample of refuted/unresolved challenger reasons so
    # a degraded gate is visible at cycle-rollup without opening individual tasks.
    "research_spawn_gate_refuted_reasons",
    "research_spawn_gate_manifest_excluded_fail_closed_count",
    "research_spawn_gate_manifest_excluded_fail_closed_samples",
)

# Cap on the compact refuted/unresolved reasons sample persisted per cycle and
# rolled up downstream — keeps the payload bounded across many cycles.
_SPAWN_GATE_REASONS_SAMPLE_CAP = 5
# Max length of an individual sampled reason string (challenger reasons are
# already truncated to 480 chars upstream; trim further for the compact sample).
_SPAWN_GATE_REASON_MAX_LEN = 200
_ADVERSARIAL_WHOLESALE_RETRY_KEY = "_adversarial_wholesale_retry_count"
_ADVERSARIAL_WHOLESALE_RETRY_LIMIT = 1
# Targeted retry: used when the non-response cause is time-based (timeout/stall).
# A reduced-manifest grader is spawned BEFORE falling back to the wholesale retry,
# so the total grader budget is: initial + at most one targeted + at most one wholesale.
_ADVERSARIAL_TARGETED_RETRY_KEY = "_adversarial_targeted_retry_count"
_ADVERSARIAL_TARGETED_RETRY_LIMIT = 1
_ADVERSARIAL_INFRA_FAILURE_MODE_KEY = "adversarial_spawn_gate_infra_failure_mode"
_ADVERSARIAL_INFRA_FAILURE_MODE_ENFORCE = "enforce"
_ADVERSARIAL_INFRA_UNVERIFIED_VERDICT = "unverified"

def _trim_finding_for_memo(finding: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a dropped finding to a bounded dict for embedding in a MemoPayload.

    Keeps the identifying/contextual fields a memo agent needs and drops bulky
    internal calibration fields (``_complexity_signals`` etc.) and unbounded
    text so the memo payload stays well under the payload size budget.
    """
    trimmed: Dict[str, Any] = {}
    for key in (
        "title",
        "type",
        "scope",
        "risk",
        "priority",
        "actionability_grade",
        "verification_basis",
        "direction_id",
    ):
        val = finding.get(key)
        if val is not None:
            trimmed[key] = val
    desc = finding.get("description")
    if isinstance(desc, str) and desc:
        trimmed["description"] = desc[:1000]
    related = finding.get("related_files")
    if isinstance(related, list) and related:
        trimmed["related_files"] = [str(f) for f in related[:20]]
    # Carry the cited refs found absent on HEAD — broken file paths + ungrounded
    # path:symbol refs already computed during grounding and merged onto the
    # finding's _provenance_warnings — so a reproduce-on-HEAD memo can name
    # exactly what was stale without strategic review re-deriving it.
    prov = finding.get("_provenance_warnings")
    if isinstance(prov, dict):
        absent: list[str] = [str(r) for r in (prov.get("broken_refs") or [])]
        absent += [str(r) for r in (prov.get("ungrounded_refs") or [])]
        if absent:
            trimmed["absent_references"] = absent[:20]
    challenge = finding.get(_FINDING_CHALLENGE_RECORD_KEY)
    if isinstance(challenge, dict):
        trimmed[_FINDING_CHALLENGE_RECORD_KEY] = {
            str(k): v
            for k, v in challenge.items()
            if k
            in {
                "challenged_boundary",
                "first_verdict",
                "second_opinion_verdict",
                "agreement",
                "reason",
                "sidecar_task_id",
                "schema_version",
                "source_task_id",
                "produced_by",
            }
        }
    return trimmed


def _synthesize_deferred_finding_memo(
    sf: Dict[str, Any],
    drop_reason: str,
    source_task_id: str,
) -> Dict[str, Any] | None:
    """Synthesize a memo dict for a deferred finding without a model call.

    Returns None when the finding has no title or description (insufficient data).
    The backend already holds the full finding at spawn time, so no VM is needed.
    """
    title = str(sf.get("title") or "").strip()
    description = str(sf.get("description") or "").strip()
    if not title and not description:
        return None
    parts = [f"Dropped finding: {title}"] if title else []
    parts.append(f"Reason: {drop_reason}")
    if description:
        parts.append(f"Description: {description}")
    absent = sf.get("absent_references")
    if isinstance(absent, list) and absent:
        parts.append("Cited code absent on HEAD: " + ", ".join(str(r) for r in absent))
    challenge = sf.get(_FINDING_CHALLENGE_RECORD_KEY)
    if isinstance(challenge, dict):
        agreement = str(challenge.get("agreement") or "unknown")
        second = str(challenge.get("second_opinion_verdict") or "unknown")
        reason = str(challenge.get("reason") or "").strip()
        challenge_line = (
            f"Second opinion: {agreement} with original "
            f"{challenge.get('challenged_boundary') or 'finding'} verdict; "
            f"second verdict={second}."
        )
        if reason:
            challenge_line += f" Reason: {reason}"
        parts.append(challenge_line)
    return {
        "summary": (title or description)[:200],
        "reasoning": "\n\n".join(parts),
        "citations": [f"task:{source_task_id}"] if source_task_id else [],
        "tags": ["deferred-finding", drop_reason],
    }


def _finding_challenge_enabled(boundary: str) -> bool:
    if boundary in (
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    ):
        return bool(_runtime_config.get(_RESEARCH_FINDING_CHALLENGE_FLAG, False))
    if boundary == CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH:
        return bool(
            _runtime_config.get(_RESEARCH_FINDING_INDETERMINATE_CHALLENGE_FLAG, False)
        )
    return False


def _consume_finding_challenge_record(
    task: Task,
    finding: Dict[str, Any],
    *,
    boundary: str,
    first_verdict: str,
) -> Dict[str, Any] | None:
    """Attach one returned challenge verdict to the finding it gates.

    ``task_evaluators`` writes a single active record on the source task. The
    research handler consumes that active record on the resume pass, stores a
    bounded source-task history, and removes the active key so another later
    high-stakes finding can be challenged without double-applying this verdict.
    """
    ctx = dict(task.context or {})
    record = ctx.get(FINDING_CHALLENGE_VERDICT_KEY)
    if not isinstance(record, dict):
        return None
    if record.get("challenged_boundary") != boundary:
        return None
    if str(record.get("first_verdict") or "") != str(first_verdict):
        return None
    # Identity check: reject if the record was stamped for a different finding.
    record_fhash = record.get("finding_hash")
    if record_fhash is not None:
        current_fhash = _finding_hash(
            finding.get("title", ""),
            finding.get("description") or finding.get("prompt", ""),
        )
        if current_fhash != record_fhash:
            return None

    attached = dict(record)
    finding[_FINDING_CHALLENGE_RECORD_KEY] = attached
    with contextlib.suppress(Exception):
        flag_modified(task, "plan_output")
    history = [
        h
        for h in (ctx.get(_FINDING_CHALLENGE_HISTORY_KEY) or [])
        if isinstance(h, dict)
    ]
    history.append(attached)
    ctx[_FINDING_CHALLENGE_HISTORY_KEY] = history[-20:]
    ctx.pop(FINDING_CHALLENGE_VERDICT_KEY, None)
    ctx.pop("awaiting_finding_challenge", None)
    task.context = ctx
    flag_modified(task, "context")
    return attached


def _challenge_agrees(record: Dict[str, Any] | None) -> bool:
    return isinstance(record, dict) and record.get("agreement") == CHALLENGE_AGREES


def _challenge_disagrees(record: Dict[str, Any] | None) -> bool:
    return isinstance(record, dict) and record.get("agreement") == CHALLENGE_DISAGREES


def _challenge_second_verdict(record: Dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return ""
    return str(record.get("second_opinion_verdict") or "").lower()


def _challenge_confirms_verdict(record: Dict[str, Any] | None, verdict: str) -> bool:
    return (
        _challenge_agrees(record)
        and _challenge_second_verdict(record) == str(verdict).lower()
    )


def _reconcile_reproduce_provenance_state(
    finding: Dict[str, Any], verdict: str
) -> None:
    """Keep reproduce-on-HEAD provenance aligned with an overturned challenge.

    When a reproduce-absent challenge keeps the finding, the child task must not
    inherit a stale ``citation_state == 'absent'`` seed. Reconcile that seed to
    the same keep-verdict so downstream context builders and scoring see the
    updated reproduce state.
    """
    if verdict not in (CITATION_STATE_PRESENT, CITATION_STATE_INDETERMINATE):
        return
    prov = finding.get("_provenance_warnings")
    if not isinstance(prov, dict):
        return
    if prov.get("citation_state") != CITATION_STATE_ABSENT:
        return
    reconciled = dict(prov)
    reconciled["citation_state"] = verdict
    finding["_provenance_warnings"] = reconciled


def _parse_research_findings(text: str) -> List[Dict[str, Any]] | None:
    """Extract structured research findings JSON array from agent output text."""
    from app.task_completion import _extract_json_array

    json_str = _extract_json_array(text, "title", "prompt")
    if json_str:
        try:
            findings = json.loads(json_str)
            if isinstance(findings, list):
                return findings
        except json.JSONDecodeError:
            # JSON may be truncated — try to recover complete objects
            # Find last complete top-level object using depth tracking
            depth = 0
            last_close = -1
            in_string = False
            escape_next = False
            for i, c in enumerate(json_str):
                if escape_next:
                    escape_next = False
                    continue
                if c == "\\" and in_string:
                    escape_next = True
                    continue
                if c == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        last_close = i
            if last_close > 0:
                try:
                    truncated = json_str[: last_close + 1] + "]"
                    findings = json.loads(truncated)
                    if isinstance(findings, list):
                        logger.info(
                            f"[RESEARCH] Recovered {len(findings)} findings from truncated JSON"
                        )
                        return findings
                except json.JSONDecodeError:
                    pass
    return None


def _cap_findings_by_priority(
    findings: List[Dict[str, Any]], effective_cap: int
) -> tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """Cap findings to effective_cap, preserving high-priority items first.

    Returns (capped_findings, batch_cap_applied_count, batch_cap_deferred).
    Synchronous helper for asyncio.to_thread() offloading.
    """
    _BATCH_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
    batch_cap_applied_count = max(0, len(findings) - effective_cap)
    _batch_cap_deferred: list[dict] = []
    if len(findings) > effective_cap:
        _ranked = sorted(
            enumerate(findings),
            key=lambda pair: _BATCH_PRIORITY_ORDER.get(
                pair[1].get("priority", "medium"), 1
            ),
        )
        _kept_set = {i for i, _ in _ranked[:effective_cap]}
        # Co-defer any kept finding whose depends_on_indices points at a finding
        # the cap dropped. Without this, the index remap below silently DROPS
        # that edge (the target index is not in _old_to_new), and the dependent
        # spawns as an ordinary child with no edge and no blocking need --
        # dispatching against a foundation that does not exist yet. Mirror the
        # same-cycle prerequisite co-deferral: shelve the dependent alongside its
        # deferred foundation so the pair re-surfaces together next cycle.
        # Iterate to a fixpoint so an entire dependent chain co-defers, never
        # just the leaf.
        while True:
            _co_defer = {
                i
                for i in _kept_set
                if any(
                    0 <= d < len(findings) and d not in _kept_set
                    for d in (findings[i].get("depends_on_indices") or [])
                )
            }
            if not _co_defer:
                break
            _kept_set -= _co_defer
        _kept_orig_indices = sorted(_kept_set)
        _batch_cap_deferred = [
            f for i, f in enumerate(findings) if i not in _kept_set
        ]
        _old_to_new = {old: new for new, old in enumerate(_kept_orig_indices)}
        findings_capped = [findings[i] for i in _kept_orig_indices]
        for _f in findings_capped:
            _deps = _f.get("depends_on_indices")
            if _deps:
                _f["depends_on_indices"] = [
                    _old_to_new[d] for d in _deps if d in _old_to_new
                ]
        return findings_capped, batch_cap_applied_count, _batch_cap_deferred
    return findings, batch_cap_applied_count, _batch_cap_deferred


def _annotate_capability_declarations(
    findings: List[Dict[str, Any]], registry_names: List[str]
) -> None:
    """Normalize each finding's capability declarations and softly flag off-registry refs.

    For every finding this:

      * normalizes an optional declared ``prerequisite`` (machine-resolvable
        ``{kind, ref}`` need) onto ``_prerequisite`` — malformed/unknown-kind
        declarations are dropped so only explicit references ever drive an edge;
      * carries an explicit ``provides_capability`` string onto
        ``_provides_capability`` so a sibling finding can structurally depend on it.

    It then attaches a SOFT, FAIL-OPEN advisory marker: any declared capability ref
    (the ``provides_capability`` stamp, or a ``{kind: capability}`` prerequisite ref)
    that is NOT in ``problem_model.capabilities`` (``registry_names``) is recorded on
    ``_off_registry_capabilities`` so the strategist can fold it into the shared
    vocabulary. The off-registry ref is NEVER rejected or dropped — it still
    resolves EXACTLY against any provider that advertises it (matchers stay exact);
    the marker is purely informational. Mutates ``findings`` in place.
    """
    for _f in findings:
        if not isinstance(_f, dict):
            continue
        _prereq_raw = _f.get("prerequisite") or _f.get("requires")
        _prereq = normalize_prerequisite(_prereq_raw)
        if _prereq is not None:
            _f["_prerequisite"] = _prereq
            _f.pop("_malformed_prerequisite", None)
        else:
            _f.pop("_prerequisite", None)
            # Retirable advisory: a declared kind=task prerequisite whose ref is
            # title-like (not a task UUID / finding hash) is malformed — it names
            # no machine-resolvable foundation, so normalize_prerequisite dropped
            # it and it must NOT become durable stranded demand nor drive an edge.
            # Record the dropped declaration (never block on it) so the strategist
            # can see it and the agent learns a named foundation belongs to
            # kind=capability. Purely additive, fail-open.
            if (
                isinstance(_prereq_raw, dict)
                and isinstance(_prereq_raw.get("kind"), str)
                and _prereq_raw["kind"].strip().lower() == "task"
                and isinstance(_prereq_raw.get("ref"), str)
                and _prereq_raw["ref"].strip()
            ):
                _f["_malformed_prerequisite"] = {
                    "kind": "task",
                    "ref": _prereq_raw["ref"].strip()[:NEED_REF_MAX_LEN],
                    "reason": (
                        "title-like task ref (not a task id or finding hash); "
                        "declare a named foundation as kind=capability"
                    ),
                }
            else:
                _f.pop("_malformed_prerequisite", None)
        # An explicit capability a finding's task advertises, so a sibling
        # finding declaring {kind:capability, ref:<name>} can structurally
        # depend on it (intra-batch foundation sharing). Explicit string only.
        _cap = _f.get("provides_capability") or _f.get("capability")
        if isinstance(_cap, str) and _cap.strip():
            _f["_provides_capability"] = _cap.strip()
        else:
            _f.pop("_provides_capability", None)
        # Soft, fail-open vocabulary convergence: flag (never drop) any declared
        # capability ref absent from the strategist-curated registry.
        _cap_refs: List[str] = []
        if _f.get("_provides_capability"):
            _cap_refs.append(_f["_provides_capability"])
        if _prereq is not None and _prereq.get("kind") == "capability":
            _cap_refs.append(_prereq["ref"])
        _off = off_registry_capability_refs(_cap_refs, registry_names)
        if _off:
            _f["_off_registry_capabilities"] = _off
        else:
            _f.pop("_off_registry_capabilities", None)


async def _parse_research_insights(task_id: str) -> List[Dict[str, Any]]:
    """Read structured research insights from the agent's /tmp output file."""
    insights_path = f"/tmp/research_insights_{task_id}.json"

    def _read_sync() -> List[Dict[str, Any]]:
        try:
            if not os.path.exists(insights_path):
                return []
            with open(insights_path) as f:
                raw = f.read(200_000)
            data = json.loads(raw)
            if isinstance(data, list):
                return [i for i in data if isinstance(i, dict) and "summary" in i]
        except Exception as e:
            logger.warning(
                f"[RESEARCH] Failed to read insights file {insights_path}: {e}"
            )
        return []

    return await asyncio.to_thread(_read_sync)


# Finding ``type`` values that declare an empirical-measurement intent and should
# route to a ``compute_experiment`` child (a budgeted, auto-torn-down compute job)
# rather than a code-writing implementer.
COMPUTE_EXPERIMENT_FINDING_TYPES: frozenset[str] = frozenset(
    {"compute_experiment", "measurement", "benchmark"}
)


def _apply_structural_actionability_downgrade(findings: list) -> int:
    """Downgrade each finding's actionability_grade when its structural support
    (claims count, valid sources, description length) does not justify the
    claimed grade. Mutates findings in place. Returns the count of downgrades.

    Extracted from _handle_research_completion so unit tests that exercise
    orthogonal behaviors (deduplication, dispatch, etc.) can patch this gate
    to a no-op via mocker.patch without having to attach structural metadata
    to every test fixture.
    """
    grade_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    downgrades = 0
    for f in findings:
        claims = f.get("claims") if isinstance(f.get("claims"), list) else []
        # Discount claims that reference code absent at HEAD: a claim asserting
        # facts about a nonexistent symbol/file must not count as structural
        # support, mirroring how an invalid file citation fails to count as a
        # valid source. _ungrounded_claim_count is set at ingestion by
        # summarize_ungrounded_claims.
        ungrounded = int(f.get("_ungrounded_claim_count", 0) or 0)
        effective_claims = max(0, len(claims) - ungrounded)
        desc = str(f.get("description") or "").strip()
        sources = f.get("sources") if isinstance(f.get("sources"), list) else []
        valid_sources = sum(
            1
            for s in sources
            if isinstance(s, dict)
            and (
                str(s.get("type") or "").lower() in ("pr", "message")
                or s.get("valid", True) is not False
            )
        )
        structural_grade = "LOW"
        if effective_claims >= 2 and valid_sources >= 1 and len(desc) >= 80:
            structural_grade = "HIGH"
        elif effective_claims >= 1 and len(desc) >= 40:
            structural_grade = "MEDIUM"
        current = f.get("actionability_grade", "MEDIUM")
        if grade_order[structural_grade] < grade_order.get(current, 1):
            f["actionability_grade"] = structural_grade
            downgrades += 1
    return downgrades


# Terminal reason for a deliver task whose declared deliverable is a sent
# message or a written report but which produced neither (and no findings). This
# is the task-orchestrator peer of compute's _NO_DATA_TERMINAL_REASON: a green
# COMPLETED here would mask a zero-output outcome (the a70fe988 pathology —
# 'no actionable findings (degraded: provider capacity)' recorded as SUCCESS).
_NO_DELIVERED_OUTPUT_TERMINAL_REASON = (
    "deliver task declared a message/report deliverable but produced none — "
    "zero findings, no message sent, and no report written; recorded as FAILED "
    "rather than a green completion that masks the empty outcome"
)

# Explicit phrases that DECLARE a task's deliverable to BE a sent message or a
# written report (as opposed to the usual research findings). The match is kept
# deliberately explicit/conservative — mirroring compute's fail-soft
# _job_expects_data guard — so an ordinary research task whose prose merely
# mentions the words 'report' or 'message' is never mis-flagged and false-failed.
_DELIVERED_OUTPUT_DELIVERABLE_PHRASES = (
    "message/report deliverable",
    "report/message deliverable",
    "deliverable of this task is a sent message",
    "deliverable of this task is a message",
    "deliverable of this task is a report",
    "deliverable of this task is a written report",
    "deliverable is a sent message",
    "deliverable is a message",
    "deliverable is a report",
    "deliverable is a written report",
    "declared deliverable is a sent message",
    "declared deliverable is a message",
    "declared deliverable is a report",
    "message deliverable",
    "report deliverable",
)

# Structured deliverable-kind values (context/payload) that declare a delivered
# output rather than parsed findings. Preferred over prose matching; this is what
# forward-looking spawners should stamp.
_DELIVERED_OUTPUT_DELIVERABLE_KINDS = {"message", "report", "message_or_report"}


def _declares_delivered_output_deliverable(task: Task) -> bool:
    """True when the task DECLARED that its deliverable is a sent message or a
    written report/document, rather than parsed research findings.

    Most research tasks deliver findings; an empty-but-honest finding set is a
    legitimate green completion for those, so this predicate must NOT fire for
    them. A subset of research/deliver tasks instead declare their deliverable to
    BE a sent operator message or a written report (e.g. operator-requested
    state-of-internals reports like task a70fe988). For those, a zero-findings
    run that ALSO sent no message and wrote no report produced nothing — the
    green-status-masks-empty-outcome pathology the no-output guard closes.

    Detection is conservative (mirrors compute's fail-soft ``_job_expects_data``):
    it fires only on an EXPLICIT structured field or an explicit deliverable
    phrase, never on incidental prose.
    """
    payload = task.first_payload or {}
    context = task.context or {}
    # 1. Structured declaration (preferred; what future spawners should set).
    for source in (context, payload):
        if not isinstance(source, dict):
            continue
        kind = source.get("deliverable_kind")
        if (
            isinstance(kind, str)
            and kind.strip().lower() in _DELIVERED_OUTPUT_DELIVERABLE_KINDS
        ):
            return True
        if source.get("requires_delivered_output") is True:
            return True
    # 2. Explicit prose declaration in the description / prompt / acceptance
    #    evidence observable_behavior.
    texts: list[str] = [task.description or "", str(payload.get("prompt") or "")]
    evidence = payload.get("acceptance_evidence")
    if isinstance(evidence, dict):
        texts.append(
            str(evidence.get("description") or evidence.get("observable_behavior") or "")
        )
    haystack = " ".join(texts).lower()
    return any(phrase in haystack for phrase in _DELIVERED_OUTPUT_DELIVERABLE_PHRASES)


async def _delivered_output_present(task: Task, session: AsyncSession) -> bool:
    """True when this task produced a durable delivered output.

    A delivered output is either a sent operator message (``ProjectMessage`` with
    ``from_task_id == task.id``) or a written report/document (``KnowledgeDocument``
    with ``source_task_id == task.id``). Used by the no-output guard to tell a
    genuinely-empty deliver task apart from one that sent/wrote its deliverable
    even though it parsed no findings.
    """
    from app.models import KnowledgeDocument, ProjectMessage

    message_id = (
        (
            await session.execute(
                select(ProjectMessage.id)
                .where(ProjectMessage.from_task_id == task.id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if message_id is not None:
        return True
    document_id = (
        (
            await session.execute(
                select(KnowledgeDocument.id)
                .where(KnowledgeDocument.source_task_id == task.id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return document_id is not None


def _record_research_survival_signal(task: Task, *, surviving: int) -> None:
    """Record the honest research success signal on the task at dispatch time.

    ``surviving`` is the count of findings that survived ingestion validation +
    dedup + actionability gating and were routed to work (spawned, or deferred
    only for capacity/health reasons). Stored on ``task.context`` so the outcome
    evaluator (``task_outcomes._evaluate_research``) scores the cycle on real
    surviving work rather than the raw parsed-findings list.

    A cycle whose findings were ALL rejected (stale duplicates, citations absent
    on HEAD, low actionability) yields ``surviving == 0``; in that case any
    first-attempt-success flag that bare-COMPLETED status set upstream is
    downgraded to honest ``False`` — a no-surviving cycle is a neutral terminal
    outcome, not a first-attempt win. The flag is never upgraded to ``True``
    here (a later attempt keeps its existing ``False``).
    """
    ctx = dict(task.context or {})
    ctx["research_surviving_findings"] = int(surviving)
    if surviving <= 0 and ctx.get("_first_attempt_succeeded"):
        ctx["_first_attempt_succeeded"] = False
    task.context = ctx
    flag_modified(task, "context")


async def _stamp_no_findings_refractory(task: Task, session: AsyncSession) -> None:
    """Stamp last_researched_at, possibly_saturated, and refractory_until on the direction.

    Called in the no-actionable-findings early-return path, which skips
    _apply_direction_state_updates entirely and therefore never sets
    last_researched_at. Without this stamp, the direction is immediately
    eligible for re-selection on the next post-merge hook (the 20s gap
    observed in the 2026-06-10 incident where cycle 10 spawned 2 minutes
    after cycle 9 completed with "no actionable findings").

    Also applies the same auto-completion criteria used by apply_post_spawn_state:
    if the direction becomes saturated (3+ consecutive unproductive cycles) and has
    accumulated enough merged PRs (>= 3), it is marked completed so that fully
    exhausted, proven directions are retired consistently regardless of whether the
    triggering cycle happened to produce any findings.
    """
    direction_id = (task.context or {}).get("direction_id") or task.first_payload.get(
        "current_direction_id"
    )
    if not direction_id or task.project_id is None:
        return
    now_iso = _utcnow().isoformat()

    _auto_complete_info: list[dict] = []

    def _apply_no_findings_refractory(dirs: list) -> None:
        target = next((d for d in dirs if d.get("id") == direction_id), None)
        if not target:
            return
        _task_id_str = str(task.id)
        target["last_researched_at"] = now_iso
        target["last_cycle_productive"] = False
        if target.get("_last_productivity_task_id") != _task_id_str:
            target["_last_productivity_task_id"] = _task_id_str
            new_consec = int(target.get("consecutive_unproductive_cycles", 0) or 0) + 1
            target["consecutive_unproductive_cycles"] = new_consec
            # Only mark saturated after 3+ consecutive unproductive cycles, consistent
            # with _compute_saturation's history-based threshold.
            target["possibly_saturated"] = new_consec >= 3
        stamp_direction_refractory(target)

        # Auto-complete the most exhausted directions (those producing zero
        # findings) rather than looping forever — same criteria as
        # apply_post_spawn_state:
        #   * PROVEN route: saturated AND >= 3 verified PRs merged.
        #   * ADDITIVE exhaustion route: genuinely exhausted (saturated with no
        #     recent output / deadlocked) with < 3 merges, under the code-failing
        #     and externally-frozen guardrails.
        if target.get("status") == "active" and target.get("possibly_saturated"):
            from app.strategic_completion import (
                _exhaustion_complete_reason,
                _should_autocomplete_exhausted,
            )

            history = target.get("research_history", [])
            agg = target.get("history_aggregate", {})
            total_merged = sum(credit_gate_merged_count(e) for e in history) + max(
                0,
                int(
                    agg.get(
                        "total_verified_prs_merged",
                        agg.get("total_prs_merged", 0),
                    )
                    or 0
                ) - int(agg.get("total_stranded_prs_merged", 0) or 0),
            )
            total_cycles = len(history) + (agg.get("total_cycles") or 0)
            # Direction completion falls back to its own proven-merge and
            # exhaustion logic. The acceptance contract is enforced only at
            # qualify (the ledger gate in compute_composite), so a direction
            # retiring while a criterion is unmet still cannot promote an unmet
            # version — there is no pre-qualification coverage precondition here.
            if total_merged >= 3:
                target["status"] = "completed"
                target["completed_at"] = now_iso
                target["auto_completed"] = True
                target["auto_completed_reason"] = (
                    f"Auto-completed: direction saturated with {total_merged} "
                    f"verified PRs merged across {total_cycles} cycles"
                )
                _auto_complete_info.append(
                    {
                        "text": target.get("text", direction_id),
                        "reason": target["auto_completed_reason"],
                        "total_merged": total_merged,
                        "total_cycles": total_cycles,
                    }
                )
            elif _should_autocomplete_exhausted(target):
                reason = _exhaustion_complete_reason(target)
                target["status"] = "completed"
                target["completed_at"] = now_iso
                target["auto_completed"] = True
                target["auto_completed_reason"] = reason
                _auto_complete_info.append(
                    {
                        "text": target.get("text", direction_id),
                        "reason": reason,
                        "total_merged": total_merged,
                        "total_cycles": total_cycles,
                    }
                )

    await with_directions_locked(
        session, task.project_id, _apply_no_findings_refractory
    )

    # Reconcile problem-model gap staffing when the direction was auto-completed.
    # The no-findings path bypasses _apply_direction_state_updates, so gap
    # reconciliation would otherwise be skipped — leaving the direction in
    # active_moves until the next lifecycle prune removes the completed entry.
    if _auto_complete_info:
        from app.problem_model import reconcile_gaps_for_retired_directions  # noqa: PLC0415

        _reconcile_now = _utcnow().isoformat()
        _retired_id = direction_id

        def _apply_gap_reconcile_no_findings(pm: dict) -> None:
            reconcile_gaps_for_retired_directions(pm, [_retired_id], _reconcile_now)

        try:
            await with_problem_model_locked(
                session, task.project_id, _apply_gap_reconcile_no_findings
            )
        except Exception as _gap_exc:
            logger.warning(
                "[NO_FINDINGS] Gap reconcile failed for auto-completed direction "
                "%s: %s",
                direction_id,
                _gap_exc,
            )

    for info in _auto_complete_info:
        from app.models import Alert, AlertSeverity  # noqa: PLC0415

        session.add(
            Alert(
                project_id=task.project_id,
                severity=AlertSeverity.INFO,
                title="Direction auto-completed",
                message=(
                    f"Direction '{info['text'][:80]}' was auto-completed: "
                    f"{info['reason']}."
                ),
            )
        )
        try:
            from app.notification_dispatch import (  # noqa: PLC0415
                post_direction_milestone_message,
            )

            await post_direction_milestone_message(
                session,
                task.project_id,
                direction_text=info["text"],
                milestone="auto-completed",
                reason=info["reason"],
                direction_id=direction_id,
            )
        except Exception as _notify_exc:
            logger.warning(
                "[NOTIFY] Failed to post direction milestone message: %s",
                _notify_exc,
            )


def _summarize_dispatch_outcome(
    spawned: int,
    total_findings: int,
    dedup_outcome: Any | None,
    deferred_reason_parts: list[str] | None = None,
    degraded_reason: str | None = None,
) -> str:
    """Build the post-dispatch status_message that surfaces spawn / dedup / defer
    counts. Today these stats live only in logs and task.context; surfacing them
    on the task itself lets the admin UI and ./tasks failures CLI show why a
    research cycle produced N children instead of M.

    When ``degraded_reason`` is set, appends a degradation qualifier so readers
    can distinguish a genuinely thorough empty result from one truncated by a
    provider capacity/rate-limit error mid-run.
    """
    parts: list[str] = [f"spawned {spawned}/{total_findings} findings"]
    if dedup_outcome is not None:
        dedup_breakdown: list[str] = []
        if getattr(dedup_outcome, "hash_dedup_count", 0):
            dedup_breakdown.append(f"{dedup_outcome.hash_dedup_count} hash")
        if getattr(dedup_outcome, "word_overlap_count", 0):
            dedup_breakdown.append(f"{dedup_outcome.word_overlap_count} word-overlap")
        if getattr(dedup_outcome, "file_overlap_count", 0):
            dedup_breakdown.append(f"{dedup_outcome.file_overlap_count} file-overlap")
        if getattr(dedup_outcome, "evidence_overlap_count", 0):
            dedup_breakdown.append(
                f"{dedup_outcome.evidence_overlap_count} evidence-overlap"
            )
        if getattr(dedup_outcome, "retired_approach_count", 0):
            dedup_breakdown.append(
                f"{dedup_outcome.retired_approach_count} retired-approach"
            )
        if getattr(dedup_outcome, "archived_diagnoses_count", 0):
            dedup_breakdown.append(
                f"{dedup_outcome.archived_diagnoses_count} archived-diagnosis"
            )
        if dedup_breakdown:
            parts.append("dedup filtered: " + ", ".join(dedup_breakdown))
    if deferred_reason_parts:
        parts.append("deferred: " + " + ".join(deferred_reason_parts))
    if degraded_reason:
        parts.append(f"degraded: {degraded_reason} — results may be incomplete")
    return " | ".join(parts)[:500]


def _check_capacity_degradation(task: Task, logs: str = "") -> str | None:
    """Check whether a research task encountered a provider capacity/rate-limit
    error during its run that may have degraded its output.

    Examines (1) the task context for a persisted ``EXTERNAL_API`` /
    ``provider_quota`` failure classification (set by
    :func:`persist_failure_category` during a terminal-FAILED path on a prior
    pass, or mid-run by the agent-status router), and (2) the task's current
    ``latest_status_message`` plus the ``logs`` string for provider-quota
    signal keywords via :func:`_external_api_subtype_from_message`.

    Returns a short human-readable reason string when degradation is detected,
    or ``None`` when the task shows no capacity-signal evidence.
    """
    if task is None:
        return None
    # 1. Persisted failure classification on task context.
    ctx = task.context or {}
    fc = ctx.get("failure_category")
    est = ctx.get("external_api_subtype")
    if fc == EXTERNAL_API and est == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA:
        return "provider capacity/rate-limit error"

    # Compute the message subtype once from status + logs for steps 2-3.
    subtype = classify_external_api_subtype(task, logs)

    # 2. Agent self-reported failure category — cross-check against message text
    #    to avoid false positives from unrelated ``claude_code_failed`` events.
    raw_fc = ctx.get("agent_failure_category_raw", "")
    if raw_fc in ("rate_limited", "claude_code_failed") and subtype == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA:
        return "provider capacity/rate-limit error"

    # 3. Current status message + logs — catches errors the agent reported
    #    mid-run that weren't separately persisted to task context.
    if subtype == EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA:
        return "provider capacity/rate-limit error"

    return None


def _mark_capacity_degraded(task: Task, reason: str) -> None:
    """Record a capacity-degradation signal on a task's context dict.

    Used by both the no-findings path and the dispatch path so the structured
    indicator stays consistent across both sites.
    """
    _ctx = dict(task.context or {})
    _ctx["degraded_by_capacity"] = True
    _ctx["degraded_by_capacity_reason"] = reason
    task.context = _ctx
    flag_modified(task, "context")


# Finding ``type`` values that are processed separately and never spawn an
# implementation/child task (direction proposals/notes, design-doc observations,
# direction updates). Used by the blast-radius predicate below.
_NON_SPAWNING_FINDING_TYPES = frozenset(
    {
        "direction_proposal",
        "direction_note",
        "design_doc_observation",
        "direction_update",
        "note",
    }
)

def _compute_direction_productivity(
    direction_id: str,
    tasks_by_dir: dict[str, int],
    current_direction_id: str | None,
    has_non_spawn_findings: bool,
) -> bool:
    """Return True when a direction's research cycle should be credited productive.

    A cycle is productive when it spawns >=1 child task for that direction OR
    when it produces non-spawning findings (verification/metadata types such as
    design_doc_observation, direction_note, or direction_proposal) for the
    current direction.  Crediting verification-only cycles prevents live-verification
    sweeps and concluded design/research cycles from incurring the longer unproductive
    refractory window.

    Scope note: this flag drives the COOLDOWN cadence only (``direction_refractory``
    picks the fast productive window when ``last_cycle_productive`` is True). It is a
    spawn-/notes-level signal and deliberately does NOT mean the direction LANDED
    work, so it must NOT be treated on its own as a merge-rate pause shield: a
    direction that spawns every cycle but never merges is still spawn-productive here
    yet is churn for pause purposes. The still-productive pause shield
    (``strategic_completion._direction_still_productive``) therefore additionally
    requires a real landing / no-op-completion signal from the direction's history —
    see that function.
    """
    if tasks_by_dir.get(direction_id, 0) > 0:
        return True
    return has_non_spawn_findings and direction_id == current_direction_id


# Research findings only seed a reproduce-on-HEAD verdict when the finding is an
# explicit defect claim. Other finding types (feature/improvement/refactor/etc.)
# keep the legacy "no verdict" semantics so merged implementation work still
# scores as success instead of shipped-but-unproven.
_REPRODUCE_VERDICT_FINDING_TYPES = frozenset({"bug"})

# Child task types whose agent handler renders the shared capability vocabulary
# (research/plan via research-plan.ts, design via design.ts). The additive,
# optional ``available_capabilities`` payload field is injected only for these
# read-only planning/knowledge children — implementation children (create/fix/
# refactor) do not render it, so the field is omitted to keep their payload lean.
_VOCAB_RENDERING_TASK_TYPES = frozenset({"plan", "research", "design"})


def _is_high_stakes_finding(finding: Dict[str, Any]) -> bool:
    """Return True when a finding warrants an adversarial second opinion.

    Selects findings with large blast radius (scope=large, risk=high) OR any
    finding carrying a partial-staleness / over-claim signal regardless of scope
    (downgraded verification_basis, broken file refs, ungrounded path:symbol
    claims, cited-files-changed staleness). Non-spawning types and LOW-
    actionability findings are never high-stakes (they don't launch work).
    """
    if not isinstance(finding, dict):
        return False
    if finding.get("type") in _NON_SPAWNING_FINDING_TYPES:
        return False
    # A finding must have a non-blank prompt or description to potentially spawn work.
    # Checks prompt first so that an explicitly blank prompt (e.g. "   ") treats the
    # finding as bodiless even when a non-empty description is also present.
    if not str(finding.get("prompt") or finding.get("description") or "").strip():
        return False
    if finding.get("already_spawned"):
        return False
    if finding.get("actionability_grade") == "LOW":
        return False
    # Capability-foundation work is structurally non-adversarial: a finding that
    # declares it provides a capability, or one that depends on a capability,
    # should flow through the normal capability resolver instead of getting
    # parked in the refute-first spawn gate.
    if finding.get("_provides_capability") or finding.get("provides_capability"):
        return False
    _capability_prereq = finding.get("_prerequisite") or normalize_prerequisite(
        finding.get("prerequisite") or finding.get("requires")
    )
    if (
        isinstance(_capability_prereq, dict)
        and _capability_prereq.get("kind") == "capability"
    ):
        return False

    # Large blast radius: large scope
    if str(finding.get("scope") or "").lower() == "large":
        return True

    # High risk
    if str(finding.get("risk") or "").lower() in ("high", "critical", "very_high"):
        return True

    # Over-claim signal: verification_basis was downgraded because citations failed HEAD
    if finding.get("verification_basis_downgraded_from"):
        return True

    # Provenance warnings: broken file refs or ungrounded path:symbol claims
    prov = finding.get("_provenance_warnings")
    if isinstance(prov, dict):
        if prov.get("broken_refs") or prov.get("ungrounded_refs"):
            return True
        if prov.get("cited_files_changed_since_generation"):
            return True

    # Deferred-finding staleness: cited files changed since generation
    if finding.get("needs_revalidation") is True:
        return True

    # Compute-experiment: GPU use, material spend, or long deadline are high-stakes.
    # A budgeted compute launch that proposes accelerator hardware, exceeds or
    # approaches the autonomous spend threshold, or runs for longer than one hour
    # warrants an independent adversarial challenge before the job is created.
    # Malformed contracts (missing/invalid spend_cap_usd) are NOT flagged here —
    # they defer through the existing _validate_compute_launch_contract path.
    if finding.get("type") in COMPUTE_EXPERIMENT_FINDING_TYPES:
        _nested = (
            finding.get("compute_experiment") or finding.get("launch_contract") or {}
        )
        if not isinstance(_nested, dict):
            _nested = {}

        def _cpick(key: str) -> Any:
            val = _nested.get(key)
            if val is None:
                val = finding.get(key)
            return val

        _accel = _cpick("accelerator")
        if _accel and str(_accel).strip():
            return True

        _spend = _cpick("spend_cap_usd")
        try:
            _spend_f = float(_spend)
        except (TypeError, ValueError):
            _spend_f = None
        if _spend_f is not None and _spend_f > 0:
            _threshold = resolve_compute_spend_threshold(_runtime_config, None)
            if _threshold == 0 or _spend_f >= _threshold * 0.5:
                return True

        _deadline = _cpick("deadline_seconds")
        try:
            _deadline_i = int(_deadline)
        except (TypeError, ValueError):
            _deadline_i = None
        if _deadline_i is not None and _deadline_i > 3600:
            return True

    return False


def _research_findings_would_spawn(findings: List[Dict[str, Any]]) -> bool:
    """True if at least one finding would spawn downstream implementation work.

    This is a blast-radius predicate, not the exact spawn decision (dedup and
    confidence gating still run in the spawn loop). It deliberately over-counts
    rather than under-counts: it is better to grade a research artifact that
    later dedups to zero children than to let an ungraded finding spawn a PR.
    """
    for f in findings:
        if not isinstance(f, dict):
            continue
        if (f.get("type") or "") in _NON_SPAWNING_FINDING_TYPES:
            continue
        if not str(f.get("prompt") or "").strip():
            continue
        if f.get("already_spawned"):
            continue
        if f.get("actionability_grade") == "LOW":
            continue
        return True
    return False


def converge_spawn_batch_foundations(
    child_tasks: List[Task],
    active_tasks: List[Task],
) -> List[Task]:
    """Serialize genuine duplicate foundations at spawn time, BEFORE the just-
    spawned children begin executing.

    Considers the new batch (``child_tasks``) AND currently-active tasks
    (``active_tasks``) together and reuses the SAME high-confidence predicate and
    apply-with-guards helper as the reaper Step 15 sweep
    (``digest_metrics.compute_foundation_convergence_actions`` /
    ``apply_foundation_convergence_action``), so spawn-time and reaper-time
    convergence can never drift. For each genuine-duplicate pair — a shared finding
    origin, a shared explicit capability marker, or near-identical related-file
    sets at the convergence Jaccard — the LATER task is wired to depend on the
    earlier (canonical) one so the duplicate rebases on the canonical's landed work
    and builds-on instead of rebuilding.

    Only a just-spawned child is ever made the dependent: the canonical is always
    the earliest-by-``created_at`` task, so a pre-existing active task is always the
    canonical and is never re-pointed at a peer here (active-vs-active pairs are the
    reaper's job). Conservative by construction — sub-threshold incidental
    shared-infra overlap is left to proceed in parallel and reconcile at merge.
    ``exclude_same_parent=False`` is passed because a batch's children all share the
    one research parent; coordinated siblings (already linked by explicit
    ``depends_on`` / auto-decomposition chaining) are instead left alone by the
    apply-with-guards helper, which refuses an already-present or cycle-closing
    edge along with a self-edge.

    Returns the list of dependent child tasks that had an edge wired (each already
    mutated in place with ``depends_on`` plus the ``_foundation_converged`` context
    marker); the caller persists them.
    """
    from app.digest_metrics import (
        apply_foundation_convergence_action,
        compute_foundation_convergence_actions,
    )

    candidates = list(active_tasks) + list(child_tasks)
    by_id = {str(t.id): t for t in candidates}
    child_ids = {str(c.id) for c in child_tasks}
    converged: List[Task] = []
    # exclude_same_parent=False: a batch's children all share the one research
    # parent, so the coarse parent check would mask every within-batch duplicate.
    # Coordinated siblings (already linked by explicit depends_on / depends_on_indices
    # or auto-decomposition chaining) are still left alone — the apply-with-guards
    # helper refuses an already-present or cycle-closing edge.
    for action in compute_foundation_convergence_actions(
        candidates, exclude_same_parent=False
    ):
        if action["dependent_id"] not in child_ids:
            continue
        dependent = apply_foundation_convergence_action(
            action, by_id, candidates, logger=logger
        )
        if dependent is None:
            continue
        converged.append(dependent)
        logger.info(
            "[RESEARCH] Serialized duplicate foundation at spawn — task %s now "
            "depends on %s (%s)",
            dependent.id,
            action["foundation_id"],
            action["reason"],
        )
    if converged:
        logger.info(
            "[RESEARCH] Spawn-time foundation convergence wired %d edge(s)",
            len(converged),
        )
    return converged


# Mirrors the ux_task_active_idempotency_key partial-index predicate EXACTLY
# (models.py / alembic 0199): the statuses whose rows occupy an idempotency
# slot. Used by the self-claim collision probe in
# _commit_or_handle_duplicate_spawn — a status the index counts but this list
# omits would misclassify a recoverable self-claim collision as a duplicate
# batch and discard the spawned children. Same mirror as reaper.py's C2 guard.
_IDEMPOTENCY_INDEX_ACTIVE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)


def _is_active_idempotency_integrity_error(exc: IntegrityError) -> bool:
    """Return True for the active idempotency partial-index collision."""
    message = str(exc)
    return (
        "ux_task_active_idempotency_key" in message
        or "UNIQUE constraint failed: task.idempotency_key" in message
    )


async def _commit_or_handle_duplicate_spawn(
    session: AsyncSession, task: Task, *, stage: str
) -> bool:
    """Commit the spawn batch, distinguishing the two idempotency-collision causes.

    Returns True when the commit landed (directly, or after self-claim
    recovery below). Returns False when the commit was rejected by
    ``ux_task_active_idempotency_key`` for a genuinely duplicate batch — a
    peer (a concurrent post-commit retry, or a pipelined duplicate research
    cycle) already holds the colliding key in an active status, so this
    batch's rollback loses nothing. Without the rollback here the poisoned
    session cascaded PendingRollbackError through the rest of the request
    (prod burst 2026-06-07 00:56). Any other IntegrityError re-raises into
    the generic handler-failure recovery.

    SELF-CLAIM RECOVERY: the colliding key can also be this task's OWN
    direction-claim key (research_direction_claim_key) rather than a
    duplicate child batch. The agent-reported terminal commit briefly parks
    the parent COMPLETED, which releases the claim (the partial index only
    covers active statuses); if the autonomous loop re-claims the direction
    with a fresh research spawn inside that window, the EVALUATING flip in
    THIS commit re-enters the index with the same key and collides — but the
    child tasks are unique, undispatched work, not duplicates (prod
    2026-06-09: task 5ac5c8c1's whole 2-finding batch was discarded and the
    direction re-researched from scratch). Detected by probing whether a
    DIFFERENT task now actively holds this task's key; recovered by releasing
    this task's claim (the new peer legitimately owns the direction now),
    re-applying the batch, and committing again.

    On the False path the session has been rolled back — which expires every
    loaded ORM instance even with expire_on_commit=False — and ``task`` has
    been force re-populated from its DB row so the caller (and
    _process_post_commit after it) can keep reading its attributes without
    tripping a sync lazy-load (MissingGreenlet).
    """
    _task_pk = task.id
    _task_id_str = str(_task_pk)
    # Snapshot BEFORE the commit attempt: rollback() expunges pending INSERTs
    # and expires every loaded attribute, so this state is unrecoverable
    # afterwards. __dict__ reads only (never attribute access) — no lazy IO.
    _pending_objects = list(session.new)
    _parent_state = {
        attr: task.__dict__[attr]
        for attr in ("status", "context", "latest_status_message")
        if attr in task.__dict__
    }
    _own_key = task.__dict__.get("idempotency_key")
    try:
        await session.commit()
    except IntegrityError as _dup_exc:
        if not _is_active_idempotency_integrity_error(_dup_exc):
            raise
        await session.rollback()
        # ``populate_existing`` (not refresh()) so any rolled-back in-memory
        # task mutation is discarded rather than re-flushed; ``no_autoflush``
        # so the re-load itself cannot autoflush it and re-raise.
        with contextlib.suppress(Exception), session.no_autoflush:
            await session.execute(
                select(Task)
                .where(Task.id == _task_pk)
                .execution_options(populate_existing=True)
            )
        # Self-claim probe: does a DIFFERENT task actively hold THIS task's
        # key? If so the flip back into the active set is what collided (or
        # is guaranteed to collide on retry), and the batch is unique work.
        # In the true-duplicate case the key holder is this task itself
        # (Task.id != excludes it), so the probe stays empty and the
        # duplicate disposition below is unchanged.
        _peer_id = None
        if _own_key:
            with contextlib.suppress(Exception):
                _peer_id = (
                    (
                        await session.execute(
                            select(Task.id).where(
                                Task.idempotency_key == _own_key,
                                Task.id != _task_pk,
                                Task.status.in_(_IDEMPOTENCY_INDEX_ACTIVE_STATUSES),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
        if _peer_id is not None:
            # Release this task's claim — the peer legitimately re-claimed the
            # direction while this task sat terminal — and re-apply the batch:
            # re-add the (now-transient, attribute-state-intact) pending rows
            # and restore the parent's spawn-commit mutations on top of the
            # re-populated DB state. Mutations on OTHER persistent rows in the
            # original commit are not replayed — same loss as the prior
            # all-batch rollback; the children and the EVALUATING flip are the
            # durable state that must not be lost.
            task.idempotency_key = None
            for attr, value in _parent_state.items():
                setattr(task, attr, value)
            if "context" in _parent_state:
                flag_modified(task, "context")
            for _obj in _pending_objects:
                session.add(_obj)
            session.add(task)
            try:
                await session.commit()
            except IntegrityError as _retry_exc:
                if not _is_active_idempotency_integrity_error(_retry_exc):
                    raise
                # Still colliding: some other key in the batch is genuinely
                # duplicated — fall through to the duplicate disposition.
                await session.rollback()
                with contextlib.suppress(Exception), session.no_autoflush:
                    await session.execute(
                        select(Task)
                        .where(Task.id == _task_pk)
                        .execution_options(populate_existing=True)
                    )
            else:
                logger.warning(
                    f"[RESEARCH] Task {_task_id_str}: {stage} commit collided on "
                    f"its OWN direction-claim key — peer {_peer_id} re-claimed "
                    f"the direction while this task sat terminal. Released this "
                    f"task's claim and re-committed the batch "
                    f"({len(_pending_objects)} pending row(s) preserved)."
                )
                return True
        logger.warning(
            f"[RESEARCH] Task {_task_id_str}: {stage} commit hit "
            f"ux_task_active_idempotency_key (a peer already spawned this work "
            f"— duplicate-spawn guard working). Rolled back this duplicate "
            f"batch; the peer owns the post-spawn state: {str(_dup_exc)[:200]}"
        )
        return False
    return True


def _build_salvage_payload(
    salvage_candidates: list[dict],
    source_task_id: uuid.UUID | str,
) -> dict:
    """Build the salvage bundle payload dict from decayed/discarded findings.

    Each finding entry carries rich context (prompt, description, related_files,
    claims, sources) so the consuming agent can address all findings in one PR.
    """
    return {
        "type": "salvage_bundle",
        "source_task_id": str(source_task_id),
        "salvaged_findings": [
            {
                "title": f.get("title") or (f.get("description") or "")[:80],
                "discard_reason": f.get("deferred_reason") or "unknown",
                "direction_id": f.get("direction_id"),
                "prompt": f.get("prompt"),
                "description": (f.get("description") or "")[:500] if f.get("description") else None,
                "related_files": f.get("related_files"),
                "claims": f.get("claims"),
                "sources": f.get("sources"),
            }
            for f in salvage_candidates
        ],
        "total_discarded": len(salvage_candidates),
    }


#: Durable context key on a spawned dependent recording capability prerequisites
#: that resolved as satisfied-by-the-codebase but whose ``working`` capability
#: carries no ``proven`` evidence — the SPAWN-time surfacing of the unverified
#: status (observe/off modes). Each entry: ``{kind, ref, proof_status}``.
SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY = "_spawn_capability_proof_unverified"


def spawn_capability_gate_disposition(
    need: Mapping[str, Any],
    proof_status_by_name: Mapping[str, Any],
    *,
    mode: str,
) -> tuple[str, Any]:
    """Classify how the spawn-time resolver must treat a capability prerequisite
    that already resolved as satisfied-by-the-codebase.

    Returns ``(disposition, proof_status)`` where ``disposition`` is one of:

    * ``"block"`` — ENFORCE mode and the capability is not ``proven``: the
      prerequisite does NOT satisfy the dependent, which must park BLOCKED (the
      need is wired as blocking). Mirrors
      :func:`capability_need_blocked_by_proof_gate` exactly, so this seam cannot
      drift from the blocked-resume sweep or the release-qualifier seam.
    * ``"surface"`` — non-ENFORCE mode (off/observe) and a KNOWN non-``proven``
      status (``unproven`` / ``partial`` / ``unverified`` / ``contradicted``):
      the need still resolves as satisfied-by-the-codebase, but the unverified
      status is surfaced durably on the dependent (see
      :data:`SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY`) so an operator can see the
      unproven foundation instead of it living only in a log line.
    * ``"ok"`` — the capability is ``proven``, or the need matches no ``working``
      registry capability (``proof_status is None``): satisfied, no gate, nothing
      to surface.

    Pure and IO-free: the single source of truth for the spawn seam's three-way
    branch, so the classification and the enforce decision live in one place.
    """
    if capability_need_blocked_by_proof_gate(need, proof_status_by_name, mode=mode):
        return "block", capability_need_proof_status(need, proof_status_by_name)
    proof_status = capability_need_proof_status(need, proof_status_by_name)
    if proof_status is not None and proof_status != "proven":
        return "surface", proof_status
    return "ok", proof_status


async def _handle_research_completion(task: Task, logs: str, session: AsyncSession):
    """Parse research findings and create tasks from them.

    Forward-quality gate (escalated path): when the research-quality grader is
    enabled and the parsed findings would spawn downstream implementation work,
    this handler does NOT spawn children on the first pass. It instead spawns an
    INDEPENDENT grader task and parks itself EVALUATING. The grader's verdict
    returns through the re-entrant completion path, which re-invokes this
    handler with ``context['quality_grader']`` set:
      * verdict PASS  -> proceed to the normal spawn-on-findings path;
      * verdict FAIL  -> fail the research task and spawn NOTHING (unverified
        findings must never launch work).
    Findings that would not spawn anything (pure observations) keep the cheap
    synchronous check and never escalate — the gate is proportional to blast
    radius, as the framework intends.
    """
    from app.task_evaluators import (
        ADVERSARIAL_GATE_VERDICT_KEY,
        QUALITY_GRADER_VERDICT_KEY,
        EvaluationVerdict,
        record_failed_gate,
        spawn_forward_quality_grader,
    )

    # Re-entry signal: the independent grader has returned its verdict.
    _grader_verdict = (task.context or {}).get(QUALITY_GRADER_VERDICT_KEY)
    _adversarial_verdict_raw = (task.context or {}).get(ADVERSARIAL_GATE_VERDICT_KEY)
    _finding_challenge_verdict = (task.context or {}).get(FINDING_CHALLENGE_VERDICT_KEY)

    # Parked-awaiting guards: re-dispatch while a sidecar verdict is pending is
    # a safe no-op — return immediately so the spawn loop does not run ungated.
    # The stranded-sidecar sweep owns redelivery when a sidecar dies silently.
    if (task.context or {}).get(
        "awaiting_adversarial_grader"
    ) and _adversarial_verdict_raw is None:
        logger.info(
            "[RESEARCH] Task %s re-dispatched while parked awaiting adversarial "
            "grader — no-op (verdict not yet present)",
            task.id,
        )
        return
    if (task.context or {}).get(
        "awaiting_finding_challenge"
    ) and _finding_challenge_verdict is None:
        logger.info(
            "[RESEARCH] Task %s re-dispatched while parked awaiting finding "
            "challenge — no-op (verdict not yet present)",
            task.id,
        )
        return

    if _grader_verdict is not None and not _grader_verdict.get("passed", False):
        # FAILED forward-quality gate: do NOT spawn implementation tasks and do
        # NOT complete clean. Mark FAILED so the research is not trusted, and
        # record the verdict for observability + direction-health accounting.
        record_failed_gate(
            task,
            EvaluationVerdict(
                False,
                str(_grader_verdict.get("reason") or "forward-quality grade failed"),
                "forward-quality",
            ),
        )
        session.add(task)
        await session.commit()
        try:
            await _record_research_task_failure(task, session)
        except Exception:  # noqa: BLE001 — failure accounting must not crash the gate
            logger.debug(
                "[RESEARCH] failure accounting after failed quality gate raised",
                exc_info=True,
            )
        logger.info(
            "[RESEARCH] Task %s failed the forward-quality gate; spawned no "
            "implementation tasks: %s",
            task.id,
            _grader_verdict.get("reason"),
        )
        return

    # Increment _research_spawn_count on completion (variable name is legacy;
    # see autonomous_research.py:837 — semantic is "completed research cycles
    # since last strategic refresh"). The strategic_research_frequency gate
    # uses this to decide when to fire a strategic_review. Incrementing here
    # rather than at spawn time prevents the cascade observed on 2026-05-03
    # where one strategic_review spawned 4 research tasks AT ONCE and
    # immediately tripped the modulo-6 gate for the next strategic_review.
    project_obj = await session.get(Project, task.project_id)

    _task_id_str = str(task.id)

    def _increment_research_spawn_count(vision: dict) -> None:
        # Idempotency guard: on post-commit replay the three-verdict guard
        # below still passes (all verdicts are None on the initial pass), so
        # we track counted task IDs to prevent double-incrementing.
        counted = vision.get("_research_spawn_counted_ids") or []
        if _task_id_str in counted:
            return
        vision["_research_spawn_count"] = (vision.get("_research_spawn_count") or 0) + 1
        vision["_research_spawn_counted_ids"] = (counted + [_task_id_str])[
            -_RESEARCH_SPAWN_COUNTED_IDS_MAX:
        ]

    # Only on the FIRST pass — a re-entrant resume after grading or a
    # per-finding challenge must not re-count the cycle (verdict already
    # present means we're on a re-entrant pass, not the initial completion).
    if (
        project_obj is not None
        and _grader_verdict is None
        and _adversarial_verdict_raw is None
        and _finding_challenge_verdict is None
    ):
        await with_vision_locked(
            session, task.project_id, _increment_research_spawn_count
        )

    # Assess whether this research task encountered a provider capacity/rate-limit
    # error mid-run but still completed (rather than hard-failing). When detected,
    # the reason is surfaced in the completion message and task context so
    # downstream readers (strategist, operator, future research cycles) can
    # distinguish thin-but-honest results from capacity-truncated ones.
    _capacity_degradation_reason = _check_capacity_degradation(task, logs)

    # Depth guard: prevent infinite research recursion
    MAX_RESEARCH_DEPTH = 3
    research_depth = (task.context or {}).get("research_depth", 0)
    parent_research_task_id = (task.context or {}).get("parent_research_task_id")
    if research_depth >= MAX_RESEARCH_DEPTH:
        task.latest_status_message = f"Research depth limit ({MAX_RESEARCH_DEPTH}) reached — not spawning further tasks"
        session.add(task)
        await session.commit()
        logger.warning(
            f"[RESEARCH] Depth limit reached for task {task.id} (depth={research_depth})"
        )
        return

    findings = await asyncio.to_thread(_parse_research_findings, logs)
    insights = await _parse_research_insights(str(task.id))
    # Agent JSON sometimes contains non-dict entries (bare strings, nulls).
    # The downstream .get() loop would AttributeError and the handler-failed
    # branch in task_lifecycle would terminally FAIL a research task that
    # actually completed. Drop them here.
    if findings:
        findings = [f for f in findings if isinstance(f, dict)]
    if (
        not findings
        and _grader_verdict is not None
        and isinstance(task.plan_output, list)
    ):
        # Grader resume: the re-dispatch reconstructs the artifact from the
        # findings stored on the first pass. If that reconstruction does not
        # round-trip through the findings parser, fall back to the stored
        # findings directly so a PASSING grade can never silently spawn nothing.
        findings = [f for f in task.plan_output if isinstance(f, dict)]
    if (
        not findings
        and (task.context or {}).get("_research_spawn_pending")
        and isinstance(task.plan_output, list)
    ):
        # Spawn-pending resume: a prior pass was interrupted mid-spawn (lock
        # timeout / network error after _ensure_branch committed the marker).
        # The sweep retry replays with the same agent logs, which may no longer
        # parse cleanly. Fall back to the findings committed to plan_output on
        # the first pass so the spawn can proceed without re-running the agent.
        findings = [f for f in task.plan_output if isinstance(f, dict)]
        logger.info(
            "[RESEARCH] Task %s: resuming interrupted spawn from stored plan_output",
            task.id,
        )
    # Stranded-resume backstop (cause B). A research task can reach this handler
    # with no parseable findings in the (possibly stale/re-dispatched) logs while
    # task.plan_output STILL holds the findings a prior pass committed AND zero
    # children were ever spawned. This is the "completed/redispatched but
    # stranded" state: the spawn errored mid-flight on the first pass and the
    # _research_spawn_pending marker was never set (or was cleared) — so neither
    # resume guard above fires, and the branch below would clobber plan_output to
    # [] and report "no actionable findings" despite real undispatched work
    # sitting in plan_output. Detect it explicitly and resume the spawn from the
    # stored findings instead of burying them. Idempotent: if children already
    # exist this guard is skipped, and the downstream duplicate-spawn guard makes
    # any genuine re-run a no-op.
    if (
        not findings
        and isinstance(task.plan_output, list)
        and task.plan_output
    ):
        _stored_findings = [f for f in task.plan_output if isinstance(f, dict)]
        if _stored_findings:
            _existing_child_id = (
                (
                    await session.execute(
                        select(Task.id)
                        .where(Task.parent_task_id == task.id)
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if _existing_child_id is None:
                findings = _stored_findings
                logger.warning(
                    "[RESEARCH] Task %s: STRANDED research detected — %d finding(s) "
                    "in plan_output but 0 children spawned and logs no longer parse; "
                    "resuming spawn from stored findings (was previously buried as "
                    "'no actionable findings')",
                    task.id,
                    len(_stored_findings),
                )
                task.latest_status_message = (
                    f"Recovering stranded research: resuming spawn of "
                    f"{len(_stored_findings)} stored finding(s)"
                )[:500]
    if not findings:
        # No-output guard: a task whose DECLARED deliverable is a sent message or
        # a written report, but which parsed zero findings AND sent no message AND
        # wrote no report, produced nothing. Record it as a terminal FAILURE
        # instead of the green COMPLETED it inherited — otherwise the empty
        # outcome is masked as success (task a70fe988: 'no actionable findings
        # (degraded: provider capacity)' recorded green). Ordinary research tasks
        # (no declared message/report deliverable) stay green on an honest empty
        # finding set — the predicate is conservative to avoid false failures.
        _expects_delivered_output = _declares_delivered_output_deliverable(task)
        _delivered_output = (
            await _delivered_output_present(task, session)
            if _expects_delivered_output
            else False
        )
        _no_output_downgrade = _expects_delivered_output and not _delivered_output

        if _no_output_downgrade:
            _reason = _NO_DELIVERED_OUTPUT_TERMINAL_REASON
            if _capacity_degradation_reason:
                _reason = f"{_reason} (degraded: {_capacity_degradation_reason})"
                _mark_capacity_degraded(task, _capacity_degradation_reason)
            task.status = TaskStatus.FAILED
            task.latest_status_message = _reason[:500]
        elif _capacity_degradation_reason:
            task.latest_status_message = (
                "Research completed with no actionable findings "
                f"(degraded: {_capacity_degradation_reason} — results may be incomplete)"
            )
            _mark_capacity_degraded(task, _capacity_degradation_reason)
        else:
            task.latest_status_message = "Research completed with no actionable findings"
        task.plan_output = []
        _record_research_survival_signal(task, surviving=0)
        # Stamp refractory on the direction before committing — this path skips
        # _apply_direction_state_updates, so last_researched_at would otherwise
        # never be set and the direction would be immediately eligible again.
        await _stamp_no_findings_refractory(task, session)
        session.add(task)
        await session.commit()
        if _no_output_downgrade:
            # Mirror the forward-quality FAIL path: record the failure on the
            # direction's research_history so direction-health can observe the
            # empty deliver task. Guarded so accounting can never crash the
            # handler (the task is already terminally FAILED and committed).
            try:
                await _record_research_task_failure(task, session)
            except Exception:  # noqa: BLE001 — accounting must not crash the gate
                logger.debug(
                    "[RESEARCH] failure accounting after no-delivered-output "
                    "downgrade raised",
                    exc_info=True,
                )
        # Evergreen-fill completion adapter: zero findings → verified_stale.
        # This early-return path bypasses the main adapter call below, so we
        # must fire it here.
        _evergreen_item_id_early = (task.context or {}).get("evergreen_backlog_item_id")
        if _evergreen_item_id_early:
            try:
                from app.evergreen_backlog import apply_research_outcome

                await apply_research_outcome(
                    session,
                    item_id=str(_evergreen_item_id_early),
                    impl_spawned=0,
                    reproduce_rejected=0,
                    surviving=0,
                )
            except Exception as _eg_exc:  # noqa: BLE001
                logger.warning(
                    "[EVERGREEN] failed to apply research outcome for item %s: %s",
                    _evergreen_item_id_early,
                    _eg_exc,
                )
            await session.commit()
        return

    # Normalize actionability_grade: any missing or invalid value defaults to MEDIUM.
    # Then downgrade when the finding structure does not support the claimed grade.
    _valid_grades = {"HIGH", "MEDIUM", "LOW"}
    for _f in findings:
        _g = _f.get("actionability_grade")
        if _g not in _valid_grades:
            logger.warning(
                "[RESEARCH] Finding '%s' has missing or invalid actionability_grade=%r; "
                "defaulting to MEDIUM",
                _f.get("title", "<untitled>"),
                _g,
            )
            _f["actionability_grade"] = "MEDIUM"

    # Normalize verification_basis: how the finding's claim was established
    # (evidence strength), orthogonal to actionability_grade (implementability).
    # Any missing or invalid value defaults to the lowest-evidence tier
    # (pattern_inferred) so legacy findings are never mis-promoted. A non-missing
    # invalid value is logged just like the grade path.
    for _f in findings:
        _vb = _f.get("verification_basis")
        if _vb not in VALID_VERIFICATION_BASES:
            if _vb is not None:
                logger.warning(
                    "[RESEARCH] Finding '%s' has invalid verification_basis=%r; "
                    "defaulting to pattern_inferred",
                    _f.get("title", "<untitled>"),
                    _vb,
                )
            _f["verification_basis"] = "pattern_inferred"

    # Resolve the project's real integration-branch verification context once so
    # file-type source refs validate against the GitHub tree rather than a local
    # /tmp/workspace checkout, which the backend/orchestrator does not have (git
    # isn't even installed there). Without this every file citation collapses to
    # "not found" and gets a spurious broken-reference downgrade. Degrades
    # gracefully: on any failure the branch-only dict (or empty) is used, which
    # makes validate_source_refs fall back to local git HEAD, and an
    # indeterminate result there never marks a citation invalid.
    _src_ref_ctx: Dict[str, Any] = {}
    if project_obj is not None:
        try:
            _src_ref_ctx = await resolve_branch_verification_context(
                session, project_obj
            )
        except Exception:  # noqa: BLE001 — context resolution must never crash ingestion
            logger.debug(
                "[RESEARCH] source-ref verification context resolution failed; "
                "falling back to local git HEAD",
                exc_info=True,
            )
            _src_ref_ctx = {}

    # Whether ANY existence/symbol check can actually run (GitHub creds, or a
    # local git checkout). When neither is available every grounding check
    # degrades to indeterminate, so the reproduce-on-HEAD verdict must NEVER
    # be "absent" — :func:`classify_reproduction` short-circuits to
    # indeterminate on a falsy backend. Resolved once and shared by both the
    # fresh-ingestion gate and the deferred-recovery re-grounding below.
    _has_backend = _has_validation_backend(
        _src_ref_ctx.get("token"),
        _src_ref_ctx.get("owner"),
        _src_ref_ctx.get("repo"),
        _src_ref_ctx.get("branch"),
        "/tmp/workspace",
    )

    # Record the integration HEAD these findings were generated against, so a
    # later re-dispatch can answer the one cheap question that gates trust: have
    # the cited files changed since? Resolved once against the same verification
    # context the citation checks use. Degrades to None (no stamp) when the SHA
    # is unknown (no creds + no checkout) — a missing SHA never marks a finding
    # stale downstream.
    _generated_head_sha: str | None = None
    if project_obj is not None:
        try:
            _generated_head_sha = await resolve_head_commit(**_src_ref_ctx)
        except Exception:  # noqa: BLE001 — provenance must never crash ingestion
            logger.debug(
                "[RESEARCH] head-commit provenance resolution failed", exc_info=True
            )
            _generated_head_sha = None

    # Validate optional structured fields: claims and sources.
    # Count findings whose cited code is absent on the integration HEAD (broken
    # file citation or ungrounded path:symbol claim). A finding that cites code
    # not present on HEAD definitionally does not reproduce on HEAD, so this
    # count is recorded on the task as the honest "rejected for does-not-exist-
    # on-HEAD" signal — separate from the surviving-finding success signal below.
    _broken_citation_findings = 0
    for _f in findings:
        if _generated_head_sha and not _f.get("generated_head_sha"):
            _f["generated_head_sha"] = _generated_head_sha
        if "claims" in _f and not isinstance(_f.get("claims"), list):
            _f["claims"] = []
        _broken_provenance: Dict[str, Any] | None = None
        if "sources" in _f:
            if not isinstance(_f["sources"], list):
                _f["sources"] = []
            else:
                _f["sources"] = [
                    s for s in _f["sources"] if isinstance(s, dict) and s.get("ref")
                ]
                # Validate file-type source refs at write time against the
                # project's real integration branch (resolved once above).
                _f["sources"] = await validate_source_refs(
                    _f["sources"], **_src_ref_ctx
                )
                # Attach a structured provenance summary when the validation
                # marks file refs invalid. The finding still spawns; the data
                # is here so downstream consumers (admin UI, agent prompts)
                # can flag research with bad citations as lower-trust.
                _broken_provenance = summarize_broken_refs(_f["sources"])
        # Symbol-level grounding: verify path:symbol references in claims (and
        # file source excerpts) name code that actually exists on the project's
        # real integration branch, threading the same GitHub verification
        # context the file-level check above uses (local git HEAD only as a
        # graceful fallback when credentials don't resolve). A claim
        # asserting facts about a function/class/constant that is absent from an
        # otherwise-existing file is surfaced the same way a broken file citation
        # is, and discounted from the actionability grade below.
        _ungrounded = await _summarize_ungrounded_claims_grounded(
            _f.get("claims"), _f.get("sources"), _src_ref_ctx
        )
        if _ungrounded is not None:
            _f["_ungrounded_claim_count"] = _ungrounded["ungrounded_claim_count"]
        # Merge file-citation, symbol grounding, and any already-known staleness
        # warnings into the provenance blob propagated to child task context.
        _merge_provenance_warnings(
            _f,
            _broken_provenance,
            _ungrounded,
            _staleness_provenance_warning(_f),
        )
        if _broken_provenance or _ungrounded:
            _broken_citation_findings += 1
        # Reproduce-on-HEAD verdict (the GATE input). A prose-claim finding whose
        # ONLY citations are gone — every cited file deleted AND every cited
        # path:symbol orphaned — does not reproduce on HEAD and must be rejected
        # at the spawn loop, not merely discounted. Reuse the broken-ref +
        # ungrounded-symbol signals already in hand (no new I/O); a partially
        # grounded finding (some citation still resolves) and an indeterminate
        # context (no backend) are NEVER stamped absent.
        _repro = classify_reproduction(
            present_file_count=_file_source_present_count(_f.get("sources")),
            broken_file_count=(_broken_provenance or {}).get(
                "broken_source_ref_count", 0
            ),
            total_symbol_refs=_count_total_symbol_refs(
                _f.get("claims"), _f.get("sources")
            ),
            ungrounded_symbol_count=len(
                (_ungrounded or {}).get("ungrounded_refs") or []
            ),
            has_backend=_has_backend,
        )
        # Store the FULL verdict (present/absent/indeterminate), not only the
        # absent stamp: the spawn gate below keys on ``== "absent"`` (unchanged),
        # but a spawned fix needs to carry present/indeterminate so its outcome
        # scoring can refuse bare-merge credit when the defect never reproduced.
        if _repro in (
            CITATION_STATE_PRESENT,
            CITATION_STATE_ABSENT,
            CITATION_STATE_INDETERMINATE,
        ):
            _f["_reproduce_on_head"] = _repro
        # Reconcile the self-reported verification_basis against the validation
        # just computed: a finding cannot honestly claim a verified-against-code
        # tier while its own citations are absent at HEAD. Reuses the broken-ref
        # and ungrounded-claim signals already in hand (no new I/O).
        _reconcile_verification_basis(_f, _broken_provenance, _ungrounded)

    # Normalize each finding's declared prerequisite / provides_capability and
    # softly flag any off-registry capability ref (advisory — never rejected; the
    # ref still resolves EXACTLY against any provider that advertises it). The
    # registry is the strategist-curated problem_model.capabilities vocabulary.
    _registry_names = registry_capability_names(
        getattr(project_obj, "problem_model", None)
    )
    # The agent-facing capability vocabulary (canonical name + status) surfaced
    # into read-only planning/knowledge child payloads below so producers and
    # consumers draw declared capabilities from the SAME shared namespace.
    _registry_vocab = annotate_capability_proof_status(
        registry_capability_vocabulary(getattr(project_obj, "problem_model", None)),
        getattr(project_obj, "capability_proof_records", None),
        registry_capability_evidence_refs_map(getattr(project_obj, "problem_model", None)),
    )
    _annotate_capability_declarations(findings, _registry_names)

    _structural_downgrades = _apply_structural_actionability_downgrade(findings)

    # Store all findings for admin visibility before filtering
    task.plan_output = findings
    task.context = {
        **(task.context or {}),
        "_actionability_structural_downgrades": _structural_downgrades,
        "research_broken_citation_rejected": _broken_citation_findings,
    }
    session.add(task)
    await session.commit()

    # Forward-quality ESCALATION gate (blast-radius-proportional). When enabled
    # and these findings would spawn downstream implementation work, do not
    # spawn anything yet: stand up an INDEPENDENT grader and park this task
    # EVALUATING. The grader's verdict returns via the re-entrant completion
    # path, which re-invokes this handler with the verdict in context (the
    # `_grader_verdict is None` guard below is what lets the resume pass fall
    # through to the spawn loop instead of escalating again). Findings that
    # would spawn nothing keep the cheap synchronous check and never escalate.
    if (
        _grader_verdict is None
        and _runtime_config.get("research_quality_grader_enabled", False)
        and _research_findings_would_spawn(findings)
    ):
        findings_json = await asyncio.to_thread(json.dumps, findings)
        grader = await spawn_forward_quality_grader(task, findings_json, session)
        if grader is not None:
            logger.info(
                "[RESEARCH] Task %s parked awaiting forward-quality grade "
                "(grader %s) before spawning implementation tasks",
                task.id,
                grader.id,
            )
            return

    # Separate direction proposals and notes from actionable findings — these are processed
    # separately and must not enter the hash-based deduplication loop (they never create tasks,
    # so their hashes would never be stored, allowing identical entries to accumulate).
    direction_proposals = [f for f in findings if f.get("type") == "direction_proposal"]
    direction_notes = [f for f in findings if f.get("type") == "direction_note"]
    findings = [
        f
        for f in findings
        if f.get("type") not in ("direction_proposal", "direction_note")
    ]
    # Recover findings that were deferred in the previous cycle due to pipeline degradation.
    # These were already researched but couldn't be spawned; prepend them so they go through
    # the full dedup + health gate pipeline alongside this cycle's new findings.
    prior_deferred = (task.context or {}).get("prior_deferred_findings", [])
    # Initialized unconditionally: the give-up marker is consulted far below
    # (when stamping task.context) on every code path, including the one where
    # there were no prior-deferred findings to decay.
    _finding_give_up: dict | None = None
    _low_actionability_decayed: list[dict] = []
    _low_actionability_refinement_deferred: list[dict] = []
    _salvage_candidates: list[dict] = []
    if prior_deferred:
        # Filter out non-actionable types that would have been handled separately above
        prior_actionable = [
            f
            for f in prior_deferred
            if isinstance(f, dict)
            and f.get("type") not in ("direction_proposal", "direction_note")
        ]
        # Discard findings older than the configured TTL to prevent stale
        # entries from blocking fresh findings (priority inversion) and to bound
        # the deferred list during extended pipeline degradation.
        _deferred_ttl_days = int(_runtime_config.get("deferred_finding_ttl_days", 7))
        _stale_cutoff = _utcnow() - timedelta(days=_deferred_ttl_days)
        _fresh_actionable: list[dict] = []
        _stale_discarded: list[dict] = []
        for f in prior_actionable:
            _stamp = f.get("originally_researched_at")
            _stamp_dt = None
            if isinstance(_stamp, str):
                try:
                    from datetime import datetime as _dt

                    _stamp_dt = _dt.fromisoformat(_stamp.replace("Z", "+00:00"))
                    if _stamp_dt.tzinfo is not None:
                        _stamp_dt = _stamp_dt.replace(tzinfo=None)
                except ValueError:
                    _stamp_dt = None
            if _stamp_dt is not None and _stamp_dt < _stale_cutoff:
                _stale_discarded.append(f)
            else:
                _fresh_actionable.append(f)
        if _stale_discarded:
            logger.warning(
                f"[RESEARCH] Discarding {len(_stale_discarded)} stale deferred finding(s) "
                f"(older than {_deferred_ttl_days}d) from task {task.id}: "
                f"titles={[f.get('title') or f.get('description', '')[:60] for f in _stale_discarded]}"
            )
        prior_actionable = _fresh_actionable
        # Decay: discard findings that have been deferred repeatedly for outcome-blind
        # reasons (ultra_low_confidence / degradation_gate / low_actionability /
        # prereq_deferred_same_cycle / adversarial_gate). Retrying them unchanged
        # wastes a findings slot each cycle. batch_cap deferrals are pure capacity
        # constraints and are not decayed.
        _decayed: list[dict] = []
        _kept: list[dict] = []
        for f in prior_actionable:
            _count = int(f.get("deferred_count", 0) or 0)
            _reason = f.get("deferred_reason")
            if (
                (_reason == "ultra_low_confidence" and _count >= 4)
                or (_reason == "degradation_gate" and _count >= 3)
            ) or (_reason == "adversarial_gate" and _count >= 3):
                _decayed.append(f)
            elif _reason == "low_actionability" and _count >= 3:
                _record = _consume_finding_challenge_record(
                    task,
                    f,
                    boundary=CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
                    first_verdict="LOW",
                )
                _challenge_enabled = _finding_challenge_enabled(
                    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY
                )
                if _challenge_enabled and _record is None:
                    _challenge = await spawn_finding_challenge(
                        task,
                        f,
                        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
                        "LOW",
                        session,
                        question=(
                            "Is this finding genuinely unactionable after repeated "
                            "deferral, or should it be refined and kept?"
                        ),
                        finding_hash=_finding_hash(
                            f.get("title", ""),
                            f.get("description") or f.get("prompt", ""),
                        ),
                    )
                    if _challenge is not None or (task.context or {}).get(
                        "awaiting_finding_challenge"
                    ):
                        logger.info(
                            "[RESEARCH] Task %s parked awaiting low-actionability "
                            "decay challenge for finding %r",
                            task.id,
                            f.get("title") or f.get("description", "")[:60],
                        )
                        return
                if _challenge_enabled:
                    _disagrees_with_rationale = _challenge_disagrees(_record) and bool(
                        ((_record or {}).get("reason") or "").strip()
                    )
                    if _disagrees_with_rationale:
                        f["low_actionability_refinement_reason"] = str(
                            (_record or {}).get("reason")
                        )[:500]
                        _low_actionability_refinement_deferred.append(f)
                    else:
                        _low_actionability_decayed.append(f)
                        _decayed.append(f)
                else:
                    _low_actionability_decayed.append(f)
                    _decayed.append(f)
            elif _reason == "prereq_deferred_same_cycle" and _count >= 3:
                # Co-deferred dependent whose foundation never re-surfaced in time:
                # give up after 3 cycles (its deferred foundation has typically
                # decayed by then too) rather than retrying the coupled pair forever.
                _decayed.append(f)
            else:
                _kept.append(f)
        if _decayed:
            logger.warning(
                f"[RESEARCH] Discarding {len(_decayed)} repeatedly-deferred finding(s) "
                f"from task {task.id}: "
                f"titles={[f.get('title') or f.get('description', '')[:60] for f in _decayed]}"
            )
            _by_reason: dict[str, int] = {}
            _by_direction: dict[str, int] = {}
            for f in _decayed:
                _reason_key = f.get("deferred_reason") or "unknown"
                _by_reason[_reason_key] = _by_reason.get(_reason_key, 0) + 1
                _direction_key = (
                    f.get("direction_id")
                    or (task.context or {}).get("direction_id")
                    or "(none)"
                )
                _by_direction[_direction_key] = _by_direction.get(_direction_key, 0) + 1
            _finding_give_up = {
                "at": _utcnow().isoformat(),
                "total": len(_decayed),
                "by_reason": _by_reason,
                "by_direction": _by_direction,
                "sample": [
                    {
                        "title": f.get("title") or f.get("description", "")[:60],
                        "reason": f.get("deferred_reason") or "unknown",
                        "direction_id": f.get("direction_id")
                        or (task.context or {}).get("direction_id"),
                    }
                    for f in _decayed[:5]
                ],
            }
            # Carry the grader non-response cause onto the give-up record when any
            # adversarial-gate finding was decayed, so a structural give-up driven
            # by a non-responsive grader is attributable. Observability only.
            _give_up_cause = (
                (task.context or {}).get("adversarial_gate_diagnostic") or {}
            ).get("cause")
            if _give_up_cause and any(
                f.get("deferred_reason") == "adversarial_gate" for f in _decayed
            ):
                _finding_give_up["adversarial_gate_cause"] = _give_up_cause
            _salvage_candidates.extend(_decayed)
        # Include TTL-expired deferred findings in the salvage bundle so they
        # are durably captured instead of only logged and silently dropped.
        # This runs unconditionally: stale discarded findings exist independently
        # of whether any repeatedly deferred findings decayed this cycle.
        _salvage_candidates.extend(_stale_discarded)
        prior_actionable = _kept
        # Stale-targeting: a deferred finding carries the integration HEAD it was
        # generated against. On re-dispatch, ask whether any cited file changed
        # between that commit and the current branch tip; if so, the finding may
        # no longer reproduce — flag it for the reproduce-on-HEAD gate rather than
        # trusting it as fresh. Conservative: an unknown SHA or an uncomputable
        # diff (no creds + no checkout, API error) never raises the flag.
        for f in prior_actionable:
            _gen_sha = f.get("generated_head_sha")
            if _gen_sha:
                try:
                    _changed = await cited_paths_changed_since(
                        _gen_sha, finding_cited_paths(f), **_src_ref_ctx
                    )
                except Exception:  # noqa: BLE001 — re-validation must never crash recovery
                    _changed = None
                if _changed is True:
                    f["needs_revalidation"] = True
                    f["needs_revalidation_reason"] = (
                        "cited_files_changed_since_generation"
                    )
                _merge_provenance_warnings(f, _staleness_provenance_warning(f))
            # Re-ground cited FILES and SYMBOLS against the CURRENT HEAD, the same
            # way fresh ingestion does. The ingestion-time verdict was computed
            # against an older branch tip; a finding deferred while its symbol
            # existed, then orphaned when that symbol was deleted, would otherwise
            # come back and spawn as if fresh (the old path re-checked files but
            # never re-grounded symbols). The cited_paths_changed_since flag above
            # is the softer staleness warning; this is the reproduce-on-HEAD GATE
            # verdict. Conservative throughout: any error or indeterminate result
            # leaves the finding unstamped.
            try:
                _re_sources = await validate_source_refs(
                    f.get("sources") or [], **_src_ref_ctx
                )
            except Exception:  # noqa: BLE001 — recovery re-validation must never crash
                _re_sources = f.get("sources") or []
            _re_broken = summarize_broken_refs(_re_sources)
            try:
                _re_ungrounded = await _summarize_ungrounded_claims_grounded(
                    f.get("claims"), f.get("sources"), _src_ref_ctx
                )
            except Exception:  # noqa: BLE001 — recovery re-grounding must never crash
                _re_ungrounded = None
            _re_repro = classify_reproduction(
                present_file_count=_file_source_present_count(_re_sources),
                broken_file_count=(_re_broken or {}).get("broken_source_ref_count", 0),
                total_symbol_refs=_count_total_symbol_refs(
                    f.get("claims"), f.get("sources")
                ),
                ungrounded_symbol_count=len(
                    (_re_ungrounded or {}).get("ungrounded_refs") or []
                ),
                has_backend=_has_backend,
            )
            if _re_repro in (
                CITATION_STATE_PRESENT,
                CITATION_STATE_ABSENT,
                CITATION_STATE_INDETERMINATE,
            ):
                f["_reproduce_on_head"] = _re_repro
        if prior_actionable:
            logger.info(
                f"[RESEARCH] Prepending {len(prior_actionable)} deferred finding(s) from previous cycle "
                f"to findings list for task {task.id}"
            )
            # Rebase fresh findings' intra-batch depends_on_indices before the
            # prepend. The agent emits 0-based indices into its own (fresh-only)
            # findings array, so each index must be shifted up by the number of
            # recovered findings that will occupy the front of the combined list.
            _n_prepended = len(prior_actionable)
            for _fresh_f in findings:
                _deps = _fresh_f.get("depends_on_indices")
                if _deps:
                    _fresh_f["depends_on_indices"] = [j + _n_prepended for j in _deps]
            findings = prior_actionable + findings

    # Adversarial spawn-gate (DEFAULT ON). When enabled and no adversarial
    # verdict has been written yet, select the high-stakes findings and spawn a
    # refute-first per-finding adversarial challenger. The source parks
    # EVALUATING until the challenger returns, then re-enters this handler with
    # ADVERSARIAL_GATE_VERDICT_KEY in context. On re-entry the per-finding
    # verdicts are stamped onto findings below, and the spawn loop defers any
    # finding marked failed (not dropped — deferred to the next cycle).
    # FAIL-CLOSED: a crashed or absent grader leaves high-stakes findings with
    # passed=False so they are deferred rather than silently spawned. Low-stakes findings
    # keep the cheap synchronous path and are never escalated.
    _spawn_gate_challenged_count = 0
    _spawn_gate_refuted_count = 0
    _spawn_gate_survived_count = 0
    _spawn_gate_findings: list[dict] = []
    if _adversarial_verdict_raw is None and _runtime_config.get(
        "adversarial_spawn_gate_enabled", True
    ):
        _high_stakes = [f for f in findings if _is_high_stakes_finding(f)]
        if _high_stakes:
            # Build a compact, budget-bounded manifest for the adversarial grader.
            # Each finding gets a stable hash identifier; the manifest stays valid
            # JSON under the per-prompt char budget regardless of finding count or size.
            from app.finding_deduplication import finding_hash as _fh

            _hs_with_hash = []
            for _f in _high_stakes:
                _h = _fh(
                    str(_f.get("title", "")),
                    _canonical_finding_body(_f),
                )
                _hs_with_hash.append({**_f, "hash": _h})
            _manifest_items, _manifest_json = build_adversarial_challenge_manifest(
                _hs_with_hash
            )
            _challenged_hashes = [item["hash"] for item in _manifest_items]
            adv_grader = await spawn_forward_quality_grader(
                task,
                _manifest_json,
                session,
                adversarial_mode=True,
                finding_hashes=_challenged_hashes,
            )
            if adv_grader is not None:
                # Persist the challenged hashes before parking so
                # _resolve_quality_grader_completion can build all_hashes
                # from these — which include prior_deferred findings prepended
                # after plan_output was written — rather than from plan_output
                # alone. Without this, verdicts for recovered deferred findings
                # are ignored (unknown hash) and those findings get a fail-closed unresolved verdict.
                _ctx = dict(task.context or {})
                _ctx["_adversarial_challenged_hashes"] = _challenged_hashes
                task.context = _ctx
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
                logger.info(
                    "[RESEARCH] Task %s parked awaiting adversarial challenge "
                    "(grader %s, %d high-stakes finding(s), %d in manifest)",
                    task.id,
                    adv_grader.id,
                    len(_high_stakes),
                    len(_manifest_items),
                )
                return
            else:
                # Grader spawn returned None (single-shot guard hit / grader already in flight).
                # Mark as awaiting and return to prevent ungated spawn of high-stakes findings.
                # On re-entry the guard will detect this marker and no-op until the verdict arrives.
                task.status = TaskStatus.EVALUATING
                task.latest_status_message = (
                    "Awaiting adversarial challenge verdict before spawning "
                    "high-stakes implementation tasks"
                )
                _ctx = dict(task.context or {})
                _ctx["awaiting_adversarial_grader"] = True
                _ctx["_adversarial_challenged_hashes"] = _challenged_hashes
                task.context = _ctx
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
                logger.info(
                    "[RESEARCH] Task %s parked awaiting adversarial challenge "
                    "(spawn returned None — grader in flight or single-shot guard hit, "
                    "%d high-stakes finding(s))",
                    task.id,
                    len(_high_stakes),
                )
                return

    # Adversarial re-entry: stamp per-finding verdicts so the spawn loop can
    # skip challenged-and-failed findings. The gate is fail-closed: any finding
    # whose hash is absent from the verdict dict defaults to passed=False (deferred).
    # Decision-quality breakdown of the inclusive survived/refuted buckets.
    _spawn_gate_explicit_pass_count = 0
    _spawn_gate_fail_open_count = 0
    _spawn_gate_infra_fail_open_count = 0
    _spawn_gate_unresolved_count = 0
    _spawn_gate_refuted_reasons: list[str] = []
    _spawn_gate_manifest_excluded_count = 0
    _spawn_gate_manifest_excluded_samples: list[dict] = []
    _spawn_gate_by_direction: dict[str, dict] = {}
    _spawn_gate_parse_outcome = None
    _spawn_gate_healthy_cycle = False
    if _adversarial_verdict_raw is not None:
        _spawn_gate_parse_outcome = _adversarial_verdict_raw.get("parse_outcome")
        # Machine-readable non-response cause from the resolution-time diagnostic
        # (or the gate record). Stamped onto each unresolved finding below so the
        # deferred record is attributable. Observability only.
        _spawn_gate_cause = _adversarial_verdict_raw.get("cause") or (
            (task.context or {}).get("adversarial_gate_diagnostic") or {}
        ).get("cause")
        _adv_verdicts: dict[str, dict] = (
            _adversarial_verdict_raw.get("finding_verdicts") or {}
        )
        from app.finding_deduplication import finding_hash as _fh

        # Hashes that were actually in the challenge manifest. Used to distinguish
        # "challenged but no verdict returned" (fail-closed) from "never challenged
        # because low-stakes" (should pass through the gate unaffected).
        _challenged_hashes_ctx: list[str] = (task.context or {}).get(
            "_adversarial_challenged_hashes"
        ) or []
        _challenged_hashes_set: set[str] = set(_challenged_hashes_ctx)
        _explicit_verdicts = [
            v
            for v in _adv_verdicts.values()
            if isinstance(v, dict) and v.get("explicit")
        ]
        _wholesale_parse_outcome = _spawn_gate_parse_outcome
        if (
            _wholesale_parse_outcome is None
            and _challenged_hashes_set
            and not _adv_verdicts
        ):
            # Legacy gate records predate parse_outcome; an empty verdict map for
            # a non-empty manifest is equivalent to the parser finding no matches.
            _wholesale_parse_outcome = ADVERSARIAL_PARSE_NO_HASH_MATCHES
        _wholesale_non_response = (
            bool(_challenged_hashes_set)
            and not _explicit_verdicts
            and _wholesale_parse_outcome in ADVERSARIAL_WHOLESALE_NON_RESPONSE_OUTCOMES
        )
        _spawn_gate_healthy_cycle = not _wholesale_non_response
        _spawn_gate_cause_category = (
            _spawn_gate_cause.get("category")
            if isinstance(_spawn_gate_cause, dict)
            else None
        )
        _infra_non_response_cause = (
            _spawn_gate_cause_category in GRADER_TIMEOUT_CAUSES
            or _spawn_gate_cause_category in GRADER_TRANSIENT_INFRA_CAUSES
            or _wholesale_parse_outcome == ADVERSARIAL_PARSE_CHALLENGER_FAILED
        )

        def _spawn_gate_dir_entry(_direction_id: str) -> dict:
            return _spawn_gate_by_direction.setdefault(
                _direction_id,
                {
                    "challenged_count": 0,
                    "refuted_count": 0,
                    "survived_count": 0,
                    "explicit_pass_count": 0,
                    "fail_open_count": 0,
                    "unresolved_count": 0,
                    "wholesale_retry_exhausted": bool(
                        _wholesale_non_response
                        and (task.context or {}).get(
                            "research_spawn_gate_wholesale_retry_exhausted"
                        )
                    ),
                },
            )

        if _wholesale_non_response:
            # Dispatch on the diagnosed non-response cause so the retry
            # strategy matches the failure mode rather than always doing the
            # same wholesale re-challenge.
            _cause_category = _spawn_gate_cause_category or ""
            # _do_wholesale: True means fall through to the existing wholesale
            # retry logic.  The refusal path sets it False to skip retries.
            _do_wholesale = True

            if _cause_category in GRADER_TIMEOUT_CAUSES:
                # The grader ran out of time.  Retry with a reduced manifest
                # (half the challenged findings) so the next grader is less
                # likely to time out.  Findings excluded from the reduced set
                # fail closed via the existing manifest-exclusion path.
                _targeted_count = int(
                    (task.context or {}).get(_ADVERSARIAL_TARGETED_RETRY_KEY) or 0
                )
                if _targeted_count < _ADVERSARIAL_TARGETED_RETRY_LIMIT:
                    _half = max(1, len(_challenged_hashes_set) // 2)
                    _retry_findings = []
                    for _f in findings:
                        _fhash = _fh(
                            str(_f.get("title", "")),
                            _canonical_finding_body(_f),
                        )
                        if (
                            _fhash in _challenged_hashes_set
                            and len(_retry_findings) < _half
                        ):
                            _retry_findings.append({**_f, "hash": _fhash})
                    _manifest_items, _manifest_json = (
                        build_adversarial_challenge_manifest(_retry_findings)
                    )
                    _retry_hashes = [
                        item["hash"]
                        for item in _manifest_items
                        if item.get("hash") in _challenged_hashes_set
                    ]
                    if _retry_hashes:
                        _ctx = dict(task.context or {})
                        _ctx.pop(ADVERSARIAL_GATE_VERDICT_KEY, None)
                        _ctx[_ADVERSARIAL_TARGETED_RETRY_KEY] = _targeted_count + 1
                        # Preserve the original full hash list before narrowing to the
                        # reduced targeted-retry manifest. The wholesale fallback reads
                        # this key so a second timeout still challenges all findings
                        # rather than only the already-reduced subset.
                        if not _ctx.get("_adversarial_original_challenged_hashes"):
                            _ctx["_adversarial_original_challenged_hashes"] = list(
                                _challenged_hashes_set
                            )
                        _ctx["_adversarial_challenged_hashes"] = _retry_hashes
                        _ctx["research_spawn_gate_last_wholesale_parse_outcome"] = (
                            _wholesale_parse_outcome
                        )
                        _ctx["research_spawn_gate_challenged_count"] = len(
                            _retry_hashes
                        )
                        _ctx["research_spawn_gate_refuted_count"] = 0
                        _ctx["research_spawn_gate_survived_count"] = 0
                        _ctx["research_spawn_gate_explicit_pass_count"] = 0
                        _ctx["research_spawn_gate_fail_open_count"] = 0
                        _ctx["research_spawn_gate_unresolved_count"] = len(
                            _retry_hashes
                        )
                        _ctx["research_spawn_gate_parse_outcome"] = (
                            _wholesale_parse_outcome
                        )
                        task.context = _ctx
                        flag_modified(task, "context")
                        session.add(task)
                        await session.flush()
                        adv_grader = await spawn_forward_quality_grader(
                            task,
                            _manifest_json,
                            session,
                            adversarial_mode=True,
                            finding_hashes=_retry_hashes,
                        )
                        if adv_grader is not None:
                            logger.warning(
                                "[RESEARCH] Task %s deferred timeout adversarial "
                                "non-response (%s) and spawned targeted retry "
                                "grader %s (%d/%d findings in reduced manifest)",
                                task.id,
                                _wholesale_parse_outcome,
                                adv_grader.id,
                                len(_retry_hashes),
                                len(_challenged_hashes_set),
                            )
                            return
                        # Single-shot guard blocked spawn; fall through to wholesale.
                # Targeted retry exhausted or spawn blocked: fall through to
                # wholesale retry so the cycle still gets one more chance with
                # the full manifest before deferring everything fail-closed.

            elif _cause_category in GRADER_TRANSIENT_INFRA_CAUSES:
                # Transient-infra failure (rate-limit, provisioning, etc.):
                # fall through to wholesale retry with _do_wholesale=True.
                pass

            elif _cause_category == GRADER_CAUSE_REFUSAL:
                # The grader completed but explicitly refused to answer.
                # Re-asking an identical prompt predictably refuses again;
                # skip all retries and defer fail-closed immediately so the
                # cycle does not burn extra budget on a known-futile re-ask.
                logger.warning(
                    "[RESEARCH] Task %s adversarial grader refused (parse=%s) — "
                    "skipping retries, all challenged findings deferred fail-closed",
                    task.id,
                    _wholesale_parse_outcome,
                )
                _ctx = dict(task.context or {})
                _ctx["research_spawn_gate_wholesale_retry_exhausted"] = True
                _ctx["research_spawn_gate_refusal_skip_retry"] = True
                _ctx["research_spawn_gate_last_wholesale_parse_outcome"] = (
                    _wholesale_parse_outcome
                )
                task.context = _ctx
                flag_modified(task, "context")
                _do_wholesale = False

            if _do_wholesale:
                # Transient-infra cause (rate-limit, provisioning, reassignment,
                # etc.), format miss, unknown, or timeout after targeted retry
                # exhausted: use the existing wholesale retry (same manifest,
                # fresh grader), counted against the wholesale budget.
                _retry_count = int(
                    (task.context or {}).get(_ADVERSARIAL_WHOLESALE_RETRY_KEY) or 0
                )
                if _retry_count < _ADVERSARIAL_WHOLESALE_RETRY_LIMIT:
                    # If the targeted retry narrowed _adversarial_challenged_hashes to
                    # a subset, _challenged_hashes_set reflects only that subset here.
                    # Recover the original full list from the preservation key written
                    # at targeted-retry spawn time so the wholesale challenge covers all
                    # originally-challenged findings, not just the reduced half.
                    _wholesale_original = (task.context or {}).get(
                        "_adversarial_original_challenged_hashes"
                    )
                    _wholesale_hashes_set = (
                        set(_wholesale_original)
                        if _wholesale_original
                        else _challenged_hashes_set
                    )
                    _retry_findings = []
                    for _f in findings:
                        _fhash = _fh(
                            str(_f.get("title", "")),
                            _canonical_finding_body(_f),
                        )
                        if _fhash in _wholesale_hashes_set:
                            _retry_findings.append({**_f, "hash": _fhash})
                    _manifest_items, _manifest_json = (
                        build_adversarial_challenge_manifest(_retry_findings)
                    )
                    _retry_hashes = [
                        item["hash"]
                        for item in _manifest_items
                        if item.get("hash") in _wholesale_hashes_set
                    ]
                    if _retry_hashes:
                        _ctx = dict(task.context or {})
                        _ctx.pop(ADVERSARIAL_GATE_VERDICT_KEY, None)
                        _ctx.pop("_adversarial_original_challenged_hashes", None)
                        _ctx[_ADVERSARIAL_WHOLESALE_RETRY_KEY] = _retry_count + 1
                        _ctx["_adversarial_challenged_hashes"] = _retry_hashes
                        _ctx["research_spawn_gate_last_wholesale_parse_outcome"] = (
                            _wholesale_parse_outcome
                        )
                        _ctx["research_spawn_gate_challenged_count"] = len(
                            _retry_hashes
                        )
                        _ctx["research_spawn_gate_refuted_count"] = 0
                        _ctx["research_spawn_gate_survived_count"] = 0
                        _ctx["research_spawn_gate_explicit_pass_count"] = 0
                        _ctx["research_spawn_gate_fail_open_count"] = 0
                        _ctx["research_spawn_gate_unresolved_count"] = len(
                            _retry_hashes
                        )
                        _ctx["research_spawn_gate_parse_outcome"] = (
                            _wholesale_parse_outcome
                        )
                        task.context = _ctx
                        flag_modified(task, "context")
                        session.add(task)
                        await session.flush()
                        adv_grader = await spawn_forward_quality_grader(
                            task,
                            _manifest_json,
                            session,
                            adversarial_mode=True,
                            finding_hashes=_retry_hashes,
                        )
                        if adv_grader is not None:
                            logger.warning(
                                "[RESEARCH] Task %s deferred wholesale adversarial "
                                "non-response (%s) and spawned bounded retry grader %s",
                                task.id,
                                _wholesale_parse_outcome,
                                adv_grader.id,
                            )
                            return
                        # If the single-shot guard refuses a retry, continue into the
                        # fail-closed path; high-stakes work still must not spawn.
                else:
                    _ctx = dict(task.context or {})
                    _ctx["research_spawn_gate_wholesale_retry_exhausted"] = True
                    _ctx["research_spawn_gate_last_wholesale_parse_outcome"] = (
                        _wholesale_parse_outcome
                    )
                    task.context = _ctx
                    flag_modified(task, "context")

        _infra_fail_open_unverified = bool(
            _wholesale_non_response
            and _infra_non_response_cause
            and (task.context or {}).get("research_spawn_gate_wholesale_retry_exhausted")
            and str(
                _runtime_config.get(_ADVERSARIAL_INFRA_FAILURE_MODE_KEY, "observe")
                or "observe"
            )
            .strip()
            .lower()
            != _ADVERSARIAL_INFRA_FAILURE_MODE_ENFORCE
        )
        _infra_fail_open_reason = (
            "unverified: adversarial challenger infrastructure failed after "
            "retry exhaustion"
        )

        for _f in findings:
            _fhash = _fh(
                str(_f.get("title", "")),
                _canonical_finding_body(_f),
            )
            _verdict_obj = _adv_verdicts.get(_fhash)
            _was_challenged = (
                _fhash in _challenged_hashes_set
                if _challenged_hashes_set
                else _is_high_stakes_finding(_f)
            )
            if _verdict_obj is None:
                # Determine whether this finding was actually challenged.
                # When _adversarial_challenged_hashes is stored, use it; otherwise
                # fall back to high-stakes check (legacy / missing context).
                if not _was_challenged:
                    if _is_high_stakes_finding(_f):
                        # high-stakes findings must never bypass the gate
                        # just because the bounded manifest could not admit them.
                        _passed = False
                        _reason = (
                            "fail-closed: high-stakes finding was not admitted "
                            "to the adversarial manifest"
                        )
                        _explicit = False
                        _spawn_gate_manifest_excluded_count += 1
                        if (
                            len(_spawn_gate_manifest_excluded_samples)
                            < _SPAWN_GATE_REASONS_SAMPLE_CAP
                        ):
                            _spawn_gate_manifest_excluded_samples.append(
                                {
                                    "hash": _fhash,
                                    "title": str(_f.get("title") or "")[:120],
                                    "reason": _reason[:_SPAWN_GATE_REASON_MAX_LEN],
                                }
                            )
                    else:
                        # Finding was never in the challenge manifest (e.g. low-stakes).
                        # Do not gate it — no _adversarial_verdict stamp means it passes through.
                        continue
                else:
                    # Hash in manifest but absent from grader output: fail-closed.
                    _passed = False
                    _reason = (
                        _wholesale_parse_outcome
                        if _wholesale_non_response and _wholesale_parse_outcome
                        else "fail-closed: no verdict returned for this finding"
                    )
                    _explicit = False
            elif isinstance(_verdict_obj, bool):
                # Normalise legacy bool verdicts so old stored context still works.
                _passed = _verdict_obj
                _reason = ""
                _explicit = True
            else:
                # Validate "passed" is actually a boolean; malformed/missing -> fail-closed
                passed_value = _verdict_obj.get("passed")
                _passed = passed_value if isinstance(passed_value, bool) else False
                _reason = str(_verdict_obj.get("reason") or "")
                # Validate "explicit" is actually a boolean; malformed/missing -> implicit
                explicit_value = _verdict_obj.get("explicit")
                _explicit = (
                    explicit_value if isinstance(explicit_value, bool) else False
                )
            if (
                _infra_fail_open_unverified
                and _was_challenged
                and not _explicit
            ):
                _passed = True
                _reason = _infra_fail_open_reason
                _f["_adversarial_unverified"] = True
                _f["_adversarial_unverified_reason"] = _reason
                _f["_adversarial_unverified_parse_outcome"] = _wholesale_parse_outcome
                if _spawn_gate_cause:
                    _f["_adversarial_unverified_cause"] = _spawn_gate_cause
            if not _passed:
                _f["_adversarial_verdict"] = "fail"
                if not _explicit:
                    _unresolved_reason = (
                        _verdict_obj.get("unresolved_reason")
                        if isinstance(_verdict_obj, dict)
                        else None
                    )
                    _reason = str(
                        _unresolved_reason
                        or _reason
                        or "fail-closed: no verdict returned for this finding"
                    )
                    # Attribute the unresolved finding to the grader non-response
                    # cause so the deferred record carries WHY, not just WHAT.
                    if _spawn_gate_cause:
                        _f["_adversarial_verdict_cause"] = _spawn_gate_cause
            _f["_adversarial_verdict_reason"] = _reason
            _f["_adversarial_verdict_explicit"] = _explicit
        # Bound counters to findings that were actually in the challenge manifest.
        # Without this, manifest-excluded high-stakes findings (budget-capped) inflate
        # the refuted bucket while missing from the challenged count, causing
        # survived = challenged - refuted to go negative.
        if _challenged_hashes_set:
            _high_stakes_challenged = [
                _f
                for _f in findings
                if _fh(
                    str(_f.get("title", "")),
                    _canonical_finding_body(_f),
                )
                in _challenged_hashes_set
            ]
        else:
            _high_stakes_challenged = [
                _f for _f in findings if _is_high_stakes_finding(_f)
            ]
        _spawn_gate_challenged_count = len(_challenged_hashes_ctx) or len(
            _high_stakes_challenged
        )
        _spawn_gate_refuted_count = sum(
            1
            for _f in _high_stakes_challenged
            if _f.get("_adversarial_verdict") == "fail"
        )
        _spawn_gate_survived_count = (
            _spawn_gate_challenged_count - _spawn_gate_refuted_count
        )
        # Break the inclusive survived/refuted buckets into decision-quality
        # outcomes using the per-finding verdict + explicit flag stamped above:
        #   passed (verdict != "fail") + explicit  -> explicit challenger pass
        #   passed                     + !explicit -> fail-open / defaulted-open pass
        #   !passed                    + !explicit -> unresolved / no verdict returned
        #   !passed                    + explicit  -> explicit refutation (refuted only)
        # so explicit_pass + fail_open == survived and
        # unresolved + explicit-refutation == refuted.
        _seen_reasons: set[str] = set()
        for _f in _high_stakes_challenged:
            _passed = _f.get("_adversarial_verdict") != "fail"
            _explicit = bool(_f.get("_adversarial_verdict_explicit"))
            _direction_id = str(
                _f.get("direction_id") or (task.context or {}).get("direction_id") or ""
            )
            if _direction_id:
                _dir_entry = _spawn_gate_dir_entry(_direction_id)
                _dir_entry["challenged_count"] += 1
                if _spawn_gate_parse_outcome:
                    _dir_entry["parse_outcome"] = _spawn_gate_parse_outcome
                if _spawn_gate_cause:
                    _dir_entry["cause"] = _spawn_gate_cause
            if _passed and _explicit:
                _spawn_gate_explicit_pass_count += 1
                if _direction_id:
                    _dir_entry["explicit_pass_count"] += 1
                    _dir_entry["survived_count"] += 1
            elif _passed and not _explicit:
                _spawn_gate_fail_open_count += 1
                if _f.get("_adversarial_unverified"):
                    _spawn_gate_infra_fail_open_count += 1
                if _direction_id:
                    _dir_entry["fail_open_count"] += 1
                    _dir_entry["survived_count"] += 1
                    if _f.get("_adversarial_unverified"):
                        _dir_entry["infra_fail_open_count"] = (
                            int(_dir_entry.get("infra_fail_open_count") or 0) + 1
                        )
            elif not _passed and not _explicit:
                _spawn_gate_unresolved_count += 1
                if _direction_id:
                    _dir_entry["unresolved_count"] += 1
                    _dir_entry["refuted_count"] += 1
                    if _spawn_gate_cause:
                        _cause_category = (
                            _spawn_gate_cause.get("category")
                            if isinstance(_spawn_gate_cause, dict)
                            else None
                        )
                        if _cause_category:
                            _cause_counts = _dir_entry.setdefault("cause_counts", {})
                            _cause_counts[_cause_category] = (
                                int(_cause_counts.get(_cause_category) or 0) + 1
                            )
            elif not _passed:
                if _direction_id:
                    _dir_entry["refuted_count"] += 1
            if not _passed:
                _reason = str(_f.get("_adversarial_verdict_reason") or "").strip()[
                    :_SPAWN_GATE_REASON_MAX_LEN
                ]
                if _direction_id and _reason:
                    _dir_reasons = _dir_entry.setdefault("refuted_reasons", [])
                    if (
                        _reason not in _dir_reasons
                        and len(_dir_reasons) < _SPAWN_GATE_REASONS_SAMPLE_CAP
                    ):
                        _dir_reasons.append(_reason)
                if (
                    _reason
                    and _reason not in _seen_reasons
                    and len(_spawn_gate_refuted_reasons)
                    < _SPAWN_GATE_REASONS_SAMPLE_CAP
                ):
                    _seen_reasons.add(_reason)
                    _spawn_gate_refuted_reasons.append(_reason)

        if _spawn_gate_manifest_excluded_samples:
            _finding_direction_by_hash = {
                _fh(
                    str(_f.get("title", "")),
                    _canonical_finding_body(_f),
                ): str(
                    _f.get("direction_id")
                    or (task.context or {}).get("direction_id")
                    or ""
                )
                for _f in findings
            }
            for _sample in _spawn_gate_manifest_excluded_samples:
                _sample_direction_id = _finding_direction_by_hash.get(
                    _sample.get("hash")
                )
                if not _sample_direction_id:
                    continue
                _dir_entry = _spawn_gate_dir_entry(_sample_direction_id)
                _dir_entry["manifest_excluded_count"] = (
                    int(_dir_entry.get("manifest_excluded_count") or 0) + 1
                )
                _dir_samples = _dir_entry.setdefault("manifest_excluded_samples", [])
                if len(_dir_samples) < _SPAWN_GATE_REASONS_SAMPLE_CAP:
                    _dir_samples.append(_sample)

    # Per-cycle finding ceiling. Capacity is NOT throttled here — see
    # _MAX_FINDINGS_PER_CYCLE: the dispatcher gates real VM starts on free slots
    # and _assess_project_spawn_health (below) clamps to _DEGRADED_SPAWN_CAP on a
    # genuinely degraded fleet. The previous slots*2 / accounts*3 / 15-queued
    # math throttled queue admission on instantaneous (fleet-global) dispatch
    # capacity the dispatcher already owns, suppressing healthy research yield and
    # pushing findings into lossy deferral. Spawned children are cheap QUEUED rows.
    _effective_cap = _MAX_FINDINGS_PER_CYCLE
    # Select the top _effective_cap findings by priority so high-priority
    # findings survive capacity-limited cycles regardless of the agent's
    # output ordering. We preserve agent order among survivors and remap
    # each surviving finding's depends_on_indices to the compacted positions
    # so _validate_plan_dependencies doesn't see spurious self-cycles and
    # UUID resolution wires deps to the correct child tasks.

    # Cap findings to prevent runaway task creation and backlog build-up.
    # Excess findings are deferred to the next cycle (via task.context['deferred_findings'])
    # rather than discarded, so capacity-constrained cycles don't permanently lose work.
    # Offloaded to thread via asyncio.to_thread since the sort + fixpoint loop is CPU-bound.
    (
        findings,
        batch_cap_applied_count,
        _batch_cap_deferred,
    ) = await asyncio.to_thread(_cap_findings_by_priority, findings, _effective_cap)
    if _batch_cap_deferred:
        logger.info(
            "[RESEARCH] Batch cap (%d) exceeded — deferring %d lower-priority finding(s) to next cycle",
            _effective_cap,
            len(_batch_cap_deferred),
        )

    original_base = (task.first_payload).get(
        "base_branch"
    ) or _get_project_integration_branch(project_obj)
    short_id = str(task.id).replace("-", "")[:8]
    research_branch_prefix = getattr(project_obj, "research_branch_prefix", "research/")
    # When this task is a requeue-clone that already opened a rollup PR, carry
    # the original staging branch forward instead of deriving a new
    # research/<id> branch from the clone's task ID.  Reusing the branch keeps
    # the merge-guard invariant (reviewed-branch == PR-head) intact: the PR
    # head still points at the original branch, so the merge guard won't abort
    # with a branch-mismatch error on every subsequent iteration.
    # Guard: only reuse when BOTH research_branch AND rollup_pr_number are
    # present — rollup_pr_number confirms a PR was actually opened (not just a
    # branch-setup step that failed before the PR was created).
    _rctx = task.context or {}
    _inherited_research_branch = _rctx.get("research_branch")
    _has_rollup_pr = bool(_rctx.get("rollup_pr_number"))
    _adopted_foreign_branch = False
    if _inherited_research_branch and _has_rollup_pr:
        research_branch = _inherited_research_branch
        # This requeue-clone adopts an ancestor-named (foreign) staging branch
        # to keep the merge-guard invariant intact. Stamp a DURABLE ownership
        # flag so the rollup OWNER-gate lets this task carry the branch to base
        # even though the name doesn't match its own id-prefix (invariant #2).
        _adopted_foreign_branch = True
    else:
        research_branch = f"{research_branch_prefix}{short_id}"

    # Stamp spawn-pending before _ensure_branch commits its row-lock release
    # (task_completion.py ~line 1260). If spawn is interrupted mid-flight by a
    # lock timeout or network error, this marker is already durable and tells the
    # sweep retry to resume the spawn from task.plan_output instead of discarding
    # the findings and re-running the agent.
    _ctx_sp = dict(task.context or {})
    _ctx_sp["_research_spawn_pending"] = True
    if _adopted_foreign_branch:
        _ctx_sp["owns_rollup_branch"] = True
    task.context = _ctx_sp
    flag_modified(task, "context")

    # Create staging branch for this research batch.
    # Round 4 W1 (audit C2): _ensure_branch now returns Task | None.
    # On success, returns the freshly-locked Task (caller mutates under
    # the new lock). On failure or peer-mutation defer, returns None.
    from app.task_completion import _ensure_branch

    relocked = await _ensure_branch(
        task, research_branch, original_base, "RESEARCH", session
    )
    if relocked is None:
        # Research is one-shot: there is no "next sweep" that re-runs the
        # handler on this task, so the comment-promised recovery in
        # _ensure_branch never fires. Shelve the findings into
        # prior_deferred_findings (in a fresh session — see helper) so the
        # next research cycle on this direction can recover them. The
        # failure path (task FAILED) is loud already; this covers the
        # peer-mutation defer path that used to drop findings silently.
        await _shelve_findings_for_next_cycle(
            task.id, findings, "branch_creation_defer"
        )
        return
    task = relocked

    # Validate dependencies if present. On a cycle, break it MINIMALLY — drop
    # only the edges that close a cycle (mirroring the plan path) rather than
    # wholesale-discarding every declared edge, so valid prerequisite chains
    # elsewhere in the batch keep gating their dependents.
    if not _validate_plan_dependencies(findings):
        _dropped_cycle_edges = _break_dependency_cycles(findings, "RESEARCH")
        logger.warning(
            "[RESEARCH] Findings have circular dependencies — broke %d cycle-closing "
            "edge(s), preserved the remaining declarations",
            len(_dropped_cycle_edges),
        )
        if _dropped_cycle_edges:
            _ctx = dict(task.context or {})
            _ctx["dropped_finding_cycle_edges"] = _dropped_cycle_edges
            task.context = _ctx

    # Query active/queued tasks for word-overlap deduplication
    existing_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == task.project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                            TaskStatus.PROVISIONING,
                            # A BLOCKED task is in-flight work waiting for a precondition
                            # to be met — dedup against it so we don't re-spawn a
                            # near-duplicate of work that's waiting on prerequisites.
                            TaskStatus.BLOCKED,
                            # A PARKED task is in-flight work that deterministically
                            # resumes on its wake — dedup against it so we don't
                            # re-spawn a near-duplicate of work that's merely parked.
                            TaskStatus.PARKED,
                        ]
                    ),
                )
                .limit(500)
                .order_by(Task.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if len(existing_tasks) >= 500:
        logger.warning(
            "[SPAWN] active-tasks scan hit cap=500 — word-overlap dedup "
            "is truncated; consider filtering by project to reduce result size"
        )
    # Build compact active-task overlap candidates for region-identity dedup:
    # (direction_id, title, task_type, related_files, claims). The related_files
    # (4th element) are the necessary signal for region matching — a spawned task
    # records the finding's related_files in its context (see the spawn payload
    # below), so in-flight work exposes the files it targets. The claims (5th
    # element) feed claim-agreement corroboration for small-file-set cases.
    existing_tasks_with_dirs = [
        (
            (t.context or {}).get("direction_id"),
            t.title.lower(),
            (t.first_payload or {}).get("type"),
            (t.context or {}).get("related_files"),
            (t.context or {}).get("claims") or [],
        )
        for t in existing_tasks
    ]

    # Query recent tasks (last 90 days) for hash-based deduplication across research cycles
    ninety_days_ago = _utcnow() - timedelta(days=90)
    recent_tasks_for_dedup = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == task.project_id,
                    Task.created_at >= ninety_days_ago,
                )
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    if len(recent_tasks_for_dedup) >= 500:
        logger.warning(
            "[SPAWN] recent-tasks scan hit cap=500 — hash-based dedup "
            "is truncated; consider adding index or reducing window"
        )
    # Bounded projecting query for memo dedup — avoids filtering the full 90-day
    # task scan in Python by projecting only the JSONB field we need.
    _memo_hash_rows = (
        (
            await session.execute(
                select(Task.context["memo_source_finding_hash"].as_string())
                .where(
                    Task.project_id == task.project_id,
                    Task.created_at >= ninety_days_ago,
                    Task.context["memo_source_finding_hash"].as_string().isnot(None),
                )
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    _existing_memo_finding_hashes: set[str] = {h for h in _memo_hash_rows if h}
    # Collect parent IDs for COMPLETED+pr_merged tasks to detect ghost merges
    # (code merged to staging branch but parent rollup PR to main failed)
    potential_ghost_parent_ids = {
        t.parent_task_id
        for t in recent_tasks_for_dedup
        if t.parent_task_id
        and t.status == TaskStatus.COMPLETED
        and (t.context or {}).get("pr_merged")
    }
    failed_parent_ids: set = set()
    if potential_ghost_parent_ids:
        parent_tasks_rows = (
            (
                await session.execute(
                    select(Task).where(Task.id.in_(potential_ghost_parent_ids))
                )
            )
            .scalars()
            .all()
        )
        # Base case: parent FAILED or CANCELLED — code never reached integration branch.
        failed_parent_ids = {
            p.id
            for p in parent_tasks_rows
            if p.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
        }
        # Extended case (a): parent COMPLETED but rollup PR never merged — child's
        # code is stranded on the staging branch with no path to integration branch.
        failed_parent_ids |= {
            p.id
            for p in parent_tasks_rows
            if p.status == TaskStatus.COMPLETED
            and not (p.context or {}).get("pr_merged")
        }
        # Extended case (b): parent in non-terminal state — attempt git-ancestry
        # verification on the rollup PR's merge commit.  Only mark stranded on a
        # DEFINITIVE negative (is_ancestor_of returns False); indeterminate (None,
        # missing merge SHA, or API error) falls back to flag-trust so a transient
        # GitHub outage never triggers spurious re-spawns.
        _non_terminal_statuses = frozenset(
            {
                TaskStatus.EVALUATING,
                TaskStatus.QUEUED,
                TaskStatus.IN_PROGRESS,
                TaskStatus.PROVISIONING,
            }
        )
        _non_terminal_parents = [
            p for p in parent_tasks_rows if p.status in _non_terminal_statuses
        ]
        if _non_terminal_parents:
            _integration_branch_rc = _get_project_integration_branch(project_obj)
            _gh_creds_rc: tuple | None = None
            try:
                from app.release_qualifier import (
                    _resolve_github_with_session as _rc_resolve_gh,
                )
                from app import github as _rc_github

                if project_obj is not None:
                    _gh_creds_rc = await _rc_resolve_gh(session, project_obj)
            except Exception as _exc_rc:  # noqa: BLE001 — credential failure → skip
                logger.debug(
                    "[RESEARCH] GitHub credential resolution skipped for ancestry check: %s",
                    _exc_rc,
                )
            if _gh_creds_rc is not None:
                _rc_token, _rc_owner, _rc_repo = _gh_creds_rc
                _rc_pr_cache: dict[int, dict] = {}
                for _p in _non_terminal_parents:
                    _p_ctx = _p.context or {}
                    _pr_num_raw = _p_ctx.get("rollup_pr_number") or _p_ctx.get(
                        "pr_number"
                    )
                    if not _pr_num_raw:
                        continue
                    try:
                        _pr_num = int(_pr_num_raw)
                        if _pr_num not in _rc_pr_cache:
                            _rc_pr_cache[_pr_num] = await _rc_github.get_pull_request(
                                _rc_token,
                                _rc_owner,
                                _rc_repo,
                                _pr_num,
                                project_id=str(task.project_id),
                            )
                        _pr_data = _rc_pr_cache[_pr_num] or {}
                        _merge_sha = _pr_data.get("merge_commit_sha")
                        if not _merge_sha:
                            # PR not yet merged — no commit to check; indeterminate,
                            # fall back to flag-trust (not stranded).
                            continue
                        _is_anc = await _rc_github.is_ancestor_of(
                            _rc_token,
                            _rc_owner,
                            _rc_repo,
                            _merge_sha,
                            _integration_branch_rc,
                            project_id=str(task.project_id),
                        )
                        if _is_anc is False:
                            # Squash-merge fallback: the stored merge_commit_sha is the
                            # pre-squash commit, not the squash commit on main.  Check
                            # whether recent integration-branch commits reference this
                            # PR number or any of its original commit SHAs before
                            # declaring the parent stranded.
                            if str(_p.id) in _SQUASH_LANDED_CONFIRMED_PARENT_IDS:
                                # Already confirmed as squash-landed on a prior cycle.
                                continue
                            try:
                                _pr_commits = await _rc_github.get_pull_request_commits(
                                    _rc_token, _rc_owner, _rc_repo, _pr_num
                                )
                                _pr_commit_shas = [
                                    c.get("sha")
                                    for c in (_pr_commits or [])
                                    if isinstance(c, dict) and c.get("sha")
                                ]
                                _is_sq_ref = await _rc_github.check_squash_referenced(
                                    _rc_token,
                                    _rc_owner,
                                    _rc_repo,
                                    _integration_branch_rc,
                                    _pr_num,
                                    commit_shas=_pr_commit_shas,
                                    project_id=str(task.project_id),
                                )
                            except Exception as _exc_sq:  # noqa: BLE001
                                logger.debug(
                                    "[RESEARCH] squash-reference check failed for "
                                    "parent task %s: %s",
                                    _p.id,
                                    _exc_sq,
                                )
                                _is_sq_ref = None
                            if _is_sq_ref is True:
                                _SQUASH_LANDED_CONFIRMED_PARENT_IDS.add(str(_p.id))
                                logger.info(
                                    "[RESEARCH] Parent task %s: ancestry False but "
                                    "squash reference found on %s — not stranded",
                                    _p.id,
                                    _integration_branch_rc,
                                )
                                continue
                            if _is_sq_ref is False:
                                failed_parent_ids.add(_p.id)
                                logger.info(
                                    "[RESEARCH] Parent task %s: ancestry+squash check "
                                    "confirms merge commit %s not on %s — marking stranded",
                                    _p.id,
                                    _merge_sha[:8],
                                    _integration_branch_rc,
                                )
                            # _is_sq_ref is None (API error) — indeterminate; fall
                            # through without stranding (flag-trust).
                    except Exception as _exc_p:  # noqa: BLE001 — API error → skip
                        logger.debug(
                            "[RESEARCH] ancestry check failed for parent task %s: %s",
                            _p.id,
                            _exc_p,
                        )

    # Cross-cycle suppression: load previously filtered hashes and retired direction
    # failed approaches so findings rejected last cycle stay rejected, and findings
    # overlapping retired directions' known-bad approaches are pre-emptively skipped.
    _direction_id_for_dedup = (task.context or {}).get("direction_id")
    _previously_filtered_hashes: set[str] = set()
    _retired_failed_approaches: list[str] = []
    _dedup_project_dirs = list((project_obj.directions if project_obj else None) or [])
    for _d in _dedup_project_dirs:
        if _d.get("id") == _direction_id_for_dedup:
            _pfh = _d.get("recently_filtered_finding_hashes") or []
            _previously_filtered_hashes = set(_pfh)
            break
    _retired_ctx_list: list[dict] = []
    for _d in _dedup_project_dirs:
        if (
            _d.get("status") in RETIRED_DIRECTION_STATUSES
            and _d.get("id") != _direction_id_for_dedup
        ):
            _rc = _d.get("research_context", "") or ""
            _parsed_rc = _parse_research_context(_rc) if _rc else {}
            _retired_ctx_list.append(
                {
                    "failed_approaches": _parsed_rc.get("failed", []),
                    "recently_completed_finding_titles": _parsed_rc.get(
                        "confirmed", []
                    ),
                    "category": _d.get("category") or "mixed",
                }
            )
    _direction_categories: dict[str, str] = {
        _d["id"]: _d.get("category") or "mixed"
        for _d in _dedup_project_dirs
        if _d.get("id")
    }
    _retired_failed_approaches = extract_retired_failed_approaches(_retired_ctx_list)
    _archived_diagnoses = extract_archived_diagnoses(
        project_obj.design_doc if project_obj else None
    )

    recent_finding_hashes, failed_hashes, prior_failure_by_hash = build_dedup_state(
        recent_tasks_for_dedup,
        failed_parent_ids,
        previously_filtered_hashes=_previously_filtered_hashes,
    )
    recent_terminal_findings = extract_recent_terminal_finding_history(
        [t for t in recent_tasks_for_dedup if t.id != task.id],
        failed_parent_ids,
    )

    # Query open PR artifacts to detect file-scope conflicts with in-flight work
    open_pr_stmt = (
        select(Artifact.pr_url, Artifact.logs, Task.context)
        .join(Task, Artifact.task_id == Task.id)
        .where(
            Task.project_id == task.project_id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            Artifact.pr_url.isnot(None),
            # Exclude BLOCKED alongside the terminal statuses: a BLOCKED task
            # cannot currently progress, so its related_files are not actively
            # contested and must not suppress otherwise-valid new work as a
            # false-positive file-scope conflict.
            Task.status.not_in(
                [
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.BLOCKED,
                ]
            ),
        )
        .limit(200)
    )
    open_pr_rows = (await session.execute(open_pr_stmt)).all()
    if len(open_pr_rows) >= 200:
        logger.warning(
            f"[RESEARCH] open PR artifact query hit 200-row cap for project {task.project_id} — file-overlap dedup may be incomplete"
        )
    open_pr_files: set[str] = set()
    for _, _, task_ctx in open_pr_rows:
        if task_ctx:
            for f in task_ctx.get("related_files") or []:
                open_pr_files.add(f)

    # Sort findings by priority (high → medium → low) and apply the hash,
    # word-overlap, and file-overlap dedup stages. The orchestrator dispatches
    # by (status, priority, created_at), so higher-priority tasks naturally
    # run first without needing artificial wave dependencies.
    # Pre-build title→hash map so filtered-finding hashes can be persisted for
    # cross-cycle suppression after dedup runs.
    _title_to_fhash: dict[str, str] = {
        f.get("title", f"Research finding #{i + 1}"): _finding_hash(
            f.get("title", f"Research finding #{i + 1}"),
            _canonical_finding_body(f),
        )
        for i, f in enumerate(findings)
    }
    _dedup_outcome = deduplicate_findings(
        findings,
        task.context,
        recent_finding_hashes,
        failed_hashes,
        existing_tasks_with_dirs,
        open_pr_files,
        retired_failed_approaches=_retired_failed_approaches,
        archived_diagnoses=_archived_diagnoses,
        direction_categories=_direction_categories,
        recent_terminal_findings=recent_terminal_findings,
    )
    deduplicated = _dedup_outcome.deduplicated
    hash_dedup_count = _dedup_outcome.hash_dedup_count
    word_overlap_count = _dedup_outcome.word_overlap_count
    file_overlap_count = _dedup_outcome.file_overlap_count
    evidence_overlap_count = _dedup_outcome.evidence_overlap_count
    filtered_findings = _dedup_outcome.filtered
    # Capture dedup-filtered PERF findings into the salvage buffer so they
    # are durably captured as a consolidated task instead of silently vanishing
    # through the dedup pipeline (hash-match, region-identity, file-overlap,
    # evidence-overlap, retired-approach, archived-diagnosis gates). Per-finding
    # actionability thresholds are NOT relaxed — bundling filtered findings via
    # salvage avoids flooding the pipeline with individual tasks.
    # Track finding object ids added here so the reason-based loop below
    # does not add the same finding a second time.
    _perf_salvaged_finding_ids: set[int] = set()
    for _filtered_entry in _dedup_outcome.filtered:
        if _filtered_entry.get("terminal_history"):
            continue
        _inner_finding = _filtered_entry.get("finding")
        if _inner_finding and _inner_finding.get("type") == "perf":
            _inner_finding["deferred_reason"] = (
                _filtered_entry.get("reason", "dedup_filtered")[:80]
            )
            _salvage_candidates.append(_inner_finding)
            _perf_salvaged_finding_ids.add(id(_inner_finding))

    type_filtered_count = 0
    type_filtered_by_dir: dict[str, int] = {}
    empty_prompt_count = 0

    # Hashes of findings that survived parsing into `findings` but were dropped
    # from the SPAWN set by deduplicate_findings (duplicate of existing in-flight,
    # recently-terminal, or already-shipped work — word/hash/file/evidence
    # overlap). Every finding either survives into `deduplicated` or is filtered,
    # disjointly, so the dropped set is exactly {all hashes} − {survivor hashes}.
    # Consumed at spawn-wiring (the depends_on_indices missing-sibling loop): a
    # SURVIVING dependent that declared an edge to one of these duplicates spawns
    # standalone instead of cancelling on a dead prerequisite.
    _survivor_finding_hashes = {h for (_idx, _f, h) in _dedup_outcome.deduplicated}
    _deduped_dropped_hashes = {
        h for h in _title_to_fhash.values() if h not in _survivor_finding_hashes
    }

    # Feed dedup-filtered findings into the salvage pipeline so they are
    # durably captured instead of silently dropped. The full finding payload
    # is carried in each filtered entry's ``finding`` key (set by all
    # dedup_gates in finding_deduplication.py), so we read it directly —
    # no need for a fragile title-based lookup.
    # Exclude findings that were suppressed for reasons indicating the work
    # is already genuinely resolved or known-unproductive:
    #   - cross-cycle hash suppression (previously_filtered_hashes)
    #   - recent terminal region overlap (already resolved/shipped)
    #   - retired approach (known failed direction)
    #   - archived diagnosis (known stale diagnosis)
    # Only salvage findings filtered by reasons that indicate the finding is
    # still relevant but was suppressed by in-flight work:
    #   - region overlap with an existing task (blocked, not resolved)
    #   - all files covered by open PR (blocked until PR merges)
    #   - evidence overlap with existing finding (Jaccard dedup — blocked
    #     by another finding in the same batch, not resolved)
    # Exclude findings filtered by terminal/retired/archived/hash reasons
    # — those represent work already resolved, attempted, duplicated,
    # or captured by another surviving finding in the same batch.
    _SALVAGEABLE_DEDUP_REASONS = (
        "region overlap with:",
        "all files covered by open PR",
        "evidence overlap with:",
    )
    # Build a fallback lookup dict for filtered entries that lack the
    # ``finding`` key (legacy pattern; all current append sites set it).
    _findings_by_key: dict[tuple[str, object], dict] = {}
    for _orig in findings:
        _key = (_orig.get("title", "")[:120], _orig.get("direction_id"))
        if _key not in _findings_by_key:
            _findings_by_key[_key] = _orig

    _dedup_filtered_salvage: list[dict] = []
    for _filt in _dedup_outcome.filtered:
        if _filt.get("terminal_history"):
            continue
        _filt_reason = _filt.get("reason", "dedup filtered")
        if not any(_filt_reason.startswith(_sr) for _sr in _SALVAGEABLE_DEDUP_REASONS):
            continue
        # Read full finding payload directly from the filtered entry.
        _candidate_info = _filt.get("finding")
        if _candidate_info is None:
            # Fall back to key-based lookup for backward compat.
            _key = (_filt.get("title", ""), _filt.get("direction_id"))
            _candidate_info = _findings_by_key.get(_key)
        if _candidate_info is None:
            continue
        # Skip findings already added by the perf-type loop above to avoid
        # double-counting a finding that is both type=="perf" and filtered
        # for a salvageable reason.
        if id(_candidate_info) in _perf_salvaged_finding_ids:
            continue
        # Skip cross-cycle hash suppression.
        _candidate_hash = _title_to_fhash.get(_candidate_info.get("title", ""))
        if _candidate_hash and _candidate_hash in _previously_filtered_hashes:
            continue
        _candidate = dict(_candidate_info)
        _candidate["deferred_reason"] = f"dedup: {_filt_reason}"
        _dedup_filtered_salvage.append(_candidate)
    if _dedup_filtered_salvage:
        _salvage_candidates.extend(_dedup_filtered_salvage)
        logger.info(
            "[RESEARCH] Added %d dedup-filtered finding(s) to salvage candidates (task %s)",
            len(_dedup_filtered_salvage),
            task.id,
        )

    # Assess pipeline health once before spawning implementation tasks.
    # Metadata findings (design_doc_observation, direction_update) are processed
    # regardless; only VM-consuming implementation tasks are capped.
    _pipeline_degraded, _health_reason = await _assess_project_spawn_health(
        session,
        task.project_id,
        _effective_cap,
    )
    _impl_spawned = 0
    _degradation_gate_deferred: list[dict] = []
    _low_actionability_deferred: list[dict] = []
    # Findings rejected by the reproduce-on-HEAD gate: their cited code is
    # unambiguously gone, so they are NOT spawned into work. They are also NOT
    # re-deferred — re-validating them next cycle would yield the same verdict
    # (a retry loop), and they are excluded from the surviving-finding success
    # signal so the cycle scores neutral, never a spurious success/failure.
    _reproduce_rejected: list[dict] = []
    # Findings rejected because they belong to a criterion-carrying direction but
    # lack acceptance_evidence:{kind:named_test}. Hard rejection (not deferred) —
    # the finding cannot close the criterion coverage loop without a named-test
    # proof declaration.
    _criterion_evidence_rejected: list[dict] = []
    # compute_experiment findings whose authored launch contract was incomplete
    # or malformed — deferred loudly rather than spawning a broken compute job.
    _compute_contract_deferred: list[dict] = []
    # Findings that failed the adversarial per-finding challenge: deferred (not
    # dropped) so they resurface on the next cycle for a fresh attempt.
    _adversarial_gate_deferred: list[dict] = []
    _low_conf_skipped_findings: list[dict] = []
    _scope_skip_counts: dict[str, int] = {}

    # Persist research-audit knowledge document when the direction is in audit/measurement
    # mode. This runs before the spawn loop so the document is durable even if all findings
    # are deduped away. Best-effort: a failure never blocks the spawn loop.
    _audit_direction_id = (task.context or {}).get("direction_id")
    _audit_direction: dict | None = None
    if _audit_direction_id and project_obj is not None:
        _audit_direction = next(
            (
                d
                for d in (project_obj.directions or [])
                if d.get("id") == _audit_direction_id
            ),
            None,
        )
    if _normalized_recommended_mode(_audit_direction) == "audit" and (
        findings or insights
    ):
        try:
            from app.knowledge_completion import persist_research_audit_document

            await persist_research_audit_document(
                task, findings, insights, _audit_direction, session
            )
        except Exception:  # noqa: BLE001 — best-effort; must not block spawn loop
            logger.warning(
                "[RESEARCH] Failed to persist research-audit knowledge doc for task %s",
                task.id,
                exc_info=True,
            )

    # Create all child tasks — no wave dependencies, priority ordering handles dispatch order
    child_tasks = []
    finding_indices = []  # Maps child_tasks index → findings array index
    # Parallel flag: True when the child came from an auto-decomposed sub-finding.
    # Used to chain consecutive siblings of the same parent finding via depends_on
    # (so they execute serially, each agent seeing prior siblings' actually-merged
    # work in its base branch instead of partial scaffolding from in-flight peers).
    _auto_decomposed_flags: list[bool] = []

    # Pre-load project for scope calibration (SQLAlchemy identity map — no extra query if already loaded)
    _scope_cal_project = await session.get(Project, task.project_id)

    # Default a child compute_experiment's container_image to the project's
    # declared, digest-pinned baked image. Shared with the dispatch path via
    # resolve_project_default_container_image so the two cannot drift (fail-open).
    from app.compute_backends import resolve_project_default_container_image
    _project_default_container_image = await resolve_project_default_container_image(
        session, _scope_cal_project
    )

    _directions_by_id: dict = {}
    if _scope_cal_project:
        for _d in _scope_cal_project.directions or []:
            if _d.get("id"):
                _directions_by_id[_d["id"]] = _d
    # Persist filtered finding hashes to the current direction so next cycle's
    # build_dedup_state can fold them in and keep them suppressed (bounded at 50).
    _new_filtered_hashes = {
        _title_to_fhash[f["title"]]
        for f in _dedup_outcome.filtered
        if f.get("title") in _title_to_fhash
    }
    if _new_filtered_hashes:

        def _update_filtered_hashes(dirs: list) -> None:
            for _d in dirs:
                if _d.get("id") == _direction_id_for_dedup:
                    _existing_fh = _d.get("recently_filtered_finding_hashes") or []
                    _combined_fh = list(
                        dict.fromkeys(_existing_fh + list(_new_filtered_hashes))
                    )
                    _d["recently_filtered_finding_hashes"] = _combined_fh[-50:]
                    break

        await with_directions_locked(session, task.project_id, _update_filtered_hashes)

    # Also index by parent_direction_id in task context as fallback
    _parent_direction_id = (task.context or {}).get("direction_id")

    _cochange_index_for_decomp = (
        (_scope_cal_project.design_doc or {}).get("cochange_index")
        if _scope_cal_project
        else None
    )
    # Global kill-switch for co-change auto-decomposition. Default OFF: live data
    # showed split sub-findings merging at ~0% while ~3x-ing task fan-out, so by
    # default findings pass through unsplit. Flip cochange_decomposition_enabled
    # to re-enable (the per-direction self-suppressor below still applies when
    # on). Only the splitting is gated — the cochange_index that also feeds
    # scope_complexity scoring + agent-prompt hints is unaffected.
    if not _runtime_config.get("cochange_decomposition_enabled", False):
        pass  # decomposition disabled — leave `deduplicated` unsplit
    else:
        _suppressed_decomp_dirs = {
            str(_d.get("id"))
            for _d in _directions_by_id.values()
            if _cochange_decomposition_suppressed(_d)
        }
        if _suppressed_decomp_dirs:
            _to_decomp: list = []
            _skip_decomp: list = []
            for _item in deduplicated:
                _fid = str(_item[1].get("direction_id") or _parent_direction_id or "")
                if _fid in _suppressed_decomp_dirs:
                    _skip_decomp.append(_item)
                else:
                    _to_decomp.append(_item)
            if _skip_decomp:
                logger.info(
                    "[RESEARCH] Suppressing cochange decomposition for %d finding(s) "
                    "in %d direction(s) with poor historical sub-finding merge rate",
                    len(_skip_decomp),
                    len(_suppressed_decomp_dirs),
                )
            deduplicated = (
                auto_decompose_findings_by_cochange(
                    _to_decomp,
                    _cochange_index_for_decomp,
                    task.context,
                    _finding_hash,
                )
                + _skip_decomp
            )
        else:
            deduplicated = auto_decompose_findings_by_cochange(
                deduplicated,
                _cochange_index_for_decomp,
                task.context,
                _finding_hash,
            )
    # Track auto-decomposition outcomes per direction so direction_outcomes
    # can correlate decomposition success with merge rates (parallels
    # plan-parent decomposition_quality tracking).
    _decomp_stats_by_dir: dict[str, dict] = {}
    for _oi, _f, _ in deduplicated:
        if not _f.get("auto_decomposed"):
            continue
        _did = str(_f.get("direction_id") or _parent_direction_id or "")
        if not _did:
            continue
        _s = _decomp_stats_by_dir.setdefault(
            _did, {"sub_findings_produced": 0, "_orig_ids": set()}
        )
        _s["sub_findings_produced"] += 1
        _s["_orig_ids"].add(_oi)
    if _decomp_stats_by_dir:
        _ctx = dict(task.context or {})
        _ctx["cochange_decomposition_stats"] = {
            _did: {
                "medium_findings_split": len(_s["_orig_ids"]),
                "sub_findings_produced": _s["sub_findings_produced"],
            }
            for _did, _s in _decomp_stats_by_dir.items()
        }
        task.context = _ctx
        flag_modified(task, "context")

    # Pre-fetch criterion_keys for every direction referenced by deduplicated
    # findings (plus the parent direction).  A finding may carry its own
    # direction_id that differs from _parent_direction_id; without this
    # pre-fetch, _direction_criterion_cache would miss that key and the spawned
    # task would inherit no criterion_ids even though its direction has some.
    # Best-effort: a lookup failure leaves that direction absent from the cache.
    _direction_criterion_cache: dict[str, list[str]] = {}
    from app.acceptance_criteria import (
        direction_criterion_release_version_id as _direction_criterion_version,
        resolve_task_criterion_release_version_id as _resolve_criterion_version,
        task_criterion_release_version_id as _task_criterion_version,
    )

    _criterion_release_version_id = _task_criterion_version(task)
    _criterion_version_was_explicit = _criterion_release_version_id is not None
    if _criterion_release_version_id is None:
        _criterion_release_version_id = await _resolve_criterion_version(
            session, task, task.project_id
        )
    _dirs_to_prefetch: set[str] = set()
    if _parent_direction_id:
        _dirs_to_prefetch.add(_parent_direction_id)
    for _, _pf_finding, _ in deduplicated:
        _pf_did = _pf_finding.get("direction_id")
        if _pf_did and isinstance(_pf_did, str):
            _dirs_to_prefetch.add(_pf_did)
    if _dirs_to_prefetch:
        try:
            from app.directions_repo import get_direction_rows as _get_dir_rows

            # Single batched SELECT for every direction we need, keyed for O(1)
            # lookup — avoids one round-trip per direction (N+1).
            _pf_rows = await _get_dir_rows(
                session, task.project_id, _dirs_to_prefetch
            )
            for _pf_dir_id, _pf_row in _pf_rows.items():
                try:
                    if (
                        _pf_row is not None
                        and _pf_row.criterion_keys
                        and (
                            _direction_criterion_version(_pf_row)
                            == _criterion_release_version_id
                            or (
                                not _criterion_version_was_explicit
                                and _direction_criterion_version(_pf_row) is None
                            )
                        )
                    ):
                        from app.acceptance_criteria import (
                            filter_active_criterion_keys as _filter_active,
                        )
                        _active_keys = await _filter_active(
                            session,
                            task.project_id,
                            list(_pf_row.criterion_keys),
                            release_version_id=_criterion_release_version_id,
                        )
                        if _active_keys:
                            _direction_criterion_cache[_pf_dir_id] = _active_keys
                except Exception:
                    logger.debug(
                        "[RESEARCH] criterion_keys lookup failed for direction %s",
                        _pf_dir_id,
                    )
        except Exception:
            logger.debug("[RESEARCH] directions_repo import failed; criterion cache empty")

    for orig_i, finding, fhash in deduplicated:
        scope = finding.get("scope", DEFAULT_TASK_SCOPE)
        risk = finding.get("risk", "low")
        title = finding.get("title", f"Research finding #{orig_i + 1}")
        prompt = _canonical_finding_body(finding)

        # Pre-spawn scope-budget gate: catch obvious file-count/scope mismatch before
        # an agent is dispatched. Runs before history-based calibration so all
        # subsequent steps work on the corrected scope.
        _sbg_files = finding.get("related_files") or []
        scope, _sbg_cal_from, _scope_budget_warning, _budget_requires_approval = (
            check_scope_budget_mismatch(scope, _sbg_files)
        )

        # Determine task type early — some findings don't spawn tasks
        finding_type = finding.get("type", "improvement")

        if finding_type == "design_doc_observation":
            # Append to design doc proposals for Phase 1 to review
            _obs_text = finding.get("description", "")[:500]
            _obs_section = finding.get("section", "general")
            _obs_entry = {
                "id": f"pc-{uuid.uuid4().hex[:8]}",
                "observation": _obs_text,
                "section": _obs_section,
                "proposed_by_task": str(task.id),
                "proposed_at": _utcnow().isoformat(),
                "status": "pending",
            }

            def _add_obs_proposal(doc: dict) -> None:
                proposals = doc.get("proposed_changes", [])
                # Dedup: skip if identical observation already exists
                existing = next(
                    (p for p in proposals
                     if p.get("observation") == _obs_text and p.get("section") == _obs_section),
                    None
                )
                if not existing:
                    proposals.append(_obs_entry)
                doc["proposed_changes"] = proposals[-30:]  # cap at 30 pending

            await with_design_doc_locked(session, task.project_id, _add_obs_proposal)
            type_filtered_count += 1
            _tf_did = finding.get("direction_id") or _parent_direction_id
            if _tf_did:
                type_filtered_by_dir[_tf_did] = type_filtered_by_dir.get(_tf_did, 0) + 1
            continue  # Don't create a task

        if finding_type == "direction_update":
            # Process as a direction modification proposal, not a task
            target_id = finding.get("direction_id")
            if not target_id:
                type_filtered_count += 1
                _tf_did = finding.get("direction_id") or _parent_direction_id
                if _tf_did:
                    type_filtered_by_dir[_tf_did] = (
                        type_filtered_by_dir.get(_tf_did, 0) + 1
                    )
                continue  # direction_update requires a target direction_id
            _du_finding = finding
            _du_target_id = target_id
            _du_task_id = str(task.id)

            def _set_pending_update(dirs: list) -> None:
                for d in dirs:
                    if d.get("id") == _du_target_id:
                        d["pending_update"] = {
                            "suggested_text": _du_finding.get(
                                "suggested_text", d.get("text")
                            ),
                            "suggested_priority": _du_finding.get(
                                "suggested_priority", d.get("priority")
                            ),
                            "suggested_paths": _du_finding.get(
                                "suggested_paths", d.get("paths", [])
                            ),
                            "rationale": _du_finding.get("description", "")[:300],
                            "proposed_at": _utcnow().isoformat(),
                            "proposed_by": _du_task_id,
                        }
                        break

            await with_directions_locked(session, task.project_id, _set_pending_update)
            type_filtered_count += 1
            _tf_did = finding.get("direction_id") or _parent_direction_id
            if _tf_did:
                type_filtered_by_dir[_tf_did] = type_filtered_by_dir.get(_tf_did, 0) + 1
            continue  # Don't create a task for direction_update findings

        if not prompt:
            empty_prompt_count += 1
            continue

        # Skip findings already directly dispatched by the research agent (scope=small+risk=low).
        # These were sent via /api/agent/spawn before research completed; no task needed here.
        if finding.get("already_spawned"):
            continue

        # Adversarial gate: defer findings that failed the per-finding adversarial
        # challenge. The finding is deferred (not dropped) so it resurfaces in
        # prior_deferred_findings on the next research cycle for a fresh attempt.
        # The _adversarial_verdict key is cleared in the deferral stamp so the
        # finding re-enters the next cycle clean (without a stale fail verdict).
        if finding.get("_adversarial_verdict") == "fail":
            _adversarial_gate_deferred.append(finding)
            logger.info(
                "[RESEARCH] Deferring finding %r (task %s): failed adversarial challenge",
                finding.get("title") or finding.get("description", "")[:60],
                task.id,
            )
            continue

        # Reproduce-on-HEAD GATE. A finding stamped ``_reproduce_on_head ==
        # "absent"`` (at fresh ingestion or deferred-recovery re-grounding) cites
        # code that is unambiguously gone — every cited file deleted and every
        # cited symbol orphaned. Do NOT route it into a fix/create task: it would
        # chase a defect that no longer exists. Reject it (not re-deferred, to
        # avoid a retry loop); it is excluded from the surviving-finding success
        # signal so the cycle scores neutral. Indeterminate/partially-grounded
        # findings are never stamped absent and fall through untouched.
        if finding.get("_reproduce_on_head") == "absent":
            _record = _consume_finding_challenge_record(
                task,
                finding,
                boundary=CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
                first_verdict="absent",
            )
            _challenge_enabled = _finding_challenge_enabled(
                CHALLENGE_BOUNDARY_REPRODUCE_ABSENT
            )
            if _challenge_enabled and _record is None:
                _challenge = await spawn_finding_challenge(
                    task,
                    finding,
                    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
                    "absent",
                    session,
                    question=(
                        "Is the actual subject gone on current HEAD, or did the "
                        "cited path/symbol move or rename while the semantic "
                        "target still exists?"
                    ),
                    finding_hash=_finding_hash(
                        finding.get("title", ""),
                        finding.get("description") or finding.get("prompt", ""),
                    ),
                )
                if _challenge is not None or (task.context or {}).get(
                    "awaiting_finding_challenge"
                ):
                    logger.info(
                        "[RESEARCH] Task %s parked awaiting reproduce-absent "
                        "challenge for finding %r",
                        task.id,
                        finding.get("title") or finding.get("description", "")[:60],
                    )
                    return
            if _challenge_enabled:
                second = _challenge_second_verdict(_record)
                if _challenge_confirms_verdict(_record, "absent"):
                    _reproduce_rejected.append(finding)
                    logger.warning(
                        "[RESEARCH] Rejecting finding %r (task %s): cited code is absent on "
                        "HEAD (does not reproduce); not spawning.",
                        finding.get("title") or finding.get("description", "")[:60],
                        task.id,
                    )
                    continue
                if _challenge_disagrees(_record) and second == CITATION_STATE_PRESENT:
                    finding["_reproduce_on_head"] = CITATION_STATE_PRESENT
                    _reconcile_reproduce_provenance_state(
                        finding, finding["_reproduce_on_head"]
                    )
                    logger.info(
                        "[RESEARCH] Keeping finding %r after reproduce-absent "
                        "challenge established the target still exists: %s",
                        finding.get("title") or finding.get("description", "")[:60],
                        (_record or {}).get("reason"),
                    )
                else:
                    logger.info(
                        "[RESEARCH] Reproduce-absent challenge for finding %r did not "
                        "establish a non-absent target; preserving original absent "
                        "verdict",
                        finding.get("title") or finding.get("description", "")[:60],
                    )
                    _reproduce_rejected.append(finding)
                    logger.warning(
                        "[RESEARCH] Rejecting finding %r (task %s): cited code is absent on "
                        "HEAD (does not reproduce); not spawning.",
                        finding.get("title") or finding.get("description", "")[:60],
                        task.id,
                    )
                    continue
            else:
                if _record is not None and not _challenge_agrees(_record):
                    logger.info(
                        "[RESEARCH] Reproduce-absent challenge for finding %r "
                        "was indeterminate; preserving original absent verdict",
                        finding.get("title") or finding.get("description", "")[:60],
                    )
                _reproduce_rejected.append(finding)
                logger.warning(
                    "[RESEARCH] Rejecting finding %r (task %s): cited code is absent on "
                    "HEAD (does not reproduce); not spawning.",
                    finding.get("title") or finding.get("description", "")[:60],
                    task.id,
                )
                continue

        if (
            finding.get("_reproduce_on_head") == CITATION_STATE_INDETERMINATE
            and finding_type in _REPRODUCE_VERDICT_FINDING_TYPES
            and _finding_challenge_enabled(
                CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH
            )
        ):
            _record = _consume_finding_challenge_record(
                task,
                finding,
                boundary=CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
                first_verdict=CITATION_STATE_INDETERMINATE,
            )
            if _record is None:
                _challenge = await spawn_finding_challenge(
                    task,
                    finding,
                    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
                    CITATION_STATE_INDETERMINATE,
                    session,
                    question=(
                        "Before dispatch, can this defect finding's target be "
                        "confirmed present or absent on current HEAD?"
                    ),
                    finding_hash=_finding_hash(
                        finding.get("title", ""),
                        finding.get("description") or finding.get("prompt", ""),
                    ),
                )
                if _challenge is not None or (task.context or {}).get(
                    "awaiting_finding_challenge"
                ):
                    logger.info(
                        "[RESEARCH] Task %s parked awaiting indeterminate "
                        "reproduce challenge for finding %r",
                        task.id,
                        finding.get("title") or finding.get("description", "")[:60],
                    )
                    return
            elif _challenge_agrees(_record):
                second = str(_record.get("second_opinion_verdict") or "").lower()
                if second in (
                    CITATION_STATE_PRESENT,
                    CITATION_STATE_ABSENT,
                    CITATION_STATE_INDETERMINATE,
                ):
                    finding["_reproduce_on_head"] = second
            elif _challenge_disagrees(_record):
                second = str(_record.get("second_opinion_verdict") or "").lower()
                if second in (CITATION_STATE_PRESENT, CITATION_STATE_ABSENT):
                    finding["_reproduce_on_head"] = second

        if finding.get("_reproduce_on_head") == "absent":
            _reproduce_rejected.append(finding)
            logger.warning(
                "[RESEARCH] Rejecting finding %r (task %s): cited code is absent on "
                "HEAD (does not reproduce); not spawning.",
                finding.get("title") or finding.get("description", "")[:60],
                task.id,
            )
            continue

        # Spawn-time health gate: when pipeline is degraded, cap implementation task creation
        # to avoid queuing tasks that will fail immediately or stall indefinitely.
        # Metadata findings (already handled above) and already_spawned bypass this check.
        if _pipeline_degraded and _impl_spawned >= _DEGRADED_SPAWN_CAP:
            _degradation_gate_deferred.append(finding)
            continue

        # Apply scope calibration from direction's historical merge rates (skip if already calibrated)
        scope_calibrated_from: str | None = (
            finding.get("scope_calibrated_from") or _sbg_cal_from
        )
        _history_calibration_fired = False
        if not (
            scope_calibrated_from or (task.context or {}).get("scope_calibrated_from")
        ):
            _dir_id = finding.get("direction_id") or _parent_direction_id
            _direction = _directions_by_id.get(str(_dir_id)) if _dir_id else None
            if _direction:
                # Determine preliminary task_type from finding_type so success-pattern
                # evidence in _feedback_scope_override can be used during calibration.
                # The scope-dependent branch (plan/create) is resolved using the
                # pre-calibration scope, which is the proposed scope at this point.
                if finding_type == "research":
                    _prelim_task_type: str | None = "research"
                elif finding_type in COMPUTE_EXPERIMENT_FINDING_TYPES:
                    _prelim_task_type = "compute_experiment"
                elif finding_type in (
                    "security_audit",
                    "qa",
                    "deps",
                    "docs",
                    "migrate",
                    "perf",
                ):
                    _prelim_task_type = finding_type
                elif finding_type in ("web_research", "design"):
                    # knowledge-producing types: spawn the matching knowledge
                    # task rather than falling through to the scope-based
                    # plan/create fallback below.
                    _prelim_task_type = finding_type
                elif finding_type == "refactor":
                    _prelim_task_type = "refactor"
                elif scope == "large":
                    _prelim_task_type = "plan"
                else:
                    _prelim_task_type = "create"
                scope, scope_calibrated_from = _calibrate_scope(
                    scope, _direction, _scope_cal_project, _prelim_task_type
                )
                _history_calibration_fired = scope_calibrated_from is not None

        # Pre-spawn scope-upgrade heuristics (file count, cross-module span,
        # expansion-ratio prediction, composite complexity). Extracted into
        # _calibrate_finding_scope so the heuristics are independently testable.
        # Skip when _calibrate_scope just fired: historical merge-rate data is
        # a stronger signal than file-count heuristics, so a freshly-applied
        # downgrade (e.g. medium→small) should not be reversed by the small→medium
        # heuristic that fires on related_files >= 4.
        # Also skip heuristics for auto-decomposed findings AT SMALL SCOPE: the
        # decomposition is a deliberate small-scope decision that must not be
        # reversed by the small→medium related_files>=4 heuristic (decomposed
        # clusters often span 3-5 files by design). Medium-scope decomposed
        # sub-findings still need the medium→large safety gates (file count,
        # cross-module span, expansion ratio, composite complexity).
        _skip_auto_decomp = finding.get("auto_decomposed") and scope == "small"
        if not _history_calibration_fired and not _skip_auto_decomp:
            _heur_dir_id = finding.get("direction_id") or _parent_direction_id
            _heur_direction = (
                _directions_by_id.get(str(_heur_dir_id)) if _heur_dir_id else None
            )
            scope, _heur_calibrated_from = _calibrate_finding_scope(
                finding,
                scope,
                _heur_direction,
                _cochange_index_for_decomp,
                all_directions=list(_directions_by_id.values()),
            )
            if _heur_calibrated_from is not None:
                scope_calibrated_from = _heur_calibrated_from

        if finding_type == "research":
            task_type = "research"  # deeper investigation before implementation
        elif finding_type in COMPUTE_EXPERIMENT_FINDING_TYPES:
            # Empirical-measurement finding -> budgeted compute-job substrate.
            # The launch contract is authored + validated below; only spawnable
            # from THIS research path (plan decomposition is NOT loosened: the
            # type stays out of PLAN_SUBTASK_TYPES so plan_completion can't spawn
            # a contract-less compute job from a bare prompt).
            task_type = "compute_experiment"
        elif finding_type in (
            "security_audit",
            "qa",
            "deps",
            "docs",
            "migrate",
            "perf",
        ):
            task_type = finding_type  # spawn specialized agent
        elif finding_type in ("web_research", "design"):
            # Knowledge findings: the work hinges on external facts/best-practices
            # (web_research) or needs a file-grounded design document before code
            # can land safely (design). These route to their registered knowledge
            # task type — NOT a generic create/plan PR task — so the completion
            # handler persists a durable knowledge document. Must come before the
            # scope-based plan/create fallback so a large-scope knowledge finding
            # is never mis-routed to plan/create.
            task_type = finding_type
        elif finding_type == "refactor":
            task_type = "refactor"  # code quality improvement, no behavior change
        elif scope == "large":
            task_type = "plan"  # large scope needs decomposition
        else:
            task_type = "create"  # small/medium scope is single-PR work

        # Low-actionability gate: defer findings with actionability_grade == LOW.
        # These findings are under-specified and spawn implementers that cannot succeed.
        # Defer with decay rule: discard after 3 consecutive deferrals (mirrors degradation_gate).
        # Only apply to new findings; previously-deferred findings have their reason stamped
        # from the prior cycle and are handled by the decay logic.
        if finding.get("actionability_grade") == "LOW" and not finding.get(
            "deferred_reason"
        ):
            _low_actionability_deferred.append(finding)
            logger.info(
                "[RESEARCH] Deferring low-actionability finding '%s' "
                "(actionability_grade=LOW)",
                title,
            )
            continue

        # compute_experiment launch-contract authoring + loud validation + the
        # spend-threshold approval decision. Author a concrete launch contract
        # (command / spend_cap_usd / deadline_seconds / optional success_criterion)
        # BEFORE creating any task; on an incomplete or malformed contract, defer
        # the finding loudly (exactly like the low-actionability/scope-budget defer
        # paths) rather than spawning a broken job. When the proposed spend cap
        # exceeds the configured threshold (per-direction override else global),
        # the spawn is held for operator approval via requires_approval.
        _compute_contract: dict | None = None
        _compute_requires_approval = False
        _compute_threshold = 0.0
        if task_type == "compute_experiment":
            _compute_contract = _author_compute_launch_contract(finding)
            if not _compute_contract.get("container_image") and _project_default_container_image:
                _compute_contract["container_image"] = _project_default_container_image
            _stamp_container_image_pin_status(_compute_contract, task)
            _cc_ok, _cc_reason = _validate_compute_launch_contract(_compute_contract)
            if not _cc_ok:
                _compute_contract_deferred.append(finding)
                logger.warning(
                    "[RESEARCH] Deferring compute_experiment finding '%s': "
                    "malformed launch contract (%s)",
                    title,
                    _cc_reason,
                )
                continue
            _cc_dir_id = finding.get("direction_id") or _parent_direction_id
            _cc_direction = (
                _directions_by_id.get(str(_cc_dir_id)) if _cc_dir_id else None
            )
            _compute_requires_approval, _compute_threshold = (
                resolve_compute_spend_approval(
                    runtime_config=_runtime_config,
                    direction=_cc_direction,
                    spend_cap_usd=float(_compute_contract["spend_cap_usd"]),
                )
            )

        # Apply failure-feedback mitigation: downscope when rejection patterns are high
        # for this direction, and bias toward smaller scopes with proven success patterns.
        _mit_dir_id = finding.get("direction_id") or _parent_direction_id
        _mit_direction = (
            _directions_by_id.get(str(_mit_dir_id)) if _mit_dir_id else None
        )
        if _mit_direction and _scope_cal_project:
            from app.strategic_spawning import _build_failure_feedback_mitigation

            scope, _, _ = _build_failure_feedback_mitigation(
                _mit_direction, scope, task_type, _scope_cal_project.design_doc
            )

        # Pre-spawn approval removed: the review pipeline handles quality gates.
        # Only final merge to main requires human approval (critical files gate).
        # Exception: large-scope findings that far exceed the large-scope file budget
        # require operator confirmation before dispatching (scope_budget_mismatch gate).
        # A compute_experiment whose proposed spend exceeds the threshold is also
        # held (composes with the scope-budget gate — held if EITHER trips).
        needs_approval = _budget_requires_approval or _compute_requires_approval

        direction_id = finding.get("direction_id")

        # Knowledge-commission dedup guard (web_research / design only).
        # Skip the finding when a knowledge document for the same direction AND
        # topic was produced within the cooldown window, OR when an identical
        # knowledge task is currently in-flight for that direction+topic pair.
        # Both checks require direction_id AND topic — a missing key disables
        # the guard (callers must allow the commission rather than over-block).
        if task_type in ("web_research", "design"):
            from app.knowledge_query import (  # noqa: PLC0415
                knowledge_doc_recently_produced,
                knowledge_task_in_flight,
            )
            from app.shared_state import runtime_config as _rc_kguard  # noqa: PLC0415

            _kguard_topic = finding.get("topic") or title
            _kguard_dir = direction_id or (task.context or {}).get("direction_id")
            _kguard_cooldown_cfg = _rc_kguard.get("knowledge_commission_cooldown_hours")
            _kguard_cooldown: float | None = (
                float(_kguard_cooldown_cfg)
                if isinstance(_kguard_cooldown_cfg, (int, float))
                and not isinstance(_kguard_cooldown_cfg, bool)
                else None
            )
            try:
                _kguard_recent = await knowledge_doc_recently_produced(
                    session,
                    task.project_id,
                    _kguard_dir,
                    _kguard_topic,
                    cooldown_hours=_kguard_cooldown,
                )
                _kguard_active = await knowledge_task_in_flight(
                    session, task.project_id, _kguard_dir, _kguard_topic
                )
            except Exception:  # noqa: BLE001 — guard is advisory; must not crash spawn
                logger.warning(
                    "[RESEARCH] knowledge-commission dedup guard failed for task %s "
                    "(direction=%s, topic=%r) — allowing commission",
                    task.id,
                    _kguard_dir,
                    _kguard_topic,
                    exc_info=True,
                )
                _kguard_recent = _kguard_active = False
            if _kguard_recent or _kguard_active:
                _reason = "recent_doc" if _kguard_recent else "in_flight_task"
                logger.info(
                    "[RESEARCH] Skipping %s commission for finding '%s' "
                    "(direction=%s, topic=%r): %s",
                    task_type,
                    title,
                    _kguard_dir,
                    _kguard_topic,
                    _reason,
                )
                continue

        # Ignore finding.model: it's a legacy anthropic-only hint that
        # silently anchored children to a provider regardless of the
        # operator's category_routing. Model resolution is the
        # orchestrator's job (via provider_preference + effort matrix).
        # Operator-level pins via runtime_config.task_type_models still
        # flow through.
        finding_model = _runtime_config.get("task_type_models", {}).get(task_type)
        _effort_rank = {"max": 4, "high": 3, "medium": 2, "low": 1}
        finding_effort = finding.get("effort")
        config_effort = _runtime_config.get("task_type_efforts", {}).get(task_type)
        if finding_effort and config_effort:
            finding_effort = (
                finding_effort
                if _effort_rank.get(finding_effort, 0)
                >= _effort_rank.get(config_effort, 0)
                else config_effort
            )
        elif config_effort:
            finding_effort = config_effort

        related_files = finding.get("related_files", [])
        full_prompt = (
            prompt
            + (
                "\n\nKey files to review or modify:\n"
                + "\n".join(f"- {f}" for f in related_files)
            )
            if related_files
            else prompt
        )
        type_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)

        # For sub-research tasks, enrich prompt with parent context to avoid duplication
        if task_type == "research":
            sibling_titles = [
                f.get("title", "")
                for _, f, _ in deduplicated
                if f.get("title") and f != finding
            ][:5]
            if sibling_titles:
                parent_summary = (
                    "\n\nContext from parent research task: "
                    f"This sub-research is part of a broader investigation (task {str(task.id)[:8]}) "
                    f"that also produced these parallel findings:\n"
                    + "\n".join(f"- {t}" for t in sibling_titles)
                    + "\nFocus your investigation on the specific area described above without duplicating these parallel efforts."
                )
                full_prompt = full_prompt + parent_summary

        # Predict spawn success probability using in-memory direction + project data
        _confidence_threshold = _runtime_config.get("spawn_confidence_threshold", 0.25)
        _pred_dir_id = finding.get("direction_id") or _parent_direction_id
        _pred_direction = (
            _directions_by_id.get(str(_pred_dir_id)) if _pred_dir_id else {}
        )
        _pred_p = predict_spawn_success(
            scope,
            _pred_direction or {},
            _scope_cal_project,
            related_files=related_files,
            complexity_score=finding.get("_complexity_score"),
        )
        _low_confidence = _pred_p < _confidence_threshold
        if _low_confidence:
            logger.info(
                "[RESEARCH] Low-confidence spawn '%s' (scope=%s, type=%s): P(success)=%.2f < threshold=%.2f",
                title,
                scope,
                task_type,
                _pred_p,
                _confidence_threshold,
            )
        # Hard skip gate: ultra-low-confidence medium findings are dropped
        # entirely rather than spawned, escalated, or explored. Runs BEFORE
        # the exploration lottery so exploration cannot override the gate.
        # Exception: data-starved directions (no medium scope_breakdown in the
        # last 3 cycles) fall through to the exploration lottery, which has
        # its own data-starved bypass. Otherwise weak pipeline-wide fallback
        # signals create a death spiral where medium is never attempted.
        _hard_skip_threshold = _runtime_config.get("medium_hard_skip_threshold", 0.10)
        _hard_skip_data_starved = False
        if scope == "medium":
            _hard_skip_data_starved = True
            for _entry in ((_pred_direction or {}).get("research_history") or [])[-3:]:
                if (_entry.get("scope_breakdown") or {}).get("medium"):
                    _hard_skip_data_starved = False
                    break
        if (
            scope == "medium"
            and not _hard_skip_data_starved
            and isinstance(_hard_skip_threshold, (int, float))
            and _pred_p < float(_hard_skip_threshold)
        ):
            logger.info(
                "[RESEARCH] Hard-skipping ultra-low-confidence finding '%s' "
                "(scope=%s): P(success)=%.2f < hard_skip=%.2f",
                title,
                scope,
                _pred_p,
                float(_hard_skip_threshold),
            )
            _low_conf_skipped_findings.append(finding)
            _scope_skip_counts[scope] = _scope_skip_counts.get(scope, 0) + 1
            continue

        _medium_exploration = False
        if (
            _low_confidence
            and scope == "medium"
            # Knowledge tasks (web_research/design) have no scope-based plan/create
            # fallback — escalating them to "plan" would silently misroute the finding
            # away from its knowledge completion handler. Keep their declared type.
            and task_type not in ("web_research", "design")
            and not _poor_decomp_history(_pred_direction or {})
        ):
            # Exploration fraction: let a configurable share of medium findings bypass the
            # confidence gate so the pipeline can accumulate real medium-scope conversion
            # data. Without this, medium rates stay low because no medium tasks ever run,
            # which keeps P(success) below threshold in a self-reinforcing loop.
            _explore_frac = _runtime_config.get("medium_exploration_fraction", 0.2)
            _explore_frac_base = _explore_frac
            # Adapt the exploration fraction from this direction's observed
            # medium-scope exploration outcomes. Directions where exploration
            # consistently merges get a boosted fraction so they accumulate
            # medium data faster; directions where exploration consistently
            # fails get a reduced fraction so they waste fewer attempts.
            _exp_created = 0
            _exp_merged = 0
            for _entry in ((_pred_direction or {}).get("research_history") or [])[-5:]:
                _med = (_entry.get("scope_breakdown") or {}).get("medium") or {}
                _exp_created += int(_med.get("exploration_created") or 0)
                _exp_merged += int(_med.get("exploration_merged") or 0)
            if _exp_created > 0 and isinstance(_explore_frac, (int, float)):
                _exp_rate = _exp_merged / _exp_created
                _base = float(_explore_frac)
                if _exp_rate >= 0.5:
                    _explore_frac = min(1.0, _base * 2.0)
                else:
                    _explore_frac = _base * (_exp_rate / 0.5)
                logger.info(
                    "[RESEARCH] Adaptive exploration fraction for direction %s: rate=%.2f (%d/%d), frac=%.3f",
                    _pred_dir_id,
                    _exp_rate,
                    _exp_merged,
                    _exp_created,
                    float(_explore_frac),
                )
            elif _exp_created == 0 and isinstance(_explore_frac, (int, float)):
                # No direction-level exploration history: use pipeline-wide medium
                # merge rate as a prior so new directions don't burn attempts at the
                # optimistic fixed default when the pipeline is underperforming.
                # Exception: if the direction has ZERO medium-scope outcomes in its
                # recent research_history (no medium key in scope_breakdown across
                # the last 3 cycles), it is data-starved — applying the pipeline
                # discount creates a self-reinforcing loop where medium is never
                # attempted, no direction-specific data is collected, and the
                # fraction stays suppressed forever. Use the base fraction so the
                # direction gets a fair chance to collect its own medium data.
                _has_medium_history = False
                for _entry in ((_pred_direction or {}).get("research_history") or [])[
                    -3:
                ]:
                    if (_entry.get("scope_breakdown") or {}).get("medium"):
                        _has_medium_history = True
                        break
                _pipeline_rates = (
                    _runtime_config.get("pipeline_scope_merge_rates") or {}
                )
                _pipeline_medium = _pipeline_rates.get("medium")
                if (
                    _has_medium_history
                    and isinstance(_pipeline_medium, (int, float))
                    and _pipeline_medium < 0.5
                ):
                    _base = float(_explore_frac)
                    _explore_frac = _base * (float(_pipeline_medium) / 0.5)
                    logger.info(
                        "[RESEARCH] Pipeline-prior exploration fraction for direction %s: "
                        "pipeline_medium=%.2f, frac=%.3f",
                        _pred_dir_id,
                        float(_pipeline_medium),
                        float(_explore_frac),
                    )
                elif not _has_medium_history:
                    logger.info(
                        "[RESEARCH] Data-starved direction %s: no medium history, "
                        "using base exploration fraction=%.3f (pipeline prior skipped)",
                        _pred_dir_id,
                        float(_explore_frac),
                    )
            # Suppress exploration when medium→large escalations in this direction
            # have also been failing: if both direct medium execution and the
            # escalation fallback merge poorly, spawning more medium findings just
            # feeds a losing pipeline regardless of exploration-merge stats.
            _esc_attempted = 0
            _esc_merged = 0
            for _entry in ((_pred_direction or {}).get("research_history") or [])[-3:]:
                _esc = (_entry.get("escalation_outcomes") or {}).get(
                    "medium_to_large"
                ) or {}
                _esc_attempted += int(_esc.get("attempted") or 0)
                _esc_merged += int(_esc.get("merged") or 0)
            if (
                _esc_attempted >= 3
                and isinstance(_explore_frac, (int, float))
                and (_esc_merged / _esc_attempted) < 0.30
            ):
                _explore_frac = float(_explore_frac) * 0.5
                logger.info(
                    "[RESEARCH] Suppressing exploration for direction %s: medium→large "
                    "escalation rate=%.2f (%d/%d) < 0.30, frac=%.3f",
                    _pred_dir_id,
                    _esc_merged / _esc_attempted,
                    _esc_merged,
                    _esc_attempted,
                    float(_explore_frac),
                )
            # Per-finding modulation: structurally complex findings (low
            # composite complexity score) are more likely to overflow at medium
            # scope, so halve their exploration fraction to bias the lottery
            # away from them without affecting simpler findings in the same
            # direction. Applied after direction-level adaptations but before
            # the floor so the floor can still rescue over-suppressed fractions.
            _cs = finding.get("_complexity_score")
            if (
                isinstance(_cs, (int, float))
                and float(_cs) < 0.35
                and isinstance(_explore_frac, (int, float))
            ):
                _explore_frac = float(_explore_frac) * 0.5
                logger.info(
                    "[RESEARCH] Complexity-modulated exploration for '%s': "
                    "complexity_score=%.2f < 0.35, frac=%.3f",
                    title,
                    float(_cs),
                    float(_explore_frac),
                )
            # Enforce a minimum exploration fraction so directions with zero
            # observed exploration merges still occasionally attempt medium scope —
            # otherwise the fraction locks at 0 and no new data is ever collected.
            # Skip the floor when the operator has explicitly disabled exploration
            # by setting the base fraction to 0.
            # Once a direction has enough exploration data to make an informed
            # decision (3+ attempts with 0 merges), skip the floor — the data
            # collection purpose is fulfilled and continued exploration is waste.
            _exploration_proven_unviable = _exp_created >= 3 and _exp_merged == 0
            # Pipeline-wide evidence of unviability: if the pipeline medium merge
            # rate is already at or below the floor, new directions shouldn't
            # independently re-discover that medium is unviable — treat the
            # pipeline signal as sufficient proof and skip the floor.
            _pipeline_rates_cfg = (
                _runtime_config.get("pipeline_scope_merge_rates") or {}
            )
            _pipeline_medium_cfg = _pipeline_rates_cfg.get("medium")
            _pipeline_proven_unviable = (
                isinstance(_pipeline_medium_cfg, (int, float))
                and float(_pipeline_medium_cfg) <= MEDIUM_EXPLORATION_FLOOR
            )
            if (
                isinstance(_explore_frac, (int, float))
                and isinstance(_explore_frac_base, (int, float))
                and float(_explore_frac_base) > 0
                and 0 <= float(_explore_frac) < MEDIUM_EXPLORATION_FLOOR
                and not _exploration_proven_unviable
                and not _pipeline_proven_unviable
            ):
                _explore_frac = MEDIUM_EXPLORATION_FLOOR
            if (
                isinstance(_explore_frac, (int, float))
                and _explore_frac > 0
                and random.random() < float(_explore_frac)
            ):
                _medium_exploration = True
                logger.info(
                    "[RESEARCH] Exploration bypass keeping medium scope for '%s' (P=%.2f, explore_frac=%.2f)",
                    title,
                    _pred_p,
                    float(_explore_frac),
                )
            else:
                logger.info(
                    "[RESEARCH] Auto-escalating scope medium→large for '%s' (low-confidence spawn: P=%.2f < %.2f)",
                    title,
                    _pred_p,
                    _confidence_threshold,
                )
                scope, scope_calibrated_from, task_type = "large", "medium", "plan"

        finding_priority = TASK_PRIORITY_MAP.get(finding.get("priority", "medium"), 50)
        # Inherit parent direction_id if the finding doesn't specify one
        parent_direction_id = (task.context or {}).get("direction_id")

        # If this finding matches a prior failed task (e.g. re-spawn after CODE_ERROR cooldown),
        # inject its failure context using the _previous_* naming convention so the agent's
        # buildRetryContext renders prior-attempt guidance.
        prior_retry_ctx: dict = {}
        _retry_prompt_suffix = ""
        _prior = prior_failure_by_hash.get(fhash)
        if _prior is not None:
            # Infra failures (SCOPE_EMPTY, PROVISION_FAILURE, RATE_LIMIT, …) are
            # pre-emptions or infra events — the task never produced useful output.
            # Treat re-spawns as fresh attempts; injecting "_attempt_number: 2"
            # for an infra failure would mislead the agent into conservative retry
            # behaviour that is only appropriate after a real code failure.
            try:
                if classify_failure(_prior) in INFRA_CLASSES:
                    _prior = None
            except Exception:
                pass
        if _prior is not None:
            _prior_ctx = _prior.context or {}
            prior_retry_ctx["_attempt_number"] = 2
            if _prior.latest_status_message:
                prior_retry_ctx["_previous_error"] = _prior.latest_status_message[:5000]
            failure_class_val: str | None = None
            try:
                failure_class_val = classify_failure(_prior)
                prior_retry_ctx["_previous_failure_class"] = failure_class_val
            except Exception:
                pass
            # Enriched failure diagnosis to mirror task_lifecycle._requeue_parent_task,
            # so research-spawned retries get the same buildRetryContext rendering as
            # inline retries (failure stage labels, recurring warnings, error summary).
            try:
                from app.anomaly_detection import (
                    _error_fingerprint,
                    _is_recurring_failure_class,
                )
                from app.task_lifecycle import _derive_failure_stage
                from app import shared_state as _ss

                error_msg = _prior.latest_status_message or ""
                prior_retry_ctx["_previous_error_fingerprint"] = _error_fingerprint(
                    error_msg
                )
                if failure_class_val:
                    stage = _derive_failure_stage(failure_class_val, error_msg)
                    if stage:
                        prior_retry_ctx["_previous_failure_stage"] = stage
                    if failure_class_val == "CODE_ERROR":
                        prior_retry_ctx["_previous_error_summary"] = error_msg[:500]
                    proj_anomalies = _ss.persistent_anomalies.get(
                        str(_prior.project_id), {}
                    )
                    if _is_recurring_failure_class(proj_anomalies, failure_class_val):
                        prior_retry_ctx["_previous_is_recurring"] = True
            except Exception:
                logger.exception(
                    "[RESEARCH_COMPLETION] Failed to enrich prior_retry_ctx for finding %s",
                    fhash,
                )
            if _prior_ctx.get("_analysis_plan"):
                prior_retry_ctx["_previous_analysis_plan"] = _prior_ctx[
                    "_analysis_plan"
                ]
            if _prior_ctx.get("_pr_changed_files"):
                prior_retry_ctx["_previous_changed_files"] = _prior_ctx[
                    "_pr_changed_files"
                ]
            if _prior_ctx.get("_pr_diff_summary"):
                prior_retry_ctx["_previous_diff_summary"] = _prior_ctx[
                    "_pr_diff_summary"
                ]
            if _prior_ctx.get("_pr_url"):
                prior_retry_ctx["_previous_pr_url"] = _prior_ctx["_pr_url"]
            if _prior_ctx.get("_test_failure_summary"):
                prior_retry_ctx["_previous_test_failure_summary"] = _prior_ctx[
                    "_test_failure_summary"
                ]
            if _prior_ctx.get("_self_review_outcome"):
                prior_retry_ctx["_previous_self_review_outcome"] = _prior_ctx[
                    "_self_review_outcome"
                ]
            if _prior_ctx.get("_file_change_details"):
                prior_retry_ctx["_previous_file_change_details"] = _prior_ctx[
                    "_file_change_details"
                ]
            # Tighten analysis plan file limit for medium-scope retries whose
            # prior attempt tripped a scope gate or touched ≥80% of the medium
            # file threshold. The agent reads _analysis_plan_file_limit and
            # overrides MEDIUM_ANALYSIS_PLAN_FILE_LIMIT (default 7) so the
            # retry plans a narrower change set.
            if scope == "medium":
                _prior_scope_gate = _prior_ctx.get("scope_gate_triggered")
                _prior_changed = _prior_ctx.get("_pr_changed_files") or []
                _prior_file_count = (
                    len(_prior_changed) if isinstance(_prior_changed, list) else 0
                )
                _tight_triggers = {
                    "scope_trim",
                    "analysis_overflow",
                    "post_review_overflow",
                    "small_overflow",
                }
                if _prior_scope_gate in _tight_triggers or _prior_file_count >= 8:
                    prior_retry_ctx["_analysis_plan_file_limit"] = 5
            # Generate failure-class-specific retry guidance, mirroring
            # task_lifecycle._requeue_parent_task so re-spawned findings get the
            # same tailored prompt_suffix as directly requeued retries.
            try:
                from app.retry_prompt import _build_retry_prompt_suffix

                _retry_prompt_suffix, _scope_override = _build_retry_prompt_suffix(
                    failure_class_val,
                    _prior.latest_status_message,
                    prior_retry_ctx["_attempt_number"],
                    scope,
                    prior_retry_ctx,
                )
                if _scope_override:
                    scope = _scope_override
                    prior_retry_ctx["_scope_reduced"] = True
            except Exception:
                logger.exception(
                    "[RESEARCH_COMPLETION] Failed to build retry prompt_suffix for finding %s",
                    fhash,
                )
        _combined_suffix = _retry_prompt_suffix
        if type_suffix:
            _combined_suffix = (
                _combined_suffix + ("\n" if _combined_suffix else "") + type_suffix
            )
        # When the direction carries active acceptance criteria, inject a
        # criterion_keys warning into the spawned task's prompt_suffix so the
        # implementing agent knows findings must declare named_test evidence.
        # Without this upfront signal the research agent unknowingly omits the
        # evidence field and the spawn loop hard-rejects the finding, wasting
        # one full research cycle per occurrence.
        _suffix_crit_dir_id = direction_id or _parent_direction_id
        _suffix_criterion_ids = _direction_criterion_cache.get(
            _suffix_crit_dir_id or "", []
        )
        if _suffix_criterion_ids:
            _criterion_suffix = (
                "\n\nCRITICAL: This direction has active acceptance criteria. "
                "Every finding must declare acceptance_evidence with "
                "kind 'named_test' and a test_path naming the test that "
                "verifies the change. Findings without this evidence are "
                "rejected at spawn time.\n"
                'Include in each finding:\n'
                '  acceptance_evidence: {kind: "named_test", test_path: "path/to/test.py"}'
            )
            _combined_suffix = (
                _combined_suffix + _criterion_suffix
                if _combined_suffix
                else _criterion_suffix.lstrip("\n")
            )
        # Scope-aware effort override for implementation task types: the scope
        # (calibrated through history/heuristic/low-confidence/retry) drives the
        # baseline effort — small→low, medium→medium, large→high — replacing
        # the coarse type-level config default. Explicit finding_effort still
        # wins when it ranks higher.
        if task_type in _SCOPE_AWARE_EFFORT_TYPES:
            _scope_effort = scope_to_effort(scope)
            _finding_explicit = finding.get("effort")
            if _finding_explicit and _effort_rank.get(
                _finding_explicit, 0
            ) > _effort_rank.get(_scope_effort, 0):
                finding_effort = _finding_explicit
            else:
                finding_effort = _scope_effort
            # Complexity-aware boost: complex tasks (low P(merge) complexity
            # score) get elevated effort so the agent has deeper inference to
            # navigate the complexity without requiring a scope upgrade.
            _cscore = finding.get("_complexity_score")
            if (
                _cscore is not None
                and _cscore < 0.4
                and _effort_rank.get(finding_effort, 0) < _effort_rank["high"]
            ):
                finding_effort = "high"
        # Predicted file-budget utilization for medium-scope tasks: gives the
        # implementing agent early visibility into budget pressure before it
        # commits to a broad approach. Complements the review-pipeline's runtime
        # pressure system (which fires at 70%/90% after changes are made).
        _predicted_utilization: float | None = None
        _file_budget_warning: str | None = None
        if scope == "medium" and related_files:
            _util_ratio = (
                _avg_medium_expansion_ratio([_pred_direction])
                if _pred_direction
                else None
            )
            if _util_ratio is None:
                _util_ratio = _avg_medium_expansion_ratio(
                    list(_directions_by_id.values())
                )
            if _util_ratio is None:
                _util_ratio = 2.0
            _medium_budget = (
                SCOPE_FILE_BUDGETS.get("medium", {}).get("total_files_max") or 10
            )
            _predicted_utilization = round(
                len(related_files) * _util_ratio / _medium_budget, 3
            )
            if _predicted_utilization > 0.7:
                _file_budget_warning = (
                    f"Predicted file-budget utilization is {_predicted_utilization:.0%} of the "
                    f"medium-scope limit ({_medium_budget} files). Plan conservatively: prioritize "
                    f"the most critical files from related_files and avoid incidental edits that "
                    f"would push the change past the budget."
                )
        from app import shared_state as _ss_pc

        _project_anomalies = _ss_pc.persistent_anomalies.get(str(task.project_id))
        _preventive = _build_preventive_context(
            finding,
            _directions_by_id,
            _project_anomalies,
        )

        # Surface 'what has compounded here' from prior playbook/success-* entries.
        # Additive to the existing scope-bias and design_doc playbook injection.
        if task_type in RECENT_SUCCESSES_TASK_TYPES and _scope_cal_project is not None:
            _recent = summarize_recent_successes(
                _scope_cal_project.design_doc or {},
                task_type,
                _pred_dir_id,
                scope,
            )
            if _recent:
                full_prompt = full_prompt + "\n\n" + _recent

        _payload_acceptance_evidence, _context_acceptance_evidence = (
            normalize_acceptance_evidence(finding.get("acceptance_evidence"))
        )

        # Reject criterion-carrying findings that lack named_test acceptance
        # evidence. The spawned task inherits criterion_ids from the direction,
        # but without a named_test declaration the acceptance pipeline records 0
        # proven-verdict rows, leaving criterion coverage perpetually uncovered.
        # PR #13528 added a non-blocking provenance warning here; the direction's
        # retirement condition requires a hard rejection so the spawn is blocked
        # and the finding never becomes work that cannot close the coverage loop.
        _crit_eff_dir_id = direction_id or parent_direction_id
        _crit_criterion_ids = _direction_criterion_cache.get(
            _crit_eff_dir_id or "", []
        )
        if _crit_criterion_ids:
            _has_named_test = (
                _payload_acceptance_evidence is not None
                and _payload_acceptance_evidence.get("kind") == "named_test"
            )
            if not _has_named_test:
                _criterion_evidence_rejected.append(finding)
                logger.warning(
                    "[RESEARCH] Rejecting finding %r (task %s): criterion-carrying "
                    "direction %s but finding lacks acceptance_evidence:{kind:named_test}; "
                    "not spawning.",
                    finding.get("title") or finding.get("description", "")[:60],
                    task.id,
                    _crit_eff_dir_id,
                )
                continue

        if task_type == "compute_experiment":
            # Build + validate the compute_experiment payload from the launch
            # contract authored above (already validated non-empty command /
            # positive spend cap / positive deadline). Proposed spend + deadline
            # are present in the payload so they are operator-visible; the
            # handler+metric gate read budget/deadline kills as intentional
            # terminal outcomes unchanged.
            _cc = _compute_contract or {}
            finding_payload = validate_internal_payload(
                {
                    "type": "compute_experiment",
                    "command": _cc["command"],
                    "spend_cap_usd": float(_cc["spend_cap_usd"]),
                    "deadline_seconds": int(_cc["deadline_seconds"]),
                    **(
                        {"success_criterion": _cc["success_criterion"]}
                        if _cc.get("success_criterion") is not None
                        else {}
                    ),
                    **{
                        k: _cc[k]
                        for k in (
                            "machine_type",
                            "experiment",
                            "benchmark",
                            "metric",
                            "hypothesis",
                            "cwd",
                            "env",
                            "accelerator",
                            "provisioning_model",
                            "live_proof",
                            "live_proof_marker",
                            "live_release_proof",
                            "live_release_proof_marker",
                            "requires_live_release",
                            "require_durable_artifact",
                            "sweep_id",
                            "setup_step",
                            "no_progress_timeout_seconds",
                            "input_artifact_job_id",
                            "container_image",
                            "container_image_digest_pinned",
                            "container_registry_auth",
                            "declared_metric",
                            "arms",
                            "smoke_sample",
                        )
                        if _cc.get(k) is not None
                    },
                    **({"model": finding_model} if finding_model else {}),
                    **({"effort": finding_effort} if finding_effort else {}),
                    **(
                        {"acceptance_evidence": _payload_acceptance_evidence}
                        if _payload_acceptance_evidence
                        else {}
                    ),
                },
                "research_completion:finding:compute_experiment",
            )
        else:
            # For knowledge task types (web_research / design), stamp the
            # dedup key fields (topic + direction_id) onto the payload so the
            # knowledge_task_in_flight guard can detect duplicate commissions
            # from concurrent research cycles.  Callers that already set topic
            # explicitly (e.g. strategic spawner) are unaffected.
            _knowledge_payload_extras: dict = {}
            if task_type in ("web_research", "design"):
                _ktopic = finding.get("topic") or title
                _kdir = direction_id or (task.context or {}).get("direction_id")
                if _ktopic:
                    _knowledge_payload_extras["topic"] = _ktopic
                if _kdir:
                    _knowledge_payload_extras["direction_id"] = _kdir
            finding_payload = validate_internal_payload(
                {
                    "type": task_type,
                    "prompt": full_prompt,
                    "base_branch": research_branch,
                    **({"model": finding_model} if finding_model else {}),
                    **({"effort": finding_effort} if finding_effort else {}),
                    **({"prompt_suffix": _combined_suffix} if _combined_suffix else {}),
                    **(
                        {"acceptance_evidence": _payload_acceptance_evidence}
                        if _payload_acceptance_evidence
                        else {}
                    ),
                    # Additive, optional shared-vocabulary field: the canonical
                    # registry capability names (with statuses) the planning /
                    # knowledge handlers render so the child draws prerequisite /
                    # provides_capability refs from the same namespace. Older
                    # agents ignore the field and still validate.
                    **(
                        {"available_capabilities": _registry_vocab}
                        if _registry_vocab and task_type in _VOCAB_RENDERING_TASK_TYPES
                        else {}
                    ),
                    **_knowledge_payload_extras,
                },
                f"research_completion:finding:{task_type}",
            )
        # Audit W1: previous_error_fingerprint is a column. Pull it out of
        # prior_retry_ctx so it lands on the column, not in JSONB context.
        _prev_fp_col = prior_retry_ctx.pop("_previous_error_fingerprint", None)
        # Cited paths the finding references — propagated so the launch-time
        # staleness recompute can diff them against the current integration HEAD.
        _child_cited_paths = finding_cited_paths(finding)
        # Optional plan-time acceptance-evidence contract. Read it straight off
        # the surviving ``finding`` dict (NOT routed through the
        # depends_on_indices remap), so it survives batch-cap truncation and
        # dedup for every finding that survives. Validated via the shared bridge:
        # an absent or malformed declaration yields None on each side and the
        # child context/payload stay byte-for-byte unchanged from today.
        _acceptance_evidence = _context_acceptance_evidence
        _finding_lineage = {
            str(v).strip().lower()
            for v in (
                fhash,
                finding.get("finding_hash"),
                _finding_hash(
                    str(finding.get("title", title) or ""),
                    _canonical_finding_body(finding),
                ),
            )
            if isinstance(v, str) and v.strip()
        }
        # Inherit criterion_ids from the direction this task works on (best-effort).
        # The cache was pre-fetched for the parent direction before the loop; a
        # finding-level direction_id falls back to the parent direction's criteria.
        _spawn_effective_dir_id = direction_id or parent_direction_id
        _spawn_criterion_ids = _direction_criterion_cache.get(
            _spawn_effective_dir_id or "", []
        )
        # Fall back to the parent task's own criterion_ids when the direction
        # cache has no entry (e.g. corroboration tasks that carry criterion_ids
        # directly on the Task row but have no direction_id). Without this
        # fallback, research children spawned from a corroboration task would
        # inherit empty criterion_ids and record_task_merge_evidence would
        # produce no evidence, defeating the attestation->covered upgrade.
        if not _spawn_criterion_ids and task.criterion_ids:
            _spawn_criterion_ids = list(task.criterion_ids)
        new_task = Task(
            project_id=task.project_id,
            title=title,
            description=finding.get("description", prompt[:200]),
            parent_task_id=task.id,
            payloads=[finding_payload],
            previous_error_fingerprint=_prev_fp_col,
            criterion_ids=_spawn_criterion_ids,
            context={
                "research_task_id": str(task.id),
                "parent_research_task_id": str(task.id),
                "finding_index": orig_i,
                "scope": scope,
                "risk": risk,
                "research_branch": research_branch,
                "original_base": original_base,
                "research_depth": research_depth + 1,
                "related_files": related_files,
                "claims": (finding.get("claims") or [])[:8],
                "finding_hash": fhash,
                "finding_lineage": sorted(_finding_lineage),
                **(
                    {"acceptance_evidence": _acceptance_evidence}
                    if _acceptance_evidence
                    else {}
                ),
                **(
                    {"provides_capability": finding["_provides_capability"]}
                    if finding.get("_provides_capability")
                    else {}
                ),
                **(
                    {"off_registry_capabilities": finding["_off_registry_capabilities"]}
                    if finding.get("_off_registry_capabilities")
                    else {}
                ),
                **(
                    {"direction_id": direction_id}
                    if direction_id
                    else (
                        {"direction_id": parent_direction_id}
                        if parent_direction_id
                        else {}
                    )
                ),
                **(
                    {"parent_direction_id": parent_direction_id}
                    if parent_direction_id
                    else {}
                ),
                **(
                    {
                        "criterion_release_version_id": str(
                            _criterion_release_version_id
                        )
                    }
                    if _criterion_release_version_id is not None
                    else {}
                ),
                **(
                    {"scope_calibrated_from": scope_calibrated_from}
                    if scope_calibrated_from
                    else {}
                ),
                **(
                    {
                        "complexity_score": finding["_complexity_score"],
                        "complexity_signals": finding.get("_complexity_signals", {}),
                    }
                    if "_complexity_score" in finding
                    else {}
                ),
                **(
                    {
                        "low_confidence_spawn": True,
                        "low_confidence_spawn_rate": round(_pred_p, 3),
                    }
                    if _low_confidence
                    else {}
                ),
                **({"medium_exploration": True} if _medium_exploration else {}),
                **({"auto_decomposed": True} if finding.get("auto_decomposed") else {}),
                **(
                    {"_provenance_warnings": finding["_provenance_warnings"]}
                    if finding.get("_provenance_warnings")
                    else {}
                ),
                **(
                    {
                        _FINDING_CHALLENGE_RECORD_KEY: finding[
                            _FINDING_CHALLENGE_RECORD_KEY
                        ]
                    }
                    if isinstance(finding.get(_FINDING_CHALLENGE_RECORD_KEY), dict)
                    else {}
                ),
                **(
                    {
                        # Baseline reproduce-on-HEAD verdict seeded from the
                        # already-computed citation-grounding signal (no new
                        # validation pass): a defect finding whose cited code is
                        # absent on HEAD definitionally does not reproduce there,
                        # so seed `already_resolved`; otherwise the defect-truth
                        # is not yet established, so seed `indeterminate`.
                        # Non-underscore key so the review/fix respawn
                        # carry-forward preserves it, and the agent overwrites it
                        # with a determined verdict on terminal status. See
                        # schemas.ReproduceVerdict.
                        "reproduce_verdict": {
                            "status": (
                                "already_resolved"
                                if (
                                    isinstance(
                                        finding.get("_provenance_warnings"), dict
                                    )
                                    and finding["_provenance_warnings"].get(
                                        "citation_state"
                                    )
                                    == "absent"
                                )
                                else "indeterminate"
                            ),
                            "reason": (
                                "seeded from research citation grounding: cited "
                                "code absent on HEAD"
                                if (
                                    isinstance(
                                        finding.get("_provenance_warnings"), dict
                                    )
                                    and finding["_provenance_warnings"].get(
                                        "citation_state"
                                    )
                                    == "absent"
                                )
                                else "seeded baseline: reproduce-on-HEAD not yet "
                                "determined"
                            ),
                        }
                    }
                    if finding_type in _REPRODUCE_VERDICT_FINDING_TYPES
                    else {}
                ),
                # Provenance the launch-time staleness recompute needs: the
                # integration HEAD the finding was generated against and the file
                # paths it cites. With these the orchestrator can re-check, at
                # dispatch, whether any cited file changed since generation against
                # the CURRENT branch tip (the ingestion-time warning is inert
                # because generated HEAD == ingestion HEAD). Only set when present.
                **(
                    {"generated_head_sha": finding["generated_head_sha"]}
                    if finding.get("generated_head_sha")
                    else {}
                ),
                **(
                    {"_finding_cited_paths": _child_cited_paths}
                    if _child_cited_paths
                    else {}
                ),
                **(
                    {"_predicted_file_utilization": _predicted_utilization}
                    if _predicted_utilization is not None
                    else {}
                ),
                **(
                    {"_file_budget_warning": _file_budget_warning}
                    if _file_budget_warning
                    else {}
                ),
                **(
                    {"scope_budget_warning": _scope_budget_warning}
                    if _scope_budget_warning
                    else {}
                ),
                # Record WHY this task is held out of dispatch when the
                # scope-budget gate set requires_approval=True: this is a
                # deliberate SPEND/BUDGET decision (large scope far over the
                # file budget needs operator confirmation), NOT a review-
                # pipeline escalation. The recovery sweep reads this marker to
                # apply the longer, operator-configurable window and resolve to
                # a clean non-failure CANCELLED terminal rather than 2h→FAILED.
                # Reuse SPEND_BUDGET_APPROVAL_HOLD_REASON for any future
                # spend-threshold hold (e.g. compute_experiment) — see the
                # marker-convention note in spawn_calibration.py.
                **(
                    {"approval_hold_reason": SPEND_BUDGET_APPROVAL_HOLD_REASON}
                    if _budget_requires_approval or _compute_requires_approval
                    else {}
                ),
                **(
                    {"preventive_context": _preventive, "has_preventive_context": True}
                    if _preventive
                    else {}
                ),
                "prompt_telemetry": _compute_prompt_telemetry(
                    full_prompt, related_files, scope
                ),
                **prior_retry_ctx,
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=needs_approval,
            depends_on=[],
            priority=finding_priority,
        )
        _spawn_gate_challenged = bool(
            _adversarial_verdict_raw is not None and _is_high_stakes_finding(finding)
        )
        _spawn_gate_verdict = (
            _ADVERSARIAL_INFRA_UNVERIFIED_VERDICT
            if _spawn_gate_challenged and finding.get("_adversarial_unverified")
            else (
                "pass"
                if _spawn_gate_challenged
                and finding.get("_adversarial_verdict") != "fail"
                else None
            )
        )
        new_task.context = {
            **(new_task.context or {}),
            "research_spawn_gate_challenged": _spawn_gate_challenged,
            "research_spawn_gate_verdict": _spawn_gate_verdict,
            "research_spawn_gate_reason": (
                finding.get("_adversarial_verdict_reason") or None
            )
            if _spawn_gate_challenged
            else None,
            "research_spawn_gate_explicit": (
                finding.get("_adversarial_verdict_explicit")
            )
            if _spawn_gate_challenged
            else None,
            "research_spawn_gate_unverified": (
                bool(finding.get("_adversarial_unverified"))
                if _spawn_gate_challenged
                else None
            ),
            "research_spawn_gate_unverified_reason": (
                finding.get("_adversarial_unverified_reason") or None
            )
            if _spawn_gate_challenged
            else None,
            "research_spawn_gate_cause": (
                finding.get("_adversarial_unverified_cause") or None
            )
            if _spawn_gate_challenged
            else None,
            "research_spawn_gate_parse_outcome": (
                finding.get("_adversarial_unverified_parse_outcome") or None
            )
            if _spawn_gate_challenged
            else None,
        }
        # Record the defect's reproduce-on-HEAD verdict on the spawned fix so its
        # outcome scoring can refuse bare-merge success when the target defect
        # was never established to reproduce. Absent findings are already gated
        # out above (not spawned), so this carries present/indeterminate only;
        # the helper no-ops for a missing/unrecognized verdict, preserving the
        # legacy bare-merge behavior for findings without a recorded verdict.
        record_target_defect_reproduction(
            new_task.context, finding.get("_reproduce_on_head")
        )
        session.add(new_task)
        if (
            task_type == "compute_experiment"
            and _compute_requires_approval
            and _compute_contract
        ):
            # Operator-visible reason naming proposed spend, deadline and the
            # threshold it crossed, so the existing approval queue + 2h timeout +
            # operator approve flow surface a held launch distinctly from a
            # running/completed one.
            new_task.latest_status_message = (
                "Held for operator approval: compute_experiment proposes "
                f"${float(_compute_contract['spend_cap_usd']):.2f} spend cap / "
                f"{int(_compute_contract['deadline_seconds'])}s deadline, exceeding "
                f"the ${_compute_threshold:.2f} approval threshold."
            )
        child_tasks.append(new_task)
        finding_indices.append(orig_i)
        _auto_decomposed_flags.append(bool(finding.get("auto_decomposed")))
        _impl_spawned += 1

    _now_iso = _utcnow().isoformat()

    def _stamp_deferral(f: dict, reason: str) -> dict:
        stamped = dict(f)
        stamped["deferred_count"] = int(stamped.get("deferred_count", 0)) + 1
        stamped["deferred_reason"] = reason
        stamped.setdefault("originally_researched_at", _now_iso)
        if not stamped.get("finding_hash"):
            stamped["finding_hash"] = _finding_hash(
                stamped.get("title", ""),
                _canonical_finding_body(stamped),
            )
        return stamped

    # Adversarially-deferred findings: stamp deferral + carry the challenger's
    # reason + clear the internal verdict keys so the finding re-enters the next
    # cycle clean.
    _adv_deferred_stamped = []
    # Carry challenger task ID from the diagnostic so operators can connect a
    # missing-verdict deferral to its challenger task without opening each one.
    _adv_diagnostic = (task.context or {}).get("adversarial_gate_diagnostic") or {}
    _adv_challenger_id = (
        str(
            _adv_diagnostic.get("challenger_task_id")
            or (
                _adversarial_verdict_raw.get("grader_task_id")
                if isinstance(_adversarial_verdict_raw, dict)
                else None
            )
            or ""
        ).strip()
        or None
    )
    # Machine-readable cause (taxonomy class / refusal / format-miss + optional
    # stale-image tag) for the grader non-response, lifted from the resolution-
    # time diagnostic so each deferred finding is attributable without opening
    # the challenger task. Observability only.
    _adv_cause = (
        _adv_diagnostic.get("cause") if isinstance(_adv_diagnostic, dict) else None
    ) or (
        _adversarial_verdict_raw.get("cause")
        if isinstance(_adversarial_verdict_raw, dict)
        else None
    )
    for _adf in _adversarial_gate_deferred:
        _adf_stamped = _stamp_deferral(_adf, "adversarial_gate")
        _adf_stamped.pop("_adversarial_verdict", None)
        _gate_reason = _adf_stamped.pop("_adversarial_verdict_reason", None)
        _adf_stamped.pop("_adversarial_verdict_explicit", None)
        if _gate_reason:
            _adf_stamped["adversarial_gate_reason"] = _gate_reason
        if _adv_challenger_id:
            _adf_stamped["adversarial_challenger_task_id"] = _adv_challenger_id
        if _adv_cause:
            _adf_stamped["adversarial_gate_cause"] = _adv_cause
        _adv_deferred_stamped.append(_adf_stamped)

    _all_deferred = (
        [_stamp_deferral(f, "low_actionability") for f in _low_actionability_deferred]
        + [
            _stamp_deferral(f, _LOW_ACTIONABILITY_REFINEMENT_REASON)
            for f in _low_actionability_refinement_deferred
        ]
        + [_stamp_deferral(f, "degradation_gate") for f in _degradation_gate_deferred]
        + [
            _stamp_deferral(f, "compute_contract_invalid")
            for f in _compute_contract_deferred
        ]
        + [_stamp_deferral(f, "batch_cap") for f in _batch_cap_deferred]
        + _adv_deferred_stamped
        + [
            {
                **_stamp_deferral(f, "ultra_low_confidence"),
                "skip_reason": "ultra_low_confidence",
            }
            for f in _low_conf_skipped_findings
        ]
    )
    # Spawn memo tasks for findings dropped for outcome-blind reasons
    # (low_actionability / degradation_gate / reproduce-on-HEAD rejection). These
    # would otherwise vanish leaving only an aggregate count; a terminal memo
    # records the reasoning — including, for a reproduce-on-HEAD rejection, WHICH
    # cited files/symbols were found absent — so strategic review can cite it by
    # id. Bounded by MEMO_SPAWN_CAP_PER_CYCLE, which is independent of the
    # implementation-task spawn caps (memos are read-only). A repeatedly-rejected
    # finding does not re-spawn: the duplicate-memo guard below keys on the
    # finding hash already recorded by an earlier cycle's memo.
    _spawned_memo_ids: list[str] = []
    _memo_candidates: list[tuple[dict, str]] = (
        [(f, "low_actionability") for f in _low_actionability_deferred]
        + [(f, "degradation_gate") for f in _degradation_gate_deferred]
        + [(f, _REPRODUCE_REJECTED_DROP_REASON) for f in _reproduce_rejected]
        + [
            (f, _CRITERION_EVIDENCE_REJECTED_DROP_REASON)
            for f in _criterion_evidence_rejected
        ]
    )
    if _finding_challenge_enabled(CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY):
        _memo_candidates.extend(
            [(f, "low_actionability_decay") for f in _low_actionability_decayed]
        )
    _research_direction_id = (task.context or {}).get("direction_id")
    _deferred_memo_enroll = bool(
        _runtime_config.get("evergreen_fill_deferred_memo_enrollment", False)
    )
    _memo_enrollment_queue: list[tuple[str, str, str | None, str | None]] = []
    for _mf, _drop_reason in _memo_candidates[:MEMO_SPAWN_CAP_PER_CYCLE]:
        _mf_hash = _mf.get("finding_hash") or _finding_hash(
            _mf.get("title", ""), _canonical_finding_body(_mf)
        )
        if _mf_hash in _existing_memo_finding_hashes:
            logger.debug(
                "[RESEARCH] Skipping duplicate memo for finding hash %s (task %s)",
                _mf_hash,
                task.id,
            )
            continue
        _memo_topic = str(
            _mf.get("title")
            or (_mf.get("description") or "")[:80]
            or "Dropped research finding"
        )[:200]
        _memo_dir_id = _mf.get("direction_id") or _research_direction_id
        _memo_payload = validate_internal_payload(
            {
                "type": "memo",
                "topic": _memo_topic,
                "reasoning_kind": "deferred_finding",
                "source_task_id": str(task.id),
                "source_finding": _trim_finding_for_memo(_mf),
                **({"direction_id": _memo_dir_id} if _memo_dir_id else {}),
            },
            "research_completion:memo:deferred_finding",
        )
        _memo_task = Task(
            project_id=task.project_id,
            title=f"Memo: {_memo_topic[:120]}",
            description=f"Reasoning memo for dropped finding ({_drop_reason})",
            parent_task_id=task.id,
            payloads=[_memo_payload],
            context={
                "memo_source_task_id": str(task.id),
                "memo_reasoning_kind": "deferred_finding",
                "memo_drop_reason": _drop_reason,
                "memo_source_finding_hash": _mf_hash,
                **({"direction_id": _memo_dir_id} if _memo_dir_id else {}),
            },
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            depends_on=[],
            priority=TASK_PRIORITY_MAP.get("low", 0),
        )
        session.add(_memo_task)
        try:
            from app.task_completion import _apply_synthesized_memo

            _synth = _synthesize_deferred_finding_memo(
                _memo_payload.get("source_finding") or {},
                _drop_reason,
                str(task.id),
            )
            if _synth:
                _apply_synthesized_memo(_memo_task, _synth, session)
            else:
                # Finding has no title or description — synthesize a minimal memo so
                # the task never provisions a VM for an effectively-empty payload.
                _fallback_memo = {
                    "summary": "Dropped finding (no identifying data)",
                    "reasoning": (
                        f"Reason: {_drop_reason}\n\n"
                        f"Source task: {task.id}\n\n"
                        "Finding had no title or description to synthesize from."
                    ),
                    "citations": [f"task:{task.id}"],
                    "tags": ["deferred-finding", _drop_reason],
                }
                _apply_synthesized_memo(_memo_task, _fallback_memo, session)
        except Exception:
            _memo_task.status = TaskStatus.FAILED
            raise
        _spawned_memo_ids.append(str(_memo_task.id))
        if _deferred_memo_enroll:
            _eq_detail = ((_mf.get("description") or "")[:1000]) or None
            _memo_enrollment_queue.append(
                (_mf_hash, _memo_topic, _eq_detail, _memo_dir_id)
            )
    if _spawned_memo_ids:
        logger.info(
            "[RESEARCH] Spawned %d memo task(s) for dropped findings (task %s)",
            len(_spawned_memo_ids),
            task.id,
        )
    if _memo_enrollment_queue:
        try:
            from app.evergreen_backlog import (  # noqa: PLC0415
                enroll_backlog_item as _enroll_backlog_item,
            )

            await session.flush()
            for _eq_hash, _eq_title, _eq_detail, _eq_dir_id in _memo_enrollment_queue:
                await _enroll_backlog_item(
                    session,
                    project_id=task.project_id,
                    finding_key=_eq_hash,
                    title=_eq_title,
                    detail=_eq_detail,
                    direction_id=_eq_dir_id,
                    provenance="deferred_memo",
                )
        except Exception:
            logger.warning(
                "[RESEARCH] deferred-memo evergreen enrollment failed"
                " (task %s, count %d)",
                task.id,
                len(_memo_enrollment_queue),
                exc_info=True,
            )

    # Spawn a consolidated salvage-bundle task if any findings were discarded
    # via decay (ultra_low_confidence >=4, low_actionability >=3, degradation_gate >=3,
    # adversarial_gate >=3, prereq_deferred >=3) or dedup filtering (hash match,
    # region overlap, file overlap, evidence overlap, retired approach, archived
    # diagnosis). This aggregates them into one
    # actionable task rather than only individual memo tasks or silent drops.
    if _salvage_candidates:
        _salvage_payload = _build_salvage_payload(_salvage_candidates, task.id)
        _salvage_task = Task(
            project_id=task.project_id,
            title=f"Salvage bundle: {len(_salvage_candidates)} discarded finding(s)",
            description=(
                f"Consolidated fix batch for {len(_salvage_candidates)} research finding(s) "
                "discarded via deduplication or deferral decay."
            ),
            parent_task_id=task.id,
            payloads=[_salvage_payload],
            context={
                "research_task_id": str(task.id),
                "salvage_source_task_id": str(task.id),
                "salvage_finding_count": len(_salvage_candidates),
                "audience": "strategist-internal",
            },
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            depends_on=[],
            priority=TASK_PRIORITY_MAP.get("medium", 50),
        )
        session.add(_salvage_task)
        # Record salvage activity on the parent research task context so
        # downstream consumers (strategist, operator, admin UI) can see
        # that salvage was triggered and trace to the spawned bundle.
        _salvage_ctx = dict(task.context or {})
        _salvage_ctx["salvage_activity"] = {
            "salvage_task_id": str(_salvage_task.id),
            "salvaged_count": len(_salvage_candidates),
            "salvaged_at": _utcnow().isoformat(),
        }
        task.context = _salvage_ctx
        flag_modified(task, "context")
        session.add(task)
        logger.info(
            "[RESEARCH] Spawned consolidated salvage-bundle task for %d discarded finding(s) (task %s)",
            len(_salvage_candidates),
            task.id,
        )

    _deferral_reasons: list[str] = []
    if _low_actionability_deferred:
        _deferral_reasons.append(
            f"low_actionability ({len(_low_actionability_deferred)})"
        )
    if _low_actionability_refinement_deferred:
        _deferral_reasons.append(
            "low_actionability refinement "
            f"({len(_low_actionability_refinement_deferred)})"
        )
    if _degradation_gate_deferred:
        _deferral_reasons.append(
            f"degradation gate ({_health_reason}; {len(_degradation_gate_deferred)})"
        )
    if _batch_cap_deferred:
        _deferral_reasons.append(f"batch cap ({len(_batch_cap_deferred)})")
    if _compute_contract_deferred:
        _deferral_reasons.append(
            f"compute_contract_invalid ({len(_compute_contract_deferred)})"
        )
    if _adversarial_gate_deferred:
        _deferral_reasons.append(
            f"adversarial_gate ({len(_adversarial_gate_deferred)})"
        )

    if (
        _all_deferred
        or _scope_skip_counts
        or _spawned_memo_ids
        or _finding_give_up
        or _reproduce_rejected
        or _criterion_evidence_rejected
        or _spawn_gate_challenged_count
        or _spawn_gate_manifest_excluded_count
    ):
        ctx = dict(task.context or {})
        if _all_deferred:
            ctx["deferred_findings"] = bound_deferred_findings(_all_deferred)
        if _finding_give_up:
            ctx["finding_structural_give_up"] = _finding_give_up
        if _scope_skip_counts:
            ctx["skipped_low_confidence_by_scope"] = _scope_skip_counts
        if _spawned_memo_ids:
            ctx["spawned_memo_ids"] = _spawned_memo_ids
        if _reproduce_rejected:
            # Machine-readable count of findings the reproduce-on-HEAD gate
            # rejected this cycle. Excluded from deferred_findings (no retry) and
            # from the surviving-finding signal (neutral, not success/failure).
            ctx["research_reproduce_rejected"] = len(_reproduce_rejected)
        if _criterion_evidence_rejected:
            # Machine-readable count of findings the criterion-evidence gate
            # rejected this cycle. Follows the same pattern as reproduce_rejected:
            # excluded from deferred_findings (no retry) and from the surviving-
            # finding signal (neutral).
            ctx["research_criterion_evidence_rejected"] = len(
                _criterion_evidence_rejected
            )
        if _spawn_gate_challenged_count:
            if _spawn_gate_healthy_cycle:
                for _retry_key in (
                    "research_spawn_gate_wholesale_retry_exhausted",
                    "research_spawn_gate_refusal_skip_retry",
                    "research_spawn_gate_last_wholesale_parse_outcome",
                    _ADVERSARIAL_WHOLESALE_RETRY_KEY,
                    _ADVERSARIAL_TARGETED_RETRY_KEY,
                    "_adversarial_original_challenged_hashes",
                ):
                    ctx.pop(_retry_key, None)
            ctx["research_spawn_gate_challenged_count"] = _spawn_gate_challenged_count
            ctx["research_spawn_gate_refuted_count"] = _spawn_gate_refuted_count
            ctx["research_spawn_gate_survived_count"] = _spawn_gate_survived_count
            # Decision-quality breakdown (additive; survived/refuted unchanged).
            ctx["research_spawn_gate_explicit_pass_count"] = (
                _spawn_gate_explicit_pass_count
            )
            ctx["research_spawn_gate_fail_open_count"] = _spawn_gate_fail_open_count
            ctx["research_spawn_gate_infra_fail_open_count"] = (
                _spawn_gate_infra_fail_open_count
            )
            ctx["research_spawn_gate_unresolved_count"] = _spawn_gate_unresolved_count
            if _spawn_gate_refuted_reasons:
                ctx["research_spawn_gate_refuted_reasons"] = _spawn_gate_refuted_reasons
            if _spawn_gate_parse_outcome:
                ctx["research_spawn_gate_parse_outcome"] = _spawn_gate_parse_outcome
            # Per-cycle machine-readable cause export (additive observability):
            # surfaces the grader non-response WHY (taxonomy class / refusal /
            # format-miss + optional stale-image tag) alongside the existing
            # spawn-gate metrics so a downstream consumer can plumb it into
            # research_history. `research_spawn_gate_cause` is the cause object;
            # `research_spawn_gate_cause_counts` maps its category to the number
            # of unresolved findings it accounts for this cycle.
            _cycle_cause = (ctx.get("adversarial_gate_diagnostic") or {}).get("cause")
            if _cycle_cause:
                ctx["research_spawn_gate_cause"] = _cycle_cause
                _cause_category = (
                    _cycle_cause.get("category")
                    if isinstance(_cycle_cause, dict)
                    else None
                )
                if _cause_category and _spawn_gate_unresolved_count > 0:
                    ctx["research_spawn_gate_cause_counts"] = {
                        _cause_category: _spawn_gate_unresolved_count
                    }
        if _spawn_gate_by_direction:
            ctx["research_spawn_gate_by_direction"] = _spawn_gate_by_direction
        if _spawn_gate_manifest_excluded_count:
            ctx["research_spawn_gate_manifest_excluded_fail_closed_count"] = (
                _spawn_gate_manifest_excluded_count
            )
            ctx["research_spawn_gate_manifest_excluded_fail_closed_samples"] = (
                _spawn_gate_manifest_excluded_samples
            )
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)
        _reason = " + ".join(_deferral_reasons) if _deferral_reasons else "unknown"
        logger.warning(
            f"[RESEARCH] Deferred {len(_all_deferred)} findings ({_reason}); "
            f"spawning {_impl_spawned} tasks. "
            f"Stored in task.context['deferred_findings'] for next cycle."
        )

    # Surface the cycle outcome on the task itself. Previously this lived only
    # in logs + task.context, leaving `latest_status_message` stuck at the last
    # agent heartbeat (e.g. "[15.7MB] Using tool: Bash") and giving no signal
    # in the admin UI / failures CLI when spawned-vs-filtered drifted.
    task.latest_status_message = _summarize_dispatch_outcome(
        spawned=_impl_spawned,
        total_findings=len(findings),
        dedup_outcome=_dedup_outcome,
        deferred_reason_parts=_deferral_reasons,
        degraded_reason=_capacity_degradation_reason,
    )
    # Structured indicator for downstream consumers: when a research task hit a
    # provider capacity/rate-limit error mid-run but still completed (did not
    # hard-fail), mark it so the strategist / operator / future research cycles
    # can distinguish thin-but-honest results from capacity-truncated ones.
    if _capacity_degradation_reason:
        _mark_capacity_degraded(task, _capacity_degradation_reason)
    # Honest success signal: count findings that SURVIVED ingestion validation +
    # dedup + actionability and were routed to work — spawned, or deferred only
    # for capacity/health reasons (batch cap, pipeline degradation). Findings
    # dropped as low-actionability / stale-duplicate / broken-citation, or
    # rejected by the reproduce-on-HEAD gate, did NOT survive and are excluded
    # (the gate uses `continue`, so they never reach _impl_spawned or either
    # capacity-deferred list), so a cycle whose findings were all rejected scores
    # neutral instead of a spurious success.
    _surviving_findings = (
        _impl_spawned + len(_batch_cap_deferred) + len(_degradation_gate_deferred)
    )
    _record_research_survival_signal(task, surviving=_surviving_findings)

    # Evergreen backlog adapter: if this research probe was spawned from the
    # evergreen ledger, transition its row from the probe's outcome. Children
    # still come ONLY from the surviving-finding gates above (_impl_spawned) —
    # this adapter never creates a fix/create task, it only records the verdict.
    _evergreen_item_id = (task.context or {}).get("evergreen_backlog_item_id")
    if _evergreen_item_id:
        try:
            from app.evergreen_backlog import apply_research_outcome

            await apply_research_outcome(
                session,
                item_id=str(_evergreen_item_id),
                impl_spawned=_impl_spawned,
                reproduce_rejected=len(_reproduce_rejected),
                surviving=_surviving_findings,
            )
        except Exception as _eg_exc:  # noqa: BLE001 — outcome adapter must not block completion
            logger.warning(
                "[EVERGREEN] failed to apply research outcome for item %s: %s",
                _evergreen_item_id,
                _eg_exc,
            )

    # Flip the parent to EVALUATING ATOMICALLY with the child-task INSERTs in the
    # commit below, so the durable post-spawn state is never COMPLETED-with-children.
    # The canonical flip (apply_post_spawn_state -> _apply_terminal_transition) runs
    # in a LATER commit, after depends_on / foundation wiring; a contention kill
    # (e.g. a 120s statement_timeout) between this commit and that one used to strand
    # the parent frozen at the agent-reported COMPLETED with live children — and
    # _check_and_trigger_rollup early-returns unless the parent is EVALUATING, so the
    # whole subtree stranded on the research branch (task 98249c8a, 2026-06-09). This
    # mirrors `plan`, which already flips EVALUATING before its child-spawn commit
    # (plan_completion.py). Gate on "this pass spawned children OR a prior pass already
    # owns them" — the same has_existing_children probe _apply_terminal_transition uses
    # at its terminal commit — so a genuine zero-children cycle still takes the legit
    # COMPLETED branch there. _apply_terminal_transition re-sets EVALUATING idempotently.
    _has_prior_children = False
    if not child_tasks:
        _has_prior_children = bool(
            (
                await session.execute(
                    select(Task.id).where(Task.parent_task_id == task.id).limit(1)
                )
            )
            .scalars()
            .first()
        )
    # Clear spawn-pending atomically with the child-task spawn commit.
    # Whether or not this pass spawned children, the spawn is completing
    # successfully and future passes should not use the resume path.
    _ctx_post = dict(task.context or {})
    _ctx_post.pop("_research_spawn_pending", None)
    task.context = _ctx_post
    flag_modified(task, "context")
    if child_tasks or _has_prior_children:
        task.status = TaskStatus.EVALUATING
        task.context = {
            **(task.context or {}),
            "research_branch": research_branch,
            "original_base": original_base,
        }
    session.add(task)

    # This commit flushes the child-task INSERTs — the collision point for the
    # duplicate-spawn guard when a peer already spawned this batch. The
    # EVALUATING flip above (when children exist) lands in the SAME commit, so
    # children + EVALUATING are durable together — never COMPLETED-with-children.
    if not await _commit_or_handle_duplicate_spawn(
        session, task, stage="child-task spawn"
    ):
        return
    # The self-claim recovery path inside _commit_or_handle_duplicate_spawn
    # rolls the session back before re-committing, which expires EVERY loaded
    # ORM instance (rollback expires even with expire_on_commit=False). The
    # helper re-populates ``task`` itself, but the other instances this
    # function keeps reading below (project_obj, the dedup scans) would
    # otherwise lazy-load synchronously on the next attribute access —
    # MissingGreenlet under the async engine. Detect the rollback via the
    # ``expired`` flag (no IO) and batch re-populate in place;
    # ``populate_existing`` refreshes the SAME identity-map instances, so
    # every existing reference becomes live again.
    from sqlalchemy import inspect as _sa_inspect

    with session.no_autoflush:
        if project_obj is not None and _sa_inspect(project_obj).expired:
            await session.execute(
                select(Project)
                .where(Project.id == _sa_inspect(project_obj).identity[0])
                .execution_options(populate_existing=True)
            )
        _stale_scan_ids = [
            _sa_inspect(t).identity[0]
            for t in (*existing_tasks, *recent_tasks_for_dedup)
            if _sa_inspect(t).expired
        ]
        if _stale_scan_ids:
            await session.execute(
                select(Task)
                .where(Task.id.in_(_stale_scan_ids))
                .execution_options(populate_existing=True)
            )
    # Batch-reload all child tasks (N+1 guard: commit above expired the
    # session; one batch SELECT + identity-map lookups replaces N individual
    # session.refresh calls). Build a fresh map so the downstream code that
    # accesses attributes on child_tasks entries gets non-expired instances.
    if child_tasks:
        _ct_ids = [ct.id for ct in child_tasks]
        _ct_result = await session.execute(
            select(Task).where(Task.id.in_(_ct_ids))
        )
        _ct_fresh = {str(t.id): t for t in _ct_result.scalars().all()}
        for _i, _ct in enumerate(child_tasks):
            _fresh = _ct_fresh.get(str(_ct.id))
            if _fresh is not None:
                child_tasks[_i] = _fresh

    # Resolve explicit depends_on_indices to actual UUIDs (agent-specified inter-finding deps only)
    finding_to_child = {fi: ci for ci, fi in enumerate(finding_indices)}
    for ci, ct in enumerate(child_tasks):
        fi = finding_indices[ci]
        dep_indices = findings[fi].get("depends_on_indices", [])
        explicit_deps = [
            str(child_tasks[finding_to_child[j]].id)
            for j in dep_indices
            if j in finding_to_child
        ]
        if explicit_deps:
            ct.depends_on = explicit_deps
            remember_structural_depends_on(ct, explicit_deps)
            session.add(ct)

    # Foundation-aware cross-task wiring: when a finding declares an EXPLICIT
    # {kind, ref} prerequisite, resolve it against (a) tasks already active /
    # queued / in-flight in this project and (b) the sibling tasks just spawned
    # in this batch, then append the matched foundation's id to depends_on. This
    # lets a finding that requires a shared foundation depend on the single task
    # already building it — in-flight or a sibling — instead of each dependent
    # rebuilding the foundation. Conservative: explicit references only (see
    # resolve_prerequisite_task); never overwrites the explicit_deps above.
    # Any edge is acyclicity-checked with the SAME predicate the runtime
    # need-resolution path applies (blocked_need_resolution._would_create_cycle),
    # so a declared prerequisite that would close a depends_on cycle is refused at
    # the structural source rather than deferred to the dispatch-time cycle breaker.
    from app.blocked_need_resolution import _would_create_cycle
    from app.capability_registry import (
        consumer_gate_proof_status,
        registry_satisfies_capability_need,
        task_backed_capability_need_blocked_by_proof_gate,
        working_capability_names,
    )
    from app.shared_state import runtime_config as _rc
    from app.task_blocked import build_need, enter_blocked_state

    _prereq_candidates = list(existing_tasks) + list(child_tasks)
    # Capabilities the strategist registry already declares ``working`` exist in
    # the codebase itself: a prerequisite naming one is satisfied by the repo, so
    # it must neither wire an edge nor block the dependent. Without this, a
    # finding that honestly declares "builds on existing capability X" parks
    # BLOCKED on a need no task will ever advertise (providers are stamped only
    # while X is being BUILT), rides the recheck sweep to the escalation deadline,
    # and lands on the operator — for work that was runnable all along.
    _working_registry_caps = working_capability_names(
        getattr(project_obj, "problem_model", None)
    )
    # Proof-status companion of the working-capability set, built from the SAME
    # registry so an unproven "declared working with zero evidence" resolution can
    # be surfaced.  In OBSERVE mode this map drives a WARNING log below and does
    # NOT change which needs resolve; in ENFORCE mode (checked below via
    # capability_need_blocked_by_proof_gate) an unproven capability need DOES
    # block the dependent — the enforce gate is implemented and active.
    #
    # SECURITY (audit finding #4): graded through consumer_gate_proof_status, NOT
    # from working_capability_proof_status alone. The latter reads only
    # problem_model.capabilities[].evidence_refs / proof_exempt — AGENT-WRITABLE
    # JSONB — so a task could write itself a {"kind":"named_test","proven":true}
    # ref and clear this gate for its own dependents with nothing verified. The
    # shared grader corroborates every ``proven`` claim against
    # project.capability_proof_records (the platform-only ledger), exactly as the
    # release-qualifier seam does; only a provenance-authorized proof_exempt clears
    # without a ledger entry.
    _working_registry_proof = consumer_gate_proof_status(
        getattr(project_obj, "problem_model", None),
        getattr(project_obj, "capability_proof_records", None),
    )
    # Prerequisites already satisfied by SHIPPED prior-cycle work or a durable
    # knowledge document are resolved at spawn time too — the completed-foundation
    # and knowledge rungs the BLOCKED-resume sweep applies
    # (reaper_sweeps._resolve_blocked_task_needs) but the spawn-time provider
    # universe (active tasks + same-cycle siblings, built above) OMITS. Without
    # them a task-kind prerequisite whose only provider is a COMPLETED+merged
    # foundation is mis-routed to CANCEL ("no_live_or_deferred_provider"), so the
    # dependent — runnable all along — is permanently discarded and deterministically
    # re-cancelled on every re-discovery. recent_tasks_for_dedup already holds the
    # 90-day same-project COMPLETED rows; completed_foundation_for_need re-applies
    # the shipped gate (pr_merged + non-ghost via failed_parent_ids).
    from app.blocked_need_resolution import (
        completed_foundation_for_need,
        knowledge_substance_satisfies_need,
    )
    from app.knowledge_query import fetch_knowledge_substance_index

    _completed_foundations = [
        t for t in recent_tasks_for_dedup if t.status == TaskStatus.COMPLETED
    ]
    _knowledge_substance_index = await fetch_knowledge_substance_index(
        session, task.project_id
    )
    # Identities (kind, ref) that a finding deferred THIS cycle would satisfy. A
    # dependent blocked on one of these sits live-blocked behind dormant data: the
    # deferred foundation is not a real task, so nothing can satisfy the need until
    # it is both re-surfaced AND re-spawned in some future cycle. If that re-surface
    # keeps getting throttled (decay / capacity), the dependent exhausts its bounded
    # blocked re-evaluation budget and is auto-cancelled, discarding researched
    # work. Such a dependent is co-deferred with its prerequisite (below) so the two
    # re-surface as a unit instead of being left live.
    _deferred_identities: set[tuple] = set()
    for _df in _all_deferred:
        _dfh = _df.get("finding_hash")
        if _dfh:
            _deferred_identities.add(("task", _dfh))
        _dcap = _df.get("_provides_capability")
        if isinstance(_dcap, str) and _dcap.strip():
            _deferred_identities.add(("capability", _dcap.strip()))
    _codeferred_findings: list[dict] = []

    def _wire_prerequisite_edge(
        dependent: Task,
        foundation: Task,
        *,
        finding_title: str,
        prereq_label: str,
    ) -> bool:
        if foundation.id == dependent.id:
            return False
        if _would_create_cycle(dependent, foundation, _prereq_candidates):
            logger.warning(
                "[RESEARCH] Finding '%s' prerequisite %s resolved to foundation %s "
                "but the edge would close a depends_on cycle — refused at spawn "
                "(no edge)",
                finding_title,
                prereq_label,
                foundation.id,
            )
            return False
        foundation_id = str(foundation.id)
        existing_dep = list(dependent.depends_on or [])
        if foundation_id not in existing_dep:
            existing_dep.append(foundation_id)
            dependent.depends_on = existing_dep
            remember_structural_depends_on(dependent, foundation_id)
            session.add(dependent)
            logger.info(
                "[RESEARCH] Wired finding '%s' depends_on foundation %s "
                "(prerequisite %s)",
                finding_title,
                foundation_id,
                prereq_label,
            )
        return True

    def _maybe_inherit_base(
        dependent: Task,
        foundation: Task,
        *,
        finding_title: str,
    ) -> None:
        """When *foundation* is a genuine cross-tree code-dep, update *dependent*'s
        base_branch to match so the pair is same-base from birth and avoids the
        cross-base landed-signal wait path entirely."""
        if not dependent.payloads:
            return
        from app.orchestrator_health import (
            _same_base_branch,
            is_genuine_code_dependency,
        )

        if is_genuine_code_dependency(dependent, foundation) and not _same_base_branch(
            dependent, foundation
        ):
            foundation_base = (foundation.first_payload or {}).get("base_branch")
            if foundation_base is None:
                return
            dependent.payloads[0]["base_branch"] = foundation_base
            flag_modified(dependent, "payloads")
            session.add(dependent)
            logger.info(
                "[RESEARCH] Finding '%s' cross-tree genuine code-dep: set "
                "base_branch to %r (foundation %s)",
                finding_title,
                foundation_base,
                str(foundation.id),
            )

    for ci, ct in enumerate(child_tasks):
        fi = finding_indices[ci]
        finding_title = findings[fi].get("title", f"#{fi}")
        # Collect EVERY declared-but-unsatisfied prerequisite for this child into a
        # single set of machine-resolvable needs, so the dependent stays gated
        # until ALL of them are accounted for — not just the first. A single
        # dependent can have more than one unresolved prerequisite, and gating on
        # only the first one lets it resume (and rebuild the others) the moment
        # that first foundation lands. Two prerequisite sources contribute:
        #   (a) EVERY missing sibling finding named via depends_on_indices that was
        #       deduped/deferred this cycle (previously only missing_dep_indices[0]
        #       was gated); and
        #   (b) an explicit {kind, ref} prerequisite that did NOT resolve to an
        #       in-flight / sibling foundation (previously dropped whenever a
        #       sibling prerequisite was also missing — the `continue` short-circuit).
        # A prerequisite that DID resolve still gets a real depends_on edge wired
        # below (that path is correct); only the unresolved remainder becomes needs.
        needs: list = []
        unresolved_entries: list = []
        structurally_dead_entries: list = []
        # depends_on edges dropped because the named sibling was deduped away as a
        # duplicate of existing/covered work (recorded for observability; the
        # dependent spawns standalone — see the missing-sibling loop below).
        dropped_duplicate_prereqs: list = []

        dep_indices = findings[fi].get("depends_on_indices", [])
        missing_dep_indices = [j for j in dep_indices if j not in finding_to_child]
        for missing_fi in missing_dep_indices:
            if not (0 <= missing_fi < len(findings)):
                logger.warning(
                    "[RESEARCH] Finding '%s' depends_on_indices contains out-of-range "
                    "index %d (valid range: 0..%d); skipping",
                    finding_title,
                    missing_fi,
                    len(findings) - 1,
                )
                continue
            missing_finding = findings[missing_fi]
            missing_title = missing_finding.get("title", f"#{missing_fi}")
            missing_type = str(missing_finding.get("type") or "")
            missing_label = str(missing_title or "")
            if (
                missing_type in _NON_SPAWNING_FINDING_TYPES
                or missing_label.lower().startswith("obs:")
            ):
                structurally_dead_entries.append(
                    {
                        "finding_index": missing_fi,
                        "title": missing_title,
                        "type": missing_type or None,
                        "reason": "non_spawning_finding",
                    }
                )
                continue
            missing_hash = _finding_hash(
                str(missing_title),
                _canonical_finding_body(missing_finding),
            )
            lineage_need = {"kind": "task", "ref": missing_hash}
            same_batch_foundation = next(
                (
                    candidate
                    for candidate in child_tasks
                    if need_matches_task_lineage(lineage_need, candidate)
                ),
                None,
            )
            if same_batch_foundation is not None and same_batch_foundation.id != ct.id:
                _wire_prerequisite_edge(
                    ct,
                    same_batch_foundation,
                    finding_title=finding_title,
                    prereq_label=f"task:{missing_hash}",
                )
                continue
            # Not in same-batch spawns — check existing in-flight / recently-completed
            # foundations the same way the explicit-prerequisite path does. A sibling
            # dropped by deduplicate_findings (deduped against an existing task) leaves
            # its dependents with a missing_dep_indices entry whose hash matches that
            # existing task exactly. Wire an edge rather than creating a dead need.
            existing_foundation = resolve_prerequisite_task(
                lineage_need, _prereq_candidates
            )
            if existing_foundation is not None and existing_foundation.id != ct.id:
                _wired = _wire_prerequisite_edge(
                    ct,
                    existing_foundation,
                    finding_title=finding_title,
                    prereq_label=f"task:{missing_hash}",
                )
                if _wired:
                    _maybe_inherit_base(
                        ct, existing_foundation, finding_title=finding_title
                    )
                continue
            # The named sibling survived parsing into `findings` but was dropped
            # from the SPAWN set by deduplicate_findings — it duplicates work that
            # already exists, is in-flight, or has shipped. resolve_prerequisite_task
            # could not wire an edge because the duplicate is matched fuzzily (title /
            # files / evidence), not by the exact finding_hash this loop recomputes,
            # so the two identities diverge across independent research runs. The
            # foundation IS covered, though, so the declared edge is ORPHANED, not a
            # real unmet dependency: drop it and let the dependent spawn standalone
            # rather than cancelling it as a dead need (no_live_or_deferred_provider)
            # and failing the whole research task. Serialization against the
            # duplicate was only a merge-order optimization the pipeline tolerates.
            if missing_hash in _deduped_dropped_hashes:
                logger.info(
                    "[RESEARCH] Finding '%s' depends_on sibling '%s' that was deduped "
                    "as a duplicate of existing/covered work — dropping the orphaned "
                    "edge and spawning standalone (no dead need)",
                    finding_title,
                    missing_title,
                )
                dropped_duplicate_prereqs.append(
                    {
                        "finding_index": missing_fi,
                        "title": missing_title,
                        "finding_hash": missing_hash,
                    }
                )
                continue
            unresolved_entries.append(
                {
                    "finding_index": missing_fi,
                    "title": missing_title,
                    "finding_hash": missing_hash,
                }
            )
            sibling_need = build_need("task", missing_hash, label=missing_title)
            if sibling_need:
                needs.append(sibling_need)

        # Stamp the orphaned-edge drops on the child context for observability:
        # operators can see WHY a declared depends_on edge is absent (its sibling
        # was a duplicate) without re-deriving it from logs.
        if dropped_duplicate_prereqs:
            _dd_ctx = dict(ct.context or {})
            _dd_ctx["_spawn_prereq_dropped_duplicate"] = (
                list(_dd_ctx.get("_spawn_prereq_dropped_duplicate") or [])
                + dropped_duplicate_prereqs
            )
            ct.context = _dd_ctx
            flag_modified(ct, "context")
            session.add(ct)

        # Explicit {kind, ref} prerequisite: wire a real edge if it resolved to an
        # in-flight / sibling foundation this cycle, otherwise add it to the needs
        # set. This branch is NO LONGER short-circuited when a sibling prerequisite
        # is also missing, so an explicit prerequisite is never silently dropped.
        prereq = findings[fi].get("_prerequisite")
        if prereq:
            foundation = resolve_prerequisite_task(prereq, _prereq_candidates)
            if foundation is not None and foundation.id != ct.id:
                prereq_need = build_need(prereq.get("kind"), prereq.get("ref"), label=prereq.get("label"))
                if prereq_need and task_backed_capability_need_blocked_by_proof_gate(
                    prereq_need, getattr(project_obj, "capability_proof_records", None),
                    getattr(project_obj, "problem_model", None),
                    mode=str(_rc.get("capability_proof_gate_mode", "off") or "off"),
                ):
                    needs.append(prereq_need)
                    unresolved_entries.append(prereq)
                else:
                    _wired = _wire_prerequisite_edge(
                        ct, foundation, finding_title=finding_title,
                        prereq_label=f"{prereq.get('kind')}:{prereq.get('ref')}",
                    )
                    if _wired:
                        _maybe_inherit_base(ct, foundation, finding_title=finding_title)
            elif foundation is None:
                # Unresolved explicit prerequisite: a finding declared an explicit
                # {kind, ref} foundation it must build on, but no in-flight or
                # sibling task matched. Add it to the needs set so the dependent is
                # gated (BOTH task and capability kinds trigger blocking; the
                # blocked-resume sweep wires the edge when the foundation appears,
                # resumes with no edge when it has already landed, or escalates).
                prereq_need = build_need(
                    prereq.get("kind"),
                    prereq.get("ref"),
                    label=prereq.get("label"),
                )
                if prereq_need and registry_satisfies_capability_need(
                    prereq_need, _working_registry_caps
                ):
                    _disposition, _proof_status = spawn_capability_gate_disposition(
                        prereq_need,
                        _working_registry_proof,
                        mode=str(_rc.get("capability_proof_gate_mode", "off") or "off"),
                    )
                    if _disposition == "block":
                        # ENFORCE mode: an unproven/partial 'working' capability
                        # does NOT satisfy the prerequisite. Gate the dependent —
                        # wire the need as blocking exactly as an unresolved
                        # explicit prerequisite would, so the task parks BLOCKED
                        # instead of spawning on an unproven codebase foundation.
                        logger.warning(
                            "[RESEARCH][capability-proof-enforce] Finding '%s' "
                            "prerequisite capability:%s NOT satisfied — the "
                            "registry capability is 'working' with proof_status=%s "
                            "(unproven); enforce mode gates the dependent BLOCKED",
                            finding_title,
                            prereq.get("ref"),
                            _proof_status,
                        )
                        needs.append(prereq_need)
                        unresolved_entries.append(prereq)
                    elif _disposition == "surface":
                        # OBSERVE/OFF mode: surface (never gate) a resolution backed
                        # by a capability declared 'working' with no proven evidence.
                        # The need still resolves as satisfied-by-the-codebase; this
                        # makes the unproven claim visible while enforce is off —
                        # both as a WARNING log AND, so an operator sees it without
                        # re-deriving it from logs, as a durable marker on the
                        # spawned dependent's context (mirroring the
                        # ``_spawn_prereq_dropped_duplicate`` surfacing above).
                        logger.warning(
                            "[RESEARCH][capability-proof-observe] Finding '%s' "
                            "prerequisite capability:%s resolved as "
                            "satisfied-by-the-codebase, but the registry "
                            "capability is 'working' with proof_status=%s — "
                            "unproven resolution (observe mode, not gated)",
                            finding_title,
                            prereq.get("ref"),
                            _proof_status,
                        )
                        _uv_ctx = dict(ct.context or {})
                        _uv_ctx[SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY] = [
                            *(_uv_ctx.get(SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY) or []),
                            {
                                "kind": prereq_need.get("kind"),
                                "ref": prereq_need.get("ref"),
                                "proof_status": _proof_status,
                            },
                        ]
                        ct.context = _uv_ctx
                        flag_modified(ct, "context")
                        session.add(ct)
                    else:
                        logger.info(
                            "[RESEARCH] Finding '%s' prerequisite capability:%s is "
                            "already 'working' per the problem_model registry — "
                            "satisfied by the codebase, no gate",
                            finding_title,
                            prereq.get("ref"),
                        )
                elif prereq_need:
                    needs.append(prereq_need)
                    unresolved_entries.append(prereq)
            # foundation == ct itself (self-prerequisite): neither edge nor need.

        # Deduplicate needs by (kind, ref) to avoid recording duplicate entries.
        # When a dependent names the same missing sibling more than once, or an
        # explicit prerequisite that coincides with a missing sibling, we should
        # record only one need for that (kind, ref) pair.
        if needs:
            seen_keys = set()
            deduped_needs = []
            for need in needs:
                key = (need.get("kind"), need.get("ref"))
                if key not in seen_keys:
                    seen_keys.add(key)
                    deduped_needs.append(need)
            needs = deduped_needs

        # Drop needs already satisfied by a SHIPPED COMPLETED foundation or a
        # durable knowledge document — the completed-foundation/knowledge rungs the
        # spawn-time provider universe omits (see _completed_foundations /
        # _knowledge_substance_index above). A task-kind need satisfied here was
        # runnable all along, so it must NOT cancel or block the dependent;
        # capability needs are already covered by the working registry and never
        # reach this filter. If this empties `needs`, the dependent spawns runnable
        # via the `if not needs` guard below (resume-with-no-edge semantics).
        if needs:
            _satisfied_keys = set()
            for _need in needs:
                if not (isinstance(_need, dict) and _need.get("kind") == "task"):
                    continue
                if completed_foundation_for_need(
                    ct, _need, _completed_foundations, failed_parent_ids
                ) is not None or knowledge_substance_satisfies_need(
                    _need, _knowledge_substance_index
                ):
                    _satisfied_keys.add((_need.get("kind"), _need.get("ref")))
            if _satisfied_keys:
                _resolved_records: list = []
                for _k in _satisfied_keys:
                    logger.info(
                        "[RESEARCH] Finding '%s' prerequisite %s:%s already satisfied "
                        "by shipped COMPLETED foundation or knowledge document — "
                        "resolved at spawn (no gate)",
                        finding_title,
                        _k[0],
                        _k[1],
                    )
                    _resolved_records.append({"kind": _k[0], "ref": _k[1]})
                # Stamp the resolved prerequisites on the child task context for
                # observability: operators can see which prereqs were auto-resolved at
                # spawn (vs. gated/blocked until the blocked-resume sweep fires).
                _rsp_ctx = dict(ct.context or {})
                _prev_resolved = list(_rsp_ctx.get("_spawn_prereq_resolved") or [])
                _rsp_ctx["_spawn_prereq_resolved"] = _prev_resolved + _resolved_records
                ct.context = _rsp_ctx
                flag_modified(ct, "context")
                session.add(ct)
                needs = [
                    n
                    for n in needs
                    if (n.get("kind"), n.get("ref")) not in _satisfied_keys
                ]

        if not needs:
            if structurally_dead_entries:
                ctx = dict(ct.context or {})
                ctx["unresolved_prerequisite"] = (
                    list(ctx.get("unresolved_prerequisite") or [])
                    + structurally_dead_entries
                )
                ctx["prerequisite_escalated_to_parent"] = True
                ctx["prerequisite_escalation_reason"] = "structurally_unresolvable"
                ct.context = ctx
                flag_modified(ct, "context")
                ct.status = TaskStatus.CANCELLED
                ct.latest_status_message = (
                    f"Escalated to parent: finding '{finding_title}' declared only "
                    "structurally unresolvable prerequisite(s)."
                )[:500]
                session.add(ct)
                logger.warning(
                    "[RESEARCH] Finding '%s' escalated to parent instead of BLOCKED "
                    "on structurally unresolvable prerequisite(s): %s",
                    finding_title,
                    structurally_dead_entries,
                )
            continue

        live_need_keys = {
            (n.get("kind"), n.get("ref"))
            for n in needs
            if isinstance(n, dict)
            and not (
                n.get("kind") == "task"
                and (n.get("kind"), n.get("ref")) not in _deferred_identities
            )
        }
        dead_task_needs = [
            n
            for n in needs
            if isinstance(n, dict)
            and n.get("kind") == "task"
            and (n.get("kind"), n.get("ref")) not in _deferred_identities
        ]
        if dead_task_needs and not live_need_keys:
            ctx = dict(ct.context or {})
            existing_unresolved = ctx.get("unresolved_prerequisite") or []
            if not isinstance(existing_unresolved, list):
                existing_unresolved = [existing_unresolved]
            existing_unresolved.extend(unresolved_entries)
            existing_unresolved.extend(structurally_dead_entries)
            ctx["unresolved_prerequisite"] = existing_unresolved
            ctx["prerequisite_escalated_to_parent"] = True
            ctx["prerequisite_escalation_reason"] = "no_live_or_deferred_provider"
            ct.context = ctx
            flag_modified(ct, "context")
            ct.status = TaskStatus.CANCELLED
            ct.latest_status_message = (
                f"Escalated to parent: finding '{finding_title}' declared task "
                "prerequisite(s) with no spawned, deferred, or in-flight provider."
            )[:500]
            session.add(ct)
            logger.warning(
                "[RESEARCH] Finding '%s' escalated to parent instead of BLOCKED "
                "on dead task prerequisite(s): %s",
                finding_title,
                ", ".join(f"{n['kind']}:{n['ref']}" for n in dead_task_needs),
            )
            continue

        # Co-defer a dependent whose foundation was deferred THIS cycle. Leaving it
        # live-blocked strands it behind dormant data (see _deferred_identities):
        # shelve its finding alongside the deferred prerequisite so the two
        # re-surface as a unit next cycle, and cancel the live task so it cannot
        # expire its blocked re-evaluation budget. Independent deferred findings
        # (no live dependent) keep the existing capacity/actionability throttle.
        if _deferred_identities and any(
            (n.get("kind"), n.get("ref")) in _deferred_identities for n in needs
        ):
            _codeferred_findings.append(
                _stamp_deferral(findings[fi], "prereq_deferred_same_cycle")
            )
            # Stamp a durable non-death marker so the death-semantics policy
            # (failure_classification.is_non_death_cancellation, consulted by the
            # resume system's dead-foundation detector and the dispatch-time
            # cascade-cancel classifier) recognizes this CANCELLED task as
            # recoverable shelving — NOT a genuine death. Without it, any blocked
            # dependent whose need matches this shelved task would be wrongly
            # terminated as "foundation died".
            co_ctx = dict(ct.context or {})
            co_ctx[CO_DEFERRED_SHELVED] = True
            ct.context = co_ctx
            flag_modified(ct, "context")
            ct.status = TaskStatus.CANCELLED
            ct.latest_status_message = (
                f"Co-deferred with same-cycle-deferred prerequisite: finding "
                f"'{finding_title}' shelved to re-surface with its foundation"
            )[:500]
            session.add(ct)
            logger.warning(
                "[RESEARCH] Co-deferring dependent '%s' with its same-cycle-deferred "
                "prerequisite (cancelled task %s; both re-surface next cycle)",
                finding_title,
                ct.id,
            )
            continue

        # Block the dependent ONCE on the FULL set of unresolved prerequisites.
        # enter_blocked_state persists the whole list under context['blocker']
        # ['needs'] (mirroring needs[0] into the legacy 'need' key), so the
        # blocked-resume sweep keeps it gated until every prerequisite resolves.
        ctx = dict(ct.context or {})
        existing_unresolved = ctx.get("unresolved_prerequisite") or []
        if not isinstance(existing_unresolved, list):
            existing_unresolved = [existing_unresolved]
        existing_unresolved.extend(unresolved_entries)
        ctx["unresolved_prerequisite"] = existing_unresolved
        ct.context = ctx
        primary = needs[0]
        waiting_on = f"foundation {primary['kind']}:{primary['ref']}"
        if len(needs) > 1:
            waiting_on = f"{waiting_on} (+{len(needs) - 1} more)"
        reason = (
            f"Finding '{finding_title}' declared {len(needs)} unresolved "
            "prerequisite(s) not satisfied this cycle; blocked until every "
            "prerequisite is wired to an in-flight foundation or has landed."
        )
        enter_blocked_state(
            ct,
            waiting_on=waiting_on,
            reason=reason,
            source="research_prerequisite",
            blocker_kind="upstream_dependency",
            needs=needs,
        )
        session.add(ct)
        logger.warning(
            "[RESEARCH] Finding '%s' BLOCKED on %d unresolved prerequisite(s): %s",
            finding_title,
            len(needs),
            ", ".join(f"{n['kind']}:{n['ref']}" for n in needs),
        )

    # Persist co-deferred dependents into the same deferred_findings list as their
    # prerequisites so autonomous_research re-injects the coupled set next cycle.
    if _codeferred_findings:
        _cd_ctx = dict(task.context or {})
        _cd_ctx["deferred_findings"] = bound_deferred_findings(
            list(_cd_ctx.get("deferred_findings") or []) + _codeferred_findings
        )
        task.context = _cd_ctx
        flag_modified(task, "context")
        session.add(task)

    # Adjust the surviving-findings signal to exclude children cancelled during
    # prerequisite wiring (escalated-to-parent or co-deferred-shelved). These
    # tasks were counted in _surviving_findings at spawn time but never actually
    # ran; leaving them in the signal causes the saturation detector to see
    # tasks_created>=1 on a cycle that produced no real work, preventing
    # auto-completion for stuck directions.
    _prereq_cancelled_count = sum(
        1
        for ct in child_tasks
        if ct.status == TaskStatus.CANCELLED
        and (
            (ct.context or {}).get("prerequisite_escalated_to_parent")
            or (ct.context or {}).get(CO_DEFERRED_SHELVED)
        )
    )
    if _prereq_cancelled_count:
        _record_research_survival_signal(
            task, surviving=max(0, _surviving_findings - _prereq_cancelled_count)
        )

    # Chain auto-decomposed siblings: when a finding is split into N sub-tasks
    # by cochange/path clustering, run them serially rather than in parallel.
    # Each sibling's base branch otherwise picks up partial sibling-merged
    # state, which confuses agents (see dcd6e840: cluster 3's agent saw
    # cluster 1's already-merged scaffolding in task_completion.py and edited
    # the wrong file). Sequential execution lets each agent work against the
    # actually-merged state of prior siblings.
    for ci in range(1, len(child_tasks)):
        if not (_auto_decomposed_flags[ci] and _auto_decomposed_flags[ci - 1]):
            continue
        if finding_indices[ci] != finding_indices[ci - 1]:
            continue
        prev_id = str(child_tasks[ci - 1].id)
        existing = list(child_tasks[ci].depends_on or [])
        if prev_id not in existing:
            existing.append(prev_id)
            child_tasks[ci].depends_on = existing
            remember_structural_depends_on(child_tasks[ci], prev_id)
            session.add(child_tasks[ci])

    # Log file overlap warnings for observability and surface them to agents
    overlaps = _log_file_overlaps(findings, "RESEARCH")
    for i, j, shared_files in overlaps:
        for fi in (i, j):
            ci = finding_to_child.get(fi)
            if ci is not None and ci < len(child_tasks):
                ctx = dict(child_tasks[ci].context or {})
                warnings = ctx.get("file_overlap_warnings", [])
                other_fi = j if fi == i else i
                other_title = findings[other_fi].get("title", f"#{other_fi}")
                warnings.append(
                    {"shared_files": sorted(shared_files), "with_task": other_title}
                )
                ctx["file_overlap_warnings"] = warnings
                child_tasks[ci].context = ctx
                session.add(child_tasks[ci])

    # Spawn-time foundation convergence: serialize GENUINE duplicate foundations
    # behind one canonical task BEFORE the just-spawned children begin executing,
    # rather than letting two duplicate peers both burn agent-VM runtime until the
    # reaper's Step 15 sweep converges them after the fact. See
    # converge_spawn_batch_foundations — it reuses the SAME predicate and the SAME
    # apply-with-guards helper as the reaper sweep, so the two paths can never drift.
    for _dep in converge_spawn_batch_foundations(child_tasks, existing_tasks):
        session.add(_dep)

    if not await _commit_or_handle_duplicate_spawn(
        session, task, stage="spawn-batch wiring"
    ):
        return

    await apply_post_spawn_state(
        session=session,
        task=task,
        findings=findings,
        child_tasks=child_tasks,
        deduplicated=deduplicated,
        insights=insights,
        direction_notes=direction_notes,
        direction_proposals=direction_proposals,
        hash_dedup_count=hash_dedup_count,
        word_overlap_count=word_overlap_count,
        file_overlap_count=file_overlap_count,
        evidence_overlap_count=evidence_overlap_count,
        type_filtered_count=type_filtered_count,
        type_filtered_by_dir=type_filtered_by_dir,
        empty_prompt_count=empty_prompt_count,
        batch_cap_applied_count=batch_cap_applied_count,
        research_branch=research_branch,
        original_base=original_base,
        research_depth=research_depth,
        parent_research_task_id=parent_research_task_id,
        filtered_findings=filtered_findings,
        salvage_count=len(_salvage_candidates),
    )

    # Persist per-cycle deferred-finding counts on the direction's latest
    # research_history entry so the next research agent can see how many of
    # its prior findings failed to spawn and why. Without this, the agent
    # cannot tell whether to produce fewer, higher-confidence findings.
    _total_deferred = len(_all_deferred)
    if _total_deferred > 0:
        _denom = _impl_spawned + _total_deferred
        _deferred_stats = {
            "total": _total_deferred,
            "by_reason": {
                "low_actionability": len(_low_actionability_deferred),
                _LOW_ACTIONABILITY_REFINEMENT_REASON: len(
                    _low_actionability_refinement_deferred
                ),
                "degradation_gate": len(_degradation_gate_deferred),
                "batch_cap": len(_batch_cap_deferred),
                "ultra_low_confidence": len(_low_conf_skipped_findings),
            },
            "spawn_ratio": (_impl_spawned / _denom) if _denom > 0 else None,
        }
        if _spawned_memo_ids:
            _deferred_stats["memo_ids"] = _spawned_memo_ids
        _ds_task_id = str(task.id)

        def _write_deferred_stats(dirs: list) -> None:
            for _d in dirs:
                _hist = _d.get("research_history") or []
                if _hist and _hist[-1].get("task_id") == _ds_task_id:
                    _hist[-1]["deferred_stats"] = _deferred_stats
                    _d["research_history"] = _hist

        await with_directions_locked(session, task.project_id, _write_deferred_stats)
        await session.commit()

    # Mark per-direction cycle productivity so direction_lifecycle can pick
    # an adaptive cooldown: productive cycles (>=1 task spawned) get a shorter
    # cooldown, unproductive cycles keep the standard cooldown.
    tasks_by_dir: dict[str, int] = {}
    for ct in child_tasks:
        did = (ct.context or {}).get("direction_id")
        if did:
            tasks_by_dir[did] = tasks_by_dir.get(did, 0) + 1
    current_direction_id = (task.context or {}).get("direction_id") or (
        task.first_payload
    ).get("current_direction_id")
    touched_dir_ids = set(tasks_by_dir.keys())
    if current_direction_id:
        touched_dir_ids.add(current_direction_id)
    if touched_dir_ids:

        _productivity_task_id = str(task.id)
        # Verification-only cycles produce non-spawning findings (design_doc_observation,
        # direction_note, direction_proposal, etc.) with zero child tasks.  These are still
        # productive — the agent confirmed system state or updated direction metadata.
        # direction_proposals and direction_notes were split out of findings at line 1761.
        _has_non_spawn_findings = bool(
            direction_proposals
            or direction_notes
            or any(f.get("type") in _NON_SPAWNING_FINDING_TYPES for f in findings)
        )

        def _update_productivity(dirs: list) -> None:
            for d in dirs:
                did = d.get("id")
                if did in touched_dir_ids:
                    if d.get("_last_productivity_task_id") == _productivity_task_id:
                        continue
                    d["_last_productivity_task_id"] = _productivity_task_id
                    productive = _compute_direction_productivity(
                        did, tasks_by_dir, current_direction_id, _has_non_spawn_findings
                    )
                    d["last_cycle_productive"] = productive
                    if productive:
                        d["consecutive_unproductive_cycles"] = 0
                    else:
                        d["consecutive_unproductive_cycles"] = (
                            int(d.get("consecutive_unproductive_cycles", 0) or 0) + 1
                        )

        await with_directions_locked(session, task.project_id, _update_productivity)
        await session.commit()

        def _stamp_refractory(dirs: list) -> None:
            for d in dirs:
                if d.get("id") in touched_dir_ids:
                    stamp_direction_refractory(d)

        await with_directions_locked(session, task.project_id, _stamp_refractory)
        await session.commit()

    # Credit playbook entries shown to this research agent when research succeeded.
    # followed_count tracks how many times each entry was seen by a successful research agent,
    # giving a denominator for per-entry success rates used by the archival ranker.
    _shown_pb_keys = list((task.context or {}).get("_shown_playbook_keys") or [])
    if _shown_pb_keys and _impl_spawned > 0:

        def _credit_playbook_keys(doc: dict) -> None:
            for _pb_key in _shown_pb_keys:
                _pb_full_key = f"playbook/{_pb_key}"
                _pb_entry = doc.get(_pb_full_key)
                if isinstance(_pb_entry, dict):
                    _pb_entry = dict(_pb_entry)
                    _pb_entry["followed_count"] = _pb_entry.get("followed_count", 0) + 1
                    doc[_pb_full_key] = _pb_entry

        await with_design_doc_locked(session, task.project_id, _credit_playbook_keys)
        await session.commit()


async def _record_research_task_failure(task: Task, session: AsyncSession) -> None:
    """Record a FAILED research task in the direction's research_history.

    Closes a blind spot where research tasks that never reached _handle_research_completion
    (VM crash, provision failure, agent timeout, etc.) left no trace on the direction,
    so _check_direction_health could not observe the stalled cycle.

    Appends a minimal history entry tagged research_task_failed=True to the direction
    referenced in task.context.direction_id (falling back to the payload field).
    """
    from app.failure_classification import INFRA_CLASSES, classify_failure

    # Evergreen-fill requeue adapter: an environmental (infra-class) failure of
    # an evergreen probe requeues the backlog item (attempts+=1, bounded), rather
    # than burning it. Runs before the direction guard below because an evergreen
    # probe may carry no direction_id (synthetic strand).
    _ev_item_id = (task.context or {}).get("evergreen_backlog_item_id")
    if _ev_item_id:
        _ev_failure_category = classify_failure(task, task.latest_status_message or "")
        if _ev_failure_category in INFRA_CLASSES:
            try:
                from app.evergreen_backlog import apply_research_outcome

                await apply_research_outcome(
                    session,
                    item_id=str(_ev_item_id),
                    impl_spawned=0,
                    reproduce_rejected=0,
                    surviving=0,
                    environmental_failure=True,
                )
            except Exception as _eg_exc:  # noqa: BLE001
                logger.warning(
                    "[EVERGREEN] failed to apply environmental failure outcome for item %s: %s",
                    _ev_item_id,
                    _eg_exc,
                )
            await session.commit()

    direction_id = (task.context or {}).get("direction_id") or task.first_payload.get(
        "current_direction_id"
    )
    if not direction_id:
        return
    now_iso = _utcnow().isoformat()
    failure_category = classify_failure(task, task.latest_status_message or "")
    entry = {
        "timestamp": now_iso,
        "task_id": str(task.id),
        "tasks_created": 0,
        "tasks_failed": 0,
        "tasks_completed": 0,
        "prs_merged": 0,
        "summary": f"research task failed: {failure_category}",
        "finding_titles": [],
        "key_finding_descriptions": [],
        "research_task_failed": True,
        "failure_category": failure_category,
    }
    # Infra failures are transient — use the shorter productive cooldown so the
    # direction is retried promptly instead of waiting out the full unproductive window.
    productive = failure_category in INFRA_CLASSES
    _found = False

    def _record_failure(dirs: list) -> None:
        nonlocal _found
        target = next((d for d in dirs if d.get("id") == direction_id), None)
        if not target:
            return
        _found = True
        history = target.get("research_history", [])
        # Guard against duplicate task_ids on post-commit retry: if this task_id
        # is already in the history, skip the append to ensure idempotency.
        existing_task_ids = {e.get("task_id") for e in history}
        agg = target.get("history_aggregate") or {}
        for _ev in (agg.get("evicted_findings") or []):
            if _ev.get("task_id"):
                existing_task_ids.add(_ev.get("task_id"))
        if entry.get("task_id") in existing_task_ids:
            return
        new_history = [*history, entry]
        # Share the existing eviction/aggregate rule: keep last 10, aggregate older entries
        if len(new_history) > 10:
            target["history_aggregate"] = _fold_evicted_into_aggregate(
                target.get("history_aggregate"), new_history[:-10]
            )
            # Store compact per-cycle snapshots so post-eviction patch paths
            # (post-merge recompute, stranded-PR discount) can apply deltas to the
            # aggregate rather than silently no-oping when the live entry is gone.
            _evicted_snaps = target["history_aggregate"].setdefault(
                "_evicted_cycle_snapshots", {}
            )
            for _ev in new_history[:-10]:
                _tid = _ev.get("task_id")
                if _tid:
                    _evicted_snaps[str(_tid)] = build_evicted_cycle_snapshot(_ev)
        target["research_history"] = new_history[-10:]
        target["last_researched_at"] = now_iso
        target["last_cycle_productive"] = productive
        if productive:
            target["consecutive_unproductive_cycles"] = 0
        else:
            target["consecutive_unproductive_cycles"] = (
                int(target.get("consecutive_unproductive_cycles", 0) or 0) + 1
            )
        stamp_direction_refractory(target)

    await with_directions_locked(session, task.project_id, _record_failure)
    if _found:
        logger.warning(
            f"[RESEARCH] Recorded failed research task {task.id} on direction "
            f"{direction_id} ({failure_category})"
        )


_BOUNDED_CHALLENGE_SURFACE_KEYS: frozenset = frozenset(
    {
        "challenged_boundary",
        "first_verdict",
        "second_opinion_verdict",
        "agreement",
        "reason",
        "sidecar_task_id",
    }
)


def resolve_research_findings_for_surface(
    plan_output: List[Any] | None,
    artifact_logs: str | None,
) -> List[Dict[str, Any]] | None:
    """Return the canonical challenge-enriched finding list for surface rendering.

    Prefers task.plan_output (which carries attached finding_challenge records)
    over the legacy RESEARCH_FINDINGS artifact log stream (written at agent-
    submission time, before challenge resolution). Falls back to parsing
    artifact_logs for tasks that completed before challenge records were added.

    The finding_challenge record on each returned finding is bounded to
    {challenged_boundary, first_verdict, second_opinion_verdict, agreement,
    reason, sidecar_task_id} so consumers receive a stable, bounded contract.
    Findings without a challenge record are returned unchanged.

    Returns None when neither source yields a non-empty list of dicts.
    """

    def _trim_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
        challenge = finding.get(_FINDING_CHALLENGE_RECORD_KEY)
        if not isinstance(challenge, dict):
            return finding
        bounded = {
            k: v for k, v in challenge.items() if k in _BOUNDED_CHALLENGE_SURFACE_KEYS
        }
        if bounded == challenge:
            return finding
        out = dict(finding)
        out[_FINDING_CHALLENGE_RECORD_KEY] = bounded
        return out

    # Prefer plan_output: non-empty list of dicts carries challenge records
    if isinstance(plan_output, list) and plan_output:
        findings = [f for f in plan_output if isinstance(f, dict)]
        if findings:
            return [_trim_finding(f) for f in findings]

    # Fall back to artifact logs; support both [list] and {"logs": [...]} shapes
    if artifact_logs:
        try:
            parsed = json.loads(artifact_logs[:100000])
            if isinstance(parsed, list):
                findings = [f for f in parsed if isinstance(f, dict)]
                if findings:
                    return findings
            elif isinstance(parsed, dict):
                inner = parsed.get("logs", [])
                if isinstance(inner, list):
                    findings = [f for f in inner if isinstance(f, dict)]
                    if findings:
                        return findings
        except (json.JSONDecodeError, AttributeError):
            pass

    return None
