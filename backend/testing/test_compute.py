"""
Tests for app/compute.py

Currently focused on _store_agent_secret IAM policy ETag retry + idempotency.
"""
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import Aborted

from app import compute


class _FakeBinding:
    """Mimics the subset of policy_pb2.Binding used by the idempotency check."""

    def __init__(self, role: str, members):
        self.role = role
        self.members = list(members)


class _FakePolicy:
    """Mimics the subset of the IAM policy proto used by _store_agent_secret."""

    def __init__(self, bindings=None):
        self.bindings = list(bindings or [])


def _make_client(existing_policy_bindings=None, set_iam_side_effects=None):
    """Build a MagicMock SecretManagerServiceClient with sensible defaults.

    - create_secret returns an object whose .name looks like a real secret resource
    - add_secret_version returns an object whose .name looks like a version resource
    - get_iam_policy returns a fresh _FakePolicy with the given bindings on every call
    - set_iam_policy applies set_iam_side_effects in order (e.g. Aborted exceptions)
    """
    client = MagicMock()
    secret_obj = MagicMock()
    secret_obj.name = "projects/test-proj/secrets/agent-creds-vm123"
    client.create_secret.return_value = secret_obj

    version_obj = MagicMock()
    version_obj.name = "projects/test-proj/secrets/agent-creds-vm123/versions/1"
    client.add_secret_version.return_value = version_obj

    # get_iam_policy returns a fresh policy each call so retries re-read it
    client.get_iam_policy.side_effect = lambda request: _FakePolicy(
        bindings=[_FakeBinding(b.role, b.members) for b in (existing_policy_bindings or [])]
    )

    if set_iam_side_effects is not None:
        client.set_iam_policy.side_effect = set_iam_side_effects

    return client


@pytest.fixture()
def patched_client():
    """Patch the secretmanager client factory inside _store_agent_secret.

    The real imports happen lazily inside the function, so we patch the module
    that gets imported at call time.
    """
    with patch("google.cloud.secretmanager.SecretManagerServiceClient") as factory:
        client = _make_client()
        factory.return_value = client
        yield client


class TestStoreAgentSecretIamRetry:
    """Retry + idempotency tests for _store_agent_secret IAM policy handling."""

    def test_retries_on_aborted_and_succeeds(self, monkeypatch):
        """set_iam_policy raises Aborted twice then succeeds; function returns version name.

        Expects the get/set read-modify-write loop to be retried with a fresh
        get_iam_policy each iteration (ETag-correct behavior).
        """
        # Don't actually sleep during retries
        monkeypatch.setattr(compute.time, "sleep", lambda _s: None)

        # First two set_iam_policy calls raise Aborted, third succeeds.
        side_effects = [
            Aborted("etag mismatch 1"),
            Aborted("etag mismatch 2"),
            None,
        ]
        client = _make_client(set_iam_side_effects=side_effects)

        with patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=client):
            result = compute._store_agent_secret("vm123", '{"token": "abc"}')

        assert result == "projects/test-proj/secrets/agent-creds-vm123/versions/1"
        assert client.set_iam_policy.call_count == 3
        # Each retry must re-read the policy to get a fresh ETag
        assert client.get_iam_policy.call_count == 3

    def test_idempotent_skip_when_sa_already_bound(self):
        """If the agent SA is already bound for secretAccessor, skip set_iam_policy.

        Prevents duplicate bindings when a secret is reused (e.g. AlreadyExists
        catch at the top of the function).
        """
        agent_member = f"serviceAccount:{compute.AGENT_SERVICE_ACCOUNT_EMAIL}"
        existing = [
            _FakeBinding(
                role="roles/secretmanager.secretAccessor",
                members=[agent_member],
            ),
        ]
        client = _make_client(existing_policy_bindings=existing)

        with patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=client):
            result = compute._store_agent_secret("vm123", '{"token": "abc"}')

        assert result == "projects/test-proj/secrets/agent-creds-vm123/versions/1"
        # Policy was read once to check the binding, then skipped.
        assert client.get_iam_policy.call_count == 1
        assert client.set_iam_policy.call_count == 0

    def test_aborted_exhausts_retries_and_raises(self, monkeypatch):
        """If Aborted fires max_retries (5) times, the final attempt re-raises Aborted."""
        monkeypatch.setattr(compute.time, "sleep", lambda _s: None)

        # Supply one more than max_retries (5) to prove we never exceed.
        side_effects = [Aborted(f"etag mismatch {i}") for i in range(6)]
        client = _make_client(set_iam_side_effects=side_effects)

        with patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=client):
            with pytest.raises(Aborted):
                compute._store_agent_secret("vm123", '{"token": "abc"}')

        assert client.set_iam_policy.call_count == 5
        assert client.get_iam_policy.call_count == 5
