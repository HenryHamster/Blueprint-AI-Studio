"""Outbound webhook notification.

Extracted from ``app.main`` (P5 service-locator decomposition) so callers
(deploy_health, auto_remediation, pipeline_health_state, lifecycle, and the
alert-dispatch helpers) can import ``_send_webhook_notification`` directly
without depending on the FastAPI app entrypoint. ``app.main`` re-exports the
symbol for backward compatibility.

Depends on ``app.time_utils``, ``app.url_security``, and the stdlib/httpx.
"""

import asyncio
import logging
import os
import urllib.parse

from app.time_utils import _utcnow
from app.url_security import _resolve_safe_external_url

logger = logging.getLogger(__name__)


async def _send_webhook_notification(event_type: str, title: str, details: str) -> None:
    """Fire-and-forget webhook notification for critical pipeline events.

    Defeats DNS rebinding by resolving the hostname, validating the IP is
    public, then pinning the connection to that exact IP — substituting the
    resolved address into the URL while preserving the original Host header
    and TLS SNI hostname so cert validation still works against the hostname.
    """
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

    # SSRF guard: resolve + validate IP before connecting.
    safe, reason, resolved_ip = await asyncio.to_thread(
        _resolve_safe_external_url, url
    )
    if not safe or resolved_ip is None:
        logger.warning(
            "[WEBHOOK] SSRF check blocked notification to %r: %s", url, reason
        )
        return

    parsed = urllib.parse.urlparse(url)
    original_host = parsed.hostname or ""
    # IPv6 literals need bracketing in URL netloc
    ip_in_netloc = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip
    netloc = f"{ip_in_netloc}:{parsed.port}" if parsed.port else ip_in_netloc
    pinned_url = urllib.parse.urlunparse(parsed._replace(netloc=netloc))

    # Preserve original Host header (for HTTP virtual hosting) and TLS SNI
    # (for cert validation against the hostname, not the IP).
    request_host_header = (
        f"{original_host}:{parsed.port}" if parsed.port else original_host
    )
    headers = {"Host": request_host_header}
    extensions = {"sni_hostname": original_host}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                pinned_url, json=payload, headers=headers, extensions=extensions
            )
            if resp.status_code >= 400:
                logger.warning(f"[WEBHOOK] POST returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"[WEBHOOK] Failed to send notification: {e}")
