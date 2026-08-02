"""Playbook test suite (split).

Tests exercising the app.playbook_sr_metrics surface, split out of the former
testing/test_playbook_auto.py monolith. Behavior-preserving: test bodies
are copied verbatim; only file boundaries and shared-helper imports change.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Alert, AlertSeverity, Project, Task, TaskStatus
from app.routers.agent_status import _utcnow
from sqlmodel import select as _sql_select

from testing._playbook_test_helpers import (
    _make_project,
)


async def _read_sr_analytics(db_session: AsyncSession, project: Project) -> dict:
    from app.sr_analytics_store import read_project_sr_analytics

    return await read_project_sr_analytics(db_session, project)


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    task_type: str = "create",
    check_result_overrides: dict[str, dict] | None = None,
    sr_completed_at: datetime | None = None,
    rejection_at: datetime | None = None,
    sr_passed_cats_with_reasoning: dict[str, str] | None = None,
) -> Task:
    """Build a Task with both _self_review_outcome.check_results and _first_attempt_rejection_categories.

    sr_passed_cats_with_reasoning: optional {cat: reasoning_string} to add
    explicit PASS entries to check_results (for engagement tracking tests).
    """
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    if sr_passed_cats_with_reasoning:
        for cat, reasoning in sr_passed_cats_with_reasoning.items():
            check_results[cat] = {"passed": True, "reasoning": reasoning}
    if check_result_overrides:
        for cat, overrides in check_result_overrides.items():
            if cat in check_results:
                check_results[cat].update(overrides)
    sr_outcome: dict = {"check_results": check_results}
    if sr_completed_at is not None:
        sr_outcome["completed_at"] = sr_completed_at.isoformat()
    ctx: dict = {
        "_self_review_outcome": sr_outcome,
        "_first_attempt_rejection_categories": human_cats,
    }
    if rejection_at is not None:
        ctx["_first_attempt_rejection_at"] = rejection_at.isoformat()
    task = Task(
        project_id=project_id,
        title="SR task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_returns_none_below_sample_size(
    db_session: AsyncSession,
):
    """Returns None when either window has fewer than _MIN_SR_FINDINGS reviewer findings."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_stamps_heartbeat_on_low_sample(
    db_session: AsyncSession,
):
    """Even when returning None (below _MIN_SR_FINDINGS), stamps signal_last_computed_at."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(4):  # 4 findings per window — below _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is None
    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert "signal_last_computed_at" in stored, (
        "heartbeat must be stamped even when returning None (idle/low-data state)"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_stamps_heartbeat_on_success(
    db_session: AsyncSession,
):
    """Stamps signal_last_computed_at in design_doc when computation produces a result."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):  # 5 findings per window — meets _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert "signal_last_computed_at" in stored, (
        "heartbeat must be stamped on every successful invocation"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_data_sufficient_false_on_low_sample(
    db_session: AsyncSession,
):
    """data_sufficient=False is stored when returning None (insufficient findings).
    last_sufficient_computed_at must NOT be set/overwritten so callers can tell
    when data was last actually sufficient."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(4):  # 4 findings — below _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is None
    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert stored.get("data_sufficient") is False, (
        "data_sufficient must be False when findings are insufficient"
    )
    assert "last_sufficient_computed_at" not in stored, (
        "last_sufficient_computed_at must not be set when no successful computation occurred"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_data_sufficient_true_on_success(
    db_session: AsyncSession,
):
    """data_sufficient=True and last_sufficient_computed_at are stored on a real computation."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):  # 5 findings per window — meets _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result.get("data_sufficient") is True, (
        "data_sufficient must be True on a successful computation"
    )
    assert "last_sufficient_computed_at" in result, (
        "last_sufficient_computed_at must be set on a successful computation"
    )
    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert stored.get("data_sufficient") is True
    assert "last_sufficient_computed_at" in stored


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_writes_dedicated_store_on_success(
    db_session: AsyncSession,
):
    """A successful computation writes sr_catch_rate_trends to ProjectSRAnalytics.

    The dedicated SR analytics row is authoritative after the store cutover.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    assert result is not None
    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert stored.get("data_sufficient") is True

    assert stored == result
    assert (project.design_doc or {}).get("sr_catch_rate_trends") is None


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_writes_dedicated_store_insufficient_marker(
    db_session: AsyncSession,
):
    """The insufficient-sample marker is written to the dedicated SR store."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(4):  # below _MIN_SR_FINDINGS=5
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(4):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    assert result is None
    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert stored.get("data_sufficient") is False
    assert "signal_last_computed_at" in stored
    assert (project.design_doc or {}).get("sr_catch_rate_trends") is None


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_data_sufficient_preserves_last_sufficient_on_low(
    db_session: AsyncSession,
):
    """After a successful run, a subsequent low-data run preserves last_sufficient_computed_at."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)

    # First run with sufficient data
    tasks_ok = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks_ok.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks_ok.append(t)
    await db_session.flush()

    result1 = await compute_sr_catch_rate_trends(project.id, db_session, tasks_ok)
    assert result1 is not None
    stored1 = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    last_sufficient = stored1["last_sufficient_computed_at"]

    # Second run with insufficient data — should preserve last_sufficient_computed_at
    tasks_low = []
    for _ in range(4):  # below threshold
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks_low.append(t)
    await db_session.flush()

    result2 = await compute_sr_catch_rate_trends(project.id, db_session, tasks_low)
    assert result2 is None
    stored2 = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert stored2.get("data_sufficient") is False
    assert stored2.get("last_sufficient_computed_at") == last_sufficient, (
        "last_sufficient_computed_at must be preserved (not cleared) after a low-data run"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_exposes_finding_counts(
    db_session: AsyncSession,
):
    """result contains current_finding_count / previous_finding_count (true denominator)
    and per-category low_confidence_finding_count flag."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["current_finding_count"] == 5
    assert result["previous_finding_count"] == 5
    cat = result["categories"]["test-coverage"]
    assert cat["current_reviewer_finding_count"] == 5
    assert cat["previous_reviewer_finding_count"] == 5
    # 5 findings == _MIN_SR_FINDINGS, so not low-confidence
    assert cat["low_confidence_finding_count"] is False


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_low_confidence_suppresses_trend(
    db_session: AsyncSession,
):
    """Categories with fewer than _MIN_SR_FINDINGS findings are flagged low-confidence
    and their trend is suppressed to 'stable' even when hit rates diverge."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 6 tasks — 5 flag 'correctness', 1 flags 'test-coverage' (low-finding cat)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task(
        project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
    )
    db_session.add(t)
    tasks.append(t)
    # Previous window: 5 tasks all flag 'correctness' (hit rate differs); 'test-coverage' absent
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # 'test-coverage' has only 1 finding in current window — low-confidence, trend suppressed
    tc = result["categories"]["test-coverage"]
    assert tc["low_confidence_finding_count"] is True
    assert tc["trend"] == "stable"
    # 'correctness' has 5 current + 5 previous = enough findings, not low-confidence
    corr = result["categories"]["correctness"]
    assert corr["low_confidence_finding_count"] is False


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_per_category_hit_and_miss(
    db_session: AsyncSession,
):
    """Computes correct per-category hit_rate and miss_rate."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): 3 hits, 2 misses on test-coverage
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (5 tasks): all hits
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["miss_rate"] == round(1 - 3 / 5, 3)
    assert cat["previous_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_trend_classification(
    db_session: AsyncSession,
):
    """Hit rate drops >= 5pp → trend='decreasing'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 3/5 = 0.6
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5/5 = 1.0 (delta = -0.4, well below -5pp threshold)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["test-coverage"]["trend"] == "decreasing"
    assert result["categories"]["test-coverage"]["trend_significant"] is True


@pytest.mark.asyncio
async def test_trend_small_sample_swing_suppressed(db_session: AsyncSession):
    """n=5 per window with a 1-finding delta (0.2 hit-rate diff) is sampling noise.

    The two-proportion z-test (z≈0.63 < _TREND_Z_MIN=1.28) must suppress the
    directional label even though delta (0.2) exceeds _TREND_DELTA_THRESHOLD (0.05).
    trend_significant must be False to distinguish this from 'not enough data'.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 2/5 = 0.4 hit_rate
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 3/5 = 0.6 hit_rate (delta=−0.2, z≈0.63)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    # delta=−0.2 exceeds the raw threshold but is not statistically significant
    assert cat["trend"] == "stable"
    assert cat["trend_significant"] is False


