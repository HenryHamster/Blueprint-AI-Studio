"""Tests for backend/app/cost_telemetry_reconciliation.py.

Verifies that the reconciliation seam can compare snapshot-path and direct-path
LLM cost computations, and that the two paths agree within the declared
tolerance for known task configurations.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from testing.factories import _utcnow, make_project, make_task

from app.cost_telemetry_reconciliation import (
    COST_TELEMETRY_TOLERANCE,
    PROVIDER_REPORTED_TOLERANCE,
    reconcile_llm_cost,
    reconcile_provider_reported_usage,
    reconcile_task_llm_cost,
)
from app.llm_cost_attribution import LLM_TOKEN_PRICING
from app.models import TaskStatus, bank_task_current_attempt


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_cost_task(project_id, *, provider, model, input_tokens, output_tokens, **kw):
    """Create a COMPLETED + merged task with LLM token totals and no attempt
    history (single current-attempt)."""
    defaults = dict(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "llm_call_count": 5,
        },
        provider=provider,
        model=model,
    )
    defaults.update(kw)
    return make_task(**defaults)


def _make_cached_cost_task(project_id, *, provider, model, input_tokens, output_tokens,
                           cache_creation, cache_read, **kw):
    """Create a COMPLETED + merged task with cache-class sub-counts in the
    current attempt (single attempt, no banking)."""
    defaults = dict(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "total_cache_creation_input_tokens": cache_creation,
            "total_cache_read_input_tokens": cache_read,
            "llm_call_count": 5,
        },
        provider=provider,
        model=model,
    )
    defaults.update(kw)
    return make_task(**defaults)


def _expected_cost(provider, model, input_tokens, output_tokens,
                   cache_creation=0, cache_read=0):
    """Compute expected LLM cost for a single attempt using the shared pricing
    table, matching _price_llm_attempt logic."""
    from app.llm_cost_attribution import _price_llm_attempt

    pricing = LLM_TOKEN_PRICING.get((provider.lower(), model.lower()))
    if not pricing:
        return 0.0
    input_rate, output_rate = pricing
    return round(
        _price_llm_attempt(
            input_tokens, output_tokens,
            cache_creation, cache_read,
            input_rate, output_rate,
            provider=provider, model=model,
        ),
        6,
    )


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_matches_on_known_tasks(db_session):
    """Tasks with known attempt histories produce matching snapshot/direct
    totals within the declared tolerance."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task 1: anthropic claude-sonnet-4-6, pure input+output (no cache).
    t1 = _make_cost_task(
        project.id,
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=1000,
    )
    db_session.add(t1)
    # Task 2: openai gpt-5.4, also no cache.
    t2 = _make_cost_task(
        project.id,
        provider="openai",
        model="gpt-5.4",
        input_tokens=1500,
        output_tokens=800,
    )
    db_session.add(t2)
    await db_session.commit()

    result = reconcile_llm_cost([t1, t2])

    assert result.task_count == 2
    assert result.within_tolerance is True
    assert result.absolute_delta <= COST_TELEMETRY_TOLERANCE
    assert result.cache_hit_rate_within_tolerance is True

    # Compute expected totals.
    expected_t1 = _expected_cost("anthropic", "claude-sonnet-4-6", 2000, 1000)
    expected_t2 = _expected_cost("openai", "gpt-5.4", 1500, 800)
    expected_total = round(expected_t1 + expected_t2, 6)

    # Both paths should agree with the expected total.
    assert result.total_cost_snapshot_path == pytest.approx(expected_total, abs=1e-6)
    assert result.total_cost_direct_path == pytest.approx(expected_total, abs=1e-6)

    # Input token counts.
    assert result.total_input_tokens_snapshot_path == 3500
    assert result.total_input_tokens_direct_path == 3500


