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
"""
import contextlib
import importlib.util
import io
import json
import math
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
    """At the direction's own target inputs (share 0.80, R=0.85) cost ~$1.52.

    The doc reports $1.52; the target is on the edge of the optimistically
    parameterized range, not comfortably under it.
    """
    mod = _load_module()
    target_case = mod.project_cycle_cost(0.85, 0.80, 0.0)
    assert 1.40 < target_case < 1.60, f"unexpected boundary value: {target_case:.2f}"


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
    assert str(monthly["monthly_spend_usd"]) in out


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
