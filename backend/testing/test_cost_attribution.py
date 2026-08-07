"""Cost attribution: provider_reported_usage captured at the LLM-call boundary.

The operator-reported defect (message f1d05e9b): ``task.provider_reported_usage``
was JSON NULL on ~90% of deepseek tasks, so real token/cache-read volume never
reached the cost estimator. Root cause was a silent null — the agent runner only
read Anthropic-shaped usage keys, so OpenAI-shaped DeepSeek responses parsed to
zeros and the accumulator emitted NULL instead of a record.

These tests pin the fix:

1. EVERY supported provider (anthropic, openai, gemini, zai, deepseek) persists
   a non-null ``provider_reported_usage`` record with token counts (including
   cache-creation / cache-read sub-counts where reported) through the heartbeat
   write path.
2. Retried/streaming paths preserve the probe: ``bank_task_current_attempt``
   carries ``provider_reported_usage`` into the ``attempt_history`` snapshot and
   clears the live column, and ``reconcile_provider_reported_usage`` ACCUMULATES
   both the provider and agent sides across banked + current attempts instead of
   overwriting with the last attempt.
3. A provider that genuinely reports no usage records the explicit
   ``{"reported": false}`` marker rather than a silent null, and reconciliation
   skips the marker (``task_count == 0``) without a false failure. A
   ``reported: true`` all-zero dict is a genuine probe (counted) — the marker
   distinguishes "reported zero tokens" from "not reported at all".
"""

from __future__ import annotations

import pytest
from testing.factories import _utcnow, make_project, make_task

from app.auth import create_agent_token
from app.cost_telemetry_reconciliation import reconcile_provider_reported_usage
from app.models import TaskStatus, bank_task_current_attempt


# Every provider the runner can invoke. DeepSeek is included because it was the
# provider whose usage was being silently nulled (OpenAI-shaped usage keys).
SUPPORTED_PROVIDERS = [
    ("anthropic", "claude-sonnet-4-6"),
    ("openai", "gpt-5.4"),
    ("gemini", "gemini-2.5-pro"),
    ("zai", "zai-pro-max"),
    ("deepseek", "deepseek-v4-flash"),
]


def _probe(
    *,
    input_tokens: int = 1000,
    output_tokens: int = 400,
    cache_creation: int = 200,
    cache_read: int = 300,
    reported: bool = True,
) -> dict:
    """Provider-reported usage probe shape emitted by the agent accumulator.

    ``input_tokens`` is the UNCACHED input; the inclusive input total the agent
    spreads into api_call_summary equals input_tokens + cache_creation +
    cache_read (mirrors how the agent emits both sides so reconciliation's
    independent cross-check sees matching inclusive totals).
    """
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "reported": reported,
    }


def _summary(*, input_tokens: int, output_tokens: int, cache_creation: int = 0, cache_read: int = 0) -> dict:
    """Flat api_call_summary fields the agent posts on the heartbeat.

    ``total_input_tokens`` is the INCLUSIVE sum of uncached + cache-creation +
    cache-read input, matching the provider probe's classes so the
    reconciliation compares like-for-like.
    """
    summary: dict = {
        "llm_call_count": 5,
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
    }
    if cache_creation:
        summary["total_cache_creation_input_tokens"] = cache_creation
    if cache_read:
        summary["total_cache_read_input_tokens"] = cache_read
    return summary


