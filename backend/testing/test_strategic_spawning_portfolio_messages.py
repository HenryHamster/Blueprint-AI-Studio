"""strategic_spawning: portfolio framing, operator steering, and cycle messages.

Covers open-message citation summaries and inbox counts, portfolio-coherence
hints, deploy-model framing, realtime ``strategic_cycle_started`` emission,
operator steering survival across phases and message aging, and the design-doc
compact index in the payload.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
import app.shared_state as _shared_state
from app.models import (
    Task,
    TaskStatus,
    MessageType,
    ProjectMessage,
)
from app.strategic_trigger import ORIGIN_OPERATOR, ORIGIN_QUALIFICATION
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)
from app.autonomous_research import _spawn_research_task
from app.strategic_spawning import (
    _spawn_design_doc_update,
    _spawn_strategic_review,
    _direction_activity_score,
    _build_operator_steering_directive,
    _agent_view_problem_model_packed,
    _AGENT_VIEW_FLOOR_BYTES,
    _FLOOR_MIN_GAPS,
    _STRATEGIC_REVIEW_SCHEMA,
    _design_doc_index,
    _summarize_design_doc_index,
    _DESIGN_DOC_INDEX_SUMMARY_CHARS,
)
from app.payload_schemas import pack_under_budget, validate_internal_payload
from app.capability_demand import collect_missing_capability_demand
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    _ensure_test_account_pool,
    _make_project,
    _patch_spawning_deps,
    _seed_legacy_cycles,
    _select_agent_tasks,
    _utcnow_naive,
)


def test_open_message_citation_summary_promotes_mixed_present_state_with_refs():
    """A partially grounded (present) report keeps its ref detail instead of being stripped."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "present",
            "broken_refs": ["backend/app/x.py"],
            "ungrounded_refs": [],
        }
    }
    summary = _open_message_citation_summary(context)
    # Non-absent state is preserved (NOT collapsed/stripped) so the strategist
    # can see it is mixed rather than fully stale.
    assert summary["citation_state"] == "present"
    assert summary["broken_refs"] == ["backend/app/x.py"]
    assert "ungrounded_refs" not in summary


def test_open_message_citation_summary_promotes_indeterminate_state_with_refs():
    """An indeterminate report carrying ungrounded refs surfaces those refs."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "indeterminate",
            "broken_refs": [],
            "ungrounded_refs": ["backend/app/z.py:bar"],
        }
    }
    summary = _open_message_citation_summary(context)
    assert summary["citation_state"] == "indeterminate"
    assert summary["ungrounded_refs"] == ["backend/app/z.py:bar"]
    assert "broken_refs" not in summary


def test_open_message_citation_summary_omits_non_absent_state_without_refs():
    """A non-absent state with no broken/ungrounded refs has nothing to flag."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "present",
            "broken_refs": [],
            "ungrounded_refs": [],
        }
    }
    assert _open_message_citation_summary(context) == {}


def test_open_message_citation_summary_empty_without_warning():
    """No recorded warning yields an empty summary (no keys added)."""
    from app.strategic_spawning import _open_message_citation_summary

    assert _open_message_citation_summary({}) == {}
    assert _open_message_citation_summary(None) == {}
    assert _open_message_citation_summary({"_provenance_warnings": "nope"}) == {}


@pytest.mark.asyncio
async def test_open_message_counts_ship_when_inbox_dropped(db_session):
    """Trim-proof open-message count ships at critical tier and survives a full inbox drop.

    open_message_count is computed from the raw feed BEFORE packing and placed at
    critical tier, so even when the entire open_messages list is dropped under byte
    budget pressure the agent can distinguish a genuinely empty inbox from a dropped
    one and knows to ./fetch messages list to retrieve them.
    """
    from app.payload_schemas import pack_under_budget
    from app.strategic_spawning import (
        _summarize_open_messages,
        _floor_open_messages,
    )

    # Build a schema that gives open_messages only 1 byte of budget by inflating
    # all critical+high content to crowd it out — simulating extreme byte pressure
    # where the inbox would normally be dropped wholesale.
    # We test the pack contract directly rather than spawning a task.
    messages = [
        {
            "id": f"msg-{i}",
            "type": "NOTE",
            "content": f"operator message #{i} with some content",
            "created_at": f"2026-06-01T00:00:{i:02d}",
        }
        for i in range(40)
    ]

    # Use a tiny budget so open_messages (high priority, multi-KB) gets dropped
    # while open_message_count (critical, tiny scalar) always ships.
    tiny_budget = 512  # bytes — far too small to hold the message list

    packed = pack_under_budget(
        {
            "open_message_count": len(messages),
            "open_messages": messages,
        },
        schema={
            "open_message_count": {"priority": "critical"},
            "open_messages": {
                "priority": "high",
                "summarize": _summarize_open_messages,
                "floor": _floor_open_messages,
            },
        },
        budget=tiny_budget,
    )

    meta = packed["_packer_meta"]
    dropped_keys = {entry["key"] for entry in meta["dropped"]}
    trimmed_keys = {entry["key"] for entry in meta["trimmed"]}

    # The trim-proof count must always ship at critical tier.
    assert "open_message_count" in packed, (
        "open_message_count must ship at critical tier even under extreme budget pressure"
    )
    assert packed["open_message_count"] == 40, (
        f"open_message_count must equal the full pre-trim inbox depth; got {packed.get('open_message_count')}"
    )
    # The critical scalar must never appear in trimmed or dropped lists.
    assert "open_message_count" not in dropped_keys | trimmed_keys, (
        f"open_message_count must not be dropped or trimmed; meta={meta}"
    )
    # open_messages was dropped or trimmed (i.e. the inbox did NOT ship in full).
    assert "open_messages" in dropped_keys or "open_messages" in trimmed_keys, (
        "Under tiny budget open_messages must be dropped or trimmed to exercise the count-fallback path"
    )
    # open_messages must NOT appear in packed when dropped.
    if "open_messages" in dropped_keys:
        assert "open_messages" not in packed, (
            "Dropped open_messages must not appear in the packed payload"
        )


class TestPortfolioCoherenceHint:
    """Capacity is context for task execution, never a direction-count target."""

    def test_capacity_is_explicitly_a_ceiling(self):
        from app.strategic_spawning import _build_portfolio_coherence_hint

        hint = _build_portfolio_coherence_hint(
            1, max_concurrent_vms=8, max_active_directions=10
        )
        assert "PORTFOLIO COHERENCE" in hint
        assert "1/10 active direction slots" in hint
        assert "8 VMs" in hint
        assert "capacity is a ceiling" in hint
        assert "never a target for direction count" in hint

    def test_parallel_execution_uses_tasks_under_a_direction(self):
        from app.strategic_spawning import _build_portfolio_coherence_hint

        hint = _build_portfolio_coherence_hint(
            4, max_concurrent_vms=15, max_active_directions=10
        )
        assert "multiple tasks under the existing direction" in hint
        assert "genuinely distinct durable outcome" in hint
        assert (
            "never to divide files, subsystems, implementation stages, or workers"
            in hint
        )
        assert "open a direction for each" not in hint
        assert "capacity is idle" not in hint

    def test_unknown_capacity_omits_a_concrete_vm_count(self):
        from app.strategic_spawning import _build_portfolio_coherence_hint

        hint = _build_portfolio_coherence_hint(
            10, max_concurrent_vms=0, max_active_directions=10
        )
        assert hint
        assert "VMs concurrently" not in hint
        assert "Available execution capacity" in hint


# ── Deploy-model framing (integration-branch landing ≠ deployment) ────────────


