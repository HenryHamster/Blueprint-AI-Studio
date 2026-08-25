"""Orchestrator error aggregation and acknowledgement endpoints."""

import asyncio
import os
import re
import time
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import OrchestratorErrorAck, _utcnow
from app.auth import get_current_user, require_admin
from app.time_utils import utc_iso

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-orchestrator-errors"],
    dependencies=[Depends(get_current_user)],
)


ORCHESTRATOR_ERRORS_CACHE_TTL_SECONDS = 60
ORCHESTRATOR_SERVICE_NAME = os.getenv(
    "ORCHESTRATOR_SERVICE_NAME", "blueprint-orchestrator"
)
_orchestrator_errors_cache: Dict[Tuple[int, int], Tuple[float, Dict[str, Any]]] = {}

# Acknowledged error buckets are persisted in the OrchestratorErrorAck table.
# Key is "exception_class|source" for a specific bucket, or "*" for a global ack.
# Buckets whose last_seen <= acknowledged_at are filtered from responses.

_EXCEPTION_LINE_RE = re.compile(
    r"^(?P<cls>(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning|Timeout|NotFound|Denied|Conflict))(?::|\b)",
    re.MULTILINE,
)
_TRACEBACK_FRAME_RE = re.compile(
    r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[^\n]+)',
)


