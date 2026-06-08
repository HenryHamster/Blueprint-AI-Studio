"""Direction history metric and aggregation helpers.

Pure metric/aggregation functions extracted from app.research_completion.
These operate on direction/project data structures and have no coupling to the
research pipeline orchestration.
"""

from typing import Any, Dict

from sqlalchemy.orm.attributes import flag_modified

from app.direction_outcomes import (
    consumed_merged_count,
    proven_merged_count,
    verified_merged_count,
)
from app.failure_classification import (
    TAXONOMY_CODE_DEFECT,
    TAXONOMY_INFRA_RELATED,
    TAXONOMY_REVIEW_QUALITY,
    TAXONOMY_SCOPE_RELATED,
)

_AUTONOMY_RETIREMENT_THRESHOLD = (
    10  # autonomous merges required for retirement eligibility
)

# Proof-weighting tiers for the strategic-DECISION consumption layer. A merged
# code change earns FULL credit only when it was machine-PROVEN (a satisfied
# named_test, or a satisfied downstream_artifact proven by recorded consumption);
# a merge that merely DECLARED forward evidence without proving it earns partial
# credit; an UNDECLARED merge (no commitment to forward evidence — the "merely
# delivered" proxy this direction is meant to stop crediting) earns the least.
# All tiers are <= 1.0 so the proof-weighted count is STRICTLY <=
# verified_merged_count and never inflates a direction's apparent output.
_PROOF_WEIGHT_PROVEN = 1.0
_PROOF_WEIGHT_DECLARED_UNPROVEN = 0.7
_PROOF_WEIGHT_UNDECLARED = 0.4

def _weight_merged(
    verified: int, merged_write: int, proven: int, declared: int
) -> float:
    """Proof-weight a verified-merge universe by declaration/proof tiers.

    Partitions the ``verified`` merges into three nested tiers — PROVEN (machine-
    checked), DECLARED-but-unproven, and UNDECLARED — and applies the
    ``_PROOF_WEIGHT_*`` multipliers. The result is STRICTLY ``<= verified`` (every
    tier weight is <= 1.0) so proof-weighting can only ever DISCOUNT, never inflate,
    a direction's apparent merged output.

    Fail-safe: when ``merged_write <= 0`` (a read-only / research cycle with no
    merged write task — declaration coverage is undefined) or ``verified <= 0``,
    the full verified count is returned unweighted so such cycles degrade to
    today's behavior rather than being retro-penalized to a discounted value.
    """
    if merged_write <= 0 or verified <= 0:
        return float(verified)
    proven = min(max(0, proven), verified)
    declared_unproven = max(0, min(max(0, declared), verified) - proven)
    undeclared = max(0, verified - proven - declared_unproven)
    return (
        proven * _PROOF_WEIGHT_PROVEN
        + declared_unproven * _PROOF_WEIGHT_DECLARED_UNPROVEN
        + undeclared * _PROOF_WEIGHT_UNDECLARED
    )


def proof_weighted_merged(entry: dict) -> float:
    """Proof/declaration-weighted merged count for one research_history entry.

    The strategic-DECISION counterpart to :func:`verified_merged_count`: it credits
    a machine-PROVEN merge fully, a declared-but-unproven merge partially, and an
    UNDECLARED merge least (see ``_PROOF_WEIGHT_*``). Always ``<=
    verified_merged_count(entry)`` so it can only discount the "merely delivered"
    proxy, never inflate it.

    Fail-safe over legacy data: an entry written before the acceptance contract has
    no ``acceptance_merged_write_total`` key, so the proof state is unknown and the
    raw verified count is returned unchanged (the same legacy gate the acceptance-
    metric invariant guard uses). Read-only / research cycles (zero merged write
    tasks) keep full credit, because only the verified WRITE slice is proof-weighted.
    """
    verified = verified_merged_count(entry)
    if "acceptance_merged_write_total" not in entry:
        return float(verified)
    if "acceptance_verified_write_merged" not in entry:
        return _weight_merged(
            verified,
            int(entry.get("acceptance_merged_write_total") or 0),
            proven_merged_count(entry),
            int(entry.get("acceptance_declared_merged") or 0),
        )
    verified_write = int(entry.get("acceptance_verified_write_merged") or 0)
    read_only_verified = max(0, verified - verified_write)
    return _weight_merged(
        verified_write,
        int(entry.get("acceptance_merged_write_total") or 0),
        proven_merged_count(entry),
        int(entry.get("acceptance_declared_merged") or 0),
    ) + read_only_verified


