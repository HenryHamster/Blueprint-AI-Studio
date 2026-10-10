# Design history

These documents record proposals, decisions, and handoffs from earlier development. They are retained at their original paths because implementation comments and evidence refer to them. They are not a current product roadmap or a list of supported features.

Use the [portfolio architecture](../portfolio/ARCHITECTURE.md) to navigate the current source. Use [AGENTS.md](../../AGENTS.md) for authoritative boundaries, especially the supported conversational Workday path, the retired shadow/control-plane Resident architecture, and release qualification.

## Reading by topic

| Topic | Selected historical records |
| --- | --- |
| Task lifecycle and recovery | [Lifecycle centralization](task-lifecycle-centralization-plan-v1.md), [error budget](orchestrator-error-budget-remediation.md), [pipeline repair](pipeline-operability-repair-20260725.md) |
| Providers | [Groundwork](multi-provider-groundwork-plan-v1.md), [runtime groundwork](multi-provider-runtime-groundwork-plan-v1.md), [provider plan](multi-provider-llm-plan-v3.md) |
| Release and infrastructure | [Integration-branch design](automain-lts-plan-v1.md), [local infrastructure costs](local-first-infrastructure-cost-plan-20260727.md) |
| Research loops | [Experiment loop](experiment-loop-redesign-20260711.md), [spawn budget](autonomous-spawn-budget-redesign-v1.md), [convergence checkpoint](blueprint-rsi-convergence-checkpoint-20260801.md) |
| Operator experience | [Workbench roadmap](operator-workbench-full-usability-roadmap.md), [conversation design](live-conversation-resident-design-20260726.md) |

Resident redesign proposals in this directory include abandoned alternatives. Their presence must not be interpreted as authority to restore retired models, workers, task types, APIs, or controls.
