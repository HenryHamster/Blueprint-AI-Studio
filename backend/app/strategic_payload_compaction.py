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
        "auto_merge_enabled",
        "enabled_at",
        "managed_check_wait",
        "migration_carrying",
        "why_not_native_automerge",
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
        "theme_class",
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
    if sub_scores:
        out["sub_scores"] = sub_scores
    acceptance = value.get("acceptance_aggregate")
    if isinstance(acceptance, dict):
        out["acceptance_aggregate"] = acceptance
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
    theme_delivery = _compact_cert(value.get("theme_delivery"), text_cap=text_cap)
    if theme_delivery:
        out["theme_delivery"] = theme_delivery
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
        "migration_carrying",
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
        "version_number",
        "phase",
        "goal",
        "theme",
        "goal_needed",
        "theme_needed",
        "phase_changed_at",
        "tasks_merged_this_version",
        "qualification_attempts",
        "serving_status",
        "served_sha",
        "served_at",
        "deploy_failure_reason",
    ):
        _put_present(
            out,
            block,
            key,
            text_cap=text_cap if key in {"goal", "theme"} else None,
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
    artifacts = _compact_release_artifacts(block.get("artifacts"))
    if artifacts:
        out["artifacts"] = artifacts
    pr = _compact_promotion_readiness(block.get("promotion_readiness"), floor=floor)
    if pr:
        out["promotion_readiness"] = pr
    for key in (
        "last_qualification_candidate_currency",
        "stabilize_stall",
        "build_stall",
    ):
        compact = _compact_scalar_mapping(block.get(key), text_cap=text_cap)
        if compact:
            out[key] = compact
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


def _direction_activity_score(stat: dict) -> int:
    """Sort key for choosing which direction stats stay in the strategic payload.

    Higher = more activity. Combines work shipped (merged), work pending (failed),
    and recency (cycles). High-priority directions get a small boost so a quiet
    high-priority direction doesn't get evicted by a chatty low-priority one.

    "Work shipped" counts VERIFIED merges (``verified_prs_merged``, populated in
    the direction_stats loop) so a direction whose merges never actually landed —
    or landed degraded/unverified — does not win payload space over one that
    truly shipped. Falls back to the raw ``prs_merged`` for back-compat with
    stat dicts that predate the verified field.
    """
    if not isinstance(stat, dict):
        return 0
    merged = int(stat.get("verified_prs_merged", stat.get("prs_merged", 0)) or 0)
    failed = int(stat.get("tasks_failed") or 0)
    cycles = int(stat.get("total_cycles") or 0)
    priority = stat.get("priority")
    priority_bonus = {"high": 5, "medium": 0, "low": -2}.get(priority, 0)
    proposed_bonus = 3 if stat.get("status") == "proposed" else 0
    return merged * 3 + failed + cycles + priority_bonus + proposed_bonus


def _summarize_directions(stats: list) -> list:
    """Keep top direction_summary entries and compact bulky per-entry evidence.

    Dropped directions are still discoverable by id via the agent's
    ``./fetch direction <id>`` helper, so the agent can pull the full
    record when it needs to reason about a quiet direction.
    """
    if not isinstance(stats, list):
        return stats
    sorted_stats = sorted(stats, key=_direction_activity_score, reverse=True)
    return [
        _compact_direction_summary_entry(stat)
        for stat in sorted_stats[:_DIRECTIONS_SUMMARY_KEEP]
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


def _compact_direction_summary_entry(stat: dict) -> dict:
    out: dict[str, Any] = {}
    for key in (
        "id",
        "priority",
        "status",
        "total_cycles",
        "prs_merged",
        "verified_prs_merged",
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
    if stat.get("research_context"):
        out["research_context_headline"] = _bounded_text(
            stat.get("research_context"), _DIRECTION_SUMMARY_LONG_TEXT_CAP
        )
    for key in (
        "taxonomy_summary",
        "recent_findings_grade_distribution",
        "recent_findings_verification_basis",
        "recent_findings_challenge_summary",
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
    return out