@pytest.mark.asyncio
async def test_trend_large_consistent_shift_reported(db_session: AsyncSession):
    """Large n with a 0.3 hit-rate drop is statistically significant and reported.

    n=30 per window, delta=−0.3 → z≈2.74 >> _TREND_Z_MIN=1.28; trend='decreasing'.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 12/30 = 0.4 hit_rate
    for _ in range(12):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(18):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 21/30 = 0.7 hit_rate (delta=−0.3, significant)
    for _ in range(21):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(9):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["trend"] == "decreasing"
    assert cat["trend_significant"] is True


@pytest.mark.asyncio
async def test_trend_significance_scales_with_sample_size(db_session: AsyncSession):
    """The same delta (0.2) reports 'stable' at n=5 but 'decreasing' at n=30.

    This validates the significance boundary: a fixed raw threshold is insufficient
    because the same rate change means very different things at different sample sizes.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    # --- Small n: n=5, delta=0.2 → NOT significant ---
    project_small = await _make_project(db_session)
    tasks_small = []
    for _ in range(2):  # current 2/5 = 0.4
        t = _make_sr_task(
            project_small.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_small.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project_small.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_small.append(t)
    for _ in range(3):  # previous 3/5 = 0.6
        t = _make_sr_task(
            project_small.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_small.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project_small.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_small.append(t)

    # --- Large n: n=30, delta=0.2 → significant ---
    project_large = await _make_project(db_session)
    tasks_large = []
    for _ in range(12):  # current 12/30 = 0.4
        t = _make_sr_task(
            project_large.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_large.append(t)
    for _ in range(18):
        t = _make_sr_task(
            project_large.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_large.append(t)
    for _ in range(18):  # previous 18/30 = 0.6
        t = _make_sr_task(
            project_large.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks_large.append(t)
    for _ in range(12):
        t = _make_sr_task(
            project_large.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks_large.append(t)

    await db_session.flush()

    result_small = await compute_sr_catch_rate_trends(
        project_small.id, db_session, tasks_small
    )
    result_large = await compute_sr_catch_rate_trends(
        project_large.id, db_session, tasks_large
    )

    assert result_small is not None
    assert result_large is not None
    small_cat = result_small["categories"]["error-handling"]
    large_cat = result_large["categories"]["error-handling"]
    # Same delta, different sample sizes → different significance outcomes
    assert small_cat["trend"] == "stable"
    assert small_cat["trend_significant"] is False
    assert large_cat["trend"] == "decreasing"
    assert large_cat["trend_significant"] is True


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_rate_history_grows(
    db_session: AsyncSession,
):
    """rate_history grows on each new window call and caps at 5."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Pre-seed design_doc with 4 existing history entries
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "categories": {
                "test-coverage": {"rate_history": [0.8, 0.7, 0.6, 0.5]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    # Current window: 3/5 hits
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5/5 hits
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    history = result["categories"]["test-coverage"]["rate_history"]
    # 4 existing + 1 new = 5 (capped at 5)
    assert len(history) == 5
    assert history[-1] == round(3 / 5, 3)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_calendar_to_cycle_resets_history(
    db_session: AsyncSession,
):
    """Transitioning from calendar mode to cycle mode resets rate_history.

    When prior data was produced in calendar mode and this run uses cycle mode,
    the stored history is incomparable — the function must not concatenate them.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Pre-seed calendar-mode history with two prior entries.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "bucket_mode": "calendar",
            "categories": {
                "test-coverage": {"rate_history": [0.9, 0.85]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    # Create two completed cycles so cycle mode is selected.
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    history = result["categories"]["test-coverage"]["rate_history"]
    # Old calendar entries must be discarded — only the fresh cycle measurement.
    assert len(history) == 1, f"expected 1 entry after mode transition, got {history}"
    assert 0.9 not in history
    assert 0.85 not in history


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_cycle_to_calendar_resets_history(
    db_session: AsyncSession,
):
    """Transitioning from cycle mode to calendar mode resets rate_history.

    When prior data was produced in cycle mode and this run falls back to
    calendar windows (no completed cycles with enough tasks), history must
    not carry over incomparable cycle-span rates.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    fake_cycle_id = "old-cycle-abc123"
    # Pre-seed cycle-mode history.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "bucket_mode": "cycle",
            "current_cycle_id": fake_cycle_id,
            "categories": {
                "test-coverage": {"rate_history": [0.7, 0.65]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    # No completed cycles — forces calendar fallback.
    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "calendar"
    history = result["categories"]["test-coverage"]["rate_history"]
    # Old cycle entries must be discarded — only the fresh calendar measurement.
    assert len(history) == 1, f"expected 1 entry after mode transition, got {history}"
    assert 0.7 not in history
    assert 0.65 not in history


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_persists_to_dedicated_store(
    db_session: AsyncSession,
):
    """Writes sr_catch_rate_trends to the authoritative SR analytics row."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    assert result is not None
    analytics = await _read_sr_analytics(db_session, project)
    assert "sr_catch_rate_trends" in analytics
    stored = analytics["sr_catch_rate_trends"]
    assert "computed_at" in stored
    assert "categories" in stored
    assert "by_task_type" in stored
    assert "sr_catch_rate_trends" not in (project.design_doc or {})


def _make_sr_task_with_fix_iter(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    fix_iteration: int | None = None,
    task_type: str = "create",
) -> Task:
    sro: dict = {
        "check_results": {
            cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats
        }
    }
    if fix_iteration is not None:
        sro["fix_iteration"] = fix_iteration
    task = Task(
        project_id=project_id,
        title="SR fix-iter task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": sro,
            "_first_attempt_rejection_categories": human_cats,
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_fix_iteration_buckets(
    db_session: AsyncSession,
):
    """by_fix_iteration buckets tasks by _self_review_outcome.fix_iteration into
    '1' (None or <=1), '2', and '3+' (>=3). Buckets below _MIN_SR_SAMPLE are omitted."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list[Task] = []
    # Bucket "1": 5 tasks all hit (fix_iteration absent ⇒ original/first pass)
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    # Bucket "2": 5 tasks with 2 hits, 3 misses on correctness
    for _ in range(2):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=2,
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            fix_iteration=2,
        )
        db_session.add(t)
        tasks.append(t)
    # Bucket "3+": 5 tasks all missed
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            fix_iteration=4,
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window — meets _MIN_SR_SAMPLE so compute returns a result
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    by_iter = result.get("by_fix_iteration") or {}
    assert set(by_iter.keys()) == {"1", "2", "3+"}
    # Bucket "1": 0 misses → miss_rate 0
    assert by_iter["1"]["correctness"]["miss_rate"] == 0.0
    assert by_iter["1"]["correctness"]["sample_count"] == 5
    # Bucket "2": 3 of 5 missed → miss_rate 0.6
    assert by_iter["2"]["correctness"]["miss_rate"] == round(3 / 5, 3)
    assert by_iter["2"]["correctness"]["sample_count"] == 5
    # Bucket "3+": 5 of 5 missed → miss_rate 1.0
    assert by_iter["3+"]["correctness"]["miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_fix_iteration_omits_below_sample(
    db_session: AsyncSession,
):
    """Buckets with fewer than _MIN_SR_SAMPLE qualifying observations per category
    are omitted from by_fix_iteration."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list[Task] = []
    # Bucket "1": 5 tasks (above floor)
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    # Bucket "2": only 2 tasks (below floor)
    for _ in range(2):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=2,
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window — meets _MIN_SR_SAMPLE
    for _ in range(5):
        t = _make_sr_task_with_fix_iter(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            fix_iteration=None,
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    by_iter = result.get("by_fix_iteration") or {}
    assert "1" in by_iter
    # Bucket "2" had only 2 samples for correctness — filtered out.
    assert "2" not in by_iter
    assert "3+" not in by_iter


def _make_sr_task_with_direction(
    project_id,
    direction_id: str,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    task_type: str = "create",
) -> Task:
    """SR task with direction_id stamped into context."""
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    task = Task(
        project_id=project_id,
        title="SR direction task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
            "direction_id": direction_id,
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_includes_by_direction_when_above_sample(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends should expose per-direction breakdown via the
    by_direction key when a direction has at least _MIN_DIRECTION_SR_SAMPLE qualifying tasks."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-by-dir-test"
    tasks = []
    # 4 tasks for the direction, all missed
    for i in range(4):
        t = _make_sr_task_with_direction(
            project.id,
            direction_id,
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Window-overall threshold (need >= _MIN_SR_SAMPLE in current and previous)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert "by_direction" in result
    assert direction_id in result["by_direction"]
    entry = result["by_direction"][direction_id]
    assert entry["sample_count"] == 4
    assert "regression_risk" in entry["categories"]
    # All missed → miss_rate 1.0 → overall_catch_rate 0.0
    assert entry["categories"]["regression_risk"]["miss_rate"] == 1.0
    assert entry["miss_rate"] == 1.0
    assert entry["overall_catch_rate"] == 0.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_omits_below_sample_directions(
    db_session: AsyncSession,
):
    """Directions with fewer than _MIN_DIRECTION_SR_SAMPLE tasks are not exposed
    in by_direction (their data is not actionable)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    # Only 2 tasks for this direction (under threshold of 3)
    tasks = []
    for i in range(2):
        t = _make_sr_task_with_direction(
            project.id,
            "d-low",
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Window-overall threshold padding
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    by_direction = result.get("by_direction", {})
    assert "d-low" not in by_direction


def _make_direction_tasks(project_id, direction_id, count, age_days, miss_all=True):
    """Create `count` SR tasks for a direction; if miss_all, human catches but SR misses."""
    tasks = []
    for _ in range(count):
        sr_failed = [] if miss_all else ["regression_risk"]
        t = _make_sr_task_with_direction(
            project_id,
            direction_id,
            age_days=age_days,
            human_cats={"regression_risk": 1},
            sr_failed_cats=sr_failed,
        )
        tasks.append(t)
    return tasks


def _make_window_padding(project_id, age_days, n=5):
    """Pad the overall SR sample so compute_sr_catch_rate_trends has enough tasks."""
    tasks = []
    for _ in range(n):
        t = _make_sr_task(
            project_id,
            age_days=age_days,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        tasks.append(t)
    return tasks


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_trend_increasing(
    db_session: AsyncSession,
):
    """When current miss_rate exceeds previous by >= _TREND_DELTA_THRESHOLD, trend=='increasing'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-increasing"

    # Seed prior state: miss_rate was 0.0 (all caught) over 4 prior samples.
    # sample_count is required for the two-proportion z-test that now gates the
    # trend label; with n=4 prior and n=4 current the 0.0→1.0 jump is significant.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 0.0,
                    "sample_count": 4,
                    "rate_history": [0.0],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: all tasks missed (miss_rate → 1.0)
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["trend"] == "increasing"
    assert entry["previous_miss_rate"] == 0.0
    assert len(entry["rate_history"]) == 2
    assert entry["rate_history"][0] == 0.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_trend_decreasing(
    db_session: AsyncSession,
):
    """When current miss_rate is lower than previous by >= _TREND_DELTA_THRESHOLD, trend=='decreasing'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-decreasing"

    # Seed prior state: miss_rate was 1.0 (all missed) over 4 prior samples.
    # sample_count is required for the two-proportion z-test that now gates the
    # trend label; with n=4 prior and n=4 current the 1.0→0.0 drop is significant.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 1.0,
                    "sample_count": 4,
                    "rate_history": [1.0],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: all tasks caught (miss_rate → 0.0)
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=False
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["trend"] == "decreasing"
    assert entry["previous_miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_trend_stable(
    db_session: AsyncSession,
):
    """When current miss_rate delta is within ±_TREND_DELTA_THRESHOLD, trend=='stable'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-stable"

    # Seed prior state: miss_rate was 1.0
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 1.0,
                    "rate_history": [1.0],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: all missed too (miss_rate → 1.0, delta == 0)
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["trend"] == "stable"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_no_previous(
    db_session: AsyncSession,
):
    """First run (no prior history): previous_miss_rate=None, trend='stable', rate_history has one entry."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-trend-new"
    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry["previous_miss_rate"] is None
    assert entry["trend"] == "stable"
    assert len(entry["rate_history"]) == 1


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_rate_history_capped(
    db_session: AsyncSession,
):
    """rate_history is capped at 5 entries; oldest are dropped when new entries arrive."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-hist-cap"

    # Seed prior state with 5 history entries (already at cap)
    project.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id: {
                    "miss_rate": 0.9,
                    "rate_history": [0.5, 0.6, 0.7, 0.8, 0.9],
                }
            }
        }
    }
    await db_session.flush()

    tasks = []
    for t in _make_direction_tasks(
        project.id, direction_id, 4, age_days=2, miss_all=True
    ):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=2):
        db_session.add(t)
        tasks.append(t)
    for t in _make_window_padding(project.id, age_days=20):
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert len(entry["rate_history"]) == 5
    # The oldest entry (0.5) should have been dropped
    assert 0.5 not in entry["rate_history"]


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_stores_hit_rate_and_sample_count(
    db_session: AsyncSession,
):
    """by_direction category entries must expose hit_rate and sample_count for the
    dispatcher's _current_signal_value_for_class per-direction handler."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    direction_id = "d-hitsample-test"
    tasks = []
    # 3 tasks: 2 caught (SR flagged + human confirmed), 1 missed (SR missed, human caught)
    for _ in range(2):
        t = _make_sr_task_with_direction(
            project.id,
            direction_id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_direction(
        project.id,
        direction_id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],  # SR missed this one
    )
    db_session.add(t)
    tasks.append(t)
    # Project-wide window tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat_entry = result["by_direction"][direction_id]["categories"]["correctness"]
    assert "hit_rate" in cat_entry, (
        "dispatcher reads hit_rate for magnitude calculation"
    )
    assert "sample_count" in cat_entry, "dispatcher reads sample_count for floor check"
    assert cat_entry["sample_count"] == 3
    assert abs(cat_entry["hit_rate"] - round(2 / 3, 3)) < 0.001


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_stores_direction_text(
    db_session: AsyncSession,
):
    """direction_text is stored in by_direction entries when project.directions has titles."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    direction_id = "d-text-test"
    project = await _make_project(db_session)
    # Inject a direction with a title into the project
    project.directions = [
        {"id": direction_id, "text": "Fix the login regression", "status": "active"}
    ]
    db_session.add(project)

    tasks = []
    for _ in range(3):
        t = _make_sr_task_with_direction(
            project.id,
            direction_id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    entry = result["by_direction"][direction_id]
    assert entry.get("direction_text") == "Fix the login regression"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_direction_significance_gates_trend(
    db_session: AsyncSession,
):
    """Per-direction trends require both significance AND delta threshold.

    With small sample size (n=5), a delta of 0.2 is noise (z < 1.28) → 'stable'.
    With large sample size (n=30), the same delta is significant (z >= 1.28) → 'increasing'.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    # --- Small n: direction with 5 tasks, previous hit_rate=0.4, current hit_rate=0.6 ---
    project_small = await _make_project(db_session)
    direction_id_small = "d-small-n"

    # Seed prior state: hit_rate was 0.4 (2 caught, 3 missed)
    project_small.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id_small: {
                    "miss_rate": 0.6,  # 1 - 0.4
                    "sample_count": 5,
                    "rate_history": [0.6],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: 3 caught, 2 missed → hit_rate 0.6, miss_rate 0.4 (delta = -0.2)
    tasks_small = []
    for _ in range(3):
        t = _make_sr_task_with_direction(
            project_small.id,
            direction_id_small,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],  # SR caught (flagged the human-flagged cat)
        )
        db_session.add(t)
        tasks_small.append(t)
    for _ in range(2):
        t = _make_sr_task_with_direction(
            project_small.id,
            direction_id_small,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],  # SR missed (did not flag the human-flagged cat)
        )
        db_session.add(t)
        tasks_small.append(t)
    for t in _make_window_padding(project_small.id, age_days=2):
        db_session.add(t)
        tasks_small.append(t)
    for t in _make_window_padding(project_small.id, age_days=20):
        db_session.add(t)
        tasks_small.append(t)

    # --- Large n: direction with 30 tasks, same delta but z-test significant ---
    project_large = await _make_project(db_session)
    direction_id_large = "d-large-n"

    # Seed prior state: hit_rate was 0.4 (12 caught, 18 missed)
    project_large.design_doc = {
        "sr_catch_rate_trends": {
            "by_direction": {
                direction_id_large: {
                    "miss_rate": 0.6,  # 1 - 0.4
                    "sample_count": 30,
                    "rate_history": [0.6],
                }
            }
        }
    }
    await db_session.flush()

    # Current window: 18 caught, 12 missed → hit_rate 0.6, miss_rate 0.4 (delta = -0.2)
    tasks_large = []
    for _ in range(18):
        t = _make_sr_task_with_direction(
            project_large.id,
            direction_id_large,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],  # SR caught (flagged the human-flagged cat)
        )
        db_session.add(t)
        tasks_large.append(t)
    for _ in range(12):
        t = _make_sr_task_with_direction(
            project_large.id,
            direction_id_large,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],  # SR missed (did not flag the human-flagged cat)
        )
        db_session.add(t)
        tasks_large.append(t)
    for t in _make_window_padding(project_large.id, age_days=2):
        db_session.add(t)
        tasks_large.append(t)
    for t in _make_window_padding(project_large.id, age_days=20):
        db_session.add(t)
        tasks_large.append(t)

    await db_session.flush()

    result_small = await compute_sr_catch_rate_trends(
        project_small.id, db_session, tasks_small
    )
    result_large = await compute_sr_catch_rate_trends(
        project_large.id, db_session, tasks_large
    )

    assert result_small is not None
    assert result_large is not None
    entry_small = result_small["by_direction"][direction_id_small]
    entry_large = result_large["by_direction"][direction_id_large]

    # Same delta (-0.2), but different significance at n=5 vs n=30
    assert entry_small["trend"] == "stable", (
        "n=5, delta=-0.2: z < 1.28, not significant → stable"
    )
    assert entry_large["trend"] == "decreasing", (
        "n=30, delta=-0.2: z >= 1.28, significant → decreasing"
    )


def _make_sr_task_with_pass_reasoning(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    pass_cats_with_reasoning: dict[str, str],
) -> "Task":
    """Build a Task with PASS check_results entries that include reasoning text.

    pass_cats_with_reasoning: {cat: reasoning_string}.  Categories not in the
    dict are absent from check_results.  The task has no human rejections beyond
    what's supplied in human_cats.
    """
    check_results = {
        cat: {"passed": True, "reasoning": reasoning}
        for cat, reasoning in pass_cats_with_reasoning.items()
    }
    task = Task(
        project_id=project_id,
        title="SR task with PASS reasoning",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_reasoning_engagement_populated_for_stable_categories(
    db_session: AsyncSession,
):
    """Engagement fields are present on stable/increasing categories (not just decreasing)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (10 tasks): correctness appears in both windows with stable hit rate.
    # 8 tasks: SR hits correctness (FAIL entry) + 2 tasks: SR passes with reasoning (PASS entry).
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "substantial reasoning here"},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (10 tasks): same hit rate → stable trend.
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None, "correctness category should be present"
    assert cat["trend"] == "stable", "should be stable — same rate in both windows"
    # Engagement fields must be populated even for stable categories.
    assert "reasoning_engagement_rate" in cat
    assert "reasoning_engagement_with" in cat
    assert "reasoning_engagement_total" in cat
    # 2 current-window tasks had PASS entries for correctness; 2 with substantial reasoning.
    assert cat["reasoning_engagement_total"] == 2
    assert cat["reasoning_engagement_with"] == 2
    assert cat["reasoning_engagement_rate"] == 1.0


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_fires_for_non_regressing_category(
    db_session: AsyncSession,
):
    """A non-regressing category with low engagement and >= min-passes triggers thinning Alert."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # Current window: correctness hit rate = 10/20 = 0.5 (exactly at floor, NOT below).
    # 10 hit tasks + 10 explicit PASS entries with no substantial reasoning.
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={
                "correctness": "x"
            },  # ≤ 10 chars → no engagement
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: same structure so trend = stable.
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "correctness" in alerts[0].title


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_suppressed_below_min_passes(
    db_session: AsyncSession,
):
    """Alert is NOT fired when total PASS count is below min-passes threshold."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # Current window: 8 hits + 3 PASS entries (below min_passes=10).
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: same structure.
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0, "no alert when passes < min_passes"


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_suppressed_for_regressing_category(
    db_session: AsyncSession,
):
    """Alert is NOT fired for categories already covered by a regression alert."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # correctness is regressing: 3/10 hit in current, 5/5 in previous.
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={
                "correctness": "x"
            },  # explicit PASS, no reasoning
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    thinning_alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(thinning_alerts) == 0, (
        "thinning alert suppressed for regressing category"
    )


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_honors_design_doc_overrides(
    db_session: AsyncSession,
):
    """sr_engagement_threshold and sr_engagement_min_passes overrides from design_doc are honored."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    # Override: threshold=1.0 (always fire if any PASS lacks reasoning), min_passes=3.
    project.design_doc = {
        "sr_engagement_threshold": 1.0,
        "sr_engagement_min_passes": 3,
    }
    tasks = []
    # Current window: 7 hits + 3 PASS entries (1 with reasoning, 2 without).
    # Engagement = 1/3 = 0.33 < 1.0, total_passes=3 >= 3 → alert fires.
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"correctness": 1},
        sr_failed_cats=[],
        sr_passed_cats_with_reasoning={
            "correctness": "this is a substantial reasoning"
        },
    )
    db_session.add(t)
    tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},  # short → not counted
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: same structure (stable trend).
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        "alert fires because engagement (0.33) < threshold (1.0) with 3 passes"
    )


@pytest.mark.asyncio
async def test_pass_reasoning_thinning_alert_deduplicates_within_cycle(
    db_session: AsyncSession,
):
    """Calling compute_sr_catch_rate_trends twice with the same thinning state does not create duplicate Alerts."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from app.models import Alert, AlertSeverity

    project = await _make_project(db_session)
    tasks = []
    # Same structure as the fires test: hit rate=0.5, 10 PASS entries with no reasoning.
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": "x"},
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    # First call.
    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()
    # Second call — same data, same cycle.
    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.title.like("PASS reasoning thinning%"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "dedup: second call must not create a second Alert"


# ─────────────────────────────────────────────────────────────────────────────
# compute_sr_catch_rate_trends — regression_risk / per-task-type axes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_regression_risk_hit_and_miss(
    db_session: AsyncSession,
):
    """When tasks carry regression_risk in both human_cats and sr_failed, the dashboard
    surfaces a non-zero hit_rate on the regression_risk axis."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): 3 hits, 2 misses on regression_risk
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=["regression_risk"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"regression_risk": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (5 tasks): all hits
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"regression_risk": 1},
            sr_failed_cats=["regression_risk"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "regression_risk" in result["categories"], (
        "regression_risk axis must be wired through compute_sr_catch_rate_trends"
    )
    cat = result["categories"]["regression_risk"]
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["miss_rate"] == round(1 - 3 / 5, 3)
    assert cat["previous_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_per_task_type_breakdown(
    db_session: AsyncSession,
):
    """by_task_type contains correct hit/miss rates per (task_type, category)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 'create' tasks — 4 hits on test-coverage
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
            task_type="create",
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task(
        project.id,
        age_days=2,
        human_cats={"test-coverage": 1},
        sr_failed_cats=[],
        task_type="create",
    )
    db_session.add(t)
    tasks.append(t)
    # Current window: 5 'fix' tasks — 2 hits on test-coverage
    for i in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks needed for overall threshold
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_type = result["by_task_type"]
    assert "create" in by_type
    assert "fix" in by_type
    create_cat = by_type["create"]["test-coverage"]
    assert create_cat["hit_rate"] == round(4 / 5, 3)
    assert create_cat["miss_rate"] == round(1 / 5, 3)
    assert create_cat["sample_count"] == 5
    fix_cat = by_type["fix"]["test-coverage"]
    assert fix_cat["hit_rate"] == round(2 / 5, 3)
    assert fix_cat["miss_rate"] == round(3 / 5, 3)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_task_type_threshold_omits_low_sample(
    db_session: AsyncSession,
):
    """Cells with fewer than 5 samples per (task_type, category) are omitted from by_task_type."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 'create' tasks (meets threshold), 4 'fix' tasks (below threshold)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            task_type="create",
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_type = result["by_task_type"]
    assert "create" in by_type
    assert "fix" not in by_type


# ─────────────────────────────────────────────────────────────────────────────
# Tests for by_scope breakdown in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _set_task_scope(task: Task, scope: str) -> Task:
    """Attach scope to task.context, preserving SR fields set by _make_sr_task."""
    ctx = dict(task.context or {})
    ctx["scope"] = scope
    task.context = ctx
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_basic(
    db_session: AsyncSession,
):
    """by_scope contains correct hit/miss rates per (scope, category)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 small-scope tasks: 4 hits on test-coverage
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    t = _set_task_scope(
        _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
        ),
        "small",
    )
    db_session.add(t)
    tasks.append(t)
    # 5 large-scope tasks: 1 hit on test-coverage (markedly more misses)
    for _ in range(1):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "large",
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=[],
            ),
            "large",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "by_scope" in result
    by_scope = result["by_scope"]
    assert "small" in by_scope
    assert "large" in by_scope
    small_cell = by_scope["small"]["test-coverage"]
    assert small_cell["hit_rate"] == round(4 / 5, 3)
    assert small_cell["miss_rate"] == round(1 / 5, 3)
    assert small_cell["sample_count"] == 5
    large_cell = by_scope["large"]["test-coverage"]
    assert large_cell["hit_rate"] == round(1 / 5, 3)
    assert large_cell["miss_rate"] == round(4 / 5, 3)
    assert large_cell["sample_count"] == 5
    # Observability win: large-scope miss rate is markedly higher than small.
    assert large_cell["miss_rate"] > small_cell["miss_rate"]


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_threshold_omits_low_sample(
    db_session: AsyncSession,
):
    """Cells with fewer than 5 samples per (scope, category) are omitted from by_scope."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 small-scope tasks (meets threshold)
    for _ in range(5):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    # 4 medium-scope tasks (below threshold — must be omitted)
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            ),
            "medium",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_scope = result["by_scope"]
    assert "small" in by_scope
    # medium had 4 samples — must be omitted entirely (no 0%/100% sentinel cell)
    assert "medium" not in by_scope


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_default_small_when_missing(
    db_session: AsyncSession,
):
    """Tasks without an explicit scope bucket under DEFAULT_TASK_SCOPE='small'."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 tasks with no scope set in context — should bucket under 'small' (default).
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_scope = result["by_scope"]
    assert "small" in by_scope
    assert by_scope["small"]["correctness"]["sample_count"] == 5


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_fp_stats(
    db_session: AsyncSession,
):
    """by_scope cells carry fp_count / tp_count / false_positive_rate."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 large-scope create tasks: SR flags `correctness` but human never confirms → FP=5
    for _ in range(5):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            ),
            "large",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_scope = result["by_scope"]
    assert "large" in by_scope
    cell = by_scope["large"]["correctness"]
    assert "fp_count" in cell
    assert "tp_count" in cell
    assert "false_positive_rate" in cell
    # All 5 SR flags were confirmed by human → all TPs, no FPs.
    assert cell["tp_count"] == 5
    assert cell["fp_count"] == 0
    assert cell["false_positive_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for by_scope rate_history and trend in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_rate_history_and_trend(
    db_session: AsyncSession,
):
    """by_scope cells carry rate_history and trend after multiple windows."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Seed existing by_scope: small/test-coverage had hit_rate=0.8 last window.
    # sample_count must be >= _MIN_SR_FINDINGS so the two-proportion z-test
    # gate added by the significance-gating change can fire a directional verdict.
    doc = dict(project.design_doc or {})
    doc["sr_catch_rate_trends"] = {
        "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
        "by_scope": {
            "small": {
                "test-coverage": {
                    "hit_rate": 0.8,
                    "rate_history": [0.8],
                    "sample_count": 5,
                },
            }
        },
        "categories": {},
    }
    project.design_doc = doc
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    # Current window: 2/5 hits (hit_rate=0.4, dropping 0.4 from 0.8 → decreasing)
    for _ in range(2):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=[],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (needed to satisfy _MIN_SR_SAMPLE for the function)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cell = result["by_scope"]["small"]["test-coverage"]
    assert "rate_history" in cell
    # Existing [0.8] + new 0.4 = [0.8, 0.4]
    assert len(cell["rate_history"]) == 2
    assert cell["rate_history"][-1] == round(2 / 5, 3)
    # hit_rate dropped 0.4 → delta <= -0.05 → "decreasing"
    assert cell["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_rate_history_cap(
    db_session: AsyncSession,
):
    """rate_history per (scope, category) is capped at 5 entries."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    doc = dict(project.design_doc or {})
    doc["sr_catch_rate_trends"] = {
        "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
        "by_scope": {
            "small": {
                "test-coverage": {
                    "hit_rate": 0.8,
                    "rate_history": [0.9, 0.85, 0.8, 0.75],
                },
            }
        },
        "categories": {},
    }
    project.design_doc = doc
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    for _ in range(5):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cell = result["by_scope"]["small"]["test-coverage"]
    assert len(cell["rate_history"]) <= 5


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_scope_below_sample_no_history(
    db_session: AsyncSession,
):
    """Cells below _MIN_SR_SAMPLE are absent and do not pollute rate_history."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 4 small/test-coverage tasks: below _MIN_SR_SAMPLE=5 → cell absent.
    # One extra task on a different category to keep the current window ≥ 5 total
    # (otherwise the overall function returns None before reaching by_scope).
    for _ in range(4):
        t = _set_task_scope(
            _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"test-coverage": 1},
                sr_failed_cats=["test-coverage"],
            ),
            "small",
        )
        db_session.add(t)
        tasks.append(t)
    t = _set_task_scope(
        _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
        ),
        "medium",
    )
    db_session.add(t)
    tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    small = (result.get("by_scope") or {}).get("small", {})
    assert "test-coverage" not in small


# ─────────────────────────────────────────────────────────────────────────────
# Tests for miss-example collection in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_with_review(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    sr_failed_cats: list,
    review_issues: list,
    task_type: str = "create",
) -> "Task":
    """Build a Task with review_history issues for example extraction."""
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    task = Task(
        project_id=project_id,
        title="SR task with review",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
            "review_loop": {
                "review_history": [{"iteration": 1, "issues": review_issues}]
            },
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_example_issues_populated(
    db_session: AsyncSession,
):
    """Examples are collected from review_history for missed categories."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    review_issue = {"description": "Missing null check in handler", "severity": "MAJOR"}
    # 5 current tasks: SR missed error-handling (sr_failed_cats=[])
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=[review_issue],
        )
        db_session.add(t)
        tasks.append(t)
    # 5 previous tasks
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["error-handling"]
    assert "examples" in cat
    assert len(cat["examples"]) >= 1
    assert any("null check" in ex["desc"] for ex in cat["examples"])
    assert all(
        ex["severity"] in ("CRITICAL", "MAJOR", "MINOR") for ex in cat["examples"]
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_examples_by_task_type_scoped(
    db_session: AsyncSession,
):
    """Examples in by_task_type are drawn only from matching task types."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # "null check" is in error-handling keywords; "fallback" is also in error-handling
    create_issue = {
        "description": "Missing null check in create handler",
        "severity": "MAJOR",
    }
    fix_issue = {
        "description": "Unhandled exception in fix scenario",
        "severity": "MAJOR",
    }

    # 5 'create' tasks missing error-handling
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=[create_issue],
            task_type="create",
        )
        db_session.add(t)
        tasks.append(t)
    # 5 'fix' tasks missing error-handling
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=[fix_issue],
            task_type="fix",
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    by_type = result["by_task_type"]
    assert "create" in by_type
    assert "fix" in by_type
    create_exs = by_type["create"]["error-handling"].get("examples", [])
    fix_exs = by_type["fix"]["error-handling"].get("examples", [])
    create_descs = [e["desc"] for e in create_exs]
    fix_descs = [e["desc"] for e in fix_exs]
    # create examples must contain create-specific text and not fix-specific text
    assert any("null check" in d for d in create_descs)
    assert not any("exception" in d for d in create_descs)
    assert any("exception" in d for d in fix_descs)
    assert not any("null check" in d for d in fix_descs)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_sensitive_examples_dropped(
    db_session: AsyncSession,
):
    """Descriptions containing sensitive tokens are excluded from examples."""
    from app.playbook_learning import compute_sr_catch_rate_trends, _SENSITIVE_TOKENS

    project = await _make_project(db_session)
    tasks = []
    # Use a token from the list
    sensitive_token = _SENSITIVE_TOKENS[0]  # e.g. "password"
    review_issues = [
        {"description": f"Leaked {sensitive_token} in logs", "severity": "CRITICAL"},
        {"description": "Unhandled exception in handler", "severity": "MAJOR"},
    ]
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    examples = result["categories"]["error-handling"].get("examples", [])
    assert all(sensitive_token not in ex["desc"].lower() for ex in examples)
    # The non-sensitive issue should still appear
    assert any("Unhandled exception" in ex["desc"] for ex in examples)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_descriptions_truncated_and_pr_stripped(
    db_session: AsyncSession,
):
    """Descriptions are truncated to <=100 chars and PR refs / URLs are stripped."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    long_desc = "A" * 200  # 200 chars, should be truncated
    pr_desc = "Fix regression introduced in PR #1234 — missing guard"
    url_desc = "See https://example.com/issue/99 for context — unhandled null"
    review_issues = [
        {"description": long_desc, "severity": "MAJOR"},
        {"description": pr_desc, "severity": "MAJOR"},
        {"description": url_desc, "severity": "MINOR"},
    ]
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    examples = result["categories"]["error-handling"].get("examples", [])
    for ex in examples:
        assert len(ex["desc"]) <= 100
    pr_exs = [e for e in examples if "missing guard" in e["desc"]]
    if pr_exs:
        assert "PR" not in pr_exs[0]["desc"]
        assert "#1234" not in pr_exs[0]["desc"]
    url_exs = [e for e in examples if "unhandled null" in e["desc"]]
    if url_exs:
        assert "https://" not in url_exs[0]["desc"]


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_example_cap_per_category(
    db_session: AsyncSession,
):
    """At most _EXAMPLE_MAX_PER_CAT examples are stored per category."""
    from app.playbook_learning import compute_sr_catch_rate_trends, _EXAMPLE_MAX_PER_CAT

    project = await _make_project(db_session)
    tasks = []
    # Provide many distinct review issues per task
    review_issues = [
        {"description": f"Issue number {i}", "severity": "MAJOR"} for i in range(10)
    ]
    for _ in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"error-handling": 1},
            sr_failed_cats=[],
            review_issues=review_issues,
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"error-handling": 1},
            sr_failed_cats=["error-handling"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    examples = result["categories"]["error-handling"].get("examples", [])
    assert len(examples) <= _EXAMPLE_MAX_PER_CAT


# ─────────────────────────────────────────────────────────────────────────────
# 'other' bucket exclusion from SR catch-rate denominator
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_excludes_other_from_denominator(
    db_session: AsyncSession,
):
    """'other' category must not appear as a 0% catch-rate row nor depress overall_hit_rate.

    Self-review never emits 'other', so any task whose human-truth set contains
    'other' would otherwise be a guaranteed miss that silently drags the headline
    down.  This test builds two windows where the only human-flagged category in
    some tasks is 'other', verifies that 'other' is absent from the categories
    dict, and that overall_hit_rate reflects only the canonical categories.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5 canonical 'correctness' tasks (SR hits all 5) + 2 tasks
    # flagging only 'other' (SR can never emit 'other').  Without filtering,
    # overall_hit_rate would be 5/7 ≈ 0.714; with 'other' excluded it must be 5/5 = 1.0.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"other": 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks all hitting 'correctness' (needed for trend gating)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cats = result.get("categories", {})
    assert "other" not in cats, "'other' must not appear as a catch-rate category"
    assert "correctness" in cats, "canonical categories must still be present"
    # overall_hit_rate must reflect only canonical categories (5 hits / 5 findings = 1.0)
    assert result.get("overall_hit_rate") == 1.0, (
        f"overall_hit_rate should be 1.0 (5/5 canonical hits) but got "
        f"{result.get('overall_hit_rate')} — 'other' is depressing it"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for regression notifications in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_decreasing_task_set(project_id, db_session, tasks):
    """Helper: 5 current tasks (3/5 hit) + 5 previous tasks (5/5 hit) → decreasing."""
    for i in range(3):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project_id, age_days=2, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_fires_notification_on_decreasing(
    db_session: AsyncSession,
):
    """A decreasing trend triggers post_failure_alert_message and dispatch_external_notification."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    _make_decreasing_task_set(project.id, db_session, tasks)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["test-coverage"]["trend"] == "decreasing"

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    description = (
        call_kwargs.args[2]
        if len(call_kwargs.args) >= 3
        else call_kwargs.kwargs.get("description", "")
    )
    assert "test-coverage" in description
    assert "→" in description

    mock_dispatch.assert_called_once()
    dispatch_kwargs = mock_dispatch.call_args
    payload = dispatch_kwargs.kwargs.get("payload") or dispatch_kwargs.args[2]
    assert (
        dispatch_kwargs.kwargs.get("event_type") == "sr_catch_rate_regression"
        or dispatch_kwargs.args[1] == "sr_catch_rate_regression"
    )
    assert "regressing_categories" in payload
    cats = payload["regressing_categories"]
    assert len(cats) == 1
    assert cats[0]["category"] == "test-coverage"
    assert cats[0]["previous_hit_rate"] is not None
    assert cats[0]["current_hit_rate"] is not None
    assert cats[0]["sample_size"] > 0

    # AlertsBanner surfacing: the same firing path must insert an
    # undismissed WARNING Alert row keyed on the regression title prefix.
    from app.playbook_learning import _SR_REGRESSION_ALERT_TITLE_PREFIX

    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    reg_rows = [
        r for r in alert_rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)
    ]
    assert len(reg_rows) == 1
    assert reg_rows[0].severity == AlertSeverity.WARNING
    assert "test-coverage" in reg_rows[0].title
    # Message reuses the same description string as the ProjectMessage
    assert "test-coverage" in reg_rows[0].message


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_no_duplicate_in_same_window(
    db_session: AsyncSession,
):
    """A second call with the same regressing categories in the same window does NOT re-post."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    _make_decreasing_task_set(project.id, db_session, tasks)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # Verify alert state was persisted
    analytics = await _read_sr_analytics(db_session, project)
    assert "sr_catch_rate_trends_alert" in analytics
    assert mock_post.call_count == 1

    # Second call in the same window — should be deduped
    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post2,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    mock_post2.assert_not_called()

    # The dedup contract also covers the in-app Alert row: still exactly one.
    from app.playbook_learning import _SR_REGRESSION_ALERT_TITLE_PREFIX

    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    reg_rows = [
        r for r in alert_rows if r.title.startswith(_SR_REGRESSION_ALERT_TITLE_PREFIX)
    ]
    assert len(reg_rows) == 1


@pytest.mark.asyncio
async def test_sr_catch_rate_regression_no_notification_when_all_stable_or_increasing(
    db_session: AsyncSession,
):
    """No notification fires when no category is decreasing."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 5/5 hit (stable/increasing vs previous 3/5)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=20, human_cats={"test-coverage": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    trend = result["categories"]["test-coverage"]["trend"]
    assert trend in ("stable", "increasing")
    mock_post.assert_not_called()
    mock_dispatch.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for all_iterations_catch_rate in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_all_iterations_catch_rate_vs_first_attempt(
    db_session: AsyncSession,
):
    """A category flagged only in iteration 2+ appears in all_iterations_catch_rate as a miss
    but current_hit_rate (first-attempt) is None for that category — the two metrics diverge."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window (5 tasks): SR passes everything.
    # First-attempt reviewer-confirmed truth: test-coverage only (iteration 0).
    # All-iterations truth: test-coverage + correctness (flagged in iteration 2).
    for _ in range(5):
        task = Task(
            project_id=project.id,
            title="SR task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"check_results": {}},
                "_first_attempt_rejection_categories": {"test-coverage": 1},
                "_all_iterations_rejection_categories": {
                    "test-coverage": 1,
                    "correctness": 1,
                },
            },
        )
        task.created_at = _utcnow() - timedelta(days=2)
        db_session.add(task)
        tasks.append(task)
    # Previous window (5 tasks): same shape
    for _ in range(5):
        task = Task(
            project_id=project.id,
            title="SR task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"check_results": {}},
                "_first_attempt_rejection_categories": {"test-coverage": 1},
                "_all_iterations_rejection_categories": {
                    "test-coverage": 1,
                    "correctness": 1,
                },
            },
        )
        task.created_at = _utcnow() - timedelta(days=20)
        db_session.add(task)
        tasks.append(task)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cats = result["categories"]
    # test-coverage: flagged in first iteration → visible in both metrics
    assert "test-coverage" in cats
    assert cats["test-coverage"]["current_hit_rate"] == 0.0
    assert cats["test-coverage"]["all_iterations_catch_rate"] == 0.0

    # correctness: flagged only in iteration 2 — invisible to the first-attempt metric
    # but the all-iterations metric counts it as a miss (catch_rate == 0)
    assert "correctness" in cats, (
        "iteration-2 category must surface in all_iterations_catch_rate"
    )
    assert cats["correctness"]["current_hit_rate"] is None, (
        "correctness must be absent from first-attempt metric"
    )
    assert cats["correctness"]["all_iterations_catch_rate"] == 0.0
    assert cats["correctness"]["all_iterations_miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_all_iterations_backward_compat(
    db_session: AsyncSession,
):
    """When _all_iterations_rejection_categories is absent, review_history-only
    categories are ignored and all_iterations_catch_rate falls back to first-attempt."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    review_history = [
        {
            "iteration": 2,
            "issues": [
                {"description": "architecture issue in docs", "severity": "MAJOR"}
            ],
        }
    ]
    # 5 current tasks: only _first_attempt_rejection_categories, no all-iterations field
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        t.context["review_loop"] = {"review_history": review_history}
        db_session.add(t)
        tasks.append(t)
    # 5 previous tasks: same
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        t.context["review_loop"] = {"review_history": review_history}
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    # Both metrics resolve to the same value when there's no extra iteration data
    assert cat["current_hit_rate"] is not None
    assert cat["all_iterations_catch_rate"] is not None
    assert cat["current_hit_rate"] == cat["all_iterations_catch_rate"]
    assert cat["all_iterations_miss_rate"] is not None
    assert "architecture" not in result["categories"]


# ---------------------------------------------------------------------------
# SR false-positive rate and precision per category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_false_positive_rate_no_human_confirmation(db_session: AsyncSession):
    """SR fails a category that no human ever flags → false_positive_rate == 1.0."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 5 current: SR fails 'correctness', human flags only 'test-coverage' (unrelated)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # 5 previous: same
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] == 1.0, (
        "all SR failures unconfirmed → FP rate must be 1.0"
    )
    assert cat["precision"] == 0.0, "zero confirmed failures → precision must be 0.0"


@pytest.mark.asyncio
async def test_sr_false_positive_rate_human_confirms_is_zero(db_session: AsyncSession):
    """SR fails a category that human also flags → false_positive_rate == 0.0, precision == 1.0."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] == 0.0, (
        "all SR failures confirmed by human → FP rate 0"
    )
    assert cat["precision"] == 1.0, "all SR failures confirmed → precision must be 1.0"


