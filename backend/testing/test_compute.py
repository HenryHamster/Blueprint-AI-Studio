"""
Tests for app/compute.py

Covers:
- _store_agent_secret IAM policy ETag retry + idempotency.
- Zone success-rate tracking (rolling window, zone ordering, reset, summary).
"""
from collections import deque
from datetime import datetime, timezone, timedelta
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


# ─── Zone success-rate tracking tests ────────────────────────────────────────

@pytest.fixture(autouse=False)
def clean_zone_health():
    """Reset zone health state before and after each test that uses this fixture."""
    compute._zone_health.clear()
    compute._zone_failures.clear()
    yield
    compute._zone_health.clear()
    compute._zone_failures.clear()


class TestZoneSuccessRate:
    """Tests for _zone_success_rate helper and rolling-window tracking."""

    def test_no_history_returns_neutral(self, clean_zone_health):
        """A zone with no history returns the neutral rate (0.5)."""
        rate = compute._zone_success_rate("us-central1-f")
        assert rate == compute._ZONE_NEUTRAL_RATE

    def test_all_successes(self, clean_zone_health):
        """After N successes the success rate is 1.0."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "recent_outcomes": deque([True] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        assert compute._zone_success_rate(zone) == 1.0

    def test_all_failures(self, clean_zone_health):
        """After N failures the success rate is 0.0."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "recent_outcomes": deque([False] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        assert compute._zone_success_rate(zone) == 0.0

    def test_mixed_rate(self, clean_zone_health):
        """3 failures + 1 success out of 4 provisions → 25% success rate."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "recent_outcomes": deque([False, False, False, True], maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        assert compute._zone_success_rate(zone) == pytest.approx(0.25)

    def test_rolling_window_evicts_old_entries(self, clean_zone_health):
        """After ZONE_HISTORY_WINDOW provisions the oldest entries are evicted."""
        zone = "us-central1-f"
        window = compute.ZONE_HISTORY_WINDOW
        # Fill window with failures, then add one success past the limit
        outcomes: deque = deque(maxlen=window)
        for _ in range(window):
            outcomes.append(False)
        outcomes.append(True)  # Pushes out the oldest False

        compute._zone_health[zone] = {"recent_outcomes": outcomes}
        # Window still has `window` entries; one success + (window-1) failures
        rate = compute._zone_success_rate(zone)
        assert rate == pytest.approx(1.0 / window)

    def test_record_zone_failure_appends_false(self, clean_zone_health):
        """record_zone_failure() appends False to recent_outcomes."""
        zone = "us-central1-f"
        compute.record_zone_failure(zone, reason="TEST")
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert list(outcomes) == [False]

    def test_rate_reset_after_extended_healthy_period(self, clean_zone_health):
        """After the rolling window fills with successes, old failures are gone."""
        zone = "us-central1-f"
        window = compute.ZONE_HISTORY_WINDOW
        # Start with half failures
        outcomes: deque = deque(maxlen=window)
        for _ in range(window // 2):
            outcomes.append(False)
        compute._zone_health[zone] = {"recent_outcomes": outcomes}

        # Simulate window of successful provisions — each pushes out an old failure
        for _ in range(window):
            outcomes.append(True)

        assert compute._zone_success_rate(zone) == 1.0


class TestZoneOrdering:
    """Tests for zone selection ordering by success rate."""

    def test_high_success_rate_zone_preferred(self, clean_zone_health):
        """A zone with a high success rate sorts above one with a low rate."""
        good_zone = "us-central1-f"
        bad_zone = "us-central1-c"

        compute._zone_health[good_zone] = {
            "recent_outcomes": deque([True] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        compute._zone_health[bad_zone] = {
            "recent_outcomes": deque([False] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }

        assert compute._zone_success_rate(good_zone) > compute._zone_success_rate(bad_zone)

    def test_no_history_zone_gets_neutral_rate(self, clean_zone_health):
        """A zone with no history is treated as neutral (0.5), not pessimistic."""
        assert compute._zone_success_rate("us-east4-b") == 0.5


class TestZoneHealthOutput:
    """Tests for get_zone_health() and get_zone_health_summary() success_rate fields."""

    def test_get_zone_health_includes_success_rate(self, clean_zone_health):
        """get_zone_health() includes success_rate and provision_count per zone."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "last_success": datetime.now(timezone.utc),
            "consecutive_failures": 0,
            "recent_outcomes": deque([True, True, False], maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        result = compute.get_zone_health()
        assert zone in result
        assert result[zone]["success_rate"] == pytest.approx(2 / 3)
        assert result[zone]["provision_count"] == 3

    def test_get_zone_health_no_history_has_none_rate(self, clean_zone_health):
        """Zones with no history report success_rate=None and provision_count=0."""
        result = compute.get_zone_health()
        for zone_data in result.values():
            assert zone_data["success_rate"] is None
            assert zone_data["provision_count"] == 0

    def test_get_zone_health_summary_includes_success_rates(self, clean_zone_health):
        """get_zone_health_summary() includes zone_success_rates for zones with history."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "consecutive_failures": 0,
            "recent_outcomes": deque([True, False, True, True], maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        summary = compute.get_zone_health_summary()
        assert "zone_success_rates" in summary
        assert zone in summary["zone_success_rates"]
        assert summary["zone_success_rates"][zone] == pytest.approx(0.75)

    def test_get_zone_health_summary_empty_rates_when_no_history(self, clean_zone_health):
        """zone_success_rates is empty when no zone has any history."""
        summary = compute.get_zone_health_summary()
        assert summary["zone_success_rates"] == {}
