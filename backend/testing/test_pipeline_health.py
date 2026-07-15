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
from unittest.mock import AsyncMock, MagicMock

from app.routers.admin_digest import compute_pipeline_health_summary
from app.routers.admin_scope_measurement import (
    _compute_scope_baselines,
    _compute_medium_scope_measurement,
    _compute_first_attempt_measurement,
)
from app.models import TaskStatus
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=3),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=4),
            latest_status_message="ImportError: missing module",
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=1),
            latest_status_message="timeout",
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=1),
            latest_status_message="SyntaxError: invalid syntax",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=2),
            latest_status_message="AttributeError: NoneType has no attribute",
        ),
        # PROVISION_FAILURE
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=3),
            latest_status_message="CPUS_ALL_REGIONS quota exceeded for zone us-central1",
        ),
        # RATE_LIMIT
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=4),
            latest_status_message="API Error: Rate limit reached",
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    dist = result["failure_distribution"]
    assert dist.get("CODE_ERROR") == 2
    assert dist.get("PROVISION_FAILURE") == 1
    assert dist.get("RATE_LIMIT") == 1


@pytest.mark.asyncio
async def test_failure_distribution_projection_loads_api_call_summary(db_session):
    """Regression: the health-summary task query loads rows with
    load_only(..., raiseload=True) (app/pipeline_health.py); classify_failure →
    _stuck_llm_call_no_progress (failure_classification.py) reads
    task.api_call_summary inside a broad ``try/except Exception: return False``.
    When the projection omitted the column, the raiseload InvalidRequestError
    was silently swallowed and the silent-provider-hang → RATE_LIMIT
    reclassification was disabled: the stalled task below misclassified as
    AGENT_TIMEOUT. Uses a REAL DB and a FRESH session (empty identity map) so
    the projection genuinely applies — the mocked-session tests above bypass it.
    """
    from conftest import TestingSessionLocal

    project = make_project(directions=[])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stalled = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=_utcnow() - timedelta(hours=1),
        latest_status_message="Fatal error: Claude Code exceeded 45-minute time limit",
        api_call_summary={"llm_call_count": 1, "total_output_tokens": 0},
    )
    db_session.add(stalled)
    await db_session.commit()

    async with TestingSessionLocal() as fresh_session:
        result = await compute_pipeline_health_summary(fresh_session, project.id)

    assert result["failure_distribution"].get("no_progress_timeout") == 1, (
        "stalled-LLM failure must remain retryable without cooling an account — got "
        f"{result['failure_distribution']!r}; api_call_summary was likely "
        "unreadable on the projected row"
    )


@pytest.mark.asyncio
async def test_failure_taxonomy_distribution_mixed():
    """failure_taxonomy bucket counts and percentages sum to ~100%."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        # review_quality (test/lint keywords)
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=1),
            latest_status_message="pytest tests failed",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=2),
            latest_status_message="ruff lint error",
        ),
        # scope_related (positive scope evidence: status-msg scope keyword)
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=3),
            latest_status_message="too many files changed",
            context={"scope": "large", "scope_calibrated_from": "medium"},
        ),
        # infra_related
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=4),
            latest_status_message="API Error: Rate limit reached",
        ),
        # code_defect (no scope, no keywords)
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=5),
            latest_status_message="SyntaxError: unexpected",
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)
    tax = result["failure_taxonomy"]
    assert tax["total_failures"] == 5
    assert tax["categories"]["review_quality"]["count"] == 2
    assert tax["categories"]["scope_related"]["count"] == 1
    assert tax["categories"]["infra_related"]["count"] == 1
    assert tax["categories"]["code_defect"]["count"] == 1
    total_pct = sum(v["percentage"] for v in tax["categories"].values())
    assert 99.9 <= total_pct <= 100.1
    assert tax["dominant_category"] == "review_quality"


@pytest.mark.asyncio
async def test_failure_taxonomy_empty():
    """No failures → zero counts, None dominant_category."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, uuid.uuid4())
    tax = result["failure_taxonomy"]
    assert tax["total_failures"] == 0
    assert tax["dominant_category"] is None
    assert all(v["count"] == 0 for v in tax["categories"].values())


@pytest.mark.asyncio
async def test_failure_distribution_top_3_only():
    """Failure distribution is capped at top 3 classes by count."""
    now = _utcnow()
    project_id = uuid.uuid4()

    # 4 distinct failure classes — only top 3 should appear
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=1),
            latest_status_message="SyntaxError",
        ),  # CODE_ERROR x3
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=2),
            latest_status_message="ImportError",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=3),
            latest_status_message="NameError",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=4),
            latest_status_message="CPUS_ALL_REGIONS quota exceeded",
        ),  # PROVISION_FAILURE x2
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=5),
            latest_status_message="vm startup failed network",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=6),
            latest_status_message="API Error: Rate limit reached",
        ),  # RATE_LIMIT x1 (should be cut)
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=7),
            latest_status_message="heartbeat lost timeout oom",
        ),  # AGENT_TIMEOUT x1 (should be cut)
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
    prev_completed = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=36),
    )
    prev_failed = make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        created_at=now - timedelta(hours=36),
        latest_status_message="error",
    )
    # Current window (0–24h ago): 4 completed, 1 failed → 80%
    curr_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=5),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=6),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=7),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=8),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=9),
            latest_status_message="error",
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["trend"] is None


