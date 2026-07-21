"""Tests for usage-meter serialization and reliable metering fixes.

Covers:
- UsageSnapshot.to_dict() / from_dict() round-trip serialization
- probed_at preserved end-to-end through persist/restore cycle
- Extra windows (beyond five_hour/seven_day) preserved
- Backwards-compat with old blobs lacking 'known'/'probed_at' fields
- _snapshot_from_persisted_usage delegates to from_dict correctly
- Dark-meter warning logged for OAuth accounts with missing secrets
"""

from datetime import datetime, timedelta

import pytest

from app.providers.base import UsageSnapshot
from app.max_plan_pool import (
    _account_by_id,
    _persist_rate_limit_usage_to_db,
    _snapshot_from_persisted_usage,
    get_usage_persist_failure_count,
    get_usage_persist_failing_accounts,
    get_usage_headroom_armable_since,
    reconcile_usage_headroom_arming,
    refresh_usage_meter_snapshots,
)
from app.time_utils import _utcnow


# ── UsageSnapshot.to_dict() ──────────────────────────────────────────────────


class TestUsageSnapshotToDict:
    def test_known_snapshot_all_fields(self):
        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 30.0, "seven_day": 65.0},
            resets_at={"five_hour": "2026-07-10T05:00:00Z", "seven_day": "2026-07-14T00:00:00Z"},
            probed_at="2026-07-09T12:00:00",
        )
        d = snap.to_dict()
        assert d["source"] == "oauth_usage_probe"
        assert d["known"] is True
        assert d["probed_at"] == "2026-07-09T12:00:00"
        assert d["five_hour"] == {"utilization": 30.0, "resets_at": "2026-07-10T05:00:00Z"}
        assert d["seven_day"] == {"utilization": 65.0, "resets_at": "2026-07-14T00:00:00Z"}

    def test_unknown_snapshot_has_no_windows(self):
        snap = UsageSnapshot.unknown("2026-07-09T12:00:00")
        d = snap.to_dict()
        assert d["source"] == "oauth_usage_probe"
        assert d["known"] is False
        assert d["probed_at"] == "2026-07-09T12:00:00"
        assert "five_hour" not in d
        assert "seven_day" not in d

    def test_unknown_snapshot_without_probed_at(self):
        snap = UsageSnapshot.unknown()
        d = snap.to_dict()
        assert d["known"] is False
        assert "probed_at" not in d

    def test_extra_windows_preserved(self):
        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 10.0, "seven_day": 20.0, "claude-sonnet-5": 5.0},
            resets_at={},
            probed_at="2026-07-09T12:00:00",
        )
        d = snap.to_dict()
        assert d["claude-sonnet-5"] == {"utilization": 5.0}
        assert d["five_hour"]["utilization"] == 10.0

    def test_window_without_resets_at_omits_it(self):
        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 50.0},
            resets_at={},
            probed_at=None,
        )
        d = snap.to_dict()
        assert d["five_hour"] == {"utilization": 50.0}
        assert "resets_at" not in d["five_hour"]


# ── UsageSnapshot.from_dict() ─────────────────────────────────────────────────


