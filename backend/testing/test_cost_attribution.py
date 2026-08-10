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

The pricing half (provider truth is canonical):
4. The ledger prices each attempt from its provider-reported probe when one
   exists — the agent-accumulated ``api_call_summary`` / ``llm_*`` counters are
   only the fallback for attempts that carry no probe. A probe with cache
   sub-counts is priced at the discounted cache classes; a ``reported: false``
   marker is NOT a probe and falls back to the agent counters.
5. A task whose agent counters carry zero tokens but whose probe is genuine is
   STILL priced from the probe (the probe makes it visible to the ledger).
6. The billing reconciliation surfaces how many attempts were priced from a
   probe (``internal.provider_priced_attempt_count``), so the canonical-vs-
   fallback split is observable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from testing.factories import _utcnow, make_project, make_task

from app.auth import create_agent_token
from app.cost_telemetry_reconciliation import reconcile_provider_reported_usage
from app.models import (
    TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY,
    TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY,
    TaskStatus,
    advance_task_lifetime_attempt,
    bank_task_current_attempt,
    get_task_lifetime_attempt_ordinal,
    with_task_attempt_banking,
)


def _load_cost_reconciliation_module():
    """Load backend/scripts/cost_reconciliation.py (stdlib-only at import)."""
    spec = importlib.util.spec_from_file_location(
        "cost_reconciliation",
        Path(__file__).parent.parent / "scripts" / "cost_reconciliation.py",
    )
    assert spec is not None, "Could not locate scripts/cost_reconciliation.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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


def test_attempt_ordinal_first_budgeted_failure_advances_to_attempt_two():
    """Increment-before-bank retry code must snapshot Attempt 1, not Attempt 2."""
    task = make_task(
        status=TaskStatus.FAILED,
        attempt_count=0,
        api_call_summary=_summary(input_tokens=10, output_tokens=2),
    )
    current_ordinal = get_task_lifetime_attempt_ordinal(task)
    task.attempt_count += 1

    bank_task_current_attempt(task, attempt_ordinal=current_ordinal)
    advance_task_lifetime_attempt(task)

    assert task.attempt_history[-1]["attempt_ordinal"] == 1
    assert get_task_lifetime_attempt_ordinal(task) == 2


def test_attempt_ordinal_mixed_unbanked_budget_and_banked_exempt_failure():
    """Disjoint retry-count/history evidence advances monotonically without sum."""
    task = make_task(
        status=TaskStatus.FAILED,
        # Attempt 1 died before telemetry and consumed budget, so no history row
        # exists; this runtime-exempt failure is the live Attempt 2.
        attempt_count=1,
        api_call_summary=_summary(input_tokens=10, output_tokens=2),
    )

    bank_task_current_attempt(task)
    advance_task_lifetime_attempt(task)

    assert task.attempt_history[-1]["attempt_ordinal"] == 2
    assert task.attempt_count == 1
    assert get_task_lifetime_attempt_ordinal(task) == 3


def test_placeholder_banking_is_idempotent_and_stale_context_keeps_markers():
    """A telemetry-free boundary is counted once and stale merges keep markers."""
    task = make_task(status=TaskStatus.FAILED, attempt_count=0)

    first = bank_task_current_attempt(task)
    second = bank_task_current_attempt(task)
    merged = with_task_attempt_banking(task, {"preserved": True})

    assert first is not None
    assert first["attempt_ordinal"] == 1
    assert second is None
    assert len(task.attempt_history) == 1
    assert merged[TASK_CURRENT_ATTEMPT_ORDINAL_CONTEXT_KEY] == 1
    assert merged[TASK_LAST_BANKED_ATTEMPT_ORDINAL_CONTEXT_KEY] == 1
    assert merged["preserved"] is True


