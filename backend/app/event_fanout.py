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
import contextlib
import json
import logging
import os
import uuid

try:
    import asyncpg as _asyncpg
except ImportError:  # pragma: no cover — asyncpg absent in standalone test env
    _asyncpg = None  # type: ignore[assignment]

from app.database import enable_tcp_keepalive, split_listen_dsn

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

# --- Dedicated persistent publish connection -------------------------------
#
# WHY a dedicated connection (not the pooled engine)
# --------------------------------------------------
# Every WS control event AND every 500ms agent_log batch publishes a pg_notify.
# Previously each one checked out a full pooled ``AsyncSessionLocal`` from the
# shared 10+15 SQLAlchemy engine, ran ``SELECT pg_notify`` + commit, and
# returned the connection. Under leader event-loop saturation that pool is the
# same scarce resource the HTTP request handlers compete for, so the
# fire-and-forget messaging path directly starved request handling (cluster
# C12). pg_notify is a tiny, side-effect-only statement that does not need a
# transactional pooled session at all.
#
# Instead we own ONE module-level raw asyncpg connection, opened and supervised
# by ``publish_service`` (mirroring the LISTEN connector lifecycle), and run
# every pg_notify on it. The connection is process-wide and reused, so the hot
# publish path never touches the pool. A module-level asyncio.Lock serialises
# concurrent publishes onto the single connection (asyncpg connections are not
# safe for concurrent use). ``publish_service`` keeps the connection healthy
# with TCP keepalive + a periodic SELECT 1 watchdog and reconnects on failure,
# exactly like the LISTEN connectors.
_publish_conn: object | None = None
_publish_conn_lock: asyncio.Lock = asyncio.Lock()
# Seconds between SELECT-1 liveness probes on the publish connection.
_PUBLISH_KEEPALIVE_INTERVAL = 25
# Bound the SELECT-1 probe so a silently-hung socket is detected, not awaited
# forever (the keepalive watchdog's whole purpose).
_PUBLISH_PROBE_TIMEOUT = 10
# Minimum lifetime a connection must reach before we reset the reconnect-backoff
# attempt counter. Resetting on EVERY successful connect caused tight reconnect
# storms after rapid immediate-termination cycles (a connection that dies <1s
# after connecting would still reset backoff to 1s); gating on longevity makes
# repeated immediate terminations back off exponentially.
_CONNECTION_LONGEVITY_RESET_SECONDS = 30


def _get_raw_dsn() -> str | None:
    """Return the asyncpg-compatible DSN derived from DATABASE_URL.

    Strips the SQLAlchemy driver tag (``+asyncpg``) AND the SQLAlchemy-style
    ``ssl`` query parameter (see ``app.database.split_listen_dsn`` — left in the
    DSN it makes asyncpg send ``ssl`` as a Postgres startup parameter, which the
    server rejects with FATAL, breaking fanout). Returns None for non-Postgres /
    absent DATABASE_URL. Mirrors ``jwt_revocation._get_raw_dsn``.
    """
    return split_listen_dsn(os.getenv("DATABASE_URL"))[0]


def _get_ssl_arg() -> object | None:
    """asyncpg ``ssl=`` kwarg derived from DATABASE_URL's ssl/sslmode hint."""
    return split_listen_dsn(os.getenv("DATABASE_URL"))[1]


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
    """Emit one already-encoded payload onto the NOTIFY channel.

    Uses the dedicated persistent publish connection (see ``publish_service``)
    rather than a pooled ``AsyncSessionLocal``, so the hot per-event /
    per-500ms-batch path never competes with HTTP request handlers for the
    shared SQLAlchemy pool. asyncpg's ``execute`` of ``pg_notify`` is
    autocommitting (no explicit transaction), so the NOTIFY is delivered as
    soon as the statement returns. A module-level lock serialises concurrent
    publishes onto the single (non-reentrant) asyncpg connection. Raises on
    failure (connection not yet up, or socket error) — callers swallow.
    """
    if _publish_conn is None:
        # publish_service hasn't established the connection yet (or it is
        # mid-reconnect). Raise so the caller's best-effort guard logs and
        # degrades to local-only delivery for this event.
        raise RuntimeError("publish connection not available")
    async with _publish_conn_lock:
        conn = _publish_conn
        if conn is None:
            raise RuntimeError("publish connection not available")
        # asyncpg parameter placeholders are positional ($1, $2).
        await conn.execute("SELECT pg_notify($1, $2)", _NOTIFY_CHANNEL, encoded)


