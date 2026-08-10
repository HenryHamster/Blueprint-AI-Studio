"""Standalone tests for backend/scripts/strategic_cycle_cost_projection.py.

No database required.  Run with:
    pytest testing/test_strategic_cycle_cost_projection.py -v --noconftest

These tests pin the PROJECTION's honest behavior, not a target number: the
v28 headline claims a median strategist cycle under $1.50, but the model must
derive the number from stated inputs rather than tune backwards to it. The
assertions here lock in the findings in docs/strategic-cycle-cost-projection.md:

  * the honest base case (tier mix R=0.7 alone, cache share unchanged) is
    ABOVE $1.50;
  * even the tier ceiling (R=1.0) cannot reach $1.50 without the cache lever;
  * the cache-share lever only helps when the share actually rises (the prefix
    reorder alone cannot deliver it — see the cross-cycle bound);
  * the rendered stable prefix carries the large majority of INSTRUCTION bytes
    but well under half of TOTAL prompt bytes (the payload dominates).

Monthly-spend tests (criterion c-3fd62b9d) pin the extension added on top of
the per-cycle projection:

  * monthly = per-cycle cost x cycle volume, with the arithmetic explicit;
  * the volume anchors come from the measured 59-cycles/7-day basis and the
    strategic_max_interval_minutes cadence default — not a magic number;
  * no stored ``target_concurrency`` config exists, and when a volume input is
    unavailable the projection refuses to guess (available=False);
  * the monthly TOTAL is decision-loop cost + task-execution cost — the
    per-task term sourced from the measured fleet basis (75 tasks, $15.46 LLM
    + $8.00 VM over the 7-day window), and when per-task inputs are missing
    the total surfaces an explicit gap rather than silently dropping to zero;
  * the honest base case at the measured volume is comfortably within the
    $3,000/month envelope — an honest result, not a tuned one.

A number tuned backwards from $1.50 would fail these tests.

The baseline-anchor tests pin the fix that replaced the task-provided
$4.41/cycle constant: ``BASELINE_COST_PER_CYCLE_USD`` is derived from the
recorded baseline readings (mean of their ``cost_per_cycle``),
``baseline_per_cycle_stats()`` reconciles mean vs median, and
``headline_claim()`` labels both statistics instead of silently conflating them.
"""
import contextlib
import importlib.util
import io
import json
import math
import statistics
from itertools import pairwise
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "strategic_cycle_cost_projection.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("strategic_cycle_cost_projection", _SCRIPT_PATH)
    assert spec is not None, f"Could not locate script at {_SCRIPT_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_script_is_importable_and_exposes_the_model_functions():
    """The script must load clean (stdlib only, no DB) and expose the model."""
    mod = _load_module()
    for name in (
        "project_cycle_cost",
        "rendered_instruction_share",
        "effective_input_price_multiplier",
        "tier_blended_multiplier",
        "cross_cycle_saving_per_cycle",
        "cadence_cycles_per_month",
        "measured_cycles_per_month",
        "mean_task_execution_cost_usd",
        "measured_tasks_per_month",
        "target_concurrency_status",
        "cohort_capped",
        "baseline_record",
        "baseline_per_cycle_stats",
        "serving_sha_record",
        "serving_sha_comparison",
        "project_monthly_spend",
        "project_monthly_spend_with_tasks",
        "load_measured_split",
        "routine_share_from_split",
        "routine_share_source",
        "headline_routine_share",
        "headline_claim",
        "main",
    ):
        assert callable(getattr(mod, name, None)), f"missing {name}"


def test_rendered_instruction_share_names_the_denominators():
    """Stable prefix share: >80% of INSTRUCTION bytes, <50% of TOTAL prompt bytes.

    This mirrors measureStrategicPromptInstructionShare in
    agent/src/prompts/thinker.ts. The claim that holds is prefix-vs-instruction
    bytes; against total prompt bytes the interpolated payload dominates.
    """
    mod = _load_module()
    share = mod.rendered_instruction_share()
    assert share["stable_share_of_instruction"] > 0.8
    assert share["stable_share_of_total"] < 0.5
    assert share["stable_share_of_total"] < share["stable_share_of_instruction"]
    assert share["stable_prefix_bytes"] > 30_000
    assert share["total_prompt_bytes"] > 2 * share["stable_prefix_bytes"]


def test_honest_base_case_is_above_the_headline_target():
    """Tier mix alone (R=0.7, cache share unchanged) projects ABOVE $1.50."""
    mod = _load_module()
    honest = mod.project_cycle_cost(0.70, mod.BASELINE_CACHE_READ_SHARE, 0.0)
    assert honest > 1.50, f"honest base case must not be tuned to the target: {honest:.2f}"


def test_tier_ceiling_alone_cannot_reach_the_target():
    """Even R=1.0 (every cycle on the half-price tier) stays above $1.50."""
    mod = _load_module()
    ceiling = mod.project_cycle_cost(1.0, mod.BASELINE_CACHE_READ_SHARE, 0.0)
    assert ceiling > 1.50, f"tier ceiling alone must stay above target: {ceiling:.2f}"


def test_tier_lever_is_monotonic():
    """More routine share => strictly lower projected cost."""
    mod = _load_module()
    costs = [mod.project_cycle_cost(r, 0.55, 0.0) for r in (0.3, 0.5, 0.7, 0.85, 0.9, 1.0)]
    assert all(b < a for a, b in pairwise(costs))


def test_cache_share_lever_only_helps_when_the_share_rises():
    """A real 0.55->0.80 share rise cuts cost; leaving it at 0.55 does not."""
    mod = _load_module()
    unchanged = mod.project_cycle_cost(0.70, 0.55, 0.0)
    raised = mod.project_cycle_cost(0.70, 0.80, 0.0)
    assert raised < unchanged