class TestUsageSnapshotFromDict:
    def test_round_trip_known_snapshot(self):
        original = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 42.0, "seven_day": 78.5},
            resets_at={"five_hour": "2026-07-10T05:00:00Z"},
            probed_at="2026-07-09T12:00:00",
        )
        d = original.to_dict()
        restored = UsageSnapshot.from_dict(d)
        assert restored is not None
        assert restored.known is True
        assert restored.window_utilizations == {"five_hour": 42.0, "seven_day": 78.5}
        assert restored.resets_at == {"five_hour": "2026-07-10T05:00:00Z"}
        assert restored.probed_at == "2026-07-09T12:00:00"

    def test_round_trip_unknown_snapshot(self):
        original = UsageSnapshot.unknown("2026-07-09T12:00:00")
        d = original.to_dict()
        restored = UsageSnapshot.from_dict(d)
        assert restored is not None
        assert restored.known is False
        assert restored.probed_at == "2026-07-09T12:00:00"

    def test_round_trip_extra_windows(self):
        original = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 10.0, "seven_day": 20.0, "per_model": 5.0},
            resets_at={"per_model": "2026-07-10T00:00:00Z"},
            probed_at="2026-07-09T12:00:00",
        )
        restored = UsageSnapshot.from_dict(original.to_dict())
        assert restored is not None
        assert restored.window_utilizations["per_model"] == 5.0
        assert restored.resets_at.get("per_model") == "2026-07-10T00:00:00Z"

    def test_rejects_wrong_source(self):
        assert UsageSnapshot.from_dict({"source": "messages_api_header"}) is None

    def test_rejects_none(self):
        assert UsageSnapshot.from_dict(None) is None

    def test_rejects_non_mapping(self):
        assert UsageSnapshot.from_dict("not-a-dict") is None
        assert UsageSnapshot.from_dict(42) is None

    def test_rejects_no_numeric_windows(self):
        raw = {
            "source": "oauth_usage_probe",
            "known": True,
            "probed_at": "2026-07-09T12:00:00",
            "five_hour": {"utilization": "high"},
        }
        assert UsageSnapshot.from_dict(raw) is None

    def test_backwards_compat_old_blob_no_known_field(self):
        """Old blobs (before this fix) lack 'known' key — should default to True."""
        old_blob = {
            "source": "oauth_usage_probe",
            "updated_at": "2026-07-09T12:00:00",
            "five_hour": {"utilization": 42.0, "resets_at": "2026-07-10T05:00:00Z"},
            "seven_day": {"utilization": 63.5},
        }
        restored = UsageSnapshot.from_dict(old_blob)
        assert restored is not None
        assert restored.known is True
        assert restored.window_utilizations == {"five_hour": 42.0, "seven_day": 63.5}
        # probed_at falls back to updated_at for old blobs
        assert restored.probed_at == "2026-07-09T12:00:00"


# ── probed_at preserved through persist/restore cycle ────────────────────────


class TestProbedAtPreservedThroughPersist:
    """Ensure probed_at survives the full _persist_rate_limit_usage_to_db →
    _snapshot_from_persisted_usage round-trip.

    Previously the persist path dropped probed_at (used updated_at instead),
    and the restore path reconstructed probed_at from updated_at (write time),
    causing probe time to be conflated with write time.
    """

    @pytest.mark.asyncio
    async def test_probed_at_survives_persist_restore_cycle(self, monkeypatch):
        from app.max_plan_pool import _persist_rate_limit_usage_to_db

        probe_time = "2026-07-09T11:00:00"
        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 30.0, "seven_day": 45.0},
            resets_at={},
            probed_at=probe_time,
        )

        written: dict | None = None

        class FakeRow:
            rate_limit_usage = None
            updated_at = None

        fake_row = FakeRow()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, acct_id):
                return fake_row

            def add(self, obj):
                nonlocal written
                written = fake_row.rate_limit_usage

            async def commit(self):
                pass

        monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

        await _persist_rate_limit_usage_to_db("test-id", snap)

        assert written is not None
        assert written.get("probed_at") == probe_time, (
            "probed_at must be persisted so restore can surface the true probe time"
        )
        assert "updated_at" in written, "updated_at (write time) must still be present for freshness"

        # Now restore from the persisted blob.
        parsed = _snapshot_from_persisted_usage(written)
        assert parsed is not None
        restored_snap, _stamp = parsed
        assert restored_snap.probed_at == probe_time, (
            "probed_at must survive the full persist → restore cycle"
        )

    @pytest.mark.asyncio
    async def test_write_time_and_probe_time_can_differ(self, monkeypatch):
        """When probe happens long before the write, both timestamps must be distinct."""
        from app.max_plan_pool import _persist_rate_limit_usage_to_db

        probe_time = "2026-07-09T10:00:00"
        write_time = "2026-07-09T10:05:00"

        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 50.0, "seven_day": 60.0},
            resets_at={},
            probed_at=probe_time,
        )

        written: dict | None = None

        class FakeRow:
            rate_limit_usage = None
            updated_at = None

        fake_row = FakeRow()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, acct_id):
                return fake_row

            def add(self, obj):
                nonlocal written
                written = fake_row.rate_limit_usage

            async def commit(self):
                pass

        # Freeze _utcnow to write_time so updated_at is deterministic.
        write_dt = datetime.fromisoformat(write_time)
        monkeypatch.setattr("app.max_plan_pool._utcnow", lambda: write_dt)
        monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

        await _persist_rate_limit_usage_to_db("test-id", snap)

        assert written is not None
        assert written["probed_at"] == probe_time
        assert written["updated_at"] == write_time

        parsed = _snapshot_from_persisted_usage(written)
        assert parsed is not None
        restored_snap, stamp = parsed
        assert restored_snap.probed_at == probe_time
        assert stamp == write_dt


