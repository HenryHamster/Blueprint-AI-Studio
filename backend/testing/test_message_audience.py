"""Tests for the unified audience routing registry (message_audience.py).

Covers ``route_event`` registered-mapping correctness, the fail-closed default
(unknown event types → strategist-internal), and that ``post_event_message``
stamps the resolved audience onto every row — operator-visible for operator-*
events and non-operator (strategist-feed) for strategist-internal / fail-closed
events.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import ast
import os
from textwrap import dedent

from app.message_audience import (
    OPERATOR_ACTION_NEEDED,
    OPERATOR_FYI,
    STRATEGIST_INTERNAL,
    PIPELINE_LOG,
    get_logical_event_pairs,
    get_operator_visible_webhook_events,
    get_registered_event_types,
    is_external_webhook_operator_visible,
    is_operator_visible,
    route_event,
    route_external_webhook_event,
    stamp_message_audience,
)


# ── route_event – known mappings ────────────────────────────────────────


def test_route_event_escalation_maps_to_operator_action_needed():
    """Escalation event types that block or ask the operator."""
    assert route_event("auto_fix_escalated") == OPERATOR_ACTION_NEEDED
    assert route_event("residual_fix_escalated") == OPERATOR_ACTION_NEEDED
    assert route_event("remediation_escalated") == OPERATOR_ACTION_NEEDED
    assert route_event("direction_burn_pause") == OPERATOR_ACTION_NEEDED
    assert route_event("direction_churn_pause") == OPERATOR_ACTION_NEEDED
    assert route_event("direction_outcome_scaffolding_escalation") == OPERATOR_ACTION_NEEDED


def test_route_event_routine_maps_to_strategist_internal():
    """Routine bookkeeping event types."""
    assert route_event("pr_merged") == STRATEGIST_INTERNAL
    assert route_event("cycle_summary") == STRATEGIST_INTERNAL
    assert route_event("direction_milestone") == STRATEGIST_INTERNAL
    assert route_event("auto_fix_spawned") == STRATEGIST_INTERNAL
    assert route_event("review_feedback") == STRATEGIST_INTERNAL


def test_route_event_high_signal_maps_to_operator_fyi():
    """High-signal NOTE event types."""
    assert route_event("failure_alert") == OPERATOR_FYI
    assert route_event("rollback_recommended") == OPERATOR_FYI
    assert route_event("release_calcifying") == OPERATOR_FYI
    assert route_event("promotion_readiness_reached") == OPERATOR_FYI
    assert route_event("strategic_steering_deferred") == OPERATOR_FYI


def test_route_event_criterion_mutations_maps_to_operator_action_needed():
    """Criterion retraction/supersession removes quality gates — needs operator attention."""
    assert route_event("criterion_retracted") == OPERATOR_ACTION_NEEDED
    assert route_event("criterion_superseded") == OPERATOR_ACTION_NEEDED


def test_route_event_capability_proof_gate_graduation_split_by_mode():
    """Gate auto-graduation splits audience by target mode: observe is FYI,
    enforce (which begins blocking spawns/promotion) needs operator attention."""
    assert route_event("capability_proof_gate_graduated_observe") == OPERATOR_FYI
    assert (
        route_event("capability_proof_gate_graduated_enforce")
        == OPERATOR_ACTION_NEEDED
    )


def test_route_event_alert_types_mapped():
    """Alert event types from alert_policy.ALERT_EVENT_POLICIES are registered."""
    assert route_event("task.failed") == OPERATOR_FYI
    assert route_event("approval.needed") == OPERATOR_ACTION_NEEDED
    assert route_event("runner.deactivated") == OPERATOR_ACTION_NEEDED
    assert route_event("warm_pool.payoff") == STRATEGIST_INTERNAL
    assert route_event("migration.slot_contention") == STRATEGIST_INTERNAL


def test_route_event_self_hosted_fleet_health_alert_types_are_operator_visible():
    """Every self-hosted fleet health incident has its intended audience."""
    action_needed = {
        "runner.low_disk",
        "runner.low_memory",
        "runner.token_stale",
        "runner.fleet_no_healthy",
        "runner.post_update_silent",
        "runner.cross_project_access",
    }
    for event_type in action_needed:
        assert route_event(event_type) == OPERATOR_ACTION_NEEDED
    assert route_event("runner.idle_gpu") == OPERATOR_FYI


def test_route_event_hotfix_backmerge_size_violation_is_strategist_internal():
    """Module size-overflow alerts are strategist-internal bookkeeping."""
    assert route_event("hotfix_backmerge.size_violation") == STRATEGIST_INTERNAL


# ── route_event – fail-closed ───────────────────────────────────────────


def test_route_event_unknown_event_type_defaults_to_strategist_internal():
    """Any event_type not in the registry resolves to strategist-internal (fail-closed)."""
    assert route_event("completely.new.event_type") == STRATEGIST_INTERNAL
    assert route_event("") == STRATEGIST_INTERNAL
    assert route_event("   ") == STRATEGIST_INTERNAL
    assert route_event(None) == STRATEGIST_INTERNAL  # type: ignore[arg-type]


def test_route_event_unknown_with_message_type_fallback():
    """The message_type parameter is reserved — unknown event_type still defaults to internal."""
    assert route_event("unknown.event", message_type="NOTE") == STRATEGIST_INTERNAL


# ── is_operator_visible ────────────────────────────────────────────────


def test_is_operator_visible_true_for_operator_audiences():
    assert is_operator_visible(OPERATOR_ACTION_NEEDED) is True
    assert is_operator_visible(OPERATOR_FYI) is True


def test_is_operator_visible_false_for_internal_audiences():
    assert is_operator_visible(STRATEGIST_INTERNAL) is False
    assert is_operator_visible(PIPELINE_LOG) is False


def test_is_operator_visible_false_for_unknown_audience():
    assert is_operator_visible("unknown-audience") is False


# ── Registry completeness ───────────────────────────────────────────────


def test_registry_contains_all_known_event_types():
    """Smoke check: the registry is non-empty and contains both ledger and alert types."""
    registered = get_registered_event_types()
    assert len(registered) > 50, f"Expected 50+ entries, got {len(registered)}"
    assert "pr_merged" in registered
    assert "task.failed" in registered
    assert "hotfix_backmerge.size_violation" in registered


def test_all_alert_policy_event_types_have_registry_entry_or_explicit_audience():
    """Every key in alert_policy.ALERT_EVENT_POLICIES must be in the audience
    registry or carry an explicit non-null ``policy.audience``.

    Drift direction: a new operator-facing alert added to ALERT_EVENT_POLICIES
    without a registry entry silently fails closed to ``strategist-internal``
    and never reaches the operator.  This test forces a conscious
    operator-visibility decision for every alert event type.
    """
    from app.alert_policy import ALERT_EVENT_POLICIES

    registered = get_registered_event_types()
    missing: list[str] = []
    for event_type, policy in ALERT_EVENT_POLICIES.items():
        if event_type not in registered and policy.audience is None:
            missing.append(event_type)

    assert not missing, (
        "Alert policy event types without a registry entry or explicit audience:\n"
        + "\n".join(f"  - {et}" for et in sorted(missing))
        + "\n\nAdd each to _EVENT_AUDIENCE_REGISTRY in backend/app/message_audience.py "
        "or set an explicit ``audience`` on its AlertEventPolicy in "
        "backend/app/alert_policy.py."
    )


def test_runner_health_alert_event_constants_are_registered():
    """Runner health emitters cannot silently fall through the audience gate.

    ``runner_health_alerts`` declares its event constants at module scope and
    passes them as ``event_type`` to ``create_project_alert_if_allowed``.
    Derive that set from the source rather than duplicating it, so a future
    runner-health alert must receive an explicit audience registration.
    """
    source_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "runner_health_alerts.py"
    )
    with open(source_path, encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read(), filename=source_path)

    string_constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    emitted_event_types: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _ast_called_name(node) != "create_project_alert_if_allowed":
            continue
        for keyword in node.keywords:
            if keyword.arg != "event_type":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                emitted_event_types.add(keyword.value.value)
            elif isinstance(keyword.value, ast.Name):
                event_type = string_constants.get(keyword.value.id)
                if event_type is not None:
                    emitted_event_types.add(event_type)

    assert emitted_event_types, "Expected runner health alert emitters"
    missing = emitted_event_types - get_registered_event_types()
    assert not missing, (
        "Runner health alert event types missing from the audience registry: "
        + ", ".join(sorted(missing))
    )


# ── External-webhook audience gate ──────────────────────────────────────


def test_route_external_webhook_operator_events_are_visible():
    """Operator-facing webhook events resolve to an operator audience."""
    assert route_external_webhook_event("RUNNER_DEACTIVATED") == OPERATOR_ACTION_NEEDED
    assert route_external_webhook_event("BUDGET_EXCEEDED") == OPERATOR_ACTION_NEEDED
    assert route_external_webhook_event("APPROVAL_NEEDED") == OPERATOR_ACTION_NEEDED
    assert route_external_webhook_event("TASK_FAILED") == OPERATOR_FYI
    assert route_external_webhook_event("RUNNER_DEGRADED") == OPERATOR_FYI


def test_route_external_webhook_internal_events_are_strategist_internal():
    """Strategist-internal webhook events resolve to strategist-internal — never leak."""
    assert route_external_webhook_event("PR_MERGED") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("DIRECTION_MILESTONE") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("AUTO_FIX_SPAWNED") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("AUTO_FIX_MERGED") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("RESIDUAL_FIX_SPAWNED") == STRATEGIST_INTERNAL


def test_route_external_webhook_unmapped_fails_closed():
    """Unmapped / blank webhook event names fail closed to strategist-internal."""
    assert route_external_webhook_event("SOME_UNMAPPED_EVENT") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("") == STRATEGIST_INTERNAL
    assert route_external_webhook_event("   ") == STRATEGIST_INTERNAL
    assert route_external_webhook_event(None) == STRATEGIST_INTERNAL  # type: ignore[arg-type]


def test_is_external_webhook_operator_visible():
    """The webhook gate mirrors is_operator_visible: True for operator-*, False otherwise."""
    assert is_external_webhook_operator_visible("RUNNER_DEACTIVATED") is True
    assert is_external_webhook_operator_visible("AUTO_FIX_ESCALATED") is True
    assert is_external_webhook_operator_visible("TASK_FAILED") is True
    # Strategist-internal + unmapped are gated out.
    assert is_external_webhook_operator_visible("PR_MERGED") is False
    assert is_external_webhook_operator_visible("DIRECTION_MILESTONE") is False
    assert is_external_webhook_operator_visible("SOME_UNMAPPED_EVENT") is False


def test_operator_visible_webhook_events_excludes_internal():
    """The operator-visible webhook set contains operator events and excludes internal ones."""
    visible = get_operator_visible_webhook_events()
    assert "RUNNER_DEACTIVATED" in visible
    assert "AUTO_FIX_ESCALATED" in visible
    assert "PR_MERGED" not in visible
    assert "DIRECTION_MILESTONE" not in visible


def test_logical_event_pairs_have_consistent_audiences():
    """Every logically-paired (internal, webhook) event resolves to the same audience.

    Drift between _EVENT_AUDIENCE_REGISTRY (lowercase/dotted keys) and
    _WEBHOOK_EVENT_AUDIENCE (UPPERCASE keys) for events that represent the
    same logical operation is a silent routing bug.  This test is the
    cross-check that fails CI when a pair diverges, so an edit to one table
    that forgets to update the other is caught before shipping.
    """
    pairs = get_logical_event_pairs()
    assert pairs, "Expected at least one logical event pair to be registered"
    mismatched: list[str] = []
    for internal_key, webhook_key in pairs.items():
        internal_audience = route_event(internal_key)
        webhook_audience = route_external_webhook_event(webhook_key)
        if internal_audience != webhook_audience:
            mismatched.append(
                f"  {internal_key!r} → {internal_audience!r}  vs  "
                f"{webhook_key!r} → {webhook_audience!r}"
            )
    assert not mismatched, (
        "Logical event pairs have inconsistent audience classifications between "
        "_EVENT_AUDIENCE_REGISTRY and _WEBHOOK_EVENT_AUDIENCE:\n"
        + "\n".join(mismatched)
        + "\n\nFix by aligning the audience in one or both registries in "
        "backend/app/message_audience.py, then update _LOGICAL_EVENT_PAIRS if "
        "the events are no longer equivalent."
    )


def test_logical_event_pairs_are_registered_in_their_respective_tables():
    """Every pair entry has both its internal and webhook keys in their registries.

    Catches typos or stale entries in _LOGICAL_EVENT_PAIRS that reference a key
    that was renamed or removed from one of the tables.
    """
    pairs = get_logical_event_pairs()
    internal_registry = get_registered_event_types()
    webhook_keys = _get_all_webhook_event_keys()
    unknown: list[str] = []
    for internal_key, webhook_key in pairs.items():
        if internal_key not in internal_registry:
            unknown.append(f"  internal key {internal_key!r} not in _EVENT_AUDIENCE_REGISTRY")
        if webhook_key not in webhook_keys:
            unknown.append(f"  webhook key {webhook_key!r} not in _WEBHOOK_EVENT_AUDIENCE")
    assert not unknown, (
        "_LOGICAL_EVENT_PAIRS references keys absent from their registry:\n"
        + "\n".join(unknown)
    )


def _get_all_webhook_event_keys() -> set[str]:
    """Return all registered webhook event keys (test-only helper)."""
    from app import message_audience as _ma
    return set(getattr(_ma, "_WEBHOOK_EVENT_AUDIENCE", {}).keys())


# ── post_event_message audience gating (via notification_dispatch) ──────


@pytest.mark.asyncio
async def test_post_event_message_creates_for_operator_fyi(monkeypatch):
    """post_event_message creates a ProjectMessage for operator-fyi events."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="failure_alert",
        summary="Test failure alert",
    )
    assert msg is not None, "Expected a ProjectMessage for operator-fyi event"
    assert msg.context["audience"] == OPERATOR_FYI
    # The message was added to the session
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_tags_strategist_internal_as_non_operator(monkeypatch):
    """Strategist-internal events create a non-operator strategist-feed row.

    Per the design, ``strategist-internal`` bookkeeping still lands as a row the
    strategist may want, but is stamped ``audience="strategist-internal"`` so it
    is never operator-visible. It is NOT an operator-visible NOTE.
    """
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="pr_merged",
        summary="PR merged (bookkeeping)",
    )
    assert msg is not None, "Expected a strategist-feed row for strategist-internal event"
    assert msg.context["audience"] == STRATEGIST_INTERNAL
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_unknown_event_fails_closed_to_internal(monkeypatch):
    """Unregistered event types fail closed to strategist-internal (never operator-visible)."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="new_undocumented_event",
        summary="Brand new event",
    )
    assert msg is not None
    assert msg.context["audience"] == STRATEGIST_INTERNAL, (
        "Unregistered event must fail closed to strategist-internal, never operator-visible"
    )
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_creates_for_operator_action_needed(monkeypatch):
    """post_event_message creates a ProjectMessage for operator-action-needed events."""
    from app.notification_dispatch import post_event_message

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="direction_churn_pause",
        summary="Direction churn paused",
    )
    assert msg is not None, "Expected a ProjectMessage for operator-action-needed event"
    assert msg.context["audience"] == OPERATOR_ACTION_NEEDED
    # The message was added to the session
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_post_event_message_skips_row_for_pipeline_log(monkeypatch):
    """Pipeline-log events are pure telemetry — no ledger row is created (returns None).

    No registered event currently maps to ``pipeline-log``, so patch the resolver
    to exercise the skip branch directly.
    """
    import app.notification_dispatch as nd

    monkeypatch.setattr(nd, "route_event", lambda *a, **k: PIPELINE_LOG)

    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    msg = await nd.post_event_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        event_type="some.telemetry.event",
        summary="telemetry only",
    )
    assert msg is None, "Expected None (no ledger row) for pipeline-log event"
    mock_session.add.assert_not_called()


# ── stamp_message_audience (shared stamping primitive) ──────────────────


def test_stamp_message_audience_stamps_operator_audience():
    """The shared stamp helper writes the canonical operator audience."""
    ctx = stamp_message_audience({"branch": "main"}, "auto_fix_escalated")
    assert ctx["audience"] == OPERATOR_ACTION_NEEDED
    assert ctx["event_type"] == "auto_fix_escalated"
    assert ctx["branch"] == "main"  # caller keys preserved


def test_stamp_message_audience_fails_closed_for_unknown_event():
    """An unregistered event_type stamps strategist-internal (fail-closed)."""
    ctx = stamp_message_audience({}, "brand.new.unregistered.event")
    assert ctx["audience"] == STRATEGIST_INTERNAL


def test_stamp_message_audience_does_not_mutate_caller_dict():
    """The helper returns a copy; the caller's dict is untouched."""
    original = {"foo": "bar"}
    result = stamp_message_audience(original, "pr_merged")
    assert "audience" not in original, "caller dict must not be mutated"
    assert result["audience"] == STRATEGIST_INTERNAL
    assert result is not original