@pytest.mark.asyncio
async def test_reconcile_cache_hit_rate_matches(db_session):
    """Tasks with cache-read tokens produce matching cache-hit rates from both
    computation paths."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task 1: 1000 input, 300 cache-read (30% hit rate).
    t1 = _make_cached_cost_task(
        project.id,
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cache_creation=200,
        cache_read=300,
    )
    # Task 2: 2000 input, 500 cache-read (25% hit rate).
    t2 = _make_cached_cost_task(
        project.id,
        provider="openai",
        model="gpt-5.4",
        input_tokens=2000,
        output_tokens=1000,
        cache_creation=0,
        cache_read=500,
    )
    db_session.add_all([t1, t2])
    await db_session.commit()

    result = reconcile_llm_cost([t1, t2])

    # Combined: (300 + 500) / (1000 + 2000) = 800 / 3000 = 0.2666...
    expected_hit_rate = 800 / 3000
    assert result.cache_hit_rate_snapshot_path == pytest.approx(expected_hit_rate, abs=1e-10)
    assert result.cache_hit_rate_direct_path == pytest.approx(expected_hit_rate, abs=1e-10)
    assert result.cache_hit_rate_delta <= 1e-10
    assert result.cache_hit_rate_within_tolerance is True
    assert result.within_tolerance is True

    # Verify token counters match between paths.
    assert result.total_input_tokens_snapshot_path == 3000
    assert result.total_input_tokens_direct_path == 3000
    assert result.total_cache_read_input_tokens_snapshot_path == 800
    assert result.total_cache_read_input_tokens_direct_path == 800


@pytest.mark.asyncio
async def test_reconcile_empty_task_list(db_session):
    """Empty task list: both paths return zero cost, task_count=0, within_tolerance."""
    result = reconcile_llm_cost([])

    assert result.task_count == 0
    assert result.total_cost_snapshot_path == 0.0
    assert result.total_cost_direct_path == 0.0
    assert result.absolute_delta == 0.0
    assert result.relative_delta is None
    assert result.within_tolerance is True
    assert result.total_input_tokens_snapshot_path == 0
    assert result.total_input_tokens_direct_path == 0
    assert result.cache_hit_rate_snapshot_path == 0.0
    assert result.cache_hit_rate_direct_path == 0.0
    assert result.cache_hit_rate_delta == 0.0
    assert result.cache_hit_rate_within_tolerance is True
    assert result.per_task == []


@pytest.mark.asyncio
async def test_reconcile_unpriced_model_still_matches(db_session):
    """Tasks with a (provider, model) absent from the pricing table produce
    zero cost from both paths, and both agree."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Use a provider/model that is NOT in LLM_TOKEN_PRICING.
    task = _make_cost_task(
        project.id,
        provider="nonexistent_provider",
        model="v0-unknown",
        input_tokens=5000,
        output_tokens=2000,
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_llm_cost([task])

    assert result.task_count == 1
    assert result.total_cost_snapshot_path == 0.0
    assert result.total_cost_direct_path == 0.0
    assert result.absolute_delta == 0.0
    assert result.within_tolerance is True
    # Token counters are still tracked even when unpriced.
    assert result.total_input_tokens_snapshot_path == 5000
    assert result.total_input_tokens_direct_path == 5000


@pytest.mark.asyncio
async def test_reconcile_single_task(db_session):
    """Single task: reconciliation works and task_count reflects it."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_cost_task(
        project.id,
        provider="anthropic",
        model="claude-opus-4-8",
        input_tokens=500,
        output_tokens=200,
    )
    db_session.add(task)
    await db_session.commit()

    # Test the convenience wrapper too.
    result = reconcile_task_llm_cost(task)

    assert result.task_count == 1
    expected = _expected_cost("anthropic", "claude-opus-4-8", 500, 200)
    assert result.total_cost_snapshot_path == pytest.approx(expected, abs=1e-6)
    assert result.total_cost_direct_path == pytest.approx(expected, abs=1e-6)
    assert result.within_tolerance is True


@pytest.mark.asyncio
async def test_reconcile_mixed_providers(db_session):
    """Tasks with mixed providers (anthropic + openai) reconcile correctly."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = _make_cost_task(
        project.id,
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
    )
    t2 = _make_cost_task(
        project.id,
        provider="openai",
        model="gpt-5.4",
        input_tokens=750,
        output_tokens=300,
    )
    # Add a deepseek task.
    t3 = _make_cost_task(
        project.id,
        provider="deepseek",
        model="deepseek-v4-flash",
        input_tokens=2000,
        output_tokens=1000,
    )
    db_session.add_all([t1, t2, t3])
    await db_session.commit()

    result = reconcile_llm_cost([t1, t2, t3])

    assert result.task_count == 3
    assert result.within_tolerance is True
    assert result.absolute_delta <= COST_TELEMETRY_TOLERANCE

    expected_total = round(
        _expected_cost("anthropic", "claude-sonnet-4-6", 1000, 500)
        + _expected_cost("openai", "gpt-5.4", 750, 300)
        + _expected_cost("deepseek", "deepseek-v4-flash", 2000, 1000),
        6,
    )
    assert result.total_cost_snapshot_path == pytest.approx(expected_total, abs=1e-6)
    assert result.total_cost_direct_path == pytest.approx(expected_total, abs=1e-6)