def _agg_proof_weighted_merged(agg: dict) -> float:
    """Proof/declaration-weighted lifetime merged count from a history_aggregate.

    Mirrors :func:`proof_weighted_merged` over the evicted-cycle aggregate. Merges
    folded from cycles that PREDATE the acceptance contract are tracked separately
    (they never recorded ``acceptance_*`` totals), so they keep FULL credit here —
    only the acceptance-aware portion of lifetime verified merges is proof-weighted.
    This guarantees legacy merges are never retro-penalized when their cycle ages
    out of research_history, and the result stays ``<= total_verified_prs_merged``.
    """
    total_verified = int(
        agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
    )
    if "total_acceptance_merged_write_total" not in agg:
        return float(total_verified)
    acc_verified = int(agg.get("total_acceptance_verified_merged") or 0)
    if "total_acceptance_verified_write_merged" not in agg:
        # Legacy aggregate: no write/read-only split was persisted yet.
        legacy_verified = max(0, total_verified - acc_verified)
        weighted_acc = _weight_merged(
            acc_verified,
            int(agg.get("total_acceptance_merged_write_total") or 0),
            int(agg.get("total_acceptance_proven_merged") or 0),
            int(agg.get("total_acceptance_declared_merged") or 0),
        )
        return legacy_verified + weighted_acc
    acc_verified_write = int(agg.get("total_acceptance_verified_write_merged") or 0)
    # Pre-contract verified merges keep full credit; within the acceptance-aware
    # slice, read-only verified merges also keep full credit and only the verified
    # WRITE slice is proof-weighted. Clamp so stale/partial aggregates cannot go
    # negative.
    legacy_verified = max(0, total_verified - acc_verified)
    read_only_verified = max(0, acc_verified - acc_verified_write)
    weighted_write = _weight_merged(
        acc_verified_write,
        int(agg.get("total_acceptance_merged_write_total") or 0),
        int(agg.get("total_acceptance_proven_merged") or 0),
        int(agg.get("total_acceptance_declared_merged") or 0),
    )
    return legacy_verified + read_only_verified + weighted_write


_HISTORY_AGG_DEFAULTS = {
    "total_cycles": 0,
    "total_tasks_created": 0,
    "total_prs_merged": 0,
    # Verification-aware lifetime merge count: total_prs_merged minus merges
    # whose verification was degraded (blocked by an environment/setup failure).
    # _compute_lifetime_burn_metrics reads this so a degraded merge does not
    # suppress lifetime non-merge burn even after its cycle is evicted.
    "total_verified_prs_merged": 0,
    # Acceptance-evidence lifetime aggregates — folded so directions past the
    # 10-cycle eviction window keep an honest proven/declared signal. Distinct
    # from (and never larger than) total_verified_prs_merged: proven counts
    # machine-checked named-test proof OR recorded downstream-artifact
    # consumption; declared counts merges that committed to forward evidence at
    # plan time; merged_write_total is the coverage denominator (merged WRITE
    # tasks) so lifetime coverage = declared / merged_write_total without lossy
    # per-cycle fraction-averaging.
    "total_acceptance_proven_merged": 0,
    "total_acceptance_consumed_merged": 0,
    "total_acceptance_declared_merged": 0,
    "total_acceptance_merged_write_total": 0,
    "total_prs_rejected": 0,
    "total_tasks_failed": 0,
    "total_tasks_failed_infra": 0,
    "total_tasks_completed": 0,
    # Correct no-op completions ("deliverable already present", no PR opened) —
    # rolled up so _compute_lifetime_burn_metrics can subtract them from lifetime
    # non-merge burn even after the originating cycles are evicted from
    # research_history. Without this, a long-lived already-built direction would
    # trip the lifetime-burn backstop once its no-op cycles aged out.
    "total_tasks_completed_no_pr": 0,
    "total_tasks_cancelled": 0,
    "first_cycle_at": None,
    "total_autonomous_prs_merged": 0,
    "total_assisted_prs_merged": 0,
    "total_operator_actions": 0,
    "total_generalized_successes": 0,
    "total_generalized_terminal_outcomes": 0,
    # Acceptance proof/declaration aggregate for the proof-weighting slice —
    # lifetime survival past research_history eviction. Only the acceptance-aware
    # slice is rolled up (entries that recorded an ``acceptance_merged_write_total``);
    # pre-contract merges are excluded so they keep full credit in
    # _agg_proof_weighted_merged and are never retro-penalized.
    #   * total_acceptance_verified_merged — verified merges from acceptance-aware
    #     cycles (the universe _agg_proof_weighted_merged partitions and weights).
    #   * total_acceptance_verified_write_merged — the verified WRITE slice within
    #     that acceptance-aware universe; this is the part that gets proof-weighted.
    # total_acceptance_proven_merged / total_acceptance_declared_merged /
    # total_acceptance_merged_write_total are folded above (shared with the
    # established acceptance-coverage consumers) and reused here.
    "total_acceptance_verified_merged": 0,
    "total_acceptance_verified_write_merged": 0,
}

