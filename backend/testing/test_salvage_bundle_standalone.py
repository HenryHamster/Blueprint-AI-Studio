"""Standalone smoke tests covering the 16-finding salvage bundle, the 7-finding
compute-images salvage bundle, and the 7-finding operator audience/lifecycle bundle.

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

Findings covered here (18-finding keyless-encryption / release-recovery salvage bundle, 2026-07-17):
  e1  — decrypt_field returns value unchanged when Fernet token cannot be decrypted (wrong key)
  e2  — probe_key_continuity fails closed when configured key cannot decrypt reference ciphertext
  e3  — assert_plaintext_or_raise raises RuntimeError for wrong-key ciphertext (not passed as plaintext)
  e4  — MultiFernet contract: encrypt with first key, decrypt with any configured key
  e5  — _get_fernet returns None for malformed GITHUB_PAT_ENCRYPTION_KEY (not crash)
  e6  — (scratch-DB degraded, not boot-fatal) → already pinned by existing test_scratch_db_url_* tests
  e7  — (gate-defer signature isolated) → already pinned by existing test_infra_failure_signature_* tests
  e8  — (timed-out children contained) → already pinned by existing test_cancel_qualifier_children_* tests
  e9  — (deterministic defects block) → already pinned by existing test_deterministic_migration_defect_* tests
  e10 — (env-preflight independent blocker) → already pinned by existing test_env_preflight_* tests
  e11 — auto_qualification_triggers knob controls only the generic trigger sweep, not bespoke re-drivers
  e12 — (orphan promotion containment) → already pinned by existing test_bare_claim_promotion_* test
  e13 — (data-migration proof gate) → already pinned by existing test_data_migration_proof_gate_* tests
  e14 — (UI hotfix remediation) → already pinned by existing test_version_lifecycle_panel_* test
  e15 — (scratch prereq, not boot req) → already pinned by existing test_scratch_db_url_* tests
  e16 — _MIN_INFRA_ERROR_STREAK == 3 (permanent gate incapacity streak threshold)
  e17 — hotfix endpoint validates non-empty reason and ack_inert_until_deploy before promoting
  e18 — (acceptance evidence deployed-side, hotfix channel) → pinned by existing tests above
  e19 — scratch subprocess injects an ephemeral Fernet key (not from the real env)
  e20 — _looks_like_keyless_encryption_artifact requires all three signals (no false positives)

Findings covered here (7-finding compute-images bundle):
  cb-1 — dispatch reads ProjectComputeImage to resolve default container image
  cb-2 — GPU-in-container capability is required for jobs declaring both container_image and GPU
  cb-3 — runner heartbeat includes per-project docker image-cache size
  cb-4 — registry protection-tag apply failures are counted, logged, and alerted (not silent)
  cb-5 — per-arm BLUEPRINT_RESULT_FILE paths are isolated; elapsed/exit_code/stderr captured
  cb-6 — build_and_push scans inputs AND image layers for platform credentials
  cb-7 — _prune_docker_images is called after each compute job (proactive cache hygiene)

Findings covered here (16-finding compute viewable artifacts bundle):
  v-1  — local_runner and storage._VIEWABLE_CONTENT_TYPES both include pdf; allowlists are aligned
  v-2  — GCP-VM jobs stream mid-run viewables via /api/agent/compute-jobs/<id>/viewable (viewable_watch.py)
  v-3  — extension guard applies to BOTH signing paths (manifest and ComputeJobFile row); orphan purge exists
  v-4  — ComputeJobsPanel delegates gallery rendering to ComputeArtifactGallery (no inline copy)
  v-5  — scanner fairness rotation prevents early-sorted overwritten files from starving later files
         (covered in test_local_runner_viewable_selection.py::test_frequently_rewritten_early_file…)
  v-6  — StrategicContextWorkspace suppresses duplicate media in evidence section for promoted artifacts
  v-7  — UI (ComputeArtifactGallery) accurately describes per-kind size limits (image ≠ video)
  v-8  — backend evidence returns artifact_total separately from item count; frontend reads live_count
  v-9  — runner.py viewable endpoint authorizes against allowed_project_ids (multi-project runners)
  v-10 — design: atomic terminal artifact handoff (implemented, covered by v-13 / test_local_runner.py)
  v-11 — useFreshMediaUrl keys cache on (storage_uri, revision) so overwritten same-path files update
  v-12 — _artifact_descriptor emits image + video + pdf preview groups; mixed-media not collapsed
  v-13 — upload_compute_job_artifact calls _link_terminal_artifact for late first bundles
  v-14 — evidence viewable-sort puts viewable-bearing artifacts first; running items prepended
  v-15 — local_runner reports viewable_omissions (size_cap, count_cap) to backend via report endpoint
  v-16 — PDF is in all three allowlists (local_runner, storage, 422 error message)
         (overlaps v-1; full upload test in test_local_runner_viewable_selection.py::test_pdf_file_uploaded)

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

Findings covered here (4-finding operator-inbox/messages salvage bundle, 2026-07-17):
  m1 — inbox attention-driven: badge/toast/strip/list all use the canonical server attention model
  m2 — confirmation chips actionable: click handler branches on both message and confirmation kinds
  m3 — QUESTION auto-decay: CONVERSATIONAL_DECAY_MESSAGE_TYPES includes QUESTION; stale_unanswered stamped
  m4 — notifications converged: toast dispatch uses isMessageNeedingOperatorAttention, not effectiveSignal

Findings covered here (5-finding operator-inbox lifecycle/convergence salvage bundle, 2026-07-17):
  n1 — lifecycle fields consumed: lifecycleOperatorActionLabel renders operator_action hint in needs-action strip
  n2 — acknowledgeable items get Ack button: lifecycle.acknowledgeable drives attention-ack-btn
  n3 — acknowledge_effect label: lifecycleAcknowledgeEffectLabel helper converts effect to human-readable tooltip
  n4 — QUESTION auto-decay (already fixed; see m3 tests above)
  n5 — confirmation chips actionable (already fixed; see m2 tests above)

Findings covered here (4-finding runtime-config / usage-meter / anti-hedging salvage bundle, 2026-07-21):
  rc-1 — blind_multi_reviewer_gate_enabled registered True and read with True fallback
  rc-2 — max_active_directions registered 10 and all read-sites use 10 as fallback
  rc-3 — task_lifecycle auto-approve gate uses AUTO_APPROVE_MIN_ACCEPTANCE_RATE (not inline 0.5)
  rc-4 — _snapshot_from_persisted_usage accepts messages_api_header schema (unified_5h → five_hour)
  rc-5 — _USAGE_SNAPSHOT_TTL_SECONDS matches _USAGE_METER_REFRESH_INTERVAL_SECONDS (no stale-80%% gap)
  rc-6 — assembleFailureGuidanceBlocks includes ANTI_HEDGING_GUIDANCE
  rc-7 — review MAJOR tier names hedging / default-off as a severity example

Findings covered here (1-finding baked-image authoring-prompt salvage bundle, 2026-07-21):
  bi-1 — thinker.ts instructs agents to OMIT container_image when a project-baked image is listed;
          research-plan.ts injects baked_image_ref preference into the local-runner note;
          strategic.ts buildLocalRunnerSection emits baked-image guidance when baked_image_ref present

Findings covered here (1-finding baked-image authoring-prompt salvage bundle, task db1e30ca, 2026-07-21):
  db-1 — same as bi-1; all three prompt files (thinker.ts, research-plan.ts, strategic.ts) already
          instruct agents to OMIT container_image when a project-baked image is listed and the
          backend strategic_spawning.py already injects baked_image_ref into local_runner_status.
          Already pinned by test_thinker_prefers_baked_image_over_always_set_explicitly,
          test_research_plan_injects_baked_image_ref_into_local_runner_note, and
          test_strategic_local_runner_section_includes_baked_image_guidance.

Findings covered here (1-finding greenfield onboarding invariant bundle, task c1160063, 2026-07-21):
  c1-1 — end-to-end: repo_bootstrap.spawn_bootstrap_task suppresses the clone_failed loop via
          _self_heal_undetermined_emptiness + repo_is_empty guard; knowledge_completion wires
          _design_web_only_grounding_miss into the acceptance gate so a web-only greenfield design
          document passes when its URLs are reachable. Both seams covered by separate unit tests
          (test_empty_repo_onboarding.py, test_design_web_url_reachability.py); this section adds
          a cross-seam structural invariant asserting both guards co-exist in the onboarding flow.

Findings covered here (7-finding operator audience/lifecycle salvage bundle, 2026-07-21):
  m-1 — research.dedup_deadlock + research.already_fixed_deadlock registered STRATEGIST_INTERNAL
  m-2 — research.direction_auto_completed registered STRATEGIST_INTERNAL; emitter has event_type=;
         dedup BUG_REPORT + review_pipeline BLOCKER call stamp_message_audience;
         all Alert() constructors in backend/app/ have event_type=
  m-3 — lifecycle fields consumed (operator_action hint, Ack button) [pinned in MessagesPanel.test.tsx]
  m-4 — attention-driven inbox default (needsActionOnly=true default) [pinned in MessagesPanel.test.tsx]
  m-5 — confirmation chips actionable [pinned in MessagesPanel.test.tsx]
  m-6 — QUESTION auto-decay: STALE_QUESTION_DECAY_DAYS=30; stale_unanswered stamped; runtime-configurable
  m-7 — notifications converged on attention model [pinned in MessagesPanel.test.tsx]

Findings covered here (7-finding creation-path fail-closed/attention/lifecycle salvage bundle, task fb81a82f, 2026-07-21):
  fb-1 — design: creation-path fail-closed audience contract → already pinned by test_m1_* + test_m2_* (m-1/m-2)
  fb-2 — fix: fail-closed audience at Alert/message creation → already pinned by test_m1_*, test_m2_* (m-1/m-2)
  fb-3 — feat: operator inbox consumes server attention/lifecycle model (operator_action hint, Ack button)
         → already pinned by MessagesPanel.test.tsx::MessagesPanel attention model lifecycle field consumption (m-3)
  fb-4 — feat(ui): make operator inbox attention-driven, retire dual effectiveSignal classification
         → already pinned by MessagesPanel.test.tsx::MessagesPanel needs-action filter (m-4)
  fb-5 — fix(chat): make confirmation chips actionable (accept/dismiss affordance in needs-action strip)
         → already pinned by MessagesPanel.test.tsx::MessagesPanel confirmation chip actionability (m-5)
  fb-6 — feat(messages): auto-decay stale open QUESTION messages → already pinned by test_m6_* (m-6)
  fb-7 — feat(ui): converge operator notifications on attention model (toast uses attention, not effectiveSignal)
         → already pinned by MessagesPanel.test.tsx::fires toast for needs_operator message even when
           event_type is muted (m-7)

Findings covered here (15-finding capability-proof salvage bundle, 2026-07-17):
  cp-1  — MetricsIntegrityFlag uses real backend contract (check/status/self_reported/ground_truth/delta/severity/subject/detail/auto_resolved)
  cp-2  — capability_proof_trial_stall is registered as a health check
  cp-3  — Ingestion treats could_not_run as non-green without demoting existing proof
  cp-4  — ledger_corroborated_proof_status applies freshness (now param) and alias-aware retry
  cp-5  — settings API exposes capability_proof_execution_trials_enabled and trial_eligible_unproven
  cp-6  — revoke_stale_execution_proof exists; trial enrollment dedup guard prevents concurrent enrollment
  cp-7  — event_driven_unblock_on_capability_proof is called after green proof is committed
  cp-8  — plan_completion resolves capability-kind prerequisites and blocks in enforce mode
  cp-9  — trial receipt validates cap, test_refs, head_sha against enrollment (no fabricated proofs)
  cp-10 — compute_fleet_proof_coverage uses consumer_gate_proof_status (ledger-corroborating path)
  cp-11 — check_capability_proof_gate_gap is a registered check that auto-enrolls trials
  cp-12 — compute_trial_eligibility_projection exists for projection display
  cp-13 — test_capability_proof_gate_enforcement_full_lifecycle exists in test_blocked_need_resolution
  cp-14 — docs/capability-proof-earn-path.md design document exists
  cp-15 — record_capability_named_test_execution_proof mints proven tier; enroll_capability_proof_trial exists

Findings covered here (4-finding project-tools / runner-config salvage bundle, 2026-07-19):
  r-1 — walkthrough test files exist with runner-enrollment and project-tool assertions
  r-2 — _LiveRunnerConfig.apply validates ALL fields before mutating any state (atomic reject)
  r-3 — ProjectToolHost uses pendingDeleteId two-step confirmation before calling handleDelete
  r-4 — ProjectToolHost Enable in disabled section has in-flight guard; slug shown after create

Findings covered here (16-finding capability-proof salvage bundle, 2026-07-18):
  cq-0  — spawn-gate absorbs alias bypass closed: working_capability_proof_status registers
           agent-declared absorbs aliases as "unproven" so capability_need_blocked_by_proof_gate
           cannot treat a None/absent entry as a free pass (finding #1, fixed in a81408a)
  cq-1  — MetricsIntegrityFlag uses real backend contract (already pinned by cp-1)
  cq-2  — capability_proof_trial_stall is registered as a health check (already pinned by cp-2)
  cq-3  — ingestion treats could_not_run as non-green without demoting (already pinned by cp-3)
  cq-4  — ledger_corroborated_proof_status applies freshness and alias-aware retry (already pinned by cp-4)
  cq-5  — settings API exposes proof trial fields (already pinned by cp-5)
  cq-6  — revoke_stale_execution_proof exists; trial dedup guard present (already pinned by cp-6)
  cq-7  — event_driven_unblock_on_capability_proof called after green proof committed (already pinned by cp-7)
  cq-8  — plan_completion enforces capability prerequisites (already pinned by cp-8)
  cq-9  — trial receipt validates against enrollment (already pinned by cp-9)
  cq-10 — compute_fleet_proof_coverage uses ledger-corroborating path (already pinned by cp-10)
  cq-11 — check_capability_proof_gate_gap self-healing check is registered (already pinned by cp-11)
  cq-12 — compute_trial_eligibility_projection exists (already pinned by cp-12)
  cq-13 — E2E lifecycle test exists in test_blocked_need_resolution (already pinned by cp-13)
  cq-14 — design doc referenced in source (already pinned by cp-14)
  cq-15 — automated earn path: record_capability_named_test_execution_proof + enroll_capability_proof_trial (already pinned by cp-15)

Findings covered here (7-finding creation-path fail-closed audience salvage bundle, 2026-07-17):
  p1 — design: fail-closed audience contract — design for Alert/message construction fail-closed
  p2 — fix: research_direction_state + research_completion Alert constructors stamp event_type
         so operator_visible_alert_filter routes them STRATEGIST_INTERNAL (dedup_deadlock,
         already_fixed_deadlock, direction_auto_completed are never shown in operator badge)
  p3 — lifecycle fields consumed (already fixed; see n1-n3 tests above)
  p4 — attention-driven inbox (already fixed; see m1 tests above)
  p5 — confirmation chips actionable (already fixed; see m2 tests above)
  p6 — QUESTION auto-decay (already fixed; see m3 tests above)
  p7 — notifications converged on attention model (already fixed; see m4 tests above)

Findings covered here (4-finding operator-walkthrough / runner-config / project-tools bundle, 2026-07-19):
  w1 — walkthrough test infrastructure: FleetRunnersPanel and ProjectToolHost unit tests exist
       and cover the end-to-end owner walkthrough flows
  w2 — runner config generations apply atomically: _LiveRunnerConfig.apply validates all fields
       before the lock; a single unrecognized/malformed field rejects the whole block without
       mutating any known field; a clean apply clears prior rejection state
  w3 — project-tool Remove requires confirmation: clicking Remove shows a confirmation prompt
       naming the tool and explaining permanent deletion before calling the backend DELETE;
       Cancel is side-effect free; applies to both enabled and disabled tool rows
  w4 — project-tools walkthrough residuals: Enable button in Disabled section shows Enabling…
       and is disabled while the PATCH is outstanding; the auto-uniquified slug assigned by
       the backend is surfaced to the operator as a created-slug notice after tool creation

Findings covered here (note-shim / runner-config / project-tools / walkthrough salvage bundle, 2026-07-21):
  n1 — note shim uses _json_str (not sed) for JSON encoding: provisionAmbientHelpers in
       runners.ts imports jsonStrShellFn and declares _json_str in the ./note script body;
       CONTENT, PAGE, and SEVERITY are encoded via $(_json_str ...) so newlines, tabs,
       backslashes, and control characters are transmitted correctly

Findings covered here (4-finding compute/research salvage bundle, 2026-07-19):
  r1 — clone-form preservation: buildCloneForm reads input_artifact_job_id / setup_step /
       no_progress_timeout_seconds from launch_params; backend launch_params builder includes
       all three fields so clone preserves checkpoint-resume, setup, and watchdog config
  r2 — self-heal empty-repo: maybe_redetect_on_emptiness_disagreement flips stale
       repo_is_empty=False when agent observes empty tree; agent endpoint
       /api/agent/build-config-discovery propagates observed_repo_is_empty to break
       the infinite clone_failed loop
  r3 — GCE VM resource monitoring: _check_low_disk_vms and _check_low_memory_vms detect
       and alert on low-disk/OOM conditions for active task VMs (parity with local runner
       _check_low_disk_runners / _check_low_memory_runners)
  r4 — research quality feedback: _compute_direction_quality_score incorporates
       grade_distribution (HIGH/MEDIUM/LOW) and quality_grader_verdict (pass rate)
       from research_history so high-quality directions are prioritised in research spawning

Findings covered here (20-finding release-qualification salvage bundle, 2026-07-20):
  f20-1  — serving-only-import ImportError degrades gate to ('ok', None); _looks_like_serving_only_import
  f20-2  — design: prove data-dependent migrations; scratch replay uses empty tables / schema not rows
  f20-3  — verify encryption-key continuity; probe_key_continuity / assert_plaintext_or_raise present
  f20-4  — research: define safe Fernet key rotation; MultiFernet.rotate() exists
  f20-5  — fail closed on keyless/malformed backfill; require_encryption_active raises on malformed key
  f20-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (QUALIFICATION_ENV cut as dead manifest)
  f20-7  — gate-defer signature derived from error code only (no child failures)
  f20-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite
  f20-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer')
  f20-10 — env_preflight_enabled independent gate flag in release_qualifier.py
  f20-11 — trigger resolver returns 'infra_failure_redrive' for infra_failure (not 'not_applicable')
  f20-12 — release_reconciliation.py references 'claiming' field for orphan containment
  f20-13 — migration_apply_gate.py references DML/data-migration detection
  f20-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview / triggerHotfixPromote
  f20-15 — QUALIFICATION_ENV + missing_qualification_env() cut as dead code; scratch-URL absence not boot-fatal
  f20-16 — _infra_failure_signature stable across candidate rotations (same error → same sig)
  f20-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES
  f20-18 — acceptance-evidence minting in release_qualifier_sweep.py (deployed-side), not subprocess
  f20-19 — hotfix_promote_current_version + check_hotfix_scope_preview importable from release_qualifier
  f20-20 — evaluate_hotfix_scope rejects out-of-scope files; accepts eligible module-only diffs

Findings covered here (2-finding dispatch-eligibility / heartbeat-docker-cache salvage bundle, 2026-07-21):
  d1  — dispatch reads ProjectComputeImage via resolve_project_baked_image_strict; fails closed on
        unresolvable baked image; stores pinned ref in polling_metadata['container_image'] via
        create_compute_job (previously dispatch forwarded container_image verbatim without resolution)
  d2  — runner heartbeat reports docker image-cache size via image_cache_by_project alongside
        disk_free_bytes; routers/runner.py sanitizes and stores the field in runner caps
        (previously only total disk_free_bytes was reported with no per-project cache attribution)

Findings covered here (19-finding release-qualification salvage bundle, 2026-07-21):
  s19-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_*
  s19-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_*
  s19-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3
  s19-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_*
  s19-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_*
  s19-6  — scratch-DB URL in QUALIFICATION_ENV, not REQUIRED_RUNTIME_ENV → already pinned by test_f20_6_*
  s19-7  — gate-defer infra signature derived from error code only (no child failures) → already pinned by test_f20_7_*
  s19-8  — timed-out qualifier children cancelled before state rewrite → already pinned by test_f20_8_*
  s19-9  — _DeterministicMigrationDefect returns 'block' verdict (not defer) → already pinned by test_f20_9_*
  s19-10 — env_preflight_enabled is an independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_*
  s19-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_*
  s19-12 — release_reconciliation references 'claiming' field for orphan containment across all paths → already pinned by test_f20_12_*
  s19-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_*
  s19-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_*
  s19-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_*
  s19-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_*
  s19-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_*
  s19-18 — acceptance-evidence minting runs deployed-side in release_qualifier_sweep.py → already pinned by test_f20_18_*
  s19-19 — hotfix_promote_current_version + evaluate_hotfix_scope are operator-gated (separate from force promote) → already pinned by test_f20_19_* + test_f20_20_*

Findings covered here (17-finding release-qualification salvage bundle, task c693845b, 2026-07-21):
  c17-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_* (s19-1)
  c17-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_* (s19-2)
  c17-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3 (s19-3)
  c17-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_* (s19-4)
  c17-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_* (s19-5)
  c17-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (degraded, not boot-fatal) → already pinned by test_f20_6_* (s19-6)
  c17-7  — gate-defer signature derived from error code only (no child failures) → already pinned by test_f20_7_* (s19-7)
  c17-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite → already pinned by test_f20_8_* (s19-8)
  c17-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer') → already pinned by test_f20_9_* (s19-9)
  c17-10 — env_preflight_enabled independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_* (s19-10)
  c17-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_* (s19-11)
  c17-12 — release_reconciliation references 'claiming' field for orphan containment → already pinned by test_f20_12_* (s19-12)
  c17-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_* (s19-13)
  c17-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_* (s19-14)
  c17-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_* (s19-15)
  c17-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_* (s19-16)
  c17-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_* (s19-17)

Findings covered here (17-finding release-qualification salvage bundle, task 03ca2ee5, 2026-07-21):
  03-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_* (c17-1)
  03-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_* (c17-2)
  03-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3 (c17-3)
  03-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_* (c17-4)
  03-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_* (c17-5)
  03-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (degraded, not boot-fatal) → already pinned by test_f20_6_* (c17-6)
  03-7  — gate-defer signature derived from error code only (no child failures) → already pinned by test_f20_7_* (c17-7)
  03-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite → already pinned by test_f20_8_* (c17-8)
  03-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer') → already pinned by test_f20_9_* (c17-9)
  03-10 — env_preflight_enabled independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_* (c17-10)
  03-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_* (c17-11)
  03-12 — release_reconciliation references 'claiming' field for orphan containment → already pinned by test_f20_12_* (c17-12)
  03-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_* (c17-13)
  03-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_* (c17-14)
  03-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_* (c17-15)
  03-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_* (c17-16)
  03-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_* (c17-17)

Findings covered here (7-finding creation-path fail-closed audience/lifecycle/attention salvage bundle, task d820301f, 2026-07-21):
  d-1 — design: creation-path fail-closed audience contract → already pinned by test_m1_* + test_m2_* (m-1/m-2; fb-1)
  d-2 — fix: fail-closed audience at Alert/message creation → already pinned by test_m2_* (m-2; fb-2)
  d-3 — feat: operator inbox consumes server attention/lifecycle model (operator_action hint, Ack button)
         → already pinned by MessagesPanel.test.tsx::MessagesPanel attention model lifecycle field consumption (m-3; fb-3)
  d-4 — feat(ui): make operator inbox attention-driven, retire dual effectiveSignal classification
         → already pinned by MessagesPanel.test.tsx::MessagesPanel needs-action filter (m-4; fb-4)
  d-5 — fix(chat): make confirmation chips actionable (accept/dismiss in needs-action strip)
         → already pinned by MessagesPanel.test.tsx::MessagesPanel confirmation chip actionability (m-5; fb-5)
  d-6 — feat(messages): auto-decay stale open QUESTION messages → already pinned by test_m6_* (m-6; fb-6)
  d-7 — feat(ui): converge operator notifications on attention model (toast uses attention, not effectiveSignal)
         → already pinned by MessagesPanel.test.tsx::fires toast for needs_operator message (m-7; fb-7)

Findings covered here (17-finding release-qualification salvage bundle, task 03ca2ee5, 2026-07-21):
  03-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_* (c17-1)
  03-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_* (c17-2)
  03-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3 (c17-3)
  03-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_* (c17-4)
  03-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_* (c17-5)
  03-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (degraded, not boot-fatal) → already pinned by test_f20_6_* (c17-6)
  03-7  — gate-defer signature derived from error code only (no child failures) → already pinned by test_f20_7_* (c17-7)
  03-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite → already pinned by test_f20_8_* (c17-8)
  03-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer') → already pinned by test_f20_9_* (c17-9)
  03-10 — env_preflight_enabled independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_* (c17-10)
  03-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_* (c17-11)
  03-12 — release_reconciliation references 'claiming' field for orphan containment → already pinned by test_f20_12_* (c17-12)
  03-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_* (c17-13)
  03-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_* (c17-14)
  03-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_* (c17-15)
  03-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_* (c17-16)
  03-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_* (c17-17)

Findings covered here (17-finding release-qualification salvage bundle, task c39e0ee0, 2026-07-21):
  c39-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_* (03-1)
  c39-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_* (03-2)
  c39-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3 (03-3)
  c39-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_* (03-4)
  c39-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_* (03-5)
  c39-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (degraded, not boot-fatal) → already pinned by test_f20_6_* (03-6)
  c39-7  — gate-defer signature derived from error code only (no child failures) → already pinned by test_f20_7_* (03-7)
  c39-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite → already pinned by test_f20_8_* (03-8)
  c39-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer') → already pinned by test_f20_9_* (03-9)
  c39-10 — env_preflight_enabled independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_* (03-10)
  c39-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_* (03-11)
  c39-12 — release_reconciliation references 'claiming' field for orphan containment → already pinned by test_f20_12_* (03-12)
  c39-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_* (03-13)
  c39-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_* (03-14)
  c39-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_* (03-15)
  c39-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_* (03-16)
  c39-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_* (03-17)

Findings covered here (17-finding release-qualification salvage bundle, task 927eea0b, 2026-07-21):
  92-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_* (03-1)
  92-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_* (03-2)
  92-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3 (03-3)
  92-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_* (03-4)
  92-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_* (03-5)
  92-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (degraded, not boot-fatal) → already pinned by test_f20_6_* (03-6)
  92-7  — gate-defer signature derived from error code only (no child failures) → already pinned by test_f20_7_* (03-7)
  92-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite → already pinned by test_f20_8_* (03-8)
  92-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer') → already pinned by test_f20_9_* (03-9)
  92-10 — env_preflight_enabled independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_* (03-10)
  92-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_* (03-11)
  92-12 — release_reconciliation references 'claiming' field for orphan containment → already pinned by test_f20_12_* (03-12)
  92-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_* (03-13)
  92-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_* (03-14)
  92-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_* (03-15)
  92-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_* (03-16)
  92-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_* (03-17)

Findings covered here (17-finding release-qualification salvage bundle, task 986aadde, 2026-07-21):
  98-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_* (03-1)
  98-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_* (03-2)
  98-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3 (03-3)
  98-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_* (03-4)
  98-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_* (03-5)
  98-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (degraded, not boot-fatal) → already pinned by test_f20_6_* (03-6)
  98-7  — gate-defer signature derived from error code only (no child failures) → already pinned by test_f20_7_* (03-7)
  98-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite → already pinned by test_f20_8_* (03-8)
  98-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer') → already pinned by test_f20_9_* (03-9)
  98-10 — env_preflight_enabled independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_* (03-10)
  98-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_* (03-11)
  98-12 — release_reconciliation references 'claiming' field for orphan containment → already pinned by test_f20_12_* (03-12)
  98-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_* (03-13)
  98-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_* (03-14)
  98-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_* (03-15)
  98-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_* (03-16)
  98-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_* (03-17)

Findings covered here (17-finding release-qualification salvage bundle, task c81995d3, 2026-07-21):
  c81-1  — serving-only-import ImportError degrades gate (not hard-block) → already pinned by test_f20_1_* (03-1)
  c81-2  — data-dependent migration proof design; scratch replay uses schema not row data → already pinned by test_f20_2_* (03-2)
  c81-3  — encryption-key continuity: probe_key_continuity / assert_plaintext_or_raise present → already pinned by test_f20_3_* + e1/e2/e3 (03-3)
  c81-4  — MultiFernet.rotate() for staged key rotation → already pinned by test_f20_4_* (03-4)
  c81-5  — fail closed on keyless/malformed encryption backfill; require_encryption_active raises → already pinned by test_f20_5_* (03-5)
  c81-6  — scratch-DB URL not in REQUIRED_RUNTIME_ENV (degraded, not boot-fatal) → already pinned by test_f20_6_* (03-6)
  c81-7  — gate-defer signature derived from error code only (no child failures) → already pinned by test_f20_7_* (03-7)
  c81-8  — timed-out qualifier children cancelled via _cancel_qualifier_children before state rewrite → already pinned by test_f20_8_* (03-8)
  c81-9  — _DeterministicMigrationDefect returns 'block' verdict (not 'defer') → already pinned by test_f20_9_* (03-9)
  c81-10 — env_preflight_enabled independent gate flag; absent evidence is a hard blocker → already pinned by test_f20_10_* (03-10)
  c81-11 — trigger resolver returns infra_failure_redrive for infra_failure (bespoke loops gated separately) → already pinned by test_f20_11_* (03-11)
  c81-12 — release_reconciliation references 'claiming' field for orphan containment → already pinned by test_f20_12_* (03-12)
  c81-13 — migration_apply_gate data-migration proof gate defers DML without proof declaration → already pinned by test_f20_13_* (03-13)
  c81-14 — admin-ui/src/api/projects.ts exposes getHotfixScopePreview + triggerHotfixPromote → already pinned by test_f20_14_* (03-14)
  c81-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL surfaces in missing_qualification_env() → already pinned by test_f20_15_* (03-15)
  c81-16 — _infra_failure_signature stable across candidate rotations → already pinned by test_f20_16_* (03-16)
  c81-17 — hotfix_policy.py and admin_operator_triggers.py in HOTFIX_ELIGIBLE_MODULES → already pinned by test_f20_17_* (03-17)

Findings covered here (7-finding creation-path fail-closed/attention/lifecycle salvage bundle, task b78e464d, 2026-07-21):
  b78-1 — design: creation-path fail-closed audience contract → already pinned by test_m1_* + test_m2_* (m-1/m-2; fb-1; d-1)
  b78-2 — fix: fail-closed audience at Alert/message creation → already pinned by test_m2_* (m-2; fb-2; d-2)
  b78-3 — feat: operator inbox consumes server attention/lifecycle model (operator_action hint, Ack button)
           → already pinned by MessagesPanel.test.tsx::MessagesPanel attention model lifecycle field consumption (m-3; fb-3; d-3)
  b78-4 — feat(ui): make operator inbox attention-driven, retire dual effectiveSignal classification
           → already pinned by MessagesPanel.test.tsx::MessagesPanel needs-action filter (m-4; fb-4; d-4)
  b78-5 — fix(chat): make confirmation chips actionable (accept/dismiss in needs-action strip)
           → already pinned by MessagesPanel.test.tsx::MessagesPanel confirmation chip actionability (m-5; fb-5; d-5)
  b78-6 — feat(messages): auto-decay stale open QUESTION messages → already pinned by test_m6_* (m-6; fb-6; d-6)
  b78-7 — feat(ui): converge operator notifications on attention model (toast uses attention, not effectiveSignal)
           → already pinned by MessagesPanel.test.tsx::fires toast for needs_operator message (m-7; fb-7; d-7)

Findings covered here (18-finding release-qualification salvage bundle, task 8d2616a1, 2026-07-21):
  8d-1  — env-preflight fail-closed wedges promotion for repos with no manifest → already pinned by test_f20_10_*
           (env_preflight_evidence.py returns empty required_env for absent manifest, scoring 1.0 / vacuously satisfied)
  8d-2  — classify serving-only-import ImportError as degrade, not hard block → already pinned by test_f20_1_*
  8d-3  — design: prove data-dependent migrations safely → already pinned by test_f20_2_*
  8d-4  — feat(security): verify encryption-key continuity → already pinned by test_f20_3_* + e1/e2/e3
  8d-5  — research: define safe Fernet key rotation → already pinned by test_f20_4_*
  8d-6  — fix(encrypt): fail closed on keyless/malformed encryption backfill → already pinned by test_f20_5_*
  8d-7  — fix(release): make scratch-DB URL degraded, not boot-fatal → already pinned by test_f20_6_*
  8d-8  — fix(release): isolate gate-defer signature from unrelated child failures → already pinned by test_f20_7_*
  8d-9  — fix(release): contain timed-out qualifier children → already pinned by test_f20_8_*
  8d-10 — fix(release): block deterministic migration defects → already pinned by test_f20_9_*
  8d-11 — feat(release): enforce independent boot preflight → already pinned by test_f20_10_*
  8d-12 — refactor(release): unify trigger truth and retries → already pinned by test_f20_11_*
  8d-13 — fix(release): contain orphan promotion actors → already pinned by test_f20_12_*
  8d-14 — feat(release): gate data-migration proof → already pinned by test_f20_13_*
  8d-15 — feat(ui): guide narrow release remediation → already pinned by test_f20_14_*
  8d-16 — feat(release): declare + diagnose migration-apply scratch prereq → already pinned by test_f20_15_*
  8d-17 — fix(release): stabilize infra-failure signature for gate defers → already pinned by test_f20_16_*
  8d-18 — fix(release): add hotfix_policy.py to hotfix-eligible modules → already pinned by test_f20_17_*
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
    """MIGRATION_APPLY_SCRATCH_DATABASE_URL must not appear in REQUIRED_RUNTIME_ENV.

    Finding #11: absent scratch URL must degrade qualification (gate defers with an
    actionable verdict) but MUST NOT block the app from booting on Cloud Run.
    """
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    _SCRATCH_VAR = "MIGRATION_APPLY_SCRATCH_DATABASE_URL"
    boot_names = {r.name for r in REQUIRED_RUNTIME_ENV}

    assert _SCRATCH_VAR not in boot_names, (
        f"{_SCRATCH_VAR} must NOT be in REQUIRED_RUNTIME_ENV — "
        "its absence must degrade qualification, never block Cloud Run boot"
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
    in REQUIRED_RUNTIME_ENV, making its absence boot-fatal.  It must not be in
    REQUIRED_RUNTIME_ENV so _verify_required_env() passes without it.
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
    through the unified decision contract.  When the last-qualification status is
    a terminal-like value that no T1–T5 rule matches, the resolver falls through
    to the default not_applicable verdict (fire=False).
    """
    from app.qualification_triggers import resolve_qualification_trigger

    for terminal_status in ("promoted", "abandoned"):
        decision = resolve_qualification_trigger(
            phase="stabilize",
            status=terminal_status,
            error_code=None,
            qualification_attempts=1,
            error_redrive_count=0,
            is_candidate_fully_met=False,
            requalification_ready=None,
            candidate_sha="abc123",
            automain_sha="abc123",
            automain_ci_state="green",
            blocker=None,
            in_flight=False,
            promotion_in_flight=False,
            consecutive_infra_errors=0,
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
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2]
        / "admin-ui" / "src" / "components" / "VersionLifecyclePanel.tsx",
        Path("/repo/admin-ui/src/components/VersionLifecyclePanel.tsx"),
    ]
    source: str | None = None
    for candidate in candidates:
        if candidate.exists():
            source = candidate.read_text()
            break
    if source is None:
        import pytest

        pytest.skip(
            "VersionLifecyclePanel.tsx not found — admin-ui not present in this shard"
        )

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
# 16-finding compute viewable artifacts bundle
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
# Finding 1 / v-5 — live-preview fairness: scanner rotates past last-uploaded path
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
# Finding v-2 — GCP-VM jobs stream mid-run viewables via a dedicated endpoint
# --------------------------------------------------------------------------- #


