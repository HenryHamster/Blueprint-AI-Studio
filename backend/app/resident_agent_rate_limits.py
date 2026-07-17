"""Shared two-tier throttling identities for Resident agent endpoints."""

import hashlib

from fastapi import Request
from slowapi.util import get_remote_address


def resident_task_token_rate_key(request: Request) -> str:
    token = request.headers.get("x-agent-token", "")
    material = f"task-token:{token}" if token else f"missing-token:{get_remote_address(request)}"
    return hashlib.sha256(material.encode()).hexdigest()


def resident_remote_rate_key(request: Request) -> str:
    return hashlib.sha256(
        f"remote:{get_remote_address(request)}".encode()
    ).hexdigest()


__all__ = ["resident_remote_rate_key", "resident_task_token_rate_key"]