_DECOMP_AGG_DEFAULTS = {
    "total_decompositions": 0,
    "subtask_merge_rate_weighted_sum": 0.0,
    "subtask_count_sum": 0,
    "cross_module_count_sum": 0,
}

_OUTCOME_KIND_STATUSES = ("success", "neutral", "failure")
_READ_ONLY_UNCLASSIFIED_KIND = "read_only_unclassified"


def _entry_generalized_successes(entry: dict[str, Any]) -> int:
    """Return generalized successes for one research_history entry.

    Legacy entries predate generalized_successes; for those, merged PRs are the
    only durable success signal available, so they serve as the additive
    fallback.
    """
    return int(entry.get("generalized_successes", entry.get("prs_merged", 0)) or 0)


def _entry_generalized_terminal_outcomes(entry: dict[str, Any]) -> int:
    """Return the number of terminal outcomes represented by one entry.

    Prefer the structured outcome breakdown when available. Otherwise fall back
    to the terminal task counters, and finally to merged PRs for sparse legacy
    rows that only recorded merge counts.
    """
    breakdown = entry.get("outcome_kind_breakdown") or {}
    if isinstance(breakdown, dict) and breakdown:
        return sum(
            int((counts or {}).get(status, 0) or 0)
            for counts in breakdown.values()
            if isinstance(counts, dict)
            for status in _OUTCOME_KIND_STATUSES
        )

    terminal = sum(
        int(entry.get(field, 0) or 0)
        for field in ("tasks_completed", "tasks_failed", "tasks_cancelled")
    )
    if terminal > 0:
        return terminal

    generalized_successes = _entry_generalized_successes(entry)
    if generalized_successes > 0:
        return generalized_successes
    return int(entry.get("prs_merged", 0) or 0)