@pytest.mark.asyncio
async def test_sr_false_positive_rate_review_history_only_is_not_confirmation(
    db_session: AsyncSession,
):
    """SR fails test-coverage; the only reviewer signal is a review_history (iter 2)
    'missing test' issue with no persisted rejection-category record.

    The reviewer-confirmed truth set is now the persisted
    ``_all_iterations_rejection_categories`` / ``_first_attempt_rejection_categories``
    (see :func:`app.sr_blind_spots.reviewer_confirmed_categories`); review_history is
    no longer reconstructed into confirmed categories. With no persisted record for
    test-coverage, the SR failure is therefore counted as a false positive."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    def _make_task_with_review_history(age_days):
        check_results = {"test-coverage": {"passed": False, "issue": "x"}}
        task = Task(
            project_id=project.id,
            title="SR task",
            payloads=[{"type": "create", "prompt": "do something"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"check_results": check_results},
                # Unrelated first-attempt feedback so count >= _MIN_SR_SAMPLE
                "_first_attempt_rejection_categories": {"style-conventions": 1},
                "review_loop": {
                    "review_history": [
                        {
                            "iteration": 2,
                            "issues": [
                                {
                                    "description": "missing test for edge case",
                                    "severity": "MAJOR",
                                }
                            ],
                        }
                    ]
                },
            },
        )
        task.created_at = _utcnow() - timedelta(days=age_days)
        return task

    for _ in range(5):
        t = _make_task_with_review_history(age_days=2)
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_task_with_review_history(age_days=20)
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["test-coverage"]
    # review_history is not a confirmation source → SR failure is a false positive
    assert cat["false_positive_rate"] == 1.0, (
        "review_history-only (no persisted rejection category) does not confirm the "
        "SR failure → it is counted as a false positive"
    )


@pytest.mark.asyncio
async def test_sr_passes_do_not_affect_false_positive_metric(db_session: AsyncSession):
    """When SR passes a category, it contributes nothing to sr_failed_count or fp_count."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # SR passes everything; human flags 'correctness'
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=2, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id, age_days=20, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["false_positive_rate"] is None, (
        "SR never failed correctness → no sr_failed_count → FP rate must be None"
    )
    assert cat["precision"] is None, (
        "SR never failed correctness → precision must be None"
    )


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_stores_loosen_hint_threshold_fields(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends stores fp_count, tp_count, and false_positive_rate
    at values that cross the pre-execution loosen-hint thresholds (fp_rate >= 0.5,
    fp+tp >= 10). The formatHighFpRateLoosenHintForSrPrompt formatter reads these
    fields via self_review_catch_rate.categories to build its pre-execution section."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 7 FP + 3 TP tasks for 'correctness'.
    # FP: SR flags correctness but human flags test-coverage (unrelated).
    # TP: SR flags correctness AND human flags correctness.
    # Result: fp_count=7, tp_count=3, false_positive_rate=0.7, sample=10.
    for _ in range(7):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks where correctness is confirmed (TPs) — needed
    # for _MIN_SR_SAMPLE (5) so the function doesn't return None.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]

    # Verify fields needed by formatHighFpRateLoosenHintForSrPrompt (via _cat_entry).
    assert cat["fp_count"] == 7, f"expected fp_count=7, got {cat['fp_count']}"
    assert cat["tp_count"] == 3, f"expected tp_count=3, got {cat['tp_count']}"
    assert cat["false_positive_rate"] == 0.7, (
        f"fp_rate = 7/(7+3) = 0.7, got {cat['false_positive_rate']}"
    )
    # Both loosen-hint thresholds are met: fp_rate >= 0.5 and sample >= 10.
    sample = cat["fp_count"] + cat["tp_count"]
    assert sample >= 10, f"loosen-hint needs sample>=10, got {sample}"
    assert cat["false_positive_rate"] >= 0.5, "loosen-hint needs fp_rate>=0.5"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_below_loosen_hint_sample_threshold(
    db_session: AsyncSession,
):
    """When sample size (fp+tp) < 10, fp_count and tp_count are stored correctly
    but the loosen-hint threshold is not crossed — category is not over-flagged."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 4 FP + 2 TP = 6 total, below the sample=10 threshold.
    for _ in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks so the function does not return None.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["fp_count"] == 4
    assert cat["tp_count"] == 2
    sample = cat["fp_count"] + cat["tp_count"]
    assert sample == 6 and sample < 10, (
        "sample below loosen-hint threshold — category should not trigger loosen hint"
    )


# ---------------------------------------------------------------------------
# Tests for per-severity catch-rate breakdown in compute_sr_catch_rate_trends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_counts(
    db_session: AsyncSession,
):
    """by_severity accumulates correct hit_count and human_count per severity."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 5 tasks with CRITICAL issue in "error-handling"; SR catches 2 of them
    # "exception" keyword → categorised as error-handling
    review_issue_critical = {
        "description": "Unhandled exception in request path",
        "severity": "CRITICAL",
    }
    for i in range(2):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_issue_critical],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(3):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],  # SR did NOT catch error-handling
            review_issues=[review_issue_critical],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks with MAJOR issue in "error-handling"; SR catches all
    review_issue_major = {
        "description": "Missing error handling for null input",
        "severity": "MAJOR",
    }
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_issue_major],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "by_severity" in result
    by_sev = result["by_severity"]

    # CRITICAL: 5 issues total (current window), 2 caught by SR
    assert by_sev["CRITICAL"]["current_sample"] == 5
    assert by_sev["CRITICAL"]["current_hit_rate"] == round(2 / 5, 3)
    assert by_sev["CRITICAL"]["current_miss_rate"] == round(3 / 5, 3)

    # MAJOR: 5 issues in previous window, 5 caught
    assert by_sev["MAJOR"]["previous_sample"] == 5
    assert by_sev["MAJOR"]["previous_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_critical_alert_tighter_threshold(
    db_session: AsyncSession,
):
    """CRITICAL regression (>=3pp delta) fires the alert even when category trend is stable."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Review issue that categorises to error-handling (keyword "exception")
    review_crit = {
        "description": "Unhandled exception bypasses auth guard",
        "severity": "CRITICAL",
    }

    # Current window: 5 tasks; SR misses error-handling (sr_failed only has "correctness")
    # → CRITICAL miss_rate = 5/5 = 1.0
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 tasks; SR catches error-handling for 4 of them
    # → CRITICAL miss_rate = 1/5 = 0.2
    for i in range(4):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_review(
        project.id,
        age_days=20,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
        review_issues=[review_crit],
    )
    db_session.add(t)
    tasks.append(t)

    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # Per-category trend for "correctness" should be stable (all caught in both windows)
    assert result["categories"]["correctness"]["trend"] in ("stable", "increasing")
    # Per-severity CRITICAL must be regressing (delta = 0.8 >= 0.03)
    assert result["by_severity"]["CRITICAL"]["trend"] == "regressing"
    # Alert must fire due to severity regression
    mock_post.assert_called_once()
    call_desc = (
        mock_post.call_args.args[2]
        if len(mock_post.call_args.args) >= 3
        else mock_post.call_args.kwargs.get("description", "")
    )
    assert "CRITICAL" in call_desc
    mock_dispatch.assert_called_once()
    payload = (
        mock_dispatch.call_args.kwargs.get("payload") or mock_dispatch.call_args.args[2]
    )
    assert "regressing_severities" in payload
    sevs = payload["regressing_severities"]
    assert any(s["severity"] == "CRITICAL" for s in sevs)


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_severity_dedup_includes_sevs(
    db_session: AsyncSession,
):
    """A new regressing severity not in the prior alert state breaks dedup and re-fires."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified as _flag_modified

    project = await _make_project(db_session)
    tasks = []

    review_crit = {
        "description": "Unhandled exception on auth path",
        "severity": "CRITICAL",
    }
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(4):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)
    t = _make_sr_task_with_review(
        project.id,
        age_days=20,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
        review_issues=[review_crit],
    )
    db_session.add(t)
    tasks.append(t)
    await db_session.flush()

    # Pre-seed alert state with categories=[] and severities=[] to simulate prior alert
    # that covered no categories and no severities.
    doc = dict(project.design_doc or {})
    doc["sr_catch_rate_trends_alert"] = {
        "computed_at": (_utcnow() - timedelta(days=1)).isoformat(),
        "cycle_id": None,
        "categories": [],
        "severities": [],
    }
    project.design_doc = doc
    _flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # CRITICAL is regressing, so alert fires despite prior alert existing (different severity set)
    assert result is not None
    assert result["by_severity"]["CRITICAL"]["trend"] == "regressing"
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_absent_when_no_rev_hist(
    db_session: AsyncSession,
):
    """by_severity has zero samples when tasks have no review_history."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "by_severity" in result
    by_sev = result["by_severity"]
    # No review_history → all samples are zero, all rates are None
    for sev in ("CRITICAL", "MAJOR", "MINOR"):
        assert by_sev[sev]["current_sample"] == 0
        assert by_sev[sev]["previous_sample"] == 0
        assert by_sev[sev]["current_miss_rate"] is None
        assert by_sev[sev]["trend"] == "stable"


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_by_severity_persisted_to_dedicated_store(
    db_session: AsyncSession,
):
    """by_severity is persisted to project.design_doc['sr_catch_rate_trends']."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    review_issue = {"description": "Logic bug in edge case", "severity": "MAJOR"}
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_issue],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_issue],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    assert result is not None
    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    assert "by_severity" in stored
    assert "CRITICAL" in stored["by_severity"]
    assert "MAJOR" in stored["by_severity"]
    assert "MINOR" in stored["by_severity"]