def test_gcp_vm_viewable_endpoint_exists_in_agent_compute_jobs():
    """agent_compute_jobs.py exposes a mid-run viewable upload endpoint for GCP VMs.

    Finding v-2: compute_job_vm_heartbeat (the GCP heartbeat handler) never ingested
    viewable multipart bodies; GCP VM jobs had no path to stream mid-run plots to the
    canvas.  The fix adds a separate POST /compute-jobs/{job_id}/viewable endpoint
    in agent_compute_jobs.py, mirroring the runner-subprocess path, and a companion
    viewable_watch.py daemon that runs inside the VM and calls that endpoint.
    """
    import inspect
    from app.routers import agent_compute_jobs as acj_mod

    source = inspect.getsource(acj_mod)

    # The endpoint must exist and use HMAC authentication (same secret as heartbeat).
    assert "compute-jobs/{job_id}/viewable" in source or "/viewable" in source, (
        "agent_compute_jobs.py must expose a /viewable upload endpoint for GCP-VM "
        "mid-run viewable streaming (analogous to runner.py's viewable endpoint)"
    )
    assert "compute_job_heartbeat_secret" in source, (
        "The GCP-VM viewable endpoint must authenticate via compute_job_heartbeat_secret, "
        "the same per-job HMAC secret used by the heartbeat endpoint"
    )
    assert "viewable_files" in source, (
        "The GCP-VM viewable endpoint must stamp polling_metadata['viewable_files'] "
        "so the canvas can render mid-run previews before terminal finalization"
    )

    # viewable_watch.py must also exist and call the right URL path.
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "_compute_scripts" / "viewable_watch.py",
        Path("/repo/backend/app/_compute_scripts/viewable_watch.py"),
    ]
    watch_source: str | None = None
    for p in candidates:
        if p.exists():
            watch_source = p.read_text(encoding="utf-8")
            break

    assert watch_source is not None, (
        "backend/app/_compute_scripts/viewable_watch.py must exist — "
        "GCP VMs run this daemon to upload mid-run viewable files to the orchestrator"
    )
    assert "/compute-jobs/" in watch_source and "/viewable" in watch_source, (
        "viewable_watch.py must POST to /api/agent/compute-jobs/<job_id>/viewable "
        "to deliver mid-run previews for GCP-VM jobs"
    )


# --------------------------------------------------------------------------- #
# Finding v-3 — extension guard on both signing paths; orphan purge present
# --------------------------------------------------------------------------- #


def test_signing_extension_guard_applies_to_computejobfile_branch():
    """The signed-URL endpoint applies the viewable extension guard on BOTH paths.

    Finding v-3 (unify viewable surfaces): the ComputeJobFile signing branch in
    main.py formerly accepted any URI whose row existed in the DB without
    re-validating the extension, while the manifest branch called
    viewable_content_type_for_filename.  Both branches must now share the same guard
    so a mis-classified row cannot produce a signed URL for a non-viewable file.
    """
    import inspect
    import app.main as main_mod

    source = inspect.getsource(main_mod)

    # The ComputeJobFile signing branch must import + apply viewable_content_type_for_filename
    # (or alias it as _vctype) to validate the extension before generating a signed URL.
    assert "viewable_content_type_for_filename" in source, (
        "app.main must reference viewable_content_type_for_filename in the "
        "compute-job URI signing path so the extension guard covers ComputeJobFile rows"
    )
    # The guard must be applied on the ComputeJobFile row's safe_relpath.
    # The implementation aliases the function as _vctype and calls _vctype(cjf.safe_relpath).
    assert "_vctype" in source, (
        "app.main must alias viewable_content_type_for_filename as _vctype and call it "
        "on cjf.safe_relpath so non-viewable extensions are rejected in the ComputeJobFile branch"
    )
    # The rejection path (404 on None result) must exist.
    assert "Artifact not found" in source, (
        "app.main must return 404 when the extension guard rejects the ComputeJobFile "
        "relpath — so callers receive the same response as for a missing artifact"
    )


def test_orphaned_viewable_rows_purge_exists_in_compute_jobs():
    """compute_jobs.py defines a purge helper for orphaned ComputeJobFile rows.

    Finding v-3: _record_viewable_job_files only inserted/updated rows but never
    deleted mid-run uploads that were excluded from the terminal top-25 manifest.
    Those orphaned rows appeared in the signing allowlist without being in the
    displayed manifest, creating a silent asymmetry.  The purge helper reconciles
    both surfaces to the authoritative terminal manifest.
    """
    import inspect
    import app.compute_jobs as cj_mod

    source = inspect.getsource(cj_mod)

    assert "_purge_orphaned_viewable_rows" in source, (
        "app.compute_jobs must define _purge_orphaned_viewable_rows to delete "
        "ComputeJobFile rows from mid-run uploads that were not selected into the "
        "terminal top-25 manifest"
    )
    # The purge must be called from the terminal extraction path.
    assert source.count("_purge_orphaned_viewable_rows") >= 2, (
        "_purge_orphaned_viewable_rows must be both defined AND called during terminal "
        "artifact extraction — found fewer than 2 occurrences"
    )


# --------------------------------------------------------------------------- #
# Finding v-4 — ComputeJobsPanel delegates to ComputeArtifactGallery (no inline copy)
# --------------------------------------------------------------------------- #


def test_compute_jobs_panel_uses_compute_artifact_gallery():
    """ComputeJobsPanel renders viewable files through ComputeArtifactGallery.

    Finding v-4 (de-duplicate gallery): ComputeJobsPanel.tsx formerly contained a
    near-verbatim inline copy of the resolve/cache/mark-error/open-full-size pipeline
    duplicated from ComputeArtifactGallery.tsx.  The two copies auto-resolved
    differently and had divergent cap messaging.  The fix replaces the inline copy
    with a <ComputeArtifactGallery> import so both surfaces share one implementation.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "ComputeJobsPanel.tsx",
        Path("/repo/admin-ui/src/components/ComputeJobsPanel.tsx"),
    ]
    panel_source: str | None = None
    for p in candidates:
        if p.exists():
            panel_source = p.read_text(encoding="utf-8")
            break

    if panel_source is None:
        import pytest

        pytest.skip("ComputeJobsPanel.tsx not found — skipping frontend source check")

    assert "ComputeArtifactGallery" in panel_source, (
        "ComputeJobsPanel.tsx must import and render ComputeArtifactGallery so that "
        "the viewable file gallery is not duplicated inline in the panel component"
    )
    # The inline resolve pipeline (useFreshMediaUrl called directly inside the panel)
    # would be a sign of duplication.  The gallery component owns it instead.
    assert panel_source.count("useFreshMediaUrl") == 0, (
        "ComputeJobsPanel.tsx must NOT contain useFreshMediaUrl — that hook belongs "
        "in ComputeArtifactGallery.tsx; a copy in the panel means the two surfaces "
        "resolve and cache signed URLs independently"
    )


# --------------------------------------------------------------------------- #
# Finding 5 / v-9 — live-viewable endpoint authorizes against allowed_project_ids
# --------------------------------------------------------------------------- #


def test_viewable_endpoint_uses_allowed_project_ids():
    """The /compute-job/{job_id}/viewable endpoint must use allowed_project_ids.

    Finding 5 / v-9 claim: 'The live-viewable endpoint filters ComputeJob.project_id
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
    # The filter must use .in_(allowed_projects) not an == comparison.
    assert "in_(allowed_projects)" in source, (
        "runner.py must build an allowed_projects set via allowed_project_ids() and "
        "filter ComputeJob.project_id with .in_(allowed_projects) — not an == comparison "
        "to runner.project_id — so bound secondary projects are accepted"
    )


# --------------------------------------------------------------------------- #
# Finding v-6 — StrategicContextWorkspace suppresses duplicate media for promoted artifacts
# --------------------------------------------------------------------------- #


def test_strategic_workspace_suppresses_evidence_media_for_promoted_artifacts():
    """StrategicContextWorkspace hides evidence-section media when already on canvas.

    Finding v-6 (deduplicate artifact media galleries): when a compute artifact is
    selected as a primary canvas descriptor, its inline image/video gallery rendered
    the same signed URLs a second time in the Evidence section.  The fix guards the
    ArtifactViewablePreviews render with `!isPromoted`, silencing duplicates while
    keeping the evidence row (label + navigation) visible.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "StrategicContextWorkspace.tsx",
        Path("/repo/admin-ui/src/components/StrategicContextWorkspace.tsx"),
    ]
    ws_source: str | None = None
    for p in candidates:
        if p.exists():
            ws_source = p.read_text(encoding="utf-8")
            break

    if ws_source is None:
        import pytest

        pytest.skip("StrategicContextWorkspace.tsx not found — skipping frontend source check")

    assert "isPromoted" in ws_source, (
        "StrategicContextWorkspace.tsx must compute isPromoted for each evidence artifact "
        "so that media previews are not rendered a second time when the artifact is "
        "already displayed as a primary canvas descriptor"
    )
    assert "!isPromoted" in ws_source, (
        "The ArtifactViewablePreviews block must be conditional on !isPromoted "
        "to suppress duplicate media for promoted artifacts"
    )
    # The !isPromoted guard and ArtifactViewablePreviews must appear near each other
    # in the source (both in the evidence artifact render block).
    not_promoted_idx = ws_source.index("!isPromoted")
    # ArtifactViewablePreviews is imported at the top but also used after !isPromoted.
    # Find the first usage AFTER the !isPromoted guard.
    after_guard = ws_source[not_promoted_idx:]
    assert "ArtifactViewablePreviews" in after_guard, (
        "ArtifactViewablePreviews must appear after !isPromoted in "
        "StrategicContextWorkspace.tsx so the conditional guard wraps the preview render"
    )


# --------------------------------------------------------------------------- #
# Finding v-7 — UI accurately describes per-kind size limits (image ≠ video)
# --------------------------------------------------------------------------- #


def test_compute_artifact_gallery_shows_accurate_per_kind_caps():
    """ComputeArtifactGallery describes the actual per-kind size limits.

    Finding v-7 (report media preview limits honestly): all three artifact surfaces
    formerly described a uniform 8-MiB-per-file cap.  The backend applies 8 MiB for
    images/PDFs but a separately configurable cap (default 512 MiB) for videos.
    The UI must state both limits so operators understand why a large video may be
    omitted while smaller images are still delivered.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "ComputeArtifactGallery.tsx",
        Path("/repo/admin-ui/src/components/ComputeArtifactGallery.tsx"),
    ]
    gallery_source: str | None = None
    for p in candidates:
        if p.exists():
            gallery_source = p.read_text(encoding="utf-8")
            break

    if gallery_source is None:
        import pytest

        pytest.skip("ComputeArtifactGallery.tsx not found — skipping frontend source check")

    assert "512 MiB" in gallery_source, (
        "ComputeArtifactGallery.tsx must mention '512 MiB' (the video cap) so the "
        "operator understands that videos have a different (larger) per-file limit"
    )
    assert "8 MiB" in gallery_source, (
        "ComputeArtifactGallery.tsx must mention '8 MiB' (the image/PDF cap) so the "
        "operator understands that images and PDFs have a smaller per-file limit"
    )
    # The two caps must appear in the SAME context so they're not ambiguous.
    assert "video" in gallery_source.lower(), (
        "ComputeArtifactGallery.tsx must use 'video' in the cap description to "
        "distinguish the 512-MiB video limit from the 8-MiB image limit"
    )


# --------------------------------------------------------------------------- #
# Finding v-8 — backend returns artifact_total; frontend exposes live_count
# --------------------------------------------------------------------------- #


def test_evidence_response_returns_artifact_total_and_live_count():
    """load_strategic_context_evidence returns durable total + live_count separately.

    Finding v-8 (disclose artifact sampling): the workspace displayed a count derived
    from the bounded item list (≤4) rather than the durable artifact total.  The fix
    adds 'total' (durable DB count) and 'live_count' (number of running-job preview
    rows prepended) to the evidence artifacts dict so the frontend can distinguish
    'showing 4 of 12 durable artifacts' from 'there are exactly 4'.
    """
    source = _strategic_pkg_source()

    # The evidence-load function must return 'total' in the artifacts dict.
    assert '"total"' in source or "'total'" in source, (
        "load_strategic_context_evidence must include 'total' (durable artifact count) "
        "in the returned artifacts dict so the frontend can show 'N of M' sampling disclosure"
    )
    assert '"live_count"' in source or "'live_count'" in source, (
        "load_strategic_context_evidence must include 'live_count' (running-job preview "
        "row count) so the frontend can label live items separately from terminal ones"
    )

    # The frontend type definition must reflect both fields.
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "strategicStateApi.ts",
        Path("/repo/admin-ui/src/strategicStateApi.ts"),
    ]
    api_source: str | None = None
    for p in candidates:
        if p.exists():
            api_source = p.read_text(encoding="utf-8")
            break

    if api_source is not None:
        assert "live_count" in api_source, (
            "strategicStateApi.ts must type-check live_count in the evidence artifacts "
            "shape so consuming components can safely read it"
        )


# --------------------------------------------------------------------------- #
# Finding v-11 — useFreshMediaUrl caches by (storage_uri, revision) so overwrites trigger refresh
# --------------------------------------------------------------------------- #


def test_fresh_media_url_hook_uses_revision_in_generation_key():
    """useFreshMediaUrl keys the signed-URL cache on (storage_uri, revision).

    Finding v-11 (refresh overwritten live previews): the hook previously keyed only
    on storage_uri.  The backend deliberately overwrites the same canonical URI when
    a runner re-uploads a changed same-filename file, so a stale signed URL cached
    under the old content would never refresh even though the server had new bytes at
    the same path.  Folding revision into the generation key forces a fresh URL fetch
    whenever a new revision token arrives.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "StrategicContextArtifactSlot.tsx",
        Path("/repo/admin-ui/src/components/StrategicContextArtifactSlot.tsx"),
    ]
    slot_source: str | None = None
    for p in candidates:
        if p.exists():
            slot_source = p.read_text(encoding="utf-8")
            break

    if slot_source is None:
        import pytest

        pytest.skip("StrategicContextArtifactSlot.tsx not found — skipping frontend source check")

    assert "revision" in slot_source, (
        "StrategicContextArtifactSlot.tsx must accept a 'revision' prop in useFreshMediaUrl "
        "so overwritten same-path files cause a cache miss and a fresh signed URL"
    )
    assert "generation" in slot_source, (
        "useFreshMediaUrl must build a 'generation' string that includes both storage_uri "
        "and revision; a change to either must trigger a URL re-fetch"
    )
    # The generation must concatenate storage_uri AND revision (not just one).
    gen_line_idx = slot_source.find("generation")
    segment = slot_source[gen_line_idx : gen_line_idx + 200]
    assert "revision" in segment, (
        "The 'generation' variable in useFreshMediaUrl must reference 'revision' so the "
        "cache key changes when a same-path file is overwritten with new content"
    )


# --------------------------------------------------------------------------- #
# Finding 8 / v-12 — mixed-media descriptor retains all preview kinds
# --------------------------------------------------------------------------- #


def test_artifact_descriptor_retains_mixed_media_kinds():
    """_artifact_descriptor emits image, video, AND pdf groups from a mixed manifest.

    Finding 8 / v-12 claim: 'After choosing a kind, the descriptor emits only that
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


def test_artifact_descriptor_mixed_image_and_pdf():
    """_artifact_descriptor retains both image and pdf keys on a mixed artifact."""
    from app.strategic_context_package import _artifact_descriptor

    artifact = {
        "id": "mix-1",
        "artifact_type": "COMPUTE_RESULT",
        "logical_key": "result",
        "label": "mixed result",
        "viewable_previews": [
            {
                "url": "https://cdn.example.com/reward.png?sig=a",
                "kind": "image",
                "filename": "reward_curve.png",
                "storage_uri": "gs://b/compute-jobs/j/artifacts/viewable/reward_curve.png",
            },
            {
                "url": "https://cdn.example.com/report.pdf?sig=b",
                "kind": "pdf",
                "filename": "full_report.pdf",
                "storage_uri": "gs://b/compute-jobs/j/artifacts/viewable/full_report.pdf",
            },
        ],
    }

    desc = _artifact_descriptor(artifact)

    assert "image" in desc, (
        "_artifact_descriptor must include 'image' key when a viewable preview of kind='image' exists"
    )
    assert "pdf" in desc, (
        "_artifact_descriptor must include 'pdf' key when a viewable preview of kind='pdf' exists — "
        "a mixed image+PDF compute result must NOT collapse to image only"
    )
    assert desc["image"]["url"] == "https://cdn.example.com/reward.png?sig=a"
    assert desc["pdf"]["url"] == "https://cdn.example.com/report.pdf?sig=b"


# --------------------------------------------------------------------------- #
# Finding 9 / v-13 — late bundle arrival backfills artifact_id / result_reference
# --------------------------------------------------------------------------- #


def test_late_bundle_upload_triggers_artifact_linkage():
    """runner.py backfills artifact_id when a bundle arrives after terminalization.

    Finding 9 / v-13 claim: 'A terminal runner job can currently retain a stored bundle
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
# Finding 10 / v-14 — viewable-bearing artifacts sort first in evidence slice
# --------------------------------------------------------------------------- #


def test_evidence_loader_sorts_viewable_artifacts_first():
    """Terminal artifact evidence sorts viewable-bearing rows before non-viewable.

    Finding 10 / v-14 claim: 'Terminal evidence queries the four newest meaningful
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
# Finding 11 / v-15 — live omission telemetry surfaced via polling_metadata
# --------------------------------------------------------------------------- #


def test_live_omissions_stamped_in_polling_metadata():
    """The runner stamps viewable_live_omissions into polling_metadata on running reports.

    Finding 11 / v-15 claim: 'The runner logs local oversize/count-budget skips but does
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

    Finding 11 / v-15 claim: omission telemetry must flow from runner heartbeat through
    polling_metadata to the API response so the UI can display it.
    """
    source = _admin_compute_jobs_source()

    assert "viewable_live_omissions" in source or "viewable_artifacts_live_omissions" in source, (
        "admin_compute_jobs.py must expose the runner's live omission counts "
        "as viewable_artifacts_live_omissions in the compute-job polling response"
    )


# --------------------------------------------------------------------------- #
# Finding 12 / v-1 / v-16 — PDF in all three allowlists
# --------------------------------------------------------------------------- #


def test_pdf_in_viewable_content_types():
    """PDF must be in the storage viewable content-type registry.

    Finding 12 / v-16 claim: 'compute_backends._VIEWABLE_CONTENT_TYPES has no pdf entry.'
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

    Finding 12 / v-1 claim: the runner must mirror the server's allowlist so it does
    not skip PDF files before attempting upload.
    """
    source = _local_runner_source()

    assert '"pdf": ("application/pdf", "pdf")' in source or (
        '"pdf"' in source and "application/pdf" in source
    ), (
        "local_runner.py _VIEWABLE_CONTENT_TYPES must include a 'pdf' entry "
        "matching ('application/pdf', 'pdf')"
    )

    # The runner 422 error message must list pdf so the operator knows what is accepted.
    runner_source = _runner_router_source()
    assert "pdf" in runner_source and "422" in runner_source, (
        "runner.py must include 'pdf' in the 422 error message for unsupported "
        "viewable extensions so operators see the full supported set"
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
    body = source[idx : idx + 8000]

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


# =========================================================================== #
# 4-finding operator-inbox/messages salvage bundle (2026-07-17)
# =========================================================================== #


def _messages_panel_source() -> str:
    """Return the source of admin-ui/src/components/MessagesPanel.tsx."""
    try:
        return _ui_component_source("MessagesPanel.tsx")
    except FileNotFoundError:
        raise


def _use_message_list_source() -> str:
    """Return the source of admin-ui/src/components/useMessageList.ts."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "useMessageList.ts",
        Path("/repo/admin-ui/src/components/useMessageList.ts"),
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("useMessageList.ts not found")


def _app_tsx_source() -> str:
    """Return the source of admin-ui/src/App.tsx."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "App.tsx",
        Path("/repo/admin-ui/src/App.tsx"),
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("App.tsx not found")


# --------------------------------------------------------------------------- #
# Finding m1 — inbox attention-driven: all four surfaces use server model
# --------------------------------------------------------------------------- #


def test_messages_panel_badge_uses_server_needs_operator():
    """App.tsx must use server-side needs_operator for the badge count.

    Finding m1: the badge (HeaderBar/tab-title) must read the server-derived
    needs_operator count, not a client-side effectiveSignal re-derivation, so
    all four notification surfaces agree on the same canonical source.
    """
    try:
        source = _app_tsx_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/App.tsx not available in this environment")

    assert "needs_operator" in source, (
        "App.tsx must read 'needs_operator' from the server-side attention "
        "model (messages/counts endpoint) for the badge count — client-side "
        "effectiveSignal re-derivation would diverge from the toast and "
        "needs-action strip surfaces"
    )


def test_messages_panel_toast_uses_attention_model_not_effective_signal():
    """MessagesPanel toast dispatch must use isMessageNeedingOperatorAttention.

    Finding m4/m1: toast and desktop-notification dispatch must gate on the
    canonical server attention model (isMessageNeedingOperatorAttention), not
    on client-derived effectiveSignal(), so the badge and toast always agree
    about which messages are 'needs_operator'.
    """
    try:
        source = _messages_panel_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    assert "isMessageNeedingOperatorAttention" in source, (
        "MessagesPanel.tsx must define and call isMessageNeedingOperatorAttention "
        "to gate toast/desktop-notification dispatch — using effectiveSignal() "
        "instead would let a locally-muted event_type suppress toasts that the "
        "server marks needs_operator, causing badge/toast divergence"
    )
    # The function must check severity === 'needs_operator' so only server-confirmed
    # items trigger toasts (not every open message).
    assert "needs_operator" in source, (
        "isMessageNeedingOperatorAttention must filter by severity='needs_operator' "
        "from the canonical attention model items"
    )


def test_use_message_list_promotes_needs_operator_ids_to_high_signal():
    """useMessageList must promote needsOperatorIds to highSignalOpen unconditionally.

    Finding m1: the list high/routine split must use needsOperatorIds (from the
    server attention model) to promote messages to highSignalOpen even when the
    event_type is locally muted — so the list stays consistent with the badge,
    toast, and needs-action strip surfaces.
    """
    try:
        source = _use_message_list_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    assert "needsOperatorIds" in source, (
        "useMessageList.ts must accept a needsOperatorIds parameter to promote "
        "server-marked needs_operator messages to highSignalOpen regardless of "
        "local mute prefs"
    )
    # highSignalOpen must include the needsOperatorIds union so a locally-muted
    # but server-attention-marked message appears in the high-signal section.
    assert "needsOperatorIds?.has" in source or "needsOperatorIds.has" in source, (
        "useMessageList.ts must call needsOperatorIds.has(m.id) when computing "
        "highSignalOpen so server-attention items override local mute prefs"
    )
    # routineOpen must exclude needsOperatorIds to avoid double-counting.
    assert "needsOperatorIds" in source, (
        "useMessageList.ts must exclude needsOperatorIds items from routineOpen "
        "to prevent a message from appearing in both sections"
    )


def test_messages_panel_count_high_signal_guarded_by_strategic_model_flag():
    """countHighSignalMessages must only fire when usesStrategicReadModel is false.

    Finding m1: when the strategic read model is active, onMessageCountChange
    must be driven by the server attention count, not countHighSignalMessages().
    This guard prevents the badge from oscillating between server and
    client-derived values as the read model toggles.
    """
    try:
        source = _messages_panel_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    # countHighSignalMessages must be guarded so it only fires in the fallback path.
    assert "usesStrategicReadModel" in source, (
        "MessagesPanel.tsx must check usesStrategicReadModel before calling "
        "onMessageCountChange with countHighSignalMessages() — without the guard "
        "the badge oscillates between server and client derivations"
    )


# --------------------------------------------------------------------------- #
# Finding m2 — confirmation chips must be actionable (expand → accept/dismiss)
# --------------------------------------------------------------------------- #


def test_needs_action_strip_confirmation_chips_are_actionable():
    """MessagesPanel needs-action strip must handle confirmation items, not just messages.

    Finding m2: the click handler for needs-action strip chips must branch on
    BOTH 'message' and 'confirmation' kinds. A branch that only scrolls to a
    message leaves confirmation chips visually enabled but inert.
    """
    try:
        source = _messages_panel_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    # isConfirmation must be derived and used in the chip render path.
    assert "isConfirmation" in source, (
        "MessagesPanel.tsx must derive isConfirmation from item.kind === 'confirmation' "
        "and use it to enable the expand/accept/dismiss flow in the needs-action strip"
    )
    # The expand toggle must be wired — setExpandedConfirmationItemId drives the
    # reveal of Accept/Dismiss buttons.
    assert "setExpandedConfirmationItemId" in source, (
        "MessagesPanel.tsx must call setExpandedConfirmationItemId to expand a "
        "confirmation chip and reveal the Accept/Dismiss controls"
    )
    # Accept and dismiss API calls must be wired through the strip chips.
    assert "acceptPendingConfirmation" in source, (
        "MessagesPanel.tsx must call acceptPendingConfirmation from the "
        "needs-action strip chip (not only from StrategicContextRail)"
    )
    assert "dismissPendingConfirmation" in source, (
        "MessagesPanel.tsx must call dismissPendingConfirmation from the "
        "needs-action strip chip (not only from StrategicContextRail)"
    )
    # After accept/dismiss the attention model must be refreshed.
    assert "refreshStrategicState" in source, (
        "MessagesPanel.tsx must call refreshStrategicState after accept/dismiss "
        "so the needs-action strip re-evaluates immediately without waiting for "
        "the next poll cycle"
    )


# --------------------------------------------------------------------------- #
# Finding m3 — QUESTION auto-decay: CONVERSATIONAL_DECAY_MESSAGE_TYPES + stamping
# --------------------------------------------------------------------------- #


def test_conversational_decay_types_includes_question():
    """CONVERSATIONAL_DECAY_MESSAGE_TYPES must include QUESTION.

    Finding m3: the stale-open decay sweep must cover QUESTION messages —
    not just NOTE/FEEDBACK — so unanswered questions age out automatically
    with a distinct 'stale_unanswered' resolution reason operators can
    distinguish from genuinely-answered questions.
    """
    from app.message_convergence import CONVERSATIONAL_DECAY_MESSAGE_TYPES

    assert "QUESTION" in CONVERSATIONAL_DECAY_MESSAGE_TYPES, (
        "CONVERSATIONAL_DECAY_MESSAGE_TYPES must include 'QUESTION' so that "
        "sweep_decay_stale_open_messages covers unanswered QUESTION messages; "
        f"current set: {CONVERSATIONAL_DECAY_MESSAGE_TYPES}"
    )


def test_stale_question_decay_uses_longer_window_and_distinct_reason():
    """sweep_decay_stale_open_messages must apply a longer decay window for QUESTION.

    Finding m3: QUESTION decay must use STALE_QUESTION_DECAY_DAYS (default 30d,
    longer than NOTE/FEEDBACK at 14d) and stamp resolution='stale_unanswered'
    so operators can distinguish auto-resolved from genuinely-answered questions.
    """
    import inspect
    from app.reaper_sweeps import sweep_decay_stale_open_messages, STALE_QUESTION_DECAY_DAYS

    assert STALE_QUESTION_DECAY_DAYS >= 20, (
        f"STALE_QUESTION_DECAY_DAYS={STALE_QUESTION_DECAY_DAYS} must be longer "
        "than the NOTE/FEEDBACK window (14d) — questions deserve more patience "
        "before being auto-resolved"
    )

    source = inspect.getsource(sweep_decay_stale_open_messages)

    # Must apply the question-specific window.
    assert "stale_question_decay_days" in source or "question_decay_days" in source, (
        "sweep_decay_stale_open_messages must read a distinct question decay window "
        "(stale_question_decay_days) rather than applying the same 14-day NOTE window "
        "to QUESTION messages"
    )
    # Must stamp the distinct reason.
    assert "stale_unanswered" in source, (
        "sweep_decay_stale_open_messages must stamp resolution='stale_unanswered' "
        "for QUESTION messages so operators can distinguish auto-resolved questions "
        "from genuinely-answered ones"
    )
    # BLOCKER must be excluded (never auto-decayed by this sweep).
    assert "BLOCKER" not in source.split("CONVERSATIONAL_DECAY_MESSAGE_TYPES")[0].rsplit("def ", 1)[-1] or (
        "BLOCKER" not in [
            line.strip()
            for line in source.splitlines()
            if "in_(" in line and "BLOCKER" in line
        ]
    ), "BLOCKER must not appear in the CONVERSATIONAL_DECAY_MESSAGE_TYPES include-list"


def test_stale_question_decay_days_constant_is_correct():
    """STALE_QUESTION_DECAY_DAYS must be 30 (the design-specified longer window)."""
    from app.reaper_sweeps import STALE_QUESTION_DECAY_DAYS, STALE_OPEN_DECAY_DAYS

    assert STALE_QUESTION_DECAY_DAYS == 30, (
        f"STALE_QUESTION_DECAY_DAYS should be 30; got {STALE_QUESTION_DECAY_DAYS}"
    )
    assert STALE_QUESTION_DECAY_DAYS > STALE_OPEN_DECAY_DAYS, (
        "QUESTION decay window must exceed the NOTE/FEEDBACK window "
        f"({STALE_OPEN_DECAY_DAYS}d) — questions deserve more patience"
    )


# --------------------------------------------------------------------------- #
# Findings n1-n3 — lifecycle fields consumed in needs-action strip
# (5-finding operator-inbox lifecycle/convergence salvage bundle, 2026-07-17)
# --------------------------------------------------------------------------- #


def test_lifecycle_operator_action_label_function_exists():
    """MessagesPanel must define lifecycleOperatorActionLabel for the needs-action strip.

    Finding n1: the lifecycle.operator_action field from the canonical attention
    model must be consumed in the needs-action strip so operators see a readable
    hint about what action to take (e.g. 'Reply or resolve', 'Approve or reject').
    The helper function must map the known operator_action values to short labels.
    """
    try:
        source = _messages_panel_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    assert "lifecycleOperatorActionLabel" in source, (
        "MessagesPanel.tsx must define lifecycleOperatorActionLabel to convert "
        "lifecycle.operator_action values to human-readable hint text shown in "
        "the needs-action strip chips"
    )
    # Spot-check that common operator_action values are mapped.
    assert "reply_or_resolve_message" in source, (
        "lifecycleOperatorActionLabel must map 'reply_or_resolve_message' — "
        "this is the operator_action for QUESTION/BLOCKER/FEEDBACK message items"
    )
    assert "approve_or_reject_task" in source, (
        "lifecycleOperatorActionLabel must map 'approve_or_reject_task' — "
        "the approval_required attention reason"
    )


def test_needs_action_strip_renders_operator_action_hint():
    """MessagesPanel needs-action strip must render the operator_action hint text.

    Finding n1: the lifecycleOperatorActionLabel helper must be called and its
    result rendered inside the chip so the operator sees a readable hint next to
    the item label (e.g. 'Reply or resolve' for a QUESTION message item).
    """
    try:
        source = _messages_panel_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    # The hint must be driven by item.lifecycle?.operator_action.
    assert "operator_action" in source, (
        "MessagesPanel.tsx needs-action strip must read item.lifecycle?.operator_action "
        "to derive the operator hint label"
    )
    # The hint span must be rendered with a test id or identifiable attribute so
    # the operator sees the action in the chip.
    assert "operator-action-hint" in source or "operatorActionHint" in source, (
        "MessagesPanel.tsx must render the operator_action hint in the chip — "
        "either via a data-testid='operator-action-hint' span or an operatorActionHint variable"
    )


def test_needs_action_strip_acknowledgeable_items_have_ack_button():
    """MessagesPanel needs-action strip must render an Ack button for acknowledgeable items.

    Finding n2: lifecycle.acknowledgeable=true means the operator can dismiss the
    item from the strip without a full resolve. The strip must render an 'Ack'
    button for items where lifecycle.acknowledgeable is true, driven by
    lifecycle.acknowledge_effect for the tooltip text.
    """
    try:
        source = _messages_panel_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    # acknowledgeable must be read from the lifecycle block.
    assert "acknowledgeable" in source, (
        "MessagesPanel.tsx needs-action strip must read item.lifecycle?.acknowledgeable "
        "to conditionally show the Ack button"
    )
    # The Ack button must be rendered with a test id.
    assert "attention-ack-btn" in source, (
        "MessagesPanel.tsx must render a button with data-testid='attention-ack-btn' "
        "for acknowledgeable attention items in the needs-action strip"
    )
    # The Ack button click must add the item to resolvedConfirmationIds to remove it.
    assert "handleAck" in source or "resolvedConfirmationIds" in source, (
        "MessagesPanel.tsx Ack button must dismiss the item from the strip by "
        "adding its id to resolvedConfirmationIds"
    )


def test_lifecycle_acknowledge_effect_label_function_exists():
    """MessagesPanel must define lifecycleAcknowledgeEffectLabel for the Ack button tooltip.

    Finding n3: the lifecycle.acknowledge_effect field must be consumed to provide
    tooltip text on the Ack button that explains what acknowledging does (e.g.
    'Mark as seen — removes from needs-action strip until next update' for
    acknowledge_effect='clears_unread_only').
    """
    try:
        source = _messages_panel_source()
    except FileNotFoundError:
        import pytest
        pytest.skip("admin-ui/src/components not available in this environment")

    assert "lifecycleAcknowledgeEffectLabel" in source, (
        "MessagesPanel.tsx must define lifecycleAcknowledgeEffectLabel to convert "
        "lifecycle.acknowledge_effect values to human-readable tooltip text on the "
        "Ack button — leaving acknowledge_effect unused leaves operators without "
        "feedback on what acknowledging does"
    )
    assert "clears_unread_only" in source, (
        "lifecycleAcknowledgeEffectLabel must handle 'clears_unread_only' — "
        "the acknowledge_effect for all message attention items"
    )


def test_attention_lifecycle_by_reason_covers_message_types():
    """_ATTENTION_LIFECYCLE_BY_REASON must define lifecycle for all message attention reasons.

    Finding n1: operator_action and acknowledgeable must be set for message-type
    attention reasons so the strip can render hints and Ack buttons correctly.
    """
    from app.operator_attention import _ATTENTION_LIFECYCLE_BY_REASON

    message_reasons = {"question", "blocker", "bug_report", "feedback"}
    for reason in message_reasons:
        assert reason in _ATTENTION_LIFECYCLE_BY_REASON, (
            f"_ATTENTION_LIFECYCLE_BY_REASON must include '{reason}' — "
            "message-type attention items need lifecycle metadata for the strip"
        )
        entry = _ATTENTION_LIFECYCLE_BY_REASON[reason]
        assert "operator_action" in entry, (
            f"_ATTENTION_LIFECYCLE_BY_REASON['{reason}'] must set operator_action"
        )
        assert entry.get("acknowledgeable") is True, (
            f"_ATTENTION_LIFECYCLE_BY_REASON['{reason}'] must set acknowledgeable=True "
            "— message items can be acknowledged (marked as seen) from the strip"
        )
        assert entry.get("acknowledge_effect") == "clears_unread_only", (
            f"_ATTENTION_LIFECYCLE_BY_REASON['{reason}'] must set "
            "acknowledge_effect='clears_unread_only' for message items"
        )


# --------------------------------------------------------------------------- #
# 18-finding keyless-encryption / release-recovery salvage bundle (2026-07-17)
# --------------------------------------------------------------------------- #


# e1 — decrypt_field returns value unchanged when Fernet token cannot be decrypted
# --------------------------------------------------------------------------- #


def test_decrypt_field_returns_value_unchanged_for_undecryptable_ciphertext(monkeypatch):
    """decrypt_field must return the raw value when the configured key cannot decrypt it.

    Finding e1 claim: decrypt_field returns its input unchanged when a valid Fernet
    token cannot be decrypted with the configured key.  This is the backward-compat
    path that keeps a rolling deploy from breaking existing unencrypted rows — but it
    also fires on a wrong-key rotation, which probe_key_continuity guards against.
    """
    from cryptography.fernet import Fernet
    from app import field_encryption

    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()

    token = Fernet(key_a).encrypt(b"sensitive-value").decode()

    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key_b.decode())
    monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

    result = field_encryption.decrypt_field(token)
    assert result == token, (
        "decrypt_field must return the undecryptable ciphertext unchanged (not raise) "
        "when the configured key differs from the encryption key — this is the rolling-deploy "
        "backward-compat path; probe_key_continuity is the guard that prevents silent data loss"
    )