async def _spawn_strategic_review_payload(project, session) -> dict:
    """Run a strategy-only cycle and return the spawned strategic_review payload."""
    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        await _spawn_research_task(project, session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old
    result = await session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1, f"Expected one task, got {len(tasks)}"
    payload = tasks[0].payloads[0]
    assert payload["type"] == "strategic_review", payload["type"]
    return payload


@pytest.mark.asyncio
async def test_deploy_framing_present_when_integration_and_promotion_differ(db_session):
    """When the integration branch and promotion target differ (the LTS split),
    the strategic payload carries the deploy-model framing so the agent does not
    treat 'landed on the integration branch' as 'deployed/live'."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" in suffix, "deploy framing missing from prompt_suffix"
    # Both sides of the split are named, using the project's real branch names.
    assert "`automain`" in suffix and "`main`" in suffix
    # The operative warnings that defuse the enable-and-measure / hard-pivot trap.
    assert "not yet PROMOTED" in suffix
    assert "nothing live to measure" in suffix


@pytest.mark.asyncio
async def test_deploy_framing_absent_when_branches_equal(db_session):
    """A single-branch project (integration branch == promotion target) deploys
    merges directly, so the deploy-model framing must NOT appear — it would be
    actively misleading there."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" not in suffix, (
        "deploy framing must be omitted when integration branch == promotion target"
    )


@pytest.mark.asyncio
async def test_deploy_framing_uses_project_branch_names_not_hardcoded(db_session):
    """The framing is project-generic: it interpolates the project's own
    integration/promotion branch names and never hardcodes Blueprint's
    'automain'/'main' literals."""
    project = _make_project(
        vision={"_strategic_cycle_count": 1, "_research_spawn_count": 5},
        integration_branch="staging",
        promotion_target="release",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = await _spawn_strategic_review_payload(project, db_session)
    suffix = payload.get("prompt_suffix") or ""
    assert "Deployment model" in suffix
    assert "`staging`" in suffix and "`release`" in suffix
    # No Blueprint-specific branch literals leak into a non-Blueprint project.
    assert "automain" not in suffix
    assert "`main`" not in suffix


# ── Realtime strategic_cycle_started emission (see app.strategic_events) ───────


@pytest.mark.asyncio
async def test_spawn_strategic_review_emits_cycle_started(db_session):
    """_spawn_strategic_review broadcasts strategic_cycle_started (phase 2) post-commit."""
    project = _make_project(
        directions=[
            {
                "id": "d-evt01",
                "status": "active",
                "text": "A direction",
                "priority": "medium",
                "research_history": [{"prs_merged": 1}],
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    with patch(
        "app.strategic_events.broadcast_to_project", new=AsyncMock()
    ) as mock_bcast:
        spawned = await _spawn_strategic_review(project, db_session)

    assert spawned is True
    started = [
        c.args
        for c in mock_bcast.await_args_list
        if c.args[1] == "strategic_cycle_started"
    ]
    assert len(started) == 1, "expected exactly one strategic_cycle_started broadcast"
    _proj_id, _event, data = started[0]
    assert _proj_id == str(project.id)
    assert data["phase"] == 2
    assert data.get("task_id")


@pytest.mark.asyncio
async def test_arch_cycle_emits_cycle_started_phase1(db_session):
    """A design_doc_update spawn broadcasts strategic_cycle_started with phase 1."""
    project = _make_project(
        vision={
            "_strategic_cycle_count": 2,
            "_research_spawn_count": 5,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old = _shared_state.runtime_config.get("architecture_review_frequency", 3)
    _shared_state.runtime_config["architecture_review_frequency"] = 3
    try:
        with patch(
            "app.strategic_events.broadcast_to_project", new=AsyncMock()
        ) as mock_bcast:
            await _spawn_research_task(project, db_session)
    finally:
        _shared_state.runtime_config["architecture_review_frequency"] = old

    started = [
        c.args
        for c in mock_bcast.await_args_list
        if c.args[1] == "strategic_cycle_started"
    ]
    assert len(started) == 1, "expected one strategic_cycle_started for the arch spawn"
    assert started[0][2]["phase"] == 1


@pytest.mark.asyncio
async def test_running_update_superseded_signal_present_when_recent_ledger_entry(
    db_session,
):
    """A recent running_update_superseded ledger entry surfaces a derived signal
    in the strategic payload so next cycle's thinker treats the document as
    operator-authoritative."""
    from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
    from app.vision_merge import REJECTED_EDITS_KEY

    recent_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    project = _make_project(
        directions=[
            {
                "id": "d-rus01",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
            }
        ],
        vision={
            "purpose": "test purpose",
            REJECTED_EDITS_KEY: [
                {
                    "at": recent_at,
                    "field": "running_update",
                    "kind": RUNNING_UPDATE_REJECTED_KIND,
                    "agent_value": "superseded agent doc",
                },
                # An unrelated vision field conflict sharing the ledger — must be ignored.
                {
                    "at": recent_at,
                    "field": "purpose",
                    "kind": "field_conflict",
                    "agent_value": "x",
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "running_update_superseded" in payload
    signal = payload["running_update_superseded"]
    assert signal["count"] == 1
    assert signal["most_recent_at"] == recent_at


@pytest.mark.asyncio
async def test_running_update_superseded_signal_absent_without_ledger_entry(db_session):
    """No running_update_superseded ledger entry → field omitted from the payload."""
    project = _make_project(
        directions=[
            {
                "id": "d-rus02",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
            }
        ],
        vision={"purpose": "test purpose"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "running_update_superseded" not in payload


@pytest.mark.asyncio
async def test_running_update_superseded_signal_excludes_stale_entries(db_session):
    """A ledger entry older than the 30-day window is not surfaced."""
    from app.running_update_merge import RUNNING_UPDATE_REJECTED_KIND
    from app.vision_merge import REJECTED_EDITS_KEY

    stale_at = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    project = _make_project(
        directions=[
            {
                "id": "d-rus03",
                "status": "active",
                "text": "Some direction",
                "priority": "medium",
            }
        ],
        vision={
            "purpose": "test purpose",
            REJECTED_EDITS_KEY: [
                {
                    "at": stale_at,
                    "field": "running_update",
                    "kind": RUNNING_UPDATE_REJECTED_KIND,
                    "agent_value": "old superseded doc",
                }
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "running_update_superseded" not in payload


# ── operator-steered cycle: steering directive in prompt_suffix ───────────────


def _operator_refresh_marker(reason: str = "operator_message") -> dict:
    """A pending operator-origin strategic-refresh marker (as request_strategic_refresh writes it)."""
    return {
        "requested_at": "2026-06-03T00:00:00",
        "reason": reason,
        "count": 1,
        "origin": ORIGIN_OPERATOR,
    }


@pytest.mark.asyncio
async def test_operator_steered_cycle_appends_steering_directive_to_prompt_suffix(
    db_session,
):
    """An operator-origin spawn surfaces a high-priority steering directive that
    flags the cycle as operator-steered AND quotes the operator's message."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    steering_text = "Please pivot the roadmap toward billing reliability this week."
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=steering_text,
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert "operator steering" in suffix.lower()
    # The actual operator message must be quoted, not just referenced.
    assert steering_text in suffix


@pytest.mark.asyncio
async def test_qualification_origin_marker_adds_no_steering_directive(db_session):
    """A qualification-origin marker must NOT be framed as operator steering."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": {
                "requested_at": "2026-06-03T00:00:00",
                "reason": "qualification_verdict",
                "count": 1,
                "origin": ORIGIN_QUALIFICATION,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="A user message that must not be promoted to a steering directive.",
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "OPERATOR STEERING" not in payload.get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_no_marker_adds_no_steering_directive(db_session):
    """A cadence cycle (no pending marker at all) carries no steering framing."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="Just a routine question, not operator steering of this cycle.",
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert "OPERATOR STEERING" not in payload.get("prompt_suffix", "")


@pytest.mark.asyncio
async def test_operator_steering_directive_quotes_most_recent_message_and_truncates(
    db_session,
):
    """With several operator messages, the newest is quoted and long content is bounded."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    old_text = "An older operator note from earlier in the session."
    newest_text = "NEWEST: " + ("x" * 2000)  # exceeds the 600-char quote cap
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=old_text,
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=newest_text,
            created_at=datetime(2026, 6, 3, 2, 0, 0),
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    suffix = payload["prompt_suffix"]
    # Newest message is present (its distinctive prefix) and is truncated.
    assert "NEWEST:" in suffix
    assert "…" in suffix
    # The full 2000-char body must not appear verbatim — it was bounded.
    assert ("x" * 2000) not in suffix


# ── architecture-cycle operator wake: directive survives Phase 1 → Phase 2 ────
#
# Regression for review iter-1 MAJOR (autonomous_research.py:1193): on an
# architecture cycle the operator-origin refresh marker is consumed when Phase 1
# (design_doc_update) is recorded, so by the time Phase 2 (strategic_review)
# chains off the design-doc completion the marker reads None and the steering
# directive would be silently dropped (~1/3 of operator wakes). The fix threads
# an ``operator_steered`` flag through the design_doc task context into Phase 2.


@pytest.mark.asyncio
async def test_design_doc_update_stamps_operator_steered_when_marker_operator_origin(
    db_session,
):
    """Phase 1 stamps ``operator_steered`` on the design_doc task context when an
    operator-origin refresh marker is armed, so the signal survives the marker's
    consumption and reaches Phase 2."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(
        _select_agent_tasks(project.id)
    )
    task = result.scalars().all()[0]
    assert task.payloads[0]["type"] == "design_doc_update"
    assert (task.context or {}).get("operator_steered") is True


@pytest.mark.asyncio
async def test_design_doc_update_start_event_includes_cycle_and_steering(
    db_session, mocker
):
    steering_excerpt = "Focus the next strategy cycle on billing reliability."
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_started", new=emit)
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(
                steering_excerpt
            ),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert await _spawn_design_doc_update(project, db_session) is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    emit.assert_awaited_once()
    assert emit.await_args.args == (project.id, task.id, 1)
    assert emit.await_args.kwargs["cycle_id"] == task.context["strategic_cycle_id"]
    assert emit.await_args.kwargs["steering_origin"] == ORIGIN_OPERATOR
    assert emit.await_args.kwargs["steering_excerpt"] == steering_excerpt


@pytest.mark.asyncio
async def test_design_doc_update_no_operator_steered_when_no_marker(db_session):
    """A cadence (non-operator) architecture cycle carries no operator_steered flag."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    assert "operator_steered" not in (task.context or {})


@pytest.mark.asyncio
async def test_strategic_review_start_event_includes_cycle_and_steering(
    db_session, mocker
):
    _patch_spawning_deps(mocker)
    mocker.patch(
        "app.task_lifecycle._has_active_strategic_task",
        new=AsyncMock(return_value=False),
    )
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_started", new=emit)
    cycle_id = "sc-phase2evt"
    steering_excerpt = "Tie the outcome to the operator steer."
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (
        await _spawn_strategic_review(
            project,
            db_session,
            cycle_id=cycle_id,
            operator_steered=True,
            steering_excerpt=steering_excerpt,
        )
        is True
    )

    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    emit.assert_awaited_once()
    assert emit.await_args.args == (project.id, task.id, 2)
    assert emit.await_args.kwargs == {
        "cycle_id": cycle_id,
        "steering_origin": ORIGIN_OPERATOR,
        "steering_excerpt": steering_excerpt,
    }


@pytest.mark.asyncio
async def test_architecture_cycle_operator_wake_reaches_phase2_after_marker_cleared(
    db_session, mocker
):
    """End-to-end: an operator wake landing on an architecture cycle still
    surfaces the steering directive in the Phase 2 strategic_review prompt, even
    though the refresh marker was consumed when Phase 1 was recorded.

    This is the path the four strategy-only tests above never exercise:
    design_doc_update → _handle_design_doc_completion → _spawn_strategic_review.
    """
    from app.task_completion import _handle_design_doc_completion

    # Phase-1 → Phase-2 completion emits cycle events; stub them so the test
    # exercises only the steering-directive threading, not the SSE layer.
    mocker.patch("app.strategic_events.emit_strategic_cycle_started", new=AsyncMock())
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=AsyncMock())

    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker(),
        },
        design_doc={"overview": {"body": "x"}, "_meta": {"revision": 1}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    steering_text = "Pivot the roadmap toward billing reliability this week."
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content=steering_text,
            created_at=datetime(2026, 6, 3, 1, 0, 0),
        )
    )
    await db_session.commit()

    # Phase 1: spawn the design_doc_update task (stamps operator_steered).
    assert await _spawn_design_doc_update(project, db_session) is True
    design_doc_task = (
        (
            await db_session.execute(
                _select_agent_tasks(project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    assert (design_doc_task.context or {}).get("operator_steered") is True

    # Simulate the autonomous loop consuming the operator marker when it records
    # the Phase 1 spawn (clear_strategic_refresh in autonomous_research.py), and
    # mark Phase 1 complete so the Phase 2 active-task guard does not skip.
    from app.strategic_trigger import strategic_refresh_origin

    preferred_runner_id = str(uuid.uuid4())
    design_doc_task.context = {
        **(design_doc_task.context or {}),
        "runner_target_id": preferred_runner_id,
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
        STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
    }
    design_doc_task.vm_instance_id = f"runner:{preferred_runner_id}"
    project.vision = {
        k: v for k, v in project.vision.items() if k != "_strategic_refresh"
    }
    design_doc_task.status = TaskStatus.COMPLETED
    db_session.add(project)
    db_session.add(design_doc_task)
    await db_session.commit()
    await db_session.refresh(project)
    # Precondition: the marker is gone, so Phase 2 cannot re-derive operator
    # origin from vision — it must rely on the threaded flag.
    assert strategic_refresh_origin(project.vision) is None

    # Phase 2: chain the strategic review off the design-doc completion.
    await _handle_design_doc_completion(
        design_doc_task, "", db_session, project=project
    )

    strategic_task = (
        (
            await db_session.execute(
                _select_agent_tasks(project.id)
            )
        )
        .scalars()
        .all()
    )
    review = next(
        t for t in strategic_task if t.payloads[0]["type"] == "strategic_review"
    )
    suffix = review.payloads[0].get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert steering_text in suffix
    assert (
        (review.context or {}).get("preferred_runner_target_id")
        == preferred_runner_id
    )
    assert (
        (review.context or {}).get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY)
        is True
    )
    assert (
        (review.context or {}).get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY)
        == "workbench_continuous"
    )
    assert "runner_target_id" not in (review.context or {})
    assert not (review.vm_instance_id or "").startswith("runner:")


# ── persisted steering excerpt survives the conversation-thread aging out ─────
#
# Root-cause regression: the operator's message is re-derived from a short,
# volatile recent-messages snapshot at spawn time. When enough messages
# accumulate before the deferred cycle reaches its review phase, that snapshot
# no longer carries the operator message and the directive was silently dropped.
# The fix persists the excerpt on the refresh marker at arm time and uses it as
# a durable fallback when the live thread yields nothing.


def _operator_refresh_marker_with_excerpt(excerpt: str) -> dict:
    marker = _operator_refresh_marker()
    marker["excerpt"] = excerpt
    return marker


def test_steering_directive_falls_back_to_excerpt_when_thread_has_no_operator_msg():
    """With no qualifying operator message in the thread, the directive is built
    from the persisted fallback excerpt rather than returning None."""
    excerpt = "Persisted steering: prioritize billing reliability."
    directive = _build_operator_steering_directive([], fallback_excerpt=excerpt)
    assert directive is not None
    assert "OPERATOR STEERING" in directive
    assert excerpt in directive


def test_steering_directive_fallback_excerpt_is_truncated():
    """A long fallback excerpt is bounded by the same per-message quote cap."""
    long_excerpt = "LEAD: " + ("y" * 2000)
    directive = _build_operator_steering_directive([], fallback_excerpt=long_excerpt)
    assert directive is not None
    assert "LEAD:" in directive
    assert "…" in directive
    assert ("y" * 2000) not in directive


def test_steering_directive_none_when_no_thread_and_no_excerpt():
    """No operator message and no fallback excerpt → no steering framing."""
    assert _build_operator_steering_directive([], fallback_excerpt=None) is None
    assert _build_operator_steering_directive([], fallback_excerpt="   ") is None


def test_steering_directive_prefers_live_thread_over_fallback_excerpt():
    """When the live thread still carries the operator message, that message is
    quoted in preference to the (possibly stale) fallback excerpt."""
    live = [
        {
            "from": "user",
            "type": MessageType.FEEDBACK.value,
            "content": "LIVE thread steering message.",
        }
    ]
    directive = _build_operator_steering_directive(
        live, fallback_excerpt="STALE fallback excerpt."
    )
    assert directive is not None
    assert "LIVE thread steering message." in directive
    assert "STALE fallback excerpt." not in directive


@pytest.mark.asyncio
async def test_strategy_only_cycle_uses_marker_excerpt_when_message_aged_out(
    db_session,
):
    """The operator message has aged out of the recent-messages snapshot, but the
    excerpt persisted on the marker still produces a steering directive."""
    excerpt = "Persisted steering: ship the billing reliability fix this week."
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(excerpt),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)
    # No operator ProjectMessage seeded — it has aged out of the snapshot.

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    suffix = payload.get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert excerpt in suffix


@pytest.mark.asyncio
async def test_design_doc_update_stamps_steering_excerpt_from_marker(db_session):
    """Phase 1 persists the marker excerpt on the design_doc task context so Phase
    2 can rebuild the directive after the marker is consumed."""
    excerpt = "Persisted steering: focus on billing reliability."
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(excerpt),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    assert (task.context or {}).get("operator_steered") is True
    assert (task.context or {}).get("steering_excerpt") == excerpt


# ── regression: operator steering survives a *fully aged-out* recent-messages ──
# snapshot. The parent bug is that the operator's text is re-derived from a
# short (15-message) volatile snapshot at the deferred cycle's review phase;
# when that window is entirely crowded out by system-posted digest events the
# operator message is gone and — pre-fix — the directive vanished silently.
# The tests below pin the durable-excerpt fallback against exactly that
# "snapshot is non-empty but carries zero qualifying operator steering" shape
# that the existing empty-thread / single-message tests above never exercise.


def _system_note_digest_thread(n: int) -> list[dict]:
    """A conversation_thread snapshot of *n* system-posted NOTE digest events.

    Mirrors the shape ``_spawn_strategic_review`` builds from ``ProjectMessage``
    rows: ``from``/``type``/``content``/``context`` keys. These are passive
    NOTEs carrying a routine digest ``event_type`` (``cycle_summary``), so
    ``_build_operator_steering_directive``'s ``classify_operator_arming`` gate
    rejects every one — the thread is non-empty yet yields zero qualifying
    operator steering messages, the exact aged-out condition the fix targets.
    ``from='user'`` deliberately passes the cheap from-filter so the test proves
    the *classification* gate (not just the author check) is what excludes them.
    """
    return [
        {
            "from": "user",
            "type": MessageType.NOTE.value,
            "content": f"[cycle digest {i}] routine system summary line.",
            "context": {"event_type": "cycle_summary"},
        }
        for i in range(n)
    ]


def test_steering_directive_fallback_when_thread_is_all_system_notes():
    """Req 1 — deferred-cycle directive fallback.

    With a *populated* 15-entry thread that contains zero qualifying operator
    steering messages (all system-posted NOTE digest events), the persisted
    fallback excerpt must still produce a non-None directive whose body quotes
    the excerpt verbatim. Pre-fix (no excerpt fallback) this returned None and
    the operator's steering was dropped."""
    excerpt = "Pivot the roadmap toward billing reliability this quarter."
    thread = _system_note_digest_thread(15)

    directive = _build_operator_steering_directive(thread, fallback_excerpt=excerpt)

    assert directive is not None
    assert "OPERATOR STEERING" in directive
    # The excerpt content appears verbatim (it is short enough to dodge the cap).
    assert excerpt in directive
    # None of the crowding system-digest lines leaked into the directive.
    assert "cycle digest" not in directive


@pytest.mark.asyncio
async def test_spawn_strategic_review_thread_aged_out_uses_steering_excerpt(
    db_session,
):
    """Req 2 — thread-aged-out end-to-end through ``_spawn_strategic_review``.

    An operator FEEDBACK message is seeded with an *old* timestamp, then 15
    newer system NOTE digest messages crowd it out of the 15-row recent-messages
    window the spawn builds its ``conversation_thread`` from. With
    ``operator_steered=True`` and a threaded ``steering_excerpt`` (the Phase 2
    architecture-cycle path, where the marker was already consumed at Phase 1),
    the directive must still reach the payload's ``prompt_suffix`` from the
    durable excerpt."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    # The real operator steering message — but stamped old so it sorts out of
    # the 15-most-recent window once the digest events pile up on top of it.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="Original operator steering that has since aged out.",
            created_at=datetime(2026, 6, 1, 0, 0, 0),
        )
    )
    # 15 newer system-posted digest NOTEs that crowd the operator message out.
    for i in range(15):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="system",
                message_type=MessageType.NOTE,
                content=f"[cycle digest {i}] routine system summary.",
                context={"event_type": "cycle_summary"},
                created_at=datetime(2026, 6, 3, 1, 0, i),
            )
        )
    await db_session.commit()

    steering_excerpt = "Pivot toward billing reliability"
    spawned = await _spawn_strategic_review(
        project,
        db_session,
        operator_steered=True,
        steering_excerpt=steering_excerpt,
    )
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    suffix = payload.get("prompt_suffix", "")
    assert "OPERATOR STEERING" in suffix
    assert steering_excerpt in suffix
    # Sanity: the operator message really did age out of the shipped snapshot,
    # so the directive came from the durable excerpt — not the live thread.
    thread_contents = [
        m.get("content", "") for m in payload.get("conversation_thread", [])
    ]
    assert not any("aged out" in c for c in thread_contents)


@pytest.mark.asyncio
async def test_spawn_design_doc_update_no_excerpt_when_operator_marker_lacks_excerpt(
    db_session,
):
    """Req 3 (negative edge) — Phase 1 stamps ``operator_steered`` but NOT a
    ``steering_excerpt`` when the consumed operator marker carries no ``excerpt``
    key (an old-format marker, or an empty-content steering message). The
    positive path is covered by
    ``test_design_doc_update_stamps_steering_excerpt_from_marker`` above; this
    pins that a missing excerpt does not synthesize a bogus one."""
    project = _make_project(
        vision={
            "purpose": "test purpose",
            # Operator-origin marker with NO "excerpt" field.
            "_strategic_refresh": _operator_refresh_marker(),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    ctx = task.context or {}
    assert ctx.get("operator_steered") is True
    # No excerpt on the marker → no steering_excerpt key on the task context.
    assert "steering_excerpt" not in ctx


@pytest.mark.asyncio
async def test_spawn_design_doc_update_stamps_longer_distinct_excerpt(db_session):
    """Req 3 (value-flow) — the exact marker excerpt flows verbatim onto the
    Phase 1 task context, independent of the specific text. Uses a distinct,
    longer excerpt than the sibling positive test so the assertion proves a
    value copy rather than a hardcoded default."""
    excerpt = (
        "Refocus the next cycle on the auth-token refresh race that is dropping "
        "operator sessions during deploys; deprioritize the dashboard polish."
    )
    project = _make_project(
        vision={
            "purpose": "test purpose",
            "_strategic_refresh": _operator_refresh_marker_with_excerpt(excerpt),
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    task = result.scalars().all()[0]
    assert (task.context or {}).get("steering_excerpt") == excerpt


def _recent_strategic_task(project_id, minutes_ago: int) -> Task:
    """A terminal strategic task created within the spawn-cooldown window.

    Puts ``_spawn_research_task`` on the cooldown branch so an armed operator
    marker is what drives the strategic spawn (and its consume), mirroring the
    helper in test_autonomous_research.py."""
    return Task(
        project_id=project_id,
        title="Recent strategic cycle",
        payloads=[{"type": "design_doc_update", "base_branch": "main"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        created_at=_utcnow_naive() - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_steering_acted_confirmation_skipped_when_marker_has_no_excerpt(
    db_session, mocker
):
    """Req 4 (negative) — ``_maybe_notify_operator_steering_acted`` is NOT called
    when the consumed marker is operator-origin but carries no ``excerpt`` (an
    old-format marker / empty-content message). With no preserved text the woken
    cycle has no directive to act on, so the confirmation must be suppressed."""
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    notify_mock = mocker.patch(
        "app.autonomous_research._maybe_notify_operator_steering_acted",
        new=AsyncMock(),
    )

    project = _make_project(
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow_naive().isoformat(),
                "reason": "operator_message",
                "count": 1,
                "origin": ORIGIN_OPERATOR,
                # no "excerpt" key
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    # Marker was consumed by the spawn ...
    assert (project.vision or {}).get("_strategic_refresh") is None
    # ... but with no excerpt the confirmation was suppressed.
    assert notify_mock.await_count == 0


@pytest.mark.asyncio
async def test_steering_acted_confirmation_posted_when_marker_has_excerpt(
    db_session, mocker
):
    """Req 4 (positive contrast) — ``_maybe_notify_operator_steering_acted`` IS
    called when the consumed marker is operator-origin AND carries a non-empty
    ``excerpt``: the operator's steering text was preserved, so the woken cycle
    genuinely has a directive to act on."""
    mocker.patch(
        "app.strategic_spawning._spawn_strategic_review",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.strategic_spawning._spawn_design_doc_update",
        new=AsyncMock(return_value=True),
    )
    notify_mock = mocker.patch(
        "app.autonomous_research._maybe_notify_operator_steering_acted",
        new=AsyncMock(),
    )

    project = _make_project(
        vision={
            "purpose": "test",
            "_strategic_refresh": {
                "requested_at": _utcnow_naive().isoformat(),
                "reason": "operator_message",
                "count": 1,
                "origin": ORIGIN_OPERATOR,
                "excerpt": "Please refocus on the billing reliability work.",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(_recent_strategic_task(project.id, minutes_ago=5))
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_refresh") is None
    # Excerpt was preserved → confirmation fired exactly once.
    assert notify_mock.await_count == 1


@pytest.mark.asyncio
async def test_arm_operator_refresh_persists_excerpt_and_replaces_on_coalesce(
    db_session,
):
    """Req 5 — excerpt stored on the marker at arm time, replaced on coalesce.

    ``maybe_arm_operator_refresh`` (the wake-gate both message endpoints share)
    must persist the operator's message text on the refresh marker as the
    steering excerpt. A second steering message coalesces into the single-slot
    marker and replaces the excerpt with the newer text — the most recent
    operator message is the active steering intent."""
    from app.strategic_trigger import (
        ARMING_STEERING,
        maybe_arm_operator_refresh,
        strategic_refresh_excerpt,
    )

    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    armed = await maybe_arm_operator_refresh(
        db_session,
        project.id,
        MessageType.FEEDBACK,
        message_content="test steering content",
    )
    assert armed == ARMING_STEERING
    await db_session.commit()
    await db_session.refresh(project)

    marker = (project.vision or {}).get("_strategic_refresh")
    assert isinstance(marker, dict)
    assert marker.get("origin") == ORIGIN_OPERATOR
    # Short content (< cap) is stored verbatim.
    assert marker.get("excerpt") == "test steering content"
    assert strategic_refresh_excerpt(project.vision) == "test steering content"

    # A second steering message coalesces and replaces the excerpt.
    armed2 = await maybe_arm_operator_refresh(
        db_session,
        project.id,
        MessageType.FEEDBACK,
        message_content="newer steering content wins",
    )
    assert armed2 == ARMING_STEERING
    await db_session.commit()
    await db_session.refresh(project)

    marker2 = (project.vision or {}).get("_strategic_refresh")
    assert marker2.get("excerpt") == "newer steering content wins"
    # Coalesced into the same single slot (not stacked into a second marker).
    assert marker2.get("count") == 2


@pytest.mark.asyncio
async def test_open_bug_report_older_than_thread_cap_appears_in_open_messages(
    db_session,
):
    """Open BUG_REPORT created before 15+ newer messages still appears in payload.

    Regression test for: strategist's open-message feed survives the 15-msg
    thread cap. The conversation_thread is capped at 15 recent messages of ANY
    status, but open_messages must come from a dedicated query so open
    BUG_REPORT/BLOCKER items older than the thread cap are NOT aged out of view.
    """
    project = _make_project(
        vision={"purpose": "test project"},
        problem_model={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create an old open BUG_REPORT (will be older than the 15-message cap).
    old_bug_content = "Ancient bug that needs fixing"
    old_bug = ProjectMessage(
        project_id=project.id,
        from_type="system",
        message_type=MessageType.BUG_REPORT,
        content=old_bug_content,
        status="open",
    )
    db_session.add(old_bug)
    await db_session.commit()

    # Create 20+ newer messages to push the old bug out of the 15-message
    # conversation_thread window.
    for i in range(20):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="system" if i % 2 == 0 else "user",
                message_type=MessageType.NOTE if i % 3 == 0 else MessageType.FEEDBACK,
                content=f"Recent message {i}",
                status="resolved" if i % 5 == 0 else "open",
            )
        )
    await db_session.commit()

    # Spawn the strategic review; the old bug should still appear in open_messages.
    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    tasks = result.scalars().all()
    assert len(tasks) == 1
    payload = tasks[0].payloads[0]

    # The old bug should be in open_messages despite being aged out of
    # conversation_thread (which is capped at 15 most recent rows).
    open_msg_contents = [m["content"] for m in payload["open_messages"]]
    assert old_bug_content in open_msg_contents, (
        "Old open BUG_REPORT aged out of conversation_thread must still appear "
        "in open_messages via dedicated status-filtered query"
    )

    # Verify the old bug is NOT in conversation_thread (proves it aged out).
    thread_contents = [m["content"] for m in payload["conversation_thread"]]
    assert old_bug_content not in thread_contents, (
        "Old bug aged out of 15-message thread cap as expected"
    )


def _pathological_problem_model(n_gaps: int = 400) -> dict:
    """A problem_model whose ACTIVE gap list (no count cap by design) has grown
    so large that the count-based agent-view trim alone cannot fit it under the
    payload byte budget."""
    big = "x" * 2000  # bulky free-text body per gap
    gaps = []
    for i in range(n_gaps):
        gaps.append(
            {
                "id": f"g-{i}",
                "name": f"gap {i}",
                "description": big,
                "evidence": big,
                "priority": "high" if i % 3 == 0 else "low",
                "status": "active",
                "opened": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                "history": [{"ts": "2026-01-01T00:00:00Z", "event": big}],
                "past_moves": [f"d-{i}-{j}" for j in range(20)],
            }
        )
    return {"gaps": gaps, "capabilities": [], "constraints": []}


def test_agent_view_problem_model_packed_shrinks_under_floor():
    """A pathologically large model is shrunk under the guaranteed-small floor
    and carries a marker of what was shortened — never returned oversized."""
    pm = _pathological_problem_model()
    packed = _agent_view_problem_model_packed(pm)

    serialized = len(json.dumps(packed, default=str).encode())
    assert serialized <= _AGENT_VIEW_FLOOR_BYTES, (
        f"floor shrink must bring the model under {_AGENT_VIEW_FLOOR_BYTES} "
        f"bytes, got {serialized}"
    )
    marker = packed.get("_agent_view_floor_applied")
    assert marker is not None, "shrink must leave a loud, visible marker"
    assert marker["floor_bytes"] == _AGENT_VIEW_FLOOR_BYTES
    # At least the highest-value gaps survive so the strategist can still reason.
    assert len(packed["gaps"]) >= _FLOOR_MIN_GAPS


def _caps_friction_heavy_problem_model(
    n_caps: int = 120, n_friction: int = 120
) -> dict:
    """A problem_model whose bulk lives in capabilities and friction (verbose
    free-text bodies), with few/no gaps — so gap-shrinking alone cannot reach
    the floor."""
    big = "y" * 800
    caps = []
    for i in range(n_caps):
        caps.append(
            {
                "name": f"cap {i}",
                "status": "working" if i % 2 == 0 else "planned",
                "notes": big,
                "source": {"kind": "pr", "ref": big},
                "backing_paths": [f"backend/app/mod_{i}_{j}.py" for j in range(8)],
            }
        )
    friction = []
    for i in range(n_friction):
        friction.append(
            {
                "id": f"f-{i}",
                "name": f"friction {i}",
                "status": "active" if i % 2 == 0 else "resolved",
                "description": big,
                "seen_in": [f"d-{i}-{j}" for j in range(8)],
                "mitigations_tried": [big],
                "source": {"kind": "agent_observation", "ref": big},
            }
        )
    return {"gaps": [], "capabilities": caps, "friction": friction}


def test_floor_shrinks_caps_and_friction_when_gaps_are_not_the_bulk():
    """A capability/friction-heavy model (few/no gaps) is shrunk under the floor
    by reducing capabilities and friction to a name+status skeleton, and is
    delivered with an honest summarized-section marker — never dropped."""
    pm = _caps_friction_heavy_problem_model()
    packed = _agent_view_problem_model_packed(pm)

    serialized = len(json.dumps(packed, default=str).encode())
    assert serialized <= _AGENT_VIEW_FLOOR_BYTES, (
        f"caps/friction-heavy model must be shrunk under {_AGENT_VIEW_FLOOR_BYTES} "
        f"bytes, got {serialized}"
    )
    marker = packed.get("_agent_view_floor_applied")
    assert marker is not None, "shrink must leave a loud, visible marker"
    assert marker["summarized_capabilities"] > 0, "capabilities must be reduced"
    assert marker["summarized_friction"] > 0, "friction must be reduced"
    # Reduced entries are name+status skeletons — detail fields are gone.
    cap = packed["capabilities"][0]
    assert "notes" not in cap and "backing_paths" not in cap and "source" not in cap
    assert {"name", "status"} <= set(cap.keys())
    fric = packed["friction"][0]
    assert "description" not in fric and "seen_in" not in fric
    assert "mitigations_tried" not in fric and "source" not in fric
    assert {"id", "name", "status"} <= set(fric.keys())


def test_floor_leaves_caps_friction_untouched_when_gaps_are_the_bulk():
    """Regression: when gap-shrinking alone reaches the floor, capabilities and
    friction are NOT touched and no summarized-section marker is recorded — the
    gap-only shrink path is unchanged."""
    # Gaps whose bulk is the (strippable) append-only history; stripping the
    # first floor level brings the model under the floor without dropping gaps.
    big = "z" * 1200
    gaps = [
        {
            "id": f"g-{i}",
            "name": f"gap {i}",
            "status": "active",
            "priority": "high",
            "opened": "2026-01-01T00:00:00Z",
            "history": [{"ts": "2026-01-01T00:00:00Z", "event": big}],
            "past_moves": [big],
        }
        for i in range(30)
    ]
    pm = {
        "gaps": gaps,
        "capabilities": [
            {
                "name": "keep me",
                "status": "working",
                "notes": "n",
                "backing_paths": ["a.py"],
            }
        ],
        "friction": [
            {"id": "f-keep", "name": "keep", "status": "active", "description": "d"}
        ],
    }
    packed = _agent_view_problem_model_packed(pm)

    marker = packed["_agent_view_floor_applied"]
    assert marker["dropped_gaps"] == 0, "field-stripping should reach the floor"

    marker = packed["_agent_view_floor_applied"]
    assert "summarized_capabilities" not in marker
    assert "summarized_friction" not in marker
    # Detail-bearing capability/friction entries survive intact.
    assert packed["capabilities"][0]["notes"] == "n"
    assert packed["friction"][0]["description"] == "d"


def test_floor_no_pressure_path_is_byte_identical():
    """A model that already fits the floor is returned byte-identical — no
    marker, no reordering, no shrink."""
    pm = {
        "gaps": [{"id": "g1", "name": "g", "status": "active", "priority": "high"}],
        "capabilities": [{"name": "c", "status": "working", "notes": "n"}],
        "friction": [{"id": "f1", "name": "f", "status": "active", "description": "d"}],
    }
    packed = _agent_view_problem_model_packed(pm)
    assert "_agent_view_floor_applied" not in packed
    assert packed["capabilities"][0]["notes"] == "n"
    assert packed["friction"][0]["description"] == "d"


def test_pathological_problem_model_degrades_loudly_when_it_cannot_fit():
    """The GENUINELY EXCEPTIONAL case: a problem_model so large it cannot fit the
    packer budget even on its own. The floor reservation now equals the WHOLE
    agent-view model, so an oversized model's reservation exceeds the budget and
    the packer drops the floor guarantee — at which point the ``summarize``
    callback (_agent_view_problem_model_packed -> _shrink_pm_to_floor) degrades
    the model LOUDLY rather than silently: it ships a bounded floored skeleton
    carrying the ``_agent_view_floor_applied`` marker, recorded in
    ``_packer_meta['trimmed']`` so the agent's problemModelRecovery resolves to
    'fetch'. This is the visibly-exceptional fallback, never the normal mode.

    Siblings are kept modest here so the degradation is clearly attributable to
    the model's own size — the normal/contended whole-model guarantee (where the
    model fits and siblings yield first) is exercised separately in
    ``test_strategic_review_ships_whole_problem_model_under_contention``."""
    pm = _pathological_problem_model()  # ~MBs of active-gap bulk, >> 120KB budget
    payload = {
        "type": "strategic_review",
        "project_name": "p",
        "base_branch": "main",
        "model": "claude-opus-4-8",
        "effort": "medium",
        "problem_model": pm,
        "model_health": {"model_size_status": "oversized", "gap_count": 400},
    }
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    meta = packed["_packer_meta"]
    dropped_keys = {d["key"] for d in meta["dropped"]}

    # The model is DELIVERED (degraded), never silently dropped.
    assert "problem_model" in packed, (
        "problem_model must be delivered degraded, never silently dropped"
    )
    assert "problem_model" not in dropped_keys, (
        f"problem_model was dropped: {meta['dropped']}"
    )
    # Degraded loudly: the floored skeleton carries the marker so the agent
    # knows it must ./fetch the full model.
    assert "_agent_view_floor_applied" in packed["problem_model"], (
        "an oversized model must ship floored with a loud marker"
    )
    serialized = len(json.dumps(packed["problem_model"], default=str).encode())
    assert serialized <= _AGENT_VIEW_FLOOR_BYTES, (
        f"floored model must fit the {_AGENT_VIEW_FLOOR_BYTES}-byte floor, got {serialized}"
    )
    # Recorded in _packer_meta['trimmed'] (kind=summarized) so problemModelRecovery
    # becomes 'fetch'. This is the agent's single fetch-decision channel.
    trimmed_pm = [t for t in meta["trimmed"] if t["key"] == "problem_model"]
    assert trimmed_pm, (
        f"the degraded model must be recorded in _packer_meta['trimmed']: {meta['trimmed']}"
    )
    assert trimmed_pm[0]["kind"] == "summarized"
    # The tiny size/health signal still ships alongside it.
    assert "model_health" in packed
    assert "model_health" not in dropped_keys


def _mature_agent_view_problem_model() -> dict:
    """A realistic, mature-but-healthy agent-view problem_model that fits well
    under the 120KB packer budget while carrying full per-entry detail.

    Sized at the healthy thresholds (12 gaps / 20 capabilities / 8 friction) with
    substantial — but not pathological — free-text bodies, so the WHOLE model is
    expected to ship inline under normal cycle sizes (the floor never fires)."""
    gap_body = "g" * 1600
    gaps = [
        {
            "id": f"g-{i}",
            "name": f"gap {i}: concrete delta between intent and capability {i}",
            "description": gap_body,
            "evidence": gap_body,
            "priority": "high" if i % 2 == 0 else "medium",
            "status": "active",
            "opened": f"2026-02-{(i % 27) + 1:02d}T00:00:00Z",
            "history": [{"ts": "2026-02-01T00:00:00Z", "event": "opened"}],
            "past_moves": [f"d-{i}-{j}" for j in range(3)],
        }
        for i in range(12)
    ]
    cap_notes = "c" * 1400
    capabilities = [
        {
            "name": f"cap {i}: registry-derived subsystem {i}",
            "status": "working" if i % 2 == 0 else "partial",
            "notes": cap_notes,
            "source": {"type": "shipped_change", "pr_number": 7000 + i},
            "backing_paths": [
                f"backend/app/mod_{i}.py",
                f"backend/testing/test_mod_{i}.py",
            ],
        }
        for i in range(20)
    ]
    fric_body = "f" * 1400
    friction = [
        {
            "id": f"f-{i}",
            "name": f"friction {i}: recurring blocker observed across moves",
            "status": "active",
            "description": fric_body,
            "seen_in": [f"d-{i}", f"d-{i + 1}"],
            "mitigations_tried": [f"tried approach {k}" for k in range(3)],
        }
        for i in range(8)
    ]
    return {"gaps": gaps, "capabilities": capabilities, "friction": friction}


def test_strategic_review_ships_whole_problem_model_under_contention():
    """The NORMAL operating mode: a realistic mature problem_model ships WHOLE
    inline even when recoverable siblings genuinely contend for the budget.

    Because the floor reservation now equals the whole agent-view model, its
    bytes are reserved off the top of the budget, so the recoverable siblings
    (directions_summary, conversation_thread, self_reflection_log, …) degrade or
    drop FIRST while the model keeps its full capabilities and friction detail.
    The skeletonizing path (_skeletonize_caps_friction / _shrink_pm_to_floor)
    must NOT fire: no ``_agent_view_floor_applied`` marker, full per-entry detail
    preserved, and the model is neither in the dropped nor the trimmed list."""
    pm = _mature_agent_view_problem_model()
    model_bytes = len(json.dumps(pm, default=str).encode())
    # Sanity: the realistic model is comfortably under the 120KB budget so the
    # whole-model guarantee is the expected outcome (not the floored fallback).
    assert model_bytes < 120 * 1024

    payload = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "p",
        "base_branch": "main",
        "model": "claude-opus-4-8",
        "effort": "medium",
        "vision": {"purpose": "x" * 8000},
        "problem_model": pm,
        "model_health": {"model_size_status": "healthy", "gap_count": 12},
        # Recoverable, summarizable siblings sized to genuinely contend so the
        # reservation is what protects the model — not slack budget.
        "directions_summary": [
            {"id": f"d-{i}", "text": "z" * 1100, "priority": "high"} for i in range(50)
        ],
        "conversation_thread": [
            {"id": f"m-{i}", "role": "user", "content": "w" * 1100} for i in range(40)
        ],
        "self_reflection_log": [{"what_was_hard": "s" * 1100} for _ in range(20)],
    }
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA)

    meta = packed["_packer_meta"]
    dropped_keys = {d["key"] for d in meta["dropped"]}
    trimmed_keys = {t["key"] for t in meta["trimmed"]}

    # Genuine contention: at least one recoverable sibling degraded or dropped.
    assert (dropped_keys | trimmed_keys) - {"problem_model"}, (
        f"expected recoverable siblings to yield first; meta={meta}"
    )

    # The WHOLE model shipped inline — present, not dropped, not trimmed.
    assert "problem_model" in packed
    assert "problem_model" not in dropped_keys, f"model dropped: {meta['dropped']}"
    assert "problem_model" not in trimmed_keys, f"model trimmed: {meta['trimmed']}"

    shipped = packed["problem_model"]
    # The skeletonizing floor path did NOT fire.
    assert "_agent_view_floor_applied" not in shipped, (
        "the whole model must ship without the floored/skeletonized marker"
    )
    # Full per-entry detail survived — capabilities/friction were NOT collapsed
    # to name+status skeletons.
    assert len(shipped["capabilities"]) == 20
    assert len(shipped["friction"]) == 8
    assert len(shipped["gaps"]) == 12
    assert shipped["capabilities"][0].get("notes"), "capability detail was collapsed"
    assert shipped["capabilities"][0].get("backing_paths"), (
        "capability detail collapsed"
    )
    assert shipped["friction"][0].get("description"), "friction detail was collapsed"


def test_strategic_review_degrades_loudly_in_near_budget_band():
    """The NEAR-BUDGET band: a problem_model that fits the budget on its OWN, but
    whose bytes plus the always-critical identity scalars exceed it. The
    full-model floor reservation is ``<= budget`` (so the old drop condition kept
    the guarantee), yet the critical identity keys' bytes starve it. The reworked
    packer accounts for non-floored critical keys' bytes and drops the floor
    guarantee, so the model degrades LOUDLY via summarize (marker + _packer_meta
    record) and the cycle still runs — it must NOT raise or silently drop the
    model."""
    pm = _mature_agent_view_problem_model()
    model_bytes = len(json.dumps(pm, default=str).encode())
    # Always-critical identity scalars (the legacy vision body was removed from
    # the payload, so it no longer competes for budget at critical priority).
    # The model alone fits the budget; model + critical identity keys does not.
    identity = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "project_name": "p",
        "base_branch": "main",
        "model": "claude-opus-4-8",
        "effort": "medium",
    }
    # The packer measures each key's size individually, so the critical-full
    # byte count is the SUM of the per-key sizes, not the serialized dict.
    critical_bytes = sum(
        len(json.dumps(v, default=str).encode()) for v in identity.values()
    )
    # Budget fits the model alone (reservation <= budget) but NOT model + critical.
    budget = model_bytes + critical_bytes - 1
    assert model_bytes <= budget < model_bytes + critical_bytes

    payload = {
        **identity,
        "problem_model": pm,
        "model_health": {"model_size_status": "healthy", "gap_count": 12},
    }
    # Must NOT raise PayloadTooLargeError — the cycle still runs.
    packed = pack_under_budget(payload, _STRATEGIC_REVIEW_SCHEMA, budget=budget)

    meta = packed["_packer_meta"]
    dropped_keys = {d["key"] for d in meta["dropped"]}

    # The model is delivered DEGRADED (floored), never silently dropped.
    assert "problem_model" in packed
    assert "problem_model" not in dropped_keys, f"model dropped: {meta['dropped']}"
    assert "_agent_view_floor_applied" in packed["problem_model"], (
        "the degraded model must carry the loud floor-applied marker"
    )
    # Recorded in _packer_meta['trimmed'] so problemModelRecovery resolves to 'fetch'.
    trimmed_pm = [t for t in meta["trimmed"] if t["key"] == "problem_model"]
    assert trimmed_pm and trimmed_pm[0]["kind"] == "summarized", (
        f"the degraded model must be recorded as summarized: {meta['trimmed']}"
    )


# ── design_doc compact index in the strategic payload ─────────────────────────
# The strategic cycle ships the design doc as a COMPACT, BOUNDED navigation
# index (key + bounded summary + reconciled presence status/modules/warning for
# systems/capabilities) instead of full inline prose; full per-section content
# is fetched on demand via POST /api/agent/design-doc (op:get).


_BIG_DESIGN_PROSE = "LOREM_" + ("x" * 4000)


def _design_doc_with_sections(prose: str) -> dict:
    """A design doc whose section bodies carry *prose* (made large to prove the
    index does not scale with content)."""
    return {
        "systems/auth": {
            "status": "mature",
            "description": prose,
            "modules": ["backend/app/auth.py", "backend/app/routers/agent.py"],
        },
        "capabilities/login": {
            "status": "working",
            "notes": prose,
            "modules": ["backend/app/auth.py"],
        },
        "notes/roadmap": {"title": "Roadmap", "body": prose},
        # Operator-only / internal keys — must be excluded from the index.
        "_meta": {"last_updated": "2026-01-01"},
        "playbook_archive": [{"key": "x", "value": prose}],
    }


def test_design_doc_index_is_complete():
    """Every non-internal section is represented by its key; operator-only and
    underscore-prefixed keys are excluded."""
    index = _design_doc_index(_design_doc_with_sections("short prose"))
    keys = {e["key"] for e in index["sections"]}
    assert keys == {"systems/auth", "capabilities/login", "notes/roadmap"}
    assert "_meta" not in keys
    assert "playbook_archive" not in keys


def test_design_doc_index_excludes_proposed_changes_bookkeeping_key():
    """proposed_changes is the reconciliation queue, shipped as its OWN top-level
    payload field — it is not an architecture section and must not pollute the
    index with a summary-less 'Other sections' entry."""
    doc = _design_doc_with_sections("short prose")
    doc["proposed_changes"] = [{"key": "systems/auth", "op": "set"}]
    keys = {e["key"] for e in _design_doc_index(doc)["sections"]}
    assert "proposed_changes" not in keys
    assert keys == {"systems/auth", "capabilities/login", "notes/roadmap"}


def test_design_doc_index_carries_reconciled_presence():
    """systems/* and capabilities/* entries carry the reconciled presence
    status, the declared backing modules, and any presence_warning."""
    doc = {
        "systems/auth": {
            "status": "missing",
            "description": "x",
            "modules": ["backend/app/auth.py"],
            "presence_warning": (
                "claimed mature but all referenced module file(s) are absent"
            ),
        },
        "capabilities/login": {
            "status": "partial",
            "notes": "y",
            "modules": ["backend/app/auth.py"],
        },
    }
    by_key = {e["key"]: e for e in _design_doc_index(doc)["sections"]}
    assert by_key["systems/auth"]["status"] == "missing"
    assert by_key["systems/auth"]["modules"] == ["backend/app/auth.py"]
    assert by_key["systems/auth"]["presence_warning"].startswith("claimed mature")
    assert by_key["capabilities/login"]["status"] == "partial"
    assert by_key["capabilities/login"]["modules"] == ["backend/app/auth.py"]
    # notes/* are not presence-reconciled — no status field.
    assert "status" not in _design_doc_index({"notes/x": {"body": "z"}})["sections"][0]


def test_design_doc_index_is_bounded_does_not_scale_with_prose():
    """The index serialized size stays roughly stable when section prose grows
    massively — per-entry summaries are bounded, so the 4000x blow-up does not
    propagate."""
    small = _design_doc_index(_design_doc_with_sections("short"))
    large = _design_doc_index(_design_doc_with_sections(_BIG_DESIGN_PROSE))
    small_bytes = len(json.dumps(small).encode())
    large_bytes = len(json.dumps(large).encode())
    # The big-prose variant adds at most a few bounded summaries' worth of bytes.
    assert large_bytes - small_bytes < 3 * (_DESIGN_DOC_INDEX_SUMMARY_CHARS + 64)
    for entry in large["sections"]:
        if "summary" in entry:
            assert len(entry["summary"]) <= _DESIGN_DOC_INDEX_SUMMARY_CHARS


def test_design_doc_index_summary_truncated_with_ellipsis():
    doc = {"notes/x": {"summary": "A" * (_DESIGN_DOC_INDEX_SUMMARY_CHARS + 500)}}
    summary = _design_doc_index(doc)["sections"][0]["summary"]
    assert len(summary) <= _DESIGN_DOC_INDEX_SUMMARY_CHARS
    assert summary.endswith("…")


def test_design_doc_index_empty_doc():
    assert _design_doc_index(None) == {"sections": []}
    assert _design_doc_index({}) == {"sections": []}


def test_summarize_design_doc_index_drops_summaries_keeps_skeleton():
    """The budget-pressure fallback drops per-entry summaries but keeps the
    navigation skeleton (key + status + modules + presence_warning)."""
    index = _design_doc_index(
        {
            "systems/auth": {
                "status": "mature",
                "description": "prose here",
                "modules": ["backend/app/auth.py"],
            }
        }
    )
    assert "summary" in index["sections"][0]
    skeleton = _summarize_design_doc_index(index)
    entry = skeleton["sections"][0]
    assert "summary" not in entry
    assert entry["key"] == "systems/auth"
    assert entry["status"] == "mature"
    assert entry["modules"] == ["backend/app/auth.py"]


@pytest.mark.asyncio
async def test_strategic_review_ships_design_doc_index_not_full_prose(
    db_session, mocker
):
    """The strategic_review packed payload carries the compact design_doc index
    (not full inline prose) for every non-internal section, with reconciled
    presence statuses on systems/capabilities."""
    unique = "UNIQUE_PROSE_MARKER_SR_" + ("z" * 4000)
    design_doc = {
        "systems/auth": {
            "status": "mature",
            "description": unique,
            "modules": ["backend/app/auth.py"],
        },
        "capabilities/login": {
            "status": "working",
            "notes": unique,
            "modules": ["backend/app/auth.py"],
        },
        "notes/roadmap": {"title": "Roadmap", "body": unique},
    }
    # Passthrough grounding so the index is built from exactly this doc and the
    # presence statuses are deterministic (no git/GitHub dependency in tests).
    mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(side_effect=lambda doc, **kw: doc),
    )
    project = _make_project(vision={"purpose": "p"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    dd = payload["design_doc"]
    assert set(dd.keys()) == {"sections"}
    by_key = {e["key"]: e for e in dd["sections"]}
    assert set(by_key) == {"systems/auth", "capabilities/login", "notes/roadmap"}
    assert by_key["systems/auth"]["status"] == "mature"
    assert by_key["systems/auth"]["modules"] == ["backend/app/auth.py"]
    # Full prose is NOT inline anywhere in the packed design_doc field.
    assert unique not in json.dumps(dd)


@pytest.mark.asyncio
async def test_design_doc_update_ships_design_doc_index_not_full_prose(
    db_session, mocker
):
    """The design_doc_update packed payload also carries the compact index and
    not the full inline prose."""
    unique = "UNIQUE_PROSE_MARKER_DDU_" + ("z" * 4000)
    design_doc = {
        "systems/auth": {
            "status": "mature",
            "description": unique,
            "modules": ["backend/app/auth.py"],
        },
        "capabilities/login": {
            "status": "working",
            "notes": unique,
            "modules": ["backend/app/auth.py"],
        },
    }
    mocker.patch(
        "app.strategic_spawning.ground_design_doc",
        new=AsyncMock(side_effect=lambda doc, **kw: doc),
    )
    project = _make_project(vision={"purpose": "p"}, design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    assert payload["type"] == "design_doc_update"

    dd = payload["design_doc"]
    assert set(dd.keys()) == {"sections"}
    by_key = {e["key"]: e for e in dd["sections"]}
    assert set(by_key) == {"systems/auth", "capabilities/login"}
    assert by_key["capabilities/login"]["status"] == "working"
    assert unique not in json.dumps(dd)


# ── Missing-foundation demand in strategic payloads ──────────────────────────


def _blocked_dependent(project_id, need, *, direction_id="d-strat-dep"):
    """A BLOCKED dependent waiting on a single capability/foundation need."""
    from app.time_utils import _utcnow as _ut

    return Task(
        project_id=project_id,
        title="create: needs a missing foundation",
        payloads=[{"type": "create", "prompt": "build feature", "base_branch": "main"}],
        status=TaskStatus.BLOCKED,
        blocked_at=_ut() - timedelta(hours=4),
        context={
            "direction_id": direction_id,
            "blocker": {
                "source": "agent",
                "waiting_on": need.get("ref"),
                "reason": "BLOCKER: waiting on foundation",
                "need": need,
                "needs": [need],
            },
        },
        created_by="ORCHESTRATOR",
    )


@pytest.mark.asyncio
async def test_strategic_review_includes_missing_capability_demand(db_session):
    """The strategic_review payload surfaces aggregated missing-foundation demand
    (matching the collector) plus an advisory hint, and validates cleanly."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    need = {"kind": "capability", "ref": "auth-subsystem", "label": "Auth subsystem"}
    db_session.add(_blocked_dependent(project.id, need))
    await db_session.commit()

    expected = await collect_missing_capability_demand(db_session, project.id)
    assert expected["total"] == 1

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "strategic_review"
    ][0].payloads[0]

    assert payload["missing_capability_demand"] == expected
    assert "Missing-foundation demand" in (payload.get("prompt_suffix") or "")
    validate_internal_payload(dict(payload), "test_sr_demand")


@pytest.mark.asyncio
async def test_strategic_review_omits_demand_when_none(db_session):
    """The strategic_review payload omits the demand field when no demand exists."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_legacy_cycles(db_session, project)

    assert (await collect_missing_capability_demand(db_session, project.id))[
        "total"
    ] == 0

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "strategic_review"
    ][0].payloads[0]

    assert "missing_capability_demand" not in payload
    validate_internal_payload(dict(payload), "test_sr_no_demand")


@pytest.mark.asyncio
async def test_design_doc_update_includes_missing_capability_demand(db_session):
    """The design_doc_update payload surfaces aggregated missing-foundation demand
    (matching the collector) plus an advisory hint, and validates cleanly."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    need = {"kind": "capability", "ref": "search-index", "label": "Search index"}
    db_session.add(_blocked_dependent(project.id, need))
    await db_session.commit()

    expected = await collect_missing_capability_demand(db_session, project.id)
    assert expected["total"] == 1

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "design_doc_update"
    ][0].payloads[0]

    assert payload["missing_capability_demand"] == expected
    assert "Missing-foundation demand" in (payload.get("prompt_suffix") or "")
    validate_internal_payload(dict(payload), "test_ddu_demand")


@pytest.mark.asyncio
async def test_design_doc_update_omits_demand_when_none(db_session):
    """The design_doc_update payload omits the demand field when no demand exists."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (await collect_missing_capability_demand(db_session, project.id))[
        "total"
    ] == 0

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True
    result = await db_session.execute(select(Task).where(Task.project_id == project.id))
    payload = [
        t
        for t in result.scalars().all()
        if t.payloads[0].get("type") == "design_doc_update"
    ][0].payloads[0]

    assert "missing_capability_demand" not in payload
    validate_internal_payload(dict(payload), "test_ddu_no_demand")


# ---------------------------------------------------------------------------
# _direction_activity_score: verified-merge selection input
# ---------------------------------------------------------------------------


def test_direction_activity_score_prefers_verified_merges():
    """A degraded merge (verified_prs_merged=0) must not boost the activity score."""
    degraded = {"verified_prs_merged": 0, "prs_merged": 5}
    verified = {"verified_prs_merged": 5, "prs_merged": 5}
    # The degraded direction shipped nothing verified → no merge boost (score 0),
    # while the verified direction earns the 3x-per-merge boost.
    assert _direction_activity_score(degraded) == 0
    assert _direction_activity_score(verified) == 15
    assert _direction_activity_score(verified) > _direction_activity_score(degraded)


def test_direction_activity_score_legacy_falls_back_to_prs_merged():
    """Stat dict predating verified_prs_merged uses raw prs_merged (back-compat)."""
    legacy = {"prs_merged": 4}
    # 4 merges * 3 = 12, no other signals.
    assert _direction_activity_score(legacy) == 12


def test_direction_activity_score_prefers_reachable_prs_merged():
    """Reachable merges (landed on integration) take precedence over verified merges."""
    # When reachable_prs_merged is present, use it instead of verified_prs_merged
    reachable_stat = {"reachable_prs_merged": 3, "verified_prs_merged": 5, "prs_merged": 5}
    # 3 reachable merges * 3 = 9, not 5 * 3 = 15
    assert _direction_activity_score(reachable_stat) == 9

    # When reachable_prs_merged is 0 (key present), use 0 — do NOT fall back to verified
    zero_reachable = {"reachable_prs_merged": 0, "verified_prs_merged": 4}
    # Key present with value 0 means reachability was recorded and nothing landed: score 0
    assert _direction_activity_score(zero_reachable) == 0

    # When both reachable and verified are present and non-zero, reachable wins
    both_present = {"reachable_prs_merged": 2, "verified_prs_merged": 5}
    # 2 reachable * 3 = 6, not 5 * 3 = 15
    assert _direction_activity_score(both_present) == 6

    # Missing reachable_prs_merged falls back through to verified_prs_merged
    missing_reachable = {"verified_prs_merged": 3}
    # 3 verified * 3 = 9
    assert _direction_activity_score(missing_reachable) == 9

    # All three missing falls back to prs_merged (legacy back-compat)
    legacy_only = {"prs_merged": 2}
    # 2 merges * 3 = 6
    assert _direction_activity_score(legacy_only) == 6


# ---------------------------------------------------------------------------
# Direction-health/prioritization consumes the proven-vs-delivered signal.
#
# These exercise the strategic-DECISION consumption layer: the same scorers that
# feed prioritization (autonomous_research._direction_score), health/quality, and
# difficulty must distinguish a direction shipping PROVEN merges from one shipping
# an equal pile of UNDECLARED merges — additively and fail-safe over legacy data.
# ---------------------------------------------------------------------------


def _accept_cycle(*, proven, declared, merged=2, **extra):
    """A research_history cycle that recorded the acceptance contract.

    ``merged`` verified write-task merges, of which ``proven`` are machine-proven
    and ``declared`` declared forward evidence (proven ⊆ declared ⊆ merged).
    """
    entry = {
        "tasks_created": merged,
        "tasks_completed": merged,
        "tasks_failed": 0,
        "tasks_failed_infra": 0,
        "prs_merged": merged,
        "verified_prs_merged": merged,
        "acceptance_merged_write_total": merged,
        "acceptance_proven_merged": proven,
        "acceptance_declared_merged": declared,
        "acceptance_declaration_coverage": round(declared / merged, 3)
        if merged
        else None,
    }
    entry.update(extra)
    return entry


def test_direction_health_reflects_declaration_coverage():
    """Proven-merge directions out-score equal-volume undeclared ones across the
    automated health/prioritization scorers; legacy data is untouched."""
    from app.direction_metrics import (
        _compute_difficulty_index,
        _compute_direction_quality_score,
        _compute_productivity_score,
        proof_weighted_merged,
    )
    from app.direction_outcomes import verified_merged_count

    # Equal MERGE VOLUME (3 cycles, 2 verified merges each); only proof differs.
    proven_dir = {
        "research_history": [_accept_cycle(proven=2, declared=2) for _ in range(3)]
    }
    undeclared_dir = {
        "research_history": [_accept_cycle(proven=0, declared=0) for _ in range(3)]
    }

    # 1. Quality/health: the undeclared pile no longer reads as fully healthy.
    q_proven = _compute_direction_quality_score(proven_dir)
    q_undeclared = _compute_direction_quality_score(undeclared_dir)
    assert q_proven > q_undeclared

    # 2. Productivity (prioritization input): proven work is more productive.
    assert _compute_productivity_score(proven_dir) > _compute_productivity_score(
        undeclared_dir
    )

    # 3. Difficulty (prioritization input): undeclared work reads as HARDER
    #    (more tasks per proven merge) so it is deprioritized, not credited.
    assert _compute_difficulty_index(undeclared_dir) > _compute_difficulty_index(
        proven_dir
    )

    # 4. Proof-weighting only ever DISCOUNTS — never inflates above verified.
    for entry in undeclared_dir["research_history"] + proven_dir["research_history"]:
        assert proof_weighted_merged(entry) <= verified_merged_count(entry)

    # 5. Fail-safe: the proof discount is GATED on acceptance data being present.
    #    Stripping the acceptance_* fields yields the legacy twin of the undeclared
    #    direction — no proof component, no discount — so it scores strictly HIGHER
    #    than the undeclared one and its merges keep full (verified) credit.
    legacy_twin = {
        "research_history": [
            {k: v for k, v in e.items() if not k.startswith("acceptance_")}
            for e in undeclared_dir["research_history"]
        ]
    }
    for entry in legacy_twin["research_history"]:
        assert proof_weighted_merged(entry) == float(verified_merged_count(entry))
    assert _compute_direction_quality_score(legacy_twin) > q_undeclared
    assert _compute_productivity_score(legacy_twin) > _compute_productivity_score(
        undeclared_dir
    )


# ── _summarize_memo_citations tests ──────────────────────────────────────────


def test_summarize_memo_citations_legacy_shape_unchanged():
    """Legacy {task_id, summary_excerpt} entries pass through unchanged."""
    from app.strategic_spawning import _summarize_memo_citations

    memos = [
        {"task_id": "t1", "summary_excerpt": "First finding."},
        {"task_id": "t2", "summary_excerpt": "Second finding."},
    ]
    result = _summarize_memo_citations(memos)
    assert len(result) == 2
    assert result[0]["task_id"] == "t1"
    assert result[0]["summary_excerpt"] == "First finding."
    assert result[1]["task_id"] == "t2"
    assert result[1]["summary_excerpt"] == "Second finding."


def test_summarize_memo_citations_strips_heavy_fields():
    """Heavy fields are stripped, compact retirement excerpts survive."""
    from app.strategic_spawning import _summarize_memo_citations

    memos = [
        {
            "task_id": "t-ret",
            "summary_excerpt": "Retirement memo.",
            "reasoning_kind": "direction_retirement",
            "direction_id": "dir-xyz",
            "tags": ["retirement"],
            "citations": ["task-cit-1", "task-cit-2"],
            "final_metrics": {"merged_prs": 10},
            "success_evidence": "All tests passed.",
            "retirement_condition": "KPIs met.",
            "rationale": "Long rationale text.",
            "reasoning": "Full multi-paragraph reasoning that is verbose and should be dropped.",
            "citation_refs": "task-cit-1, task-cit-2",
            "final_metrics_excerpt": "final_prs_merged=10, final_cycles_active=4",
            "rationale_excerpt": "Long rationale text.",
            "reasoning_excerpt": "Direction: retired because KPIs were met.",
        }
    ]
    result = _summarize_memo_citations(memos)
    assert len(result) == 1
    entry = result[0]
    assert entry["task_id"] == "t-ret"
    assert entry["reasoning_kind"] == "direction_retirement"
    assert entry["direction_id"] == "dir-xyz"
    assert entry["tags"] == ["retirement"]
    assert entry["success_evidence"] == "All tests passed."
    assert entry["retirement_condition"] == "KPIs met."
    assert entry["citation_refs"] == "task-cit-1, task-cit-2"
    assert (
        entry["final_metrics_excerpt"] == "final_prs_merged=10, final_cycles_active=4"
    )
    assert entry["rationale_excerpt"] == "Long rationale text."
    assert entry["reasoning_excerpt"] == "Direction: retired because KPIs were met."
    # Heavy/verbose fields stripped
    assert "citations" not in entry, "_summarize_memo_citations must strip citations"
    assert "final_metrics" not in entry, (
        "_summarize_memo_citations must strip final_metrics"
    )
    assert "rationale" not in entry, "_summarize_memo_citations must strip rationale"
    assert "reasoning" not in entry, (
        "_summarize_memo_citations must strip full reasoning"
    )


def test_summarize_memo_citations_strips_full_reasoning_keeps_excerpt():
    """Full reasoning is stripped in the budget-degraded path; reasoning_excerpt survives."""
    from app.strategic_spawning import _summarize_memo_citations

    long_reasoning = "Paragraph one. " * 100  # simulate a verbose reasoning blob
    memos = [
        {
            "task_id": "t-full",
            "summary_excerpt": "A retirement memo with full reasoning.",
            "reasoning": long_reasoning,
            "reasoning_excerpt": "Compact excerpt that fits in budget.",
            "reasoning_kind": "direction_retirement",
        }
    ]
    result = _summarize_memo_citations(memos)
    assert len(result) == 1
    entry = result[0]
    assert "reasoning" not in entry, (
        "full reasoning must be stripped to honour budget bounds"
    )
    assert entry["reasoning_excerpt"] == "Compact excerpt that fits in budget."
    assert entry["task_id"] == "t-full"


def test_summarize_memo_citations_caps_entry_count():
    """Entry count is capped to _MEMO_CITATIONS_KEEP (newest-first)."""
    from app.strategic_spawning import _summarize_memo_citations, _MEMO_CITATIONS_KEEP

    memos = [{"task_id": f"t{i}", "summary_excerpt": f"memo {i}"} for i in range(20)]
    result = _summarize_memo_citations(memos)
    assert len(result) == _MEMO_CITATIONS_KEEP
    assert result[0]["task_id"] == "t0"


def test_summarize_memo_citations_caps_excerpt_length():
    """summary_excerpt is capped to _MEMO_EXCERPT_MAX_CHARS."""
    from app.strategic_spawning import (
        _summarize_memo_citations,
        _MEMO_EXCERPT_MAX_CHARS,
    )

    long_text = "x" * (_MEMO_EXCERPT_MAX_CHARS + 100)
    memos = [{"task_id": "t1", "summary_excerpt": long_text}]
    result = _summarize_memo_citations(memos)
    assert len(result[0]["summary_excerpt"]) == _MEMO_EXCERPT_MAX_CHARS


def test_summarize_memo_citations_non_list_passthrough():
    """Non-list input is returned as-is (guard for legacy callers)."""
    from app.strategic_spawning import _summarize_memo_citations

    assert _summarize_memo_citations(None) is None  # type: ignore[arg-type]
    assert _summarize_memo_citations("invalid") == "invalid"  # type: ignore[arg-type]


def test_summarize_memo_citations_non_dict_entries_preserved():
    """Non-dict entries in the list are preserved unchanged."""
    from app.strategic_spawning import _summarize_memo_citations

    memos = [{"task_id": "t1", "summary_excerpt": "ok"}, "not-a-dict"]
    result = _summarize_memo_citations(memos)
    assert result[1] == "not-a-dict"


# ---------------------------------------------------------------------------
# _summarize_efficiency_snapshot — format_compliance and agent_timeout_rate
# ---------------------------------------------------------------------------


def test_summarize_efficiency_snapshot_includes_format_compliance():
    """format_compliance is forwarded with current/baseline/drift_pct only (no history)."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "baseline_captured_at": "2026-04-05T00:00:00",
        "last_snapshot_at": "2026-05-01T00:00:00",
        "format_compliance": {
            "format_compliance_rate": {
                "current": 0.92,
                "baseline": 0.88,
                "drift_pct": 4.5,
                "history": [{"captured_at": "2026-04-01", "value": 0.85}],
            },
            "self_repair_success_rate": {
                "current": 0.75,
                "baseline": 0.70,
                "drift_pct": 7.1,
                "history": [],
            },
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "format_compliance" in result
    fc = result["format_compliance"]
    assert "format_compliance_rate" in fc
    assert fc["format_compliance_rate"] == {
        "current": 0.92,
        "baseline": 0.88,
        "drift_pct": 4.5,
    }
    assert "history" not in fc["format_compliance_rate"]
    assert "self_repair_success_rate" in fc
    assert fc["self_repair_success_rate"]["current"] == 0.75


def test_summarize_efficiency_snapshot_includes_agent_timeout_rate():
    """agent_timeout_rate overall + dominant fields are forwarded (no history)."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
        "agent_timeout_rate": {
            "overall": {
                "current": 0.08,
                "baseline": 0.06,
                "drift_pct": 33.3,
                "history": [{"captured_at": "2026-04-01", "value": 0.07}],
            },
            "by_subtype": {"context_limit": {"current": 0.05}},
            "dominant_subtype": "context_limit",
            "dominant_share": "62%",
            "denominator": 100,
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "agent_timeout_rate" in result
    atr = result["agent_timeout_rate"]
    assert atr["overall"] == {"current": 0.08, "baseline": 0.06, "drift_pct": 33.3}
    assert "history" not in atr["overall"]
    assert atr["dominant_subtype"] == "context_limit"
    assert atr["dominant_share"] == "62%"
    assert "by_subtype" not in atr
    assert "denominator" not in atr


def test_summarize_efficiency_snapshot_forwards_axis_seed_provenance():
    """Axis provenance is preserved when it differs from baseline_captured_at."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "baseline_captured_at": "2026-04-05T00:00:00",
        "axes": {
            "llm_cost_per_merged_pr": {
                "current": 1.2,
                "baseline": 0.9,
                "drift_pct": 33.3,
                "baseline_seeded_at": "2026-04-12T00:00:00",
                "history": [],
            }
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    axis = result["axes"]["llm_cost_per_merged_pr"]
    assert axis["seeded_at"] == "2026-04-12T00:00:00"
    assert "baseline_seeded_at" not in axis


def test_summarize_efficiency_snapshot_omits_blocks_when_absent():
    """format_compliance and agent_timeout_rate keys are absent when raw snapshot lacks them."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    snapshot = {
        "axes": {
            "cost_per_merged_pr": {
                "current": 1.5,
                "baseline": 2.0,
                "drift_pct": -25.0,
                "history": [],
            }
        },
    }
    result = _summarize_efficiency_snapshot(snapshot)
    assert "format_compliance" not in result
    assert "agent_timeout_rate" not in result


def test_summarize_efficiency_snapshot_empty_returns_empty():
    """Empty/None snapshot returns empty dict."""
    from app.strategic_spawning import _summarize_efficiency_snapshot

    assert _summarize_efficiency_snapshot({}) == {}
    assert _summarize_efficiency_snapshot(None) == {}  # type: ignore[arg-type]