# ── extra windows persist through DB ─────────────────────────────────────────


class TestExtraWindowsPreservedInPersist:
    @pytest.mark.asyncio
    async def test_extra_windows_written_to_db(self, monkeypatch):
        from app.max_plan_pool import _persist_rate_limit_usage_to_db

        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 10.0, "seven_day": 20.0, "claude-opus-4-8": 5.0},
            resets_at={"claude-opus-4-8": "2026-07-10T00:00:00Z"},
            probed_at="2026-07-09T12:00:00",
        )

        written: dict | None = None

        class FakeRow:
            rate_limit_usage = None
            updated_at = None

        fake_row = FakeRow()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, acct_id):
                return fake_row

            def add(self, obj):
                nonlocal written
                written = fake_row.rate_limit_usage

            async def commit(self):
                pass

        monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

        await _persist_rate_limit_usage_to_db("test-id", snap)

        assert written is not None
        assert "claude-opus-4-8" in written, "extra window must be persisted"
        assert written["claude-opus-4-8"]["utilization"] == 5.0
        assert written["claude-opus-4-8"]["resets_at"] == "2026-07-10T00:00:00Z"

        # Verify it round-trips correctly.
        parsed = _snapshot_from_persisted_usage(written)
        assert parsed is not None
        restored, _stamp = parsed
        assert restored.window_utilizations.get("claude-opus-4-8") == 5.0


# ── _snapshot_from_persisted_usage compatibility ──────────────────────────────


class TestSnapshotFromPersistedUsageWithNewBlobs:
    def test_reads_probed_at_from_new_blob(self):
        now = _utcnow()
        raw = {
            "source": "oauth_usage_probe",
            "known": True,
            "probed_at": "2026-07-09T11:00:00",
            "updated_at": now.isoformat(),
            "five_hour": {"utilization": 42.0},
            "seven_day": {"utilization": 63.5},
        }
        parsed = _snapshot_from_persisted_usage(raw)
        assert parsed is not None
        snap, stamp = parsed
        assert snap.probed_at == "2026-07-09T11:00:00"
        assert stamp == now

    def test_returns_none_for_unknown_known_blob(self):
        """A persisted known=False blob should not restore as a known snapshot."""
        raw = {
            "source": "oauth_usage_probe",
            "known": False,
            "probed_at": "2026-07-09T12:00:00",
            "updated_at": _utcnow().isoformat(),
        }
        assert _snapshot_from_persisted_usage(raw) is None

    def test_missing_updated_at_returns_none(self):
        raw = {
            "source": "oauth_usage_probe",
            "known": True,
            "five_hour": {"utilization": 10.0},
        }
        assert _snapshot_from_persisted_usage(raw) is None


# ── persist-failure counter recovery ─────────────────────────────────────────