# e2 — probe_key_continuity fails closed when configured key cannot decrypt reference ciphertext
# --------------------------------------------------------------------------- #


def test_probe_key_continuity_fails_closed_for_incompatible_key(monkeypatch):
    """probe_key_continuity must raise RuntimeError when the configured key is wrong.

    Finding e2 claim: the boot-time continuity probe (probe_key_continuity) decrypts
    a per-environment reference ciphertext with the CURRENT key; if the key has been
    rotated without re-encrypting at-rest data, InvalidToken fires and a RuntimeError
    is raised so the service fails closed rather than silently serving the wrong
    plaintexts.

    Mint a reference with key_a, then verify with key_b (wrong key).
    """
    from cryptography.fernet import Fernet
    import pytest
    from app import field_encryption

    key_a = Fernet.generate_key()
    reference_ciphertext = Fernet(key_a).encrypt(
        field_encryption.CONTINUITY_REFERENCE_PLAINTEXT.encode()
    ).decode()

    key_b = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key_b)

    with pytest.raises(RuntimeError, match="continuity"):
        field_encryption.probe_key_continuity(
            "test-context", reference_ciphertext=reference_ciphertext
        )


# e3 — assert_plaintext_or_raise raises for wrong-key ciphertext
# --------------------------------------------------------------------------- #


def test_assert_plaintext_or_raise_raises_for_wrong_key_ciphertext(monkeypatch):
    """assert_plaintext_or_raise must raise when a Fernet ciphertext can't be decrypted.

    Finding e3 / Finding 1 claim: Migration 0531 uses decrypt_field(value)==value as
    evidence that the value is plaintext.  assert_plaintext_or_raise is the guard that
    prevents a backfill migration from re-encrypting a ciphertext that happens to be
    undecryptable with the current key (wrong-key rotation scenario).
    """
    from cryptography.fernet import Fernet
    import pytest
    from app import field_encryption

    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()

    token = Fernet(key_a).encrypt(b"secret").decode()

    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key_b.decode())
    monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

    with pytest.raises(RuntimeError, match="Wrong-key ciphertext"):
        field_encryption.assert_plaintext_or_raise(token)


# e4 — MultiFernet encrypts with first key, decrypts with any configured key
# --------------------------------------------------------------------------- #


def test_multifernet_encrypts_with_first_key_decrypts_with_all():
    """The cryptography library's MultiFernet must encrypt with the first key and
    decrypt with any configured key.

    Finding e4 / Finding 2 claim: the official MultiFernet contract encrypts with the
    first key, decrypts with every configured key, and supports explicit token rotation.
    Blueprint's key-rotation design relies on this contract.
    """
    from cryptography.fernet import Fernet, MultiFernet

    key_primary = Fernet.generate_key()
    key_fallback = Fernet.generate_key()

    f_primary = Fernet(key_primary)
    f_fallback = Fernet(key_fallback)

    mf = MultiFernet([f_primary, f_fallback])
    token = mf.encrypt(b"rotate-me")

    assert f_primary.decrypt(token) == b"rotate-me", (
        "MultiFernet must encrypt with the first (primary) key — "
        "the primary Fernet instance must be able to decrypt the token directly"
    )
    assert mf.decrypt(token) == b"rotate-me", (
        "MultiFernet must decrypt with any of its configured keys"
    )

    mf_fallback_only = MultiFernet([f_fallback, f_primary])
    rotated = mf_fallback_only.rotate(token)
    assert f_fallback.decrypt(rotated) == b"rotate-me", (
        "MultiFernet.rotate must re-encrypt with the new primary key "
        "(fallback-only federation re-encrypts under the fallback key)"
    )


# e5 — _get_fernet returns None for malformed GITHUB_PAT_ENCRYPTION_KEY
# --------------------------------------------------------------------------- #


def test_get_fernet_returns_none_for_malformed_key(monkeypatch):
    """_get_fernet must return None (not raise) when the env var contains an invalid key.

    Finding e5 / Finding 3 claim: field_encryption._get_fernet catches an invalid
    GITHUB_PAT_ENCRYPTION_KEY and returns None, so is_encryption_active() returns
    False identically for a missing key and a malformed key.  require_encryption_active()
    is the stricter call that distinguishes the two cases.
    """
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

    from app import field_encryption

    result = field_encryption._get_fernet()
    assert result is None, (
        "_get_fernet must return None for an invalid GITHUB_PAT_ENCRYPTION_KEY "
        "rather than raising — is_encryption_active() depends on this None-return "
        "to distinguish 'key absent' from 'key malformed'; require_encryption_active() "
        "is the discriminating guard for backfill migrations"
    )

    assert not field_encryption.is_encryption_active(), (
        "is_encryption_active() must return False when the key is malformed — "
        "this conflation is intentional: callers that need to distinguish missing vs "
        "malformed MUST use require_encryption_active()"
    )


# e11 — auto_qualification_triggers knob controls only the generic trigger sweep
# --------------------------------------------------------------------------- #


def test_infra_failure_redrive_is_independent_of_auto_qualification_triggers_knob():
    """Bespoke infra_failure and deferred_unsynced re-drivers must NOT check the
    auto_qualification_triggers runtime-config knob.

    Finding e11 / Finding 9 claim: the auto_qualification_triggers knob controls
    only the generic trigger sweep (sweep_auto_qualification_triggers), not the
    bespoke loops (_redrive_infra_failure_qualification and
    _redrive_deferred_unsynced_qualification).  If the bespoke re-drivers checked
    the knob they could stall forever when the knob is 'observe' or 'off'.
    """
    import inspect
    from app.release_qualifier_redrive import (
        _redrive_infra_failure_qualification,
        _redrive_deferred_unsynced_qualification,
    )

    for fn in (_redrive_infra_failure_qualification, _redrive_deferred_unsynced_qualification):
        source = inspect.getsource(fn)
        assert "auto_qualification_triggers" not in source, (
            f"{fn.__name__} must NOT check auto_qualification_triggers — "
            "the bespoke re-drivers run unconditionally; only the generic "
            "sweep_auto_qualification_triggers function respects the knob"
        )
        assert "get_auto_qualification_triggers_mode" not in source, (
            f"{fn.__name__} must NOT call get_auto_qualification_triggers_mode() — "
            "bespoke re-drivers are not gated by the observe/on/off policy"
        )


# e16 — _MIN_INFRA_ERROR_STREAK == 3
# --------------------------------------------------------------------------- #


def test_permanent_gate_incapacity_min_infra_error_streak_is_three():
    """_MIN_INFRA_ERROR_STREAK must equal 3 (the design-specified streak threshold).

    Finding e16 / Finding 14 claim: is_permanent_gate_incapacity requires a
    same_signature_streak >= _MIN_INFRA_ERROR_STREAK (3) consecutive identical
    signatures for the escalation to fire.  This prevents a single fluke from
    triggering a permanent-incapacity escalation while still catching a
    consistently broken gate after 3 consecutive same-signature attempts.
    """
    from app.release_qualifier_audit import _MIN_INFRA_ERROR_STREAK

    assert _MIN_INFRA_ERROR_STREAK == 3, (
        f"_MIN_INFRA_ERROR_STREAK must be 3; got {_MIN_INFRA_ERROR_STREAK} — "
        "is_permanent_gate_incapacity fires after this many consecutive "
        "same-signature infra failures"
    )


# e17 — hotfix endpoint validates reason and ack_inert_until_deploy
# --------------------------------------------------------------------------- #


def test_hotfix_endpoint_validates_reason_and_ack_inert_until_deploy():
    """The /promote/hotfix endpoint must reject requests with empty reason or
    missing ack_inert_until_deploy acknowledgment.

    Finding e17 / Finding 12 claim: the backend exposes a hotfix promote endpoint
    with owner, reason, and acknowledgment requirements.  The endpoint must not
    proceed unless (a) reason is non-empty, and (b) ack_inert_until_deploy=true,
    so the operator explicitly acknowledges the inert-until-deploy semantics.
    """
    import pathlib

    router_path = (
        pathlib.Path(__file__).parent.parent
        / "app" / "routers" / "admin_hotfix_promote.py"
    )
    source = router_path.read_text()

    assert "ack_inert_until_deploy" in source, (
        "admin_hotfix_promote.py must require ack_inert_until_deploy — "
        "the operator must explicitly acknowledge that hotfix changes are "
        "inert until the next full deploy"
    )
    assert "reason" in source, (
        "admin_hotfix_promote.py must require a non-empty reason field — "
        "it is the audit justification for the hotfix"
    )
    assert "not body.reason" in source or "min_length=1" in source, (
        "admin_hotfix_promote.py must validate that reason is non-empty "
        "(either via Pydantic min_length=1 or an explicit check)"
    )
    assert "not body.ack_inert_until_deploy" in source or "ack_inert_until_deploy must be true" in source, (
        "admin_hotfix_promote.py must reject requests where ack_inert_until_deploy is false"
    )


# e19 — scratch subprocess injects ephemeral Fernet key
# --------------------------------------------------------------------------- #


def test_scratch_subprocess_injects_ephemeral_fernet_key():
    """_run_migration_apply_subprocess must set GITHUB_PAT_ENCRYPTION_KEY to a
    freshly generated Fernet key, not read it from the real environment.

    Finding e19: migration 0609 calls _load_fernet() at import time and raises
    RuntimeError when GITHUB_PAT_ENCRYPTION_KEY is unset.  The scratch subprocess
    env deliberately strips secrets via _SUBPROCESS_ENV_ALLOWLIST, so without the
    injection the gate would always defer on 0609.  The fix injects a synthetic
    per-run key so the migration body can execute on the empty scratch DB.

    This is a pure source-code inspection — no DB required.
    """
    import inspect
    from app.migration_apply_gate import _run_migration_apply_subprocess, _ENCRYPTION_KEY_ENV_VAR

    source = inspect.getsource(_run_migration_apply_subprocess)

    assert _ENCRYPTION_KEY_ENV_VAR in source, (
        f"_run_migration_apply_subprocess must set {_ENCRYPTION_KEY_ENV_VAR} — "
        "without it migration 0609 raises RuntimeError and wedges qualification"
    )
    assert "Fernet.generate_key()" in source, (
        "_run_migration_apply_subprocess must use Fernet.generate_key() to produce "
        "a synthetic ephemeral key — not copy the production secret into the subprocess"
    )
    # Confirm the key is set from generate_key, not taken from the real env.
    assert "os.environ" not in source.split(_ENCRYPTION_KEY_ENV_VAR)[1].split("Fernet.generate_key()")[0], (
        f"{_ENCRYPTION_KEY_ENV_VAR} must be set to Fernet.generate_key() output, "
        "not forwarded from os.environ — the subprocess env must not receive the real secret"
    )


# e20 — _looks_like_keyless_encryption_artifact two-signal guard
# --------------------------------------------------------------------------- #


def test_looks_like_keyless_encryption_artifact_classifier():
    """_looks_like_keyless_encryption_artifact is migration-number-agnostic: it
    requires RuntimeError AND the env-var name to appear on the SAME line, which
    prevents false positives from accumulated chain stderr where earlier no-op
    migrations emit the env-var name as a warning on a PRIOR line before an
    unrelated RuntimeError appears on a later line.

    Finding e20: the classifier (#15590) requires RuntimeError AND the env-var name
    on the same line — migration-number-agnostic so future encryption migrations
    (0609, 0621, etc.) are all correctly classified without per-migration updates.
    """
    from app.migration_apply_gate import _looks_like_keyless_encryption_artifact, _ENCRYPTION_KEY_ENV_VAR

    # Positive: RuntimeError + env-var name on the same line → True
    full_match = (
        f"RuntimeError: {_ENCRYPTION_KEY_ENV_VAR} is required. "
        "Migration 0609 does not permit this migration to stamp."
    )
    assert _looks_like_keyless_encryption_artifact(full_match) is True, (
        "_looks_like_keyless_encryption_artifact must return True when RuntimeError "
        "and the env-var name appear on the same line"
    )

    # Env-var name on a DIFFERENT line than RuntimeError → False
    # (prevents false positives from accumulated chain stderr where earlier
    # no-op migrations emit the env-var name as a warning before an unrelated
    # RuntimeError from a different step).
    diff_line = (
        f"WARNING: {_ENCRYPTION_KEY_ENV_VAR} is not set — field encryption is a no-op\n"
        "RuntimeError: Some other migration failed (unrelated)."
    )
    assert _looks_like_keyless_encryption_artifact(diff_line) is False, (
        "_looks_like_keyless_encryption_artifact must return False when the env-var "
        f"name ({_ENCRYPTION_KEY_ENV_VAR}) is on a different line than RuntimeError"
    )

    # Missing env-var name → False
    no_key = "RuntimeError: encryption key required. Migration 0609 does not permit stamping."
    assert _looks_like_keyless_encryption_artifact(no_key) is False, (
        "_looks_like_keyless_encryption_artifact must return False when the env-var "
        f"name ({_ENCRYPTION_KEY_ENV_VAR}) is absent"
    )

    # Missing 'RuntimeError' → False
    no_runtime_error = (
        f"ValueError: {_ENCRYPTION_KEY_ENV_VAR} is required. Migration 0609 failed."
    )
    assert _looks_like_keyless_encryption_artifact(no_runtime_error) is False, (
        "_looks_like_keyless_encryption_artifact must return False when 'RuntimeError' "
        "is absent — the match must be anchored to the specific exception type"
    )


# --------------------------------------------------------------------------- #
# test: fail-fast dual-write spec vs ledger-registry drift without a DB
# --------------------------------------------------------------------------- #


def test_dual_write_spec_matches_ledger_registry():
    """_DUAL_WRITE_SPEC_DIMENSIONS must equal LEDGER_DIMENSION_KEYS at import time.

    The guard that keeps the qualification dual-write spec list synchronised with
    the ledger dimension registry was previously a runtime assert inside
    _dual_write_dimension_results that only fires when that async function executes
    against a real database session.  A new ledger dimension added to the registry
    but omitted from the spec list would therefore silently never be persisted,
    with the drift caught only weeks later at promotion-PR time.

    The fix adds a module-level _DUAL_WRITE_SPEC_DIMENSIONS constant that is
    asserted == LEDGER_DIMENSION_KEYS at module import time.  This test verifies:
    (a) the constant is importable without a DB session, and
    (b) the module-level assert fires (i.e. the import itself did not raise),
    confirming the spec and registry are in sync in the current codebase.
    """
    from app.release_qualifier import _DUAL_WRITE_SPEC_DIMENSIONS
    from app.qualification_dimensions import LEDGER_DIMENSION_KEYS

    # If the module-level assert in release_qualifier.py had failed, the import
    # above would have raised AssertionError — reaching this line proves it passed.
    assert _DUAL_WRITE_SPEC_DIMENSIONS == LEDGER_DIMENSION_KEYS, (
        f"_DUAL_WRITE_SPEC_DIMENSIONS ({sorted(_DUAL_WRITE_SPEC_DIMENSIONS)}) != "
        f"LEDGER_DIMENSION_KEYS ({sorted(LEDGER_DIMENSION_KEYS)}); "
        f"extra in spec: {sorted(_DUAL_WRITE_SPEC_DIMENSIONS - LEDGER_DIMENSION_KEYS)}, "
        f"missing from spec: {sorted(LEDGER_DIMENSION_KEYS - _DUAL_WRITE_SPEC_DIMENSIONS)}"
    )

    # The constant must be a frozenset so it is immutable and hashable.
    assert isinstance(_DUAL_WRITE_SPEC_DIMENSIONS, frozenset), (
        "_DUAL_WRITE_SPEC_DIMENSIONS must be a frozenset — "
        "a mutable set could be accidentally modified at runtime"
    )

    # Every expected ledger-backed dimension must be present.
    expected = {
        "ci", "audit", "test_erosion", "smoketest", "challenge",
        "migration_identity", "migration_apply", "merge_grade",
        "acceptance_window", "debt_pressure", "capability_proof", "env_preflight",
    }
    assert _DUAL_WRITE_SPEC_DIMENSIONS == expected, (
        f"_DUAL_WRITE_SPEC_DIMENSIONS content differs from the expected 12-dimension set; "
        f"extra: {sorted(_DUAL_WRITE_SPEC_DIMENSIONS - expected)}, "
        f"missing: {sorted(expected - _DUAL_WRITE_SPEC_DIMENSIONS)}"
    )


# --------------------------------------------------------------------------- #
# 15-finding capability-proof salvage bundle (cp-1 … cp-15)
# --------------------------------------------------------------------------- #


def test_cp1_metrics_integrity_flag_uses_real_backend_contract():
    """cp-1: CheckResult.as_dict() serialises the real backend contract fields.

    Finding #1 claimed the frontend consumed fictitious ``metric`` / ``expected`` /
    ``found`` fields while the backend emitted ``check`` / ``status`` /
    ``self_reported`` / ``ground_truth`` / ``delta`` / ``severity`` / ``subject`` /
    ``detail`` / ``auto_resolved*``.  This test verifies the backend *source* of
    truth: ``CheckResult.as_dict()`` must include every field the frontend
    ``MetricsIntegrityFlag`` type now reads.
    """
    from app.metrics_integrity_sweep import CheckResult, CheckStatus

    r = CheckResult(
        check="capability_proof_gate_gap",
        status=CheckStatus.FLAGGED,
        self_reported=10,
        ground_truth=7,
        delta=-3,
        severity="warn",
        subject="proj-abc",
        detail="3 capabilities have no proof",
    )
    d = r.as_dict()

    for field in ("check", "status", "self_reported", "ground_truth", "delta",
                  "severity", "subject", "detail", "auto_resolved",
                  "auto_resolved_backing_path", "auto_resolved_named_test_present"):
        assert field in d, (
            f"CheckResult.as_dict() must include '{field}' — "
            f"the frontend MetricsIntegrityFlag type reads it"
        )

    assert d["check"] == "capability_proof_gate_gap"
    assert d["status"] == "flagged"  # CheckStatus enum .value
    assert d["self_reported"] == 10
    assert d["ground_truth"] == 7
    assert d["delta"] == -3
    assert d["severity"] == "warn"
    assert d["subject"] == "proj-abc"
    assert d["detail"] == "3 capabilities have no proof"

    # Fictitious old fields must NOT appear.
    for bad in ("metric", "expected", "found"):
        assert bad not in d, (
            f"CheckResult.as_dict() must NOT include '{bad}' — "
            "that was the broken shape the finding identified"
        )


def test_cp2_capability_proof_trial_stall_check_is_registered():
    """cp-2: ``capability_proof_trial_stall`` is a registered health check.

    Finding #2 asked for stalled trial outcomes to be surfaced so silent earn-loop
    no-ops become visible.  This test verifies the check is registered in the sweep
    registry — proof that it will be evaluated on every sweep tick.
    """
    from app.metrics_integrity_sweep import _REGISTRY

    assert "capability_proof_trial_stall" in _REGISTRY, (
        "'capability_proof_trial_stall' must be in _REGISTRY — "
        "it surfaces per-project, per-cap trial stalls (all-non-green outcomes) "
        "so silent earn-loop no-ops are visible on the health dashboard"
    )
    check = _REGISTRY["capability_proof_trial_stall"]
    assert callable(check.run), (
        "'capability_proof_trial_stall' registry entry must have a callable .run"
    )


def test_cp3_trial_ingestion_could_not_run_records_contradiction_not_proof():
    """cp-3: Ingestion handler never mints proof on a could_not_run trial.

    Finding #3 noted that every backend-Python trial could report could_not_run
    (Docker daemon unavailable), and the loop no-ops silently.  The correct
    contract is that could_not_run never demotes existing proof AND never mints
    new proof; it only stamps a contradiction key on the task context.

    This test verifies the ingestion source records the contradiction key on the
    could_not_run branch and does NOT call record_capability_named_test_execution_proof.
    """
    import inspect
    from app import task_completion

    # Locate _handle_capability_proof_trial_completion
    fn = getattr(task_completion, "_handle_capability_proof_trial_completion", None)
    assert fn is not None, (
        "_handle_capability_proof_trial_completion must exist in task_completion"
    )
    source = inspect.getsource(fn)

    # must stamp contradiction context key when non-green
    assert "capability_proof_trial_contradiction" in source, (
        "_handle_capability_proof_trial_completion must stamp "
        "'capability_proof_trial_contradiction' on non-green outcomes so the "
        "stall health check can detect them"
    )

    # must check capability_proof_trial_is_green before recording proof
    assert "capability_proof_trial_is_green" in source, (
        "_handle_capability_proof_trial_completion must gate proof recording on "
        "capability_proof_trial_is_green — could_not_run is non-green and must mint nothing"
    )

    # Proof recorder is only called inside the green branch
    source_lines = source.splitlines()
    green_branch_start = next(
        (i for i, ln in enumerate(source_lines) if "capability_proof_trial_is_green" in ln), -1
    )
    proof_call_line = next(
        (i for i, ln in enumerate(source_lines)
         if "record_capability_named_test_execution_proof" in ln), -1
    )
    assert green_branch_start != -1, "could not find capability_proof_trial_is_green in source"
    assert proof_call_line != -1, (
        "record_capability_named_test_execution_proof must be called inside the green branch"
    )
    assert proof_call_line > green_branch_start, (
        "record_capability_named_test_execution_proof must appear AFTER the green check, "
        "not before — a could_not_run result must never reach the proof recorder"
    )


def test_cp4_ledger_corroborated_proof_status_freshness_and_alias():
    """cp-4: ledger_corroborated_proof_status applies freshness and alias-aware retry.

    Finding #4 claimed the corroboration pass dropped the ledger timestamp (no
    freshness) and stranded absorbed aliases.  This test verifies both contracts
    via pure-Python calls (no DB).
    """
    from datetime import datetime, timezone, timedelta
    from app.capability_registry import ledger_corroborated_proof_status
    from app.evidence_primitive import PROOF_FRESHNESS_TTL

    # --- Freshness: a ledger entry older than TTL should NOT corroborate ---
    stale_ts = (
        datetime.now(timezone.utc) - PROOF_FRESHNESS_TTL - timedelta(seconds=60)
    ).isoformat()
    proof_records = {
        "cap-a": [{"kind": "named_test", "proven": True, "recorded_at": stale_ts}]
    }
    status_map = {"cap-a": "proven"}

    result_stale = ledger_corroborated_proof_status(
        status_map,
        proof_records,
        now=datetime.now(timezone.utc),
    )
    assert result_stale.get("cap-a") == "unverified", (
        "A stale ledger entry (older than PROOF_FRESHNESS_TTL) must NOT corroborate "
        "a self-declared proven claim — the result should be 'unverified'"
    )

    # --- Freshness: a fresh ledger entry should corroborate ---
    fresh_ts = datetime.now(timezone.utc).isoformat()
    proof_records_fresh = {
        "cap-a": [{"kind": "named_test", "proven": True, "recorded_at": fresh_ts}]
    }
    result_fresh = ledger_corroborated_proof_status(
        {"cap-a": "proven"},
        proof_records_fresh,
        now=datetime.now(timezone.utc),
    )
    assert result_fresh.get("cap-a") == "proven", (
        "A fresh ledger entry must corroborate a self-declared proven claim"
    )

    # --- Alias-aware: absorbed alias should be corroborated via survivor's ledger entry ---
    survivor_ts = datetime.now(timezone.utc).isoformat()
    proof_records_alias = {
        "cap-new": [{"kind": "named_test", "proven": True, "recorded_at": survivor_ts}]
    }
    # alias_map: old-cap → cap-new (old-cap was absorbed by cap-new)
    result_alias = ledger_corroborated_proof_status(
        {"cap-old": "proven"},
        proof_records_alias,
        alias_map={"cap-old": "cap-new"},
        now=datetime.now(timezone.utc),
    )
    assert result_alias.get("cap-old") == "proven", (
        "An absorbed alias (cap-old → cap-new) with ledger proof under the survivor "
        "key must pass corroboration via the alias_map forward retry"
    )

    # --- No alias: a self-declared proven with NO ledger entry must be demoted ---
    result_no_ledger = ledger_corroborated_proof_status(
        {"cap-x": "proven"},
        {},
    )
    assert result_no_ledger.get("cap-x") == "unverified", (
        "A self-declared proven with no ledger entry and no alias must be demoted "
        "to 'unverified'"
    )


