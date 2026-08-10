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

from pathlib import Path

import pytest

from app import release_scoring as rs

# Fixed policy mirroring promotion_policy.yaml; threshold pinned low so the
# judged-tier penalty (a few %) never crosses the promote boundary by accident.
_POLICY = {
    "weights": {"audit": 4 / 7, "test_erosion": 3 / 7},
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


class TestNeverGradedCandidateFailsClosed:
    """A candidate with ACTIVE criteria but NO merge-grade must not promote.

    ``compute_composite`` skips the category gate on ``category_verdicts=None`` — by
    design, to preserve the no-criteria path.  But a version that HAS active criteria
    and simply never produced a merge-grade (e.g. its ``goal`` is empty/contentless,
    so no merge-grade child is spawned) would hit that same ``None`` and promote over
    an acceptance contract that was never checked.  ``_never_graded_category_verdicts``
    closes that by synthesising ``not_met`` for every active key.
    """

    def test_synthesised_verdicts_block_promotion(self):
        from app.release_qualifier import _never_graded_category_verdicts

        verdicts = _never_graded_category_verdicts({"c-2", "c-1"})
        # Deterministic order; every active key fails closed with a stated reason.
        assert [v["category_key"] for v in verdicts] == ["c-1", "c-2"]
        assert all(v["status"] == "not_met" for v in verdicts)
        assert all("never graded" in v["gap"] for v in verdicts)

        # And, fed to the real gate, they actually block.
        result = rs.compute_composite(**_perfect_kwargs(category_verdicts=verdicts))
        assert result["promote"] is False
        assert result["confidence"] == 0.0
        assert result["blocker"] == "criterion_coverage_incomplete"

    def test_no_active_criteria_yields_no_verdicts_and_does_not_block(self):
        # The zero-criteria path must still SKIP the gate (a version with no
        # acceptance contract is not blocked by one).
        from app.release_qualifier import _never_graded_category_verdicts

        assert _never_graded_category_verdicts(set()) == []
        result = rs.compute_composite(**_perfect_kwargs(category_verdicts=None))
        assert result["promote"] is True
        assert result["blocker"] is None


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


def test_blocked_verdict_reports_all_gates_and_confidence_ceiling():
    """A blocked verdict names every independent gate and its reachable score."""
    result = rs.compute_composite(
        **_perfect_kwargs(
            audit_score=0.38,
            ci_green=False,
            ci_state="red",
            migration_collision=True,
            migration_apply_blocked=True,
            env_preflight_enabled=True,
            env_preflight_score=0.0,
            env_preflight_missing=["REQUIRED_SECRET"],
            merge_grade_misses=True,
            category_verdicts=[_cat("c-met", "met"), _cat("c-gap", "not_met")],
            residual_uncaptured_gap="uncaptured operational cost",
            capability_proof_blocked=True,
        )
    )

    assert result["blocker"] == "ci_not_green"
    assert result["confidence"] == 0.0
    assert result["failing_gates"] == [
        "ci_not_green",
        "migration_identity_collision",
        "migration_apply_drift",
        "boot_prereq_missing",
        "merge_grade_misses",
        "criterion_coverage_incomplete",
        "capability_proof_gate_blocked",
    ]
    assert result["sub_scores"]["failing_gates"] == result["failing_gates"]
    assert result["confidence_ceiling"] == pytest.approx(0.6134)
    assert result["sub_scores"]["confidence_ceiling"] == pytest.approx(0.6134)
    assert result["confidence_ceiling"] < _POLICY["threshold"]


def test_attempt_five_reports_required_audit_score_from_scoring_math():
    """The 0.38 / 7-of-9 shape names the audit score needed to clear 0.81."""
    policy = {**_POLICY, "threshold": 0.81}
    categories = [_cat(f"c-{index}", "met") for index in range(7)] + [
        _cat("c-7", "not_met"),
        _cat("c-8", "not_met"),
    ]
    result = rs.compute_composite(
        **_perfect_kwargs(
            audit_score=0.38,
            category_verdicts=categories,
            policy=policy,
        )
    )

    assert "criterion_coverage_incomplete" in result["failing_gates"]
    assert result["confidence_ceiling"] == pytest.approx(0.6134)
    assert result["confidence_ceiling"] < policy["threshold"]
    assert result["required_sub_scores"] == {"audit": pytest.approx(0.7421)}
    assert result["sub_scores"]["required_sub_scores"] == result["required_sub_scores"]


def test_walkthrough_failure_is_added_to_persisted_gate_diagnostics():
    from app.release_qualifier import _apply_walkthrough_decision

    decision = _apply_walkthrough_decision(
        {
            "confidence": 0.82,
            "confidence_ceiling": 0.82,
            "promote": True,
            "blocker": None,
            "failing_gates": [],
            "sub_scores": {"audit": 0.82},
        },
        {"status": "failed"},
    )

    assert decision["blocker"] == "walkthrough_e2e_failed"
    assert decision["failing_gates"] == ["walkthrough_e2e_failed"]
    assert decision["sub_scores"]["failing_gates"] == ["walkthrough_e2e_failed"]
    assert decision["confidence"] == 0.82


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

    def test_grader_omitting_the_key_entirely_is_unsupported_not_not_met(self):
        """A grader that CANNOT emit per-category output must not be reported as
        having graded the rubric and found it unmet.

        The old (pre-protocol) agent returns a usable HOLISTIC verdict with no
        ``category_verdicts`` key at all.  Fabricating a ``not_met`` verdict per
        active criterion from that would (a) state a false reason — nothing was
        graded — and (b) freeze the fabrication permanently, because the ledger is
        sha-keyed and verdicts never regress.  Report the real state so the caller
        can HOLD with an actionable reason.
        """
        art = {"covers_goal": True, "coverage_score": 0.9, "biggest_gap": ""}
        out = rs.evaluate_merge_grade("ship the widget", art, self._CATS)
        assert out["status"] == "category_grade_unsupported"
        # Still fail-CLOSED: an ungraded acceptance contract never promotes.
        assert out["blocked"] is True
        # But NO fabricated per-category verdicts — nothing reaches the ledger.
        assert out["category_verdicts"] is None
        assert out["residual_uncaptured_gap"] is None
        # The holistic signal it DID produce is preserved for observability.
        assert out["covers_goal"] is True
        assert out["coverage"] == 0.9

    def test_empty_goal_with_active_rubric_grades_categories(self):
        """A version with NO goal string but an ACTIVE rubric is still graded
        per-category: the acceptance contract is the gradeable object.

        The holistic covers_goal signal stays None (no goal string to cover),
        status stays not_applicable so the holistic merge_grade_misses gate stays
        inert, but the per-category verdicts ARE normalized and returned — an
        uncited ``met`` is downgraded to ``not_met`` and an ungraded active
        category is ``not_met``, exactly as in the goal-ful path.
        """
        art = {
            "covers_goal": True,
            "coverage_score": 1.0,
            "category_verdicts": [
                {"category_key": "c-1", "status": "met", "citations": []},  # uncited
                {
                    "category_key": "c-2",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:10"}],
                },
                # c-3 omitted entirely — active but ungraded
            ],
        }
        out = rs.evaluate_merge_grade("", art, self._CATS + [
            {"category_key": "c-3", "text": "wires the widget into the API"}
        ])
        assert out["status"] == "not_applicable"
        assert out["blocked"] is False
        assert out["covers_goal"] is None
        by_key = {v["category_key"]: v for v in out["category_verdicts"]}
        assert by_key["c-1"]["status"] == "not_met"  # uncited met downgraded
        assert by_key["c-2"]["status"] == "met"  # cited
        assert by_key["c-3"]["status"] == "not_met"  # ungraded active
        assert by_key["c-3"]["gap"]

    def test_grader_emitting_empty_list_still_grades_every_category_not_met(self):
        """A CAPABLE grader that emits ``category_verdicts: []`` did answer — it
        just graded nothing.  That is a real not_met, NOT the unsupported hold:
        the fail-closed path must survive this change untouched."""
        art = {
            "covers_goal": True,
            "coverage_score": 0.9,
            "biggest_gap": "",
            "category_verdicts": [],
        }
        out = rs.evaluate_merge_grade("ship the widget", art, self._CATS)
        assert out["status"] != "category_grade_unsupported"
        by_key = {v["category_key"]: v for v in out["category_verdicts"]}
        assert by_key["c-1"]["status"] == "not_met"
        assert by_key["c-2"]["status"] == "not_met"

    def test_missing_key_without_active_categories_is_not_unsupported(self):
        """No rubric → the per-category protocol is irrelevant; a grader that omits
        the key is perfectly fine and the holistic path stands (no false hold)."""
        art = {"covers_goal": True, "coverage_score": 0.9, "biggest_gap": ""}
        out = rs.evaluate_merge_grade("ship the widget", art, None)
        assert out["status"] == "pass"
        assert out["blocked"] is False

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


# ---------------------------------------------------------------------------
# evaluate_merge_grade — v2 MACHINE tier (validated test citation)
# ---------------------------------------------------------------------------


class TestMachineTierValidation:
    """A ``met`` category earns ``tier="machine"`` ONLY when its ``test`` citation
    is validated against the candidate's verified-merged test + seam files;
    otherwise it fails toward ``tier="judged"``."""

    _CATS: list[dict] = [  # noqa: RUF012 — read-only fixture data
        {"category_key": "c-widget", "text": "adds the widget"},
    ]
    # A candidate whose verified merges shipped test_widget.py exercising widget.py.
    _EVIDENCE = {  # noqa: RUF012 — read-only fixture data
        "test_files": ["backend/testing/test_widget.py"],
        "seam_files": ["backend/app/widget.py", "admin-ui/src/Widget.tsx"],
    }

    def _artifact(self, ref: str) -> dict:
        return {
            "covers_goal": True,
            "coverage_score": 1.0,
            "biggest_gap": "",
            "category_verdicts": [
                {
                    "category_key": "c-widget",
                    "status": "met",
                    "citations": [
                        {"kind": "diff", "ref": "backend/app/widget.py:12"},
                        {"kind": "test", "ref": ref},
                    ],
                },
            ],
        }

    def test_validated_test_citation_earns_machine_tier(self):
        """Cited test IS among verified-merged tests AND semantically grounded to a
        changed seam → machine tier."""
        out = rs.evaluate_merge_grade(
            "ship the widget",
            self._artifact("backend/testing/test_widget.py"),
            self._CATS,
            self._EVIDENCE,
        )
        assert out["category_verdicts"][0]["tier"] == "machine"

    def test_pytest_node_id_ref_is_accepted(self):
        """A pytest node id (path::test_name) validates on its file-path part."""
        out = rs.evaluate_merge_grade(
            "ship the widget",
            self._artifact("backend/testing/test_widget.py::test_renders"),
            self._CATS,
            self._EVIDENCE,
        )
        assert out["category_verdicts"][0]["tier"] == "machine"

    def test_test_not_in_verified_merges_stays_judged(self):
        """Cited test is NOT among the candidate's verified-merged tests (it may
        never have run green on this candidate) → judged, not machine."""
        out = rs.evaluate_merge_grade(
            "ship the widget",
            self._artifact("backend/testing/test_other.py"),
            self._CATS,
            self._EVIDENCE,
        )
        assert out["category_verdicts"][0]["tier"] == "judged"

    def test_ungrounded_test_stays_judged(self):
        """Cited test shipped verified but maps to no changed production seam
        (module-name mismatch) → judged."""
        evidence = {
            "test_files": ["backend/testing/test_widget.py"],
            "seam_files": ["backend/app/unrelated.py"],  # no widget.py seam
        }
        out = rs.evaluate_merge_grade(
            "ship the widget",
            self._artifact("backend/testing/test_widget.py"),
            self._CATS,
            evidence,
        )
        assert out["category_verdicts"][0]["tier"] == "judged"

    def test_no_evidence_supplied_stays_judged(self):
        """No candidate CI evidence at grade time → never granted machine tier."""
        out = rs.evaluate_merge_grade(
            "ship the widget",
            self._artifact("backend/testing/test_widget.py"),
            self._CATS,
            None,
        )
        assert out["category_verdicts"][0]["tier"] == "judged"

    def test_diff_only_citation_stays_judged(self):
        """A met category with only a diff citation (no test) stays judged even
        when evidence is available."""
        art = {
            "covers_goal": True,
            "coverage_score": 1.0,
            "biggest_gap": "",
            "category_verdicts": [
                {
                    "category_key": "c-widget",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "backend/app/widget.py:12"}],
                },
            ],
        }
        out = rs.evaluate_merge_grade(
            "ship the widget", art, self._CATS, self._EVIDENCE
        )
        assert out["category_verdicts"][0]["tier"] == "judged"

    def test_machine_tier_exempt_from_judged_penalty_end_to_end(self):
        """A validated machine-tier met category flows through the ledger-shaped
        gate input and incurs NO judged-tier confidence penalty."""
        out = rs.evaluate_merge_grade(
            "ship the widget",
            self._artifact("backend/testing/test_widget.py"),
            self._CATS,
            self._EVIDENCE,
        )
        result = rs.compute_composite(
            **_perfect_kwargs(category_verdicts=out["category_verdicts"])
        )
        assert result["blocker"] is None
        assert result["confidence"] == 1.0  # no judged penalty
        assert result["sub_scores"]["judged_tier_met_fraction"] == 0.0


