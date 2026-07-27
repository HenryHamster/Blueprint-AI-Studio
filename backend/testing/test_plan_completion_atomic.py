"""Regression tests for C15: plan-completion atomicity.

Before the fix, _handle_plan_completion committed child tasks with
``depends_on=[]`` in a first commit, then resolved depends_on UUIDs and
committed again.  The window between those two commits let the dispatcher
pick up QUEUED children before their serial ordering was established,
causing parallel execution instead of the plan-specified order.

The fix collapses the three commits into one: flush() to assign PKs,
resolve depends_on in-memory, then a single commit() so children and
their dependency ordering are always durable together.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.knowledge_completion import (
    _handle_design_completion,
    _handle_web_research_completion,
)
from app.models import KnowledgeDocument
from app.compute_experiment_contract import (
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY,
    COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY,
)
from app.models import ProjectComputeImage, Task, TaskHistory, TaskStatus
from app.plan_completion import _handle_plan_completion
from app.task_capabilities import PLAN_KNOWLEDGE_SUBTASK_TYPES, PLAN_SUBTASK_TYPES
from testing.factories import make_project, make_task


@pytest.fixture
def _quiet_mocks(mocker):
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)


async def _setup_plan_task(db_session, mocker, subtasks_json, base_branch="main"):
    """Create a project + plan task, patch _ensure_branch to skip GitHub, and
    return the plan task ready for _handle_plan_completion."""
    project = make_project(name="AtomicPlanTest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Plan: test ordering",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "do things", "base_branch": base_branch}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Patch _ensure_branch (imported lazily from app.task_completion) to
    # return the task without actually calling GitHub.
    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=task,
    )

    return task


@pytest.mark.asyncio
async def test_depends_on_set_atomically_no_empty_window(
    db_session, mocker, _quiet_mocks
):
    """Children persisted with depends_on already populated (C15 regression).

    A plan with subtask B depending on subtask A (via depends_on_indices: [0]).
    After _handle_plan_completion:
    - child A has depends_on == []
    - child B has depends_on == [str(child_A.id)]

    Before the fix, child B was committed with depends_on=[] first, then
    updated in a second commit — creating a window where the dispatcher could
    read B as having no dependencies and dispatch it in parallel with A.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Step A", "prompt": "do step A", "scope": "small"},
                {
                    "title": "Step B",
                    "prompt": "do step B after A",
                    "scope": "small",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 2, f"Expected 2 child tasks, got {len(children)}"

    by_title = {c.title: c for c in children}
    child_a = by_title["Step A"]
    child_b = by_title["Step B"]

    # A has no dependencies.
    assert (child_a.depends_on or []) == [], (
        f"Step A should have no deps, got {child_a.depends_on}"
    )

    # B must depend on A — critically, this must be true as-persisted, not
    # only after a hypothetical second commit.
    assert child_b.depends_on == [str(child_a.id)], (
        f"Step B.depends_on should be [{child_a.id!r}], "
        f"got {child_b.depends_on!r} — atomicity broken"
    )


@pytest.mark.asyncio
async def test_three_step_chain_ordering_is_atomic(db_session, mocker, _quiet_mocks):
    """Three-step serial chain: C depends on B depends on A.

    All three dependency assignments must be durable in the same transaction
    that creates the tasks — no intermediate committed state with empty deps.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Step 1", "prompt": "first", "scope": "small"},
                {
                    "title": "Step 2",
                    "prompt": "second",
                    "scope": "small",
                    "depends_on_indices": [0],
                },
                {
                    "title": "Step 3",
                    "prompt": "third",
                    "scope": "small",
                    "depends_on_indices": [1],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 3

    by_title = {c.title: c for c in children}
    s1 = by_title["Step 1"]
    s2 = by_title["Step 2"]
    s3 = by_title["Step 3"]

    # Verify chain: s1 has no deps, s2 depends on s1, s3 depends on s2.
    assert (s1.depends_on or []) == []
    assert s2.depends_on == [str(s1.id)], (
        f"Step 2 should depend on Step 1, got {s2.depends_on!r}"
    )
    assert s3.depends_on == [str(s2.id)], (
        f"Step 3 should depend on Step 2, got {s3.depends_on!r}"
    )


@pytest.mark.asyncio
async def test_history_records_created_in_same_transaction(
    db_session, mocker, _quiet_mocks
):
    """TaskHistory SPAWNED records must be present immediately after commit.

    Before the fix there were three commits; history was added in the third.
    With the fix, history lands in the single final commit alongside children
    and their depends_on.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Alpha", "prompt": "alpha work", "scope": "small"},
                {
                    "title": "Beta",
                    "prompt": "beta work",
                    "scope": "small",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    child_ids = {c.id for c in children}
    assert len(child_ids) == 2

    # Both children must have a SPAWNED history record.
    history = (
        (
            await db_session.execute(
                select(TaskHistory).where(
                    TaskHistory.task_id.in_(child_ids),
                    TaskHistory.action == "SPAWNED",
                )
            )
        )
        .scalars()
        .all()
    )
    spawned_task_ids = {h.task_id for h in history}
    assert spawned_task_ids == child_ids, (
        f"Expected SPAWNED history for all children {child_ids}, "
        f"but only found records for {spawned_task_ids}"
    )


@pytest.mark.asyncio
async def test_no_dep_subtasks_all_have_empty_depends_on(
    db_session, mocker, _quiet_mocks
):
    """Subtasks without depends_on_indices get empty depends_on (not affected by fix)."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "T1", "prompt": "independent task 1", "scope": "small"},
                {"title": "T2", "prompt": "independent task 2", "scope": "small"},
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 2
    for child in children:
        assert (child.depends_on or []) == [], (
            f"Independent child {child.title!r} should have empty depends_on, "
            f"got {child.depends_on!r}"
        )


@pytest.mark.asyncio
async def test_file_overlap_deps_injected_atomically(db_session, mocker, _quiet_mocks):
    """Auto-injected file-overlap deps are also committed atomically.

    Two subtasks sharing a related_file — _inject_file_overlap_deps adds a
    dep from the second to the first.  This must also be atomically committed
    (same transaction as child creation), not left empty in an intermediate
    committed state.
    """
    shared_file = "backend/app/models.py"
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Edit models first",
                    "prompt": "add column",
                    "scope": "small",
                    "related_files": [shared_file],
                },
                {
                    "title": "Edit models second",
                    "prompt": "add another column",
                    "scope": "small",
                    "related_files": [shared_file],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 2

    by_title = {c.title: c for c in children}
    first = by_title["Edit models first"]
    second = by_title["Edit models second"]

    # File-overlap injection should have added first as a dependency of second.
    assert (first.depends_on or []) == [], (
        f"First child should have no deps, got {first.depends_on!r}"
    )
    assert second.depends_on == [str(first.id)], (
        f"Second child should depend on first (file overlap), got {second.depends_on!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible_type", sorted(PLAN_SUBTASK_TYPES))
async def test_registry_eligible_subtype_is_honored_not_downgraded(
    db_session, mocker, _quiet_mocks, eligible_type
):
    """Every registry plan-eligible write type is honored as a plan child.

    Previously the eligible set was the hardcoded literal
    {create, refactor, migrate, qa}; a plan asking for e.g. ``docs`` or
    ``deps`` was silently rewritten to ``create``. The gate now derives from
    PLAN_SUBTASK_TYPES, so each declared eligible type is spawned AS ITSELF.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": f"Step {eligible_type}",
                    "prompt": "do it",
                    "type": eligible_type,
                }
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, (
        f"Expected 1 child for {eligible_type}, got {len(children)}"
    )
    assert children[0].first_payload.get("type") == eligible_type, (
        f"Child should keep its declared eligible type {eligible_type!r}, "
        f"got {children[0].first_payload.get('type')!r}"
    )


@pytest.mark.asyncio
async def test_non_eligible_subtask_type_is_skipped_not_rewritten(
    db_session, mocker, _quiet_mocks
):
    """A non-coercible ineligible type is SKIPPED; a coercible synonym is coerced.

    ``research`` is non-coercible and non-eligible — it is skipped outright.
    ``fix`` is a coercible synonym for fresh-work: it is coerced to ``create``
    at plan ingestion rather than skipped, so it spawns alongside the eligible
    sibling. Only ``research`` (neither coercible nor eligible) stays out.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Valid", "prompt": "implement", "type": "create"},
                {"title": "Bogus", "prompt": "go research", "type": "research"},
                {"title": "NeedsPR", "prompt": "patch it", "type": "fix"},
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    # The eligible ``create`` and the coerced ``fix`` → ``create`` both spawn.
    assert len(children) == 2, f"Expected 2 spawned children, got {len(children)}"
    by_title = {c.title: c for c in children}
    assert "Valid" in by_title, "Eligible 'create' subtask must spawn"
    assert "NeedsPR" in by_title, "Coerced 'fix' → 'create' subtask must spawn"
    assert "Bogus" not in by_title, "Non-coercible 'research' must not spawn"
    assert by_title["Valid"].first_payload.get("type") == "create"
    assert by_title["NeedsPR"].first_payload.get("type") == "create"
    # The coerced child carries a traceability record.
    coercion = (by_title["NeedsPR"].context or {}).get("type_coercion")
    assert coercion is not None, "Coerced child must record the coercion in context"
    assert coercion["original_type"] == "fix"
    assert coercion["coerced_type"] == "create"
    # No child is typed as research or fix.
    assert not any(c.first_payload.get("type") in {"research", "fix"} for c in children)


@pytest.mark.asyncio
async def test_dependent_on_skipped_producer_fails_plan(
    db_session, mocker, _quiet_mocks
):
    """A dependent whose declared producer is skipped must not dispatch un-gated.

    If subtask B depends on subtask A, and subtask A is non-eligible and skipped,
    then the plan must FAIL rather than spawning B with an empty depends_on list.
    This prevents silent de-gating of structural dependencies.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Foundation (non-eligible)",
                    "prompt": "research something",
                    "type": "research",
                },
                {
                    "title": "Dependent",
                    "prompt": "implement based on foundation",
                    "type": "create",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    # Refresh to see the updated status and message.
    await db_session.refresh(task)

    # Plan must FAIL because a spawned dependent references a skipped producer.
    assert task.status == TaskStatus.FAILED, (
        f"Plan should FAIL when dependent references skipped producer, "
        f"got status {task.status}"
    )
    assert "unsatisfiable dependency" in (task.latest_status_message or "").lower(), (
        f"Expected 'unsatisfiable dependency' in error message, "
        f"got {task.latest_status_message!r}"
    )

    # No children should have been spawned.
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 0, (
        f"No children should spawn when plan fails due to unsatisfiable dependency, "
        f"got {len(children)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "knowledge_type", sorted(PLAN_KNOWLEDGE_SUBTASK_TYPES - {"compute_experiment"})
)
async def test_knowledge_prereq_subtype_spawns_as_itself(
    db_session, mocker, _quiet_mocks, knowledge_type
):
    """A plan subtask of a knowledge-prerequisite type (web_research / design)
    spawns AS ITSELF, carries the parent's direction context, and is NOT given
    the implementation ``create`` prompt suffix.

    These types produce a durable knowledge document, not a branch/PR, so they
    must become spawnable plan children without being treated as PR-producing
    implementation subtypes.
    """
    # Configure a create suffix so we can assert the knowledge child does NOT
    # receive it (the suffix is for code/PR work only).
    from app.shared_state import runtime_config

    runtime_config["agent_prompt_suffixes"] = {"create": "IMPLEMENTATION SUFFIX"}

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": f"Knowledge {knowledge_type}",
                    "prompt": "gather the prerequisite knowledge",
                    "type": knowledge_type,
                }
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    # Thread direction context onto the parent plan, as the real pipeline does.
    task.context = {
        **(task.context or {}),
        "direction_id": "dir-knowledge-1",
        "research_task_id": "res-knowledge-1",
    }
    db_session.add(task)
    await db_session.commit()

    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, (
        f"Expected 1 child for {knowledge_type}, got {len(children)}"
    )
    child = children[0]
    # Spawned as the knowledge type, not downgraded to create.
    assert child.first_payload.get("type") == knowledge_type, (
        f"Knowledge child should keep type {knowledge_type!r}, "
        f"got {child.first_payload.get('type')!r}"
    )
    # No implementation prompt suffix for a non-PR producer.
    assert "prompt_suffix" not in child.first_payload, (
        f"Knowledge child must not receive the create prompt_suffix, "
        f"got {child.first_payload.get('prompt_suffix')!r}"
    )
    # Direction context threaded to the child like every other plan child.
    assert (child.context or {}).get("direction_id") == "dir-knowledge-1"
    assert (child.context or {}).get("research_task_id") == "res-knowledge-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "knowledge_type", sorted(PLAN_KNOWLEDGE_SUBTASK_TYPES - {"compute_experiment"})
)
async def test_plan_knowledge_child_carries_finding_provenance(
    db_session, mocker, _quiet_mocks, knowledge_type
):
    """Plan-spawned knowledge subtasks preserve research-finding provenance.

    The child must inherit the parent plan's finding metadata so the durable
    KnowledgeDocument can retain the research/challenge origin instead of
    collapsing to unchallenged.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": f"Knowledge {knowledge_type}",
                    "prompt": "gather the prerequisite knowledge",
                    "type": knowledge_type,
                }
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    task.context = {
        **(task.context or {}),
        "direction_id": "dir-finding-1",
        "research_task_id": "res-plan-1",
        "parent_research_task_id": "res-origin-1",
        "finding_index": 3,
        "finding_hash": "finding-hash-abc123",
        "finding_challenge": {
            "challenged_boundary": "reproduce_absent",
            "first_verdict": "absent",
            "second_opinion_verdict": "present",
            "agreement": "disagrees",
            "reason": "The cited behavior moved but still reproduces.",
            "sidecar_task_id": "sidecar-1",
        },
    }
    db_session.add(task)
    await db_session.commit()

    await _handle_plan_completion(task, plan_logs, db_session)

    child = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .one()
    )
    assert (child.context or {}).get("research_task_id") == "res-plan-1"
    assert (child.context or {}).get("parent_research_task_id") == "res-origin-1"
    assert (child.context or {}).get("finding_index") == 3
    assert (child.context or {}).get("finding_hash") == "finding-hash-abc123"
    assert (child.context or {}).get("finding_challenge", {}).get("agreement") == (
        "disagrees"
    )

    if knowledge_type == "design":
        document = {
            "title": "Design prerequisite doc",
            "summary": "Challenge-aware design context.",
            "body": "# Body\n\nOrigin details.",
            "citations": ["docs/design-prereq.md"],
            "tags": ["origin"],
            "topic": "origin",
        }
        handler = _handle_design_completion
    else:
        document = {
            "title": "Research prerequisite doc",
            "summary": "Challenge-aware research context.",
            "body": "# Body\n\nOrigin details.",
            "citations": ["https://example.com/origin"],
            "tags": ["origin"],
            "topic": "origin",
        }
        handler = _handle_web_research_completion

    await handler(child, json.dumps(document), db_session)

    doc = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == child.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert doc.origin_state == "research_finding"
    assert doc.origin_research_task_id == "res-plan-1"
    assert doc.origin_parent_research_task_id == "res-origin-1"
    assert doc.origin_finding_index == 3
    assert doc.origin_finding_hash == "finding-hash-abc123"
    assert doc.origin_challenge_state == "overturned"


@pytest.mark.asyncio
async def test_compute_experiment_prereq_spawns_launch_payload_and_dependency(
    db_session, mocker, _quiet_mocks
):
    """A plan may spawn a compute_experiment prerequisite as a non-PR producer.

    The child payload must carry the authored launch contract, omit the create
    prompt suffix, and stamp prerequisite mode so completion treats the success
    criterion as advisory while downstream dependents still wait on its task ID.
    """
    from app.shared_state import runtime_config

    runtime_config["agent_prompt_suffixes"] = {"create": "IMPLEMENTATION SUFFIX"}
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Measure the baseline",
                    "prompt": "benchmark the current implementation",
                    "type": "compute_experiment",
                    "command": "python bench.py --mode baseline",
                    "spend_cap_usd": 3.5,
                    "deadline_seconds": 900,
                    "success_criterion": {
                        "metric": "accuracy",
                        "operator": ">=",
                        "threshold": 0.9,
                    },
                    "machine_type": "n2-standard-4",
                    "experiment": "baseline-accuracy",
                    "benchmark": "suite-v1",
                    "metric": "accuracy",
                    "cwd": "backend",
                    "env": {"BENCHMARK_MODE": "baseline"},
                    "accelerator": "T4",
                    "provisioning_model": "spot",
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                },
                {
                    "title": "Implement against the measurement",
                    "prompt": "use the measured result",
                    "type": "create",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 2
    by_title = {c.title: c for c in children}
    producer = by_title["Measure the baseline"]
    dependent = by_title["Implement against the measurement"]

    payload = producer.first_payload or {}
    assert payload["type"] == "compute_experiment"
    assert payload["command"] == "python bench.py --mode baseline"
    assert payload["spend_cap_usd"] == pytest.approx(3.5)
    assert payload["deadline_seconds"] == 900
    assert payload["success_criterion"]["metric"] == "accuracy"
    assert payload["machine_type"] == "n2-standard-4"
    assert payload["experiment"] == "baseline-accuracy"
    assert payload["benchmark"] == "suite-v1"
    assert payload["metric"] == "accuracy"
    assert payload["cwd"] == "backend"
    assert payload["env"] == {"BENCHMARK_MODE": "baseline"}
    assert payload["accelerator"] == "T4"
    assert payload["provisioning_model"] == "spot"
    assert "prompt_suffix" not in payload
    assert "prompt" not in payload
    assert (producer.context or {})[
        COMPUTE_EXPERIMENT_PREREQUISITE_MODE_CONTEXT_KEY
    ] == COMPUTE_EXPERIMENT_PREREQUISITE_MODE_ADVISORY
    assert dependent.depends_on == [str(producer.id)]


async def _setup_plan_task_with_project(
    db_session, mocker, subtasks_json, *, build_config=None, base_branch="main"
):
    """Like ``_setup_plan_task`` but returns ``(task, project)`` and lets the
    caller seed ``Project.build_config`` (for baked compute-image defaulting)."""
    project = make_project(name="ImgDefaultPlan", build_config=build_config)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Plan: image defaulting",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "x", "base_branch": base_branch}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=task,
    )
    return task, project


def _compute_prereq_plan_logs():
    return json.dumps(
        {
            "subtasks": [
                {
                    "title": "Measure the baseline",
                    "prompt": "benchmark it",
                    "type": "compute_experiment",
                    "command": "python bench.py",
                    "spend_cap_usd": 1.0,
                    # Required by _validate_compute_launch_contract — without it the
                    # contract is invalid, the subtask is skipped at the plan gate,
                    # and NO child spawns (every other compute_experiment fixture in
                    # this file declares it).
                    "deadline_seconds": 600,
                    "success_criterion": {
                        "metric": "accuracy",
                        "operator": ">=",
                        "threshold": 0.9,
                    },
                    "machine_type": "n2-standard-4",
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                }
            ]
        }
    )


_PLAN_MANIFEST_TAG = "registry.example.com/proj/compute:latest"
_PLAN_PINNED_REF = "registry.example.com/proj/compute@sha256:" + ("b" * 64)


@pytest.mark.asyncio
async def test_plan_compute_experiment_defaults_to_pinned_ref(
    db_session, mocker, _quiet_mocks
):
    """A plan-spawned compute_experiment with no declared container_image
    inherits the project's digest-pinned baked reference (preferred over the
    mutable manifest tag).

    Production resolves the default via
    ``compute_backends.resolve_digest_pinned_default_image``, which pins the
    mutable manifest tag to the ``pinned_ref`` of the matching built
    ``ProjectComputeImage`` row — so the reproducibility win is exercised by
    seeding that durable build record, not ``Project.build_config``."""
    task, project = await _setup_plan_task_with_project(
        db_session,
        mocker,
        _compute_prereq_plan_logs(),
    )
    # image_name is derived from the manifest tag exactly as the resolver does:
    # "registry.example.com/proj/compute:latest" -> "compute".
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="compute",
            registry_repo="registry.example.com/proj",
            dep_hash="deadbeef",
            tag="latest",
            pinned_ref=_PLAN_PINNED_REF,
            digest="sha256:" + ("b" * 64),
            built=True,
        )
    )
    await db_session.commit()
    mocker.patch(
        "app.routers.admin_project_tools._discover_manifest_tools",
        new_callable=AsyncMock,
        return_value={
            "compute": {
                "image": {
                    "name": _PLAN_MANIFEST_TAG,
                    # A build recipe declares this as a Blueprint-managed image.
                    # Buildless images are external/prebuilt and intentionally
                    # preserve their declared ref instead of consulting build rows.
                    "build": {"dockerfile": "Dockerfile"},
                }
            }
        },
    )

    await _handle_plan_completion(task, _compute_prereq_plan_logs(), db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    payload = children[0].first_payload or {}
    assert payload["container_image"] == _PLAN_PINNED_REF


@pytest.mark.asyncio
async def test_plan_compute_experiment_falls_back_to_manifest_tag(
    db_session, mocker, _quiet_mocks
):
    """With no baked pinned_ref, the plan-spawned compute_experiment still
    inherits the mutable manifest tag exactly as before (backward compat)."""
    task, _project = await _setup_plan_task_with_project(
        db_session,
        mocker,
        _compute_prereq_plan_logs(),
        build_config=None,
    )
    mocker.patch(
        "app.routers.admin_project_tools._discover_manifest_tools",
        new_callable=AsyncMock,
        return_value={"compute": {"image": {"name": _PLAN_MANIFEST_TAG}}},
    )

    await _handle_plan_completion(task, _compute_prereq_plan_logs(), db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    payload = children[0].first_payload or {}
    assert payload["container_image"] == _PLAN_MANIFEST_TAG


@pytest.mark.asyncio
async def test_compute_experiment_prereq_propagates_live_proof_contract(
    db_session, mocker, _quiet_mocks
):
    """A live-proof compute_experiment subtask keeps its proof markers on the child.

    The plan-completion child payload must carry the proof marker fields through
    to first_payload so the agent-side launcher can enforce the live-release
    precondition instead of launching the job as ordinary compute.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Prove the release is serving",
                    "prompt": "launch a live-proof compute job",
                    "type": "compute_experiment",
                    "command": "python live_proof.py",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                    "live_proof": True,
                    "live_proof_marker": "plan-live-proof",
                    "live_release_proof": {
                        "enabled": True,
                        "marker": "plan-live-proof",
                    },
                    "live_release_proof_marker": "plan-live-proof",
                    "requires_live_release": True,
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                }
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    payload = children[0].first_payload or {}
    assert payload["type"] == "compute_experiment"
    assert payload["live_proof"] is True
    assert payload["live_proof_marker"] == "plan-live-proof"
    assert payload["live_release_proof"] == {
        "enabled": True,
        "marker": "plan-live-proof",
    }
    assert payload["live_release_proof_marker"] == "plan-live-proof"
    assert payload["requires_live_release"] is True


