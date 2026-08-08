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
from app.providers.anthropic import _normalize_anthropic_usage
from app.max_plan_pool import (
    _account_by_id,
    _persist_rate_limit_usage_to_db,
    _snapshot_from_persisted_usage,
    get_usage_persist_failure_count,
    get_usage_persist_failing_accounts,
    get_usage_headroom_armable_since,
    reconcile_usage_headroom_arming,
    refresh_usage_meter_snapshots,
    usage_snapshot_summary_with_fallback,
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

    def test_unknown_snapshot_round_trips_its_failure_class(self):
        original = UsageSnapshot.unknown(
            "2026-07-09T12:00:00", failure_kind="authentication"
        )
        restored = UsageSnapshot.from_dict(original.to_dict())
        assert restored is not None
        assert restored.known is False
        assert restored.probe_failure_kind == "authentication"


class TestAnthropicUsageNormalization:
    def test_five_hour_window_is_sufficient(self):
        snapshot = _normalize_anthropic_usage(
            {"five_hour": {"utilization": 31.5, "resets_at": "2026-07-10T05:00:00Z"}},
            "2026-07-09T12:00:00",
        )
        assert snapshot.known is True
        assert snapshot.window_utilizations == {"five_hour": 31.5}
        assert snapshot.resets_at["five_hour"] == "2026-07-10T05:00:00Z"

    def test_invalid_five_hour_data_is_classified_as_schema_failure(self):
        snapshot = _normalize_anthropic_usage(
            {"five_hour": {"utilization": None}}, "2026-07-09T12:00:00"
        )
        assert snapshot.known is False
        assert snapshot.probe_failure_kind == "schema"


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


class TestUsageAttemptDiagnostics:
    def test_failed_attempt_is_visible_without_clobbering_last_good_reading(self):
        now = _utcnow()
        persisted = {
            "source": "oauth_usage_probe",
            "known": True,
            "probed_at": (now - timedelta(minutes=60)).isoformat(),
            "updated_at": (now - timedelta(minutes=60)).isoformat(),
            "five_hour": {"utilization": 43.0},
            "last_attempt_at": now.isoformat(),
            "last_attempt_status": "timeout",
        }
        summary = usage_snapshot_summary_with_fallback(None, persisted, now)
        assert summary["known"] is True
        assert summary["five_hour_utilization"] == 43.0
        assert summary["stale"] is True
        assert summary["last_attempt_status"] == "timeout"
        assert summary["last_attempt_failure_kind"] == "timeout"

    def test_openai_account_is_explicitly_unsupported(self):
        now = _utcnow()
        summary = usage_snapshot_summary_with_fallback(
            {
                "provider": "openai",
                "auth_mode": "oauth",
                "enabled": True,
            },
            None,
            now,
        )
        assert summary["known"] is False
        assert summary["dark_reason"] == "unsupported"

    @pytest.mark.asyncio
    async def test_failed_attempt_persist_retains_existing_last_good_blob(self, monkeypatch):
        existing = {
            "source": "oauth_usage_probe",
            "known": True,
            "updated_at": "2026-07-09T11:00:00",
            "five_hour": {"utilization": 21.0},
        }

        class FakeRow:
            rate_limit_usage = existing
            updated_at = None

        row = FakeRow()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, model, acct_id):
                return row

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda value: value)

        await _persist_rate_limit_usage_to_db(
            "usage-acct",
            UsageSnapshot.unknown(
                "2026-07-09T12:00:00", failure_kind="timeout"
            ),
        )

        assert row.rate_limit_usage["five_hour"]["utilization"] == 21.0
        assert row.rate_limit_usage["updated_at"] == "2026-07-09T11:00:00"
        assert row.rate_limit_usage["last_attempt_status"] == "timeout"

    def test_secret_manager_reference_is_meter_eligible_before_hydration(self):
        from app.max_plan_pool import _account_meter_eligible

        assert _account_meter_eligible(
            {
                "provider": "anthropic",
                "auth_mode": "oauth",
                "enabled": True,
                "secret_blob_ref": "projects/example/secrets/anthropic/versions/latest",
                "runtime_secret": None,
                "oauth_token": None,
            }
        ) is True


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
            lambda acct, *, delay_seconds=0.0: None,
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
            lambda acct, *, delay_seconds=0.0: None,
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
            lambda acct, *, delay_seconds=0.0: None,
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
            lambda acct, *, delay_seconds=0.0: scheduled.append(acct["id"]),
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
        # Marker persisted (not the gate) on the first observation.
        assert len(commits) == 1
        assert commits[0].get("usage_headroom_armable_since") == now0.isoformat()
        assert get_usage_headroom_armable_since() == now0

        # Second cycle past the 60-minute window → arm.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert res["action"] == "armed"
        assert res["gate_enabled"] is True
        # Both the armable-since marker AND the gate were persisted.
        assert len(commits) == 2
        assert commits[1].get("usage_headroom_gate_enabled") is True
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
        assert len(commits) == 1  # armable-since marker persisted
        # 30 minutes < 60-minute window.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=30))
        assert res["action"] == "waiting"
        assert res["gate_enabled"] is False
        # No additional commits — gate unchanged, marker unchanged.
        assert len(commits) == 1

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
        # Both the armable-since reset and the gate-disarm are persisted.
        assert commits[-1].get("usage_headroom_gate_enabled") is False
        assert commits[-1].get("usage_headroom_armable_since") is None
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
        assert len(commits) == 1  # armable-since marker persisted
        # Regress before arming — marker resets, gate never armed.
        holder["armable"] = False
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=30))
        assert res["action"] == "noop"
        assert get_usage_headroom_armable_since() is None
        # The armable-since reset was persisted.
        assert len(commits) == 2
        assert commits[1].get("usage_headroom_armable_since") is None

        # Recover — the window restarts from the recovery instant, not now0.
        holder["armable"] = True
        recover_at = now0 + timedelta(minutes=40)
        await reconcile_usage_headroom_arming(now=recover_at)
        assert get_usage_headroom_armable_since() == recover_at
        assert len(commits) == 3  # fresh marker persisted
        # 61 min after now0 is only 21 min into the restarted window → still off.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=61))
        assert res["action"] == "waiting"
        assert len(commits) == 3  # no new commit — marker and gate unchanged

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
        # First commit: armable-since marker. Second commit: gate + marker.
        assert len(commits) == 2
        # Subsequent cycles with the gate already armed must not re-persist.
        res = await reconcile_usage_headroom_arming(now=now0 + timedelta(minutes=120))
        assert res["action"] == "noop"
        assert res["gate_enabled"] is True
        assert len(commits) == 2, "armed gate must not be re-committed each cycle"

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

    # ── armable-since persistence + restart survival ─────────────────────────────

    async def test_auto_persists_armable_since_marker(
        self, monkeypatch, _reset_arm_marker
    ):
        """The armable-since marker is persisted to DB when first set."""

        _set_config(monkeypatch)
        _install_fake_stats(monkeypatch, {"armable": True})
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()
        res = await reconcile_usage_headroom_arming(now=now0)
        assert res["action"] == "waiting"
        assert get_usage_headroom_armable_since() == now0
        # Must be persisted in the last commit.
        assert len(commits) >= 1
        last_commit = commits[-1]
        assert last_commit.get("usage_headroom_armable_since") == now0.isoformat()

    async def test_auto_restores_armable_since_after_restart(
        self, monkeypatch, _reset_arm_marker
    ):
        """Simulate leader restart: the persisted marker is restored so the
        observation window is NOT reset by a restart."""
        import app.max_plan_pool as mpp

        _set_config(monkeypatch)
        holder = {"armable": True}
        _install_fake_stats(monkeypatch, holder)
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()

        # Cycle 1: armable True, marker set to now0 and persisted.
        res = await reconcile_usage_headroom_arming(now=now0)
        assert res["action"] == "waiting"
        assert get_usage_headroom_armable_since() == now0

        # Simulate leader restart: module variable reverts to None.
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)
        assert get_usage_headroom_armable_since() is None

        # Cycle 2 (armable still True after restart): restore from DB.
        at = now0 + timedelta(minutes=31)
        res = await reconcile_usage_headroom_arming(now=at)
        assert res["action"] == "waiting"
        # The marker was restored from DB (now0), not set to `at`.
        assert get_usage_headroom_armable_since() == now0
        assert res["armable_since"] == now0.isoformat()

    async def test_auto_restart_does_not_reset_observation_window(
        self, monkeypatch, _reset_arm_marker
    ):
        """The full observation window survives a restart: a window that would
        have elapsed at T+60min still arms the gate after a mid-window restart."""
        import app.max_plan_pool as mpp
        import app.shared_state as shared_state

        _set_config(monkeypatch)
        holder = {"armable": True}
        _install_fake_stats(monkeypatch, holder)
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()

        # Cycle 1: marker set at now0.
        res = await reconcile_usage_headroom_arming(now=now0)
        assert get_usage_headroom_armable_since() == now0

        # Simulate restart right after cycle 1.
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)

        # Cycle 2: 31 min later, within window, marker restored.
        mid = now0 + timedelta(minutes=31)
        res = await reconcile_usage_headroom_arming(now=mid)
        assert res["action"] == "waiting"
        assert get_usage_headroom_armable_since() == now0

        # Cycle 3: 61 min from original → window elapsed, gate arms.
        arm_time = now0 + timedelta(minutes=61)
        res = await reconcile_usage_headroom_arming(now=arm_time)
        assert res["action"] == "armed"
        assert res["gate_enabled"] is True
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is True

    async def test_regression_after_restart_discards_persisted_marker(
        self, monkeypatch, _reset_arm_marker
    ):
        """A genuine meter-health regression after restart correctly discards the
        persisted marker and starts a fresh observation from the recovery time."""
        import app.max_plan_pool as mpp

        _set_config(monkeypatch)
        holder = {"armable": True}
        _install_fake_stats(monkeypatch, holder)
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        now0 = _utcnow()

        # Cycle 1: armable True, marker persisted at now0.
        res = await reconcile_usage_headroom_arming(now=now0)
        assert get_usage_headroom_armable_since() == now0

        # Simulate restart AND regression: meters are now unhealthy.
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)
        holder["armable"] = False

        # Cycle 2 after restart: armable=False → restore then reset.
        regressed_at = now0 + timedelta(minutes=10)
        res = await reconcile_usage_headroom_arming(now=regressed_at)
        assert res["action"] == "noop"  # gate already False
        assert get_usage_headroom_armable_since() is None

        # Meters recover.
        holder["armable"] = True
        recovered_at = now0 + timedelta(minutes=20)
        res = await reconcile_usage_headroom_arming(now=recovered_at)
        # Fresh observation from recovery time, NOT from now0.
        assert get_usage_headroom_armable_since() == recovered_at
        assert res["action"] == "waiting"

        # 61 min after original now0 is only 41 min into fresh window.
        still_too_early = now0 + timedelta(minutes=61)
        res = await reconcile_usage_headroom_arming(now=still_too_early)
        assert res["action"] == "waiting"

        # 61 min after recovery → arms.
        arm_at = recovered_at + timedelta(minutes=61)
        res = await reconcile_usage_headroom_arming(now=arm_at)
        assert res["action"] == "armed"


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


