"""
CORS-on-error tests: every exception-handler response must carry the request's
Origin header so the browser surfaces the real status code instead of a
phantom CORS failure (the wildcard `*` Cloud Run injects is incompatible with
allow_credentials=true).
"""

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

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


def test_wildcard_origin_startup_rejected():
    """The production guard in main._resolve_allowed_origins must reject any
    list containing '*'. Wildcard + allow_credentials=True lets any origin
    authenticate on behalf of logged-in users."""
    from app.main import _resolve_allowed_origins

    # Plain '*'.
    with pytest.raises(ValueError, match=r"ALLOWED_ORIGINS may not contain '\*'"):
        _resolve_allowed_origins("*")

    # '*' mixed with explicit origins — the same bypass applies because
    # Starlette's CORSMiddleware will still echo arbitrary origins back when
    # '*' is anywhere in allow_origins with allow_credentials=True.
    with pytest.raises(ValueError, match=r"ALLOWED_ORIGINS may not contain '\*'"):
        _resolve_allowed_origins("*,https://foo.com")
    with pytest.raises(ValueError, match=r"ALLOWED_ORIGINS may not contain '\*'"):
        _resolve_allowed_origins("https://foo.com,*")
    # Whitespace around '*' must not let it slip past the check.
    with pytest.raises(ValueError, match=r"ALLOWED_ORIGINS may not contain '\*'"):
        _resolve_allowed_origins(" * ")

    # Concrete origins should parse cleanly.
    assert _resolve_allowed_origins("https://foo.com,https://bar.com") == [
        "https://foo.com",
        "https://bar.com",
    ]
    # Empty env falls back to the localhost defaults.
    assert "http://localhost:3000" in _resolve_allowed_origins("")


@pytest.mark.asyncio
async def test_wildcard_allowlist_does_not_echo_specific_origin():
    """When '*' is in the runtime allowlist, _apply_cors_headers must NOT echo
    a specific unrecognised Origin back — that would let arbitrary sites send
    credentialed requests on behalf of logged-in users."""
    evil_origin = "https://attacker.example.com"
    # Temporarily place '*' in the list (simulates misconfigured runtime state).
    allowed_origins.append("*")
    try:
        async with _client() as c:
            r = await c.get("/__test/raise-http-exc", headers={"Origin": evil_origin})
        # _apply_cors_headers must NOT reflect the attacker's specific origin.
        # A non-credentialed '*' from CORSMiddleware is acceptable; echoing the
        # attacker's domain with credentials is the vulnerability we prevent.
        assert r.headers.get("access-control-allow-origin") != evil_origin
    finally:
        allowed_origins.remove("*")