@pytest.mark.asyncio
async def test_compute_experiment_require_durable_artifact_forwarded_to_payload(
    db_session, mocker, _quiet_mocks
):
    """A strict compute_experiment subtask carries its durable-artifact contract.

    ``require_durable_artifact`` and ``hypothesis`` are declared on the subtask
    definition and must travel through the launch-contract assembler onto the
    spawned child's first_payload, where the completion gate can enforce the
    strict durable-delivery requirement. Experiments that omit the flag remain
    fail-soft (no field is stamped).
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Run the strict experiment",
                    "prompt": "launch a durable-artifact compute job",
                    "type": "compute_experiment",
                    "command": "python strict_bench.py",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                    "require_durable_artifact": True,
                    "hypothesis": "the optimized kernel beats the baseline by 10%",
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                },
                {
                    "title": "Run the fail-soft experiment",
                    "prompt": "launch an ordinary compute job",
                    "type": "compute_experiment",
                    "command": "python soft_bench.py",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 2
    by_title = {c.title: c for c in children}

    strict_payload = by_title["Run the strict experiment"].first_payload or {}
    assert strict_payload["type"] == "compute_experiment"
    assert strict_payload["require_durable_artifact"] is True
    assert strict_payload["hypothesis"] == (
        "the optimized kernel beats the baseline by 10%"
    )

    soft_payload = by_title["Run the fail-soft experiment"].first_payload or {}
    assert soft_payload["type"] == "compute_experiment"
    assert "require_durable_artifact" not in soft_payload
    assert "hypothesis" not in soft_payload


@pytest.mark.asyncio
async def test_implementation_child_can_depend_on_knowledge_prereq(
    db_session, mocker, _quiet_mocks
):
    """An implementation child may depend on a knowledge-prerequisite producer.

    web_research produces the knowledge a downstream ``create`` needs. Because
    the knowledge type is now a valid plan child (not skipped), the dependent
    create spawns with depends_on pointing at the producer — and the plan does
    NOT fail with an unsatisfiable-dependency error.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Research the vendor API",
                    "prompt": "learn the vendor protocol",
                    "type": "web_research",
                },
                {
                    "title": "Implement the integration",
                    "prompt": "build it on the research",
                    "type": "create",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    await db_session.refresh(task)
    assert task.status != TaskStatus.FAILED, (
        f"Plan must not fail when a create depends on a knowledge prereq, "
        f"got status {task.status} / {task.latest_status_message!r}"
    )

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 2, f"Expected 2 children, got {len(children)}"
    by_title = {c.title: c for c in children}
    producer = by_title["Research the vendor API"]
    dependent = by_title["Implement the integration"]

    assert producer.first_payload.get("type") == "web_research"
    assert dependent.depends_on == [str(producer.id)], (
        f"Dependent create should depend on the knowledge producer, "
        f"got {dependent.depends_on!r}"
    )


@pytest.mark.asyncio
async def test_invalid_compute_prereq_contract_fails_when_kept_subtask_depends_on_it(
    db_session, mocker, _quiet_mocks
):
    """An invalid compute prerequisite is skipped, and dependents fail cleanly."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Broken measurement",
                    "prompt": "measure first",
                    "type": "compute_experiment",
                    "command": "   ",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                },
                {
                    "title": "Implement using the measurement",
                    "prompt": "use its result",
                    "type": "create",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)
    await db_session.refresh(task)

    assert task.status == TaskStatus.FAILED
    msg = task.latest_status_message or ""
    assert "unsatisfiable dependency" in msg.lower()
    assert "invalid compute_experiment launch contract" in msg
    assert "missing or empty command" in msg
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert children == []


@pytest.mark.asyncio
async def test_plan_exceeding_subtask_cap_fails_with_record(
    db_session, mocker, _quiet_mocks
):
    """A plan with more than 10 subtasks fails with a durable record of what was dropped.

    Rather than silently truncating at 10 and losing the remaining subtasks,
    the plan must FAIL and persist a subtask_cap_exceeded record containing:
    - The total subtask count
    - The dropped indices and titles
    - Any unsatisfiable dependencies on dropped subtasks
    """
    # Create 12 subtasks to exceed the cap.
    subtasks_list = [
        {"title": f"Step {i}", "prompt": f"do step {i}", "scope": "small"}
        for i in range(12)
    ]
    plan_logs = json.dumps({"subtasks": subtasks_list})

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    await db_session.refresh(task)

    # Plan must FAIL due to exceeding the subtask cap.
    assert task.status == TaskStatus.FAILED, (
        f"Plan should FAIL when exceeding subtask cap, got status {task.status}"
    )

    # Error message should mention the cap and dropped count.
    assert "exceed" in (task.latest_status_message or "").lower(), (
        f"Expected 'exceed' in error message, got {task.latest_status_message!r}"
    )
    assert "12" in (task.latest_status_message or ""), (
        f"Expected total count '12' in error message, got {task.latest_status_message!r}"
    )
    assert "2" in (task.latest_status_message or ""), (
        f"Expected dropped count '2' in error message, got {task.latest_status_message!r}"
    )

    # Context must record dropped subtasks durably.
    cap_record = (task.context or {}).get("subtask_cap_exceeded")
    assert cap_record, (
        f"subtask_cap_exceeded must be recorded on task.context, got {cap_record!r}"
    )
    assert cap_record.get("total_subtasks") == 12, (
        f"Record must capture total subtask count, got {cap_record.get('total_subtasks')!r}"
    )
    assert cap_record.get("dropped_count") == 2, (
        f"Record must capture dropped count, got {cap_record.get('dropped_count')!r}"
    )
    assert cap_record.get("dropped_indices") == [10, 11], (
        f"Record must list dropped indices [10, 11], got {cap_record.get('dropped_indices')!r}"
    )

    # No children should have been spawned.
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 0, (
        f"No children should spawn when plan exceeds subtask cap, got {len(children)}"
    )


@pytest.mark.asyncio
async def test_kept_subtask_depending_on_dropped_subtask_fails_plan(
    db_session, mocker, _quiet_mocks
):
    """A kept subtask with a broken dependency on a dropped subtask causes plan failure.

    This catches the scenario where a subtask references a dropped subtask in its
    depends_on_indices. The plan must FAIL rather than silently removing the dependency
    edge (which would cause premature dispatch). The broken dependency must be recorded
    in the context.
    """
    # Create 11 subtasks: steps 0-9 kept, step 10 dropped.
    # Step 8 depends on step 10 (which will be dropped).
    subtasks_list = [
        {"title": "Step 0", "prompt": "step 0", "scope": "small"},
        {"title": "Step 1", "prompt": "step 1", "scope": "small"},
        {"title": "Step 2", "prompt": "step 2", "scope": "small"},
        {"title": "Step 3", "prompt": "step 3", "scope": "small"},
        {"title": "Step 4", "prompt": "step 4", "scope": "small"},
        {"title": "Step 5", "prompt": "step 5", "scope": "small"},
        {"title": "Step 6", "prompt": "step 6", "scope": "small"},
        {"title": "Step 7", "prompt": "step 7", "scope": "small"},
        {
            "title": "Step 8",
            "prompt": "step 8 depends on dropped",
            "scope": "small",
            "depends_on_indices": [10],  # Broken: index 10 will be dropped
        },
        {"title": "Step 9", "prompt": "step 9", "scope": "small"},
        {"title": "Step 10 (dropped)", "prompt": "step 10", "scope": "small"},
    ]
    plan_logs = json.dumps({"subtasks": subtasks_list})

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    await db_session.refresh(task)

    # Plan must FAIL due to exceeding subtask cap + broken dependency.
    assert task.status == TaskStatus.FAILED, (
        f"Plan should FAIL when cap exceeded with broken dependencies, "
        f"got status {task.status}"
    )

    # Error message should mention unsatisfiable dependencies.
    msg = task.latest_status_message or ""
    assert "unsatisfiable" in msg.lower() or "exceed" in msg.lower(), (
        f"Expected 'unsatisfiable' or 'exceed' in error message, got {msg!r}"
    )

    # Context must record both the cap exceeded and the broken dependencies.
    cap_record = (task.context or {}).get("subtask_cap_exceeded")
    assert cap_record, f"subtask_cap_exceeded must be recorded, got {cap_record!r}"

    # The broken dependency must be documented.
    broken_deps = cap_record.get("broken_dependencies", [])
    assert len(broken_deps) >= 1, (
        f"Must record at least one broken dependency, got {broken_deps!r}"
    )

    # Verify the broken dependency record details.
    step_8_dep = next(
        (d for d in broken_deps if d.get("subtask_title") == "Step 8"),
        None,
    )
    assert step_8_dep, (
        f"Step 8's broken dependency must be recorded, got {broken_deps!r}"
    )
    assert step_8_dep.get("broken_dep_index") == 10, (
        f"Broken dependency should reference dropped index 10, "
        f"got {step_8_dep.get('broken_dep_index')!r}"
    )
    assert step_8_dep.get("broken_dep_title") == "Step 10 (dropped)", (
        f"Broken dependency should reference dropped title, "
        f"got {step_8_dep.get('broken_dep_title')!r}"
    )

    # No children should have been spawned.
    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 0, (
        f"No children should spawn when broken dependencies exist, got {len(children)}"
    )


@pytest.mark.asyncio
async def test_kept_subtask_depending_on_out_of_range_index_fails_gracefully(
    db_session, mocker, _quiet_mocks
):
    """A kept subtask depending on an index beyond the array must not crash.

    Regression: a plausible LLM miscount can produce a depends_on_indices entry
    that points past the end of the subtasks list (e.g. [12] in a 12-subtask
    plan). The cap path's broken-dependency title lookup must NOT IndexError on
    such an entry — doing so would abort _handle_plan_completion before the
    FAILED status and subtask_cap_exceeded record commit, leaving the plan stuck.
    The dependency is still unsatisfiable, so it must be recorded (with a safe
    fallback title) and the plan must FAIL cleanly.
    """
    # 12 subtasks: steps 0-9 kept, steps 10-11 dropped. Step 0 declares a
    # dependency on index 12, which is beyond the end of the 12-element list.
    subtasks_list = [
        {
            "title": "Step 0",
            "prompt": "step 0 depends on out-of-range index",
            "scope": "small",
            "depends_on_indices": [12],  # Out of range: len(subtasks) == 12
        },
    ]
    for i in range(1, 12):
        subtasks_list.append(
            {"title": f"Step {i}", "prompt": f"step {i}", "scope": "small"}
        )
    plan_logs = json.dumps({"subtasks": subtasks_list})

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    # Must not raise IndexError.
    await _handle_plan_completion(task, plan_logs, db_session)

    await db_session.refresh(task)

    # Plan must FAIL cleanly (not stuck) due to exceeding the cap.
    assert task.status == TaskStatus.FAILED, (
        f"Plan should FAIL cleanly when cap exceeded with out-of-range dep, "
        f"got status {task.status}"
    )

    # The cap record and the broken dependency must still be committed.
    cap_record = (task.context or {}).get("subtask_cap_exceeded")
    assert cap_record, f"subtask_cap_exceeded must be recorded, got {cap_record!r}"

    broken_deps = cap_record.get("broken_dependencies", [])
    step_0_dep = next(
        (d for d in broken_deps if d.get("subtask_title") == "Step 0"),
        None,
    )
    assert step_0_dep, (
        f"Step 0's out-of-range dependency must be recorded, got {broken_deps!r}"
    )
    assert step_0_dep.get("broken_dep_index") == 12, (
        f"Broken dependency should reference index 12, "
        f"got {step_0_dep.get('broken_dep_index')!r}"
    )
    # Safe fallback title for the out-of-range index.
    assert step_0_dep.get("broken_dep_title") == "#12", (
        f"Out-of-range dependency should use safe fallback title '#12', "
        f"got {step_0_dep.get('broken_dep_title')!r}"
    )


@pytest.mark.asyncio
async def test_injection_cycle_drops_only_offending_edge_keeps_rest(
    db_session, mocker, _quiet_mocks
):
    """Post-injection cycle removes only the offending injected edge.

    Subtask A explicitly declares a forward dependency on subtask B, and A and B
    share a file — so file-overlap injection adds B→A, forming a 2-cycle with
    A→B. A second, UNRELATED same-file pair (C, D) gets its own conflict-
    prevention edge D→C that has nothing to do with the cycle.

    The old behaviour wholesale-discarded EVERY injected edge, so D→C was lost
    and C/D could dispatch in parallel and conflict. The fix must drop ONLY the
    cycle-closing injected edge (B→A) while preserving the unrelated D→C
    serialization, and record what was dropped on task.context.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "A",
                    "prompt": "edit shared foo",
                    "scope": "small",
                    "related_files": ["foo.py"],
                    "depends_on_indices": [1],  # explicit forward reference to B
                },
                {
                    "title": "B",
                    "prompt": "edit shared foo too",
                    "scope": "small",
                    "related_files": ["foo.py"],  # shared with A → injects B→A
                },
                {
                    "title": "C",
                    "prompt": "edit shared bar",
                    "scope": "small",
                    "related_files": ["bar.py"],
                },
                {
                    "title": "D",
                    "prompt": "edit shared bar too",
                    "scope": "small",
                    "related_files": ["bar.py"],  # shared with C → injects D→C
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 4
    by_title = {c.title: c for c in children}
    a, b, c, d = (by_title["A"], by_title["B"], by_title["C"], by_title["D"])

    # Explicit edge A→B survives; cycle-closing injected edge B→A is dropped.
    assert a.depends_on == [str(b.id)], (
        f"Explicit A→B must survive, got {a.depends_on!r}"
    )
    assert (b.depends_on or []) == [], (
        f"Cycle-closing injected B→A must be dropped, got {b.depends_on!r}"
    )

    # Unrelated conflict-prevention edge D→C must be PRESERVED (not wholesale-dropped).
    assert (c.depends_on or []) == [], f"C should have no deps, got {c.depends_on!r}"
    assert d.depends_on == [str(c.id)], (
        f"Unrelated injected D→C must survive the cycle break, got {d.depends_on!r}"
    )

    # Resolution is observable beyond a bare error log.
    await db_session.refresh(task)
    dropped = (task.context or {}).get("dropped_serialization_edges")
    assert dropped, f"Dropped edges must be recorded on task.context, got {dropped!r}"
    assert any(e["successor"] == "B" and e["predecessor"] == "A" for e in dropped), (
        f"Dropped record must name the B→A edge, got {dropped!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Acceptance-evidence propagation (plan decomposition → child payload + context)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_acceptance_evidence_propagated_to_child_payload(
    db_session, mocker, _quiet_mocks
):
    """A subtask's declared acceptance_evidence lands on its child payload.

    A plan declaring a well-formed named_test acceptance_evidence on a subtask
    must propagate it verbatim onto the spawned child task's first_payload,
    validated by the child payload schema's AcceptanceEvidence model.
    """
    evidence = {
        "kind": "named_test",
        "test_path": "testing/test_foo.py",
        "test_id": "test_seam_behavior",
    }
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: implement seam",
                    "prompt": "implement the seam",
                    "scope": "small",
                    "type": "create",
                    "acceptance_evidence": evidence,
                }
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, f"Expected 1 child, got {len(children)}"
    child_evidence = children[0].first_payload.get("acceptance_evidence")
    assert child_evidence is not None, "acceptance_evidence missing from child payload"
    assert child_evidence["kind"] == "named_test"
    assert child_evidence["test_path"] == "testing/test_foo.py"
    assert child_evidence["test_id"] == "test_seam_behavior"


