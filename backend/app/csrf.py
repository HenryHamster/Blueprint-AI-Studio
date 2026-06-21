"""CSRF protection middleware using the double-submit cookie pattern."""

import secrets
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths authenticated by non-cookie mechanisms (HMAC signatures, etc.) that
# cannot satisfy the double-submit cookie requirement and are not CSRF-vulnerable.
_EXEMPT_PATHS = frozenset({"/api/github/webhook"})


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection for state-changing requests.

    Safe methods (GET, HEAD, OPTIONS) pass through unconditionally; a
    csrf_token cookie is issued on GET responses when not already present
    so the frontend can read it and attach it to future mutations.

    State-changing methods (POST, PUT, PATCH, DELETE) are exempt when:
    - The path is in _EXEMPT_PATHS (HMAC-signed server-to-server endpoints).
    - The request carries an Authorization: Bearer header — Bearer-token auth
      cannot be forged cross-origin without the secret.
    - The request carries an X-Agent-Token header — agent fleet auth uses a
      signed JWT in this header and never sends cookies, so CSRF is inapplicable.

    All other state-changing requests must supply an X-CSRF-Token header whose
    value matches the csrf_token cookie; mismatch yields 403.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method in _SAFE_METHODS:
            response = await call_next(request)
            if CSRF_COOKIE_NAME not in request.cookies:
                _set_csrf_cookie(response, request)
            return response

        # Path-level exemption for HMAC-signed server-to-server endpoints
        # (e.g. GitHub webhooks) that send neither cookies nor Bearer tokens.
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # Bearer-authenticated requests are immune to CSRF — the Authorization
        # header cannot be set by cross-origin HTML forms or redirects.
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return await call_next(request)

        # Agent-fleet requests authenticate via X-Agent-Token (a signed JWT).
        # They never send cookies, so the double-submit pattern cannot apply,
        # and they are not vulnerable to CSRF attacks.
        if request.headers.get("X-Agent-Token"):
            return await call_next(request)

        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        header_token = request.headers.get(CSRF_HEADER_NAME)

        if not cookie_token or not header_token:
            return _csrf_forbidden("CSRF token missing")

        if not secrets.compare_digest(cookie_token, header_token):
            return _csrf_forbidden("CSRF token mismatch")

        return await call_next(request)


def _set_csrf_cookie(response: Response, request: Request) -> None:
    token = secrets.token_urlsafe(32)
    is_secure = request.url.scheme == "https"
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,  # Must be JS-readable so the frontend can send it in X-CSRF-Token
        samesite="strict",
        secure=is_secure,
        max_age=86400,
        path="/",
    )


def _csrf_forbidden(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"error": {"code": 403, "message": message}},
    )
