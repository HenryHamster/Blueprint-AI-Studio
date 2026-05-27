"""Cross-instance WebSocket control-event fanout via PostgreSQL LISTEN/NOTIFY.

PROBLEM this solves
-------------------
The operator admin UI relies on a WebSocket event stream for live updates
(Kanban/task board, artifact/PR notifications, deploy-state changes, alerts,
new operator messages, desktop notifications). Those events are delivered
through the in-process ``ConnectionManager`` in ``app.broadcast``: when a
backend instance pushes a project event, only the WebSocket clients connected
to THAT SAME instance receive it. The live service runs on Cloud Run with up
to 3 instances behind a load balancer, so an operator's browser WebSocket and
the event source frequently land on different instances and the operator
silently misses realtime updates.

APPROACH
--------
Mirror the proven cross-instance pattern already in ``app.jwt_revocation``:
a dedicated raw asyncpg connection that LISTENs on a Postgres channel, a
transactional ``pg_notify`` publish, reconnect-with-bounded-backoff, and a
no-op guard on non-Postgres dialects. ``broadcast_to_project`` additionally
publishes the event to the channel so every instance's listener re-delivers it
to ITS OWN locally-connected clients.

LOOP-SAFETY (structural)
------------------------
An instance must NOT re-publish an event it received from the channel, or
events would rebroadcast infinitely. This is enforced structurally:
  - 'originally broadcast here'  -> ``broadcast_to_project`` = local deliver
    (``manager.broadcast``) + ``publish_project_event``.
  - 'received from a peer'       -> this module's NOTIFY handler = local
    deliver ONLY (``manager.broadcast`` directly), never
    ``broadcast_to_project``.
The NOTIFY handler therefore never calls the publish path, so a received event
is delivered locally exactly once and is never re-emitted onto the channel.

PAYLOAD CONSTRAINTS
-------------------
Postgres NOTIFY has an ~8KB payload limit and is for DISCRETE, low-volume
control events only. The high-volume per-line ``agent_log`` event is excluded
via ``_SKIP_EVENT_TYPES``, and oversized payloads are skipped (still delivered
locally) so a large payload never raises. The frontend re-fetches task state
on ``task_updated``, so a compact signal payload is sufficient.
"""

import asyncio
import json
import logging
import os
import uuid

try:
    import asyncpg as _asyncpg
except ImportError:  # pragma: no cover — asyncpg absent in standalone test env
    _asyncpg = None  # type: ignore[assignment]

from sqlalchemy import text

logger = logging.getLogger(__name__)

_NOTIFY_CHANNEL = "project_event"
# True when the process is connected to Postgres; controls no-op guards.
_IS_POSTGRES = "postgresql" in (os.getenv("DATABASE_URL") or "")

# Per-process origin id, regenerated each process start. Stamped into every
# published payload so the listener can recognise and DROP notifications this
# same process emitted. Postgres delivers a NOTIFY to EVERY listening session
# including the publisher's own event_fanout listener; without this guard the
# originating instance would deliver each control event twice (once via
# broadcast_to_project's direct manager.broadcast, once via its listener's
# _on_notify), producing duplicate tracked-task timeline/log entries in the UI.
_ORIGIN_ID = str(uuid.uuid4())

# High-volume / per-line events that must NEVER cross the NOTIFY channel.
# agent_log streams one event per log line and would both blow past the ~8KB
# NOTIFY payload ceiling and overwhelm the channel; it is delivered only to
# clients on the originating instance (operators watch a task's live log on the
# instance their log WebSocket landed on). Cross-instance live-log delivery is a
# separate, dedicated concern (see the sibling agent_log fanout task).
_SKIP_EVENT_TYPES = frozenset({"agent_log"})

# Conservative ceiling below the Postgres ~8000-byte NOTIFY limit, leaving
# headroom for channel framing. Payloads at or above this are skipped for the
# publish (the originating instance still delivers them locally).
_MAX_NOTIFY_PAYLOAD_BYTES = 7000

