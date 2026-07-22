"""SR regression-risk, false-attestation trends, and alert drilldown.

Extracted from ``sr_analytics.py``. ``sr_analytics`` re-exports all public
names from this module for backward compatibility.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Alert, AlertSeverity, StrategicCycle, _utcnow
from app.sr_blind_spots import _REVIEWER_ORIGINS, safe_check_results
from app.sr_catch_rate_trends import (
    _MIN_SR_SAMPLE,
    _SR_CODE_CHANGE_TYPES,
    _TREND_DELTA_THRESHOLD,
    _format_sr_alert_title,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SR regression_risk vs testFix-loop signal cross-validation
# ---------------------------------------------------------------------------


async def compute_sr_regression_risk_test_signal_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cross-validate SR ``regression_risk.passed`` verdicts against testFix-loop signal.

    Companion metric to ``compute_sr_catch_rate_trends`` — that one counts a
    miss only when a human reviewer raised the ``regression_risk`` category.
    This one uses the agent's own testFix loop as an objective signal: when the
    test loop ran non-trivially, real regression surface existed regardless of
    whether any human reviewer flagged it.

    Per-task classification (qualifying = code-change task with a non-skip
    self-review outcome that includes ``check_results.regression_risk.passed``)::

      sr_passed_no_signal   (true-pass)             — SR ok, test loop quiet
      sr_passed_with_signal (miss-by-test-signal)   — SR ok, test loop ran
      sr_failed_with_signal (hit-by-test-signal)    — SR flagged, test loop ran
      sr_failed_no_signal   (false-flag)            — SR flagged, test loop quiet

    Test-signal predicate: ``len(history.get("test") or []) >= 2`` OR the last
    test attempt's ``exit_reason`` is non-``passed`` (covers single-iteration
    runs that diverged or maxed out attempts).

    Mirrors :func:`compute_sr_catch_rate_trends` cycle-vs-calendar bucketing
    and the :data:`_MIN_SR_SAMPLE` floor.

    **Side effect**: writes the result to
    ``project.design_doc["sr_regression_risk_test_signal_trends"]``.
    """
    from app.sr_blind_spots import is_skipped_sr_outcome

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifies(task) -> bool:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            return False
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            return False
        rr = safe_check_results(sro).get("regression_risk")
        return isinstance(rr, dict) and "passed" in rr

    def _has_test_signal(task) -> bool:
        history = getattr(task, "fix_loop_history", None) or {}
        test_attempts = history.get("test") or []
        if len(test_attempts) >= 2:
            return True
        if test_attempts:
            last = test_attempts[-1]
            if isinstance(last, dict):
                reason = last.get("exit_reason")
                if reason and reason != "passed":
                    return True
        return False

    def _aggregate(tasks: list) -> dict[str, int]:
        stats = {
            "sr_passed_no_signal": 0,
            "sr_passed_with_signal": 0,
            "sr_failed_with_signal": 0,
            "sr_failed_no_signal": 0,
        }
        for t in tasks:
            if not _qualifies(t):
                continue
            sro = (t.context or {}).get("_self_review_outcome") or {}
            rr = safe_check_results(sro).get("regression_risk") or {}
            sr_passed = bool(rr.get("passed"))
            signal = _has_test_signal(t)
            if sr_passed and signal:
                stats["sr_passed_with_signal"] += 1
            elif sr_passed:
                stats["sr_passed_no_signal"] += 1
            elif signal:
                stats["sr_failed_with_signal"] += 1
            else:
                stats["sr_failed_no_signal"] += 1
        return stats

    def _qualifying_count(tasks: list) -> int:
        return sum(1 for t in tasks if _qualifies(t))

    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        # id-only projection (bucketing below uses only .id) + the
        # ix_strategiccycle_project_outcome_phase1 index: ordered index scan
        # satisfies LIMIT 4 directly (was a full scan + sort of every completed
        # cycle → 120s statement_timeout that poisoned the claim, 2026-05-29).
        select(StrategicCycle.id)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    current_qual = _qualifying_count(current_bucket)
    previous_qual = _qualifying_count(previous_bucket)
    if current_qual < _MIN_SR_SAMPLE or previous_qual < _MIN_SR_SAMPLE:
        return None

    current_counts = _aggregate(current_bucket)
    previous_counts = _aggregate(previous_bucket)

    def _miss_rate(stats: dict[str, int]) -> tuple[Optional[float], int, int]:
        numerator = stats["sr_passed_with_signal"]
        denominator = stats["sr_passed_no_signal"] + stats["sr_passed_with_signal"]
        if denominator == 0:
            return None, numerator, denominator
        return round(numerator / denominator, 3), numerator, denominator

    current_rate, current_numer, current_denom = _miss_rate(current_counts)
    previous_rate, previous_numer, previous_denom = _miss_rate(previous_counts)

    if current_rate is not None and previous_rate is not None:
        delta = round(current_rate - previous_rate, 3)
        if delta >= _TREND_DELTA_THRESHOLD:
            trend = "regressing"
        elif delta <= -_TREND_DELTA_THRESHOLD:
            trend = "improving"
        else:
            trend = "stable"
    else:
        delta = None
        trend = "stable"

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "current": {
            **current_counts,
            "miss_by_test_signal_rate": current_rate,
            "numerator": current_numer,
            "denominator": current_denom,
            "sample_size": current_qual,
        },
        "previous": {
            **previous_counts,
            "miss_by_test_signal_rate": previous_rate,
            "numerator": previous_numer,
            "denominator": previous_denom,
            "sample_size": previous_qual,
        },
        "miss_by_test_signal_rate_delta": delta,
        "trend": trend,
    }

    from app.task_locking import with_sr_analytics_locked  # noqa: PLC0415

    _result = result

    def _store(data: dict) -> None:
        data["sr_regression_risk_test_signal_trends"] = _result

    applied = await with_sr_analytics_locked(session, project_id, _store)
    if not applied:
        return None

    logger.info(
        "[SR_REGRESSION_RISK_TEST_SIGNAL] project=%s trend=%s miss_rate=%s n=%d",
        project_id,
        trend,
        current_rate,
        current_qual,
    )

    return result


