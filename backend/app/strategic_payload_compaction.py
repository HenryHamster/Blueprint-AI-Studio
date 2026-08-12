"""Payload compaction helpers for strategic review context.

These functions are deliberately pure and schema-shaped: strategic_spawning.py
builds the full context, while this module decides how high-value sections
summarize or floor under pack_under_budget pressure.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

_VERSION_PHASE_RECENT_VERSIONS_CAP = 3
_VERSION_PHASE_RECENT_VERSIONS_FLOOR_CAP = 2
_VERSION_PHASE_TEXT_CAP = 500
_VERSION_PHASE_TEXT_FLOOR_CAP = 240
_VERSION_PHASE_CRITERIA_CAP = 20
_VERSION_PHASE_CRITERIA_FLOOR_CAP = 10
_DIRECTION_SUMMARY_TEXT_CAP = 260
_DIRECTION_SUMMARY_LONG_TEXT_CAP = 420
_DIRECTION_SUMMARY_LIST_CAP = 3
_DIRECTION_SUMMARY_EVIDENCE_CAP = 2


def _bounded_text(value: Any, cap: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= cap:
        return text
    if cap <= 3:
        return text[:cap]
    return f"{text[: cap - 3]}..."


def _put_present(
    out: dict[str, Any],
    source: dict,
    key: str,
    *,
    text_cap: int | None = None,
) -> None:
    if key not in source:
        return
    value = source.get(key)
    if text_cap is not None:
        value = _bounded_text(value, text_cap)
        if value is None:
            return
    out[key] = value


def _compact_scalar_mapping(value: Any, *, text_cap: int) -> dict | None:
    """Keep scalar map entries and bounded strings; drop nested bodies."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[str(key)] = _bounded_text(item, text_cap) if isinstance(item, str) else item
    return out or None


def _compact_policy_projection(value: Any, *, text_cap: int) -> dict | None:
    """Preserve the bounded policy contract, including its nested weights."""

    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("threshold", "source", "fallback_error", "policy_hash"):
        if key in value:
            item = value.get(key)
            out[key] = _bounded_text(item, text_cap) if isinstance(item, str) else item
    raw_weights = value.get("weights")
    if isinstance(raw_weights, dict):
        out["weights"] = {
            str(key)[:64]: item
            for key, item in list(sorted(raw_weights.items()))[:8]
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        }
    return out or None


def _compact_category_verdicts(value: Any, *, text_cap: int) -> list[dict] | None:
    """Compact per-category grader verdicts to their load-bearing fields.

    Each CategoryVerdict carries a status/tier/gap the strategist needs to know
    WHY a category is unmet, plus bulky citations that stay in the database.
    Keep the identity + status + tier + bounded gap text; drop citations and any
    other nested bodies so the payload stays small under budget pressure.
    """
    if not isinstance(value, list):
        return None
    out: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        compact: dict[str, Any] = {}
        for key in ("category_key", "status", "tier"):
            if key in row:
                compact[key] = row[key]
        gap = _bounded_text(row.get("gap"), text_cap)
        if gap:
            compact["gap"] = gap
        if compact:
            out.append(compact)
    return out or None