def _fallback_outcome_kind_breakdown(entry: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Synthesize a minimal outcome-kind breakdown for sparse legacy entries."""
    merged = int(entry.get("prs_merged", 0) or 0)
    if merged <= 0:
        return {}
    return {"code_merge": {"success": merged, "neutral": 0, "failure": 0}}


def _merge_outcome_kind_breakdown(
    target: dict[str, dict[str, int]], source: dict[str, Any] | None
) -> None:
    """Add per-kind generalized outcome counts into ``target`` in place."""
    if not isinstance(source, dict):
        return
    for kind, counts in source.items():
        if not isinstance(counts, dict):
            continue
        bucket = target.setdefault(
            kind, {"success": 0, "neutral": 0, "failure": 0}
        )
        for status in _OUTCOME_KIND_STATUSES:
            bucket[status] += int(counts.get(status, 0) or 0)


def _copy_outcome_kind_breakdown(
    source: dict[str, Any] | None,
) -> dict[str, dict[str, int]]:
    """Return a normalized copy of an outcome-kind breakdown mapping."""
    normalized: dict[str, dict[str, int]] = {}
    if not isinstance(source, dict):
        return normalized
    for kind, counts in source.items():
        if not isinstance(counts, dict):
            continue
        normalized[kind] = {
            status: int(counts.get(status, 0) or 0)
            for status in _OUTCOME_KIND_STATUSES
        }
    return normalized


def _sum_outcome_kind_statuses(
    breakdown: dict[str, dict[str, int]], statuses: tuple[str, ...]
) -> int:
    """Return the total counts across ``statuses`` for all outcome kinds."""
    return sum(
        int((counts or {}).get(status, 0) or 0)
        for counts in breakdown.values()
        if isinstance(counts, dict)
        for status in statuses
    )


def _backfill_breakdown_to_generalized_totals(
    breakdown: dict[str, dict[str, int]],
    *,
    generalized_successes: int,
    generalized_terminal_outcomes: int,
) -> dict[str, dict[str, int]]:
    """Fill sparse aggregate breakdowns so their counts match generalized totals."""
    normalized = _copy_outcome_kind_breakdown(breakdown)
    breakdown_successes = _sum_outcome_kind_statuses(normalized, ("success",))
    breakdown_terminal = _sum_outcome_kind_statuses(
        normalized, _OUTCOME_KIND_STATUSES
    )
    missing_successes = max(0, generalized_successes - breakdown_successes)
    missing_terminal = max(0, generalized_terminal_outcomes - breakdown_terminal)
    if missing_successes <= 0 and missing_terminal <= 0:
        return normalized

    bucket = normalized.setdefault(
        _READ_ONLY_UNCLASSIFIED_KIND,
        {"success": 0, "neutral": 0, "failure": 0},
    )
    if missing_successes > 0:
        bucket["success"] += missing_successes
    residual_terminal = max(0, missing_terminal - missing_successes)
    if residual_terminal > 0:
        bucket["neutral"] += residual_terminal
    return normalized


def _normalize_aggregate_generalized_state(
    agg: dict[str, Any] | None,
) -> dict[str, Any]:
    """Upgrade a history aggregate so generalized totals stay compatible with legacy data."""
    normalized = dict(agg or {})
    breakdown = _copy_outcome_kind_breakdown(
        normalized.get("outcome_kind_breakdown")
    )
    legacy_merged = int(normalized.get("total_prs_merged", 0) or 0)
    if legacy_merged > 0:
        code_merge = breakdown.setdefault(
            "code_merge", {"success": 0, "neutral": 0, "failure": 0}
        )
        code_merge["success"] = max(
            int(code_merge.get("success", 0) or 0), legacy_merged
        )

    breakdown_successes = _sum_outcome_kind_statuses(breakdown, ("success",))
    normalized["total_generalized_successes"] = max(
        int(normalized.get("total_generalized_successes", 0) or 0),
        legacy_merged,
        breakdown_successes,
    )

    legacy_terminal = sum(
        int(normalized.get(field, 0) or 0)
        for field in (
            "total_tasks_completed",
            "total_tasks_failed",
            "total_tasks_cancelled",
        )
    )
    breakdown_terminal = _sum_outcome_kind_statuses(
        breakdown, _OUTCOME_KIND_STATUSES
    )
    breakdown_non_success = breakdown_terminal - breakdown_successes
    normalized["total_generalized_terminal_outcomes"] = max(
        int(normalized.get("total_generalized_terminal_outcomes", 0) or 0),
        legacy_terminal,
        breakdown_terminal,
        int(normalized["total_generalized_successes"] or 0),
        int(normalized["total_generalized_successes"] or 0) + breakdown_non_success,
    )
    normalized["outcome_kind_breakdown"] = _backfill_breakdown_to_generalized_totals(
        breakdown,
        generalized_successes=int(normalized["total_generalized_successes"] or 0),
        generalized_terminal_outcomes=int(
            normalized["total_generalized_terminal_outcomes"] or 0
        ),
    )
    return normalized


def _aggregate_generalized_terminal_outcomes(agg: dict[str, Any]) -> int:
    """Return lifetime terminal outcomes from a direction history aggregate."""
    normalized = _normalize_aggregate_generalized_state(agg)
    total = int(normalized.get("total_generalized_terminal_outcomes") or 0)
    if total > 0:
        return total

    terminal = sum(
        int(normalized.get(field, 0) or 0)
        for field in (
            "total_tasks_completed",
            "total_tasks_failed",
            "total_tasks_cancelled",
        )
    )
    if terminal > 0:
        return terminal

    generalized_successes = int(normalized.get("total_generalized_successes") or 0)
    return generalized_successes


def _compute_lifetime_generalized_outcome_state(direction: dict[str, Any]) -> dict[str, Any]:
    """Return lifetime generalized success metrics for a direction."""
    history = direction.get("research_history") or []
    agg = _normalize_aggregate_generalized_state(
        direction.get("history_aggregate") or {}
    )

    generalized_successes = sum(
        _entry_generalized_successes(entry) for entry in history
    ) + int(agg.get("total_generalized_successes") or 0)
    generalized_terminal_outcomes = sum(
        _entry_generalized_terminal_outcomes(entry) for entry in history
    ) + _aggregate_generalized_terminal_outcomes(agg)

    outcome_kind_breakdown: dict[str, dict[str, int]] = {}
    for entry in history:
        _merge_outcome_kind_breakdown(
            outcome_kind_breakdown,
            entry.get("outcome_kind_breakdown") or _fallback_outcome_kind_breakdown(entry),
        )
    _merge_outcome_kind_breakdown(
        outcome_kind_breakdown, agg.get("outcome_kind_breakdown")
    )
    if not outcome_kind_breakdown and generalized_successes > 0:
        _merge_outcome_kind_breakdown(
            outcome_kind_breakdown,
            _fallback_outcome_kind_breakdown(
                {"prs_merged": agg.get("total_prs_merged") or generalized_successes}
            ),
        )

    return {
        "generalized_successes": generalized_successes,
        "generalized_terminal_outcomes": generalized_terminal_outcomes,
        "generalized_health_rate": (
            round(generalized_successes / generalized_terminal_outcomes, 3)
            if generalized_terminal_outcomes > 0
            else None
        ),
        "outcome_kind_breakdown": outcome_kind_breakdown or None,
    }


def _fold_evicted_into_aggregate(prev_agg: dict | None, evicted: list[dict]) -> dict:
    """Fold evicted research_history entries into a running history_aggregate.

    Preserves basic counters plus per-scope breakdown and decomposition-quality
    signals that _calibrate_scope and predict_spawn_success read. Without this,
    directions older than 10 cycles silently lost all scope/decomposition data.
    """
    agg = prev_agg or {}
    for k, v in _HISTORY_AGG_DEFAULTS.items():
        agg.setdefault(k, v)
    normalized_generalized = _normalize_aggregate_generalized_state(agg)
    agg["total_generalized_successes"] = normalized_generalized[
        "total_generalized_successes"
    ]
    agg["total_generalized_terminal_outcomes"] = normalized_generalized[
        "total_generalized_terminal_outcomes"
    ]
    agg["outcome_kind_breakdown"] = normalized_generalized["outcome_kind_breakdown"]
    sb = agg.setdefault("scope_breakdown", {})
    dq = agg.setdefault("decomposition_quality", dict(_DECOMP_AGG_DEFAULTS))
    outcome_kind_breakdown = agg["outcome_kind_breakdown"]
    for ev in evicted:
        agg["total_cycles"] += 1
        agg["total_tasks_created"] += ev.get("tasks_created", 0)
        agg["total_prs_merged"] += ev.get("prs_merged", 0)
        # Legacy fallback: pre-verification entries have no verified_prs_merged;
        # treat all merges as verified so lifetime burn is unchanged for old data.
        agg["total_verified_prs_merged"] += ev.get(
            "verified_prs_merged", ev.get("prs_merged", 0)
        )
        # Acceptance-evidence aggregates — legacy-0 fallback: pre-aggregate
        # entries claim nothing proven/declared and contribute no denominator,
        # matching proven_merged_count / declared_merged_count semantics.
        agg["total_acceptance_proven_merged"] += ev.get(
            "acceptance_proven_merged", 0
        )
        agg["total_acceptance_consumed_merged"] += consumed_merged_count(ev)
        agg["total_acceptance_declared_merged"] += ev.get(
            "acceptance_declared_merged", 0
        )
        agg["total_acceptance_merged_write_total"] += ev.get(
            "acceptance_merged_write_total", 0
        )
        agg["total_prs_rejected"] += ev.get("prs_rejected", 0)
        agg["total_tasks_failed"] += ev.get("tasks_failed", 0)
        agg["total_tasks_failed_infra"] += ev.get("tasks_failed_infra", 0)
        agg["total_tasks_completed"] += ev.get("tasks_completed", 0)
        agg["total_tasks_completed_no_pr"] += ev.get("tasks_completed_no_pr", 0)
        agg["total_tasks_cancelled"] += ev.get("tasks_cancelled", 0)
        # Legacy fallback: pre-attribution entries have no autonomous_prs_merged;
        # treat all merges as autonomous so CPM is conservative for old data.
        agg["total_autonomous_prs_merged"] += ev.get(
            "autonomous_prs_merged", ev.get("prs_merged", 0)
        )
        agg["total_assisted_prs_merged"] += ev.get("assisted_prs_merged", 0)
        agg["total_operator_actions"] += ev.get("operator_action_count", 0)
        # Proof-weighting slice — the verified-merge universe restricted to
        # acceptance-aware evicted cycles (those that recorded the merged-write
        # denominator). Pre-contract entries lack the key and contribute nothing
        # here, so their verified merges stay outside the proof-weighted slice and
        # keep full credit. proven / declared / merged_write_total are folded above
        # (shared with the established acceptance-coverage consumers); the
        # acceptance-aware verified slice is split into write vs read-only so only
        # the WRITE subset is proof-weighted.
        if "acceptance_merged_write_total" in ev:
            agg["total_acceptance_verified_merged"] += verified_merged_count(ev)
            agg["total_acceptance_verified_write_merged"] += ev.get(
                "acceptance_verified_write_merged",
                0,
            )
        agg["total_generalized_successes"] += _entry_generalized_successes(ev)
        agg["total_generalized_terminal_outcomes"] += (
            _entry_generalized_terminal_outcomes(ev)
        )
        _merge_outcome_kind_breakdown(
            outcome_kind_breakdown,
            ev.get("outcome_kind_breakdown") or _fallback_outcome_kind_breakdown(ev),
        )
        if not agg["first_cycle_at"] and ev.get("timestamp"):
            agg["first_cycle_at"] = ev["timestamp"]
        for scope, stats in (ev.get("scope_breakdown") or {}).items():
            bucket = sb.setdefault(
                scope,
                {
                    "created": 0,
                    "merged": 0,
                    "failed": 0,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                },
            )
            bucket["created"] += stats.get("created", 0)
            bucket["merged"] += stats.get("merged", 0)
            bucket["failed"] += stats.get("failed", 0)
            bucket["failed_infra"] += stats.get("failed_infra", 0)
            bucket["failed_scope_overflow"] = bucket.get(
                "failed_scope_overflow", 0
            ) + stats.get("failed_scope_overflow", 0)
        for qd in ev.get("decomposition_quality") or []:
            dq["total_decompositions"] += 1
            count = qd.get("subtask_count") or 0
            dq["subtask_count_sum"] += count
            dq["cross_module_count_sum"] += qd.get("cross_module_count") or 0
            rate = qd.get("subtask_merge_rate")
            if isinstance(rate, (int, float)) and count > 0:
                dq["subtask_merge_rate_weighted_sum"] += float(rate) * count
    normalized_generalized = _normalize_aggregate_generalized_state(agg)
    agg["total_generalized_successes"] = normalized_generalized[
        "total_generalized_successes"
    ]
    agg["total_generalized_terminal_outcomes"] = normalized_generalized[
        "total_generalized_terminal_outcomes"
    ]
    agg["outcome_kind_breakdown"] = normalized_generalized["outcome_kind_breakdown"]
    return agg


def _compute_momentum_factor(d: dict) -> float:
    """Return a momentum multiplier based on recent research history productivity."""
    history = d.get("research_history", [])
    if len(history) >= 2 and all(e.get("tasks_created", 0) > 0 for e in history[-2:]):
        return 2.0  # hot streak: last 2 cycles both productive
    if len(history) >= 3 and all(e.get("tasks_created", 0) == 0 for e in history[-3:]):
        return 0.5  # stalled: last 3 cycles all empty
    return 1.0  # neutral


def _compute_acceptance_rate(d: dict) -> float | None:
    """Return operator acceptance rate (merged / (merged + rejected)) with recency weighting.

    Weights the last 3 cycles 3x relative to the lifetime average, matching
    _compute_productivity_score's recency scheme. Returns None when no PRs exist (merged
    or rejected) so callers can distinguish "unknown" from a low rate.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (
        agg.get("total_prs_merged") or 0
    )
    total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (
        agg.get("total_prs_rejected") or 0
    )
    total_prs = total_merged + total_rejected
    if total_prs == 0:
        return None  # no data yet
    lifetime_rate = total_merged / total_prs
    # Weight last 3 cycles 3x for responsiveness to recent changes
    recent = history[-3:]
    if not recent:
        return lifetime_rate
    recent_merged = sum(e.get("prs_merged", 0) for e in recent)
    recent_rejected = sum(e.get("prs_rejected", 0) for e in recent)
    recent_prs = recent_merged + recent_rejected
    if recent_prs == 0:
        return lifetime_rate
    recent_rate = recent_merged / recent_prs
    return (recent_rate * 3 + lifetime_rate) / 4


