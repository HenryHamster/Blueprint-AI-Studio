from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.frontend_static import (
    frontend_available,
    frontend_file_response,
    frontend_index_response,
    install_frontend_routes,
)


def _make_test_client(app: FastAPI) -> TestClient:
    """Construct the pinned Starlette client without its internal warning."""

    # Starlette 0.27 (pinned by FastAPI 0.104) still passes the deprecated
    # internal httpx `app=` shortcut. Suppress only that framework warning at
    # construction, matching the existing database/agent TestClient tests.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return TestClient(app)


def _write_frontend(root: Path) -> None:
    (root / "assets").mkdir()
    (root / "index.html").write_text("<html>Blueprint</html>", encoding="utf-8")
    (root / "assets" / "index-deadbeef.js").write_text(
        "console.log('Blueprint')", encoding="utf-8"
    )
    (root / "assets" / "unhashed.js").write_text(
        "console.log('mutable')", encoding="utf-8"
    )
    (root / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (root / "icons.svg").write_text("<svg></svg>", encoding="utf-8")


def test_frontend_availability_requires_index(tmp_path: Path):
    assert not frontend_available(tmp_path)
    _write_frontend(tmp_path)
    assert frontend_available(tmp_path)


def test_index_is_revalidated_and_hardened(tmp_path: Path):
    _write_frontend(tmp_path)

    response = frontend_index_response(tmp_path)

    assert response.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin-allow-popups"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    enforced_policy = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in enforced_policy
    assert "object-src 'none'" in enforced_policy
    policy = response.headers["content-security-policy-report-only"]
    assert "default-src 'self'" in policy
    assert "base-uri 'none'" in policy
    assert "object-src 'none'" in policy


def test_only_hashed_asset_is_immutable(tmp_path: Path):
    _write_frontend(tmp_path)

    asset = frontend_file_response(tmp_path, "assets/index-deadbeef.js")
    unhashed = frontend_file_response(tmp_path, "assets/unhashed.js")
    favicon = frontend_file_response(tmp_path, "favicon.svg")

    assert asset is not None
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert unhashed is not None
    assert unhashed.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert favicon is not None
    assert favicon.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"


def test_missing_file_returns_none_and_traversal_is_rejected(tmp_path: Path):
    _write_frontend(tmp_path)
    outside = tmp_path.parent / "outside-secret"
    outside.write_text("secret", encoding="utf-8")

    assert frontend_file_response(tmp_path, "missing.txt") is None
    with pytest.raises(PermissionError, match="escapes"):
        frontend_file_response(tmp_path, "../outside-secret")


def test_symlink_escape_is_rejected(tmp_path: Path):
    _write_frontend(tmp_path)
    outside = tmp_path.parent / "outside-symlink-secret"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "assets" / "secret-link"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(PermissionError, match="escapes"):
        frontend_file_response(tmp_path, "assets/secret-link")


def test_index_symlink_escape_is_not_available(tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    outside = tmp_path.parent / "outside-index.html"
    outside.write_text("secret", encoding="utf-8")
    try:
        (tmp_path / "index.html").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    assert not frontend_available(tmp_path)
    with pytest.raises(PermissionError, match="escapes"):
        frontend_index_response(tmp_path)


def test_installed_routes_serve_ui_without_masking_api_404s(tmp_path: Path):
    _write_frontend(tmp_path)
    app = FastAPI()

    @app.get("/api/known")
    def known_api():
        return {"ok": True}

    install_frontend_routes(app, tmp_path)
    client = _make_test_client(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "Blueprint" in root.text
    assert root.headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert client.get("/assets/index-deadbeef.js").status_code == 200
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/known").json() == {"ok": True}
    assert client.get("/api/typo").status_code == 404

    for path in ("/", "/assets/index-deadbeef.js", "/favicon.svg", "/icons.svg"):
        response = client.head(path)
        assert response.status_code == 200
        assert response.content == b""
        assert "cache-control" in response.headers


def test_installed_routes_preserve_health_response_without_bundle(tmp_path: Path):
    app = FastAPI()
    install_frontend_routes(app, tmp_path)

    response = _make_test_client(app).get("/")

    assert response.json() == {
        "status": "online",
        "service": "Blueprint Orchestrator",
    }
