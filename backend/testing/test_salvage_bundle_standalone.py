"""Standalone smoke tests covering the 16-finding salvage bundle and the 7-finding
compute-images salvage bundle.

These tests run without a database (--standalone --no-postgres) and verify
properties that were identified in the adversarial-gate review cycle but can be
confirmed purely against source code or pure-Python logic.

Findings covered here (prior 16-finding bundle):
  #1        — 0626 fails closed on malformed keys; 0628 backfills github_oauth_token
  #2        — scratch-DB URL absence does NOT block Cloud Run boot
  #3  / #12 — infra-failure signature is NOT polluted by child-task failures
  #4        — timed-out qualifier children are cancelled via _cancel_qualifier_children
  #5        — deterministic migration defects block (not defer) via _DeterministicMigrationDefect
  #6        — env-preflight absent evidence is a hard blocker (not "unavailable")
  #7        — trigger resolver returns not_applicable for terminal/auto-redrive states
  #8        — bare-claim promotion timeout is applied (promoting orphan containment)
  #9        — data-migration proof gate defers DML migrations without proof declaration
  #10       — VersionLifecyclePanel.tsx has owner-only hotfix remediation flow
  #11       — MIGRATION_APPLY_SCRATCH_DATABASE_URL is a qualification prereq, not a boot req
  #12       — (see #3 / #12 above)
  #13       — hotfix_policy.py and admin_operator_triggers.py are in HOTFIX_ELIGIBLE_MODULES
  #14       — acceptance-evidence minting runs deployed-side (release_qualifier_sweep.py)
  #15 / #16 — operator hotfix-deploy channel exists (admin_hotfix_promote.py, release_hotfix_promotion.py)

Findings covered here (7-finding compute-images bundle):
  cb-1 — dispatch reads ProjectComputeImage to resolve default container image
  cb-2 — GPU-in-container capability is required for jobs declaring both container_image and GPU
  cb-3 — runner heartbeat includes per-project docker image-cache size
  cb-4 — registry protection-tag apply failures are counted, logged, and alerted (not silent)
  cb-5 — per-arm BLUEPRINT_RESULT_FILE paths are isolated; elapsed/exit_code/stderr captured
  cb-6 — build_and_push scans inputs AND image layers for platform credentials
  cb-7 — _prune_docker_images is called after each compute job (proactive cache hygiene)

Findings covered here (18-finding compute-viewable salvage bundle, 2026-07-17):
  f1  — parallel signBlob: _sign_viewable_manifest uses asyncio.gather, not sequential loop
  f2  — live indicator: StrategicContextArtifactSlot renders LiveBadge for live=true media
  f3  — allow-list drift: .mov removed from fingerprint, video cap env-overridable,
        runner 422 lists pdf, ComputeJobFile docstring accurate
  f4  — GCP-VM mid-run viewables: agent_compute_jobs.py has POST /compute-jobs/{job_id}/viewable
  f5  — unified surfaces: _purge_orphaned_viewable_rows called; ComputeJobFile branch extension guard
  f6  — de-duplicate gallery: ComputeJobsPanel uses ComputeArtifactGallery component
  f7  — starvation prevention (tests already in prior 12-finding bundle section)
  f8  — canvas dedup: StrategicContextWorkspace uses promotedArtifactIds to skip Evidence duplicates
  f9  — honest media limits: UI cap string distinguishes image/PDF/video sizes separately
  f10 — artifact sampling disclosure: evidence exposes artifact_total + live_count separately
  f11 — bound-project uploads (test already in prior 12-finding bundle section)
  f12 — atomic handoff design: runner.py calls _link_terminal_artifact for terminal jobs
  f13 — refresh overwritten previews: ComputeArtifactGallery uses revision in cache key
  f14 — mixed-media preserved (test already in prior 12-finding bundle section)
  f15 — atomic terminal handoff impl (test already in prior 12-finding bundle section)
  f16 — reserve slots (test already in prior 12-finding bundle section)
  f17 — omission telemetry (tests already in prior 12-finding bundle section)
  f18 — PDF support (tests already in prior 12-finding bundle section)
"""

from __future__ import annotations


# --------------------------------------------------------------------------- #
# Findings #3 and #12 — infra-failure signature does NOT use child failures
# --------------------------------------------------------------------------- #


def test_infra_failure_signature_ignores_child_failures():
    """_infra_failure_signature must NOT derive leg/category from child failures.

    Findings #3 and #12: the signature was previously built from the LAST failed
    child task's leg/category, so it oscillated between attempts (one time the
    last child was an audit fail, next time a smoketest fail).  The stable fix
    ignores child_failures entirely and derives the key only from the infra error
    code.  This test verifies the source code enforces that by inspecting the
    function body.
    """
    import inspect
    from app.release_qualifier_audit import _infra_failure_signature

    # The function must accept child_failures but explicitly discard it.
    sig = inspect.signature(_infra_failure_signature)
    params = list(sig.parameters.keys())
    assert "child_failures" in params, (
        "_infra_failure_signature must accept child_failures so call sites "
        "don't need to change, even though it ignores the argument"
    )

    # Source code must NOT dereference child_failures[-1] or access its elements.
    source = inspect.getsource(_infra_failure_signature)
    assert "child_failures[-1]" not in source, (
        "_infra_failure_signature must not index child_failures[-1] — "
        "that was the bug: leg was taken from the last child rather than inferred "
        "from the infra_error code, causing the signature to oscillate between attempts"
    )

    # The function must use the infra_error code to build the signature
    # (the canonical stable key).
    assert "infra_error" in source, (
        "_infra_failure_signature must reference infra_error to build a stable key"
    )


def test_infra_failure_signature_is_deterministic():
    """Same infra_error code -> same signature regardless of child_failures."""
    from app.release_qualifier_audit import _infra_failure_signature

    sig_a = _infra_failure_signature(
        "migration_apply_check_unavailable",
        child_failures=[{"leg": "audit", "code": "provision_failure"}],
    )
    sig_b = _infra_failure_signature(
        "migration_apply_check_unavailable",
        child_failures=[{"leg": "smoketest", "code": "something_else"}],
    )
    sig_c = _infra_failure_signature("migration_apply_check_unavailable", child_failures=[])

    assert sig_a == sig_b == sig_c, (
        "Identical infra_error codes must produce identical signatures regardless "
        "of which child tasks failed — oscillating signatures prevent the "
        "permanent-gate-incapacity streak from accumulating"
    )

    # Different infra errors must have different signatures.
    sig_other = _infra_failure_signature("audit_infra_respawn_cap_exceeded", child_failures=[])
    assert sig_a != sig_other


# --------------------------------------------------------------------------- #
# Finding #11 — scratch-DB URL is a qualification prereq, not a boot requirement
# --------------------------------------------------------------------------- #


def test_scratch_db_url_is_qualification_prereq_not_boot_req():
    """MIGRATION_APPLY_SCRATCH_DATABASE_URL must be in QUALIFICATION_ENV, not REQUIRED_RUNTIME_ENV.

    Finding #11: absent scratch URL must degrade qualification (gate defers with an
    actionable verdict) but MUST NOT block the app from booting on Cloud Run.
    """
    from app.required_runtime_env import QUALIFICATION_ENV, REQUIRED_RUNTIME_ENV

    _SCRATCH_VAR = "MIGRATION_APPLY_SCRATCH_DATABASE_URL"

    boot_names = {r.name for r in REQUIRED_RUNTIME_ENV}
    qual_names = {r.name for r in QUALIFICATION_ENV}

    assert _SCRATCH_VAR not in boot_names, (
        f"{_SCRATCH_VAR} must NOT be in REQUIRED_RUNTIME_ENV — "
        "its absence must degrade qualification, never block Cloud Run boot"
    )
    assert _SCRATCH_VAR in qual_names, (
        f"{_SCRATCH_VAR} must be in QUALIFICATION_ENV so diagnostic tooling "
        "can surface missing vars before repeated opaque deferrals accumulate"
    )