def _compute_difficulty_index(d: dict) -> float:
    """Return tasks-per-merge ratio across direction lifetime.

    Higher values mean the direction needed more attempts per successful merge
    (empirically harder). Defaults to 1.0 when no merges have happened yet
    (treat as easy until proven otherwise) and is capped at 30 to avoid
    blowups from directions with many failures and zero merges.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_tasks = sum(e.get("tasks_created", 0) for e in history) + (
        agg.get("total_tasks_created") or 0
    )
    # Tasks-per-merge counts PROOF-WEIGHTED merges: a degraded/unverified merge did
    # not ship (excluded by verified_merged_count), and an undeclared/unproven merge
    # is the "merely delivered" proxy this direction must stop crediting — so a
    # direction shipping piles of undeclared merges reads as HARDER (more tasks per
    # proven merge) than one shipping proven work. Legacy/read-only cycles degrade
    # to the verified count inside proof_weighted_merged / _agg_proof_weighted_merged.
    total_merged = sum(proof_weighted_merged(e) for e in history) + (
        _agg_proof_weighted_merged(agg)
    )
    if total_merged <= 0:
        return 1.0
    return min(30.0, total_tasks / total_merged)


def _compute_productivity_score(d: dict) -> float:
    """Return net productivity score for a direction, with recency weighting.

    Accounts for both merged PRs (positive signal) and rejected PRs (negative
    signal from operator close-without-merge). Weights the last 3 cycles 3x
    relative to the lifetime average for responsiveness to recent changes.
    Returns 0.5 (neutral) if never researched.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    # Both the lifetime and recent merge totals are PROOF-WEIGHTED: a degraded merge
    # never shipped (excluded by verified_merged_count), and an undeclared/unproven
    # merge — the "merely delivered" proxy — contributes only partial productivity so
    # a direction shipping undeclared work does not read as productive as one shipping
    # proven work. Legacy/read-only cycles degrade to the verified count.
    total_merged = sum(proof_weighted_merged(e) for e in history) + (
        _agg_proof_weighted_merged(agg)
    )
    total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (
        agg.get("total_prs_rejected") or 0
    )
    if total_cycles == 0:
        return 0.5  # unknown, neutral
    # Net productivity: merge contributes +1, rejection contributes -0.5
    # (rejection is penalized but not as harshly as a merge is rewarded)
    lifetime_rate = (total_merged - 0.5 * total_rejected) / total_cycles
    # Weight last 3 cycles 3x for responsiveness to recent changes
    recent = history[-3:]
    if not recent:
        return lifetime_rate
    recent_merged = sum(proof_weighted_merged(e) for e in recent)
    recent_rejected = sum(e.get("prs_rejected", 0) for e in recent)
    recent_rate = (recent_merged - 0.5 * recent_rejected) / len(recent)
    return (recent_rate * 3 + lifetime_rate) / 4