# --- Cross-instance live agent_log relay (batched) ---------------------------
#
# TRANSPORT CHOICE & TRADEOFF
# ---------------------------
# agent_log is high-volume (one event per log line) and is therefore EXCLUDED
# from the per-event publish path (_SKIP_EVENT_TYPES): a per-line pg_notify
# would both blow past the ~8KB NOTIFY ceiling and flood the channel. To still
# deliver live logs cross-instance, lines are BUFFERED per (project, task) on
# the instance the agent's stream landed on and flushed on a short interval as a
# SINGLE batched NOTIFY ("agent_log_batch") carrying many lines. Each instance's
# listener expands a received batch back into individual "agent_log" deliveries
# to ITS local clients, so the frontend log-event shape is unchanged.
#
# Why batching over the alternatives:
#   - vs. naive per-line pg_notify: bounds the NOTIFY rate (one per flush per
#     active task instead of one per line) and the payload size.
#   - vs. a separate streaming transport (e.g. Redis pub/sub): adds no new infra
#     dependency — it reuses the Postgres LISTEN/NOTIFY channel the control-event
#     fanout already owns.
# The cost is a small added latency (up to one flush interval) and best-effort
# line loss under extreme volume (the per-task buffer is bounded). Live logs are
# ephemeral and not persisted, so dropping under pressure is acceptable.
_AGENT_LOG_BATCH_EVENT = "agent_log_batch"
# Seconds between buffer drains. Short enough to feel live, long enough to
# coalesce a burst of lines into one NOTIFY.
_AGENT_LOG_FLUSH_INTERVAL = 0.5
# Per-task buffer cap — bounds memory if a task logs faster than we can flush.
# Oldest lines are dropped first (the live tail is what the operator watches).
_MAX_BUFFERED_LINES_PER_TASK = 2000
# A single line larger than this is truncated before relay so it always fits in
# its own batch under _MAX_NOTIFY_PAYLOAD_BYTES (the originating instance still
# delivered the full line locally; only the cross-instance copy is truncated).
_MAX_RELAY_LINE_BYTES = 6000

# Per-instance buffer of agent_log lines awaiting cross-instance relay, keyed by
# (project_id, task_id). All access is on the single asyncio event loop and no
# mutation awaits mid-operation, so no lock is needed: enqueue appends, the relay
# service snapshots-and-clears atomically. Populated by enqueue_agent_log (from
# the agent WS handler) and drained by agent_log_relay_service.
_agent_log_buffer: dict[tuple[str, str], list[str]] = {}


def _get_raw_dsn() -> str | None:
    """Return asyncpg-compatible DSN derived from DATABASE_URL.

    Strips the SQLAlchemy driver tag (e.g., '+asyncpg') so asyncpg can parse it
    directly. Returns None if DATABASE_URL is absent or does not refer to
    Postgres. Mirrors ``jwt_revocation._get_raw_dsn``.
    """
    url = os.getenv("DATABASE_URL") or ""
    if "postgresql" not in url:
        return None
    if "+" in url.split("://")[0]:
        scheme, rest = url.split("://", 1)
        base_scheme = scheme.split("+")[0]
        url = f"{base_scheme}://{rest}"
    return url


def _encode_payload(project_id: str, event_type: str, data: dict) -> str | None:
    """Serialize a control event for the NOTIFY channel.

    Returns the JSON string, or None when the event must be skipped (in the
    skip-list, not serializable, or larger than the NOTIFY payload ceiling).
    Never raises — a skip degrades to local-only delivery, not a crash.
    """
    if event_type in _SKIP_EVENT_TYPES:
        return None
    try:
        encoded = json.dumps(
            {
                "project_id": project_id,
                "event": event_type,
                "data": data,
                "origin": _ORIGIN_ID,
            }
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "[EVENT_FANOUT] Payload not JSON-serializable (project=%s, event=%s): %s",
            project_id,
            event_type,
            exc,
        )
        return None
    if len(encoded.encode("utf-8")) >= _MAX_NOTIFY_PAYLOAD_BYTES:
        logger.warning(
            "[EVENT_FANOUT] Payload too large for NOTIFY (project=%s, event=%s, "
            "bytes=%d); skipping cross-instance publish (delivered locally only)",
            project_id,
            event_type,
            len(encoded.encode("utf-8")),
        )
        return None
    return encoded