class TestPersistFailureCounterRecovery:
    """_usage_persist_failing_accounts must clear for an account on a successful
    persist so a transient DB hiccup does not permanently block usage_headroom arming.
    """

    @pytest.mark.asyncio
    async def test_persist_failure_counter_recovers_after_success(self, monkeypatch):
        import app.max_plan_pool as mpp

        # Seed the per-account set to simulate a prior failure.
        original = mpp._usage_persist_failing_accounts.copy()
        mpp._usage_persist_failing_accounts.clear()
        mpp._usage_persist_failing_accounts.add("test-id")
        try:
            snap = UsageSnapshot(
                known=True,
                window_utilizations={"five_hour": 30.0, "seven_day": 45.0},
                resets_at={},
                probed_at="2026-07-09T12:00:00",
            )

            class FakeRow:
                rate_limit_usage = None
                updated_at = None

            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a, **k):
                    pass

                async def get(self, model, acct_id):
                    return FakeRow()

                def add(self, obj):
                    pass

                async def commit(self):
                    pass

            monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
            monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

            assert get_usage_persist_failure_count() == 1, "failing set must be seeded"
            await _persist_rate_limit_usage_to_db("test-id", snap)
            assert get_usage_persist_failure_count() == 0, (
                "account must be cleared from failing set after a successful persist so armable can recover"
            )
        finally:
            mpp._usage_persist_failing_accounts.clear()
            mpp._usage_persist_failing_accounts.update(original)

    @pytest.mark.asyncio
    async def test_counter_does_not_go_below_zero(self, monkeypatch):
        import app.max_plan_pool as mpp

        original = mpp._usage_persist_failing_accounts.copy()
        mpp._usage_persist_failing_accounts.clear()
        try:
            snap = UsageSnapshot(
                known=True,
                window_utilizations={"five_hour": 10.0},
                resets_at={},
                probed_at="2026-07-09T12:00:00",
            )

            class FakeRow:
                rate_limit_usage = None
                updated_at = None

            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a, **k):
                    pass

                async def get(self, model, acct_id):
                    return FakeRow()

                def add(self, obj):
                    pass

                async def commit(self):
                    pass

            monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
            monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

            await _persist_rate_limit_usage_to_db("test-id", snap)
            assert get_usage_persist_failure_count() == 0, (
                "count must stay at 0 when account was not in failing set (no negative)"
            )
        finally:
            mpp._usage_persist_failing_accounts.clear()
            mpp._usage_persist_failing_accounts.update(original)

    @pytest.mark.asyncio
    async def test_persist_failure_attributed_per_account(self, monkeypatch):
        """One account's successful persist must NOT clear another account's failure.

        Account A persistently fails; Account B succeeds on every attempt.
        After B's success, A must still appear in the failing set — the count
        stays >= 1 and get_usage_persist_failing_accounts() names A.
        Once A itself succeeds, the count drops to 0.
        """
        import app.max_plan_pool as mpp

        original = mpp._usage_persist_failing_accounts.copy()
        mpp._usage_persist_failing_accounts.clear()
        try:
            snap = UsageSnapshot(
                known=True,
                window_utilizations={"five_hour": 20.0},
                resets_at={},
                probed_at="2026-07-09T12:00:00",
            )

            class FakeRow:
                rate_limit_usage = None
                updated_at = None

            # Fake session that always succeeds (controls success for B)
            class SuccessSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a, **k):
                    pass

                async def get(self, model, acct_id):
                    return FakeRow()

                def add(self, obj):
                    pass

                async def commit(self):
                    pass

            # Simulate A having previously failed — seed it manually
            mpp._usage_persist_failing_accounts.add("account-a")

            monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: SuccessSession())
            monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

            # B succeeds — must not clear A's failure
            await _persist_rate_limit_usage_to_db("account-b", snap)
            assert get_usage_persist_failure_count() >= 1, (
                "A's failure must persist after B's success"
            )
            failing_ids = {a["id"] for a in get_usage_persist_failing_accounts()}
            assert "account-a" in failing_ids, (
                "get_usage_persist_failing_accounts() must name account-a as failing"
            )
            assert "account-b" not in failing_ids, (
                "account-b succeeded and must not appear in the failing set"
            )

            # Now A itself succeeds — count must drop to 0
            await _persist_rate_limit_usage_to_db("account-a", snap)
            assert get_usage_persist_failure_count() == 0, (
                "count must reach 0 once account-a itself persists successfully"
            )
            failing_ids_after = {a["id"] for a in get_usage_persist_failing_accounts()}
            assert "account-a" not in failing_ids_after, (
                "account-a must be cleared from failing set after its own success"
            )
        finally:
            mpp._usage_persist_failing_accounts.clear()
            mpp._usage_persist_failing_accounts.update(original)