def _compact_qualification_provenance(value: Any, *, text_cap: int) -> dict | None:
    """Keep bounded scalar attempt provenance while dropping arbitrary bodies."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("active_projection", "selected_verdict", "retry_episode"):
        compact = _compact_scalar_mapping(value.get(key), text_cap=text_cap)
        if compact:
            out[key] = compact
    if isinstance(value.get("lifetime_attempts"), int):
        out["lifetime_attempts"] = value["lifetime_attempts"]
    children = value.get("child_tasks")
    if isinstance(children, dict):
        compact_children = {
            str(name): compact
            for name, child in children.items()
            if (compact := _compact_scalar_mapping(child, text_cap=text_cap))
        }
        if compact_children:
            out["child_tasks"] = compact_children
    return out or None


def _compact_dimension_states(value: Any, *, text_cap: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for row in value[:14]:
        if not isinstance(row, dict):
            continue
        compact = {
            key: row.get(key)
            for key in ("key", "label", "status", "score", "reason")
            if key in row
        }
        provenance = _compact_scalar_mapping(row.get("provenance"), text_cap=text_cap)
        if provenance:
            compact["provenance"] = provenance
        if compact:
            out.append(compact)
    return out


def _compact_release_artifacts(rows: Any) -> list[dict]:
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows[:6]:
        if not isinstance(row, dict):
            continue
        compact: dict[str, Any] = {}
        for key in (
            "artifact_kind",
            "required",
            "serving_status",
            "served_identifier",
            "served_at",
            "reason_code",
        ):
            if key in row:
                compact[key] = row.get(key)
        if compact:
            out.append(compact)
    return out


def _compact_promotion_pending(value: Any, *, text_cap: int) -> dict | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "pr_number",
        "pr_url",
        "enabled_at",
        "managed_check_wait",
        "promotion_age_seconds",
        "promotion_timeout_seconds",
        "promotion_past_timeout",
        "promotion_nearing_timeout",
        "ci_state",
    ):
        _put_present(out, value, key, text_cap=text_cap if key == "pr_url" else None)
    snapshot = value.get("check_snapshot")
    if isinstance(snapshot, dict):
        out["check_snapshot"] = {
            k: v
            for k, v in snapshot.items()
            if isinstance(v, (str, int, float, bool)) or v is None
        }
    return out or None


def _compact_cert(value: Any, *, text_cap: int) -> dict | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "state",
        "status",
        "deterministic_grade",
        "assessed_grade",
        "threshold_warn",
        "threshold_block",
        "coverage",
        "blocked",
        "biggest_gap",
        "directional_contradiction",
        "net_loc",
        "goal_alignment",
        "reason",
        "summary",
    ):
        _put_present(
            out,
            value,
            key,
            text_cap=text_cap if key in {"biggest_gap", "reason", "summary"} else None,
        )
    scores = _compact_scalar_mapping(value.get("scores"), text_cap=text_cap)
    if scores:
        out["scores"] = scores
    return out or None


def _compact_verdict_like(value: Any, *, text_cap: int) -> dict | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "status",
        "state",
        "verdict",
        "error",
        "detail",
        "reason",
        "summary",
        "blocked",
        "confidence",
    ):
        _put_present(
            out,
            value,
            key,
            text_cap=text_cap if key in {"detail", "reason", "summary"} else None,
        )
    for list_key in (
        "findings",
        "verdicts",
        "challenge_verdicts",
        "confirmed",
        "refuted_false_positive",
        "uncertain",
    ):
        item = value.get(list_key)
        if isinstance(item, list):
            out[f"{list_key}_count"] = len(item)
    return out or None


def _compact_last_qualification(value: Any, *, floor: bool = False) -> dict | None:
    if not isinstance(value, dict) or not value:
        return None
    text_cap = _VERSION_PHASE_TEXT_FLOOR_CAP if floor else _VERSION_PHASE_TEXT_CAP
    out: dict[str, Any] = {}
    for key in (
        "status",
        "qualification_attempt_id",
        "retry_episode",
        "retry_episode_cap",
        "candidate_sha",
        "ci_green",
        "smoketest_enabled",
        "confidence",
        "blocker",
        "threshold",
        "started_at",
        "finalized_at",
        "audit_task_id",
        "challenge_task_id",
        "audit_challenge_task_id",
        "smoketest_task_id",
        "merge_grade_task_id",
        "audit_challenge_spawn_error",
        "audit_challenge_retry_reason",
        "audit_challenge_respawn_count",
        "audit_challenge_retry_primary_findings_count",
        "challenge_infra_respawn_count",
        "audit_missing_reason",
        "migration_safety",
        "migration_apply_safety",
        "merge_grade_status",
        "merge_grade_coverage",
        "would_promote",
        "error",
        "promotion_target",
        "first_deferred_at",
        "deferred_at",
        "prior_superseded",
        "qualify_request_blocked_at",
        "debt_enforce_confidence_preview",
        "debt_enforce_weight_preview",
    ):
        _put_present(out, value, key)
    for key in (
        "detail",
        "reason",
        "test_erosion_detail",
        "smoketest_detail",
        "migration_safety_detail",
        "migration_apply_safety_detail",
        "merge_grade_biggest_gap",
    ):
        _put_present(out, value, key, text_cap=text_cap)
    for key in (
        "audit_findings_total",
        "audit_findings_truncated",
        "excluded_audit_findings_total",
        "excluded_audit_findings_truncated",
        "challenge_excluded_findings_count",
    ):
        if key in value:
            out[key] = value.get(key)
    for source_key, count_key in (
        ("audit_findings", "audit_findings_count"),
        ("excluded_audit_findings", "excluded_audit_findings_count"),
        ("challenger_refuted_audit_findings", "challenger_refuted_audit_findings_count"),
    ):
        if isinstance(value.get(source_key), list):
            out.setdefault(count_key, len(value[source_key]))
    sub_scores = _compact_scalar_mapping(value.get("sub_scores"), text_cap=text_cap)
    raw_sub_scores = value.get("sub_scores")
    raw_task_records = (
        raw_sub_scores.get("acceptance_window_task_records")
        if isinstance(raw_sub_scores, dict)
        else None
    )
    if isinstance(raw_task_records, list):
        sub_scores = sub_scores or {}
        sub_scores["acceptance_window_task_records"] = [
            {
                key: row[key]
                for key in (
                    "task_id",
                    "bucket",
                    "credited",
                    "_pr_changed_files_stamp_skipped",
                )
                if key in row
            }
            for row in raw_task_records
            if isinstance(row, dict)
        ]
    if sub_scores:
        out["sub_scores"] = sub_scores
    # Per-category grader verdicts — WHY each category is unmet. Bounded to the
    # load-bearing identity/status/tier/gap fields so the strategist sees the
    # grader's reason without a fetch round-trip (citations stay in the DB).
    category_verdicts = _compact_category_verdicts(
        value.get("category_verdicts"), text_cap=text_cap
    )
    if category_verdicts:
        out["category_verdicts"] = category_verdicts
    residual_gap = _bounded_text(value.get("residual_uncaptured_gap"), text_cap)
    if residual_gap:
        out["residual_uncaptured_gap"] = residual_gap
    acceptance = value.get("acceptance_aggregate")
    if isinstance(acceptance, dict):
        out["acceptance_aggregate"] = acceptance
    qrb = _compact_scalar_mapping(value.get("qualify_request_blocked"), text_cap=text_cap)
    if qrb:
        out["qualify_request_blocked"] = qrb
    promo = _compact_promotion_pending(value.get("promotion_pending"), text_cap=text_cap)
    if promo:
        out["promotion_pending"] = promo
    ci_resolution = _compact_verdict_like(value.get("ci_resolution"), text_cap=text_cap)
    if ci_resolution:
        out["ci_resolution"] = ci_resolution
    audit_reconciliation = _compact_verdict_like(
        value.get("audit_reconciliation"), text_cap=text_cap
    )
    if audit_reconciliation:
        out["audit_reconciliation"] = audit_reconciliation
    challenge_summary = _compact_verdict_like(value.get("challenge_verdicts"), text_cap=text_cap)
    if challenge_summary:
        out["challenge_verdicts_summary"] = challenge_summary
    merge_grade = _compact_cert(value.get("merge_grade"), text_cap=text_cap)
    if merge_grade:
        out["merge_grade"] = merge_grade
    goal_alignment = _compact_cert(value.get("goal_alignment"), text_cap=text_cap)
    if goal_alignment:
        out["goal_alignment"] = goal_alignment
    return out or None


def _compact_qualification_state(value: Any, *, floor: bool = False) -> dict | None:
    if not isinstance(value, dict) or not value:
        return None
    text_cap = _VERSION_PHASE_TEXT_FLOOR_CAP if floor else _VERSION_PHASE_TEXT_CAP
    out: dict[str, Any] = {}
    for key in (
        "state",
        "serving_status",
        "deploy_failure_reason",
        "blocker",
        "promotion_pr_number",
        "promotion_pr_url",
        "promotion_age_seconds",
        "promotion_timeout_seconds",
        "promotion_past_timeout",
        "promotion_nearing_timeout",
        "error",
    ):
        _put_present(out, value, key, text_cap=text_cap if key == "promotion_pr_url" else None)
    for key in ("summary", "detail"):
        _put_present(out, value, key, text_cap=text_cap)
    artifacts = _compact_release_artifacts(value.get("artifacts"))
    if artifacts:
        out["artifacts"] = artifacts
    return out or None


def _compact_promotion_readiness(value: Any, *, floor: bool = False) -> dict | None:
    if not isinstance(value, dict) or not value:
        return None
    text_cap = _VERSION_PHASE_TEXT_FLOOR_CAP if floor else _VERSION_PHASE_TEXT_CAP
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"thresholds", "blocker_counts", "confidence", "recent_verdicts"}:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[key] = _bounded_text(item, text_cap) if isinstance(item, str) else item
    conf = value.get("confidence")
    if isinstance(conf, dict):
        out["confidence"] = {
            k: conf.get(k) for k in ("mean", "stdev", "stable") if k in conf
        }
    blockers = value.get("recurring_blockers")
    if isinstance(blockers, list):
        compact_blockers: list[Any] = []
        for blocker in blockers[:5]:
            if isinstance(blocker, dict):
                compact = _compact_scalar_mapping(blocker, text_cap=text_cap)
                if compact:
                    compact_blockers.append(compact)
            elif isinstance(blocker, str):
                compact = _bounded_text(blocker, text_cap)
                if compact:
                    compact_blockers.append(compact)
        if compact_blockers:
            out["recurring_blockers"] = compact_blockers
    elif isinstance(blockers, dict):
        out["recurring_blockers"] = {
            str(k): v
            for k, v in list(blockers.items())[:5]
            if isinstance(v, (str, int, float, bool)) or v is None
        }
    return out or None


def _compact_finding_resolution(value: Any, *, text_cap: int) -> dict | None:
    """Compact the version_phase finding-resolution block.

    Keeps the per-finding keys/severity/state — load-bearing for dispatching
    remediation and recording ledger resolutions — while bounding summaries and
    dropping verbose provenance, so the block survives budget pressure. The raw
    carried ``audit_findings`` list is stripped to a count by the compact
    qualification serializer, so this block is the durable carrier of the keys.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "requalification_ready",
        "gating_total",
        "gating_resolved",
        "audit_findings_truncated",
        "finding_total",
        "resolved_finding_total",
        "projected_audit_score",
        "required_audit_score",
        "projected_confidence",
        "promotion_confidence_threshold",
        "projected_promotion_ready",
    ):
        if value.get(key) is not None:
            out[key] = value.get(key)
    reason = _bounded_text(value.get("reason"), text_cap)
    if reason:
        out["reason"] = reason
    findings = value.get("findings")
    if isinstance(findings, list):
        compact: list[dict[str, Any]] = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            entry: dict[str, Any] = {}
            for fkey in ("key", "severity", "state"):
                if f.get(fkey) is not None:
                    entry[fkey] = f.get(fkey)
            summary = _bounded_text(f.get("summary"), text_cap)
            if summary:
                entry["summary"] = summary
            if entry:
                compact.append(entry)
        if compact:
            out["findings"] = compact
    return out or None


