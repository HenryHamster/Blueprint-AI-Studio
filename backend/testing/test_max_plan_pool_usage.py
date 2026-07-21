"""Tests for usage-meter serialization and reliable metering fixes.

Covers:
- UsageSnapshot.to_dict() / from_dict() round-trip serialization
- probed_at preserved end-to-end through persist/restore cycle
- Extra windows (beyond five_hour/seven_day) preserved
- Backwards-compat with old blobs lacking 'known'/'probed_at' fields
- _snapshot_from_persisted_usage delegates to from_dict correctly
- Dark-meter warning logged for OAuth accounts with missing secrets
"""

from datetime import datetime

import pytest

from app.providers.base import UsageSnapshot
from app.max_plan_pool import (
    _account_by_id,
    _snapshot_from_persisted_usage,
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
