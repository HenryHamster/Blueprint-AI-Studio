"""Tests for the agent pipeline-visibility read surfaces
(app.routers.agent_pipeline_visibility) and the shared byte budget
(app.agent_read_budget):

  GET /api/agent/projects/{project_id}/attention
  GET /api/agent/projects/{project_id}/alerts
  GET /api/agent/projects/{project_id}/autonomy-debug
  GET /api/agent/projects/{project_id}/deploy-state

Three things are being defended here, in order of importance:

1. **The byte ceiling.** Every one of these responses lands in the strategic
   envelope, which degrades into a coreless stub — and livelocks the reclaim
   loop — above MAX_CANONICAL_ENVELOPE_BYTES (PR #17424). The cap is pinned
   both as a literal and as its derivation, each surface is driven with
   worst-case data to prove the transmitted body actually fits, and the clamp
   is required to *report* a miss rather than trim quietly and hope.
2. **The trust boundary.** Authority is server-supplied from the resolved
   project; a cross-project path is a 404, not a 403; no route mutates; and no
   route reaches an admin computation behind a fabricated principal.
3. **The projections themselves** — that the counts survive truncation, that
   aggregate maps are folded rather than left unbounded, that operator
   read-cursor state is stripped, that /deploy-state exposes an allowlist
   rather than whatever the admin builder happens to return, and that the
   delegated admin computations are called with a signature that actually
   works (the real functions are exercised; only their network leaf is
   stubbed).
"""

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.agent_read_budget import (
    AGENT_READ_AGGREGATE_TOP_N,
    AGENT_READ_BOUNDS_RESERVE_BYTES,
    AGENT_READ_MAX_KEY_BYTES,
    AGENT_READ_MAX_RESPONSE_BYTES,
    AGENT_READ_MAX_TEXT_BYTES,
    bound_counts,
    clamp_read_response,
    clip_text,
    serialized_bytes,
)
from app.auth import create_agent_token
from app.models import Alert, AlertSeverity, Project, ProjectMember, Task
from app.routers import agent_pipeline_visibility
from app.routers.agent_pipeline_visibility import (
    ATTENTION_MAX_ITEMS,
    _DEPLOY_STATE_FIELDS,
    project_operator_attention,
)
from app.routers.agent_pipeline_visibility import router as visibility_router
from app.strategic_memory_envelope import MAX_CANONICAL_ENVELOPE_BYTES

# An event_type registered as operator-visible; the alert filter is fail-closed,
# so an unregistered type is invisible by construction.
OPERATOR_EVENT_TYPE = "failure_alert"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_project_and_task(db_session, *, name: str = "Visibility Test"):
    """Create a project (with its owner membership row) plus the calling task.

    The owner ``ProjectMember`` row is not decoration: the delegated admin
    computations resolve membership through ``ProjectMember``, not through
    ``Project.user_id``, so a project whose owner has no membership row is a 404
    to its own owner. Production projects always carry one.
    """
    project = Project(name=name, user_id="test-user-visibility")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(
            project_id=project.id, user_id="test-user-visibility", role="owner"
        )
    )
    await db_session.commit()

    calling_task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-visibility"
    )
    db_session.add(calling_task)
    await db_session.commit()
    await db_session.refresh(calling_task)
    return project, calling_task


def _auth(calling_task: Task) -> dict[str, str]:
    return {
        "x-agent-token": create_agent_token(str(calling_task.id), "agent-vm-visibility")
    }


async def _add_alert(
    db_session,
    project: Project,
    *,
    severity: AlertSeverity = AlertSeverity.ERROR,
    event_type: str | None = OPERATOR_EVENT_TYPE,
    title: str = "alert title",
    message: str = "alert message",
    created_at: datetime | None = None,
    dismissed_at: datetime | None = None,
) -> Alert:
    alert = Alert(
        project_id=project.id,
        severity=severity,
        event_type=event_type,
        title=title,
        message=message,
        dismissed_at=dismissed_at,
    )
    if created_at is not None:
        alert.created_at = created_at
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)
    return alert