def _compact_recent_version_entry(value: Any, *, floor: bool = False) -> dict | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "version_number",
        "status",
        "phase",
        "tasks_merged_this_version",
        "serving_status",
        "served_sha",
        "deploy_failure_reason",
    ):
        _put_present(out, value, key)
    acceptance = value.get("acceptance_aggregate")
    if isinstance(acceptance, dict):
        out["acceptance_aggregate"] = acceptance
    lq = _compact_last_qualification(value.get("last_qualification"), floor=floor)
    if lq:
        out["last_qualification"] = lq
    qs = _compact_qualification_state(value.get("qualification_state"), floor=floor)
    if qs:
        out["qualification_state"] = qs
    artifacts = _compact_release_artifacts(value.get("artifacts"))
    if artifacts:
        out["artifacts"] = artifacts
    return out or None


def _compact_version_phase(block: dict, *, floor: bool = False) -> dict:
    if not isinstance(block, dict) or not block:
        return {}
    out: dict[str, Any] = {}
    text_cap = _VERSION_PHASE_TEXT_FLOOR_CAP if floor else _VERSION_PHASE_TEXT_CAP
    for key in (
        "lifecycle_signal",
        "signal_status",
        "build_unavailable",
        "error",
        "integration_branch",
        "promotion_target",
        "version_number",
        "phase",
        "goal",
        "goal_needed",
        "phase_changed_at",
        "tasks_merged_this_version",
        "qualification_attempts",
        "qualification_retry_episode",
        "active_qualification_attempt_id",
        "serving_status",
        "served_sha",
        "served_at",
        "deploy_failure_reason",
    ):
        _put_present(
            out,
            block,
            key,
            text_cap=text_cap if key == "goal" else None,
        )
    _put_present(out, block, "phase_reasoning", text_cap=text_cap)
    acceptance = block.get("acceptance_aggregate")
    if isinstance(acceptance, dict):
        out["acceptance_aggregate"] = acceptance
    lq = _compact_last_qualification(block.get("last_qualification"), floor=floor)
    if lq:
        out["last_qualification"] = lq
    qs = _compact_qualification_state(block.get("qualification_state"), floor=floor)
    if qs:
        out["qualification_state"] = qs
    for policy_key in (
        "current_effective_policy",
        "active_or_last_attempt_policy",
    ):
        policy_projection = _compact_policy_projection(
            block.get(policy_key), text_cap=text_cap
        )
        if policy_projection:
            out[policy_key] = policy_projection
    provenance = _compact_qualification_provenance(
        block.get("qualification_provenance"), text_cap=text_cap
    )
    if provenance:
        out["qualification_provenance"] = provenance
    dimension_states = _compact_dimension_states(
        block.get("dimension_states"), text_cap=text_cap
    )
    if dimension_states:
        out["dimension_states"] = dimension_states
    # Small scalar dict {last_qualification_status, verdict_terminal_status}
    # emitted when a finalized verdict superseded a stale pending record. Kept so
    # the finalized-verdict disagreement survives packing and stays consistent
    # with the agent fetch verb's ``qualification_state_disagreement`` field.
    disagreement = _compact_scalar_mapping(
        block.get("qualification_state_disagreement"), text_cap=text_cap
    )
    if disagreement:
        out["qualification_state_disagreement"] = disagreement
    artifacts = _compact_release_artifacts(block.get("artifacts"))
    if artifacts:
        out["artifacts"] = artifacts
    pr = _compact_promotion_readiness(block.get("promotion_readiness"), floor=floor)
    if pr:
        out["promotion_readiness"] = pr
    drift = _compact_scalar_mapping(block.get("goal_coverage_drift"), text_cap=text_cap)
    if drift:
        out["goal_coverage_drift"] = drift
    trigger = block.get("qualification_trigger_state")
    if isinstance(trigger, dict):
        compact_trigger = _compact_scalar_mapping(trigger, text_cap=text_cap) or {}
        for key in ("error_retry_episode", "active_projection", "selected_verdict"):
            nested = _compact_scalar_mapping(trigger.get(key), text_cap=text_cap)
            if nested:
                compact_trigger[key] = nested
        trigger_provenance = _compact_qualification_provenance(
            trigger.get("qualification_provenance"), text_cap=text_cap
        )
        if trigger_provenance:
            compact_trigger["qualification_provenance"] = trigger_provenance
        if compact_trigger:
            out["qualification_trigger_state"] = compact_trigger
    coverage = block.get("criterion_coverage")
    if isinstance(coverage, dict):
        compact_coverage: dict[str, Any] = {}
        for key in ("total", "covered", "attested", "partial", "uncovered", "stuck_count"):
            if key in coverage:
                compact_coverage[key] = coverage[key]
        criteria = coverage.get("criteria")
        if isinstance(criteria, list):
            if not floor:
                # Normal path: include per-criterion list capped to bounded length.
                compact_coverage["criteria"] = criteria[:_VERSION_PHASE_CRITERIA_CAP]
            else:
                # Floor path: include a tightly capped, priority-sorted subset so
                # the strategist knows WHICH criteria need attention without a fetch.
                # Priority order: stuck first, then uncovered, then partial.
                # Keep only load-bearing identity/state fields.
                def _criterion_sort_key(c: dict) -> int:
                    if not isinstance(c, dict):
                        return 3
                    if c.get("stuck_reason") is not None:
                        return 0
                    cov = c.get("coverage")
                    if cov == "uncovered":
                        return 1
                    if cov == "partial":
                        return 2
                    return 3

                sorted_criteria = sorted(criteria, key=_criterion_sort_key)
                floor_criteria = []
                for c in sorted_criteria[:_VERSION_PHASE_CRITERIA_FLOOR_CAP]:
                    if not isinstance(c, dict):
                        continue
                    entry: dict[str, Any] = {}
                    # ``status`` is load-bearing: the strategic renderer
                    # (formatCriteriaRubric) filters the rubric to
                    # ``status == 'active'`` criteria, so a floored entry without it
                    # renders as "None authored yet" — the exact floored-out/no-criteria
                    # conflation this floor path exists to avoid. Keep it (plus the small
                    # identity/text/stuck fields) so the rubric stays truthful under budget.
                    for fkey in ("criterion_key", "coverage", "status", "stuck_reason"):
                        if fkey in c:
                            entry[fkey] = c[fkey]
                    text = _bounded_text(c.get("text"), _VERSION_PHASE_TEXT_FLOOR_CAP)
                    if text:
                        entry["text"] = text
                    if entry:
                        floor_criteria.append(entry)
                if floor_criteria:
                    compact_coverage["criteria"] = floor_criteria
        if compact_coverage:
            out["criterion_coverage"] = compact_coverage
    for key in (
        "last_qualification_candidate_currency",
        "stabilize_stall",
        "build_stall",
    ):
        compact = _compact_scalar_mapping(block.get(key), text_cap=text_cap)
        if compact:
            out[key] = compact
    fr = _compact_finding_resolution(block.get("finding_resolution"), text_cap=text_cap)
    if fr:
        out["finding_resolution"] = fr
    rv = block.get("recent_versions")
    if isinstance(rv, list):
        cap = (
            _VERSION_PHASE_RECENT_VERSIONS_FLOOR_CAP
            if floor
            else _VERSION_PHASE_RECENT_VERSIONS_CAP
        )
        compact_recent = [
            compact
            for entry in rv[:cap]
            if (compact := _compact_recent_version_entry(entry, floor=floor))
        ]
        if compact_recent:
            out["recent_versions"] = compact_recent
    return out