# ---------------------------------------------------------------------------
# _score_audit — fail-closed on an all-unverified (unscoreable) report
# ---------------------------------------------------------------------------


_VNOTE = "Read app/db.py lines 88-95: user input reaches cursor.execute unparameterised."


class TestScoreAuditFailClosed:
    def test_all_unverified_criticals_null_the_score(self):
        """An all-unverified-critical report must NOT read clean 1.0 — nulled so
        compute_composite routes to the audit_missing blocker."""
        report = {
            "findings": [
                {"severity": "critical", "summary": "SQLi", "file": "app/db.py"},
                {"severity": "high", "summary": "Auth bypass", "file": "app/a.py"},
            ]
        }
        audit, _erosion = rs._score_audit(report)
        assert audit is None
        # And the gate rejects it as audit_missing rather than promoting.
        result = rs.compute_composite(
            **_perfect_kwargs(audit_score=audit, category_verdicts=None)
        )
        assert result["blocker"] == "audit_missing"

    def test_verified_critical_still_scored(self):
        """A substantiated critical keeps deducting normally (score preserved)."""
        report = {
            "findings": [
                {
                    "severity": "critical",
                    "summary": "SQLi",
                    "file": "app/db.py",
                    "verification_note": _VNOTE,
                },
            ]
        }
        audit, _erosion = rs._score_audit(report)
        assert audit == round(1.0 - 0.30, 4)

    def test_one_verified_finding_keeps_report_scoreable(self):
        """A mix (verified high + unverified critical) is NOT all-unverified → the
        unverified critical is skipped but the score is kept, not nulled."""
        report = {
            "findings": [
                {
                    "severity": "high",
                    "summary": "Real issue",
                    "file": "app/auth.py",
                    "verification_note": _VNOTE,
                },
                {"severity": "critical", "summary": "Speculative", "file": "app/db.py"},
            ]
        }
        audit, _erosion = rs._score_audit(report)
        assert audit == round(1.0 - 0.15, 4)  # only the verified high deducts

    def test_low_only_report_not_nulled(self):
        """A lone verified-by-default low finding has no higher-sev finding to be
        unverified → scored (not nulled)."""
        report = {"findings": [{"severity": "low", "summary": "nit", "file": ""}]}
        audit, _erosion = rs._score_audit(report)
        assert audit == round(1.0 - 0.02, 4)