async def _post_usage_heartbeat(client, task, *, provider_reported_usage, summary=None):
    """POST a heartbeat carrying provider_reported_usage (+ flat summary)."""
    token = create_agent_token(str(task.id), task.vm_instance_id)
    payload = {"task_id": str(task.id)}
    payload["provider_reported_usage"] = provider_reported_usage
    if summary:
        payload.update(summary)
    resp = await client.post(
        "/api/agent/heartbeat", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200, resp.text


# ── 1. Per-provider persistence through the heartbeat write path ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, model", SUPPORTED_PROVIDERS)
async def test_provider_reported_usage_persists_for_every_provider(
    client, db_session, provider, model
):
    """A simulated response from each supported provider (deepseek included)
    persists a NON-NULL provider_reported_usage record with token counts."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{provider}",
        provider=provider,
        model=model,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    usage = _probe()
    await _post_usage_heartbeat(
        client,
        task,
        provider_reported_usage=usage,
        summary=_summary(input_tokens=1500, output_tokens=400, cache_creation=200, cache_read=300),
    )

    await db_session.refresh(task)
    assert task.provider_reported_usage is not None, (
        f"{provider} heartbeat must persist a non-null provider_reported_usage "
        f"(silent null was the deepseek defect)"
    )
    assert task.provider_reported_usage == usage, (
        f"{provider} provider_reported_usage must round-trip the probe unchanged; "
        f"got {task.provider_reported_usage!r}"
    )
    # Cache-read / cache-write tokens reported by the provider must be captured.
    assert task.provider_reported_usage["cache_read_input_tokens"] == 300
    assert task.provider_reported_usage["cache_creation_input_tokens"] == 200
    assert task.provider_reported_usage["reported"] is True


# ── 2. Streaming / retry paths preserve the probe; accumulate across attempts ─


def test_bank_task_current_attempt_carries_provider_usage_into_history():
    """bank_task_current_attempt snapshots the live provider_reported_usage into
    attempt_history and clears the live column so a retried attempt starts fresh."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary=_summary(input_tokens=1500, output_tokens=400, cache_creation=200, cache_read=300),
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    task.provider_reported_usage = _probe()

    bank_task_current_attempt(task, stopped_at=_utcnow())

    # The probe survives into the banked snapshot.
    assert len(task.attempt_history) == 1
    snapshot = task.attempt_history[-1]
    assert snapshot["provider_reported_usage"] == _probe(), (
        "banked attempt must carry the provider-reported usage probe"
    )
    # The agent-side totals are banked alongside so reconciliation can pair them.
    assert snapshot["llm_input_tokens"] == 1500
    assert snapshot["llm_output_tokens"] == 400
    assert snapshot["llm_cache_creation_input_tokens"] == 200
    assert snapshot["llm_cache_read_input_tokens"] == 300
    # The live column is cleared so the next attempt does not double-count.
    assert task.provider_reported_usage is None


def test_reconciliation_accumulates_provider_usage_across_attempts():
    """Reconciliation SUMS provider-reported usage across a banked attempt plus
    the current attempt rather than overwriting with the last one."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary=_summary(input_tokens=130, output_tokens=50, cache_creation=10, cache_read=20),
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    task.provider_reported_usage = _probe(
        input_tokens=100, output_tokens=50, cache_creation=10, cache_read=20
    )
    # Bank attempt 1 (probe: 100 input + 10 creation + 20 read = 130 inclusive).
    bank_task_current_attempt(task, stopped_at=_utcnow())

    # Attempt 2 (current): probe 200 input + 0 creation + 30 read = 230 inclusive.
    task.provider_reported_usage = _probe(
        input_tokens=200, output_tokens=80, cache_creation=0, cache_read=30
    )
    task.api_call_summary = _summary(
        input_tokens=230, output_tokens=80, cache_read=30
    )

    result = reconcile_provider_reported_usage([task])

    assert result.task_count == 1
    # Provider side is the SUM of both attempts, not just the last (200).
    assert result.provider_reported_input_tokens == 100 + 200
    assert result.provider_reported_output_tokens == 50 + 80
    assert result.provider_reported_cache_creation_input_tokens == 10 + 0
    assert result.provider_reported_cache_read_input_tokens == 20 + 30
    # Agent side likewise accumulates.
    assert result.agent_accumulated_input_tokens == 130 + 230
    assert result.agent_accumulated_output_tokens == 50 + 80
    assert result.agent_accumulated_cache_creation_input_tokens == 10
    assert result.agent_accumulated_cache_read_input_tokens == 20 + 30
    # Inclusive totals match → no divergence from accumulation.
    assert result.provider_reported_within_tolerance is True
    assert result.provider_reported_delta == 0.0


def test_reconciliation_sums_banked_attempts_only_once():
    """Attempts that carry NO probe (legacy banked rows) are excluded from BOTH
    sides, so they cannot manufacture a false divergence."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary=_summary(input_tokens=500, output_tokens=200),
        provider="anthropic",
        model="claude-sonnet-4-6",
    )
    # Legacy banked attempt WITHOUT a provider_reported_usage key (pre-marker
    # agent) plus a current attempt that carries a probe.
    task.attempt_history = [
        {
            "vm_provisioned_at": None,
            "vm_stopped_at": _utcnow().isoformat(),
            "llm_input_tokens": 500,
            "llm_output_tokens": 200,
            "llm_call_count": 2,
        }
    ]
    task.provider_reported_usage = _probe(
        input_tokens=300, output_tokens=100, cache_creation=0, cache_read=0
    )
    task.api_call_summary = _summary(input_tokens=300, output_tokens=100)

    result = reconcile_provider_reported_usage([task])

    # Only the current (probe-carrying) attempt contributes to both sides.
    assert result.task_count == 1
    assert result.provider_reported_input_tokens == 300
    assert result.agent_accumulated_input_tokens == 300
    assert result.agent_accumulated_output_tokens == 100
    assert result.provider_reported_within_tolerance is True