def _summarize_version_phase(block: dict) -> dict:
    """Trim version_phase for inclusion under budget pressure.

    Keeps lifecycle facts but removes verbose audit artifacts/findings and
    nested task bodies so the version context degrades instead of disappearing.
    """
    return _compact_version_phase(block, floor=False)


def _version_phase_floor(block: dict) -> dict:
    """Guaranteed-small lifecycle anchor for version_phase."""
    return _compact_version_phase(block, floor=True)


def _summarize_drift_signals(drift: dict) -> dict:
    """Return a payload-friendly subset of drift_detection.compute_direction_drift.

    Strips per-direction signals when the project has many directions and
    keeps only flagged or non-neutral entries, so the strategic payload
    doesn't carry zero-information rows for every direction.
    """
    if not drift:
        return {}
    by_direction = drift.get("by_direction") or {}
    by_system = drift.get("by_system") or {}
    # Keep flagged directions in full; for others summarize only if the
    # consistency dropped below 0.8.
    interesting_directions = {
        did: sig
        for did, sig in by_direction.items()
        if (
            sig.get("over_claim_flag")
            or sig.get("stalled_despite_churn_flag")
            or (
                sig.get("claim_shipped_consistency", 1.0) < 0.8
                and sig.get("claimed_changes_count", 0) > 0
            )
        )
    }
    return {
        "by_direction": interesting_directions,
        "by_system": by_system,
        "generated_at": drift.get("generated_at"),
        "window_days": drift.get("window_days"),
    }


