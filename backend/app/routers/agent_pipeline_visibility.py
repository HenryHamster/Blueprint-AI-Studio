"""Agent-facing read surfaces for pipeline state the Strategic Resident was blind to.

Everything the resident could previously fetch described work it had authored
itself. The signals that actually explain a stalled pipeline — the operator
attention census, the alert feed, why autonomy is not spawning, and what SHA is
serving — existed only behind admin/browser auth, so the resident could reason
about its own tasks and nothing else.

These endpoints wrap the *existing* admin computations rather than reimplement
them; the only thing added here is authority and bounding:

* **Authority** is server-supplied *and real*. The caller presents a
  task-scoped agent JWT; :func:`_resolve_agent_project` proves the calling task
  belongs to the project in the path, and the project's own owner id is then
  used for the underlying member-scoped lookups. The agent never names the
  principal it acts as, and a cross-project path returns 404 rather than 403.

  The corollary is a hard rule for this module: **a fabricated principal is
  never an acceptable way to reach an admin computation.** An earlier revision
  of this file called ``get_recent_orchestrator_errors`` — otherwise reachable
  only behind ``Depends(require_admin)`` — with a synthetic
  ``{"user_id": "AGENT_READ"}``. The path scoping was decorative there, because
  that payload is a process-wide Cloud Logging read which is not project-scoped
  and cannot be made project-scoped: the filter selects a Cloud Run *service*,
  and the log bodies are raw tracebacks carrying no project attribution. Any
  project's agent token could therefore read the entire control plane's
  exceptions. The surface was removed rather than papered over; the
  project-scoped view of the same failures is ``/alerts``. Do not reintroduce
  it, or the pattern, for some other admin computation.
* **Bounding** is mandatory, not stylistic. Every response is clamped to
  ``AGENT_READ_MAX_RESPONSE_BYTES`` (see :mod:`app.agent_read_budget`) because
  an oversized read degrades the strategic envelope into a coreless stub and
  livelocks the reclaim loop (PR #17424). Counts survive clamping; detail rows
  are what gets shed; an aggregate map must be folded with ``bound_counts``
  before it arrives, because the clamp cannot shrink one; and ``bounds``
  reports both what was shed and whether the ceiling was actually met.

Read-only by construction: no route here mutates state, and none accepts a body.
"""

# NOTE: deliberately no ``from __future__ import annotations``. FastAPI resolves
# path/query parameter annotations through pydantic at request time, and
# stringized annotations leave ``uuid.UUID`` as an unresolvable forward ref
# ("TypeAdapter ... is not fully defined") on every request to this router.
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.agent_read_budget import bound_counts, clamp_read_response, clip_text
from app.database import get_session
from app.message_audience import operator_visible_alert_filter
from app.models import Alert
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter
from app.time_utils import utc_iso

router = APIRouter(prefix="/api/agent", tags=["agent"])

# ── Per-surface item caps ───────────────────────────────────────────────────
# Applied before the whole-response byte clamp so the shape of a bounded answer
# is deterministic rather than "whatever happened to fit".
ATTENTION_MAX_ITEMS = 8
ALERTS_WINDOW_HOURS_DEFAULT = 24
ALERTS_WINDOW_HOURS_MAX = 168
ALERTS_RECENT_DEFAULT = 10
ALERTS_RECENT_MAX = 25
ALERTS_EVENT_TYPE_TOP_N = 15
AUTONOMY_DIRECTIONS_DEFAULT = 12
AUTONOMY_DIRECTIONS_MAX = 25
AUTONOMY_CYCLE_HISTORY_MAX = 5
# Depth ceiling for the generic compactor: admin payloads nest a few levels,
# and anything deeper is detail the resident should fetch elsewhere.
_COMPACT_MAX_DEPTH = 6

