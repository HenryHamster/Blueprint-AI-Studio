"""Seam tests for cluster C5 — approval/safeguard gate as a single invariant.

ROOT CAUSE addressed: requires_approval and the cross-cutting safeguard gate
were managed as point-in-time side-effects, and require_approval_gates was
consulted inconsistently across set/check/release sites. Symptoms:
  (a) rollup scan-error / scan-blocked / alembic-block + the cross-cutting
      safeguard spawn set requires_approval WITHOUT consulting
      require_approval_gates → with gates OFF, services.py's 2h timeout FAILs a
      healthy parent and discards merged siblings;
  (b) the merge primitive (_attempt_merge_with_retry_and_escalation) never
      re-read requires_approval → a gate set after ci_waiting parking was
      bypassed and a merge proceeded before the safeguard finished;
  (c) the safeguard gate was released only on the COMPLETED branch of
      security_audit → a FAILED/reaped safeguard stranded requires_approval
      forever, and when the gated orig is already COMPLETED the release path
      skipped the merge re-arm so the open PR stranded forever.

Each test below would FAIL against the pre-fix code: gates-off set sites set
the flag, the merge primitive merged through a set gate, and the
release-on-COMPLETED-orig path never fired the held merge.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app import shared_state
from app.models import Project, Task, TaskStatus


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    """Default the pre-merge Alembic dual-head guard to a no-op.

    ``_attempt_merge_with_retry_and_escalation`` runs
    ``enforce_premerge_migration_guard`` (which calls ``github.get_pr_files``)
    AFTER the requires_approval gate. These gate tests only mock
    ``merge_pull_request``; returning an empty file list short-circuits the
    guard to ``ok`` so the gate behavior under test is what is exercised.
    """
    mocker.patch("app.github.get_pr_files", AsyncMock(return_value=[{"filename": "README.md"}]))


@pytest.fixture
def _gates_off():
    """Set require_approval_gates=False for the test, restore after."""
    original = shared_state.runtime_config.get("require_approval_gates", True)
    shared_state.runtime_config["require_approval_gates"] = False
    try:
        yield
    finally:
        shared_state.runtime_config["require_approval_gates"] = original


@pytest.fixture
def _gates_on():
    original = shared_state.runtime_config.get("require_approval_gates", True)
    shared_state.runtime_config["require_approval_gates"] = True
    try:
        yield
    finally:
        shared_state.runtime_config["require_approval_gates"] = original


def _make_merge_task(db_session, project, *, requires_approval, pr_number=42):
    """An EVALUATING task parked at the merge gate (both review_loop gates met)."""
    task = Task(
        project_id=project.id,
        title="C5 merge gate task",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": "blueprint/c5-branch",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": pr_number,
                "branch": "blueprint/c5-branch",
                "base_branch": "main",
                "iteration": 1,
                "review_approved": True,
                "ci_passed": True,
                "ci_waiting": True,
            }
        },
        vm_instance_id=f"agent-vm-c5-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
        requires_approval=requires_approval,
    )
    db_session.add(task)
    return task


# ---------------------------------------------------------------------------
# Fix #2 — merge primitive re-checks requires_approval (symptom b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_primitive_refuses_when_requires_approval_and_gates_on(
    db_session, mocker, _gates_on
):
    """With gates ON and requires_approval set AFTER ci_waiting parking, the
    merge primitive must NOT merge — it leaves the task EVALUATING and returns
    False. Pre-fix: it merged because it only checked review_approved+ci_passed.
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = Project(name="C5 gate-on", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_merge_task(db_session, project, requires_approval=True)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "tok", "owner", "repo", db_session, source="poll"
    )

    assert result is False, "Must refuse to merge while approval gate is set"
    merge_mock.assert_not_called()
    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING, "Task must stay EVALUATING, not FAIL"
    assert task.requires_approval is True, "Gate flag is untouched (still gated)"


@pytest.mark.asyncio
async def test_merge_primitive_proceeds_when_requires_approval_but_gates_off(
    db_session, mocker, _gates_off
):
    """With gates OFF the flag is advisory — the merge must proceed (the
    set sites no longer set it, but a legacy flag must not block forever).
    """
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.ci_completion._post_merge_summary",
        new_callable=AsyncMock,
        create=True,
    )

    project = Project(name="C5 gate-off", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_merge_task(db_session, project, requires_approval=True)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "tok", "owner", "repo", db_session, source="poll"
    )

    assert result is True, "Gates off → advisory flag must not block the merge"
    merge_mock.assert_awaited()