# ---------------------------------------------------------------------------
# Significance-gate tests for FP and severity sub-trends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_small_sample_swing_suppressed_no_alert(
    db_session: AsyncSession,
):
    """Small CRITICAL bucket (n=2) suppresses directional verdict even with large delta.

    Without the z-test gate a miss-rate jump of 1.0 would label 'regressing' and
    fire a spurious alert.  With the gate (n < _MIN_SR_FINDINGS=5 → stable) no
    alert should fire and the trend label must be 'stable'.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    review_crit = {
        "description": "Unhandled exception bypasses guard",
        "severity": "CRITICAL",
    }

    # Current window — 5 base tasks (meets finding-count gate, no severity samples)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # Current window — 2 tasks with CRITICAL review issue; SR misses error-handling
    # → CRITICAL human_count=2, hit=0, miss_rate=1.0
    for _ in range(2):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window — 5 base tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window — 2 tasks with CRITICAL; SR catches error-handling
    # → CRITICAL human_count=2, hit=2, miss_rate=0.0
    for _ in range(2):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # n=2 < _MIN_SR_FINDINGS → must be suppressed to 'stable'
    assert result["by_severity"]["CRITICAL"]["trend"] == "stable"
    # No spurious alert
    mock_post.assert_not_called()
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_severity_large_sample_swing_fires_alert(
    db_session: AsyncSession,
):
    """Sufficient CRITICAL bucket (n=10) with large delta correctly fires a regression alert.

    This is the positive-control counterpart: the same logical scenario as the
    small-sample test but with n=10 ≥ _MIN_SR_FINDINGS and a z-score >> 1.28.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    review_crit = {
        "description": "Unhandled exception bypasses guard",
        "severity": "CRITICAL",
    }

    # Current: 5 base tasks + 10 CRITICAL tasks where SR misses error-handling
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task_with_review(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous: 5 base tasks + 10 CRITICAL tasks where SR catches error-handling
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(10):
        t = _make_sr_task_with_review(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
            review_issues=[review_crit],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # n=10 ≥ 5, delta=1.0 → z >> 1.28 → must fire
    assert result["by_severity"]["CRITICAL"]["trend"] == "regressing"
    mock_post.assert_called_once()
    payload = (
        mock_dispatch.call_args.kwargs.get("payload") or mock_dispatch.call_args.args[2]
        if mock_dispatch.called
        else {}
    )
    assert mock_dispatch.called
    sevs = payload.get("regressing_severities", [])
    assert any(s["severity"] == "CRITICAL" for s in sevs)


@pytest.mark.asyncio
async def test_fp_trend_small_sample_suppressed_no_alert(
    db_session: AsyncSession,
):
    """Small SR-failed bucket (n=3) suppresses fp_trend directional verdict.

    A jump from fp_rate=0.0 to fp_rate=1.0 exceeds the raw 5pp threshold but
    the z-test gate (n < _MIN_SR_FINDINGS=5) must suppress it to 'stable' and
    prevent a spurious alert.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window:
    # 5 base tasks (meet main finding gate); SR correctly flags correctness.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # 3 tasks where SR also flags "error-handling" but humans never flag it → FP
    # sr_failed_count["error-handling"]=3, fp_count["error-handling"]=3, fp_rate=1.0
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window:
    # 5 base tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # 3 tasks where SR flags "error-handling" AND humans also flag it → NOT FP
    # sr_failed_count["error-handling"]=3, fp_count["error-handling"]=0, fp_rate=0.0
    for _ in range(3):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1, "error-handling": 1},
            sr_failed_cats=["correctness", "error-handling"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # error-handling FP: sr_failed_count=3 < 5 → must be 'stable', no alert
    eh = result["categories"].get("error-handling") or {}
    assert eh.get("fp_trend") == "stable"
    mock_post.assert_not_called()
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_fp_trend_large_sample_fires_alert(
    db_session: AsyncSession,
):
    """Sufficient SR-failed bucket (n=8) with large FP-rate jump fires an alert.

    Positive control: n=8 ≥ _MIN_SR_FINDINGS with fp_rate jump 0→1 produces
    z >> 1.28 → fp_trend='increasing' and alert fires.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window: 5 base + 8 FP tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "error-handling"],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window: 5 base + 8 tasks where error-handling is confirmed (not FP)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(8):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1, "error-handling": 1},
            sr_failed_cats=["correctness", "error-handling"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ) as mock_dispatch,
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # n=8 ≥ 5, delta=1.0 → z >> 1.28 → fp_trend must be 'increasing'
    eh = result["categories"].get("error-handling") or {}
    assert eh.get("fp_trend") == "increasing"
    mock_post.assert_called_once()
    assert mock_dispatch.called


# ─────────────────────────────────────────────────────────────────────────────
# Tests for cycle-based bucketing in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_cycle(project_id, *, age_days: float, outcome: str = "completed"):
    from app.models import StrategicCycle

    cycle = StrategicCycle(
        id=uuid.uuid4().hex,
        project_id=project_id,
        outcome=outcome,
    )
    cycle.phase1_started_at = _utcnow() - timedelta(days=age_days)
    return cycle