def test_bank_task_current_attempt_retains_distinct_bounded_failure_diagnostics():
    """Closed attempts keep their own forensic evidence across retry epochs."""
    task = make_task(
        status=TaskStatus.FAILED,
        attempt_count=1,
        api_call_summary=_summary(input_tokens=100, output_tokens=20),
        context={
            "failure_class": "EXTERNAL_API",
            "failure_category": "EXTERNAL_API",
            "agent_failure_category_raw": "claude_code_failed",
            "agent_error_report": {
                "schema": 1,
                "provider": "anthropic",
                "exit_code": 1,
                "model_output_seen": False,
                "result_event_seen": False,
                "stderr_tail": "first ghp_firstattemptsecret provider failure",
                "provider_errors": [],
            },
        },
    )
    task.latest_status_message = (
        "x" * 2200 + " first ghp_firstattemptsecret failure tail"
    )

    bank_task_current_attempt(task, stopped_at=_utcnow())

    # Simulate a separately requeued retry epoch whose retry budget was reset.
    # Its lifetime ordinal must still advance, while its retry counter remains an
    # explicitly separate value.  This attempt intentionally has no report.
    task.status = TaskStatus.FAILED
    task.attempt_count = 1
    task.api_call_summary = _summary(input_tokens=200, output_tokens=30)
    task.context = {
        "failure_class": "CODE_ERROR",
        "failure_category": "CODE_ERROR",
        "agent_failure_category_raw": "incomplete",
    }
    task.latest_status_message = "second ghs_secondattemptsecret failure tail"

    bank_task_current_attempt(task, stopped_at=_utcnow())

    assert task.attempt_history is not None
    first, second = task.attempt_history
    assert [first["attempt_ordinal"], second["attempt_ordinal"]] == [2, 3]
    assert [first["retry_count_at_bank"], second["retry_count_at_bank"]] == [1, 1]
    assert [first["status"], second["status"]] == ["FAILED", "FAILED"]
    assert [first["failure_class"], second["failure_class"]] == [
        "EXTERNAL_API",
        "CODE_ERROR",
    ]
    assert [
        first["agent_failure_category_raw"],
        second["agent_failure_category_raw"],
    ] == ["claude_code_failed", "incomplete"]
    assert "first" in first["failure_message_tail"]
    assert "second" in second["failure_message_tail"]
    assert len(first["failure_message_tail"]) == 2000
    assert len(second["failure_message_tail"]) <= 2000
    assert "firstattemptsecret" not in first["failure_message_tail"]
    assert "secondattemptsecret" not in second["failure_message_tail"]
    assert "firstattemptsecret" not in first["agent_error_report"]["stderr_tail"]
    assert "agent_error_report" not in second, (
        "a report-less attempt must not inherit the previous attempt's report"
    )


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


# ── 4. Provider truth is the canonical pricing input ──────────────────────

# DeepSeek flash rates, mirrored from LLM_TOKEN_PRICING / LLM_CACHE_READ_INPUT_PRICING.
_FLASH_INPUT_RATE = 0.14e-6
_FLASH_OUTPUT_RATE = 0.28e-6
_FLASH_CACHE_READ_RATE = 0.0028e-6


def test_ledger_prices_from_provider_probe_over_agent_counters():
    """The ledger prices the live attempt from the PROVIDER-REPORTED probe when
    one exists, not the agent-accumulated ``api_call_summary`` counters. Provider
    truth is the canonical input; agent counters are only the fallback.

    Probe: 300 uncached + 200 cache-creation + 500 cache-read = 1000 inclusive.
    Agent counters claim 1500 inclusive + 200 output — must be ignored.
    Cost = 300 x $0.14/M + 200 x $0.14/M + 500 x $0.0028/M + 100 x $0.28/M.
    """
    from app.llm_cost_attribution import compute_llm_cost_breakdown

    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        provider="deepseek",
        model="deepseek-v4-flash",
        api_call_summary=_summary(
            input_tokens=1500, output_tokens=200, cache_creation=100, cache_read=400
        ),
        provider_reported_usage=_probe(
            input_tokens=300, output_tokens=100, cache_creation=200, cache_read=500
        ),
    )

    result = compute_llm_cost_breakdown([task])

    # The probe's inclusive input (300+200+500=1000) wins over the agent's 1500.
    assert result["totals"]["total_input_tokens"] == 1000
    assert result["totals"]["total_output_tokens"] == 100
    assert result["totals"]["total_cache_creation_input_tokens"] == 200
    assert result["totals"]["total_cache_read_input_tokens"] == 500
    # Exact cost exercises the discounted deepseek cache-read rate. The
    # breakdown rounds total_cost_usd to 6 decimals, so round the expectation
    # to match (9.94e-5 -> 9.9e-5).
    expected_cost = (
        300 * _FLASH_INPUT_RATE
        + 200 * _FLASH_INPUT_RATE
        + 500 * _FLASH_CACHE_READ_RATE
        + 100 * _FLASH_OUTPUT_RATE
    )
    assert result["totals"]["total_cost_usd"] == pytest.approx(
        round(expected_cost, 6), abs=1e-12
    )