# ── Dark-meter warning for OAuth accounts with missing secrets ────────────────


@pytest.fixture(autouse=True)
def _clear_pool():
    _account_by_id.clear()
    yield
    _account_by_id.clear()


@pytest.mark.asyncio
class TestDarkMeterWarningForMissingSecret:
    """refresh_usage_meter_snapshots must log a warning when an Anthropic OAuth
    account is excluded from probing because its secret is unavailable.

    This distinguishes "secret gone (fixable)" from "probe failed (transient)"
    so operators know what is keeping the meter dark.
    """

    async def test_warns_when_oauth_secret_missing(self, monkeypatch, caplog):
        import logging

        _account_by_id["no-secret"] = {
            "id": "no-secret",
            "name": "no-secret",
            "provider": "anthropic",
            "auth_mode": "oauth",
            "enabled": True,
            "oauth_token": None,
            "runtime_secret": None,
            "active_tasks": 0,
        }

        monkeypatch.setattr(
            "app.max_plan_pool._schedule_usage_refresh",
            lambda acct: None,
        )

        with caplog.at_level(logging.WARNING, logger="app.max_plan_pool"):
            await refresh_usage_meter_snapshots()

        assert any(
            "no-secret" in r.message and "OAuth secret unavailable" in r.message
            for r in caplog.records
        ), "expected dark-meter warning for account with missing OAuth secret"

    async def test_no_warning_for_api_key_accounts(self, monkeypatch, caplog):
        """API-key accounts have no usage source — that's structural, not a warning."""
        import logging

        _account_by_id["api-key-acct"] = {
            "id": "api-key-acct",
            "name": "api-key-acct",
            "provider": "anthropic",
            "auth_mode": "api_key",
            "enabled": True,
            "oauth_token": None,
            "runtime_secret": None,
            "active_tasks": 0,
        }

        monkeypatch.setattr(
            "app.max_plan_pool._schedule_usage_refresh",
            lambda acct: None,
        )

        with caplog.at_level(logging.WARNING, logger="app.max_plan_pool"):
            await refresh_usage_meter_snapshots()

        secret_warnings = [
            r for r in caplog.records if "OAuth secret unavailable" in r.message
        ]
        assert not secret_warnings, "API-key accounts must not generate a secret-unavailable warning"

    async def test_no_warning_for_disabled_accounts(self, monkeypatch, caplog):
        import logging

        _account_by_id["disabled"] = {
            "id": "disabled",
            "name": "disabled",
            "provider": "anthropic",
            "auth_mode": "oauth",
            "enabled": False,
            "oauth_token": None,
            "runtime_secret": None,
            "active_tasks": 0,
        }

        monkeypatch.setattr(
            "app.max_plan_pool._schedule_usage_refresh",
            lambda acct: None,
        )

        with caplog.at_level(logging.WARNING, logger="app.max_plan_pool"):
            await refresh_usage_meter_snapshots()

        secret_warnings = [
            r for r in caplog.records if "OAuth secret unavailable" in r.message
        ]
        assert not secret_warnings, "disabled accounts must not generate a secret-unavailable warning"

    async def test_no_warning_when_secret_present(self, monkeypatch, caplog):
        import logging

        _account_by_id["has-secret"] = {
            "id": "has-secret",
            "name": "has-secret",
            "provider": "anthropic",
            "auth_mode": "oauth",
            "enabled": True,
            "oauth_token": "sk-ant-oauth-valid-token",
            "runtime_secret": None,
            "active_tasks": 0,
        }

        scheduled: list[str] = []
        monkeypatch.setattr(
            "app.max_plan_pool._schedule_usage_refresh",
            lambda acct: scheduled.append(acct["id"]),
        )

        with caplog.at_level(logging.WARNING, logger="app.max_plan_pool"):
            await refresh_usage_meter_snapshots()

        secret_warnings = [
            r for r in caplog.records if "OAuth secret unavailable" in r.message
        ]
        assert not secret_warnings
        assert "has-secret" in scheduled


