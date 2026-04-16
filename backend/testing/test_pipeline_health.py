"""Pipeline-health tests.

The original block tests `compute_pipeline_health_summary` against a fully
mocked AsyncSession — those need no DB and are imported via conftest.
The pipeline-event monitor block at the bottom exercises the in-memory
rolling buffer in `app.shared_state` plus the /api/health/detailed endpoint.
Run with: bash scripts/test-backend.sh testing/test_pipeline_health.py
"""
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.admin_analytics import compute_pipeline_health_summary
from app.models import Task, TaskStatus, Project
from testing.factories import make_task, make_project, _utcnow


def _make_mock_session(tasks=None, project=None):
    """Build a mock AsyncSession whose execute() returns a given task list."""
    session = AsyncMock()

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = tasks or []

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    session.execute = AsyncMock(return_value=result_mock)
    session.get = AsyncMock(return_value=project)
    return session


# ── Empty project ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_project_returns_sensible_defaults():
    """Empty project (no tasks, no directions) → null success_rate, empty distribution."""
    project = make_project(directions=[])
    session = _make_mock_session(tasks=[], project=project)
    project_id = uuid.uuid4()

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] is None
    assert result["trend"] is None
    assert result["failure_distribution"] == {}
    assert result["direction_stats"] == []


# ── Success rate ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_success_rate_computed_correctly():
    """3 completed + 1 failed → success_rate = 0.75."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=1)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=2)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=3)),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=4),
                  latest_status_message="ImportError: missing module"),
    ]
    # status_log is empty so _derive_completed_at falls back to created_at (within window)
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] == 0.75


@pytest.mark.asyncio
async def test_success_rate_all_completed():
    """All completed → success_rate = 1.0."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=1)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=2)),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_success_rate_all_failed():
    """All failed → success_rate = 0.0."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=1),
                  latest_status_message="timeout"),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["success_rate"] == 0.0


# ── Failure distribution ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failure_distribution_uses_classify_failure_categories():
    """Failure distribution maps to the correct classify_failure classes."""
    now = _utcnow()
    project_id = uuid.uuid4()

    tasks = [
        # CODE_ERROR (default)
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=1),
                  latest_status_message="SyntaxError: invalid syntax"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=2),
                  latest_status_message="AttributeError: NoneType has no attribute"),
        # PROVISION_FAILURE
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=3),
                  latest_status_message="gcp quota exceeded for zone us-central1"),
        # RATE_LIMIT
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=4),
                  latest_status_message="API Error: Rate limit reached"),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    dist = result["failure_distribution"]
    assert dist.get("CODE_ERROR") == 2
    assert dist.get("PROVISION_FAILURE") == 1
    assert dist.get("RATE_LIMIT") == 1


@pytest.mark.asyncio
async def test_failure_distribution_top_3_only():
    """Failure distribution is capped at top 3 classes by count."""
    now = _utcnow()
    project_id = uuid.uuid4()

    # 4 distinct failure classes — only top 3 should appear
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=1),
                  latest_status_message="SyntaxError"),  # CODE_ERROR x3
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=2),
                  latest_status_message="ImportError"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=3),
                  latest_status_message="NameError"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=4),
                  latest_status_message="gcp quota exceeded"),  # PROVISION_FAILURE x2
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=5),
                  latest_status_message="vm startup failed network"),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=6),
                  latest_status_message="API Error: Rate limit reached"),  # RATE_LIMIT x1 (should be cut)
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=7),
                  latest_status_message="heartbeat lost timeout oom"),  # AGENT_TIMEOUT x1 (should be cut)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    dist = result["failure_distribution"]
    assert len(dist) == 3
    assert "CODE_ERROR" in dist
    assert "PROVISION_FAILURE" in dist


# ── Trend ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trend_improving_when_success_rate_increased():
    """success_rate 0.5 → 0.8 (delta +0.3 > 0.05) → trend = improving."""
    now = _utcnow()
    project_id = uuid.uuid4()

    # Previous window (24–48h ago): 1 completed, 1 failed → 50%
    prev_completed = make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                                created_at=now - timedelta(hours=36))
    prev_failed = make_task(project_id=project_id, status=TaskStatus.FAILED,
                             created_at=now - timedelta(hours=36),
                             latest_status_message="error")
    # Current window (0–24h ago): 4 completed, 1 failed → 80%
    curr_tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=5)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=6)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=7)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=8)),
        make_task(project_id=project_id, status=TaskStatus.FAILED, created_at=now - timedelta(hours=9),
                  latest_status_message="error"),
    ]
    tasks = [prev_completed, prev_failed] + curr_tasks
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["trend"] == "improving"


@pytest.mark.asyncio
async def test_trend_none_when_no_previous_window_data():
    """No tasks in previous window → trend is None (can't compare)."""
    now = _utcnow()
    project_id = uuid.uuid4()

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED, created_at=now - timedelta(hours=2)),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["trend"] is None


