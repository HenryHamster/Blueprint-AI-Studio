"""Tests for backend/scripts/cost_reconciliation.py.

The reconciliation REPORTS the residual between internally attributed cost
(computed from recorded per-attempt usage) and an operator-supplied provider
billing export. It never asserts agreement -- a large residual with a high
missing-usage count is the correct output, not a bug.

These tests pin the report's honesty, per the task's PROVE IT requirements:
  1. internal totals are computed from RECORDED PER-ATTEMPT usage, never from
     ``estimated_cost_usd`` (the field the operator's billing export showed
     captures only ~22-25% of real spend);
  2. a synthetic window with deliberately missing usage rows produces a LARGE
     reported residual with a correct missing-task count rather than a silent
     match;
  3. the cache-read vs uncached breakout is populated on both sides, since
     unrecorded cache-read volume is the suspected dominant term.

Most tests are standalone (stdlib-only module load via importlib, no DB),
mirroring the accepted evidence precedent
``testing/test_strategic_effort_tier_evidence.py``. One DB-backed test
round-trips a real ``Task`` row through the ORM->dict conversion so the
production read path is exercised too.

Run with:
    bash scripts/test-backend.sh testing/test_cost_reconciliation.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "cost_reconciliation.py"
)

# DeepSeek V4 billing rates (api-docs.deepseek.com, 2026-07), mirrored from
# app/llm_cost_attribution.py -- the exact rates the ledger uses.
_FLASH_INPUT_RATE = 0.14e-6
_FLASH_OUTPUT_RATE = 0.28e-6
_FLASH_CACHE_READ_RATE = 0.0028e-6
_PRO_INPUT_RATE = 0.435e-6
_PRO_OUTPUT_RATE = 0.87e-6
_PRO_CACHE_READ_RATE = 0.003625e-6

_PRICING = {
    ("deepseek", "deepseek-v4-flash"): (_FLASH_INPUT_RATE, _FLASH_OUTPUT_RATE),
}
_CACHE_READ_PRICING = {
    ("deepseek", "deepseek-v4-flash"): _FLASH_CACHE_READ_RATE,
}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "cost_reconciliation", _SCRIPT_PATH
    )
    assert spec is not None, f"Could not locate script at {_SCRIPT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _task_row(
    task_id: str,
    *,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    created_at: str = "2026-08-05T00:00:00Z",
    estimated_cost_usd: float | None = None,
    attempts: list[dict] | None = None,
) -> dict:
    return {
        "id": task_id,
        "provider": provider,
        "model": model,
        "created_at": created_at,
        "estimated_cost_usd": estimated_cost_usd,
        "attempts": attempts or [],
    }


def _export(
    records: list[dict],
    *,
    provider: str = "deepseek",
    start: str = "2026-08-05",
    end: str = "2026-08-05",
) -> dict:
    return {
        "provider": provider,
        "currency": "USD",
        "exported_at": "2026-08-06T00:00:00Z",
        "window": {"start": start, "end": end},
        "records": records,
    }


def _usage_attempt(
    input_tokens: int = 1_000_000,
    output_tokens: int = 100_000,
    cache_read: int = 900_000,
    cache_creation: int = 0,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "call_count": 1,
    }


def test_script_is_importable_and_exposes_the_reconciliation_functions():
    """The script must load clean (stdlib only, no DB) and expose the API."""
    mod = _load_module()
    for name in (
        "reconcile_provider_cost",
        "load_export",
        "_task_to_row",
        "main",
    ):
        assert callable(getattr(mod, name, None)), f"missing {name}"


def test_internal_total_computed_from_recorded_usage_not_estimated_cost():
    """Internal totals come from recorded per-attempt usage, NEVER from
    ``estimated_cost_usd`` (the broken estimator the operator's export
    falsified). The task row carries a deliberately absurd
    ``estimated_cost_usd`` to prove the report ignores it.

    Attempt: 1M inclusive input, 900K cache-read, 0 cache-creation, 100K
    output, priced at flash rates:
      uncached    = 100_000 x $0.14/M      = $0.01400
      cache-read  = 900_000 x $0.0028/M    = $0.00252
      output      = 100_000 x $0.28/M      = $0.02800
      total                                  $0.04452
    """
    mod = _load_module()
    row = _task_row(
        "t1",
        estimated_cost_usd=99.0,  # would be used by the broken estimator
        attempts=[_usage_attempt()],
    )
    export = _export([], start="2026-08-05", end="2026-08-05")
    report = mod.reconcile_provider_cost(
        [row],
        export,
        "deepseek",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )

    internal = report["internal"]
    assert internal["total_cost_usd"] == pytest.approx(0.04452, abs=1e-9)
    assert internal["total_cost_usd"] != pytest.approx(99.0)
    assert internal["uncached_input_tokens"] == 100_000
    assert internal["cache_read_input_tokens"] == 900_000
    assert internal["output_tokens"] == 100_000
    assert internal["uncached_cost_usd"] == pytest.approx(0.014, abs=1e-9)
    assert internal["cache_read_cost_usd"] == pytest.approx(0.00252, abs=1e-9)
    assert internal["output_cost_usd"] == pytest.approx(0.028, abs=1e-9)
    assert report["tasks"]["with_usage"] == 1
    assert report["tasks"]["missing_usage"] == 0


def test_missing_usage_rows_produce_large_residual_with_correct_missing_count():
    """A synthetic window with deliberately missing usage rows produces a
    LARGE reported residual and a correct missing-task count -- the report
    does NOT silently match.

    Three tasks in window: one with recorded usage (~$0.04 internal), two with
    no recorded usage. Export total $10.00. The report must surface a ~$9.96
    residual (not a match), a missing-usage count of 2, and the proportional
    estimate of the missing-task share ((2/3) x $10 = $6.67).
    """
    mod = _load_module()
    rows = [
        _task_row("t1", attempts=[_usage_attempt()]),
        _task_row("t2"),  # missing usage
        _task_row("t3"),  # missing usage
    ]
    export = _export(
        [
            {
                "timestamp": "2026-08-05T12:00:00Z",
                "model": "deepseek-v4-flash",
                "input_tokens": 1_000_000,
                "cache_read_input_tokens": 9_000_000,
                "output_tokens": 1_000_000,
                "cost_usd": 10.0,
            }
        ],
        start="2026-08-05",
        end="2026-08-05",
    )
    report = mod.reconcile_provider_cost(
        rows,
        export,
        "deepseek",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )

    assert report["tasks"] == {
        "total": 3,
        "with_usage": 1,
        "missing_usage": 2,
        # The report rounds the coverage fraction to 4 decimals.
        "usage_coverage_fraction": round(1 / 3, 4),
    }
    internal_total = report["internal"]["total_cost_usd"]
    assert internal_total == pytest.approx(0.04452, abs=1e-9)
    # Large residual -- NOT a silent match.
    assert report["residual"]["absolute_usd"] == pytest.approx(
        10.0 - internal_total, abs=1e-6
    )
    assert report["residual"]["absolute_usd"] > 9.9
    assert report["residual"]["residual_fraction"] > 0.9
    # Proportional estimate of the missing-task share, clamped to the residual.
    assert report["residual"]["missing_usage_residual_estimate_usd"] == pytest.approx(
        (2 / 3) * 10.0, abs=1e-6
    )


def test_cache_read_breakout_is_populated_on_both_sides():
    """The cache-read vs uncached breakout is populated from recorded usage on
    the internal side and from the provider's token counts on the export side
    -- the suspected dominant term (unrecorded cache-read volume) is visible.
    """
    mod = _load_module()
    row = _task_row("t1", attempts=[_usage_attempt()])
    export = _export(
        [
            {
                "timestamp": "2026-08-05T12:00:00Z",
                "model": "deepseek-v4-flash",
                "input_tokens": 100_000,
                "cache_read_input_tokens": 900_000,
                "output_tokens": 100_000,
                "cost_usd": 0.2,
            }
        ],
        start="2026-08-05",
        end="2026-08-05",
    )
    report = mod.reconcile_provider_cost(
        [row],
        export,
        "deepseek",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )

    crb = report["cache_read_breakout"]
    assert crb["internal_tokens"] == 900_000
    assert crb["export_tokens"] == 900_000
    assert crb["internal_cost_usd"] == pytest.approx(0.00252, abs=1e-9)
    assert crb["export_cost_usd"] == pytest.approx(
        900_000 * _FLASH_CACHE_READ_RATE, abs=1e-9
    )
    assert crb["internal_cost_usd"] > 0
    assert crb["export_cost_usd"] > 0

    ucb = report["uncached_breakout"]
    assert ucb["internal_tokens"] == 100_000
    assert ucb["export_tokens"] == 100_000
    assert ucb["internal_cost_usd"] == pytest.approx(0.014, abs=1e-9)
    assert ucb["export_cost_usd"] == pytest.approx(
        100_000 * _FLASH_INPUT_RATE, abs=1e-9
    )
    assert ucb["internal_cost_usd"] > 0
    assert ucb["export_cost_usd"] > 0


def test_window_filters_tasks_and_export_records():
    """Tasks and export records outside the reconciliation window are excluded."""
    mod = _load_module()
    rows = [
        _task_row("in-window", created_at="2026-08-05T00:00:00Z", attempts=[_usage_attempt()]),
        _task_row("before", created_at="2026-08-01T00:00:00Z", attempts=[_usage_attempt()]),
        _task_row("after", created_at="2026-08-07T00:00:00Z", attempts=[_usage_attempt()]),
    ]
    export = _export(
        [
            {
                "timestamp": "2026-08-05T12:00:00Z",
                "model": "deepseek-v4-flash",
                "input_tokens": 1,
                "cache_read_input_tokens": 1,
                "output_tokens": 1,
                "cost_usd": 1.0,
            },
            {
                "timestamp": "2026-08-04T12:00:00Z",  # outside window
                "model": "deepseek-v4-flash",
                "input_tokens": 1,
                "cache_read_input_tokens": 1,
                "output_tokens": 1,
                "cost_usd": 2.0,
            },
            {
                "timestamp": "2026-08-06T12:00:00Z",  # outside window
                "model": "deepseek-v4-flash",
                "input_tokens": 1,
                "cache_read_input_tokens": 1,
                "output_tokens": 1,
                "cost_usd": 3.0,
            },
        ],
        start="2026-08-05",
        end="2026-08-05",
    )
    report = mod.reconcile_provider_cost(
        rows,
        export,
        "deepseek",
        start="2026-08-05",
        end="2026-08-05",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )

    assert report["tasks"]["total"] == 1
    assert report["tasks"]["with_usage"] == 1
    assert report["export"]["record_count"] == 1
    assert report["export"]["total_cost_usd"] == pytest.approx(1.0, abs=1e-9)
    # Only the in-window task's usage is priced.
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.04452, abs=1e-9)


def test_provider_filter_excludes_other_providers():
    """Task rows for other providers are excluded from the internal total."""
    mod = _load_module()
    rows = [
        _task_row("deepseek-task", provider="deepseek", attempts=[_usage_attempt()]),
        _task_row(
            "openai-task",
            provider="openai",
            model="gpt-5.6-terra",
            attempts=[_usage_attempt()],
        ),
    ]
    export = _export([], start="2026-08-05", end="2026-08-05")
    report = mod.reconcile_provider_cost(
        rows,
        export,
        "deepseek",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )
    assert report["tasks"]["total"] == 1
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.04452, abs=1e-9)


def test_unpriced_model_records_count_usage_but_zero_cost():
    """A task with recorded tokens on an unpriced model is still counted as
    having usage (with_usage), even though it contributes 0 cost -- the same
    behavior as the ledger's unpriced combos. The export side keeps the
    provider's own ``cost_usd`` in the headline total and reports the record as
    unpriced in the per-class estimate.
    """
    mod = _load_module()
    row = _task_row(
        "t-unpriced", model="deepseek-v4-unknown", attempts=[_usage_attempt()]
    )
    export = _export(
        [
            {
                "timestamp": "2026-08-05T12:00:00Z",
                "model": "deepseek-v4-unknown",
                "input_tokens": 1_000,
                "cache_read_input_tokens": 1_000,
                "output_tokens": 1_000,
                "cost_usd": 5.0,
            }
        ],
        start="2026-08-05",
        end="2026-08-05",
    )
    report = mod.reconcile_provider_cost(
        [row],
        export,
        "deepseek",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )
    assert report["tasks"]["with_usage"] == 1
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.0, abs=1e-12)
    # The provider's own cost still lands in the export total.
    assert report["export"]["total_cost_usd"] == pytest.approx(5.0, abs=1e-9)
    assert report["export"]["unpriced_record_count"] == 1


def test_mixed_tier_attempts_price_each_at_its_own_model():
    """DEFECT 1 regression: a task whose attempts ran on DIFFERENT models
    prices each attempt at its OWN recorded rate, not the task-level model.

    Attempt 1 (deepseek-v4-flash): 1M input + 100K output
      uncached = 1_000_000 x $0.14/M   = $0.14
      output   =   100_000 x $0.28/M   = $0.028
      total                               $0.168
    Attempt 2 (deepseek-v4-pro): 1M input + 100K output
      uncached = 1_000_000 x $0.435/M  = $0.435
      output   =   100_000 x $0.87/M   = $0.087
      total                               $0.522
    Combined = $0.69. Pricing BOTH attempts at the task-level flash model
    would give $0.336 -- the assertion below fails against today's code.
    """
    mod = _load_module()
    pricing = {
        ("deepseek", "deepseek-v4-flash"): (_FLASH_INPUT_RATE, _FLASH_OUTPUT_RATE),
        ("deepseek", "deepseek-v4-pro"): (_PRO_INPUT_RATE, _PRO_OUTPUT_RATE),
    }
    cache_pricing = {
        ("deepseek", "deepseek-v4-flash"): _FLASH_CACHE_READ_RATE,
        ("deepseek", "deepseek-v4-pro"): _PRO_CACHE_READ_RATE,
    }
    row = _task_row(
        "t-mixed",
        model="deepseek-v4-flash",  # task-level model
        attempts=[
            {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "call_count": 1,
            },
            {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "call_count": 1,
            },
        ],
    )
    export = _export([], start="2026-08-05", end="2026-08-05")
    report = mod.reconcile_provider_cost(
        [row], export, "deepseek", pricing=pricing, cache_read_pricing=cache_pricing
    )
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.69, abs=1e-9)
    # The all-attempts-at-task-model computation would be $0.336; the report
    # must differ from it, proving each attempt was priced at its own model.
    assert report["internal"]["total_cost_usd"] != pytest.approx(0.336, abs=1e-9)
    # Both attempts carried their own model, so no task-level fallback ran.
    assert report["internal"]["task_model_fallback_attempt_count"] == 0


def test_attempt_without_model_falls_back_to_task_model_and_is_counted():
    """An attempt whose per-attempt provider/model is genuinely absent is
    priced at the task-level model AND that fallback is OBSERVABLE in the
    report (not silent) -- the fallback counter is the visibility the defect
    was missing."""
    mod = _load_module()
    row = _task_row(
        "t-fallback",
        model="deepseek-v4-flash",
        attempts=[_usage_attempt()],  # no per-attempt provider/model
    )
    export = _export([], start="2026-08-05", end="2026-08-05")
    report = mod.reconcile_provider_cost(
        [row],
        export,
        "deepseek",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.04452, abs=1e-9)
    assert report["internal"]["task_model_fallback_attempt_count"] == 1


def test_half_filled_attempt_is_unpriced_not_blended_with_task_model():
    """An attempt with a provider but no model (or vice versa) is NOT priced
    at the task-level model -- that would manufacture a chimera combo. It is
    unpriced, matching ``app.llm_cost_attribution._resolve_pair``."""
    mod = _load_module()
    half_filled = _usage_attempt()
    half_filled["provider"] = "deepseek"  # model absent -> half-filled
    row = _task_row(
        "t-half",
        model="deepseek-v4-flash",
        attempts=[half_filled],
    )
    export = _export([], start="2026-08-05", end="2026-08-05")
    report = mod.reconcile_provider_cost(
        [row],
        export,
        "deepseek",
        pricing=_PRICING,
        cache_read_pricing=_CACHE_READ_PRICING,
    )
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.0, abs=1e-12)
    assert report["tasks"]["with_usage"] == 1  # recorded usage, just unpriced
    assert report["internal"]["task_model_fallback_attempt_count"] == 0


def test_sample_export_fixture_loads_and_reconciles():
    """The checked-in sample DeepSeek export loads, its records sum to the
    declared total, and the window carries a large residual when no internal
    usage is present (today's honest state)."""
    mod = _load_module()
    export = mod.load_export(_SCRIPT_PATH.parent / "deepseek_billing_export_sample.json")
    assert export["provider"] == "deepseek"
    assert len(export["records"]) == 8

    report = mod.reconcile_provider_cost(
        [], export, "deepseek", pricing=_PRICING, cache_read_pricing=_CACHE_READ_PRICING
    )
    # Sum of the fixture's per-record cost_usd values.
    assert report["export"]["total_cost_usd"] == pytest.approx(42.9763, abs=1e-4)
    assert report["export"]["record_count"] == 8
    assert report["export"]["cache_read_input_tokens"] == 518_000_000
    # With no internal usage recorded, the residual is the full export total.
    assert report["residual"]["absolute_usd"] == pytest.approx(42.9763, abs=1e-4)
    assert report["residual"]["residual_fraction"] == pytest.approx(1.0, abs=1e-6)
    # The cache-read breakout is populated from the export side.
    assert report["cache_read_breakout"]["export_tokens"] == 518_000_000
    assert report["cache_read_breakout"]["export_cost_usd"] > 0


def test_report_printer_shows_the_residual_without_a_db():
    """The CLI printer surfaces the residual (the artifact an operator reads)
    without needing a database -- fed a precomputed report."""
    mod = _load_module()
    rows = [
        _task_row("t1", attempts=[_usage_attempt()]),
        _task_row("t2"),
    ]
    export = _export(
        [
            {
                "timestamp": "2026-08-05T12:00:00Z",
                "model": "deepseek-v4-flash",
                "input_tokens": 1,
                "cache_read_input_tokens": 1,
                "output_tokens": 1,
                "cost_usd": 10.0,
            }
        ],
        start="2026-08-05",
        end="2026-08-05",
    )
    report = mod.reconcile_provider_cost(
        rows, export, "deepseek", pricing=_PRICING, cache_read_pricing=_CACHE_READ_PRICING
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod._print_report(report, rows, export)
    text = buf.getvalue()
    assert "RESIDUAL" in text
    assert "MISSING usage" in text
    assert "NEVER estimated_cost_usd" in text
    assert "the finding, not a pass/fail" in text


def test_no_module_level_pricing_mirror_and_defaults_resolve_through_canonical():
    """DEFECT 2 regression: the script has NO module-level copy of the pricing
    tables (a hand-maintained mirror would drift from the ledger), and the
    default pricing source resolves to the canonical ``app.llm_cost_attribution``
    tables by identity. This test fails if a mirror is reintroduced."""
    mod = _load_module()
    # No module-level mirror to silently read stale prices from.
    assert not hasattr(mod, "LLM_TOKEN_PRICING")
    assert not hasattr(mod, "LLM_CACHE_READ_INPUT_PRICING")
    assert not hasattr(mod, "CACHE_READ_INPUT_RATE_MULTIPLIER")
    assert not hasattr(mod, "CACHE_CREATION_INPUT_RATE_MULTIPLIER")

    from app.llm_cost_attribution import (
        CACHE_CREATION_INPUT_RATE_MULTIPLIER,
        CACHE_READ_INPUT_RATE_MULTIPLIER,
        LLM_CACHE_READ_INPUT_PRICING,
        LLM_TOKEN_PRICING,
    )

    ctx = mod._default_pricing_context()
    # Identity (not a copy): the script's default source IS the canonical table.
    assert ctx["token_pricing"] is LLM_TOKEN_PRICING
    assert ctx["cache_read_pricing"] is LLM_CACHE_READ_INPUT_PRICING
    assert ctx["cache_read_multiplier"] == CACHE_READ_INPUT_RATE_MULTIPLIER
    assert ctx["cache_creation_multiplier"] == CACHE_CREATION_INPUT_RATE_MULTIPLIER


def test_default_pricing_resolves_through_canonical_app_table():
    """Calling ``reconcile_provider_cost`` WITHOUT explicit pricing prices
    through the canonical ``app.llm_cost_attribution`` table (a deepseek-v4-flash
    attempt costs the flash rate), so the CLI's default path can never silently
    read a stale local mirror."""
    mod = _load_module()
    row = _task_row("t-default", attempts=[_usage_attempt()])
    export = _export([], start="2026-08-05", end="2026-08-05")
    report = mod.reconcile_provider_cost([row], export, "deepseek")
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.04452, abs=1e-9)
    assert report["tasks"]["with_usage"] == 1


@pytest.mark.asyncio
async def test_db_task_rows_round_trip_through_row_conversion_and_reconcile(db_session):
    """A real ORM Task with api_call_summary reduces through _task_to_row and
    reconciles -- exercising the production read path (the same
    ``_task_llm_attempts`` iterator the ledger prices)."""
    from app.llm_cost_attribution import _task_llm_attempts  # lazy: stdlib-only module load
    from app.models import Project  # lazy: stdlib-only module load
    from testing.factories import make_task

    project = Project(name="Cost Reconciliation", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        provider="deepseek",
        model="deepseek-v4-flash",
        created_at=datetime(2026, 8, 5, 0, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None),
        api_call_summary={
            "llm_call_count": 1,
            "total_input_tokens": 1_000_000,
            "total_output_tokens": 100_000,
            "total_cache_creation_input_tokens": 0,
            "total_cache_read_input_tokens": 900_000,
            "last_updated_at": "2026-08-05T01:00:00Z",
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mod = _load_module()
    row = mod._task_to_row(task, _task_llm_attempts(task))
    assert row["provider"] == "deepseek"
    assert row["attempts"], "expected recorded usage on the current span"
    assert row["attempts"][0]["input_tokens"] == 1_000_000
    assert row["attempts"][0]["cache_read_input_tokens"] == 900_000

    export = _export([], start="2026-08-05", end="2026-08-05")
    report = mod.reconcile_provider_cost(
        [row], export, "deepseek", pricing=_PRICING, cache_read_pricing=_CACHE_READ_PRICING
    )
    assert report["internal"]["total_cost_usd"] == pytest.approx(0.04452, abs=1e-6)
    assert report["tasks"]["with_usage"] == 1

    # Exercise the real production DB read path (_load_task_rows) against the
    # same committed Task.
    rows = await mod._load_task_rows("deepseek", "2026-08-05", "2026-08-05")
    assert len(rows) == 1
    assert rows[0]["id"] == str(task.id)
    report2 = mod.reconcile_provider_cost(
        rows, export, "deepseek", pricing=_PRICING, cache_read_pricing=_CACHE_READ_PRICING
    )
    assert report2["internal"]["total_cost_usd"] == pytest.approx(0.04452, abs=1e-6)
    assert report2["tasks"]["with_usage"] == 1