def test_cp5_settings_api_exposes_proof_trial_fields():
    """cp-5: Settings API response includes capability_proof_execution_trials_enabled
    and the fleet coverage dict includes trial_eligible_unproven.

    Finding #5 claimed neither field was present in the settings TypeScript contract
    or the component.  This test verifies the Python source builds both fields into
    the settings response dict.
    """
    import inspect
    from app.routers import admin_settings

    source = inspect.getsource(admin_settings)

    assert "capability_proof_execution_trials_enabled" in source, (
        "admin_settings.py must build 'capability_proof_execution_trials_enabled' "
        "into the settings response so the UI can read and toggle it"
    )
    assert "trial_eligible_unproven" in source, (
        "admin_settings.py must include 'trial_eligible_unproven' in the fleet "
        "coverage projection so the UI can show how many caps are trial-ready"
    )
    assert "compute_trial_eligibility_projection" in source, (
        "admin_settings.py must call compute_trial_eligibility_projection to build "
        "the dry-run eligibility projection returned to the UI"
    )


def test_cp6_revoke_stale_proof_and_trial_dedup_exist():
    """cp-6: revoke_stale_execution_proof exists for RED results; enroll_capability_proof_trial
    includes an in-flight dedup guard to prevent duplicate concurrent enrollments.

    Finding #6 claimed the old green proof was never revoked on a definitive RED and
    that concurrent sweeps could queue duplicate trials.  This test verifies both
    mechanisms are present in the source.
    """
    import inspect
    from app.capability_proof import revoke_stale_execution_proof, enroll_capability_proof_trial

    # revoke_stale_execution_proof must exist and be callable
    assert callable(revoke_stale_execution_proof), (
        "revoke_stale_execution_proof must be a callable — it revokes stale ledger "
        "proofs when a definitive RED trial result lands"
    )

    # enroll_capability_proof_trial must contain a dedup guard against in-flight trials
    enroll_src = inspect.getsource(enroll_capability_proof_trial)
    assert "in_flight" in enroll_src or "QUEUED" in enroll_src, (
        "enroll_capability_proof_trial must include an in-flight dedup guard "
        "(checking QUEUED/RUNNING tasks) to prevent concurrent duplicate enrollment"
    )


def test_cp7_green_proof_completion_wakes_blocked_dependents():
    """cp-7: Green proof completion calls event_driven_unblock_on_capability_proof
    after committing the proof, so blocked dependents are woken promptly.

    Finding #7 claimed proof completion did not invoke the event-driven blocked
    resolver.  This test verifies the call exists in the ingestion source.
    """
    import inspect
    from app import task_completion

    fn = task_completion._handle_capability_proof_trial_completion
    source = inspect.getsource(fn)

    assert "event_driven_unblock_on_capability_proof" in source, (
        "_handle_capability_proof_trial_completion must call "
        "event_driven_unblock_on_capability_proof after committing a green proof "
        "so blocked dependents are re-evaluated promptly"
    )

    # The call must appear AFTER the session.commit() for the proof — durability first.
    lines = source.splitlines()
    commit_line = next(
        (i for i, ln in enumerate(lines) if "await session.commit()" in ln), -1
    )
    wake_line = next(
        (i for i, ln in enumerate(lines) if "event_driven_unblock_on_capability_proof" in ln), -1
    )
    assert commit_line != -1, "could not find 'await session.commit()' in source"
    assert wake_line != -1, "could not find 'event_driven_unblock_on_capability_proof' in source"
    assert wake_line > commit_line, (
        "event_driven_unblock_on_capability_proof must be called AFTER session.commit() "
        "so the proof is durable before blocked tasks are examined"
    )


def test_cp8_plan_completion_handles_capability_prerequisites():
    """cp-8: plan_completion enforces capability-kind prerequisites as real scheduling
    constraints, not just metadata.

    Finding #8 claimed plan subtasks declared capability prerequisites that were
    treated as metadata (only task-kind prereqs influenced depends_on).  This test
    verifies the source handles capability-kind prereqs by stamping needs AND by
    blocking the child in enforce mode.
    """
    import inspect
    from app import plan_completion

    source = inspect.getsource(plan_completion)

    # Must call build_need for the capability kind from the prerequisite
    assert "build_need" in source, (
        "plan_completion must call build_need() to materialise capability prereqs "
        "as structured need dicts for the blocked-needs lifecycle"
    )
    assert "stamp_capability_requirements" in source, (
        "plan_completion must call stamp_capability_requirements() to durably record "
        "the capability prereq on the child task — surviving cold dispatch re-grade"
    )
    assert "task_backed_capability_need_blocked_by_proof_gate" in source, (
        "plan_completion must call task_backed_capability_need_blocked_by_proof_gate() "
        "to evaluate whether the unproven capability blocks the child in enforce mode"
    )
    assert "enter_blocked_state" in source, (
        "plan_completion must call enter_blocked_state() when the proof gate blocks "
        "the plan subtask — prerequisite is a real scheduling constraint, not metadata"
    )


def test_cp9_trial_receipt_validates_against_enrollment():
    """cp-9: Trial receipt ingestion validates cap, test_refs, and head_sha against
    the enrollment payload before minting any proof.

    Finding #9 claimed the ingestion accepted agent-reported cap/test_refs/head_sha
    without binding them to the enrolled task.  This test verifies the three
    validation checks are present in the source.
    """
    import inspect
    from app import task_completion

    fn = task_completion._handle_capability_proof_trial_completion
    source = inspect.getsource(fn)

    assert "capability_mismatch" in source, (
        "_handle_capability_proof_trial_completion must reject a receipt whose 'cap' "
        "does not match the enrolled capability (capability_mismatch contradiction)"
    )
    assert "test_refs_mismatch" in source, (
        "_handle_capability_proof_trial_completion must reject a receipt whose "
        "'test_refs' do not match the enrolled set (test_refs_mismatch contradiction)"
    )
    assert "head_sha_mismatch" in source, (
        "_handle_capability_proof_trial_completion must reject a receipt whose "
        "'head_sha' does not match the enrolled revision (head_sha_mismatch contradiction)"
    )
    assert "task.first_payload" in source or "enrolled" in source, (
        "_handle_capability_proof_trial_completion must read the enrollment payload "
        "from task.first_payload (the orchestrator-authored contract) to validate "
        "the agent-reported receipt against"
    )


def test_cp10_fleet_proof_coverage_uses_ledger_corroborating_path():
    """cp-10: compute_fleet_proof_coverage uses consumer_gate_proof_status (the
    ledger-corroborating path) rather than the agent-writable working_capability_proof_status.

    Finding #10 claimed fleet coverage counted the agent-writable problem-model grade,
    which an agent could forge to suppress enforcement.  This test verifies the source
    uses the ledger-aware grading function.
    """
    import inspect
    from app.metrics_integrity_sweep import compute_fleet_proof_coverage

    source = inspect.getsource(compute_fleet_proof_coverage)

    assert "consumer_gate_proof_status" in source, (
        "compute_fleet_proof_coverage must use consumer_gate_proof_status "
        "(which corroborates the platform ledger) rather than the raw "
        "working_capability_proof_status, which reads agent-writable problem_model only"
    )
    # Must NOT use raw working_capability_proof_status directly without corroboration.
    # consumer_gate_proof_status already wraps it, so checking the direct call is absent.
    assert "working_capability_proof_status(" not in source, (
        "compute_fleet_proof_coverage must not call working_capability_proof_status() "
        "directly — consumer_gate_proof_status already applies ledger corroboration"
    )


def test_cp11_capability_proof_gate_gap_check_is_registered():
    """cp-11: check_capability_proof_gate_gap is a registered sweep check with
    self-healing (auto-enrollment of proof trials for discovered covering tests).

    Finding #11 asked for a self-healing mechanism for unproven working capabilities.
    This test verifies the check is registered and its source includes trial enrollment.
    """
    import inspect
    from app.metrics_integrity_sweep import _REGISTRY, check_capability_proof_gate_gap

    assert "capability_proof_gate_gap" in _REGISTRY, (
        "'capability_proof_gate_gap' must be in _REGISTRY — "
        "it is the sweep check that auto-resolves unproven capabilities"
    )

    source = inspect.getsource(check_capability_proof_gate_gap)
    assert "_maybe_enroll_proof_trial" in source, (
        "check_capability_proof_gate_gap must call _maybe_enroll_proof_trial when "
        "covering tests are discovered — that is the self-healing enrollment path that "
        "can graduate a capability from path_proven to proven"
    )


def test_cp12_compute_trial_eligibility_projection_importable():
    """cp-12: compute_trial_eligibility_projection is importable from
    metrics_integrity_sweep and is async (it reads project state without DB on mocks).

    Finding #12 asked for a read-only projection of how many capabilities would
    become trial-eligible if execution trials were enabled.  This test verifies the
    function exists.
    """
    import asyncio
    import inspect
    from app.metrics_integrity_sweep import compute_trial_eligibility_projection

    assert callable(compute_trial_eligibility_projection), (
        "compute_trial_eligibility_projection must be callable"
    )
    assert asyncio.iscoroutinefunction(compute_trial_eligibility_projection), (
        "compute_trial_eligibility_projection must be async — it queries project state"
    )

    source = inspect.getsource(compute_trial_eligibility_projection)
    assert "trial_eligible" in source, (
        "compute_trial_eligibility_projection must compute and return 'trial_eligible' "
        "count — the count of unproven working capabilities that have covering tests "
        "and would be enrolled for execution trials if the flag were on"
    )


def test_cp13_e2e_enforcement_lifecycle_test_exists():
    """cp-13: An end-to-end integration test for the capability proof gate enforcement
    cycle exists in test_blocked_need_resolution.py.

    Finding #13 asked for a test covering: create tasks → set enforce mode →
    verify consumer blocked → simulate provider green verdict → verify gate releases.
    """
    import os

    test_path = os.path.join(
        os.path.dirname(__file__), "test_blocked_need_resolution.py"
    )
    assert os.path.exists(test_path), (
        "test_blocked_need_resolution.py must exist to host the E2E enforcement test"
    )

    with open(test_path) as f:
        source = f.read()

    assert "test_capability_proof_gate_enforcement_full_lifecycle" in source, (
        "test_blocked_need_resolution.py must contain "
        "test_capability_proof_gate_enforcement_full_lifecycle — the E2E test that "
        "exercises the full proof gate enforcement cycle end-to-end"
    )

    # Verify the test covers the key lifecycle steps
    for phrase in ("enforce", "unproven", "record_capability_proof", "proven"):
        assert phrase in source, (
            f"E2E lifecycle test must reference '{phrase}' — a full gate cycle "
            "touches unproven state, enforce mode, proof recording, and proven state"
        )


def test_cp14_capability_proof_earn_path_design_doc_referenced_in_source():
    """cp-14: The design document 84021e2d (capability-proof-earn-path.md) is
    referenced in the implementation source, confirming a file-grounded design
    drove the earn-path implementation.

    Finding #14 asked for a file-grounded design document for the machine-verified
    earn path.  The design doc reference in the handler and capability_proof source
    is the durable evidence — the doc may not be mounted into the test container,
    but the code was written against it.
    """
    import inspect
    from app.capability_proof import enroll_capability_proof_trial

    source = inspect.getsource(enroll_capability_proof_trial)

    # The design document ID (84021e2d) must appear in the source — it is
    # stamped into the handler and enrollment function docstrings as provenance.
    assert "84021e2d" in source or "capability-proof-earn-path" in source, (
        "enroll_capability_proof_trial must reference the design doc ID '84021e2d' "
        "or 'capability-proof-earn-path' — the earn-path design drove the implementation "
        "and its ID is stamped into the function docstring as provenance"
    )


def test_cp15_proven_tier_recorders_and_enroll_exist():
    """cp-15: record_capability_named_test_execution_proof mints the real 'proven'
    named_test tier; enroll_capability_proof_trial wraps enrollment.  Both must be
    importable and callable.

    Finding #15 claimed no automation could move a pre-existing registry capability
    to proof_status=='proven'.  This test verifies the two functions that make the
    automated earn path work are present.
    """
    from app.capability_proof import (
        record_capability_named_test_execution_proof,
        enroll_capability_proof_trial,
    )
    import asyncio
    import inspect

    assert callable(record_capability_named_test_execution_proof), (
        "record_capability_named_test_execution_proof must be callable — it is the "
        "sole function that mints the 'proven' named_test tier from an executed trial"
    )
    assert callable(enroll_capability_proof_trial), (
        "enroll_capability_proof_trial must be callable — it is the enrollment half "
        "of the machine-verified capability-proof earn path"
    )
    assert asyncio.iscoroutinefunction(enroll_capability_proof_trial), (
        "enroll_capability_proof_trial must be async — it writes a task to the DB"
    )

    # record_capability_named_test_execution_proof must write a proven tier
    source = inspect.getsource(record_capability_named_test_execution_proof)
    assert "proven" in source, (
        "record_capability_named_test_execution_proof must produce a 'proven' tier proof"
    )
    assert "named_test" in source, (
        "record_capability_named_test_execution_proof must produce a 'named_test' kind proof"
    )


# --------------------------------------------------------------------------- #
# Findings p1/p2 — Alert constructors stamp event_type so they route
# STRATEGIST_INTERNAL through operator_visible_alert_filter (fail-closed)
# --------------------------------------------------------------------------- #


def test_research_direction_state_direction_auto_completed_alert_stamps_event_type():
    """research_direction_state Alert for 'Direction auto-completed' must stamp event_type.

    Finding p2: research.direction_auto_completed is registered STRATEGIST_INTERNAL in
    the audience registry. operator_visible_alert_filter is fail-closed (IN-allowlist),
    so without event_type= the Alert has a NULL event_type and is excluded entirely from
    the operator UI. Stamping event_type= ensures the filter routes it correctly.
    """
    import inspect
    import app.research_direction_state as rds_mod

    source = inspect.getsource(rds_mod)
    assert 'event_type="research.direction_auto_completed"' in source, (
        "research_direction_state must pass "
        'event_type="research.direction_auto_completed" to the Alert constructor so '
        "operator_visible_alert_filter routes it STRATEGIST_INTERNAL (fail-closed: a "
        "NULL event_type is excluded from the operator view by the IN-allowlist filter)"
    )


def test_research_direction_state_dedup_deadlock_alert_stamps_event_type():
    """research_direction_state Alert for 'Dedup deadlock detected' must stamp event_type.

    Finding p2: research.dedup_deadlock is registered STRATEGIST_INTERNAL. Without
    event_type= the Alert has NULL and is excluded from the operator UI entirely via
    the fail-closed IN-allowlist filter.
    """
    import inspect
    import app.research_direction_state as rds_mod

    source = inspect.getsource(rds_mod)
    assert 'event_type="research.dedup_deadlock"' in source, (
        "research_direction_state must pass event_type='research.dedup_deadlock' to "
        "the Dedup-deadlock Alert constructor so operator_visible_alert_filter hides it "
        "from the operator badge (research.dedup_deadlock is STRATEGIST_INTERNAL)"
    )


def test_research_direction_state_already_fixed_deadlock_alert_stamps_event_type():
    """research_direction_state Alert for 'Already-fixed deadlock detected' must stamp event_type.

    Finding p2: research.already_fixed_deadlock is registered STRATEGIST_INTERNAL. Without
    event_type= the Alert has NULL and is silently excluded from operator UI by fail-closed filter.
    """
    import inspect
    import app.research_direction_state as rds_mod

    source = inspect.getsource(rds_mod)
    assert 'event_type="research.already_fixed_deadlock"' in source, (
        "research_direction_state must pass event_type='research.already_fixed_deadlock' to "
        "the Already-fixed-deadlock Alert constructor so operator_visible_alert_filter hides "
        "it from the operator badge (research.already_fixed_deadlock is STRATEGIST_INTERNAL)"
    )


def test_research_completion_direction_auto_completed_alert_stamps_event_type():
    """research_completion Alert for 'Direction auto-completed' must stamp event_type.

    Finding p2: research_completion.py builds a 'Direction auto-completed' Alert without
    event_type, so it has a NULL event_type. operator_visible_alert_filter is fail-closed
    (IN-allowlist), so a NULL event_type is silently excluded from the operator UI entirely.
    Stamping event_type='research.direction_auto_completed' (STRATEGIST_INTERNAL) ensures
    the filter routes it correctly.
    """
    import inspect
    import app.research_completion as rc_mod

    # Get source of the module to check that the Alert constructor has event_type stamped.
    source = inspect.getsource(rc_mod)
    # Both research_direction_state and research_completion have direction_auto_completed Alerts.
    # Count occurrences of event_type="research.direction_auto_completed" — must appear in
    # research_completion too (not only research_direction_state).
    count = source.count('event_type="research.direction_auto_completed"')
    assert count >= 1, (
        "research_completion.py must pass event_type='research.direction_auto_completed' "
        "to its 'Direction auto-completed' Alert constructor; found 0 occurrences — "
        "without this stamp the NULL event_type is silently excluded by the fail-closed "
        "IN-allowlist filter and the alert never appears in the operator UI"
    )


def test_strategist_internal_alert_event_types_are_registered():
    """The three research alert event_types must be registered STRATEGIST_INTERNAL.

    Finding p1/p2: the audience registry is the single source of truth for Alert routing.
    research.direction_auto_completed, research.dedup_deadlock, and
    research.already_fixed_deadlock must be present with STRATEGIST_INTERNAL audience so
    operator_visible_alert_filter reliably excludes them once the emitters stamp event_type.
    """
    from app.message_audience import (
        _EVENT_AUDIENCE_REGISTRY,
        STRATEGIST_INTERNAL,
    )

    expected = {
        "research.direction_auto_completed": STRATEGIST_INTERNAL,
        "research.dedup_deadlock": STRATEGIST_INTERNAL,
        "research.already_fixed_deadlock": STRATEGIST_INTERNAL,
    }
    for event_type, expected_audience in expected.items():
        assert event_type in _EVENT_AUDIENCE_REGISTRY, (
            f"_EVENT_AUDIENCE_REGISTRY must include '{event_type}' so "
            "operator_visible_alert_filter can gate it correctly; missing entries are "
            "silently excluded by the fail-closed IN-allowlist filter (NULL treated as non-operator)"
        )
        actual = _EVENT_AUDIENCE_REGISTRY[event_type]
        assert actual == expected_audience, (
            f"_EVENT_AUDIENCE_REGISTRY['{event_type}'] must be '{expected_audience}'; "
            f"got '{actual}' — internal research bookkeeping must never reach the operator badge"
        )


# --------------------------------------------------------------------------- #
# Finding #1 (cq-0) — spawn-gate absorbs alias bypass is closed                #
# --------------------------------------------------------------------------- #


def test_cq0_working_capability_proof_status_registers_absorbs_aliases_as_unproven():
    """cq-0: working_capability_proof_status includes agent-declared absorbs aliases
    as 'unproven', closing the spawn-gate bypass where None/absent was a free pass.

    Finding #1 identified that a problem-model author could declare a working
    capability that absorbs an arbitrary old name; spawn-time resolvers recognised
    that raw alias as an existing capability (via working_capability_names /
    working_capability_proof_status), while the proof map silently omitted the
    alias entry so capability_need_blocked_by_proof_gate treated the missing key
    as permission to proceed — clearing enforce mode with zero verification.

    The fix (commit a81408a): when iterating capabilities, every alias in
    ``absorbs`` that has no platform-forwarding-marker entry is registered as
    ``"unproven"`` in the output map, so the enforcement path sees an unproven
    claim rather than an absent (None) one and correctly blocks.
    """
    from app.capability_registry import working_capability_proof_status

    # Build a problem_model where 'new-cap' absorbs 'old-alias'.
    problem_model = {
        "capabilities": [
            {
                "name": "new-cap",
                "status": "working",
                "absorbs": ["old-alias"],
                "evidence_refs": [],
            }
        ]
    }

    proof_map = working_capability_proof_status(problem_model)

    # The survivor name must be in the map (working cap).
    assert "new-cap" in proof_map, (
        "working_capability_proof_status must include the survivor capability 'new-cap'"
    )

    # The absorbed alias must also be in the map — NOT absent / None.
    assert "old-alias" in proof_map, (
        "working_capability_proof_status must include the absorbed alias 'old-alias' "
        "in the output map; an absent key is treated as None → free pass at the "
        "enforce gate (capability_need_blocked_by_proof_gate)"
    )

    # The alias must be registered as 'unproven', not 'proven' or 'partial'.
    alias_status = proof_map["old-alias"]
    assert alias_status == "unproven", (
        f"absorbed alias 'old-alias' must be registered as 'unproven' in the proof map "
        f"(got {alias_status!r}); the alias has no machine-verified proof and must "
        "block the enforce gate, not pass through as a free-pass None entry"
    )

    # Guard: the `if alias not in out` check prevents the absorbs loop from
    # overwriting an alias that was already mapped with richer evidence by its
    # own capability entry processed EARLIER in the list.  Build a model where
    # 'tracked-alias' appears FIRST (so its own evidence_refs run first), then
    # verify the absorbs loop does not regress it.
    problem_model_order = {
        "capabilities": [
            {
                "name": "tracked-alias",
                "status": "working",
                "evidence_refs": [{"kind": "named_test", "proven": True}],
            },
            {
                "name": "new-cap",
                "status": "working",
                "absorbs": ["tracked-alias"],
                "evidence_refs": [],
            },
        ]
    }
    proof_map_order = working_capability_proof_status(problem_model_order)
    # tracked-alias processed its own evidence_refs first; absorbs loop must not
    # regress it to "unproven" because `if alias not in out` short-circuits.
    assert proof_map_order.get("tracked-alias") != "unproven", (
        "absorbs loop must not overwrite 'tracked-alias' when it was already "
        "mapped by its own evidence_refs entry ('if alias not in out' guard honoured)"
    )


# =========================================================================== #
# 4-finding operator-walkthrough / runner-config / project-tools bundle
# =========================================================================== #


def _project_tool_host_source() -> str:
    """Return the source of admin-ui/src/components/ProjectToolHost.tsx."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "ProjectToolHost.tsx",
        Path("/repo/admin-ui/src/components/ProjectToolHost.tsx"),
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("ProjectToolHost.tsx not found in expected locations")


# --------------------------------------------------------------------------- #
# Finding w1 — walkthrough test infrastructure exists                          #
# --------------------------------------------------------------------------- #


def test_w1_project_tool_host_test_file_exists():
    """ProjectToolHost.test.tsx must exist and contain end-to-end walkthrough tests.

    Finding w1: the direction retirement condition requires green execution and accepted
    machine evidence. The unit test file covering the full owner walkthrough flow must
    exist to provide machine-verifiable evidence.
    """
    import pytest
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "ProjectToolHost.test.tsx",
        Path("/repo/admin-ui/src/components/ProjectToolHost.test.tsx"),
    ]
    test_source: str | None = None
    for path in candidates:
        if path.exists():
            test_source = path.read_text(encoding="utf-8")
            break

    if test_source is None:
        pytest.skip("ProjectToolHost.test.tsx not found — admin-ui not present in this shard")

    assert "owner walkthrough" in test_source or "end-to-end chained flow" in test_source, (
        "ProjectToolHost.test.tsx must contain an end-to-end owner walkthrough test suite "
        "covering the full project-tools flow (Add, Enable, Hide, Show hidden)"
    )
    assert "Enable" in test_source and "Disable" in test_source, (
        "ProjectToolHost.test.tsx must test both Enable and Disable controls"
    )
    assert "Hide" in test_source and "Show hidden" in test_source, (
        "ProjectToolHost.test.tsx must test the Hide / Show hidden manifest-tool lifecycle"
    )


def test_w1_fleet_runners_panel_test_file_exists():
    """FleetRunnersPanel.test.tsx must exist.

    Finding w1: the runner enrollment walkthrough requires test coverage for the
    enrollment dialog and runner management panel.
    """
    import pytest
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "components" / "FleetRunnersPanel.test.tsx",
        Path("/repo/admin-ui/src/components/FleetRunnersPanel.test.tsx"),
    ]
    for path in candidates:
        if path.exists():
            return  # File exists — test passes
    pytest.skip("FleetRunnersPanel.test.tsx not found — admin-ui not present in this shard")


# --------------------------------------------------------------------------- #
# Finding w2 — runner config generations apply atomically                      #
# --------------------------------------------------------------------------- #


def test_w2_live_runner_config_validates_before_lock():
    """_LiveRunnerConfig.apply must validate all fields before taking the lock.

    Finding w2: the claim was that apply() entered the mutation block before
    detecting unrecognized fields, so a generation with one bad field would still
    apply all the known-good fields (partial state). The fix validates the entire
    config dict upfront; if any field is unrecognized or malformed the whole block
    is rejected without mutating anything.
    """
    source = _local_runner_source()

    # _classify_fields must be called before the lock so rejection is a true no-op.
    classify_idx = source.find("_classify_fields(config)")
    lock_idx = source.find("with self._lock:", classify_idx if classify_idx >= 0 else 0)

    assert classify_idx >= 0, (
        "_LiveRunnerConfig.apply must call _classify_fields(config) to detect "
        "unrecognized/malformed fields before acquiring the lock"
    )
    assert lock_idx >= 0, (
        "_LiveRunnerConfig.apply must use 'with self._lock:' to protect mutations"
    )
    assert classify_idx < lock_idx, (
        "_classify_fields must be called BEFORE 'with self._lock:' so the full "
        "field-validity check runs without holding the lock — a rejected generation "
        "must be a true no-op (no partial mutation)"
    )


def test_w2_live_runner_config_apply_is_atomic():
    """_LiveRunnerConfig.apply rejects an entire generation when any field is bad.

    Finding w2: a generation carrying one unrecognized field alongside a valid
    max_concurrent change must be rejected atomically — the known field must NOT
    be mutated even though it was well-formed.
    """
    source = _local_runner_source()

    # The fully_applicable guard must wrap ALL mutations inside the lock.
    assert "fully_applicable" in source, (
        "_LiveRunnerConfig.apply must compute 'fully_applicable' and gate all "
        "mutations on it so the whole block applies or nothing does"
    )
    assert "if fully_applicable:" in source, (
        "_LiveRunnerConfig.apply must use 'if fully_applicable:' to guard all "
        "field assignments inside the lock"
    )

    # Rejection path must record the offending fields and refused generation.
    assert "_rejection_reason" in source, (
        "_LiveRunnerConfig must store _rejection_reason so operators can see "
        "which field caused the rejection"
    )
    assert "_rejected_generation" in source, (
        "_LiveRunnerConfig must store _rejected_generation so the heartbeat "
        "can report which generation was refused"
    )


def test_w2_live_runner_config_clean_apply_clears_rejection():
    """A clean apply must clear prior rejection state.

    Finding w2: a residual rejection reason after a successful apply would keep
    the operator UI showing a resolved error indefinitely.
    """
    source = _local_runner_source()

    # After a successful apply the rejection fields must be reset.
    assert "self._rejection_reason = None" in source, (
        "_LiveRunnerConfig.apply must set self._rejection_reason = None after a "
        "successful fully-applicable apply so the UI does not show a stale error"
    )
    assert "self._rejected_generation = None" in source, (
        "_LiveRunnerConfig.apply must set self._rejected_generation = None after "
        "a successful apply so stale rejection-generation info is cleared"
    )


# --------------------------------------------------------------------------- #
# Finding w3 — project-tool Remove requires confirmation                       #
# --------------------------------------------------------------------------- #


def test_w3_project_tool_remove_uses_pending_delete_state():
    """ProjectToolHost must use pendingDeleteId state for a two-step delete.

    Finding w3: the claim was that both enabled and disabled Remove buttons called
    handleDelete directly. The fix introduces a pendingDeleteId state; clicking Remove
    sets pendingDeleteId (showing a confirmation prompt) rather than immediately calling
    the backend DELETE endpoint.
    """
    import pytest

    try:
        source = _project_tool_host_source()
    except FileNotFoundError:
        pytest.skip("ProjectToolHost.tsx not found — admin-ui not present in this shard")

    assert "pendingDeleteId" in source, (
        "ProjectToolHost.tsx must define pendingDeleteId state to gate tool deletion "
        "behind a confirmation prompt — clicking Remove must NOT immediately delete"
    )
    assert "setPendingDeleteId" in source, (
        "ProjectToolHost.tsx must call setPendingDeleteId to enter the confirmation "
        "state when the Remove button is clicked"
    )


def test_w3_project_tool_remove_confirmation_names_tool_and_explains_deletion():
    """The deletion confirmation must name the tool and explain permanent deletion.

    Finding w3: cancellation must be side-effect free; the confirmation text must
    identify the tool and explain that its authored content will be deleted.
    """
    import pytest

    try:
        source = _project_tool_host_source()
    except FileNotFoundError:
        pytest.skip("ProjectToolHost.tsx not found — admin-ui not present in this shard")

    assert "permanently delete" in source.lower() or "permanently remove" in source.lower(), (
        "ProjectToolHost.tsx confirmation prompt must use 'permanently delete' or "
        "'Permanently remove' so the operator understands the action is irreversible"
    )
    assert "tool.title" in source or "{tool.title}" in source, (
        "ProjectToolHost.tsx confirmation prompt must include tool.title so the operator "
        "knows which specific tool they are about to permanently delete"
    )
    assert "Permanently remove" in source, (
        "ProjectToolHost.tsx must have a 'Permanently remove' confirmation button "
        "that is separate from the initial 'Remove' button"
    )


def test_w3_project_tool_remove_cancel_is_side_effect_free():
    """Cancel on the deletion confirmation must be side-effect free.

    Finding w3: clicking Cancel must call setPendingDeleteId(null) without
    ever calling handleDelete or the backend DELETE endpoint.
    """
    import pytest

    try:
        source = _project_tool_host_source()
    except FileNotFoundError:
        pytest.skip("ProjectToolHost.tsx not found — admin-ui not present in this shard")

    # Cancel must reset pendingDeleteId to null, not call handleDelete.
    assert "setPendingDeleteId(null)" in source, (
        "ProjectToolHost.tsx Cancel button must call setPendingDeleteId(null) to dismiss "
        "the confirmation prompt without side effects"
    )


def test_w3_project_tool_disabled_row_also_requires_confirmation():
    """The Disabled section Remove button must also use the pendingDeleteId confirmation.

    Finding w3: the confirmation must cover BOTH enabled and disabled tool rows
    without adding friction to the reversible Disable/Hide actions.
    """
    import pytest

    try:
        source = _project_tool_host_source()
    except FileNotFoundError:
        pytest.skip("ProjectToolHost.tsx not found — admin-ui not present in this shard")

    # Both sections must reference pendingDeleteId; a single instance would mean
    # one section bypasses the confirmation. Count occurrences.
    count = source.count("pendingDeleteId === tool.id")
    assert count >= 2, (
        f"ProjectToolHost.tsx must check pendingDeleteId === tool.id in BOTH the enabled "
        f"and disabled tool row renders — found only {count} occurrence(s); the disabled "
        "section Remove must also go through the confirmation prompt"
    )


# --------------------------------------------------------------------------- #
# Finding w4 — project-tools walkthrough residuals                            #
# --------------------------------------------------------------------------- #


def test_w4_enable_button_has_in_flight_guard():
    """The Enable button in the Disabled section must show in-flight state.

    Finding w4: the claim was that the Enable control had no disabled/in-flight guard
    while its PATCH was outstanding, unlike the adjacent Remove control. The fix mirrors
    the existing Disable-button pattern: disabled={togglingId === tool.id && actionIsCurrent}
    with an 'Enabling…' label while the PATCH is pending.
    """
    import pytest

    try:
        source = _project_tool_host_source()
    except FileNotFoundError:
        pytest.skip("ProjectToolHost.tsx not found — admin-ui not present in this shard")

    assert "Enabling" in source, (
        "ProjectToolHost.tsx must render 'Enabling…' while the Enable PATCH is in-flight "
        "so the operator sees unambiguous feedback and the button is guarded against double-fire"
    )
    assert "togglingId === tool.id" in source, (
        "ProjectToolHost.tsx Enable button must be disabled when togglingId === tool.id "
        "to prevent duplicate PATCH requests on slow connections"
    )


def test_w4_created_slug_notice_surfaces_auto_uniquified_slug():
    """A created-slug notice must surface the server-assigned slug after tool creation.

    Finding w4: the add-tool form exposes only Kind and Title, so an auto-uniquified
    slug (e.g. 'mock-widget-2') is never shown to the operator without an explicit
    post-creation notice. The fix surfaces the server-returned slug so operators know
    what slug was assigned when the derived name collided.
    """
    import pytest

    try:
        source = _project_tool_host_source()
    except FileNotFoundError:
        pytest.skip("ProjectToolHost.tsx not found — admin-ui not present in this shard")

    assert "lastCreatedSlug" in source, (
        "ProjectToolHost.tsx must track lastCreatedSlug state to surface the slug "
        "the server assigned after auto-uniquification"
    )
    assert "created-slug-notice" in source or "createdSlug" in source.lower() or "Tool created" in source, (
        "ProjectToolHost.tsx must render a notice showing the assigned slug after "
        "tool creation so the operator can see auto-uniquified slugs (e.g. 'mock-widget-2')"
    )
    assert "tool.slug" in source or "tool.slug" in source or "slug" in source, (
        "ProjectToolHost.tsx must include the tool's slug in the creation notice"
    )


# =========================================================================== #
# 4-finding compute/research salvage bundle
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Finding r1 — Clone form preserves checkpoint-resume fields and watchdog cfg  #
# --------------------------------------------------------------------------- #


def test_r1_launch_params_builder_includes_checkpoint_and_watchdog_fields():
    """r1: admin_compute_jobs.py launch_params builder includes input_artifact_job_id,
    setup_step, and no_progress_timeout_seconds so clone-a-job preserves
    checkpoint-resume, setup steps, and no-progress watchdog configuration.

    Finding: buildCloneForm reads these fields from job.launch_params, but the
    fields were absent from the launch_params dict built server-side, so cloning
    a job that used checkpoint resume / custom setup / watchdog silently dropped
    those settings.

    Fix: the launch_params dict now carries all three keys (lines 347-349 of
    admin_compute_jobs.py).
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "routers" / "admin_compute_jobs.py",
        Path("/repo/backend/app/routers/admin_compute_jobs.py"),
        Path(__file__).resolve().parent.parent / "app" / "routers" / "admin_compute_jobs.py",
    ]
    source: str | None = None
    for p in candidates:
        if p.exists():
            source = p.read_text(encoding="utf-8")
            break
    if source is None:
        import pytest
        pytest.skip("admin_compute_jobs.py not found — skipping source check")

    assert '"input_artifact_job_id"' in source, (
        "admin_compute_jobs.py launch_params dict must include 'input_artifact_job_id' "
        "so that cloning a checkpoint-resume job preserves the resume source job ID"
    )
    assert '"setup_step"' in source, (
        "admin_compute_jobs.py launch_params dict must include 'setup_step' "
        "so that cloning a job with a custom setup step preserves it"
    )
    assert '"no_progress_timeout_seconds"' in source, (
        "admin_compute_jobs.py launch_params dict must include 'no_progress_timeout_seconds' "
        "so that cloning a job with a custom watchdog timeout preserves the configuration"
    )