@pytest.mark.asyncio
async def test_merge_primitive_merges_normally_when_no_gate(
    db_session, mocker, _gates_on
):
    """Control: with no approval gate set the merge proceeds as before."""
    from app.ci_completion import _attempt_merge_with_retry_and_escalation

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = Project(name="C5 no-gate", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = _make_merge_task(db_session, project, requires_approval=False)
    await db_session.commit()
    await db_session.refresh(task)

    result = await _attempt_merge_with_retry_and_escalation(
        task, "tok", "owner", "repo", db_session, source="poll"
    )

    assert result is True
    merge_mock.assert_awaited()
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# Fix #1 — cross-cutting safeguard spawn gated on require_approval_gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safeguard_spawn_skipped_when_gates_off(db_session, mocker, _gates_off):
    """With gates OFF, maybe_spawn_cross_cutting_safeguard must early-return:
    no VM spawned, orig.requires_approval never set. Pre-fix it spawned a
    safeguard VM and set requires_approval regardless of the gate.
    """
    from app.review_pipeline import maybe_spawn_cross_cutting_safeguard

    mocker.patch(
        "app.review_pipeline.broadcast_to_project",
        new_callable=AsyncMock,
        create=True,
    )

    project = Project(name="C5 safeguard gates-off", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="cross-cutting orig",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.EVALUATING,
        # scope=large always trips _is_cross_cutting_pr when reached
        context={"scope": "large", "_pr_changed_files": ["backend/a.py", "agent/b.ts"]},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    spawned = await maybe_spawn_cross_cutting_safeguard(
        orig, 99, "blueprint/branch", "main", db_session
    )

    assert spawned is False, "No safeguard should spawn with gates off"
    await db_session.refresh(orig)
    assert orig.requires_approval is False, "Gate flag must NOT be set with gates off"
    assert "safeguard_task_id" not in (orig.context or {})


@pytest.mark.asyncio
async def test_safeguard_spawn_sets_gate_when_gates_on(db_session, mocker, _gates_on):
    """Control: with gates ON a cross-cutting PR spawns the safeguard and sets
    the merge gate — the intended behaviour the gates-off path opts out of.
    """
    from app.review_pipeline import maybe_spawn_cross_cutting_safeguard

    mocker.patch(
        "app.review_pipeline.broadcast_to_project",
        new_callable=AsyncMock,
        create=True,
    )

    project = Project(name="C5 safeguard gates-on", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="cross-cutting orig",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.EVALUATING,
        context={"scope": "large", "_pr_changed_files": ["backend/a.py", "agent/b.ts"]},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    spawned = await maybe_spawn_cross_cutting_safeguard(
        orig, 99, "blueprint/branch", "main", db_session
    )

    assert spawned is True
    await db_session.refresh(orig)
    assert orig.requires_approval is True
    assert (orig.context or {}).get("safeguard_task_id")


# ---------------------------------------------------------------------------
# Fix #3c — release-on-COMPLETED-orig fires the held merge (symptom c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_gate_fires_merge_when_orig_already_completed(
    db_session, mocker
):
    """When the gated orig is already COMPLETED with an open mergeable PR,
    releasing the gate must FIRE the held merge (idempotency-guarded), not just
    clear the flag. Pre-fix: the flag was cleared and the PR stranded forever.
    """
    from app.task_completion import _maybe_release_safeguard_gate

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "open", "merged": False},
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    project = Project(name="C5 release completed orig", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="orig already completed",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        context={},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = Task(
        project_id=project.id,
        title="safeguard",
        payloads=[{"type": "security_audit", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": 321,
        },
    )
    db_session.add(safeguard)
    await db_session.commit()
    await db_session.refresh(safeguard)

    await _maybe_release_safeguard_gate(safeguard, [], db_session)

    await db_session.refresh(orig)
    assert orig.requires_approval is False, "Gate must be released"
    get_pr.assert_awaited_once()
    # Held merge must fire for an already-COMPLETED orig.
    merge_mock.assert_awaited_once()
    assert (orig.context or {}).get("_safeguard_merge_fired") is True


# Same-revision dual-head collision diff: the PR adds a 0002 migration while
# the integration branch already has a 0002 — a duplicate revision id / dual
# head that the pre-merge guard must block.
_C5_DUAL_HEAD_DIFF = (
    "diff --git a/backend/alembic/versions/0002_dup.py "
    "b/backend/alembic/versions/0002_dup.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/backend/alembic/versions/0002_dup.py\n"
    '+"""dup migration"""\n'
    '+revision = "0002"\n'
    '+down_revision = "0001"\n'
)


def _patch_c5_dual_head_collision(mocker):
    """Make enforce_premerge_migration_guard see a same-revision dual head."""
    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(
            return_value=[{
                "filename": "backend/alembic/versions/0002_dup.py",
                "status": "added",
                "patch": '+"""dup migration"""\n+revision = "0002"\n+down_revision = "0001"',
            }]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        AsyncMock(return_value=_C5_DUAL_HEAD_DIFF),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        AsyncMock(return_value=["0001_init.py", "0002_existing.py"]),
    )


@pytest.mark.asyncio
async def test_release_gate_completed_orig_held_on_dual_head(db_session, mocker):
    """The post-gate merge of an already-COMPLETED orig HOLDS on a dual head.

    When the held PR targets the integration/promotion branch (_sg_gated=True)
    and the combined migration chain has a duplicate revision id, the pre-merge
    Alembic guard must block: merge_pull_request is NOT called and the PR is
    left open for the guarded retry/resolver flow. The idempotency flag is still
    committed (the gate-clear branch ran), so the merge is never re-attempted
    from here. Pre-fix the guard was absent on this branch and the dual head
    would have merged through.
    """
    from app.task_completion import _maybe_release_safeguard_gate

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    # PR is open and targets the project's integration branch, so _sg_gated=True
    # and the dual-head guard runs.
    get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "merged": False,
            "base": {"ref": "automain"},
        },
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    _patch_c5_dual_head_collision(mocker)

    project = Project(
        name="C5 dual-head hold", user_id="u1", integration_branch="automain"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="orig already completed",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        context={},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = Task(
        project_id=project.id,
        title="safeguard",
        payloads=[{"type": "security_audit", "base_branch": "automain"}],
        status=TaskStatus.COMPLETED,
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": 909,
        },
    )
    db_session.add(safeguard)
    await db_session.commit()
    await db_session.refresh(safeguard)

    await _maybe_release_safeguard_gate(safeguard, [], db_session)

    await db_session.refresh(orig)
    # Gate is still released (the clear-and-fire branch ran)...
    assert orig.requires_approval is False, "Gate must be released"
    # ...but the dual head HOLDS the held merge: PR left open, no merge call.
    get_pr.assert_awaited_once()
    merge_mock.assert_not_called()
    # Idempotency flag committed so the merge is never re-attempted from here.
    assert (orig.context or {}).get("_safeguard_merge_fired") is True


@pytest.mark.asyncio
async def test_release_gate_completed_orig_is_idempotent(db_session, mocker):
    """A second release call must NOT re-fire the merge (idempotency flag)."""
    from app.task_completion import _maybe_release_safeguard_gate

    mocker.patch(
        "app.task_completion.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "open", "merged": False},
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    project = Project(name="C5 idempotent", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="orig completed",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        # already fired once
        context={"_safeguard_merge_fired": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = Task(
        project_id=project.id,
        title="safeguard",
        payloads=[{"type": "security_audit"}],
        status=TaskStatus.COMPLETED,
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": 321,
        },
    )
    db_session.add(safeguard)
    await db_session.commit()
    await db_session.refresh(safeguard)

    await _maybe_release_safeguard_gate(safeguard, [], db_session)

    await db_session.refresh(orig)
    assert orig.requires_approval is False
    # Must not re-fire the merge once the idempotency flag is set.
    merge_mock.assert_not_called()


@pytest.mark.asyncio
async def test_release_gate_evaluating_orig_rearms_ci_waiting(db_session, mocker):
    """Control: an EVALUATING orig re-arms ci_waiting (hand-off to CI loop),
    NOT a direct merge — preserves the existing happy-path behaviour.
    """
    from app.task_completion import _maybe_release_safeguard_gate

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    project = Project(name="C5 evaluating orig", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="orig evaluating",
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"review_loop": {"review_approved": True}},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = Task(
        project_id=project.id,
        title="safeguard",
        payloads=[{"type": "security_audit"}],
        status=TaskStatus.COMPLETED,
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": 654,
        },
    )
    db_session.add(safeguard)
    await db_session.commit()
    await db_session.refresh(safeguard)

    await _maybe_release_safeguard_gate(safeguard, [], db_session)

    await db_session.refresh(orig)
    assert orig.requires_approval is False
    assert (orig.context or {})["review_loop"]["ci_waiting"] is True
    # EVALUATING orig hands off to the CI loop; no direct merge here.
    merge_mock.assert_not_called()


@pytest.mark.asyncio
async def test_release_gate_maintained_on_critical_finding(db_session, mocker):
    """Control: a critical/high finding keeps the gate (no release, no merge)."""
    from app.task_completion import _maybe_release_safeguard_gate

    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )

    project = Project(name="C5 critical finding", user_id="u1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    orig = Task(
        project_id=project.id,
        title="orig completed",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        context={},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    safeguard = Task(
        project_id=project.id,
        title="safeguard",
        payloads=[{"type": "security_audit"}],
        status=TaskStatus.COMPLETED,
        context={
            "safeguard_for_task_id": str(orig.id),
            "safeguard_pr_number": 7,
        },
    )
    db_session.add(safeguard)
    await db_session.commit()
    await db_session.refresh(safeguard)

    await _maybe_release_safeguard_gate(
        safeguard, [{"severity": "critical", "title": "rce"}], db_session
    )

    await db_session.refresh(orig)
    assert orig.requires_approval is True, "Gate held on critical finding"
    merge_mock.assert_not_called()
