"""
CORS-on-error tests: every exception-handler response must carry the request's
Origin header so the browser surfaces the real status code instead of a
phantom CORS failure (the wildcard `*` Cloud Run injects is incompatible with
allow_credentials=true).
"""

import pytest
from fastapi import HTTPException
from httpx import AsyncClient, ASGITransport

from app.main import allowed_origins, app


@pytest.fixture(autouse=True)
def _allow_test_origin():
    """Inject the test origin into the module-level allowlist so the handler
    will echo it. _apply_cors_headers resolves allowed_origins at call time."""
    test_origin = "https://blueprint-orchestrator.web.app"
    added = test_origin not in allowed_origins
    if added:
        allowed_origins.append(test_origin)
    yield
    if added:
        allowed_origins.remove(test_origin)


TEST_ORIGIN = "https://blueprint-orchestrator.web.app"


@app.get("/__test/raise-http-exc")
async def _raise_http_exc():
    raise HTTPException(status_code=429, detail="rate limited")


@app.get("/__test/raise-generic")
async def _raise_generic():
    raise RuntimeError("boom")


def _client():
    # raise_app_exceptions=False so generic RuntimeError doesn't re-raise into
    # the test (Starlette's ServerErrorMiddleware invokes our handler AND then
    # re-raises in debug). We want the handler's response surface in the test.
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://t",
    )


@pytest.mark.asyncio
async def test_http_exception_echoes_origin():
    async with _client() as c:
        r = await c.get("/__test/raise-http-exc", headers={"Origin": TEST_ORIGIN})
    assert r.status_code == 429
    assert r.headers.get("access-control-allow-origin") == TEST_ORIGIN
    assert r.headers.get("access-control-allow-credentials") == "true"
    # CORSMiddleware also adds Vary:Origin on the way out, so the final value
    # may be "Origin" or "Origin, Origin" — check membership, not equality.
    assert "Origin" in r.headers.get("vary", "")


@pytest.mark.asyncio
async def test_generic_exception_echoes_origin():
    async with _client() as c:
        r = await c.get("/__test/raise-generic", headers={"Origin": TEST_ORIGIN})
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == TEST_ORIGIN
    assert r.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_disallowed_origin_gets_no_specific_header():
    """A request from an origin not in the allowlist must NOT receive that
    origin echoed — otherwise we'd be granting CORS to arbitrary sites."""
    async with _client() as c:
        r = await c.get(
            "/__test/raise-http-exc", headers={"Origin": "https://evil.example.com"}
        )
    assert r.status_code == 429
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


@pytest.mark.asyncio
async def test_no_origin_header_no_cors_header():
    async with _client() as c:
        r = await c.get("/__test/raise-http-exc")
    assert r.status_code == 429
    # When no Origin is sent, no per-origin header should be emitted unless `*`
    # is explicitly in the allowlist (which the test fixture does not add).
    if "*" not in allowed_origins:
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