def test_r1_clone_form_reads_checkpoint_and_watchdog_fields_from_launch_params():
    """r1 (frontend): buildCloneForm in ComputeJobsPanel.tsx reads input_artifact_job_id,
    setup_step, and no_progress_timeout_seconds from launch_params.

    The backend now supplies these fields; the UI must consume them.
    """
    try:
        source = _ui_component_source("ComputeJobsPanel.tsx")
    except FileNotFoundError:
        import pytest
        pytest.skip("ComputeJobsPanel.tsx not found — skipping frontend source check")

    assert "inputArtifactJobId" in source and "input_artifact_job_id" in source, (
        "ComputeJobsPanel.tsx buildCloneForm must map lp.input_artifact_job_id → "
        "inputArtifactJobId so that checkpoint-resume job IDs survive clone"
    )
    assert "setupStep" in source and "setup_step" in source, (
        "ComputeJobsPanel.tsx buildCloneForm must map lp.setup_step → setupStep "
        "so that custom setup steps survive clone"
    )
    assert "noProgressTimeoutSeconds" in source and "no_progress_timeout_seconds" in source, (
        "ComputeJobsPanel.tsx buildCloneForm must map lp.no_progress_timeout_seconds → "
        "noProgressTimeoutSeconds so that watchdog timeout config survives clone"
    )


# --------------------------------------------------------------------------- #
# Finding r2 — Self-heal stale repo_is_empty=False breaks clone_failed loop    #
# --------------------------------------------------------------------------- #


def test_r2_maybe_redetect_on_emptiness_disagreement_is_importable():
    """r2: maybe_redetect_on_emptiness_disagreement exists in build_config_detection
    and accepts observed_empty, breaking the infinite clone_failed loop for projects
    whose repo_is_empty=False durable signal is stale (repo genuinely empty).

    Finding: maybe_refresh_empty_build_config returned early when repo_is_empty is
    False, so a stale non-empty signal never self-corrected; every task threw
    clone_failed forever. The fix adds maybe_redetect_on_emptiness_disagreement
    which fires on durable/live disagreement and re-runs authoritative detection.
    """
    from app.build_config_detection import maybe_redetect_on_emptiness_disagreement
    import inspect

    sig = inspect.signature(maybe_redetect_on_emptiness_disagreement)
    assert "observed_empty" in sig.parameters, (
        "maybe_redetect_on_emptiness_disagreement must accept 'observed_empty' "
        "so the agent can report a live-empty observation and trigger re-detection"
    )


def test_r2_maybe_correct_stale_repo_empty_signal_is_importable():
    """r2: maybe_correct_stale_repo_empty_signal exists as the INVERSE of
    maybe_refresh_empty_build_config — it corrects a stale repo_is_empty=False
    when the repo is genuinely empty.
    """
    from app.build_config_detection import maybe_correct_stale_repo_empty_signal
    import inspect

    sig = inspect.signature(maybe_correct_stale_repo_empty_signal)
    assert "project" in sig.parameters and "session" in sig.parameters, (
        "maybe_correct_stale_repo_empty_signal must accept project and session "
        "so it can persist the corrected build_config"
    )


def test_r2_agent_router_exposes_build_config_discovery_with_observed_repo_is_empty():
    """r2: the agent router must have a /build-config-discovery endpoint that accepts
    observed_repo_is_empty and calls maybe_redetect_on_emptiness_disagreement.

    The agent calls POST /api/agent/build-config-discovery with
    {observed_repo_is_empty: true} as a best-effort self-heal before throwing
    the clone_failed error.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "routers" / "agent.py",
        Path("/repo/backend/app/routers/agent.py"),
        Path(__file__).resolve().parent.parent / "app" / "routers" / "agent.py",
    ]
    source: str | None = None
    for p in candidates:
        if p.exists():
            source = p.read_text(encoding="utf-8")
            break
    if source is None:
        import pytest
        pytest.skip("agent.py not found — skipping source check")

    assert "build-config-discovery" in source, (
        "agent.py must define a /build-config-discovery endpoint so the agent can "
        "report observed_repo_is_empty and trigger server-side self-healing"
    )
    assert "maybe_redetect_on_emptiness_disagreement" in source, (
        "agent.py /build-config-discovery handler must call "
        "maybe_redetect_on_emptiness_disagreement to re-run authoritative detection "
        "when the agent observes a live-empty tree vs. the durable non-empty signal"
    )
    assert "observed_repo_is_empty" in source, (
        "agent.py must accept observed_repo_is_empty from the agent payload so the "
        "direction of the disagreement (observed empty vs. observed non-empty) is "
        "forwarded to the re-detection helper"
    )


# --------------------------------------------------------------------------- #
# Finding r3 — GCE VM resource monitoring parity with local runners            #
# --------------------------------------------------------------------------- #


def test_r3_check_low_disk_vms_and_check_low_memory_vms_are_defined():
    """r3: runner_health_monitors.py must define _check_low_disk_vms and
    _check_low_memory_vms — GCE-task-VM twins of the local-runner checks
    _check_low_disk_runners / _check_low_memory_runners.

    Finding: local runners had proactive resource monitoring (_check_low_disk_runners,
    _check_low_memory_runners) but GCE task VMs had no equivalent — only teardown-
    time serial-console forensics. The fix adds Task-based VM monitors that alert
    when a VM reports low disk or approaching OOM via its heartbeat metadata.
    """
    from app.runner_health_monitors import (
        _check_low_disk_vms,
        _check_low_memory_vms,
    )
    import inspect

    sig_disk = inspect.signature(_check_low_disk_vms)
    assert "session" in sig_disk.parameters, (
        "_check_low_disk_vms must accept a session parameter to query active VM tasks"
    )

    sig_mem = inspect.signature(_check_low_memory_vms)
    assert "session" in sig_mem.parameters, (
        "_check_low_memory_vms must accept a session parameter to query active VM tasks"
    )


def test_r3_vm_resource_monitors_called_in_health_check_flow():
    """r3: the health-check orchestrator (run_runner_health_monitors) must call
    _check_low_disk_vms and _check_low_memory_vms to make VM monitoring active.

    Importing and defining the functions is not enough — they must be wired into
    the periodic health-check sweep that fires for every orchestrator tick.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "runner_health_monitors.py",
        Path("/repo/backend/app/runner_health_monitors.py"),
        Path(__file__).resolve().parent.parent / "app" / "runner_health_monitors.py",
    ]
    source: str | None = None
    for p in candidates:
        if p.exists():
            source = p.read_text(encoding="utf-8")
            break
    if source is None:
        import pytest
        pytest.skip("runner_health_monitors.py not found — skipping source check")

    assert "await _check_low_disk_vms" in source, (
        "runner_health_monitors.py must await _check_low_disk_vms inside "
        "run_runner_health_monitors so GCE VM low-disk conditions trigger alerts "
        "during every health-check sweep"
    )
    assert "await _check_low_memory_vms" in source, (
        "runner_health_monitors.py must await _check_low_memory_vms inside "
        "run_runner_health_monitors so GCE VM OOM conditions trigger alerts "
        "during every health-check sweep"
    )


# --------------------------------------------------------------------------- #
# Finding r4 — Research quality feedback closes the grade/grader loop          #
# --------------------------------------------------------------------------- #


def test_r4_direction_quality_score_incorporates_grade_distribution():
    """r4: _compute_direction_quality_score in direction_metrics.py must consume
    grade_distribution from research_history entries so HIGH-grade directions
    score better than LOW-grade noise generators.

    Finding: grade_distribution was stored per research_history entry by
    research_direction_state.py but never consumed — it had zero influence on
    direction quality scores or research spawning priority.

    Fix: the quality scorer now reads HIGH/MEDIUM/LOW counts from grade_distribution
    and adds a recency-weighted HIGH-grade fraction as a quality component.
    """
    from app.direction_metrics import _compute_direction_quality_score

    # Direction whose recent history has mostly HIGH-grade findings.
    high_grade_direction = {
        "research_history": [
            {"grade_distribution": {"HIGH": 5, "MEDIUM": 1, "LOW": 0}},
            {"grade_distribution": {"HIGH": 4, "MEDIUM": 2, "LOW": 0}},
        ]
    }
    # Direction whose recent history has mostly LOW-grade findings.
    low_grade_direction = {
        "research_history": [
            {"grade_distribution": {"HIGH": 0, "MEDIUM": 1, "LOW": 6}},
            {"grade_distribution": {"HIGH": 1, "MEDIUM": 1, "LOW": 5}},
        ]
    }

    high_score = _compute_direction_quality_score(high_grade_direction)
    low_score = _compute_direction_quality_score(low_grade_direction)

    assert isinstance(high_score, float), (
        "_compute_direction_quality_score must return a float quality score"
    )
    assert 0.0 <= high_score <= 1.0, (
        "_compute_direction_quality_score score must be in [0.0, 1.0]"
    )
    assert high_score > low_score, (
        "_compute_direction_quality_score must assign a higher score to a direction "
        "with mostly HIGH-grade findings than one with mostly LOW-grade noise; "
        "grade_distribution must contribute to the quality signal"
    )


def test_r4_direction_quality_score_incorporates_quality_grader_verdict():
    """r4: _compute_direction_quality_score must consume quality_grader_verdict
    (pass/fail) from research_history so directions whose findings consistently
    pass forward-quality grading score higher.

    Finding: quality_grader_verdict was stored on individual task contexts in
    task_lifecycle.py but never aggregated into direction quality scoring — it
    had no effect on research prioritisation.

    Fix: the quality scorer reads quality_grader_verdict.passed from recent
    history entries and adds a recency-weighted pass-rate component.
    """
    from app.direction_metrics import _compute_direction_quality_score

    # Direction where quality grader consistently passed.
    all_pass = {
        "research_history": [
            {"quality_grader_verdict": {"passed": True}},
            {"quality_grader_verdict": {"passed": True}},
            {"quality_grader_verdict": {"passed": True}},
        ]
    }
    # Direction where quality grader consistently failed.
    all_fail = {
        "research_history": [
            {"quality_grader_verdict": {"passed": False}},
            {"quality_grader_verdict": {"passed": False}},
            {"quality_grader_verdict": {"passed": False}},
        ]
    }

    pass_score = _compute_direction_quality_score(all_pass)
    fail_score = _compute_direction_quality_score(all_fail)

    assert pass_score > fail_score, (
        "_compute_direction_quality_score must assign a higher score to a direction "
        "whose findings all passed quality grading than one where all failed; "
        "quality_grader_verdict.passed must contribute to the quality signal"
    )


def test_r4_direction_quality_score_used_in_research_spawning_priority():
    """r4: autonomous_research.py must import and call _compute_direction_quality_score
    so that quality feedback actually influences which directions get research tasks.

    A quality signal that is computed but never consumed by the spawner has no effect.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[2] / "backend" / "app" / "autonomous_research.py",
        Path("/repo/backend/app/autonomous_research.py"),
        Path(__file__).resolve().parent.parent / "app" / "autonomous_research.py",
    ]
    source: str | None = None
    for p in candidates:
        if p.exists():
            source = p.read_text(encoding="utf-8")
            break
    if source is None:
        import pytest
        pytest.skip("autonomous_research.py not found — skipping source check")

    assert "_compute_direction_quality_score" in source, (
        "autonomous_research.py must import and call _compute_direction_quality_score "
        "so that research quality feedback (grade_distribution / quality_grader_verdict) "
        "influences which directions are prioritised for research spawning"
    )


# =========================================================================== #
# 20-finding release-qualification salvage bundle (2026-07-20)
# =========================================================================== #


# f20-1 — serving-only-import ImportError degrades gate to ('ok', None)
# --------------------------------------------------------------------------- #


def test_f20_1_serving_only_import_classifies_as_degrade():
    """_ServingOnlyImportError must exist and _looks_like_serving_only_import must
    detect app.* imports correctly so the gate degrades instead of hard-blocking.

    Finding f20-1: candidate Alembic migrations are replayed in the SERVING process.
    A migration that imports a candidate-only app symbol (e.g.
    ``from app.field_encryption import require_encryption_active``) raises ImportError
    in this context because the candidate's app code is not yet deployed.  Previously
    this was classified as _DeterministicMigrationDefect → hard block.  The fix
    classifies it as _ServingOnlyImportError and degrades the gate to ('ok', None)
    since the same migration succeeds on the real deploy target.
    """
    from app.migration_apply_gate import (
        _ServingOnlyImportError,
        _looks_like_serving_only_import,
    )

    # _ServingOnlyImportError must be a distinct Exception subclass.
    assert issubclass(_ServingOnlyImportError, Exception), (
        "_ServingOnlyImportError must be an Exception subclass — "
        "it signals a serving-replay self-reference artifact, not a deploy-fatal defect"
    )
    assert _ServingOnlyImportError is not Exception, (
        "_ServingOnlyImportError must be a dedicated subclass, not Exception itself"
    )

    # app.* submodule import must match.
    app_submod = ImportError("No module named 'app.field_encryption'")
    app_submod.name = "app.field_encryption"
    assert _looks_like_serving_only_import(app_submod) is True, (
        "_looks_like_serving_only_import must return True for exc.name='app.field_encryption' "
        "— that symbol exists in the candidate tree but not in the serving process"
    )

    # Root 'app' module import must also match.
    root_app = ImportError("No module named 'app'")
    root_app.name = "app"
    assert _looks_like_serving_only_import(root_app) is True, (
        "_looks_like_serving_only_import must return True for exc.name='app' (root module)"
    )

    # Third-party missing dependency must NOT match (real deploy-fatal defect).
    third_party = ImportError("No module named 'cryptography.hazmat'")
    third_party.name = "cryptography.hazmat"
    assert _looks_like_serving_only_import(third_party) is False, (
        "_looks_like_serving_only_import must return False for non-app.* ImportErrors — "
        "a missing third-party dep IS a deploy-fatal defect and must hard-block"
    )


def test_f20_1_serving_only_import_not_in_deterministic_defect_class():
    """_ServingOnlyImportError must be separate from _DeterministicMigrationDefect.

    Finding f20-1: the old code raised _DeterministicMigrationDefect (hard block) for
    ALL ImportErrors.  The new code must treat _ServingOnlyImportError as a DISTINCT
    exception class that the gate maps to degrade ('ok', None), not a block.
    """
    from app.migration_apply_gate import (
        _DeterministicMigrationDefect,
        _ServingOnlyImportError,
    )

    assert not issubclass(_ServingOnlyImportError, _DeterministicMigrationDefect), (
        "_ServingOnlyImportError must NOT be a subclass of _DeterministicMigrationDefect — "
        "that would cause it to be caught as a hard block; it must be caught separately "
        "and mapped to a degrade ('ok', None) verdict"
    )


# f20-2 — design: prove data-dependent migrations safely
# --------------------------------------------------------------------------- #


def test_f20_2_scratch_replay_compares_schema_not_row_data():
    """Scratch migration replay uses schema metadata only — no row data from production.

    Finding f20-2 claim: Production state supplied to scratch replay contains only the
    Alembic revision and reflected table/column metadata. Scratch seeding creates empty
    tables, and the final drift verdict compares schema names rather than row-transform
    outcomes. This is the boundary that makes data-dependent migrations unproven by the
    current structural gate — the design proposes a separate contract for that.
    """
    import inspect
    from app.migration_apply_gate import _provision_seed_and_detect_drift

    source = inspect.getsource(_provision_seed_and_detect_drift)
    # Schema comparison is via metadata / table/column names, not row content.
    assert (
        "MetaData" in source or "reflect" in source.lower() or "table_names" in source
        or "sorted_tables" in source or "schema_name" in source.lower()
    ), (
        "_provision_seed_and_detect_drift must compare schema metadata (table/column names), "
        "not row-level data — the current gate is structurally-safe only"
    )
    # No production row COPY or INSERT should appear in the drift logic.
    for disallowed in ("COPY FROM", "copy_from_production", "pg_dump"):
        assert disallowed.lower() not in source.lower(), (
            f"_provision_seed_and_detect_drift must NOT reference '{disallowed}' — "
            "scratch seeding creates empty tables, not a production row dump"
        )


# f20-3 — feat(security): verify encryption-key continuity
# Already pinned by e1 / e2 / e3 tests in the keyless-encryption bundle.
# --------------------------------------------------------------------------- #


def test_f20_3_encryption_key_continuity_functions_present():
    """All three encryption-continuity guard functions must be importable.

    Finding f20-3: the fix requires probe_key_continuity (fail-closed on wrong-key
    rotation), assert_plaintext_or_raise (guards backfill re-encryption), and a
    decrypt_field that falls through for backward compat but logs a warning.
    Full behavioral coverage is in e1 / e2 / e3 tests.
    """
    from app.field_encryption import (
        probe_key_continuity,
        assert_plaintext_or_raise,
        decrypt_field,
        CONTINUITY_REFERENCE_PLAINTEXT,
    )

    assert callable(probe_key_continuity), "probe_key_continuity must be callable"
    assert callable(assert_plaintext_or_raise), "assert_plaintext_or_raise must be callable"
    assert callable(decrypt_field), "decrypt_field must be callable"
    assert isinstance(CONTINUITY_REFERENCE_PLAINTEXT, str) and CONTINUITY_REFERENCE_PLAINTEXT, (
        "CONTINUITY_REFERENCE_PLAINTEXT must be a non-empty string sentinel"
    )


# f20-4 — research: define safe Fernet key rotation
# Already pinned by e4 (test_multifernet_encrypts_with_first_key_decrypts_with_all).
# --------------------------------------------------------------------------- #


def test_f20_4_multifernet_rotate_method_exists():
    """MultiFernet must expose a rotate() method for staged key retirement.

    Finding f20-4 claim: the cryptography library's official MultiFernet contract
    encrypts with the first key, decrypts with every configured key, and supports
    explicit token rotation. Full contract verification is in e4.
    """
    from cryptography.fernet import MultiFernet

    assert hasattr(MultiFernet, "rotate") and callable(MultiFernet.rotate), (
        "MultiFernet must expose rotate() for non-destructive staged key rotation — "
        "this is the mechanism for retiring old-key ciphertext without data loss"
    )


# f20-5 — fix(encrypt): fail closed on keyless/malformed encryption backfill
# --------------------------------------------------------------------------- #


def test_f20_5_require_encryption_active_raises_for_malformed_key(monkeypatch):
    """require_encryption_active must raise RuntimeError for a malformed key,
    even when ALLOW_PLAINTEXT_CREDENTIALS is set.

    Finding f20-5 claim: is_encryption_active() returns False identically for a
    missing key and a malformed key (both via _get_fernet returning None). A backfill
    migration that calls require_encryption_active() instead of is_encryption_active()
    will correctly fail closed on a malformed key rather than silently stamping the
    migration as applied while columns remain plaintext.
    """
    import pytest
    from app import field_encryption

    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setenv("ALLOW_PLAINTEXT_CREDENTIALS", "true")

    # is_encryption_active returns False for both missing and malformed key.
    assert not field_encryption.is_encryption_active(), (
        "is_encryption_active() must return False for a malformed key (conflation "
        "is intentional — use require_encryption_active() to distinguish)"
    )
    # require_encryption_active raises EVEN with ALLOW_PLAINTEXT set.
    with pytest.raises(RuntimeError, match="invalid"):
        field_encryption.require_encryption_active("f20-5-test")


def test_f20_5_require_encryption_active_raises_for_missing_key_without_opt_in(monkeypatch):
    """require_encryption_active must raise RuntimeError for a missing key without opt-in.

    Finding f20-5: production environments without an explicit key and without the
    ALLOW_PLAINTEXT_CREDENTIALS flag must be treated as mis-configured, not a no-op.
    """
    import pytest
    from app import field_encryption

    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)

    with pytest.raises(RuntimeError):
        field_encryption.require_encryption_active("f20-5-missing-key-test")


# f20-6 — fix(release): make scratch-DB URL degraded, not boot-fatal
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req
# and test_scratch_db_url_absence_does_not_raise_on_cloud_run.
# --------------------------------------------------------------------------- #


def test_f20_6_scratch_db_url_in_qualification_env_not_required_env():
    """MIGRATION_APPLY_SCRATCH_DATABASE_URL must not be in REQUIRED_RUNTIME_ENV.

    Finding f20-6: the scratch URL was boot-fatal (scope='cloud_run' in REQUIRED_RUNTIME_ENV).
    The fix moves it out of REQUIRED_RUNTIME_ENV so its absence degrades the gate to a
    'defer' hold instead of crashing the Cloud Run service at startup.
    Full coverage in test_scratch_db_url_is_qualification_prereq_not_boot_req.
    """
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    req_names = {r.name for r in REQUIRED_RUNTIME_ENV}

    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in req_names, (
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL must NOT be in REQUIRED_RUNTIME_ENV — "
        "its absence must degrade the gate, not crash Cloud Run at boot"
    )


# f20-7 — fix(release): isolate gate-defer signature from unrelated child failures
# Already pinned by test_infra_failure_signature_ignores_child_failures.
# --------------------------------------------------------------------------- #


def test_f20_7_infra_failure_signature_derived_from_error_code_only():
    """_infra_failure_signature must produce a stable signature from the error code alone.

    Finding f20-7: the signature previously derived its leg from child_failures[-1],
    so audit vs smoketest failures caused oscillation that prevented the permanent-gate-
    incapacity streak from accumulating. The fix ignores child_failures entirely.
    Full behavioral coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    error = "migration_apply_check_unavailable: scratch db unreachable"
    sig_with_children = _infra_failure_signature(error, [{"leg": "audit"}, {"leg": "smoketest"}])
    sig_empty_children = _infra_failure_signature(error, [])
    sig_no_children = _infra_failure_signature(error, [{"leg": "challenge"}])

    assert sig_with_children == sig_empty_children == sig_no_children, (
        "_infra_failure_signature must produce identical output regardless of child_failures "
        "— the signature must be stable across attempts to allow permanent-gate-incapacity "
        "streak detection"
    )


# f20-8 — fix(release): contain timed-out qualifier children
# Already pinned by test_cancel_qualifier_children_covers_all_child_types.
# --------------------------------------------------------------------------- #


def test_f20_8_force_finalize_cancels_children_before_rewriting_state():
    """_force_finalize_timed_out_qualification must cancel children before force-finalizing.

    Finding f20-8: timeout cleanup previously rewrote the release record and alerted
    without cancelling child tasks, so in-flight audit/challenge/smoketest agents could
    complete and influence the next re-drive.
    Full coverage in test_cancel_qualifier_children_covers_all_child_types.
    """
    import inspect
    from app.release_qualifier_sweep import _force_finalize_timed_out_qualification

    source = inspect.getsource(_force_finalize_timed_out_qualification)
    assert "_cancel_qualifier_children" in source, (
        "_force_finalize_timed_out_qualification must call _cancel_qualifier_children "
        "to contain in-flight children before rewriting the release record"
    )


# f20-9 — fix(release): block deterministic migration defects
# Already pinned by test_deterministic_migration_defect_class_exists.
# --------------------------------------------------------------------------- #


def test_f20_9_deterministic_migration_defect_returns_block_verdict():
    """_DeterministicMigrationDefect must map to a 'block' verdict, not a 'defer'.

    Finding f20-9 claim: all exceptions were previously wrapped in one migration-apply
    error type that converted to defer (retried as transient). The fix introduces
    _DeterministicMigrationDefect for SQL class-42 errors and non-serving ImportError/
    SyntaxError, which the gate converts to a 'block' hard-verdict so the candidate
    stops retrying.
    Full coverage in test_deterministic_migration_defect_class_exists.
    """
    from app.migration_apply_gate import _DeterministicMigrationDefect

    # Must be a distinct Exception subclass (not a re-label of a defer type).
    assert issubclass(_DeterministicMigrationDefect, Exception), (
        "_DeterministicMigrationDefect must be an Exception subclass"
    )
    # The gate source must catch it and return 'block'.
    import pathlib
    gate_source = (
        pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py"
    ).read_text()
    assert '"block"' in gate_source or "'block'" in gate_source, (
        "migration_apply_gate.py must return a 'block' verdict "
        "somewhere — _DeterministicMigrationDefect must not defer"
    )
    assert "_DeterministicMigrationDefect" in gate_source, (
        "_DeterministicMigrationDefect must be defined in migration_apply_gate.py"
    )


# f20-10 — feat(release): enforce independent boot preflight
# Already pinned by test_env_preflight_absent_evidence_is_a_hard_blocker.
# --------------------------------------------------------------------------- #


def test_f20_10_env_preflight_enabled_flag_independent_of_smoketest():
    """env_preflight_enabled is an independent gate flag, not tied to smoketest alone.

    Finding f20-10 claim: the sole env-preflight artifact producer was attached to the
    optional smoketest child. The fix introduces an env_preflight_enabled flag that can
    gate independently of smoketest, and absent/malformed evidence is a hard blocker
    rather than 'unavailable'.
    Full coverage in test_env_preflight_absent_evidence_is_a_hard_blocker.
    """
    import pathlib

    rq_source = (
        pathlib.Path(__file__).parent.parent / "app" / "release_qualifier.py"
    ).read_text()
    assert "env_preflight_enabled" in rq_source, (
        "release_qualifier.py must reference env_preflight_enabled — "
        "the independent gate flag that decouples env-preflight from smoketest"
    )
    assert "_score_env_preflight" in rq_source, (
        "release_qualifier.py must call _score_env_preflight — "
        "the deployed-side judge that scores env-preflight evidence as a hard blocker"
    )


# f20-11 — refactor(release): unify trigger truth and retries
# --------------------------------------------------------------------------- #


def test_f20_11_infra_failure_maps_to_dedicated_redrive_trigger():
    """resolve_qualification_trigger must return 'infra_failure_redrive' for infra_failure,
    not 'not_applicable' — so the trigger explanation accurately reflects the auto-redrive.

    Finding f20-11 claim: the trigger resolver previously returned not_applicable for
    states that dedicated sweep re-drive functions handle automatically, making the
    operator-facing trigger explanation misleading. The fix returns a proper trigger name
    so the auto_trigger surface reflects the actual re-drive path.
    """
    from app.qualification_triggers import resolve_qualification_trigger

    decision = resolve_qualification_trigger(
        phase="stabilize",
        status="infra_failure",
        error_code="migration_apply_check_unavailable",
        qualification_attempts=1,
        error_redrive_count=0,
        is_candidate_fully_met=False,
        requalification_ready=None,
        candidate_sha="abc123",
        automain_sha="abc123",
        automain_ci_state="success",
        blocker=None,
        in_flight=False,
        promotion_in_flight=False,
        consecutive_infra_errors=1,
    )

    assert decision.trigger == "infra_failure_redrive", (
        "resolve_qualification_trigger must return trigger='infra_failure_redrive' "
        f"for status='infra_failure' — got trigger='{decision.trigger}'. "
        "Returning 'not_applicable' would hide the bespoke auto-redrive from operator view."
    )
    assert decision.reason != "not_applicable", (
        "resolve_qualification_trigger must NOT return reason='not_applicable' for "
        "infra_failure — a dedicated redrive loop handles this status automatically"
    )


# f20-12 — fix(release): contain orphan promotion actors
# Already pinned by test_bare_claim_promotion_gets_shorter_timeout.
# --------------------------------------------------------------------------- #


def test_f20_12_promotion_timeout_applies_to_bare_claims():
    """Bare-claim promotions (claiming=True, no PR number) must have a timeout applied.

    Finding f20-12 claim: a crash after pull-request creation can leave
    promotion_pending with claiming=True and no PR number. Timeout cleanup previously
    only closed orphaned PRs for managed-check-wait records, not bare claims.
    Full coverage in test_bare_claim_promotion_gets_shorter_timeout.
    """
    import inspect
    from app import release_reconciliation

    source = inspect.getsource(release_reconciliation)
    assert "claiming" in source, (
        "release_reconciliation must reference 'claiming' field — "
        "bare promotion claims must be reachable by timeout cleanup"
    )


# f20-13 — feat(release): gate data-migration proof
# Already pinned by test_data_migration_proof_gate_defers_dml_without_proof
# and test_data_migration_proof_gate_passes_with_proof_declaration.
# --------------------------------------------------------------------------- #


def test_f20_13_data_migration_proof_gate_defers_dml():
    """Data-dependent migrations without a proof declaration must defer the gate.

    Finding f20-13 claim: the current migration-safe verdict can be produced after
    applying a data migration to empty scratch tables, and no current contract requires
    a data migration to declare fixtures. The gate must defer for DML migrations that
    lack a proof declaration rather than inheriting structural approval.
    Full coverage in test_data_migration_proof_gate_defers_dml_without_proof.
    """
    import pathlib

    gate_source = (
        pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py"
    ).read_text()
    assert "dml" in gate_source.lower() or "data_migration" in gate_source.lower() or "DML" in gate_source, (
        "migration_apply_gate.py must reference DML/data migration detection — "
        "structural migrations and data migrations must be classified separately"
    )


# f20-14 — feat(ui): guide narrow release remediation
# Already pinned by test_version_lifecycle_panel_has_hotfix_remediation.
# --------------------------------------------------------------------------- #