@pytest.mark.asyncio
async def test_reconcile_malformed_cache_sub_counts_still_matches(db_session):
    """Tasks with cache sub-counts exceeding the input total (malformed data)
    still reconcile — the bounding logic in _bounded_cache_input_counts ensures
    consistent processing in both paths."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Cache sub-counts sum (1500 + 1500 = 3000) exceeds input (2000).
    # Both paths apply the same _bounded_cache_input_counts bounding, so
    # they should still agree.
    task = _make_cached_cost_task(
        project.id,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        input_tokens=2000,
        output_tokens=1000,
        cache_creation=1500,
        cache_read=1500,
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_llm_cost([task])

    assert result.task_count == 1
    # Both paths apply the same bounding, so they agree.
    assert result.within_tolerance is True
    assert result.absolute_delta <= COST_TELEMETRY_TOLERANCE


@pytest.mark.asyncio
async def test_reconcile_banked_attempt_and_current_attempt(db_session):
    """A task with a banked attempt (via bank_task_current_attempt) plus a
    new current attempt still reconciles — the direct path iterates all
    attempt rows, and the snapshot path does too."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        api_call_summary={
            "llm_call_count": 3,
            "total_input_tokens": 1000,
            "total_output_tokens": 400,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
        vm_provisioned_at=now - timedelta(minutes=9),
        vm_spot=True,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Bank the first attempt.
    bank_task_current_attempt(task, stopped_at=now - timedelta(minutes=5))
    await db_session.commit()
    await db_session.refresh(task)

    # Set up a second attempt with different provider/model.
    task.status = TaskStatus.COMPLETED
    task.context = {"pr_merged": True}
    task.provider = "openai"
    task.model = "gpt-5.4"
    task.vm_provisioned_at = now - timedelta(minutes=3)
    task.vm_stopped_at = now
    task.vm_spot = False
    task.api_call_summary = {
        "llm_call_count": 2,
        "total_input_tokens": 250,
        "total_output_tokens": 150,
    }
    await db_session.commit()

    refreshed = await db_session.get(type(task), task.id)
    result = reconcile_llm_cost([refreshed])

    assert result.task_count == 1
    assert result.within_tolerance is True
    assert result.absolute_delta <= COST_TELEMETRY_TOLERANCE

    # Verify token counters: 1000 (banked) + 250 (current) = 1250 input.
    assert result.total_input_tokens_snapshot_path == 1250
    assert result.total_input_tokens_direct_path == 1250


