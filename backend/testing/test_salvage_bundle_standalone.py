"""Standalone smoke tests covering the 16-finding salvage bundle and the 7-finding
compute-images salvage bundle.

These tests run without a database (--standalone --no-postgres) and verify
properties that were identified in the adversarial-gate review cycle but can be
confirmed purely against source code or pure-Python logic.

Findings covered here (prior 16-finding bundle):
  #3  / #12 — infra-failure signature is NOT polluted by child-task failures
  #6        — env-preflight absent evidence is a hard blocker (not "unavailable")
  #11       — MIGRATION_APPLY_SCRATCH_DATABASE_URL is a qualification prereq, not a boot req
  #13       — hotfix_policy.py and admin_operator_triggers.py are in HOTFIX_ELIGIBLE_MODULES

Findings covered here (7-finding compute-images bundle):
  cb-1 — dispatch reads ProjectComputeImage to resolve default container image
  cb-2 — GPU-in-container capability is required for jobs declaring both container_image and GPU
  cb-3 — runner heartbeat includes per-project docker image-cache size
  cb-4 — registry protection-tag apply failures are counted, logged, and alerted (not silent)
  cb-5 — per-arm BLUEPRINT_RESULT_FILE paths are isolated; elapsed/exit_code/stderr captured
  cb-6 — build_and_push scans inputs AND image layers for platform credentials
  cb-7 — _prune_docker_images is called after each compute job (proactive cache hygiene)
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
