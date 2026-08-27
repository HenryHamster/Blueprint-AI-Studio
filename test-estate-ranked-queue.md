# Test Estate Ranked Queue

> GENERATED FILE. Do not hand-edit mutable estate facts.
> Regenerate with `python3 scripts/generate_test_estate_ranked_queue.py --write`;
> validate with `python3 scripts/generate_test_estate_ranked_queue.py --check`.
> Estate totals are not committed here — they aggregate across every file,
> so every branch would rewrite them. Print them with
> `python3 scripts/generate_test_estate_ranked_queue.py --summary`.
> On Windows the interpreter is `python`: the python.org installer ships
> no `python3`, so the commands above cannot run there as written.
> Policy and threshold definitions: `test-estate-rubric.md`.

Rows are in rank order, highest priority first; there is deliberately no rank ordinal, because an ordinal renumbers every row below an insertion and made this report conflict on 40% of merges. Every row is a function of its own file alone, so concurrent branches insert disjoint lines and merge cleanly.

The queue includes every currently collected backend, admin-ui unit, admin-ui viewport, and agent test file. Backend duration coverage is partial: an `unknown` cost is intentionally not treated as zero or ranked as cheap. Admin-ui and agent costs are complete directional signals from their committed artifacts. Protected Workday counts are copied verbatim from the manifest for reporting only; this generator never edits that manifest.

## Ranked queue