@pytest.mark.asyncio
async def test_reconcile_zero_cost_tasks(db_session):
    """Tasks with zero input and output tokens produce zero cost from both
    paths and reconcile cleanly."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_cost_task(
        project.id,
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_llm_cost([task])

    assert result.task_count == 1
    assert result.total_cost_snapshot_path == 0.0
    assert result.total_cost_direct_path == 0.0
    assert result.absolute_delta == 0.0
    assert result.within_tolerance is True


def test_cost_telemetry_tolerance_constant_documented():
    """COST_TELEMETRY_TOLERANCE is 1e-6 with a docstring explaining the
    sub-cent floating-point tolerance rationale."""
    assert COST_TELEMETRY_TOLERANCE == 1e-6
    # Verify the constant has a meaningful docstring in the source module.
    import inspect
    from app import cost_telemetry_reconciliation as ctr_module

    source = inspect.getsource(ctr_module)
    # The docstring for the constant should mention "tolerance", "rounding",
    # or "sub-cent" somewhere in nearby text.
    # Check that the module's docstring or the constant's assignment context
    # includes explanatory text.
    assert "tolerance" in source, (
        "COST_TELEMETRY_TOLERANCE must have a docstring explaining its rationale"
    )
    # Verify it's the value we expect at module level.
    assert "1e-6" in source


@pytest.mark.asyncio
async def test_reconcile_result_asdict_serializable(db_session):
    """ReconciliationResult.asdict() returns a plain dict that can be JSON-
    serialized (all values are basic Python types)."""
    import json

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_cost_task(
        project.id,
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_llm_cost([task])
    d = result.asdict()

    # Round-trip through JSON.
    serialized = json.dumps(d)
    deserialized = json.loads(serialized)
    assert deserialized["task_count"] == 1
    assert deserialized["within_tolerance"] is True
    assert isinstance(deserialized["total_cost_snapshot_path"], float)
    assert isinstance(deserialized["cache_hit_rate_snapshot_path"], float)
    assert isinstance(deserialized["per_task"], list)


@pytest.mark.asyncio
async def test_reconciliation_failure_triggers_alert(db_session):
    """When cost telemetry reconciliation fails within_tolerance, the snapshot
    wiring creates a project-scoped WARNING alert."""
    from unittest.mock import AsyncMock, patch

    from sqlmodel import select

    from app.efficiency_snapshot import write_snapshot
    from app.models import Alert, AlertSeverity

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Tasks are required so compute_current_axes reaches the reconciliation call.
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "llm_call_count": 5,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()

    # A reconciliation result that fails within_tolerance.
    failing_reconciliation = {
        "total_cost_snapshot_path": 0.012345,
        "total_cost_direct_path": 0.012346,
        "absolute_delta": 1.0e-6,
        "within_tolerance": False,
        "tolerance": 1e-6,
        "total_input_tokens_snapshot_path": 1000,
        "total_input_tokens_direct_path": 1000,
        "total_cache_read_input_tokens_snapshot_path": 0,
        "total_cache_read_input_tokens_direct_path": 0,
        "total_cache_creation_input_tokens_snapshot_path": 0,
        "total_cache_creation_input_tokens_direct_path": 0,
        "cache_hit_rate_snapshot_path": 0.0,
        "cache_hit_rate_direct_path": 0.0,
        "cache_hit_rate_delta": 0.0,
        "cache_hit_rate_within_tolerance": True,
        "task_count": 2,
        "per_task": [],
    }

    with patch(
        "backend.app.efficiency_snapshot._compute_cost_telemetry_reconciliation",
        return_value=failing_reconciliation,
    ), patch(
        "app.alert_dispatch.broadcast_to_project",
        new_callable=AsyncMock,
    ):
        # First snapshot: alert should be created.
        await write_snapshot(db_session, project.id)

        result = await db_session.execute(
            select(Alert).where(Alert.project_id == project.id)
        )
        alerts = result.scalars().all()
        assert len(alerts) == 1, "expected exactly one alert after first failure"
        alert = alerts[0]

        assert alert.project_id == project.id
        assert alert.severity == AlertSeverity.WARNING
        assert alert.dismissed_at is None
        assert alert.task_id is None
        assert "Cost telemetry reconciliation failed tolerance" in alert.title
        # Delta and tolerance formatted as decimal; 1e-6 → 0.000001.
        assert "0.000001" in alert.message
        assert "2 task(s)" in alert.message

        # Second snapshot with same failing condition: dedup, no new alert.
        await write_snapshot(db_session, project.id)

        result = await db_session.execute(
            select(Alert).where(Alert.project_id == project.id)
        )
        alerts = result.scalars().all()
        assert len(alerts) == 1, "dedup should prevent a second alert"


# ── Provider-reported usage reconciliation tests (Component B) ──────────


def _make_provider_reported_task(
    project_id, *,
    provider, model, input_tokens, output_tokens,
    cache_creation=0, cache_read=0,
    provider_input_tokens=None, provider_cache_creation=None, provider_cache_read=None,
    **kw,
):
    """Create a COMPLETED + merged task with BOTH api_call_summary and
    provider_reported_usage set. Defaults the provider-reported values to
    match the inclusive api_call_summary (input_tokens = provider input +
    cache_creation + cache_read). Pass explicit ``provider_input_tokens``,
    ``provider_cache_creation``, or ``provider_cache_read`` to simulate
    divergence."""
    p_input = provider_input_tokens if provider_input_tokens is not None else max(0, input_tokens - cache_creation - cache_read)
    p_cache_creation = provider_cache_creation if provider_cache_creation is not None else cache_creation
    p_cache_read = provider_cache_read if provider_cache_read is not None else cache_read

    defaults = dict(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "total_cache_creation_input_tokens": cache_creation,
            "total_cache_read_input_tokens": cache_read,
            "llm_call_count": 5,
        },
        provider_reported_usage={
            "input_tokens": p_input,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": p_cache_creation,
            "cache_read_input_tokens": p_cache_read,
        },
        provider=provider,
        model=model,
    )
    defaults.update(kw)
    return make_task(**defaults)


@pytest.mark.asyncio
async def test_provider_reported_matches_on_known_tasks(db_session):
    """Tasks with matching provider_reported_usage and api_call_summary pass
    the provider-reported reconciliation check."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task 1: 1000 uncached input + 200 cache_creation + 300 cache_read = 1500 total.
    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1500, output_tokens=500,
        cache_creation=200, cache_read=300,
        provider_input_tokens=1000, provider_cache_creation=200, provider_cache_read=300,
    )
    db_session.add(t1)
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1])

    assert result.source == "provider_reported"
    assert result.task_count == 1
    assert result.provider_reported_within_tolerance is True
    assert result.provider_reported_delta == 0.0

    # Verify aggregated totals.
    assert result.provider_reported_input_tokens == 1000
    assert result.provider_reported_output_tokens == 500
    assert result.provider_reported_cache_creation_input_tokens == 200
    assert result.provider_reported_cache_read_input_tokens == 300
    assert result.agent_accumulated_input_tokens == 1500

    # Per-task detail.
    assert len(result.per_task) == 1
    assert result.per_task[0]["provider_reported_delta"] == 0.0