# ---------------------------------------------------------------------------
# SR false-attestation trends
# ---------------------------------------------------------------------------

_FALSE_ATTESTATION_RATE_THRESHOLD = 0.10
_FALSE_ATTESTATION_ALERT_TITLE_PREFIX = "SR false attestation rate elevated"


async def compute_sr_false_attestation_trends(
    project_id: uuid.UUID,
    session: AsyncSession,
    all_tasks: list,
) -> Optional[dict[str, Any]]:
    """Cross-check SR execution-evidence pass claims against CI outcomes cycle-over-cycle.

    For each code-change task whose SR marks tests_executed_cleanly, lint_executed_cleanly,
    or typecheck_executed_cleanly as passed=True, checks context["review_loop"]["ci_passed"].
    Counts tasks where SR claimed passed but CI recorded ci_passed=False as false attestations.

    Mirrors :func:`compute_sr_regression_risk_test_signal_trends` cycle-vs-calendar bucketing.

    **Side effect**: writes the result to
    ``project.design_doc["sr_false_attestation_trends"]`` and emits a WARNING Alert when
    any category's false attestation rate exceeds _FALSE_ATTESTATION_RATE_THRESHOLD.
    """
    from app.sr_blind_spots import (
        aggregate_sr_false_attestation_rates,
        is_skipped_sr_outcome,
    )

    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _qualifies(task) -> bool:
        if (task.first_payload or {}).get("type") not in _SR_CODE_CHANGE_TYPES:
            return False
        ctx = task.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            return False
        rl = ctx.get("review_loop")
        return rl is not None and "ci_passed" in rl

    def _qualifying_count(tasks: list) -> int:
        return sum(1 for t in tasks if _qualifies(t))

    use_cycle_mode = False
    current_cycle_id: Optional[str] = None
    previous_cycle_id: Optional[str] = None
    current_bucket: list = []
    previous_bucket: list = []

    cycle_stmt = (
        # id-only projection (bucketing below uses only .id) + the
        # ix_strategiccycle_project_outcome_phase1 index: ordered index scan
        # satisfies LIMIT 4 directly (was a full scan + sort of every completed
        # cycle → 120s statement_timeout that poisoned the claim, 2026-05-29).
        select(StrategicCycle.id)
        .where(StrategicCycle.project_id == project_id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await session.execute(cycle_stmt)).all()

    if len(cycle_rows) >= 2:
        task_by_cycle: dict[str, list] = {}
        for t in all_tasks:
            cid = getattr(t, "strategic_cycle_id", None)
            if cid:
                task_by_cycle.setdefault(cid, []).append(t)
        for i in range(len(cycle_rows) - 1):
            c_cycle = cycle_rows[i]
            p_cycle = cycle_rows[i + 1]
            c_tasks = task_by_cycle.get(c_cycle.id, [])
            p_tasks = task_by_cycle.get(p_cycle.id, [])
            if (
                _qualifying_count(c_tasks) >= _MIN_SR_SAMPLE
                and _qualifying_count(p_tasks) >= _MIN_SR_SAMPLE
            ):
                use_cycle_mode = True
                current_cycle_id = c_cycle.id
                previous_cycle_id = p_cycle.id
                current_bucket = c_tasks
                previous_bucket = p_tasks
                break

    if not use_cycle_mode:
        cutoff_14d_n = _naive(now - timedelta(days=14))
        cutoff_28d_n = _naive(now - timedelta(days=28))
        sentinel_n = datetime(9999, 12, 31)
        current_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_14d_n <= _naive(t.created_at) < sentinel_n
        ]
        previous_bucket = [
            t
            for t in all_tasks
            if t.created_at and cutoff_28d_n <= _naive(t.created_at) < cutoff_14d_n
        ]

    if _qualifying_count(current_bucket) < _MIN_SR_SAMPLE:
        return None

    current_rates = aggregate_sr_false_attestation_rates(current_bucket, min_sample=1)
    previous_rates = aggregate_sr_false_attestation_rates(previous_bucket, min_sample=1)

    if not current_rates:
        return None

    per_category: dict[str, Any] = {}
    for cat, curr in current_rates.items():
        prev = (previous_rates or {}).get(cat) or {}
        curr_rate = curr.get("false_attestation_rate")
        prev_rate = prev.get("false_attestation_rate")
        if curr_rate is not None and prev_rate is not None:
            delta = round(curr_rate - prev_rate, 3)
            trend = (
                "regressing"
                if delta >= _TREND_DELTA_THRESHOLD
                else "improving"
                if delta <= -_TREND_DELTA_THRESHOLD
                else "stable"
            )
        else:
            delta = None
            trend = "stable"
        per_category[cat] = {
            **curr,
            "previous_false_attestation_rate": prev_rate,
            "delta": delta,
            "trend": trend,
        }

    result: dict[str, Any] = {
        "computed_at": now.isoformat(),
        "bucket_mode": "cycle" if use_cycle_mode else "calendar",
        "current_cycle_id": current_cycle_id,
        "previous_cycle_id": previous_cycle_id,
        "per_category": per_category,
    }

    from app.task_locking import with_sr_analytics_locked  # noqa: PLC0415

    _result = result

    def _store_false_attest(data: dict) -> None:
        data["sr_false_attestation_trends"] = _result

    applied = await with_sr_analytics_locked(session, project_id, _store_false_attest)
    if not applied:
        return None

    logger.info(
        "[SR_FALSE_ATTESTATION] project=%s bucket=%s cats=%s",
        project_id,
        "cycle" if use_cycle_mode else "calendar",
        list(per_category.keys()),
    )

    elevated = {
        cat: data
        for cat, data in per_category.items()
        if (data.get("false_attestation_rate") or 0) > _FALSE_ATTESTATION_RATE_THRESHOLD
    }
    if elevated:
        try:
            from app.notification_dispatch import post_failure_alert_message  # noqa: PLC0415

            lines = [
                "SR false attestation rate elevated (SR claimed passed but CI failed):"
            ]
            for cat, data in sorted(elevated.items()):
                rate = data.get("false_attestation_rate")
                n = data.get("false_attested", 0)
                total = data.get("ci_confirmed", 0) + n
                lines.append(f"  • {cat}: {rate:.0%} ({n}/{total})")
            description = "\n".join(lines)
            await post_failure_alert_message(session, project_id, description)

            new_title = _format_sr_alert_title(
                _FALSE_ATTESTATION_ALERT_TITLE_PREFIX, sorted(elevated.keys())
            )
            existing_alerts = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == project_id,
                            Alert.task_id.is_(None),
                            Alert.severity == AlertSeverity.WARNING,
                            Alert.dismissed_at.is_(None),
                            Alert.title.like(
                                f"{_FALSE_ATTESTATION_ALERT_TITLE_PREFIX}%"
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not any(a.title == new_title for a in existing_alerts):
                for a in existing_alerts:
                    a.dismissed_at = now
                    session.add(a)
                session.add(
                    Alert(
                        project_id=project_id,
                        severity=AlertSeverity.WARNING,
                        title=new_title,
                        message=description,
                        event_type="sr.catch_rate_below_target",
                    )
                )
        except Exception:
            logger.exception(
                "[SR_FALSE_ATTESTATION] alert emission failed (non-critical)"
            )

    return result


# ---------------------------------------------------------------------------
# SR alert drilldown helper
# ---------------------------------------------------------------------------


def get_sr_alert_drilldown_tasks(
    all_tasks: list,
    design_doc: dict,
    categories: list[str],
    *,
    alert_type: str = "trends",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """Return tasks from the current SR alert window where SR missed the given categories.

    Finds tasks where the human reviewer flagged at least one category from
    ``categories`` but SR did not (category absent from ``check_results`` or
    ``passed`` is True).  Window is derived from the stored alert state:
    - cycle_id present -> filter by ``strategic_cycle_id``
    - no cycle_id -> last 14 calendar days (calendar fallback)

    PR URLs are not fetched here; the calling endpoint should batch-query
    Artifact rows by the returned task_id list.
    """
    now = _utcnow()

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    alert_key = (
        "sr_catch_rate_trends_alert"
        if alert_type == "trends"
        else "sr_catch_rate_floor_alert"
    )
    alert_state: dict = dict(design_doc.get(alert_key) or {})
    cycle_id: Optional[str] = alert_state.get("cycle_id")
    target_cats = set(categories)

    if cycle_id:
        window_tasks = [
            t for t in all_tasks if getattr(t, "strategic_cycle_id", None) == cycle_id
        ]
    else:
        cutoff = _naive(now - timedelta(days=14))
        window_tasks = [
            t for t in all_tasks if t.created_at and _naive(t.created_at) >= cutoff
        ]

    results: list[dict[str, Any]] = []
    for t in window_tasks:
        ctx = t.context or {}
        if "_self_review_outcome" not in ctx:
            continue
        check_results = safe_check_results(ctx["_self_review_outcome"])
        # Author SR failures only — a reviewer-rescued category should still
        # surface in the drilldown as `missed_in_target` because the SR itself
        # did not catch it.
        sr_failed = {
            c
            for c, r in check_results.items()
            if isinstance(r, dict)
            and not r.get("passed", True)
            and r.get("origin") not in _REVIEWER_ORIGINS
        }
        human_cats = set(ctx.get("_first_attempt_rejection_categories") or [])
        missed_in_target = (human_cats & target_cats) - sr_failed
        if not missed_in_target:
            continue
        caught_in_target = human_cats & target_cats & sr_failed
        payload = t.first_payload or {}
        findings_list = ctx["_self_review_outcome"].get("reviewer_findings") or []
        findings_by_cat = {
            f["category"]: f
            for f in findings_list
            if isinstance(f, dict) and "category" in f
        }
        missed_category_findings: dict[str, Any] = {}
        for cat in missed_in_target:
            finding = findings_by_cat.get(cat)
            if finding:
                desc = finding.get("description") or ""
                missed_category_findings[cat] = {
                    "severity": finding.get("severity"),
                    "description": desc[:140],
                    "has_more": len(desc) > 140,
                    "full_description": desc if len(desc) > 140 else None,
                }
            else:
                missed_category_findings[cat] = {
                    "severity": None,
                    "description": "no description recorded",
                    "has_more": False,
                    "full_description": None,
                }
        results.append(
            {
                "task_id": str(t.id),
                "title": t.title or "",
                "task_type": payload.get("type", ""),
                "scope": ctx.get("scope"),
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "missed_categories": sorted(missed_in_target),
                "caught_categories": sorted(caught_in_target),
                "sr_verdict": ctx["_self_review_outcome"].get("verdict"),
                "missed_category_findings": missed_category_findings,
            }
        )

    results.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return results[:page_size]
