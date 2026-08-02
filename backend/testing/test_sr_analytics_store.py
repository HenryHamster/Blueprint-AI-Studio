"""Focused tests for the dedicated SR analytics read/write seam."""

import pytest

from app.models import ProjectSRAnalytics
from app.sr_analytics_store import (
    LEGACY_SR_ANALYTICS_KEYS,
    read_project_sr_analytics,
)
from app.task_locking import _RESERVED_DESIGN_DOC_KEYS, with_sr_analytics_locked
from testing.factories import make_project


def test_legacy_sr_fallback_keys_remain_reserved_from_agent_writes():
    assert LEGACY_SR_ANALYTICS_KEYS <= _RESERVED_DESIGN_DOC_KEYS


@pytest.mark.asyncio
async def test_read_project_sr_analytics_merges_legacy_per_key_with_dedicated_winning(
    db_session,
):
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {"source": "legacy"},
        "sr_engagement_threshold": 0.7,
        "unrelated": {"preserved_for_fallback_only": True},
    }
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectSRAnalytics(
            project_id=project.id,
            data={
                "sr_catch_rate_trends": {"source": "dedicated"},
                "edges_trend": {"trend": "improving"},
            },
        )
    )
    await db_session.commit()

    data = await read_project_sr_analytics(db_session, project)

    assert data["sr_catch_rate_trends"] == {"source": "dedicated"}
    assert data["sr_engagement_threshold"] == 0.7
    assert data["edges_trend"] == {"trend": "improving"}
    assert "unrelated" not in data


@pytest.mark.asyncio
async def test_sr_write_lazily_seeds_documented_legacy_keys(db_session):
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {"source": "legacy"},
        "sr_engagement_threshold": 0.7,
        "unrelated": {"must_not_move": True},
    }
    db_session.add(project)
    await db_session.commit()

    def mutate(data):
        data["edges_trend"] = {"source": "dedicated"}

    assert await with_sr_analytics_locked(db_session, project.id, mutate)
    await db_session.commit()

    stored = await db_session.get(ProjectSRAnalytics, project.id)
    assert stored is not None
    assert stored.data == {
        "sr_catch_rate_trends": {"source": "legacy"},
        "sr_engagement_threshold": 0.7,
        "edges_trend": {"source": "dedicated"},
    }


@pytest.mark.asyncio
async def test_sr_write_seeds_missing_legacy_keys_into_partial_row(db_session):
    project = make_project()
    project.design_doc = {
        "sr_catch_rate_trends": {"source": "legacy"},
        "sr_engagement_threshold": 0.7,
    }
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectSRAnalytics(
            project_id=project.id,
            data={"edges_trend": {"source": "dedicated"}},
        )
    )
    await db_session.commit()

    def mutate(data):
        data["sr_regression_risk_test_signal_trends"] = {"source": "new-write"}

    assert await with_sr_analytics_locked(db_session, project.id, mutate)
    await db_session.commit()

    stored = await db_session.get(ProjectSRAnalytics, project.id)
    assert stored is not None
    assert stored.data == {
        "sr_catch_rate_trends": {"source": "legacy"},
        "sr_engagement_threshold": 0.7,
        "edges_trend": {"source": "dedicated"},
        "sr_regression_risk_test_signal_trends": {"source": "new-write"},
    }


@pytest.mark.asyncio
async def test_pipeline_health_reads_sr_metrics_from_dedicated_store(db_session):
    from app.pipeline_health import compute_pipeline_health_summary

    project = make_project()
    project.design_doc = {
        "sr_false_attestation_trends": {"source": "legacy"}
    }
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectSRAnalytics(
            project_id=project.id,
            data={
                "sr_catch_rate_trends": {
                    "computed_at": "2026-07-22T00:00:00",
                    "data_sufficient": True,
                    "categories": {},
                },
                "sr_regression_risk_test_signal_trends": {"source": "dedicated"},
                "sr_false_attestation_trends": {"source": "dedicated"},
                "edges_trend": {
                    "computed_at": "2026-07-22T00:00:00",
                    "current_avg_edges": 3.0,
                    "previous_avg_edges": 2.0,
                    "delta": 1.0,
                    "trend": "increasing",
                    "data_sufficient": True,
                },
            },
        )
    )
    await db_session.commit()

    result = await compute_pipeline_health_summary(db_session, project.id)

    assert result["sr_regression_risk_test_signal_trends"] == {
        "source": "dedicated"
    }
    assert result["sr_false_attestation_trends"] == {"source": "dedicated"}
    assert result["edges_trend"]["trend"] == "increasing"
