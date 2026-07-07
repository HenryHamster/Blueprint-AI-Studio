"""Tests for release_readiness module — _annotate_regime and enrichment parity."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest

from app import release_version as rv
from app.models import Project
from app.release_readiness import (
    _annotate_regime,
    _compute_requalification_decision,
    compute_promotion_ci_green_gate,
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


class TestComputeRequalificationDecision:
    """Unit tests for the pure requalification readiness decision function.

    Exercises _compute_requalification_decision (sync, no DB) which is the
    logic that was previously inlined in compute_requalification_readiness.
    The key regression is the truncated-but-fully-resolved wedge (all visible
    gating findings resolved, truncated=True) — this used to force ready=False
    forever, permanently blocking the auto-redrive.
    """

    def _finding(self, key: str, severity: str = "medium") -> dict:
        return {"key": key, "severity": severity, "summary": f"Finding {key}"}

    # ── core wedge fix ──────────────────────────────────────────────────────

    def test_truncated_all_resolved_is_ready(self):
        """Truncated verdict + all visible gating findings resolved → ready=True.

        This is the v22 wedge: 12 findings at the cap, all resolved in the
        ledger, but truncated=True permanently blocked re-qualification.
        """
        carried = [self._finding("k1"), self._finding("k2", "high")]
        ledger = {"k1": "resolved", "k2": "stale"}
        result = _compute_requalification_decision(
            carried, truncated=True, ledger_states=ledger, open_version_id="vid"
        )
        assert result["requalification_ready"] is True
        assert result["audit_findings_truncated"] is True
        assert result["gating_total"] == 2
        assert result["gating_resolved"] == 2
        assert "re-qualification will re-score" in result["reason"]

    def test_truncated_partial_resolved_not_ready(self):
        """Truncated verdict with unresolved visible gating findings → ready=False."""
        carried = [self._finding("k1"), self._finding("k2")]
        ledger = {"k1": "resolved"}  # k2 missing → unresolved
        result = _compute_requalification_decision(
            carried, truncated=True, ledger_states=ledger, open_version_id="vid"
        )
        assert result["requalification_ready"] is False
        assert result["audit_findings_truncated"] is True
        assert result["gating_resolved"] == 1
        assert result["gating_total"] == 2
        assert "partial list" in result["reason"]

    # ── non-truncated behavior unchanged ────────────────────────────────────

    def test_not_truncated_all_resolved_is_ready(self):
        carried = [self._finding("k1", "high")]
        result = _compute_requalification_decision(
            carried, truncated=False, ledger_states={"k1": "resolved"}, open_version_id="vid"
        )
        assert result["requalification_ready"] is True
        assert result["audit_findings_truncated"] is False

    def test_not_truncated_unresolved_not_ready(self):
        carried = [self._finding("k1")]
        result = _compute_requalification_decision(
            carried, truncated=False, ledger_states={}, open_version_id="vid"
        )
        assert result["requalification_ready"] is False
        assert "unresolved" in result["reason"]

    def test_no_gating_findings_ready(self):
        """Low-severity findings only → gating_total=0 → ready regardless of truncation."""
        carried = [{"key": "k", "severity": "low", "summary": "s"}]
        result = _compute_requalification_decision(
            carried, truncated=False, ledger_states={}, open_version_id="vid"
        )
        assert result["requalification_ready"] is True
        assert result["gating_total"] == 0

    def test_keyless_finding_uses_sentinel(self):
        """A finding without a key resolves via the __keyless__ sentinel."""
        carried = [{"severity": "medium", "summary": "no key"}]
        ledger = {"__keyless__": "resolved"}
        result = _compute_requalification_decision(
            carried, truncated=False, ledger_states=ledger, open_version_id="vid"
        )
        assert result["requalification_ready"] is True
        assert result["findings"][0]["key"] == "__keyless__"

    def test_result_shape(self):
        """Return dict always carries all expected keys."""
        result = _compute_requalification_decision([], False, {}, "test-version-id")
        for key in (
            "requalification_ready",
            "open_version_id",
            "audit_findings_truncated",
            "gating_total",
            "gating_resolved",
            "reason",
            "findings",
        ):
            assert key in result, f"missing key: {key}"
        assert result["open_version_id"] == "test-version-id"


async def _make_project_and_version(db_session, *, phase: str = "stabilize"):
    """Minimal project (authorized repo, distinct integration/promotion branches)
    plus an open version in the requested phase."""
    project = Project(
        name="RR-Test",
        user_id="rr-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    if phase != "build":
        rv.set_version_phase(version, phase, reasoning="test setup")
        db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


def _patch_gate_github(
    mocker,
    *,
    run: dict | None,
    head_sha: str = "abc123def456",
    resolved=("tok", "acme", "widget"),
    dispatch_ok: bool = True,
):
    """Monkeypatch the github helpers the CI green gate consults — no live network.

    Returns the ``dispatch_workflow_run`` mock so tests can assert on it.
    """
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=resolved),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=head_sha))
    mocker.patch(
        "app.github.get_workflow_run_for_sha", new=AsyncMock(return_value=run)
    )
    return mocker.patch(
        "app.github.dispatch_workflow_run", new=AsyncMock(return_value=dispatch_ok)
    )


class TestPromotionCiGreenGate:
    """Unit tests for compute_promotion_ci_green_gate state mapping (mocked GitHub)."""

    @pytest.mark.asyncio
    async def test_completed_success_maps_to_green(self, db_session, mocker):
        project, _ = await _make_project_and_version(db_session)
        disp = _patch_gate_github(
            mocker,
            run={
                "run_id": 11,
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://gh/run/11",
            },
            head_sha="deadbeef",
        )
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=True
        )
        assert gate["gate_state"] == "green"
        assert gate["head_sha"] == "deadbeef"
        assert gate["run_id"] == 11
        # A green (or any existing) run must NOT trigger a fresh dispatch.
        disp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_progress_maps_to_pending(self, db_session, mocker):
        project, _ = await _make_project_and_version(db_session)
        _patch_gate_github(
            mocker,
            run={"run_id": 12, "status": "in_progress", "conclusion": None},
        )
        gate = await compute_promotion_ci_green_gate(db_session, project.id)
        assert gate["gate_state"] == "pending"
        assert gate["run_id"] == 12

    @pytest.mark.asyncio
    async def test_completed_failure_maps_to_red(self, db_session, mocker):
        project, _ = await _make_project_and_version(db_session)
        _patch_gate_github(
            mocker,
            run={"run_id": 13, "status": "completed", "conclusion": "failure"},
        )
        gate = await compute_promotion_ci_green_gate(db_session, project.id)
        assert gate["gate_state"] == "red"
        assert gate["run_id"] == 13

    @pytest.mark.asyncio
    async def test_completed_cancelled_without_dispatch_maps_to_no_signal(
        self, db_session, mocker
    ):
        project, _ = await _make_project_and_version(db_session)
        disp = _patch_gate_github(
            mocker,
            run={"run_id": 14, "status": "completed", "conclusion": "cancelled"},
            head_sha="deadbeef",
        )
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=False
        )
        assert gate["gate_state"] == "no_signal"
        assert gate["run_id"] == 14
        assert gate["head_sha"] == "deadbeef"
        disp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_completed_cancelled_with_dispatch_redispatches(
        self, db_session, mocker
    ):
        """A cancelled run with dispatch=True must trigger a fresh dispatch."""
        project, _ = await _make_project_and_version(db_session)
        disp = _patch_gate_github(
            mocker,
            run={"run_id": 15, "status": "completed", "conclusion": "cancelled"},
            head_sha="cafe5678",
        )
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=True
        )
        assert gate["gate_state"] == "dispatched"
        assert gate["head_sha"] == "cafe5678"
        disp.assert_awaited_once()
        args = disp.await_args.args
        assert args[3] == "ci.yml"
        assert args[4] == "automain"

    @pytest.mark.asyncio
    async def test_no_run_with_dispatch_maps_to_dispatched(self, db_session, mocker):
        project, _ = await _make_project_and_version(db_session)
        disp = _patch_gate_github(mocker, run=None, head_sha="cafe1234")
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=True
        )
        assert gate["gate_state"] == "dispatched"
        assert gate["head_sha"] == "cafe1234"
        disp.assert_awaited_once()
        # Must dispatch ci.yml on the project's integration branch.
        args = disp.await_args.args
        assert args[3] == "ci.yml"
        assert args[4] == "automain"

    @pytest.mark.asyncio
    async def test_no_run_without_dispatch_maps_to_no_signal(self, db_session, mocker):
        project, _ = await _make_project_and_version(db_session)
        disp = _patch_gate_github(mocker, run=None)
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=False
        )
        assert gate["gate_state"] == "no_signal"
        disp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_credentials_maps_to_unavailable(self, db_session, mocker):
        project, _ = await _make_project_and_version(db_session)
        mocker.patch(
            "app.release_promotion._resolve_github_with_session",
            new=AsyncMock(return_value=None),
        )
        gate = await compute_promotion_ci_green_gate(db_session, project.id)
        assert gate["gate_state"] == "unavailable"

    @pytest.mark.asyncio
    async def test_promotion_ci_green_gate_defers_when_suite_not_green(
        self, db_session, mocker
    ):
        """Named acceptance test: a non-green integration-HEAD CI run makes the
        promotion_failed re-driver DEFER — it returns False without calling
        run_qualification, so no audit and no circuit-breaker attempt are burned.

        Exercises the real production code end-to-end: compute_promotion_ci_green_gate
        (release_readiness) resolves a FAILED ci.yml run for the integration HEAD,
        and _redrive_promotion_failed_qualification (release_qualifier_redrive) reads
        that non-green verdict and defers.
        """
        from app.release_qualifier_sweep import (
            _redrive_promotion_failed_qualification,
        )

        project, version = await _make_project_and_version(db_session)
        version.last_qualification = {
            "status": "promotion_failed",
            "candidate_sha": "abc123def456",
            "error": "required_check_failed",
            "promotion_failure_count": 1,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Remediation window clear → readiness ready; the ONLY thing that should
        # stop the re-drive is the not-green CI gate.
        mocker.patch(
            "app.release_readiness.compute_requalification_readiness",
            new=AsyncMock(return_value={"requalification_ready": True}),
        )
        # Real gate runs against these mocked github helpers → a FAILED run.
        _patch_gate_github(
            mocker,
            run={"run_id": 99, "status": "completed", "conclusion": "failure"},
            head_sha="abc123def456",
        )
        run_qual = mocker.patch(
            "app.release_qualifier.run_qualification",
            new=AsyncMock(return_value={"status": "spawned"}),
        )

        @contextlib.asynccontextmanager
        async def _factory():
            yield db_session

        result = await _redrive_promotion_failed_qualification(
            version.id, session_factory=_factory
        )

        assert result is False
        run_qual.assert_not_awaited()
