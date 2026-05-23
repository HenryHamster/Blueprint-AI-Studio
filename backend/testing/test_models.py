"""Tests for constants and helper methods defined in app.models."""

import uuid
from datetime import datetime, timedelta

from app.models import (
    CYCLE_META_MEMOS_AVAILABLE,
    CYCLE_META_MEMOS_CITED,
    MaxPlanAccount,
    RegressionRemediationCase,
    StrategicCycle,
)

_PROJECT_ID = uuid.uuid4()


def _make_cycle(cycle_metadata=None):
    return StrategicCycle(
        id=str(uuid.uuid4()),
        project_id=_PROJECT_ID,
        cycle_metadata=cycle_metadata if cycle_metadata is not None else {},
    )


def test_cycle_meta_constants_have_expected_values():
    assert CYCLE_META_MEMOS_AVAILABLE == "memos_available"
    assert CYCLE_META_MEMOS_CITED == "memos_cited"


def test_memo_stats_returns_zeros_when_metadata_empty():
    cycle = _make_cycle({})
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 0
    assert stats[CYCLE_META_MEMOS_CITED] == 0


def test_memo_stats_returns_zeros_when_metadata_none():
    cycle = _make_cycle(None)
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 0
    assert stats[CYCLE_META_MEMOS_CITED] == 0


def test_memo_stats_reads_values_from_metadata():
    cycle = _make_cycle(
        {
            CYCLE_META_MEMOS_AVAILABLE: 7,
            CYCLE_META_MEMOS_CITED: 3,
        }
    )
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 7
    assert stats[CYCLE_META_MEMOS_CITED] == 3


def test_memo_stats_coerces_to_int():
    cycle = _make_cycle(
        {
            CYCLE_META_MEMOS_AVAILABLE: "4",
            CYCLE_META_MEMOS_CITED: "1",
        }
    )
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 4
    assert stats[CYCLE_META_MEMOS_CITED] == 1


# ---------------------------------------------------------------------------
# RegressionRemediationCase — per-episode investigate anchor
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, 12, 0, 0)


def _make_regression_case(investigate_spawned_at=None, fix_log=None):
    return RegressionRemediationCase(
        id=str(uuid.uuid4()),
        project_id=_PROJECT_ID,
        regression_class="test_class",
        first_seen_at=_T0,
        investigate_spawned_at=investigate_spawned_at,
        fingerprint_to_fix_log=fix_log or [],
    )


def test_current_episode_investigate_start_returns_none_when_no_anchor():
    case = _make_regression_case(investigate_spawned_at=None, fix_log=[])
    assert case._current_episode_investigate_start() is None


def test_current_episode_investigate_start_first_episode():
    inv = _T0 + timedelta(minutes=5)
    case = _make_regression_case(investigate_spawned_at=inv)
    assert case._current_episode_investigate_start() == inv


def test_current_episode_investigate_start_advances_after_recovery():
    inv = _T0 + timedelta(minutes=5)
    recovered = _T0 + timedelta(hours=2)
    case = _make_regression_case(
        investigate_spawned_at=inv,
        fix_log=[
            {
                "fingerprint": "test_class",
                "fix_pr_number": 1,
                "merged_at": (_T0 + timedelta(hours=1)).isoformat(),
            },
            {
                "fingerprint": "test_class",
                "recovered_at": recovered.isoformat(),
                "post_merge_value": 0.1,
            },
        ],
    )
    # After a recovery the anchor should be the recovered_at, not the original investigate_spawned_at
    assert case._current_episode_investigate_start() == recovered


def test_mark_merged_records_episode_investigate_started_at_first_episode():
    inv = _T0 + timedelta(minutes=5)
    case = _make_regression_case(investigate_spawned_at=inv)
    case.mark_merged(pr_number=42, signal_snapshot={"value": 0.9})
    entry = case.fingerprint_to_fix_log[-1]
    assert "episode_investigate_started_at" in entry
    assert datetime.fromisoformat(entry["episode_investigate_started_at"]) == inv


# ---------------------------------------------------------------------------
# MaxPlanAccount.is_in_rate_limit_cooldown — persisted-state check
# ---------------------------------------------------------------------------