def test_banked_attempt_probe_prices_from_provider_usage():
    """A banked attempt in ``attempt_history`` carrying a provider-reported probe
    prices from the probe, not the agent-side ``llm_*`` snapshot counters."""
    from app.llm_cost_attribution import _task_llm_attempts, compute_llm_cost_breakdown

    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        provider="deepseek",
        model="deepseek-v4-flash",
        api_call_summary=None,
    )
    task.attempt_history = [
        {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "llm_input_tokens": 500,
            "llm_output_tokens": 200,
            "llm_call_count": 3,
            "provider_reported_usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "reported": True,
            },
        }
    ]

    attempts = _task_llm_attempts(task)
    assert len(attempts) == 1
    _provider, _model, inp, out, calls, cc, cr = attempts[0]
    # Probe inclusive input = 100 + 20 + 30 = 150, NOT the agent-side 500.
    assert inp == 150
    assert out == 50
    assert cc == 20
    assert cr == 30
    # call_count always comes from the agent counters (probes carry no calls).
    assert calls == 3

    result = compute_llm_cost_breakdown([task])
    assert result["totals"]["total_input_tokens"] == 150
    assert result["totals"]["total_output_tokens"] == 50
    assert result["totals"]["total_cache_creation_input_tokens"] == 20
    assert result["totals"]["total_cache_read_input_tokens"] == 30


def test_no_probe_falls_back_to_agent_counters():
    """Tasks without any provider-reported probe keep pricing from the
    agent-accumulated counters — the required fallback for pre-probe rows."""
    from app.llm_cost_attribution import compute_llm_cost_breakdown

    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        provider="deepseek",
        model="deepseek-v4-flash",
        api_call_summary=_summary(input_tokens=1000, output_tokens=200),
        provider_reported_usage=None,
    )

    result = compute_llm_cost_breakdown([task])

    assert result["totals"]["total_input_tokens"] == 1000
    assert result["totals"]["total_output_tokens"] == 200
    expected_cost = 1000 * _FLASH_INPUT_RATE + 200 * _FLASH_OUTPUT_RATE
    assert result["totals"]["total_cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_not_reported_marker_falls_back_to_agent_counters():
    """The explicit ``{"reported": false}`` marker is NOT a probe: pricing falls
    back to the agent-accumulated counters instead of zeroing the attempt."""
    from app.llm_cost_attribution import compute_llm_cost_breakdown

    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        provider="deepseek",
        model="deepseek-v4-flash",
        api_call_summary=_summary(input_tokens=1000, output_tokens=200),
        provider_reported_usage={"reported": False},
    )

    result = compute_llm_cost_breakdown([task])

    assert result["totals"]["total_input_tokens"] == 1000
    expected_cost = 1000 * _FLASH_INPUT_RATE + 200 * _FLASH_OUTPUT_RATE
    assert result["totals"]["total_cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_zero_agent_counters_with_probe_produce_priced_row():
    """Root-cause fix: when the agent's ``api_call_summary`` carries no tokens
    but a provider-reported probe exists, the task is STILL priced from the probe
    — previously it was invisible to the ledger (zero tokens == no attempt row)."""
    from app.llm_cost_attribution import _task_llm_attempts, compute_llm_cost_breakdown

    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        provider="deepseek",
        model="deepseek-v4-flash",
        api_call_summary=_summary(input_tokens=0, output_tokens=0),
        provider_reported_usage=_probe(
            input_tokens=300, output_tokens=100, cache_creation=200, cache_read=500
        ),
    )

    attempts = _task_llm_attempts(task)
    assert len(attempts) == 1
    assert attempts[0][2] == 1000  # inclusive input from the probe

    result = compute_llm_cost_breakdown([task])
    assert result["totals"]["total_cost_usd"] > 0
    assert result["totals"]["total_input_tokens"] == 1000
    assert result["totals"]["total_output_tokens"] == 100