# ── Direction stats ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direction_stats_conversion_rate():
    """Direction with 2 research cycles and 1 PR merged from 4 tasks created → conversion_rate = 0.25, no merge_rate key."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Improve test coverage",
                "possibly_saturated": False,
                "completion_criteria": "Coverage > 80%",
                "possibly_complete": True,
                "category": "structural",
                "research_history": [
                    {
                        "timestamp": (now - timedelta(hours=5)).isoformat(),
                        "prs_merged": 1,
                        "tasks_created": 4,
                        "prs_rejected": 3,
                        "acceptance_proven_merged": 1,
                        "acceptance_consumed_merged": 1,
                    },
                ],
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=6),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["direction_id"] == direction_id
    assert stats[0]["cycles"] == 2
    assert "merge_rate" not in stats[0]
    assert stats[0]["conversion_rate"] == 0.25
    assert stats[0]["possibly_saturated"] is False
    assert stats[0]["completion_criteria"] == "Coverage > 80%"
    assert stats[0]["possibly_complete"] is True
    assert stats[0]["category"] == "structural"
    assert stats[0]["acceptance_rate"] == 0.25
    assert stats[0]["verified_prs_merged"] == 1
    assert stats[0]["proven_prs_merged"] == 1
    assert stats[0]["consumed_prs_merged"] == 1


@pytest.mark.asyncio
async def test_conversion_rate_excludes_degraded_unverified_merges():
    """Direction- and pipeline-level conversion exclude degraded-unverified merges.

    Cycle has 3 merged PRs from 4 tasks created, but only 2 verified (verified +
    benign-unverified); 1 was degraded-unverified. Conversion must be 2/4=0.5,
    not 3/4=0.75 — agreeing with the per-task verification layer.
    """
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Improve test coverage",
                "research_history": [
                    {
                        "timestamp": (now - timedelta(hours=5)).isoformat(),
                        "prs_merged": 3,
                        "verified_prs_merged": 2,
                        "tasks_created": 4,
                    },
                ],
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["conversion_rate"] == 0.5
    assert stats[0]["proven_prs_merged"] == 0
    assert stats[0]["consumed_prs_merged"] == 0
    # Pipeline-level conversion is verification-aware too: 2 verified / 4 created.
    assert result["pipeline_conversion_rate"] == 0.5


@pytest.mark.asyncio
async def test_conversion_rate_excludes_pr_ineligible_task_types():
    """conversion_rate denominator drops research/memo tasks (which never open a PR).

    research_history records 6 tasks_created but type_breakdown shows 4 of them are
    PR-ineligible (research/memo), leaving 2 PR-eligible. With 1 PR merged the
    direction conversion_rate AND the headline pipeline_conversion_rate must be
    1/2 = 0.5, not 1/6 = 0.167.
    """
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Mixed task-type direction",
                "research_history": [
                    {
                        "timestamp": (now - timedelta(hours=5)).isoformat(),
                        "prs_merged": 1,
                        "tasks_created": 6,
                        "type_breakdown": {
                            "research": {"created": 3},
                            "memo": {"created": 1},
                            "create": {"created": 2},
                        },
                    }
                ],
            }
        ]
    )
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    # 1 merged / (6 created - 4 PR-ineligible) = 1/2
    assert stats[0]["conversion_rate"] == 0.5
    # Headline pipeline rate uses the same PR-eligible denominator.
    assert result["pipeline_conversion_rate"] == 0.5


@pytest.mark.asyncio
async def test_direction_with_zero_cycles_excluded():
    """Active direction with no completed research cycles in window is excluded from stats."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Direction with no cycles",
                "possibly_saturated": False,
                "research_history": [],
            }
        ]
    )

    # Task exists but is FAILED, not COMPLETED
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
            latest_status_message="error",
        ),
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

    project = make_project(
        directions=[
            {
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
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
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

    project = make_project(
        directions=[
            {
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
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
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

    project = make_project(
        directions=[
            {
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
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1
    assert stats[0]["dominant_dropout_stage"] is None, (
        f"Expected None for all-zero stage_dropout, got {stats[0]['dominant_dropout_stage']}"
    )


@pytest.mark.asyncio
async def test_direction_stats_sorted_by_cycles_descending():
    """direction_stats are ordered highest-cycles first so dashboard shows busiest directions at top."""
    now = _utcnow()
    project_id = uuid.uuid4()
    dir_a = str(uuid.uuid4())
    dir_b = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
                "id": dir_a,
                "status": "active",
                "research_history": [
                    {
                        "timestamp": (now - timedelta(hours=3)).isoformat(),
                        "prs_merged": 1,
                        "tasks_created": 3,
                    }
                ],
            },
            {
                "id": dir_b,
                "status": "active",
                "research_history": [
                    {
                        "timestamp": (now - timedelta(hours=3)).isoformat(),
                        "prs_merged": 0,
                        "tasks_created": 1,
                    }
                ],
            },
        ]
    )
    tasks = [
        # dir_a: 3 completed research cycles
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": dir_a},
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": dir_a},
            created_at=now - timedelta(hours=4),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": dir_a},
            created_at=now - timedelta(hours=6),
        ),
        # dir_b: 1 completed research cycle
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": dir_b},
            created_at=now - timedelta(hours=5),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 2
    assert stats[0]["direction_id"] == dir_a, (
        "Direction with more cycles should be first"
    )
    assert stats[0]["cycles"] == 3
    assert stats[1]["direction_id"] == dir_b
    assert stats[1]["cycles"] == 1


@pytest.mark.asyncio
async def test_direction_stats_entry_shape_pinned():
    """Each direction_stats entry exposes all dashboard-facing keys."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "possibly_saturated": True,
                "possibly_complete": False,
                "completion_criteria": "done",
                "category": "reliability",
                "research_history": [
                    {
                        "timestamp": (now - timedelta(hours=2)).isoformat(),
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "prs_rejected": 1,
                    }
                ],
            }
        ]
    )
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=1),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    entry = result["direction_stats"][0]
    expected_keys = {
        "direction_id",
        "cycles",
        "possibly_saturated",
        "conversion_rate",
        "dominant_dropout_stage",
        "completion_criteria",
        "possibly_complete",
        "category",
        "acceptance_rate",
    }
    missing = expected_keys - entry.keys()
    assert not missing, f"direction_stats entry missing keys: {missing}"


# ── Timezone-aware research_history timestamps ───────────────────────────────


@pytest.mark.asyncio
async def test_research_history_tz_aware_timestamp_included_in_window():
    """research_history entries with tz-aware (UTC-Z) timestamps are counted, not silently dropped."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    # Produce a tz-aware ISO timestamp (the 'Z' / '+00:00' form) that is
    # inside the default 24-hour lookback window.
    aware_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

    project = make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "research_history": [
                    {
                        "timestamp": aware_ts,
                        "prs_merged": 2,
                        "tasks_created": 4,
                        "prs_rejected": 1,
                    }
                ],
            }
        ]
    )
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    # Must not raise TypeError for tz-aware vs naive comparison
    result = await compute_pipeline_health_summary(session, project_id)

    stats = result["direction_stats"]
    assert len(stats) == 1, "direction should appear in stats"
    assert stats[0]["cycles"] == 1
    # prs_merged/tasks_created should be counted from the tz-aware entry
    assert stats[0]["conversion_rate"] == 0.5  # 2 merged / 4 created
    assert stats[0]["acceptance_rate"] == pytest.approx(2 / 3, abs=0.001)


# ── First-review approval rate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_review_approval_rate_computes_correctly():
    """create/refactor merged tasks without rejection categories count as approved on first review."""
    project_id = uuid.uuid4()
    now = _utcnow()
    tasks = [
        # Approved on first review (no _first_attempt_rejection_categories)
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"pr_merged": True},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "refactor"}],
            context={"pr_merged": True},
            created_at=now - timedelta(hours=1),
        ),
        # Rejected on first review (has _first_attempt_rejection_categories)
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "pr_merged": True,
                "_first_attempt_rejection_categories": ["test-coverage"],
            },
            created_at=now - timedelta(hours=1),
        ),
        # review-type task — excluded from eligible set
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"pr_merged": True},
            created_at=now - timedelta(hours=1),
        ),
        # Not merged — excluded
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"pr_merged": False},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={
                "pr_merged": True,
                "landed_on_integration": False,
                "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            },
            created_at=now - timedelta(hours=1),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    fra = result["first_review_approval_rate"]
    # 2 approved out of 3 eligible landed tasks.
    assert fra["overall"] == round(2 / 3, 2)
    assert fra["sample_size"] == 3
    assert "create" in fra["by_type"]
    assert fra["by_type"]["create"] == round(1 / 2, 2)
    assert fra["by_type"]["refactor"] == 1.0


@pytest.mark.asyncio
async def test_first_review_approval_rate_none_when_no_eligible_tasks():
    """Returns None overall when no create/refactor tasks have pr_merged=True."""
    project_id = uuid.uuid4()
    now = _utcnow()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"pr_merged": True},
            created_at=now - timedelta(hours=1),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    fra = result["first_review_approval_rate"]
    assert fra["overall"] is None
    assert fra["sample_size"] == 0


@pytest.mark.asyncio
async def test_first_review_approval_rate_shape_pinned():
    """first_review_approval_rate always exposes all dashboard-facing sub-keys."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    fra = result["first_review_approval_rate"]
    expected_keys = {
        "overall",
        "sample_size",
        "by_type",
        "by_scope",
        "with_playbook",
        "without_playbook",
        "with_measurements",
        "without_measurements",
    }
    missing = expected_keys - fra.keys()
    assert not missing, f"first_review_approval_rate missing keys: {missing}"


# ── Return structure ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_return_structure_has_all_required_keys():
    """Result always contains all required keys regardless of data."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert set(result.keys()) == {
        "success_rate",
        "trend",
        "park_wake_health",
        "failure_distribution",
        "failure_taxonomy",
        "failure_taxonomy_by_scope",
        "direction_stats",
        "first_attempt_success_rate",
        "first_review_approval_rate",
        "pipeline_conversion_rate",
        "self_review_effectiveness",
        "self_review_prediction_accuracy",
        "self_review_prediction_accuracy_trends",
        "fresh_reviewer_trend",
        "edges_trend",
        "edges_considered_metrics",
        "review_accuracy",
        "persistent_review_issues",
        "review_effectiveness_trends",
        "review_recurrence_by_category",
        "scope_conversion_rates",
        "scope_file_budgets",
        "medium_scope_review_iterations",
        "direction_first_attempt_feedback",
        "medium_scope_measurement",
        "auto_playbook_idle_retired_24h",
        "auto_playbook_low_success_retired_24h",
        "sr_regression_risk_test_signal_trends",
        "sr_false_attestation_trends",
    }
    fa = result["first_attempt_success_rate"]
    assert "first_attempt_by_review_category" in fa
    assert "with_arch_context" in fa
    assert "without_arch_context" in fa
    assert "arch_rejection_rate_with_context" in fa
    assert "arch_rejection_rate_without_context" in fa


@pytest.mark.asyncio
async def test_arch_context_first_attempt_rate_computed_correctly():
    """Tasks with _had_arch_context=True/False are split for separate success rate computation."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        # With arch context: 2 succeed, 1 fails → rate 0.67
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={"_had_arch_context": True, "_first_attempt_succeeded": True},
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
            context={"_had_arch_context": True, "_first_attempt_succeeded": True},
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=3),
            context={"_had_arch_context": True, "_first_attempt_succeeded": False},
        ),
        # Without arch context: 1 succeeds, 1 fails → rate 0.5
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=4),
            context={"_had_arch_context": False, "_first_attempt_succeeded": True},
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=5),
            context={"_had_arch_context": False, "_first_attempt_succeeded": False},
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    fa = result["first_attempt_success_rate"]
    assert fa["with_arch_context"] == pytest.approx(0.67, abs=0.01)
    assert fa["without_arch_context"] == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_arch_rejection_rate_computed_correctly():
    """arch_rejection_rate_with/without_context measures fraction of first-attempt failures
    with 'architecture' in _first_attempt_rejection_categories."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        # With arch context: 2 failures with rejection categories; 1 has 'architecture'
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=1),
            context={
                "_had_arch_context": True,
                "_first_attempt_succeeded": False,
                "_first_attempt_rejection_categories": {"architecture": 1, "style": 1},
            },
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=2),
            context={
                "_had_arch_context": True,
                "_first_attempt_succeeded": False,
                "_first_attempt_rejection_categories": {"style": 1},
            },
        ),
        # Without arch context: 2 failures; both have 'architecture'
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=3),
            context={
                "_had_arch_context": False,
                "_first_attempt_succeeded": False,
                "_first_attempt_rejection_categories": {"architecture": 1},
            },
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=4),
            context={
                "_had_arch_context": False,
                "_first_attempt_succeeded": False,
                "_first_attempt_rejection_categories": {"architecture": 1, "tests": 1},
            },
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    fa = result["first_attempt_success_rate"]
    # With context: 1 of 2 failures had architecture → 0.5
    assert fa["arch_rejection_rate_with_context"] == pytest.approx(0.5, abs=0.01)
    # Without context: 2 of 2 failures had architecture → 1.0
    assert fa["arch_rejection_rate_without_context"] == pytest.approx(1.0, abs=0.01)