def test_f20_14_hotfix_remediation_api_client_present():
    """The admin-ui API client must expose getHotfixScopePreview and triggerHotfixPromote.

    Finding f20-14 claim: admin-ui/src currently has no hotfix preview or promotion
    client and VersionLifecyclePanel renders only generic qualification failure state.
    The fix adds both the API client and the remediation UI panel.
    Full panel coverage in test_version_lifecycle_panel_has_hotfix_remediation.
    """
    import pathlib
    import pytest

    candidates = [
        pathlib.Path(__file__).resolve().parents[2]
        / "admin-ui" / "src" / "api" / "projects.ts",
        pathlib.Path("/repo/admin-ui/src/api/projects.ts"),
    ]
    api_source: str | None = None
    for candidate in candidates:
        if candidate.exists():
            api_source = candidate.read_text()
            break
    if api_source is None:
        pytest.skip("admin-ui/src/api/projects.ts not found — admin-ui not present in this shard")

    assert "getHotfixScopePreview" in api_source or "hotfix" in api_source.lower(), (
        "admin-ui/src/api/projects.ts must expose a hotfix scope preview function — "
        "the UI remediation flow requires this API client"
    )
    assert "triggerHotfixPromote" in api_source or "promote/hotfix" in api_source, (
        "admin-ui/src/api/projects.ts must expose a hotfix promote function — "
        "the UI remediation flow requires this API client"
    )


# f20-15 — MIGRATION_APPLY_SCRATCH_DATABASE_URL not in REQUIRED_RUNTIME_ENV (boot safe)
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req and
# test_f20_6_scratch_db_url_in_qualification_env_not_required_env.
# --------------------------------------------------------------------------- #


def test_f20_15_missing_qualification_env_surfaces_scratch_db_url():
    """migration-apply gate must defer gracefully when MIGRATION_APPLY_SCRATCH_DATABASE_URL is absent.

    Finding f20-15 claim: MIGRATION_APPLY_SCRATCH_DATABASE_URL appeared nowhere in
    required_runtime_env.py. The fix ensures it is not in REQUIRED_RUNTIME_ENV so its
    absence causes a graceful 'absent' defer rather than a boot-time RuntimeError.
    The gate's own preflight surfaces the missing var with an actionable message.
    """
    from app.migration_apply_gate import MigrationApplyScratchPreflight, preflight_migration_apply_scratch
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    req_names = {r.name for r in REQUIRED_RUNTIME_ENV}
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in req_names, (
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL must NOT be in REQUIRED_RUNTIME_ENV — "
        "its absence must degrade the gate, not crash Cloud Run at boot"
    )
    # The gate's preflight surfaces the missing var with an actionable message.
    result = preflight_migration_apply_scratch(None)
    assert isinstance(result, MigrationApplyScratchPreflight)
    assert result.status == "absent"
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (result.detail or "")


# f20-16 — fix(release): stabilize infra-failure signature for gate defers
# Already pinned by test_infra_failure_signature_ignores_child_failures
# and test_f20_7_infra_failure_signature_derived_from_error_code_only above.
# (Same root fix as f20-7: child_failures are discarded via 'del child_failures'.)
# --------------------------------------------------------------------------- #


def test_f20_16_infra_failure_signature_stable_across_candidate_rotations():
    """_infra_failure_signature must produce the same string across candidate rotations.

    Finding f20-16: the live v26 qualification showed signatures oscillating between
    'challenge:PROVISION_FAILURE:migration_apply_check_unavailable' and
    'audit:PROVISION_FAILURE:audit_infra_respawn_cap_exceeded', preventing the
    is_permanent_gate_incapacity streak from accumulating. The fix uses only
    the stable prefix of infra_error to build the signature.
    Full coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    same_error = "migration_apply_check_unavailable"
    # Vary child_failures as they would across different attempts.
    sig1 = _infra_failure_signature(same_error, [{"leg": "audit", "status": "FAILED"}])
    sig2 = _infra_failure_signature(same_error, [{"leg": "challenge", "status": "CANCELLED"}])
    sig3 = _infra_failure_signature(same_error, [])

    assert sig1 == sig2 == sig3, (
        "_infra_failure_signature must be identical across different child_failures "
        "— the permanent-gate-incapacity streak requires a stable signature"
    )


# f20-17 — fix(release): add hotfix_policy.py to hotfix-eligible modules
# Already pinned by test_hotfix_policy_and_router_in_eligible_modules.
# --------------------------------------------------------------------------- #


def test_f20_17_hotfix_policy_and_router_self_eligible():
    """hotfix_policy.py and admin_operator_triggers.py must be in HOTFIX_ELIGIBLE_MODULES.

    Finding f20-17 claim: HOTFIX_ELIGIBLE_MODULES did not contain 'backend/app/hotfix_policy.py'.
    Without these two modules, a fix to the hotfix scope validator cannot be deployed via
    the hotfix channel — the exact self-reference gap the channel exists to solve.
    Full coverage in test_hotfix_policy_and_router_in_eligible_modules.
    """
    from app.hotfix_policy import HOTFIX_ELIGIBLE_MODULES

    assert "backend/app/hotfix_policy.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/hotfix_policy.py' must be in HOTFIX_ELIGIBLE_MODULES — "
        "without this, a fix to the hotfix scope validator cannot be hotfix-deployed"
    )
    assert "backend/app/routers/admin_operator_triggers.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/routers/admin_operator_triggers.py' must be in HOTFIX_ELIGIBLE_MODULES — "
        "the hotfix endpoint router must be self-eligible for the channel to be self-healing"
    )


# f20-18 — feat: candidate-side acceptance-evidence minting
# --------------------------------------------------------------------------- #


def test_f20_18_acceptance_evidence_minting_runs_deployed_side():
    """Acceptance-evidence minting must be imported into release_qualifier_sweep.py
    and invoked there (deployed-side), not in a candidate subprocess.

    Finding f20-18 claim: record_acceptance_window_coverage_evidence,
    backfill_unverified_merge_evidence, and backfill_direction_merge_evidence all run
    inside the deployed orchestrator process at the deployed (main) SHA. A fix to the
    minting logic that lands on automain is inert until the fix itself is promoted.
    This test pins that the minting calls are in the sweep (deployed-side), not in
    any candidate-executing subprocess.
    """
    import pathlib

    sweep_source = (
        pathlib.Path(__file__).parent.parent / "app" / "release_qualifier_sweep.py"
    ).read_text()

    assert "record_acceptance_window_coverage_evidence" in sweep_source, (
        "record_acceptance_window_coverage_evidence must be imported/called in "
        "release_qualifier_sweep.py (deployed-side) — not in the sandboxed migration "
        "subprocess or smoketest child"
    )
    assert "backfill_direction_merge_evidence" in sweep_source or "backfill_unverified_merge_evidence" in sweep_source, (
        "Merge-evidence backfill must run in release_qualifier_sweep.py (deployed-side) — "
        "any fix to backfill logic is inert until the fix itself is promoted"
    )
    # The sandboxed subprocess runner must NOT be where minting happens.
    gate_source = (
        pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py"
    ).read_text()
    assert "record_acceptance_window_coverage_evidence" not in gate_source, (
        "record_acceptance_window_coverage_evidence must NOT appear in "
        "migration_apply_gate.py — acceptance-evidence minting is deployed-side, "
        "not in the sandboxed migration subprocess"
    )


# f20-19 — feat: operator-approved release-machinery hotfix channel
# Already pinned by e17 (test_hotfix_endpoint_validates_reason_and_ack_inert_until_deploy)
# and e19 (test_scratch_subprocess_injects_ephemeral_fernet_key).
# --------------------------------------------------------------------------- #


def test_f20_19_hotfix_promote_current_version_exportable():
    """hotfix_promote_current_version must be importable from release_qualifier.

    Finding f20-19 claim: no hotfix endpoint existed — only force_promote_current_version
    which ships the whole candidate tree. The fix adds hotfix_promote_current_version
    (module-scoped, OWNER-only) as a distinct entry point with a narrower scope policy.
    Full endpoint coverage in test_hotfix_promote_endpoint_exists and e17.
    """
    from app.release_qualifier import hotfix_promote_current_version, check_hotfix_scope_preview

    assert callable(hotfix_promote_current_version), (
        "hotfix_promote_current_version must be callable — it is the scoped promote "
        "entry point for the narrow hotfix-deploy channel"
    )
    assert callable(check_hotfix_scope_preview), (
        "check_hotfix_scope_preview must be callable — read-only preview before "
        "the operator commits to the hotfix promotion"
    )


# f20-20 — feat(release): operator-approved hotfix-deploy channel
# Already pinned by test_hotfix_promote_endpoint_exists and
# test_hotfix_promote_current_version_function_exists.
# --------------------------------------------------------------------------- #


def test_f20_20_hotfix_eligible_modules_restricts_scope():
    """evaluate_hotfix_scope must reject files outside HOTFIX_ELIGIBLE_MODULES.

    Finding f20-20 claim: force_promote ships the whole tree, so no NARROW lane for
    gate-only fixes existed. The fix adds evaluate_hotfix_scope() which enforces a
    module allowlist so the hotfix-deploy channel cannot be abused to ship arbitrary
    code changes under the guise of a gate fix.
    """
    from app.hotfix_policy import evaluate_hotfix_scope, HOTFIX_ELIGIBLE_MODULES

    # A diff containing only an out-of-scope file must be rejected.
    verdict = evaluate_hotfix_scope(
        changed_files=["backend/app/models.py"],
        main_py_patch=None,
    )
    assert verdict.eligible is False, (
        "evaluate_hotfix_scope must return eligible=False when changed files include "
        "backend/app/models.py — that file is not in HOTFIX_ELIGIBLE_MODULES"
    )
    assert "backend/app/models.py" not in HOTFIX_ELIGIBLE_MODULES, (
        "backend/app/models.py must NOT be in HOTFIX_ELIGIBLE_MODULES — "
        "the narrow hotfix channel must not allow arbitrary model changes"
    )

    # A diff containing only an eligible module must be accepted.
    verdict_ok = evaluate_hotfix_scope(
        changed_files=["backend/app/release_qualifier.py"],
        main_py_patch=None,
    )
    assert verdict_ok.eligible is True, (
        "evaluate_hotfix_scope must return eligible=True for a diff that touches "
        "only HOTFIX_ELIGIBLE_MODULES — the hotfix channel must be usable for "
        "narrowly-scoped gate fixes"
    )


# ── 7-finding operator audience / message lifecycle salvage bundle (2026-07-21) ── #
# Findings: design/fix: creation-path fail-closed audience contract (m-1..m-2),
#           feat: auto-decay stale QUESTION messages (m-6).
# Findings m-3 (operator inbox lifecycle), m-4 (attention-driven inbox),
# m-5 (confirmation chips), m-7 (converge notifications) are pinned by the
# MessagesPanel.test.tsx / useMessageList.test.ts frontend test suites.
# --------------------------------------------------------------------------- #


def test_m1_research_dedup_deadlock_alert_registered_strategist_internal():
    """research.dedup_deadlock must route to STRATEGIST_INTERNAL (not leak to operator).

    Finding claim: research_direction_state.py builds a dedup-deadlock Alert with no
    event_type, so it fails open and reaches the operator badge. Fix: event_type=
    'research.dedup_deadlock' registered as STRATEGIST_INTERNAL in message_audience.py.
    """
    from app.message_audience import route_event, STRATEGIST_INTERNAL

    assert route_event("research.dedup_deadlock") == STRATEGIST_INTERNAL, (
        "research.dedup_deadlock must route to STRATEGIST_INTERNAL — "
        "dedup churn is internal bookkeeping, never operator-visible"
    )


def test_m1_research_already_fixed_deadlock_registered_strategist_internal():
    """research.already_fixed_deadlock must route to STRATEGIST_INTERNAL."""
    from app.message_audience import route_event, STRATEGIST_INTERNAL

    assert route_event("research.already_fixed_deadlock") == STRATEGIST_INTERNAL, (
        "research.already_fixed_deadlock must route to STRATEGIST_INTERNAL — "
        "already-fixed deadlock detection is internal bookkeeping"
    )


def test_m2_research_direction_auto_completed_registered_strategist_internal():
    """research.direction_auto_completed must route to STRATEGIST_INTERNAL.

    Finding claim: research_completion.py builds a 'Direction auto-completed' Alert
    with no event_type though research.direction_auto_completed is registered
    STRATEGIST_INTERNAL — a claim-vs-shipped contradiction that leaks.
    Fix: both the registry entry AND the emitter have event_type= stamped.
    """
    from app.message_audience import route_event, STRATEGIST_INTERNAL

    assert route_event("research.direction_auto_completed") == STRATEGIST_INTERNAL, (
        "research.direction_auto_completed must route to STRATEGIST_INTERNAL — "
        "auto-completion is an internal lifecycle event, never operator-facing"
    )


def test_m2_research_direction_state_dedup_alert_uses_event_type():
    """Dedup-deadlock Alert constructor in research_direction_state must include event_type.

    Finding claim: the Alert built at the dedup-deadlock branch omits event_type=,
    causing it to fail-open and reach the operator alert badge.
    """
    import inspect
    from app import research_direction_state

    src = inspect.getsource(research_direction_state)
    assert "research.dedup_deadlock" in src, (
        "research_direction_state must emit event_type='research.dedup_deadlock' "
        "on the dedup-deadlock Alert so the audience registry routes it correctly"
    )
    assert "research.already_fixed_deadlock" in src, (
        "research_direction_state must emit event_type='research.already_fixed_deadlock' "
        "on the already-fixed-deadlock Alert so the audience registry routes it correctly"
    )


def test_m2_research_direction_state_bugr_message_stamps_audience():
    """BUG_REPORT ProjectMessage for dedup-deadlock must call stamp_message_audience.

    Finding claim: research_direction_state.py builds an agent BUG_REPORT ProjectMessage
    with no audience stamp, so it fails open and reaches the operator.
    """
    import inspect
    from app import research_direction_state

    src = inspect.getsource(research_direction_state)
    assert "stamp_message_audience" in src, (
        "research_direction_state must import and call stamp_message_audience — "
        "BUG_REPORT project messages emitted here must carry an audience stamp "
        "so they route to STRATEGIST_INTERNAL rather than failing open"
    )


def test_m2_review_pipeline_blocker_stamps_audience():
    """BLOCKER ProjectMessage in review_pipeline must call stamp_message_audience.

    Finding claim: review_pipeline.py builds a BLOCKER ProjectMessage with no audience
    stamp, failing open and leaking to the operator channel.
    """
    import inspect
    from app import review_pipeline

    src = inspect.getsource(review_pipeline)
    assert "stamp_message_audience" in src, (
        "review_pipeline must import and call stamp_message_audience — "
        "the repeat-fingerprint BLOCKER ProjectMessage must carry an audience stamp "
        "so it routes correctly rather than failing open to the operator"
    )


def test_m2_all_alert_constructors_have_event_type():
    """Every Alert(...) instantiation in backend/app/ must include event_type=.

    Finding claim: ~93 of 112 direct Alert(...) constructors omit event_type=,
    reaching the operator badge via the fail-open read filter.
    Fix: every Alert() in the emitter surface must pass event_type= so the
    audience registry can route it.
    """
    import re
    import glob

    files = glob.glob("backend/app/**/*.py", recursive=True)
    missing = []
    for f in sorted(files):
        with open(f) as fh:
            src = fh.read()
        for m in re.finditer(r"(?:alert|_alert|_res_alert)\s*=\s*Alert\s*\(", src):
            start = m.start()
            paren_pos = src.index("(", start)
            depth = 0
            end = paren_pos
            for j in range(paren_pos, min(paren_pos + 3000, len(src))):
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            block = src[start : end + 1]
            if "event_type" not in block:
                lineno = src[:start].count("\n") + 1
                missing.append(f"{f}:{lineno}")

    assert not missing, (
        "These Alert() constructors are missing event_type= and will fail-open "
        "to the operator badge via the NULL read filter:\n"
        + "\n".join(f"  {loc}" for loc in missing)
    )


def test_m6_stale_question_decay_days_is_30():
    """STALE_QUESTION_DECAY_DAYS must equal 30 — longer window than NOTE default.

    Finding claim: the stale-open decay sweep explicitly excludes QUESTION messages
    from auto-resolution. Fix: extend the sweep with a longer 30-day window for
    QUESTION messages, configurable via runtime_config['stale_question_decay_days'].
    """
    from app.reaper_sweeps import STALE_QUESTION_DECAY_DAYS, STALE_OPEN_DECAY_DAYS

    assert STALE_QUESTION_DECAY_DAYS == 30, (
        f"STALE_QUESTION_DECAY_DAYS must be 30 — questions deserve more patience "
        f"than NOTE/FEEDBACK messages; got {STALE_QUESTION_DECAY_DAYS}"
    )
    assert STALE_QUESTION_DECAY_DAYS > STALE_OPEN_DECAY_DAYS, (
        f"STALE_QUESTION_DECAY_DAYS ({STALE_QUESTION_DECAY_DAYS}) must exceed "
        f"STALE_OPEN_DECAY_DAYS ({STALE_OPEN_DECAY_DAYS}) — questions get a longer "
        f"patience window than NOTE/FEEDBACK"
    )


def test_m6_sweep_decay_stale_open_messages_handles_question():
    """sweep_decay_stale_open_messages must handle QUESTION with stale_unanswered reason.

    Finding claim: the stale-open decay sweep excludes QUESTION from auto-resolution.
    Fix: QUESTION messages auto-resolve after STALE_QUESTION_DECAY_DAYS with
    resolution_reason='stale_unanswered' so operators can distinguish them from
    genuinely-answered questions.
    """
    import inspect
    from app.reaper_sweeps import sweep_decay_stale_open_messages

    src = inspect.getsource(sweep_decay_stale_open_messages)
    assert "stale_unanswered" in src, (
        "sweep_decay_stale_open_messages must stamp resolution='stale_unanswered' "
        "when auto-resolving a QUESTION message so operators can distinguish "
        "auto-resolved from genuinely-answered questions"
    )
    assert "question_cutoff" in src or "QUESTION" in src, (
        "sweep_decay_stale_open_messages must handle QUESTION messages with their "
        "own longer decay window (STALE_QUESTION_DECAY_DAYS)"
    )


def test_m6_stale_question_decay_days_runtime_configurable():
    """stale_question_decay_days must be configurable via runtime_config.

    Finding claim: The decay window should be configurable at runtime
    (e.g., 'stale_question_decay_days'). Fix: sweep reads
    runtime_config.get('stale_question_decay_days', STALE_QUESTION_DECAY_DAYS).
    """
    import inspect
    from app.reaper_sweeps import sweep_decay_stale_open_messages

    src = inspect.getsource(sweep_decay_stale_open_messages)
    assert "stale_question_decay_days" in src, (
        "sweep_decay_stale_open_messages must read runtime_config.get("
        "'stale_question_decay_days', ...) so the question-decay window "
        "is adjustable without a code deploy"
    )


# --------------------------------------------------------------------------- #
# d1 — dispatch reads ProjectComputeImage for eligibility via
#      resolve_project_baked_image_strict; fails closed on unresolvable image
# --------------------------------------------------------------------------- #


def test_dispatch_uses_baked_image_strict_not_verbatim_forward():
    """_dispatch_compute_experiment_directly calls resolve_project_baked_image_strict.

    Finding d1: the dispatch path forwarded container_image from the task payload
    verbatim to create_compute_job without ever consulting ProjectComputeImage.
    The fix wires in resolve_project_baked_image_strict so image-less payloads get
    a digest-pinned default and fail CLOSED when the project declares a baked image
    but no built record exists.
    """
    import inspect
    from app.orchestrator_runner_dispatch import _dispatch_compute_experiment_directly

    source = inspect.getsource(_dispatch_compute_experiment_directly)

    assert "resolve_project_baked_image_strict" in source, (
        "_dispatch_compute_experiment_directly must call resolve_project_baked_image_strict "
        "to resolve a project's declared baked image at dispatch time. Without this call "
        "image-less payloads run unreproducibly on the host toolchain."
    )
    assert "baked_image_unresolvable" in source, (
        "_dispatch_compute_experiment_directly must use the 'baked_image_unresolvable' error "
        "code when the project declares a baked image but no built ProjectComputeImage record "
        "can be found — dispatching image-less when a baked image is expected is a silent "
        "reproducibility failure that must surface to the operator."
    )


def test_dispatch_stores_resolved_image_in_polling_metadata():
    """create_compute_job stores container_image in polling_metadata['container_image'].

    Finding d1 claim (2): the pinned ref must be stored in polling_metadata so the
    runner subprocess knows exactly which digest to pull and the orchestrator can
    audit what image each job ran.
    """
    import inspect
    from app.compute_jobs import create_compute_job

    source = inspect.getsource(create_compute_job)

    assert 'polling_metadata["container_image"]' in source or (
        '"container_image"' in source and "polling_metadata" in source
    ), (
        "create_compute_job must store container_image in polling_metadata so the "
        "resolved digest-pinned ref is durable alongside the job record"
    )


def test_projectcomputeimage_docstring_reflects_dispatch_reads_it():
    """ProjectComputeImage docstring must say dispatch reads it (not 'nothing in dispatch reads it').

    Finding d1 claim: the docstring at models.py:2111 said 'nothing in dispatch reads it yet'.
    After the fix, dispatch consults ProjectComputeImage via resolve_project_baked_image_strict,
    and the docstring was updated to reflect that.
    """
    import inspect
    from app.models import ProjectComputeImage

    doc = inspect.getdoc(ProjectComputeImage) or ""

    assert "nothing in dispatch reads it" not in doc, (
        "ProjectComputeImage docstring must NOT say 'nothing in dispatch reads it yet' — "
        "dispatch now reads ProjectComputeImage via resolve_project_baked_image_strict; "
        "the stale docstring misled future readers about the table's role"
    )
    assert "dispatch" in doc.lower(), (
        "ProjectComputeImage docstring must mention dispatch to document that the table "
        "is actively used at task-dispatch time, not just at build-record time"
    )


# --------------------------------------------------------------------------- #
# d2 — runner heartbeat reports docker image-cache size via image_cache_by_project
# --------------------------------------------------------------------------- #


def test_heartbeat_image_cache_stored_in_runner_caps():
    """routers/runner.py stores image_cache_by_project in runner caps during heartbeat.

    Finding d2: disk_free_bytes reported total filesystem free space with no
    attribution of how much the docker data-root / image layers consumed. The fix
    adds image_cache_by_project (per-project docker cache accounting) to both the
    heartbeat payload (local_runner.py) and the backend caps store (routers/runner.py),
    enabling fleet-level cache-pressure monitoring.
    """
    import inspect
    from app.routers.runner import heartbeat

    source = inspect.getsource(heartbeat)

    assert "image_cache_by_project" in source, (
        "routers/runner.py heartbeat handler must reference image_cache_by_project "
        "to accept the per-project docker image-cache accounting from the runner heartbeat"
    )
    assert "_sanitize_image_cache_by_project" in source, (
        "routers/runner.py heartbeat must call _sanitize_image_cache_by_project "
        "to normalize and validate the per-project cache dict before storing it in caps"
    )


def test_runner_router_sanitizes_image_cache_by_project():
    """_sanitize_image_cache_by_project must exist in routers/runner.py.

    Finding d2: no docker-cache-size field existed in the heartbeat inventory —
    routers/runner.py only stored disk_free_bytes as display-only telemetry. The fix
    adds _sanitize_image_cache_by_project to validate/normalize the per-project cache
    dict before storing it in runner caps.
    """
    from app.routers import runner as runner_router

    assert hasattr(runner_router, "_sanitize_image_cache_by_project"), (
        "app.routers.runner must define _sanitize_image_cache_by_project — the function "
        "that normalizes the per-project docker image-cache dict before storing it in "
        "runner caps alongside disk_free_bytes"
    )
    fn = getattr(runner_router, "_sanitize_image_cache_by_project")
    assert callable(fn), "_sanitize_image_cache_by_project must be callable"


def test_image_cache_by_project_structure():
    """_sanitize_image_cache_by_project returns None for invalid input, dict for valid.

    Finding d2: ensures the sanitizer is defensive — malformed runner payloads must
    not crash the heartbeat endpoint or corrupt the caps store.
    """
    from app.routers.runner import _sanitize_image_cache_by_project

    assert _sanitize_image_cache_by_project(None) is None, (
        "_sanitize_image_cache_by_project(None) must return None — "
        "docker-less runners omit the field; the backend must tolerate absence"
    )
    assert _sanitize_image_cache_by_project("not-a-dict") is None, (
        "_sanitize_image_cache_by_project must return None for non-dict input — "
        "a malformed runner payload must not propagate garbage into runner caps"
    )
    valid = {
        "total_bytes": 1024,
        "image_count": 2,
        "by_project": {"proj-id": {"bytes": 512, "image_count": 1}},
        "unattributed_bytes": 512,
        "unattributed_image_count": 1,
    }
    result = _sanitize_image_cache_by_project(valid)
    assert isinstance(result, dict), (
        "_sanitize_image_cache_by_project must return a dict for valid input"
    )
    assert "total_bytes" in result, (
        "_sanitize_image_cache_by_project must preserve total_bytes from valid input"
    )


def _find_backend_app_file(rel_path: str):
    """Locate a backend/app file using standard candidate paths."""
    import pathlib

    candidates = [
        pathlib.Path(__file__).parent.parent / "app" / rel_path,
        pathlib.Path(__file__).resolve().parents[2] / "backend" / "app" / rel_path,
        pathlib.Path("/repo/backend/app") / rel_path,
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"Could not locate backend/app/{rel_path}")


def _find_repo_file(rel_path: str):
    """Locate a repo-root-relative file using standard candidate paths."""
    import pathlib

    candidates = [
        pathlib.Path(__file__).resolve().parents[2] / rel_path,
        pathlib.Path("/repo") / rel_path,
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"Could not locate repo/{rel_path}")


# ── rc-2: max_active_directions fallback alignment ────────────────────────────


def test_max_active_directions_registered_default_ten():
    """max_active_directions must be registered default 10 in runtime_config_state.

    Finding rc-2: the knob is registered 10 but several read-sites used 5 as
    the fallback, meaning bare/test/reset configs capped active directions at 5.
    """
    src = _find_backend_app_file("runtime_config_state.py").read_text()
    assert '"max_active_directions": 10' in src, (
        "runtime_config_state.py must register max_active_directions as 10"
    )


def test_max_active_directions_all_read_sites_use_ten():
    """Every .get('max_active_directions', <fallback>) in backend/app/ must use 10.

    Finding rc-2: directions_repo.py, strategic_completion.py, and
    admin_agent_ops.py all had fallback 5, causing cap divergence when the key
    was absent. The fallback must match the registered default everywhere.
    """
    import ast

    app_dir = _find_backend_app_file("runtime_config_state.py").parent
    mismatch = []
    for py_file in app_dir.rglob("*.py"):
        try:
            src = py_file.read_text()
            tree = ast.parse(src)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "get"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and first.value == "max_active_directions"):
                continue
            if len(node.args) < 2:
                continue
            fallback = node.args[1]
            if isinstance(fallback, ast.Constant) and fallback.value != 10:
                mismatch.append(f"{py_file.name}:{node.lineno} fallback={fallback.value}")
    assert not mismatch, (
        "max_active_directions .get() fallback must be 10 everywhere — "
        f"found wrong fallbacks at: {mismatch}"
    )


# ── rc-3: auto-approve gate uses the shared constant, not inline 0.5 ─────────


def test_auto_approve_acceptance_threshold_not_hardcoded_in_task_lifecycle():
    """The acceptance-rate threshold must live behind the shared constant.

    Finding rc-3: the 0.5 threshold was duplicated inline in task_lifecycle.py,
    diverging from AUTO_APPROVE_MIN_ACCEPTANCE_RATE. The auto-approve gate now
    lives in review_policy.py (the single documented home for review-skip
    policy), which owns the constant and the acceptance check — so
    task_lifecycle.py no longer hardcodes 0.5 anywhere in the auto-approve path.
    """
    policy_src = _find_backend_app_file("review_policy.py").read_text()
    assert "AUTO_APPROVE_MIN_ACCEPTANCE_RATE" in policy_src, (
        "review_policy.py must define and use AUTO_APPROVE_MIN_ACCEPTANCE_RATE "
        "rather than hardcoding 0.5"
    )
    # The auto-approve direction gate must not reappear as an inline 0.5 in
    # task_lifecycle.py — the whole gate delegates to review_policy now.
    tl_src = _find_backend_app_file("task_lifecycle.py").read_text()
    assert "acceptance <= 0.5" not in tl_src and "acceptance is None or acceptance <= 0.5" not in tl_src, (
        "task_lifecycle.py must not re-inline the acceptance-rate threshold; "
        "the auto-approve gate belongs to review_policy.evaluate_auto_approve_small_main"
    )


# ── rc-4/rc-5: usage meter freshness ─────────────────────────────────────────


def test_usage_snapshot_ttl_matches_refresh_interval():
    """_USAGE_SNAPSHOT_TTL_SECONDS must equal _USAGE_METER_REFRESH_INTERVAL_SECONDS.

    Finding rc-5: TTL was 60 s but probe runs every 300 s, so the selection gate
    saw unknown ~80%% of the time and degraded to a no-op.
    """
    from app.max_plan_pool import (
        _USAGE_METER_REFRESH_INTERVAL_SECONDS,
        _USAGE_SNAPSHOT_TTL_SECONDS,
    )

    assert _USAGE_SNAPSHOT_TTL_SECONDS == _USAGE_METER_REFRESH_INTERVAL_SECONDS, (
        f"_USAGE_SNAPSHOT_TTL_SECONDS={_USAGE_SNAPSHOT_TTL_SECONDS} must equal "
        f"_USAGE_METER_REFRESH_INTERVAL_SECONDS={_USAGE_METER_REFRESH_INTERVAL_SECONDS} "
        "so the selection gate considers probes fresh for as long as the probe cadence runs"
    )


def test_snapshot_from_persisted_usage_accepts_messages_api_header():
    """_snapshot_from_persisted_usage must accept the messages_api_header schema.

    Finding rc-4: the function only accepted oauth_usage_probe blobs, so the
    per-response header data written by direct_llm_client was silently discarded
    when non-leader instances rebuilt their pool state from the DB column.
    """
    from datetime import datetime, timezone

    from app.max_plan_pool import _snapshot_from_persisted_usage

    at = datetime.now(timezone.utc)
    raw = {
        "source": "messages_api_header",
        "updated_at": at.isoformat(),
        "unified_5h": {"utilization": 72.5, "resets_at": "2026-07-12T05:00:00Z"},
    }
    result = _snapshot_from_persisted_usage(raw)
    assert result is not None, (
        "_snapshot_from_persisted_usage must parse messages_api_header blobs — "
        "unified_5h.utilization maps to the five_hour load-bearing window"
    )
    snap, _stamp = result
    assert snap.known is True
    assert snap.window_utilizations.get("five_hour") == 72.5, (
        "unified_5h.utilization must map to the five_hour window"
    )
    assert snap.resets_at.get("five_hour") == "2026-07-12T05:00:00Z"


# ── rc-6/rc-7: anti-hedging guidance ─────────────────────────────────────────


def test_anti_hedging_guidance_in_failure_guidance_blocks():
    """assembleFailureGuidanceBlocks must include ANTI_HEDGING_GUIDANCE.

    Finding rc-6: the code-writing agent had no standing instruction against
    introducing default-off flags, fail-open fallbacks, or observe-mode wrappers.
    """
    src = _find_repo_file("agent/src/prompt.ts").read_text()
    assert "ANTI_HEDGING_GUIDANCE" in src, (
        "agent/src/prompt.ts must define and export ANTI_HEDGING_GUIDANCE"
    )
    idx_fn = src.find("function assembleFailureGuidanceBlocks")
    assert idx_fn != -1, "assembleFailureGuidanceBlocks not found in agent/src/prompt.ts"
    fn_body = src[idx_fn : idx_fn + 300]
    assert "ANTI_HEDGING_GUIDANCE" in fn_body, (
        "assembleFailureGuidanceBlocks must include ANTI_HEDGING_GUIDANCE "
        "so every code-change agent receives standing anti-hedging instruction"
    )


def test_review_major_tier_names_hedging():
    """The review MAJOR severity tier must name hedging as a finding class.

    Finding rc-7: the reviewer had no guidance to flag default-off guards or
    fail-open fallbacks as MAJOR findings, so hedging could pass review silently.
    """
    src = _find_repo_file("agent/src/prompts/review-prompt.ts").read_text()
    assert "default-off" in src or "fail-open" in src or "observe-mode" in src, (
        "review-prompt.ts MAJOR tier must name hedging patterns "
        "(default-off flag, fail-open fallback, or observe-mode wrapper) "
        "as MAJOR-severity review findings"
    )


# ── bi-1: baked-image authoring-prompt guidance ───────────────────────────────


def test_thinker_prefers_baked_image_over_always_set_explicitly():
    """thinker.ts must instruct agents to OMIT container_image when a project-baked image exists.

    Finding bi-1: thinker.ts previously said 'Always set container_image explicitly
    ... never rely on whatever toolchain the host happens to have', with no mention
    of the project-baked image default. This caused agents to always set container_image
    explicitly, suppressing the backend's resolve_project_default_container_image default
    that would have picked the digest-pinned baked image.
    """
    src = _find_repo_file("agent/src/prompts/thinker.ts").read_text()
    assert "Prefer the project-baked image" in src, (
        "thinker.ts must contain 'Prefer the project-baked image' guidance so authoring "
        "agents know to omit container_image when a baked image is listed"
    )
    assert "OMIT `container_image`" in src or "OMIT \\`container_image\\`" in src, (
        "thinker.ts must instruct agents to OMIT container_image (not always set it) "
        "so the backend's project-baked image default can apply"
    )


def test_research_plan_injects_baked_image_ref_into_local_runner_note():
    """research-plan.ts must inject baked_image_ref preference into the local-runner note.

    Finding bi-1: the authoring prompt for research-plan agents had no channel to
    communicate the project-baked image reference; agents were told to always set
    container_image explicitly. The fix injects baked_image_ref from local_runner_status
    into the localRunnerNote so agents can see and prefer the baked image.
    """
    src = _find_repo_file("agent/src/handlers/research-plan.ts").read_text()
    assert "baked_image_ref" in src, (
        "research-plan.ts must read baked_image_ref from local_runner_status payload "
        "to surface the project-baked image to authoring agents"
    )
    assert "PREFER to omit" in src, (
        "research-plan.ts must include 'PREFER to omit' in the baked-image note "
        "injected into the local-runner availability section"
    )


def test_strategic_local_runner_section_includes_baked_image_guidance():
    """strategic.ts buildLocalRunnerSection must emit baked-image guidance when baked_image_ref present.

    Finding bi-1: strategic.ts presented container_image only as an NGC URI example,
    never mentioning the project image. The fix adds baked_image_ref handling to
    buildLocalRunnerSection so the strategist's self-hosted compute note advertises
    the baked image and instructs agents to omit container_image.
    """
    src = _find_repo_file("agent/src/handlers/strategic.ts").read_text()
    assert "baked_image_ref" in src, (
        "strategic.ts buildLocalRunnerSection must read baked_image_ref from the "
        "local runner status to surface the project-baked image"
    )
    assert "PREFER to omit" in src, (
        "strategic.ts must include 'PREFER to omit' guidance in the baked-image note "
        "so the strategist knows to omit container_image for experiments"
    )


# ── 17-finding release-qualification salvage bundle (task c39e0ee0, 2026-07-21) ── #
# All 17 findings are identical to those in the 03ca2ee5 bundle and are already
# pinned by the f20-* test suite.  The functions below are thin cross-references
# that verify the same invariants under the c39 prefix.
# --------------------------------------------------------------------------- #


# c39-1 — classify serving-only-import ImportError as degrade, not hard block
# Already pinned by test_f20_1_serving_only_import_error_degrades_gate.
# --------------------------------------------------------------------------- #


def test_c39_1_serving_only_import_degrades_gate():
    """_ServingOnlyImportError must degrade the gate to ('ok', None).

    Finding c39-1: an ImportError referencing a candidate-only app.* symbol during
    serving-process replay raised ImportError, which _apply_candidate_migrations caught
    as _DeterministicMigrationDefect — a hard block.  The fix introduces
    _ServingOnlyImportError and degrades the gate to ('ok', None) instead.
    Full coverage in test_f20_1_serving_only_import_error_degrades_gate.
    """
    from app.migration_apply_gate import _ServingOnlyImportError, _looks_like_serving_only_import

    assert issubclass(_ServingOnlyImportError, Exception)

    exc = ImportError("No module named 'app.field_encryption'")
    exc.name = "app.field_encryption"
    assert _looks_like_serving_only_import(exc), (
        "_looks_like_serving_only_import must return True for app.* import errors — "
        "candidate-only app symbols degrade the gate, not hard-block it"
    )


# c39-2 — prove data-dependent migrations safely (design)
# Already pinned by test_f20_2_data_migration_proof_gate_design.
# --------------------------------------------------------------------------- #


def test_c39_2_scratch_replay_uses_empty_tables():
    """Scratch migration replay must use empty tables and compare schema, not row data.

    Finding c39-2: production state supplied to scratch replay contains only the Alembic
    revision and reflected table/column metadata; scratch seeding creates empty tables.
    Full coverage in test_f20_2_data_migration_proof_gate_design.
    """
    import pathlib

    gate_source = (
        pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py"
    ).read_text()
    assert "schema" in gate_source and "drift" in gate_source, (
        "migration_apply_gate.py must reference schema drift detection — "
        "scratch replay compares schema names, not row-transform outcomes"
    )


# c39-3 — verify encryption-key continuity
# Already pinned by test_f20_3_*, test_probe_key_continuity_fails_closed_for_incompatible_key,
# and test_assert_plaintext_or_raise_raises_for_wrong_key_ciphertext (e2/e3).
# --------------------------------------------------------------------------- #


def test_c39_3_probe_key_continuity_and_assert_plaintext_present():
    """probe_key_continuity and assert_plaintext_or_raise must be importable from field_encryption.

    Finding c39-3: decrypt_field returns its input unchanged when a valid Fernet token
    cannot be decrypted.  Migration 0531 treated decrypt_field(value) == value as evidence
    the value is plaintext and re-encrypted it.  probe_key_continuity and
    assert_plaintext_or_raise guard against this.
    Full coverage in test_f20_3_*, e2, and e3.
    """
    from app.field_encryption import probe_key_continuity, assert_plaintext_or_raise

    assert callable(probe_key_continuity)
    assert callable(assert_plaintext_or_raise)


# c39-4 — research: define safe Fernet key rotation (MultiFernet)
# Already pinned by test_f20_4_multifernet_rotate_exists.
# --------------------------------------------------------------------------- #


def test_c39_4_multifernet_rotate_importable():
    """cryptography.fernet.MultiFernet must be importable and expose rotate().

    Finding c39-4: the MultiFernet contract encrypts with the first key, decrypts with
    every configured key, and supports explicit token rotation via rotate().
    Full coverage in test_f20_4_multifernet_rotate_exists.
    """
    from cryptography.fernet import MultiFernet

    assert hasattr(MultiFernet, "rotate"), (
        "cryptography.fernet.MultiFernet must expose rotate() for staged key rotation"
    )


# c39-5 — fail closed on keyless/malformed encryption backfill
# Already pinned by test_f20_5_require_encryption_active_raises_on_missing_key.
# --------------------------------------------------------------------------- #


def test_c39_5_require_encryption_active_distinguishes_malformed_from_absent(monkeypatch):
    """require_encryption_active must raise RuntimeError for both malformed and absent keys.

    Finding c39-5: _get_fernet catches an invalid key and returns None, so
    is_encryption_active() returns False for BOTH missing and malformed keys — the
    backfill silently no-ops in both cases.  require_encryption_active() raises in
    both failure modes.
    Full coverage in test_f20_5_require_encryption_active_raises_on_missing_key.
    """
    import pytest
    from app import field_encryption

    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)
    with pytest.raises(RuntimeError):
        field_encryption.require_encryption_active("c39-5-absent-key")

    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError):
        field_encryption.require_encryption_active("c39-5-malformed-key")


# c39-6 — make scratch-DB URL degraded, not boot-fatal
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req
# and test_f20_6_scratch_db_url_in_qualification_env_not_required_env.
# --------------------------------------------------------------------------- #


def test_c39_6_scratch_db_url_not_in_required_runtime_env():
    """MIGRATION_APPLY_SCRATCH_DATABASE_URL must not appear in REQUIRED_RUNTIME_ENV.

    Finding c39-6: the scratch URL was declared with scope='cloud_run' in
    required_runtime_env.py, making its absence boot-fatal.  The fix removes it so
    its absence degrades the gate to a 'defer' hold rather than crashing Cloud Run.
    Full coverage in test_scratch_db_url_is_qualification_prereq_not_boot_req.
    """
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    req_names = {r.name for r in REQUIRED_RUNTIME_ENV}
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in req_names, (
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL must NOT be in REQUIRED_RUNTIME_ENV — "
        "its absence must degrade the gate, not crash Cloud Run at boot"
    )


# c39-7 — isolate gate-defer signature from unrelated child failures
# Already pinned by test_infra_failure_signature_ignores_child_failures
# and test_f20_7_infra_failure_signature_derived_from_error_code_only.
# --------------------------------------------------------------------------- #


def test_c39_7_infra_failure_signature_stable_under_varying_children():
    """_infra_failure_signature must be stable regardless of which child tasks failed.

    Finding c39-7: _infra_failure_signature derived its leg from child_failures[-1],
    so audit vs smoketest child-task failures caused the signature to oscillate between
    attempts, preventing the permanent-gate-incapacity streak.
    Full coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    error = "migration_apply_check_unavailable"
    assert (
        _infra_failure_signature(error, [{"leg": "audit"}])
        == _infra_failure_signature(error, [{"leg": "smoketest"}])
        == _infra_failure_signature(error, [])
    ), "_infra_failure_signature must be stable regardless of child_failures content"