# ── reconcile_usage_headroom_arming: close the loop (auto-arm) ────────────────


class _FakeDeployState:
    """Minimal stand-in for models.DeploymentState(id=1)."""

    def __init__(self, config_json=None):
        self.runtime_config_json = config_json


def _install_fake_stats(monkeypatch, holder):
    """Drive get_meter_reliability_stats' `armable` verdict from a mutable holder
    so a test can flip reliability without wiring a full account pool."""
    import app.max_plan_pool as mpp

    async def _fake_stats(now=None, min_coverage_pct=80):
        return {
            "armable": holder["armable"],
            "coverage_ratio": 1.0 if holder["armable"] else 0.0,
            "persist_failure_count": 0,
        }

    monkeypatch.setattr(mpp, "get_meter_reliability_stats", _fake_stats)


def _install_fake_state(monkeypatch, commits, *, fail=False, missing=False):
    """Install a fake AsyncSessionLocal that persists into a shared DeploymentState
    stand-in and records each commit's runtime_config_json snapshot."""
    import app.max_plan_pool as mpp

    state = None if missing else _FakeDeployState({})

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, pk):
            return state

        def add(self, obj):
            pass

        async def commit(self):
            if fail:
                raise RuntimeError("simulated commit failure")
            commits.append(dict(state.runtime_config_json or {}))

    monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: _Sess())
    return state


def _set_config(monkeypatch, **overrides):
    import app.shared_state as shared_state

    base = {
        "usage_headroom_auto_arm_mode": "auto",
        "usage_headroom_auto_arm_observation_minutes": 60,
        "usage_headroom_gate_enabled": False,
        "usage_headroom_min_coverage_pct": 80,
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setitem(shared_state.runtime_config, k, v)


@pytest.fixture
def _reset_arm_marker(monkeypatch):
    import app.max_plan_pool as mpp

    monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)
    yield