@pytest.mark.asyncio
async def test_first_attempt_review_measurements_split():
    """with_review_measurements and without_review_measurements split tasks correctly."""
    project_id = uuid.uuid4()
    now = _utcnow()
    tasks = [
        # With review measurements — succeeded
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={
                "_had_review_measurements": True,
                "_first_attempt_succeeded": True,
            },
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
            context={
                "_had_review_measurements": True,
                "_first_attempt_succeeded": False,
            },
        ),
        # Without review measurements — all failed
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=3),
            context={
                "_had_review_measurements": False,
                "_first_attempt_succeeded": False,
            },
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, project_id)
    fa = result["first_attempt_success_rate"]
    assert fa["with_review_measurements"] == 0.5
    assert fa["without_review_measurements"] == 0.0


@pytest.mark.asyncio
async def test_first_attempt_review_measurements_none_when_no_data():
    """with_review_measurements and without_review_measurements are None when no tasks have the flag."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, uuid.uuid4())
    fa = result["first_attempt_success_rate"]
    assert fa["with_review_measurements"] is None
    assert fa["without_review_measurements"] is None


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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"blueprint_review_verdict": "APPROVE"},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"blueprint_review_verdict": "APPROVE"},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"blueprint_review_verdict": "APPROVE"},
            created_at=now - timedelta(hours=1),
        ),
        # Approve overridden by user (too lenient)
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": "APPROVE",
                "user_overrode_review": True,
            },
            created_at=now - timedelta(hours=1),
        ),
        # Review dismissed
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": "REQUEST_CHANGES",
                "review_dismissed": True,
            },
            created_at=now - timedelta(hours=1),
        ),
        # Approve but user caught an issue Blueprint missed (recall hit)
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"blueprint_review_verdict": "APPROVE", "user_caught_issues": True},
            created_at=now - timedelta(hours=1),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    ra = result["review_accuracy"]
    assert ra is not None
    assert ra["total_reviews"] == 6
    assert ra["overrides"] == 1
    assert ra["dismissals"] == 1
    assert ra["misses"] == 1
    assert (
        ra["total_approvals"] == 5
    )  # 3 plain + 1 overridden + 1 user_caught (all APPROVE verdicts)
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"blueprint_review_verdict": "APPROVE"},
            created_at=now - timedelta(hours=1),
        )
        for _ in range(5)
    ]
    tasks_without_verdict = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={},
            created_at=now - timedelta(hours=1),
        )
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


@pytest.mark.asyncio
async def test_review_recall_never_negative():
    """recall must never be negative, even when verdicts change after misses are recorded.

    Scenario: A task approved-then-changed can have user_caught_issues set while the
    current verdict is REQUEST_CHANGES. The denominator (approvals) shrinks while the
    numerator (misses) stays the same, making naive recall = 1 - misses/approvals negative.
    Fix: Only count misses from tasks whose CURRENT verdict is APPROVE.
    """
    project_id = uuid.uuid4()
    now = _utcnow()
    tasks = [
        # 3 tasks: approved & correct
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={"blueprint_review_verdict": "APPROVE"},
            created_at=now - timedelta(hours=i),
        )
        for i in range(1, 4)
    ]
    # Add 2 tasks that were approved but have user_caught_issues, then re-reviewed to REQUEST_CHANGES
    # This is the problematic case: misses include them, but approvals doesn't
    tasks.extend([
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": "REQUEST_CHANGES",
                "user_caught_issues": True,
            },
            created_at=now - timedelta(hours=4),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "review"}],
            context={
                "blueprint_review_verdict": "REQUEST_CHANGES",
                "user_caught_issues": True,
            },
            created_at=now - timedelta(hours=5),
        ),
    ])
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    ra = result["review_accuracy"]
    assert ra is not None
    # With the fix: misses should only count tasks with BOTH user_caught_issues AND current APPROVE verdict
    # So misses = 0, total_approvals = 3, recall = 1 - 0/3 = 1.0
    assert ra["misses"] == 0, "Misses should only count from currently-APPROVE tasks"
    assert ra["total_approvals"] == 3
    # Most importantly: recall must be in valid range [0, 1]
    assert ra["recall"] is not None
    assert 0 <= ra["recall"] <= 1, f"recall={ra['recall']} must be in [0, 1]"


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
    project = make_project(
        directions=[], design_doc={"review_effectiveness": review_effectiveness}
    )
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


# ── SR prediction accuracy trends ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_trends_none_when_no_design_doc():
    """self_review_prediction_accuracy_trends is None when project has no design_doc."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert result["self_review_prediction_accuracy_trends"] is None


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_trends_returned_when_present():
    """self_review_prediction_accuracy_trends exposes per-category SR catch-rate from design_doc."""
    sr_trends = {
        "computed_at": "2026-04-15T00:00:00",
        "categories": {
            "test-coverage": {
                "current_hit_rate": 0.7,
                "previous_hit_rate": 0.4,
                "miss_rate": 0.3,
                "trend": "increasing",
                "rate_history": [0.4, 0.7],
            },
        },
    }
    project = make_project(
        directions=[], design_doc={"sr_catch_rate_trends": sr_trends}
    )
    session = _make_mock_session(tasks=[], project=project)

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    trends = result["self_review_prediction_accuracy_trends"]
    assert trends is not None
    assert trends["computed_at"] == "2026-04-15T00:00:00"
    cat = trends["categories"]["test-coverage"]
    assert cat["current_hit_rate"] == 0.7
    assert cat["previous_hit_rate"] == 0.4
    assert cat["trend"] == "increasing"


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_trends_surfaces_regression_risk():
    """The regression_risk axis flows through compute_pipeline_health_summary unchanged."""
    sr_trends = {
        "computed_at": "2026-04-15T00:00:00",
        "categories": {
            "regression_risk": {
                "current_hit_rate": 0.5,
                "previous_hit_rate": 0.2,
                "miss_rate": 0.5,
                "trend": "increasing",
                "rate_history": [0.2, 0.5],
            },
        },
    }
    project = make_project(
        directions=[], design_doc={"sr_catch_rate_trends": sr_trends}
    )
    session = _make_mock_session(tasks=[], project=project)

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    trends = result["self_review_prediction_accuracy_trends"]
    assert trends is not None
    assert "regression_risk" in trends["categories"]
    cat = trends["categories"]["regression_risk"]
    assert cat["current_hit_rate"] == 0.5
    assert cat["previous_hit_rate"] == 0.2
    assert cat["trend"] == "increasing"


@pytest.mark.asyncio
async def test_return_structure_includes_self_review_prediction_accuracy_trends():
    """Result always contains self_review_prediction_accuracy_trends key."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert "self_review_prediction_accuracy_trends" in result


# ── Direction first-attempt feedback ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_direction_first_attempt_feedback_absent_when_no_design_doc():
    """direction_first_attempt_feedback is None when design_doc has no entry."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    assert "direction_first_attempt_feedback" in result
    assert result["direction_first_attempt_feedback"] is None


@pytest.mark.asyncio
async def test_direction_first_attempt_feedback_returned_when_present():
    """direction_first_attempt_feedback exposes per-direction rejection tallies."""
    dfa = {
        "dir-abc": {
            "style": 3,
            "logic": 1,
            "total": 4,
            "last_updated": "2026-04-01T00:00:00",
        },
        "dir-xyz": {"tests": 2, "total": 2, "last_updated": "2026-04-02T00:00:00"},
    }
    project = make_project(
        directions=[], design_doc={"direction_first_attempt_feedback": dfa}
    )
    session = _make_mock_session(tasks=[], project=project)

    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    feedback = result["direction_first_attempt_feedback"]
    assert feedback is not None
    assert "dir-abc" in feedback
    assert feedback["dir-abc"]["style"] == 3
    assert feedback["dir-abc"]["total"] == 4
    assert "dir-xyz" in feedback
    assert feedback["dir-xyz"]["tests"] == 2


