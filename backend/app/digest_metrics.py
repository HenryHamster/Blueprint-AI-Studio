"""Metric-computation helpers extracted from admin_digest.

Most are synchronous transforms over already-loaded Task / RegressionRemediationCase
data — no AsyncSession and no FastAPI Request. A small number (e.g.
``compute_remediation_funnel``) are async DB helpers that run their own self-contained
queries off a passed-in ``AsyncSession``. They are imported (and re-exported) by
``app.routers.admin_digest`` so existing call sites and test patch targets keep working.
"""

import logging
import math
import statistics
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import load_only
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.capability_trial_verdict import get_trial_definitions
from app.database import resolve_dialect_name
from app.landed_signal import has_landed_signal, merge_verification_status
from app.models import (
    Alert,
    Project,
    QualificationVerdict,
    Task,
    TaskStatus,
    TaskWakeRegistration,
    WakeDeliveryState,
    get_task_total_vm_cost_usd,
)
from app.time_utils import _coerce_naive_utc, _utcnow

# Remediation funnel + recurring-failure-pattern helpers relocated to their own
# module. Re-exported here so existing import paths are unchanged.
from app.digest_remediation import (  # noqa: F401
    compute_remediation_and_patterns,
    compute_remediation_funnel,
)

# Per-direction progress aggregation (the single largest analytic) relocated to
# its own module. Re-exported here so existing import paths are unchanged.
from app.digest_direction_progress import compute_direction_progress  # noqa: F401

# Per-effort-tier / per-actual-model efficiency rollup lives next to the shared
# LLM pricing table in efficiency_snapshot (so its totals reconcile with the
# per-model cost breakdown). Re-exported here so the digest consumer and tests
# import it from the same module as the other digest metrics.
from app.efficiency_snapshot import (  # noqa: F401
    compute_effort_model_efficiency,
    compute_provider_routing_share,
)

# SR/review-quality metrics cluster relocated to its own module.
# Re-exported here so existing import paths are unchanged.
from app.sr_review_quality_metrics import (  # noqa: F401
    _SR_CODE_CHANGE_TYPES,
    _SR_SEVERITY_WEIGHTS,
    _is_skipped_sr_outcome,
    _SR_TREND_STALENESS_DAYS,
    _REVIEW_ELIGIBLE_TYPES,
    _stash_projected_task_fields,
    _task_prompt_fingerprint,
    _author_sr_failed_categories,
    _serialize_sr_catch_rate_trends,
    _compute_sr_skip_rate,
    _compute_sr_disallowed_blind_spot_rate,
    _sr_miss_drilldown,
    _compute_edges_considered_metrics,
    compute_sr_prediction_accuracy,
    compute_review_quality_metrics,
)

# Capability-trial arm DB helpers relocated to their own module.
# Re-exported here so existing import paths (and mock.patch targets) are unchanged.
from app.digest_trial_arms import (  # noqa: F401
    _json_truthy,
    compute_trial_arm_accumulation,
    compute_trial_arm_accrual,
    compute_trial_arm_cost_delta,
)

# Outer-loop convergence helpers (PR-chain grouping, escalation-alert parsing,
# terminal-outcome resolution) relocated to their own module. Re-exported here
# so existing import paths (and mock.patch targets) are unchanged.
from app.digest_outer_loop import (  # noqa: F401
    _ESCALATION_TITLE_PREFIX,
    _ESCALATION_LABEL_TO_REASON,
    _TERMINAL_OUTCOME_KEY,
    _TERMINAL_OUTCOME_KEYS,
    _ESCALATION_OUTCOME_KEYS,
    _outer_loop_pct,
    _outer_loop_chain_key,
    _outer_loop_terminal_outcome,
    _group_outer_loop_chains,
    _scan_escalation_alerts,
    _resolve_chain_outcome,
    _outer_loop_chain_observation,
    _resolve_outer_loop_chains,
)

# Lane-waste diagnostics (v10 pilot taxonomy + baseline trend) relocated to their
# own module. Re-exported here so existing import paths are unchanged.
from app.digest_lane_waste import (  # noqa: F401
    LANE_WASTE_CATEGORIES,
    _lane_waste_completed_at,
    classify_lane_waste_category,
    _lane_waste_task_row,
    compute_lane_waste_diagnostics,
    compute_lane_baseline_trend,
)
from app.failure_classification import (
    ADMIN_CANCEL,
    AGENT_STALLED,
    AGENT_TIMEOUT,
    APPROVED_CAP_PR_CLOSED,
    BLOCKED_PRECONDITION,
    BRANCH_GONE,
    REPO_UNINITIALIZED,
    CODE_ERROR,
    DEAD_AUTH,
    EXTERNAL_API,
    MIGRATION_ERROR,
    NO_PROGRESS_TIMEOUT,
    PARK_WAKE_STRANDED,
    PARK_WAKE_TIMEOUT,
    PROVISION_FAILURE,
    RATE_LIMIT,
    REASSIGNED,
    SCOPE_EMPTY,
    SCOPE_OVERFLOW,
    TIMEOUT_CASCADE,
    TIMEOUT_EXECUTION,
    TIMEOUT_HEARTBEAT_LOST,
    TIMEOUT_ITERATION_LIMIT,
    TIMEOUT_OOM,
    TIMEOUT_PROCESS_CRASH,
    TIMEOUT_UNKNOWN,
    UNPLACEABLE_EXECUTION_SITE,
    VIEWPORT_REGRESSION,
)

logger = logging.getLogger(__name__)

_CPM_RETIREMENT_MULTIPLIER = 3.0  # cost_per_merge > median × this → retirement signal

# Minimum clean repair attempts (repair_attempts - infra_failures) for a per-type
# self_repair_success_rate to be considered reliable.  Mirrors the same threshold
# in efficiency_snapshot.SELF_REPAIR_MIN_ATTEMPTS (efficiency_snapshot imports
# digest_metrics only lazily inside a function, so a module-level import here would
# not create a cycle, but duplicating the literal keeps the dependency one-directional).
_SELF_REPAIR_MIN_ATTEMPTS = 10

_FAILURE_CLASS_LABELS = {
    CODE_ERROR: "Code Error",
    PROVISION_FAILURE: "Provision Failure",
    AGENT_TIMEOUT: "Agent Timeout",
    EXTERNAL_API: "External API",
    RATE_LIMIT: "Rate Limit",
    DEAD_AUTH: "Dead Auth — account disabled (re-authenticate to restore)",
    SCOPE_OVERFLOW: "Scope Overflow",
    MIGRATION_ERROR: "Migration Error",
    VIEWPORT_REGRESSION: "Viewport Regression",
    AGENT_STALLED: "Agent Stalled",
    BLOCKED_PRECONDITION: "Blocked — precondition",
    BRANCH_GONE: "Branch deleted (PR merged before clone)",
    REPO_UNINITIALIZED: "Repository has no commits (never seeded)",
    SCOPE_EMPTY: "Scope Empty",
    REASSIGNED: "Agent Reassigned",
    NO_PROGRESS_TIMEOUT: "No-Progress Timeout",
    PARK_WAKE_TIMEOUT: "Park/Wake — source never fired (timed out)",
    PARK_WAKE_STRANDED: "Park/Wake — stranded (no live wake registration)",
    APPROVED_CAP_PR_CLOSED: "Approved Cap PR Closed — fix-loop cap hit post-approval",
    ADMIN_CANCEL: "Admin Cancel",
    UNPLACEABLE_EXECUTION_SITE: "Unplaceable — declared execution site has no dispatch path",
}

_FINDING_STRUCTURAL_GIVE_UP_LIST_LIMIT = 5


def _compute_first_attempt_breakdowns(
    terminal_tasks: List[Task],
    prev_terminal_tasks: List[Task] | None = None,
    *,
    trial_definitions: list | None = None,
) -> Dict[str, Any]:
    """Compute overall first-attempt success rate plus playbook and arch-context
    effectiveness comparisons from a list of terminal tasks."""

    def _fa_rate(tasks):
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        succ = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(succ / len(measured), 2)

    measured = [
        t for t in terminal_tasks if "_first_attempt_succeeded" in (t.context or {})
    ]
    with_pb = [t for t in measured if (t.context or {}).get("_had_playbook") is True]
    without_pb = [
        t for t in measured if (t.context or {}).get("_had_playbook") is False
    ]
    with_arch = [
        t for t in measured if (t.context or {}).get("_had_arch_context") is True
    ]
    without_arch = [
        t for t in measured if (t.context or {}).get("_had_arch_context") is False
    ]
    sample_size = len(measured)

    _defs = (
        trial_definitions if trial_definitions is not None else get_trial_definitions()
    )
    _min_lift_sample = 5
    trial_breakdowns: Dict[str, Any] = {}
    for _td in _defs:
        _cap = _td["cap_name"]
        _ctx_key = _td["context_key"]
        # Intent-to-treat: when an assignment key is present, partition on
        # assignment; fall back to the receipt key for legacy tasks that
        # pre-date the assignment field (i.e. _assign_key is absent).
        _assign_key = _td.get("assignment_key", _ctx_key)
        _with_arm = [
            t
            for t in measured
            if (t.context or {}).get(_assign_key) is True
            or (
                (t.context or {}).get(_assign_key) is None
                and (t.context or {}).get(_ctx_key) is True
            )
        ]
        _without_arm = [
            t
            for t in measured
            if (t.context or {}).get(_assign_key) is False
            or (
                (t.context or {}).get(_assign_key) is None
                and (t.context or {}).get(_ctx_key) is False
            )
        ]
        _with_rate = _fa_rate(_with_arm)
        _without_rate = _fa_rate(_without_arm)
        _lift_val: float | None = (
            round(_with_rate - _without_rate, 2)
            if (
                _with_rate is not None
                and _without_rate is not None
                and len(_with_arm) >= _min_lift_sample
                and len(_without_arm) >= _min_lift_sample
            )
            else None
        )
        _trial_entry: Dict[str, Any] = {
            "lift": _lift_val,
            "with_n": len(_with_arm),
            "without_n": len(_without_arm),
            "with_rate": _with_rate,
            "without_rate": _without_rate,
        }
        # Compliance sub-dict: among treatment-arm tasks, what fraction actually
        # received the capability (receipt key True). Only present when assignment
        # and receipt keys differ, so adding it does not break consumers that only
        # check the existing flat keys.
        if _assign_key != _ctx_key:
            _receipt_n = len(_with_arm)
            _receipt_true = sum(
                1 for t in _with_arm if (t.context or {}).get(_ctx_key) is True
            )
            _trial_entry["compliance"] = {
                "with_rate": round(_receipt_true / _receipt_n, 2)
                if _receipt_n
                else None,
                "without_rate": round((_receipt_n - _receipt_true) / _receipt_n, 2)
                if _receipt_n
                else None,
            }
        trial_breakdowns[_cap] = _trial_entry

    def _lift(flag, tasks):
        tasks = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        a = [t for t in tasks if (t.context or {}).get(flag) is True]
        b = [t for t in tasks if (t.context or {}).get(flag) is False]
        return (
            round(_fa_rate(a) - _fa_rate(b), 2)
            if len(a) >= 10 and len(b) >= 10
            else None
        )

    playbook_lift = _lift("_had_playbook", measured)
    arch_context_lift = _lift("_had_arch_context", measured)

    by_type_groups: Dict[str, list] = {}
    for t in terminal_tasks:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type_groups.setdefault(task_type, []).append(t)
    by_type: Dict[str, float] = {}
    for task_type, tasks in by_type_groups.items():
        rate = _fa_rate(tasks)
        if rate is not None:
            by_type[task_type] = rate

    by_scope_groups: Dict[str, list] = {}
    for t in terminal_tasks:
        scope = (t.context or {}).get("scope", "unknown")
        by_scope_groups.setdefault(scope, []).append(t)
    by_scope: Dict[str, float] = {}
    for scope, tasks in by_scope_groups.items():
        rate = _fa_rate(tasks)
        if rate is not None:
            by_scope[scope] = rate

    overall = _fa_rate(terminal_tasks)
    trend: str | None = None
    if prev_terminal_tasks is not None:
        prev_rate = _fa_rate(prev_terminal_tasks)
        if overall is not None and prev_rate is not None:
            delta = overall - prev_rate
            if delta > 0.05:
                trend = "improving"
            elif delta < -0.05:
                trend = "declining"
            else:
                trend = "stable"
    playbook_lift_trend: str | None = None
    arch_context_lift_trend: str | None = None
    if prev_terminal_tasks is not None:
        prev_playbook_lift = _lift("_had_playbook", prev_terminal_tasks)
        prev_arch_context_lift = _lift("_had_arch_context", prev_terminal_tasks)
        playbook_lift_trend = _format_compliance_trend(
            playbook_lift, prev_playbook_lift
        )
        arch_context_lift_trend = _format_compliance_trend(
            arch_context_lift, prev_arch_context_lift
        )

    first_attempt_by_review_category: Dict[str, float] | None = None
    failed_with_cats = [
        t
        for t in terminal_tasks
        if "_first_attempt_rejection_categories" in (t.context or {})
    ]
    if failed_with_cats:
        total_with_cats = len(failed_with_cats)
        cat_counts: Dict[str, int] = {}
        for t in failed_with_cats:
            cats = (t.context or {}).get("_first_attempt_rejection_categories", {})
            for cat in cats:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        first_attempt_by_review_category = {
            cat: round(count / total_with_cats, 2)
            for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1])
        }

    # verified_overall: excludes degraded-unverified merges from the success
    # count.  A task with _first_attempt_succeeded=True whose PR merged via a
    # degraded fall-open (environment/setup failure blocked verification) is not
    # a proven fix on HEAD and should not inflate the success rate.  Benign
    # fall-opens (no detectable suite, kill-switch off) still count.  Additive
    # — does not replace "overall".
    def _fa_verified_rate(tasks):
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        succ = sum(
            1
            for t in measured
            if t.context["_first_attempt_succeeded"]
            and merge_verification_status(t) != "unverified_degraded"
        )
        return round(succ / len(measured), 2)

    # Build flat alias keys from trial_breakdowns for backward compatibility.
    _flat_trial: Dict[str, Any] = {}
    for _cap, _bd in trial_breakdowns.items():
        _flat_trial[f"{_cap}_lift"] = _bd["lift"]
        _flat_trial[f"with_{_cap}_n"] = _bd["with_n"]
        _flat_trial[f"without_{_cap}_n"] = _bd["without_n"]
        _flat_trial[f"with_{_cap}"] = _bd["with_rate"]
        _flat_trial[f"without_{_cap}"] = _bd["without_rate"]

    return {
        "overall": overall,
        "verified_overall": _fa_verified_rate(terminal_tasks),
        "with_playbook": _fa_rate(with_pb),
        "without_playbook": _fa_rate(without_pb),
        "playbook_lift": playbook_lift,
        "playbook_lift_trend": playbook_lift_trend,
        "with_arch_context": _fa_rate(with_arch),
        "without_arch_context": _fa_rate(without_arch),
        "arch_context_lift": arch_context_lift,
        "arch_context_lift_trend": arch_context_lift_trend,
        **_flat_trial,
        "trial_breakdowns": trial_breakdowns,
        "sample_size": sample_size,
        "by_type": by_type,
        "by_scope": by_scope,
        "trend": trend,
        "first_attempt_by_review_category": first_attempt_by_review_category,
    }