def _decode_payload(payload: str) -> tuple[str, str, dict, str] | None:
    """Decode a NOTIFY payload back into (project_id, event_type, data, origin).

    ``origin`` is the per-process id of the publisher (see ``_ORIGIN_ID``); the
    listener compares it against its own id to drop self-originated events.
    Returns None for malformed/incomplete payloads so the listener can drop
    them without raising. The inverse of ``_encode_payload``.
    """
    try:
        event = json.loads(payload)
        return event["project_id"], event["event"], event["data"], event["origin"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("[EVENT_FANOUT] Dropping malformed NOTIFY payload: %s", exc)
        return None


async def publish_project_event(project_id: str, event_type: str, data: dict) -> None:
    """Publish a control event onto the cross-instance NOTIFY channel.

    Best-effort: no-ops on non-Postgres dialects, skips high-volume/oversized
    payloads, and swallows every exception so the publish can never crash or
    raise out of the HTTP handler / reaper / agent path that triggered the
    broadcast. The transactional ``pg_notify`` runs on its own short-lived
    session (independent of any caller transaction); the commit is required for
    Postgres to actually deliver the NOTIFY.
    """
    if not _IS_POSTGRES:
        return
    encoded = _encode_payload(project_id, event_type, data)
    if encoded is None:
        return
    try:
        await _publish_encoded(encoded)
    except Exception as exc:
        logger.warning(
            "[EVENT_FANOUT] publish_project_event failed (project=%s, event=%s): %s",
            project_id,
            event_type,
            exc,
        )


async def _publish_encoded(encoded: str) -> None:
    """Emit one already-encoded payload onto the NOTIFY channel and commit.

    The transactional ``pg_notify`` runs on its own short-lived session
    (independent of any caller transaction); the commit is required for Postgres
    to actually deliver the NOTIFY. Raises on failure — callers swallow.
    """
    # Late import avoids a circular dependency: broadcast -> event_fanout
    # (publish) and event_fanout (listener) -> broadcast (manager).
    from app.database import AsyncSessionLocal  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": _NOTIFY_CHANNEL, "payload": encoded},
        )
        await session.commit()


def enqueue_agent_log(project_id: str, task_id: str, line: str) -> None:
    """Buffer one sanitized agent_log line for batched cross-instance relay.

    No-op on non-Postgres / single-instance backends, where the agent WS handler
    already delivers the line to local clients via ``broadcast_to_project`` and
    there are no sibling instances to relay to — enqueuing there would risk
    double delivery and waste memory. Best-effort: never raises into the agent's
    streaming WS handler. The per-task buffer is bounded; oldest lines drop first.
    """
    if not _IS_POSTGRES or not line:
        return
    try:
        if len(line.encode("utf-8")) > _MAX_RELAY_LINE_BYTES:
            line = (
                line.encode("utf-8")[:_MAX_RELAY_LINE_BYTES].decode("utf-8", "ignore")
                + " [relay-truncated]"
            )
        buf = _agent_log_buffer.setdefault((project_id, task_id), [])
        buf.append(line)
        if len(buf) > _MAX_BUFFERED_LINES_PER_TASK:
            del buf[: len(buf) - _MAX_BUFFERED_LINES_PER_TASK]
    except Exception as exc:
        logger.warning(
            "[EVENT_FANOUT] enqueue_agent_log failed (project=%s, task=%s): %s",
            project_id,
            task_id,
            exc,
        )