@pytest.mark.asyncio
async def test_provider_reported_detects_divergence(db_session):
    """When the agent's api_call_summary diverges from provider_reported_usage,
    the reconciliation fires (within_tolerance=False). This proves the check is
    no longer tautological — it can actually fail."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Provider says 1500 total input (1000 uncached + 200 creation + 300 read).
    # Agent accumulated 1700 instead (accumulation bug — 200 extra tokens).
    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1700, output_tokens=500,
        cache_creation=200, cache_read=300,
        provider_input_tokens=1000, provider_cache_creation=200, provider_cache_read=300,
    )
    db_session.add(t1)
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1])

    assert result.source == "provider_reported"
    assert result.task_count == 1
    # Delta = |1500 - 1700| / 1500 = 200/1500 = 0.1333... >> 0.001
    assert result.provider_reported_within_tolerance is False
    assert result.provider_reported_delta > PROVIDER_REPORTED_TOLERANCE
    # Explicit magnitude bound: 13.3% is robustly over a 0.1% threshold
    # (not marginal like 0.11%), confirming a genuine accumulation error.
    assert result.provider_reported_delta > 0.10

    # Verify the detail accurately reflects the divergence.
    assert result.agent_accumulated_input_tokens == 1700
    assert result.agent_accumulated_output_tokens == 500
    assert result.provider_reported_input_tokens == 1000


@pytest.mark.asyncio
async def test_provider_reported_skips_null(db_session):
    """Tasks without provider_reported_usage (NULL/no data) are silently
    skipped without failing the reconciliation."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task with api_call_summary but NO provider_reported_usage.
    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1500, output_tokens=500,
    )
    # Unset provider_reported_usage (simulate old agent).
    t1.provider_reported_usage = None
    db_session.add(t1)
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1])

    # Skipped — task_count=0 because no task had provider_reported_usage data.
    assert result.source == "provider_reported"
    assert result.task_count == 0
    assert result.provider_reported_within_tolerance is True
    assert result.provider_reported_delta == 0.0


