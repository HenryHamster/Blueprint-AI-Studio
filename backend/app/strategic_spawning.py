"""Strategic cycle spawning functions.

Handles Phase 1 (design_doc_update) and Phase 2 (strategic_review) of the
strategic review cycle. Extracted from autonomous_research.py to free up
function slots in that module.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app import strategic_cycle
from app.direction_metrics import (
    _compute_direction_quality_score,
    canonical_cost_per_merged_pr,
)
from app.direction_outcomes import (
    compute_direction_autonomy_state,
    declared_merged_count,
    discount_stranded_pr_from_direction_credit,
    proven_merged_count,
    reachable_merged_count,
    verified_merged_count,
)
from app.direction_mode import mode_prompt_hint, normalized_recommended_mode
from app.knowledge_query import build_knowledge_index, build_compute_sweep_groups, fetch_direction_knowledge_index, build_pending_followups, build_strategic_memory_index
from app.models import (
    Task,
    Project,
    TaskHistory,
    TaskStatus,
    MessageType,
    ProjectMessage,
    SystemMaturity,
)
from app.deploy_identity import (
    project_is_deploy_repo,
    project_matches_deploy_identity,
)
from app.resource_envelope import resolve_project_require_local_runner
from app.payload_schemas import pack_under_budget, validate_internal_payload
from app.strategic_payload_compaction import (
    _OUTCOME_DIRECTION_OUTCOMES_KEEP,
    _VERSION_PHASE_RECENT_VERSIONS_CAP,
    _direction_activity_score,  # noqa: F401 — re-exported for test imports
    _directions_summary_floor,
    _enrich_drift_with_trend,
    _summarize_directions,
    _summarize_drift_signals,
    _summarize_version_phase,
    _version_phase_floor,
)
from app.release_artifacts import (
    summarize_required_artifact_gap,
    version_serving_summary as _version_serving_summary,
)
from app.autonomous_spawn_budget import (
    DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE,
    autonomous_spawn_task_clause,
    autonomous_spawn_budget_config_from_runtime,
    build_autonomous_spawn_budget_guard,
    compute_autonomous_spawn_budget_snapshot,
)
from app.release_version import QUALIFICATION_STATE_VOCAB
from app.capability_demand import (
    fetch_missing_demand_for_spawn,
    missing_demand_prompt_hint,
)
from app.capability_trial_verdict import get_trial_verdicts
from app.capability_registry import (
    annotate_capability_proof_status as _annotate_capability_proof_status,
    registry_capability_names,
    registry_capability_vocabulary,
)
from app.project_telemetry import read_telemetry
from app.problem_model import (
    REJECTED_AGENT_EDITS_KEY as PM_REJECTED_EDITS_KEY,
    resolve_project_purpose,
    sanitize_problem_model_raw,
)

# The agent-view problem_model builder cluster (count-based trim, guaranteed-
# small floor shrink, and the packer summarize/floor callbacks) lives in a
# sibling module to keep this file under its size cap. Re-imported into this
# namespace so every existing `from app.strategic_spawning import ...` call
# site and test keeps resolving these names unchanged.
from app.strategic_problem_view import (  # noqa: F401 — re-exported for callers/tests
    _AGENT_VIEW_FLOOR_BYTES,
    _CAP_KEEP_RANK,
    _CAP_SKELETON_KEEP,
    _FLOOR_MARKER_RESERVE,
    _FLOOR_MIN_GAPS,
    _FLOOR_SHRINK_LEVELS,
    _FRICTION_KEEP_RANK,
    _FRICTION_SKELETON_KEEP,
    _GAP_EVIDENCE_KEEP,
    _GAP_HISTORY_KEEP,
    _GAP_PAST_MOVES_KEEP,
    _GAP_PRIORITY_RANK,
    _agent_view_problem_model,
    _agent_view_problem_model_floor,
    _agent_view_problem_model_packed,
    _compute_model_health,
    _gap_floor_keep_rank,
    _serialized_bytes,
    _shrink_pm_to_floor,
    _skeletonize_caps_friction,
)
from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
from app.source_ref_validation import (
    classify_paths_tristate,
    ground_design_doc,
    resolve_branch_verification_context,
)
from app.strategic_trigger import (
    ARMING_STEERING,
    ORIGIN_OPERATOR,
    classify_operator_arming,
    strategic_refresh_excerpt,
    strategic_refresh_origin,
)
from app.strategic_authority_locks import (
    LegacyEngineAuthorityExpired,
    commit_legacy_authorized,
    lock_and_validate_legacy_authority,
)
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.task_evaluators import (
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)
import app.shared_state as _shared_state
from app.shared_state import (
    get_project_integration_branch,
    get_project_promotion_target,
    runtime_config as _runtime_config,
)
from app.size_limit_policy import classify_breach
from app.time_utils import _utcnow, _coerce_naive_utc
from app.vision_merge import REJECTED_EDITS_KEY
from app.landed_signal import LANDED_ON_INTEGRATION_KEY, mark_stranded_non_landing
from app.project_message_projection import agent_thread_message_dict
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)

from app.strategic_local_runner import (  # noqa: F401 — re-exported for callers/tests
    _RUNNER_LIVENESS_SECONDS,
    _load_baked_image_ref,
    _load_local_runner_status,
    _agent_visible_vision,
    _local_runner_handles_task_type,
    _local_runner_is_live,
    _local_runner_agent_available,
    _resolve_local_continuation_runner_affinity,
    _resolve_local_continuation_runner_intent,
)

from app.strategic_payload_summaries import (  # noqa: F401 — re-exported for callers/tests
    _summarize_conversation_thread,
    _MEMO_CITATIONS_KEEP,
    _MEMO_EXCERPT_MAX_CHARS,
    _MEMO_HEAVY_FIELDS,
    _MEMO_VERBOSE_FIELDS,
    _strip_verbose_fields,
    _cap_excerpt,
    _summarize_memo_citations,
    _OPEN_MESSAGES_FETCH_LIMIT,
    _OPEN_MESSAGES_KEEP,
    _OPEN_MESSAGES_KEEP_WHEN_TRIMMED,
    _OPEN_MESSAGE_EXCERPT_MAX_CHARS,
    _CRITICAL_OPEN_MESSAGE_TYPES,
    _OPERATOR_CONTEXT_MESSAGE_FETCH_LIMIT,
    _OPERATOR_CONTEXT_REQUESTS_KEEP,
    _OPERATOR_CONTEXT_REFS_KEEP,
    _OPERATOR_CONTEXT_REF_SCAN_LIMIT,
    _OPERATOR_CONTEXT_DESCRIPTOR_SCAN_LIMIT,
    _OPERATOR_CONTEXT_FROM_TYPES,
    _OPERATOR_CONTEXT_TARGET_KEYS,
    _open_message_survival_key,
    _bound_open_messages,
    _summarize_open_messages,
    _OPEN_MESSAGES_FLOOR_KEEP,
    _OPEN_MESSAGE_FLOOR_EXCERPT_MAX_CHARS,
    _floor_open_messages,
    _operator_context_text,
    _operator_context_ref,
    _operator_context_target,
    _operator_context_descriptor,
    _operator_context_descriptor_map,
    _operator_context_package_descriptor,
    _operator_context_created_sort_value,
    _build_operator_context_refs_from_messages,
    _fetch_operator_context_refs,
    _open_message_citation_summary,
    _fetch_open_messages,
    _fetch_recent_handled_conversation,
    _RECENT_HANDLED_KEEP,
    _RECENT_HANDLED_FETCH_LIMIT,
    _RECENT_HANDLED_EXCERPT_MAX_CHARS,
    _summarize_recent_interventions,
    _summarize_capability_trial_verdicts,
    _OPERATOR_OVERRIDES_SUMMARIZE_KEEP,
    _OVERRIDE_VALUE_SUMMARIZE_CAP,
    _summarize_operator_overrides,
    _KNOWLEDGE_INDEX_SUMMARIZE_KEEP,
    _KNOWLEDGE_INDEX_SUMMARIZE_SUMMARY_CAP,
    _KNOWLEDGE_INDEX_HEAVY_FIELDS,
    _KNOWLEDGE_INDEX_KEEP_KEYS,
    _summarize_knowledge_index,
    _PREV_OUTCOMES_ROLE_KEEP,
    _PREV_OUTCOMES_TEXT_CAP,
    _PREV_OUTCOMES_SUMMARY_TEXT_CAP,
    _PREV_OUTCOMES_RATIONALE_CAP,
    _PREV_OUTCOMES_HEAVY_FIELDS,
    _summarize_previous_decision_outcome_entry,
    _summarize_previous_decision_outcomes,
    _PLAYBOOK_SUMMARY_ENTRIES_KEEP,
    _PLAYBOOK_SUMMARY_CONTENT_CAP,
    _PLAYBOOK_SUMMARY_DROP_FIELDS,
    _summarize_playbook_summary,
    _PAST_CRITIQUES_KEEP,
    _PAST_CRITIQUE_TEXT_CAP,
    _PAST_CRITIQUE_TEXT_FIELDS,
    _summarize_past_retirement_critiques,
    _SELF_REFLECTION_KEEP,
    _SELF_REFLECTION_TEXT_CAP,
    _SELF_REFLECTION_TEXT_FIELDS,
    _build_self_reflection_themes,
    _summarize_self_reflection_log,
)


logger = logging.getLogger(__name__)

STRATEGIC_CYCLE_MANUAL_CONTEXT_KEY = "strategic_cycle_manual"

_CHALLENGE_BOUNDARY_BUCKETS = (
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
)
_CHALLENGE_AGREEMENT_BUCKETS = (
    CHALLENGE_AGREES,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)


def _empty_finding_challenge_summary() -> dict[str, Any]:
    return {
        "challenged_findings": 0,
        "agreement_distribution": dict.fromkeys(_CHALLENGE_AGREEMENT_BUCKETS, 0),
        "boundary_distribution": dict.fromkeys(_CHALLENGE_BOUNDARY_BUCKETS, 0),
        "fail_closed_unparseable": 0,
    }


def _positive_int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _merge_finding_challenge_summary(target: dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    target["challenged_findings"] += _positive_int(source.get("challenged_findings"))
    target["fail_closed_unparseable"] += _positive_int(
        source.get("fail_closed_unparseable")
    )
    source_agreements = source.get("agreement_distribution") or {}
    if isinstance(source_agreements, dict):
        for key in _CHALLENGE_AGREEMENT_BUCKETS:
            target["agreement_distribution"][key] += _positive_int(
                source_agreements.get(key)
            )
    source_boundaries = source.get("boundary_distribution") or {}
    if isinstance(source_boundaries, dict):
        for key in _CHALLENGE_BOUNDARY_BUCKETS:
            target["boundary_distribution"][key] += _positive_int(
                source_boundaries.get(key)
            )


async def _strategic_recent_version_entry(
    session, version, project=None
) -> dict[str, Any]:
    from app.qualification_state_resolver import (
        resolve_open_version_qualification_read_model,
    )

    aggregate, artifacts = _version_serving_summary(version, project)
    # Route through the shared seam so this surface's qualification_state carries
    # the ledger-backed per-dimension sub_score overlay instead of the mutable
    # last_qualification blob, matching the dimension_states shown elsewhere.
    qualification_read = await resolve_open_version_qualification_read_model(
        session, version, project, serving=(aggregate, artifacts)
    )
    return {
        "version_number": version.version_number,
        "status": version.status,
        "phase": version.phase,
        "last_qualification": version.last_qualification,
        "qualification_state": qualification_read.qualification_state,
        "qualification_provenance": qualification_read.provenance,
        "dimension_states": qualification_read.dimension_states,
        "serving_status": aggregate["serving_status"],
        "served_sha": (version.served_sha or "")[:12] or None,
        "deploy_failure_reason": aggregate["reason_code"],
        "artifacts": artifacts,
    }


def _build_portfolio_coherence_hint(
    active_count: int, max_concurrent_vms: int, max_active_directions: int
) -> str:
    """Describe direction/task semantics without turning capacity into a quota."""
    cap_clause = (
        f"The fleet can run about {max_concurrent_vms} VMs concurrently, but that capacity"
        if max_concurrent_vms
        else "Available execution capacity"
    )
    return (
        f"PORTFOLIO COHERENCE: {active_count}/{max_active_directions} active "
        f"direction slots are in use. {cap_clause} is a ceiling, "
        f"never a target for direction count. Keep directions as a small set of "
        f"durable, distinct outcomes. Parallelize bounded implementation, "
        f"research, design, or compute with multiple tasks under the existing "
        f"direction. Create another direction only for a genuinely distinct "
        f"durable outcome, never to divide files, subsystems, implementation "
        f"stages, or workers."
    )


# Keys kept in Project.design_doc for operator/UI consumption but never
# consumed by the agent. Stripping them before the payload goes out prevents
# unbounded state (e.g. playbook_archive) from bloating claim responses —
# see the silent pre-claim death investigation (task 595c235d).
_AGENT_UNUSED_DESIGN_DOC_KEYS = frozenset({"playbook_archive"})


def _agent_view_design_doc(doc: dict | None) -> dict:
    """Shallow copy of *doc* with agent-unused + internal (``_``-prefixed) keys
    dropped. The agent's design-doc handler only renders 200-char previews of
    each key, so operator-only state like ``playbook_archive`` has no
    functional purpose downstream and just inflates the payload."""
    if not doc:
        return {}
    return {
        k: v
        for k, v in doc.items()
        if k not in _AGENT_UNUSED_DESIGN_DOC_KEYS and not k.startswith("_")
    }


# Maximum length of the one-line per-entry summary carried in the design_doc
# index. Keeps each entry navigation-level — enough to recognize a section,
# never its full prose/specs — so the index stays bounded no matter how large
# the underlying design doc grows. The agent pulls full content on demand via
# ``POST /api/agent/design-doc`` (op:get).
_DESIGN_DOC_INDEX_SUMMARY_CHARS = 200
# Per-field char cap for repo_overview inline fields (tech_stack,
# directory_layout, first_steps_summary). Enough to orient the first strategic
# cycle without unbounding the index on verbose bootstrap output.
_REPO_OVERVIEW_INLINE_CHARS = 300
# Cap on backing module paths carried per systems/capabilities entry so a
# pathological entry with hundreds of modules cannot unbound the index. The
# declared paths are kept (capped) so the agent-side reconciliation over the
# cloned workspace remains possible as an optional second pass.
_DESIGN_DOC_INDEX_MODULES_KEEP = 16
# Non-section bookkeeping keys that live inside project.design_doc but are NOT
# architecture sections and so must NOT appear as index entries. ``proposed_changes``
# is the operator/agent reconciliation queue — it is shipped as its OWN top-level
# payload field (see _spawn_design_doc_update), so indexing it here would be
# redundant noise ('Other sections' entry with no summary).
_DESIGN_DOC_INDEX_NON_SECTION_KEYS = frozenset({"proposed_changes"})


def _design_doc_entry_summary(value: Any) -> str | None:
    """Derive a bounded, single-line summary for one design-doc section.

    Prefers an explicit ``title``/``summary`` field, else the first prose body
    (``description`` for systems, ``notes`` for capabilities, or any string).
    Collapses whitespace and truncates to :data:`_DESIGN_DOC_INDEX_SUMMARY_CHARS`
    so the index entry never carries full prose."""
    text: str | None = None
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        for field in ("title", "summary", "description", "notes", "purpose"):
            candidate = value.get(field)
            if isinstance(candidate, str) and candidate.strip():
                text = candidate
                break
    if not isinstance(text, str) or not text.strip():
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) > _DESIGN_DOC_INDEX_SUMMARY_CHARS:
        return collapsed[: _DESIGN_DOC_INDEX_SUMMARY_CHARS - 1].rstrip() + "…"
    return collapsed


def _design_doc_index(doc: dict | None) -> dict:
    """Build a compact, bounded navigation index over an already-grounded
    agent-view design doc.

    Replaces the full-content design_doc field in the strategic payload: instead
    of shipping every section's full prose/specs inline (unbounded as the doc
    grows), ships ONE entry per non-internal section carrying only navigation-
    and architecture-level metadata:

      * ``key``    — the section key (e.g. ``systems/auth``)
      * ``summary``— a single, whitespace-collapsed, char-bounded line (when the
        section has any prose to summarize)
      * for ``systems/*`` and ``capabilities/*`` additionally the reconciled
        ``status`` (presence — computed upstream by :func:`ground_design_doc`),
        the declared ``modules`` backing paths (capped), and any
        ``presence_warning`` the grounding pass stamped on.

    The full, server-side-reconciled content of any section remains available on
    demand via ``POST /api/agent/design-doc`` (op:get). The index size does NOT
    scale with section prose: every text field is bounded and module lists are
    capped, so the index stays small however large the design doc becomes.
    """
    if not doc:
        return {"sections": []}
    entries: list[dict] = []
    for key in sorted(k for k in doc if isinstance(k, str)):
        if (
            key in _AGENT_UNUSED_DESIGN_DOC_KEYS
            or key in _DESIGN_DOC_INDEX_NON_SECTION_KEYS
            or key.startswith("_")
        ):
            continue
        value = doc[key]
        entry: dict[str, Any] = {"key": key}
        summary = _design_doc_entry_summary(value)
        if summary is not None:
            entry["summary"] = summary
        if isinstance(value, dict) and (
            key.startswith("systems/") or key.startswith("capabilities/")
        ):
            status = value.get("status")
            if isinstance(status, str) and status.strip():
                entry["status"] = status
            modules = value.get("modules")
            if isinstance(modules, list):
                module_paths = [m for m in modules if isinstance(m, str) and m.strip()][
                    :_DESIGN_DOC_INDEX_MODULES_KEEP
                ]
                if module_paths:
                    entry["modules"] = module_paths
            warning = value.get("presence_warning")
            if isinstance(warning, str) and warning.strip():
                entry["presence_warning"] = warning
        elif isinstance(value, dict) and key == "repo_overview":
            # repo_overview is a first-class bootstrap section: carry all overview
            # fields inline so the first strategic cycle has actual repo context
            # without a fetch round-trip. Optional fields are included when present.
            for field in (
                "tech_stack",
                "directory_layout",
                "first_steps_summary",
                "entry_points",
                "test_layout",
                "key_conventions",
                "environment_requirements",
            ):
                raw = value.get(field)
                if isinstance(raw, str) and raw.strip():
                    collapsed = " ".join(raw.split())
                    entry[field] = (
                        collapsed[: _REPO_OVERVIEW_INLINE_CHARS - 1].rstrip() + "…"
                        if len(collapsed) > _REPO_OVERVIEW_INLINE_CHARS
                        else collapsed
                    )
        entries.append(entry)
    return {"sections": entries}


def _summarize_design_doc_index(index: Any) -> Any:
    """Budget-pressure fallback for the design_doc index: drop the per-entry
    one-line summaries, keeping only the navigation skeleton (key + reconciled
    status + backing modules + presence_warning). The full content of any
    section — including its prose/summary — remains fetchable on demand via the
    design-doc op:get endpoint, so dropping summaries here is non-destructive.

    The index is already compact, so this almost never fires; it exists so the
    field degrades gracefully (skeleton) rather than vanishing wholesale under
    genuine budget exhaustion."""
    if not isinstance(index, dict):
        return index
    sections = index.get("sections")
    if not isinstance(sections, list):
        return index
    skeleton = [
        {k: v for k, v in entry.items() if k != "summary"}
        if isinstance(entry, dict)
        else entry
        for entry in sections
    ]
    return {**index, "sections": skeleton}


# Maximum operator-override entries surfaced to the next strategic cycle, and
# the per-value character cap. The override summary closes the operator-wins
# reconciliation loop: the reconciliation writers record each overridden agent
# value into rolling, capped ledgers (vision ``_rejected_agent_edits`` and
# problem_model ``rejected_agent_edits``) "so next cycle's agent can see what
# was overridden and reconcile" — this helper reads those ledgers (read-only;
# never clears or mutates them) into one bounded list. Kept small so it can
# never bloat the prompt; the payload packer drops it whole under budget.
_OPERATOR_OVERRIDES_MAX = 8
_OVERRIDE_VALUE_MAXLEN = 240


def _truncate_override_value(value: Any) -> Any:
    """Char-bound a single ledger value so the override summary stays compact.

    Strings are truncated with an ellipsis marker; non-strings are coerced to
    their ``repr`` and truncated the same way so a stray dict/list cannot blow
    the per-entry budget.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    if len(text) > _OVERRIDE_VALUE_MAXLEN:
        return text[:_OVERRIDE_VALUE_MAXLEN] + "…"
    return text


def _build_operator_overrides_summary(project: Project) -> list[dict]:
    """Consolidate the operator-wins rejected-edit ledgers into one bounded list.

    Reads both reconciliation ledgers — ``project.vision[_rejected_agent_edits]``
    (vision scalar conflicts + ``running_update_superseded`` entries) and
    ``project.problem_model[rejected_agent_edits]`` (operator-owned intent /
    context / constraints conflicts) — and normalizes each entry to a compact,
    char-bounded shape::

        {document, field, kind, agent_value, operator_value, at}

    ``document`` is ``running_update`` | ``vision`` | ``intent`` so the agent
    can tell which document it overrode. Running-update entries have no operator
    value (the operator's winning value is the live running_update body itself),
    so ``operator_value`` is ``None`` there. Sorted newest-first and capped at
    :data:`_OPERATOR_OVERRIDES_MAX`. Read-only: neither ledger is cleared or
    mutated. Returns ``[]`` when both ledgers are empty.
    """
    entries: list[dict] = []

    vision = project.vision if isinstance(project.vision, dict) else {}
    for e in vision.get(REJECTED_EDITS_KEY) or []:
        if not isinstance(e, dict):
            continue
        field = e.get("field") or ""
        document = "running_update" if field == "running_update" else "vision"
        entries.append(
            {
                "document": document,
                "field": field,
                "kind": e.get("kind"),
                "agent_value": _truncate_override_value(e.get("agent_value")),
                # Vision conflicts log the operator's winning value as
                # ``user_value``; running_update entries carry none.
                "operator_value": _truncate_override_value(e.get("user_value")),
                "at": e.get("at"),
            }
        )

    pm = project.problem_model if isinstance(project.problem_model, dict) else {}
    for e in pm.get(PM_REJECTED_EDITS_KEY) or []:
        if not isinstance(e, dict):
            continue
        field = e.get("field") or ""
        document = "intent" if field.startswith("intent") else "problem_model"
        entries.append(
            {
                "document": document,
                "field": field,
                "kind": e.get("kind"),
                "agent_value": _truncate_override_value(e.get("agent_value")),
                "operator_value": _truncate_override_value(e.get("operator_value")),
                "at": e.get("at"),
            }
        )

    # Newest-first by ISO timestamp; missing/empty ``at`` sorts last.
    entries.sort(key=lambda x: x.get("at") or "", reverse=True)
    return entries[:_OPERATOR_OVERRIDES_MAX]


async def _verify_capability_backing_paths(
    pm: dict | None,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Annotate each capability in *pm* with advisory backing-path verification.

    Mirrors the design-doc ``systems/<id>.modules`` verification in
    ``autonomous_research.py``: for every capability that declares
    ``backing_paths``, classify those paths against the integration-branch
    file tree (prefer the project's real GitHub repo/branch when available,
    else local git HEAD via :func:`classify_paths_tristate`) and attach a
    per-capability ``verification`` block::

        {"present": [...], "missing": [...], "effective_status": "..."}

    ``effective_status`` framing so downstream prompts reason about what the
    code actually shows rather than trusting the stored status verbatim:
      - ``backing_paths`` empty → ``"unverified"`` (no linkage to check)
      - all paths present → the capability's own stored status (verified)
      - no definite absence but unknowable paths → ``"unverified"`` (a rate
        limit or API outage is never evidence of absent code)
      - mixed (some definitely missing, rest present/unknowable) → ``"partial"``
      - all paths definitely missing → ``"absent"`` (claimed but no backing
        code exists)

    Operates on a copy: the input *pm* (and therefore the durable
    ``project.problem_model``) is never mutated. *pm* is expected to already
    be ``_agent_view_problem_model``-trimmed; this helper only adds keys.
    """
    if not pm:
        return {}
    annotated = dict(pm)
    caps = annotated.get("capabilities") or []
    if not isinstance(caps, list):
        return annotated
    new_caps: list = []
    for cap in caps:
        if not isinstance(cap, dict):
            new_caps.append(cap)
            continue
        cap2 = dict(cap)
        paths = [p for p in (cap2.get("backing_paths") or []) if isinstance(p, str)]
        if not paths:
            cap2["verification"] = {
                "present": [],
                "missing": [],
                "effective_status": "unverified",
            }
            new_caps.append(cap2)
            continue
        present, missing, indeterminate = await classify_paths_tristate(
            paths,
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )
        if not missing and not indeterminate:
            effective_status = str(cap2.get("status", "")) or "unverified"
        elif not missing:
            # Unknowable paths only (rate limit / quota reserve / API error):
            # no verdict either way — never launder an outage into absence.
            effective_status = "unverified"
        elif present or indeterminate:
            effective_status = "partial"
        else:
            # Every declared backing path is unambiguously absent from the
            # integration branch: the capability is claimed but its code does
            # not exist.
            effective_status = "absent"
        cap2["verification"] = {
            "present": present,
            "missing": missing,
            **({"indeterminate": indeterminate} if indeterminate else {}),
            "effective_status": effective_status,
        }
        new_caps.append(cap2)
    annotated["capabilities"] = new_caps
    return annotated


# Per-system maturity rows can be heavy if `top_issues` and `evidence` carry
# bulky payloads — cap the count per project to keep the strategic payload
# under the 256KB ceiling. SystemMaturity is per (project_id, system_id), so
# the natural ceiling is the count of distinct system_ids on the project.
_SYSTEM_MATURITY_PAYLOAD_CAP = 25

# Per-row issue and evidence caps so a single noisy system can't dominate the
# payload. Operators can still see the full record via the API.
_SYSTEM_MATURITY_TOP_ISSUES_CAP = 5


async def _build_system_maturity_payload(
    project: Project,
    session: AsyncSession,
) -> list[dict]:
    """Fetch SystemMaturity rows for *project* and shape them for the agent.

    Returns at most _SYSTEM_MATURITY_PAYLOAD_CAP entries, ordered by
    last_audited_at descending (oldest audits get trimmed first when over
    the cap, so the agent always sees the freshest snapshots). Returns an
    empty list when no rows exist — strategic_spawning's payload includes
    the field only when non-empty so cold-start projects don't carry an
    empty field through the cap.
    """
    result = await session.execute(
        select(SystemMaturity)
        .where(SystemMaturity.project_id == project.id)
        .order_by(SystemMaturity.last_audited_at.desc())
        .limit(_SYSTEM_MATURITY_PAYLOAD_CAP)
    )
    rows = result.scalars().all()
    out: list[dict] = []
    for row in rows:
        top_issues = (row.top_issues or [])[:_SYSTEM_MATURITY_TOP_ISSUES_CAP]
        out.append(
            {
                "system_id": row.system_id,
                "score": row.score,
                "components": dict(row.components or {}),
                "recommended_mode": row.recommended_mode,
                "last_audited_at": (
                    row.last_audited_at.isoformat() if row.last_audited_at else None
                ),
                "top_issues": top_issues,
            }
        )
    return out


_EFFICIENCY_SNAPSHOT_HISTORY_TAIL = 3


def _summarize_efficiency_snapshot(snapshot: dict) -> dict:
    """Trim efficiency_snapshot for inclusion in strategic-agent payloads.

    Strips full history arrays down to a 3-point tail per axis so the agent
    can read a trend without dragging months of weekly history through the
    payload budget. Also forwards format_compliance and agent_timeout_rate
    blocks (current/baseline/drift_pct only, no history) so the strategist
    can answer 'is self-repair working?' and 'is the timeout rate improving?'
    without a separate fetch.
    """
    if not snapshot:
        return {}
    axes = snapshot.get("axes") or {}
    trimmed_axes: dict = {}
    for axis, data in axes.items():
        if not isinstance(data, dict):
            trimmed_axes[axis] = data
            continue
        history = data.get("history") or []
        seeded_at = data.get("baseline_seeded_at")
        if seeded_at == snapshot.get("baseline_captured_at"):
            seeded_at = None
        trimmed_axes[axis] = {
            "current": data.get("current"),
            "baseline": data.get("baseline"),
            "drift_pct": data.get("drift_pct"),
            "history_tail": history[-_EFFICIENCY_SNAPSHOT_HISTORY_TAIL:],
        }
        if seeded_at:
            trimmed_axes[axis]["seeded_at"] = seeded_at
    result: dict = {
        "axes": trimmed_axes,
        "baseline_captured_at": snapshot.get("baseline_captured_at"),
        "last_snapshot_at": snapshot.get("last_snapshot_at"),
    }
    raw_fc = snapshot.get("format_compliance") or {}
    if raw_fc:
        fc_trimmed: dict = {}
        for rate_key in ("format_compliance_rate", "self_repair_success_rate"):
            rate_data = raw_fc.get(rate_key)
            if isinstance(rate_data, dict):
                fc_trimmed[rate_key] = {
                    "current": rate_data.get("current"),
                    "baseline": rate_data.get("baseline"),
                    "drift_pct": rate_data.get("drift_pct"),
                }
        if fc_trimmed:
            fc_trimmed["repair_attempts"] = raw_fc.get("repair_attempts")
            miss_by_schema_and_reason = (
                raw_fc.get("format_miss_by_schema_and_reason") or {}
            )
            if miss_by_schema_and_reason:
                schema_totals = {
                    schema: sum(reasons.values())
                    for schema, reasons in miss_by_schema_and_reason.items()
                    if isinstance(reasons, dict)
                }
                total_misses = sum(schema_totals.values())
                if schema_totals and total_misses > 0:
                    dominant_schema = max(schema_totals, key=schema_totals.__getitem__)
                    schema_reasons = miss_by_schema_and_reason[dominant_schema]
                    dominant_reason = max(
                        schema_reasons, key=schema_reasons.__getitem__
                    )
                    fc_trimmed["dominant_schema"] = dominant_schema
                    fc_trimmed["dominant_reason"] = dominant_reason
                    fc_trimmed["dominant_share"] = round(
                        schema_totals[dominant_schema] / total_misses, 3
                    )
            result["format_compliance"] = fc_trimmed
    raw_atr = snapshot.get("agent_timeout_rate") or {}
    if raw_atr:
        overall = raw_atr.get("overall") or {}
        result["agent_timeout_rate"] = {
            "overall": {
                "current": overall.get("current"),
                "baseline": overall.get("baseline"),
                "drift_pct": overall.get("drift_pct"),
            },
            "dominant_subtype": raw_atr.get("dominant_subtype"),
            "dominant_share": raw_atr.get("dominant_share"),
        }
    raw_attr = snapshot.get("llm_cost_attribution") or {}
    if raw_attr:
        by_model = raw_attr.get("by_model") or []
        by_type = raw_attr.get("by_task_type") or []
        by_failure_class = raw_attr.get("by_failure_class") or []
        # Find the dominant non-MERGED failure class (highest cost).
        dominant_failure_class = None
        for fc_row in by_failure_class:
            if fc_row.get("failure_class") != "MERGED":
                dominant_failure_class = fc_row
                break
        result["llm_cost_attribution"] = {
            "total_llm_usd": raw_attr.get("total_llm_usd"),
            "rework_share": raw_attr.get("rework_share"),
            "self_repair_burn_share": raw_attr.get("self_repair_burn_share"),
            # Dominant driver signals: the single highest-cost model, task type,
            # and failure class so the strategist sees WHY llm_cost_per_merged_pr
            # drifted, not just THAT it did. All are sorted desc by cost_usd by
            # _compact_llm_cost_attribution.
            "dominant_model": by_model[0] if by_model else None,
            "dominant_task_type": by_type[0] if by_type else None,
            "dominant_failure_class": dominant_failure_class,
        }
    return result


def _format_fleet_utilization_axis(fleet: dict | None) -> str | None:
    """Render a fleet-utilization snapshot as a compact axis ``current`` string.

    Returns None when capacity is unknown (no enabled, capped accounts) so the
    axis is omitted rather than shipping a meaningless row. The cooled-account
    breakdown is appended only when accounts are actually cooled, keeping the
    common (healthy) case terse — the cause of low utilization (idle headroom
    vs rate-limit cooldown) is what would otherwise mislead the strategist into
    manufacturing work.
    """
    if not fleet:
        return None
    cap = fleet.get("nominal_capacity")
    if not cap:
        return None
    active = fleet.get("active_now", 0)
    util_now = fleet.get("util_now")
    util_avg = fleet.get("util_avg")
    window = fleet.get("window_hours", 24)
    now_pct = f"{round(util_now * 100)}%" if util_now is not None else "n/a"
    line = f"now {active}/{cap} ({now_pct})"
    if util_avg is not None:
        line += f", {window}h-avg {round(util_avg * 100)}%"
    cooled = fleet.get("cooled_accounts") or 0
    usable = fleet.get("usable_capacity")
    if cooled and usable is not None and usable != cap:
        line += f" — {cooled} acct cooled, {usable} usable"
    vm_cap = fleet.get("vm_cap")
    vm_slots_available = fleet.get("vm_slots_available")
    active_task_vms = fleet.get("active_task_vms")
    idle_pool_vms = fleet.get("idle_pool_vms")
    managed_vm_count = fleet.get("managed_vm_count")
    if vm_cap:
        effective = None
        if (
            active_task_vms is not None
            and managed_vm_count is not None
            and idle_pool_vms is not None
        ):
            effective = max(active_task_vms, managed_vm_count - idle_pool_vms)
        if effective is not None:
            saturated_marker = " [SATURATED]" if vm_slots_available == 0 else ""
            line += f" | vm {effective}/{vm_cap}, {vm_slots_available} free{saturated_marker}"
        elif vm_cap:
            line += f" | vm cap {vm_cap}"
    return line




# Cap on staged-direction entries surfaced to the strategist. Staged directions
# are inert next-version work; the strategist needs to see what is already queued
# so it does not re-propose the same work, but the list must stay bounded under
# the payload budget.
_STAGED_DIRECTIONS_KEEP = 12


def _build_staged_directions_summary(project: Project) -> list[dict]:
    """Bounded list of the project's currently-staged directions.

    A *staged* direction (``status == "staged"``) is inert work the strategist
    or an operator marked to begin only after the version that was open when it
    was staged has promoted — the per-direction ``staged_for_version`` marker
    records that version number. Staged directions are deliberately EXCLUDED from
    ``directions_summary`` (which ships only active/paused/proposed), so without
    this block the strategist has no view of what is already queued for the next
    version and would re-propose the same work.

    Each entry is ``{id, text, priority, staged_for_version, staged_at,
    staged_age_days}``. ``staged_at`` (the staging timestamp) and the derived
    ``staged_age_days`` let the strategist see HOW LONG each item has waited so it
    can self-correct stale staged work (retarget / cancel) instead of leaving it
    to rot — the same stall the reaper raises an operator alert for. Both are
    additive: ``staged_at`` is ``None`` and ``staged_age_days`` is ``None`` when
    the timestamp is absent or unparseable, keeping the schema backward-compatible.
    Returns ``[]`` when no directions are staged so the caller can omit the field
    on the common (nothing-staged) path.
    """
    staged = [
        d
        for d in (project.directions or [])
        if isinstance(d, dict) and d.get("status") == "staged"
    ]
    now = _utcnow()
    out: list[dict] = []
    for d in staged[:_STAGED_DIRECTIONS_KEEP]:
        staged_at_raw = d.get("staged_at")
        staged_age_days: int | None = None
        if staged_at_raw:
            try:
                staged_at = _coerce_naive_utc(
                    datetime.fromisoformat(str(staged_at_raw))
                )
                staged_age_days = max(0, (now - staged_at).days)
            except (TypeError, ValueError):
                staged_age_days = None
        out.append(
            {
                "id": d.get("id"),
                "text": d.get("text"),
                "priority": d.get("priority"),
                "staged_for_version": d.get("staged_for_version"),
                "staged_at": staged_at_raw,
                "staged_age_days": staged_age_days,
            }
        )
    return out


def _summarize_outcome_evaluation(oe: dict) -> dict:
    """Trim an outcome_evaluation payload to the strategic agent's working set.

    The biggest growth driver is ``direction_outcomes`` (one entry per active
    direction, each carrying scope_performance, type_distribution, lifetime
    aggregates, etc). Cap to top-N by activity. Top-level summaries
    (``overall_summary``, ``systems``, trend) are kept in full — they're
    aggregates that don't grow with project size.
    """
    if not isinstance(oe, dict):
        return oe
    trimmed = dict(oe)
    direction_outcomes = trimmed.get("direction_outcomes")
    if (
        isinstance(direction_outcomes, list)
        and len(direction_outcomes) > _OUTCOME_DIRECTION_OUTCOMES_KEEP
    ):
        sorted_outcomes = sorted(
            direction_outcomes,
            key=lambda d: (
                int((d or {}).get("merged") or 0) * 3
                + int((d or {}).get("failed") or 0)
                + int((d or {}).get("cycles") or 0)
            ),
            reverse=True,
        )
        trimmed["direction_outcomes"] = sorted_outcomes[
            :_OUTCOME_DIRECTION_OUTCOMES_KEEP
        ]
    return trimmed


# Per-list cap for the cycle-start reconciliation block. The block is meant to
# be a conflict-focused starting-state digest, NOT a restatement of the full
# problem model or direction list, so each conflict list is capped and the true
# total carried in ``counts`` (the renderer shows "+N more"). Twelve is enough
# to surface the conflicts that matter without ever bloating the payload.
_RECON_LIST_CAP = 12
# Prompt-facing sample cap for per-direction recent merges. Keep this bounded so
# the recent-work section stays compact; exact verdict counts are carried
# separately for reconciliation.
_RECENT_MERGED_TITLES_CAP = 10
# Char cap for any direction/gap/capability label echoed into the block.
_RECON_LABEL_MAXLEN = 120
# Maps raw last_qualification.status values to the set of canonical
# summarize_qualification_state states that legitimately arise under them.
# Used by the status_state_mismatch conflict check so a new state added to the
# classifier under an existing status is caught by tests rather than silently
# producing false conflicts in every strategic payload.
_QUAL_STATUS_STATE_MAP: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {
            "qualifying",
            "promotion_check_wait",
            "audit_crashed",
        }
    ),
    "promotion_failed": frozenset(
        {
            "promotion_failed",
            "required_check_failed",
            "protection_blocked_review",
        }
    ),
    "promoted": frozenset(
        {
            "promoted",
            "promoted_unconfirmed",
            "promotion_deploy_failed",
            "promotion_confirmation_timed_out",
        }
    ),
}
# Guard: ensure all declared states in _QUAL_STATUS_STATE_MAP are valid members of
# the authoritative QUALIFICATION_STATE_VOCAB. A mismatch (e.g., renamed/removed state,
# or a typo) trips here at module load time rather than silently causing false conflicts
# in strategic payloads downstream.
_declared_states = {state for states in _QUAL_STATUS_STATE_MAP.values() for state in states}
assert _declared_states <= QUALIFICATION_STATE_VOCAB, (
    f"_QUAL_STATUS_STATE_MAP declares invalid states not in QUALIFICATION_STATE_VOCAB: "
    f"{_declared_states - QUALIFICATION_STATE_VOCAB!r}"
)

# Name-length threshold for the long-name hygiene signal in prune candidates.
# Capability/gap names longer than this are flagged as advisory cleanup targets.
_CAPABILITY_LONG_NAME_THRESHOLD = 80
# Salient keyword buckets for domain-based grouping of live capability names.
# When 2+ capabilities share a bucket, they are advisory merge candidates.
# Keys are human-readable bucket labels; values are lowercase substrings to
# match against the capability name (first matching bucket wins).
_CAPABILITY_DOMAIN_KEYWORD_BUCKETS: dict[str, list[str]] = {
    "strategic": ["strategic"],
    "task": ["task"],
    "operator": ["operator"],
    "research": ["research"],
    "capability": ["capability"],
    "compute": ["compute"],
    "ci": ["pipeline", "release", "deploy"],
}

# GitHub-ancestry verification (in _build_recent_interventions) runs OUTSIDE any
# DB transaction and is bounded so it can never approach the 60s
# idle_in_transaction_session_timeout that previously killed the strategic cycle.
# Concurrency keeps the wall-clock low for large merged sets; the per-call
# timeout caps the worst case of a single hung GitHub request.
_ANCESTRY_CONCURRENCY = 6
_ANCESTRY_CALL_TIMEOUT_S = 15.0
# Matches an 8-hex-char short SHA in prose, but not the hex suffix of a direction
# ID (e.g. 'd-f0224ab4') — the negative lookbehind suppresses matches preceded by '-'.
_QUAL_SHA_PAT = re.compile(r"(?<!-)\b([0-9a-f]{8})\b")


def _stamp_direction_in_flight(
    direction_stats: list[dict],
    active_by_direction: dict[str, int] | None,
) -> None:
    """Stamp a per-direction ``in_flight`` task count onto each stat in place.

    Drawn from the SAME ``active_by_direction`` mapping that feeds
    :func:`_build_cycle_start_reconciliation` (``direction_pipeline[*].in_flight``)
    so the floored/summarized ``directions_summary`` line and the reconciliation
    surface can never silently disagree. The count is stamped EVEN WHEN ZERO so
    the strategist can distinguish a staffed direction (>0) from an idle one (0)
    even under extreme byte pressure where richer sections are dropped; a
    direction absent from ``active_by_direction`` defaults to 0.
    """
    active = active_by_direction or {}
    for stat in direction_stats:
        if not isinstance(stat, dict):
            continue
        did = str(stat.get("id") or "")
        stat["in_flight"] = int(active.get(did, 0) or 0)


def _build_cycle_start_reconciliation(
    direction_stats: list[dict],
    problem_model: dict | None,
    active_by_direction: dict[str, int] | None,
    held_by_direction: dict[str, int] | None = None,
    per_direction_delta: list[dict] | None = None,
    running_update: str | None = None,
    last_qual: dict | None = None,
    version_phase_block: dict | None = None,
) -> dict | None:
    """Compute one authoritative cycle-start reconciliation of the strategic surfaces.

    The strategic cycle reasons over four surfaces that can silently disagree:
    problem-model capabilities (agent-asserted), grounded design-doc/file-tree
    verification, active directions (with their durable ``gap_id`` linkage and
    deferral metadata), and each direction's research pipeline. Rather than make
    the agent hand-reconcile these every cycle, this computes the *conflicts*
    between them up front:

      * ``capability_contradictions`` — capabilities whose stored ``status``
        disagrees with the grounded file-tree check (``verification``): a
        ``working`` capability whose backing paths are all missing (``absent``)
        or only partly present (``partial``). The grounding is advisory (see
        :func:`_verify_capability_backing_paths`); a contradiction means the
        stored status is not backed by code that currently exists.
      * ``orphaned_gaps`` — open (``active`` or ``blocked``) gaps with no active
        direction pointing back via ``gap_id`` AND no intentional
        :class:`~app.problem_model.GapDeferral`. Each entry carries the gap's
        ``status`` so a drifted active gap is distinguishable from an orphaned
        blocked one.
        A gap carrying a ``deferral`` is *deliberately* unstaffed and is NOT an
        orphan — it is excluded and counted separately so the agent can tell a
        drifted gap from a parked one.
      * ``ungrounded_directions`` — ``active`` directions whose ``gap_id`` no
        longer resolves to an active gap (closed/archived/never-existed), or that
        carry no gap linkage at all. These advance no tracked gap.
      * ``direction_pipeline`` — per active direction, whether its research is
        ``research_waiting_on_action`` (findings surfaced but no task in flight
        and no fresher completion/held signal), ``tasks_in_progress`` (work
        already moving), ``held_on_dependency`` (live BLOCKED/PARKED work),
        ``recently_converted`` (freshly completed/merged work with no active
        task), or ``idle``.
      * ``merge_accounting`` — per active direction, where the merge BADGE
        (which asserts a PR shipped) disagrees with ancestry verification over
        the same recent merged-title window used for recent-work. Built from the
        tri-state ``merge_verdict`` already stamped on each
        ``per_direction_delta[*].recent_merged_titles`` entry, plus the exact
        verdict counts/sample emitted alongside that visible list (no second
        GitHub/ancestry pass): ``verified_landed`` (on the integration branch),
        ``verified_not_landed`` (badge-asserted but ancestry says NOT reachable —
        the real accounting conflict), and ``unverifiable`` (ancestry could not
        be confirmed — reported as unknown, NEVER manufactured into a
        contradiction). Only directions with at least one ``verified_not_landed``
        title are surfaced as conflicts; the landed/unverifiable tallies ride in
        ``counts`` so nothing is silently truncated.
      * ``phantom_direction_refs`` — direction IDs cited in
        ``intent.current_focus`` or ``running_update`` prose (matching
        ``d-[0-9a-f]{8}``) that do not resolve to any known direction in
        ``direction_stats``. A phantom ref means the prose narrates a direction
        that no longer exists (or was never created), silently persisting stale
        framing across cycles.
      * ``qualification_surface_conflicts`` — disagreements between the payload's
        qualification narrative surfaces (``last_qualification``, derived
        ``qualification_state``, ``promotion_readiness``, and candidate currency).
        Criterion evidence coverage remains observational rather than release
        truth. Populated only when ``version_phase_block`` is
        present (LTS split). Each conflict names the two surfaces, what each
        asserts, and a reason for the disagreement.

    Returns ``None`` when there is nothing to reconcile (no active directions and
    no conflicts), so the caller omits the field for cold-start projects. Pure
    and side-effect free — operates only on the data handed in — so the backend
    computation is directly unit-testable.
    """
    pm = problem_model if isinstance(problem_model, dict) else {}
    active_by_direction = active_by_direction or {}
    held_by_direction = held_by_direction or {}

    def _label(text: Any) -> str:
        return str(text or "")[:_RECON_LABEL_MAXLEN]

    def _count_field(container: dict[str, Any], key: str) -> int | None:
        value = container.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return max(0, value)

    # Active directions and the set of gap_ids they reference (forward linkage).
    active_dirs = [
        d
        for d in direction_stats
        if isinstance(d, dict) and d.get("status") == "active"
    ]
    active_dir_ids: set[str] = {str(d["id"]) for d in active_dirs if d.get("id")}
    referenced_gap_ids: set[str] = {
        str(d["gap_id"]) for d in active_dirs if d.get("gap_id")
    }

    gaps = pm.get("gaps") if isinstance(pm.get("gaps"), list) else []
    # Active gaps only resolve here — closed/abandoned gaps move to
    # archived_gaps (stripped from the agent view), so an unresolved gap_id is a
    # genuine dangling reference.
    active_gap_ids: set[str] = {
        str(g.get("id"))
        for g in gaps
        if isinstance(g, dict) and g.get("id") and g.get("status") == "active"
    }

    # 1. Capability ↔ grounded-verification contradictions.
    capability_contradictions: list[dict] = []
    for cap in pm.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        verification = cap.get("verification")
        if not isinstance(verification, dict):
            continue
        effective = verification.get("effective_status")
        stored = cap.get("status")
        # Only a contradiction when the grounded check DISAGREES with the stored
        # status. ``unverified`` (no backing_paths) and an effective status that
        # matches the stored one are not conflicts.
        if effective in ("absent", "partial") and effective != stored:
            missing = [
                m for m in (verification.get("missing") or []) if isinstance(m, str)
            ]
            capability_contradictions.append(
                {
                    "name": _label(cap.get("name")),
                    "stored_status": stored,
                    "effective_status": effective,
                    "missing": missing[:6],
                    "missing_total": len(missing),
                }
            )

    # 2. Orphaned open gaps (active or blocked, excluding deferred ones).
    # Blocked gaps are still open problems with no direction working them, so
    # they are orphan-eligible too — just flagged with their status.
    orphaned_gaps: list[dict] = []
    staffed_but_deferred: list[dict] = []
    deferred_gap_count = 0
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") not in ("active", "blocked"):
            continue
        gid = str(g.get("id") or "")
        staffed = bool(gid and gid in referenced_gap_ids)
        if staffed:
            if g.get("deferral"):
                staffed_but_deferred.append(
                    {
                        "gap_id": gid,
                        "name": _label(g.get("name")),
                        "priority": g.get("priority"),
                        "status": g.get("status"),
                        "deferral": g.get("deferral"),
                    }
                )
            continue
        if g.get("deferral"):
            deferred_gap_count += 1
            continue
        orphaned_gaps.append(
            {
                "gap_id": gid,
                "name": _label(g.get("name")),
                "priority": g.get("priority"),
                "status": g.get("status"),
            }
        )

    # 3. Ungrounded active directions.
    ungrounded_directions: list[dict] = []
    for d in active_dirs:
        gap_id = d.get("gap_id")
        if not gap_id:
            reason = "no_gap"
        elif str(gap_id) not in active_gap_ids:
            reason = "gap_missing"
        else:
            continue
        ungrounded_directions.append(
            {
                "direction_id": d.get("id"),
                "text": _label(d.get("text")),
                "reason": reason,
            }
        )

    # 3b. Stale active_moves: gap.active_moves references a direction that is no
    # longer active (retired, completed, deleted, or simply absent from the known
    # direction set). The forward-linkage check (ungrounded_directions) catches
    # directions whose gap_id is stale; this is the reverse: gap entries whose
    # active_moves list is stale.
    stale_active_moves: list[dict] = []
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") not in ("active", "blocked"):
            continue
        moves = [str(m) for m in (g.get("active_moves") or []) if m]
        stale = [m for m in moves if m not in active_dir_ids]
        if not stale:
            continue
        stale_active_moves.append(
            {
                "gap_id": str(g.get("id") or ""),
                "gap_name": _label(g.get("name")),
                "stale_direction_ids": stale,
            }
        )

    # 4. Per-direction research pipeline state.
    direction_pipeline: list[dict] = []
    pipeline_counts = {
        "research_waiting_on_action": 0,
        "tasks_in_progress": 0,
        "held_on_dependency": 0,
        "recently_converted": 0,
        "idle": 0,
    }
    recent_completed_by_direction: dict[str, int] = {}
    for delta in per_direction_delta or []:
        if not isinstance(delta, dict):
            continue
        did = str(delta.get("direction_id") or "")
        if not did:
            continue
        recent_titles = delta.get("recent_merged_titles")
        pr_count = (
            sum(1 for entry in recent_titles if isinstance(entry, dict))
            if isinstance(recent_titles, list)
            else 0
        )
        non_pr_count = int(delta.get("recent_non_pr_completed") or 0)
        if pr_count or non_pr_count:
            recent_completed_by_direction[did] = pr_count + non_pr_count
    for d in active_dirs:
        did = str(d.get("id") or "")
        in_flight = int(active_by_direction.get(did, 0) or 0)
        held = int(held_by_direction.get(did, 0) or 0)
        recently_completed = int(recent_completed_by_direction.get(did, 0) or 0)
        findings = len(d.get("recent_findings") or [])
        if in_flight > 0:
            state = "tasks_in_progress"
        elif held > 0:
            state = "held_on_dependency"
        elif recently_completed > 0:
            state = "recently_converted"
        elif findings > 0:
            state = "research_waiting_on_action"
        else:
            state = "idle"
        pipeline_counts[state] += 1
        direction_pipeline.append(
            {
                "direction_id": did,
                "text": _label(d.get("text")),
                "state": state,
                "in_flight": in_flight,
                "held": held,
                "recently_completed": recently_completed,
                "findings": findings,
            }
        )
    direction_pipeline.sort(
        key=lambda item: (
            {
                "tasks_in_progress": 0,
                "held_on_dependency": 1,
                "recently_converted": 2,
                "research_waiting_on_action": 3,
                "idle": 4,
            }.get(str(item.get("state")), 5),
            -int(item.get("findings") or 0),
            -int(item.get("recently_completed") or 0),
            -int(item.get("held") or 0),
            -int(item.get("in_flight") or 0),
            str(item.get("text") or ""),
        )
    )

    # 5. Merge-accounting drift: badge-asserted-shipped vs ancestry-verified.
    # Reuses the tri-state ``merge_verdict`` already stamped on each
    # ``recent_merged_titles`` entry, plus the exact per-direction verdict
    # counts/sample carried alongside that visible list (no second GitHub/
    # ancestry pass). Only a ``verified_not_landed`` title is a real
    # accounting conflict; ``unverifiable`` is tallied as unknown, never
    # manufactured into a contradiction.
    merge_accounting: list[dict] = []
    pending_rollup_accounting: list[dict] = []
    merge_landed_total = 0
    merge_not_landed_total = 0
    merge_unverifiable_total = 0
    merge_pending_in_open_rollup_total = 0
    for delta in per_direction_delta or []:
        if not isinstance(delta, dict):
            continue
        titles = delta.get("recent_merged_titles")
        titles_list = titles if isinstance(titles, list) else []
        count_block = (
            delta.get("recent_merge_verdict_counts")
            if isinstance(delta.get("recent_merge_verdict_counts"), dict)
            else None
        )
        sampled_titles = delta.get("recent_not_landed_titles")
        not_landed = (
            [_label(title) for title in sampled_titles if title is not None]
            if isinstance(sampled_titles, list)
            else []
        )
        landed_n = _count_field(count_block, "verified_landed") if count_block else None
        not_landed_n = (
            _count_field(count_block, "verified_not_landed") if count_block else None
        )
        unverifiable_n = (
            _count_field(count_block, "unverifiable") if count_block else None
        )
        pending_n = (
            _count_field(count_block, "pending_in_open_rollup") if count_block else None
        )
        if landed_n is None or not_landed_n is None or unverifiable_n is None:
            not_landed = []
            landed_n = 0
            not_landed_n = 0
            unverifiable_n = 0
            pending_n = 0
            for entry in titles_list:
                if not isinstance(entry, dict):
                    continue
                verdict = entry.get("merge_verdict")
                if verdict == "verified_not_landed":
                    not_landed.append(_label(entry.get("title")))
                    not_landed_n += 1
                elif verdict == "verified_landed":
                    landed_n += 1
                elif verdict == "pending_in_open_rollup":
                    pending_n += 1
                else:
                    # Absent/unknown verdict → unverifiable, NOT a conflict.
                    unverifiable_n += 1
        else:
            if pending_n is None:
                pending_n = 0
            if not not_landed:
                for entry in titles_list:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("merge_verdict") == "verified_not_landed":
                        not_landed.append(_label(entry.get("title")))
        merge_landed_total += landed_n
        merge_not_landed_total += not_landed_n
        merge_unverifiable_total += unverifiable_n
        merge_pending_in_open_rollup_total += pending_n
        # Surface only directions with a genuine verified-not-landed conflict.
        if not_landed_n > 0:
            merge_accounting.append(
                {
                    "direction_id": delta.get("direction_id"),
                    "direction_text": _label(delta.get("direction_text")),
                    "not_landed_titles": not_landed,
                    "landed": landed_n,
                    "not_landed": not_landed_n,
                    "unverifiable": unverifiable_n,
                }
            )
        # Surface directions with PRs pending in an open rollup branch.
        if pending_n > 0:
            pending_titles: list[str] = []
            for entry in titles_list:
                if not isinstance(entry, dict):
                    continue
                if entry.get("merge_verdict") == "pending_in_open_rollup":
                    pending_titles.append(_label(entry.get("title")))
            pending_rollup_accounting.append(
                {
                    "direction_id": delta.get("direction_id"),
                    "direction_text": _label(delta.get("direction_text")),
                    "pending_in_open_rollup": pending_n,
                    "pending_in_open_rollup_titles": pending_titles,
                }
            )
    merge_accounting.sort(
        key=lambda m: (
            -int(m.get("not_landed") or 0),
            str(m.get("direction_text") or ""),
        )
    )
    pending_rollup_accounting.sort(
        key=lambda m: (
            -int(m.get("pending_in_open_rollup") or 0),
            str(m.get("direction_text") or ""),
        )
    )

    # 6. Phantom direction-refs: prose cites a d-XXXXXXXX that resolves to nothing.
    _DIR_REF_PAT = re.compile(r"\bd-([0-9a-f]{8})\b")
    all_direction_ids: set[str] = {
        str(d["id"]) for d in direction_stats if isinstance(d, dict) and d.get("id")
    }
    _prose_to_check: list[tuple[str, str]] = []
    intent = pm.get("intent")
    if isinstance(intent, dict):
        cf = intent.get("current_focus")
        if isinstance(cf, str) and cf:
            _prose_to_check.append(("intent.current_focus", cf))
    if isinstance(running_update, str) and running_update:
        _prose_to_check.append(("running_update", running_update))
    phantom_direction_refs: list[dict] = []
    _seen_phantom: set[str] = set()
    for field_name, prose in _prose_to_check:
        for m in _DIR_REF_PAT.finditer(prose):
            ref_id = f"d-{m.group(1)}"
            if ref_id not in all_direction_ids and ref_id not in _seen_phantom:
                _seen_phantom.add(ref_id)
                phantom_direction_refs.append(
                    {"direction_id": ref_id, "cited_in": field_name}
                )

    # 7. Qualification prose-drift: prose cites a short SHA that does not match
    # the current active qualification candidate.  When the strategist embeds a
    # specific candidate commit in current_focus or running_update, that SHA
    # becomes stale the moment the qualification advances.  Scans the same prose
    # fields as the phantom-direction-ref check; only fires when last_qual carries
    # a candidate_sha so cold-start projects are never flagged.
    qualification_prose_drift: list[dict] = []
    _active_candidate_short: str | None = None
    if isinstance(last_qual, dict):
        _cand = last_qual.get("candidate_sha")
        if isinstance(_cand, str) and len(_cand) >= 8:
            _active_candidate_short = _cand[:8]
            _seen_stale: set[str] = set()
            for field_name, prose in _prose_to_check:
                for m in _QUAL_SHA_PAT.finditer(prose):
                    found_sha = m.group(1)
                    if found_sha == _active_candidate_short:
                        continue
                    if found_sha in _seen_stale:
                        continue
                    _seen_stale.add(found_sha)
                    qualification_prose_drift.append(
                        {
                            "stale_sha": found_sha,
                            "active_candidate_sha": _active_candidate_short,
                            "cited_in": field_name,
                        }
                    )

    # 8. Qualification surface coherence: cross-check the payload's own
    # qualification narrative (last_qualification record, qualification_state,
    # criterion_coverage, promotion_readiness, candidate currency) for
    # disagreements. Only runs when version_phase_block is present (LTS split).
    qualification_surface_conflicts: list[dict] = []
    if isinstance(version_phase_block, dict):
        vpb_last_qual = version_phase_block.get("last_qualification")
        vpb_qual_state = version_phase_block.get("qualification_state")
        vpb_promotion_ready = version_phase_block.get("promotion_readiness")
        vpb_candidate_currency = version_phase_block.get(
            "last_qualification_candidate_currency"
        )

        # Disagreement 1: candidate currency says stale AND last_qual carries a
        # 'promotion_failed' status. A pending+stale candidate is deliberately
        # NOT flagged (integration-branch drift during an active qualification
        # is expected); the conflict fires only when a *failed* promotion left
        # the recorded candidate pinned to a now-stale SHA instead of advancing.
        if isinstance(vpb_candidate_currency, dict) and isinstance(
            vpb_last_qual, dict
        ):
            # The currency dict stored here is the raw return of
            # ``_resolve_candidate_currency`` (verdict/resolved/current/
            # candidate_sha/head_sha) — it emits "stale" as the verdict when
            # the pinned SHA is resolved and no longer equals integration HEAD.
            is_stale = vpb_candidate_currency.get("verdict") == "stale"
            candidate_status = vpb_last_qual.get("status")
            if (
                is_stale
                and candidate_status == "promotion_failed"
            ):
                qualification_surface_conflicts.append(
                    {
                        "conflict_type": "stale_candidate_with_promotion_failed_status",
                        "description": (
                            "Candidate currency says the pinned SHA is stale/behind "
                            f"HEAD, but last_qualification.status is '{candidate_status}'. "
                            "After a failed promotion the recorded candidate should "
                            "have advanced past the now-stale SHA; a stale candidate "
                            "still carrying a promotion_failed status means the "
                            "qualification surface was not refreshed."
                        ),
                    }
                )

        # Disagreement 2: last_qualification.status vs qualification_state.
        # Skip when qualification_state_disagreement is already present — the
        # resolver documented the substitution explicitly, so the raw/derived
        # divergence is an EXPLAINED mismatch, not an unexplained conflict.
        vpb_qual_disagreement = version_phase_block.get(
            "qualification_state_disagreement"
        )
        if (
            vpb_qual_disagreement is None
            and isinstance(vpb_last_qual, dict)
            and isinstance(vpb_qual_state, dict)
        ):
            raw_status = vpb_last_qual.get("status")
            derived_state = vpb_qual_state.get("state")
            if (
                raw_status is not None
                and derived_state is not None
                and raw_status != derived_state
            ):
                expected_states = _QUAL_STATUS_STATE_MAP.get(raw_status, frozenset())
                if derived_state not in expected_states:
                    qualification_surface_conflicts.append(
                        {
                            "conflict_type": "status_state_mismatch",
                            "description": (
                                f"last_qualification.status='{raw_status}' but "
                                f"qualification_state.state='{derived_state}' "
                                "(states do not align for this status)."
                            ),
                        }
                    )

        # Disagreement 3: promotion_readiness trend vs current qualification status
        if isinstance(vpb_promotion_ready, dict) and isinstance(vpb_last_qual, dict):
            ready_state = vpb_promotion_ready.get("ready")
            qual_status = vpb_last_qual.get("status")
            # If promotion_readiness says not ready but status is
            # "promotion_failed", the surfaces contradict.
            if (
                ready_state is False
                and qual_status == "promotion_failed"
                and vpb_promotion_ready.get("is_current_readiness") is not False
            ):
                qualification_surface_conflicts.append(
                    {
                        "conflict_type": "promotion_readiness_unready",
                        "description": (
                            "Promotion readiness is not ready, but "
                            f"last_qualification.status is '{qual_status}'. "
                            "The trend advises against promotion while the "
                            "qualification is progressing."
                        ),
                    }
                )

    # Nothing to reconcile (e.g. a cold-start project): let the caller omit it.
    if (
        not active_dirs
        and not capability_contradictions
        and not orphaned_gaps
        and not staffed_but_deferred
        and not deferred_gap_count
        and not stale_active_moves
        and not merge_accounting
        and not pending_rollup_accounting
        and not phantom_direction_refs
        and not qualification_prose_drift
        and not qualification_surface_conflicts
    ):
        return None

    def _cap(items: list) -> tuple[list, int]:
        if len(items) <= _RECON_LIST_CAP:
            return items, 0
        return items[:_RECON_LIST_CAP], len(items) - _RECON_LIST_CAP

    capped_caps, caps_total = _cap(capability_contradictions)
    capped_orphans, orphans_total = _cap(orphaned_gaps)
    capped_staffed_deferred, staffed_deferred_total = _cap(staffed_but_deferred)
    capped_ungrounded, ungrounded_total = _cap(ungrounded_directions)
    capped_stale, stale_total = _cap(stale_active_moves)
    capped_pipeline, pipeline_dropped = _cap(direction_pipeline)
    capped_merge, merge_dropped = _cap(merge_accounting)
    capped_pending_rollup, pending_rollup_dropped = _cap(pending_rollup_accounting)
    capped_phantoms, phantoms_total = _cap(phantom_direction_refs)
    capped_qual_drift, qual_drift_total = _cap(qualification_prose_drift)
    capped_qual_conflicts, qual_conflicts_total = _cap(qualification_surface_conflicts)
    # Cap each surfaced direction's not-landed title list too, preserving the
    # true count (``not_landed``) so the renderer can show "+N more".
    for m in capped_merge:
        full_titles = m["not_landed_titles"]
        if len(full_titles) > _RECON_LIST_CAP:
            m["not_landed_titles"] = full_titles[:_RECON_LIST_CAP]
        shown_titles = m["not_landed_titles"]
        m["not_landed_titles_overflow"] = max(
            0, int(m.get("not_landed") or 0) - len(shown_titles)
        )
    # Cap each pending-rollup direction's title list too.
    for m in capped_pending_rollup:
        full_titles = m["pending_in_open_rollup_titles"]
        if len(full_titles) > _RECON_LIST_CAP:
            m["pending_in_open_rollup_titles"] = full_titles[:_RECON_LIST_CAP]
        shown_titles = m["pending_in_open_rollup_titles"]
        m["pending_in_open_rollup_titles_overflow"] = max(
            0, int(m.get("pending_in_open_rollup") or 0) - len(shown_titles)
        )

    return {
        "capability_contradictions": capped_caps,
        "orphaned_gaps": capped_orphans,
        "staffed_but_deferred": capped_staffed_deferred,
        "ungrounded_directions": capped_ungrounded,
        "stale_active_moves": capped_stale,
        "direction_pipeline": capped_pipeline,
        "merge_accounting": capped_merge,
        "pending_in_open_rollup": capped_pending_rollup,
        "phantom_direction_refs": capped_phantoms,
        "qualification_prose_drift": capped_qual_drift,
        "qualification_surface_conflicts": capped_qual_conflicts,
        "counts": {
            # True totals (shown + overflow) so the renderer can say "+N more".
            "capability_contradictions": len(capped_caps) + caps_total,
            "orphaned_gaps": len(capped_orphans) + orphans_total,
            "staffed_but_deferred": len(capped_staffed_deferred) + staffed_deferred_total,
            "ungrounded_directions": len(capped_ungrounded) + ungrounded_total,
            "stale_active_moves": len(capped_stale) + stale_total,
            "deferred_gaps": deferred_gap_count,
            "active_directions": len(active_dirs),
            "direction_pipeline": len(direction_pipeline),
            "direction_pipeline_overflow": pipeline_dropped,
            "pipeline_waiting_on_action": pipeline_counts["research_waiting_on_action"],
            "pipeline_tasks_in_progress": pipeline_counts["tasks_in_progress"],
            "pipeline_held_on_dependency": pipeline_counts["held_on_dependency"],
            "pipeline_recently_converted": pipeline_counts["recently_converted"],
            "pipeline_idle": pipeline_counts["idle"],
            # Merge-accounting drift: true direction-with-conflict total (shown +
            # overflow) plus aggregate landed/not-landed/unverifiable tallies.
            "merge_accounting": len(capped_merge) + merge_dropped,
            "merge_landed": merge_landed_total,
            "merge_not_landed": merge_not_landed_total,
            "merge_unverifiable": merge_unverifiable_total,
            # Pending rollup merges: PRs merged into staging branches not yet
            # squash-merged into the integration branch. These are NOT conflicts.
            "merge_pending_in_open_rollup": merge_pending_in_open_rollup_total,
            "merge_pending_in_open_rollup_accounting": len(capped_pending_rollup) + pending_rollup_dropped,
            "phantom_direction_refs": len(capped_phantoms) + phantoms_total,
            "qualification_prose_drift": len(capped_qual_drift) + qual_drift_total,
            "qualification_surface_conflicts": len(capped_qual_conflicts) + qual_conflicts_total,
        },
    }


# Direction statuses that mean the direction is no longer live work, so an active
# gap still pointing at it (via ``active_moves``) is resolved-in-fact-but-open.
# Mirrors the retired set used in research_direction_state / strategic_analysis.
_PRUNABLE_DIRECTION_STATUSES = frozenset(
    {"retired", "completed", "superseded", "expired"}
)


def _build_prune_candidates(
    problem_model: dict | None,
    directions: list | None,
    model_health: dict | None,
    now_iso: str,
) -> dict | None:
    """Concrete, named cleanup candidates — surfaced ONLY when the model is heavy.

    The problem model only grows unless the strategist explicitly closes entries;
    the size signal is advisory and there is no structural backstop. When the
    model reaches ``large`` or ``oversized`` the strategist is told to prune, but
    is given no concrete list of WHICH entries are prunable. This reuses signals
    the backend already computes to name specific entries that are resolved in
    fact but still open:

      * ``resolved_gaps`` — ``active`` gaps whose ``active_moves`` resolve to a
        direction that is retired/completed/superseded/expired (the work is done
        or abandoned) or to a direction with merged PRs (shipped). The gap was
        worked and the work landed/stopped, yet the gap is still open.
      * ``absent_capabilities`` — capabilities the grounded file-tree check marks
        ``absent`` (claimed-but-absent): the backing code does not exist, so the
        capability is a stale assertion safe to consolidate/drop.
      * ``expired_holds`` — ``active`` gaps carrying a deferral whose
        ``hold_until`` has already passed (parsed against ``now_iso``): the hold
        the strategist set has elapsed, so the gap is due for a decision.

    Returns ``None`` unless ``model_health["model_size_status"]`` is ``large`` or
    ``oversized`` (the nudge is only actionable when the model is actually heavy),
    or when no candidates surface. Strictly advisory — a suggestion list, never an
    auto-deletion. Pure and side-effect free, so it is directly unit-testable.
    Complements :func:`_build_cycle_start_reconciliation`, which surfaces
    orphaned/ungrounded items but not resolved-yet-still-open ones.
    """
    status = (model_health or {}).get("model_size_status")
    if status not in ("large", "oversized"):
        return None

    pm = problem_model if isinstance(problem_model, dict) else {}
    directions = directions or []

    def _label(text: Any) -> str:
        return str(text or "")[:_RECON_LABEL_MAXLEN]

    def _hold_expired(hold_until: Any) -> bool:
        # Tolerate missing/malformed values: only an ISO timestamp strictly
        # earlier than now counts as expired; anything unparseable is ignored.
        if not isinstance(hold_until, str) or not hold_until:
            return False
        try:
            held = datetime.fromisoformat(hold_until.replace("Z", "+00:00"))
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        # Compare in a tz-consistent frame (treat naive as UTC).
        if held.tzinfo is None:
            held = held.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return held < now

    # direction_id -> status, and whether it has any merged PRs (shipped work).
    dir_status: dict[str, str] = {}
    dir_shipped: dict[str, bool] = {}
    for d in directions:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "")
        if not did:
            continue
        dir_status[did] = str(d.get("status") or "active")
        history = d.get("research_history") or []
        # Retiring a capability gap is a ground-truth "shipped" decision, so it
        # must use the verification-aware merged count — a degraded/unverified
        # merge (test/CI verdict blocked by an environment failure) is NOT proof
        # the work landed. Mirror the rest of the system: verified_merged_count
        # per entry (with its legacy prs_merged fallback) plus the verified
        # aggregate key (falling back to total_prs_merged for pre-attribution
        # folded history).
        merged = sum(verified_merged_count(h) for h in history if isinstance(h, dict))
        agg = d.get("history_aggregate") or {}
        merged += int(
            agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
        )
        dir_shipped[did] = merged > 0

    gaps = pm.get("gaps") if isinstance(pm.get("gaps"), list) else []

    # 1. Resolved-yet-open gaps + 3. expired-hold gaps (single active-gap pass).
    resolved_gaps: list[dict] = []
    expired_holds: list[dict] = []
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") != "active":
            continue
        moves = [str(m) for m in (g.get("active_moves") or []) if m]
        retired_moves = [
            m for m in moves if dir_status.get(m) in _PRUNABLE_DIRECTION_STATUSES
        ]
        shipped_moves = [m for m in moves if dir_shipped.get(m)]
        if retired_moves or shipped_moves:
            resolved_gaps.append(
                {
                    "gap_id": str(g.get("id") or ""),
                    "name": _label(g.get("name")),
                    "reason": (
                        "direction_retired" if retired_moves else "direction_shipped"
                    ),
                    "directions": (retired_moves or shipped_moves)[:6],
                }
            )
        deferral = g.get("deferral")
        if isinstance(deferral, dict) and _hold_expired(deferral.get("hold_until")):
            expired_holds.append(
                {
                    "gap_id": str(g.get("id") or ""),
                    "name": _label(g.get("name")),
                    "hold_until": deferral.get("hold_until"),
                }
            )

    # 2. Claimed-but-absent capabilities (grounded check says the code is gone).
    absent_capabilities: list[dict] = []
    for cap in pm.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        verification = cap.get("verification")
        if not isinstance(verification, dict):
            continue
        if verification.get("effective_status") == "absent":
            absent_capabilities.append(
                {
                    "name": _label(cap.get("name")),
                    "stored_status": cap.get("status"),
                }
            )

    # 4. Ungroundable capabilities: no backing_paths, no source, no verification.
    # These are unverifiable claims that have never been grounded — different from
    # absent_capabilities which requires the grounded check to have run and found
    # the code missing. Advisory: ground/consolidate or add backing_paths.
    ungroundable_capabilities: list[dict] = []
    for cap in pm.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        backing = cap.get("backing_paths")
        if backing:  # non-empty list means grounded
            continue
        if cap.get("source"):  # claimed provenance present
            continue
        if cap.get("verification") is not None:  # already checked
            continue
        ungroundable_capabilities.append(
            {"name": _label(cap.get("name")), "status": cap.get("status")}
        )

    # 5. Domain merge groups: keyword-bucket grouping of live capability names.
    # Capabilities in the same bucket are advisory consolidation candidates.
    _domain_buckets: dict[str, list[str]] = {}
    for cap in pm.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        raw_name = str(cap.get("name") or "")
        if not raw_name:
            continue
        lower_name = raw_name.lower()
        for bucket, keywords in _CAPABILITY_DOMAIN_KEYWORD_BUCKETS.items():
            if any(kw in lower_name for kw in keywords):
                _domain_buckets.setdefault(bucket, []).append(_label(raw_name))
                break  # first matching bucket wins
    domain_merge_groups: list[dict] = [
        {"bucket": bucket, "names": names}
        for bucket, names in _domain_buckets.items()
        if len(names) >= 2
    ]

    # 6. Heavy gaps: top-N active gaps by serialized history weight (compaction targets).
    # Only gaps with non-empty history are included — empty-history gaps need no compaction.
    _gap_sizes: list[tuple[int, dict]] = []
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") != "active":
            continue
        history = g.get("history") or []
        if not isinstance(history, list) or len(history) == 0:
            continue
        approx_bytes = len(json.dumps(history))
        _gap_sizes.append(
            (
                approx_bytes,
                {
                    "gap_id": str(g.get("id") or ""),
                    "name": _label(g.get("name")),
                    "history_entry_count": len(history),
                    "approx_bytes": approx_bytes,
                },
            )
        )
    _gap_sizes.sort(key=lambda x: x[0], reverse=True)
    heavy_gaps: list[dict] = [item for _, item in _gap_sizes[:_RECON_LIST_CAP]]

    # 7. Long names: capabilities and gaps whose names exceed the threshold.
    # Oversized names inflate every payload that ships the capability vocabulary.
    long_names: list[dict] = []
    for cap in pm.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        raw_name = str(cap.get("name") or "")
        if len(raw_name) > _CAPABILITY_LONG_NAME_THRESHOLD:
            long_names.append(
                {
                    "kind": "capability",
                    "name": _label(raw_name),
                    "length": len(raw_name),
                }
            )
    for g in gaps:
        if not isinstance(g, dict):
            continue
        raw_name = str(g.get("name") or "")
        if len(raw_name) > _CAPABILITY_LONG_NAME_THRESHOLD:
            long_names.append(
                {"kind": "gap", "name": _label(raw_name), "length": len(raw_name)}
            )

    if (
        not resolved_gaps
        and not absent_capabilities
        and not expired_holds
        and not ungroundable_capabilities
        and not domain_merge_groups
        and not heavy_gaps
        and not long_names
    ):
        return None

    def _cap(items: list) -> tuple[list, int]:
        if len(items) <= _RECON_LIST_CAP:
            return items, 0
        return items[:_RECON_LIST_CAP], len(items) - _RECON_LIST_CAP

    capped_resolved, _ = _cap(resolved_gaps)
    capped_absent, _ = _cap(absent_capabilities)
    capped_holds, _ = _cap(expired_holds)
    capped_ungroundable, _ = _cap(ungroundable_capabilities)
    capped_long_names, _ = _cap(long_names)

    return {
        "model_size_status": status,
        "resolved_gaps": capped_resolved,
        "absent_capabilities": capped_absent,
        "expired_holds": capped_holds,
        "ungroundable_capabilities": capped_ungroundable,
        "domain_merge_groups": domain_merge_groups,
        "heavy_gaps": heavy_gaps,
        "long_names": capped_long_names,
        "counts": {
            # True totals (uncapped) so the renderer can say "+N more".
            "resolved_gaps": len(resolved_gaps),
            "absent_capabilities": len(absent_capabilities),
            "expired_holds": len(expired_holds),
            "ungroundable_capabilities": len(ungroundable_capabilities),
            "domain_merge_groups": len(domain_merge_groups),
            "heavy_gaps": len(heavy_gaps),
            "long_names": len(long_names),
        },
    }


# Threshold for gap history + past_moves entry count that triggers a
# compaction hint. Gaps whose combined entries reach this level are candidates
# for ``_compactions`` watermark compaction — the strategist should review and
# emit a compaction directive rather than letting the history grow unbounded.
_COMPACTION_HISTORY_ENTRY_THRESHOLD = 15


def _build_compaction_hints(problem_model: dict | None) -> list[dict] | None:
    """Identify active gaps warranting ``_compactions`` watermark compaction.

    Strategic cycles accumulate problem-model gap history entries over time;
    this returns a list of hints — one per active gap whose combined history
    and past_moves entries exceed the threshold — so the strategist can apply
    targeted compaction without exhaustive manual inspection.

    Returns None when no gap qualifies (no field emitted on the payload).
    Pure and side-effect free.
    """
    pm = problem_model if isinstance(problem_model, dict) else {}
    hints: list[dict] = []
    for g in pm.get("gaps") or []:
        if not isinstance(g, dict) or g.get("status") != "active":
            continue
        history = g.get("history")
        if not isinstance(history, list):
            history = []
        past_moves = g.get("past_moves")
        if not isinstance(past_moves, list):
            past_moves = []
        total = len(history) + len(past_moves)
        if total >= _COMPACTION_HISTORY_ENTRY_THRESHOLD:
            hints.append(
                {
                    "gap_id": str(g.get("id") or ""),
                    "name": str(g.get("name") or "")[:_RECON_LABEL_MAXLEN],
                    "history_entry_count": len(history),
                    "past_moves_count": len(past_moves),
                    "total_entries": total,
                }
            )
    return hints if hints else None


_OPERATOR_STEERING_QUOTE_MAX_CHARS = 600
_OPERATOR_STEERING_QUOTE_COUNT = 2


def _build_operator_steering_directive(
    conversation_thread: list, *, fallback_excerpt: str | None = None
) -> str | None:
    """High-priority steering directive for an operator-woken strategic cycle.

    When a strategic cycle is spawned *because* an operator-origin
    strategic-refresh marker was armed, the operator's steering message would
    otherwise appear only as one undifferentiated entry in the (capped)
    ``conversation_thread`` snapshot. This selects the most recent operator
    steering message(s) from that same thread — ``from == 'user'``, excluding
    passive ``NOTE``s and system/routine digest events via the shared
    ``classify_operator_arming`` gate so "steering" means exactly what arming
    meant — and frames them as the reason this cycle exists, so the strategist
    treats them as a priority directive rather than one of N messages.

    ``fallback_excerpt`` is the operator message text persisted on the refresh
    marker at arm time. When the live thread snapshot carries no qualifying
    operator steering message (e.g. it aged out of the recent-messages window
    before the deferred cycle reached its review phase), the directive is
    synthesized from this durable excerpt instead — so an operator wake never
    silently loses its steering. Returns ``None`` only when neither the thread
    nor the fallback yields any steering text, in which case the caller appends
    no steering framing.
    """
    steering_msgs = []
    for m in conversation_thread:
        if m.get("from") != "user":
            continue
        try:
            mtype = m.get("type")
            if not isinstance(mtype, MessageType):
                mtype = MessageType(mtype)
            event_type = (m.get("context") or {}).get("event_type")
            if (
                classify_operator_arming(mtype, event_type=event_type)
                != ARMING_STEERING
            ):
                continue
        except Exception:  # noqa: BLE001 — fail open: treat as steering
            # Unknown/malformed type — treat as steering rather than silently
            # dropping a potential directive.
            logger.debug(
                "[STRATEGIC] message arming classification failed; treating as steering",
                exc_info=True,
            )
        steering_msgs.append(m)
    # conversation_thread is chronological (oldest→newest); the steering
    # message(s) that woke this cycle are the most recent, so quote the tail.
    quoted = []
    for m in steering_msgs[-_OPERATOR_STEERING_QUOTE_COUNT:]:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _OPERATOR_STEERING_QUOTE_MAX_CHARS:
            content = content[:_OPERATOR_STEERING_QUOTE_MAX_CHARS] + "…"
        quoted.append(f"  > {content}")
    # Thread-aging fallback: the live snapshot yielded no operator message, but
    # the excerpt was durably persisted on the refresh marker at arm time. Build
    # the directive from it using the same truncation/framing as a found message.
    if not quoted and fallback_excerpt:
        content = fallback_excerpt.strip()
        if content:
            if len(content) > _OPERATOR_STEERING_QUOTE_MAX_CHARS:
                content = content[:_OPERATOR_STEERING_QUOTE_MAX_CHARS] + "…"
            quoted.append(f"  > {content}")
    if not quoted:
        return None
    return (
        "OPERATOR STEERING — THIS CYCLE WAS TRIGGERED BY OPERATOR STEERING.\n"
        "An operator sent a live-chat steering message that woke this strategic "
        "cycle; acting on that steering is the reason this cycle exists. Treat "
        "the operator message(s) below as a priority directive, NOT as one of "
        "the undifferentiated conversation_thread entries:\n" + "\n".join(quoted)
    )




# Schemas for pack_under_budget. Each key declares its priority tier and an
# optional summarize_fn. Critical = always shipped (raises if can't fit).
# High = shipped unless budget exhausted by summarized criticals + earlier
# high items. Medium and low get progressively more aggressive trimming.
#
# When new fields are added to the strategic payload, register them here or
# the packer will warn (and treat them as low). A registered key with no
# summarize_fn is dropped wholesale rather than trimmed when over budget.
_STRATEGIC_REVIEW_SCHEMA: dict[str, dict] = {
    # Identity / routing — must always reach the agent
    "type": {"priority": "critical"},
    "research_mode": {"priority": "critical"},
    "project_name": {"priority": "critical"},
    "base_branch": {"priority": "critical"},
    # Non-behavioral schema key registration for serialized payload field (not a
    # self-repo gate). Exempt from deploy-identity de-gating: registering the
    # priority tier of the packed is_self_repo field, not reading project.is_self_repo.
    "is_self_repo": {"priority": "critical"},
    "enable_self_improvement": {"priority": "critical"},
    "model": {"priority": "critical"},
    "effort": {"priority": "critical"},
    # Resident-only Fast activation is a tiny server-owned launch contract.
    # It must survive packing because the runner starts from this payload.
    "typed_fast_enabled": {"priority": "critical"},
    "typed_fast_models": {"priority": "critical", "exempt": True},
    "typed_fast_effort": {"priority": "critical"},
    "typed_fast_qualification": {"priority": "critical"},
    # Trim-proof authoritative direction counts (true active/paused/total computed
    # before any trimming) and the operator's real max-active ceiling. Critical
    # tier with no summarize_fn: tiny scalars the packer ships verbatim, so the
    # agent's direction-health/portfolio/list views can anchor their headline
    # count, slots line, and gap flags on the true active set even when the
    # heavier ``directions_summary`` detail list is trimmed or dropped for budget.
    "direction_counts": {"priority": "critical"},
    "max_active_directions": {"priority": "critical"},
    # Trim-proof authoritative count of unresolved open operator messages computed
    # BEFORE packing. Critical tier (tiny scalar) so the agent sees the true inbox
    # depth even when ``open_messages`` itself is trimmed/dropped under byte budget.
    "open_message_count": {"priority": "critical"},
    "prompt_suffix": {"priority": "high", "exempt": True},
    # Vision + intent — small but load-bearing for every decision
    "vision": {"priority": "critical"},
    "effective_purpose": {"priority": "critical"},
    # User-facing state the agent must respond to. Core section: the open-message
    # inbox has BOTH a summarize fallback AND a floor callback, so at least its
    # floored form (top survival-priority items, tight excerpts) ALWAYS ships —
    # the whole inbox is never dropped wholesale under budget pressure.
    "open_messages": {
        "priority": "high",
        "summarize": _summarize_open_messages,
        "floor": _floor_open_messages,
        "fetch_verb": "message",
    },
    # Recently-answered operator conversation tail (non-actionable). Already
    # hard-bounded at build time (≤8 messages, per-message excerpt cap), so the
    # packer drops it whole under byte pressure rather than trimming — a coherence
    # aid, never load-bearing, so a low priority + exempt bound is correct.
    "recent_handled_conversation": {"priority": "medium", "exempt": True},
    "running_update": {"priority": "high", "exempt": True},
    "running_update_acknowledged_at": {"priority": "high", "exempt": True},
    "user_last_active_at": {"priority": "high", "exempt": True},
    # Timestamp when the first autonomous improvement (create/fix/refactor task)
    # merged on external projects. Absent for self-repo and projects with no
    # merged autonomous improvements yet. High priority, exempt: tiny scalar.
    "first_autonomous_improvement_at": {"priority": "high", "exempt": True},
    # Bounded summary of the operator's recent reconciliation overrides (what the
    # agent proposed vs. what the operator kept, per document). Medium priority
    # with no summarize_fn: it is already capped + char-bounded at build time, so
    # the packer drops it whole under budget rather than trimming. Closes the
    # operator-wins reconciliation loop so the agent reconciles instead of
    # re-proposing overridden values. Summarize fallback keeps the newest few
    # entries (routing keys + tightened values) instead of dropping the whole
    # ledger; full detail is reconstructable from the live documents.
    "operator_overrides": {
        "priority": "medium",
        "summarize": _summarize_operator_overrides,
    },
    # Recent operator-selected Strategic workbench pointers promoted out of
    # ProjectMessage.context.strategic_session so selected refs survive when the
    # conversation thread is trimmed. Already capped and pointer-only.
    "operator_context_refs": {"priority": "high", "exempt": True},
    # Computed cycle-start reconciliation of the four strategic surfaces
    # (capabilities vs grounding, gaps vs directions, per-direction research
    # pipeline). Conflict-focused and bounded at build time, so it ships whole
    # or not at all. High priority: it is the authoritative starting state the
    # agent reasons from, and it is small.
    "cycle_start_reconciliation": {"priority": "high", "exempt": True},
    # Core working set (the big stuff) — summarize when over budget. Core
    # section: the floor callback guarantees a compact active-direction skeleton
    # (top directions, id/status/headline only) ALWAYS ships, so per-direction
    # detail degrades before the active-direction core disappears entirely.
    "directions_summary": {
        "priority": "high",
        "summarize": _summarize_directions,
        "floor": _directions_summary_floor,
        "fetch_verb": "direction",
    },
    # Directions already queued for the NEXT version (status=staged). Medium
    # priority with no summarize_fn: it is already capped at build time, so the
    # packer drops it whole under budget rather than trimming. Surfaced so the
    # strategist knows what is already staged and does NOT re-propose it.
    "staged_directions": {"priority": "medium", "exempt": True},
    "problem_model": {
        "priority": "high",
        "summarize": _agent_view_problem_model_packed,
        # Floor reservation = the WHOLE agent-view model. The packer reserves the
        # full model's bytes off the top of the budget so every other recoverable
        # sibling degrades or drops BEFORE the problem_model is ever reduced —
        # under normal cycle sizes the whole model ships intact and the
        # skeletonizing summarize path never fires. Only when the full model
        # cannot fit at all does the reservation exceed budget; the packer then
        # drops the floor guarantee and the summarize callback degrades the model
        # loudly (marker + _packer_meta record). The floor callback is a simple,
        # independent function that cannot fail, so the reservation is honored
        # even if the summarize callback raises an exception.
        "floor": _agent_view_problem_model_floor,
        "fetch_verb": "problem-model",
    },
    # Tiny, load-bearing soft size/health signal computed from the DURABLE
    # problem_model (honest counts, not the trimmed agent view). High priority,
    # exempt — it should only drop under genuine budget exhaustion.
    "model_health": {"priority": "high", "exempt": True},
    # Concrete cleanup candidates surfaced only when the model is large/oversized.
    # High priority so the block survives the same payload pressure that triggered
    # the large/oversized status — a medium-priority block would be dropped under
    # the exact budget conditions where the candidates are most actionable.
    "prune_candidates": {"priority": "high", "exempt": True},
    # Compact index of all playbook keys — survives budget pressure so the
    # strategist always knows what operational knowledge exists and can fetch
    # entries on demand via `./fetch playbook <key>`. High priority to persist
    # under budget pressure, even if the heavier playbook_summary is dropped.
    "playbook_keys_index": {"priority": "high", "exempt": True},
    # Compact, bounded navigation index over the grounded agent-view design doc
    # (one entry per non-internal section: key + bounded summary + reconciled
    # presence status/modules/warning for systems/capabilities). The full
    # per-section content is fetched on demand via POST /api/agent/design-doc
    # (op:get) — see _design_doc_index. The summarize fallback drops per-entry
    # summaries to a navigation skeleton under genuine budget exhaustion.
    "design_doc": {"priority": "medium", "summarize": _summarize_design_doc_index, "fetch_verb": "section"},
    # Compact, body-omitted index of existing knowledge documents (web_research /
    # design output). Medium priority. Summarize fallback keeps the newest few
    # entries with their navigation pointers (id/title/topic/direction) and drops
    # the heavy per-arm-verdict / provenance sub-fields; full bodies remain
    # fetchable on demand via ``./fetch knowledge <id>``.
    "knowledge_index": {
        "priority": "medium",
        "summarize": _summarize_knowledge_index,
        "fetch_verb": "knowledge",
    },
    # Pending knowledge follow-ups: design/web_research docs the strategist
    # hasn't consumed/dismissed yet. Already bounded at build time (max 20
    # entries, summaries truncated to ~200 chars), so exempt from the packer.
    "pending_followups": {
        "priority": "medium",
        "exempt": True,
        "fetch_verb": "knowledge",
    },
    # Per-sweep comparative arm grouping — bounded at build time so the packer
    # drops it whole under budget rather than trimming. Same tier as
    # knowledge_index.
    "compute_experiment_sweeps": {"priority": "medium", "exempt": True},
    "capability_trial_verdicts": {
        "priority": "medium",
        "summarize": _summarize_capability_trial_verdicts,
    },
    "outcome_evaluation": {
        "priority": "medium",
        "summarize": _summarize_outcome_evaluation,
    },
    "recent_interventions": {
        "priority": "medium",
        "summarize": _summarize_recent_interventions,
    },
    "conversation_thread": {
        "priority": "medium",
        "summarize": _summarize_conversation_thread,
    },
    # Long-tail context — first to be dropped when budget is tight.
    # All exempt: already bounded at build time (list caps + char limits applied
    # before packing), so drop-whole is correct degradation for these low-priority
    # sections. Fuller detail is fetchable via playbook/direction fetch verbs.
    "self_reflection_log": {
        "priority": "low",
        "summarize": _summarize_self_reflection_log,
    },
    # Already compact (note + set_at per entry, capped by the handler at build
    # time), so drop-whole is correct degradation under budget pressure.
    "strategic_memory_notes": {
        "priority": "low",
        "exempt": True,
    },
    "previous_decision_outcomes": {
        "priority": "low",
        "summarize": _summarize_previous_decision_outcomes,
        # Recoverable via the exact section-fetch endpoint (see
        # SECTION_FETCH_INVENTORY "sections"); a broad ./fetch direction cannot
        # return this section summary. Keep in sync with the inventory.
        "fetch_verb": "sections",
    },
    "anomaly_summary": {"priority": "low", "exempt": True},
    "playbook_summary": {
        "priority": "low",
        "summarize": _summarize_playbook_summary,
        "fetch_verb": "playbook",
    },
    "rejected_proposals": {"priority": "low", "exempt": True},
    "running_update_superseded": {"priority": "low", "exempt": True},
    "prediction_calibration_factor": {"priority": "low", "exempt": True},
    "prediction_calibration_note": {"priority": "low", "exempt": True},
    # Mode-aware additions: only present when non-empty
    "system_maturity": {"priority": "low", "exempt": True},
    # drift_signals is actionable mode-switch input (audit/pause triggers), so it
    # must survive budget pressure — promoted to medium alongside conversation_thread.
    "drift_signals": {"priority": "medium", "summarize": _summarize_drift_signals},
    # Lessons from retired directions — helps the agent avoid repeating patterns.
    # Summarize fallback keeps the newest few critiques (direction_id + truncated
    # lesson fields) rather than dropping the whole feed.
    "past_retirement_critiques": {
        "priority": "low",
        "summarize": _summarize_past_retirement_critiques,
        # Recoverable via the exact section-fetch endpoint (SECTION_FETCH_INVENTORY
        # "sections"), not a per-direction fetch. Keep in sync with the inventory.
        "fetch_verb": "sections",
    },
    # Raw retired-direction context (metrics + failed_approaches). Lower priority
    # than past_retirement_critiques (curated) — dropped first when budget is tight.
    # Recoverable via the exact section-fetch endpoint (SECTION_FETCH_INVENTORY
    # "sections"), not a per-direction fetch. Keep in sync with the inventory.
    "retired_directions_summary": {
        "priority": "low",
        "exempt": True,
        "fetch_verb": "sections",
    },
    # Bounded fail-closed retirement deferrals from the prior cycle. High
    # priority so a supported/unsupported retirement second opinion stays
    # visible to the next strategist pass instead of getting dropped behind
    # lower-value long-tail context.
    "retirement_deferrals": {"priority": "high", "exempt": True},
    # LTS version/phase context — present only on self-repo projects. High priority
    # so it survives budget pressure (the agent needs it for phase-transition decisions).
    "version_phase": {
        "priority": "high",
        "summarize": _summarize_version_phase,
        "floor": _version_phase_floor,
    },
    # Cycle-start SHA divergence check: present only when _resolve_candidate_currency
    # resolves.  Small dict of scalars — no summarize_fn, drops whole under extreme
    # pressure.  High priority so it ships alongside version_phase.
    "payload_staleness": {"priority": "high", "exempt": True},
    # What is actually deployed (running live on the promotion target) vs what
    # has merged to the integration branch but is not yet promoted. High
    # priority: it is load-bearing for every runtime-metric judgment, and small.
    "deployment_state": {"priority": "high", "exempt": True},
    # Citable memo digest — terminal reasoning artifacts spawned from dropped research
    # findings. Medium priority so it survives typical payload pressure; the summarize_fn
    # trims memos and excerpts so it degrades gracefully (fewer memos / shorter text)
    # rather than vanishing entirely. The strategic reviewer must see memos to cite them.
    "memo_citations": {"priority": "medium", "summarize": _summarize_memo_citations},
    # Per-axis efficiency axes with drift vs baseline. Medium priority so it
    # survives typical payload pressure; the summarize_fn trims history tails
    # so it degrades gracefully before being dropped entirely.
    "efficiency_snapshot": {
        "priority": "medium",
        "summarize": _summarize_efficiency_snapshot,
    },
    # CI-compliance signal for the current integration branch HEAD: module-size
    # guardrail breaches and branch-literal violations. Low priority — advisory
    # signal that degrades before load-bearing state. Dropped whole under budget;
    # full detail fetchable via ./fetch ci-compliance.
    "ci_compliance": {"priority": "low", "exempt": True},
    # Open completion-debt ledger: durable unfinished-work obligations from
    # default-OFF feature merges, observe-mode knobs, and retirement residuals.
    # Medium priority, exempt: bounded by the ledger query cap at build time.
    # Fetchable via ./fetch completion-debt when dropped under budget pressure.
    "open_completion_debt": {"priority": "medium", "exempt": True},
    # Held-directions block: directions that have hit the consecutive-failure
    # hold threshold. Low priority — purely observational planning context the
    # strategic agent reads to decide whether to refine/retire a stuck direction.
    "held_directions": {"priority": "low", "exempt": True},
    # Aggregated missing-foundation demand (capability/foundation prerequisites
    # declared by dependents that no task provides). Medium priority, exempt:
    # bounded at build time by collect_missing_capability_demand.
    "missing_capability_demand": {"priority": "medium", "exempt": True},
    # Shared capability vocabulary (canonical registry name + status) the strategist
    # draws prerequisite / provides_capability refs from. Small and bounded; exempt.
    "available_capabilities": {"priority": "medium", "exempt": True},
    # Live local-runner availability (ever_registered, active_total, active_gpu,
    # require_local_runner, plus a bounded per-live-runner `devices` hardware
    # list — GPU models/VRAM/container capability/load — capped per-runner and
    # in total under the experiment-authoring shared budget, see
    # app.experiment_authoring_context). Surfaced only when the project has
    # ever registered a runner OR declares the enforce-local policy. Medium
    # priority, exempt: small and bounded at build time — the strategist uses
    # it to avoid authoring GPU experiments that would fall back to billable
    # cloud GPU (or be held indefinitely when require_local_runner is set) and
    # to size experiments to the fleet's actual VRAM/device counts.
    "local_runner_status": {"priority": "medium", "exempt": True},
    # Persisted integrity-conflict trend over the last 10 completed cycles.
    # Three scalars (stamped_cycles, consecutive_clean_cycles) + last_counts
    # dict. Low priority, exempt: absent until the first stamped cycle lands and
    # tiny once present. Drives the '2 consecutive clean cycles' retirement
    # condition for the direction-gap referential-integrity work. Fetchable on
    # demand via ./fetch integrity-conflict-trend.
    "integrity_conflict_trend": {"priority": "low", "exempt": True},
}


_DESIGN_DOC_UPDATE_SCHEMA: dict[str, dict] = {
    "type": {"priority": "critical"},
    "project_name": {"priority": "critical"},
    "base_branch": {"priority": "critical"},
    "model": {"priority": "critical"},
    "effort": {"priority": "critical"},
    "vision": {"priority": "critical"},
    "effective_purpose": {"priority": "critical"},
    # Trim-proof authoritative count of unresolved open operator messages — see
    # _STRATEGIC_REVIEW_SCHEMA["open_message_count"]. Critical tier so the agent
    # sees the true inbox depth even when open_messages is dropped under budget.
    "open_message_count": {"priority": "critical"},
    # Core section — see _STRATEGIC_REVIEW_SCHEMA["open_messages"]: floor callback
    # guarantees the operator inbox never drops wholesale under budget pressure.
    "open_messages": {
        "priority": "high",
        "summarize": _summarize_open_messages,
        "floor": _floor_open_messages,
        "fetch_verb": "message",
    },
    # See _STRATEGIC_REVIEW_SCHEMA: compact pointer-only operator context. Exempt:
    # bounded at build time (short ref strings, no unbounded growth).
    "operator_context_refs": {"priority": "high", "exempt": True},
    # Compact, bounded navigation index over the grounded agent-view design doc
    # — see _STRATEGIC_REVIEW_SCHEMA["design_doc"] and _design_doc_index. Full
    # per-section content is fetched on demand via POST /api/agent/design-doc.
    "design_doc": {"priority": "high", "summarize": _summarize_design_doc_index, "fetch_verb": "section"},
    "problem_model": {
        "priority": "high",
        "summarize": _agent_view_problem_model_packed,
        # Floor reservation = the WHOLE agent-view model. The packer reserves the
        # full model's bytes off the top of the budget so every other recoverable
        # sibling degrades or drops BEFORE the problem_model is ever reduced —
        # under normal cycle sizes the whole model ships intact and the
        # skeletonizing summarize path never fires. Only when the full model
        # cannot fit at all does the reservation exceed budget; the packer then
        # drops the floor guarantee and the summarize callback degrades the model
        # loudly (marker + _packer_meta record). The floor callback is a simple,
        # independent function that cannot fail, so the reservation is honored
        # even if the summarize callback raises an exception.
        "floor": _agent_view_problem_model_floor,
        "fetch_verb": "problem-model",
    },
    # See _STRATEGIC_REVIEW_SCHEMA: tiny, load-bearing soft size/health signal.
    # Exempt: small and bounded (a single computed dict).
    "model_health": {"priority": "high", "exempt": True},
    # Exempt: bounded at build time (list cap applied by _build_proposed_changes).
    "proposed_changes": {"priority": "high", "exempt": True},
    # Exempt: capped at 50 merged tasks at build time.
    "shipped_changes_manifest": {"priority": "medium", "exempt": True},
    # Advisory prompt hint framing missing-foundation demand to act on. High
    # priority so the directive survives budget pressure (it is small).
    "prompt_suffix": {"priority": "high", "exempt": True},
    # Aggregated missing-foundation demand — see _STRATEGIC_REVIEW_SCHEMA. Medium
    # priority; exempt: bounded at build time by collect_missing_capability_demand.
    "missing_capability_demand": {"priority": "medium", "exempt": True},
    # Compact, body-omitted index of existing knowledge documents — see
    # _STRATEGIC_REVIEW_SCHEMA["knowledge_index"]. Summarize fallback degrades to
    # the newest few navigation pointers rather than dropping the whole index.
    "knowledge_index": {
        "priority": "medium",
        "summarize": _summarize_knowledge_index,
    },
    # Pending knowledge follow-ups (design/web_research) — exempt: bounded at build time.
    "pending_followups": {"priority": "medium", "exempt": True},
    # Per-sweep comparative arm grouping — exempt: bounded at build time.
    "compute_experiment_sweeps": {"priority": "medium", "exempt": True},
    # Shared capability vocabulary — see _STRATEGIC_REVIEW_SCHEMA. Exempt: small, bounded.
    "available_capabilities": {"priority": "medium", "exempt": True},
}


async def _build_shipped_changes_manifest(
    project: Project, session: AsyncSession
) -> list[dict]:
    """Build a manifest of merged tasks since the last architecture review.

    Uses design_doc._meta.last_updated as the cutoff, falling back to the
    most recent phase1_completed_at from the StrategicCycle table.
    Returns a list of direction-grouped dicts, capped at 50 merged tasks.
    """

    # Determine cutoff timestamp.
    # Task.updated_at is TIMESTAMP WITHOUT TIME ZONE — the cutoff must be naive
    # UTC or asyncpg raises "can't subtract offset-naive and offset-aware
    # datetimes" when binding the parameter.
    def _parse_naive_utc(ts: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except (ValueError, TypeError):
            return None

    cutoff: datetime | None = None
    last_updated = (project.design_doc or {}).get("_meta", {}).get("last_updated")
    if last_updated:
        cutoff = _parse_naive_utc(last_updated)

    if cutoff is None:
        # Audit W2: cycle history is the StrategicCycle table now. Walk
        # newest-first; the helper returns rows in that order.
        recent_cycles = await strategic_cycle.all_for_project(
            session, project.id, limit=20
        )
        for c in recent_cycles:
            if c.phase1_completed_at:
                cutoff = c.phase1_completed_at
                break

    # Query completed tasks updated after cutoff (filter pr_merged in Python)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
        )
        .order_by(Task.updated_at.desc())
        .limit(200)
    )
    if cutoff is not None:
        stmt = stmt.where(Task.updated_at > cutoff)

    result = await session.execute(stmt)
    completed_tasks = result.scalars().all()

    # Filter to merged tasks and collect manifest entries (cap at 50).
    # Only include root tasks (parent_task_id is None): rollup children merge to a
    # rollup staging branch, not the integration branch, so they have not "shipped"
    # until the rollup parent itself merges. Including children would tell the design
    # doc agent that work shipped when it only reached staging.
    manifest_entries: list[dict] = []
    for t in completed_tasks:
        ctx = t.context or {}
        if not ctx.get("pr_merged") or t.parent_task_id is not None:
            continue
        entry: dict = {
            "title": t.title,
            "direction_id": ctx.get("direction_id"),
            "changed_files": ctx.get("_pr_changed_files") or [],
            "scope": ctx.get("scope"),
        }
        manifest_entries.append(entry)
        if len(manifest_entries) >= 50:
            break

    if not manifest_entries:
        return []

    # Group by direction_id
    by_direction: dict[str, dict] = {}
    for entry in manifest_entries:
        dir_id = entry["direction_id"] or "unassigned"
        if dir_id not in by_direction:
            by_direction[dir_id] = {
                "direction_id": entry["direction_id"],
                "tasks": [],
                "all_changed_files": [],
            }
        by_direction[dir_id]["tasks"].append(
            {
                "title": entry["title"],
                "scope": entry["scope"],
                "changed_files": entry["changed_files"],
            }
        )
        for f in entry["changed_files"]:
            if f not in by_direction[dir_id]["all_changed_files"]:
                by_direction[dir_id]["all_changed_files"].append(f)

    return list(by_direction.values())


_TRIVIAL_ROLLUP_RE = None  # lazy: re module imported inline


def _rollup_body_is_trivial(body: str | None) -> bool:
    """Rollup PR bodies usually start with 'Rollup of N subtasks...' — that
    string carries no information about what actually shipped. Flag so callers
    can substitute per-subtask detail instead."""
    if not body:
        return True

    return bool(re.match(r"^\s*Rollup of \d+ subtasks?\b", body))


def _recent_commit_messages_match_task(
    messages: list[str],
    *,
    title: str,
    pr_numbers: set[int],
) -> bool:
    """Return True when a bounded commit-message sample names this task.

    PR-number references are the primary matching evidence. If PR numbers are
    available, only they can rescue a task. Title substring matching is used
    only as a fallback when no PR numbers are available for the task.
    """

    # Primary: Check PR numbers first — most reliable evidence.
    for pr_number in pr_numbers:
        pr_ref_re = re.compile(rf"(?<![\w#])#{pr_number}(?!\d)")
        if any(pr_ref_re.search(message) for message in messages):
            return True

    # Fallback: Check title substring only if no PR numbers are available.
    # If PR numbers exist but don't match, don't use title to avoid false
    # rescues from similar/repeated task titles.
    if not pr_numbers:
        normalized_messages = [" ".join(m.casefold().split()) for m in messages]
        normalized_title = " ".join((title or "").casefold().split())
        if len(normalized_title) >= 8 and any(
            normalized_title in message for message in normalized_messages
        ):
            return True

    return False


async def _build_recent_interventions(
    project: Project,
    session: AsyncSession,
    limit: int = 15,
    lookback_days: int = 21,
    last_strategic_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a rolling-window view of recent merged work.

    Replaces the prior weekly_rates view because weekly buckets are too coarse
    for this pipeline's velocity (20-40 tasks/day → 150-300/week). Weekly
    signal arrives 7 days late and noise dominates small samples. The rolling
    windows let the strategic agent reason about what's happening NOW vs the
    recent trend without waiting for week-end.

    Returns:
      - merges: last `limit` merged tasks with pr_title, pr_number, body
        (rollup body if non-trivial; else a synthesized summary from
        subtask titles), direction_id, direction_text, merged_at, hours_ago
      - rolling_rates: create/merge/rate for multiple windows:
        last_24h, last_3d, last_7d, since_last_strategic
      - per_direction_delta: for each direction, what shipped in each window
      - lookback_days: the outer data window
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    # Phase F3 (H3): bound the per-project lookback scan. With a 14-day default
    # window and 20-40 tasks/day, a busy project hits 280-560 rows; the cap at
    # 500 keeps the working set bounded for projects that burst higher. Order
    # newest-first so a truncated result still reflects the most recent rolling
    # windows (last_24h, last_3d, last_7d, since_last_strategic) — those rolling
    # buckets are the load-bearing outputs of this function. Drop heavy JSONBs
    # we don't read here (plan_output) via load_only, but keep status_log so
    # the Task event listeners (models.py ~360) don't trip MissingGreenlet under
    # async sessions even on read-only access.
    tasks_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.created_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.created_at,
                Task.updated_at,
                Task.context,
                Task.payloads,
                Task.parent_task_id,
                Task.status_log,
                # title + description are read by _build_recent_interventions
                # (lines ~613, 629, 638) when constructing the merge summary the
                # strategic agent consumes. Excluding them caused MissingGreenlet
                # at lazy-load time when this function ran from
                # task_completion._handle_design_doc_completion → spawn fails
                # silently and the strategic cycle stalls.
                Task.title,
                Task.description,
                # project_id is read by the stranded-PR discount path
                # (direction_outcomes.discount_stranded_pr_from_direction_credit,
                # invoked from the cycle-start reconciliation below). Excluding
                # it raised InvalidRequestError under raiseload=True and aborted
                # the entire per-project autonomous spawn cycle.
                Task.project_id,
                raiseload=True,
            )
        )
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    tasks_in_window = list(tasks_result.scalars().all())
    if len(tasks_in_window) >= 500:
        logger.warning(
            "[STRATEGIC] creation_velocity truncated to 500 rows for project=%s "
            "(lookback=%dd) — extend lookback OR add LIMIT-aware logic if the "
            "rolling rates need to span a busier window",
            project.id,
            lookback_days,
        )
    active_tasks_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status.in_(_IN_FLIGHT_TASK_STATUSES),
        )
        .options(
            load_only(Task.id, Task.status, Task.context, Task.payloads, raiseload=True)
        )
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    active_tasks = list(active_tasks_result.scalars().all())
    if len(active_tasks) >= 500:
        logger.warning(
            "[STRATEGIC] active_tasks truncated to 500 rows for project=%s "
            "— active_by_direction/active_by_scope metrics may be incomplete",
            project.id,
        )
    held_tasks_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status.in_((TaskStatus.BLOCKED, TaskStatus.PARKED)),
        )
        .options(load_only(Task.id, Task.status, Task.context, raiseload=True))
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    held_tasks = list(held_tasks_result.scalars().all())
    if len(held_tasks) >= 500:
        logger.warning(
            "[STRATEGIC] held_tasks truncated to 500 rows for project=%s "
            "— held_by_direction metrics may be incomplete",
            project.id,
        )

    # Build direction lookup for text enrichment
    directions_by_id: dict[str, str] = {
        d.get("id", ""): d.get("text", "")[:200]
        for d in (project.directions or [])
        if d.get("id")
    }

    # Rolling windows: tighter than weekly, self-scaling to throughput.
    # "since_last_strategic" anchors to the actual review cadence so the
    # agent sees what's shipped since it last looked, not since Monday.
    _24h_ago = now - timedelta(hours=24)
    _3d_ago = now - timedelta(days=3)
    _7d_ago = now - timedelta(days=7)
    _strategic_cutoff = last_strategic_at  # may be None for first-ever cycle

    _TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)

    def _window_counts(tasks: list[Task], since: datetime) -> dict[str, Any]:
        """Count *terminal* root tasks that reached their final state within the window.

        Filtering by terminal-updated-in-window instead of created-in-window
        avoids the in-flight bias that would otherwise make short windows
        look artificially bad: e.g. 16 medium tasks spawned in the last 24h
        of which 12 are still QUEUED/IN_PROGRESS/EVALUATING would read as
        "1/16 merged, 6% rate" even though the measured rate is 40%+ once
        those tasks finish. Slower-scope tasks (medium, large) were the
        biggest victims. "merged/finished" answers the operator question
        "of the work that landed or failed in this window, what fraction
        succeeded?" — the number they actually want to read.

        Both numerator (merged) and denominator (finished) count only root tasks
        (parent_task_id is None), excluding rollup children whose pr_merged status
        reflects integration into a staging branch, not the integration branch itself.
        This ensures the merge_rate reflects the actual integration-landing rate.
        """
        from app.time_utils import _to_naive_utc

        since_naive = _to_naive_utc(since)
        merged = 0
        finished = 0
        spawned = 0  # retained for throughput visibility
        for t in tasks:
            # Task.created_at and updated_at are TIMESTAMPTZ since 0061; since is naive.
            created_at = _to_naive_utc(t.created_at)
            if created_at and since_naive and created_at >= since_naive:
                spawned += 1
            if t.status not in _TERMINAL:
                continue
            updated_at = _to_naive_utc(t.updated_at) or created_at
            if not updated_at or (since_naive and updated_at < since_naive):
                continue
            # Count only root tasks (parent_task_id is None). Rollup children should
            # not be counted in the denominator: their pr_merged=True means they landed
            # on a staging branch, not the integration branch, so they are not actual
            # integration-landing work. Both numerator and denominator must count the
            # same population for a valid merge_rate calculation.
            if t.parent_task_id is not None:
                continue
            finished += 1
            # Count only integration-landed merges: root tasks whose PR merged
            # directly to the integration branch.
            if (
                t.status == TaskStatus.COMPLETED
                and (t.context or {}).get("pr_merged")
            ):
                merged += 1
        rate = round(merged / finished, 3) if finished > 0 else None
        return {
            "finished": finished,
            "merged": merged,
            "spawned": spawned,
            "merge_rate": rate,
            # Legacy alias so existing consumers that read .created keep
            # working. Points at finished (not spawned) because "denominator"
            # for the merge_rate is finished-in-window.
            "created": finished,
        }

    rolling_rates = {
        "last_24h": _window_counts(tasks_in_window, _24h_ago),
        "last_3d": _window_counts(tasks_in_window, _3d_ago),
        "last_7d": _window_counts(tasks_in_window, _7d_ago),
    }
    if _strategic_cutoff:
        rolling_rates["since_last_strategic"] = _window_counts(
            tasks_in_window, _strategic_cutoff
        )

    # Extract the latest N merged tasks, newest first
    merged_tasks = [
        t
        for t in tasks_in_window
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged")
    ]
    merged_tasks.sort(
        key=lambda t: (
            t.updated_at or t.created_at or datetime.min.replace(tzinfo=timezone.utc)
        ),
        reverse=True,
    )

    # Rollup-aware automain landing check.
    # A task with pr_merged=True may not be on automain yet when it was merged
    # into a rollup staging branch but the rollup PR itself hasn't been merged.
    # Root tasks (no parent): on automain only when git-ancestry confirms it; unverifiable
    # cases report False (fail-closed).
    # Child tasks → on automain iff their rollup parent's pr_merged is True.
    # We fetch parent contexts in one query so the per-direction loop below
    # can reuse the same map without extra round-trips.
    _all_parent_ids: set[uuid.UUID] = {
        t.parent_task_id for t in merged_tasks if t.parent_task_id is not None
    }
    _parent_pr_merged: dict[uuid.UUID, bool] = {}
    _parent_rollup_pr: dict[uuid.UUID, int | str | None] = {}
    if _all_parent_ids:
        _parents_result = await session.execute(
            select(Task)
            .where(Task.id.in_(_all_parent_ids))
            .options(load_only(Task.id, Task.context, Task.status_log, raiseload=True))
        )
        for _p in _parents_result.scalars().all():
            _p_ctx = _p.context or {}
            _parent_pr_merged[_p.id] = bool(_p_ctx.get("pr_merged", False))
            _parent_rollup_pr[_p.id] = _p_ctx.get("rollup_pr_number") or _p_ctx.get(
                "pr_number"
            )

    # GitHub-ancestry verification for the on_automain signal.
    # The pr_merged flag is set when a PR is observed merged, but does NOT
    # confirm the commit is reachable from the integration branch (e.g. the
    # PR may have landed on a rollup staging branch that itself never merged).
    # We attempt a best-effort ancestry check using the existing github
    # primitives.  When credentials are unavailable or an API call fails the
    # fallback is fail-closed (False), NOT fail-open — see _on_automain below.
    _gh_token_ss: str | None = None
    _gh_owner_ss: str | None = None
    _gh_repo_ss: str | None = None
    _integration_branch_ss = get_project_integration_branch(project)
    _promotion_target_ss = get_project_promotion_target(project)
    _automain_verified: dict[uuid.UUID, bool] = {}
    _on_main_verified: dict[uuid.UUID, bool] = {}
    _open_rollup_task_ids: set[uuid.UUID] = set()

    try:
        from app import github as _github_ss
        from app.release_qualifier import _resolve_github_with_session as _resolve_gh

        _gh_creds = await _resolve_gh(session, project)
        if _gh_creds:
            _gh_token_ss, _gh_owner_ss, _gh_repo_ss = _gh_creds
    except Exception as _exc_ss:  # noqa: BLE001 — credential failure → flag fallback
        logger.debug("[STRATEGIC] GitHub credential resolution skipped: %s", _exc_ss)

    if _gh_token_ss and _gh_owner_ss and _gh_repo_ss:
        # CRITICAL: the ancestry verification below makes sequential, awaited
        # GitHub API calls (get_pull_request + is_ancestor_of). Those calls MUST
        # NOT run while a DB transaction is open on ``session``: a large merged
        # set makes the loop exceed Postgres'
        # ``idle_in_transaction_session_timeout`` (60s, set in database.py), the
        # server kills the connection mid-flight, and the next query
        # (children-titles, below) raises ``InterfaceError: connection is
        # closed`` — silently swallowed upstream, so the entire strategic cycle
        # fails and no strategic task ever spawns. To avoid that we (1) extract
        # everything the network phase needs into plain Python below while the
        # txn is still open, (2) commit to release the connection so it is no
        # longer idle-in-transaction, then (3) do the GitHub I/O with NO open
        # transaction. SQLAlchemy auto-begins a fresh transaction on the next
        # query (the children query). This function is read-only, so committing
        # here is equivalent to a rollback for correctness; the caller
        # (_spawn_strategic_review) holds no pending writes at this point.

        # --- (1) extract verification inputs into plain Python (txn still open) ---
        # Collect the bounded set of tasks that will actually appear in the feed:
        # top-limit merges_out entries + tasks merged since the last strategic
        # cycle (the per_direction_delta recent window).
        _recent_cutoff_verify = _strategic_cutoff or _24h_ago
        _to_verify_ids: set[uuid.UUID] = {t.id for t in merged_tasks[:limit]}
        for _t_v in merged_tasks:
            _ts_v = _t_v.updated_at or _t_v.created_at
            if _ts_v and _ts_v >= _recent_cutoff_verify:
                _to_verify_ids.add(_t_v.id)

        # Resolve which PR each verifiable task hangs its ancestry on, and group
        # task ids by that PR number. A rollup child task merges into the rollup
        # staging branch, so the child PR's own merge commit lives on that branch
        # — once the rollup PR squash-merges, the child commit is NOT an ancestor
        # of the integration branch. The child is on automain iff the *parent's*
        # rollup PR landed, so verify that PR instead of the child's own. Grouping
        # by PR number preserves the original per-PR ``get_pull_request`` dedup
        # (multiple children share one parent rollup PR) and lets us fan the
        # per-PR ancestry checks out concurrently without redundant fetches.
        _tasks_by_pr: dict[int, list[uuid.UUID]] = {}
        _rollup_message_evidence_by_task: dict[uuid.UUID, tuple[str, set[int]]] = {}
        for _t in (t for t in merged_tasks if t.id in _to_verify_ids):
            _ctx_t = _t.context or {}
            _task_pr_numbers: set[int] = set()
            for _raw_task_pr in (
                _ctx_t.get("pr_number"),
                _ctx_t.get("original_pr_number"),
            ):
                if not _raw_task_pr:
                    continue
                try:
                    _task_pr_numbers.add(int(_raw_task_pr))
                except (TypeError, ValueError):
                    continue
            _rollup_message_evidence_by_task[_t.id] = (
                _t.title or "",
                _task_pr_numbers,
            )
            if _t.parent_task_id is not None:
                _pr_num_raw = _parent_rollup_pr.get(_t.parent_task_id)
            else:
                _pr_num_raw = _ctx_t.get("rollup_pr_number") or _ctx_t.get("pr_number")
            if not _pr_num_raw:
                # No PR number available — cannot verify, use flag fallback later
                continue
            try:
                _pr_num = int(_pr_num_raw)
            except (TypeError, ValueError):
                continue
            _tasks_by_pr.setdefault(_pr_num, []).append(_t.id)

        # --- (2) release the read transaction BEFORE any network I/O ---
        # Read-only up to here, so commit() (or rollback()) is correctness-neutral;
        # it returns the connection to the pool so the GitHub calls below run with
        # no idle-in-transaction clock ticking.
        await session.commit()

        # --- (3) GitHub ancestry checks with NO open transaction ---
        # Bounded-concurrency fan-out (semaphore) + per-call timeout as
        # defense-in-depth so this verification can never again approach the 60s
        # idle-in-transaction ceiling even if it did somehow run under a txn.
        _ancestry_sem = asyncio.Semaphore(_ANCESTRY_CONCURRENCY)

        async def _verify_pr(
            _pr_num: int,
        ) -> tuple[int, bool | None, bool | None, bool]:
            """Return (pr_num, automain_landed, on_main_landed, is_open) for one PR.

            ``automain_landed``/``on_main_landed`` mirror the tri-state of
            ``is_ancestor_of``: True = verified landed, False = verified NOT
            landed / no merge SHA, None = unverifiable (API failure) → caller
            leaves the task absent from the verified map and the flag fallback
            applies. ``on_main_landed`` stays None when there is no LTS split.
            ``is_open`` is True when the PR has no merge SHA and is still open
            (state == "open") — distinguishes pending-rollup from genuinely lost.
            """
            async with _ancestry_sem:
                try:
                    _pr_data = await asyncio.wait_for(
                        _github_ss.get_pull_request(
                            _gh_token_ss,
                            _gh_owner_ss,
                            _gh_repo_ss,
                            _pr_num,
                            project_id=str(project.id),
                        ),
                        timeout=_ANCESTRY_CALL_TIMEOUT_S,
                    )
                    _merge_sha = _pr_data.get("merge_commit_sha")
                    if not _merge_sha:
                        # PR has no merge SHA: open PRs are pending a rollup merge,
                        # not genuinely lost. Closed PRs without a merge SHA are
                        # unmerged/abandoned and treated as not-landed.
                        _is_open = _pr_data.get("state") == "open"
                        return _pr_num, False, None, _is_open

                    _is_anc = await asyncio.wait_for(
                        _github_ss.is_ancestor_of(
                            _gh_token_ss,
                            _gh_owner_ss,
                            _gh_repo_ss,
                            _merge_sha,
                            _integration_branch_ss,
                            project_id=str(project.id),
                        ),
                        timeout=_ANCESTRY_CALL_TIMEOUT_S,
                    )
                    # is_ancestor_of returns True (merge commit reachable from the
                    # integration branch), False (genuinely not reachable), or None
                    # (compare API indeterminate). Only True confirms the change
                    # landed; False and None are both degrade-safe NOT-landed.
                    _automain_landed = _is_anc is True

                    # Deployed signal: also test reachability from the promotion
                    # target. on_automain=True & on_main=False is exactly "landed on
                    # the integration branch but NOT yet promoted" — i.e. not running
                    # live. Reuses the already-fetched merge SHA, so it adds only one
                    # compare call per already-verified PR, and only when there is an
                    # LTS split (otherwise the two branches are identical and
                    # on_main == on_automain).
                    _on_main_landed: bool | None = None
                    if _promotion_target_ss != _integration_branch_ss:
                        _is_anc_main = await asyncio.wait_for(
                            _github_ss.is_ancestor_of(
                                _gh_token_ss,
                                _gh_owner_ss,
                                _gh_repo_ss,
                                _merge_sha,
                                _promotion_target_ss,
                                project_id=str(project.id),
                            ),
                            timeout=_ANCESTRY_CALL_TIMEOUT_S,
                        )
                        _on_main_landed = _is_anc_main is True
                    return _pr_num, _automain_landed, _on_main_landed, False
                except Exception as _exc_t:  # noqa: BLE001 — API failure → flag fallback
                    logger.debug(
                        "[STRATEGIC] ancestry check failed for pr=%s: %s",
                        _pr_num,
                        _exc_t,
                    )
                    # None → leave the tasks absent from _automain_verified so the
                    # flag fallback applies (verdict "unverifiable").
                    return _pr_num, None, None, False

        if _tasks_by_pr:
            _verify_results = await asyncio.gather(
                *(_verify_pr(_pr_num) for _pr_num in _tasks_by_pr)
            )
            for _pr_num, _automain_landed, _on_main_landed, _is_open in _verify_results:
                for _tid in _tasks_by_pr[_pr_num]:
                    if _automain_landed is not None:
                        _automain_verified[_tid] = _automain_landed
                    if _on_main_landed is not None:
                        _on_main_verified[_tid] = _on_main_landed
                    if _is_open:
                        _open_rollup_task_ids.add(_tid)

        # One bounded squash-body scan can rescue rollup-rewritten work from a
        # false NOT-landed verdict. Scan failure degrades those negatives to
        # unverifiable so merge accounting does not manufacture a conflict.
        # Exclude open-rollup tasks: their pending_in_open_rollup verdict is
        # already confirmed by a successful get_pull_request call — running the
        # squash-body scan for them is pointless and, if it fails, would
        # incorrectly pop() the confirmed verdict back to unverifiable.
        _ancestry_false_task_ids = [
            _tid
            for _tid, _landed in _automain_verified.items()
            if _landed is False and _tid not in _open_rollup_task_ids
        ]
        if _ancestry_false_task_ids:
            try:
                _recent_commit_messages = await asyncio.wait_for(
                    _github_ss.list_recent_commit_messages(
                        _gh_token_ss,
                        _gh_owner_ss,
                        _gh_repo_ss,
                        _integration_branch_ss,
                        project_id=str(project.id),
                    ),
                    timeout=_ANCESTRY_CALL_TIMEOUT_S,
                )
            except Exception as _exc_rollup:  # noqa: BLE001 — degrade to unverifiable
                logger.debug(
                    "[STRATEGIC] rollup squash message scan failed: %s",
                    _exc_rollup,
                )
                _recent_commit_messages = None

            if _recent_commit_messages is None:
                for _tid in _ancestry_false_task_ids:
                    _automain_verified.pop(_tid, None)
            else:
                _message_rescued_task_ids: list[uuid.UUID] = []
                for _tid in _ancestry_false_task_ids:
                    _title, _task_pr_numbers = _rollup_message_evidence_by_task.get(
                        _tid, ("", set())
                    )
                    if _recent_commit_messages_match_task(
                        _recent_commit_messages,
                        title=_title,
                        pr_numbers=_task_pr_numbers,
                    ):
                        _automain_verified[_tid] = True
                        _message_rescued_task_ids.append(_tid)

                # If the project uses an LTS split, exact-SHA ancestry can be
                # equally false on the promotion target after a rollup squash.
                # Apply the same commit-message evidence there before calling
                # the merge integration-only; otherwise promoted rollup squash
                # work is incorrectly reported as undeployed.
                if (
                    _message_rescued_task_ids
                    and _promotion_target_ss != _integration_branch_ss
                ):
                    try:
                        _recent_promotion_messages = await asyncio.wait_for(
                            _github_ss.list_recent_commit_messages(
                                _gh_token_ss,
                                _gh_owner_ss,
                                _gh_repo_ss,
                                _promotion_target_ss,
                                project_id=str(project.id),
                            ),
                            timeout=_ANCESTRY_CALL_TIMEOUT_S,
                        )
                    except Exception as _exc_rollup_main:  # noqa: BLE001
                        logger.debug(
                            "[STRATEGIC] promotion-target squash message scan failed: %s",
                            _exc_rollup_main,
                        )
                        _recent_promotion_messages = None

                    if _recent_promotion_messages is not None:
                        for _tid in _message_rescued_task_ids:
                            _title, _task_pr_numbers = (
                                _rollup_message_evidence_by_task.get(_tid, ("", set()))
                            )
                            if _recent_commit_messages_match_task(
                                _recent_promotion_messages,
                                title=_title,
                                pr_numbers=_task_pr_numbers,
                            ):
                                _on_main_verified[_tid] = True

    def _on_automain(task: Task) -> bool:
        # Prefer the GitHub-verified result when available.
        if task.id in _automain_verified:
            return _automain_verified[task.id]
        # Fallback: flag heuristic (when credentials unavailable or PR has no number).
        # Root tasks with unverifiable ancestry report False (fail-closed) — we cannot
        # confirm the commit is reachable from the integration branch, so we must not
        # assume it landed.  Child tasks depend on whether their rollup parent's PR was
        # merged (DB-recorded flag), which already defaults to False.
        if task.parent_task_id is None:
            return False
        return _parent_pr_merged.get(task.parent_task_id, False)

    def _on_main(task: Task) -> bool:
        # No LTS split: the integration branch IS the promotion target, so
        # landing on one is landing on the other.
        if _promotion_target_ss == _integration_branch_ss:
            return _on_automain(task)
        # Verified reachable from the promotion target = promoted (deploying / on
        # the live branch). Fail-closed to False when unverified — un-promoted
        # work must never be reported as deployed.
        return _on_main_verified.get(task.id, False)

    def _merge_verdict(task: Task) -> str:
        """Tri-state ancestry verdict for the recent merged-title window.

        ``_on_automain`` is fail-closed: it collapses "ancestry-verified NOT
        landed" and "ancestry could not be confirmed" into a single ``False``.
        Cycle-start merge-accounting reconciliation needs to keep these apart —
        only a verified-not-landed merge is a real accounting conflict, while an
        unverifiable one must NOT manufacture a contradiction. This reads only
        the already-populated ``_automain_verified`` map (present True = landed,
        present False = verified-not-landed, absent = unverifiable), so it adds
        no second GitHub/ancestry pass.
        """
        if task.id in _automain_verified:
            if _automain_verified[task.id]:
                return "verified_landed"
            if task.id in _open_rollup_task_ids:
                # PR is still open — merged into a staging branch not yet squash-merged
                # into the integration branch. Not a real accounting conflict.
                return "pending_in_open_rollup"
            return "verified_not_landed"
        return "unverifiable"

    # For rollups: look up child titles so the agent sees what actually
    # shipped instead of "Rollup of 4 subtasks" + a title.
    merged_parent_ids = {str(t.id) for t in merged_tasks[:limit]}
    child_titles_by_parent: dict[str, list[str]] = {}
    if merged_parent_ids:
        children_result = await session.execute(
            select(Task).where(
                Task.parent_task_id.in_([uuid.UUID(p) for p in merged_parent_ids]),
                Task.status == TaskStatus.COMPLETED,
            )
        )
        for c in children_result.scalars().all():
            # Only include real product-work children — skip review/fix pipeline tasks
            c_type = c.first_payload.get("type") if c.first_payload else ""
            if c_type in ("review", "fix"):
                continue
            child_titles_by_parent.setdefault(str(c.parent_task_id), []).append(
                (c.title or "")[:150]
            )

    merges_out: list[dict[str, Any]] = []
    for t in merged_tasks[:limit]:
        ctx = t.context or {}
        merged_at = t.updated_at or t.created_at
        direction_id = ctx.get("direction_id")
        hours_ago: float | None = None
        if merged_at:
            hours_ago = round((now - merged_at).total_seconds() / 3600.0, 1)

        # Body resolution: use t.description when available and informative.
        # If it's the trivial "Rollup of N subtasks..." boilerplate, replace
        # with a synthesized summary from child titles — the agent needs to
        # know WHAT shipped, not just how many things.
        body = (t.description or "")[:1200]
        if _rollup_body_is_trivial(body):
            children = child_titles_by_parent.get(str(t.id), [])
            if children:
                body = "Rollup contents:\n- " + "\n- ".join(children[:8])
            else:
                body = ""

        merges_out.append(
            {
                "pr_title": (t.title or "")[:200],
                "pr_number": ctx.get("rollup_pr_number") or ctx.get("pr_number"),
                "direction_id": direction_id,
                "direction_text": directions_by_id.get(direction_id, "")
                if direction_id
                else "",
                "merged_at": merged_at.isoformat() if merged_at else None,
                "hours_ago": hours_ago,
                "body": body,
                "on_automain": _on_automain(t),
                "on_main": _on_main(t),
                "landed_on_integration": ctx.get(LANDED_ON_INTEGRATION_KEY),
            }
        )

    # Per-direction delta: for each active direction, counts + titles per
    # window. Lets the agent see "direction X shipped N things since my
    # last cycle" without having to cross-correlate merges[] by direction_id.
    per_direction_delta: list[dict[str, Any]] = []
    # Stranded-PR reconciliation pairs — PLAIN-VALUE snapshots (task_id,
    # had_pr_merged), never ORM instances — collected during the loops below
    # and drained after them, each pair on its OWN fresh session (the #13191
    # post-commit-fresh-session pattern). Rationale:
    # discount_stranded_pr_from_direction_credit's best-effort except path
    # rolls back the session it is handed, and a rollback expires EVERY
    # instance loaded in that session (regardless of expire_on_commit=False).
    # Run inline on the shared session, one failed discount made the next
    # iteration's `t.context` read — and the caller's later `project` reads
    # (_spawn_strategic_review → source_ref_validation) — fire a sync
    # lazy-load → MissingGreenlet and abort the whole autonomous spawn cycle.
    _stranding_queue: list[tuple[uuid.UUID, bool]] = []
    for d in project.directions or []:
        d_id = d.get("id")
        if not d_id or d.get("status") != "active":
            continue
        d_tasks = [
            t for t in tasks_in_window if (t.context or {}).get("direction_id") == d_id
        ]
        entry: dict[str, Any] = {
            "direction_id": d_id,
            "direction_text": (d.get("text") or "")[:200],
            "last_24h": _window_counts(d_tasks, _24h_ago),
            "last_3d": _window_counts(d_tasks, _3d_ago),
        }
        if _strategic_cutoff:
            entry["since_last_strategic"] = _window_counts(d_tasks, _strategic_cutoff)
        # Titles of things that shipped since last strategic (or last 24h as fallback).
        # Each entry is a dict so the agent can distinguish automain-landed from
        # staging-only merges (on_automain=False means rollup not yet merged).
        recent_cutoff = _strategic_cutoff or _24h_ago
        recent_merges_dicts: list[dict[str, Any]] = []
        recent_not_landed_titles: list[str] = []
        recent_merge_verdict_counts = {
            "verified_landed": 0,
            "verified_not_landed": 0,
            "unverifiable": 0,
            "pending_in_open_rollup": 0,
        }
        for t in d_tasks:
            if (
                t.status != TaskStatus.COMPLETED
                or not (t.context or {}).get("pr_merged")
                or (
                    t.updated_at
                    or t.created_at
                    or datetime.min.replace(tzinfo=timezone.utc)
                )
                < recent_cutoff
            ):
                continue
            merge_verdict = _merge_verdict(t)
            title = (t.title or "")[:160]
            recent_merges_dicts.append(
                {
                    "title": title,
                    "on_automain": _on_automain(t),
                    "merge_verdict": merge_verdict,
                    "landed_on_integration": (t.context or {}).get(
                        LANDED_ON_INTEGRATION_KEY
                    ),
                }
            )
            if merge_verdict not in recent_merge_verdict_counts:
                merge_verdict = "unverifiable"
            recent_merge_verdict_counts[merge_verdict] += 1
            if (
                merge_verdict == "verified_not_landed"
                and len(recent_not_landed_titles) < _RECON_LIST_CAP
            ):
                recent_not_landed_titles.append(title)
            if merge_verdict == "verified_not_landed" and (t.context or {}).get(
                LANDED_ON_INTEGRATION_KEY
            ) is not False:
                # Deferred: see _stranding_queue above — snapshot plain values
                # only; the pair runs later on its own fresh session so a
                # failure can never poison this session's loaded instances.
                _stranding_queue.append(
                    (t.id, bool((t.context or {}).get("pr_merged")))
                )
        if recent_merges_dicts:
            entry["recent_merged_titles"] = recent_merges_dicts[
                :_RECENT_MERGED_TITLES_CAP
            ]
            entry["recent_merge_verdict_counts"] = recent_merge_verdict_counts
            if recent_not_landed_titles:
                entry["recent_not_landed_titles"] = recent_not_landed_titles
        # Count recently-completed tasks that produced no PR (design, web_research,
        # compute_experiment, etc.).  These represent real pipeline progress even
        # though they never set pr_merged=True.
        recent_non_pr_completed = 0
        for t in d_tasks:
            if t.status != TaskStatus.COMPLETED or (t.context or {}).get("pr_merged"):
                continue
            t_updated = (
                t.updated_at
                or t.created_at
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            if t_updated >= recent_cutoff:
                recent_non_pr_completed += 1
        if recent_non_pr_completed:
            entry["recent_non_pr_completed"] = recent_non_pr_completed
        per_direction_delta.append(entry)

    # Active-by-scope: tasks currently in flight per scope. Surfaced so the
    # strategic agent doesn't lose sight of in-flight work now that the
    # merge_rate denominators are "finished-in-window" only. Without this,
    # pulling in-flight out of the rate would hide slow-moving large-scope
    # tasks entirely — the agent would see "no large merges in 24h" with
    # no signal that 3 large tasks are actively running.
    active_by_scope: dict[str, int] = {
        "small": 0,
        "medium": 0,
        "large": 0,
        "unknown": 0,
    }
    # In-flight task count per direction — the "is work already moving on this
    # direction" signal the cycle-start reconciliation reads to distinguish a
    # direction whose research is piling up unactioned from one with tasks
    # already in progress. Counts ANY in-flight task (research/plan/create/fix/
    # review) attributed to the direction, since each means the lane is active.
    active_by_direction: dict[str, int] = {}
    for t in active_tasks:
        did = (t.context or {}).get("direction_id")
        if did:
            active_by_direction[did] = active_by_direction.get(did, 0) + 1
        # Only count ROLLUP_TYPES work — pipeline tasks (review/fix) aren't
        # scope-classified and would inflate the counts.
        t_type = (t.first_payload or {}).get("type", "")
        if t_type in ("review", "fix"):
            continue
        scope = (t.context or {}).get("scope") or "unknown"
        if scope not in active_by_scope:
            active_by_scope[scope] = 0
        active_by_scope[scope] += 1

    held_by_direction: dict[str, int] = {}
    for t in held_tasks:
        did = (t.context or {}).get("direction_id")
        if did:
            held_by_direction[did] = held_by_direction.get(did, 0) + 1

    # Directions whose shipped work has landed on the integration branch but is
    # NOT yet on the promotion target (promoted) — the capabilities whose
    # live/runtime metrics cannot move until the version promotes. The concrete
    # companion to the deploy-model framing. Only populated when the promotion
    # target was actually verifiable (creds present + an LTS split), so a
    # credential gap never manufactures false "undeployed" noise.
    undeployed_directions: list[dict[str, Any]] = []
    if (
        _gh_token_ss
        and _gh_owner_ss
        and _gh_repo_ss
        and _promotion_target_ss != _integration_branch_ss
    ):
        _undeployed_by_dir: dict[str, dict[str, Any]] = {}
        for _m in merges_out:
            if _m.get("on_automain") and not _m.get("on_main"):
                _did = _m.get("direction_id") or "_unattributed"
                _slot = _undeployed_by_dir.setdefault(
                    _did,
                    {
                        "direction_id": _m.get("direction_id"),
                        "direction_text": _m.get("direction_text") or "",
                        "merge_titles": [],
                    },
                )
                if len(_slot["merge_titles"]) < 5:
                    _slot["merge_titles"].append(_m.get("pr_title") or "")
        undeployed_directions = list(_undeployed_by_dir.values())

    # Cycle-start stranded-PR reconciliation — each queued pair runs on its
    # OWN fresh session (#13191 post-commit-fresh-session pattern): the task
    # is re-fetched by id, marked stranded, discounted, and committed there.
    # A failure rolls back only that private session — the shared `session`
    # and every instance loaded in it (including `project`, which the caller
    # _spawn_strategic_review keeps reading) are never touched. Each pair is
    # individually guarded, so one task's failure skips just that task (it
    # simply re-strands next cycle) and never aborts the interventions build
    # or the remaining strandings. Guard logging uses the snapshotted id, not
    # ORM attributes — an attribute read on an expired instance would itself
    # raise MissingGreenlet inside the except.
    if _stranding_queue:
        from app.database import AsyncSessionLocal as _StrandSession

        for _st_task_id, _st_had_pr_merged in _stranding_queue:
            try:
                async with _StrandSession() as _st_session:
                    _st_task = await _st_session.get(Task, _st_task_id)
                    if _st_task is None:
                        continue
                    mark_stranded_non_landing(
                        _st_task, reason="cycle_start_reconciliation_ancestry_check"
                    )
                    await discount_stranded_pr_from_direction_credit(
                        _st_task, _st_session, had_pr_merged=_st_had_pr_merged
                    )
                    # Persist the stranding marker even when the discount
                    # early-returned before its own commit (e.g. missing
                    # research_task_id / direction attribution) — marker
                    # durability must not depend on discount applicability.
                    # No-op when the discount already committed.
                    await _st_session.commit()
            except Exception:  # noqa: BLE001 — per-pair isolation is the point
                logger.warning(
                    "[STRATEGIC] stranded-PR reconciliation failed for task %s",
                    _st_task_id,
                    exc_info=True,
                )

    result: dict[str, Any] = {
        "merges": merges_out,
        "rolling_rates": rolling_rates,
        "per_direction_delta": per_direction_delta,
        "active_by_scope": active_by_scope,
        "active_by_direction": active_by_direction,
        "held_by_direction": held_by_direction,
        "lookback_days": lookback_days,
        "undeployed_directions": undeployed_directions,
    }
    if _strategic_cutoff:
        result["hours_since_last_strategic"] = round(
            (now - _strategic_cutoff).total_seconds() / 3600.0, 1
        )
    return result


def _build_previous_decision_outcomes(
    project: Project, cycle_history: list
) -> dict[str, Any] | None:
    """Summarize outcomes of the most recent completed strategic cycle's
    direction create/retire decisions so the agent can learn from them.

    Walks ``cycle_history`` newest-first for an entry with outcome
    "completed" that also recorded created_direction_ids or
    retired_direction_ids. For each direction id it looks up the current
    direction in project.directions and reports cycles_active, prs_merged,
    quality_score, and paused flags. Retired directions missing from the
    current list are reported with status="missing".

    Audit W2: ``cycle_history`` is now passed in (was JSONB); the async
    caller pre-fetches via ``strategic_cycle.recent_for_agent_context``.
    """
    cycle_history = list(cycle_history or [])
    if not cycle_history:
        return None
    prev_entry = None
    for entry in reversed(cycle_history):
        if entry.get("outcome") != "completed":
            continue
        if entry.get("created_direction_ids") or entry.get("retired_direction_ids"):
            prev_entry = entry
            break
    if prev_entry is None:
        return None

    directions_by_id: dict[str, dict] = {
        d.get("id"): d for d in (project.directions or []) if d.get("id")
    }

    created_rationales = prev_entry.get("created_rationales") or {}
    retired_rationales = prev_entry.get("retired_rationales") or {}
    created_predictions = prev_entry.get("created_predictions") or {}
    retired_final_metrics = prev_entry.get("retired_final_metrics") or {}

    def _summarize(
        direction_id: str, role: str, rationale: str | None
    ) -> dict[str, Any]:
        d = directions_by_id.get(direction_id)
        if d is None:
            entry: dict[str, Any] = {
                "direction_id": direction_id,
                "role": role,
                "status": "missing",
            }
            if rationale:
                entry["rationale"] = rationale
            if role == "retired" and direction_id in retired_final_metrics:
                entry["final_metrics"] = retired_final_metrics[direction_id]
            return entry
        history = d.get("research_history") or []
        agg = d.get("history_aggregate") or {}
        cycles_active = len(history) + (agg.get("total_cycles") or 0)
        # Verified merge total — the previous-decision-outcome prior (and the
        # derived calibration_delta) must reflect work that actually landed, not
        # degraded/unverified merges. Legacy fallback handled by
        # verified_merged_count / the aggregate .get chain.
        prs_merged = sum(verified_merged_count(h) for h in history) + (
            agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
        )
        is_paused = d.get("status") == "paused"
        auto_paused = bool(d.get("auto_paused"))
        entry = {
            "direction_id": direction_id,
            "role": role,
            "status": d.get("status"),
            "text": (d.get("text") or "")[:200],
            "cycles_active": cycles_active,
            "prs_merged": prs_merged,
            "quality_score": round(_compute_direction_quality_score(d), 3),
            "auto_paused": auto_paused,
            "user_paused": is_paused and not auto_paused,
        }
        if rationale:
            entry["rationale"] = rationale
        if role == "created" and direction_id in created_predictions:
            prediction = created_predictions[direction_id]
            entry["prediction"] = prediction
            expected = prediction.get("similar_past_merges", 0)
            entry["calibration_delta"] = entry["prs_merged"] - expected
        if role == "retired" and direction_id in retired_final_metrics:
            entry["final_metrics"] = retired_final_metrics[direction_id]
        return entry

    created = [
        _summarize(did, "created", created_rationales.get(did))
        for did in (prev_entry.get("created_direction_ids") or [])
    ]
    retired = [
        _summarize(did, "retired", retired_rationales.get(did))
        for did in (prev_entry.get("retired_direction_ids") or [])
    ]
    result: dict[str, Any] = {
        "cycle_id": prev_entry.get("cycle_id"),
        "phase2_completed_at": prev_entry.get("phase2_completed_at"),
        "summary_text": prev_entry.get("summary_text"),
        "created": created,
        "retired": retired,
    }
    cycle_reasoning = prev_entry.get("cycle_reasoning")
    if cycle_reasoning:
        result["cycle_reasoning"] = cycle_reasoning
    return result


def _build_past_retirement_critiques(
    cycle_history: list, n: int = 5
) -> list[dict[str, Any]]:
    """Collect the most recent N retirement critiques from cycle history.

    Each element is a critique object persisted by ``set_retirement_critiques``
    (fields: direction_id, what_it_addressed, pattern_that_produced_it, lesson)
    or the sentinel ``{"no_eligible_retirements": true}``. Returns [] when no
    cycle has recorded retirement_critiques yet.
    """
    results: list[dict[str, Any]] = []
    for entry in cycle_history:
        if entry.get("outcome") != "completed":
            continue
        critiques = entry.get("retirement_critiques")
        if critiques is None:
            continue
        if isinstance(critiques, dict) and critiques.get("no_eligible_retirements"):
            results.append({"no_eligible_retirements": True})
        elif isinstance(critiques, list):
            results.extend(critiques)
        if len(results) >= n:
            break
    return results[:n]


def _build_past_retirement_deferrals(
    cycle_history: list, n: int = 5
) -> list[dict[str, Any]]:
    """Collect the most recent N fail-closed retirement deferrals.

    Surface the deferrals from completed strategic cycles so the next review
    pass can see which direction retirements were deferred and why.
    """
    results: list[dict[str, Any]] = []
    for entry in cycle_history:
        if entry.get("outcome") != "completed":
            continue
        deferrals = entry.get("retirement_deferrals")
        if not isinstance(deferrals, list):
            continue
        for deferral in deferrals:
            if not isinstance(deferral, dict):
                continue
            results.append(
                {
                    "direction_id": str(deferral.get("direction_id", "")).strip()[:64],
                    "approved": bool(deferral.get("approved")),
                    "challenge_status": str(
                        deferral.get("challenge_status", "")
                    ).strip()[:32],
                    "verdict": str(deferral.get("verdict", "")).strip()[:32],
                    "reason": str(deferral.get("reason", "")).strip()[:300],
                    "recorded_at": str(deferral.get("recorded_at", "")).strip()[:64],
                }
            )
            if len(results) >= n:
                return results[:n]
    return results[:n]


def _build_anomaly_summary(project: Project) -> dict[str, Any] | None:
    """Summarize high-occurrence persistent anomalies for the strategic agent.

    Surfaces entries with occurrence_count >= 3, splitting cross-direction
    fingerprints (which span multiple directions) from other recurring
    anomalies. Returns None when nothing qualifies so the key is omitted.
    """
    proj_anomalies = _shared_state.persistent_anomalies.get(str(project.id)) or {}
    if not proj_anomalies:
        return None

    cross_direction: list[dict[str, Any]] = []
    recurring: list[dict[str, Any]] = []
    for key, entry in proj_anomalies.items():
        # Skip non-dict sibling values (e.g. the "signal_last_computed_at"
        # ISO-string sentinel written by anomaly_detection._compute_research_anomalies
        # for the staleness watchdog). Every other consumer of persistent_anomalies
        # guards the same way; this one was the lone exception and crashed here.
        if not isinstance(entry, dict):
            continue
        occurrences = entry.get("occurrence_count") or 0
        if occurrences < 3:
            continue
        item: dict[str, Any] = {
            "key": key,
            "occurrence_count": occurrences,
            "severity": entry.get("severity"),
            "first_seen": entry.get("first_seen"),
            "last_seen": entry.get("last_seen"),
        }
        if key.startswith("CrossDirectionFingerprint_"):
            item["fingerprint"] = entry.get("fingerprint")
            item["affected_directions"] = list(entry.get("affected_directions") or [])
            if entry.get("failure_count"):
                item["failure_count"] = entry["failure_count"]
            cross_direction.append(item)
        else:
            if entry.get("affected_directions"):
                item["affected_directions"] = list(entry["affected_directions"])
            if entry.get("direction_id"):
                item["direction_id"] = entry["direction_id"]
            recurring.append(item)

    if not cross_direction and not recurring:
        return None

    cross_direction.sort(key=lambda e: e.get("occurrence_count") or 0, reverse=True)
    recurring.sort(key=lambda e: e.get("occurrence_count") or 0, reverse=True)
    return {
        "cross_direction_fingerprints": cross_direction[:10],
        "recurring_anomalies": recurring[:10],
    }


_STRATEGIC_PLAYBOOK_SOURCES = {
    "auto-generated",  # recurring failure patterns
    "review-extracted",  # review feedback patterns
    "first-attempt-pattern",  # direction-level rejection patterns
}
_STRATEGIC_PLAYBOOK_KEYWORDS = (
    "direction",
    "auto-pause",
    "auto_pause",
    "auto paused",
    "recurring",
    "repeated failure",
    "architecture",
)


def _build_playbook_summary(project: Project) -> dict[str, Any] | None:
    """Summarize playbook entries most relevant to strategic decisions.

    Pulls entries from project.design_doc whose key starts with ``playbook/``
    and either come from a strategy-relevant source (failure patterns, review
    patterns, first-attempt rejection patterns) or whose content mentions
    directions, auto-pause, or recurring failures. Returns the 10 most recently
    updated, with content truncated for prompt budget. Returns None when
    nothing qualifies so the key can be omitted from the payload.
    """
    doc = project.design_doc or {}
    from app.playbook_storage import visible_playbook_entry

    candidates: list[dict[str, Any]] = []
    for key, entry in doc.items():
        if not key.startswith("playbook/"):
            continue
        entry = visible_playbook_entry(entry)
        if entry is None:
            continue
        source = entry.get("source") or ""
        content = entry.get("content") or ""
        content_lower = content.lower()
        relevant = source in _STRATEGIC_PLAYBOOK_SOURCES or any(
            kw in content_lower for kw in _STRATEGIC_PLAYBOOK_KEYWORDS
        )
        if not relevant:
            continue
        item: dict[str, Any] = {
            "key": key[len("playbook/") :],
            "content": content[:400],
            "source": source or None,
            "updated_at": entry.get("updated_at"),
        }
        for opt in (
            "hit_count",
            "confirmed_count",
            "scope",
            "learned_from_direction",
            "rejection_count",
        ):
            if entry.get(opt) is not None:
                item[opt] = entry[opt]
        candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return {"entries": candidates[:10]}


def _build_playbook_keys_index(project: Project) -> dict[str, Any] | None:
    """Complete index of all playbook entry keys with minimal metadata.

    Lists ALL playbook entries (not just the top-N strategically relevant ones),
    with just their keys and minimal metadata (source, updated_at). This compact
    index survives budget pressure better than the full playbook_summary and lets
    the strategist discover what knowledge exists in the playbook, then fetch
    individual entries on demand via `./fetch playbook <key>`.

    Returns None when no playbook entries exist so the key can be omitted.
    """
    doc = project.design_doc or {}
    from app.playbook_storage import visible_playbook_entry

    entries: list[dict[str, Any]] = []
    for key, entry in doc.items():
        if not key.startswith("playbook/"):
            continue
        entry = visible_playbook_entry(entry)
        if entry is None:
            continue
        entries.append(
            {
                "key": key[len("playbook/") :],
                "source": entry.get("source"),
                "updated_at": entry.get("updated_at"),
            }
        )
    if not entries:
        return None
    # Sorted newest-first so the strategist sees recent changes first
    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return {"entries": entries}


_IN_FLIGHT_TASK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)
"""Statuses treated as live in-flight work for strategic pipeline summaries."""


async def _find_last_strategic_cycle_time(
    project: Project, session: AsyncSession
) -> datetime | None:
    """Return the updated_at of the most recent completed strategic task
    (strategic_review or design_doc_update) for this project, or None.

    Both task types constitute strategic-engine activity: design_doc_update is
    Phase 1 and strategic_review is Phase 2. Including both makes the
    since_last_strategic window accurate when Phase 1 just completed but
    Phase 2 hasn't run yet — otherwise the window would anchor to the prior
    Phase 2 completion and make recent_interventions look artificially stale.
    This also keeps the definition consistent with the stall detector in
    pipeline_regression_dispatcher, which treats both types as cycle activity.
    """
    result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
            Task.payloads[0]["type"].as_string().in_(sorted(STRATEGIC_TASK_TYPES)),
        )
        .order_by(Task.updated_at.desc())
        .limit(1)
    )
    task = result.scalars().first()
    if not task:
        return None
    return task.updated_at or task.created_at


async def _bootstrap_pending(project: Project, session: AsyncSession) -> bool:
    """Return True if a repo_bootstrap task is still in-flight for this project,
    or if an automatic recovery attempt is scheduled but not yet due.

    False for the deploy's own repo (``project_is_deploy_repo``) and for
    projects whose bootstrap has already completed (``bootstrap_completed_at``
    is set).  A terminal bootstrap
    (FAILED / COMPLETED / …) with no pending recovery also returns False so a
    permanently degraded bootstrap never deadlocks autonomous spawning.
    """
    from app.repo_bootstrap import _parse_recovery_at

    _build_cfg = project.build_config or {}
    if _build_cfg.get("bootstrap_completed_at") or project_is_deploy_repo(project):
        return False
    pending = (
        await session.execute(
            select(Task.id)
            .where(
                Task.project_id == project.id,
                Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                    ]
                ),
            )
            .limit(1)
        )
    ).first()
    if pending is not None:
        return True
    # Also defer during a scheduled recovery backoff window so that the recovery
    # attempt gets a chance to produce repo memory before implementation cycles start.
    if _build_cfg.get("bootstrap_recovery_state") == "backoff":
        next_at = _parse_recovery_at(_build_cfg.get("bootstrap_recovery_next_at"))
        if next_at is not None and next_at > _utcnow():
            return True
    return False


async def _maybe_notify_operator_bootstrap_deferred(
    project: Project, session: AsyncSession, reason: str
) -> None:
    """Reuse the one-time operator deferral notice for bootstrap-pending gates."""
    from app.autonomous_research import _maybe_notify_operator_refresh_deferred

    await _maybe_notify_operator_refresh_deferred(project, session, reason)


async def _strategic_spawn_budget_guard(
    session: AsyncSession, spawn_category: str
) -> dict[str, Any] | None:
    """Return the autonomous budget guard for a strategic spawn, if any."""
    try:
        snapshot = await compute_autonomous_spawn_budget_snapshot(
            session, runtime_config=_runtime_config
        )
        logger.info(
            "[AUTONOMOUS] Spawn budget snapshot: %s",
            snapshot.as_log_fields(),
        )
    except Exception as exc:  # snapshot read failed; fail posture is mode-dependent
        # A persistent snapshot/DB failure must NOT silently disable an
        # operator-enabled 'enforce' budget (that would be the exact opposite
        # of enforcement). The configured mode comes from runtime_config and
        # needs no DB, so we can still read it here. Fail CLOSED under enforce
        # (block the spawn) and preserve fail-open for the observational
        # off/observe modes where the guard is only advisory.
        try:
            mode = autonomous_spawn_budget_config_from_runtime(_runtime_config).mode
        except Exception:  # noqa: BLE001 - malformed config; stay observational
            mode = DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE
        if mode == "enforce":
            logger.exception(
                "[AUTONOMOUS] Spawn budget snapshot failed in enforce mode; "
                "failing CLOSED (blocking %s spawn)",
                spawn_category,
            )
            return {
                "guard": "budget_snapshot_unavailable",
                "reason": (
                    "Autonomous spawn-budget snapshot could not be computed in "
                    "enforce mode; failing closed to block unbounded spawns: "
                    f"{exc}"
                ),
                "mode": mode,
                "scope": "global",
                "category": spawn_category,
            }
        # Non-enforce (off/observe): the categorized budget guard is advisory,
        # but a snapshot failure must NOT remove the hard cross-project ceiling
        # entirely. Mirror the sibling raw COUNT(*) backstop in
        # autonomous_research._spawn_research_task so a snapshot error degrades to
        # the same coarse global rate limit rather than failing fully open.
        try:
            global_max = _runtime_config.get(
                "max_global_autonomous_tasks_per_hour",
                _runtime_config.get("max_autonomous_tasks_per_hour", 12) * 10,
            )
            one_hour_ago = _utcnow() - timedelta(hours=1)
            global_recent = (
                await session.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        autonomous_spawn_task_clause(),
                        Task.created_at >= one_hour_ago,
                    )
                )
            ).scalar() or 0
        except Exception as count_exc:  # noqa: BLE001 - fallback count failed too
            logger.warning(
                "[AUTONOMOUS] Spawn budget snapshot AND raw fallback count "
                "failed (mode=%s); failing open: snapshot=%s count=%s",
                mode,
                exc,
                count_exc,
            )
            return None
        if global_recent >= global_max:
            logger.warning(
                "[AUTONOMOUS] Spawn budget snapshot failed (mode=%s); raw "
                "COUNT(*) fallback ceiling reached: %s/%s tasks/hour",
                mode,
                global_recent,
                global_max,
            )
            return {
                "guard": "global_rate_limit_exceeded",
                "reason": (
                    "Autonomous spawn-budget snapshot unavailable; raw global "
                    f"backstop reached: {global_recent}/{global_max} tasks/hour"
                ),
                "current": global_recent,
                "limit": global_max,
                "headroom": max(0, global_max - global_recent),
                "mode": mode,
                "scope": "global",
                "category": spawn_category,
            }
        logger.warning(
            "[AUTONOMOUS] Spawn budget calculation failed open (mode=%s); raw "
            "fallback ceiling not reached (%s/%s): %s",
            mode,
            global_recent,
            global_max,
            exc,
        )
        return None
    return build_autonomous_spawn_budget_guard(
        snapshot=snapshot,
        spawn_category=spawn_category,
        enforce_initiative=snapshot.mode == "enforce",
    )


async def _spawn_design_doc_update(
    project: Project,
    session: AsyncSession,
    *,
    apply_spawn_budget_guard: bool = False,
    expected_legacy_generation: int | None = None,
) -> bool:
    """Spawn a design doc update task (Phase 1 of strategic cycle).

    This runs as a separate task type so it doesn't block tactical research.
    On completion, it chains to _spawn_strategic_review (Phase 2).
    Returns True if a task was created, False if skipped.
    """
    from app.task_lifecycle import _has_active_strategic_task

    try:
        if expected_legacy_generation is None:
            expected_legacy_generation = await lock_and_validate_legacy_authority(
                session, project.id
            )
            await session.commit()
    except LegacyEngineAuthorityExpired:
        await session.rollback()
        return False

    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.warning(
            f"[STRATEGIC] Skipping design doc update — strategic task already active for {project.id}"
        )
        return False

    # Guard: defer architecture cycles until repo_bootstrap completes.
    if await _bootstrap_pending(project, session):
        await _maybe_notify_operator_bootstrap_deferred(
            project,
            session,
            "repo_bootstrap task is pending (bootstrap_completed_at not set)",
        )
        logger.info(
            "[STRATEGIC] Deferring design doc update for project %s — "
            "repo_bootstrap task is pending (bootstrap_completed_at not set)",
            project.id,
        )
        return False

    if apply_spawn_budget_guard:
        budget_guard = await _strategic_spawn_budget_guard(session, "initiative")
        if budget_guard is not None:
            logger.warning("[AUTONOMOUS] Spawn budget block: %s", budget_guard)
            return False

    doc_model = (
        _runtime_config.get("task_type_models", {}).get("design_doc_update")
        or _runtime_config.get("task_type_models", {}).get("research")
        or None
    )
    doc_effort = (
        _runtime_config.get("task_type_efforts", {}).get("design_doc_update")
        or _runtime_config.get("task_type_efforts", {}).get("research")
        or None
    )

    # Fetch open messages for the design doc agent via the shared authoritative
    # feed (status-filtered query, size-bounded, BUG_REPORT/BLOCKER-prioritized)
    # so this path and the strategic_review path stay unified.
    open_msgs = await _fetch_open_messages(session, project.id)
    operator_context_refs = await _fetch_operator_context_refs(session, project.id)

    # Build shipped_changes_manifest: tasks merged since the last arch review
    shipped_changes_manifest = await _build_shipped_changes_manifest(project, session)

    verification_kwargs = await resolve_branch_verification_context(session, project)

    # Ground the design doc against the real integration branch before shipping
    # it: present-but-absent systems/capabilities are downgraded so the agent
    # never sees a status the backing code can't support. Read-only/advisory —
    # the stored design_doc is untouched; reconcile operates on the agent view.
    # The grounded doc is then reduced to a compact, bounded navigation index
    # (key + bounded summary + reconciled presence status/modules/warning); the
    # full per-section content stays fetchable on demand via the design-doc
    # op:get endpoint so the field can no longer dominate the payload budget.
    reconciled_design_doc = await ground_design_doc(
        _agent_view_design_doc(project.design_doc), **verification_kwargs
    )

    # Surface aggregated missing-foundation demand to the strategist so it can open
    # a foundation direction or record an explicit retirement of stranded demand.
    # Read-only, fail-open, additive: included only when demand is non-empty.
    _missing_demand = await fetch_missing_demand_for_spawn(session, project.id)
    _missing_demand_present = bool(_missing_demand.get("demands"))
    _dd_registry_names = registry_capability_names(project.problem_model)
    _missing_demand_hint = (
        missing_demand_prompt_hint(_missing_demand, registry_names=_dd_registry_names)
        if _missing_demand_present
        else None
    )
    _dd_available_capabilities = _annotate_capability_proof_status(
        registry_capability_vocabulary(project.problem_model),
        getattr(project, "capability_proof_records", None),
    )
    # Compact, body-omitted index of existing knowledge documents (web_research /
    # design output) so the strategist knows what knowledge already exists and
    # can fetch full bodies on demand — included only when documents exist.
    _knowledge_index = await build_knowledge_index(session, project.id)
    _dd_compute_sweep_groups = await build_compute_sweep_groups(session, project.id)
    _pending_followups = await build_pending_followups(session, project.id)

    payload: dict = pack_under_budget(
        {
            "type": "design_doc_update",
            "design_doc": _design_doc_index(reconciled_design_doc),
            **({"knowledge_index": _knowledge_index} if _knowledge_index else {}),
            **({"pending_followups": _pending_followups} if _pending_followups else {}),
            **({"compute_experiment_sweeps": _dd_compute_sweep_groups} if _dd_compute_sweep_groups else {}),
            "proposed_changes": await read_telemetry(
                session, project.id, "proposed_changes"
            ) or (project.design_doc or {}).get("proposed_changes", []),
            "project_name": project.name,
            "vision": _agent_visible_vision(project.vision),
            "effective_purpose": resolve_project_purpose(project),
            **(
                {"missing_capability_demand": _missing_demand}
                if _missing_demand_present
                else {}
            ),
            **({"prompt_suffix": _missing_demand_hint} if _missing_demand_hint else {}),
            **(
                {"available_capabilities": _dd_available_capabilities}
                if _dd_available_capabilities
                else {}
            ),
            "problem_model": await _verify_capability_backing_paths(
                _agent_view_problem_model(project.problem_model),
                **verification_kwargs,
            ),
            # Soft size/health signal from the DURABLE pre-trim model so counts
            # are honest (the agent view above is trimmed). Fail-open: omitted
            # when the durable model fails strict validation.
            **(
                {"model_health": _dd_model_health}
                if (_dd_model_health := _compute_model_health(project.problem_model))
                else {}
            ),
            "open_message_count": len(open_msgs),
            "open_messages": open_msgs,
            **(
                {"operator_context_refs": operator_context_refs}
                if operator_context_refs
                else {}
            ),
            "shipped_changes_manifest": shipped_changes_manifest,
            "base_branch": get_project_integration_branch(project),
            **({"model": doc_model} if doc_model else {}),
            **({"effort": doc_effort} if doc_effort else {}),
        },
        _DESIGN_DOC_UPDATE_SCHEMA,
    )
    payload = validate_internal_payload(
        payload, "autonomous_research:design_doc_update"
    )
    # Audit W2: cycle history is a real table now (StrategicCycle); the
    # 10-entry sliding-window cap in vision JSONB is gone. Snapshot the
    # vision at cycle start so the 3-way merge in strategic_completion can
    # compare user edits against the same baseline the agent was given.
    cycle_id = strategic_cycle.new_cycle_id()
    # Capture the operator-origin steering signal at spawn time: the pending
    # refresh marker is still armed here (the autonomous loop's spawn-record
    # consumes it AFTER this returns), but it will be gone by the time Phase 2
    # (_spawn_strategic_review) chains off this task's completion. Persist the
    # flag on the task context so the Phase 2 chain can re-surface the operator
    # steering directive rather than silently dropping it on architecture cycles.
    operator_steered = strategic_refresh_origin(project.vision) == ORIGIN_OPERATOR
    local_runner_intent = await _resolve_local_continuation_runner_intent(
        session,
        project.id,
        "design_doc_update",
    )
    task_context: dict = {"strategic_cycle_id": cycle_id, **local_runner_intent}
    if not apply_spawn_budget_guard:
        # Manual / operator-triggered cycles should not inherit the autonomous
        # budget backstop when Phase 1 completes.
        task_context[STRATEGIC_CYCLE_MANUAL_CONTEXT_KEY] = True
    if operator_steered:
        task_context["operator_steered"] = True
        # Persist the operator steering excerpt alongside the flag: the marker is
        # consumed when this Phase 1 spawn is recorded, so Phase 2 must carry the
        # text forward (the live thread snapshot may age it out before then).
        steering_excerpt = strategic_refresh_excerpt(project.vision)
        if steering_excerpt:
            task_context["steering_excerpt"] = steering_excerpt
    try:
        await lock_and_validate_legacy_authority(
            session,
            project.id,
            expected_generation=expected_legacy_generation,
        )
    except LegacyEngineAuthorityExpired:
        await session.rollback()
        return False
    new_task = Task(
        project_id=project.id,
        title=f"Architecture review: {project.name}",
        description="Update project design document",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context=task_context,
        strategic_cycle_id=cycle_id,
    )
    session.add(new_task)
    await session.flush()  # assign new_task.id for the FK below
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "design_doc_update"},
    )
    session.add(history)
    await strategic_cycle.create(
        session,
        project_id=project.id,
        cycle_id=cycle_id,
        phase1_task_id=new_task.id,
        baseline_vision=project.vision or {},
        baseline_problem_model=sanitize_problem_model_raw(project.problem_model or {}),
        baseline_running_update=project.running_update,
    )
    await commit_legacy_authorized(
        session, project.id, expected_legacy_generation
    )
    # Post-commit: signal the admin UI that a strategic cycle (Phase 1) started.
    from app.strategic_events import emit_strategic_cycle_started

    await emit_strategic_cycle_started(
        project.id,
        new_task.id,
        1,
        cycle_id=cycle_id,
        steering_origin=ORIGIN_OPERATOR if operator_steered else None,
        steering_excerpt=task_context.get("steering_excerpt"),
    )
    logger.info(
        f"[STRATEGIC] Spawned design_doc_update task {new_task.id} for project {project.id} (cycle {cycle_id})"
    )
    return True


def _freshness_generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_freshness_shas_block(currency: dict) -> dict:
    """Build a normalized SHA-freshness dict from a resolved candidate-currency dict.

    Takes a RESOLVED ``{"current": bool, "candidate_sha": str, "head_sha": str}``
    dict returned by ``_resolve_candidate_currency`` and returns an additive block
    that exposes payload_sha, active_candidate_sha, integration_head_sha, stale, and
    stale_reason.  payload_compiled_sha is kept for backward-compat with older
    agent consumers that read it directly.  Only call this for a resolved
    (fresh/stale) currency dict — unresolved states go through the unknown /
    api-unreachable builders which never set ``stale``.
    """
    from datetime import datetime

    is_stale = not currency["current"]
    return {
        "verdict": "stale" if is_stale else "fresh",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "payload_sha": currency["candidate_sha"],
        "payload_compiled_sha": currency["candidate_sha"],
        "active_candidate_sha": currency["candidate_sha"],
        "integration_head_sha": currency["head_sha"],
        "generated_at": _freshness_generated_at(),
        "stale": is_stale,
        "stale_reason": (
            "payload_sha_differs_from_integration_head" if is_stale else None
        ),
    }


def _build_unknown_freshness_block(currency: dict | None = None) -> dict:
    """Build a freshness block for GENUINE local indeterminacy.

    Returns a block with verdict='unknown' so consumers can distinguish an
    unresolved freshness check from a confirmed-fresh or confirmed-stale result.
    Genuine indeterminacy means the local state could not produce a comparison:
    no open version, no candidate SHA, unavailable repo credentials, or a branch
    that does not exist.  Never sets ``stale`` — an unknown state is not stale.
    When available, carries the ``reason`` and ``active_candidate_sha`` so the
    agent can see WHY the check was indeterminate.
    """
    from datetime import datetime

    block: dict = {
        "verdict": "unknown",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "generated_at": _freshness_generated_at(),
    }
    if isinstance(currency, dict):
        if isinstance(currency.get("reason"), str) and currency["reason"]:
            block["reason"] = currency["reason"]
        if isinstance(currency.get("candidate_sha"), str) and currency["candidate_sha"]:
            block["active_candidate_sha"] = currency["candidate_sha"]
    return block


def _build_api_unreachable_freshness_block(currency: dict | None = None) -> dict:
    """Build a freshness block for a TRANSIENT GitHub/API/transport failure.

    Distinct from ``unknown``: the currency check could not complete because
    GitHub was unreachable (5xx / rate-limit / transport / timeout) after a
    bounded retry, NOT because the local state is genuinely indeterminate.  The
    agent must treat this as a temporary API-reachability problem — the payload
    is NOT known to be stale, so ``stale`` is never set.  Carries ``reason`` and
    ``active_candidate_sha`` when available for diagnostics.
    """
    from datetime import datetime

    block: dict = {
        "verdict": "api_unreachable",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "generated_at": _freshness_generated_at(),
    }
    if isinstance(currency, dict):
        if isinstance(currency.get("reason"), str) and currency["reason"]:
            block["reason"] = currency["reason"]
        if isinstance(currency.get("candidate_sha"), str) and currency["candidate_sha"]:
            block["active_candidate_sha"] = currency["candidate_sha"]
    return block


def _build_payload_staleness_block(currency: dict | None) -> dict:
    """Dispatch a currency dict to the correct freshness block by verdict.

    Resolved (fresh/stale) currency dicts produce a full SHA-freshness block;
    ``api_unreachable`` produces the transient-failure block; everything else
    (genuine local indeterminacy or a missing dict) produces the unknown block.
    """
    if not isinstance(currency, dict):
        return _build_unknown_freshness_block()
    # A resolved currency dict always carries a boolean ``current``.  Test this
    # directly (rather than the newer ``resolved`` flag) so legacy callers/mocks
    # that return the bare ``{"current", "candidate_sha", "head_sha"}`` shape are
    # still recognised as resolved.
    if isinstance(currency.get("current"), bool):
        return _build_freshness_shas_block(currency)
    if currency.get("verdict") == "api_unreachable":
        return _build_api_unreachable_freshness_block(currency)
    return _build_unknown_freshness_block(currency)


def _currency_unknown(reason: str, *, candidate_sha: str | None = None) -> dict:
    """Typed indeterminate-currency result (genuine local indeterminacy)."""
    result: dict = {"verdict": "unknown", "resolved": False, "reason": reason}
    if isinstance(candidate_sha, str) and candidate_sha:
        result["candidate_sha"] = candidate_sha
    return result


def _currency_api_unreachable(reason: str, *, candidate_sha: str | None = None) -> dict:
    """Typed transient-failure currency result (GitHub/API unreachable)."""
    result: dict = {"verdict": "api_unreachable", "resolved": False, "reason": reason}
    if isinstance(candidate_sha, str) and candidate_sha:
        result["candidate_sha"] = candidate_sha
    return result


# Bounded retry for the transient GitHub path in _resolve_candidate_currency.
# ``get_branch_sha`` already retries 5xx/rate-limit/transport failures inside
# ``_github_request``; this is a thin outer bound so a single transient blip that
# escapes that layer is retried once more before we classify api_unreachable.
_CURRENCY_RETRY_ATTEMPTS = 2
_CURRENCY_RETRY_DELAY_S = 0.75


async def _resolve_candidate_currency(
    session: AsyncSession,
    project: Project,
    last_qual: dict,
    integration_branch: str,
) -> dict:
    """Best-effort check: is a qualification's candidate commit still HEAD?

    The qualifier scores its audit findings against a pinned ``candidate_sha``
    snapshot, but the integration branch advances continuously and fixes for
    those very findings often land afterward.  Resolve the live integration
    branch HEAD and compare it to the qualification's candidate commit.

    Always returns a typed dict with a ``verdict`` field; never raises — a
    failure to resolve must never block the strategic payload build:

    * ``{"verdict": "fresh"|"stale", "resolved": True, "current": bool,
      "candidate_sha": str, "head_sha": str}`` when the comparison resolves.
      The resolved shape keeps the legacy ``current``/``candidate_sha``/
      ``head_sha`` keys for backward compatibility.
    * ``{"verdict": "api_unreachable", "resolved": False, "reason": str, ...}``
      when GitHub/transport failed transiently (5xx / rate-limit / timeout /
      connect) after a bounded retry — a temporary reachability problem, NOT a
      known-stale payload.
    * ``{"verdict": "unknown", "resolved": False, "reason": str, ...}`` for
      genuine local indeterminacy: no candidate SHA on the record, unavailable
      repo credentials, or a branch that does not exist.
    """
    import httpx
    from fastapi import HTTPException

    candidate_sha = last_qual.get("candidate_sha")
    if not candidate_sha or not isinstance(candidate_sha, str):
        return _currency_unknown("no_candidate_sha")

    # Resolve repo credentials once.  A None result (or a resolver error) is
    # genuine local indeterminacy — no transient GitHub call was even attempted.
    try:
        from app.release_qualifier import _resolve_github_with_session

        _resolved = await _resolve_github_with_session(session, project)
    except Exception:
        logger.exception(
            "[STRATEGIC] repo-credential resolution failed for candidate-currency check"
        )
        return _currency_unknown("repo_credentials_error", candidate_sha=candidate_sha)
    if _resolved is None:
        return _currency_unknown(
            "repo_credentials_unavailable", candidate_sha=candidate_sha
        )
    _gh_token, _gh_owner, _gh_repo = _resolved

    from app import github as _github

    last_transient_reason = "github_api_unreachable"
    for _attempt in range(_CURRENCY_RETRY_ATTEMPTS):
        try:
            head_sha = await _github.get_branch_sha(
                _gh_token, _gh_owner, _gh_repo, integration_branch
            )
        except HTTPException as exc:
            status_code = getattr(exc, "status_code", None)
            # A 404 means the integration branch genuinely does not exist — that
            # is not a transient reachability problem and retrying cannot help.
            if status_code == 404:
                return _currency_unknown(
                    "branch_not_found", candidate_sha=candidate_sha
                )
            last_transient_reason = (
                f"github_http_{status_code}" if status_code else "github_http_error"
            )
        except httpx.HTTPError as exc:
            # Transport-layer / timeout failure — transient by nature.
            last_transient_reason = f"github_transport_{type(exc).__name__}"
        except Exception:
            # Unexpected failure (e.g. malformed payload): stay conservative and
            # report genuine indeterminacy rather than claiming API-unreachable.
            logger.exception(
                "[STRATEGIC] could not resolve integration branch HEAD for "
                "candidate-currency check"
            )
            return _currency_unknown("resolve_error", candidate_sha=candidate_sha)
        else:
            if not head_sha or not isinstance(head_sha, str):
                return _currency_unknown(
                    "head_sha_unavailable", candidate_sha=candidate_sha
                )
            is_current = candidate_sha == head_sha
            return {
                "verdict": "fresh" if is_current else "stale",
                "resolved": True,
                "current": is_current,
                "candidate_sha": candidate_sha,
                "head_sha": head_sha,
            }
        # Transient failure — retry once more within the bounded budget.
        if _attempt < _CURRENCY_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_CURRENCY_RETRY_DELAY_S)

    logger.warning(
        "[STRATEGIC] candidate-currency check unreachable after %d attempt(s): %s",
        _CURRENCY_RETRY_ATTEMPTS,
        last_transient_reason,
    )
    return _currency_api_unreachable(
        last_transient_reason, candidate_sha=candidate_sha
    )


def ci_compliance_qualify_gate(
    project: Project,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Return whether current CI-compliance violations block qualification."""
    records = project.ci_compliance or {}
    selected_sha = head_sha
    if selected_sha is None and isinstance(records, dict) and records:
        selected_sha = next(reversed(records))
    record = records.get(selected_sha) if selected_sha else None
    if not isinstance(record, dict):
        return {
            "blocked": False,
            "head_sha": selected_sha,
            "violations": [],
            "violation_count": 0,
            "branch_literal_violations": [],
            "branch_literal_violation_count": 0,
            "reason": "ci_compliance_record_missing",
        }
    violations = record.get("violations") if isinstance(record.get("violations"), list) else []
    branch_literal_violations = (
        record.get("branch_literal_violations")
        if isinstance(record.get("branch_literal_violations"), list)
        else []
    )
    blocked = record.get("compliant") is False and (
        bool(violations) or bool(branch_literal_violations)
    )
    return {
        "blocked": blocked,
        "head_sha": selected_sha,
        "violations": violations,
        "violation_count": len(violations),
        "branch_literal_violations": branch_literal_violations,
        "branch_literal_violation_count": len(branch_literal_violations),
        "compliant": record.get("compliant"),
    }


async def _maybe_spawn_ci_compliance_cap_bump_fix(
    session: AsyncSession,
    project: Project,
    gate: dict[str, Any],
) -> Task | None:
    """Spawn one cap-bump remediation task for a non-compliant HEAD."""
    if not gate.get("blocked"):
        return None
    head_sha = gate.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return None

    active_statuses = {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
    rows_result = await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_(active_statuses),
        )
        .order_by(Task.created_at.desc())
        .limit(100)
    )
    rows = list(rows_result.scalars().all())
    if len(rows) >= 100:
        logger.warning(
            "[COMPLIANCE_CAP_BUMP] dedup query truncated to 100 rows "
            "for project=%s, head_sha=%s — dedup may miss existing tasks",
            project.id,
            head_sha,
        )
    for task in rows:
        context = task.context or {}
        if (
            context.get("spawned_by") == "ci_compliance_cap_bump_gate"
            and context.get("head_sha") == head_sha
        ):
            return task

    violations = gate.get("violations") if isinstance(gate.get("violations"), list) else []
    branch_literal_violations = (
        gate.get("branch_literal_violations")
        if isinstance(gate.get("branch_literal_violations"), list)
        else []
    )

    # Classify each capped violation as chronic or routine.
    chronic_violations: list[dict[str, Any]] = []
    routine_violations: list[dict[str, Any]] = []
    for v in violations:
        if not isinstance(v, dict):
            continue
        if v.get("cap") is None:
            # Uncapped: new file, never chronic — always a routine bump.
            routine_violations.append(v)
            continue
        try:
            verdict = await asyncio.to_thread(
                classify_breach,
                module_key=v["rel"],
                current_size=v["count"],
                section=v.get("section", "backend"),
            )
        except Exception:  # noqa: BLE001 — advisory; fall back to routine
            verdict = {"is_chronic": False}
        if verdict.get("is_chronic"):
            chronic_violations.append(v)
        else:
            routine_violations.append(v)

    has_chronic = bool(chronic_violations)

    branch_literal_lines = "\n".join(
        f"- {v.get('section')}/{v.get('rel')}:{v.get('line')}: {v.get('text')}"
        for v in branch_literal_violations
        if isinstance(v, dict)
    )
    prompt_parts = [
        "Automain HEAD is failing the CI-compliance guardrail.",
    ]
    if chronic_violations:
        chronic_lines = "\n".join(
            f"- {v.get('section')}/{v.get('rel')}: {v.get('count')} lines"
            f" (cap {v.get('cap')} — chronic offender, decomposition required)"
            for v in chronic_violations
        )
        prompt_parts += [
            "CHRONIC SIZE OFFENDERS — do NOT run `--fix` or bump the cap for these files.",
            "These modules have been cap-bumped repeatedly and must be DECOMPOSED:",
            chronic_lines,
            "For each chronic offender: extract cohesive clusters into a sibling module "
            "so the file falls back under its existing cap. Do not raise the cap.",
        ]
    if routine_violations:
        routine_lines = "\n".join(
            f"- {v.get('section')}/{v.get('rel')}: {v.get('count')} lines"
            + (f" > cap {v.get('cap')}" if v.get("cap") is not None else " (uncapped)")
            for v in routine_violations
        )
        prompt_parts += [
            "Routine size violations (run `python3 backend/testing/check_size_limits.py --fix` "
            "from the repo root and commit the updated `backend/testing/module_size_limits.json`):",
            routine_lines or "- see ci_compliance payload",
        ]
    if not chronic_violations and not routine_violations and violations:
        # Fallback: emit --fix for any violations we couldn't classify.
        violation_lines = "\n".join(
            f"- {v.get('section')}/{v.get('rel')}: {v.get('count')} lines"
            + (f" > cap {v.get('cap')}" if v.get("cap") is not None else " (uncapped)")
            for v in violations
            if isinstance(v, dict)
        )
        prompt_parts += [
            "Size violations (run `python3 backend/testing/check_size_limits.py --fix` "
            "from the repo root and commit the updated `backend/testing/module_size_limits.json`):",
            violation_lines or "- see ci_compliance payload",
        ]
    if branch_literal_violations:
        prompt_parts += [
            "Branch-literal violations (remove hardcoded branch names from the listed files):",
            branch_literal_lines,
        ]
    prompt_parts.append(f"\nHEAD: {head_sha}")
    prompt = "\n".join(prompt_parts)

    if has_chronic:
        task_title_payload = "Decompose chronic CI-compliance offenders"
        task_title = f"Decompose chronic CI-compliance offenders for {head_sha[:8]}"
        task_description = (
            "One or more modules are chronic size-cap offenders. "
            "Extract cohesive clusters into sibling modules to bring them under "
            "the existing caps — do NOT bump the caps."
        )
    else:
        task_title_payload = "Bump CI-compliance size caps"
        task_title = f"Fix CI-compliance cap bump for {head_sha[:8]}"
        task_description = (
            "Run backend/testing/check_size_limits.py --fix and commit the "
            "resulting size-cap update."
        )

    payload = validate_internal_payload(
        {
            "type": "create",
            "title": task_title_payload,
            "prompt": prompt,
            "base_branch": get_project_integration_branch(project),
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_ci_compliance_gate.py",
            },
        },
        "strategic_spawning:ci_compliance_cap_bump",
    )
    task = Task(
        project_id=project.id,
        title=task_title,
        description=task_description,
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={
            "spawned_by": "ci_compliance_cap_bump_gate",
            "ci_compliance_cap_bump": True,
            "head_sha": head_sha,
            "violation_count": gate.get("violation_count", 0),
        },
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "CI_COMPLIANCE_CAP_BUMP_GATE",
                "head_sha": head_sha,
            },
        )
    )
    return task


def _bound_retired_direction_grounding(
    grounding: Any, *, field_max: int = 500
) -> dict[str, Any] | None:
    """Return a bounded copy of a direction's ``retired_direction_grounding``.

    The write side (``build_retired_direction_grounding``) already produces a
    compact dict, but this defends the payload against oversized ``reason`` /
    ``summary`` text and drops any unexpected nested bodies so the active-
    direction summary stays within the same char discipline as its sibling
    context fields. Returns ``None`` when the input is not a usable mapping.
    """
    if not isinstance(grounding, dict):
        return None
    out: dict[str, Any] = {}
    direction_id = str(grounding.get("direction_id") or "").strip()
    if direction_id:
        out["direction_id"] = direction_id[:field_max]
    classification = str(grounding.get("classification") or "").strip()
    if classification:
        out["classification"] = classification[:field_max]
    reason = str(grounding.get("reason") or "").strip()
    if reason:
        out["reason"] = reason[:field_max]
    metrics = grounding.get("metrics")
    if isinstance(metrics, dict):
        bounded_metrics = {
            str(k): v
            for k, v in metrics.items()
            if isinstance(v, (int, float, bool)) or v is None
        }
        if bounded_metrics:
            out["metrics"] = bounded_metrics
    gap_id = grounding.get("gap_id")
    if gap_id:
        out["gap_id"] = str(gap_id)[:field_max]
    memo = grounding.get("retirement_memo")
    if isinstance(memo, dict):
        bounded_memo: dict[str, Any] = {}
        memo_id = memo.get("knowledge_doc_id")
        if memo_id:
            bounded_memo["knowledge_doc_id"] = str(memo_id)[:field_max]
        summary = str(memo.get("summary") or "").strip()
        if summary:
            bounded_memo["summary"] = summary[:field_max]
        if bounded_memo:
            out["retirement_memo"] = bounded_memo
    return out or None


def _bound_stall_advisory(
    advisory: Any, *, field_max: int = 500, evidence_max: int = 10
) -> dict[str, Any] | None:
    """Return the latest durable stall judgment in a prompt-safe shape.

    Stall advisories are qualitative decision records, not a score.  Keep only
    the fields the next Strategic cycle needs to revisit the call, and bound
    every value independently so one verbose rationale cannot crowd direction
    evidence out of the packed payload.
    """
    if not isinstance(advisory, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "blocking_invariant",
        "continuation_pivot_rationale",
        "recorded_at",
        "cycle_task_id",
        "invariant_fingerprint",
    ):
        value = advisory.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text[:field_max]
    recommendation = str(advisory.get("recommendation") or "").strip().lower()
    if recommendation in {"continue", "pivot"}:
        out["recommendation"] = recommendation
    evidence = advisory.get("new_evidence_since_previous_cycle")
    if isinstance(evidence, list):
        bounded_evidence = [
            str(item).strip()[:field_max]
            for item in evidence[:evidence_max]
            if str(item).strip()
        ]
        out["new_evidence_since_previous_cycle"] = bounded_evidence
    for key in ("version", "repeat_without_evidence_count", "cycle_index"):
        value = advisory.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    if isinstance(advisory.get("warning"), bool):
        out["warning"] = advisory["warning"]
    return out or None


def _build_direction_stats(
    project: Project,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the rich per-direction stats list the strategist consumes.

    Pure over ``project.directions`` (no DB / no network I/O): folds each active/
    paused/proposed direction's research history + aggregate into lifetime merge,
    proof, failure, autonomy, cost, and outcome-progress signals. Returns
    ``(included_directions, direction_stats)`` — the cadence caller reuses
    ``included_directions`` for its mode-hint lines. Used by the cadence
    ``_spawn_strategic_review`` payload build.
    """
    # Include `active` + `paused` + `proposed`: paused entries are a strong user
    # signal the strategic agent should respect rather than re-propose; proposed
    # entries exist so the strategic agent can approve/reject them in the same
    # cycle.
    included_directions = [
        d
        for d in (project.directions or [])
        if d.get("status") in ("active", "paused", "proposed")
    ]
    direction_stats: list[dict[str, Any]] = []
    for d in included_directions:
        history = d.get("research_history", [])
        agg = d.get("history_aggregate", {})
        total_cycles = len(history) + (agg.get("total_cycles") or 0)
        total_merged = sum(h.get("prs_merged", 0) for h in history) + (
            agg.get("total_prs_merged") or 0
        )
        # Verified lifetime merge total — degraded/unverified merges excluded
        # (legacy fallback baked into verified_merged_count / the aggregate .get).
        # The displayed prs_merged and operator_rejection_rate below stay RAW
        # (operator-judgment metrics); only the activity-score selection input
        # reads this verified field.
        total_verified_merged = sum(verified_merged_count(h) for h in history) + (
            agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
        )
        # Proven lifetime merge total — STRICTER than verified: only merges with
        # machine-checked named-test proof OR recorded downstream-artifact
        # consumption (proven_merged_count); legacy entries degrade to 0. Always
        # <= total_verified_merged for the same window, so a direction that
        # shipped many merges but proved few is visibly distinct.
        total_proven_merged = sum(proven_merged_count(h) for h in history) + (
            agg.get("total_acceptance_proven_merged", 0) or 0
        )
        total_consumed_merged = sum(
            h.get("acceptance_consumed_merged", 0) for h in history
        ) + (agg.get("total_acceptance_consumed_merged", 0) or 0)
        # Ancestry-reachable lifetime merge total — returns None for legacy entries
        # (unknown reachability); aggregate falls back to 0. Only included in
        # stat_entry when at least one history entry has reachable_prs_merged.
        _reachable_counts = [reachable_merged_count(h) for h in history]
        _any_reachable_recorded = any(c is not None for c in _reachable_counts) or bool(
            agg.get("total_reachable_prs_merged")
        ) or (agg.get("total_reachable_recorded_cycles", 0) or 0) > 0
        total_reachable_merged: int | None = None
        if _any_reachable_recorded:
            total_reachable_merged = sum(
                c for c in _reachable_counts if c is not None
            ) + (agg.get("total_reachable_prs_merged", 0) or 0)
        # Declaration coverage = declared / merged-write, recomputed from summed
        # counts (not averaged per-cycle fractions) so it survives eviction.
        total_declared_merged = sum(declared_merged_count(h) for h in history) + (
            agg.get("total_acceptance_declared_merged", 0) or 0
        )
        total_merged_write = sum(
            h.get("acceptance_merged_write_total", 0) for h in history
        ) + (agg.get("total_acceptance_merged_write_total", 0) or 0)
        acceptance_declaration_coverage = (
            round(total_declared_merged / total_merged_write, 3)
            if total_merged_write > 0
            else None
        )
        total_rejected = sum(h.get("prs_rejected", 0) for h in history) + (
            agg.get("total_prs_rejected") or 0
        )
        total_failed = sum(h.get("tasks_failed", 0) for h in history) + (
            agg.get("total_tasks_failed") or 0
        )
        total_failed_infra = sum(h.get("tasks_failed_infra", 0) for h in history) + (
            agg.get("total_tasks_failed_infra") or 0
        )
        # Autonomy lifetime totals + retirement eligibility — derived by the
        # single source of truth (compute_direction_autonomy_state) rather than
        # re-computed inline, so this payload can never silently disagree with the
        # helper. It credits only verifiably-landed autonomous merges toward the
        # 10-merge milestone and applies the same two-step legacy fallback.
        _autonomy_state = compute_direction_autonomy_state(d)
        lifetime_autonomous = _autonomy_state["lifetime_autonomous_prs_merged"]
        lifetime_op_actions = _autonomy_state["lifetime_operator_actions"]
        retirement_eligible = _autonomy_state["retirement_eligible"]
        taxonomy_totals: dict[str, int] = {}
        for h in history:
            for tkey, tval in (h.get("taxonomy_summary") or {}).items():
                if tval:
                    taxonomy_totals[tkey] = taxonomy_totals.get(tkey, 0) + tval
        recent_history = history[-5:]
        recent_findings = []
        recent_failures = []
        _agg_grade: dict = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        _agg_basis: dict = {
            "code_verified": 0,
            "path_verified": 0,
            "pattern_inferred": 0,
            "deduced": 0,
        }
        _agg_challenge = _empty_finding_challenge_summary()
        for h in recent_history:
            for desc in h.get("key_finding_descriptions") or []:
                if desc and desc not in recent_findings:
                    recent_findings.append(desc)
            recent_failures.extend(
                fd for fd in (h.get("failed_task_details") or []) if fd
            )
            for _gk, _gv in (h.get("grade_distribution") or {}).items():
                if _gk in _agg_grade:
                    _agg_grade[_gk] = _agg_grade.get(_gk, 0) + int(_gv or 0)
            for _bk, _bv in (h.get("verification_basis_distribution") or {}).items():
                if _bk in _agg_basis:
                    _agg_basis[_bk] = _agg_basis.get(_bk, 0) + int(_bv or 0)
            _merge_finding_challenge_summary(
                _agg_challenge, h.get("finding_challenge_summary")
            )
        total_prs = total_merged + total_rejected
        operator_rejection_rate = (
            round(total_rejected / total_prs, 2) if total_prs > 0 else None
        )
        stat_entry: dict = {
            "id": d.get("id"),
            "text": d.get("text"),
            "priority": d.get("priority"),
            "status": d.get("status"),
            "total_cycles": total_cycles,
            "prs_merged": total_merged,
            "verified_prs_merged": total_verified_merged,
            # badge_only = merges counted via is_merged_completion badge but not
            # yet ancestry-verified; max(0,...) guards against legacy data where
            # verified could transiently exceed raw due to aggregate drift.
            "badge_only_prs_merged": max(0, total_merged - total_verified_merged),
            # Proven count + declaration coverage: the honest, machine-checked
            # subset of verified merges (proven <= verified) plus how much merged
            # code even committed to forward evidence. Lets the strategist
            # prioritize closing the proof gap, not just delivery.
            "proven_prs_merged": total_proven_merged,
            "consumed_prs_merged": total_consumed_merged,
            "acceptance_declaration_coverage": acceptance_declaration_coverage,
            "tasks_failed": total_failed,
            **({"reachable_prs_merged": total_reachable_merged} if total_reachable_merged is not None else {}),
            "possibly_saturated": d.get("possibly_saturated", False),
        }
        # Surface the direction→gap linkage so the strategic agent can reconcile
        # active directions against problem-model gaps (which gaps have a working
        # direction, which directions trace to no gap) without cross-referencing.
        if d.get("gap_id"):
            stat_entry["gap_id"] = str(d["gap_id"])
        if total_failed_infra > 0:
            stat_entry["tasks_failed_infra"] = total_failed_infra
        if taxonomy_totals:
            stat_entry["taxonomy_summary"] = taxonomy_totals
        stat_entry["quality_score"] = round(_compute_direction_quality_score(d), 3)
        stat_entry["lifetime_autonomous_prs_merged"] = lifetime_autonomous
        stat_entry["lifetime_operator_actions"] = lifetime_op_actions
        stat_entry["retirement_eligible"] = retirement_eligible
        if total_rejected > 0:
            stat_entry["prs_rejected"] = total_rejected
        if operator_rejection_rate is not None and total_rejected > 0:
            stat_entry["operator_rejection_rate"] = operator_rejection_rate
        from app.routers.admin_analytics import ON_DEMAND_RATE
        from app.strategic_analysis import _entry_cost_usd

        # Include evicted cycles folded into history_aggregate so the lifetime
        # cost numerator is not truncated at the 10-cycle eviction window — the
        # denominator (total_merged) already folds in the evicted aggregate, so
        # a window-only numerator would understate cost for aged directions.
        # Mirror strategic_analysis.summarize_direction_outcomes exactly.
        try:
            agg_cost = max(0.0, float(agg.get("total_estimated_cost_usd") or 0.0))
        except (TypeError, ValueError):
            agg_cost = 0.0
        estimated_cost_usd = round(
            sum(_entry_cost_usd(h, ON_DEMAND_RATE) for h in history) + agg_cost, 2
        )
        if estimated_cost_usd > 0:
            stat_entry["estimated_cost_usd"] = estimated_cost_usd
            cpm = canonical_cost_per_merged_pr(estimated_cost_usd, total_merged)
            if cpm is not None:
                stat_entry["cost_per_merged_pr"] = cpm
        ctx = d.get("research_context")
        if ctx:
            stat_entry["research_context"] = ctx[:3000]
        # Surface the direction's stated outcome goal so the strategic agent's
        # per-cycle review is grounded in success_evidence/retirement_condition,
        # not just merge/cost metrics. Mirror the write-side cap from main.py.
        _DIR_FIELD_MAX = 500
        dir_context = d.get("context") or {}
        _success_evidence = str(dir_context.get("success_evidence") or "").strip()
        if _success_evidence:
            stat_entry["success_evidence"] = _success_evidence[:_DIR_FIELD_MAX]
        _retirement_condition = str(
            dir_context.get("retirement_condition") or ""
        ).strip()
        if _retirement_condition:
            stat_entry["retirement_condition"] = _retirement_condition[:_DIR_FIELD_MAX]
        # Retired-origin grounding: when this direction was created as a
        # continuation/re-staffing of a retired one, strategic_completion stores
        # a compact grounding object under context["retired_direction_grounding"]
        # (classification/metrics/reason + optional gap_id/retirement_memo). Surface
        # it — bounded like the sibling context fields — so the strategist can tell
        # an active direction picks up prior work and how that prior work ended.
        _grounding = _bound_retired_direction_grounding(
            dir_context.get("retired_direction_grounding"), field_max=_DIR_FIELD_MAX
        )
        if _grounding:
            stat_entry["retired_direction_grounding"] = _grounding
        # The latest advisory stall call is durable decision context for the
        # next Strategic cycle.  Surface it next to the direction's evidence
        # contract so the agent must reconcile what remained blocked, what new
        # evidence arrived, and whether the recommendation was continue/pivot.
        # This remains qualitative and advisory: no numeric information-gain
        # score is synthesized here.
        _stall_advisory = _bound_stall_advisory(d.get("stall_advisory"))
        if _stall_advisory:
            stat_entry["stall_advisory"] = _stall_advisory
        # Advisory outcome-less-scaffolding flag: surfaces a long-running direction
        # that keeps merging PRs but never advances its success_evidence, so the
        # strategic agent sees it as a first-class signal (not just merge/cost metrics).
        if d.get("outcome_scaffolding_flagged"):
            stat_entry["outcome_scaffolding_flagged"] = True
            _osc_ev = d.get("outcome_scaffolding_evidence")
            if _osc_ev:
                stat_entry["outcome_scaffolding_evidence"] = _osc_ev
        _rfe = d.get("recent_findings_evidence") or []
        if _rfe:
            from app.research_context import filter_direction_evidence

            _filtered_ev = filter_direction_evidence(ctx, _rfe)
            if _filtered_ev:
                stat_entry["recent_findings_evidence"] = _filtered_ev
        if recent_findings:
            stat_entry["recent_findings"] = recent_findings[:10]
        if any(_agg_grade.values()):
            stat_entry["recent_findings_grade_distribution"] = _agg_grade
        if any(_agg_basis.values()):
            stat_entry["recent_findings_verification_basis"] = _agg_basis
        if _agg_challenge["challenged_findings"] > 0:
            stat_entry["recent_findings_challenge_summary"] = _agg_challenge
        if recent_failures:
            stat_entry["recent_failures"] = recent_failures[:5]
        # Surface cumulative confirmed-finding count and trend from
        # outcome_progress_history (eviction-robust signal used by scaffolding
        # detector). Shared helper keeps thresholds in sync with strategic_analysis.
        from app.strategic_completion import outcome_progress_summary_fields

        stat_entry.update(outcome_progress_summary_fields(d))
        if d.get("system_id"):
            stat_entry["system_id"] = d["system_id"]
        if d.get("status") == "paused":
            stat_entry["auto_paused"] = bool(d.get("auto_paused"))
            if d.get("paused_reason"):
                stat_entry["paused_reason"] = d.get("paused_reason")
            if d.get("lifetime_burn_pause_count") is not None:
                stat_entry["lifetime_burn_pause_count"] = d.get(
                    "lifetime_burn_pause_count"
                )
        elif d.get("status") == "proposed":
            if d.get("proposed_by"):
                stat_entry["proposed_by"] = str(d["proposed_by"])
            if d.get("proposal_rationale"):
                stat_entry["proposal_rationale"] = d["proposal_rationale"]
            if d.get("proposed_at"):
                stat_entry["proposed_at"] = d["proposed_at"]
        # Surface auto_retire_pending metadata regardless of direction status:
        # the scaffolding retire path sets auto_retire_pending=True while keeping
        # the direction active (retired on the next lifecycle tick), so this must
        # not be gated on status=="paused".
        if d.get("auto_retire_pending"):
            stat_entry["auto_retire_pending"] = True
            if d.get("auto_retire_pending_reason"):
                stat_entry["auto_retire_pending_reason"] = d.get(
                    "auto_retire_pending_reason"
                )
            if d.get("auto_retire_pending_at"):
                stat_entry["auto_retire_pending_at"] = d.get(
                    "auto_retire_pending_at"
                )
        # Churn-family retirement recommendation (churn-guardrail redesign
        # d-73f03cc0): the zombie ladder no longer auto-retires; it flags a
        # reviewable recommendation and keeps the direction paused. Surface the
        # flag + bounded evidence so the strategist can decide whether to retire
        # it through the existing retirement path or give it a new approach.
        if d.get("churn_retirement_recommended"):
            stat_entry["churn_retirement_recommended"] = True
            _cre_ev = d.get("churn_retirement_evidence")
            if _cre_ev:
                stat_entry["churn_retirement_evidence"] = _cre_ev
            if d.get("churn_retirement_recommended_reason"):
                stat_entry["churn_retirement_recommended_reason"] = d.get(
                    "churn_retirement_recommended_reason"
                )
            if d.get("churn_retirement_recommended_at"):
                stat_entry["churn_retirement_recommended_at"] = d.get(
                    "churn_retirement_recommended_at"
                )
        # Surface non-default recommended_mode so the strategic agent can bias
        # its direction decisions (audit/fundamentals → prefer non-feature work;
        # pause → tactical spawning should be skipped by autonomous_research).
        # 'ship' and unset are equivalent (baseline behavior) so they're omitted.
        _rec_mode = d.get("recommended_mode")
        if _rec_mode and _rec_mode != "ship":
            stat_entry["recommended_mode"] = _rec_mode
        # Research spawn-gate effectiveness (distinct from strategic self-challenge).
        # Only emitted when the direction has actual spawn-gate activity recorded.
        from app.digest_direction_progress import compute_spawn_gate_aggregate

        _sgm = compute_spawn_gate_aggregate(history)
        if _sgm is not None:
            stat_entry["spawn_gate_metrics"] = _sgm
        direction_stats.append(stat_entry)
    return included_directions, direction_stats


def _build_direction_counts(project: Project) -> dict[str, Any]:
    """Authoritative, trim-proof direction tally for the strategist payload.

    Pure over ``project.directions`` + runtime config (no DB / no network):
    ``{active, paused, total, max_active, by_system}``. Used by the cadence
    payload build so it anchors on the true active set even when
    ``directions_summary`` is trimmed.
    """
    _all_directions = project.directions or []
    _active_directions_count = sum(
        1 for d in _all_directions if d.get("status") == "active"
    )
    _max_active_directions = _runtime_config.get("max_active_directions", 10)
    # Authoritative per-system ACTIVE-direction tally, computed from the FULL
    # direction set before any trimming. Nested under ``direction_counts`` so it
    # inherits the same ``critical`` trim-proof packing — this lets the agent's
    # per-system coverage-gap and concentration-risk signals stay truthful even
    # when ``directions_summary`` is trimmed for budget. Directions with no
    # ``system_id`` simply do not contribute.
    _direction_active_by_system: dict[str, int] = {}
    for _d in _all_directions:
        if _d.get("status") != "active":
            continue
        _sys_id = _d.get("system_id")
        if not _sys_id:
            continue
        _direction_active_by_system[_sys_id] = (
            _direction_active_by_system.get(_sys_id, 0) + 1
        )
    return {
        "active": _active_directions_count,
        "paused": sum(1 for d in _all_directions if d.get("status") == "paused"),
        "total": sum(
            1
            for d in _all_directions
            if d.get("status") in ("active", "paused", "proposed")
        ),
        "max_active": _max_active_directions,
        "by_system": _direction_active_by_system,
    }


async def _finalize_strategic_review_spawn(
    session: AsyncSession,
    *,
    project_id: Any,
    project_name: str,
    payload: dict[str, Any],
    cycle_id: str,
    strategy_only: bool,
    base_vision: dict[str, Any],
    base_problem_model: dict[str, Any],
    base_running_update: str,
    local_runner_intent: dict[str, Any],
    preferred_runner_target_id: str | None,
    extra_context: dict[str, Any] | None,
    operator_steered: bool,
    steering_excerpt: str | None,
    launched_by_user_id: str | None = None,
    runner_pin_id: str | None = None,
    integrity_conflict_counts: dict[str, int] | None = None,
    expected_legacy_generation: int | None = None,
) -> bool:
    """Guarded INSERT/commit tail for the cadence spawn path.

    Re-arms the duplicate-spawn guards, creates the ``strategic_review`` task
    (born already carrying ``extra_context``), records the StrategicCycle row,
    commits, and emits the cycle-started event — the short, sub-second
    transaction the caller finishes on. The cadence path reaches here after its
    ~30s payload build.

    Re-arm the duplicate-spawn guards before the INSERT: the cadence payload
    build takes ~30s in prod and ``_build_recent_interventions`` COMMITS mid-way
    (#8842 — releases the read transaction before its GitHub ancestry fan-out).
    That commit also releases the research-spawn advisory xact-lock taken by the
    autonomous tick (#8927) and the project-row FOR UPDATE taken by
    ``_has_active_strategic_task`` at the top of the caller, so everything after
    it was an unguarded check-then-act window: two orchestrator instances both
    passed the top guard before either's spawn committed and double-spawned a
    cycle (2026-06-11, d8f82aa8 / 39eea7b8, 29s apart). Re-take the lock and
    re-run the active check on the current transaction so check→INSERT→commit
    shrinks back to milliseconds. Bail rolls back so the session is clean for
    the caller — the discarded build-state (drift snapshot, version bootstrap)
    is recomputed by the surviving spawner's cycle.
    """
    from app.autonomous_research import _try_research_spawn_advisory_lock
    from app.task_lifecycle import _has_active_strategic_task

    if expected_legacy_generation is not None:
        try:
            await lock_and_validate_legacy_authority(
                session,
                project_id,
                expected_generation=expected_legacy_generation,
            )
        except LegacyEngineAuthorityExpired:
            await session.rollback()
            return False

    # ``project_id`` was captured at the caller's function entry (before any
    # payload build), so the rollback-expiry MissingGreenlet (BUG_REPORT
    # e530efec) cannot bite this finalize path even when the connection dropped
    # mid-build.
    if not await _try_research_spawn_advisory_lock(session, project_id):
        await session.rollback()
        logger.warning(
            "[STRATEGIC] Aborting strategic review spawn for project %s — a peer "
            "instance took the research-spawn lock during the payload build",
            project_id,
        )
        return False
    if await _has_active_strategic_task(project_id, session):
        await session.rollback()
        logger.warning(
            "[STRATEGIC] Aborting strategic review spawn for project %s — a "
            "strategic task appeared during the payload build",
            project_id,
        )
        return False

    effective_preferred_runner_target_id = (
        str(preferred_runner_target_id)
        if preferred_runner_target_id
        else local_runner_intent.get("preferred_runner_target_id")
    )
    task_context: dict[str, Any] = {"strategic_cycle_id": cycle_id}
    if local_runner_intent.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY):
        task_context[STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY] = True
        task_context[STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY] = local_runner_intent.get(
            STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
            "workbench_continuous",
        )
    if effective_preferred_runner_target_id:
        task_context["preferred_runner_target_id"] = effective_preferred_runner_target_id
    # Seed any caller-supplied born-context LAST so the task is committed already
    # carrying it in its first (and only) creating INSERT — no separate follow-up
    # transaction, no claimable-without-the-flag window. Merged over the derived
    # context so an explicit caller value wins on collision.
    if extra_context:
        task_context.update(extra_context)

    # Runner-pin AT BIRTH (``runner_pin_id`` is None for every cadence caller,
    # leaving their QUEUED + fresh ``agent-vm`` id and normal VM-dispatch entry
    # unchanged). When set, the task is born already PROVISIONING and pinned to
    # the operator's local runner so ``POST /api/runner/next-task`` hands it
    # straight to that runner — independent of the general VM-dispatch cycle
    # (which may be paused, ``max_concurrent_vms=0``). This mirrors the
    # orchestrator's own pin contract (``orchestrator._try_runner_dispatch``:
    # ``vm_instance_id='runner:{id}'`` + ``context['runner_target_id']`` +
    # PROVISIONING + ``vm_provisioned_at``) at the INSERT instead of a beat later.
    # ``started_at`` stays NULL (the runner stamps it on pull), which also keeps
    # the reaper carve-out
    # (``reaper_orphan_classify._is_runner_pinned_awaiting_pull``) protecting it
    # while the operator's runner is briefly offline.
    if runner_pin_id:
        task_context["runner_target_id"] = str(runner_pin_id)
        born_vm_instance_id = f"runner:{runner_pin_id}"
        born_status = TaskStatus.PROVISIONING
        born_vm_provisioned_at: datetime | None = _utcnow()
    else:
        born_vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
        born_status = TaskStatus.QUEUED
        born_vm_provisioned_at = None

    new_task = Task(
        project_id=project_id,
        title=f"Strategic review: {project_name}",
        description="Vision and direction review",
        payloads=[payload],
        vm_instance_id=born_vm_instance_id,
        status=born_status,
        vm_provisioned_at=born_vm_provisioned_at,
        created_by="ORCHESTRATOR",
        launched_by_user_id=launched_by_user_id,
        context=task_context,
        strategic_cycle_id=cycle_id,
    )
    session.add(new_task)
    await session.flush()  # assign new_task.id for the cycle update below
    history = TaskHistory(
        task_id=new_task.id,
        action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "strategic_review"},
    )
    session.add(history)
    if strategy_only:
        # Phase-2-only cycle: create the row now (architecture cycles created
        # theirs at design_doc spawn). ``phase1_*`` stays empty — there is no
        # architecture phase — and the baselines were snapshotted at function
        # entry while the instance was live. Created only after the re-armed
        # spawn guards above passed, so a bail leaves no orphan row.
        await strategic_cycle.create(
            session,
            project_id=project_id,
            cycle_id=cycle_id,
            phase1_task_id=None,
            baseline_vision=base_vision,
            baseline_problem_model=base_problem_model,
            baseline_running_update=base_running_update,
        )
    # Audit W2: record phase1 completion + phase2 task_id on the cycle row
    # instead of patching vision._strategic_cycle_history JSONB. For a
    # strategy-only cycle this flips the just-created row to phase2_running with
    # phase1_completed_at set (its architecture phase is a no-op).
    _cycle_row = await strategic_cycle.mark_phase1_completed(
        session,
        cycle_id,
        phase2_task_id=new_task.id,
    )
    if _cycle_row is not None and integrity_conflict_counts is not None:
        strategic_cycle.record_integrity_conflict_counts(_cycle_row, integrity_conflict_counts)
        session.add(_cycle_row)
    await commit_legacy_authorized(
        session, project_id, expected_legacy_generation
    )
    # Post-commit: signal the admin UI that a strategic cycle (Phase 2) started.
    from app.strategic_events import emit_strategic_cycle_started

    await emit_strategic_cycle_started(
        project_id,
        new_task.id,
        2,
        cycle_id=cycle_id,
        steering_origin=ORIGIN_OPERATOR if operator_steered else None,
        steering_excerpt=steering_excerpt,
    )
    logger.info(
        f"[STRATEGIC] Spawned strategic_review task {new_task.id} for project {project_id}"
    )
    return True


async def _spawn_strategic_review(
    project: Project,
    session: AsyncSession,
    cycle_id: str | None = None,
    operator_steered: bool = False,
    steering_excerpt: str | None = None,
    preferred_runner_target_id: str | None = None,
    local_runner_context: dict[str, Any] | None = None,
    extra_context: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
    resident_minimal: bool = False,
    runner_pin_id: str | None = None,
    *,
    launched_by_user_id: str | None = None,
    apply_spawn_budget_guard: bool = False,
    expected_legacy_generation: int | None = None,
) -> bool:
    """Spawn Phase 2 of the strategic cycle: vision + direction review.

    Called by _handle_design_doc_completion after the design doc is updated.
    Uses type 'strategic_review' so it doesn't block tactical research.
    Returns True if a task was created, False if skipped.

    ``extra_context`` (default ``None`` — every cadence caller is unaffected)
    seeds additional keys into the created task's ``context`` in the SAME
    INSERT/commit that creates the task, so the committed task is BORN already
    carrying them (it never exists in a committed state without them). Merged
    over the context this function derives, so an explicit ``extra_context``
    value wins on any key collision (e.g. the local-runner pin), leaving
    runner-pin semantics identical to stamping it a beat later.

    ``extra_payload`` is the equivalent birth-time contract for the task's
    first payload. It is reserved for the minimal Resident spawn path: the
    task reaches a warm runner already carrying the foreground capability
    configuration, rather than relying on a runner-local environment toggle.

    ``operator_steered`` carries the operator-origin signal forward when this
    spawn is the Phase 2 tail of an architecture cycle. On that path the
    pending refresh marker has already been consumed by the autonomous loop's
    spawn-record (it cleared once for the Phase 1 design_doc spawn), so
    ``strategic_refresh_origin(project.vision)`` reads ``None`` here and the
    steering directive would otherwise be dropped (~1/3 of operator wakes land
    on an architecture cycle). The caller threads the flag through the
    design_doc task context so the directive still reaches the strategist.

    ``steering_excerpt`` is the operator's message text, threaded forward on the
    architecture-cycle Phase 2 path (where the marker — and its persisted
    excerpt — was already consumed at Phase 1 spawn). On the strategy-only path
    it is ``None`` and the excerpt is read directly from the still-armed marker.
    Either way it feeds ``_build_operator_steering_directive`` as the durable
    fallback so the directive survives the operator message aging out of the
    volatile conversation-thread snapshot.

    ``runner_pin_id`` (default ``None`` — every cadence caller is unaffected, so
    their task is born QUEUED + ``agent-vm`` and enters normal VM dispatch) is an
    AUTHORITATIVE local-runner pin. When set, the created ``strategic_review`` is
    born already PROVISIONING with ``vm_instance_id='runner:{id}'`` +
    ``context['runner_target_id']`` (the exact contract ``POST
    /api/runner/next-task`` filters on) so the operator's local runner claims it
    directly, independent of the general VM-dispatch cycle (which may be paused).
    Unlike ``preferred_runner_target_id`` (a non-authoritative affinity hint
    dispatch may or may not honor), this is a hard birth pin.

    ``preferred_runner_target_id`` is a non-authoritative local-runner affinity
    hint from the preceding architecture phase. Dispatch may honor it only if
    that runner is still live, eligible, agent-available, and has capacity.

    ``local_runner_context`` carries a hard local-only Strategic intent from
    the Phase 1 task context. It is captured by the caller before any retry or
    rollback-prone work so this function does not need to re-read mutable
    project ORM attributes in the post-build finalize path.
    """
    from app.task_lifecycle import _has_active_strategic_task

    try:
        if expected_legacy_generation is None and not resident_minimal:
            expected_legacy_generation = await lock_and_validate_legacy_authority(
                session, project.id
            )
            await session.commit()
    except LegacyEngineAuthorityExpired:
        await session.rollback()
        return False

    if extra_payload and not resident_minimal:
        raise ValueError("extra_payload is reserved for resident_minimal strategic spawns")

    # Guard: no design_doc_update or strategic_review already running
    if await _has_active_strategic_task(project.id, session):
        logger.warning(
            f"[STRATEGIC] Skipping strategic review — strategic task already active for {project.id}"
        )
        return False

    # Guard: defer implementation spawning until repo_bootstrap completes.
    # If bootstrap_completed_at is unset AND there is a pending bootstrap task,
    # this project is still being onboarded — skip this cycle. If bootstrap has
    # FAILED or is otherwise terminal, proceed gracefully (degraded path).
    if await _bootstrap_pending(project, session):
        await _maybe_notify_operator_bootstrap_deferred(
            project,
            session,
            "repo_bootstrap task is pending (bootstrap_completed_at not set)",
        )
        logger.info(
            "[STRATEGIC] Deferring strategic review for project %s — "
            "repo_bootstrap task is pending (bootstrap_completed_at not set)",
            project.id,
        )
        return False

    if apply_spawn_budget_guard:
        budget_guard = await _strategic_spawn_budget_guard(session, "initiative")
        if budget_guard is not None:
            logger.warning("[AUTONOMOUS] Spawn budget block: %s", budget_guard)
            return False

    # Capture project scalars — and, for strategy-only cycles, the baseline
    # snapshots — NOW, while ``project`` is guaranteed live (it was fresh-read
    # under the caller's lock). The payload build below takes ~30s and
    # ``_build_recent_interventions`` COMMITs mid-way; a Cloud SQL connection
    # drop anywhere in that window invalidates the connection and rolls the
    # session back, which EXPIRES every ORM instance (expire_on_commit=False
    # governs commit, not rollback). After that, any synchronous
    # ``project.<attr>`` read lazy-loads and raises MissingGreenlet in async
    # context. That is exactly what broke the architecture Phase1->Phase2
    # handoff (_handle_design_doc_completion -> here): every architecture cycle
    # stalled and the digest "latest strategic cycle" card went blank. The
    # previous capture sat AFTER the build, so the capture line itself could
    # raise; holding the scalars here means the post-build INSERT/finalize path
    # never touches the expirable instance.
    strategy_only = cycle_id is None
    _project_id = project.id
    _project_name = project.name
    _base_vision: dict = {}
    _base_problem_model: dict = {}
    _base_running_update: str = ""
    if strategy_only:
        # Strategy-only cycles (no architecture Phase 1) historically minted no
        # cycle_id and so were never recorded in StrategicCycle. With
        # architecture_review_frequency defaulting to every 3rd cycle, ~2/3 of
        # all strategic cycles were invisible to the digest "latest cycle" card
        # and to the strategist's own cycle-history context — and the card had
        # nothing fresh to show whenever the rare (and fragile) architecture
        # cycle stalled. Mint a cycle id and snapshot the baselines so this
        # review is recorded and finalized by the normal phase-2 completion
        # accounting (strategic_completion.mark_phase2_completed), exactly like
        # an architecture cycle's Phase 2.
        cycle_id = strategic_cycle.new_cycle_id()
        _base_vision = dict(project.vision or {})
        _base_problem_model = sanitize_problem_model_raw(project.problem_model or {})
        _base_running_update = (
            project.running_update if isinstance(project.running_update, str) else ""
        )
    local_runner_intent = dict(local_runner_context or {})
    if not local_runner_intent.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY):
        local_runner_intent = await _resolve_local_continuation_runner_intent(
            session,
            _project_id,
            "strategic_review",
        )

    if resident_minimal:
        # A resident strategic session runs SYNCHRONOUSLY inside the operator's
        # START request and then builds most of its own context on the runner via
        # Claude Code. So skip ONLY the slow, GitHub-fetching context builders the
        # cadence path runs below — ``ground_design_doc`` +
        # ``_verify_capability_backing_paths`` (per-file ``contents``/``git/trees``
        # fetches), ``_build_recent_interventions`` (ancestry fan-out), and
        # ``resolve_branch_verification_context`` (credential resolve), plus the
        # branch-SHA / version-phase / ci-compliance lookups they feed. Those hold
        # the request's DB connection open long enough for asyncpg/Cloud SQL to
        # idle-close it, so the finalize flush 500s before ``task_id`` ever lands
        # (start_project_strategic_resident_session, ~143s).
        #
        # STILL include the CHEAP grounding — pure in-memory ORM scalars +
        # DB-only reads, none of which hold a connection across a network call —
        # so the resident agent is NOT context-blind at boot: without ``vision`` /
        # ``problem_model`` / ``running_update`` / ``directions_summary`` /
        # ``direction_counts`` / the retired/prior-decision context below it would
        # re-derive (and risk re-creating) existing directions, and without
        # ``open_messages`` an operator message older than the resident's
        # ~2-minute first-poll cursor would surface NOWHERE (not in bootstrap,
        # not in the first poll). ``problem_model`` is the UNVERIFIED agent-view
        # (the backing-path verification is exactly the skipped GitHub I/O). The
        # transaction stays short: cheap reads only, zero network.
        review_model = (
            _runtime_config.get("task_type_models", {}).get("strategic_review")
            or _runtime_config.get("task_type_models", {}).get("research")
            or None
        )
        review_effort = (
            _runtime_config.get("task_type_efforts", {}).get("strategic_review")
            or _runtime_config.get("task_type_efforts", {}).get("research")
            or None
        )
        _res_direction_counts = _build_direction_counts(project)
        _res_direction_stats = _build_direction_stats(project)[1]
        from app.strategic_analysis import _build_retired_directions_summary
        from app.direction_event_trail import repair_retired_direction_memory_texts

        cycle_history = await strategic_cycle.recent_for_agent_context(
            session,
            project.id,
            limit=20,
        )
        _vision_for_repair = project.vision or {}
        if await repair_retired_direction_memory_texts(session, project.id, _vision_for_repair):
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(project, "vision")
            session.add(project)
        _res_retired_summary = _build_retired_directions_summary(project)
        _res_prev_outcomes = _build_previous_decision_outcomes(project, cycle_history)
        _res_open_messages = await _fetch_open_messages(session, project.id)
        # Recently-answered operator conversation tail — DB-only, bounded, and
        # explicitly non-actionable. Surfaces a correction the operator just made
        # (now marked handled/resolved, so gone from the open feed) so a restarted
        # resident session does not re-propose what the operator already settled.
        _res_recent_handled = await _fetch_recent_handled_conversation(
            session, project.id
        )
        # Ship the resident-session pacing knobs from _runtime_config onto the
        # payload so the agent's residentConfig() reads LIVE, operator-tunable
        # values instead of falling back to hardcoded defaults. The exploration
        # interval is deliberately distinct from cheap polling/heartbeats: only
        # an unchanged-state MODEL turn uses it. All are tiny critical-tier
        # scalars (never trimmed).
        _res_pacing_knobs = {
            "strategic_session_idle_tick_seconds": int(
                _runtime_config.get("strategic_session_idle_tick_seconds", 5)
            ),
            "strategic_session_turn_timeout_seconds": int(
                _runtime_config.get("strategic_session_turn_timeout_seconds", 120)
            ),
            "strategic_session_message_poll_seconds": float(
                _runtime_config.get("strategic_session_message_poll_seconds", 1.5)
            ),
            "strategic_session_exploration_interval_seconds": int(
                _runtime_config.get(
                    "strategic_session_exploration_interval_seconds", 14_400
                )
            ),
            "strategic_session_max_ops_per_turn": int(
                _runtime_config.get("strategic_session_max_ops_per_turn", 3)
            ),
            "strategic_session_autonomous_turn_timeout_seconds": int(
                _runtime_config.get(
                    "strategic_session_autonomous_turn_timeout_seconds", 300
                )
            ),
        }
        minimal_payload = validate_internal_payload(
            pack_under_budget(
                {
                    "type": "strategic_review",
                    "research_mode": "strategic",
                    "project_name": _project_name,
                    "base_branch": get_project_integration_branch(project),
                    # Cheap in-memory ORM scalars (no I/O).
                    "vision": _agent_visible_vision(project.vision),
                    "problem_model": _agent_view_problem_model(project.problem_model),
                    "running_update": project.running_update or "",
                    # Pure-computation direction grounding (no I/O).
                    "directions_summary": _res_direction_stats,
                    "direction_counts": _res_direction_counts,
                    "max_active_directions": _res_direction_counts["max_active"],
                    **(
                        {"retired_directions_summary": _res_retired_summary}
                        if _res_retired_summary
                        else {}
                    ),
                    **(
                        {"previous_decision_outcomes": _res_prev_outcomes}
                        if _res_prev_outcomes
                        else {}
                    ),
                    # DB-only open-operator-message feed (no network) — the
                    # load-bearing key: surfaces messages older than the runner's
                    # first-poll cursor that would otherwise be missed entirely.
                    "open_message_count": len(_res_open_messages),
                    "open_messages": _res_open_messages,
                    **(
                        {"recent_handled_conversation": _res_recent_handled}
                        if _res_recent_handled
                        else {}
                    ),
                    **({"model": review_model} if review_model else {}),
                    **({"effort": review_effort} if review_effort else {}),
                    # Live, operator-tunable resident-session pacing knobs.
                    **_res_pacing_knobs,
                    # Compact, durable behavioral memory notes written by the
                    # set_memory strategic op. Delivered as the KnowledgeDocument
                    # index (newest 25, body-omitted) so the resident agent gets
                    # the same format as spawned agents.
                    "strategic_memory_notes": await build_strategic_memory_index(session, project.id),
                    **(extra_payload or {}),
                    # Gaps whose history entries warrant compaction — surfaced
                    # directly in the payload so the strategist sees an automatic
                    # signal without manual inspection. Pure in-memory computation.
                    **(
                        {"compaction_hints": _rm_compaction_hints}
                        if (_rm_compaction_hints := _build_compaction_hints(project.problem_model))
                        else {}
                    ),
                },
                _STRATEGIC_REVIEW_SCHEMA,
            ),
            "autonomous_research:strategic_review",
        )
        return await _finalize_strategic_review_spawn(
            session,
            project_id=_project_id,
            project_name=_project_name,
            payload=minimal_payload,
            cycle_id=cycle_id,
            strategy_only=strategy_only,
            base_vision=_base_vision,
            base_problem_model=_base_problem_model,
            base_running_update=_base_running_update,
            local_runner_intent=local_runner_intent,
            preferred_runner_target_id=preferred_runner_target_id,
            extra_context=extra_context,
            operator_steered=operator_steered,
            steering_excerpt=steering_excerpt,
            launched_by_user_id=launched_by_user_id,
            # Born PROVISIONING + pinned to the operator's runner (only the resident
            # spawn path passes this; cadence spawns leave it None → QUEUED as before)
            # so POST /api/runner/next-task claims it directly.
            runner_pin_id=runner_pin_id,
            expected_legacy_generation=expected_legacy_generation,
        )

    # Build direction stats with rich data (pure over ``project.directions``).
    included_directions, direction_stats = _build_direction_stats(project)

    mode_hint_lines = []
    for d in included_directions:
        mode = normalized_recommended_mode(d)
        hint = mode_prompt_hint(mode)
        if not hint and mode != "pause":
            continue
        direction_label = f"{d.get('id')}: {(d.get('text') or '')[:120]}".strip()
        if mode == "pause":
            hint = (
                "Mode hint: this direction is in pause mode. do not propose new feature "
                "work for this direction in this cycle; defer new findings against it."
            )
        mode_hint_lines.append(f"- {direction_label} — {hint}")
    prompt_suffix = (
        "\n".join(["Mode hints for this cycle:", *mode_hint_lines])
        if mode_hint_lines
        else None
    )
    spawn_gate_hint_lines = []
    for stat_entry in direction_stats:
        spawn_gate_metrics = stat_entry.get("spawn_gate_metrics") or {}
        exhaustion_count = int(spawn_gate_metrics.get("exhaustion_count") or 0)
        if exhaustion_count <= 0:
            continue
        direction_label = (
            f"{stat_entry.get('id')}: {(stat_entry.get('text') or '')[:120]}".strip()
        )
        cause_distribution = spawn_gate_metrics.get("cause_distribution") or {}
        if cause_distribution:
            cause_parts = ", ".join(
                f"{cat} x{cnt}" for cat, cnt in sorted(cause_distribution.items())
            )
            cause_note = f"observed causes: {cause_parts}"
        else:
            cause_note = "repeated grader non-response looks infrastructural rather than like a real finding refutation"
        spawn_gate_hint_lines.append(
            f"- {direction_label} — Spawn-gate grader note: wholesale retry exhausted {exhaustion_count} time(s); {cause_note}."
        )
    if spawn_gate_hint_lines:
        spawn_gate_block = "\n".join(
            ["Spawn-gate grader notes for this cycle:", *spawn_gate_hint_lines]
        )
        prompt_suffix = (
            f"{prompt_suffix}\n{spawn_gate_block}"
            if prompt_suffix
            else spawn_gate_block
        )

    # Recently rejected proposals (last 30 days) — surfaced so the strategic
    # agent avoids re-proposing similar work the user already declined.
    cutoff_iso = (_utcnow() - timedelta(days=30)).isoformat()
    rejected_proposals = [
        {
            "text": (d.get("text") or "")[:300],
            "rejected_at": d.get("rejected_at"),
            "rationale": (d.get("rationale") or "")[:300]
            if d.get("rationale")
            else None,
        }
        for d in (project.directions or [])
        if d.get("status") == "rejected" and (d.get("rejected_at") or "") >= cutoff_iso
    ]
    rejected_proposals.sort(key=lambda r: r.get("rejected_at") or "", reverse=True)
    rejected_proposals = rejected_proposals[:10]

    # running_update operator-override signal (last 30 days). When the
    # operator-wins reconciliation preserved an operator mid-cycle edit over the
    # agent's running_update rewrite, ``apply_reconciled_running_update`` records a
    # ``running_update_superseded`` entry in the vision rejected-edits ledger.
    # Surface a small derived signal so next cycle's thinker treats the document
    # as operator-authoritative rather than a blank slate to rewrite. Derived
    # server-side (count + most-recent ``at``) so it survives even if the full
    # vision blob is summarized away under the payload byte budget.
    _superseded_entries = [
        e
        for e in ((project.vision or {}).get(REJECTED_EDITS_KEY) or [])
        if isinstance(e, dict)
        and e.get("kind") == RUNNING_UPDATE_REJECTED_KIND
        and (e.get("at") or "") >= cutoff_iso
    ]
    running_update_superseded = (
        {
            "count": len(_superseded_entries),
            "most_recent_at": max((e.get("at") or "") for e in _superseded_entries),
        }
        if _superseded_entries
        else None
    )

    # Fetch unified conversation thread — recent messages chronologically,
    # regardless of status.  Gives the strategic agent a coherent back-and-forth
    # instead of flat, separated open/resolved lists. Capped at 15 because the
    # full strategic payload sits dangerously close to the 256 KB hard limit
    # (design_doc + problem_model alone are ~160 KB), and conversation_thread
    # is the most aggressively trimmable component without losing critical state.
    thread_result = await session.execute(
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project.id,
        )
        .order_by(ProjectMessage.created_at.desc())
        .limit(15)
    )
    all_thread_msgs = list(reversed(thread_result.scalars().all()))  # chronological
    conversation_thread = [agent_thread_message_dict(m) for m in all_thread_msgs]
    # The strategist's open-message inbox is its OWN authoritative status query
    # — NOT a slice of the conversation_thread above (capped at 15 by recency).
    # Deriving it from that slice meant any OPEN BUG_REPORT/BLOCKER older than
    # the 15 newest messages aged out of the strategist's view and could never
    # be resolved. _fetch_open_messages keeps them visible until fixed/resolved.
    open_msgs = await _fetch_open_messages(session, project.id)
    operator_context_refs = await _fetch_operator_context_refs(session, project.id)

    # When did the user last engage? Last ProjectMessage from_type='user'.
    # The strategic agent uses this to frame its running_update "since last
    # check-in" narrative at the right granularity.
    _last_user_msg = next(
        (m for m in reversed(all_thread_msgs) if m.from_type == "user"),
        None,
    )
    user_last_active_at = (
        _last_user_msg.created_at.isoformat()
        if _last_user_msg and _last_user_msg.created_at
        else None
    )

    review_model = (
        _runtime_config.get("task_type_models", {}).get("strategic_review")
        or _runtime_config.get("task_type_models", {}).get("research")
        or None
    )
    review_effort = (
        _runtime_config.get("task_type_efforts", {}).get("strategic_review")
        or _runtime_config.get("task_type_efforts", {}).get("research")
        or None
    )

    from app.strategic_analysis import (
        _build_outcome_evaluation,
        _build_retired_directions_summary,
        _compute_prediction_calibration,
        _build_retired_approaches_block,
    )

    # Audit W2: cycle history is in the StrategicCycle table; pre-fetch once
    # and pass into all the sync analysis helpers so they don't need the session.
    cycle_history = await strategic_cycle.recent_for_agent_context(
        session,
        project.id,
        limit=20,
    )
    last_strategic_at = await _find_last_strategic_cycle_time(project, session)
    _cal = _compute_prediction_calibration(project, cycle_history)
    calibration_factor = _cal["factor"]
    calibration_note = _cal["note"]
    recent_interventions = await _build_recent_interventions(
        project, session, last_strategic_at=last_strategic_at
    )
    verification_kwargs = await resolve_branch_verification_context(session, project)
    # Mode-aware additions: per-system maturity snapshots + short-window
    # claim-vs-shipped drift. Both are best-effort: empty/None when the
    # project has no SystemMaturity rows or no in-window task data.
    system_maturity_payload = await _build_system_maturity_payload(project, session)
    try:
        from app.drift_detection import compute_direction_drift, persist_drift_snapshot

        # Best-effort drift signals must not poison the shared session. A failed
        # snapshot flush leaves the session pending-rollback; clearing it with a
        # bare ``session.rollback()`` (the prior approach) expires EVERY ORM
        # instance in the session — ``project`` and the in-flight ``task``
        # included — even though the factory sets ``expire_on_commit=False``
        # (that flag governs commit, not rollback). The next *synchronous*
        # attribute read then triggers a lazy-load and raises ``MissingGreenlet``,
        # failing the whole strategic completion handler (design_doc_update tasks
        # a9bf1317 / 94594516, 2026-06-11). A SAVEPOINT contains the failure:
        # rolling back to it discards only the drift writes and leaves
        # ``project``/``task`` loaded for the caller.
        async with session.begin_nested():
            drift_raw = await compute_direction_drift(session, project)
            await persist_drift_snapshot(session, project, drift_raw)
            drift_summary = _summarize_drift_signals(drift_raw)
            if drift_summary.get("by_direction"):
                drift_summary = await _enrich_drift_with_trend(
                    session, project.id, drift_summary
                )
    except Exception:
        logger.exception("[STRATEGIC] drift signal computation failed; continuing")
        drift_summary = {}
    # LTS version/phase context — active whenever the integration branch and
    # promotion target differ (i.e. the LTS split is on). Project-generic:
    # gated on the per-project branch pair, not project.is_self_repo. Best-effort.
    _open_version = None
    _version_phase_block: dict | None = None
    _currency: dict | None = None
    _integration_branch = get_project_integration_branch(project)
    _promotion_target = get_project_promotion_target(project)
    if _integration_branch != _promotion_target:
        # Deploy-model framing — present ONLY when the integration branch and the
        # promotion target differ (the LTS split). In that topology the strategic
        # model grounds on the integration branch, but only the promotion target is
        # deployed and runs live, so code merged to the integration branch is NOT
        # running until it is promoted. Without this the agent conflates "landed on
        # the integration branch" with "live" and adopts plans that are structurally
        # invalid for un-promoted code — enable-a-flag-and-measure-the-live-funnel,
        # or hard-pivot-away-on-a-live-metric-miss. Incident 2026-06-02: the entire
        # warm-pool was on automain only (0 hits on origin/main) yet g-vm-efficiency
        # planned to enable account-retarget + MEASURE the LIVE pool funnel and HARD
        # PIVOT on a miss — impossible until the version promotes. fr-staging-rollup-
        # lag already teaches "a shipped badge is not an integration-branch landing";
        # this is its sibling: "an integration-branch landing is not a deployment."
        # When the two branches are equal (no split) merges deploy directly, so the
        # framing is correctly omitted. Branch names are project-generic (never
        # hardcoded) — resolved from the per-project integration/promotion columns.
        _deploy_framing = (
            "Deployment model — read before reasoning about runtime behavior or "
            f"metrics. `{_integration_branch}` is the integration branch you ground "
            f"on; only `{_promotion_target}` is deployed and runs live. Work merged "
            f"to `{_integration_branch}` but not yet PROMOTED to "
            f"`{_promotion_target}` is NOT running yet. Every live/runtime signal — "
            "pool and VM-reuse funnels, hit rates, throughput, cost-per-merged-PR, "
            "any 'is this capability moving its metric' read — reflects ONLY "
            f"promoted+deployed `{_promotion_target}` code, never "
            f"`{_integration_branch}`-only work. Consequences for a capability "
            f"landed on `{_integration_branch}` but not promoted: (1) do NOT plan to "
            "'enable a runtime flag and measure the live funnel' — there is nothing "
            "live to measure; (2) do NOT pivot away from an approach because live "
            "metrics look flat — flat is the expected reading for un-promoted code, "
            f"not evidence the approach failed; (3) 'landed on `{_integration_branch}`' "
            "means the code EXISTS to reason about and modify, NOT that it is in "
            "effect. To make integration-branch runtime work actually testable and "
            "measurable, promote it first."
        )
        prompt_suffix = (
            f"{_deploy_framing}\n{prompt_suffix}" if prompt_suffix else _deploy_framing
        )
        try:
            from app.release_version import (
                count_merged_since,
                current_open_version,
                ensure_initial_release_version,
                recent_closed_versions,
                to_phase_block,
            )

            _open_version = await current_open_version(
                session,
                project.id,
                include_artifacts=True,
            )
            if _open_version is None:
                # No open version: bootstrap v1.  Resolve the integration-branch
                # HEAD SHA so the qualifier's diff is meaningful; fall back to
                # None on any error.
                _bootstrap_sha: str | None = None
                try:
                    from app import github as _github
                    from app.release_qualifier import _resolve_github_with_session

                    _resolved = await _resolve_github_with_session(session, project)
                    if _resolved is not None:
                        _gh_token, _gh_owner, _gh_repo = _resolved
                        _bootstrap_sha = await _github.get_branch_sha(
                            _gh_token, _gh_owner, _gh_repo, _integration_branch, live=True
                        )
                except Exception:
                    logger.exception(
                        "[STRATEGIC] could not resolve integration branch SHA for bootstrap"
                    )
                # goal opens as None — the strategic agent will author the
                # objective in its first cycle (via new_version_goal output).
                _open_version = await ensure_initial_release_version(
                    session,
                    project.id,
                    integration_branch=_integration_branch,
                    base_sha=_bootstrap_sha,
                    goal=None,
                    promotion_target=_promotion_target,
                )
            if _open_version is not None:
                _open_aggregate, _open_artifacts = _version_serving_summary(
                    _open_version, project
                )
                _tasks_merged = await count_merged_since(
                    session, project.id, _open_version.opened_at
                )
                # Resolve the open version's effective qualification_state through
                # the shared async seam so this payload applies ALL THREE
                # reconciliation enrichments — the serving aggregate, the
                # version-scoped audit child (so a crashed audit reconciles to
                # ``audit_crashed`` instead of telling the strategist to wait on a
                # dead audit and burn a cycle), AND the newest finalized verdict
                # (so a stale ``pending`` + ``promotion_pending`` record does not
                # read ``promotion_check_wait`` when the promotion already
                # resolved). Previously to_phase_block threaded only the audit
                # child and never applied the finalized-verdict substitution, so
                # this surface disagreed with the agent fetch verb in the
                # stale-pending window. The helper keeps audit resolution
                # best-effort internally so a failed resolve never blocks assembly.
                from app.qualification_state_resolver import (
                    resolve_open_version_qualification_read_model,
                )

                _qualification_read = await resolve_open_version_qualification_read_model(
                    session,
                    _open_version,
                    project,
                    serving=(_open_aggregate, _open_artifacts),
                )
                _open_qual_state = _qualification_read.qualification_state
                _open_qual_disagreement = _qualification_read.disagreement
                _phase_block = to_phase_block(
                    _open_version,
                    aggregate_serving_status=_open_aggregate["serving_status"],
                    aggregate_deploy_failure_reason=_open_aggregate["reason_code"],
                    artifact_evidence=_open_artifacts,
                )
                # Override the block's own (audit/verdict-unaware)
                # qualification_state with the fully-reconciled state, and surface
                # any verdict disagreement explicitly so the reconciliation is
                # visible rather than requiring manual cross-referencing. Every
                # other to_phase_block field (last_qualification, serving_status,
                # promotion_readiness, etc.) is preserved untouched.
                _phase_block["qualification_state"] = _open_qual_state
                if _open_qual_disagreement is not None:
                    _phase_block["qualification_state_disagreement"] = (
                        _open_qual_disagreement
                    )
                _version_phase_block = {
                    **_phase_block,
                    "tasks_merged_this_version": _tasks_merged,
                    "qualification_attempts": _open_version.qualification_attempts,
                    "qualification_retry_episode": (
                        _open_version.qualification_retry_episode
                    ),
                    "active_qualification_attempt_id": (
                        str(_open_version.active_qualification_attempt_id)
                        if _open_version.active_qualification_attempt_id
                        else None
                    ),
                    "qualification_provenance": _qualification_read.provenance,
                    "dimension_states": _qualification_read.dimension_states,
                    # Signals the strategist to author the objective this cycle.
                    # Absent once the goal is set (null goal → action needed).
                    **(
                        {"goal_needed": True}
                        if _open_version.goal is None
                        else {}
                    ),
                }
            # Enrich with cross-version promotion-readiness trend.  Best-effort:
            # absent when no QualificationVerdict rows exist yet.
            try:
                from app.release_qualifier import compute_promotion_readiness

                _readiness = await compute_promotion_readiness(session, project.id)
                if _readiness.get("verdicts_evaluated", 0) > 0:
                    if _version_phase_block is None:
                        _version_phase_block = {}
                    _version_phase_block["promotion_readiness"] = _readiness
            except Exception:
                logger.exception(
                    "[STRATEGIC] promotion_readiness fetch failed; continuing"
                )
            try:
                _closed = await recent_closed_versions(
                    session,
                    project.id,
                    limit=_VERSION_PHASE_RECENT_VERSIONS_CAP,
                    include_artifacts=True,
                )
                if _closed:
                    if _version_phase_block is None:
                        _version_phase_block = {}
                    _version_phase_block["recent_versions"] = [
                        await _strategic_recent_version_entry(
                            session, version, project
                        )
                        for version in _closed
                    ]
            except Exception:
                logger.exception(
                    "[STRATEGIC] recent_closed_versions fetch failed; continuing"
                )
            try:
                from app.release_readiness import compute_goal_coverage_drift_signal

                _drift = await compute_goal_coverage_drift_signal(session, project.id)
                if _drift is not None:
                    if _version_phase_block is None:
                        _version_phase_block = {}
                    _version_phase_block["goal_coverage_drift"] = _drift
            except Exception:
                logger.exception(
                    "[STRATEGIC] goal_coverage_drift fetch failed; continuing"
                )
            try:
                if _open_version is not None:
                    from app.acceptance_criteria import get_version_coverage_summary

                    _coverage = await get_version_coverage_summary(
                        session, _open_version.id
                    )
                    if _version_phase_block is None:
                        _version_phase_block = {}
                    _version_phase_block["criterion_coverage"] = _coverage
            except Exception:
                logger.exception(
                    "[STRATEGIC] criterion_coverage fetch failed; continuing"
                )
            try:
                from app.release_readiness import (
                    compute_qualification_trigger_state,
                )

                _trigger_state = await compute_qualification_trigger_state(
                    session, project.id
                )
                if _trigger_state is not None:
                    if _version_phase_block is None:
                        _version_phase_block = {}
                    _version_phase_block["qualification_trigger_state"] = (
                        _trigger_state
                    )
            except Exception:
                logger.exception(
                    "[STRATEGIC] qualification_trigger_state fetch failed; continuing"
                )
        except Exception as _vpb_exc:
            logger.exception("[STRATEGIC] version_phase block build failed; continuing")
            _version_phase_block = {
                "lifecycle_signal": "unavailable",
                "signal_status": "build_failed",
                "build_unavailable": True,
                "error": repr(_vpb_exc)[:240],
                "integration_branch": _integration_branch,
                "promotion_target": _promotion_target,
            }
    # Deployment-state block — only under an LTS split (integration branch !=
    # promotion target). Grounds the deploy-model framing in concrete numbers:
    # what is actually DEPLOYED (running live on the promotion target) vs what
    # has merged to the integration branch but is NOT yet promoted, so the agent
    # never reads a flat live/runtime metric for un-promoted code as evidence an
    # approach failed (incident 2026-06-02: the warm-pool was on automain only,
    # 0 on main, yet g-vm-efficiency planned to measure its LIVE funnel). The
    # deployed-vs-promoted SHAs are surfaced separately so a promote→rollout lag
    # (or a ROLLED_BACK/HALTED status) is visible rather than conflated.
    # Best-effort — never block the payload build.
    _deployment_state_block: dict | None = None
    if _integration_branch != _promotion_target:
        try:
            from app.models import DeploymentState, DeploymentStatus
            from app.release_version import (
                recent_closed_versions as _recent_closed_for_deploy,
            )

            _tasks_merged_unpromoted = (_version_phase_block or {}).get(
                "tasks_merged_this_version"
            ) or 0
            # Latest promoted version (rows are newest-first; the first promoted
            # one is the most recent promotion = highest version_number).
            _promoted_v = None
            try:
                for _cv in await _recent_closed_for_deploy(
                    session,
                    project.id,
                    limit=_VERSION_PHASE_RECENT_VERSIONS_CAP,
                    include_artifacts=True,
                ):
                    if _cv.status == "promoted":
                        _promoted_v = _cv
                        break
            except Exception:
                logger.exception(
                    "[STRATEGIC] promoted-version lookup failed; continuing"
                )
            # Deployed SHA: the orchestrator's own DeploymentState tracks the live
            # container of the repo THIS deployment was built from. It is
            # meaningful only for the project that IS the deploy identity
            # (deploy-level observability) — for any other project it is
            # meaningless, so fall back to the latest promoted SHA (their deploy
            # follows promotion, not this orchestrator's container).
            _deployed_sha: str | None = None
            _deployed_revision: str | None = None
            _deploy_status: str | None = None
            # Rollback provenance — only meaningful for the deploy's own
            # DeploymentState. Surfaced so the strategist sees WHY the pipeline
            # rolled back (which SHA, how many consecutive rollbacks, the
            # reason) and can prioritize stabilization rather than spawn more
            # risky feature work right after a rollback / halt.
            _rolled_back_from_sha: str | None = None
            _rollback_count: int = 0
            _rollback_reason: str | None = None
            # De-gated from project.is_self_repo to the deploy-level identity:
            # the strategist gets positive serving evidence wherever the live
            # DeploymentState actually corresponds to the project's repo.
            if project_matches_deploy_identity(project):
                try:
                    _ds = await session.get(DeploymentState, 1)
                    if _ds is not None:
                        _deployed_sha = _ds.current_sha or None
                        _deployed_revision = _ds.current_revision or None
                        _deploy_status = _ds.status
                        _rolled_back_from_sha = _ds.rolled_back_from_sha or None
                        _rollback_count = _ds.rollback_count or 0
                        _rollback_reason = _ds.rollback_reason or None
                except Exception:
                    logger.exception(
                        "[STRATEGIC] DeploymentState read failed; continuing"
                    )
            _promoted_aggregate: dict[str, Any] | None = None
            _promoted_artifacts: list[dict[str, Any]] = []
            _promoted_gap: str | None = None
            if _promoted_v is not None:
                _promoted_aggregate, _promoted_artifacts = _version_serving_summary(
                    _promoted_v, project
                )
                _promoted_gap = summarize_required_artifact_gap(_promoted_artifacts)
            # Real serving SHA only — NEVER a promoted_sha stand-in. Fall back to
            # the promoted version's served_sha only once aggregate required-artifact
            # truth says that promotion actually shipped.
            if (
                not _deployed_sha
                and _promoted_v is not None
                and _promoted_aggregate is not None
                and _promoted_aggregate["serving_status"] == "serving_confirmed"
            ):
                _deployed_sha = _promoted_v.served_sha
            _undeployed_dirs = (recent_interventions or {}).get(
                "undeployed_directions"
            ) or []
            # automain_ahead_of_main: reliable from DB alone (open, un-promoted
            # version carrying merged work), refined by the GitHub-verified
            # per-direction list when available.
            _open_unpromoted = (
                _open_version is not None and _open_version.status == "open"
            )
            _automain_ahead = bool(_undeployed_dirs) or (
                _open_unpromoted and _tasks_merged_unpromoted > 0
            )
            _deployment_state_block = {
                "integration_branch": _integration_branch,
                "promotion_target": _promotion_target,
                "deployed_sha": (_deployed_sha or "")[:12] or None,
                "deployed_revision": _deployed_revision,
                "deploy_status": _deploy_status,
                # Rollback provenance (self-repo only; None/0 otherwise) — the
                # same truth operators already see in the deploy panel.
                "rolled_back_from_sha": (_rolled_back_from_sha or "")[:12] or None,
                "rollback_count": _rollback_count,
                "rollback_reason": _rollback_reason,
                "promoted_version": (
                    _promoted_v.version_number if _promoted_v else None
                ),
                "promoted_sha": (
                    (_promoted_v.promoted_sha or "")[:12] or None
                    if _promoted_v
                    else None
                ),
                # Post-promotion serving outcome of the latest promotion: lets
                # the strategist see whether "promoted" actually shipped vs is
                # still unconfirmed or failed to deploy.
                "promoted_serving_status": (
                    _promoted_aggregate["serving_status"]
                    if _promoted_aggregate
                    else None
                ),
                "promoted_deploy_failure_reason": (
                    _promoted_aggregate["reason_code"] if _promoted_aggregate else None
                ),
                "promoted_artifacts": _promoted_artifacts,
                "promoted_artifact_gap": _promoted_gap,
                "open_version": (
                    _open_version.version_number if _open_version else None
                ),
                "open_phase": _open_version.phase if _open_version else None,
                "open_status": _open_version.status if _open_version else None,
                "tasks_merged_unpromoted": _tasks_merged_unpromoted,
                "automain_ahead_of_main": _automain_ahead,
                **(
                    {"undeployed_directions": _undeployed_dirs}
                    if _undeployed_dirs
                    else {}
                ),
            }
            # Grounded deploy line — only when there is genuinely un-promoted
            # work. Self-contained (does not depend on the general deploy-model
            # framing) so it lands value independently: it points the agent at
            # the deployment_state block and names the un-promoted directions, so
            # a flat live metric for them is not read as failure.
            if _automain_ahead:
                _open_n = _open_version.version_number if _open_version else "?"
                _deploy_line = (
                    f"Deployment status (see `deployment_state`): v{_open_n} is open "
                    f"and NOT promoted to `{_promotion_target}`; "
                    f"{_tasks_merged_unpromoted} merged change(s) sit on "
                    f"`{_integration_branch}` only and are not running live yet."
                )
                if _undeployed_dirs:
                    _ids = ", ".join(
                        str(d.get("direction_id") or "?") for d in _undeployed_dirs[:6]
                    )
                    _deploy_line += (
                        f" Directions with undeployed runtime work: {_ids}. Their "
                        f"live/runtime metrics cannot move until v{_open_n} promotes "
                        f"— do not read a flat metric for them as evidence the "
                        f"approach failed."
                    )
                prompt_suffix = (
                    f"{prompt_suffix}\n{_deploy_line}"
                    if prompt_suffix
                    else _deploy_line
                )
            # Stabilization directive — symmetric to the deploy_failed /
            # unconfirmed recovery directives, but driven by the orchestrator's
            # OWN deploy status (self-repo only). When the pipeline has
            # ROLLED_BACK (reverted to last-known-good) or HALTED (repeated
            # rollbacks → manual-intervention needed), stabilization is the
            # priority for the cycle: scrutinize the rolled-back-from change
            # rather than spawn more risky feature work, and (for HALTED)
            # recognize the repeated-rollback halt. Best-effort and naturally
            # scoped to self-repo since _deploy_status is only read there.
            if _deploy_status in (
                DeploymentStatus.ROLLED_BACK,
                DeploymentStatus.HALTED,
            ):
                _from_txt = (
                    f" The rolled-back-from SHA is "
                    f"{(_rolled_back_from_sha or '')[:12]}."
                    if _rolled_back_from_sha
                    else ""
                )
                _reason_txt = (
                    f" Rollback reason: {_rollback_reason}." if _rollback_reason else ""
                )
                _count_txt = (
                    f" Consecutive rollbacks: {_rollback_count}."
                    if _rollback_count
                    else ""
                )
                if _deploy_status == DeploymentStatus.HALTED:
                    _stabilize_directive = (
                        f"Deployment HALTED (see `deployment_state.deploy_status` "
                        f"= 'HALTED') — the pipeline has rolled back repeatedly and "
                        f"now needs manual intervention.{_count_txt}{_from_txt}"
                        f"{_reason_txt} Treat STABILIZATION as the TOP priority for "
                        f"this cycle: scrutinize the rolled-back-from change and the "
                        f"repeated-rollback cause, do NOT advance focus or spawn more "
                        f"risky feature work as if the milestone shipped. Recognize "
                        f"this is a repeated-rollback halt, not a one-off."
                    )
                else:
                    _stabilize_directive = (
                        f"Deployment ROLLED BACK (see "
                        f"`deployment_state.deploy_status` = 'ROLLED_BACK') — the "
                        f"orchestrator reverted its own live deploy to the last "
                        f"known good.{_count_txt}{_from_txt}{_reason_txt} Treat "
                        f"STABILIZATION as the priority for this cycle: scrutinize "
                        f"the rolled-back-from change rather than spawn more risky "
                        f"feature work, and do NOT advance focus as if the milestone "
                        f"shipped."
                    )
                prompt_suffix = (
                    f"{prompt_suffix}\n{_stabilize_directive}"
                    if prompt_suffix
                    else _stabilize_directive
                )
        except Exception:
            logger.exception(
                "[STRATEGIC] deployment_state block build failed; continuing"
            )
    # Citable memo digest: terminal reasoning artifacts spawned from dropped
    # research findings since the previous cycle. Surfaced so the reviewer can
    # cite memos by task_id. Low priority in the pack schema — dropped first
    # under budget pressure.
    try:
        memo_citations = await strategic_cycle.query_recent_memo_citations(
            session, project.id
        )
    except Exception:
        logger.exception("[STRATEGIC] memo citation fetch failed; continuing")
        memo_citations = []
    # Efficiency axes + drift vs baseline. Best-effort: absent when no snapshot
    # data has been written yet (cold-start projects). The summarize_fn trims
    # history tails before the packer drops it entirely under budget pressure.
    _efficiency_snapshot: dict | None = None
    try:
        from app.efficiency_snapshot import read_baseline_with_drift

        _raw_snapshot = await read_baseline_with_drift(session, project.id)
        axes = (_raw_snapshot or {}).get("axes") or {}
        if any(
            v.get("current") is not None for v in axes.values() if isinstance(v, dict)
        ):
            _efficiency_snapshot = _summarize_efficiency_snapshot(_raw_snapshot)
    except Exception:
        logger.exception("[STRATEGIC] efficiency snapshot fetch failed; continuing")

    # Fleet slot utilization — a GLOBAL, real-time signal injected as an extra
    # axis into the efficiency table the strategist already renders. Unlike the
    # per-project daily axes above, it is computed fresh each cycle from the
    # shared max-plan account pool, so it ships even before any per-project
    # efficiency baseline exists. The active count is DB-derived (not the in-
    # memory active_tasks counter, which can leak and read high), so genuine
    # idle headroom is never masked.
    try:
        from app.fleet_utilization import get_fleet_utilization_snapshot

        _fleet_line = _format_fleet_utilization_axis(
            await get_fleet_utilization_snapshot(session)
        )
        if _fleet_line is not None:
            if _efficiency_snapshot is None:
                _efficiency_snapshot = {"axes": {}}
            _efficiency_snapshot.setdefault("axes", {})["fleet_utilization"] = {
                "current": _fleet_line,
                "baseline": None,
                "drift_pct": None,
            }
    except Exception:
        logger.exception("[STRATEGIC] fleet utilization snapshot failed; continuing")
    # Held-directions block — directions that hit the consecutive-failure hold
    # threshold (3 consecutive CODE_ERROR failures; see
    # shared_state.record_direction_result). Surfaces the held flag, the
    # dominant error fingerprint, and a few RAW recent failure messages so the
    # strategic agent can decide whether to refine or retire a stuck direction.
    # READ-ONLY planning context: no spawner, no enforcement, no hard block.
    # The failed-count / acceptance-rate / recent_diagnosis already reach the
    # agent via direction_outcomes, so this block adds ONLY the held flag +
    # dominant fingerprint + raw failure messages on top of those.
    _held_directions_block: list[dict] = []
    try:
        _held = _shared_state.get_held_directions()
        if _held:
            from app.anomaly_detection import _error_fingerprint

            _project_id_str = str(project.id)
            _dir_text_map = {
                d.get("id"): d for d in (project.directions or []) if d.get("id")
            }
            # Pre-filter held directions to only those that belong to this project
            # and exist in the current direction list. This avoids a wasted query
            # when all held directions are from other projects or have been deleted.
            _applicable_held_dirs = {
                _did: _hold_info
                for _did, _hold_info in _held.items()
                if _hold_info.get("project_id") == _project_id_str
                and _did in _dir_text_map
            }
            if _applicable_held_dirs:
                # One bounded query for the project's recent FAILED tasks, then
                # partition by direction_id in memory (mirrors the gathering logic
                # in _spawn_direction_hold_investigation, copied here intentionally
                # since that spawner is slated for deletion).
                _recent_failed = (
                    (
                        await session.execute(
                            select(Task)
                            .where(
                                Task.project_id == project.id,
                                Task.status == TaskStatus.FAILED,
                            )
                            .order_by(Task.created_at.desc())
                            .limit(60)
                        )
                    )
                    .scalars()
                    .all()
                )
                for _did, _hold_info in _applicable_held_dirs.items():
                    _direction = _dir_text_map.get(_did)
                    _dir_failures = [
                        t
                        for t in _recent_failed
                        if (t.context or {}).get("direction_id") == _did
                    ][:5]
                    _failure_messages: list[str] = []
                    _fingerprints: list[str] = []
                    for t in _dir_failures:
                        _err = (t.latest_status_message or "(no message)")[:240]
                        _fingerprints.append(_error_fingerprint(_err))
                        _failure_messages.append(_err)
                    _dominant_fp = _fingerprints[0] if _fingerprints else None
                    _held_directions_block.append(
                        {
                            "direction_id": _did,
                            "direction_text": (_direction.get("text") or "?")[:300],
                            "consecutive_failures": _hold_info.get("consecutive_failures"),
                            "last_failure_class": _hold_info.get("last_failure_class"),
                            "dominant_error_fingerprint": _dominant_fp,
                            "recent_failure_messages": _failure_messages,
                        }
                    )
    except Exception:
        logger.exception("[STRATEGIC] held-directions block build failed; continuing")
    # Integrity-conflict trend — persisted per-cycle counts for the four
    # referential-integrity classes (orphaned_gaps, ungrounded_directions,
    # stale_active_moves, staffed_but_deferred) over the last 10 completed
    # cycles. Compact dict of three scalars + last_counts; low priority. Best-
    # effort: absent when no stamped cycles exist yet or the query fails.
    _integrity_trend: dict | None = None
    try:
        from app import strategic_cycle as _sc_trend

        _trend_result = await _sc_trend.recent_integrity_conflict_trend(
            session, project.id, limit=10
        )
        if _trend_result.get("stamped_cycles", 0) > 0:
            _integrity_trend = _trend_result
    except Exception:
        logger.exception("[STRATEGIC] integrity_conflict_trend fetch failed; continuing")
    # Post-promotion handoff — when a fresh version has just opened, it was
    # opened by the PROMOTION of the prior version: open_version() is only ever
    # called from _record_promotion_success and the v1 bootstrap, so a version
    # with number > 1 that has never been qualified ⇒ v{N-1} shipped to the
    # promotion target. Two things then mislead the agent into re-doing finished
    # work: (1) the `goal` field is a frozen snapshot of `current_focus` taken at
    # the promotion instant, which reliably still reads as v{N-1}'s own "get me
    # promoted" mission; and (2) v{N}'s null verdict reads as "not promoted yet".
    # The agent then plans to re-promote/re-qualify an already-live version
    # (incident 2026-06-03: a clean re-audit + research + multiple strategic
    # reviews all burned on a version already merged to main). So this hint
    # asserts the promotion POSITIVELY and forbids re-promotion, rather than only
    # flagging the carried text as stale. Fires only on the first cycles after
    # promotion (never qualified); self-clears once the first verdict lands.
    if (
        _open_version is not None
        and _open_version.version_number > 1
        and _open_version.last_qualification is None
        and (_open_version.qualification_attempts or 0) == 0
    ):
        _prior_n = _open_version.version_number - 1
        _cur_n = _open_version.version_number
        _target = _promotion_target or "main"
        _cur_goal = (_open_version.goal or "").strip()
        _goal_state = (
            f"v{_cur_n} already has `version_phase.goal` set: "
            f"{_cur_goal[:220]!r}."
            if _cur_goal
            else (
                f"`version_phase.goal` on v{_cur_n} is null, so this version "
                "still needs its objective."
            )
        )
        _setup_actions: list[str] = []
        if _cur_goal:
            _setup_actions.append(
                f"use v{_cur_n}'s existing `version_phase.goal` as the frozen "
                "objective; leave the stored version goal unchanged this cycle"
            )
        else:
            _setup_actions.append(
                f"rewrite `intent.current_focus` to state what v{_cur_n} is "
                f"for (not what v{_prior_n} achieved)"
            )
            _setup_actions.append(
                f"output `new_version_goal` — a 1-3 sentence concrete, "
                f"outcome-framed objective for v{_cur_n} (this is stored as "
                "the frozen version goal the blind merge-grade will score "
                "merged diffs against; write it as a DELIVERABLE statement, "
                "not a status line)"
            )
        _setup_instruction = "; ".join(
            f"({idx}) {action}" for idx, action in enumerate(_setup_actions, start=1)
        )
        # Ground the assertion in the prior version's actual promoted record
        # (promoted_at + final confidence) when readable — best-effort; the
        # positive "it promoted" framing holds regardless because a version with
        # number > 1 is only reachable via a promotion. Local import keeps this
        # resilient to back-merge refactors of release_version.
        _promoted_when = ""
        _verdict_txt = ""
        _prior_serving_status: str | None = None
        _prior_deploy_reason: str | None = None
        _prior_artifact_gap: str | None = None
        # When the prior-version record genuinely cannot be READ (the lookup
        # raised), fail closed below: do NOT imply the previous version is live.
        # A lookup that simply finds no matching promoted record is a different,
        # optimistic case — version_number > 1 is only reachable via a
        # promotion, so the positive "it promoted" framing still holds there.
        _prior_lookup_failed = False
        try:
            from app.release_version import recent_closed_versions as _rcv

            for _v in await _rcv(
                session,
                project.id,
                limit=_VERSION_PHASE_RECENT_VERSIONS_CAP,
                include_artifacts=True,
            ):
                if _v.version_number == _prior_n and _v.status == "promoted":
                    if _v.promoted_at is not None:
                        _promoted_when = f" at {_v.promoted_at.isoformat()}"
                    _conf = (_v.last_qualification or {}).get("confidence")
                    if _conf is not None:
                        _verdict_txt = f" (final qualification confidence {_conf})"
                    _prior_aggregate, _prior_artifacts = _version_serving_summary(
                        _v, project
                    )
                    _prior_serving_status = _prior_aggregate["serving_status"]
                    _prior_deploy_reason = _prior_aggregate["reason_code"]
                    _prior_artifact_gap = summarize_required_artifact_gap(
                        _prior_artifacts
                    )
                    break
        except Exception:
            _prior_lookup_failed = True
            logger.exception(
                "[STRATEGIC] post-promote prior-version lookup failed; continuing"
            )
        if _prior_lookup_failed:
            # Fail closed: the prior promoted record is unreadable, so we cannot
            # assert it is serving. Do NOT imply it is live.
            _status_txt = (
                f" serving_status={_prior_serving_status!r}"
                if _prior_serving_status is not None
                else ""
            )
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} was promoted "
                f"to {_target}{_promoted_when}{_verdict_txt}, but its prior "
                f"serving evidence is unknown or unreadable{_status_txt}. Do "
                f"NOT treat v{_prior_n} as live yet. Verify lifecycle/deploy "
                f"state before treating the previous version as live, and only "
                f"treat it as shipped once aggregate serving evidence is "
                f"explicitly `serving_confirmed`."
            )
        elif _prior_serving_status == "deploy_failed":
            # The prior promotion MERGED but never shipped — do NOT advance focus
            # as if it succeeded.  This is the focus-advancement gate honoring the
            # serving signal: the strategist must treat the milestone as NOT
            # delivered and prioritize recovering the deploy.
            _reason_txt = (
                f" (reason={_prior_deploy_reason})" if _prior_deploy_reason else ""
            )
            _artifact_txt = (
                f" Evidence: {_prior_artifact_gap}." if _prior_artifact_gap else ""
            )
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} MERGED to "
                f"{_target}{_promoted_when} but its deploy DID NOT confirm "
                f"serving{_reason_txt}: the promotion did NOT ship and the code "
                f"is NOT live (see `deployment_state.promoted_serving_status` = "
                f"'deploy_failed' and `version_phase.recent_versions`). Do NOT "
                f"treat v{_prior_n}'s mission as complete and do NOT advance "
                f"`intent.current_focus` to the next milestone as if it shipped. "
                f"Your TOP priority this cycle is recovering the failed deploy "
                f"(roll forward a fix / investigate why the promoted SHA never "
                f"served); only after it is serving should you move focus on."
                f"{_artifact_txt}"
            )
        elif _prior_serving_status == "confirmation_timed_out":
            # Distinct from deploy_failed: the promotion merged and likely
            # shipped, but automated serving confirmation TIMED OUT with no
            # active failure observed. This is a "verify", not a "recover from a
            # failed deploy" — do NOT trigger the roll-forward crisis, and do NOT
            # block advancing on it the way deploy_failed does.
            _reason_txt = (
                f" (reason={_prior_deploy_reason})" if _prior_deploy_reason else ""
            )
            _artifact_txt = (
                f" Evidence: {_prior_artifact_gap}." if _prior_artifact_gap else ""
            )
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} MERGED to "
                f"{_target}{_promoted_when}{_verdict_txt}, but its automated "
                f"serving confirmation TIMED OUT{_reason_txt} (see "
                f"`deployment_state.promoted_serving_status` = "
                f"'confirmation_timed_out'). This is NOT a failed deploy: NO "
                f"rollback/halt/sha-mismatch was observed — the promoted SHA "
                f"just could not be independently confirmed serving within the "
                f"window (commonly an observability gap, e.g. the live deploy "
                f"SHA could not be read). The code very likely shipped but is "
                f"UNVERIFIED. Do NOT treat this as a deploy crisis or roll back, "
                f"and do NOT re-promote/re-qualify v{_prior_n}. VERIFY serving "
                f"(it auto-recovers to serving_confirmed once the promoted SHA "
                f"is observed live); you may proceed with v{_cur_n}'s mission "
                f"meanwhile.{_artifact_txt}"
            )
        elif _prior_serving_status == "unconfirmed":
            _artifact_txt = (
                f" Evidence: {_prior_artifact_gap}." if _prior_artifact_gap else ""
            )
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} was promoted "
                f"(merged) to {_target}{_promoted_when}{_verdict_txt}, but its "
                f"deploy is NOT yet confirmed serving (see "
                f"`deployment_state.promoted_serving_status` = 'unconfirmed') — "
                f"it is still inside the post-promotion confirmation window. Do "
                f"NOT yet treat the milestone as fully shipped; the sweep will "
                f"resolve it to serving_confirmed, confirmation_timed_out, or "
                f"deploy_failed shortly. You may prepare v{_cur_n}'s mission. "
                f"{_goal_state} Next-version setup: "
                f"{_setup_instruction}. Do not "
                f"declare v{_prior_n} live until serving is confirmed, and do "
                f"NOT re-promote/re-qualify v{_prior_n}."
                f"{_artifact_txt}"
            )
        else:
            _post_promote_hint = (
                f"Version handoff — READ THIS FIRST. v{_prior_n} was SUCCESSFULLY "
                f"PROMOTED to {_target}{_promoted_when}{_verdict_txt} and is now "
                f"live; v{_cur_n} opened from that promotion. v{_prior_n}'s "
                f"mission is COMPLETE — do NOT plan, research, audit, or spawn "
                f"work to promote, qualify, re-audit, or re-qualify v{_prior_n}; "
                f"that is already done and merged to {_target} (cross-check "
                f"`version_phase.recent_versions` and `deployment_state` — "
                f"v{_prior_n} is recorded there with status='promoted'). "
                f"{_goal_state} A null "
                f"`version_phase.last_qualification` on v{_cur_n} means this "
                f"fresh version has not yet begun its OWN qualification (expected "
                f"for a just-opened version) — it does NOT mean v{_prior_n}'s "
                f"promotion is pending or failed. Perform two independent "
                f"control-plane actions this cycle: (a) complete next-version "
                f"setup: {_setup_instruction}; (b) retire directions v{_prior_n} "
                f"already satisfied. These actions may proceed in parallel; "
                f"they are not separate strategic directions. Once setup is "
                f"complete, propose the directions "
                f"the next milestone needs."
            )
        prompt_suffix = (
            f"{prompt_suffix}\n{_post_promote_hint}"
            if prompt_suffix
            else _post_promote_hint
        )
    # Failed-deploy recovery directive — even if the next version has started
    # qualification, keep the recovery priority visible while the latest promoted
    # version remains in deploy_failed state. Computed at view time from that
    # version's serving_status, so it self-clears as soon as the status flips to
    # serving_confirmed — whether by a subsequent version promoting and confirming
    # serving OR by the deploy-failure recovery sweep observing the SAME promoted
    # SHA finally serve (a slow / boot-crashed deploy that later comes up healthy;
    # see release_reconciliation._reconcile_if_deploy_failed_recovered). Fires
    # independently of the post-promotion handoff (which self-clears once the fresh
    # version starts qualifying) so failed deploys continue to drive strategic
    # attention until actually recovered.
    if _open_version is not None and _integration_branch != _promotion_target:
        _latest_promoted_version: Any | None = None
        try:
            from app.release_version import recent_closed_versions as _rcv

            for _v in await _rcv(session, project.id, limit=1, include_artifacts=True):
                if _v.status == "promoted":
                    _latest_promoted_version = _v
                    break
        except Exception:
            logger.exception(
                "[STRATEGIC] failed-deploy directive: latest promoted lookup failed; continuing"
            )
        if _latest_promoted_version is not None:
            _latest_promoted_aggregate, _latest_promoted_artifacts = (
                _version_serving_summary(_latest_promoted_version, project)
            )
            _latest_artifact_gap = summarize_required_artifact_gap(
                _latest_promoted_artifacts
            )
        else:
            _latest_promoted_aggregate = None
            _latest_artifact_gap = None
        if (
            _latest_promoted_version is not None
            and _latest_promoted_aggregate is not None
            and _latest_promoted_aggregate["serving_status"] == "deploy_failed"
        ):
            _reason_txt = (
                f" (reason={_latest_promoted_aggregate['reason_code']})"
                if _latest_promoted_aggregate["reason_code"]
                else ""
            )
            _artifact_txt = (
                f" Evidence: {_latest_artifact_gap}." if _latest_artifact_gap else ""
            )
            _failed_deploy_directive = (
                f"Active deployment crisis — v{_latest_promoted_version.version_number} "
                f"MERGED to {_promotion_target} but DID NOT confirm serving{_reason_txt} "
                f"(see `deployment_state.promoted_serving_status` = 'deploy_failed'). "
                f"The promotion did NOT ship and code is NOT live. Your TOP priority "
                f"this cycle is RECOVERING the failed deploy (roll forward a fix / "
                f"investigate why the promoted SHA never served). Only after it is "
                f"serving should you move focus on. This directive clears the moment "
                f"THIS deploy is confirmed serving — recovering the SAME promoted SHA "
                f"(a slow or boot-crashed deploy that later comes up healthy) resolves "
                f"it automatically; you do NOT need to cut and promote a subsequent "
                f"version for it to clear.{_artifact_txt}"
            )
            prompt_suffix = (
                f"{prompt_suffix}\n{_failed_deploy_directive}"
                if prompt_suffix
                else _failed_deploy_directive
            )
    # Qualification sub-state directive — last_qualification.status is overloaded
    # ("pending" = audit-scoring OR managed promotion pending), which
    # made the agent conflate "wait for the audit" with "the promotion is
    # failing" and burn whole cycles waiting (incident 2026-06-02). Surface the
    # disambiguated sub-state as an explicit directive for the two states the
    # agent misreads as "still auditing, wait". Local import keeps this resilient
    # to integration-branch refactors of release_version on back-merge.
    if _open_version is not None:
        from app.release_version import (
            QUALIFICATION_STATE_VOCAB as _QUAL_STATE_VOCAB,
        )

        _QUAL_HINT_STATES = {
            "promotion_check_wait",
            "promotion_failed",
            "required_check_failed",
            "protection_blocked_review",
        }
        assert (
            _QUAL_HINT_STATES <= _QUAL_STATE_VOCAB
        ), "prompt-suffix states not in vocab"
        # Use the resolver-applied qualification_state from _version_phase_block
        # rather than re-deriving from the raw last_qualification record, so the
        # hint reflects the finalized-verdict substitution (if any).
        _qual_state = (_version_phase_block or {}).get("qualification_state")
        if _qual_state and _qual_state.get("state") in _QUAL_HINT_STATES:
            _qual_hint = (
                f"Qualification sub-state: {_qual_state['state'].upper()} — "
                f"{_qual_state['summary']} Do NOT read "
                "version_phase.last_qualification.status='pending' as 'the audit "
                "is still running'; version_phase.qualification_state is the "
                "disambiguated source of truth."
            )
            prompt_suffix = (
                f"{prompt_suffix}\n{_qual_hint}" if prompt_suffix else _qual_hint
            )
    # Stabilize phase directive — when the open version is in stabilize, bias the
    # strategic agent's work-mix toward audit/cleanup/hardening so the version
    # converges toward qualification rather than accumulating net-new feature work.
    if _open_version is not None and _open_version.phase == "stabilize":
        _stabilize_hint = (
            "Version phase: STABILIZE. Bias the work-mix toward audit, cleanup, and "
            "hardening this cycle. Prefer recommended_mode='audit' or 'fundamentals' "
            "for active directions. Defer new feature directions — do not propose "
            "net-new feature work while the version is stabilizing."
        )
        # When the last qualification carried audit findings, point the agent at
        # the specific findings so it creates a TARGETED direction
        # (recommended_mode='audit') rather than doing blind generic cleanup that
        # may never touch the actual blockers.  Audit is NOT automatically the
        # binding constraint just because it flagged findings — frame it as such
        # only when audit is genuinely the limiting dimension; otherwise name the
        # real constraint (a hard blocker, or a lower sub-score) so hardening is
        # scoped at what actually blocks promotion.
        _last_qual = _open_version.last_qualification or {}
        _carried_findings = _last_qual.get("audit_findings")
        if isinstance(_carried_findings, list) and _carried_findings:
            _top = "; ".join(
                f"[{f.get('severity', '?')}] {f.get('summary', '')}"
                for f in _carried_findings[:5]
                if isinstance(f, dict)
            )
            # Determine the dimension that actually limits promotion.  A hard
            # blocker forces confidence to zero regardless of sub-scores; with no
            # blocker the binding dimension is the lowest current sub-score.
            # Retired smoke scores are archival and never binding.
            _blocker = _last_qual.get("blocker")
            if _blocker == "smoketest_startup_failed":
                _blocker = None
            _sub_scores = _last_qual.get("sub_scores")
            _binding_desc = "the audit sub-score is the binding constraint on promotion"
            if _blocker:
                _binding_desc = (
                    f"a hard blocker ({_blocker}) is the binding constraint on "
                    f"promotion — it forces confidence to zero regardless of the "
                    f"sub-scores, so resolving it is the priority"
                )
            elif isinstance(_sub_scores, dict):
                _active = {
                    _k: _v
                    for _k, _v in (
                        ("audit", _sub_scores.get("audit")),
                        ("test_erosion", _sub_scores.get("test_erosion")),
                    )
                    if isinstance(_v, (int, float)) and not isinstance(_v, bool)
                }
                if _active:
                    _binding_key = min(_active, key=lambda _k: _active[_k])
                    if _binding_key == "audit":
                        _binding_desc = (
                            "the audit sub-score is the binding constraint on promotion"
                        )
                    else:
                        _binding_desc = (
                            f"the {_binding_key} sub-score "
                            f"({_active[_binding_key]:.2f}) is the binding "
                            f"constraint on promotion — it is lower than audit "
                            f"({_active.get('audit', 0.0):.2f}), so audit is not "
                            f"the limiting dimension even though it flagged "
                            f"findings; address {_binding_key} alongside the "
                            f"audit findings"
                        )
            # Establish whether the carried findings are still current before
            # presenting them as live promotion blockers.  The findings were
            # scored against a pinned candidate commit; if the integration
            # branch has advanced past it, later commits may already have fixed
            # them, so re-targeting research/plan/fix work directly at them
            # churns at 0% conversion.  Best-effort — None means indeterminate.
            _currency = await _resolve_candidate_currency(
                session, project, _last_qual, _integration_branch
            )
            _currency_resolved = _currency.get("resolved") is True
            _candidate_behind = _currency_resolved and not _currency["current"]
            if _version_phase_block is not None and _currency is not None:
                _version_phase_block["last_qualification_candidate_currency"] = (
                    _currency
                )
            # Trigger-based re-qualification steering.  The candidate-behind heuristic
            # alone is not enough to recommend a blanket re-qualification: the
            # pre-registered trigger is "every gating high+medium carried finding
            # resolved-or-stale".  Consult the per-finding resolution ledger so the
            # steer matches what was actually resolved — re-qualify ONLY when the
            # signal is met; otherwise drive the outstanding fix-first work.  This is
            # only relevant on the behind-head path; on the live/indeterminate path
            # the findings may genuinely still block, handled by the else branch.
            # Best-effort: a failure to compute the ledger signal degrades to the
            # prior blanket re-qualify steer so the strategic spawn never breaks.
            _readiness: dict | None = None
            _requal_ready = True
            if _candidate_behind:
                try:
                    from app.release_readiness import (
                        compute_requalification_readiness,
                    )

                    _readiness = await compute_requalification_readiness(
                        session, project.id
                    )
                    _requal_ready = bool(_readiness.get("requalification_ready"))
                except Exception:
                    logger.exception(
                        "[STRATEGIC] requalification readiness compute failed; "
                        "defaulting to blanket re-qualify steer"
                    )
                    _readiness = None
                    _requal_ready = True
            # Re-qualification is the highest-leverage steer ONLY on the behind-head
            # path with the readiness trigger met; everywhere else there is fix work
            # to dispatch (so the create-a-direction escalation below should fire).
            _prefer_requalify = _candidate_behind and _requal_ready
            if _candidate_behind:
                _cand_short = _currency["candidate_sha"][:8]
                _head_short = _currency["head_sha"][:8]
                _stale_preamble = (
                    f" The last qualification flagged {len(_carried_findings)} "
                    f"audit finding(s), but it scored them against candidate "
                    f"commit {_cand_short}, which is now BEHIND the live "
                    f"{_integration_branch} HEAD ({_head_short}). These findings "
                    f"are POSSIBLY-STALE: they were scored against a superseded "
                    f"snapshot and may already be resolved by commits that landed "
                    f"afterward — do NOT treat them as confirmed current promotion "
                    f"blockers."
                )
                if _readiness is None:
                    # Graceful degradation: ledger signal unavailable → preserve the
                    # prior blanket steer toward a fresh re-qualification.
                    _stabilize_hint += _stale_preamble + (
                        f" Before scoping brand-new hardening work directly at them, "
                        f"PREFER emitting a fresh qualify_request to re-qualify the "
                        f"current HEAD; a new audit against the current snapshot will "
                        f"report which finding(s), if any, genuinely remain. Prior "
                        f"list (against {_cand_short}) in "
                        f"version_phase.last_qualification.audit_findings. Top prior "
                        f"findings: {_top}."
                    )
                elif _requal_ready:
                    _stabilize_hint += _stale_preamble + (
                        f" The finding-resolution ledger shows every gating "
                        f"high/medium finding is resolved-or-stale "
                        f"({_readiness.get('gating_resolved', 0)}/"
                        f"{_readiness.get('gating_total', 0)}), so the pre-registered "
                        f"re-qualification trigger is MET. PREFER emitting a fresh "
                        f"qualify_request to re-qualify the current HEAD; a new audit "
                        f"against the current snapshot will confirm the findings are "
                        f"cleared. Prior list (against {_cand_short}) in "
                        f"version_phase.last_qualification.audit_findings. Top prior "
                        f"findings: {_top}."
                    )
                else:
                    _gating_total = _readiness.get("gating_total", 0)
                    _gating_resolved = _readiness.get("gating_resolved", 0)
                    _outstanding = max(_gating_total - _gating_resolved, 0)
                    _stabilize_hint += _stale_preamble + (
                        f" The finding-resolution ledger shows {_outstanding} of "
                        f"{_gating_total} gating high/medium finding(s) still "
                        f"UNRESOLVED, so the re-qualification trigger is NOT met. Do "
                        f"NOT emit a blanket qualify_request yet — it would just "
                        f"re-flag the unresolved finding(s). Instead, prioritize the "
                        f"outstanding fix-first work: scope audit/fundamentals-mode "
                        f"remediation at the unresolved finding(s) and record each as "
                        f"resolved or code-verified stale in the resolution ledger. "
                        f"Once every gating finding is resolved-or-stale the readiness "
                        f"signal flips and a fresh qualify_request becomes the right "
                        f"move. Prior list (against {_cand_short}) in "
                        f"version_phase.last_qualification.audit_findings. Top prior "
                        f"findings: {_top}."
                    )
            else:
                _stabilize_hint += (
                    f" The last qualification's audit flagged "
                    f"{len(_carried_findings)} finding(s) — full list in "
                    f"version_phase.last_qualification.audit_findings — and "
                    f"{_binding_desc}. Top findings: {_top}."
                )
                if not _currency_resolved:
                    _stabilize_hint += (
                        " (Could not confirm the candidate commit these findings "
                        "were scored against is still the integration-branch HEAD, "
                        "so treat their currency as provisional — if a fix may have "
                        "already landed, a fresh qualify_request will confirm what "
                        "genuinely remains.)"
                    )
            # The create-a-direction escalation dispatches fix-first remediation, so
            # it fires on every path EXCEPT behind-head + readiness-met (where a fresh
            # re-qualification — not new remediation at a superseded snapshot — is the
            # highest-leverage move).  That deliberately INCLUDES the behind-head +
            # NOT-ready path: gating findings are still unresolved there, so the agent
            # must dispatch the fix work — and the live/indeterminate path where the
            # findings may genuinely still block promotion.
            if not _prefer_requalify:
                # Only audit/fundamentals-mode directions do remediation work; a
                # ship/pause direction that merely mentions the audit topic does
                # not dispatch fixes.  When no remediation-mode direction is
                # active, the agent must CREATE one — and we say so unambiguously.
                # The prior "create OR update a direction" phrasing let the agent
                # satisfy the instruction by appending a status note to an
                # unrelated ship-mode direction (observed 2026-05-27: v2 audit
                # dropped to 0.47 with 10 real findings, the agent added a status
                # note to a ship-mode direction and opened nothing, so the
                # findings were never dispatched and the score stayed below bar).
                _remediation_modes = {"audit", "fundamentals"}
                _has_remediation_direction = any(
                    d.get("status") == "active"
                    and d.get("recommended_mode") in _remediation_modes
                    for d in (project.directions or [])
                )
                if _has_remediation_direction:
                    _stabilize_hint += (
                        " You already have an active audit/fundamentals-mode "
                        "direction; confirm it explicitly targets THESE findings "
                        "(update its text/scope if not) so the remediation work "
                        "is actually dispatched this cycle."
                    )
                else:
                    _stabilize_hint += (
                        " There is currently NO active direction in audit or "
                        "fundamentals mode, so nothing is set up to FIX these "
                        "findings. Ensure one durable direction owns the outcome "
                        "and uses recommended_mode='audit': update an existing "
                        "direction when it already owns these findings, otherwise "
                        "create one. A status note alone does NOT count and will "
                        "not move the audit score. Establishing that executable "
                        "ownership is the highest-leverage action this cycle."
                    )
        prompt_suffix = (
            f"{prompt_suffix}\n{_stabilize_hint}" if prompt_suffix else _stabilize_hint
        )
        # Stabilize-stall directive — when the version has been in stabilize too
        # long without progress toward qualification (zero attempts or only soft
        # near-misses), explicitly prompt the agent to emit qualify_request or
        # resolve whatever is blocking readiness.  Skips a dry-run hold (passing
        # candidate waiting for promotion_enabled) and hard-blocker stalls (covered
        # by the calcification alert).  Best-effort — never block the payload build.
        try:
            from app.release_qualifier import (
                _STABILIZE_STALL_AGE_DAYS,
                _STABILIZE_STALL_MERGED_TASKS,
            )
            from app.release_version import count_merged_since

            _stab_qual = _open_version.last_qualification or {}
            _stab_qual_status = _stab_qual.get("status")
            _stab_is_healthy = _stab_qual_status == "dry_run" or (
                _stab_qual_status == "not_promoted" and _stab_qual.get("blocker")
            )
            if not _stab_is_healthy:
                _stab_ref = _open_version.phase_changed_at or _open_version.opened_at
                _stab_age_days = (
                    max(0, (_utcnow() - _stab_ref).days) if _stab_ref else 0
                )
                # Stabilize-phase-scoped merged-task count, mirroring the
                # qualifier backstop _check_version_stabilize_stall (which counts
                # count_merged_since(..., phase_changed_at or opened_at)).  Using
                # the version-lifetime count from _version_phase_block here would
                # include build-phase merges and misreport the stall in prompt
                # text on the common age-triggered path.
                _stab_merged = (
                    await count_merged_since(session, project.id, _stab_ref)
                    if _stab_ref
                    else 0
                )
                if (
                    _stab_age_days >= _STABILIZE_STALL_AGE_DAYS
                    or _stab_merged >= _STABILIZE_STALL_MERGED_TASKS
                ):
                    _stab_stall_hint = (
                        f"Version phase: STABILIZE — STALLED. "
                        f"v{_open_version.version_number} has been in the stabilize "
                        f"phase for {_stab_age_days} day(s) with {_stab_merged} merged "
                        f"task(s) without making progress toward qualification. "
                        f"The FIRST qualification now fires AUTOMATICALLY once "
                        f"criterion coverage is complete — qualify_request is no "
                        f"longer required to start it. Check "
                        f"version_phase.qualification_trigger_state: if it is "
                        f"'blocked' the summary/reason tells you what to resolve "
                        f"(e.g. coverage N/M) to let the trigger arm. "
                        f"qualify_request only ACCELERATES the first qualification "
                        f"(fires it before the next sweep) or re-drives after a "
                        f"conclusive verdict — emit one to fire sooner, but "
                        f"resolving the blocker is what actually unsticks the stall."
                    )
                    prompt_suffix = (
                        f"{prompt_suffix}\n{_stab_stall_hint}"
                        if prompt_suffix
                        else _stab_stall_hint
                    )
                    if _version_phase_block is not None:
                        _version_phase_block["stabilize_stall"] = {
                            "age_days": _stab_age_days,
                            "tasks_merged": _stab_merged,
                        }
        except Exception:
            logger.exception(
                "[STRATEGIC] stabilize-stall hint build failed; continuing"
            )
    # Build-stall directive — when the open version is in build and has stalled
    # past the same threshold the qualifier backstop uses, explicitly tell the
    # strategic agent the version has been building for N days / M merged tasks
    # without advancing and should consider stabilizing it.  build→stabilize is
    # agent-driven with no cadence, so this nudge is the only thing prompting
    # the agent to move a parked version forward.  Best-effort — never block the
    # payload build.
    elif _open_version is not None and _open_version.phase == "build":
        try:
            from app.release_qualifier import _is_build_stalled
            from app.release_version import count_merged_since

            _build_ref = _open_version.phase_changed_at or _open_version.opened_at
            _build_age_days = max(0, (_utcnow() - _build_ref).days) if _build_ref else 0
            # Build-phase-scoped merged-task count, mirroring the qualifier
            # backstop _check_version_build_stall (which counts
            # count_merged_since(..., phase_changed_at or opened_at)).  Using
            # the version-lifetime count from _version_phase_block here would
            # include earlier build-phase and stabilize-phase merges and
            # misreport the stall for a version re-entering build from stabilize.
            _build_merged = (
                await count_merged_since(session, project.id, _build_ref)
                if _build_ref
                else 0
            )
            if _is_build_stalled(_build_age_days, _build_merged):
                _build_stall_hint = (
                    f"Version phase: BUILD — STALLED. v{_open_version.version_number} "
                    f"has been in the build phase for {_build_age_days} day(s) with "
                    f"{_build_merged} merged task(s) without advancing to stabilize. "
                    f"build→stabilize is agent-driven and has no automatic cadence — "
                    f"consider emitting a version_phase_decision to move it to "
                    f"'stabilize' so it can progress toward qualification and "
                    f"promotion."
                )
                prompt_suffix = (
                    f"{prompt_suffix}\n{_build_stall_hint}"
                    if prompt_suffix
                    else _build_stall_hint
                )
                if _version_phase_block is not None:
                    _version_phase_block["build_stall"] = {
                        "age_days": _build_age_days,
                        "tasks_merged": _build_merged,
                    }
        except Exception:
            logger.exception("[STRATEGIC] build-stall hint build failed; continuing")
    # Fallback currency resolution: the stabilize+carried-findings path above
    # already set _currency when applicable.  For every other cycle (build phase,
    # or stabilize with no carried findings), resolve it here so the freshness
    # verdict is accurate on all paths, not just the stabilize+findings one.
    if _currency is None and _open_version is not None:
        try:
            _currency = await _resolve_candidate_currency(
                session,
                project,
                _open_version.last_qualification or {},
                _integration_branch,
            )
        except Exception:
            logger.exception(
                "[STRATEGIC] fallback candidate-currency resolution failed; continuing"
            )
        # Surface the typed currency block through version_phase on the fallback
        # path too (the stabilize+carried-findings path already set it above), so
        # an api_unreachable/stale/fresh verdict reaches buildVersionPhaseSection
        # on every cycle that has an open version.
        if (
            _currency is not None
            and _version_phase_block is not None
            and "last_qualification_candidate_currency" not in _version_phase_block
        ):
            _version_phase_block["last_qualification_candidate_currency"] = _currency
    # Derive structured payload-staleness signal from the resolved _currency dict.
    # A resolved dict exposes payload_compiled_sha, active_candidate_sha,
    # integration_head_sha, and an explicit stale/current verdict; an unresolved
    # dict carries verdict='unknown' (genuine local indeterminacy) or
    # 'api_unreachable' (transient GitHub failure) WITHOUT a stale flag, so the
    # agent can never mistake either indeterminate state for confirmed-fresh.
    _payload_staleness: dict = _build_payload_staleness_block(_currency)
    # Inject explicit list of failed approaches from retired directions so the
    # agent has a clear "do not re-propose" block in the prompt.
    from app.direction_event_trail import repair_retired_direction_memory_texts  # noqa: PLC0415
    _vision_for_repair = project.vision or {}
    if await repair_retired_direction_memory_texts(session, project.id, _vision_for_repair):
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(project, "vision")
        session.add(project)
    _retired_summary = _build_retired_directions_summary(project)
    if _retired_summary:
        _approaches_block = _build_retired_approaches_block(_retired_summary)
        if _approaches_block:
            prompt_suffix = (
                f"{prompt_suffix}\n{_approaches_block}"
                if prompt_suffix
                else _approaches_block
            )
    # Authoritative, trim-proof direction counts computed from the FULL untrimmed
    # ``project.directions`` (NOT the budget-droppable ``directions_summary``).
    # These anchor the agent-side Direction-Health / Portfolio-Composition /
    # Current-research-directions views so their headline count, slots line, and
    # gap flags can never be starved out by the same byte-budget pressure that
    # trims or drops the per-direction detail list. Shipped as a ``critical``
    # payload key (see _STRATEGIC_REVIEW_SCHEMA) so the packer never reduces them.
    _direction_counts = _build_direction_counts(project)
    _active_directions_count = _direction_counts["active"]
    _max_active_directions = _direction_counts["max_active"]
    # Keep runtime capacity subordinate to portfolio coherence. Directions are
    # durable outcomes; bounded parallel work belongs in tasks beneath them.
    _portfolio_coherence_hint = _build_portfolio_coherence_hint(
        _active_directions_count,
        _runtime_config.get("max_concurrent_vms", 0),
        _max_active_directions,
    )
    if _portfolio_coherence_hint:
        prompt_suffix = (
            f"{prompt_suffix}\n{_portfolio_coherence_hint}"
            if prompt_suffix
            else _portfolio_coherence_hint
        )
    # If this cycle was woken by operator steering, lead the prompt_suffix with
    # a high-priority steering directive so the operator's message reaches and
    # steers the strategist instead of being diluted into conversation_thread.
    # Two ways an operator wake reaches here:
    #   - Strategy-only cycle: the operator-origin pending refresh marker is
    #     still armed (read here BEFORE autonomous_research.clear_strategic_refresh
    #     consumes it on the loop's spawn-record path).
    #   - Architecture cycle Phase 2: the marker was already consumed when the
    #     Phase 1 design_doc spawn was recorded, so it reads None now —
    #     ``operator_steered`` is threaded in from the design_doc task context
    #     to keep the directive alive across the Phase 1 → Phase 2 chain.
    # Read-only on the marker; scheduled/qualification cycles never reach this.
    if operator_steered or strategic_refresh_origin(project.vision) == ORIGIN_OPERATOR:
        # Durable fallback excerpt: the threaded Phase 2 value (architecture
        # cycle, marker already consumed) takes precedence; otherwise read it
        # from the still-armed marker (strategy-only cycle). This keeps the
        # directive alive when the operator message has aged out of the thread.
        _fallback_excerpt = steering_excerpt or strategic_refresh_excerpt(
            project.vision
        )
        _steering_directive = _build_operator_steering_directive(
            conversation_thread, fallback_excerpt=_fallback_excerpt
        )
        if _steering_directive:
            prompt_suffix = (
                f"{_steering_directive}\n{prompt_suffix}"
                if prompt_suffix
                else _steering_directive
            )
    # Surface aggregated missing-foundation demand to the strategist so it can open
    # a foundation direction or record an explicit retirement of stranded demand.
    # Read-only, fail-open, additive: included only when demand is non-empty, and
    # the advisory hint is appended to the cycle prompt_suffix.
    _missing_demand = await fetch_missing_demand_for_spawn(session, project.id)
    _missing_demand_present = bool(_missing_demand.get("demands"))
    _sr_registry_names = registry_capability_names(project.problem_model)
    _sr_available_capabilities = _annotate_capability_proof_status(
        registry_capability_vocabulary(project.problem_model),
        getattr(project, "capability_proof_records", None),
    )
    _sr_capability_trial_verdicts = get_trial_verdicts(project)
    if _missing_demand_present:
        _missing_demand_hint = missing_demand_prompt_hint(
            _missing_demand, registry_names=_sr_registry_names
        )
        if _missing_demand_hint:
            prompt_suffix = (
                f"{prompt_suffix}\n{_missing_demand_hint}"
                if prompt_suffix
                else _missing_demand_hint
            )
    # Compute the grounded problem_model once so it backs BOTH the payload field
    # and the cycle-start reconciliation (capabilities carry their advisory
    # ``verification`` block here). Reusing the single computed view keeps the
    # reconciliation consistent with what the agent sees in problem_model.
    _verified_problem_model = await _verify_capability_backing_paths(
        _agent_view_problem_model(project.problem_model),
        **verification_kwargs,
    )
    # Stamp per-direction in-flight counts onto direction_stats from the SAME
    # active_by_direction mapping the reconciliation reads, so the (droppable)
    # directions_summary floor and cycle_start_reconciliation.direction_pipeline
    # can never disagree. Stamped even when 0 so staffed>0 stays distinguishable
    # from idle=0 after the reconciliation section is dropped under byte pressure.
    _stamp_direction_in_flight(
        direction_stats, (recent_interventions or {}).get("active_by_direction")
    )
    _cycle_start_reconciliation = _build_cycle_start_reconciliation(
        direction_stats,
        _verified_problem_model,
        (recent_interventions or {}).get("active_by_direction"),
        (recent_interventions or {}).get("held_by_direction"),
        (recent_interventions or {}).get("per_direction_delta"),
        running_update=project.running_update or "",
        last_qual=_open_version.last_qualification if _open_version is not None else None,
        version_phase_block=_version_phase_block,
    )
    # Soft size/health signal from the DURABLE pre-trim model so counts are honest
    # (the agent view above is trimmed). Fail-open: None when the durable model
    # fails strict validation. Hoisted above the payload so the prune-candidate
    # helper can reuse it (it only fires when the model is large/oversized).
    _sr_model_health = _compute_model_health(project.problem_model)
    # Concrete cleanup candidates, surfaced ONLY when the model is heavy — reuses
    # the grounded verification + direction linkage already computed. Advisory.
    _prune_candidates = _build_prune_candidates(
        _verified_problem_model,
        project.directions,
        _sr_model_health,
        _utcnow().isoformat(),
    )
    # Compact, body-omitted index of existing knowledge documents (web_research /
    # design output) so the strategist knows what knowledge already exists and
    # can fetch full bodies on demand — included only when documents exist.
    _knowledge_index = await build_knowledge_index(session, project.id)
    _compute_sweep_groups = await build_compute_sweep_groups(session, project.id)
    _pending_followups = await build_pending_followups(session, project.id)
    # Compact, bounded index of active strategic-memory facts (KnowledgeDocument
    # rows, source_kind='strategic_memory') — replaces shipping the full
    # strategic_memory_notes JSONB list. Full bodies via ./fetch knowledge.
    _strategic_memory_index = await build_strategic_memory_index(session, project.id)
    # Live self-hosted local-runner availability, so the strategist avoids
    # authoring GPU compute_experiment tasks (which silently fall back to
    # billable cloud GPU) while the operator's hardware is offline. Included in
    # the payload when the project has ever registered a runner OR declares the
    # enforce-local policy (below).
    _local_runner_status = await _load_local_runner_status(session, project.id)
    # Surface the enforce-local policy alongside availability so the strategist
    # can reason about consequence: when require_local_runner is True a GPU
    # experiment authored while no runner is live is HELD (never billable cloud
    # fallback) and terminally fails after the hold window; when False it falls
    # back to billable cloud GPU. Merged at the call site (not inside
    # _load_local_runner_status) so that helper's return shape stays stable for
    # its unit tests. Resolved from build_config.resource_envelope.
    _local_runner_status["require_local_runner"] = resolve_project_require_local_runner(
        project
    )
    # Baked compute image ref: the most recent digest-pinned ref built for this
    # project, so the strategist can prefer the project image over a raw NGC URI
    # when authoring compute_experiments. Merged at the call site (not inside
    # _load_local_runner_status) to keep that helper's return shape stable.
    # Fail-open: any error is swallowed and the field is simply absent.
    try:
        _baked_ref = await _load_baked_image_ref(session, project.id)
        if _baked_ref:
            _local_runner_status["baked_image_ref"] = _baked_ref
    except Exception:  # noqa: BLE001 — fail-open; baked ref is advisory
        logger.debug("[STRATEGIC] baked image ref load failed; continuing")
    # Experiment-authoring context: the experiment LEDGER (a bounded query over
    # recent terminal compute_experiment tasks — what was already measured, at
    # what cost, with what verdict) plus one-line near-duplicate advisories for
    # pending experiments that closely match a prior entry. All experiment-
    # authoring additions — the devices block riding inside local_runner_status
    # above, the ledger lines, and the advisories — share ONE char budget with
    # priority devices > ledger > advisories (module constants in
    # app.experiment_authoring_context, no runtime-config keys), so they can
    # never crowd out existing context. Fail-open: any error drops the block.
    _experiment_ledger_block = ""
    try:
        from app.experiment_authoring_context import (
            apply_experiment_authoring_budget,
            build_experiment_ledger,
            build_near_duplicate_advisories,
            fetch_pending_experiment_specs,
            render_experiment_ledger_lines,
            render_experiment_ledger_prompt_block,
        )

        _ledger_entries = await build_experiment_ledger(session, project.id)
        _ledger_lines = render_experiment_ledger_lines(_ledger_entries)
        _dup_advisories = build_near_duplicate_advisories(
            await fetch_pending_experiment_specs(session, project.id),
            _ledger_entries,
        )
        _budgeted_devices, _ledger_lines, _dup_advisories = (
            apply_experiment_authoring_budget(
                _local_runner_status.get("devices"), _ledger_lines, _dup_advisories
            )
        )
        if _budgeted_devices:
            _local_runner_status["devices"] = _budgeted_devices
        else:
            _local_runner_status.pop("devices", None)
        _experiment_ledger_block = render_experiment_ledger_prompt_block(
            _ledger_lines, _dup_advisories
        )
    except Exception:
        logger.exception(
            "[STRATEGIC] experiment-authoring ledger build failed; continuing"
        )
    if _experiment_ledger_block:
        prompt_suffix = (
            f"{prompt_suffix}\n{_experiment_ledger_block}"
            if prompt_suffix
            else _experiment_ledger_block
        )
    _ci_compliance_block: dict[str, Any] | None = None
    try:
        from app import ci_compliance as _ci_compliance

        _ci_status = await _ci_compliance.ensure_current_ci_compliance(session, project)
        if _ci_status.get("status") == "success":
            _ci_compliance_block = _ci_compliance.compact_ci_compliance(
                _ci_status.get("data")
            )
            _ci_gate = ci_compliance_qualify_gate(
                project, _ci_status.get("head_sha")
            )
            if _ci_gate.get("blocked"):
                await _maybe_spawn_ci_compliance_cap_bump_fix(
                    session, project, _ci_gate
                )
    except Exception:
        logger.exception("[STRATEGIC] ci_compliance signal build failed; continuing")
    _open_completion_debt_block: dict[str, Any] | None = None
    try:
        from importlib import import_module

        _completion_debt = import_module("app.completion_debt")
        _open_completion_debt_block = await _completion_debt.get_open_completion_debt(
            session, project.id
        )
    except Exception:
        logger.exception("[STRATEGIC] open_completion_debt signal build failed; continuing")
    _active_direction_ids = [
        d.get("id")
        for d in (project.directions or [])
        if d.get("status") in ("active", "expired", "paused") and d.get("id")
    ]
    _knowledge_by_direction = await fetch_direction_knowledge_index(
        session, project.id, _active_direction_ids
    )

    payload: dict = pack_under_budget(
        {
            "type": "strategic_review",
            "research_mode": "strategic",
            # Compact, bounded navigation index over the grounded agent-view
            # design doc (full per-section content fetched on demand via the
            # design-doc op:get endpoint) — keeps the design doc from dominating
            # the payload budget as it grows. See _design_doc_index.
            "design_doc": _design_doc_index(
                await ground_design_doc(
                    _agent_view_design_doc(project.design_doc), **verification_kwargs
                )
            ),
            "vision": _agent_visible_vision(project.vision),
            "effective_purpose": resolve_project_purpose(project),
            "problem_model": _verified_problem_model,
            **(
                {"cycle_start_reconciliation": _cycle_start_reconciliation}
                if _cycle_start_reconciliation
                else {}
            ),
            # Soft size/health signal from the DURABLE pre-trim model so counts
            # are honest (the agent view above is trimmed). Fail-open: omitted
            # when the durable model fails strict validation (computed above).
            **({"model_health": _sr_model_health} if _sr_model_health else {}),
            # Concrete prune candidates — present only when the model is
            # large/oversized AND something is actually prunable. Advisory list.
            **({"prune_candidates": _prune_candidates} if _prune_candidates else {}),
            # Gaps whose history entries warrant compaction — surfaced so the
            # strategist can apply targeted compaction without manual inspection.
            **(
                {"compaction_hints": _cadence_compaction_hints}
                if (_cadence_compaction_hints := _build_compaction_hints(_verified_problem_model))
                else {}
            ),
            **({"knowledge_index": _knowledge_index} if _knowledge_index else {}),
            **({"pending_followups": _pending_followups} if _pending_followups else {}),
            **({"compute_experiment_sweeps": _compute_sweep_groups} if _compute_sweep_groups else {}),
            # Live local-runner availability — surfaced when the operator has
            # ever registered self-hosted hardware OR the project declares the
            # enforce-local policy (require_local_runner). Absent => agent authors
            # for cloud normally; present => agent can hold GPU experiments while
            # active_gpu == 0 to avoid silent billable-cloud-GPU fallback (or, when
            # require_local_runner is set, an indefinite hold + terminal fail).
            **(
                {"local_runner_status": _local_runner_status}
                if (
                    _local_runner_status.get("ever_registered")
                    or _local_runner_status.get("require_local_runner")
                    or _local_runner_status.get("baked_image_ref")
                )
                else {}
            ),
            "running_update": project.running_update or "",
            "running_update_acknowledged_at": (
                project.running_update_acknowledged_at.isoformat()
                if project.running_update_acknowledged_at
                else None
            ),
            "user_last_active_at": user_last_active_at,
            "self_reflection_log": (project.self_reflection_log or [])[-5:],
            **(
                {"self_reflection_themes": _self_reflection_themes}
                if (
                    _self_reflection_themes := _build_self_reflection_themes(
                        project.self_reflection_log
                    )
                )
                else {}
            ),
            "strategic_memory_notes": _strategic_memory_index,
            # De-gated from the raw project.is_self_repo column to the deploy-repo
            # predicate: the strategist's self-improvement framing (modify your own
            # prompts/schemas/orchestration) keys off this field, so it must follow
            # deploy identity like every other migrated backend gate. Falls back to
            # is_self_repo verbatim on deployments with no stamped identity, so this
            # cannot regress unstamped deployments or tests.
            "is_self_repo": project_is_deploy_repo(project),
            # Machine-checkable g-generality retirement signal: set when a
            # create/fix/refactor task has merged on this external project for
            # the first time.  Absent (no key) for self-repo and projects with
            # no merged improvements yet.
            **(
                {"first_autonomous_improvement_at": _first_improvement_at}
                if (
                    _first_improvement_at := (project.build_config or {}).get(
                        "first_autonomous_improvement_at"
                    )
                )
                else {}
            ),
            "enable_self_improvement": project.enable_self_improvement,
            # Trim-proof authoritative direction counts + operator ceiling. Both
            # are ``critical`` so the agent's direction views always anchor on the
            # true active set even when ``directions_summary`` is trimmed/dropped.
            "direction_counts": _direction_counts,
            "max_active_directions": _max_active_directions,
            "directions_summary": direction_stats,
            **(
                {"staged_directions": _staged_directions}
                if (_staged_directions := _build_staged_directions_summary(project))
                else {}
            ),
            "outcome_evaluation": _build_outcome_evaluation(
                project,
                cycle_history,
                knowledge_by_direction=_knowledge_by_direction or None,
            ),
            "recent_interventions": recent_interventions,
            "prediction_calibration_factor": calibration_factor,
            "prediction_calibration_note": calibration_note,
            **(
                {"previous_decision_outcomes": _prev_outcomes}
                if (
                    _prev_outcomes := _build_previous_decision_outcomes(
                        project, cycle_history
                    )
                )
                else {}
            ),
            **(
                {"anomaly_summary": _anomaly_summary}
                if (_anomaly_summary := _build_anomaly_summary(project))
                else {}
            ),
            **(
                {"playbook_summary": _playbook_summary}
                if (_playbook_summary := _build_playbook_summary(project))
                else {}
            ),
            **(
                {"playbook_keys_index": _playbook_keys_index}
                if (_playbook_keys_index := _build_playbook_keys_index(project))
                else {}
            ),
            "project_name": project.name,
            "open_message_count": len(open_msgs),
            "open_messages": open_msgs,
            **(
                {"operator_context_refs": operator_context_refs}
                if operator_context_refs
                else {}
            ),
            "conversation_thread": conversation_thread,
            **(
                {"operator_overrides": _operator_overrides}
                if (_operator_overrides := _build_operator_overrides_summary(project))
                else {}
            ),
            **(
                {"rejected_proposals": rejected_proposals} if rejected_proposals else {}
            ),
            **(
                {"running_update_superseded": running_update_superseded}
                if running_update_superseded
                else {}
            ),
            **({"prompt_suffix": prompt_suffix} if prompt_suffix else {}),
            # Mode-aware: include only when non-empty so cold-start projects
            # don't carry empty fields through the strategic payload size cap.
            **(
                {"system_maturity": system_maturity_payload}
                if system_maturity_payload
                else {}
            ),
            **({"drift_signals": drift_summary} if drift_summary else {}),
            **({"version_phase": _version_phase_block} if _version_phase_block else {}),
            **({"payload_staleness": _payload_staleness} if _payload_staleness else {}),
            **({"ci_compliance": _ci_compliance_block} if _ci_compliance_block else {}),
            **(
                {"open_completion_debt": _open_completion_debt_block}
                if _open_completion_debt_block
                else {}
            ),
            **(
                {"deployment_state": _deployment_state_block}
                if _deployment_state_block
                else {}
            ),
            **({"memo_citations": memo_citations} if memo_citations else {}),
            **(
                {"past_retirement_critiques": _past_critiques}
                if (_past_critiques := _build_past_retirement_critiques(cycle_history))
                else {}
            ),
            **(
                {"retirement_deferrals": _retirement_deferrals}
                if (
                    _retirement_deferrals := _build_past_retirement_deferrals(
                        cycle_history
                    )
                )
                else {}
            ),
            **(
                {"retired_directions_summary": _retired_summary}
                if _retired_summary
                else {}
            ),
            **(
                {"efficiency_snapshot": _efficiency_snapshot}
                if _efficiency_snapshot
                else {}
            ),
            **(
                {"integrity_conflict_trend": _integrity_trend}
                if _integrity_trend
                else {}
            ),
            **(
                {"held_directions": _held_directions_block}
                if _held_directions_block
                else {}
            ),
            **(
                {"missing_capability_demand": _missing_demand}
                if _missing_demand_present
                else {}
            ),
            **(
                {"available_capabilities": _sr_available_capabilities}
                if _sr_available_capabilities
                else {}
            ),
            **(
                {"capability_trial_verdicts": _sr_capability_trial_verdicts}
                if _sr_capability_trial_verdicts
                else {}
            ),
            "base_branch": get_project_integration_branch(project),
            **({"model": review_model} if review_model else {}),
            **({"effort": review_effort} if review_effort else {}),
        },
        _STRATEGIC_REVIEW_SCHEMA,
    )
    payload = validate_internal_payload(payload, "autonomous_research:strategic_review")

    # Extract the three integrity-conflict counts from the reconciliation so they
    # can be persisted on the cycle row for trend queries.
    _recon_counts = (
        (_cycle_start_reconciliation or {}).get("counts") or {}
        if _cycle_start_reconciliation
        else {}
    )
    _integrity_counts: dict[str, int] | None = {
        "orphaned_gaps": int(_recon_counts.get("orphaned_gaps") or 0),
        "ungrounded_directions": int(_recon_counts.get("ungrounded_directions") or 0),
        "stale_active_moves": int(_recon_counts.get("stale_active_moves") or 0),
        "staffed_but_deferred": int(_recon_counts.get("staffed_but_deferred") or 0),
    }

    # Re-arm the duplicate-spawn guards, create the task, record the cycle, and
    # commit — the short finalize transaction (see
    # ``_finalize_strategic_review_spawn``).
    return await _finalize_strategic_review_spawn(
        session,
        project_id=_project_id,
        project_name=_project_name,
        payload=payload,
        cycle_id=cycle_id,
        strategy_only=strategy_only,
        base_vision=_base_vision,
        base_problem_model=_base_problem_model,
        base_running_update=_base_running_update,
        local_runner_intent=local_runner_intent,
        preferred_runner_target_id=preferred_runner_target_id,
        extra_context=extra_context,
        operator_steered=operator_steered,
        steering_excerpt=steering_excerpt,
        launched_by_user_id=launched_by_user_id,
        integrity_conflict_counts=_integrity_counts,
        expected_legacy_generation=expected_legacy_generation,
    )


# ---------------------------------------------------------------------------
# Spawn-time failure-feedback mitigation helper
# ---------------------------------------------------------------------------

# Minimum first-attempt rejections in a single category to trigger downscoping.
_FAILURE_DOWNSCOPE_THRESHOLD = 3
# Minimum success-pattern observations to bias scope selection toward a smaller scope.
_SUCCESS_BIAS_THRESHOLD = 3

_SCOPE_DOWNGRADE: dict[str, str] = {"large": "medium", "medium": "small"}
_SCOPE_ORDER_LIST = ["small", "medium", "large"]


def _build_failure_feedback_mitigation(
    direction: dict,
    proposed_scope: str,
    task_type: str,
    design_doc: dict | None,
) -> tuple[str, str | None, dict]:
    """Return (final_scope, coaching_note, evidence) for spawn-time mitigation.

    Reads direction_first_attempt_feedback for per-category rejection counts and
    playbook/success-* entries for successful scope shapes.

    - Downscopes by one level when any category reaches _FAILURE_DOWNSCOPE_THRESHOLD
      first-attempt rejections, and sets a coaching note naming the failure mode.
    - Biases toward the smallest scope that has _SUCCESS_BIAS_THRESHOLD+ successes
      in playbook/success-{task_type}-{scope}, applied after failure downscoping.

    Pure — no DB queries. Returns proposed_scope unchanged when no signal fires.
    """
    doc = design_doc or {}
    direction_id = str(direction.get("id") or "")
    final_scope = proposed_scope
    coaching_note: str | None = None
    evidence: dict = {"proposed_scope": proposed_scope}

    # --- Failure-concentration downscoping ---
    dfa = doc.get("direction_first_attempt_feedback") or {}
    fa_entry = dfa.get(direction_id) or {}
    cat_counts = {
        k: v
        for k, v in fa_entry.items()
        if k not in ("total", "last_updated") and isinstance(v, int) and v > 0
    }
    if cat_counts:
        top_cat = max(cat_counts, key=lambda k: cat_counts[k])
        top_count = cat_counts[top_cat]
        if (
            top_count >= _FAILURE_DOWNSCOPE_THRESHOLD
            and final_scope in _SCOPE_DOWNGRADE
        ):
            downscoped = _SCOPE_DOWNGRADE[final_scope]
            evidence["failure_downscope"] = {
                "category": top_cat,
                "count": top_count,
                "from": final_scope,
                "to": downscoped,
            }
            logger.info(
                "[SPAWN] Downscoping %s→%s for direction '%s' (first-attempt %s=%d)",
                final_scope,
                downscoped,
                direction_id,
                top_cat,
                top_count,
            )
            final_scope = downscoped
            coaching_note = (
                f"Historical pattern: {top_count} first-attempt rejections in "
                f"'{top_cat.replace('_', ' ')}' for this direction. "
                f"Pay extra attention to {top_cat.replace('_', ' ')} before submitting."
            )

    # --- Success-pattern scope bias ---
    proposed_idx = (
        _SCOPE_ORDER_LIST.index(proposed_scope)
        if proposed_scope in _SCOPE_ORDER_LIST
        else -1
    )
    if proposed_idx > 0:
        for candidate in _SCOPE_ORDER_LIST[:proposed_idx]:
            key = f"playbook/success-{task_type}-{candidate}"
            entry = doc.get(key)
            success_count = (
                entry.get("success_count") if isinstance(entry, dict) else None
            )
            if (
                isinstance(success_count, int)
                and success_count >= _SUCCESS_BIAS_THRESHOLD
            ):
                evidence["success_bias"] = {
                    "key": key,
                    "success_count": success_count,
                    "from": final_scope,
                    "to": candidate,
                }
                logger.info(
                    "[SPAWN] Success bias %s→%s for task_type='%s' ('%s': %d successes)",
                    final_scope,
                    candidate,
                    task_type,
                    key,
                    success_count,
                )
                final_scope = candidate
                break  # use smallest qualifying scope

    evidence["final_scope"] = final_scope
    return final_scope, coaching_note, evidence