def _extract_error_message(entry: Any) -> str:
    """Return the textual payload from a Cloud Logging entry, regardless of shape."""
    payload = getattr(entry, "payload", None)
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("message", "msg", "exception", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(payload, default=str)[:4000]
    if payload is None:
        # No text payload — extract container-level metadata from the entry
        parts: list[str] = []
        severity = getattr(entry, "severity", None)
        if severity:
            parts.append(f"severity={severity}")
        resource = getattr(entry, "resource", None)
        if resource:
            labels = getattr(resource, "labels", None) or {}
            if isinstance(labels, dict):
                for k in ("revision_name", "configuration_name"):
                    if labels.get(k):
                        parts.append(f"{k}={labels[k]}")
        http_req = getattr(entry, "http_request", None)
        if http_req and isinstance(http_req, dict):
            method = http_req.get("requestMethod", "")
            url = http_req.get("requestUrl", "")
            status = http_req.get("status", "")
            if method or url:
                parts.append(f"{method} {url} → {status}")
        labels = getattr(entry, "labels", None)
        if labels and isinstance(labels, dict):
            for k in ("instanceId", "run.googleapis.com/execution_environment"):
                if labels.get(k):
                    parts.append(f"{k}={labels[k]}")
        if parts:
            return "[Cloud Run system] " + " | ".join(parts)
        return ""
    return str(payload)[:4000]


def _parse_exception_class(message: str) -> str:
    """Scan a log payload for the last exception class name in a traceback."""
    if not message:
        return "UnknownError"
    if message.startswith("[Cloud Run system]"):
        return "ContainerError"
    matches = _EXCEPTION_LINE_RE.findall(message)
    if matches:
        return matches[-1].rstrip(":")
    for line in reversed(message.strip().splitlines()):
        stripped = line.strip()
        if (
            stripped
            and stripped[0].isupper()
            and ("Error" in stripped or "Exception" in stripped)
        ):
            head = stripped.split(":", 1)[0].strip()
            if head:
                return head
    return "UnknownError"


def _parse_source_location(message: str) -> Tuple[str, int | None]:
    """Return the first traceback frame inside the app source tree."""
    app_frame: Tuple[str, int] | None = None
    last_frame: Tuple[str, int] | None = None
    for match in _TRACEBACK_FRAME_RE.finditer(message):
        path = match.group("path")
        try:
            line_no = int(match.group("line"))
        except ValueError:
            continue
        last_frame = (path, line_no)
        if "/app/app/" in path or path.startswith("app/") or "\\app\\app\\" in path:
            app_frame = (path, line_no)
    chosen = app_frame or last_frame
    if chosen is None:
        return ("unknown", None)
    path, line_no = chosen
    marker = "/app/app/"
    idx = path.find(marker)
    if idx != -1:
        return (path[idx + len("/app/") :], line_no)
    return (path, line_no)


def _parse_entry_timestamp(entry: Any) -> datetime:
    ts = getattr(entry, "timestamp", None)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _bucket_log_entries(entries: List[Any], limit: int) -> List[Dict[str, Any]]:
    """Group log entries by (exception_class, source_file, source_line)."""
    buckets: Dict[Tuple[str, str, int | None], Dict[str, Any]] = {}
    for entry in entries:
        message = _extract_error_message(entry)
        exception_class = _parse_exception_class(message)
        source_file, source_line = _parse_source_location(message)
        key = (exception_class, source_file, source_line)
        ts = _parse_entry_timestamp(entry)
        bucket = buckets.get(key)
        if bucket is None:
            sample = message.strip()
            if len(sample) > 3000:
                sample = sample[:3000] + "..."
            buckets[key] = {
                "exception_class": exception_class,
                "source": f"{source_file}:{source_line}"
                if source_line
                else source_file,
                "count": 1,
                "first_seen_iso": utc_iso(ts),
                "last_seen_iso": utc_iso(ts),
                "sample_message": sample,
            }
            continue
        bucket["count"] += 1
        if ts.isoformat() < bucket["first_seen_iso"]:
            bucket["first_seen_iso"] = ts.isoformat()
        if ts.isoformat() > bucket["last_seen_iso"]:
            bucket["last_seen_iso"] = ts.isoformat()
    ranked = sorted(
        buckets.values(),
        key=lambda b: (b["count"], b["last_seen_iso"]),
        reverse=True,
    )
    return ranked[:limit]


def _fetch_orchestrator_error_entries(hours: int) -> List[Any]:
    """Query Cloud Logging for ERROR-severity entries from the orchestrator service."""
    from google.cloud import logging_v2

    gcp_project_id = os.getenv("GCP_PROJECT_ID", "")
    if not gcp_project_id or gcp_project_id == "your-gcp-project-id":
        raise RuntimeError("GCP_PROJECT_ID not configured")

    client = logging_v2.Client(project=gcp_project_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filter_str = (
        'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{ORCHESTRATOR_SERVICE_NAME}" '
        "severity>=ERROR "
        f'timestamp>="{cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
    )
    entries_iter = client.list_entries(
        filter_=filter_str,
        order_by=logging_v2.DESCENDING,
        page_size=500,
    )
    collected: List[Any] = []
    for entry in entries_iter:
        collected.append(entry)
        if len(collected) >= 500:
            break
    return collected


def _parse_bucket_last_seen(last_seen_iso: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(last_seen_iso)
    except (TypeError, ValueError):
        return None
    return (
        dt.astimezone(timezone.utc).replace(tzinfo=None)
        if dt.tzinfo is not None
        else dt
    )


def _filter_acknowledged_buckets(
    buckets: List[Dict[str, Any]],
    acks: Dict[str, datetime],
) -> List[Dict[str, Any]]:
    """Remove buckets whose last_seen is at or before the acknowledged timestamp."""
    if not acks:
        return buckets
    global_ack = acks.get("*")
    filtered = []
    for b in buckets:
        last_seen = _parse_bucket_last_seen(b["last_seen_iso"])
        if last_seen is None:
            filtered.append(b)
            continue
        key = f"{b['exception_class']}|{b['source']}"
        ack_ts = acks.get(key)
        if ack_ts and last_seen <= ack_ts:
            continue
        if global_ack and last_seen <= global_ack:
            continue
        filtered.append(b)
    return filtered


async def _load_acknowledgements(session: AsyncSession) -> Dict[str, datetime]:
    """Return all persisted error-bucket acknowledgements as a key→datetime map."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    result = await session.execute(
        select(OrchestratorErrorAck)
        .where(OrchestratorErrorAck.acknowledged_at >= cutoff)
        .limit(500)
    )
    return {row.key: row.acknowledged_at for row in result.scalars().all()}


async def _upsert_ack(session: AsyncSession, key: str, when: datetime, by: str) -> None:
    existing = await session.get(OrchestratorErrorAck, key)
    if existing is None:
        session.add(
            OrchestratorErrorAck(key=key, acknowledged_at=when, acknowledged_by=by)
        )
    else:
        existing.acknowledged_at = when
        existing.acknowledged_by = by


@router.get("/recent-orchestrator-errors")
async def get_recent_orchestrator_errors(
    hours: int = Query(default=1, ge=1, le=24),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate ERROR-severity logs from Cloud Logging for the orchestrator service."""
    acks = await _load_acknowledgements(session)
    cache_key = (hours, limit)
    now = time.time()
    cached = _orchestrator_errors_cache.get(cache_key)
    if cached is not None and (now - cached[0]) < ORCHESTRATOR_ERRORS_CACHE_TTL_SECONDS:
        cached_payload = cached[1]
        cached_buckets = cached_payload.get("buckets", [])
        visible = _filter_acknowledged_buckets(cached_buckets, acks)
        return {
            **cached_payload,
            "buckets": visible,
            "acknowledged_count": len(cached_buckets) - len(visible),
        }

    try:
        entries = await asyncio.to_thread(_fetch_orchestrator_error_entries, hours)
    except Exception as exc:
        logger.exception("Failed to fetch orchestrator error logs from Cloud Logging")
        payload: Dict[str, Any] = {
            "buckets": [],
            "hours": hours,
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _orchestrator_errors_cache[cache_key] = (now, payload)
        return payload

    all_buckets = _bucket_log_entries(entries, limit)
    _orchestrator_errors_cache[cache_key] = (
        now,
        {
            "buckets": all_buckets,
            "hours": hours,
            "total_entries_scanned": len(entries),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    visible = _filter_acknowledged_buckets(all_buckets, acks)
    return {
        "buckets": visible,
        "hours": hours,
        "total_entries_scanned": len(entries),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "acknowledged_count": len(all_buckets) - len(visible),
    }


@router.post("/orchestrator-errors/acknowledge")
async def acknowledge_orchestrator_error(
    exception_class: str = Query(...),
    source: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Acknowledge a specific orchestrator error bucket so it's hidden until new occurrences arrive."""
    key = f"{exception_class}|{source}"
    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    await _upsert_ack(session, key, _utcnow(), user_email)
    await session.commit()
    logger.info(f"[ADMIN] Orchestrator error acknowledged: {key} by {user_email}")
    return {"status": "acknowledged", "key": key}


@router.post("/orchestrator-errors/acknowledge-all")
async def acknowledge_all_orchestrator_errors(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Acknowledge all currently-visible orchestrator error buckets.

    Scope is the union of bucket keys across cached snapshots. Error-cached
    snapshots (those carrying an ``error`` field from a failed log fetch) are
    skipped so a transient upstream failure can't seed the acknowledge-all set.
    With no buckets to acknowledge the action is a no-op — it must never install
    a global wildcard suppression, which would hide real, unseen error buckets.
    """
    now_dt = _utcnow()
    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    acknowledged_keys: List[str] = []
    seen: set[str] = set()
    for _cached_ts, cached_payload in _orchestrator_errors_cache.values():
        # An error-cached snapshot has an empty bucket list plus an "error"
        # field; never let it (or any empty snapshot) widen the ack scope.
        if cached_payload.get("error"):
            continue
        for b in cached_payload.get("buckets", []):
            key = f"{b['exception_class']}|{b['source']}"
            if key in seen:
                continue
            seen.add(key)
            await _upsert_ack(session, key, now_dt, user_email)
            acknowledged_keys.append(key)
    await session.commit()
    logger.info(
        f"[ADMIN] All orchestrator errors acknowledged ({len(acknowledged_keys)} buckets) by {user_email}"
    )
    return {"status": "acknowledged", "count": len(acknowledged_keys)}


@router.delete("/orchestrator-errors/acknowledged")
async def clear_acknowledged_orchestrator_errors(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Clear all acknowledgments so previously hidden errors reappear."""
    from sqlalchemy import delete as sa_delete

    result = await session.execute(sa_delete(OrchestratorErrorAck))
    await session.commit()
    count = result.rowcount or 0
    return {"status": "cleared", "count": count}
