"""Tests for /api/agent/annotate, including the size_breach annotation type.

The size_breach flow lets a failing test invariant route a structured
breach signal to the strategic agent (via ProjectMessage) so the create/
fix agent doesn't have to reason about whether to refactor — that's a
strategic decision. Server returns the policy-driven new_limit so the
agent can apply it to module_size_limits.json without any judgment of
its own.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.auth import create_agent_token
from app.main import _bump_size_limit
from app.models import (
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)


@pytest.mark.asyncio
async def test_size_breach_creates_structured_message_and_returns_new_limit(
    client: AsyncClient,
    db_session,
):
    """size_breach annotation persists structured fields in ProjectMessage.context
    and returns new_limit so the agent can apply it to the JSON."""
    project = Project(name="Size Breach Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-sb1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-sb1", task_type="create")
    resp = await client.post(
        "/api/agent/annotate",
        json={
            "task_id": str(task.id),
            "annotation_type": "size_breach",
            # AlertsBanner.tsx has no cap-bump history, so this deterministically
            # exercises the routine path in both full and shallow checkouts.
            # (A historically-bumped file like DashboardHome.tsx is a genuine
            # chronic offender once full history is visible.)
            "file_path": "components/AlertsBanner.tsx",
            "current_size": 1720,
            "change_summary": "Added telemetry panel for token usage per task type",
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["new_limit"] == _bump_size_limit(1720)

    # Persisted as a FEEDBACK ProjectMessage with structured context.
    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.message_type == MessageType.FEEDBACK
    assert msg.from_type == "agent"
    assert msg.context["annotation_type"] == "size_breach"
    assert msg.context["file_path"] == "components/AlertsBanner.tsx"
    assert msg.context["current_size"] == 1720
    assert msg.context["new_limit"] == body["new_limit"]
    assert "telemetry panel" in msg.context["change_summary"]


@pytest.mark.asyncio
async def test_size_breach_governed_module_directs_paired_source_cap_repair(
    client: AsyncClient,
    db_session,
    mocker,
):
    """A governed cap repair must direct the JSON and source-constant edits."""
    project = Project(name="Governed Size Breach", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-governed-size",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-governed-size", task_type="fix")
    mocker.patch("app.size_limit_policy.parse_bump_history", return_value=[])
    resp = await client.post(
        "/api/agent/annotate",
        json={
            "task_id": str(task.id),
            "annotation_type": "size_breach",
            "file_path": "release_qualifier.py",
            "current_size": 10400,
            "change_summary": "Added a governed qualification check",
            "section": "backend",
        },
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The governed module clamps to its declared constant (10,649) instead of
    # taking generic +15% headroom (11,950); the constant itself never grows.
    assert body["new_limit"] == 10649
    expected_repair = {
        "source_path": "backend/app/release_qualifier.py",
        "constant_name": "_MODULE_SIZE_GOVERNANCE_CAP",
        "new_limit": 10649,
    }
    assert body["governing_cap_repair"] == expected_repair

    msg = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == task.id)
            )
        )
        .scalars()
        .one()
    )
    assert msg.context["governing_cap_repair"] == expected_repair
    assert "_MODULE_SIZE_GOVERNANCE_CAP" in msg.content


@pytest.mark.asyncio
async def test_size_breach_rejects_missing_required_fields(
    client: AsyncClient,
    db_session,
):
    project = Project(name="Size Breach Missing", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-sb2",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), "agent-vm-sb2", task_type="create")

    def _err_message(resp) -> str:
        body = resp.json()
        return body.get("detail") or body.get("error", {}).get("message") or ""

    # Missing file_path.
    resp = await client.post(
        "/api/agent/annotate",
        json={
            "task_id": str(task.id),
            "annotation_type": "size_breach",
            "current_size": 1720,
            "change_summary": "x",
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400
    assert "file_path" in _err_message(resp)

    # Missing current_size.
    resp = await client.post(
        "/api/agent/annotate",
        json={
            "task_id": str(task.id),
            "annotation_type": "size_breach",
            "file_path": "x.py",
            "change_summary": "x",
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400
    assert "current_size" in _err_message(resp)

    # Missing change_summary.
    resp = await client.post(
        "/api/agent/annotate",
        json={
            "task_id": str(task.id),
            "annotation_type": "size_breach",
            "file_path": "x.py",
            "current_size": 1720,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400
    assert "change_summary" in _err_message(resp)


@pytest.mark.asyncio
async def test_existing_annotation_types_still_work(
    client: AsyncClient,
    db_session,
):
    """Regression: size_breach addition must not break comment/issue/vision."""
    project = Project(name="Annotation Backcompat", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-sb3",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), "agent-vm-sb3", task_type="create")

    for ann_type, expected_msg_type in (
        ("comment", MessageType.NOTE),
        ("issue", MessageType.BUG_REPORT),
        ("vision", MessageType.FEEDBACK),
    ):
        resp = await client.post(
            "/api/agent/annotate",
            json={
                "task_id": str(task.id),
                "annotation_type": ann_type,
                "content": f"some {ann_type} text",
            },
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 200, f"{ann_type}: {resp.text}"

    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 3
    by_type = {m.context["annotation_type"]: m for m in msgs}
    assert by_type["comment"].message_type == MessageType.NOTE
    assert by_type["issue"].message_type == MessageType.BUG_REPORT
    assert by_type["vision"].message_type == MessageType.FEEDBACK


@pytest.mark.asyncio
async def test_size_breach_chronic_offender_no_new_limit(
    client: AsyncClient,
    db_session,
    mocker,
):
    """Chronic offender size_breach: response must include is_chronic=True and
    omit new_limit so the agent knows decomposition is required."""
    from datetime import datetime, timedelta, timezone

    from app.size_limit_policy import CHRONIC_WINDOW_DAYS, BumpRecord

    project = Project(name="Chronic Offender Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-chronic",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-chronic", task_type="create")

    def _rec(old, new, days_ago):
        date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return BumpRecord(
            commit_sha=f"sha{days_ago}", commit_date=date, old_cap=old, new_cap=new
        )

    chronic_history = [
        _rec(1000, 1200, CHRONIC_WINDOW_DAYS - 10),
        _rec(1200, 1400, CHRONIC_WINDOW_DAYS - 60),
        _rec(1400, 1600, 30),
    ]

    with mocker.patch(
        "app.size_limit_policy.parse_bump_history", return_value=chronic_history
    ):
        resp = await client.post(
            "/api/agent/annotate",
            json={
                "task_id": str(task.id),
                "annotation_type": "size_breach",
                "file_path": "orchestrator.py",
                "current_size": 1720,
                "change_summary": "Added monitoring hooks",
                "section": "backend",
            },
            headers={"x-agent-token": token},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    # Chronic offender: must flag is_chronic, must NOT return new_limit.
    assert body.get("is_chronic") is True, f"Expected is_chronic=True, got: {body}"
    assert "new_limit" not in body, (
        f"Chronic offender must not return new_limit: {body}"
    )
    assert "bump_count" in body

    # Persisted message must describe decomposition, not offer a cap bump.
    msgs = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.from_task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.context["is_chronic"] is True
    assert "new_limit" not in msg.context
    assert "chronic" in msg.content.lower()


@pytest.mark.asyncio
async def test_size_breach_force_chronic_override_grants_bump(
    client: AsyncClient,
    db_session,
    mocker,
):
    """force_chronic_override=True on a chronic offender must still grant new_limit."""
    from datetime import datetime, timedelta, timezone

    from app.size_limit_policy import CHRONIC_WINDOW_DAYS, BumpRecord

    project = Project(name="Force Chronic Override", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-force",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-force", task_type="create")

    def _rec(old, new, days_ago):
        date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return BumpRecord(
            commit_sha=f"sha{days_ago}", commit_date=date, old_cap=old, new_cap=new
        )

    chronic_history = [
        _rec(1000, 1200, CHRONIC_WINDOW_DAYS - 10),
        _rec(1200, 1400, CHRONIC_WINDOW_DAYS - 60),
        _rec(1400, 1600, 30),
    ]

    with mocker.patch(
        "app.size_limit_policy.parse_bump_history", return_value=chronic_history
    ):
        resp = await client.post(
            "/api/agent/annotate",
            json={
                "task_id": str(task.id),
                "annotation_type": "size_breach",
                "file_path": "orchestrator.py",
                "current_size": 1720,
                "change_summary": "Operator-approved bump",
                "section": "backend",
                "force_chronic_override": True,
            },
            headers={"x-agent-token": token},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "new_limit" in body, f"Override must grant new_limit: {body}"
    assert body["new_limit"] == _bump_size_limit(1720)


def test_bump_size_limit_policy():
    """The policy is max(current+200, current*1.15) rounded up to nearest 50."""
    # current=1720: floor=1920, proportional=1978, max=1978 → 2000
    assert _bump_size_limit(1720) == 2000
    # current=1000: floor=1200, proportional=1150, max=1200 → 1200
    assert _bump_size_limit(1000) == 1200
    # current=5000: floor=5200, proportional=5750, max=5750 → 5750
    assert _bump_size_limit(5000) == 5750
    # current=200: floor=400, proportional=230, max=400 → 400
    assert _bump_size_limit(200) == 400
    # Always rounded to multiple of 50.
    for n in (100, 250, 333, 1001, 1250, 1875):
        assert _bump_size_limit(n) % 50 == 0


def test_size_limits_json_loaded_into_test_invariants():
    """test_invariants reads limits from module_size_limits.json — neither dict
    can be empty if the JSON loaded correctly."""
    from testing.test_invariants import MODULE_SIZE_LIMITS, FRONTEND_SIZE_LIMITS

    assert "main.py" in MODULE_SIZE_LIMITS
    assert "components/DashboardHome.tsx" in FRONTEND_SIZE_LIMITS
    assert isinstance(MODULE_SIZE_LIMITS["main.py"], int)
    assert isinstance(FRONTEND_SIZE_LIMITS["components/DashboardHome.tsx"], int)