def test_provider_probe_cache_breakout_uses_discounted_rate():
    """A probe whose input is entirely cache-read is priced at the deepseek
    discounted cache-read rate ($0.0028/M), not the $0.14/M base input rate."""
    from app.llm_cost_attribution import compute_llm_cost_breakdown

    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        provider="deepseek",
        model="deepseek-v4-flash",
        api_call_summary=_summary(input_tokens=0, output_tokens=0),
        provider_reported_usage=_probe(
            input_tokens=0, output_tokens=0, cache_creation=0, cache_read=1_000_000
        ),
    )

    result = compute_llm_cost_breakdown([task])

    assert result["totals"]["total_cache_read_input_tokens"] == 1_000_000
    assert result["totals"]["total_cost_usd"] == pytest.approx(
        1_000_000 * _FLASH_CACHE_READ_RATE, rel=1e-9
    )


def test_no_history_banked_columns_stay_separate_from_probe_row():
    """Legacy no-attempt-history tasks with banked retry spend (no probe) keep
    the banked row priced from agent counters while the live probe row prices
    from provider truth — banked spend is not dropped when a probe exists."""
    from app.llm_cost_attribution import _task_llm_attempts, compute_llm_cost_breakdown

    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        provider="deepseek",
        model="deepseek-v4-flash",
        api_call_summary=_summary(input_tokens=400, output_tokens=50),
        provider_reported_usage=_probe(
            input_tokens=300, output_tokens=100, cache_creation=0, cache_read=100
        ),
    )
    # Banked columns are hybrid properties, not constructor fields.
    task.llm_input_tokens_banked = 600
    task.llm_output_tokens_banked = 100
    task.llm_call_count_banked = 3

    attempts = _task_llm_attempts(task)
    # Banked row (agent counters, no cache) + probe row.
    assert len(attempts) == 2
    banked = attempts[0]
    assert banked[2] == 600
    assert banked[3] == 100
    assert banked[5] == 0 and banked[6] == 0  # no cache breakdown on banked spend
    probe = attempts[1]
    assert probe[2] == 400  # 300 uncached + 0 cc + 100 cr
    assert probe[3] == 100
    assert probe[5] == 0 and probe[6] == 100

    result = compute_llm_cost_breakdown([task])
    assert result["totals"]["total_input_tokens"] == 1000  # 600 + 400
    assert result["totals"]["total_output_tokens"] == 200  # 100 + 100


def test_billing_reconciliation_counts_provider_priced_attempts():
    """The billing reconciliation's internal report counts attempts priced from
    a provider-reported probe (``internal.provider_priced_attempt_count``), so
    the canonical-vs-fallback split is observable."""
    from datetime import datetime, timezone

    from app.llm_cost_attribution import _task_llm_attempts

    mod = _load_cost_reconciliation_module()
    task = make_task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        provider="deepseek",
        model="deepseek-v4-flash",
        created_at=datetime(2026, 8, 5, 0, 0, 0, tzinfo=timezone.utc).replace(
            tzinfo=None
        ),
        api_call_summary=_summary(
            input_tokens=1500, output_tokens=200, cache_creation=100, cache_read=400
        ),
        provider_reported_usage=_probe(
            input_tokens=300, output_tokens=100, cache_creation=200, cache_read=500
        ),
    )

    row = mod._task_to_row(task, _task_llm_attempts(task))
    assert row["provider_priced_attempt_count"] == 1

    report = mod.reconcile_provider_cost(
        [row],
        {
            "provider": "deepseek",
            "window": {"start": "2026-08-05", "end": "2026-08-05"},
            "records": [],
        },
        "deepseek",
    )
    # The attempt was priced from the probe (inclusive 1000, uncached 300).
    assert report["internal"]["provider_priced_attempt_count"] == 1
    assert report["internal"]["uncached_input_tokens"] == 300
    assert report["internal"]["cache_creation_input_tokens"] == 200
    assert report["internal"]["cache_read_input_tokens"] == 500
    assert report["internal"]["output_tokens"] == 100


def test_reconciliation_uses_shared_probe_helper():
    """``cost_telemetry_reconciliation`` imports the probe helper from
    ``llm_cost_attribution`` (single source of truth — a divergent copy would
    drift and let pricing and reconciliation disagree on what a probe is)."""
    import app.cost_telemetry_reconciliation as ctr
    import app.llm_cost_attribution as lca

    assert ctr._is_provider_probe is lca._is_provider_probe