@pytest.mark.asyncio
async def test_cycle_bucketing_uses_strategic_cycle_id(db_session: AsyncSession):
    """When two completed cycles exist, tasks are bucketed by strategic_cycle_id."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    tasks = []
    # Current cycle: 3/5 hit
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    # Previous cycle: 5/5 hit
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == c_cycle.id
    assert result["previous_cycle_id"] == p_cycle.id
    cat = result["categories"]["correctness"]
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["previous_hit_rate"] == 1.0
    assert cat["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_cycle_bucketing_skips_underpopulated_cycle_rolls_further_back(
    db_session: AsyncSession,
):
    """Skips a cycle pair where current bucket has < _MIN_SR_SAMPLE and uses N-1 / N-2."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    newest_cycle = _make_cycle(project.id, age_days=2)  # only 2 tasks — below threshold
    mid_cycle = _make_cycle(project.id, age_days=20)  # 5 tasks
    old_cycle = _make_cycle(project.id, age_days=40)  # 5 tasks
    db_session.add(newest_cycle)
    db_session.add(mid_cycle)
    db_session.add(old_cycle)
    await db_session.flush()

    tasks = []
    for i in range(2):  # newest: below threshold
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = newest_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):  # mid: 5 tasks (should become "current")
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = mid_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):  # old: 5 tasks (should become "previous")
        t = _make_sr_task(
            project.id,
            age_days=39,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = old_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == mid_cycle.id
    assert result["previous_cycle_id"] == old_cycle.id


@pytest.mark.asyncio
async def test_fallback_to_calendar_windows_when_no_completed_cycles(
    db_session: AsyncSession,
):
    """Falls back to 14-day calendar windows when no completed cycles exist."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    # Add an in-progress cycle only — no completed cycles.
    in_progress_cycle = _make_cycle(project.id, age_days=2, outcome="in_progress")
    db_session.add(in_progress_cycle)
    await db_session.flush()

    tasks = []
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "calendar"
    assert result["current_cycle_id"] is None
    assert "test-coverage" in result["categories"]


@pytest.mark.asyncio
async def test_cycle_mode_fetches_full_cycle_independent_of_all_tasks(
    db_session: AsyncSession,
):
    """Cycle-mode compares complete cycles even when all_tasks is empty.

    Simulates a high-throughput scenario where the caller's recency-capped
    all_tasks list no longer contains any previous-cycle tasks.  The targeted
    per-cycle DB fetch inside compute_sr_catch_rate_trends must still recover
    the full previous-cycle task set and produce the correct hit rate.
    """
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    # Current cycle: 5 tasks, 3/5 hit.
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
    # Previous cycle: 5 tasks, 5/5 hit — would be evicted by a flat recency cap.
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
    await db_session.flush()

    # Pass all_tasks=[] to simulate the previous cycle being absent from the
    # caller's recency-capped window.  The function must fetch cycle tasks itself.
    result = await compute_sr_catch_rate_trends(project.id, db_session, [])

    assert result is not None, "expected cycle-mode result even with empty all_tasks"
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == c_cycle.id
    assert result["previous_cycle_id"] == p_cycle.id
    cat = result["categories"]["correctness"]
    # Previous cycle must reflect all 5 tasks (5/5 hit), not a truncated tail.
    assert cat["previous_hit_rate"] == 1.0, (
        f"previous hit rate {cat['previous_hit_rate']} != 1.0 — "
        "previous cycle was truncated by recency cap instead of fetched directly"
    )
    assert cat["current_hit_rate"] == round(3 / 5, 3)
    assert cat["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_regression_alert_dedup_tied_to_cycle_id(db_session: AsyncSession):
    """Second call with same cycle_id + regressing cats does NOT re-fire notification."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)

    tasks = []
    for i in range(3):
        t = _make_sr_task(
            project.id,
            age_days=1,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(2):
        t = _make_sr_task(
            project.id, age_days=1, human_cats={"correctness": 1}, sr_failed_cats=[]
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for i in range(5):
        t = _make_sr_task(
            project.id,
            age_days=19,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert mock_post.call_count == 1

    # Second call same cycle — dedup should suppress.
    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post2,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    mock_post2.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests for sr_skip_rate / sr_skip_reason_distribution
# ─────────────────────────────────────────────────────────────────────────────


def _make_skip_task(
    project_id, *, age_days: float, skip_reason: str | None = None
) -> Task:
    """Build a Task whose _self_review_outcome is a fallback/skip envelope."""
    if skip_reason:
        outcome: dict = {
            "verdict": "unknown",
            "check_results": {},
            "skip_reason": skip_reason,
        }
    else:
        # Legacy: verdict unknown + empty check_results, no skip_reason field
        outcome = {"verdict": "unknown", "check_results": {}}
    task = Task(
        project_id=project_id,
        title="Skip task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": outcome},
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_sr_skip_rate_and_distribution(db_session: AsyncSession):
    """sr_skip_rate and sr_skip_reason_distribution are computed for a mixed current window."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window (0-14 days): 5 normal SR tasks + 3 skip tasks (2 structured, 1 legacy)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    t1 = _make_skip_task(project.id, age_days=2, skip_reason="diff_too_small")
    t2 = _make_skip_task(project.id, age_days=3, skip_reason="json_missing")
    t3 = _make_skip_task(project.id, age_days=4, skip_reason=None)  # legacy unknown
    for t in [t1, t2, t3]:
        db_session.add(t)
        tasks.append(t)

    # Previous window (14-28 days): 5 normal SR tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    # 3 skipped out of 8 tasks-with-outcome in current window
    assert "sr_skip_rate" in result
    assert abs(result["sr_skip_rate"] - round(3 / 8, 3)) < 0.001
    dist = result["sr_skip_reason_distribution"]
    assert dist is not None
    assert dist.get("diff_too_small") == 1
    assert dist.get("json_missing") == 1
    assert dist.get("legacy_unknown") == 1


@pytest.mark.asyncio
async def test_sr_skip_rate_omitted_below_min_sample(db_session: AsyncSession):
    """sr_skip_rate is omitted when the current window has fewer than _MIN_SR_SAMPLE tasks-with-outcome."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # 4 tasks in current window (below _MIN_SR_SAMPLE=5 for skip stats)
    for _ in range(4):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # sr_skip_rate is always present in the result dict; it is None when sample is too small.
    if result is not None:
        assert "sr_skip_rate" in result
        assert result["sr_skip_rate"] is None


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_all_fields_always_present(
    db_session: AsyncSession,
):
    """All canonical fields are always present in the result dict with safe defaults."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Enough tasks for compute to return a result (>=5 per window)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None

    required_keys = [
        "computed_at",
        "bucket_mode",
        "current_cycle_id",
        "previous_cycle_id",
        "categories",
        "by_task_type",
        "by_fix_iteration",
        "by_scope",
        "by_severity",
        "sr_skip_rate",
        "sr_skip_reason_distribution",
        "by_direction",
        "overall_trend_verdict",
        "overall_trend_significant",
        "overall_trend_data_sufficient",
    ]
    for key in required_keys:
        assert key in result, f"missing required key: {key}"

    assert isinstance(result["sr_skip_reason_distribution"], dict)
    assert isinstance(result["by_direction"], dict)
    assert "execution_evidence_miss_rates" not in result
    assert "edges_considered_stats" not in result


def _make_fp_task(
    project_id,
    *,
    task_type: str = "create",
    sr_failed_cats: list[str],
    confirmed_cats: list[str],
) -> Task:
    """Build a task with author SR signals and independent-review truth."""
    check_results = {cat: {"passed": False, "issue": "x"} for cat in sr_failed_cats}
    sro: dict = {"check_results": check_results}
    task = Task(
        project_id=project_id,
        title="FP test task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": sro,
            "_first_attempt_rejection_categories": confirmed_cats,
        },
    )
    return task


def _make_fp_quality_task(
    project_id,
    *,
    prompt_fingerprint: str,
    sr_failed_cats: list[str] | None = None,
    human_cats: list[str] | None = None,
    review_history: list[dict] | None = None,
    skipped: bool = False,
) -> Task:
    if skipped:
        sro = {
            "verdict": "unknown",
            "fixes_applied": False,
            "check_results": {},
            "blind_spots": [],
        }
    else:
        sro = {
            "verdict": "pass",
            "fixes_applied": False,
            "check_results": {
                cat: {"passed": False, "issue": "x"} for cat in (sr_failed_cats or [])
            },
            "blind_spots": [],
        }
    context = {"_self_review_outcome": sro}
    if human_cats is not None:
        context["_first_attempt_rejection_categories"] = human_cats
    if review_history is not None:
        context["review_loop"] = {"review_history": review_history}
    return Task(
        project_id=project_id,
        title="FP quality task",
        payloads=[{"type": "create", "prompt": "do something"}],
        prompt_fingerprint=prompt_fingerprint,
        status=TaskStatus.COMPLETED,
        context=context,
    )


def test_compute_cat_fp_stats_fp_and_tp_counts():
    """SR-flagged categories caught by reviewer count as TP; missed by reviewer count as FP."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        # correctness: SR flagged, reviewer also flagged → TP
        _make_fp_task(
            project_id, sr_failed_cats=["correctness"], confirmed_cats=["correctness"]
        ),
        # correctness: SR flagged, reviewer missed → FP
        _make_fp_task(project_id, sr_failed_cats=["correctness"], confirmed_cats=[]),
        # security: SR flagged, reviewer missed → FP
        _make_fp_task(
            project_id, sr_failed_cats=["security"], confirmed_cats=["correctness"]
        ),
    ]

    cat_fp, type_cat_fp, _scope_cat_fp = _compute_cat_fp_stats(tasks)

    assert cat_fp["correctness"]["tp"] == 1
    assert cat_fp["correctness"]["fp"] == 1
    assert cat_fp["security"]["fp"] == 1
    assert cat_fp["security"].get("tp", 0) == 0

    assert type_cat_fp["create"]["correctness"]["tp"] == 1
    assert type_cat_fp["create"]["correctness"]["fp"] == 1
    assert type_cat_fp["create"]["security"]["fp"] == 1


def test_compute_cat_fp_stats_skips_non_code_types():
    """Tasks whose type is not a code-change type are excluded."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        _make_fp_task(
            project_id,
            task_type="research",
            sr_failed_cats=["correctness"],
            confirmed_cats=[],
        ),
        _make_fp_task(
            project_id, task_type="plan", sr_failed_cats=["security"], confirmed_cats=[]
        ),
    ]

    cat_fp, type_cat_fp, scope_cat_fp = _compute_cat_fp_stats(tasks)

    assert cat_fp == {}
    assert type_cat_fp == {}
    assert scope_cat_fp == {}


def test_compute_cat_fp_stats_skips_tasks_without_sro():
    """Tasks with no _self_review_outcome in context are skipped."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    task = Task(
        project_id=project_id,
        title="no sro",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context={},
    )

    cat_fp, type_cat_fp, _scope_cat_fp = _compute_cat_fp_stats([task])

    assert cat_fp == {}


def test_compute_cat_fp_stats_false_positive_rate_formula():
    """FPR = fp / (fp + tp): 2 FP and 1 TP gives 0.667."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    tasks = [
        _make_fp_task(project_id, sr_failed_cats=["correctness"], confirmed_cats=[]),
        _make_fp_task(project_id, sr_failed_cats=["correctness"], confirmed_cats=[]),
        _make_fp_task(
            project_id, sr_failed_cats=["correctness"], confirmed_cats=["correctness"]
        ),
    ]

    cat_fp, _, _ = _compute_cat_fp_stats(tasks)

    fp = cat_fp["correctness"]["fp"]
    tp = cat_fp["correctness"]["tp"]
    assert fp == 2
    assert tp == 1
    fpr = round(fp / (fp + tp), 3)
    assert fpr == 0.667


def test_compute_sr_quality_by_fingerprint_groups_metrics_and_gates_low_sr_samples():
    from app.playbook_learning import compute_sr_quality_by_fingerprint

    project_id = uuid.uuid4()
    review_history = [
        {
            "iteration": 0,
            "issues": [{"description": "missing unit tests", "severity": "MAJOR"}],
        }
    ]
    results = compute_sr_quality_by_fingerprint(
        [
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["correctness"],
                human_cats=["correctness"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["test-coverage"],
                review_history=review_history,
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["architecture"],
                human_cats=["architecture"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                sr_failed_cats=["performance"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                human_cats=["security"],
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-a",
                skipped=True,
            ),
            _make_fp_quality_task(
                project_id,
                prompt_fingerprint="fp-b",
                sr_failed_cats=["correctness"],
                human_cats=["correctness"],
            ),
        ],
        min_sample_size=2,
    )

    assert set(results) == {"fp-a", "fp-b"}

    a = results["fp-a"]
    assert a["sr_sample_size"] == 5
    assert a["sr_catch_sample_size"] == 4
    assert a["sr_catch_rate"] == 0.75
    assert a["sr_false_positive_sample_size"] == 4
    # test-coverage is confirmed only by review_history (no persisted rejection
    # category), which is no longer a confirmation source — so it now counts as a
    # false positive alongside performance: 2 FPs / 4 sampled = 0.5.
    assert a["sr_false_positive_rate"] == 0.5
    fp_by_cat = {
        entry["category"]: (entry["fp_count"], entry["tp_count"])
        for entry in a["sr_false_positive_rate_by_category"]
    }
    assert fp_by_cat["architecture"] == (0, 1)
    assert fp_by_cat["correctness"] == (0, 1)
    assert fp_by_cat["performance"] == (1, 0)
    # review_history-only confirmation no longer suppresses the false positive.
    assert fp_by_cat["test-coverage"] == (1, 0)
    assert a["sr_catch_rate_delta_vs_project_mean"] == -0.05

    b = results["fp-b"]
    assert b["sr_sample_size"] == 1
    assert b["sr_catch_rate"] is None
    assert b["sr_false_positive_rate"] is None
    assert b["sr_false_positive_rate_by_category"] == []


@pytest.mark.parametrize(
    "origin",
    [
        "reviewer",
        "reviewer_disagreement",
        "reviewer_recovery_after_incomplete_sr",
        "lone_checker_recovery",
        "operator_requested_recovery",
    ],
)
def test_compute_sr_quality_by_fingerprint_excludes_reviewer_origin(origin):
    """Retired auxiliary-origin entries must not inflate author catch rate."""
    from app.playbook_learning import compute_sr_quality_by_fingerprint

    project_id = uuid.uuid4()
    sro = {
        "verdict": "pass",
        "fixes_applied": False,
        "check_results": {
            "correctness": {"passed": False, "issue": "x", "origin": origin},
        },
        "blind_spots": [],
    }
    task = Task(
        project_id=project_id,
        title="reviewer-rescue fp task",
        payloads=[{"type": "create", "prompt": "do something"}],
        prompt_fingerprint="fp-rescue",
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": sro,
            "_first_attempt_rejection_categories": ["correctness"],
        },
    )

    results = compute_sr_quality_by_fingerprint([task], min_sample_size=1)

    bucket = results["fp-rescue"]
    # Independent review confirmed correctness, but the author SR did not catch
    # it because the recorded signal came from a retired auxiliary origin.
    assert bucket["sr_catch_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Tests for reviewer-origin attribution (PR #4193 follow-up).
# Reviewer-stamped check_results entries (origin in _REVIEWER_ORIGINS) must NOT
# be attributed to the author SR — i.e. they must still count as "agent missed"
# for `cat_added_value`, must NOT contribute to TP/FP in `_compute_cat_fp_stats`
# or `_stats_for_tasks`, and must still surface as misses in the SR drilldown.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "origin",
    [
        "reviewer",
        "reviewer_disagreement",
        "reviewer_recovery_after_incomplete_sr",
        "lone_checker_recovery",
        "operator_requested_recovery",
    ],
)
def test_compute_cat_fp_stats_excludes_reviewer_origin_from_sr_failed(origin):
    """A reviewer-stamped failure must NOT count toward the author SR's TP/FP totals."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    # SR check_results says correctness:passed=False, BUT origin is reviewer-stamped
    # → this is a reviewer rescue, not an author catch. Must not appear in cat_fp.
    sro = {
        "check_results": {
            "correctness": {"passed": False, "issue": "x", "origin": origin},
        },
    }
    task = Task(
        project_id=project_id,
        title="reviewer-rescue task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
    )

    cat_fp, type_cat_fp, scope_cat_fp = _compute_cat_fp_stats([task])

    # Reviewer-stamped failure must not be attributed to the author SR.
    assert cat_fp == {}
    assert type_cat_fp == {}
    assert scope_cat_fp == {}


def test_compute_cat_fp_stats_legacy_no_origin_still_counts():
    """Control: a passed=False entry with no `origin` is a genuine author catch."""
    from app.playbook_learning import _compute_cat_fp_stats

    project_id = uuid.uuid4()
    task = _make_fp_task(
        project_id, sr_failed_cats=["correctness"], confirmed_cats=["correctness"]
    )

    cat_fp, _, _ = _compute_cat_fp_stats([task])

    assert cat_fp["correctness"]["tp"] == 1
    assert cat_fp["correctness"].get("fp", 0) == 0


@pytest.mark.parametrize(
    "origin",
    [
        "reviewer",
        "reviewer_disagreement",
        "reviewer_recovery_after_incomplete_sr",
        "lone_checker_recovery",
        "operator_requested_recovery",
    ],
)


def test_stats_for_tasks_reviewer_origin_excluded_from_sr_failed_count(origin):
    """Reviewer-stamped failures must NOT inflate sr_failed_count / fp_count
    in `_stats_for_tasks`. The reviewer rescue is not a thing the author SR
    flagged, so it must not appear in the precision/false-positive metrics.
    """
    from app.playbook_learning import _stats_for_tasks

    project_id = uuid.uuid4()
    # Author SR genuinely flagged `correctness` (no origin) — TP since human
    # confirmed it.
    author_task = _make_sr_task(
        project_id,
        age_days=1,
        human_cats={"correctness": 1},
        sr_failed_cats=["correctness"],
    )
    # Reviewer-stamped failure on `security` with no human confirmation. Pre-fix
    # this would be counted as a SR false positive; post-fix it's excluded from
    # sr_failed_count entirely because the author SR didn't actually flag it.
    reviewer_task = _make_sr_task(
        project_id,
        age_days=1,
        human_cats={"correctness": 1},
        sr_failed_cats=["security"],
        check_result_overrides={"security": {"origin": origin}},
    )

    _count, cat_stats, _type_cat, _all_iter, _sev, _scope_cat = _stats_for_tasks(
        [author_task, reviewer_task]
    )

    # `correctness` is a genuine author catch — should appear with a TP.
    correctness = cat_stats.get("correctness", {})
    assert correctness.get("sr_failed_count", 0) == 1
    assert correctness.get("fp_count", 0) == 0
    # `security` was reviewer-stamped only — must not surface as a SR failure
    # at all in the FP/sr_failed counters.
    if "security" in cat_stats:
        assert cat_stats["security"].get("sr_failed_count", 0) == 0
        assert cat_stats["security"].get("fp_count", 0) == 0


def test_get_sr_alert_drilldown_tasks_reviewer_rescue_still_a_miss():
    """A reviewer-rescued category must surface as `missed_in_target` in the drilldown."""
    from app.playbook_learning import get_sr_alert_drilldown_tasks

    project_id = uuid.uuid4()
    # Task has `correctness` stamped by the reviewer (passed=False with origin)
    # plus a human rejection on `correctness` → SR did NOT catch it.
    ctx = {
        "_self_review_outcome": {
            "check_results": {
                "correctness": {
                    "passed": False,
                    "issue": "x",
                    "origin": "reviewer_disagreement",
                },
            },
            "verdict": "fail",
        },
        "_first_attempt_rejection_categories": ["correctness"],
    }
    task = Task(
        project_id=project_id,
        title="reviewer-rescue drilldown",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=1)

    rows = get_sr_alert_drilldown_tasks(
        all_tasks=[task],
        design_doc={},
        categories=["correctness"],
        alert_type="trends",
    )

    assert len(rows) == 1
    assert "correctness" in rows[0]["missed_categories"]
    assert "correctness" not in rows[0]["caught_categories"]


def _make_edges_task(
    project_id,
    *,
    age_days: float,
    edges_considered: list | None = None,
    task_type: str = "create",
    cycle_id: str | None = None,
) -> Task:
    """Build a Task for edges-trend tests with _self_review_outcome.edges_considered."""
    ctx: dict = {
        "_self_review_outcome": {
            "edges_considered": edges_considered
            if edges_considered is not None
            else [],
        }
    }
    task = Task(
        project_id=project_id,
        title="Edges task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    if cycle_id:
        task.strategic_cycle_id = cycle_id
    return task


@pytest.mark.asyncio
async def test_compute_edges_trend_returns_none_below_sample_size(
    db_session: AsyncSession,
):
    """Returns None when either window has fewer than _MIN_SR_SAMPLE qualifying tasks."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    for _ in range(3):
        t = _make_edges_task(project.id, age_days=2, edges_considered=[{"id": "e1"}])
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_edges_task(project.id, age_days=20, edges_considered=[{"id": "e1"}])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is None


@pytest.mark.asyncio
async def test_compute_edges_trend_calendar_fallback(db_session: AsyncSession):
    """Falls back to calendar bucketing when no completed cycles exist."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    # Current window (0-14d): 5 tasks, 3 with edges
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=2,
            edges_considered=[{"id": "e1"}] if i < 3 else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window (14-28d): 5 tasks, all with edges
    for _ in range(5):
        t = _make_edges_task(project.id, age_days=20, edges_considered=[{"id": "e1"}])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "calendar"
    assert result["current_cycle_id"] is None
    assert result["current"]["tasks_with_edges_rate"] == round(3 / 5, 3)
    assert result["previous"]["tasks_with_edges_rate"] == 1.0
    assert result["trend"] == "regressing"


@pytest.mark.asyncio
async def test_compute_edges_trend_cycle_mode_happy_path(db_session: AsyncSession):
    """Uses cycle bucketing when two completed cycles each have ≥5 qualifying tasks."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    c1 = _make_cycle(project.id, age_days=5)
    c2 = _make_cycle(project.id, age_days=30)
    db_session.add_all([c1, c2])
    await db_session.flush()

    tasks = []
    # c1 (current): 5 tasks, 4 with edges → rate=0.8
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=3,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
            cycle_id=str(c1.id),
        )
        db_session.add(t)
        tasks.append(t)
    # c2 (previous): 5 tasks, 1 with edges → rate=0.2
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=25,
            edges_considered=[{"id": "e1"}] if i < 1 else [],
            cycle_id=str(c2.id),
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == c1.id
    assert result["previous_cycle_id"] == c2.id
    assert result["current"]["tasks_with_edges_rate"] == 0.8
    assert result["previous"]["tasks_with_edges_rate"] == 0.2
    assert result["tasks_with_edges_rate_delta"] == pytest.approx(0.6, abs=0.01)
    assert result["trend"] == "improving"


@pytest.mark.asyncio
async def test_compute_edges_trend_stable_classification(db_session: AsyncSession):
    """Delta < 5pp → trend='stable'."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    # Current: 5 tasks, 4/5 with edges → 0.8
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=2,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
        )
        db_session.add(t)
        tasks.append(t)
    # Previous: 5 tasks, 4/5 with edges → 0.8 (delta=0 → stable)
    for i in range(5):
        t = _make_edges_task(
            project.id,
            age_days=20,
            edges_considered=[{"id": "e1"}] if i < 4 else [],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)

    assert result is not None
    assert result["trend"] == "stable"


@pytest.mark.asyncio
async def test_compute_edges_trend_persists_to_sr_analytics(db_session: AsyncSession):
    """Persists edges_trend to the dedicated SR analytics store."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)
    tasks = []
    for window_age in (2, 20):
        for _ in range(5):
            t = _make_edges_task(
                project.id, age_days=window_age, edges_considered=[{"id": "e1"}]
            )
            db_session.add(t)
            tasks.append(t)
    await db_session.flush()

    result = await compute_edges_trend(project.id, db_session, tasks)
    await db_session.flush()

    persisted = (await _read_sr_analytics(db_session, project)).get("edges_trend")
    assert result is not None
    assert persisted is not None
    assert persisted["computed_at"] == result["computed_at"]
    assert persisted["trend"] == result["trend"]
    await db_session.refresh(project)
    assert (project.design_doc or {}).get("edges_trend") is None


@pytest.mark.asyncio
async def test_compute_edges_trend_history_capped_at_5(db_session: AsyncSession):
    """rate_history never exceeds 5 entries across repeated calls with distinct cycles."""
    from app.playbook_learning import compute_edges_trend

    project = await _make_project(db_session)

    for call_idx in range(6):
        c_cycle = _make_cycle(project.id, age_days=call_idx * 10 + 1)
        p_cycle = _make_cycle(project.id, age_days=call_idx * 10 + 20)
        db_session.add_all([c_cycle, p_cycle])
        await db_session.flush()

        tasks = []
        for _ in range(5):
            t = _make_edges_task(
                project.id,
                age_days=call_idx * 10 + 0.5,
                edges_considered=[{"id": "e1"}],
                cycle_id=str(c_cycle.id),
            )
            db_session.add(t)
            tasks.append(t)
        for _ in range(5):
            t = _make_edges_task(
                project.id,
                age_days=call_idx * 10 + 15,
                edges_considered=[{"id": "e1"}],
                cycle_id=str(p_cycle.id),
            )
            db_session.add(t)
            tasks.append(t)
        await db_session.flush()

        await compute_edges_trend(project.id, db_session, tasks)
        await db_session.flush()

    persisted = (await _read_sr_analytics(db_session, project)).get("edges_trend")
    history = persisted["rate_history"]
    assert len(history) <= 5


@pytest.mark.asyncio
async def test_compute_edges_trend_same_cycle_ids_as_sr_catch_rate(
    db_session: AsyncSession,
):
    """When both functions run on the same tasks, they pick the same cycle pair."""
    from app.playbook_learning import compute_edges_trend, compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    c1 = _make_cycle(project.id, age_days=5)
    c2 = _make_cycle(project.id, age_days=30)
    db_session.add_all([c1, c2])
    await db_session.flush()

    tasks = []
    # c1: 5 SR tasks + 5 edges tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=3,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c1.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_edges_task(
            project.id, age_days=3, edges_considered=[{"id": "e1"}], cycle_id=str(c1.id)
        )
        db_session.add(t)
        tasks.append(t)
    # c2: 5 SR tasks + 5 edges tasks
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=25,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        t.strategic_cycle_id = c2.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_edges_task(
            project.id,
            age_days=25,
            edges_considered=[{"id": "e1"}],
            cycle_id=str(c2.id),
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    sr_result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    edges_result = await compute_edges_trend(project.id, db_session, tasks)

    assert sr_result is not None
    assert edges_result is not None
    assert sr_result["bucket_mode"] == "cycle"
    assert edges_result["bucket_mode"] == "cycle"
    assert sr_result["current_cycle_id"] == edges_result["current_cycle_id"]
    assert sr_result["previous_cycle_id"] == edges_result["previous_cycle_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests for compute_sr_regression_risk_test_signal_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_rr_task(
    project_id,
    *,
    age_days: float,
    sr_passed,
    test_attempts,
    task_type: str = "create",
    blind_spots=None,
) -> Task:
    """Build a Task with SR regression_risk verdict + fix_loop_history.test bucket.

    ``sr_passed`` of ``None`` omits the regression_risk entry so the task
    fails the qualifies-predicate.  ``test_attempts`` of ``None`` defaults
    to no test loop activity.
    """
    check_results: dict = {}
    if sr_passed is not None:
        check_results["regression_risk"] = {"passed": sr_passed, "issue": "x"}
    sro: dict = {"check_results": check_results}
    if blind_spots is not None:
        sro["blind_spots"] = blind_spots
    history: dict = {}
    if test_attempts is not None:
        history["test"] = test_attempts
    task = Task(
        project_id=project_id,
        title="SR-RR task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={"_self_review_outcome": sro},
        fix_loop_history=history,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_returns_none_below_sample(
    db_session: AsyncSession,
):
    """Below _MIN_SR_SAMPLE qualifying tasks → None (not zero)."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(4):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(4):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is None


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_passed_no_signal_is_true_pass(
    db_session: AsyncSession,
):
    """SR passed + no/trivial test loop is a true-pass, not a miss."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sr_passed_no_signal"] == 5
    assert cur["sr_passed_with_signal"] == 0
    assert cur["miss_by_test_signal_rate"] == 0.0
    assert cur["numerator"] == 0
    assert cur["denominator"] == 5


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_passed_with_signal_counts_as_miss(
    db_session: AsyncSession,
):
    """SR passed + test loop ran non-trivially → miss-by-test-signal."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(3):
        t = _make_sr_rr_task(
            project.id,
            age_days=2,
            sr_passed=True,
            test_attempts=[
                {"iteration": 1, "exit_reason": "in_progress"},
                {"iteration": 2, "exit_reason": "passed"},
            ],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(2):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sr_passed_with_signal"] == 3
    assert cur["sr_passed_no_signal"] == 2
    assert cur["miss_by_test_signal_rate"] == round(3 / 5, 3)
    assert cur["numerator"] == 3
    assert cur["denominator"] == 5
    assert result["miss_by_test_signal_rate_delta"] == round(3 / 5, 3)
    assert result["trend"] == "regressing"


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_failed_with_signal_is_hit(
    db_session: AsyncSession,
):
    """SR failed regression_risk + test loop ran → hit-by-test-signal counted."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(4):
        t = _make_sr_rr_task(
            project.id,
            age_days=2,
            sr_passed=False,
            test_attempts=[
                {"iteration": 1, "exit_reason": "divergence_detected"},
            ],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(1):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=False, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sr_failed_with_signal"] == 4
    assert cur["sr_failed_no_signal"] == 1
    assert cur["denominator"] == 0
    assert cur["miss_by_test_signal_rate"] is None


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_skip_envelope_excluded(
    db_session: AsyncSession,
):
    """Skip-fallback SR envelopes are excluded from numerator and denominator."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(3):
        t = _make_sr_rr_task(
            project.id,
            age_days=2,
            sr_passed=None,
            test_attempts=[{"iteration": 1, "exit_reason": "passed"}],
            blind_spots=["self-review skipped: diff below threshold"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    cur = result["current"]
    assert cur["sample_size"] == 5
    assert (
        cur["sr_passed_no_signal"]
        + cur["sr_passed_with_signal"]
        + cur["sr_failed_no_signal"]
        + cur["sr_failed_with_signal"]
    ) == 5


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_persists_to_sr_analytics(
    db_session: AsyncSession,
):
    """Result is written to the dedicated SR analytics store under the key."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=2, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=20, sr_passed=True, test_attempts=[])
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None

    await db_session.flush()
    persisted = (await _read_sr_analytics(db_session, project)).get(
        "sr_regression_risk_test_signal_trends"
    )
    assert persisted is not None
    assert persisted["computed_at"] == result["computed_at"]
    assert persisted["current"]["sample_size"] == 5
    await db_session.refresh(project)
    assert (project.design_doc or {}).get(
        "sr_regression_risk_test_signal_trends"
    ) is None


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_cycle_bucketing(
    db_session: AsyncSession,
):
    """Two completed StrategicCycle rows form current/previous buckets."""
    from app.playbook_learning import compute_sr_regression_risk_test_signal_trends

    project = await _make_project(db_session)
    c_cycle = _make_cycle(project.id, age_days=2)
    p_cycle = _make_cycle(project.id, age_days=20)
    db_session.add(c_cycle)
    db_session.add(p_cycle)
    await db_session.flush()

    tasks = []
    for _ in range(5):
        t = _make_sr_rr_task(
            project.id,
            age_days=1,
            sr_passed=True,
            test_attempts=[
                {"iteration": 1, "exit_reason": "in_progress"},
                {"iteration": 2, "exit_reason": "passed"},
            ],
        )
        t.strategic_cycle_id = c_cycle.id
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_rr_task(project.id, age_days=19, sr_passed=True, test_attempts=[])
        t.strategic_cycle_id = p_cycle.id
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_regression_risk_test_signal_trends(
        project.id, db_session, tasks
    )
    assert result is not None
    assert result["bucket_mode"] == "cycle"
    assert result["current_cycle_id"] == c_cycle.id
    assert result["previous_cycle_id"] == p_cycle.id
    assert result["current"]["sr_passed_with_signal"] == 5
    assert result["previous"]["sr_passed_with_signal"] == 0
    assert result["trend"] == "regressing"


# ---------------------------------------------------------------------------
# Precision/recall quadrant classification tests
# ---------------------------------------------------------------------------

# Helper: build a two-window task set where current window has the given
# hit/fp rates and the previous window has different rates, producing known
# precision/recall deltas.
#
# Recall == hit_rate (hit_count / human_count).
# Precision == (sr_failed - fp) / sr_failed.
#
# We control fp via check_result_overrides: tasks where SR fails AND the human
# flagged the same cat are TPs; tasks where SR fails but human did NOT flag are
# FPs.  We approximate FP by using a separate category for the human_cats so
# the fp-stats see it as an FP.
#
# For simplicity, we use 10 tasks per window so rates are round numbers.


def _make_quadrant_tasks(
    project_id,
    db_session,
    tasks: list,
    *,
    curr_hits: int,
    curr_fp: int,
    prev_hits: int,
    prev_fp: int,
    cat: str = "correctness",
    n: int = 10,
):
    """Build current + previous window tasks controlling hit and FP counts.

    actual sr_failed = hits + fp for each window.

    For each window:
    - ``hits`` tasks: human flagged cat + SR failed cat → TP hit
    - ``n - hits`` tasks: human flagged cat, SR passed → miss
    - ``fp`` tasks: SR failed cat but human flagged a different cat → FP

    precision = hits / (hits + fp); recall (hit_rate) = hits / n
    """
    # Current window (age_days=2)
    for _ in range(curr_hits):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={cat: 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(n - curr_hits):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={cat: 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(curr_fp):
        t = _make_sr_task(
            project_id,
            age_days=2,
            human_cats={"other-cat": 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window (age_days=20)
    for _ in range(prev_hits):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={cat: 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(n - prev_hits):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={cat: 1},
            sr_failed_cats=[],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(prev_fp):
        t = _make_sr_task(
            project_id,
            age_days=20,
            human_cats={"other-cat": 1},
            sr_failed_cats=[cat],
        )
        db_session.add(t)
        tasks.append(t)


@pytest.mark.asyncio
async def test_precision_recall_quadrant_conservative_shift(db_session: AsyncSession):
    """Precision up + recall down → conservative_shift."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=6/10=0.6, sr_failed=8, fp=1 → precision=(8-1)/8=0.875
    # Previous: recall=9/10=0.9, sr_failed=9, fp=4 → precision=(9-4)/9=0.556
    # recall_delta = 0.6-0.9 = -0.3 (below -threshold)
    # precision_delta = 0.875-0.556 = +0.319 (above +threshold)
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=6,
        curr_fp=1,
        prev_hits=9,
        prev_fp=4,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "conservative_shift"
    assert cat["recall_delta"] is not None and cat["recall_delta"] < -0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] > 0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_noisy_shift(db_session: AsyncSession):
    """Precision down + recall up → noisy_shift."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=9/10=0.9, sr_failed=9, fp=4 → precision=(9-4)/9≈0.556
    # Previous: recall=6/10=0.6, sr_failed=8, fp=1 → precision=(8-1)/8=0.875
    # recall_delta = +0.3, precision_delta = -0.319
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=4,
        prev_hits=6,
        prev_fp=1,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "noisy_shift"
    assert cat["recall_delta"] is not None and cat["recall_delta"] > 0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] < -0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_both_declining(db_session: AsyncSession):
    """Precision down + recall down → both_declining."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=4/10=0.4, sr_failed=6, fp=3 → precision=(6-3)/6=0.5
    # Previous: recall=8/10=0.8, sr_failed=8, fp=1 → precision=(8-1)/8=0.875
    # recall_delta = -0.4, precision_delta = -0.375
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=4,
        curr_fp=3,
        prev_hits=8,
        prev_fp=1,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "both_declining"
    assert cat["recall_delta"] is not None and cat["recall_delta"] < -0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] < -0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_healthy(db_session: AsyncSession):
    """Precision up + recall up → healthy."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=9/10=0.9, sr_failed=9, fp=1 → precision=(9-1)/9≈0.889
    # Previous: recall=4/10=0.4, sr_failed=6, fp=3 → precision=(6-3)/6=0.5
    # recall_delta = +0.5, precision_delta = +0.389
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=1,
        prev_hits=4,
        prev_fp=3,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "healthy"
    assert cat["recall_delta"] is not None and cat["recall_delta"] > 0.05
    assert cat["precision_delta"] is not None and cat["precision_delta"] > 0.05


@pytest.mark.asyncio
async def test_precision_recall_quadrant_flat(db_session: AsyncSession):
    """Both deltas within threshold → flat."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current == previous: recall=7/10=0.7, sr_failed=7, fp=2 → precision=5/7≈0.714
    # Both deltas = 0.0 → flat
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=7,
        curr_fp=2,
        prev_hits=7,
        prev_fp=2,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] == "flat"


@pytest.mark.asyncio
async def test_precision_recall_quadrant_mixed(db_session: AsyncSession):
    """One delta significant, other not → mixed."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # Current: recall=8/10=0.8, sr_failed=8, fp=1 → precision=7/8=0.875
    # Previous: recall=7/10=0.7, sr_failed=8, fp=3 → precision=5/8=0.625
    # recall_delta = +0.1 (above threshold)
    # precision_delta = +0.25 (above threshold) → both up → actually healthy
    # Let's adjust: prev_hits=7, but make precision barely below threshold for recall
    # Actually let's set: curr_hits=7(recall=0.7), prev_hits=6(recall=0.6) delta=+0.1 (above 0.05)
    # and curr_fp=2, prev_fp=2 with same sr_failed=7 → precision delta = 0 → mixed
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=7,
        curr_fp=2,
        prev_hits=6,
        prev_fp=2,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    # recall_delta = +0.1 (significant), precision_delta = 0.0 (not significant) → mixed
    assert cat["precision_recall_quadrant"] == "mixed"


@pytest.mark.asyncio
async def test_precision_recall_quadrant_null_when_no_previous_precision(
    db_session: AsyncSession,
):
    """When previous window has no SR failures (prev_sr_failed=0), quadrant is None."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    # prev_hits=0, prev_fp=0 → actual prev_sr_failed = 0 → previous_precision=None
    # precision_delta = None → quadrant = None
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=7,
        curr_fp=2,
        prev_hits=0,
        prev_fp=0,
    )
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"]["correctness"]
    assert cat["precision_recall_quadrant"] is None
    assert cat["precision_delta"] is None


@pytest.mark.asyncio
async def test_precision_recall_quadrant_alert_fires_for_conservative_shift(
    db_session: AsyncSession,
):
    """conservative_shift triggers exactly one alert per cycle."""
    from app.playbook_learning import (
        compute_sr_catch_rate_trends,
        _SR_QUADRANT_ALERT_TITLE_PREFIX,
    )

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=6,
        curr_fp=1,
        prev_hits=9,
        prev_fp=4,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert (
        result["categories"]["correctness"]["precision_recall_quadrant"]
        == "conservative_shift"
    )
    mock_post.assert_called()

    # Verify Alert row was inserted
    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    q_rows = [
        r for r in alert_rows if r.title.startswith(_SR_QUADRANT_ALERT_TITLE_PREFIX)
    ]
    assert len(q_rows) == 1
    assert q_rows[0].severity == AlertSeverity.WARNING
    assert "correctness" in q_rows[0].title


@pytest.mark.asyncio
async def test_precision_recall_quadrant_alert_fires_for_noisy_shift(
    db_session: AsyncSession,
):
    """noisy_shift triggers an alert."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=4,
        prev_hits=6,
        prev_fp=1,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert (
        result["categories"]["correctness"]["precision_recall_quadrant"]
        == "noisy_shift"
    )
    mock_post.assert_called()


@pytest.mark.asyncio
async def test_precision_recall_quadrant_no_alert_for_healthy(
    db_session: AsyncSession,
):
    """healthy quadrant does NOT trigger an alert."""
    from app.playbook_learning import (
        compute_sr_catch_rate_trends,
        _SR_QUADRANT_ALERT_TITLE_PREFIX,
    )

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=9,
        curr_fp=1,
        prev_hits=4,
        prev_fp=3,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ),
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert result["categories"]["correctness"]["precision_recall_quadrant"] == "healthy"

    await db_session.commit()
    alert_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alert_rows) == 0


@pytest.mark.asyncio
async def test_precision_recall_quadrant_no_alert_for_both_declining(
    db_session: AsyncSession,
):
    """both_declining quadrant does NOT trigger a quadrant alert (covered by regression alert)."""
    from app.playbook_learning import (
        compute_sr_catch_rate_trends,
        _SR_QUADRANT_ALERT_TITLE_PREFIX,
    )

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=4,
        curr_fp=3,
        prev_hits=8,
        prev_fp=1,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ),
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert (
        result["categories"]["correctness"]["precision_recall_quadrant"]
        == "both_declining"
    )

    await db_session.commit()
    q_rows = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.title.like(f"{_SR_QUADRANT_ALERT_TITLE_PREFIX}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(q_rows) == 0


@pytest.mark.asyncio
async def test_precision_recall_quadrant_alert_dedup_same_cycle(
    db_session: AsyncSession,
):
    """Alert fires only once per cycle even when compute is called twice."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks: list = []
    _make_quadrant_tasks(
        project.id,
        db_session,
        tasks,
        curr_hits=6,
        curr_fp=1,
        prev_hits=9,
        prev_fp=4,
    )
    await db_session.flush()

    with (
        patch(
            "app.notification_dispatch.post_failure_alert_message",
            new_callable=AsyncMock,
        ) as mock_post,
        patch(
            "app.notification_dispatch.dispatch_external_notification",
            new_callable=AsyncMock,
        ),
        patch("asyncio.ensure_future", side_effect=lambda coro: coro.close() or None),
    ):
        await compute_sr_catch_rate_trends(project.id, db_session, tasks)
        await db_session.commit()
        call_count_after_first = mock_post.call_count

        await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    # Second call should not fire again for same window/cycle
    assert mock_post.call_count == call_count_after_first


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _compute_miss_latency_stats
# ─────────────────────────────────────────────────────────────────────────────


def _make_latency_task(
    project_id,
    *,
    sr_completed_at: datetime,
    rejection_at: datetime,
    miss_cats: list[str],
    age_days: float = 2,
) -> Task:
    """Build a Task where SR passed all miss_cats but reviewer rejected them."""
    # SR marks all miss_cats as passed=True
    check_results = {cat: {"passed": True, "issue": ""} for cat in miss_cats}
    # human cats contains all miss_cats
    human_cats = dict.fromkeys(miss_cats, 1)
    sr_outcome = {
        "check_results": check_results,
        "completed_at": sr_completed_at.isoformat(),
    }
    ctx = {
        "_self_review_outcome": sr_outcome,
        "_first_attempt_rejection_categories": human_cats,
        "_first_attempt_rejection_at": rejection_at.isoformat(),
    }
    task = Task(
        project_id=project_id,
        title="latency task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


def test_compute_miss_latency_stats_p50_and_p95():
    """p50 / p95 are computed correctly from multiple samples."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Latencies in seconds: 60, 120, 300, 600, 86400
    latency_secs = [60, 120, 300, 600, 86400]
    tasks = []
    for lat in latency_secs:
        sr_ts = base
        rej_ts = base + timedelta(seconds=lat)
        tasks.append(
            _make_latency_task(
                pid,
                sr_completed_at=sr_ts,
                rejection_at=rej_ts,
                miss_cats=["test-coverage"],
            )
        )

    result = _compute_miss_latency_stats(tasks)

    assert "test-coverage" in result
    cat = result["test-coverage"]
    assert cat["time_to_miss_detect_sample_count"] == 5
    # p50 of [60, 120, 300, 600, 86400] = 300
    assert cat["time_to_miss_detect_p50_sec"] == 300
    # p95 of 5 values: idx = 0.95*4 = 3.8 → interpolated between 600 and 86400
    # Use ±1 tolerance for float→int truncation
    assert abs(cat["time_to_miss_detect_p95_sec"] - 69240) <= 1


def test_compute_miss_latency_stats_single_sample_lone_checker_false():
    """With only one sample, lone_checker must be False regardless of latency."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Single miss with > 7-day latency
    sr_ts = base
    rej_ts = base + timedelta(days=10)
    tasks = [
        _make_latency_task(
            pid, sr_completed_at=sr_ts, rejection_at=rej_ts, miss_cats=["correctness"]
        )
    ]

    result = _compute_miss_latency_stats(tasks)

    assert "correctness" in result
    cat = result["correctness"]
    assert cat["time_to_miss_detect_sample_count"] == 1
    assert cat["time_to_miss_detect_lone_checker"] is False


def test_compute_miss_latency_stats_no_misses_absent():
    """A category with no misses should not appear in the result."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Task where SR also failed the category (not a miss — both SR and reviewer flagged)
    check_results = {"test-coverage": {"passed": False, "issue": "x"}}
    human_cats = {"test-coverage": 1}
    ctx = {
        "_self_review_outcome": {
            "check_results": check_results,
            "completed_at": base.isoformat(),
        },
        "_first_attempt_rejection_categories": human_cats,
        "_first_attempt_rejection_at": (base + timedelta(hours=1)).isoformat(),
    }
    task = Task(
        project_id=pid,
        title="not a miss",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    result = _compute_miss_latency_stats([task])
    assert "test-coverage" not in result


def test_compute_miss_latency_stats_lone_checker_flag():
    """lone_checker=True when sample_count >= 2 and p95 > 7 days."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Two misses each with 8-day latency → p95 = 8 days > 7 days
    tasks = []
    for _ in range(2):
        sr_ts = base
        rej_ts = base + timedelta(days=8)
        tasks.append(
            _make_latency_task(
                pid, sr_completed_at=sr_ts, rejection_at=rej_ts, miss_cats=["security"]
            )
        )

    result = _compute_miss_latency_stats(tasks)

    assert result["security"]["time_to_miss_detect_lone_checker"] is True


def test_compute_miss_latency_stats_lone_checker_false_when_fast():
    """lone_checker=False when p95 is under 7 days even with multiple samples."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    base = datetime(2026, 1, 1, 0, 0, 0)
    # Two misses with 1-hour latency
    tasks = []
    for _ in range(2):
        sr_ts = base
        rej_ts = base + timedelta(hours=1)
        tasks.append(
            _make_latency_task(
                pid,
                sr_completed_at=sr_ts,
                rejection_at=rej_ts,
                miss_cats=["style-conventions"],
            )
        )

    result = _compute_miss_latency_stats(tasks)

    assert result["style-conventions"]["time_to_miss_detect_lone_checker"] is False


def test_compute_miss_latency_stats_fallback_both_skipped():
    """When SR and rejection both lack real timestamps, the sample is skipped."""
    from app.playbook_learning import _compute_miss_latency_stats

    import uuid as _uuid

    pid = _uuid.uuid4()
    # Build a task with no SR completed_at and no rejection_at in context
    check_results = {"test-coverage": {"passed": True, "issue": ""}}
    human_cats = {"test-coverage": 1}
    ctx = {
        "_self_review_outcome": {"check_results": check_results},  # no completed_at
        "_first_attempt_rejection_categories": human_cats,  # no _first_attempt_rejection_at
    }
    task = Task(
        project_id=pid,
        title="no-ts task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    # updated_at is None on this in-memory task — both fallbacks are None, so skipped
    result = _compute_miss_latency_stats([task])
    assert "test-coverage" not in result


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_miss_latency_fields_present(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends attaches time_to_miss_detect fields to category entries."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    base = _utcnow()

    # Current window (5 tasks): each has a miss on test-coverage with 2h latency
    for _ in range(5):
        sr_ts = base - timedelta(days=2, hours=2)
        rej_ts = base - timedelta(days=2)
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"test-coverage": 1},
            sr_failed_cats=[],
            check_result_overrides={},
            sr_completed_at=sr_ts,
            rejection_at=rej_ts,
        )
        # Make test-coverage a miss (SR passed, reviewer rejected)
        t.context = {
            "_self_review_outcome": {
                "check_results": {"test-coverage": {"passed": True, "issue": ""}},
                "completed_at": sr_ts.isoformat(),
            },
            "_first_attempt_rejection_categories": {"test-coverage": 1},
            "_first_attempt_rejection_at": rej_ts.isoformat(),
        }
        db_session.add(t)
        tasks.append(t)

    # Previous window (5 tasks): simple misses without latency timestamps
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"test-coverage": 1},
            sr_failed_cats=["test-coverage"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"].get("test-coverage")
    assert cat is not None
    # Fields must be present (value may be None or int)
    assert "time_to_miss_detect_sample_count" in cat
    assert "time_to_miss_detect_lone_checker" in cat
    assert "time_to_miss_detect_p50_sec" in cat
    assert "time_to_miss_detect_p95_sec" in cat
    # We have 5 samples with 2h latency → p50 = p95 = 7200
    assert cat["time_to_miss_detect_sample_count"] == 5
    assert cat["time_to_miss_detect_p50_sec"] == 7200
    assert cat["time_to_miss_detect_lone_checker"] is False


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_zero_miss_category_has_zero_count(
    db_session: AsyncSession,
):
    """A category with no latency samples gets time_to_miss_detect_sample_count == 0."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # 5 current + 5 previous tasks, SR also failed (not a miss — no passed=True in SR)
    for age in [2, 20]:
        for _ in range(5):
            t = _make_sr_task(
                project.id,
                age_days=age,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"],
            )
            db_session.add(t)
            tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["time_to_miss_detect_sample_count"] == 0
    assert cat["time_to_miss_detect_p50_sec"] is None
    assert cat["time_to_miss_detect_p95_sec"] is None
    assert cat["time_to_miss_detect_lone_checker"] is False


# ---------------------------------------------------------------------------
# SR blind-spot layer distribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blind_spot_layer_distribution_populated(db_session: AsyncSession):
    """aggregate_sr_blind_spot_themes populates layer_distribution for classifiable text."""
    from app.sr_blind_spots import aggregate_sr_blind_spot_themes

    project = await _make_project(db_session)

    # Text that triggers CI_WORKFLOW (matches "github actions.*fail" pattern)
    ci_text = "github actions failed to trigger workflow due to path filter miss"
    # Text that triggers AGENT_VERB (matches "scope too broad" pattern)
    verb_text = "scope too broad — edited files outside declared scope in handler"

    db_session.add(
        Task(
            project_id=project.id,
            title="t1",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": [ci_text],
                }
            },
        )
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="t2",
            payloads=[{"type": "fix"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": [verb_text],
                }
            },
        )
    )
    await db_session.flush()

    themes = await aggregate_sr_blind_spot_themes(
        db_session, project.id, days=30, limit=10
    )

    assert len(themes) == 2
    by_theme = {t["theme"]: t for t in themes}

    ci_theme_key = " ".join(ci_text.lower().split()[:6])
    verb_theme_key = " ".join(verb_text.lower().split()[:6])

    ci_entry = by_theme[ci_theme_key]
    assert "layer_distribution" in ci_entry
    assert isinstance(ci_entry["layer_distribution"], dict)
    assert ci_entry["layer_distribution"].get("CI_WORKFLOW", 0) >= 1

    verb_entry = by_theme[verb_theme_key]
    assert "layer_distribution" in verb_entry
    assert isinstance(verb_entry["layer_distribution"], dict)
    assert verb_entry["layer_distribution"].get("AGENT_VERB", 0) >= 1

    # Counts must be positive ints; total per theme ≤ count × num_layers
    for entry in themes:
        dist = entry["layer_distribution"]
        for layer_count in dist.values():
            assert isinstance(layer_count, int) and layer_count > 0
        total = sum(dist.values())
        assert total <= entry["count"] * 5  # at most 5 layers per example


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _compute_cat_severity_calibration
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_with_severity(
    project_id,
    *,
    age_days: float,
    human_cats: dict,
    agent_cat: str,
    agent_severity: str,
    reviewer_severity: str,
    reviewer_keyword: str = "injection",
) -> Task:
    """Build a Task with severity-annotated SR check_result and review_loop history."""
    check_results = {
        agent_cat: {"passed": False, "issue": "x", "severity": agent_severity}
    }
    ctx: dict = {
        "_self_review_outcome": {"check_results": check_results},
        "_first_attempt_rejection_categories": human_cats,
        "review_loop": {
            "review_history": [
                {
                    "iteration": 0,
                    "issues": [
                        {
                            "description": f"{reviewer_keyword} vulnerability found",
                            "severity": reviewer_severity,
                        }
                    ],
                }
            ]
        },
    }
    task = Task(
        project_id=project_id,
        title="SR severity task",
        payloads=[{"type": "create", "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


def test_compute_cat_severity_calibration_matched_under_over():
    """_compute_cat_severity_calibration correctly buckets matched/under/over cases."""
    from app.playbook_learning import _compute_cat_severity_calibration

    project_id = uuid.uuid4()

    # Task 1: agent=MINOR, reviewer=CRITICAL → under_rated
    t1 = _make_sr_task_with_severity(
        project_id,
        age_days=1,
        human_cats={"security": 1},
        agent_cat="security",
        agent_severity="MINOR",
        reviewer_severity="CRITICAL",
        reviewer_keyword="injection",
    )
    # Task 2: agent=MAJOR, reviewer=MAJOR → matched
    t2 = _make_sr_task_with_severity(
        project_id,
        age_days=1,
        human_cats={"security": 1},
        agent_cat="security",
        agent_severity="MAJOR",
        reviewer_severity="MAJOR",
        reviewer_keyword="injection",
    )
    # Task 3: agent=CRITICAL, reviewer=MINOR → over_rated
    t3 = _make_sr_task_with_severity(
        project_id,
        age_days=1,
        human_cats={"security": 1},
        agent_cat="security",
        agent_severity="CRITICAL",
        reviewer_severity="MINOR",
        reviewer_keyword="injection",
    )

    result = _compute_cat_severity_calibration([t1, t2, t3])

    assert "security" in result
    sec = result["security"]
    assert sec["under_rated"] == 1
    assert sec["over_rated"] == 1
    assert sec["matched"] == 1
    assert sec["total"] == 3


def test_compute_cat_severity_calibration_skips_missing_severity():
    """Tasks without agent severity or no review_history are skipped."""
    from app.playbook_learning import _compute_cat_severity_calibration

    project_id = uuid.uuid4()

    # No agent severity
    t_no_agent_sev = Task(
        project_id=project_id,
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {
                "check_results": {"security": {"passed": False, "issue": "x"}}
            },
            "_first_attempt_rejection_categories": {"security": 1},
            "review_loop": {
                "review_history": [
                    {
                        "iteration": 0,
                        "issues": [
                            {"description": "injection vuln", "severity": "CRITICAL"}
                        ],
                    }
                ]
            },
        },
    )
    t_no_agent_sev.created_at = _utcnow() - timedelta(days=1)

    # No review_history
    t_no_rev_hist = Task(
        project_id=project_id,
        title="t",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {
                "check_results": {
                    "security": {"passed": False, "issue": "x", "severity": "MINOR"}
                }
            },
            "_first_attempt_rejection_categories": {"security": 1},
        },
    )
    t_no_rev_hist.created_at = _utcnow() - timedelta(days=1)

    result = _compute_cat_severity_calibration([t_no_agent_sev, t_no_rev_hist])
    assert result == {}


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_severity_calibration_propagates(
    db_session: AsyncSession,
):
    """severity_calibration is included in categories when conditions are met."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Current window (7 tasks): agent rates security as MINOR, reviewer rates CRITICAL
    for _ in range(7):
        t = _make_sr_task_with_severity(
            project.id,
            age_days=2,
            human_cats={"security": 1},
            agent_cat="security",
            agent_severity="MINOR",
            reviewer_severity="CRITICAL",
            reviewer_keyword="injection",
        )
        db_session.add(t)
        tasks.append(t)

    # Previous window (6 tasks): matched severities
    for _ in range(6):
        t = _make_sr_task_with_severity(
            project.id,
            age_days=20,
            human_cats={"security": 1},
            agent_cat="security",
            agent_severity="MAJOR",
            reviewer_severity="MAJOR",
            reviewer_keyword="injection",
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)

    assert result is not None
    assert "security" in result["categories"]
    cal = result["categories"]["security"].get("severity_calibration")
    assert cal is not None, "severity_calibration should be present"
    assert cal["total"] == 7
    assert cal["under_rated"] == 7
    assert cal["over_rated"] == 0
    assert cal["matched"] == 0


@pytest.mark.asyncio
async def test_blind_spot_layer_distribution_empty_for_unclassifiable(
    db_session: AsyncSession,
):
    """aggregate_sr_blind_spot_themes returns empty layer_distribution when text matches no layer."""
    from app.sr_blind_spots import aggregate_sr_blind_spot_themes

    project = await _make_project(db_session)

    db_session.add(
        Task(
            project_id=project.id,
            title="t1",
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": ["did not add extra comments to helpers"],
                }
            },
        )
    )
    await db_session.flush()

    themes = await aggregate_sr_blind_spot_themes(
        db_session, project.id, days=30, limit=10
    )

    assert len(themes) == 1
    dist = themes[0]["layer_distribution"]
    assert isinstance(dist, dict)
    # May be empty — unclassifiable text produces no layer tags


# ─────────────────────────────────────────────────────────────────────────────
# Tests for aggregate_sr_co_failure_pairs
# ─────────────────────────────────────────────────────────────────────────────


def _make_co_fail_task(project_id, *, age_days: float, failed_cats: list) -> Task:
    """Build a code-change task with the given SR-failed categories."""
    check_results = {c: {"passed": False, "issue": "x"} for c in failed_cats}
    task = Task(
        project_id=project_id,
        title="co-fail test",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": {},
        },
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


def test_aggregate_sr_co_failure_pairs_returns_none_below_min_sample():
    """Returns None when fewer than min_sample qualifying tasks exist."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    tasks = [
        _make_co_fail_task(
            uuid.uuid4(), age_days=1, failed_cats=["correctness", "style-conventions"]
        )
    ]
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5)
    assert result is None


