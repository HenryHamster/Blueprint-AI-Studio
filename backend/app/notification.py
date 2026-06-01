"""Outbound webhook notification.

Extracted from ``app.main`` (P5 service-locator decomposition) so callers
(deploy_health, auto_remediation, pipeline_health_state, lifecycle, and the
alert-dispatch helpers) can import ``_send_webhook_notification`` directly
without depending on the FastAPI app entrypoint. ``app.main`` re-exports the
symbol for backward compatibility.

Pure leaf module — depends only on ``app.time_utils`` and the stdlib/httpx.
"""

import logging
import os

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _send_webhook_notification(event_type: str, title: str, details: str) -> None:
    """Fire-and-forget webhook notification for critical pipeline events."""
    url = os.getenv("PIPELINE_WEBHOOK_URL")
    if not url:
        return
    import httpx

    payload = {
        "event_type": event_type,
        "title": title,
        "details": details,
        "timestamp": _utcnow().isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"[WEBHOOK] POST returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"[WEBHOOK] Failed to send notification: {e}")