# How many stored snapshots to look back when computing consecutive_flagged_cycles.
_DRIFT_TREND_LOOKBACK = 5


async def _enrich_drift_with_trend(
    session: AsyncSession,
    project_id: uuid.UUID,
    drift_summary: dict,
) -> dict:
    """Add consecutive_flagged_cycles to each direction already in drift_summary.

    For each interesting direction, reads the last _DRIFT_TREND_LOOKBACK stored
    snapshots (newest first) and counts how many consecutive leading entries had
    at least one flag set. This lets the strategic agent distinguish a one-off
    spike from a sustained breakdown without reading the full history.

    Only enriches directions already filtered into drift_summary by
    _summarize_drift_signals, so the extra queries are bounded by the
    flagged-direction count (typically 0-3 per project).
    """
    from app.drift_detection import last_n_drift_snapshots

    by_direction = (drift_summary or {}).get("by_direction") or {}
    if not by_direction:
        return drift_summary
    enriched_directions = {}
    for direction_id, sig in by_direction.items():
        snapshots = await last_n_drift_snapshots(
            session, project_id, direction_id, n=_DRIFT_TREND_LOOKBACK
        )
        consecutive = 0
        for snap in snapshots:
            if snap.over_claim_flag or snap.stalled_despite_churn_flag:
                consecutive += 1
            else:
                break
        enriched_sig = dict(sig)
        if consecutive > 0:
            enriched_sig["consecutive_flagged_cycles"] = consecutive
        enriched_directions[direction_id] = enriched_sig
    return {**drift_summary, "by_direction": enriched_directions}