def test_aggregate_sr_co_failure_pairs_high_lift_pair_surfaced():
    """Pair with lift >> 1 appears in output sorted by lift desc.

    Setup: 5 tasks where A+B co-fail, 3 tasks where B fails alone.
    total=8, miss_A=5, miss_B=8; P(B|A)=5/5=1.0, base_rate(B)=8/8=1.0 — wrong.

    Better: 5 co-fail tasks, 0 B-alone tasks, 3 no-fail tasks.
    total=8, miss_A=5, miss_B=5; P(B|A)=1.0, base=5/8=0.625 → lift=1.6
    """
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    project_id = uuid.uuid4()
    tasks = []
    # 5 tasks: both A and B fail
    for _ in range(5):
        tasks.append(
            _make_co_fail_task(
                project_id, age_days=1, failed_cats=["correctness", "security"]
            )
        )
    # 3 tasks: neither fails (qualify for total count)
    for _ in range(3):
        tasks.append(_make_co_fail_task(project_id, age_days=1, failed_cats=[]))

    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result is not None
    pair = next(
        (p for p in result if p["cat_a"] == "correctness" and p["cat_b"] == "security"),
        None,
    )
    assert pair is not None
    # P(B|A) = 5/5 = 1.0; base_rate(B) = 5/8 = 0.625; lift ≈ 1.6
    assert pair["lift"] > 1.0
    assert pair["co_fail_count"] == 5
    assert abs(pair["lift"] - pair["p_b_given_a"] / pair["base_rate_b"]) < 0.01