def test_direction_target_case_lands_at_the_documented_boundary():
    """At the direction's own target inputs (share 0.80, R=0.85) cost ~$2.59.

    The doc reports $2.59; the target is on the edge of the optimistically
    parameterized range, not comfortably under it.
    """
    mod = _load_module()
    target_case = mod.project_cycle_cost(0.85, 0.80, 0.0)
    assert 2.40 < target_case < 2.80, f"unexpected boundary value: {target_case:.2f}"


def test_cross_cycle_term_is_bounded_small():
    """Even at full cache survival the cross-cycle prefix saving is < $0.05."""
    mod = _load_module()
    saving = mod.cross_cycle_saving_per_cycle(0.70, 1.0)
    assert saving < 0.05


def test_projection_headline_uses_measured_routine_share(tmp_path, monkeypatch):
    """The >=30%-below-baseline headline derives from the MEASURED effort-tier
    split, never an assumed routine share.

    Proves the wiring three ways:
      (a) a measured split's routine_share feeds the headline cost and the
          below-baseline percentage;
      (b) changing the measured value changes the headline (so it is wired to
          the split, not a hardcoded constant);
      (c) an unmeasured split FAILS CLOSED -- available=False, claim UNVERIFIED,
          no cost -- so reintroducing an assumed share (the original defect)
          cannot silently resurrect the claim.
    """
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"

    def write_split(routine_share, status="measured", denominator=20):
        split_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "measurement_status": status,
                    "routine_share": routine_share,
                    "split": {
                        "routine": routine_share,
                        "consequential": (
                            None if routine_share is None else 1.0 - routine_share
                        ),
                        "pinned": 0.0,
                        "other": 0.0,
                    },
                    "counts": (
                        {"routine": round(denominator * routine_share)}
                        if routine_share is not None
                        else {}
                    ),
                    "denominator_cycles": denominator,
                    "completed_missing_tier_count": 0,
                    "non_completed_with_tier_count": 0,
                    "rows_considered": denominator,
                    "window_days": 7,
                    "window_start": "2026-07-30T00:00:00+00:00",
                    "window_end": "2026-08-06T00:00:00+00:00",
                    "measured_at": "2026-08-06T00:00:00+00:00",
                    "provenance": "test fixture",
                }
            )
        )

    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)

    # (a) measured R drives the headline.
    write_split(0.85)
    claim = mod.headline_claim()
    assert claim["available"] is True
    assert claim["routine_share"] == 0.85
    expected_cost = mod.project_cycle_cost(0.85, mod.BASELINE_CACHE_READ_SHARE, 0.0)
    assert claim["cost_per_cycle_usd"] == pytest.approx(expected_cost)
    expected_below = (
        mod.BASELINE_COST_PER_CYCLE_USD - expected_cost
    ) / mod.BASELINE_COST_PER_CYCLE_USD
    assert claim["below_baseline_pct"] == pytest.approx(expected_below)
    assert claim["claim_verified"] is True

    # (b) a different measured value moves the headline (wired to the split,
    # not a constant).
    write_split(0.30)
    claim_low = mod.headline_claim()
    assert claim_low["available"] is True
    assert claim_low["routine_share"] == 0.30
    assert claim_low["cost_per_cycle_usd"] > expected_cost
    # The low measured R falsifies the >=30% claim.
    assert claim_low["claim_verified"] is False

    # (c) unmeasured split -> fail closed: UNVERIFIED, no assumed share.
    write_split(None, status="unmeasured")
    claim_unmeasured = mod.headline_claim()
    assert claim_unmeasured["available"] is False
    assert claim_unmeasured["routine_share"] is None
    assert claim_unmeasured["cost_per_cycle_usd"] is None
    assert claim_unmeasured["below_baseline_pct"] is None
    assert claim_unmeasured["claim_verified"] is False
    assert "unmeasured" in claim_unmeasured["reason"].lower()


# --- Replayed fail-closed lower bound (strategic_effort_tier_evidence --replay)
# --- The replayed payload schema is produced by split_to_json_payload:
# --- measurement_status 'replayed', bound_kind 'fail_closed_lower_bound',
# --- top-level routine_share = the replayed LOWER BOUND, and the ``split``
# --- sub-object preserved as the (possibly empty) measured view.

def _replayed_payload(
    routine_share,
    denominator=40,
    measured_routine=None,
    bound_kind="fail_closed_lower_bound",
    measured_denominator=None,
):
    """A replayed payload in the exact schema split_to_json_payload emits."""
    return {
        "schema_version": 1,
        "measurement_status": "replayed",
        "bound_kind": bound_kind,
        "routine_share": routine_share,
        "split": {
            "routine": measured_routine,
            "consequential": (
                None if measured_routine is None else 1.0 - measured_routine
            ),
            "pinned": 0.0,
            "other": 0.0,
        },
        "counts": (
            {"routine": round(denominator * routine_share)}
            if routine_share is not None and isinstance(routine_share, (int, float))
            and math.isfinite(routine_share) and routine_share >= 0.0
            else {}
        ),
        "denominator_cycles": denominator,
        "measured_denominator_cycles": (
            denominator if measured_denominator is None else measured_denominator
        ),
        "completed_missing_tier_count": 0,
        "non_completed_with_tier_count": 0,
        "rows_considered": denominator,
        "window_days": 7,
        "window_start": "2026-07-30T00:00:00+00:00",
        "window_end": "2026-08-06T00:00:00+00:00",
        "measured_at": "2026-08-06T00:00:00+00:00",
        "provenance": "test fixture",
        "signal_resolution": {"open_message_count": {"defaulted-fail-closed": denominator}},
        "replay_sources": {"stamp": 0, "replay": denominator, "unresolved": 0},
    }


