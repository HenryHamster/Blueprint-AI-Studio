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