def _compute_direction_quality_score(d: dict) -> float:
    """Aggregate recent taxonomy signals into a [0.0, 1.0] quality score.

    Reads the last 3 research_history entries and combines three signals:
    (a) taxonomy health — actionable failures (code_defect + scope_related) vs
        structural (infra_related + review_quality). Higher is better.
    (b) wasted-cycle signal — 1 - (cycles with tasks_created>0 but prs_merged==0
        over cycles with tasks_created>0). Higher is better.
    (c) scope calibration — 1.0 when scope_related does not dominate (>50%),
        else 0.0.

    Uses linear recency weighting within the 3-cycle window (oldest=1, newest=3),
    mirroring the 3x recency emphasis in _compute_productivity_score. Returns
    0.5 (neutral) when no history exists.
    """
    history = d.get("research_history", [])
    recent = history[-3:]
    if not recent:
        return 0.5

    weights = list(range(1, len(recent) + 1))  # oldest-of-window = 1x, newest = 3x
    actionable_w = structural_w = scope_w = total_taxon_w = 0.0
    active_w = wasted_w = 0.0

    for entry, w in zip(recent, weights, strict=True):
        tx = entry.get("taxonomy_summary") or {}
        code_defect = tx.get(TAXONOMY_CODE_DEFECT, 0) or 0
        scope_related = tx.get(TAXONOMY_SCOPE_RELATED, 0) or 0
        infra_related = tx.get(TAXONOMY_INFRA_RELATED, 0) or 0
        review_quality = tx.get(TAXONOMY_REVIEW_QUALITY, 0) or 0
        actionable_w += w * (code_defect + scope_related)
        structural_w += w * (infra_related + review_quality)
        scope_w += w * scope_related
        total_taxon_w += w * (
            code_defect + scope_related + infra_related + review_quality
        )

        tasks_created = entry.get("tasks_created", 0) or 0
        if tasks_created > 0:
            active_w += w
            if verified_merged_count(entry) == 0:
                wasted_w += w

    if (actionable_w + structural_w) > 0:
        taxonomy_health = actionable_w / (actionable_w + structural_w)
    else:
        taxonomy_health = 0.5

    productivity_signal = 1.0 - (wasted_w / active_w) if active_w > 0 else 0.5

    if total_taxon_w > 0:
        scope_signal = 0.0 if (scope_w / total_taxon_w) > 0.5 else 1.0
    else:
        scope_signal = 0.5

    components = [taxonomy_health, productivity_signal, scope_signal]

    # (d) proof signal — what share of recent VERIFIED merges were actually proven
    # (or at least declared) versus the "merely delivered" undeclared proxy. Added
    # ONLY when the window contains acceptance-aware cycles that produced verified
    # merges (the same legacy gate proof_weighted_merged uses), so legacy/read-only
    # directions keep the original 3-component score unchanged. proof_signal ranges
    # over [_PROOF_WEIGHT_UNDECLARED, 1.0]: an all-proven direction reads as fully
    # healthy here while an all-undeclared one is pulled down — so a pile of
    # unprovable merges no longer scores the same as proven work.
    acc_recent = [e for e in recent if "acceptance_merged_write_total" in e]
    recent_verified = sum(verified_merged_count(e) for e in acc_recent)
    if acc_recent and recent_verified > 0:
        recent_weighted = sum(proof_weighted_merged(e) for e in acc_recent)
        components.append(max(0.0, min(1.0, recent_weighted / recent_verified)))

    score = sum(components) / len(components)
    return max(0.0, min(1.0, score))


