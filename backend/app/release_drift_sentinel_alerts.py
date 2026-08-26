"""Alert LIFECYCLE for the full-suite drift sentinel.

The sentinel's operator incidents are CONDITIONS ("full-suite CI on the
integration branch is not verifiably green"), not point-in-time notices.  The
raw ``session.add(Alert(...))`` writes this producer grew up with were missing
all three things a condition needs, and the live feed showed the cost: 126 open
``release.ci_unresolved`` rows accumulated over twelve days across 122 distinct
HEAD shas, and not one of them described a condition that was still true.

1. POLICY STAMP.  Every row landed with the model's ``core`` domain default and
   a NULL source, even though ``ALERT_EVENT_POLICIES["release.ci_unresolved"]``
   declares ``release_lifecycle``.  Mis-attributed in every domain rollup and
   unreachable through the ``?domain=`` / ``?source=`` filters on
   ``/api/admin/alerts``.  Like ``release_stall_alerts._create_release_alert``,
   this producer STAMPS the canonical domain but deliberately does not consult
   the domain opt-in gate: suppressing a genuine release blocker is far worse
   than one extra row, so the emit decision stays fail-open.

2. PER-PROJECT KEY.  "CI is unverified / red on the integration branch" is one
   incident, not one per automain merge.  #17604 collapsed the dispatch-rejected
   key and left ``red`` and ``cancelled`` keyed per HEAD, so they kept minting.

3. A RESOLVER.  ``release.ci_unresolved`` declares
   ``AlertRetirementMode.SOURCE_CONDITION`` -- the producer owns retirement --
   and the producer retired nothing, ever.  A sentinel sweep that read green
   simply returned and left every prior incident open forever.

Retirement is condition-derived, never age-derived, and always fail-open: an
incident is dismissed only when the sentinel holds positive evidence that its
specific claim is now false, and any recurrence re-raises the same standing row
with a bumped ``occurrence_count`` rather than minting a fresh one.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alert_policy import get_alert_event_policy
from app.models import Alert, AlertSeverity
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)
_MODULE_SIZE_GOVERNANCE_CAP: int = 280

# STATICALLY RESOLVABLE ON PURPOSE.  The ``Alert(...)`` constructor below passes
# this module constant, never the equivalent ``policy.event_type`` attribute.
# ``operator_visible_alert_filter`` is fail-closed: an event_type missing from
# ``_EVENT_AUDIENCE_REGISTRY`` is silently dropped from the operator badge, so
# ``test_message_audience`` AST-scans every ``Alert(event_type=...)`` under
# ``backend/app/`` and rejects any value it cannot resolve to a registered
# literal.  An attribute access is unresolvable to that scanner and fails the
# whole-app invariant; a bare ``NAME = "literal"`` is resolved and checked.
# Keep this assignment unannotated and literal -- ``_resolve_module_constant``
# only matches that exact shape.
DRIFT_SENTINEL_EVENT_TYPE = "release.ci_unresolved"
DRIFT_SENTINEL_SOURCE = "drift_sentinel"
_KEY_PREFIX = "drift_sentinel:"

STATE_UNAVAILABLE = "unavailable"
STATE_DISPATCH_REJECTED = "dispatch_rejected"
STATE_CANCELLED = "cancelled"
STATE_RED = "red"

# These three states all make the SAME claim: "full-suite CI drift could not be
# EVALUATED for the current integration HEAD".  Any authoritative ci.yml
# conclusion -- green or red -- is direct positive evidence that the claim is
# now false, so they retire together.  A dispatch authority that is still
# latently broken re-raises its standing incident the very next time the gate
# has to dispatch, so retiring here cannot hide it.
EVALUABILITY_STATES: tuple[str, ...] = (
    STATE_UNAVAILABLE,
    STATE_DISPATCH_REJECTED,
    STATE_CANCELLED,
)
ALL_STATES: tuple[str, ...] = (*EVALUABILITY_STATES, STATE_RED)

# States that carry exactly ONE standing incident per project.  ``red``,
# ``cancelled`` and ``unavailable`` each describe a single branch-level
# condition, so raising one supersedes any other open row for that state -- this
# is what reaps rows a previous per-HEAD serving image minted.
# ``dispatch_rejected`` is excluded on purpose: its key carries the DIAGNOSIS
# (403 "denied this authority" vs 404 "workflow unavailable to this authority"),
# which are different operator fixes that must be able to stand open together.
_SINGLE_INCIDENT_STATES = frozenset({STATE_UNAVAILABLE, STATE_CANCELLED, STATE_RED})


def drift_sentinel_dedupe_key(
    state: str,
    project_id: uuid.UUID | str,
    *,
    diagnosis: str | None = None,
) -> str:
    """The canonical PER-PROJECT dedupe key for one sentinel state.

    Never per-HEAD: the integration branch moves on every merge, so a per-HEAD
    key turns one standing condition into an unbounded alert stream.
    """
    key = f"{_KEY_PREFIX}{state}:{project_id}"
    return f"{key}:{diagnosis}" if diagnosis else key


async def _open_drift_sentinel_alerts(
    session: AsyncSession,
    project_id: uuid.UUID,
    states: tuple[str, ...] | frozenset[str],
) -> list[Alert]:
    """Open sentinel-owned alerts for ``states``.

    The prefix match runs in Python rather than as a SQL ``LIKE``: the keys
    embed a project UUID and this must behave identically on SQLite and
    Postgres without escaping games.  The row set is bounded by the number of
    open alerts for one project.
    """
    prefixes = tuple(f"{_KEY_PREFIX}{state}:" for state in states)
    if not prefixes:
        return []
    rows = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.event_type == DRIFT_SENTINEL_EVENT_TYPE,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [a for a in rows if str(a.dedupe_key or "").startswith(prefixes)]


async def resolve_drift_sentinel_alerts(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    states: tuple[str, ...] | frozenset[str],
    reason: str,
    keep_key: str | None = None,
) -> int:
    """Dismiss the sentinel incidents whose condition has provably ended.

    ``reason`` is logged verbatim: an alert that vanishes without a stated cause
    is as unactionable as one that never closes.  ``keep_key`` protects the
    incident currently being raised so supersession cannot dismiss it.
    """
    resolved = 0
    now = _utcnow()
    for alert in await _open_drift_sentinel_alerts(session, project_id, states):
        if keep_key is not None and alert.dedupe_key == keep_key:
            continue
        alert.dismissed_at = now
        session.add(alert)
        resolved += 1
    if resolved:
        logger.info(
            "[DRIFT_SENTINEL] retired %d operator incident(s) for project %s: %s",
            resolved,
            project_id,
            reason,
        )
    return resolved


async def raise_drift_sentinel_alert(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    state: str,
    severity: AlertSeverity,
    title: str,
    message: str,
    diagnosis: str | None = None,
) -> tuple[Alert, bool]:
    """Create or refresh the ONE standing incident for ``state``.

    Returns ``(alert, created)``.  A recurrence refreshes title and message so
    the incident always names the CURRENT HEAD, and bumps ``occurrence_count``
    -- that counter is the "how long has this been broken" signal the deleted
    per-HEAD alert stream used to carry.  Callers own the commit.
    """
    dedupe_key = drift_sentinel_dedupe_key(state, project_id, diagnosis=diagnosis)
    # ``policy`` is consulted for the canonical DOMAIN only; the event_type is
    # the key it was looked up by, so it comes straight from the constant.
    policy = get_alert_event_policy(DRIFT_SENTINEL_EVENT_TYPE)
    now = _utcnow()
    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.dedupe_key == dedupe_key,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        alert = Alert(
            project_id=project_id,
            severity=severity,
            title=title,
            message=message,
            domain=policy.domain,
            event_type=DRIFT_SENTINEL_EVENT_TYPE,
            source=DRIFT_SENTINEL_SOURCE,
            dedupe_key=dedupe_key,
            occurrence_count=1,
            last_seen_at=now,
        )
        session.add(alert)
        created = True
    else:
        existing.severity = severity
        existing.title = title
        existing.message = message
        existing.domain = policy.domain
        existing.event_type = DRIFT_SENTINEL_EVENT_TYPE
        existing.source = DRIFT_SENTINEL_SOURCE
        existing.occurrence_count = int(existing.occurrence_count or 1) + 1
        existing.last_seen_at = now
        session.add(existing)
        alert = existing
        created = False
    if state in _SINGLE_INCIDENT_STATES:
        await resolve_drift_sentinel_alerts(
            session,
            project_id,
            states=(state,),
            reason=(
                f"superseded by the standing {state} incident ({dedupe_key})"
                " -- one condition, one row"
            ),
            keep_key=dedupe_key,
        )
    return alert, created