# ── Direction stats ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_direction_stats_merge_rate():
    """Direction with 2 research cycles and 1 PR merged → merge_rate = 0.5."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Improve test coverage",
        "possibly_saturated": False,
        "research_history": [
            {"timestamp": (now - timedelta(hours=5)).isoformat(), "prs_merged": 1},
        ],
    }])

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=4)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=6)),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["direction_id"] == direction_id
    assert stats[0]["cycles"] == 2
    assert stats[0]["merge_rate"] == 0.5
    assert stats[0]["possibly_saturated"] is False


@pytest.mark.asyncio
async def test_direction_with_zero_cycles_excluded():
    """Active direction with no completed research cycles in window is excluded from stats."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Direction with no cycles",
        "possibly_saturated": False,
        "research_history": [],
    }])

    # Task exists but is FAILED, not COMPLETED
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=4),
                  latest_status_message="error"),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["direction_stats"] == []


# ── Dominant dropout stage ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dominant_dropout_stage_surfaces_highest_count():
    """dominant_dropout_stage picks the stage_dropout key with the highest total."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Stage dropout direction",
        "possibly_saturated": False,
        "research_history": [
            {
                "timestamp": (now - timedelta(hours=5)).isoformat(),
                "prs_merged": 1,
                "tasks_created": 4,
                "stage_dropout": {
                    "tasks_failed_code_error": 2,
                    "tasks_failed_infra": 1,
                    "tasks_required_approval": 0,
                    "tasks_merged_staging_only": 0,
                },
            }
        ],
    }])

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=4)),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["dominant_dropout_stage"] == "tasks_failed_code_error"


@pytest.mark.asyncio
async def test_dominant_dropout_stage_none_when_no_stage_dropout_data():
    """dominant_dropout_stage is None when research_history has no stage_dropout entries."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "No dropout data direction",
        "possibly_saturated": False,
        "research_history": [
            {
                "timestamp": (now - timedelta(hours=5)).isoformat(),
                "prs_merged": 1,
                "tasks_created": 2,
            }
        ],
    }])

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=4)),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["dominant_dropout_stage"] is None


@pytest.mark.asyncio
async def test_dominant_dropout_stage_none_when_all_zeros():
    """dominant_dropout_stage is None when stage_dropout is present but all counts are 0."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Perfect cycle direction",
        "possibly_saturated": False,
        "research_history": [
            {
                "timestamp": (now - timedelta(hours=5)).isoformat(),
                "prs_merged": 2,
                "tasks_created": 2,
                "stage_dropout": {
                    "tasks_failed_code_error": 0,
                    "tasks_failed_infra": 0,
                    "tasks_required_approval": 0,
                    "tasks_merged_staging_only": 0,
                },
            }
        ],
    }])

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "research"}],
                  context={"direction_id": direction_id},
                  created_at=now - timedelta(hours=4)),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["dominant_dropout_stage"] is None, (
        f"Expected None for all-zero stage_dropout, got {stats[0]['dominant_dropout_stage']}"
    )


# ── Return structure ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_return_structure_has_all_required_keys():
    """Result always contains all required keys regardless of data."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert set(result.keys()) == {"success_rate", "trend", "failure_distribution", "direction_stats", "first_attempt_success_rate", "pipeline_conversion_rate", "review_accuracy"}
    assert "first_attempt_by_review_category" in result["first_attempt_success_rate"]


