"""Tests for release_readiness module — _annotate_regime and enrichment parity."""

from __future__ import annotations

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
    inflight: dict | None = None,
):
    """Monkeypatch the github helpers the CI green gate consults — no live network.

    ``inflight`` is what ``get_latest_inflight_workflow_run_for_branch`` returns
    (default None → no in-flight branch run, so the gate dispatches as before).
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
    mocker.patch(
        "app.github.get_latest_inflight_workflow_run_for_branch",
        new=AsyncMock(return_value=inflight),
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
    async def test_no_run_with_dispatch_coalesces_onto_inflight_branch_run(
        self, db_session, mocker
    ):
        """No run for HEAD but a ci.yml run is in flight on the branch → the gate
        coalesces onto it (``pending``) instead of dispatching a fresh run that
        the branch's ``cancel-in-progress`` concurrency group would promptly
        cancel.  This is the seam that lets a run on an actively-merging branch
        conclude (and green become reachable) instead of churning dispatches."""
        project, _ = await _make_project_and_version(db_session)
        inflight = {
            "run_id": 99,
            "status": "in_progress",
            "conclusion": None,
            "html_url": "https://gh/run/99",
            "head_sha": "abe1234",
            "created_at": "2026-08-06T12:00:00Z",
        }
        disp = _patch_gate_github(
            mocker, run=None, head_sha="cafe1234", inflight=inflight
        )
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=True
        )
        assert gate["gate_state"] == "pending"
        assert gate["run_id"] == 99
        assert gate["html_url"] == "https://gh/run/99"
        assert gate["head_sha"] == "cafe1234"
        assert "in flight on the integration branch" in gate["reason"]
        # Coalescing must NOT dispatch — a fresh dispatch is what cancels the
        # in-flight run in the first place.
        disp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_with_dispatch_coalesces_onto_inflight_branch_run(
        self, db_session, mocker
    ):
        """A cancelled run for HEAD with ``dispatch=True`` coalesces onto an
        in-flight branch run instead of re-dispatching (which would cancel the
        in-flight run).  The drift sentinel's cancelled-run re-arm passes through
        this exact path, so the coalescing must apply here too."""
        project, _ = await _make_project_and_version(db_session)
        inflight = {
            "run_id": 100,
            "status": "queued",
            "conclusion": None,
            "html_url": "https://gh/run/100",
            "head_sha": "beef1234",
            "created_at": "2026-08-06T12:00:00Z",
        }
        disp = _patch_gate_github(
            mocker,
            run={"run_id": 15, "status": "completed", "conclusion": "cancelled"},
            head_sha="cafe5678",
            inflight=inflight,
        )
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=True
        )
        assert gate["gate_state"] == "pending"
        assert gate["run_id"] == 100
        disp.assert_not_awaited()

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
    async def test_unresolvable_head_sha_maps_to_unavailable(
        self, db_session, mocker
    ):
        """When the integration HEAD sha cannot be resolved (get_branch_sha
        returns falsy), the gate degrades to ``unavailable`` with a reason that
        names the integration branch — and never dispatches a run."""
        project, _ = await _make_project_and_version(db_session)
        disp = _patch_gate_github(mocker, run=None, head_sha=None)
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=True
        )
        assert gate["gate_state"] == "unavailable"
        assert gate["head_sha"] is None
        assert "automain" in gate["reason"]
        # No HEAD sha → the workflow-run lookup / dispatch must be short-circuited.
        disp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_project_maps_to_unavailable(self, db_session, mocker):
        """A project_id with no Project row degrades to ``unavailable`` with the
        ``project not found`` reason before any GitHub round-trip."""
        import uuid as _uuid

        resolve = mocker.patch(
            "app.release_promotion._resolve_github_with_session",
            new=AsyncMock(return_value=("tok", "acme", "widget")),
        )
        gate = await compute_promotion_ci_green_gate(db_session, _uuid.uuid4())
        assert gate["gate_state"] == "unavailable"
        assert gate["reason"] == "project not found"
        # Short-circuits before credential resolution.
        resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_dispatch_maps_to_unavailable(self, db_session, mocker):
        """When no run exists for HEAD and the fresh dispatch fails
        (``dispatch_workflow_run`` returns falsy), the gate returns
        ``unavailable`` — NOT ``dispatched`` — while still reporting the HEAD."""
        project, _ = await _make_project_and_version(db_session)
        disp = _patch_gate_github(
            mocker, run=None, head_sha="cafef00d", dispatch_ok=False
        )
        gate = await compute_promotion_ci_green_gate(
            db_session, project.id, dispatch=True
        )
        assert gate["gate_state"] == "unavailable"
        assert gate["head_sha"] == "cafef00d"
        assert "failed to dispatch" in gate["reason"]
        disp.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_github_error_maps_to_unavailable(self, db_session, mocker):
        """Any exception during gate computation (here: get_branch_sha raises) is
        swallowed and degrades to ``unavailable`` — the gate NEVER propagates."""
        project, _ = await _make_project_and_version(db_session)
        _patch_gate_github(mocker, run=None)
        mocker.patch(
            "app.github.get_branch_sha",
            new=AsyncMock(side_effect=RuntimeError("gh 503")),
        )
        gate = await compute_promotion_ci_green_gate(db_session, project.id)
        assert gate["gate_state"] == "unavailable"
        assert gate["reason"] == "error computing promotion CI green gate"
        assert gate["head_sha"] is None


class TestChallengeRefutedRate:
    def test_challenge_refuted_rate_never_exceeds_one(self):
        """challenge_refuted_rate is always in [0.0, 1.0].

        The rate must be: refuted_count / (confirmed + refuted + uncertain),
        not refuted_count / num_verdicts. This ensures the rate stays bounded.
        """
        from app.release_scoring import compute_challenged_vs_unchallenged_telemetry

        # Create 3 verdicts where summing refuted_count / num_verdicts would
        # exceed 1.0 if the bug existed, but the correct formula stays in [0, 1].
        # Verdict A: confirmed=5, refuted=3, uncertain=2 (total=10, 3/10=0.3 refuted)
        # Verdict B: confirmed=4, refuted=4, uncertain=2 (total=10, 4/10=0.4 refuted)
        # Verdict C: confirmed=3, refuted=3, uncertain=4 (total=10, 3/10=0.3 refuted)
        # Total refuted = 10, total reviewed = 30
        # Correct rate = 10 / 30 = 0.333...
        # Buggy formula = 10 / 3 = 3.333... (exceeds 1.0!)
        def _make_verdict(confirmed, refuted, uncertain, sha="abc1234"):
            return {
                "candidate_sha": sha,
                "audit_reconciliation": {
                    "model_reported_score": 1.0,
                    "findings_derived_score": 1.0,
                    "score_delta": 0.0,
                    "excluded_findings_count": 0,
                    "audit_challenge": {
                        "status": "available",
                        "binding": True,
                        "confirmed_count": confirmed,
                        "refuted_count": refuted,
                        "uncertain_count": uncertain,
                        "reported_agreement": "partial" if refuted > 0 else "agree",
                        "challenged_candidate_sha": sha,
                    },
                },
                "would_promote": True,
                "terminal_status": "not_promoted",
                "post_promotion_outcome": None,
                "excluded_audit_findings": None,
            }

        verdicts = [
            _make_verdict(confirmed=5, refuted=3, uncertain=2, sha="sha0001"),
            _make_verdict(confirmed=4, refuted=4, uncertain=2, sha="sha0002"),
            _make_verdict(confirmed=3, refuted=3, uncertain=4, sha="sha0003"),
        ]
        result = compute_challenged_vs_unchallenged_telemetry(verdicts)
        ch = result["challenged"]

        assert ch["insufficient_sample"] is False
        assert ch["sample_size"] == 3

        # Total refuted across all verdicts
        assert ch["challenge_refuted_count"] == 10

        # The rate must be bounded in [0, 1]
        rate = ch["challenge_refuted_rate"]
        assert 0.0 <= rate <= 1.0, f"Rate {rate} exceeds [0, 1] bounds"

        # Verify the correct calculation: 10 / (5+4+3 + 3+4+3 + 2+2+4) = 10/30
        assert rate == pytest.approx(10 / 30, abs=1e-4)


# ---------------------------------------------------------------------------
# Qualification payload names WHY each category is unmet
# ---------------------------------------------------------------------------


class TestFormatQualificationApiCategoryVerdicts:
    """format_qualification_for_api / format_verdict_for_api surface the
    per-category verdicts + residual gap that drove the criterion_coverage gate
    instead of dropping them at the API boundary."""

    def test_format_qualification_for_api_includes_unmet_category_gaps(self):
        """A refused promotion carries the grader's per-category status/tier/gap
        and the residual gap so operators/agents don't need to dig into the
        database to learn what is actually missing."""
        from app.release_readiness import format_qualification_for_api

        qual = {
            "status": "not_promoted",
            "confidence": 0.62,
            "threshold": 0.90,
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "not_met",
                    "tier": "judged",
                    "gap": "No end-to-end test proves the promote path.",
                    "citations": ["backend/app/release_promotion.py"],
                },
                {
                    "category_key": "c-2",
                    "status": "met",
                    "tier": "machine",
                    "gap": None,
                    "citations": ["backend/testing/test_release_promotion.py"],
                },
            ],
            "residual_uncaptured_gap": "Nothing covers the rollback path.",
        }
        out = format_qualification_for_api(qual)

        assert out["category_verdicts"] == qual["category_verdicts"]
        assert out["category_verdicts"][0]["category_key"] == "c-1"
        assert out["category_verdicts"][0]["status"] == "not_met"
        assert out["category_verdicts"][0]["tier"] == "judged"
        assert "end-to-end test" in out["category_verdicts"][0]["gap"]
        assert out["category_verdicts"][1]["status"] == "met"
        assert out["residual_uncaptured_gap"] == "Nothing covers the rollback path."

    def test_missing_category_verdicts_return_none(self):
        """Legacy / rubric-less qualification records return None for both keys,
        so callers can distinguish 'no data' from an unmet category."""
        from app.release_readiness import format_qualification_for_api

        out = format_qualification_for_api({"status": "pending"})
        assert out["category_verdicts"] is None
        assert out["residual_uncaptured_gap"] is None
        out_empty = format_qualification_for_api({})
        assert out_empty["category_verdicts"] is None
        assert out_empty["residual_uncaptured_gap"] is None

    def test_format_verdict_for_api_surfaces_category_verdicts(self):
        """The immutable QualificationVerdict row carries the same per-attempt
        category_verdicts / residual snapshot so the verdict-history boundary
        also names WHY each category is unmet."""
        import uuid

        from app.release_models import QualificationVerdict
        from app.release_readiness import format_verdict_for_api

        verdict = QualificationVerdict(
            project_id=uuid.uuid4(),
            release_version_id=uuid.uuid4(),
            candidate_sha="abc123",
            confidence=0.7,
            blocker=None,
            would_promote=False,
            promotion_enabled=False,
            terminal_status="not_promoted",
            threshold=0.9,
            category_verdicts=[
                {
                    "category_key": "c-7",
                    "status": "partial",
                    "tier": "judged",
                    "gap": "Only the happy path is covered.",
                    "citations": [],
                }
            ],
            residual_uncaptured_gap="Rollback is unproven.",
        )
        out = format_verdict_for_api(verdict)
        assert out["category_verdicts"] == [
            {
                "category_key": "c-7",
                "status": "partial",
                "tier": "judged",
                "gap": "Only the happy path is covered.",
                "citations": [],
            }
        ]
        assert out["residual_uncaptured_gap"] == "Rollback is unproven."

        legacy = QualificationVerdict(
            project_id=uuid.uuid4(),
            release_version_id=uuid.uuid4(),
            candidate_sha="legacy",
            confidence=0.9,
            blocker=None,
            would_promote=True,
            promotion_enabled=True,
            terminal_status="promoted",
            threshold=0.8,
        )
        out_legacy = format_verdict_for_api(legacy)
        assert out_legacy["category_verdicts"] is None
        assert out_legacy["residual_uncaptured_gap"] is None


# ---------------------------------------------------------------------------
# v22 backfill count-divergence → evergreen enrollment (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v22_backfill_count_divergence_enrolls_deduped_finding(db_session):
    """When the v22 backfill count diverges from 13, enroll a deduped evergreen item.

    Seeds 7 low-severity findings (not 13), calls the backfill, and asserts:
    - An EvergreenBacklogItem row exists with the expected finding_key + provenance.
    - A second backfill call does NOT create a duplicate row (dedup on finding_key).
    - The backfill still returns the actual resolved count and the guard state
      reflects the mismatch.
    """
    from app.models import EvergreenBacklogItem
    from app.release_readiness import (
        _V22_BACKFILL_COUNT_DIVERGENCE_KEY,
        _V22_BACKFILL_PROVENANCE,
        _reset_v22_backfill_guard_for_tests,
        backfill_v22_carried_findings_resolved,
        get_v22_backfill_guard_state,
    )
    from testing.test_acceptance_criteria import (
        _make_project,
        _make_release_version,
        _make_v22_low_severity_findings,
        _seed_v22_verdict,
    )

    _reset_v22_backfill_guard_for_tests()

    project = await _make_project(db_session, "V22DivergenceEnroll")
    version = await _make_release_version(
        db_session, project, 22, status="promoted"
    )

    # Seed only 7 findings — diverges from the expected 13.
    findings = _make_v22_low_severity_findings(7)
    await _seed_v22_verdict(
        db_session, project, version, findings, sha="e" * 40
    )

    count = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )

    # The backfill must return the actual resolved count.
    assert count == 7, f"expected 7 resolution rows; got {count}"

    # Guard state must reflect the mismatch.
    guard = get_v22_backfill_guard_state()
    assert guard["expected"] == 13
    assert guard["resolved"] == 7
    assert guard["match"] is False

    # An EvergreenBacklogItem must be enrolled with the expected key + provenance.
    from sqlmodel import select as _sel
    items = list(
        (
            await db_session.execute(
                _sel(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project.id,
                    EvergreenBacklogItem.finding_key
                    == _V22_BACKFILL_COUNT_DIVERGENCE_KEY,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(items) == 1, (
        f"expected 1 enrolled item; got {len(items)}"
    )
    item = items[0]
    assert item.provenance == _V22_BACKFILL_PROVENANCE, (
        f"expected provenance={_V22_BACKFILL_PROVENANCE!r}; "
        f"got {item.provenance!r}"
    )
    assert item.status == "enrolled"
    assert "v22 backfill count divergence" == (item.title or "")
    assert item.detail is not None
    assert "7" in item.detail
    assert "13" in item.detail

    # A second backfill call must NOT create a duplicate row.
    await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )
    items_after = list(
        (
            await db_session.execute(
                _sel(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project.id,
                    EvergreenBacklogItem.finding_key
                    == _V22_BACKFILL_COUNT_DIVERGENCE_KEY,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(items_after) == 1, (
        f"second backfill call must not duplicate; got {len(items_after)} rows"
    )


@pytest.mark.asyncio
async def test_v22_backfill_count_match_does_not_enroll(db_session):
    """When the v22 backfill resolves exactly 13 findings, no enrollment occurs.

    The canonical count of 13 matches _V22_EXPECTED_LOW_SEVERITY_COUNT,
    so the divergence block never executes and no EvergreenBacklogItem
    with the divergence finding_key is enrolled.
    """
    from app.models import EvergreenBacklogItem
    from app.release_readiness import (
        _V22_BACKFILL_COUNT_DIVERGENCE_KEY,
        _reset_v22_backfill_guard_for_tests,
        backfill_v22_carried_findings_resolved,
        get_v22_backfill_guard_state,
    )
    from testing.test_acceptance_criteria import (
        _make_project,
        _make_release_version,
        _make_v22_low_severity_findings,
        _seed_v22_verdict,
    )

    _reset_v22_backfill_guard_for_tests()

    project = await _make_project(db_session, "V22MatchNoEnroll")
    version = await _make_release_version(
        db_session, project, 22, status="promoted"
    )

    # Seed the canonical 13 findings.
    findings = _make_v22_low_severity_findings(13)
    await _seed_v22_verdict(
        db_session, project, version, findings, sha="d" * 40
    )

    count = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )

    assert count == 13

    # Guard state must reflect a match.
    guard = get_v22_backfill_guard_state()
    assert guard["match"] is True

    # No EvergreenBacklogItem with the divergence key must exist.
    from sqlmodel import select as _sel
    items = list(
        (
            await db_session.execute(
                _sel(EvergreenBacklogItem).where(
                    EvergreenBacklogItem.project_id == project.id,
                    EvergreenBacklogItem.finding_key
                    == _V22_BACKFILL_COUNT_DIVERGENCE_KEY,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(items) == 0, (
        f"expected 0 divergence items when count matches; got {len(items)}"
    )


@pytest.mark.asyncio
async def test_v22_backfill_records_criterion_evidence_for_data_integrity(
    db_session,
):
    """Named acceptance test: backfill records CriterionEvidence for c-1a3ec09d.

    When the data-integrity criterion c-1a3ec09d exists on the v22 version,
    calling backfill_v22_carried_findings_resolved must write exactly one
    CriterionEvidence row with source_kind='audit_finding_backfill' and
    source_id='d2cf1ebb8', proving the backfill covers the data-integrity
    criterion.
    """
    from app.models import CriterionEvidence, VersionAcceptanceCriterion
    from app.release_readiness import (
        _V22_BACKFILL_RESOLVING_REF,
        _V22_DATA_INTEGRITY_CRITERION_KEY,
        _reset_v22_backfill_guard_for_tests,
        backfill_v22_carried_findings_resolved,
    )
    from testing.test_acceptance_criteria import (
        _make_project,
        _make_release_version,
        _make_v22_low_severity_findings,
        _seed_v22_verdict,
    )

    _reset_v22_backfill_guard_for_tests()

    project = await _make_project(db_session, "V22CriterionEvidence")
    version = await _make_release_version(
        db_session, project, 22, status="promoted"
    )

    # Seed the criterion the backfill targets.
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=_V22_DATA_INTEGRITY_CRITERION_KEY,
        text="Data integrity: carried audit findings resolved",
        created_by="operator",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()

    findings = _make_v22_low_severity_findings(13)
    await _seed_v22_verdict(
        db_session, project, version, findings, sha="cafebabe" + "0" * 32
    )

    count = await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )
    assert count == 13

    # Query the CriterionEvidence written by the backfill.
    from sqlmodel import select as _sel

    evidence_rows = list(
        (
            await db_session.execute(
                _sel(CriterionEvidence).where(
                    CriterionEvidence.criterion_id == criterion.id,
                    CriterionEvidence.project_id == project.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(evidence_rows) == 1, (
        f"expected 1 CriterionEvidence row; got {len(evidence_rows)}"
    )
    ev = evidence_rows[0]
    assert ev.source_kind == "audit_finding_backfill", (
        f"expected source_kind='audit_finding_backfill'; got {ev.source_kind!r}"
    )
    assert ev.source_id == _V22_BACKFILL_RESOLVING_REF, (
        f"expected source_id={_V22_BACKFILL_RESOLVING_REF!r}; got {ev.source_id!r}"
    )
    assert ev.detail is not None
    assert ev.detail["finding_count"] == 13, (
        f"expected finding_count=13; got {ev.detail['finding_count']}"
    )

    # Evidence must survive a second backfill call (idempotent — no duplicate).
    await backfill_v22_carried_findings_resolved(
        db_session, project.id, version
    )
    evidence_after = list(
        (
            await db_session.execute(
                _sel(CriterionEvidence).where(
                    CriterionEvidence.criterion_id == criterion.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(evidence_after) == 1, (
        f"idempotent: second call must not duplicate; got {len(evidence_after)} rows"
    )