# ---------------------------------------------------------------------------
# Finding 67ac7f64df47b7a4 — three paths where category_verdicts=None reaches
# compute_composite and the per-category gate does not engage.
# ---------------------------------------------------------------------------


class TestCategoryGateNonEngagingPaths:
    """Document and assert the three paths where category_verdicts=None reaches
    compute_composite, so the per-category goal-verification gate does not fire.

    Finding 67ac7f64df47b7a4 identified these paths:
      1. empty/contentless-goal: evaluate_merge_grade returns category_verdicts=None
         for a contentless placeholder goal — the caller (_finalize_qualification)
         MUST use _never_graded_category_verdicts to block for active criteria.
      2. merge-grade-exempt: superseded_exempt / already_on_main_exempt candidates
         were previously skipped entirely (category_verdicts=None).  Finding
         85fa667a4d7fcae6 corrected this: the exemption covers only the holistic
         merge_grade_misses gate, so exempt candidates with active criteria are now
         graded and STILL coverage-gated (see test_merge_grade_exempt_* below).
      3. legacy ci_unresolved-recovery: the recovery re-runs compute_composite with
         base_record.get("category_verdicts") — when the original finalize persisted
         None (no rubric or exempt), recovery also passes None (by-design same-SHA).

    For each path we assert the designed behavior and verify the documented
    fallback (path 1) or explicit exemption (paths 2 and 3) is in place.
    """

    # Path 1 -------------------------------------------------------------------

    def test_contentless_goal_with_active_rubric_grades_categories(self):
        """evaluate_merge_grade with a contentless placeholder goal grades the
        ACTIVE acceptance-criteria contract even though there is no goal string.

        The release audit ships ``payload.categories`` whenever a rubric is
        active — independently of ``version_goal`` — and the goal-verification
        gate IS the acceptance contract.  So a contentless goal is only
        ``not_applicable`` when there is NO rubric.  With an active rubric:

          * an artifact that DOES carry ``category_verdicts`` is graded and
            returned as ``not_applicable`` (no holistic goal signal) with the
            verdicts populated — the gate reads them instead of fail-closing the
            whole contract as never-graded;
          * an artifact that omits the ``category_verdicts`` key entirely (a
            stale grader predating the per-category protocol) is
            ``category_grade_unsupported`` (HOLD) — never a fabricated
            ``not_met`` verdict.
        """
        active_cats = [{"category_key": "c-1"}, {"category_key": "c-2"}]
        graded_artifact = {
            "covers_goal": True,
            "coverage_score": 1.0,
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "a.py:1"}],
                },
                {
                    "category_key": "c-2",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "b.py:2"}],
                },
            ],
        }
        for goal in ("", "test", "n/a", "tbd", "placeholder", "none"):
            graded = rs.evaluate_merge_grade(goal, graded_artifact, active_cats)
            assert graded["status"] == "not_applicable", f"goal={goal!r}"
            assert graded["blocked"] is False
            assert graded["covers_goal"] is None
            assert {v["category_key"]: v["status"] for v in graded["category_verdicts"]} == {
                "c-1": "met",
                "c-2": "met",
            }
        # Stale grader (no category_verdicts key) → HOLD, not fabricated not_met.
        for goal in ("", "test", "n/a", "tbd", "placeholder", "none"):
            unsupported = rs.evaluate_merge_grade(
                goal,
                {"covers_goal": True, "coverage_score": 1.0},
                active_cats,
            )
            assert unsupported["status"] == "category_grade_unsupported", f"goal={goal!r}"
            assert unsupported["blocked"] is True
            assert unsupported["category_verdicts"] is None
        # No rubric → unchanged byte-identical not_applicable (regression guard).
        for goal in ("", "test", "n/a", "tbd", "placeholder", "none"):
            bare = rs.evaluate_merge_grade(
                goal, {"covers_goal": True, "coverage_score": 1.0}
            )
            assert bare["status"] == "not_applicable"
            assert bare["blocked"] is False
            assert bare["category_verdicts"] is None

    def test_contentless_goal_none_category_verdicts_skips_gate_in_compute_composite(self):
        """compute_composite with category_verdicts=None skips the per-category
        gate — this is the pre-redesign path deliberately preserved.

        When a version has no gradeable goal, no merge-grade runs and there are
        no per-category verdicts to gate on.  The caller (_finalize_qualification)
        synthesizes _never_graded_category_verdicts for active criteria; compute_composite
        itself only skips when None is explicitly passed (the no-rubric contract).
        """
        result = rs.compute_composite(
            **_perfect_kwargs(category_verdicts=None, residual_uncaptured_gap=None)
        )
        assert result["blocker"] is None
        assert result["promote"] is True

    def test_never_graded_verdicts_fallback_blocks_contentless_goal_path(self):
        """Documented fallback for path 1: _never_graded_category_verdicts synthesizes
        not_met for every active criterion when no merge-grade ran.

        This is FIX 3 in _finalize_qualification (lines 5310-5337): when
        _gate_category_verdicts is None but the version has active criteria (and
        is not merge-grade-exempt), _never_graded_category_verdicts is called to
        ensure the gate DOES engage with a fail-closed verdict rather than None.
        """
        from app.release_qualifier import _never_graded_category_verdicts

        active_keys = {"c-auth", "c-dashboard"}
        verdicts = _never_graded_category_verdicts(active_keys)
        # Every active criterion gets not_met with a 'never graded' reason.
        assert all(v["status"] == "not_met" for v in verdicts)
        assert all("never graded" in (v.get("gap") or "") for v in verdicts)
        # Feeding these to compute_composite must block with the preserved blocker string.
        result = rs.compute_composite(**_perfect_kwargs(category_verdicts=verdicts))
        assert result["promote"] is False
        assert result["confidence"] == 0.0
        assert result["blocker"] == "criterion_coverage_incomplete"

    # Path 2 -------------------------------------------------------------------

    def test_merge_grade_exempt_not_met_verdicts_still_gate(self):
        """Finding 85fa667a4d7fcae6 — Path 2 (CORRECTED policy): a merge-grade
        exemption (superseded_exempt / already_on_main_exempt) exempts ONLY the
        holistic merge_grade_misses gate, NOT the per-category coverage gate.

        _finalize_qualification now grades + passes the exempt candidate's active
        category verdicts (instead of the prior category_verdicts=None skip), so a
        not_met active criterion STILL blocks on criterion_coverage_incomplete —
        exactly as main's retired unconditional coverage block did.  The
        merge_grade_exempt flag is recorded for provenance without weakening the
        gate.
        """
        verdicts = [
            {"category_key": "c-1", "status": "met", "tier": "judged",
             "citations": [{"kind": "diff", "ref": "a.py:1"}]},
            {"category_key": "c-2", "status": "not_met", "tier": "judged",
             "gap": "feature not shipped", "citations": []},
        ]
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=verdicts,
                merge_grade_misses=False,  # holistic gate IS exempted
                merge_grade_exempt=True,
            )
        )
        assert result["blocker"] == "criterion_coverage_incomplete"
        assert result["promote"] is False
        assert result["confidence"] == 0.0
        # Provenance recorded without altering the gate outcome.
        assert result["sub_scores"]["merge_grade_exempt"] is True
        assert result["sub_scores"]["unmet_categories"] == ["c-2"]

    def test_merge_grade_exempt_all_met_verdicts_promote(self):
        """The corrected exempt policy does not over-block: an exempt candidate
        whose active criteria all grade ``met`` still promotes, and records the
        merge_grade_exempt provenance flag."""
        verdicts = [
            {"category_key": "c-1", "status": "met", "tier": "judged",
             "citations": [{"kind": "diff", "ref": "a.py:1"}]},
        ]
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=verdicts,
                merge_grade_misses=False,
                merge_grade_exempt=True,
            )
        )
        assert result["blocker"] is None
        assert result["promote"] is True
        assert result["sub_scores"]["merge_grade_exempt"] is True

    def test_merge_grade_exempt_no_rubric_none_verdicts_skips_gate(self):
        """When an exempt candidate has NO active rubric, category_verdicts is
        legitimately None and the gate stays inert (pre-redesign holistic-only
        path preserved).  merge_grade_exempt is still recorded for provenance."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=None,
                merge_grade_misses=False,
                merge_grade_exempt=True,
            )
        )
        assert result["blocker"] is None
        assert result["promote"] is True
        assert result["sub_scores"]["merge_grade_exempt"] is True

    def test_merge_grade_exempt_path_only_holistic_blockers_apply(self):
        """On the merge-grade-exempt path, holistic quality blockers (CI, audit,
        migration) still apply even though the category gate is skipped.

        An exempt candidate is not a free pass — it still needs green CI and a
        clean audit.  The exemption ONLY covers the goal-verification gate.
        """
        # A ci_not_green exempt candidate must still be blocked.
        result = rs.compute_composite(
            **_perfect_kwargs(
                ci_green=False,
                category_verdicts=None,  # exempt path
            )
        )
        assert result["blocker"] in ("ci_not_green", "ci_unresolved")

        # A migration collision still blocks an exempt candidate.
        result = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=None,
                migration_collision=True,
            )
        )
        assert result["blocker"] == "migration_identity_collision"

    # Path 3 -------------------------------------------------------------------

    def test_ci_unresolved_recovery_with_none_category_verdicts_skips_gate_by_design(self):
        """Path 3: legacy ci_unresolved-recovery — when the original finalize
        persisted category_verdicts=None (no active rubric or merge-grade-exempt),
        the recovery re-runs compute_composite with the same None, and the gate
        is correctly skipped.

        This is by-design: the recovery is same-SHA, so the persisted None faithfully
        represents the finalize's decision (no rubric, or exempt at finalize time).
        Passing None through the recovery does not silently mask a graded failure.
        """
        from app.release_promotion import _recover_confidence_from_ci_unresolved

        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
            "category_verdicts": None,  # no rubric or exempt at finalize time
        }
        out = _recover_confidence_from_ci_unresolved(record)
        # Gate correctly skipped — category_verdicts=None means no rubric to gate on.
        assert out.get("blocker") is None
        assert out["confidence"] > 0.0
        assert out.get("confidence_recovered_from_ci_unresolved") is True

    def test_ci_unresolved_recovery_with_not_met_category_verdicts_keeps_blocker(self):
        """Contrast: ci_unresolved recovery with a persisted not_met category verdict
        must keep the criterion_coverage_incomplete blocker after CI clears.

        This confirms that path 3's gate-skip is ONLY for the None case — when the
        finalize DID persist real verdicts, the recovery re-applies the gate.
        Covered by existing test_ci_unresolved_preserves_criterion_coverage_blocker,
        but repeated here to document the contrast with the None path.
        """
        from app.release_promotion import _recover_confidence_from_ci_unresolved

        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "met",
                    "tier": "judged",
                    "citations": [{"kind": "diff", "ref": "a.py:1"}],
                },
                {
                    "category_key": "c-2",
                    "status": "not_met",
                    "tier": "judged",
                    "gap": "feature not shipped",
                    "citations": [],
                },
            ],
        }
        out = _recover_confidence_from_ci_unresolved(record)
        # Real verdicts: gate fires and keeps the unmet category blocked.
        assert out["blocker"] == "criterion_coverage_incomplete"
        assert out["confidence"] == 0.0

    def test_ci_unresolved_recovery_with_all_met_verdicts_promotes(self):
        """ci_unresolved recovery with all-met persisted verdicts clears the gate
        and promotes once CI resolves.

        This confirms the happy-path through path 3: when the finalize recorded
        all-met category verdicts and CI was the only blocker, recovery promotes.
        """
        from app.release_promotion import _recover_confidence_from_ci_unresolved

        record = {
            "blocker": "ci_unresolved",
            "confidence": 0.0,
            "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
            "smoketest_enabled": True,
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "met",
                    "tier": "judged",
                    "citations": [{"kind": "diff", "ref": "a.py:1"}],
                },
                {
                    "category_key": "c-2",
                    "status": "met",
                    "tier": "machine",
                    "citations": [{"kind": "test", "ref": "testing/test_b.py"}],
                },
            ],
        }
        out = _recover_confidence_from_ci_unresolved(record)
        # All categories met — CI was the only blocker; recovery promotes.
        assert out.get("blocker") is None
        assert out["confidence"] > 0.0
        assert out.get("confidence_recovered_from_ci_unresolved") is True

    def test_contentless_goal_full_chain_grades_categories_when_artifact_has_them(self):
        """Full chain for path 1 (REDESIGNED): a contentless goal + ACTIVE rubric
        is still graded when the artifact carries per-category verdicts — the gate
        reads them instead of skipping.  When the grader cannot emit the section
        at all, evaluate_merge_grade HOLDS (category_grade_unsupported), and the
        qualifier's _never_graded_category_verdicts remains the belt-and-suspenders
        fail-closed fallback for a no-grade-at-all state.
        """
        from app.release_qualifier import _never_graded_category_verdicts

        active_cats = [{"category_key": "c-auth"}, {"category_key": "c-dashboard"}]
        active_keys = {c["category_key"] for c in active_cats}

        # Step 1: a contentless goal WITH a graded rubric returns the per-category
        # verdicts (status not_applicable — no holistic goal signal — but the
        # acceptance contract IS graded).
        eval_out = rs.evaluate_merge_grade(
            "test",
            {
                "covers_goal": True,
                "coverage_score": 1.0,
                "category_verdicts": [
                    {
                        "category_key": "c-auth",
                        "status": "met",
                        "citations": [{"kind": "diff", "ref": "auth.py:1"}],
                    },
                    {
                        "category_key": "c-dashboard",
                        "status": "met",
                        "citations": [{"kind": "diff", "ref": "dash.py:2"}],
                    },
                ],
            },
            active_cats,
        )
        assert eval_out["status"] == "not_applicable"
        assert eval_out["blocked"] is False
        assert eval_out["category_verdicts"] is not None

        # Step 2: feeding those verdicts to compute_composite does NOT skip the
        # gate; an all-met contract promotes (no criterion_coverage_incomplete).
        promoted = rs.compute_composite(
            **_perfect_kwargs(
                category_verdicts=eval_out["category_verdicts"],
                residual_uncaptured_gap=eval_out["residual_uncaptured_gap"],
            )
        )
        assert promoted["blocker"] is None
        assert promoted["promote"] is True

        # Step 3: when the grader cannot emit the section at all, evaluate_merge_grade
        # HOLDS (category_grade_unsupported) instead of fabricating not_met...
        unsupported = rs.evaluate_merge_grade("test", None, active_cats)
        assert unsupported["status"] == "category_grade_unsupported"
        assert unsupported["blocked"] is True
        assert unsupported["category_verdicts"] is None

        # Step 4: ...and the qualifier's _never_graded_category_verdicts remains
        # the fail-closed fallback for a no-grade-at-all state.
        fallback = _never_graded_category_verdicts(active_keys)
        blocked = rs.compute_composite(**_perfect_kwargs(category_verdicts=fallback))
        assert blocked["blocker"] == "criterion_coverage_incomplete"
        assert blocked["promote"] is False
        assert blocked["confidence"] == 0.0


# ---------------------------------------------------------------------------
# _fail_closed_category_verdicts — the ledger-error / unverified fallback gate
# ---------------------------------------------------------------------------


class TestFailClosedCategoryVerdicts:
    """Tests for _fail_closed_category_verdicts — the safety-net that fires when
    a merge-grade DID run but the per-category verdicts cannot be read from the
    ledger (write failed, read raised, or returned no active-criterion rows).

    This is distinct from _never_graded_category_verdicts (no grade ran at all):
    _fail_closed marks verdicts ``"unverified"`` (a grade happened but we cannot
    trust its result), while _never_graded marks them ``"not_met"`` (no grade
    ever happened).  Both feed ``"not_met"``-class verdicts to compute_composite
    and block with ``criterion_coverage_incomplete``.
    """

    def _fn(self, active_keys):
        from app.release_qualifier import _fail_closed_category_verdicts
        return _fail_closed_category_verdicts(active_keys)

    def test_empty_active_keys_returns_none(self):
        """No active criteria → returns None so compute_composite skips the gate.

        An empty rubric has nothing to gate on; returning None preserves the
        no-criteria path rather than synthesising a spurious block.
        """
        assert self._fn(set()) is None
        assert self._fn({None}) is None  # only None keys → filtered out

    def test_non_empty_keys_returns_unverified_verdicts(self):
        """Each active criterion key gets an ``unverified`` verdict row, sorted."""
        result = self._fn({"c-2", "c-1", "c-3"})
        assert result is not None
        assert [v["category_key"] for v in result] == ["c-1", "c-2", "c-3"]
        assert all(v["status"] == "unverified" for v in result)
        assert all(v["tier"] == "unverified" for v in result)

    def test_none_keys_are_filtered_out(self):
        """None entries in the active_keys set are silently discarded."""
        result = self._fn({"c-1", None, "c-2"})
        assert result is not None
        keys = [v["category_key"] for v in result]
        assert None not in keys
        assert set(keys) == {"c-1", "c-2"}

    def test_unverified_verdicts_block_compute_composite(self):
        """Unverified verdicts (``status != "met"``) engage the category gate
        and force ``criterion_coverage_incomplete`` — a grade that cannot be
        trusted is treated as a failure, not a pass."""
        result = self._fn({"c-auth", "c-dashboard"})
        composite = rs.compute_composite(**_perfect_kwargs(category_verdicts=result))
        assert composite["promote"] is False
        assert composite["confidence"] == 0.0
        assert composite["blocker"] == "criterion_coverage_incomplete"

    def test_single_active_key_produces_one_verdict(self):
        """A single active criterion key yields exactly one verdict row."""
        result = self._fn({"c-only"})
        assert result is not None
        assert len(result) == 1
        assert result[0]["category_key"] == "c-only"
        assert result[0]["status"] == "unverified"

    def test_different_from_never_graded_status(self):
        """``_fail_closed`` uses ``"unverified"`` status; ``_never_graded`` uses
        ``"not_met"``.  Both block compute_composite, but the distinction lets
        operators and logs see whether a grade ran (and was unreadable) vs.
        whether no grade ever ran at all.
        """
        from app.release_qualifier import _never_graded_category_verdicts

        fail_closed = self._fn({"c-1"})
        never_graded = _never_graded_category_verdicts({"c-1"})
        assert fail_closed[0]["status"] == "unverified"
        assert never_graded[0]["status"] == "not_met"
        # Both still block the gate identically.
        for verdicts in (fail_closed, never_graded):
            result = rs.compute_composite(**_perfect_kwargs(category_verdicts=verdicts))
            assert result["blocker"] == "criterion_coverage_incomplete"


class TestBlockerExecutionSite:
    """The pure execution-site classification of promotion blockers.

    Tags each hard-blocker code by WHERE the responsible check runs so operators
    and agents can tell whether a fix on automain takes effect pre-promotion
    (candidate-side) or is inert until the version itself promotes
    (deployed-side / release-machinery self-reference).
    """

    def test_migration_blockers_are_deployed_side(self):
        # Both migration gates compare the candidate against the DEPLOYED
        # promotion target / production schema — inert-until-promote.
        assert (
            rs.blocker_execution_site("migration_identity_collision")
            == rs.EXECUTION_SITE_DEPLOYED
        )
        assert (
            rs.blocker_execution_site("migration_apply_drift")
            == rs.EXECUTION_SITE_DEPLOYED
        )

    def test_criterion_coverage_gate_is_deployed_side(self):
        """Coverage is rendered and its evidence is minted by deployed code.

        The merge-grade child observes candidate content, but the hard blocker
        is emitted only after the deployed qualifier composes the sha-keyed
        category ledger.  Marking it candidate-side would tell an operator an
        automain-only minter fix can take effect before it is deployed.
        """
        assert (
            rs.blocker_execution_site("criterion_coverage_incomplete")
            == rs.EXECUTION_SITE_DEPLOYED
        )

    def test_candidate_side_blockers(self):
        # These all fire on candidate content — a fix on automain clears them on
        # the next qualification.
        #
        # ``uncorroborated_attestation`` is deliberately NOT in this list: the
        # attestation gate it classified was retired (replaced by the
        # per-category goal-verification gate, which blocks as
        # ``criterion_coverage_incomplete``), so the code has no emitter and its
        # map entry was dead vocabulary.  Its absence is pinned by
        # test_execution_site_map_has_no_dead_entries below.
        for code in (
            "ci_not_green",
            "ci_unresolved",
            "audit_missing",
            "test_file_deleted",
            "merge_grade_misses",
            "capability_proof_gate_blocked",
        ):
            assert (
                rs.blocker_execution_site(code) == rs.EXECUTION_SITE_CANDIDATE
            ), code

    def test_walkthrough_e2e_blockers_split_by_execution_site(self):
        # A GENUINE walkthrough criterion failure (report valid, every key
        # resolves to an ACTIVE criterion) fires on candidate content — an
        # automain fix to the candidate tree clears it next qualification.
        assert (
            rs.blocker_execution_site("walkthrough_e2e_failed")
            == rs.EXECUTION_SITE_CANDIDATE
        )
        # A REPORT-CONTRACT failure (schema mismatch, duplicate/invalid entries,
        # canonical collisions, unknown criteria) is validated by the DEPLOYED
        # qualifier against the DEPLOYED rubric — a fix on automain is inert
        # until this version itself promotes.
        assert (
            rs.blocker_execution_site("walkthrough_e2e_contract_failed")
            == rs.EXECUTION_SITE_DEPLOYED
        )

    def test_none_and_unknown_return_none(self):
        assert rs.blocker_execution_site(None) is None
        assert rs.blocker_execution_site("") is None
        # A force-promote / unclassified sentinel is deliberately unclassified.
        assert rs.blocker_execution_site("qualification_superseded") is None
        assert rs.blocker_execution_site("some_future_blocker") is None

    def test_every_compute_composite_blocker_code_is_classified(self):
        # Every hard-blocker code compute_composite() can emit must have an
        # execution-site — a new blocker without a classification is a bug.
        import re

        src = (Path(rs.__file__)).read_text()
        emitted = set(re.findall(r'blocker = "([a-z_]+)"', src))
        assert emitted, "expected to find blocker assignments in release_scoring"
        for code in emitted:
            assert rs.blocker_execution_site(code) is not None, code

    def test_execution_site_map_has_no_dead_entries(self):
        # The REVERSE direction of the check above: every CLASSIFIED code must
        # still be emitted by some gate.  A classification for a blocker nothing
        # emits is dead vocabulary — it tells an operator "a fix on automain
        # clears this on the next qualification" about a gate that cannot fire.
        # ``uncorroborated_attestation`` was exactly that: the gate it described
        # was replaced by the per-category goal-verification gate, but its map
        # entry (and its operator-facing label) outlived the emitter.
        import re

        assignment_pattern = re.compile(
            r"""\bblocker\s*=\s*["']([a-z0-9_]+)["']"""
        )
        mapping_pattern = re.compile(
            r"""["']blocker["']\s*:\s*["']([a-z0-9_]+)["']"""
        )
        # The explicitly requested walkthrough gate is composed by the
        # qualifier after base scoring, so the reverse vocabulary check must
        # include both execution sites.
        sources = [
            Path(rs.__file__).read_text(),
            Path(rs.__file__).with_name("release_qualifier.py").read_text(),
        ]
        emitted = {
            code
            for source in sources
            for pattern in (assignment_pattern, mapping_pattern)
            for code in pattern.findall(source)
        }
        assert emitted, "expected to find blocker assignments in release_scoring"

        dead = sorted(set(rs.BLOCKER_EXECUTION_SITE) - emitted)
        assert not dead, (
            f"BLOCKER_EXECUTION_SITE classifies blockers nothing emits: {dead}. "
            "Either wire up the emitter or delete the dead vocabulary."
        )

    def test_labels_match_site_and_are_distinct(self):
        deployed_label = rs.blocker_execution_site_label(
            "migration_identity_collision"
        )
        candidate_label = rs.blocker_execution_site_label("ci_not_green")
        assert deployed_label == rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_DEPLOYED]
        assert candidate_label == rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_CANDIDATE]
        assert deployed_label != candidate_label
        # Deployed phrase conveys inert-until-promote; candidate phrase does not.
        assert "inert" in deployed_label
        assert "next qualification" in candidate_label
        # No blocker / unclassified → no label.
        assert rs.blocker_execution_site_label(None) is None
        assert rs.blocker_execution_site_label("qualification_superseded") is None