@pytest.mark.asyncio
class TestReconcileUsageHeadroomArming:
    """Close-the-loop reconciler: sustained meter reliability arms the gate;
    a regression fail-safe disarms it; mode governs whether it acts at all."""

    async def test_auto_arms_after_window_elapses(
        self, monkeypatch, _reset_arm_marker
    ):
        import app.shared_state as shared_state

        _set_config(monkeypatch)
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        state = _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()
        # First cycle: armable holds but window has not elapsed → wait, not armed.
        res = await reconcile_usage_headroom_arming(now=now0)
        assert res["action"] == "waiting"
        assert res["gate_enabled"] is False
        assert commits == [], "must NOT persist before the window elapses"
        assert get_usage_headroom_armable_since() == now0

        # Second cycle past the 60-minute window → arm.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert res["action"] == "armed"
        assert res["gate_enabled"] is True
        # Persisted to DeploymentState BEFORE the in-memory mutation.
        assert commits == [{"usage_headroom_gate_enabled": True}]
        assert state.runtime_config_json["usage_headroom_gate_enabled"] is True
        # In-memory config mirrored after the durable commit.
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is True

    async def test_auto_stays_off_before_window(
        self, monkeypatch, _reset_arm_marker
    ):
        _set_config(monkeypatch)
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()
        await reconcile_usage_headroom_arming(now=now0)
        # 30 minutes < 60-minute window.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=30))
        assert res["action"] == "waiting"
        assert res["gate_enabled"] is False
        assert commits == []

    async def test_auto_failsafe_disarms_on_regression(
        self, monkeypatch, _reset_arm_marker
    ):
        import app.shared_state as shared_state

        _set_config(monkeypatch)
        holder = {"armable": True}
        _install_fake_stats(monkeypatch, holder)
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()
        await reconcile_usage_headroom_arming(now=now0)
        await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is True

        # Reliability regresses — immediate fail-safe disarm, no window.
        holder["armable"] = False
        res = await reconcile_usage_headroom_arming(
            now=now0 + timedelta(minutes=62)
        )
        assert res["action"] == "disarmed"
        assert res["gate_enabled"] is False
        assert commits[-1] == {"usage_headroom_gate_enabled": False}
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is False
        assert get_usage_headroom_armable_since() is None

    async def test_mid_window_regression_resets_clock(
        self, monkeypatch, _reset_arm_marker
    ):
        _set_config(monkeypatch)
        holder = {"armable": True}
        _install_fake_stats(monkeypatch, holder)
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()
        await reconcile_usage_headroom_arming(now=now0)
        # Regress before arming — marker resets, gate never armed.
        holder["armable"] = False
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=30))
        assert res["action"] == "noop"
        assert get_usage_headroom_armable_since() is None
        assert commits == []

        # Recover — the window restarts from the recovery instant, not now0.
        holder["armable"] = True
        recover_at = now0 + timedelta(minutes=40)
        await reconcile_usage_headroom_arming(now=recover_at)
        assert get_usage_headroom_armable_since() == recover_at
        # 61 min after now0 is only 21 min into the restarted window → still off.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert res["action"] == "waiting"
        assert commits == []

    async def test_manual_mode_never_touches_gate(
        self, monkeypatch, _reset_arm_marker
    ):
        import app.shared_state as shared_state

        _set_config(
            monkeypatch,
            usage_headroom_auto_arm_mode="manual",
            usage_headroom_gate_enabled=True,
        )
        _install_fake_stats(monkeypatch, {"armable": False})
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        # Even with armable False, manual mode leaves the operator's gate as-is.
        res = await reconcile_usage_headroom_arming(now=_utcnow())
        assert res["action"] == "noop"
        assert res["mode"] == "manual"
        assert commits == []
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is True

    async def test_off_mode_forces_gate_false(
        self, monkeypatch, _reset_arm_marker
    ):
        import app.shared_state as shared_state

        _set_config(
            monkeypatch,
            usage_headroom_auto_arm_mode="off",
            usage_headroom_gate_enabled=True,
        )
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        res = await reconcile_usage_headroom_arming(now=_utcnow())
        assert res["action"] == "disarmed"
        assert res["gate_enabled"] is False
        assert commits == [{"usage_headroom_gate_enabled": False}]
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is False

    async def test_off_mode_idempotent_when_already_false(
        self, monkeypatch, _reset_arm_marker
    ):
        _set_config(
            monkeypatch,
            usage_headroom_auto_arm_mode="off",
            usage_headroom_gate_enabled=False,
        )
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        res = await reconcile_usage_headroom_arming(now=_utcnow())
        assert res["action"] == "noop"
        assert commits == [], "no redundant write when already disarmed"

    async def test_arm_is_idempotent_no_redundant_writes(
        self, monkeypatch, _reset_arm_marker
    ):
        _set_config(monkeypatch)
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()
        await reconcile_usage_headroom_arming(now=now0)
        await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert len(commits) == 1
        # Subsequent cycles with the gate already armed must not re-persist.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=120))
        assert res["action"] == "noop"
        assert res["gate_enabled"] is True
        assert len(commits) == 1, "armed gate must not be re-committed each cycle"

    async def test_persist_failure_leaves_memory_unchanged(
        self, monkeypatch, _reset_arm_marker
    ):
        import app.shared_state as shared_state

        _set_config(monkeypatch)
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        _install_fake_state(monkeypatch, commits, fail=True)

        now0 = _utcnow()
        await reconcile_usage_headroom_arming(now=now0)
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert res["action"] == "persist_failed"
        # Persist failed → in-memory gate must remain the un-armed default.
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is False

    async def test_missing_deployment_state_does_not_arm(
        self, monkeypatch, _reset_arm_marker
    ):
        import app.shared_state as shared_state

        _set_config(monkeypatch)
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        _install_fake_state(monkeypatch, commits, missing=True)

        now0 = _utcnow()
        await reconcile_usage_headroom_arming(now=now0)
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert res["action"] == "persist_failed"
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is False


