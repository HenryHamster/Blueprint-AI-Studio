"""Tests for the integration-landing signals and acceptance verdict stamping.

``mark_landed_on_integration`` is the canonical primitive every real
integration-branch landing path routes through.  Since every such path must
record the code-task acceptance verdict (so rollup descendants declared with
acceptance evidence end up with a verdict rather than being stuck in
'undeclared'), the verdict recording is folded into this primitive.

``apply_merged_landed_signal`` is the guarded write path that routes through
``mark_landed_on_integration`` and also stamps the onboarding proof and
capability proof.
"""

import uuid

import pytest

from app.acceptance_gate import (
    CODE_ACCEPTANCE_VERDICT_KEY,
    classify_merged_acceptance_context,
)
from app.branch_resolution import get_project_integration_branch
from app.landed_signal import (
    apply_merged_landed_signal,
    mark_landed_on_integration,
    LANDED_MERGE_SHA_KEY,
    LANDED_SIGNAL_KEY,
    NON_MERGE_UNBLOCK_REASON_KEY,
)
from app.models import Project, Task, TaskStatus
from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY, LANDED_ON_INTEGRATION_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_code_task_with_evidence(
    *,
    acceptance_evidence: dict | None = None,
    changed_files: list[str] | None = None,
    unverified_reason: str | None = None,
) -> Task:
    payload: dict = {"type": "create", "prompt": "do the thing"}
    if acceptance_evidence is not None:
        payload["acceptance_evidence"] = acceptance_evidence
    context: dict = {}
    if changed_files is not None:
        context["_pr_changed_files"] = changed_files
    # The merge gate always stamps review_loop; an unverified merge carries
    # unverified_merge_reason inside it. An empty dict = gate ran and the merge
    # was test-verified — and an ABSENT review_loop is treated as verified too
    # (matching landed_signal.is_verified_merge / _merge_was_test_verified), so
    # the verified case is stamped explicitly for the propagation tests below.
    if unverified_reason is not None:
        context[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: unverified_reason}
    else:
        context[REVIEW_LOOP_KEY] = {}
    return Task(
        project_id=uuid.uuid4(),
        title="code task",
        payloads=[payload],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context=context,
    )


# ---------------------------------------------------------------------------
# Acceptance verdict via mark_landed_on_integration (propagation paths)
# ---------------------------------------------------------------------------


def test_acceptance_verdict_recorded_on_propagation_landing():
    """mark_landed_on_integration stamps the acceptance verdict for declared tasks.

    This is the named acceptance-evidence test required by the task plan:
    a task that declared a named_test with a real diff lands via the propagation
    primitive and ends up with code_acceptance_verdict present and classified as
    non-'undeclared'.
    """
    task = _make_code_task_with_evidence(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,  # verified merge
    )

    mark_landed_on_integration(task)

    assert CODE_ACCEPTANCE_VERDICT_KEY in (task.context or {})
    verdict = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert verdict["satisfied"] is True
    assert verdict["proven"] is True
    assert verdict["kind"] == "named_test"
    # Must classify out of 'undeclared' — the root-cause fix.
    bucket = classify_merged_acceptance_context(task.context)
    assert bucket != "undeclared", f"expected non-undeclared bucket, got {bucket!r}"
    assert bucket == "proven"


def test_acceptance_verdict_not_recorded_for_undeclared_task():
    """mark_landed_on_integration leaves code_acceptance_verdict absent for tasks
    with no declared acceptance evidence (the no-op path stays intact)."""
    task = _make_code_task_with_evidence(
        acceptance_evidence=None,
        changed_files=["backend/app/other.py"],
    )

    mark_landed_on_integration(task)

    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task.context or {})
    bucket = classify_merged_acceptance_context(task.context)
    assert bucket == "undeclared"


def test_acceptance_verdict_failure_does_not_block_landing(monkeypatch):
    """A failure inside record_code_acceptance_verdict must never prevent
    landed_on_integration from being written — the verdict is best-effort."""
    import app.acceptance_gate as ag

    monkeypatch.setattr(ag, "evaluate_code_acceptance", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))

    task = _make_code_task_with_evidence(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
    )

    # Must not raise even though verdict recording will throw.
    mark_landed_on_integration(task)

    # The landing bit must be written regardless.
    from app.landed_signal import LANDED_ON_INTEGRATION_KEY
    assert task.context.get(LANDED_ON_INTEGRATION_KEY) is True