# ── Scope conversion rates ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scope_conversion_rates_computed_from_research_history():
    """scope_conversion_rates aggregates scope_breakdown across directions for the 24h window."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
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
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    smr = result["scope_conversion_rates"]
    assert isinstance(smr, dict)
    assert "medium" in smr
    assert smr["medium"]["current_rate"] == 0.5
    assert "small" in smr
    assert smr["small"]["current_rate"] == 1.0


@pytest.mark.asyncio
async def test_scope_conversion_rates_trend_improving():
    """scope_conversion_rates trend is 'improving' when current_rate exceeds prev_rate by > 0.05."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
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
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    smr = result["scope_conversion_rates"]
    assert "medium" in smr
    medium = smr["medium"]
    assert medium["current_rate"] == 0.75
    assert medium["prev_rate"] == 0.25
    assert medium["trend"] == "improving"


@pytest.mark.asyncio
async def test_scope_conversion_rates_no_data_returns_empty_dict():
    """scope_conversion_rates is an empty dict when no research_history entries have scope_breakdown."""
    project_id = uuid.uuid4()
    now = _utcnow()
    direction_id = str(uuid.uuid4())

    project = make_project(
        directions=[
            {
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
            }
        ]
    )

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": direction_id},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    result = await compute_pipeline_health_summary(session, project_id)

    assert result["scope_conversion_rates"] == {}


@pytest.mark.asyncio
async def test_scope_file_budgets_surfaced_with_agent_limits():
    """scope_file_budgets exposes the hard file-count caps implementing agents enforce."""
    session = _make_mock_session(tasks=[], project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, uuid.uuid4())

    budgets = result["scope_file_budgets"]
    assert budgets["small"]["total_files_max"] == 6
    assert budgets["medium"]["production_files_max"] == 10
    assert budgets["medium"]["total_files_max"] == 10
    assert budgets["large"]["total_files_max"] == 20


@pytest.mark.asyncio
async def test_medium_scope_review_iterations_averages_correctly():
    """medium_scope_review_iterations averages review_loop.iteration for medium-scope terminal tasks."""
    now = _utcnow()
    project_id = uuid.uuid4()

    tasks = [
        # medium scope, 2 iterations
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "review_loop": {"iteration": 2}},
            created_at=now - timedelta(hours=1),
        ),
        # medium scope, 4 iterations
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "review_loop": {"iteration": 4}},
            created_at=now - timedelta(hours=2),
        ),
        # small scope — should be excluded
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small", "review_loop": {"iteration": 1}},
            created_at=now - timedelta(hours=3),
        ),
        # medium scope but no review_loop — excluded
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium"},
            created_at=now - timedelta(hours=4),
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small", "review_loop": {"iteration": 1}},
            created_at=now - timedelta(hours=1),
        ),
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
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {},
                },
                "_first_attempt_succeeded": True,
            },
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {},
                },
                "_first_attempt_succeeded": True,
            },
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
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {},
                },
                "_first_attempt_succeeded": True,
            },
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {},
                },
                "_first_attempt_succeeded": True,
            },
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": True,
                    "check_results": {},
                },
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
                    "check_results": {
                        "style": {"passed": False, "issue": "style problems"}
                    },
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
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {},
                }
            },
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {},
                }
            },
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "PASS",
                    "fixes_applied": False,
                    "check_results": {},
                }
            },
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


# ── Self-review prediction accuracy ──────────────────────────────────────────


def _make_sr_task(project_id, check_results, human_cats, hours_ago=1):
    """Helper to build a task with both self-review and human rejection data."""
    now = _utcnow()
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={
            "_self_review_outcome": {"check_results": check_results},
            "_first_attempt_rejection_categories": human_cats,
        },
        created_at=now - timedelta(hours=hours_ago),
    )


def test_per_type_failure_rate_excludes_empty_type_rows():
    """Per-type failure rate must exclude empty-type rows from the sample.

    When querying for a specific task type, rows with empty/absent task_type
    should not be included in the sample. This prevents empty-type failures from
    inflating a healthy type's failure rate and empty-type successes from
    deflating a genuinely-failing type below the throttle threshold.
    """
    from app.shared_state import get_task_type_failure_rate
    import app.shared_state as ss
    from app.time_utils import _utcnow
    from collections import deque

    ss.deploy_monitoring["task_results"] = deque(maxlen=100)
    ts = _utcnow().isoformat()

    # Healthy "create" type: 3 successes
    ss.deploy_monitoring["task_results"].extend(
        [
            (ts, "success", "", "create"),
            (ts, "success", "", "create"),
            (ts, "success", "", "create"),
        ]
    )

    # Empty-type failures that should NOT contaminate the "create" sample
    ss.deploy_monitoring["task_results"].extend(
        [
            (ts, "failure", "CODE_ERROR"),  # old 3-tuple format
            (ts, "failure", "CODE_ERROR", ""),  # 4-tuple with empty type
            (ts, "failure", "CODE_ERROR", ""),
        ]
    )

    # Query for "create" type specifically
    rate, sample_size = get_task_type_failure_rate("create", window_minutes=15)

    # The sample should contain only the 3 "create" successes, not the empty-type failures
    assert sample_size == 3, f"Expected sample_size=3 (only 'create' rows), got {sample_size}"
    assert (
        rate == 0.0
    ), f"Expected rate=0.0 (3 successes, 0 failures in 'create'), got {rate}"


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_null_when_sample_too_small():
    """Returns None when fewer than 5 tasks have both fields."""
    project_id = uuid.uuid4()
    tasks = [
        _make_sr_task(
            project_id,
            {"test-coverage": {"passed": False}},
            {"test-coverage": 1},
            hours_ago=i,
        )
        for i in range(1, 5)  # only 4 tasks
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, project_id)
    assert result["self_review_prediction_accuracy"] is None


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_perfect_overlap():
    """Perfect overlap: every human category was flagged by self-review → hit_rate=1.0."""
    project_id = uuid.uuid4()
    tasks = [
        _make_sr_task(
            project_id,
            {"test-coverage": {"passed": False}, "type-safety": {"passed": False}},
            {"test-coverage": 1, "type-safety": 1},
            hours_ago=i,
        )
        for i in range(1, 7)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, project_id)
    acc = result["self_review_prediction_accuracy"]
    assert acc is not None
    assert acc["prediction_hit_rate"] == 1.0
    assert acc["miss_rate"] == 0.0
    assert acc["sample_size"] == 6


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_complete_miss():
    """Complete miss: self-review passes everything but human rejects → hit_rate=0.0."""
    project_id = uuid.uuid4()
    tasks = [
        _make_sr_task(
            project_id,
            {"error-handling": {"passed": True}},
            {"error-handling": 1},
            hours_ago=i,
        )
        for i in range(1, 7)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, project_id)
    acc = result["self_review_prediction_accuracy"]
    assert acc is not None
    assert acc["prediction_hit_rate"] == 0.0
    assert acc["miss_rate"] == 1.0


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_partial_overlap():
    """Partial overlap: self-review catches 1 of 2 human categories → hit_rate=0.5."""
    project_id = uuid.uuid4()
    # Each task: 2 human cats, self-review flags only 1
    tasks = [
        _make_sr_task(
            project_id,
            {"test-coverage": {"passed": False}, "type-safety": {"passed": True}},
            {"test-coverage": 1, "type-safety": 1},
            hours_ago=i,
        )
        for i in range(1, 7)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, project_id)
    acc = result["self_review_prediction_accuracy"]
    assert acc is not None
    assert acc["prediction_hit_rate"] == 0.5
    assert acc["miss_rate"] == 0.5
    # false_alarm_rate: self-review flagged 1 cat (test-coverage), human rejected it too → 0 false alarms
    assert acc["false_alarm_rate"] == 0.0


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_includes_later_iteration_only():
    """A task first APPROVED then rejected on a later iteration (only
    ``_all_iterations_rejection_categories`` set, no
    ``_first_attempt_rejection_categories``) is in the prediction-accuracy
    sample — matching the scorer's any-iteration ground truth."""
    project_id = uuid.uuid4()
    now = _utcnow()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "check_results": {"test-coverage": {"passed": False}}
                },
                # First review approved; rejected only on a later iteration.
                "_all_iterations_rejection_categories": {"test-coverage": 1},
            },
            created_at=now - timedelta(hours=i),
        )
        for i in range(1, 7)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, project_id)
    acc = result["self_review_prediction_accuracy"]
    assert acc is not None
    # All six later-iteration-only tasks are counted in the sample.
    assert acc["sample_size"] == 6
    # SR flagged test-coverage and the reviewer confirmed it (later) → hit, not
    # a false alarm.
    assert acc["prediction_hit_rate"] == 1.0
    assert acc["false_alarm_rate"] == 0.0