# Per-payload caps used by the strategic-spawn schema's summarize_fns. These are
# what the agent sees by default; for full content the agent uses ./fetch.
_DIRECTIONS_SUMMARY_KEEP = 12
_OUTCOME_DIRECTION_OUTCOMES_KEEP = 10
_CONVERSATION_THREAD_KEEP_WHEN_TRIMMED = 8

# Core-floor caps for directions_summary. The floor is the guaranteed-minimum
# active-direction core the strategist ALWAYS receives even under extreme byte
# pressure — tighter than the summarize path (_DIRECTIONS_SUMMARY_KEEP) and
# stripped to the load-bearing identity/status scalars plus a short headline.
# Fuller per-direction detail (and dropped directions) stay fetchable via
# ``./fetch direction <id>``.
_DIRECTIONS_FLOOR_KEEP = 5
_DIRECTION_FLOOR_TEXT_CAP = 120


def _direction_activity_score(stat: dict) -> int:
    """Sort key for choosing which direction stats stay in the strategic payload.

    Higher = more activity. Combines work shipped (merged), work pending (failed),
    and recency (cycles). High-priority directions get a small boost so a quiet
    high-priority direction doesn't get evicted by a chatty low-priority one.

    "Work shipped" prefers REACHABLE merges (``reachable_prs_merged``: commits
    confirmed landed on integration branch), falling back to VERIFIED merges
    (``verified_prs_merged``: CI/acceptance-bucket-verified), then raw
    ``prs_merged`` for back-compat. This ensures directions with stranded merges
    (CI-passing but never landed) do not crowd out directions whose work truly shipped.
    """
    if not isinstance(stat, dict):
        return 0
    merged = int(
        stat["reachable_prs_merged"]
        if "reachable_prs_merged" in stat
        else (stat.get("verified_prs_merged", stat.get("prs_merged", 0)) or 0)
    )
    failed = int(stat.get("tasks_failed") or 0)
    cycles = int(stat.get("total_cycles") or 0)
    priority = stat.get("priority")
    priority_bonus = {"high": 5, "medium": 0, "low": -2}.get(priority, 0)
    proposed_bonus = 3 if stat.get("status") == "proposed" else 0
    return merged * 3 + failed + cycles + priority_bonus + proposed_bonus


def _direction_decision_value(stat: dict) -> int:
    """Rank direction context by the decision it can change this cycle.

    A direction with fresh findings and no task in flight is the strongest
    spawn signal in the strategic portfolio. Give that signal a separate rank
    above historical activity so a quiet, actionable direction is not evicted
    by generic summaries.
    """
    if not isinstance(stat, dict):
        return 0
    findings = stat.get("recent_findings")
    finding_count = (
        len(findings)
        if isinstance(findings, list)
        else int(stat.get("recent_findings_total") or 0)
    )
    in_flight = stat.get("in_flight", stat.get("tasks_in_flight", 0))
    try:
        in_flight_count = max(0, int(in_flight or 0))
    except (TypeError, ValueError):
        in_flight_count = 0
    spawn_signal = (
        stat.get("status") == "active" and finding_count > 0 and in_flight_count == 0
    )
    return (1_000_000 if spawn_signal else 0) + _direction_activity_score(stat)


def _directions_section_decision_value(value: Any) -> int:
    """Return the strongest direction decision signal in a section."""
    if not isinstance(value, list):
        return 0
    # Keep the existing tier/size behavior for ordinary portfolios. Only the
    # actionable finding-without-work signal changes section ordering; this
    # prevents a quiet direction's normal activity score from reshuffling
    # unrelated high-tier lifecycle sections.
    scores = [_direction_decision_value(item) for item in value]
    return max((score for score in scores if score >= 1_000_000), default=0)


