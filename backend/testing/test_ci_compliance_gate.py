"""CI-compliance fetch and strategic cap-bump gate tests."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select

from app import ci_compliance
from app.auth import create_agent_token
from app.models import Project, Task, TaskStatus
from app.strategic_spawning import (
    _maybe_spawn_ci_compliance_cap_bump_fix,
    _spawn_strategic_review,
    ci_compliance_qualify_gate,
)
from testing import check_size_limits as csl

_HEAD_SHA = "a" * 40
_CAPPED_REL = "main.py"
_CAP = 10


def _limits_json(cap: int = _CAP) -> str:
    return json.dumps({"backend": {_CAPPED_REL: cap}})


def _fake_reader(module_line_count: int):
    async def _reader(path, *, token, owner, repo, ref, project_id=None):
        if path == ci_compliance._MODULE_SIZE_LIMITS_PATH:
            return _limits_json()
        if path == f"backend/app/{_CAPPED_REL}":
            return "\n".join("x" for _ in range(module_line_count))
        return None

    return _reader


async def _fake_resolve(_session, _project):
    return ("token", "owner", "repo")


async def _fake_branch_sha(*_args, **_kwargs):
    return _HEAD_SHA


def _violation_result(count: int) -> dict:
    return {
        "ref": _HEAD_SHA,
        "compliant": False,
        "violations": [
            {
                "section": "backend",
                "rel": _CAPPED_REL,
                "count": count,
                "cap": _CAP,
            }
        ],
        "checked": 1,
        "limits_available": True,
    }


async def _make_project_with_agent_task(db_session, *, module_line_count: int, mocker):
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=_fake_resolve,
    )
    mocker.patch.object(
        ci_compliance.github,
        "get_repo_installation_token",
        new=mocker.AsyncMock(return_value="token"),
    )
    mocker.patch.object(ci_compliance.github, "get_branch_sha", new=_fake_branch_sha)
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(module_line_count),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=mocker.AsyncMock(return_value=[]),
    )

    project = Project(
        name="CI Compliance Gate",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test CI-compliance gate"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-ci-compliance",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(project)
    return project, task


@pytest.mark.asyncio
async def test_ci_compliance_fetch_gate_and_cap_bump_spawn(
    client, db_session, mocker
):
    """A violating automain HEAD is fetched, gated, and remediated by one task."""
    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP + 3, mocker=mocker
    )

    token = create_agent_token(str(agent_task.id), "agent-vm-ci-compliance")
    response = await client.get(
        f"/api/agent/projects/{project.id}/ci-compliance",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["head_sha"] == _HEAD_SHA
    assert body["compliant"] is False
    assert body["violation_count"] == 1
    assert body["violations"] == [
        {
            "section": "backend",
            "rel": _CAPPED_REL,
            "count": _CAP + 3,
            "cap": _CAP,
        }
    ]

    await db_session.refresh(project)
    gate = ci_compliance_qualify_gate(project, _HEAD_SHA)
    assert gate["blocked"] is True
    assert gate["violation_count"] == 1

    spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()
    assert spawned is not None

    cap_bump_task = (
        await db_session.execute(select(Task).where(Task.id == spawned.id))
    ).scalar_one()
    assert cap_bump_task.status == TaskStatus.QUEUED
    assert cap_bump_task.created_by == "ORCHESTRATOR"
    assert cap_bump_task.context["spawned_by"] == "ci_compliance_cap_bump_gate"
    assert cap_bump_task.context["head_sha"] == _HEAD_SHA
    assert cap_bump_task.first_payload["type"] == "create"
    assert "python3 backend/testing/check_size_limits.py --fix" in cap_bump_task.first_payload[
        "prompt"
    ]


@pytest.mark.asyncio
async def test_strategic_review_surfaces_ci_compliance_and_dedupes_cap_bump(
    db_session, mocker
):
    """Strategic spawn carries ci_compliance and creates only one remediation."""
    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP + 2, mocker=mocker
    )
    agent_task.status = TaskStatus.COMPLETED
    stored_result = _violation_result(_CAP + 2)
    ci_compliance.record_ci_compliance(project, stored_result)
    db_session.add(agent_task)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    async def _stored_current(_session, _project):
        return {
            "status": "success",
            "integration_branch": "automain",
            "head_sha": _HEAD_SHA,
            "data": stored_result,
            "computed": False,
        }

    mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=_stored_current,
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    await db_session.commit()

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    strategic_payload = next(
        task.first_payload
        for task in tasks
        if task.first_payload.get("type") == "strategic_review"
        and task.created_by == "ORCHESTRATOR"
    )
    assert strategic_payload["ci_compliance"]["head_sha"] == _HEAD_SHA
    assert strategic_payload["ci_compliance"]["compliant"] is False
    assert strategic_payload["ci_compliance"]["violation_count"] == 1

    cap_bump_tasks = [
        task
        for task in tasks
        if (task.context or {}).get("spawned_by") == "ci_compliance_cap_bump_gate"
    ]
    assert len(cap_bump_tasks) == 1
    assert cap_bump_tasks[0].context["head_sha"] == _HEAD_SHA


@pytest.mark.asyncio
async def test_compliant_head_fetch_gate_and_spawn_are_noops(client, db_session, mocker):
    """A compliant automain HEAD is surfaced but does not block or spawn a fix."""
    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP - 1, mocker=mocker
    )

    token = create_agent_token(str(agent_task.id), "agent-vm-ci-compliance")
    response = await client.get(
        f"/api/agent/projects/{project.id}/ci-compliance",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["compliant"] is True
    assert body["violation_count"] == 0
    assert body["violations"] == []

    await db_session.refresh(project)
    gate = ci_compliance_qualify_gate(project, _HEAD_SHA)
    assert gate["blocked"] is False
    assert gate["violations"] == []

    spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()
    assert spawned is None
    rows = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert not any(
        (task.context or {}).get("spawned_by") == "ci_compliance_cap_bump_gate"
        for task in rows
    )


@pytest.mark.asyncio
async def test_noncompliant_qualify_request_is_blocked_and_spawns_cap_bump(
    db_session, mocker
):
    """A later qualify_request cannot bypass the CI-compliance gate."""
    from app import release_version as rv
    from app.strategic_completion import _apply_qualify_request

    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP + 4, mocker=mocker
    )
    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="ready to qualify")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(project)

    run_qualification = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    await _apply_qualify_request(
        {"reason": "ready"},
        agent_task,
        project,
        db_session,
        path="test",
    )
    await db_session.commit()

    run_qualification.assert_not_awaited()
    await db_session.refresh(project)
    gate = ci_compliance_qualify_gate(project, _HEAD_SHA)
    assert gate["blocked"] is True

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    cap_bump_tasks = [
        task
        for task in tasks
        if (task.context or {}).get("spawned_by") == "ci_compliance_cap_bump_gate"
    ]
    assert len(cap_bump_tasks) == 1
    assert cap_bump_tasks[0].status == TaskStatus.QUEUED
    assert cap_bump_tasks[0].context["head_sha"] == _HEAD_SHA
    assert "python3 backend/testing/check_size_limits.py --fix" in cap_bump_tasks[
        0
    ].first_payload["prompt"]

    # Observability: the CI-compliance block is recorded durably on the version
    # row (queryable via the strategic payload / version fetch), not just logged.
    await db_session.refresh(version)
    assert version.last_qualification is not None
    blocked_trace = version.last_qualification["qualify_request_blocked"]
    assert blocked_trace["blocker"] == "ci_compliance"
    assert blocked_trace["head_sha"] == _HEAD_SHA
    assert blocked_trace["cap_bump_task"] == str(cap_bump_tasks[0].id)
    assert "qualify_request_blocked_at" in version.last_qualification


def test_cap_bump_resolves_orchestrator_runner_dispatch_breach():
    """After bumping caps via check_size_limits --fix, orchestrator_runner_dispatch
    must be within its new cap and the module_size_limits.json must be consistent
    with the actual file size on disk.

    This is a merge-test gate: it runs against the real file tree, so a PR that
    grows orchestrator_runner_dispatch.py past its cap will fail here even before
    the CI invariant suite runs.

    2026-07-12 remediation (automain HEAD 683abce3): orchestrator_runner_dispatch.py
    reached 2250 lines over its old cap of 2200. The fix bumps that cap in
    module_size_limits.json to the canonical policy value; this gate proves the
    bumped JSON is consistent with the actual file size on disk, so the test fails
    if the cap is reverted (or the file grows past the new cap) before the CI
    invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "orchestrator_runner_dispatch.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    # Resolve app/ as testing/'s sibling (same as test_invariants._check_module):
    # in the Docker test harness only the backend tree is present, so the
    # dev-checkout layout <repo>/backend/app that resolve_section_root assumes
    # does not exist there.
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(target_rel, actual_count, cap, "backend", with_verdict=False)

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_admin_release_version_breach():
    """After bumping caps via check_size_limits --fix, routers/admin_release_version.py
    must be within its new cap on the real file tree.

    This is the breach the 2026-07-12 ci-compliance remediation targets: automain
    HEAD reported routers/admin_release_version.py at 1953 lines over its old cap of
    1950. The fix bumps that cap in module_size_limits.json; this merge-test gate
    proves the bumped JSON is consistent with the actual file size on disk, so the
    test fails if the cap is reverted (or the file grows past the new cap) before
    the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "routers/admin_release_version.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    # Resolve app/ as testing/'s sibling, matching the Docker harness layout
    # (see test_cap_bump_resolves_orchestrator_runner_dispatch_breach above).
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


@pytest.mark.parametrize(
    "target_rel",
    ["autonomous_research.py", "compute_jobs.py"],
)
def test_cap_bump_resolves_d18e8b14_breaches(target_rel):
    """After bumping caps via check_size_limits --fix, the modules the 2026-07-12
    d18e8b14 ci-compliance remediation targets must be within their new caps on the
    real file tree.

    automain HEAD d18e8b14 reported backend/autonomous_research.py at 3353 lines over
    its old cap of 3350, and backend/compute_jobs.py at 3642 over its old cap of 3400
    (the latter introduced by the cross-job checkpoint-resume merge). The fix bumps
    both caps in module_size_limits.json; this merge-test gate proves the bumped JSON
    is consistent with the actual file sizes on disk, so the test fails if a cap is
    reverted (or a file grows past its new cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    # Resolve app/ as testing/'s sibling, matching the Docker harness layout
    # (see test_cap_bump_resolves_orchestrator_runner_dispatch_breach above).
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_github_refs_breach():
    """After bumping caps via check_size_limits --fix, github_refs.py must be within
    its new cap on the real file tree.

    This is the breach the 2026-07-14 ci-compliance remediation targets: automain
    HEAD f4907e36 reported backend/github_refs.py at 1042 lines over its old cap of
    1000. The fix bumps that cap in module_size_limits.json; this merge-test gate
    proves the bumped JSON is consistent with the actual file size on disk, so the
    test fails if the cap is reverted (or the file grows past the new cap) before
    the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "github_refs.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    # Resolve app/ as testing/'s sibling, matching the Docker harness layout
    # (see test_cap_bump_resolves_orchestrator_runner_dispatch_breach above).
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def _resolve_agent_src_root() -> str | None:
    """Resolve the agent/src tree, mirroring resolve_section_root's candidates.

    The dev checkout lives at <repo>/agent/src (testing/../../agent/src); the
    backend Docker test harness mounts it read-only at /agent/src (see
    scripts/test-backend.sh). Returns the first that exists, else None so the
    caller skips when the agent tree is absent (e.g. CI's backend-only Docker
    build context that mounts neither).
    """
    candidates = [
        os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "agent", "src")
        ),
        os.path.join(os.sep, "agent", "src"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


def test_cap_bump_resolves_discovery_ts_breach():
    """After bumping caps via check_size_limits --fix, agent prompts/discovery.ts
    must be within its new cap on the real file tree.

    This is the breach the 2026-07-14 ci-compliance remediation targets: automain
    HEAD 21927aa8 reported agent/prompts/discovery.ts at 1064 lines over its old
    cap of 1050. The fix bumps that cap in the agent section of
    module_size_limits.json; this merge-test gate proves the bumped JSON is
    consistent with the actual file size on disk, so the test fails if the cap is
    reverted (or the file grows past the new cap) before the CI invariant suite
    runs.

    Skips when agent/src is absent (CI's backend-only Docker build context that
    mounts neither /agent/src nor the repo), mirroring the agent-section skip in
    test_invariants.TestAgentModuleSizeGuardrails.
    """
    agent_root = _resolve_agent_src_root()
    if agent_root is None:
        pytest.skip("agent/src not available in this test environment")

    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    agent_caps = data.get("agent", {})

    target_rel = "prompts/discovery.ts"
    assert target_rel in agent_caps, (
        f"{target_rel} must have a cap entry in the agent section of "
        "module_size_limits.json"
    )

    target_path = os.path.join(agent_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = agent_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "agent", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_capped_backend_modules_within_cap_on_disk():
    """Every capped backend module must be at or under its cap on the real tree.

    This is the fleet-wide merge-test gate the cap-bump remediation exists to
    satisfy: a violating automain HEAD is remediated by bumping the breached
    cap in module_size_limits.json, and this test proves the bumped JSON is
    consistent with the actual file sizes on disk. It exercises EVERY backend
    cap entry (not just one hard-coded module), so a PR that bumps a cap below
    the file's real size — or lets a capped module grow past its cap without a
    bump — fails here against the real file tree before the invariant suite runs.

    Concretely: the admin.py / release_scoring.py cap bumps this remediation
    lands are validated here against the real files, closing the loop the
    ci_compliance gate opens.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})
    assert backend_caps, "module_size_limits.json must define backend caps"

    # Resolve app/ as testing/'s sibling, matching the Docker harness layout
    # (see test_cap_bump_resolves_orchestrator_runner_dispatch_breach above).
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    breaches = []
    for rel, cap in backend_caps.items():
        target_path = os.path.join(backend_root, rel)
        if not os.path.isfile(target_path):
            # Absent at this ref: a missing file cannot have grown past its cap
            # (mirrors classify_size_record's count-is-None semantics).
            continue
        actual_count = csl.count_lines(target_path)
        result = csl.classify_size_record(
            rel, actual_count, cap, "backend", with_verdict=False
        )
        if result["breach"]:
            breaches.append((rel, actual_count, cap))

    assert not breaches, (
        "Capped backend modules exceed their caps on disk: "
        + ", ".join(f"{rel} ({count} > {cap})" for rel, count, cap in breaches)
        + ". Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def _resolve_admin_ui_src_root() -> str | None:
    """Resolve the admin-ui/src tree, mirroring resolve_section_root's candidates.

    The dev checkout lives at <repo>/admin-ui/src (testing/../../admin-ui/src);
    the backend Docker test harness mounts it read-only at /admin-ui/src (see
    scripts/test-backend.sh). Returns the first that exists, else None so the
    caller skips when the admin-ui tree is absent (e.g. CI's backend-only Docker
    build context that mounts neither).
    """
    candidates = [
        os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "admin-ui", "src")
        ),
        os.path.join(os.sep, "admin-ui", "src"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


def test_cap_bump_resolves_models_py_breach():
    """After bumping caps via check_size_limits --fix, backend/models.py must be
    within its new cap on the real file tree.

    This is a breach the 2026-07-16 ci-compliance remediation targets: automain
    HEAD 516a94a2 reported backend/models.py at 5588 lines over its old cap of
    5550. The fix bumps that cap to 6450 in module_size_limits.json; this
    merge-test gate proves the bumped JSON is consistent with the actual file
    size on disk, so the test fails if the cap is reverted (or the file grows
    past the new cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "models.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    # Resolve app/ as testing/'s sibling, matching the Docker harness layout.
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_admin_project_strategic_session_breach():
    """After bumping caps via check_size_limits --fix,
    routers/admin_project_strategic_session.py must be within its new cap on the
    real file tree.

    This is a breach the 2026-07-16 ci-compliance remediation targets: automain
    HEAD 516a94a2 reported routers/admin_project_strategic_session.py at 3500
    lines over its old cap of 3150. The fix bumps that cap to 4050 in
    module_size_limits.json; this merge-test gate proves the bumped JSON is
    consistent with the actual file size on disk, so the test fails if the cap
    is reverted (or the file grows past the new cap) before the CI invariant
    suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "routers/admin_project_strategic_session.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_admin_notifications_breach():
    """After bumping caps via check_size_limits --fix,
    routers/admin_notifications.py must be within its new cap on the real file
    tree.

    This is a breach the 2026-07-16 ci-compliance remediation targets: automain
    HEAD 516a94a2 reported routers/admin_notifications.py at 1866 lines over its
    old cap of 1850. The fix bumps that cap to 2150 in module_size_limits.json;
    this merge-test gate proves the bumped JSON is consistent with the actual
    file size on disk, so the test fails if the cap is reverted (or the file
    grows past the new cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "routers/admin_notifications.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_messages_panel_tsx_breach():
    """After bumping caps via check_size_limits --fix,
    frontend/components/MessagesPanel.tsx must be within its new cap on the real
    file tree.

    This is a breach the 2026-07-16 ci-compliance remediation targets: automain
    HEAD 516a94a2 reported frontend/components/MessagesPanel.tsx at 2816 lines
    over its old cap of 2800. The fix bumps that cap to 3250 in
    module_size_limits.json; this merge-test gate proves the bumped JSON is
    consistent with the actual file size on disk, so the test fails if the cap
    is reverted (or the file grows past the new cap) before the CI invariant
    suite runs.

    Skips when admin-ui/src is absent (CI's backend-only Docker build context
    that mounts neither /admin-ui/src nor the repo), mirroring the frontend-skip
    in test_invariants.
    """
    admin_root = _resolve_admin_ui_src_root()
    if admin_root is None:
        pytest.skip("admin-ui/src not available in this test environment")

    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    frontend_caps = data.get("frontend", {})

    target_rel = "components/MessagesPanel.tsx"
    assert target_rel in frontend_caps, (
        f"{target_rel} must have a cap entry in the frontend section of "
        "module_size_limits.json"
    )

    target_path = os.path.join(admin_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = frontend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "frontend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_orchestrator_ci_monitor_breach():
    """After bumping caps via check_size_limits --fix, orchestrator_ci_monitor.py
    must be within its new cap on the real file tree.

    This is the breach the 2026-07-17 ci-compliance remediation targets: automain
    HEAD 5203c5fd reported backend/orchestrator_ci_monitor.py at 3069 lines over
    its old cap of 3000. The fix bumps that cap to 3550 in module_size_limits.json;
    this merge-test gate proves the bumped JSON is consistent with the actual file
    size on disk, so the test fails if the cap is reverted (or the file grows past
    the new cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "orchestrator_ci_monitor.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )

    # Resolve app/ as testing/'s sibling, matching the Docker harness layout.
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_strategic_state_api_breach():
    """After bumping caps via check_size_limits --fix,
    frontend/strategicStateApi.ts must be within its new cap on the real file
    tree.

    This is a breach the 2026-07-16 ci-compliance remediation targets: automain
    HEAD 516a94a2 reported frontend/strategicStateApi.ts at 1603 lines with no
    prior cap. The fix adds a cap of 1850 in module_size_limits.json; this
    merge-test gate proves the new cap is consistent with the actual file size
    on disk, so the test fails if the cap is reverted (or the file grows past
    the new cap) before the CI invariant suite runs.

    Skips when admin-ui/src is absent (CI's backend-only Docker build context
    that mounts neither /admin-ui/src nor the repo), mirroring the frontend-skip
    in test_invariants.
    """
    admin_root = _resolve_admin_ui_src_root()
    if admin_root is None:
        pytest.skip("admin-ui/src not available in this test environment")

    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    frontend_caps = data.get("frontend", {})

    target_rel = "strategicStateApi.ts"
    assert target_rel in frontend_caps, (
        f"{target_rel} must have a cap entry in the frontend section of "
        "module_size_limits.json"
    )

    target_path = os.path.join(admin_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = frontend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "frontend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_compute_jobs_breach():
    """After bumping caps via check_size_limits --fix, compute_jobs.py must be
    within its new cap on the real file tree.

    This is the breach the 2026-07-17 ci-compliance remediation targets: automain
    HEAD fa52e42d reported backend/compute_jobs.py at 4861 lines over its old cap
    of 4850. The fix bumps that cap to 5600 in module_size_limits.json; this
    merge-test gate proves the bumped JSON is consistent with the actual file size
    on disk, so the test fails if the cap is reverted (or the file grows past the
    new cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "compute_jobs.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] >= 5600, (
        f"{target_rel} cap must be at least 5600 (bumped from old 4850 cap); "
        f"got {backend_caps[target_rel]}"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_capability_proof_breach():
    """After bumping caps via check_size_limits --fix, capability_proof.py must be
    within its new cap on the real file tree.

    This is the breach the 2026-07-17 ci-compliance remediation targets: automain
    HEAD 9a9456fb reported backend/capability_proof.py at 2412 lines over its old
    cap of 2250. The fix bumps that cap in module_size_limits.json to 2800 (the
    canonical bump_size_limit(2412) policy value); this merge-test gate proves the
    bumped JSON is consistent with the actual file size on disk, so the test fails
    if the cap is reverted (or the file grows past the new cap) before the CI
    invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "capability_proof.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] >= 2800, (
        f"{target_rel} cap must be at least 2800 (bumped from old 2250 cap); "
        f"got {backend_caps[target_rel]}"
    )

    # Resolve app/ as testing/'s sibling, matching the Docker harness layout
    # (see test_cap_bump_resolves_orchestrator_runner_dispatch_breach above).
    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_code_change_ts_breach():
    """After bumping caps via check_size_limits --fix, agent
    handlers/code-change.ts must be within its new cap on the real file tree.

    This is the breach the 2026-07-17 ci-compliance remediation targets: automain
    HEAD edce081e reported agent/handlers/code-change.ts at 601 lines over its old
    cap of 600. The fix bumps that cap to 850 in the agent section of
    module_size_limits.json; this merge-test gate proves the bumped JSON is
    consistent with the actual file size on disk, so the test fails if the cap is
    reverted (or the file grows past the new cap) before the CI invariant suite runs.

    Skips when agent/src is absent (CI's backend-only Docker build context that
    mounts neither /agent/src nor the repo), mirroring the agent-section skip in
    test_invariants.TestAgentModuleSizeGuardrails.
    """
    agent_root = _resolve_agent_src_root()
    if agent_root is None:
        pytest.skip("agent/src not available in this test environment")

    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    agent_caps = data.get("agent", {})

    target_rel = "handlers/code-change.ts"
    assert target_rel in agent_caps, (
        f"{target_rel} must have a cap entry in the agent section of "
        "module_size_limits.json"
    )
    assert agent_caps[target_rel] >= 850, (
        f"{target_rel} cap must be at least 850 (bumped from old 600 cap); "
        f"got {agent_caps[target_rel]}"
    )

    target_path = os.path.join(agent_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = agent_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "agent", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_migration_apply_gate_breach():
    """After bumping caps via check_size_limits --fix, migration_apply_gate.py must
    be within its new cap on the real file tree.

    This is the breach the 2026-07-17 ci-compliance remediation targets: automain
    HEAD edce081e reported backend/migration_apply_gate.py at 1851 lines with no
    prior cap. The fix adds a cap of 2150 in module_size_limits.json; this
    merge-test gate proves the new cap is consistent with the actual file size on
    disk, so the test fails if the cap is reverted (or the file grows past the new
    cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "migration_apply_gate.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] >= 2150, (
        f"{target_rel} cap must be at least 2150 (new cap added for uncapped file); "
        f"got {backend_caps[target_rel]}"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_reaper_sweeps_breach():
    """After bumping caps via check_size_limits --fix, reaper_sweeps.py must be
    within its new cap on the real file tree.

    This is the breach the 2026-07-19 ci-compliance remediation (4a13d25f) targets:
    automain HEAD 4a13d25f reported backend/reaper_sweeps.py at 3467 lines over its
    old cap of 3450. The fix bumps that cap to 4000 in module_size_limits.json; this
    merge-test gate proves the bumped JSON is consistent with the actual file size on
    disk, so the test fails if the cap is reverted (or the file grows past the new
    cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "reaper_sweeps.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] >= 4000, (
        f"{target_rel} cap must be at least 4000 (bumped from old 3450 cap); "
        f"got {backend_caps[target_rel]}"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_admin_task_mutations_breach():
    """After bumping caps via check_size_limits --fix, admin_task_mutations.py must
    be within its new cap on the real file tree.

    This is the breach the 2026-07-17 ci-compliance remediation targets: automain
    HEAD 46bb84f6 reported backend/admin_task_mutations.py at 1426 lines over its old
    cap of 1350. The fix bumps that cap to 1650 in module_size_limits.json; this
    merge-test gate proves the bumped JSON is consistent with the actual file size on
    disk, so the test fails if the cap is reverted (or the file grows past the new
    cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "admin_task_mutations.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] >= 1650, (
        f"{target_rel} cap must be at least 1650 (bumped from old 1350 cap); "
        f"got {backend_caps[target_rel]}"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_metrics_integrity_sweep_breach():
    """After bumping caps via check_size_limits --fix, metrics_integrity_sweep.py
    must be within its new cap on the real file tree.

    This is the breach the 2026-07-17 ci-compliance remediation targets: automain
    HEAD 358c9b81 reported backend/metrics_integrity_sweep.py at 5622 lines over
    its old cap of 5550. The fix bumps that cap to 6500 in module_size_limits.json;
    this merge-test gate proves the bumped JSON is consistent with the actual file
    size on disk, so the test fails if the cap is reverted (or the file grows past
    the new cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "metrics_integrity_sweep.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] >= 6500, (
        f"{target_rel} cap must be at least 6500 (bumped from old 5550 cap); "
        f"got {backend_caps[target_rel]}"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_agent_status_decomposition_resolves_size_breach():
    """The resident retry extraction must keep agent_status at or below 4550.

    Automain b86e3441 reported routers/agent_status.py at 4593 lines, above its
    4550 cap. The Workday qualification repair decomposes the resident retry
    binding into its own module, so the original stronger cap remains enforceable.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "routers/agent_status.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] == 4550, (
        f"{target_rel} must retain the stronger 4550 cap after decomposition; "
        f"got {backend_caps[target_rel]}"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    assert actual_count <= 4550, (
        f"{target_rel} decomposition must keep the file at or below 4550 lines; "
        f"got {actual_count}"
    )
    assert actual_count <= cap
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_cap_bump_resolves_strategic_resident_session_breach():
    """After bumping caps via check_size_limits --fix,
    strategic_resident_session.py must be within its new cap on the real file
    tree.

    This is the breach the 2026-07-22 ci-compliance remediation (b86e3441) targets:
    automain HEAD b86e3441 reported backend/strategic_resident_session.py at 3615
    lines with no prior cap. The fix adds a cap of 4200 in module_size_limits.json;
    this merge-test gate proves the new cap is consistent with the actual file size
    on disk, so the test fails if the cap is reverted (or the file grows past the
    new cap) before the CI invariant suite runs.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    backend_caps = data.get("backend", {})

    target_rel = "strategic_resident_session.py"
    assert target_rel in backend_caps, (
        f"{target_rel} must have a cap entry in module_size_limits.json"
    )
    assert backend_caps[target_rel] >= 4200, (
        f"{target_rel} cap must be at least 4200 (new cap added for uncapped file); "
        f"got {backend_caps[target_rel]}"
    )

    backend_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    assert os.path.isdir(backend_root), "backend app/ must exist next to testing/"

    target_path = os.path.join(backend_root, target_rel)
    assert os.path.isfile(target_path), f"{target_path} must exist on disk"

    actual_count = csl.count_lines(target_path)
    cap = backend_caps[target_rel]
    result = csl.classify_size_record(
        target_rel, actual_count, cap, "backend", with_verdict=False
    )

    assert not result["breach"], (
        f"{target_rel} has {actual_count} lines which exceeds its cap of {cap}. "
        "Run `python3 backend/testing/check_size_limits.py --fix` and commit "
        "the updated module_size_limits.json."
    )


def test_test_files_within_size_limits():
    """Prove that the ``testing`` section of module_size_limits.json exists and
    is non-empty, and that every test file listed there is actually within its
    cap on disk.

    This gate exercises the seam between the new ``testing`` section in
    module_size_limits.json, the ``resolve_section_root`` branch for
    ``"testing"`` in check_size_limits.py, and the per-file cap enforcement in
    TestTestingSizeGuardrails (test_invariants.py). A non-empty testing section
    with at least one file within its cap means all three components are wired up
    correctly.
    """
    json_path = os.path.join(os.path.dirname(__file__), "module_size_limits.json")
    data = csl.load_limits(json_path)
    testing_caps = data.get("testing", {})

    assert testing_caps, (
        "module_size_limits.json must define a non-empty 'testing' section. "
        "Run `python3 backend/testing/check_size_limits.py --fix --sections testing` "
        "and commit the updated JSON."
    )

    testing_root = os.path.normpath(os.path.dirname(__file__))
    assert os.path.isdir(testing_root), "backend/testing/ must exist"

    breaches = []
    for rel, cap in testing_caps.items():
        path = os.path.join(testing_root, rel)
        if not os.path.isfile(path):
            continue
        count = csl.count_lines(path)
        result = csl.classify_size_record(rel, count, cap, "testing", with_verdict=False)
        if result["breach"]:
            breaches.append((rel, count, cap))

    assert not breaches, (
        f"{len(breaches)} test file(s) exceed their cap in module_size_limits.json:\n"
        + "\n".join(
            f"  {rel}: {count} lines (cap: {cap})" for rel, count, cap in breaches
        )
        + "\nRun `python3 backend/testing/check_size_limits.py --fix --sections testing` "
        "and commit the updated module_size_limits.json."
    )