def _make_account(rate_limit_state=None):
    return MaxPlanAccount(
        user_id="test-user",
        name="test-account",
        rate_limit_state=rate_limit_state,
    )


def test_is_in_rate_limit_cooldown_no_state():
    acct = _make_account(rate_limit_state=None)
    assert acct.is_in_rate_limit_cooldown() is False


def test_is_in_rate_limit_cooldown_empty_disabled_models():
    acct = _make_account(rate_limit_state={"disabled_models": {}})
    assert acct.is_in_rate_limit_cooldown() is False


def test_is_in_rate_limit_cooldown_active_entry():
    """An entry disabled 5 minutes ago (within 4-hour window) → True."""
    at = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {"at": at, "hits": 1, "kind": "rate_limit"}
            }
        }
    )
    assert acct.is_in_rate_limit_cooldown() is True


def test_is_in_rate_limit_cooldown_expired_entry():
    """An entry disabled 5 hours ago (beyond 4-hour window) → False."""
    at = (datetime.utcnow() - timedelta(hours=5)).isoformat()
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {
                "claude-opus-4-7": {"at": at, "hits": 2, "kind": "rate_limit"}
            }
        }
    )
    assert acct.is_in_rate_limit_cooldown() is False


def test_is_in_rate_limit_cooldown_malformed_at_field():
    """Unparseable at string → False (no exception raised)."""
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {"claude-sonnet-4-6": {"at": "not-a-date"}}
        }
    )
    assert acct.is_in_rate_limit_cooldown() is False


def test_mark_merged_records_episode_investigate_started_at_re_armed():
    inv = _T0 + timedelta(minutes=5)
    recovered = _T0 + timedelta(hours=2)
    case = _make_regression_case(
        investigate_spawned_at=inv,
        fix_log=[
            {
                "fingerprint": "test_class",
                "fix_pr_number": 1,
                "merged_at": (_T0 + timedelta(hours=1)).isoformat(),
            },
            {
                "fingerprint": "test_class",
                "recovered_at": recovered.isoformat(),
                "post_merge_value": 0.1,
            },
        ],
    )
    case.mark_merged(pr_number=99, signal_snapshot={"value": 0.8})
    entry = case.fingerprint_to_fix_log[-1]
    # Second episode should anchor on recovered_at, not the original investigate_spawned_at
    assert datetime.fromisoformat(entry["episode_investigate_started_at"]) == recovered


# ---------------------------------------------------------------------------
# RegressionRemediationCase.mark_merged — merge_sha ancestry audit trail
# ---------------------------------------------------------------------------


def test_mark_merged_stores_fix_merge_sha_when_provided():
    """fix_merge_sha is recorded in the fix-log entry when a merge commit SHA is given."""
    case = _make_regression_case(investigate_spawned_at=_T0)
    case.mark_merged(
        pr_number=42, signal_snapshot={"value": 0.9}, merge_sha="abc123def456"
    )
    entry = case.fingerprint_to_fix_log[-1]
    assert entry.get("fix_merge_sha") == "abc123def456"


def test_mark_merged_omits_fix_merge_sha_when_not_provided():
    """fix_merge_sha key is absent from the fix-log entry when no SHA is passed (backward compat)."""
    case = _make_regression_case(investigate_spawned_at=_T0)
    case.mark_merged(pr_number=42, signal_snapshot={"value": 0.9})
    entry = case.fingerprint_to_fix_log[-1]
    assert "fix_merge_sha" not in entry


def test_mark_merged_fix_merge_sha_coexists_with_other_fields():
    """fix_merge_sha is stored alongside layers_targeted and episode anchor without conflict."""
    case = _make_regression_case(investigate_spawned_at=_T0)
    case.mark_merged(
        pr_number=55,
        signal_snapshot={"value": 0.7},
        layers_targeted=["PROMPTS", "TESTS"],
        merge_sha="deadbeef1234",
    )
    entry = case.fingerprint_to_fix_log[-1]
    assert entry.get("fix_merge_sha") == "deadbeef1234"
    assert entry.get("layers_targeted") == ["PROMPTS", "TESTS"]
    assert "episode_investigate_started_at" in entry
