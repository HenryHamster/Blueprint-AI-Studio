"""Tests for the digest direction-progress block (app.digest_direction_progress)."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.models import (
    TaskStatus,
)
from app.task_evaluators import (
    CHALLENGE_AGREES,
    CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
    CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
    CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    CHALLENGE_DISAGREES,
    CHALLENGE_INDETERMINATE,
)
from testing.factories import make_project, make_task, make_membership, _utcnow


@pytest.mark.asyncio
async def test_digest_direction_progress_basic(client: AsyncClient, db_session):
    """Active direction with completed research tasks appears in direction_progress."""
    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Improve test coverage",
                "status": "active",
                "priority": "high",
                "possibly_saturated": False,
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Research task for that direction, completed within the window
    task = make_task(
        project_id=project.id,
        title="Research task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
        context={"direction_id": "dir-1"},
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert "direction_progress" in data
    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    assert dp["direction_id"] == "dir-1"
    assert dp["direction_text"] == "Improve test coverage"
    assert dp["priority"] == "high"
    assert dp["cycles_in_window"] == 1
    assert dp["tasks_created"] >= 1
    assert dp["possibly_saturated"] is False


@pytest.mark.asyncio
async def test_digest_direction_progress_excludes_inactive(
    client: AsyncClient, db_session
):
    """Inactive directions are not included in direction_progress even if tasks exist."""
    project = make_project(
        directions=[
            {"id": "dir-active", "text": "Active direction", "status": "active"},
            {"id": "dir-inactive", "text": "Inactive direction", "status": "inactive"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-active", "dir-inactive"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task for {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert "dir-active" in ids
    assert "dir-inactive" not in ids


@pytest.mark.asyncio
async def test_digest_direction_progress_includes_held_and_paused(
    client: AsyncClient, db_session
):
    """Held and auto_paused directions with recent task activity appear in direction_progress."""
    project = make_project(
        directions=[
            {"id": "dir-held", "text": "Held direction", "status": "held"},
            {"id": "dir-paused", "text": "Paused direction", "status": "auto_paused"},
            {"id": "dir-inactive", "text": "Inactive direction", "status": "inactive"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-held", "dir-paused", "dir-inactive"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Task for {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    by_id = {dp["direction_id"]: dp for dp in data["direction_progress"]}
    assert "dir-held" in by_id
    assert by_id["dir-held"]["status"] == "held"
    assert "dir-paused" in by_id
    assert by_id["dir-paused"]["status"] == "auto_paused"
    assert "dir-inactive" not in by_id


@pytest.mark.asyncio
async def test_digest_direction_progress_excludes_no_activity(
    client: AsyncClient, db_session
):
    """Active directions with no tasks in the window are omitted from direction_progress."""
    project = make_project(
        directions=[
            {"id": "dir-busy", "text": "Busy direction", "status": "active"},
            {"id": "dir-idle", "text": "Idle direction", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Only create a task for dir-busy
    db_session.add(
        make_task(
            project_id=project.id,
            title="Task for busy",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-busy"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert "dir-busy" in ids
    assert "dir-idle" not in ids


@pytest.mark.asyncio
async def test_digest_direction_progress_authoritative_portfolio_includes_quiet(
    client: AsyncClient, db_session
):
    """Quiet active directions stay in direction_portfolio even when dropped from direction_progress."""
    project = make_project(
        directions=[
            {
                "id": "dir-busy",
                "text": "Busy direction",
                "status": "active",
                "research_history": [
                    {"timestamp": _utcnow().isoformat(), "autonomous_prs_merged": 11}
                ],
                "history_aggregate": {"total_operator_actions": 0},
            },
            {
                "id": "dir-quiet",
                "text": "Quiet direction",
                "status": "held",
                "research_history": [
                    {"timestamp": _utcnow().isoformat(), "autonomous_prs_merged": 12}
                ],
                "history_aggregate": {"total_operator_actions": 0},
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Task for busy",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-busy"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert [dp["direction_id"] for dp in data["direction_progress"]] == ["dir-busy"]
    portfolio = data["direction_portfolio"]
    assert portfolio["active_count"] == 2
    assert portfolio["self_sufficient_count"] == 2
    assert portfolio["composition"] == [
        {
            "direction_id": "dir-busy",
            "direction_text": "Busy direction",
            "status": "active",
            "priority": "medium",
            "quiet_this_window": False,
        },
        {
            "direction_id": "dir-quiet",
            "direction_text": "Quiet direction",
            "status": "held",
            "priority": "medium",
            "quiet_this_window": True,
        },
    ]


@pytest.mark.asyncio
async def test_digest_direction_progress_authoritative_portfolio_all_quiet(
    client: AsyncClient, db_session
):
    """When every active direction is quiet, direction_progress can be empty while direction_portfolio stays truthful."""
    project = make_project(
        directions=[
            {"id": "dir-a", "text": "Quiet A", "status": "active"},
            {"id": "dir-b", "text": "Quiet B", "status": "auto_paused"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["direction_progress"] == []
    portfolio = data["direction_portfolio"]
    assert portfolio["active_count"] == 2
    assert portfolio["self_sufficient_count"] == 0
    assert [entry["quiet_this_window"] for entry in portfolio["composition"]] == [
        True,
        True,
    ]


@pytest.mark.asyncio
async def test_digest_direction_progress_prs_merged(client: AsyncClient, db_session):
    """prs_merged is summed from research_history entries within the time window."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction with PRs",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "prs_merged": 3},  # inside window
                    {"timestamp": old_ts, "prs_merged": 10},  # outside window
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    assert data["direction_progress"][0]["prs_merged"] == 3  # only the recent entry


@pytest.mark.asyncio
async def test_digest_direction_progress_verified_prs_merged(
    client: AsyncClient, db_session
):
    """The per-direction output dict exposes verified_prs_merged (summed via
    verified_merged_count), which excludes unverified-degraded merges while the
    raw prs_merged key still counts them."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction with degraded merge",
                "status": "active",
                "research_history": [
                    # 3 merged this window, but only 2 verified (1 degraded).
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 3,
                        "verified_prs_merged": 2,
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    assert dp["prs_merged"] == 3  # raw count still includes the degraded merge
    assert dp["verified_prs_merged"] == 2  # verified count excludes it


@pytest.mark.asyncio
async def test_digest_direction_progress_proven_prs_merged(
    client: AsyncClient, db_session
):
    """The per-direction output dict exposes proven_prs_merged (machine-checked
    named-test proof, summed via proven_merged_count) and the declaration
    coverage (declared / merged-write), STRICTER than verified_prs_merged. A
    legacy entry that predates the acceptance aggregates degrades to 0 / None."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-proven",
                "text": "Direction with proven subset",
                "status": "active",
                "research_history": [
                    # 3 verified merges this window, but only 2 PROVEN; one of
                    # those proven merges is downstream-artifact consumed proof.
                    # 2 of 3 merged WRITE tasks declared forward acceptance evidence.
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 3,
                        "verified_prs_merged": 3,
                        "acceptance_proven_merged": 2,
                        "acceptance_consumed_merged": 1,
                        "acceptance_declared_merged": 2,
                        "acceptance_merged_write_total": 3,
                    },
                ],
            },
            {
                "id": "dir-legacy",
                "text": "Direction with legacy entry (no acceptance fields)",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "verified_prs_merged": 2,
                    },
                ],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-proven", "dir-legacy"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Research task {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    by_id = {dp["direction_id"]: dp for dp in data["direction_progress"]}
    proven = by_id["dir-proven"]
    # proven is the strict subset of verified — always <= verified for the window.
    assert proven["verified_prs_merged"] == 3
    assert proven["proven_prs_merged"] == 2
    assert proven["consumed_prs_merged"] == 1
    assert proven["proven_prs_merged"] <= proven["verified_prs_merged"]
    assert proven["consumed_prs_merged"] <= proven["proven_prs_merged"]
    assert proven["acceptance_declaration_coverage"] == round(2 / 3, 3)

    legacy = by_id["dir-legacy"]
    # Legacy entries claim nothing proven and have no coverage denominator.
    assert legacy["verified_prs_merged"] == 2
    assert legacy["proven_prs_merged"] == 0
    assert legacy["consumed_prs_merged"] == 0
    assert legacy["acceptance_declaration_coverage"] is None