@pytest.mark.asyncio
async def test_acceptance_evidence_absent_not_synthesized(
    db_session, mocker, _quiet_mocks
):
    """A subtask with no acceptance_evidence spawns a child without the key.

    Absence must change nothing — the key stays out of the child payload; the
    backend never synthesizes one.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: no evidence",
                    "prompt": "do it",
                    "scope": "small",
                    "type": "create",
                }
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, f"Expected 1 child, got {len(children)}"
    assert "acceptance_evidence" not in children[0].first_payload, (
        "acceptance_evidence must not be synthesized when absent, got "
        f"{children[0].first_payload.get('acceptance_evidence')!r}"
    )


@pytest.mark.asyncio
async def test_malformed_acceptance_evidence_degrades_not_aborts_spawn(
    db_session, mocker, _quiet_mocks
):
    """A malformed acceptance_evidence must NOT abort the whole decomposition.

    An LLM-generated declaration is untrusted. A malformed one (here: a
    named_test pointing at a non-test path) must degrade to 'no declaration' on
    its own child — the key is simply omitted — while every other subtask spawns
    normally. The pre-validation must never raise out of the spawn loop, which
    would roll back EVERY child and lose the decomposition permanently.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: malformed evidence",
                    "prompt": "implement A",
                    "scope": "small",
                    "type": "create",
                    # Not a test file → AcceptanceEvidence validation fails.
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": "app/handler.py",
                    },
                },
                {
                    "title": "feat: well-formed sibling",
                    "prompt": "implement B",
                    "scope": "small",
                    "type": "create",
                    "acceptance_evidence": {
                        "kind": "named_test",
                        "test_path": "testing/test_bar.py",
                        "test_id": "test_sibling_behavior",
                    },
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    # Both children survive — the malformed declaration did not abort the batch.
    assert len(children) == 2, f"Expected 2 children, got {len(children)}"
    by_title = {c.title: c for c in children}
    malformed_child = by_title["feat: malformed evidence"]
    sibling_child = by_title["feat: well-formed sibling"]
    # Malformed declaration degraded to 'no declaration' (key omitted).
    assert "acceptance_evidence" not in malformed_child.first_payload, (
        "malformed acceptance_evidence must degrade to omitted, got "
        f"{malformed_child.first_payload.get('acceptance_evidence')!r}"
    )
    # The well-formed sibling still carries its declaration verbatim.
    sibling_evidence = sibling_child.first_payload.get("acceptance_evidence")
    assert sibling_evidence is not None, "well-formed sibling lost its evidence"
    assert sibling_evidence["test_path"] == "testing/test_bar.py"


# ─────────────────────────────────────────────────────────────────────────────
# Acceptance-evidence propagation (plan decomposition → child CONTEXT)
# The lighter {kind, descriptor, selector} proof descriptor the agent renders.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_subtask_acceptance_evidence_propagates_to_child_context(
    db_session, mocker, _quiet_mocks
):
    """A well-formed subtask acceptance_evidence round-trips onto child context."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Step A",
                    "prompt": "do step A",
                    "scope": "small",
                    "acceptance_evidence": {
                        "kind": "test",
                        "descriptor": "seam test exercises new path end-to-end",
                        "selector": "testing/test_foo.py::test_bar",
                    },
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert children[0].context["acceptance_evidence"] == {
        "kind": "test",
        "descriptor": "seam test exercises new path end-to-end",
        "selector": "testing/test_foo.py::test_bar",
    }


@pytest.mark.asyncio
async def test_plan_subtask_absent_acceptance_evidence_is_noop(
    db_session, mocker, _quiet_mocks
):
    """No declaration ⇒ child context carries no acceptance_evidence key at all."""
    plan_logs = json.dumps(
        {"subtasks": [{"title": "Step A", "prompt": "do step A", "scope": "small"}]}
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert "acceptance_evidence" not in (children[0].context or {})


# ─────────────────────────────────────────────────────────────────────────────
# Acceptance-evidence forward-carry through decomposition (PARENT → child)
# When the plan agent does not re-declare per-subtask evidence, the parent
# finding's declared acceptance evidence must still survive one hop onto the
# child/children that deliver the declared seam.
# ─────────────────────────────────────────────────────────────────────────────


async def _setup_plan_task_with_evidence(
    db_session,
    mocker,
    *,
    payload_evidence=None,
    context_evidence=None,
):
    """Build a plan task that carries the parent finding's declared acceptance
    evidence on the payload and/or context channel, as research_completion does
    when it routes a finding through the plan (decomposition) agent."""
    project = make_project(name="ParentEvidenceTest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = {"type": "plan", "prompt": "do things", "base_branch": "main"}
    if payload_evidence is not None:
        payload["acceptance_evidence"] = payload_evidence

    task = make_task(
        project_id=project.id,
        title="Plan: carry evidence",
        status=TaskStatus.IN_PROGRESS,
        payloads=[payload],
        context=(
            {"acceptance_evidence": context_evidence}
            if context_evidence is not None
            else {}
        ),
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=task,
    )
    return task


async def _children_of(db_session, task):
    return (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_parent_acceptance_evidence_survives_decomposition(
    db_session, mocker, _quiet_mocks
):
    """The parent finding's declared payload acceptance_evidence lands on a child
    that delivers the seam and did not self-declare.

    This is the core decomposition-hop guarantee: a finding that carefully
    declares 'prove this with test T' must not lose T when the plan agent omits
    a per-subtask declaration. The child whose related_files reference the
    declared test path inherits the parent's evidence onto its payload.
    """
    parent_evidence = {
        "kind": "named_test",
        "test_path": "backend/testing/test_seam.py",
        "test_id": "test_seam_end_to_end",
    }
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: deliver the seam",
                    "prompt": "implement the seam end-to-end",
                    "scope": "small",
                    "type": "create",
                    "related_files": ["backend/testing/test_seam.py"],
                },
            ]
        }
    )

    task = await _setup_plan_task_with_evidence(
        db_session, mocker, payload_evidence=parent_evidence
    )
    await _handle_plan_completion(task, plan_logs, db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1, f"Expected 1 child, got {len(children)}"
    child_ev = children[0].first_payload.get("acceptance_evidence")
    assert child_ev is not None, (
        "parent acceptance_evidence must survive decomposition onto the "
        "delivering child's payload"
    )
    assert child_ev["kind"] == "named_test"
    assert child_ev["test_path"] == "backend/testing/test_seam.py"
    assert child_ev["test_id"] == "test_seam_end_to_end"


@pytest.mark.asyncio
async def test_parent_context_acceptance_evidence_survives_decomposition(
    db_session, mocker, _quiet_mocks
):
    """The parent's context-channel PlanAcceptanceEvidence carries onto the
    delivering child's context when the subtask does not self-declare."""
    context_evidence = {
        "kind": "test",
        "descriptor": "seam test exercises the new path end-to-end",
        "selector": "backend/testing/test_seam.py::test_seam",
    }
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: deliver the seam",
                    "prompt": "implement it",
                    "scope": "small",
                    "type": "create",
                    "related_files": ["backend/testing/test_seam.py"],
                },
            ]
        }
    )

    task = await _setup_plan_task_with_evidence(
        db_session, mocker, context_evidence=context_evidence
    )
    await _handle_plan_completion(task, plan_logs, db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1
    assert children[0].context.get("acceptance_evidence") == context_evidence


@pytest.mark.asyncio
async def test_subtask_own_acceptance_evidence_not_overwritten_by_parent(
    db_session, mocker, _quiet_mocks
):
    """A subtask that declares its OWN evidence keeps it — the parent's declared
    evidence never overwrites a self-declared subtask."""
    parent_evidence = {
        "kind": "named_test",
        "test_path": "backend/testing/test_parent.py",
        "test_id": "test_parent_seam",
    }
    own_evidence = {
        "kind": "named_test",
        "test_path": "backend/testing/test_child.py",
        "test_id": "test_child_seam",
    }
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: self-declares",
                    "prompt": "implement it",
                    "scope": "small",
                    "type": "create",
                    "related_files": ["backend/testing/test_parent.py"],
                    "acceptance_evidence": own_evidence,
                },
            ]
        }
    )

    task = await _setup_plan_task_with_evidence(
        db_session, mocker, payload_evidence=parent_evidence
    )
    await _handle_plan_completion(task, plan_logs, db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1
    child_ev = children[0].first_payload.get("acceptance_evidence")
    assert child_ev["test_path"] == "backend/testing/test_child.py", (
        "subtask's own evidence must win over the parent's declaration"
    )
    assert child_ev["test_id"] == "test_child_seam"


@pytest.mark.asyncio
async def test_parent_evidence_lands_on_terminal_child_when_no_seam_match(
    db_session, mocker, _quiet_mocks
):
    """When no subtask references the declared seam path, the parent's evidence
    lands on the terminal (integrating) child only — not every child.

    Subtask B depends on A; neither references the declared test path, so the
    path-match policy finds nothing and falls back to the terminal sink (B).
    """
    parent_evidence = {
        "kind": "named_test",
        "test_path": "backend/testing/test_unreferenced.py",
        "test_id": "test_integration",
    }
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Step A",
                    "prompt": "build the foundation",
                    "scope": "small",
                    "type": "create",
                },
                {
                    "title": "Step B",
                    "prompt": "integrate on the foundation",
                    "scope": "small",
                    "type": "create",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task_with_evidence(
        db_session, mocker, payload_evidence=parent_evidence
    )
    await _handle_plan_completion(task, plan_logs, db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 2
    by_title = {c.title: c for c in children}
    # Foundation (source) does NOT inherit.
    assert "acceptance_evidence" not in by_title["Step A"].first_payload, (
        "non-terminal child must not inherit the parent's evidence"
    )
    # Terminal/integrating child (sink) inherits.
    child_ev = by_title["Step B"].first_payload.get("acceptance_evidence")
    assert child_ev is not None, "terminal child must inherit the parent's evidence"
    assert child_ev["test_path"] == "backend/testing/test_unreferenced.py"


@pytest.mark.asyncio
async def test_parent_declares_nothing_children_unchanged(
    db_session, mocker, _quiet_mocks
):
    """A parent plan task with no declared evidence leaves children untouched —
    nothing is synthesized (preserves today's no-synthesis behavior)."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: no evidence anywhere",
                    "prompt": "implement it",
                    "scope": "small",
                    "type": "create",
                },
            ]
        }
    )

    task = await _setup_plan_task_with_evidence(db_session, mocker)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1
    assert "acceptance_evidence" not in children[0].first_payload
    assert "acceptance_evidence" not in (children[0].context or {})


@pytest.mark.asyncio
async def test_plan_subtask_malformed_acceptance_evidence_is_dropped(
    db_session, mocker, _quiet_mocks
):
    """A malformed declaration (bad kind) is dropped — no key, no raise."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Step A",
                    "prompt": "do step A",
                    "scope": "small",
                    "acceptance_evidence": {"kind": "screenshot", "descriptor": "nope"},
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert "acceptance_evidence" not in (children[0].context or {})


@pytest.mark.asyncio
async def test_compute_experiment_subtask_carries_downstream_artifact_evidence(
    db_session, mocker, _quiet_mocks
):
    """A compute_experiment plan subtask with downstream_artifact acceptance_evidence
    carries that evidence onto its spawned first_payload.

    The artifact delivery contract reads first_payload via read_acceptance_evidence
    to find the downstream_artifact logical key; before this fix the evidence was
    dropped from compute_experiment payloads and the COMPUTE_RESULT Artifact was
    never stamped with the declared logical key.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Measure the baseline",
                    "prompt": "benchmark the current implementation",
                    "type": "compute_experiment",
                    "command": "python bench.py --mode baseline",
                    "spend_cap_usd": 3.5,
                    "deadline_seconds": 900,
                    "acceptance_evidence": {
                        "kind": "downstream_artifact",
                        "artifact_id": "benchmark-result-bundle",
                    },
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    payload = child.first_payload or {}
    assert payload["type"] == "compute_experiment"
    child_evidence = payload.get("acceptance_evidence")
    assert child_evidence is not None, (
        "acceptance_evidence missing from compute_experiment payload"
    )
    assert child_evidence["kind"] == "downstream_artifact"
    assert child_evidence["artifact_id"] == "benchmark-result-bundle"


# ─────────────────────────────────────────────────────────────────────────────
# Type coercion: mislabeled fresh-work synonyms → eligible plan-spawnable type
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mislabeled_fresh_work_type_is_coerced_and_plan_spawns(
    db_session, mocker, _quiet_mocks
):
    """A plan with a mislabeled fresh-work subtask type spawns via coercion.

    The canonical regression: plan emitting a fresh-work subtask typed 'fix'
    (matching its conventional-commit title prefix) must not kill the plan with
    'unsatisfiable dependency'. The 'fix' type is coerced to 'create' at plan
    ingestion, the coercion is recorded on the child context for traceability,
    and the child is spawned successfully.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "fix: apply the design-doc grounding integrity fix",
                    "prompt": "implement the integrity fix",
                    "type": "fix",
                }
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    await db_session.refresh(task)
    assert task.status != TaskStatus.FAILED, (
        f"Plan must not fail on mislabeled 'fix' type: {task.latest_status_message!r}"
    )

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, f"Expected 1 child after coercion, got {len(children)}"
    child = children[0]
    assert child.first_payload.get("type") == "create", (
        f"Mislabeled 'fix' must be coerced to 'create', got {child.first_payload.get('type')!r}"
    )
    coercion = (child.context or {}).get("type_coercion")
    assert coercion is not None, "type_coercion must be recorded on child context"
    assert coercion["original_type"] == "fix"
    assert coercion["coerced_type"] == "create"


@pytest.mark.asyncio
async def test_fix_type_coerced_to_create(db_session, mocker, _quiet_mocks):
    """A subtask typed 'fix' is coerced to 'create' and spawns successfully."""
    plan_logs = json.dumps(
        {"subtasks": [{"title": "Fix the bug", "prompt": "patch it", "type": "fix"}]}
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert children[0].first_payload.get("type") == "create"


@pytest.mark.asyncio
async def test_test_type_coerced_to_qa(db_session, mocker, _quiet_mocks):
    """A subtask typed 'test' (an unregistered synonym) is coerced to 'qa'."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Add coverage", "prompt": "write tests", "type": "test"}
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    assert children[0].first_payload.get("type") == "qa"


@pytest.mark.asyncio
async def test_coercion_recorded_on_child_context(db_session, mocker, _quiet_mocks):
    """original_type and coerced_type are present in child context after coercion."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {"title": "Feat: new thing", "prompt": "build it", "type": "feat"}
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1
    coercion = (children[0].context or {}).get("type_coercion")
    assert coercion is not None, "type_coercion must be set on coerced child context"
    assert coercion["original_type"] == "feat"
    assert coercion["coerced_type"] == "create"


@pytest.mark.asyncio
async def test_cascade_skip_spawns_unrelated_siblings(db_session, mocker, _quiet_mocks):
    """Cascade-skipped subtrees do not block unrelated siblings from spawning.

    A skipped non-coercible producer (research) and its dependent are both
    cascade-skipped, but an independent sibling still spawns.  The plan must NOT
    be failed — only the dependent subtree is silenced.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Research foundation",
                    "prompt": "research something",
                    "type": "research",
                },
                {
                    "title": "Implement on research",
                    "prompt": "build on the research",
                    "type": "create",
                    "depends_on_indices": [0],
                },
                {
                    "title": "Unrelated work",
                    "prompt": "independent task",
                    "type": "create",
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    await db_session.refresh(task)
    assert task.status != TaskStatus.FAILED, (
        f"Plan must not fail when unrelated sibling can still spawn: "
        f"{task.latest_status_message!r}"
    )

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 1, (
        f"Only the unrelated sibling should spawn, got {[c.title for c in children]}"
    )
    assert children[0].title == "Unrelated work"

    # Cascade skip recorded on task context.
    ctx = task.context or {}
    cascade = ctx.get("plan_subtask_cascade_skipped")
    assert cascade, "cascade_skipped must be recorded on task context"
    cascade_titles = {entry["title"] for entry in cascade}
    assert "Implement on research" in cascade_titles


@pytest.mark.asyncio
async def test_all_cascade_skipped_fails_plan(db_session, mocker, _quiet_mocks):
    """When every subtask ends up cascade-skipped, the plan fails outright."""
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Research only",
                    "prompt": "research something",
                    "type": "research",
                },
                {
                    "title": "Depends on research",
                    "prompt": "build on it",
                    "type": "create",
                    "depends_on_indices": [0],
                },
            ]
        }
    )

    task = await _setup_plan_task(db_session, mocker, plan_logs)
    await _handle_plan_completion(task, plan_logs, db_session)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED, (
        f"Plan must FAIL when all subtasks are cascade-skipped, got {task.status}"
    )
    assert "unsatisfiable dependency" in (task.latest_status_message or "").lower(), (
        f"Expected 'unsatisfiable dependency' in error message, "
        f"got {task.latest_status_message!r}"
    )

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 0, (
        f"No children should spawn when all are cascade-skipped, got {len(children)}"
    )


@pytest.mark.asyncio
async def test_visual_evidence_propagates_through_decomposition(
    db_session, mocker, _quiet_mocks
):
    """A visual_evidence declaration on a parent plan task survives onto the
    spawned child's payload unchanged.

    Regression guard: the plan_completion distribution path must forward the
    'visual' kind through the AcceptanceEvidence schema exactly as it does for
    'named_test' / 'observable_behavior' / 'downstream_artifact'.  The child
    that does not self-declare its own evidence must inherit the parent's.
    """
    parent_evidence = {
        "kind": "visual",
        "component": "MetricsDashboard",
        "viewport_width": 1280,
    }
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "feat: update MetricsDashboard component",
                    "prompt": "implement the component changes",
                    "scope": "small",
                    "type": "create",
                },
            ]
        }
    )

    task = await _setup_plan_task_with_evidence(
        db_session, mocker, payload_evidence=parent_evidence
    )
    await _handle_plan_completion(task, plan_logs, db_session)

    children = await _children_of(db_session, task)
    assert len(children) == 1, f"Expected 1 child, got {len(children)}"
    child_ev = children[0].first_payload.get("acceptance_evidence")
    assert child_ev is not None, (
        "visual acceptance_evidence must survive decomposition onto the child payload"
    )
    assert child_ev["kind"] == "visual"
    assert child_ev["component"] == "MetricsDashboard"
    assert child_ev["viewport_width"] == 1280


# ─────────────────────────────────────────────────────────────────────────────
# Regression: compute_experiment subtasks must inherit plan_branch base_branch
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_experiment_subtask_inherits_plan_branch(
    db_session, mocker, _quiet_mocks
):
    """A compute_experiment subtask spawned by plan completion must carry
    base_branch == plan_branch in its payload.

    Without this fix the field was absent, making the compute subtask appear
    cross-base relative to sibling create tasks and permanently QUEUED waiting
    on a landed_on_integration signal that never fires.
    """
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Create the feature",
                    "prompt": "implement the feature",
                    "type": "create",
                },
                {
                    "title": "Measure the result",
                    "prompt": "benchmark the feature",
                    "type": "compute_experiment",
                    "command": "python bench.py",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                    "depends_on_indices": [0],
                    "smoke_sample": {
                        "exit_code": 0,
                        "startup_ok": True,
                        "metric_captured": True,
                        "timed_out": False,
                    },
                },
            ]
        }
    )

    task = await _setup_plan_task(
        db_session, mocker, plan_logs, base_branch="plan/abc123"
    )
    await _handle_plan_completion(task, plan_logs, db_session)

    children = (
        (await db_session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )
    assert len(children) == 2
    by_title = {c.title: c for c in children}
    create_child = by_title["Create the feature"]
    compute_child = by_title["Measure the result"]

    create_payload = create_child.first_payload or {}
    compute_payload = compute_child.first_payload or {}

    create_base = create_payload.get("base_branch")
    assert create_base is not None, "create subtask must carry a base_branch"
    assert create_base.startswith("plan/"), (
        f"create subtask base_branch should be a plan/* branch, got {create_base!r}"
    )
    compute_base = compute_payload.get("base_branch")
    assert compute_base is not None, (
        "compute_experiment subtask must carry base_branch (was missing before the fix)"
    )
    assert compute_base == create_base, (
        f"compute_experiment base_branch {compute_base!r} must match sibling create "
        f"base_branch {create_base!r} so they are same-base in dep resolution"
    )


# ---------------------------------------------------------------------------
# Cross-tree base inheritance for genuine code dependents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_subtask_with_cross_tree_prereq_inherits_base(
    db_session, mocker, _quiet_mocks
):
    """A plan subtask whose explicit prerequisite is a cross-tree in-flight
    task with file overlap inherits the foundation's base_branch at spawn time.

    This prevents the cross-base landed-signal wedge on the plan path: if the
    plan branch is ``plan/abc`` but the prerequisite is on ``main``, the
    dependent subtask must land on ``main`` to see the foundation's merged code.
    """
    from testing.factories import make_task

    project = make_project(name="XbaseInheritPlan")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Existing in-flight foundation on main with a shared file.
    foundation = make_task(
        project_id=project.id,
        title="Build payment core",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "base_branch": "main"}],
    )
    foundation.context = {"related_files": ["backend/app/payments.py"]}
    db_session.add(foundation)
    await db_session.commit()
    await db_session.refresh(foundation)

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Add refund endpoint",
                    "prompt": "implement refunds",
                    "type": "create",
                    "prerequisite": {"kind": "task", "ref": str(foundation.id)},
                    "related_files": ["backend/app/payments.py"],
                }
            ]
        }
    )

    plan_task = make_task(
        project_id=project.id,
        title="Plan: add refunds",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "add refunds", "base_branch": "plan/abc"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )

    await _handle_plan_completion(plan_task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == plan_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.depends_on == [str(foundation.id)], (
        "explicit cross-tree prerequisite must persist a dependency edge, not "
        "only inherit the foundation base branch"
    )
    assert (child.first_payload or {}).get("base_branch") == "main", (
        "plan subtask with cross-tree genuine code-dep prereq must inherit "
        "foundation base_branch 'main'"
    )


@pytest.mark.asyncio
async def test_plan_subtask_ordering_only_prereq_keeps_own_base(
    db_session, mocker, _quiet_mocks
):
    """A plan subtask whose explicit prerequisite has NO file overlap does NOT
    inherit the foundation's base_branch — it keeps its own plan branch."""
    from testing.factories import make_task

    project = make_project(name="XbaseNoInheritPlan")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Foundation on main, but with disjoint files from the dependent.
    foundation = make_task(
        project_id=project.id,
        title="Set up CI pipeline",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "base_branch": "main"}],
    )
    foundation.context = {"related_files": ["infra/deploy.sh"]}
    db_session.add(foundation)
    await db_session.commit()
    await db_session.refresh(foundation)

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Add user settings page",
                    "prompt": "implement user settings",
                    "type": "create",
                    "prerequisite": {"kind": "task", "ref": str(foundation.id)},
                    "related_files": ["frontend/src/settings.tsx"],
                }
            ]
        }
    )

    plan_task = make_task(
        project_id=project.id,
        title="Plan: settings",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "plan", "prompt": "add settings", "base_branch": "plan/xyz"}
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )

    await _handle_plan_completion(plan_task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == plan_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    child_base = (child.first_payload or {}).get("base_branch") or ""
    assert child_base != "main", (
        "ordering-only cross-tree plan subtask must NOT inherit foundation base_branch"
    )
    assert "plan/" in child_base, (
        f"ordering-only dep must keep its own plan branch, got {child_base!r}"
    )


@pytest.mark.asyncio
async def test_plan_subtask_cross_tree_none_base_branch_is_noop(
    db_session, mocker, _quiet_mocks
):
    """_maybe_inherit_base is a no-op when the foundation has no base_branch.

    A foundation task with base_branch=None (e.g. an open version with no
    goal set) must not propagate None into the dependent's payload, which
    would crash downstream git operations expecting a string.
    """
    project = make_project(name="XbaseNoneFoundationPlan")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Foundation with NO base_branch in its payload (None goal / open version).
    foundation = make_task(
        project_id=project.id,
        title="Foundation no base",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
    )
    foundation.context = {"related_files": ["backend/app/payments.py"]}
    db_session.add(foundation)
    await db_session.commit()
    await db_session.refresh(foundation)

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Add refund endpoint",
                    "prompt": "implement refunds",
                    "type": "create",
                    "prerequisite": {"kind": "task", "ref": str(foundation.id)},
                    "related_files": ["backend/app/payments.py"],
                }
            ]
        }
    )

    plan_task = make_task(
        project_id=project.id,
        title="Plan: add refunds none base",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "add refunds", "base_branch": "plan/abc"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )

    await _handle_plan_completion(plan_task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == plan_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    child_base = (child.first_payload or {}).get("base_branch")
    assert child_base is not None, (
        "_maybe_inherit_base must not propagate None base_branch from foundation"
    )
    assert "plan/" in child_base, (
        "child must keep its own plan branch when foundation has no base_branch, "
        f"got {child_base!r}"
    )


@pytest.mark.asyncio
async def test_requeue_clone_reuses_existing_plan_branch(
    db_session, mocker, _quiet_mocks
):
    """A requeue-clone that already has a rollup PR open reuses the original
    staging branch so the merge guard sees reviewed-branch == PR-head.

    Scenario (real incident): plan task A has plan_branch='plan/aabbccdd' and
    rollup_pr_number=102. Its parent is requeued, A is cancelled, and the clone
    B inherits A's context (plan_branch='plan/aabbccdd', rollup_pr_number=102).
    When B runs plan_completion, it MUST use 'plan/aabbccdd', not derive a new
    'plan/<B-id>' — otherwise the merge guard aborts with "reviewed branch
    plan/<B-id> does not match PR head plan/aabbccdd".
    """
    plan_logs = json.dumps(
        {"subtasks": [{"title": "Step X", "prompt": "do step X", "scope": "small"}]}
    )

    project = make_project(name="RequeueBranchReuseTest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Clone task inherits plan_branch + rollup_pr_number from the cancelled original.
    original_plan_branch = "plan/aabbccdd"
    clone_task = make_task(
        project_id=project.id,
        title="Plan: requeue clone",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "do step X", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        context={
            "plan_branch": original_plan_branch,
            "rollup_pr_number": 102,
            "_attempt_number": 2,
        },
    )
    db_session.add(clone_task)
    await db_session.commit()
    await db_session.refresh(clone_task)

    ensure_branch_mock = mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=clone_task,
    )

    await _handle_plan_completion(clone_task, plan_logs, db_session)

    # _ensure_branch must have been called with the ORIGINAL branch, not the
    # clone's derived 'plan/<clone-id-prefix>' branch.
    assert ensure_branch_mock.called, "_ensure_branch should have been called"
    called_branch = ensure_branch_mock.call_args[0][1]
    assert called_branch == original_plan_branch, (
        f"Expected _ensure_branch called with '{original_plan_branch}', "
        f"got '{called_branch}'"
    )


@pytest.mark.asyncio
async def test_fresh_plan_task_derives_branch_from_task_id(
    db_session, mocker, _quiet_mocks
):
    """A plan task without a rollup_pr_number still derives its branch from the
    task ID — the reuse guard must not activate for fresh (non-retry) tasks."""
    plan_logs = json.dumps(
        {"subtasks": [{"title": "Step Y", "prompt": "do step Y", "scope": "small"}]}
    )

    project = make_project(name="FreshBranchTest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fresh_task = make_task(
        project_id=project.id,
        title="Plan: fresh task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "do step Y", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        # No plan_branch, no rollup_pr_number — fresh first attempt
    )
    db_session.add(fresh_task)
    await db_session.commit()
    await db_session.refresh(fresh_task)

    ensure_branch_mock = mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=fresh_task,
    )

    await _handle_plan_completion(fresh_task, plan_logs, db_session)

    assert ensure_branch_mock.called
    called_branch = ensure_branch_mock.call_args[0][1]
    short_id = str(fresh_task.id).replace("-", "")[:8]
    expected_branch = f"plan/{short_id}"
    assert called_branch == expected_branch, (
        f"Fresh task must derive branch from task ID — expected '{expected_branch}', "
        f"got '{called_branch}'"
    )


