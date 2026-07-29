"""Small, server-only LiveKit participant-token boundary for call mode."""

import hashlib
import os
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit


class LiveKitConfigurationError(RuntimeError):
    """Voice transport is explicitly configured but cannot safely issue a token."""


def _livekit_config() -> tuple[str, str, str] | None:
    server_url = os.environ.get("LIVEKIT_URL", "").strip()
    api_key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    if not any((server_url, api_key, api_secret)):
        return None
    if not all((server_url, api_key, api_secret)):
        raise LiveKitConfigurationError("LiveKit transport is configured incompletely")
    parsed = urlsplit(server_url)
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LiveKitConfigurationError(
            "LiveKit transport URL must be a credential-free wss:// endpoint"
        )
    if len(api_key) > 512 or len(api_secret) > 4096 or "\x00" in api_key + api_secret:
        raise LiveKitConfigurationError("LiveKit transport credentials are invalid")
    return server_url, api_key, api_secret


def _issue_credentials(
    *, call: dict[str, Any], identity: str, name: str, browser: bool
) -> dict[str, str]:
    config = _livekit_config()
    if config is None:
        raise LiveKitConfigurationError("LiveKit transport is not configured")
    server_url, api_key, api_secret = config
    try:
        from livekit import api as livekit_api
    except ImportError as exc:
        raise LiveKitConfigurationError(
            "LiveKit transport is configured but the livekit-api dependency is unavailable"
        ) from exc
    call_id = str(call["call_id"])
    generation = int(call["generation"])
    room = f"bp-call-{call_id}-{generation}"
    grants = livekit_api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        # Neither participant needs camera, screen-share, or arbitrary tracks:
        # both publish exactly one microphone-shaped audio source plus trusted
        # data packets.  Keep the coordinator least-privileged too; a leaked
        # five-minute agent token must not become a general room publisher.
        can_publish_sources=["microphone"],
    )
    token = (
        livekit_api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(name)
        .with_ttl(timedelta(minutes=5))
        .with_grants(grants)
        .to_jwt()
    )
    return {
        "server_url": server_url,
        "participant_token": token,
        "room": room,
        "participant_identity": identity,
    }


def issue_browser_call_credentials(
    *, call: dict[str, Any], user_id: str
) -> dict[str, str]:
    """Issue one short-lived microphone/data browser token for the lease owner."""
    identity = _browser_identity(call, user_id)
    credentials = _issue_credentials(
        call=call,
        identity=identity,
        name="Blueprint operator",
        browser=True,
    )
    worker_task_id = str(call.get("worker_task_id") or "").strip()
    if credentials and worker_task_id:
        credentials["agent_identity"] = _resident_identity(call, worker_task_id)
    return credentials


def _resident_identity(call: dict[str, Any], task_id: str) -> str:
    task_tag = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
    return f"bp-coordinator-{call['call_id']}-{int(call['generation'])}-{task_tag}"


def _browser_identity(call: dict[str, Any], user_id: str) -> str:
    user_tag = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return f"bp-browser-{call['call_id']}-{int(call['generation'])}-{user_tag}"


def issue_resident_call_credentials(
    *, call: dict[str, Any], task_id: str
) -> dict[str, str]:
    """Issue a coordinator join token; it never exposes the server API secret."""
    credentials = _issue_credentials(
        call=call,
        identity=_resident_identity(call, task_id),
        name="Blueprint call coordinator",
        browser=False,
    )
    actor_user_id = str(call.get("actor_user_id") or "").strip()
    if credentials and actor_user_id:
        credentials["operator_identity"] = _browser_identity(call, actor_user_id)
    return credentials