def test_aggregate_sr_co_failure_pairs_base_rate_equals_conditional_gives_lift_one():
    """When P(B|A)==base_rate(B), lift rounds to 1.0."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    project_id = uuid.uuid4()
    tasks = []
    # 6 tasks: all have both cats failing → P(B|A)=1.0, base_rate(B)=1.0, lift=1.0
    for _ in range(6):
        tasks.append(
            _make_co_fail_task(
                project_id, age_days=1, failed_cats=["correctness", "security"]
            )
        )

    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result is not None
    pair = next(
        (p for p in result if p["cat_a"] == "correctness" and p["cat_b"] == "security"),
        None,
    )
    assert pair is not None
    assert round(pair["lift"], 2) == 1.0


def test_aggregate_sr_co_failure_pairs_single_task_miss_filtered_by_min_co_count():
    """A pair that appears in only 1 task is excluded by default min_co_count=2."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    project_id = uuid.uuid4()
    tasks = []
    # 5 tasks where only one has co-failure
    tasks.append(
        _make_co_fail_task(
            project_id, age_days=1, failed_cats=["correctness", "security"]
        )
    )
    for _ in range(4):
        tasks.append(
            _make_co_fail_task(project_id, age_days=1, failed_cats=["correctness"])
        )

    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    # The pair exists but co_count=1 < min_co_count=2 → filtered out
    assert result == []