# ── Review accuracy ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_accuracy_none_when_fewer_than_5_reviews():
    """review_accuracy is None when fewer than 5 review tasks exist."""
    project_id = uuid.uuid4()
    now = _utcnow()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"blueprint_review_verdict": "APPROVE"},
            created_at=now - timedelta(hours=1),
        )
        for _ in range(4)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["review_accuracy"] is None


@pytest.mark.asyncio
async def test_review_accuracy_computes_precision_and_recall():
    """review_accuracy computes precision and recall when >= 5 review tasks exist."""
    project_id = uuid.uuid4()
    now = _utcnow()
    # 6 reviews: 5 APPROVE (1 overridden, 1 dismissed), 1 REQUEST_CHANGES (1 miss)
    tasks = [
        # Plain approve — correct
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={"blueprint_review_verdict": "APPROVE"},
                  created_at=now - timedelta(hours=1)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={"blueprint_review_verdict": "APPROVE"},
                  created_at=now - timedelta(hours=1)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={"blueprint_review_verdict": "APPROVE"},
                  created_at=now - timedelta(hours=1)),
        # Approve overridden by user (too lenient)
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={"blueprint_review_verdict": "APPROVE", "user_overrode_review": True},
                  created_at=now - timedelta(hours=1)),
        # Review dismissed
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={"blueprint_review_verdict": "REQUEST_CHANGES", "review_dismissed": True},
                  created_at=now - timedelta(hours=1)),
        # Approve but user caught an issue Blueprint missed (recall hit)
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={"blueprint_review_verdict": "APPROVE", "user_caught_issues": True},
                  created_at=now - timedelta(hours=1)),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    ra = result["review_accuracy"]
    assert ra is not None
    assert ra["total_reviews"] == 6
    assert ra["overrides"] == 1
    assert ra["dismissals"] == 1
    assert ra["misses"] == 1
    assert ra["total_approvals"] == 5  # 3 plain + 1 overridden + 1 user_caught (all APPROVE verdicts)
    # precision = 1 - (1 override + 1 dismissal) / 6 = 1 - 2/6 = 0.67
    assert ra["precision"] == round(1 - 2 / 6, 2)
    # recall = 1 - 1 miss / 5 approvals = 0.8
    assert ra["recall"] == round(1 - 1 / 5, 2)


@pytest.mark.asyncio
async def test_review_accuracy_skips_tasks_without_verdict():
    """Tasks without blueprint_review_verdict are excluded from accuracy computation."""
    project_id = uuid.uuid4()
    now = _utcnow()
    # 5 tasks with verdict + 2 without — only 5 should count
    tasks_with_verdict = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={"blueprint_review_verdict": "APPROVE"},
                  created_at=now - timedelta(hours=1))
        for _ in range(5)
    ]
    tasks_without_verdict = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  payloads=[{"type": "review"}],
                  context={},
                  created_at=now - timedelta(hours=1))
        for _ in range(2)
    ]
    session = _make_mock_session(
        tasks=tasks_with_verdict + tasks_without_verdict,
        project=make_project(directions=[]),
    )

    result = await compute_pipeline_health_summary(session, project_id)

    ra = result["review_accuracy"]
    assert ra is not None
    assert ra["total_reviews"] == 5


# ── Pipeline-health rolling event monitor ────────────────────────────────────

import app.shared_state as _ss  # noqa: E402
from app.shared_state import (  # noqa: E402
    PIPELINE_ALERT_DEDUP_SECONDS,
    PIPELINE_ALERT_THRESHOLDS,
    count_pipeline_events,
    get_pipeline_health_snapshot,
    record_pipeline_event,
)


