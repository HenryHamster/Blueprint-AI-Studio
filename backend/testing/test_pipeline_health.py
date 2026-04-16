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

from app.routers.admin_analytics import compute_pipeline_health_summary, _compute_scope_baselines, _compute_medium_scope_measurement
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

    assert set(result.keys()) == {"success_rate", "trend", "failure_distribution", "direction_stats", "first_attempt_success_rate", "pipeline_conversion_rate", "self_review_effectiveness", "review_accuracy", "persistent_review_issues", "review_effectiveness_trends", "review_recurrence_by_category", "scope_merge_rates", "medium_scope_review_iterations"}
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


# ── Review effectiveness trends ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_effectiveness_trends_none_when_no_design_doc():
    """review_effectiveness_trends is None when project has no design_doc."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert result["review_effectiveness_trends"] is None


@pytest.mark.asyncio
async def test_review_effectiveness_trends_none_when_no_review_effectiveness():
    """review_effectiveness_trends is None when design_doc has no review_effectiveness key."""
    project = make_project(directions=[], design_doc={"other_key": "value"})
    session = _make_mock_session(tasks=[], project=project)

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert result["review_effectiveness_trends"] is None


@pytest.mark.asyncio
async def test_review_effectiveness_trends_returned_when_present():
    """review_effectiveness_trends exposes per-category data from design_doc."""
    review_effectiveness = {
        "computed_at": "2026-04-01T00:00:00",
        "persistent_issues": ["style"],
        "categories": {
            "style": {
                "current_rate": 0.35,
                "previous_rate": 0.30,
                "trend": "increasing",
                "current_sample_size": 20,
                "previous_sample_size": 18,
                "rate_history": [0.28, 0.30, 0.35],
            },
            "logic": {
                "current_rate": 0.10,
                "previous_rate": 0.15,
                "trend": "decreasing",
                "current_sample_size": 20,
                "previous_sample_size": 18,
                "rate_history": [0.20, 0.15, 0.10],
            },
        },
    }
    project = make_project(directions=[], design_doc={"review_effectiveness": review_effectiveness})
    session = _make_mock_session(tasks=[], project=project)

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    trends = result["review_effectiveness_trends"]
    assert trends is not None
    assert trends["computed_at"] == "2026-04-01T00:00:00"
    assert trends["persistent_issues"] == ["style"]
    categories = trends["categories"]
    assert "style" in categories
    assert categories["style"]["current_rate"] == 0.35
    assert categories["style"]["previous_rate"] == 0.30
    assert categories["style"]["trend"] == "increasing"
    assert categories["style"]["rate_history"] == [0.28, 0.30, 0.35]
    assert "logic" in categories
    assert categories["logic"]["trend"] == "decreasing"


@pytest.mark.asyncio
async def test_return_structure_includes_review_effectiveness_trends():
    """Result always contains review_effectiveness_trends key."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert "review_effectiveness_trends" in result


