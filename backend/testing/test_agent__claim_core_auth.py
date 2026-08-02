# AUTO-SPLIT from test_agent.py — see refactor: split mega backend test files.
# Behavior-preserving: conftest.py fixtures (db_session, client, override_get_session) are
# inherited automatically; shared imports, fixtures, and helpers are re-exported by
# _agent_testhelpers.py. This file holds the claim core + auth domain slice: claim success,
# token validation, JWT scoping, VM-scoped rejection, capability slice, migration
# reservation, cross-claim prevention, terminal-task rejection, ancestor chain query.
"""Domain slice: claim core + auth

Split from test_agent.py. Covers the /api/agent/claim endpoint — success paths,
token validation, JWT scoping (task-scoped vs VM-scoped), capability slice,
migration reservation inheritance, cross-claim prevention, terminal-task 409,
and ancestor-chain CTE query."""
from _agent_testhelpers import *  # noqa: F403


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_claim_success(client: AsyncClient, db_session):
    """
    Tests an agent successfully claiming a QUEUED task.
    """
    project = Project(name="Agent Claim Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, payloads=[{"cmd": "ls"}], vm_instance_id="agent-vm-999"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Generate a valid JWT for this task/VM
    token = create_agent_token(str(task.id), "agent-vm-999")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] is not None
    assert data["title"] is not None
    assert "payloads" in data
    assert "task_capability" not in data

    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_agent_claim_exposes_bounded_discovery_cycle_lineage(
    client: AsyncClient, db_session
):
    """A cycle-owned task receives the lineage and bounded prompt mode."""
    from app.models import DiscoveryCycle

    project = Project(name="Discovery Claim Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = DiscoveryCycle(
        project_id=project.id,
        direction_key="d-agent-claim",
        outcome_kind="empirical",
    )
    db_session.add(cycle)
    await db_session.commit()
    await db_session.refresh(cycle)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "answer the bounded bet"}],
        vm_instance_id="agent-vm-discovery-claim",
        discovery_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-discovery-claim")
    resp = await client.post(
        "/api/agent/claim", headers={"x-agent-token": token}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["discovery_cycle_id"] == str(cycle.id)
    assert data["discovery_mode"] == "bounded_cycle"
    assert data["payloads"][0]["discovery_cycle_id"] == str(cycle.id)
    assert data["payloads"][0]["discovery_mode"] == "bounded_cycle"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_claim_unauthorized(client: AsyncClient):
    """
    Tests that missing or invalid token returns 401.
    """
    resp = await client.post(
        "/api/agent/claim", headers={"x-agent-token": "wrong-token"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_agent_claim_missing_task_row_returns_401(client: AsyncClient):
    """Claim auth fails closed when the JWT points at a missing task row."""
    token = create_agent_token(
        "00000000-0000-0000-0000-000000000000", "non-existent-vm"
    )
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_agent_claim_scoped_to_jwt_task(client: AsyncClient, db_session):
    """With multiple tasks pinned to one shared vm_instance_id (persistent
    runner pattern), a claim returns exactly the JWT's task — never an
    arbitrary sibling. Regression: the unscoped claim let the agent spawned
    for task B grab older task A, then every follow-up call 403'd
    "Task ID mismatch" and the agent crash-looped (2026-07-11 pile-up)."""
    project = Project(name="Claim Scoping Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner_vm = "runner:dd59526e-78f1-44ad-913e-85c72cd41151"
    task_a = Task(
        project_id=project.id, payloads=[{"cmd": "a"}], vm_instance_id=runner_vm
    )
    db_session.add(task_a)
    await db_session.commit()
    task_b = Task(
        project_id=project.id, payloads=[{"cmd": "b"}], vm_instance_id=runner_vm
    )
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    token_b = create_agent_token(str(task_b.id), runner_vm)
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token_b})

    assert resp.status_code == 200
    assert resp.json()["task_id"] == str(task_b.id)

    await db_session.refresh(task_a)
    await db_session.refresh(task_b)
    assert task_b.status == TaskStatus.IN_PROGRESS
    assert task_a.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_agent_claim_rejects_taskless_vm_scoped_token(
    client: AsyncClient, db_session
):
    """A VM-scoped (taskless) token cannot claim: /claim requires a task sub."""
    project = Project(name="Claim VM-Scoped Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-pool-taskless"
    task = Task(
        project_id=project.id, payloads=[{"cmd": "ls"}], vm_instance_id=vm_id
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_vm_scoped_token(vm_id, str(project.id))
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 403
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_agent_claim_includes_task_capability_slice(
    client: AsyncClient, db_session
):
    project = Project(name="Agent Claim Capability Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "cmd": "ls"}],
        vm_instance_id="agent-vm-capability",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-capability")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 200
    data = resp.json()
    expected = {entry["id"]: entry for entry in task_type_catalog()}["create"]
    assert data["task_capability"] == expected


@pytest.mark.asyncio
async def test_agent_claim_surfaces_reserved_migration_revision(
    client: AsyncClient, db_session
):
    """A migration task with an ACTIVE reservation surfaces it on the claim.

    Proves the reserved identifiers reach the agent end to end: the orchestrator
    locks a provisional revision, and the claim response carries it verbatim so
    the agent consumes it instead of deriving max+1 locally.
    """
    from app.models import MigrationReservation, MigrationReservationStatus

    project = Project(name="Agent Claim Reservation Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "migrate", "cmd": "ls"}],
        vm_instance_id="agent-vm-reservation",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    reservation = MigrationReservation(
        project_id=project.id,
        task_id=task.id,
        provisional_revision="0153",
        provisional_down_revision="0151",
        status=MigrationReservationStatus.ACTIVE,
    )
    db_session.add(reservation)
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-reservation")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 200
    data = resp.json()
    assert data["migration_reservation"] == {
        "provisional_revision": "0153",
        "provisional_down_revision": "0151",
    }


@pytest.mark.asyncio
async def test_agent_claim_fix_task_inherits_reservation_via_original_task_id(
    client: AsyncClient, db_session, mocker
):
    """A fix task inherits the create task's reservation in the real spawn topology.

    Regression for the sibling-topology bug: review/fix tasks are spawned with
    ``parent_task_id`` set to the create task's plan/research parent (so they are
    SIBLINGS of the create task, not its children). The parent-walk therefore
    never reaches the create task that holds the MigrationReservation; inheritance
    must come from ``payload.original_task_id``. This exercises the full claim
    handler end to end so a regression in the seeding logic is caught.
    """
    from app.models import MigrationReservation, MigrationReservationStatus

    project = Project(
        name="Agent Claim Fix Inherit",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Plan/research parent that both the create and fix task hang off of.
    plan_task = Task(
        project_id=project.id,
        payloads=[{"type": "plan"}],
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    # The original create task: child of the plan, holds the reservation.
    create_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        parent_task_id=plan_task.id,
    )
    db_session.add(create_task)
    await db_session.commit()
    await db_session.refresh(create_task)

    reservation = MigrationReservation(
        project_id=project.id,
        task_id=create_task.id,
        provisional_revision="0207",
        provisional_down_revision="0205",
        status=MigrationReservationStatus.ACTIVE,
    )
    db_session.add(reservation)
    await db_session.commit()

    # The fix task: SIBLING of the create task (parent is the plan, NOT the
    # create task), carrying original_task_id pointing at the create task.
    fix_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 42,
                "original_task_id": str(create_task.id),
            }
        ],
        context={
            "review_loop": {
                "fix_target": {
                    "project_id": str(project.id),
                    "pr_number": 42,
                    "base_sha": "base-42",
                    "head_sha": "head-42",
                }
            }
        },
        parent_task_id=plan_task.id,
        vm_instance_id="agent-vm-fix-inherit",
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    # Sanity: the parent walk alone never reaches the reservation holder, and the
    # fix task has no reservation of its own.
    assert fix_task.parent_task_id == plan_task.id
    assert fix_task.parent_task_id != create_task.id

    mocker.patch(
        "app.review_claim_fence.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.review_claim_fence.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-42"},
                "head": {"sha": "head-42"},
            }
        ),
    )

    token = create_agent_token(str(fix_task.id), "agent-vm-fix-inherit")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 200
    assert resp.json()["migration_reservation"] == {
        "provisional_revision": "0207",
        "provisional_down_revision": "0205",
    }


@pytest.mark.asyncio
async def test_agent_claim_review_task_inherits_reservation_via_review_loop_context(
    client: AsyncClient, db_session, mocker
):
    """A review task inherits the reservation via context.review_loop.original_task_id.

    Complements the fix-task test: the original_task_id is carried in
    ``context['review_loop']`` (as review_pipeline propagates it) rather than the
    payload, and the review task is again a sibling of the create task.
    """
    from app.models import MigrationReservation, MigrationReservationStatus

    project = Project(
        name="Agent Claim Review Inherit",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    plan_task = Task(project_id=project.id, payloads=[{"type": "plan"}])
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    create_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        parent_task_id=plan_task.id,
    )
    db_session.add(create_task)
    await db_session.commit()
    await db_session.refresh(create_task)

    reservation = MigrationReservation(
        project_id=project.id,
        task_id=create_task.id,
        provisional_revision="0211",
        provisional_down_revision="0209",
        status=MigrationReservationStatus.ACTIVE,
    )
    db_session.add(reservation)
    await db_session.commit()

    review_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 7}],
        context={
            "review_loop": {
                "original_task_id": str(create_task.id),
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 7,
                    "base_sha": "base-7",
                    "head_sha": "head-7",
                },
            }
        },
        parent_task_id=plan_task.id,
        vm_instance_id="agent-vm-review-inherit",
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    mocker.patch(
        "app.review_claim_fence.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.review_claim_fence.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-7"},
                "head": {"sha": "head-7"},
            }
        ),
    )

    token = create_agent_token(str(review_task.id), "agent-vm-review-inherit")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 200
    assert resp.json()["migration_reservation"] == {
        "provisional_revision": "0211",
        "provisional_down_revision": "0209",
    }


