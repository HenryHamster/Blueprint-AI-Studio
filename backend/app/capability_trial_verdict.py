"""Durable adopt-or-reject verdict for capability trials.

Converts a trial's measurement (lift value plus per-arm sample counts) into a
durable adoption decision and persists it on ``project.capability_adoption_verdicts``.

Verdicts:
  - ``adopt``         — lift is positive and both arms meet the minimum sample size
  - ``reject``        — lift is negative enough to confidently recommend removal
  - ``continue-trial`` — insufficient data or lift too ambiguous to decide

Each entry records the evidence snapshot (lift, sample counts, window, timestamps,
thresholds used) so the decision is auditable and re-derivable: a later
contradicting measurement can supersede an earlier verdict with a recorded
transition. The old entry is replaced, not appended.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, TypedDict

from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

# ── Default thresholds (configurable via config dict) ────────────────────────
_DEFAULT_MIN_N_PER_ARM = 30  # minimum tasks per arm before deciding
_DEFAULT_ADOPT_LIFT = 0.05  # lift >= this → adopt
_DEFAULT_REJECT_LIFT = -0.03  # lift <= this → reject
_DEFAULT_FUTILITY_N_MULTIPLIER = 3  # futility_n = min_n_per_arm * this
# Engagement guard: at the futility bound, suppress REJECT when the fraction of
# with-arm-assigned tasks that actually received the capability is below this
# threshold.  A trial where the treatment was never delivered cannot produce a
# valid futility verdict — the measured (near-zero) lift is an artifact of
# non-delivery, not a genuine null effect.
_DEFAULT_FUTILITY_MIN_DELIVERY_RATE = 0.50
# Contamination guard: suppress a decided verdict when the fraction of holdback
# (without-arm-assigned) tasks that nonetheless received the capability exceeds
# this threshold.  Contaminated holdback tasks inflate without-arm success and
# can manufacture a false null/negative verdict for a capability that was never
# cleanly suppressed.
_DEFAULT_FUTILITY_MAX_HOLDBACK_CONTAMINATION_RATE = 0.20
# Significance gate: minimum z-score (|lift| / SE) required before issuing a
# durable adopt or reject verdict.  Uses a conservative SE estimate assuming
# p=0.5 (maximum binomial variance).  None disables the gate (opt-in per trial).
# At n=30 per arm, a +0.05 lift yields z≈0.39 — well below 1.65 — so a trial
# using this threshold cannot adopt on sampling noise alone.
_DEFAULT_MIN_SIGNIFICANCE_Z: float | None = None


def _default_config() -> dict[str, Any]:
    return {
        "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
        "adopt_lift": _DEFAULT_ADOPT_LIFT,
        "reject_lift": _DEFAULT_REJECT_LIFT,
        "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
        "futility_min_delivery_rate": _DEFAULT_FUTILITY_MIN_DELIVERY_RATE,
        "futility_max_holdback_contamination_rate": _DEFAULT_FUTILITY_MAX_HOLDBACK_CONTAMINATION_RATE,
        "min_significance_z": _DEFAULT_MIN_SIGNIFICANCE_Z,
    }


class _TrialDefinitionRequired(TypedDict):
    cap_name: str
    context_key: str
    thresholds: dict[str, Any]


class TrialDefinition(_TrialDefinitionRequired, total=False):
    # Optional: when present, arm partitioning uses this key for intent-to-treat
    # assignment rather than the receipt key (context_key). Fallback to context_key
    # for legacy tasks that pre-date the assignment field.
    assignment_key: str


# ── Lane pilot threshold constants (pre-registered, auditable) ────────────────
# All thresholds are module-level named constants so they appear in trial
# registry entries and are not buried as magic numbers inside branch bodies.

_LANE_PILOT_MIN_N = 30
# KILL conditions
_LANE_PILOT_KILL_MERGE_RATE_FLOOR = -0.03  # lift (lane_rate - control_rate) <= this
_LANE_PILOT_KILL_COST_CEILING = 0.10  # cost-per-merged-PR >= +10% vs control
_LANE_PILOT_KILL_GATE_VIOLATIONS_MAX = 3  # >= this → KILL + immediate operator alert
_LANE_PILOT_KILL_CHECKPOINT_LOSS_CEILING = 0.05  # checkpoint-loss rate > this
_LANE_PILOT_FUTILITY_N = 90  # n per arm without reaching adopt → KILL
# Control-arm contamination guard: any control-arm task carrying a warm-resume
# receipt means warm context leaked into the control arm.  Even one such task
# invalidates the control baseline and triggers an immediate KILL + alert.
_LANE_PILOT_KILL_CONTROL_WARM_RESUME_MAX = 0
# ADOPT conditions
_LANE_PILOT_ADOPT_MERGE_LIFT_FLOOR = (
    -0.01
)  # merge-rate lift must exceed this (non-inferiority)
_LANE_PILOT_ADOPT_COST_IMPROVEMENT = 0.05  # cost-per-merged-PR must improve >= this
# Engagement guard: futility KILL is suppressed when warm-resume engagement is below
# this fraction among lane-arm fix tasks.  A trial that never exercised warm-resume
# cannot produce a valid futility verdict — the treatment was never delivered.
_LANE_PILOT_FUTILITY_MIN_ENGAGEMENT = 0.50

# ── Canonical exported keys (downstream pilot-wiring subtask imports these) ──
LANE_PILOT_CAP_NAME = "review_fix_lane"
LANE_PILOT_ASSIGNMENT_KEY = "_review_fix_lane_arm"  # intent-to-treat arm key
LANE_PILOT_WARM_RESUME_RECEIPT_KEY = "_review_fix_warm_resume"
LANE_PILOT_CHECKPOINT_RECEIPT_KEY = "_review_fix_checkpoint"

_DECIDED_VERDICTS = frozenset({"adopt", "reject", "no_effect", "ADOPT", "KILL"})
_CONTINUE_VERDICTS = frozenset({"continue-trial", "CONTINUE"})
_SUFFICIENT_DATA_CONTINUE_REASONS = frozenset(
    {"ambiguous_lift", "receipt_only_trial", "insignificant_lift"}
)


class LanePilotAggregates(TypedDict, total=False):
    """Per-arm aggregate statistics consumed by evaluate_lane_pilot_verdict."""

    n_lane: int
    n_control: int
    lane_merge_rate: float | None
    control_merge_rate: float | None
    lane_cost_per_merged: float | None
    control_cost_per_merged: float | None
    lane_runtime_cost_per_merged: float | None
    control_runtime_cost_per_merged: float | None
    lane_reserved_idle_cost_per_merged: float | None
    control_reserved_idle_cost_per_merged: float | None
    gate_integrity_violations: int
    checkpoint_loss_rate: float | None
    # Diagnostic counters for the attempt-level checkpoint-loss formula.
    checkpoint_attempts_total: int
    checkpoint_requeues_total: int
    # Treatment-engagement telemetry (optional; absent when no fix tasks yet).
    # warm_resume_fraction: fraction of lane-arm fix tasks that carried the
    #   warm-resume receipt (actually claimed a warm workspace).
    # checkpoint_fraction: fraction of lane-arm fix tasks that wrote a checkpoint.
    # lane_fix_tasks_sampled: denominator for the two fractions above.
    warm_resume_fraction: float | None
    checkpoint_fraction: float | None
    lane_fix_tasks_sampled: int
    # Shadow-pilot arm count (pilot_arm="shadow"). Shadow arms are EXCLUDED from
    # n_lane/n_control and from min_n threshold checks so they never influence
    # KILL/ADOPT verdict decisions — they are observational only.
    n_shadow: int
    # Control-arm contamination: count of control-arm fix tasks that actually
    # received a warm-resume receipt.  Under correct operation this is always 0;
    # any non-zero value means warm context leaked into the control baseline.
    control_warm_resume_count: int


def is_lane_pilot_arm_member(context: dict[str, Any]) -> str | None:
    """Return the arm ('lane' or 'control') for a task, or None if not assigned.

    Uses LANE_PILOT_ASSIGNMENT_KEY for intent-to-treat partitioning.  A task
    that carries only receipt keys (warm-resume, checkpoint) but has no
    assignment key is NOT a member of either arm and must not be counted.
    """
    arm = context.get(LANE_PILOT_ASSIGNMENT_KEY) if isinstance(context, dict) else None
    if arm in ("lane", "control"):
        return arm
    return None


def evaluate_lane_pilot_verdict(
    aggregates: LanePilotAggregates,
    *,
    pilot_active: bool | None = None,
) -> dict[str, Any]:
    """Return a KILL / ADOPT / CONTINUE verdict for the review-fix lane pilot.

    Kill conditions are checked in priority order before adopt.  Gate-integrity
    violations additionally set ``immediate_alert=True`` regardless of sample
    size — the caller is responsible for raising the operator alert.

    Parameters
    ----------
    aggregates:
        Per-arm aggregate statistics.  All rate fields may be None when
        not yet measurable (insufficient merges, no cost data, etc.).
    pilot_active:
        Tri-state enablement signal (default ``None`` preserves legacy behavior
        exactly). When explicitly ``False`` AND both arms are empty (n_lane == 0
        and n_control == 0), the pilot cannot accrue intent-to-treat traffic —
        ``record_lane_arm_assignment`` writes no arm key while the pilot is off —
        so the verdict is annotated ``awaiting_enablement`` (with an explicit
        ``awaiting_enablement`` marker) instead of the generic ``insufficient_data``.
        The enablement decision itself is tracked by the dormant-knob completion
        debt, so this signal lets the debt path suppress a duplicate
        capability-trial obligation. The branch is placed AFTER the immediate-alert
        / contamination KILL checks (which at n=0 cannot fire, since no violation
        counter can be non-zero without arm traffic) so it can never mask a
        genuine KILL.

    Returns
    -------
    A dict with keys:
      verdict            : "KILL" | "ADOPT" | "CONTINUE"
      verdict_reason     : machine-readable slug
      immediate_alert    : bool — True when gate-integrity violations triggered
      thresholds         : snapshot of the pre-registered threshold constants
      <metric fields>    : snapshot of every input field from aggregates
    """
    n_lane = int(aggregates.get("n_lane") or 0)
    n_control = int(aggregates.get("n_control") or 0)
    lane_merge_rate = aggregates.get("lane_merge_rate")
    control_merge_rate = aggregates.get("control_merge_rate")
    lane_cost = aggregates.get("lane_cost_per_merged")
    control_cost = aggregates.get("control_cost_per_merged")
    lane_runtime_cost = aggregates.get("lane_runtime_cost_per_merged")
    control_runtime_cost = aggregates.get("control_runtime_cost_per_merged")
    lane_reserved_idle_cost = aggregates.get("lane_reserved_idle_cost_per_merged")
    control_reserved_idle_cost = aggregates.get("control_reserved_idle_cost_per_merged")
    gate_violations = int(aggregates.get("gate_integrity_violations") or 0)
    checkpoint_loss = aggregates.get("checkpoint_loss_rate")
    warm_resume_fraction = aggregates.get("warm_resume_fraction")
    checkpoint_fraction = aggregates.get("checkpoint_fraction")
    lane_fix_tasks_sampled = int(aggregates.get("lane_fix_tasks_sampled") or 0)
    control_warm_resume_count = int(aggregates.get("control_warm_resume_count") or 0)

    thresholds = {
        "min_n": _LANE_PILOT_MIN_N,
        "kill_merge_rate_floor": _LANE_PILOT_KILL_MERGE_RATE_FLOOR,
        "kill_cost_ceiling": _LANE_PILOT_KILL_COST_CEILING,
        "kill_gate_violations_max": _LANE_PILOT_KILL_GATE_VIOLATIONS_MAX,
        "kill_checkpoint_loss_ceiling": _LANE_PILOT_KILL_CHECKPOINT_LOSS_CEILING,
        "kill_control_warm_resume_max": _LANE_PILOT_KILL_CONTROL_WARM_RESUME_MAX,
        "futility_n": _LANE_PILOT_FUTILITY_N,
        "adopt_merge_lift_floor": _LANE_PILOT_ADOPT_MERGE_LIFT_FLOOR,
        "adopt_cost_improvement": _LANE_PILOT_ADOPT_COST_IMPROVEMENT,
        "futility_min_engagement": _LANE_PILOT_FUTILITY_MIN_ENGAGEMENT,
    }

    base: dict[str, Any] = {
        "n_lane": n_lane,
        "n_control": n_control,
        "lane_merge_rate": lane_merge_rate,
        "control_merge_rate": control_merge_rate,
        "lane_cost_per_merged": lane_cost,
        "control_cost_per_merged": control_cost,
        "lane_runtime_cost_per_merged": lane_runtime_cost,
        "control_runtime_cost_per_merged": control_runtime_cost,
        "lane_reserved_idle_cost_per_merged": lane_reserved_idle_cost,
        "control_reserved_idle_cost_per_merged": control_reserved_idle_cost,
        "gate_integrity_violations": gate_violations,
        "checkpoint_loss_rate": checkpoint_loss,
        "warm_resume_fraction": warm_resume_fraction,
        "checkpoint_fraction": checkpoint_fraction,
        "lane_fix_tasks_sampled": lane_fix_tasks_sampled,
        "control_warm_resume_count": control_warm_resume_count,
        "thresholds": thresholds,
    }

    # Gate-integrity violations trigger immediate alert regardless of sample size.
    immediate_alert = gate_violations >= _LANE_PILOT_KILL_GATE_VIOLATIONS_MAX

    # ── KILL checks (ordered; first match wins) ───────────────────────────────
    if immediate_alert:
        return {
            **base,
            "verdict": "KILL",
            "verdict_reason": "gate_integrity_violations_exceeded",
            "immediate_alert": True,
        }

    # Control-arm contamination: warm context must never reach control tasks.
    # Any non-zero count means the comparison baseline is invalid — KILL + alert
    # regardless of sample size, identical to gate_integrity_violations priority.
    if control_warm_resume_count > _LANE_PILOT_KILL_CONTROL_WARM_RESUME_MAX:
        return {
            **base,
            "verdict": "KILL",
            "verdict_reason": "control_arm_warm_resume_contamination",
            "immediate_alert": True,
        }

    # Awaiting-enablement: when the pilot is explicitly disabled and NO arm
    # traffic has accrued (both arms empty), the trial cannot make progress until
    # an operator flips the pilot on.  Report this distinctly from the generic
    # insufficient_data continue so the debt path can defer to the dormant-knob
    # obligation instead of opening a duplicate, never-resolvable trial debt.
    # Safe against masking a KILL: the immediate_alert and control-contamination
    # KILL checks above already returned; at n_lane == 0 and n_control == 0 no
    # violation counter can be non-zero, so no genuine KILL is possible here.
    if (
        pilot_active is False
        and n_lane == 0
        and n_control == 0
        and gate_violations == 0
        and control_warm_resume_count == 0
    ):
        return {
            **base,
            "verdict": "CONTINUE",
            "verdict_reason": "awaiting_enablement",
            "awaiting_enablement": True,
            "immediate_alert": False,
        }

    if n_lane < _LANE_PILOT_MIN_N or n_control < _LANE_PILOT_MIN_N:
        return {
            **base,
            "verdict": "CONTINUE",
            "verdict_reason": "insufficient_data",
            "immediate_alert": False,
        }

    if lane_merge_rate is not None and control_merge_rate is not None:
        merge_lift = lane_merge_rate - control_merge_rate
        if merge_lift <= _LANE_PILOT_KILL_MERGE_RATE_FLOOR:
            return {
                **base,
                "verdict": "KILL",
                "verdict_reason": "lane_merge_rate_below_floor",
                "immediate_alert": False,
            }

    if lane_cost is not None and control_cost is not None and control_cost > 0:
        cost_delta = (lane_cost - control_cost) / control_cost
        if cost_delta >= _LANE_PILOT_KILL_COST_CEILING:
            return {
                **base,
                "verdict": "KILL",
                "verdict_reason": "lane_cost_ceiling_exceeded",
                "cost_delta_pct": round(cost_delta * 100, 2),
                "immediate_alert": False,
            }

    if (
        checkpoint_loss is not None
        and checkpoint_loss > _LANE_PILOT_KILL_CHECKPOINT_LOSS_CEILING
    ):
        return {
            **base,
            "verdict": "KILL",
            "verdict_reason": "checkpoint_loss_rate_exceeded",
            "immediate_alert": False,
        }

    adopt_criteria_met = (
        lane_merge_rate is not None
        and control_merge_rate is not None
        and (lane_merge_rate - control_merge_rate) > _LANE_PILOT_ADOPT_MERGE_LIFT_FLOOR
        and lane_cost is not None
        and control_cost is not None
        and control_cost > 0
        and (control_cost - lane_cost) / control_cost
        >= _LANE_PILOT_ADOPT_COST_IMPROVEMENT
        and gate_violations == 0
    )
    if (
        n_lane >= _LANE_PILOT_FUTILITY_N
        and n_control >= _LANE_PILOT_FUTILITY_N
        and not adopt_criteria_met
    ):
        # Engagement guard: if the treatment was never delivered (warm-resume
        # engagement below threshold), futility cannot be concluded — the trial
        # accrued arm counts without the treatment actually running.  Emit
        # CONTINUE so the operator can diagnose the infrastructure gap.
        if (
            warm_resume_fraction is not None
            and warm_resume_fraction < _LANE_PILOT_FUTILITY_MIN_ENGAGEMENT
        ):
            return {
                **base,
                "verdict": "CONTINUE",
                "verdict_reason": "treatment_not_engaging",
                "immediate_alert": True,
                "engagement_low": True,
            }
        return {
            **base,
            "verdict": "KILL",
            "verdict_reason": "futility_at_max_n",
            "immediate_alert": False,
        }

    # ── ADOPT check ───────────────────────────────────────────────────────────
    if adopt_criteria_met:
        return {
            **base,
            "verdict": "ADOPT",
            "verdict_reason": "adopt_criteria_met",
            "immediate_alert": False,
        }

    return {
        **base,
        "verdict": "CONTINUE",
        "verdict_reason": "accumulating_data",
        "immediate_alert": False,
    }


TRIAL_DEFINITIONS: list[TrialDefinition] = [
    {
        "cap_name": "repo_map",
        "context_key": "_had_repo_map",
        "assignment_key": "_repo_map_arm",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
            "futility_min_delivery_rate": _DEFAULT_FUTILITY_MIN_DELIVERY_RATE,
        },
    },
    {
        "cap_name": "module_interfaces",
        "context_key": "_had_module_interfaces",
        "assignment_key": "_module_interfaces_arm",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
            "futility_min_delivery_rate": _DEFAULT_FUTILITY_MIN_DELIVERY_RATE,
        },
    },
    {
        "cap_name": "related_tests",
        "context_key": "_had_related_tests",
        "assignment_key": "_related_tests_arm",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
            "futility_min_delivery_rate": _DEFAULT_FUTILITY_MIN_DELIVERY_RATE,
        },
    },
    {
        "cap_name": "affected_consumers",
        "context_key": "_had_affected_consumers",
        "assignment_key": "_affected_consumers_arm",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
            "futility_min_delivery_rate": _DEFAULT_FUTILITY_MIN_DELIVERY_RATE,
        },
    },
    {
        "cap_name": "diagnostics_after_edit",
        "context_key": "_had_diagnostics_after_edit",
        "assignment_key": "_diagnostics_after_edit_arm",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
            "futility_min_delivery_rate": _DEFAULT_FUTILITY_MIN_DELIVERY_RATE,
        },
    },
    {
        "cap_name": LANE_PILOT_CAP_NAME,
        "context_key": LANE_PILOT_ASSIGNMENT_KEY,
        "assignment_key": LANE_PILOT_ASSIGNMENT_KEY,
        "thresholds": {
            "min_n": _LANE_PILOT_MIN_N,
            "kill_merge_rate_floor": _LANE_PILOT_KILL_MERGE_RATE_FLOOR,
            "kill_cost_ceiling": _LANE_PILOT_KILL_COST_CEILING,
            "kill_gate_violations_max": _LANE_PILOT_KILL_GATE_VIOLATIONS_MAX,
            "kill_checkpoint_loss_ceiling": _LANE_PILOT_KILL_CHECKPOINT_LOSS_CEILING,
            "futility_n": _LANE_PILOT_FUTILITY_N,
            "adopt_merge_lift_floor": _LANE_PILOT_ADOPT_MERGE_LIFT_FLOOR,
            "adopt_cost_improvement": _LANE_PILOT_ADOPT_COST_IMPROVEMENT,
            "futility_min_engagement": _LANE_PILOT_FUTILITY_MIN_ENGAGEMENT,
        },
    },
]


def get_trial_definitions() -> list[TrialDefinition]:
    """Return the list of registered capability trial definitions."""
    return TRIAL_DEFINITIONS


def evaluate_trial_verdict(
    lift: float | None,
    n_with: int,
    n_without: int,
    *,
    config: dict[str, Any] | None = None,
    window_hours: int | None = None,
    evaluated_at: str | None = None,
    accumulation_basis: str | None = None,
    arm_composition: dict | None = None,
    receipt_only_trial: bool = False,
    per_arm_cost: dict | None = None,
) -> dict[str, Any]:
    """Return a verdict dict for a capability trial based on measured lift and sample sizes.

    Parameters
    ----------
    lift:           Float lift value (with_arm_rate - without_arm_rate), or None
                    when unmeasured (→ continue-trial).
    n_with:         Number of tasks in the WITH-capability arm.
    n_without:      Number of tasks in the WITHOUT-capability arm.
    config:         Optional threshold overrides.  Missing keys fall back to
                    module defaults.
    window_hours:   Dashboard measurement window (informational only — describes
                    the digest display window, NOT the decision basis).
    evaluated_at:   ISO-8601 timestamp string (caller-supplied so tests are
                    deterministic).  When None the entry omits evaluated_at.
    accumulation_basis: Label naming WHAT population produced ``lift``/``n_with``/
                    ``n_without`` (e.g. ``"cumulative_terminal_arm_tasks"``), so a
                    reader can tell the decision came from cumulative-since-activation
                    counts rather than a single ``window_hours`` digest window.
                    When None the entry omits the key.
    arm_composition: Per-basis breakdown dict from ``compute_trial_arm_accumulation``
                    (keys: ``with_assigned``, ``with_receipt``, ``without_assigned``,
                    ``without_receipt``). When provided, the entry is annotated with it
                    for auditability, and a DECIDED verdict (adopt/reject) additionally
                    requires the assignment-based subpopulation alone to satisfy
                    ``min_n_per_arm`` — fallback-receipt tasks cannot tip a decision.
                    When None the entry omits the key and the purity gate is skipped.
    receipt_only_trial: When True, a decided verdict (adopt/reject) is blocked and
                    replaced with ``continue-trial`` / ``receipt_only_trial``. Use for
                    trials that have no ``assignment_key`` and therefore accumulate only
                    receipt-context evidence — structurally confounded evidence must
                    never produce a durable adoption decision.

    Returns
    -------
    A dict with: verdict, verdict_reason, lift, n_with, n_without, window_hours,
    thresholds, and optionally evaluated_at, accumulation_basis, arm_composition.
    """
    cfg = _default_config()
    if config:
        cfg.update({k: v for k, v in config.items() if v is not None})

    min_n = int(cfg["min_n_per_arm"])
    adopt_threshold = float(cfg["adopt_lift"])
    reject_threshold = float(cfg["reject_lift"])
    futility_n = int(cfg["futility_n"])
    futility_min_delivery_rate = float(cfg["futility_min_delivery_rate"])
    futility_max_holdback_contamination_rate = float(
        cfg["futility_max_holdback_contamination_rate"]
    )

    entry: dict[str, Any] = {
        "lift": lift,
        "n_with": n_with,
        "n_without": n_without,
        "window_hours": window_hours,
        "thresholds": {
            "min_n_per_arm": min_n,
            "adopt_lift": adopt_threshold,
            "reject_lift": reject_threshold,
            "futility_n": futility_n,
        },
    }
    if accumulation_basis is not None:
        entry["accumulation_basis"] = accumulation_basis
    if evaluated_at is not None:
        entry["evaluated_at"] = evaluated_at
    if arm_composition is not None:
        entry["arm_composition"] = arm_composition
    if per_arm_cost is not None:
        entry["per_arm_cost"] = per_arm_cost

    if lift is None or n_with < min_n or n_without < min_n:
        entry["verdict"] = "continue-trial"
        entry["verdict_reason"] = "insufficient_data"
        return entry

    if lift >= adopt_threshold:
        verdict = "adopt"
        verdict_reason = "sufficient_data"
    elif lift <= reject_threshold:
        verdict = "reject"
        verdict_reason = "sufficient_data"
    else:
        # Lift is ambiguous — check futility bound before continuing the trial.
        if n_with >= futility_n and n_without >= futility_n:
            # Engagement guard: if the with-arm delivery rate is below the
            # threshold, the treatment was never truly delivered — the
            # near-zero lift is an artifact of non-delivery, not a genuine
            # null effect.  Emit continue-trial so the operator can diagnose
            # the infrastructure gap rather than durably rejecting a
            # capability that was never trialed.
            if arm_composition is not None:
                _with_assigned = int(arm_composition.get("with_assigned") or 0)
                _with_delivered = arm_composition.get("with_assigned_delivered")
                if _with_delivered is not None and _with_assigned > 0:
                    _delivery_rate = int(_with_delivered) / _with_assigned
                    if _delivery_rate < futility_min_delivery_rate:
                        entry["delivery_rate"] = round(_delivery_rate, 4)
                        entry["immediate_alert"] = True
                        entry["verdict"] = "continue-trial"
                        entry["verdict_reason"] = "treatment_not_engaging"
                        return entry
            verdict = "no_effect"
            verdict_reason = "futility_bound_reached"
        else:
            verdict = "continue-trial"
            verdict_reason = "ambiguous_lift"

    # Significance gate: a durable adopt/reject verdict requires the measured lift
    # to exceed the noise floor implied by arm sample sizes.  Uses a conservative
    # SE estimate with p=0.5 (maximum binomial variance); trial operators can
    # lower or disable the threshold via config["min_significance_z"].
    # no_effect (futility bound) is intentionally excluded — it is a structural
    # bound, not a precision claim.
    min_significance_z = cfg.get("min_significance_z")
    if (
        verdict in ("adopt", "reject")
        and min_significance_z is not None
        and lift is not None
    ):
        se = math.sqrt(0.25 * (1.0 / n_with + 1.0 / n_without))
        if se > 0 and abs(lift) / se < float(min_significance_z):
            verdict = "continue-trial"
            verdict_reason = "insignificant_lift"

    # Receipt-only guard: trials with no assignment key accumulate only receipt
    # context — the arms are structurally confounded and must never produce a
    # decided verdict regardless of sample size.
    if verdict in ("adopt", "reject", "no_effect") and receipt_only_trial:
        verdict = "continue-trial"
        verdict_reason = "receipt_only_trial"

    # Contamination guard: suppress a decided verdict when the holdback arm has an
    # abnormally high fraction of tasks that received the capability despite being
    # assigned to holdback.  These contaminated tasks inflate without-arm success
    # and can manufacture a false null/negative verdict.  Requires the accumulator
    # to have populated ``without_contaminated`` in arm_composition.
    if verdict in ("adopt", "reject", "no_effect") and arm_composition is not None:
        _without_assigned = int(arm_composition.get("without_assigned") or 0)
        _without_contaminated = arm_composition.get("without_contaminated")
        if _without_contaminated is not None and _without_assigned > 0:
            _contamination_rate = int(_without_contaminated) / _without_assigned
            if _contamination_rate > futility_max_holdback_contamination_rate:
                entry["contamination_rate"] = round(_contamination_rate, 4)
                entry["immediate_alert"] = True
                entry["verdict"] = "continue-trial"
                entry["verdict_reason"] = "holdback_contaminated"
                return entry

    # Purity gate: a decided verdict requires the assignment-based subpopulation
    # alone to satisfy min_n per arm. Receipt-fallback tasks (legacy tasks that
    # pre-date intent-to-treat assignment) are confounded and must not tip the
    # decision. Only applies when arm_composition is provided.
    if verdict in ("adopt", "reject", "no_effect") and arm_composition is not None:
        with_assigned = int(arm_composition.get("with_assigned") or 0)
        without_assigned = int(arm_composition.get("without_assigned") or 0)
        if with_assigned < min_n or without_assigned < min_n:
            verdict = "continue-trial"
            verdict_reason = "insufficient_assigned_arm_data"

    entry["verdict"] = verdict
    entry["verdict_reason"] = verdict_reason
    return entry


def record_trial_verdict(
    project: Any,
    cap_name: str,
    entry: dict[str, Any],
) -> None:
    """Upsert a trial verdict entry for ``cap_name`` into ``project.capability_adoption_verdicts``.

    A decided verdict (``adopt`` or ``reject``) is only superseded by another decided
    verdict, an explicit strategist re-declaration (``evidence_source="strategic_review"``
    on the incoming entry), or a ``continue-trial`` with ``verdict_reason="ambiguous_lift"``
    (sufficient data present, but lift is inconclusive).  An insufficient-data
    ``continue-trial`` must never downgrade a decided verdict: once ``adopt`` sets
    holdback to 0.0, the without-arm cannot repopulate, so the system cannot re-derive
    that verdict from live data.  Reverting to ``continue-trial`` silently re-enables a
    rejected capability or silently reopens the holdback on an adopted one.

    Strategist-declared prior verdicts (``evidence_source="strategic_review"`` on the
    stored entry) are similarly protected from insufficient-data self-reverts.

    Callers must flush/commit the session after this call.
    """
    if not isinstance(getattr(project, "capability_adoption_verdicts", None), dict):
        project.capability_adoption_verdicts = {}

    prior = project.capability_adoption_verdicts.get(cap_name)
    prior_verdict = (prior or {}).get("verdict")
    prior_source = (prior or {}).get("evidence_source")
    new_verdict = entry.get("verdict")
    new_reason = entry.get("verdict_reason")

    _authorized_sources = {"strategic_review", "operator"}

    if (
        cap_name == LANE_PILOT_CAP_NAME
        and prior_verdict == "KILL"
        and entry.get("evidence_source") not in _authorized_sources
        and new_verdict != "KILL"
    ):
        logger.info(
            "[CAPABILITY_TRIAL] skipping non-strategic write for %s — "
            "durable KILL must survive later automated verdicts until an "
            "explicit strategist re-declaration arrives",
            cap_name,
        )
        return

    if (
        new_verdict in _CONTINUE_VERDICTS
        and entry.get("evidence_source") not in _authorized_sources
        and (prior_verdict in _DECIDED_VERDICTS or prior_source == "strategic_review")
        and new_reason not in _SUFFICIENT_DATA_CONTINUE_REASONS
    ):
        logger.info(
            "[CAPABILITY_TRIAL] skipping continue-trial write for %s — "
            "decided verdict %r is standing (insufficient-data evaluation cannot "
            "self-revert a decision; explicit strategist re-declaration required)",
            cap_name,
            prior_verdict,
        )
        return

    # Carry forward ambiguous_lift_since so dwell-time escalation can fire.
    if new_verdict in _CONTINUE_VERDICTS and new_reason == "ambiguous_lift":
        prior_since = (prior or {}).get("ambiguous_lift_since")
        if prior_since and (prior or {}).get("verdict_reason") == "ambiguous_lift":
            entry = dict(entry)
            entry["ambiguous_lift_since"] = prior_since
        elif "ambiguous_lift_since" not in entry:
            entry = dict(entry)
            entry["ambiguous_lift_since"] = (
                entry.get("evaluated_at") or datetime.now(timezone.utc).isoformat()
            )

    project.capability_adoption_verdicts[cap_name] = entry
    flag_modified(project, "capability_adoption_verdicts")

    if prior_verdict and prior_verdict != new_verdict:
        logger.info(
            "[CAPABILITY_TRIAL] verdict transition for %s: %s → %s "
            "(lift=%s, n_with=%s, n_without=%s)",
            cap_name,
            prior_verdict,
            new_verdict,
            entry.get("lift"),
            entry.get("n_with"),
            entry.get("n_without"),
        )
    else:
        logger.info(
            "[CAPABILITY_TRIAL] verdict recorded for %s: %s "
            "(lift=%s, n_with=%s, n_without=%s)",
            cap_name,
            new_verdict,
            entry.get("lift"),
            entry.get("n_with"),
            entry.get("n_without"),
        )


async def _check_open_message_exists(
    session: Any,
    project_id: Any,
    dedupe_key: str,
) -> bool:
    """Check if an open ProjectMessage with the given dedupe_key exists.

    Returns True if a matching open message is found, False otherwise.
    Uses a bounded select(id).limit(1) query for efficiency.
    """
    from sqlalchemy import select

    from app.models import ProjectMessage

    result = await session.execute(
        select(ProjectMessage.id)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.status == "open",
            ProjectMessage.context["dedupe_key"].as_string() == dedupe_key,
        )
        .limit(1)
    )
    return result.scalar() is not None


async def raise_capability_verdict_notice(
    session: Any,
    project_id: Any,
    cap_name: str,
    prior_verdict: str | None,
    new_verdict: str,
    entry: dict[str, Any],
) -> None:
    """Record a durable operator notice when a capability trial transitions to a decided verdict.

    Deduplicated on cap_name + new_verdict so a verdict reversal (adopt → reject)
    fires a new notice while repeated re-evaluations of the same standing decided
    verdict are silent.
    """
    from app.models import MessageType, ProjectMessage
    from app.project_message_creation import build_project_message

    dedupe_key = f"capability_verdict_decided_{cap_name}_{new_verdict}"
    if await _check_open_message_exists(session, project_id, dedupe_key):
        return

    lift = entry.get("lift")
    n_with = entry.get("n_with")
    n_without = entry.get("n_without")
    verdict_reason = entry.get("verdict_reason", "")
    transition_desc = (
        f"{prior_verdict} → {new_verdict}"
        if prior_verdict
        else f"undecided → {new_verdict}"
    )
    if new_verdict == "no_effect":
        content = (
            f"[CAPABILITY-TRIAL] {cap_name} no measurable effect: {new_verdict} ({transition_desc}) "
            f"reason={verdict_reason} lift={lift} n_with={n_with} n_without={n_without} "
            f"— capability neither helped nor harmed; consider simplification rather than removal"
        )
    else:
        content = (
            f"[CAPABILITY-TRIAL] {cap_name} decided: {new_verdict} ({transition_desc}) "
            f"reason={verdict_reason} lift={lift} n_with={n_with} n_without={n_without}"
        )
    session.add(
        await build_project_message(
            session,
            project_id=project_id,
            message_type=MessageType.NOTE,
            from_type="system",
            content=content,
            context={
                "event_type": "capability_verdict_decided",
                "dedupe_key": dedupe_key,
                "cap_name": cap_name,
                "prior_verdict": prior_verdict,
                "new_verdict": new_verdict,
                "verdict_reason": verdict_reason,
                "lift": lift,
                "n_with": n_with,
                "n_without": n_without,
            },
        )
    )


def is_awaiting_enablement_entry(entry: dict[str, Any]) -> bool:
    """Return True when a verdict entry marks a trial as awaiting pilot enablement.

    An awaiting-enablement continue verdict (``verdict_reason == "awaiting_enablement"``
    or the explicit ``awaiting_enablement`` marker) means the trial cannot accrue arm
    traffic until an operator flips the pilot on.  That enablement decision is already
    tracked by the dormant-knob completion debt, so it must NOT also open a
    capability-trial debt.
    """
    if not isinstance(entry, dict):
        return False
    return bool(
        entry.get("verdict_reason") == "awaiting_enablement"
        or entry.get("awaiting_enablement") is True
    )


def should_record_capability_trial_debt(entry: dict[str, Any]) -> bool:
    """Return True when a CONTINUE verdict entry warrants a capability-trial debt.

    A capability sitting in observe/trial with no decided verdict is a standing
    obligation and normally records a completion debt.  The one exception is an
    awaiting-enablement entry: the enablement decision is already tracked by the
    dormant-knob debt, so recording a capability-trial debt would duplicate a
    single operator decision as two permanent, never-resolvable obligations.
    """
    return not is_awaiting_enablement_entry(entry)


async def persist_trial_verdicts_in_transaction(
    source_session: Any,
    project_id: Any,
    *,
    pending_verdicts: list[tuple[str, dict[str, Any]]],
    gate_alert_detail: str | None = None,
) -> dict[str, Any]:
    """Durably persist a batch of trial verdicts (and an optional gate alert).

    Digest computation runs on read/request sessions that may be rolled back by
    the caller after the response payload is built (the FastAPI session
    dependency closes WITHOUT committing; the precompute path's commit is
    contingent on the L2 cache write succeeding). Verdict recording and
    gate-integrity alerts are durable controls, so — exactly like
    ``apply_lane_pilot_kill_in_transaction`` — they must not depend on that
    outer session committing.

    This opens its OWN committing session, RE-FETCHES the project fresh inside
    it (so ``record_trial_verdict``'s KILL-survival and insufficient-data
    self-revert guards evaluate against current committed state, never a stale
    early-fetched row), applies every pending verdict, raises the gate-integrity
    alert when ``gate_alert_detail`` is provided, then commits. On any exception
    it rolls back and re-raises.

    Returns a snapshot dict of the project's adoption verdicts after the commit,
    so callers can build a display payload that mirrors exactly what is durably
    persisted (rather than what was naively computed).
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.database import resolve_async_engine
    from app.models import Project

    factory = async_sessionmaker(
        resolve_async_engine(source_session),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        try:
            proof_recorded = False
            project = (
                await session.execute(
                    select(Project).where(Project.id == project_id).with_for_update()
                )
            ).scalars().first()
            if project is None:
                raise ValueError(
                    f"Project {project_id} not found for durable trial-verdict persist"
                )
            for cap_name, entry in pending_verdicts:
                prior_verdict = (
                    (project.capability_adoption_verdicts or {}).get(cap_name) or {}
                ).get("verdict")
                record_trial_verdict(project, cap_name, entry)
                actual_verdict = (
                    (project.capability_adoption_verdicts or {}).get(cap_name) or {}
                ).get("verdict")
                # Record a durable capability proof when the trial reaches or
                # maintains an adopt verdict.  The dedup key inside
                # record_capability_proof_from_trial ensures idempotency.
                if actual_verdict in ("adopt", "ADOPT"):
                    try:
                        from app.capability_proof import (
                            record_capability_proof_from_trial,
                        )

                        proof_recorded = (
                            record_capability_proof_from_trial(project, cap_name, entry)
                            or proof_recorded
                        )
                    except Exception:
                        logger.exception(
                            "[CAPABILITY_TRIAL] capability proof recording failed for "
                            "%s on project %s (non-fatal)",
                            cap_name,
                            project_id,
                        )
                # Completion-debt ledger: a capability sitting in observe/trial
                # (continue-trial) with no decided enforce verdict is a standing
                # obligation; a decided verdict discharges it. Best-effort and
                # deduped per (project, capability) so the row is unique.
                try:
                    from app.completion_debt import (
                        arecord_completion_debt,
                        aresolve_completion_debt,
                        KIND_CAPABILITY_TRIAL,
                    )

                    _debt_key = f"capability_trial:{project_id}:{cap_name}"
                    if actual_verdict in _CONTINUE_VERDICTS and (
                        not should_record_capability_trial_debt(entry)
                    ):
                        # Awaiting-enablement: the enablement decision is already
                        # tracked by the dormant-knob debt, so do NOT open a
                        # duplicate capability-trial debt.  Resolve any pre-existing
                        # open one so the operator sees a single obligation.
                        await aresolve_completion_debt(
                            session,
                            _debt_key,
                            reason="awaiting_enablement:dormant_knob_tracks_decision",
                        )
                    elif actual_verdict in _CONTINUE_VERDICTS:
                        await arecord_completion_debt(
                            session,
                            dedupe_key=_debt_key,
                            kind=KIND_CAPABILITY_TRIAL,
                            title=(
                                f"Capability '{cap_name}' in observe/trial mode "
                                "with no decided enforce verdict"
                            ),
                            project_id=project_id,
                            source="capability_trial_verdict",
                            detail={
                                "capability": cap_name,
                                "verdict": actual_verdict,
                            },
                        )
                    elif actual_verdict in _DECIDED_VERDICTS:
                        await aresolve_completion_debt(
                            session,
                            _debt_key,
                            reason=f"decided:{actual_verdict}",
                        )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[CAPABILITY_TRIAL] completion-debt record failed for "
                        "%s on project %s (non-fatal)",
                        cap_name,
                        project_id,
                    )
                if (
                    actual_verdict in _DECIDED_VERDICTS
                    and actual_verdict != prior_verdict
                    and entry.get("evidence_source") != "strategic_review"
                ):
                    await raise_capability_verdict_notice(
                        session,
                        project_id,
                        cap_name,
                        prior_verdict,
                        actual_verdict,
                        entry,
                    )
                    try:
                        from app.strategic_trigger import request_strategic_refresh

                        await request_strategic_refresh(
                            session,
                            project_id,
                            f"capability_verdict:{cap_name}:{actual_verdict}",
                        )
                    except Exception:
                        logger.exception(
                            "[CAPABILITY_TRIAL] strategic refresh arm failed for "
                            "%s verdict transition on project %s (non-fatal)",
                            cap_name,
                            project_id,
                        )
            if gate_alert_detail is not None:
                # Lazy import to avoid an import cycle: review_fix_lane_pilot
                # imports record_trial_verdict from this module.
                from app.review_fix_lane_pilot import raise_gate_integrity_alert

                await raise_gate_integrity_alert(
                    session, project_id, detail=gate_alert_detail
                )
            await session.commit()
            verdicts = get_trial_verdicts(project)
            persisted_verdicts = dict(verdicts) if isinstance(verdicts, dict) else {}
            if proof_recorded:
                # The trial-adoption proof is durable before waking waiters. The
                # shared resolver is bounded, idempotent, and all-needs-aware;
                # keep this best-effort so an immediate wake can never invalidate
                # the authoritative verdict/proof commit above.
                try:
                    from app.reaper_sweeps import (
                        event_driven_unblock_on_capability_proof,
                    )

                    await event_driven_unblock_on_capability_proof(project_id, session)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[CAPABILITY_TRIAL] event-driven capability-proof unblock "
                        "failed for project %s (non-fatal)",
                        project_id,
                    )
            return persisted_verdicts
        except Exception:
            await session.rollback()
            raise