# ── /deploy-state projection allowlist ──────────────────────────────────────
# ``build_deploy_state_payload`` is the admin console's payload and grows on the
# admin console's schedule. A denylist ("everything except gcp_project_id")
# would forward each future field to a project-scoped agent token sight unseen,
# so the exposed set is named instead. Adding a key here is a deliberate,
# reviewable act; ``bounds.dropped_upstream_fields`` reports the count of
# anything the builder returned that is not listed.
_DEPLOY_STATE_FIELDS = frozenset(
    {
        # What is serving, and what it would fall back to.
        "current_revision",
        "current_sha",
        "deployed_at",
        "status",
        "last_known_good_revision",
        "last_known_good_sha",
        # Why it is not serving what you expected.
        "rolled_back_from_sha",
        "rollback_reason",
        "rollback_count",
        "pause_reason",
        "paused_at",
        "reason",
        "pre_pause_max_vms",
        # Whether the current deploy is still being judged.
        "monitoring_window",
        "failure_rate",
        "task_results_count",
        "external_health",
        # Capacity: "nothing is dispatching" is often a ramp or a zone, not a
        # scheduling bug, and the resident cannot tell those apart otherwise.
        "max_concurrent_vms",
        "current_vms",
        "target_vms",
        "ramp_in_progress",
        "ramp_step_size",
        "zone_health",
        # Health/flap accounting behind an automatic pause.
        "health_recovery_streak",
        "health_recovery_streak_required",
        "health_recovery_flap_escalated",
        "health_recovery_history",
        "health_recovery_history_size",
        "health_failure_streak",
        "health_failure_streak_threshold",
        "health_check_interval_seconds",
        "max_auto_pause_duration_seconds",
        # The builder's own degraded answer when the singleton row is missing.
        "error",
    }
)


