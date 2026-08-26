"""Pipeline ACTION verbs on ``POST /api/agent/strategic``.

The strategist could describe the pipeline but could not act on it: there was
no op for promote, requeue/retry, unpark, reprioritise, or iteration-cap reset,
so every one of those operator instructions bottlenecked on a human driving the
admin HTTP levers by hand. These tests pin the five verbs that closed that gap.

Two things they deliberately pin hard, because getting them wrong is how the
feature becomes dangerous rather than merely broken:

* **force_promote skips the AUDIT gate and ONLY that.** Its contract text must
  keep saying, in literal words, that CI is still enforced. A Resident that
  tells an operator "promoted, CI skipped" has misinformed them about
  production, and a silently reworded manifest note is exactly how that ships.
* **Every verb reports the REASON it did not act** rather than asserting
  success — the no-op promotion status, the non-retryable source status, the
  fact that a priority write did not dispatch anything.
"""

import json
import os
import uuid

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.models import Project, ProjectMember, Task, TaskStatus

TEST_USER_ID = "u-strategic-pipeline-actions"

PIPELINE_ACTION_OPS = (
    "force_promote",
    "requeue_task",
    "reset_iteration_cap",
    "set_task_priority",
    "unhold_direction",
)

_MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "strategic_ops_manifest.json"
)


def _manifest_entries() -> dict:
    with open(_MANIFEST_PATH, encoding="utf-8") as handle:
        return {entry["op"]: entry for entry in json.load(handle)}


async def _make_project(session, owner_id=TEST_USER_ID, name="Pipeline Actions"):
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(project_id=project.id, user_id=owner_id, role="owner")
    )
    await session.commit()
    return project


async def _make_strategic_task(session, project, vm_id="agent-vm-pipeline-actions"):
    strategic = Task(
        project_id=project.id,
        title="Strategic review",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id=vm_id,
    )
    session.add(strategic)
    await session.commit()
    await session.refresh(strategic)
    token = create_agent_token(
        str(strategic.id), vm_id, task_type="strategic_review"
    )
    return strategic, token


async def _op(client, strategic, token, op, data):
    return await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={"task_id": str(strategic.id), "op": op, "data": data},
    )


def _refusal_message(response) -> str:
    """The refusal REASON off the wire, in the shape the app actually emits.

    ``app.main``'s ``@app.exception_handler(HTTPException)`` reshapes the
    handler's ``detail=`` into ``{"error": {"code": ..., "message": ...}}``, so
    a test that reads ``["detail"]`` is asserting against a body this API never
    returns. Every refusal test goes through here so the reason stays pinned to
    the real contract rather than to FastAPI's default shape.
    """
    body = response.json()
    assert "error" in body, f"unexpected error body shape: {body}"
    return body["error"]["message"]


# ---------------------------------------------------------------------------
# Contract / drift guards — these are the half that fails on the UNFIXED code.
# ---------------------------------------------------------------------------


def test_every_pipeline_action_verb_is_a_surfaced_manifest_op():
    """The five verbs exist in the shared manifest and are agent-reachable.

    Before this change the manifest exposed 43 read/plan/annotate ops and no
    ACTION verb at all, so "requeue that task" had nothing to call.
    """
    entries = _manifest_entries()
    missing = [op for op in PIPELINE_ACTION_OPS if op not in entries]
    assert not missing, f"manifest is missing pipeline action verbs: {missing}"
    for op in PIPELINE_ACTION_OPS:
        entry = entries[op]
        assert entry["alias"] == op.replace("_", "-")
        assert entry["internal_reason"] is None, (
            f"{op} must be operator-reachable, not internal-only"
        )
        assert entry["cli"] is True and entry["prompt"] is True
        assert entry.get("input"), f"{op} must publish an input contract"