def test_replayed_split_drives_headline_at_labelled_lower_bound(tmp_path, monkeypatch):
    """The REQUIRED named test: a replayed split is recognised and drives the
    headline cost/below-baseline at that share, labelled a lower bound.

    (a) the replayed routine_share feeds the headline cost and the
        below-baseline percentage (wired to the split, not a constant);
    (b) the headline carries the lower-bound label and never claims 'measured'.
    """
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    split_path.write_text(json.dumps(_replayed_payload(0.85, denominator=40)))

    claim = mod.headline_claim()
    assert claim["available"] is True
    assert claim["routine_share_status"] == "replayed"
    assert claim["bound_kind"] == "fail_closed_lower_bound"
    assert claim["routine_share"] == 0.85
    expected_cost = mod.project_cycle_cost(0.85, mod.BASELINE_CACHE_READ_SHARE, 0.0)
    assert claim["cost_per_cycle_usd"] == pytest.approx(expected_cost)
    expected_below = (
        mod.BASELINE_COST_PER_CYCLE_USD - expected_cost
    ) / mod.BASELINE_COST_PER_CYCLE_USD
    assert claim["below_baseline_pct"] == pytest.approx(expected_below)
    assert claim["claim_verified"] is True
    # (b) labelled a lower bound; the status never claims 'measured'.
    assert "fail-closed replayed lower bound" in claim["reason"]
    assert "NOT a measured realized split" in claim["reason"]
    assert claim["routine_share_status"] != "measured"


def test_replayed_lower_bound_below_threshold_reports_unverified(tmp_path, monkeypatch):
    """A replayed lower bound below ~0.60 does not clear the >=30% threshold:
    claim_verified=False with the lower-bound caveat, never an over-claim."""
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    split_path.write_text(json.dumps(_replayed_payload(0.50, denominator=40)))

    claim = mod.headline_claim()
    assert claim["available"] is True
    assert claim["routine_share"] == 0.50
    assert claim["claim_verified"] is False
    assert claim["routine_share_status"] == "replayed"
    # lower-bound caveat, not a flat falsification of a measured share.
    assert "NOT a measured realized split" in claim["reason"]
    assert "does NOT clear" in claim["reason"]
    assert "measured realized share could still clear it" in claim["reason"]


def test_unmeasured_split_still_fails_closed_exactly_as_today(tmp_path, monkeypatch):
    """No measured share and no replayed lower bound -> the original fail-closed
    behavior is unchanged: UNVERIFIED, no assumed share, unmeasured reason."""
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    split_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "measurement_status": "unmeasured",
                "routine_share": None,
                "split": {
                    "routine": None,
                    "consequential": None,
                    "pinned": None,
                    "other": None,
                },
                "counts": {},
                "denominator_cycles": 0,
                "completed_missing_tier_count": 0,
                "non_completed_with_tier_count": 0,
                "rows_considered": 0,
                "window_days": 7,
                "window_start": None,
                "window_end": None,
                "measured_at": "2026-08-06T00:00:00+00:00",
                "provenance": "test fixture",
            }
        )
    )

    claim = mod.headline_claim()
    assert claim["available"] is False
    assert claim["claim_verified"] is False
    assert claim["routine_share"] is None
    assert claim["cost_per_cycle_usd"] is None
    assert claim["below_baseline_pct"] is None
    assert claim["routine_share_status"] == "unmeasured"
    assert "unmeasured" in claim["reason"].lower()
    # The replayed lower bound is explicitly offered as the unmeasured escape.
    assert "--replay" in claim["reason"]


def test_measured_view_takes_precedence_over_replayed_lower_bound(tmp_path, monkeypatch):
    """When a replayed payload preserves a realised measured view (split.routine)
    over the WHOLE replayed basis, that measured share drives the headline —
    never the lower bound — and the reported denominator is the measured one."""
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    # replayed lower bound 0.50, but the preserved measured view says 0.85 and
    # it covers all 40 cycles the replay ran over (every cycle was stamped).
    split_path.write_text(
        json.dumps(
            _replayed_payload(
                0.50, denominator=40, measured_routine=0.85, measured_denominator=40
            )
        )
    )

    claim = mod.headline_claim()
    assert claim["available"] is True
    assert claim["routine_share"] == 0.85
    assert claim["routine_share_status"] == "measured"
    assert claim["bound_kind"] is None
    assert claim["reason"] is None
    assert claim["claim_verified"] is True
    assert claim["denominator_cycles"] == 40


def test_stamped_subset_never_preempts_the_wider_replayed_bound(tmp_path, monkeypatch):
    """A stamped SUBSET of the replayed basis must not drive the headline.

    The over-claim this guards: 3 completed cycles, 1 stamped routine. The
    stamp-only view is routine=1.0 over 1 cycle; the fail-closed replayed bound
    is 0.3333 over 3. Preferring the 1.0 flips the headline to VERIFIED on a
    single cycle while printing a 3-cycle denominator. The replay already folds
    that stamp in, so the wider bound must win — and whatever share is reported,
    the denominator must be that share's own basis.
    """
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    split_path.write_text(
        json.dumps(
            _replayed_payload(
                0.333333,
                denominator=3,
                measured_routine=1.0,
                measured_denominator=1,
            )
        )
    )

    claim = mod.headline_claim()
    assert claim["routine_share"] == pytest.approx(0.333333)
    assert claim["routine_share_status"] == "replayed"
    assert claim["bound_kind"] == "fail_closed_lower_bound"
    assert claim["denominator_cycles"] == 3
    # The single-cycle 1.0 would have cleared the threshold; the honest 3-cycle
    # lower bound does not, and is reported UNVERIFIED rather than over-claimed.
    assert claim["claim_verified"] is False

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()
    out = buf.getvalue()
    assert "Measured routine share R = 1.0000" not in out
    assert "(3 completed cycles)" in out
    # No line pairs the 1.0 numerator with the 3-cycle denominator.
    assert "1.0000 (realised split, 3 completed tiered cycles)" not in out