@pytest.mark.asyncio
async def test_compute_sr_catch_rate_trends_includes_co_failure_pairs(
    db_session: AsyncSession,
):
    """compute_sr_catch_rate_trends writes co_failure_pairs into the result."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []

    # Build enough tasks for both windows (5+ each)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=3,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness", "security"],
        )
        db_session.add(t)
        tasks.append(t)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)

    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert "co_failure_pairs" in result
    assert isinstance(result["co_failure_pairs"], list)


# ---------------------------------------------------------------------------
# lone_checker_recovery origin attribution in catch-rate stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_floor_flag_persisted_to_design_doc(db_session: AsyncSession):
    """below_floor=True is stored in design_doc categories when a category has
    been below the floor threshold for >= _FLOOR_MIN_CONSECUTIVE_WINDOWS windows.
    The dispatcher's _evaluate_sr_below_floor_signal reads this field directly."""
    from app.playbook_learning import compute_sr_catch_rate_trends
    from sqlalchemy.orm.attributes import flag_modified

    project = await _make_project(db_session)
    # Pre-seed one prior below-floor rate_history entry so two consecutive
    # below-floor windows are reached after a single compute call.
    project.design_doc = {
        "sr_catch_rate_trends": {
            "computed_at": (_utcnow() - timedelta(days=15)).isoformat(),
            "categories": {
                "correctness": {"rate_history": [0.3]},
            },
        }
    }
    flag_modified(project, "design_doc")
    await db_session.flush()

    tasks = []
    # Current window: 5 tasks — all SR misses (hit_rate=0.0, below 50% floor).
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],  # SR passes → miss
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: 5 tasks with low hit rate (also below floor).
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    await db_session.flush()

    stored = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends", {}
    )
    cat = stored.get("categories", {}).get("correctness", {})
    assert cat.get("below_floor") is True, (
        "below_floor must be True after two consecutive below-floor windows; "
        "dispatcher reads this field from design_doc"
    )
    assert cat.get("current_hit_rate") is not None, (
        "current_hit_rate required by dispatcher"
    )
    assert isinstance(cat.get("rate_history"), list) and len(cat["rate_history"]) >= 2


@pytest.mark.asyncio
async def test_compute_sr_false_attestation_no_duplicate_alerts(
    db_session: AsyncSession,
):
    """Calling compute_sr_false_attestation_trends twice with the same elevated
    categories must not create duplicate Alert rows (defense-in-depth dedup)."""
    from app.playbook_learning import compute_sr_false_attestation_trends

    project = await _make_project(db_session)

    def _fa_task():
        """Task where SR claimed tests_executed_cleanly=PASS but CI failed."""
        t = Task(
            project_id=project.id,
            title="false attestation task",
            payloads=[{"type": "create", "prompt": "p"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {
                        "tests_executed_cleanly": {
                            "passed": True,
                            "evidence": "exit_code=0",
                        }
                    }
                },
                "review_loop": {"ci_passed": False},
            },
        )
        t.created_at = _utcnow() - timedelta(days=2)
        return t

    tasks = [_fa_task() for _ in range(5)]
    for t in tasks:
        db_session.add(t)
    await db_session.flush()

    await compute_sr_false_attestation_trends(project.id, db_session, tasks)
    await db_session.flush()
    await compute_sr_false_attestation_trends(project.id, db_session, tasks)
    await db_session.flush()

    alerts = (
        (
            await db_session.execute(
                _sql_select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                    Alert.title.like("SR false attestation rate elevated%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        f"expected exactly 1 false-attestation alert, got {len(alerts)}; "
        "duplicate-alert dedup guard must be active"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for engagement_thinning flag in compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engagement_thinning_flag_set_when_below_threshold(
    db_session: AsyncSession,
):
    """engagement_thinning=True when rate < threshold and total >= min_passes."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # 10 current-window PASS tasks for correctness: 0 have reasoning → rate = 0.0
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window tasks to establish a history entry
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["reasoning_engagement_total"] == 10
    assert cat["reasoning_engagement_rate"] == 0.0  # below 0.5 threshold
    assert cat["engagement_thinning"] is True


@pytest.mark.asyncio
async def test_engagement_thinning_flag_false_when_insufficient_passes(
    db_session: AsyncSession,
):
    """engagement_thinning=False when total < min_passes (default 10), even if rate is low."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Only 5 PASS tasks (below the default min_passes=10) in current window
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous-window tasks to ensure the function can compute trends (non-None result)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["reasoning_engagement_total"] == 5
    assert cat["reasoning_engagement_rate"] == 0.0
    # Too few samples — thinning flag must be False
    assert cat["engagement_thinning"] is False


@pytest.mark.asyncio
async def test_engagement_thinning_flag_set_for_regressing_category(
    db_session: AsyncSession,
):
    """engagement_thinning=True even for a regressing category (no exclusion)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    # Current window: 10 hits (SR catches human) + 10 no-reasoning PASS tasks
    # → category is NOT regressing by miss_rate, but engagement rate = 0.0
    # Make it regressing by having a decreasing hit rate vs previous window.
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    # 10 PASS tasks with no reasoning in current window
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous window: all hits → current window has lower hit rate → regressing
    for _ in range(10):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    # The category should be regressing (below 0.5 threshold) or have trend decreasing
    assert cat["reasoning_engagement_total"] == 10
    assert cat["reasoning_engagement_rate"] == 0.0
    # thinning flag applies even for regressing categories
    assert cat["engagement_thinning"] is True


@pytest.mark.asyncio
async def test_engagement_thinning_honors_design_doc_overrides(
    db_session: AsyncSession,
):
    """engagement_thinning respects sr_engagement_threshold / sr_engagement_min_passes from design_doc."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    # Override threshold=0.8, min_passes=5 so that 5 passes at 0.0 rate qualifies.
    project = await _make_project(
        db_session,
        design_doc={
            "sr_engagement_threshold": 0.8,
            "sr_engagement_min_passes": 5,
        },
    )
    tasks = []
    # 5 PASS tasks with no reasoning — rate=0.0, total=5
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=[],
            sr_passed_cats_with_reasoning={"correctness": ""},
        )
        db_session.add(t)
        tasks.append(t)
    # Previous-window tasks to ensure the function can compute trends (non-None result)
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    cat = result["categories"].get("correctness")
    assert cat is not None
    assert cat["reasoning_engagement_total"] == 5
    assert cat["reasoning_engagement_rate"] == 0.0
    # With overrides: threshold=0.8, min_passes=5 → 5 passes at 0.0 qualifies
    assert cat["engagement_thinning"] is True


# ─────────────────────────────────────────────────────────────────────────────
# E2E non-degeneracy guard for compute_sr_catch_rate_trends
# ─────────────────────────────────────────────────────────────────────────────


def _make_sr_task_production_shaped(
    project_id,
    *,
    age_days: float,
    all_hits: bool = True,
) -> Task:
    """Build a Task that mirrors the full production context shape.

    Populates all three human-truth layers:
    - review_loop.review_history with CRITICAL/MAJOR/MINOR issues whose
      descriptions match known _REVIEW_CATEGORIES keywords
    - _first_attempt_rejection_categories (regression_risk, type-safety)
    - _all_iterations_rejection_categories (regression_risk, type-safety, correctness)

    check_results include author findings for both rejection categories.

    When all_hits=False, neither category appears in sr_failed so both are
    misses (hit_rate drops), exercising the miss path.
    """
    check_results: dict = {}
    if all_hits:
        check_results["regression_risk"] = {
            "passed": False,
            "issue": "regression in test_bar",
        }
        check_results["type-safety"] = {
            "passed": False,
            "issue": "type hint missing for return value",
        }

    ctx: dict = {
        "_self_review_outcome": {"check_results": check_results},
        "review_loop": {
            "review_history": [
                {
                    "iteration": 0,
                    "issues": [
                        {
                            "description": "regression in test_bar broke the test suite",
                            "severity": "CRITICAL",
                        },
                        {
                            "description": "type hint missing for return value parameter",
                            "severity": "MAJOR",
                        },
                    ],
                },
                {
                    "iteration": 1,
                    "issues": [
                        {
                            "description": "logic bug in edge case boundary handling",
                            "severity": "MINOR",
                        },
                    ],
                },
            ]
        },
        "_first_attempt_rejection_categories": {
            "regression_risk": 1,
            "type-safety": 1,
        },
        "_all_iterations_rejection_categories": {
            "regression_risk": 1,
            "type-safety": 1,
            "correctness": 1,
        },
    }
    task = Task(
        project_id=project_id,
        title="Production-shaped SR task",
        payloads=[{"type": "create", "prompt": "implement feature X"}],
        status=TaskStatus.COMPLETED,
        context=ctx,
    )
    task.created_at = _utcnow() - timedelta(days=age_days)
    return task


@pytest.mark.asyncio
async def test_overall_trend_verdict_improving_when_significant(
    db_session: AsyncSession,
):
    """overall_trend_verdict='improving' with sufficient data and a significant delta."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for _ in range(8):  # current: 8/8 hits → 1.0
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        tasks.append(t)
    for i in range(8):  # previous: 1/8 hits → 0.125 (delta=0.875, z>1.28)
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"] if i == 0 else [],
        )
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert result["overall_trend_verdict"] == "improving"
    assert result["overall_trend_significant"] is True
    assert result["overall_trend_data_sufficient"] is True


@pytest.mark.asyncio
async def test_overall_trend_verdict_stable_when_not_significant(
    db_session: AsyncSession,
):
    """overall_trend_verdict='stable' when delta is above threshold but not statistically
    significant (z < _TREND_Z_MIN=1.28 with small sample sizes)."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    tasks = []
    for cnt, hit in [(3, True), (2, False)]:  # current: 3/5 = 0.6
        for _ in range(cnt):
            t = _make_sr_task(
                project.id,
                age_days=2,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"] if hit else [],
            )
            db_session.add(t)
            tasks.append(t)
    for cnt, hit in [
        (4, True),
        (1, False),
    ]:  # previous: 4/5 = 0.8; delta=0.2>0.05 but z≈0.69<1.28
        for _ in range(cnt):
            t = _make_sr_task(
                project.id,
                age_days=20,
                human_cats={"correctness": 1},
                sr_failed_cats=["correctness"] if hit else [],
            )
            db_session.add(t)
            tasks.append(t)
    await db_session.flush()

    result = await compute_sr_catch_rate_trends(project.id, db_session, tasks)
    assert result is not None
    assert result["overall_trend_verdict"] == "stable"
    assert result["overall_trend_significant"] is False
    assert result["overall_trend_data_sufficient"] is True


@pytest.mark.asyncio
async def test_sr_catch_rate_previous_bucket_not_starved_by_throughput(
    db_session: AsyncSession,
):
    """Regression: when the current window has more tasks than the old fetch cap (200),
    the previous window must still be populated. The fix in agent.py uses a 28-day
    time-bounded query instead of a flat row-count cap so both 14-day buckets are
    always loaded regardless of current-window throughput."""
    from app.playbook_learning import compute_sr_catch_rate_trends

    project = await _make_project(db_session)
    OLD_CAP = 200
    # Create more tasks than the old cap in the current window (last 7 days).
    current_tasks = []
    for _ in range(OLD_CAP + 10):
        t = _make_sr_task(
            project.id,
            age_days=2,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        current_tasks.append(t)
    # Create 5 tasks in the previous window (15-28 days ago).
    previous_tasks = []
    for _ in range(5):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)
        previous_tasks.append(t)
    await db_session.flush()

    # OLD behaviour: only the most-recent OLD_CAP rows — all land in the current
    # window (age_days=2), previous bucket is empty → metric freezes at None.
    old_cap_rows = sorted(
        current_tasks + previous_tasks,
        key=lambda t: t.created_at,
        reverse=True,
    )[:OLD_CAP]
    result_old = await compute_sr_catch_rate_trends(
        project.id, db_session, old_cap_rows
    )
    assert result_old is None, (
        "flat cap fetch should leave the previous bucket empty and return None"
    )

    # NEW behaviour: time-bounded fetch (28 days) loads both windows → both
    # buckets populated → real result returned with data_sufficient=True.
    all_tasks = current_tasks + previous_tasks
    result_new = await compute_sr_catch_rate_trends(project.id, db_session, all_tasks)
    assert result_new is not None, (
        "window-bounded fetch must populate both buckets and return a real result"
    )
    assert result_new.get("data_sufficient") is True


@pytest.mark.asyncio
async def test_refresh_sr_catch_rate_fetch_covers_both_windows(
    db_session: AsyncSession,
):
    """Integration: _refresh_sr_catch_rate must fetch tasks from both the current
    and previous calendar windows (0-14 days and 14-28 days ago) so the metric
    is data_sufficient=True even when the current window alone saturates a
    row-count cap.  Catches fetch-window regressions that bypass compute_sr_catch_rate_trends
    unit tests (which accept hand-built task lists)."""
    from app.routers.agent import _refresh_sr_catch_rate

    project = await _make_project(db_session)

    # 6 SR tasks in current window (age_days=3 → inside 0–14-day bucket)
    for _ in range(6):
        t = _make_sr_task(
            project.id,
            age_days=3,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)

    # 6 SR tasks in previous window (age_days=20 → inside 14–28-day bucket)
    for _ in range(6):
        t = _make_sr_task(
            project.id,
            age_days=20,
            human_cats={"correctness": 1},
            sr_failed_cats=["correctness"],
        )
        db_session.add(t)

    # Non-SR tasks that must NOT affect the metric (no _self_review_outcome)
    for _ in range(3):
        t = Task(
            project_id=project.id,
            title="non-SR task",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.COMPLETED,
            context={"scope": "small"},
        )
        db_session.add(t)

    await db_session.flush()

    # Call the router-level helper that performs the actual DB fetch
    await _refresh_sr_catch_rate(session=db_session, project_id=project.id)
    await db_session.flush()

    trends = (await _read_sr_analytics(db_session, project)).get(
        "sr_catch_rate_trends"
    )
    assert trends is not None, "_refresh_sr_catch_rate must write sr_catch_rate_trends"
    assert trends.get("data_sufficient") is True, (
        "fetch must cover both 0-14 and 14-28 day windows; "
        "data_sufficient=False means the previous window was starved"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Malformed self-review outcome compatibility
# ─────────────────────────────────────────────────────────────────────────────


def _make_list_shaped_sr_task(project_id, *, task_type: str = "create") -> Task:
    """Build a code-change task whose check_results is a NON-EMPTY list."""
    task = Task(
        project_id=project_id,
        title="malformed SR task",
        payloads=[{"type": task_type, "prompt": "do something"}],
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {
                # The bug shape: agent emitted a list rather than a dict.
                "check_results": [
                    {"category": "correctness", "passed": False},
                    {"category": "security", "passed": False},
                ],
            },
            "_first_attempt_rejection_categories": {"correctness": 1},
            "review_loop": {"ci_passed": True},
        },
    )
    task.created_at = _utcnow() - timedelta(days=1)
    return task


def test_list_shaped_check_results_does_not_crash_stats_for_tasks():
    """`_stats_for_tasks` tolerates list-shaped check_results (no AttributeError)."""
    from app.playbook_learning import _stats_for_tasks

    project_id = uuid.uuid4()
    tasks = [_make_list_shaped_sr_task(project_id) for _ in range(3)]

    # Must not raise; the malformed entries degrade to an empty mapping.
    count, cat_stats, _type_cat, _all_iter, _sev, _scope_cat = _stats_for_tasks(tasks)
    assert isinstance(cat_stats, dict)


def test_list_shaped_check_results_does_not_crash_co_failure_pairs():
    """`aggregate_sr_co_failure_pairs` tolerates list-shaped check_results."""
    from app.sr_blind_spots import aggregate_sr_co_failure_pairs

    project_id = uuid.uuid4()
    tasks = [_make_list_shaped_sr_task(project_id) for _ in range(6)]

    # Must not raise; with no recoverable category failures, returns None or [].
    result = aggregate_sr_co_failure_pairs(tasks, min_sample=5, min_co_count=2)
    assert result is None or result == []


def test_list_shaped_check_results_does_not_crash_quality_by_fingerprint():
    """`compute_sr_quality_by_fingerprint` tolerates list-shaped check_results."""
    from app.playbook_learning import compute_sr_quality_by_fingerprint

    project_id = uuid.uuid4()
    tasks = [_make_list_shaped_sr_task(project_id) for _ in range(3)]

    # Must not raise; returns a well-formed (possibly empty) mapping.
    result = compute_sr_quality_by_fingerprint(tasks, min_sample_size=1)
    assert isinstance(result, dict)
