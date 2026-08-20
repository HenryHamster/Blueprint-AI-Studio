"""
Route-table auth invariant for /api/admin endpoints.

Every route under /api/admin must declare require_admin or get_current_user
somewhere in its FastAPI dependency tree. One forgotten Depends silently ships
an unauthenticated endpoint — this test catches that before merge.

Run with: pytest testing/test_admin_auth_invariant.py -v --noconftest
"""

import os

# auth.py raises ValueError at module load if these are absent
os.environ.setdefault("AGENT_JWT_SECRET", "test-secret-for-invariant-check-x")
os.environ.setdefault("WS_TICKET_SECRET", "test-ws-secret-for-invariant-check")

from fastapi.routing import APIRoute

from app.auth import (
    get_current_user,
    get_current_user_optional,
    get_metrics_reader,
    require_admin,
)
from app.main import app
from testing.fixgate_attribution import format_offender_marker

# Intentional exceptions — expected to be empty.
# When a route genuinely should be unauthenticated (e.g. a public health check
# under /api/admin for infra monitoring), add its path here with a comment
# explaining why auth is intentionally absent.
ALLOWLIST: set[str] = {
    # POST /api/admin/token authenticates via direct ADMIN_API_KEY comparison
    # (not Depends(get_current_user) / require_admin), because it is the
    # token-issuance endpoint that exchanges the shared secret for a JWT.
    "/api/admin/token",
    # GET /api/admin/projects/{project_id}/tools/{tool_slug}/assets/{path:path}
    # uses JWT-based auth via a ?token query parameter (not Firebase/cookie auth)
    # because this endpoint serves assets to opaque-origin (null) iframes that
    # cannot send cookies. The capability JWT carries scope: "tool-asset" and is
    # validated inline by _verify_tool_asset_token.
    "/api/admin/projects/{project_id}/tools/{tool_slug}/assets/{path:path}",
    # GET /api/admin/projects/{project_id}/tools/{tool_slug}/t/{token}/assets/{path:path}
    # is the path-embedded-token variant. The token sits in the URL path so that
    # relative subresource requests (e.g. <script src="./app.js">) inherit auth
    # automatically inside sandboxed opaque-origin iframes (where query-string
    # tokens are dropped on relative URL resolution). Auth is performed
    # identically via _verify_tool_asset_token (scope: "tool-asset").
    "/api/admin/projects/{project_id}/tools/{tool_slug}/t/{token}/assets/{path:path}",
}

# get_metrics_reader is a narrowly scoped read-only auth dep that accepts
# agent JWTs; it is a valid auth callable for metrics-only endpoints.
# get_current_user_optional is used on endpoints that accept EITHER an admin
# bearer token OR an agent JWT via x-agent-token; the endpoint enforces
# auth itself when the optional dep returns None.
_AUTH_CALLABLES = frozenset(
    {get_current_user, get_current_user_optional, require_admin, get_metrics_reader}
)


def _collect_dep_callables(dependant, visited: set) -> set:
    """Recursively collect all dependency callables in a FastAPI Dependant tree."""
    if id(dependant) in visited:
        return set()
    visited.add(id(dependant))

    found = set()
    if dependant.call is not None:
        found.add(dependant.call)
    for dep in dependant.dependencies:
        found.update(_collect_dep_callables(dep, visited))
    return found


def test_all_admin_routes_require_auth():
    """Every /api/admin route must have require_admin or get_current_user in its dependency tree."""
    offenders = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api/admin"):
            continue
        if route.path in ALLOWLIST:
            continue

        dep_callables = _collect_dep_callables(route.dependant, set())

        if not dep_callables.intersection(_AUTH_CALLABLES):
            offenders.append(f"{', '.join(sorted(route.methods or []))} {route.path}")

    assert not offenders, (
        "The following /api/admin routes have NO require_admin or get_current_user "
        "dependency — add Depends(require_admin) to each route signature, or add to "
        "ALLOWLIST if the route is intentionally public:\n"
        + "\n".join(f"  - {r}" for r in sorted(offenders))
        + "\n"
        + format_offender_marker(offenders)
    )
