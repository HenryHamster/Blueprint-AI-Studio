"""Pure-function tests for the goal-verification redesign in ``release_scoring``.

These cover the redesigned per-category goal gate that REPLACED the criterion
"coverage"-count blocker and the ``uncorroborated_attestation`` blocker:

  * ``compute_composite`` blocks when ANY active category is not ``met`` (a
    ``partial`` gates identically to ``not_met``) OR when the residual
    ``uncaptured_gap`` is non-empty — reusing the ``criterion_coverage_incomplete``
    blocker STRING so downstream re-drive/trigger plumbing keeps working.
  * The judged-vs-machine tier is folded into a proportional CONFIDENCE penalty
    (not a hard block).
  * ``evaluate_merge_grade`` normalises a merge-grade artifact's per-category
    grades against the ACTIVE rubric: an ungraded category, or a ``met`` with no
    citation, is forced to ``not_met`` (v1 "uncited it's-fine" rule).

Everything here is pure (no DB / no I/O), so it runs under the standalone
harness without Postgres.
"""

from __future__ import annotations

from app import release_scoring as rs

# Fixed policy mirroring promotion_policy.yaml; threshold pinned low so the
# judged-tier penalty (a few %) never crosses the promote boundary by accident.
_POLICY = {
    "weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
    "threshold": 0.75,
}


def _cat(key: str, status: str, *, tier: str = "judged", cited: bool = True) -> dict:
    """Build a per-category verdict dict as compute_composite consumes it."""
    return {
        "category_key": key,
        "status": status,
        "tier": tier,
        "gap": None if status == "met" else f"{key} not delivered",
        "citations": ([{"kind": "diff", "ref": f"{key}.py:1"}] if cited else []),
    }