@pytest.mark.asyncio
async def test_self_review_prediction_accuracy_snake_case_sr_does_not_match_kebab_human():
    """Snake_case SR keys (e.g. test_coverage) must NOT match kebab-case human cats (test-coverage)."""
    project_id = uuid.uuid4()
    # SR uses old snake_case keys; human cats use canonical kebab-case — no overlap expected
    tasks = [
        _make_sr_task(
            project_id,
            {"test_coverage": {"passed": False}, "type_safety": {"passed": False}},
            {"test-coverage": 1, "type-safety": 1},
            hours_ago=i,
        )
        for i in range(1, 7)
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))
    result = await compute_pipeline_health_summary(session, project_id)
    acc = result["self_review_prediction_accuracy"]
    assert acc is not None
    # snake_case SR keys are different strings from kebab-case human cats → zero overlap
    assert acc["prediction_hit_rate"] == 0.0
    assert acc["miss_rate"] == 1.0


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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small", "pr_merged": True},
            created_at=base,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small", "pr_merged": True},
            created_at=base,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "small"},
            created_at=base,
            latest_status_message="error",
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small", "pr_merged": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=now,
            latest_status_message="error",
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={},
            created_at=now,
            latest_status_message="error",
        ),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert "unknown" in result["summary"]
    assert result["summary"]["unknown"]["created"] == 2


def test_scope_baselines_weekly_bucketing_splits_across_weeks():
    """Tasks in different ISO weeks produce separate series entries for the same scope."""
    now = _utcnow()
    project_id = uuid.uuid4()
    # Place tasks in different weeks (15 days apart guarantees a different ISO week)
    week1_task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"scope": "medium"},
        created_at=now - timedelta(days=15),
    )
    week2_task = make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        context={"scope": "medium"},
        created_at=now - timedelta(days=1),
        latest_status_message="error",
    )
    result = _compute_scope_baselines([week1_task, week2_task], 30)

    medium_series = [e for e in result["series"] if e["scope"] == "medium"]
    assert len(medium_series) == 2, (
        "Expected two distinct weekly buckets for medium scope"
    )
    assert medium_series[0]["created"] == 1
    assert medium_series[1]["created"] == 1


def test_scope_baselines_trend_improving_from_first_to_last_week():
    """Trend is 'improving' when the last week's merge rate exceeds the first by > 0.05."""
    now = _utcnow()
    project_id = uuid.uuid4()
    # First week: 1/4 = 0.25
    week_a = now - timedelta(days=21)
    tasks_week_a = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=week_a,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=week_a,
            latest_status_message="e",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=week_a,
            latest_status_message="e",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=week_a,
            latest_status_message="e",
        ),
    ]
    # Last week: 3/4 = 0.75 (delta +0.5 > 0.05)
    week_b = now - timedelta(days=1)
    tasks_week_b = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=week_b,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=week_b,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=week_b,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=week_b,
            latest_status_message="e",
        ),
    ]
    result = _compute_scope_baselines(tasks_week_a + tasks_week_b, 30)

    assert result["summary"]["medium"]["trend"] == "improving"


def test_scope_baselines_trend_none_when_only_one_week():
    """Trend is None when all tasks fall in the same week (no comparison possible)."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small"},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "small"},
            created_at=now,
            latest_status_message="e",
        ),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert result["summary"]["small"]["trend"] is None


def test_scope_baselines_cancelled_counts_as_created_not_merged():
    """CANCELLED tasks increment 'created' but not 'merged'."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "large", "pr_merged": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.CANCELLED,
            context={"scope": "large"},
            created_at=now,
        ),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert result["summary"]["large"]["created"] == 2
    assert result["summary"]["large"]["merged"] == 1
    assert result["summary"]["large"]["merge_rate"] == 0.5


def test_scope_baselines_exclude_non_merge_unblocks_from_merged_count():
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small", "pr_merged": True, "landed_on_integration": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "scope": "small",
                "pr_merged": True,
                "landed_on_integration": False,
                "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            },
            created_at=now,
        ),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert result["summary"]["small"]["created"] == 2
    assert result["summary"]["small"]["merged"] == 1


def test_scope_baselines_lookback_days_preserved_in_response():
    """lookback_days in the response matches the parameter passed in."""
    result = _compute_scope_baselines([], 60)
    assert result["lookback_days"] == 60


def test_scope_baselines_week_key_is_monday():
    """The 'week' key in each series entry corresponds to the Monday of that ISO week."""
    now = _utcnow()
    project_id = uuid.uuid4()
    # Use a known date: find the Monday of the current week
    today = now.date()
    monday = today - timedelta(days=today.weekday())

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "small"},
            created_at=now,
        ),
    ]
    result = _compute_scope_baselines(tasks, 30)

    assert len(result["series"]) == 1
    assert result["series"][0]["week"] == monday.isoformat()


# ── _compute_medium_scope_measurement ────────────────────────────────────────


def test_medium_scope_no_tasks_returns_null_rates():
    """No medium-scope tasks → null merge rate, zero counts, empty series."""
    result = _compute_medium_scope_measurement([], None, None, 365)
    assert result["medium_scope_conversion_rate"] is None
    assert result["total_created"] == 0
    assert result["total_merged"] == 0
    assert result["investment_start"] is None
    assert result["pre_investment"] is None
    assert result["post_investment"] is None
    assert result["recent_30d"] is None
    assert result["recent_7d"] is None
    assert result["weekly_series"] == []
    assert result["correlated_changes"] == []


def test_medium_scope_recent_windows_filter_by_resolved_at():
    """recent_30d / recent_7d count terminal tasks by updated_at (resolved time).

    Post-93218377: rolling windows filter by when a task reached terminal
    state (updated_at), not when it was created, to avoid in-flight bias.
    """
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        # Resolved within 7d: merged
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now - timedelta(days=4),
            updated_at=now - timedelta(days=2),
        ),
        # Resolved within 30d but outside 7d: failed
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=now - timedelta(days=18),
            updated_at=now - timedelta(days=15),
            latest_status_message="e",
        ),
        # Resolved within 30d but outside 7d: merged
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now - timedelta(days=22),
            updated_at=now - timedelta(days=20),
        ),
        # Resolved outside 30d: merged — excluded from both recent windows
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now - timedelta(days=65),
            updated_at=now - timedelta(days=60),
        ),
    ]
    result = _compute_medium_scope_measurement(tasks, None, None, 365)
    r30 = result["recent_30d"]
    r7 = result["recent_7d"]
    assert r30 is not None
    assert r30["finished"] == 3
    assert r30["merged"] == 2
    assert r30["merge_rate"] == round(2 / 3, 3)
    assert r7 is not None
    assert r7["finished"] == 1
    assert r7["merged"] == 1
    assert r7["merge_rate"] == 1.0


@pytest.mark.asyncio
async def test_pipeline_health_summary_exposes_recent_medium_windows():
    """compute_pipeline_health_summary surfaces recent_30d / recent_7d in medium_scope_measurement."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now - timedelta(days=5),
            updated_at=now - timedelta(days=3),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=now - timedelta(days=22),
            updated_at=now - timedelta(days=20),
            latest_status_message="e",
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)
    ms = result["medium_scope_measurement"]
    assert ms is not None
    assert "recent_30d" in ms
    assert "recent_7d" in ms
    assert ms["recent_30d"] is not None
    assert ms["recent_30d"]["finished"] == 2
    assert ms["recent_7d"] is not None
    assert ms["recent_7d"]["finished"] == 1
    assert ms["recent_7d"]["merged"] == 1


def test_medium_scope_recent_windows_null_when_no_recent_tasks():
    """recent_30d / recent_7d are None when no tasks reached terminal in the window."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now - timedelta(days=125),
            updated_at=now - timedelta(days=120),
        ),
    ]
    result = _compute_medium_scope_measurement(tasks, None, None, 365)
    assert result["recent_30d"] is None
    assert result["recent_7d"] is None


