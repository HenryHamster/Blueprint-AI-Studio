# Design history

The [integration-branch design](automain-lts-plan-v1.md) is retained as context for references in `release_version.py` and `hotfix_backmerge.py`. It records an earlier design; current behavior is defined by the implementation and [AGENTS.md](../../AGENTS.md).

Use the [portfolio architecture](../portfolio/ARCHITECTURE.md) to navigate the current source. Use [AGENTS.md](../../AGENTS.md) for authoritative boundaries, especially the supported conversational Workday path, the retired shadow/control-plane Resident architecture, and release qualification.

Other superseded proposals and handoffs are preserved in Git history:

```bash
git log -- docs/plans
git show <commit>:docs/plans/<filename>
```

Earlier Resident redesign proposals include abandoned alternatives. Historical plans do not authorize restoring retired models, workers, task types, APIs, or controls.