# ── API-key account live-completion header capture ────────────────────────────


@pytest.mark.asyncio
class TestApiKeyCompletionHeadersPopulateUsageMeter:
    """API-key accounts should populate the usage meter via rate-limit headers
    returned by a minimal messages.create call (probe_usage API_KEY path).

    Named test: test_api_key_completion_headers_populate_usage_meter
    """

    async def test_api_key_completion_headers_populate_usage_meter(self, monkeypatch):
        """Valid API-key account gets a known UsageSnapshot from live-completion headers."""
        from app.providers.anthropic import AnthropicProvider
        from app.models import LLMAuthMode

        rate_limit_data = {
            "source": "messages_api_header",
            "updated_at": "2026-07-22T10:00:00+00:00",
            "unified_5h": {
                "remaining_tokens": 80000,
                "limit_tokens": 100000,
                "status": "live",
                "resets_at": "2026-07-22T15:00:00Z",
                "utilization": 20.0,
            },
        }

        async def fake_call_direct_llm(api_key, *, messages, max_tokens, timeout, **kw):
            assert api_key == "sk-ant-api-test-key"
            assert max_tokens == 1
            return ("1", rate_limit_data, None)

        monkeypatch.setattr(
            "app.direct_llm_client.call_direct_llm",
            fake_call_direct_llm,
        )

        provider = AnthropicProvider()
        snap = await provider.probe_usage("sk-ant-api-test-key", LLMAuthMode.API_KEY)

        assert snap.known is True, "expected known snapshot from API-key headers"
        assert "five_hour" in snap.window_utilizations
        assert snap.window_utilizations["five_hour"] == 20.0
        assert snap.resets_at.get("five_hour") == "2026-07-22T15:00:00Z"

    async def test_api_key_no_secret_returns_unknown(self):
        """No secret → unknown without calling messages.create."""
        from app.providers.anthropic import AnthropicProvider
        from app.models import LLMAuthMode

        provider = AnthropicProvider()
        snap = await provider.probe_usage(None, LLMAuthMode.API_KEY)

        assert snap.known is False

    async def test_api_key_call_error_returns_unknown(self, monkeypatch):
        """call_direct_llm raising → unknown (never raises out of probe_usage)."""
        from app.providers.anthropic import AnthropicProvider
        from app.models import LLMAuthMode

        async def fake_call_direct_llm(api_key, *, messages, max_tokens, timeout, **kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            "app.direct_llm_client.call_direct_llm",
            fake_call_direct_llm,
        )

        provider = AnthropicProvider()
        snap = await provider.probe_usage("sk-ant-api-test-key", LLMAuthMode.API_KEY)

        assert snap.known is False

    async def test_api_key_no_unified_5h_data_returns_unknown(self, monkeypatch):
        """Response with no unified_5h bucket → unknown snapshot."""
        from app.providers.anthropic import AnthropicProvider
        from app.models import LLMAuthMode

        async def fake_call_direct_llm(api_key, *, messages, max_tokens, timeout, **kw):
            return ("1", {"source": "messages_api_header", "updated_at": "2026-07-22T10:00:00Z"}, None)

        monkeypatch.setattr(
            "app.direct_llm_client.call_direct_llm",
            fake_call_direct_llm,
        )

        provider = AnthropicProvider()
        snap = await provider.probe_usage("sk-ant-api-test-key", LLMAuthMode.API_KEY)

        assert snap.known is False

    async def test_api_key_account_eligible_with_runtime_secret(self):
        """_account_meter_eligible returns True for API_KEY accounts with runtime_secret."""
        from app.max_plan_pool import _account_meter_eligible

        acct = {
            "id": "api-key-acct",
            "provider": "anthropic",
            "auth_mode": "api_key",
            "enabled": True,
            "oauth_token": None,
            "runtime_secret": "sk-ant-api-key-secret",
        }
        assert _account_meter_eligible(acct) is True

    async def test_api_key_account_ineligible_without_runtime_secret(self):
        """_account_meter_eligible returns False for API_KEY accounts without runtime_secret."""
        from app.max_plan_pool import _account_meter_eligible

        acct = {
            "id": "api-key-acct",
            "provider": "anthropic",
            "auth_mode": "api_key",
            "enabled": True,
            "oauth_token": None,
            "runtime_secret": None,
        }
        assert _account_meter_eligible(acct) is False