def _perfect_kwargs(**overrides):
    base = {
        "ci_green": True,
        "audit_score": 1.0,
        "test_erosion": 1.0,
        "smoketest_score": 1.0,
        "policy": _POLICY,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# compute_composite — the redesigned goal-verification gate
# ---------------------------------------------------------------------------


class TestCategoryGateHardBlock:
    def test_block_on_not_met_category(self):
        """Any active category not ``met`` forces confidence 0 and the
        preserved ``criterion_coverage_incomplete`` blocker string."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[_cat("c-1", "met"), _cat("c-2", "not_met")],
            )
        )
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        assert result["blocker"] == "criterion_coverage_incomplete"

    def test_partial_gates_identically_to_not_met(self):
        """A ``partial`` category blocks exactly like ``not_met``."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[_cat("c-1", "met"), _cat("c-2", "partial")],
            )
        )
        assert result["confidence"] == 0.0
        assert result["blocker"] == "criterion_coverage_incomplete"

    def test_block_on_residual_even_when_all_met(self):
        """An all-``met`` rubric still blocks when the grader named a goal
        aspect no category captures (residual gap)."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[_cat("c-1", "met"), _cat("c-2", "met")],
                residual_uncaptured_gap="The goal's rollback path is untested by any criterion.",
            )
        )
        assert result["confidence"] == 0.0
        assert result["blocker"] == "criterion_coverage_incomplete"
        assert (
            result["sub_scores"]["residual_uncaptured_gap"]
            == "The goal's rollback path is untested by any criterion."
        )

    def test_none_verdicts_skips_gate(self):
        """No rubric (``category_verdicts=None``) skips the gate entirely —
        the pre-redesign no-criteria path."""
        result = rs.compute_composite(
            **_perfect_kwargs(category_verdicts=None, residual_uncaptured_gap=None)
        )
        assert result["blocker"] is None
        assert result["promote"] is True

    def test_empty_verdicts_list_does_not_block(self):
        """An empty (but non-None) verdict list has no unmet category."""
        result = rs.compute_composite(**_perfect_kwargs(category_verdicts=[]))
        assert result["blocker"] is None

    def test_unmet_categories_recorded_in_sub_scores(self):
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[
                    _cat("c-1", "met"),
                    _cat("c-2", "not_met"),
                    _cat("c-3", "partial"),
                ],
            )
        )
        ss = result["sub_scores"]
        assert ss["category_count"] == 3
        assert ss["met_category_count"] == 1
        assert set(ss["unmet_categories"]) == {"c-2", "c-3"}


class TestCategoryGateOrdering:
    def test_ci_red_precedes_category_gate(self):
        """A confirmed red build outranks the category gate."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                ci_green=False,
                category_verdicts=[_cat("c-1", "not_met")],
            )
        )
        assert result["blocker"] == "ci_not_green"

    def test_merge_grade_misses_precedes_category_gate(self):
        result = rs.compute_composite(
            **_perfect_kwargs(
                merge_grade_misses=True,
                category_verdicts=[_cat("c-1", "not_met")],
            )
        )
        assert result["blocker"] == "merge_grade_misses"

    def test_category_gate_precedes_capability_proof(self):
        """An unmet category is reported before the capability-proof gate."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[_cat("c-1", "not_met")],
                capability_proof_blocked=True,
            )
        )
        assert result["blocker"] == "criterion_coverage_incomplete"


class TestJudgedTierPenalty:
    def test_all_met_judged_incurs_penalty(self):
        """All categories met at the JUDGED tier: promotes, but confidence is
        docked by the judged-tier penalty vs the no-rubric baseline."""
        baseline = rs.compute_composite(**_perfect_kwargs(category_verdicts=None))
        judged = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[_cat("c-1", "met"), _cat("c-2", "met")],
            )
        )
        assert baseline["confidence"] == 1.0
        # 1.0 * (1 - 0.05 * (2 judged / 2 met)) == 0.95
        assert judged["confidence"] == round(
            1.0 * (1.0 - rs.JUDGED_TIER_PENALTY_WEIGHT), 4
        )
        assert judged["confidence"] < baseline["confidence"]
        assert judged["promote"] is True
        assert judged["blocker"] is None

    def test_machine_tier_met_incurs_no_penalty(self):
        """Categories met at the MACHINE tier (v2) carry no judged penalty."""
        machine = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[
                    _cat("c-1", "met", tier="machine"),
                    _cat("c-2", "met", tier="machine"),
                ],
            )
        )
        assert machine["confidence"] == 1.0
        assert machine["sub_scores"]["judged_tier_met_fraction"] == 0.0

    def test_penalty_proportional_to_judged_fraction(self):
        """Half judged / half machine → half the full judged penalty."""
        mixed = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=[
                    _cat("c-1", "met", tier="judged"),
                    _cat("c-2", "met", tier="machine"),
                ],
            )
        )
        assert mixed["sub_scores"]["judged_tier_met_fraction"] == 0.5
        assert mixed["confidence"] == round(
            1.0 * (1.0 - rs.JUDGED_TIER_PENALTY_WEIGHT * 0.5), 4
        )


# ---------------------------------------------------------------------------
# evaluate_merge_grade — per-category normalisation against the active rubric
# ---------------------------------------------------------------------------


class TestEvaluateMergeGradeCategories:
    _CATS: list[dict] = [  # noqa: RUF012 — read-only fixture data
        {"category_key": "c-1", "text": "adds the widget endpoint"},
        {"category_key": "c-2", "text": "wires the widget into the UI"},
    ]

    def test_no_active_categories_holistic_only(self):
        """No rubric supplied → category_verdicts is None (holistic-only)."""
        art = {"covers_goal": True, "coverage_score": 0.9, "biggest_gap": ""}
        out = rs.evaluate_merge_grade("ship the widget", art, None)
        assert out["status"] == "pass"
        assert out["category_verdicts"] is None
        assert out["residual_uncaptured_gap"] is None

    def test_uncited_met_downgraded_to_not_met(self):
        """A ``met`` verdict with no citations is not evidence → not_met."""
        art = {
            "covers_goal": True,
            "coverage_score": 0.9,
            "biggest_gap": "",
            "category_verdicts": [
                {"category_key": "c-1", "status": "met", "citations": []},
                {
                    "category_key": "c-2",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:10"}],
                },
            ],
        }
        out = rs.evaluate_merge_grade("ship the widget", art, self._CATS)
        by_key = {v["category_key"]: v for v in out["category_verdicts"]}
        assert by_key["c-1"]["status"] == "not_met"  # uncited
        assert by_key["c-1"]["gap"]  # a gap is synthesised
        assert by_key["c-2"]["status"] == "met"  # cited
        assert all(v["tier"] == "judged" for v in out["category_verdicts"])

    def test_ungraded_active_category_is_not_met(self):
        """An active category the artifact never graded → not_met."""
        art = {
            "covers_goal": False,
            "coverage_score": 0.5,
            "biggest_gap": "ui not wired",
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "api.py:3"}],
                },
                # c-2 omitted entirely
            ],
        }
        out = rs.evaluate_merge_grade("ship the widget", art, self._CATS)
        by_key = {v["category_key"]: v for v in out["category_verdicts"]}
        assert by_key["c-1"]["status"] == "met"
        assert by_key["c-2"]["status"] == "not_met"
        assert by_key["c-2"]["gap"]

    def test_partial_status_preserved(self):
        art = {
            "covers_goal": False,
            "coverage_score": 0.5,
            "biggest_gap": "partly done",
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "partial",
                    "gap": "only half wired",
                    "citations": [{"kind": "diff", "ref": "api.py:3"}],
                },
                {
                    "category_key": "c-2",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:9"}],
                },
            ],
        }
        out = rs.evaluate_merge_grade("ship the widget", art, self._CATS)
        by_key = {v["category_key"]: v for v in out["category_verdicts"]}
        assert by_key["c-1"]["status"] == "partial"
        assert by_key["c-1"]["gap"] == "only half wired"

    def test_residual_passthrough(self):
        art = {
            "covers_goal": True,
            "coverage_score": 1.0,
            "biggest_gap": "",
            "residual_uncaptured_gap": "no migration for the widget table",
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "api.py:3"}],
                },
                {
                    "category_key": "c-2",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:9"}],
                },
            ],
        }
        out = rs.evaluate_merge_grade("ship the widget", art, self._CATS)
        assert out["residual_uncaptured_gap"] == "no migration for the widget table"

    def test_end_to_end_gate_from_evaluate_output(self):
        """The per-category output of evaluate_merge_grade drives the gate:
        an uncited 'met' → not_met → the gate blocks."""
        art = {
            "covers_goal": True,
            "coverage_score": 1.0,
            "biggest_gap": "",
            "category_verdicts": [
                {"category_key": "c-1", "status": "met", "citations": []},  # uncited
                {
                    "category_key": "c-2",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:9"}],
                },
            ],
        }
        out = rs.evaluate_merge_grade("ship the widget", art, self._CATS)
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=out["category_verdicts"],
                residual_uncaptured_gap=out["residual_uncaptured_gap"],
            )
        )
        assert result["blocker"] == "criterion_coverage_incomplete"
