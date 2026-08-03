"""Hardened responses for the bundled Blueprint administration frontend."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https:",
        "font-src 'self' data:",
        "connect-src 'self' https://*.googleapis.com https://*.firebaseio.com "
        "wss://*.firebaseio.com",
        "frame-src 'self' https://accounts.google.com https://*.firebaseapp.com",
        "worker-src 'self' blob:",
        "manifest-src 'self'",
        "form-action 'self' https://accounts.google.com",
        "upgrade-insecure-requests",
    )
)

_BASE_HEADERS = {
    # Enforce the non-breaking structural controls immediately. The broader
    # provider/network policy below remains report-only for the first rollout.
    "Content-Security-Policy": (
        "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self' https://accounts.google.com"
    ),
    # Enforce after the clean-browser Firebase/GitHub OAuth rollout smoke. A
    # report-only first deployment prevents an incomplete provider allow-list
    # from turning an otherwise safe image update into an authentication outage.
    "Content-Security-Policy-Report-Only": _CONTENT_SECURITY_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # The public endpoint is HTTPS-only through Tailscale Funnel. Keep this
    # host-scoped because Blueprint does not control the parent ts.net domain.
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

_HASHED_ASSET_NAME = re.compile(r"-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")


def _frontend_file_path(
    static_dir: str | Path, full_path: str
) -> tuple[Path, Path] | None:
    """Resolve a real file below the static root, including through symlinks."""

    root = Path(static_dir).resolve(strict=True)
    candidate = (root / full_path).resolve(strict=False)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError("frontend path escapes the static root") from exc
    if not candidate.is_file():
        return None
    return candidate, relative


def frontend_available(static_dir: str | Path) -> bool:
    """Return whether a complete frontend entrypoint is installed."""

    try:
        return _frontend_file_path(static_dir, "index.html") is not None
    except (FileNotFoundError, PermissionError):
        return False


def _headers(*, immutable: bool) -> dict[str, str]:
    headers = dict(_BASE_HEADERS)
    headers["Cache-Control"] = (
        "public, max-age=31536000, immutable"
        if immutable
        else "no-cache, max-age=0, must-revalidate"
    )
    return headers


def frontend_index_response(static_dir: str | Path) -> FileResponse:
    """Serve the SPA entrypoint without allowing a stale deployment shell."""

    resolved = _frontend_file_path(static_dir, "index.html")
    if resolved is None:
        raise FileNotFoundError("frontend index is not installed")
    index_path, _ = resolved
    return FileResponse(index_path, headers=_headers(immutable=False))


def frontend_file_response(
    static_dir: str | Path, full_path: str
) -> FileResponse | None:
    """Serve a real frontend file while rejecting traversal and symlink escapes."""

    resolved = _frontend_file_path(static_dir, full_path)
    if resolved is None:
        return None
    candidate, relative = resolved
    immutable = bool(
        relative.parts
        and relative.parts[0] == "assets"
        and _HASHED_ASSET_NAME.search(relative.name)
    )
    return FileResponse(candidate, headers=_headers(immutable=immutable))


def install_frontend_routes(app: FastAPI, static_dir: str | Path) -> None:
    """Install the narrow UI surface without intercepting backend API paths."""

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def serve_frontend_index():
        if frontend_available(static_dir):
            return frontend_index_response(static_dir)
        return {"status": "online", "service": "Blueprint Orchestrator"}

    if not frontend_available(static_dir):
        return

    @app.api_route(
        "/assets/{asset_path:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def serve_frontend_asset(asset_path: str):
        try:
            response = frontend_file_response(static_dir, f"assets/{asset_path}")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Access denied") from exc
        if response is not None:
            return response
        raise HTTPException(status_code=404, detail="Frontend asset not found")

    @app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_frontend_favicon():
        response = frontend_file_response(static_dir, "favicon.svg")
        if response is None:
            raise HTTPException(status_code=404, detail="Frontend file not found")
        return response

    @app.api_route("/icons.svg", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_frontend_icons():
        response = frontend_file_response(static_dir, "icons.svg")
        if response is None:
            raise HTTPException(status_code=404, detail="Frontend file not found")
        return response