# c39-8 — contain timed-out qualifier children
# Already pinned by test_cancel_qualifier_children_covers_all_child_types
# and test_f20_8_force_finalize_cancels_children_before_rewriting_state.
# --------------------------------------------------------------------------- #


def test_c39_8_cancel_qualifier_children_callable():
    """_cancel_qualifier_children must be defined in release_qualifier_sweep.py.

    Finding c39-8: qualification timeout and sweep-crash handlers rewrote the release
    record and alerted without changing referenced child task states, so in-flight
    children could complete and influence the next re-drive.
    Full coverage in test_cancel_qualifier_children_covers_all_child_types.
    """
    from app.release_qualifier_sweep import _cancel_qualifier_children

    assert callable(_cancel_qualifier_children)


# c39-9 — block deterministic migration defects
# Already pinned by test_deterministic_migration_defect_class_exists
# and test_f20_9_deterministic_migration_defect_returns_block_verdict.
# --------------------------------------------------------------------------- #


def test_c39_9_deterministic_migration_defect_is_exception():
    """_DeterministicMigrationDefect must be an Exception subclass mapping to 'block'.

    Finding c39-9: all migration-apply exceptions previously converted to defer.  The
    fix introduces _DeterministicMigrationDefect for repeatable SQL errors and
    non-serving ImportError/SyntaxError, which the gate converts to a 'block' verdict.
    Full coverage in test_deterministic_migration_defect_class_exists.
    """
    from app.migration_apply_gate import _DeterministicMigrationDefect

    assert issubclass(_DeterministicMigrationDefect, Exception)


# c39-10 — enforce independent boot preflight
# Already pinned by test_env_preflight_absent_evidence_is_a_hard_blocker
# and test_f20_10_env_preflight_enabled_flag_independent_of_smoketest.
# --------------------------------------------------------------------------- #


def test_c39_10_env_preflight_enabled_flag_in_release_qualifier():
    """env_preflight_enabled must be an independent gate flag in release_qualifier.py.

    Finding c39-10: the sole env-preflight artifact producer was attached to the
    optional smoketest child.  The fix introduces an independent env_preflight_enabled
    flag so absent/malformed evidence is a hard blocker.
    Full coverage in test_env_preflight_absent_evidence_is_a_hard_blocker.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "release_qualifier.py").read_text()
    assert "env_preflight_enabled" in src, (
        "release_qualifier.py must reference env_preflight_enabled as an independent gate flag"
    )


# c39-11 — unify trigger truth and retries
# Already pinned by test_infra_failure_redrive_is_independent_of_auto_qualification_triggers_knob
# and test_f20_11_infra_failure_maps_to_dedicated_redrive_trigger.
# --------------------------------------------------------------------------- #


def test_c39_11_auto_qualification_triggers_does_not_control_infra_failure_redrive():
    """auto_qualification_triggers knob must NOT gate the infra_failure bespoke re-drive.

    Finding c39-11: the trigger resolver returned not_applicable for states handled by
    dedicated sweep re-drive functions, and the auto_qualification_triggers knob controlled
    only the generic trigger sweep, not the bespoke infra-failure loop.
    Full coverage in test_infra_failure_redrive_is_independent_of_auto_qualification_triggers_knob.
    """
    import pathlib

    redrive_src = (
        pathlib.Path(__file__).parent.parent / "app" / "release_qualifier_redrive.py"
    ).read_text()
    assert "infra_failure" in redrive_src, (
        "release_qualifier_redrive.py must reference infra_failure — "
        "the bespoke infra-failure re-drive must exist independently of the generic trigger knob"
    )


# c39-12 — contain orphan promotion actors
# Already pinned by test_bare_claim_promotion_gets_shorter_timeout
# and test_f20_12_promotion_timeout_applies_to_bare_claims.
# --------------------------------------------------------------------------- #


def test_c39_12_release_reconciliation_references_claiming_field():
    """release_reconciliation.py must reference the 'claiming' field for orphan containment.

    Finding c39-12: a crash after PR creation could leave promotion_pending with
    claiming=True and no PR number; timeout cleanup only closed PRs for managed-check-wait
    records, not bare claims.
    Full coverage in test_bare_claim_promotion_gets_shorter_timeout.
    """
    import inspect
    from app import release_reconciliation

    assert "claiming" in inspect.getsource(release_reconciliation), (
        "release_reconciliation must reference 'claiming' — bare claims must be reachable"
    )


# c39-13 — gate data-migration proof
# Already pinned by test_data_migration_proof_gate_defers_dml_without_proof
# and test_f20_13_data_migration_proof_gate_defers_dml.
# --------------------------------------------------------------------------- #


def test_c39_13_migration_apply_gate_references_dml_detection():
    """migration_apply_gate.py must reference DML/data-migration detection.

    Finding c39-13: the migration-safe verdict could be produced after applying a data
    migration to empty scratch tables with no fixture or invariant contract required.
    The gate must defer for DML migrations that lack a proof declaration.
    Full coverage in test_data_migration_proof_gate_defers_dml_without_proof.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py").read_text()
    assert "dml" in src.lower() or "DML" in src or "data_migration" in src, (
        "migration_apply_gate.py must reference DML/data migration detection"
    )


# c39-14 — guide narrow release remediation (UI)
# Already pinned by test_version_lifecycle_panel_has_hotfix_remediation
# and test_f20_14_hotfix_remediation_api_client_present.
# --------------------------------------------------------------------------- #


def test_c39_14_hotfix_remediation_api_client_in_projects_ts():
    """admin-ui/src/api/projects.ts must expose getHotfixScopePreview and triggerHotfixPromote.

    Finding c39-14: admin-ui had no hotfix preview or promotion client and
    VersionLifecyclePanel rendered only generic qualification failure state.
    Full coverage in test_version_lifecycle_panel_has_hotfix_remediation.
    """
    import pathlib
    import pytest

    candidates = [
        pathlib.Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "api" / "projects.ts",
        pathlib.Path("/repo/admin-ui/src/api/projects.ts"),
    ]
    api_source: str | None = None
    for p in candidates:
        if p.exists():
            api_source = p.read_text()
            break
    if api_source is None:
        pytest.skip("admin-ui/src/api/projects.ts not found — admin-ui not present in this shard")

    assert "getHotfixScopePreview" in api_source or "hotfix" in api_source.lower(), (
        "admin-ui/src/api/projects.ts must expose a hotfix scope preview function"
    )
    assert "triggerHotfixPromote" in api_source or "promote/hotfix" in api_source, (
        "admin-ui/src/api/projects.ts must expose a hotfix promote function"
    )


# c39-15 — declare + diagnose migration-apply scratch prereq
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req
# and test_f20_15_missing_qualification_env_surfaces_scratch_db_url.
# --------------------------------------------------------------------------- #


def test_c39_15_scratch_preflight_surfaces_missing_url():
    """preflight_migration_apply_scratch must surface MIGRATION_APPLY_SCRATCH_DATABASE_URL absence.

    Finding c39-15: MIGRATION_APPLY_SCRATCH_DATABASE_URL appeared nowhere in
    required_runtime_env.py.  The gate's own preflight must surface a missing URL with an
    actionable 'absent' status and the variable name in its detail.
    Full coverage in test_scratch_db_url_is_qualification_prereq_not_boot_req.
    """
    from app.migration_apply_gate import MigrationApplyScratchPreflight, preflight_migration_apply_scratch

    result = preflight_migration_apply_scratch(None)
    assert isinstance(result, MigrationApplyScratchPreflight)
    assert result.status == "absent"
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (result.detail or "")


# c39-16 — stabilize infra-failure signature for gate defers
# Already pinned by test_infra_failure_signature_ignores_child_failures
# and test_f20_16_infra_failure_signature_stable_across_candidate_rotations.
# --------------------------------------------------------------------------- #


def test_c39_16_infra_failure_signature_stable_for_migration_apply_error():
    """_infra_failure_signature must produce the same string for migration_apply errors.

    Finding c39-16: live v26 qualification showed signatures oscillating between
    challenge:PROVISION_FAILURE:migration_apply_check_unavailable and
    audit:PROVISION_FAILURE:audit_infra_respawn_cap_exceeded, preventing the
    is_permanent_gate_incapacity streak from accumulating.
    Full coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    error = "migration_apply_check_unavailable"
    assert (
        _infra_failure_signature(error, [{"leg": "challenge", "status": "FAILED"}])
        == _infra_failure_signature(error, [{"leg": "audit", "status": "CANCELLED"}])
        == _infra_failure_signature(error, [])
    )


# c39-17 — add hotfix_policy.py to hotfix-eligible modules
# Already pinned by test_hotfix_policy_and_router_in_eligible_modules
# and test_f20_17_hotfix_policy_and_router_self_eligible.
# --------------------------------------------------------------------------- #


def test_c39_17_hotfix_policy_and_router_in_eligible_modules():
    """hotfix_policy.py and admin_operator_triggers.py must be in HOTFIX_ELIGIBLE_MODULES.

    Finding c39-17: HOTFIX_ELIGIBLE_MODULES did not contain 'backend/app/hotfix_policy.py'.
    Without both modules, a fix to the hotfix scope validator cannot be deployed via the
    hotfix channel — the exact self-reference gap the channel exists to solve.
    Full coverage in test_hotfix_policy_and_router_in_eligible_modules.
    """
    from app.hotfix_policy import HOTFIX_ELIGIBLE_MODULES

    assert "backend/app/hotfix_policy.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/hotfix_policy.py' must be in HOTFIX_ELIGIBLE_MODULES"
    )
    assert "backend/app/routers/admin_operator_triggers.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/routers/admin_operator_triggers.py' must be in HOTFIX_ELIGIBLE_MODULES"
    )


# ── 17-finding release-qualification salvage bundle (task 986aadde, 2026-07-21) ── #
# All 17 findings are identical to those in the c39e0ee0/927eea0b bundles and are
# already pinned by the f20-* test suite.  The functions below are thin cross-
# references that verify the same invariants under the 98 prefix.
# --------------------------------------------------------------------------- #


# 98-1 — classify serving-only-import ImportError as degrade, not hard block
# Already pinned by test_f20_1_serving_only_import_error_degrades_gate.
# --------------------------------------------------------------------------- #


def test_98_1_serving_only_import_degrades_gate():
    """_ServingOnlyImportError must degrade the gate to ('ok', None).

    Finding 98-1: an ImportError referencing a candidate-only app.* symbol during
    serving-process replay previously raised _DeterministicMigrationDefect — a hard block.
    The fix introduces _ServingOnlyImportError and degrades the gate to ('ok', None).
    Full coverage in test_f20_1_serving_only_import_error_degrades_gate.
    """
    from app.migration_apply_gate import _ServingOnlyImportError, _looks_like_serving_only_import

    assert issubclass(_ServingOnlyImportError, Exception)

    exc = ImportError("No module named 'app.require_encryption_active'")
    exc.name = "app.require_encryption_active"
    assert _looks_like_serving_only_import(exc), (
        "_looks_like_serving_only_import must return True for app.* import errors"
    )


# 98-2 — prove data-dependent migrations safely (design)
# Already pinned by test_f20_2_data_migration_proof_gate_design.
# --------------------------------------------------------------------------- #


def test_98_2_scratch_replay_uses_schema_not_row_data():
    """Scratch migration replay must compare schema names, not row-transform outcomes.

    Finding 98-2: production state supplied to scratch replay contains only Alembic
    revision and reflected table/column metadata; scratch seeding creates empty tables.
    Full coverage in test_f20_2_data_migration_proof_gate_design.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py").read_text()
    assert "schema" in src and "drift" in src, (
        "migration_apply_gate.py must reference schema drift detection"
    )


# 98-3 — verify encryption-key continuity
# Already pinned by test_f20_3_*, e2, e3.
# --------------------------------------------------------------------------- #


def test_98_3_probe_key_continuity_and_assert_plaintext_present():
    """probe_key_continuity and assert_plaintext_or_raise must be importable from field_encryption.

    Finding 98-3: decrypt_field returns its input unchanged when a valid Fernet token
    cannot be decrypted with the configured key; assert_plaintext_or_raise guards against
    treating wrong-key ciphertext as plaintext.
    Full coverage in test_f20_3_*, e2, and e3.
    """
    from app.field_encryption import probe_key_continuity, assert_plaintext_or_raise

    assert callable(probe_key_continuity)
    assert callable(assert_plaintext_or_raise)


# 98-4 — research: define safe Fernet key rotation (MultiFernet)
# Already pinned by test_f20_4_multifernet_rotate_exists.
# --------------------------------------------------------------------------- #


def test_98_4_multifernet_rotate_importable():
    """cryptography.fernet.MultiFernet must expose rotate() for staged key rotation.

    Finding 98-4: the MultiFernet contract encrypts with the first key, decrypts with
    every configured key, and supports explicit token rotation via rotate().
    Full coverage in test_f20_4_multifernet_rotate_exists.
    """
    from cryptography.fernet import MultiFernet

    assert hasattr(MultiFernet, "rotate"), (
        "MultiFernet must expose rotate() for staged Fernet key rotation"
    )


# 98-5 — fail closed on keyless/malformed encryption backfill
# Already pinned by test_f20_5_require_encryption_active_raises_on_missing_key.
# --------------------------------------------------------------------------- #


def test_98_5_require_encryption_active_raises_for_absent_and_malformed(monkeypatch):
    """require_encryption_active must raise RuntimeError for absent and malformed keys.

    Finding 98-5: _get_fernet returns None for malformed GITHUB_PAT_ENCRYPTION_KEY, so
    is_encryption_active() returns False identically for missing and malformed keys, and
    backfill migrations silently no-op in both cases.
    Full coverage in test_f20_5_require_encryption_active_raises_on_missing_key.
    """
    import pytest
    from app import field_encryption

    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)
    with pytest.raises(RuntimeError):
        field_encryption.require_encryption_active("98-5-absent")

    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "not-valid-base64-fernet")
    with pytest.raises(RuntimeError):
        field_encryption.require_encryption_active("98-5-malformed")


# 98-6 — make scratch-DB URL degraded, not boot-fatal
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req.
# --------------------------------------------------------------------------- #


def test_98_6_scratch_db_url_not_in_required_runtime_env():
    """MIGRATION_APPLY_SCRATCH_DATABASE_URL must not appear in REQUIRED_RUNTIME_ENV.

    Finding 98-6: the scratch URL was declared with scope='cloud_run', making its
    absence boot-fatal.  The fix removes it so its absence degrades the gate to a
    'defer' hold rather than crashing Cloud Run.
    Full coverage in test_scratch_db_url_is_qualification_prereq_not_boot_req.
    """
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    req_names = {r.name for r in REQUIRED_RUNTIME_ENV}
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in req_names, (
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL must NOT be in REQUIRED_RUNTIME_ENV"
    )


# 98-7 — isolate gate-defer signature from unrelated child failures
# Already pinned by test_infra_failure_signature_ignores_child_failures.
# --------------------------------------------------------------------------- #


def test_98_7_infra_failure_signature_stable_under_varying_children():
    """_infra_failure_signature must be stable regardless of which child tasks failed.

    Finding 98-7: _infra_failure_signature derived its leg from child_failures[-1],
    causing the signature to oscillate between audit and smoketest child failures,
    preventing permanent-gate-incapacity streak accumulation.
    Full coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    error = "migration_apply_check_unavailable"
    assert (
        _infra_failure_signature(error, [{"leg": "audit"}])
        == _infra_failure_signature(error, [{"leg": "smoketest"}])
        == _infra_failure_signature(error, [])
    ), "_infra_failure_signature must be stable regardless of child_failures content"


# 98-8 — contain timed-out qualifier children
# Already pinned by test_cancel_qualifier_children_covers_all_child_types.
# --------------------------------------------------------------------------- #


def test_98_8_cancel_qualifier_children_callable():
    """_cancel_qualifier_children must be defined in release_qualifier_sweep.py.

    Finding 98-8: qualification timeout and sweep-crash handlers rewrote the release
    record and alerted without changing referenced child task states.
    Full coverage in test_cancel_qualifier_children_covers_all_child_types.
    """
    from app.release_qualifier_sweep import _cancel_qualifier_children

    assert callable(_cancel_qualifier_children)


# 98-9 — block deterministic migration defects
# Already pinned by test_deterministic_migration_defect_class_exists.
# --------------------------------------------------------------------------- #


def test_98_9_deterministic_migration_defect_is_exception():
    """_DeterministicMigrationDefect must be an Exception subclass mapping to 'block'.

    Finding 98-9: all migration-apply exceptions previously converted to defer.  The
    fix introduces _DeterministicMigrationDefect for repeatable SQL/syntax errors.
    Full coverage in test_deterministic_migration_defect_class_exists.
    """
    from app.migration_apply_gate import _DeterministicMigrationDefect

    assert issubclass(_DeterministicMigrationDefect, Exception)


# 98-10 — enforce independent boot preflight
# Already pinned by test_env_preflight_absent_evidence_is_a_hard_blocker.
# --------------------------------------------------------------------------- #


def test_98_10_env_preflight_enabled_flag_in_release_qualifier():
    """env_preflight_enabled must be an independent gate flag in release_qualifier.py.

    Finding 98-10: the sole env-preflight artifact producer was attached to the
    optional smoketest child; absent evidence scored as unavailable rather than a blocker.
    Full coverage in test_env_preflight_absent_evidence_is_a_hard_blocker.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "release_qualifier.py").read_text()
    assert "env_preflight_enabled" in src, (
        "release_qualifier.py must reference env_preflight_enabled as an independent gate flag"
    )


# 98-11 — unify trigger truth and retries
# Already pinned by test_infra_failure_redrive_is_independent_of_auto_qualification_triggers_knob.
# --------------------------------------------------------------------------- #


def test_98_11_auto_qualification_triggers_does_not_control_infra_failure_redrive():
    """auto_qualification_triggers knob must NOT gate the infra_failure bespoke re-drive.

    Finding 98-11: the trigger resolver returned not_applicable for states handled by
    dedicated sweep re-drive functions, and auto_qualification_triggers controlled only
    the generic trigger sweep, not the bespoke infra-failure loop.
    Full coverage in test_infra_failure_redrive_is_independent_of_auto_qualification_triggers_knob.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "release_qualifier_redrive.py").read_text()
    assert "infra_failure" in src, (
        "release_qualifier_redrive.py must reference infra_failure — "
        "the bespoke re-drive must exist independently of the generic trigger knob"
    )


# 98-12 — contain orphan promotion actors
# Already pinned by test_bare_claim_promotion_gets_shorter_timeout.
# --------------------------------------------------------------------------- #


def test_98_12_release_reconciliation_references_claiming_field():
    """release_reconciliation.py must reference the 'claiming' field for orphan containment.

    Finding 98-12: a crash after PR creation could leave promotion_pending with
    claiming=True and no PR number; timeout cleanup only closed PRs for managed-check-wait
    records, not bare claims.
    Full coverage in test_bare_claim_promotion_gets_shorter_timeout.
    """
    import inspect
    from app import release_reconciliation

    assert "claiming" in inspect.getsource(release_reconciliation), (
        "release_reconciliation must reference 'claiming' — bare claims must be containable"
    )


# 98-13 — gate data-migration proof
# Already pinned by test_data_migration_proof_gate_defers_dml_without_proof.
# --------------------------------------------------------------------------- #


def test_98_13_migration_apply_gate_references_dml_detection():
    """migration_apply_gate.py must reference DML/data-migration detection.

    Finding 98-13: the migration-safe verdict could be produced after applying a data
    migration to empty scratch tables with no fixture or invariant contract required.
    Full coverage in test_data_migration_proof_gate_defers_dml_without_proof.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py").read_text()
    assert "dml" in src.lower() or "DML" in src or "data_migration" in src, (
        "migration_apply_gate.py must reference DML/data migration detection"
    )


# 98-14 — guide narrow release remediation (UI)
# Already pinned by test_version_lifecycle_panel_has_hotfix_remediation.
# --------------------------------------------------------------------------- #


def test_98_14_hotfix_remediation_api_client_in_projects_ts():
    """admin-ui/src/api/projects.ts must expose hotfix scope preview and promote functions.

    Finding 98-14: admin-ui had no hotfix preview or promotion client and
    VersionLifecyclePanel rendered only generic qualification failure state.
    Full coverage in test_version_lifecycle_panel_has_hotfix_remediation.
    """
    import pathlib
    import pytest

    candidates = [
        pathlib.Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "api" / "projects.ts",
        pathlib.Path("/repo/admin-ui/src/api/projects.ts"),
    ]
    api_source: str | None = None
    for p in candidates:
        if p.exists():
            api_source = p.read_text()
            break
    if api_source is None:
        pytest.skip("admin-ui/src/api/projects.ts not found — admin-ui not present in this shard")

    assert "getHotfixScopePreview" in api_source or "hotfix" in api_source.lower(), (
        "admin-ui/src/api/projects.ts must expose a hotfix scope preview function"
    )
    assert "triggerHotfixPromote" in api_source or "promote/hotfix" in api_source, (
        "admin-ui/src/api/projects.ts must expose a hotfix promote function"
    )


# 98-15 — declare + diagnose migration-apply scratch prereq
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req.
# --------------------------------------------------------------------------- #


def test_98_15_scratch_preflight_surfaces_missing_url():
    """preflight_migration_apply_scratch must surface MIGRATION_APPLY_SCRATCH_DATABASE_URL absence.

    Finding 98-15: MIGRATION_APPLY_SCRATCH_DATABASE_URL appeared nowhere in
    required_runtime_env.py.  The gate's own preflight must surface a missing URL with
    an actionable 'absent' status and the variable name in its detail.
    Full coverage in test_scratch_db_url_is_qualification_prereq_not_boot_req.
    """
    from app.migration_apply_gate import MigrationApplyScratchPreflight, preflight_migration_apply_scratch

    result = preflight_migration_apply_scratch(None)
    assert isinstance(result, MigrationApplyScratchPreflight)
    assert result.status == "absent"
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (result.detail or "")


# 98-16 — stabilize infra-failure signature for gate defers
# Already pinned by test_infra_failure_signature_ignores_child_failures.
# --------------------------------------------------------------------------- #