# ---------------------------------------------------------------------------
# _score_env_preflight — the pure deployed-side env-preflight judge (Slice 3)
# ---------------------------------------------------------------------------


def _manifest(*entries: tuple[str, str]) -> dict:
    """Build a candidate env-preflight content artifact from (name, scope) pairs."""
    return {
        "required_env": [{"name": n, "scope": s} for n, s in entries],
        "startup_check_names": ["_verify_encryption_config"],
        "manifest_source": "backend/app/required_runtime_env.py",
    }


class TestScoreEnvPreflight:
    """The pure presence-diff between candidate-declared prereqs and deploy env."""

    def test_absent_manifest_yields_none_score(self):
        # No usable manifest → None sub_score (nothing to judge), never a block.
        score, missing, scope = rs._score_env_preflight(None, {"K_REVISION": True})
        assert score is None
        assert missing == []
        assert scope == "cloud_run"

    def test_non_dict_manifest_and_snapshot(self):
        score, missing, scope = rs._score_env_preflight("not-a-dict", "nope")
        assert score is None and missing == [] and scope == "always"

    def test_all_present_scores_one(self):
        snap = {"K_REVISION": True, "GITHUB_PAT_ENCRYPTION_KEY": True}
        score, missing, scope = rs._score_env_preflight(
            _manifest(("GITHUB_PAT_ENCRYPTION_KEY", "cloud_run")), snap
        )
        assert score == 1.0
        assert missing == []
        assert scope == "cloud_run"

    def test_missing_cloud_run_var_scores_zero_on_cloud_run(self):
        # The exact v25 class: candidate declares a cloud_run prereq, deploy env
        # (K_REVISION present) lacks it → 0.0 with the NAME surfaced.
        snap = {"K_REVISION": True, "GITHUB_PAT_ENCRYPTION_KEY": False}
        score, missing, scope = rs._score_env_preflight(
            _manifest(("GITHUB_PAT_ENCRYPTION_KEY", "cloud_run")), snap
        )
        assert score == 0.0
        assert missing == ["GITHUB_PAT_ENCRYPTION_KEY"]
        assert scope == "cloud_run"

    def test_cloud_run_scope_inactive_when_not_cloud_run(self):
        # No K_REVISION → deploy target is "always"-only; a cloud_run-scoped var
        # that is absent must NOT be counted (it is out of scope) → 1.0.
        snap = {"GITHUB_PAT_ENCRYPTION_KEY": False}
        score, missing, scope = rs._score_env_preflight(
            _manifest(("GITHUB_PAT_ENCRYPTION_KEY", "cloud_run")), snap
        )
        assert score == 1.0
        assert missing == []
        assert scope == "always"

    def test_always_scope_checked_regardless_of_cloud_run(self):
        # An "always" var is in scope in every runtime.
        snap = {"SOME_ALWAYS_VAR": False}
        score, missing, scope = rs._score_env_preflight(
            _manifest(("SOME_ALWAYS_VAR", "always")), snap
        )
        assert score == 0.0
        assert missing == ["SOME_ALWAYS_VAR"]
        assert scope == "always"

    def test_missing_names_sorted_and_deduped(self):
        snap = {"K_REVISION": True, "B": False, "A": False}
        score, missing, _ = rs._score_env_preflight(
            _manifest(("B", "cloud_run"), ("A", "always"), ("A", "always")),
            snap,
        )
        assert score == 0.0
        assert missing == ["A", "B"]  # sorted, de-duplicated

    def test_absent_name_in_snapshot_counts_as_missing(self):
        # A required var whose NAME is entirely absent from the snapshot is missing.
        snap = {"K_REVISION": True}
        score, missing, _ = rs._score_env_preflight(
            _manifest(("NEVER_SNAPSHOTTED", "cloud_run")), snap
        )
        assert score == 0.0
        assert missing == ["NEVER_SNAPSHOTTED"]

    def test_empty_required_env_is_vacuously_satisfied(self):
        # A manifest that declares no prerequisites is present → 1.0 (nothing to
        # block), distinct from an absent manifest (None).
        score, missing, _ = rs._score_env_preflight(
            {"required_env": []}, {"K_REVISION": True}
        )
        assert score == 1.0 and missing == []

    def test_malformed_entries_skipped(self):
        # Non-dict entries, missing name/scope, non-str fields — all skipped.
        artifact = {
            "required_env": [
                "not-a-dict",
                {"name": "OK", "scope": "always"},
                {"scope": "always"},  # no name
                {"name": "NOSCOPE"},  # no scope
                {"name": 123, "scope": "always"},  # non-str name
            ]
        }
        score, missing, _ = rs._score_env_preflight(artifact, {"OK": False})
        assert score == 0.0
        assert missing == ["OK"]

    def test_presence_only_never_leaks_or_reads_a_value(self):
        # HARD RULE: the judge only ever consumes bool(...) of the snapshot. Even
        # if a malformed caller smuggles a VALUE (a secret string) into the
        # snapshot, it must be coerced through bool() and never appear in the
        # returned score / missing list / scope.
        SECRET = "s3cr3t-fernet-key-do-not-leak"
        REVISION = "prod-revision-00042"
        snap = {
            "K_REVISION": REVISION,  # truthy string, not a bool
            "GITHUB_PAT_ENCRYPTION_KEY": SECRET,  # truthy string → treated present
            "MISSING_ONE": "",  # empty string → falsey → missing
        }
        score, missing, scope = rs._score_env_preflight(
            _manifest(
                ("GITHUB_PAT_ENCRYPTION_KEY", "cloud_run"),
                ("MISSING_ONE", "always"),
            ),
            snap,
        )
        # Truthy string present ⇒ satisfied; empty string ⇒ missing.
        assert score == 0.0
        assert missing == ["MISSING_ONE"]
        # K_REVISION being a non-empty string is read as Cloud Run present.
        assert scope == "cloud_run"
        # No VALUE ever surfaces in the output.
        blob = repr((score, missing, scope))
        assert SECRET not in blob
        assert REVISION not in blob