| Priority | Area | File | LOC | Tests | Threshold class | Cost signal | Workday protection |
|:--------:|------|------|----:|------:|------------------|-------------|---------------------|
| P2 | backend | `backend/testing/test_strategic_completion.py` | 22,105 | 631 | critical (>5000 LOC) | 603.712s measured | — |
| P2 | backend | `backend/testing/test_release_qualifier__compute_composite.py` | 5,968 | 264 | critical (>5000 LOC) | 400.868s measured | — |
| P2 | backend | `backend/testing/test_pipeline.py` | 6,841 | 277 | critical (>5000 LOC) | 318.896s measured | — |
| P2 | backend | `backend/testing/test_review_pipeline.py` | 20,894 | 437 | critical (>5000 LOC) | 300.644s measured | — |
| P2 | backend | `backend/testing/test_task_lifecycle.py` | 13,145 | 323 | critical (>5000 LOC) | 207.992s measured | — |
| P2 | backend | `backend/testing/test_reaper.py` | 21,855 | 502 | critical (>5000 LOC) | 154.118s measured | protected; declared floor 502 |
| P2 | backend | `backend/testing/test_research_completion.py` | 24,174 | 534 | critical (>5000 LOC) | 137.469s measured | — |
| P2 | backend | `backend/testing/test_autonomous_research.py` | 20,308 | 393 | critical (>5000 LOC) | 124.505s measured | — |
| P2 | backend | `backend/testing/test_invariants.py` | 15,823 | 279 | critical (>5000 LOC) | 120.389s measured | — |
| P2 | backend | `backend/testing/test_local_runner.py` | 14,047 | 348 | critical (>5000 LOC) | 111.153s measured | — |
| P2 | backend | `backend/testing/test_digest.py` | 11,003 | 231 | critical (>5000 LOC) | 86.555s measured | — |
| P2 | backend | `backend/testing/test_directions.py` | 11,809 | 339 | critical (>5000 LOC) | 79.334s measured | — |
| P2 | backend | `backend/testing/test_strategic_resident_session.py` | 5,475 | 85 | critical (>5000 LOC) | 77.770s measured | protected; declared floor 85 |
| P2 | backend | `backend/testing/test_acceptance_criteria.py` | 13,369 | 283 | critical (>5000 LOC) | 74.580s measured | — |
| P2 | backend | `backend/testing/test_release_qualifier__ci_unresolved_blocker.py` | 6,523 | 163 | critical (>5000 LOC) | 67.628s measured | — |
| P2 | backend | `backend/testing/test_agent.py` | 29,484 | 515 | critical (>5000 LOC) | 65.904s measured | — |
| P2 | backend | `backend/testing/test_metrics_integrity_sweep.py` | 8,161 | 216 | critical (>5000 LOC) | 65.620s measured | — |
| P2 | backend | `backend/testing/test_admin_routers__version_lifecycle.py` | 5,122 | 104 | critical (>5000 LOC) | 58.078s measured | — |
| P2 | backend | `backend/testing/test_compute.py` | 9,365 | 192 | critical (>5000 LOC) | 57.415s measured | — |
| P2 | backend | `backend/testing/test_orchestrator.py` | 18,818 | 362 | critical (>5000 LOC) | 57.238s measured | — |
| P2 | backend | `backend/testing/test_compute_experiment_completion.py` | 7,235 | 134 | critical (>5000 LOC) | 55.582s measured | — |
| P2 | backend | `backend/testing/test_knowledge_documents.py` | 6,801 | 164 | critical (>5000 LOC) | 48.488s measured | — |
| P2 | backend | `backend/testing/test_release_reconciliation.py` | 8,826 | 150 | critical (>5000 LOC) | 43.934s measured | — |
| P2 | backend | `backend/testing/test_compute_image_build_local.py` | 8,190 | 228 | critical (>5000 LOC) | 42.722s measured | — |
| P2 | backend | `backend/testing/test_efficiency_snapshot.py` | 12,745 | 314 | critical (>5000 LOC) | 39.993s measured | — |
| P2 | backend | `backend/testing/test_playbook_sr_metrics.py` | 8,196 | 148 | critical (>5000 LOC) | 37.850s measured | — |
| P2 | backend | `backend/testing/test_ci_completion.py` | 9,226 | 151 | critical (>5000 LOC) | 37.248s measured | — |
| P2 | backend | `backend/testing/test_admin_routers.py` | 18,236 | 455 | critical (>5000 LOC) | 34.420s measured | protected; declared floor 455 |
| P2 | backend | `backend/testing/test_evaluating.py` | 6,414 | 97 | critical (>5000 LOC) | 33.035s measured | — |
| P2 | backend | `backend/testing/test_release_qualifier__classify_check_runs.py` | 7,531 | 134 | critical (>5000 LOC) | 30.084s measured | — |
| P2 | backend | `backend/testing/test_rollup_management.py` | 6,226 | 106 | critical (>5000 LOC) | 26.818s measured | — |
| P2 | backend | `backend/testing/test_direction_outcomes.py` | 10,397 | 191 | critical (>5000 LOC) | 26.802s measured | — |
| P2 | backend | `backend/testing/test_github.py` | 5,931 | 187 | critical (>5000 LOC) | 17.135s measured | — |
| P2 | backend | `backend/testing/test_direction_lifecycle.py` | 5,886 | 103 | critical (>5000 LOC) | 13.910s measured | — |
| P2 | agent | `agent/src/handlers/strategic-resident.test.ts` | 9,165 | 135 | critical (>5000 LOC) | 13.729s measured | protected; declared floor 135 |
| P2 | backend | `backend/testing/test_salvage_bundle_standalone.py` | 7,775 | 239 | critical (>5000 LOC) | 8.485s measured | — |
| P2 | backend | `backend/testing/test_digest_metrics.py` | 8,216 | 251 | critical (>5000 LOC) | 8.434s measured | — |
| P2 | backend | `backend/testing/test_agent_strategic_fetch.py` | 7,132 | 140 | critical (>5000 LOC) | 7.724s measured | protected; declared floor 140 |
| P2 | backend | `backend/testing/test_release_qualifier__finalize_qualification_2.py` | 7,021 | 112 | critical (>5000 LOC) | 5.220s measured | — |
| P2 | backend | `backend/testing/test_strategic_state.py` | 10,654 | 174 | critical (>5000 LOC) | 4.542s measured | protected; declared floor 174 |
| P2 | backend | `backend/testing/test_release_qualifier__sweep_stuck_qualifications.py` | 7,146 | 145 | critical (>5000 LOC) | 3.776s measured | — |
| P2 | backend | `backend/testing/test_release_qualifier__finalize_qualification.py` | 7,663 | 106 | critical (>5000 LOC) | 3.708s measured | — |
| P2 | backend | `backend/testing/test_warm_pool_maintainer.py` | 5,921 | 123 | critical (>5000 LOC) | 3.564s measured | — |
| P2 | admin-ui | `admin-ui/src/components/VersionLifecyclePanel.test.tsx` | 5,098 | 177 | critical (>5000 LOC) | 2.891s measured | — |
| P2 | agent | `agent/src/handlers/research-plan.test.ts` | 5,390 | 396 | critical (>5000 LOC) | 2.122s measured | — |
| P2 | backend | `backend/testing/test_acceptance_gate.py` | 5,813 | 133 | critical (>5000 LOC) | 1.790s measured | — |
| P2 | backend | `backend/testing/test_max_plan_pool.py` | 6,364 | 359 | critical (>5000 LOC) | 1.344s measured | — |
| P2 | backend | `backend/testing/test_capability_trial_verdict.py` | 5,371 | 286 | critical (>5000 LOC) | 0.804s measured | — |
| P2 | backend | `backend/testing/test_problem_model.py` | 6,802 | 304 | critical (>5000 LOC) | 0.769s measured | — |
| P2 | backend | `backend/testing/test_capability_proof.py` | 6,622 | 247 | critical (>5000 LOC) | 0.548s measured | — |
| P3 | backend | `backend/testing/test_strategic_analysis.py` | 4,050 | 163 | high (3000–5000 LOC) | 329.000s measured | — |
| P3 | backend | `backend/testing/test_release_version.py` | 4,041 | 156 | high (3000–5000 LOC) | 148.400s measured | — |
| P3 | backend | `backend/testing/test_task_completion.py` | 4,674 | 181 | high (3000–5000 LOC) | 116.632s measured | — |
| P3 | backend | `backend/testing/test_release_qualifier__site_b_lock_window_fix.py` | 3,656 | 108 | high (3000–5000 LOC) | 104.664s measured | — |
| P3 | backend | `backend/testing/test_pipeline_health.py` | 4,048 | 120 | high (3000–5000 LOC) | 89.848s measured | — |
| P3 | backend | `backend/testing/test_rollup_cleanup.py` | 4,300 | 75 | high (3000–5000 LOC) | 82.971s measured | — |
| P3 | backend | `backend/testing/test_admin_analytics.py` | 4,247 | 98 | high (3000–5000 LOC) | 49.510s measured | — |
| P3 | backend | `backend/testing/test_plan_completion_atomic.py` | 3,311 | 55 | high (3000–5000 LOC) | 48.729s measured | — |
| P3 | backend | `backend/testing/test_pipeline_integration.py` | 3,851 | 35 | high (3000–5000 LOC) | 47.777s measured | — |
| P3 | backend | `backend/testing/test_compute_runner_dispatch.py` | 3,269 | 68 | high (3000–5000 LOC) | 42.232s measured | — |
| P3 | backend | `backend/testing/test_background.py` | 4,222 | 87 | high (3000–5000 LOC) | 38.844s measured | — |
| P3 | backend | `backend/testing/test_services.py` | 4,343 | 105 | high (3000–5000 LOC) | 32.037s measured | — |
| P3 | backend | `backend/testing/test_admin_routers__projects.py` | 3,609 | 132 | high (3000–5000 LOC) | 31.726s measured | — |
| P3 | backend | `backend/testing/test_admin_compute_jobs.py` | 3,312 | 79 | high (3000–5000 LOC) | 29.428s measured | — |
| P3 | backend | `backend/testing/test_project_tools.py` | 3,447 | 108 | high (3000–5000 LOC) | 27.728s measured | — |
| P3 | backend | `backend/testing/test_digest_sr.py` | 3,895 | 74 | high (3000–5000 LOC) | 27.494s measured | — |
| P3 | backend | `backend/testing/test_improvement_lane.py` | 3,811 | 100 | high (3000–5000 LOC) | 26.526s measured | — |
| P3 | backend | `backend/testing/test_compute_experiment_spawn.py` | 4,555 | 151 | high (3000–5000 LOC) | 26.515s measured | — |
| P3 | backend | `backend/testing/test_digest_direction_progress.py` | 3,284 | 61 | high (3000–5000 LOC) | 25.744s measured | — |
| P3 | backend | `backend/testing/test_reaper_sweeps_compute_image.py` | 3,046 | 77 | high (3000–5000 LOC) | 25.598s measured | — |
| P3 | backend | `backend/testing/test_pipeline_reliability.py` | 4,259 | 54 | high (3000–5000 LOC) | 23.104s measured | — |
| P3 | backend | `backend/testing/test_message_audience.py` | 3,605 | 136 | high (3000–5000 LOC) | 22.745s measured | — |
| P3 | backend | `backend/testing/test_no_ci_merge_gate_e2e.py` | 3,800 | 59 | high (3000–5000 LOC) | 22.715s measured | — |
| P3 | backend | `backend/testing/test_failure_classification.py` | 4,481 | 395 | high (3000–5000 LOC) | 21.006s measured | — |
| P3 | backend | `backend/testing/test_hotfix_backmerge.py` | 4,062 | 85 | high (3000–5000 LOC) | 18.743s measured | — |
| P3 | backend | `backend/testing/test_notification_dispatch.py` | 3,458 | 121 | high (3000–5000 LOC) | 15.923s measured | — |
| P3 | admin-ui | `admin-ui/src/components/ComputeJobsPanel.test.tsx` | 4,912 | 219 | high (3000–5000 LOC) | 11.886s measured | — |
| P3 | backend | `backend/testing/test_onboard.py` | 4,672 | 142 | high (3000–5000 LOC) | 10.811s measured | — |
| P3 | backend | `backend/testing/test_blocked_need_resolution.py` | 4,481 | 138 | high (3000–5000 LOC) | 8.477s measured | — |
| P3 | backend | `backend/testing/test_pack_under_budget.py` | 3,236 | 67 | high (3000–5000 LOC) | 6.628s measured | — |
| P3 | agent | `agent/src/handlers/review-fix.test.ts` | 4,604 | 242 | high (3000–5000 LOC) | 4.556s measured | — |
| P3 | backend | `backend/testing/test_admin_routers__messages.py` | 4,964 | 141 | high (3000–5000 LOC) | 3.650s measured | protected; declared floor 141 |
| P3 | backend | `backend/testing/test_ci_compliance_gate.py` | 3,019 | 55 | high (3000–5000 LOC) | 1.847s measured | — |
| P3 | agent | `agent/src/index.watchdog.test.ts` | 3,507 | 113 | high (3000–5000 LOC) | 1.616s measured | — |
| P3 | backend | `backend/testing/test_alert_dispatch.py` | 3,397 | 83 | high (3000–5000 LOC) | 1.590s measured | — |
| P3 | backend | `backend/testing/test_repo_bootstrap.py` | 3,989 | 81 | high (3000–5000 LOC) | 0.380s measured | — |
| P3 | agent | `agent/src/handlers/standard.test.ts` | 3,316 | 214 | high (3000–5000 LOC) | 0.281s measured | — |
| P3 | agent | `agent/src/handlers/strategic-resident-lib.test.ts` | 4,322 | 310 | high (3000–5000 LOC) | 0.104s measured | protected; declared floor 310 |
| P3 | agent | `agent/src/utils/test-runners.test.ts` | 3,880 | 368 | high (3000–5000 LOC) | 0.084s measured | — |
| P5 | backend | `backend/testing/test_release_scoring.py` | 2,266 | 109 | moderate (1500–2999 LOC) | 172.678s measured | — |
| P5 | backend | `backend/testing/test_anomaly_actions.py` | 1,525 | 75 | moderate (1500–2999 LOC) | 136.702s measured | — |
| P5 | backend | `backend/testing/test_review_learning.py` | 2,804 | 85 | moderate (1500–2999 LOC) | 135.006s measured | — |
| P5 | backend | `backend/testing/test_strategic_spawning_capability_verification.py` | 2,575 | 76 | moderate (1500–2999 LOC) | 105.758s measured | — |
| P5 | backend | `backend/testing/test_qualification_state_resolver_dimensions.py` | 2,300 | 71 | moderate (1500–2999 LOC) | 86.090s measured | — |
| P5 | backend | `backend/testing/test_strategic_spawning_summaries.py` | 2,357 | 54 | moderate (1500–2999 LOC) | 83.890s measured | — |
| P5 | backend | `backend/testing/test_strategic_spawning_portfolio_messages.py` | 2,612 | 75 | moderate (1500–2999 LOC) | 66.628s measured | — |
| P5 | backend | `backend/testing/test_strategic_context_package.py` | 1,920 | 47 | moderate (1500–2999 LOC) | 56.956s measured | — |
| P5 | backend | `backend/testing/test_strategic_session_messages.py` | 2,949 | 42 | moderate (1500–2999 LOC) | 49.513s measured | protected; declared floor 42 |
| P5 | backend | `backend/testing/test_strategic_spawning_capability_prediction.py` | 2,607 | 72 | moderate (1500–2999 LOC) | 49.466s measured | — |
| P5 | backend | `backend/testing/test_admin_project_config.py` | 1,906 | 78 | moderate (1500–2999 LOC) | 45.335s measured | — |
| P5 | agent | `agent/src/handlers/strategic-helpers.test.ts` | 2,361 | 118 | moderate (1500–2999 LOC) | 39.534s measured | protected; declared floor 118 |
| P5 | backend | `backend/testing/test_admin_pool_accounts.py` | 2,864 | 52 | moderate (1500–2999 LOC) | 37.238s measured | — |
| P5 | backend | `backend/testing/test_admin_approval_authority.py` | 1,510 | 16 | moderate (1500–2999 LOC) | 36.201s measured | — |
| P5 | backend | `backend/testing/test_admin_runners.py` | 1,826 | 62 | moderate (1500–2999 LOC) | 34.994s measured | — |
| P5 | backend | `backend/testing/test_alerts.py` | 2,287 | 60 | moderate (1500–2999 LOC) | 33.602s measured | — |
| P5 | backend | `backend/testing/test_strategic_cycle.py` | 1,602 | 65 | moderate (1500–2999 LOC) | 31.970s measured | — |
| P5 | agent | `agent/src/prompts/check-helper.test.ts` | 2,350 | 69 | moderate (1500–2999 LOC) | 30.009s measured | — |
| P5 | backend | `backend/testing/test_staged_directions.py` | 1,694 | 40 | moderate (1500–2999 LOC) | 29.493s measured | — |
| P5 | agent | `agent/src/prompt.guidance.test.ts` | 2,153 | 176 | moderate (1500–2999 LOC) | 28.990s measured | — |
| P5 | backend | `backend/testing/test_api.py` | 1,587 | 39 | moderate (1500–2999 LOC) | 26.354s measured | — |
| P5 | backend | `backend/testing/test_warm_pool_dispatch.py` | 1,961 | 40 | moderate (1500–2999 LOC) | 24.592s measured | — |
| P5 | backend | `backend/testing/test_generated_shell.py` | 2,722 | 111 | moderate (1500–2999 LOC) | 23.466s measured | — |
| P5 | backend | `backend/testing/test_admin_routers__notifications.py` | 1,595 | 54 | moderate (1500–2999 LOC) | 23.459s measured | — |
| P5 | backend | `backend/testing/test_review_merge_authority.py` | 1,540 | 18 | moderate (1500–2999 LOC) | 23.223s measured | — |
| P5 | backend | `backend/testing/test_discovery_review.py` | 2,938 | 61 | moderate (1500–2999 LOC) | 21.216s measured | — |
| P5 | backend | `backend/testing/test_admin_routers__strategic_directions.py` | 2,071 | 29 | moderate (1500–2999 LOC) | 20.878s measured | protected; declared floor 29 |
| P5 | backend | `backend/testing/test_audit_finding_class_guards.py` | 2,068 | 35 | moderate (1500–2999 LOC) | 20.635s measured | — |
| P5 | backend | `backend/testing/test_approved_pr_stranding.py` | 1,687 | 34 | moderate (1500–2999 LOC) | 20.351s measured | — |
| P5 | backend | `backend/testing/test_release_commit_authorship.py` | 2,412 | 53 | moderate (1500–2999 LOC) | 18.205s measured | — |
| P5 | backend | `backend/testing/test_deploy_health.py` | 2,237 | 81 | moderate (1500–2999 LOC) | 17.586s measured | — |
| P5 | backend | `backend/testing/test_migration_apply_gate.py` | 2,900 | 86 | moderate (1500–2999 LOC) | 17.539s measured | — |
| P5 | backend | `backend/testing/test_compute_park_wake.py` | 1,721 | 12 | moderate (1500–2999 LOC) | 17.022s measured | — |
| P5 | backend | `backend/testing/test_strategic_spawning_scope_budget.py` | 2,578 | 41 | moderate (1500–2999 LOC) | 14.284s measured | — |
| P5 | admin-ui | `admin-ui/src/components/MessagesPanel.test.tsx` | 2,818 | 78 | moderate (1500–2999 LOC) | 14.213s measured | protected; declared floor 78 |
| P5 | agent | `agent/src/prompts/code-change-helpers.test.ts` | 1,900 | 90 | moderate (1500–2999 LOC) | 13.227s measured | — |
| P5 | backend | `backend/testing/test_warm_pool.py` | 1,591 | 45 | moderate (1500–2999 LOC) | 13.002s measured | — |
| P5 | backend | `backend/testing/test_workday_resident_invariants.py` | 1,646 | 38 | moderate (1500–2999 LOC) | 12.536s measured | protected; declared floor 38 |
| P5 | backend | `backend/testing/test_lock_window.py` | 2,373 | 36 | moderate (1500–2999 LOC) | 11.653s measured | — |
| P5 | backend | `backend/testing/test_task_locking.py` | 2,456 | 62 | moderate (1500–2999 LOC) | 11.250s measured | — |
| P5 | admin-ui | `admin-ui/src/components/MessagesPanel.send.test.tsx` | 1,908 | 38 | moderate (1500–2999 LOC) | 11.185s measured | — |
| P5 | backend | `backend/testing/test_service_credentials.py` | 1,500 | 30 | moderate (1500–2999 LOC) | 10.779s measured | — |
| P5 | backend | `backend/testing/test_auto_remediation.py` | 2,857 | 62 | moderate (1500–2999 LOC) | 9.882s measured | — |
| P5 | backend | `backend/testing/test_diagnostics.py` | 1,505 | 59 | moderate (1500–2999 LOC) | 9.673s measured | — |
| P5 | backend | `backend/testing/test_strategic_spawning_type_selection.py` | 2,620 | 64 | moderate (1500–2999 LOC) | 9.580s measured | — |
| P5 | admin-ui | `admin-ui/src/components/MessagesPanel.filters.test.tsx` | 1,701 | 55 | moderate (1500–2999 LOC) | 8.977s measured | — |
| P5 | admin-ui | `admin-ui/src/components/MessagesPanel.interactions.test.tsx` | 1,846 | 52 | moderate (1500–2999 LOC) | 8.913s measured | — |
| P5 | backend | `backend/testing/test_completion_debt.py` | 2,246 | 56 | moderate (1500–2999 LOC) | 8.440s measured | — |
| P5 | backend | `backend/testing/test_cost_telemetry_reconciliation.py` | 1,694 | 42 | moderate (1500–2999 LOC) | 7.868s measured | — |
| P5 | backend | `backend/testing/test_strategic_spawning_outcome_evaluation.py` | 2,605 | 48 | moderate (1500–2999 LOC) | 6.466s measured | — |
| P5 | backend | `backend/testing/test_evergreen_backlog.py` | 1,539 | 50 | moderate (1500–2999 LOC) | 6.364s measured | — |
| P5 | backend | `backend/testing/test_compute_gcp_startup.py` | 1,991 | 62 | moderate (1500–2999 LOC) | 5.984s measured | — |
| P5 | backend | `backend/testing/test_warm_backlog_reuse.py` | 2,019 | 53 | moderate (1500–2999 LOC) | 5.982s measured | — |
| P5 | backend | `backend/testing/test_payload_schemas.py` | 2,161 | 148 | moderate (1500–2999 LOC) | 5.802s measured | — |
| P5 | admin-ui | `admin-ui/src/components/MessagesPanel.notifications.test.tsx` | 1,524 | 65 | moderate (1500–2999 LOC) | 5.501s measured | — |
| P5 | agent | `agent/src/handlers/audits.test.ts` | 2,190 | 92 | moderate (1500–2999 LOC) | 5.229s measured | — |
| P5 | admin-ui | `admin-ui/src/components/DashboardHome.test.tsx` | 2,682 | 80 | moderate (1500–2999 LOC) | 5.207s measured | — |
| P5 | backend | `backend/testing/test_build_config_commands.py` | 1,718 | 56 | moderate (1500–2999 LOC) | 5.114s measured | — |
| P5 | backend | `backend/testing/test_failure_residual.py` | 1,518 | 59 | moderate (1500–2999 LOC) | 4.634s measured | — |
| P5 | admin-ui | `admin-ui/src/components/MessagesPanel.advanced.test.tsx` | 1,932 | 32 | moderate (1500–2999 LOC) | 4.328s measured | protected; declared floor 32 |
| P5 | backend | `backend/testing/test_operator_inbox_liveness.py` | 2,505 | 50 | moderate (1500–2999 LOC) | 3.890s measured | — |
| P5 | admin-ui | `admin-ui/src/components/KnowledgeBrowser.test.tsx` | 1,689 | 46 | moderate (1500–2999 LOC) | 3.815s measured | — |
| P5 | admin-ui | `admin-ui/src/components/SrTrendBreakdown.test.tsx` | 2,335 | 149 | moderate (1500–2999 LOC) | 3.713s measured | — |
| P5 | backend | `backend/testing/test_lane_baseline_snapshot.py` | 1,520 | 45 | moderate (1500–2999 LOC) | 3.637s measured | — |
| P5 | backend | `backend/testing/test_discovery_review_packet_budget.py` | 1,624 | 25 | moderate (1500–2999 LOC) | 3.599s measured | — |
| P5 | admin-ui | `admin-ui/src/components/StrategicContextRail.test.tsx` | 2,550 | 60 | moderate (1500–2999 LOC) | 3.312s measured | protected; declared floor 60 |
| P5 | backend | `backend/testing/test_strategic_spawning_version_phase.py` | 2,800 | 42 | moderate (1500–2999 LOC) | 3.265s measured | — |
| P5 | admin-ui | `admin-ui/src/components/WarmVmPanel.test.tsx` | 1,769 | 67 | moderate (1500–2999 LOC) | 3.029s measured | — |
| P5 | agent | `agent/src/handlers/strategic.test.ts` | 2,923 | 89 | moderate (1500–2999 LOC) | 3.023s measured | — |
| P5 | admin-ui | `admin-ui/src/components/FleetRunnersPanel.test.tsx` | 2,942 | 94 | moderate (1500–2999 LOC) | 2.946s measured | — |
| P5 | admin-ui | `admin-ui/src/components/TaskDetailDrawerMeta.test.tsx` | 2,200 | 109 | moderate (1500–2999 LOC) | 2.512s measured | — |
| P5 | agent | `agent/src/index.llm-runner.test.ts` | 1,969 | 95 | moderate (1500–2999 LOC) | 2.156s measured | — |
| P5 | backend | `backend/testing/test_test_durations_freshness.py` | 1,669 | 45 | moderate (1500–2999 LOC) | 2.129s measured | — |
| P5 | backend | `backend/testing/test_orchestrator_liveness.py` | 2,379 | 69 | moderate (1500–2999 LOC) | 1.893s measured | — |
| P5 | agent | `agent/src/index.provider-runner.test.ts` | 1,880 | 134 | moderate (1500–2999 LOC) | 1.864s measured | protected; declared floor 134 |
| P5 | backend | `backend/testing/test_fleet_utilization.py` | 1,527 | 42 | moderate (1500–2999 LOC) | 1.586s measured | — |
| P5 | backend | `backend/testing/test_empty_repo_onboarding.py` | 1,837 | 51 | moderate (1500–2999 LOC) | 1.565s measured | — |
| P5 | agent | `agent/src/index.pool-mode.test.ts` | 2,602 | 73 | moderate (1500–2999 LOC) | 1.499s measured | — |
| P5 | admin-ui | `admin-ui/src/hooks/useWorkbenchCommands.test.tsx` | 2,238 | 58 | moderate (1500–2999 LOC) | 1.277s measured | — |
| P5 | backend | `backend/testing/test_review_loop_convergence.py` | 2,684 | 52 | moderate (1500–2999 LOC) | 1.084s measured | — |
| P5 | backend | `backend/testing/test_warm_vm_integration.py` | 2,999 | 28 | moderate (1500–2999 LOC) | 0.992s measured | — |
| P5 | admin-ui | `admin-ui/src/hooks/useTaskModal.test.ts` | 1,698 | 79 | moderate (1500–2999 LOC) | 0.848s measured | — |
| P5 | backend | `backend/testing/test_migration_chain.py` | 1,697 | 80 | moderate (1500–2999 LOC) | 0.714s measured | — |
| P5 | backend | `backend/testing/test_size_offender_governance.py` | 2,953 | 87 | moderate (1500–2999 LOC) | 0.565s measured | — |
| P5 | backend | `backend/testing/test_dependency_contracts.py` | 1,804 | 44 | moderate (1500–2999 LOC) | 0.551s measured | — |
| P5 | backend | `backend/testing/test_ci_resolution.py` | 2,942 | 165 | moderate (1500–2999 LOC) | 0.331s measured | — |
| P5 | backend | `backend/testing/test_finding_deduplication.py` | 2,485 | 131 | moderate (1500–2999 LOC) | 0.286s measured | — |
| P5 | backend | `backend/testing/test_migration_divergence.py` | 2,302 | 76 | moderate (1500–2999 LOC) | 0.275s measured | — |
| P5 | backend | `backend/testing/test_ci_compliance.py` | 1,617 | 37 | moderate (1500–2999 LOC) | 0.253s measured | — |
| P5 | backend | `backend/testing/test_health_zone_pool.py` | 2,149 | 118 | moderate (1500–2999 LOC) | 0.236s measured | — |
| P5 | admin-ui | `admin-ui/src/hooks/useLogViewer.test.ts` | 1,593 | 48 | moderate (1500–2999 LOC) | 0.217s measured | — |
| P5 | backend | `backend/testing/test_size_limits_checker.py` | 2,537 | 102 | moderate (1500–2999 LOC) | 0.162s measured | — |
| P5 | agent | `agent/src/handlers/specialized.test.ts` | 2,570 | 146 | moderate (1500–2999 LOC) | 0.143s measured | — |
| P5 | agent | `agent/src/index.test.ts` | 1,885 | 201 | moderate (1500–2999 LOC) | 0.117s measured | — |
| P5 | backend | `backend/testing/test_llm_runtime.py` | 1,885 | 47 | moderate (1500–2999 LOC) | 0.100s measured | — |
| P5 | agent | `agent/src/index.utilities.test.ts` | 1,556 | 131 | moderate (1500–2999 LOC) | 0.091s measured | — |
| P5 | backend | `backend/testing/test_max_plan_pool_usage.py` | 2,499 | 96 | moderate (1500–2999 LOC) | 0.087s measured | — |
| P5 | agent | `agent/src/handlers/strategic-sections.test.ts` | 2,963 | 198 | moderate (1500–2999 LOC) | 0.085s measured | — |
| P5 | backend | `backend/testing/test_impacted_tests.py` | 1,576 | 62 | moderate (1500–2999 LOC) | 0.082s measured | — |
| P5 | agent | `agent/src/workspacePrep.test.ts` | 1,887 | 74 | moderate (1500–2999 LOC) | 0.063s measured | protected; declared floor 74 |
| P5 | admin-ui | `admin-ui/src/components/MessagesPanel.utils.test.ts` | 1,622 | 168 | moderate (1500–2999 LOC) | 0.063s measured | — |
| P5 | agent | `agent/src/prompt.project-context.test.ts` | 2,116 | 158 | moderate (1500–2999 LOC) | 0.062s measured | — |
| P5 | agent | `agent/src/prompt.test.ts` | 2,513 | 178 | moderate (1500–2999 LOC) | 0.048s measured | — |
| P5 | backend | `backend/testing/test_artifact_refs.py` | 1,546 | 29 | moderate (1500–2999 LOC) | 0.036s measured | — |
| P5 | agent | `agent/src/prompts/thinker.test.ts` | 1,904 | 101 | moderate (1500–2999 LOC) | 0.032s measured | — |
| P5 | agent | `agent/src/prompt.format.test.ts` | 1,512 | 102 | moderate (1500–2999 LOC) | 0.029s measured | — |
| P5 | agent | `agent/src/prompts/pr-helper.test.ts` | 2,353 | 41 | moderate (1500–2999 LOC) | 0.025s measured | — |
| G | backend | `backend/testing/test_scripts_test_lane.py` | 389 | 3 | under 1500 LOC | 208.064s measured | — |
| G | backend | `backend/testing/test_task_outcomes.py` | 1,375 | 108 | under 1500 LOC | 187.368s measured | — |
| G | backend | `backend/testing/test_task_evaluators.py` | 1,172 | 79 | under 1500 LOC | 153.388s measured | — |
| G | backend | `backend/testing/test_qualification_triggers.py` | 1,059 | 80 | under 1500 LOC | 143.296s measured | — |
| G | backend | `backend/testing/test_rate_limit_detection.py` | 995 | 68 | under 1500 LOC | 130.206s measured | — |
| G | backend | `backend/testing/test_task_constructor_authority_manifest.py` | 606 | 9 | under 1500 LOC | 118.662s measured | — |
| G | backend | `backend/testing/test_provider_settings_bag.py` | 945 | 53 | under 1500 LOC | 114.172s measured | — |
| G | backend | `backend/testing/test_research_context_eviction.py` | 1,013 | 64 | under 1500 LOC | 108.568s measured | — |
| G | backend | `backend/testing/test_failure_layer_classifier.py` | 542 | 72 | under 1500 LOC | 100.534s measured | — |
| G | backend | `backend/testing/test_providers.py` | 1,256 | 80 | under 1500 LOC | 98.906s measured | — |
| G | backend | `backend/testing/test_migration_0724_name_reuse_after_revocation.py` | 287 | 6 | under 1500 LOC | 90.314s measured | — |
| G | backend | `backend/testing/test_migration_0681_0694_preservation.py` | 1,093 | 3 | under 1500 LOC | 86.621s measured | — |
| G | backend | `backend/testing/test_rollup_orphan_prs.py` | 350 | 32 | under 1500 LOC | 79.804s measured | — |
| G | backend | `backend/testing/test_project_resource_envelope.py` | 883 | 65 | under 1500 LOC | 77.566s measured | — |
| G | backend | `backend/testing/test_rollup_state.py` | 209 | 24 | under 1500 LOC | 62.286s measured | — |
| G | backend | `backend/testing/test_harness_timeout_budgets.py` | 1,051 | 19 | under 1500 LOC | 57.849s measured | — |
| G | agent | `agent/src/prompts/terminal-checkpoint-helpers.test.ts` | 435 | 12 | under 1500 LOC | 52.983s measured | — |
| G | backend | `backend/testing/test_scope_complexity.py` | 380 | 32 | under 1500 LOC | 52.778s measured | — |
| G | backend | `backend/testing/test_migration_0640_0682_workday_chain.py` | 334 | 1 | under 1500 LOC | 50.136s measured | — |
| G | backend | `backend/testing/test_local_runner_viewable_selection.py` | 1,039 | 43 | under 1500 LOC | 47.838s measured | — |
| G | backend | `backend/testing/test_review_policy_parity.py` | 372 | 22 | under 1500 LOC | 47.704s measured | — |
| G | backend | `backend/testing/test_playbook_storage.py` | 723 | 24 | under 1500 LOC | 46.644s measured | — |
| G | backend | `backend/testing/test_release_audit_contract.py` | 785 | 22 | under 1500 LOC | 45.066s measured | — |
| G | backend | `backend/testing/test_qualification_dimensions.py` | 180 | 12 | under 1500 LOC | 36.808s measured | — |
| G | backend | `backend/testing/test_runner_progress_protocol.py` | 467 | 19 | under 1500 LOC | 35.190s measured | — |
| G | backend | `backend/testing/test_strategic_trigger.py` | 809 | 52 | under 1500 LOC | 33.716s measured | — |
| G | backend | `backend/testing/test_migration_0722_discovery_controller.py` | 147 | 1 | under 1500 LOC | 32.526s measured | — |
| G | backend | `backend/testing/test_release_readiness.py` | 1,345 | 48 | under 1500 LOC | 31.104s measured | — |
| G | backend | `backend/testing/test_admin_routers__problem_model.py` | 1,230 | 39 | under 1500 LOC | 31.084s measured | — |
| G | backend | `backend/testing/test_durations_measurability.py` | 280 | 10 | under 1500 LOC | 30.756s measured | — |
| G | backend | `backend/testing/test_agent_tasks.py` | 1,393 | 41 | under 1500 LOC | 30.227s measured | — |
| G | backend | `backend/testing/test_alert_policy.py` | 444 | 21 | under 1500 LOC | 29.090s measured | — |
| G | backend | `backend/testing/test_playbook_learning.py` | 245 | 11 | under 1500 LOC | 28.850s measured | — |
| G | backend | `backend/testing/test_anomaly_detection.py` | 1,298 | 46 | under 1500 LOC | 28.734s measured | — |
| G | backend | `backend/testing/test_running_update_merge.py` | 100 | 8 | under 1500 LOC | 28.518s measured | — |
| G | agent | `agent/src/handlers/bake-smoke-run.test.ts` | 144 | 6 | under 1500 LOC | 28.446s measured | — |
| G | backend | `backend/testing/test_vacuous_guard_shapes.py` | 958 | 7 | under 1500 LOC | 24.779s measured | — |
| G | backend | `backend/testing/test_work_session_commands.py` | 755 | 16 | under 1500 LOC | 24.280s measured | — |
| G | backend | `backend/testing/test_stranded_conversation_message_sweep.py` | 727 | 21 | under 1500 LOC | 23.895s measured | — |
| G | backend | `backend/testing/test_admin_approval_merge_submission_recovery.py` | 1,272 | 16 | under 1500 LOC | 23.418s measured | — |
| G | backend | `backend/testing/test_review_policy.py` | 112 | 13 | under 1500 LOC | 23.108s measured | — |
| G | backend | `backend/testing/test_scripts_dir_resolution_guard.py` | 617 | 11 | under 1500 LOC | 22.819s measured | — |
| G | backend | `backend/testing/test_time_utils.py` | 195 | 20 | under 1500 LOC | 22.100s measured | — |
| G | backend | `backend/testing/test_release_artifacts.py` | 989 | 31 | under 1500 LOC | 21.982s measured | — |
| G | backend | `backend/testing/test_admin_pipeline_observability.py` | 953 | 21 | under 1500 LOC | 21.160s measured | — |
| G | backend | `backend/testing/test_unactionable_approval_holds.py` | 890 | 33 | under 1500 LOC | 21.063s measured | — |
| G | backend | `backend/testing/test_release_stall_alerts.py` | 1,027 | 28 | under 1500 LOC | 20.466s measured | — |
| G | backend | `backend/testing/test_mode_knob_graduation.py` | 1,188 | 22 | under 1500 LOC | 20.344s measured | — |
| G | backend | `backend/testing/test_rollup_review_respawn_cap.py` | 586 | 15 | under 1500 LOC | 19.004s measured | — |
| G | backend | `backend/testing/test_release_qualification_pause_deferral.py` | 628 | 6 | under 1500 LOC | 18.377s measured | — |
| G | backend | `backend/testing/test_admin_routers__runners.py` | 1,317 | 33 | under 1500 LOC | 18.104s measured | — |
| G | backend | `backend/testing/test_agent__claim_core_auth.py` | 725 | 15 | under 1500 LOC | 18.041s measured | — |
| G | backend | `backend/testing/test_approval_exact_target_publication.py` | 1,275 | 9 | under 1500 LOC | 17.933s measured | — |
| G | backend | `backend/testing/test_admin_routers__users.py` | 876 | 34 | under 1500 LOC | 17.656s measured | — |
| G | backend | `backend/testing/test_acceptance_criteria__trials.py` | 586 | 13 | under 1500 LOC | 17.408s measured | — |
| G | backend | `backend/testing/test_release_promotion_metric_manifest.py` | 173 | 6 | under 1500 LOC | 17.020s measured | — |
| G | backend | `backend/testing/test_admin_acceptance_criteria_router.py` | 1,193 | 31 | under 1500 LOC | 16.509s measured | — |
| G | agent | `agent/src/vitest.globalSetup.test.ts` | 237 | 16 | under 1500 LOC | 16.428s measured | — |
| G | backend | `backend/testing/test_url_security.py` | 179 | 11 | under 1500 LOC | 16.416s measured | — |
| G | backend | `backend/testing/test_admin_tasks_fix_loop_summary.py` | 158 | 16 | under 1500 LOC | 16.378s measured | — |
| G | backend | `backend/testing/test_viewable_watch.py` | 335 | 19 | under 1500 LOC | 16.048s measured | — |
| G | backend | `backend/testing/test_pipeline_alert_scan.py` | 736 | 21 | under 1500 LOC | 15.851s measured | — |
| G | agent | `agent/src/handlers/walkthrough-stack.test.ts` | 404 | 14 | under 1500 LOC | 15.826s measured | — |
| G | backend | `backend/testing/test_review_park_disposition.py` | 994 | 26 | under 1500 LOC | 15.819s measured | — |
| G | backend | `backend/testing/test_conversation_coordinator_two_speed.py` | 1,445 | 17 | under 1500 LOC | 15.452s measured | protected; declared floor 17 |
| G | backend | `backend/testing/test_admin_agent_ops.py` | 895 | 10 | under 1500 LOC | 15.303s measured | — |
| G | backend | `backend/testing/test_agent_pipeline_visibility.py` | 807 | 27 | under 1500 LOC | 15.151s measured | — |
| G | backend | `backend/testing/test_admin_warm_pool.py` | 865 | 28 | under 1500 LOC | 15.046s measured | — |
| G | backend | `backend/testing/test_runtime_config_defaults.py` | 310 | 7 | under 1500 LOC | 14.319s measured | — |
| G | backend | `backend/testing/test_workday_artifact_materialization.py` | 1,011 | 17 | under 1500 LOC | 14.190s measured | protected; declared floor 17 |
| G | backend | `backend/testing/test_resource_envelope_enforcement.py` | 1,189 | 27 | under 1500 LOC | 14.188s measured | — |
| G | backend | `backend/testing/test_release_qualifier_audit.py` | 1,145 | 18 | under 1500 LOC | 13.918s measured | — |
| G | backend | `backend/testing/test_admin_project_roles.py` | 741 | 10 | under 1500 LOC | 13.756s measured | — |
| G | backend | `backend/testing/test_reaper_post_orphan_sweep.py` | 608 | 10 | under 1500 LOC | 13.719s measured | — |
| G | backend | `backend/testing/test_release_qualifier_walkthrough_evidence.py` | 968 | 10 | under 1500 LOC | 13.320s measured | — |
| G | backend | `backend/testing/test_admin_playbook.py` | 559 | 17 | under 1500 LOC | 13.160s measured | — |
| G | backend | `backend/testing/test_playbook_auto.py` | 368 | 14 | under 1500 LOC | 13.002s measured | — |
| G | backend | `backend/testing/test_viewport_coverage.py` | 170 | 7 | under 1500 LOC | 12.910s measured | — |
| G | backend | `backend/testing/test_system_state.py` | 713 | 22 | under 1500 LOC | 12.798s measured | — |
| G | backend | `backend/testing/test_rollup_size_preflight.py` | 1,259 | 38 | under 1500 LOC | 12.728s measured | — |
| G | backend | `backend/testing/test_alert_mint_sites.py` | 422 | 6 | under 1500 LOC | 12.544s measured | — |
| G | backend | `backend/testing/test_project_operator_access.py` | 1,006 | 16 | under 1500 LOC | 12.497s measured | — |
| G | backend | `backend/testing/test_release_qualifier__trigger_and_dispatch_regressions.py` | 946 | 17 | under 1500 LOC | 12.452s measured | — |
| G | backend | `backend/testing/test_admin_approval_comment_recovery.py` | 514 | 12 | under 1500 LOC | 12.320s measured | — |
| G | backend | `backend/testing/test_rollup_lineage_fence.py` | 1,089 | 30 | under 1500 LOC | 12.206s measured | — |
| G | backend | `backend/testing/test_strategic_spawn_requests.py` | 1,089 | 20 | under 1500 LOC | 11.955s measured | — |
| G | agent | `agent/src/prompts/code-change-helpers.blocked-receipt.test.ts` | 167 | 2 | under 1500 LOC | 11.482s measured | — |
| G | backend | `backend/testing/test_provider_settings.py` | 873 | 38 | under 1500 LOC | 11.456s measured | — |
| G | backend | `backend/testing/test_admin_approval_dedup_authority.py` | 382 | 4 | under 1500 LOC | 11.431s measured | — |
| G | backend | `backend/testing/test_research_spawn_pending_recovery.py` | 518 | 8 | under 1500 LOC | 11.364s measured | — |
| G | backend | `backend/testing/test_release_prepromotion_rehearsal.py` | 987 | 26 | under 1500 LOC | 11.068s measured | — |
| G | backend | `backend/testing/test_strategic_conversation_query.py` | 923 | 15 | under 1500 LOC | 11.062s measured | — |
| G | backend | `backend/testing/test_review_claim_fence.py` | 678 | 11 | under 1500 LOC | 10.997s measured | — |
| G | backend | `backend/testing/test_compute_quota.py` | 679 | 13 | under 1500 LOC | 10.912s measured | — |
| G | backend | `backend/testing/test_compute_image_environment.py` | 937 | 23 | under 1500 LOC | 10.626s measured | — |
| G | backend | `backend/testing/test_admin_routers__autonomous.py` | 1,171 | 27 | under 1500 LOC | 10.581s measured | — |
| G | backend | `backend/testing/test_attempt24_release_safety.py` | 717 | 9 | under 1500 LOC | 10.224s measured | — |
| G | backend | `backend/testing/test_self_audit.py` | 1,293 | 39 | under 1500 LOC | 10.168s measured | — |
| G | backend | `backend/testing/test_timeout_rate_metric_manifest.py` | 146 | 6 | under 1500 LOC | 10.148s measured | — |
| G | backend | `backend/testing/test_structural_health_audit.py` | 416 | 17 | under 1500 LOC | 10.128s measured | — |
| G | backend | `backend/testing/test_artifact_download.py` | 1,358 | 46 | under 1500 LOC | 10.092s measured | — |
| G | backend | `backend/testing/test_acceptance_criteria__walkthrough.py` | 700 | 13 | under 1500 LOC | 9.803s measured | — |
| G | backend | `backend/testing/test_sequenced_dependent_spawn.py` | 672 | 18 | under 1500 LOC | 9.758s measured | — |
| G | backend | `backend/testing/test_playbook_learning_modularity.py` | 193 | 2 | under 1500 LOC | 9.580s measured | — |
| G | backend | `backend/testing/test_drift_detection.py` | 1,051 | 25 | under 1500 LOC | 9.558s measured | — |
| G | agent | `agent/src/handlers/resident-repo-freshness.test.ts` | 97 | 4 | under 1500 LOC | 9.541s measured | — |
| G | backend | `backend/testing/test_migration_reservations.py` | 752 | 27 | under 1500 LOC | 9.423s measured | — |
| G | backend | `backend/testing/test_research_lock_order.py` | 373 | 7 | under 1500 LOC | 9.396s measured | — |
| G | backend | `backend/testing/test_work_session_metrics.py` | 110 | 3 | under 1500 LOC | 8.840s measured | — |
| G | backend | `backend/testing/test_github_pulls.py` | 1,155 | 40 | under 1500 LOC | 8.351s measured | — |
| G | backend | `backend/testing/test_retry_hint_parser.py` | 65 | 5 | under 1500 LOC | 8.208s measured | — |
| G | backend | `backend/testing/test_strategic_watches.py` | 475 | 11 | under 1500 LOC | 8.031s measured | — |
| G | backend | `backend/testing/test_strategic_workday_handoff_recovery.py` | 307 | 4 | under 1500 LOC | 7.967s measured | protected; declared floor 4 |
| G | backend | `backend/testing/test_strategic_consultation_detail.py` | 654 | 15 | under 1500 LOC | 7.942s measured | — |
| G | backend | `backend/testing/test_migration_0608_projectmessage_foundation.py` | 717 | 10 | under 1500 LOC | 7.721s measured | — |
| G | backend | `backend/testing/test_strategic_cycle_gating.py` | 798 | 19 | under 1500 LOC | 7.701s measured | — |
| G | backend | `backend/testing/test_playbook_lifecycle.py` | 388 | 11 | under 1500 LOC | 7.700s measured | — |
| G | backend | `backend/testing/test_check_gate_fail_closed.py` | 1,497 | 35 | under 1500 LOC | 7.637s measured | — |
| G | backend | `backend/testing/test_github_onboarding_access.py` | 596 | 15 | under 1500 LOC | 7.441s measured | — |
| G | backend | `backend/testing/test_project_message_projection.py` | 1,201 | 30 | under 1500 LOC | 7.374s measured | protected; declared floor 30 |
| G | backend | `backend/testing/test_blocked_retry_authority.py` | 605 | 12 | under 1500 LOC | 7.347s measured | — |
| G | backend | `backend/testing/test_compute_watchdog.py` | 1,476 | 58 | under 1500 LOC | 7.328s measured | — |
| G | backend | `backend/testing/test_rollup_handoff_ci_gate.py` | 204 | 6 | under 1500 LOC | 7.164s measured | — |
| G | backend | `backend/testing/test_review_pending_requeue.py` | 480 | 8 | under 1500 LOC | 7.009s measured | — |
| G | backend | `backend/testing/test_attempt24_operator_flows.py` | 422 | 7 | under 1500 LOC | 6.967s measured | — |
| G | backend | `backend/testing/test_gate_revalidation.py` | 700 | 39 | under 1500 LOC | 6.930s measured | — |
| G | backend | `backend/testing/test_compute__experiments.py` | 1,375 | 35 | under 1500 LOC | 6.879s measured | — |
| G | backend | `backend/testing/test_review_admission_ownership.py` | 298 | 9 | under 1500 LOC | 6.859s measured | — |
| G | backend | `backend/testing/test_admin_request_changes_authority.py` | 642 | 5 | under 1500 LOC | 6.848s measured | — |
| G | backend | `backend/testing/test_dependency_deprecations.py` | 258 | 8 | under 1500 LOC | 6.801s measured | — |
| G | backend | `backend/testing/test_review_wait_terminal_boundaries.py` | 910 | 9 | under 1500 LOC | 6.756s measured | — |
| G | admin-ui | `admin-ui/src/components/NewTaskModal.test.tsx` | 680 | 20 | under 1500 LOC | 6.704s measured | — |
| G | backend | `backend/testing/test_blind_merge_grade.py` | 882 | 18 | under 1500 LOC | 6.703s measured | — |
| G | backend | `backend/testing/test_review_loop_operator_park.py` | 563 | 25 | under 1500 LOC | 6.620s measured | — |
| G | backend | `backend/testing/test_strategic_pipeline_actions.py` | 795 | 22 | under 1500 LOC | 6.594s measured | — |
| G | backend | `backend/testing/test_project_tool_context.py` | 361 | 14 | under 1500 LOC | 6.589s measured | — |
| G | backend | `backend/testing/test_resolve_dialect_name.py` | 115 | 5 | under 1500 LOC | 6.508s measured | — |
| G | backend | `backend/testing/test_sr_compute_wiring.py` | 710 | 12 | under 1500 LOC | 6.277s measured | — |
| G | backend | `backend/testing/test_sr_catch_rate_statistics.py` | 248 | 7 | under 1500 LOC | 6.254s measured | — |
| G | backend | `backend/testing/test_vm_cost_rates.py` | 41 | 4 | under 1500 LOC | 6.220s measured | — |
| G | backend | `backend/testing/test_evaluating_handmerge_latch.py` | 642 | 8 | under 1500 LOC | 6.145s measured | — |
| G | backend | `backend/testing/test_ci_review_wait_dedup_windows.py` | 434 | 4 | under 1500 LOC | 6.111s measured | — |
| G | backend | `backend/testing/test_discovery_models.py` | 382 | 10 | under 1500 LOC | 6.078s measured | — |
| G | backend | `backend/testing/test_qualifier_smoketest_retired.py` | 86 | 6 | under 1500 LOC | 5.944s measured | — |
| G | backend | `backend/testing/test_perf_completion.py` | 345 | 7 | under 1500 LOC | 5.899s measured | — |
| G | backend | `backend/testing/test_strategic_resident_continuous_liveness.py` | 495 | 8 | under 1500 LOC | 5.858s measured | protected; declared floor 8 |
| G | backend | `backend/testing/test_test_imports_checker.py` | 758 | 21 | under 1500 LOC | 5.849s measured | — |
| G | backend | `backend/testing/test_monthly_budget_alerts.py` | 644 | 18 | under 1500 LOC | 5.812s measured | — |
| G | backend | `backend/testing/test_strategic_autonomous_handoff.py` | 416 | 3 | under 1500 LOC | 5.800s measured | — |
| G | backend | `backend/testing/test_admin_create_task.py` | 873 | 22 | under 1500 LOC | 5.675s measured | — |
| G | backend | `backend/testing/test_strategic_memory_envelope.py` | 1,113 | 21 | under 1500 LOC | 5.674s measured | — |
| G | backend | `backend/testing/test_publication_gate_receipt.py` | 618 | 17 | under 1500 LOC | 5.652s measured | — |
| G | admin-ui | `admin-ui/src/test/mock-factory-export-coverage.test.ts` | 1,173 | 17 | under 1500 LOC | 5.549s measured | — |
| G | backend | `backend/testing/test_user_management.py` | 186 | 17 | under 1500 LOC | 5.533s measured | — |
| G | backend | `backend/testing/test_local_runner_holds.py` | 1,299 | 31 | under 1500 LOC | 5.507s measured | — |
| G | agent | `agent/src/viewport-snapshot-script.test.ts` | 337 | 18 | under 1500 LOC | 5.405s measured | — |
| G | backend | `backend/testing/test_release_qualifier_walkthrough_request.py` | 495 | 6 | under 1500 LOC | 5.299s measured | — |
| G | backend | `backend/testing/test_github_token_resolver.py` | 799 | 20 | under 1500 LOC | 5.069s measured | — |
| G | backend | `backend/testing/test_migration_0697_retained_telemetry_history.py` | 431 | 6 | under 1500 LOC | 5.041s measured | — |
| G | backend | `backend/testing/test_schema_invariants.py` | 831 | 14 | under 1500 LOC | 5.029s measured | — |
| G | admin-ui | `admin-ui/src/components/CommandPalette.test.tsx` | 423 | 15 | under 1500 LOC | 5.018s measured | — |
| G | backend | `backend/testing/test_strategic_lane_linearization.py` | 1,300 | 20 | under 1500 LOC | 4.980s measured | — |
| G | backend | `backend/testing/test_admin_warm_vm.py` | 483 | 8 | under 1500 LOC | 4.973s measured | — |
| G | backend | `backend/testing/test_shard_balance.py` | 541 | 10 | under 1500 LOC | 4.919s measured | — |
| G | admin-ui | `admin-ui/src/components/NewProjectModal.test.tsx` | 539 | 15 | under 1500 LOC | 4.881s measured | — |
| G | backend | `backend/testing/test_project_message_delivery.py` | 475 | 8 | under 1500 LOC | 4.860s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useStrategicSession.test.tsx` | 914 | 26 | under 1500 LOC | 4.852s measured | protected; declared floor 26 |
| G | backend | `backend/testing/test_review_pipeline_modularity.py` | 209 | 4 | under 1500 LOC | 4.836s measured | — |
| G | backend | `backend/testing/test_bulk_action.py` | 906 | 15 | under 1500 LOC | 4.685s measured | — |
| G | backend | `backend/testing/test_evaluating_parent_convergence.py` | 392 | 6 | under 1500 LOC | 4.682s measured | — |
| G | backend | `backend/testing/test_vm_lifecycle_forensics.py` | 196 | 5 | under 1500 LOC | 4.632s measured | — |
| G | backend | `backend/testing/test_promotion_read_surface.py` | 823 | 20 | under 1500 LOC | 4.518s measured | — |
| G | backend | `backend/testing/test_deploy_rollback.py` | 536 | 13 | under 1500 LOC | 4.455s measured | — |
| G | backend | `backend/testing/test_admin_approval_gate_release_authority.py` | 281 | 4 | under 1500 LOC | 4.431s measured | — |
| G | backend | `backend/testing/test_direction_saturation.py` | 579 | 12 | under 1500 LOC | 4.424s measured | — |
| G | backend | `backend/testing/test_cost_attribution.py` | 812 | 21 | under 1500 LOC | 4.404s measured | — |
| G | backend | `backend/testing/test_release_qualifier_walkthrough_site.py` | 431 | 6 | under 1500 LOC | 4.351s measured | — |
| G | backend | `backend/testing/test_strategic_consultation_status.py` | 620 | 3 | under 1500 LOC | 4.339s measured | protected; declared floor 3 |
| G | backend | `backend/testing/test_admin_orchestrator_errors.py` | 282 | 9 | under 1500 LOC | 4.325s measured | — |
| G | backend | `backend/testing/test_sr_blind_spots.py` | 569 | 36 | under 1500 LOC | 4.255s measured | — |
| G | admin-ui | `admin-ui/src/components/SettingsOrchestratorSection.test.tsx` | 561 | 31 | under 1500 LOC | 4.234s measured | — |
| G | backend | `backend/testing/test_automatic_task_creation_authority.py` | 280 | 5 | under 1500 LOC | 4.183s measured | — |
| G | backend | `backend/testing/test_provider_cost_summary.py` | 401 | 10 | under 1500 LOC | 4.170s measured | — |
| G | backend | `backend/testing/test_anomaly_proposals.py` | 527 | 13 | under 1500 LOC | 4.142s measured | — |
| G | backend | `backend/testing/test_project_message_creation_latency.py` | 708 | 15 | under 1500 LOC | 4.130s measured | — |
| G | admin-ui | `admin-ui/src/components/OperatorWorkspaces.test.tsx` | 580 | 18 | under 1500 LOC | 4.024s measured | — |
| G | backend | `backend/testing/test_project_telemetry.py` | 508 | 18 | under 1500 LOC | 4.010s measured | — |
| G | backend | `backend/testing/test_parent_terminal_parity.py` | 315 | 4 | under 1500 LOC | 4.003s measured | — |
| G | backend | `backend/testing/test_message_convergence.py` | 434 | 21 | under 1500 LOC | 3.954s measured | — |
| G | backend | `backend/testing/test_release_qualifier_candidate_sha_stamp.py` | 225 | 5 | under 1500 LOC | 3.920s measured | — |
| G | admin-ui | `admin-ui/src/components/TaskDetailDrawer.test.tsx` | 620 | 29 | under 1500 LOC | 3.896s measured | — |
| G | backend | `backend/testing/test_memo_completion.py` | 986 | 24 | under 1500 LOC | 3.851s measured | — |
| G | backend | `backend/testing/test_module_finding_chain.py` | 1,192 | 26 | under 1500 LOC | 3.812s measured | — |
| G | backend | `backend/testing/test_fix_loop_telemetry.py` | 871 | 26 | under 1500 LOC | 3.777s measured | — |
| G | backend | `backend/testing/test_approval_gate_config.py` | 377 | 13 | under 1500 LOC | 3.745s measured | — |
| G | backend | `backend/testing/test_admin_endpoint_cache.py` | 812 | 16 | under 1500 LOC | 3.717s measured | — |
| G | backend | `backend/testing/test_message_supersession.py` | 428 | 12 | under 1500 LOC | 3.704s measured | — |
| G | backend | `backend/testing/test_strategic_resident_pipeline_projection.py` | 324 | 3 | under 1500 LOC | 3.676s measured | protected; declared floor 3 |
| G | backend | `backend/testing/test_strategic_memory_claim_redaction_repair.py` | 414 | 5 | under 1500 LOC | 3.641s measured | — |
| G | backend | `backend/testing/test_projects.py` | 186 | 8 | under 1500 LOC | 3.595s measured | — |
| G | agent | `agent/src/handlers/merge-grade.test.ts` | 539 | 34 | under 1500 LOC | 3.577s measured | — |
| G | backend | `backend/testing/test_warm_pool_readiness.py` | 402 | 6 | under 1500 LOC | 3.574s measured | — |
| G | backend | `backend/testing/test_release_qualifier__retry_episode_cap.py` | 533 | 18 | under 1500 LOC | 3.564s measured | — |
| G | backend | `backend/testing/test_admin_promotion_enablement.py` | 114 | 3 | under 1500 LOC | 3.547s measured | — |
| G | backend | `backend/testing/test_strategic_resident_work_state.py` | 462 | 10 | under 1500 LOC | 3.522s measured | — |
| G | backend | `backend/testing/test_review_wait_monitor_owned_gate.py` | 206 | 1 | under 1500 LOC | 3.501s measured | — |
| G | backend | `backend/testing/test_agent_github.py` | 323 | 7 | under 1500 LOC | 3.469s measured | — |
| G | backend | `backend/testing/test_release_qualifier_contracts.py` | 297 | 7 | under 1500 LOC | 3.469s measured | — |
| G | backend | `backend/testing/test_discovery_authority_reconciliation.py` | 342 | 5 | under 1500 LOC | 3.453s measured | — |
| G | backend | `backend/testing/test_rollup_deliverable_premerge_guard.py` | 548 | 11 | under 1500 LOC | 3.448s measured | — |
| G | backend | `backend/testing/test_workbench_events.py` | 711 | 16 | under 1500 LOC | 3.438s measured | — |
| G | backend | `backend/testing/test_firebase_auth_coverage.py` | 741 | 51 | under 1500 LOC | 3.408s measured | — |
| G | backend | `backend/testing/test_project_cascade.py` | 494 | 6 | under 1500 LOC | 3.404s measured | — |
| G | backend | `backend/testing/test_completion_automatic_task_authority.py` | 306 | 8 | under 1500 LOC | 3.395s measured | — |
| G | backend | `backend/testing/test_rejection_patterns.py` | 424 | 16 | under 1500 LOC | 3.374s measured | — |
| G | backend | `backend/testing/test_cost_reconciliation.py` | 952 | 24 | under 1500 LOC | 3.225s measured | — |
| G | backend | `backend/testing/test_migration_0466_encrypt_max_plan_credentials.py` | 170 | 4 | under 1500 LOC | 3.224s measured | — |
| G | backend | `backend/testing/test_capability_demand.py` | 939 | 36 | under 1500 LOC | 3.216s measured | — |
| G | backend | `backend/testing/test_cross_user_isolation.py` | 298 | 11 | under 1500 LOC | 3.194s measured | — |
| G | backend | `backend/testing/test_system_message_dedupe.py` | 146 | 6 | under 1500 LOC | 3.172s measured | — |
| G | admin-ui | `admin-ui/src/components/RemediationFunnelSection.test.tsx` | 680 | 42 | under 1500 LOC | 3.162s measured | — |
| G | backend | `backend/testing/test_admin_serial_console.py` | 353 | 7 | under 1500 LOC | 3.084s measured | — |
| G | backend | `backend/testing/test_post_commit_recovery_claim.py` | 120 | 3 | under 1500 LOC | 3.024s measured | — |
| G | backend | `backend/testing/test_runner_binding_capacity_reason.py` | 212 | 10 | under 1500 LOC | 2.998s measured | — |
| G | backend | `backend/testing/test_recovery_sidecar.py` | 155 | 3 | under 1500 LOC | 2.975s measured | — |
| G | backend | `backend/testing/test_agent_annotate.py` | 456 | 8 | under 1500 LOC | 2.950s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useDeploymentState.test.ts` | 772 | 34 | under 1500 LOC | 2.924s measured | — |
| G | backend | `backend/testing/test_release_qualifier_infra_continuation.py` | 422 | 5 | under 1500 LOC | 2.917s measured | — |
| G | backend | `backend/testing/test_project_resource_summary.py` | 338 | 4 | under 1500 LOC | 2.896s measured | — |
| G | backend | `backend/testing/test_rollup_premerge_tip.py` | 276 | 2 | under 1500 LOC | 2.831s measured | — |
| G | backend | `backend/testing/test_security_audit_completion.py` | 359 | 8 | under 1500 LOC | 2.808s measured | — |
| G | backend | `backend/testing/test_work_session_command_execution.py` | 520 | 5 | under 1500 LOC | 2.805s measured | — |
| G | backend | `backend/testing/test_plan_noop.py` | 250 | 8 | under 1500 LOC | 2.793s measured | — |
| G | backend | `backend/testing/test_wire_timestamp_zone.py` | 577 | 18 | under 1500 LOC | 2.790s measured | — |
| G | backend | `backend/testing/test_strategic_memory_runtime.py` | 355 | 8 | under 1500 LOC | 2.784s measured | — |
| G | backend | `backend/testing/test_direction_lineage_spend.py` | 556 | 13 | under 1500 LOC | 2.777s measured | — |
| G | backend | `backend/testing/test_strategic_events.py` | 485 | 17 | under 1500 LOC | 2.750s measured | — |
| G | backend | `backend/testing/test_evaluating_fence_hold.py` | 354 | 3 | under 1500 LOC | 2.747s measured | — |
| G | backend | `backend/testing/test_required_env_deploy_parity.py` | 134 | 5 | under 1500 LOC | 2.706s measured | — |
| G | backend | `backend/testing/test_direction_table.py` | 357 | 11 | under 1500 LOC | 2.684s measured | — |
| G | backend | `backend/testing/test_module_size_live_tree.py` | 365 | 8 | under 1500 LOC | 2.648s measured | — |
| G | backend | `backend/testing/test_admin_activity.py` | 411 | 10 | under 1500 LOC | 2.639s measured | — |
| G | backend | `backend/testing/test_strategic_effort_tier_evidence.py` | 780 | 26 | under 1500 LOC | 2.634s measured | — |
| G | agent | `agent/src/prompts/task-type-focus.test.ts` | 634 | 81 | under 1500 LOC | 2.629s measured | — |
| G | agent | `agent/src/screenshots-pr-section.test.ts` | 177 | 10 | under 1500 LOC | 2.627s measured | — |
| G | backend | `backend/testing/test_strategic_memory_retrieval.py` | 339 | 7 | under 1500 LOC | 2.618s measured | — |
| G | backend | `backend/testing/test_message_attachment_retention.py` | 305 | 9 | under 1500 LOC | 2.599s measured | — |
| G | backend | `backend/testing/test_release_promotion_compute_image_refresh.py` | 124 | 3 | under 1500 LOC | 2.584s measured | — |
| G | admin-ui | `admin-ui/src/components/ResearchBrowser.test.tsx` | 839 | 33 | under 1500 LOC | 2.577s measured | — |
| G | backend | `backend/testing/test_required_runtime_env.py` | 261 | 14 | under 1500 LOC | 2.514s measured | — |
| G | backend | `backend/testing/test_platform_kill_metrics.py` | 465 | 26 | under 1500 LOC | 2.491s measured | — |
| G | backend | `backend/testing/test_reaper_sweeps_viewable_retire.py` | 273 | 8 | under 1500 LOC | 2.471s measured | — |
| G | backend | `backend/testing/test_strategic_memory_control.py` | 319 | 7 | under 1500 LOC | 2.444s measured | — |
| G | admin-ui | `admin-ui/src/components/ArchiveView.test.tsx` | 216 | 5 | under 1500 LOC | 2.432s measured | — |
| G | backend | `backend/testing/test_filter_migration_serialization.py` | 226 | 9 | under 1500 LOC | 2.413s measured | — |
| G | agent | `agent/src/sr-pr-section.test.ts` | 206 | 15 | under 1500 LOC | 2.412s measured | — |
| G | backend | `backend/testing/test_auth.py` | 1,098 | 52 | under 1500 LOC | 2.412s measured | — |
| G | backend | `backend/testing/test_runner_rate_limiter.py` | 456 | 14 | under 1500 LOC | 2.410s measured | — |
| G | admin-ui | `admin-ui/src/components/ImprovementLanesPanel.test.tsx` | 741 | 30 | under 1500 LOC | 2.398s measured | — |
| G | agent | `agent/src/prompts/done-helper.test.ts` | 972 | 25 | under 1500 LOC | 2.351s measured | — |
| G | backend | `backend/testing/test_reaper_sweeps.py` | 501 | 12 | under 1500 LOC | 2.343s measured | — |
| G | backend | `backend/testing/test_observability_payload_projection.py` | 514 | 8 | under 1500 LOC | 2.337s measured | — |
| G | backend | `backend/testing/test_strategic_memory_service.py` | 403 | 8 | under 1500 LOC | 2.295s measured | — |
| G | backend | `backend/testing/test_leader_loop_saturation_c3.py` | 400 | 8 | under 1500 LOC | 2.293s measured | — |
| G | backend | `backend/testing/test_release_activation_after_timeout.py` | 143 | 4 | under 1500 LOC | 2.225s measured | — |
| G | agent | `agent/src/mock-factory-export-coverage.test.ts` | 690 | 13 | under 1500 LOC | 2.196s measured | — |
| G | backend | `backend/testing/test_admin_strategic_memory.py` | 99 | 2 | under 1500 LOC | 2.190s measured | — |
| G | agent | `agent/src/guard-suppressions-pr-section.test.ts` | 200 | 11 | under 1500 LOC | 2.171s measured | — |
| G | admin-ui | `admin-ui/src/components/OuterReviewLoopConvergenceWidget.test.tsx` | 419 | 12 | under 1500 LOC | 2.150s measured | — |
| G | backend | `backend/testing/test_warm_claim_account_attribution.py` | 274 | 5 | under 1500 LOC | 2.138s measured | — |
| G | backend | `backend/testing/test_strategic_memory_source_schema.py` | 280 | 8 | under 1500 LOC | 2.115s measured | — |
| G | backend | `backend/testing/test_strategic_fast_live_work.py` | 567 | 5 | under 1500 LOC | 2.091s measured | protected; declared floor 5 |
| G | backend | `backend/testing/test_filter_rollup_conflicts.py` | 491 | 8 | under 1500 LOC | 2.087s measured | — |
| G | backend | `backend/testing/test_category_verdict_ledger.py` | 591 | 9 | under 1500 LOC | 2.085s measured | — |
| G | backend | `backend/testing/test_discovery_admission.py` | 356 | 7 | under 1500 LOC | 2.070s measured | — |
| G | admin-ui | `admin-ui/src/components/DirectionsPanel.test.tsx` | 456 | 17 | under 1500 LOC | 2.069s measured | — |
| G | admin-ui | `admin-ui/src/components/PipelineWorkspace.test.tsx` | 855 | 23 | under 1500 LOC | 2.059s measured | — |
| G | backend | `backend/testing/test_cost_summary.py` | 326 | 11 | under 1500 LOC | 2.053s measured | — |
| G | agent | `agent/src/handlers/audit-challenge.test.ts` | 702 | 32 | under 1500 LOC | 2.047s measured | — |
| G | backend | `backend/testing/test_auto_review.py` | 395 | 11 | under 1500 LOC | 2.020s measured | — |
| G | backend | `backend/testing/test_research_mem_eval.py` | 172 | 2 | under 1500 LOC | 1.993s measured | — |
| G | backend | `backend/testing/test_discovery_review__successor_direction.py` | 162 | 5 | under 1500 LOC | 1.964s measured | — |
| G | backend | `backend/testing/test_memo_exit_e2e.py` | 362 | 7 | under 1500 LOC | 1.950s measured | — |
| G | backend | `backend/testing/test_review_fix_lane_pilot_costs.py` | 1,499 | 20 | under 1500 LOC | 1.940s measured | — |
| G | backend | `backend/testing/test_experiment_authoring_context.py` | 455 | 15 | under 1500 LOC | 1.908s measured | — |
| G | admin-ui | `admin-ui/src/components/AutonomousStatus.test.tsx` | 401 | 22 | under 1500 LOC | 1.904s measured | — |
| G | backend | `backend/testing/test_terminal_reason_stamping.py` | 700 | 22 | under 1500 LOC | 1.898s measured | — |
| G | backend | `backend/testing/test_discovery_runtime_integration.py` | 227 | 7 | under 1500 LOC | 1.897s measured | — |
| G | backend | `backend/testing/test_playbook_capture_success.py` | 833 | 31 | under 1500 LOC | 1.883s measured | — |
| G | backend | `backend/testing/test_task_counts_endpoint.py` | 100 | 3 | under 1500 LOC | 1.858s measured | — |
| G | agent | `agent/src/providers/codex-rate-limit-reset.test.ts` | 474 | 21 | under 1500 LOC | 1.855s measured | — |
| G | backend | `backend/testing/test_operator_workbench_contracts.py` | 865 | 11 | under 1500 LOC | 1.836s measured | protected; declared floor 11 |
| G | admin-ui | `admin-ui/src/components/TaskCard.test.tsx` | 1,030 | 79 | under 1500 LOC | 1.829s measured | — |
| G | backend | `backend/testing/test_admin_bulk_cancellation_fencing.py` | 246 | 2 | under 1500 LOC | 1.827s measured | — |
| G | backend | `backend/testing/test_discovery_review_sequencing.py` | 271 | 4 | under 1500 LOC | 1.794s measured | — |
| G | backend | `backend/testing/test_strategic_ops_registry.py` | 246 | 6 | under 1500 LOC | 1.784s measured | — |
| G | backend | `backend/testing/test_migration_0682_workday_repair.py` | 600 | 6 | under 1500 LOC | 1.768s measured | — |
| G | backend | `backend/testing/test_filter_file_conflicts.py` | 1,153 | 42 | under 1500 LOC | 1.767s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectResourceEnvelopeSection.test.tsx` | 555 | 16 | under 1500 LOC | 1.725s measured | — |
| G | backend | `backend/testing/test_terminal_rollup_pr_sweep.py` | 219 | 5 | under 1500 LOC | 1.700s measured | — |
| G | backend | `backend/testing/test_digest_shared_cache.py` | 251 | 11 | under 1500 LOC | 1.699s measured | — |
| G | backend | `backend/testing/test_strategic_memory_governance.py` | 245 | 5 | under 1500 LOC | 1.697s measured | — |
| G | backend | `backend/testing/test_orchestrator_modularity.py` | 403 | 8 | under 1500 LOC | 1.685s measured | — |
| G | backend | `backend/testing/test_conversational_workday_protection.py` | 1,250 | 38 | under 1500 LOC | 1.672s measured | — |
| G | backend | `backend/testing/test_golden_suite.py` | 962 | 35 | under 1500 LOC | 1.658s measured | — |
| G | backend | `backend/testing/test_landed_signal.py` | 1,009 | 28 | under 1500 LOC | 1.653s measured | — |
| G | admin-ui | `admin-ui/src/components/StrategicContextArtifactSlot.test.tsx` | 981 | 34 | under 1500 LOC | 1.635s measured | — |
| G | backend | `backend/testing/test_admin_scope_measurement.py` | 180 | 7 | under 1500 LOC | 1.592s measured | — |
| G | backend | `backend/testing/test_runner_isolation_gpu_markers.py` | 146 | 2 | under 1500 LOC | 1.580s measured | — |
| G | backend | `backend/testing/test_agent_task_capabilities.py` | 88 | 4 | under 1500 LOC | 1.578s measured | — |
| G | backend | `backend/testing/test_compute_lane_arm_time_medians.py` | 287 | 7 | under 1500 LOC | 1.576s measured | — |
| G | admin-ui | `admin-ui/src/components/DirectionCard.test.tsx` | 907 | 72 | under 1500 LOC | 1.575s measured | — |
| G | admin-ui | `admin-ui/src/components/MessagesComposer.test.tsx` | 182 | 12 | under 1500 LOC | 1.574s measured | — |
| G | backend | `backend/testing/test_compute_terminal_reason_history.py` | 189 | 4 | under 1500 LOC | 1.557s measured | — |
| G | backend | `backend/testing/test_agent_branch_invariants.py` | 610 | 6 | under 1500 LOC | 1.550s measured | — |
| G | backend | `backend/testing/test_migration_exemptions_admin.py` | 293 | 13 | under 1500 LOC | 1.547s measured | — |
| G | admin-ui | `admin-ui/src/components/SettingsNotificationsSection.test.tsx` | 384 | 15 | under 1500 LOC | 1.546s measured | — |
| G | backend | `backend/testing/test_runner_enroll_docs_accuracy.py` | 599 | 19 | under 1500 LOC | 1.546s measured | — |
| G | backend | `backend/testing/test_strategic_memory_knowledge.py` | 245 | 4 | under 1500 LOC | 1.537s measured | — |
| G | backend | `backend/testing/test_issue_implementation.py` | 693 | 15 | under 1500 LOC | 1.493s measured | — |
| G | backend | `backend/testing/test_compute_zone_health.py` | 1,251 | 94 | under 1500 LOC | 1.492s measured | — |
| G | admin-ui | `admin-ui/src/components/TaskContextRail.test.tsx` | 469 | 18 | under 1500 LOC | 1.488s measured | — |
| G | backend | `backend/testing/test_admin_direction_events.py` | 179 | 5 | under 1500 LOC | 1.453s measured | — |
| G | agent | `agent/src/providers/call-resident-tool.test.ts` | 46 | 2 | under 1500 LOC | 1.447s measured | — |
| G | admin-ui | `admin-ui/src/components/ProviderModelBreakdownPanel.test.tsx` | 346 | 13 | under 1500 LOC | 1.432s measured | — |
| G | admin-ui | `admin-ui/src/components/PipelineView.test.tsx` | 409 | 21 | under 1500 LOC | 1.430s measured | — |
| G | backend | `backend/testing/test_sr_analytics_store.py` | 148 | 5 | under 1500 LOC | 1.410s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useProjectResourceSummary.test.tsx` | 209 | 16 | under 1500 LOC | 1.380s measured | — |
| G | backend | `backend/testing/test_task_list_pagination.py` | 175 | 3 | under 1500 LOC | 1.340s measured | — |
| G | admin-ui | `admin-ui/src/components/AlertsBanner.test.tsx` | 194 | 12 | under 1500 LOC | 1.327s measured | — |
| G | backend | `backend/testing/test_lifecycle_crash_investigation.py` | 414 | 18 | under 1500 LOC | 1.326s measured | — |
| G | backend | `backend/testing/test_terminal_rollup_requeue.py` | 710 | 10 | under 1500 LOC | 1.300s measured | — |
| G | backend | `backend/testing/test_db_consistency_mediums.py` | 235 | 12 | under 1500 LOC | 1.296s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectToolHost.test.tsx` | 789 | 28 | under 1500 LOC | 1.289s measured | — |
| G | backend | `backend/testing/test_acceptance_gate__test_corroboration.py` | 1,058 | 26 | under 1500 LOC | 1.289s measured | — |
| G | backend | `backend/testing/test_acceptance_gate__rule_version.py` | 708 | 4 | under 1500 LOC | 1.288s measured | — |
| G | backend | `backend/testing/test_digest_precompute.py` | 848 | 25 | under 1500 LOC | 1.258s measured | — |
| G | admin-ui | `admin-ui/src/components/PRMonitorSection.test.tsx` | 411 | 24 | under 1500 LOC | 1.249s measured | — |
| G | backend | `backend/testing/test_vm_heartbeat_image.py` | 506 | 13 | under 1500 LOC | 1.240s measured | — |
| G | admin-ui | `admin-ui/src/components/DirectionPortfolioSection.test.tsx` | 645 | 20 | under 1500 LOC | 1.225s measured | — |
| G | admin-ui | `admin-ui/src/components/EffortEfficiencyPanel.test.tsx` | 507 | 24 | under 1500 LOC | 1.199s measured | — |
| G | admin-ui | `admin-ui/src/components/KeyboardShortcutsOverlay.test.tsx` | 60 | 3 | under 1500 LOC | 1.165s measured | — |
| G | backend | `backend/testing/test_warm_pool_cost_metrics.py` | 625 | 17 | under 1500 LOC | 1.152s measured | — |
| G | agent | `agent/src/providers/resident-reply-tool.test.ts` | 113 | 3 | under 1500 LOC | 1.148s measured | — |
| G | admin-ui | `admin-ui/src/components/EvergreenFillObservabilityPanel.test.tsx` | 193 | 17 | under 1500 LOC | 1.146s measured | — |
| G | backend | `backend/testing/test_admin_task_requeue_terminal.py` | 180 | 2 | under 1500 LOC | 1.143s measured | — |
| G | backend | `backend/testing/test_compute_experiment_draft.py` | 364 | 20 | under 1500 LOC | 1.141s measured | — |
| G | backend | `backend/testing/test_db.py` | 67 | 3 | under 1500 LOC | 1.132s measured | — |
| G | backend | `backend/testing/test_discovery_withheld_findings.py` | 396 | 7 | under 1500 LOC | 1.112s measured | — |
| G | backend | `backend/testing/test_no_ci_merge_gate_setup_retry.py` | 500 | 5 | under 1500 LOC | 1.110s measured | — |
| G | backend | `backend/testing/test_findings_index.py` | 416 | 11 | under 1500 LOC | 1.097s measured | — |
| G | admin-ui | `admin-ui/src/components/NotesTool.test.tsx` | 424 | 15 | under 1500 LOC | 1.087s measured | protected; declared floor 15 |
| G | backend | `backend/testing/test_release_qualifier_retained_scoring.py` | 125 | 1 | under 1500 LOC | 1.082s measured | — |
| G | agent | `agent/src/handlers/strategic-resident-cold-fast.stream.test.ts` | 340 | 8 | under 1500 LOC | 1.054s measured | — |
| G | backend | `backend/testing/test_security.py` | 716 | 68 | under 1500 LOC | 0.981s measured | — |
| G | backend | `backend/testing/test_optimistic_version.py` | 199 | 3 | under 1500 LOC | 0.904s measured | — |
| G | backend | `backend/testing/test_c5_approval_gate_invariant.py` | 309 | 3 | under 1500 LOC | 0.894s measured | — |
| G | backend | `backend/testing/test_blocked_recheck_autonomy.py` | 595 | 12 | under 1500 LOC | 0.878s measured | — |
| G | backend | `backend/testing/test_strategic_memory_claim_redaction_repair_retry.py` | 252 | 2 | under 1500 LOC | 0.866s measured | — |
| G | admin-ui | `admin-ui/src/components/DashboardMetricCards.test.tsx` | 208 | 24 | under 1500 LOC | 0.862s measured | — |
| G | backend | `backend/testing/test_task_direction_refs.py` | 50 | 3 | under 1500 LOC | 0.860s measured | — |
| G | backend | `backend/testing/test_migration_0696_project_sr_analytics.py` | 269 | 6 | under 1500 LOC | 0.856s measured | — |
| G | backend | `backend/testing/test_automatic_task_creation_producers.py` | 233 | 6 | under 1500 LOC | 0.840s measured | — |
| G | admin-ui | `admin-ui/src/components/SettingsDeploySection.test.tsx` | 233 | 13 | under 1500 LOC | 0.809s measured | — |
| G | backend | `backend/testing/test_strategic_cycle_cost_projection.py` | 1,143 | 46 | under 1500 LOC | 0.761s measured | — |
| G | admin-ui | `admin-ui/src/components/PrimaryTabNav.test.tsx` | 257 | 12 | under 1500 LOC | 0.759s measured | — |
| G | backend | `backend/testing/test_research_deferred_carry.py` | 62 | 1 | under 1500 LOC | 0.738s measured | — |
| G | admin-ui | `admin-ui/src/components/TaskDetailDrawerSections.test.tsx` | 1,016 | 81 | under 1500 LOC | 0.737s measured | — |
| G | admin-ui | `admin-ui/src/components/DirectionAutoPauseWidget.test.tsx` | 102 | 4 | under 1500 LOC | 0.737s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useProjectConsultations.test.tsx` | 97 | 5 | under 1500 LOC | 0.723s measured | — |
| G | backend | `backend/testing/test_admin_cancel_impact.py` | 189 | 2 | under 1500 LOC | 0.701s measured | — |
| G | admin-ui | `admin-ui/eslint-rules/no-oversized-file.test.js` | 60 | 0 | under 1500 LOC | 0.693s measured | — |
| G | admin-ui | `admin-ui/src/components/StrategicContextWorkspace.test.tsx` | 775 | 20 | under 1500 LOC | 0.692s measured | protected; declared floor 20 |
| G | backend | `backend/testing/test_operator_attention_query_shape.py` | 524 | 11 | under 1500 LOC | 0.678s measured | — |
| G | backend | `backend/testing/test_direct_llm_client.py` | 613 | 28 | under 1500 LOC | 0.676s measured | — |
| G | backend | `backend/testing/test_migration_0720_direction_index_repair.py` | 255 | 8 | under 1500 LOC | 0.671s measured | — |
| G | backend | `backend/testing/test_task_completion_modularity.py` | 169 | 2 | under 1500 LOC | 0.670s measured | — |
| G | backend | `backend/testing/test_runner_decline_epoch.py` | 419 | 8 | under 1500 LOC | 0.641s measured | — |
| G | backend | `backend/testing/test_digest_cache.py` | 271 | 13 | under 1500 LOC | 0.635s measured | — |
| G | backend | `backend/testing/test_inline_llm.py` | 345 | 10 | under 1500 LOC | 0.626s measured | — |
| G | admin-ui | `admin-ui/eslint-rules/no-fixed-width-overflow.test.js` | 92 | 0 | under 1500 LOC | 0.605s measured | — |
| G | backend | `backend/testing/test_admin_notifications.py` | 85 | 2 | under 1500 LOC | 0.603s measured | — |
| G | admin-ui | `admin-ui/src/components/KanbanColumn.test.tsx` | 150 | 6 | under 1500 LOC | 0.591s measured | — |
| G | backend | `backend/testing/test_retry_budget.py` | 31 | 2 | under 1500 LOC | 0.586s measured | — |
| G | admin-ui | `admin-ui/src/components/LaneDiagPanel.test.tsx` | 524 | 23 | under 1500 LOC | 0.570s measured | — |
| G | admin-ui | `admin-ui/src/components/TaskComputeJobsSection.test.tsx` | 145 | 3 | under 1500 LOC | 0.557s measured | — |
| G | admin-ui | `admin-ui/src/components/KanbanBoard.test.tsx` | 244 | 12 | under 1500 LOC | 0.542s measured | — |
| G | admin-ui | `admin-ui/src/components/ReleaseQualificationFindings.test.tsx` | 179 | 8 | under 1500 LOC | 0.542s measured | — |
| G | backend | `backend/testing/test_orch_db_isolation.py` | 181 | 2 | under 1500 LOC | 0.536s measured | — |
| G | backend | `backend/testing/test_metrics_integrity.py` | 203 | 4 | under 1500 LOC | 0.535s measured | — |
| G | backend | `backend/testing/test_migration_0669_backfill_direction_fa_telemetry.py` | 271 | 9 | under 1500 LOC | 0.534s measured | — |
| G | backend | `backend/testing/test_migration_0698_cochange_state.py` | 217 | 2 | under 1500 LOC | 0.525s measured | — |
| G | backend | `backend/testing/test_rate_limit_verbatim_corpus.py` | 269 | 2 | under 1500 LOC | 0.520s measured | — |
| G | backend | `backend/testing/test_rollup_open_pr_reconciliation.py` | 94 | 1 | under 1500 LOC | 0.517s measured | — |
| G | backend | `backend/testing/test_branch_resolution_alerts.py` | 100 | 2 | under 1500 LOC | 0.509s measured | — |
| G | admin-ui | `admin-ui/src/components/WarmReuseVsCostTrend.test.tsx` | 218 | 9 | under 1500 LOC | 0.492s measured | — |
| G | admin-ui | `admin-ui/src/components/FixLoopConvergenceWidget.test.tsx` | 156 | 2 | under 1500 LOC | 0.491s measured | — |
| G | backend | `backend/testing/test_concurrency_overlap.py` | 303 | 7 | under 1500 LOC | 0.486s measured | — |
| G | backend | `backend/testing/test_llm_cost_attribution.py` | 981 | 19 | under 1500 LOC | 0.465s measured | — |
| G | admin-ui | `admin-ui/src/components/ScopeTrendsSection.test.tsx` | 234 | 17 | under 1500 LOC | 0.464s measured | — |
| G | admin-ui | `admin-ui/src/components/StrategicSessionControl.test.tsx` | 426 | 22 | under 1500 LOC | 0.459s measured | protected; declared floor 22 |
| G | admin-ui | `admin-ui/src/components/DriftSignalsCard.test.tsx` | 389 | 11 | under 1500 LOC | 0.450s measured | — |
| G | backend | `backend/testing/test_database_disconnect.py` | 511 | 27 | under 1500 LOC | 0.448s measured | — |
| G | backend | `backend/testing/test_migration_0718_strategic_memory_timezone_columns.py` | 84 | 1 | under 1500 LOC | 0.447s measured | — |
| G | backend | `backend/testing/test_work_session_actions.py` | 187 | 5 | under 1500 LOC | 0.440s measured | — |
| G | backend | `backend/testing/test_spend_cap_trustworthiness.py` | 355 | 15 | under 1500 LOC | 0.439s measured | — |
| G | backend | `backend/testing/test_lock_ordering.py` | 152 | 2 | under 1500 LOC | 0.436s measured | — |
| G | admin-ui | `admin-ui/src/components/HeaderBar.test.tsx` | 136 | 5 | under 1500 LOC | 0.434s measured | — |
| G | backend | `backend/testing/test_migration_0717_strategic_memory_semantic_ledger.py` | 217 | 3 | under 1500 LOC | 0.425s measured | — |
| G | backend | `backend/testing/test_strategic_memory_corrections.py` | 532 | 14 | under 1500 LOC | 0.416s measured | — |
| G | backend | `backend/testing/test_digest_latency.py` | 200 | 10 | under 1500 LOC | 0.415s measured | — |
| G | admin-ui | `admin-ui/src/components/MarkdownContentInner.test.tsx` | 115 | 12 | under 1500 LOC | 0.413s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useDirectionsData.readOnly.test.ts` | 342 | 6 | under 1500 LOC | 0.401s measured | — |
| G | admin-ui | `admin-ui/src/components/TaskDurationWidget.test.tsx` | 64 | 7 | under 1500 LOC | 0.399s measured | — |
| G | admin-ui | `admin-ui/src/components/BulkActionBar.test.tsx` | 142 | 5 | under 1500 LOC | 0.396s measured | — |
| G | admin-ui | `admin-ui/src/components/RuntimeAccountsPanel.test.tsx` | 250 | 7 | under 1500 LOC | 0.381s measured | — |
| G | admin-ui | `admin-ui/src/utils/renderWhiteboardArtifact.test.ts` | 217 | 8 | under 1500 LOC | 0.380s measured | — |
| G | admin-ui | `admin-ui/src/components/MetricsDashboard.test.tsx` | 751 | 14 | under 1500 LOC | 0.378s measured | — |
| G | backend | `backend/testing/test_websocket_server_integration.py` | 104 | 1 | under 1500 LOC | 0.364s measured | — |
| G | backend | `backend/testing/test_task_capabilities.py` | 812 | 37 | under 1500 LOC | 0.363s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useStrategicLiveness.test.tsx` | 102 | 6 | under 1500 LOC | 0.362s measured | protected; declared floor 6 |
| G | admin-ui | `admin-ui/src/components/MessagesSummaryCard.test.tsx` | 88 | 5 | under 1500 LOC | 0.360s measured | — |
| G | backend | `backend/testing/test_strategic_memory_sources.py` | 174 | 5 | under 1500 LOC | 0.354s measured | — |
| G | agent | `agent/src/prompts/review-prompt.test.ts` | 367 | 42 | under 1500 LOC | 0.349s measured | — |
| G | admin-ui | `admin-ui/src/components/Toasts.test.tsx` | 81 | 2 | under 1500 LOC | 0.348s measured | — |
| G | admin-ui | `admin-ui/src/components/SettingsSectionRouter.test.tsx` | 141 | 7 | under 1500 LOC | 0.347s measured | — |
| G | admin-ui | `admin-ui/src/components/MetricsIntegrityObservabilityPanel.test.tsx` | 128 | 3 | under 1500 LOC | 0.347s measured | — |
| G | backend | `backend/testing/test_frontend_static.py` | 155 | 8 | under 1500 LOC | 0.341s measured | — |
| G | admin-ui | `admin-ui/src/components/ComputeArtifactGallery.test.tsx` | 559 | 24 | under 1500 LOC | 0.341s measured | — |
| G | admin-ui | `admin-ui/src/components/SaveViewDialog.test.tsx` | 112 | 4 | under 1500 LOC | 0.335s measured | — |
| G | backend | `backend/testing/test_admin_digest_modularity.py` | 571 | 10 | under 1500 LOC | 0.335s measured | — |
| G | backend | `backend/testing/test_scripts_lane_hermetic_credentials.py` | 431 | 8 | under 1500 LOC | 0.315s measured | — |
| G | admin-ui | `admin-ui/src/components/FailureStageWidget.test.tsx` | 61 | 5 | under 1500 LOC | 0.313s measured | — |
| G | backend | `backend/testing/test_outer_loop_convergence.py` | 546 | 22 | under 1500 LOC | 0.311s measured | — |
| G | admin-ui | `admin-ui/src/components/MessageTypeDropdown.test.tsx` | 115 | 7 | under 1500 LOC | 0.309s measured | — |
| G | admin-ui | `admin-ui/src/components/SettingsAccountsSection.test.tsx` | 362 | 10 | under 1500 LOC | 0.303s measured | — |
| G | backend | `backend/testing/test_spawn_calibration.py` | 581 | 31 | under 1500 LOC | 0.302s measured | — |
| G | admin-ui | `admin-ui/src/components/MessageBubble.test.tsx` | 711 | 41 | under 1500 LOC | 0.296s measured | protected; declared floor 41 |
| G | backend | `backend/testing/test_migration_0657_0671_resident_session_id.py` | 228 | 4 | under 1500 LOC | 0.281s measured | — |
| G | backend | `backend/testing/test_migration_0679_backlog_clear.py` | 423 | 7 | under 1500 LOC | 0.278s measured | — |
| G | admin-ui | `admin-ui/src/components/TaskDetailDrawerSideboardContext.test.tsx` | 325 | 7 | under 1500 LOC | 0.276s measured | — |
| G | admin-ui | `admin-ui/src/components/WarmStartOriginRow.test.tsx` | 33 | 3 | under 1500 LOC | 0.276s measured | — |
| G | backend | `backend/testing/test_migration_0656_projectmessage_context_audience_index.py` | 194 | 2 | under 1500 LOC | 0.265s measured | — |
| G | admin-ui | `admin-ui/src/components/WhiteboardTool.test.tsx` | 687 | 14 | under 1500 LOC | 0.263s measured | protected; declared floor 14 |
| G | backend | `backend/testing/test_compute_gpu.py` | 341 | 20 | under 1500 LOC | 0.261s measured | — |
| G | backend | `backend/testing/test_strategic_payload.py` | 389 | 7 | under 1500 LOC | 0.260s measured | — |
| G | backend | `backend/testing/test_github_refs.py` | 395 | 14 | under 1500 LOC | 0.258s measured | — |
| G | agent | `agent/src/git-credential.test.ts` | 170 | 11 | under 1500 LOC | 0.248s measured | — |
| G | backend | `backend/testing/test_deploy_smoketest.py` | 119 | 6 | under 1500 LOC | 0.242s measured | — |
| G | backend | `backend/testing/test_playbook_archive.py` | 1,016 | 35 | under 1500 LOC | 0.242s measured | — |
| G | backend | `backend/testing/test_migration_0714_strategic_conversation_correctness.py` | 414 | 3 | under 1500 LOC | 0.238s measured | — |
| G | admin-ui | `admin-ui/src/components/VersionLifecyclePanelPrimitives.test.tsx` | 1,063 | 107 | under 1500 LOC | 0.237s measured | — |
| G | agent | `agent/src/handlers/verify-tests.test.ts` | 1,356 | 62 | under 1500 LOC | 0.234s measured | — |
| G | backend | `backend/testing/test_release_activation_approval.py` | 133 | 6 | under 1500 LOC | 0.233s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectBuildConfigSection.test.tsx` | 129 | 2 | under 1500 LOC | 0.229s measured | — |
| G | admin-ui | `admin-ui/src/components/NullBranchDiagnosticsPanel.test.tsx` | 147 | 10 | under 1500 LOC | 0.228s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectMembers.test.tsx` | 111 | 7 | under 1500 LOC | 0.226s measured | — |
| G | agent | `agent/src/providers/runners.test.ts` | 941 | 0 | under 1500 LOC | 0.225s measured | — |
| G | backend | `backend/testing/test_source_ref_validation.py` | 1,469 | 78 | under 1500 LOC | 0.225s measured | — |
| G | backend | `backend/testing/test_test_estate_ranked_queue.py` | 706 | 13 | under 1500 LOC | 0.224s measured | — |
| G | backend | `backend/testing/test_csrf.py` | 299 | 22 | under 1500 LOC | 0.218s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useDirectionsData.test.ts` | 245 | 3 | under 1500 LOC | 0.216s measured | — |
| G | backend | `backend/testing/test_failure_classification_rate_limit.py` | 1,008 | 48 | under 1500 LOC | 0.214s measured | — |
| G | admin-ui | `admin-ui/src/components/RecurringPatternsSection.test.tsx` | 342 | 12 | under 1500 LOC | 0.212s measured | — |
| G | backend | `backend/testing/test_request_duration_telemetry.py` | 231 | 8 | under 1500 LOC | 0.204s measured | — |
| G | backend | `backend/testing/test_jwt_revocation.py` | 666 | 29 | under 1500 LOC | 0.203s measured | — |
| G | backend | `backend/testing/test_pause_clock.py` | 351 | 25 | under 1500 LOC | 0.202s measured | — |
| G | agent | `agent/src/prompts/self-review.test.ts` | 610 | 76 | under 1500 LOC | 0.201s measured | — |
| G | admin-ui | `admin-ui/src/components/SystemMaturityCard.test.tsx` | 106 | 13 | under 1500 LOC | 0.196s measured | — |
| G | admin-ui | `admin-ui/src/components/EvergreenBacklogPanel.test.tsx` | 140 | 12 | under 1500 LOC | 0.195s measured | — |
| G | admin-ui | `admin-ui/src/components/StrategicAuditPanel.test.tsx` | 247 | 13 | under 1500 LOC | 0.193s measured | — |
| G | backend | `backend/testing/test_stall_advisory.py` | 298 | 9 | under 1500 LOC | 0.190s measured | — |
| G | backend | `backend/testing/test_vitest_durations_freshness.py` | 742 | 29 | under 1500 LOC | 0.186s measured | — |
| G | backend | `backend/testing/test_filter_malformed_payload.py` | 159 | 8 | under 1500 LOC | 0.186s measured | — |
| G | admin-ui | `admin-ui/src/components/ProblemModelViewer.test.tsx` | 418 | 13 | under 1500 LOC | 0.180s measured | — |
| G | backend | `backend/testing/test_verification_toolchain.py` | 203 | 5 | under 1500 LOC | 0.179s measured | — |
| G | backend | `backend/testing/test_resident_supersession_livelock.py` | 395 | 19 | under 1500 LOC | 0.178s measured | — |
| G | backend | `backend/testing/test_runbook_drift.py` | 103 | 5 | under 1500 LOC | 0.177s measured | — |
| G | backend | `backend/testing/test_migration_0716_strategic_memory_source_control_plane.py` | 112 | 2 | under 1500 LOC | 0.176s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useConversation.test.tsx` | 1,218 | 36 | under 1500 LOC | 0.174s measured | protected; declared floor 36 |
| G | backend | `backend/testing/test_chain_continuity_funnel.py` | 135 | 5 | under 1500 LOC | 0.173s measured | — |
| G | admin-ui | `admin-ui/src/components/ConsultationProgressRow.test.tsx` | 156 | 5 | under 1500 LOC | 0.171s measured | — |
| G | backend | `backend/testing/test_migration_0713_hot_sweep_indexes.py` | 124 | 2 | under 1500 LOC | 0.171s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectManagementModal.test.tsx` | 227 | 16 | under 1500 LOC | 0.170s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectAlertDomainsSection.test.tsx` | 77 | 3 | under 1500 LOC | 0.168s measured | — |
| G | admin-ui | `admin-ui/src/api.test.ts` | 557 | 29 | under 1500 LOC | 0.166s measured | — |
| G | backend | `backend/testing/test_staging_base_refresh.py` | 377 | 16 | under 1500 LOC | 0.166s measured | — |
| G | backend | `backend/testing/test_env_preflight_evidence.py` | 284 | 17 | under 1500 LOC | 0.162s measured | — |
| G | admin-ui | `admin-ui/src/components/FleetHealthCard.test.tsx` | 325 | 16 | under 1500 LOC | 0.161s measured | — |
| G | admin-ui | `admin-ui/src/components/AttachmentPreviewModal.test.tsx` | 204 | 15 | under 1500 LOC | 0.161s measured | — |
| G | admin-ui | `admin-ui/src/components/catchupHelpers.test.ts` | 126 | 17 | under 1500 LOC | 0.161s measured | — |
| G | backend | `backend/testing/test_completion_topic_overlap.py` | 237 | 17 | under 1500 LOC | 0.160s measured | — |
| G | backend | `backend/testing/test_websocket_emitter_inventory.py` | 348 | 7 | under 1500 LOC | 0.160s measured | — |
| G | backend | `backend/testing/test_compute_image_remote_build.py` | 1,142 | 41 | under 1500 LOC | 0.156s measured | — |
| G | backend | `backend/testing/test_release_serving_reason_attribution.py` | 108 | 2 | under 1500 LOC | 0.155s measured | — |
| G | backend | `backend/testing/test_research_completion__source_refs.py` | 1,090 | 76 | under 1500 LOC | 0.155s measured | — |
| G | admin-ui | `admin-ui/src/components/RecentErrorsWidget.test.tsx` | 105 | 4 | under 1500 LOC | 0.155s measured | — |
| G | admin-ui | `admin-ui/src/components/OrchestratorHealthBar.test.tsx` | 486 | 24 | under 1500 LOC | 0.154s measured | — |
| G | admin-ui | `admin-ui/src/components/UILoadTimingPanel.test.tsx` | 338 | 27 | under 1500 LOC | 0.150s measured | — |
| G | admin-ui | `admin-ui/src/components/ConsoleHome.test.tsx` | 201 | 7 | under 1500 LOC | 0.146s measured | — |
| G | backend | `backend/testing/test_deepseek_cost_attribution.py` | 872 | 31 | under 1500 LOC | 0.145s measured | — |
| G | admin-ui | `admin-ui/src/firebase.test.ts` | 46 | 2 | under 1500 LOC | 0.145s measured | — |
| G | agent | `agent/src/node-preflight.test.ts` | 125 | 19 | under 1500 LOC | 0.141s measured | — |
| G | backend | `backend/testing/test_event_fanout.py` | 844 | 46 | under 1500 LOC | 0.139s measured | — |
| G | backend | `backend/testing/test_compute_artifact_iam.py` | 204 | 8 | under 1500 LOC | 0.136s measured | — |
| G | backend | `backend/testing/test_leader_stale_reaper.py` | 247 | 8 | under 1500 LOC | 0.134s measured | — |
| G | admin-ui | `admin-ui/src/components/ErrorBoundary.test.tsx` | 144 | 11 | under 1500 LOC | 0.132s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectWorkspace.readOnly.test.tsx` | 134 | 5 | under 1500 LOC | 0.131s measured | — |
| G | admin-ui | `admin-ui/src/components/ProviderCostPanel.test.tsx` | 320 | 10 | under 1500 LOC | 0.130s measured | — |
| G | admin-ui | `admin-ui/src/components/PoolAccountHealthPanel.test.tsx` | 348 | 15 | under 1500 LOC | 0.128s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectAutoReviewSection.test.tsx` | 50 | 2 | under 1500 LOC | 0.126s measured | — |
| G | admin-ui | `admin-ui/src/components/MuteWindowMenu.test.tsx` | 66 | 4 | under 1500 LOC | 0.125s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useTasksState.test.ts` | 365 | 18 | under 1500 LOC | 0.124s measured | — |
| G | admin-ui | `admin-ui/src/components/MessageAttachments.test.tsx` | 75 | 3 | under 1500 LOC | 0.123s measured | — |
| G | admin-ui | `admin-ui/src/components/FleetCapacityCard.test.tsx` | 320 | 22 | under 1500 LOC | 0.121s measured | — |
| G | admin-ui | `admin-ui/eslint-rules/no-arbitrary-font-size.test.js` | 128 | 0 | under 1500 LOC | 0.120s measured | — |
| G | admin-ui | `admin-ui/src/uiLoadTiming.test.ts` | 66 | 4 | under 1500 LOC | 0.120s measured | — |
| G | backend | `backend/testing/test_issue_ledger.py` | 989 | 36 | under 1500 LOC | 0.118s measured | — |
| G | admin-ui | `admin-ui/src/components/CapabilityTrialsPanel.test.tsx` | 161 | 11 | under 1500 LOC | 0.117s measured | — |
| G | admin-ui | `admin-ui/src/utils/attachments.test.ts` | 82 | 5 | under 1500 LOC | 0.117s measured | — |
| G | backend | `backend/testing/test_agent_access_projections.py` | 294 | 5 | under 1500 LOC | 0.116s measured | — |
| G | backend | `backend/testing/test_hotfix_narrow_candidate.py` | 390 | 13 | under 1500 LOC | 0.115s measured | — |
| G | backend | `backend/testing/test_shared_state.py` | 610 | 29 | under 1500 LOC | 0.113s measured | — |
| G | admin-ui | `admin-ui/src/components/SettingsGeneralSection.test.tsx` | 105 | 8 | under 1500 LOC | 0.113s measured | — |
| G | backend | `backend/testing/test_discovery_evidence_bound.py` | 223 | 11 | under 1500 LOC | 0.110s measured | — |
| G | backend | `backend/testing/test_migration_0626_safe_encrypt_backfill.py` | 344 | 6 | under 1500 LOC | 0.110s measured | — |
| G | admin-ui | `admin-ui/src/components/DependencyHealthCard.test.tsx` | 196 | 9 | under 1500 LOC | 0.108s measured | — |
| G | backend | `backend/testing/test_filter_scope_empty.py` | 342 | 12 | under 1500 LOC | 0.106s measured | — |
| G | agent | `agent/src/handlers/verify-tests.gate-bound.test.ts` | 249 | 5 | under 1500 LOC | 0.106s measured | — |
| G | backend | `backend/testing/test_direction_referential_guard.py` | 472 | 33 | under 1500 LOC | 0.105s measured | — |
| G | backend | `backend/testing/test_process_role_lifecycle.py` | 347 | 25 | under 1500 LOC | 0.101s measured | — |
| G | backend | `backend/testing/test_anomaly_persistence.py` | 203 | 7 | under 1500 LOC | 0.100s measured | — |
| G | backend | `backend/testing/test_task_lifecycle_modularity.py` | 162 | 2 | under 1500 LOC | 0.098s measured | — |
| G | backend | `backend/testing/test_github_repo_snapshot.py` | 924 | 39 | under 1500 LOC | 0.096s measured | — |
| G | backend | `backend/testing/test_account_model_availability.py` | 829 | 32 | under 1500 LOC | 0.094s measured | — |
| G | backend | `backend/testing/test_backmerge_invariants.py` | 201 | 3 | under 1500 LOC | 0.093s measured | — |
| G | backend | `backend/testing/test_sweep_stale_state.py` | 202 | 11 | under 1500 LOC | 0.092s measured | — |
| G | agent | `agent/src/providers/resident-session.test.ts` | 1,067 | 48 | under 1500 LOC | 0.091s measured | protected; declared floor 48 |
| G | admin-ui | `admin-ui/eslint-rules/min-touch-target.test.js` | 105 | 0 | under 1500 LOC | 0.091s measured | — |
| G | admin-ui | `admin-ui/src/test/viewport-coverage.test.ts` | 263 | 23 | under 1500 LOC | 0.090s measured | — |
| G | admin-ui | `admin-ui/src/components/AccountMenu.test.tsx` | 116 | 6 | under 1500 LOC | 0.090s measured | — |
| G | admin-ui | `admin-ui/src/components/TaskStreamPanel.test.tsx` | 80 | 4 | under 1500 LOC | 0.090s measured | — |
| G | backend | `backend/testing/test_models.py` | 532 | 34 | under 1500 LOC | 0.089s measured | — |
| G | agent | `agent/src/handlers/artifact-consume.test.ts` | 436 | 19 | under 1500 LOC | 0.089s measured | — |
| G | backend | `backend/testing/test_migration_0715_strategic_context_envelopes.py` | 53 | 1 | under 1500 LOC | 0.087s measured | — |
| G | backend | `backend/testing/test_compute_secret_manifest.py` | 252 | 10 | under 1500 LOC | 0.086s measured | — |
| G | admin-ui | `admin-ui/src/hooks/__tests__/useLogViewer.test.ts` | 511 | 8 | under 1500 LOC | 0.086s measured | — |
| G | agent | `agent/src/handlers/live-conversation-resident.test.ts` | 257 | 13 | under 1500 LOC | 0.086s measured | — |
| G | backend | `backend/testing/test_evidence_primitive.py` | 543 | 52 | under 1500 LOC | 0.086s measured | — |
| G | admin-ui | `admin-ui/src/components/StrategicContextMap.test.tsx` | 140 | 5 | under 1500 LOC | 0.081s measured | — |
| G | admin-ui | `admin-ui/src/components/EventVisibilityMenu.test.tsx` | 87 | 5 | under 1500 LOC | 0.081s measured | — |
| G | agent | `agent/src/handlers/typed-fast-resident.test.ts` | 481 | 16 | under 1500 LOC | 0.080s measured | protected; declared floor 16 |
| G | admin-ui | `admin-ui/src/components/ScopeConversionRatesSection.test.tsx` | 377 | 26 | under 1500 LOC | 0.080s measured | — |
| G | backend | `backend/testing/test_deploy_identity.py` | 206 | 18 | under 1500 LOC | 0.079s measured | — |
| G | backend | `backend/testing/test_field_encryption.py` | 504 | 44 | under 1500 LOC | 0.078s measured | — |
| G | agent | `agent/src/token-file.test.ts` | 280 | 25 | under 1500 LOC | 0.078s measured | — |
| G | backend | `backend/testing/test_direction_health.py` | 431 | 28 | under 1500 LOC | 0.078s measured | — |
| G | backend | `backend/testing/test_admin_discovery_cycles.py` | 306 | 6 | under 1500 LOC | 0.077s measured | — |
| G | backend | `backend/testing/test_design_doc_grounding.py` | 771 | 30 | under 1500 LOC | 0.075s measured | — |
| G | agent | `agent/src/handlers/review-fix-migration.test.ts` | 272 | 20 | under 1500 LOC | 0.075s measured | — |
| G | admin-ui | `admin-ui/src/components/messageArming.test.ts` | 146 | 23 | under 1500 LOC | 0.075s measured | — |
| G | backend | `backend/testing/test_measure_repair_success.py` | 127 | 9 | under 1500 LOC | 0.075s measured | — |
| G | agent | `agent/src/handlers/code-change.test.ts` | 941 | 37 | under 1500 LOC | 0.073s measured | — |
| G | backend | `backend/testing/test_design_web_url_reachability.py` | 282 | 20 | under 1500 LOC | 0.073s measured | — |
| G | backend | `backend/testing/test_migration_env.py` | 197 | 13 | under 1500 LOC | 0.072s measured | — |
| G | backend | `backend/testing/test_direction_outcome_metric_manifest.py` | 155 | 5 | under 1500 LOC | 0.071s measured | — |
| G | backend | `backend/testing/test_namespace_backfill.py` | 230 | 13 | under 1500 LOC | 0.071s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useWorkbenchShortcutRouter.test.tsx` | 194 | 6 | under 1500 LOC | 0.071s measured | — |
| G | admin-ui | `admin-ui/src/components/RailMiniCanvas.test.tsx` | 191 | 24 | under 1500 LOC | 0.071s measured | — |
| G | admin-ui | `admin-ui/src/components/SystemStatusCard.test.tsx` | 117 | 9 | under 1500 LOC | 0.071s measured | — |
| G | backend | `backend/testing/test_github_commit_resolution.py` | 150 | 8 | under 1500 LOC | 0.070s measured | — |
| G | backend | `backend/testing/test_migration_0621_safe_encrypt_backfill.py` | 259 | 4 | under 1500 LOC | 0.069s measured | — |
| G | backend | `backend/testing/test_operator_failure_messages.py` | 444 | 27 | under 1500 LOC | 0.069s measured | — |
| G | agent | `agent/src/moduleSize.contract.test.ts` | 156 | 4 | under 1500 LOC | 0.068s measured | — |
| G | backend | `backend/testing/test_build_config_detection.py` | 608 | 11 | under 1500 LOC | 0.067s measured | — |
| G | admin-ui | `admin-ui/eslint-rules/no-surface-colour.test.js` | 82 | 0 | under 1500 LOC | 0.066s measured | — |
| G | admin-ui | `admin-ui/eslint-rules/require-workbench-focus.test.js` | 75 | 0 | under 1500 LOC | 0.066s measured | — |
| G | backend | `backend/testing/test_knowledge_completion.py` | 355 | 26 | under 1500 LOC | 0.066s measured | — |
| G | backend | `backend/testing/test_migration_0609_reconcile_encrypted_columns.py` | 337 | 8 | under 1500 LOC | 0.065s measured | — |
| G | admin-ui | `admin-ui/src/components/MediumScopeWidget.test.tsx` | 320 | 28 | under 1500 LOC | 0.065s measured | — |
| G | backend | `backend/testing/test_materialize_manifest.py` | 710 | 25 | under 1500 LOC | 0.063s measured | — |
| G | admin-ui | `admin-ui/src/components/StrategicSessionEvents.test.tsx` | 120 | 4 | under 1500 LOC | 0.062s measured | protected; declared floor 4 |
| G | backend | `backend/testing/test_direction_event_trail.py` | 319 | 21 | under 1500 LOC | 0.062s measured | — |
| G | admin-ui | `admin-ui/src/components/LlmCostBreakdownPanel.test.tsx` | 136 | 6 | under 1500 LOC | 0.061s measured | — |
| G | backend | `backend/testing/test_broadcast.py` | 300 | 36 | under 1500 LOC | 0.060s measured | — |
| G | backend | `backend/testing/test_deployed_sha.py` | 781 | 31 | under 1500 LOC | 0.059s measured | — |
| G | backend | `backend/testing/test_digest_payload_metric_manifest.py` | 256 | 6 | under 1500 LOC | 0.058s measured | — |
| G | agent | `agent/src/handlers/audits-reopen.test.ts` | 390 | 15 | under 1500 LOC | 0.057s measured | — |
| G | admin-ui | `admin-ui/src/components/SavedViewsTabStrip.test.tsx` | 88 | 8 | under 1500 LOC | 0.056s measured | — |
| G | agent | `agent/src/handlers/discovery-review.test.ts` | 529 | 17 | under 1500 LOC | 0.055s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useCommandPaletteShortcut.test.tsx` | 205 | 10 | under 1500 LOC | 0.054s measured | — |
| G | backend | `backend/testing/test_migration_0668_drop_projectcomputeimagebuild.py` | 218 | 3 | under 1500 LOC | 0.053s measured | — |
| G | backend | `backend/testing/test_llm_route_fallback.py` | 351 | 5 | under 1500 LOC | 0.052s measured | — |
| G | admin-ui | `admin-ui/src/components/EvidenceTierBadge.test.tsx` | 98 | 16 | under 1500 LOC | 0.051s measured | — |
| G | backend | `backend/testing/test_json_extract.py` | 258 | 30 | under 1500 LOC | 0.051s measured | — |
| G | backend | `backend/testing/test_capability_execution_fence.py` | 329 | 19 | under 1500 LOC | 0.050s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useOperatorKeyboardShortcuts.test.tsx` | 355 | 17 | under 1500 LOC | 0.049s measured | — |
| G | admin-ui | `admin-ui/src/components/ConversationUtilityBar.test.tsx` | 891 | 27 | under 1500 LOC | 0.048s measured | protected; declared floor 27 |
| G | admin-ui | `admin-ui/src/components/SystemHealthCard.test.tsx` | 139 | 11 | under 1500 LOC | 0.048s measured | — |
| G | agent | `agent/src/utils/test-fix-loop.test.ts` | 1,376 | 56 | under 1500 LOC | 0.047s measured | — |
| G | admin-ui | `admin-ui/src/components/ReleaseEvidence.test.tsx` | 186 | 16 | under 1500 LOC | 0.047s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useProjectMembers.test.tsx` | 92 | 3 | under 1500 LOC | 0.047s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useStrategicReadModel.test.tsx` | 414 | 17 | under 1500 LOC | 0.046s measured | — |
| G | admin-ui | `admin-ui/eslint-rules/canonical-radius.test.js` | 66 | 0 | under 1500 LOC | 0.046s measured | — |
| G | admin-ui | `admin-ui/src/components/ui/Badge.test.tsx` | 110 | 18 | under 1500 LOC | 0.045s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectReadOnlyEditors.test.tsx` | 95 | 2 | under 1500 LOC | 0.045s measured | — |
| G | admin-ui | `admin-ui/src/components/WhiteboardRailSection.test.tsx` | 91 | 5 | under 1500 LOC | 0.045s measured | — |
| G | admin-ui | `admin-ui/src/components/ParkWakeHealthCard.test.tsx` | 61 | 6 | under 1500 LOC | 0.045s measured | — |
| G | backend | `backend/testing/test_dispatch_ordering.py` | 578 | 29 | under 1500 LOC | 0.045s measured | — |
| G | backend | `backend/testing/test_config_sync.py` | 486 | 22 | under 1500 LOC | 0.043s measured | — |
| G | backend | `backend/testing/test_admin_task_mutations_import.py` | 85 | 2 | under 1500 LOC | 0.043s measured | — |
| G | backend | `backend/testing/test_failure_reason.py` | 152 | 9 | under 1500 LOC | 0.042s measured | — |
| G | admin-ui | `admin-ui/src/components/DirectionsPanel.readOnly.test.tsx` | 124 | 1 | under 1500 LOC | 0.042s measured | — |
| G | admin-ui | `admin-ui/src/components/StrategicChallengeChip.test.tsx` | 71 | 7 | under 1500 LOC | 0.042s measured | — |
| G | backend | `backend/testing/test_context_bounds.py` | 302 | 21 | under 1500 LOC | 0.041s measured | — |
| G | backend | `backend/testing/test_direction_metrics.py` | 410 | 31 | under 1500 LOC | 0.040s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectResourceAvailabilityCard.test.tsx` | 75 | 3 | under 1500 LOC | 0.040s measured | — |
| G | admin-ui | `admin-ui/eslint-rules/no-disabled-react-hooks.test.js` | 74 | 1 | under 1500 LOC | 0.040s measured | — |
| G | admin-ui | `admin-ui/src/components/ToolResidentHandoffControls.test.tsx` | 48 | 3 | under 1500 LOC | 0.040s measured | protected; declared floor 3 |
| G | agent | `agent/src/call-speech/call-speech.test.ts` | 309 | 13 | under 1500 LOC | 0.039s measured | — |
| G | backend | `backend/testing/test_strategic_payload_size.py` | 547 | 6 | under 1500 LOC | 0.038s measured | — |
| G | backend | `backend/testing/test_release_operator_commit_gate.py` | 181 | 6 | under 1500 LOC | 0.037s measured | — |
| G | agent | `agent/src/prompt.context.test.ts` | 1,434 | 116 | under 1500 LOC | 0.037s measured | — |
| G | agent | `agent/src/handlers/index.test.ts` | 635 | 102 | under 1500 LOC | 0.037s measured | protected; declared floor 102 |
| G | admin-ui | `admin-ui/src/hooks/usePoller.test.ts` | 187 | 11 | under 1500 LOC | 0.037s measured | — |
| G | admin-ui | `admin-ui/src/components/CompletionDebtPanel.test.tsx` | 48 | 1 | under 1500 LOC | 0.037s measured | — |
| G | agent | `agent/src/handlers/prior-knowledge.test.ts` | 313 | 27 | under 1500 LOC | 0.036s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useUrlFilterState.test.ts` | 149 | 14 | under 1500 LOC | 0.036s measured | — |
| G | agent | `agent/src/prompts/compute-progress-contract.test.ts` | 218 | 14 | under 1500 LOC | 0.035s measured | — |
| G | backend | `backend/testing/test_storage_signed_url.py` | 124 | 4 | under 1500 LOC | 0.035s measured | — |
| G | admin-ui | `admin-ui/src/components/MergeGateFleetHealthCard.test.tsx` | 77 | 7 | under 1500 LOC | 0.033s measured | — |
| G | backend | `backend/testing/test_build_config_lint.py` | 230 | 22 | under 1500 LOC | 0.033s measured | — |
| G | admin-ui | `admin-ui/src/components/ProjectToolIframe.test.tsx` | 95 | 2 | under 1500 LOC | 0.032s measured | — |
| G | backend | `backend/testing/test_release_ownership.py` | 195 | 4 | under 1500 LOC | 0.032s measured | — |
| G | backend | `backend/testing/test_cochange_decomposition.py` | 277 | 20 | under 1500 LOC | 0.031s measured | — |
| G | agent | `agent/src/utils/provisioning.test.ts` | 402 | 40 | under 1500 LOC | 0.031s measured | — |
| G | agent | `agent/src/utils/design-doc-presence.test.ts` | 330 | 25 | under 1500 LOC | 0.031s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useSearchFilters.test.ts` | 197 | 22 | under 1500 LOC | 0.031s measured | — |
| G | backend | `backend/testing/test_cors_on_errors.py` | 141 | 6 | under 1500 LOC | 0.030s measured | — |
| G | admin-ui | `admin-ui/src/hooks/dashboardAccessGating.test.tsx` | 97 | 2 | under 1500 LOC | 0.030s measured | — |
| G | admin-ui | `admin-ui/src/components/PipelineEfficiencyPanel.test.tsx` | 81 | 4 | under 1500 LOC | 0.030s measured | — |
| G | backend | `backend/testing/test_strategic_op_contracts.py` | 179 | 9 | under 1500 LOC | 0.030s measured | — |
| G | agent | `agent/src/prompts/context-prefix.test.ts` | 1,205 | 69 | under 1500 LOC | 0.029s measured | — |
| G | admin-ui | `admin-ui/src/components/useMessageList.test.ts` | 223 | 14 | under 1500 LOC | 0.029s measured | — |
| G | admin-ui | `admin-ui/src/utils/taskCsv.test.ts` | 125 | 6 | under 1500 LOC | 0.029s measured | — |
| G | backend | `backend/testing/test_project_outcome_projection.py` | 277 | 7 | under 1500 LOC | 0.029s measured | — |
| G | backend | `backend/testing/test_merge_size_limits.py` | 166 | 13 | under 1500 LOC | 0.029s measured | — |
| G | backend | `backend/testing/test_direction_terminal_guard.py` | 80 | 5 | under 1500 LOC | 0.028s measured | — |
| G | admin-ui | `admin-ui/src/test/viewport-diff.test.ts` | 98 | 4 | under 1500 LOC | 0.028s measured | — |
| G | admin-ui | `admin-ui/src/hooks/usePersistedState.test.ts` | 87 | 9 | under 1500 LOC | 0.028s measured | — |
| G | agent | `agent/src/prompts/check-helper.fixture-deps.test.ts` | 77 | 4 | under 1500 LOC | 0.028s measured | — |
| G | backend | `backend/testing/test_compute_execution_documents.py` | 130 | 4 | under 1500 LOC | 0.027s measured | — |
| G | agent | `agent/src/handlers/design.test.ts` | 864 | 36 | under 1500 LOC | 0.027s measured | — |
| G | agent | `agent/src/handlers/walkthrough-e2e.test.ts` | 746 | 30 | under 1500 LOC | 0.027s measured | — |
| G | agent | `agent/src/media/livekit-call-media.test.ts` | 382 | 13 | under 1500 LOC | 0.027s measured | — |
| G | admin-ui | `admin-ui/src/components/SearchFilterBar.test.tsx` | 23 | 1 | under 1500 LOC | 0.027s measured | — |
| G | backend | `backend/testing/test_strategic_correctness_preflight.py` | 150 | 2 | under 1500 LOC | 0.027s measured | — |
| G | backend | `backend/testing/test_local_app_release.py` | 134 | 3 | under 1500 LOC | 0.026s measured | — |
| G | admin-ui | `admin-ui/src/components/AnomalyThrottleBanner.test.tsx` | 54 | 3 | under 1500 LOC | 0.026s measured | — |
| G | agent | `agent/src/reset.test.ts` | 722 | 32 | under 1500 LOC | 0.025s measured | — |
| G | agent | `agent/src/prompt-helper-refs.test.ts` | 326 | 5 | under 1500 LOC | 0.025s measured | — |
| G | admin-ui | `admin-ui/src/utils/strategicWorkSessionMetrics.test.ts` | 40 | 2 | under 1500 LOC | 0.025s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useVersionLifecycle.test.tsx` | 33 | 1 | under 1500 LOC | 0.025s measured | — |
| G | backend | `backend/testing/test_notification.py` | 134 | 5 | under 1500 LOC | 0.025s measured | — |
| G | backend | `backend/testing/test_voice_credentials.py` | 178 | 7 | under 1500 LOC | 0.025s measured | — |
| G | backend | `backend/testing/test_c18_check_runs_cache_error_bypass.py` | 330 | 8 | under 1500 LOC | 0.025s measured | — |
| G | backend | `backend/testing/test_branch_resolution.py` | 196 | 15 | under 1500 LOC | 0.024s measured | — |
| G | backend | `backend/testing/test_strategic_workday_mutation_rebind.py` | 148 | 2 | under 1500 LOC | 0.023s measured | protected; declared floor 2 |
| G | backend | `backend/testing/test_livekit_tokens.py` | 143 | 5 | under 1500 LOC | 0.023s measured | — |
| G | backend | `backend/testing/test_github_checks.py` | 670 | 24 | under 1500 LOC | 0.023s measured | — |
| G | backend | `backend/testing/test_pr_is_not_behind_counter.py` | 224 | 13 | under 1500 LOC | 0.023s measured | — |
| G | agent | `agent/src/handlers/memo.test.ts` | 850 | 40 | under 1500 LOC | 0.023s measured | — |
| G | admin-ui | `admin-ui/src/__tests__/strategicReconnect.test.tsx` | 160 | 2 | under 1500 LOC | 0.023s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useConversationSend.test.tsx` | 85 | 5 | under 1500 LOC | 0.023s measured | — |
| G | backend | `backend/testing/test_strategic_memory_invariants.py` | 182 | 7 | under 1500 LOC | 0.022s measured | — |
| G | backend | `backend/testing/test_migration_0273_normalise_indexes.py` | 158 | 5 | under 1500 LOC | 0.022s measured | — |
| G | agent | `agent/src/providers/resident-stream.test.ts` | 380 | 45 | under 1500 LOC | 0.022s measured | protected; declared floor 45 |
| G | admin-ui | `admin-ui/src/hooks/useStrategicContextNavigation.test.tsx` | 253 | 7 | under 1500 LOC | 0.022s measured | — |
| G | backend | `backend/testing/test_migration_0531_backfill_encryption.py` | 203 | 2 | under 1500 LOC | 0.022s measured | — |
| G | backend | `backend/testing/test_direction_mode.py` | 126 | 12 | under 1500 LOC | 0.021s measured | — |
| G | backend | `backend/testing/test_migration_0141_reconcile.py` | 121 | 3 | under 1500 LOC | 0.021s measured | — |
| G | backend | `backend/testing/test_migration_0719_validate_task_context_size_constraint.py` | 57 | 4 | under 1500 LOC | 0.021s measured | — |
| G | backend | `backend/testing/test_migration_path.py` | 72 | 10 | under 1500 LOC | 0.021s measured | — |
| G | agent | `agent/src/telemetry.test.ts` | 773 | 74 | under 1500 LOC | 0.021s measured | — |
| G | agent | `agent/src/handlers/web-research.test.ts` | 653 | 28 | under 1500 LOC | 0.021s measured | — |
| G | backend | `backend/testing/test_migration_reconciliation.py` | 111 | 7 | under 1500 LOC | 0.021s measured | — |
| G | agent | `agent/src/handlers/design-doc.test.ts` | 325 | 19 | under 1500 LOC | 0.020s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useStrategistStatus.test.ts` | 120 | 10 | under 1500 LOC | 0.020s measured | — |
| G | agent | `agent/src/utils/classifier-fixtures.test.ts` | 110 | 6 | under 1500 LOC | 0.020s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useProjectConfig.test.tsx` | 68 | 2 | under 1500 LOC | 0.020s measured | — |
| G | admin-ui | `admin-ui/src/strategicStateApi.test.ts` | 562 | 28 | under 1500 LOC | 0.019s measured | — |
| G | admin-ui | `admin-ui/src/utils/residentToolHandoff.test.ts` | 129 | 7 | under 1500 LOC | 0.019s measured | protected; declared floor 7 |
| G | admin-ui | `admin-ui/src/hooks/useViewPreferences.test.ts` | 40 | 5 | under 1500 LOC | 0.019s measured | — |
| G | backend | `backend/testing/test_release_prepromotion_rehearsal_contracts.py` | 99 | 5 | under 1500 LOC | 0.019s measured | — |
| G | backend | `backend/testing/test_account_model_regression_stats.py` | 148 | 12 | under 1500 LOC | 0.019s measured | — |
| G | agent | `agent/src/utils/agent-token.test.ts` | 312 | 21 | under 1500 LOC | 0.018s measured | — |
| G | agent | `agent/src/providers/foreground-resident.test.ts` | 175 | 9 | under 1500 LOC | 0.018s measured | — |
| G | agent | `agent/src/handlers/artifacts-helper.test.ts` | 151 | 4 | under 1500 LOC | 0.018s measured | — |
| G | backend | `backend/testing/test_migration_0532_message_backlog.py` | 190 | 3 | under 1500 LOC | 0.018s measured | — |
| G | backend | `backend/testing/test_message_audience_routing.py` | 165 | 10 | under 1500 LOC | 0.018s measured | — |
| G | backend | `backend/testing/test_ci_infra_rerun.py` | 104 | 10 | under 1500 LOC | 0.017s measured | — |
| G | agent | `agent/src/handlers/structured-output-repair.test.ts` | 732 | 32 | under 1500 LOC | 0.017s measured | — |
| G | admin-ui | `admin-ui/src/utils/strategicContextDescriptor.test.ts` | 343 | 10 | under 1500 LOC | 0.017s measured | — |
| G | admin-ui | `admin-ui/src/components/SrBadges.test.tsx` | 43 | 1 | under 1500 LOC | 0.017s measured | — |
| G | backend | `backend/testing/test_vm_lifecycle_runner_doomed_marker.py` | 89 | 3 | under 1500 LOC | 0.016s measured | — |
| G | backend | `backend/testing/test_classifier_fixtures.py` | 255 | 10 | under 1500 LOC | 0.016s measured | — |
| G | agent | `agent/src/handlers/capability-proof-trial.test.ts` | 404 | 27 | under 1500 LOC | 0.016s measured | — |
| G | backend | `backend/testing/test_research_actionability.py` | 204 | 5 | under 1500 LOC | 0.016s measured | — |
| G | backend | `backend/testing/test_migration_0151_reconcile.py` | 98 | 5 | under 1500 LOC | 0.015s measured | — |
| G | backend | `backend/testing/test_orchestrator_health_authority.py` | 32 | 3 | under 1500 LOC | 0.015s measured | — |
| G | agent | `agent/src/prompts/discovery.test.ts` | 668 | 45 | under 1500 LOC | 0.015s measured | — |
| G | agent | `agent/src/handlers/review-fix-derived-conflicts.test.ts` | 210 | 17 | under 1500 LOC | 0.015s measured | — |
| G | agent | `agent/src/json-extract.test.ts` | 176 | 29 | under 1500 LOC | 0.015s measured | — |
| G | admin-ui | `admin-ui/src/utils/directionHelpers.test.ts` | 140 | 17 | under 1500 LOC | 0.015s measured | — |
| G | backend | `backend/testing/test_leader_lock_keepalive.py` | 149 | 6 | under 1500 LOC | 0.015s measured | — |
| G | backend | `backend/testing/test_release_artifact_freshness.py` | 150 | 5 | under 1500 LOC | 0.014s measured | — |
| G | agent | `agent/src/handlers/criterion-named-test-trial.test.ts` | 376 | 17 | under 1500 LOC | 0.014s measured | — |
| G | admin-ui | `admin-ui/src/utils/mergeTestVerdict.test.ts` | 374 | 36 | under 1500 LOC | 0.014s measured | — |
| G | admin-ui | `admin-ui/src/components/ComputeJobsPanel.utils.test.ts` | 217 | 19 | under 1500 LOC | 0.014s measured | — |
| G | admin-ui | `admin-ui/src/utils/citationLinks.test.ts` | 77 | 13 | under 1500 LOC | 0.014s measured | — |
| G | backend | `backend/testing/test_execution_capability_requirements.py` | 126 | 4 | under 1500 LOC | 0.014s measured | — |
| G | backend | `backend/testing/test_release_activation.py` | 84 | 2 | under 1500 LOC | 0.014s measured | — |
| G | backend | `backend/testing/test_release_qualifier.py` | 124 | 5 | under 1500 LOC | 0.013s measured | — |
| G | backend | `backend/testing/test_event_type_invariants.py` | 320 | 7 | under 1500 LOC | 0.013s measured | — |
| G | admin-ui | `admin-ui/src/operatorWorkbenchContracts.test.ts` | 551 | 3 | under 1500 LOC | 0.013s measured | — |
| G | agent | `agent/src/handlers/walkthrough-criterion-gates.test.ts` | 463 | 40 | under 1500 LOC | 0.013s measured | — |
| G | agent | `agent/src/handlers/verify-tests.setup-retry.test.ts` | 175 | 5 | under 1500 LOC | 0.013s measured | — |
| G | backend | `backend/testing/test_migration_0472_no_double_encrypt.py` | 147 | 3 | under 1500 LOC | 0.012s measured | — |
| G | admin-ui | `admin-ui/src/utils/taskStatus.test.ts` | 102 | 24 | under 1500 LOC | 0.012s measured | — |
| G | agent | `agent/src/handlers/research-plan-prompt-blocks.test.ts` | 96 | 10 | under 1500 LOC | 0.012s measured | — |
| G | admin-ui | `admin-ui/src/utils/mathContent.test.ts` | 59 | 12 | under 1500 LOC | 0.012s measured | — |
| G | admin-ui | `admin-ui/src/utils/strategicSessionLive.test.ts` | 686 | 44 | under 1500 LOC | 0.011s measured | protected; declared floor 44 |
| G | agent | `agent/src/build-config.test.ts` | 546 | 46 | under 1500 LOC | 0.011s measured | — |
| G | agent | `agent/src/handlers/research-direction-context.test.ts` | 392 | 36 | under 1500 LOC | 0.011s measured | — |
| G | agent | `agent/src/handlers/strategic-ops-registry.test.ts` | 199 | 13 | under 1500 LOC | 0.011s measured | — |
| G | agent | `agent/src/providers/usage.test.ts` | 147 | 10 | under 1500 LOC | 0.011s measured | — |
| G | agent | `agent/src/processTracking.test.ts` | 144 | 4 | under 1500 LOC | 0.011s measured | — |
| G | admin-ui | `admin-ui/src/components/boardSavedViews.test.ts` | 131 | 11 | under 1500 LOC | 0.011s measured | — |
| G | backend | `backend/testing/test_compute_outcome_status.py` | 34 | 2 | under 1500 LOC | 0.010s measured | — |
| G | admin-ui | `admin-ui/src/utils/pendingWhiteboardArtifactRef.test.ts` | 222 | 6 | under 1500 LOC | 0.010s measured | protected; declared floor 6 |
| G | admin-ui | `admin-ui/src/accountManager.test.ts` | 191 | 9 | under 1500 LOC | 0.010s measured | — |
| G | agent | `agent/src/handlers/conversation-message-streamer.test.ts` | 183 | 6 | under 1500 LOC | 0.010s measured | — |
| G | admin-ui | `admin-ui/src/utils/parseTimestamp-guard.test.ts` | 155 | 15 | under 1500 LOC | 0.010s measured | — |
| G | backend | `backend/testing/test_compute_cloud_run.py` | 968 | 13 | under 1500 LOC | 0.009s measured | — |
| G | agent | `agent/src/handlers/compute-experiment.test.ts` | 253 | 12 | under 1500 LOC | 0.009s measured | — |
| G | admin-ui | `admin-ui/src/components/MessagesPanel.invariant.test.ts` | 212 | 9 | under 1500 LOC | 0.009s measured | — |
| G | agent | `agent/src/oauth-rotation.test.ts` | 205 | 10 | under 1500 LOC | 0.009s measured | — |
| G | admin-ui | `admin-ui/src/App.wiring.test.ts` | 124 | 14 | under 1500 LOC | 0.009s measured | protected; declared floor 14 |
| G | agent | `agent/src/taskCapabilities.test.ts` | 114 | 10 | under 1500 LOC | 0.009s measured | — |
| G | admin-ui | `admin-ui/src/utils/pendingAttachmentStore.test.ts` | 77 | 3 | under 1500 LOC | 0.009s measured | protected; declared floor 3 |
| G | admin-ui | `admin-ui/src/utils/strategicWorkbenchSession.test.ts` | 66 | 4 | under 1500 LOC | 0.009s measured | — |
| G | backend | `backend/testing/test_agent_memory_consolidation.py` | 43 | 2 | under 1500 LOC | 0.009s measured | — |
| G | backend | `backend/testing/test_migration_0457_encrypt_github_pat.py` | 82 | 2 | under 1500 LOC | 0.009s measured | — |
| G | backend | `backend/testing/test_github_oauth_origins.py` | 52 | 3 | under 1500 LOC | 0.009s measured | — |
| G | backend | `backend/testing/test_strategic_resident_activity.py` | 85 | 3 | under 1500 LOC | 0.008s measured | — |
| G | agent | `agent/src/prompts/project-context.test.ts` | 409 | 23 | under 1500 LOC | 0.008s measured | — |
| G | admin-ui | `admin-ui/src/components/eventPrefs.test.ts` | 320 | 40 | under 1500 LOC | 0.008s measured | — |
| G | agent | `agent/src/handlers/verify-tests.provision.test.ts` | 159 | 4 | under 1500 LOC | 0.008s measured | — |
| G | admin-ui | `admin-ui/src/utils/conversationDraftState.test.ts` | 111 | 6 | under 1500 LOC | 0.008s measured | — |
| G | agent | `agent/src/fix-loop-events.test.ts` | 258 | 20 | under 1500 LOC | 0.007s measured | — |
| G | admin-ui | `admin-ui/src/utils/outcomeKinds.test.ts` | 185 | 19 | under 1500 LOC | 0.007s measured | — |
| G | admin-ui | `admin-ui/src/utils/sidecarTasks.test.ts` | 173 | 15 | under 1500 LOC | 0.007s measured | — |
| G | agent | `agent/src/handlers/strategic-stall-advisory.test.ts` | 48 | 3 | under 1500 LOC | 0.007s measured | — |
| G | backend | `backend/testing/test_migration_0639_align_projectcomputeimagebuild_timestamptz.py` | 105 | 1 | under 1500 LOC | 0.007s measured | — |
| G | backend | `backend/testing/test_shared_state_facade.py` | 191 | 4 | under 1500 LOC | 0.007s measured | — |
| G | backend | `backend/testing/test_strategic_stall_visibility.py` | 71 | 2 | under 1500 LOC | 0.006s measured | — |
| G | backend | `backend/testing/test_branch_cleanup.py` | 50 | 3 | under 1500 LOC | 0.006s measured | — |
| G | agent | `agent/src/handlers/direction-renderer.test.ts` | 252 | 20 | under 1500 LOC | 0.006s measured | — |
| G | agent | `agent/src/handlers/strategic-context-envelope.test.ts` | 224 | 7 | under 1500 LOC | 0.006s measured | — |
| G | agent | `agent/src/utils/viewport-shot-download.test.ts` | 140 | 11 | under 1500 LOC | 0.006s measured | — |
| G | admin-ui | `admin-ui/src/taskCapabilities.test.ts` | 136 | 11 | under 1500 LOC | 0.006s measured | — |
| G | admin-ui | `admin-ui/src/utils/statusFilter.test.ts` | 49 | 7 | under 1500 LOC | 0.006s measured | — |
| G | backend | `backend/testing/test_compute_provision_slots.py` | 49 | 2 | under 1500 LOC | 0.006s measured | — |
| G | agent | `agent/src/handlers/review-fix-history.test.ts` | 184 | 21 | under 1500 LOC | 0.005s measured | — |
| G | admin-ui | `admin-ui/src/test/composition-root-wiring.test.ts` | 168 | 5 | under 1500 LOC | 0.005s measured | — |
| G | admin-ui | `admin-ui/src/api/projectTools.residentArtifact.test.ts` | 135 | 5 | under 1500 LOC | 0.005s measured | protected; declared floor 5 |
| G | admin-ui | `admin-ui/src/test/orphan-component-guard.test.ts` | 129 | 2 | under 1500 LOC | 0.005s measured | — |
| G | agent | `agent/src/handlers/verify-tests.setup-probe.test.ts` | 115 | 2 | under 1500 LOC | 0.005s measured | — |
| G | admin-ui | `admin-ui/src/utils/unverifiedReasonLabels.test.ts` | 100 | 13 | under 1500 LOC | 0.005s measured | — |
| G | admin-ui | `admin-ui/src/utils/newProjectOAuthResume.test.ts` | 84 | 5 | under 1500 LOC | 0.005s measured | — |
| G | admin-ui | `admin-ui/src/utils/verdictFormatting.test.ts` | 63 | 9 | under 1500 LOC | 0.005s measured | — |
| G | admin-ui | `admin-ui/src/utils/externalUrl.test.ts` | 57 | 12 | under 1500 LOC | 0.005s measured | — |
| G | agent | `agent/src/handlers/discovery-review-workspace.test.ts` | 44 | 2 | under 1500 LOC | 0.005s measured | — |
| G | backend | `backend/testing/test_admin_auth_invariant.py` | 100 | 1 | under 1500 LOC | 0.005s measured | — |
| G | agent | `agent/src/handlers/research-plan-contract.test.ts` | 198 | 11 | under 1500 LOC | 0.004s measured | — |
| G | agent | `agent/src/reset-invariant.test.ts` | 152 | 9 | under 1500 LOC | 0.004s measured | — |
| G | agent | `agent/src/heartbeat-classify.test.ts` | 104 | 13 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/App.test.tsx` | 83 | 4 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/types/workbenchEvents.test.ts` | 77 | 4 | under 1500 LOC | 0.004s measured | — |
| G | agent | `agent/src/telemetry.prompt_size.test.ts` | 77 | 3 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/components/FleetRunnersPanel.utils.test.ts` | 58 | 5 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/utils/idempotencyKey.test.ts` | 56 | 5 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/utils/fixLoopLayers.test.ts` | 40 | 6 | under 1500 LOC | 0.004s measured | — |
| G | agent | `agent/src/handlers/strategic-resident-prompt.test.ts` | 39 | 3 | under 1500 LOC | 0.004s measured | protected; declared floor 3 |
| G | admin-ui | `admin-ui/src/test/workbench-theme-tokens.test.ts` | 32 | 1 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/utils/time.test.ts` | 30 | 5 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/appWorkbenchConstants.test.ts` | 27 | 3 | under 1500 LOC | 0.004s measured | — |
| G | admin-ui | `admin-ui/src/utils/projectSelection.test.ts` | 27 | 2 | under 1500 LOC | 0.004s measured | — |
| G | backend | `backend/testing/test_claim_analytics.py` | 40 | 2 | under 1500 LOC | 0.004s measured | — |
| G | backend | `backend/testing/test_docs_runbook_drift.py` | 274 | 8 | under 1500 LOC | 0.004s measured | — |
| G | agent | `agent/src/handlers/strategic-resident-wake.test.ts` | 173 | 9 | under 1500 LOC | 0.003s measured | — |
| G | agent | `agent/src/workspaceDisk.test.ts` | 111 | 6 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/utils/computeAnalysisLabels.test.ts` | 96 | 12 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/utils/taskGraph.test.ts` | 94 | 11 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/accessControl.test.ts` | 91 | 4 | under 1500 LOC | 0.003s measured | — |
| G | agent | `agent/src/handlers/structured-repair.test.ts` | 88 | 3 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/utils/mergeGateSummary.test.ts` | 82 | 7 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/utils/imageEdits.test.ts` | 72 | 10 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/types/workbenchTargets.test.ts` | 71 | 4 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/components/projectToolHidden.test.ts` | 47 | 6 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/utils/conversationAttachmentDraftFinalization.test.ts` | 42 | 1 | under 1500 LOC | 0.003s measured | — |
| G | admin-ui | `admin-ui/src/utils/conversationMessageIdentity.test.ts` | 24 | 2 | under 1500 LOC | 0.003s measured | — |
| G | backend | `backend/testing/test_failure_classification_retained.py` | 11 | 1 | under 1500 LOC | 0.003s measured | — |
| G | backend | `backend/testing/test_local_runner_terminal_log.py` | 151 | 1 | under 1500 LOC | 0.002s measured | — |
| G | agent | `agent/src/branch-literal-regression.test.ts` | 122 | 3 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/qualificationStateVocab.contract.test.ts` | 77 | 4 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/test/viewport-capture-config.test.ts` | 57 | 5 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/hooks/useUIState.test.ts` | 41 | 3 | under 1500 LOC | 0.002s measured | — |
| G | agent | `agent/src/qualificationStateVocab.contract.test.ts` | 41 | 2 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/utils/strategicAttention.test.ts` | 39 | 3 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/utils/convergenceVerdict.test.ts` | 35 | 4 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/utils/conversationSendState.test.ts` | 30 | 1 | under 1500 LOC | 0.002s measured | — |
| G | agent | `agent/src/utils/sanitize.test.ts` | 17 | 3 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/useAppState.selection.test.ts` | 15 | 2 | under 1500 LOC | 0.002s measured | — |
| G | admin-ui | `admin-ui/src/branch-literal-regression.test.ts` | 93 | 3 | under 1500 LOC | 0.001s measured | — |
| G | admin-ui | `admin-ui/src/utils/failureClass.test.ts` | 43 | 4 | under 1500 LOC | 0.001s measured | — |
| G | agent | `agent/src/_agent_scripts/materialize_manifest.parity.test.ts` | 34 | 1 | under 1500 LOC | 0.001s measured | — |
| G | agent | `agent/src/_agent_scripts/fixgate_attribution.parity.test.ts` | 31 | 1 | under 1500 LOC | 0.001s measured | — |
| G | admin-ui | `admin-ui/src/utils/composerRequestOwnership.test.ts` | 28 | 2 | under 1500 LOC | 0.001s measured | — |
| G | admin-ui | `admin-ui/src/test/environment.test.ts` | 15 | 1 | under 1500 LOC | 0.001s measured | — |
| G | admin-ui | `admin-ui/src/components/AdversarialQualityPanel.test.tsx` | 9 | 1 | under 1500 LOC | 0.001s measured | — |
| G | backend | `backend/testing/test_runner_heartbeat.py` | 1,411 | 34 | under 1500 LOC | 0.000s measured | — |
| G | agent | `agent/src/handlers/verify-tests.integration.test.ts` | 134 | 1 | under 1500 LOC | 0.000s measured | — |
| G | agent | `agent/src/prompts/diagnostics-after-edit-hook.test.ts` | 77 | 0 | under 1500 LOC | 0.000s measured | — |
| G | admin-ui-viewport | `admin-ui/src/components/VersionSummaryPanel.viewport.test.tsx` | 1,049 | 43 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SrUnhandledEdgesRetrospectivesPanel.viewport.test.tsx` | 927 | 21 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ComputeJobsPanel.viewport.test.tsx` | 875 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MessagesPanel.viewport.test.tsx` | 786 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/VersionLifecyclePanel.viewport.test.tsx` | 763 | 22 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProblemModelViewer.viewport.test.tsx` | 728 | 19 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DashboardHome.viewport.test.tsx` | 718 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectToolHost.viewport.test.tsx` | 689 | 20 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/NewTaskModal.viewport.test.tsx` | 688 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MessagesListView.viewport.test.tsx` | 648 | 33 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 33 |
| G | backend | `backend/testing/test_lifecycle_integration.py` | 634 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WorkbenchComposition.viewport.test.tsx` | 590 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StrategicContextRail.viewport.test.tsx` | 570 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SettingsDeploySection.viewport.test.tsx` | 563 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DirectionCard.viewport.test.tsx` | 503 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerMeta.viewport.test.tsx` | 483 | 14 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/HeaderBar.viewport.test.tsx` | 479 | 21 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/KnowledgeBrowser.viewport.test.tsx` | 443 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ActivityFeed.viewport.test.tsx` | 422 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MessagesComposer.viewport.test.tsx` | 386 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/PipelineWorkspace.viewport.test.tsx` | 348 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WarmVmPanel.viewport.test.tsx` | 342 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/NewProjectModal.viewport.test.tsx` | 325 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SrTrendBreakdown.viewport.test.tsx` | 323 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/LlmCostBreakdownPanel.viewport.test.tsx` | 305 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StrategicContextArtifactSlot.viewport.test.tsx` | 305 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectBuildConfigSection.viewport.test.tsx` | 297 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SettingsSectionRouter.viewport.test.tsx` | 294 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MetricsDashboard.viewport.test.tsx` | 291 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DirectionsPanel.viewport.test.tsx` | 290 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawer.viewport.test.tsx` | 285 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/RunningUpdatePanel.viewport.test.tsx` | 277 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MessageBubble.viewport.test.tsx` | 272 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerFailureDetail.viewport.test.tsx` | 272 | 11 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StrategicContextWorkspace.viewport.test.tsx` | 254 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/NotesTool.viewport.test.tsx` | 253 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/RemediationFunnelSection.viewport.test.tsx` | 246 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/FleetRunnersPanel.viewport.test.tsx` | 243 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WhiteboardTool.viewport.test.tsx` | 237 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/AutonomousStatus.viewport.test.tsx` | 234 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ResearchBrowser.viewport.test.tsx` | 234 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ViewportDebtPanel.viewport.test.tsx` | 223 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/LaneDiagPanel.viewport.test.tsx` | 222 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/PRMonitorSection.viewport.test.tsx` | 219 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskCard.viewport.test.tsx` | 214 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/OrchestratorHealthBar.viewport.test.tsx` | 212 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/KanbanBoard.viewport.test.tsx` | 211 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SettingsNotificationsSection.viewport.test.tsx` | 210 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ConversationUtilityBar.viewport.test.tsx` | 202 | 8 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 8 |
| G | admin-ui-viewport | `admin-ui/src/components/PipelineView.viewport.test.tsx` | 197 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProviderModelBreakdownPanel.viewport.test.tsx` | 192 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SearchFilterBar.viewport.test.tsx` | 192 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/BulkActionBar.viewport.test.tsx` | 191 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/KanbanColumn.viewport.test.tsx` | 183 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProblemModelEditor.viewport.test.tsx` | 178 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ConfirmDialog.viewport.test.tsx` | 175 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/EffortEfficiencyPanel.viewport.test.tsx` | 173 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MergeVerificationBanner.viewport.test.tsx` | 167 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskComputeJobsSection.viewport.test.tsx` | 165 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SettingsAccountsSection.viewport.test.tsx` | 163 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/test/kanban-composed.viewport.test.tsx` | 158 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/VersionMismatchBanner.viewport.test.tsx` | 155 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/RuntimeWorkspace.viewport.test.tsx` | 154 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ArchiveView.viewport.test.tsx` | 153 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/OuterReviewLoopConvergenceWidget.viewport.test.tsx` | 153 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/RecentErrorsWidget.viewport.test.tsx` | 151 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/AlertsBanner.viewport.test.tsx` | 149 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ConsoleHome.viewport.test.tsx` | 147 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DriftSignalsCard.viewport.test.tsx` | 146 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WorkbenchLayout.viewport.test.tsx` | 144 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerLogs.viewport.test.tsx` | 142 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/FleetHealthCard.viewport.test.tsx` | 140 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/FixLoopConvergenceWidget.viewport.test.tsx` | 136 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ReleaseQualificationFindings.viewport.test.tsx` | 133 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DirectionPortfolioSection.viewport.test.tsx` | 129 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MediumScopeWidget.viewport.test.tsx` | 127 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerDiagnostics.viewport.test.tsx` | 126 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskStreamPanel.viewport.test.tsx` | 126 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectIntegrationBranchSection.viewport.test.tsx` | 125 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ImprovementLanesPanel.viewport.test.tsx` | 123 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/CompactJobsTable.viewport.test.tsx` | 121 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/test/pipeline-composed.viewport.test.tsx` | 119 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerSelfReview.viewport.test.tsx` | 117 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/Pagination.viewport.test.tsx` | 116 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectManagementModal.viewport.test.tsx` | 116 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StrategicAuditPanel.viewport.test.tsx` | 115 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProviderSettingsPanel.viewport.test.tsx` | 114 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectBranchesSection.viewport.test.tsx` | 113 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectMembers.viewport.test.tsx` | 108 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/RuntimeAccountsPanel.viewport.test.tsx` | 108 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/test/messages-composed.viewport.test.tsx` | 108 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MessageTypeDropdown.viewport.test.tsx` | 106 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectToolIframe.viewport.test.tsx` | 106 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ComputeArtifactGallery.viewport.test.tsx` | 105 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MessageAttachments.viewport.test.tsx` | 105 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/PendingConfirmationsSection.viewport.test.tsx` | 105 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectPromotionTargetSection.viewport.test.tsx` | 105 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ScopeConversionRatesSection.viewport.test.tsx` | 104 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskContextRail.viewport.test.tsx` | 102 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/CapabilityTrialsPanel.viewport.test.tsx` | 101 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SystemMaturityCard.viewport.test.tsx` | 101 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ScopeTrendsSection.viewport.test.tsx` | 100 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ReviewQualitySection.viewport.test.tsx` | 98 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ParkWakeHealthCard.viewport.test.tsx` | 97 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/CriterionVerdictBadge.viewport.test.tsx` | 96 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MetricsIntegrityObservabilityPanel.viewport.test.tsx` | 95 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WarmReuseVsCostTrend.viewport.test.tsx` | 95 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/OperatorWorkspaces.viewport.test.tsx` | 94 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/FleetDetailDrawer.viewport.test.tsx` | 92 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SettingsOrchestratorSection.viewport.test.tsx` | 91 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SrRetrospectiveShared.viewport.test.tsx` | 90 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/UserManagement.viewport.test.tsx` | 90 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectResourceEnvelopeSection.viewport.test.tsx` | 88 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SystemStatusCard.viewport.test.tsx` | 88 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WhiteboardRailSection.viewport.test.tsx` | 88 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/FleetRunnerConfigPanel.viewport.test.tsx` | 87 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectAutoRemediationSection.viewport.test.tsx` | 87 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SettingsGeneralSection.viewport.test.tsx` | 87 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/CommandPalette.viewport.test.tsx` | 85 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DirectionAutoPauseWidget.viewport.test.tsx` | 85 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SrBadges.viewport.test.tsx` | 85 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/CompletionDebtPanel.viewport.test.tsx` | 84 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/EvergreenBacklogPanel.viewport.test.tsx` | 84 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ErrorBoundary.viewport.test.tsx` | 82 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SettingsAdvancedSection.viewport.test.tsx` | 81 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectAlertDomainsSection.viewport.test.tsx` | 80 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProviderCategoriesPanel.viewport.test.tsx` | 80 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DependencyHealthCard.viewport.test.tsx` | 79 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ReleaseEvidence.viewport.test.tsx` | 76 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectIssueImplementationSection.viewport.test.tsx` | 75 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectResourceAvailabilityCard.viewport.test.tsx` | 75 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProviderCostPanel.viewport.test.tsx` | 74 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectAutoReviewSection.viewport.test.tsx` | 73 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StrategicSessionControl.viewport.test.tsx` | 73 | 2 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 2 |
| G | admin-ui-viewport | `admin-ui/src/components/UILoadTimingPanel.viewport.test.tsx` | 73 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/AutonomousConfigSection.viewport.test.tsx` | 70 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DesignDocViewer.viewport.test.tsx` | 70 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/EventVisibilityMenu.viewport.test.tsx` | 70 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MuteWindowMenu.viewport.test.tsx` | 69 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskDurationWidget.viewport.test.tsx` | 69 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/DashboardMetricCards.viewport.test.tsx` | 68 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StrategicStreamingPreview.viewport.test.tsx` | 68 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StructuralHealthObservabilityPanel.viewport.test.tsx` | 68 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ConsultationProgressRow.viewport.test.tsx` | 67 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MarkdownContent.viewport.test.tsx` | 67 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ProjectSwitcherDropdown.viewport.test.tsx` | 66 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/FailureStageWidget.viewport.test.tsx` | 65 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/FleetRunnersDashboard.viewport.test.tsx` | 65 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/RecurringPatternsSection.viewport.test.tsx` | 65 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SavedViewsTabStrip.viewport.test.tsx` | 65 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/AcceptanceGradeBadge.viewport.test.tsx` | 64 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/EvergreenFillObservabilityPanel.viewport.test.tsx` | 63 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/SaveViewDialog.viewport.test.tsx` | 63 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/NullBranchDiagnosticsPanel.viewport.test.tsx` | 62 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MergeGateFleetHealthCard.viewport.test.tsx` | 61 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/ToolResidentHandoffControls.viewport.test.tsx` | 61 | 2 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 2 |
| G | admin-ui-viewport | `admin-ui/src/components/PoolAccountHealthPanel.viewport.test.tsx` | 60 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/StrategistStatusChip.viewport.test.tsx` | 58 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | backend | `backend/testing/test_agent_task_contract.py` | 50 | 0 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WarmStartOriginRow.viewport.test.tsx` | 49 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/AccountMenu.viewport.test.tsx` | 45 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/MessagesSummaryCard.viewport.test.tsx` | 41 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/EvidenceTierBadge.viewport.test.tsx` | 39 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/KeyboardShortcutsOverlay.viewport.test.tsx` | 36 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/TaskFixLoopPanel.viewport.test.tsx` | 32 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/AdversarialQualityPanel.viewport.test.tsx` | 28 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| G | admin-ui-viewport | `admin-ui/src/components/WelcomeScreen.viewport.test.tsx` | 28 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