# ── config plumbing: sync + PATCH validation for the auto-arm knobs ──────────


class _FakeSyncState:
    """Minimal DeploymentState stand-in for sync_runtime_config_from_state."""

    def __init__(self, config_json):
        from app.models import DeploymentStatus

        self.status = DeploymentStatus.STABLE
        self.runtime_config_json = config_json


@pytest.mark.asyncio
class TestAutoArmConfigPlumbing:
    """The two new knobs must propagate across instances via
    sync_runtime_config_from_state and be validated in the PATCH handler."""

    async def test_sync_propagates_auto_arm_keys(self, monkeypatch):
        import app.shared_state as shared_state
        from app.runtime_config_state import sync_runtime_config_from_state

        monkeypatch.setitem(
            shared_state.runtime_config, "usage_headroom_auto_arm_mode", "auto"
        )
        monkeypatch.setitem(
            shared_state.runtime_config,
            "usage_headroom_auto_arm_observation_minutes",
            60,
        )
        state = _FakeSyncState(
            {
                "usage_headroom_auto_arm_mode": "off",
                "usage_headroom_auto_arm_observation_minutes": 30,
            }
        )
        await sync_runtime_config_from_state(state)
        assert shared_state.runtime_config["usage_headroom_auto_arm_mode"] == "off"
        assert (
            shared_state.runtime_config[
                "usage_headroom_auto_arm_observation_minutes"
            ]
            == 30
        )

    async def test_sync_rejects_bad_mode_and_clamps_minutes(self, monkeypatch):
        import app.shared_state as shared_state
        from app.runtime_config_state import sync_runtime_config_from_state

        monkeypatch.setitem(
            shared_state.runtime_config, "usage_headroom_auto_arm_mode", "auto"
        )
        monkeypatch.setitem(
            shared_state.runtime_config,
            "usage_headroom_auto_arm_observation_minutes",
            60,
        )
        state = _FakeSyncState(
            {
                # Bogus mode is ignored (stays at prior value).
                "usage_headroom_auto_arm_mode": "bogus",
                # Absurd window is clamped to the [1, 1440] bound.
                "usage_headroom_auto_arm_observation_minutes": 999999,
            }
        )
        await sync_runtime_config_from_state(state)
        assert shared_state.runtime_config["usage_headroom_auto_arm_mode"] == "auto"
        assert (
            shared_state.runtime_config[
                "usage_headroom_auto_arm_observation_minutes"
            ]
            == 1440
        )

    async def test_patch_validation_rejects_bad_mode(self):
        from fastapi import HTTPException
        from app.routers.admin_settings import _apply_settings

        with pytest.raises(HTTPException) as ei:
            await _apply_settings(
                {"usage_headroom_auto_arm_mode": "sometimes"}, None, {}
            )
        assert ei.value.status_code == 400

    async def test_patch_validation_rejects_out_of_range_minutes(self):
        from fastapi import HTTPException
        from app.routers.admin_settings import _apply_settings

        with pytest.raises(HTTPException) as ei:
            await _apply_settings(
                {"usage_headroom_auto_arm_observation_minutes": 0}, None, {}
            )
        assert ei.value.status_code == 400
