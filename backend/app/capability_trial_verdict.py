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
from typing import Any, TypedDict

from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

# ── Default thresholds (configurable via config dict) ────────────────────────
_DEFAULT_MIN_N_PER_ARM = 30          # minimum tasks per arm before deciding
_DEFAULT_ADOPT_LIFT = 0.05           # lift >= this → adopt
_DEFAULT_REJECT_LIFT = -0.03         # lift <= this → reject
_DEFAULT_FUTILITY_N_MULTIPLIER = 3   # futility_n = min_n_per_arm * this


def _default_config() -> dict[str, Any]:
    return {
        "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
        "adopt_lift": _DEFAULT_ADOPT_LIFT,
        "reject_lift": _DEFAULT_REJECT_LIFT,
        "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
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
        },
    },
    {
        "cap_name": "module_interfaces",
        "context_key": "_had_module_interfaces",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
        },
    },
    {
        "cap_name": "related_tests",
        "context_key": "_had_related_tests",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
        },
    },
    {
        "cap_name": "affected_consumers",
        "context_key": "_had_affected_consumers",
        "thresholds": {
            "min_n_per_arm": _DEFAULT_MIN_N_PER_ARM,
            "adopt_lift": _DEFAULT_ADOPT_LIFT,
            "reject_lift": _DEFAULT_REJECT_LIFT,
            "futility_n": _DEFAULT_MIN_N_PER_ARM * _DEFAULT_FUTILITY_N_MULTIPLIER,
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
            verdict = "reject"
            verdict_reason = "futility_bound_reached"
        else:
            verdict = "continue-trial"
            verdict_reason = "ambiguous_lift"

    # Purity gate: a decided verdict requires the assignment-based subpopulation
    # alone to satisfy min_n per arm. Receipt-fallback tasks (legacy tasks that
    # pre-date intent-to-treat assignment) are confounded and must not tip the
    # decision. Only applies when arm_composition is provided.
    if verdict in ("adopt", "reject") and arm_composition is not None:
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

    if (
        new_verdict == "continue-trial"
        and entry.get("evidence_source") != "strategic_review"
        and (prior_verdict in ("adopt", "reject") or prior_source == "strategic_review")
        and new_reason != "ambiguous_lift"
    ):
        logger.info(
            "[CAPABILITY_TRIAL] skipping continue-trial write for %s — "
            "decided verdict %r is standing (insufficient-data evaluation cannot "
            "self-revert a decision; explicit strategist re-declaration required)",
            cap_name,
            prior_verdict,
        )
        return

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


def get_trial_verdicts(project: Any) -> dict[str, Any]:
    """Return the current adoption-verdict dict from a project, or empty dict."""
    verdicts = getattr(project, "capability_adoption_verdicts", None)
    if isinstance(verdicts, dict):
        return verdicts
    return {}


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
    if verdict == "reject":
        return 1.0
    return default_fraction