def test_medium_scope_headline_rate_computed_correctly():
    """Headline rate = merged / total terminal; CANCELLED counts as created not merged."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=now,
            latest_status_message="error",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.CANCELLED,
            context={"scope": "medium"},
            created_at=now,
        ),
    ]
    result = _compute_medium_scope_measurement(tasks, None, None, 365)
    assert result["total_created"] == 4
    assert result["total_merged"] == 2
    assert result["medium_scope_conversion_rate"] == round(2 / 4, 3)


def test_medium_scope_pre_post_split_from_research_history():
    """Pre/post windows split correctly using earliest research_history timestamp."""
    now = _utcnow()
    project_id = uuid.uuid4()
    direction_id = str(uuid.uuid4())
    investment_ts = now - timedelta(days=30)

    project = make_project(
        directions=[
            {
                "id": direction_id,
                "status": "active",
                "text": "Reliable medium-scope execution",
                "research_history": [
                    {
                        "timestamp": (investment_ts + timedelta(days=5)).isoformat(),
                        "prs_merged": 1,
                    },
                    {"timestamp": investment_ts.isoformat(), "prs_merged": 2},
                ],
            }
        ]
    )

    # 2 tasks before investment start, 3 after
    pre_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=investment_ts - timedelta(days=10),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=investment_ts - timedelta(days=5),
            latest_status_message="e",
        ),
    ]
    post_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=investment_ts + timedelta(days=10),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=investment_ts + timedelta(days=15),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium"},
            created_at=investment_ts + timedelta(days=20),
            latest_status_message="e",
        ),
    ]

    result = _compute_medium_scope_measurement(
        pre_tasks + post_tasks, project, direction_id, 365
    )

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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "scope": "medium",
                "pr_merged": True,
                "direction_id": direction_id,
            },
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "scope": "medium",
                "pr_merged": True,
                "direction_id": direction_id,
            },
            created_at=now,
        ),
        # Merged but different direction — should NOT count in direction_merges
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "scope": "medium",
                "pr_merged": True,
                "direction_id": other_direction_id,
            },
            created_at=now,
        ),
        # Direction-associated but not merged
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"scope": "medium", "direction_id": direction_id},
            created_at=now,
            latest_status_message="e",
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            title="Add retry logic",
            context={
                "scope": "medium",
                "pr_merged": True,
                "direction_id": direction_id,
            },
            created_at=now,
        ),
        # Merged but no direction — should NOT appear
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            title="Unrelated task",
            context={"scope": "medium", "pr_merged": True},
            created_at=now,
        ),
        # Direction-associated but not merged — should NOT appear
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            title="Failed direction task",
            context={"scope": "medium", "direction_id": direction_id},
            created_at=now,
            latest_status_message="e",
        ),
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
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"scope": "medium", "pr_merged": True},
            created_at=now,
        ),
    ]
    result = _compute_medium_scope_measurement(tasks, None, None, 365)
    assert result["correlated_changes"] == []
    assert result["investment_start"] is None


# ── _compute_first_attempt_measurement tests ─────────────────────────────────


def test_first_attempt_measurement_empty_returns_null_rate():
    """No tasks → null rate, zero sample_size, empty series, null by_category."""
    result = _compute_first_attempt_measurement([], None, 90)
    assert result["first_attempt_approval_rate"] is None
    assert result["sample_size"] == 0
    assert result["pre_split"] is None
    assert result["post_split"] is None
    assert result["weekly_series"] == []
    assert result["by_category"] is None
    assert result["lookback_days"] == 90


def test_first_attempt_measurement_basic_rate():
    """Rate = succeeded / measured; tasks missing the key are excluded from sample."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": False},
            created_at=now,
        ),
        # No key — excluded from measurement
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={},
            created_at=now,
        ),
    ]
    result = _compute_first_attempt_measurement(tasks, None, 90)
    assert result["sample_size"] == 3
    assert result["first_attempt_approval_rate"] == round(2 / 3, 2)
    assert result["pre_split"] is None
    assert result["post_split"] is None


def test_first_attempt_measurement_pre_post_split():
    """Tasks split correctly on split_date using _derive_completed_at."""
    now = _utcnow()
    split = now - timedelta(days=15)
    project_id = uuid.uuid4()

    # 2 pre-split tasks: 1 success, 1 failure
    pre_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=split - timedelta(days=10),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"_first_attempt_succeeded": False},
            created_at=split - timedelta(days=5),
            latest_status_message="e",
        ),
    ]
    # 3 post-split tasks: 3 successes
    post_tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=split + timedelta(days=5),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=split + timedelta(days=10),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=split + timedelta(days=20),
        ),
    ]
    result = _compute_first_attempt_measurement(pre_tasks + post_tasks, split, 90)

    assert result["pre_split"] is not None
    assert result["post_split"] is not None
    assert result["pre_split"]["sample_size"] == 2
    assert result["pre_split"]["rate"] == 0.5
    assert result["post_split"]["sample_size"] == 3
    assert result["post_split"]["rate"] == 1.0
    # Window boundaries
    assert result["pre_split"]["window_end"] == split.isoformat()
    assert result["post_split"]["window_start"] == split.isoformat()


def test_first_attempt_measurement_by_category_breakdown():
    """by_category counts rejection categories from _first_attempt_rejection_categories."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={
                "_first_attempt_rejection_categories": {"test-coverage": 1, "style": 1}
            },
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_rejection_categories": {"test-coverage": 1}},
            created_at=now,
        ),
        # No rejection category key — excluded from by_category
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=now,
        ),
    ]
    result = _compute_first_attempt_measurement(tasks, None, 90)
    by_cat = result["by_category"]
    assert by_cat is not None
    # test-coverage appears in 2/2 tasks with cats → 1.0; style in 1/2 → 0.5
    assert by_cat["test-coverage"] == 1.0
    assert by_cat["style"] == 0.5


def test_first_attempt_measurement_no_measured_tasks():
    """Tasks without _first_attempt_succeeded key → null rate, zero sample, empty series."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={},
            created_at=now,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={},
            created_at=now,
            latest_status_message="e",
        ),
    ]
    result = _compute_first_attempt_measurement(tasks, None, 90)
    assert result["first_attempt_approval_rate"] is None
    assert result["sample_size"] == 0
    assert result["weekly_series"] == []


def test_first_attempt_measurement_weekly_series_delta():
    """weekly_series delta_from_previous is None for first week, computed for subsequent."""
    project_id = uuid.uuid4()
    # Week 1: 1/2 success (0.5); Week 2: 2/2 success (1.0)
    week1_date = _utcnow() - timedelta(days=14)
    week2_date = _utcnow() - timedelta(days=7)
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=week1_date,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            context={"_first_attempt_succeeded": False},
            created_at=week1_date,
            latest_status_message="e",
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=week2_date,
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            context={"_first_attempt_succeeded": True},
            created_at=week2_date,
        ),
    ]
    result = _compute_first_attempt_measurement(tasks, None, 90)
    series = result["weekly_series"]
    assert len(series) == 2
    assert series[0]["delta_from_previous"] is None
    assert series[0]["rate"] == 0.5
    assert series[1]["rate"] == 1.0
    assert series[1]["delta_from_previous"] == round(1.0 - 0.5, 2)


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

    blacklist_calls = [
        c for c in spy.call_args_list if c.args[0] == "account_blacklisted"
    ]
    assert len(blacklist_calls) == 1
    assert blacklist_calls[0].args == ("account_blacklisted", threshold, threshold)
    snapshot = get_pipeline_health_snapshot()
    assert "account_blacklisted" in snapshot["alerts_active"]


def test_threshold_cross_twice_within_dedup_window_fires_once(
    clean_pipeline_events, mocker
):
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

    pre_calls = [
        c for c in spy.call_args_list if c.args[0] == "provision_timeout_pre_claim"
    ]
    assert len(pre_calls) == 1


def test_alert_fires_again_after_dedup_window_elapses(clean_pipeline_events, mocker):
    """Forcing the dedup last-ts older than PIPELINE_ALERT_DEDUP_SECONDS lets
    the next over-threshold event trigger a fresh alert."""
    spy = mocker.patch("app.shared_state._post_pipeline_alert")
    threshold = PIPELINE_ALERT_THRESHOLDS["account_blacklisted"]

    for i in range(threshold):
        record_pipeline_event(
            "account_blacklisted", task_id=f"t{i}", account_id=f"a{i}"
        )
    pre_calls = [c for c in spy.call_args_list if c.args[0] == "account_blacklisted"]
    assert len(pre_calls) == 1

    _ss.deploy_monitoring["pipeline_alert_last_ts"]["account_blacklisted"] = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=PIPELINE_ALERT_DEDUP_SECONDS + 60)
    )

    record_pipeline_event("account_blacklisted", task_id="t-new", account_id="a-new")

    post_calls = [c for c in spy.call_args_list if c.args[0] == "account_blacklisted"]
    assert len(post_calls) == 2


def test_events_older_than_one_hour_excluded_from_counts(clean_pipeline_events):
    """Stale events older than the rolling 1h window must not be counted."""
    stale_ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    for i in range(20):
        _ss.deploy_monitoring["pipeline_events"].append(
            {
                "type": "heartbeat_timeout_post_claim",
                "ts": stale_ts,
                "task_id": f"old-{i}",
                "vm_id": f"vm-{i}",
                "zone": None,
                "account_id": None,
            }
        )
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
    assert set(snapshot.keys()) == {
        "counts_1h",
        "last_event_ts",
        "thresholds",
        "alerts_active",
        "held_directions",
    }
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
    assert set(ph.keys()) == {
        "counts_1h",
        "last_event_ts",
        "thresholds",
        "alerts_active",
        "held_directions",
    }
    assert ph["counts_1h"]["never_started"] == 1
    assert ph["counts_1h"]["heartbeat_timeout_post_claim"] == 1

    assert "pipeline_health" in data["components"]
    assert data["components"]["pipeline_health"]["status"] == "ok"
    assert "provider_apis" in data["components"]


