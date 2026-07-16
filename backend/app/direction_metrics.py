"""Direction history metric and aggregation helpers.

Pure metric/aggregation functions extracted from app.research_completion.
These operate on direction/project data structures and have no coupling to the
research pipeline orchestration.
"""

from typing import Any

from app.direction_outcomes import (
    PR_INELIGIBLE_TASK_TYPES,
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
    "total_pr_ineligible_tasks_created": 0,
    "total_prs_merged": 0,
    # Verification-aware lifetime merge count: total_prs_merged minus merges
    # whose verification was degraded (blocked by an environment/setup failure).
    # _compute_lifetime_burn_metrics reads this so a degraded merge does not
    # suppress lifetime non-merge burn even after its cycle is evicted.
    "total_verified_prs_merged": 0,
    # Ancestry-confirmed subset of total_prs_merged: cycles where the merge
    # commit was confirmed reachable from the integration branch (explicit
    # landed_on_integration=True). Legacy-0 fallback on eviction: pre-reachability
    # entries have unknown reachability, so they contribute 0 here, keeping the
    # aggregate honest rather than inflating with unconfirmed ancestry.
    "total_reachable_prs_merged": 0,
    # Count of evicted cycles that CONTAINED the reachable_prs_merged key (presence
    # check, not truthiness). Non-zero means reachability WAS measured for at least
    # one evicted cycle; a zero total_reachable_prs_merged alongside a non-zero count
    # means confirmed-zero (known-zero), not legacy-unknown (field absent).
    "total_reachable_recorded_cycles": 0,
    # Credit-gate lifetime stranded count: confirmed-unreachable merges that are
    # subtracted from total_verified_prs_merged to get the credit-eligible total
    # after a cycle is evicted from research_history. Folded by
    # _fold_evicted_into_aggregate so the credit_gate discount survives eviction.
    "total_stranded_prs_merged": 0,
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
    # Lifetime count of undeclared merges upgraded to the inferred_test_cochange
    # bucket (verified CI + test/prod co-change, no stamped verdict). Folded so
    # directions past the eviction window keep the inferred-coverage signal.
    # Disjoint from total_acceptance_declared_merged by construction.
    "total_acceptance_inferred_merged": 0,
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
    # Verification-aware lifetime autonomous merge count: the honest basis for
    # the retirement-eligibility milestone in compute_direction_autonomy_state.
    # Only autonomous merges that also landed in a verified acceptance bucket
    # count, so this stays <= total_autonomous_prs_merged and survives cycle
    # eviction. Two-step legacy fallback on fold (see _fold_evicted_into_aggregate)
    # keeps pre-field entries whole rather than dropping them to zero.
    "total_verified_autonomous_prs_merged": 0,
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
    # Lifetime estimated spend folded from evicted cycles. Legacy-0 fallback on
    # eviction: pre-attribution entries have no recorded cost and contribute 0,
    # matching the existing legacy-0 convention for other aggregate keys.
    "total_estimated_cost_usd": 0.0,
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
    # Detect legacy aggregates that have accumulated autonomous credit in
    # total_autonomous_prs_merged but predate total_verified_autonomous_prs_merged.
    # Must check BEFORE setdefault materializes the key at 0.
    _seed_verified_autonomous = (
        "total_verified_autonomous_prs_merged" not in agg
        and agg.get("total_autonomous_prs_merged", 0) > 0
    )
    for k, v in _HISTORY_AGG_DEFAULTS.items():
        agg.setdefault(k, v)
    # Seed from total_autonomous_prs_merged so pre-deploy folded credit is not
    # silently dropped the first time this key is materialized. Subsequent folds
    # find the key present and skip this branch.
    if _seed_verified_autonomous:
        agg["total_verified_autonomous_prs_merged"] = agg["total_autonomous_prs_merged"]
        # Backfill legacy eviction snapshots with the autonomous fallback so that
        # post-eviction delta calculations (_apply_direction_outcomes) compute the
        # correct _snap_val baseline. Without this, a late-merge event on a legacy
        # snapshot (_snap_val=0) adds the full recomputed total on top of the
        # already-seeded aggregate, counting pre-eviction merges twice.
        for _snap in (agg.get("_evicted_cycle_snapshots") or {}).values():
            if "verified_autonomous_prs_merged" not in _snap:
                _snap["verified_autonomous_prs_merged"] = int(
                    _snap.get(
                        "autonomous_prs_merged", _snap.get("prs_merged", 0)
                    )
                    or 0
                )
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
        type_breakdown = ev.get("type_breakdown") or {}
        agg["total_pr_ineligible_tasks_created"] += sum(
            (type_breakdown.get(ttype) or {}).get("created", 0) or 0
            for ttype in PR_INELIGIBLE_TASK_TYPES
        )
        agg["total_prs_merged"] += ev.get("prs_merged", 0)
        # Legacy fallback: pre-verification entries have no verified_prs_merged;
        # treat all merges as verified so lifetime burn is unchanged for old data.
        agg["total_verified_prs_merged"] += ev.get(
            "verified_prs_merged", ev.get("prs_merged", 0)
        )
        # Legacy-0 fallback: pre-reachability entries lack the key; contribute 0
        # so the aggregate stays honest (unknown ancestry ≠ confirmed reachable).
        agg["total_reachable_prs_merged"] += ev.get("reachable_prs_merged", 0)
        # Track presence of the key (not truthiness) so a zero-total aggregate
        # still signals "reachability was measured" vs "field was never recorded".
        if "reachable_prs_merged" in ev:
            agg["total_reachable_recorded_cycles"] += 1
        # Preserve the credit-gate discount so confirmed-stranded merges remain
        # excluded from the credit-eligible total after their cycle is evicted.
        agg["total_stranded_prs_merged"] += int(ev.get("stranded_prs_merged") or 0)
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
        agg["total_acceptance_inferred_merged"] += ev.get(
            "acceptance_inferred_merged", 0
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
        # Verification-aware autonomous fold. Two-step legacy fallback consistent
        # with verified_autonomous_merged_count: pre-field entries degrade to the
        # raw autonomous count, and pre-attribution entries to prs_merged, so no
        # historical direction silently loses accumulated autonomy credit.
        agg["total_verified_autonomous_prs_merged"] += ev.get(
            "verified_autonomous_prs_merged",
            ev.get("autonomous_prs_merged", ev.get("prs_merged", 0)),
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
        # Legacy-0 fallback: pre-attribution entries have no estimated_cost_usd.
        try:
            agg["total_estimated_cost_usd"] += max(
                0.0, float(ev.get("estimated_cost_usd") or 0.0)
            )
        except (TypeError, ValueError):
            pass
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
            dq["cross_module_count_sum"] += qd.get("cross_module_count") or 0
            rate = qd.get("subtask_merge_rate")
            # Only plans that contribute to the weighted-average NUMERATOR may
            # contribute to its DENOMINATOR. A None-rate plan (subtasks exist
            # but no merged/failed-not-classified outcome yet) would otherwise
            # inflate subtask_count_sum without any numerator, biasing the
            # weighted merge-rate downward. This mirrors the live-history
            # consumers in spawn_calibration.py, which only add a plan's weight
            # to `total` when its rate is numeric.
            if isinstance(rate, (int, float)) and count > 0:
                dq["subtask_count_sum"] += count
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


def build_evicted_cycle_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """Compact per-cycle baseline stored under ``_evicted_cycle_snapshots``.

    Records the EXACT per-cycle contributions that :func:`_fold_evicted_into_aggregate`
    credited for one research_history entry, so a post-eviction late recompute
    (``_apply_direction_outcomes`` in ``direction_outcomes``) can compute correct
    incremental deltas for the aggregate keys once the live entry is gone.

    Every value here mirrors the same quantity the fold adds for this cycle:

      * the 11 primitive merge counters (with the fold's legacy fallbacks), and
      * the acceptance-verified + generalized baselines that the fold folds but
        that older snapshots omitted — namely the acceptance-verified merged base
        (``verified_merged_count`` credited only when the cycle is acceptance-aware,
        matching the fold's ``"acceptance_merged_write_total" in ev`` gate), the
        verified WRITE slice, generalized successes / terminal outcomes, and the
        per-kind outcome breakdown.

    This is the SINGLE constructor used by both snapshot-writing sites
    (``research_direction_state`` and ``research_completion``) so the two can
    never drift, and it is also used to refresh the snapshot after a delta is
    applied so repeated recomputes are no-ops.
    """
    acceptance_aware = "acceptance_merged_write_total" in entry
    return {
        "prs_merged": int(entry.get("prs_merged") or 0),
        "verified_prs_merged": int(
            entry.get("verified_prs_merged", entry.get("prs_merged", 0)) or 0
        ),
        "autonomous_prs_merged": int(
            entry.get("autonomous_prs_merged", entry.get("prs_merged", 0)) or 0
        ),
        "verified_autonomous_prs_merged": int(
            entry.get(
                "verified_autonomous_prs_merged",
                entry.get("autonomous_prs_merged", entry.get("prs_merged", 0)),
            )
            or 0
        ),
        "assisted_prs_merged": int(entry.get("assisted_prs_merged") or 0),
        "reachable_prs_merged": int(entry.get("reachable_prs_merged") or 0),
        "stranded_prs_merged": int(entry.get("stranded_prs_merged") or 0),
        "acceptance_proven_merged": int(entry.get("acceptance_proven_merged") or 0),
        "acceptance_consumed_merged": int(entry.get("acceptance_consumed_merged") or 0),
        "acceptance_declared_merged": int(entry.get("acceptance_declared_merged") or 0),
        "acceptance_inferred_merged": int(entry.get("acceptance_inferred_merged") or 0),
        "acceptance_merged_write_total": int(
            entry.get("acceptance_merged_write_total") or 0
        ),
        # Acceptance-verified + generalized baselines. Gated identically to the
        # fold so a cycle that is not acceptance-aware records 0 (and so the fold
        # and the delta cannot diverge on the acceptance-aware boundary).
        "acceptance_verified_merged": (
            verified_merged_count(entry) if acceptance_aware else 0
        ),
        "acceptance_verified_write_merged": (
            int(entry.get("acceptance_verified_write_merged") or 0)
            if acceptance_aware
            else 0
        ),
        "generalized_successes": _entry_generalized_successes(entry),
        "generalized_terminal_outcomes": _entry_generalized_terminal_outcomes(entry),
        "outcome_kind_breakdown": _copy_outcome_kind_breakdown(
            entry.get("outcome_kind_breakdown") or _fallback_outcome_kind_breakdown(entry)
        ),
    }


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

    # (e) grade quality signal — recency-weighted fraction of HIGH-grade findings.
    # Consumes the grade_distribution stored per research_history entry by
    # research_direction_state.  A direction that consistently produces HIGH-grade
    # findings scores better than one producing mostly LOW-grade noise.  Added ONLY
    # when at least one cycle in the window has grade_distribution data; cycles
    # without the key are neutral so legacy entries do not drag down new ones.
    grade_data_w = 0.0
    grade_high_w = 0.0
    for entry, w in zip(recent, weights, strict=True):
        gd = entry.get("grade_distribution") or {}
        high = gd.get("HIGH", 0) or 0
        medium = gd.get("MEDIUM", 0) or 0
        low = gd.get("LOW", 0) or 0
        total = high + medium + low
        if total > 0:
            grade_high_w += w * high / total
            grade_data_w += w
    if grade_data_w > 0:
        components.append(grade_high_w / grade_data_w)

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


def canonical_cost_per_merged_pr(
    total_cost: float, total_merged_prs: int
) -> float | None:
    """Return cost-per-merged-PR using a population-consistent formula.

    The numerator (total_cost) covers ALL work on a direction — autonomous AND
    operator-assisted. The denominator MUST cover the same population: ALL
    merged PRs (autonomous + assisted). Dividing by autonomous-only merges
    while including assisted costs in the numerator over-states the metric for
    any direction that accepted operator help, and can trigger spurious
    retirement flags. This helper is the single source of truth for that
    population rule so the three compute sites (digest_direction_progress,
    strategic_analysis, strategic_spawning) can never drift again.
    """
    if total_merged_prs <= 0:
        return None
    return round(total_cost / total_merged_prs, 4)