@pytest.fixture()
def clean_pipeline_events():
    """Reset the pipeline event buffer + alert dedup state around each test."""
    buf = _ss.deploy_monitoring["pipeline_events"]
    last_ts = _ss.deploy_monitoring["pipeline_alert_last_ts"]
    saved_events = list(buf)
    saved_last_ts = dict(last_ts)
    buf.clear()
    last_ts.clear()
    yield
    buf.clear()
    last_ts.clear()
    buf.extend(saved_events)
    last_ts.update(saved_last_ts)


def test_below_threshold_does_not_fire_alert(clean_pipeline_events, mocker):
    """Recording fewer events than the threshold leaves alerts_active empty
    and never invokes the post helper."""
    spy = mocker.patch("app.shared_state._post_pipeline_alert")
    threshold = PIPELINE_ALERT_THRESHOLDS["heartbeat_timeout_post_claim"]

    for i in range(threshold - 1):
        record_pipeline_event(
            "heartbeat_timeout_post_claim",
            task_id=f"t{i}",
            vm_id=f"vm{i}",
            zone="us-central1-a",
        )

    snapshot = get_pipeline_health_snapshot()
    assert snapshot["counts_1h"]["heartbeat_timeout_post_claim"] == threshold - 1
    assert "heartbeat_timeout_post_claim" not in snapshot["alerts_active"]
    spy.assert_not_called()


def test_threshold_cross_fires_alert_exactly_once(clean_pipeline_events, mocker):
    """Crossing threshold by recording exactly N events fires the alert
    once for the per-category threshold."""
    spy = mocker.patch("app.shared_state._post_pipeline_alert")
    threshold = PIPELINE_ALERT_THRESHOLDS["account_blacklisted"]

    for i in range(threshold):
        record_pipeline_event(
            "account_blacklisted",
            task_id=f"t{i}",
            account_id=f"acct-{i}",
        )

    blacklist_calls = [c for c in spy.call_args_list if c.args[0] == "account_blacklisted"]
    assert len(blacklist_calls) == 1
    assert blacklist_calls[0].args == ("account_blacklisted", threshold, threshold)
    snapshot = get_pipeline_health_snapshot()
    assert "account_blacklisted" in snapshot["alerts_active"]


def test_threshold_cross_twice_within_dedup_window_fires_once(clean_pipeline_events, mocker):
    """Hitting threshold a second time inside the dedup window must NOT
    trigger a second alert."""
    spy = mocker.patch("app.shared_state._post_pipeline_alert")
    threshold = PIPELINE_ALERT_THRESHOLDS["provision_timeout_pre_claim"]

    for i in range(threshold + 5):
        record_pipeline_event(
            "provision_timeout_pre_claim",
            task_id=f"t{i}",
            vm_id=f"vm{i}",
            zone="us-central1-a",
        )

    pre_calls = [c for c in spy.call_args_list if c.args[0] == "provision_timeout_pre_claim"]
    assert len(pre_calls) == 1


def test_alert_fires_again_after_dedup_window_elapses(clean_pipeline_events, mocker):
    """Forcing the dedup last-ts older than PIPELINE_ALERT_DEDUP_SECONDS lets
    the next over-threshold event trigger a fresh alert."""
    spy = mocker.patch("app.shared_state._post_pipeline_alert")
    threshold = PIPELINE_ALERT_THRESHOLDS["account_blacklisted"]

    for i in range(threshold):
        record_pipeline_event("account_blacklisted", task_id=f"t{i}", account_id=f"a{i}")
    pre_calls = [c for c in spy.call_args_list if c.args[0] == "account_blacklisted"]
    assert len(pre_calls) == 1

    _ss.deploy_monitoring["pipeline_alert_last_ts"]["account_blacklisted"] = (
        datetime.utcnow() - timedelta(seconds=PIPELINE_ALERT_DEDUP_SECONDS + 60)
    )

    record_pipeline_event("account_blacklisted", task_id="t-new", account_id="a-new")

    post_calls = [c for c in spy.call_args_list if c.args[0] == "account_blacklisted"]
    assert len(post_calls) == 2