async def publish_service() -> None:
    """Background service: own the dedicated persistent pg_notify connection.

    Opens ONE raw asyncpg connection (never borrowed from the SQLAlchemy pool)
    used by ``_publish_encoded`` for every control-event and agent_log-batch
    pg_notify. Mirrors the LISTEN connector lifecycle: TCP keepalive on the
    socket, a periodic SELECT 1 watchdog (bounded by ``wait_for``) to detect a
    silently-hung socket, reconnect with bounded exponential backoff, and a
    longevity-gated backoff reset so rapid immediate terminations back off
    exponentially instead of storming reconnects.

    No-op sleep loop on non-Postgres / when asyncpg is absent (publishes already
    no-op via ``publish_project_event``'s ``_IS_POSTGRES`` guard).
    """
    global _publish_conn

    dsn = _get_raw_dsn()
    _ssl_arg = _get_ssl_arg()
    if not dsn or _asyncpg is None:
        logger.info(
            "[EVENT_FANOUT] Non-Postgres dialect or asyncpg absent; publish service inactive"
        )
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(365 * 24 * 3600)
        return

    attempt = 0
    while True:
        try:
            conn = await _asyncpg.connect(dsn, ssl=_ssl_arg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — connector loop must retry on any connect error
            wait = min(1 * 2**attempt, 60)
            logger.warning(
                "[EVENT_FANOUT] Publish connect failed (attempt %d, retry in %ds): %s",
                attempt,
                wait,
                exc,
            )
            attempt += 1
            await asyncio.sleep(wait)
            continue

        enable_tcp_keepalive(conn)
        connected_at = asyncio.get_event_loop().time()
        async with _publish_conn_lock:
            _publish_conn = conn
        logger.info("[EVENT_FANOUT] Publish connection established")

        try:
            # Periodic SELECT-1 watchdog: bounds detection of a silently-hung
            # socket (TCP keepalive is the kernel-level backstop; this is the
            # application-level one). A failed/timed-out probe breaks to the
            # reconnect path.
            while True:
                await asyncio.sleep(_PUBLISH_KEEPALIVE_INTERVAL)
                async with _publish_conn_lock:
                    probe_conn = _publish_conn
                    if probe_conn is None:
                        break
                    await asyncio.wait_for(
                        probe_conn.execute("SELECT 1"),
                        timeout=_PUBLISH_PROBE_TIMEOUT,
                    )
                # Reset backoff only once the connection has lived long enough
                # to be considered stable (gate on longevity, not on the mere
                # fact of a successful connect).
                if (
                    attempt
                    and asyncio.get_event_loop().time() - connected_at
                    >= _CONNECTION_LONGEVITY_RESET_SECONDS
                ):
                    attempt = 0
        except asyncio.CancelledError:
            # Clear the shared ref WITHOUT awaiting the lock: cancellation is
            # delivered on the single event loop and awaiting the lock here could
            # itself re-raise before the assignment runs. A bare assignment is
            # atomic w.r.t. the loop, so concurrent publishers see None safely.
            if _publish_conn is conn:
                _publish_conn = None
            with contextlib.suppress(Exception):
                await conn.close()
            raise
        except Exception as exc:  # noqa: BLE001 — connector loop logs and reconnects on any error
            logger.warning("[EVENT_FANOUT] Publish connection error: %s", exc)
        finally:
            # Same rationale as the cancel path: a bare assignment is atomic on
            # the event loop, so no lock is needed to retract the dead ref.
            if _publish_conn is conn:
                _publish_conn = None
            with contextlib.suppress(Exception):
                await conn.close()

        wait = min(1 * 2**attempt, 60)
        attempt += 1
        await asyncio.sleep(wait)


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


# Seconds between SELECT-1 liveness probes on a LISTEN connection while it is
# blocked waiting for a termination event. Bounds silent-partition detection.
_LISTEN_KEEPALIVE_INTERVAL = 25
_LISTEN_PROBE_TIMEOUT = 10


async def _wait_terminated_or_dead(
    conn: object, terminated_event: asyncio.Event, label: str
) -> None:
    """Block until the LISTEN connection terminates OR a SELECT-1 probe fails.

    ``terminated_event`` is set by asyncpg's termination listener on a clean
    server-side close. But a SILENT network partition (NLB idle drop, firewall
    eviction) delivers no FIN/RST, so the termination callback never fires and
    a bare ``terminated_event.wait()`` would hang forever, stranding fanout /
    revocation until process restart. This races the wait against a periodic
    SELECT-1 probe (bounded by ``wait_for``); a failed/timed-out probe means
    the socket is dead and we return so the caller reconnects. TCP keepalive
    (set on the socket at connect) is the kernel-level backstop; this is the
    application-level one. Returns when either condition is met; propagates
    CancelledError.
    """
    while not terminated_event.is_set():
        try:
            await asyncio.wait_for(
                terminated_event.wait(), timeout=_LISTEN_KEEPALIVE_INTERVAL
            )
            return  # terminated_event fired
        except asyncio.TimeoutError:  # noqa: PERF203 — per-iteration probe is the loop's purpose
            # Probe interval elapsed without a termination — verify liveness.
            try:
                await asyncio.wait_for(
                    conn.execute("SELECT 1"), timeout=_LISTEN_PROBE_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any probe failure means the socket is dead
                logger.warning(
                    "[%s] Liveness probe failed (silent partition?); reconnecting: %s",
                    label,
                    exc,
                )
                return


async def event_fanout_service() -> None:
    """Background service: LISTEN for cross-instance project control events.

    Opens a dedicated raw asyncpg connection (never borrowed from the
    SQLAlchemy pool, which recycles connections and enforces
    idle_in_transaction_session_timeout). On each NOTIFY, re-delivers the event
    to THIS instance's locally-connected WebSocket clients via the in-memory
    manager's local-delivery path (``manager.broadcast``) — NOT
    ``broadcast_to_project``, which would re-publish and loop forever.

    Reconnects with bounded exponential backoff (1s -> 60s) using asyncpg's
    add_termination_listener + asyncio.Event. The raw socket has TCP keepalive
    enabled and a companion periodic SELECT-1 watchdog bounds detection of a
    SILENT network partition (where ``terminated_event`` would otherwise never
    fire). The backoff reset is gated on connection longevity so rapid
    immediate terminations back off exponentially rather than storming
    reconnects. Mirrors ``jwt_revocation.jwt_revocation_service``.
    """
    # Late import avoids a circular dependency (broadcast imports this module's
    # publish helper); also lets us read the live module-level manager.
    from app.broadcast import manager  # noqa: PLC0415

    dsn = _get_raw_dsn()
    _ssl_arg = _get_ssl_arg()
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
            conn = await _asyncpg.connect(dsn, ssl=_ssl_arg)
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

        enable_tcp_keepalive(conn)
        connected_at = asyncio.get_event_loop().time()
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

            # Block until the server terminates the connection OR a liveness
            # probe detects a silent partition.
            await _wait_terminated_or_dead(conn, terminated_event, "EVENT_FANOUT")
            # Reset backoff ONLY if the connection lived long enough to be
            # considered stable (gate on longevity, not on the mere fact of a
            # successful connect). Resetting on every connect caused tight
            # reconnect storms after rapid immediate-termination cycles.
            if (
                attempt
                and asyncio.get_event_loop().time() - connected_at
                >= _CONNECTION_LONGEVITY_RESET_SECONDS
            ):
                attempt = 0
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