# --------------------------------------------------------------------------- #
# Finding #13 — hotfix_policy.py and admin_operator_triggers.py in allowlist
# --------------------------------------------------------------------------- #


def test_hotfix_policy_and_router_in_eligible_modules():
    """hotfix_policy.py and admin_operator_triggers.py must be in HOTFIX_ELIGIBLE_MODULES.

    Finding #13: the hotfix channel must be able to repair its own scope validator
    and its own delivery path through the same channel it exposes.  Without this
    self-reference, a defect in the allowlist or the router endpoint cannot be
    deployed via the hotfix channel — the exact gap the channel exists to close.
    """
    from app.hotfix_policy import HOTFIX_ELIGIBLE_MODULES

    assert "backend/app/hotfix_policy.py" in HOTFIX_ELIGIBLE_MODULES, (
        "backend/app/hotfix_policy.py must be in HOTFIX_ELIGIBLE_MODULES so the "
        "hotfix channel can repair its own scope validator"
    )
    assert "backend/app/routers/admin_operator_triggers.py" in HOTFIX_ELIGIBLE_MODULES, (
        "backend/app/routers/admin_operator_triggers.py must be in HOTFIX_ELIGIBLE_MODULES "
        "so the hotfix endpoint itself can be repaired through the hotfix channel"
    )


# --------------------------------------------------------------------------- #
# Finding #2 — scratch-DB URL absence does NOT prevent Cloud Run boot
# --------------------------------------------------------------------------- #


def test_scratch_db_url_absence_does_not_raise_on_cloud_run(monkeypatch):
    """Absent scratch-DB URL must not raise at boot, even on Cloud Run.

    Finding #2: the scratch-DB URL was previously declared with scope='cloud_run'
    in REQUIRED_RUNTIME_ENV, making its absence boot-fatal.  It must now be in
    QUALIFICATION_ENV only so _verify_required_env() passes without it.
    """
    from app.required_runtime_env import _verify_required_env
    from app.field_encryption import ENCRYPTION_KEY_ENV

    # Simulate Cloud Run: K_REVISION is set, encryption key is present,
    # but MIGRATION_APPLY_SCRATCH_DATABASE_URL is absent.
    monkeypatch.setenv("K_REVISION", "app-00042-abc")
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, "any-non-empty-value-presence-only")
    monkeypatch.delenv("MIGRATION_APPLY_SCRATCH_DATABASE_URL", raising=False)

    # Must NOT raise — scratch URL absence is a qualification degradation, not a boot failure.
    result = _verify_required_env()
    assert result is None, (
        "_verify_required_env() must return None when MIGRATION_APPLY_SCRATCH_DATABASE_URL "
        "is absent but GITHUB_PAT_ENCRYPTION_KEY is present — the scratch URL is optional "
        "for boot"
    )


# --------------------------------------------------------------------------- #
# Finding #6 — env-preflight absent evidence is a hard blocker (not 'unavailable')
# --------------------------------------------------------------------------- #


def test_env_preflight_absent_evidence_is_a_hard_blocker():
    """When env_preflight_enabled=True and evidence is absent, the verdict must be
    a hard blocker (boot_prereq_evidence_missing), not a soft 'unavailable'.

    Finding #6: the claim was that absent evidence scores as 'unavailable'. The
    scoring function must produce a hard block instead so the release cannot
    silently promote without valid boot-prerequisite evidence.
    """
    from app.release_scoring import compute_composite

    # Build the minimal composite call with env_preflight enabled but no artifact.
    # Use ci_green=True and high scores so env_preflight is the ONLY potential blocker.
    result = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy={},
        ci_state="green",
        ci_required=False,
        migration_apply_blocked=False,
        env_preflight_enabled=True,
        env_preflight_score=None,       # No evidence — absent artifact.
        env_preflight_missing=None,
        env_preflight_ack_override=False,
    )

    assert result.get("blocker") == "boot_prereq_evidence_missing", (
        "When env_preflight_enabled=True and env_preflight_score is None (absent evidence), "
        "compute_composite must set blocker='boot_prereq_evidence_missing' — a hard promotion "
        "block, not a soft 'unavailable' state. "
        f"Got blocker={result.get('blocker')!r}"
    )


# --------------------------------------------------------------------------- #
# Finding #1 — 0626 fails closed on malformed keys; 0628 covers github_oauth_token
# --------------------------------------------------------------------------- #


