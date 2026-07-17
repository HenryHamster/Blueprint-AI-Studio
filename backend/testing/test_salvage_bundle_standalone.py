"""Standalone smoke tests covering the 16-finding salvage bundle.

These tests run without a database (--standalone --no-postgres) and verify
properties that were identified in the adversarial-gate review cycle but can be
confirmed purely against source code or pure-Python logic.

Findings covered here:
  #3  / #12 — infra-failure signature is NOT polluted by child-task failures
  #6        — env-preflight absent evidence is a hard blocker (not "unavailable")
  #11       — MIGRATION_APPLY_SCRATCH_DATABASE_URL is a qualification prereq, not a boot req
  #13       — hotfix_policy.py and admin_operator_triggers.py are in HOTFIX_ELIGIBLE_MODULES
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