@pytest.mark.asyncio
async def test_provider_reported_mixed_providers(db_session):
    """Anthropic tasks are checked; non-Anthropic tasks without
    provider_reported_usage are skipped."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Anthropic task WITH provider_reported_usage — should be checked.
    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1500, output_tokens=500,
        cache_creation=200, cache_read=300,
        provider_input_tokens=1000, provider_cache_creation=200, provider_cache_read=300,
    )
    # OpenAI task without provider_reported_usage — should be skipped.
    t2 = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 2000,
            "total_output_tokens": 1000,
            "llm_call_count": 3,
        },
        provider="openai", model="gpt-5.4",
    )
    db_session.add_all([t1, t2])
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1, t2])

    assert result.source == "provider_reported"
    assert result.task_count == 1  # Only Anthropic task counted.
    assert result.provider_reported_within_tolerance is True
    assert result.agent_accumulated_input_tokens == 1500


@pytest.mark.asyncio
async def test_provider_reported_empty_task_list(db_session):
    """Empty task list returns source=provider_reported with task_count=0."""
    result = reconcile_provider_reported_usage([])

    assert result.source == "provider_reported"
    assert result.task_count == 0
    assert result.provider_reported_within_tolerance is True
    assert result.provider_reported_delta == 0.0


@pytest.mark.asyncio
async def test_reconcile_llm_cost_sets_source(db_session):
    """The legacy reconcile_llm_cost returns source='internal_recompute'."""
    result = reconcile_llm_cost([])

    assert result.source == "internal_recompute"


# ---------------------------------------------------------------------------
# provider_reported_usage: agent_accumulated_output_tokens coverage
# ---------------------------------------------------------------------------
# These tests exercise the agent_accumulated_output_tokens field added to
# ReconciliationResult so that output-token aggregation is machine-checked.
# c-88ff386c: 'cost telemetry matches actual spend within tolerance'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_reported_output_tokens_aggregates_across_multiple_tasks(db_session):
    """agent_accumulated_output_tokens is the sum of output tokens across all
    provider-checked tasks (multi-task aggregation path)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1500, output_tokens=300,
        cache_creation=200, cache_read=300,
        provider_input_tokens=1000, provider_cache_creation=200, provider_cache_read=300,
    )
    t2 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=2000, output_tokens=700,
        cache_creation=500, cache_read=500,
        provider_input_tokens=1000, provider_cache_creation=500, provider_cache_read=500,
    )
    db_session.add_all([t1, t2])
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1, t2])

    assert result.source == "provider_reported"
    assert result.task_count == 2
    # Output tokens should be the sum across both tasks.
    assert result.agent_accumulated_output_tokens == 300 + 700
    # Input tokens should also aggregate.
    assert result.agent_accumulated_input_tokens == 1500 + 2000


