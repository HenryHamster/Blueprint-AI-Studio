"""Focused server-side speech credential boundary tests."""

from types import SimpleNamespace

import pytest

from app import voice_credentials
from app.routers import runner as runner_router
from app import shared_state


def _task(*, resident: bool, task_type: str = "strategic_review"):
    return SimpleNamespace(
        first_payload={"type": task_type},
        context={"_resident_session": True} if resident else {},
    )


def _clear_voice_configuration(monkeypatch):
    for key in voice_credentials._VOICE_CONFIGURATION_KEYS:
        monkeypatch.delenv(key, raising=False)


def _configure_voice(monkeypatch):
    monkeypatch.setenv(
        "CALL_DEEPGRAM_API_KEY_SECRET_VERSION",
        "projects/test/secrets/deepgram/versions/7",
    )
    monkeypatch.setenv(
        "CALL_CARTESIA_API_KEY_SECRET_VERSION",
        "projects/test/secrets/cartesia/versions/3",
    )
    monkeypatch.setenv("CALL_DEEPGRAM_STT_MODEL", "flux-general-en")
    monkeypatch.setenv("CALL_CARTESIA_TTS_MODEL", "sonic-3")
    monkeypatch.setenv("CALL_CARTESIA_VOICE_ID", "voice-id")


def test_call_deployment_configuration_requires_exact_foreground_attestation(
    monkeypatch,
):
    _configure_voice(monkeypatch)
    monkeypatch.delenv("CALL_FOREGROUND_QUALIFICATION", raising=False)
    with pytest.raises(
        voice_credentials.VoiceCredentialConfigurationError,
        match="not configured",
    ):
        voice_credentials.validate_resident_call_deployment_configuration()

    monkeypatch.setenv("CALL_FOREGROUND_QUALIFICATION", "unreviewed")
    with pytest.raises(
        voice_credentials.VoiceCredentialConfigurationError,
        match="not qualification-approved",
    ):
        voice_credentials.validate_resident_call_deployment_configuration()

    monkeypatch.setenv(
        "CALL_FOREGROUND_QUALIFICATION",
        voice_credentials.FOREGROUND_QUALIFICATION_CONTRACT,
    )
    assert (
        voice_credentials.validate_resident_call_deployment_configuration()
        == voice_credentials.FOREGROUND_QUALIFICATION_CONTRACT
    )


@pytest.mark.asyncio
async def test_unconfigured_voice_does_not_block_resident_workday(monkeypatch):
    _clear_voice_configuration(monkeypatch)

    async def unexpected_secret_access(_):  # pragma: no cover - assertion below
        raise AssertionError("unconfigured Workday must not read Secret Manager")

    monkeypatch.setattr(voice_credentials, "get_user_secret", unexpected_secret_access)

    assert await voice_credentials.resolve_resident_call_speech_credentials(
        _task(resident=True)
    ) == {}


@pytest.mark.asyncio
async def test_nonresident_never_reads_or_receives_speech_credentials(monkeypatch):
    _configure_voice(monkeypatch)
    calls = []

    async def secret_access(ref):
        calls.append(ref)
        return "not-returned"

    monkeypatch.setattr(voice_credentials, "get_user_secret", secret_access)

    assert await voice_credentials.resolve_resident_call_speech_credentials(
        _task(resident=False)
    ) == {}
    assert await voice_credentials.resolve_resident_call_speech_credentials(
        _task(resident=True, task_type="research")
    ) == {}
    assert calls == []


@pytest.mark.asyncio
async def test_runner_dispatch_never_includes_call_speech_credentials(monkeypatch):
    """The persistent Workday process fetches call credentials only on demand."""
    _configure_voice(monkeypatch)
    monkeypatch.setattr(shared_state, "runtime_config", {})

    ok, resolved, reason, code = await runner_router._resolve_pool_credentials_for_dispatch(
        _task(resident=True), session=None, runner=None
    )

    assert ok is True
    assert resolved is not None and resolved.credentials is None
    assert reason is None
    assert code is None


@pytest.mark.asyncio
async def test_configured_resident_receives_exact_vended_speech_envelope(monkeypatch):
    _configure_voice(monkeypatch)

    async def secret_access(ref):
        return {
            "projects/test/secrets/deepgram/versions/7": "deepgram-secret",
            "projects/test/secrets/cartesia/versions/3": "cartesia-secret",
        }[ref]

    monkeypatch.setattr(voice_credentials, "get_user_secret", secret_access)

    env = await voice_credentials.resolve_resident_call_speech_credentials(
        _task(resident=True)
    )

    assert env == {
        "CALL_DEEPGRAM_STT_MODEL": "flux-general-en",
        "CALL_CARTESIA_TTS_MODEL": "sonic-3",
        "CALL_CARTESIA_VOICE_ID": "voice-id",
        "CALL_DEEPGRAM_API_KEY": "deepgram-secret",
        "CALL_CARTESIA_API_KEY": "cartesia-secret",
    }


@pytest.mark.asyncio
async def test_partial_voice_configuration_fails_closed(monkeypatch):
    _clear_voice_configuration(monkeypatch)
    monkeypatch.setenv(
        "CALL_DEEPGRAM_API_KEY_SECRET_VERSION",
        "projects/test/secrets/deepgram/versions/7",
    )

    async def unexpected_secret_access(_):  # pragma: no cover - assertion below
        raise AssertionError("partial configuration must fail before secret access")

    monkeypatch.setattr(voice_credentials, "get_user_secret", unexpected_secret_access)

    with pytest.raises(
        voice_credentials.VoiceCredentialConfigurationError,
        match="not configured",
    ):
        await voice_credentials.resolve_resident_call_speech_credentials(
            _task(resident=True)
        )


@pytest.mark.asyncio
async def test_unreadable_configured_secret_fails_closed(monkeypatch):
    _configure_voice(monkeypatch)

    async def empty_secret(_):
        return ""

    monkeypatch.setattr(voice_credentials, "get_user_secret", empty_secret)

    with pytest.raises(
        voice_credentials.VoiceCredentialConfigurationError,
        match="unavailable",
    ):
        await voice_credentials.resolve_resident_call_speech_credentials(
            _task(resident=True)
        )
