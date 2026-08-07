"""Tests for the spend-cap trustworthiness surface.

The project-level spend cap gates dispatch on a confidence-weighted LLM figure
(``ceiling_gate_total``) that inflates self-reported non-Anthropic cost by
``NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER``. Before that figure can be trusted,
it must be verified against real recorded spend: the gate must be a CONSERVATIVE
upper bound that can never under-count real spend (an inaccurate cap is worse
than none).

These tests exercise :func:`assess_ceiling_gate_trustworthiness` and
:func:`spend_confidence_label` (backend/app/resource_envelope.py) against real
Task rows: the conservative-upper-bound invariant, the exact buffer, the
confidence-label transitions, and Anthropic reconciliation coverage.
"""

from __future__ import annotations

from testing.factories import make_project, make_task

from app.llm_cost_attribution import (
    MIN_TRUSTWORTHY_PROBE_COVERAGE,
    NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER,
    _task_llm_cost_split,
)
from app.models import TaskStatus
from app.resource_envelope import (
    assess_ceiling_gate_trustworthiness,
    spend_confidence_label,
)

# Priced rates used below (from LLM_TOKEN_PRICING):
#   anthropic/claude-sonnet-4-6 : (3e-6, 15e-6)
#   openai/gpt-5.5              : (5e-6, 30e-6)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_task(project_id, *, provider, model, input_tokens, output_tokens,
               with_probe=False, **kw):
    """Create a COMPLETED task with LLM token totals, optionally carrying a
    matching provider-reported probe (for Anthropic reconciliation)."""
    defaults = {
        "project_id": project_id,
        "status": TaskStatus.COMPLETED,
        "context": {"pr_merged": True},
        "api_call_summary": {
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "llm_call_count": 5,
        },
        "provider": provider,
        "model": model,
    }
    if with_probe:
        # No cache sub-counts, so provider input == inclusive api_call_summary input.
        defaults["provider_reported_usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    defaults.update(kw)
    return make_task(**defaults)


def _anthropic_task(project_id, **kw):
    return _make_task(
        project_id, provider="anthropic", model="claude-sonnet-4-6",
        input_tokens=1000, output_tokens=500, with_probe=True, **kw,
    )


def _openai_task(project_id, **kw):
    return _make_task(
        project_id, provider="openai", model="gpt-5.5",
        input_tokens=1000, output_tokens=500, **kw,
    )


# ── Conservative-upper-bound invariant (named acceptance test) ─────────────


def test_ceiling_gate_total_is_conservative_upper_bound():
    """ceiling_gate_total >= total for Anthropic-only, mixed, and all-non-Anthropic
    task sets — the gate can never under-count real recorded spend.

    This is the core 'inaccurate cap is worse than none' invariant. It is verified
    against production spend math (assess_ceiling_gate_trustworthiness), not a
    hand-constructed number.
    """
    pid = make_project().id

    anthropic_only = [_anthropic_task(pid), _anthropic_task(pid)]
    mixed = [_anthropic_task(pid), _openai_task(pid)]
    all_non_anthropic = [_openai_task(pid), _openai_task(pid)]

    for label, tasks in (
        ("anthropic_only", anthropic_only),
        ("mixed", mixed),
        ("all_non_anthropic", all_non_anthropic),
    ):
        verdict = assess_ceiling_gate_trustworthiness(tasks)
        assert verdict["ceiling_gate_total"] >= verdict["total"], (
            f"{label}: ceiling_gate_total {verdict['ceiling_gate_total']} "
            f"< total {verdict['total']} — gate under-counts recorded spend"
        )
        assert verdict["conservative_upper_bound"] is True, label

    # Anthropic-only: nothing self-reported, so the weighted figure EQUALS the raw
    # total — the gate adds no buffer where it has an independent probe.
    a_verdict = assess_ceiling_gate_trustworthiness(anthropic_only)
    assert a_verdict["ceiling_gate_total"] == a_verdict["total"]
    assert a_verdict["non_anthropic_buffer_usd"] == 0.0

    # All-non-Anthropic: the whole LLM cost is self-reported, so the gate strictly
    # OVER-counts (ceiling_gate_total > total).
    na_verdict = assess_ceiling_gate_trustworthiness(all_non_anthropic)
    assert na_verdict["ceiling_gate_total"] > na_verdict["total"]


def test_buffer_equals_multiplier_minus_one_times_non_anthropic_cost():
    """(b) The non-Anthropic buffer is exactly (multiplier - 1.0) * priced
    non-Anthropic cost — no more, no less."""
    pid = make_project().id
    tasks = [_anthropic_task(pid), _openai_task(pid)]

    _, non_anthropic_cost, _ = _task_llm_cost_split(tasks[1])
    verdict = assess_ceiling_gate_trustworthiness(tasks)

    expected_buffer = (NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER - 1.0) * non_anthropic_cost
    assert abs(verdict["non_anthropic_buffer_usd"] - expected_buffer) < 1e-12
    assert verdict["buffer_exact"] is True
    assert abs(
        verdict["ceiling_gate_total"] - (verdict["total"] + expected_buffer)
    ) < 1e-12


# ── Anthropic reconciliation coverage (c, d) ───────────────────────────────


def test_anthropic_only_reconciles_within_tolerance_and_full_coverage():
    """(c) For an Anthropic-only set the independent probe reconciles within
    tolerance, coverage_fraction is 1.0, and the set is trustworthy."""
    pid = make_project().id
    tasks = [_anthropic_task(pid), _anthropic_task(pid)]

    verdict = assess_ceiling_gate_trustworthiness(tasks)

    assert verdict["provider_reported_within_tolerance"] is True
    assert verdict["coverage_fraction"] == 1.0
    assert verdict["non_anthropic_share"] == 0.0
    assert verdict["trustworthy"] is True
    assert verdict["reconciliation"]["source"] == "provider_reported"


def test_partial_coverage_reports_self_reported_share():
    """(d) A mixed set reports partial coverage and a non-zero self-reported
    share of total LLM cost."""
    pid = make_project().id
    tasks = [_anthropic_task(pid), _openai_task(pid)]

    anthropic_cost, non_anthropic_cost, _ = (0.0, 0.0, False)
    for t in tasks:
        a, na, _ = _task_llm_cost_split(t)
        anthropic_cost += a
        non_anthropic_cost += na
    total_llm = anthropic_cost + non_anthropic_cost

    verdict = assess_ceiling_gate_trustworthiness(tasks)

    # Only the Anthropic task carries a probe → 50% coverage.
    assert verdict["coverage_fraction"] == 0.5
    assert abs(verdict["non_anthropic_share"] - (non_anthropic_cost / total_llm)) < 1e-12
    assert 0.0 < verdict["non_anthropic_share"] < 1.0


def test_all_non_anthropic_has_zero_coverage():
    """An all-self-reported window reports 0.0 coverage (not None) so the operator
    can see the figure is entirely unverified.

    Zero coverage means the Anthropic probe (the only independent component) was
    verified against NOTHING — the weighted figure is a conservative bound, but
    it is NOT a trustworthy verdict from zero evidence.
    """
    pid = make_project().id
    tasks = [_openai_task(pid), _openai_task(pid)]

    verdict = assess_ceiling_gate_trustworthiness(tasks)

    assert verdict["coverage_fraction"] == 0.0
    assert verdict["non_anthropic_share"] == 1.0
    # Still a conservative upper bound...
    assert verdict["conservative_upper_bound"] is True
    # ...but NOT trustworthy: no task carried an independent probe.
    assert verdict["provider_reported_verified"] is False
    assert verdict["trustworthy"] is False
    assert (
        verdict["reason"]
        == "no task carried provider-reported usage — nothing independently verified"
    )


def test_empty_task_set_coverage_is_none():
    """No tasks → coverage_fraction is None and totals are zero.

    An empty window verified nothing, so it must not report trustworthy.
    """
    verdict = assess_ceiling_gate_trustworthiness([])
    assert verdict["coverage_fraction"] is None
    assert verdict["total"] == 0.0
    assert verdict["ceiling_gate_total"] == 0.0
    assert verdict["conservative_upper_bound"] is True
    assert verdict["provider_reported_verified"] is False
    assert verdict["trustworthy"] is False


def test_mixed_probe_coverage_is_trustworthy_when_all_probed_reconcile():
    """A mixed window where the probed Anthropic tasks reconcile within tolerance
    IS trustworthy despite partial coverage — the non-Anthropic share is buffered
    and the independent portion was genuinely verified."""
    pid = make_project().id
    tasks = [_anthropic_task(pid), _openai_task(pid)]

    verdict = assess_ceiling_gate_trustworthiness(tasks)

    assert verdict["coverage_fraction"] == 0.5
    assert verdict["provider_reported_verified"] is True
    assert verdict["provider_reported_within_tolerance"] is True
    assert verdict["coverage_sufficient"] is True
    assert verdict["trustworthy"] is True


# ── Probe-coverage gate (e) ────────────────────────────────────────────────


def test_thin_probe_coverage_is_not_trustworthy():
    """A window with ~10% probe coverage must NOT report trustworthy.

    ``provider_reported_verified`` only proves SOMETHING was independently
    checked; one probed task out of ten still lets the gate claim to be a
    conservative upper bound on real spend. Coverage must be sufficient too —
    the operator's billing export shows internal attribution capturing only
    ~22-25% of real DeepSeek spend, so a verdict certified from a thin probe
    slice is exactly the false confidence that let a ~10% sampled run pass.
    """
    pid = make_project().id
    tasks = [_anthropic_task(pid)] + [_openai_task(pid) for _ in range(9)]

    verdict = assess_ceiling_gate_trustworthiness(tasks)

    assert verdict["coverage_fraction"] == 0.1
    assert verdict["provider_reported_verified"] is True
    assert verdict["provider_reported_within_tolerance"] is True
    assert verdict["coverage_sufficient"] is False
    assert verdict["min_coverage_fraction"] == MIN_TRUSTWORTHY_PROBE_COVERAGE
    assert verdict["trustworthy"] is False
    assert "coverage" in verdict["reason"].lower()
    assert "below" in verdict["reason"].lower()


def test_coverage_threshold_parameter_raises_requirement():
    """A window that passes the default bar can still fail a stricter one."""
    pid = make_project().id
    # 1 probed Anthropic task of 2 → 50% coverage, exactly at the default bar.
    tasks = [_anthropic_task(pid), _openai_task(pid)]

    default_verdict = assess_ceiling_gate_trustworthiness(tasks)
    assert default_verdict["coverage_sufficient"] is True
    assert default_verdict["trustworthy"] is True

    strict_verdict = assess_ceiling_gate_trustworthiness(
        tasks, min_coverage_fraction=0.9
    )
    assert strict_verdict["coverage_sufficient"] is False
    assert strict_verdict["min_coverage_fraction"] == 0.9
    assert strict_verdict["trustworthy"] is False
    assert "coverage" in strict_verdict["reason"].lower()


# ── Buffer-adequacy gate (f) ───────────────────────────────────────────────


def test_buffer_adequate_fails_when_measured_capture_below_multiplier():
    """A measured capture ratio the flat buffer cannot cover must fail.

    The operator's billing export shows internal attribution capturing only
    ~22-25% of real DeepSeek spend; a 1.25x buffer cannot absorb a ~4x
    under-count, so the verdict must not certify the gate as a conservative
    upper bound.
    """
    pid = make_project().id
    tasks = [_anthropic_task(pid), _openai_task(pid)]

    verdict = assess_ceiling_gate_trustworthiness(
        tasks, provider_capture_fractions={"deepseek": 0.25}
    )

    assert verdict["buffer_adequate"] is False
    assert verdict["min_provider_capture_fraction"] == 0.25
    assert verdict["coverage_sufficient"] is True
    assert verdict["trustworthy"] is False
    assert "capture" in verdict["reason"].lower()


def test_buffer_adequate_passes_when_capture_covered():
    """A capture ratio inside the buffer's reach keeps the verdict green."""
    pid = make_project().id
    tasks = [_anthropic_task(pid), _openai_task(pid)]

    verdict = assess_ceiling_gate_trustworthiness(
        tasks, provider_capture_fractions={"deepseek": 0.9}
    )

    assert verdict["buffer_adequate"] is True
    assert verdict["min_provider_capture_fraction"] == 0.9
    assert verdict["trustworthy"] is True


# ── Confidence label transitions ───────────────────────────────────────────


def test_confidence_label_verified_for_anthropic_only():
    pid = make_project().id
    verdict = assess_ceiling_gate_trustworthiness([_anthropic_task(pid)])
    assert verdict["confidence"] == "verified"


def test_confidence_label_estimated_when_non_anthropic_present():
    pid = make_project().id
    verdict = assess_ceiling_gate_trustworthiness(
        [_anthropic_task(pid), _openai_task(pid)]
    )
    assert verdict["confidence"] == "estimated"


def test_confidence_label_low_when_unpriced_share_present():
    """A model/provider with no pricing row is UNPRICED/unverifiable → 'low'."""
    pid = make_project().id
    unpriced = _make_task(
        pid, provider="openai", model="no-such-model-xyz",
        input_tokens=1000, output_tokens=500,
    )
    verdict = assess_ceiling_gate_trustworthiness([unpriced])
    assert verdict["any_unpriced"] is True
    assert verdict["confidence"] == "low"


def test_spend_confidence_label_direct():
    """Direct unit coverage of the label function's three branches."""
    assert spend_confidence_label(non_anthropic_llm=0.0, any_unpriced=False) == "verified"
    assert spend_confidence_label(non_anthropic_llm=1.0, any_unpriced=False) == "estimated"
    assert spend_confidence_label(non_anthropic_llm=1.0, any_unpriced=True) == "low"
    # unpriced dominates even when non-Anthropic is zero.
    assert spend_confidence_label(non_anthropic_llm=0.0, any_unpriced=True) == "low"