# ---------------------------------------------------------------------------
# Merge commit identity recording
# ---------------------------------------------------------------------------


def test_mark_landed_on_integration_records_merge_sha():
    """The canonical landing write records the exact merge commit SHA.

    This is the named acceptance-evidence test required by the task plan: a
    landed write task stamped through ``mark_landed_on_integration`` with a
    merge commit carries ``merge_commit_sha`` in context so the
    discovery-review receipt's ``_merge_sha`` reader can resolve exact commit
    identity instead of reporting "lacks exact commit identity".
    """
    task = _make_code_task_with_evidence(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )

    mark_landed_on_integration(task, merge_commit_sha="a" * 40)

    assert task.context.get(LANDED_SIGNAL_KEY) is True
    assert task.context.get(LANDED_ON_INTEGRATION_KEY) is True
    assert task.context.get(LANDED_MERGE_SHA_KEY) == "a" * 40


def test_mark_landed_on_integration_truncates_long_merge_sha():
    """A merge SHA longer than 64 chars is truncated to the reader's bound."""
    task = _make_code_task_with_evidence(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )

    mark_landed_on_integration(task, merge_commit_sha="b" * 80)

    recorded = task.context.get(LANDED_MERGE_SHA_KEY)
    assert recorded == "b" * 64
    assert len(recorded) == 64


def test_mark_landed_on_integration_preserves_existing_merge_sha_when_none():
    """Re-stamping a landing without a SHA must not erase a recorded SHA."""
    task = _make_code_task_with_evidence(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )
    task.context = {
        **(task.context or {}),
        LANDED_MERGE_SHA_KEY: "c" * 40,
    }

    mark_landed_on_integration(task)

    assert task.context.get(LANDED_MERGE_SHA_KEY) == "c" * 40


def test_apply_merged_landed_signal_stamps_merge_sha_only_on_integration():
    """apply_merged_landed_signal forwards the SHA on an integration landing."""
    task = _make_code_task_with_evidence(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )
    project = Project(
        name="sha integration", user_id="test-user", integration_branch="automain"
    )
    integration = get_project_integration_branch(project)

    landed = apply_merged_landed_signal(
        task,
        project,
        base_branch=integration,
        hotfix_lane=False,
        merge_commit_sha="d" * 40,
    )

    assert landed is True
    assert task.context.get(LANDED_MERGE_SHA_KEY) == "d" * 40


def test_apply_merged_landed_signal_omits_sha_on_non_integration_landing():
    """A merge onto a staging branch records the unblock marker but no SHA."""
    task = _make_code_task_with_evidence(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )
    project = Project(
        name="sha staging", user_id="test-user", integration_branch="automain"
    )

    landed = apply_merged_landed_signal(
        task,
        project,
        base_branch="research/def01234",
        hotfix_lane=False,
        non_merge_reason="merged_pr_non_integration_branch",
        merge_commit_sha="e" * 40,
    )

    assert landed is False
    assert task.context.get(LANDED_MERGE_SHA_KEY) is None
    assert task.context.get(NON_MERGE_UNBLOCK_REASON_KEY) == (
        "merged_pr_non_integration_branch"
    )


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


def test_first_autonomous_improvement_skips_deploy_identity_repo(monkeypatch):
    """A repo matching the stamped deploy identity is treated as the deploy's
    own repo and skipped, even when is_self_repo is False."""
    from app.onboarding_proof import stamp_first_autonomous_improvement

    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    project = Project(
        name="deploy repo",
        user_id="test-user",
        is_self_repo=False,
        github_owner="acme",
        github_repo="widget",
    )
    task = Task(
        project_id=project.id,
        title="create task",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
    )

    stamp_first_autonomous_improvement(task, project)

    assert (project.build_config or {}).get("first_autonomous_improvement_at") is None


def test_first_autonomous_improvement_stamped_when_identity_mismatch(monkeypatch):
    """is_self_repo misset True but the repo is NOT the deploy identity → the
    external-improvement milestone is stamped (identity is authoritative)."""
    from app.onboarding_proof import stamp_first_autonomous_improvement

    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")
    project = Project(
        name="external repo misflagged",
        user_id="test-user",
        is_self_repo=True,
        github_owner="other",
        github_repo="proj",
    )
    task = Task(
        project_id=project.id,
        title="fix task",
        payloads=[{"type": "fix"}],
        status=TaskStatus.EVALUATING,
    )

    stamp_first_autonomous_improvement(task, project)

    assert (
        project.build_config or {}
    ).get("first_autonomous_improvement_at") is not None


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