# ── Scope merge rates ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scope_merge_rates_computed_from_research_history():
    """scope_merge_rates aggregates scope_breakdown across directions for the 24h window."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Medium scope direction",
        "possibly_saturated": False,
        "research_history": [
            {
                "timestamp": (now - timedelta(hours=5)).isoformat(),
                "prs_merged": 2,
                "tasks_created": 4,
                "scope_breakdown": {
                    "small": {"created": 2, "merged": 2, "failed": 0},
                    "medium": {"created": 2, "merged": 1, "failed": 1},
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

    smr = result["scope_merge_rates"]
    assert isinstance(smr, dict)
    assert "medium" in smr
    assert smr["medium"]["current_rate"] == 0.5
    assert "small" in smr
    assert smr["small"]["current_rate"] == 1.0


@pytest.mark.asyncio
async def test_scope_merge_rates_trend_improving():
    """scope_merge_rates trend is 'improving' when current_rate exceeds prev_rate by > 0.05."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Trending direction",
        "possibly_saturated": False,
        "research_history": [
            # Previous window entry (25-49h ago)
            {
                "timestamp": (now - timedelta(hours=30)).isoformat(),
                "prs_merged": 1,
                "tasks_created": 4,
                "scope_breakdown": {
                    "medium": {"created": 4, "merged": 1, "failed": 3},
                },
            },
            # Current window entry (within 24h)
            {
                "timestamp": (now - timedelta(hours=5)).isoformat(),
                "prs_merged": 3,
                "tasks_created": 4,
                "scope_breakdown": {
                    "medium": {"created": 4, "merged": 3, "failed": 1},
                },
            },
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

    smr = result["scope_merge_rates"]
    assert "medium" in smr
    medium = smr["medium"]
    assert medium["current_rate"] == 0.75
    assert medium["prev_rate"] == 0.25
    assert medium["trend"] == "improving"


@pytest.mark.asyncio
async def test_scope_merge_rates_no_data_returns_empty_dict():
    """scope_merge_rates is an empty dict when no research_history entries have scope_breakdown."""
    project_id = uuid.uuid4()
    now = _utcnow()
    direction_id = str(uuid.uuid4())

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "No scope breakdown",
        "possibly_saturated": False,
        "research_history": [
            {
                "timestamp": (now - timedelta(hours=5)).isoformat(),
                "prs_merged": 1,
                "tasks_created": 2,
                # no scope_breakdown key
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

    assert result["scope_merge_rates"] == {}


@pytest.mark.asyncio
async def test_medium_scope_review_iterations_averages_correctly():
    """medium_scope_review_iterations averages review_loop.iteration for medium-scope terminal tasks."""
    now = _utcnow()
    project_id = uuid.uuid4()

    tasks = [
        # medium scope, 2 iterations
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "review_loop": {"iteration": 2}},
                  created_at=now - timedelta(hours=1)),
        # medium scope, 4 iterations
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "review_loop": {"iteration": 4}},
                  created_at=now - timedelta(hours=2)),
        # small scope — should be excluded
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "small", "review_loop": {"iteration": 1}},
                  created_at=now - timedelta(hours=3)),
        # medium scope but no review_loop — excluded
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium"},
                  created_at=now - timedelta(hours=4)),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["medium_scope_review_iterations"] == 3.0  # (2 + 4) / 2


@pytest.mark.asyncio
async def test_medium_scope_review_iterations_none_when_no_medium_tasks():
    """medium_scope_review_iterations is None when no medium-scope tasks with review_loop exist."""
    now = _utcnow()
    project_id = uuid.uuid4()

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "small", "review_loop": {"iteration": 1}},
                  created_at=now - timedelta(hours=1)),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["medium_scope_review_iterations"] is None


# ── Self-review effectiveness ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_self_review_effectiveness_none_when_fewer_than_3():
    """self_review_effectiveness is None when fewer than 3 tasks have self-review outcomes."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"verdict": "PASS", "fixes_applied": False, "check_results": {}}, "_first_attempt_succeeded": True},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"verdict": "PASS", "fixes_applied": False, "check_results": {}}, "_first_attempt_succeeded": True},
            created_at=now - timedelta(hours=2),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["self_review_effectiveness"] is None


@pytest.mark.asyncio
async def test_self_review_effectiveness_computes_approval_rates():
    """self_review_effectiveness computes with_self_review vs without_self_review approval rates."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        # 3 tasks with self-review, all succeeded first-attempt
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"verdict": "PASS", "fixes_applied": False, "check_results": {}},
                "_first_attempt_succeeded": True,
            },
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"verdict": "PASS", "fixes_applied": False, "check_results": {}},
                "_first_attempt_succeeded": True,
            },
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {"verdict": "PASS", "fixes_applied": True, "check_results": {}},
                "_first_attempt_succeeded": False,
            },
            created_at=now - timedelta(hours=3),
        ),
        # 2 tasks without self-review, 1 succeeded
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=now - timedelta(hours=4),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": False},
            created_at=now - timedelta(hours=5),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    sre = result["self_review_effectiveness"]
    assert sre is not None
    assert sre["sample_size"] == 3
    assert sre["with_self_review_approval_rate"] == round(2 / 3, 2)
    assert sre["without_self_review_approval_rate"] == round(1 / 2, 2)
    assert sre["clean_verdict_approval_rate"] == 1.0
    assert sre["fixed_verdict_approval_rate"] == 0.0