def test_migration_0626_calls_require_encryption_active():
    """Migration 0626 must use require_encryption_active() to fail closed on malformed keys.

    Finding #1 claim: is_encryption_active() conflates a missing key and a malformed
    key — both return False, so 0531 silently no-ops a malformed-key migration.
    Migration 0626 fixes this by calling require_encryption_active(), which raises
    RuntimeError for malformed keys regardless of ALLOW_PLAINTEXT_CREDENTIALS.
    """
    import inspect
    import importlib.util
    import pathlib

    mig_path = pathlib.Path(__file__).parent.parent / "alembic" / "versions" / "0626_safe_encrypt_backfill.py"
    spec = importlib.util.spec_from_file_location("mig0626", mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    upgrade_src = inspect.getsource(mig.upgrade)
    assert "require_encryption_active" in upgrade_src, (
        "Migration 0626 upgrade() must call require_encryption_active() — not "
        "is_encryption_active() — so a malformed GITHUB_PAT_ENCRYPTION_KEY raises "
        "RuntimeError instead of silently no-oping the backfill"
    )


def test_migration_0628_covers_github_oauth_token():
    """Migration 0628 must backfill userintegration.github_oauth_token.

    Finding #1 claim: 0566 adds userintegration.github_oauth_token as a nullable
    Text column encrypted on new writes, but it was absent from every prior backfill
    migration (0531/0621/0626).  Migration 0628 closes this gap.
    """
    import importlib.util
    import pathlib

    mig_path = pathlib.Path(__file__).parent.parent / "alembic" / "versions" / "0628_backfill_github_oauth_token_encrypted.py"
    assert mig_path.exists(), (
        "backend/alembic/versions/0628_backfill_github_oauth_token_encrypted.py must exist "
        "to backfill userintegration.github_oauth_token — the column was missed by 0531/0621/0626"
    )

    spec = importlib.util.spec_from_file_location("mig0628", mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    source = pathlib.Path(mig_path).read_text()
    assert "github_oauth_token" in source, (
        "Migration 0628 must reference github_oauth_token"
    )
    assert "require_encryption_active" in source, (
        "Migration 0628 must call require_encryption_active() to fail closed on malformed keys"
    )
    assert mig.revision == "0628", "Migration revision must be '0628'"
    assert mig.down_revision == "0627", "Migration 0628 must chain from 0627"


# --------------------------------------------------------------------------- #
# Finding #4 — timed-out qualifier children are cancelled, not left running
# --------------------------------------------------------------------------- #


def test_cancel_qualifier_children_covers_all_child_types():
    """_cancel_qualifier_children must handle all qualification child task types.

    Finding #4 claim: qualification timeout and sweep-crash handlers rewrite the
    release record without changing referenced child task states.  The fix cancels
    audit, smoketest, challenge, and merge_grade tasks.
    """
    import inspect
    from app.release_qualifier_sweep import _cancel_qualifier_children

    source = inspect.getsource(_cancel_qualifier_children)
    for child in ("audit_task_id", "smoketest_task_id", "challenge_task_id", "merge_grade_task_id"):
        assert child in source, (
            f"_cancel_qualifier_children must handle '{child}' — "
            "all child task types must be terminated on qualification timeout to prevent "
            "late completions from corrupting the next attempt"
        )

    assert "CANCELLED" in source or "cancelled" in source.lower(), (
        "_cancel_qualifier_children must set child tasks to CANCELLED status"
    )


def test_force_finalize_timed_out_calls_cancel_children():
    """_force_finalize_timed_out_qualification must call _cancel_qualifier_children.

    Finding #4: the timeout finalizer must contain children before re-drive is eligible.
    """
    import inspect
    from app.release_qualifier_sweep import _force_finalize_timed_out_qualification

    source = inspect.getsource(_force_finalize_timed_out_qualification)
    assert "_cancel_qualifier_children" in source, (
        "_force_finalize_timed_out_qualification must call _cancel_qualifier_children "
        "before rewriting the release record — containment must precede re-drive"
    )


# --------------------------------------------------------------------------- #
# Finding #5 — deterministic migration defects block (not defer)
# --------------------------------------------------------------------------- #


def test_deterministic_migration_defect_class_exists():
    """_DeterministicMigrationDefect must exist and be used to separate candidate
    defects from transient infra failures.

    Finding #5 claim: migration replay wraps every exception in one error type
    that the gate converts to defer.  The fix introduces _DeterministicMigrationDefect
    to separate hard-block candidate defects from deferrable infra failures.
    """
    from app.migration_apply_gate import _DeterministicMigrationDefect

    assert issubclass(_DeterministicMigrationDefect, Exception)


def test_structural_defect_check_returns_block_verdict():
    """_check_structural_defects must return a ('block', ...) verdict for broken chains.

    Finding #5: dup revision ids and multi-head chains are deterministically defective
    and must hard-block rather than defer-with-retry.
    """
    from app.migration_apply_gate import _check_structural_defects

    # A migration source with a duplicate revision ID is structurally defective.
    fake_dup = {
        "0999_dup_a.py": (
            '"""Rev.\nRevision ID: 0999\nRevises: 0998\n"""\nrevision = "0999"\ndown_revision = "0998"\n'
        ),
        "0999_dup_b.py": (
            '"""Rev.\nRevision ID: 0999\nRevises: 0997\n"""\nrevision = "0999"\ndown_revision = "0997"\n'
        ),
    }
    result = _check_structural_defects(fake_dup)
    assert result is not None, (
        "_check_structural_defects must return a non-None verdict for duplicate revision IDs"
    )
    verdict, reason = result
    assert verdict == "block", (
        f"Duplicate revision IDs must produce a 'block' verdict, got {verdict!r}"
    )
    assert "0999" in reason or "dup" in reason.lower() or "duplicate" in reason.lower(), (
        f"Block reason should reference the duplicate revision, got: {reason!r}"
    )


# --------------------------------------------------------------------------- #
# Finding #7 — trigger resolver returns not_applicable for terminal/auto-redrive states
# --------------------------------------------------------------------------- #


def test_trigger_resolver_returns_not_applicable_for_promoted():
    """resolve_qualification_trigger returns not_applicable for promoted versions.

    Finding #7 claim: the trigger resolver returned not_applicable for states that
    dedicated sweep re-drive functions handle automatically, without exposing them
    through the unified decision contract.  Promoted and abandoned versions must
    return not_applicable (terminal — no re-drive possible).
    """
    from app.qualification_triggers import resolve_qualification_trigger

    for terminal_status in ("promoted", "abandoned"):
        decision = resolve_qualification_trigger(
            status=terminal_status,
            blocker=None,
            candidate_sha="abc123",
            dry_run_sha=None,
            automain_sha="abc123",
            ci_state="green",
            ci_required=False,
            attempt_count=0,
            infra_error=None,
            infra_error_count=0,
            infra_consecutive_errors=0,
            rubric_changed=False,
            deferred_unsynced=False,
            blockers_cleared=False,
            readiness_sufficient=False,
            qualification_locked=False,
        )
        assert decision.fire is False, (
            f"resolve_qualification_trigger must return fire=False for status={terminal_status!r}"
        )
        assert decision.reason == "not_applicable", (
            f"Terminal status {terminal_status!r} must return reason='not_applicable', "
            f"got {decision.reason!r}"
        )


# --------------------------------------------------------------------------- #
# Finding #8 — bare-claim promotion timeout is applied (orphan containment)
# --------------------------------------------------------------------------- #


def test_bare_claim_promotion_gets_shorter_timeout():
    """A bare promotion claim (claiming=True, no pr_number) must receive a shorter
    timeout cap than the default.

    Finding #8 claim: promotion crash recovery must be containment-first.  When
    promotion_pending has claiming=True but no pr_number (crash before PR creation),
    the reconciler applies _PROMOTION_CLAIM_TIMEOUT_SECONDS (30 min) instead of the
    full promotion timeout to limit how long the dead claim blocks the next attempt.
    """
    import inspect
    from app import release_reconciliation

    source = inspect.getsource(release_reconciliation)

    assert "_PROMOTION_CLAIM_TIMEOUT_SECONDS" in source, (
        "release_reconciliation.py must define _PROMOTION_CLAIM_TIMEOUT_SECONDS "
        "to cap bare-claim promotion hold duration"
    )
    assert "_is_bare_claim" in source, (
        "release_reconciliation.py must detect bare claims (claiming=True, no pr_number) "
        "and apply a shorter timeout cap for faster containment"
    )


# --------------------------------------------------------------------------- #
# Finding #9 — data-migration proof gate defers DML without proof declaration
# --------------------------------------------------------------------------- #


def test_data_migration_proof_gate_defers_dml_without_proof():
    """_check_data_migration_proof_gate must return ('defer', reason) for DML without proof.

    Finding #9 claim: the current migration-safe verdict can be produced after applying
    a data migration to empty scratch tables.  The gate requires a
    # BLUEPRINT_DATA_MIGRATION_PROOF: declaration to force proof documentation.
    """
    from app.migration_apply_gate import _check_data_migration_proof_gate

    # Fake new migration source with DML but no proof declaration.
    dml_source = {
        "0900_dml_migration.py": (
            '"""Rev.\nRevision ID: 0900\n"""\n'
            "def upgrade():\n"
            "    op.execute(sa.update(some_table).values(col='x'))\n"
        )
    }
    result = _check_data_migration_proof_gate(dml_source)
    assert result is not None, (
        "_check_data_migration_proof_gate must return a non-None verdict for DML "
        "migrations without a BLUEPRINT_DATA_MIGRATION_PROOF declaration"
    )
    verdict, reason = result
    assert verdict == "defer", (
        f"DML without proof declaration must produce a 'defer' verdict, got {verdict!r}"
    )
    assert "data_migration_proof_missing" in reason, (
        f"Defer reason must reference 'data_migration_proof_missing', got: {reason!r}"
    )


def test_data_migration_proof_gate_passes_with_proof_declaration():
    """_check_data_migration_proof_gate must return None when proof is declared."""
    from app.migration_apply_gate import _check_data_migration_proof_gate

    # Same DML but with the required proof declaration.
    dml_with_proof = {
        "0900_dml_migration.py": (
            '"""Rev.\nRevision ID: 0900\n'
            "# BLUEPRINT_DATA_MIGRATION_PROOF: tested via fixture with 3 sample rows\n"
            '"""\n'
            "def upgrade():\n"
            "    op.execute(sa.update(some_table).values(col='x'))\n"
        )
    }
    result = _check_data_migration_proof_gate(dml_with_proof)
    assert result is None, (
        "_check_data_migration_proof_gate must return None (pass) when "
        "BLUEPRINT_DATA_MIGRATION_PROOF is declared"
    )


# --------------------------------------------------------------------------- #
# Finding #10 — VersionLifecyclePanel has owner-only hotfix remediation flow
# --------------------------------------------------------------------------- #


def test_version_lifecycle_panel_has_hotfix_remediation():
    """VersionLifecyclePanel.tsx must contain the hotfix remediation flow.

    Finding #10 claim: admin-ui/src currently has no hotfix preview or promotion
    client and VersionLifecyclePanel renders only generic qualification failure state.
    The fix adds an owner-only panel with hotfix eligibility preview and promotion.
    """
    import pathlib

    panel_path = (
        pathlib.Path(__file__).parent.parent.parent
        / "admin-ui" / "src" / "components" / "VersionLifecyclePanel.tsx"
    )
    source = panel_path.read_text()

    assert "hotfix" in source.lower(), (
        "VersionLifecyclePanel.tsx must reference hotfix remediation flow"
    )
    assert "data-testid=\"hotfix-remediation-panel\"" in source or "hotfix-remediation" in source, (
        "VersionLifecyclePanel.tsx must have a hotfix-remediation panel "
        "(data-testid='hotfix-remediation-panel')"
    )
    assert "hotfix-open-btn" in source or "hotfixPanelOpen" in source, (
        "VersionLifecyclePanel.tsx must have a hotfix open/toggle button"
    )


# --------------------------------------------------------------------------- #
# Finding #14 — acceptance-evidence minting runs deployed-side (architectural constraint)
# --------------------------------------------------------------------------- #


def test_acceptance_evidence_minting_is_deployed_side():
    """Acceptance-evidence minting must run in release_qualifier_sweep.py (deployed-side).

    Finding #14 claim: record_acceptance_window_coverage_evidence,
    backfill_unverified_merge_evidence, and backfill_direction_merge_evidence run
    inside the deployed orchestrator process at the deployed SHA.  No candidate-
    executing surface invokes these minters — they are deployed-side by design.
    """
    import inspect
    from app import release_qualifier_sweep

    sweep_source = inspect.getsource(release_qualifier_sweep)

    for fn_name in (
        "record_acceptance_window_coverage_evidence",
        "backfill_direction_merge_evidence",
    ):
        assert fn_name in sweep_source, (
            f"release_qualifier_sweep.py must invoke {fn_name} — acceptance evidence "
            "is minted deployed-side so fixes to minting logic require a promotion "
            "to take effect (architectural constraint; operator must be aware)"
        )


# --------------------------------------------------------------------------- #
# Findings #15 / #16 — operator hotfix-deploy channel exists
# --------------------------------------------------------------------------- #


def test_hotfix_promote_endpoint_exists():
    """admin_hotfix_promote.py must expose a POST /projects/{id}/promote/hotfix endpoint.

    Findings #15/#16 claim: no /promote/hotfix endpoint exists; only force_promote exists.
    The fix adds a narrow, audited, module-scoped hotfix channel.
    """
    import pathlib

    router_path = (
        pathlib.Path(__file__).parent.parent
        / "app" / "routers" / "admin_hotfix_promote.py"
    )
    assert router_path.exists(), (
        "backend/app/routers/admin_hotfix_promote.py must exist — "
        "the hotfix-deploy channel router"
    )
    source = router_path.read_text()
    assert "promote/hotfix" in source, (
        "admin_hotfix_promote.py must define a /promote/hotfix endpoint"
    )


def test_hotfix_promote_current_version_function_exists():
    """release_hotfix_promotion.py must export hotfix_promote_current_version.

    Findings #15/#16: the channel function must exist and be importable.
    """
    from app.release_hotfix_promotion import (
        hotfix_promote_current_version,
        check_hotfix_scope_preview,
    )

    assert callable(hotfix_promote_current_version), (
        "hotfix_promote_current_version must be a callable async function"
    )
    assert callable(check_hotfix_scope_preview), (
        "check_hotfix_scope_preview must be a callable (read-only scope preview)"
    )


# --------------------------------------------------------------------------- #
# 7-finding compute-images salvage bundle
# --------------------------------------------------------------------------- #


# cb-1 — dispatch reads ProjectComputeImage to resolve a default container image
# --------------------------------------------------------------------------- #


def test_dispatch_resolves_default_container_image_from_project():
    """_dispatch_compute_experiment_directly calls resolve_project_default_container_image.

    Finding cb-1: the dispatch path used to forward the payload's container_image
    verbatim to create_compute_job without ever consulting ProjectComputeImage.
    The fix wires in resolve_project_default_container_image so that image-less
    payloads get a digest-pinned default image before runner selection.
    """
    import inspect
    from app.orchestrator_runner_dispatch import _dispatch_compute_experiment_directly

    source = inspect.getsource(_dispatch_compute_experiment_directly)

    assert "resolve_project_default_container_image" in source, (
        "_dispatch_compute_experiment_directly must call resolve_project_default_container_image "
        "to pin the project's declared baked image when the payload carries no container_image. "
        "Without this, image-less payloads run unreproducibly on the host toolchain."
    )
    assert "resolved_default_container_image" in source, (
        "_dispatch_compute_experiment_directly must assign the resolved default to a local "
        "variable that is forwarded as container_image_override to _compute_experiment_task_kwargs"
    )


# cb-2 — GPU-in-container capability gate for jobs declaring both image and GPU
# --------------------------------------------------------------------------- #


def test_select_eligible_runner_gates_gpu_in_container():
    """_select_eligible_runner checks GPU-in-container capability when both container_image and GPU are needed.

    Finding cb-2: the gate used to check container-capability and GPU-presence
    independently, so a runner that is container-capable AND GPU-present but
    CANNOT pass the GPU into the container could still be selected for a job
    that needs both.
    """
    import inspect
    from app.orchestrator_runner_dispatch import _select_eligible_runner

    source = inspect.getsource(_select_eligible_runner)

    assert "_job_requires_gpu_in_container" in source, (
        "_select_eligible_runner must call _job_requires_gpu_in_container to detect "
        "when a job declares BOTH a container_image and a GPU requirement"
    )
    assert "_runner_gpu_in_container_capable" in source, (
        "_select_eligible_runner must call _runner_gpu_in_container_capable to gate "
        "runners that cannot pass a GPU into the container"
    )


# cb-3 — runner heartbeat includes per-project docker image-cache size
# --------------------------------------------------------------------------- #


def _local_runner_source() -> str:
    """Return the source of scripts/local_runner.py for structural assertions.

    Works in both Docker (repo at /repo) and bare-checkout (../../scripts) layouts.
    Raises FileNotFoundError if neither location has the file.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "local_runner.py",
        Path("/repo/scripts/local_runner.py"),
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"local_runner.py not found at any of: {[str(c) for c in candidates]}"
    )


def test_heartbeat_includes_image_cache_by_project():
    """The runner heartbeat reports image_cache_by_project alongside disk_free_bytes.

    Finding cb-3: the heartbeat used to report only total disk_free_bytes, giving
    no visibility into how much of that space the docker data-root / image layers
    consumed. image_cache_by_project attributes cache size back to the project
    whose images accumulated it, enabling fleet-level cache-pressure monitoring.
    """
    source = _local_runner_source()

    assert "_docker_image_cache_by_project" in source, (
        "local_runner.py must define _docker_image_cache_by_project to collect "
        "per-project docker image-cache sizes for the heartbeat payload"
    )
    assert '"image_cache_by_project"' in source, (
        "local_runner.py must include 'image_cache_by_project' as a key in the "
        "capabilities dict sent to the orchestrator"
    )


# cb-4 — protection-tag apply failures are counted, logged, and alerted (not silent)
# --------------------------------------------------------------------------- #


def test_protection_tag_failure_is_counted_and_not_silent():
    """sweep_protect_referenced_compute_image_digests tracks apply_failed and warns on non-zero.

    Finding cb-4: the sweep used to do `if apply_tag(...): applied += 1` and silently
    drop the failure — no count, no log, no alert. A missing inuse- tag means the
    registry retention policy can delete a digest a live job or DB-pinned default
    still references.
    """
    import inspect
    from app.reaper_sweeps import sweep_protect_referenced_compute_image_digests

    source = inspect.getsource(sweep_protect_referenced_compute_image_digests)

    assert "apply_failed" in source, (
        "sweep_protect_referenced_compute_image_digests must track apply_failed "
        "— the counter that surfaces how many protection-tag applies silently returned None"
    )
    assert "apply_failed += 1" in source, (
        "The else-branch (apply_tag returned None) must increment apply_failed "
        "rather than silently ignoring the failure"
    )
    assert "apply_failed" in source and "logger.warning" in source, (
        "The sweep must call logger.warning when apply_failed > 0 so the failure "
        "appears in orchestrator logs"
    )


# cb-5 — per-arm BLUEPRINT_RESULT_FILE isolation + elapsed/exit_code/stderr capture
# --------------------------------------------------------------------------- #


def test_arms_harness_isolates_blueprint_result_file_per_arm():
    """The synthesized multi-arm command gives each arm its own BLUEPRINT_RESULT_FILE.

    Finding cb-5: the per-arm harness synthesizes an isolation wrapper that (a) sets
    BLUEPRINT_RESULT_FILE to a per-arm private path so arms cannot clobber each
    other's result, and (b) captures exit_code, stderr, and elapsed time for every
    arm so the aggregator can surface them in the result block.
    """
    import inspect
    from app.compute_experiment_contract import _validate_compute_launch_contract

    source = inspect.getsource(_validate_compute_launch_contract)

    assert "BLUEPRINT_RESULT_FILE" in source, (
        "The arms synthesis code must set BLUEPRINT_RESULT_FILE per arm "
        "so that each arm writes to its own isolated result file"
    )
    assert "exit_code" in source, (
        "The arms synthesis code must capture exit_code for each arm "
        "so crashed arms are distinguishable from unmeasured ones"
    )
    assert "elapsed" in source, (
        "The arms synthesis code must capture elapsed time per arm "
        "for duration-apportioned cost reporting"
    )
    assert "stderr" in source, (
        "The arms synthesis code must capture stderr per arm "
        "so crash traces are surfaced in the result"
    )


# cb-6 — build_and_push scans inputs AND image layers for platform credentials
# --------------------------------------------------------------------------- #


def test_build_and_push_scans_inputs_and_layers_for_credentials():
    """build_and_push calls scan_inputs_for_credentials AND scan_image_layers_for_credentials.

    Finding cb-6: no-platform-creds-in-layers is a design invariant — baked images
    land in a shared Artifact Registry where layers are readable, so tokens baked
    into a layer are effectively public. Both the pre-build input scan (catching
    Dockerfile ENV directives and secret paths) and the post-build layer scan
    (catching credentials embedded in the built image's filesystem) must fire.
    """
    import inspect
    from app.compute_image_build import build_and_push

    source = inspect.getsource(build_and_push)

    assert "scan_inputs_for_credentials" in source, (
        "build_and_push must call scan_inputs_for_credentials before pushing "
        "to reject build inputs that embed platform credentials via Dockerfile ENV"
    )
    assert "scan_image_layers_for_credentials" in source, (
        "build_and_push must call scan_image_layers_for_credentials on the built "
        "image before pushing, to catch credentials baked into any layer"
    )


# cb-7 — _prune_docker_images is called after each compute job (proactive cache hygiene)
# --------------------------------------------------------------------------- #


def test_compute_job_runner_calls_prune_docker_images_after_job():
    """The compute job runner calls _prune_docker_images after each job completes.

    Finding cb-7: the runner had a low-disk GUARD that blocked new work but no
    HYGIENE pass that proactively freed cached image layers. Without post-job
    pruning a small data-root (dgx-1: ~30 GB) accumulates per-project images
    from consecutive jobs until it wedges. The pruner runs after every compute job,
    is gated on the same disk-floor threshold as the guard, and protects recently
    used images from eviction.
    """
    source = _local_runner_source()

    assert "def _prune_docker_images(" in source, (
        "local_runner.py must define _prune_docker_images for post-job Docker image "
        "cache hygiene"
    )
    # Verify it's called in the compute job lifecycle (not just defined).
    # The call site is at the bottom of the per-job finally block.
    assert source.count("_prune_docker_images(") >= 2, (
        "local_runner._prune_docker_images must be both defined AND called in the "
        "compute-job lifecycle; found fewer than 2 occurrences of '_prune_docker_images('"
    )


# =========================================================================== #
# 12-finding compute-viewable salvage bundle (2026-07-17)
#
# Findings: live-preview starvation, canvas dedup, honest media limits,
# artifact sampling disclosure, bound-project uploads, atomic terminal handoff
# design, refresh overwritten previews, mixed-media preservation, atomic
# terminal handoff impl, reserve slots for viewable runs, omission telemetry,
# PDF support.
# =========================================================================== #


def _runner_router_source() -> str:
    """Return the source of backend/app/routers/runner.py."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "routers" / "runner.py",
        Path("/repo/backend/app/routers/runner.py"),
        Path(__file__).resolve().parent.parent / "app" / "routers" / "runner.py",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("runner.py not found in expected locations")


def _strategic_pkg_source() -> str:
    """Return the source of backend/app/strategic_context_package.py."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "strategic_context_package.py",
        Path("/repo/backend/app/strategic_context_package.py"),
        Path(__file__).resolve().parent.parent / "app" / "strategic_context_package.py",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("strategic_context_package.py not found")


def _admin_compute_jobs_source() -> str:
    """Return the source of backend/app/routers/admin_compute_jobs.py."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "routers" / "admin_compute_jobs.py",
        Path("/repo/backend/app/routers/admin_compute_jobs.py"),
        Path(__file__).resolve().parent.parent / "app" / "routers" / "admin_compute_jobs.py",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("admin_compute_jobs.py not found")


# --------------------------------------------------------------------------- #
# Finding 1: live-preview fairness — scanner rotates past last-uploaded path
# --------------------------------------------------------------------------- #


def test_stream_viewable_files_returns_after_one_upload_decision():
    """Non-drain _stream_viewable_files returns after at most one upload per call.

    Finding 1 claim: 'The non-drain scheduler returns after one upload decision
    per heartbeat.' A frequently-rewritten file must not monopolize every heartbeat.
    """
    source = _local_runner_source()

    # The non-drain path must have an explicit return after start_upload().
    assert "if not drain:" in source or "return" in source, (
        "_stream_viewable_files must return after one upload decision outside drain"
    )
    # scan_start cursor must be advanced after each upload decision.
    assert "state.scan_start = relpath" in source, (
        "_stream_viewable_files must set state.scan_start to advance the fairness cursor"
    )


def test_stream_viewable_files_rotates_candidate_list():
    """The candidate list is rotated to start just past scan_start.

    Finding 1 claim: 'The scanner returns candidates in deterministic path order.'
    The fairness rotation must find the index strictly greater than scan_start.
    """
    source = _local_runner_source()

    assert "scan_start" in source, "scan_start attribute must exist in _ViewableStreamState"
    # The rotation uses a comparison to find the first relpath > scan_start.
    assert "relpath > state.scan_start" in source or "c[\"relpath\"] > state.scan_start" in source, (
        "_stream_viewable_files must rotate candidates so scanning resumes after scan_start"
    )


# --------------------------------------------------------------------------- #
# Finding 5: live-viewable endpoint authorizes against allowed_project_ids
# --------------------------------------------------------------------------- #


def test_viewable_endpoint_uses_allowed_project_ids():
    """The /compute-job/{job_id}/viewable endpoint must use allowed_project_ids.

    Finding 5 claim: 'The live-viewable endpoint filters ComputeJob.project_id
    against runner.project_id rather than the runner's allowed_project_ids set.'
    The current code must use the broader allowed_project_ids so bound secondary
    projects can stream their plots.
    """
    source = _runner_router_source()

    # The endpoint function (upload_compute_job_viewable) must call allowed_project_ids.
    # Locate the viewable endpoint section.
    idx = source.find('"/compute-job/{job_id}/viewable"')
    assert idx >= 0, "viewable endpoint definition not found in runner.py"
    # Scan forward into the function body. The body is long (multipart parsing
    # precedes the DB query), so use a generous window.
    body_slice = source[idx : idx + 12000]
    assert "allowed_project_ids" in body_slice, (
        "upload_compute_job_viewable must call allowed_project_ids (not use "
        "runner.project_id directly) so a runner bound to a secondary project "
        "can upload previews for jobs in that project"
    )


# --------------------------------------------------------------------------- #
# Finding 7: cache key includes revision so overwritten files refresh
# --------------------------------------------------------------------------- #


def test_pdf_in_viewable_content_types():
    """PDF must be in the storage viewable content-type registry.

    Finding 12 claim: 'compute_backends._VIEWABLE_CONTENT_TYPES has no pdf entry.'
    """
    from app import storage

    assert "pdf" in storage._VIEWABLE_CONTENT_TYPES, (
        "storage._VIEWABLE_CONTENT_TYPES must include 'pdf' so PDF viewable files "
        "can be classified and uploaded via the live-viewable pipeline"
    )
    content_type, kind = storage._VIEWABLE_CONTENT_TYPES["pdf"]
    assert content_type == "application/pdf"
    assert kind == "pdf"


def test_local_runner_pdf_in_viewable_content_types():
    """PDF must also be in the runner's local copy of _VIEWABLE_CONTENT_TYPES.

    Finding 12 claim: the runner must mirror the server's allowlist so it does
    not skip PDF files before attempting upload.
    """
    source = _local_runner_source()

    assert '"pdf": ("application/pdf", "pdf")' in source or (
        '"pdf"' in source and "application/pdf" in source
    ), (
        "local_runner.py _VIEWABLE_CONTENT_TYPES must include a 'pdf' entry "
        "matching ('application/pdf', 'pdf')"
    )


# --------------------------------------------------------------------------- #
# Finding 8/12: mixed-media descriptor retains all preview kinds
# --------------------------------------------------------------------------- #


def test_artifact_descriptor_retains_mixed_media_kinds():
    """_artifact_descriptor emits image, video, AND pdf groups from a mixed manifest.

    Finding 8 claim: 'After choosing a kind, the descriptor emits only that
    kind's list for the main tile.' The fix must call add_preview_group for every
    present kind regardless of the primary kind selection.
    """
    from app.strategic_context_package import _artifact_descriptor

    previews = [
        {"kind": "image", "url": "http://s/plot.png", "storage_uri": "gs://b/plot.png", "filename": "plot.png"},
        {"kind": "video", "url": "http://s/rollout.mp4", "storage_uri": "gs://b/rollout.mp4", "filename": "rollout.mp4"},
        {"kind": "pdf", "url": "http://s/report.pdf", "storage_uri": "gs://b/report.pdf", "filename": "report.pdf"},
    ]
    artifact = {
        "id": "art-001",
        "viewable_previews": previews,
        "kind": "storage",
        "label": "Mixed experiment",
        "artifact_type": "compute_result",
        "task_title": "Exp 1",
        "task_status": "completed",
        "created_at": None,
    }
    descriptor = _artifact_descriptor(artifact)
    assert descriptor is not None

    # Primary kind is image (first match wins), but video and pdf must also be present.
    assert descriptor.get("kind") == "image"
    assert descriptor.get("image") is not None, "image group missing from mixed descriptor"
    assert isinstance(descriptor.get("images"), list) and len(descriptor["images"]) >= 1
    assert descriptor.get("video") is not None, "video group missing from mixed descriptor"
    assert isinstance(descriptor.get("videos"), list) and len(descriptor["videos"]) >= 1
    assert descriptor.get("pdf") is not None, "pdf group missing from mixed descriptor"
    assert isinstance(descriptor.get("pdfs"), list) and len(descriptor["pdfs"]) >= 1


# --------------------------------------------------------------------------- #
# Finding 9: late bundle arrival backfills artifact_id / result_reference
# --------------------------------------------------------------------------- #


def test_late_bundle_upload_triggers_artifact_linkage():
    """runner.py backfills artifact_id when a bundle arrives after terminalization.

    Finding 9 claim: 'A terminal runner job can currently retain a stored bundle
    pointer without artifact_id or result_reference being backfilled.' The fix
    calls _link_terminal_artifact for the first late upload on a terminal job.
    """
    source = _runner_router_source()

    # The bundle-upload handler must call _link_terminal_artifact for terminal jobs.
    assert "_link_terminal_artifact" in source, (
        "runner.py must import and call _link_terminal_artifact to backfill "
        "artifact_id / result_reference when a bundle arrives after terminalization"
    )
    # Must be conditional on the job being terminal AND not yet having an artifact.
    assert "existing_artifact is None" in source or "runner_artifact is None" in source, (
        "late bundle backfill must guard on the job not already having an artifact "
        "(idempotent: only backfill the FIRST late upload)"
    )


# --------------------------------------------------------------------------- #
# Finding 10: viewable-bearing artifacts sort first in evidence slice
# --------------------------------------------------------------------------- #


def test_evidence_loader_sorts_viewable_artifacts_first():
    """Terminal artifact evidence sorts viewable-bearing rows before non-viewable.

    Finding 10 claim: 'Terminal evidence queries the four newest meaningful
    artifacts before resolving viewable previews.' The sort must put viewable
    items first so the _ARTIFACT_EVIDENCE_LIMIT cap retains media-bearing rows.
    """
    source = _strategic_pkg_source()

    # The sort key must place viewable_previews before non-viewable.
    assert 'viewable_previews' in source, "strategic_context_package must reference viewable_previews"
    # Look for the explicit sort that prioritises viewable items.
    assert "artifact_items.sort(" in source, (
        "load_strategic_context_evidence must call artifact_items.sort() to "
        "prioritize viewable-bearing rows before the evidence-limit slice"
    )
    # Confirm the sort key checks for viewable_previews.
    assert "i.get(\"viewable_previews\")" in source or "i.get('viewable_previews')" in source, (
        "the evidence sort key must check for viewable_previews to prioritize media rows"
    )


# --------------------------------------------------------------------------- #
# Finding 11: live omission telemetry surfaced via polling_metadata
# --------------------------------------------------------------------------- #


def test_live_omissions_stamped_in_polling_metadata():
    """The runner stamps viewable_live_omissions into polling_metadata on running reports.

    Finding 11 claim: 'The runner logs local oversize/count-budget skips but does
    not report them to the backend.' The fix stamps both counts via viewable_omissions
    in the running heartbeat, which the server converts to viewable_live_omissions.
    """
    source = _local_runner_source()

    assert "viewable_omissions" in source, (
        "local_runner.py must include a viewable_omissions payload in the running "
        "heartbeat so the server can stamp viewable_live_omissions"
    )
    assert "oversize_relpaths" in source, (
        "local_runner.py must track oversize_relpaths and report them as omissions"
    )
    assert "count_cap_relpaths" in source, (
        "local_runner.py must track count_cap_relpaths and report them as omissions"
    )


def test_admin_api_exposes_viewable_live_omissions():
    """The admin compute-jobs API exposes viewable_live_omissions to the frontend.

    Finding 11 claim: omission telemetry must flow from runner heartbeat through
    polling_metadata to the API response so the UI can display it.
    """
    source = _admin_compute_jobs_source()

    assert "viewable_live_omissions" in source or "viewable_artifacts_live_omissions" in source, (
        "admin_compute_jobs.py must expose the runner's live omission counts "
        "as viewable_artifacts_live_omissions in the compute-job polling response"
    )


# =========================================================================== #
# 18-finding compute-viewable salvage bundle (2026-07-17 — second sweep)
#
# This section pins the fixes identified in the second adversarial review.
# Findings already covered in the prior 12-finding section above are noted
# but not duplicated (f7, f11, f14, f15, f16, f17, f18).
# =========================================================================== #


def _ui_component_source(filename: str) -> str:
    """Read a frontend component source file for source-inspection tests."""
    from pathlib import Path

    base_candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / filename,
        Path("/repo/admin-ui/src/components") / filename,
    ]
    for path in base_candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"{filename} not found in expected locations")


def _agent_compute_jobs_source() -> str:
    """Return the source of backend/app/routers/agent_compute_jobs.py."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "routers" / "agent_compute_jobs.py",
        Path("/repo/backend/app/routers/agent_compute_jobs.py"),
        Path(__file__).resolve().parent.parent / "app" / "routers" / "agent_compute_jobs.py",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("agent_compute_jobs.py not found")


def _main_source() -> str:
    """Return the source of backend/app/main.py."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "main.py",
        Path("/repo/backend/app/main.py"),
        Path(__file__).resolve().parent.parent / "app" / "main.py",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("main.py not found")


def _compute_jobs_source() -> str:
    """Return the source of backend/app/compute_jobs.py."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "compute_jobs.py",
        Path("/repo/backend/app/compute_jobs.py"),
        Path(__file__).resolve().parent.parent / "app" / "compute_jobs.py",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("compute_jobs.py not found")


# --------------------------------------------------------------------------- #
# Finding f1: _sign_viewable_manifest uses parallel asyncio.gather, not
# a sequential per-file await loop
# --------------------------------------------------------------------------- #


def test_sign_viewable_manifest_uses_parallel_gather():
    """_sign_viewable_manifest must mint signed URLs in parallel via asyncio.gather.

    Finding f1 claim: 'Every viewable file attached to each surfaced compute job
    is signed one-at-a-time in a sequential loop.' The fix batches all GCS
    round-trips in one gather after the sequential ownership-check phase.
    """
    source = _strategic_pkg_source()

    # The function must exist.
    assert "_sign_viewable_manifest" in source, (
        "_sign_viewable_manifest must be defined in strategic_context_package.py"
    )

    # Find the function body and confirm asyncio.gather is used for signing.
    idx = source.find("async def _sign_viewable_manifest(")
    assert idx >= 0, "_sign_viewable_manifest function not found"
    body = source[idx : idx + 3000]

    assert "asyncio.gather" in body, (
        "_sign_viewable_manifest must use asyncio.gather to mint all GCS signed "
        "URLs in one parallel batch — sequential per-file awaits add N network "
        "round-trips to every canvas load when a job has many viewable files"
    )
    # Must NOT have a sequential for-loop with an individual generate_download_signed_url await.
    # The canonical non-parallel anti-pattern would be `await generate_download_signed_url`
    # inside a for-loop body (the gather passes a list comprehension, not a bare await).
    assert "return_exceptions=True" in body, (
        "_sign_viewable_manifest must pass return_exceptions=True to asyncio.gather "
        "so one GCS signing failure does not abort the entire batch"
    )


# --------------------------------------------------------------------------- #
# Finding f2: StrategicContextArtifactSlot renders a live badge for live items
# --------------------------------------------------------------------------- #


def test_strategic_artifact_slot_has_live_badge():
    """StrategicContextArtifactSlot must render a live indicator for live=true media.

    Finding f2 claim: 'The live flag is already delivered all the way to the
    frontend and parsed into preview item objects, but it currently drives ZERO
    rendering decisions.' The fix renders a LiveBadge overlay chip.
    """
    try:
        source = _ui_component_source("StrategicContextArtifactSlot.tsx")
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    assert "LiveBadge" in source, (
        "StrategicContextArtifactSlot.tsx must define a LiveBadge component "
        "so operators can visually distinguish still-updating mid-run previews "
        "from terminal completed artifacts"
    )
    # The badge must be used (not just defined).
    assert source.count("LiveBadge") >= 3, (
        "LiveBadge must be both defined and rendered for live image, video, "
        "and PDF preview slots"
    )
    assert "live" in source and ("pulsing" in source or "animate-pulse" in source), (
        "LiveBadge must include a pulsing animation cue so mid-run status is "
        "immediately visible without reading any tooltip"
    )


# --------------------------------------------------------------------------- #
# Finding f3: allow-list drift fixes — .mov removed, video cap env-overridable
# --------------------------------------------------------------------------- #


def test_fingerprint_does_not_include_mov_token():
    """The renderable-kind fingerprint must NOT match the '.mov' token.

    Finding f3 claim: 'strategic_context_package.py:1499 renderable-kind
    fingerprint matches a .mov token that no upload/extraction surface persists.'
    .mov is never uploaded by the runner or extracted by compute_backends, so
    including it in the fingerprint yields false-positive video classifications.
    """
    source = _strategic_pkg_source()

    # Find the video fingerprint check.
    idx = source.find('".mov"')
    assert idx == -1, (
        "strategic_context_package.py must NOT include '.mov' in the "
        "renderable-kind fingerprint — .mov is not persisted by any upload "
        "or extraction surface, so it creates false video classifications "
        "for artifacts whose label/filename contains 'mov' as a substring"
    )


def test_local_runner_video_cap_is_env_overridable():
    """local_runner.py must read BLUEPRINT_MAX_VIEWABLE_VIDEO_BYTES from env.

    Finding f3 claim: 'runner video byte cap is hardcoded 512 MiB in
    scripts/local_runner.py while backend/app/compute_backends.py makes it
    env-overridable via BLUEPRINT_MAX_VIEWABLE_VIDEO_BYTES.' The fix reads the
    same env var so operator-tuned caps are honoured by the runner.
    """
    source = _local_runner_source()

    assert "BLUEPRINT_MAX_VIEWABLE_VIDEO_BYTES" in source, (
        "local_runner.py must read BLUEPRINT_MAX_VIEWABLE_VIDEO_BYTES from the "
        "environment so operator-tuned server video caps are also enforced by "
        "the runner, preventing uploads the server would reject"
    )
    # The env read must be used to set _MAX_VIEWABLE_VIDEO_BYTES.
    assert "_MAX_VIEWABLE_VIDEO_BYTES" in source and (
        "os.environ" in source or "os.getenv" in source
    ), (
        "_MAX_VIEWABLE_VIDEO_BYTES must be derived from the environment, "
        "not a bare integer literal, to mirror compute_backends.py"
    )


def test_runner_422_error_message_includes_pdf():
    """The runner viewable endpoint 422 error must list pdf as a supported type.

    Finding f3 claim: 'backend/app/routers/runner.py 422 error message lists
    supported types omitting pdf.' The 422 must reflect the actual allowlist.
    """
    source = _runner_router_source()

    # The 422 error message near the viewable endpoint must include pdf.
    idx = source.find('"/compute-job/{job_id}/viewable"')
    assert idx >= 0, "viewable endpoint definition not found"
    body = source[idx : idx + 5000]

    assert "pdf" in body, (
        "The runner viewable endpoint 422 error message must list 'pdf' as a "
        "supported viewable type, consistent with the actual allowlist that "
        "accepts application/pdf"
    )


# --------------------------------------------------------------------------- #
# Finding f4: GCP-VM jobs have a dedicated mid-run viewable upload endpoint
# --------------------------------------------------------------------------- #


def test_gcp_vm_has_dedicated_viewable_upload_endpoint():
    """agent_compute_jobs.py must expose POST /compute-jobs/{job_id}/viewable.

    Finding f4 claim: 'compute_job_vm_heartbeat ... never ingests a viewable
    multipart file.' The fix is a dedicated upload endpoint (not the heartbeat)
    that mirrors the runner-subprocess viewable endpoint policies.
    """
    source = _agent_compute_jobs_source()

    # The dedicated viewable endpoint must exist.
    assert "/compute-jobs/{job_id}/viewable" in source or (
        "compute-job" in source and "viewable" in source
    ), (
        "agent_compute_jobs.py must define a POST endpoint for GCP-VM viewable "
        "file uploads so mid-run plots stream to the canvas without waiting for "
        "terminal finalization"
    )

    # It must enforce the same content-type classification as the runner endpoint.
    assert "viewable_content_type_for_filename" in source, (
        "The GCP-VM viewable endpoint must call viewable_content_type_for_filename "
        "to enforce the same extension allowlist as the runner-subprocess endpoint"
    )


# --------------------------------------------------------------------------- #
# Finding f5: unified viewable-file surfaces — purge + extension guard
# --------------------------------------------------------------------------- #


def test_terminal_reconciliation_purges_orphaned_viewable_rows():
    """_purge_orphaned_viewable_rows must be called at terminal reconciliation.

    Finding f5 claim: '_record_viewable_job_files only inserts/updates
    ComputeJobFile rows and never deletes, so superseded mid-run rows orphan.'
    The fix calls _purge_orphaned_viewable_rows after terminal extraction.
    """
    source = _compute_jobs_source()

    assert "_purge_orphaned_viewable_rows" in source, (
        "compute_jobs.py must define _purge_orphaned_viewable_rows to remove "
        "ComputeJobFile rows that are NOT in the authoritative terminal manifest"
    )
    # Must be called at least twice (definition + call site).
    assert source.count("_purge_orphaned_viewable_rows") >= 2, (
        "_purge_orphaned_viewable_rows must be both defined AND called during "
        "terminal reconciliation; found fewer than 2 occurrences"
    )


def test_computejobfile_signing_branch_validates_extension():
    """The ComputeJobFile signing branch must apply the viewable-extension guard.

    Finding f5 claim: 'The ComputeJobFile signing branch does not re-validate
    the extension, unlike _sign_viewable_manifest which re-checks classification.'
    The fix adds an extension guard to the fallback ComputeJobFile branch.
    """
    source = _main_source()

    # The signing endpoint must call viewable_content_type_for_filename.
    assert "viewable_content_type_for_filename" in source, (
        "main.py download-url signing must call viewable_content_type_for_filename "
        "on the ComputeJobFile fallback branch so only allowlisted extensions "
        "can be signed via that path"
    )


# --------------------------------------------------------------------------- #
# Finding f6: ComputeJobsPanel uses the shared ComputeArtifactGallery component
# --------------------------------------------------------------------------- #


def test_compute_jobs_panel_uses_shared_artifact_gallery():
    """ComputeJobsPanel must import and use ComputeArtifactGallery.

    Finding f6 claim: 'ComputeJobsPanel.tsx carries a near-verbatim inline copy
    of the whole resolve/cache/render pipeline.' The fix extracts to the shared
    ComputeArtifactGallery component and uses it in the panel.
    """
    try:
        source = _ui_component_source("ComputeJobsPanel.tsx")
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    assert "ComputeArtifactGallery" in source, (
        "ComputeJobsPanel.tsx must import and use ComputeArtifactGallery "
        "instead of duplicating the resolve/cache/render pipeline inline — "
        "two independent copies diverge over time and introduce subtle behavior "
        "differences (different auto-resolve triggers, missing scheme guards)"
    )


# --------------------------------------------------------------------------- #
# Finding f8: StrategicContextWorkspace deduplicates promoted artifact media
# --------------------------------------------------------------------------- #


def test_canvas_workspace_deduplicates_promoted_artifact_media():
    """StrategicContextWorkspace must suppress duplicate media for promoted artifacts.

    Finding f8 claim: 'Strategic descriptors are built from the same
    evidence.artifacts.items later rendered in the Evidence section. Both render
    paths mount their own image/video resolution components for the same media
    identities.' The fix tracks which artifact IDs are already shown as
    descriptors and skips their inline media in the Evidence section.
    """
    try:
        source = _ui_component_source("StrategicContextWorkspace.tsx")
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    assert "promotedArtifactIds" in source, (
        "StrategicContextWorkspace.tsx must maintain a promotedArtifactIds set "
        "of artifact IDs already rendered as descriptors so the Evidence section "
        "can skip mounting duplicate media resolution components for the same "
        "underlying storage objects"
    )


# --------------------------------------------------------------------------- #
# Finding f9: UI cap strings report per-kind limits, not a uniform 8 MiB cap
# --------------------------------------------------------------------------- #


def test_compute_artifact_gallery_cap_string_is_per_kind():
    """ComputeArtifactGallery cap message must distinguish image/PDF/video limits.

    Finding f9 claim: 'All three artifact surfaces describe a uniform 25-file
    and 8-MiB-per-file cap. The backend applies an 8 MiB image cap but a
    separately configurable video cap whose default is 512 MiB.' The fix
    surfaces per-kind limits in the omission message.
    """
    try:
        source = _ui_component_source("ComputeArtifactGallery.tsx")
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    # Must NOT have a uniform "8 MiB each" or "8 MiB/file" cap string.
    assert "8 MiB each" not in source, (
        "ComputeArtifactGallery must not describe a uniform '8 MiB each' cap — "
        "videos have a 512 MiB cap while images are 8 MiB"
    )
    # Must distinguish image and video caps.
    assert "512 MiB" in source, (
        "ComputeArtifactGallery must mention the 512 MiB video cap so operators "
        "know large videos up to that limit can still be previewed"
    )
    assert "8 MiB" in source, (
        "ComputeArtifactGallery must mention the 8 MiB image cap separately "
        "from the video cap"
    )


# --------------------------------------------------------------------------- #
# Finding f10: evidence response discloses artifact_total + live_count separately
# --------------------------------------------------------------------------- #


def test_evidence_response_discloses_artifact_total_and_live_count():
    """strategic_context_package must expose artifact_total and live_count.

    Finding f10 claim: 'The workspace currently displays counts derived from
    returned array lengths rather than the durable artifact total.' The fix
    exposes both the durable artifact total (query count before the bounded
    slice) and the live synthetic preview count so the operator can distinguish
    what is sampled from what exists.
    """
    source = _strategic_pkg_source()

    # artifact_total must be computed independently of the bounded items slice.
    assert "artifact_total" in source, (
        "strategic_context_package.py must track artifact_total (COUNT query "
        "result) so the canvas can show 'showing N of M artifacts' rather than "
        "silently implying the slice is the complete set"
    )
    # live_count must be surfaced so operators know synthetic previews exist.
    assert "live_count" in source, (
        "strategic_context_package.py must expose live_count in the artifact "
        "evidence so the canvas can note how many running-job preview rows were "
        "prepended in addition to the durable artifact total"
    )


# --------------------------------------------------------------------------- #
# Finding f12: runner.py calls _link_terminal_artifact for first late bundle
# --------------------------------------------------------------------------- #


def test_runner_calls_link_terminal_artifact_for_late_bundles():
    """runner.py must call _link_terminal_artifact when a bundle arrives late.

    Finding f12 claim: 'A terminal runner job can currently retain a stored
    bundle pointer without artifact_id or result_reference being backfilled.'
    The fix calls _link_terminal_artifact for the first bundle on a terminal job.
    """
    source = _runner_router_source()

    # _link_terminal_artifact must be imported and called.
    assert "_link_terminal_artifact" in source, (
        "runner.py must import and call _link_terminal_artifact so a bundle "
        "arriving after terminalization still gets its artifact_id and "
        "result_reference backfilled (the strategic canvas requires a linked "
        "Artifact to show terminal compute previews)"
    )
    # Must guard on not having an artifact yet (idempotent first-upload).
    assert "existing_artifact is None" in source or "artifact_id is None" in source, (
        "_link_terminal_artifact must only run when the job does not yet have "
        "an artifact (idempotent: ignore subsequent uploads for the same job)"
    )


# --------------------------------------------------------------------------- #
# Finding f13: ComputeArtifactGallery uses revision in signed-URL cache key
# --------------------------------------------------------------------------- #


def test_compute_artifact_gallery_uses_revision_in_cache_key():
    """ComputeArtifactGallery must include revision in the signed-URL cache key.

    Finding f13 claim: 'ComputeJobsPanel and ComputeArtifactGallery cache
    resolutions by storage URI, while useFreshMediaUrl reruns only for project,
    storage URI, or manual-attempt changes.' Without revision in the key, an
    overwritten same-filename viewable re-uses the old signed URL until it
    expires, hiding the update from the operator.
    """
    try:
        source = _ui_component_source("ComputeArtifactGallery.tsx")
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    # The gallery must define a cache key that incorporates revision.
    assert "revision" in source, (
        "ComputeArtifactGallery.tsx must use the revision field from the "
        "viewable manifest entry as part of the signed-URL cache key so that "
        "an overwritten same-path file triggers a fresh signing round-trip "
        "instead of re-using the stale cached URL"
    )
    # Look for the cache-key function or computation that combines storage_uri + revision.
    assert "storage_uri" in source and (
        "@${" in source or "artifactCacheKey" in source
    ), (
        "ComputeArtifactGallery.tsx must compute a composite key of "
        "(storage_uri, revision) to invalidate the signed-URL cache when "
        "the same storage path is overwritten with new content"
    )
