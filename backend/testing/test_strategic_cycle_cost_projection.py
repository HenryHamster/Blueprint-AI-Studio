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

A number tuned backwards from $1.50 would fail these tests.
"""
import importlib.util
from itertools import pairwise
from pathlib import Path

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
