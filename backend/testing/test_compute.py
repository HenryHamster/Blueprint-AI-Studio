"""
Tests for app/compute.py

Covers:
- _store_agent_secret IAM policy ETag retry + idempotency.
- Zone success-rate tracking (rolling window, zone ordering, reset, summary).
"""
from collections import deque
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import uuid

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


def _make_client(existing_policy_bindings=None, set_iam_side_effects=None, payload: bytes | None = None):
    """Build a MagicMock SecretManagerServiceClient with sensible defaults.

    - create_secret returns an object whose .name looks like a real secret resource
    - add_secret_version returns an object whose .name looks like a version resource
    - get_iam_policy returns a fresh _FakePolicy with the given bindings on every call
    - set_iam_policy applies set_iam_side_effects in order (e.g. Aborted exceptions)
    - access_secret_version (audit E15: post-create verification) returns the
      payload that was supplied via ``payload=`` so the verify branch sees a
      match. Tests that don't pass payload= will get an empty bytes payload
      and trip the verification mismatch — which is the correct behavior to
      cover the failure path.
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

    # E15 verification path: read-back must match what was written.
    verify_obj = MagicMock()
    verify_obj.payload.data = payload if payload is not None else b""
    client.access_secret_version.return_value = verify_obj

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
        payload = '{"token": "abc"}'
        client = _make_client(set_iam_side_effects=side_effects, payload=payload.encode("utf-8"))

        with patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=client):
            result = compute._store_agent_secret("vm123", payload)

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
        payload = '{"token": "abc"}'
        client = _make_client(existing_policy_bindings=existing, payload=payload.encode("utf-8"))

        with patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=client):
            result = compute._store_agent_secret("vm123", payload)

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


class TestAgentSecretManifest:
    def test_build_agent_secret_manifest_includes_runtime_identity(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-123",
            task_id=task_id,
            oauth_token="sk-ant-test",
            task_type="create",
            provider="anthropic",
            auth_mode="oauth",
            model="claude-sonnet-4-6",
            execution_mode="advisor",
        )

        assert manifest["env"]["PROVIDER"] == "anthropic"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["LLM_MODEL"] == "claude-sonnet-4-6"
        assert manifest["env"]["LLM_EXECUTION_MODE"] == "advisor"
        assert manifest["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-test"
        assert manifest["env"]["TASK_ID"] == str(task_id)
        assert manifest["metadata"]["task_type"] == "create"
        assert manifest["files"] == []

    def test_build_agent_secret_manifest_materializes_file_based_provider_auth(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-openai",
            task_id=task_id,
            oauth_token='{"tokens":{"access_token":"abc"}}',
            task_type="create",
            provider="openai",
            auth_mode="oauth",
            model="gpt-5.4",
        )

        assert manifest["env"]["PROVIDER"] == "openai"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["CODEX_HOME"] == "/opt/agent/openai"
        assert manifest["files"] == [
            {
                "path": "/opt/agent/openai/auth.json",
                "content": '{"tokens":{"access_token":"abc"}}',
                "mode": "0600",
            }
        ]

    def test_build_agent_secret_manifest_includes_provider_metadata_backed_files(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-gemini",
            task_id=task_id,
            oauth_token='{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
            task_type="research",
            provider="gemini",
            auth_mode="service_account",
            model="gemini-2.5-pro",
            provider_metadata={"project": "demo-proj", "location": "us-central1"},
        )

        assert manifest["env"]["GOOGLE_APPLICATION_CREDENTIALS"] == "/opt/agent/gemini/sa.json"
        assert manifest["env"]["GOOGLE_CLOUD_PROJECT"] == "demo-proj"
        assert manifest["env"]["GOOGLE_CLOUD_LOCATION"] == "us-central1"
        assert manifest["files"][0]["path"] == "/opt/agent/gemini/sa.json"
        assert '"client_email":"bot@example.com"' in manifest["files"][0]["content"]


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


# ─── Spot preemption override tests ──────────────────────────────────────────

class TestSpotPreemptionOverride:
    """provision_agent_vm respects the provisioning_model override parameter.

    A task with spot_preemption_count >= 2 causes the orchestrator to pass
    provisioning_model="STANDARD" to provision_agent_vm, suppressing the SPOT
    scheduling block even when the process-wide VM_PROVISIONING_MODEL is "SPOT".
    """

    async def test_provisioning_model_override_passed_to_gcp(self, monkeypatch):
        """provision_agent_vm forwards provisioning_model="STANDARD" to _provision_gcp."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        captured = {}

        async def fake_gcp(*args, **kwargs):
            captured["provisioning_model"] = kwargs.get("provisioning_model")
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_gcp)

        await compute.provision_agent_vm(
            "vm-preempt-test", uuid.uuid4(), provisioning_model="STANDARD"
        )
        assert captured["provisioning_model"] == "STANDARD"

    async def test_provisioning_model_none_passes_none_to_gcp(self, monkeypatch):
        """When provisioning_model=None, None propagates so the global env drives it."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        captured = {}

        async def fake_gcp(*args, **kwargs):
            captured["provisioning_model"] = kwargs.get("provisioning_model")
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_gcp)

        await compute.provision_agent_vm("vm-spot-test", uuid.uuid4())
        assert captured["provisioning_model"] is None

    async def test_standard_override_wins_over_spot_global_env(self, monkeypatch):
        """STANDARD override beats VM_PROVISIONING_MODEL=SPOT (simulates 2nd preemption path).

        When spot_preemption_count >= 2, the orchestrator passes provisioning_model='STANDARD'
        to provision_agent_vm even though the process-wide env is SPOT.  This verifies
        the compute layer passes 'STANDARD' through to _provision_gcp in that scenario.
        """
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "SPOT")
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        captured = {}

        async def fake_gcp(*args, **kwargs):
            captured["provisioning_model"] = kwargs.get("provisioning_model")
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_gcp)

        await compute.provision_agent_vm("vm-2nd-preempt", uuid.uuid4(), provisioning_model="STANDARD")
        # STANDARD override must arrive at _provision_gcp even though global env is SPOT
        assert captured["provisioning_model"] == "STANDARD"

    def test_get_zone_health_summary_empty_rates_when_no_history(self, clean_zone_health):
        """zone_success_rates is empty when no zone has any history."""
        summary = compute.get_zone_health_summary()
        assert summary["zone_success_rates"] == {}


class TestZoneHealthRestoreDeque:
    """Tests for the recent_outcomes deque restoration on startup."""

    def _restore_zone_health(self, zone_health_state: dict) -> None:
        """Simulate the startup restore logic from main.py lifespan."""
        from collections import deque as _deque

        compute._zone_health.clear()
        for zone, data in zone_health_state.items():
            if not isinstance(data, dict):
                continue
            restored = {}
            for k, v in data.items():
                if isinstance(v, str) and k in ("last_success", "last_failure", "cooldown_expires_at"):
                    try:
                        restored[k] = datetime.fromisoformat(v)
                    except ValueError:
                        continue
                elif k == "recent_outcomes" and isinstance(v, list):
                    valid = [e for e in v if isinstance(e, bool)]
                    restored[k] = _deque(valid[-compute.ZONE_HISTORY_WINDOW:], maxlen=compute.ZONE_HISTORY_WINDOW)
                elif k == "consecutive_failures":
                    restored[k] = v if isinstance(v, int) else 0
                elif k == "last_failure_reason":
                    restored[k] = v if (v is None or isinstance(v, str)) else None
                else:
                    restored[k] = v
            compute._zone_health[zone] = restored

    def test_recent_outcomes_restored_as_deque(self):
        """recent_outcomes list from DB is converted to a deque with correct maxlen."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"consecutive_failures": 0, "recent_outcomes": [True, False, True]}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        assert outcomes.maxlen == compute.ZONE_HISTORY_WINDOW
        assert list(outcomes) == [True, False, True]

    def test_recent_outcomes_capped_to_window(self):
        """Persisted list longer than ZONE_HISTORY_WINDOW is truncated to the last N entries."""
        zone = "us-central1-f"
        oversized = [True] * 5 + [False] * (compute.ZONE_HISTORY_WINDOW + 5)
        self._restore_zone_health({zone: {"recent_outcomes": oversized}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        assert outcomes.maxlen == compute.ZONE_HISTORY_WINDOW
        assert len(outcomes) == compute.ZONE_HISTORY_WINDOW
        # Tail entries should all be False (the last ZONE_HISTORY_WINDOW elements)
        assert all(v is False for v in outcomes)

    def test_appending_beyond_window_evicts_old_entries(self):
        """After restore, appending beyond maxlen still evicts the oldest entry."""
        zone = "us-central1-f"
        initial = [True] * compute.ZONE_HISTORY_WINDOW
        self._restore_zone_health({zone: {"recent_outcomes": initial}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert len(outcomes) == compute.ZONE_HISTORY_WINDOW
        outcomes.append(False)  # Should evict oldest True
        assert len(outcomes) == compute.ZONE_HISTORY_WINDOW
        assert outcomes[-1] is False
        assert outcomes[0] is True  # remaining entries still True

    def test_missing_recent_outcomes_key_is_unaffected(self):
        """Zones without recent_outcomes in persisted data are restored normally."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"consecutive_failures": 2}})
        assert "recent_outcomes" not in compute._zone_health[zone]
        assert compute._zone_health[zone]["consecutive_failures"] == 2

    def test_recent_outcomes_corrupted_elements_filtered(self):
        """Non-boolean elements in recent_outcomes are dropped; valid booleans kept."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"recent_outcomes": [True, "bad", None, False, 1]}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        # Only the two actual booleans should survive
        assert list(outcomes) == [True, False]

    def test_recent_outcomes_fully_corrupt_yields_empty_deque(self):
        """Entirely non-boolean recent_outcomes list yields an empty deque (neutral rate)."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"recent_outcomes": ["bad", None, 0, 1]}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        assert len(outcomes) == 0
        # _zone_success_rate should return the neutral rate for empty deque
        assert compute._zone_success_rate(zone) == compute._ZONE_NEUTRAL_RATE

    def test_consecutive_failures_corrupted_defaults_to_zero(self):
        """Non-integer consecutive_failures falls back to 0."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"consecutive_failures": "bad"}})
        assert compute._zone_health[zone]["consecutive_failures"] == 0

    def test_last_failure_reason_corrupted_defaults_to_none(self):
        """Non-string last_failure_reason falls back to None."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"last_failure_reason": 42}})
        assert compute._zone_health[zone]["last_failure_reason"] is None

    def test_last_failure_reason_none_is_valid(self):
        """None is a valid value for last_failure_reason and is preserved."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"last_failure_reason": None}})
        assert compute._zone_health[zone]["last_failure_reason"] is None


class TestZoneCooldownSeconds:
    """Cover _zone_cooldown_seconds exponential backoff with 1800s cap."""

    def test_zero_failures_baseline(self):
        assert compute._zone_cooldown_seconds(0) == compute.ZONE_COOLDOWN_SECONDS

    def test_doubling_on_each_failure(self):
        base = compute.ZONE_COOLDOWN_SECONDS
        assert compute._zone_cooldown_seconds(1) == min(base * 2, 1800)
        assert compute._zone_cooldown_seconds(2) == min(base * 4, 1800)
        assert compute._zone_cooldown_seconds(3) == min(base * 8, 1800)

    def test_cap_at_1800_seconds(self):
        assert compute._zone_cooldown_seconds(10) == 1800
        assert compute._zone_cooldown_seconds(100) == 1800

    def test_monotonically_non_decreasing(self):
        prev = 0
        for n in range(0, 15):
            current = compute._zone_cooldown_seconds(n)
            assert current >= prev
            prev = current


class TestValidatePayloadUri:
    """Cover validate_payload_uri GCS URI format validation."""

    def test_valid_simple_uri(self):
        compute.validate_payload_uri("gs://bucket/object")

    def test_valid_nested_path(self):
        compute.validate_payload_uri("gs://my-bucket/path/to/file.json")

    def test_valid_with_allowed_special_chars(self):
        compute.validate_payload_uri("gs://bucket.name_1-2/a_b.c-d/e.json")

    @pytest.mark.parametrize("uri", [
        "",
        "   ",
        "http://bucket/object",
        "s3://bucket/object",
        "gs://bucket",
        "gs://bucket/",
        "gs:///object",
        "gs://bucket/object with spaces",
        "gs://bucket/obj#frag",
        "gs://bucket/obj?query",
        "gs://bucket/obj*",
        "/local/path",
    ])
    def test_invalid_uris_raise_value_error(self, uri):
        with pytest.raises(ValueError, match="Invalid payload_uri"):
            compute.validate_payload_uri(uri)


def _cooldown_all_zones(expires_at: datetime) -> None:
    """Mark every fallback zone as in cooldown with the given expiry."""
    for zone, _subnet in compute.FALLBACK_ZONES:
        compute._zone_health[zone] = {"cooldown_expires_at": expires_at}


class TestAnyZoneAvailable:
    """Tests for any_zone_available()."""

    def test_empty_health_dict_returns_true(self, clean_zone_health):
        assert compute.any_zone_available() is True

    def test_one_available_others_in_cooldown(self, clean_zone_health):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        # Clear cooldown on one zone
        compute._zone_health[compute.FALLBACK_ZONES[0][0]] = {"cooldown_expires_at": None}
        assert compute.any_zone_available() is True

    def test_all_zones_in_cooldown_returns_false(self, clean_zone_health):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        assert compute.any_zone_available() is False

    def test_cooldown_expired_returns_true(self, clean_zone_health):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        _cooldown_all_zones(past)
        assert compute.any_zone_available() is True


class TestSoonestZoneRecovery:
    """Tests for soonest_zone_recovery()."""

    def test_no_zones_in_health_returns_none(self, clean_zone_health):
        assert compute.soonest_zone_recovery() is None

    def test_one_zone_available_returns_none(self, clean_zone_health):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        compute._zone_health[compute.FALLBACK_ZONES[0][0]] = {"cooldown_expires_at": None}
        assert compute.soonest_zone_recovery() is None

    def test_all_in_cooldown_returns_earliest(self, clean_zone_health):
        now = datetime.now(timezone.utc)
        for idx, (zone, _subnet) in enumerate(compute.FALLBACK_ZONES):
            compute._zone_health[zone] = {
                "cooldown_expires_at": now + timedelta(minutes=10 + idx),
            }
        expected = now + timedelta(minutes=10)
        assert compute.soonest_zone_recovery() == expected

    def test_expired_cooldown_returns_none(self, clean_zone_health):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        _cooldown_all_zones(past)
        assert compute.soonest_zone_recovery() is None


class TestZoneInDisabledRegion:
    """Tests for _zone_in_disabled_region()."""

    def test_zone_in_disabled_region_extracts_region(self, monkeypatch):
        monkeypatch.setattr(compute, "_DISABLED_REGIONS", frozenset(["us-east1", "us-west1"]))
        assert compute._zone_in_disabled_region("us-east1-a") is True
        assert compute._zone_in_disabled_region("us-east1-c") is True
        assert compute._zone_in_disabled_region("us-west1-b") is True
        assert compute._zone_in_disabled_region("us-central1-f") is False
        assert compute._zone_in_disabled_region("us-east4-a") is False

    def test_zone_in_disabled_region_empty_set(self, monkeypatch):
        monkeypatch.setattr(compute, "_DISABLED_REGIONS", frozenset())
        for zone, _ in compute.FALLBACK_ZONES:
            assert compute._zone_in_disabled_region(zone) is False


class TestActiveFallbackZones:
    """Tests for _active_fallback_zones()."""

    def test_no_disabled_returns_full_list(self, monkeypatch):
        monkeypatch.setattr(compute, "_DISABLED_REGIONS", frozenset())
        assert compute._active_fallback_zones() == compute.FALLBACK_ZONES

    def test_filters_one_region(self, monkeypatch):
        monkeypatch.setattr(compute, "_DISABLED_REGIONS", frozenset(["us-east1"]))
        result = compute._active_fallback_zones()
        result_zones = [z for z, _ in result]
        assert "us-east1-c" not in result_zones
        assert "us-east1-b" not in result_zones
        assert "us-central1-a" in result_zones
        assert "us-east4-a" in result_zones  # us-east4 not disabled, still present
        assert len(result) == len(compute.FALLBACK_ZONES) - 2  # us-east1 had 2 zones

    def test_filters_three_regions_leaves_central(self, monkeypatch):
        monkeypatch.setattr(
            compute, "_DISABLED_REGIONS", frozenset(["us-east1", "us-east4", "us-west1"])
        )
        result = compute._active_fallback_zones()
        result_zones = [z for z, _ in result]
        assert all(z.startswith("us-central1-") for z in result_zones)
        assert len(result) == 4  # us-central1 has zones a, b, c, f


class TestFormatOrphanStatus:
    """Tests for _format_orphan_status()."""

    def test_terminated_in_spot_mode_annotated(self, monkeypatch):
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "SPOT")
        assert compute._format_orphan_status("TERMINATED") == "TERMINATED (likely spot preemption)"

    def test_terminated_in_standard_mode_passthrough(self, monkeypatch):
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "STANDARD")
        assert compute._format_orphan_status("TERMINATED") == "TERMINATED"

    def test_running_in_spot_mode_passthrough(self, monkeypatch):
        # Only TERMINATED gets the spot annotation; other statuses are
        # not preemption-implied.
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "SPOT")
        assert compute._format_orphan_status("RUNNING") == "RUNNING"
        assert compute._format_orphan_status("STOPPING") == "STOPPING"
        assert compute._format_orphan_status("PROVISIONING") == "PROVISIONING"


class TestDisabledRegionsParsing:
    """Tests for DISABLED_REGIONS env var parsing.

    The parser at module scope reads os.getenv once. We can't re-trigger that
    without importlib.reload, so we exercise the same logic inline.
    """

    @staticmethod
    def _parse(raw: str) -> frozenset[str]:
        # Mirror the production parser at compute.py:162-165.
        normalized = raw.replace(";", ",")
        return frozenset(r.strip() for r in normalized.split(",") if r.strip())

    def test_empty_string(self):
        assert self._parse("") == frozenset()

    def test_single_region(self):
        assert self._parse("us-east1") == frozenset({"us-east1"})

    def test_comma_separated(self):
        assert self._parse("us-east1,us-east4,us-west1") == frozenset(
            {"us-east1", "us-east4", "us-west1"}
        )

    def test_semicolon_separated(self):
        # Cloudbuild.yaml uses semicolons because gcloud --set-env-vars uses
        # `,` as KEY=VAL delimiter.
        assert self._parse("us-east1;us-east4;us-west1") == frozenset(
            {"us-east1", "us-east4", "us-west1"}
        )

    def test_mixed_separators(self):
        assert self._parse("us-east1;us-east4,us-west1") == frozenset(
            {"us-east1", "us-east4", "us-west1"}
        )

    def test_whitespace_normalized(self):
        assert self._parse(" us-east1 ; us-east4 ") == frozenset({"us-east1", "us-east4"})

    def test_trailing_separators_dropped(self):
        assert self._parse(";us-east1;;us-east4;") == frozenset({"us-east1", "us-east4"})

    def test_duplicate_collapsed(self):
        assert self._parse("us-east1,us-east1") == frozenset({"us-east1"})


class TestGetVmStatus:
    """Tests for get_vm_status() — used by reaper to detect spot preemption."""

    def test_zone_unknown_returns_none(self):
        assert compute.get_vm_status("agent-vm-abc", "unknown") is None
        assert compute.get_vm_status("agent-vm-abc", "") is None

    def test_running_status(self):
        from unittest.mock import MagicMock, patch
        mock_instance = MagicMock()
        mock_instance.status = "RUNNING"
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.return_value = mock_instance
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") == "RUNNING"

    def test_terminated_status(self):
        from unittest.mock import MagicMock, patch
        mock_instance = MagicMock()
        mock_instance.status = "TERMINATED"
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.return_value = mock_instance
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") == "TERMINATED"

    def test_not_found_returns_deleted(self):
        from unittest.mock import patch
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("404 was not found in zone")
            # SPOT VMs use DELETE termination action, so preempted VMs become 404.
            # get_vm_status returns the synthetic "DELETED" so reaper can distinguish
            # vanished from API-failed.
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") == "DELETED"

    def test_other_api_error_returns_none(self):
        from unittest.mock import patch
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("503 backend timeout")
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") is None