@pytest.mark.asyncio
async def test_digest_direction_progress_generalized_success_metrics(
    client: AsyncClient, db_session
):
    """Windowed direction progress includes generalized successes and kind breakdowns."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-gen",
                "text": "Direction with generalized outcomes",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "prs_merged": 2},
                    {
                        "timestamp": recent_ts2,
                        "generalized_successes": 3,
                        "tasks_completed": 3,
                        "tasks_failed": 1,
                        "outcome_kind_breakdown": {
                            "research_quality": {
                                "success": 3,
                                "neutral": 0,
                                "failure": 1,
                            }
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-gen"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]

    assert dp["generalized_successes"] == 5
    assert dp["generalized_terminal_outcomes"] == 6
    assert dp["generalized_health_rate"] == 0.833
    assert dp["outcome_kind_breakdown"] == {
        "code_merge": {"success": 2, "neutral": 0, "failure": 0},
        "research_quality": {"success": 3, "neutral": 0, "failure": 1},
    }


@pytest.mark.asyncio
async def test_digest_direction_progress_prompt_quality_hint(
    client: AsyncClient, db_session
):
    """prompt_quality_hint is synthesized from research_history prompt_telemetry when available."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    telemetry_cycle = {
        "timestamp": recent_ts,
        "prompt_telemetry": {
            "merged": {
                "avg_char_count": 800,
                "avg_related_file_count": 3,
                "frac_mentions_tests": 0.8,
                "frac_mentions_symbols": 0.8,
            },
            "failed": {
                "avg_char_count": 300,
                "avg_related_file_count": 1,
                "frac_mentions_tests": 0.2,
                "frac_mentions_symbols": 0.2,
            },
        },
    }
    project = make_project(
        directions=[
            {
                "id": "dir-pq",
                "text": "Direction with telemetry",
                "status": "active",
                "research_history": [telemetry_cycle, dict(telemetry_cycle)],
            },
            {
                "id": "dir-no-pq",
                "text": "Direction without telemetry",
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 1}],
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did in ("dir-pq", "dir-no-pq"):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Research {did}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": did},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}
    assert isinstance(by_id["dir-pq"]["prompt_quality_hint"], str)
    assert by_id["dir-pq"]["prompt_quality_hint"]
    assert by_id["dir-no-pq"]["prompt_quality_hint"] is None


@pytest.mark.asyncio
async def test_digest_direction_progress_quality_score(client: AsyncClient, db_session):
    """quality_score appears in direction_progress for directions with research history."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": "dir-q",
                "text": "Quality direction",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "tasks_created": 4, "prs_merged": 3},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-q"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert "quality_score" in dp
    assert isinstance(dp["quality_score"], float)
    assert 0.0 <= dp["quality_score"] <= 1.0


@pytest.mark.asyncio
async def test_digest_direction_progress_review_quality(
    client: AsyncClient, db_session
):
    """review_quality aggregates reviews_dismissed/overridden/user_issues_caught across in-window entries."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=2)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-rq",
                "text": "Review quality direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "reviews_dismissed": 2,
                        "reviews_overridden": 1,
                        "user_issues_caught": 3,
                    },
                    {
                        "timestamp": recent_ts2,
                        "reviews_dismissed": 1,
                        "reviews_overridden": 0,
                        "user_issues_caught": 2,
                    },
                    {
                        "timestamp": old_ts,
                        "reviews_dismissed": 99,
                        "reviews_overridden": 99,
                        "user_issues_caught": 99,
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-rq"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["review_quality"] == {
        "reviews_dismissed": 3,
        "reviews_overridden": 1,
        "user_issues_caught": 5,
    }


@pytest.mark.asyncio
async def test_digest_direction_progress_lifetime_metrics(
    client: AsyncClient, db_session
):
    """Lifetime fields combine research_history with history_aggregate evicted totals."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-life",
                "text": "Lifetime direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "prs_rejected": 1,
                        "tasks_created": 5,
                        "tasks_failed": 1,
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 1,
                        "prs_rejected": 0,
                        "tasks_created": 3,
                        "tasks_failed": 2,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 7,
                    "total_tasks_created": 20,
                    "total_prs_merged": 8,
                    "total_prs_rejected": 4,
                    "total_tasks_failed": 3,
                },
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-life"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]

    assert dp["lifetime_cycles"] == 9  # 2 history entries + 7 aggregated
    assert dp["lifetime_prs_merged"] == 11  # 2 + 1 + 8
    assert dp["lifetime_prs_rejected"] == 5  # 1 + 0 + 4
    assert dp["lifetime_tasks_failed"] == 6  # 1 + 2 + 3
    assert dp["lifetime_conversion_rate"] == round(
        11 / 28, 3
    )  # 11 merged / (5 + 3 + 20) created


@pytest.mark.asyncio
async def test_digest_direction_progress_lifetime_generalized_metrics(
    client: AsyncClient, db_session
):
    """Lifetime generalized metrics combine live history with history_aggregate."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-life-gen",
                "text": "Lifetime generalized direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "generalized_successes": 1,
                        "tasks_completed": 1,
                        "outcome_kind_breakdown": {
                            "memo_artifact": {
                                "success": 1,
                                "neutral": 0,
                                "failure": 0,
                            }
                        },
                    }
                ],
                "history_aggregate": {
                    "total_generalized_successes": 4,
                    "total_generalized_terminal_outcomes": 6,
                },
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-life-gen"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]

    assert dp["lifetime_generalized_successes"] == 5
    assert dp["lifetime_generalized_health_rate"] == round(5 / 7, 3)