def _encode_agent_log_batch(project_id: str, task_id: str, lines: list[str]) -> str:
    """Serialize a batch of agent_log lines for the NOTIFY channel.

    Uses compact separators (no spaces) so the wire-format size matches the
    +1-per-comma overhead estimate in ``_pack_log_batches``. Default json.dumps
    would emit ``", "`` between elements (2 bytes), causing the packer to
    underestimate batch size and risk exceeding the ~8KB Postgres NOTIFY limit.
    """
    return json.dumps(
        {
            "project_id": project_id,
            "event": _AGENT_LOG_BATCH_EVENT,
            "data": {"task_id": task_id, "lines": lines},
            "origin": _ORIGIN_ID,
        },
        separators=(",", ":"),
    )


def _pack_log_batches(project_id: str, task_id: str, lines: list[str]) -> list[str]:
    """Greedily pack lines into encoded batches under the NOTIFY ceiling.

    Each line is already truncated to ``_MAX_RELAY_LINE_BYTES`` at enqueue time,
    so a single line always fits in its own batch. Returns a list of encoded JSON
    payloads ready to hand to ``_publish_encoded``.
    """
    # Fixed framing overhead: the envelope with an empty lines array.
    base = len(_encode_agent_log_batch(project_id, task_id, []).encode("utf-8"))
    encoded_batches: list[str] = []
    current: list[str] = []
    size = base
    for line in lines:
        # json.dumps on the single string yields its exact serialized length
        # (quotes + escapes); +1 for the separating comma.
        line_len = len(json.dumps(line).encode("utf-8")) + 1
        if current and size + line_len >= _MAX_NOTIFY_PAYLOAD_BYTES:
            encoded_batches.append(
                _encode_agent_log_batch(project_id, task_id, current)
            )
            current = []
            size = base
        current.append(line)
        size += line_len
    if current:
        encoded_batches.append(_encode_agent_log_batch(project_id, task_id, current))
    return encoded_batches