def test_every_pipeline_action_verb_has_a_live_backend_handler():
    """Manifest ↔ STRATEGIC_AGENT_OPS ↔ real elif handler, for these five."""
    from app.routers.admin_agent_ops import STRATEGIC_AGENT_OPS, strategic_operation

    import inspect

    source = inspect.getsource(strategic_operation)
    for op in PIPELINE_ACTION_OPS:
        assert op in STRATEGIC_AGENT_OPS, (
            f"{op} is in the manifest but not in STRATEGIC_AGENT_OPS"
        )
        assert f'payload.op == "{op}"' in source, (
            f"{op} is inventoried but has no elif handler in strategic_operation"
        )


def test_each_action_verb_states_what_it_does_not_do():
    """Every action contract must carry an explicit negative clause.

    A verb that only advertises what it does invites the caller to assume the
    rest. Each of these five has a specific, load-bearing thing it does NOT do
    (skip CI, unpark a loop, re-spawn, dispatch, change status), and the
    contract text is the only place the agent ever reads it.
    """
    entries = _manifest_entries()
    for op in PIPELINE_ACTION_OPS:
        notes = entries[op]["input"].get("notes") or []
        joined = " ".join(notes).lower()
        assert any(
            marker in joined for marker in ("does not", "skips:", "never")
        ), f"{op} contract notes never say what it does NOT do: {notes}"


def test_force_promote_contract_says_it_does_not_skip_ci():
    """LITERAL pin. force-promote skips the AUDIT gate and only the audit gate.

    This assertion is deliberately spelled out rather than compared against a
    constant lifted from the source: the whole point is that a future edit
    cannot quietly soften the wording and still be green. If the phrasing has
    to change, the new phrasing must still say all three things below, and
    changing this test is the review signal that someone touched the sentence
    an operator's production decision rests on.
    """
    entry = _manifest_entries()["force_promote"]
    description = entry["description"].lower()
    notes = " ".join(entry["input"]["notes"]).lower()

    assert "audit" in description and "not skip ci" in description, description
    assert "does not skip ci" in notes
    assert "audit" in notes
    # The two-phase shape is part of the contract: the first call cuts the PR
    # and waits. A caller that does not know this reports a wait as a failure.
    assert "two-phase" in notes or "call it again" in notes
    # And it must never be described to the operator as skipping CI.
    assert "never report" in notes


def test_reset_iteration_cap_is_documented_as_the_unpark_verb():
    """unpark and cap-reset are ONE server operation, not two.

    ``review_park_disposition`` documents this endpoint as one of only three
    releases for a chain parked for operator disposition. Shipping a separate
    "unpark" verb would have been a second implementation of the same thing.
    """
    notes = " ".join(
        _manifest_entries()["reset_iteration_cap"]["input"]["notes"]
    ).lower()
    assert "unpark" in notes
    assert "does not re-spawn" in notes


def test_requeue_task_is_documented_as_the_retry_verb():
    """retry and requeue are ONE operation; there is deliberately no retry op."""
    entries = _manifest_entries()
    assert "retry" not in entries, (
        "retry must not be a separate op — retrying a task IS requeueing it"
    )
    notes = " ".join(entries["requeue_task"]["input"]["notes"]).lower()
    assert "retry verb" in notes


# ---------------------------------------------------------------------------
# Pure argument parsing — no DB, no I/O.
# ---------------------------------------------------------------------------


def test_priority_accepts_labels_and_integers_and_rejects_the_rest():
    from app.strategic_pipeline_actions import (
        PipelineActionError,
        parse_priority,
    )

    assert parse_priority("high") == 100
    assert parse_priority("MEDIUM") == 50
    assert parse_priority("low") == 0
    assert parse_priority(0) == 0
    assert parse_priority(77) == 77
    assert parse_priority("77") == 77

    for bad in (True, False, -1, 101, "urgent", None, [50]):
        with pytest.raises(PipelineActionError) as excinfo:
            parse_priority(bad)
        assert excinfo.value.status_code == 400