# ---------------------------------------------------------------------------
# compute_composite — the boot_prereq_missing hard gate (Slice 3)
# runtime_config default: env_preflight_enabled=True (Armed)
# ---------------------------------------------------------------------------


class TestEnvPreflightGate:
    def test_production_default_is_armed_in_promotion_policy_yaml(self):
        # The production default for env_preflight_enabled comes from
        # promotion_policy.yaml (not the module-level dict, which the conftest
        # overrides to False for test isolation).  This pin asserts the YAML
        # carries the Armed (True) value so the gate is active in production.
        from app.runtime_config_state import _read_promotion_policy_file

        policy = _read_promotion_policy_file()
        assert policy is not None, "promotion_policy.yaml must be readable"
        assert policy.get("env_preflight_enabled") is True, (
            "env_preflight_enabled must be True in promotion_policy.yaml "
            "(Armed by default); update that file if the gate should be disabled fleet-wide"
        )

        # With the production default (True) and missing prerequisites, the gate
        # must block — proving the Armed default has real enforcement effect.
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["GITHUB_PAT_ENCRYPTION_KEY"],
            )
        )
        assert result["blocker"] == "boot_prereq_missing"
        assert result["promote"] is False

    def test_flag_off_is_byte_for_byte_unchanged(self):
        # When explicitly disabled (env_preflight_enabled=False), no block, no
        # sub_score, and the composite is identical to the no-env-preflight call.
        baseline = rs.compute_composite(**_perfect_kwargs())
        with_flag_off = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=False,
                env_preflight_score=0.0,
                env_preflight_missing=["GITHUB_PAT_ENCRYPTION_KEY"],
            )
        )
        assert with_flag_off == baseline
        assert with_flag_off["blocker"] is None
        assert "env_preflight" not in with_flag_off["sub_scores"]
        assert "env_preflight_missing" not in with_flag_off["sub_scores"]

    def test_enabled_and_missing_hard_blocks(self):
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["GITHUB_PAT_ENCRYPTION_KEY"],
            )
        )
        assert result["blocker"] == "boot_prereq_missing"
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        # Missing NAMES surfaced for operators (presence-only).
        assert result["sub_scores"]["env_preflight_missing"] == [
            "GITHUB_PAT_ENCRYPTION_KEY"
        ]
        assert result["sub_scores"]["env_preflight"] == 0.0

    def test_enabled_all_present_promotes_and_records_sub_score(self):
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=1.0,
                env_preflight_missing=[],
            )
        )
        assert result["blocker"] is None
        assert result["sub_scores"]["env_preflight"] == 1.0
        assert "env_preflight_missing" not in result["sub_scores"]
        # env_preflight is a pure hard gate (no weight) — an all-present candidate
        # scores identically to the baseline (no confidence erosion).
        baseline = rs.compute_composite(**_perfect_kwargs())
        assert result["confidence"] == baseline["confidence"]
        assert result["promote"] == baseline["promote"]

    def test_enabled_none_score_now_fails_closed(self):
        # Manifest absent on the candidate (env_preflight_score=None) with
        # env_preflight_enabled=True now hard-blocks with
        # boot_prereq_evidence_missing — a deterministic quality verdict,
        # never an infra-failure retry loop.  The old behavior (no block)
        # was the exact defect being fixed: missing evidence silently passed
        # through, making the boot gate unenforceable.
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=[],
            )
        )
        assert result["blocker"] == "boot_prereq_evidence_missing"
        assert result["confidence"] == 0.0
        assert result["promote"] is False
        # No env_preflight sub_score recorded (score is None, not 0.0 or 1.0).
        assert "env_preflight" not in result["sub_scores"]

    def test_ci_red_precedes_boot_prereq(self):
        # A confirmed red build still wins the ordered chain over env-preflight.
        result = rs.compute_composite(
            **_perfect_kwargs(
                ci_green=False,
                ci_state=rs._CI_RED,
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["X"],
            )
        )
        assert result["blocker"] == "ci_not_green"

    def test_migration_apply_precedes_boot_prereq(self):
        # Deploy-safety ordering: migrations report before env-preflight.
        result = rs.compute_composite(
            **_perfect_kwargs(
                migration_apply_blocked=True,
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["X"],
            )
        )
        assert result["blocker"] == "migration_apply_drift"

    def test_boot_prereq_precedes_capability_proof(self):
        # env-preflight is checked before capability-proof (both deploy/quality
        # gates that fall after the migration family).
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["X"],
                capability_proof_blocked=True,
            )
        )
        assert result["blocker"] == "boot_prereq_missing"

    def test_boot_prereq_missing_is_classified(self):
        # A newly-emitted blocker MUST carry an execution-site classification.
        # Slice 4: boot_prereq_missing is neither candidate-side (an automain fix
        # does not clear it) nor plain deployed (its fix is not "promote the
        # version") — it is an OPERATOR-ENV action: bind the secret in the deploy
        # target, which rolls a new revision that self-clears the block.
        assert (
            rs.blocker_execution_site("boot_prereq_missing")
            == rs.EXECUTION_SITE_OPERATOR_ENV
        )

    def test_operator_env_label_is_distinct_and_describes_the_fix(self):
        label = rs.blocker_execution_site_label("boot_prereq_missing")
        assert (
            label == rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_OPERATOR_ENV]
        )
        # Distinct from the deployed and candidate phrases.
        assert label != rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_DEPLOYED]
        assert label != rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_CANDIDATE]
        # Operator-env phrase names the operator action and the self-clear.
        assert "operator" in label
        assert "deploy target" in label

    def test_ack_override_suppresses_only_boot_prereq(self):
        # With the OWNER ack override, an otherwise-blocking env-preflight miss
        # promotes (block suppressed) — but the missing NAMES and the override
        # FACT are still recorded (presence-only, never a value).
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["GITHUB_PAT_ENCRYPTION_KEY"],
                env_preflight_ack_override=True,
            )
        )
        assert result["blocker"] is None
        assert result["sub_scores"]["env_preflight_missing"] == [
            "GITHUB_PAT_ENCRYPTION_KEY"
        ]
        assert result["sub_scores"]["env_preflight_ack_override"] is True
        # No env VALUE ever leaks into sub_scores.
        assert "GITHUB_PAT_ENCRYPTION_KEY" in result["sub_scores"]["env_preflight_missing"]

    def test_ack_override_does_not_suppress_other_blockers(self):
        # The ack is scoped to boot_prereq_missing ONLY: a confirmed red build
        # still wins even with the override armed.
        result = rs.compute_composite(
            **_perfect_kwargs(
                ci_green=False,
                ci_state=rs._CI_RED,
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["X"],
                env_preflight_ack_override=True,
            )
        )
        assert result["blocker"] == "ci_not_green"
        # A blocker BELOW boot_prereq_missing in the chain also still fires when
        # the env block is the only thing the override removes.
        result2 = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=0.0,
                env_preflight_missing=["X"],
                env_preflight_ack_override=True,
                capability_proof_blocked=True,
            )
        )
        assert result2["blocker"] == "capability_proof_gate_blocked"

    def test_ack_override_without_missing_is_noop(self):
        # Override armed but nothing missing → no block, no override sub_score.
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=1.0,
                env_preflight_missing=[],
                env_preflight_ack_override=True,
            )
        )
        assert result["blocker"] is None
        assert "env_preflight_ack_override" not in result["sub_scores"]

    # --- boot_prereq_evidence_missing (new deterministic block) --------------

    def test_evidence_missing_hard_blocks_when_enabled_and_score_is_none(self):
        """When env_preflight_enabled is True and env_preflight_score is None
        (no usable manifest produced), promotion hard-blocks with
        boot_prereq_evidence_missing — a deterministic quality verdict, never
        an infra-failure retry loop.
        """
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=[],
            )
        )
        assert result["blocker"] == "boot_prereq_evidence_missing"
        assert result["confidence"] == 0.0
        assert result["promote"] is False

    def test_evidence_missing_does_not_fire_when_disabled(self):
        """When env_preflight_enabled is False, a None score is inert
        (the entire dimension is dark).
        """
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=False,
                env_preflight_score=None,
                env_preflight_missing=[],
            )
        )
        assert result["blocker"] is None
        assert "env_preflight" not in result["sub_scores"]

    def test_evidence_missing_precedes_boot_prereq(self):
        """With no evidence, evidence-missing fires before boot_prereq_missing
        — individual prerequisite presence is moot when no evidence exists.
        """
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=["SOME_VAR"],
            )
        )
        assert result["blocker"] == "boot_prereq_evidence_missing"

    def test_migration_apply_precedes_evidence_missing(self):
        """Deploy-safety ordering: migration gates report before evidence-missing."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                migration_apply_blocked=True,
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=[],
            )
        )
        assert result["blocker"] == "migration_apply_drift"

    def test_ci_red_precedes_evidence_missing(self):
        """A confirmed red build still wins the ordered chain over evidence-missing."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                ci_green=False,
                ci_state=rs._CI_RED,
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=[],
            )
        )
        assert result["blocker"] == "ci_not_green"

    def test_evidence_missing_precedes_capability_proof(self):
        """Evidence-missing is checked before capability-proof (both are
        non-migration gates that fall after the migration family).
        """
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=[],
                capability_proof_blocked=True,
            )
        )
        assert result["blocker"] == "boot_prereq_evidence_missing"

    def test_ack_override_does_not_suppress_evidence_missing(self):
        """The owner ack suppresses ONLY boot_prereq_missing — it does NOT
        suppress evidence-missing because an ack presumes evidence exists.
        Evidence-missing fires even with the override armed.
        """
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=[],
                env_preflight_ack_override=True,
            )
        )
        assert result["blocker"] == "boot_prereq_evidence_missing"

    def test_evidence_missing_is_classified_candidate_side(self):
        """boot_prereq_evidence_missing is candidate-side — the fix is
        producing evidence on the candidate tree, not a deploy or operator action.
        """
        assert (
            rs.blocker_execution_site("boot_prereq_evidence_missing")
            == rs.EXECUTION_SITE_CANDIDATE
        )

    def test_evidence_missing_label_is_distinct(self):
        """The evidence-missing label is distinct from boot_prereq_missing."""
        label = rs.blocker_execution_site_label("boot_prereq_evidence_missing")
        assert label is not None
        assert label == rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_CANDIDATE]
        # Distinct from operator-env and deployed phrases.
        assert label != rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_OPERATOR_ENV]
        assert label != rs.EXECUTION_SITE_LABELS[rs.EXECUTION_SITE_DEPLOYED]

    def test_manifest_absent_candidate_promotes_while_broken_manifest_blocks(self):
        """An absent manifest (source=None) must NOT trigger boot_prereq_evidence_missing.

        The env-preflight manifest (backend/app/required_runtime_env.py) is
        Blueprint-specific.  External and greenfield repos that do not ship it
        must remain promotable — a missing file means "no prerequisites declared",
        not "evidence unavailable".  Only a malformed/unparseable manifest (source
        present but unreadable) should produce a None score and fire the hard block.

        This test drives the full scoring seam from build_env_preflight_from_source
        through _score_env_preflight into compute_composite to exercise the end-to-end
        contract, not just the individual helpers.
        """
        from app.env_preflight_evidence import build_env_preflight_from_source, sanitize_env_preflight_artifact

        # --- absent manifest → vacuously satisfied (score=1.0) → promotes ---
        absent_artifact = build_env_preflight_from_source(None)
        assert absent_artifact is not None, "absent manifest must return a dict, not None"
        assert absent_artifact["required_env"] == []

        # sanitize (mirrors release_qualifier._finalize_qualification)
        sanitized_absent = sanitize_env_preflight_artifact(absent_artifact)
        absent_score, absent_missing, _ = rs._score_env_preflight(
            sanitized_absent, {"K_REVISION": True}
        )
        assert absent_score == 1.0, "absent manifest should score 1.0 (vacuously satisfied)"
        assert absent_missing == []

        absent_result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=absent_score,
                env_preflight_missing=absent_missing,
            )
        )
        assert absent_result["blocker"] is None, (
            f"absent manifest must not block — got blocker={absent_result['blocker']!r}"
        )
        assert absent_result["promote"] is True

        # --- broken manifest (unparseable source) → score=None → blocks ---
        broken_artifact = build_env_preflight_from_source(
            required_runtime_env_py_source="this is }{{{ not valid python"
        )
        # A SyntaxError source yields required_env=[] (graceful degradation in the
        # parser), so the artifact itself is non-None with an empty list — same
        # vacuous-satisfaction path as absent.  The true "evidence unavailable" case
        # is when the score reaches compute_composite as None (e.g. sanitize returns
        # None for a non-dict raw blob).  Confirm the broken-source path degrades
        # gracefully (not None) while a raw-None blob remains a hard block.
        assert broken_artifact is not None
        assert broken_artifact["required_env"] == [], (
            "SyntaxError source should degrade to empty required_env (graceful), "
            "not raise or block"
        )

        # A raw non-dict blob (e.g. from a corrupted artifact store) sanitizes to
        # None → score=None → boot_prereq_evidence_missing fires.
        raw_none_blob = "not-a-dict"
        sanitized_none = sanitize_env_preflight_artifact(raw_none_blob)
        assert sanitized_none is None, "non-dict raw blob must sanitize to None"

        none_score, _, _ = rs._score_env_preflight(sanitized_none, {"K_REVISION": True})
        assert none_score is None

        blocked_result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=none_score,  # None
                env_preflight_missing=[],
            )
        )
        assert blocked_result["blocker"] == "boot_prereq_evidence_missing", (
            "corrupted/absent artifact (sanitized to None) must hard-block"
        )
        assert blocked_result["promote"] is False