def test_measured_denominator_is_reported_for_a_stamp_only_split(tmp_path, monkeypatch):
    """A plain measured split reports its own denominator on the claim."""
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    split_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "measurement_status": "measured",
                "routine_share": 0.9,
                "split": {"routine": 0.9, "consequential": 0.1},
                "counts": {"routine": 18, "consequential": 2},
                "denominator_cycles": 20,
                "measured_denominator_cycles": 20,
            }
        )
    )

    claim = mod.headline_claim()
    assert claim["routine_share_status"] == "measured"
    assert claim["denominator_cycles"] == 20
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()
    assert "0.9000 (realised split, 20 completed tiered cycles)" in buf.getvalue()


def test_cli_sections_name_the_replayed_lower_bound(tmp_path, monkeypatch):
    """The [0] and [6] CLI sections print which status drove the headline and
    the lower-bound caveat for a replayed split."""
    mod = _load_module()
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    split_path.write_text(json.dumps(_replayed_payload(0.50, denominator=40)))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()
    out = buf.getvalue()
    assert "fail-closed LOWER BOUND" in out
    assert "NOT a measured realized split" in out
    assert "Replayed R" in out
    assert "does NOT clear the >=30%-below-baseline threshold" in out


def test_routine_share_from_split_rejects_invalid_replayed_bounds():
    """A replayed share that is NaN, negative, non-finite, over zero denominator,
    or missing the fail-closed bound_kind is not a valid lower bound:
    routine_share_from_split fails closed to None."""
    mod = _load_module()
    for share in (float("nan"), float("inf"), -0.5):
        payload = _replayed_payload(share, denominator=40)
        assert mod.routine_share_from_split(payload) is None, share
    # zero denominator -> not a bound (no completed cycles).
    payload = _replayed_payload(0.0, denominator=0)
    assert mod.routine_share_from_split(payload) is None
    # missing the fail-closed bound_kind contract.
    payload = _replayed_payload(0.85, denominator=40, bound_kind="something_else")
    assert mod.routine_share_from_split(payload) is None


# --- Monthly spend (criterion c-3fd62b9d) --------------------------------

def test_project_monthly_spend_is_per_cycle_times_volume():
    """monthly = per-cycle cost x cycles/month — the arithmetic is explicit."""
    mod = _load_module()
    m = mod.project_monthly_spend(2.8665, 256.57)
    assert m["available"] is True
    assert m["monthly_spend_usd"] == round(2.8665 * 256.57, 2)
    assert m["cycles_per_month"] == round(256.57, 2)


def test_project_monthly_spend_refuses_to_guess_without_a_volume():
    """None cycle volume -> available=False, no fabricated number."""
    mod = _load_module()
    m = mod.project_monthly_spend(2.87, None)
    assert m["available"] is False
    assert m["monthly_spend_usd"] is None
    assert m["reason"]


def test_project_monthly_spend_rejects_negative_or_non_finite_volume():
    """Negative/NaN/inf volume is the same unavailable-input case."""
    mod = _load_module()
    for bad in (-1.0, float("nan"), float("inf")):
        m = mod.project_monthly_spend(2.87, bad)
        assert m["available"] is False, bad
        assert m["monthly_spend_usd"] is None


def test_measured_cycle_volume_matches_the_documented_basis():
    """59 cycles / 7-day window scaled to a month ~ 256.6 cycles/month."""
    mod = _load_module()
    expected = 59 * mod.DAYS_PER_MONTH / 7.0
    assert mod.measured_cycles_per_month() == expected
    assert 250 < expected < 265


def test_cadence_floor_cycle_volume_from_config_default():
    """strategic_max_interval_minutes=1440 (daily backstop) -> ~30.4/month."""
    mod = _load_module()
    expected = mod.MINUTES_PER_MONTH / mod.STRATEGIC_MAX_INTERVAL_MINUTES
    assert mod.cadence_cycles_per_month() == expected
    assert 29 < expected < 32


def test_target_concurrency_input_is_surfaced_as_unavailable():
    """No stored 'target_concurrency' config exists; the projection says so."""
    mod = _load_module()
    status = mod.target_concurrency_status()
    assert status["available"] is False
    assert "target_concurrency" in status["reason"]


def test_honest_monthly_spend_at_measured_volume_is_within_the_envelope():
    """Gradeable c-3fd62b9d number: honest base case at measured volume is
    comfortably under the $3,000/month envelope."""
    mod = _load_module()
    cost = mod.project_cycle_cost(0.70, mod.BASELINE_CACHE_READ_SHARE, 0.0)
    monthly = mod.project_monthly_spend(cost, mod.measured_cycles_per_month())
    assert monthly["available"] is True
    assert monthly["monthly_spend_usd"] < mod.MONTHLY_ENVELOPE_USD
    # Reproducible arithmetic: total == per-cycle x volume.
    assert monthly["monthly_spend_usd"] == round(cost * mod.measured_cycles_per_month(), 2)


def test_main_output_includes_the_monthly_arithmetic():
    """The CLI surfaces the monthly computation and the unavailable-input case
    (the artifact a reader checks c-3fd62b9d against)."""
    mod = _load_module()
    base_cost = mod.project_cycle_cost(0.70, mod.BASELINE_CACHE_READ_SHARE, 0.0)
    monthly = mod.project_monthly_spend(base_cost, mod.measured_cycles_per_month())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()
    out = buf.getvalue()
    assert "[7] Projected monthly spend" in out
    assert "Target-concurrency input: NOT AVAILABLE" in out
    # The CLI prints the monthly figure comma-grouped (e.g. "1,253.31"), which
    # matches the formatted projection value.
    assert f"{monthly['monthly_spend_usd']:,.2f}" in out


