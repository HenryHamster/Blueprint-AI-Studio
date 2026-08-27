# Test Estate Ranked Queue

> GENERATED FILE. Do not hand-edit mutable estate facts.
> Regenerate with `python3 scripts/generate_test_estate_ranked_queue.py --write`;
> validate with `python3 scripts/generate_test_estate_ranked_queue.py --check`.
> On Windows the interpreter is `python`: the python.org installer ships
> no `python3`, so the two commands above cannot run there as written.
> Policy and threshold definitions: `test-estate-rubric.md`.

## Estate summary

| Area | Files | Test LOC | Canonical tests | Measured cost | Unknown-cost files |
|------|------:|---------:|----------------:|--------------:|-------------------:|
| backend | 647 | 1,014,577 | 26,941 | 11,134.932s (645 files) | 2 |
| admin-ui | 254 | 101,607 | 4,537 | 209.625s (254 files) | 0 |
| admin-ui-viewport | 165 | 31,747 | 822 | 0.000s (0 files) | 165 |
| agent | 129 | 113,094 | 6,178 | 317.583s (129 files) | 0 |

The queue includes every currently collected backend, admin-ui unit, admin-ui viewport, and agent test file. Backend duration coverage is partial: an `unknown` cost is intentionally not treated as zero or ranked as cheap. Admin-ui and agent costs are complete directional signals from their committed artifacts. Protected Workday counts are copied verbatim from the manifest for reporting only; this generator never edits that manifest.

## Ranked queue

