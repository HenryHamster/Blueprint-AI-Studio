"""Tests for the onboarding-proof first-PR-merged stamp wired into the
central merge-landed choke point ``apply_merged_landed_signal``.

``apply_merged_landed_signal`` is the single place every merge-landed path
(ci_completion, rollup, reaper, recovery, …) routes through to record the
capability proof, so the first-PR-merged onboarding milestone is stamped there
— mirroring ``record_capability_proof`` (no session parameter; the caller's
session already tracks the project).
"""

import pytest

from app.branch_resolution import get_project_integration_branch
from app.landed_signal import apply_merged_landed_signal
from app.models import Project, Task, TaskStatus


@pytest.mark.asyncio
async def test_first_pr_merged_stamped_on_integration_landing(db_session):
    """A merge that lands on the integration branch stamps first_pr_merged_at."""
    project = Project(name="first pr merged", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="merged create task",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    integration = get_project_integration_branch(project)
    landed = apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )

    assert landed is True
    assert (project.build_config or {}).get("first_pr_merged_at") is not None


@pytest.mark.asyncio
async def test_first_pr_merged_stamp_is_idempotent(db_session):
    """A second landing never overwrites the first first_pr_merged_at value."""
    project = Project(name="first pr merged idempotent", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="merged create task",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    integration = get_project_integration_branch(project)
    apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )
    first_value = (project.build_config or {}).get("first_pr_merged_at")
    assert first_value is not None

    # A later merge landing must not move the recorded first-merge milestone.
    apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )
    assert (project.build_config or {}).get("first_pr_merged_at") == first_value


@pytest.mark.asyncio
async def test_first_pr_merged_not_stamped_for_non_integration_landing(db_session):
    """A merge that did NOT land on the integration branch records nothing."""
    project = Project(name="non-integration landing", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="merged onto feature branch",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    landed = apply_merged_landed_signal(
        task,
        project,
        base_branch="some-unrelated-feature-branch",
        hotfix_lane=False,
        non_merge_reason="merged_pr_non_integration_branch",
    )

    assert landed is False
    assert (project.build_config or {}).get("first_pr_merged_at") is None


# ---------------------------------------------------------------------------
# stamp_first_autonomous_improvement tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_autonomous_improvement_stamped_for_create_task(db_session):
    """A create task merging on a non-self-repo project stamps the milestone."""
    project = Project(name="external project", user_id="test-user", is_self_repo=False)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="create task",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    integration = get_project_integration_branch(project)
    landed = apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )

    assert landed is True
    assert (project.build_config or {}).get(
        "first_autonomous_improvement_at"
    ) is not None


@pytest.mark.asyncio
async def test_first_autonomous_improvement_not_stamped_for_self_repo(db_session):
    """Self-repo projects never get the external-improvement milestone."""
    project = Project(name="self repo", user_id="test-user", is_self_repo=True)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="create task",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    integration = get_project_integration_branch(project)
    apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )

    assert (project.build_config or {}).get("first_autonomous_improvement_at") is None


@pytest.mark.asyncio
async def test_first_autonomous_improvement_not_stamped_for_migrate_task(db_session):
    """A migrate task (not in the code-change set) does not stamp the milestone."""
    project = Project(name="external migrate", user_id="test-user", is_self_repo=False)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="migrate task",
        payloads=[{"type": "migrate"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    integration = get_project_integration_branch(project)
    apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )

    assert (project.build_config or {}).get("first_autonomous_improvement_at") is None


@pytest.mark.asyncio
async def test_first_autonomous_improvement_stamp_is_idempotent(db_session):
    """A second landing never overwrites the first autonomous-improvement timestamp."""
    project = Project(
        name="external idempotent", user_id="test-user", is_self_repo=False
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="fix task",
        payloads=[{"type": "fix"}],
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    integration = get_project_integration_branch(project)
    apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )
    first_value = (project.build_config or {}).get("first_autonomous_improvement_at")
    assert first_value is not None

    apply_merged_landed_signal(
        task, project, base_branch=integration, hotfix_lane=False
    )
    assert (project.build_config or {}).get(
        "first_autonomous_improvement_at"
    ) == first_value
