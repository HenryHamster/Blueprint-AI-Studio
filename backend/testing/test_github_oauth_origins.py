from fastapi import HTTPException

from app.routers.github import (
    _admin_fallback_origin,
    _allowed_admin_origins,
    _canonical_ui_origin,
    _github_callback_uri,
)


def test_canonical_ui_origin_rejects_open_redirect_shapes():
    assert _canonical_ui_origin("https://hamsterwheel.example") == (
        "https://hamsterwheel.example"
    )
    assert _canonical_ui_origin("https://hamsterwheel.example/") == (
        "https://hamsterwheel.example"
    )
    assert _canonical_ui_origin("https://user@hamsterwheel.example") is None
    assert _canonical_ui_origin("https://hamsterwheel.example/path") is None
    assert _canonical_ui_origin("https://hamsterwheel.example?next=evil") is None
    assert _canonical_ui_origin("http://hamsterwheel.example") is None
    assert _canonical_ui_origin("http://localhost:3001", allow_local_http=True) == (
        "http://localhost:3001"
    )


def test_allowed_admin_origins_are_exact_configured_origins(monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_ORIGINS",
        "https://blueprint.web.app,https://hamsterwheel.example,*,https://evil.test/path",
    )
    assert _allowed_admin_origins("https://hamsterwheel.example") == {
        "https://blueprint.web.app",
        "https://hamsterwheel.example",
    }


def test_callback_and_fallback_origins_fail_closed(monkeypatch):
    monkeypatch.setenv("ADMIN_UI_URL", "https://user@evil.test")
    assert _admin_fallback_origin() == "https://blueprint-admin-9c6b7.web.app"

    monkeypatch.setenv("ORCHESTRATOR_URL", "https://hamsterwheel.example/")
    assert _github_callback_uri() == (
        "https://hamsterwheel.example/api/github/oauth/callback"
    )
    monkeypatch.setenv("ORCHESTRATOR_URL", "https://evil.test/path")
    try:
        _github_callback_uri()
    except HTTPException as exc:
        assert exc.status_code == 500
    else:
        raise AssertionError("invalid ORCHESTRATOR_URL was accepted")
