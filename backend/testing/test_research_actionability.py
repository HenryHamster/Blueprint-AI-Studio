"""Pure-math unit tests for research_actionability aggregation helpers.

Covers the extended (forward-quality) aggregation that counts a finding's
follow-on as satisfied when it achieved ANY terminal evaluation success — a
merged PR OR a successful non-code outcome (audit/memo/research) — alongside
the preserved merge-only sub-metrics.

These exercise the helpers directly (no DB, no HTTP) so the math is asserted
in isolation.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.models import Task, TaskStatus
from app.research_actionability import (
    _aggregate_actionability_extended,
    _compute_actionability_rates_extended,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _research_task(findings: list[dict], created_at: datetime) -> Task:
    """A research parent carrying graded findings on plan_output."""
    return Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        title="research",
        payloads=[{"type": "research"}],
        context={"research_findings_finalized": True},
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        plan_output=findings,
        created_at=created_at,
    )


def _followon(
    task_type: str,
    direction_id: str,
    created_at: datetime,
    *,
    context: dict | None = None,
    plan_output=None,
    status: TaskStatus = TaskStatus.COMPLETED,
) -> Task:
    ctx = {"direction_id": direction_id}
    if context:
        ctx.update(context)
    return Task(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        title=f"{task_type} follow-on",
        payloads=[{"type": task_type}],
        context=ctx,
        status=status,
        created_by="ORCHESTRATOR",
        plan_output=plan_output,
        created_at=created_at,
    )


def test_extended_counts_non_code_success_alongside_merge():
    """A HIGH finding whose follow-on is a successful audit (no PR) is counted
    in grade_succeeded but NOT in grade_merged."""
    t0 = _utcnow() - timedelta(hours=2)
    research = _research_task(
        [
            {
                "title": "Audit the X subsystem",
                "actionability_grade": "HIGH",
                "direction_id": "dir-audit",
            },
            {
                "title": "Ship the Y fix",
                "actionability_grade": "HIGH",
                "direction_id": "dir-merge",
            },
        ],
        created_at=t0,
    )
    # Non-code follow-on: a security_audit that produced structured findings.
    audit = _followon(
        "security_audit",
        "dir-audit",
        created_at=t0 + timedelta(minutes=30),
        plan_output={"findings": [{"severity": "high"}]},
    )
    # Code follow-on: a merged create task.
    merged = _followon(
        "create",
        "dir-merge",
        created_at=t0 + timedelta(minutes=30),
        context={"pr_merged": True},
    )

    counts, tasked, merged_c, succeeded = _aggregate_actionability_extended(
        [research], [audit, merged], {research.id}
    )

    assert counts["HIGH"] == 2
    assert tasked["HIGH"] == 2  # both directions got a follow-on
    # Merge sub-metric: only the code follow-on merged.
    assert merged_c["HIGH"] == 1
    # Generalized: both the merge AND the successful audit count.
    assert succeeded["HIGH"] == 2


def test_extended_rates_generalized_calibration():
    """grade_success_rate and generalized_calibration_score derive from
    grade_succeeded; merge rate/calibration stay as the code sub-metric."""
    counts = {"HIGH": 4, "MEDIUM": 2, "LOW": 4}
    tasked = {"HIGH": 4, "MEDIUM": 2, "LOW": 4}
    merged = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}
    succeeded = {"HIGH": 4, "MEDIUM": 1, "LOW": 1}

    rates = _compute_actionability_rates_extended(counts, tasked, merged, succeeded)

    # Code sub-metric preserved (merge-based).
    assert rates["grade_merge_rate"]["HIGH"] == 0.5
    assert rates["grade_merge_rate"]["LOW"] == 0.0
    assert rates["calibration_score"] == 0.5  # 0.5 - 0.0
    # Generalized forward-quality signal.
    assert rates["grade_success_rate"]["HIGH"] == 1.0
    assert rates["grade_success_rate"]["LOW"] == 0.25
    assert rates["generalized_calibration_score"] == 0.75  # 1.0 - 0.25


def test_extended_rates_none_when_no_findings():
    """Rates are None (not 0.0) when a grade has zero findings, for both the
    merge sub-metric and the generalized success rate."""
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    rates = _compute_actionability_rates_extended(counts, counts, counts, counts)
    assert rates["grade_merge_rate"]["HIGH"] is None
    assert rates["grade_success_rate"]["HIGH"] is None
    assert rates["calibration_score"] is None
    assert rates["generalized_calibration_score"] is None


def test_unfinalized_research_findings_do_not_enter_actionability_truth():
    research = _research_task(
        [
            {
                "title": "Rejected raw finding",
                "actionability_grade": "HIGH",
                "direction_id": "dir-raw",
            }
        ],
        created_at=_utcnow() - timedelta(hours=1),
    )
    research.context = {}

    counts, tasked, merged, succeeded = _aggregate_actionability_extended(
        [research], [], {research.id}
    )

    assert counts == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    assert tasked == merged == succeeded == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}


def test_extended_code_only_matches_merge_metrics():
    """A code-only portfolio: grade_succeeded equals grade_merged (a merge is a
    generalized success), so the generalized signal does not diverge."""
    t0 = _utcnow() - timedelta(hours=1)
    research = _research_task(
        [
            {
                "title": "Fix A",
                "actionability_grade": "HIGH",
                "direction_id": "dir-a",
            },
            {
                "title": "Fix B",
                "actionability_grade": "LOW",
                "direction_id": "dir-b",
            },
        ],
        created_at=t0,
    )
    merged_a = _followon(
        "create",
        "dir-a",
        created_at=t0 + timedelta(minutes=10),
        context={"pr_merged": True},
    )
    # dir-b follow-on tasked but not merged (no landed signal).
    tasked_b = _followon(
        "fix",
        "dir-b",
        created_at=t0 + timedelta(minutes=10),
    )

    counts, tasked, merged, succeeded = _aggregate_actionability_extended(
        [research], [merged_a, tasked_b], {research.id}
    )
    # For code follow-ons, succeeded == merged (merge is the only success path).
    assert succeeded == merged
    assert merged["HIGH"] == 1
    assert succeeded["HIGH"] == 1
    assert merged["LOW"] == 0
    assert succeeded["LOW"] == 0