def test_mean_task_execution_cost_matches_documented_basis():
    """Per-task execution cost = (fleet LLM + VM) / tasks = $0.3128/task.

    The direction's measured basis (d-e860fe17): the non-strategic fleet that
    merged 75 PRs over the 7-day window cost $15.46 LLM + $8.00 VM — the same
    VM/LLM accounting that backs the existing per-task cost reporting.
    """
    mod = _load_module()
    expected = (mod.MEASURED_FLEET_LLM_USD_PER_7_DAYS + mod.MEASURED_FLEET_VM_USD_PER_7_DAYS) / mod.MEASURED_TASKS_PER_7_DAYS
    assert mod.mean_task_execution_cost_usd() == expected
    assert 0.25 < expected < 0.40


def test_measured_task_volume_matches_documented_basis():
    """75 tasks / 7-day window scaled to a month ~ 326.1 tasks/month."""
    mod = _load_module()
    expected = mod.MEASURED_TASKS_PER_7_DAYS * mod.DAYS_PER_MONTH / 7.0
    assert mod.measured_tasks_per_month() == expected
    assert 320 < expected < 335


def test_monthly_spend_includes_per_task_term():
    """The monthly TOTAL is decision-loop cost + task-execution cost.

    (a) For non-zero task volume the total strictly exceeds the decision-loop-
    only figure; (b) missing per-task inputs surface as an explicit gap, never
    a silent zero that understates the monthly bill.
    """
    mod = _load_module()
    decision_only = mod.project_monthly_spend(2.8665, 256.57)
    assert decision_only["available"] is True

    total = mod.project_monthly_spend_with_tasks(2.8665, 256.57, 0.31, 100.0)
    assert total["available"] is True
    # (a) the total strictly exceeds the decision-loop-only figure.
    assert total["total_monthly_spend_usd"] > decision_only["monthly_spend_usd"]
    # Both terms reported separately so the split is auditable.
    assert total["decision_loop_monthly_usd"] == decision_only["monthly_spend_usd"]
    assert total["task_execution_monthly_usd"] == round(0.31 * 100.0, 2)
    assert total["total_monthly_spend_usd"] == round(
        total["decision_loop_monthly_usd"] + total["task_execution_monthly_usd"], 2
    )
    # (b) missing per-task inputs surface as an explicit gap, not a silent zero.
    for kwargs in (
        {"per_task_cost_usd": None, "tasks_per_month": 100.0},
        {"per_task_cost_usd": 0.31, "tasks_per_month": None},
        {"per_task_cost_usd": 0.31, "tasks_per_month": -1.0},
        {"per_task_cost_usd": float("nan"), "tasks_per_month": 100.0},
    ):
        gap = mod.project_monthly_spend_with_tasks(2.8665, 256.57, **kwargs)
        assert gap["available"] is False, kwargs
        assert gap["total_monthly_spend_usd"] is None, kwargs
        assert "task" in gap["reason"].lower(), kwargs


def test_monthly_spend_with_tasks_preserves_cycle_volume_refusal():
    """The total projection keeps the decision-loop no-fabrication discipline:
    an unavailable cycle volume is the same unavailable-input gap."""
    mod = _load_module()
    gap = mod.project_monthly_spend_with_tasks(2.8665, None, 0.31, 100.0)
    assert gap["available"] is False
    assert "cycle volume" in gap["reason"]
    assert gap["total_monthly_spend_usd"] is None


# --- Baseline measurement record (fc135ee2) ------------------------------

def test_baseline_record_carries_cohort_provenance_for_both_readings():
    """All three baseline readings surface verbatim with cohort provenance:
    a 5000-task cohort at _COHORT_CAP, capped=True.

    docs/strategic-cycle-cost-baseline.md is the narrative form of this exact
    record; the field mapping against the agent efficiency-snapshot read
    surface is documented there.
    """
    mod = _load_module()
    record = mod.baseline_record()
    assert record["available"] is True
    assert record["cohort_cap"] == mod.COHORT_CAP == 5000
    assert record["requested_window_days"] == mod.AXIS_WINDOW_DAYS == 7
    assert record["cost_arithmetic_consistent"] is True
    assert len(record["readings"]) == 3

    reading_1, reading_2, reading_3 = record["readings"]
    assert reading_1["reading"] == 1
    assert reading_1["date"] == "2026-08-07"
    assert reading_1["integration_head"] == "9e25341c"
    assert reading_1["cycle_count"] == 212
    assert reading_1["strategic_cycle_cost_usd"] == pytest.approx(1610.081459)
    assert reading_1["cost_per_cycle"] == pytest.approx(7.594724)
    assert reading_1["total_llm_usd"] == pytest.approx(3439.92)
    assert reading_1["unpriced_coverage"] == pytest.approx(0.9752)
    assert reading_1["cohort_task_count"] == 5000
    assert reading_1["cohort_capped"] is True
    assert reading_1["requested_window_days"] == 7
    assert reading_1["cohort_cap"] == 5000
    assert reading_1["cost_per_cycle_consistent"] is True
    # The reading records the window that gives it provenance rather than
    # disclosing it as unknown. Both 2026-08-07 readings predate the agent
    # endpoint exposing the effective window, so the recorded values are the
    # requested query-window bounds (7 days ending on the reading date).
    assert reading_1["cohort_effective_window_start"] == "2026-07-31T00:00:00+00:00"
    assert reading_1["cohort_effective_window_end"] == "2026-08-07T00:00:00+00:00"

    assert reading_2["reading"] == 2
    assert reading_2["date"] == "2026-08-07"
    assert reading_2["integration_head"] == "83813e87"
    assert reading_2["cycle_count"] == 212
    assert reading_2["strategic_cycle_cost_usd"] == pytest.approx(1618.175162)
    assert reading_2["cost_per_cycle"] == pytest.approx(7.632902)
    assert reading_2["total_llm_usd"] == pytest.approx(3446.586451)
    assert reading_2["unpriced_coverage"] == pytest.approx(0.9752)
    assert reading_2["cohort_task_count"] == 5000
    assert reading_2["cohort_capped"] is True
    assert reading_2["requested_window_days"] == 7
    assert reading_2["cohort_cap"] == 5000
    assert reading_2["cost_per_cycle_consistent"] is True
    assert reading_2["cohort_effective_window_start"] == "2026-07-31T00:00:00+00:00"
    assert reading_2["cohort_effective_window_end"] == "2026-08-07T00:00:00+00:00"

    # Reading 3: first post-promotion reading (2026-08-08).
    assert reading_3["reading"] == 3
    assert reading_3["date"] == "2026-08-08"
    assert reading_3["integration_head"] == "5b97819"
    assert reading_3["cycle_count"] == 230
    assert reading_3["strategic_cycle_cost_usd"] == pytest.approx(1683.2)
    assert reading_3["cost_per_cycle"] == pytest.approx(7.318261)
    assert reading_3["total_llm_usd"] == pytest.approx(3390.42)
    # unpriced_coverage dropped to 0.72 from 0.9752: 72% of spend still predates
    # v29 serving; attribution will improve as the cohort ages out pre-promotion tasks.
    assert reading_3["unpriced_coverage"] == pytest.approx(0.72)
    assert reading_3["cohort_task_count"] == 5000
    assert reading_3["cohort_capped"] is True
    assert reading_3["requested_window_days"] == 7
    assert reading_3["cohort_cap"] == 5000
    assert reading_3["cost_per_cycle_consistent"] is True
    # Reading 3 postdates the endpoint change: effective window is the true span.
    assert reading_3["cohort_effective_window_start"] == "2026-08-01T00:00:00+00:00"
    assert reading_3["cohort_effective_window_end"] == "2026-08-08T00:00:00+00:00"


