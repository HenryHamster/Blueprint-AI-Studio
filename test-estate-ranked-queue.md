# Test Estate Ranked Queue

> GENERATED FILE. Do not hand-edit mutable estate facts.
> Regenerate with `python3 scripts/generate_test_estate_ranked_queue.py --write`;
> validate with `python3 scripts/generate_test_estate_ranked_queue.py --check`.
> Policy and threshold definitions: `test-estate-rubric.md`.

## Estate summary

| Area | Files | Test LOC | Canonical tests | Measured cost | Unknown-cost files |
|------|------:|---------:|----------------:|--------------:|-------------------:|
| backend | 617 | 968,388 | 25,909 | 13,675.964s (372 files) | 245 |
| admin-ui | 253 | 100,519 | 4,414 | 209.620s (253 files) | 0 |
| agent | 125 | 111,097 | 5,860 | 72.033s (125 files) | 0 |

The queue includes every currently collected backend, admin-ui, and agent test file. Backend duration coverage is partial: an `unknown` cost is intentionally not treated as zero or ranked as cheap. Admin-ui and agent costs are complete directional signals from their committed artifacts. Protected Workday counts are copied verbatim from the manifest for reporting only; this generator never edits that manifest.

## Ranked queue

| Rank | Priority | Area | File | LOC | Tests | Threshold class | Cost signal | Workday protection |
|-----:|:--------:|------|------|----:|------:|------------------|-------------|---------------------|
| 1 | P2 | backend | `backend/testing/test_strategic_completion.py` | 21,432 | 616 | critical (>5000 LOC) | 1,290.842s measured | — |
| 2 | P2 | backend | `backend/testing/test_release_qualifier__compute_composite.py` | 5,968 | 264 | critical (>5000 LOC) | 777.408s measured | — |
| 3 | P2 | backend | `backend/testing/test_review_pipeline.py` | 19,975 | 422 | critical (>5000 LOC) | 557.274s measured | — |
| 4 | P2 | backend | `backend/testing/test_pipeline.py` | 6,837 | 277 | critical (>5000 LOC) | 524.226s measured | — |
| 5 | P2 | backend | `backend/testing/test_task_lifecycle.py` | 12,505 | 314 | critical (>5000 LOC) | 413.882s measured | — |
| 6 | P2 | backend | `backend/testing/test_reaper.py` | 20,684 | 484 | critical (>5000 LOC) | 154.118s measured | protected; declared floor 484 |
| 7 | P2 | backend | `backend/testing/test_research_completion.py` | 23,835 | 530 | critical (>5000 LOC) | 137.469s measured | — |
| 8 | P2 | backend | `backend/testing/test_playbook_sr_metrics.py` | 8,196 | 148 | critical (>5000 LOC) | 133.180s measured | — |
| 9 | P2 | backend | `backend/testing/test_autonomous_research.py` | 20,226 | 393 | critical (>5000 LOC) | 125.162s measured | — |
| 10 | P2 | backend | `backend/testing/test_agent.py` | 28,667 | 503 | critical (>5000 LOC) | 122.224s measured | — |
| 11 | P2 | backend | `backend/testing/test_invariants.py` | 15,142 | 271 | critical (>5000 LOC) | 120.389s measured | — |
| 12 | P2 | backend | `backend/testing/test_local_runner.py` | 13,861 | 345 | critical (>5000 LOC) | 111.153s measured | — |
| 13 | P2 | backend | `backend/testing/test_digest.py` | 11,003 | 231 | critical (>5000 LOC) | 86.555s measured | — |
| 14 | P2 | backend | `backend/testing/test_release_qualifier__ci_unresolved_blocker.py` | 6,463 | 162 | critical (>5000 LOC) | 82.078s measured | — |
| 15 | P2 | backend | `backend/testing/test_directions.py` | 11,762 | 339 | critical (>5000 LOC) | 79.334s measured | — |
| 16 | P2 | backend | `backend/testing/test_admin_routers.py` | 17,952 | 443 | critical (>5000 LOC) | 75.000s measured | protected; declared floor 443 |
| 17 | P2 | backend | `backend/testing/test_acceptance_criteria.py` | 13,383 | 283 | critical (>5000 LOC) | 74.580s measured | — |
| 18 | P2 | backend | `backend/testing/test_orchestrator.py` | 17,472 | 347 | critical (>5000 LOC) | 70.488s measured | — |
| 19 | P2 | backend | `backend/testing/test_metrics_integrity_sweep.py` | 8,110 | 215 | critical (>5000 LOC) | 65.620s measured | — |
| 20 | P2 | backend | `backend/testing/test_compute.py` | 8,520 | 170 | critical (>5000 LOC) | 57.415s measured | — |
| 21 | P2 | backend | `backend/testing/test_compute_experiment_completion.py` | 6,922 | 126 | critical (>5000 LOC) | 55.582s measured | — |
| 22 | P2 | backend | `backend/testing/test_rollup_management.py` | 6,020 | 101 | critical (>5000 LOC) | 54.108s measured | — |
| 23 | P2 | backend | `backend/testing/test_knowledge_documents.py` | 6,801 | 164 | critical (>5000 LOC) | 48.488s measured | — |
| 24 | P2 | backend | `backend/testing/test_release_reconciliation.py` | 8,324 | 141 | critical (>5000 LOC) | 43.934s measured | — |
| 25 | P2 | backend | `backend/testing/test_release_qualifier__classify_check_runs.py` | 7,570 | 134 | critical (>5000 LOC) | 42.784s measured | — |
| 26 | P2 | backend | `backend/testing/test_compute_image_build_local.py` | 8,169 | 228 | critical (>5000 LOC) | 42.722s measured | — |
| 27 | P2 | backend | `backend/testing/test_efficiency_snapshot.py` | 12,740 | 314 | critical (>5000 LOC) | 39.993s measured | — |
| 28 | P2 | backend | `backend/testing/test_ci_completion.py` | 8,900 | 146 | critical (>5000 LOC) | 37.248s measured | — |
| 29 | P2 | backend | `backend/testing/test_evaluating.py` | 6,043 | 92 | critical (>5000 LOC) | 33.386s measured | — |
| 30 | P2 | backend | `backend/testing/test_direction_outcomes.py` | 10,387 | 191 | critical (>5000 LOC) | 26.802s measured | — |
| 31 | P2 | backend | `backend/testing/test_warm_pool_maintainer.py` | 5,828 | 122 | critical (>5000 LOC) | 17.404s measured | — |
| 32 | P2 | backend | `backend/testing/test_github.py` | 5,847 | 185 | critical (>5000 LOC) | 17.135s measured | — |
| 33 | P2 | backend | `backend/testing/test_salvage_bundle_standalone.py` | 7,737 | 239 | critical (>5000 LOC) | 8.485s measured | — |
| 34 | P2 | backend | `backend/testing/test_digest_metrics.py` | 7,840 | 245 | critical (>5000 LOC) | 8.434s measured | — |
| 35 | P2 | backend | `backend/testing/test_agent_strategic_fetch.py` | 7,041 | 139 | critical (>5000 LOC) | 7.724s measured | protected; declared floor 139 |
| 36 | P2 | agent | `agent/src/handlers/strategic-resident.test.ts` | 9,153 | 127 | critical (>5000 LOC) | 7.566s measured | protected; declared floor 127 |
| 37 | P2 | backend | `backend/testing/test_release_qualifier__finalize_qualification_2.py` | 6,954 | 112 | critical (>5000 LOC) | 5.220s measured | — |
| 38 | P2 | backend | `backend/testing/test_strategic_state.py` | 10,632 | 174 | critical (>5000 LOC) | 4.542s measured | protected; declared floor 174 |
| 39 | P2 | backend | `backend/testing/test_release_qualifier__sweep_stuck_qualifications.py` | 6,242 | 124 | critical (>5000 LOC) | 3.776s measured | — |
| 40 | P2 | backend | `backend/testing/test_release_qualifier__finalize_qualification.py` | 7,284 | 101 | critical (>5000 LOC) | 3.708s measured | — |
| 41 | P2 | admin-ui | `admin-ui/src/components/VersionLifecyclePanel.test.tsx` | 5,098 | 174 | critical (>5000 LOC) | 2.891s measured | — |
| 42 | P2 | backend | `backend/testing/test_acceptance_gate.py` | 5,489 | 127 | critical (>5000 LOC) | 1.800s measured | — |
| 43 | P2 | backend | `backend/testing/test_max_plan_pool.py` | 6,364 | 359 | critical (>5000 LOC) | 1.344s measured | — |
| 44 | P2 | backend | `backend/testing/test_capability_trial_verdict.py` | 5,371 | 286 | critical (>5000 LOC) | 0.804s measured | — |
| 45 | P2 | backend | `backend/testing/test_problem_model.py` | 6,802 | 304 | critical (>5000 LOC) | 0.769s measured | — |
| 46 | P2 | backend | `backend/testing/test_capability_proof.py` | 6,622 | 247 | critical (>5000 LOC) | 0.548s measured | — |
| 47 | P2 | agent | `agent/src/handlers/research-plan.test.ts` | 5,326 | 353 | critical (>5000 LOC) | 0.309s measured | — |
| 48 | P2 | backend | `backend/testing/test_strategic_resident_session.py` | 5,475 | 85 | critical (>5000 LOC) | unknown (no committed measurement) | protected; declared floor 85 |
| 49 | P2 | backend | `backend/testing/test_admin_routers__version_lifecycle.py` | 5,059 | 103 | critical (>5000 LOC) | unknown (no committed measurement) | — |
| 50 | P3 | backend | `backend/testing/test_strategic_analysis.py` | 4,050 | 163 | high (3000–5000 LOC) | 465.090s measured | — |
| 51 | P3 | backend | `backend/testing/test_task_completion.py` | 4,674 | 181 | high (3000–5000 LOC) | 265.682s measured | — |
| 52 | P3 | backend | `backend/testing/test_release_version.py` | 3,837 | 150 | high (3000–5000 LOC) | 246.640s measured | — |
| 53 | P3 | backend | `backend/testing/test_release_qualifier__site_b_lock_window_fix.py` | 3,656 | 108 | high (3000–5000 LOC) | 230.584s measured | — |
| 54 | P3 | backend | `backend/testing/test_pipeline_health.py` | 4,041 | 120 | high (3000–5000 LOC) | 154.448s measured | — |
| 55 | P3 | backend | `backend/testing/test_project_tools.py` | 3,426 | 108 | high (3000–5000 LOC) | 82.858s measured | — |
| 56 | P3 | backend | `backend/testing/test_failure_classification.py` | 4,339 | 384 | high (3000–5000 LOC) | 73.366s measured | — |
| 57 | P3 | backend | `backend/testing/test_admin_analytics.py` | 4,247 | 98 | high (3000–5000 LOC) | 49.510s measured | — |
| 58 | P3 | backend | `backend/testing/test_plan_completion_atomic.py` | 3,311 | 55 | high (3000–5000 LOC) | 48.729s measured | — |
| 59 | P3 | backend | `backend/testing/test_admin_routers__projects.py` | 3,609 | 132 | high (3000–5000 LOC) | 45.976s measured | — |
| 60 | P3 | backend | `backend/testing/test_background.py` | 4,210 | 87 | high (3000–5000 LOC) | 38.844s measured | — |
| 61 | P3 | backend | `backend/testing/test_pipeline_reliability.py` | 4,182 | 53 | high (3000–5000 LOC) | 37.254s measured | — |
| 62 | P3 | backend | `backend/testing/test_services.py` | 4,343 | 105 | high (3000–5000 LOC) | 32.037s measured | — |
| 63 | P3 | backend | `backend/testing/test_admin_compute_jobs.py` | 3,311 | 79 | high (3000–5000 LOC) | 29.428s measured | — |
| 64 | P3 | backend | `backend/testing/test_digest_sr.py` | 3,895 | 74 | high (3000–5000 LOC) | 27.494s measured | — |
| 65 | P3 | backend | `backend/testing/test_improvement_lane.py` | 3,811 | 100 | high (3000–5000 LOC) | 26.526s measured | — |
| 66 | P3 | backend | `backend/testing/test_compute_experiment_spawn.py` | 4,481 | 150 | high (3000–5000 LOC) | 26.515s measured | — |
| 67 | P3 | backend | `backend/testing/test_digest_direction_progress.py` | 3,284 | 61 | high (3000–5000 LOC) | 25.744s measured | — |
| 68 | P3 | backend | `backend/testing/test_reaper_sweeps_compute_image.py` | 3,046 | 77 | high (3000–5000 LOC) | 25.598s measured | — |
| 69 | P3 | backend | `backend/testing/test_message_audience.py` | 3,600 | 136 | high (3000–5000 LOC) | 22.745s measured | — |
| 70 | P3 | backend | `backend/testing/test_no_ci_merge_gate_e2e.py` | 3,784 | 59 | high (3000–5000 LOC) | 22.715s measured | — |
| 71 | P3 | backend | `backend/testing/test_hotfix_backmerge.py` | 3,861 | 82 | high (3000–5000 LOC) | 18.743s measured | — |
| 72 | P3 | backend | `backend/testing/test_notification_dispatch.py` | 3,420 | 120 | high (3000–5000 LOC) | 15.923s measured | — |
| 73 | P3 | backend | `backend/testing/test_direction_lifecycle.py` | 4,998 | 81 | high (3000–5000 LOC) | 13.910s measured | — |
| 74 | P3 | admin-ui | `admin-ui/src/components/ComputeJobsPanel.test.tsx` | 4,876 | 212 | high (3000–5000 LOC) | 11.886s measured | — |
| 75 | P3 | backend | `backend/testing/test_onboard.py` | 4,413 | 137 | high (3000–5000 LOC) | 10.811s measured | — |
| 76 | P3 | backend | `backend/testing/test_blocked_need_resolution.py` | 4,481 | 138 | high (3000–5000 LOC) | 8.477s measured | — |
| 77 | P3 | backend | `backend/testing/test_pack_under_budget.py` | 3,211 | 66 | high (3000–5000 LOC) | 6.628s measured | — |
| 78 | P3 | backend | `backend/testing/test_admin_routers__messages.py` | 4,897 | 140 | high (3000–5000 LOC) | 3.650s measured | protected; declared floor 140 |
| 79 | P3 | backend | `backend/testing/test_alert_dispatch.py` | 3,259 | 78 | high (3000–5000 LOC) | 1.590s measured | — |
| 80 | P3 | agent | `agent/src/index.watchdog.test.ts` | 3,507 | 113 | high (3000–5000 LOC) | 0.480s measured | — |
| 81 | P3 | backend | `backend/testing/test_repo_bootstrap.py` | 3,989 | 81 | high (3000–5000 LOC) | 0.380s measured | — |
| 82 | P3 | agent | `agent/src/handlers/review-fix.test.ts` | 4,333 | 223 | high (3000–5000 LOC) | 0.081s measured | — |
| 83 | P3 | agent | `agent/src/handlers/strategic-resident-lib.test.ts` | 4,237 | 297 | high (3000–5000 LOC) | 0.060s measured | protected; declared floor 297 |
| 84 | P3 | agent | `agent/src/handlers/standard.test.ts` | 3,316 | 205 | high (3000–5000 LOC) | 0.056s measured | — |
| 85 | P3 | agent | `agent/src/utils/test-runners.test.ts` | 3,880 | 367 | high (3000–5000 LOC) | 0.045s measured | — |
| 86 | P3 | backend | `backend/testing/test_rollup_cleanup.py` | 4,286 | 75 | high (3000–5000 LOC) | unknown (no committed measurement) | — |
| 87 | P3 | backend | `backend/testing/test_pipeline_integration.py` | 3,792 | 35 | high (3000–5000 LOC) | unknown (no committed measurement) | — |
| 88 | P3 | backend | `backend/testing/test_compute_runner_dispatch.py` | 3,349 | 69 | high (3000–5000 LOC) | unknown (no committed measurement) | — |
| 89 | P5 | backend | `backend/testing/test_release_scoring.py` | 2,240 | 108 | moderate (1500–2999 LOC) | 350.308s measured | — |
| 90 | P5 | backend | `backend/testing/test_strategic_spawning_capability_verification.py` | 2,575 | 76 | moderate (1500–2999 LOC) | 271.198s measured | — |
| 91 | P5 | backend | `backend/testing/test_anomaly_actions.py` | 1,525 | 75 | moderate (1500–2999 LOC) | 215.272s measured | — |
| 92 | P5 | backend | `backend/testing/test_review_learning.py` | 2,804 | 85 | moderate (1500–2999 LOC) | 149.356s measured | — |
| 93 | P5 | backend | `backend/testing/test_qualification_state_resolver_dimensions.py` | 2,300 | 71 | moderate (1500–2999 LOC) | 114.390s measured | — |
| 94 | P5 | backend | `backend/testing/test_strategic_spawning_portfolio_messages.py` | 2,612 | 75 | moderate (1500–2999 LOC) | 105.778s measured | — |
| 95 | P5 | backend | `backend/testing/test_strategic_spawning_summaries.py` | 2,357 | 54 | moderate (1500–2999 LOC) | 98.130s measured | — |
| 96 | P5 | backend | `backend/testing/test_strategic_context_package.py` | 1,920 | 47 | moderate (1500–2999 LOC) | 86.066s measured | — |
| 97 | P5 | backend | `backend/testing/test_admin_runners.py` | 1,826 | 62 | moderate (1500–2999 LOC) | 76.884s measured | — |
| 98 | P5 | backend | `backend/testing/test_strategic_spawning_capability_prediction.py` | 2,607 | 72 | moderate (1500–2999 LOC) | 76.266s measured | — |
| 99 | P5 | backend | `backend/testing/test_generated_shell.py` | 2,450 | 105 | moderate (1500–2999 LOC) | 50.616s measured | — |
| 100 | P5 | backend | `backend/testing/test_strategic_spawning_scope_budget.py` | 2,578 | 41 | moderate (1500–2999 LOC) | 42.384s measured | — |
| 101 | P5 | backend | `backend/testing/test_audit_finding_class_guards.py` | 1,979 | 33 | moderate (1500–2999 LOC) | 34.404s measured | — |
| 102 | P5 | backend | `backend/testing/test_warm_backlog_reuse.py` | 2,019 | 53 | moderate (1500–2999 LOC) | 33.622s measured | — |
| 103 | P5 | backend | `backend/testing/test_staged_directions.py` | 1,694 | 40 | moderate (1500–2999 LOC) | 29.493s measured | — |
| 104 | P5 | backend | `backend/testing/test_warm_pool.py` | 1,591 | 45 | moderate (1500–2999 LOC) | 26.162s measured | — |
| 105 | P5 | backend | `backend/testing/test_payload_schemas.py` | 2,161 | 148 | moderate (1500–2999 LOC) | 18.922s measured | — |
| 106 | P5 | backend | `backend/testing/test_deploy_health.py` | 2,180 | 80 | moderate (1500–2999 LOC) | 17.586s measured | — |
| 107 | P5 | backend | `backend/testing/test_migration_apply_gate.py` | 2,900 | 86 | moderate (1500–2999 LOC) | 17.539s measured | — |
| 108 | P5 | backend | `backend/testing/test_ci_compliance_gate.py` | 2,992 | 55 | moderate (1500–2999 LOC) | 16.687s measured | — |
| 109 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.test.tsx` | 2,791 | 77 | moderate (1500–2999 LOC) | 14.213s measured | protected; declared floor 77 |
| 110 | P5 | agent | `agent/src/prompts/pr-helper.test.ts` | 2,166 | 37 | moderate (1500–2999 LOC) | 13.665s measured | — |
| 111 | P5 | backend | `backend/testing/test_lock_window.py` | 2,373 | 36 | moderate (1500–2999 LOC) | 11.653s measured | — |
| 112 | P5 | backend | `backend/testing/test_task_locking.py` | 2,456 | 62 | moderate (1500–2999 LOC) | 11.250s measured | — |
| 113 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.send.test.tsx` | 1,908 | 38 | moderate (1500–2999 LOC) | 11.185s measured | — |
| 114 | P5 | backend | `backend/testing/test_auto_remediation.py` | 2,857 | 62 | moderate (1500–2999 LOC) | 9.882s measured | — |
| 115 | P5 | backend | `backend/testing/test_diagnostics.py` | 1,505 | 59 | moderate (1500–2999 LOC) | 9.673s measured | — |
| 116 | P5 | backend | `backend/testing/test_strategic_spawning_type_selection.py` | 2,620 | 64 | moderate (1500–2999 LOC) | 9.580s measured | — |
| 117 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.filters.test.tsx` | 1,701 | 55 | moderate (1500–2999 LOC) | 8.977s measured | — |
| 118 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.interactions.test.tsx` | 1,846 | 50 | moderate (1500–2999 LOC) | 8.913s measured | — |
| 119 | P5 | backend | `backend/testing/test_completion_debt.py` | 2,246 | 56 | moderate (1500–2999 LOC) | 8.440s measured | — |
| 120 | P5 | backend | `backend/testing/test_cost_telemetry_reconciliation.py` | 1,694 | 42 | moderate (1500–2999 LOC) | 7.868s measured | — |
| 121 | P5 | backend | `backend/testing/test_strategic_spawning_outcome_evaluation.py` | 2,605 | 48 | moderate (1500–2999 LOC) | 6.466s measured | — |
| 122 | P5 | backend | `backend/testing/test_evergreen_backlog.py` | 1,539 | 50 | moderate (1500–2999 LOC) | 6.364s measured | — |
| 123 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.notifications.test.tsx` | 1,524 | 65 | moderate (1500–2999 LOC) | 5.501s measured | — |
| 124 | P5 | admin-ui | `admin-ui/src/components/DashboardHome.test.tsx` | 2,682 | 80 | moderate (1500–2999 LOC) | 5.207s measured | — |
| 125 | P5 | backend | `backend/testing/test_build_config_commands.py` | 1,718 | 56 | moderate (1500–2999 LOC) | 5.114s measured | — |
| 126 | P5 | agent | `agent/src/handlers/audits.test.ts` | 2,190 | 92 | moderate (1500–2999 LOC) | 5.039s measured | — |
| 127 | P5 | backend | `backend/testing/test_failure_residual.py` | 1,518 | 59 | moderate (1500–2999 LOC) | 4.634s measured | — |
| 128 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.advanced.test.tsx` | 1,932 | 32 | moderate (1500–2999 LOC) | 4.328s measured | protected; declared floor 32 |
| 129 | P5 | backend | `backend/testing/test_operator_inbox_liveness.py` | 2,505 | 50 | moderate (1500–2999 LOC) | 3.890s measured | — |
| 130 | P5 | admin-ui | `admin-ui/src/components/KnowledgeBrowser.test.tsx` | 1,689 | 46 | moderate (1500–2999 LOC) | 3.815s measured | — |
| 131 | P5 | admin-ui | `admin-ui/src/components/SrTrendBreakdown.test.tsx` | 2,335 | 149 | moderate (1500–2999 LOC) | 3.713s measured | — |
| 132 | P5 | admin-ui | `admin-ui/src/components/StrategicContextRail.test.tsx` | 2,550 | 57 | moderate (1500–2999 LOC) | 3.312s measured | protected; declared floor 57 |
| 133 | P5 | backend | `backend/testing/test_strategic_spawning_version_phase.py` | 2,693 | 40 | moderate (1500–2999 LOC) | 3.265s measured | — |
| 134 | P5 | admin-ui | `admin-ui/src/components/WarmVmPanel.test.tsx` | 1,769 | 67 | moderate (1500–2999 LOC) | 3.029s measured | — |
| 135 | P5 | admin-ui | `admin-ui/src/components/FleetRunnersPanel.test.tsx` | 2,942 | 93 | moderate (1500–2999 LOC) | 2.946s measured | — |
| 136 | P5 | admin-ui | `admin-ui/src/components/TaskDetailDrawerMeta.test.tsx` | 2,200 | 109 | moderate (1500–2999 LOC) | 2.512s measured | — |
| 137 | P5 | backend | `backend/testing/test_fleet_utilization.py` | 1,527 | 42 | moderate (1500–2999 LOC) | 1.586s measured | — |
| 138 | P5 | backend | `backend/testing/test_empty_repo_onboarding.py` | 1,837 | 51 | moderate (1500–2999 LOC) | 1.565s measured | — |
| 139 | P5 | agent | `agent/src/prompts/check-helper.test.ts` | 2,326 | 68 | moderate (1500–2999 LOC) | 1.473s measured | — |
| 140 | P5 | admin-ui | `admin-ui/src/hooks/useWorkbenchCommands.test.tsx` | 2,238 | 50 | moderate (1500–2999 LOC) | 1.277s measured | — |
| 141 | P5 | backend | `backend/testing/test_review_loop_convergence.py` | 2,684 | 52 | moderate (1500–2999 LOC) | 1.084s measured | — |
| 142 | P5 | backend | `backend/testing/test_warm_vm_integration.py` | 2,999 | 28 | moderate (1500–2999 LOC) | 0.992s measured | — |
| 143 | P5 | agent | `agent/src/handlers/strategic-helpers.test.ts` | 2,251 | 115 | moderate (1500–2999 LOC) | 0.866s measured | protected; declared floor 115 |
| 144 | P5 | admin-ui | `admin-ui/src/hooks/useTaskModal.test.ts` | 1,698 | 79 | moderate (1500–2999 LOC) | 0.848s measured | — |
| 145 | P5 | agent | `agent/src/prompts/code-change-helpers.test.ts` | 1,900 | 71 | moderate (1500–2999 LOC) | 0.730s measured | — |
| 146 | P5 | backend | `backend/testing/test_migration_chain.py` | 1,648 | 80 | moderate (1500–2999 LOC) | 0.714s measured | — |
| 147 | P5 | backend | `backend/testing/test_size_offender_governance.py` | 1,999 | 64 | moderate (1500–2999 LOC) | 0.565s measured | — |
| 148 | P5 | backend | `backend/testing/test_dependency_contracts.py` | 1,804 | 44 | moderate (1500–2999 LOC) | 0.551s measured | — |
| 149 | P5 | agent | `agent/src/handlers/strategic.test.ts` | 2,923 | 89 | moderate (1500–2999 LOC) | 0.532s measured | — |
| 150 | P5 | agent | `agent/src/prompt.guidance.test.ts` | 2,127 | 173 | moderate (1500–2999 LOC) | 0.438s measured | — |
| 151 | P5 | backend | `backend/testing/test_ci_resolution.py` | 2,780 | 158 | moderate (1500–2999 LOC) | 0.331s measured | — |
| 152 | P5 | agent | `agent/src/index.pool-mode.test.ts` | 2,602 | 73 | moderate (1500–2999 LOC) | 0.297s measured | — |
| 153 | P5 | backend | `backend/testing/test_finding_deduplication.py` | 2,485 | 131 | moderate (1500–2999 LOC) | 0.286s measured | — |
| 154 | P5 | backend | `backend/testing/test_migration_divergence.py` | 2,302 | 76 | moderate (1500–2999 LOC) | 0.275s measured | — |
| 155 | P5 | backend | `backend/testing/test_ci_compliance.py` | 1,617 | 37 | moderate (1500–2999 LOC) | 0.253s measured | — |
| 156 | P5 | backend | `backend/testing/test_health_zone_pool.py` | 2,149 | 118 | moderate (1500–2999 LOC) | 0.236s measured | — |
| 157 | P5 | admin-ui | `admin-ui/src/hooks/useLogViewer.test.ts` | 1,593 | 48 | moderate (1500–2999 LOC) | 0.217s measured | — |
| 158 | P5 | backend | `backend/testing/test_size_limits_checker.py` | 1,895 | 81 | moderate (1500–2999 LOC) | 0.162s measured | — |
| 159 | P5 | backend | `backend/testing/test_llm_runtime.py` | 1,885 | 47 | moderate (1500–2999 LOC) | 0.100s measured | — |
| 160 | P5 | backend | `backend/testing/test_max_plan_pool_usage.py` | 2,499 | 96 | moderate (1500–2999 LOC) | 0.087s measured | — |
| 161 | P5 | agent | `agent/src/index.llm-runner.test.ts` | 1,969 | 95 | moderate (1500–2999 LOC) | 0.072s measured | — |
| 162 | P5 | agent | `agent/src/index.provider-runner.test.ts` | 1,880 | 125 | moderate (1500–2999 LOC) | 0.066s measured | protected; declared floor 125 |
| 163 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.utils.test.ts` | 1,622 | 157 | moderate (1500–2999 LOC) | 0.063s measured | — |
| 164 | P5 | agent | `agent/src/index.test.ts` | 1,885 | 192 | moderate (1500–2999 LOC) | 0.044s measured | — |
| 165 | P5 | agent | `agent/src/index.utilities.test.ts` | 1,556 | 131 | moderate (1500–2999 LOC) | 0.040s measured | — |
| 166 | P5 | backend | `backend/testing/test_artifact_refs.py` | 1,546 | 29 | moderate (1500–2999 LOC) | 0.036s measured | — |
| 167 | P5 | agent | `agent/src/handlers/specialized.test.ts` | 2,570 | 133 | moderate (1500–2999 LOC) | 0.034s measured | — |
| 168 | P5 | agent | `agent/src/prompt.test.ts` | 2,513 | 178 | moderate (1500–2999 LOC) | 0.024s measured | — |
| 169 | P5 | agent | `agent/src/prompt.project-context.test.ts` | 2,116 | 152 | moderate (1500–2999 LOC) | 0.023s measured | — |
| 170 | P5 | agent | `agent/src/handlers/strategic-sections.test.ts` | 2,924 | 192 | moderate (1500–2999 LOC) | 0.020s measured | — |
| 171 | P5 | agent | `agent/src/workspacePrep.test.ts` | 1,875 | 74 | moderate (1500–2999 LOC) | 0.018s measured | protected; declared floor 74 |
| 172 | P5 | agent | `agent/src/prompts/thinker.test.ts` | 1,904 | 101 | moderate (1500–2999 LOC) | 0.016s measured | — |
| 173 | P5 | agent | `agent/src/prompt.format.test.ts` | 1,512 | 102 | moderate (1500–2999 LOC) | 0.012s measured | — |
| 174 | P5 | backend | `backend/testing/test_strategic_session_messages.py` | 2,949 | 42 | moderate (1500–2999 LOC) | unknown (no committed measurement) | protected; declared floor 42 |
| 175 | P5 | backend | `backend/testing/test_admin_pool_accounts.py` | 2,860 | 52 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 176 | P5 | backend | `backend/testing/test_release_commit_authorship.py` | 2,406 | 53 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 177 | P5 | backend | `backend/testing/test_alerts.py` | 2,287 | 60 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 178 | P5 | backend | `backend/testing/test_admin_routers__strategic_directions.py` | 2,071 | 29 | moderate (1500–2999 LOC) | unknown (no committed measurement) | protected; declared floor 29 |
| 179 | P5 | backend | `backend/testing/test_discovery_review.py` | 2,028 | 45 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 180 | P5 | backend | `backend/testing/test_warm_pool_dispatch.py` | 1,961 | 40 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 181 | P5 | backend | `backend/testing/test_admin_project_config.py` | 1,906 | 78 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 182 | P5 | backend | `backend/testing/test_compute_gcp_startup.py` | 1,904 | 61 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 183 | P5 | backend | `backend/testing/test_compute_park_wake.py` | 1,690 | 12 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 184 | P5 | backend | `backend/testing/test_approved_pr_stranding.py` | 1,683 | 34 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 185 | P5 | backend | `backend/testing/test_api.py` | 1,587 | 39 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 186 | P5 | backend | `backend/testing/test_admin_routers__notifications.py` | 1,555 | 53 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 187 | P5 | backend | `backend/testing/test_admin_approval_authority.py` | 1,510 | 16 | moderate (1500–2999 LOC) | unknown (no committed measurement) | — |
| 188 | G | backend | `backend/testing/test_task_outcomes.py` | 1,375 | 108 | under 1500 LOC | 340.028s measured | — |
| 189 | G | backend | `backend/testing/test_task_evaluators.py` | 1,172 | 79 | under 1500 LOC | 290.618s measured | — |
| 190 | G | backend | `backend/testing/test_failure_layer_classifier.py` | 542 | 72 | under 1500 LOC | 268.284s measured | — |
| 191 | G | backend | `backend/testing/test_rate_limit_detection.py` | 995 | 68 | under 1500 LOC | 235.766s measured | — |
| 192 | G | backend | `backend/testing/test_qualification_triggers.py` | 938 | 72 | under 1500 LOC | 210.946s measured | — |
| 193 | G | backend | `backend/testing/test_research_context_eviction.py` | 1,013 | 64 | under 1500 LOC | 206.448s measured | — |
| 194 | G | backend | `backend/testing/test_providers.py` | 1,256 | 80 | under 1500 LOC | 180.326s measured | — |
| 195 | G | backend | `backend/testing/test_project_resource_envelope.py` | 883 | 65 | under 1500 LOC | 162.316s measured | — |
| 196 | G | backend | `backend/testing/test_rollup_orphan_prs.py` | 350 | 32 | under 1500 LOC | 149.254s measured | — |
| 197 | G | backend | `backend/testing/test_scope_complexity.py` | 380 | 32 | under 1500 LOC | 132.808s measured | — |
| 198 | G | backend | `backend/testing/test_provider_settings_bag.py` | 945 | 53 | under 1500 LOC | 128.652s measured | — |
| 199 | G | backend | `backend/testing/test_local_runner_viewable_selection.py` | 1,036 | 43 | under 1500 LOC | 118.198s measured | — |
| 200 | G | backend | `backend/testing/test_strategic_trigger.py` | 750 | 50 | under 1500 LOC | 100.516s measured | — |
| 201 | G | backend | `backend/testing/test_playbook_storage.py` | 723 | 24 | under 1500 LOC | 86.894s measured | — |
| 202 | G | backend | `backend/testing/test_review_policy_parity.py` | 372 | 22 | under 1500 LOC | 76.934s measured | — |
| 203 | G | backend | `backend/testing/test_time_utils.py` | 195 | 20 | under 1500 LOC | 62.680s measured | — |
| 204 | G | backend | `backend/testing/test_rollup_state.py` | 209 | 24 | under 1500 LOC | 62.286s measured | — |
| 205 | G | backend | `backend/testing/test_work_session_commands.py` | 755 | 16 | under 1500 LOC | 51.750s measured | — |
| 206 | G | backend | `backend/testing/test_qualification_dimensions.py` | 180 | 12 | under 1500 LOC | 49.858s measured | — |
| 207 | G | backend | `backend/testing/test_runner_progress_protocol.py` | 489 | 19 | under 1500 LOC | 48.290s measured | — |
| 208 | G | backend | `backend/testing/test_strategic_cycle.py` | 1,472 | 60 | under 1500 LOC | 45.070s measured | — |
| 209 | G | backend | `backend/testing/test_release_audit_contract.py` | 785 | 22 | under 1500 LOC | 45.066s measured | — |
| 210 | G | backend | `backend/testing/test_release_readiness.py` | 1,166 | 41 | under 1500 LOC | 44.504s measured | — |
| 211 | G | backend | `backend/testing/test_admin_tasks_fix_loop_summary.py` | 158 | 16 | under 1500 LOC | 42.968s measured | — |
| 212 | G | backend | `backend/testing/test_playbook_learning.py` | 245 | 11 | under 1500 LOC | 41.100s measured | — |
| 213 | G | backend | `backend/testing/test_review_policy.py` | 112 | 13 | under 1500 LOC | 37.828s measured | — |
| 214 | G | backend | `backend/testing/test_release_artifacts.py` | 989 | 31 | under 1500 LOC | 36.942s measured | — |
| 215 | G | backend | `backend/testing/test_viewable_watch.py` | 335 | 19 | under 1500 LOC | 30.448s measured | — |
| 216 | G | backend | `backend/testing/test_required_runtime_env.py` | 261 | 14 | under 1500 LOC | 29.704s measured | — |
| 217 | G | backend | `backend/testing/test_alert_policy.py` | 444 | 21 | under 1500 LOC | 29.090s measured | — |
| 218 | G | backend | `backend/testing/test_anomaly_detection.py` | 1,298 | 46 | under 1500 LOC | 28.734s measured | — |
| 219 | G | backend | `backend/testing/test_running_update_merge.py` | 100 | 8 | under 1500 LOC | 28.518s measured | — |
| 220 | G | backend | `backend/testing/test_playbook_lifecycle.py` | 388 | 11 | under 1500 LOC | 22.540s measured | — |
| 221 | G | backend | `backend/testing/test_project_message_projection.py` | 954 | 24 | under 1500 LOC | 21.524s measured | protected; declared floor 24 |
| 222 | G | backend | `backend/testing/test_vm_cost_rates.py` | 41 | 4 | under 1500 LOC | 21.020s measured | — |
| 223 | G | backend | `backend/testing/test_gate_revalidation.py` | 700 | 39 | under 1500 LOC | 19.830s measured | — |
| 224 | G | backend | `backend/testing/test_rollup_review_respawn_cap.py` | 586 | 15 | under 1500 LOC | 19.004s measured | — |
| 225 | G | backend | `backend/testing/test_workbench_events.py` | 707 | 16 | under 1500 LOC | 18.138s measured | — |
| 226 | G | backend | `backend/testing/test_release_promotion_metric_manifest.py` | 138 | 5 | under 1500 LOC | 17.020s measured | — |
| 227 | G | backend | `backend/testing/test_admin_acceptance_criteria_router.py` | 1,193 | 31 | under 1500 LOC | 16.509s measured | — |
| 228 | G | backend | `backend/testing/test_url_security.py` | 179 | 11 | under 1500 LOC | 16.416s measured | — |
| 229 | G | backend | `backend/testing/test_agent_branch_invariants.py` | 610 | 6 | under 1500 LOC | 16.300s measured | — |
| 230 | G | agent | `agent/src/handlers/walkthrough-stack.test.ts` | 392 | 14 | under 1500 LOC | 15.025s measured | — |
| 231 | G | backend | `backend/testing/test_strategic_ops_registry.py` | 246 | 6 | under 1500 LOC | 14.904s measured | — |
| 232 | G | backend | `backend/testing/test_vm_heartbeat_image.py` | 506 | 13 | under 1500 LOC | 14.890s measured | — |
| 233 | G | backend | `backend/testing/test_deploy_identity.py` | 206 | 18 | under 1500 LOC | 14.879s measured | — |
| 234 | G | backend | `backend/testing/test_runner_heartbeat.py` | 1,233 | 30 | under 1500 LOC | 14.700s measured | — |
| 235 | G | backend | `backend/testing/test_runtime_config_defaults.py` | 310 | 7 | under 1500 LOC | 14.319s measured | — |
| 236 | G | backend | `backend/testing/test_rate_limit_verbatim_corpus.py` | 269 | 2 | under 1500 LOC | 14.200s measured | — |
| 237 | G | backend | `backend/testing/test_work_session_actions.py` | 187 | 5 | under 1500 LOC | 13.800s measured | — |
| 238 | G | backend | `backend/testing/test_websocket_emitter_inventory.py` | 196 | 5 | under 1500 LOC | 13.560s measured | — |
| 239 | G | agent | `agent/src/handlers/bake-smoke-run.test.ts` | 144 | 6 | under 1500 LOC | 13.457s measured | — |
| 240 | G | backend | `backend/testing/test_leader_lock_keepalive.py` | 149 | 6 | under 1500 LOC | 13.415s measured | — |
| 241 | G | backend | `backend/testing/test_playbook_auto.py` | 368 | 14 | under 1500 LOC | 13.002s measured | — |
| 242 | G | backend | `backend/testing/test_viewport_coverage.py` | 170 | 7 | under 1500 LOC | 12.910s measured | — |
| 243 | G | backend | `backend/testing/test_system_state.py` | 713 | 22 | under 1500 LOC | 12.798s measured | — |
| 244 | G | backend | `backend/testing/test_provider_settings.py` | 873 | 38 | under 1500 LOC | 11.456s measured | — |
| 245 | G | backend | `backend/testing/test_self_audit.py` | 1,293 | 39 | under 1500 LOC | 10.168s measured | — |
| 246 | G | backend | `backend/testing/test_timeout_rate_metric_manifest.py` | 146 | 6 | under 1500 LOC | 10.148s measured | — |
| 247 | G | backend | `backend/testing/test_structural_health_audit.py` | 416 | 17 | under 1500 LOC | 10.128s measured | — |
| 248 | G | backend | `backend/testing/test_artifact_download.py` | 1,358 | 46 | under 1500 LOC | 10.092s measured | — |
| 249 | G | backend | `backend/testing/test_playbook_learning_modularity.py` | 193 | 2 | under 1500 LOC | 9.580s measured | — |
| 250 | G | backend | `backend/testing/test_drift_detection.py` | 1,051 | 25 | under 1500 LOC | 9.558s measured | — |
| 251 | G | backend | `backend/testing/test_migration_reservations.py` | 752 | 27 | under 1500 LOC | 9.423s measured | — |
| 252 | G | backend | `backend/testing/test_work_session_metrics.py` | 110 | 3 | under 1500 LOC | 8.840s measured | — |
| 253 | G | backend | `backend/testing/test_github_pulls.py` | 1,155 | 40 | under 1500 LOC | 8.351s measured | — |
| 254 | G | backend | `backend/testing/test_retry_hint_parser.py` | 65 | 5 | under 1500 LOC | 8.208s measured | — |
| 255 | G | backend | `backend/testing/test_migration_0608_projectmessage_foundation.py` | 697 | 10 | under 1500 LOC | 7.721s measured | — |
| 256 | G | backend | `backend/testing/test_compute_watchdog.py` | 1,256 | 56 | under 1500 LOC | 7.367s measured | — |
| 257 | G | backend | `backend/testing/test_compute__experiments.py` | 1,360 | 35 | under 1500 LOC | 6.879s measured | — |
| 258 | G | backend | `backend/testing/test_dependency_deprecations.py` | 258 | 8 | under 1500 LOC | 6.801s measured | — |
| 259 | G | admin-ui | `admin-ui/src/components/NewTaskModal.test.tsx` | 680 | 20 | under 1500 LOC | 6.704s measured | — |
| 260 | G | backend | `backend/testing/test_resolve_dialect_name.py` | 115 | 5 | under 1500 LOC | 6.508s measured | — |
| 261 | G | backend | `backend/testing/test_sr_catch_rate_statistics.py` | 248 | 7 | under 1500 LOC | 6.254s measured | — |
| 262 | G | backend | `backend/testing/test_perf_completion.py` | 345 | 7 | under 1500 LOC | 5.899s measured | — |
| 263 | G | backend | `backend/testing/test_monthly_budget_alerts.py` | 644 | 18 | under 1500 LOC | 5.812s measured | — |
| 264 | G | backend | `backend/testing/test_admin_create_task.py` | 873 | 22 | under 1500 LOC | 5.675s measured | — |
| 265 | G | admin-ui | `admin-ui/src/test/mock-factory-export-coverage.test.ts` | 1,173 | 17 | under 1500 LOC | 5.549s measured | — |
| 266 | G | backend | `backend/testing/test_local_runner_holds.py` | 811 | 20 | under 1500 LOC | 5.507s measured | — |
| 267 | G | backend | `backend/testing/test_github_token_resolver.py` | 799 | 20 | under 1500 LOC | 5.069s measured | — |
| 268 | G | admin-ui | `admin-ui/src/components/CommandPalette.test.tsx` | 423 | 15 | under 1500 LOC | 5.018s measured | — |
| 269 | G | admin-ui | `admin-ui/src/components/NewProjectModal.test.tsx` | 539 | 15 | under 1500 LOC | 4.881s measured | — |
| 270 | G | admin-ui | `admin-ui/src/hooks/useStrategicSession.test.tsx` | 914 | 25 | under 1500 LOC | 4.852s measured | protected; declared floor 25 |
| 271 | G | backend | `backend/testing/test_review_pipeline_modularity.py` | 209 | 4 | under 1500 LOC | 4.836s measured | — |
| 272 | G | backend | `backend/testing/test_bulk_action.py` | 906 | 15 | under 1500 LOC | 4.685s measured | — |
| 273 | G | backend | `backend/testing/test_deploy_rollback.py` | 536 | 13 | under 1500 LOC | 4.455s measured | — |
| 274 | G | backend | `backend/testing/test_sr_blind_spots.py` | 569 | 36 | under 1500 LOC | 4.255s measured | — |
| 275 | G | admin-ui | `admin-ui/src/components/SettingsOrchestratorSection.test.tsx` | 561 | 31 | under 1500 LOC | 4.234s measured | — |
| 276 | G | backend | `backend/testing/test_project_message_creation_latency.py` | 708 | 15 | under 1500 LOC | 4.130s measured | — |
| 277 | G | admin-ui | `admin-ui/src/components/OperatorWorkspaces.test.tsx` | 580 | 18 | under 1500 LOC | 4.024s measured | — |
| 278 | G | backend | `backend/testing/test_project_telemetry.py` | 508 | 18 | under 1500 LOC | 4.010s measured | — |
| 279 | G | backend | `backend/testing/test_parent_terminal_parity.py` | 295 | 4 | under 1500 LOC | 4.003s measured | — |
| 280 | G | backend | `backend/testing/test_message_convergence.py` | 434 | 21 | under 1500 LOC | 3.954s measured | — |
| 281 | G | admin-ui | `admin-ui/src/components/TaskDetailDrawer.test.tsx` | 620 | 29 | under 1500 LOC | 3.896s measured | — |
| 282 | G | backend | `backend/testing/test_memo_completion.py` | 986 | 24 | under 1500 LOC | 3.851s measured | — |
| 283 | G | backend | `backend/testing/test_fix_loop_telemetry.py` | 871 | 26 | under 1500 LOC | 3.777s measured | — |
| 284 | G | backend | `backend/testing/test_admin_endpoint_cache.py` | 807 | 16 | under 1500 LOC | 3.717s measured | — |
| 285 | G | backend | `backend/testing/test_message_supersession.py` | 428 | 12 | under 1500 LOC | 3.704s measured | — |
| 286 | G | backend | `backend/testing/test_lane_baseline_snapshot.py` | 1,014 | 36 | under 1500 LOC | 3.637s measured | — |
| 287 | G | agent | `agent/src/handlers/merge-grade.test.ts` | 539 | 34 | under 1500 LOC | 3.514s measured | — |
| 288 | G | backend | `backend/testing/test_firebase_auth_coverage.py` | 741 | 51 | under 1500 LOC | 3.408s measured | — |
| 289 | G | backend | `backend/testing/test_rejection_patterns.py` | 424 | 16 | under 1500 LOC | 3.374s measured | — |
| 290 | G | backend | `backend/testing/test_review_fix_lane_pilot_costs.py` | 1,333 | 19 | under 1500 LOC | 3.320s measured | — |
| 291 | G | backend | `backend/testing/test_migration_0466_encrypt_max_plan_credentials.py` | 170 | 4 | under 1500 LOC | 3.224s measured | — |
| 292 | G | backend | `backend/testing/test_capability_demand.py` | 939 | 36 | under 1500 LOC | 3.216s measured | — |
| 293 | G | backend | `backend/testing/test_cross_user_isolation.py` | 298 | 11 | under 1500 LOC | 3.194s measured | — |
| 294 | G | backend | `backend/testing/test_system_message_dedupe.py` | 146 | 6 | under 1500 LOC | 3.172s measured | — |
| 295 | G | admin-ui | `admin-ui/src/components/RemediationFunnelSection.test.tsx` | 680 | 39 | under 1500 LOC | 3.162s measured | — |
| 296 | G | backend | `backend/testing/test_agent_annotate.py` | 456 | 8 | under 1500 LOC | 2.950s measured | — |
| 297 | G | admin-ui | `admin-ui/src/hooks/useDeploymentState.test.ts` | 772 | 34 | under 1500 LOC | 2.924s measured | — |
| 298 | G | backend | `backend/testing/test_security_audit_completion.py` | 359 | 8 | under 1500 LOC | 2.808s measured | — |
| 299 | G | backend | `backend/testing/test_plan_noop.py` | 250 | 8 | under 1500 LOC | 2.793s measured | — |
| 300 | G | backend | `backend/testing/test_strategic_events.py` | 485 | 17 | under 1500 LOC | 2.750s measured | — |
| 301 | G | backend | `backend/testing/test_required_env_deploy_parity.py` | 134 | 5 | under 1500 LOC | 2.706s measured | — |
| 302 | G | backend | `backend/testing/test_direction_table.py` | 357 | 11 | under 1500 LOC | 2.684s measured | — |
| 303 | G | backend | `backend/testing/test_admin_activity.py` | 402 | 10 | under 1500 LOC | 2.639s measured | — |
| 304 | G | backend | `backend/testing/test_message_attachment_retention.py` | 305 | 9 | under 1500 LOC | 2.599s measured | — |
| 305 | G | admin-ui | `admin-ui/src/components/ResearchBrowser.test.tsx` | 839 | 33 | under 1500 LOC | 2.577s measured | — |
| 306 | G | backend | `backend/testing/test_reaper_sweeps_viewable_retire.py` | 273 | 8 | under 1500 LOC | 2.471s measured | — |
| 307 | G | admin-ui | `admin-ui/src/components/ArchiveView.test.tsx` | 216 | 5 | under 1500 LOC | 2.432s measured | — |
| 308 | G | backend | `backend/testing/test_filter_migration_serialization.py` | 226 | 9 | under 1500 LOC | 2.413s measured | — |
| 309 | G | backend | `backend/testing/test_auth.py` | 1,098 | 52 | under 1500 LOC | 2.412s measured | — |
| 310 | G | admin-ui | `admin-ui/src/components/ImprovementLanesPanel.test.tsx` | 741 | 30 | under 1500 LOC | 2.398s measured | — |
| 311 | G | backend | `backend/testing/test_reaper_sweeps.py` | 501 | 12 | under 1500 LOC | 2.343s measured | — |
| 312 | G | backend | `backend/testing/test_observability_payload_projection.py` | 514 | 8 | under 1500 LOC | 2.337s measured | — |
| 313 | G | backend | `backend/testing/test_leader_loop_saturation_c3.py` | 400 | 8 | under 1500 LOC | 2.293s measured | — |
| 314 | G | admin-ui | `admin-ui/src/components/OuterReviewLoopConvergenceWidget.test.tsx` | 419 | 12 | under 1500 LOC | 2.150s measured | — |
| 315 | G | backend | `backend/testing/test_filter_rollup_conflicts.py` | 491 | 8 | under 1500 LOC | 2.087s measured | — |
| 316 | G | backend | `backend/testing/test_category_verdict_ledger.py` | 591 | 9 | under 1500 LOC | 2.085s measured | — |
| 317 | G | admin-ui | `admin-ui/src/components/DirectionsPanel.test.tsx` | 456 | 17 | under 1500 LOC | 2.069s measured | — |
| 318 | G | admin-ui | `admin-ui/src/components/PipelineWorkspace.test.tsx` | 855 | 23 | under 1500 LOC | 2.059s measured | — |
| 319 | G | backend | `backend/testing/test_cost_summary.py` | 326 | 11 | under 1500 LOC | 2.053s measured | — |
| 320 | G | backend | `backend/testing/test_auto_review.py` | 395 | 11 | under 1500 LOC | 2.020s measured | — |
| 321 | G | agent | `agent/src/handlers/audit-challenge.test.ts` | 702 | 31 | under 1500 LOC | 2.016s measured | — |
| 322 | G | backend | `backend/testing/test_memo_exit_e2e.py` | 362 | 7 | under 1500 LOC | 1.950s measured | — |
| 323 | G | backend | `backend/testing/test_experiment_authoring_context.py` | 455 | 15 | under 1500 LOC | 1.908s measured | — |
| 324 | G | admin-ui | `admin-ui/src/components/AutonomousStatus.test.tsx` | 401 | 22 | under 1500 LOC | 1.904s measured | — |
| 325 | G | backend | `backend/testing/test_orchestrator_liveness.py` | 1,387 | 54 | under 1500 LOC | 1.893s measured | — |
| 326 | G | backend | `backend/testing/test_playbook_capture_success.py` | 833 | 31 | under 1500 LOC | 1.883s measured | — |
| 327 | G | backend | `backend/testing/test_operator_workbench_contracts.py` | 628 | 8 | under 1500 LOC | 1.836s measured | protected; declared floor 8 |
| 328 | G | admin-ui | `admin-ui/src/components/TaskCard.test.tsx` | 1,030 | 79 | under 1500 LOC | 1.829s measured | — |
| 329 | G | backend | `backend/testing/test_filter_file_conflicts.py` | 1,153 | 42 | under 1500 LOC | 1.767s measured | — |
| 330 | G | admin-ui | `admin-ui/src/components/ProjectResourceEnvelopeSection.test.tsx` | 555 | 16 | under 1500 LOC | 1.725s measured | — |
| 331 | G | backend | `backend/testing/test_digest_shared_cache.py` | 251 | 11 | under 1500 LOC | 1.699s measured | — |
| 332 | G | backend | `backend/testing/test_orchestrator_modularity.py` | 394 | 8 | under 1500 LOC | 1.685s measured | — |
| 333 | G | backend | `backend/testing/test_golden_suite.py` | 962 | 35 | under 1500 LOC | 1.658s measured | — |
| 334 | G | backend | `backend/testing/test_landed_signal.py` | 1,009 | 28 | under 1500 LOC | 1.653s measured | — |
| 335 | G | admin-ui | `admin-ui/src/components/StrategicContextArtifactSlot.test.tsx` | 981 | 30 | under 1500 LOC | 1.635s measured | — |
| 336 | G | agent | `agent/src/providers/codex-rate-limit-reset.test.ts` | 474 | 21 | under 1500 LOC | 1.620s measured | — |
| 337 | G | backend | `backend/testing/test_admin_scope_measurement.py` | 180 | 7 | under 1500 LOC | 1.592s measured | — |
| 338 | G | backend | `backend/testing/test_runner_isolation_gpu_markers.py` | 135 | 2 | under 1500 LOC | 1.580s measured | — |
| 339 | G | backend | `backend/testing/test_compute_lane_arm_time_medians.py` | 287 | 7 | under 1500 LOC | 1.576s measured | — |
| 340 | G | admin-ui | `admin-ui/src/components/DirectionCard.test.tsx` | 907 | 72 | under 1500 LOC | 1.575s measured | — |
| 341 | G | admin-ui | `admin-ui/src/components/MessagesComposer.test.tsx` | 182 | 12 | under 1500 LOC | 1.574s measured | — |
| 342 | G | backend | `backend/testing/test_compute_terminal_reason_history.py` | 189 | 4 | under 1500 LOC | 1.557s measured | — |
| 343 | G | backend | `backend/testing/test_migration_exemptions_admin.py` | 293 | 13 | under 1500 LOC | 1.547s measured | — |
| 344 | G | admin-ui | `admin-ui/src/components/SettingsNotificationsSection.test.tsx` | 384 | 15 | under 1500 LOC | 1.546s measured | — |
| 345 | G | backend | `backend/testing/test_issue_implementation.py` | 693 | 15 | under 1500 LOC | 1.493s measured | — |
| 346 | G | backend | `backend/testing/test_compute_zone_health.py` | 1,251 | 94 | under 1500 LOC | 1.492s measured | — |
| 347 | G | admin-ui | `admin-ui/src/components/TaskContextRail.test.tsx` | 469 | 18 | under 1500 LOC | 1.488s measured | — |
| 348 | G | backend | `backend/testing/test_admin_direction_events.py` | 179 | 5 | under 1500 LOC | 1.453s measured | — |
| 349 | G | admin-ui | `admin-ui/src/components/ProviderModelBreakdownPanel.test.tsx` | 346 | 13 | under 1500 LOC | 1.432s measured | — |
| 350 | G | admin-ui | `admin-ui/src/components/PipelineView.test.tsx` | 409 | 21 | under 1500 LOC | 1.430s measured | — |
| 351 | G | admin-ui | `admin-ui/src/hooks/useProjectResourceSummary.test.tsx` | 209 | 8 | under 1500 LOC | 1.380s measured | — |
| 352 | G | admin-ui | `admin-ui/src/components/AlertsBanner.test.tsx` | 194 | 12 | under 1500 LOC | 1.327s measured | — |
| 353 | G | backend | `backend/testing/test_lifecycle_crash_investigation.py` | 414 | 18 | under 1500 LOC | 1.326s measured | — |
| 354 | G | backend | `backend/testing/test_terminal_rollup_requeue.py` | 598 | 8 | under 1500 LOC | 1.300s measured | — |
| 355 | G | backend | `backend/testing/test_db_consistency_mediums.py` | 235 | 12 | under 1500 LOC | 1.296s measured | — |
| 356 | G | admin-ui | `admin-ui/src/components/ProjectToolHost.test.tsx` | 789 | 28 | under 1500 LOC | 1.289s measured | — |
| 357 | G | backend | `backend/testing/test_digest_precompute.py` | 787 | 22 | under 1500 LOC | 1.258s measured | — |
| 358 | G | admin-ui | `admin-ui/src/components/PRMonitorSection.test.tsx` | 411 | 24 | under 1500 LOC | 1.249s measured | — |
| 359 | G | admin-ui | `admin-ui/src/components/DirectionPortfolioSection.test.tsx` | 645 | 20 | under 1500 LOC | 1.225s measured | — |
| 360 | G | admin-ui | `admin-ui/src/components/EffortEfficiencyPanel.test.tsx` | 507 | 24 | under 1500 LOC | 1.199s measured | — |
| 361 | G | admin-ui | `admin-ui/src/components/KeyboardShortcutsOverlay.test.tsx` | 60 | 3 | under 1500 LOC | 1.165s measured | — |
| 362 | G | backend | `backend/testing/test_warm_pool_cost_metrics.py` | 625 | 17 | under 1500 LOC | 1.152s measured | — |
| 363 | G | admin-ui | `admin-ui/src/components/EvergreenFillObservabilityPanel.test.tsx` | 193 | 17 | under 1500 LOC | 1.146s measured | — |
| 364 | G | backend | `backend/testing/test_compute_experiment_draft.py` | 364 | 20 | under 1500 LOC | 1.141s measured | — |
| 365 | G | backend | `backend/testing/test_db.py` | 67 | 3 | under 1500 LOC | 1.132s measured | — |
| 366 | G | backend | `backend/testing/test_no_ci_merge_gate_setup_retry.py` | 484 | 5 | under 1500 LOC | 1.110s measured | — |
| 367 | G | backend | `backend/testing/test_findings_index.py` | 416 | 11 | under 1500 LOC | 1.097s measured | — |
| 368 | G | admin-ui | `admin-ui/src/components/NotesTool.test.tsx` | 424 | 14 | under 1500 LOC | 1.087s measured | protected; declared floor 14 |
| 369 | G | agent | `agent/src/handlers/strategic-resident-cold-fast.stream.test.ts` | 243 | 4 | under 1500 LOC | 1.051s measured | — |
| 370 | G | backend | `backend/testing/test_security.py` | 716 | 68 | under 1500 LOC | 0.981s measured | — |
| 371 | G | backend | `backend/testing/test_optimistic_version.py` | 199 | 3 | under 1500 LOC | 0.904s measured | — |
| 372 | G | backend | `backend/testing/test_c5_approval_gate_invariant.py` | 293 | 3 | under 1500 LOC | 0.894s measured | — |
| 373 | G | admin-ui | `admin-ui/src/components/DashboardMetricCards.test.tsx` | 208 | 23 | under 1500 LOC | 0.862s measured | — |
| 374 | G | backend | `backend/testing/test_task_direction_refs.py` | 50 | 3 | under 1500 LOC | 0.860s measured | — |
| 375 | G | admin-ui | `admin-ui/src/components/SettingsDeploySection.test.tsx` | 233 | 13 | under 1500 LOC | 0.809s measured | — |
| 376 | G | agent | `agent/src/prompts/terminal-checkpoint-helpers.test.ts` | 435 | 9 | under 1500 LOC | 0.807s measured | — |
| 377 | G | admin-ui | `admin-ui/src/components/PrimaryTabNav.test.tsx` | 257 | 12 | under 1500 LOC | 0.759s measured | — |
| 378 | G | admin-ui | `admin-ui/src/components/TaskDetailDrawerSections.test.tsx` | 1,016 | 81 | under 1500 LOC | 0.737s measured | — |
| 379 | G | admin-ui | `admin-ui/src/components/DirectionAutoPauseWidget.test.tsx` | 102 | 4 | under 1500 LOC | 0.737s measured | — |
| 380 | G | admin-ui | `admin-ui/src/hooks/useProjectConsultations.test.tsx` | 97 | 5 | under 1500 LOC | 0.723s measured | — |
| 381 | G | backend | `backend/testing/test_admin_cancel_impact.py` | 189 | 2 | under 1500 LOC | 0.701s measured | — |
| 382 | G | admin-ui | `admin-ui/eslint-rules/no-oversized-file.test.js` | 60 | 0 | under 1500 LOC | 0.693s measured | — |
| 383 | G | admin-ui | `admin-ui/src/components/StrategicContextWorkspace.test.tsx` | 775 | 20 | under 1500 LOC | 0.692s measured | protected; declared floor 20 |
| 384 | G | backend | `backend/testing/test_operator_attention_query_shape.py` | 524 | 11 | under 1500 LOC | 0.678s measured | — |
| 385 | G | backend | `backend/testing/test_direct_llm_client.py` | 613 | 28 | under 1500 LOC | 0.676s measured | — |
| 386 | G | backend | `backend/testing/test_task_completion_modularity.py` | 169 | 2 | under 1500 LOC | 0.670s measured | — |
| 387 | G | backend | `backend/testing/test_digest_cache.py` | 271 | 13 | under 1500 LOC | 0.635s measured | — |
| 388 | G | backend | `backend/testing/test_inline_llm.py` | 345 | 10 | under 1500 LOC | 0.626s measured | — |
| 389 | G | admin-ui | `admin-ui/eslint-rules/no-fixed-width-overflow.test.js` | 92 | 0 | under 1500 LOC | 0.605s measured | — |
| 390 | G | backend | `backend/testing/test_admin_notifications.py` | 85 | 2 | under 1500 LOC | 0.603s measured | — |
| 391 | G | admin-ui | `admin-ui/src/components/KanbanColumn.test.tsx` | 150 | 6 | under 1500 LOC | 0.591s measured | — |
| 392 | G | backend | `backend/testing/test_retry_budget.py` | 31 | 2 | under 1500 LOC | 0.586s measured | — |
| 393 | G | admin-ui | `admin-ui/src/components/LaneDiagPanel.test.tsx` | 307 | 16 | under 1500 LOC | 0.570s measured | — |
| 394 | G | admin-ui | `admin-ui/src/components/TaskComputeJobsSection.test.tsx` | 145 | 3 | under 1500 LOC | 0.557s measured | — |
| 395 | G | admin-ui | `admin-ui/src/components/KanbanBoard.test.tsx` | 244 | 12 | under 1500 LOC | 0.542s measured | — |
| 396 | G | admin-ui | `admin-ui/src/components/ReleaseQualificationFindings.test.tsx` | 179 | 8 | under 1500 LOC | 0.542s measured | — |
| 397 | G | backend | `backend/testing/test_orch_db_isolation.py` | 181 | 2 | under 1500 LOC | 0.536s measured | — |
| 398 | G | backend | `backend/testing/test_metrics_integrity.py` | 203 | 4 | under 1500 LOC | 0.535s measured | — |
| 399 | G | backend | `backend/testing/test_branch_resolution_alerts.py` | 100 | 2 | under 1500 LOC | 0.509s measured | — |
| 400 | G | admin-ui | `admin-ui/src/components/WarmReuseVsCostTrend.test.tsx` | 218 | 9 | under 1500 LOC | 0.492s measured | — |
| 401 | G | admin-ui | `admin-ui/src/components/FixLoopConvergenceWidget.test.tsx` | 156 | 2 | under 1500 LOC | 0.491s measured | — |
| 402 | G | admin-ui | `admin-ui/src/components/ScopeTrendsSection.test.tsx` | 234 | 17 | under 1500 LOC | 0.464s measured | — |
| 403 | G | admin-ui | `admin-ui/src/components/StrategicSessionControl.test.tsx` | 426 | 22 | under 1500 LOC | 0.459s measured | protected; declared floor 22 |
| 404 | G | admin-ui | `admin-ui/src/components/DriftSignalsCard.test.tsx` | 389 | 11 | under 1500 LOC | 0.450s measured | — |
| 405 | G | backend | `backend/testing/test_database_disconnect.py` | 511 | 27 | under 1500 LOC | 0.448s measured | — |
| 406 | G | backend | `backend/testing/test_spend_cap_trustworthiness.py` | 355 | 15 | under 1500 LOC | 0.439s measured | — |
| 407 | G | backend | `backend/testing/test_lock_ordering.py` | 152 | 2 | under 1500 LOC | 0.436s measured | — |
| 408 | G | admin-ui | `admin-ui/src/components/HeaderBar.test.tsx` | 136 | 5 | under 1500 LOC | 0.434s measured | — |
| 409 | G | backend | `backend/testing/test_digest_latency.py` | 200 | 10 | under 1500 LOC | 0.415s measured | — |
| 410 | G | admin-ui | `admin-ui/src/components/MarkdownContentInner.test.tsx` | 105 | 11 | under 1500 LOC | 0.413s measured | — |
| 411 | G | admin-ui | `admin-ui/src/hooks/useDirectionsData.readOnly.test.ts` | 342 | 6 | under 1500 LOC | 0.401s measured | — |
| 412 | G | admin-ui | `admin-ui/src/components/TaskDurationWidget.test.tsx` | 64 | 7 | under 1500 LOC | 0.399s measured | — |
| 413 | G | admin-ui | `admin-ui/src/components/BulkActionBar.test.tsx` | 142 | 5 | under 1500 LOC | 0.396s measured | — |
| 414 | G | admin-ui | `admin-ui/src/components/RuntimeAccountsPanel.test.tsx` | 250 | 7 | under 1500 LOC | 0.381s measured | — |
| 415 | G | admin-ui | `admin-ui/src/utils/renderWhiteboardArtifact.test.ts` | 217 | 8 | under 1500 LOC | 0.380s measured | — |
| 416 | G | admin-ui | `admin-ui/src/components/MetricsDashboard.test.tsx` | 751 | 14 | under 1500 LOC | 0.378s measured | — |
| 417 | G | backend | `backend/testing/test_task_capabilities.py` | 812 | 37 | under 1500 LOC | 0.363s measured | — |
| 418 | G | admin-ui | `admin-ui/src/hooks/useStrategicLiveness.test.tsx` | 102 | 6 | under 1500 LOC | 0.362s measured | protected; declared floor 6 |
| 419 | G | admin-ui | `admin-ui/src/components/MessagesSummaryCard.test.tsx` | 88 | 5 | under 1500 LOC | 0.360s measured | — |
| 420 | G | admin-ui | `admin-ui/src/components/Toasts.test.tsx` | 81 | 2 | under 1500 LOC | 0.348s measured | — |
| 421 | G | admin-ui | `admin-ui/src/components/SettingsSectionRouter.test.tsx` | 141 | 7 | under 1500 LOC | 0.347s measured | — |
| 422 | G | admin-ui | `admin-ui/src/components/MetricsIntegrityObservabilityPanel.test.tsx` | 128 | 3 | under 1500 LOC | 0.347s measured | — |
| 423 | G | admin-ui | `admin-ui/src/components/ComputeArtifactGallery.test.tsx` | 559 | 24 | under 1500 LOC | 0.341s measured | — |
| 424 | G | admin-ui | `admin-ui/src/components/SaveViewDialog.test.tsx` | 112 | 4 | under 1500 LOC | 0.335s measured | — |
| 425 | G | backend | `backend/testing/test_admin_digest_modularity.py` | 571 | 10 | under 1500 LOC | 0.335s measured | — |
| 426 | G | admin-ui | `admin-ui/src/components/FailureStageWidget.test.tsx` | 61 | 5 | under 1500 LOC | 0.313s measured | — |
| 427 | G | backend | `backend/testing/test_outer_loop_convergence.py` | 546 | 22 | under 1500 LOC | 0.311s measured | — |
| 428 | G | admin-ui | `admin-ui/src/components/MessageTypeDropdown.test.tsx` | 115 | 7 | under 1500 LOC | 0.309s measured | — |
| 429 | G | admin-ui | `admin-ui/src/components/SettingsAccountsSection.test.tsx` | 362 | 10 | under 1500 LOC | 0.303s measured | — |
| 430 | G | backend | `backend/testing/test_spawn_calibration.py` | 581 | 31 | under 1500 LOC | 0.302s measured | — |
| 431 | G | admin-ui | `admin-ui/src/components/MessageBubble.test.tsx` | 685 | 40 | under 1500 LOC | 0.296s measured | protected; declared floor 40 |
| 432 | G | admin-ui | `admin-ui/src/components/TaskDetailDrawerSideboardContext.test.tsx` | 325 | 7 | under 1500 LOC | 0.276s measured | — |
| 433 | G | admin-ui | `admin-ui/src/components/WarmStartOriginRow.test.tsx` | 33 | 3 | under 1500 LOC | 0.276s measured | — |
| 434 | G | admin-ui | `admin-ui/src/components/WhiteboardTool.test.tsx` | 687 | 14 | under 1500 LOC | 0.263s measured | protected; declared floor 14 |
| 435 | G | agent | `agent/src/prompts/code-change-helpers.blocked-receipt.test.ts` | 167 | 2 | under 1500 LOC | 0.263s measured | — |
| 436 | G | backend | `backend/testing/test_github_refs.py` | 252 | 10 | under 1500 LOC | 0.258s measured | — |
| 437 | G | backend | `backend/testing/test_deploy_smoketest.py` | 119 | 6 | under 1500 LOC | 0.242s measured | — |
| 438 | G | backend | `backend/testing/test_playbook_archive.py` | 1,016 | 35 | under 1500 LOC | 0.242s measured | — |
| 439 | G | admin-ui | `admin-ui/src/components/VersionLifecyclePanelPrimitives.test.tsx` | 1,063 | 103 | under 1500 LOC | 0.237s measured | — |
| 440 | G | admin-ui | `admin-ui/src/components/ProjectBuildConfigSection.test.tsx` | 129 | 2 | under 1500 LOC | 0.229s measured | — |
| 441 | G | admin-ui | `admin-ui/src/components/NullBranchDiagnosticsPanel.test.tsx` | 147 | 10 | under 1500 LOC | 0.228s measured | — |
| 442 | G | admin-ui | `admin-ui/src/components/ProjectMembers.test.tsx` | 111 | 5 | under 1500 LOC | 0.226s measured | — |
| 443 | G | backend | `backend/testing/test_source_ref_validation.py` | 1,469 | 78 | under 1500 LOC | 0.225s measured | — |
| 444 | G | agent | `agent/src/prompts/done-helper.test.ts` | 972 | 25 | under 1500 LOC | 0.221s measured | — |
| 445 | G | backend | `backend/testing/test_csrf.py` | 299 | 22 | under 1500 LOC | 0.218s measured | — |
| 446 | G | admin-ui | `admin-ui/src/hooks/useDirectionsData.test.ts` | 245 | 3 | under 1500 LOC | 0.216s measured | — |
| 447 | G | backend | `backend/testing/test_failure_classification_rate_limit.py` | 1,008 | 48 | under 1500 LOC | 0.214s measured | — |
| 448 | G | admin-ui | `admin-ui/src/components/RecurringPatternsSection.test.tsx` | 342 | 12 | under 1500 LOC | 0.212s measured | — |
| 449 | G | backend | `backend/testing/test_request_duration_telemetry.py` | 231 | 8 | under 1500 LOC | 0.204s measured | — |
| 450 | G | backend | `backend/testing/test_jwt_revocation.py` | 649 | 27 | under 1500 LOC | 0.203s measured | — |
| 451 | G | backend | `backend/testing/test_pause_clock.py` | 351 | 25 | under 1500 LOC | 0.202s measured | — |
| 452 | G | admin-ui | `admin-ui/src/components/SystemMaturityCard.test.tsx` | 106 | 13 | under 1500 LOC | 0.196s measured | — |
| 453 | G | admin-ui | `admin-ui/src/components/EvergreenBacklogPanel.test.tsx` | 140 | 12 | under 1500 LOC | 0.195s measured | — |
| 454 | G | admin-ui | `admin-ui/src/components/StrategicAuditPanel.test.tsx` | 247 | 13 | under 1500 LOC | 0.193s measured | — |
| 455 | G | admin-ui | `admin-ui/src/components/ProblemModelViewer.test.tsx` | 418 | 13 | under 1500 LOC | 0.180s measured | — |
| 456 | G | agent | `agent/src/viewport-snapshot-script.test.ts` | 329 | 16 | under 1500 LOC | 0.178s measured | — |
| 457 | G | admin-ui | `admin-ui/src/hooks/useConversation.test.tsx` | 941 | 29 | under 1500 LOC | 0.174s measured | protected; declared floor 29 |
| 458 | G | backend | `backend/testing/test_chain_continuity_funnel.py` | 135 | 5 | under 1500 LOC | 0.173s measured | — |
| 459 | G | admin-ui | `admin-ui/src/components/ConsultationProgressRow.test.tsx` | 156 | 5 | under 1500 LOC | 0.171s measured | — |
| 460 | G | admin-ui | `admin-ui/src/components/ProjectManagementModal.test.tsx` | 227 | 15 | under 1500 LOC | 0.170s measured | — |
| 461 | G | admin-ui | `admin-ui/src/components/ProjectAlertDomainsSection.test.tsx` | 77 | 3 | under 1500 LOC | 0.168s measured | — |
| 462 | G | admin-ui | `admin-ui/src/api.test.ts` | 557 | 29 | under 1500 LOC | 0.166s measured | — |
| 463 | G | backend | `backend/testing/test_env_preflight_evidence.py` | 284 | 17 | under 1500 LOC | 0.162s measured | — |
| 464 | G | agent | `agent/src/sr-pr-section.test.ts` | 206 | 15 | under 1500 LOC | 0.162s measured | — |
| 465 | G | admin-ui | `admin-ui/src/components/FleetHealthCard.test.tsx` | 325 | 16 | under 1500 LOC | 0.161s measured | — |
| 466 | G | admin-ui | `admin-ui/src/components/AttachmentPreviewModal.test.tsx` | 204 | 15 | under 1500 LOC | 0.161s measured | — |
| 467 | G | admin-ui | `admin-ui/src/components/catchupHelpers.test.ts` | 126 | 17 | under 1500 LOC | 0.161s measured | — |
| 468 | G | agent | `agent/src/mock-factory-export-coverage.test.ts` | 690 | 13 | under 1500 LOC | 0.159s measured | — |
| 469 | G | backend | `backend/testing/test_compute_image_remote_build.py` | 1,142 | 41 | under 1500 LOC | 0.156s measured | — |
| 470 | G | backend | `backend/testing/test_research_completion__source_refs.py` | 1,090 | 76 | under 1500 LOC | 0.155s measured | — |
| 471 | G | admin-ui | `admin-ui/src/components/RecentErrorsWidget.test.tsx` | 105 | 4 | under 1500 LOC | 0.155s measured | — |
| 472 | G | agent | `agent/src/prompts/task-type-focus.test.ts` | 634 | 80 | under 1500 LOC | 0.154s measured | — |
| 473 | G | admin-ui | `admin-ui/src/components/OrchestratorHealthBar.test.tsx` | 486 | 24 | under 1500 LOC | 0.154s measured | — |
| 474 | G | admin-ui | `admin-ui/src/components/UILoadTimingPanel.test.tsx` | 338 | 27 | under 1500 LOC | 0.150s measured | — |
| 475 | G | admin-ui | `admin-ui/src/components/ConsoleHome.test.tsx` | 201 | 7 | under 1500 LOC | 0.146s measured | — |
| 476 | G | backend | `backend/testing/test_deepseek_cost_attribution.py` | 872 | 31 | under 1500 LOC | 0.145s measured | — |
| 477 | G | admin-ui | `admin-ui/src/firebase.test.ts` | 46 | 2 | under 1500 LOC | 0.145s measured | — |
| 478 | G | backend | `backend/testing/test_event_fanout.py` | 844 | 46 | under 1500 LOC | 0.139s measured | — |
| 479 | G | backend | `backend/testing/test_leader_stale_reaper.py` | 247 | 8 | under 1500 LOC | 0.134s measured | — |
| 480 | G | admin-ui | `admin-ui/src/components/ErrorBoundary.test.tsx` | 144 | 11 | under 1500 LOC | 0.132s measured | — |
| 481 | G | agent | `agent/src/prompts/diagnostics-after-edit-hook.test.ts` | 77 | 0 | under 1500 LOC | 0.132s measured | — |
| 482 | G | admin-ui | `admin-ui/src/components/ProjectWorkspace.readOnly.test.tsx` | 134 | 5 | under 1500 LOC | 0.131s measured | — |
| 483 | G | admin-ui | `admin-ui/src/components/ProviderCostPanel.test.tsx` | 320 | 10 | under 1500 LOC | 0.130s measured | — |
| 484 | G | admin-ui | `admin-ui/src/components/PoolAccountHealthPanel.test.tsx` | 348 | 15 | under 1500 LOC | 0.128s measured | — |
| 485 | G | admin-ui | `admin-ui/src/components/ProjectAutoReviewSection.test.tsx` | 50 | 2 | under 1500 LOC | 0.126s measured | — |
| 486 | G | admin-ui | `admin-ui/src/components/MuteWindowMenu.test.tsx` | 66 | 4 | under 1500 LOC | 0.125s measured | — |
| 487 | G | admin-ui | `admin-ui/src/hooks/useTasksState.test.ts` | 365 | 18 | under 1500 LOC | 0.124s measured | — |
| 488 | G | agent | `agent/src/guard-suppressions-pr-section.test.ts` | 200 | 11 | under 1500 LOC | 0.124s measured | — |
| 489 | G | admin-ui | `admin-ui/src/components/MessageAttachments.test.tsx` | 75 | 3 | under 1500 LOC | 0.123s measured | — |
| 490 | G | agent | `agent/src/vitest.globalSetup.test.ts` | 237 | 16 | under 1500 LOC | 0.122s measured | — |
| 491 | G | admin-ui | `admin-ui/src/components/FleetCapacityCard.test.tsx` | 320 | 22 | under 1500 LOC | 0.121s measured | — |
| 492 | G | admin-ui | `admin-ui/eslint-rules/no-arbitrary-font-size.test.js` | 128 | 0 | under 1500 LOC | 0.120s measured | — |
| 493 | G | admin-ui | `admin-ui/src/uiLoadTiming.test.ts` | 66 | 4 | under 1500 LOC | 0.120s measured | — |
| 494 | G | admin-ui | `admin-ui/src/components/CapabilityTrialsPanel.test.tsx` | 161 | 11 | under 1500 LOC | 0.117s measured | — |
| 495 | G | admin-ui | `admin-ui/src/utils/attachments.test.ts` | 82 | 5 | under 1500 LOC | 0.117s measured | — |
| 496 | G | backend | `backend/testing/test_shared_state.py` | 610 | 29 | under 1500 LOC | 0.113s measured | — |
| 497 | G | admin-ui | `admin-ui/src/components/SettingsGeneralSection.test.tsx` | 105 | 8 | under 1500 LOC | 0.113s measured | — |
| 498 | G | agent | `agent/src/screenshots-pr-section.test.ts` | 177 | 10 | under 1500 LOC | 0.112s measured | — |
| 499 | G | backend | `backend/testing/test_migration_0626_safe_encrypt_backfill.py` | 344 | 6 | under 1500 LOC | 0.110s measured | — |
| 500 | G | admin-ui | `admin-ui/src/components/DependencyHealthCard.test.tsx` | 196 | 9 | under 1500 LOC | 0.108s measured | — |
| 501 | G | backend | `backend/testing/test_filter_scope_empty.py` | 342 | 12 | under 1500 LOC | 0.106s measured | — |
| 502 | G | backend | `backend/testing/test_direction_referential_guard.py` | 472 | 33 | under 1500 LOC | 0.105s measured | — |
| 503 | G | agent | `agent/src/handlers/resident-repo-freshness.test.ts` | 97 | 4 | under 1500 LOC | 0.104s measured | — |
| 504 | G | backend | `backend/testing/test_github_repo_snapshot.py` | 575 | 30 | under 1500 LOC | 0.096s measured | — |
| 505 | G | backend | `backend/testing/test_account_model_availability.py` | 829 | 32 | under 1500 LOC | 0.094s measured | — |
| 506 | G | backend | `backend/testing/test_backmerge_invariants.py` | 201 | 3 | under 1500 LOC | 0.093s measured | — |
| 507 | G | agent | `agent/src/providers/runners.test.ts` | 941 | 44 | under 1500 LOC | 0.093s measured | — |
| 508 | G | backend | `backend/testing/test_sweep_stale_state.py` | 202 | 11 | under 1500 LOC | 0.092s measured | — |
| 509 | G | admin-ui | `admin-ui/eslint-rules/min-touch-target.test.js` | 105 | 0 | under 1500 LOC | 0.091s measured | — |
| 510 | G | admin-ui | `admin-ui/src/test/viewport-coverage.test.ts` | 263 | 18 | under 1500 LOC | 0.090s measured | — |
| 511 | G | admin-ui | `admin-ui/src/components/AccountMenu.test.tsx` | 116 | 6 | under 1500 LOC | 0.090s measured | — |
| 512 | G | admin-ui | `admin-ui/src/components/TaskStreamPanel.test.tsx` | 80 | 3 | under 1500 LOC | 0.090s measured | — |
| 513 | G | backend | `backend/testing/test_models.py` | 532 | 34 | under 1500 LOC | 0.089s measured | — |
| 514 | G | admin-ui | `admin-ui/src/hooks/__tests__/useLogViewer.test.ts` | 511 | 8 | under 1500 LOC | 0.086s measured | — |
| 515 | G | backend | `backend/testing/test_evidence_primitive.py` | 543 | 52 | under 1500 LOC | 0.086s measured | — |
| 516 | G | backend | `backend/testing/test_impacted_tests.py` | 881 | 39 | under 1500 LOC | 0.082s measured | — |
| 517 | G | admin-ui | `admin-ui/src/components/StrategicContextMap.test.tsx` | 140 | 5 | under 1500 LOC | 0.081s measured | — |
| 518 | G | admin-ui | `admin-ui/src/components/EventVisibilityMenu.test.tsx` | 87 | 5 | under 1500 LOC | 0.081s measured | — |
| 519 | G | admin-ui | `admin-ui/src/components/ScopeConversionRatesSection.test.tsx` | 377 | 26 | under 1500 LOC | 0.080s measured | — |
| 520 | G | backend | `backend/testing/test_field_encryption.py` | 504 | 44 | under 1500 LOC | 0.078s measured | — |
| 521 | G | backend | `backend/testing/test_direction_health.py` | 431 | 28 | under 1500 LOC | 0.078s measured | — |
| 522 | G | backend | `backend/testing/test_design_doc_grounding.py` | 771 | 30 | under 1500 LOC | 0.075s measured | — |
| 523 | G | admin-ui | `admin-ui/src/components/messageArming.test.ts` | 146 | 19 | under 1500 LOC | 0.075s measured | — |
| 524 | G | backend | `backend/testing/test_measure_repair_success.py` | 127 | 9 | under 1500 LOC | 0.075s measured | — |
| 525 | G | backend | `backend/testing/test_design_web_url_reachability.py` | 282 | 20 | under 1500 LOC | 0.073s measured | — |
| 526 | G | backend | `backend/testing/test_migration_env.py` | 197 | 13 | under 1500 LOC | 0.072s measured | — |
| 527 | G | backend | `backend/testing/test_direction_outcome_metric_manifest.py` | 155 | 5 | under 1500 LOC | 0.071s measured | — |
| 528 | G | backend | `backend/testing/test_namespace_backfill.py` | 230 | 13 | under 1500 LOC | 0.071s measured | — |
| 529 | G | admin-ui | `admin-ui/src/hooks/useWorkbenchShortcutRouter.test.tsx` | 194 | 6 | under 1500 LOC | 0.071s measured | — |
| 530 | G | admin-ui | `admin-ui/src/components/RailMiniCanvas.test.tsx` | 191 | 24 | under 1500 LOC | 0.071s measured | — |
| 531 | G | admin-ui | `admin-ui/src/components/SystemStatusCard.test.tsx` | 117 | 9 | under 1500 LOC | 0.071s measured | — |
| 532 | G | backend | `backend/testing/test_migration_0621_safe_encrypt_backfill.py` | 259 | 4 | under 1500 LOC | 0.069s measured | — |
| 533 | G | backend | `backend/testing/test_build_config_detection.py` | 608 | 11 | under 1500 LOC | 0.067s measured | — |
| 534 | G | admin-ui | `admin-ui/eslint-rules/no-surface-colour.test.js` | 82 | 0 | under 1500 LOC | 0.066s measured | — |
| 535 | G | admin-ui | `admin-ui/eslint-rules/require-workbench-focus.test.js` | 75 | 0 | under 1500 LOC | 0.066s measured | — |
| 536 | G | backend | `backend/testing/test_knowledge_completion.py` | 355 | 26 | under 1500 LOC | 0.066s measured | — |
| 537 | G | backend | `backend/testing/test_migration_0609_reconcile_encrypted_columns.py` | 337 | 8 | under 1500 LOC | 0.065s measured | — |
| 538 | G | admin-ui | `admin-ui/src/components/MediumScopeWidget.test.tsx` | 320 | 28 | under 1500 LOC | 0.065s measured | — |
| 539 | G | agent | `agent/src/providers/call-resident-tool.test.ts` | 46 | 2 | under 1500 LOC | 0.064s measured | — |
| 540 | G | backend | `backend/testing/test_materialize_manifest.py` | 710 | 25 | under 1500 LOC | 0.063s measured | — |
| 541 | G | admin-ui | `admin-ui/src/components/StrategicSessionEvents.test.tsx` | 120 | 4 | under 1500 LOC | 0.062s measured | protected; declared floor 4 |
| 542 | G | backend | `backend/testing/test_direction_event_trail.py` | 285 | 20 | under 1500 LOC | 0.062s measured | — |
| 543 | G | admin-ui | `admin-ui/src/components/LlmCostBreakdownPanel.test.tsx` | 136 | 6 | under 1500 LOC | 0.061s measured | — |
| 544 | G | backend | `backend/testing/test_broadcast.py` | 300 | 36 | under 1500 LOC | 0.060s measured | — |
| 545 | G | backend | `backend/testing/test_deployed_sha.py` | 667 | 29 | under 1500 LOC | 0.059s measured | — |
| 546 | G | backend | `backend/testing/test_digest_payload_metric_manifest.py` | 256 | 6 | under 1500 LOC | 0.058s measured | — |
| 547 | G | admin-ui | `admin-ui/src/components/SavedViewsTabStrip.test.tsx` | 88 | 8 | under 1500 LOC | 0.056s measured | — |
| 548 | G | admin-ui | `admin-ui/src/hooks/useCommandPaletteShortcut.test.tsx` | 205 | 10 | under 1500 LOC | 0.054s measured | — |
| 549 | G | agent | `agent/src/providers/resident-reply-tool.test.ts` | 113 | 3 | under 1500 LOC | 0.051s measured | — |
| 550 | G | admin-ui | `admin-ui/src/components/EvidenceTierBadge.test.tsx` | 98 | 16 | under 1500 LOC | 0.051s measured | — |
| 551 | G | backend | `backend/testing/test_json_extract.py` | 258 | 30 | under 1500 LOC | 0.051s measured | — |
| 552 | G | backend | `backend/testing/test_capability_execution_fence.py` | 329 | 19 | under 1500 LOC | 0.050s measured | — |
| 553 | G | admin-ui | `admin-ui/src/hooks/useOperatorKeyboardShortcuts.test.tsx` | 355 | 17 | under 1500 LOC | 0.049s measured | — |
| 554 | G | admin-ui | `admin-ui/src/components/ConversationUtilityBar.test.tsx` | 716 | 22 | under 1500 LOC | 0.048s measured | protected; declared floor 22 |
| 555 | G | admin-ui | `admin-ui/src/components/SystemHealthCard.test.tsx` | 139 | 11 | under 1500 LOC | 0.048s measured | — |
| 556 | G | admin-ui | `admin-ui/src/components/ReleaseEvidence.test.tsx` | 186 | 16 | under 1500 LOC | 0.047s measured | — |
| 557 | G | admin-ui | `admin-ui/src/hooks/useProjectMembers.test.tsx` | 92 | 3 | under 1500 LOC | 0.047s measured | — |
| 558 | G | admin-ui | `admin-ui/src/hooks/useStrategicReadModel.test.tsx` | 414 | 17 | under 1500 LOC | 0.046s measured | — |
| 559 | G | admin-ui | `admin-ui/eslint-rules/canonical-radius.test.js` | 66 | 0 | under 1500 LOC | 0.046s measured | — |
| 560 | G | admin-ui | `admin-ui/src/components/ui/Badge.test.tsx` | 110 | 14 | under 1500 LOC | 0.045s measured | — |
| 561 | G | admin-ui | `admin-ui/src/components/ProjectReadOnlyEditors.test.tsx` | 95 | 2 | under 1500 LOC | 0.045s measured | — |
| 562 | G | admin-ui | `admin-ui/src/components/WhiteboardRailSection.test.tsx` | 91 | 5 | under 1500 LOC | 0.045s measured | — |
| 563 | G | admin-ui | `admin-ui/src/components/ParkWakeHealthCard.test.tsx` | 61 | 6 | under 1500 LOC | 0.045s measured | — |
| 564 | G | backend | `backend/testing/test_dispatch_ordering.py` | 578 | 29 | under 1500 LOC | 0.045s measured | — |
| 565 | G | backend | `backend/testing/test_config_sync.py` | 486 | 22 | under 1500 LOC | 0.043s measured | — |
| 566 | G | admin-ui | `admin-ui/src/components/DirectionsPanel.readOnly.test.tsx` | 124 | 1 | under 1500 LOC | 0.042s measured | — |
| 567 | G | admin-ui | `admin-ui/src/components/StrategicChallengeChip.test.tsx` | 71 | 7 | under 1500 LOC | 0.042s measured | — |
| 568 | G | backend | `backend/testing/test_context_bounds.py` | 302 | 21 | under 1500 LOC | 0.041s measured | — |
| 569 | G | backend | `backend/testing/test_direction_metrics.py` | 248 | 21 | under 1500 LOC | 0.040s measured | — |
| 570 | G | admin-ui | `admin-ui/src/components/ProjectResourceAvailabilityCard.test.tsx` | 75 | 3 | under 1500 LOC | 0.040s measured | — |
| 571 | G | admin-ui | `admin-ui/eslint-rules/no-disabled-react-hooks.test.js` | 74 | 1 | under 1500 LOC | 0.040s measured | — |
| 572 | G | admin-ui | `admin-ui/src/components/ToolResidentHandoffControls.test.tsx` | 48 | 3 | under 1500 LOC | 0.040s measured | protected; declared floor 3 |
| 573 | G | agent | `agent/src/handlers/verify-tests.integration.test.ts` | 120 | 1 | under 1500 LOC | 0.038s measured | — |
| 574 | G | admin-ui | `admin-ui/src/hooks/usePoller.test.ts` | 187 | 11 | under 1500 LOC | 0.037s measured | — |
| 575 | G | admin-ui | `admin-ui/src/components/CompletionDebtPanel.test.tsx` | 48 | 1 | under 1500 LOC | 0.037s measured | — |
| 576 | G | admin-ui | `admin-ui/src/hooks/useUrlFilterState.test.ts` | 149 | 14 | under 1500 LOC | 0.036s measured | — |
| 577 | G | backend | `backend/testing/test_storage_signed_url.py` | 124 | 4 | under 1500 LOC | 0.035s measured | — |
| 578 | G | admin-ui | `admin-ui/src/components/MergeGateFleetHealthCard.test.tsx` | 77 | 7 | under 1500 LOC | 0.033s measured | — |
| 579 | G | backend | `backend/testing/test_build_config_lint.py` | 129 | 15 | under 1500 LOC | 0.033s measured | — |
| 580 | G | admin-ui | `admin-ui/src/components/ProjectToolIframe.test.tsx` | 95 | 2 | under 1500 LOC | 0.032s measured | — |
| 581 | G | backend | `backend/testing/test_cochange_decomposition.py` | 277 | 20 | under 1500 LOC | 0.031s measured | — |
| 582 | G | agent | `agent/src/providers/resident-session.test.ts` | 1,067 | 48 | under 1500 LOC | 0.031s measured | protected; declared floor 48 |
| 583 | G | admin-ui | `admin-ui/src/hooks/useSearchFilters.test.ts` | 197 | 22 | under 1500 LOC | 0.031s measured | — |
| 584 | G | backend | `backend/testing/test_cors_on_errors.py` | 141 | 6 | under 1500 LOC | 0.030s measured | — |
| 585 | G | agent | `agent/src/handlers/verify-tests.test.ts` | 1,356 | 62 | under 1500 LOC | 0.030s measured | — |
| 586 | G | admin-ui | `admin-ui/src/hooks/dashboardAccessGating.test.tsx` | 97 | 2 | under 1500 LOC | 0.030s measured | — |
| 587 | G | admin-ui | `admin-ui/src/components/PipelineEfficiencyPanel.test.tsx` | 81 | 4 | under 1500 LOC | 0.030s measured | — |
| 588 | G | admin-ui | `admin-ui/src/components/useMessageList.test.ts` | 223 | 14 | under 1500 LOC | 0.029s measured | — |
| 589 | G | admin-ui | `admin-ui/src/utils/taskCsv.test.ts` | 125 | 6 | under 1500 LOC | 0.029s measured | — |
| 590 | G | backend | `backend/testing/test_merge_size_limits.py` | 166 | 13 | under 1500 LOC | 0.029s measured | — |
| 591 | G | admin-ui | `admin-ui/src/test/viewport-diff.test.ts` | 98 | 4 | under 1500 LOC | 0.028s measured | — |
| 592 | G | admin-ui | `admin-ui/src/hooks/usePersistedState.test.ts` | 87 | 9 | under 1500 LOC | 0.028s measured | — |
| 593 | G | admin-ui | `admin-ui/src/components/SearchFilterBar.test.tsx` | 23 | 1 | under 1500 LOC | 0.027s measured | — |
| 594 | G | admin-ui | `admin-ui/src/components/AnomalyThrottleBanner.test.tsx` | 54 | 3 | under 1500 LOC | 0.026s measured | — |
| 595 | G | agent | `agent/src/prompt-helper-refs.test.ts` | 326 | 5 | under 1500 LOC | 0.025s measured | — |
| 596 | G | admin-ui | `admin-ui/src/utils/strategicWorkSessionMetrics.test.ts` | 40 | 2 | under 1500 LOC | 0.025s measured | — |
| 597 | G | admin-ui | `admin-ui/src/hooks/useVersionLifecycle.test.tsx` | 33 | 1 | under 1500 LOC | 0.025s measured | — |
| 598 | G | backend | `backend/testing/test_notification.py` | 134 | 5 | under 1500 LOC | 0.025s measured | — |
| 599 | G | backend | `backend/testing/test_c18_check_runs_cache_error_bypass.py` | 330 | 8 | under 1500 LOC | 0.025s measured | — |
| 600 | G | backend | `backend/testing/test_branch_resolution.py` | 196 | 15 | under 1500 LOC | 0.024s measured | — |
| 601 | G | backend | `backend/testing/test_github_checks.py` | 670 | 24 | under 1500 LOC | 0.023s measured | — |
| 602 | G | backend | `backend/testing/test_pr_is_not_behind_counter.py` | 224 | 13 | under 1500 LOC | 0.023s measured | — |
| 603 | G | agent | `agent/src/handlers/typed-fast-resident.test.ts` | 481 | 16 | under 1500 LOC | 0.023s measured | protected; declared floor 16 |
| 604 | G | admin-ui | `admin-ui/src/__tests__/strategicReconnect.test.tsx` | 160 | 2 | under 1500 LOC | 0.023s measured | — |
| 605 | G | admin-ui | `admin-ui/src/hooks/useConversationSend.test.tsx` | 85 | 5 | under 1500 LOC | 0.023s measured | — |
| 606 | G | backend | `backend/testing/test_migration_0273_normalise_indexes.py` | 158 | 5 | under 1500 LOC | 0.022s measured | — |
| 607 | G | admin-ui | `admin-ui/src/hooks/useStrategicContextNavigation.test.tsx` | 253 | 7 | under 1500 LOC | 0.022s measured | — |
| 608 | G | backend | `backend/testing/test_migration_0531_backfill_encryption.py` | 203 | 2 | under 1500 LOC | 0.022s measured | — |
| 609 | G | backend | `backend/testing/test_direction_mode.py` | 126 | 12 | under 1500 LOC | 0.021s measured | — |
| 610 | G | backend | `backend/testing/test_migration_0141_reconcile.py` | 121 | 3 | under 1500 LOC | 0.021s measured | — |
| 611 | G | backend | `backend/testing/test_migration_path.py` | 72 | 10 | under 1500 LOC | 0.021s measured | — |
| 612 | G | agent | `agent/src/handlers/artifacts-helper.test.ts` | 151 | 4 | under 1500 LOC | 0.021s measured | — |
| 613 | G | backend | `backend/testing/test_migration_reconciliation.py` | 111 | 7 | under 1500 LOC | 0.021s measured | — |
| 614 | G | admin-ui | `admin-ui/src/hooks/useStrategistStatus.test.ts` | 120 | 10 | under 1500 LOC | 0.020s measured | — |
| 615 | G | admin-ui | `admin-ui/src/hooks/useProjectConfig.test.tsx` | 68 | 1 | under 1500 LOC | 0.020s measured | — |
| 616 | G | agent | `agent/src/utils/test-fix-loop.test.ts` | 1,355 | 62 | under 1500 LOC | 0.019s measured | — |
| 617 | G | admin-ui | `admin-ui/src/strategicStateApi.test.ts` | 562 | 25 | under 1500 LOC | 0.019s measured | — |
| 618 | G | agent | `agent/src/handlers/artifact-consume.test.ts` | 436 | 19 | under 1500 LOC | 0.019s measured | — |
| 619 | G | admin-ui | `admin-ui/src/utils/residentToolHandoff.test.ts` | 129 | 7 | under 1500 LOC | 0.019s measured | protected; declared floor 7 |
| 620 | G | admin-ui | `admin-ui/src/hooks/useViewPreferences.test.ts` | 40 | 5 | under 1500 LOC | 0.019s measured | — |
| 621 | G | backend | `backend/testing/test_account_model_regression_stats.py` | 148 | 12 | under 1500 LOC | 0.019s measured | — |
| 622 | G | backend | `backend/testing/test_migration_0532_message_backlog.py` | 190 | 3 | under 1500 LOC | 0.018s measured | — |
| 623 | G | backend | `backend/testing/test_message_audience_routing.py` | 165 | 10 | under 1500 LOC | 0.018s measured | — |
| 624 | G | backend | `backend/testing/test_ci_infra_rerun.py` | 104 | 10 | under 1500 LOC | 0.017s measured | — |
| 625 | G | agent | `agent/src/handlers/index.test.ts` | 635 | 37 | under 1500 LOC | 0.017s measured | protected; declared floor 37 |
| 626 | G | admin-ui | `admin-ui/src/utils/strategicContextDescriptor.test.ts` | 343 | 10 | under 1500 LOC | 0.017s measured | — |
| 627 | G | admin-ui | `admin-ui/src/components/SrBadges.test.tsx` | 43 | 1 | under 1500 LOC | 0.017s measured | — |
| 628 | G | backend | `backend/testing/test_classifier_fixtures.py` | 255 | 10 | under 1500 LOC | 0.016s measured | — |
| 629 | G | backend | `backend/testing/test_research_actionability.py` | 204 | 5 | under 1500 LOC | 0.016s measured | — |
| 630 | G | backend | `backend/testing/test_migration_0151_reconcile.py` | 98 | 5 | under 1500 LOC | 0.015s measured | — |
| 631 | G | agent | `agent/src/handlers/design.test.ts` | 864 | 36 | under 1500 LOC | 0.015s measured | — |
| 632 | G | admin-ui | `admin-ui/src/utils/directionHelpers.test.ts` | 140 | 17 | under 1500 LOC | 0.015s measured | — |
| 633 | G | agent | `agent/src/prompt.context.test.ts` | 1,434 | 109 | under 1500 LOC | 0.014s measured | — |
| 634 | G | admin-ui | `admin-ui/src/utils/mergeTestVerdict.test.ts` | 374 | 36 | under 1500 LOC | 0.014s measured | — |
| 635 | G | admin-ui | `admin-ui/src/components/ComputeJobsPanel.utils.test.ts` | 217 | 19 | under 1500 LOC | 0.014s measured | — |
| 636 | G | admin-ui | `admin-ui/src/utils/citationLinks.test.ts` | 77 | 13 | under 1500 LOC | 0.014s measured | — |
| 637 | G | backend | `backend/testing/test_event_type_invariants.py` | 320 | 7 | under 1500 LOC | 0.013s measured | — |
| 638 | G | agent | `agent/src/handlers/code-change.test.ts` | 941 | 36 | under 1500 LOC | 0.013s measured | — |
| 639 | G | agent | `agent/src/reset.test.ts` | 722 | 32 | under 1500 LOC | 0.013s measured | — |
| 640 | G | admin-ui | `admin-ui/src/operatorWorkbenchContracts.test.ts` | 551 | 3 | under 1500 LOC | 0.013s measured | — |
| 641 | G | backend | `backend/testing/test_migration_0472_no_double_encrypt.py` | 147 | 3 | under 1500 LOC | 0.012s measured | — |
| 642 | G | agent | `agent/src/moduleSize.contract.test.ts` | 156 | 4 | under 1500 LOC | 0.012s measured | — |
| 643 | G | admin-ui | `admin-ui/src/utils/taskStatus.test.ts` | 102 | 21 | under 1500 LOC | 0.012s measured | — |
| 644 | G | admin-ui | `admin-ui/src/utils/mathContent.test.ts` | 59 | 12 | under 1500 LOC | 0.012s measured | — |
| 645 | G | agent | `agent/src/handlers/walkthrough-e2e.test.ts` | 746 | 29 | under 1500 LOC | 0.011s measured | — |
| 646 | G | admin-ui | `admin-ui/src/utils/strategicSessionLive.test.ts` | 541 | 38 | under 1500 LOC | 0.011s measured | protected; declared floor 38 |
| 647 | G | agent | `agent/src/handlers/design-doc.test.ts` | 325 | 19 | under 1500 LOC | 0.011s measured | — |
| 648 | G | admin-ui | `admin-ui/src/components/boardSavedViews.test.ts` | 131 | 11 | under 1500 LOC | 0.011s measured | — |
| 649 | G | agent | `agent/src/prompts/context-prefix.test.ts` | 1,205 | 68 | under 1500 LOC | 0.010s measured | — |
| 650 | G | agent | `agent/src/prompts/discovery.test.ts` | 668 | 45 | under 1500 LOC | 0.010s measured | — |
| 651 | G | agent | `agent/src/handlers/audits-reopen.test.ts` | 390 | 13 | under 1500 LOC | 0.010s measured | — |
| 652 | G | agent | `agent/src/call-speech/call-speech.test.ts` | 309 | 13 | under 1500 LOC | 0.010s measured | — |
| 653 | G | admin-ui | `admin-ui/src/utils/pendingWhiteboardArtifactRef.test.ts` | 222 | 6 | under 1500 LOC | 0.010s measured | protected; declared floor 6 |
| 654 | G | admin-ui | `admin-ui/src/accountManager.test.ts` | 191 | 9 | under 1500 LOC | 0.010s measured | — |
| 655 | G | admin-ui | `admin-ui/src/utils/parseTimestamp-guard.test.ts` | 155 | 15 | under 1500 LOC | 0.010s measured | — |
| 656 | G | backend | `backend/testing/test_compute_cloud_run.py` | 968 | 13 | under 1500 LOC | 0.009s measured | — |
| 657 | G | agent | `agent/src/handlers/memo.test.ts` | 850 | 40 | under 1500 LOC | 0.009s measured | — |
| 658 | G | agent | `agent/src/handlers/web-research.test.ts` | 653 | 28 | under 1500 LOC | 0.009s measured | — |
| 659 | G | agent | `agent/src/handlers/capability-proof-trial.test.ts` | 404 | 27 | under 1500 LOC | 0.009s measured | — |
| 660 | G | agent | `agent/src/media/livekit-call-media.test.ts` | 382 | 13 | under 1500 LOC | 0.009s measured | — |
| 661 | G | admin-ui | `admin-ui/src/components/MessagesPanel.invariant.test.ts` | 212 | 9 | under 1500 LOC | 0.009s measured | — |
| 662 | G | admin-ui | `admin-ui/src/App.wiring.test.ts` | 117 | 13 | under 1500 LOC | 0.009s measured | protected; declared floor 13 |
| 663 | G | admin-ui | `admin-ui/src/utils/pendingAttachmentStore.test.ts` | 77 | 3 | under 1500 LOC | 0.009s measured | protected; declared floor 3 |
| 664 | G | admin-ui | `admin-ui/src/utils/strategicWorkbenchSession.test.ts` | 66 | 4 | under 1500 LOC | 0.009s measured | — |
| 665 | G | backend | `backend/testing/test_migration_0457_encrypt_github_pat.py` | 82 | 2 | under 1500 LOC | 0.009s measured | — |
| 666 | G | agent | `agent/src/telemetry.test.ts` | 773 | 62 | under 1500 LOC | 0.008s measured | — |
| 667 | G | agent | `agent/src/prompts/self-review.test.ts` | 610 | 66 | under 1500 LOC | 0.008s measured | — |
| 668 | G | agent | `agent/src/utils/provisioning.test.ts` | 402 | 31 | under 1500 LOC | 0.008s measured | — |
| 669 | G | agent | `agent/src/handlers/criterion-named-test-trial.test.ts` | 376 | 17 | under 1500 LOC | 0.008s measured | — |
| 670 | G | admin-ui | `admin-ui/src/components/eventPrefs.test.ts` | 320 | 35 | under 1500 LOC | 0.008s measured | — |
| 671 | G | agent | `agent/src/utils/agent-token.test.ts` | 312 | 21 | under 1500 LOC | 0.008s measured | — |
| 672 | G | agent | `agent/src/handlers/live-conversation-resident.test.ts` | 257 | 13 | under 1500 LOC | 0.008s measured | — |
| 673 | G | admin-ui | `admin-ui/src/utils/conversationDraftState.test.ts` | 111 | 6 | under 1500 LOC | 0.008s measured | — |
| 674 | G | agent | `agent/src/providers/resident-stream.test.ts` | 380 | 45 | under 1500 LOC | 0.007s measured | protected; declared floor 45 |
| 675 | G | agent | `agent/src/handlers/strategic-ops-registry.test.ts` | 199 | 13 | under 1500 LOC | 0.007s measured | — |
| 676 | G | admin-ui | `admin-ui/src/utils/outcomeKinds.test.ts` | 185 | 19 | under 1500 LOC | 0.007s measured | — |
| 677 | G | admin-ui | `admin-ui/src/utils/sidecarTasks.test.ts` | 173 | 13 | under 1500 LOC | 0.007s measured | — |
| 678 | G | backend | `backend/testing/test_migration_0639_align_projectcomputeimagebuild_timestamptz.py` | 105 | 1 | under 1500 LOC | 0.007s measured | — |
| 679 | G | backend | `backend/testing/test_shared_state_facade.py` | 191 | 4 | under 1500 LOC | 0.007s measured | — |
| 680 | G | backend | `backend/testing/test_branch_cleanup.py` | 50 | 3 | under 1500 LOC | 0.006s measured | — |
| 681 | G | agent | `agent/src/handlers/structured-output-repair.test.ts` | 732 | 32 | under 1500 LOC | 0.006s measured | — |
| 682 | G | agent | `agent/src/handlers/walkthrough-criterion-gates.test.ts` | 463 | 40 | under 1500 LOC | 0.006s measured | — |
| 683 | G | agent | `agent/src/handlers/discovery-review.test.ts` | 437 | 14 | under 1500 LOC | 0.006s measured | — |
| 684 | G | agent | `agent/src/handlers/verify-tests.gate-bound.test.ts` | 249 | 5 | under 1500 LOC | 0.006s measured | — |
| 685 | G | admin-ui | `admin-ui/src/taskCapabilities.test.ts` | 136 | 11 | under 1500 LOC | 0.006s measured | — |
| 686 | G | admin-ui | `admin-ui/src/utils/statusFilter.test.ts` | 49 | 7 | under 1500 LOC | 0.006s measured | — |
| 687 | G | backend | `backend/testing/test_compute_provision_slots.py` | 49 | 2 | under 1500 LOC | 0.006s measured | — |
| 688 | G | agent | `agent/src/utils/design-doc-presence.test.ts` | 330 | 25 | under 1500 LOC | 0.005s measured | — |
| 689 | G | agent | `agent/src/handlers/prior-knowledge.test.ts` | 313 | 26 | under 1500 LOC | 0.005s measured | — |
| 690 | G | agent | `agent/src/token-file.test.ts` | 280 | 18 | under 1500 LOC | 0.005s measured | — |
| 691 | G | agent | `agent/src/handlers/compute-experiment.test.ts` | 253 | 12 | under 1500 LOC | 0.005s measured | — |
| 692 | G | agent | `agent/src/prompts/review-prompt.test.ts` | 248 | 27 | under 1500 LOC | 0.005s measured | — |
| 693 | G | agent | `agent/src/oauth-rotation.test.ts` | 205 | 10 | under 1500 LOC | 0.005s measured | — |
| 694 | G | agent | `agent/src/handlers/verify-tests.setup-retry.test.ts` | 175 | 5 | under 1500 LOC | 0.005s measured | — |
| 695 | G | agent | `agent/src/git-credential.test.ts` | 170 | 10 | under 1500 LOC | 0.005s measured | — |
| 696 | G | agent | `agent/src/handlers/verify-tests.provision.test.ts` | 159 | 4 | under 1500 LOC | 0.005s measured | — |
| 697 | G | agent | `agent/src/utils/viewport-shot-download.test.ts` | 140 | 11 | under 1500 LOC | 0.005s measured | — |
| 698 | G | admin-ui | `admin-ui/src/api/projectTools.residentArtifact.test.ts` | 135 | 5 | under 1500 LOC | 0.005s measured | protected; declared floor 5 |
| 699 | G | admin-ui | `admin-ui/src/test/orphan-component-guard.test.ts` | 129 | 2 | under 1500 LOC | 0.005s measured | — |
| 700 | G | admin-ui | `admin-ui/src/utils/unverifiedReasonLabels.test.ts` | 100 | 13 | under 1500 LOC | 0.005s measured | — |
| 701 | G | admin-ui | `admin-ui/src/utils/newProjectOAuthResume.test.ts` | 84 | 5 | under 1500 LOC | 0.005s measured | — |
| 702 | G | admin-ui | `admin-ui/src/utils/verdictFormatting.test.ts` | 63 | 9 | under 1500 LOC | 0.005s measured | — |
| 703 | G | admin-ui | `admin-ui/src/utils/externalUrl.test.ts` | 57 | 12 | under 1500 LOC | 0.005s measured | — |
| 704 | G | backend | `backend/testing/test_admin_auth_invariant.py` | 100 | 1 | under 1500 LOC | 0.005s measured | — |
| 705 | G | agent | `agent/src/build-config.test.ts` | 546 | 46 | under 1500 LOC | 0.004s measured | — |
| 706 | G | agent | `agent/src/prompts/project-context.test.ts` | 409 | 23 | under 1500 LOC | 0.004s measured | — |
| 707 | G | agent | `agent/src/handlers/research-direction-context.test.ts` | 392 | 33 | under 1500 LOC | 0.004s measured | — |
| 708 | G | agent | `agent/src/handlers/review-fix-migration.test.ts` | 272 | 20 | under 1500 LOC | 0.004s measured | — |
| 709 | G | agent | `agent/src/fix-loop-events.test.ts` | 258 | 20 | under 1500 LOC | 0.004s measured | — |
| 710 | G | agent | `agent/src/providers/foreground-resident.test.ts` | 175 | 9 | under 1500 LOC | 0.004s measured | — |
| 711 | G | agent | `agent/src/utils/classifier-fixtures.test.ts` | 110 | 6 | under 1500 LOC | 0.004s measured | — |
| 712 | G | admin-ui | `admin-ui/src/App.test.tsx` | 83 | 4 | under 1500 LOC | 0.004s measured | — |
| 713 | G | admin-ui | `admin-ui/src/types/workbenchEvents.test.ts` | 77 | 4 | under 1500 LOC | 0.004s measured | — |
| 714 | G | admin-ui | `admin-ui/src/components/FleetRunnersPanel.utils.test.ts` | 58 | 5 | under 1500 LOC | 0.004s measured | — |
| 715 | G | admin-ui | `admin-ui/src/utils/idempotencyKey.test.ts` | 56 | 5 | under 1500 LOC | 0.004s measured | — |
| 716 | G | admin-ui | `admin-ui/src/utils/fixLoopLayers.test.ts` | 40 | 6 | under 1500 LOC | 0.004s measured | — |
| 717 | G | admin-ui | `admin-ui/src/test/workbench-theme-tokens.test.ts` | 32 | 1 | under 1500 LOC | 0.004s measured | — |
| 718 | G | admin-ui | `admin-ui/src/utils/time.test.ts` | 30 | 5 | under 1500 LOC | 0.004s measured | — |
| 719 | G | admin-ui | `admin-ui/src/appWorkbenchConstants.test.ts` | 27 | 3 | under 1500 LOC | 0.004s measured | — |
| 720 | G | admin-ui | `admin-ui/src/utils/projectSelection.test.ts` | 27 | 2 | under 1500 LOC | 0.004s measured | — |
| 721 | G | backend | `backend/testing/test_claim_analytics.py` | 40 | 2 | under 1500 LOC | 0.004s measured | — |
| 722 | G | backend | `backend/testing/test_docs_runbook_drift.py` | 263 | 8 | under 1500 LOC | 0.004s measured | — |
| 723 | G | agent | `agent/src/handlers/strategic-context-envelope.test.ts` | 224 | 7 | under 1500 LOC | 0.003s measured | — |
| 724 | G | agent | `agent/src/handlers/review-fix-history.test.ts` | 184 | 21 | under 1500 LOC | 0.003s measured | — |
| 725 | G | agent | `agent/src/json-extract.test.ts` | 176 | 28 | under 1500 LOC | 0.003s measured | — |
| 726 | G | agent | `agent/src/providers/usage.test.ts` | 147 | 10 | under 1500 LOC | 0.003s measured | — |
| 727 | G | agent | `agent/src/branch-literal-regression.test.ts` | 127 | 3 | under 1500 LOC | 0.003s measured | — |
| 728 | G | agent | `agent/src/taskCapabilities.test.ts` | 114 | 10 | under 1500 LOC | 0.003s measured | — |
| 729 | G | admin-ui | `admin-ui/src/utils/computeAnalysisLabels.test.ts` | 96 | 12 | under 1500 LOC | 0.003s measured | — |
| 730 | G | admin-ui | `admin-ui/src/utils/taskGraph.test.ts` | 94 | 11 | under 1500 LOC | 0.003s measured | — |
| 731 | G | admin-ui | `admin-ui/src/accessControl.test.ts` | 91 | 3 | under 1500 LOC | 0.003s measured | — |
| 732 | G | admin-ui | `admin-ui/src/utils/mergeGateSummary.test.ts` | 82 | 7 | under 1500 LOC | 0.003s measured | — |
| 733 | G | admin-ui | `admin-ui/src/utils/imageEdits.test.ts` | 72 | 10 | under 1500 LOC | 0.003s measured | — |
| 734 | G | admin-ui | `admin-ui/src/types/workbenchTargets.test.ts` | 71 | 4 | under 1500 LOC | 0.003s measured | — |
| 735 | G | admin-ui | `admin-ui/src/components/projectToolHidden.test.ts` | 47 | 6 | under 1500 LOC | 0.003s measured | — |
| 736 | G | admin-ui | `admin-ui/src/utils/conversationAttachmentDraftFinalization.test.ts` | 42 | 1 | under 1500 LOC | 0.003s measured | — |
| 737 | G | admin-ui | `admin-ui/src/utils/conversationMessageIdentity.test.ts` | 24 | 2 | under 1500 LOC | 0.003s measured | — |
| 738 | G | backend | `backend/testing/test_local_runner_terminal_log.py` | 140 | 1 | under 1500 LOC | 0.002s measured | — |
| 739 | G | agent | `agent/src/handlers/direction-renderer.test.ts` | 252 | 20 | under 1500 LOC | 0.002s measured | — |
| 740 | G | agent | `agent/src/reset-invariant.test.ts` | 152 | 7 | under 1500 LOC | 0.002s measured | — |
| 741 | G | agent | `agent/src/processTracking.test.ts` | 144 | 4 | under 1500 LOC | 0.002s measured | — |
| 742 | G | agent | `agent/src/node-preflight.test.ts` | 125 | 19 | under 1500 LOC | 0.002s measured | — |
| 743 | G | agent | `agent/src/handlers/verify-tests.setup-probe.test.ts` | 115 | 2 | under 1500 LOC | 0.002s measured | — |
| 744 | G | agent | `agent/src/heartbeat-classify.test.ts` | 104 | 13 | under 1500 LOC | 0.002s measured | — |
| 745 | G | agent | `agent/src/handlers/research-plan-prompt-blocks.test.ts` | 96 | 10 | under 1500 LOC | 0.002s measured | — |
| 746 | G | admin-ui | `admin-ui/src/qualificationStateVocab.contract.test.ts` | 77 | 4 | under 1500 LOC | 0.002s measured | — |
| 747 | G | agent | `agent/src/telemetry.prompt_size.test.ts` | 77 | 3 | under 1500 LOC | 0.002s measured | — |
| 748 | G | admin-ui | `admin-ui/src/test/viewport-capture-config.test.ts` | 57 | 5 | under 1500 LOC | 0.002s measured | — |
| 749 | G | admin-ui | `admin-ui/src/hooks/useUIState.test.ts` | 41 | 3 | under 1500 LOC | 0.002s measured | — |
| 750 | G | admin-ui | `admin-ui/src/utils/strategicAttention.test.ts` | 39 | 3 | under 1500 LOC | 0.002s measured | — |
| 751 | G | admin-ui | `admin-ui/src/utils/convergenceVerdict.test.ts` | 35 | 4 | under 1500 LOC | 0.002s measured | — |
| 752 | G | admin-ui | `admin-ui/src/utils/conversationSendState.test.ts` | 30 | 1 | under 1500 LOC | 0.002s measured | — |
| 753 | G | agent | `agent/src/utils/sanitize.test.ts` | 17 | 1 | under 1500 LOC | 0.002s measured | — |
| 754 | G | admin-ui | `admin-ui/src/useAppState.selection.test.ts` | 15 | 2 | under 1500 LOC | 0.002s measured | — |
| 755 | G | agent | `agent/src/handlers/strategic-resident-wake.test.ts` | 173 | 9 | under 1500 LOC | 0.001s measured | — |
| 756 | G | agent | `agent/src/workspaceDisk.test.ts` | 111 | 6 | under 1500 LOC | 0.001s measured | — |
| 757 | G | admin-ui | `admin-ui/src/branch-literal-regression.test.ts` | 93 | 3 | under 1500 LOC | 0.001s measured | — |
| 758 | G | agent | `agent/src/handlers/structured-repair.test.ts` | 88 | 3 | under 1500 LOC | 0.001s measured | — |
| 759 | G | agent | `agent/src/handlers/research-plan-contract.test.ts` | 77 | 4 | under 1500 LOC | 0.001s measured | — |
| 760 | G | agent | `agent/src/handlers/strategic-stall-advisory.test.ts` | 48 | 3 | under 1500 LOC | 0.001s measured | — |
| 761 | G | agent | `agent/src/handlers/discovery-review-workspace.test.ts` | 44 | 2 | under 1500 LOC | 0.001s measured | — |
| 762 | G | admin-ui | `admin-ui/src/utils/failureClass.test.ts` | 43 | 4 | under 1500 LOC | 0.001s measured | — |
| 763 | G | agent | `agent/src/qualificationStateVocab.contract.test.ts` | 41 | 2 | under 1500 LOC | 0.001s measured | — |
| 764 | G | agent | `agent/src/handlers/strategic-resident-prompt.test.ts` | 39 | 3 | under 1500 LOC | 0.001s measured | protected; declared floor 3 |
| 765 | G | agent | `agent/src/_agent_scripts/materialize_manifest.parity.test.ts` | 34 | 1 | under 1500 LOC | 0.001s measured | — |
| 766 | G | agent | `agent/src/_agent_scripts/fixgate_attribution.parity.test.ts` | 31 | 1 | under 1500 LOC | 0.001s measured | — |
| 767 | G | admin-ui | `admin-ui/src/utils/composerRequestOwnership.test.ts` | 28 | 2 | under 1500 LOC | 0.001s measured | — |
| 768 | G | admin-ui | `admin-ui/src/test/environment.test.ts` | 15 | 1 | under 1500 LOC | 0.001s measured | — |
| 769 | G | admin-ui | `admin-ui/src/components/AdversarialQualityPanel.test.tsx` | 9 | 1 | under 1500 LOC | 0.001s measured | — |
| 770 | G | backend | `backend/testing/test_service_credentials.py` | 1,489 | 30 | under 1500 LOC | unknown (no committed measurement) | — |
| 771 | G | backend | `backend/testing/test_admin_routers__runners.py` | 1,317 | 33 | under 1500 LOC | unknown (no committed measurement) | — |
| 772 | G | backend | `backend/testing/test_check_gate_fail_closed.py` | 1,307 | 33 | under 1500 LOC | unknown (no committed measurement) | — |
| 773 | G | backend | `backend/testing/test_approval_exact_target_publication.py` | 1,275 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 774 | G | backend | `backend/testing/test_admin_approval_merge_submission_recovery.py` | 1,272 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| 775 | G | backend | `backend/testing/test_conversation_coordinator_two_speed.py` | 1,241 | 14 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 14 |
| 776 | G | backend | `backend/testing/test_admin_routers__problem_model.py` | 1,230 | 39 | under 1500 LOC | unknown (no committed measurement) | — |
| 777 | G | backend | `backend/testing/test_resource_envelope_enforcement.py` | 1,189 | 27 | under 1500 LOC | unknown (no committed measurement) | — |
| 778 | G | backend | `backend/testing/test_mode_knob_graduation.py` | 1,188 | 22 | under 1500 LOC | unknown (no committed measurement) | — |
| 779 | G | backend | `backend/testing/test_strategic_cycle_cost_projection.py` | 1,143 | 46 | under 1500 LOC | unknown (no committed measurement) | — |
| 780 | G | backend | `backend/testing/test_release_qualifier_audit.py` | 1,136 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 781 | G | backend | `backend/testing/test_conversational_workday_protection.py` | 1,132 | 35 | under 1500 LOC | unknown (no committed measurement) | — |
| 782 | G | backend | `backend/testing/test_strategic_memory_envelope.py` | 1,113 | 21 | under 1500 LOC | unknown (no committed measurement) | — |
| 783 | G | backend | `backend/testing/test_migration_0681_0694_preservation.py` | 1,093 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 784 | G | backend | `backend/testing/test_strategic_spawn_requests.py` | 1,089 | 20 | under 1500 LOC | unknown (no committed measurement) | — |
| 785 | G | backend | `backend/testing/test_acceptance_gate__test_corroboration.py` | 1,058 | 26 | under 1500 LOC | unknown (no committed measurement) | — |
| 786 | G | backend | `backend/testing/test_strategic_lane_linearization.py` | 1,050 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 787 | G | backend | `backend/testing/test_workday_resident_invariants.py` | 1,040 | 25 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 25 |
| 788 | G | backend | `backend/testing/test_release_stall_alerts.py` | 1,027 | 28 | under 1500 LOC | unknown (no committed measurement) | — |
| 789 | G | backend | `backend/testing/test_admin_routers__autonomous.py` | 1,017 | 25 | under 1500 LOC | unknown (no committed measurement) | — |
| 790 | G | backend | `backend/testing/test_workday_artifact_materialization.py` | 1,011 | 17 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 17 |
| 791 | G | backend | `backend/testing/test_release_prepromotion_rehearsal.py` | 987 | 26 | under 1500 LOC | unknown (no committed measurement) | — |
| 792 | G | backend | `backend/testing/test_llm_cost_attribution.py` | 981 | 19 | under 1500 LOC | unknown (no committed measurement) | — |
| 793 | G | backend | `backend/testing/test_release_qualifier_walkthrough_evidence.py` | 968 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 794 | G | backend | `backend/testing/test_project_operator_access.py` | 962 | 14 | under 1500 LOC | unknown (no committed measurement) | — |
| 795 | G | backend | `backend/testing/test_cost_reconciliation.py` | 952 | 24 | under 1500 LOC | unknown (no committed measurement) | — |
| 796 | G | backend | `backend/testing/test_compute_image_environment.py` | 937 | 23 | under 1500 LOC | unknown (no committed measurement) | — |
| 797 | G | backend | `backend/testing/test_strategic_conversation_query.py` | 923 | 15 | under 1500 LOC | unknown (no committed measurement) | — |
| 798 | G | backend | `backend/testing/test_discovery_review_packet_budget.py` | 913 | 13 | under 1500 LOC | unknown (no committed measurement) | — |
| 799 | G | backend | `backend/testing/test_review_wait_terminal_boundaries.py` | 910 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 800 | G | backend | `backend/testing/test_module_finding_chain.py` | 898 | 17 | under 1500 LOC | unknown (no committed measurement) | — |
| 801 | G | backend | `backend/testing/test_admin_agent_ops.py` | 895 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 802 | G | backend | `backend/testing/test_blind_merge_grade.py` | 882 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 803 | G | backend | `backend/testing/test_admin_routers__users.py` | 876 | 34 | under 1500 LOC | unknown (no committed measurement) | — |
| 804 | G | backend | `backend/testing/test_admin_warm_pool.py` | 865 | 28 | under 1500 LOC | unknown (no committed measurement) | — |
| 805 | G | backend | `backend/testing/test_rollup_size_preflight.py` | 861 | 27 | under 1500 LOC | unknown (no committed measurement) | — |
| 806 | G | backend | `backend/testing/test_admin_pipeline_observability.py` | 857 | 20 | under 1500 LOC | unknown (no committed measurement) | — |
| 807 | G | backend | `backend/testing/test_issue_ledger.py` | 847 | 32 | under 1500 LOC | unknown (no committed measurement) | — |
| 808 | G | backend | `backend/testing/test_schema_invariants.py` | 831 | 14 | under 1500 LOC | unknown (no committed measurement) | — |
| 809 | G | backend | `backend/testing/test_cost_attribution.py` | 812 | 21 | under 1500 LOC | unknown (no committed measurement) | — |
| 810 | G | backend | `backend/testing/test_strategic_effort_tier_evidence.py` | 780 | 26 | under 1500 LOC | unknown (no committed measurement) | — |
| 811 | G | backend | `backend/testing/test_agent_tasks.py` | 770 | 27 | under 1500 LOC | unknown (no committed measurement) | — |
| 812 | G | backend | `backend/testing/test_admin_project_roles.py` | 741 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 813 | G | backend | `backend/testing/test_pipeline_alert_scan.py` | 736 | 21 | under 1500 LOC | unknown (no committed measurement) | — |
| 814 | G | backend | `backend/testing/test_agent__claim_core_auth.py` | 725 | 15 | under 1500 LOC | unknown (no committed measurement) | — |
| 815 | G | backend | `backend/testing/test_attempt24_release_safety.py` | 717 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 816 | G | backend | `backend/testing/test_rollup_lineage_fence.py` | 715 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 817 | G | backend | `backend/testing/test_sr_compute_wiring.py` | 710 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 818 | G | backend | `backend/testing/test_review_merge_authority.py` | 709 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 819 | G | backend | `backend/testing/test_test_durations_freshness.py` | 701 | 30 | under 1500 LOC | unknown (no committed measurement) | — |
| 820 | G | backend | `backend/testing/test_acceptance_criteria__walkthrough.py` | 700 | 13 | under 1500 LOC | unknown (no committed measurement) | — |
| 821 | G | backend | `backend/testing/test_compute_quota.py` | 679 | 13 | under 1500 LOC | unknown (no committed measurement) | — |
| 822 | G | backend | `backend/testing/test_review_claim_fence.py` | 677 | 11 | under 1500 LOC | unknown (no committed measurement) | — |
| 823 | G | backend | `backend/testing/test_sequenced_dependent_spawn.py` | 672 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 824 | G | backend | `backend/testing/test_acceptance_gate__rule_version.py` | 670 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 825 | G | backend | `backend/testing/test_vitest_durations_freshness.py` | 660 | 27 | under 1500 LOC | unknown (no committed measurement) | — |
| 826 | G | backend | `backend/testing/test_strategic_consultation_detail.py` | 654 | 15 | under 1500 LOC | unknown (no committed measurement) | — |
| 827 | G | backend | `backend/testing/test_admin_request_changes_authority.py` | 642 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 828 | G | backend | `backend/testing/test_lifecycle_integration.py` | 634 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 829 | G | backend | `backend/testing/test_publication_gate_receipt.py` | 632 | 17 | under 1500 LOC | unknown (no committed measurement) | — |
| 830 | G | backend | `backend/testing/test_release_qualification_pause_deferral.py` | 628 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 831 | G | backend | `backend/testing/test_strategic_consultation_status.py` | 620 | 3 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 3 |
| 832 | G | backend | `backend/testing/test_release_qualifier__trigger_and_dispatch_regressions.py` | 614 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 833 | G | backend | `backend/testing/test_test_imports_checker.py` | 614 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| 834 | G | backend | `backend/testing/test_reaper_post_orphan_sweep.py` | 608 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 835 | G | backend | `backend/testing/test_blocked_retry_authority.py` | 605 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 836 | G | backend | `backend/testing/test_migration_0682_workday_repair.py` | 600 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 837 | G | backend | `backend/testing/test_github_onboarding_access.py` | 596 | 15 | under 1500 LOC | unknown (no committed measurement) | — |
| 838 | G | backend | `backend/testing/test_acceptance_criteria__trials.py` | 586 | 13 | under 1500 LOC | unknown (no committed measurement) | — |
| 839 | G | backend | `backend/testing/test_direction_saturation.py` | 579 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 840 | G | backend | `backend/testing/test_strategic_cycle_gating.py` | 578 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 841 | G | backend | `backend/testing/test_strategic_fast_live_work.py` | 567 | 5 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 5 |
| 842 | G | backend | `backend/testing/test_review_loop_operator_park.py` | 563 | 25 | under 1500 LOC | unknown (no committed measurement) | — |
| 843 | G | backend | `backend/testing/test_admin_playbook.py` | 559 | 17 | under 1500 LOC | unknown (no committed measurement) | — |
| 844 | G | backend | `backend/testing/test_direction_lineage_spend.py` | 556 | 13 | under 1500 LOC | unknown (no committed measurement) | — |
| 845 | G | backend | `backend/testing/test_strategic_payload_size.py` | 547 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 846 | G | backend | `backend/testing/test_release_qualifier__retry_episode_cap.py` | 533 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 847 | G | backend | `backend/testing/test_strategic_memory_corrections.py` | 532 | 14 | under 1500 LOC | unknown (no committed measurement) | — |
| 848 | G | backend | `backend/testing/test_anomaly_proposals.py` | 527 | 13 | under 1500 LOC | unknown (no committed measurement) | — |
| 849 | G | backend | `backend/testing/test_work_session_command_execution.py` | 520 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 850 | G | backend | `backend/testing/test_research_spawn_pending_recovery.py` | 518 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 851 | G | backend | `backend/testing/test_runner_enroll_docs_accuracy.py` | 517 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 852 | G | backend | `backend/testing/test_admin_approval_comment_recovery.py` | 514 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 853 | G | backend | `backend/testing/test_stranded_conversation_message_sweep.py` | 508 | 15 | under 1500 LOC | unknown (no committed measurement) | — |
| 854 | G | backend | `backend/testing/test_shard_balance.py` | 501 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 855 | G | backend | `backend/testing/test_release_qualifier_walkthrough_request.py` | 499 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 856 | G | backend | `backend/testing/test_strategic_resident_continuous_liveness.py` | 495 | 8 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 8 |
| 857 | G | backend | `backend/testing/test_project_cascade.py` | 494 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 858 | G | backend | `backend/testing/test_admin_warm_vm.py` | 483 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 859 | G | backend | `backend/testing/test_review_pending_requeue.py` | 480 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 860 | G | backend | `backend/testing/test_project_message_delivery.py` | 475 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 861 | G | backend | `backend/testing/test_strategic_watches.py` | 475 | 11 | under 1500 LOC | unknown (no committed measurement) | — |
| 862 | G | backend | `backend/testing/test_runner_rate_limiter.py` | 456 | 14 | under 1500 LOC | unknown (no committed measurement) | — |
| 863 | G | backend | `backend/testing/test_ci_review_wait_dedup_windows.py` | 434 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 864 | G | backend | `backend/testing/test_migration_0697_retained_telemetry_history.py` | 431 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 865 | G | backend | `backend/testing/test_release_qualifier_walkthrough_site.py` | 431 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 866 | G | backend | `backend/testing/test_migration_0679_backlog_clear.py` | 423 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 867 | G | backend | `backend/testing/test_attempt24_operator_flows.py` | 422 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 868 | G | backend | `backend/testing/test_release_qualifier_infra_continuation.py` | 422 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 869 | G | backend | `backend/testing/test_runner_decline_epoch.py` | 419 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 870 | G | backend | `backend/testing/test_strategic_autonomous_handoff.py` | 416 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 871 | G | backend | `backend/testing/test_migration_0714_strategic_conversation_correctness.py` | 414 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 872 | G | backend | `backend/testing/test_strategic_memory_claim_redaction_repair.py` | 414 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 873 | G | backend | `backend/testing/test_strategic_memory_service.py` | 403 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 874 | G | backend | `backend/testing/test_warm_pool_readiness.py` | 402 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 875 | G | backend | `backend/testing/test_provider_cost_summary.py` | 401 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 876 | G | backend | `backend/testing/test_strategic_payload.py` | 389 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 877 | G | backend | `backend/testing/test_admin_approval_dedup_authority.py` | 382 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 878 | G | backend | `backend/testing/test_discovery_models.py` | 382 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 879 | G | backend | `backend/testing/test_approval_gate_config.py` | 377 | 13 | under 1500 LOC | unknown (no committed measurement) | — |
| 880 | G | backend | `backend/testing/test_staging_base_refresh.py` | 377 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| 881 | G | backend | `backend/testing/test_research_lock_order.py` | 368 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 882 | G | backend | `backend/testing/test_project_tool_context.py` | 361 | 14 | under 1500 LOC | unknown (no committed measurement) | — |
| 883 | G | backend | `backend/testing/test_discovery_admission.py` | 356 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 884 | G | backend | `backend/testing/test_strategic_memory_runtime.py` | 355 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 885 | G | backend | `backend/testing/test_admin_serial_console.py` | 353 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 886 | G | backend | `backend/testing/test_process_role_lifecycle.py` | 347 | 25 | under 1500 LOC | unknown (no committed measurement) | — |
| 887 | G | backend | `backend/testing/test_discovery_authority_reconciliation.py` | 342 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 888 | G | backend | `backend/testing/test_compute_gpu.py` | 341 | 20 | under 1500 LOC | unknown (no committed measurement) | — |
| 889 | G | backend | `backend/testing/test_strategic_memory_retrieval.py` | 339 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 890 | G | backend | `backend/testing/test_project_resource_summary.py` | 338 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 891 | G | backend | `backend/testing/test_migration_0640_0682_workday_chain.py` | 334 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 892 | G | backend | `backend/testing/test_strategic_resident_pipeline_projection.py` | 324 | 3 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 3 |
| 893 | G | backend | `backend/testing/test_agent_github.py` | 323 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 894 | G | backend | `backend/testing/test_strategic_memory_control.py` | 319 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 895 | G | backend | `backend/testing/test_admin_discovery_cycles.py` | 306 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 896 | G | backend | `backend/testing/test_completion_automatic_task_authority.py` | 306 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 897 | G | backend | `backend/testing/test_concurrency_overlap.py` | 303 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 898 | G | backend | `backend/testing/test_review_admission_ownership.py` | 298 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 899 | G | backend | `backend/testing/test_stall_advisory.py` | 298 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 900 | G | backend | `backend/testing/test_release_qualifier_contracts.py` | 297 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 901 | G | backend | `backend/testing/test_agent_access_projections.py` | 294 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 902 | G | backend | `backend/testing/test_migration_0724_name_reuse_after_revocation.py` | 287 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 903 | G | backend | `backend/testing/test_task_constructor_authority_manifest.py` | 285 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 904 | G | backend | `backend/testing/test_admin_orchestrator_errors.py` | 282 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 905 | G | backend | `backend/testing/test_admin_approval_gate_release_authority.py` | 281 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 906 | G | backend | `backend/testing/test_automatic_task_creation_authority.py` | 280 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 907 | G | backend | `backend/testing/test_strategic_memory_source_schema.py` | 280 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 908 | G | backend | `backend/testing/test_project_outcome_projection.py` | 277 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 909 | G | backend | `backend/testing/test_warm_claim_account_attribution.py` | 274 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 910 | G | backend | `backend/testing/test_discovery_review_sequencing.py` | 271 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 911 | G | backend | `backend/testing/test_migration_0669_backfill_direction_fa_telemetry.py` | 271 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 912 | G | backend | `backend/testing/test_test_estate_ranked_queue.py` | 270 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 913 | G | backend | `backend/testing/test_migration_0696_project_sr_analytics.py` | 269 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 914 | G | backend | `backend/testing/test_rollup_premerge_tip.py` | 260 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 915 | G | backend | `backend/testing/test_scripts_test_lane.py` | 257 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 916 | G | backend | `backend/testing/test_migration_0720_direction_index_repair.py` | 255 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 917 | G | backend | `backend/testing/test_compute_secret_manifest.py` | 252 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 918 | G | backend | `backend/testing/test_strategic_memory_claim_redaction_repair_retry.py` | 252 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 919 | G | backend | `backend/testing/test_strategic_resident_work_state.py` | 248 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 920 | G | backend | `backend/testing/test_admin_bulk_cancellation_fencing.py` | 246 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 921 | G | backend | `backend/testing/test_strategic_memory_governance.py` | 245 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 922 | G | backend | `backend/testing/test_strategic_memory_knowledge.py` | 245 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 923 | G | backend | `backend/testing/test_strategic_workday_handoff_recovery.py` | 242 | 3 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 3 |
| 924 | G | backend | `backend/testing/test_automatic_task_creation_producers.py` | 233 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 925 | G | backend | `backend/testing/test_migration_0657_0671_resident_session_id.py` | 228 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 926 | G | backend | `backend/testing/test_discovery_runtime_integration.py` | 227 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 927 | G | backend | `backend/testing/test_release_qualifier_candidate_sha_stamp.py` | 225 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 928 | G | backend | `backend/testing/test_terminal_rollup_pr_sweep.py` | 219 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 929 | G | backend | `backend/testing/test_migration_0668_drop_projectcomputeimagebuild.py` | 218 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 930 | G | backend | `backend/testing/test_migration_0698_cochange_state.py` | 217 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 931 | G | backend | `backend/testing/test_migration_0717_strategic_memory_semantic_ledger.py` | 217 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 932 | G | backend | `backend/testing/test_review_wait_monitor_owned_gate.py` | 206 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 933 | G | backend | `backend/testing/test_compute_artifact_iam.py` | 204 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 934 | G | backend | `backend/testing/test_rollup_handoff_ci_gate.py` | 204 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 935 | G | backend | `backend/testing/test_anomaly_persistence.py` | 203 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 936 | G | backend | `backend/testing/test_vm_lifecycle_forensics.py` | 196 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 937 | G | backend | `backend/testing/test_release_ownership.py` | 195 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 938 | G | backend | `backend/testing/test_migration_0656_projectmessage_context_audience_index.py` | 194 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 939 | G | backend | `backend/testing/test_projects.py` | 186 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 940 | G | backend | `backend/testing/test_user_management.py` | 186 | 17 | under 1500 LOC | unknown (no committed measurement) | — |
| 941 | G | backend | `backend/testing/test_strategic_memory_invariants.py` | 182 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 942 | G | backend | `backend/testing/test_release_operator_commit_gate.py` | 181 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 943 | G | backend | `backend/testing/test_admin_task_requeue_terminal.py` | 180 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 944 | G | backend | `backend/testing/test_strategic_op_contracts.py` | 179 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 945 | G | backend | `backend/testing/test_voice_credentials.py` | 178 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 946 | G | backend | `backend/testing/test_strategic_memory_sources.py` | 174 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 947 | G | backend | `backend/testing/test_research_mem_eval.py` | 172 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 948 | G | backend | `backend/testing/test_task_lifecycle_modularity.py` | 162 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 949 | G | backend | `backend/testing/test_frontend_static.py` | 155 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 950 | G | backend | `backend/testing/test_recovery_sidecar.py` | 155 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 951 | G | backend | `backend/testing/test_release_artifact_freshness.py` | 150 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 952 | G | backend | `backend/testing/test_strategic_correctness_preflight.py` | 150 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 953 | G | backend | `backend/testing/test_sr_analytics_store.py` | 148 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 954 | G | backend | `backend/testing/test_strategic_workday_mutation_rebind.py` | 148 | 2 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 2 |
| 955 | G | backend | `backend/testing/test_migration_0722_discovery_controller.py` | 147 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 956 | G | backend | `backend/testing/test_livekit_tokens.py` | 143 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 957 | G | backend | `backend/testing/test_release_activation_after_timeout.py` | 143 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 958 | G | backend | `backend/testing/test_compute_execution_documents.py` | 130 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 959 | G | backend | `backend/testing/test_execution_capability_requirements.py` | 126 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 960 | G | backend | `backend/testing/test_release_qualifier_retained_scoring.py` | 125 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 961 | G | backend | `backend/testing/test_migration_0713_hot_sweep_indexes.py` | 124 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 962 | G | backend | `backend/testing/test_release_promotion_compute_image_refresh.py` | 124 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 963 | G | backend | `backend/testing/test_release_qualifier.py` | 124 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 964 | G | backend | `backend/testing/test_verification_toolchain.py` | 121 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 965 | G | backend | `backend/testing/test_post_commit_recovery_claim.py` | 120 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 966 | G | backend | `backend/testing/test_local_app_release.py` | 118 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 967 | G | backend | `backend/testing/test_admin_promotion_enablement.py` | 114 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 968 | G | backend | `backend/testing/test_migration_0716_strategic_memory_source_control_plane.py` | 112 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 969 | G | backend | `backend/testing/test_release_serving_reason_attribution.py` | 108 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 970 | G | backend | `backend/testing/test_websocket_server_integration.py` | 104 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 971 | G | backend | `backend/testing/test_task_counts_endpoint.py` | 100 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 972 | G | backend | `backend/testing/test_admin_strategic_memory.py` | 99 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 973 | G | backend | `backend/testing/test_release_prepromotion_rehearsal_contracts.py` | 99 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 974 | G | backend | `backend/testing/test_rollup_open_pr_reconciliation.py` | 94 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 975 | G | backend | `backend/testing/test_runbook_drift.py` | 91 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 976 | G | backend | `backend/testing/test_vm_lifecycle_runner_doomed_marker.py` | 89 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 977 | G | backend | `backend/testing/test_agent_task_capabilities.py` | 88 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 978 | G | backend | `backend/testing/test_qualifier_smoketest_retired.py` | 86 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 979 | G | backend | `backend/testing/test_admin_task_mutations_import.py` | 85 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 980 | G | backend | `backend/testing/test_strategic_resident_activity.py` | 85 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 981 | G | backend | `backend/testing/test_migration_0718_strategic_memory_timezone_columns.py` | 84 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 982 | G | backend | `backend/testing/test_release_activation.py` | 84 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 983 | G | backend | `backend/testing/test_direction_terminal_guard.py` | 80 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 984 | G | backend | `backend/testing/test_blocked_recheck_autonomy.py` | 79 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 985 | G | backend | `backend/testing/test_strategic_stall_visibility.py` | 71 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 986 | G | backend | `backend/testing/test_research_deferred_carry.py` | 62 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 987 | G | backend | `backend/testing/test_migration_0719_validate_task_context_size_constraint.py` | 57 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 988 | G | backend | `backend/testing/test_migration_0715_strategic_context_envelopes.py` | 53 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 989 | G | backend | `backend/testing/test_github_oauth_origins.py` | 52 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 990 | G | backend | `backend/testing/test_task_list_pagination.py` | 51 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 991 | G | backend | `backend/testing/test_agent_task_contract.py` | 50 | 0 | under 1500 LOC | unknown (no committed measurement) | — |
| 992 | G | backend | `backend/testing/test_agent_memory_consolidation.py` | 43 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 993 | G | backend | `backend/testing/test_compute_outcome_status.py` | 34 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 994 | G | backend | `backend/testing/test_orchestrator_health_authority.py` | 32 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 995 | G | backend | `backend/testing/test_failure_classification_retained.py` | 11 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
