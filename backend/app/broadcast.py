"""WebSocket broadcast module.

Contains the ConnectionManager, broadcast_to_project helper, and log
sanitization utilities.  Extracted from main.py to break the circular
import web: all extracted backend modules (task_lifecycle, task_completion,
reaper, services, routers/agent, admin) previously needed deferred imports
from app.main just to call broadcast_to_project.  Now they import directly
from this module at the top level.
"""

import asyncio
import ipaddress
import logging
import re
import time
from typing import Dict, List

from fastapi import WebSocket
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)


def _get_websocket_client_ip(websocket: WebSocket) -> str:
    """Return the real client IP for a WebSocket connection.

    In Cloud Run, the GCP load balancer terminates external TLS and forwards
    traffic over an internal (RFC-1918) TCP connection.  We trust
    X-Forwarded-For only when the direct TCP peer is a private IP, because
    external attackers cannot forge their TCP source address.  If the direct
    connection is already from a public IP (e.g. local dev, direct access),
    we use it as-is and ignore X-Forwarded-For to prevent spoofing.
    """
    if not websocket.client:
        return "unknown"
    direct_ip = websocket.client.host
    try:
        is_private = ipaddress.ip_address(direct_ip).is_private
    except ValueError:
        return direct_ip
    if is_private:
        xff = websocket.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return direct_ip


# --- In-Memory Event Bus for WebSockets ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
        self.ip_connections: Dict[str, int] = {}
        self.last_activity: Dict[WebSocket, float] = {}
        self.MAX_CONNECTIONS_PER_IP = 20
        self._lock = asyncio.Lock()
        self._last_cleanup: float = 0.0

    async def check_and_add_ip(self, websocket: WebSocket) -> bool:
        client_ip = _get_websocket_client_ip(websocket)
        if client_ip != "unknown":
            if self.ip_connections.get(client_ip, 0) >= self.MAX_CONNECTIONS_PER_IP:
                await websocket.close(
                    code=1008, reason="Too many connections from this IP"
                )
                return False
            self.ip_connections[client_ip] = self.ip_connections.get(client_ip, 0) + 1
        return True

    def _remove_ip(self, websocket: WebSocket):
        client_ip = _get_websocket_client_ip(websocket)
        if client_ip != "unknown" and client_ip in self.ip_connections:
            self.ip_connections[client_ip] -= 1
            if self.ip_connections[client_ip] <= 0:
                del self.ip_connections[client_ip]

    # Keep old name for any external callers
    def remove_ip(self, websocket: WebSocket):
        self._remove_ip(websocket)

    def update_activity(self, websocket: WebSocket) -> None:
        """Update the last-activity timestamp for a connection."""
        self.last_activity[websocket] = time.time()

    async def connect(self, websocket: WebSocket, project_id: str) -> bool:
        async with self._lock:
            if not await self.check_and_add_ip(websocket):
                return False

            if project_id not in self.active_connections:
                self.active_connections[project_id] = []
            self.active_connections[project_id].append(websocket)
            self.last_activity[websocket] = time.time()
            return True

    async def disconnect(self, websocket: WebSocket, project_id: str):
        async with self._lock:
            if project_id in self.active_connections:
                if websocket in self.active_connections[project_id]:
                    self.active_connections[project_id].remove(websocket)
                if not self.active_connections[project_id]:
                    del self.active_connections[project_id]

            self._remove_ip(websocket)
            self.last_activity.pop(websocket, None)

    async def cleanup_idle(self, max_idle_seconds: float = 90.0) -> None:
        """Close connections that have had no activity for longer than max_idle_seconds."""
        now = time.time()
        async with self._lock:
            idle = [
                (ws, pid)
                for pid, conns in self.active_connections.items()
                for ws in list(conns)
                if now - self.last_activity.get(ws, now) > max_idle_seconds
            ]
        for ws, pid in idle:
            logger.info(
                f"[WS] Closing idle connection (project={pid}, idle>{max_idle_seconds:.0f}s)"
            )
            # Skip close() if the socket already closed itself out-of-band.
            # Calling close() on a DISCONNECTED websocket raises starlette's
            # `RuntimeError: Cannot call "send" once a close message has been
            # sent`, which surfaced 1× in production at 19:51:17 UTC during
            # the Cloud Run cutover cascade. The connection state check makes
            # the cleanup idempotent.
            if (
                ws.application_state != WebSocketState.DISCONNECTED
                and ws.client_state != WebSocketState.DISCONNECTED
            ):
                try:
                    await ws.close(code=1001, reason="Idle timeout")
                except RuntimeError as _close_exc:
                    # Race: client closed between the state check and the
                    # close() call. Benign — just clean up our bookkeeping.
                    logger.debug(
                        f"[WS] Idle close race for project {pid}: {_close_exc}"
                    )
                except Exception:
                    logger.exception(
                        f"[WS] Failed to close idle connection for project {pid}"
                    )
            await self.disconnect(ws, pid)

    async def broadcast(self, project_id: str, event_type: str, data: dict):
        # Opportunistic idle cleanup — runs at most once every 30 seconds
        now = time.time()
        if now - self._last_cleanup >= 30.0:
            self._last_cleanup = now
            asyncio.create_task(self.cleanup_idle())

        async with self._lock:
            if project_id not in self.active_connections:
                return
            connections = list(self.active_connections[project_id])  # noqa: PERF101 — snapshot under lock
        message = {"event": event_type, "data": data}
        for connection in connections:
            try:
                await asyncio.wait_for(connection.send_json(message), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[WS] Send timeout for client (project={project_id}, event={event_type})"
                )
                await self.disconnect(connection, project_id)
            except Exception as e:
                logger.warning(
                    f"[WS] Failed to send message to client (project={project_id}, event={event_type}): {e}"
                )
                await self.disconnect(connection, project_id)

    async def broadcast_to_all(self, event_type: str, data: dict):
        """Broadcasts a message to all connected WebSocket clients across all projects."""
        for project_id in list(self.active_connections.keys()):  # noqa: PERF101 — broadcast() can remove entries via disconnect()
            await self.broadcast(project_id, event_type, data)


