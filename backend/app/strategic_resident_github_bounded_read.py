"""Shared hard-bounded GitHub JSON reads for Resident capabilities."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

from app.github import _get_client, _record_call, _record_consumer_call
from app.github_http import (
    is_rate_limited_response,
    raise_rate_limited,
    retry_after_seconds_from_response,
)
from app.github_token_resolver import invalidate_github_token_cache


MAX_GITHUB_JSON_BYTES = 1_000_000
MAX_PR_METADATA_BYTES = 256_000
_STREAM_CHUNK_BYTES = 4 * 1024
_DEFAULT_PR_METADATA_CONSUMER = "strategic_resident_pr_checks_metadata"


async def stream_github_json_object(
    token: str,
    *,
    url: str,
    project_id: uuid.UUID,
    max_bytes: int,
    consumer: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Stream one GitHub JSON object without buffering beyond ``max_bytes``."""

    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_GITHUB_JSON_BYTES:
        raise ValueError("GitHub JSON byte bound is invalid")
    if not isinstance(consumer, str) or not consumer or len(consumer) > 100:
        raise ValueError("GitHub consumer identity is invalid")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Accept-Encoding": "identity",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    stream_client = client or _get_client()
    body = bytearray()
    try:
        _record_call("GET", url)
        _record_consumer_call(consumer, url)
        async with stream_client.stream(
            "GET", url, headers=headers, timeout=20.0
        ) as response:
            if response.status_code == 401:
                invalidate_github_token_cache(str(project_id))
                raise HTTPException(
                    status_code=401,
                    detail="GitHub authentication failed",
                )
            if response.status_code in {404, 410}:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="GitHub resource is unavailable",
                )
            if is_rate_limited_response(response):
                raise_rate_limited(
                    "[GITHUB] Resident bounded inspection rate limited",
                    "GitHub rate limit exhausted during Resident inspection",
                    retry_after_seconds_from_response(response),
                )
            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail="GitHub bounded JSON request failed",
                )
            content_type = response.headers.get("Content-Type", "").casefold()
            if content_type and "json" not in content_type:
                raise HTTPException(
                    status_code=502,
                    detail="GitHub returned a non-JSON bounded response",
                )
            encoding = response.headers.get("Content-Encoding", "").casefold()
            if encoding not in {"", "identity"}:
                raise HTTPException(
                    status_code=502,
                    detail="GitHub returned an encoded bounded response",
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    declared_length = int(raw_length)
                except (TypeError, ValueError):
                    declared_length = -1
                if declared_length < 0 or declared_length > max_bytes:
                    raise HTTPException(
                        status_code=502,
                        detail="GitHub JSON response exceeds its byte bound",
                    )
            async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
                if not chunk:
                    continue
                remaining = max_bytes + 1 - len(body)
                if remaining > 0:
                    body.extend(chunk[:remaining])
                if len(body) > max_bytes:
                    raise HTTPException(
                        status_code=502,
                        detail="GitHub JSON response exceeds its byte bound",
                    )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail="GitHub bounded JSON stream is temporarily unavailable",
        ) from exc
    try:
        payload = json.loads(bytes(body))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail="GitHub returned invalid bounded JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="GitHub returned invalid bounded JSON",
        )
    return payload


async def stream_project_pr_metadata(
    token: str,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: uuid.UUID,
    consumer: str = _DEFAULT_PR_METADATA_CONSUMER,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch one PR metadata object under the shared hard byte ceiling."""

    return await stream_github_json_object(
        token,
        url=f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        project_id=project_id,
        max_bytes=MAX_PR_METADATA_BYTES,
        consumer=consumer,
        client=client,
    )


__all__ = [
    "MAX_GITHUB_JSON_BYTES",
    "MAX_PR_METADATA_BYTES",
    "stream_github_json_object",
    "stream_project_pr_metadata",
]