def get_trial_verdicts(project: Any) -> dict[str, Any]:
    """Return the current adoption-verdict dict from a project, or empty dict."""
    verdicts = getattr(project, "capability_adoption_verdicts", None)
    if isinstance(verdicts, dict):
        return verdicts
    return {}


def carry_adoption_verdicts_for_consolidation(
    capabilities_consolidated: list[dict],
    adoption_verdicts: dict[str, Any],
) -> dict[str, Any]:
    """Merge absorbed capability adoption verdicts onto the survivor's key.

    For each {survivor, absorbed[...]} consolidation event, the highest-priority
    absorbed verdict (adopt/reject > strategist-declared > continue-trial) is
    written to the survivor key ONLY when the survivor has no existing decided
    verdict (adopt/reject) and no strategist-declared verdict.  Absorbed keys are
    removed regardless.

    Returns a NEW dict — does not mutate the input.  Callers are responsible for
    flag_modified on the JSONB column after assigning the result.
    """
    if not capabilities_consolidated or not isinstance(adoption_verdicts, dict):
        return adoption_verdicts

    # Priority rank: decided (adopt/reject) > strategist-declared > continue-trial
    def _verdict_priority(entry: dict) -> int:
        if not isinstance(entry, dict):
            return -1
        verdict = entry.get("verdict")
        if verdict in ("adopt", "reject", "no_effect"):
            return 2
        if entry.get("evidence_source") == "strategic_review":
            return 1
        return 0

    result = dict(adoption_verdicts)
    for event in capabilities_consolidated:
        if not isinstance(event, dict):
            continue
        survivor = event.get("survivor")
        absorbed_names = event.get("absorbed") or []
        if not survivor or not absorbed_names:
            continue

        # Collect the best absorbed verdict.
        best_absorbed: dict | None = None
        for abs_name in absorbed_names:
            abs_entry = result.get(abs_name)
            if abs_entry is None:
                abs_entry = adoption_verdicts.get(abs_name)
            if not isinstance(abs_entry, dict):
                continue
            if best_absorbed is None or _verdict_priority(
                abs_entry
            ) > _verdict_priority(best_absorbed):
                best_absorbed = abs_entry

        # Drop absorbed keys.
        for abs_name in absorbed_names:
            result.pop(abs_name, None)

        if best_absorbed is None:
            continue

        # Only write to survivor when it has no existing decided/strategist verdict.
        survivor_entry = result.get(survivor)
        if isinstance(survivor_entry, dict):
            sv = survivor_entry.get("verdict")
            ss = survivor_entry.get("evidence_source")
            if sv in ("adopt", "reject", "no_effect") or ss == "strategic_review":
                logger.info(
                    "[CAPABILITY_TRIAL] consolidation carry-through skipped: "
                    "survivor=%s already has decided verdict %r",
                    survivor,
                    sv,
                )
                continue

        result[survivor] = best_absorbed
        logger.info(
            "[CAPABILITY_TRIAL] consolidation carry-through: survivor=%s "
            "absorbed=%s carried_verdict=%r",
            survivor,
            absorbed_names,
            best_absorbed.get("verdict"),
        )

    return result