async def agent_log_relay_service() -> None:
    """Background service: flush buffered agent_log lines as batched NOTIFYs.

    Runs on EVERY instance (per-instance, like the listener): it drains the
    buffer this instance accumulated from agents whose streams landed here and
    publishes the lines for sibling instances to re-deliver to their own clients.
    No-op sleep loop on non-Postgres. Best-effort — a publish failure is logged
    and those lines are dropped (live logs are ephemeral); the loop continues.
    """
    if not _IS_POSTGRES:
        # Sleep until cancelled so the monitor doesn't tight-loop restarts.
        try:
            await asyncio.sleep(365 * 24 * 3600)
        except asyncio.CancelledError:
            pass
        return

    while True:
        try:
            await asyncio.sleep(_AGENT_LOG_FLUSH_INTERVAL)
            if not _agent_log_buffer:
                continue
            # Atomic drain: snapshot then clear with no await in between, so a
            # concurrent enqueue (same event loop) cannot lose a line. copy() is
            # shallow — the snapshot holds the existing list objects; clear()
            # removes the keys and subsequent enqueues create fresh lists.
            snapshot = _agent_log_buffer.copy()
            _agent_log_buffer.clear()
            for (project_id, task_id), lines in snapshot.items():
                if not lines:
                    continue
                for encoded in _pack_log_batches(project_id, task_id, lines):
                    try:
                        await _publish_encoded(encoded)
                    except Exception as exc:
                        logger.warning(
                            "[EVENT_FANOUT] agent_log batch publish failed "
                            "(project=%s, task=%s): %s",
                            project_id,
                            task_id,
                            exc,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[EVENT_FANOUT] agent_log relay loop error: %s", exc)


async def event_fanout_service() -> None:
    """Background service: LISTEN for cross-instance project control events.

    Opens a dedicated raw asyncpg connection (never borrowed from the
    SQLAlchemy pool, which recycles connections and enforces
    idle_in_transaction_session_timeout). On each NOTIFY, re-delivers the event
    to THIS instance's locally-connected WebSocket clients via the in-memory
    manager's local-delivery path (``manager.broadcast``) — NOT
    ``broadcast_to_project``, which would re-publish and loop forever.

    Reconnects with bounded exponential backoff (1s -> 60s) using asyncpg's
    add_termination_listener + asyncio.Event. Mirrors
    ``jwt_revocation.jwt_revocation_service``.
    """
    # Late import avoids a circular dependency (broadcast imports this module's
    # publish helper); also lets us read the live module-level manager.
    from app.broadcast import manager  # noqa: PLC0415

    dsn = _get_raw_dsn()
    if not dsn or _asyncpg is None:
        logger.info(
            "[EVENT_FANOUT] Non-Postgres dialect or asyncpg absent; listener inactive"
        )
        # Sleep until cancelled so the monitor doesn't tight-loop restarts.
        try:
            await asyncio.sleep(365 * 24 * 3600)
        except asyncio.CancelledError:
            pass
        return

    attempt = 0
    while True:
        try:
            conn = await _asyncpg.connect(dsn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            wait = min(1 * 2**attempt, 60)
            logger.warning(
                "[EVENT_FANOUT] Connect failed (attempt %d, retry in %ds): %s",
                attempt,
                wait,
                exc,
            )
            attempt += 1
            await asyncio.sleep(wait)
            continue

        attempt = 0  # reset backoff after successful connect
        # A fresh Event starts cleared. Do not clear it again after registering
        # the termination listener or an immediate callback can be lost before
        # we enter the wait below.
        terminated_event: asyncio.Event = asyncio.Event()
        # Hold strong references to in-flight local-delivery tasks. asyncio keeps
        # only weak references to tasks created with create_task, so without this
        # a scheduled delivery could be garbage-collected before it completes,
        # silently dropping a cross-instance event (ruff RUF006).
        pending_deliveries: set[asyncio.Task] = set()

        def _on_terminate(conn):  # noqa: ANN001 — asyncpg callback
            terminated_event.set()

        def _on_notify(conn, pid, channel, payload):  # noqa: ANN001
            # asyncpg invokes this synchronously on the event loop. Local
            # delivery (manager.broadcast) is async, so schedule it as a task.
            # Crucially this calls manager.broadcast (local-only) and NEVER
            # publish_project_event / broadcast_to_project — that is the
            # structural loop-break for peer-received events.
            if not payload:
                return
            decoded = _decode_payload(payload)
            if decoded is None:
                return
            project_id, event_type, data, origin = decoded
            # Self-origin guard: Postgres delivers a NOTIFY to every listening
            # session including this publisher's own listener. The originating
            # instance already delivered locally inside broadcast_to_project, so
            # re-delivering here would duplicate the event. Drop it.
            if origin == _ORIGIN_ID:
                return
            if event_type == _AGENT_LOG_BATCH_EVENT:
                # Expand a relayed batch back into individual agent_log
                # deliveries so the frontend's per-line event shape is unchanged.
                task_id = data.get("task_id")
                for log_line in data.get("lines") or []:
                    t = asyncio.create_task(
                        manager.broadcast(
                            project_id,
                            "agent_log",
                            {"task_id": task_id, "log": log_line},
                        )
                    )
                    pending_deliveries.add(t)
                    t.add_done_callback(pending_deliveries.discard)
                return
            task = asyncio.create_task(manager.broadcast(project_id, event_type, data))
            pending_deliveries.add(task)
            task.add_done_callback(pending_deliveries.discard)

        try:
            conn.add_termination_listener(_on_terminate)
            await conn.add_listener(_NOTIFY_CHANNEL, _on_notify)
            logger.info(
                "[EVENT_FANOUT] LISTEN %s — cross-instance event fanout active",
                _NOTIFY_CHANNEL,
            )

            # Block until the server terminates the connection.
            await terminated_event.wait()
            logger.warning("[EVENT_FANOUT] Connection terminated; will reconnect")
        except asyncio.CancelledError:
            try:
                await conn.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.warning("[EVENT_FANOUT] Listener error: %s", exc)
        finally:
            try:
                await conn.close()
            except Exception:
                pass

        wait = min(1 * 2**attempt, 60)
        attempt += 1
        await asyncio.sleep(wait)