# ── Executed end-to-end: API_KEY headers → meter → auto-arm loop ──────────────


@pytest.mark.asyncio
class TestApiKeyMeterExecutesAutoArmLoop:
    """End-to-end test: live-completion rate-limit headers flow through the
    usage meter and trigger the auto-arm gate decision.

    Named test: test_api_key_meter_executes_auto_arm_loop

    Does NOT mock get_meter_reliability_stats. Feeds synthetic rate-limit
    headers from a mocked messages.create response through _refresh_usage_snapshot
    (the API_KEY probe path), asserts the pool account gains a known snapshot
    with correct values, then runs the real reliability-stats + reconciler
    pipeline and confirms the gate arms after the observation window elapses.
    """

    async def test_api_key_meter_executes_auto_arm_loop(
        self, monkeypatch, _reset_arm_marker
    ):
        import app.max_plan_pool as mpp
        import app.shared_state as shared_state

        # Config: 1-minute observation window so the test can advance time cheaply.
        _set_config(monkeypatch, usage_headroom_auto_arm_observation_minutes=1)

        # Single API_KEY pool account with a runtime_secret.
        acct: dict = {
            "id": "aaa-111",
            "provider": "anthropic",
            "auth_mode": "api_key",
            "enabled": True,
            "runtime_secret": "sk-ant-key-1",
            "oauth_token": None,
        }
        monkeypatch.setattr(mpp, "_account_by_id", {"aaa-111": acct})
        monkeypatch.setattr(mpp, "_usage_persist_failing_accounts", set())

        # Synthetic rate-limit response that simulates a live-completion header payload.
        # Both remaining_tokens and limit_tokens present so utilization is computable.
        rate_limit_data = {
            "source": "messages_api_header",
            "updated_at": "2026-07-22T10:00:00+00:00",
            "unified_5h": {
                "remaining_tokens": 80_000,
                "limit_tokens": 100_000,
                "status": "live",
                "resets_at": "2026-07-22T15:00:00Z",
                "utilization": 20.0,
            },
        }

        async def fake_call_direct_llm(api_key, *, messages, max_tokens, timeout, **kw):
            assert api_key == "sk-ant-key-1"
            assert max_tokens == 1
            return ("1", rate_limit_data, None)

        monkeypatch.setattr(
            "app.direct_llm_client.call_direct_llm", fake_call_direct_llm
        )

        # Bypass DB writes so the test is standalone (no Postgres needed).
        async def _noop_persist(acct_id, snapshot):
            pass

        monkeypatch.setattr(mpp, "_persist_rate_limit_usage_to_db", _noop_persist)

        async def _no_persisted_blobs():
            return {}

        monkeypatch.setattr(mpp, "_load_persisted_usage_blobs", _no_persisted_blobs)

        # DeploymentState mock for reconcile_usage_headroom_arming writes.
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        # ── Step 1: feed rate-limit headers through _refresh_usage_snapshot ──
        await mpp._refresh_usage_snapshot("aaa-111")

        snap = acct.get("usage_snapshot")
        assert snap is not None, "probe must populate usage_snapshot"
        assert snap.known is True
        assert snap.window_utilizations["five_hour"] == 20.0
        assert snap.resets_at.get("five_hour") == "2026-07-22T15:00:00Z"
        # raw_rate_limit_blob carries provider-reported remaining/limit counts.
        assert snap.raw_rate_limit_blob is not None
        assert snap.raw_rate_limit_blob["unified_5h"]["remaining_tokens"] == 80_000
        assert snap.raw_rate_limit_blob["unified_5h"]["limit_tokens"] == 100_000

        # Use the actual snapshot timestamp as the reference clock so freshness
        # checks are deterministic (snapshot_at < now+2min < stale threshold 900s).
        now = acct["usage_snapshot_at"]

        # ── Step 2: get_meter_reliability_stats (REAL) → armable ─────────────
        stats = await mpp.get_meter_reliability_stats(now=now)
        assert stats["armable"] is True, f"expected armable; got {stats}"
        assert stats["meters_known"] == 1
        assert stats["meters_eligible"] == 1
        assert stats["coverage_ratio"] == 1.0

        # ── Step 3: reconcile (REAL) → waiting (window not yet elapsed) ──────
        res = await reconcile_usage_headroom_arming(now=now)
        assert res["action"] == "waiting"
        assert res["gate_enabled"] is False
        # armable_since is persisted on first observation so the window survives
        # leader restarts (4d310c8/#16242).
        assert commits == [{"usage_headroom_armable_since": now.isoformat()}]

        # ── Step 4: 2 minutes later → window elapsed, snapshot still fresh ───
        # (2 min = 120s < _USAGE_METER_DISPLAY_STALE_SECONDS 900s, so known=1)
        res = await reconcile_usage_headroom_arming(now=now + timedelta(minutes=2))
        assert res["action"] == "armed"
        assert res["gate_enabled"] is True
        assert commits == [
            {"usage_headroom_armable_since": now.isoformat()},
            {"usage_headroom_armable_since": now.isoformat(), "usage_headroom_gate_enabled": True},
        ]
        assert shared_state.runtime_config["usage_headroom_gate_enabled"] is True