def is_trial_stalled(entry: dict[str, Any]) -> bool:
    """Return True when a verdict entry represents a permanently stalled trial.

    A trial is stalled when it is still in ``continue-trial``/``insufficient_data`` and
    its accrual projection shows that the holdback arm can NEVER reach ``min_n_per_arm``
    at the current accrual rate (``days_to_min_n is None``).

    Returns False for:
    - decided verdicts (adopt/reject/no_effect/ADOPT/KILL)
    - healthy continue-trials with a finite ``days_to_min_n``
    - sufficient-data continue-trials (``ambiguous_lift``, ``receipt_only_trial``)
    - ``treatment_not_engaging`` / ``holdback_contaminated`` which raise their own
      ``immediate_alert`` and must not double-notify
    - entries with no ``accrual_projection`` (not yet projected)
    - trials that have never received holdback-arm assignments (not yet started)
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("verdict") != "continue-trial":
        return False
    if entry.get("verdict_reason") != "insufficient_data":
        return False
    accrual = entry.get("accrual_projection")
    if not isinstance(accrual, dict):
        return False
    if accrual.get("days_to_min_n") is not None:
        return False
    # A trial whose holdback arm has never received any assignments hasn't started
    # yet — zero accrual rate on zero participants is not a stall, it's a cold start.
    arm_comp = entry.get("arm_composition") or {}
    return bool(int(arm_comp.get("without_assigned") or 0))


async def raise_stalled_trial_notice(
    session: Any,
    project_id: Any,
    cap_name: str,
    entry: dict[str, Any],
) -> bool:
    """Record a durable operator notice when a capability trial is permanently stalled.

    Deduplicated on ``cap_name`` so repeated digest re-evaluations of the same stalled
    trial emit exactly ONE open notice per capability.  The notice carries the actionable
    data the digest already computed: which arm is stalled, its assigned count vs
    ``min_n``, the recent accrual rates, the current ``holdback_fraction``, and the
    ``alternative_projections`` map of concrete options the operator can act on.

    Returns True when a new notice was written, False when the dedupe key already existed.
    """
    from app.models import MessageType, ProjectMessage
    from app.project_message_creation import build_project_message

    dedupe_key = f"capability_trial_stalled_{cap_name}"
    if await _check_open_message_exists(session, project_id, dedupe_key):
        return False

    accrual = entry.get("accrual_projection") or {}
    with_7d = accrual.get("with_7d")
    without_7d = accrual.get("without_7d")
    window_days = accrual.get("window_days")
    holdback_fraction = accrual.get("holdback_fraction")
    alternative_projections = accrual.get("alternative_projections") or {}
    arm_comp = entry.get("arm_composition") or {}
    thresholds = entry.get("thresholds") or {}
    min_n = thresholds.get("min_n_per_arm")
    without_assigned = arm_comp.get("without_assigned")

    content = (
        f"[CAPABILITY-TRIAL] {cap_name} stalled: holdback arm cannot reach min_n at current accrual "
        f"(holdback_fraction={holdback_fraction} without_assigned={without_assigned} min_n={min_n} "
        f"with_7d={with_7d} without_7d={without_7d} window_days={window_days}) "
        f"— raise holdback_fraction or extend trial window"
    )
    session.add(
        await build_project_message(
            session,
            project_id=project_id,
            message_type=MessageType.NOTE,
            from_type="system",
            content=content,
            context={
                "event_type": "capability_trial_stalled",
                "dedupe_key": dedupe_key,
                "cap_name": cap_name,
                "with_7d": with_7d,
                "without_7d": without_7d,
                "window_days": window_days,
                "holdback_fraction": holdback_fraction,
                "without_assigned": without_assigned,
                "min_n": min_n,
                "alternative_projections": alternative_projections,
            },
        )
    )
    return True


async def persist_stalled_trial_notices_in_transaction(
    source_session: Any,
    project_id: Any,
    *,
    stalled: list[tuple[str, dict[str, Any]]],
) -> None:
    """Durably persist stall notices for permanently stalled capability trials.

    Opens its OWN committing AsyncSession (mirroring ``persist_trial_verdicts_in_transaction``)
    so notices survive regardless of whether the outer digest request session commits.
    On any exception it rolls back and re-raises.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.database import resolve_async_engine

    factory = async_sessionmaker(
        resolve_async_engine(source_session),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        try:
            for cap_name, entry in stalled:
                newly_noticed = await raise_stalled_trial_notice(
                    session, project_id, cap_name, entry
                )
                if newly_noticed:
                    try:
                        from app.strategic_trigger import request_strategic_refresh

                        await request_strategic_refresh(
                            session,
                            project_id,
                            f"capability_trial_stalled:{cap_name}",
                        )
                    except Exception:
                        logger.exception(
                            "[CAPABILITY_TRIAL] strategic refresh failed for stalled trial "
                            "%s on project %s (non-fatal)",
                            cap_name,
                            project_id,
                        )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Ambiguous-lift dwell-time escalation ─────────────────────────────────────

_AMBIGUOUS_LIFT_ESCALATION_THRESHOLD_DAYS = 14


def is_ambiguous_lift_escalation_needed(
    entry: dict[str, Any],
    *,
    threshold_days: int = _AMBIGUOUS_LIFT_ESCALATION_THRESHOLD_DAYS,
    now_iso: str | None = None,
) -> bool:
    """Return True when an ambiguous_lift trial has dwelled past the threshold.

    Returns False when:
    - entry is not a dict
    - verdict/verdict_reason are not ambiguous_lift
    - ``ambiguous_lift_since`` is absent or unparseable
    - dwell time is below ``threshold_days``
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("verdict") != "continue-trial":
        return False
    if entry.get("verdict_reason") != "ambiguous_lift":
        return False
    since_str = entry.get("ambiguous_lift_since")
    if not since_str:
        return False
    try:
        since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        if now_iso is not None:
            now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
        else:
            now_dt = datetime.now(timezone.utc)
        dwell_days = (now_dt - since_dt).total_seconds() / 86400
        return dwell_days >= threshold_days
    except (ValueError, TypeError):
        return False


async def raise_ambiguous_lift_escalation_notice(
    session: Any,
    project_id: Any,
    cap_name: str,
    entry: dict[str, Any],
) -> bool:
    """Record a durable operator notice when an ambiguous_lift trial exceeds the dwell threshold.

    Deduplicated on ``cap_name`` so repeated digest re-evaluations of the same
    long-dwelling trial emit exactly ONE open notice per capability.

    Returns True when a new notice was written, False when the dedupe key already existed.
    """
    from app.models import MessageType, ProjectMessage
    from app.project_message_creation import build_project_message

    dedupe_key = f"capability_trial_ambiguous_lift_escalation_{cap_name}"
    if await _check_open_message_exists(session, project_id, dedupe_key):
        return False

    since_str = entry.get("ambiguous_lift_since")
    lift = entry.get("lift")
    n_with = entry.get("n_with")
    n_without = entry.get("n_without")
    thresholds = entry.get("thresholds") or {}
    min_n = thresholds.get("min_n_per_arm")

    content = (
        f"[CAPABILITY-TRIAL] {cap_name} ambiguous_lift escalation: trial has been inconclusive "
        f"since {since_str} (lift={lift} n_with={n_with} n_without={n_without} min_n={min_n}) "
        f"— operator review required: consider expanding trial, adjusting thresholds, or closing"
    )
    session.add(
        await build_project_message(
            session,
            project_id=project_id,
            message_type=MessageType.NOTE,
            from_type="system",
            content=content,
            context={
                "event_type": "capability_trial_ambiguous_lift_escalation",
                "dedupe_key": dedupe_key,
                "cap_name": cap_name,
                "ambiguous_lift_since": since_str,
                "lift": lift,
                "n_with": n_with,
                "n_without": n_without,
                "min_n": min_n,
            },
        )
    )
    return True


async def persist_ambiguous_lift_escalations_in_transaction(
    source_session: Any,
    project_id: Any,
    *,
    escalated: list[tuple[str, dict[str, Any]]],
) -> None:
    """Durably persist ambiguous-lift escalation notices for long-dwelling trials.

    Opens its OWN committing AsyncSession (mirroring ``persist_stalled_trial_notices_in_transaction``)
    so notices survive regardless of whether the outer digest request session commits.
    On any exception it rolls back and re-raises.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.database import resolve_async_engine

    factory = async_sessionmaker(
        resolve_async_engine(source_session),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        try:
            for cap_name, entry in escalated:
                newly_noticed = await raise_ambiguous_lift_escalation_notice(
                    session, project_id, cap_name, entry
                )
                if newly_noticed:
                    try:
                        from app.strategic_trigger import request_strategic_refresh

                        await request_strategic_refresh(
                            session,
                            project_id,
                            f"capability_trial_ambiguous_lift:{cap_name}",
                        )
                    except Exception:
                        logger.exception(
                            "[CAPABILITY_TRIAL] strategic refresh failed for ambiguous-lift "
                            "escalation %s on project %s (non-fatal)",
                            cap_name,
                            project_id,
                        )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def attach_accrual_projection_to_verdict(
    project: Any,
    cap_name: str,
    accrual_projection: dict[str, Any],
    is_stalled: bool = False,
) -> None:
    """Stamp accrual_projection and is_stalled onto an existing persisted verdict entry.

    Supplements ``record_trial_verdict``: once the core verdict is committed, this
    patches a fresh accrual-rate snapshot so strategic readers can see whether the
    trial can reach min_n at current throughput.  Callers must flush/commit the session.
    No-op when the cap has no existing verdict entry.
    """
    verdicts = getattr(project, "capability_adoption_verdicts", None)
    if not isinstance(verdicts, dict):
        return
    entry = verdicts.get(cap_name)
    if not isinstance(entry, dict):
        return
    verdicts[cap_name] = {
        **entry,
        "accrual_projection": accrual_projection,
        "is_stalled": is_stalled,
    }
    flag_modified(project, "capability_adoption_verdicts")


async def persist_accrual_projections_in_transaction(
    source_session: Any,
    project_id: Any,
    *,
    projections: list[tuple[str, dict[str, Any], bool]],
) -> None:
    """Durably persist accrual-projection snapshots onto existing verdict entries.

    Opens its OWN committing AsyncSession (mirroring ``persist_trial_verdicts_in_transaction``)
    so projection data survives regardless of whether the outer digest session commits.
    Each tuple is (cap_name, accrual_projection, is_stalled).  Entries with no existing
    verdict are silently skipped.  On any exception rolls back and re-raises.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.database import resolve_async_engine
    from app.models import Project

    factory = async_sessionmaker(
        resolve_async_engine(source_session),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        try:
            project = await session.get(Project, project_id)
            if project is None:
                return
            for cap_name, accrual_projection, is_stalled in projections:
                attach_accrual_projection_to_verdict(
                    project, cap_name, accrual_projection, is_stalled
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_effective_holdback_fraction(
    verdicts: Any,
    cap_name: str,
    default_fraction: float,
) -> float:
    """Return the holdback fraction to apply for a capability, based on its verdict.

    - ``adopt``          → 0.0 (stop holding back; use the capability for all tasks)
    - ``reject``         → 1.0 (always hold back; do not use the capability)
    - anything else      → ``default_fraction`` (trial still in progress)

    Falls back to ``default_fraction`` when ``verdicts`` is not a dict, when
    ``cap_name`` is absent, or when the entry lacks a ``"verdict"`` key.
    """
    if not isinstance(verdicts, dict):
        return default_fraction
    entry = verdicts.get(cap_name)
    if not isinstance(entry, dict):
        return default_fraction
    verdict = entry.get("verdict")
    if verdict == "adopt":
        return 0.0
    if verdict in ("reject", "no_effect"):
        return 1.0
    return default_fraction