def _accumulate_format_compliance(tasks: List[Task]) -> Dict[str, Any]:
    """Roll a list of tasks' ``format_compliance_summary`` into one count+rate block.

    Tasks whose ``format_compliance_summary`` is NULL/missing or not a dict are
    skipped (fail-safe neutral — older agents leave it NULL). ``format_error_total``
    is the extraction layer's count of unparseable FINAL outputs (a "format miss");
    a task is *well-formed* when that count is zero. Self-repair counts are the
    focused re-ask attempts after a miss and how many of them parsed.

    ``by_schema`` in the returned block aggregates per-schema repair attempts and
    successes across tasks. Tasks from older agents that lack per-schema maps
    contribute to the scalar totals but not to ``by_schema``.
    """
    measured = 0
    well_formed = 0
    error_total = 0
    repair_attempts = 0
    repair_successes = 0
    repair_infra_failures = 0
    repair_input_tokens = 0
    repair_output_tokens = 0
    miss_by_reason: defaultdict[str, int] = defaultdict(int)
    repair_miss_by_reason: defaultdict[str, int] = defaultdict(int)
    miss_by_schema: defaultdict[str, int] = defaultdict(int)
    miss_by_schema_and_reason: defaultdict[str, defaultdict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    repair_attempts_by_schema: defaultdict[str, int] = defaultdict(int)
    repair_successes_by_schema: defaultdict[str, int] = defaultdict(int)
    repair_infra_failures_by_schema: defaultdict[str, int] = defaultdict(int)
    repair_input_tokens_by_schema: defaultdict[str, int] = defaultdict(int)
    repair_output_tokens_by_schema: defaultdict[str, int] = defaultdict(int)
    repair_miss_by_schema: defaultdict[str, int] = defaultdict(int)
    for t in tasks:
        summary = getattr(t, "format_compliance_summary", None)
        if not isinstance(summary, dict):
            continue
        measured += 1
        e = int(summary.get("format_error_total") or 0)
        a = int(summary.get("format_repair_attempts") or 0)
        s = int(summary.get("format_repair_successes") or 0)
        f = int(summary.get("format_repair_infra_failures") or 0)
        it = int(summary.get("format_repair_input_tokens") or 0)
        ot = int(summary.get("format_repair_output_tokens") or 0)
        error_total += e
        repair_attempts += a
        repair_successes += s
        repair_infra_failures += f
        repair_input_tokens += it
        repair_output_tokens += ot
        if e == 0:
            well_formed += 1
        for reason, count in (summary.get("format_miss_by_reason") or {}).items():
            miss_by_reason[str(reason)] += int(count)
        for reason, count in (
            summary.get("format_repair_miss_by_reason") or {}
        ).items():
            repair_miss_by_reason[str(reason)] += int(count)
        for schema, count in (summary.get("format_miss_by_schema") or {}).items():
            miss_by_schema[str(schema)] += int(count)
        for schema, reasons in (summary.get("format_miss_by_schema_and_reason") or {}).items():
            if isinstance(reasons, dict):
                for reason, count in reasons.items():
                    miss_by_schema_and_reason[str(schema)][str(reason)] += int(count)
        for schema, count in (
            summary.get("format_repair_attempts_by_schema") or {}
        ).items():
            repair_attempts_by_schema[str(schema)] += int(count)
        for schema, count in (
            summary.get("format_repair_successes_by_schema") or {}
        ).items():
            repair_successes_by_schema[str(schema)] += int(count)
        for schema, count in (
            summary.get("format_repair_infra_failures_by_schema") or {}
        ).items():
            repair_infra_failures_by_schema[str(schema)] += int(count)
        for schema, count in (
            summary.get("format_repair_input_tokens_by_schema") or {}
        ).items():
            repair_input_tokens_by_schema[str(schema)] += int(count)
        for schema, count in (
            summary.get("format_repair_output_tokens_by_schema") or {}
        ).items():
            repair_output_tokens_by_schema[str(schema)] += int(count)
        for schema, count in (
            summary.get("format_repair_miss_by_schema") or {}
        ).items():
            repair_miss_by_schema[str(schema)] += int(count)
    all_schemas = (
        set(miss_by_schema)
        | set(repair_attempts_by_schema)
        | set(repair_successes_by_schema)
        | set(repair_infra_failures_by_schema)
        | set(repair_input_tokens_by_schema)
        | set(repair_output_tokens_by_schema)
        | set(repair_miss_by_schema)
    )

    # Minimum clean repair attempts per schema before self_repair_success_rate
    # is meaningful. Below this threshold the ratio is too noisy (1-2 repairs
    # flip the rate between 0% and 100%) and the value is suppressed to None.
    _SCHEMA_MIN_REPAIR_ATTEMPTS = 3

    def _schema_entry(schema: str) -> dict:
        attempts = repair_attempts_by_schema.get(schema, 0)
        successes = repair_successes_by_schema.get(schema, 0)
        infra = repair_infra_failures_by_schema.get(schema, 0)
        clean = attempts - infra
        return {
            "format_miss": miss_by_schema.get(schema, 0),
            "repair_attempts": attempts,
            "repair_successes": successes,
            "repair_infra_failures": infra,
            "repair_miss": repair_miss_by_schema.get(schema, 0),
            "repair_input_tokens": repair_input_tokens_by_schema.get(schema, 0),
            "repair_output_tokens": repair_output_tokens_by_schema.get(schema, 0),
            "self_repair_success_rate": round(successes / clean, 3)
            if clean >= _SCHEMA_MIN_REPAIR_ATTEMPTS
            else None,
        }

    by_schema = {schema: _schema_entry(schema) for schema in sorted(all_schemas)}
    # Exclude crashed reopens (infra failures) from the denominator so the rate
    # reflects genuine format repair quality, not infra noise.  For historical
    # tasks that predate infra-failure reporting, repair_infra_failures == 0 and
    # the value is identical to the previous formula — backwards-compatible.
    clean_attempts = repair_attempts - repair_infra_failures
    # LLM cost estimate for repair calls using Sonnet 4.6 rates (3e-6 input,
    # 15e-6 output per token) as the operational default. Tasks from older agents
    # that predate token tracking contribute 0 tokens and therefore 0 cost —
    # backwards-compatible. None when no repair attempts occurred.
    _REPAIR_INPUT_RATE = 3e-6
    _REPAIR_OUTPUT_RATE = 15e-6
    repair_llm_usd: float | None = None
    if repair_attempts > 0:
        repair_llm_usd = round(
            repair_input_tokens * _REPAIR_INPUT_RATE
            + repair_output_tokens * _REPAIR_OUTPUT_RATE,
            6,
        )
    tokens_per_repair_attempt: float | None = (
        round((repair_input_tokens + repair_output_tokens) / repair_attempts, 1)
        if repair_attempts > 0
        else None
    )
    return {
        "tasks_measured": measured,
        "well_formed_tasks": well_formed,
        # Per-case well-formed rate (Aider's percent_cases_well_formed): the
        # share of measured tasks that produced no unparseable final output.
        "format_compliance_rate": round(well_formed / measured, 3)
        if measured
        else None,
        "format_error_total": error_total,
        "repair_attempts": repair_attempts,
        "repair_successes": repair_successes,
        "repair_infra_failures": repair_infra_failures,
        "repair_input_tokens": repair_input_tokens,
        "repair_output_tokens": repair_output_tokens,
        "repair_llm_usd": repair_llm_usd,
        "tokens_per_repair_attempt": tokens_per_repair_attempt,
        # Share of self-repair re-asks that produced a parseable output, with
        # crashed reopens (infra failures) excluded from the denominator.
        # None (not 0) when the clean denominator is <= 0.
        "self_repair_success_rate": round(repair_successes / clean_attempts, 3)
        if clean_attempts > 0
        else None,
        "by_schema": by_schema,
        "format_miss_by_reason": dict(miss_by_reason),
        "format_repair_miss_by_reason": dict(repair_miss_by_reason),
        "format_miss_by_schema_and_reason": {
            schema: dict(reasons) for schema, reasons in miss_by_schema_and_reason.items()
        },
    }


def _format_compliance_trend(
    curr_rate: float | None, prev_rate: float | None
) -> str | None:
    """Compute improving/declining/stable trend using ±0.05 threshold (mirrors _compute_first_attempt_breakdowns)."""
    if curr_rate is None or prev_rate is None:
        return None
    delta = curr_rate - prev_rate
    if delta > 0.05:
        return "improving"
    if delta < -0.05:
        return "declining"
    return "stable"


def compute_format_compliance_metrics(
    terminal_tasks: List[Task],
    prev_terminal_tasks: List[Task] | None = None,
) -> Dict[str, Any]:
    """Windowed structured-output format-compliance + self-repair-success measure.

    Pure function — no database calls. Aggregates the per-task
    ``format_compliance_summary`` JSONB (written by the ``/heartbeat`` endpoint
    from the agent's structured-output extraction-layer telemetry) into a
    windowed pipeline measure, overall and grouped by task type. The caller
    passes a pre-windowed terminal-task list, mirroring
    ``_compute_first_attempt_breakdowns`` / ``compute_failure_grouping``.

    The measure reports two rates that Aider's edit leaderboard treats as
    SEPARATE axes from task success (knowledge doc 5c6a2b4c — "evaluate format
    adherence and task success as separate axes"; percent_cases_well_formed):
      * ``format_compliance_rate`` — share of measured tasks whose structured
        output was well-formed (``format_error_total == 0``), i.e. no unparseable
        final output was observed.
      * ``self_repair_success_rate`` — share of self-repair attempts that then
        produced a parseable output (Σ successes / Σ attempts) (knowledge doc
        6f14116c — format-error self-correction).

    The counts come straight from the extraction layer's own parse verdict
    (ground truth), so neither rate is gameable by the agent's self-report.
    Tasks with a NULL/missing ``format_compliance_summary`` are not counted.

    When ``prev_terminal_tasks`` is supplied (the immediately prior window),
    each block gains ``trend`` (improving / declining / stable / None),
    ``prev_format_compliance_rate``, and ``prev_self_repair_success_rate``
    alongside the existing snapshot keys, mirroring the before/after shape of
    ``_compute_first_attempt_breakdowns``.

    Returns ``{"overall": <block>, "by_type": {<task type>: <block>}}`` where
    each block is the dict produced by ``_accumulate_format_compliance``.
    """
    overall = _accumulate_format_compliance(terminal_tasks)
    by_type_groups: Dict[str, list] = {}
    for t in terminal_tasks:
        if not isinstance(getattr(t, "format_compliance_summary", None), dict):
            continue
        task_type = (t.first_payload or {}).get("type", "create")
        by_type_groups.setdefault(task_type, []).append(t)
    by_type = {
        task_type: _accumulate_format_compliance(tasks)
        for task_type, tasks in by_type_groups.items()
    }
    # Suppress self_repair_success_rate on per-type blocks with fewer than the
    # minimum clean attempts — small-volume task types (e.g. strategic_review with
    # ~2 repairs/window) produce phantom regression deltas that are pure noise.
    for _block in by_type.values():
        _clean = (_block.get("repair_attempts") or 0) - (
            _block.get("repair_infra_failures") or 0
        )
        if _clean < _SELF_REPAIR_MIN_ATTEMPTS:
            _block["self_repair_success_rate"] = None

    if prev_terminal_tasks is not None:
        prev_overall = _accumulate_format_compliance(prev_terminal_tasks)
        overall["prev_format_compliance_rate"] = prev_overall["format_compliance_rate"]
        overall["prev_self_repair_success_rate"] = prev_overall[
            "self_repair_success_rate"
        ]
        overall["trend"] = _format_compliance_trend(
            overall["format_compliance_rate"], prev_overall["format_compliance_rate"]
        )

        prev_by_type_groups: Dict[str, list] = {}
        for t in prev_terminal_tasks:
            if not isinstance(getattr(t, "format_compliance_summary", None), dict):
                continue
            task_type = (t.first_payload or {}).get("type", "create")
            prev_by_type_groups.setdefault(task_type, []).append(t)

        for task_type in set(by_type) | set(prev_by_type_groups):
            curr_block = by_type.get(task_type) or _accumulate_format_compliance([])
            prev_block = _accumulate_format_compliance(
                prev_by_type_groups.get(task_type) or []
            )
            curr_block["prev_format_compliance_rate"] = prev_block[
                "format_compliance_rate"
            ]
            curr_block["prev_self_repair_success_rate"] = prev_block[
                "self_repair_success_rate"
            ]
            curr_block["trend"] = _format_compliance_trend(
                curr_block["format_compliance_rate"],
                prev_block["format_compliance_rate"],
            )
            by_type[task_type] = curr_block

    return {"overall": overall, "by_type": by_type}


def _compute_first_review_approval_breakdowns(
    terminal_tasks: List[Task],
) -> Dict[str, Any]:
    """Compute first-review approval rate with playbook/measurement/type/scope breakdowns.

    A task is 'review-eligible' if it is a create/refactor type with pr_merged=True.
    A task is 'approved on first review' if _first_attempt_rejection_categories is absent.
    """

    def _rate(tasks):
        eligible = [
            t
            for t in tasks
            if (t.first_payload or {}).get("type", "create") in _REVIEW_ELIGIBLE_TYPES
            and has_landed_signal(t)
        ]
        if not eligible:
            return None
        approved = sum(
            1
            for t in eligible
            if "_first_attempt_rejection_categories" not in (t.context or {})
        )
        return round(approved / len(eligible), 2)

    by_type: Dict[str, list] = {}
    by_scope: Dict[str, list] = {}
    for t in terminal_tasks:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type.setdefault(task_type, []).append(t)
        scope = (t.context or {}).get("scope", "unknown")
        by_scope.setdefault(scope, []).append(t)

    fra_by_type: Dict[str, float] = {}
    for task_type, tasks in by_type.items():
        r = _rate(tasks)
        if r is not None:
            fra_by_type[task_type] = r
    fra_by_scope: Dict[str, float] = {}
    for scope, tasks in by_scope.items():
        r = _rate(tasks)
        if r is not None:
            fra_by_scope[scope] = r

    with_pb = [
        t for t in terminal_tasks if (t.context or {}).get("_had_playbook") is True
    ]
    without_pb = [
        t for t in terminal_tasks if (t.context or {}).get("_had_playbook") is False
    ]
    with_m = [
        t
        for t in terminal_tasks
        if (t.context or {}).get("_had_review_measurements") is True
    ]
    without_m = [
        t
        for t in terminal_tasks
        if (t.context or {}).get("_had_review_measurements") is False
    ]

    eligible_merged = [
        t
        for t in terminal_tasks
        if (t.first_payload or {}).get("type", "create") in _REVIEW_ELIGIBLE_TYPES
        and has_landed_signal(t)
    ]

    return {
        "overall": _rate(terminal_tasks),
        "sample_size": len(eligible_merged),
        "by_type": fra_by_type,
        "by_scope": fra_by_scope,
        "with_playbook": _rate(with_pb),
        "without_playbook": _rate(without_pb),
        "with_measurements": _rate(with_m),
        "without_measurements": _rate(without_m),
    }


def compute_failure_grouping(
    *,
    failed_tasks: List[Task],
    error_log_map: Dict[str, Any],
    dir_id_to_text: Dict[str, str],
    window_start: "datetime",
    now: "datetime",
) -> Dict[str, Any]:
    """Group failed tasks by failure class with cost/stage/waste breakdowns.

    Pure function — no database calls. Returns a dict with:
      failure_groups, cost_by_failure_class, total_failed_cost_usd,
      provision_waste_cost_usd, failure_stage_counts,
      analysis_overflow_summary, provision_waste_by_direction.
    """
    from app.failure_classification import (
        classify_failure,
        classify_failure_taxonomy,
        classify_timeout_subtype,
    )
    from app.failure_stage import _derive_failure_stage
    from app.routers.admin_analytics import (
        ON_DEMAND_RATE,
        SPOT_RATE,
        _derive_completed_at,
    )

    _failure_class_groups: Dict[str, List[Dict[str, Any]]] = {}
    _failure_stage_counts: Dict[str, int] = {}
    _cost_by_failure_class: Dict[str, float] = {}
    _provision_waste_cost = 0.0
    _provision_waste_by_dir: Dict[str, Dict[str, Any]] = {}

    for t in failed_tasks:
        tid = str(t.id)
        cls = classify_failure(
            t, error_log_map.get(tid), at_time=t.last_heartbeat or t.started_at
        )
        _task_cost = 0.0
        if t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start:
            _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            _task_cost = _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            _cost_by_failure_class[cls] = (
                _cost_by_failure_class.get(cls, 0.0) + _task_cost
            )
        if cls == "PROVISION_FAILURE":
            _provision_waste_cost += _task_cost
            _pw_did = (t.context or {}).get("direction_id")
            if _pw_did:
                _pw_key = str(_pw_did)
                _pw_entry = _provision_waste_by_dir.setdefault(
                    _pw_key,
                    {
                        "direction_id": _pw_key,
                        "direction_text": dir_id_to_text.get(_pw_key, ""),
                        "provision_cost_usd": 0.0,
                        "provision_task_count": 0,
                    },
                )
                _pw_entry["provision_cost_usd"] += _task_cost
                _pw_entry["provision_task_count"] += 1
        waiting_since = _derive_completed_at(t)
        waiting_hours = (
            round((now - waiting_since).total_seconds() / 3600)
            if waiting_since
            else None
        )
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        _stage = (t.context or {}).get("_previous_failure_stage")
        if not _stage:
            _stage = _derive_failure_stage(
                cls, t.latest_status_message or error_log_map.get(tid) or ""
            )
        if _stage:
            _failure_stage_counts[_stage] = _failure_stage_counts.get(_stage, 0) + 1
        _failure_class_groups.setdefault(cls, []).append(
            {
                "task_id": tid,
                "title": t.title or "Failed task",
                "error_snippet": error_log_map.get(tid),
                "attempt_count": t.attempt_count,
                "waiting_hours": waiting_hours,
                "taxonomy": classify_failure_taxonomy(t),
                "timeout_subtype": classify_timeout_subtype(t, error_log_map.get(tid))
                if cls == AGENT_TIMEOUT
                else None,
                "direction_id": t_dir_id,
                "direction_text": t_dir_text,
                "failure_stage": _stage,
            }
        )

    failure_groups = sorted(
        [
            {
                "failure_class": cls,
                "label": _FAILURE_CLASS_LABELS.get(cls, cls),
                "count": len(tasks),
                "tasks": tasks,
            }
            for cls, tasks in _failure_class_groups.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )
    cost_by_failure_class = {k: round(v, 4) for k, v in _cost_by_failure_class.items()}
    total_failed_cost_usd = round(sum(_cost_by_failure_class.values()), 4)
    provision_waste_cost_usd = round(_provision_waste_cost, 4)

    _analysis_overflow_count = 0
    _analysis_overflow_cost = 0.0
    _analysis_overflow_by_scope: Dict[str, int] = {}
    for t in failed_tasks:
        _ctx = t.context or {}
        if _ctx.get("scope_gate_triggered") == "analysis_overflow":
            _analysis_overflow_count += 1
            _scope = _ctx.get("scope") or "unknown"
            _analysis_overflow_by_scope[_scope] = (
                _analysis_overflow_by_scope.get(_scope, 0) + 1
            )
            if (
                t.vm_provisioned_at
                and t.vm_stopped_at
                and t.vm_stopped_at >= window_start
            ):
                _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                _analysis_overflow_cost += (
                    _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
                )
    analysis_overflow_summary = {
        "total_count": _analysis_overflow_count,
        "by_scope": _analysis_overflow_by_scope,
        "cost_usd": round(_analysis_overflow_cost, 4),
    }
    provision_waste_by_direction = sorted(
        [
            {**e, "provision_cost_usd": round(e["provision_cost_usd"], 4)}
            for e in _provision_waste_by_dir.values()
        ],
        key=lambda x: x["provision_cost_usd"],
        reverse=True,
    )[:5]

    return {
        "failure_groups": failure_groups,
        "cost_by_failure_class": cost_by_failure_class,
        "total_failed_cost_usd": total_failed_cost_usd,
        "provision_waste_cost_usd": provision_waste_cost_usd,
        "failure_stage_counts": _failure_stage_counts,
        "analysis_overflow_summary": analysis_overflow_summary,
        "provision_waste_by_direction": provision_waste_by_direction,
    }


def compute_recurring_error_clusters(
    *,
    failed_tasks: List[Task],
    error_log_map: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Group failed tasks by normalized error fingerprint.

    Pure function — no database calls. Returns up to 5 clusters with count >= 2,
    sorted by count descending. Each cluster aggregates direction_ids and
    task_types across the constituent failed tasks.
    """
    from app.anomaly_detection import _error_fingerprint

    _clusters: Dict[str, Dict[str, Any]] = {}
    for _t in failed_tasks:
        _tid = str(_t.id)
        _msg = error_log_map.get(_tid) or (_t.latest_status_message or "")
        if not _msg:
            continue
        _fp = _error_fingerprint(_msg)
        _entry = _clusters.get(_fp)
        if _entry is None:
            _entry = {
                "fingerprint": _fp,
                "count": 0,
                "error_snippet": _msg[:200],
                "direction_ids": set(),
                "task_types": set(),
                "first_task_id": _tid,
            }
            _clusters[_fp] = _entry
        _entry["count"] += 1
        _dir_id = (_t.context or {}).get("direction_id")
        if _dir_id:
            _entry["direction_ids"].add(str(_dir_id))
        _ttype = (_t.first_payload or {}).get("type")
        if _ttype:
            _entry["task_types"].add(str(_ttype))
    return [
        {
            "fingerprint": c["fingerprint"],
            "count": c["count"],
            "error_snippet": c["error_snippet"],
            "direction_ids": sorted(c["direction_ids"]),
            "task_types": sorted(c["task_types"]),
            "first_task_id": c["first_task_id"],
        }
        for c in sorted(_clusters.values(), key=lambda x: x["count"], reverse=True)
        if c["count"] >= 2
    ][:5]


def compute_retry_pattern_summary(failed_tasks: List[Task]) -> Dict[str, Any]:
    """Summarize multi-attempt failures and whether retries shifted failure class.

    Pure function — no database calls. Looks at each failed task's
    ``_failure_class_history`` chain; entries with ≥2 attempts contribute
    to ``total_multi_attempt_failures``. ``repeat_class_count`` counts how
    many such chains had two consecutive identical classes.
    """
    from app.retry_budget import failure_class_history_classes

    _multi_attempt = []
    for _t in failed_tasks:
        _hist = failure_class_history_classes(
            (_t.context or {}).get("_failure_class_history")
        )
        if len(_hist) >= 2:
            _multi_attempt.append(_hist)
    _repeat_count = sum(
        1 for h in _multi_attempt if any(h[i] == h[i + 1] for i in range(len(h) - 1))
    )
    _pattern_counts: Dict[str, int] = {}
    for _hist in _multi_attempt:
        _key = " -> ".join(_hist)
        _pattern_counts[_key] = _pattern_counts.get(_key, 0) + 1
    _top_patterns = sorted(_pattern_counts.items(), key=lambda x: x[1], reverse=True)[
        :3
    ]
    return {
        "total_multi_attempt_failures": len(_multi_attempt),
        "repeat_class_count": _repeat_count,
        "top_patterns": [{"sequence": s, "count": c} for s, c in _top_patterns],
    }


def compute_review_cap_exhaustion_summary(
    failed_tasks: List[Task],
    *,
    window_start: "datetime",
) -> Dict[str, Any]:
    """Summarize tasks that FAILED with failure_detail=review_cap_exhausted.

    Pure function — no database calls. Aggregates iteration counts, top
    directions, and VM cost burned while the review-fix loop was unable
    to converge.
    """
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    _cap_tasks = [
        t
        for t in failed_tasks
        if (t.context or {}).get("failure_detail") == "review_cap_exhausted"
    ]
    _cap_iters: List[int] = []
    _cap_dir_counts: Dict[str, int] = {}
    _cap_total_cost = 0.0
    for _t in _cap_tasks:
        _ctx = _t.context or {}
        _it = _ctx.get("review_cap_iterations")
        if isinstance(_it, (int, float)):
            _cap_iters.append(int(_it))
        _did = _ctx.get("direction_id")
        if _did:
            _did_s = str(_did)
            _cap_dir_counts[_did_s] = _cap_dir_counts.get(_did_s, 0) + 1
        if (
            _t.vm_provisioned_at
            and _t.vm_stopped_at
            and _t.vm_stopped_at >= window_start
        ):
            _rate = SPOT_RATE if _t.vm_spot else ON_DEMAND_RATE
            _cap_total_cost += (
                _rate * (_t.vm_stopped_at - _t.vm_provisioned_at).total_seconds()
            )
    _top_cap_dirs = sorted(_cap_dir_counts.items(), key=lambda x: x[1], reverse=True)[
        :3
    ]
    return {
        "count": len(_cap_tasks),
        "avg_iterations": (sum(_cap_iters) / len(_cap_iters)) if _cap_iters else None,
        "top_directions": [{"direction_id": d, "count": c} for d, c in _top_cap_dirs],
        "total_cost_usd": round(_cap_total_cost, 4),
        "avg_cost_usd": round(_cap_total_cost / len(_cap_tasks), 4)
        if _cap_tasks
        else None,
    }


def compute_outer_loop_convergence(
    *,
    all_tasks: List[Task],
    escalation_alerts: List[Alert],
    window_start: "datetime",
) -> Dict[str, Any]:
    """Aggregate OUTER review<->fix PR-cycle convergence health.

    Pure function — no database calls. The INNER per-task lint/test/check loop is
    aggregated by ``get_fix_loop_convergence``; this rolls up the OUTER PR cycle
    (reviewer finds issues -> fixer addresses -> reviewer re-reviews) recorded
    per-PR in ``task.context["review_loop"]`` plus the early-escalation operator
    Alerts written by ``alert_dispatch._maybe_create_escalation_alert``.

    Rolls up, over tasks created within the window:
      - review<->fix cycles-to-merge distribution (iteration median/p95 + buckets)
      - issue-churn detection totals and the number of PRs that saw churn
      - drift-signal presence (files leaking in/out across iterations)
      - terminal outcomes: merged, rejected, cap-exhausted, repeat-fingerprint halt
      - early-escalation Alert counts by reason (stuck / standoff / repeat-fingerprint)

    ``convergence_rate`` = merged / (merged + rejected + escalations that have not
    yet reached a merged/rejected terminal), i.e. the fraction of decided-or-stuck
    outer loops that actually merged. Returns zeros/None on an empty window.

    Aggregation is PER PR-CHAIN, not per review-loop Task. Each review/fix cycle
    spawns a new Task (review_pipeline._spawn_pipeline_task) carrying review_loop
    with an incremented ``iteration`` and a shared ``original_task_id``/``pr_number``,
    so a single k-cycle PR yields k in-window tasks. We group those tasks by chain
    key (see ``_outer_loop_chain_key``) and reduce each chain to ONE observation:
    ``max(iteration)`` as cycles-to-merge and a single terminal outcome. The
    terminal flag is read from the chain's tasks AND the chain-original task
    (``pr_merged`` is stamped on the original by ci_completion, which does NOT
    carry review_loop), so merges are attributed even though the original is
    absent from ``loop_tasks``.
    """
    tasks_by_id, chains, task_id_to_chain = _group_outer_loop_chains(
        all_tasks, window_start
    )

    iterations: List[int] = []
    iteration_distribution = {"1": 0, "2": 0, "3": 0, "4+": 0}
    churn_total = 0
    churn_pr_count = 0
    drift_pr_count = 0
    merged_chains: set = set()
    rejected_chains: set = set()
    cap_exhausted = 0
    repeat_fingerprint_halt = 0
    terminal_outcome_histogram: Dict[str, int] = {
        "merged": 0,
        "rejected": 0,
        "cap_exhausted": 0,
        "stuck": 0,
        "standoff": 0,
        "repeat_fingerprint_halt": 0,
    }
    stuck_pr_count = 0
    standoff_pr_count = 0
    repeat_fingerprint_pr_count = 0
    overall_escalation_pr_count = 0

    # Parse escalation Alerts into per-chain reason sets + the deduped histogram.
    (
        alert_reasons_by_chain,
        escalation_by_reason,
        escalated_task_ids,
    ) = _scan_escalation_alerts(escalation_alerts, task_id_to_chain)
    escalated_chain_keys: set = set()

    # Per-non-converged-chain candidate-cause tallies, used to rank the single
    # dominant non-convergence blocker. Each non-converged chain (outcome !=
    # "merged") contributes to every cause it exhibits, so shares are normalised
    # on ONE common base (non-converged PR-chains) rather than comparing the raw
    # counts directly — the escalation-reason signals are reason-occurrences on a
    # different base than the per-chain churn/drift/outcome counts.
    non_converged_total = 0
    blocker_cause_counts: Dict[str, int] = {
        "churn": 0,
        "drift": 0,
        "stuck": 0,
        "standoff": 0,
        "repeat_fingerprint_halt": 0,
        "cap_exhausted": 0,
    }

    for key, chain_tasks in chains.items():
        obs = _outer_loop_chain_observation(
            key, chain_tasks, tasks_by_id, alert_reasons_by_chain.get(key, set())
        )

        max_iteration = obs["max_iteration"]
        if max_iteration is not None:
            iterations.append(max_iteration)
            iteration_distribution[
                "4+" if max_iteration >= 4 else str(max_iteration)
            ] += 1
        chain_churn = obs["churn_count"]
        if chain_churn > 0:
            churn_total += chain_churn
            churn_pr_count += 1
        if obs["has_drift"]:
            drift_pr_count += 1

        outcome = obs["outcome"]
        if outcome in terminal_outcome_histogram:
            terminal_outcome_histogram[outcome] += 1
        if outcome == "merged":
            merged_chains.add(key)
        elif outcome == "rejected":
            rejected_chains.add(key)
        elif outcome == "cap_exhausted":
            cap_exhausted += 1
            rejected_chains.add(key)
        elif outcome == "repeat_fingerprint_halt":
            repeat_fingerprint_halt += 1
        if outcome == "stuck":
            stuck_pr_count += 1
        if outcome == "standoff":
            standoff_pr_count += 1
        if outcome == "repeat_fingerprint_halt":
            repeat_fingerprint_pr_count += 1
        if outcome in _ESCALATION_OUTCOME_KEYS:
            overall_escalation_pr_count += 1
            escalated_chain_keys.add(key)

        # Tally candidate non-convergence causes for the dominant-blocker ranking.
        # A chain is non-converged when it did NOT merge; it contributes to every
        # cause it exhibits (a chain can churn AND drift, etc.).
        if outcome != "merged":
            non_converged_total += 1
            if chain_churn > 0:
                blocker_cause_counts["churn"] += 1
            if obs["has_drift"]:
                blocker_cause_counts["drift"] += 1
            if outcome in blocker_cause_counts:
                blocker_cause_counts[outcome] += 1

    # Outer-loops still escalated but not yet merged/rejected count as
    # non-convergence in the denominator (oscillating / stuck awaiting a human).
    # Map escalated tasks to their chain so an unresolved escalation counts once
    # per chain, not once per iteration-task.
    escalated_chains: set = set()
    for tid in escalated_task_ids:
        escalated_chains.add(task_id_to_chain.get(tid, f"task:{tid}"))
    escalated_chains.update(escalated_chain_keys)
    escalated_unresolved = escalated_chains - merged_chains - rejected_chains
    decided = len(merged_chains) + len(rejected_chains) + len(escalated_unresolved)
    convergence_rate = round(len(merged_chains) / decided, 3) if decided else None

    # Rank the candidate causes and name a single dominant blocker only when one
    # cause clearly dominates: it must be present in >=2 non-converged chains,
    # cover >=50% of them, AND strictly outrank the runner-up (an even split is
    # "no clear dominant"). Mirrors the dominant-failure-class gating in
    # _build_digest_narrative; emits None on empty/ambiguous windows.
    dominant_blocker: Optional[Dict[str, Any]] = None
    if non_converged_total > 0:
        ranked = sorted(blocker_cause_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top_cause, top_count = ranked[0]
        second_count = ranked[1][1] if len(ranked) > 1 else 0
        if (
            top_count >= 2
            and top_count / non_converged_total >= 0.5
            and top_count > second_count
        ):
            dominant_blocker = {
                "cause": top_cause,
                "count": top_count,
                "total": non_converged_total,
                "share": round(top_count / non_converged_total, 3),
            }

    return {
        "prs_with_review_loop": len(chains),
        "iteration_median": _outer_loop_pct(iterations, 50),
        "iteration_p95": _outer_loop_pct(iterations, 95),
        "iteration_distribution": iteration_distribution,
        "churn_total": churn_total,
        "churn_pr_count": churn_pr_count,
        "drift_pr_count": drift_pr_count,
        "outcomes": {
            "merged": len(merged_chains),
            "rejected": len(rejected_chains),
            "cap_exhausted": cap_exhausted,
            "repeat_fingerprint_halt": repeat_fingerprint_halt,
        },
        "terminal_outcome_histogram": terminal_outcome_histogram,
        "incidence_counts": {
            "churn": churn_pr_count,
            "stuck": stuck_pr_count,
            "standoff": standoff_pr_count,
            "repeat_fingerprint": repeat_fingerprint_pr_count,
            "overall_escalations": overall_escalation_pr_count,
        },
        "incidence_rates": {
            "churn": round(churn_pr_count / len(chains), 3) if chains else None,
            "stuck": round(stuck_pr_count / len(chains), 3) if chains else None,
            "standoff": round(standoff_pr_count / len(chains), 3) if chains else None,
            "repeat_fingerprint": round(repeat_fingerprint_pr_count / len(chains), 3)
            if chains
            else None,
            "overall_escalations": round(overall_escalation_pr_count / len(chains), 3)
            if chains
            else None,
        },
        "escalation_total": len(escalated_task_ids),
        "escalation_by_reason": escalation_by_reason,
        "convergence_rate": convergence_rate,
        "non_converged_total": non_converged_total,
        "dominant_blocker": dominant_blocker,
        # Non-code outcomes — ADDITIVE. Tasks without a review_loop context
        # (research, audit, memo, verify_tests, etc.) are not part of the PR
        # review chain and are absent from convergence_rate. This sub-dict
        # surfaces their generalized outcome so they are visible alongside the
        # PR-chain metrics rather than silently excluded from the digest view.
        "non_code_outcomes": _compute_non_code_outcomes(all_tasks, window_start),
    }


def _compute_non_code_outcomes(
    all_tasks: List[Task], window_start: "datetime"
) -> Dict[str, Any]:
    """Summarize terminal outcomes of tasks that are NOT part of PR review chains.

    Uses evaluate_task_outcome so each task type contributes its own honest
    success signal rather than appearing as a zero-output in the PR-based view.
    Window is determined by terminal/completion time, not created_at: a task
    created before the window but completed within it is included; a task created
    within the window but completed before it is excluded.
    Returns an empty dict when no qualifying tasks exist in the window.
    """
    from app.task_outcomes import evaluate_task_outcome  # noqa: PLC0415
    from app.routers.admin_analytics import _derive_completed_at

    success = 0
    neutral = 0
    failure = 0
    by_kind: Dict[str, Dict[str, int]] = {}
    for t in all_tasks:
        # Only terminal tasks have an honest evaluation outcome — in-flight
        # (RUNNING/PENDING/BLOCKED) tasks would otherwise be scored 'neutral'
        # and inflate total/neutrals. Mirrors the terminal filter in
        # direction_outcomes._update_direction_research_outcomes.
        if t.status not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            continue
        if isinstance((t.context or {}).get("review_loop"), dict):
            continue
        completed_at = _derive_completed_at(t)
        if completed_at < window_start:
            continue
        outcome = evaluate_task_outcome(t)
        kind = outcome.kind
        if kind not in by_kind:
            by_kind[kind] = {"success": 0, "neutral": 0, "failure": 0}
        by_kind[kind][outcome.status] += 1
        if outcome.status == "success":
            success += 1
        elif outcome.status == "failure":
            failure += 1
        else:
            neutral += 1
    total = success + neutral + failure
    if total == 0:
        return {}
    result: Dict[str, Any] = {
        "total": total,
        "successes": success,
        "neutrals": neutral,
        "failures": failure,
    }
    if by_kind:
        result["by_kind"] = by_kind
    return result


def compute_resurrection_outcome_summary(
    completed_tasks: List[Task],
    failed_tasks: List[Task],
) -> Dict[str, Any]:
    """Count tasks that the reaper auto-resurrected and how many still failed.

    Pure function — no database calls. A task is considered resurrected when
    its context's ``_resurrection_count`` is ≥1.
    """
    _resurrected_completed = 0
    _resurrected_failed = 0
    for _t in completed_tasks:
        if (_t.context or {}).get("_resurrection_count", 0) >= 1:
            _resurrected_completed += 1
    for _t in failed_tasks:
        if (_t.context or {}).get("_resurrection_count", 0) >= 1:
            _resurrected_failed += 1
    _resurrected_total = _resurrected_completed + _resurrected_failed
    return {
        "total": _resurrected_total,
        "completed": _resurrected_completed,
        "failed_again": _resurrected_failed,
        "success_rate": (_resurrected_completed / _resurrected_total)
        if _resurrected_total
        else None,
    }


def compute_rejection_pattern_summary(
    all_tasks: List[Task],
    *,
    window_start: "datetime",
) -> Dict[str, Any]:
    """Aggregate ``pr_rejected`` tasks completed within the window.

    Pure function — no database calls. Normalises rejection notes (lower-cased,
    whitespace-collapsed, truncated to 120 chars) so near-duplicate notes count
    together; returns the top three by frequency.
    """
    from app.routers.admin_analytics import _derive_completed_at

    _rejected_in_window = [
        t
        for t in all_tasks
        if (t.context or {}).get("pr_rejected")
        and _derive_completed_at(t) >= window_start
    ]
    _note_counts: Dict[str, int] = {}
    for _t in _rejected_in_window:
        _note = ((_t.context or {}).get("pr_rejection_note") or "").strip()
        if not _note:
            continue
        _snip = " ".join(_note.lower().split())[:120]
        _note_counts[_snip] = _note_counts.get(_snip, 0) + 1
    _top_notes = sorted(_note_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    return {
        "total_rejected": len(_rejected_in_window),
        "top_notes": [{"snippet": s, "count": c} for s, c in _top_notes],
    }


def compute_anomaly_adjustment_summary() -> Dict[str, Any]:
    """Snapshot the in-memory anomaly-driven pipeline adjustment state.

    Reads from ``app.shared_state.anomaly_adjustments`` (an in-process dict
    mutated by the anomaly-detection loop). Pure transform over that dict.
    """
    from app.shared_state import anomaly_adjustments as _anomaly_adjustments

    return {
        "active": bool(_anomaly_adjustments.get("active")),
        "adjusted_at": _anomaly_adjustments.get("adjusted_at"),
        "trigger_anomalies": list(_anomaly_adjustments.get("trigger_anomalies") or []),
        "pre_adjustment_max_vms": _anomaly_adjustments.get("pre_adjustment_max_vms"),
        "adjusted_max_vms": _anomaly_adjustments.get("adjusted_max_vms"),
        "failure_gate_threshold": _anomaly_adjustments.get("failure_gate_threshold"),
        "pre_adjustment_failure_gate": _anomaly_adjustments.get(
            "pre_adjustment_failure_gate"
        ),
        "forced_restoration_count": _anomaly_adjustments.get(
            "forced_restoration_count", 0
        ),
        "forced_restored_at": _anomaly_adjustments.get("forced_restored_at"),
    }


# ── Dependency-health observability (read-only) ────────────────────────────
#
# Two failure modes the foundation-resolution work exists to prevent are
# otherwise invisible to operators and to the pipeline's own metrics:
#   1. PARKED BLOCKERS — a BLOCKED task that outlived its escalation deadline is
#      escalated once (ERROR alert) and then parked by pushing recheck_after far
#      into the future (``reaper_sweeps._BLOCKED_ESCALATED_PARK``), dropping it
#      off the recovery sweep's due-queue until a manual operator requeue. Nothing
#      counts how many tasks sit in this parked state.
#   2. DUPLICATED FOUNDATIONS — the file-conflict filter deliberately lets two
#      write-tasks under DIFFERENT parents touch the same files in parallel and
#      reconcile at merge, so two tasks independently building the same foundation
#      are never flagged.
# Both helpers below are READ-ONLY and observational: they make the patterns
# measurable without changing resume or conflict-resolution behavior.

# Write task types that produce a PR and can build a foundation. Includes both
# SR code-change types (create, fix, refactor, migrate) and other PR-producing types
# (qa, docs, deps) for comprehensive duplicate-foundation detection. SR analytics uses
# _SR_CODE_CHANGE_TYPES (narrower scope); foundation convergence needs the full set.
_WRITE_TASK_TYPES = frozenset(_SR_CODE_CHANGE_TYPES | {"qa", "docs", "deps"})
# Two active write-tasks under DIFFERENT parents whose related-file sets overlap
# by at least this Jaccard ratio are surfaced as a candidate duplicated
# foundation. Deliberately conservative (strong overlap only) so the signal
# flags genuine duplication rather than incidental shared-file edits.
_FOUNDATION_OVERLAP_THRESHOLD = 0.5
# Defensive caps on the surfaced lists so a large backlog never bloats the digest.
_PARKED_BLOCKER_LIST_LIMIT = 100
_DUPLICATE_FOUNDATION_LIST_LIMIT = 50
# Unresumable-cancellation aggregate: how far back to count terminal give-ups,
# and the cap on the surfaced per-task list.
_UNRESUMABLE_CANCEL_WINDOW = timedelta(days=7)
_UNRESUMABLE_CANCEL_LIST_LIMIT = 100
# Live blocked-population aggregate: cap on the surfaced per-task drill-down list,
# and the coarse age buckets (in hours) the waiting population is bucketed into.
_LIVE_BLOCKED_LIST_LIMIT = 100
# Blocked-resume aggregate: how far back to count successful resumes, and the cap
# on the surfaced per-task list. Same window as the symmetric give-up signal.
_BLOCKED_RESUME_WINDOW = timedelta(days=7)
_BLOCKED_RESUME_LIST_LIMIT = 100
# Missing-capability-demand aggregate: cap on the surfaced per-demand list
# (the producer is already largest-demand-first, so this preserves the top tail).
_MISSING_DEMAND_LIST_LIMIT = 50


def _task_related_files(t: "Task") -> set:
    """Best-effort related-file set for a task (context first, then payload)."""
    files = (t.context or {}).get("related_files")
    if not isinstance(files, list):
        files = (t.first_payload or {}).get("related_files")
    if not isinstance(files, list):
        return set()
    return {f for f in files if isinstance(f, str) and f}


def compute_parked_blocker_summary(blocked_tasks: List[Task]) -> Dict[str, Any]:
    """Count and list BLOCKED tasks and base-re-cut-held tasks that are parked.

    Pure function — no database calls. A *parked* item is either:
      * A BLOCKED task whose context carries ``_blocked_escalated`` (set by
        ``reaper_sweeps._escalate_blocked_task`` after an operator escalation alert),
        which stops rechecking and pushes ``recheck_after`` far into the future, OR
      * A task (any status) with ``_recut_escalated`` marker (set by
        ``orchestrator_health._record_recut_failure`` when a cross-base re-cut hits
        conflict threshold), which indicates a persistent base re-cut failure.

    Returns the total count plus a capped list with enough context to find and
    requeue each one. Newest-parked first.
    """
    parked: List[Dict[str, Any]] = []
    for t in blocked_tasks:
        ctx = t.context or {}
        is_blocked_escalated = bool(ctx.get("_blocked_escalated"))
        is_recut_escalated = bool(ctx.get("_recut_escalated"))

        if not (is_blocked_escalated or is_recut_escalated):
            continue

        blocked_at = getattr(t, "blocked_at", None)
        recheck_after = getattr(t, "recheck_after", None)

        # For re-cut holds, use created_at as the "blocked_at" equivalent
        if is_recut_escalated and not blocked_at:
            blocked_at = getattr(t, "created_at", None)

        hold_reason = None
        if is_recut_escalated:
            outcome = ctx.get("_recut_last_outcome", "unknown")
            hold_reason = (
                f"Base re-cut {outcome}" if outcome != "unknown" else "Base re-cut hold"
            )
        elif is_blocked_escalated:
            hold_reason = None  # blocked_reason will be populated from blocker_kind

        parked.append(
            {
                "task_id": str(t.id),
                "title": t.title or str(t.id)[:8],
                "project_id": str(t.project_id)
                if getattr(t, "project_id", None) is not None
                else None,
                "blocker_kind": hold_reason or getattr(t, "blocker_kind", None),
                "blocked_reason": getattr(t, "blocked_reason", None)
                or t.latest_status_message,
                "blocked_at": blocked_at.isoformat() if blocked_at else None,
                "recheck_after": recheck_after.isoformat() if recheck_after else None,
                "direction_id": ctx.get("direction_id"),
            }
        )
    parked.sort(key=lambda r: r["blocked_at"] or "", reverse=True)
    return {"total": len(parked), "tasks": parked[:_PARKED_BLOCKER_LIST_LIMIT]}


def compute_duplicate_foundation_signal(
    active_write_tasks: List[Task],
) -> Dict[str, Any]:
    """Count candidate duplicated foundations among concurrently-active write-tasks.

    Pure function — no database calls. A *candidate* is a pair of active
    code-change tasks under DIFFERENT parents whose related-file sets overlap by at
    least ``_FOUNDATION_OVERLAP_THRESHOLD`` (Jaccard). Same-parent siblings are
    skipped — those are intra-decomposition work gated by explicit ``depends_on``
    edges, not the silent cross-parent duplication this signal surfaces. Returns
    the candidate-pair count plus a capped list (the two task ids, the overlap
    ratio, and the shared files). Observational only — the pipeline still lets the
    tasks proceed and reconcile at merge.
    """
    enriched = [
        (t, _task_related_files(t))
        for t in active_write_tasks
        if (t.first_payload or {}).get("type") in _WRITE_TASK_TYPES
    ]
    enriched = [(t, files) for t, files in enriched if files]

    candidates: List[Dict[str, Any]] = []
    for i in range(len(enriched)):
        ti, fi = enriched[i]
        pi = getattr(ti, "parent_task_id", None)
        for j in range(i + 1, len(enriched)):
            tj, fj = enriched[j]
            pj = getattr(tj, "parent_task_id", None)
            # Different parents only — same non-None parent reconciles by design.
            if pi is not None and pi == pj:
                continue
            inter = fi & fj
            if not inter:
                continue
            union = fi | fj
            overlap = len(inter) / len(union)
            if overlap < _FOUNDATION_OVERLAP_THRESHOLD:
                continue
            candidates.append(
                {
                    "task_ids": [str(ti.id), str(tj.id)],
                    "overlap_ratio": round(overlap, 3),
                    "shared_files": sorted(inter),
                }
            )
    candidates.sort(key=lambda c: c["overlap_ratio"], reverse=True)
    return {
        "total": len(candidates),
        "candidates": candidates[:_DUPLICATE_FOUNDATION_LIST_LIMIT],
    }


def compute_unresumable_cancellation_summary(
    cancelled_tasks: List["Task"],
    *,
    window_start: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Count blocked dependents terminally CANCELLED because they can never resume.

    Pure function — no database calls. Reads the durable
    ``reaper_sweeps.UNRESUMABLE_CANCEL_CONTEXT_KEY`` marker stamped by
    ``_cancel_blocker_as_dead`` when the resume system gives up on a blocked
    dependent (its foundation died, or its need outlived the bounded parked
    re-evaluations). This give-up IS the failure mode of structural gating, so its
    rate is the single best health signal — a spike (e.g. from a resume-matcher
    regression stranding finding-hash/capability needs) is otherwise invisible in
    aggregate.

    Tasks with no marker are skipped. When ``window_start`` is given, a marker
    whose ``at`` timestamp is older (or unparseable) is skipped, so the count
    reflects only a recent window. Returns the total plus breakdowns by give-up
    class and by originating direction, and a capped newest-first task list.
    """
    from app.reaper_sweeps import UNRESUMABLE_CANCEL_CONTEXT_KEY

    rows: List[Dict[str, Any]] = []
    by_class: Dict[str, int] = defaultdict(int)
    by_direction: Dict[str, int] = defaultdict(int)
    window_iso = window_start.isoformat() if window_start else None

    for t in cancelled_tasks:
        marker = (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
        if not isinstance(marker, dict):
            continue
        at = marker.get("at")
        # ISO-8601 strings from a common naive-UTC source sort lexicographically,
        # so a string compare is a correct, cheap window filter.
        if window_iso and (not isinstance(at, str) or at < window_iso):
            continue
        give_up_class = marker.get("give_up_class") or "unknown"
        direction_id = marker.get("direction_id") or "(none)"
        by_class[give_up_class] += 1
        by_direction[direction_id] += 1
        rows.append(
            {
                "task_id": str(t.id),
                "title": t.title or str(t.id)[:8],
                "project_id": str(t.project_id)
                if getattr(t, "project_id", None) is not None
                else None,
                "give_up_class": give_up_class,
                "need": marker.get("need"),
                "direction_id": marker.get("direction_id"),
                "at": at,
            }
        )

    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return {
        "total": len(rows),
        "by_class": dict(by_class),
        "by_direction": dict(by_direction),
        "tasks": rows[:_UNRESUMABLE_CANCEL_LIST_LIMIT],
    }


def compute_live_blocked_summary(blocked_tasks: List["Task"]) -> Dict[str, Any]:
    """Summarize the CURRENT BLOCKED population — the healthy waiting set.

    Pure function — no database calls. Unlike ``compute_parked_blocker_summary``
    (which deliberately counts ONLY parked/escalated blockers — the unhealthy tail),
    this counts EVERY currently-BLOCKED task, including not-yet-escalated dependents
    that are healthily waiting for a foundation to land. A regression where resumes
    stall shows up here as a growing population with ageing waits, which the
    parked-only and give-up signals cannot see until escalation.

    Buckets each task by how long it has been blocked (derived from ``blocked_at``):
    ``<1h``, ``1-6h``, ``6-24h``, ``>24h`` (``unknown`` when ``blocked_at`` is
    missing). Returns the total, the bucket histogram, and a capped newest-blocked-
    first task list for drill-down.
    """
    now = _utcnow()
    rows: List[Dict[str, Any]] = []
    by_age: Dict[str, int] = defaultdict(int)
    for t in blocked_tasks:
        blocked_at = getattr(t, "blocked_at", None)
        if blocked_at is None:
            bucket = "unknown"
        else:
            # blocked_at is naive-UTC like _utcnow(); guard tz-aware just in case.
            age_hours = (now - blocked_at).total_seconds() / 3600.0
            if age_hours < 1:
                bucket = "<1h"
            elif age_hours < 6:
                bucket = "1-6h"
            elif age_hours < 24:
                bucket = "6-24h"
            else:
                bucket = ">24h"
        by_age[bucket] += 1
        ctx = t.context or {}
        rows.append(
            {
                "task_id": str(t.id),
                "title": t.title or str(t.id)[:8],
                "project_id": str(t.project_id)
                if getattr(t, "project_id", None) is not None
                else None,
                "blocker_kind": getattr(t, "blocker_kind", None),
                "blocked_reason": getattr(t, "blocked_reason", None)
                or t.latest_status_message,
                "blocked_at": blocked_at.isoformat() if blocked_at else None,
                "age_bucket": bucket,
                "direction_id": ctx.get("direction_id"),
            }
        )
    rows.sort(key=lambda r: r["blocked_at"] or "", reverse=True)
    return {
        "total": len(rows),
        "by_age": dict(by_age),
        "tasks": rows[:_LIVE_BLOCKED_LIST_LIMIT],
    }


def compute_blocked_resume_summary(
    resumed_tasks: List["Task"],
    *,
    window_start: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Count blocked dependents that successfully RESUMED when their foundation landed.

    Pure function — no database calls. The positive, symmetric counterpart to
    ``compute_unresumable_cancellation_summary``: reads the durable
    ``reaper_sweeps.RESUME_MARKER_CONTEXT_KEY`` marker stamped by
    ``_apply_needs_disposition`` when a blocked dependent is requeued (status →
    QUEUED). The transient status message and the in-memory ``requeued`` counter do
    not survive, so without this marker a regression where resumes stall looks
    identical to 'all healthy'.

    Tasks with no marker are skipped. When ``window_start`` is given, a marker whose
    ``at`` timestamp is older (or unparseable) is skipped, so the count reflects only
    a recent window. Returns the total plus a breakdown by ``resolved_by`` (``edge``
    vs ``completed``) and by originating direction, and a capped newest-first task
    list. NOTE: resumed tasks advance past QUEUED through IN_PROGRESS to terminal
    states while keeping the marker, so the DB query that feeds this MUST be
    status-agnostic.
    """
    from app.reaper_sweeps import RESUME_MARKER_CONTEXT_KEY

    rows: List[Dict[str, Any]] = []
    by_resolved_by: Dict[str, int] = defaultdict(int)
    by_direction: Dict[str, int] = defaultdict(int)
    window_iso = window_start.isoformat() if window_start else None

    for t in resumed_tasks:
        marker = (t.context or {}).get(RESUME_MARKER_CONTEXT_KEY)
        if not isinstance(marker, dict):
            continue
        at = marker.get("at")
        # Naive-UTC ISO-8601 strings sort lexicographically — a cheap window filter.
        if window_iso and (not isinstance(at, str) or at < window_iso):
            continue
        resolved_by = marker.get("resolved_by") or "unknown"
        direction_id = marker.get("direction_id") or "(none)"
        by_resolved_by[resolved_by] += 1
        by_direction[direction_id] += 1
        rows.append(
            {
                "task_id": str(t.id),
                "title": t.title or str(t.id)[:8],
                "project_id": str(t.project_id)
                if getattr(t, "project_id", None) is not None
                else None,
                "resolved_by": resolved_by,
                "foundation": marker.get("foundation"),
                "direction_id": marker.get("direction_id"),
                "at": at,
            }
        )

    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return {
        "total": len(rows),
        "by_resolved_by": dict(by_resolved_by),
        "by_direction": dict(by_direction),
        "tasks": rows[:_BLOCKED_RESUME_LIST_LIMIT],
    }


def _compute_finding_structural_give_up_summary(
    research_tasks: List["Task"],
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    by_reason: Dict[str, int] = defaultdict(int)
    by_direction: Dict[str, int] = defaultdict(int)
    for t in research_tasks:
        marker = (t.context or {}).get("finding_structural_give_up")
        if not isinstance(marker, dict):
            continue
        rows.append(
            {
                "task_id": str(t.id),
                "title": t.title or str(t.id)[:8],
                "at": marker.get("at") or _coerce_naive_utc(t.updated_at).isoformat(),
                "sample": list(marker.get("sample") or []),
                "total": _coerce_count(marker.get("total")) or 0,
            }
        )
        for reason, count in (marker.get("by_reason") or {}).items():
            by_reason[reason or "unknown"] += _coerce_count(count) or 0
        for direction_id, count in (marker.get("by_direction") or {}).items():
            by_direction[direction_id or "(none)"] += _coerce_count(count) or 0
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    sample: List[Dict[str, Any]] = []
    for row in rows:
        for item in row["sample"]:
            sample.append({"task_id": row["task_id"], **item})
            if len(sample) >= _FINDING_STRUCTURAL_GIVE_UP_LIST_LIMIT:
                break
        if len(sample) >= _FINDING_STRUCTURAL_GIVE_UP_LIST_LIMIT:
            break
    return {
        "total": sum(row["total"] for row in rows),
        "by_reason": dict(by_reason),
        "by_direction": dict(by_direction),
        "findings": sample,
    }


# Action-confidence threshold for *converging* (serializing) a pair — strictly
# ABOVE the observability threshold. Strong overlap alone is intentionally NOT
# enough: the pipeline lets independent tasks edit shared-infrastructure files in
# parallel and reconcile at merge. We only auto-serialize a pair on a genuine-
# duplicate signal (shared finding origin, shared capability marker, or
# near-identical file sets at this near-1.0 Jaccard), so legitimate parallel work
# is never needlessly serialized.
_FOUNDATION_CONVERGE_THRESHOLD = 0.85


def _genuine_duplicate_reason(ti: "Task", fi: set, tj: "Task", fj: set):
    """Return a short reason if ``(ti, tj)`` are a HIGH-CONFIDENCE genuine
    duplicate foundation, else ``None``. Pure. Any one signal suffices:

      * a shared finding origin (identical non-empty ``finding_hash``),
      * a shared explicit capability marker, or
      * near-identical related-file sets (Jaccard >= the convergence threshold).

    Strong-but-partial file overlap below the convergence threshold is NOT
    genuine — that is the incidental shared-infra case the pipeline reconciles at
    merge — so it returns ``None`` and the pair is left untouched.
    """
    from app.finding_deduplication import _task_finding_hash, _task_capability_markers

    hi, hj = _task_finding_hash(ti), _task_finding_hash(tj)
    if hi and hi == hj:
        return f"shared finding origin {hi}"
    shared_caps = _task_capability_markers(ti) & _task_capability_markers(tj)
    if shared_caps:
        return f"shared capability '{sorted(shared_caps)[0]}'"
    union = fi | fj
    if union and len(fi & fj) / len(union) >= _FOUNDATION_CONVERGE_THRESHOLD:
        return (
            f"near-identical file sets ({round(len(fi & fj) / len(union), 3)} Jaccard)"
        )
    return None


def compute_foundation_convergence_actions(
    active_write_tasks: List[Task],
    *,
    exclude_same_parent: bool = True,
) -> List[Dict[str, Any]]:
    """High-confidence genuine duplicate-foundation pairs to serialize. Pure.

    For each cross-parent pair of active write-tasks that passes
    :func:`_genuine_duplicate_reason`, emit an action that makes the LATER task
    (by ``created_at``, id-tiebroken) depend on the earlier — the canonical
    foundation — so the duplicate rebases on the canonical's landed work and
    builds-on instead of rebuilding. Earlier-is-canonical ordering keeps the edge
    direction deterministic and the wired graph acyclic by construction. The
    caller still applies a self-edge / cycle guard before wiring.

    ``exclude_same_parent`` (default ``True``) skips pairs that share a non-null
    ``parent_task_id`` — coordinated decomposition siblings that reconcile at
    merge. The reaper Step 15 sweep and the observability signal keep this on
    (cross-decomposition tasks have distinct parents). The spawn-time path passes
    ``exclude_same_parent=False``: a just-spawned batch's children all share the
    one research-task parent, so the coarse parent check would mask EVERY
    within-batch genuine duplicate. There the finer "already gated by explicit
    ``depends_on``" exclusion is enforced downstream by the apply-with-guards
    helper instead — an already-linked or cycle-closing edge is refused — so
    coordinated siblings are still left alone while undeclared genuine duplicates
    converge.
    """
    enriched = [
        (t, _task_related_files(t))
        for t in active_write_tasks
        if (t.first_payload or {}).get("type") in _WRITE_TASK_TYPES
    ]

    def _order_key(t: "Task"):
        return (getattr(t, "created_at", None) or datetime.min, str(t.id))

    actions: List[Dict[str, Any]] = []
    for i in range(len(enriched)):
        ti, fi = enriched[i]
        pi = getattr(ti, "parent_task_id", None)
        for j in range(i + 1, len(enriched)):
            tj, fj = enriched[j]
            pj = getattr(tj, "parent_task_id", None)
            if exclude_same_parent and pi is not None and pi == pj:
                continue
            reason = _genuine_duplicate_reason(ti, fi, tj, fj)
            if reason is None:
                continue
            canonical, dependent = sorted((ti, tj), key=_order_key)
            actions.append(
                {
                    "dependent_id": str(dependent.id),
                    "foundation_id": str(canonical.id),
                    "reason": reason,
                    "shared_files": sorted(fi & fj),
                }
            )
    return actions


async def compute_missing_capability_demand_summary(
    session: AsyncSession,
    project_id: "uuid.UUID | None" = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Summarize stranded capability/foundation prerequisites as operator demand.

    Thin reshape over ``capability_demand.collect_missing_capability_demand`` — does
    NOT reimplement the aggregation. The producer scans BLOCKED dependents (unresolved
    foundation needs, ``still_blocked``) and recently give-up-CANCELLED dependents
    retired as missing-foundation (``already_cancelled``), folding both by normalized
    ``(kind, ref)`` identity, largest-demand-first. This stranded demand IS the
    feedback signal the missing-foundation loop acts on; surfacing it next to the
    give-up and resume metrics lets an operator see unmet prerequisites that are about
    to be (or already were) abandoned without a foundation ever being commissioned.

    Returns the distinct-demand ``total``, the ``still_blocked`` vs
    ``already_cancelled`` split summed across demands, a ``by_direction`` count of how
    many demands each originating direction carries, and a bounded ``demands`` sample
    that preserves the producer's largest-first order. Read-only — no DB writes.
    Tolerant of the producer's empty ``{"total": 0, "demands": []}`` shape.
    """
    from app import capability_demand

    demand = await capability_demand.collect_missing_capability_demand(
        session, project_id, now=now
    )
    demands = demand.get("demands") or []

    still_blocked = 0
    already_cancelled = 0
    by_direction: Dict[str, int] = defaultdict(int)
    for entry in demands:
        still_blocked += int(entry.get("still_blocked", 0) or 0)
        already_cancelled += int(entry.get("already_cancelled", 0) or 0)
        for direction_id in entry.get("direction_ids") or []:
            by_direction[str(direction_id)] += 1

    return {
        "total": demand.get("total", len(demands)),
        "still_blocked": still_blocked,
        "already_cancelled": already_cancelled,
        "by_direction": dict(by_direction),
        "demands": demands[:_MISSING_DEMAND_LIST_LIMIT],
    }


def apply_foundation_convergence_action(
    action: Dict[str, Any],
    by_id: Dict[str, "Task"],
    candidates: List["Task"],
    *,
    logger=None,
) -> "Task | None":
    """Wire ONE convergence action's ``depends_on`` edge with the shared guards.

    Looks up the dependent and canonical foundation in ``by_id`` and, applying the
    SAME guards the reaper Step 15 sweep uses, refuses a self-edge, a duplicate
    edge, or any edge that would close a ``depends_on`` cycle
    (``blocked_need_resolution._would_create_cycle`` over ``candidates``). On
    success it appends the edge, records its structural provenance, and stamps the
    dependent's ``_foundation_converged`` context marker so an operator can see why
    the task was serialized. Returns the mutated dependent task when an edge was
    wired (the caller still does ``session.add`` / logging / broadcast), else
    ``None``.

    Both the spawn-time path (``research_completion``) and the reaper Step 15
    sweep (``reaper_sweeps.sweep_converge_duplicate_foundations``) call this, so
    the wiring-and-guards step can never drift from the predicate that emitted the
    action.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.blocked_need_resolution import _would_create_cycle
    from app.depends_on_provenance import remember_structural_depends_on

    dependent = by_id.get(action["dependent_id"])
    foundation = by_id.get(action["foundation_id"])
    if dependent is None or foundation is None:
        return None
    found_id = str(foundation.id)
    if found_id == str(dependent.id):  # defensive self-edge guard
        return None
    existing = [str(d) for d in (dependent.depends_on or [])]
    if found_id in existing:
        return None
    if _would_create_cycle(dependent, foundation, candidates):
        if logger is not None:
            logger.info(
                "[CONVERGE] skipped converging %s -> %s — edge would close a "
                "depends_on cycle",
                dependent.id,
                found_id,
            )
        return None
    dependent.depends_on = existing + [found_id]
    remember_structural_depends_on(dependent, found_id)
    ctx = dependent.context or {}
    ctx["_foundation_converged"] = {
        "foundation_id": found_id,
        "reason": action["reason"],
        "shared_files": action["shared_files"],
    }
    dependent.context = ctx
    flag_modified(dependent, "depends_on")
    flag_modified(dependent, "context")
    return dependent


# A FIRED-but-undelivered registration older than this is treated as a stalled
# delivery: the wake source fired but the leader-owned wake-delivery sweep
# (``reaper_sweeps.sweep_deliver_task_wakes``) never advanced the PARKED task.
# Healthy delivery happens within one reaper cycle, so a registration sitting
# FIRED for many minutes is the signature of a wedged-but-not-crashed sweep.
_PARK_WAKE_DELIVERY_STALL_SECONDS = 15 * 60


def _age_seconds(now: datetime, ts: "datetime | None") -> "int | None":
    """Non-negative whole-second age of ``ts`` relative to ``now`` (naive-UTC safe).

    Returns ``None`` when ``ts`` is missing. Both timestamps are coerced to naive
    UTC so a stray tz-aware value cannot raise on the subtraction.
    """
    if ts is None:
        return None
    return max(
        0,
        int((_coerce_naive_utc(now) - _coerce_naive_utc(ts)).total_seconds()),
    )


async def compute_park_wake_health(
    session: AsyncSession,
    project_id: "uuid.UUID | None" = None,
) -> Dict[str, Any]:
    """Read-only operational health for the task park-and-wake primitive.

    Deliberately DISTINCT from ``parked_blockers`` (which is about escalated
    BLOCKED tasks): this measures the ``TaskStatus.PARKED`` population and the
    ``TaskWakeRegistration`` delivery backlog so v7 requalification can ground
    "park/wake is healthy" in observed runtime state. Never mutates anything;
    runs four small aggregate queries. Scoped to ``project_id`` when provided,
    else fleet-wide.

    Returns a dict with:
      * ``parked_task_count``         — tasks currently in ``TaskStatus.PARKED``
      * ``oldest_parked_age_seconds`` — age of the oldest PARKED task (None if none)
      * ``fired_undelivered_count``   — FIRED registrations the sweep has not yet
                                        delivered (the live delivery backlog)
      * ``oldest_fired_age_seconds``  — age of the oldest FIRED registration (None)
      * ``delivery_stalled``          — True when the oldest FIRED registration has
                                        gone undelivered past the stall threshold
      * ``delivery_stall_threshold_seconds`` — the threshold used, for context
    """
    now = _utcnow()

    parked_filters = [Task.status == TaskStatus.PARKED]
    if project_id is not None:
        parked_filters.append(Task.project_id == project_id)
    parked_task_count = (
        await session.execute(
            select(func.count()).select_from(Task).where(*parked_filters)
        )
    ).scalar() or 0
    oldest_parked_at = (
        await session.execute(select(func.min(Task.parked_at)).where(*parked_filters))
    ).scalar()

    fired_filters = [TaskWakeRegistration.delivery_state == WakeDeliveryState.FIRED]
    if project_id is not None:
        fired_filters.append(TaskWakeRegistration.project_id == project_id)
    fired_undelivered_count = (
        await session.execute(
            select(func.count()).select_from(TaskWakeRegistration).where(*fired_filters)
        )
    ).scalar() or 0
    oldest_fired_at = (
        await session.execute(
            select(func.min(TaskWakeRegistration.fired_at)).where(*fired_filters)
        )
    ).scalar()

    oldest_fired_age_seconds = _age_seconds(now, oldest_fired_at)

    # Unified stall definition: the same cycle-based counter that drives the
    # reaper log and the operator Alert. The age-based threshold
    # (_PARK_WAKE_DELIVERY_STALL_SECONDS) is kept for the oldest_fired_age_seconds
    # observability field and delivery_stall_threshold_seconds; it is no longer
    # used for the delivery_stalled boolean so all stall surfaces agree.
    from app.reaper_sweeps import (
        _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD,
        get_wake_delivery_stall_cycles,
    )

    delivery_stalled = (
        get_wake_delivery_stall_cycles() >= _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD
    )

    # Historical breakdown of park_wake_timeout outcomes by cause over the last 7 days.
    # Buckets: wake_lost (delivery bug), provisioning_starved (job never reached
    # running state), ran_out_of_time (job started but did not complete in time),
    # unknown (no failure_detail on pre-enrichment failures).
    _timeout_window_start = now - timedelta(days=7)
    _timeout_task_filters = [
        Task.status == TaskStatus.FAILED,
        Task.updated_at >= _timeout_window_start,
    ]
    if project_id is not None:
        _timeout_task_filters.append(Task.project_id == project_id)
    _timeout_ctxs = (
        (
            await session.execute(
                select(Task.context).where(*_timeout_task_filters).limit(500)
            )
        )
        .scalars()
        .all()
    )

    timeout_by_cause: Dict[str, int] = {}
    for _ctx in _timeout_ctxs:
        if not isinstance(_ctx, dict):
            continue
        if _ctx.get("failure_class") != "park_wake_timeout":
            continue
        detail = _ctx.get("failure_detail") or "unknown"
        if detail == "compute_job_terminal_job_not_terminal_by_deadline":
            diag = _ctx.get("park_wake_timeout_diagnostics") or {}
            if "job_started_at" not in diag:
                # Pre-enrichment failure — sub-cause not recorded; group by detail.
                cause = detail
            elif diag.get("job_started_at") is None:
                cause = "provisioning_starved"
            else:
                cause = "ran_out_of_time"
        else:
            cause = detail
        timeout_by_cause[cause] = timeout_by_cause.get(cause, 0) + 1

    return {
        "parked_task_count": int(parked_task_count),
        "oldest_parked_age_seconds": _age_seconds(now, oldest_parked_at),
        "fired_undelivered_count": int(fired_undelivered_count),
        "oldest_fired_age_seconds": oldest_fired_age_seconds,
        "delivery_stalled": delivery_stalled,
        "delivery_stall_threshold_seconds": _PARK_WAKE_DELIVERY_STALL_SECONDS,
        "stall_cycles": get_wake_delivery_stall_cycles(),
        "timeout_by_cause": timeout_by_cause,
        "timeout_cause_window_days": 7,
    }


async def compute_dependency_health_signals(
    session: AsyncSession,
    project_id: "uuid.UUID | None" = None,
) -> Dict[str, Any]:
    """Surface the parked-blocker and duplicate-foundation observability signals.

    Read-only: runs two small self-contained queries (parked tasks; active
    IN_PROGRESS tasks) and delegates to the pure summarizers above. Never mutates
    state. Scoped to ``project_id`` when provided, else fleet-wide.

    The parked query matches BOTH parked classes the summarizer handles:
      * BLOCKED tasks carrying ``_blocked_escalated`` (operator-escalated blocks), AND
      * any-status tasks carrying ``_recut_escalated`` (persistent base re-cut holds,
        set by ``orchestrator_health._record_recut_failure`` on the dependent —
        which may be QUEUED or BLOCKED-without-``_blocked_escalated``).
    Without the ``_recut_escalated`` arm the summarizer's re-cut branch would be
    unreachable in production.
    """
    if resolve_dialect_name(session) == "sqlite":
        parked_predicate = or_(
            and_(
                Task.status == TaskStatus.BLOCKED,
                func.json_extract(Task.context, "$._blocked_escalated").is_not(None),
            ),
            func.json_extract(Task.context, "$._recut_escalated").is_not(None),
        )
    else:
        parked_predicate = or_(
            and_(
                Task.status == TaskStatus.BLOCKED,
                Task.context["_blocked_escalated"].astext == "true",
            ),
            Task.context["_recut_escalated"].astext == "true",
        )
    blocked_filters = [parked_predicate]
    if project_id is not None:
        blocked_filters.append(Task.project_id == project_id)
    blocked = (
        (
            await session.execute(
                select(Task).where(*blocked_filters).limit(_PARKED_BLOCKER_LIST_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    active_filters = [Task.status == TaskStatus.IN_PROGRESS]
    if project_id is not None:
        active_filters.append(Task.project_id == project_id)
    active = (
        (await session.execute(select(Task).where(*active_filters).limit(1000)))
        .scalars()
        .all()
    )

    # Terminal give-up cancellations of blocked dependents over a recent window.
    # Cheaply pre-filtered by updated_at (advances on the CANCELLED transition);
    # the summarizer refines by the marker's own ``at`` and buckets the counts.
    from app.reaper_sweeps import UNRESUMABLE_CANCEL_CONTEXT_KEY
    from app.time_utils import _utcnow

    window_start = _utcnow() - _UNRESUMABLE_CANCEL_WINDOW
    # Key-existence on ``Task.context``: ``has_key`` compiles to the Postgres
    # JSONB ``?`` operator (fails on the SQLite smoke gate with a parameter
    # placeholder error), so branch the SQLite path through ``json_extract``,
    # which resolves the marker to NULL only when the key is absent.
    if resolve_dialect_name(session) == "sqlite":
        marker_present = func.json_extract(
            Task.context, f"$.{UNRESUMABLE_CANCEL_CONTEXT_KEY}"
        ).is_not(None)
    else:
        marker_present = Task.context.has_key(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    cancel_filters = [
        Task.status == TaskStatus.CANCELLED,
        marker_present,
        Task.updated_at >= window_start,
    ]
    if project_id is not None:
        cancel_filters.append(Task.project_id == project_id)
    cancelled = (
        (
            await session.execute(
                select(Task)
                .where(*cancel_filters)
                .limit(_UNRESUMABLE_CANCEL_LIST_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    # Live BLOCKED population — the healthy waiting set the parked-only signal
    # deliberately excludes. Status-scoped (BLOCKED) so it counts every dependent
    # currently waiting, escalated or not.
    live_blocked_filters = [Task.status == TaskStatus.BLOCKED]
    if project_id is not None:
        live_blocked_filters.append(Task.project_id == project_id)
    live_blocked = (
        (
            await session.execute(
                select(Task)
                .where(*live_blocked_filters)
                .limit(_LIVE_BLOCKED_LIST_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    # Successful blocked-dependent resumes over a recent window. The marker
    # persists past the QUEUED transition into IN_PROGRESS/terminal states, so this
    # query is STATUS-AGNOSTIC — it filters ONLY on marker presence and the
    # updated_at pre-filter, then the summarizer refines by the marker's own ``at``.
    from app.reaper_sweeps import RESUME_MARKER_CONTEXT_KEY

    resume_window_start = _utcnow() - _BLOCKED_RESUME_WINDOW
    if resolve_dialect_name(session) == "sqlite":
        resume_marker_present = func.json_extract(
            Task.context, f"$.{RESUME_MARKER_CONTEXT_KEY}"
        ).is_not(None)
    else:
        resume_marker_present = Task.context.has_key(RESUME_MARKER_CONTEXT_KEY)
    resume_filters = [
        resume_marker_present,
        Task.updated_at >= resume_window_start,
    ]
    if project_id is not None:
        resume_filters.append(Task.project_id == project_id)
    resumed = (
        (
            await session.execute(
                select(Task).where(*resume_filters).limit(_BLOCKED_RESUME_LIST_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    if resolve_dialect_name(session) == "sqlite":
        give_up_present = func.json_extract(
            Task.context, "$.finding_structural_give_up"
        ).is_not(None)
    else:
        give_up_present = Task.context.has_key("finding_structural_give_up")
    give_up_filters = [Task.updated_at >= window_start, give_up_present]
    if project_id is not None:
        give_up_filters.append(Task.project_id == project_id)
    finding_give_up = (
        (await session.execute(select(Task).where(*give_up_filters).limit(1000)))
        .scalars()
        .all()
    )

    return {
        "parked_blockers": compute_parked_blocker_summary(list(blocked)),
        "duplicate_foundation_candidates": compute_duplicate_foundation_signal(
            list(active)
        ),
        "unresumable_cancellations": compute_unresumable_cancellation_summary(
            list(cancelled), window_start=window_start
        ),
        "finding_structural_give_up": _compute_finding_structural_give_up_summary(
            list(finding_give_up)
        ),
        "live_blocked": compute_live_blocked_summary(list(live_blocked)),
        "resumes": compute_blocked_resume_summary(
            list(resumed), window_start=resume_window_start
        ),
        "missing_capability_demand": await compute_missing_capability_demand_summary(
            session, project_id
        ),
    }


def _coerce_count(value: Any) -> "int | None":
    """Best-effort int coercion for a JSONB tally value; ``None`` when the value
    is missing or non-numeric. Lives outside the aggregation loop so the
    per-entry try/except does not incur the PERF203 in-loop overhead."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_gate_outcome_in_window(
    outcome: Dict[str, Any], window_start: "datetime"
) -> bool:
    """True when a ``recent_outcomes`` entry's ``at`` timestamp is at/after
    ``window_start``. Defensive: a missing/non-string/unparseable ``at`` is
    treated as out-of-window (dropped) rather than raising.
    """
    at = outcome.get("at")
    if not isinstance(at, str):
        return False
    try:
        parsed = datetime.fromisoformat(at)
    except (ValueError, TypeError):
        return False
    return _coerce_naive_utc(parsed) >= window_start


async def compute_merge_gate_fleet_health(
    session: AsyncSession,
    project_id: "uuid.UUID | None" = None,
    window_start: "datetime | None" = None,
) -> Dict[str, Any]:
    """Fleet-wide no-CI merge-gate verification health — a READ-ONLY observability
    signal that NEVER influences any merge decision.

    Answers the systemic question no per-project view can: across every project
    whose no-CI merge gate has run, what fraction of merges actually ran real
    tests vs. fell open to an UNVERIFIED merge, which fall-open reason dominates,
    and how many projects have a degraded gate. A systemically-broken gate (e.g.
    broken provisioning) merges UNVERIFIED at scale while each project looks
    individually unremarkable; this surfaces that aggregate.

    Data source: per-project ``Project.merge_gate_runtime`` (written by
    ``ci_resolution.record_no_ci_gate_runtime``) which carries ``recent_outcomes``
    (a list capped at 20 of ``{"at": iso, "verified": bool, "reason": key|None}``)
    and the all-time ``unverified_reason_counts`` tally. When ``window_start`` is
    provided, ``recent_outcomes`` is Python-filtered to entries at/after it (the
    list is small, so an in-memory filter is cheap and avoids JSONB date math).

    Scoped to ``project_id`` when provided, else fleet-wide. Defensive against a
    null / legacy four-key-only / malformed ``merge_gate_runtime`` so it can never
    raise. Returns all-zero counts and ``None`` rates/reason when no project has
    any gate data.
    """
    # Narrow the projection: only merge_gate_runtime is read in the loop.
    # raiseload=True catches any future accidental full-ORM attribute access.
    stmt = select(Project).options(
        load_only(Project.id, Project.merge_gate_runtime, raiseload=True)
    )
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    projects = (await session.execute(stmt)).scalars().all()

    verified_count = 0
    fell_open_count = 0
    degraded_project_count = 0
    project_count = 0
    reason_histogram: Dict[str, int] = {}
    windowed_reason_histogram: Dict[str, int] = {}

    for project in projects:
        raw = getattr(project, "merge_gate_runtime", None)
        if not isinstance(raw, dict) or not raw:
            continue

        # Aggregate the all-time per-reason fall-open tally across the fleet so
        # the dominant fall-open reason can be surfaced regardless of window.
        raw_counts = raw.get("unverified_reason_counts")
        if isinstance(raw_counts, dict):
            for reason_key, count in raw_counts.items():
                coerced = _coerce_count(count)
                if coerced is not None:
                    reason_histogram[str(reason_key)] = (
                        reason_histogram.get(str(reason_key), 0) + coerced
                    )

        raw_outcomes = raw.get("recent_outcomes")
        if not isinstance(raw_outcomes, list):
            # Legacy four-key-only record (no rolling history) — no window
            # activity to count.
            continue
        outcomes = [o for o in raw_outcomes if isinstance(o, dict)]
        if window_start is not None:
            outcomes = [
                o for o in outcomes if _merge_gate_outcome_in_window(o, window_start)
            ]
        if not outcomes:
            continue

        project_count += 1
        proj_verified = sum(1 for o in outcomes if o.get("verified"))
        proj_fell_open = len(outcomes) - proj_verified
        verified_count += proj_verified
        fell_open_count += proj_fell_open
        if proj_fell_open > 0:
            degraded_project_count += 1

        # Tally windowed fall-open reasons for the dominant reason when a
        # window is provided. Each outcome carries its own reason, so we
        # can derive the windowed dominant reason directly without using
        # the all-time tally.
        if window_start is not None:
            for outcome in outcomes:
                if not outcome.get("verified"):
                    reason = outcome.get("reason")
                    if reason is not None:
                        reason_key = str(reason)
                        windowed_reason_histogram[reason_key] = (
                            windowed_reason_histogram.get(reason_key, 0) + 1
                        )

    total = verified_count + fell_open_count
    verification_rate = (verified_count / total) if total > 0 else None
    most_common_reason = None
    # When a window is provided, use the windowed tally; otherwise use the
    # all-time tally for completeness.
    histogram_to_use = (
        windowed_reason_histogram if window_start is not None else reason_histogram
    )
    if histogram_to_use:
        # Deterministic tie-break: highest count, then reason key (mirrors
        # ci_resolution.merge_gate_runtime_block's per-project tie-break).
        most_common_reason = sorted(
            histogram_to_use.items(), key=lambda kv: (-kv[1], kv[0])
        )[0][0]

    return {
        "verified_count": verified_count,
        "fell_open_count": fell_open_count,
        "verification_rate": verification_rate,
        "most_common_reason": most_common_reason,
        "degraded_project_count": degraded_project_count,
        "project_count": project_count,
    }


def compute_migration_health(
    completed_tasks: List[Task],
    failed_tasks: List[Task],
    *,
    error_log_map: Dict[str, Any],
    active_tasks: List[Task] | None = None,
    migration_slot_contention: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Summarize migration-task throughput and migration-error incidence.

    Pure function — no database calls. Imports ``EXPECTED_SCHEMA_REVISION``
    at call time to avoid a hard import cycle with ``app.main``.
    """
    from app.main import EXPECTED_SCHEMA_REVISION
    from app.failure_classification import classify_failure as _mig_classify

    def _task_type(t: Task) -> Optional[str]:
        return (t.first_payload or {}).get("type")

    def _touched_migration(t: Task) -> bool:
        ctx = t.context or {}
        files = ctx.get("related_files") or []
        if isinstance(files, list) and any(
            isinstance(f, str) and f.startswith("backend/alembic/versions/")
            for f in files
        ):
            return True
        payload_files = (t.first_payload or {}).get("related_files") or []
        return isinstance(payload_files, list) and any(
            isinstance(f, str) and f.startswith("backend/alembic/versions/")
            for f in payload_files
        )

    _migrate_completed = [t for t in completed_tasks if _task_type(t) == "migrate"]
    _migrate_failed = [t for t in failed_tasks if _task_type(t) == "migrate"]
    _migration_errors = sum(
        1
        for t in _migrate_failed
        if (t.context or {}).get("failure_class") == "MIGRATION_ERROR"
        or _mig_classify(t, error_log_map.get(str(t.id))) == "MIGRATION_ERROR"
    )
    _auto_generated_migrations = sum(
        1
        for t in (completed_tasks + failed_tasks)
        if _task_type(t) != "migrate" and _touched_migration(t)
    )

    contention_summary = migration_slot_contention
    if contention_summary is None:
        from app.orchestrator_filters import _is_migration_task  # noqa: PLC0415

        active_migration_tasks = [
            t for t in (active_tasks or []) if _is_migration_task(t)
        ]
        contention_summary = compute_migration_slot_contention_summary(
            [t for t in active_migration_tasks if t.status == TaskStatus.IN_PROGRESS],
            [
                t
                for t in active_migration_tasks
                if t.status in (TaskStatus.QUEUED, TaskStatus.BLOCKED)
            ],
        )

    deferred_count = int(contention_summary.get("waiting_count") or 0)
    if not contention_summary.get("slot_held") and deferred_count == 0:
        contention_status = "idle"
        contention_severity = "info"
    elif deferred_count == 0:
        contention_status = "held"
        contention_severity = "info"
    elif contention_summary.get("threshold_exceeded"):
        contention_status = "elevated"
        contention_severity = "critical"
    else:
        contention_status = "contention"
        contention_severity = "warning"

    return {
        "current_schema_revision": EXPECTED_SCHEMA_REVISION,
        "migration_tasks_in_window": len(_migrate_completed) + len(_migrate_failed),
        "migration_tasks_succeeded": len(_migrate_completed),
        "migration_tasks_failed": len(_migrate_failed),
        "migration_errors_in_window": _migration_errors,
        "auto_generated_migrations": _auto_generated_migrations,
        "migration_slot_contention": {
            "status": contention_status,
            "severity": contention_severity,
            "holder": {
                "task_id": contention_summary.get("holder_task_id"),
                "title": contention_summary.get("holder_title"),
                "started_at": contention_summary.get("holder_started_at"),
                "hold_seconds": contention_summary.get("holder_held_seconds"),
            }
            if contention_summary.get("holder_task_id") is not None
            else None,
            "deferred_count": deferred_count,
            "longest_wait": {
                "task_id": contention_summary.get("longest_wait_task_id"),
                "title": contention_summary.get("longest_wait_title"),
                "waiting_since": contention_summary.get("longest_wait_since"),
                "wait_seconds": contention_summary.get("longest_wait_seconds"),
            }
            if contention_summary.get("longest_wait_task_id") is not None
            else None,
        },
    }


# ── Migration-serialization slot contention (read-only observability) ────────
#
# A migration-producing task (``migrate`` type, or one touching
# ``backend/app/models.py`` / ``backend/alembic/versions/``) that is IN_PROGRESS
# holds the per-project migration-serialization slot; while it is held, sibling
# schema-touching tasks defer to QUEUED/BLOCKED ("Waiting: serializing migration
# …"). The existing ``compute_migration_health`` summary reports migration
# throughput and error counts but NOT this contention, and the parked-blocker
# signal counts ONLY BLOCKED tasks past escalation — migration-deferred siblings
# are QUEUED, so neither metric sees a multi-hour hold parking the schema queue
# (the recent incident: one holder held the slot ~2h and parked 5+ tasks before a
# human noticed). This pair surfaces it: per project, the current holder and how
# long it has held, how many migration-producing tasks are deferred waiting, and
# the longest current wait. Read-only — it never changes serialization behavior.

# A slot held at least this long WHILE tasks are waiting marks the per-project
# entry ``threshold_exceeded`` (the loud self-surfacing signal the UI highlights
# and the reaper alert sweep can escalate on). One hour: well below the ~2h
# incident, well above a normal migration author+test run.
_MIGRATION_SLOT_HOLD_ALERT_SECONDS = 3600
# Defensive cap on the surfaced per-project list so a large fleet never bloats
# the digest payload.
_MIGRATION_SLOT_PROJECT_LIST_LIMIT = 100


def _migration_slot_hold_seconds(holder: "Task", now: "datetime") -> "float | None":
    """Seconds the holder has been IN_PROGRESS (its slot-hold age).

    Anchored on ``started_at`` (stamped at dispatch) and falling back to
    ``created_at`` so a holder that somehow lacks ``started_at`` still yields a
    non-null age. ``None`` only when neither timestamp is present.
    """
    anchor = getattr(holder, "started_at", None) or getattr(holder, "created_at", None)
    if anchor is None:
        return None
    return max(0.0, (now - anchor).total_seconds())


def compute_migration_slot_contention_summary(
    holders: List["Task"],
    waiters: List["Task"],
) -> Dict[str, Any]:
    """Reduce migration-slot holders and waiters into a contention summary.

    Pure function — no database calls; duration math only. ``holders`` are the
    IN_PROGRESS migration-producing tasks; ``waiters`` are the QUEUED/BLOCKED
    migration-producing tasks deferred on the slot. Both are grouped by project.

    For each project the representative holder is the LONGEST-held IN_PROGRESS
    migration task (the likeliest bottleneck — under the reservation scheme more
    than one may be in flight, so ``holder_count`` is reported alongside). A
    waiter's wait age is derived from ``updated_at`` (which advances when the task
    is re-stamped with its "Waiting: serializing migration …" deferral status).
    ``threshold_exceeded`` fires only when the slot is held, the hold age reaches
    ``_MIGRATION_SLOT_HOLD_ALERT_SECONDS``, AND at least one task is waiting — so a
    long solo migration with no waiters never raises the loud signal.

    Returns a flat AGGREGATE view (max hold age / total waiters / max wait across
    the scoped projects, plus the representative holder) and a per-project
    ``projects`` list for fleet-wide consumers (e.g. the reaper alert sweep).
    Non-zero exactly when migration tasks are actually deferred on the slot.
    """
    now = _utcnow()

    def _proj(t: "Task") -> "str | None":
        pid = getattr(t, "project_id", None)
        return str(pid) if pid is not None else None

    holders_by_project: Dict[Any, List["Task"]] = defaultdict(list)
    for t in holders:
        holders_by_project[_proj(t)].append(t)
    waiters_by_project: Dict[Any, List["Task"]] = defaultdict(list)
    for t in waiters:
        waiters_by_project[_proj(t)].append(t)

    projects: List[Dict[str, Any]] = []
    for pkey in set(holders_by_project) | set(waiters_by_project):
        p_holders = holders_by_project.get(pkey, [])
        p_waiters = waiters_by_project.get(pkey, [])

        holder = None
        holder_held_seconds: "float | None" = None
        for h in p_holders:
            held = _migration_slot_hold_seconds(h, now)
            if holder is None or (
                held is not None
                and (holder_held_seconds is None or held > holder_held_seconds)
            ):
                holder = h
                holder_held_seconds = held

        wait_ages = [
            max(0.0, (now - w.updated_at).total_seconds())
            for w in p_waiters
            if getattr(w, "updated_at", None) is not None
        ]
        longest_wait_seconds = max(wait_ages) if wait_ages else None
        longest_waiter = max(
            (w for w in p_waiters if getattr(w, "updated_at", None) is not None),
            key=lambda w: max(0.0, (now - w.updated_at).total_seconds()),
            default=None,
        )

        slot_held = holder is not None
        waiting_count = len(p_waiters)
        threshold_exceeded = bool(
            slot_held
            and waiting_count > 0
            and holder_held_seconds is not None
            and holder_held_seconds >= _MIGRATION_SLOT_HOLD_ALERT_SECONDS
        )

        projects.append(
            {
                "project_id": pkey,
                "slot_held": slot_held,
                "holder_task_id": str(holder.id) if holder is not None else None,
                "holder_title": (holder.title or str(holder.id)[:8])
                if holder is not None
                else None,
                "holder_started_at": holder.started_at.isoformat()
                if holder is not None and holder.started_at is not None
                else None,
                "holder_held_seconds": round(holder_held_seconds)
                if holder_held_seconds is not None
                else None,
                "holder_count": len(p_holders),
                "waiting_count": waiting_count,
                "longest_wait_task_id": str(longest_waiter.id)
                if longest_waiter is not None
                else None,
                "longest_wait_title": longest_waiter.title
                if longest_waiter is not None
                else None,
                "longest_wait_since": longest_waiter.updated_at.isoformat()
                if longest_waiter is not None and longest_waiter.updated_at is not None
                else None,
                "longest_wait_seconds": round(longest_wait_seconds)
                if longest_wait_seconds is not None
                else None,
                "threshold_exceeded": threshold_exceeded,
            }
        )

    # Newest-incident-first: most-contended projects (threshold breaches, then
    # longest holds) lead the capped list.
    projects.sort(
        key=lambda p: (
            p["threshold_exceeded"],
            p["holder_held_seconds"] or 0,
            p["waiting_count"],
        ),
        reverse=True,
    )
    projects = projects[:_MIGRATION_SLOT_PROJECT_LIST_LIMIT]

    # Aggregate roll-up — the representative (longest-held) holder across scope.
    rep = projects[0] if projects else None
    aggregate_longest_wait = max(
        (p for p in projects if p.get("longest_wait_task_id") is not None),
        key=lambda p: p["longest_wait_seconds"] or 0,
        default=None,
    )
    return {
        "slot_held": any(p["slot_held"] for p in projects),
        "holder_task_id": rep["holder_task_id"] if rep else None,
        "holder_title": rep["holder_title"] if rep else None,
        "holder_project_id": rep["project_id"] if rep else None,
        "holder_started_at": rep["holder_started_at"] if rep else None,
        "holder_held_seconds": max(
            (p["holder_held_seconds"] or 0 for p in projects), default=0
        )
        if projects
        else None,
        "waiting_count": sum(p["waiting_count"] for p in projects),
        "longest_wait_task_id": aggregate_longest_wait["longest_wait_task_id"]
        if aggregate_longest_wait
        else None,
        "longest_wait_title": aggregate_longest_wait["longest_wait_title"]
        if aggregate_longest_wait
        else None,
        "longest_wait_since": aggregate_longest_wait["longest_wait_since"]
        if aggregate_longest_wait
        else None,
        "longest_wait_seconds": max(
            (p["longest_wait_seconds"] or 0 for p in projects), default=0
        )
        if projects
        else None,
        "threshold_exceeded": any(p["threshold_exceeded"] for p in projects),
        "projects": projects,
    }


async def compute_migration_slot_contention(
    session: AsyncSession,
    project_id: "uuid.UUID | None" = None,
) -> Dict[str, Any]:
    """Surface migration-serialization-slot contention (read-only observability).

    Runs two small self-contained queries — IN_PROGRESS tasks (potential slot
    holders) and QUEUED/BLOCKED tasks (potential waiters) — filters each to the
    migration-producing subset via the canonical
    :func:`orchestrator_filters._is_migration_task` detector, and delegates the
    grouping/duration math to :func:`compute_migration_slot_contention_summary`.
    Never mutates state. Scoped to ``project_id`` when provided, else fleet-wide.

    ``_is_migration_task`` is deferred-imported to keep
    ``app.orchestrator_filters`` off this module's import path at load time (its
    own imports are leaf: migration_path, models, shared_state, time_utils,
    task_capabilities).
    """
    from app.orchestrator_filters import _is_migration_task  # noqa: PLC0415

    holder_filters = [Task.status == TaskStatus.IN_PROGRESS]
    if project_id is not None:
        holder_filters.append(Task.project_id == project_id)
    in_progress = (
        (await session.execute(select(Task).where(*holder_filters).limit(1000)))
        .scalars()
        .all()
    )

    waiter_filters = [Task.status.in_([TaskStatus.QUEUED, TaskStatus.BLOCKED])]
    if project_id is not None:
        waiter_filters.append(Task.project_id == project_id)
    waiting = (
        (await session.execute(select(Task).where(*waiter_filters).limit(2000)))
        .scalars()
        .all()
    )

    return compute_migration_slot_contention_summary(
        [t for t in in_progress if _is_migration_task(t)],
        [t for t in waiting if _is_migration_task(t)],
    )


def compute_digest_trend_history(
    *,
    all_tasks: List[Task],
    hours: int,
    now: "datetime",
) -> List[Dict[str, Any]]:
    """Build the 7-period trend series (6 previous + current, oldest first).

    Pure function — no database calls. Returns a list of per-period dicts
    containing success_rate, completed, failed, failed_by_class,
    failed_by_taxonomy, period_cost_usd, and first-attempt metrics.
    """
    from app.failure_classification import (
        classify_failure as _classify_failure,
        classify_failure_taxonomy as _classify_taxonomy,
    )
    from app.routers.admin_analytics import (
        ON_DEMAND_RATE,
        SPOT_RATE,
        _derive_completed_at,
    )

    trend_history = []
    for _i in range(7):
        _p_start = now - timedelta(hours=hours * (7 - _i))
        _p_end = now - timedelta(hours=hours * (6 - _i))
        _p_completed = sum(
            1
            for t in all_tasks
            if t.status == TaskStatus.COMPLETED
            and _p_start <= _derive_completed_at(t) < _p_end
        )
        _p_failed_tasks = [
            t
            for t in all_tasks
            if t.status == TaskStatus.FAILED
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_failed = len(_p_failed_tasks)
        _p_by_class: dict[str, int] = {}
        _p_by_taxonomy: dict[str, int] = {}
        for _ft in _p_failed_tasks:
            _cls = _classify_failure(_ft, at_time=_ft.last_heartbeat or _ft.started_at)
            _p_by_class[_cls] = _p_by_class.get(_cls, 0) + 1
            _tax = _classify_taxonomy(_ft)
            _p_by_taxonomy[_tax] = _p_by_taxonomy.get(_tax, 0) + 1
        _p_total = _p_completed + _p_failed
        _p_cost = sum(
            ((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE)
            * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            for t in all_tasks
            if t.vm_provisioned_at
            and t.vm_stopped_at
            and _p_start <= t.vm_stopped_at < _p_end
        )
        _p_terminal = [
            t
            for t in all_tasks
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            and _p_start <= _derive_completed_at(t) < _p_end
        ]
        _p_measured = [
            t for t in _p_terminal if "_first_attempt_succeeded" in (t.context or {})
        ]
        _p_fa_rate = (
            round(
                sum(
                    1
                    for t in _p_measured
                    if (t.context or {}).get("_first_attempt_succeeded")
                )
                / len(_p_measured),
                2,
            )
            if _p_measured
            else None
        )
        trend_history.append(
            {
                "period_end": _p_end.isoformat(),
                "success_rate": round(_p_completed / _p_total, 2)
                if _p_total > 0
                else None,
                "completed": _p_completed,
                "failed": _p_failed,
                "failed_by_class": _p_by_class,
                "failed_by_taxonomy": _p_by_taxonomy,
                "period_cost_usd": round(_p_cost, 4),
                "first_attempt_success_rate": _p_fa_rate,
                "first_attempt_sample_size": len(_p_measured),
            }
        )
    return trend_history


def compute_digest_cost_economics(
    *,
    all_tasks: List[Task],
    direction_progress_list: List[Dict[str, Any]],
    window_start: "datetime",
    now: "datetime",
    daily_cost_ceiling_usd: float,
    compute_job_daily_cost_usd: float = 0.0,
) -> Dict[str, Any]:
    """Compute the digest's entire cost/economics section.

    Pure function — no database calls. Reads only the already-loaded task
    list, the already-computed direction-progress list, the window start,
    the current time, and the configured daily cost ceiling.

    Mutates ``direction_progress_list`` in place: stamps a
    ``cost_per_merge_exceeds_threshold`` flag onto each direction entry
    (preserving the original in-place behavior of ``_compute_digest``).

    Returns a dict with total/spot/on-demand estimated cost, provisioning
    overhead (USD and pct), daily cost vs ceiling, overall product_ratio,
    median cost-per-merged-PR, per-task-type cost-per-merge, and
    pipeline-level acceptance/conversion rates, autonomous/assisted merge
    counts, operator-action count, overall type distribution, and
    filter-funnel aggregation.
    """
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

    # Compute total estimated cost (directed + undirected tasks)
    directed_task_ids = {
        t.id for t in all_tasks if (t.context or {}).get("direction_id")
    }
    undirected_cost = sum(
        ((t.vm_spot and SPOT_RATE) or ON_DEMAND_RATE)
        * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        for t in all_tasks
        if t.vm_provisioned_at
        and t.vm_stopped_at
        and t.vm_stopped_at >= window_start
        and t.id not in directed_task_ids
    )
    total_estimated_cost_usd = round(
        sum(d["estimated_cost_usd"] for d in direction_progress_list) + undirected_cost,
        2,
    )
    total_spot_cost_usd = 0.0
    total_on_demand_cost_usd = 0.0
    for t in all_tasks:
        if not (
            t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start
        ):
            continue
        rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
        t_cost = rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        if t.vm_spot:
            total_spot_cost_usd += t_cost
        else:
            total_on_demand_cost_usd += t_cost
    total_spot_cost_usd = round(total_spot_cost_usd, 2)
    total_on_demand_cost_usd = round(total_on_demand_cost_usd, 2)

    # Provisioning overhead: cost of time spent between VM provision and agent claim
    # (vm_provisioned_at → vm_started_at). When vm_started_at is missing, the agent
    # never claimed the task, so the entire VM lifetime counts as provisioning waste.
    _provisioning_overhead_raw = 0.0
    _total_compute_cost_raw = total_spot_cost_usd + total_on_demand_cost_usd
    for t in all_tasks:
        if not (
            t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start
        ):
            continue
        rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
        prov_end = (
            t.vm_started_at
            if (t.vm_started_at and t.vm_started_at >= t.vm_provisioned_at)
            else t.vm_stopped_at
        )
        if prov_end <= t.vm_provisioned_at:
            continue
        _provisioning_overhead_raw += (
            rate * (prov_end - t.vm_provisioned_at).total_seconds()
        )
    provisioning_overhead_usd = round(_provisioning_overhead_raw, 2)
    provisioning_overhead_pct = (
        round(_provisioning_overhead_raw / _total_compute_cost_raw, 3)
        if _total_compute_cost_raw > 0
        else None
    )

    # Compute today's cost for cost ceiling warning. The VM figure comes from
    # task VM-runtime (using the machine-type-aware helper so larger instances are
    # costed correctly); off-VM compute-job spend is passed in pre-aggregated by
    # the caller (admin_digest) from the ComputeJob ledger so daily_cost_usd
    # means total spend, not just VM runtime.
    # In-flight tasks (vm_stopped_at IS NULL) are estimated to now so the figure
    # is current rather than lagging until the task eventually stops.
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_vm_cost_usd = round(
        sum(
            get_task_total_vm_cost_usd(
                t,
                stopped_at=now if t.vm_stopped_at is None else None,
            )
            for t in all_tasks
            if t.vm_provisioned_at is not None
            and (t.vm_stopped_at is None or t.vm_stopped_at >= today_start)
        ),
        2,
    )
    daily_compute_cost_usd = round(float(compute_job_daily_cost_usd or 0.0), 2)
    daily_cost_usd = round(daily_vm_cost_usd + daily_compute_cost_usd, 2)

    # Compute overall product_ratio and type_distribution across all directions
    all_product_ratios = [
        d["product_ratio"]
        for d in direction_progress_list
        if d.get("product_ratio") is not None
    ]
    overall_product_ratio = (
        round(sum(all_product_ratios) / len(all_product_ratios), 3)
        if all_product_ratios
        else None
    )

    all_cpms = [
        d["cost_per_merged_pr"]
        for d in direction_progress_list
        if d.get("cost_per_merged_pr") is not None
    ]
    median_cost_per_merged_pr = (
        round(statistics.median(all_cpms), 4) if all_cpms else None
    )
    for d in direction_progress_list:
        cpm = d.get("cost_per_merged_pr")
        if cpm is not None and median_cost_per_merged_pr is not None:
            d["cost_per_merge_exceeds_threshold"] = cpm > (
                median_cost_per_merged_pr * _CPM_RETIREMENT_MULTIPLIER
            )
        else:
            d["cost_per_merge_exceeds_threshold"] = False

    # Per-task-type cost-per-merge: for each task type, total VM cost / merged PR count
    _type_cost: Dict[str, float] = {}
    _type_merges: Dict[str, int] = {}
    for t in all_tasks:
        if not (
            t.vm_provisioned_at and t.vm_stopped_at and t.vm_stopped_at >= window_start
        ):
            continue
        rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
        t_cost = rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        task_type = (t.first_payload or {}).get("type", "unknown")
        _type_cost[task_type] = _type_cost.get(task_type, 0.0) + t_cost
        if has_landed_signal(t):
            _type_merges[task_type] = _type_merges.get(task_type, 0) + 1
    _type_cpms = {
        ttype: _type_cost[ttype] / _type_merges[ttype]
        for ttype in _type_cost
        if _type_merges.get(ttype, 0) > 0
    }
    # Compute median excluding the typeless bucket ("unknown") so retirement
    # thresholds are based on real task types, not phantom untyped tasks.
    _type_cpms_for_median = {
        ttype: cpm for ttype, cpm in _type_cpms.items() if ttype != "unknown"
    }
    median_cpm_by_type = (
        round(statistics.median(list(_type_cpms_for_median.values())), 4)
        if _type_cpms_for_median
        else None
    )
    cost_per_merge_by_type = {
        ttype: {
            "total_cost_usd": round(_type_cost[ttype], 4),
            "merged_count": _type_merges.get(ttype, 0),
            "cost_per_merge": round(_type_cpms[ttype], 4)
            if ttype in _type_cpms
            else None,
            "exceeds_threshold": (
                _type_cpms[ttype] > (median_cpm_by_type * _CPM_RETIREMENT_MULTIPLIER)
                if ttype in _type_cpms and median_cpm_by_type is not None
                else False
            ),
        }
        for ttype in _type_cost
    }

    # Pipeline-level acceptance rate: total merged / (total merged + total rejected) across directions in window.
    # Sum the RAW merge count (operator-accepted merges) matching the per-direction acceptance_rate convention
    # used by digest_direction_progress and direction_metrics, which both divide raw prs_merged by
    # raw (merged + rejected). Unverified-degraded merges are counted because the per-direction rates
    # count them — this metric reports raw operator-acceptance outcomes consistently.
    _pipeline_total_merged_d = sum(
        d.get("prs_merged") or 0
        for d in direction_progress_list
    )
    _pipeline_total_rejected_d = sum(
        d.get("prs_rejected") or 0 for d in direction_progress_list
    )
    _pipeline_total_decided = _pipeline_total_merged_d + _pipeline_total_rejected_d
    pipeline_acceptance_rate = (
        round(_pipeline_total_merged_d / _pipeline_total_decided, 3)
        if _pipeline_total_decided > 0
        else None
    )
    # Conversion rate denominator is PR-eligible tasks (excludes research/audit/
    # memo/investigate types that never open a PR), consistent with the per-direction
    # denominator. NOTE: the numerator uses raw prs_merged (same as acceptance_rate above),
    # which diverges from the per-direction conversion_rate that uses verified_prs_merged
    # (digest_direction_progress.py:914-915). Pipeline conversion_rate is intentionally
    # raw to report operator-observable outcomes; the per-direction rate is verification-aware.
    _pipeline_total_eligible_d = sum(
        d.get("pr_eligible_tasks_created") or 0 for d in direction_progress_list
    )
    pipeline_conversion_rate = (
        round(_pipeline_total_merged_d / _pipeline_total_eligible_d, 3)
        if _pipeline_total_eligible_d > 0
        else None
    )
    # Retired: pipeline_autonomous_prs_merged / pipeline_assisted_prs_merged /
    # pipeline_operator_action_count were window-level sums of the per-direction
    # autonomous_prs_merged / assisted_prs_merged / operator_action_count fields.
    # They were emitted to /api/admin/digest but had no named consumer — no UI
    # component or backend decision read them, and they pure-duplicated the
    # per-direction fields already surfaced in DirectionCard. Removed to satisfy
    # the "every emitted metric has a named consumer" invariant.
    # Retired: generalized success / health / outcome-kind rollups were computed here
    # but never surfaced to /api/admin/digest or any UI consumer.
    overall_type_distribution: Dict[str, int] = {}
    for d in direction_progress_list:
        for ttype, cnt in (d.get("type_distribution") or {}).items():
            overall_type_distribution[ttype] = (
                overall_type_distribution.get(ttype, 0) + cnt
            )

    pipeline_filter_funnel: Dict[str, int] = {}
    for d in direction_progress_list:
        ff = d.get("filter_funnel") or {}
        for _k, _v in ff.items():
            pipeline_filter_funnel[_k] = pipeline_filter_funnel.get(_k, 0) + (_v or 0)

    # LLM-dollar companions: total LLM spend, LLM share of combined (VM+LLM) spend,
    # and median LLM cost per merged PR. Uses the same pricing helpers as the
    # efficiency snapshot and llm-cost-breakdown endpoint so all figures reconcile.
    from app.llm_cost_attribution import _task_llm_cost

    _total_llm_cost_raw = 0.0
    _merged_llm_costs: list[float] = []
    for t in all_tasks:
        if not (t.vm_stopped_at is None or t.vm_stopped_at >= window_start):
            continue
        llm_cost, _unpriced = _task_llm_cost(t)
        _total_llm_cost_raw += llm_cost
        if has_landed_signal(t):
            _merged_llm_costs.append(llm_cost)
    total_llm_cost_usd = round(_total_llm_cost_raw, 4)
    _combined_raw = _total_compute_cost_raw + _total_llm_cost_raw
    llm_share_of_combined_cost = (
        round(_total_llm_cost_raw / _combined_raw, 4) if _combined_raw > 0 else None
    )
    median_llm_cost_per_merged_pr = (
        round(statistics.median(_merged_llm_costs), 4) if _merged_llm_costs else None
    )

    return {
        "total_estimated_cost_usd": total_estimated_cost_usd,
        "total_spot_cost_usd": total_spot_cost_usd,
        "total_on_demand_cost_usd": total_on_demand_cost_usd,
        "provisioning_overhead_usd": provisioning_overhead_usd,
        "provisioning_overhead_pct": provisioning_overhead_pct,
        "daily_cost_usd": daily_cost_usd,
        "daily_vm_cost_usd": daily_vm_cost_usd,
        "daily_compute_cost_usd": daily_compute_cost_usd,
        "daily_cost_ceiling_usd": daily_cost_ceiling_usd,
        "overall_product_ratio": overall_product_ratio,
        "median_cost_per_merged_pr": median_cost_per_merged_pr,
        "cost_per_merge_by_type": cost_per_merge_by_type,
        "median_cpm_by_type": median_cpm_by_type,
        "pipeline_acceptance_rate": pipeline_acceptance_rate,
        "pipeline_conversion_rate": pipeline_conversion_rate,
        # Retired: pipeline_autonomous_prs_merged / pipeline_assisted_prs_merged /
        # pipeline_operator_action_count (consumer-less digest duplicates of the
        # per-direction outcome fields — see retirement note above).
        "overall_type_distribution": overall_type_distribution,
        "pipeline_filter_funnel": pipeline_filter_funnel,
        # LLM-dollar companions (additive; existing VM-only fields unchanged).
        "total_llm_cost_usd": total_llm_cost_usd,
        "llm_share_of_combined_cost": llm_share_of_combined_cost,
        "median_llm_cost_per_merged_pr": median_llm_cost_per_merged_pr,
    }


def compute_churn_direction(buckets: List[Dict[str, Any]]) -> str:
    """Canonical churn-trend direction for a list of per-day trend buckets.

    Single source of truth shared by the narrative verdict
    (compute_narrative_summary) and the dashboard widget glyph (consumed via
    the ``churn_direction`` field on the trend endpoint). Both surfaces MUST
    derive direction here so the lifted "Review/fix churn is X." sentence can
    never contradict the rising/falling glyph rendered beside it.

    Compares the first-half vs second-half AVERAGE of per-day churn (robust to
    single-day endpoint anomalies) with a 1% threshold to avoid flip-flopping
    on near-equal halves. Returns 'falling' | 'rising' | 'flat'.
    """
    if len(buckets) < 2:
        return "flat"

    def _bucket_churn(bucket: Dict[str, Any]) -> float:
        incidence = bucket.get("incidence_counts") or {}
        churn = incidence.get("churn")
        if churn is None:
            churn = bucket.get("churn_total")
        return float(churn or 0)

    midpoint = math.ceil(len(buckets) / 2)
    first_half = buckets[:midpoint]
    second_half = buckets[midpoint:]
    avg_first = sum(_bucket_churn(b) for b in first_half) / len(first_half)
    avg_second = sum(_bucket_churn(b) for b in second_half) / len(second_half)

    threshold = 0.01 * max(avg_first, avg_second, 1)
    difference = avg_second - avg_first
    if difference < -threshold:
        return "falling"
    if difference > threshold:
        return "rising"
    return "flat"


# ---------------------------------------------------------------------------
# Durable, windowed AGENT_TIMEOUT-rate measure (direction d-46468a41).
#
# The equipping lane needs a VALIDATED pipeline measure to cite a real
# before/after delta on. AGENT_TIMEOUT is the most-flagged failure class, so the
# headline measure is its windowed RATE, decomposed by the seven persisted
# subtypes. The dominant subtype routes the operator failure-guidance line to a
# known OSS countermeasure (oom -> memory/footprint; execution_timeout ->
# long-command/tool budgets a la SWE-agent; heartbeat_lost/process_crash ->
# liveness/checkpoint a la OpenHands; iteration_limit -> scope/condensation a la
# Aider).
#
# GROUND-TRUTH RULE: this measure reads ONLY persisted per-task ground truth —
# ``ctx['failure_category']`` (the numerator class) and ``ctx['timeout_subtype']``
# (the subtype) — both stamped at terminal-FAILED time by
# ``failure_classification.persist_failure_category``. It NEVER calls
# ``classify_timeout_subtype`` / ``classify_failure`` (which DERIVE from
# log/status text), so the rate is stable against later status-message rewrites
# and is not gameable by re-derivation.
# ---------------------------------------------------------------------------

# The seven persisted AGENT_TIMEOUT subtypes, in a fixed order so every emitted
# rate block carries all seven keys (zero-filled) and the manifest can enumerate
# them deterministically.
TIMEOUT_SUBTYPES: tuple[str, ...] = (
    TIMEOUT_HEARTBEAT_LOST,
    TIMEOUT_OOM,
    TIMEOUT_EXECUTION,
    TIMEOUT_CASCADE,
    TIMEOUT_ITERATION_LIMIT,
    TIMEOUT_PROCESS_CRASH,
    TIMEOUT_UNKNOWN,
)

# Window for the rate measure — same length as the efficiency axes so the daily
# snapshot it folds into reads apples-to-apples with the other axes.
_TIMEOUT_RATE_WINDOW_DAYS = 7

# Confident-dominance gate for the operator guidance line: a subtype is reported
# as the dominant cause only when it covers >=2 timeouts AND >=60% of them, and
# is not the catch-all "unknown" bucket. Mirrors the long-standing narrative gate
# in :func:`_compute_dominant_timeout_subtype` so switching the narrative to
# persisted ground truth does not change WHEN advice is refined, only its source.
_TIMEOUT_DOMINANT_MIN_COUNT = 2
_TIMEOUT_DOMINANT_MIN_SHARE = 0.6


def _persisted_timeout_subtype(task: Task) -> str | None:
    """Return a task's persisted timeout subtype, or None if it is not a timeout.

    Reads ONLY the durable ground truth ``ctx['failure_category']`` /
    ``ctx['timeout_subtype']`` — never re-derives from logs. A task whose
    persisted ``failure_category`` is not AGENT_TIMEOUT returns None; an
    AGENT_TIMEOUT task with no recorded subtype collapses to ``unknown`` (the
    same fallback the writer uses), so it is still counted in the numerator.
    """
    ctx = task.context or {}
    if not isinstance(ctx, dict):
        return None
    if ctx.get("failure_category") != AGENT_TIMEOUT:
        return None
    subtype = ctx.get("timeout_subtype") or TIMEOUT_UNKNOWN
    return subtype if subtype in TIMEOUT_SUBTYPES else TIMEOUT_UNKNOWN


def _dominant_persisted_timeout(
    tasks: List[Task],
) -> tuple[str | None, float | None, int]:
    """Return ``(dominant_subtype, dominant_share, timeout_count)`` from ground truth.

    ``dominant_subtype`` is the most common persisted subtype among the timeout
    tasks (None when there are no timeouts); ``dominant_share`` is its fraction of
    all timeouts. No confidence gate is applied here — that is the consumer's job
    (see :func:`confident_dominant_timeout_subtype`).
    """
    counts: Dict[str, int] = {}
    total = 0
    for t in tasks:
        subtype = _persisted_timeout_subtype(t)
        if subtype is None:
            continue
        total += 1
        counts[subtype] = counts.get(subtype, 0) + 1
    if not counts:
        return None, None, 0
    dominant, dominant_count = max(counts.items(), key=lambda kv: kv[1])
    return dominant, round(dominant_count / total, 4), total


def confident_dominant_timeout_subtype(tasks: List[Task]) -> str | None:
    """Persisted dominant timeout subtype, gated for the operator guidance line.

    Returns the subtype only when it covers >=2 timeouts AND >=60% of them and is
    not ``unknown`` — otherwise None (no confident single cause to route advice
    to). Reads persisted ground truth via :func:`_persisted_timeout_subtype`, so
    the digest AGENT_TIMEOUT guidance is driven by the same durable signal the
    snapshot measure persists, not by log re-derivation.
    """
    dominant, share, count = _dominant_persisted_timeout(tasks)
    if dominant is None or share is None:
        return None
    dominant_count = round(share * count)
    if (
        dominant_count >= _TIMEOUT_DOMINANT_MIN_COUNT
        and share >= _TIMEOUT_DOMINANT_MIN_SHARE
        and dominant != TIMEOUT_UNKNOWN
    ):
        return dominant
    return None


def compute_timeout_rate(
    tasks: List[Task], *, window_days: int = _TIMEOUT_RATE_WINDOW_DAYS
) -> Dict[str, Any]:
    """Windowed AGENT_TIMEOUT rate, decomposed by persisted subtype.

    DENOMINATOR — ``execution_or_timed_out``: terminal tasks in the window that
    either reached execution (carry a ``started_at`` timestamp) or persisted as
    AGENT_TIMEOUT without reaching execution. This makes the rate a fraction over
    tasks that COULD have timed out (a never-started AGENT_TIMEOUT is still a task
    that timed out, just before dispatching), so numerator and denominator remain
    consistent and rates are not inflated by never-dispatched volume. Callers pass
    the already-windowed terminal task set (e.g. efficiency_snapshot._load_recent_tasks);
    ``tasks`` is not re-filtered by time here.

    NUMERATOR — ``timeout_count``: tasks whose persisted
    ``ctx['failure_category']`` is AGENT_TIMEOUT. Read from durable ground truth
    only — never re-derived from logs.

    Returns a block with the overall rate, a per-subtype ``{count, rate}`` map
    covering all seven subtypes (rate over the SAME execution-or-timed-out denominator,
    so the per-subtype rates sum to the overall rate), and the dominant subtype +
    its share among timeouts. ``overall_rate`` / per-subtype ``rate`` are None
    only when the denominator is 0 (no tasks reached execution or timed out —
    rate undefined, distinct from a real 0.0). ``window_days`` is echoed for provenance.
    """
    denominator = sum(
        1 for t in tasks
        if getattr(t, "started_at", None) is not None or _persisted_timeout_subtype(t) is not None
    )
    subtype_counts: Dict[str, int] = {s: 0 for s in TIMEOUT_SUBTYPES}
    timeout_count = 0
    for t in tasks:
        subtype = _persisted_timeout_subtype(t)
        if subtype is None:
            continue
        timeout_count += 1
        subtype_counts[subtype] += 1

    def _rate(n: int) -> float | None:
        if denominator <= 0:
            return None
        return round(n / denominator, 4)

    dominant, dominant_share, _ = _dominant_persisted_timeout(tasks)

    return {
        "window_days": window_days,
        "denominator": denominator,
        "denominator_definition": (
            "terminal tasks in the window that either reached execution (started_at "
            "present) or timed out before execution (persisted AGENT_TIMEOUT without "
            "started_at); a task in this set could have timed out"
        ),
        "timeout_count": timeout_count,
        "overall_rate": _rate(timeout_count),
        "by_subtype": {
            subtype: {
                "count": subtype_counts[subtype],
                "rate": _rate(subtype_counts[subtype]),
            }
            for subtype in TIMEOUT_SUBTYPES
        },
        "dominant_subtype": dominant,
        "dominant_share": dominant_share,
    }


# ---------------------------------------------------------------------------
# Metric-governance manifest for the AGENT_TIMEOUT-rate measure.
#
# Mirrors RESEARCH_HISTORY_METRIC_MANIFEST in direction_outcomes.py: every field
# the measure emits declares EITHER a named ground-truth source + named consumer,
# OR a dated pending/retired reason. The guard test
# ``backend/testing/test_timeout_rate_metric_manifest.py`` AST-extracts the dict
# keys ``compute_timeout_rate`` actually returns and asserts they all appear here
# (and that no manifest "consumer" key has stopped being emitted), so the measure
# cannot silently rot — a new emitted field cannot land without declaring who
# reads it and where its ground truth comes from.
#
# ``ground_truth`` names the durable source each field is computed from (so the
# manifest documents that the measure reads persisted per-task truth, not logs).
# The per-subtype rates live under the ``by_subtype`` key, which is declared once
# here (its inner shape is fixed: one ``{count, rate}`` entry per TIMEOUT_SUBTYPES
# member).
# ---------------------------------------------------------------------------
TIMEOUT_RATE_METRIC_MANIFEST: dict[str, dict] = {
    "window_days": {
        "ground_truth": "constant _TIMEOUT_RATE_WINDOW_DAYS (provenance, not measured)",
        "consumer": "efficiency_snapshot.read_baseline_with_drift (agent_timeout_rate block provenance)",
    },
    "denominator": {
        "ground_truth": "started_at IS NOT NULL OR persisted AGENT_TIMEOUT subtype (execution-or-timed-out population) over the windowed terminal task set",
        "consumer": "efficiency-axes response (agent_timeout_rate.current denominator), strategic layer",
    },
    "denominator_definition": {
        "ground_truth": "static documentation string (no measured input)",
        "consumer": "efficiency-axes response (operator-facing denominator definition)",
    },
    "timeout_count": {
        "ground_truth": "persisted ctx['failure_category'] == AGENT_TIMEOUT",
        "consumer": "efficiency-axes response (agent_timeout_rate numerator)",
    },
    "overall_rate": {
        "ground_truth": "timeout_count / execution-or-timed-out denominator (both persisted ground truth)",
        "consumer": "digest narrative AGENT_TIMEOUT before/after sentence (admin_digest.py), efficiency-axes agent_timeout_rate.overall drift",
    },
    "by_subtype": {
        "ground_truth": "persisted ctx['timeout_subtype'] per AGENT_TIMEOUT task, over the execution-or-timed-out denominator",
        "consumer": "efficiency-axes agent_timeout_rate.by_subtype drift (per-subtype current-vs-baseline)",
    },
    "dominant_subtype": {
        "ground_truth": "most common persisted ctx['timeout_subtype'] among window timeouts",
        "consumer": "digest narrative AGENT_TIMEOUT guidance line via confident_dominant_timeout_subtype (admin_digest.py / compute_narrative_summary)",
    },
    "dominant_share": {
        "ground_truth": "dominant subtype count / timeout_count (persisted ground truth)",
        "consumer": "efficiency-axes agent_timeout_rate.dominant_share (operator + strategic context)",
    },
}


def _format_timeout_rate_drift(drift: Dict[str, Any] | None) -> str | None:
    """Render the AGENT_TIMEOUT-rate before/after sentence from the overall block.

    ``drift`` is the ``{current, baseline, drift_pct}`` overall block from the
    persisted measure (efficiency_snapshot.read_baseline_with_drift). Returns a
    sentence like ``"Agent-timeout rate 8%, down from a baseline of 12%."`` when
    both current and baseline are present, or None when there is nothing durable
    to report yet (no snapshot/baseline). Rates are stored as fractions (0..1)
    and rendered as whole-number percentages.
    """
    if not isinstance(drift, dict):
        return None
    current = drift.get("current")
    baseline = drift.get("baseline")
    if not isinstance(current, (int, float)) or not isinstance(baseline, (int, float)):
        return None
    cur_pct = round(current * 100)
    base_pct = round(baseline * 100)
    if cur_pct < base_pct:
        direction = "down"
    elif cur_pct > base_pct:
        direction = "up"
    else:
        direction = "unchanged"
    if direction == "unchanged":
        return f"Agent-timeout rate {cur_pct}%, unchanged from baseline."
    return f"Agent-timeout rate {cur_pct}%, {direction} from a baseline of {base_pct}%."


def compute_narrative_summary(
    *,
    hours: int,
    project_id: uuid.UUID | None,
    completed_count: int,
    failed_count: int,
    pending_approval_count: int,
    needs_attention: List[Dict[str, Any]],
    failure_groups: List[Dict[str, Any]],
    failure_taxonomy_distribution: Dict[str, Any],
    trend: str | None,
    completed_delta: int,
    prev_completed_count: int,
    sorted_completions: List[Task],
    sorted_failures: List[Task],
    direction_progress_list: List[Dict[str, Any]],
    running_count: int,
    queued_count: int,
    pr_count: int,
    error_log_map: Dict[str, str],
    failed_tasks: List[Task],
    outer_loop_convergence_summary: Dict[str, Any] | None = None,
    outer_loop_convergence_trend: Dict[str, Any] | None = None,
    timeout_rate_drift: Dict[str, Any] | None = None,
) -> str:
    """Build the human-readable digest narrative from precomputed metrics.

    ``timeout_rate_drift`` is the durable AGENT_TIMEOUT-rate overall block
    (``{current, baseline, drift_pct}``) read off the efficiency snapshot via
    ``read_baseline_with_drift``. When the dominant failure class is
    AGENT_TIMEOUT and this block carries a current+baseline, a before/after
    sentence is appended reporting the rate's drift from baseline so the operator
    sees whether the measure moved, not just the in-window count.
    """
    from app.failure_classification import (
        classify_failure as _classify_failure,
        classify_failure_taxonomy as _classify_taxonomy,
        TAXONOMY_REVIEW_QUALITY,
        TAXONOMY_SCOPE_RELATED,
        TAXONOMY_CODE_DEFECT,
    )

    narrative_parts = []

    if pending_approval_count == 0 and failed_count == 0 and completed_count > 0:
        all_clear = f"All clear — {completed_count} task{'s' if completed_count != 1 else ''} completed"
        if pr_count > 0:
            all_clear += f" with {pr_count} PR{'s' if pr_count != 1 else ''} merged"
        all_clear += ", no action needed."
        return all_clear

    if pending_approval_count > 0:
        approval_items = [
            x for x in needs_attention if x["reason"] == "approval_required"
        ]
        max_wait = max((x.get("waiting_hours") or 0 for x in approval_items), default=0)
        approval_str = f"{pending_approval_count} task{'s' if pending_approval_count != 1 else ''} awaiting your approval"
        if max_wait > 0:
            approval_str += f" ({max_wait}h)"
        narrative_parts.append(approval_str + ".")

    if failed_count > 0:
        dominant_group = failure_groups[0] if failure_groups else None
        dominant_msg = None
        if (
            dominant_group
            and dominant_group["count"] >= 2
            and dominant_group["count"] / failed_count >= 0.6
        ):
            cls = dominant_group["failure_class"]
            cnt = dominant_group["count"]
            _root_cause_msgs = {
                "RATE_LIMIT": f"Most failures ({cnt}/{failed_count}) are rate-limit errors — consider pausing until account recovery.",
                "PROVISION_FAILURE": f"Most failures ({cnt}/{failed_count}) are provisioning issues — check GCP quota and zone health.",
                "EXTERNAL_API": f"Most failures ({cnt}/{failed_count}) are external API errors — GitHub/Claude API may be experiencing issues.",
                "AGENT_TIMEOUT": f"Most failures ({cnt}/{failed_count}) are agent timeouts — tasks may need complexity reduction.",
                "CODE_ERROR": f"Most failures ({cnt}/{failed_count}) are code errors in agent-written code.",
                "SCOPE_OVERFLOW": f"Most failures ({cnt}/{failed_count}) exceeded scope budget (touched more files than scope allows) — consider task decomposition or scope escalation.",
            }
            if cls == "AGENT_TIMEOUT":
                # Drive the refined guidance line from PERSISTED ground truth
                # (ctx['failure_category'] / ctx['timeout_subtype']) via the same
                # durable signal the snapshot measure persists — not from the
                # log-derived subtype on the failure-grouping entries.
                _timeout_subtype = confident_dominant_timeout_subtype(failed_tasks)
                if _timeout_subtype:
                    _timeout_msgs = {
                        "oom": f"Most failures ({cnt}/{failed_count}) are out-of-memory timeouts — check task memory usage and consider memory/footprint optimization.",
                        "heartbeat_lost": f"Most failures ({cnt}/{failed_count}) are heartbeat-timeout errors — may indicate hangs or liveness issues.",
                        "process_crash": f"Most failures ({cnt}/{failed_count}) are process crashes — may indicate hangs or liveness issues.",
                        "execution_timeout": f"Most failures ({cnt}/{failed_count}) are execution timeouts — tasks may need complexity reduction or scope adjustment.",
                        "iteration_limit": f"Most failures ({cnt}/{failed_count}) hit iteration limits — tasks may need complexity reduction or scope adjustment.",
                        "cascade_cancellation": f"Most failures ({cnt}/{failed_count}) are cascade cancellations — check upstream dependencies and parent task status.",
                    }
                    _refined_msg = _timeout_msgs.get(_timeout_subtype)
                    if _refined_msg:
                        _root_cause_msgs["AGENT_TIMEOUT"] = _refined_msg
            if cls == "CODE_ERROR":
                _ce_tax: Dict[str, int] = {}
                for _t in failed_tasks:
                    if (
                        _classify_failure(_t, error_log_map.get(str(_t.id)))
                        == "CODE_ERROR"
                    ):
                        _sub = _classify_taxonomy(_t)
                        _ce_tax[_sub] = _ce_tax.get(_sub, 0) + 1
                if _ce_tax:
                    _sub_dom, _sub_cnt = max(_ce_tax.items(), key=lambda kv: kv[1])
                    if _sub_cnt / cnt >= 0.6:
                        _sub_msgs = {
                            TAXONOMY_SCOPE_RELATED: f"Most failures ({cnt}/{failed_count}) are code errors driven by scope budget overruns — consider task decomposition or scope escalation.",
                            TAXONOMY_REVIEW_QUALITY: f"Most failures ({cnt}/{failed_count}) are code errors from test/lint failures — strengthen self-review before PR creation.",
                            TAXONOMY_CODE_DEFECT: f"Most failures ({cnt}/{failed_count}) are logical code defects in agent-written code.",
                        }
                        _refined = _sub_msgs.get(_sub_dom)
                        if _refined:
                            _root_cause_msgs["CODE_ERROR"] = _refined
            dominant_msg = _root_cause_msgs.get(cls)
            # Before/after sentence: when agent-timeouts dominate, report the
            # durable AGENT_TIMEOUT-rate measure's current-vs-baseline drift so
            # the operator sees whether the validated pipeline measure actually
            # moved, not just the in-window absolute count.
            if cls == "AGENT_TIMEOUT" and dominant_msg:
                _drift_sentence = _format_timeout_rate_drift(timeout_rate_drift)
                if _drift_sentence:
                    dominant_msg = f"{dominant_msg} {_drift_sentence}"
        if dominant_msg:
            narrative_parts.append(dominant_msg)
        else:
            failure_titles = [t.title or "Failed task" for t in sorted_failures[:2]]
            failed_str = f"{failed_count} task{'s' if failed_count != 1 else ''} failed"
            if failure_titles:
                failed_str += f" ({', '.join(failure_titles)})"
            narrative_parts.append(failed_str + ".")

        _dom_cat = failure_taxonomy_distribution.get("dominant_category")
        _tax_tot = failure_taxonomy_distribution.get("total_failures") or 0
        if _dom_cat and _tax_tot >= 2:
            _dom_count = failure_taxonomy_distribution["categories"][_dom_cat]["count"]
            if _dom_count / _tax_tot >= 0.5:
                _taxonomy_msgs = {
                    "review_quality": "Review quality issues dominate failures — improving lint/test coverage may help.",
                    "scope_related": "Scope mismatch is the top failure pattern — consider task decomposition.",
                    "infra_related": "Infrastructure issues dominate — check provisioning and rate limits.",
                    "code_defect": "Code defects dominate failures — strengthening self-review may help.",
                }
                _tax_msg = _taxonomy_msgs.get(_dom_cat)
                if _tax_msg:
                    narrative_parts.append(_tax_msg)

    if outer_loop_convergence_summary and outer_loop_convergence_trend:
        buckets = outer_loop_convergence_trend.get("buckets") or []
        # Consume the SINGLE canonical churn direction. In production the trend
        # endpoint already stamps `churn_direction` via compute_churn_direction;
        # when a caller passes raw buckets without it (e.g. unit tests) fall back
        # to the SAME helper so the verdict can never diverge from the widget
        # glyph, which derives from the identical algorithm.
        churn_dir = outer_loop_convergence_trend.get("churn_direction")
        if churn_dir not in {"falling", "rising", "flat"}:
            churn_dir = compute_churn_direction(buckets)
        if len(buckets) >= 2:
            has_churn_signal = any(
                (
                    b.get("incidence_counts", {}).get("churn")
                    or b.get("churn_total")
                    or 0
                )
                > 0
                for b in buckets
            )
            if has_churn_signal:
                verdict = f"Review/fix churn is {churn_dir}"
                dominant_blocker = outer_loop_convergence_summary.get(
                    "dominant_blocker"
                )
                if (
                    dominant_blocker
                    and dominant_blocker.get("count")
                    and dominant_blocker.get("total")
                ):
                    verdict += (
                        f"; the main blocker is {dominant_blocker['cause'].replace('_', ' ')}"
                        f" ({dominant_blocker['count']}/{dominant_blocker['total']} non-converged PR chains)"
                    )
                narrative_parts.append(verdict + ".")

    if completed_count > 0:
        trend_prefix = ""
        if trend == "declining":
            trend_prefix = "Pipeline health declining — "
        elif trend == "improving":
            trend_prefix = "Good progress — "
        shipped = f"{trend_prefix}{completed_count} task{'s' if completed_count != 1 else ''} completed"
        if pr_count > 0:
            shipped += f" with {pr_count} PR{'s' if pr_count != 1 else ''} opened"
        completion_titles = [
            t.title or "Completed task" for t in sorted_completions[:2]
        ]
        completion_titles = [
            t[:50] + "..." if len(t) > 50 else t for t in completion_titles
        ]
        if completion_titles:
            shipped += f" including {', '.join(completion_titles)}"
        if completed_delta > 0:
            shipped += f" (up from {prev_completed_count} last period)"
        elif completed_delta < 0:
            shipped += f" (down from {prev_completed_count} last period)"
        narrative_parts.append(shipped + ".")

    if direction_progress_list:
        gate_challenged_spawned = 0
        gate_challenged_merged = 0
        gate_unchallenged_spawned = 0
        gate_unchallenged_merged = 0
        gate_refuted_count = 0
        for dp in direction_progress_list:
            gate = dp.get("research_spawn_gate") or {}
            gate_challenged_spawned += int(gate.get("challenged_spawned") or 0)
            gate_challenged_merged += int(gate.get("challenged_merged") or 0)
            gate_unchallenged_spawned += int(gate.get("unchallenged_spawned") or 0)
            gate_unchallenged_merged += int(gate.get("unchallenged_merged") or 0)
            gate_refuted_count += int(gate.get("refuted_count") or 0)
        challenged_rate = (
            round(gate_challenged_merged / gate_challenged_spawned, 3)
            if gate_challenged_spawned > 0
            else None
        )
        unchallenged_rate = (
            round(gate_unchallenged_merged / gate_unchallenged_spawned, 3)
            if gate_unchallenged_spawned > 0
            else None
        )
        if challenged_rate is not None and unchallenged_rate is not None:
            narrative_parts.append(
                "Before/after, research spawn gate: challenged-and-survived spawns "
                f"merged at {challenged_rate:.1%} versus {unchallenged_rate:.1%} "
                f"for unchallenged spawns; {gate_refuted_count} refuted finding"
                f"{'s' if gate_refuted_count != 1 else ''} were prevented from spawning."
            )

    if project_id and direction_progress_list:
        _dir_tax_hints = {
            "review_quality": "mostly review-quality failures",
            "scope_related": "mostly scope-related failures",
            "infra_related": "mostly infra-related failures",
            "code_defect": "mostly code-defect failures",
        }
        dir_summaries = []
        for dp in direction_progress_list:
            dt = (dp["direction_text"] or "")[:40]
            if dp["prs_merged"] > 0:
                entry = f"{dt}: {dp['prs_merged']} PR{'s' if dp['prs_merged'] != 1 else ''} merged"
            elif dp.get("possibly_saturated"):
                entry = f"{dt}: may be saturated"
            elif dp["tasks_created"] > 0:
                entry = f"{dt}: {dp['tasks_created']} task{'s' if dp['tasks_created'] != 1 else ''} in progress"
            else:
                continue
            _tax: Dict[str, int] = dp.get("taxonomy_summary") or {}
            _tax_total = sum(_tax.values())
            if _tax_total >= 2:
                _dom = max(_tax, key=lambda k: _tax[k])
                if _tax[_dom] / _tax_total >= 0.5:
                    _hint = _dir_tax_hints.get(_dom)
                    if _hint:
                        entry += f" ({_hint})"
            dir_summaries.append(entry)
        if dir_summaries:
            narrative_parts.append("Research: " + "; ".join(dir_summaries) + ".")

    if running_count > 0 or queued_count > 0:
        pipeline_parts = []
        if running_count > 0:
            pipeline_parts.append(f"{running_count} running")
        if queued_count > 0:
            pipeline_parts.append(f"{queued_count} queued")
        narrative_parts.append(", ".join(pipeline_parts) + ".")

    if narrative_parts:
        return " ".join(narrative_parts)
    return f"All quiet — no activity in the last {hours}h."


def compute_scope_mismatch_summary(
    *,
    completed_tasks: List[Task],
    failed_tasks: List[Task],
    dir_id_to_text: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Aggregate scope-calibration data across terminal tasks in the window.

    Surfaces systematic scope-estimation drift and chronic-calibration
    directions: counts calibrated tasks, computes their success rate, builds a
    breakdown keyed by original declared scope, and ranks the top directions by
    calibration rate. Pure function — reads only the already-loaded
    completed/failed task lists and the direction-id-to-text map; no database
    calls. Returns the summary dict, or None when no calibrated tasks exist in
    the window.
    """
    terminal_in_window = completed_tasks + failed_tasks
    calibrated_tasks = [
        t for t in terminal_in_window if (t.context or {}).get("scope_calibrated_from")
    ]
    if not calibrated_tasks:
        return None

    cal_completed = sum(1 for t in calibrated_tasks if t.status == TaskStatus.COMPLETED)
    cal_total = len(calibrated_tasks)
    by_orig: Dict[str, Dict[str, int]] = {}
    by_dir_calibrated: Dict[str, int] = {}
    for t in calibrated_tasks:
        orig_raw = str((t.context or {}).get("scope_calibrated_from") or "")
        orig = orig_raw.split(":", 1)[0] or "unknown"
        bucket = by_orig.setdefault(orig, {"total": 0, "completed": 0, "failed": 0})
        bucket["total"] += 1
        if t.status == TaskStatus.COMPLETED:
            bucket["completed"] += 1
        elif t.status == TaskStatus.FAILED:
            bucket["failed"] += 1
        did = (t.context or {}).get("direction_id")
        if did:
            by_dir_calibrated[str(did)] = by_dir_calibrated.get(str(did), 0) + 1

    by_dir_total: Dict[str, int] = {}
    for t in terminal_in_window:
        did = (t.context or {}).get("direction_id")
        if did:
            by_dir_total[str(did)] = by_dir_total.get(str(did), 0) + 1

    top_dirs = []
    for did, cal_cnt in by_dir_calibrated.items():
        tot = by_dir_total.get(did, 0)
        if tot <= 0:
            continue
        top_dirs.append(
            {
                "direction_id": did,
                "direction_text": dir_id_to_text.get(did, ""),
                "calibrated_count": cal_cnt,
                "total_tasks": tot,
                "calibration_rate": round(cal_cnt / tot, 3),
            }
        )
    top_dirs.sort(
        key=lambda d: (d["calibration_rate"], d["calibrated_count"]), reverse=True
    )

    return {
        "total_calibrated": cal_total,
        "calibrated_success_rate": round(cal_completed / cal_total, 3)
        if cal_total
        else None,
        "by_original_scope": by_orig,
        "top_directions": top_dirs[:3],
    }


def compute_retry_analytics(
    *,
    failed_tasks: List[Task],
    completed_tasks: List[Task],
    dir_id_to_text: Dict[str, str],
    window_start: "datetime",
) -> Dict[str, Any]:
    """Aggregate retry-pattern cost and outcome analytics from task lists.

    Pure function — no database calls. Walks failed_tasks and completed_tasks
    reading context fields _attempt_number, _previous_failure_class, and
    _prior_attempts_cost to produce retry cost, waste, and success metrics.

    Returns a dict with keys:
      retry_overhead_cost_usd, retry_recovery_cost_usd,
      llm_retry_overhead_cost_usd, llm_retry_recovery_cost_usd,
      retry_waste_by_direction (top-5), retry_task_count,
      retry_success_count, avg_retries_before_failure,
      retry_success_rate_by_class, retry_wasted_rate.
    """
    from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE
    from app.llm_cost_attribution import _task_llm_cost

    _retry_overhead_cost = 0.0
    _retry_overhead_llm_cost = 0.0
    _retry_task_count = 0
    _retry_attempt_sum = 0
    _retry_waste_by_dir: Dict[str, Dict[str, Any]] = {}
    for t in failed_tasks:
        _attempt = (t.context or {}).get("_attempt_number") or 0
        if _attempt > 1:
            _retry_task_count += 1
            _retry_attempt_sum += _attempt - 1
            _task_retry_cost = 0.0
            if (
                t.vm_provisioned_at
                and t.vm_stopped_at
                and t.vm_stopped_at >= window_start
            ):
                _rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
                _task_retry_cost = (
                    _rate * (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
                )
                _retry_overhead_cost += _task_retry_cost
            try:
                _task_llm_cost_val, _ = _task_llm_cost(t)
                _retry_overhead_llm_cost += _task_llm_cost_val
            except Exception:
                pass
            _rw_did = (t.context or {}).get("direction_id")
            if _rw_did:
                _key = str(_rw_did)
                _entry = _retry_waste_by_dir.setdefault(
                    _key,
                    {
                        "direction_id": _key,
                        "direction_text": dir_id_to_text.get(_key, ""),
                        "retry_cost_usd": 0.0,
                        "retry_task_count": 0,
                    },
                )
                _entry["retry_cost_usd"] += _task_retry_cost
                _entry["retry_task_count"] += 1

    retry_overhead_cost_usd = round(_retry_overhead_cost, 4)
    llm_retry_overhead_cost_usd = round(_retry_overhead_llm_cost, 4)
    retry_waste_by_direction = sorted(
        [
            {**e, "retry_cost_usd": round(e["retry_cost_usd"], 4)}
            for e in _retry_waste_by_dir.values()
        ],
        key=lambda x: x["retry_cost_usd"],
        reverse=True,
    )[:5]
    retry_task_count = _retry_task_count
    avg_retries_before_failure = (
        round(_retry_attempt_sum / _retry_task_count, 2) if _retry_task_count else None
    )

    _retry_success_by_class: Dict[str, int] = {}
    _retry_failure_by_class: Dict[str, int] = {}
    _retry_success_count = 0
    _retry_recovery_cost = 0.0
    _retry_recovery_llm_cost = 0.0
    for t in completed_tasks:
        _ctx = t.context or {}
        if (_ctx.get("_attempt_number") or 0) > 1:
            _retry_success_count += 1
            _cls = _ctx.get("_previous_failure_class") or "UNKNOWN"
            _retry_success_by_class[_cls] = _retry_success_by_class.get(_cls, 0) + 1
            try:
                _retry_recovery_cost += float(_ctx.get("_prior_attempts_cost") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                _retry_recovery_llm_cost += float(
                    _ctx.get("_prior_attempts_llm_cost") or 0.0
                )
            except (TypeError, ValueError):
                pass
    retry_recovery_cost_usd = round(_retry_recovery_cost, 4)
    llm_retry_recovery_cost_usd = round(_retry_recovery_llm_cost, 4)
    for t in failed_tasks:
        _ctx = t.context or {}
        if (_ctx.get("_attempt_number") or 0) > 1:
            _cls = _ctx.get("_previous_failure_class") or "UNKNOWN"
            _retry_failure_by_class[_cls] = _retry_failure_by_class.get(_cls, 0) + 1
    _retry_classes = set(_retry_success_by_class) | set(_retry_failure_by_class)
    retry_success_rate_by_class = {
        _cls: {
            "merged": _retry_success_by_class.get(_cls, 0),
            "failed": _retry_failure_by_class.get(_cls, 0),
            "success_rate": round(
                _retry_success_by_class.get(_cls, 0)
                / (
                    _retry_success_by_class.get(_cls, 0)
                    + _retry_failure_by_class.get(_cls, 0)
                ),
                4,
            ),
        }
        for _cls in _retry_classes
    }
    retry_success_count = _retry_success_count
    _total_retried = _retry_success_count + _retry_task_count
    retry_wasted_rate = (
        round(_retry_task_count / _total_retried, 4) if _total_retried else None
    )

    return {
        "retry_overhead_cost_usd": retry_overhead_cost_usd,
        "retry_recovery_cost_usd": retry_recovery_cost_usd,
        "llm_retry_overhead_cost_usd": llm_retry_overhead_cost_usd,
        "llm_retry_recovery_cost_usd": llm_retry_recovery_cost_usd,
        "retry_waste_by_direction": retry_waste_by_direction,
        "retry_task_count": retry_task_count,
        "retry_success_count": retry_success_count,
        "avg_retries_before_failure": avg_retries_before_failure,
        "retry_success_rate_by_class": retry_success_rate_by_class,
        "retry_wasted_rate": retry_wasted_rate,
    }


async def compute_release_qualifier_infra_metrics(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    window_start: datetime,
) -> Dict[str, Any]:
    """Aggregate release-qualifier audit-pipeline infra failure counters.

    Scans the immutable QualificationVerdict table (one row per finalized
    attempt) and returns infra counters over the digest window.  Using verdict
    history rather than ReleaseVersion.last_qualification means earlier infra
    attempts are not silently dropped when a later re-qualification succeeds.
    Returns zeroed counters when no qualifying rows exist.

    Counters:
      qualifier_runs_total                       — all finalized qualifications in window
      release_infra_failure_total                — subset with status==infra_failure
      audit_report_missing_count                 — error==audit_report_missing
      audit_report_parseable_but_unscoreable_count — error==audit_report_parseable_but_unscoreable
      smoketest_report_missing_count             — error==smoketest_report_missing
      release_infra_failure_streak_max           — highest consecutive_infra_errors seen
      release_infra_failure_repeat_signature_total — infra failures matching prior signature
      release_infra_failure_repeat_different_candidate_total — repeat signature on new candidate
    """
    stmt = select(QualificationVerdict).where(
        QualificationVerdict.finalized_at >= window_start
    )
    if project_id:
        stmt = stmt.where(QualificationVerdict.project_id == project_id)

    result = await session.execute(stmt)
    verdicts = result.scalars().all()

    qualifier_runs_total = 0
    release_infra_failure_total = 0
    audit_report_missing_count = 0
    audit_report_parseable_but_unscoreable_count = 0
    smoketest_report_missing_count = 0
    release_infra_failure_streak_max = 0
    release_infra_failure_repeat_signature_total = 0
    release_infra_failure_repeat_different_candidate_total = 0

    for verdict in verdicts:
        qualifier_runs_total += 1
        if verdict.terminal_status != "infra_failure":
            continue
        release_infra_failure_total += 1
        infra = (verdict.sub_scores or {}).get("infra_failure") or {}
        if infra.get("same_error_as_prior"):
            release_infra_failure_repeat_signature_total += 1
        if infra.get("same_error_different_candidate"):
            release_infra_failure_repeat_different_candidate_total += 1
        error = infra.get("error") or ""
        if error == "audit_report_missing":
            audit_report_missing_count += 1
        elif error == "audit_report_parseable_but_unscoreable":
            audit_report_parseable_but_unscoreable_count += 1
        elif error == "smoketest_report_missing":
            smoketest_report_missing_count += 1
        streak = int(infra.get("consecutive_infra_errors") or 0)
        if streak > release_infra_failure_streak_max:
            release_infra_failure_streak_max = streak

    return {
        "qualifier_runs_total": qualifier_runs_total,
        "release_infra_failure_total": release_infra_failure_total,
        "audit_report_missing_count": audit_report_missing_count,
        "audit_report_parseable_but_unscoreable_count": audit_report_parseable_but_unscoreable_count,
        "smoketest_report_missing_count": smoketest_report_missing_count,
        "release_infra_failure_streak_max": release_infra_failure_streak_max,
        "release_infra_failure_repeat_signature_total": release_infra_failure_repeat_signature_total,
        "release_infra_failure_repeat_different_candidate_total": release_infra_failure_repeat_different_candidate_total,
    }


# Minimum number of in-window samples before a task type's average actual cost is
# allowed to flag as over its declared ceiling. A type that has only run once or
# twice in the window is too noisy to call "chronically over budget".
TASK_TYPE_COST_ADHERENCE_MIN_SAMPLES = 3


def _cost_pct_float(values: List[float], p: int) -> Optional[float]:
    """Nearest-rank percentile over a list of floats (None when empty)."""
    if not values:
        return None
    s = sorted(values)
    return s[min(int(len(s) * p / 100), len(s) - 1)]


def compute_task_type_cost_adherence(
    tasks: List["Task"],
    *,
    window_start: datetime,
    ceilings: Dict[str, Optional[float]],
    min_samples: int = TASK_TYPE_COST_ADHERENCE_MIN_SAMPLES,
    compute_job_cost_by_task: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """Per-task-type actual cost (avg/p95/sample count) vs the declared ceiling.

    Pure transform over already-loaded ``Task`` rows — no DB access. Buckets
    terminal, in-window tasks by their declared type (``first_payload['type']``)
    and compares the per-type average against the declared ``cost_ceiling_usd``
    for that type.

    Cost source is type-dependent so the surface is honest for off-VM types:

    * Task types that run on the **compute-job substrate** (registry predicate
      ``runs_on_compute_substrate`` — ``requires_compute AND NOT requires_code``,
      e.g. ``compute_experiment``) hold only a lightweight monitoring VM; their
      real spend lives on the linked ``ComputeJob.final_cost_usd``. For these we
      source cost from ``compute_job_cost_by_task`` (keyed by ``str(task.id)``),
      NOT from the task's VM fields. A task with no entry there — or a
      non-positive entry — contributes no sample (the compute job has not yet
      reported durable spend).
    * All other types use ``get_task_total_vm_cost_usd_or_none`` (which includes
      banked retry spend), as before.

    ``compute_job_cost_by_task`` maps ``str(task_id) -> summed final_cost_usd`` for
    that task's compute jobs; the caller aggregates it from the ``ComputeJob``
    ledger. ``None`` (the default) means "no compute-job ledger supplied" — every
    compute-substrate task then yields no sample, keeping legacy callers valid.

    ``ceilings`` maps task-type id -> declared ceiling (or None for types with no
    declared ceiling). The returned rows cover every type present in ``ceilings``
    so the operator catalog can render a row per registered type even when a type
    has no in-window runs (``sample_count == 0``). ``over_ceiling`` is True only
    once a type has at least ``min_samples`` observed runs AND its average exceeds
    a positive declared ceiling — a None or non-positive ceiling never flags, and
    a thin sample never flags.

    Tasks whose cost is None (never provisioned a VM, no banked spend) contribute
    no sample. The caller MUST load the columns the cost accessor reads
    (``vm_provisioned_at``, ``vm_stopped_at``, ``vm_spot``, ``vm_machine_type``,
    ``vm_runtime_cost_banked`` and ``context``) or SQLAlchemy will lazy-load them
    off an async session and raise MissingGreenlet.
    """
    from app.models import (
        TaskStatus,
        get_task_total_vm_cost_usd_or_none,
    )
    from app.task_capabilities import runs_on_compute_substrate

    compute_job_cost_by_task = compute_job_cost_by_task or {}
    _terminal = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
    costs_by_type: Dict[str, List[float]] = defaultdict(list)
    for t in tasks:
        if t.status not in _terminal:
            continue
        if t.created_at is None or t.created_at < window_start:
            continue
        task_type = (t.first_payload or {}).get("type", "create")
        if runs_on_compute_substrate(task_type):
            # Off-VM type: honest spend lives on the linked compute job, not the
            # lightweight monitoring VM. No (or non-positive) ledger entry => no
            # sample.
            cost = compute_job_cost_by_task.get(str(t.id))
            if cost is None or cost <= 0:
                continue
        else:
            cost = get_task_total_vm_cost_usd_or_none(t)
            if cost is None:
                continue
        costs_by_type[task_type].append(cost)

    rows: List[Dict[str, Any]] = []
    # Render a row for every registered type, plus any observed type not in the
    # registry (so unexpected types are still visible rather than silently dropped).
    type_ids = list(ceilings.keys())
    for extra in costs_by_type:
        if extra not in ceilings:
            type_ids.append(extra)

    for type_id in type_ids:
        samples = costs_by_type.get(type_id, [])
        ceiling = ceilings.get(type_id)
        sample_count = len(samples)
        avg_cost = round(sum(samples) / sample_count, 4) if sample_count else None
        p95_cost = _cost_pct_float(samples, 95)
        over_ceiling = bool(
            sample_count >= min_samples
            and ceiling is not None
            and ceiling > 0
            and avg_cost is not None
            and avg_cost > ceiling
        )
        rows.append(
            {
                "id": type_id,
                "declared_ceiling_usd": ceiling,
                "avg_cost_usd": avg_cost,
                "p95_cost_usd": round(p95_cost, 4) if p95_cost is not None else None,
                "sample_count": sample_count,
                "over_ceiling": over_ceiling,
            }
        )
    return rows