# ── Review measurement attribution split ─────────────────────────────────────


@pytest.mark.asyncio
async def test_with_measurements_split_computes_approval_rate():
    """Tasks with _had_review_measurements=True produce a with_measurements rate."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={
                "_had_review_measurements": True,
                "_first_attempt_succeeded": True,
            },
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
            context={
                "_had_review_measurements": True,
                "_first_attempt_succeeded": False,
            },
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    fa = result["first_attempt_success_rate"]
    assert fa["with_measurements"] == 0.5
    assert fa["without_measurements"] is None


@pytest.mark.asyncio
async def test_without_measurements_includes_missing_field():
    """Tasks without _had_review_measurements field are counted as 'without'."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={"_first_attempt_succeeded": True},
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
            context={"_first_attempt_succeeded": True},
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(hours=3),
            context={
                "_had_review_measurements": False,
                "_first_attempt_succeeded": False,
            },
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    fa = result["first_attempt_success_rate"]
    # All three tasks go to without_measurements (no field or False)
    assert fa["with_measurements"] is None
    assert fa["without_measurements"] == round(2 / 3, 2)


@pytest.mark.asyncio
async def test_measurements_split_null_when_sample_too_small():
    """Null returned for a group when no tasks have _first_attempt_succeeded recorded."""
    now = _utcnow()
    project_id = uuid.uuid4()
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            context={"_had_review_measurements": True},  # no _first_attempt_succeeded
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    result = await compute_pipeline_health_summary(session, project_id)

    fa = result["first_attempt_success_rate"]
    assert fa["with_measurements"] is None
    assert fa["without_measurements"] is None


# ── lookback_hours parameter ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookback_hours_excludes_tasks_outside_window():
    """Tasks older than lookback_hours window are excluded from success_rate."""
    now = _utcnow()
    project_id = uuid.uuid4()

    # One task completed 2 hours ago (inside 3h window, outside 1h window)
    # One task completed 30 minutes ago (inside both windows)
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=2),
            status_log=[
                {"t": (now - timedelta(hours=2)).isoformat(), "s": "COMPLETED"}
            ],
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            created_at=now - timedelta(minutes=30),
            status_log=[
                {"t": (now - timedelta(minutes=30)).isoformat(), "s": "FAILED"}
            ],
        ),
    ]
    session = _make_mock_session(tasks=tasks, project=make_project(directions=[]))

    # With 1h lookback: only the 30-min-old FAILED task is in window → success_rate = 0.0
    result_1h = await compute_pipeline_health_summary(
        session, project_id, lookback_hours=1
    )
    assert result_1h["success_rate"] == 0.0

    # With 3h lookback: both tasks in window → 1 completed, 1 failed → success_rate = 0.5
    result_3h = await compute_pipeline_health_summary(
        session, project_id, lookback_hours=3
    )
    assert result_3h["success_rate"] == 0.5


# ── All-accounts-blocked stall detection ─────────────────────────────────────


def test_all_accounts_blocked_action_fires_once_per_episode():
    """Every eligible task blocked, nothing dispatched → fire, then debounce."""
    from app.stall_alert_diagnostics import _compute_all_accounts_blocked_action as act

    # Stall starts: fire a single alert.
    assert act(blocked_count=3, dispatched_count=0, already_alerted=False) == "fire"
    # Stall persists across cycles: do not re-alert.
    assert act(blocked_count=3, dispatched_count=0, already_alerted=True) == "noop"


def test_all_accounts_blocked_action_resets_on_dispatch():
    """Any successful dispatch re-arms the debounce for the next stall."""
    from app.stall_alert_diagnostics import _compute_all_accounts_blocked_action as act

    assert act(blocked_count=0, dispatched_count=2, already_alerted=True) == "reset"
    # Mixed cycle (some dispatched, some blocked) is not a full stall.
    assert act(blocked_count=1, dispatched_count=1, already_alerted=True) == "reset"


def test_all_accounts_blocked_action_noop_when_no_signal():
    """Empty cycle or no account-blocked skips should not trip the alert."""
    from app.stall_alert_diagnostics import _compute_all_accounts_blocked_action as act

    assert act(blocked_count=0, dispatched_count=0, already_alerted=False) == "noop"
    assert act(blocked_count=0, dispatched_count=0, already_alerted=True) == "noop"


# ── stall alert title/body formatting ────────────────────────────────────────


def test_format_stall_alert_falls_back_when_no_reasons():
    """No reasons captured → keep the original rate-limit copy. Preserves
    dedup keys and existing operator expectations for legacy callers."""
    from app.stall_alert_diagnostics import _format_stall_alert

    title, body = _format_stall_alert(None, None)
    assert "rate-limited" in title.lower()
    assert "rate-limit cooldown" in body.lower()
    assert "Waiting for cooldown." in body


def test_format_stall_alert_rate_limit_only_includes_recovery():
    """A pure rate-limit cooldown stall should mention earliest recovery
    in the body rather than the generic "Waiting for cooldown" string.
    Title says "eligible" not "all" because describe_pool_blockage
    summarizes per-task-model state — accounts that don't serve this task's
    model can still be healthy. (2026-04-24 misleading-alert fix.)"""
    from app.stall_alert_diagnostics import _format_stall_alert

    earliest = datetime(2026, 4, 25, 12, 30, tzinfo=timezone.utc)
    reasons = {
        "all accounts in rate-limit cooldown (earliest recovery: 2026-04-25T12:30:00+00:00)"
    }
    title, body = _format_stall_alert(reasons, earliest)
    assert title == "Pipeline stalled — eligible Max plan accounts rate-limited"
    assert "Earliest recovery: 2026-04-25T12:30:00+00:00." in body


def test_format_stall_alert_capacity_only_no_cooldown_copy():
    """Slot/cap contention is NOT a rate-limit problem — the title and body
    must not claim cooldown or earliest recovery (none exists)."""
    from app.stall_alert_diagnostics import _format_stall_alert

    title, body = _format_stall_alert({"no eligible accounts or slot contention"}, None)
    assert title == "Pipeline stalled — eligible Max plan accounts at capacity"
    assert "rate-limited" not in title.lower()
    assert "cooldown" not in body.lower()
    assert "Waiting for cooldown" not in body


def test_format_stall_alert_mixed_rate_limit_and_capacity():
    """When both rate-limit and capacity pressure are present (e.g., one
    account cooling down and another at daily-cap), the title must reflect
    the mix instead of pretending it's pure rate-limit."""
    from app.stall_alert_diagnostics import _format_stall_alert

    earliest = datetime(2026, 4, 25, 14, 0, tzinfo=timezone.utc)
    reasons = {
        "all accounts in rate-limit cooldown (earliest recovery: 2026-04-25T14:00:00+00:00)",
        "no eligible accounts or slot contention",
    }
    title, body = _format_stall_alert(reasons, earliest)
    assert (
        title
        == "Pipeline stalled — eligible Max plan accounts unavailable (rate limits + capacity)"
    )
    # Both reasons must be reflected in the body so operators see what's
    # actually wrong rather than only one half of the truth.
    assert "rate-limit cooldown" in body
    assert "slot contention" in body
    assert "Earliest recovery:" in body


def test_format_stall_alert_no_accounts_configured():
    """No-accounts case is its own diagnostic — different fix (add an
    account, not wait for cooldown), so the title must not say cooldown."""
    from app.stall_alert_diagnostics import _format_stall_alert

    title, body = _format_stall_alert({"no accounts configured"}, None)
    assert title == "Pipeline stalled — no Max plan accounts configured"
    assert "rate-limited" not in title.lower()
    assert "cooldown" not in body.lower()


def test_format_stall_alert_no_ready_accounts():
    """Disabled accounts or accounts missing credentials are not a cooldown
    problem; operators need to make an account ready."""
    from app.stall_alert_diagnostics import _format_stall_alert

    title, body = _format_stall_alert({"no ready accounts for provider gemini"}, None)
    assert title == "Pipeline stalled — no Max plan accounts ready"
    assert "rate-limited" not in title.lower()
    assert "cooldown" not in body.lower()

    title, body = _format_stall_alert(
        {"no accounts configured for provider gemini"}, None
    )
    assert title == "Pipeline stalled — no Max plan accounts ready"
    assert "cooldown" not in body.lower()


def test_missing_runtime_credentials_stall_reason_distinguishes_no_pool_and_no_ready():
    from app.stall_alert_diagnostics import (
        _missing_runtime_credentials_stall_reason as reason,
    )

    assert (
        reason(
            user_pool_count=0,
            provider_account_count=0,
            ready_provider_account_count=0,
            runtime_provider="gemini",
        )
        == "no accounts configured"
    )
    assert (
        reason(
            user_pool_count=2,
            provider_account_count=2,
            ready_provider_account_count=0,
            runtime_provider="gemini",
        )
        == "no ready accounts for provider gemini"
    )
    assert (
        reason(
            user_pool_count=2,
            provider_account_count=0,
            ready_provider_account_count=0,
            runtime_provider="gemini",
        )
        == "no accounts configured for provider gemini"
    )