def test_baseline_record_includes_post_promotion_reading():
    """Reading 3 (2026-08-08, integration HEAD 5b97819) is the first
    post-promotion reading: unpriced_coverage 0.72 vs 0.9752 on the earlier
    readings, and effective window captured from the true endpoint span.

    The named acceptance test: verifies that baseline_record() carries the
    post-promotion reading with consistent cost arithmetic and correct
    cohort-capped provenance.
    """
    mod = _load_module()
    record = mod.baseline_record()
    assert len(record["readings"]) >= 3, "Reading 3 (post-promotion) must be present"

    reading_3 = record["readings"][2]
    assert reading_3["reading"] == 3
    assert reading_3["date"] == "2026-08-08"
    assert reading_3["integration_head"] == "5b97819"
    assert reading_3["cycle_count"] == 230

    # Cost arithmetic must be self-consistent (total / count within tolerance).
    assert reading_3["cost_per_cycle_consistent"] is True
    assert reading_3["cost_per_cycle"] == pytest.approx(
        reading_3["strategic_cycle_cost_usd"] / reading_3["cycle_count"], abs=0.0005
    )

    # unpriced_coverage dropped vs the two 2026-08-07 readings — reflects v29
    # promotion: 72% of spend still predates serving, not 97.52%.
    assert reading_3["unpriced_coverage"] == pytest.approx(0.72)
    earlier_coverages = [r["unpriced_coverage"] for r in record["readings"][:2]]
    assert all(c > reading_3["unpriced_coverage"] for c in earlier_coverages), (
        "Reading 3 unpriced_coverage should be lower than pre-promotion readings"
    )

    # Cohort provenance: still capped (5000 rows).
    assert reading_3["cohort_capped"] is True
    assert reading_3["cohort_task_count"] == 5000

    # Effective window is the true span from the endpoint (not the query-window bounds).
    assert reading_3["cohort_effective_window_start"] == "2026-08-01T00:00:00+00:00"
    assert reading_3["cohort_effective_window_end"] == "2026-08-08T00:00:00+00:00"


def test_cohort_capped_flag_matches_the_task_count_cap():
    """cohort_capped is True exactly when the cohort reached the 5000-row cap.

    Mirrors the agent read surface (`cohort_task_count >= _COHORT_CAP`): a
    4999-task cohort is provably uncapped, 5000 is capped, and beyond the cap
    stays capped.
    """
    mod = _load_module()
    assert mod.cohort_capped(4999) is False
    assert mod.cohort_capped(4999.99) is False
    assert mod.cohort_capped(5000) is True
    assert mod.cohort_capped(5000.0) is True
    assert mod.cohort_capped(5001) is True


def test_cohort_capped_fails_closed_on_unknown_count():
    """A missing or non-numeric cohort count reports None, never False.

    None means "unknown provenance", not "uncapped" — a caller must not
    mistake an uncaptured count for a cohort that provably stayed under the
    cap.
    """
    mod = _load_module()
    for unknown in (None, "5000", True, False, ""):
        assert mod.cohort_capped(unknown) is None, repr(unknown)


def test_baseline_record_derives_cost_per_cycle_and_flags_disagreement():
    """cost_per_cycle is derived when omitted and validated when recorded.

    A recorded value that disagrees with strategic_cycle_cost_usd/cycle_count
    beyond tolerance sets cost_per_cycle_consistent=False instead of silently
    printing a number that does not add up.
    """
    mod = _load_module()
    derived = mod.baseline_record(
        [
            {
                "reading": 1,
                "date": "2026-08-07",
                "integration_head": "abc123",
                "cycle_count": 212,
                "strategic_cycle_cost_usd": 1610.081459,
                "total_llm_usd": 3439.92,
                "unpriced_coverage": 0.9752,
                "cohort_task_count": 5000,
            }
        ]
    )
    reading = derived["readings"][0]
    assert reading["cost_per_cycle"] == pytest.approx(1610.081459 / 212)
    assert reading["cost_per_cycle_consistent"] is True
    assert derived["cost_arithmetic_consistent"] is True

    inconsistent = mod.baseline_record(
        [
            {
                "reading": 1,
                "date": "2026-08-07",
                "integration_head": "abc123",
                "cycle_count": 212,
                "strategic_cycle_cost_usd": 1610.081459,
                "cost_per_cycle": 9.99,
                "cohort_task_count": 5000,
            }
        ]
    )
    assert inconsistent["readings"][0]["cost_per_cycle_consistent"] is False
    assert inconsistent["cost_arithmetic_consistent"] is False