@pytest.mark.asyncio
async def test_pr_review_idempotent_claim_replay_rechecks_live_fence(
    client: AsyncClient, db_session, mocker
):
    project = Project(
        name="PR claim replay fence",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    target = {
        "project_id": str(project.id),
        "pr_number": 8,
        "base_sha": "base-8",
        "head_sha": "head-8",
    }
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 8}],
        context={"review_loop": {"review_target": target}},
        vm_instance_id="agent-vm-pr-replay",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    mocker.patch(
        "app.review_claim_fence.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    live_mock = mocker.patch(
        "app.review_claim_fence.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-8"},
                "head": {"sha": "head-8"},
            }
        ),
    )
    token = create_agent_token(str(task.id), "agent-vm-pr-replay")
    headers = {"x-agent-token": token}

    first = await client.post("/api/agent/claim", headers=headers)
    replay = await client.post("/api/agent/claim", headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["task_id"] == str(task.id)
    assert live_mock.await_count == 2


@pytest.mark.asyncio
async def test_agent_claim_omits_reservation_when_none(client: AsyncClient, db_session):
    """A task with no reservation omits the key (backward-compatible claim)."""
    project = Project(name="Agent Claim No Reservation", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "migrate", "cmd": "ls"}],
        vm_instance_id="agent-vm-no-reservation",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-reservation")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 200
    assert "migration_reservation" not in resp.json()