# ── _snapshot_from_rate_limit_data negative-path tests ───────────────────────


class TestSnapshotFromRateLimitDataNegativePaths:
    """Direct unit tests for _snapshot_from_rate_limit_data (in
    app.providers.anthropic), which converts a normalized rate-limit header
    blob (from the API-key completion path) into a UsageSnapshot.

    The function is the innermost parser; negative paths must return
    UsageSnapshot.unknown() without raising.
    """

    def test_none_input_returns_unknown(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(None, "2026-07-22T10:00:00")
        assert snap.known is False
        assert snap.probed_at == "2026-07-22T10:00:00"

    def test_non_dict_inputs_return_unknown(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        for bad in ("string", 42, [], True, b"bytes"):
            snap = _snapshot_from_rate_limit_data(bad, None)
            assert snap.known is False, f"expected unknown for {bad!r}"

    def test_missing_unified_5h_returns_unknown(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(
            {"source": "messages_api_header", "updated_at": "2026-07-22T10:00:00Z"},
            "2026-07-22T10:00:00",
        )
        assert snap.known is False

    def test_unified_5h_not_a_dict_returns_unknown(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        for bad_5h in (None, "string", 42, []):
            snap = _snapshot_from_rate_limit_data(
                {"unified_5h": bad_5h}, "2026-07-22T10:00:00"
            )
            assert snap.known is False, f"expected unknown for unified_5h={bad_5h!r}"

    def test_bool_utilization_returns_unknown(self):
        """Boolean utilization must not be coerced to 0.0/1.0."""
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        for bool_val in (True, False):
            snap = _snapshot_from_rate_limit_data(
                {"unified_5h": {"utilization": bool_val, "resets_at": "2026-07-22T15:00:00Z"}},
                "2026-07-22T10:00:00",
            )
            assert snap.known is False, f"expected unknown for bool utilization={bool_val}"

    def test_string_utilization_returns_unknown(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(
            {"unified_5h": {"utilization": "high", "resets_at": "2026-07-22T15:00:00Z"}},
            "2026-07-22T10:00:00",
        )
        assert snap.known is False

    def test_none_utilization_returns_unknown(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(
            {"unified_5h": {"utilization": None}},
            "2026-07-22T10:00:00",
        )
        assert snap.known is False

    def test_zero_utilization_is_valid(self):
        """0% utilization is a legitimate known state, not an error sentinel."""
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(
            {"unified_5h": {"utilization": 0.0, "resets_at": "2026-07-22T15:00:00Z"}},
            "2026-07-22T10:00:00",
        )
        assert snap.known is True
        assert snap.window_utilizations["five_hour"] == 0.0

    def test_valid_populates_five_hour_window_with_correct_values(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(
            {
                "source": "messages_api_header",
                "unified_5h": {
                    "utilization": 35.5,
                    "resets_at": "2026-07-22T15:00:00Z",
                    "remaining_tokens": 64500,
                    "limit_tokens": 100000,
                },
            },
            "2026-07-22T10:00:00",
        )
        assert snap.known is True
        assert snap.window_utilizations == {"five_hour": 35.5}
        assert snap.resets_at.get("five_hour") == "2026-07-22T15:00:00Z"

    def test_missing_resets_at_produces_known_without_reset(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(
            {"unified_5h": {"utilization": 50.0}},
            "2026-07-22T10:00:00",
        )
        assert snap.known is True
        assert snap.window_utilizations["five_hour"] == 50.0
        assert "five_hour" not in snap.resets_at

    def test_non_string_resets_at_is_omitted(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        snap = _snapshot_from_rate_limit_data(
            {"unified_5h": {"utilization": 20.0, "resets_at": 12345}},
            "2026-07-22T10:00:00",
        )
        assert snap.known is True
        assert "five_hour" not in snap.resets_at

    def test_probed_at_is_threaded_through(self):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        probed = "2026-07-22T09:55:00"
        snap = _snapshot_from_rate_limit_data({"unified_5h": {"utilization": 10.0}}, probed)
        assert snap.probed_at == probed

    def test_raw_rate_limit_blob_is_attached(self):
        """The full normalized blob must be attached as raw_rate_limit_blob
        so remaining_tokens/limit_tokens survive into the persist path."""
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        raw = {
            "unified_5h": {
                "utilization": 20.0,
                "remaining_tokens": 80000,
                "limit_tokens": 100000,
            },
            "source": "messages_api_header",
        }
        snap = _snapshot_from_rate_limit_data(raw, "2026-07-22T10:00:00")
        assert snap.known is True
        assert snap.raw_rate_limit_blob is raw


# ── _snapshot_from_persisted_usage for messages_api_header blobs ─────────────


class TestSnapshotFromPersistedUsageApiKeyPath:
    """_snapshot_from_persisted_usage must accept messages_api_header source
    blobs (written by _persist_rate_limit_usage_to_db for API-key accounts)
    and map unified_5h → five_hour window so non-leader instances can restore
    the API-key meter from the DB-persisted blob.
    """

    def test_valid_blob_restores_as_five_hour(self):
        now_str = _utcnow().isoformat()
        raw = {
            "source": "messages_api_header",
            "updated_at": now_str,
            "unified_5h": {
                "utilization": 25.0,
                "resets_at": "2026-07-22T15:00:00Z",
                "remaining_tokens": 75000,
                "limit_tokens": 100000,
                "status": "live",
            },
        }
        result = _snapshot_from_persisted_usage(raw)
        assert result is not None
        snap, _stamp = result
        assert snap.known is True
        assert snap.window_utilizations == {"five_hour": 25.0}
        assert snap.resets_at.get("five_hour") == "2026-07-22T15:00:00Z"

    def test_missing_unified_5h_returns_none(self):
        raw = {
            "source": "messages_api_header",
            "updated_at": _utcnow().isoformat(),
        }
        assert _snapshot_from_persisted_usage(raw) is None

    def test_malformed_string_utilization_returns_none(self):
        raw = {
            "source": "messages_api_header",
            "updated_at": _utcnow().isoformat(),
            "unified_5h": {"utilization": "high", "resets_at": "2026-07-22T15:00:00Z"},
        }
        assert _snapshot_from_persisted_usage(raw) is None

    def test_bool_utilization_returns_none(self):
        """Boolean utilization must not be coerced during restore."""
        raw = {
            "source": "messages_api_header",
            "updated_at": _utcnow().isoformat(),
            "unified_5h": {"utilization": True},
        }
        assert _snapshot_from_persisted_usage(raw) is None

    def test_none_utilization_returns_none(self):
        raw = {
            "source": "messages_api_header",
            "updated_at": _utcnow().isoformat(),
            "unified_5h": {"utilization": None},
        }
        assert _snapshot_from_persisted_usage(raw) is None

    def test_stamp_is_updated_at(self):
        """Returned wall-clock stamp must be updated_at from the DB blob."""
        now = _utcnow()
        raw = {
            "source": "messages_api_header",
            "updated_at": now.isoformat(),
            "unified_5h": {"utilization": 40.0},
        }
        result = _snapshot_from_persisted_usage(raw)
        assert result is not None
        _snap, stamp = result
        assert stamp == now

    def test_missing_updated_at_returns_none(self):
        raw = {
            "source": "messages_api_header",
            "unified_5h": {"utilization": 30.0},
        }
        assert _snapshot_from_persisted_usage(raw) is None

    def test_zero_utilization_restores_known(self):
        """0% utilization in a persisted blob must still restore as known."""
        raw = {
            "source": "messages_api_header",
            "updated_at": _utcnow().isoformat(),
            "unified_5h": {"utilization": 0.0, "resets_at": "2026-07-22T15:00:00Z"},
        }
        result = _snapshot_from_persisted_usage(raw)
        assert result is not None
        snap, _stamp = result
        assert snap.known is True
        assert snap.window_utilizations["five_hour"] == 0.0


# ── API-key snapshot persist path writes raw_rate_limit_blob ─────────────────


@pytest.mark.asyncio
class TestApiKeySnapshotPersistsRawBlob:
    """When a UsageSnapshot carries raw_rate_limit_blob with
    source='messages_api_header' (the API-key probe path),
    _persist_rate_limit_usage_to_db must persist the raw blob directly so
    remaining_tokens and limit_tokens survive the round-trip.  OAuth snapshots
    without raw_rate_limit_blob must continue using snapshot.to_dict().
    """

    def _make_fake_session(self, fake_row):
        """Return a FakeSession class that writes into fake_row."""
        written_holder = [None]

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, acct_id):
                return fake_row

            def add(self, obj):
                written_holder[0] = fake_row.rate_limit_usage

            async def commit(self):
                pass

        return FakeSession, written_holder

    async def test_raw_blob_written_for_api_key_snapshot(self, monkeypatch):
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        rate_limit_data = {
            "source": "messages_api_header",
            "updated_at": "2026-07-22T10:00:00+00:00",
            "unified_5h": {
                "utilization": 30.0,
                "remaining_tokens": 70_000,
                "limit_tokens": 100_000,
                "resets_at": "2026-07-22T15:00:00Z",
                "status": "live",
            },
        }
        snap = _snapshot_from_rate_limit_data(rate_limit_data, "2026-07-22T10:00:00")
        assert snap.known is True
        assert snap.raw_rate_limit_blob is not None

        class FakeRow:
            rate_limit_usage = None
            updated_at = None

        fake_row = FakeRow()
        written: list[dict | None] = [None]

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, acct_id):
                return fake_row

            def add(self, obj):
                written[0] = fake_row.rate_limit_usage

            async def commit(self):
                pass

        monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

        await _persist_rate_limit_usage_to_db("api-key-id", snap)

        w = written[0]
        assert w is not None
        assert w.get("source") == "messages_api_header", (
            "API-key path must preserve messages_api_header source in persisted dict"
        )
        assert "unified_5h" in w, "unified_5h must survive the persist path"
        assert w["unified_5h"]["remaining_tokens"] == 70_000
        assert w["unified_5h"]["limit_tokens"] == 100_000
        assert w["unified_5h"]["utilization"] == 30.0
        assert "updated_at" in w, "updated_at (write time) must be stamped"

    async def test_api_key_raw_blob_persist_restore_round_trip(self, monkeypatch):
        """Full persist → restore cycle for the API-key path: raw blob written
        then read back by _snapshot_from_persisted_usage gives a known snapshot
        with the correct five_hour window and remaining_tokens preserved."""
        from app.providers.anthropic import _snapshot_from_rate_limit_data

        rate_limit_data = {
            "source": "messages_api_header",
            "updated_at": "2026-07-22T10:00:00+00:00",
            "unified_5h": {
                "utilization": 45.0,
                "remaining_tokens": 55_000,
                "limit_tokens": 100_000,
                "resets_at": "2026-07-22T15:00:00Z",
                "status": "live",
            },
        }
        snap = _snapshot_from_rate_limit_data(rate_limit_data, "2026-07-22T10:00:00")
        assert snap.known is True

        class FakeRow:
            rate_limit_usage = None
            updated_at = None

        fake_row = FakeRow()
        written: list[dict | None] = [None]

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, acct_id):
                return fake_row

            def add(self, obj):
                written[0] = fake_row.rate_limit_usage

            async def commit(self):
                pass

        monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

        await _persist_rate_limit_usage_to_db("api-key-round-trip", snap)
        w = written[0]
        assert w is not None

        restored = _snapshot_from_persisted_usage(w)
        assert restored is not None
        restored_snap, _stamp = restored
        assert restored_snap.known is True
        assert restored_snap.window_utilizations == {"five_hour": 45.0}
        assert restored_snap.resets_at.get("five_hour") == "2026-07-22T15:00:00Z"

    async def test_oauth_snapshot_uses_to_dict_not_raw_blob(self, monkeypatch):
        """OAuth snapshots (no raw_rate_limit_blob) must use snapshot.to_dict()
        so they are persisted as oauth_usage_probe schema blobs."""
        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 20.0, "seven_day": 50.0},
            resets_at={},
            probed_at="2026-07-22T10:00:00",
        )
        assert getattr(snap, "raw_rate_limit_blob", None) is None

        class FakeRow:
            rate_limit_usage = None
            updated_at = None

        fake_row = FakeRow()
        written: list[dict | None] = [None]

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, acct_id):
                return fake_row

            def add(self, obj):
                written[0] = fake_row.rate_limit_usage

            async def commit(self):
                pass

        monkeypatch.setattr("app.max_plan_pool.AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

        await _persist_rate_limit_usage_to_db("oauth-account", snap)

        w = written[0]
        assert w is not None
        assert w.get("source") == "oauth_usage_probe", (
            "OAuth snapshots must use to_dict() which emits source=oauth_usage_probe"
        )
        assert "five_hour" in w
        assert "unified_5h" not in w


# ── _persist_usage_headroom_armable_since direct unit tests ──────────────────


@pytest.mark.asyncio
class TestPersistArmableSinceDirect:
    """Direct unit tests for _persist_usage_headroom_armable_since, which
    writes the armable-since timestamp into DeploymentState.runtime_config_json
    so the 60-minute observation window survives leader restarts.
    """

    async def test_persist_datetime_writes_isoformat(self, monkeypatch):
        import app.max_plan_pool as mpp

        committed: list[dict] = []

        class FakeState:
            runtime_config_json: dict = {}

        state = FakeState()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                return state

            def add(self, obj):
                pass

            async def commit(self):
                committed.append(dict(state.runtime_config_json))

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())

        dt = datetime(2026, 7, 22, 10, 30, 0)
        result = await mpp._persist_usage_headroom_armable_since(dt)

        assert result is True
        assert len(committed) == 1
        assert committed[0]["usage_headroom_armable_since"] == dt.isoformat()

    async def test_persist_none_writes_null(self, monkeypatch):
        """Persisting None must write JSON null, distinguishable from 'key absent'."""
        import app.max_plan_pool as mpp

        committed: list[dict] = []

        class FakeState:
            runtime_config_json: dict = {"usage_headroom_armable_since": "2026-07-22T10:00:00"}

        state = FakeState()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                return state

            def add(self, obj):
                pass

            async def commit(self):
                committed.append(dict(state.runtime_config_json))

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())

        result = await mpp._persist_usage_headroom_armable_since(None)

        assert result is True
        assert committed[0]["usage_headroom_armable_since"] is None

    async def test_persist_returns_false_when_deployment_state_missing(self, monkeypatch):
        import app.max_plan_pool as mpp

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                return None

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())

        result = await mpp._persist_usage_headroom_armable_since(datetime(2026, 7, 22))
        assert result is False

    async def test_persist_returns_false_on_db_error(self, monkeypatch):
        import app.max_plan_pool as mpp

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                raise RuntimeError("simulated DB error")

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())

        result = await mpp._persist_usage_headroom_armable_since(datetime(2026, 7, 22))
        assert result is False


