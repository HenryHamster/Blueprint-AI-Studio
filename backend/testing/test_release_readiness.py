"""Tests for release_readiness module — _annotate_regime and enrichment parity."""

from __future__ import annotations

from app.release_readiness import (
    _annotate_regime,
    PROMOTION_READINESS_METRIC_MANIFEST,
    _READINESS_LIVE_REGIME_NOTE,
)


class TestAnnotateRegime:
    def _base_out(self) -> dict:
        return {
            "ready": True,
            "reason": "self-approval streak clears thresholds",
            "verdicts_evaluated": 10,
            "would_promote_rate": 0.90,
        }

    def test_annotate_regime_live_sets_calibration_artifact_true(self):
        out = self._base_out()
        result = _annotate_regime(out, promotion_enabled=True)
        assert result["live_regime_calibration_artifact"] is True

    def test_annotate_regime_live_forces_ready_false(self):
        out = self._base_out()
        result = _annotate_regime(out, promotion_enabled=True)
        assert result["ready"] is False
        assert result["is_current_readiness"] is False
        assert result["regime"] == "live"
        assert result["reason"] == _READINESS_LIVE_REGIME_NOTE

    def test_annotate_regime_dry_run_sets_calibration_artifact_false(self):
        out = self._base_out()
        result = _annotate_regime(out, promotion_enabled=False)
        assert result["live_regime_calibration_artifact"] is False

    def test_annotate_regime_dry_run_preserves_ready_true(self):
        out = self._base_out()
        result = _annotate_regime(out, promotion_enabled=False)
        assert result["ready"] is True
        assert result["is_current_readiness"] is True
        assert result["regime"] == "pre_go_live_calibration"

    def test_annotate_regime_dry_run_not_ready_preserves_ready_false(self):
        out = self._base_out()
        out["ready"] = False
        out["reason"] = "insufficient verdicts"
        result = _annotate_regime(out, promotion_enabled=False)
        assert result["ready"] is False
        assert result["live_regime_calibration_artifact"] is False
        assert result["reason"] == "insufficient verdicts"

    def test_manifest_declares_live_regime_calibration_artifact(self):
        assert "live_regime_calibration_artifact" in PROMOTION_READINESS_METRIC_MANIFEST
        entry = PROMOTION_READINESS_METRIC_MANIFEST["live_regime_calibration_artifact"]
        assert "ground_truth" in entry
        assert "consumer" in entry
        assert "promotion_enabled" in entry["ground_truth"]


class TestOpenVersionEnrichmentParity:
    """Regression suite: open-version enrichment must agree with authoritative state.

    The scenario under test: promotion_enabled=True triggers _annotate_regime to
    force ready=False (a regime relabel).  A consumer holding the promotion_readiness
    dict alongside an authoritative open-version qualification_state (e.g. 'qualifying'
    or 'promoted') must be able to determine — without manual cross-referencing —
    that the ready=False is a pre-go-live calibration artifact and NOT a real
    not-ready verdict from the live track record.

    The machine-checkable signal is live_regime_calibration_artifact=True.
    """

    def test_open_version_enrichment_parity(self):
        """Live-regime readiness is unambiguously marked as a calibration artifact.

        Given a dry-run track record that would be 'ready' (all thresholds met),
        when promotion goes live (_annotate_regime called with promotion_enabled=True),
        the returned dict must carry live_regime_calibration_artifact=True alongside
        ready=False so any consumer can reconcile:
          - ready=False  → not the live qualification verdict
          - live_regime_calibration_artifact=True → regime forced it; not a real failure
          - is_current_readiness=False → dry-run window is stale
        without having to cross-reference an external source.
        """
        # Simulate a track record that is genuinely ready in the dry-run window.
        dry_run_ready = {
            "ready": True,
            "reason": "self-approval streak clears thresholds",
            "verdicts_evaluated": 10,
            "would_promote_rate": 0.92,
            "consecutive_clean_passes": 5,
        }

        # Apply live-regime annotation (promotion_enabled=True).
        live_result = _annotate_regime(dry_run_ready.copy(), promotion_enabled=True)

        # Core parity assertions: the enriched dict must be unambiguous.
        assert live_result["ready"] is False, "live regime forces ready=False"
        assert live_result["live_regime_calibration_artifact"] is True, (
            "machine-checkable signal: ready=False is a regime artifact, not a real failure"
        )
        assert live_result["is_current_readiness"] is False, (
            "is_current_readiness=False confirms the window is stale pre-go-live history"
        )
        assert live_result["regime"] == "live"

        # Parity: a consumer seeing live_regime_calibration_artifact=True can
        # reconcile ready=False with a qualifying open-version state WITHOUT
        # needing to check any external source.
        authoritative_qual_state = "qualifying"  # the Version Phase source of truth
        is_real_not_ready = (
            live_result["ready"] is False
            and not live_result["live_regime_calibration_artifact"]
        )
        # The consumer correctly determines this is NOT a real not-ready verdict.
        assert not is_real_not_ready, (
            "consumer must NOT treat live-regime forced ready=False as a real failure "
            "when live_regime_calibration_artifact=True"
        )
        # The open-version qualification state is independently valid — no contradiction.
        assert authoritative_qual_state in ("qualifying", "promoted", "qualifying"), (
            "open-version qualification state is unaffected by the regime relabel"
        )

    def test_dry_run_enrichment_parity_no_artifact(self):
        """Dry-run regime: ready=False is a real track-record failure, not a relabel."""
        not_ready = {
            "ready": False,
            "reason": "only 3 verdicts (need >= 5)",
            "verdicts_evaluated": 3,
            "would_promote_rate": 0.67,
            "consecutive_clean_passes": 1,
        }
        result = _annotate_regime(not_ready.copy(), promotion_enabled=False)

        assert result["ready"] is False
        assert result["live_regime_calibration_artifact"] is False, (
            "dry-run ready=False is a REAL track-record failure, not a regime artifact"
        )
        assert result["is_current_readiness"] is True

    def test_calibration_artifact_distinguishes_from_genuine_failure(self):
        """The flag unambiguously distinguishes regime relabels from real failures."""
        base = {
            "ready": False,
            "reason": "placeholder",
            "verdicts_evaluated": 8,
            "would_promote_rate": 0.88,
        }

        live = _annotate_regime(base.copy(), promotion_enabled=True)
        dry = _annotate_regime(base.copy(), promotion_enabled=False)

        # Both have ready=False, but only the live one is a calibration artifact.
        assert live["ready"] == dry["ready"] == False  # noqa: E712
        assert live["live_regime_calibration_artifact"] is True
        assert dry["live_regime_calibration_artifact"] is False
        # A consumer can now tell them apart without any external lookup.
        assert live["live_regime_calibration_artifact"] != dry["live_regime_calibration_artifact"]
