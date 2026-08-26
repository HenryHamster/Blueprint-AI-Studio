"""Pre-merge rollup child-deliverable gate.

Incident 2026-08-24: rollup PR #17804 merged into ``automain`` having erased
every one of the 12 changed files contributed by child PR #17707 (a conflict
resolution on the rollup branch resolved wholesale toward the base). The loss
was detected only AFTER the merge, when the destroyed diff was already on the
trunk and the sole remaining action was to fail research parent 78ad7812. Two
days later the work was still absent from ``automain``.

Everything here is about doing that comparison BEFORE the merge PUT: hold the
merge, name the files, spawn the repair — and degrade, never wedge.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.models import Project, Task, TaskStatus
from app.rollup_deliverable_guard import (
    DELIVERABLE_REASON_CODE_KEY,
    MUST_PRESERVE_KEY,
    PREMERGE_HOLD_CAP,
    PREMERGE_HOLD_COUNT_KEY,
    RC_INDETERMINATE,
    RC_NOT_A_ROLLUP_MERGE,
    RC_PREMERGE_HOLD,
    RC_PREMERGE_HOLD_CAP_EXHAUSTED,
    RESTORATION_FIX_TASK_KEY,
    build_must_preserve_prompt_block,
    enforce_premerge_rollup_deliverable_guard,
)

ROLLUP_PR = 17804
CHILD_PR = 17707
CHILD_FILES = [
    "backend/app/size_limit_policy.py",
    "backend/testing/test_size_limit_policy.py",
]
# What the rollup PR's diff actually contained after the bad conflict fix:
# not one file of the child's.
ROLLUP_LIVE_FILES = [
    "backend/app/unrelated_feature.py",
    "backend/app/chat_liveness.py",
]


async def _make_project(db_session, name: str) -> Project:
    project = Project(name=name, user_id="test-user-premerge")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_rollup_parent(
    db_session,
    project: Project,
    *,
    rollup_pr_number: int | None = ROLLUP_PR,
    extra_context: dict | None = None,
) -> Task:
    context: dict = {"_is_rollup_review": True}
    if rollup_pr_number is not None:
        context["rollup_pr_number"] = rollup_pr_number
    context.update(extra_context or {})
    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        description="Roll up the discovery cycle's merged children",
        payloads=[{"type": "create", "prompt": "rollup", "base_branch": "automain"}],
        context=context,
        status=TaskStatus.EVALUATING,
        vm_instance_id=f"agent-vm-parent-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    return parent


async def _make_merged_child(
    db_session,
    project: Project,
    parent: Task,
    *,
    files: list[str] = CHILD_FILES,
    pr_number: int = CHILD_PR,
) -> Task:
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="Structural size-limit policy",
        payloads=[{"type": "create", "prompt": "child work", "pr_number": pr_number}],
        context={
            "pr_merged": True,
            "_pr_changed_files": list(files),
            "_pr_changed_files_github_verified": True,
        },
        status=TaskStatus.COMPLETED,
        vm_instance_id=f"agent-vm-child-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)
    return child


def _mock_live_rollup_files(mocker, files: list[str] = ROLLUP_LIVE_FILES):
    return mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": f, "status": "modified"} for f in files]),
    )


def _mock_spawn(mocker, spawned: list):
    async def _spawn(source_task, task_type, _session, pr_number, branch, base_branch, **kwargs):
        row = Task(
            project_id=source_task.project_id,
            parent_task_id=source_task.id,
            title="restoration fix",
            payloads=[{"type": "fix", "prompt": kwargs.get("fix_prompt", "")}],
            status=TaskStatus.QUEUED,
        )
        row.id = uuid.uuid4()
        spawned.append(
            {
                "task_type": task_type,
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "fix_prompt": kwargs.get("fix_prompt", ""),
                "fix_reason": kwargs.get("fix_reason", ""),
                "task": row,
            }
        )
        return row, True

    return mocker.patch("app.review_pipeline._spawn_pipeline_task", side_effect=_spawn)


def _ci_loop(*, conflict_fix: bool = True) -> dict:
    loop: dict = {
        "pr_number": ROLLUP_PR,
        "branch": "research/78ad7812",
        "base_branch": "automain",
        "ci_passed": True,
    }
    if conflict_fix:
        loop["conflict_fix_spawned"] = str(uuid.uuid4())
    return loop


# ---------------------------------------------------------------------------
# The hold itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_premerge_gate_holds_merge_when_child_deliverable_dropped(
    db_session, mocker
):
    """The 2026-08-24 signature, decided BEFORE the merge instead of after.

    A merged child's GitHub-verified files are entirely absent from the LIVE
    rollup diff and a conflict fix ran on this PR. The gate must refuse the
    merge and stamp the machine-readable reason plus the must-preserve receipt
    naming the exact child PR and files.
    """
    project = await _make_project(db_session, "premerge hold")
    parent = await _make_rollup_parent(db_session, project)
    await _make_merged_child(db_session, project, parent)
    _mock_live_rollup_files(mocker)
    spawned: list = []
    _mock_spawn(mocker, spawned)

    verdict = await enforce_premerge_rollup_deliverable_guard(
        parent,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=_ci_loop(),
        branch="research/78ad7812",
        base_branch="automain",
    )

    assert verdict.allowed is False, (
        "a positively verified deliverable drop must HOLD the merge -- "
        "detecting it after the PUT is what made #17804 unrecoverable"
    )
    assert verdict.reason_code == RC_PREMERGE_HOLD
    assert f"PR #{CHILD_PR}" in (verdict.reason or "")

    # The gate stages its receipts; the merge caller owns the commit.
    await db_session.commit()
    await db_session.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get(DELIVERABLE_REASON_CODE_KEY) == RC_PREMERGE_HOLD
    assert ctx.get(PREMERGE_HOLD_COUNT_KEY) == 1
    preserve = ctx.get(MUST_PRESERVE_KEY)
    assert isinstance(preserve, list) and len(preserve) == 1
    assert preserve[0]["pr_number"] == CHILD_PR
    assert CHILD_FILES[0] in preserve[0]["missing_files"]
    assert f"PR #{CHILD_PR}" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_premerge_hold_spawns_restoration_fix_naming_the_files(
    db_session, mocker
):
    """Holding without repairing would only postpone the same loss.

    The hold must spawn the fix that puts the deliverable back, and that fix's
    prompt must name the dropped child PR and its files -- a prompt that only
    says "resolve the conflicts" is what deleted them in the first place.
    """
    project = await _make_project(db_session, "premerge respawn")
    parent = await _make_rollup_parent(db_session, project)
    await _make_merged_child(db_session, project, parent)
    _mock_live_rollup_files(mocker)
    spawned: list = []
    _mock_spawn(mocker, spawned)

    verdict = await enforce_premerge_rollup_deliverable_guard(
        parent,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=_ci_loop(),
        branch="research/78ad7812",
        base_branch="automain",
    )

    assert verdict.allowed is False
    assert len(spawned) == 1, "a held rollup must spawn exactly one restoration fix"
    call = spawned[0]
    assert call["task_type"] == "fix"
    assert call["pr_number"] == ROLLUP_PR
    assert call["branch"] == "research/78ad7812"
    assert call["base_branch"] == "automain"
    assert call["fix_reason"] == "rollup_deliverable_restoration"
    prompt = call["fix_prompt"]
    assert f"PR #{CHILD_PR}" in prompt, (
        "the fix agent cannot restore work it is never told about"
    )
    for path in CHILD_FILES:
        assert path in prompt
    assert "MUST-PRESERVE" in prompt

    # The gate stages its receipts; the merge caller owns the commit.
    await db_session.commit()
    await db_session.refresh(parent)
    assert (parent.context or {}).get(RESTORATION_FIX_TASK_KEY) == str(
        call["task"].id
    )


@pytest.mark.asyncio
async def test_premerge_hold_degrades_at_cap_instead_of_wedging(db_session, mocker):
    """A bounded budget must DECIDE at exhaustion, never escalate or wedge.

    An unbounded hold on an otherwise-mergeable rollup PR is a worse platform
    defect than the loss it prevents. At the cap the gate allows the merge with
    a distinct reason code and leaves the post-merge guard to fail the parent
    with the full receipt.
    """
    project = await _make_project(db_session, "premerge cap")
    parent = await _make_rollup_parent(
        db_session,
        project,
        extra_context={PREMERGE_HOLD_COUNT_KEY: PREMERGE_HOLD_CAP},
    )
    await _make_merged_child(db_session, project, parent)
    _mock_live_rollup_files(mocker)
    spawned: list = []
    _mock_spawn(mocker, spawned)

    verdict = await enforce_premerge_rollup_deliverable_guard(
        parent,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=_ci_loop(),
        branch="research/78ad7812",
        base_branch="automain",
    )

    assert verdict.allowed is True, (
        "the hold budget must degrade to today's behaviour, not strand the PR"
    )
    assert verdict.reason_code == RC_PREMERGE_HOLD_CAP_EXHAUSTED
    assert spawned == [], "no further repair spawns once the budget is spent"
    # The gate stages its receipts; the merge caller owns the commit.
    await db_session.commit()
    await db_session.refresh(parent)
    assert (parent.context or {}).get(
        DELIVERABLE_REASON_CODE_KEY
    ) == RC_PREMERGE_HOLD_CAP_EXHAUSTED


# ---------------------------------------------------------------------------
# Wiring: the gate must find the parent from the row the merge path passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_premerge_gate_resolves_parent_via_original_task_id(
    db_session, mocker
):
    """ci_completion passes the CI row, not the rollup parent.

    Keying the gate off the passed task's own context would make it silently
    no-op on exactly the path that merged #17804. The parent is resolved from
    ``review_loop.original_task_id``.
    """
    project = await _make_project(db_session, "premerge indirect")
    parent = await _make_rollup_parent(db_session, project)
    await _make_merged_child(db_session, project, parent)
    ci_task = Task(
        project_id=project.id,
        title="CI row for the rollup PR",
        payloads=[{"type": "review", "prompt": "ci"}],
        context={},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(ci_task)
    await db_session.commit()
    await db_session.refresh(ci_task)
    _mock_live_rollup_files(mocker)
    _mock_spawn(mocker, [])

    loop = _ci_loop()
    loop["original_task_id"] = str(parent.id)
    verdict = await enforce_premerge_rollup_deliverable_guard(
        ci_task,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=loop,
        branch="research/78ad7812",
        base_branch="automain",
    )

    assert verdict.allowed is False, (
        "the gate must hold when the merge path passes the CI row and names "
        "the rollup parent at review_loop.original_task_id"
    )
    # The gate stages its receipts; the merge caller owns the commit.
    await db_session.commit()
    await db_session.refresh(parent)
    assert (parent.context or {}).get(MUST_PRESERVE_KEY)


# ---------------------------------------------------------------------------
# Fail-open: a gate that false-blocks healthy merges is its own defect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_premerge_gate_is_free_and_open_for_non_rollup_merges(
    db_session, mocker
):
    """An ordinary task PR must not pay for, or be blocked by, this gate."""
    project = await _make_project(db_session, "premerge non-rollup")
    parent = await _make_rollup_parent(db_session, project, rollup_pr_number=None)
    await _make_merged_child(db_session, project, parent)
    files_mock = _mock_live_rollup_files(mocker)

    verdict = await enforce_premerge_rollup_deliverable_guard(
        parent,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=_ci_loop(),
        branch="blueprint/task-1",
        base_branch="automain",
    )

    assert verdict.allowed is True
    assert verdict.reason_code == RC_NOT_A_ROLLUP_MERGE
    files_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_premerge_gate_fails_open_when_verification_raises(
    db_session, mocker
):
    """A crash in the guard must never block a merge."""
    project = await _make_project(db_session, "premerge failopen")
    parent = await _make_rollup_parent(db_session, project)
    await _make_merged_child(db_session, project, parent)
    mocker.patch(
        "app.rollup_deliverable_guard.verify_rollup_child_deliverables",
        new=AsyncMock(side_effect=RuntimeError("github exploded")),
    )

    verdict = await enforce_premerge_rollup_deliverable_guard(
        parent,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=_ci_loop(),
        branch="research/78ad7812",
        base_branch="automain",
    )

    assert verdict.allowed is True
    assert verdict.reason_code == RC_INDETERMINATE


@pytest.mark.asyncio
async def test_premerge_gate_allows_when_no_conflict_fix_evidence(
    db_session, mocker
):
    """Zero overlap WITHOUT a conflict fix on record is ambiguous -> allow.

    The base branch may legitimately have absorbed identical content. The
    post-merge guard still stamps its loud warning receipt; the merge proceeds.
    """
    project = await _make_project(db_session, "premerge ambiguous")
    parent = await _make_rollup_parent(db_session, project)
    await _make_merged_child(db_session, project, parent)
    _mock_live_rollup_files(mocker)
    spawned: list = []
    _mock_spawn(mocker, spawned)

    verdict = await enforce_premerge_rollup_deliverable_guard(
        parent,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=_ci_loop(conflict_fix=False),
        branch="research/78ad7812",
        base_branch="automain",
    )

    assert verdict.allowed is True
    assert spawned == []


@pytest.mark.asyncio
async def test_premerge_gate_allows_when_deliverables_survive(db_session, mocker):
    """The healthy case: overlap present -> merge proceeds, nothing stamped."""
    project = await _make_project(db_session, "premerge intact")
    parent = await _make_rollup_parent(db_session, project)
    await _make_merged_child(db_session, project, parent)
    _mock_live_rollup_files(mocker, [CHILD_FILES[0], "backend/app/other.py"])
    spawned: list = []
    _mock_spawn(mocker, spawned)

    verdict = await enforce_premerge_rollup_deliverable_guard(
        parent,
        db_session,
        pr_number=ROLLUP_PR,
        gh_token="token",
        owner="owner",
        repo="repo",
        ci_loop=_ci_loop(),
        branch="research/78ad7812",
        base_branch="automain",
    )

    assert verdict.allowed is True
    assert spawned == []
    # The gate stages its receipts; the merge caller owns the commit.
    await db_session.commit()
    await db_session.refresh(parent)
    assert MUST_PRESERVE_KEY not in (parent.context or {})


# ---------------------------------------------------------------------------
# The conflict-fix prompt that caused the loss
# ---------------------------------------------------------------------------


def test_must_preserve_block_is_empty_without_a_receipt():
    """Callers concatenate unconditionally, so a clean parent adds nothing."""
    task = Task(title="rollup", payloads=[{}], context={})
    assert build_must_preserve_prompt_block(task) == ""


def test_must_preserve_block_names_pr_and_files_and_forbids_goal_filtering():
    """The block must contradict the instruction that caused the deletion.

    The rollup conflict-fix prompt tells the agent to re-apply only changes
    that serve the rollup goal -- a <=500-char blurb that names none of the
    children's files. Every child file is therefore "unexplained" and, under a
    base-is-source-of-truth rule, deletable. This block must say so explicitly.
    """
    task = Task(
        title="rollup",
        payloads=[{}],
        context={
            MUST_PRESERVE_KEY: [
                {
                    "pr_number": CHILD_PR,
                    "title": "Structural size-limit policy",
                    "missing_files": CHILD_FILES,
                    "file_count": 12,
                }
            ]
        },
    )
    block = build_must_preserve_prompt_block(task)
    assert f"PR #{CHILD_PR}" in block
    for path in CHILD_FILES:
        assert path in block
    assert "10 more file(s)" in block, "the truncated remainder must be counted"
    assert "must NOT drop them for that reason" in block


def test_rollup_conflict_fix_prompt_does_not_make_the_base_authoritative():
    """Root cause pin for the 12/12 revert.

    ``rollup_management`` used to tell the conflict-fix agent that the target
    branch "is the source of truth" and to "re-apply only the changes that
    serve the rollup goal". Applied to a rollup -- whose payload IS several
    children's already-merged diffs -- that instruction authorises deleting all
    of them. Structure and content must be split, and the agent must be told to
    self-check the surviving file list.
    """
    import inspect

    from app import rollup_management

    src = inspect.getsource(rollup_management)
    assert "is the source of truth. Keep its refactors" not in src, (
        "the base-is-source-of-truth conflict-fix instruction is the root of "
        "the #17804 deliverable wipe and must not come back"
    )
    assert "Re-apply only the changes that serve the rollup goal" not in src
    assert "NEVER delete a change just because the goal does not explain" in src
    assert "git diff --name-only origin/{target_branch}...HEAD" in src
    assert "build_must_preserve_prompt_block(parent)" in src