def test_stamp_message_audience_registry_wins_over_caller_audience():
    """A caller-supplied audience never overrides the registry resolution."""
    ctx = stamp_message_audience(
        {"audience": OPERATOR_FYI}, "pr_merged"
    )
    assert ctx["audience"] == STRATEGIST_INTERNAL


def test_stamp_message_audience_handles_none_context():
    ctx = stamp_message_audience(None, "direction_burn_pause")
    assert ctx == {
        "event_type": "direction_burn_pause",
        "audience": OPERATOR_ACTION_NEEDED,
    }


# ── Fail-closed structural guard: no operator-reaching emitter bypasses ──
#
# post_event_message stamps the audience inline; the directly-constructed
# BLOCKER escalation rows in notification_dispatch.py must route through the
# same gate (stamp_message_audience) instead of building a raw context dict.
# This AST scan is the structural ratchet: a NEW operator-reaching (system,
# agent, or strategic-authored) ProjectMessage added to notification_dispatch.py
# without an audience stamp in its enclosing function fails CI, closing the
# fail-closed guarantee's hole.

_AUDIENCE_STAMP_CALLS = frozenset({"stamp_message_audience", "route_event"})
_OPERATOR_REACHING_FROM_TYPES: frozenset[str] = frozenset({"system", "agent", "strategic"})