def _body_bytes(data) -> int:
    import json

    return len(
        json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


# ---------------------------------------------------------------------------
# app.agent_read_budget — the ceiling itself
# ---------------------------------------------------------------------------


def test_read_budget_is_a_quarter_of_the_strategic_envelope():
    """The ceiling is pinned as a literal AND as its derivation.

    Asserting only ``AGENT_READ_MAX_RESPONSE_BYTES == MAX_CANONICAL_ENVELOPE_BYTES
    // 4`` would be self-certifying: a change to either constant would keep the
    test green while silently moving the livelock threshold (PR #17424). The
    literals are therefore stated outright, so raising the cap has to be an
    explicit, reviewable edit to this file.

    The aggregate bounds are pinned as literals for the same reason: they are
    what keeps an unbounded key vocabulary from breaching the ceiling, and a
    silent widening would restore exactly the defect that motivated them.
    """
    assert MAX_CANONICAL_ENVELOPE_BYTES == 64 * 1024 == 65536
    assert AGENT_READ_MAX_RESPONSE_BYTES == 16384
    assert AGENT_READ_MAX_RESPONSE_BYTES == MAX_CANONICAL_ENVELOPE_BYTES // 4
    # Blast radius, NOT a capacity promise: one read may consume at most a
    # quarter of the envelope ceiling. It does not follow that four reads fit —
    # the envelope also carries the canonical core, and nothing at this layer
    # counts reads per turn. See the agent_read_budget module docstring.
    assert AGENT_READ_MAX_RESPONSE_BYTES * 4 <= MAX_CANONICAL_ENVELOPE_BYTES
    assert AGENT_READ_MAX_TEXT_BYTES == 240
    assert 0 < AGENT_READ_BOUNDS_RESERVE_BYTES < AGENT_READ_MAX_RESPONSE_BYTES
    assert AGENT_READ_AGGREGATE_TOP_N == 20
    assert AGENT_READ_MAX_KEY_BYTES == 64
    # Worst case for one folded map: every key at the key ceiling, every value
    # a wide nested breakdown. Must stay a small fraction of the response cap,
    # or bounding cardinality would not have bounded anything.
    assert (
        (AGENT_READ_AGGREGATE_TOP_N + 1) * (AGENT_READ_MAX_KEY_BYTES + 8)
        < AGENT_READ_MAX_RESPONSE_BYTES // 8
    )


def test_clip_text_trims_on_a_utf8_boundary_and_preserves_none():
    assert clip_text(None) is None
    assert clip_text("short") == "short"

    clipped = clip_text("x" * 5000)
    assert clipped is not None
    assert clipped.endswith("…")
    assert len(clipped.encode("utf-8")) <= AGENT_READ_MAX_TEXT_BYTES + 3

    # Multi-byte text must not be cut mid-codepoint (would raise on decode).
    multibyte = clip_text("é" * 5000)
    assert multibyte is not None
    multibyte.encode("utf-8").decode("utf-8")
    assert len(multibyte.encode("utf-8")) <= AGENT_READ_MAX_TEXT_BYTES + 3


def test_clamp_read_response_sheds_rows_and_never_counts():
    payload = {
        "total": 5000,
        "by_status": {"BLOCKED": 4000, "QUEUED": 1000},
        "rows": [{"text": "y" * 900} for _ in range(200)],
    }
    clamped = clamp_read_response(
        payload, trim_order=("rows",), returned_counts={"rows_returned": "rows"}
    )

    assert serialized_bytes(clamped) <= AGENT_READ_MAX_RESPONSE_BYTES
    # The aggregate answer is intact; only the examples were dropped.
    assert clamped["total"] == 5000
    assert clamped["by_status"] == {"BLOCKED": 4000, "QUEUED": 1000}
    assert 0 < len(clamped["rows"]) < 200
    bounds = clamped["bounds"]
    assert bounds["truncated"] is True
    assert bounds["truncated_sections"] == ["rows"]
    assert bounds["max_response_bytes"] == AGENT_READ_MAX_RESPONSE_BYTES
    assert bounds["rows_returned"] == len(clamped["rows"])
    # Trimming worked: this is a truncated answer, not a missed bound.
    assert bounds["over_budget"] is False
    # response_bytes counts itself: it is the exact transmitted length.
    assert bounds["response_bytes"] == serialized_bytes(clamped)


def test_clamp_read_response_leaves_a_small_payload_untouched():
    payload = {"total": 2, "rows": [{"a": 1}, {"a": 2}]}
    clamped = clamp_read_response(payload, trim_order=("rows",))
    assert clamped["rows"] == [{"a": 1}, {"a": 2}]
    assert clamped["bounds"]["truncated"] is False
    assert clamped["bounds"]["over_budget"] is False
    assert "truncated_sections" not in clamped["bounds"]


def test_clamp_read_response_flags_a_ceiling_it_could_not_reach():
    """An untrimmable map over the ceiling must SAY so, not report success.

    This is the exact shape that shipped ~2x the cap while reporting only
    ``truncated: True``: the trim order names a list, the bytes are in an
    aggregate map, and emptying the list changes nothing. ``truncated`` says
    trimming happened; only ``over_budget`` says whether it was enough.
    """
    payload = {
        "by_type": {f"type_{i:04d}": {"QUEUED": 1, "RUNNING": 2} for i in range(1000)},
        "rows": [{"text": "y" * 400} for _ in range(50)],
    }
    clamped = clamp_read_response(payload, trim_order=("rows",))

    # Everything trimmable was shed, and it still did not fit.
    assert clamped["rows"] == []
    assert serialized_bytes(clamped) > AGENT_READ_MAX_RESPONSE_BYTES
    assert clamped["bounds"]["truncated"] is True
    assert clamped["bounds"]["over_budget"] is True

    # Folding the map is what actually fixes it, and that is the caller's job.
    refixed = clamp_read_response(
        {
            "by_type": bound_counts(payload["by_type"]),
            "rows": [{"text": "y" * 400} for _ in range(50)],
        },
        trim_order=("rows",),
    )
    assert serialized_bytes(refixed) <= AGENT_READ_MAX_RESPONSE_BYTES
    assert refixed["bounds"]["over_budget"] is False


# ---------------------------------------------------------------------------
# bound_counts — the bound clamp_read_response cannot apply for you
# ---------------------------------------------------------------------------


def _count_total(counts) -> int:
    """Sum a flat ``{k: int}`` or nested ``{k: {sub: int}}`` count map."""
    total = 0
    for value in counts.values():
        total += sum(value.values()) if isinstance(value, dict) else value
    return total


def test_bound_counts_folds_the_tail_and_keeps_the_total_exact():
    counts = {f"type_{i:03d}": i + 1 for i in range(300)}
    bounded = bound_counts(counts)

    assert len(bounded) == AGENT_READ_AGGREGATE_TOP_N + 1
    # Heaviest keys survive by name; the tail is folded, never dropped.
    assert bounded["type_299"] == 300
    assert "type_000" not in bounded
    assert "other" in bounded
    assert _count_total(bounded) == _count_total(counts) == sum(range(1, 301))


def test_bound_counts_preserves_the_nested_breakdown_shape():
    counts = {f"type_{i:03d}": {"QUEUED": 1, "RUNNING": 2} for i in range(100)}
    bounded = bound_counts(counts)

    assert len(bounded) == AGENT_READ_AGGREGATE_TOP_N + 1
    # Equal weights tie-break on the key, so the fold is deterministic.
    assert bounded["type_000"] == {"QUEUED": 1, "RUNNING": 2}
    assert "type_099" not in bounded
    # The bucket keeps the breakdown rather than collapsing to a scalar.
    assert bounded["other"] == {"QUEUED": 80, "RUNNING": 160}
    assert _count_total(bounded) == _count_total(counts) == 300


def test_bound_counts_clips_long_keys_and_merges_the_collisions():
    """A bounded key count is not a bound if one key can be 200 KB."""
    prefix = "t" * 200
    counts = {prefix + "a": 5, prefix + "b": 7, "short": 1}
    bounded = bound_counts(counts)

    assert bounded["short"] == 1
    (merged_key,) = [key for key in bounded if key != "short"]
    assert len(merged_key.encode("utf-8")) <= AGENT_READ_MAX_KEY_BYTES + 3
    # Clipping collided the two keys; the total still has to be right.
    assert bounded[merged_key] == 12
    assert _count_total(bounded) == 13


def test_bound_counts_merges_a_literal_other_key_with_the_fold():
    """An upstream key named ``other`` must not be shadowed by the bucket."""
    counts = {"other": 1000, **{f"t{i:03d}": 1 for i in range(50)}}
    bounded = bound_counts(counts)

    assert len(bounded) == AGENT_READ_AGGREGATE_TOP_N
    assert bounded["other"] == 1000 + 31
    assert _count_total(bounded) == _count_total(counts) == 1050


def test_bound_counts_leaves_a_small_map_alone():
    assert bound_counts({"a": 1, "b": 2}) == {"a": 1, "b": 2}
    assert bound_counts(None) == {}


# ---------------------------------------------------------------------------
# project_operator_attention — pure projection shared with the spawn payload
# ---------------------------------------------------------------------------


def test_project_operator_attention_strips_operator_read_cursors():
    """``unread``/``last_seen_at`` describe Henry's browser, not the world.

    The resident can neither hold nor advance a read position, so reporting one
    would invite it to treat "unread" as a fact about the pipeline. This mirrors
    agent_access._remove_actor_relative_read_state.
    """
    attention = {
        "level": "attention",
        "summary": "3 items need a look",
        "last_seen_at": "2026-08-20T00:00:00+00:00",
        "has_more": True,
        "counts": {"needs_operator": 3, "watch": 1, "unread": 7},
        "items": [
            {"kind": "message_blocker", "label": f"item {i}", "unread": True}
            for i in range(30)
        ],
    }

    projected = project_operator_attention(attention)

    assert "last_seen_at" not in projected
    assert "unread" not in projected["counts"]
    assert projected["counts"]["needs_operator"] == 3
    assert projected["level"] == "attention"
    assert projected["has_more"] is True
    assert len(projected["items"]) == ATTENTION_MAX_ITEMS
    assert all("unread" not in item for item in projected["items"])
    assert projected["bounds"]["max_items"] == ATTENTION_MAX_ITEMS
    # Pure function: the caller's dict is not mutated.
    assert attention["last_seen_at"] == "2026-08-20T00:00:00+00:00"
    assert len(attention["items"]) == 30


def test_project_operator_attention_clips_free_text():
    projected = project_operator_attention(
        {"summary": "s" * 4000, "counts": {}, "items": []}
    )
    assert len(projected["summary"].encode("utf-8")) <= AGENT_READ_MAX_TEXT_BYTES + 3
    assert projected["bounds"]["max_response_bytes"] == AGENT_READ_MAX_RESPONSE_BYTES


def test_project_operator_attention_is_byte_clamped_inside_the_projection():
    """The clamp belongs to the projection, not to the HTTP call site.

    ``strategic_spawning._spawn_strategic_review`` embeds this same block in
    the review task's payload — the copy that goes straight into the envelope
    the cap exists to protect — and wraps the call in ``except Exception``. A
    clamp applied at the route would leave that copy unbounded and a failure
    there silent. Bounding here means no caller can obtain an unbounded
    variant, so this asserts the bound on the function, not on a response.
    """
    attention = {
        "level": "attention",
        "summary": "Backlog",
        "counts": {"needs_operator": 900},
        "items": [
            {"kind": "message_blocker", **{f"field_{j}": "D" * 400 for j in range(16)}}
            for _ in range(ATTENTION_MAX_ITEMS)
        ],
    }

    projected = project_operator_attention(attention)

    assert serialized_bytes(projected) <= AGENT_READ_MAX_RESPONSE_BYTES
    assert projected["bounds"]["over_budget"] is False
    # It fit by shedding items, which is the trim order actually being used.
    assert projected["bounds"]["truncated"] is True
    assert projected["bounds"]["truncated_sections"] == ["items"]
    assert 0 < len(projected["items"]) < ATTENTION_MAX_ITEMS
    # Counts are never the thing that gets shed.
    assert projected["counts"]["needs_operator"] == 900


# ---------------------------------------------------------------------------
# Router-level invariants
# ---------------------------------------------------------------------------


def test_every_visibility_route_is_read_only():
    """Read surfaces only: this router must never grow a mutating verb."""
    for route in visibility_router.routes:
        methods = set(getattr(route, "methods", set()) or set())
        assert methods <= {"GET", "HEAD"}, (route.path, methods)


# ---------------------------------------------------------------------------
# GET /attention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attention_returns_the_bounded_operator_census(
    client: AsyncClient, db_session
):
    """The canonical attention census, computed for real, bounded on the way out."""
    project, calling_task = await _make_project_and_task(db_session, name="Attn")
    await _add_alert(db_session, project, title="something broke")

    res = await client.get(
        f"/api/agent/projects/{project.id}/attention", headers=_auth(calling_task)
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert "level" in data
    assert "counts" in data
    assert isinstance(data["items"], list)
    assert data["bounds"]["max_items"] == ATTENTION_MAX_ITEMS
    assert data["bounds"]["max_response_bytes"] == AGENT_READ_MAX_RESPONSE_BYTES
    assert _body_bytes(data) <= AGENT_READ_MAX_RESPONSE_BYTES
    # Operator read-cursor state never reaches the agent.
    assert "last_seen_at" not in data
    assert "unread" not in data["counts"]


# ---------------------------------------------------------------------------
# GET /alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alerts_leads_with_exact_aggregates_over_the_window(
    client: AsyncClient, db_session
):
    """Counts are exact for the declared frame; rows are only examples.

    The alert feed is dominated by point-in-time notices minted durable, so a
    default page of it characterises nothing. What must be right is the frame:
    this project, undismissed, operator-visible, inside the window.
    """
    project, calling_task = await _make_project_and_task(db_session, name="Alerts")
    now = datetime.now(timezone.utc)

    for _ in range(3):
        await _add_alert(db_session, project, severity=AlertSeverity.ERROR)
    await _add_alert(db_session, project, severity=AlertSeverity.WARNING)
    # Excluded three different ways.
    await _add_alert(db_session, project, dismissed_at=now.replace(tzinfo=None))
    await _add_alert(db_session, project, created_at=now - timedelta(days=9))
    await _add_alert(db_session, project, event_type="not_an_operator_event_type")
    # Another project's alert must not leak into this project's counts.
    other = Project(name="Alerts Other", user_id="someone-else")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    await _add_alert(db_session, other)

    res = await client.get(
        f"/api/agent/projects/{project.id}/alerts", headers=_auth(calling_task)
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["window_hours"] == 24
    assert data["total"] == 4
    assert data["by_severity"] == {"ERROR": 3, "WARNING": 1}
    assert data["by_event_type"] == {OPERATOR_EVENT_TYPE: 4}
    assert data["other_event_types"] == 0
    assert data["bounds"]["counts_are_exact_for_window"] is True
    assert len(data["recent"]) == 4
    assert data["bounds"]["recent_returned"] == 4
    assert _body_bytes(data) <= AGENT_READ_MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_alerts_stays_under_the_cap_with_worst_case_rows(
    client: AsyncClient, db_session
):
    """Max recent rows, all with oversized free text, still fits the ceiling."""
    project, calling_task = await _make_project_and_task(db_session, name="AlertsCap")
    for _ in range(30):
        await _add_alert(
            db_session, project, title="T" * 4000, message="M" * 4000
        )

    res = await client.get(
        f"/api/agent/projects/{project.id}/alerts?recent=25",
        headers=_auth(calling_task),
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["total"] == 30  # counts survive whatever the clamp did
    assert _body_bytes(data) <= AGENT_READ_MAX_RESPONSE_BYTES
    assert data["bounds"]["response_bytes"] <= AGENT_READ_MAX_RESPONSE_BYTES
    for row in data["recent"]:
        assert len(row["title"].encode("utf-8")) <= AGENT_READ_MAX_TEXT_BYTES + 3
        assert len(row["message"].encode("utf-8")) <= AGENT_READ_MAX_TEXT_BYTES + 3


# ---------------------------------------------------------------------------
# Removed surface: GET /orchestrator-errors
# ---------------------------------------------------------------------------


def test_no_agent_route_reaches_the_process_wide_orchestrator_error_log():
    """The orchestrator-errors read was removed, and must not come back.

    It called ``get_recent_orchestrator_errors`` — a ``Depends(require_admin)``
    handler — with a fabricated ``{"user_id": "AGENT_READ"}`` principal. The
    underlying Cloud Logging query filters on the orchestrator service name and
    a severity floor with no project dimension at all, so there was nothing to
    scope: any project's agent token read raw tracebacks for the entire control
    plane. It could not be narrowed, so it was deleted rather than papered over.

    This asserts the absence structurally, because the natural "fix" for a
    missing surface is to re-add the handler exactly as it was.
    """
    paths = [getattr(route, "path", "") for route in visibility_router.routes]
    assert not any("orchestrator" in path for path in paths), paths

    # A synthetic principal is the mechanism, not just this one route: no agent
    # surface may invent an identity to satisfy an admin dependency. Checked on
    # the AST rather than the text so the module can keep explaining *why* the
    # surface was removed without the explanation tripping its own guard.
    tree = ast.parse(Path(agent_pipeline_visibility.__file__).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert not [text for text in literals if "AGENT_READ" in text], literals

    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    referenced |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            referenced |= {alias.name for alias in node.names}
    assert "get_recent_orchestrator_errors" not in referenced


@pytest.mark.asyncio
async def test_orchestrator_errors_path_is_not_served(
    client: AsyncClient, db_session
):
    project, calling_task = await _make_project_and_task(db_session, name="OrchGone")
    res = await client.get(
        f"/api/agent/projects/{project.id}/orchestrator-errors",
        headers=_auth(calling_task),
    )
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# GET /autonomy-debug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autonomy_debug_runs_the_real_admin_diagnostic(
    client: AsyncClient, db_session
):
    """No stub: proves the direct call into the admin handler actually works."""
    project, calling_task = await _make_project_and_task(db_session, name="Autonomy")
    res = await client.get(
        f"/api/agent/projects/{project.id}/autonomy-debug",
        headers=_auth(calling_task),
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert "spawn_blocked" in data
    assert "blocking_guards" in data
    assert data["directions_summary"]["active_total"] == 0
    assert data["bounds"]["cycle_history_max"] == 5
    assert _body_bytes(data) <= AGENT_READ_MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_autonomy_debug_puts_stranded_directions_first(
    client: AsyncClient, db_session, monkeypatch
):
    """Ineligible before eligible: the stranded row must not fall off the end."""
    import app.routers.admin_debug as admin_debug

    async def _fake(project_id, session, current_user):
        return {
            "spawn_blocked": True,
            "automatic_strategic_cycles_enabled": True,
            "blocking_guards": [{"name": "budget", "detail": "D" * 3000}],
            "directions": (
                [
                    {
                        "id": f"eligible-{i}",
                        "text": "E" * 3000,
                        "is_eligible": True,
                        "block_reason_code": None,
                    }
                    for i in range(40)
                ]
                + [
                    {
                        "id": "stranded",
                        "text": "waiting on evidence",
                        "is_eligible": False,
                        "seconds_until_eligible": 900,
                        "block_reason_code": "EVIDENCE_PENDING",
                        "block_reason": "discovery snapshot truncated",
                        "next_action": "resolve-discovery",
                        "discovery_admission": {"can_admit_root": False},
                    }
                ]
            ),
            "strategic_cycle_history": [{"cycle": i} for i in range(50)],
        }

    monkeypatch.setattr(admin_debug, "get_autonomy_debug", _fake)

    project, calling_task = await _make_project_and_task(db_session, name="Stranded")
    res = await client.get(
        f"/api/agent/projects/{project.id}/autonomy-debug",
        headers=_auth(calling_task),
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["spawn_blocked"] is True
    assert data["directions_summary"] == {
        "active_total": 41,
        "eligible": 40,
        "blocked_by_code": {"EVIDENCE_PENDING": 1},
    }
    # The one stranded direction survives both the item cap and the byte clamp.
    assert data["directions"][0]["id"] == "stranded"
    assert data["directions"][0]["block_reason_code"] == "EVIDENCE_PENDING"
    assert data["directions"][0]["can_admit_root"] is False
    assert len(data["strategic_cycle_history"]) <= 5
    assert _body_bytes(data) <= AGENT_READ_MAX_RESPONSE_BYTES


# ---------------------------------------------------------------------------
# GET /deploy-state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_state_projects_an_allowlist_not_a_denylist(
    client: AsyncClient, db_session, monkeypatch
):
    """A field the admin builder grows later must not reach the agent by default.

    Filtering out ``gcp_project_id`` and forwarding everything else makes every
    future upstream key an implicit grant across a trust boundary the upstream
    builder does not answer to. ``some_future_admin_field`` stands in for that
    key: it is dropped because it was never named, and the drop is *counted* so
    a reader can tell a narrow answer from a complete one.
    """
    import app.admin as admin_module

    async def _fake(session):
        return {
            "current_sha": "abc123",
            "status": "STABLE",
            "gcp_project_id": "blueprint-prod-123456",
            "some_future_admin_field": "operator-only detail",
            "health_recovery_history": [{"n": i, "note": "H" * 3000} for i in range(80)],
        }

    monkeypatch.setattr(admin_module, "build_deploy_state_payload", _fake)

    project, calling_task = await _make_project_and_task(db_session, name="Deploy")
    res = await client.get(
        f"/api/agent/projects/{project.id}/deploy-state", headers=_auth(calling_task)
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["current_sha"] == "abc123"
    assert data["status"] == "STABLE"
    assert "gcp_project_id" not in data
    assert "some_future_admin_field" not in data
    assert data["bounds"]["projection"] == "allowlist"
    assert data["bounds"]["dropped_upstream_fields"] == 2
    assert "singleton" in data["scope"]
    assert _body_bytes(data) <= AGENT_READ_MAX_RESPONSE_BYTES


def test_deploy_state_allowlist_excludes_infrastructure_identity():
    """Pinned by name: the one field the admin builder emits that must not ship."""
    assert "gcp_project_id" not in _DEPLOY_STATE_FIELDS


@pytest.mark.asyncio
async def test_deploy_state_runs_the_real_builder(client: AsyncClient, db_session):
    project, calling_task = await _make_project_and_task(db_session, name="DeployReal")
    res = await client.get(
        f"/api/agent/projects/{project.id}/deploy-state", headers=_auth(calling_task)
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert "gcp_project_id" not in data
    assert "scope" in data
    # Everything transmitted was named, not merely "not denied".
    assert set(data) - {"scope", "bounds"} <= _DEPLOY_STATE_FIELDS
    assert _body_bytes(data) <= AGENT_READ_MAX_RESPONSE_BYTES


# ---------------------------------------------------------------------------
# Trust boundary
# ---------------------------------------------------------------------------


VISIBILITY_PATHS = (
    "attention",
    "alerts",
    "autonomy-debug",
    "deploy-state",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", VISIBILITY_PATHS)
async def test_visibility_surfaces_404_across_projects(
    client: AsyncClient, db_session, suffix: str
):
    """A project the calling task does not belong to is indistinguishable from absent."""
    _project, calling_task = await _make_project_and_task(db_session, name=f"X{suffix}")
    other = Project(name=f"Other {suffix}", user_id="not-the-caller")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    res = await client.get(
        f"/api/agent/projects/{other.id}/{suffix}", headers=_auth(calling_task)
    )
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", VISIBILITY_PATHS)
async def test_visibility_surfaces_require_an_agent_token(
    client: AsyncClient, db_session, suffix: str
):
    project, _calling_task = await _make_project_and_task(
        db_session, name=f"NoAuth {suffix}"
    )

    res = await client.get(
        f"/api/agent/projects/{project.id}/{suffix}",
        headers={"x-agent-token": "not-a-real-token"},
    )
    assert res.status_code == 401, res.text

    # A missing header is rejected before the handler runs (FastAPI validation).
    res = await client.get(f"/api/agent/projects/{project.id}/{suffix}")
    assert res.status_code in (401, 403, 422), res.text


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", VISIBILITY_PATHS)
async def test_visibility_surfaces_404_for_an_unknown_project(
    client: AsyncClient, db_session, suffix: str
):
    _project, calling_task = await _make_project_and_task(
        db_session, name=f"Unknown {suffix}"
    )
    res = await client.get(
        f"/api/agent/projects/{uuid.uuid4()}/{suffix}", headers=_auth(calling_task)
    )
    assert res.status_code == 404, res.text