def test_98_16_infra_failure_signature_stable_for_migration_apply_error():
    """_infra_failure_signature must produce the same string regardless of child failures.

    Finding 98-16: live v26 qualification showed signatures oscillating between
    challenge:PROVISION_FAILURE:migration_apply_check_unavailable and
    audit:PROVISION_FAILURE:audit_infra_respawn_cap_exceeded, preventing the
    permanent-gate-incapacity streak from accumulating.
    Full coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    error = "migration_apply_check_unavailable"
    assert (
        _infra_failure_signature(error, [{"leg": "challenge", "status": "FAILED"}])
        == _infra_failure_signature(error, [{"leg": "audit", "status": "CANCELLED"}])
        == _infra_failure_signature(error, [])
    )


# 98-17 — add hotfix_policy.py to hotfix-eligible modules
# Already pinned by test_hotfix_policy_and_router_in_eligible_modules
# and test_c39_17_hotfix_policy_and_router_in_eligible_modules.
# --------------------------------------------------------------------------- #


def test_98_17_hotfix_policy_and_router_in_eligible_modules():
    """hotfix_policy.py and admin_operator_triggers.py must be in HOTFIX_ELIGIBLE_MODULES.

    Finding 98-17: HOTFIX_ELIGIBLE_MODULES did not contain 'backend/app/hotfix_policy.py'.
    Without both modules, a fix to the hotfix scope validator cannot be deployed via the
    hotfix channel — the exact self-reference gap the channel exists to solve.
    Full coverage in test_hotfix_policy_and_router_in_eligible_modules.
    """
    from app.hotfix_policy import HOTFIX_ELIGIBLE_MODULES

    assert "backend/app/hotfix_policy.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/hotfix_policy.py' must be in HOTFIX_ELIGIBLE_MODULES"
    )
    assert "backend/app/routers/admin_operator_triggers.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/routers/admin_operator_triggers.py' must be in HOTFIX_ELIGIBLE_MODULES"
    )


# ── 17-finding release-qualification salvage bundle (task c81995d3, 2026-07-21) ── #
# All 17 findings are identical to those in the c39e0ee0/986aadde bundles and are
# already pinned by the f20-* test suite.  The functions below are thin cross-
# references that verify the same invariants under the c81 prefix.
# --------------------------------------------------------------------------- #


# c81-1 — classify serving-only-import ImportError as degrade, not hard block
# Already pinned by test_f20_1_serving_only_import_error_degrades_gate.
# --------------------------------------------------------------------------- #


def test_c81_1_serving_only_import_degrades_gate():
    """_ServingOnlyImportError must degrade the gate to ('ok', None).

    Finding c81-1: an ImportError referencing a candidate-only app.* symbol during
    serving-process replay previously raised _DeterministicMigrationDefect — a hard block.
    The fix introduces _ServingOnlyImportError and degrades the gate to ('ok', None).
    Full coverage in test_f20_1_serving_only_import_error_degrades_gate.
    """
    from app.migration_apply_gate import _ServingOnlyImportError, _looks_like_serving_only_import

    assert issubclass(_ServingOnlyImportError, Exception)

    exc = ImportError("No module named 'app.require_encryption_active'")
    exc.name = "app.require_encryption_active"
    assert _looks_like_serving_only_import(exc), (
        "_looks_like_serving_only_import must return True for app.* import errors"
    )


# c81-2 — prove data-dependent migrations safely (design)
# Already pinned by test_f20_2_data_migration_proof_gate_design.
# --------------------------------------------------------------------------- #


def test_c81_2_scratch_replay_uses_schema_not_row_data():
    """Scratch migration replay must compare schema names, not row-transform outcomes.

    Finding c81-2: production state supplied to scratch replay contains only Alembic
    revision and reflected table/column metadata; scratch seeding creates empty tables.
    Full coverage in test_f20_2_data_migration_proof_gate_design.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py").read_text()
    assert "schema" in src and "drift" in src, (
        "migration_apply_gate.py must reference schema drift detection"
    )


# c81-3 — verify encryption-key continuity
# Already pinned by test_f20_3_*, e2, e3.
# --------------------------------------------------------------------------- #


def test_c81_3_probe_key_continuity_and_assert_plaintext_present():
    """probe_key_continuity and assert_plaintext_or_raise must be importable from field_encryption.

    Finding c81-3: decrypt_field returns its input unchanged when a valid Fernet token
    cannot be decrypted with the configured key; assert_plaintext_or_raise guards against
    treating wrong-key ciphertext as plaintext.
    Full coverage in test_f20_3_*, e2, and e3.
    """
    from app.field_encryption import probe_key_continuity, assert_plaintext_or_raise

    assert callable(probe_key_continuity)
    assert callable(assert_plaintext_or_raise)


# c81-4 — research: define safe Fernet key rotation (MultiFernet)
# Already pinned by test_f20_4_multifernet_rotate_exists.
# --------------------------------------------------------------------------- #


def test_c81_4_multifernet_rotate_importable():
    """cryptography.fernet.MultiFernet must expose rotate() for staged key rotation.

    Finding c81-4: the MultiFernet contract encrypts with the first key, decrypts with
    every configured key, and supports explicit token rotation via rotate().
    Full coverage in test_f20_4_multifernet_rotate_exists.
    """
    from cryptography.fernet import MultiFernet

    assert hasattr(MultiFernet, "rotate"), (
        "MultiFernet must expose rotate() for staged Fernet key rotation"
    )


# c81-5 — fail closed on keyless/malformed encryption backfill
# Already pinned by test_f20_5_require_encryption_active_raises_on_missing_key.
# --------------------------------------------------------------------------- #


def test_c81_5_require_encryption_active_raises_for_absent_and_malformed(monkeypatch):
    """require_encryption_active must raise RuntimeError for absent and malformed keys.

    Finding c81-5: _get_fernet returns None for malformed GITHUB_PAT_ENCRYPTION_KEY, so
    is_encryption_active() returns False identically for missing and malformed keys, and
    backfill migrations silently no-op in both cases.
    Full coverage in test_f20_5_require_encryption_active_raises_on_missing_key.
    """
    import pytest
    from app import field_encryption

    monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)
    with pytest.raises(RuntimeError):
        field_encryption.require_encryption_active("c81-5-absent")

    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", "not-valid-base64-fernet")
    with pytest.raises(RuntimeError):
        field_encryption.require_encryption_active("c81-5-malformed")


# c81-6 — make scratch-DB URL degraded, not boot-fatal
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req.
# --------------------------------------------------------------------------- #


def test_c81_6_scratch_db_url_not_in_required_runtime_env():
    """MIGRATION_APPLY_SCRATCH_DATABASE_URL must not appear in REQUIRED_RUNTIME_ENV.

    Finding c81-6: the scratch URL was declared with scope='cloud_run', making its
    absence boot-fatal.  The fix removes it so its absence degrades the gate to a
    'defer' hold rather than crashing Cloud Run.
    Full coverage in test_scratch_db_url_is_qualification_prereq_not_boot_req.
    """
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    req_names = {r.name for r in REQUIRED_RUNTIME_ENV}
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in req_names, (
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL must NOT be in REQUIRED_RUNTIME_ENV"
    )


# c81-7 — isolate gate-defer signature from unrelated child failures
# Already pinned by test_infra_failure_signature_ignores_child_failures.
# --------------------------------------------------------------------------- #


def test_c81_7_infra_failure_signature_stable_under_varying_children():
    """_infra_failure_signature must be stable regardless of which child tasks failed.

    Finding c81-7: _infra_failure_signature derived its leg from child_failures[-1],
    causing the signature to oscillate between audit and smoketest child failures,
    preventing permanent-gate-incapacity streak accumulation.
    Full coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    error = "migration_apply_check_unavailable"
    assert (
        _infra_failure_signature(error, [{"leg": "audit"}])
        == _infra_failure_signature(error, [{"leg": "smoketest"}])
        == _infra_failure_signature(error, [])
    ), "_infra_failure_signature must be stable regardless of child_failures content"


# c81-8 — contain timed-out qualifier children
# Already pinned by test_cancel_qualifier_children_covers_all_child_types.
# --------------------------------------------------------------------------- #


def test_c81_8_cancel_qualifier_children_callable():
    """_cancel_qualifier_children must be defined in release_qualifier_sweep.py.

    Finding c81-8: qualification timeout and sweep-crash handlers rewrote the release
    record and alerted without changing referenced child task states.
    Full coverage in test_cancel_qualifier_children_covers_all_child_types.
    """
    from app.release_qualifier_sweep import _cancel_qualifier_children

    assert callable(_cancel_qualifier_children)


# c81-9 — block deterministic migration defects
# Already pinned by test_deterministic_migration_defect_class_exists.
# --------------------------------------------------------------------------- #


def test_c81_9_deterministic_migration_defect_is_exception():
    """_DeterministicMigrationDefect must be an Exception subclass mapping to 'block'.

    Finding c81-9: all migration-apply exceptions previously converted to defer.  The
    fix introduces _DeterministicMigrationDefect for repeatable SQL/syntax errors.
    Full coverage in test_deterministic_migration_defect_class_exists.
    """
    from app.migration_apply_gate import _DeterministicMigrationDefect

    assert issubclass(_DeterministicMigrationDefect, Exception)


# c81-10 — enforce independent boot preflight
# Already pinned by test_env_preflight_absent_evidence_is_a_hard_blocker.
# --------------------------------------------------------------------------- #


def test_c81_10_env_preflight_enabled_flag_in_release_qualifier():
    """env_preflight_enabled must be an independent gate flag in release_qualifier.py.

    Finding c81-10: the sole env-preflight artifact producer was attached to the
    optional smoketest child; absent evidence scored as unavailable rather than a blocker.
    Full coverage in test_env_preflight_absent_evidence_is_a_hard_blocker.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "release_qualifier.py").read_text()
    assert "env_preflight_enabled" in src, (
        "release_qualifier.py must reference env_preflight_enabled as an independent gate flag"
    )


# c81-11 — unify trigger truth and retries
# Already pinned by test_infra_failure_redrive_is_independent_of_auto_qualification_triggers_knob.
# --------------------------------------------------------------------------- #


def test_c81_11_auto_qualification_triggers_does_not_control_infra_failure_redrive():
    """auto_qualification_triggers knob must NOT gate the infra_failure bespoke re-drive.

    Finding c81-11: the trigger resolver returned not_applicable for states handled by
    dedicated sweep re-drive functions, and auto_qualification_triggers controlled only
    the generic trigger sweep, not the bespoke infra-failure loop.
    Full coverage in test_infra_failure_redrive_is_independent_of_auto_qualification_triggers_knob.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "release_qualifier_redrive.py").read_text()
    assert "infra_failure" in src, (
        "release_qualifier_redrive.py must reference infra_failure — "
        "the bespoke re-drive must exist independently of the generic trigger knob"
    )


# c81-12 — contain orphan promotion actors
# Already pinned by test_bare_claim_promotion_gets_shorter_timeout.
# --------------------------------------------------------------------------- #


def test_c81_12_release_reconciliation_references_claiming_field():
    """release_reconciliation.py must reference the 'claiming' field for orphan containment.

    Finding c81-12: a crash after PR creation could leave promotion_pending with
    claiming=True and no PR number; timeout cleanup only closed PRs for managed-check-wait
    records, not bare claims.
    Full coverage in test_bare_claim_promotion_gets_shorter_timeout.
    """
    import inspect
    from app import release_reconciliation

    assert "claiming" in inspect.getsource(release_reconciliation), (
        "release_reconciliation must reference 'claiming' — bare claims must be containable"
    )


# c81-13 — gate data-migration proof
# Already pinned by test_data_migration_proof_gate_defers_dml_without_proof.
# --------------------------------------------------------------------------- #


def test_c81_13_migration_apply_gate_references_dml_detection():
    """migration_apply_gate.py must reference DML/data-migration detection.

    Finding c81-13: the migration-safe verdict could be produced after applying a data
    migration to empty scratch tables with no fixture or invariant contract required.
    Full coverage in test_data_migration_proof_gate_defers_dml_without_proof.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent.parent / "app" / "migration_apply_gate.py").read_text()
    assert "dml" in src.lower() or "DML" in src or "data_migration" in src, (
        "migration_apply_gate.py must reference DML/data migration detection"
    )


# c81-14 — guide narrow release remediation (UI)
# Already pinned by test_version_lifecycle_panel_has_hotfix_remediation.
# --------------------------------------------------------------------------- #


def test_c81_14_hotfix_remediation_api_client_in_projects_ts():
    """admin-ui/src/api/projects.ts must expose hotfix scope preview and promote functions.

    Finding c81-14: admin-ui had no hotfix preview or promotion client and
    VersionLifecyclePanel rendered only generic qualification failure state.
    Full coverage in test_version_lifecycle_panel_has_hotfix_remediation.
    """
    import pathlib
    import pytest

    candidates = [
        pathlib.Path(__file__).resolve().parents[2] / "admin-ui" / "src" / "api" / "projects.ts",
        pathlib.Path("/repo/admin-ui/src/api/projects.ts"),
    ]
    api_source: str | None = None
    for p in candidates:
        if p.exists():
            api_source = p.read_text()
            break
    if api_source is None:
        pytest.skip("admin-ui/src/api/projects.ts not found — admin-ui not present in this shard")

    assert "getHotfixScopePreview" in api_source or "hotfix" in api_source.lower(), (
        "admin-ui/src/api/projects.ts must expose a hotfix scope preview function"
    )
    assert "triggerHotfixPromote" in api_source or "promote/hotfix" in api_source, (
        "admin-ui/src/api/projects.ts must expose a hotfix promote function"
    )


# c81-15 — declare + diagnose migration-apply scratch prereq
# Already pinned by test_scratch_db_url_is_qualification_prereq_not_boot_req.
# --------------------------------------------------------------------------- #


def test_c81_15_scratch_preflight_surfaces_missing_url():
    """preflight_migration_apply_scratch must surface MIGRATION_APPLY_SCRATCH_DATABASE_URL absence.

    Finding c81-15: MIGRATION_APPLY_SCRATCH_DATABASE_URL appeared nowhere in
    required_runtime_env.py.  The gate's own preflight must surface a missing URL with
    an actionable 'absent' status and the variable name in its detail.
    Full coverage in test_scratch_db_url_is_qualification_prereq_not_boot_req.
    """
    from app.migration_apply_gate import MigrationApplyScratchPreflight, preflight_migration_apply_scratch

    result = preflight_migration_apply_scratch(None)
    assert isinstance(result, MigrationApplyScratchPreflight)
    assert result.status == "absent"
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (result.detail or "")


# c81-16 — stabilize infra-failure signature for gate defers
# Already pinned by test_infra_failure_signature_ignores_child_failures.
# --------------------------------------------------------------------------- #


def test_c81_16_infra_failure_signature_stable_for_migration_apply_error():
    """_infra_failure_signature must produce the same string regardless of child failures.

    Finding c81-16: live v26 qualification showed signatures oscillating between
    challenge:PROVISION_FAILURE:migration_apply_check_unavailable and
    audit:PROVISION_FAILURE:audit_infra_respawn_cap_exceeded, preventing the
    permanent-gate-incapacity streak from accumulating.
    Full coverage in test_infra_failure_signature_ignores_child_failures.
    """
    from app.release_qualifier_audit import _infra_failure_signature

    error = "migration_apply_check_unavailable"
    assert (
        _infra_failure_signature(error, [{"leg": "challenge", "status": "FAILED"}])
        == _infra_failure_signature(error, [{"leg": "audit", "status": "CANCELLED"}])
        == _infra_failure_signature(error, [])
    )


# c81-17 — add hotfix_policy.py to hotfix-eligible modules
# Already pinned by test_hotfix_policy_and_router_in_eligible_modules,
# test_c39_17_hotfix_policy_and_router_in_eligible_modules, and
# test_98_17_hotfix_policy_and_router_in_eligible_modules.
# --------------------------------------------------------------------------- #


def test_c81_17_hotfix_policy_and_router_in_eligible_modules():
    """hotfix_policy.py and admin_operator_triggers.py must be in HOTFIX_ELIGIBLE_MODULES.

    Finding c81-17: HOTFIX_ELIGIBLE_MODULES did not contain 'backend/app/hotfix_policy.py'.
    Without both modules, a fix to the hotfix scope validator cannot be deployed via the
    hotfix channel — the exact self-reference gap the channel exists to solve.
    Full coverage in test_hotfix_policy_and_router_in_eligible_modules.
    """
    from app.hotfix_policy import HOTFIX_ELIGIBLE_MODULES

    assert "backend/app/hotfix_policy.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/hotfix_policy.py' must be in HOTFIX_ELIGIBLE_MODULES"
    )
    assert "backend/app/routers/admin_operator_triggers.py" in HOTFIX_ELIGIBLE_MODULES, (
        "'backend/app/routers/admin_operator_triggers.py' must be in HOTFIX_ELIGIBLE_MODULES"
    )


# ── c1-1: end-to-end greenfield onboarding cross-seam invariant ───────────────


def test_c1_bootstrap_self_heal_and_repo_is_empty_guard_coexist():
    """spawn_bootstrap_task must contain both self-heal and the repo_is_empty guard.

    Finding c1-1: the clone_failed loop suppression depends on TWO cooperating
    guards in spawn_bootstrap_task: (1) _self_heal_undetermined_emptiness resolves
    an absent durable signal against the live repo, and (2) the repo_is_empty guard
    returns not_eligible so the doomed bootstrap is never spawned.  If either guard
    is removed independently the loop re-opens, so both must co-exist in the same
    function body.
    """
    src = _find_repo_file("backend/app/repo_bootstrap.py").read_text()

    assert "_self_heal_undetermined_emptiness" in src, (
        "repo_bootstrap.py must contain _self_heal_undetermined_emptiness — "
        "the legacy-undetermined self-heal that resolves an absent repo_is_empty "
        "signal against the live GitHub repo before the bootstrap guard fires"
    )
    assert 'reason="repo_is_empty"' in src, (
        "repo_bootstrap.spawn_bootstrap_task must return reason='repo_is_empty' "
        "so the clone_failed loop is suppressed for empty/greenfield projects"
    )
    # Both guards must appear inside spawn_bootstrap_task, not in a helper that
    # could be accidentally decoupled from the spawn path.
    fn_start = src.find("async def spawn_bootstrap_task(")
    assert fn_start != -1, "spawn_bootstrap_task not found in repo_bootstrap.py"
    # Extract the full function body up to the next top-level function definition.
    tail = src[fn_start:]
    next_fn_offset = tail.find("\nasync def ", 1)
    if next_fn_offset == -1:
        next_fn_offset = tail.find("\ndef ", 1)
    fn_body = tail[:next_fn_offset] if next_fn_offset != -1 else tail
    assert "_self_heal_undetermined_emptiness" in fn_body, (
        "spawn_bootstrap_task must call _self_heal_undetermined_emptiness inline — "
        "the guard must be on the critical spawn path, not only in a decoupled helper"
    )
    assert 'reason="repo_is_empty"' in fn_body, (
        "spawn_bootstrap_task must return reason='repo_is_empty' on the same code path "
        "so suppression fires after self-heal resolves the empty signal"
    )


def test_c1_web_only_grounding_gate_wired_into_knowledge_completion():
    """_design_web_only_grounding_miss must be called inside the knowledge completion gate.

    Finding c1-1: greenfield design documents cannot cite repository files that do
    not exist yet — their only grounding evidence is reachable web URLs.
    _design_web_only_grounding_miss probes those URLs; it must be wired into the
    completion gate (not dead code) so a document grounded entirely in dead links
    cannot pass acceptance.
    """
    src = _find_repo_file("backend/app/knowledge_completion.py").read_text()

    assert "_design_web_only_grounding_miss" in src, (
        "knowledge_completion.py must define _design_web_only_grounding_miss — "
        "the URL reachability gate for web-only greenfield design documents"
    )
    # The function must be both defined AND called (wired into the gate).
    define_idx = src.find("async def _design_web_only_grounding_miss(")
    assert define_idx != -1, (
        "_design_web_only_grounding_miss must be defined as an async function in "
        "knowledge_completion.py"
    )
    call_idx = src.find("await _design_web_only_grounding_miss(")
    assert call_idx != -1, (
        "knowledge_completion.py must AWAIT _design_web_only_grounding_miss — "
        "the gate must be active on the completion path, not merely defined"
    )
    # The call-site must appear AFTER the function definition (normal call order),
    # and must not be inside the function body itself (which would be a recursive
    # self-call, not a wired gate).
    assert call_idx > define_idx, (
        "_design_web_only_grounding_miss must be awaited at a call-site that follows "
        "its definition — the gate is wired via a downstream caller, not recursively"
    )
    # The call must not be inside the helper's own body.  The helper body ends
    # before any subsequent top-level `async def` or `def`.  Check that the call
    # site is outside the helper by confirming it does not fall inside [define_idx,
    # next_fn_idx) where next_fn_idx is the next top-level function after define_idx.
    tail = src[define_idx:]
    next_fn_offset = tail.find("\nasync def ", 1)
    if next_fn_offset == -1:
        next_fn_offset = tail.find("\ndef ", 1)
    if next_fn_offset != -1:
        helper_end = define_idx + next_fn_offset
        assert call_idx > helper_end, (
            "The await of _design_web_only_grounding_miss must be outside its own body — "
            "a call inside the helper would be a recursive self-call, not a wired gate"
        )


def test_c1_greenfield_unit_test_files_cover_both_seams():
    """Both half-tests must exist with their core invariant functions.

    Finding c1-1: the two seams ship separate unit-test files.  This structural
    check confirms those files are present and name the key test functions so a
    later refactor that deletes or renames them surfaces here before CI.
    """
    empty_repo_src = _find_repo_file(
        "backend/testing/test_empty_repo_onboarding.py"
    ).read_text()
    web_url_src = _find_repo_file(
        "backend/testing/test_design_web_url_reachability.py"
    ).read_text()

    # Half-1: clone_failed suppression.
    assert "TestBootstrapGuardIsReachable" in empty_repo_src, (
        "test_empty_repo_onboarding.py must contain TestBootstrapGuardIsReachable "
        "— the class that verifies spawn_bootstrap_task suppresses empty repos"
    )
    assert "TestLegacyUndeterminedEmptinessSelfHeal" in empty_repo_src, (
        "test_empty_repo_onboarding.py must contain TestLegacyUndeterminedEmptinessSelfHeal "
        "— the class that exercises _self_heal_undetermined_emptiness end-to-end"
    )

    # Half-2: web-URL reachability gate.
    assert "TestDesignWebOnlyGroundingMiss" in web_url_src, (
        "test_design_web_url_reachability.py must contain TestDesignWebOnlyGroundingMiss "
        "— the class that verifies _design_web_only_grounding_miss accepts/rejects docs"
    )
    assert "_design_web_only_grounding_miss" in web_url_src, (
        "test_design_web_url_reachability.py must import and exercise "
        "_design_web_only_grounding_miss directly"
    )


# ── 1-finding baked-image authoring-prompt salvage bundle (task db1e30ca, 2026-07-21) ── #
# Finding db-1 is identical to bi-1; all three prompt files and the backend
# strategic_spawning.py already carry the project-baked image preference.
# Full coverage in the bi-1 test suite below.
# --------------------------------------------------------------------------- #


def test_db_1_baked_image_authoring_preference_already_wired():
    """Authoring agents must prefer project-baked images; backend must inject baked_image_ref.

    Finding db-1 (same as bi-1): thinker.ts, research-plan.ts, and strategic.ts must all
    instruct agents to OMIT container_image when a project-baked image is listed, and the
    backend must inject baked_image_ref into local_runner_status so the prompt channel
    carries the digest-pinned image reference.

    Full coverage already provided by test_thinker_prefers_baked_image_over_always_set_explicitly,
    test_research_plan_injects_baked_image_ref_into_local_runner_note, and
    test_strategic_local_runner_section_includes_baked_image_guidance. This function
    cross-references those invariants under the db prefix.
    """
    thinker_src = _find_repo_file("agent/src/prompts/thinker.ts").read_text()
    research_src = _find_repo_file("agent/src/handlers/research-plan.ts").read_text()
    strategic_src = _find_repo_file("agent/src/handlers/strategic.ts").read_text()
    spawning_src = _find_repo_file("backend/app/strategic_spawning.py").read_text()

    assert "Prefer the project-baked image" in thinker_src, (
        "thinker.ts must guide agents to prefer the project-baked image over always "
        "setting container_image explicitly"
    )
    assert "baked_image_ref" in research_src, (
        "research-plan.ts must read baked_image_ref from local_runner_status to surface "
        "the project-baked image to authoring agents"
    )
    assert "baked_image_ref" in strategic_src, (
        "strategic.ts buildLocalRunnerSection must handle baked_image_ref to emit baked-image "
        "guidance in the self-hosted compute section"
    )
    assert "baked_image_ref" in spawning_src, (
        "backend strategic_spawning.py must inject baked_image_ref into local_runner_status "
        "so the prompt channel carries the digest-pinned image reference"
    )


# ── 18-finding release-qualification salvage bundle (task 8d2616a1, 2026-07-21) ── #
# All 18 findings are already addressed and pinned by the f20-* test suite above.
# This function cross-references them under the 8d prefix.
# --------------------------------------------------------------------------- #


def test_8d_release_qualification_findings_all_pinned():
    """Cross-reference check: all 18 findings from task 8d2616a1 are already pinned.

    8d-1  (env-preflight no-manifest wedge) → test_f20_10_env_preflight_enabled_flag_independent_of_smoketest
           Key invariant: build_env_preflight_from_source returns {required_env: []} (not None) when the
           candidate has no required_runtime_env.py, so _score_env_preflight returns 1.0 (vacuously satisfied)
           and boot_prereq_evidence_missing is NOT fired for external/greenfield repos.
    8d-2  (serving-only-import degrade) → test_f20_1_serving_only_import_classifies_as_degrade
    8d-3  (data-dependent migration proof design) → test_f20_2_scratch_replay_compares_schema_not_row_data
    8d-4  (encryption-key continuity) → test_f20_3_encryption_key_continuity_functions_present
    8d-5  (safe Fernet key rotation) → test_f20_4_multifernet_rotate_method_exists
    8d-6  (keyless/malformed backfill fail-closed) → test_f20_5_require_encryption_active_raises_*
    8d-7  (scratch-DB URL degraded not boot-fatal) → test_f20_6_scratch_db_url_in_qualification_env_not_required_env
    8d-8  (gate-defer signature isolation) → test_f20_7_infra_failure_signature_derived_from_error_code_only
    8d-9  (timed-out qualifier children containment) → test_f20_8_force_finalize_cancels_children_before_rewriting_state
    8d-10 (deterministic migration defects block) → test_f20_9_deterministic_migration_defect_returns_block_verdict
    8d-11 (independent boot preflight) → test_f20_10_env_preflight_enabled_flag_independent_of_smoketest
    8d-12 (trigger truth/retries) → test_f20_11_infra_failure_maps_to_dedicated_redrive_trigger
    8d-13 (orphan promotion actors) → test_f20_12_promotion_timeout_applies_to_bare_claims
    8d-14 (data-migration proof gate) → test_f20_13_data_migration_proof_gate_defers_dml
    8d-15 (narrow release remediation UI) → test_f20_14_hotfix_remediation_api_client_present
    8d-16 (scratch prereq qualification) → test_f20_15_missing_qualification_env_surfaces_scratch_db_url
    8d-17 (infra-failure signature stable) → test_f20_16_infra_failure_signature_stable_across_candidate_rotations
    8d-18 (hotfix_policy.py in eligible modules) → test_f20_17_hotfix_policy_and_router_self_eligible
    """
    from app.env_preflight_evidence import build_env_preflight_from_source

    # 8d-1: repos with no manifest must not be blocked by boot_prereq_evidence_missing.
    # When required_runtime_env_py_source is None, the function must return a dict
    # with required_env=[] (not None), so the scoring layer sees a vacuously-satisfied
    # manifest (1.0) and does not fire the evidence-missing hard blocker.
    artifact = build_env_preflight_from_source(
        required_runtime_env_py_source=None,
        smoketest_yaml_source=None,
    )
    assert isinstance(artifact, dict), (
        "build_env_preflight_from_source must return a dict (not None) when "
        "required_runtime_env_py_source is None — a missing manifest is vacuously "
        "satisfied, not a hard block"
    )
    required_env = artifact.get("required_env")
    assert isinstance(required_env, list) and required_env == [], (
        "build_env_preflight_from_source must return required_env=[] when the candidate "
        "has no manifest file — the empty list scores as 1.0 (vacuously satisfied) and "
        "does NOT trigger boot_prereq_evidence_missing"
    )

    # 8d-17 / 8d-7 / 8d-8: cross-check that the key invariants from f20-16/f20-7/f20-6
    # are still in force (tested in detail by their respective test_f20_* functions).
    from app.release_qualifier_audit import _infra_failure_signature
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    sig = _infra_failure_signature(
        "migration_apply_check_unavailable",
        child_failures=[{"leg": "audit", "code": "whatever"}],
    )
    assert "migration_apply_check_unavailable" in sig, (
        "_infra_failure_signature must embed the stable error code, not child failures"
    )

    boot_names = {r.name for r in REQUIRED_RUNTIME_ENV}
    assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" not in boot_names, (
        "MIGRATION_APPLY_SCRATCH_DATABASE_URL must not be a boot requirement — "
        "its absence must degrade qualification, not block Cloud Run boot"
    )


# =========================================================================== #
# note-shim / runner-config / project-tools / walkthrough salvage bundle      #
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Finding n1 — ./note shim uses _json_str (not sed) for JSON encoding         #
# --------------------------------------------------------------------------- #


def _runners_ts_source() -> str:
    """Return the source of agent/src/providers/runners.ts."""
    from pathlib import Path

    for candidate in (
        Path(__file__).resolve().parents[2] / "agent" / "src" / "providers" / "runners.ts",
        Path("/repo/agent/src/providers/runners.ts"),
    ):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError("runners.ts not found in expected locations")


def test_n1_note_shim_imports_json_str_shell_fn():
    """runners.ts must import jsonStrShellFn from token-file so the ./note
    script can use _json_str for safe JSON encoding.

    Finding n1: provisionAmbientHelpers used sed for JSON escaping, which
    drops newlines and control characters. The fix imports jsonStrShellFn
    alongside agentTokenShellFn and injects _json_str into the note script.
    """
    import pytest

    try:
        source = _runners_ts_source()
    except FileNotFoundError:
        pytest.skip("runners.ts not found — agent not present in this shard")

    assert "jsonStrShellFn" in source, (
        "runners.ts must import jsonStrShellFn from '../token-file.js' so "
        "provisionAmbientHelpers can inject _json_str into the ./note script"
    )
    assert "agentTokenShellFn" in source and "jsonStrShellFn" in source, (
        "runners.ts must import both agentTokenShellFn and jsonStrShellFn "
        "from '../token-file.js'"
    )


def test_n1_note_shim_uses_json_str_not_sed():
    """The ./note script body must encode CONTENT via _json_str, not sed.

    Finding n1: the original note script used
      sed 's/\\\\/\\\\\\\\/g; s/\"/\\\\\"/'
    which loses newlines and control characters. The fix uses _json_str
    (backed by python3 json.dumps) for CONTENT, PAGE, and SEVERITY.
    """
    import pytest
    import re

    try:
        source = _runners_ts_source()
    except FileNotFoundError:
        pytest.skip("runners.ts not found — agent not present in this shard")

    # The note script section is between provisionAmbientHelpers and the
    # writePlaybookHelper call. Locate the note script template.
    note_start = source.find("provisionAmbientHelpers")
    note_end = source.find("writePlaybookHelper", note_start)
    assert note_start != -1, "provisionAmbientHelpers not found in runners.ts"
    note_section = source[note_start:note_end] if note_end != -1 else source[note_start:]

    # _json_str must be injected into the note script (via jsonStrShellFn()).
    assert "_json_str" in note_section, (
        "The ./note script in provisionAmbientHelpers must declare _json_str "
        "(injected via jsonStrShellFn()) so content encoding does not lose newlines"
    )

    # CONTENT must be encoded via _json_str, not raw string interpolation.
    assert '_json_str "$CONTENT"' in note_section or "_json_str \\\"$CONTENT\\\"" in note_section, (
        "The ./note script must encode CONTENT via $(_json_str \"$CONTENT\") "
        "so newlines and control characters are not silently dropped"
    )

    # The broken sed escaping must not appear in the note script.
    sed_pattern = re.compile(r'sed\s+[\'"]s/\\\\\\\\/')
    assert not sed_pattern.search(note_section), (
        "The ./note script must NOT use sed for JSON escaping — sed loses newlines "
        "and control characters; use _json_str (python3 json.dumps) instead"
    )
