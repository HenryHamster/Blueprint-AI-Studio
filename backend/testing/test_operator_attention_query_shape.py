"""Query-shape regressions for the operator attention read model."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.message_audience import OPERATOR_ACTION_NEEDED
from app.message_convergence import LINKED_STATUS, RESOLVED_STATUS, derive_needs_action
from app.models import (
    Alert,
    AlertSeverity,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.operator_attention import (
    _local_runner_hold_preview_statement,
    build_operator_attention,
    get_operator_message_counts,
)

TEST_USER_ID = "test-user-attn-alert"


def test_local_runner_hold_union_keeps_narrow_task_projection():
    project_id = uuid.uuid4()
    statement = _local_runner_hold_preview_statement(
        (
            Task.project_id == project_id,
            Task.status == TaskStatus.QUEUED,
        ),
        (
            Task.project_id == project_id,
            Task.status == TaskStatus.PROVISIONING,
        ),
    )

    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "UNION ALL" in sql
    assert sql.count("LIMIT 4") == 2
    assert "status_log" not in sql
    assert "payloads" not in sql
    assert "api_call_summary" not in sql
    assert "fix_loop_history" not in sql


async def _seed_alert_project(db_session) -> Project:
    """Create a project (owner member) for alert-audience assertions."""
    project = Project(name="Alert audience", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    db_session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await db_session.commit()
    await db_session.refresh(project)
    return project


def _add_alert(db_session, project_id, *, severity, event_type, title):
    db_session.add(
        Alert(
            project_id=project_id,
            severity=severity,
            title=title,
            message=f"{title} body",
            event_type=event_type,
        )
    )


@pytest.mark.asyncio
async def test_strategist_internal_alerts_excluded_from_operator_attention(db_session):
    """Alert rows whose event_type is registered strategist-internal must not
    reach the operator badge count, severity aggregate, or ERROR/WARNING preview.

    Untagged (NULL event_type) legacy alerts and operator-visible alerts are
    retained. Exercises all three Alert query paths in operator_attention.py that
    now apply operator_visible_alert_filter(Alert.event_type).
    """
    project = await _seed_alert_project(db_session)

    # Operator-visible + legacy-NULL alerts are retained.
    _add_alert(
        db_session,
        project.id,
        severity=AlertSeverity.ERROR,
        event_type="task.failed",
        title="Operator-visible error",
    )
    _add_alert(
        db_session,
        project.id,
        severity=AlertSeverity.WARNING,
        event_type=None,
        title="Legacy untagged warning",
    )
    # Strategist-internal alerts must be filtered out of every operator surface.
    _add_alert(
        db_session,
        project.id,
        severity=AlertSeverity.ERROR,
        event_type="warm_pool.payoff",
        title="Internal payoff error",
    )
    _add_alert(
        db_session,
        project.id,
        severity=AlertSeverity.WARNING,
        event_type="hotfix_backmerge.size_violation",
        title="Internal size-violation warning",
    )
    await db_session.commit()

    # (b)+(c) severity aggregate + preview via build_operator_attention.
    attention = await build_operator_attention(db_session, project, TEST_USER_ID)
    counts = attention["counts"]
    assert counts["error_alerts"] == 1, "only the operator-visible ERROR alert counts"
    assert counts["warning_alerts"] == 1, "only the legacy-NULL WARNING alert counts"
    assert counts["alerts"] == 2

    alert_titles = {
        item["label"] for item in attention["items"] if item["kind"] == "alert"
    }
    assert "Operator-visible error" in alert_titles
    assert "Legacy untagged warning" in alert_titles
    assert "Internal payoff error" not in alert_titles
    assert "Internal size-violation warning" not in alert_titles

    # (a) cross-tab badge count via get_operator_message_counts.
    message_counts = await get_operator_message_counts(db_session, project.id)
    assert message_counts["active_alerts"] == 2, (
        "badge count excludes both strategist-internal alerts"
    )


def _pm(*, message_type: MessageType, status: str = "open", context: dict | None = None) -> ProjectMessage:
    msg = ProjectMessage(
        project_id=uuid.uuid4(),
        from_type="agent",
        message_type=message_type,
        status=status,
        body="test",
        context=context or {},
    )
    return msg


def test_derive_needs_action_single_source_of_truth():
    """derive_needs_action is the single predicate for operator needs-action:
    QUESTION/BLOCKER open → True; resolved → False; audience-routed open → True;
    NOTE/FEEDBACK → False; audience-routed but resolved → False.
    """
    assert derive_needs_action(_pm(message_type=MessageType.QUESTION)) is True
    assert derive_needs_action(_pm(message_type=MessageType.BLOCKER)) is True
    assert (
        derive_needs_action(_pm(message_type=MessageType.QUESTION, status=RESOLVED_STATUS))
        is False
    )
    assert (
        derive_needs_action(_pm(message_type=MessageType.BLOCKER, status=RESOLVED_STATUS))
        is False
    )
    assert derive_needs_action(_pm(message_type=MessageType.NOTE)) is False
    assert derive_needs_action(_pm(message_type=MessageType.FEEDBACK)) is False
    # Audience-routed action messages (non-QUESTION/BLOCKER type) are also needs-action.
    assert derive_needs_action(
        _pm(message_type=MessageType.NOTE, context={"audience": OPERATOR_ACTION_NEEDED})
    ) is True
    # But not when resolved.
    assert derive_needs_action(
        _pm(message_type=MessageType.NOTE, status=RESOLVED_STATUS, context={"audience": OPERATOR_ACTION_NEEDED})
    ) is False


@pytest.mark.asyncio
async def test_linked_blocker_counted_in_badge_and_attention(db_session):
    """A BLOCKER with status='linked' must appear in the operator badge count
    and the build_operator_attention preview.

    derive_needs_action() returns True for BLOCKER at any non-resolved status,
    so a linked BLOCKER remains actionable.  The badge SQL must use
    status IN ('open', 'linked') to reflect this, not status='open' only.
    """
    project = Project(name="Linked blocker test", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    db_session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )

    # Linked BLOCKER: still needs-action per derive_needs_action, status='linked'.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="agent",
            message_type=MessageType.BLOCKER,
            status=LINKED_STATUS,
            body="Linked blocker body",
            context={"audience": "operator-action-needed"},
        )
    )
    # Open QUESTION: control — must also appear.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="agent",
            message_type=MessageType.QUESTION,
            status="open",
            body="Open question body",
            context={"audience": "operator-action-needed"},
        )
    )
    await db_session.commit()
    await db_session.refresh(project)

    counts = await get_operator_message_counts(db_session, project.id)

    assert counts["blocker"] == 1, (
        f"linked BLOCKER must appear in badge count, got blocker={counts['blocker']}"
    )
    assert counts["question"] == 1, "open QUESTION must appear in badge count"
    assert counts["needs_operator"] == 2, (
        f"needs_operator must include both linked BLOCKER and open QUESTION, "
        f"got {counts['needs_operator']}"
    )

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)
    item_labels = [item["label"] for item in attention.get("items", [])]
    blocker_items = [
        item for item in attention.get("items", [])
        if item.get("reason") == "blocker"
    ]
    assert len(blocker_items) >= 1, (
        f"linked BLOCKER must appear in build_operator_attention preview, "
        f"items={item_labels}"
    )
