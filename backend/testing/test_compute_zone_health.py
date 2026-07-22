"""
Zone health management and quota-scope tests for app/compute.py:
- Zone success-rate tracking (rolling window, zone ordering, reset, summary)
- Spot preemption override
- Quota-scope classification and backoff
- Disabled regions parsing
"""

import uuid
import pytest
from collections import deque
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from app import compute
from app import provision_error_scope


# ─── Zone success-rate tracking tests ────────────────────────────────────────


@pytest.fixture(autouse=False)
def clean_zone_health():
    """Reset zone health state before and after each test that uses this fixture."""

    def _reset():
        compute._zone_health.clear()
        compute._zone_failures.clear()
        provision_error_scope._global_quota_state.clear()
        provision_error_scope._region_quota_state.clear()
        compute._zone_suspect_events.clear()
        # GPU-isolated state
        compute._gpu_zone_health.clear()
        compute._gpu_zone_failures.clear()
        provision_error_scope._gpu_global_quota_state.clear()
        provision_error_scope._gpu_region_quota_state.clear()

    _reset()
    yield
    _reset()


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
            "recent_outcomes": deque(
                [False, False, False, True], maxlen=compute.ZONE_HISTORY_WINDOW
            ),
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

    def test_record_zone_failure_appends_false_when_authoritative(
        self, clean_zone_health
    ):
        """An authoritative zone-scoped reason appends False to recent_outcomes
        immediately (no corroboration needed)."""
        zone = "us-central1-f"
        compute.record_zone_failure(zone, reason="RESOURCE_POOL_EXHAUSTED")
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

        assert compute._zone_success_rate(good_zone) > compute._zone_success_rate(
            bad_zone
        )

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
            "recent_outcomes": deque(
                [True, True, False], maxlen=compute.ZONE_HISTORY_WINDOW
            ),
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
            "recent_outcomes": deque(
                [True, False, True, True], maxlen=compute.ZONE_HISTORY_WINDOW
            ),
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

        await compute.provision_agent_vm(
            "vm-2nd-preempt", uuid.uuid4(), provisioning_model="STANDARD"
        )
        # STANDARD override must arrive at _provision_gcp even though global env is SPOT
        assert captured["provisioning_model"] == "STANDARD"

    def test_get_zone_health_summary_empty_rates_when_no_history(
        self, clean_zone_health
    ):
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
                if isinstance(v, str) and k in (
                    "last_success",
                    "last_failure",
                    "cooldown_expires_at",
                ):
                    try:
                        restored[k] = datetime.fromisoformat(v)
                    except ValueError:
                        continue
                elif k == "recent_outcomes" and isinstance(v, list):
                    valid = [e for e in v if isinstance(e, bool)]
                    restored[k] = _deque(
                        valid[-compute.ZONE_HISTORY_WINDOW :],
                        maxlen=compute.ZONE_HISTORY_WINDOW,
                    )
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
        self._restore_zone_health(
            {zone: {"consecutive_failures": 0, "recent_outcomes": [True, False, True]}}
        )
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
        self._restore_zone_health(
            {zone: {"recent_outcomes": [True, "bad", None, False, 1]}}
        )
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

    def test_zero_failures_baseline(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
        assert compute._zone_cooldown_seconds(0) == compute.ZONE_COOLDOWN_SECONDS

    def test_doubling_on_each_failure(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
        base = compute.ZONE_COOLDOWN_SECONDS
        assert compute._zone_cooldown_seconds(1) == min(base * 2, 1800)
        assert compute._zone_cooldown_seconds(2) == min(base * 4, 1800)
        assert compute._zone_cooldown_seconds(3) == min(base * 8, 1800)

    def test_cap_at_1800_seconds(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
        assert compute._zone_cooldown_seconds(10) == 1800
        assert compute._zone_cooldown_seconds(100) == 1800

    def test_monotonically_non_decreasing(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
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

    @pytest.mark.parametrize(
        "uri",
        [
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
        ],
    )
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
        compute._zone_health[compute.FALLBACK_ZONES[0][0]] = {
            "cooldown_expires_at": None
        }
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
        compute._zone_health[compute.FALLBACK_ZONES[0][0]] = {
            "cooldown_expires_at": None
        }
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


class TestZoneRecoveryWaitSeconds:
    """Regression tests for zone_recovery_wait_seconds() (Audit C10).

    soonest_zone_recovery() returns a tz-AWARE datetime. The orchestrator
    previously subtracted the app's naive _utcnow() at two sites, which raised
    TypeError during zone exhaustion — silently rolling back the infra-requeue's
    QUEUED/retry_after state and spamming the all-zones-cooldown error counter.
    This helper centralises the aware subtraction so the math can't regress.
    """

    def test_none_when_zone_available(self, clean_zone_health):
        """No cooldown state → a zone is available → None (not 0)."""
        assert compute.zone_recovery_wait_seconds() is None

    def test_aware_cooldown_does_not_raise_typeerror(self, clean_zone_health):
        """All zones in cooldown with tz-AWARE expiries must NOT raise TypeError.

        Reproduces the C10 bug: _zone_health stores aware cooldown_expires_at
        (built from datetime.now(timezone.utc)), so any naive subtraction inside
        the helper would raise here. The pre-fix orchestrator code did exactly
        that. Asserts a sane positive wait instead.
        """
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        wait = compute.zone_recovery_wait_seconds()
        assert wait is not None
        # ~300s minus a few ms of test execution; must be positive and bounded.
        assert 240 < wait <= 300

    def test_floored_at_zero_for_just_expired(self, clean_zone_health):
        """A cooldown in the (very recent) past floors at 0, never negative.

        soonest_zone_recovery() returns None once any zone is past expiry, but
        guard the floor explicitly so a race between the two aware now() reads
        can never produce a negative wait.
        """
        # Set every zone to expire 1ms in the future so soonest_zone_recovery
        # still returns it, but by the time the helper subtracts now() it may be
        # in the past — the max(0.0, ...) floor must hold.
        near = datetime.now(timezone.utc) + timedelta(milliseconds=1)
        _cooldown_all_zones(near)
        wait = compute.zone_recovery_wait_seconds()
        # Either None (already expired by the time soonest_zone_recovery checks)
        # or a non-negative float — never negative, never a TypeError.
        assert wait is None or wait >= 0.0

    def test_orchestrator_imports_aware_helper(self):
        """The orchestrator must use zone_recovery_wait_seconds(), not its own
        naive subtraction. Guards against a future reintroduction of the C10
        naive/aware footgun at the dispatch / infra-requeue sites."""
        import app.orchestrator as _orch

        assert _orch.zone_recovery_wait_seconds is compute.zone_recovery_wait_seconds


# ─── Quota-scope classification (zone-failure misclassification fix) ─────────


# Classification matrix: (error text, expected scope)
_QUOTA_SCOPE_MATRIX = [
    # Global: the error text literally says "globally" / names an
    # ALL_REGIONS quota metric.
    (
        "403 QUOTA_EXCEEDED: Quota 'CPUS_ALL_REGIONS' exceeded. Limit: 32.0 globally.",
        provision_error_scope.QUOTA_SCOPE_GLOBAL,
    ),
    (
        "Quota 'GPUS_ALL_REGIONS' exceeded. Limit: 0.0 globally.",
        provision_error_scope.QUOTA_SCOPE_GLOBAL,
    ),
    (
        "quota 'cpus_all_regions' exceeded. limit: 32.0 globally.",
        provision_error_scope.QUOTA_SCOPE_GLOBAL,
    ),
    ("Quota exceeded. Limit: 64.0 globally.", provision_error_scope.QUOTA_SCOPE_GLOBAL),
    # Regional: region-scoped quota metrics.
    (
        "403 QUOTA_EXCEEDED: Quota 'CPUS' exceeded. Limit: 24.0 in region us-central1.",
        provision_error_scope.QUOTA_SCOPE_REGIONAL,
    ),
    (
        "Quota 'IN_USE_ADDRESSES' exceeded. Limit: 8.0 in region us-east1.",
        provision_error_scope.QUOTA_SCOPE_REGIONAL,
    ),
    # Unscoped quota wording defaults to regional (the conservative middle
    # ground: never paints other regions, never silently pauses the project).
    ("QUOTA_EXCEEDED: quota exceeded", provision_error_scope.QUOTA_SCOPE_REGIONAL),
    # Zonal: genuine zone stockouts / shortages.
    (
        "ZONE_RESOURCE_POOL_EXHAUSTED: The zone 'projects/p/zones/us-central1-f' "
        "does not have enough resources available to fulfill the request.",
        provision_error_scope.QUOTA_SCOPE_ZONAL,
    ),
    (
        "The zone 'projects/p/zones/us-east4-b' does not have enough resources "
        "available to fulfill the request. Try a different zone.",
        provision_error_scope.QUOTA_SCOPE_ZONAL,
    ),
    # Non-quota retryables (subnet missing, etc.) stay zone-scoped.
    (
        "Invalid value for field 'subnetwork': resource was not found",
        provision_error_scope.QUOTA_SCOPE_ZONAL,
    ),
    ("", provision_error_scope.QUOTA_SCOPE_ZONAL),
]


class TestQuotaScopeClassification:
    """Matrix tests for classify_provision_error_scope().

    A single CPUS_ALL_REGIONS exhaustion is a PROJECT-GLOBAL condition; the
    old classifier treated any error containing QUOTA as a zone failure and
    cycled every enabled zone into cooldown.
    """

    @pytest.mark.parametrize("err_str,expected", _QUOTA_SCOPE_MATRIX)
    def test_classification_matrix(self, err_str, expected):
        assert provision_error_scope.classify_provision_error_scope(err_str) == expected

    def test_zonal_marker_wins_over_generic_quota_wording(self):
        """A decisive zone-stockout marker stays zonal even if the message also
        mentions quota somewhere."""
        err = (
            "ZONE_RESOURCE_POOL_EXHAUSTED: zone does not have enough resources; "
            "see quota documentation"
        )
        assert (
            provision_error_scope.classify_provision_error_scope(err)
            == provision_error_scope.QUOTA_SCOPE_ZONAL
        )


_GLOBAL_QUOTA_ERR = (
    "403 QUOTA_EXCEEDED: Quota 'CPUS_ALL_REGIONS' exceeded. Limit: 32.0 globally."
)
_REGIONAL_QUOTA_ERR = (
    "403 QUOTA_EXCEEDED: Quota 'IN_USE_ADDRESSES' exceeded. "
    "Limit: 8.0 in region us-central1."
)


class TestGlobalQuotaBackoff:
    """Project-level backoff for global quota exhaustion — NO zone marks."""

    def test_record_sets_cooldown_and_marks_no_zones(self, clean_zone_health):
        cooldown_secs = provision_error_scope._record_global_quota_exhaustion(
            _GLOBAL_QUOTA_ERR
        )
        assert cooldown_secs == provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS
        expires = provision_error_scope._global_quota_state["cooldown_expires_at"]
        assert expires > datetime.now(timezone.utc)
        # The whole point: no zone is charged.
        assert compute._zone_health == {}
        assert compute._zone_failures == {}

    def test_global_cooldown_blocks_every_zone(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert compute.any_zone_available() is False
        recovery = compute.soonest_zone_recovery()
        assert (
            recovery == provision_error_scope._global_quota_state["cooldown_expires_at"]
        )
        wait = compute.zone_recovery_wait_seconds()
        assert wait is not None and wait > 0

    def test_cooldown_expiry_restores_availability(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._global_quota_state["cooldown_expires_at"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
        assert compute.any_zone_available() is True

    def test_parallel_failures_debounced(self, clean_zone_health):
        """Concurrent provision attempts failing on the same quota event must
        not escalate the backoff (mirrors the per-zone debounce)."""
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert provision_error_scope._global_quota_state["consecutive"] == 1

    def test_distinct_events_escalate_exponentially(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        # Age the last event past the debounce window.
        provision_error_scope._global_quota_state["last_event"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS + 1)
        cooldown_secs = provision_error_scope._record_global_quota_exhaustion(
            _GLOBAL_QUOTA_ERR
        )
        assert provision_error_scope._global_quota_state["consecutive"] == 2
        assert cooldown_secs == min(
            provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS * 2,
            provision_error_scope._QUOTA_COOLDOWN_CAP_SECONDS,
        )

    def test_backoff_capped(self):
        assert (
            provision_error_scope._quota_backoff_seconds(
                100, provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS
            )
            == provision_error_scope._QUOTA_COOLDOWN_CAP_SECONDS
        )

    def test_operator_alert_fired_and_deduplicated(
        self, clean_zone_health, monkeypatch
    ):
        alerts = []
        monkeypatch.setattr(
            provision_error_scope,
            "_post_global_quota_alert",
            lambda err, secs, consec: alerts.append((err, secs, consec)),
        )
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert len(alerts) == 1
        # A storm of failures within the dedup window must not spam the operator.
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert len(alerts) == 1
        # Past the dedup window the alert re-fires.
        provision_error_scope._global_quota_state["last_alert_at"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=provision_error_scope._QUOTA_ALERT_DEDUP_SECONDS + 1)
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert len(alerts) == 2

    def test_success_clears_global_cooldown(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._clear_quota_cooldowns_after_success("us-central1-f")
        assert provision_error_scope._global_quota_state == {}
        assert compute.any_zone_available() is True

    def test_summary_surfaces_global_cooldown(self, clean_zone_health):
        summary = compute.get_zone_health_summary()
        assert summary["global_quota_cooldown_until"] is None
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        summary = compute.get_zone_health_summary()
        assert summary["global_quota_cooldown_until"] is not None


class TestRegionQuotaBackoff:
    """Region-level backoff for regional quota exhaustion — blocks only that
    region's zones, marks no individual zone."""

    def test_record_blocks_only_that_region(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        now = datetime.now(timezone.utc)
        assert compute._zone_blocked_until("us-central1-f", now) is not None
        assert compute._zone_blocked_until("us-central1-a", now) is not None
        assert compute._zone_blocked_until("us-east4-b", now) is None
        assert compute._zone_blocked_until("us-west1-a", now) is None
        # Other regions keep the fleet available.
        assert compute.any_zone_available() is True
        # And no zone was charged.
        assert compute._zone_health == {}

    def test_all_regions_blocked_means_nothing_available(self, clean_zone_health):
        regions = {
            provision_error_scope._zone_region(z) for z, _ in compute.FALLBACK_ZONES
        }
        for region in regions:
            provision_error_scope._record_region_quota_exhaustion(
                region, _REGIONAL_QUOTA_ERR
            )
        assert compute.any_zone_available() is False

    def test_success_in_region_clears_its_cooldown_only(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        provision_error_scope._record_region_quota_exhaustion(
            "us-east4", _REGIONAL_QUOTA_ERR
        )
        provision_error_scope._clear_quota_cooldowns_after_success("us-central1-f")
        assert "us-central1" not in provision_error_scope._region_quota_state
        assert "us-east4" in provision_error_scope._region_quota_state

    def test_summary_surfaces_region_cooldowns(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        summary = compute.get_zone_health_summary()
        assert "us-central1" in summary["region_quota_cooldowns"]

    def test_region_debounce_and_escalation(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        assert (
            provision_error_scope._region_quota_state["us-central1"]["consecutive"] == 1
        )
        provision_error_scope._region_quota_state["us-central1"]["last_event"] = (
            datetime.now(timezone.utc)
            - timedelta(seconds=provision_error_scope.REGION_QUOTA_COOLDOWN_SECONDS + 1)
        )
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        assert (
            provision_error_scope._region_quota_state["us-central1"]["consecutive"] == 2
        )


class TestZoneSuspectCorroboration:
    """record_zone_failure() must not charge a zone for a single silent VM
    death (HEARTBEAT_STARVATION / AGENT_STARTUP_FAILURE / …) — those are
    usually VM/image/startup-script failures, not zone health. Charging
    requires corroboration: N reports in the same zone within the window."""

    def test_single_vm_death_does_not_charge_zone(self, clean_zone_health):
        zone = "us-central1-f"
        compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        assert zone not in compute._zone_health
        assert zone not in compute._zone_failures
        assert len(compute._zone_suspect_events[zone]) == 1
        assert compute.any_zone_available() is True

    def test_below_threshold_reports_do_not_charge(self, clean_zone_health):
        zone = "us-central1-f"
        for _ in range(compute.ZONE_SUSPECT_CORROBORATION_THRESHOLD - 1):
            compute.record_zone_failure(zone, reason="AGENT_STARTUP_FAILURE")
        assert zone not in compute._zone_health

    def test_corroborated_reports_charge_zone_once(self, clean_zone_health):
        zone = "us-central1-f"
        for _ in range(compute.ZONE_SUSPECT_CORROBORATION_THRESHOLD):
            compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        health = compute._zone_health[zone]
        assert health["consecutive_failures"] == 1
        assert health["cooldown_expires_at"] > datetime.now(timezone.utc)
        assert health["last_failure_reason"] == "HEARTBEAT_STARVATION_CORROBORATED"
        # Exactly one success-rate hit for the whole corroborated event.
        assert list(health["recent_outcomes"]) == [False]
        # Evidence consumed: the next charge needs fresh corroboration.
        assert len(compute._zone_suspect_events[zone]) == 0

    def test_reports_outside_window_expire(self, clean_zone_health):
        zone = "us-central1-f"
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=compute.ZONE_SUSPECT_WINDOW_SECONDS + 60
        )
        events = compute._zone_suspect_events.setdefault(zone, deque(maxlen=64))
        for _ in range(compute.ZONE_SUSPECT_CORROBORATION_THRESHOLD - 1):
            events.append(stale)
        compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        # Stale evidence was pruned, so this is report 1/N — no charge.
        assert zone not in compute._zone_health
        assert len(compute._zone_suspect_events[zone]) == 1

    def test_reports_isolated_per_zone(self, clean_zone_health):
        """Deaths spread across different zones must not corroborate each other."""
        for zone in ("us-central1-f", "us-central1-c", "us-east4-b"):
            compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        assert compute._zone_health == {}

    def test_authoritative_reason_charges_immediately(self, clean_zone_health):
        zone = "us-central1-f"
        compute.record_zone_failure(zone, reason="RESOURCE_POOL_EXHAUSTED")
        health = compute._zone_health[zone]
        assert health["consecutive_failures"] == 1
        assert health["cooldown_expires_at"] > datetime.now(timezone.utc)
        assert health["last_failure_reason"] == "RESOURCE_POOL_EXHAUSTED"


class TestProvisionQuotaScope:
    """Integration: the _provision_gcp zone loop must not paint zones on
    global/regional quota errors."""

    @pytest.fixture()
    def _provision_env(self, monkeypatch):
        async def _ok(*args, **kwargs):
            return True

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(compute, "_acquire_provision_slot", _ok)
        monkeypatch.setattr(compute, "_release_provision_slot", _noop)
        monkeypatch.setattr(compute, "_delete_agent_secret", lambda vm_id: None)

    async def test_global_quota_error_marks_no_zone_and_stops_cycling(
        self, clean_zone_health, _provision_env
    ):
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = Exception(_GLOBAL_QUOTA_ERR)

            result = await compute._provision_gcp(
                "vm-global-quota-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is False
        # One insert only — cycling to the next zone would re-trip the same
        # project-global quota in every remaining zone.
        assert mock_client.return_value.insert.call_count == 1
        # NO zone marks: the old behavior painted every enabled zone failed.
        assert compute._zone_health == {}
        assert compute._zone_failures == {}
        # Project-level backoff armed instead.
        assert "cooldown_expires_at" in provision_error_scope._global_quota_state
        assert compute.any_zone_available() is False
        # Error surfaced for the reaper's INFRA_PROVISION_PATTERNS classifier.
        err = compute.get_provision_error("vm-global-quota-test")
        assert "quota" in err.lower()

    async def test_provision_fails_fast_while_global_cooldown_active(
        self, clean_zone_health, _provision_env
    ):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            result = await compute._provision_gcp(
                "vm-cooldown-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )
        assert result is False
        mock_client.return_value.insert.assert_not_called()
        err = compute.get_provision_error("vm-cooldown-test")
        assert "quota" in err.lower()

    async def test_regional_quota_skips_region_and_succeeds_elsewhere(
        self, clean_zone_health, _provision_env
    ):
        # Make us-central1-f sort first (perfect success history).
        compute._zone_health["us-central1-f"] = {
            "recent_outcomes": deque([True] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        attempted = []

        def _insert(*args, **kwargs):
            zone = kwargs["zone"]
            attempted.append(zone)
            if zone.startswith("us-central1"):
                raise Exception(_REGIONAL_QUOTA_ERR)
            op = MagicMock()
            op.name = "op-regional-test"
            return op

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = _insert

            result = await compute._provision_gcp(
                "vm-regional-quota-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is True
        # First attempt hit the regional quota; the OTHER us-central1 zones
        # were skipped (region cooldown), and the very next attempt was in a
        # different region.
        assert attempted[0] == "us-central1-f"
        assert len(attempted) == 2
        assert not attempted[1].startswith("us-central1")
        # The exhausted region is in cooldown; the failing zone was NOT charged.
        assert "us-central1" in provision_error_scope._region_quota_state
        f_health = compute._zone_health.get("us-central1-f", {})
        assert f_health.get("cooldown_expires_at") is None
        assert f_health.get("consecutive_failures", 0) == 0
        assert list(f_health.get("recent_outcomes", [])) == [True] * 10


class TestZoneInDisabledRegion:
    """Tests for _zone_in_disabled_region()."""

    def test_zone_in_disabled_region_extracts_region(self, monkeypatch):
        monkeypatch.setattr(
            compute, "_DISABLED_REGIONS", frozenset(["us-east1", "us-west1"])
        )
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
            compute,
            "_DISABLED_REGIONS",
            frozenset(["us-east1", "us-east4", "us-west1"]),
        )
        result = compute._active_fallback_zones()
        result_zones = [z for z, _ in result]
        assert all(z.startswith("us-central1-") for z in result_zones)
        assert len(result) == 4  # us-central1 has zones a, b, c, f


class TestFormatOrphanStatus:
    """Tests for _format_orphan_status()."""

    def test_terminated_in_spot_mode_annotated(self, monkeypatch):
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "SPOT")
        assert (
            compute._format_orphan_status("TERMINATED")
            == "TERMINATED (likely spot preemption)"
        )

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
        assert self._parse(" us-east1 ; us-east4 ") == frozenset(
            {"us-east1", "us-east4"}
        )

    def test_trailing_separators_dropped(self):
        assert self._parse(";us-east1;;us-east4;") == frozenset(
            {"us-east1", "us-east4"}
        )

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
            assert (
                compute.get_vm_status("agent-vm-abc", "us-central1-a") == "TERMINATED"
            )

    def test_not_found_returns_deleted(self):
        from unittest.mock import patch

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception(
                "404 was not found in zone"
            )
            # SPOT VMs use DELETE termination action, so preempted VMs become 404.
            # get_vm_status returns the synthetic "DELETED" so reaper can distinguish
            # vanished from API-failed.
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") == "DELETED"

    def test_other_api_error_returns_none(self):
        from unittest.mock import patch

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("503 backend timeout")
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") is None


class TestBootDiskType:
    """provision_agent_vm requests a pd-balanced (SSD-backed) boot disk.

    pd-balanced boots meaningfully faster than the pd-standard (HDD) default
    GCP uses when disk_type is unset. disk_type is zone-qualified, so the value
    sent to insert() must reference the same zone the instance is created in.
    """

    async def test_disk_type_is_pd_balanced_and_zone_matched(
        self, monkeypatch, clean_zone_health
    ):
        captured = {}

        def _capture_insert(*args, **kwargs):
            inst = kwargs["instance_resource"]
            captured["zone"] = kwargs["zone"]
            captured["disk_type"] = inst.disks[0].initialize_params.disk_type
            captured["machine_type"] = inst.machine_type
            op = MagicMock()
            op.name = "op-disk-test"
            return op

        async def _ok(*args, **kwargs):
            return True

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(compute, "_acquire_provision_slot", _ok)
        monkeypatch.setattr(compute, "_release_provision_slot", _noop)

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            # No stale VM to clean up — get() raising 404 short-circuits the delete.
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = _capture_insert

            result = await compute._provision_gcp(
                "vm-disktype-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is True
        assert (
            captured["disk_type"] == f"zones/{captured['zone']}/diskTypes/pd-balanced"
        )
        # Disk zone must track the machine-type zone (same insert target).
        assert f"zones/{captured['zone']}/" in captured["machine_type"]

    async def test_ssd_quota_retries_same_zone_with_pd_standard(
        self, monkeypatch, clean_zone_health
    ):
        inserts = []

        def _capture_insert(*args, **kwargs):
            inst = kwargs["instance_resource"]
            inserts.append(
                {
                    "zone": kwargs["zone"],
                    "disk_type": inst.disks[0].initialize_params.disk_type,
                }
            )
            op = MagicMock()
            op.name = f"op-disk-fallback-{len(inserts)}"
            if len(inserts) == 1:
                op.result.side_effect = Exception(
                    "403 FORBIDDEN QUOTA_EXCEEDED: Quota 'SSD_TOTAL_GB' exceeded"
                )
            return op

        async def _ok(*args, **kwargs):
            return True

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(compute, "_acquire_provision_slot", _ok)
        monkeypatch.setattr(compute, "_release_provision_slot", _noop)

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = _capture_insert

            result = await compute._provision_gcp(
                "vm-disktype-fallback-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is True
        assert len(inserts) == 2
        assert inserts[0]["zone"] == inserts[1]["zone"]
        assert inserts[0]["disk_type"].endswith("/diskTypes/pd-balanced")
        assert inserts[1]["disk_type"].endswith("/diskTypes/pd-standard")


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Quota 'SSD_TOTAL_GB' exceeded", True),
        ("quota SSD-TOTAL-GB exhausted", True),
        ("Quota CPUS exceeded", False),
        ("ZONE_RESOURCE_POOL_EXHAUSTED", False),
    ],
)
def test_is_ssd_boot_disk_quota_exhaustion(message, expected):
    assert compute._is_ssd_boot_disk_quota_exhaustion(message) is expected