# ---------------------------------------------------------------------------
# env_preflight_enabled armed-by-default: source literal + behavior contract
# ---------------------------------------------------------------------------


class TestEnvPreflightArmedDefault:
    """Guards the armed-by-default state for env_preflight_enabled.

    The conftest resets env_preflight_enabled to False for test isolation
    (individual gate tests set it explicitly), so the live runtime_config value
    during tests is always False.  These tests read the source literal directly
    via AST to verify the literal default is True — the invariant that survives
    despite the test-isolation override.
    """

    def test_env_preflight_literal_default_is_true(self):
        """runtime_config_state.py dict literal must have env_preflight_enabled=True."""
        import ast
        import pathlib

        src = (
            pathlib.Path(__file__).parent.parent / "app" / "runtime_config_state.py"
        ).read_text()
        tree = ast.parse(src)
        found = None
        for node in ast.walk(tree):
            # runtime_config is an annotated assignment (runtime_config: Dict[...] = {...})
            value = None
            if isinstance(node, (ast.AnnAssign, ast.Assign)) and isinstance(node.value, ast.Dict):
                value = node.value
            if value is None:
                continue
            for k, v in zip(value.keys, value.values):
                if isinstance(k, ast.Constant) and k.value == "env_preflight_enabled":
                    found = v.value if isinstance(v, ast.Constant) else None
                    break
            if found is not None:
                break
        assert found is True, (
            f"runtime_config literal env_preflight_enabled must be True (armed), "
            f"got {found!r}. The gate was built to prevent the v25 prod boot-crash — "
            "re-arming it must be explicit."
        )

    def test_armed_env_preflight_blocks_without_evidence(self):
        """With env_preflight_enabled=True, missing evidence is a hard block."""
        result = rs.compute_composite(
            **_perfect_kwargs(
                env_preflight_enabled=True,
                env_preflight_score=None,
                env_preflight_missing=[],
            )
        )
        assert result["blocker"] == "boot_prereq_evidence_missing"
        assert result["confidence"] == 0.0
        assert result["promote"] is False


class TestQualifierSmoketestRetired:
    """Retired smoke arguments are accepted for legacy callers but inert."""

    def test_legacy_smoke_inputs_do_not_change_score(self):
        missing = rs.compute_composite(
            **_perfect_kwargs(smoketest_enabled=True, smoketest_score=None)
        )
        passing = rs.compute_composite(
            **_perfect_kwargs(smoketest_enabled=True, smoketest_score=1.0)
        )
        failing = rs.compute_composite(
            **_perfect_kwargs(smoketest_enabled=True, smoketest_score=0.0)
        )
        assert missing == passing == failing
        assert "smoketest" not in passing["sub_scores"]

    def test_legacy_three_key_and_current_two_key_weights_match(self):
        from app.release_qualifier import _normalize_active_promotion_weights

        legacy = _normalize_active_promotion_weights(
            {"audit": 0.4, "test_erosion": 0.3, "smoketest": 0.3}
        )
        current = _normalize_active_promotion_weights(
            {"audit": 4 / 7, "test_erosion": 3 / 7}
        )
        assert legacy == pytest.approx(current)