def test_every_action_verb_requires_a_reason():
    """An unexplained privileged pipeline action is unreviewable afterwards."""
    from app.strategic_pipeline_actions import (
        PipelineActionError,
        parse_force_promote,
        parse_requeue_task,
        parse_reset_iteration_cap,
        parse_set_task_priority,
        parse_unhold_direction,
    )

    task_id = str(uuid.uuid4())
    cases = (
        (parse_force_promote, {}),
        (parse_requeue_task, {"target_task_id": task_id}),
        (parse_reset_iteration_cap, {"target_task_id": task_id}),
        (parse_set_task_priority, {"target_task_id": task_id, "priority": "high"}),
        (parse_unhold_direction, {"direction_id": "d-abc123"}),
    )
    for parser, data in cases:
        with pytest.raises(PipelineActionError) as excinfo:
            parser({**data, "reason": "   "})
        assert excinfo.value.status_code == 400
        assert "reason" in excinfo.value.detail


def test_requeue_guidance_defaults_to_the_reason_and_refuses_internal_key():
    """Requeueing with no instruction repeats the attempt that just failed."""
    from app.strategic_pipeline_actions import (
        PipelineActionError,
        parse_requeue_task,
    )

    task_id = uuid.uuid4()
    _tid, reason, guidance = parse_requeue_task(
        {"target_task_id": str(task_id), "reason": "Platform-killed on timeout."}
    )
    assert _tid == task_id
    assert guidance == reason == "Platform-killed on timeout."

    _tid, _reason, guidance = parse_requeue_task(
        {
            "target_task_id": str(task_id),
            "reason": "Platform-killed on timeout.",
            "guidance": "Start from the branch as it stands.",
        }
    )
    assert guidance == "Start from the branch as it stands."

    with pytest.raises(PipelineActionError) as excinfo:
        parse_requeue_task(
            {
                "target_task_id": str(task_id),
                "reason": "why",
                "_original_prompt_suffix": "smuggled",
            }
        )
    assert excinfo.value.status_code == 400
    assert "_original_prompt_suffix" in excinfo.value.detail