# ── 3. Explicit not-reported marker (never a silent null) ───────────────────


@pytest.mark.asyncio
async def test_not_reported_marker_persisted_not_null(client, db_session):
    """A provider that makes LLM calls but reports NO usage block records the
    explicit ``{"reported": false}`` marker rather than a silent null."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-noreport",
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    marker = {"reported": False}
    await _post_usage_heartbeat(
        client,
        task,
        provider_reported_usage=marker,
        summary=_summary(input_tokens=5000, output_tokens=1200),
    )

    await db_session.refresh(task)
    assert task.provider_reported_usage is not None, (
        "a not-reported provider must persist an explicit marker, never a silent null"
    )
    assert task.provider_reported_usage == marker
    assert task.provider_reported_usage["reported"] is False


def test_not_reported_marker_banked_on_retry():
    """The explicit not-reported marker is carried into the banked snapshot so a
    retried attempt does not lose the knowledge that usage was not reported."""
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        api_call_summary=_summary(input_tokens=1000, output_tokens=300),
        provider="gemini",
        model="gemini-2.5-pro",
    )
    task.provider_reported_usage = {"reported": False}

    bank_task_current_attempt(task, stopped_at=_utcnow())

    assert len(task.attempt_history) == 1
    assert task.attempt_history[-1]["provider_reported_usage"] == {"reported": False}
    # Live column cleared for the next attempt.
    assert task.provider_reported_usage is None


@pytest.mark.asyncio
async def test_not_reported_marker_skipped_by_reconciliation(client, db_session):
    """A task carrying the explicit not-reported marker is skipped by
    reconciliation (task_count == 0) without a false tolerance failure."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary=_summary(input_tokens=5000, output_tokens=1200),
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    task.provider_reported_usage = {"reported": False}
    db_session.add(task)
    await db_session.commit()

    result = reconcile_provider_reported_usage([task])

    assert result.task_count == 0, (
        "the not-reported marker is not a probe and must not be counted"
    )
    # Zero coverage → the tolerance verdict is UNPROVEN, never a green from zero
    # evidence (see test_provider_reported_skips_null /
    # test_provider_reported_empty_task_list in test_cost_telemetry_reconciliation.py).
    assert result.provider_reported_within_tolerance is False
    assert result.provider_reported_verified is False
    assert result.provider_reported_delta == 0.0


def test_reported_true_all_zero_probe_is_counted():
    """A ``reported: true`` all-zero dict is a genuine probe (the provider DID
    report a usage block of zero tokens) — distinct from the not-reported marker
    and counted by reconciliation."""
    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        api_call_summary=_summary(input_tokens=0, output_tokens=0),
        provider="openai",
        model="gpt-5.4",
    )
    task.provider_reported_usage = _probe(
        input_tokens=0, output_tokens=0, cache_creation=0, cache_read=0, reported=True
    )

    result = reconcile_provider_reported_usage([task])

    assert result.task_count == 1, (
        "reported: true zero tokens must count as a probe, not be skipped like null"
    )
    assert result.provider_reported_within_tolerance is True
    assert result.provider_reported_delta == 0.0