| Rank | Priority | Area | File | LOC | Tests | Threshold class | Cost signal | Workday protection |
|-----:|:--------:|------|------|----:|------:|------------------|-------------|---------------------|
| 1 | P2 | backend | `backend/testing/test_strategic_completion.py` | 21,914 | 629 | critical (>5000 LOC) | 603.712s measured | — |
| 2 | P2 | backend | `backend/testing/test_release_qualifier__compute_composite.py` | 5,968 | 264 | critical (>5000 LOC) | 400.868s measured | — |
| 3 | P2 | backend | `backend/testing/test_pipeline.py` | 6,841 | 277 | critical (>5000 LOC) | 318.896s measured | — |
| 4 | P2 | backend | `backend/testing/test_review_pipeline.py` | 20,894 | 437 | critical (>5000 LOC) | 300.644s measured | — |
| 5 | P2 | backend | `backend/testing/test_task_lifecycle.py` | 13,145 | 323 | critical (>5000 LOC) | 207.992s measured | — |
| 6 | P2 | backend | `backend/testing/test_reaper.py` | 21,674 | 499 | critical (>5000 LOC) | 154.118s measured | protected; declared floor 499 |
| 7 | P2 | backend | `backend/testing/test_research_completion.py` | 24,174 | 534 | critical (>5000 LOC) | 137.469s measured | — |
| 8 | P2 | backend | `backend/testing/test_autonomous_research.py` | 20,308 | 393 | critical (>5000 LOC) | 124.505s measured | — |
| 9 | P2 | backend | `backend/testing/test_invariants.py` | 15,829 | 279 | critical (>5000 LOC) | 120.389s measured | — |
| 10 | P2 | backend | `backend/testing/test_local_runner.py` | 14,056 | 348 | critical (>5000 LOC) | 111.153s measured | — |
| 11 | P2 | backend | `backend/testing/test_digest.py` | 11,003 | 231 | critical (>5000 LOC) | 86.555s measured | — |
| 12 | P2 | backend | `backend/testing/test_directions.py` | 11,809 | 339 | critical (>5000 LOC) | 79.334s measured | — |
| 13 | P2 | backend | `backend/testing/test_strategic_resident_session.py` | 5,475 | 85 | critical (>5000 LOC) | 77.770s measured | protected; declared floor 85 |
| 14 | P2 | backend | `backend/testing/test_acceptance_criteria.py` | 13,369 | 283 | critical (>5000 LOC) | 74.580s measured | — |
| 15 | P2 | backend | `backend/testing/test_release_qualifier__ci_unresolved_blocker.py` | 6,523 | 163 | critical (>5000 LOC) | 67.628s measured | — |
| 16 | P2 | backend | `backend/testing/test_agent.py` | 29,484 | 515 | critical (>5000 LOC) | 65.904s measured | — |
| 17 | P2 | backend | `backend/testing/test_metrics_integrity_sweep.py` | 8,161 | 216 | critical (>5000 LOC) | 65.620s measured | — |
| 18 | P2 | backend | `backend/testing/test_admin_routers__version_lifecycle.py` | 5,122 | 104 | critical (>5000 LOC) | 58.078s measured | — |
| 19 | P2 | backend | `backend/testing/test_compute.py` | 9,365 | 192 | critical (>5000 LOC) | 57.415s measured | — |
| 20 | P2 | backend | `backend/testing/test_orchestrator.py` | 18,818 | 362 | critical (>5000 LOC) | 57.238s measured | — |
| 21 | P2 | backend | `backend/testing/test_compute_experiment_completion.py` | 7,235 | 134 | critical (>5000 LOC) | 55.582s measured | — |
| 22 | P2 | backend | `backend/testing/test_knowledge_documents.py` | 6,801 | 164 | critical (>5000 LOC) | 48.488s measured | — |
| 23 | P2 | backend | `backend/testing/test_release_reconciliation.py` | 8,826 | 150 | critical (>5000 LOC) | 43.934s measured | — |
| 24 | P2 | backend | `backend/testing/test_compute_image_build_local.py` | 8,190 | 228 | critical (>5000 LOC) | 42.722s measured | — |
| 25 | P2 | backend | `backend/testing/test_efficiency_snapshot.py` | 12,745 | 314 | critical (>5000 LOC) | 39.993s measured | — |
| 26 | P2 | backend | `backend/testing/test_playbook_sr_metrics.py` | 8,196 | 148 | critical (>5000 LOC) | 37.850s measured | — |
| 27 | P2 | backend | `backend/testing/test_ci_completion.py` | 9,226 | 151 | critical (>5000 LOC) | 37.248s measured | — |
| 28 | P2 | backend | `backend/testing/test_admin_routers.py` | 18,236 | 455 | critical (>5000 LOC) | 34.420s measured | protected; declared floor 455 |
| 29 | P2 | backend | `backend/testing/test_evaluating.py` | 6,414 | 97 | critical (>5000 LOC) | 33.035s measured | — |
| 30 | P2 | backend | `backend/testing/test_release_qualifier__classify_check_runs.py` | 7,531 | 134 | critical (>5000 LOC) | 30.084s measured | — |
| 31 | P2 | backend | `backend/testing/test_rollup_management.py` | 6,226 | 106 | critical (>5000 LOC) | 26.818s measured | — |
| 32 | P2 | backend | `backend/testing/test_direction_outcomes.py` | 10,397 | 191 | critical (>5000 LOC) | 26.802s measured | — |
| 33 | P2 | backend | `backend/testing/test_github.py` | 5,931 | 187 | critical (>5000 LOC) | 17.135s measured | — |
| 34 | P2 | backend | `backend/testing/test_direction_lifecycle.py` | 5,517 | 89 | critical (>5000 LOC) | 13.910s measured | — |
| 35 | P2 | agent | `agent/src/handlers/strategic-resident.test.ts` | 9,165 | 135 | critical (>5000 LOC) | 13.729s measured | protected; declared floor 135 |
| 36 | P2 | backend | `backend/testing/test_salvage_bundle_standalone.py` | 7,775 | 239 | critical (>5000 LOC) | 8.485s measured | — |
| 37 | P2 | backend | `backend/testing/test_digest_metrics.py` | 8,216 | 251 | critical (>5000 LOC) | 8.434s measured | — |
| 38 | P2 | backend | `backend/testing/test_agent_strategic_fetch.py` | 7,132 | 140 | critical (>5000 LOC) | 7.724s measured | protected; declared floor 140 |
| 39 | P2 | backend | `backend/testing/test_release_qualifier__finalize_qualification_2.py` | 7,021 | 112 | critical (>5000 LOC) | 5.220s measured | — |
| 40 | P2 | backend | `backend/testing/test_strategic_state.py` | 10,654 | 174 | critical (>5000 LOC) | 4.542s measured | protected; declared floor 174 |
| 41 | P2 | backend | `backend/testing/test_release_qualifier__sweep_stuck_qualifications.py` | 7,146 | 145 | critical (>5000 LOC) | 3.776s measured | — |
| 42 | P2 | backend | `backend/testing/test_release_qualifier__finalize_qualification.py` | 7,663 | 106 | critical (>5000 LOC) | 3.708s measured | — |
| 43 | P2 | backend | `backend/testing/test_warm_pool_maintainer.py` | 5,921 | 123 | critical (>5000 LOC) | 3.564s measured | — |
| 44 | P2 | admin-ui | `admin-ui/src/components/VersionLifecyclePanel.test.tsx` | 5,098 | 177 | critical (>5000 LOC) | 2.891s measured | — |
| 45 | P2 | agent | `agent/src/handlers/research-plan.test.ts` | 5,390 | 396 | critical (>5000 LOC) | 2.122s measured | — |
| 46 | P2 | backend | `backend/testing/test_acceptance_gate.py` | 5,489 | 127 | critical (>5000 LOC) | 1.800s measured | — |
| 47 | P2 | backend | `backend/testing/test_max_plan_pool.py` | 6,364 | 359 | critical (>5000 LOC) | 1.344s measured | — |
| 48 | P2 | backend | `backend/testing/test_capability_trial_verdict.py` | 5,371 | 286 | critical (>5000 LOC) | 0.804s measured | — |
| 49 | P2 | backend | `backend/testing/test_problem_model.py` | 6,802 | 304 | critical (>5000 LOC) | 0.769s measured | — |
| 50 | P2 | backend | `backend/testing/test_capability_proof.py` | 6,622 | 247 | critical (>5000 LOC) | 0.548s measured | — |
| 51 | P3 | backend | `backend/testing/test_strategic_analysis.py` | 4,050 | 163 | high (3000–5000 LOC) | 329.000s measured | — |
| 52 | P3 | backend | `backend/testing/test_release_version.py` | 4,041 | 156 | high (3000–5000 LOC) | 148.400s measured | — |
| 53 | P3 | backend | `backend/testing/test_task_completion.py` | 4,674 | 181 | high (3000–5000 LOC) | 116.632s measured | — |
| 54 | P3 | backend | `backend/testing/test_release_qualifier__site_b_lock_window_fix.py` | 3,656 | 108 | high (3000–5000 LOC) | 104.664s measured | — |
| 55 | P3 | backend | `backend/testing/test_pipeline_health.py` | 4,048 | 120 | high (3000–5000 LOC) | 89.848s measured | — |
| 56 | P3 | backend | `backend/testing/test_rollup_cleanup.py` | 4,300 | 75 | high (3000–5000 LOC) | 82.971s measured | — |
| 57 | P3 | backend | `backend/testing/test_admin_analytics.py` | 4,247 | 98 | high (3000–5000 LOC) | 49.510s measured | — |
| 58 | P3 | backend | `backend/testing/test_plan_completion_atomic.py` | 3,311 | 55 | high (3000–5000 LOC) | 48.729s measured | — |
| 59 | P3 | backend | `backend/testing/test_pipeline_integration.py` | 3,851 | 35 | high (3000–5000 LOC) | 47.777s measured | — |
| 60 | P3 | backend | `backend/testing/test_compute_runner_dispatch.py` | 3,269 | 68 | high (3000–5000 LOC) | 42.232s measured | — |
| 61 | P3 | backend | `backend/testing/test_background.py` | 4,222 | 87 | high (3000–5000 LOC) | 38.844s measured | — |
| 62 | P3 | backend | `backend/testing/test_services.py` | 4,343 | 105 | high (3000–5000 LOC) | 32.037s measured | — |
| 63 | P3 | backend | `backend/testing/test_admin_routers__projects.py` | 3,609 | 132 | high (3000–5000 LOC) | 31.726s measured | — |
| 64 | P3 | backend | `backend/testing/test_admin_compute_jobs.py` | 3,312 | 79 | high (3000–5000 LOC) | 29.428s measured | — |
| 65 | P3 | backend | `backend/testing/test_project_tools.py` | 3,447 | 108 | high (3000–5000 LOC) | 27.728s measured | — |
| 66 | P3 | backend | `backend/testing/test_digest_sr.py` | 3,895 | 74 | high (3000–5000 LOC) | 27.494s measured | — |
| 67 | P3 | backend | `backend/testing/test_improvement_lane.py` | 3,811 | 100 | high (3000–5000 LOC) | 26.526s measured | — |
| 68 | P3 | backend | `backend/testing/test_compute_experiment_spawn.py` | 4,555 | 151 | high (3000–5000 LOC) | 26.515s measured | — |
| 69 | P3 | backend | `backend/testing/test_digest_direction_progress.py` | 3,284 | 61 | high (3000–5000 LOC) | 25.744s measured | — |
| 70 | P3 | backend | `backend/testing/test_reaper_sweeps_compute_image.py` | 3,046 | 77 | high (3000–5000 LOC) | 25.598s measured | — |
| 71 | P3 | backend | `backend/testing/test_pipeline_reliability.py` | 4,259 | 54 | high (3000–5000 LOC) | 23.104s measured | — |
| 72 | P3 | backend | `backend/testing/test_message_audience.py` | 3,605 | 136 | high (3000–5000 LOC) | 22.745s measured | — |
| 73 | P3 | backend | `backend/testing/test_no_ci_merge_gate_e2e.py` | 3,800 | 59 | high (3000–5000 LOC) | 22.715s measured | — |
| 74 | P3 | backend | `backend/testing/test_failure_classification.py` | 4,481 | 395 | high (3000–5000 LOC) | 21.006s measured | — |
| 75 | P3 | backend | `backend/testing/test_hotfix_backmerge.py` | 4,062 | 85 | high (3000–5000 LOC) | 18.743s measured | — |
| 76 | P3 | backend | `backend/testing/test_notification_dispatch.py` | 3,458 | 121 | high (3000–5000 LOC) | 15.923s measured | — |
| 77 | P3 | admin-ui | `admin-ui/src/components/ComputeJobsPanel.test.tsx` | 4,912 | 219 | high (3000–5000 LOC) | 11.886s measured | — |
| 78 | P3 | backend | `backend/testing/test_onboard.py` | 4,672 | 142 | high (3000–5000 LOC) | 10.811s measured | — |
| 79 | P3 | backend | `backend/testing/test_blocked_need_resolution.py` | 4,481 | 138 | high (3000–5000 LOC) | 8.477s measured | — |
| 80 | P3 | backend | `backend/testing/test_pack_under_budget.py` | 3,236 | 67 | high (3000–5000 LOC) | 6.628s measured | — |
| 81 | P3 | agent | `agent/src/handlers/review-fix.test.ts` | 4,604 | 242 | high (3000–5000 LOC) | 4.556s measured | — |
| 82 | P3 | backend | `backend/testing/test_admin_routers__messages.py` | 4,964 | 141 | high (3000–5000 LOC) | 3.650s measured | protected; declared floor 141 |
| 83 | P3 | backend | `backend/testing/test_ci_compliance_gate.py` | 3,019 | 55 | high (3000–5000 LOC) | 1.847s measured | — |
| 84 | P3 | agent | `agent/src/index.watchdog.test.ts` | 3,507 | 113 | high (3000–5000 LOC) | 1.616s measured | — |
| 85 | P3 | backend | `backend/testing/test_alert_dispatch.py` | 3,397 | 83 | high (3000–5000 LOC) | 1.590s measured | — |
| 86 | P3 | backend | `backend/testing/test_repo_bootstrap.py` | 3,989 | 81 | high (3000–5000 LOC) | 0.380s measured | — |
| 87 | P3 | agent | `agent/src/handlers/standard.test.ts` | 3,316 | 214 | high (3000–5000 LOC) | 0.281s measured | — |
| 88 | P3 | agent | `agent/src/handlers/strategic-resident-lib.test.ts` | 4,322 | 310 | high (3000–5000 LOC) | 0.104s measured | protected; declared floor 310 |
| 89 | P3 | agent | `agent/src/utils/test-runners.test.ts` | 3,880 | 368 | high (3000–5000 LOC) | 0.084s measured | — |
| 90 | P5 | backend | `backend/testing/test_release_scoring.py` | 2,266 | 109 | moderate (1500–2999 LOC) | 172.678s measured | — |
| 91 | P5 | backend | `backend/testing/test_anomaly_actions.py` | 1,525 | 75 | moderate (1500–2999 LOC) | 136.702s measured | — |
| 92 | P5 | backend | `backend/testing/test_review_learning.py` | 2,804 | 85 | moderate (1500–2999 LOC) | 135.006s measured | — |
| 93 | P5 | backend | `backend/testing/test_strategic_spawning_capability_verification.py` | 2,575 | 76 | moderate (1500–2999 LOC) | 105.758s measured | — |
| 94 | P5 | backend | `backend/testing/test_qualification_state_resolver_dimensions.py` | 2,300 | 71 | moderate (1500–2999 LOC) | 86.090s measured | — |
| 95 | P5 | backend | `backend/testing/test_strategic_spawning_summaries.py` | 2,357 | 54 | moderate (1500–2999 LOC) | 83.890s measured | — |
| 96 | P5 | backend | `backend/testing/test_strategic_spawning_portfolio_messages.py` | 2,612 | 75 | moderate (1500–2999 LOC) | 66.628s measured | — |
| 97 | P5 | backend | `backend/testing/test_strategic_context_package.py` | 1,920 | 47 | moderate (1500–2999 LOC) | 56.956s measured | — |
| 98 | P5 | backend | `backend/testing/test_strategic_session_messages.py` | 2,949 | 42 | moderate (1500–2999 LOC) | 49.513s measured | protected; declared floor 42 |
| 99 | P5 | backend | `backend/testing/test_strategic_spawning_capability_prediction.py` | 2,607 | 72 | moderate (1500–2999 LOC) | 49.466s measured | — |
| 100 | P5 | backend | `backend/testing/test_admin_project_config.py` | 1,906 | 78 | moderate (1500–2999 LOC) | 45.335s measured | — |
| 101 | P5 | agent | `agent/src/handlers/strategic-helpers.test.ts` | 2,361 | 118 | moderate (1500–2999 LOC) | 39.534s measured | protected; declared floor 118 |
| 102 | P5 | backend | `backend/testing/test_admin_pool_accounts.py` | 2,864 | 52 | moderate (1500–2999 LOC) | 37.238s measured | — |
| 103 | P5 | backend | `backend/testing/test_admin_approval_authority.py` | 1,510 | 16 | moderate (1500–2999 LOC) | 36.201s measured | — |
| 104 | P5 | backend | `backend/testing/test_admin_runners.py` | 1,826 | 62 | moderate (1500–2999 LOC) | 34.994s measured | — |
| 105 | P5 | backend | `backend/testing/test_alerts.py` | 2,287 | 60 | moderate (1500–2999 LOC) | 33.602s measured | — |
| 106 | P5 | agent | `agent/src/prompts/check-helper.test.ts` | 2,350 | 69 | moderate (1500–2999 LOC) | 30.009s measured | — |
| 107 | P5 | backend | `backend/testing/test_staged_directions.py` | 1,694 | 40 | moderate (1500–2999 LOC) | 29.493s measured | — |
| 108 | P5 | agent | `agent/src/prompt.guidance.test.ts` | 2,153 | 176 | moderate (1500–2999 LOC) | 28.990s measured | — |
| 109 | P5 | backend | `backend/testing/test_api.py` | 1,587 | 39 | moderate (1500–2999 LOC) | 26.354s measured | — |
| 110 | P5 | backend | `backend/testing/test_warm_pool_dispatch.py` | 1,961 | 40 | moderate (1500–2999 LOC) | 24.592s measured | — |
| 111 | P5 | backend | `backend/testing/test_generated_shell.py` | 2,724 | 111 | moderate (1500–2999 LOC) | 23.466s measured | — |
| 112 | P5 | backend | `backend/testing/test_admin_routers__notifications.py` | 1,595 | 54 | moderate (1500–2999 LOC) | 23.459s measured | — |
| 113 | P5 | backend | `backend/testing/test_review_merge_authority.py` | 1,540 | 18 | moderate (1500–2999 LOC) | 23.223s measured | — |
| 114 | P5 | backend | `backend/testing/test_discovery_review.py` | 2,938 | 61 | moderate (1500–2999 LOC) | 21.216s measured | — |
| 115 | P5 | backend | `backend/testing/test_admin_routers__strategic_directions.py` | 2,071 | 29 | moderate (1500–2999 LOC) | 20.878s measured | protected; declared floor 29 |
| 116 | P5 | backend | `backend/testing/test_audit_finding_class_guards.py` | 2,069 | 35 | moderate (1500–2999 LOC) | 20.635s measured | — |
| 117 | P5 | backend | `backend/testing/test_approved_pr_stranding.py` | 1,687 | 34 | moderate (1500–2999 LOC) | 20.351s measured | — |
| 118 | P5 | backend | `backend/testing/test_release_commit_authorship.py` | 2,412 | 53 | moderate (1500–2999 LOC) | 18.205s measured | — |
| 119 | P5 | backend | `backend/testing/test_deploy_health.py` | 2,237 | 81 | moderate (1500–2999 LOC) | 17.586s measured | — |
| 120 | P5 | backend | `backend/testing/test_migration_apply_gate.py` | 2,900 | 86 | moderate (1500–2999 LOC) | 17.539s measured | — |
| 121 | P5 | backend | `backend/testing/test_compute_park_wake.py` | 1,721 | 12 | moderate (1500–2999 LOC) | 17.022s measured | — |
| 122 | P5 | backend | `backend/testing/test_strategic_spawning_scope_budget.py` | 2,578 | 41 | moderate (1500–2999 LOC) | 14.284s measured | — |
| 123 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.test.tsx` | 2,818 | 78 | moderate (1500–2999 LOC) | 14.213s measured | protected; declared floor 78 |
| 124 | P5 | agent | `agent/src/prompts/code-change-helpers.test.ts` | 1,900 | 90 | moderate (1500–2999 LOC) | 13.227s measured | — |
| 125 | P5 | backend | `backend/testing/test_warm_pool.py` | 1,591 | 45 | moderate (1500–2999 LOC) | 13.002s measured | — |
| 126 | P5 | backend | `backend/testing/test_workday_resident_invariants.py` | 1,646 | 38 | moderate (1500–2999 LOC) | 12.536s measured | protected; declared floor 38 |
| 127 | P5 | backend | `backend/testing/test_lock_window.py` | 2,373 | 36 | moderate (1500–2999 LOC) | 11.653s measured | — |
| 128 | P5 | backend | `backend/testing/test_task_locking.py` | 2,456 | 62 | moderate (1500–2999 LOC) | 11.250s measured | — |
| 129 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.send.test.tsx` | 1,908 | 38 | moderate (1500–2999 LOC) | 11.185s measured | — |
| 130 | P5 | backend | `backend/testing/test_service_credentials.py` | 1,500 | 30 | moderate (1500–2999 LOC) | 10.779s measured | — |
| 131 | P5 | backend | `backend/testing/test_auto_remediation.py` | 2,857 | 62 | moderate (1500–2999 LOC) | 9.882s measured | — |
| 132 | P5 | backend | `backend/testing/test_diagnostics.py` | 1,505 | 59 | moderate (1500–2999 LOC) | 9.673s measured | — |
| 133 | P5 | backend | `backend/testing/test_strategic_spawning_type_selection.py` | 2,620 | 64 | moderate (1500–2999 LOC) | 9.580s measured | — |
| 134 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.filters.test.tsx` | 1,701 | 55 | moderate (1500–2999 LOC) | 8.977s measured | — |
| 135 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.interactions.test.tsx` | 1,846 | 52 | moderate (1500–2999 LOC) | 8.913s measured | — |
| 136 | P5 | backend | `backend/testing/test_completion_debt.py` | 2,246 | 56 | moderate (1500–2999 LOC) | 8.440s measured | — |
| 137 | P5 | backend | `backend/testing/test_cost_telemetry_reconciliation.py` | 1,694 | 42 | moderate (1500–2999 LOC) | 7.868s measured | — |
| 138 | P5 | backend | `backend/testing/test_check_gate_fail_closed.py` | 1,503 | 35 | moderate (1500–2999 LOC) | 7.637s measured | — |
| 139 | P5 | backend | `backend/testing/test_strategic_spawning_outcome_evaluation.py` | 2,605 | 48 | moderate (1500–2999 LOC) | 6.466s measured | — |
| 140 | P5 | backend | `backend/testing/test_evergreen_backlog.py` | 1,539 | 50 | moderate (1500–2999 LOC) | 6.364s measured | — |
| 141 | P5 | backend | `backend/testing/test_compute_gcp_startup.py` | 1,991 | 62 | moderate (1500–2999 LOC) | 5.984s measured | — |
| 142 | P5 | backend | `backend/testing/test_warm_backlog_reuse.py` | 2,019 | 53 | moderate (1500–2999 LOC) | 5.982s measured | — |
| 143 | P5 | backend | `backend/testing/test_payload_schemas.py` | 2,161 | 148 | moderate (1500–2999 LOC) | 5.802s measured | — |
| 144 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.notifications.test.tsx` | 1,524 | 65 | moderate (1500–2999 LOC) | 5.501s measured | — |
| 145 | P5 | agent | `agent/src/handlers/audits.test.ts` | 2,190 | 92 | moderate (1500–2999 LOC) | 5.229s measured | — |
| 146 | P5 | admin-ui | `admin-ui/src/components/DashboardHome.test.tsx` | 2,682 | 80 | moderate (1500–2999 LOC) | 5.207s measured | — |
| 147 | P5 | backend | `backend/testing/test_build_config_commands.py` | 1,718 | 56 | moderate (1500–2999 LOC) | 5.114s measured | — |
| 148 | P5 | backend | `backend/testing/test_failure_residual.py` | 1,518 | 59 | moderate (1500–2999 LOC) | 4.634s measured | — |
| 149 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.advanced.test.tsx` | 1,932 | 32 | moderate (1500–2999 LOC) | 4.328s measured | protected; declared floor 32 |
| 150 | P5 | backend | `backend/testing/test_operator_inbox_liveness.py` | 2,505 | 50 | moderate (1500–2999 LOC) | 3.890s measured | — |
| 151 | P5 | admin-ui | `admin-ui/src/components/KnowledgeBrowser.test.tsx` | 1,689 | 46 | moderate (1500–2999 LOC) | 3.815s measured | — |
| 152 | P5 | admin-ui | `admin-ui/src/components/SrTrendBreakdown.test.tsx` | 2,335 | 149 | moderate (1500–2999 LOC) | 3.713s measured | — |
| 153 | P5 | backend | `backend/testing/test_lane_baseline_snapshot.py` | 1,520 | 45 | moderate (1500–2999 LOC) | 3.637s measured | — |
| 154 | P5 | backend | `backend/testing/test_discovery_review_packet_budget.py` | 1,624 | 25 | moderate (1500–2999 LOC) | 3.599s measured | — |
| 155 | P5 | admin-ui | `admin-ui/src/components/StrategicContextRail.test.tsx` | 2,550 | 60 | moderate (1500–2999 LOC) | 3.312s measured | protected; declared floor 60 |
| 156 | P5 | backend | `backend/testing/test_strategic_spawning_version_phase.py` | 2,800 | 42 | moderate (1500–2999 LOC) | 3.265s measured | — |
| 157 | P5 | admin-ui | `admin-ui/src/components/WarmVmPanel.test.tsx` | 1,769 | 67 | moderate (1500–2999 LOC) | 3.029s measured | — |
| 158 | P5 | agent | `agent/src/handlers/strategic.test.ts` | 2,923 | 89 | moderate (1500–2999 LOC) | 3.023s measured | — |
| 159 | P5 | admin-ui | `admin-ui/src/components/FleetRunnersPanel.test.tsx` | 2,942 | 94 | moderate (1500–2999 LOC) | 2.946s measured | — |
| 160 | P5 | admin-ui | `admin-ui/src/components/TaskDetailDrawerMeta.test.tsx` | 2,200 | 109 | moderate (1500–2999 LOC) | 2.512s measured | — |
| 161 | P5 | agent | `agent/src/index.llm-runner.test.ts` | 1,969 | 95 | moderate (1500–2999 LOC) | 2.156s measured | — |
| 162 | P5 | backend | `backend/testing/test_test_durations_freshness.py` | 1,669 | 45 | moderate (1500–2999 LOC) | 2.129s measured | — |
| 163 | P5 | backend | `backend/testing/test_orchestrator_liveness.py` | 2,379 | 69 | moderate (1500–2999 LOC) | 1.893s measured | — |
| 164 | P5 | agent | `agent/src/index.provider-runner.test.ts` | 1,880 | 134 | moderate (1500–2999 LOC) | 1.864s measured | protected; declared floor 134 |
| 165 | P5 | backend | `backend/testing/test_fleet_utilization.py` | 1,527 | 42 | moderate (1500–2999 LOC) | 1.586s measured | — |
| 166 | P5 | backend | `backend/testing/test_empty_repo_onboarding.py` | 1,837 | 51 | moderate (1500–2999 LOC) | 1.565s measured | — |
| 167 | P5 | agent | `agent/src/index.pool-mode.test.ts` | 2,602 | 73 | moderate (1500–2999 LOC) | 1.499s measured | — |
| 168 | P5 | admin-ui | `admin-ui/src/hooks/useWorkbenchCommands.test.tsx` | 2,238 | 58 | moderate (1500–2999 LOC) | 1.277s measured | — |
| 169 | P5 | backend | `backend/testing/test_review_loop_convergence.py` | 2,684 | 52 | moderate (1500–2999 LOC) | 1.084s measured | — |
| 170 | P5 | backend | `backend/testing/test_warm_vm_integration.py` | 2,999 | 28 | moderate (1500–2999 LOC) | 0.992s measured | — |
| 171 | P5 | admin-ui | `admin-ui/src/hooks/useTaskModal.test.ts` | 1,698 | 79 | moderate (1500–2999 LOC) | 0.848s measured | — |
| 172 | P5 | backend | `backend/testing/test_migration_chain.py` | 1,697 | 80 | moderate (1500–2999 LOC) | 0.714s measured | — |
| 173 | P5 | backend | `backend/testing/test_size_offender_governance.py` | 2,956 | 87 | moderate (1500–2999 LOC) | 0.565s measured | — |
| 174 | P5 | backend | `backend/testing/test_dependency_contracts.py` | 1,804 | 44 | moderate (1500–2999 LOC) | 0.551s measured | — |
| 175 | P5 | backend | `backend/testing/test_ci_resolution.py` | 2,942 | 165 | moderate (1500–2999 LOC) | 0.331s measured | — |
| 176 | P5 | backend | `backend/testing/test_finding_deduplication.py` | 2,485 | 131 | moderate (1500–2999 LOC) | 0.286s measured | — |
| 177 | P5 | backend | `backend/testing/test_migration_divergence.py` | 2,302 | 76 | moderate (1500–2999 LOC) | 0.275s measured | — |
| 178 | P5 | backend | `backend/testing/test_ci_compliance.py` | 1,617 | 37 | moderate (1500–2999 LOC) | 0.253s measured | — |
| 179 | P5 | backend | `backend/testing/test_health_zone_pool.py` | 2,149 | 118 | moderate (1500–2999 LOC) | 0.236s measured | — |
| 180 | P5 | admin-ui | `admin-ui/src/hooks/useLogViewer.test.ts` | 1,593 | 48 | moderate (1500–2999 LOC) | 0.217s measured | — |
| 181 | P5 | backend | `backend/testing/test_size_limits_checker.py` | 2,537 | 102 | moderate (1500–2999 LOC) | 0.162s measured | — |
| 182 | P5 | agent | `agent/src/handlers/specialized.test.ts` | 2,570 | 146 | moderate (1500–2999 LOC) | 0.143s measured | — |
| 183 | P5 | agent | `agent/src/index.test.ts` | 1,885 | 201 | moderate (1500–2999 LOC) | 0.117s measured | — |
| 184 | P5 | backend | `backend/testing/test_llm_runtime.py` | 1,885 | 47 | moderate (1500–2999 LOC) | 0.100s measured | — |
| 185 | P5 | agent | `agent/src/index.utilities.test.ts` | 1,556 | 131 | moderate (1500–2999 LOC) | 0.091s measured | — |
| 186 | P5 | backend | `backend/testing/test_max_plan_pool_usage.py` | 2,499 | 96 | moderate (1500–2999 LOC) | 0.087s measured | — |
| 187 | P5 | agent | `agent/src/handlers/strategic-sections.test.ts` | 2,963 | 198 | moderate (1500–2999 LOC) | 0.085s measured | — |
| 188 | P5 | backend | `backend/testing/test_impacted_tests.py` | 1,576 | 62 | moderate (1500–2999 LOC) | 0.082s measured | — |
| 189 | P5 | agent | `agent/src/workspacePrep.test.ts` | 1,887 | 74 | moderate (1500–2999 LOC) | 0.063s measured | protected; declared floor 74 |
| 190 | P5 | admin-ui | `admin-ui/src/components/MessagesPanel.utils.test.ts` | 1,622 | 168 | moderate (1500–2999 LOC) | 0.063s measured | — |
| 191 | P5 | agent | `agent/src/prompt.project-context.test.ts` | 2,116 | 158 | moderate (1500–2999 LOC) | 0.062s measured | — |
| 192 | P5 | agent | `agent/src/prompt.test.ts` | 2,513 | 178 | moderate (1500–2999 LOC) | 0.048s measured | — |
| 193 | P5 | backend | `backend/testing/test_artifact_refs.py` | 1,546 | 29 | moderate (1500–2999 LOC) | 0.036s measured | — |
| 194 | P5 | agent | `agent/src/prompts/thinker.test.ts` | 1,904 | 101 | moderate (1500–2999 LOC) | 0.032s measured | — |
| 195 | P5 | agent | `agent/src/prompt.format.test.ts` | 1,512 | 102 | moderate (1500–2999 LOC) | 0.029s measured | — |
| 196 | P5 | agent | `agent/src/prompts/pr-helper.test.ts` | 2,353 | 41 | moderate (1500–2999 LOC) | 0.025s measured | — |
| 197 | G | backend | `backend/testing/test_scripts_test_lane.py` | 389 | 3 | under 1500 LOC | 208.064s measured | — |
| 198 | G | backend | `backend/testing/test_task_outcomes.py` | 1,375 | 108 | under 1500 LOC | 187.368s measured | — |
| 199 | G | backend | `backend/testing/test_task_evaluators.py` | 1,172 | 79 | under 1500 LOC | 153.388s measured | — |
| 200 | G | backend | `backend/testing/test_qualification_triggers.py` | 1,059 | 80 | under 1500 LOC | 143.296s measured | — |
| 201 | G | backend | `backend/testing/test_rate_limit_detection.py` | 995 | 68 | under 1500 LOC | 130.206s measured | — |
| 202 | G | backend | `backend/testing/test_task_constructor_authority_manifest.py` | 606 | 9 | under 1500 LOC | 118.662s measured | — |
| 203 | G | backend | `backend/testing/test_provider_settings_bag.py` | 945 | 53 | under 1500 LOC | 114.172s measured | — |
| 204 | G | backend | `backend/testing/test_research_context_eviction.py` | 1,013 | 64 | under 1500 LOC | 108.568s measured | — |
| 205 | G | backend | `backend/testing/test_failure_layer_classifier.py` | 542 | 72 | under 1500 LOC | 100.534s measured | — |
| 206 | G | backend | `backend/testing/test_providers.py` | 1,256 | 80 | under 1500 LOC | 98.906s measured | — |
| 207 | G | backend | `backend/testing/test_migration_0724_name_reuse_after_revocation.py` | 287 | 6 | under 1500 LOC | 90.314s measured | — |
| 208 | G | backend | `backend/testing/test_migration_0681_0694_preservation.py` | 1,093 | 3 | under 1500 LOC | 86.621s measured | — |
| 209 | G | backend | `backend/testing/test_rollup_orphan_prs.py` | 350 | 32 | under 1500 LOC | 79.804s measured | — |
| 210 | G | backend | `backend/testing/test_project_resource_envelope.py` | 883 | 65 | under 1500 LOC | 77.566s measured | — |
| 211 | G | backend | `backend/testing/test_rollup_state.py` | 209 | 24 | under 1500 LOC | 62.286s measured | — |
| 212 | G | backend | `backend/testing/test_harness_timeout_budgets.py` | 1,159 | 20 | under 1500 LOC | 57.849s measured | — |
| 213 | G | agent | `agent/src/prompts/terminal-checkpoint-helpers.test.ts` | 435 | 12 | under 1500 LOC | 52.983s measured | — |
| 214 | G | backend | `backend/testing/test_scope_complexity.py` | 380 | 32 | under 1500 LOC | 52.778s measured | — |
| 215 | G | backend | `backend/testing/test_migration_0640_0682_workday_chain.py` | 334 | 1 | under 1500 LOC | 50.136s measured | — |
| 216 | G | backend | `backend/testing/test_local_runner_viewable_selection.py` | 1,039 | 43 | under 1500 LOC | 47.838s measured | — |
| 217 | G | backend | `backend/testing/test_review_policy_parity.py` | 372 | 22 | under 1500 LOC | 47.704s measured | — |
| 218 | G | backend | `backend/testing/test_playbook_storage.py` | 723 | 24 | under 1500 LOC | 46.644s measured | — |
| 219 | G | backend | `backend/testing/test_release_audit_contract.py` | 785 | 22 | under 1500 LOC | 45.066s measured | — |
| 220 | G | backend | `backend/testing/test_qualification_dimensions.py` | 180 | 12 | under 1500 LOC | 36.808s measured | — |
| 221 | G | backend | `backend/testing/test_runner_progress_protocol.py` | 467 | 19 | under 1500 LOC | 35.190s measured | — |
| 222 | G | backend | `backend/testing/test_strategic_trigger.py` | 809 | 52 | under 1500 LOC | 33.716s measured | — |
| 223 | G | backend | `backend/testing/test_migration_0722_discovery_controller.py` | 147 | 1 | under 1500 LOC | 32.526s measured | — |
| 224 | G | backend | `backend/testing/test_strategic_cycle.py` | 1,472 | 60 | under 1500 LOC | 31.970s measured | — |
| 225 | G | backend | `backend/testing/test_release_readiness.py` | 1,345 | 48 | under 1500 LOC | 31.104s measured | — |
| 226 | G | backend | `backend/testing/test_admin_routers__problem_model.py` | 1,230 | 39 | under 1500 LOC | 31.084s measured | — |
| 227 | G | backend | `backend/testing/test_durations_measurability.py` | 280 | 10 | under 1500 LOC | 30.756s measured | — |
| 228 | G | backend | `backend/testing/test_agent_tasks.py` | 1,393 | 41 | under 1500 LOC | 30.227s measured | — |
| 229 | G | backend | `backend/testing/test_alert_policy.py` | 444 | 21 | under 1500 LOC | 29.090s measured | — |
| 230 | G | backend | `backend/testing/test_playbook_learning.py` | 245 | 11 | under 1500 LOC | 28.850s measured | — |
| 231 | G | backend | `backend/testing/test_anomaly_detection.py` | 1,298 | 46 | under 1500 LOC | 28.734s measured | — |
| 232 | G | backend | `backend/testing/test_running_update_merge.py` | 100 | 8 | under 1500 LOC | 28.518s measured | — |
| 233 | G | agent | `agent/src/handlers/bake-smoke-run.test.ts` | 144 | 6 | under 1500 LOC | 28.446s measured | — |
| 234 | G | backend | `backend/testing/test_vacuous_guard_shapes.py` | 962 | 7 | under 1500 LOC | 24.779s measured | — |
| 235 | G | backend | `backend/testing/test_work_session_commands.py` | 755 | 16 | under 1500 LOC | 24.280s measured | — |
| 236 | G | backend | `backend/testing/test_stranded_conversation_message_sweep.py` | 727 | 21 | under 1500 LOC | 23.895s measured | — |
| 237 | G | backend | `backend/testing/test_admin_approval_merge_submission_recovery.py` | 1,272 | 16 | under 1500 LOC | 23.418s measured | — |
| 238 | G | backend | `backend/testing/test_review_policy.py` | 112 | 13 | under 1500 LOC | 23.108s measured | — |
| 239 | G | backend | `backend/testing/test_scripts_dir_resolution_guard.py` | 617 | 11 | under 1500 LOC | 22.819s measured | — |
| 240 | G | backend | `backend/testing/test_time_utils.py` | 195 | 20 | under 1500 LOC | 22.100s measured | — |
| 241 | G | backend | `backend/testing/test_release_artifacts.py` | 989 | 31 | under 1500 LOC | 21.982s measured | — |
| 242 | G | backend | `backend/testing/test_admin_pipeline_observability.py` | 953 | 21 | under 1500 LOC | 21.160s measured | — |
| 243 | G | backend | `backend/testing/test_unactionable_approval_holds.py` | 890 | 33 | under 1500 LOC | 21.063s measured | — |
| 244 | G | backend | `backend/testing/test_release_stall_alerts.py` | 1,027 | 28 | under 1500 LOC | 20.466s measured | — |
| 245 | G | backend | `backend/testing/test_mode_knob_graduation.py` | 1,188 | 22 | under 1500 LOC | 20.344s measured | — |
| 246 | G | backend | `backend/testing/test_rollup_review_respawn_cap.py` | 586 | 15 | under 1500 LOC | 19.004s measured | — |
| 247 | G | backend | `backend/testing/test_release_qualification_pause_deferral.py` | 628 | 6 | under 1500 LOC | 18.377s measured | — |
| 248 | G | backend | `backend/testing/test_admin_routers__runners.py` | 1,317 | 33 | under 1500 LOC | 18.104s measured | — |
| 249 | G | backend | `backend/testing/test_agent__claim_core_auth.py` | 725 | 15 | under 1500 LOC | 18.041s measured | — |
| 250 | G | backend | `backend/testing/test_approval_exact_target_publication.py` | 1,275 | 9 | under 1500 LOC | 17.933s measured | — |
| 251 | G | backend | `backend/testing/test_admin_routers__users.py` | 876 | 34 | under 1500 LOC | 17.656s measured | — |
| 252 | G | backend | `backend/testing/test_acceptance_criteria__trials.py` | 586 | 13 | under 1500 LOC | 17.408s measured | — |
| 253 | G | backend | `backend/testing/test_release_promotion_metric_manifest.py` | 173 | 6 | under 1500 LOC | 17.020s measured | — |
| 254 | G | backend | `backend/testing/test_admin_acceptance_criteria_router.py` | 1,193 | 31 | under 1500 LOC | 16.509s measured | — |
| 255 | G | agent | `agent/src/vitest.globalSetup.test.ts` | 237 | 16 | under 1500 LOC | 16.428s measured | — |
| 256 | G | backend | `backend/testing/test_url_security.py` | 179 | 11 | under 1500 LOC | 16.416s measured | — |
| 257 | G | backend | `backend/testing/test_admin_tasks_fix_loop_summary.py` | 158 | 16 | under 1500 LOC | 16.378s measured | — |
| 258 | G | backend | `backend/testing/test_viewable_watch.py` | 335 | 19 | under 1500 LOC | 16.048s measured | — |
| 259 | G | backend | `backend/testing/test_pipeline_alert_scan.py` | 736 | 21 | under 1500 LOC | 15.851s measured | — |
| 260 | G | agent | `agent/src/handlers/walkthrough-stack.test.ts` | 404 | 14 | under 1500 LOC | 15.826s measured | — |
| 261 | G | backend | `backend/testing/test_review_park_disposition.py` | 994 | 26 | under 1500 LOC | 15.819s measured | — |
| 262 | G | backend | `backend/testing/test_conversation_coordinator_two_speed.py` | 1,445 | 17 | under 1500 LOC | 15.452s measured | protected; declared floor 17 |
| 263 | G | backend | `backend/testing/test_admin_agent_ops.py` | 895 | 10 | under 1500 LOC | 15.303s measured | — |
| 264 | G | backend | `backend/testing/test_agent_pipeline_visibility.py` | 807 | 27 | under 1500 LOC | 15.151s measured | — |
| 265 | G | backend | `backend/testing/test_admin_warm_pool.py` | 865 | 28 | under 1500 LOC | 15.046s measured | — |
| 266 | G | backend | `backend/testing/test_runtime_config_defaults.py` | 310 | 7 | under 1500 LOC | 14.319s measured | — |
| 267 | G | backend | `backend/testing/test_workday_artifact_materialization.py` | 1,011 | 17 | under 1500 LOC | 14.190s measured | protected; declared floor 17 |
| 268 | G | backend | `backend/testing/test_resource_envelope_enforcement.py` | 1,189 | 27 | under 1500 LOC | 14.188s measured | — |
| 269 | G | backend | `backend/testing/test_release_qualifier_audit.py` | 1,145 | 18 | under 1500 LOC | 13.918s measured | — |
| 270 | G | backend | `backend/testing/test_admin_project_roles.py` | 741 | 10 | under 1500 LOC | 13.756s measured | — |
| 271 | G | backend | `backend/testing/test_reaper_post_orphan_sweep.py` | 608 | 10 | under 1500 LOC | 13.719s measured | — |
| 272 | G | backend | `backend/testing/test_release_qualifier_walkthrough_evidence.py` | 968 | 10 | under 1500 LOC | 13.320s measured | — |
| 273 | G | backend | `backend/testing/test_admin_playbook.py` | 559 | 17 | under 1500 LOC | 13.160s measured | — |
| 274 | G | backend | `backend/testing/test_playbook_auto.py` | 368 | 14 | under 1500 LOC | 13.002s measured | — |
| 275 | G | backend | `backend/testing/test_viewport_coverage.py` | 170 | 7 | under 1500 LOC | 12.910s measured | — |
| 276 | G | backend | `backend/testing/test_system_state.py` | 713 | 22 | under 1500 LOC | 12.798s measured | — |
| 277 | G | backend | `backend/testing/test_rollup_size_preflight.py` | 1,259 | 38 | under 1500 LOC | 12.728s measured | — |
| 278 | G | backend | `backend/testing/test_alert_mint_sites.py` | 422 | 6 | under 1500 LOC | 12.544s measured | — |
| 279 | G | backend | `backend/testing/test_project_operator_access.py` | 1,006 | 16 | under 1500 LOC | 12.497s measured | — |
| 280 | G | backend | `backend/testing/test_release_qualifier__trigger_and_dispatch_regressions.py` | 946 | 17 | under 1500 LOC | 12.452s measured | — |
| 281 | G | backend | `backend/testing/test_admin_approval_comment_recovery.py` | 514 | 12 | under 1500 LOC | 12.320s measured | — |
| 282 | G | backend | `backend/testing/test_rollup_lineage_fence.py` | 1,089 | 30 | under 1500 LOC | 12.206s measured | — |
| 283 | G | backend | `backend/testing/test_strategic_spawn_requests.py` | 1,089 | 20 | under 1500 LOC | 11.955s measured | — |
| 284 | G | agent | `agent/src/prompts/code-change-helpers.blocked-receipt.test.ts` | 167 | 2 | under 1500 LOC | 11.482s measured | — |
| 285 | G | backend | `backend/testing/test_provider_settings.py` | 873 | 38 | under 1500 LOC | 11.456s measured | — |
| 286 | G | backend | `backend/testing/test_admin_approval_dedup_authority.py` | 382 | 4 | under 1500 LOC | 11.431s measured | — |
| 287 | G | backend | `backend/testing/test_research_spawn_pending_recovery.py` | 518 | 8 | under 1500 LOC | 11.364s measured | — |
| 288 | G | backend | `backend/testing/test_release_prepromotion_rehearsal.py` | 987 | 26 | under 1500 LOC | 11.068s measured | — |
| 289 | G | backend | `backend/testing/test_strategic_conversation_query.py` | 923 | 15 | under 1500 LOC | 11.062s measured | — |
| 290 | G | backend | `backend/testing/test_review_claim_fence.py` | 678 | 11 | under 1500 LOC | 10.997s measured | — |
| 291 | G | backend | `backend/testing/test_compute_quota.py` | 679 | 13 | under 1500 LOC | 10.912s measured | — |
| 292 | G | backend | `backend/testing/test_compute_image_environment.py` | 937 | 23 | under 1500 LOC | 10.626s measured | — |
| 293 | G | backend | `backend/testing/test_admin_routers__autonomous.py` | 1,171 | 27 | under 1500 LOC | 10.581s measured | — |
| 294 | G | backend | `backend/testing/test_attempt24_release_safety.py` | 717 | 9 | under 1500 LOC | 10.224s measured | — |
| 295 | G | backend | `backend/testing/test_self_audit.py` | 1,293 | 39 | under 1500 LOC | 10.168s measured | — |
| 296 | G | backend | `backend/testing/test_timeout_rate_metric_manifest.py` | 146 | 6 | under 1500 LOC | 10.148s measured | — |
| 297 | G | backend | `backend/testing/test_structural_health_audit.py` | 416 | 17 | under 1500 LOC | 10.128s measured | — |
| 298 | G | backend | `backend/testing/test_artifact_download.py` | 1,358 | 46 | under 1500 LOC | 10.092s measured | — |
| 299 | G | backend | `backend/testing/test_acceptance_criteria__walkthrough.py` | 700 | 13 | under 1500 LOC | 9.803s measured | — |
| 300 | G | backend | `backend/testing/test_sequenced_dependent_spawn.py` | 672 | 18 | under 1500 LOC | 9.758s measured | — |
| 301 | G | backend | `backend/testing/test_playbook_learning_modularity.py` | 193 | 2 | under 1500 LOC | 9.580s measured | — |
| 302 | G | backend | `backend/testing/test_drift_detection.py` | 1,051 | 25 | under 1500 LOC | 9.558s measured | — |
| 303 | G | agent | `agent/src/handlers/resident-repo-freshness.test.ts` | 97 | 4 | under 1500 LOC | 9.541s measured | — |
| 304 | G | backend | `backend/testing/test_migration_reservations.py` | 752 | 27 | under 1500 LOC | 9.423s measured | — |
| 305 | G | backend | `backend/testing/test_research_lock_order.py` | 373 | 7 | under 1500 LOC | 9.396s measured | — |
| 306 | G | backend | `backend/testing/test_work_session_metrics.py` | 110 | 3 | under 1500 LOC | 8.840s measured | — |
| 307 | G | backend | `backend/testing/test_github_pulls.py` | 1,155 | 40 | under 1500 LOC | 8.351s measured | — |
| 308 | G | backend | `backend/testing/test_retry_hint_parser.py` | 65 | 5 | under 1500 LOC | 8.208s measured | — |
| 309 | G | backend | `backend/testing/test_strategic_watches.py` | 475 | 11 | under 1500 LOC | 8.031s measured | — |
| 310 | G | backend | `backend/testing/test_strategic_workday_handoff_recovery.py` | 307 | 4 | under 1500 LOC | 7.967s measured | protected; declared floor 4 |
| 311 | G | backend | `backend/testing/test_strategic_consultation_detail.py` | 654 | 15 | under 1500 LOC | 7.942s measured | — |
| 312 | G | backend | `backend/testing/test_migration_0608_projectmessage_foundation.py` | 717 | 10 | under 1500 LOC | 7.721s measured | — |
| 313 | G | backend | `backend/testing/test_strategic_cycle_gating.py` | 798 | 19 | under 1500 LOC | 7.701s measured | — |
| 314 | G | backend | `backend/testing/test_playbook_lifecycle.py` | 388 | 11 | under 1500 LOC | 7.700s measured | — |
| 315 | G | backend | `backend/testing/test_github_onboarding_access.py` | 596 | 15 | under 1500 LOC | 7.441s measured | — |
| 316 | G | backend | `backend/testing/test_project_message_projection.py` | 1,201 | 30 | under 1500 LOC | 7.374s measured | protected; declared floor 30 |
| 317 | G | backend | `backend/testing/test_blocked_retry_authority.py` | 605 | 12 | under 1500 LOC | 7.347s measured | — |
| 318 | G | backend | `backend/testing/test_compute_watchdog.py` | 1,476 | 58 | under 1500 LOC | 7.328s measured | — |
| 319 | G | backend | `backend/testing/test_rollup_handoff_ci_gate.py` | 204 | 6 | under 1500 LOC | 7.164s measured | — |
| 320 | G | backend | `backend/testing/test_review_pending_requeue.py` | 480 | 8 | under 1500 LOC | 7.009s measured | — |
| 321 | G | backend | `backend/testing/test_attempt24_operator_flows.py` | 422 | 7 | under 1500 LOC | 6.967s measured | — |
| 322 | G | backend | `backend/testing/test_gate_revalidation.py` | 700 | 39 | under 1500 LOC | 6.930s measured | — |
| 323 | G | backend | `backend/testing/test_compute__experiments.py` | 1,375 | 35 | under 1500 LOC | 6.879s measured | — |
| 324 | G | backend | `backend/testing/test_review_admission_ownership.py` | 298 | 9 | under 1500 LOC | 6.859s measured | — |
| 325 | G | backend | `backend/testing/test_admin_request_changes_authority.py` | 642 | 5 | under 1500 LOC | 6.848s measured | — |
| 326 | G | backend | `backend/testing/test_dependency_deprecations.py` | 258 | 8 | under 1500 LOC | 6.801s measured | — |
| 327 | G | backend | `backend/testing/test_review_wait_terminal_boundaries.py` | 910 | 9 | under 1500 LOC | 6.756s measured | — |
| 328 | G | admin-ui | `admin-ui/src/components/NewTaskModal.test.tsx` | 680 | 20 | under 1500 LOC | 6.704s measured | — |
| 329 | G | backend | `backend/testing/test_blind_merge_grade.py` | 882 | 18 | under 1500 LOC | 6.703s measured | — |
| 330 | G | backend | `backend/testing/test_review_loop_operator_park.py` | 563 | 25 | under 1500 LOC | 6.620s measured | — |
| 331 | G | backend | `backend/testing/test_strategic_pipeline_actions.py` | 795 | 22 | under 1500 LOC | 6.594s measured | — |
| 332 | G | backend | `backend/testing/test_project_tool_context.py` | 361 | 14 | under 1500 LOC | 6.589s measured | — |
| 333 | G | backend | `backend/testing/test_resolve_dialect_name.py` | 115 | 5 | under 1500 LOC | 6.508s measured | — |
| 334 | G | backend | `backend/testing/test_sr_compute_wiring.py` | 710 | 12 | under 1500 LOC | 6.277s measured | — |
| 335 | G | backend | `backend/testing/test_sr_catch_rate_statistics.py` | 248 | 7 | under 1500 LOC | 6.254s measured | — |
| 336 | G | backend | `backend/testing/test_vm_cost_rates.py` | 41 | 4 | under 1500 LOC | 6.220s measured | — |
| 337 | G | backend | `backend/testing/test_evaluating_handmerge_latch.py` | 642 | 8 | under 1500 LOC | 6.145s measured | — |
| 338 | G | backend | `backend/testing/test_ci_review_wait_dedup_windows.py` | 434 | 4 | under 1500 LOC | 6.111s measured | — |
| 339 | G | backend | `backend/testing/test_discovery_models.py` | 382 | 10 | under 1500 LOC | 6.078s measured | — |
| 340 | G | backend | `backend/testing/test_qualifier_smoketest_retired.py` | 86 | 6 | under 1500 LOC | 5.944s measured | — |
| 341 | G | backend | `backend/testing/test_perf_completion.py` | 345 | 7 | under 1500 LOC | 5.899s measured | — |
| 342 | G | backend | `backend/testing/test_strategic_resident_continuous_liveness.py` | 495 | 8 | under 1500 LOC | 5.858s measured | protected; declared floor 8 |
| 343 | G | backend | `backend/testing/test_test_imports_checker.py` | 764 | 21 | under 1500 LOC | 5.849s measured | — |
| 344 | G | backend | `backend/testing/test_monthly_budget_alerts.py` | 644 | 18 | under 1500 LOC | 5.812s measured | — |
| 345 | G | backend | `backend/testing/test_strategic_autonomous_handoff.py` | 416 | 3 | under 1500 LOC | 5.800s measured | — |
| 346 | G | backend | `backend/testing/test_admin_create_task.py` | 873 | 22 | under 1500 LOC | 5.675s measured | — |
| 347 | G | backend | `backend/testing/test_strategic_memory_envelope.py` | 1,113 | 21 | under 1500 LOC | 5.674s measured | — |
| 348 | G | backend | `backend/testing/test_publication_gate_receipt.py` | 618 | 17 | under 1500 LOC | 5.652s measured | — |
| 349 | G | admin-ui | `admin-ui/src/test/mock-factory-export-coverage.test.ts` | 1,173 | 17 | under 1500 LOC | 5.549s measured | — |
| 350 | G | backend | `backend/testing/test_user_management.py` | 186 | 17 | under 1500 LOC | 5.533s measured | — |
| 351 | G | backend | `backend/testing/test_local_runner_holds.py` | 1,299 | 31 | under 1500 LOC | 5.507s measured | — |
| 352 | G | agent | `agent/src/viewport-snapshot-script.test.ts` | 337 | 18 | under 1500 LOC | 5.405s measured | — |
| 353 | G | backend | `backend/testing/test_release_qualifier_walkthrough_request.py` | 495 | 6 | under 1500 LOC | 5.299s measured | — |
| 354 | G | backend | `backend/testing/test_github_token_resolver.py` | 799 | 20 | under 1500 LOC | 5.069s measured | — |
| 355 | G | backend | `backend/testing/test_migration_0697_retained_telemetry_history.py` | 431 | 6 | under 1500 LOC | 5.041s measured | — |
| 356 | G | backend | `backend/testing/test_schema_invariants.py` | 831 | 14 | under 1500 LOC | 5.029s measured | — |
| 357 | G | admin-ui | `admin-ui/src/components/CommandPalette.test.tsx` | 423 | 15 | under 1500 LOC | 5.018s measured | — |
| 358 | G | backend | `backend/testing/test_strategic_lane_linearization.py` | 1,300 | 20 | under 1500 LOC | 4.980s measured | — |
| 359 | G | backend | `backend/testing/test_admin_warm_vm.py` | 483 | 8 | under 1500 LOC | 4.973s measured | — |
| 360 | G | backend | `backend/testing/test_shard_balance.py` | 543 | 10 | under 1500 LOC | 4.919s measured | — |
| 361 | G | admin-ui | `admin-ui/src/components/NewProjectModal.test.tsx` | 539 | 15 | under 1500 LOC | 4.881s measured | — |
| 362 | G | backend | `backend/testing/test_project_message_delivery.py` | 475 | 8 | under 1500 LOC | 4.860s measured | — |
| 363 | G | admin-ui | `admin-ui/src/hooks/useStrategicSession.test.tsx` | 914 | 26 | under 1500 LOC | 4.852s measured | protected; declared floor 26 |
| 364 | G | backend | `backend/testing/test_review_pipeline_modularity.py` | 209 | 4 | under 1500 LOC | 4.836s measured | — |
| 365 | G | backend | `backend/testing/test_bulk_action.py` | 906 | 15 | under 1500 LOC | 4.685s measured | — |
| 366 | G | backend | `backend/testing/test_evaluating_parent_convergence.py` | 392 | 6 | under 1500 LOC | 4.682s measured | — |
| 367 | G | backend | `backend/testing/test_vm_lifecycle_forensics.py` | 196 | 5 | under 1500 LOC | 4.632s measured | — |
| 368 | G | backend | `backend/testing/test_promotion_read_surface.py` | 823 | 20 | under 1500 LOC | 4.518s measured | — |
| 369 | G | backend | `backend/testing/test_deploy_rollback.py` | 536 | 13 | under 1500 LOC | 4.455s measured | — |
| 370 | G | backend | `backend/testing/test_admin_approval_gate_release_authority.py` | 281 | 4 | under 1500 LOC | 4.431s measured | — |
| 371 | G | backend | `backend/testing/test_direction_saturation.py` | 579 | 12 | under 1500 LOC | 4.424s measured | — |
| 372 | G | backend | `backend/testing/test_cost_attribution.py` | 812 | 21 | under 1500 LOC | 4.404s measured | — |
| 373 | G | backend | `backend/testing/test_release_qualifier_walkthrough_site.py` | 431 | 6 | under 1500 LOC | 4.351s measured | — |
| 374 | G | backend | `backend/testing/test_strategic_consultation_status.py` | 620 | 3 | under 1500 LOC | 4.339s measured | protected; declared floor 3 |
| 375 | G | backend | `backend/testing/test_admin_orchestrator_errors.py` | 282 | 9 | under 1500 LOC | 4.325s measured | — |
| 376 | G | backend | `backend/testing/test_sr_blind_spots.py` | 569 | 36 | under 1500 LOC | 4.255s measured | — |
| 377 | G | admin-ui | `admin-ui/src/components/SettingsOrchestratorSection.test.tsx` | 561 | 31 | under 1500 LOC | 4.234s measured | — |
| 378 | G | backend | `backend/testing/test_automatic_task_creation_authority.py` | 280 | 5 | under 1500 LOC | 4.183s measured | — |
| 379 | G | backend | `backend/testing/test_provider_cost_summary.py` | 401 | 10 | under 1500 LOC | 4.170s measured | — |
| 380 | G | backend | `backend/testing/test_anomaly_proposals.py` | 527 | 13 | under 1500 LOC | 4.142s measured | — |
| 381 | G | backend | `backend/testing/test_project_message_creation_latency.py` | 708 | 15 | under 1500 LOC | 4.130s measured | — |
| 382 | G | admin-ui | `admin-ui/src/components/OperatorWorkspaces.test.tsx` | 580 | 18 | under 1500 LOC | 4.024s measured | — |
| 383 | G | backend | `backend/testing/test_project_telemetry.py` | 508 | 18 | under 1500 LOC | 4.010s measured | — |
| 384 | G | backend | `backend/testing/test_parent_terminal_parity.py` | 315 | 4 | under 1500 LOC | 4.003s measured | — |
| 385 | G | backend | `backend/testing/test_message_convergence.py` | 434 | 21 | under 1500 LOC | 3.954s measured | — |
| 386 | G | backend | `backend/testing/test_release_qualifier_candidate_sha_stamp.py` | 225 | 5 | under 1500 LOC | 3.920s measured | — |
| 387 | G | admin-ui | `admin-ui/src/components/TaskDetailDrawer.test.tsx` | 620 | 29 | under 1500 LOC | 3.896s measured | — |
| 388 | G | backend | `backend/testing/test_memo_completion.py` | 986 | 24 | under 1500 LOC | 3.851s measured | — |
| 389 | G | backend | `backend/testing/test_module_finding_chain.py` | 1,192 | 26 | under 1500 LOC | 3.812s measured | — |
| 390 | G | backend | `backend/testing/test_fix_loop_telemetry.py` | 871 | 26 | under 1500 LOC | 3.777s measured | — |
| 391 | G | backend | `backend/testing/test_approval_gate_config.py` | 377 | 13 | under 1500 LOC | 3.745s measured | — |
| 392 | G | backend | `backend/testing/test_admin_endpoint_cache.py` | 812 | 16 | under 1500 LOC | 3.717s measured | — |
| 393 | G | backend | `backend/testing/test_message_supersession.py` | 428 | 12 | under 1500 LOC | 3.704s measured | — |
| 394 | G | backend | `backend/testing/test_strategic_resident_pipeline_projection.py` | 324 | 3 | under 1500 LOC | 3.676s measured | protected; declared floor 3 |
| 395 | G | backend | `backend/testing/test_strategic_memory_claim_redaction_repair.py` | 414 | 5 | under 1500 LOC | 3.641s measured | — |
| 396 | G | backend | `backend/testing/test_projects.py` | 186 | 8 | under 1500 LOC | 3.595s measured | — |
| 397 | G | agent | `agent/src/handlers/merge-grade.test.ts` | 539 | 34 | under 1500 LOC | 3.577s measured | — |
| 398 | G | backend | `backend/testing/test_warm_pool_readiness.py` | 402 | 6 | under 1500 LOC | 3.574s measured | — |
| 399 | G | backend | `backend/testing/test_release_qualifier__retry_episode_cap.py` | 533 | 18 | under 1500 LOC | 3.564s measured | — |
| 400 | G | backend | `backend/testing/test_admin_promotion_enablement.py` | 114 | 3 | under 1500 LOC | 3.547s measured | — |
| 401 | G | backend | `backend/testing/test_strategic_resident_work_state.py` | 462 | 10 | under 1500 LOC | 3.522s measured | — |
| 402 | G | backend | `backend/testing/test_review_wait_monitor_owned_gate.py` | 206 | 1 | under 1500 LOC | 3.501s measured | — |
| 403 | G | backend | `backend/testing/test_agent_github.py` | 323 | 7 | under 1500 LOC | 3.469s measured | — |
| 404 | G | backend | `backend/testing/test_release_qualifier_contracts.py` | 297 | 7 | under 1500 LOC | 3.469s measured | — |
| 405 | G | backend | `backend/testing/test_discovery_authority_reconciliation.py` | 342 | 5 | under 1500 LOC | 3.453s measured | — |
| 406 | G | backend | `backend/testing/test_rollup_deliverable_premerge_guard.py` | 548 | 11 | under 1500 LOC | 3.448s measured | — |
| 407 | G | backend | `backend/testing/test_workbench_events.py` | 711 | 16 | under 1500 LOC | 3.438s measured | — |
| 408 | G | backend | `backend/testing/test_firebase_auth_coverage.py` | 741 | 51 | under 1500 LOC | 3.408s measured | — |
| 409 | G | backend | `backend/testing/test_project_cascade.py` | 494 | 6 | under 1500 LOC | 3.404s measured | — |
| 410 | G | backend | `backend/testing/test_completion_automatic_task_authority.py` | 306 | 8 | under 1500 LOC | 3.395s measured | — |
| 411 | G | backend | `backend/testing/test_rejection_patterns.py` | 424 | 16 | under 1500 LOC | 3.374s measured | — |
| 412 | G | backend | `backend/testing/test_cost_reconciliation.py` | 952 | 24 | under 1500 LOC | 3.225s measured | — |
| 413 | G | backend | `backend/testing/test_migration_0466_encrypt_max_plan_credentials.py` | 170 | 4 | under 1500 LOC | 3.224s measured | — |
| 414 | G | backend | `backend/testing/test_capability_demand.py` | 939 | 36 | under 1500 LOC | 3.216s measured | — |
| 415 | G | backend | `backend/testing/test_cross_user_isolation.py` | 298 | 11 | under 1500 LOC | 3.194s measured | — |
| 416 | G | backend | `backend/testing/test_system_message_dedupe.py` | 146 | 6 | under 1500 LOC | 3.172s measured | — |
| 417 | G | admin-ui | `admin-ui/src/components/RemediationFunnelSection.test.tsx` | 680 | 42 | under 1500 LOC | 3.162s measured | — |
| 418 | G | backend | `backend/testing/test_admin_serial_console.py` | 353 | 7 | under 1500 LOC | 3.084s measured | — |
| 419 | G | backend | `backend/testing/test_post_commit_recovery_claim.py` | 120 | 3 | under 1500 LOC | 3.024s measured | — |
| 420 | G | backend | `backend/testing/test_runner_binding_capacity_reason.py` | 212 | 10 | under 1500 LOC | 2.998s measured | — |
| 421 | G | backend | `backend/testing/test_recovery_sidecar.py` | 155 | 3 | under 1500 LOC | 2.975s measured | — |
| 422 | G | backend | `backend/testing/test_agent_annotate.py` | 456 | 8 | under 1500 LOC | 2.950s measured | — |
| 423 | G | admin-ui | `admin-ui/src/hooks/useDeploymentState.test.ts` | 772 | 34 | under 1500 LOC | 2.924s measured | — |
| 424 | G | backend | `backend/testing/test_release_qualifier_infra_continuation.py` | 422 | 5 | under 1500 LOC | 2.917s measured | — |
| 425 | G | backend | `backend/testing/test_project_resource_summary.py` | 338 | 4 | under 1500 LOC | 2.896s measured | — |
| 426 | G | backend | `backend/testing/test_rollup_premerge_tip.py` | 276 | 2 | under 1500 LOC | 2.831s measured | — |
| 427 | G | backend | `backend/testing/test_security_audit_completion.py` | 359 | 8 | under 1500 LOC | 2.808s measured | — |
| 428 | G | backend | `backend/testing/test_work_session_command_execution.py` | 520 | 5 | under 1500 LOC | 2.805s measured | — |
| 429 | G | backend | `backend/testing/test_plan_noop.py` | 250 | 8 | under 1500 LOC | 2.793s measured | — |
| 430 | G | backend | `backend/testing/test_wire_timestamp_zone.py` | 577 | 18 | under 1500 LOC | 2.790s measured | — |
| 431 | G | backend | `backend/testing/test_strategic_memory_runtime.py` | 355 | 8 | under 1500 LOC | 2.784s measured | — |
| 432 | G | backend | `backend/testing/test_direction_lineage_spend.py` | 556 | 13 | under 1500 LOC | 2.777s measured | — |
| 433 | G | backend | `backend/testing/test_strategic_events.py` | 485 | 17 | under 1500 LOC | 2.750s measured | — |
| 434 | G | backend | `backend/testing/test_evaluating_fence_hold.py` | 354 | 3 | under 1500 LOC | 2.747s measured | — |
| 435 | G | backend | `backend/testing/test_required_env_deploy_parity.py` | 134 | 5 | under 1500 LOC | 2.706s measured | — |
| 436 | G | backend | `backend/testing/test_direction_table.py` | 357 | 11 | under 1500 LOC | 2.684s measured | — |
| 437 | G | backend | `backend/testing/test_module_size_live_tree.py` | 368 | 8 | under 1500 LOC | 2.648s measured | — |
| 438 | G | backend | `backend/testing/test_admin_activity.py` | 411 | 10 | under 1500 LOC | 2.639s measured | — |
| 439 | G | backend | `backend/testing/test_strategic_effort_tier_evidence.py` | 780 | 26 | under 1500 LOC | 2.634s measured | — |
| 440 | G | agent | `agent/src/prompts/task-type-focus.test.ts` | 634 | 81 | under 1500 LOC | 2.629s measured | — |
| 441 | G | agent | `agent/src/screenshots-pr-section.test.ts` | 177 | 10 | under 1500 LOC | 2.627s measured | — |
| 442 | G | backend | `backend/testing/test_strategic_memory_retrieval.py` | 339 | 7 | under 1500 LOC | 2.618s measured | — |
| 443 | G | backend | `backend/testing/test_message_attachment_retention.py` | 305 | 9 | under 1500 LOC | 2.599s measured | — |
| 444 | G | backend | `backend/testing/test_release_promotion_compute_image_refresh.py` | 124 | 3 | under 1500 LOC | 2.584s measured | — |
| 445 | G | admin-ui | `admin-ui/src/components/ResearchBrowser.test.tsx` | 839 | 33 | under 1500 LOC | 2.577s measured | — |
| 446 | G | backend | `backend/testing/test_required_runtime_env.py` | 261 | 14 | under 1500 LOC | 2.514s measured | — |
| 447 | G | backend | `backend/testing/test_platform_kill_metrics.py` | 465 | 26 | under 1500 LOC | 2.491s measured | — |
| 448 | G | backend | `backend/testing/test_reaper_sweeps_viewable_retire.py` | 273 | 8 | under 1500 LOC | 2.471s measured | — |
| 449 | G | backend | `backend/testing/test_strategic_memory_control.py` | 319 | 7 | under 1500 LOC | 2.444s measured | — |
| 450 | G | admin-ui | `admin-ui/src/components/ArchiveView.test.tsx` | 216 | 5 | under 1500 LOC | 2.432s measured | — |
| 451 | G | backend | `backend/testing/test_filter_migration_serialization.py` | 226 | 9 | under 1500 LOC | 2.413s measured | — |
| 452 | G | agent | `agent/src/sr-pr-section.test.ts` | 206 | 15 | under 1500 LOC | 2.412s measured | — |
| 453 | G | backend | `backend/testing/test_auth.py` | 1,098 | 52 | under 1500 LOC | 2.412s measured | — |
| 454 | G | backend | `backend/testing/test_runner_rate_limiter.py` | 456 | 14 | under 1500 LOC | 2.410s measured | — |
| 455 | G | admin-ui | `admin-ui/src/components/ImprovementLanesPanel.test.tsx` | 741 | 30 | under 1500 LOC | 2.398s measured | — |
| 456 | G | agent | `agent/src/prompts/done-helper.test.ts` | 972 | 25 | under 1500 LOC | 2.351s measured | — |
| 457 | G | backend | `backend/testing/test_reaper_sweeps.py` | 501 | 12 | under 1500 LOC | 2.343s measured | — |
| 458 | G | backend | `backend/testing/test_observability_payload_projection.py` | 514 | 8 | under 1500 LOC | 2.337s measured | — |
| 459 | G | backend | `backend/testing/test_strategic_memory_service.py` | 403 | 8 | under 1500 LOC | 2.295s measured | — |
| 460 | G | backend | `backend/testing/test_leader_loop_saturation_c3.py` | 400 | 8 | under 1500 LOC | 2.293s measured | — |
| 461 | G | backend | `backend/testing/test_release_activation_after_timeout.py` | 143 | 4 | under 1500 LOC | 2.225s measured | — |
| 462 | G | agent | `agent/src/mock-factory-export-coverage.test.ts` | 690 | 13 | under 1500 LOC | 2.196s measured | — |
| 463 | G | backend | `backend/testing/test_admin_strategic_memory.py` | 99 | 2 | under 1500 LOC | 2.190s measured | — |
| 464 | G | agent | `agent/src/guard-suppressions-pr-section.test.ts` | 200 | 11 | under 1500 LOC | 2.171s measured | — |
| 465 | G | admin-ui | `admin-ui/src/components/OuterReviewLoopConvergenceWidget.test.tsx` | 419 | 12 | under 1500 LOC | 2.150s measured | — |
| 466 | G | backend | `backend/testing/test_warm_claim_account_attribution.py` | 274 | 5 | under 1500 LOC | 2.138s measured | — |
| 467 | G | backend | `backend/testing/test_strategic_memory_source_schema.py` | 280 | 8 | under 1500 LOC | 2.115s measured | — |
| 468 | G | backend | `backend/testing/test_strategic_fast_live_work.py` | 567 | 5 | under 1500 LOC | 2.091s measured | protected; declared floor 5 |
| 469 | G | backend | `backend/testing/test_filter_rollup_conflicts.py` | 491 | 8 | under 1500 LOC | 2.087s measured | — |
| 470 | G | backend | `backend/testing/test_category_verdict_ledger.py` | 591 | 9 | under 1500 LOC | 2.085s measured | — |
| 471 | G | backend | `backend/testing/test_discovery_admission.py` | 356 | 7 | under 1500 LOC | 2.070s measured | — |
| 472 | G | admin-ui | `admin-ui/src/components/DirectionsPanel.test.tsx` | 456 | 17 | under 1500 LOC | 2.069s measured | — |
| 473 | G | admin-ui | `admin-ui/src/components/PipelineWorkspace.test.tsx` | 855 | 23 | under 1500 LOC | 2.059s measured | — |
| 474 | G | backend | `backend/testing/test_cost_summary.py` | 326 | 11 | under 1500 LOC | 2.053s measured | — |
| 475 | G | agent | `agent/src/handlers/audit-challenge.test.ts` | 702 | 32 | under 1500 LOC | 2.047s measured | — |
| 476 | G | backend | `backend/testing/test_auto_review.py` | 395 | 11 | under 1500 LOC | 2.020s measured | — |
| 477 | G | backend | `backend/testing/test_research_mem_eval.py` | 172 | 2 | under 1500 LOC | 1.993s measured | — |
| 478 | G | backend | `backend/testing/test_discovery_review__successor_direction.py` | 162 | 5 | under 1500 LOC | 1.964s measured | — |
| 479 | G | backend | `backend/testing/test_memo_exit_e2e.py` | 362 | 7 | under 1500 LOC | 1.950s measured | — |
| 480 | G | backend | `backend/testing/test_review_fix_lane_pilot_costs.py` | 1,499 | 20 | under 1500 LOC | 1.940s measured | — |
| 481 | G | backend | `backend/testing/test_experiment_authoring_context.py` | 455 | 15 | under 1500 LOC | 1.908s measured | — |
| 482 | G | admin-ui | `admin-ui/src/components/AutonomousStatus.test.tsx` | 401 | 22 | under 1500 LOC | 1.904s measured | — |
| 483 | G | backend | `backend/testing/test_terminal_reason_stamping.py` | 700 | 22 | under 1500 LOC | 1.898s measured | — |
| 484 | G | backend | `backend/testing/test_discovery_runtime_integration.py` | 227 | 7 | under 1500 LOC | 1.897s measured | — |
| 485 | G | backend | `backend/testing/test_playbook_capture_success.py` | 833 | 31 | under 1500 LOC | 1.883s measured | — |
| 486 | G | backend | `backend/testing/test_task_counts_endpoint.py` | 100 | 3 | under 1500 LOC | 1.858s measured | — |
| 487 | G | agent | `agent/src/providers/codex-rate-limit-reset.test.ts` | 474 | 21 | under 1500 LOC | 1.855s measured | — |
| 488 | G | backend | `backend/testing/test_operator_workbench_contracts.py` | 865 | 11 | under 1500 LOC | 1.836s measured | protected; declared floor 11 |
| 489 | G | admin-ui | `admin-ui/src/components/TaskCard.test.tsx` | 1,030 | 79 | under 1500 LOC | 1.829s measured | — |
| 490 | G | backend | `backend/testing/test_admin_bulk_cancellation_fencing.py` | 246 | 2 | under 1500 LOC | 1.827s measured | — |
| 491 | G | backend | `backend/testing/test_discovery_review_sequencing.py` | 271 | 4 | under 1500 LOC | 1.794s measured | — |
| 492 | G | backend | `backend/testing/test_strategic_ops_registry.py` | 246 | 6 | under 1500 LOC | 1.784s measured | — |
| 493 | G | backend | `backend/testing/test_migration_0682_workday_repair.py` | 600 | 6 | under 1500 LOC | 1.768s measured | — |
| 494 | G | backend | `backend/testing/test_filter_file_conflicts.py` | 1,153 | 42 | under 1500 LOC | 1.767s measured | — |
| 495 | G | admin-ui | `admin-ui/src/components/ProjectResourceEnvelopeSection.test.tsx` | 555 | 16 | under 1500 LOC | 1.725s measured | — |
| 496 | G | backend | `backend/testing/test_terminal_rollup_pr_sweep.py` | 219 | 5 | under 1500 LOC | 1.700s measured | — |
| 497 | G | backend | `backend/testing/test_digest_shared_cache.py` | 251 | 11 | under 1500 LOC | 1.699s measured | — |
| 498 | G | backend | `backend/testing/test_strategic_memory_governance.py` | 245 | 5 | under 1500 LOC | 1.697s measured | — |
| 499 | G | backend | `backend/testing/test_orchestrator_modularity.py` | 405 | 8 | under 1500 LOC | 1.685s measured | — |
| 500 | G | backend | `backend/testing/test_conversational_workday_protection.py` | 1,254 | 38 | under 1500 LOC | 1.672s measured | — |
| 501 | G | backend | `backend/testing/test_golden_suite.py` | 962 | 35 | under 1500 LOC | 1.658s measured | — |
| 502 | G | backend | `backend/testing/test_landed_signal.py` | 1,009 | 28 | under 1500 LOC | 1.653s measured | — |
| 503 | G | admin-ui | `admin-ui/src/components/StrategicContextArtifactSlot.test.tsx` | 981 | 34 | under 1500 LOC | 1.635s measured | — |
| 504 | G | backend | `backend/testing/test_admin_scope_measurement.py` | 180 | 7 | under 1500 LOC | 1.592s measured | — |
| 505 | G | backend | `backend/testing/test_runner_isolation_gpu_markers.py` | 146 | 2 | under 1500 LOC | 1.580s measured | — |
| 506 | G | backend | `backend/testing/test_agent_task_capabilities.py` | 88 | 4 | under 1500 LOC | 1.578s measured | — |
| 507 | G | backend | `backend/testing/test_compute_lane_arm_time_medians.py` | 287 | 7 | under 1500 LOC | 1.576s measured | — |
| 508 | G | admin-ui | `admin-ui/src/components/DirectionCard.test.tsx` | 907 | 72 | under 1500 LOC | 1.575s measured | — |
| 509 | G | admin-ui | `admin-ui/src/components/MessagesComposer.test.tsx` | 182 | 12 | under 1500 LOC | 1.574s measured | — |
| 510 | G | backend | `backend/testing/test_compute_terminal_reason_history.py` | 189 | 4 | under 1500 LOC | 1.557s measured | — |
| 511 | G | backend | `backend/testing/test_agent_branch_invariants.py` | 610 | 6 | under 1500 LOC | 1.550s measured | — |
| 512 | G | backend | `backend/testing/test_migration_exemptions_admin.py` | 293 | 13 | under 1500 LOC | 1.547s measured | — |
| 513 | G | admin-ui | `admin-ui/src/components/SettingsNotificationsSection.test.tsx` | 384 | 15 | under 1500 LOC | 1.546s measured | — |
| 514 | G | backend | `backend/testing/test_runner_enroll_docs_accuracy.py` | 599 | 19 | under 1500 LOC | 1.546s measured | — |
| 515 | G | backend | `backend/testing/test_strategic_memory_knowledge.py` | 245 | 4 | under 1500 LOC | 1.537s measured | — |
| 516 | G | backend | `backend/testing/test_issue_implementation.py` | 693 | 15 | under 1500 LOC | 1.493s measured | — |
| 517 | G | backend | `backend/testing/test_compute_zone_health.py` | 1,251 | 94 | under 1500 LOC | 1.492s measured | — |
| 518 | G | admin-ui | `admin-ui/src/components/TaskContextRail.test.tsx` | 469 | 18 | under 1500 LOC | 1.488s measured | — |
| 519 | G | backend | `backend/testing/test_admin_direction_events.py` | 179 | 5 | under 1500 LOC | 1.453s measured | — |
| 520 | G | agent | `agent/src/providers/call-resident-tool.test.ts` | 46 | 2 | under 1500 LOC | 1.447s measured | — |
| 521 | G | admin-ui | `admin-ui/src/components/ProviderModelBreakdownPanel.test.tsx` | 346 | 13 | under 1500 LOC | 1.432s measured | — |
| 522 | G | admin-ui | `admin-ui/src/components/PipelineView.test.tsx` | 409 | 21 | under 1500 LOC | 1.430s measured | — |
| 523 | G | backend | `backend/testing/test_sr_analytics_store.py` | 148 | 5 | under 1500 LOC | 1.410s measured | — |
| 524 | G | admin-ui | `admin-ui/src/hooks/useProjectResourceSummary.test.tsx` | 209 | 16 | under 1500 LOC | 1.380s measured | — |
| 525 | G | backend | `backend/testing/test_task_list_pagination.py` | 175 | 3 | under 1500 LOC | 1.340s measured | — |
| 526 | G | admin-ui | `admin-ui/src/components/AlertsBanner.test.tsx` | 194 | 12 | under 1500 LOC | 1.327s measured | — |
| 527 | G | backend | `backend/testing/test_lifecycle_crash_investigation.py` | 414 | 18 | under 1500 LOC | 1.326s measured | — |
| 528 | G | backend | `backend/testing/test_terminal_rollup_requeue.py` | 710 | 10 | under 1500 LOC | 1.300s measured | — |
| 529 | G | backend | `backend/testing/test_db_consistency_mediums.py` | 235 | 12 | under 1500 LOC | 1.296s measured | — |
| 530 | G | admin-ui | `admin-ui/src/components/ProjectToolHost.test.tsx` | 789 | 28 | under 1500 LOC | 1.289s measured | — |
| 531 | G | backend | `backend/testing/test_acceptance_gate__test_corroboration.py` | 1,058 | 26 | under 1500 LOC | 1.289s measured | — |
| 532 | G | backend | `backend/testing/test_acceptance_gate__rule_version.py` | 670 | 4 | under 1500 LOC | 1.288s measured | — |
| 533 | G | backend | `backend/testing/test_digest_precompute.py` | 848 | 25 | under 1500 LOC | 1.258s measured | — |
| 534 | G | admin-ui | `admin-ui/src/components/PRMonitorSection.test.tsx` | 411 | 24 | under 1500 LOC | 1.249s measured | — |
| 535 | G | backend | `backend/testing/test_vm_heartbeat_image.py` | 506 | 13 | under 1500 LOC | 1.240s measured | — |
| 536 | G | admin-ui | `admin-ui/src/components/DirectionPortfolioSection.test.tsx` | 645 | 20 | under 1500 LOC | 1.225s measured | — |
| 537 | G | admin-ui | `admin-ui/src/components/EffortEfficiencyPanel.test.tsx` | 507 | 24 | under 1500 LOC | 1.199s measured | — |
| 538 | G | admin-ui | `admin-ui/src/components/KeyboardShortcutsOverlay.test.tsx` | 60 | 3 | under 1500 LOC | 1.165s measured | — |
| 539 | G | backend | `backend/testing/test_warm_pool_cost_metrics.py` | 625 | 17 | under 1500 LOC | 1.152s measured | — |
| 540 | G | agent | `agent/src/providers/resident-reply-tool.test.ts` | 113 | 3 | under 1500 LOC | 1.148s measured | — |
| 541 | G | admin-ui | `admin-ui/src/components/EvergreenFillObservabilityPanel.test.tsx` | 193 | 17 | under 1500 LOC | 1.146s measured | — |
| 542 | G | backend | `backend/testing/test_admin_task_requeue_terminal.py` | 180 | 2 | under 1500 LOC | 1.143s measured | — |
| 543 | G | backend | `backend/testing/test_compute_experiment_draft.py` | 364 | 20 | under 1500 LOC | 1.141s measured | — |
| 544 | G | backend | `backend/testing/test_db.py` | 67 | 3 | under 1500 LOC | 1.132s measured | — |
| 545 | G | backend | `backend/testing/test_discovery_withheld_findings.py` | 396 | 7 | under 1500 LOC | 1.112s measured | — |
| 546 | G | backend | `backend/testing/test_no_ci_merge_gate_setup_retry.py` | 500 | 5 | under 1500 LOC | 1.110s measured | — |
| 547 | G | backend | `backend/testing/test_findings_index.py` | 416 | 11 | under 1500 LOC | 1.097s measured | — |
| 548 | G | admin-ui | `admin-ui/src/components/NotesTool.test.tsx` | 424 | 15 | under 1500 LOC | 1.087s measured | protected; declared floor 15 |
| 549 | G | backend | `backend/testing/test_release_qualifier_retained_scoring.py` | 125 | 1 | under 1500 LOC | 1.082s measured | — |
| 550 | G | agent | `agent/src/handlers/strategic-resident-cold-fast.stream.test.ts` | 340 | 8 | under 1500 LOC | 1.054s measured | — |
| 551 | G | backend | `backend/testing/test_security.py` | 716 | 68 | under 1500 LOC | 0.981s measured | — |
| 552 | G | backend | `backend/testing/test_optimistic_version.py` | 199 | 3 | under 1500 LOC | 0.904s measured | — |
| 553 | G | backend | `backend/testing/test_c5_approval_gate_invariant.py` | 309 | 3 | under 1500 LOC | 0.894s measured | — |
| 554 | G | backend | `backend/testing/test_blocked_recheck_autonomy.py` | 595 | 12 | under 1500 LOC | 0.878s measured | — |
| 555 | G | backend | `backend/testing/test_strategic_memory_claim_redaction_repair_retry.py` | 252 | 2 | under 1500 LOC | 0.866s measured | — |
| 556 | G | admin-ui | `admin-ui/src/components/DashboardMetricCards.test.tsx` | 208 | 24 | under 1500 LOC | 0.862s measured | — |
| 557 | G | backend | `backend/testing/test_task_direction_refs.py` | 50 | 3 | under 1500 LOC | 0.860s measured | — |
| 558 | G | backend | `backend/testing/test_migration_0696_project_sr_analytics.py` | 269 | 6 | under 1500 LOC | 0.856s measured | — |
| 559 | G | backend | `backend/testing/test_automatic_task_creation_producers.py` | 233 | 6 | under 1500 LOC | 0.840s measured | — |
| 560 | G | admin-ui | `admin-ui/src/components/SettingsDeploySection.test.tsx` | 233 | 13 | under 1500 LOC | 0.809s measured | — |
| 561 | G | backend | `backend/testing/test_strategic_cycle_cost_projection.py` | 1,143 | 46 | under 1500 LOC | 0.761s measured | — |
| 562 | G | admin-ui | `admin-ui/src/components/PrimaryTabNav.test.tsx` | 257 | 12 | under 1500 LOC | 0.759s measured | — |
| 563 | G | backend | `backend/testing/test_research_deferred_carry.py` | 62 | 1 | under 1500 LOC | 0.738s measured | — |
| 564 | G | admin-ui | `admin-ui/src/components/TaskDetailDrawerSections.test.tsx` | 1,016 | 81 | under 1500 LOC | 0.737s measured | — |
| 565 | G | admin-ui | `admin-ui/src/components/DirectionAutoPauseWidget.test.tsx` | 102 | 4 | under 1500 LOC | 0.737s measured | — |
| 566 | G | admin-ui | `admin-ui/src/hooks/useProjectConsultations.test.tsx` | 97 | 5 | under 1500 LOC | 0.723s measured | — |
| 567 | G | backend | `backend/testing/test_admin_cancel_impact.py` | 189 | 2 | under 1500 LOC | 0.701s measured | — |
| 568 | G | admin-ui | `admin-ui/eslint-rules/no-oversized-file.test.js` | 60 | 0 | under 1500 LOC | 0.693s measured | — |
| 569 | G | admin-ui | `admin-ui/src/components/StrategicContextWorkspace.test.tsx` | 775 | 20 | under 1500 LOC | 0.692s measured | protected; declared floor 20 |
| 570 | G | backend | `backend/testing/test_operator_attention_query_shape.py` | 524 | 11 | under 1500 LOC | 0.678s measured | — |
| 571 | G | backend | `backend/testing/test_direct_llm_client.py` | 613 | 28 | under 1500 LOC | 0.676s measured | — |
| 572 | G | backend | `backend/testing/test_migration_0720_direction_index_repair.py` | 255 | 8 | under 1500 LOC | 0.671s measured | — |
| 573 | G | backend | `backend/testing/test_task_completion_modularity.py` | 169 | 2 | under 1500 LOC | 0.670s measured | — |
| 574 | G | backend | `backend/testing/test_runner_decline_epoch.py` | 419 | 8 | under 1500 LOC | 0.641s measured | — |
| 575 | G | backend | `backend/testing/test_digest_cache.py` | 271 | 13 | under 1500 LOC | 0.635s measured | — |
| 576 | G | backend | `backend/testing/test_inline_llm.py` | 345 | 10 | under 1500 LOC | 0.626s measured | — |
| 577 | G | admin-ui | `admin-ui/eslint-rules/no-fixed-width-overflow.test.js` | 92 | 0 | under 1500 LOC | 0.605s measured | — |
| 578 | G | backend | `backend/testing/test_admin_notifications.py` | 85 | 2 | under 1500 LOC | 0.603s measured | — |
| 579 | G | admin-ui | `admin-ui/src/components/KanbanColumn.test.tsx` | 150 | 6 | under 1500 LOC | 0.591s measured | — |
| 580 | G | backend | `backend/testing/test_retry_budget.py` | 31 | 2 | under 1500 LOC | 0.586s measured | — |
| 581 | G | admin-ui | `admin-ui/src/components/LaneDiagPanel.test.tsx` | 524 | 23 | under 1500 LOC | 0.570s measured | — |
| 582 | G | admin-ui | `admin-ui/src/components/TaskComputeJobsSection.test.tsx` | 145 | 3 | under 1500 LOC | 0.557s measured | — |
| 583 | G | admin-ui | `admin-ui/src/components/KanbanBoard.test.tsx` | 244 | 12 | under 1500 LOC | 0.542s measured | — |
| 584 | G | admin-ui | `admin-ui/src/components/ReleaseQualificationFindings.test.tsx` | 179 | 8 | under 1500 LOC | 0.542s measured | — |
| 585 | G | backend | `backend/testing/test_orch_db_isolation.py` | 181 | 2 | under 1500 LOC | 0.536s measured | — |
| 586 | G | backend | `backend/testing/test_metrics_integrity.py` | 203 | 4 | under 1500 LOC | 0.535s measured | — |
| 587 | G | backend | `backend/testing/test_migration_0669_backfill_direction_fa_telemetry.py` | 271 | 9 | under 1500 LOC | 0.534s measured | — |
| 588 | G | backend | `backend/testing/test_migration_0698_cochange_state.py` | 217 | 2 | under 1500 LOC | 0.525s measured | — |
| 589 | G | backend | `backend/testing/test_rate_limit_verbatim_corpus.py` | 269 | 2 | under 1500 LOC | 0.520s measured | — |
| 590 | G | backend | `backend/testing/test_rollup_open_pr_reconciliation.py` | 94 | 1 | under 1500 LOC | 0.517s measured | — |
| 591 | G | backend | `backend/testing/test_branch_resolution_alerts.py` | 100 | 2 | under 1500 LOC | 0.509s measured | — |
| 592 | G | admin-ui | `admin-ui/src/components/WarmReuseVsCostTrend.test.tsx` | 218 | 9 | under 1500 LOC | 0.492s measured | — |
| 593 | G | admin-ui | `admin-ui/src/components/FixLoopConvergenceWidget.test.tsx` | 156 | 2 | under 1500 LOC | 0.491s measured | — |
| 594 | G | backend | `backend/testing/test_concurrency_overlap.py` | 303 | 7 | under 1500 LOC | 0.486s measured | — |
| 595 | G | backend | `backend/testing/test_llm_cost_attribution.py` | 981 | 19 | under 1500 LOC | 0.465s measured | — |
| 596 | G | admin-ui | `admin-ui/src/components/ScopeTrendsSection.test.tsx` | 234 | 17 | under 1500 LOC | 0.464s measured | — |
| 597 | G | admin-ui | `admin-ui/src/components/StrategicSessionControl.test.tsx` | 426 | 22 | under 1500 LOC | 0.459s measured | protected; declared floor 22 |
| 598 | G | admin-ui | `admin-ui/src/components/DriftSignalsCard.test.tsx` | 389 | 11 | under 1500 LOC | 0.450s measured | — |
| 599 | G | backend | `backend/testing/test_database_disconnect.py` | 511 | 27 | under 1500 LOC | 0.448s measured | — |
| 600 | G | backend | `backend/testing/test_migration_0718_strategic_memory_timezone_columns.py` | 84 | 1 | under 1500 LOC | 0.447s measured | — |
| 601 | G | backend | `backend/testing/test_work_session_actions.py` | 187 | 5 | under 1500 LOC | 0.440s measured | — |
| 602 | G | backend | `backend/testing/test_spend_cap_trustworthiness.py` | 355 | 15 | under 1500 LOC | 0.439s measured | — |
| 603 | G | backend | `backend/testing/test_lock_ordering.py` | 152 | 2 | under 1500 LOC | 0.436s measured | — |
| 604 | G | admin-ui | `admin-ui/src/components/HeaderBar.test.tsx` | 136 | 5 | under 1500 LOC | 0.434s measured | — |
| 605 | G | backend | `backend/testing/test_migration_0717_strategic_memory_semantic_ledger.py` | 217 | 3 | under 1500 LOC | 0.425s measured | — |
| 606 | G | backend | `backend/testing/test_strategic_memory_corrections.py` | 532 | 14 | under 1500 LOC | 0.416s measured | — |
| 607 | G | backend | `backend/testing/test_digest_latency.py` | 200 | 10 | under 1500 LOC | 0.415s measured | — |
| 608 | G | admin-ui | `admin-ui/src/components/MarkdownContentInner.test.tsx` | 115 | 12 | under 1500 LOC | 0.413s measured | — |
| 609 | G | admin-ui | `admin-ui/src/hooks/useDirectionsData.readOnly.test.ts` | 342 | 6 | under 1500 LOC | 0.401s measured | — |
| 610 | G | admin-ui | `admin-ui/src/components/TaskDurationWidget.test.tsx` | 64 | 7 | under 1500 LOC | 0.399s measured | — |
| 611 | G | admin-ui | `admin-ui/src/components/BulkActionBar.test.tsx` | 142 | 5 | under 1500 LOC | 0.396s measured | — |
| 612 | G | admin-ui | `admin-ui/src/components/RuntimeAccountsPanel.test.tsx` | 250 | 7 | under 1500 LOC | 0.381s measured | — |
| 613 | G | admin-ui | `admin-ui/src/utils/renderWhiteboardArtifact.test.ts` | 217 | 8 | under 1500 LOC | 0.380s measured | — |
| 614 | G | admin-ui | `admin-ui/src/components/MetricsDashboard.test.tsx` | 751 | 14 | under 1500 LOC | 0.378s measured | — |
| 615 | G | backend | `backend/testing/test_websocket_server_integration.py` | 104 | 1 | under 1500 LOC | 0.364s measured | — |
| 616 | G | backend | `backend/testing/test_task_capabilities.py` | 812 | 37 | under 1500 LOC | 0.363s measured | — |
| 617 | G | admin-ui | `admin-ui/src/hooks/useStrategicLiveness.test.tsx` | 102 | 6 | under 1500 LOC | 0.362s measured | protected; declared floor 6 |
| 618 | G | admin-ui | `admin-ui/src/components/MessagesSummaryCard.test.tsx` | 88 | 5 | under 1500 LOC | 0.360s measured | — |
| 619 | G | backend | `backend/testing/test_strategic_memory_sources.py` | 174 | 5 | under 1500 LOC | 0.354s measured | — |
| 620 | G | agent | `agent/src/prompts/review-prompt.test.ts` | 367 | 42 | under 1500 LOC | 0.349s measured | — |
| 621 | G | admin-ui | `admin-ui/src/components/Toasts.test.tsx` | 81 | 2 | under 1500 LOC | 0.348s measured | — |
| 622 | G | admin-ui | `admin-ui/src/components/SettingsSectionRouter.test.tsx` | 141 | 7 | under 1500 LOC | 0.347s measured | — |
| 623 | G | admin-ui | `admin-ui/src/components/MetricsIntegrityObservabilityPanel.test.tsx` | 128 | 3 | under 1500 LOC | 0.347s measured | — |
| 624 | G | backend | `backend/testing/test_frontend_static.py` | 155 | 8 | under 1500 LOC | 0.341s measured | — |
| 625 | G | admin-ui | `admin-ui/src/components/ComputeArtifactGallery.test.tsx` | 559 | 24 | under 1500 LOC | 0.341s measured | — |
| 626 | G | admin-ui | `admin-ui/src/components/SaveViewDialog.test.tsx` | 112 | 4 | under 1500 LOC | 0.335s measured | — |
| 627 | G | backend | `backend/testing/test_admin_digest_modularity.py` | 571 | 10 | under 1500 LOC | 0.335s measured | — |
| 628 | G | backend | `backend/testing/test_scripts_lane_hermetic_credentials.py` | 434 | 8 | under 1500 LOC | 0.315s measured | — |
| 629 | G | admin-ui | `admin-ui/src/components/FailureStageWidget.test.tsx` | 61 | 5 | under 1500 LOC | 0.313s measured | — |
| 630 | G | backend | `backend/testing/test_outer_loop_convergence.py` | 546 | 22 | under 1500 LOC | 0.311s measured | — |
| 631 | G | admin-ui | `admin-ui/src/components/MessageTypeDropdown.test.tsx` | 115 | 7 | under 1500 LOC | 0.309s measured | — |
| 632 | G | admin-ui | `admin-ui/src/components/SettingsAccountsSection.test.tsx` | 362 | 10 | under 1500 LOC | 0.303s measured | — |
| 633 | G | backend | `backend/testing/test_spawn_calibration.py` | 581 | 31 | under 1500 LOC | 0.302s measured | — |
| 634 | G | admin-ui | `admin-ui/src/components/MessageBubble.test.tsx` | 711 | 41 | under 1500 LOC | 0.296s measured | protected; declared floor 41 |
| 635 | G | backend | `backend/testing/test_migration_0657_0671_resident_session_id.py` | 228 | 4 | under 1500 LOC | 0.281s measured | — |
| 636 | G | backend | `backend/testing/test_migration_0679_backlog_clear.py` | 423 | 7 | under 1500 LOC | 0.278s measured | — |
| 637 | G | admin-ui | `admin-ui/src/components/TaskDetailDrawerSideboardContext.test.tsx` | 325 | 7 | under 1500 LOC | 0.276s measured | — |
| 638 | G | admin-ui | `admin-ui/src/components/WarmStartOriginRow.test.tsx` | 33 | 3 | under 1500 LOC | 0.276s measured | — |
| 639 | G | backend | `backend/testing/test_migration_0656_projectmessage_context_audience_index.py` | 194 | 2 | under 1500 LOC | 0.265s measured | — |
| 640 | G | admin-ui | `admin-ui/src/components/WhiteboardTool.test.tsx` | 687 | 14 | under 1500 LOC | 0.263s measured | protected; declared floor 14 |
| 641 | G | backend | `backend/testing/test_compute_gpu.py` | 341 | 20 | under 1500 LOC | 0.261s measured | — |
| 642 | G | backend | `backend/testing/test_strategic_payload.py` | 389 | 7 | under 1500 LOC | 0.260s measured | — |
| 643 | G | backend | `backend/testing/test_github_refs.py` | 395 | 14 | under 1500 LOC | 0.258s measured | — |
| 644 | G | agent | `agent/src/git-credential.test.ts` | 170 | 11 | under 1500 LOC | 0.248s measured | — |
| 645 | G | backend | `backend/testing/test_deploy_smoketest.py` | 119 | 6 | under 1500 LOC | 0.242s measured | — |
| 646 | G | backend | `backend/testing/test_playbook_archive.py` | 1,016 | 35 | under 1500 LOC | 0.242s measured | — |
| 647 | G | backend | `backend/testing/test_migration_0714_strategic_conversation_correctness.py` | 414 | 3 | under 1500 LOC | 0.238s measured | — |
| 648 | G | admin-ui | `admin-ui/src/components/VersionLifecyclePanelPrimitives.test.tsx` | 1,063 | 107 | under 1500 LOC | 0.237s measured | — |
| 649 | G | agent | `agent/src/handlers/verify-tests.test.ts` | 1,356 | 62 | under 1500 LOC | 0.234s measured | — |
| 650 | G | admin-ui | `admin-ui/src/components/ProjectBuildConfigSection.test.tsx` | 129 | 2 | under 1500 LOC | 0.229s measured | — |
| 651 | G | admin-ui | `admin-ui/src/components/NullBranchDiagnosticsPanel.test.tsx` | 147 | 10 | under 1500 LOC | 0.228s measured | — |
| 652 | G | admin-ui | `admin-ui/src/components/ProjectMembers.test.tsx` | 111 | 7 | under 1500 LOC | 0.226s measured | — |
| 653 | G | agent | `agent/src/providers/runners.test.ts` | 941 | 0 | under 1500 LOC | 0.225s measured | — |
| 654 | G | backend | `backend/testing/test_source_ref_validation.py` | 1,469 | 78 | under 1500 LOC | 0.225s measured | — |
| 655 | G | backend | `backend/testing/test_test_estate_ranked_queue.py` | 542 | 10 | under 1500 LOC | 0.224s measured | — |
| 656 | G | backend | `backend/testing/test_csrf.py` | 299 | 22 | under 1500 LOC | 0.218s measured | — |
| 657 | G | admin-ui | `admin-ui/src/hooks/useDirectionsData.test.ts` | 245 | 3 | under 1500 LOC | 0.216s measured | — |
| 658 | G | backend | `backend/testing/test_failure_classification_rate_limit.py` | 1,008 | 48 | under 1500 LOC | 0.214s measured | — |
| 659 | G | admin-ui | `admin-ui/src/components/RecurringPatternsSection.test.tsx` | 342 | 12 | under 1500 LOC | 0.212s measured | — |
| 660 | G | backend | `backend/testing/test_request_duration_telemetry.py` | 231 | 8 | under 1500 LOC | 0.204s measured | — |
| 661 | G | backend | `backend/testing/test_jwt_revocation.py` | 649 | 27 | under 1500 LOC | 0.203s measured | — |
| 662 | G | backend | `backend/testing/test_pause_clock.py` | 351 | 25 | under 1500 LOC | 0.202s measured | — |
| 663 | G | agent | `agent/src/prompts/self-review.test.ts` | 610 | 76 | under 1500 LOC | 0.201s measured | — |
| 664 | G | admin-ui | `admin-ui/src/components/SystemMaturityCard.test.tsx` | 106 | 13 | under 1500 LOC | 0.196s measured | — |
| 665 | G | admin-ui | `admin-ui/src/components/EvergreenBacklogPanel.test.tsx` | 140 | 12 | under 1500 LOC | 0.195s measured | — |
| 666 | G | admin-ui | `admin-ui/src/components/StrategicAuditPanel.test.tsx` | 247 | 13 | under 1500 LOC | 0.193s measured | — |
| 667 | G | backend | `backend/testing/test_stall_advisory.py` | 298 | 9 | under 1500 LOC | 0.190s measured | — |
| 668 | G | backend | `backend/testing/test_vitest_durations_freshness.py` | 742 | 29 | under 1500 LOC | 0.186s measured | — |
| 669 | G | backend | `backend/testing/test_filter_malformed_payload.py` | 159 | 8 | under 1500 LOC | 0.186s measured | — |
| 670 | G | admin-ui | `admin-ui/src/components/ProblemModelViewer.test.tsx` | 418 | 13 | under 1500 LOC | 0.180s measured | — |
| 671 | G | backend | `backend/testing/test_verification_toolchain.py` | 203 | 5 | under 1500 LOC | 0.179s measured | — |
| 672 | G | backend | `backend/testing/test_runbook_drift.py` | 103 | 5 | under 1500 LOC | 0.177s measured | — |
| 673 | G | backend | `backend/testing/test_migration_0716_strategic_memory_source_control_plane.py` | 112 | 2 | under 1500 LOC | 0.176s measured | — |
| 674 | G | admin-ui | `admin-ui/src/hooks/useConversation.test.tsx` | 1,218 | 36 | under 1500 LOC | 0.174s measured | protected; declared floor 36 |
| 675 | G | backend | `backend/testing/test_chain_continuity_funnel.py` | 135 | 5 | under 1500 LOC | 0.173s measured | — |
| 676 | G | admin-ui | `admin-ui/src/components/ConsultationProgressRow.test.tsx` | 156 | 5 | under 1500 LOC | 0.171s measured | — |
| 677 | G | backend | `backend/testing/test_migration_0713_hot_sweep_indexes.py` | 124 | 2 | under 1500 LOC | 0.171s measured | — |
| 678 | G | admin-ui | `admin-ui/src/components/ProjectManagementModal.test.tsx` | 227 | 16 | under 1500 LOC | 0.170s measured | — |
| 679 | G | admin-ui | `admin-ui/src/components/ProjectAlertDomainsSection.test.tsx` | 77 | 3 | under 1500 LOC | 0.168s measured | — |
| 680 | G | admin-ui | `admin-ui/src/api.test.ts` | 557 | 29 | under 1500 LOC | 0.166s measured | — |
| 681 | G | backend | `backend/testing/test_staging_base_refresh.py` | 377 | 16 | under 1500 LOC | 0.166s measured | — |
| 682 | G | backend | `backend/testing/test_env_preflight_evidence.py` | 284 | 17 | under 1500 LOC | 0.162s measured | — |
| 683 | G | admin-ui | `admin-ui/src/components/FleetHealthCard.test.tsx` | 325 | 16 | under 1500 LOC | 0.161s measured | — |
| 684 | G | admin-ui | `admin-ui/src/components/AttachmentPreviewModal.test.tsx` | 204 | 15 | under 1500 LOC | 0.161s measured | — |
| 685 | G | admin-ui | `admin-ui/src/components/catchupHelpers.test.ts` | 126 | 17 | under 1500 LOC | 0.161s measured | — |
| 686 | G | backend | `backend/testing/test_completion_topic_overlap.py` | 237 | 17 | under 1500 LOC | 0.160s measured | — |
| 687 | G | backend | `backend/testing/test_websocket_emitter_inventory.py` | 351 | 7 | under 1500 LOC | 0.160s measured | — |
| 688 | G | backend | `backend/testing/test_compute_image_remote_build.py` | 1,142 | 41 | under 1500 LOC | 0.156s measured | — |
| 689 | G | backend | `backend/testing/test_release_serving_reason_attribution.py` | 108 | 2 | under 1500 LOC | 0.155s measured | — |
| 690 | G | backend | `backend/testing/test_research_completion__source_refs.py` | 1,090 | 76 | under 1500 LOC | 0.155s measured | — |
| 691 | G | admin-ui | `admin-ui/src/components/RecentErrorsWidget.test.tsx` | 105 | 4 | under 1500 LOC | 0.155s measured | — |
| 692 | G | admin-ui | `admin-ui/src/components/OrchestratorHealthBar.test.tsx` | 486 | 24 | under 1500 LOC | 0.154s measured | — |
| 693 | G | admin-ui | `admin-ui/src/components/UILoadTimingPanel.test.tsx` | 338 | 27 | under 1500 LOC | 0.150s measured | — |
| 694 | G | admin-ui | `admin-ui/src/components/ConsoleHome.test.tsx` | 201 | 7 | under 1500 LOC | 0.146s measured | — |
| 695 | G | backend | `backend/testing/test_deepseek_cost_attribution.py` | 872 | 31 | under 1500 LOC | 0.145s measured | — |
| 696 | G | admin-ui | `admin-ui/src/firebase.test.ts` | 46 | 2 | under 1500 LOC | 0.145s measured | — |
| 697 | G | agent | `agent/src/node-preflight.test.ts` | 125 | 19 | under 1500 LOC | 0.141s measured | — |
| 698 | G | backend | `backend/testing/test_event_fanout.py` | 844 | 46 | under 1500 LOC | 0.139s measured | — |
| 699 | G | backend | `backend/testing/test_compute_artifact_iam.py` | 204 | 8 | under 1500 LOC | 0.136s measured | — |
| 700 | G | backend | `backend/testing/test_leader_stale_reaper.py` | 247 | 8 | under 1500 LOC | 0.134s measured | — |
| 701 | G | admin-ui | `admin-ui/src/components/ErrorBoundary.test.tsx` | 144 | 11 | under 1500 LOC | 0.132s measured | — |
| 702 | G | admin-ui | `admin-ui/src/components/ProjectWorkspace.readOnly.test.tsx` | 134 | 5 | under 1500 LOC | 0.131s measured | — |
| 703 | G | admin-ui | `admin-ui/src/components/ProviderCostPanel.test.tsx` | 320 | 10 | under 1500 LOC | 0.130s measured | — |
| 704 | G | admin-ui | `admin-ui/src/components/PoolAccountHealthPanel.test.tsx` | 348 | 15 | under 1500 LOC | 0.128s measured | — |
| 705 | G | admin-ui | `admin-ui/src/components/ProjectAutoReviewSection.test.tsx` | 50 | 2 | under 1500 LOC | 0.126s measured | — |
| 706 | G | admin-ui | `admin-ui/src/components/MuteWindowMenu.test.tsx` | 66 | 4 | under 1500 LOC | 0.125s measured | — |
| 707 | G | admin-ui | `admin-ui/src/hooks/useTasksState.test.ts` | 365 | 18 | under 1500 LOC | 0.124s measured | — |
| 708 | G | admin-ui | `admin-ui/src/components/MessageAttachments.test.tsx` | 75 | 3 | under 1500 LOC | 0.123s measured | — |
| 709 | G | admin-ui | `admin-ui/src/components/FleetCapacityCard.test.tsx` | 320 | 22 | under 1500 LOC | 0.121s measured | — |
| 710 | G | admin-ui | `admin-ui/eslint-rules/no-arbitrary-font-size.test.js` | 128 | 0 | under 1500 LOC | 0.120s measured | — |
| 711 | G | admin-ui | `admin-ui/src/uiLoadTiming.test.ts` | 66 | 4 | under 1500 LOC | 0.120s measured | — |
| 712 | G | backend | `backend/testing/test_issue_ledger.py` | 989 | 36 | under 1500 LOC | 0.118s measured | — |
| 713 | G | admin-ui | `admin-ui/src/components/CapabilityTrialsPanel.test.tsx` | 161 | 11 | under 1500 LOC | 0.117s measured | — |
| 714 | G | admin-ui | `admin-ui/src/utils/attachments.test.ts` | 82 | 5 | under 1500 LOC | 0.117s measured | — |
| 715 | G | backend | `backend/testing/test_agent_access_projections.py` | 294 | 5 | under 1500 LOC | 0.116s measured | — |
| 716 | G | backend | `backend/testing/test_hotfix_narrow_candidate.py` | 390 | 13 | under 1500 LOC | 0.115s measured | — |
| 717 | G | backend | `backend/testing/test_shared_state.py` | 610 | 29 | under 1500 LOC | 0.113s measured | — |
| 718 | G | admin-ui | `admin-ui/src/components/SettingsGeneralSection.test.tsx` | 105 | 8 | under 1500 LOC | 0.113s measured | — |
| 719 | G | backend | `backend/testing/test_discovery_evidence_bound.py` | 223 | 11 | under 1500 LOC | 0.110s measured | — |
| 720 | G | backend | `backend/testing/test_migration_0626_safe_encrypt_backfill.py` | 344 | 6 | under 1500 LOC | 0.110s measured | — |
| 721 | G | admin-ui | `admin-ui/src/components/DependencyHealthCard.test.tsx` | 196 | 9 | under 1500 LOC | 0.108s measured | — |
| 722 | G | backend | `backend/testing/test_filter_scope_empty.py` | 342 | 12 | under 1500 LOC | 0.106s measured | — |
| 723 | G | agent | `agent/src/handlers/verify-tests.gate-bound.test.ts` | 249 | 5 | under 1500 LOC | 0.106s measured | — |
| 724 | G | backend | `backend/testing/test_direction_referential_guard.py` | 472 | 33 | under 1500 LOC | 0.105s measured | — |
| 725 | G | backend | `backend/testing/test_process_role_lifecycle.py` | 347 | 25 | under 1500 LOC | 0.101s measured | — |
| 726 | G | backend | `backend/testing/test_anomaly_persistence.py` | 203 | 7 | under 1500 LOC | 0.100s measured | — |
| 727 | G | backend | `backend/testing/test_task_lifecycle_modularity.py` | 162 | 2 | under 1500 LOC | 0.098s measured | — |
| 728 | G | backend | `backend/testing/test_github_repo_snapshot.py` | 924 | 39 | under 1500 LOC | 0.096s measured | — |
| 729 | G | backend | `backend/testing/test_account_model_availability.py` | 829 | 32 | under 1500 LOC | 0.094s measured | — |
| 730 | G | backend | `backend/testing/test_backmerge_invariants.py` | 201 | 3 | under 1500 LOC | 0.093s measured | — |
| 731 | G | backend | `backend/testing/test_sweep_stale_state.py` | 202 | 11 | under 1500 LOC | 0.092s measured | — |
| 732 | G | agent | `agent/src/providers/resident-session.test.ts` | 1,067 | 48 | under 1500 LOC | 0.091s measured | protected; declared floor 48 |
| 733 | G | admin-ui | `admin-ui/eslint-rules/min-touch-target.test.js` | 105 | 0 | under 1500 LOC | 0.091s measured | — |
| 734 | G | admin-ui | `admin-ui/src/test/viewport-coverage.test.ts` | 263 | 23 | under 1500 LOC | 0.090s measured | — |
| 735 | G | admin-ui | `admin-ui/src/components/AccountMenu.test.tsx` | 116 | 6 | under 1500 LOC | 0.090s measured | — |
| 736 | G | admin-ui | `admin-ui/src/components/TaskStreamPanel.test.tsx` | 80 | 4 | under 1500 LOC | 0.090s measured | — |
| 737 | G | backend | `backend/testing/test_models.py` | 532 | 34 | under 1500 LOC | 0.089s measured | — |
| 738 | G | agent | `agent/src/handlers/artifact-consume.test.ts` | 436 | 19 | under 1500 LOC | 0.089s measured | — |
| 739 | G | backend | `backend/testing/test_migration_0715_strategic_context_envelopes.py` | 53 | 1 | under 1500 LOC | 0.087s measured | — |
| 740 | G | backend | `backend/testing/test_compute_secret_manifest.py` | 252 | 10 | under 1500 LOC | 0.086s measured | — |
| 741 | G | admin-ui | `admin-ui/src/hooks/__tests__/useLogViewer.test.ts` | 511 | 8 | under 1500 LOC | 0.086s measured | — |
| 742 | G | agent | `agent/src/handlers/live-conversation-resident.test.ts` | 257 | 13 | under 1500 LOC | 0.086s measured | — |
| 743 | G | backend | `backend/testing/test_evidence_primitive.py` | 543 | 52 | under 1500 LOC | 0.086s measured | — |
| 744 | G | admin-ui | `admin-ui/src/components/StrategicContextMap.test.tsx` | 140 | 5 | under 1500 LOC | 0.081s measured | — |
| 745 | G | admin-ui | `admin-ui/src/components/EventVisibilityMenu.test.tsx` | 87 | 5 | under 1500 LOC | 0.081s measured | — |
| 746 | G | agent | `agent/src/handlers/typed-fast-resident.test.ts` | 481 | 16 | under 1500 LOC | 0.080s measured | protected; declared floor 16 |
| 747 | G | admin-ui | `admin-ui/src/components/ScopeConversionRatesSection.test.tsx` | 377 | 26 | under 1500 LOC | 0.080s measured | — |
| 748 | G | backend | `backend/testing/test_deploy_identity.py` | 206 | 18 | under 1500 LOC | 0.079s measured | — |
| 749 | G | backend | `backend/testing/test_field_encryption.py` | 504 | 44 | under 1500 LOC | 0.078s measured | — |
| 750 | G | agent | `agent/src/token-file.test.ts` | 280 | 25 | under 1500 LOC | 0.078s measured | — |
| 751 | G | backend | `backend/testing/test_direction_health.py` | 431 | 28 | under 1500 LOC | 0.078s measured | — |
| 752 | G | backend | `backend/testing/test_admin_discovery_cycles.py` | 306 | 6 | under 1500 LOC | 0.077s measured | — |
| 753 | G | backend | `backend/testing/test_design_doc_grounding.py` | 771 | 30 | under 1500 LOC | 0.075s measured | — |
| 754 | G | agent | `agent/src/handlers/review-fix-migration.test.ts` | 272 | 20 | under 1500 LOC | 0.075s measured | — |
| 755 | G | admin-ui | `admin-ui/src/components/messageArming.test.ts` | 146 | 23 | under 1500 LOC | 0.075s measured | — |
| 756 | G | backend | `backend/testing/test_measure_repair_success.py` | 127 | 9 | under 1500 LOC | 0.075s measured | — |
| 757 | G | agent | `agent/src/handlers/code-change.test.ts` | 941 | 37 | under 1500 LOC | 0.073s measured | — |
| 758 | G | backend | `backend/testing/test_design_web_url_reachability.py` | 282 | 20 | under 1500 LOC | 0.073s measured | — |
| 759 | G | backend | `backend/testing/test_migration_env.py` | 197 | 13 | under 1500 LOC | 0.072s measured | — |
| 760 | G | backend | `backend/testing/test_direction_outcome_metric_manifest.py` | 155 | 5 | under 1500 LOC | 0.071s measured | — |
| 761 | G | backend | `backend/testing/test_namespace_backfill.py` | 230 | 13 | under 1500 LOC | 0.071s measured | — |
| 762 | G | admin-ui | `admin-ui/src/hooks/useWorkbenchShortcutRouter.test.tsx` | 194 | 6 | under 1500 LOC | 0.071s measured | — |
| 763 | G | admin-ui | `admin-ui/src/components/RailMiniCanvas.test.tsx` | 191 | 24 | under 1500 LOC | 0.071s measured | — |
| 764 | G | admin-ui | `admin-ui/src/components/SystemStatusCard.test.tsx` | 117 | 9 | under 1500 LOC | 0.071s measured | — |
| 765 | G | backend | `backend/testing/test_github_commit_resolution.py` | 150 | 8 | under 1500 LOC | 0.070s measured | — |
| 766 | G | backend | `backend/testing/test_migration_0621_safe_encrypt_backfill.py` | 259 | 4 | under 1500 LOC | 0.069s measured | — |
| 767 | G | backend | `backend/testing/test_operator_failure_messages.py` | 444 | 27 | under 1500 LOC | 0.069s measured | — |
| 768 | G | agent | `agent/src/moduleSize.contract.test.ts` | 156 | 4 | under 1500 LOC | 0.068s measured | — |
| 769 | G | backend | `backend/testing/test_build_config_detection.py` | 608 | 11 | under 1500 LOC | 0.067s measured | — |
| 770 | G | admin-ui | `admin-ui/eslint-rules/no-surface-colour.test.js` | 82 | 0 | under 1500 LOC | 0.066s measured | — |
| 771 | G | admin-ui | `admin-ui/eslint-rules/require-workbench-focus.test.js` | 75 | 0 | under 1500 LOC | 0.066s measured | — |
| 772 | G | backend | `backend/testing/test_knowledge_completion.py` | 355 | 26 | under 1500 LOC | 0.066s measured | — |
| 773 | G | backend | `backend/testing/test_migration_0609_reconcile_encrypted_columns.py` | 337 | 8 | under 1500 LOC | 0.065s measured | — |
| 774 | G | admin-ui | `admin-ui/src/components/MediumScopeWidget.test.tsx` | 320 | 28 | under 1500 LOC | 0.065s measured | — |
| 775 | G | backend | `backend/testing/test_materialize_manifest.py` | 710 | 25 | under 1500 LOC | 0.063s measured | — |
| 776 | G | admin-ui | `admin-ui/src/components/StrategicSessionEvents.test.tsx` | 120 | 4 | under 1500 LOC | 0.062s measured | protected; declared floor 4 |
| 777 | G | backend | `backend/testing/test_direction_event_trail.py` | 285 | 20 | under 1500 LOC | 0.062s measured | — |
| 778 | G | admin-ui | `admin-ui/src/components/LlmCostBreakdownPanel.test.tsx` | 136 | 6 | under 1500 LOC | 0.061s measured | — |
| 779 | G | backend | `backend/testing/test_broadcast.py` | 300 | 36 | under 1500 LOC | 0.060s measured | — |
| 780 | G | backend | `backend/testing/test_deployed_sha.py` | 781 | 31 | under 1500 LOC | 0.059s measured | — |
| 781 | G | backend | `backend/testing/test_digest_payload_metric_manifest.py` | 256 | 6 | under 1500 LOC | 0.058s measured | — |
| 782 | G | agent | `agent/src/handlers/audits-reopen.test.ts` | 390 | 15 | under 1500 LOC | 0.057s measured | — |
| 783 | G | admin-ui | `admin-ui/src/components/SavedViewsTabStrip.test.tsx` | 88 | 8 | under 1500 LOC | 0.056s measured | — |
| 784 | G | agent | `agent/src/handlers/discovery-review.test.ts` | 529 | 17 | under 1500 LOC | 0.055s measured | — |
| 785 | G | admin-ui | `admin-ui/src/hooks/useCommandPaletteShortcut.test.tsx` | 205 | 10 | under 1500 LOC | 0.054s measured | — |
| 786 | G | backend | `backend/testing/test_migration_0668_drop_projectcomputeimagebuild.py` | 218 | 3 | under 1500 LOC | 0.053s measured | — |
| 787 | G | backend | `backend/testing/test_llm_route_fallback.py` | 351 | 5 | under 1500 LOC | 0.052s measured | — |
| 788 | G | admin-ui | `admin-ui/src/components/EvidenceTierBadge.test.tsx` | 98 | 16 | under 1500 LOC | 0.051s measured | — |
| 789 | G | backend | `backend/testing/test_json_extract.py` | 258 | 30 | under 1500 LOC | 0.051s measured | — |
| 790 | G | backend | `backend/testing/test_capability_execution_fence.py` | 329 | 19 | under 1500 LOC | 0.050s measured | — |
| 791 | G | admin-ui | `admin-ui/src/hooks/useOperatorKeyboardShortcuts.test.tsx` | 355 | 17 | under 1500 LOC | 0.049s measured | — |
| 792 | G | admin-ui | `admin-ui/src/components/ConversationUtilityBar.test.tsx` | 891 | 27 | under 1500 LOC | 0.048s measured | protected; declared floor 27 |
| 793 | G | admin-ui | `admin-ui/src/components/SystemHealthCard.test.tsx` | 139 | 11 | under 1500 LOC | 0.048s measured | — |
| 794 | G | agent | `agent/src/utils/test-fix-loop.test.ts` | 1,376 | 56 | under 1500 LOC | 0.047s measured | — |
| 795 | G | admin-ui | `admin-ui/src/components/ReleaseEvidence.test.tsx` | 186 | 16 | under 1500 LOC | 0.047s measured | — |
| 796 | G | admin-ui | `admin-ui/src/hooks/useProjectMembers.test.tsx` | 92 | 3 | under 1500 LOC | 0.047s measured | — |
| 797 | G | admin-ui | `admin-ui/src/hooks/useStrategicReadModel.test.tsx` | 414 | 17 | under 1500 LOC | 0.046s measured | — |
| 798 | G | admin-ui | `admin-ui/eslint-rules/canonical-radius.test.js` | 66 | 0 | under 1500 LOC | 0.046s measured | — |
| 799 | G | admin-ui | `admin-ui/src/components/ui/Badge.test.tsx` | 110 | 18 | under 1500 LOC | 0.045s measured | — |
| 800 | G | admin-ui | `admin-ui/src/components/ProjectReadOnlyEditors.test.tsx` | 95 | 2 | under 1500 LOC | 0.045s measured | — |
| 801 | G | admin-ui | `admin-ui/src/components/WhiteboardRailSection.test.tsx` | 91 | 5 | under 1500 LOC | 0.045s measured | — |
| 802 | G | admin-ui | `admin-ui/src/components/ParkWakeHealthCard.test.tsx` | 61 | 6 | under 1500 LOC | 0.045s measured | — |
| 803 | G | backend | `backend/testing/test_dispatch_ordering.py` | 578 | 29 | under 1500 LOC | 0.045s measured | — |
| 804 | G | backend | `backend/testing/test_config_sync.py` | 486 | 22 | under 1500 LOC | 0.043s measured | — |
| 805 | G | backend | `backend/testing/test_admin_task_mutations_import.py` | 85 | 2 | under 1500 LOC | 0.043s measured | — |
| 806 | G | backend | `backend/testing/test_failure_reason.py` | 152 | 9 | under 1500 LOC | 0.042s measured | — |
| 807 | G | admin-ui | `admin-ui/src/components/DirectionsPanel.readOnly.test.tsx` | 124 | 1 | under 1500 LOC | 0.042s measured | — |
| 808 | G | admin-ui | `admin-ui/src/components/StrategicChallengeChip.test.tsx` | 71 | 7 | under 1500 LOC | 0.042s measured | — |
| 809 | G | backend | `backend/testing/test_context_bounds.py` | 302 | 21 | under 1500 LOC | 0.041s measured | — |
| 810 | G | backend | `backend/testing/test_direction_metrics.py` | 248 | 21 | under 1500 LOC | 0.040s measured | — |
| 811 | G | admin-ui | `admin-ui/src/components/ProjectResourceAvailabilityCard.test.tsx` | 75 | 3 | under 1500 LOC | 0.040s measured | — |
| 812 | G | admin-ui | `admin-ui/eslint-rules/no-disabled-react-hooks.test.js` | 74 | 1 | under 1500 LOC | 0.040s measured | — |
| 813 | G | admin-ui | `admin-ui/src/components/ToolResidentHandoffControls.test.tsx` | 48 | 3 | under 1500 LOC | 0.040s measured | protected; declared floor 3 |
| 814 | G | agent | `agent/src/call-speech/call-speech.test.ts` | 309 | 13 | under 1500 LOC | 0.039s measured | — |
| 815 | G | backend | `backend/testing/test_strategic_payload_size.py` | 547 | 6 | under 1500 LOC | 0.038s measured | — |
| 816 | G | backend | `backend/testing/test_release_operator_commit_gate.py` | 181 | 6 | under 1500 LOC | 0.037s measured | — |
| 817 | G | agent | `agent/src/prompt.context.test.ts` | 1,434 | 116 | under 1500 LOC | 0.037s measured | — |
| 818 | G | agent | `agent/src/handlers/index.test.ts` | 635 | 102 | under 1500 LOC | 0.037s measured | protected; declared floor 102 |
| 819 | G | admin-ui | `admin-ui/src/hooks/usePoller.test.ts` | 187 | 11 | under 1500 LOC | 0.037s measured | — |
| 820 | G | admin-ui | `admin-ui/src/components/CompletionDebtPanel.test.tsx` | 48 | 1 | under 1500 LOC | 0.037s measured | — |
| 821 | G | agent | `agent/src/handlers/prior-knowledge.test.ts` | 313 | 27 | under 1500 LOC | 0.036s measured | — |
| 822 | G | admin-ui | `admin-ui/src/hooks/useUrlFilterState.test.ts` | 149 | 14 | under 1500 LOC | 0.036s measured | — |
| 823 | G | agent | `agent/src/prompts/compute-progress-contract.test.ts` | 218 | 14 | under 1500 LOC | 0.035s measured | — |
| 824 | G | backend | `backend/testing/test_storage_signed_url.py` | 124 | 4 | under 1500 LOC | 0.035s measured | — |
| 825 | G | admin-ui | `admin-ui/src/components/MergeGateFleetHealthCard.test.tsx` | 77 | 7 | under 1500 LOC | 0.033s measured | — |
| 826 | G | backend | `backend/testing/test_build_config_lint.py` | 230 | 22 | under 1500 LOC | 0.033s measured | — |
| 827 | G | admin-ui | `admin-ui/src/components/ProjectToolIframe.test.tsx` | 95 | 2 | under 1500 LOC | 0.032s measured | — |
| 828 | G | backend | `backend/testing/test_release_ownership.py` | 195 | 4 | under 1500 LOC | 0.032s measured | — |
| 829 | G | backend | `backend/testing/test_cochange_decomposition.py` | 277 | 20 | under 1500 LOC | 0.031s measured | — |
| 830 | G | agent | `agent/src/utils/provisioning.test.ts` | 402 | 40 | under 1500 LOC | 0.031s measured | — |
| 831 | G | agent | `agent/src/utils/design-doc-presence.test.ts` | 330 | 25 | under 1500 LOC | 0.031s measured | — |
| 832 | G | admin-ui | `admin-ui/src/hooks/useSearchFilters.test.ts` | 197 | 22 | under 1500 LOC | 0.031s measured | — |
| 833 | G | backend | `backend/testing/test_cors_on_errors.py` | 141 | 6 | under 1500 LOC | 0.030s measured | — |
| 834 | G | admin-ui | `admin-ui/src/hooks/dashboardAccessGating.test.tsx` | 97 | 2 | under 1500 LOC | 0.030s measured | — |
| 835 | G | admin-ui | `admin-ui/src/components/PipelineEfficiencyPanel.test.tsx` | 81 | 4 | under 1500 LOC | 0.030s measured | — |
| 836 | G | backend | `backend/testing/test_strategic_op_contracts.py` | 179 | 9 | under 1500 LOC | 0.030s measured | — |
| 837 | G | agent | `agent/src/prompts/context-prefix.test.ts` | 1,205 | 69 | under 1500 LOC | 0.029s measured | — |
| 838 | G | admin-ui | `admin-ui/src/components/useMessageList.test.ts` | 223 | 14 | under 1500 LOC | 0.029s measured | — |
| 839 | G | admin-ui | `admin-ui/src/utils/taskCsv.test.ts` | 125 | 6 | under 1500 LOC | 0.029s measured | — |
| 840 | G | backend | `backend/testing/test_project_outcome_projection.py` | 277 | 7 | under 1500 LOC | 0.029s measured | — |
| 841 | G | backend | `backend/testing/test_merge_size_limits.py` | 166 | 13 | under 1500 LOC | 0.029s measured | — |
| 842 | G | backend | `backend/testing/test_direction_terminal_guard.py` | 80 | 5 | under 1500 LOC | 0.028s measured | — |
| 843 | G | admin-ui | `admin-ui/src/test/viewport-diff.test.ts` | 98 | 4 | under 1500 LOC | 0.028s measured | — |
| 844 | G | admin-ui | `admin-ui/src/hooks/usePersistedState.test.ts` | 87 | 9 | under 1500 LOC | 0.028s measured | — |
| 845 | G | agent | `agent/src/prompts/check-helper.fixture-deps.test.ts` | 77 | 4 | under 1500 LOC | 0.028s measured | — |
| 846 | G | backend | `backend/testing/test_compute_execution_documents.py` | 130 | 4 | under 1500 LOC | 0.027s measured | — |
| 847 | G | agent | `agent/src/handlers/design.test.ts` | 864 | 36 | under 1500 LOC | 0.027s measured | — |
| 848 | G | agent | `agent/src/handlers/walkthrough-e2e.test.ts` | 746 | 30 | under 1500 LOC | 0.027s measured | — |
| 849 | G | agent | `agent/src/media/livekit-call-media.test.ts` | 382 | 13 | under 1500 LOC | 0.027s measured | — |
| 850 | G | admin-ui | `admin-ui/src/components/SearchFilterBar.test.tsx` | 23 | 1 | under 1500 LOC | 0.027s measured | — |
| 851 | G | backend | `backend/testing/test_strategic_correctness_preflight.py` | 150 | 2 | under 1500 LOC | 0.027s measured | — |
| 852 | G | backend | `backend/testing/test_local_app_release.py` | 134 | 3 | under 1500 LOC | 0.026s measured | — |
| 853 | G | admin-ui | `admin-ui/src/components/AnomalyThrottleBanner.test.tsx` | 54 | 3 | under 1500 LOC | 0.026s measured | — |
| 854 | G | agent | `agent/src/reset.test.ts` | 722 | 32 | under 1500 LOC | 0.025s measured | — |
| 855 | G | agent | `agent/src/prompt-helper-refs.test.ts` | 326 | 5 | under 1500 LOC | 0.025s measured | — |
| 856 | G | admin-ui | `admin-ui/src/utils/strategicWorkSessionMetrics.test.ts` | 40 | 2 | under 1500 LOC | 0.025s measured | — |
| 857 | G | admin-ui | `admin-ui/src/hooks/useVersionLifecycle.test.tsx` | 33 | 1 | under 1500 LOC | 0.025s measured | — |
| 858 | G | backend | `backend/testing/test_notification.py` | 134 | 5 | under 1500 LOC | 0.025s measured | — |
| 859 | G | backend | `backend/testing/test_voice_credentials.py` | 178 | 7 | under 1500 LOC | 0.025s measured | — |
| 860 | G | backend | `backend/testing/test_c18_check_runs_cache_error_bypass.py` | 330 | 8 | under 1500 LOC | 0.025s measured | — |
| 861 | G | backend | `backend/testing/test_branch_resolution.py` | 196 | 15 | under 1500 LOC | 0.024s measured | — |
| 862 | G | backend | `backend/testing/test_strategic_workday_mutation_rebind.py` | 148 | 2 | under 1500 LOC | 0.023s measured | protected; declared floor 2 |
| 863 | G | backend | `backend/testing/test_livekit_tokens.py` | 143 | 5 | under 1500 LOC | 0.023s measured | — |
| 864 | G | backend | `backend/testing/test_github_checks.py` | 670 | 24 | under 1500 LOC | 0.023s measured | — |
| 865 | G | backend | `backend/testing/test_pr_is_not_behind_counter.py` | 224 | 13 | under 1500 LOC | 0.023s measured | — |
| 866 | G | agent | `agent/src/handlers/memo.test.ts` | 850 | 40 | under 1500 LOC | 0.023s measured | — |
| 867 | G | admin-ui | `admin-ui/src/__tests__/strategicReconnect.test.tsx` | 160 | 2 | under 1500 LOC | 0.023s measured | — |
| 868 | G | admin-ui | `admin-ui/src/hooks/useConversationSend.test.tsx` | 85 | 5 | under 1500 LOC | 0.023s measured | — |
| 869 | G | backend | `backend/testing/test_strategic_memory_invariants.py` | 182 | 7 | under 1500 LOC | 0.022s measured | — |
| 870 | G | backend | `backend/testing/test_migration_0273_normalise_indexes.py` | 158 | 5 | under 1500 LOC | 0.022s measured | — |
| 871 | G | agent | `agent/src/providers/resident-stream.test.ts` | 380 | 45 | under 1500 LOC | 0.022s measured | protected; declared floor 45 |
| 872 | G | admin-ui | `admin-ui/src/hooks/useStrategicContextNavigation.test.tsx` | 253 | 7 | under 1500 LOC | 0.022s measured | — |
| 873 | G | backend | `backend/testing/test_migration_0531_backfill_encryption.py` | 203 | 2 | under 1500 LOC | 0.022s measured | — |
| 874 | G | backend | `backend/testing/test_direction_mode.py` | 126 | 12 | under 1500 LOC | 0.021s measured | — |
| 875 | G | backend | `backend/testing/test_migration_0141_reconcile.py` | 121 | 3 | under 1500 LOC | 0.021s measured | — |
| 876 | G | backend | `backend/testing/test_migration_0719_validate_task_context_size_constraint.py` | 57 | 4 | under 1500 LOC | 0.021s measured | — |
| 877 | G | backend | `backend/testing/test_migration_path.py` | 72 | 10 | under 1500 LOC | 0.021s measured | — |
| 878 | G | agent | `agent/src/telemetry.test.ts` | 773 | 74 | under 1500 LOC | 0.021s measured | — |
| 879 | G | agent | `agent/src/handlers/web-research.test.ts` | 653 | 28 | under 1500 LOC | 0.021s measured | — |
| 880 | G | backend | `backend/testing/test_migration_reconciliation.py` | 111 | 7 | under 1500 LOC | 0.021s measured | — |
| 881 | G | agent | `agent/src/handlers/design-doc.test.ts` | 325 | 19 | under 1500 LOC | 0.020s measured | — |
| 882 | G | admin-ui | `admin-ui/src/hooks/useStrategistStatus.test.ts` | 120 | 10 | under 1500 LOC | 0.020s measured | — |
| 883 | G | agent | `agent/src/utils/classifier-fixtures.test.ts` | 110 | 6 | under 1500 LOC | 0.020s measured | — |
| 884 | G | admin-ui | `admin-ui/src/hooks/useProjectConfig.test.tsx` | 68 | 2 | under 1500 LOC | 0.020s measured | — |
| 885 | G | admin-ui | `admin-ui/src/strategicStateApi.test.ts` | 562 | 28 | under 1500 LOC | 0.019s measured | — |
| 886 | G | admin-ui | `admin-ui/src/utils/residentToolHandoff.test.ts` | 129 | 7 | under 1500 LOC | 0.019s measured | protected; declared floor 7 |
| 887 | G | admin-ui | `admin-ui/src/hooks/useViewPreferences.test.ts` | 40 | 5 | under 1500 LOC | 0.019s measured | — |
| 888 | G | backend | `backend/testing/test_release_prepromotion_rehearsal_contracts.py` | 99 | 5 | under 1500 LOC | 0.019s measured | — |
| 889 | G | backend | `backend/testing/test_account_model_regression_stats.py` | 148 | 12 | under 1500 LOC | 0.019s measured | — |
| 890 | G | agent | `agent/src/utils/agent-token.test.ts` | 312 | 21 | under 1500 LOC | 0.018s measured | — |
| 891 | G | agent | `agent/src/providers/foreground-resident.test.ts` | 175 | 9 | under 1500 LOC | 0.018s measured | — |
| 892 | G | agent | `agent/src/handlers/artifacts-helper.test.ts` | 151 | 4 | under 1500 LOC | 0.018s measured | — |
| 893 | G | backend | `backend/testing/test_migration_0532_message_backlog.py` | 190 | 3 | under 1500 LOC | 0.018s measured | — |
| 894 | G | backend | `backend/testing/test_message_audience_routing.py` | 165 | 10 | under 1500 LOC | 0.018s measured | — |
| 895 | G | backend | `backend/testing/test_ci_infra_rerun.py` | 104 | 10 | under 1500 LOC | 0.017s measured | — |
| 896 | G | agent | `agent/src/handlers/structured-output-repair.test.ts` | 732 | 32 | under 1500 LOC | 0.017s measured | — |
| 897 | G | admin-ui | `admin-ui/src/utils/strategicContextDescriptor.test.ts` | 343 | 10 | under 1500 LOC | 0.017s measured | — |
| 898 | G | admin-ui | `admin-ui/src/components/SrBadges.test.tsx` | 43 | 1 | under 1500 LOC | 0.017s measured | — |
| 899 | G | backend | `backend/testing/test_vm_lifecycle_runner_doomed_marker.py` | 89 | 3 | under 1500 LOC | 0.016s measured | — |
| 900 | G | backend | `backend/testing/test_classifier_fixtures.py` | 255 | 10 | under 1500 LOC | 0.016s measured | — |
| 901 | G | agent | `agent/src/handlers/capability-proof-trial.test.ts` | 404 | 27 | under 1500 LOC | 0.016s measured | — |
| 902 | G | backend | `backend/testing/test_research_actionability.py` | 204 | 5 | under 1500 LOC | 0.016s measured | — |
| 903 | G | backend | `backend/testing/test_migration_0151_reconcile.py` | 98 | 5 | under 1500 LOC | 0.015s measured | — |
| 904 | G | backend | `backend/testing/test_orchestrator_health_authority.py` | 32 | 3 | under 1500 LOC | 0.015s measured | — |
| 905 | G | agent | `agent/src/prompts/discovery.test.ts` | 668 | 45 | under 1500 LOC | 0.015s measured | — |
| 906 | G | agent | `agent/src/handlers/review-fix-derived-conflicts.test.ts` | 210 | 17 | under 1500 LOC | 0.015s measured | — |
| 907 | G | agent | `agent/src/json-extract.test.ts` | 176 | 29 | under 1500 LOC | 0.015s measured | — |
| 908 | G | admin-ui | `admin-ui/src/utils/directionHelpers.test.ts` | 140 | 17 | under 1500 LOC | 0.015s measured | — |
| 909 | G | backend | `backend/testing/test_leader_lock_keepalive.py` | 149 | 6 | under 1500 LOC | 0.015s measured | — |
| 910 | G | backend | `backend/testing/test_release_artifact_freshness.py` | 150 | 5 | under 1500 LOC | 0.014s measured | — |
| 911 | G | agent | `agent/src/handlers/criterion-named-test-trial.test.ts` | 376 | 17 | under 1500 LOC | 0.014s measured | — |
| 912 | G | admin-ui | `admin-ui/src/utils/mergeTestVerdict.test.ts` | 374 | 36 | under 1500 LOC | 0.014s measured | — |
| 913 | G | admin-ui | `admin-ui/src/components/ComputeJobsPanel.utils.test.ts` | 217 | 19 | under 1500 LOC | 0.014s measured | — |
| 914 | G | admin-ui | `admin-ui/src/utils/citationLinks.test.ts` | 77 | 13 | under 1500 LOC | 0.014s measured | — |
| 915 | G | backend | `backend/testing/test_execution_capability_requirements.py` | 126 | 4 | under 1500 LOC | 0.014s measured | — |
| 916 | G | backend | `backend/testing/test_release_activation.py` | 84 | 2 | under 1500 LOC | 0.014s measured | — |
| 917 | G | backend | `backend/testing/test_release_qualifier.py` | 124 | 5 | under 1500 LOC | 0.013s measured | — |
| 918 | G | backend | `backend/testing/test_event_type_invariants.py` | 320 | 7 | under 1500 LOC | 0.013s measured | — |
| 919 | G | admin-ui | `admin-ui/src/operatorWorkbenchContracts.test.ts` | 551 | 3 | under 1500 LOC | 0.013s measured | — |
| 920 | G | agent | `agent/src/handlers/walkthrough-criterion-gates.test.ts` | 463 | 40 | under 1500 LOC | 0.013s measured | — |
| 921 | G | agent | `agent/src/handlers/verify-tests.setup-retry.test.ts` | 175 | 5 | under 1500 LOC | 0.013s measured | — |
| 922 | G | backend | `backend/testing/test_migration_0472_no_double_encrypt.py` | 147 | 3 | under 1500 LOC | 0.012s measured | — |
| 923 | G | admin-ui | `admin-ui/src/utils/taskStatus.test.ts` | 102 | 24 | under 1500 LOC | 0.012s measured | — |
| 924 | G | agent | `agent/src/handlers/research-plan-prompt-blocks.test.ts` | 96 | 10 | under 1500 LOC | 0.012s measured | — |
| 925 | G | admin-ui | `admin-ui/src/utils/mathContent.test.ts` | 59 | 12 | under 1500 LOC | 0.012s measured | — |
| 926 | G | admin-ui | `admin-ui/src/utils/strategicSessionLive.test.ts` | 686 | 44 | under 1500 LOC | 0.011s measured | protected; declared floor 44 |
| 927 | G | agent | `agent/src/build-config.test.ts` | 546 | 46 | under 1500 LOC | 0.011s measured | — |
| 928 | G | agent | `agent/src/handlers/research-direction-context.test.ts` | 392 | 36 | under 1500 LOC | 0.011s measured | — |
| 929 | G | agent | `agent/src/handlers/strategic-ops-registry.test.ts` | 199 | 13 | under 1500 LOC | 0.011s measured | — |
| 930 | G | agent | `agent/src/providers/usage.test.ts` | 147 | 10 | under 1500 LOC | 0.011s measured | — |
| 931 | G | agent | `agent/src/processTracking.test.ts` | 144 | 4 | under 1500 LOC | 0.011s measured | — |
| 932 | G | admin-ui | `admin-ui/src/components/boardSavedViews.test.ts` | 131 | 11 | under 1500 LOC | 0.011s measured | — |
| 933 | G | backend | `backend/testing/test_compute_outcome_status.py` | 34 | 2 | under 1500 LOC | 0.010s measured | — |
| 934 | G | admin-ui | `admin-ui/src/utils/pendingWhiteboardArtifactRef.test.ts` | 222 | 6 | under 1500 LOC | 0.010s measured | protected; declared floor 6 |
| 935 | G | admin-ui | `admin-ui/src/accountManager.test.ts` | 191 | 9 | under 1500 LOC | 0.010s measured | — |
| 936 | G | agent | `agent/src/handlers/conversation-message-streamer.test.ts` | 183 | 6 | under 1500 LOC | 0.010s measured | — |
| 937 | G | admin-ui | `admin-ui/src/utils/parseTimestamp-guard.test.ts` | 155 | 15 | under 1500 LOC | 0.010s measured | — |
| 938 | G | backend | `backend/testing/test_compute_cloud_run.py` | 968 | 13 | under 1500 LOC | 0.009s measured | — |
| 939 | G | agent | `agent/src/handlers/compute-experiment.test.ts` | 253 | 12 | under 1500 LOC | 0.009s measured | — |
| 940 | G | admin-ui | `admin-ui/src/components/MessagesPanel.invariant.test.ts` | 212 | 9 | under 1500 LOC | 0.009s measured | — |
| 941 | G | agent | `agent/src/oauth-rotation.test.ts` | 205 | 10 | under 1500 LOC | 0.009s measured | — |
| 942 | G | admin-ui | `admin-ui/src/App.wiring.test.ts` | 124 | 14 | under 1500 LOC | 0.009s measured | protected; declared floor 14 |
| 943 | G | agent | `agent/src/taskCapabilities.test.ts` | 114 | 10 | under 1500 LOC | 0.009s measured | — |
| 944 | G | admin-ui | `admin-ui/src/utils/pendingAttachmentStore.test.ts` | 77 | 3 | under 1500 LOC | 0.009s measured | protected; declared floor 3 |
| 945 | G | admin-ui | `admin-ui/src/utils/strategicWorkbenchSession.test.ts` | 66 | 4 | under 1500 LOC | 0.009s measured | — |
| 946 | G | backend | `backend/testing/test_agent_memory_consolidation.py` | 43 | 2 | under 1500 LOC | 0.009s measured | — |
| 947 | G | backend | `backend/testing/test_migration_0457_encrypt_github_pat.py` | 82 | 2 | under 1500 LOC | 0.009s measured | — |
| 948 | G | backend | `backend/testing/test_github_oauth_origins.py` | 52 | 3 | under 1500 LOC | 0.009s measured | — |
| 949 | G | backend | `backend/testing/test_strategic_resident_activity.py` | 85 | 3 | under 1500 LOC | 0.008s measured | — |
| 950 | G | agent | `agent/src/prompts/project-context.test.ts` | 409 | 23 | under 1500 LOC | 0.008s measured | — |
| 951 | G | admin-ui | `admin-ui/src/components/eventPrefs.test.ts` | 320 | 40 | under 1500 LOC | 0.008s measured | — |
| 952 | G | agent | `agent/src/handlers/verify-tests.provision.test.ts` | 159 | 4 | under 1500 LOC | 0.008s measured | — |
| 953 | G | admin-ui | `admin-ui/src/utils/conversationDraftState.test.ts` | 111 | 6 | under 1500 LOC | 0.008s measured | — |
| 954 | G | agent | `agent/src/fix-loop-events.test.ts` | 258 | 20 | under 1500 LOC | 0.007s measured | — |
| 955 | G | admin-ui | `admin-ui/src/utils/outcomeKinds.test.ts` | 185 | 19 | under 1500 LOC | 0.007s measured | — |
| 956 | G | admin-ui | `admin-ui/src/utils/sidecarTasks.test.ts` | 173 | 15 | under 1500 LOC | 0.007s measured | — |
| 957 | G | agent | `agent/src/handlers/strategic-stall-advisory.test.ts` | 48 | 3 | under 1500 LOC | 0.007s measured | — |
| 958 | G | backend | `backend/testing/test_migration_0639_align_projectcomputeimagebuild_timestamptz.py` | 105 | 1 | under 1500 LOC | 0.007s measured | — |
| 959 | G | backend | `backend/testing/test_shared_state_facade.py` | 191 | 4 | under 1500 LOC | 0.007s measured | — |
| 960 | G | backend | `backend/testing/test_strategic_stall_visibility.py` | 71 | 2 | under 1500 LOC | 0.006s measured | — |
| 961 | G | backend | `backend/testing/test_branch_cleanup.py` | 50 | 3 | under 1500 LOC | 0.006s measured | — |
| 962 | G | agent | `agent/src/handlers/direction-renderer.test.ts` | 252 | 20 | under 1500 LOC | 0.006s measured | — |
| 963 | G | agent | `agent/src/handlers/strategic-context-envelope.test.ts` | 224 | 7 | under 1500 LOC | 0.006s measured | — |
| 964 | G | agent | `agent/src/utils/viewport-shot-download.test.ts` | 140 | 11 | under 1500 LOC | 0.006s measured | — |
| 965 | G | admin-ui | `admin-ui/src/taskCapabilities.test.ts` | 136 | 11 | under 1500 LOC | 0.006s measured | — |
| 966 | G | admin-ui | `admin-ui/src/utils/statusFilter.test.ts` | 49 | 7 | under 1500 LOC | 0.006s measured | — |
| 967 | G | backend | `backend/testing/test_compute_provision_slots.py` | 49 | 2 | under 1500 LOC | 0.006s measured | — |
| 968 | G | agent | `agent/src/handlers/review-fix-history.test.ts` | 184 | 21 | under 1500 LOC | 0.005s measured | — |
| 969 | G | admin-ui | `admin-ui/src/test/composition-root-wiring.test.ts` | 168 | 5 | under 1500 LOC | 0.005s measured | — |
| 970 | G | admin-ui | `admin-ui/src/api/projectTools.residentArtifact.test.ts` | 135 | 5 | under 1500 LOC | 0.005s measured | protected; declared floor 5 |
| 971 | G | admin-ui | `admin-ui/src/test/orphan-component-guard.test.ts` | 129 | 2 | under 1500 LOC | 0.005s measured | — |
| 972 | G | agent | `agent/src/handlers/verify-tests.setup-probe.test.ts` | 115 | 2 | under 1500 LOC | 0.005s measured | — |
| 973 | G | admin-ui | `admin-ui/src/utils/unverifiedReasonLabels.test.ts` | 100 | 13 | under 1500 LOC | 0.005s measured | — |
| 974 | G | admin-ui | `admin-ui/src/utils/newProjectOAuthResume.test.ts` | 84 | 5 | under 1500 LOC | 0.005s measured | — |
| 975 | G | admin-ui | `admin-ui/src/utils/verdictFormatting.test.ts` | 63 | 9 | under 1500 LOC | 0.005s measured | — |
| 976 | G | admin-ui | `admin-ui/src/utils/externalUrl.test.ts` | 57 | 12 | under 1500 LOC | 0.005s measured | — |
| 977 | G | agent | `agent/src/handlers/discovery-review-workspace.test.ts` | 44 | 2 | under 1500 LOC | 0.005s measured | — |
| 978 | G | backend | `backend/testing/test_admin_auth_invariant.py` | 100 | 1 | under 1500 LOC | 0.005s measured | — |
| 979 | G | agent | `agent/src/handlers/research-plan-contract.test.ts` | 198 | 11 | under 1500 LOC | 0.004s measured | — |
| 980 | G | agent | `agent/src/reset-invariant.test.ts` | 152 | 9 | under 1500 LOC | 0.004s measured | — |
| 981 | G | agent | `agent/src/heartbeat-classify.test.ts` | 104 | 13 | under 1500 LOC | 0.004s measured | — |
| 982 | G | admin-ui | `admin-ui/src/App.test.tsx` | 83 | 4 | under 1500 LOC | 0.004s measured | — |
| 983 | G | admin-ui | `admin-ui/src/types/workbenchEvents.test.ts` | 77 | 4 | under 1500 LOC | 0.004s measured | — |
| 984 | G | agent | `agent/src/telemetry.prompt_size.test.ts` | 77 | 3 | under 1500 LOC | 0.004s measured | — |
| 985 | G | admin-ui | `admin-ui/src/components/FleetRunnersPanel.utils.test.ts` | 58 | 5 | under 1500 LOC | 0.004s measured | — |
| 986 | G | admin-ui | `admin-ui/src/utils/idempotencyKey.test.ts` | 56 | 5 | under 1500 LOC | 0.004s measured | — |
| 987 | G | admin-ui | `admin-ui/src/utils/fixLoopLayers.test.ts` | 40 | 6 | under 1500 LOC | 0.004s measured | — |
| 988 | G | agent | `agent/src/handlers/strategic-resident-prompt.test.ts` | 39 | 3 | under 1500 LOC | 0.004s measured | protected; declared floor 3 |
| 989 | G | admin-ui | `admin-ui/src/test/workbench-theme-tokens.test.ts` | 32 | 1 | under 1500 LOC | 0.004s measured | — |
| 990 | G | admin-ui | `admin-ui/src/utils/time.test.ts` | 30 | 5 | under 1500 LOC | 0.004s measured | — |
| 991 | G | admin-ui | `admin-ui/src/appWorkbenchConstants.test.ts` | 27 | 3 | under 1500 LOC | 0.004s measured | — |
| 992 | G | admin-ui | `admin-ui/src/utils/projectSelection.test.ts` | 27 | 2 | under 1500 LOC | 0.004s measured | — |
| 993 | G | backend | `backend/testing/test_claim_analytics.py` | 40 | 2 | under 1500 LOC | 0.004s measured | — |
| 994 | G | backend | `backend/testing/test_docs_runbook_drift.py` | 274 | 8 | under 1500 LOC | 0.004s measured | — |
| 995 | G | agent | `agent/src/handlers/strategic-resident-wake.test.ts` | 173 | 9 | under 1500 LOC | 0.003s measured | — |
| 996 | G | agent | `agent/src/workspaceDisk.test.ts` | 111 | 6 | under 1500 LOC | 0.003s measured | — |
| 997 | G | admin-ui | `admin-ui/src/utils/computeAnalysisLabels.test.ts` | 96 | 12 | under 1500 LOC | 0.003s measured | — |
| 998 | G | admin-ui | `admin-ui/src/utils/taskGraph.test.ts` | 94 | 11 | under 1500 LOC | 0.003s measured | — |
| 999 | G | admin-ui | `admin-ui/src/accessControl.test.ts` | 91 | 4 | under 1500 LOC | 0.003s measured | — |
| 1000 | G | agent | `agent/src/handlers/structured-repair.test.ts` | 88 | 3 | under 1500 LOC | 0.003s measured | — |
| 1001 | G | admin-ui | `admin-ui/src/utils/mergeGateSummary.test.ts` | 82 | 7 | under 1500 LOC | 0.003s measured | — |
| 1002 | G | admin-ui | `admin-ui/src/utils/imageEdits.test.ts` | 72 | 10 | under 1500 LOC | 0.003s measured | — |
| 1003 | G | admin-ui | `admin-ui/src/types/workbenchTargets.test.ts` | 71 | 4 | under 1500 LOC | 0.003s measured | — |
| 1004 | G | admin-ui | `admin-ui/src/components/projectToolHidden.test.ts` | 47 | 6 | under 1500 LOC | 0.003s measured | — |
| 1005 | G | admin-ui | `admin-ui/src/utils/conversationAttachmentDraftFinalization.test.ts` | 42 | 1 | under 1500 LOC | 0.003s measured | — |
| 1006 | G | admin-ui | `admin-ui/src/utils/conversationMessageIdentity.test.ts` | 24 | 2 | under 1500 LOC | 0.003s measured | — |
| 1007 | G | backend | `backend/testing/test_failure_classification_retained.py` | 11 | 1 | under 1500 LOC | 0.003s measured | — |
| 1008 | G | backend | `backend/testing/test_local_runner_terminal_log.py` | 151 | 1 | under 1500 LOC | 0.002s measured | — |
| 1009 | G | agent | `agent/src/branch-literal-regression.test.ts` | 122 | 3 | under 1500 LOC | 0.002s measured | — |
| 1010 | G | admin-ui | `admin-ui/src/qualificationStateVocab.contract.test.ts` | 77 | 4 | under 1500 LOC | 0.002s measured | — |
| 1011 | G | admin-ui | `admin-ui/src/test/viewport-capture-config.test.ts` | 57 | 5 | under 1500 LOC | 0.002s measured | — |
| 1012 | G | admin-ui | `admin-ui/src/hooks/useUIState.test.ts` | 41 | 3 | under 1500 LOC | 0.002s measured | — |
| 1013 | G | agent | `agent/src/qualificationStateVocab.contract.test.ts` | 41 | 2 | under 1500 LOC | 0.002s measured | — |
| 1014 | G | admin-ui | `admin-ui/src/utils/strategicAttention.test.ts` | 39 | 3 | under 1500 LOC | 0.002s measured | — |
| 1015 | G | admin-ui | `admin-ui/src/utils/convergenceVerdict.test.ts` | 35 | 4 | under 1500 LOC | 0.002s measured | — |
| 1016 | G | admin-ui | `admin-ui/src/utils/conversationSendState.test.ts` | 30 | 1 | under 1500 LOC | 0.002s measured | — |
| 1017 | G | agent | `agent/src/utils/sanitize.test.ts` | 17 | 3 | under 1500 LOC | 0.002s measured | — |
| 1018 | G | admin-ui | `admin-ui/src/useAppState.selection.test.ts` | 15 | 2 | under 1500 LOC | 0.002s measured | — |
| 1019 | G | admin-ui | `admin-ui/src/branch-literal-regression.test.ts` | 93 | 3 | under 1500 LOC | 0.001s measured | — |
| 1020 | G | admin-ui | `admin-ui/src/utils/failureClass.test.ts` | 43 | 4 | under 1500 LOC | 0.001s measured | — |
| 1021 | G | agent | `agent/src/_agent_scripts/materialize_manifest.parity.test.ts` | 34 | 1 | under 1500 LOC | 0.001s measured | — |
| 1022 | G | agent | `agent/src/_agent_scripts/fixgate_attribution.parity.test.ts` | 31 | 1 | under 1500 LOC | 0.001s measured | — |
| 1023 | G | admin-ui | `admin-ui/src/utils/composerRequestOwnership.test.ts` | 28 | 2 | under 1500 LOC | 0.001s measured | — |
| 1024 | G | admin-ui | `admin-ui/src/test/environment.test.ts` | 15 | 1 | under 1500 LOC | 0.001s measured | — |
| 1025 | G | admin-ui | `admin-ui/src/components/AdversarialQualityPanel.test.tsx` | 9 | 1 | under 1500 LOC | 0.001s measured | — |
| 1026 | G | backend | `backend/testing/test_runner_heartbeat.py` | 1,411 | 34 | under 1500 LOC | 0.000s measured | — |
| 1027 | G | agent | `agent/src/handlers/verify-tests.integration.test.ts` | 134 | 1 | under 1500 LOC | 0.000s measured | — |
| 1028 | G | agent | `agent/src/prompts/diagnostics-after-edit-hook.test.ts` | 77 | 0 | under 1500 LOC | 0.000s measured | — |
| 1029 | G | admin-ui-viewport | `admin-ui/src/components/VersionSummaryPanel.viewport.test.tsx` | 1,049 | 43 | under 1500 LOC | unknown (no committed measurement) | — |
| 1030 | G | admin-ui-viewport | `admin-ui/src/components/SrUnhandledEdgesRetrospectivesPanel.viewport.test.tsx` | 927 | 21 | under 1500 LOC | unknown (no committed measurement) | — |
| 1031 | G | admin-ui-viewport | `admin-ui/src/components/ComputeJobsPanel.viewport.test.tsx` | 875 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 1032 | G | admin-ui-viewport | `admin-ui/src/components/MessagesPanel.viewport.test.tsx` | 786 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 1033 | G | admin-ui-viewport | `admin-ui/src/components/VersionLifecyclePanel.viewport.test.tsx` | 763 | 22 | under 1500 LOC | unknown (no committed measurement) | — |
| 1034 | G | admin-ui-viewport | `admin-ui/src/components/ProblemModelViewer.viewport.test.tsx` | 728 | 19 | under 1500 LOC | unknown (no committed measurement) | — |
| 1035 | G | admin-ui-viewport | `admin-ui/src/components/DashboardHome.viewport.test.tsx` | 718 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 1036 | G | admin-ui-viewport | `admin-ui/src/components/ProjectToolHost.viewport.test.tsx` | 689 | 20 | under 1500 LOC | unknown (no committed measurement) | — |
| 1037 | G | admin-ui-viewport | `admin-ui/src/components/NewTaskModal.viewport.test.tsx` | 688 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 1038 | G | admin-ui-viewport | `admin-ui/src/components/MessagesListView.viewport.test.tsx` | 648 | 33 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 33 |
| 1039 | G | backend | `backend/testing/test_lifecycle_integration.py` | 634 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1040 | G | admin-ui-viewport | `admin-ui/src/components/WorkbenchComposition.viewport.test.tsx` | 590 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1041 | G | admin-ui-viewport | `admin-ui/src/components/StrategicContextRail.viewport.test.tsx` | 570 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 1042 | G | admin-ui-viewport | `admin-ui/src/components/SettingsDeploySection.viewport.test.tsx` | 563 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| 1043 | G | admin-ui-viewport | `admin-ui/src/components/DirectionCard.viewport.test.tsx` | 503 | 18 | under 1500 LOC | unknown (no committed measurement) | — |
| 1044 | G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerMeta.viewport.test.tsx` | 483 | 14 | under 1500 LOC | unknown (no committed measurement) | — |
| 1045 | G | admin-ui-viewport | `admin-ui/src/components/HeaderBar.viewport.test.tsx` | 479 | 21 | under 1500 LOC | unknown (no committed measurement) | — |
| 1046 | G | admin-ui-viewport | `admin-ui/src/components/KnowledgeBrowser.viewport.test.tsx` | 443 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| 1047 | G | admin-ui-viewport | `admin-ui/src/components/ActivityFeed.viewport.test.tsx` | 422 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 1048 | G | admin-ui-viewport | `admin-ui/src/components/MessagesComposer.viewport.test.tsx` | 386 | 16 | under 1500 LOC | unknown (no committed measurement) | — |
| 1049 | G | admin-ui-viewport | `admin-ui/src/components/PipelineWorkspace.viewport.test.tsx` | 348 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 1050 | G | admin-ui-viewport | `admin-ui/src/components/WarmVmPanel.viewport.test.tsx` | 342 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1051 | G | admin-ui-viewport | `admin-ui/src/components/NewProjectModal.viewport.test.tsx` | 325 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 1052 | G | admin-ui-viewport | `admin-ui/src/components/SrTrendBreakdown.viewport.test.tsx` | 323 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 1053 | G | admin-ui-viewport | `admin-ui/src/components/LlmCostBreakdownPanel.viewport.test.tsx` | 305 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1054 | G | admin-ui-viewport | `admin-ui/src/components/StrategicContextArtifactSlot.viewport.test.tsx` | 305 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 1055 | G | admin-ui-viewport | `admin-ui/src/components/ProjectBuildConfigSection.viewport.test.tsx` | 297 | 12 | under 1500 LOC | unknown (no committed measurement) | — |
| 1056 | G | admin-ui-viewport | `admin-ui/src/components/SettingsSectionRouter.viewport.test.tsx` | 294 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1057 | G | admin-ui-viewport | `admin-ui/src/components/MetricsDashboard.viewport.test.tsx` | 291 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1058 | G | admin-ui-viewport | `admin-ui/src/components/DirectionsPanel.viewport.test.tsx` | 290 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 1059 | G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawer.viewport.test.tsx` | 285 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 1060 | G | admin-ui-viewport | `admin-ui/src/components/RunningUpdatePanel.viewport.test.tsx` | 277 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 1061 | G | admin-ui-viewport | `admin-ui/src/components/MessageBubble.viewport.test.tsx` | 272 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 1062 | G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerFailureDetail.viewport.test.tsx` | 272 | 11 | under 1500 LOC | unknown (no committed measurement) | — |
| 1063 | G | admin-ui-viewport | `admin-ui/src/components/StrategicContextWorkspace.viewport.test.tsx` | 254 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1064 | G | admin-ui-viewport | `admin-ui/src/components/NotesTool.viewport.test.tsx` | 253 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 1065 | G | admin-ui-viewport | `admin-ui/src/components/RemediationFunnelSection.viewport.test.tsx` | 246 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1066 | G | admin-ui-viewport | `admin-ui/src/components/FleetRunnersPanel.viewport.test.tsx` | 243 | 9 | under 1500 LOC | unknown (no committed measurement) | — |
| 1067 | G | admin-ui-viewport | `admin-ui/src/components/WhiteboardTool.viewport.test.tsx` | 237 | 8 | under 1500 LOC | unknown (no committed measurement) | — |
| 1068 | G | admin-ui-viewport | `admin-ui/src/components/AutonomousStatus.viewport.test.tsx` | 234 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1069 | G | admin-ui-viewport | `admin-ui/src/components/ResearchBrowser.viewport.test.tsx` | 234 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1070 | G | admin-ui-viewport | `admin-ui/src/components/ViewportDebtPanel.viewport.test.tsx` | 223 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1071 | G | admin-ui-viewport | `admin-ui/src/components/LaneDiagPanel.viewport.test.tsx` | 222 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1072 | G | admin-ui-viewport | `admin-ui/src/components/PRMonitorSection.viewport.test.tsx` | 219 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1073 | G | admin-ui-viewport | `admin-ui/src/components/TaskCard.viewport.test.tsx` | 214 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 1074 | G | admin-ui-viewport | `admin-ui/src/components/OrchestratorHealthBar.viewport.test.tsx` | 212 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1075 | G | admin-ui-viewport | `admin-ui/src/components/KanbanBoard.viewport.test.tsx` | 211 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1076 | G | admin-ui-viewport | `admin-ui/src/components/SettingsNotificationsSection.viewport.test.tsx` | 210 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 1077 | G | admin-ui-viewport | `admin-ui/src/components/ConversationUtilityBar.viewport.test.tsx` | 202 | 8 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 8 |
| 1078 | G | admin-ui-viewport | `admin-ui/src/components/PipelineView.viewport.test.tsx` | 197 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1079 | G | admin-ui-viewport | `admin-ui/src/components/ProviderModelBreakdownPanel.viewport.test.tsx` | 192 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1080 | G | admin-ui-viewport | `admin-ui/src/components/SearchFilterBar.viewport.test.tsx` | 192 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 1081 | G | admin-ui-viewport | `admin-ui/src/components/BulkActionBar.viewport.test.tsx` | 191 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1082 | G | admin-ui-viewport | `admin-ui/src/components/KanbanColumn.viewport.test.tsx` | 183 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1083 | G | admin-ui-viewport | `admin-ui/src/components/ProblemModelEditor.viewport.test.tsx` | 178 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1084 | G | admin-ui-viewport | `admin-ui/src/components/ConfirmDialog.viewport.test.tsx` | 175 | 7 | under 1500 LOC | unknown (no committed measurement) | — |
| 1085 | G | admin-ui-viewport | `admin-ui/src/components/EffortEfficiencyPanel.viewport.test.tsx` | 173 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1086 | G | admin-ui-viewport | `admin-ui/src/components/MergeVerificationBanner.viewport.test.tsx` | 167 | 10 | under 1500 LOC | unknown (no committed measurement) | — |
| 1087 | G | admin-ui-viewport | `admin-ui/src/components/TaskComputeJobsSection.viewport.test.tsx` | 165 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1088 | G | admin-ui-viewport | `admin-ui/src/components/SettingsAccountsSection.viewport.test.tsx` | 163 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1089 | G | admin-ui-viewport | `admin-ui/src/test/kanban-composed.viewport.test.tsx` | 158 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1090 | G | admin-ui-viewport | `admin-ui/src/components/VersionMismatchBanner.viewport.test.tsx` | 155 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 1091 | G | admin-ui-viewport | `admin-ui/src/components/RuntimeWorkspace.viewport.test.tsx` | 154 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1092 | G | admin-ui-viewport | `admin-ui/src/components/ArchiveView.viewport.test.tsx` | 153 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1093 | G | admin-ui-viewport | `admin-ui/src/components/OuterReviewLoopConvergenceWidget.viewport.test.tsx` | 153 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1094 | G | admin-ui-viewport | `admin-ui/src/components/RecentErrorsWidget.viewport.test.tsx` | 151 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1095 | G | admin-ui-viewport | `admin-ui/src/components/AlertsBanner.viewport.test.tsx` | 149 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1096 | G | admin-ui-viewport | `admin-ui/src/components/ConsoleHome.viewport.test.tsx` | 147 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1097 | G | admin-ui-viewport | `admin-ui/src/components/DriftSignalsCard.viewport.test.tsx` | 146 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1098 | G | admin-ui-viewport | `admin-ui/src/components/WorkbenchLayout.viewport.test.tsx` | 144 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1099 | G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerLogs.viewport.test.tsx` | 142 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1100 | G | admin-ui-viewport | `admin-ui/src/components/FleetHealthCard.viewport.test.tsx` | 140 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1101 | G | admin-ui-viewport | `admin-ui/src/components/FixLoopConvergenceWidget.viewport.test.tsx` | 136 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1102 | G | admin-ui-viewport | `admin-ui/src/components/ReleaseQualificationFindings.viewport.test.tsx` | 133 | 6 | under 1500 LOC | unknown (no committed measurement) | — |
| 1103 | G | admin-ui-viewport | `admin-ui/src/components/DirectionPortfolioSection.viewport.test.tsx` | 129 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1104 | G | admin-ui-viewport | `admin-ui/src/components/MediumScopeWidget.viewport.test.tsx` | 127 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1105 | G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerDiagnostics.viewport.test.tsx` | 126 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1106 | G | admin-ui-viewport | `admin-ui/src/components/TaskStreamPanel.viewport.test.tsx` | 126 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1107 | G | admin-ui-viewport | `admin-ui/src/components/ProjectIntegrationBranchSection.viewport.test.tsx` | 125 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1108 | G | admin-ui-viewport | `admin-ui/src/components/ImprovementLanesPanel.viewport.test.tsx` | 123 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1109 | G | admin-ui-viewport | `admin-ui/src/components/CompactJobsTable.viewport.test.tsx` | 121 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1110 | G | admin-ui-viewport | `admin-ui/src/test/pipeline-composed.viewport.test.tsx` | 119 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1111 | G | admin-ui-viewport | `admin-ui/src/components/TaskDetailDrawerSelfReview.viewport.test.tsx` | 117 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1112 | G | admin-ui-viewport | `admin-ui/src/components/Pagination.viewport.test.tsx` | 116 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1113 | G | admin-ui-viewport | `admin-ui/src/components/ProjectManagementModal.viewport.test.tsx` | 116 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1114 | G | admin-ui-viewport | `admin-ui/src/components/StrategicAuditPanel.viewport.test.tsx` | 115 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1115 | G | admin-ui-viewport | `admin-ui/src/components/ProviderSettingsPanel.viewport.test.tsx` | 114 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1116 | G | admin-ui-viewport | `admin-ui/src/components/ProjectBranchesSection.viewport.test.tsx` | 113 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1117 | G | admin-ui-viewport | `admin-ui/src/components/ProjectMembers.viewport.test.tsx` | 108 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1118 | G | admin-ui-viewport | `admin-ui/src/components/RuntimeAccountsPanel.viewport.test.tsx` | 108 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1119 | G | admin-ui-viewport | `admin-ui/src/test/messages-composed.viewport.test.tsx` | 108 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1120 | G | admin-ui-viewport | `admin-ui/src/components/MessageTypeDropdown.viewport.test.tsx` | 106 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1121 | G | admin-ui-viewport | `admin-ui/src/components/ProjectToolIframe.viewport.test.tsx` | 106 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1122 | G | admin-ui-viewport | `admin-ui/src/components/ComputeArtifactGallery.viewport.test.tsx` | 105 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1123 | G | admin-ui-viewport | `admin-ui/src/components/MessageAttachments.viewport.test.tsx` | 105 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1124 | G | admin-ui-viewport | `admin-ui/src/components/PendingConfirmationsSection.viewport.test.tsx` | 105 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1125 | G | admin-ui-viewport | `admin-ui/src/components/ProjectPromotionTargetSection.viewport.test.tsx` | 105 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1126 | G | admin-ui-viewport | `admin-ui/src/components/ScopeConversionRatesSection.viewport.test.tsx` | 104 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1127 | G | admin-ui-viewport | `admin-ui/src/components/TaskContextRail.viewport.test.tsx` | 102 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1128 | G | admin-ui-viewport | `admin-ui/src/components/CapabilityTrialsPanel.viewport.test.tsx` | 101 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1129 | G | admin-ui-viewport | `admin-ui/src/components/SystemMaturityCard.viewport.test.tsx` | 101 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1130 | G | admin-ui-viewport | `admin-ui/src/components/ScopeTrendsSection.viewport.test.tsx` | 100 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1131 | G | admin-ui-viewport | `admin-ui/src/components/ReviewQualitySection.viewport.test.tsx` | 98 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1132 | G | admin-ui-viewport | `admin-ui/src/components/ParkWakeHealthCard.viewport.test.tsx` | 97 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1133 | G | admin-ui-viewport | `admin-ui/src/components/CriterionVerdictBadge.viewport.test.tsx` | 96 | 4 | under 1500 LOC | unknown (no committed measurement) | — |
| 1134 | G | admin-ui-viewport | `admin-ui/src/components/MetricsIntegrityObservabilityPanel.viewport.test.tsx` | 95 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1135 | G | admin-ui-viewport | `admin-ui/src/components/WarmReuseVsCostTrend.viewport.test.tsx` | 95 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1136 | G | admin-ui-viewport | `admin-ui/src/components/OperatorWorkspaces.viewport.test.tsx` | 94 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1137 | G | admin-ui-viewport | `admin-ui/src/components/FleetDetailDrawer.viewport.test.tsx` | 92 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1138 | G | admin-ui-viewport | `admin-ui/src/components/SettingsOrchestratorSection.viewport.test.tsx` | 91 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1139 | G | admin-ui-viewport | `admin-ui/src/components/SrRetrospectiveShared.viewport.test.tsx` | 90 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1140 | G | admin-ui-viewport | `admin-ui/src/components/UserManagement.viewport.test.tsx` | 90 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1141 | G | admin-ui-viewport | `admin-ui/src/components/ProjectResourceEnvelopeSection.viewport.test.tsx` | 88 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1142 | G | admin-ui-viewport | `admin-ui/src/components/SystemStatusCard.viewport.test.tsx` | 88 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1143 | G | admin-ui-viewport | `admin-ui/src/components/WhiteboardRailSection.viewport.test.tsx` | 88 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1144 | G | admin-ui-viewport | `admin-ui/src/components/FleetRunnerConfigPanel.viewport.test.tsx` | 87 | 5 | under 1500 LOC | unknown (no committed measurement) | — |
| 1145 | G | admin-ui-viewport | `admin-ui/src/components/ProjectAutoRemediationSection.viewport.test.tsx` | 87 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1146 | G | admin-ui-viewport | `admin-ui/src/components/SettingsGeneralSection.viewport.test.tsx` | 87 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1147 | G | admin-ui-viewport | `admin-ui/src/components/CommandPalette.viewport.test.tsx` | 85 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1148 | G | admin-ui-viewport | `admin-ui/src/components/DirectionAutoPauseWidget.viewport.test.tsx` | 85 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1149 | G | admin-ui-viewport | `admin-ui/src/components/SrBadges.viewport.test.tsx` | 85 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1150 | G | admin-ui-viewport | `admin-ui/src/components/CompletionDebtPanel.viewport.test.tsx` | 84 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1151 | G | admin-ui-viewport | `admin-ui/src/components/EvergreenBacklogPanel.viewport.test.tsx` | 84 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1152 | G | admin-ui-viewport | `admin-ui/src/components/ErrorBoundary.viewport.test.tsx` | 82 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1153 | G | admin-ui-viewport | `admin-ui/src/components/SettingsAdvancedSection.viewport.test.tsx` | 81 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1154 | G | admin-ui-viewport | `admin-ui/src/components/ProjectAlertDomainsSection.viewport.test.tsx` | 80 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1155 | G | admin-ui-viewport | `admin-ui/src/components/ProviderCategoriesPanel.viewport.test.tsx` | 80 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1156 | G | admin-ui-viewport | `admin-ui/src/components/DependencyHealthCard.viewport.test.tsx` | 79 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1157 | G | admin-ui-viewport | `admin-ui/src/components/ReleaseEvidence.viewport.test.tsx` | 76 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1158 | G | admin-ui-viewport | `admin-ui/src/components/ProjectIssueImplementationSection.viewport.test.tsx` | 75 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1159 | G | admin-ui-viewport | `admin-ui/src/components/ProjectResourceAvailabilityCard.viewport.test.tsx` | 75 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1160 | G | admin-ui-viewport | `admin-ui/src/components/ProviderCostPanel.viewport.test.tsx` | 74 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1161 | G | admin-ui-viewport | `admin-ui/src/components/ProjectAutoReviewSection.viewport.test.tsx` | 73 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1162 | G | admin-ui-viewport | `admin-ui/src/components/StrategicSessionControl.viewport.test.tsx` | 73 | 2 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 2 |
| 1163 | G | admin-ui-viewport | `admin-ui/src/components/UILoadTimingPanel.viewport.test.tsx` | 73 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1164 | G | admin-ui-viewport | `admin-ui/src/components/AutonomousConfigSection.viewport.test.tsx` | 70 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1165 | G | admin-ui-viewport | `admin-ui/src/components/DesignDocViewer.viewport.test.tsx` | 70 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1166 | G | admin-ui-viewport | `admin-ui/src/components/EventVisibilityMenu.viewport.test.tsx` | 70 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1167 | G | admin-ui-viewport | `admin-ui/src/components/MuteWindowMenu.viewport.test.tsx` | 69 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1168 | G | admin-ui-viewport | `admin-ui/src/components/TaskDurationWidget.viewport.test.tsx` | 69 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1169 | G | admin-ui-viewport | `admin-ui/src/components/DashboardMetricCards.viewport.test.tsx` | 68 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1170 | G | admin-ui-viewport | `admin-ui/src/components/StrategicStreamingPreview.viewport.test.tsx` | 68 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1171 | G | admin-ui-viewport | `admin-ui/src/components/StructuralHealthObservabilityPanel.viewport.test.tsx` | 68 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1172 | G | admin-ui-viewport | `admin-ui/src/components/ConsultationProgressRow.viewport.test.tsx` | 67 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1173 | G | admin-ui-viewport | `admin-ui/src/components/MarkdownContent.viewport.test.tsx` | 67 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1174 | G | admin-ui-viewport | `admin-ui/src/components/ProjectSwitcherDropdown.viewport.test.tsx` | 66 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1175 | G | admin-ui-viewport | `admin-ui/src/components/FailureStageWidget.viewport.test.tsx` | 65 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1176 | G | admin-ui-viewport | `admin-ui/src/components/FleetRunnersDashboard.viewport.test.tsx` | 65 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1177 | G | admin-ui-viewport | `admin-ui/src/components/RecurringPatternsSection.viewport.test.tsx` | 65 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1178 | G | admin-ui-viewport | `admin-ui/src/components/SavedViewsTabStrip.viewport.test.tsx` | 65 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1179 | G | admin-ui-viewport | `admin-ui/src/components/AcceptanceGradeBadge.viewport.test.tsx` | 64 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1180 | G | admin-ui-viewport | `admin-ui/src/components/EvergreenFillObservabilityPanel.viewport.test.tsx` | 63 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1181 | G | admin-ui-viewport | `admin-ui/src/components/SaveViewDialog.viewport.test.tsx` | 63 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1182 | G | admin-ui-viewport | `admin-ui/src/components/NullBranchDiagnosticsPanel.viewport.test.tsx` | 62 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1183 | G | admin-ui-viewport | `admin-ui/src/components/MergeGateFleetHealthCard.viewport.test.tsx` | 61 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1184 | G | admin-ui-viewport | `admin-ui/src/components/ToolResidentHandoffControls.viewport.test.tsx` | 61 | 2 | under 1500 LOC | unknown (no committed measurement) | protected; declared floor 2 |
| 1185 | G | admin-ui-viewport | `admin-ui/src/components/PoolAccountHealthPanel.viewport.test.tsx` | 60 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1186 | G | admin-ui-viewport | `admin-ui/src/components/StrategistStatusChip.viewport.test.tsx` | 58 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1187 | G | backend | `backend/testing/test_agent_task_contract.py` | 50 | 0 | under 1500 LOC | unknown (no committed measurement) | — |
| 1188 | G | admin-ui-viewport | `admin-ui/src/components/WarmStartOriginRow.viewport.test.tsx` | 49 | 2 | under 1500 LOC | unknown (no committed measurement) | — |
| 1189 | G | admin-ui-viewport | `admin-ui/src/components/AccountMenu.viewport.test.tsx` | 45 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1190 | G | admin-ui-viewport | `admin-ui/src/components/MessagesSummaryCard.viewport.test.tsx` | 41 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1191 | G | admin-ui-viewport | `admin-ui/src/components/EvidenceTierBadge.viewport.test.tsx` | 39 | 3 | under 1500 LOC | unknown (no committed measurement) | — |
| 1192 | G | admin-ui-viewport | `admin-ui/src/components/KeyboardShortcutsOverlay.viewport.test.tsx` | 36 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1193 | G | admin-ui-viewport | `admin-ui/src/components/TaskFixLoopPanel.viewport.test.tsx` | 32 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1194 | G | admin-ui-viewport | `admin-ui/src/components/AdversarialQualityPanel.viewport.test.tsx` | 28 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
| 1195 | G | admin-ui-viewport | `admin-ui/src/components/WelcomeScreen.viewport.test.tsx` | 28 | 1 | under 1500 LOC | unknown (no committed measurement) | — |