def _ast_called_name(call: ast.Call) -> str | None:
    """Return the bare callee name for ``foo(...)`` or ``mod.foo(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _collect_ungated_emitters(source: str, *, filename: str, rel: str) -> list[str]:
    """Return offender strings for operator-reaching ProjectMessage sites whose
    enclosing function does not call an audience-stamping helper."""

    def _param_defaults(func_node) -> dict[str, ast.expr]:
        """Map each parameter name that has a default to its default AST node.

        Covers positional-or-keyword defaults (tail-aligned to ``args.args``) and
        keyword-only defaults (``kwonlyargs``/``kw_defaults``). Parameters without a
        default are absent from the map.
        """
        fargs = func_node.args
        defaults: dict[str, ast.expr] = {}
        posargs = fargs.args
        if fargs.defaults:
            for name, default in zip(posargs[-len(fargs.defaults):], fargs.defaults):
                defaults[name.arg] = default
        for name, default in zip(fargs.kwonlyargs, fargs.kw_defaults):
            if default is not None:
                defaults[name.arg] = default
        return defaults

    def _is_operator_reaching_message_call(node: ast.Call, enclosing) -> bool:
        """A ProjectMessage(...) construction whose ``from_type`` provably reaches
        the operator surface (system/agent/strategic).

        ``from_type`` is operator-reaching when it is either a literal constant in
        the operator-reaching set, OR a bare parameter reference whose *default*
        value is such a constant. The defaulted-parameter case closes the hole
        where an emitter (e.g. ``post_question_answer(from_type="strategic")``)
        passes ``from_type=from_type`` — a non-literal that the constant-only check
        skipped even though its default reaches the operator. A parameter with no
        resolvable constant default stays skipped (its runtime value is unknown at
        scan time)."""
        if _ast_called_name(node) != "ProjectMessage":
            return False
        param_defaults = _param_defaults(enclosing) if enclosing is not None else {}
        for kw in node.keywords:
            if kw.arg != "from_type":
                continue
            val = kw.value
            if isinstance(val, ast.Constant):
                return val.value in _OPERATOR_REACHING_FROM_TYPES
            if isinstance(val, ast.Name):
                default = param_defaults.get(val.id)
                if isinstance(default, ast.Constant):
                    return default.value in _OPERATOR_REACHING_FROM_TYPES
            return False
        return False

    def _stamps_audience(func_node) -> bool:
        return any(
            isinstance(n, ast.Call) and _ast_called_name(n) in _AUDIENCE_STAMP_CALLS
            for n in ast.walk(func_node)
        )

    tree = ast.parse(source, filename=filename)
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _ast_called_name(node) != "ProjectMessage":
            continue
        enclosing = None
        for fn in funcs:
            if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                if enclosing is None or fn.lineno > enclosing.lineno:
                    enclosing = fn
        if not _is_operator_reaching_message_call(node, enclosing):
            continue
        func_name = enclosing.name if enclosing is not None else "<module>"
        if enclosing is None or not _stamps_audience(enclosing):
            offenders.append(
                f"  {rel}:{node.lineno} in {func_name}() — operator-reaching ProjectMessage "
                "site not routed through the audience gate (no "
                "stamp_message_audience/route_event call)"
            )
    return offenders


def test_scan_flags_unstamped_system_message():
    """A system ProjectMessage with no audience-stamping call is flagged."""
    source = dedent(
        """
        async def post_thing(session, project_id, content):
            ctx = {"event_type": "some_escalation"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="system",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:4 in post_thing() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_system_message():
    """A system ProjectMessage whose function stamps the audience passes."""
    source = dedent(
        """
        async def post_thing(session, project_id, content):
            ctx = {"event_type": "some_escalation"}
            ctx = stamp_message_audience(ctx, "some_escalation")
            msg = ProjectMessage(
                project_id=project_id,
                from_type="system",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_flags_unstamped_agent_message():
    """An agent-authored ProjectMessage with no audience-stamping call is flagged."""
    source = dedent(
        """
        async def post_agent_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="agent",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:4 in post_agent_thing() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_agent_message():
    """An agent-authored ProjectMessage whose function stamps the audience passes."""
    source = dedent(
        """
        async def post_agent_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            ctx = stamp_message_audience(ctx, "some_event")
            msg = ProjectMessage(
                project_id=project_id,
                from_type="agent",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_flags_unstamped_strategic_message():
    """A strategic-authored ProjectMessage with no audience-stamping call is flagged."""
    source = dedent(
        """
        async def post_strategic_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="strategic",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:4 in post_strategic_thing() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_strategic_message():
    """A strategic-authored ProjectMessage whose function stamps the audience passes."""
    source = dedent(
        """
        async def post_strategic_thing(session, project_id, content):
            ctx = {"event_type": "some_event"}
            ctx = stamp_message_audience(ctx, "some_event")
            msg = ProjectMessage(
                project_id=project_id,
                from_type="strategic",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_skips_user_message():
    """A user-authored ProjectMessage is NOT operator-reaching — it is skipped."""
    source = dedent(
        """
        async def post_user_thing(session, project_id, content):
            ctx = {"event_type": "chat"}
            msg = ProjectMessage(
                project_id=project_id,
                from_type="user",
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_skips_non_constant_from_type():
    """A ProjectMessage whose from_type is a variable (not a constant) is skipped —
    it cannot be proven operator-reaching at scan time."""
    source = dedent(
        """
        async def post_variable_thing(session, project_id, content, from_type):
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context={},
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_flags_defaulted_operator_reaching_from_type():
    """A ProjectMessage whose from_type is a parameter that DEFAULTS to an
    operator-reaching constant (e.g. "strategic") is flagged when unstamped — the
    default reaches the operator even though the argument is not a literal at the
    construction site."""
    source = dedent(
        """
        async def post_answer(session, project_id, content, from_type="strategic"):
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context={"event_type": "question_answer"},
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == [
        "  synthetic.py:3 in post_answer() — operator-reaching ProjectMessage site not "
        "routed through the audience gate (no stamp_message_audience/route_event call)"
    ]


def test_scan_accepts_stamped_defaulted_operator_reaching_from_type():
    """The same defaulted-from_type emitter passes once its function stamps."""
    source = dedent(
        """
        async def post_answer(session, project_id, content, from_type="strategic"):
            ctx = stamp_message_audience({"event_type": "question_answer"}, "question_answer")
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context=ctx,
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_scan_skips_defaulted_user_from_type():
    """A parameter that defaults to "user" is NOT operator-reaching — skipped."""
    source = dedent(
        """
        async def post_user_default(session, project_id, content, from_type="user"):
            msg = ProjectMessage(
                project_id=project_id,
                from_type=from_type,
                content=content,
                context={},
            )
            session.add(msg)
            return msg
        """
    )
    offenders = _collect_ungated_emitters(
        source, filename="synthetic.py", rel="synthetic.py"
    )
    assert offenders == []


def test_no_operator_visible_emitter_bypasses_audience_gate():
    """Whole-file invariant: every operator-reaching ProjectMessage constructed in
    notification_dispatch.py routes its audience through the shared gate.

    Exercises the production stamping seam: reverting any of the seven
    escalation-poster stamps (or adding a new raw operator-reaching
    ProjectMessage) makes this scan flag the offending site and fail CI.
    """
    app_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app")
    )
    fpath = os.path.join(app_dir, "notification_dispatch.py")
    with open(fpath, encoding="utf-8") as f:
        source = f.read()

    offenders = _collect_ungated_emitters(
        source, filename=fpath, rel="notification_dispatch.py"
    )
    assert not offenders, (
        "Found operator-reaching ProjectMessage site(s) in notification_dispatch.py "
        "that do NOT route their audience through the shared gate "
        "(stamp_message_audience/route_event). Every operator-reaching emitter must "
        "be stamped so a fail-closed audience classification is guaranteed:\n"
        + "\n".join(offenders)
    )


# ── Behavioral: escalation posters stamp an operator-visible audience ────


@pytest.mark.asyncio
async def test_auto_fix_escalation_message_stamps_operator_action_needed(monkeypatch):
    """post_auto_fix_escalation_message stamps operator-action-needed on the row."""
    import app.notification_dispatch as nd

    monkeypatch.setattr(
        nd, "open_system_bug_or_blocker_exists", AsyncMock(return_value=None)
    )
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await nd.post_auto_fix_escalation_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        branch="main",
        failure_count=5,
    )
    assert msg.context["audience"] == OPERATOR_ACTION_NEEDED
    assert msg.context["event_type"] == "auto_fix_escalated"
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_residual_fix_escalation_message_stamps_operator_action_needed(
    monkeypatch,
):
    """post_residual_fix_escalation_message stamps operator-action-needed."""
    import app.notification_dispatch as nd

    monkeypatch.setattr(
        nd, "open_system_bug_or_blocker_exists", AsyncMock(return_value=None)
    )
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    msg = await nd.post_residual_fix_escalation_message(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        fingerprint="abc123",
        failure_count=4,
    )
    assert msg.context["audience"] == OPERATOR_ACTION_NEEDED
    assert msg.context["event_type"] == "residual_fix_escalated"
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_question_answer_stamps_audience_default_from_type(monkeypatch):
    """post_question_answer stamps the registry audience on its context even with
    the default operator-reaching from_type="strategic". Binds the reply's
    visibility to the registry (OPERATOR_FYI today) instead of leaving it an
    unstamped, always-visible row."""
    import uuid as _uuid

    import app.notification_dispatch as nd

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    monkeypatch.setattr(
        "app.project_message_creation.lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        nd,
        "lock_project_and_next_message_sequence",
        AsyncMock(return_value=1),
    )

    orig_id = _uuid.uuid4()
    msg = await nd.post_question_answer(
        mock_session,
        project_id="00000000-0000-0000-0000-000000000001",
        original_msg_id=orig_id,
        answer="here you go",
    )
    # Default from_type is the operator-reaching "strategic".
    assert msg.from_type == "strategic"
    assert msg.context["event_type"] == "question_answer"
    assert msg.context["audience"] == OPERATOR_FYI
    assert msg.context["reply_to"] == str(orig_id)
    assert msg.sequence == 1
    assert msg.surface == "conversation"
    assert msg.handling_state == "responded"
    assert msg.reply_to_message_id == orig_id
    mock_session.add.assert_called_once()