@pytest.mark.asyncio
async def test_agent_claim_scoped_to_token_sub_prevents_cross_claim(
    client: AsyncClient, db_session,
):
    """
    When multiple QUEUED/PROVISIONING tasks share the same vm_instance_id
    (e.g. local-runner concurrent tasks all pinned to "runner:<id>"), claim_task
    must scope the primary SELECT by the token's ``sub`` (task id) so an agent
    whose token authorizes Task A does not accidentally claim Task B. Without
    this scoping, the vm_instance_id-only SELECT returns whichever row FOR UPDATE
    SKIP LOCKED picks first, causing every subsequent task-scoped call to 403
    with "Task ID mismatch" and the agent self-terminates.
    """
    project = Project(name="Cross-Claim Guard", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    shared_vm = "runner:test-123"

    task_a = Task(
        project_id=project.id,
        payloads=[{"cmd": "task_a"}],
        vm_instance_id=shared_vm,
        status=TaskStatus.PROVISIONING,
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    task_b = Task(
        project_id=project.id,
        payloads=[{"cmd": "task_b"}],
        vm_instance_id=shared_vm,
        status=TaskStatus.PROVISIONING,
    )
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_b)

    # Token scoped to task_b — claim must return task_b, not task_a
    token = create_agent_token(str(task_b.id), shared_vm)
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    claimed_id = resp.json()["task_id"]
    assert claimed_id == str(task_b.id), (
        f"Claim returned task {claimed_id} instead of the token's task {task_b.id}"
    )


@pytest.mark.asyncio
async def test_agent_claim_returns_409_when_jwt_task_is_terminal(
    client: AsyncClient, db_session
):
    """When the JWT's task exists but is terminal (cancelled/reaped/doomed),
    claim must return 409 — not 404 and not a different task.

    Regression: before the scoping fix, claim returned a DIFFERENT task (the
    oldest pinned sibling). After the scoping fix, it now correctly checks only
    the JWT's task; when that task is no longer claimable the response should be
    409 so the agent knows to stop retrying rather than treating it as a routing
    error.
    """
    project = Project(name="Claim Terminal 409", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    shared_vm = "runner:term-409-test"

    # Older task pinned to the same runner — still claimable.
    task_old = Task(
        project_id=project.id,
        payloads=[{"cmd": "old"}],
        vm_instance_id=shared_vm,
        status=TaskStatus.PROVISIONING,
    )
    db_session.add(task_old)
    await db_session.commit()
    await db_session.refresh(task_old)

    # Newer task pinned to the same runner — starts PROVISIONING, then goes terminal.
    task_new = Task(
        project_id=project.id,
        payloads=[{"cmd": "new"}],
        vm_instance_id=shared_vm,
        status=TaskStatus.PROVISIONING,
    )
    db_session.add(task_new)
    await db_session.commit()
    await db_session.refresh(task_new)

    # JWT is scoped to the newer task.
    token = create_agent_token(str(task_new.id), shared_vm)
    headers = {"x-agent-token": token}

    # Sanity: before the task moves off PROVISIONING, claim succeeds and returns the newer task.
    resp = await client.post("/api/agent/claim", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["task_id"] == str(task_new.id)

    # Now move the newer task to EVALUATING (non-terminal, so verify_agent_token
    # passes, but no longer QUEUED/PROVISIONING and outside the IN_PROGRESS
    # idempotency window — simulates a task that was claimed and then handed to
    # evaluation, after which the agent's retry must not grab the older sibling).
    task_new.status = TaskStatus.EVALUATING
    # Clear the heartbeat so the IN_PROGRESS idempotency replay window doesn't fire.
    task_new.last_heartbeat = _utcnow() - timedelta(minutes=10)
    db_session.add(task_new)
    await db_session.commit()

    # The JWT still has the newer task's sub. Claim must return 409, not 404
    # (task exists but not claimable) and not 200 (must not return the older task).
    resp2 = await client.post("/api/agent/claim", headers=headers)
    assert resp2.status_code == 409, (
        f"Expected 409 for non-claimable task but got {resp2.status_code}: {resp2.text}"
    )
    # The older pinned task must remain untouched.
    await db_session.refresh(task_old)
    assert task_old.status == TaskStatus.PROVISIONING


@pytest.mark.asyncio
async def test_agent_claim_ancestor_chain_fetched_in_single_query(
    client: AsyncClient, db_session
):
    """Ancestor walk uses a recursive CTE — all levels are found in one round-trip.

    Builds a grandparent -> parent -> task chain; the grandparent holds the
    reservation. The claim must surface the reservation even though it is two
    hops away, exercising that the CTE walks the full chain.
    """
    from app.models import MigrationReservation, MigrationReservationStatus

    project = Project(name="Agent Claim Ancestor Chain", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    grandparent_task = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
    )
    db_session.add(grandparent_task)
    await db_session.commit()
    await db_session.refresh(grandparent_task)

    reservation = MigrationReservation(
        project_id=project.id,
        task_id=grandparent_task.id,
        provisional_revision="0301",
        provisional_down_revision="0299",
        status=MigrationReservationStatus.ACTIVE,
    )
    db_session.add(reservation)
    await db_session.commit()

    parent_task = Task(
        project_id=project.id,
        payloads=[{"type": "plan"}],
        parent_task_id=grandparent_task.id,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    leaf_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        parent_task_id=parent_task.id,
        vm_instance_id="agent-vm-ancestor-chain",
    )
    db_session.add(leaf_task)
    await db_session.commit()
    await db_session.refresh(leaf_task)

    token = create_agent_token(str(leaf_task.id), "agent-vm-ancestor-chain")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 200
    assert resp.json()["migration_reservation"] == {
        "provisional_revision": "0301",
        "provisional_down_revision": "0299",
    }