def _payload_currency_decision_value(value: Any) -> int:
    """Keep resolved currency context ahead of generic high-tier summaries."""
    if not isinstance(value, dict):
        return 0
    verdict = value.get("verdict")
    if verdict in ("fresh", "stale"):
        return 900_000
    if verdict in ("unknown", "api_unreachable"):
        return 800_000
    return 700_000


def _summarize_directions(stats: list) -> list:
    """Keep top direction_summary entries and compact bulky per-entry evidence.

    Dropped directions are still discoverable by id via the agent's
    ``./fetch direction <id>`` helper, so the agent can pull the full
    record when it needs to reason about a quiet direction.
    """
    if not isinstance(stats, list):
        return stats
    sorted_stats = sorted(stats, key=_direction_decision_value, reverse=True)
    return [
        _compact_direction_summary_entry(stat)
        for stat in sorted_stats[:_DIRECTIONS_SUMMARY_KEEP]
        if isinstance(stat, dict)
    ]


def _floor_direction_entry(stat: dict) -> dict:
    """Ultra-compact per-direction skeleton for the core floor.

    Keeps only the load-bearing identity/status scalars plus a short text
    headline — enough for the strategist to know which active directions exist
    and their state. Everything else is fetchable via ``./fetch direction <id>``.
    """
    out: dict[str, Any] = {}
    for key in ("id", "priority", "status", "gap_id", "system_id", "in_flight"):
        _put_present(out, stat, key)
    headline = _bounded_text(stat.get("text"), _DIRECTION_FLOOR_TEXT_CAP)
    if headline:
        out["text"] = headline
    research_context = _bounded_text(
        stat.get("research_context"), _DIRECTION_SUMMARY_TEXT_CAP
    )
    if research_context:
        out["research_context"] = research_context
    return out


def _directions_summary_floor(stats: list) -> list:
    """Guaranteed-minimum core floor for ``directions_summary``.

    Keeps the top :data:`_DIRECTIONS_FLOOR_KEEP` highest-activity directions as
    compact id/priority/status/headline skeletons so the active-direction core
    NEVER disappears entirely under budget pressure. This is the floor callback
    registered in the strategic pack schema; the packer reserves its bytes off
    the top of the budget so recoverable siblings degrade first. Fuller detail
    and dropped directions remain fetchable via ``./fetch direction <id>``.
    """
    if not isinstance(stats, list):
        return stats
    sorted_stats = sorted(stats, key=_direction_decision_value, reverse=True)
    return [
        _floor_direction_entry(stat)
        for stat in sorted_stats[:_DIRECTIONS_FLOOR_KEEP]
        if isinstance(stat, dict)
    ]


def _compact_evidence_record(value: Any) -> dict | None:
    if isinstance(value, str):
        headline = _bounded_text(value, _DIRECTION_SUMMARY_TEXT_CAP)
        return {"summary": headline} if headline else None
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in (
        "id",
        "finding_id",
        "grade",
        "verification_basis",
        "challenge_status",
        "status",
        "created_at",
    ):
        _put_present(out, value, key)
    for key in ("source", "source_ref"):
        source = value.get(key)
        if isinstance(source, dict):
            compact_source = _compact_scalar_mapping(source, text_cap=120)
            if compact_source:
                out[key] = compact_source
        elif source is not None:
            text = _bounded_text(source, 120)
            if text:
                out[key] = text
    for key in ("summary", "description", "finding", "claim", "rationale"):
        if key in value:
            text = _bounded_text(value.get(key), _DIRECTION_SUMMARY_TEXT_CAP)
            if text:
                out["summary"] = text
                break
    return out or None


def _compact_failure_record(value: Any) -> dict | str | None:
    if isinstance(value, str):
        return _bounded_text(value, _DIRECTION_SUMMARY_TEXT_CAP)
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("task_id", "title", "task_type", "failure_class", "status"):
        _put_present(out, value, key, text_cap=160 if key == "title" else None)
    for key in ("message", "error", "latest_status_message"):
        if key in value:
            text = _bounded_text(value.get(key), _DIRECTION_SUMMARY_TEXT_CAP)
            if text:
                out["message"] = text
                break
    return out or None