def test_baseline_record_fails_closed_on_missing_cohort_count():
    """A reading that lacks a numeric cohort_task_count keeps cohort_capped
    None — provenance unknown is reported as unknown, never as uncapped."""
    mod = _load_module()
    record = mod.baseline_record(
        [
            {
                "reading": 1,
                "date": "2026-08-07",
                "integration_head": "abc123",
                "cycle_count": 212,
                "strategic_cycle_cost_usd": 1610.081459,
                "cost_per_cycle": 7.594724,
                "cohort_task_count": None,
            }
        ]
    )
    reading = record["readings"][0]
    assert reading["cohort_capped"] is None
    assert reading["requested_window_days"] == 7
    assert reading["cohort_cap"] == 5000
    assert reading["cost_per_cycle_consistent"] is True


def test_main_output_includes_the_baseline_record():
    """Section [8] prints all three dated readings and the provenance caveat."""
    mod = _load_module()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()
    out = buf.getvalue()
    assert "[8] Baseline measurement record" in out
    assert "integration HEAD 9e25341c" in out
    assert "integration HEAD 83813e87" in out
    assert "integration HEAD 5b97819" in out
    assert "not evidence about the levers" in out


# --- Baseline-anchored projection (the $4.41 constant fix) ---------------

def test_projection_anchor_sourced_from_baseline_record_and_labels_statistic(
    tmp_path, monkeypatch
):
    """The projection's per-cycle baseline anchor is DERIVED from the recorded
    baseline record (mean of the recorded cost_per_cycle values), not a
    task-provided constant — and the mean-vs-median statistic is labelled.

    This is the acceptance test for "anchor the cost projection on the recorded
    baseline, not a constant":

    (a) BASELINE_COST_PER_CYCLE_USD equals the mean of the validated baseline
        record's cost_per_cycle values and is strictly above the old $4.41
        constant;
    (b) the projection uses that derived anchor — a cycle with routine share 0
        and the baseline cache share projects at exactly the derived baseline;
    (c) baseline_per_cycle_stats() labels the statistic: mean AND median both
        reported, with a note naming the MEAN-vs-MEDIAN gap (per_event_usd is
        a mean while the projection claims a median);
    (d) headline_claim() carries the baseline keys (baseline_mean_usd,
        baseline_median_usd, mean_median_note).
    """
    mod = _load_module()
    record = mod.baseline_record()
    recorded = [r["cost_per_cycle"] for r in record["readings"]]
    expected_mean = sum(recorded) / len(recorded)

    # (a) anchor sourced from the record, not a 4.41 constant.
    assert pytest.approx(expected_mean) == mod.BASELINE_COST_PER_CYCLE_USD
    assert mod.BASELINE_COST_PER_CYCLE_USD > 4.41

    # (b) the projection uses the derived anchor.
    assert mod.project_cycle_cost(
        0.0, mod.BASELINE_CACHE_READ_SHARE, 0.0
    ) == pytest.approx(mod.BASELINE_COST_PER_CYCLE_USD)

    # (c) statistic labelled.
    stats = mod.baseline_per_cycle_stats()
    assert stats["available"] is True
    assert stats["mean_usd"] == pytest.approx(expected_mean)
    assert stats["median_usd"] == pytest.approx(statistics.median(recorded))
    assert stats["reading_count"] == len(recorded)
    note = stats["statistic_note"].lower()
    assert "mean" in note and "median" in note
    assert "per_event_usd" in stats["statistic_note"]

    # (d) headline carries the baseline keys (a measured split drives it).
    split_path = tmp_path / "measured_effort_tier_split.json"
    monkeypatch.setattr(mod, "MEASURED_SPLIT_PATH", split_path)
    split_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "measurement_status": "measured",
                "routine_share": 0.85,
                "split": {"routine": 0.85, "consequential": 0.15, "pinned": 0.0, "other": 0.0},
                "counts": {"routine": 17, "consequential": 3},
                "denominator_cycles": 20,
                "measured_denominator_cycles": 20,
            }
        )
    )
    claim = mod.headline_claim()
    assert claim["available"] is True
    assert claim["baseline_mean_usd"] == pytest.approx(expected_mean)
    assert claim["baseline_median_usd"] == pytest.approx(stats["median_usd"])
    assert "median" in claim["mean_median_note"].lower()


def test_baseline_per_cycle_stats_reconciles_mean_and_median():
    """Mean/median of the three recorded readings, plus the reading count.

    This sample is slightly left-skewed: the recorded median (7.5947) is above
    the mean (7.5153), so the note must call out that the mean anchor is NOT
    conservative relative to the recorded median — never silently conflated.
    """
    mod = _load_module()
    stats = mod.baseline_per_cycle_stats()
    recorded = [r["cost_per_cycle"] for r in mod.baseline_record()["readings"]]
    assert stats["available"] is True
    assert stats["reading_count"] == 3 == len(recorded)
    assert stats["mean_usd"] == pytest.approx(sum(recorded) / 3)
    assert stats["median_usd"] == pytest.approx(statistics.median(recorded))
    assert stats["median_usd"] > stats["mean_usd"]
    assert "per_event_usd = total_usd / cycle_count" in stats["statistic_note"]
    assert "NOT conservative" in stats["statistic_note"]


def test_baseline_per_cycle_stats_empty_readings_fails_closed():
    """No readings -> available=False, no fabricated statistics."""
    mod = _load_module()
    stats = mod.baseline_per_cycle_stats([])
    assert stats["available"] is False
    assert stats["mean_usd"] is None
    assert stats["median_usd"] is None
    assert stats["reading_count"] == 0
    assert stats["statistic_note"]