def test_force_promote_candidate_sha_is_validated_but_short_refs_allowed():
    from app.strategic_pipeline_actions import (
        PipelineActionError,
        parse_force_promote,
    )

    reason, sha = parse_force_promote({"reason": "audit lane only outstanding"})
    assert sha is None and reason

    _reason, sha = parse_force_promote(
        {"reason": "pin the proven commit", "candidate_sha": "0C45D16"}
    )
    assert sha == "0c45d16", "an abbreviated ref is legitimate; it resolves server-side"

    for bad in ("abc", "z" * 12, "0c45d16" + "0" * 40):
        with pytest.raises(PipelineActionError) as excinfo:
            parse_force_promote({"reason": "r", "candidate_sha": bad})
        assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# Live behavior over POST /api/agent/strategic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_task_priority_rewrites_the_dispatch_ordering_integer(
    client: AsyncClient, db_session
):
    """Reprioritise writes Task.priority and admits it dispatched nothing."""
    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)
    target = Task(
        project_id=project.id,
        title="Queued work",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "build"}],
        priority=50,
    )
    db_session.add(target)
    await db_session.commit()
    await db_session.refresh(target)

    response = await _op(
        client,
        strategic,
        token,
        "set_task_priority",
        {
            "target_task_id": str(target.id),
            "priority": "high",
            "reason": "Unblocks two other directions.",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["previous_priority"] == 50
    assert body["priority"] == 100
    assert body["target_status"] == TaskStatus.QUEUED.value
    # Surface the REASON, not just the state: priority only orders rows that
    # are already eligible, so the caller must not read this as "it ran".
    assert body["dispatched"] is False

    await db_session.refresh(target)
    assert target.priority == 100
    assert (target.context or {})["priority_set_reason"] == (
        "Unblocks two other directions."
    )


@pytest.mark.asyncio
async def test_pipeline_action_verbs_refuse_a_cross_project_target(
    client: AsyncClient, db_session
):
    """The strategic session's project IS the authorization boundary."""
    project = await _make_project(db_session, name="Caller project")
    other = await _make_project(
        db_session, owner_id="u-other-owner", name="Other project"
    )
    strategic, token = await _make_strategic_task(db_session, project)
    foreign = Task(
        project_id=other.id,
        title="Someone else's task",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "build"}],
    )
    db_session.add(foreign)
    await db_session.commit()
    await db_session.refresh(foreign)

    for op, data in (
        (
            "set_task_priority",
            {"target_task_id": str(foreign.id), "priority": "low", "reason": "r"},
        ),
        ("requeue_task", {"target_task_id": str(foreign.id), "reason": "r"}),
        ("reset_iteration_cap", {"target_task_id": str(foreign.id), "reason": "r"}),
    ):
        response = await _op(client, strategic, token, op, data)
        assert response.status_code == 403, (op, response.text)

    await db_session.refresh(foreign)
    assert foreign.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_requeue_task_redrives_a_failed_task_and_names_the_prior_status(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)
    target = Task(
        project_id=project.id,
        title="Platform-killed work",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "build the thing"}],
    )
    db_session.add(target)
    await db_session.commit()
    await db_session.refresh(target)
    # Snapshot the pk BEFORE expire_all() below: reading target.id off an
    # expired instance is a synchronous lazy load inside async context, which
    # raises MissingGreenlet rather than telling you the row did not change.
    target_id = target.id

    response = await _op(
        client,
        strategic,
        token,
        "requeue_task",
        {
            "target_task_id": str(target_id),
            "reason": "AGENT_TIMEOUT platform kill; the work was never attempted.",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["previous_status"] == TaskStatus.FAILED.value
    assert body["new_status"] == TaskStatus.QUEUED.value

    db_session.expire_all()
    refreshed = await db_session.get(Task, target_id)
    assert refreshed.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_requeue_task_refuses_a_non_retryable_status_with_the_reason(
    client: AsyncClient, db_session
):
    """A COMPLETED task is not resurrectable, and the refusal must say why."""
    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)
    target = Task(
        project_id=project.id,
        title="Landed work",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "build"}],
    )
    db_session.add(target)
    await db_session.commit()
    await db_session.refresh(target)
    target_id = target.id

    response = await _op(
        client,
        strategic,
        token,
        "requeue_task",
        {"target_task_id": str(target_id), "reason": "try again"},
    )
    assert response.status_code == 409, response.text
    detail = _refusal_message(response)
    assert TaskStatus.COMPLETED.value in detail
    assert "not" in detail and "retryable" in detail

    db_session.expire_all()
    refreshed = await db_session.get(Task, target_id)
    assert refreshed.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_unhold_direction_clears_a_hold_and_is_idempotent(
    client: AsyncClient, db_session
):
    import app.shared_state as _ss

    project = await _make_project(db_session)
    project.directions = [
        {"id": "d-held", "text": "Keep the fleet healthy", "status": "active"}
    ]
    db_session.add(project)
    await db_session.commit()
    strategic, token = await _make_strategic_task(db_session, project)

    from app.direction_streaks import (
        DIRECTION_HOLD_THRESHOLD,
        direction_failure_streaks,
        record_direction_result,
    )

    direction_failure_streaks.pop("d-held", None)
    for _ in range(DIRECTION_HOLD_THRESHOLD):
        record_direction_result("d-held", False, failure_class="CODE_ERROR")
    assert _ss.is_direction_held("d-held")[0] is True

    response = await _op(
        client,
        strategic,
        token,
        "unhold_direction",
        {
            "direction_id": "d-held",
            "reason": "The three failures were one platform defect, now fixed.",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["was_held"] is True
    assert body["spawned"] is False
    assert _ss.is_direction_held("d-held")[0] is False

    # Idempotent: repeating it reports was_held=False rather than erroring.
    repeat = await _op(
        client,
        strategic,
        token,
        "unhold_direction",
        {"direction_id": "d-held", "reason": "already clear"},
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["was_held"] is False


@pytest.mark.asyncio
async def test_unhold_direction_404s_on_an_id_this_project_does_not_own(
    client: AsyncClient, db_session
):
    """The hold registry is keyed by bare id, so existence must be checked."""
    project = await _make_project(db_session)
    project.directions = [{"id": "d-mine", "text": "mine", "status": "active"}]
    db_session.add(project)
    await db_session.commit()
    strategic, token = await _make_strategic_task(db_session, project)

    response = await _op(
        client,
        strategic,
        token,
        "unhold_direction",
        {"direction_id": "d-not-mine", "reason": "typo"},
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_reset_iteration_cap_unparks_a_chain_without_respawning(
    client: AsyncClient, db_session
):
    """Same body as the operator lever, reached through the strategic op."""
    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)
    parked = Task(
        project_id=project.id,
        title="Parked review chain",
        status=TaskStatus.PARKED,
        payloads=[{"type": "review", "prompt": "review", "pr_number": 4242}],
        context={
            "review_loop": {"pr_number": 4242, "iteration": 7},
            "review_loop_abandon_candidate": {"kind": "stuck"},
        },
    )
    failed_fix = Task(
        project_id=project.id,
        title="Failed fix on the same PR",
        status=TaskStatus.FAILED,
        payloads=[{"type": "fix", "prompt": "fix", "pr_number": 4242}],
    )
    db_session.add(parked)
    db_session.add(failed_fix)
    await db_session.commit()
    await db_session.refresh(parked)
    await db_session.refresh(failed_fix)
    parked_id = parked.id
    failed_fix_id = failed_fix.id

    response = await _op(
        client,
        strategic,
        token,
        "reset_iteration_cap",
        {
            "target_task_id": str(parked_id),
            "reason": "The cap tripped on infra timeouts, not real iterations.",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pr_number"] == 4242
    assert body["previous_per_chain_iteration"] == 7
    assert body["archived_count"] == 1
    # Reporting the boundary is the point: nothing was re-spawned.
    assert body["respawned"] is False

    db_session.expire_all()
    refreshed = await db_session.get(Task, parked_id)
    assert refreshed.context["review_loop"]["iteration"] == 0
    assert "review_loop_abandon_candidate" not in refreshed.context
    archived = await db_session.get(Task, failed_fix_id)
    assert archived.archived is True


@pytest.mark.asyncio
async def test_reset_iteration_cap_refuses_a_task_with_no_pr(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)
    target = Task(
        project_id=project.id,
        title="Not a review chain",
        status=TaskStatus.FAILED,
        payloads=[{"type": "create", "prompt": "build"}],
    )
    db_session.add(target)
    await db_session.commit()
    await db_session.refresh(target)

    response = await _op(
        client,
        strategic,
        token,
        "reset_iteration_cap",
        {"target_task_id": str(target.id), "reason": "unpark it"},
    )
    assert response.status_code == 400, response.text
    assert "pr_number" in _refusal_message(response)


@pytest.mark.asyncio
async def test_force_promote_reports_a_no_op_guard_as_not_promoted(
    client: AsyncClient, db_session, monkeypatch
):
    """A pre-promote guard is an ANSWER to relay, not a success to claim.

    ``promotion_check_wait`` is the single most misreportable status here: a
    promotion is already in flight, nothing new happened, and a caller that
    reads HTTP 200 as "promoted" tells the operator production moved when it
    did not.
    """
    from app import release_qualifier

    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)

    seen = {}

    async def _fake_force_promote(session, project_id, **kwargs):
        seen["project_id"] = project_id
        seen.update(kwargs)
        return {
            "status": "promotion_check_wait",
            "promotion_pr_number": 17999,
            "audit_detail_blob": {"deeply": {"nested": "x" * 5000}},
        }

    monkeypatch.setattr(
        release_qualifier, "force_promote_current_version", _fake_force_promote
    )

    response = await _op(
        client,
        strategic,
        token,
        "force_promote",
        {"reason": "Only the audit lane is outstanding."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["promoted"] is False
    assert body["promotion_status"] == "promotion_check_wait"
    assert body["promotion"]["promotion_pr_number"] == 17999
    # The response must stay bounded — an unbounded echo truncates somewhere
    # unpredictable and the REASON is what gets lost.
    assert "audit_detail_blob" not in body["promotion"]
    # And the response itself must keep saying CI was not skipped.
    assert body["audit_gate"] == "skipped"
    assert "NOT skipped" in body["ci_gate"]

    assert seen["project_id"] == project.id
    assert seen["candidate_sha"] is None
    assert seen["reason"] == "Only the audit lane is outstanding."
    assert str(strategic.id) in seen["actor"]


@pytest.mark.asyncio
async def test_force_promote_reports_a_real_promotion_as_promoted(
    client: AsyncClient, db_session, monkeypatch
):
    from app import release_qualifier

    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)

    async def _fake_force_promote(session, project_id, **kwargs):
        return {"status": "promoted", "promotion_pr_number": 18000}

    monkeypatch.setattr(
        release_qualifier, "force_promote_current_version", _fake_force_promote
    )

    response = await _op(
        client,
        strategic,
        token,
        "force_promote",
        {
            "reason": "Claims proven by landed work.",
            "candidate_sha": "0c45d166455a0f6d3c1a2b4e5d6f708192a3b4c5",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["promoted"] is True
    assert body["promotion_status"] == "promoted"


@pytest.mark.asyncio
async def test_force_promote_does_not_run_under_the_dispatch_locks(
    client: AsyncClient, db_session, monkeypatch
):
    """Slow GitHub I/O must happen AFTER the Project/Task locks are released.

    ``strategic_operation`` holds the Project row FOR UPDATE for its whole
    body. Running force-promote there idles the connection across minutes of
    GitHub I/O — the failure ``_apply_qualify_request_post_commit`` was written
    to prevent. The observable proof is that the implementation is handed a
    DIFFERENT session object than the request session that took the locks.
    """
    from app import release_qualifier
    import app.routers.admin_agent_ops as agent_ops

    project = await _make_project(db_session)
    strategic, token = await _make_strategic_task(db_session, project)

    request_sessions: list[int] = []
    original_lock = agent_ops._lock_project_or_429

    async def _tracking_lock(session, project_id, *args, **kwargs):
        request_sessions.append(id(session))
        return await original_lock(session, project_id, *args, **kwargs)

    promote_sessions: list[int] = []

    async def _fake_force_promote(session, project_id, **kwargs):
        promote_sessions.append(id(session))
        return {"status": "no_open_version"}

    monkeypatch.setattr(agent_ops, "_lock_project_or_429", _tracking_lock)
    monkeypatch.setattr(
        release_qualifier, "force_promote_current_version", _fake_force_promote
    )

    response = await _op(
        client, strategic, token, "force_promote", {"reason": "check the plumbing"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["promoted"] is False
    assert response.json()["promotion_status"] == "no_open_version"

    assert request_sessions and promote_sessions
    assert promote_sessions[0] not in request_sessions, (
        "force_promote ran on the locked request session — slow GitHub I/O "
        "would then be held under the Project FOR UPDATE lock"
    )


@pytest.mark.asyncio
async def test_action_verbs_reject_a_non_strategic_agent_token(
    client: AsyncClient, db_session
):
    """These are privileged pipeline levers; an ordinary worker cannot call them."""
    project = await _make_project(db_session)
    worker = Task(
        project_id=project.id,
        title="Ordinary worker",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create", "prompt": "build"}],
        vm_instance_id="agent-vm-worker",
    )
    db_session.add(worker)
    await db_session.commit()
    await db_session.refresh(worker)
    token = create_agent_token(str(worker.id), "agent-vm-worker", task_type="create")

    for op, data in (
        ("force_promote", {"reason": "let me out"}),
        ("requeue_task", {"target_task_id": str(worker.id), "reason": "r"}),
        ("unhold_direction", {"direction_id": "d-x", "reason": "r"}),
    ):
        response = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={"task_id": str(worker.id), "op": op, "data": data},
        )
        assert response.status_code == 403, (op, response.text)