def _compact_spawn_gate_metrics(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key.endswith("_samples") or key == "refuted_reasons":
            continue
        if isinstance(item, (int, float, bool)) or item is None:
            out[key] = item
        elif isinstance(item, dict):
            out[key] = {
                str(k): v
                for k, v in item.items()
                if isinstance(v, (int, float, bool)) or v is None
            }
    refuted = value.get("refuted_reasons")
    if isinstance(refuted, list):
        reasons = [
            text
            for item in refuted[:_DIRECTION_SUMMARY_LIST_CAP]
            if (text := _bounded_text(item, _DIRECTION_SUMMARY_TEXT_CAP))
        ]
        if reasons:
            out["refuted_reasons"] = reasons
            out["refuted_reasons_total"] = len(refuted)
    for key in (
        "manifest_excluded_fail_closed_samples",
        "adversarial_give_up_samples",
    ):
        samples = value.get(key)
        if isinstance(samples, list):
            compact_samples = [
                compact
                for item in samples[:_DIRECTION_SUMMARY_LIST_CAP]
                if (compact := _compact_evidence_record(item))
            ]
            if compact_samples:
                out[key] = compact_samples
                out[f"{key}_total"] = len(samples)
    return out or None


def _compact_retired_grounding(value: Any) -> dict | None:
    """Preserve a direction's ``retired_direction_grounding`` through compaction.

    Routing this through ``_compact_scalar_mapping`` would drop the nested
    ``metrics`` and ``retirement_memo`` bodies, so keep those explicitly while
    still bounding every string field.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("direction_id", "classification", "reason", "gap_id"):
        _put_present(out, value, key, text_cap=_DIRECTION_SUMMARY_TEXT_CAP)
    metrics = _compact_scalar_mapping(value.get("metrics"), text_cap=_DIRECTION_SUMMARY_TEXT_CAP)
    if metrics:
        out["metrics"] = metrics
    memo = value.get("retirement_memo")
    if isinstance(memo, dict):
        compact_memo: dict[str, Any] = {}
        for key in ("knowledge_doc_id", "summary"):
            _put_present(compact_memo, memo, key, text_cap=_DIRECTION_SUMMARY_TEXT_CAP)
        if compact_memo:
            out["retirement_memo"] = compact_memo
    return out or None


def _compact_direction_summary_entry(stat: dict) -> dict:
    out: dict[str, Any] = {}
    for key in (
        "id",
        "priority",
        "status",
        "in_flight",
        "total_cycles",
        "prs_merged",
        "verified_prs_merged",
        "reachable_prs_merged",
        "badge_only_prs_merged",
        "proven_prs_merged",
        "consumed_prs_merged",
        "acceptance_declaration_coverage",
        "tasks_failed",
        "tasks_failed_infra",
        "possibly_saturated",
        "gap_id",
        "quality_score",
        "lifetime_autonomous_prs_merged",
        "lifetime_operator_actions",
        "retirement_eligible",
        "prs_rejected",
        "operator_rejection_rate",
        "estimated_cost_usd",
        "cost_per_merged_pr",
        "system_id",
        "auto_paused",
        "proposed_by",
        "proposed_at",
        "recommended_mode",
        "confirmed_finding_count",
        "confirmed_finding_trend",
        "outcome_scaffolding_flagged",
        "churn_retirement_recommended",
    ):
        _put_present(out, stat, key)
    for key in (
        "text",
        "paused_reason",
        "proposal_rationale",
        "success_evidence",
        "retirement_condition",
        "outcome_scaffolding_evidence",
    ):
        _put_present(out, stat, key, text_cap=_DIRECTION_SUMMARY_LONG_TEXT_CAP)
    research_context = _bounded_text(
        stat.get("research_context"), _DIRECTION_SUMMARY_LONG_TEXT_CAP
    )
    if research_context:
        out["research_context"] = research_context
    for key in (
        "taxonomy_summary",
        "recent_findings_grade_distribution",
        "recent_findings_verification_basis",
        "recent_findings_challenge_summary",
        "churn_retirement_evidence",
    ):
        compact = _compact_scalar_mapping(stat.get(key), text_cap=_DIRECTION_SUMMARY_TEXT_CAP)
        if compact:
            out[key] = compact
    findings = stat.get("recent_findings")
    if isinstance(findings, list):
        compact_findings = [
            text
            for item in findings[:_DIRECTION_SUMMARY_LIST_CAP]
            if (text := _bounded_text(item, _DIRECTION_SUMMARY_TEXT_CAP))
        ]
        if compact_findings:
            out["recent_findings"] = compact_findings
            out["recent_findings_total"] = len(findings)
    evidence = stat.get("recent_findings_evidence")
    if isinstance(evidence, list):
        compact_evidence = [
            compact
            for item in evidence[:_DIRECTION_SUMMARY_EVIDENCE_CAP]
            if (compact := _compact_evidence_record(item))
        ]
        if compact_evidence:
            out["recent_findings_evidence"] = compact_evidence
            out["recent_findings_evidence_total"] = len(evidence)
    failures = stat.get("recent_failures")
    if isinstance(failures, list):
        compact_failures = [
            compact
            for item in failures[:_DIRECTION_SUMMARY_LIST_CAP]
            if (compact := _compact_failure_record(item))
        ]
        if compact_failures:
            out["recent_failures"] = compact_failures
            out["recent_failures_total"] = len(failures)
    spawn_gate = _compact_spawn_gate_metrics(stat.get("spawn_gate_metrics"))
    if spawn_gate:
        out["spawn_gate_metrics"] = spawn_gate
    grounding = _compact_retired_grounding(stat.get("retired_direction_grounding"))
    if grounding:
        out["retired_direction_grounding"] = grounding
    return out