@pytest.mark.asyncio
async def test_self_review_effectiveness_category_correlation():
    """category_correlation shows approval rates split by whether a category had findings."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        # category "style": flagged (passed=False), first-attempt failed
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": True,
                    "check_results": {"style": {"passed": False, "issue": "style problems"}},
                },
                "_first_attempt_succeeded": False,
            },
            created_at=now - timedelta(hours=1),
        ),
        # category "style": flagged (passed=False), first-attempt failed
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": True,
                    "check_results": {"style": {"passed": False}},
                },
                "_first_attempt_succeeded": False,
            },
            created_at=now - timedelta(hours=2),
        ),
        # category "style": clean (passed=True), first-attempt succeeded
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {"style": {"passed": True}},
                },
                "_first_attempt_succeeded": True,
            },
            created_at=now - timedelta(hours=3),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    sre = result["self_review_effectiveness"]
    assert sre is not None
    assert "style" in sre["category_correlation"]
    style = sre["category_correlation"]["style"]
    # 2 tasks with findings, both failed first-attempt → 0.0 approval rate
    assert style["with_findings_approval_rate"] == 0.0
    # 1 task clean, succeeded → 1.0 approval rate
    assert style["clean_approval_rate"] == 1.0


@pytest.mark.asyncio
async def test_self_review_effectiveness_none_when_no_first_attempt_data():
    """Rates are None when tasks have self-review outcomes but lack _first_attempt_succeeded."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"verdict": "PASS", "fixes_applied": False, "check_results": {}}},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"verdict": "PASS", "fixes_applied": False, "check_results": {}}},
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_self_review_outcome": {"verdict": "PASS", "fixes_applied": False, "check_results": {}}},
            created_at=now - timedelta(hours=3),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    sre = result["self_review_effectiveness"]
    assert sre is not None
    assert sre["sample_size"] == 3
    # No _first_attempt_succeeded → rate is None
    assert sre["with_self_review_approval_rate"] is None
    assert sre["without_self_review_approval_rate"] is None


# ── _compute_scope_baselines ─────────────────────────────────────────────────

def test_scope_baselines_empty_tasks_returns_empty():
    """No tasks → empty series and summary."""
    result = _compute_scope_baselines([], 30)
    assert result["series"] == []
    assert result["summary"] == {}
    assert result["lookback_days"] == 30


def test_scope_baselines_single_scope_bucketed_correctly():
    """Tasks in a single week/scope produce one series entry with correct counts."""
    now = _utcnow()
    project_id = uuid.uuid4()
    # All tasks fall in the same calendar week
    base = now.replace(hour=12)
    # Create 3 terminal tasks: 2 COMPLETED+pr_merged (merged) + 1 FAILED
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "small", "pr_merged": True}, created_at=base),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "small", "pr_merged": True}, created_at=base),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "small"}, created_at=base,
                  latest_status_message="error"),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert len(result["series"]) == 1
    entry = result["series"][0]
    assert entry["scope"] == "small"
    assert entry["created"] == 3
    assert entry["merged"] == 2
    assert entry["merge_rate"] == round(2 / 3, 3)

    assert "small" in result["summary"]
    assert result["summary"]["small"]["merge_rate"] == round(2 / 3, 3)
    assert result["summary"]["small"]["created"] == 3
    assert result["summary"]["small"]["merged"] == 2


def test_scope_baselines_multiple_scopes_bucketed_independently():
    """Tasks with different scopes in the same week produce separate series entries."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "small", "pr_merged": True}, created_at=now),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=now),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=now,
                  latest_status_message="error"),
    ]
    result = _compute_scope_baselines(tasks, 30)

    scopes_in_series = {e["scope"] for e in result["series"]}
    assert scopes_in_series == {"small", "medium"}

    assert result["summary"]["small"]["merge_rate"] == 1.0
    assert result["summary"]["medium"]["merge_rate"] == round(1 / 2, 3)


def test_scope_baselines_unknown_scope_for_tasks_without_scope():
    """Tasks with no scope key in context are bucketed under 'unknown'."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={}, created_at=now),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={}, created_at=now,
                  latest_status_message="error"),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert "unknown" in result["summary"]
    assert result["summary"]["unknown"]["created"] == 2