def _compact(value: Any, *, max_items: int = 8, depth: int = 0) -> Any:
    """Recursively clip free text and truncate lists inside an admin payload.

    Admin payloads are shaped for a browser that can scroll. Rather than pin a
    field list that silently goes stale when the admin side grows a key, this
    clips every string to the shared text ceiling and caps every list, so a new
    upstream field can add detail but cannot add unbounded bytes.
    """

    if depth > _COMPACT_MAX_DEPTH:
        return None
    if isinstance(value, str):
        return clip_text(value)
    if isinstance(value, dict):
        return {
            key: _compact(item, max_items=max_items, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _compact(item, max_items=max_items, depth=depth + 1)
            for item in list(value)[:max_items]
        ]
    return value


def project_operator_attention(
    attention: dict[str, Any], *, max_items: int = ATTENTION_MAX_ITEMS
) -> dict[str, Any]:
    """Bound an :func:`app.operator_attention.build_operator_attention` result.

    Pure function on the already-built block so both this router and the
    resident spawn payload share one projection (and one set of bounds).

    **The byte clamp is part of the projection, deliberately.** This block is
    emitted twice: over HTTP by ``/attention``, and inside the
    ``strategic_review`` spawn payload built by
    ``strategic_spawning._spawn_strategic_review``. The spawn copy is the one
    that goes straight into the envelope the cap exists to protect — and it was
    the copy with no cap, because the clamp lived at the HTTP call site where
    the spawn path could not forget it but did. Applying
    :func:`clamp_read_response` here instead means there is no unbounded
    variant of this projection for a third caller to pick up. Callers must not
    re-clamp; doing so is a harmless no-op but implies a bound that is already
    guaranteed.

    Read cursors are removed: ``last_seen_at`` and the ``unread`` counters are
    relative to a human operator's own read position, which an agent can
    neither hold nor advance. Reporting them to the resident would invite it to
    treat "unread" as a fact about the world instead of a fact about Henry's
    browser. This mirrors ``agent_access._remove_actor_relative_read_state``.
    """

    compacted = _compact(dict(attention), max_items=max_items)
    if not isinstance(compacted, dict):  # pragma: no cover - defensive
        return {}
    compacted.pop("last_seen_at", None)
    counts = compacted.get("counts")
    if isinstance(counts, dict):
        counts.pop("unread", None)
    items = compacted.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                item.pop("unread", None)
        compacted["items"] = items[:max_items]
    compacted.setdefault("bounds", {})["max_items"] = max_items
    return clamp_read_response(compacted, trim_order=("items",))


@router.get("/projects/{project_id}/attention")
@limiter.limit("30/minute")
async def agent_operator_attention(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """What the workbench would tell the operator deserves a look right now.

    This is the same census that renders the operator's attention rail —
    unanswered blockers and questions, stale tasks, stranded approved PRs,
    error alerts — computed by the canonical
    :func:`app.operator_attention.build_operator_attention`. It was previously
    reachable only from the browser session and the ``observer:read`` service
    scope, so the resident had no way to see the backlog it is supposed to be
    working down.
    """
    project = await _resolve_agent_project(project_id, token, session)

    from app.operator_attention import build_operator_attention

    attention = await build_operator_attention(session, project, project.user_id)
    # No clamp here: project_operator_attention applies it, so the spawn-payload
    # copy of this same projection cannot be the uncapped one.
    return {"status": "success", "data": project_operator_attention(attention)}


@router.get("/projects/{project_id}/alerts")
@limiter.limit("30/minute")
async def agent_alerts(
    request: Request,
    project_id: uuid.UUID,
    window_hours: int = Query(
        default=ALERTS_WINDOW_HOURS_DEFAULT, ge=1, le=ALERTS_WINDOW_HOURS_MAX
    ),
    recent: int = Query(default=ALERTS_RECENT_DEFAULT, ge=1, le=ALERTS_RECENT_MAX),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """Shape of the alert feed for this project, not a page of the alert feed.

    The operator alert feed is dominated by point-in-time notices that are
    minted durable, so a default page of it reads as a wall of near-identical
    rows and characterises nothing. This surface therefore leads with exact
    aggregates computed in SQL over the whole window — total, by severity, and
    the top ``ALERTS_EVENT_TYPE_TOP_N`` event types with the remainder rolled
    into ``other_event_types`` — and only then returns up to ``recent``
    newest rows as examples.

    Scope: this project only, undismissed, operator-visible event types, over
    the last ``window_hours`` (default 24, max 168) by ``created_at``. Anything
    dismissed, older than the window, or operator-invisible is out of frame by
    construction — the counts are exact for that frame and for no other.
    """
    await _resolve_agent_project(project_id, token, session)

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    base_filters = (
        Alert.project_id == project_id,
        operator_visible_alert_filter(Alert.event_type),
        Alert.dismissed_at.is_(None),
        Alert.created_at >= window_start,
    )

    total = int(
        (
            await session.execute(select(func.count()).select_from(Alert).where(*base_filters))
        ).scalar_one()
        or 0
    )

    by_severity: dict[str, int] = {}
    for severity, count in (
        await session.execute(
            select(Alert.severity, func.count()).where(*base_filters).group_by(Alert.severity)
        )
    ).all():
        by_severity[str(severity)] = int(count)

    event_rows = (
        await session.execute(
            select(Alert.event_type, func.count())
            .where(*base_filters)
            .group_by(Alert.event_type)
            .order_by(func.count().desc())
            .limit(ALERTS_EVENT_TYPE_TOP_N)
        )
    ).all()
    by_event_type = {str(event_type): int(count) for event_type, count in event_rows}
    other_event_types = max(0, total - sum(by_event_type.values()))

    recent_rows = (
        (
            await session.execute(
                select(Alert)
                .where(*base_filters)
                .order_by(Alert.created_at.desc())
                .limit(recent)
            )
        )
        .scalars()
        .all()
    )
    recent_items = [
        {
            "id": str(alert.id),
            "severity": alert.severity,
            "domain": alert.domain,
            "event_type": alert.event_type,
            "source": alert.source,
            "title": clip_text(alert.title),
            "message": clip_text(alert.message),
            "occurrence_count": alert.occurrence_count,
            "task_id": str(alert.task_id) if alert.task_id else None,
            "created_at": utc_iso(alert.created_at),
            "last_seen_at": utc_iso(alert.last_seen_at),
        }
        for alert in recent_rows
    ]

    data: dict[str, Any] = {
        "generated_at": utc_iso(now),
        "window_hours": window_hours,
        "total": total,
        "by_severity": by_severity,
        "by_event_type": by_event_type,
        "other_event_types": other_event_types,
        "recent": recent_items,
        "bounds": {
            "scope": "undismissed operator-visible alerts for this project",
            "counts_are_exact_for_window": True,
            "event_type_top_n": ALERTS_EVENT_TYPE_TOP_N,
            "recent_requested": recent,
        },
    }
    data = clamp_read_response(
        data,
        trim_order=("recent",),
        returned_counts={"recent_returned": "recent"},
    )
    return {"status": "success", "data": data}


# ── Removed: GET /projects/{project_id}/orchestrator-errors ─────────────────
# It reached ``admin_orchestrator_errors.get_recent_orchestrator_errors`` — a
# ``Depends(require_admin)`` handler — behind a fabricated
# ``{"user_id": "AGENT_READ"}`` principal, and the payload is a Cloud Logging
# read filtered by Cloud Run *service*, so it was the whole control plane's
# exception stream handed to any single project's agent token. There is no
# project-scoped form of that query: the filter has no project dimension and
# the log bodies are raw tracebacks with no project attribution. Removed rather
# than left cross-project. The project-scoped view of the same failures is
# ``/alerts`` above. See this module's docstring before adding anything shaped
# like it.


@router.get("/projects/{project_id}/autonomy-debug")
@limiter.limit("20/minute")
async def agent_autonomy_debug(
    request: Request,
    project_id: uuid.UUID,
    directions: int = Query(
        default=AUTONOMY_DIRECTIONS_DEFAULT, ge=1, le=AUTONOMY_DIRECTIONS_MAX
    ),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """Why autonomy is or is not spawning — the stranded-direction diagnostic.

    A direction can sit ineligible for days while the project looks healthy from
    every other surface: the blocking guard, the refractory window, the
    discovery-admission verdict, and the per-direction ``block_reason_code`` are
    the only things that say why. That answer already existed behind
    ``GET /api/admin/projects/{id}/autonomy-debug`` and was reachable only from
    a developer CLI; this exposes it to the resident that is supposed to act on
    it.

    Bounded projection of the admin payload: the scalar verdicts pass through
    compacted, directions are reduced to counts plus the blocked ones first
    (ineligible before eligible, so a saturated list still shows the strandings)
    capped at ``directions``, and cycle history is capped at
    ``AUTONOMY_CYCLE_HISTORY_MAX`` most-recent entries.
    """
    project = await _resolve_agent_project(project_id, token, session)

    from app.routers.admin_debug import get_autonomy_debug

    raw = await get_autonomy_debug(
        project_id=project_id,
        session=session,
        current_user={"user_id": project.user_id},
    )
    if not isinstance(raw, dict):  # pragma: no cover - defensive
        raw = {}

    raw_directions = [d for d in (raw.get("directions") or []) if isinstance(d, dict)]
    eligible_count = sum(1 for d in raw_directions if d.get("is_eligible"))
    blocked_by_code: dict[str, int] = {}
    for direction in raw_directions:
        if direction.get("is_eligible"):
            continue
        code = str(direction.get("block_reason_code") or "unknown")
        blocked_by_code[code] = blocked_by_code.get(code, 0) + 1

    # Ineligible first: a project with many directions is exactly the case where
    # the stranded one must not be the row that falls off the end.
    ordered = sorted(raw_directions, key=lambda d: bool(d.get("is_eligible")))
    direction_items = []
    for direction in ordered[:directions]:
        admission = direction.get("discovery_admission")
        direction_items.append(
            {
                "id": direction.get("id"),
                "text": clip_text(direction.get("text")),
                "is_eligible": direction.get("is_eligible"),
                "seconds_until_eligible": direction.get("seconds_until_eligible"),
                "block_reason_code": direction.get("block_reason_code"),
                "block_reason": clip_text(direction.get("block_reason")),
                "next_action": clip_text(direction.get("next_action")),
                "can_admit_root": (
                    admission.get("can_admit_root") if isinstance(admission, dict) else None
                ),
            }
        )

    history = raw.get("strategic_cycle_history") or []
    if not isinstance(history, list):
        history = []

    data: dict[str, Any] = {
        "spawn_blocked": raw.get("spawn_blocked"),
        "automatic_strategic_cycles_enabled": raw.get(
            "automatic_strategic_cycles_enabled"
        ),
        "blocking_guards": _compact(raw.get("blocking_guards") or [], max_items=8),
        "circuit_breaker": _compact(raw.get("circuit_breaker")),
        "rate_limit": _compact(raw.get("rate_limit")),
        "global_rate_limit": _compact(raw.get("global_rate_limit")),
        "next_check_at": raw.get("next_check_at"),
        "headroom": _compact(raw.get("headroom")),
        "directions_summary": {
            "active_total": len(raw_directions),
            "eligible": eligible_count,
            # Folded for the same reason the census folds by-type: nothing
            # constrains ``block_reason_code`` to a closed vocabulary, and the
            # byte clamp below cannot shrink a count map.
            "blocked_by_code": bound_counts(blocked_by_code),
        },
        "directions": direction_items,
        "strategic_cycle_history": _compact(
            history[-AUTONOMY_CYCLE_HISTORY_MAX:], max_items=AUTONOMY_CYCLE_HISTORY_MAX
        ),
        "bounds": {
            "directions_requested": directions,
            "cycle_history_max": AUTONOMY_CYCLE_HISTORY_MAX,
        },
    }
    data = clamp_read_response(
        data,
        trim_order=("strategic_cycle_history", "directions", "blocking_guards"),
        returned_counts={"directions_returned": "directions"},
    )
    return {"status": "success", "data": data}


@router.get("/projects/{project_id}/deploy-state")
@limiter.limit("20/minute")
async def agent_deploy_state(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """What is actually serving, and whether deployment is paused or rolled back.

    A merged fix is not live until a deploy carries it, so "is this fix
    deployed?" is a question the resident asks constantly and could not answer.

    **Why a project-scoped token may read an install-wide singleton.** Every
    field below is platform operational state — revisions, SHAs, pause and
    rollback status, health streaks, VM ramp — and none of it is any project's
    or any user's data. The same payload, built by the same function, is
    already pushed to every project member on WebSocket connect
    (``routers/projects.py``, the ``deploy_state`` frame sent immediately after
    ``connected``), so project membership is the established authority boundary
    for it, and an agent token scoped to one project is strictly narrower than
    that. This is emphatically *not* the case for a process-wide read of
    another dimension entirely — see the removed orchestrator-errors note above
    for the shape that is not acceptable.

    **The projection is an allowlist, not a denylist.** Filtering out
    ``gcp_project_id`` and forwarding the rest would mean any field a future
    admin-side change adds to ``build_deploy_state_payload`` reaches the agent
    surface with no review — the upstream builder answers to the admin console,
    not to this trust boundary. ``_DEPLOY_STATE_FIELDS`` therefore names what is
    exposed; anything else is dropped until someone adds it deliberately, and
    ``bounds.dropped_upstream_fields`` counts what was dropped so the omission
    is visible rather than silent.

    Deployment state is a global singleton, not per-project; the project in the
    path scopes the *caller*, not the answer, and the payload says so.
    """
    await _resolve_agent_project(project_id, token, session)

    from app.admin import build_deploy_state_payload

    raw = await build_deploy_state_payload(session)
    if not isinstance(raw, dict):  # pragma: no cover - defensive
        raw = {}
    projected = {key: value for key, value in raw.items() if key in _DEPLOY_STATE_FIELDS}
    dropped = sorted(key for key in raw if key not in _DEPLOY_STATE_FIELDS)

    data = _compact(projected, max_items=12)
    if not isinstance(data, dict):  # pragma: no cover - defensive
        data = {}
    data["scope"] = "deployment singleton (global, not project-scoped)"
    data["bounds"] = {
        "projection": "allowlist",
        "dropped_upstream_fields": len(dropped),
    }
    data = clamp_read_response(data, trim_order=("health_recovery_history",))
    return {"status": "success", "data": data}