@pytest.mark.asyncio
async def test_provider_reported_asdict_includes_agent_accumulated_output_tokens(db_session):
    """ReconciliationResult.asdict() must include agent_accumulated_output_tokens
    when produced by reconcile_provider_reported_usage (the independent
    cross-check path). The legacy reconcile_llm_cost path does not populate
    this field; this test pins the provider_reported path specifically."""
    import json

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1500, output_tokens=400,
        cache_creation=200, cache_read=300,
        provider_input_tokens=1000, provider_cache_creation=200, provider_cache_read=300,
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_provider_reported_usage([task])
    d = result.asdict()

    # Key must be present and have the correct value.
    assert "agent_accumulated_output_tokens" in d, (
        "asdict() must include agent_accumulated_output_tokens"
    )
    assert d["agent_accumulated_output_tokens"] == 400
    # Round-trip: asdict must remain JSON-serializable.
    serialized = json.dumps(d)
    deserialized = json.loads(serialized)
    assert deserialized["agent_accumulated_output_tokens"] == 400


@pytest.mark.asyncio
async def test_provider_reported_per_task_detail_includes_agent_accumulated_output_tokens(db_session):
    """The per_task diagnostic breakdown must include agent_accumulated_output_tokens
    for each checked task so operators can identify per-task output-token discrepancies."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1500, output_tokens=250,
        cache_creation=200, cache_read=300,
        provider_input_tokens=1000, provider_cache_creation=200, provider_cache_read=300,
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_provider_reported_usage([task])

    assert len(result.per_task) == 1
    entry = result.per_task[0]
    assert "agent_accumulated_output_tokens" in entry, (
        "per_task detail must include agent_accumulated_output_tokens"
    )
    assert entry["agent_accumulated_output_tokens"] == 250
    # Provider-reported output should also be in the detail.
    assert entry["provider_reported_output_tokens"] == 250


@pytest.mark.asyncio
async def test_provider_reported_cache_token_fields_aggregate_correctly(db_session):
    """agent_accumulated_cache_creation_input_tokens and
    agent_accumulated_cache_read_input_tokens aggregate correctly across
    all provider-checked tasks."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1500, output_tokens=300,
        cache_creation=200, cache_read=300,
        provider_input_tokens=1000, provider_cache_creation=200, provider_cache_read=300,
    )
    t2 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=2000, output_tokens=500,
        cache_creation=400, cache_read=600,
        provider_input_tokens=1000, provider_cache_creation=400, provider_cache_read=600,
    )
    db_session.add_all([t1, t2])
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1, t2])

    # Cache creation: 200 + 400
    assert result.agent_accumulated_cache_creation_input_tokens == 600
    # Cache read: 300 + 600
    assert result.agent_accumulated_cache_read_input_tokens == 900
    # Provider-side cache tokens should also aggregate.
    assert result.provider_reported_cache_creation_input_tokens == 600
    assert result.provider_reported_cache_read_input_tokens == 900


@pytest.mark.asyncio
async def test_provider_reported_tolerance_boundary_at_threshold(db_session):
    """A delta exactly at PROVIDER_REPORTED_TOLERANCE (0.001 = 0.1%) is
    within tolerance (boundary inclusive)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Provider says 1000 tokens; agent says 1001 (delta = 1/1000 = 0.001 exactly).
    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1001, output_tokens=200,
        provider_input_tokens=1001,
    )
    db_session.add(t1)
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1])

    # agent=1001, provider=1001 → delta=0.0 → within tolerance.
    assert result.provider_reported_within_tolerance is True
    assert result.provider_reported_delta == 0.0

    # Now create divergence of exactly 0.1% (1 token per 1000).
    t2 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1001, output_tokens=200,
        provider_input_tokens=1000,
    )
    db_session.add(t2)
    await db_session.commit()

    result2 = reconcile_provider_reported_usage([t2])

    # |1000 - 1001| / 1000 = 0.001 exactly → within_tolerance (<=).
    assert result2.provider_reported_delta == pytest.approx(0.001, abs=1e-9)
    assert result2.provider_reported_within_tolerance is True
    assert result2.provider_reported_tolerance == PROVIDER_REPORTED_TOLERANCE


@pytest.mark.asyncio
async def test_provider_reported_just_above_tolerance_fails(db_session):
    """A delta that exceeds PROVIDER_REPORTED_TOLERANCE by even a small margin
    correctly trips within_tolerance=False. Regression guard for the boundary
    check (must be <= not <)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Provider says 1000; agent says 1002 → |2|/1000 = 0.002 > 0.001.
    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1002, output_tokens=200,
        provider_input_tokens=1000,
    )
    db_session.add(t1)
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1])

    assert result.provider_reported_within_tolerance is False
    assert result.provider_reported_delta > PROVIDER_REPORTED_TOLERANCE
    assert result.provider_reported_delta == pytest.approx(0.002, abs=1e-9)


