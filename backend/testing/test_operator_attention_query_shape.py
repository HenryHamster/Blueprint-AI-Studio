"""Query-shape regressions for the operator attention read model."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.message_audience import OPERATOR_ACTION_NEEDED, OPERATOR_FYI
from app.message_convergence import LINKED_STATUS, RESOLVED_STATUS, derive_needs_action
from app.models import (
    Alert,
    AlertSeverity,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    ReleaseVersion,
    Task,
    TaskStatus,
)
from app.operator_attention import (
    _local_runner_hold_preview_statement,
    build_operator_attention,
    get_operator_message_counts,
)
from app.time_utils import _utcnow

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
async def test_strategist_internal_and_null_alerts_excluded_from_operator_attention(db_session):
    """Alert rows whose event_type is NULL, unregistered, or registered
    strategist-internal must not reach the operator badge count, severity
    aggregate, or ERROR/WARNING preview.

    operator_visible_alert_filter is fail-closed: only rows whose event_type is
    explicitly registered as operator-visible pass through.  NULL and unregistered
    event_types are excluded — they are not legacy-retained.
    Exercises all three Alert query paths in operator_attention.py.
    """
    project = await _seed_alert_project(db_session)

    # Operator-visible alert: must be retained.
    _add_alert(
        db_session,
        project.id,
        severity=AlertSeverity.ERROR,
        event_type="task.failed",
        title="Operator-visible error",
    )
    # NULL event_type: must be EXCLUDED (fail-closed, not legacy-retained).
    _add_alert(
        db_session,
        project.id,
        severity=AlertSeverity.WARNING,
        event_type=None,
        title="Untagged null warning",
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
    assert counts["warning_alerts"] == 0, (
        "NULL event_type alerts must be excluded (fail-closed), not retained"
    )
    assert counts["alerts"] == 1

    alert_titles = {
        item["label"] for item in attention["items"] if item["kind"] == "alert"
    }
    assert "Operator-visible error" in alert_titles
    assert "Untagged null warning" not in alert_titles, (
        "NULL event_type alert must not reach the operator preview (fail-closed)"
    )
    assert "Internal payoff error" not in alert_titles
    assert "Internal size-violation warning" not in alert_titles

    # (a) cross-tab badge count via get_operator_message_counts.
    message_counts = await get_operator_message_counts(db_session, project.id)
    assert message_counts["active_alerts"] == 1, (
        "badge count must exclude NULL and strategist-internal alerts (fail-closed)"
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


@pytest.mark.asyncio
async def test_decay_sweep_decrements_needs_operator_count(db_session):
    """Running sweep_decay_stale_open_messages on a stale QUESTION causes
    get_operator_message_counts to decrement question and needs_operator to zero.

    This is the e2e counterpart to the unit tests in test_reaper.py: it verifies
    that the decay sweep and the badge-count reader share the same 'resolved'
    status predicate so a decayed thread no longer inflates the operator badge.
    Success criterion: open/needs-action count reflects only genuinely-live threads.
    """
    from datetime import timedelta

    from app.reaper_sweeps import (
        STALE_QUESTION_DECAY_DAYS,
        sweep_decay_stale_open_messages,
    )
    from app.time_utils import _utcnow

    project = Project(name="decay-badge-test", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_created = _utcnow() - timedelta(days=STALE_QUESTION_DECAY_DAYS + 1)
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="strategic",
            message_type=MessageType.QUESTION,
            status="open",
            content="stale question past decay window",
            context={"audience": OPERATOR_FYI},
            created_at=stale_created,
        )
    )
    await db_session.commit()

    pre = await get_operator_message_counts(db_session, project.id)
    assert pre["question"] == 1, (
        f"expected question=1 before decay sweep, got {pre['question']}"
    )
    assert pre["needs_operator"] >= 1, (
        f"needs_operator must be ≥1 before decay, got {pre['needs_operator']}"
    )

    decayed = await sweep_decay_stale_open_messages(db_session, _utcnow())
    assert decayed == 1, f"expected sweep to resolve 1 stale message, got {decayed}"

    post = await get_operator_message_counts(db_session, project.id)
    assert post["question"] == 0, (
        f"resolved QUESTION must not appear in badge count after decay, "
        f"got question={post['question']}"
    )
    assert post["needs_operator"] == 0, (
        f"needs_operator must be 0 after stale QUESTION is decayed, "
        f"got {post['needs_operator']}"
    )


def _add_promoted_version(
    db_session, project_id, *, version_number=1, serving_status="unconfirmed"
) -> ReleaseVersion:
    version = ReleaseVersion(
        project_id=project_id,
        version_number=version_number,
        status="promoted",
        serving_status=serving_status,
        promoted_at=_utcnow(),
    )
    db_session.add(version)
    return version


@pytest.mark.asyncio
async def test_promoted_awaiting_activation_item(db_session):
    """A promoted version still ``unconfirmed`` surfaces as a needs_operator
    attention item (reason=promotion_awaiting_activation) and increments the
    promoted_awaiting_activation count and total_needs_operator.

    The aggregate serving status comes from version_serving_summary, so the
    armed-but-pending promotion is exactly the state where the operator's
    single activation approval is what the promotion-to-serving path is waiting
    on.
    """
    project = await _seed_alert_project(db_session)
    _add_promoted_version(db_session, project.id, version_number=1)
    await db_session.commit()

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)

    assert attention["counts"]["promoted_awaiting_activation"] == 1, (
        f"promoted-unconfirmed must count 1, got "
        f"{attention['counts']['promoted_awaiting_activation']}"
    )
    item = next(
        (
            i
            for i in attention["items"]
            if i.get("reason") == "promotion_awaiting_activation"
        ),
        None,
    )
    assert item is not None, (
        f"promotion_awaiting_activation item missing from attention, "
        f"items={[i.get('reason') for i in attention['items']]}"
    )
    assert item["severity"] == "needs_operator"
    assert item["kind"] == "version"
    assert "awaiting activation" in item["label"]
    # Read-model distinction: no approval yet → awaiting_approval.
    assert item["activation_state"] == "awaiting_approval"
    assert item["activation_requested_at"] is None
    assert item["ref"]["kind"] == "version"
    assert item["ref"]["project_id"] == str(project.id)
    assert attention["counts"]["needs_operator"] >= 1

    # Click-through must survive normalize_target: the item declares
    # operator_action=activate_promoted_version, so a target that normalizes
    # away to {} would leave the operator with nowhere to go.  The panel that
    # activates a promoted version renders at runtime/version-lifecycle.
    target = item.get("target")
    assert target, (
        "promotion_awaiting_activation must carry a navigable target; an "
        "unrecognized surface/section normalizes away to {}"
    )
    assert target["surface"] == "runtime"
    assert target["section"] == "version-lifecycle"
    assert target["project_id"] == str(project.id)


@pytest.mark.asyncio
async def test_promoted_approved_activation_item_distinguishes_state(db_session):
    """A promoted version with an in-product approval (activation_requested_at
    set) still counts as promotion_awaiting_activation, but the item's
    activation_state distinguishes approved-but-not-yet-executed from awaiting
    approval and the label reflects the approved state."""
    project = await _seed_alert_project(db_session)
    version = _add_promoted_version(db_session, project.id, version_number=1)
    version.activation_requested_at = _utcnow()
    version.activation_requested_by = "test@example.com"
    await db_session.commit()

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)

    assert attention["counts"]["promoted_awaiting_activation"] == 1
    item = next(
        (
            i
            for i in attention["items"]
            if i.get("reason") == "promotion_awaiting_activation"
        ),
        None,
    )
    assert item is not None
    assert item["activation_state"] == "approved_pending_execution"
    assert item["activation_requested_by"] == "test@example.com"
    assert item["activation_requested_at"] is not None
    assert "approved" in item["label"]
    assert item["target"]["surface"] == "runtime"
    assert item["target"]["section"] == "version-lifecycle"


@pytest.mark.asyncio
async def test_promoted_awaiting_activation_suppressed_when_serving_confirmed(db_session):
    """A promoted version already ``serving_confirmed`` must NOT surface the
    awaiting-activation item — the deploy resolved, so there is nothing for the
    operator to activate."""
    project = await _seed_alert_project(db_session)
    _add_promoted_version(
        db_session, project.id, version_number=1, serving_status="serving_confirmed"
    )
    await db_session.commit()

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)

    assert attention["counts"]["promoted_awaiting_activation"] == 0
    assert all(
        i.get("reason") != "promotion_awaiting_activation"
        for i in attention["items"]
    )


@pytest.mark.asyncio
async def test_promoted_awaiting_activation_absent_when_only_open_version(db_session):
    """With no promoted row (only the current open version), the count stays 0 —
    there is no prior promotion awaiting activation."""
    project = await _seed_alert_project(db_session)
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=1,
            status="open",
            phase="build",
        )
    )
    await db_session.commit()

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)

    assert attention["counts"]["promoted_awaiting_activation"] == 0
    assert all(
        i.get("reason") != "promotion_awaiting_activation"
        for i in attention["items"]
    )


@pytest.mark.asyncio
async def test_promoted_awaiting_activation_only_latest_promoted_counts(db_session):
    """Only the HIGHEST-numbered promoted version is examined: an older promoted
    version still stuck unconfirmed must not surface once a newer promotion has
    already resolved serving."""
    project = await _seed_alert_project(db_session)
    _add_promoted_version(
        db_session,
        project.id,
        version_number=1,
        serving_status="unconfirmed",
    )
    _add_promoted_version(
        db_session,
        project.id,
        version_number=2,
        serving_status="serving_confirmed",
    )
    await db_session.commit()

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)

    assert attention["counts"]["promoted_awaiting_activation"] == 0
    assert all(
        i.get("reason") != "promotion_awaiting_activation"
        for i in attention["items"]
    )
