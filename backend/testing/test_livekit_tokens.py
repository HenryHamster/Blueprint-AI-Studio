"""Focused LiveKit call-token boundary tests (no secret/token logging)."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.livekit_tokens import (
    LiveKitConfigurationError,
    issue_browser_call_credentials,
    issue_resident_call_credentials,
)


class _FakeGrants:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeAccessToken:
    last = None

    def __init__(self, key, secret):
        self.key = key
        self.secret = secret
        self.identity = None
        self.name = None
        self.ttl = None
        self.grants = None
        type(self).last = self

    def with_identity(self, value):
        self.identity = value
        return self

    def with_name(self, value):
        self.name = value
        return self

    def with_ttl(self, value):
        self.ttl = value
        return self

    def with_grants(self, value):
        self.grants = value
        return self

    def to_jwt(self):
        return "opaque-test-token"


@pytest.fixture
def livekit_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit.example.test")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "never-returned")
    fake = ModuleType("livekit")
    fake.api = SimpleNamespace(AccessToken=_FakeAccessToken, VideoGrants=_FakeGrants)
    monkeypatch.setitem(sys.modules, "livekit", fake)


def _call():
    return {
        "call_id": "call-opaque",
        "generation": 7,
        "worker_task_id": "resident-task",
        "actor_user_id": "operator-a",
    }


def test_browser_token_is_room_scoped_and_microphone_only(livekit_env):
    credentials = issue_browser_call_credentials(call=_call(), user_id="operator-a")

    assert credentials["server_url"] == "wss://livekit.example.test"
    assert credentials["room"] == "bp-call-call-opaque-7"
    assert credentials["participant_token"] == "opaque-test-token"
    assert "never-returned" not in credentials.values()
    assert "operator-a" not in credentials["participant_identity"]
    browser_grants = _FakeAccessToken.last.grants.kwargs
    assert credentials["agent_identity"] == issue_resident_call_credentials(
        call=_call(), task_id="resident-task"
    )["participant_identity"]
    assert browser_grants == {
        "room_join": True,
        "room": "bp-call-call-opaque-7",
        "can_publish": True,
        "can_subscribe": True,
        "can_publish_data": True,
        "can_publish_sources": ["microphone"],
    }


def test_resident_token_is_bound_to_task_call_and_generation(livekit_env):
    credentials = issue_resident_call_credentials(call=_call(), task_id="resident-task")

    assert credentials["room"] == "bp-call-call-opaque-7"
    assert "resident-task" not in credentials["participant_identity"]
    assert credentials["operator_identity"] == issue_browser_call_credentials(
        call=_call(), user_id="operator-a"
    )["participant_identity"]
    assert _FakeAccessToken.last.grants.kwargs["can_publish"] is True
    assert _FakeAccessToken.last.grants.kwargs["can_subscribe"] is True
    assert _FakeAccessToken.last.grants.kwargs["can_publish_data"] is True
    assert _FakeAccessToken.last.grants.kwargs["can_publish_sources"] == ["microphone"]


def test_partial_livekit_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit.example.test")
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)

    with pytest.raises(LiveKitConfigurationError, match="incompletely"):
        issue_browser_call_credentials(call=_call(), user_id="operator-a")


def test_missing_livekit_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)

    with pytest.raises(LiveKitConfigurationError, match="not configured"):
        issue_browser_call_credentials(call=_call(), user_id="operator-a")


@pytest.mark.parametrize(
    "server_url",
    [
        "ws://livekit.example.test",
        "https://livekit.example.test",
        "wss://user:secret@livekit.example.test",
        "wss://livekit.example.test?token=leak",
        "wss://livekit.example.test#fragment",
    ],
)
def test_livekit_endpoint_must_be_secure_and_credential_free(
    monkeypatch, server_url
):
    monkeypatch.setenv("LIVEKIT_URL", server_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-secret")

    with pytest.raises(LiveKitConfigurationError, match="credential-free wss"):
        issue_browser_call_credentials(call=_call(), user_id="operator-a")