def test_events_older_than_one_hour_excluded_from_counts(clean_pipeline_events):
    """Stale events older than the rolling 1h window must not be counted."""
    stale_ts = datetime.utcnow() - timedelta(hours=2)
    for i in range(20):
        _ss.deploy_monitoring["pipeline_events"].append({
            "type": "heartbeat_timeout_post_claim",
            "ts": stale_ts,
            "task_id": f"old-{i}",
            "vm_id": f"vm-{i}",
            "zone": None,
            "account_id": None,
        })
    record_pipeline_event(
        "heartbeat_timeout_post_claim",
        task_id="fresh",
        vm_id="vm-fresh",
    )

    assert count_pipeline_events("heartbeat_timeout_post_claim") == 1
    snapshot = get_pipeline_health_snapshot()
    assert snapshot["counts_1h"]["heartbeat_timeout_post_claim"] == 1
    assert "heartbeat_timeout_post_claim" not in snapshot["alerts_active"]


def test_reap_rate_aggregate_threshold_fires_alert(clean_pipeline_events, mocker):
    """Mixed reap reasons that individually stay below their per-category
    threshold should still trip the aggregate reap_rate_per_hour alert."""
    spy = mocker.patch("app.shared_state._post_pipeline_alert")
    threshold = PIPELINE_ALERT_THRESHOLDS["reap_rate_per_hour"]

    reasons = ["never_started", "absolute_timeout", "heartbeat_timeout_post_claim"]
    for i in range(threshold):
        record_pipeline_event(reasons[i % len(reasons)], task_id=f"t{i}")

    rate_calls = [c for c in spy.call_args_list if c.args[0] == "reap_rate_per_hour"]
    assert len(rate_calls) == 1
    snapshot = get_pipeline_health_snapshot()
    assert snapshot["counts_1h"]["reap_rate_per_hour"] >= threshold
    assert "reap_rate_per_hour" in snapshot["alerts_active"]


def test_unknown_event_type_dropped(clean_pipeline_events):
    """Typo/unknown event_type must not be appended to the buffer."""
    record_pipeline_event("not_a_real_event", task_id="t1")
    assert len(_ss.deploy_monitoring["pipeline_events"]) == 0


def test_snapshot_shape_matches_contract(clean_pipeline_events):
    """get_pipeline_health_snapshot returns the documented keys regardless
    of whether any events were recorded."""
    snapshot = get_pipeline_health_snapshot()
    assert set(snapshot.keys()) == {"counts_1h", "last_event_ts", "thresholds", "alerts_active", "held_directions"}
    for et in PIPELINE_ALERT_THRESHOLDS:
        assert et in snapshot["thresholds"]
    assert snapshot["alerts_active"] == []


@pytest.mark.asyncio
async def test_health_detailed_endpoint_includes_pipeline_health(
    client, clean_pipeline_events
):
    """/api/health/detailed exposes the pipeline_health block at top level
    and inside components, with the documented shape."""
    record_pipeline_event("never_started", task_id="t1")
    record_pipeline_event("heartbeat_timeout_post_claim", task_id="t2", vm_id="vm2")

    resp = await client.get("/api/health/detailed")
    assert resp.status_code == 200
    data = resp.json()

    assert "pipeline_health" in data
    ph = data["pipeline_health"]
    assert set(ph.keys()) == {"counts_1h", "last_event_ts", "thresholds", "alerts_active", "held_directions"}
    assert ph["counts_1h"]["never_started"] == 1
    assert ph["counts_1h"]["heartbeat_timeout_post_claim"] == 1

    assert "pipeline_health" in data["components"]
    assert data["components"]["pipeline_health"]["status"] == "ok"