@pytest.mark.asyncio
async def test_digest_direction_progress_lifetime_generalized_metrics_upgrade_legacy_aggregate(
    client: AsyncClient, db_session
):
    """Lifetime generalized digest metrics upgrade legacy aggregates before combining live history."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-life-legacy-gen",
                "text": "Lifetime legacy generalized direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "generalized_successes": 3,
                        "tasks_completed": 3,
                        "outcome_kind_breakdown": {
                            "memo_artifact": {
                                "success": 3,
                                "neutral": 0,
                                "failure": 0,
                            }
                        },
                    }
                ],
                "history_aggregate": {
                    "total_prs_merged": 2,
                    "total_tasks_completed": 2,
                    "total_tasks_failed": 1,
                },
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-life-legacy-gen"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]

    assert dp["lifetime_generalized_successes"] == 5
    assert dp["lifetime_generalized_health_rate"] == round(5 / 6, 3)


@pytest.mark.asyncio
async def test_digest_direction_progress_retirement_eligibility(
    client: AsyncClient, db_session
):
    """lifetime_autonomous_prs_merged, lifetime_operator_actions, and retirement_eligible are computed correctly."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-eligible",
                "text": "Eligible direction",
                "status": "active",
                # 4 autonomous from history (2 explicit + legacy fallback 2), aggregate adds 7 → total 11
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 2,
                        "prs_merged": 2,
                        "operator_action_count": 0,
                        "tasks_created": 3,
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 2,
                        "tasks_created": 2,
                    },  # legacy: no autonomous_prs_merged, falls back to prs_merged
                ],
                "history_aggregate": {
                    "total_cycles": 5,
                    "total_tasks_created": 10,
                    "total_prs_merged": 7,
                    "total_autonomous_prs_merged": 7,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "dir-ineligible-ops",
                "text": "Ineligible: has operator actions",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 10,
                        "prs_merged": 10,
                        "operator_action_count": 1,
                        "tasks_created": 10,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 0,
                    "total_prs_merged": 0,
                    "total_autonomous_prs_merged": 0,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "dir-ineligible-count",
                "text": "Ineligible: not enough autonomous merges",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 5,
                        "prs_merged": 5,
                        "operator_action_count": 0,
                        "tasks_created": 5,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 0,
                    "total_prs_merged": 0,
                    "total_autonomous_prs_merged": 4,
                    "total_operator_actions": 0,
                },
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id in ("dir-eligible", "dir-ineligible-ops", "dir-ineligible-count"):
        db_session.add(
            make_task(
                project_id=project.id,
                title="Research task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}

    eligible = by_id["dir-eligible"]
    assert eligible["lifetime_autonomous_prs_merged"] == 11  # 2 + 2 (fallback) + 7
    assert eligible["lifetime_operator_actions"] == 0
    assert eligible["retirement_eligible"] is True

    ineligible_ops = by_id["dir-ineligible-ops"]
    assert ineligible_ops["lifetime_operator_actions"] == 1
    assert ineligible_ops["retirement_eligible"] is False

    ineligible_count = by_id["dir-ineligible-count"]
    assert ineligible_count["lifetime_autonomous_prs_merged"] == 9  # 5 + 4
    assert ineligible_count["retirement_eligible"] is False


@pytest.mark.asyncio
async def test_digest_retirement_eligibility_boundary_cases(
    client: AsyncClient, db_session
):
    """At-threshold (exactly 10, 0 ops → True) and above-threshold + operator action (12, 1 op → False)."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-at-threshold",
                "text": "Exactly at threshold",
                "status": "active",
                # 4 from history + 6 from aggregate = 10 total, 0 operator actions
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 4,
                        "prs_merged": 4,
                        "operator_action_count": 0,
                        "tasks_created": 4,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 3,
                    "total_tasks_created": 6,
                    "total_prs_merged": 6,
                    "total_autonomous_prs_merged": 6,
                    "total_operator_actions": 0,
                },
            },
            {
                "id": "dir-above-with-ops",
                "text": "Above threshold but has operator action",
                "status": "active",
                # 12 autonomous merges total, but 1 operator action → zero-tolerance blocks eligibility
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "autonomous_prs_merged": 6,
                        "prs_merged": 6,
                        "operator_action_count": 1,
                        "tasks_created": 6,
                    },
                ],
                "history_aggregate": {
                    "total_cycles": 3,
                    "total_tasks_created": 6,
                    "total_prs_merged": 6,
                    "total_autonomous_prs_merged": 6,
                    "total_operator_actions": 0,
                },
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id in ("dir-at-threshold", "dir-above-with-ops"):
        db_session.add(
            make_task(
                project_id=project.id,
                title="Research task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}

    at_threshold = by_id["dir-at-threshold"]
    assert at_threshold["lifetime_autonomous_prs_merged"] == 10  # 4 + 6
    assert at_threshold["lifetime_operator_actions"] == 0
    assert at_threshold["retirement_eligible"] is True

    above_with_ops = by_id["dir-above-with-ops"]
    assert above_with_ops["lifetime_autonomous_prs_merged"] == 12  # 6 + 6
    assert above_with_ops["lifetime_operator_actions"] == 1
    assert (
        above_with_ops["retirement_eligible"] is False
    )  # zero-tolerance: any operator action blocks


@pytest.mark.asyncio
async def test_digest_direction_progress_evicted_findings_count(
    client: AsyncClient, db_session
):
    """evicted_findings_count surfaces len(history_aggregate.evicted_findings) per direction."""
    project = make_project(
        directions=[
            {
                "id": "dir-with-evicted",
                "text": "Direction with evicted findings",
                "status": "active",
                "history_aggregate": {
                    "evicted_findings": [
                        {"title": "evicted 1"},
                        {"title": "evicted 2"},
                        {"title": "evicted 3"},
                    ],
                },
            },
            {
                "id": "dir-no-evicted",
                "text": "Direction with empty evicted list",
                "status": "active",
                "history_aggregate": {"evicted_findings": []},
            },
            {
                "id": "dir-no-aggregate",
                "text": "Direction with no history_aggregate",
                "status": "active",
            },
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for dir_id in ("dir-with-evicted", "dir-no-evicted", "dir-no-aggregate"):
        db_session.add(
            make_task(
                project_id=project.id,
                title="Research task",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": dir_id},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    by_id = {dp["direction_id"]: dp for dp in response.json()["direction_progress"]}

    assert by_id["dir-with-evicted"]["evicted_findings_count"] == 3
    assert by_id["dir-no-evicted"]["evicted_findings_count"] == 0
    assert by_id["dir-no-aggregate"]["evicted_findings_count"] == 0


@pytest.mark.asyncio
async def test_digest_direction_progress_sorted_by_cycles(
    client: AsyncClient, db_session
):
    """direction_progress list is sorted by cycles_in_window descending."""
    project = make_project(
        directions=[
            {"id": "dir-few", "text": "Few cycles", "status": "active"},
            {"id": "dir-many", "text": "Many cycles", "status": "active"},
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 1 research task for dir-few, 3 for dir-many
    db_session.add(
        make_task(
            project_id=project.id,
            title="Few task 1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-few"},
        )
    )
    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"Many task {i}",
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "research"}],
                context={"direction_id": "dir-many"},
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    ids = [dp["direction_id"] for dp in data["direction_progress"]]
    assert ids.index("dir-many") < ids.index("dir-few")


@pytest.mark.asyncio
async def test_digest_direction_progress_absent_without_project_id(
    client: AsyncClient, db_session
):
    """direction_progress is empty when no project_id is supplied."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["direction_progress"] == []


@pytest.mark.asyncio
async def test_digest_direction_progress_taxonomy_and_failure_titles(
    client: AsyncClient, db_session
):
    """direction_progress aggregates taxonomy_summary and recent_failure_titles across research_history entries in-window."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-tax",
                "text": "Taxonomy direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 3,
                        "taxonomy_summary": {"scope_related": 2, "infra_related": 1},
                        "failed_task_details": [
                            {"title": "failed task A"},
                            {"title": "failed task B"},
                        ],
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "taxonomy_summary": {"scope_related": 1, "code_defect": 3},
                        "failed_task_details": [
                            {"title": "failed task C"},
                            {"title": "failed task D"},
                        ],
                    },
                    # Outside window — must be excluded
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "taxonomy_summary": {"scope_related": 99},
                        "failed_task_details": [{"title": "old failed task"}],
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-tax"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["direction_id"] == "dir-tax"

    # taxonomy counts summed across both in-window entries, old entry excluded
    assert dp["taxonomy_summary"] == {
        "scope_related": 3,
        "infra_related": 1,
        "code_defect": 3,
    }

    # recent_failure_titles: last 3 titles from in-window failed_task_details
    titles = dp["recent_failure_titles"]
    assert titles is not None
    assert len(titles) == 3
    assert "old failed task" not in titles
    assert titles[-1] == "failed task D"


@pytest.mark.asyncio
async def test_digest_direction_progress_type_breakdown(
    client: AsyncClient, db_session
):
    """direction_progress aggregates type_breakdown across in-window research_history entries."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-tb",
                "text": "Type breakdown direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 3,
                        "type_breakdown": {
                            "create": {"created": 2, "merged": 1, "failed": 1},
                            "fix": {"created": 1, "merged": 0, "failed": 0},
                        },
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "type_breakdown": {
                            "create": {"created": 1, "merged": 1, "failed": 0},
                            "review": {"created": 2, "merged": 0, "failed": 2},
                        },
                    },
                    # Outside window — must be excluded
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "type_breakdown": {
                            "create": {"created": 99, "merged": 99, "failed": 99},
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-tb"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["direction_id"] == "dir-tb"

    tb = dp["type_breakdown"]
    assert tb is not None
    assert tb["create"] == {"created": 3, "merged": 2, "failed": 1}
    assert tb["fix"] == {"created": 1, "merged": 0, "failed": 0}
    assert tb["review"] == {"created": 2, "merged": 0, "failed": 2}


@pytest.mark.asyncio
async def test_digest_direction_progress_decomposition_summary(
    client: AsyncClient, db_session
):
    """decomposition_summary aggregates decomposition_quality across in-window research_history entries."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-dq",
                "text": "Decomp direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 2,
                        "decomposition_quality": [
                            {
                                "plan_task_id": "p1",
                                "subtask_count": 3,
                                "subtask_merge_rate": 0.5,
                                "cross_module_count": 1,
                            },
                            {
                                "plan_task_id": "p2",
                                "subtask_count": 5,
                                "subtask_merge_rate": None,
                                "cross_module_count": 0,
                            },
                        ],
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 1,
                        "tasks_created": 1,
                        "decomposition_quality": [
                            {
                                "plan_task_id": "p3",
                                "subtask_count": 2,
                                "subtask_merge_rate": 1.0,
                                "cross_module_count": 3,
                            },
                        ],
                    },
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 0,
                        "decomposition_quality": [
                            {
                                "plan_task_id": "p_old",
                                "subtask_count": 99,
                                "subtask_merge_rate": 0.0,
                                "cross_module_count": 99,
                            },
                        ],
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-dq"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    summary = dp["decomposition_summary"]
    assert summary is not None
    assert summary["plans_assessed"] == 3
    # merge rate averaged over the 2 non-None values (0.5, 1.0) => 0.75
    assert summary["avg_subtask_merge_rate"] == 0.75
    # subtask_count across 3 plans: (3+5+2)/3 ≈ 3.33
    assert summary["avg_subtask_count"] == 3.33
    # cross_module across 3 plans: (1+0+3)/3 ≈ 1.33
    assert summary["avg_cross_module_count"] == 1.33


@pytest.mark.asyncio
async def test_digest_direction_progress_taxonomy_absent_when_no_entries(
    client: AsyncClient, db_session
):
    """taxonomy_summary and recent_failure_titles are null when research_history entries omit those fields."""
    recent_ts = (_utcnow() - timedelta(hours=5)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-plain",
                "text": "No taxonomy direction",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts, "prs_merged": 1, "tasks_created": 2},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-plain"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    assert dp_list[0]["taxonomy_summary"] is None
    assert dp_list[0]["recent_failure_titles"] is None
    assert dp_list[0]["stage_dropout"] is None


@pytest.mark.asyncio
async def test_digest_direction_progress_stage_dropout(client: AsyncClient, db_session):
    """direction_progress aggregates stage_dropout across in-window research_history entries."""
    recent_ts_1 = (_utcnow() - timedelta(hours=10)).isoformat()
    recent_ts_2 = (_utcnow() - timedelta(hours=5)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=50)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-drop",
                "text": "Dropout direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts_1,
                        "prs_merged": 1,
                        "tasks_created": 3,
                        "stage_dropout": {"review": 2, "ci": 1},
                    },
                    {
                        "timestamp": recent_ts_2,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "stage_dropout": {"review": 1, "agent": 4},
                    },
                    # Outside window — must be excluded
                    {
                        "timestamp": old_ts,
                        "prs_merged": 0,
                        "tasks_created": 1,
                        "stage_dropout": {"review": 99},
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-drop"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["direction_id"] == "dir-drop"
    # Stage counts summed across in-window entries only; old entry's {"review": 99} excluded.
    assert dp["stage_dropout"] == {"review": 3, "ci": 1, "agent": 4}


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_by_scope_and_status(
    client: AsyncClient, db_session
):
    """direction_progress entries include cost_by_scope and cost_by_status breakdowns."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks: small/completed (on-demand, 3600s) and medium/failed (spot, 1800s)
    db_session.add(
        make_task(
            project_id=project.id,
            title="t1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t2",
            status=TaskStatus.FAILED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "medium"},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_by_scope"] is not None
    assert "small" in dp["cost_by_scope"]
    assert "medium" in dp["cost_by_scope"]
    assert dp["cost_by_scope"]["small"] > 0
    assert dp["cost_by_status"] is not None
    assert "COMPLETED" in dp["cost_by_status"]
    assert "FAILED" in dp["cost_by_status"]
    total = round(dp["cost_by_scope"]["small"] + dp["cost_by_scope"]["medium"], 2)
    assert total == dp["estimated_cost_usd"]


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_budget_fields(
    client: AsyncClient, db_session, mocker
):
    """direction_progress exposes budget utilization and fires alert when >=80%."""
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=mocker.AsyncMock,
    )
    now = _utcnow()
    project = make_project(
        directions=[
            {
                "id": "dir-b",
                "text": "Budgeted dir",
                "status": "active",
                "cost_budget_usd": 0.01,  # 1 hour spot = $0.02 -> 200% utilization
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="t",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-b", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_budget_usd"] == 0.01
    assert dp["budget_utilization_pct"] is not None
    assert dp["budget_utilization_pct"] >= 80
    assert dp["budget_exceeded"] is True

    # Allow the scheduled ensure_future task to run.
    import asyncio as _a

    await _a.sleep(0)
    assert dispatch_mock.await_count == 1
    args, _kwargs = dispatch_mock.call_args
    assert args[1] == "BUDGET_EXCEEDED"
    assert args[2]["direction_id"] == "dir-b"


@pytest.mark.asyncio
async def test_digest_direction_progress_no_budget_fields(
    client: AsyncClient, db_session
):
    """When no cost_budget_usd is set, estimated_cost_usd is populated but utilization fields are null."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-nb", "text": "No budget dir", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="t",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-nb", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_budget_usd"] is None
    assert dp["budget_utilization_pct"] is None
    assert dp["budget_exceeded"] is False
    assert dp["estimated_cost_usd"] is not None
    assert dp["estimated_cost_usd"] > 0


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_by_provider_model(
    client: AsyncClient, db_session
):
    """direction_progress entries include cost_by_provider_model breakdown
    keyed by `<provider>|<model>`; per-provider sums match estimated_cost_usd."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-pm", "text": "PM split", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks split across (provider, model). One missing both fields buckets
    # under "unknown|unknown".
    db_session.add(
        make_task(
            project_id=project.id,
            title="t-anth",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-pm", "scope": "small"},
            provider="anthropic",
            model="claude-opus-4-7",
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t-oai",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-pm", "scope": "small"},
            provider="openai",
            model="gpt-5.4",
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t-legacy",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-pm", "scope": "small"},
            # No provider/model — legacy row should bucket under "unknown|unknown".
            vm_provisioned_at=now - timedelta(minutes=20),
            vm_stopped_at=now - timedelta(minutes=10),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    by_pm = dp["cost_by_provider_model"]
    assert by_pm is not None
    assert "anthropic|claude-opus-4-7" in by_pm
    assert "openai|gpt-5.4" in by_pm
    assert "unknown|unknown" in by_pm
    for v in by_pm.values():
        assert v > 0
    # Sum of provider/model breakdown matches the direction's overall cost.
    assert round(sum(by_pm.values()), 2) == dp["estimated_cost_usd"]


@pytest.mark.asyncio
async def test_digest_direction_progress_cost_by_type(client: AsyncClient, db_session):
    """direction_progress entries include cost_by_type breakdown keyed by task type."""
    now = _utcnow()
    project = make_project(
        directions=[{"id": "dir-1", "text": "D1", "status": "active"}]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="t1",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="t2",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-1", "scope": "small"},
            vm_provisioned_at=now - timedelta(minutes=60),
            vm_stopped_at=now - timedelta(minutes=30),
            vm_spot=True,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["cost_by_type"] is not None
    assert "research" in dp["cost_by_type"]
    assert "create" in dp["cost_by_type"]
    assert dp["cost_by_type"]["research"] > 0
    assert dp["cost_by_type"]["create"] > 0
    total = round(dp["cost_by_type"]["research"] + dp["cost_by_type"]["create"], 2)
    assert total == dp["estimated_cost_usd"]


@pytest.mark.asyncio
async def test_digest_direction_progress_filter_funnel(client: AsyncClient, db_session):
    """filter_funnel aggregates findings_produced, filter_breakdown, and batch_cap from research_history."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 3,
                        "findings_produced": 10,
                        "batch_cap_applied": 2,
                        "filter_breakdown": {
                            "hash_dedup": 1,
                            "word_overlap_dedup": 2,
                            "file_overlap_dedup": 1,
                            "type_filtered": 0,
                            "empty_prompt": 1,
                        },
                    },
                    {
                        "timestamp": recent_ts,
                        "tasks_created": 2,
                        "findings_produced": 4,
                        "batch_cap_applied": 0,
                        "filter_breakdown": {
                            "hash_dedup": 1,
                            "word_overlap_dedup": 0,
                            "file_overlap_dedup": 0,
                            "type_filtered": 1,
                            "empty_prompt": 0,
                        },
                    },
                    {
                        # Outside window — must not contribute
                        "timestamp": old_ts,
                        "tasks_created": 99,
                        "findings_produced": 99,
                        "batch_cap_applied": 99,
                        "filter_breakdown": {
                            "hash_dedup": 99,
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    ff = data["direction_progress"][0]["filter_funnel"]
    assert ff is not None
    assert ff["findings_produced"] == 14
    assert ff["hash_dedup"] == 2
    assert ff["word_overlap"] == 2
    assert ff["file_overlap"] == 1
    assert ff["type_filtered"] == 1
    assert ff["empty_prompt"] == 1
    assert ff["batch_cap"] == 2
    assert ff["tasks_created"] == 5

    # Pipeline-level aggregate mirrors per-direction totals when only one direction has activity
    pff = data["pipeline_filter_funnel"]
    assert pff is not None
    assert pff["findings_produced"] == 14
    assert pff["hash_dedup"] == 2
    assert pff["batch_cap"] == 2
    assert pff["tasks_created"] == 5


@pytest.mark.asyncio
async def test_digest_direction_progress_challenge_summary(
    client: AsyncClient, db_session
):
    """finding_challenge_summary aggregates bounded in-window challenge telemetry."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction 1",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "finding_challenge_summary": {
                            "challenged_findings": 2,
                            "agreement_distribution": {
                                CHALLENGE_AGREES: 1,
                                CHALLENGE_DISAGREES: 0,
                                CHALLENGE_INDETERMINATE: 1,
                            },
                            "boundary_distribution": {
                                CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 1,
                                CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 1,
                                CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 0,
                            },
                            "fail_closed_unparseable": 1,
                        },
                    },
                    {
                        "timestamp": recent_ts,
                        "finding_challenge_summary": {
                            "challenged_findings": 1,
                            "agreement_distribution": {
                                CHALLENGE_AGREES: 0,
                                CHALLENGE_DISAGREES: 1,
                                CHALLENGE_INDETERMINATE: 0,
                            },
                            "boundary_distribution": {
                                CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 0,
                                CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 0,
                                CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 1,
                            },
                            "fail_closed_unparseable": 0,
                        },
                    },
                    {
                        "timestamp": old_ts,
                        "finding_challenge_summary": {
                            "challenged_findings": 99,
                            "agreement_distribution": {
                                CHALLENGE_AGREES: 99,
                                CHALLENGE_DISAGREES: 99,
                                CHALLENGE_INDETERMINATE: 99,
                            },
                            "boundary_distribution": {
                                CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 99,
                                CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 99,
                                CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 99,
                            },
                            "fail_closed_unparseable": 99,
                        },
                    },
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    summary = data["direction_progress"][0]["finding_challenge_summary"]
    assert summary == {
        "challenged_findings": 3,
        "agreement_distribution": {
            CHALLENGE_AGREES: 1,
            CHALLENGE_DISAGREES: 1,
            CHALLENGE_INDETERMINATE: 1,
        },
        "boundary_distribution": {
            CHALLENGE_BOUNDARY_REPRODUCE_ABSENT: 1,
            CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY: 1,
            CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH: 1,
        },
        "fail_closed_unparseable": 1,
    }


@pytest.mark.asyncio
async def test_digest_direction_progress_cpm_threshold(client: AsyncClient, db_session):
    """median_cost_per_merged_pr is calculated correctly and directions exceeding 3x median are flagged."""
    from app.routers.admin_analytics import SPOT_RATE

    dir_configs = [
        ("dir-a", 10.0),
        ("dir-b", 20.0),
        ("dir-c", 100.0),
    ]

    recent_ts = (_utcnow() - timedelta(minutes=1)).isoformat()
    project = make_project(
        directions=[
            {
                "id": did,
                "text": did,
                "status": "active",
                "research_history": [{"timestamp": recent_ts, "prs_merged": 1}],
            }
            for did, cost in dir_configs
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for did, cost in dir_configs:
        # t_cost = SPOT_RATE * duration_sec => duration_sec = t_cost / SPOT_RATE
        duration_sec = cost / SPOT_RATE
        now = _utcnow()
        t = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_spot=True,
            vm_provisioned_at=now - timedelta(seconds=duration_sec),
            vm_started_at=now - timedelta(seconds=duration_sec),
            vm_stopped_at=now,
            context={
                "direction_id": did,
                "pr_merged": True,
            },
            payloads=[{"type": "research"}],
        )
        db_session.add(t)

    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["median_cost_per_merged_pr"] == 20.0

    dp_by_id = {dp["direction_id"]: dp for dp in data["direction_progress"]}
    assert dp_by_id["dir-a"]["cost_per_merged_pr"] == 10.0
    assert dp_by_id["dir-a"]["cost_per_merge_exceeds_threshold"] is False

    assert dp_by_id["dir-b"]["cost_per_merged_pr"] == 20.0
    assert dp_by_id["dir-b"]["cost_per_merge_exceeds_threshold"] is False

    assert dp_by_id["dir-c"]["cost_per_merged_pr"] == 100.0
    assert dp_by_id["dir-c"]["cost_per_merge_exceeds_threshold"] is True


@pytest.mark.asyncio
async def test_digest_direction_progress_operator_action_count(
    client: AsyncClient, db_session
):
    """operator_action_count is summed from research_history entries within the window."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()
    old_ts = (_utcnow() - timedelta(hours=48)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-1",
                "text": "Direction A",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 2,
                        "autonomous_prs_merged": 1,
                        "assisted_prs_merged": 1,
                        "operator_action_count": 3,
                    },
                    {"timestamp": old_ts, "prs_merged": 5, "operator_action_count": 10},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-1"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp = data["direction_progress"][0]
    assert dp["operator_action_count"] == 3  # only recent entry
    assert dp["autonomous_prs_merged"] == 1
    assert dp["assisted_prs_merged"] == 1


@pytest.mark.asyncio
async def test_digest_direction_progress_legacy_autonomous_sum(
    client: AsyncClient, db_session
):
    """Multiple legacy research_history entries (prs_merged only, no autonomous_prs_merged)
    within the window: autonomous_prs_merged equals the sum of all prs_merged values."""
    recent_ts1 = (_utcnow() - timedelta(hours=2)).isoformat()
    recent_ts2 = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-legacy-sum",
                "text": "Legacy sum direction",
                "status": "active",
                "research_history": [
                    {"timestamp": recent_ts1, "prs_merged": 3},
                    {"timestamp": recent_ts2, "prs_merged": 5},
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-legacy-sum"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    # Both entries lack autonomous_prs_merged → fallback sums prs_merged values (3 + 5 = 8)
    assert dp["autonomous_prs_merged"] == 8
    assert dp["assisted_prs_merged"] == 0
    assert dp["prs_merged"] == 8


@pytest.mark.asyncio
async def test_digest_direction_progress_empty_history_sane_defaults(
    client: AsyncClient, db_session
):
    """Direction with a task created in the window but empty research_history appears in
    direction_progress with autonomous_prs_merged=0 and cost_per_merged_pr=None."""
    project = make_project(
        directions=[
            {
                "id": "dir-empty",
                "text": "Empty history direction",
                "status": "active",
                "research_history": [],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Create task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create"}],
            context={"direction_id": "dir-empty"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp_list = response.json()["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]
    assert dp["autonomous_prs_merged"] == 0
    assert dp["cost_per_merged_pr"] is None


@pytest.mark.asyncio
async def test_digest_direction_progress_zero_autonomous_with_operator_actions(
    client: AsyncClient, db_session
):
    """When prs_merged=0 (no merges at all) but operator_action_count>0, cost_per_merged_pr
    is None (not zero, not infinity — division is guarded on the total merged count)."""
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-op",
                "text": "Operator-assisted direction",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 0,
                        "autonomous_prs_merged": 0,
                        "assisted_prs_merged": 2,
                        "operator_action_count": 3,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-op"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["autonomous_prs_merged"] == 0
    assert dp["operator_action_count"] == 3
    assert dp["cost_per_merged_pr"] is None


@pytest.mark.asyncio
async def test_cost_per_merged_pr_canonical_population_mixed_autonomous_assisted(
    client: AsyncClient, db_session
):
    """cost_per_merged_pr uses the FULL merged population (autonomous + assisted), not
    autonomous-only.

    A direction with 2 autonomous merges and 1 assisted merge (total=3) and a
    known total cost must report cost_per_merged_pr = total_cost / 3, NOT
    total_cost / 2. Using the wrong (autonomous-only) denominator would inflate
    the metric and could trigger a spurious cost_per_merge_exceeds_threshold flag
    for a direction that merely accepted operator help.
    """
    from app.routers.admin_analytics import SPOT_RATE

    # 3000-second spot task → cost = SPOT_RATE * 3000; large enough that
    # round(cost/2, 4) != round(cost/3, 4) so the population check is meaningful.
    duration_sec = 3000.0
    task_cost = round(SPOT_RATE * duration_sec, 4)
    recent_ts = (_utcnow() - timedelta(minutes=1)).isoformat()

    # Direction with 2 autonomous + 1 assisted merge = 3 total merges
    project = make_project(
        directions=[
            {
                "id": "dir-mixed",
                "text": "Mixed autonomous and assisted merges",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        "prs_merged": 3,
                        "autonomous_prs_merged": 2,
                        "assisted_prs_merged": 1,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_spot=True,
            vm_provisioned_at=now - timedelta(seconds=duration_sec),
            vm_started_at=now - timedelta(seconds=duration_sec),
            vm_stopped_at=now,
            context={"direction_id": "dir-mixed"},
            payloads=[{"type": "research"}],
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    dp_list = data["direction_progress"]
    assert len(dp_list) == 1
    dp = dp_list[0]

    # Canonical formula: total_cost / total_merged (autonomous + assisted = 3)
    expected_cpm = round(task_cost / 3, 4)
    wrong_autonomous_only_cpm = round(task_cost / 2, 4)

    assert dp["cost_per_merged_pr"] == expected_cpm, (
        f"cost_per_merged_pr should use full merged population (total=3), "
        f"got {dp['cost_per_merged_pr']!r} — expected {expected_cpm!r}, "
        f"autonomous-only (wrong) would have been {wrong_autonomous_only_cpm!r}"
    )
    assert dp["cost_per_merged_pr"] != wrong_autonomous_only_cpm, (
        "cost_per_merged_pr is using autonomous-only denominator — population mismatch"
    )

    # A direction this cheap should NOT be flagged cost-inefficient
    assert dp.get("cost_per_merge_exceeds_threshold") is False, (
        "Direction should not be flagged cost-inefficient with such low CPM"
    )


@pytest.mark.asyncio
async def test_digest_direction_progress_tz_aware_timestamp_counted(
    client: AsyncClient, db_session
):
    """research_history entries with tz-aware (UTC+Z) timestamps are counted, not dropped or raising TypeError."""
    project = make_project(
        directions=[
            {
                "id": "dir-tz-aware",
                "text": "Direction with tz-aware timestamp",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": (
                            datetime.now(timezone.utc) - timedelta(hours=5)
                        ).isoformat(),
                        "prs_merged": 3,
                        "tasks_created": 6,
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-tz-aware"},
        )
    )
    await db_session.commit()

    # Must not raise TypeError for tz-aware vs naive comparison
    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    # prs_merged/tasks_created should be counted from the tz-aware entry
    assert dp["prs_merged"] == 3
    assert dp["conversion_rate"] == 0.5  # 3 merged / 6 created


@pytest.mark.asyncio
async def test_conversion_rate_excludes_non_pr_task_types(
    client: AsyncClient, db_session
):
    """Conversion rate denominator excludes research/investigate/security_audit/memo tasks that never open PRs."""
    project = make_project(
        directions=[
            {
                "id": "dir-mixed-types",
                "text": "Direction with mixed task types",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": (
                            datetime.now(timezone.utc) - timedelta(hours=5)
                        ).isoformat(),
                        "prs_merged": 3,
                        "tasks_created": 8,
                        # 8 total tasks: 3 create (PR-eligible),
                        # 2 research + 2 security_audit + 1 memo (all in READ_ONLY_TASK_TYPES = non-PR)
                        "type_breakdown": {
                            "create": {"created": 3, "merged": 3, "failed": 0},
                            "research": {"created": 2, "merged": 0, "failed": 0},
                            "security_audit": {"created": 2, "merged": 0, "failed": 0},
                            "memo": {"created": 1, "merged": 0, "failed": 0},
                        },
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-mixed-types"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    # conversion_rate should be 3 merged / 3 PR-eligible (create tasks only)
    # Not 3 / 8 (which would be the old incorrect behavior).
    # security_audit, research, memo are all in READ_ONLY_TASK_TYPES (= PR_INELIGIBLE_TASK_TYPES).
    assert dp["prs_merged"] == 3
    assert dp["conversion_rate"] == 1.0  # 3 merged / 3 PR-eligible


@pytest.mark.asyncio
async def test_conversion_rate_excludes_degraded_unverified_merges(
    client: AsyncClient, db_session
):
    """Conversion (window + lifetime) excludes degraded-unverified merges.

    3 PRs merged from 4 created, but only 2 verified (1 degraded-unverified).
    conversion_rate must be 2/4=0.5, not 3/4=0.75 — matching the per-task layer.
    The raw prs_merged count stays 3 (back-compat).
    """
    project = make_project(
        directions=[
            {
                "id": "dir-degraded",
                "text": "Direction with a degraded-unverified merge",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": (
                            datetime.now(timezone.utc) - timedelta(hours=5)
                        ).isoformat(),
                        "prs_merged": 3,
                        "verified_prs_merged": 2,
                        "tasks_created": 4,
                    }
                ],
                "history_aggregate": {
                    "total_cycles": 1,
                    "total_prs_merged": 3,
                    "total_verified_prs_merged": 2,
                    "total_tasks_created": 4,
                },
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-degraded"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["prs_merged"] == 3
    assert dp["conversion_rate"] == 0.5  # 2 verified / 4 created
    assert dp["lifetime_conversion_rate"] == 0.5  # window + aggregate verified


@pytest.mark.asyncio
async def test_retirement_critiques_returns_critiques(client: AsyncClient, db_session):
    """Critiques stored in StrategicCycle.cycle_metadata are returned, newest cycle first."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)

    cycle = StrategicCycle(
        id="cycle-audit-1",
        project_id=project.id,
        cycle_metadata={
            "retirement_critiques": [
                {
                    "direction_id": "dir-abc",
                    "what_it_addressed": "Reduce DB queries",
                    "pattern_that_produced_it": "Overly broad direction",
                    "lesson": "Scope directions tightly",
                },
                {
                    "direction_id": "dir-def",
                    "what_it_addressed": "Improve test coverage",
                    "pattern_that_produced_it": "Coverage gaps",
                    "lesson": "Target specific modules",
                },
            ]
        },
    )
    db_session.add(cycle)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/retirement-critiques"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    critiques = data["critiques"]
    assert len(critiques) == 2
    assert critiques[0]["direction_id"] == "dir-abc"
    assert critiques[0]["what_it_addressed"] == "Reduce DB queries"
    assert critiques[0]["lesson"] == "Scope directions tightly"
    assert critiques[0]["cycle_id"] == "cycle-audit-1"
    assert critiques[1]["direction_id"] == "dir-def"


@pytest.mark.asyncio
async def test_retirement_critiques_empty_when_no_cycles(
    client: AsyncClient, db_session
):
    """Project with no strategic cycles returns empty critiques list."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/retirement-critiques"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["critiques"] == []


@pytest.mark.asyncio
async def test_retirement_critiques_skips_sentinel_entries(
    client: AsyncClient, db_session
):
    """Entries with no_eligible_retirements=True are excluded from results."""
    from app.models import StrategicCycle

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    m = make_membership(project.id)
    db_session.add(m)

    cycle = StrategicCycle(
        id="cycle-audit-sentinel",
        project_id=project.id,
        cycle_metadata={
            "retirement_critiques": [
                {"no_eligible_retirements": True},
                {
                    "direction_id": "dir-real",
                    "what_it_addressed": "Real work",
                    "pattern_that_produced_it": "Pattern",
                    "lesson": "Lesson",
                },
            ]
        },
    )
    db_session.add(cycle)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/retirement-critiques"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["critiques"][0]["direction_id"] == "dir-real"


def test_compute_spawn_gate_aggregate_cause_distribution():
    """compute_spawn_gate_aggregate sums cause_counts across history entries into cause_distribution."""
    from app.digest_direction_progress import compute_spawn_gate_aggregate

    history = [
        {
            "research_spawn_gate_challenged_count": 3,
            "research_spawn_gate_unresolved_count": 2,
            "research_spawn_gate_cause_counts": {"AGENT_TIMEOUT": 2},
        },
        {
            "research_spawn_gate_challenged_count": 2,
            "research_spawn_gate_unresolved_count": 1,
            "research_spawn_gate_cause_counts": {"AGENT_TIMEOUT": 1, "RATE_LIMIT": 1},
        },
    ]
    result = compute_spawn_gate_aggregate(history)
    assert result is not None
    cause_dist = result.get("cause_distribution")
    assert cause_dist is not None
    assert cause_dist["AGENT_TIMEOUT"] == 3
    assert cause_dist["RATE_LIMIT"] == 1


def test_compute_spawn_gate_aggregate_cause_distribution_absent_when_no_cause():
    """cause_distribution is None when no entries carry cause_counts."""
    from app.digest_direction_progress import compute_spawn_gate_aggregate

    history = [
        {
            "research_spawn_gate_challenged_count": 2,
            "research_spawn_gate_survived_count": 1,
        },
    ]
    result = compute_spawn_gate_aggregate(history)
    assert result is not None
    assert result.get("cause_distribution") is None


def test_compute_spawn_gate_aggregate_zero_cause_counts_excluded():
    """cause_distribution excludes categories whose counts are zero (healthy/resolved cycle)."""
    from app.digest_direction_progress import compute_spawn_gate_aggregate

    history = [
        {
            "research_spawn_gate_challenged_count": 2,
            "research_spawn_gate_survived_count": 2,
            "research_spawn_gate_unresolved_count": 0,
            "research_spawn_gate_cause_counts": {"AGENT_TIMEOUT": 0},
        },
    ]
    result = compute_spawn_gate_aggregate(history)
    assert result is not None
    # Zero-count cause must not pollute cause_distribution
    assert result.get("cause_distribution") is None


def test_compute_spawn_gate_aggregate_mixed_cause_counts():
    """cause_distribution only accumulates non-zero counts; zero entries are silently dropped."""
    from app.digest_direction_progress import compute_spawn_gate_aggregate

    history = [
        {
            "research_spawn_gate_challenged_count": 3,
            "research_spawn_gate_unresolved_count": 2,
            "research_spawn_gate_cause_counts": {"AGENT_TIMEOUT": 2, "RATE_LIMIT": 0},
        },
        {
            "research_spawn_gate_challenged_count": 1,
            "research_spawn_gate_unresolved_count": 0,
            "research_spawn_gate_cause_counts": {"AGENT_TIMEOUT": 0},
        },
    ]
    result = compute_spawn_gate_aggregate(history)
    assert result is not None
    cause_dist = result.get("cause_distribution")
    assert cause_dist is not None
    assert cause_dist.get("AGENT_TIMEOUT") == 2
    # RATE_LIMIT had count 0 — must be absent
    assert "RATE_LIMIT" not in cause_dist


# ---------------------------------------------------------------------------
# compute_spawn_gate_census — pure-function tests (no DB required)
# ---------------------------------------------------------------------------


def _make_census_task(
    task_id=None,
    task_type="research",
    challenged_count=0,
    parse_outcome=None,
    cause_counts=None,
    exhausted=False,
    give_up_adversarial=0,
    direction_id=None,
):
    """Build a minimal Task-like object for compute_spawn_gate_census."""
    import uuid as _uuid

    from unittest.mock import MagicMock

    t = MagicMock()
    t.id = task_id or _uuid.uuid4()
    ctx: dict = {}
    if direction_id:
        ctx["direction_id"] = direction_id
    if challenged_count:
        ctx["research_spawn_gate_challenged_count"] = challenged_count
    if parse_outcome:
        ctx["research_spawn_gate_parse_outcome"] = parse_outcome
    if cause_counts:
        ctx["research_spawn_gate_cause_counts"] = cause_counts
    if exhausted:
        ctx["research_spawn_gate_wholesale_retry_exhausted"] = True
    if give_up_adversarial:
        ctx["finding_structural_give_up"] = {
            "by_reason": {"adversarial_gate": give_up_adversarial}
        }
    t.context = ctx
    t.first_payload = {"type": task_type}
    return t


def test_compute_spawn_gate_census_empty():
    """Returns zero aggregates when no research tasks are present."""
    from app.digest_direction_progress import compute_spawn_gate_census
    from datetime import datetime

    result = compute_spawn_gate_census([], window_start=datetime.now(timezone.utc).replace(tzinfo=None))
    assert result["cycles_scanned"] == 0
    assert result["gate_invoked_count"] == 0
    assert result["graders_attempted_total"] == 0
    assert result["exhaustion_count"] == 0
    assert result["give_up_count"] == 0
    assert result["parse_outcome_distribution"] is None
    assert result["cause_distribution"] is None
    assert result["rows"] == []


def test_compute_spawn_gate_census_skips_non_research_tasks():
    """Non-research tasks are excluded from the census."""
    from app.digest_direction_progress import compute_spawn_gate_census
    from datetime import datetime

    tasks = [
        _make_census_task(task_type="create", challenged_count=3),
        _make_census_task(task_type="research", challenged_count=2),
    ]
    result = compute_spawn_gate_census(tasks, window_start=datetime.now(timezone.utc).replace(tzinfo=None))
    assert result["cycles_scanned"] == 1
    assert result["graders_attempted_total"] == 2


def test_compute_spawn_gate_census_deduplicates_by_task_id():
    """Duplicate task objects with the same id are counted once."""
    from app.digest_direction_progress import compute_spawn_gate_census
    from datetime import datetime
    import uuid

    tid = uuid.uuid4()
    t1 = _make_census_task(task_id=tid, challenged_count=3)
    t2 = _make_census_task(task_id=tid, challenged_count=3)
    result = compute_spawn_gate_census([t1, t2], window_start=datetime.now(timezone.utc).replace(tzinfo=None))
    assert result["cycles_scanned"] == 1
    assert result["gate_invoked_count"] == 1


def test_compute_spawn_gate_census_aggregates_trail_and_cause():
    """Cause distribution and parse_outcome_distribution accumulate across tasks."""
    from app.digest_direction_progress import compute_spawn_gate_census
    from datetime import datetime

    tasks = [
        _make_census_task(
            challenged_count=3,
            parse_outcome="no_verdict_returned",
            cause_counts={"AGENT_TIMEOUT": 2},
            exhausted=True,
        ),
        _make_census_task(
            challenged_count=2,
            parse_outcome="no_verdict_returned",
            cause_counts={"AGENT_TIMEOUT": 1, "RATE_LIMIT": 1},
        ),
        _make_census_task(
            challenged_count=1,
            parse_outcome="verdict_parsed",
            cause_counts=None,
        ),
    ]
    result = compute_spawn_gate_census(tasks, window_start=datetime.now(timezone.utc).replace(tzinfo=None))
    assert result["cycles_scanned"] == 3
    assert result["gate_invoked_count"] == 3
    assert result["graders_attempted_total"] == 6
    assert result["exhaustion_count"] == 1
    cause_dist = result["cause_distribution"]
    assert cause_dist is not None
    assert cause_dist["AGENT_TIMEOUT"] == 3
    assert cause_dist["RATE_LIMIT"] == 1
    parse_dist = result["parse_outcome_distribution"]
    assert parse_dist is not None
    assert parse_dist["no_verdict_returned"] == 2
    assert parse_dist["verdict_parsed"] == 1


def test_compute_spawn_gate_census_covers_undirected_tasks():
    """Research tasks without a direction_id are included in the census."""
    from app.digest_direction_progress import compute_spawn_gate_census
    from datetime import datetime

    tasks = [
        _make_census_task(challenged_count=2, direction_id=None),
        _make_census_task(challenged_count=1, direction_id="dir-1"),
    ]
    result = compute_spawn_gate_census(tasks, window_start=datetime.now(timezone.utc).replace(tzinfo=None))
    assert result["cycles_scanned"] == 2
    directions = [r["direction_id"] for r in result["rows"]]
    assert None in directions
    assert "dir-1" in directions


def test_compute_spawn_gate_census_give_up_count():
    """give_up_count totals adversarial_gate give-ups from finding_structural_give_up."""
    from app.digest_direction_progress import compute_spawn_gate_census
    from datetime import datetime

    tasks = [
        _make_census_task(give_up_adversarial=3),
        _make_census_task(give_up_adversarial=2),
    ]
    result = compute_spawn_gate_census(tasks, window_start=datetime.now(timezone.utc).replace(tzinfo=None))
    assert result["give_up_count"] == 5


def test_compute_spawn_gate_census_gate_not_invoked_when_no_challenged():
    """gate_invoked is False when challenged_count is zero and trail is empty."""
    from app.digest_direction_progress import compute_spawn_gate_census
    from datetime import datetime

    tasks = [_make_census_task(challenged_count=0)]
    result = compute_spawn_gate_census(tasks, window_start=datetime.now(timezone.utc).replace(tzinfo=None))
    assert result["gate_invoked_count"] == 0
    assert result["rows"][0]["gate_invoked"] is False


@pytest.mark.asyncio
async def test_conversion_denominator_excludes_knowledge_and_compute_children(
    client: AsyncClient, db_session
):
    """conversion_rate denominator excludes web_research, design, compute_experiment tasks.

    A direction that emits 2 knowledge findings (web_research + design) and 2
    implementation findings (create), with both implementations merged, must report
    conversion_rate=1.0 (2/2), not 0.5 (2/4).  This is the acceptance gate for the
    'fix: exclude never-PR child types from conversion stats' change.
    """
    from app.digest_direction_progress import pr_ineligible_created_in_entry

    entry = {
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "prs_merged": 2,
        "verified_prs_merged": 2,
        "tasks_created": 4,
        "type_breakdown": {
            "create": {"created": 2, "merged": 2, "failed": 0},
            "web_research": {"created": 1, "merged": 0, "failed": 0},
            "design": {"created": 1, "merged": 0, "failed": 0},
        },
    }

    # pr_ineligible_created_in_entry must count web_research + design as ineligible
    assert pr_ineligible_created_in_entry(entry) == 2

    project = make_project(
        directions=[
            {
                "id": "dir-knowledge",
                "text": "Direction with knowledge children",
                "status": "active",
                "research_history": [entry],
                "history_aggregate": {
                    "total_cycles": 1,
                    "total_prs_merged": 1,
                    "total_verified_prs_merged": 1,
                    "total_tasks_created": 3,
                    "total_pr_ineligible_tasks_created": 2,
                },
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research cycle",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-knowledge"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    # 2 merged / 2 PR-eligible (create tasks only) = 1.0, NOT 2/4 = 0.5.
    # Lifetime uses the same denominator, including folded aggregate ineligible counts:
    # (2 + 1) verified merges / ((4 + 3) created - (2 + 2) ineligible) = 1.0.
    assert dp["prs_merged"] == 2
    assert dp["conversion_rate"] == 1.0
    assert dp["lifetime_conversion_rate"] == 1.0
    assert dp["pr_eligible_tasks_created"] == 2

    # Also verify compute_experiment is excluded
    entry_ce = {
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        "prs_merged": 0,
        "tasks_created": 3,
        "type_breakdown": {
            "compute_experiment": {"created": 2, "merged": 0, "failed": 0},
            "fix": {"created": 1, "merged": 0, "failed": 0},
        },
    }
    assert pr_ineligible_created_in_entry(entry_ce) == 2


@pytest.mark.asyncio
async def test_digest_direction_progress_budget_alert_stamped_in_db_and_idempotent(
    client: AsyncClient, db_session, mocker
):
    """budget alert writes _budget_alerted via a locked re-fetch (not the stale snapshot).

    After the first digest call:
    - _budget_alerted is True in the DB row (confirms the locked write path executed).
    - A second call does NOT re-fire the BUDGET_EXCEEDED notification (idempotency).
    """
    import asyncio as _a

    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=mocker.AsyncMock,
    )
    now = _utcnow()
    project = make_project(
        directions=[
            {
                "id": "dir-locked",
                "text": "Locked budget dir",
                "status": "active",
                "cost_budget_usd": 0.01,  # spot task for 1h ≈ $0.02 → 200% utilization
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="t-budget",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-locked", "scope": "small"},
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=True,
        )
    )
    await db_session.commit()

    # First call — should fire the alert.
    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    dp = response.json()["direction_progress"][0]
    assert dp["budget_utilization_pct"] >= 80

    await _a.sleep(0)  # let ensure_future run
    assert dispatch_mock.await_count == 1
    assert dispatch_mock.call_args[0][1] == "BUDGET_EXCEEDED"

    # The locked write should have persisted _budget_alerted to the DB row.
    await db_session.refresh(project)
    dir_map = {str(d.get("id") or d.get("direction_id")): d for d in project.directions}
    assert dir_map["dir-locked"].get("_budget_alerted") is True, (
        "_budget_alerted was not persisted to DB — locked write path did not execute"
    )
    # Release the db_session transaction before the second request. StaticPool
    # shares a single SQLite connection; an open DEFERRED transaction on
    # db_session would deadlock writes from dedicated-session helpers inside the
    # second request if not closed first. Snapshot the id first: the rollback
    # expires ``project``, so a later ``project.id`` lazy-load would raise
    # MissingGreenlet under real ORM expiry.
    project_id = project.id
    await db_session.rollback()

    # Second call — _budget_alerted is now True in DB, so no re-fire.
    dispatch_mock.reset_mock()
    response2 = await client.get(f"/api/admin/digest?project_id={project_id}&hours=24")
    assert response2.status_code == 200
    await _a.sleep(0)
    assert dispatch_mock.await_count == 0, (
        "BUDGET_EXCEEDED fired again despite _budget_alerted=True in DB"
    )



@pytest.mark.asyncio
async def test_conversion_rate_not_deflated_without_type_breakdown(
    client: AsyncClient, db_session
):
    """Conversion rate is correctly computed even when type_breakdown is not yet written.

    During the (potentially multi-hour) execution window before the first finding task
    reaches terminal status, type_breakdown has not been written to the entry. Without
    this fix, the PR-ineligible task reduction would be skipped (since type_breakdown is
    missing), causing conversion_rate to be deflated by including tasks that can never
    open PRs (research, investigate, audit, memo) in the denominator.

    This test verifies that conversion_rate is computed correctly using tasks_created
    directly, even when type_breakdown is absent (fresh/ongoing cycle).
    """
    recent_ts = (_utcnow() - timedelta(hours=1)).isoformat()

    project = make_project(
        directions=[
            {
                "id": "dir-fresh",
                "text": "Direction with fresh cycle (no type_breakdown yet)",
                "status": "active",
                "research_history": [
                    {
                        "timestamp": recent_ts,
                        # 2 verified PRs merged from this cycle
                        "prs_merged": 2,
                        "verified_prs_merged": 2,
                        # 5 total finding tasks created (mix of create, research, audit, memo)
                        # But type_breakdown is NOT yet written (empty) because no tasks have
                        # reached terminal status yet.
                        "tasks_created": 5,
                        # type_breakdown is missing — this is the key test case
                        # (fresh cycle where finding tasks are still executing)
                        # Don't set type_breakdown at all
                    }
                ],
            }
        ]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        make_task(
            project_id=project.id,
            title="Research task",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research"}],
            context={"direction_id": "dir-fresh"},
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project.id}&hours=24")
    assert response.status_code == 200
    data = response.json()

    assert len(data["direction_progress"]) == 1
    dp = data["direction_progress"][0]
    # With type_breakdown missing, conversion_rate should use tasks_created directly.
    # 2 verified PRs / 5 tasks created = 0.4
    # This is more accurate than an inflated rate computed without the PR-ineligible reduction.
    assert dp["prs_merged"] == 2
    assert dp["verified_prs_merged"] == 2
    assert dp["pr_eligible_tasks_created"] == 5
    assert dp["conversion_rate"] == 0.4  # 2 / 5, not deflated