def test_scope_baselines_weekly_bucketing_splits_across_weeks():
    """Tasks in different ISO weeks produce separate series entries for the same scope."""
    now = _utcnow()
    project_id = uuid.uuid4()
    # Place tasks in different weeks (15 days apart guarantees a different ISO week)
    week1_task = make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                           context={"scope": "medium"}, created_at=now - timedelta(days=15))
    week2_task = make_task(project_id=project_id, status=TaskStatus.FAILED,
                           context={"scope": "medium"}, created_at=now - timedelta(days=1),
                           latest_status_message="error")
    result = _compute_scope_baselines([week1_task, week2_task], 30)

    medium_series = [e for e in result["series"] if e["scope"] == "medium"]
    assert len(medium_series) == 2, "Expected two distinct weekly buckets for medium scope"
    assert medium_series[0]["created"] == 1
    assert medium_series[1]["created"] == 1


def test_scope_baselines_trend_improving_from_first_to_last_week():
    """Trend is 'improving' when the last week's merge rate exceeds the first by > 0.05."""
    now = _utcnow()
    project_id = uuid.uuid4()
    # First week: 1/4 = 0.25
    week_a = now - timedelta(days=21)
    tasks_week_a = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=week_a),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=week_a,
                  latest_status_message="e"),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=week_a,
                  latest_status_message="e"),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=week_a,
                  latest_status_message="e"),
    ]
    # Last week: 3/4 = 0.75 (delta +0.5 > 0.05)
    week_b = now - timedelta(days=1)
    tasks_week_b = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=week_b),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=week_b),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=week_b),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=week_b,
                  latest_status_message="e"),
    ]
    result = _compute_scope_baselines(tasks_week_a + tasks_week_b, 30)

    assert result["summary"]["medium"]["trend"] == "improving"


def test_scope_baselines_trend_none_when_only_one_week():
    """Trend is None when all tasks fall in the same week (no comparison possible)."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "small"}, created_at=now),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "small"}, created_at=now,
                  latest_status_message="e"),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert result["summary"]["small"]["trend"] is None


def test_scope_baselines_cancelled_counts_as_created_not_merged():
    """CANCELLED tasks increment 'created' but not 'merged'."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "large", "pr_merged": True}, created_at=now),
        make_task(project_id=project_id, status=TaskStatus.CANCELLED,
                  context={"scope": "large"}, created_at=now),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert result["summary"]["large"]["created"] == 2
    assert result["summary"]["large"]["merged"] == 1
    assert result["summary"]["large"]["merge_rate"] == 0.5


def test_scope_baselines_lookback_days_preserved_in_response():
    """lookback_days in the response matches the parameter passed in."""
    result = _compute_scope_baselines([], 60)
    assert result["lookback_days"] == 60


def test_scope_baselines_week_key_is_monday():
    """The 'week' key in each series entry corresponds to the Monday of that ISO week."""
    from datetime import date as _date
    now = _utcnow()
    project_id = uuid.uuid4()
    # Use a known date: find the Monday of the current week
    today = now.date()
    monday = today - timedelta(days=today.weekday())

    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "small"}, created_at=now),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert len(result["series"]) == 1
    assert result["series"][0]["week"] == monday.isoformat()


# ── _compute_medium_scope_measurement ────────────────────────────────────────

def test_medium_scope_no_tasks_returns_null_rates():
    """No medium-scope tasks → null merge rate, zero counts, empty series."""
    result = _compute_medium_scope_measurement([], None, None, 365)
    assert result["medium_scope_merge_rate"] is None
    assert result["total_created"] == 0
    assert result["total_merged"] == 0
    assert result["investment_start"] is None
    assert result["pre_investment"] is None
    assert result["post_investment"] is None
    assert result["weekly_series"] == []
    assert result["correlated_changes"] == []