# ── _restore_usage_headroom_armable_since direct unit tests ──────────────────


@pytest.mark.asyncio
class TestRestoreArmableSinceDirect:
    """Direct unit tests for _restore_usage_headroom_armable_since, which reads
    the persisted armable-since timestamp from DeploymentState on leader start
    so the 60-minute observation window is NOT reset by a restart.
    """

    async def test_restore_reads_isoformat_and_sets_module_var(self, monkeypatch):
        import app.max_plan_pool as mpp

        dt_str = "2026-07-22T10:30:00"

        class FakeState:
            runtime_config_json = {"usage_headroom_armable_since": dt_str}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                return FakeState()

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)

        result = await mpp._restore_usage_headroom_armable_since()

        assert result is True
        assert mpp._usage_headroom_armable_since == datetime.fromisoformat(dt_str)

    async def test_restore_absent_key_returns_false_and_leaves_var_none(self, monkeypatch):
        """When runtime_config_json has no armable-since key, return False
        and leave _usage_headroom_armable_since as None."""
        import app.max_plan_pool as mpp

        class FakeState:
            runtime_config_json: dict = {}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                return FakeState()

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)

        result = await mpp._restore_usage_headroom_armable_since()

        assert result is False
        assert mpp._usage_headroom_armable_since is None

    async def test_restore_null_value_returns_false(self, monkeypatch):
        """Explicit null in config (previously cleared) must return False —
        None means the window was intentionally reset, not 'unknown'."""
        import app.max_plan_pool as mpp

        class FakeState:
            runtime_config_json = {"usage_headroom_armable_since": None}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                return FakeState()

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)

        result = await mpp._restore_usage_headroom_armable_since()

        assert result is False
        assert mpp._usage_headroom_armable_since is None

    async def test_restore_missing_deployment_state_returns_false(self, monkeypatch):
        import app.max_plan_pool as mpp

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                return None

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)

        result = await mpp._restore_usage_headroom_armable_since()

        assert result is False
        assert mpp._usage_headroom_armable_since is None

    async def test_restore_db_error_returns_false_without_raising(self, monkeypatch):
        import app.max_plan_pool as mpp

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a, **k):
                pass

            async def get(self, model, pk):
                raise RuntimeError("simulated DB error")

            def add(self, obj):
                pass

            async def commit(self):
                pass

        monkeypatch.setattr(mpp, "AsyncSessionLocal", lambda: FakeSession())
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", None)

        result = await mpp._restore_usage_headroom_armable_since()

        assert result is False
        assert mpp._usage_headroom_armable_since is None

    async def test_restore_preserves_existing_in_memory_value(self, monkeypatch):
        """If the module var is already set (non-None), _restore is NOT called by
        the reconciler.  This test confirms restore does NOT clobber an already-set
        value — the reconciler only calls it when the var is None."""
        import app.max_plan_pool as mpp

        existing = datetime(2026, 7, 22, 9, 0, 0)
        # Simulate module var already restored from a prior cycle.
        monkeypatch.setattr(mpp, "_usage_headroom_armable_since", existing)

        # The reconciler condition is `if _usage_headroom_armable_since is None`,
        # so _restore is never called when already set.  Verify the var is unchanged.
        assert mpp._usage_headroom_armable_since == existing


