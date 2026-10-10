# Documentation

For a first visit, start with the [project overview](../README.md), then read the [architecture](portfolio/ARCHITECTURE.md) and [engineering case study](portfolio/CASE_STUDY.md). [Local setup](../CONTRIBUTING.md) covers running the existing application.

## Portfolio reading path

| Document | Purpose |
| --- | --- |
| [Architecture](portfolio/ARCHITECTURE.md) | Component boundaries, task flow, and representative source files |
| [Case study](portfolio/CASE_STUDY.md) | Engineering decisions, tradeoffs, and discussion points |
| [Local UI](portfolio/DEMO.md) | Launch commands and backend/authentication requirements |
| [Verification](portfolio/VERIFICATION.md) | Checks run for the cleanup and their limits |
| [History](portfolio/HISTORY.md) | Original-history backup and a clean, unpublished portfolio snapshot |

## Development and operation

| Topic | Read first | Further reference |
| --- | --- | --- |
| Development | [Contributing](../CONTRIBUTING.md) | [Development guide](DEVELOPMENT_GUIDE.md), [testing guide](TESTING_GUIDE.md), [common patterns](COMMON_PATTERNS.md) |
| Authentication and onboarding | [Onboarding](ONBOARDING.md) | [Coding-agent access](CODING_AGENT_ACCESS.md), [operator access](HERMES_OPERATOR_ACCESS.md) |
| Task execution | [Task lifecycle](TASK_LIFECYCLE.md) | [Task payloads](PAYLOADS_AND_SCHEMAS.md), [agent handlers](AGENT_HANDLERS.md) |
| API and persistence | [API reference](API_REFERENCE.md) | [Data models](DATA_MODELS.md), [backend modules](BACKEND_MODULES.md), [concurrency](concurrency.md) |
| Operator workspace | [Operator experience](operator-experience-operating-model.md) | [Frontend components](FRONTEND_COMPONENTS.md) |
| Runners and compute | [Runner runbook](RUNNER_RUNBOOK.md) | [Runner installation](runner-install.md), [local compute](COMPUTE_RUN_LOCAL.md) |
| Deployment | [Deployment guide](../Deploy.md) | [Local control plane](local-control-plane.md), [local app release](operations/local-app-release.md), [one-command deploy](operations/one-command-deploy.md) |
| Configuration | [Configuration guide](CONFIGURATION.md) | [Environment template](../.env.example), [Node installation](node-install.md) |
| Diagnosis | [Debugging guide](DEBUG_GUIDE.md) | [Database operations](db-operations.md), [database deferrals](db-deferrals.md) |
| Recovery | [Recovery infrastructure](../infra/recovery/README.md) | [Restore verifier](../infra/recovery/restore_verifier/README.md) |

The detailed references cover a system that evolved over many iterations. When a reference and code disagree, use the current implementation and [AGENTS.md](../AGENTS.md) boundaries. The portfolio architecture is the short source-oriented map; it does not replace operational runbooks.

## Design history and evidence

- [Design plans](plans/README.md) retain the rationale and scope of earlier proposals. A proposal alone does not establish implementation or current support.
- [Recorded operator walkthrough](walkthroughs/v26-operator-walkthrough.md) preserves a July 2026 execution, including screenshots, failures, and dead ends.
- [Release authorship history](findings/release-authorship-history.md) is a generated account of release-tag provenance, not personal-contribution attribution.
- [Merge-rate measurement finding](findings/medium-scope-merge-rate-measurement.md) documents a measurement interface; its unfilled snapshot does not support a numerical performance claim.
- [Persistent-agent-memory research](archive/research-persistent-agent-memory.md) is retained as an exploratory note.
- [Viewport audit](audits/viewport_test_quality_audit.md), [CI cost audit](ci-cost-audit.md), and [dependency audit](v19-dependency-bump-audit.md) retain their original audit scope.

Generated test-estate reports remain at their existing repository paths because validation tooling consumes them: [rubric](../test-estate-rubric.md) and [ranked queue](../test-estate-ranked-queue.md).