# ---------------------------------------------------------------------------
# Stale unverified_merge_reason clearing on staging→integration promotion
# ---------------------------------------------------------------------------


def test_stale_unverified_reason_cleared_on_promotion_to_integration():
    """When a staging-merged child is promoted to integration-landed, the stale
    unverified_merge_reason is cleared so the acceptance verdict evaluates
    _merge_was_test_verified correctly.

    A staging-merged child carries _non_merge_unblock_reason (set by
    mark_unblocked_non_merge when the PR merged to a staging branch) and
    review_loop["unverified_merge_reason"]="internal_staging_base" (stamped
    by the CI monitor because staging branches lack CI).  When the rollup
    parent lands on integration and propagation calls
    mark_landed_on_integration(child), the stale staging-branch reason must
    be cleared — the task now reaches integration through the rollup, whose
    CI serves as the real verification gate.  Only clearing when
    _non_merge_unblock_reason was present ensures direct integration merges
    with legitimate unverified reasons are unaffected.
    """
    task = _make_code_task_with_evidence(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason="internal_staging_base",
    )
    # Staging-merge marker: _non_merge_unblock_reason present on original context.
    task.context = {
        **(task.context or {}),
        NON_MERGE_UNBLOCK_REASON_KEY: "merged_pr_non_integration_branch",
    }

    mark_landed_on_integration(task)

    # The stale staging-branch unverified_merge_reason must be cleared.
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    assert isinstance(review_loop, dict)
    assert UNVERIFIED_MERGE_REASON_KEY not in review_loop

    # The landing flags must be stamped correctly.
    assert task.context.get(LANDED_ON_INTEGRATION_KEY) is True
    assert NON_MERGE_UNBLOCK_REASON_KEY not in task.context

    # The acceptance verdict must be proven=True — the cleared reason lets
    # _merge_was_test_verified return True, and the test+prod files match.
    assert CODE_ACCEPTANCE_VERDICT_KEY in (task.context or {})
    verdict = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert verdict["satisfied"] is True, f"verdict unsatisfied: {verdict.get('reason')}"
    assert verdict["proven"] is True, f"verdict not proven: {verdict.get('reason')}"
    assert verdict["kind"] == "named_test"
    bucket = classify_merged_acceptance_context(task.context)
    assert bucket == "proven", f"expected 'proven', got {bucket!r}"


def test_unverified_merge_reason_preserved_on_direct_landing():
    """A direct integration merge with a legitimate unverified_merge_reason
    (e.g., kill_switch_off) and NO _non_merge_unblock_reason must preserve
    the reason — the task was never staging-merged, so the reason is real.

    The guard must be narrow: only tasks that were previously staging-merged
    (had _non_merge_unblock_reason) get their unverified_merge_reason cleared.
    A direct landing with a real unverified reason must keep it so the
    acceptance verdict correctly reflects that the merge was not CI-verified.
    """
    task = _make_code_task_with_evidence(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason="kill_switch_off",
    )
    # No _non_merge_unblock_reason — this is a direct integration merge.

    mark_landed_on_integration(task)

    # The legitimate unverified_merge_reason must NOT be cleared.
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    assert isinstance(review_loop, dict)
    assert review_loop.get(UNVERIFIED_MERGE_REASON_KEY) == "kill_switch_off"

    # The landing flags must be stamped correctly.
    assert task.context.get(LANDED_ON_INTEGRATION_KEY) is True

    # The acceptance verdict must reflect the unverified merge:
    # _merge_was_test_verified returns False, so the named_test gate
    # records satisfied=False (the merge fell open without verification).
    assert CODE_ACCEPTANCE_VERDICT_KEY in (task.context or {})
    verdict = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert verdict["satisfied"] is False, f"expected unsatisfied, got {verdict}"
    assert verdict["proven"] is False
    assert verdict["kind"] == "named_test"
    bucket = classify_merged_acceptance_context(task.context)
    assert bucket == "declared_unsatisfied", f"expected 'declared_unsatisfied', got {bucket!r}"