def test_serving_sha_reading_does_not_move_the_baseline_anchor():
    mod = _load_module()
    anchor = mod.BASELINE_COST_PER_CYCLE_USD
    serving_reading = {
        "serving_sha": "f" * 40,
        "cycle_count": 30,
        "total_usd": 180.0,
        "per_event_usd": 6.0,
        "unpriced_coverage": 0.0,
    }
    record = mod.serving_sha_record([serving_reading])
    comparison = mod.serving_sha_comparison(serving_reading)

    assert record["readings"][0]["cost_arithmetic_consistent"] is True
    assert comparison["available"] is True
    assert comparison["serving_cost_per_cycle_usd"] == pytest.approx(6.0)
    assert comparison["baseline_cost_per_cycle_usd"] == pytest.approx(anchor)
    assert comparison["below_baseline_pct"] == pytest.approx((anchor - 6.0) / anchor)


def test_serving_sha_record_derives_and_validates_per_event_arithmetic():
    mod = _load_module()
    derived = mod.serving_sha_record(
        [{"serving_sha": "a" * 40, "cycle_count": 30, "total_usd": 75.0}]
    )
    assert derived["available"] is True
    assert derived["readings"][0]["per_event_usd"] == pytest.approx(2.5)

    inconsistent = mod.serving_sha_record(
        [{
            "serving_sha": "b" * 40,
            "cycle_count": 4,
            "total_usd": 10.0,
            "per_event_usd": 99.0,
        }]
    )
    assert inconsistent["available"] is False
    assert inconsistent["readings"][0]["reading_valid"] is False

def test_thin_cohort_yields_insufficient_verdict_with_required_n_and_projected_date():
    """A valid-looking thin slice is not directional cost evidence."""
    mod = _load_module()
    result = mod.serving_sha_comparison(
        {
            "serving_sha": "c" * 40,
            "cycle_count": 10,
            "total_usd": 60.0,
            "per_event_usd": 6.0,
            "first_cycle_at": "2026-08-01T00:00:00+00:00",
            "last_cycle_at": "2026-08-02T00:00:00+00:00",
        }
    )

    assert result["available"] is False
    assert result["verdict"] == "insufficient_sample"
    assert result["n"] == 10
    assert result["required_n"] == mod.MINIMUM_COST_VERDICT_N == 30
    assert result["projected_date"] == "2026-08-04"
    assert result["serving_cost_per_cycle_usd"] is None
    assert result["dispersion"]["stddev_usd"] is None


def test_serving_sha_record_reports_validated_per_slice_dispersion():
    mod = _load_module()
    record = mod.serving_sha_record(
        [
            {"serving_sha": "a" * 40, "cycle_count": 15, "total_usd": 90.0,
             "per_event_usd": 6.0},
            {"serving_sha": "b" * 40, "cycle_count": 15, "total_usd": 120.0,
             "per_event_usd": 8.0},
        ]
    )

    assert record["available"] is True
    dispersion = record["dispersion"]
    assert dispersion["mean_usd"] == pytest.approx(7.0)
    assert dispersion["stddev_usd"] == pytest.approx(math.sqrt(2.0))

    single = mod.serving_sha_record(
        [{"serving_sha": "a" * 40, "cycle_count": 30, "total_usd": 180.0}]
    )
    assert single["qualification"]["available"] is True
    assert single["dispersion"]["stddev_usd"] is None


def test_serving_sha_record_rejects_non_sha_identity():
    """A non-blank string that is not a git SHA is not a serving identity.

    ``DeploymentState.current_sha`` initializes to the literal ``"unknown"``,
    so a bare ``isinstance(str) and non-blank`` check would qualify a reading
    whose cohort is attributed to a revision that does not exist.
    """
    mod = _load_module()
    for bogus in ("unknown", "", "   ", "not-a-sha", "z" * 40, "f" * 64, 123, None):
        record = mod.serving_sha_record(
            [
                {
                    "serving_sha": bogus,
                    "cycle_count": 30,
                    "total_usd": 75.0,
                    "per_event_usd": 2.5,
                }
            ]
        )
        assert record["readings"][0]["serving_sha_valid"] is False, bogus
        assert record["readings"][0]["reading_valid"] is False, bogus
        assert record["available"] is False, bogus

    # A real 40-hex SHA (and the 7-hex short form) still qualifies.
    for good in ("a" * 40, "abc1234"):
        record = mod.serving_sha_record(
            [
                {
                    "serving_sha": good,
                    "cycle_count": 30,
                    "total_usd": 75.0,
                    "per_event_usd": 2.5,
                }
            ]
        )
        assert record["readings"][0]["serving_sha_valid"] is True, good


def test_serving_sha_comparison_fails_closed_without_attribution_identity():
    mod = _load_module()
    result = mod.serving_sha_comparison(
        {"serving_sha": None, "cycle_count": 2, "total_usd": 1.0, "per_event_usd": 0.5}
    )
    assert result["available"] is False
    assert result["serving_cost_per_cycle_usd"] is None
    assert result["below_baseline_pct"] is None


def test_main_output_uses_the_derived_baseline():
    """The CLI anchors on the derived baseline and no longer cites $4.41.

    The mean/median reconciliation is printed in section [0]; section [1] names
    the baseline as the mean of the recorded readings; the old $4.41 constant
    is gone from the printed output entirely.
    """
    mod = _load_module()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main()
    out = buf.getvalue()
    assert "$4.41" not in out
    assert f"${mod.BASELINE_COST_PER_CYCLE_USD:.2f}" in out
    assert "recorded baseline readings" in out
    assert "[9] Serving-SHA readings" in out
    assert "median" in out and "MEAN" in out
    assert f"{mod.BASELINE_COST_PER_CYCLE_USD:.4f}" in out