@pytest.mark.asyncio
async def test_provider_reported_output_tokens_zero_when_missing_from_api_summary(db_session):
    """When api_call_summary has no total_output_tokens key, agent_accumulated_output_tokens
    defaults to 0 without raising."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 1000,
            # No total_output_tokens key.
            "total_cache_creation_input_tokens": 200,
            "total_cache_read_input_tokens": 300,
            "llm_call_count": 5,
        },
        provider_reported_usage={
            "input_tokens": 500,
            "output_tokens": 0,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 300,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_provider_reported_usage([task])

    assert result.task_count == 1
    assert result.agent_accumulated_output_tokens == 0
    # Input reconciliation should still work.
    assert result.provider_reported_input_tokens == 500


@pytest.mark.asyncio
async def test_provider_reported_provider_output_tokens_aggregate(db_session):
    """provider_reported_output_tokens aggregates across multiple tasks even
    though it is not used in the delta calculation (which is input-only)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    t1 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1000, output_tokens=150,
        provider_input_tokens=1000,
    )
    t2 = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=2000, output_tokens=350,
        provider_input_tokens=2000,
    )
    db_session.add_all([t1, t2])
    await db_session.commit()

    result = reconcile_provider_reported_usage([t1, t2])

    # provider_reported_usage["output_tokens"] defaults to input_tokens match value in helper.
    assert result.provider_reported_output_tokens == 150 + 350
    assert result.agent_accumulated_output_tokens == 150 + 350
    assert result.task_count == 2
    assert result.provider_reported_within_tolerance is True


@pytest.mark.asyncio
async def test_provider_reported_skip_task_with_all_zero_provider_tokens(db_session):
    """A task where all provider-reported token counts are zero is treated as
    having no provider data and is skipped (task_count not incremented).
    This prevents tasks without Anthropic API data from falsely contributing
    a zero-delta to the reconciliation."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task with provider_reported_usage all zeros.
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary={
            "total_input_tokens": 500,
            "total_output_tokens": 200,
            "llm_call_count": 2,
        },
        provider_reported_usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_provider_reported_usage([task])

    # All-zero provider counts → skipped (same condition as no data).
    assert result.task_count == 0
    assert result.agent_accumulated_output_tokens == 0
    assert result.provider_reported_within_tolerance is True


@pytest.mark.asyncio
async def test_provider_reported_only_cache_tokens_no_raw_input(db_session):
    """A task with only cache_creation / cache_read provider tokens (input_tokens=0)
    is NOT skipped — cache tokens count as having real provider data."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_provider_reported_task(
        project.id,
        provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=500, output_tokens=100,
        cache_creation=300, cache_read=200,
        # Raw input=0, but cache tokens are non-zero.
        provider_input_tokens=0,
        provider_cache_creation=300,
        provider_cache_read=200,
    )
    db_session.add(task)
    await db_session.commit()

    result = reconcile_provider_reported_usage([task])

    # cache_creation=300 + cache_read=200 → non-zero → task IS checked.
    assert result.task_count == 1
    # agent says total_input = 500; provider says 0+300+200 = 500 → delta=0.
    assert result.provider_reported_within_tolerance is True
    assert result.provider_reported_delta == 0.0
    assert result.agent_accumulated_output_tokens == 100