def _compute_rolling_median_review_iterations(d: dict) -> float | None:
    """Return the median of per-cycle median_review_iterations across the last 3 cycles.

    Reads median_review_iterations from each of the most recent research_history
    entries (populated by direction_outcomes). Returns None when no cycle in the
    window recorded a value, so callers can distinguish "unknown" from 0.
    """
    history = d.get("research_history", [])
    recent = history[-3:]
    values = [
        float(e["median_review_iterations"])
        for e in recent
        if isinstance(e.get("median_review_iterations"), (int, float))
    ]
    if not values:
        return None
    values.sort()
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _compute_autonomy_threshold_state(d: dict) -> dict:
    """Return lifetime autonomy state and retirement-eligibility for a direction.

    Uses the same legacy fallback as admin_digest: pre-attribution research_history
    entries (no ``autonomous_prs_merged`` key) treat all prs_merged as autonomous,
    keeping CPM conservative for old data.

    Returns a dict with:
      - ``lifetime_autonomous_prs_merged`` (int)
      - ``lifetime_operator_actions`` (int)
      - ``retirement_eligible`` (bool): True iff >= _AUTONOMY_RETIREMENT_THRESHOLD
        autonomous merges AND zero operator actions lifetime.
    """
    history = d.get("research_history") or []
    agg = d.get("history_aggregate") or {}
    lifetime_autonomous = sum(
        h.get("autonomous_prs_merged", h.get("prs_merged", 0)) for h in history
    ) + (agg.get("total_autonomous_prs_merged") or 0)
    lifetime_operator_actions = sum(
        h.get("operator_action_count", 0) for h in history
    ) + (agg.get("total_operator_actions") or 0)
    return {
        "lifetime_autonomous_prs_merged": lifetime_autonomous,
        "lifetime_operator_actions": lifetime_operator_actions,
        "retirement_eligible": lifetime_autonomous >= _AUTONOMY_RETIREMENT_THRESHOLD
        and lifetime_operator_actions == 0,
    }


def _update_cycle_history(project: Any, cycle_id: str, updates: Dict[str, Any]) -> None:
    """Find a cycle entry by cycle_id in project.vision and apply updates in-place."""
    vision = dict(project.vision or {})
    cycle_history = list(vision.get("_strategic_cycle_history") or [])
    for entry in cycle_history:
        if entry.get("cycle_id") == cycle_id:
            entry.update(updates)
            break
    vision["_strategic_cycle_history"] = cycle_history
    project.vision = vision
    flag_modified(project, "vision")