def test_medium_scope_headline_rate_computed_correctly():
    """Headline rate = merged / total terminal; CANCELLED counts as created not merged."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=now),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=now),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=now,
                  latest_status_message="error"),
        make_task(project_id=project_id, status=TaskStatus.CANCELLED,
                  context={"scope": "medium"}, created_at=now),
    ]
    result = _compute_medium_scope_measurement(tasks, None, None, 365)
    assert result["total_created"] == 4
    assert result["total_merged"] == 2
    assert result["medium_scope_merge_rate"] == round(2 / 4, 3)


def test_medium_scope_pre_post_split_from_research_history():
    """Pre/post windows split correctly using earliest research_history timestamp."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())
    investment_ts = now - timedelta(days=30)

    project = make_project(directions=[{
        "id": direction_id,
        "status": "active",
        "text": "Reliable medium-scope execution",
        "research_history": [
            {"timestamp": (investment_ts + timedelta(days=5)).isoformat(), "prs_merged": 1},
            {"timestamp": investment_ts.isoformat(), "prs_merged": 2},
        ],
    }])

    # 2 tasks before investment start, 3 after
    pre_tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True},
                  created_at=investment_ts - timedelta(days=10)),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=investment_ts - timedelta(days=5),
                  latest_status_message="e"),
    ]
    post_tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True},
                  created_at=investment_ts + timedelta(days=10)),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True},
                  created_at=investment_ts + timedelta(days=15)),
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium"}, created_at=investment_ts + timedelta(days=20),
                  latest_status_message="e"),
    ]

    result = _compute_medium_scope_measurement(pre_tasks + post_tasks, project, direction_id, 365)

    assert result["investment_start"] is not None
    pre = result["pre_investment"]
    post = result["post_investment"]
    assert pre is not None
    assert post is not None
    assert pre["created"] == 2
    assert pre["merged"] == 1
    assert pre["merge_rate"] == 0.5
    assert post["created"] == 3
    assert post["merged"] == 2
    assert post["merge_rate"] == round(2 / 3, 3)


def test_medium_scope_direction_merges_in_weekly_series():
    """direction_merges in weekly series counts only direction-associated merged tasks."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())
    other_direction_id = str(uuid.uuid4())

    # All tasks in the same week
    tasks = [
        # Direction-associated merged
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True, "direction_id": direction_id},
                  created_at=now),
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True, "direction_id": direction_id},
                  created_at=now),
        # Merged but different direction — should NOT count in direction_merges
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True, "direction_id": other_direction_id},
                  created_at=now),
        # Direction-associated but not merged
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  context={"scope": "medium", "direction_id": direction_id},
                  created_at=now, latest_status_message="e"),
    ]

    result = _compute_medium_scope_measurement(tasks, None, direction_id, 365)

    assert len(result["weekly_series"]) == 1
    entry = result["weekly_series"][0]
    assert entry["created"] == 4
    assert entry["merged"] == 3
    assert entry["direction_merges"] == 2


def test_medium_scope_correlated_changes_only_direction_merged():
    """correlated_changes includes only direction-associated COMPLETED+pr_merged tasks."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    tasks = [
        # Direction-associated merged — should appear in correlated_changes
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  title="Add retry logic",
                  context={"scope": "medium", "pr_merged": True, "direction_id": direction_id},
                  created_at=now),
        # Merged but no direction — should NOT appear
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  title="Unrelated task",
                  context={"scope": "medium", "pr_merged": True},
                  created_at=now),
        # Direction-associated but not merged — should NOT appear
        make_task(project_id=project_id, status=TaskStatus.FAILED,
                  title="Failed direction task",
                  context={"scope": "medium", "direction_id": direction_id},
                  created_at=now, latest_status_message="e"),
    ]

    result = _compute_medium_scope_measurement(tasks, None, direction_id, 365)

    changes = result["correlated_changes"]
    assert len(changes) == 1
    assert changes[0]["title"] == "Add retry logic"
    assert changes[0]["week"] is not None
    # First week has no previous, so delta is None
    assert changes[0]["merge_rate_delta"] is None


def test_medium_scope_no_direction_returns_empty_correlated_changes():
    """Without a direction_id and no project directions, correlated_changes is empty."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(project_id=project_id, status=TaskStatus.COMPLETED,
                  context={"scope": "medium", "pr_merged": True}, created_at=now),
    ]
    result = _compute_medium_scope_measurement(tasks, None, None, 365)
    assert result["correlated_changes"] == []
    assert result["investment_start"] is None


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