manager = ConnectionManager()


async def broadcast_to_project(project_id: str, event_type: str, data: dict):
    """Broadcasts a message to all WebSocket clients listening to a specific project.

    Delivers locally (to clients on THIS instance) and additionally publishes
    the event onto the cross-instance NOTIFY channel so sibling Cloud Run
    instances re-deliver it to their own locally-connected clients. This is the
    'originally broadcast here' path (local deliver + publish); the
    event_fanout listener's 'received from peer' path delivers locally only,
    which is the structural break that prevents an infinite re-publish loop.

    Swallows exceptions for both steps to prevent crashing HTTP handlers after
    DB commits (the reaper / agent / orchestrator paths rely on this).
    """
    try:
        await manager.broadcast(project_id, event_type, data)
    except Exception as e:
        logger.warning(
            f"[WS] Broadcast failed (project={project_id}, event={event_type}): {e}"
        )
    # Late import avoids a circular dependency: event_fanout's listener imports
    # `manager` from this module. publish_project_event is itself best-effort
    # (no-op on non-Postgres, swallows all errors) but we guard again here so a
    # publish failure never escapes the local-delivery contract above.
    try:
        from app.event_fanout import publish_project_event

        await publish_project_event(project_id, event_type, data)
    except Exception as e:
        logger.warning(
            f"[WS] Cross-instance publish failed (project={project_id}, event={event_type}): {e}"
        )


# Log sanitization patterns — mirrors agent/src/index.ts sanitize() to catch secrets
# that bypass agent-side sanitization (compromised agent, direct WS injection)
# keep in sync with agent/src/utils/sanitize.ts and backend/app/failure_classification.py
# Value group uses [^'"\\]|\\. (unambiguous: first alt rejects backslash, second starts with
# backslash) — avoids the exponential backtracking that ((?:\\.|(?!\4)[\s\S])+) exhibits
# on inputs with many backslashes and no closing quote.
_CRED_FIELD_RE = re.compile(
    r"""(["\']?)(access_token|refresh_token|id_token|api_key|client_secret|private_key|token)\1(\s*[:=]\s*)(["\'])((?:[^'"\\]|\\.)+)\4""",
    re.IGNORECASE,
)

_SANITIZE_PATTERNS = [
    (re.compile(r"https://x-access-token:[^@]+@"), "https://x-access-token:***@"),
    (re.compile(r"ghp_[A-Za-z0-9_]+"), "ghp_***"),
    (re.compile(r"ghs_[A-Za-z0-9_]+"), "ghs_***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "github_pat_***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-***"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "sk-proj-***"),
    (re.compile(r"sk-(?!ant-|proj-)[A-Za-z0-9_\-]{20,}"), "sk-***"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "AIza***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "eyJ***"),
    (re.compile(r'Bearer\s+[^\s,\'"]+', re.IGNORECASE), "Bearer ***"),
    (re.compile(r'oauth_token[=:]\s*[^\s&\'"]+', re.IGNORECASE), "oauth_token=***"),
    (
        re.compile(
            r"-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----"
        ),
        "***PRIVATE KEY***",
    ),
]


def _sanitize_log(text: str) -> str:
    """Redact secrets from log text before broadcasting to WebSocket clients."""
    if not text:
        return text
    for pattern, replacement in _SANITIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _CRED_FIELD_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}***{m.group(4)}",
        text,
    )
    return text