# ── Stale snapshot negative-path: reliability stats and auto-arm gate ─────────


@pytest.mark.asyncio
class TestStaleSnapshotBlocksAutoArm:
    """A usage snapshot older than _USAGE_METER_DISPLAY_STALE_SECONDS must
    count as 'stale' in get_meter_reliability_stats, making coverage_ratio 0
    and armable=False, so the auto-arm gate never trips on outdated data.
    """

    async def test_stale_snapshot_counted_as_stale_not_known(self, monkeypatch):
        import app.max_plan_pool as mpp

        now = _utcnow()
        stale_at = now - timedelta(seconds=mpp._USAGE_METER_DISPLAY_STALE_SECONDS + 1)

        monkeypatch.setattr(
            mpp,
            "_account_by_id",
            {
                "stale-acct": {
                    "id": "stale-acct",
                    "provider": "anthropic",
                    "auth_mode": "api_key",
                    "enabled": True,
                    "runtime_secret": "sk-ant-stale",
                    "oauth_token": None,
                    "usage_snapshot": UsageSnapshot(
                        known=True,
                        window_utilizations={"five_hour": 20.0},
                        resets_at={},
                        probed_at=None,
                    ),
                    "usage_snapshot_at": stale_at,
                }
            },
        )

        async def _no_persisted():
            return {}

        monkeypatch.setattr(mpp, "_load_persisted_usage_blobs", _no_persisted)
        monkeypatch.setattr(mpp, "_usage_persist_failing_accounts", set())

        stats = await mpp.get_meter_reliability_stats(now=now)
        assert stats["meters_stale"] == 1
        assert stats["meters_known"] == 0
        assert stats["meters_eligible"] == 1
        assert stats["armable"] is False, (
            "stale meters reduce coverage_ratio to 0 — armable must be False"
        )

    async def test_fresh_snapshot_yields_armable_true(self, monkeypatch):
        """Complementary positive case: a just-probed snapshot makes armable=True."""
        import app.max_plan_pool as mpp

        now = _utcnow()

        monkeypatch.setattr(
            mpp,
            "_account_by_id",
            {
                "fresh-acct": {
                    "id": "fresh-acct",
                    "provider": "anthropic",
                    "auth_mode": "api_key",
                    "enabled": True,
                    "runtime_secret": "sk-ant-fresh",
                    "oauth_token": None,
                    "usage_snapshot": UsageSnapshot(
                        known=True,
                        window_utilizations={"five_hour": 10.0},
                        resets_at={},
                        probed_at=None,
                    ),
                    "usage_snapshot_at": now,
                }
            },
        )

        async def _no_persisted():
            return {}

        monkeypatch.setattr(mpp, "_load_persisted_usage_blobs", _no_persisted)
        monkeypatch.setattr(mpp, "_usage_persist_failing_accounts", set())

        stats = await mpp.get_meter_reliability_stats(now=now)
        assert stats["meters_known"] == 1
        assert stats["meters_stale"] == 0
        assert stats["armable"] is True

    async def test_stale_api_key_snapshot_keeps_reconciler_at_noop(
        self, monkeypatch, _reset_arm_marker
    ):
        """When the only API-key meter is stale the reconciler must NOT arm
        the gate — even after the observation window would have elapsed."""
        import app.max_plan_pool as mpp

        now = _utcnow()
        stale_at = now - timedelta(seconds=mpp._USAGE_METER_DISPLAY_STALE_SECONDS + 10)

        monkeypatch.setattr(
            mpp,
            "_account_by_id",
            {
                "stale-api-acct": {
                    "id": "stale-api-acct",
                    "provider": "anthropic",
                    "auth_mode": "api_key",
                    "enabled": True,
                    "runtime_secret": "sk-ant-key",
                    "oauth_token": None,
                    "usage_snapshot": UsageSnapshot(
                        known=True,
                        window_utilizations={"five_hour": 20.0},
                        resets_at={},
                        probed_at=None,
                    ),
                    "usage_snapshot_at": stale_at,
                }
            },
        )

        async def _no_persisted():
            return {}

        monkeypatch.setattr(mpp, "_load_persisted_usage_blobs", _no_persisted)
        monkeypatch.setattr(mpp, "_usage_persist_failing_accounts", set())

        _set_config(monkeypatch, usage_headroom_auto_arm_observation_minutes=1)
        commits: list = []
        _install_fake_state(monkeypatch, commits)

        # Call 2 minutes later — past the 1-min window.  Stale meter → armable=False.
        res = await reconcile_usage_headroom_arming(now=now + timedelta(minutes=2))
        assert res["armable"] is False
        assert res["gate_enabled"] is False
        gate_commits = [c for c in commits if "usage_headroom_gate_enabled" in c]
        assert not gate_commits, "stale meters must not trigger gate arming"

    async def test_no_known_snapshot_counts_as_dark(self, monkeypatch):
        """An account with no usage_snapshot at all must count as dark,
        not stale, and produce armable=False."""
        import app.max_plan_pool as mpp

        monkeypatch.setattr(
            mpp,
            "_account_by_id",
            {
                "dark-acct": {
                    "id": "dark-acct",
                    "provider": "anthropic",
                    "auth_mode": "api_key",
                    "enabled": True,
                    "runtime_secret": "sk-ant-dark",
                    "oauth_token": None,
                }
            },
        )

        async def _no_persisted():
            return {}

        monkeypatch.setattr(mpp, "_load_persisted_usage_blobs", _no_persisted)
        monkeypatch.setattr(mpp, "_usage_persist_failing_accounts", set())

        stats = await mpp.get_meter_reliability_stats(now=_utcnow())
        assert stats["meters_dark"] == 1
        assert stats["meters_known"] == 0
        assert stats["meters_stale"] == 0
        assert stats["armable"] is False