def test_format_stall_alert_partial_rate_limit_string_still_classifies():
    """The partial-mix string from describe_pool_blockage ("N of M
    accounts rate-limited, remaining accounts at capacity") triggers BOTH
    rate-limited AND capacity classification because it actually is both."""
    from app.stall_alert_diagnostics import _format_stall_alert

    reasons = {"3 of 5 accounts rate-limited, remaining accounts at capacity"}
    title, _body = _format_stall_alert(reasons, None)
    assert (
        title
        == "Pipeline stalled — eligible Max plan accounts unavailable (rate limits + capacity)"
    )


def test_format_stall_alert_title_never_claims_pool_wide_when_per_task():
    """Regression guard for the 2026-04-24 misleading-alert bug: the alert
    fires per-cycle when at least one task can't dispatch and zero tasks
    dispatched. The reasons describe per-task-model constraints, not the
    full pool state. Title must not assert "all" pool accounts are bad."""
    from app.stall_alert_diagnostics import _format_stall_alert

    for reasons in (
        None,
        {
            "all accounts in rate-limit cooldown (earliest recovery: 2026-04-25T12:30:00+00:00)"
        },
        {"no eligible accounts or slot contention"},
        {"no accounts configured"},
        {"no ready accounts for provider gemini"},
        {"3 of 5 accounts rate-limited, remaining accounts at capacity"},
    ):
        title, _ = _format_stall_alert(reasons, None)
        # "all Max plan accounts" implies pool-wide state; we only know
        # per-task-model state, so this phrasing is forbidden.
        assert "all Max plan accounts" not in title, (
            f"Title falsely claims pool-wide state for per-task constraint: {title!r}"
        )


# ── retry_after escalation thresholds ────────────────────────────────────────


def test_retry_after_escalation_below_alert_returns_none():
    """Resets below the alert threshold leave the task alone."""
    from app.stall_alert_diagnostics import (
        _compute_retry_after_escalation_action as act,
    )

    assert act(resets=0, alert_threshold=8, fail_threshold=0) is None
    assert act(resets=7, alert_threshold=8, fail_threshold=0) is None


def test_retry_after_escalation_alert_at_threshold():
    """Reaching the alert threshold fires a WARNING."""
    from app.stall_alert_diagnostics import (
        _compute_retry_after_escalation_action as act,
    )

    assert act(resets=8, alert_threshold=8, fail_threshold=0) == "alert"
    assert act(resets=15, alert_threshold=8, fail_threshold=0) == "alert"


def test_retry_after_escalation_never_fails():
    """Pool exhaustion is a wait condition: escalation must never return 'fail',
    no matter how many resets accumulate or what fail_threshold is passed."""
    from app.stall_alert_diagnostics import (
        _compute_retry_after_escalation_action as act,
    )

    for resets in (16, 99, 9999):
        for fail_threshold in (0, 1, 16, 9999):
            assert (
                act(resets=resets, alert_threshold=8, fail_threshold=fail_threshold)
                == "alert"
            )


def test_retry_after_escalation_disabled_alert_threshold():
    """A non-positive alert threshold disables escalation entirely."""
    from app.stall_alert_diagnostics import (
        _compute_retry_after_escalation_action as act,
    )

    assert act(resets=100, alert_threshold=0, fail_threshold=0) is None


@pytest.mark.asyncio
async def test_all_accounts_blocked_emits_broadcast_and_alert(monkeypatch):
    """Simulate a full cycle where every dispatchable task hit the account-blocked
    stall: the orchestrator must broadcast pipeline_throttle with reason
    'all_accounts_blocked' and create a project-level ERROR alert (once)."""
    from app import orchestrator as orch
    from app.models import Alert, AlertSeverity

    orch._all_accounts_blocked_alerted = False

    project_id = uuid.uuid4()
    project = MagicMock()
    project.id = project_id

    added = []
    session = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))

    # First call: Project list; second call: existing-alert dedup check (none).
    proj_result = MagicMock()
    proj_result.scalars.return_value.all.return_value = [project]
    existing_result = MagicMock()
    existing_result.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(side_effect=[proj_result, existing_result])

    broadcasts = []
    ws_mock = MagicMock()
    ws_mock.broadcast_to_all = AsyncMock(
        side_effect=lambda event, payload: broadcasts.append((event, payload))
    )

    proj_events = []

    async def _bp(pid, event, payload):
        proj_events.append((pid, event, payload))

    import app.broadcast as _broadcast_mod

    monkeypatch.setattr(_broadcast_mod, "manager", ws_mock, raising=False)
    monkeypatch.setattr(_broadcast_mod, "broadcast_to_project", _bp, raising=False)

    notifications = []

    async def _dispatch(pid, kind, payload):
        notifications.append((pid, kind, payload))

    import app.notification_dispatch as _nd_mod

    monkeypatch.setattr(_nd_mod, "dispatch_external_notification", _dispatch)

    earliest = datetime.now(timezone.utc) + timedelta(minutes=10)
    await orch._emit_all_accounts_blocked_stall(session, earliest)

    assert any(
        event == "pipeline_throttle" and payload.get("reason") == "all_accounts_blocked"
        for event, payload in broadcasts
    ), broadcasts
    stall_alerts = [
        a for a in added if isinstance(a, Alert) and a.severity == AlertSeverity.ERROR
    ]
    assert len(stall_alerts) == 1
    assert (
        "all Max plan accounts" in stall_alerts[0].message.lower()
        or "rate-limited" in stall_alerts[0].title.lower()
    )


# ── rate_limit_retry_minutes helper ──────────────────────────────────────────


def test_rate_limit_retry_minutes_uses_earliest_recovery():
    """When a recovery time is known, delay scales with time-to-recovery."""
    from app.time_utils import rate_limit_retry_minutes

    now = datetime(2026, 1, 1, 12, 0, 0)
    earliest = now + timedelta(minutes=45)
    assert rate_limit_retry_minutes(earliest, now=now) == pytest.approx(45.0)


def test_rate_limit_retry_minutes_falls_back_to_short_default():
    """With no recovery time known, delay falls back to a short default (>0)."""
    from app.time_utils import rate_limit_retry_minutes

    delay = rate_limit_retry_minutes(None)
    assert 0 < delay <= 5


def test_rate_limit_retry_minutes_clamped_to_max_via_set_retry_after():
    """A far-future recovery (e.g. 4h) is clamped by set_retry_after's ceiling."""
    from app.time_utils import (
        RETRY_AFTER_MAX_MINUTES,
        _utcnow,
        parse_retry_after,
        rate_limit_retry_minutes,
        set_retry_after,
    )

    earliest = _utcnow() + timedelta(hours=4)
    raw_min = rate_limit_retry_minutes(earliest)
    # Helper itself returns the raw (uncapped) minutes — caller (set_retry_after) clamps.
    assert raw_min > RETRY_AFTER_MAX_MINUTES

    ctx: dict = {}
    set_retry_after(ctx, raw_min)
    written = parse_retry_after(ctx["retry_after"])
    assert written is not None
    # Written retry_after must not exceed now + RETRY_AFTER_MAX_MINUTES (allow 1s slack).
    max_allowed = _utcnow() + timedelta(minutes=RETRY_AFTER_MAX_MINUTES, seconds=1)
    assert written <= max_allowed
    # And must be significantly longer than the prior 2-minute fixed default.
    assert written > _utcnow() + timedelta(minutes=10)


# ── Regression: list-shaped check_results must not crash the SR branch ────────


@pytest.mark.asyncio
async def test_self_review_effectiveness_tolerates_list_shaped_check_results():
    """The self-review-effectiveness branch must not crash when an agent emits
    ``check_results`` as a JSON list instead of the expected dict.

    A non-empty list is truthy, so the ``... or {}`` guard misses it and the
    branch previously raised AttributeError on ``check_results.keys()``. The
    ``safe_check_results`` guard degrades a non-dict shape to ``{}``.
    """
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # >= 3 terminal tasks with _self_review_outcome present to enter the branch.
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.COMPLETED,
            created_at=now - timedelta(hours=1),
            payloads=[{"type": "create", "prompt": "x"}],
            context={
                "_self_review_outcome": {
                    "check_results": [
                        {"category": "correctness", "passed": False},
                        {"category": "security", "passed": False},
                    ],
                },
            },
        )
        for _ in range(3)
    ]
    session = _make_mock_session(tasks=tasks, project=project)

    # Must not raise; the malformed shape degrades to an empty mapping.
    result = await compute_pipeline_health_summary(session, project_id)
    assert "self_review_effectiveness" in result