@pytest.mark.asyncio
async def test_plan_subtask_unresolved_task_prerequisite_is_blocked(
    db_session, mocker, _quiet_mocks
):
    """An absent external task foundation becomes a durable blocked need."""
    missing_id = str(uuid.uuid4())
    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Dependent implementation",
                    "prompt": "build on the external foundation",
                    "prerequisite": {"kind": "task", "ref": missing_id},
                }
            ]
        }
    )
    task = await _setup_plan_task(db_session, mocker, plan_logs)

    await _handle_plan_completion(task, plan_logs, db_session)

    child = (
        await db_session.execute(select(Task).where(Task.parent_task_id == task.id))
    ).scalars().one()
    assert child.status == TaskStatus.BLOCKED
    assert child.context["blocker"]["needs"] == [
        {"kind": "task", "ref": missing_id}
    ]
    assert child.context["unresolved_prerequisite"] == [
        {"kind": "task", "ref": missing_id}
    ]


@pytest.mark.asyncio
async def test_plan_capability_prerequisite_obeys_enforced_proof_gate(
    db_session, mocker, _quiet_mocks
):
    """Only a ledger-backed proven working capability leaves a child queued."""
    project = make_project(
        name="PlanCapabilityProofGate",
        problem_model={
            "capabilities": [{"name": "repo-map", "status": "working"}]
        },
        capability_proof_records={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    mocker.patch.dict(
        "app.plan_completion._runtime_config",
        {"capability_proof_gate_mode": "enforce"},
    )

    async def spawn_child(title):
        plan_logs = json.dumps(
            {
                "subtasks": [
                    {
                        "title": title,
                        "prompt": "use the repository map",
                        "prerequisite": {"kind": "capability", "ref": "repo-map"},
                    }
                ]
            }
        )
        plan = make_task(
            project_id=project.id,
            title=f"Plan: {title}",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "plan", "prompt": title, "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        )
        db_session.add(plan)
        await db_session.commit()
        await db_session.refresh(plan)
        mocker.patch(
            "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=plan
        )
        await _handle_plan_completion(plan, plan_logs, db_session)
        return (
            await db_session.execute(select(Task).where(Task.parent_task_id == plan.id))
        ).scalars().one()

    unproven = await spawn_child("Unproven capability consumer")
    assert unproven.status == TaskStatus.BLOCKED
    assert unproven.context["blocker"]["needs"] == [
        {"kind": "capability", "ref": "repo-map"}
    ]

    project.capability_proof_records = {
        "repo-map": [
            {
                "proven": True,
                "satisfied": True,
                "kind": "named_test",
                "proof_ref": "testing/test_repo_map.py::test_map",
            }
        ]
    }
    db_session.add(project)
    await db_session.commit()

    proven = await spawn_child("Proven capability consumer")
    assert proven.status == TaskStatus.QUEUED
    assert "blocker" not in (proven.context or {})


@pytest.mark.asyncio
async def test_plan_subtask_active_provider_blocks_on_unproven_capability_enforce(
    db_session, mocker, _quiet_mocks
):
    """Active sibling provider + enforce mode + empty ledger → consumer is BLOCKED.

    When a sibling subtask declares provides_capability and the proof gate is
    enforced but the ledger has no platform proof, the consumer must be blocked
    rather than wired to the unproven provider.
    """
    project = make_project(
        name="PlanActiveProviderGate",
        problem_model={
            "capabilities": [{"name": "repo-map", "status": "working"}]
        },
        capability_proof_records={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    mocker.patch.dict(
        "app.plan_completion._runtime_config",
        {"capability_proof_gate_mode": "enforce"},
    )

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Provider subtask",
                    "prompt": "build the repo map",
                    "provides_capability": "repo-map",
                },
                {
                    "title": "Consumer subtask",
                    "prompt": "use the repo map",
                    "prerequisite": {"kind": "capability", "ref": "repo-map"},
                },
            ]
        }
    )
    plan = make_task(
        project_id=project.id,
        title="Plan: active provider gate test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan)
    await db_session.commit()
    await db_session.refresh(plan)
    mocker.patch(
        "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=plan
    )

    await _handle_plan_completion(plan, plan_logs, db_session)

    children = (
        await db_session.execute(
            select(Task)
            .where(Task.parent_task_id == plan.id)
            .order_by(Task.created_at)
        )
    ).scalars().all()
    assert len(children) == 2
    provider, consumer = children[0], children[1]
    assert provider.context.get("provides_capability") == "repo-map"
    # Consumer must be BLOCKED — the active sibling provides but is unproven
    assert consumer.status == TaskStatus.BLOCKED
    assert consumer.context["blocker"]["needs"] == [
        {"kind": "capability", "ref": "repo-map"}
    ]
    # Consumer must NOT be directly wired to the provider
    assert str(provider.id) not in (consumer.depends_on or [])


@pytest.mark.asyncio
async def test_plan_subtask_active_provider_unblocks_when_proven(
    db_session, mocker, _quiet_mocks
):
    """Active sibling provider + enforce mode + proven ledger → consumer is wired."""
    project = make_project(
        name="PlanActiveProviderProven",
        problem_model={
            "capabilities": [{"name": "repo-map", "status": "working"}]
        },
        capability_proof_records={
            "repo-map": [
                {
                    "proven": True,
                    "satisfied": True,
                    "kind": "named_test",
                    "proof_ref": "testing/test_repo_map.py::test_map",
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    mocker.patch.dict(
        "app.plan_completion._runtime_config",
        {"capability_proof_gate_mode": "enforce"},
    )

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Provider subtask",
                    "prompt": "build the repo map",
                    "provides_capability": "repo-map",
                },
                {
                    "title": "Consumer subtask",
                    "prompt": "use the repo map",
                    "prerequisite": {"kind": "capability", "ref": "repo-map"},
                },
            ]
        }
    )
    plan = make_task(
        project_id=project.id,
        title="Plan: active provider proven test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan)
    await db_session.commit()
    await db_session.refresh(plan)
    mocker.patch(
        "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=plan
    )

    await _handle_plan_completion(plan, plan_logs, db_session)

    children = (
        await db_session.execute(
            select(Task)
            .where(Task.parent_task_id == plan.id)
            .order_by(Task.created_at)
        )
    ).scalars().all()
    assert len(children) == 2
    provider, consumer = children[0], children[1]
    # Consumer must NOT be blocked — the provider is proven in the ledger
    assert consumer.status == TaskStatus.QUEUED
    assert "blocker" not in (consumer.context or {})
    # Consumer must be wired to the provider via depends_on
    assert str(provider.id) in (consumer.depends_on or [])


@pytest.mark.asyncio
async def test_plan_subtask_completed_provider_blocks_on_unproven_capability_enforce(
    db_session, mocker, _quiet_mocks
):
    """Completed sibling provider + enforce mode + empty ledger → consumer is BLOCKED.

    A completed task advertising provides_capability satisfies the need only if
    the proof gate allows it. In enforce mode with no platform ledger proof, the
    consumer must still be blocked even though a shipped foundation exists.
    """
    project = make_project(
        name="PlanCompletedProviderGate",
        problem_model={
            "capabilities": [{"name": "repo-map", "status": "working"}]
        },
        capability_proof_records={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    mocker.patch.dict(
        "app.plan_completion._runtime_config",
        {"capability_proof_gate_mode": "enforce"},
    )

    # Create a pre-existing completed task that provides the capability
    completed_provider = make_task(
        project_id=project.id,
        title="Completed repo-map provider",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "base_branch": "main"}],
        context={"provides_capability": "repo-map", "pr_merged": True},
    )
    db_session.add(completed_provider)
    await db_session.commit()
    await db_session.refresh(completed_provider)

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Consumer subtask",
                    "prompt": "use the repo map",
                    "prerequisite": {"kind": "capability", "ref": "repo-map"},
                }
            ]
        }
    )
    plan = make_task(
        project_id=project.id,
        title="Plan: completed provider gate test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan)
    await db_session.commit()
    await db_session.refresh(plan)
    mocker.patch(
        "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=plan
    )

    await _handle_plan_completion(plan, plan_logs, db_session)

    consumer = (
        await db_session.execute(select(Task).where(Task.parent_task_id == plan.id))
    ).scalars().one()
    # Consumer must be BLOCKED — a completed provider exists but no platform proof
    assert consumer.status == TaskStatus.BLOCKED
    assert consumer.context["blocker"]["needs"] == [
        {"kind": "capability", "ref": "repo-map"}
    ]


@pytest.mark.asyncio
async def test_unproven_capability_prereq_off_mode_surfaces_unverified_marker(
    db_session, mocker, _quiet_mocks
):
    """In off mode, unproven working capability dispatches child with surface marker.

    When capability_proof_gate_mode is "off" and a prerequisite names a
    "working" registry capability that has no platform proof, the child still
    dispatches (is not BLOCKED), but the unverified status is stamped durably
    on the child's context under _spawn_capability_proof_unverified so an
    operator can see the unproven foundation without re-deriving it from logs.
    """
    from app.research_completion import SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY

    project = make_project(
        name="PlanCapabilityOffModeSurface",
        problem_model={
            "capabilities": [{"name": "repo-map", "status": "working"}]
        },
        capability_proof_records={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    mocker.patch.dict(
        "app.plan_completion._runtime_config",
        {"capability_proof_gate_mode": "off"},
    )

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Consumer subtask",
                    "prompt": "use the repo map",
                    "prerequisite": {"kind": "capability", "ref": "repo-map"},
                }
            ]
        }
    )
    plan = make_task(
        project_id=project.id,
        title="Plan: off mode surface test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan)
    await db_session.commit()
    await db_session.refresh(plan)
    mocker.patch(
        "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=plan
    )

    await _handle_plan_completion(plan, plan_logs, db_session)

    consumer = (
        await db_session.execute(select(Task).where(Task.parent_task_id == plan.id))
    ).scalars().one()
    # In off mode the child dispatches — NOT blocked by the proof gate
    assert consumer.status == TaskStatus.QUEUED, (
        f"In off mode, consumer should dispatch QUEUED, got {consumer.status}"
    )
    assert "blocker" not in (consumer.context or {}), (
        "Off mode must not set a blocker on the consumer"
    )
    # But the unverified status is surfaced durably on the context
    surface_markers = (consumer.context or {}).get(SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY)
    assert surface_markers is not None, (
        f"Off mode must stamp {SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY!r} on child context"
    )
    assert any(m.get("ref") == "repo-map" for m in surface_markers), (
        f"Surface marker must name the unproven capability, got {surface_markers!r}"
    )


@pytest.mark.asyncio
async def test_proven_capability_prereq_dispatches_without_surface_marker(
    db_session, mocker, _quiet_mocks
):
    """A proven working capability dispatches the child cleanly with no surface marker.

    When the platform ledger holds a corroborated machine proof for the capability,
    spawn_capability_gate_disposition returns 'ok' and no unverified marker is
    stamped on the child's context in either enforce or off mode.
    """
    from app.research_completion import SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY

    project = make_project(
        name="PlanCapabilityProvenClean",
        problem_model={
            "capabilities": [{"name": "repo-map", "status": "working"}]
        },
        capability_proof_records={
            "repo-map": [
                {
                    "proven": True,
                    "satisfied": True,
                    "kind": "named_test",
                    "proof_ref": "testing/test_repo_map.py::test_map",
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    mocker.patch.dict(
        "app.plan_completion._runtime_config",
        {"capability_proof_gate_mode": "off"},
    )

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Consumer subtask",
                    "prompt": "use the proven repo map",
                    "prerequisite": {"kind": "capability", "ref": "repo-map"},
                }
            ]
        }
    )
    plan = make_task(
        project_id=project.id,
        title="Plan: proven capability clean dispatch",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan)
    await db_session.commit()
    await db_session.refresh(plan)
    mocker.patch(
        "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=plan
    )

    await _handle_plan_completion(plan, plan_logs, db_session)

    consumer = (
        await db_session.execute(select(Task).where(Task.parent_task_id == plan.id))
    ).scalars().one()
    # Proven capability: child dispatches cleanly, no blocker, no surface marker
    assert consumer.status == TaskStatus.QUEUED, (
        f"Proven capability consumer should dispatch QUEUED, got {consumer.status}"
    )
    assert "blocker" not in (consumer.context or {}), (
        "Proven capability must not set a blocker on the consumer"
    )
    assert SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY not in (consumer.context or {}), (
        f"Proven capability must not stamp {SPAWN_CAPABILITY_PROOF_UNVERIFIED_KEY!r}"
    )


@pytest.mark.asyncio
async def test_active_provider_enforce_blocks_directly_not_via_registry_fallback(
    db_session, mocker, _quiet_mocks
):
    """Active sibling + enforce + unproven → child blocked via direct path, not registry fallback.

    Specifically validates that the fix (enter_blocked_state immediately when
    task_backed_capability_need_blocked_by_proof_gate fires for an active sibling)
    correctly gates the consumer. The consumer must be BLOCKED with the need
    recorded, and must NOT be wired to the provider via depends_on.
    """
    project = make_project(
        name="PlanActiveDirectBlock",
        problem_model={
            "capabilities": [{"name": "semantic-search", "status": "working"}]
        },
        capability_proof_records={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    mocker.patch.dict(
        "app.plan_completion._runtime_config",
        {"capability_proof_gate_mode": "enforce"},
    )

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Provider subtask",
                    "prompt": "build semantic search",
                    "provides_capability": "semantic-search",
                },
                {
                    "title": "Consumer subtask",
                    "prompt": "use semantic search",
                    "prerequisite": {"kind": "capability", "ref": "semantic-search"},
                },
            ]
        }
    )
    plan = make_task(
        project_id=project.id,
        title="Plan: active direct block test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan)
    await db_session.commit()
    await db_session.refresh(plan)
    mocker.patch(
        "app.task_completion._ensure_branch", new_callable=AsyncMock, return_value=plan
    )

    await _handle_plan_completion(plan, plan_logs, db_session)

    children = (
        await db_session.execute(
            select(Task)
            .where(Task.parent_task_id == plan.id)
            .order_by(Task.created_at)
        )
    ).scalars().all()
    assert len(children) == 2
    provider, consumer = children[0], children[1]
    assert provider.context.get("provides_capability") == "semantic-search"
    # Consumer must be BLOCKED by the proof gate
    assert consumer.status == TaskStatus.BLOCKED, (
        f"Consumer must be BLOCKED in enforce mode with unproven provider, "
        f"got {consumer.status}"
    )
    assert consumer.context["blocker"]["needs"] == [
        {"kind": "capability", "ref": "semantic-search"}
    ], f"Blocker needs must name the capability, got {consumer.context.get('blocker')!r}"
    # Consumer must NOT be wired to the provider via depends_on
    assert str(provider.id) not in (consumer.depends_on or []), (
        "Consumer must not depend_on unproven provider — proof gate must prevent wiring"
    )
