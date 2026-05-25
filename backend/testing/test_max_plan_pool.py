"""Focused unit tests for pure/synchronous functions in app/max_plan_pool.py.

These tests avoid the in-memory pool dicts and DB — they construct account
dicts directly and pass an explicit `now` where the function supports it,
keeping assertions deterministic without fixtures or locks.
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.max_plan_pool import (
    _HEALTH_SCORE_DISQUALIFY_THRESHOLD,
    _HEALTH_WINDOW_SECONDS,
    _LATENCY_MIN_SAMPLES,
    _OUTCOME_WINDOW_SECONDS,
    _QUARANTINE_COOLDOWN_SECONDS,
    _QUOTA_EXHAUSTED_COOLDOWN_SECONDS,
    _account_at_cap,
    _account_available_for_model,
    _account_by_id,
    _check_quarantine_trigger,
    _compute_avg_duration_24h,
    _compute_health_score,
    _compute_success_rate_7d,
    _compute_success_rate_7d_for_type,
    _effective_success_rate,
    _cooldown_seconds_for_info,
    _daily_reset_if_elapsed,
    _max_plan_pools,
    _normalize_rl_model,
    _pending_rl_persists,
    _prune_health_events,
    _serialize_disabled_model_info,
    cascade_disabled_models,
    describe_pool_blockage,
    disable_account_for_auth,
    disable_account_model,
    get_account_health_stats,
    get_all_account_health_summaries,
    select_max_plan_account_balanced,
    summarize_pool_state,
)

OPUS = "claude-opus-4-7"
HAIKU = "claude-haiku-4-5-20251001"


def _acct(**kw) -> dict:
    base = {
        "id": "a",
        "name": "a",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
    }
    base.update(kw)
    return base


# ── _normalize_rl_model ─────────────────────────────────────────────────────


class TestNormalizeRlModel:
    def test_haiku_maps_to_opus(self):
        assert _normalize_rl_model(HAIKU) == OPUS

    def test_opus_passes_through(self):
        assert _normalize_rl_model(OPUS) == OPUS

    def test_unknown_model_passes_through(self):
        assert _normalize_rl_model("claude-some-new-model") == "claude-some-new-model"

    def test_none_stays_none(self):
        assert _normalize_rl_model(None) is None


# ── cascade_disabled_models ─────────────────────────────────────────────────


class TestCascadeDisabledModels:
    def test_opus_cascades_to_opus(self):
        assert cascade_disabled_models(OPUS) == (OPUS,)

    def test_haiku_normalizes_then_cascades(self):
        # Haiku shares Opus's meter — the cascade keys on Opus, not Haiku.
        assert cascade_disabled_models(HAIKU) == (OPUS,)

    def test_unknown_model_falls_back_to_self(self):
        # Unknown variants must not silently cascade to the whole account.
        assert cascade_disabled_models("claude-future") == ("claude-future",)


# ── _compute_health_score ───────────────────────────────────────────────────


class TestComputeHealthScore:
    def test_empty_history_scores_zero(self):
        assert _compute_health_score(_acct()) == 0.0

    def test_formula_completions_minus_twice_rl_events(self):
        now = datetime.utcnow()
        acct = _acct(
            completion_events=[
                {"at": now - timedelta(hours=1), "duration_seconds": 60.0},
                {"at": now - timedelta(hours=2), "duration_seconds": 60.0},
                {"at": now - timedelta(hours=3), "duration_seconds": 60.0},
            ],
            rl_events=[now - timedelta(hours=4)],
        )
        # 3 completions - 2*1 rl_event = 1.0
        assert _compute_health_score(acct, now=now) == 1.0

    def test_events_outside_24h_window_excluded(self):
        now = datetime.utcnow()
        old = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS + 60)
        acct = _acct(
            completion_events=[{"at": old, "duration_seconds": 60.0}],
            rl_events=[old],
        )
        assert _compute_health_score(acct, now=now) == 0.0


# ── get_account_health_stats ────────────────────────────────────────────────


class TestGetAccountHealthStats:
    def test_structure_contains_expected_keys(self):
        stats = get_account_health_stats(_acct())
        assert set(stats.keys()) == {
            "rl_events_24h",
            "completions_24h",
            "avg_duration_seconds",
            "health_score",
            "success_rate_7d",
            "success_rate_by_type_7d",
        }

    def test_aggregates_counts_and_durations(self):
        now = datetime.utcnow()
        acct = _acct(
            completion_events=[
                {"at": now - timedelta(hours=1), "duration_seconds": 60.0},
                {"at": now - timedelta(hours=2), "duration_seconds": 120.0},
            ],
            rl_events=[now - timedelta(hours=3)],
        )
        stats = get_account_health_stats(acct, now=now)
        assert stats["completions_24h"] == 2
        assert stats["rl_events_24h"] == 1
        assert stats["avg_duration_seconds"] == 90.0
        assert stats["health_score"] == 2 - 2.0  # 2 completions - 2*1 rl = 0.0

    def test_avg_duration_none_when_no_completions(self):
        assert get_account_health_stats(_acct())["avg_duration_seconds"] is None


# ── _prune_health_events ────────────────────────────────────────────────────


class TestPruneHealthEvents:
    def test_removes_events_older_than_cutoff(self):
        now = datetime.utcnow()
        old = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS + 60)
        fresh = now - timedelta(hours=1)
        acct = _acct(
            completion_events=[
                {"at": old, "duration_seconds": 1.0},
                {"at": fresh, "duration_seconds": 2.0},
            ],
            rl_events=[old, fresh],
        )
        _prune_health_events(acct, now)
        assert len(acct["completion_events"]) == 1
        assert acct["completion_events"][0]["at"] == fresh
        assert acct["rl_events"] == [fresh]

    def test_noop_when_lists_absent(self):
        acct = _acct()
        _prune_health_events(acct, datetime.utcnow())
        assert "rl_events" not in acct
        assert "completion_events" not in acct


# ── _daily_reset_if_elapsed ─────────────────────────────────────────────────


class TestDailyResetIfElapsed:
    def test_seeds_reset_at_when_missing(self):
        now = datetime.utcnow()
        acct = _acct(daily_task_count=3)
        assert _daily_reset_if_elapsed(acct, now) is False
        assert acct["daily_task_count_reset_at"] == now
        # Existing count stays — we only seed the window marker.
        assert acct["daily_task_count"] == 3

    def test_no_reset_within_window(self):
        now = datetime.utcnow()
        acct = _acct(
            daily_task_count=5, daily_task_count_reset_at=now - timedelta(hours=12)
        )
        assert _daily_reset_if_elapsed(acct, now) is False
        assert acct["daily_task_count"] == 5

    def test_resets_after_window_elapsed(self):
        now = datetime.utcnow()
        acct = _acct(
            daily_task_count=42, daily_task_count_reset_at=now - timedelta(hours=25)
        )
        assert _daily_reset_if_elapsed(acct, now) is True
        assert acct["daily_task_count"] == 0
        assert acct["daily_task_count_reset_at"] == now


# ── _account_at_cap ─────────────────────────────────────────────────────────


class TestAccountAtCap:
    def test_no_caps_configured_returns_none(self):
        assert _account_at_cap(_acct()) is None

    def test_daily_cap_reached_returns_reason(self):
        acct = _acct(daily_task_cap=10, daily_task_count=10)
        reason = _account_at_cap(acct)
        assert reason is not None
        assert "daily cap" in reason

    def test_daily_cap_under_limit_returns_none(self):
        acct = _acct(daily_task_cap=10, daily_task_count=9)
        assert _account_at_cap(acct) is None

    def test_concurrent_cap_reached_returns_reason(self):
        acct = _acct(max_concurrent_tasks=2, active_tasks=2)
        reason = _account_at_cap(acct)
        assert reason is not None
        assert "concurrent cap" in reason

    def test_concurrent_cap_under_limit_returns_none(self):
        acct = _acct(max_concurrent_tasks=2, active_tasks=1)
        assert _account_at_cap(acct) is None


# ── _account_available_for_model ────────────────────────────────────────────


class TestAccountAvailableForModel:
    def test_available_when_healthy(self):
        assert _account_available_for_model(_acct(), OPUS) is True

    def test_unavailable_when_disabled_flag_false(self):
        assert _account_available_for_model(_acct(enabled=False), OPUS) is False

    def test_unavailable_when_missing_token(self):
        assert _account_available_for_model(_acct(oauth_token=None), OPUS) is False

    def test_unavailable_during_active_cooldown(self):
        # Fresh disable → still within cooldown window.
        acct = _acct(disabled_models={OPUS: {"hits": 1, "at": datetime.utcnow()}})
        assert _account_available_for_model(acct, OPUS) is False

    def test_available_as_probing_after_cooldown_elapses(self):
        # hits=1 → 30 min cooldown; 60 min ago → cooldown elapsed → probing (still "available").
        acct = _acct(
            disabled_models={
                OPUS: {"hits": 1, "at": datetime.utcnow() - timedelta(minutes=60)}
            }
        )
        assert _account_available_for_model(acct, OPUS) is True


# ── describe_pool_blockage ──────────────────────────────────────────────────


class TestDescribePoolBlockage:
    def _reset_pools(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    def test_no_accounts_configured(self):
        self._reset_pools()
        try:
            assert (
                describe_pool_blockage(OPUS, user_id="nobody")
                == "no accounts configured"
            )
        finally:
            self._reset_pools()

    def test_all_rate_limited_reports_earliest_recovery(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            acct = _acct(
                id="r1",
                user_id="u",
                disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}},
            )
            _account_by_id[acct["id"]] = acct
            _max_plan_pools.setdefault("u", []).append(acct)
            msg = describe_pool_blockage(OPUS, user_id="u")
            assert msg.startswith("all accounts in rate-limit cooldown")
            assert "earliest recovery" in msg
        finally:
            self._reset_pools()

    def test_partial_rate_limit_mentions_counts(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            healthy = _acct(id="h", user_id="u")
            rl = _acct(
                id="r", user_id="u", disabled_models={OPUS: {"hits": 1, "at": now}}
            )
            for a in (healthy, rl):
                _account_by_id[a["id"]] = a
                _max_plan_pools.setdefault("u", []).append(a)
            msg = describe_pool_blockage(OPUS, user_id="u")
            assert "1 of 2 accounts rate-limited" in msg
        finally:
            self._reset_pools()

    def test_no_rate_limits_reports_slot_contention(self):
        self._reset_pools()
        try:
            acct = _acct(id="h", user_id="u")
            _account_by_id[acct["id"]] = acct
            _max_plan_pools.setdefault("u", []).append(acct)
            assert (
                describe_pool_blockage(OPUS, user_id="u")
                == "no eligible accounts or slot contention"
            )
        finally:
            self._reset_pools()


# ── summarize_pool_state ────────────────────────────────────────────────────


class TestSummarizePoolState:
    def _reset_pools(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    def test_aggregates_healthy_rate_limited_and_other(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            healthy = _acct(id="h", user_id="u")
            rl = _acct(
                id="r",
                user_id="u",
                disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}},
            )
            no_token = _acct(id="n", user_id="u", oauth_token=None)
            for a in (healthy, rl, no_token):
                _account_by_id[a["id"]] = a
                _max_plan_pools.setdefault("u", []).append(a)
            summary = summarize_pool_state(OPUS, user_id="u")
            assert summary["total"] == 3
            assert summary["healthy"] == 1
            assert summary["rate_limited"] == 1
            assert summary["other_unavailable"] == 1
            assert summary["earliest_recovery"] is not None
            assert summary["earliest_recovery"] > now
        finally:
            self._reset_pools()

    def test_empty_pool_returns_zero_counts(self):
        self._reset_pools()
        try:
            summary = summarize_pool_state(OPUS, user_id="missing")
            assert summary["total"] == 0
            assert summary["healthy"] == 0
            assert summary["rate_limited"] == 0
            assert summary["earliest_recovery"] is None
        finally:
            self._reset_pools()


# ── _HEALTH_SCORE_DISQUALIFY_THRESHOLD constant ──────────────────────────────


class TestHealthScoreDisqualifyThreshold:
    def test_threshold_is_negative(self):
        assert _HEALTH_SCORE_DISQUALIFY_THRESHOLD < 0

    def test_score_at_threshold_minus_one_is_below(self):
        """An account with score below threshold would be filtered when alternatives exist."""
        now = datetime.utcnow()
        # Build an account whose health score lands below _HEALTH_SCORE_DISQUALIFY_THRESHOLD.
        # score = completions - 2*rl_events; 3 rl_events → score = -6, which is below -4.
        acct = _acct(
            rl_events=[now - timedelta(hours=i) for i in range(1, 4)],
        )
        score = _compute_health_score(acct, now=now)
        assert score < _HEALTH_SCORE_DISQUALIFY_THRESHOLD

    def test_score_at_threshold_is_eligible(self):
        """An account with score == threshold is NOT filtered."""
        now = datetime.utcnow()
        # score = 0 completions - 2*2 rl_events = -4 == threshold → eligible
        acct = _acct(
            rl_events=[now - timedelta(hours=i) for i in range(1, 3)],
        )
        score = _compute_health_score(acct, now=now)
        assert score == _HEALTH_SCORE_DISQUALIFY_THRESHOLD


# ── _cooldown_seconds_for_info ───────────────────────────────────────────────


class TestCooldownSecondsForInfo:
    def test_exponential_curve_default(self):
        # hits=1 → 30 min
        assert _cooldown_seconds_for_info({"hits": 1}) == 30 * 60
        # hits=2 → 60 min
        assert _cooldown_seconds_for_info({"hits": 2}) == 60 * 60
        # hits=5 → capped at 240 min
        assert _cooldown_seconds_for_info({"hits": 5}) == 240 * 60
        # hits=10 → still capped
        assert _cooldown_seconds_for_info({"hits": 10}) == 240 * 60

    def test_quota_exhausted_flat_24h(self):
        """Billing-wall accounts get a flat 24h cooldown regardless of hit count."""
        assert (
            _cooldown_seconds_for_info({"hits": 1, "kind": "quota_exhausted"})
            == _QUOTA_EXHAUSTED_COOLDOWN_SECONDS
        )
        # Even high hit counts stay flat at 24h (not the 4h exponential cap).
        assert (
            _cooldown_seconds_for_info({"hits": 99, "kind": "quota_exhausted"})
            == _QUOTA_EXHAUSTED_COOLDOWN_SECONDS
        )

    def test_retry_after_seconds_is_floor(self):
        """Server-provided retry hint lifts the exponential cooldown when larger."""
        # hits=1 → exponential=1800s. Hint of 3600s should win.
        assert (
            _cooldown_seconds_for_info({"hits": 1, "retry_after_seconds": 3600}) == 3600
        )
        # Hint smaller than exponential is ignored.
        assert (
            _cooldown_seconds_for_info({"hits": 1, "retry_after_seconds": 60})
            == 30 * 60
        )
        # Non-numeric hint is ignored.
        assert (
            _cooldown_seconds_for_info({"hits": 1, "retry_after_seconds": "unknown"})
            == 30 * 60
        )


# ── _serialize_disabled_model_info ──────────────────────────────────────────


class TestSerializeDisabledModelInfo:
    def test_minimal_entry(self):
        at = datetime(2026, 4, 22, 3, 0, tzinfo=timezone.utc)
        out = _serialize_disabled_model_info({"hits": 2, "at": at})
        assert out == {"hits": 2, "at": at.isoformat()}

    def test_preserves_quota_exhausted_kind(self):
        at = datetime(2026, 4, 22, 3, 0, tzinfo=timezone.utc)
        out = _serialize_disabled_model_info(
            {"hits": 1, "at": at, "kind": "quota_exhausted"}
        )
        assert out["kind"] == "quota_exhausted"

    def test_preserves_quarantine_kind(self):
        at = datetime(2026, 4, 22, 3, 0, tzinfo=timezone.utc)
        out = _serialize_disabled_model_info(
            {"hits": 1, "at": at, "kind": "quarantine"}
        )
        assert out["kind"] == "quarantine"

    def test_preserves_positive_retry_hint(self):
        at = datetime(2026, 4, 22, 3, 0, tzinfo=timezone.utc)
        out = _serialize_disabled_model_info(
            {"hits": 1, "at": at, "retry_after_seconds": 120}
        )
        assert out["retry_after_seconds"] == 120

    def test_omits_zero_or_negative_retry_hint(self):
        at = datetime(2026, 4, 22, 3, 0, tzinfo=timezone.utc)
        out = _serialize_disabled_model_info(
            {"hits": 1, "at": at, "retry_after_seconds": 0}
        )
        assert "retry_after_seconds" not in out

    def test_omits_unknown_kind(self):
        """Only quota_exhausted is preserved; rate_limit is implicit (omitted)."""
        at = datetime(2026, 4, 22, 3, 0, tzinfo=timezone.utc)
        out = _serialize_disabled_model_info(
            {"hits": 1, "at": at, "kind": "rate_limit"}
        )
        assert "kind" not in out


# ── get_all_account_health_summaries ─────────────────────────────────────────


class TestGetAllAccountHealthSummaries:
    def _reset_pools(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    def test_returns_empty_for_unknown_user(self):
        self._reset_pools()
        try:
            assert get_all_account_health_summaries(user_id="nobody") == []
        finally:
            self._reset_pools()

    def test_healthy_account_has_expected_keys(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            acct = _acct(id="h1", user_id="u", name="TestAcct")
            _account_by_id["h1"] = acct
            _max_plan_pools["u"] = [acct]
            summaries = get_all_account_health_summaries(user_id="u", now=now)
            assert len(summaries) == 1
            s = summaries[0]
            assert s["state"] == "healthy"
            assert s["health_score"] == 0.0
            assert s["rate_limited_models"] == []
            assert s["disqualify_threshold"] == _HEALTH_SCORE_DISQUALIFY_THRESHOLD
            assert s["name"] == "TestAcct"
            assert s["id"] == "h1"
        finally:
            self._reset_pools()

    def test_rate_limited_account_shows_cooldown(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            acct = _acct(
                id="r1",
                user_id="u",
                disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}},
            )
            _account_by_id["r1"] = acct
            _max_plan_pools["u"] = [acct]
            summaries = get_all_account_health_summaries(user_id="u", now=now)
            s = summaries[0]
            assert s["state"] == "unavailable"
            assert len(s["rate_limited_models"]) == 1
            rl = s["rate_limited_models"][0]
            assert rl["model"] == OPUS
            assert rl["kind"] == "rate_limit"
            assert rl["cooldown_remaining_seconds"] > 0
        finally:
            self._reset_pools()

    def test_disabled_account_shows_fully_disabled(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            acct = _acct(
                id="d1",
                user_id="u",
                enabled=False,
                disabled_reason="auto-disabled after 5 rate-limit hits",
            )
            _account_by_id["d1"] = acct
            _max_plan_pools["u"] = [acct]
            summaries = get_all_account_health_summaries(user_id="u", now=now)
            s = summaries[0]
            assert s["state"] == "fully_disabled"
            assert s["disabled_reason"] is not None
        finally:
            self._reset_pools()

    def test_low_health_score_shows_disqualified(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            # 3 rl_events → score = -6, below threshold of -4
            acct = _acct(
                id="q1",
                user_id="u",
                rl_events=[now - timedelta(hours=i) for i in range(1, 4)],
            )
            _account_by_id["q1"] = acct
            _max_plan_pools["u"] = [acct]
            summaries = get_all_account_health_summaries(user_id="u", now=now)
            s = summaries[0]
            assert s["state"] == "disqualified"
            assert s["health_score"] < _HEALTH_SCORE_DISQUALIFY_THRESHOLD
        finally:
            self._reset_pools()

    def test_quota_exhausted_distinct_from_rate_limit(self):
        """quota_exhausted kind is preserved in rate_limited_models and has 24h cooldown."""
        self._reset_pools()
        try:
            now = datetime.utcnow()
            acct = _acct(
                id="qe1",
                user_id="u",
                disabled_models={
                    OPUS: {
                        "hits": 1,
                        "at": now - timedelta(minutes=5),
                        "kind": "quota_exhausted",
                    }
                },
            )
            _account_by_id["qe1"] = acct
            _max_plan_pools["u"] = [acct]
            summaries = get_all_account_health_summaries(user_id="u", now=now)
            s = summaries[0]
            rl = s["rate_limited_models"][0]
            assert rl["kind"] == "quota_exhausted"
            assert rl["cooldown_remaining_seconds"] > 23 * 3600
        finally:
            self._reset_pools()

    def test_success_rate_7d_surfaced_in_summary(self):
        """success_rate_7d appears in each account summary entry."""
        self._reset_pools()
        try:
            now = datetime.utcnow()
            acct = _acct(
                id="s1",
                user_id="u",
                outcome_events=[
                    {"at": now - timedelta(hours=i + 1), "success": i < 4}
                    for i in range(5)
                ],
            )
            _account_by_id["s1"] = acct
            _max_plan_pools["u"] = [acct]
            summaries = get_all_account_health_summaries(user_id="u", now=now)
            assert "success_rate_7d" in summaries[0]
            assert summaries[0]["success_rate_7d"] == 4 / 5
        finally:
            self._reset_pools()


# ── _compute_success_rate_7d ─────────────────────────────────────────────────


class TestComputeSuccessRate7d:
    def test_returns_none_when_no_events(self):
        assert _compute_success_rate_7d(_acct()) is None

    def test_returns_none_when_fewer_than_3_events(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=1), "success": True},
                {"at": now - timedelta(hours=2), "success": False},
            ]
        )
        assert _compute_success_rate_7d(acct, now) is None

    def test_returns_rate_from_3_plus_events(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=1), "success": True},
                {"at": now - timedelta(hours=2), "success": True},
                {"at": now - timedelta(hours=3), "success": False},
            ]
        )
        rate = _compute_success_rate_7d(acct, now)
        assert rate == 2 / 3

    def test_excludes_events_older_than_7d(self):
        now = datetime.utcnow()
        old = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS + 60)
        acct = _acct(
            outcome_events=[
                {"at": old, "success": False},
                {"at": old, "success": False},
                {"at": old, "success": False},
            ]
        )
        assert _compute_success_rate_7d(acct, now) is None

    def test_perfect_success_rate(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
            ]
        )
        assert _compute_success_rate_7d(acct, now) == 1.0

    def test_zero_success_rate(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": False} for i in range(3)
            ]
        )
        assert _compute_success_rate_7d(acct, now) == 0.0


# ── _compute_success_rate_7d — per-task-type scoping ─────────────────────────


class TestComputeSuccessRateByType:
    """TDD specs for task_type-scoped success rate (cluster 1/2 implements the kwarg).

    xfail tests auto-promote to XPASS once _compute_success_rate_7d gains a
    ``task_type`` kwarg that returns per-type rates with aggregate fallback.
    The stable test (no task_type arg) must pass both before and after.
    """

    @pytest.mark.xfail(strict=False, reason="task_type kwarg added by cluster 1/2")
    def test_per_type_rate_when_confident(self):
        """≥3 type-matching events → per-type rate, not the aggregate."""
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=(
                [
                    {
                        "at": now - timedelta(hours=i + 1),
                        "success": True,
                        "task_type": "create",
                    }
                    for i in range(5)
                ]
                + [
                    {
                        "at": now - timedelta(hours=i + 6),
                        "success": False,
                        "task_type": "review",
                    }
                    for i in range(5)
                ]
            )
        )
        # aggregate = 5/10 = 0.5; create-type = 5/5 = 1.0
        rate = _compute_success_rate_7d(acct, task_type="create", now=now)
        assert rate == 1.0

    @pytest.mark.xfail(strict=False, reason="task_type kwarg added by cluster 1/2")
    def test_sparse_type_falls_back_to_aggregate(self):
        """< 3 type-matching events → aggregate rate used as fallback."""
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=(
                [
                    {
                        "at": now - timedelta(hours=1),
                        "success": True,
                        "task_type": "create",
                    },
                    {
                        "at": now - timedelta(hours=2),
                        "success": False,
                        "task_type": "create",
                    },
                ]
                + [
                    {
                        "at": now - timedelta(hours=i + 3),
                        "success": True,
                        "task_type": "review",
                    }
                    for i in range(5)
                ]
            )
        )
        # 2 "create" events (< 3 floor) → fallback to aggregate (6/7)
        rate = _compute_success_rate_7d(acct, task_type="create", now=now)
        assert rate is not None
        assert abs(rate - 6 / 7) < 1e-9

    def test_no_task_type_returns_aggregate_unchanged(self):
        """Omitting task_type returns the aggregate — backward compatible."""
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=(
                [
                    {
                        "at": now - timedelta(hours=i + 1),
                        "success": True,
                        "task_type": "create",
                    }
                    for i in range(3)
                ]
                + [
                    {
                        "at": now - timedelta(hours=i + 4),
                        "success": False,
                        "task_type": "review",
                    }
                    for i in range(3)
                ]
            )
        )
        # 3 successes out of 6 total = 0.5 aggregate
        rate = _compute_success_rate_7d(acct, now=now)
        assert rate is not None
        assert abs(rate - 0.5) < 1e-9


# ── selection tiebreaking by success rate ─────────────────────────────────────


class TestSuccessRateTiebreaking:
    def test_higher_success_rate_scores_higher(self):
        """_compute_success_rate_7d should rank high-success above low-success accounts."""
        now = datetime.utcnow()
        good = _acct(
            id="good",
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
            ],
        )
        bad = _acct(
            id="bad",
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": i < 1}  # 1/5 success
                for i in range(5)
            ],
        )
        good_rate = _compute_success_rate_7d(good, now)
        bad_rate = _compute_success_rate_7d(bad, now)
        assert good_rate is not None and bad_rate is not None
        assert good_rate > bad_rate

    def test_no_data_accounts_score_zero(self):
        """Accounts with no outcome history contribute 0.0 (or 0.0 via `or`) to key."""
        now = datetime.utcnow()
        assert (_compute_success_rate_7d(_acct(id="a"), now) or 0.0) == 0.0
        assert (_compute_success_rate_7d(_acct(id="b"), now) or 0.0) == 0.0

    def test_selection_key_prefers_high_success(self):
        """The selection key (health, success_rate, -active_tasks) ranks the
        high-success account ahead of the low-success account."""
        now = datetime.utcnow()
        hi = _acct(
            id="hi",
            active_tasks=1,
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
            ],
        )
        lo = _acct(
            id="lo",
            active_tasks=0,
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": False} for i in range(5)
            ],
        )

        def key(a):
            return (
                _compute_health_score(a, now),
                _compute_success_rate_7d(a, now) or 0.0,
                -a.get("active_tasks", 0),
            )

        assert key(hi) > key(lo)

    def test_low_sample_below_min_treated_same_as_no_data(self):
        """An account with fewer than MIN_SAMPLES (3) events returns None from
        _compute_success_rate_7d — identical to an account with zero events.
        Both contribute 0.0 to the selection key via `or 0.0`, so a new account
        is never penalised relative to another new account."""
        now = datetime.utcnow()
        low_sample = _acct(
            id="new",
            outcome_events=[
                {"at": now - timedelta(hours=1), "success": False},
                {"at": now - timedelta(hours=2), "success": True},
            ],
        )  # 2 events — below MIN_SAMPLES threshold
        no_data = _acct(id="fresh")  # 0 events
        assert _compute_success_rate_7d(low_sample, now) is None
        assert _compute_success_rate_7d(no_data, now) is None
        # Both produce the same key contribution.
        assert (_compute_success_rate_7d(low_sample, now) or 0.0) == (
            _compute_success_rate_7d(no_data, now) or 0.0
        )

    def test_equal_success_rate_tiebreaks_by_active_tasks(self):
        """When two accounts have the same success rate, the one with fewer
        active_tasks ranks higher in the selection key."""
        now = datetime.utcnow()
        busy = _acct(
            id="busy",
            active_tasks=3,
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(4)
            ],
        )
        idle = _acct(
            id="idle",
            active_tasks=0,
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(4)
            ],
        )
        assert _compute_success_rate_7d(busy, now) == _compute_success_rate_7d(
            idle, now
        )

        def key(a):
            return (
                _compute_health_score(a, now),
                _compute_success_rate_7d(a, now) or 0.0,
                -a.get("active_tasks", 0),
            )

        assert key(idle) > key(busy)


# ── select_max_plan_account_balanced — success-rate bias ─────────────────────


class TestBalancedSuccessRateBias:
    """Tests for the pool-median success-rate filter in select_max_plan_account_balanced."""

    _USER = "test-balanced-bias-user"

    def _acct_with_rate(
        self, id_, success_rate=None, active_tasks=0, provider="anthropic"
    ):
        now = datetime.utcnow()
        acct = {
            "id": id_,
            "name": id_,
            "enabled": True,
            "active_tasks": active_tasks,
            "oauth_token": "sk-ant-test",
            "provider": provider,
        }
        if success_rate is not None:
            n = round(success_rate * 5)
            acct["outcome_events"] = [
                {"at": now - timedelta(hours=i + 1), "success": i < n} for i in range(5)
            ]
        return acct

    def setup_method(self):
        _max_plan_pools.pop(self._USER, None)

    def teardown_method(self):
        _max_plan_pools.pop(self._USER, None)

    async def test_low_success_rate_account_deprioritized(self):
        """Account with 7d rate well below pool median is excluded when a better one exists."""
        good = self._acct_with_rate("good", success_rate=1.0)
        bad = self._acct_with_rate("bad", success_rate=0.0)
        _max_plan_pools[self._USER] = [good, bad]
        result = await select_max_plan_account_balanced(
            self._USER, [("anthropic", None)]
        )
        assert result is not None
        assert result[0]["id"] == "good"

    async def test_sparse_account_not_penalized_by_filter(self):
        """Account with fewer than 3 outcomes (rate=None) is never excluded by the filter."""
        sparse = self._acct_with_rate(
            "sparse", active_tasks=0
        )  # no outcome_events → None
        rich = self._acct_with_rate("rich", success_rate=1.0, active_tasks=1)
        _max_plan_pools[self._USER] = [sparse, rich]
        result = await select_max_plan_account_balanced(
            self._USER, [("anthropic", None)]
        )
        assert result is not None
        # sparse wins on load (active_tasks=0 < 1); verifies it wasn't filtered out
        assert result[0]["id"] == "sparse"

    async def test_filter_skipped_with_single_candidate(self):
        """With only one candidate the filter is never applied; any success rate is ok."""
        only = self._acct_with_rate("only", success_rate=0.0)
        _max_plan_pools[self._USER] = [only]
        result = await select_max_plan_account_balanced(
            self._USER, [("anthropic", None)]
        )
        assert result is not None
        assert result[0]["id"] == "only"

    async def test_margin_constant_used_as_threshold(self):
        """Account just at (median - margin) is kept; account just below is excluded."""
        # Two accounts: rates 1.0 and 0.79 → median=1.0, threshold=0.80
        # 0.79 < 0.80 → excluded when alternatives exist
        hi = self._acct_with_rate("hi", success_rate=1.0)
        # 4/5 = 0.80 exactly — at threshold, should be kept
        edge = self._acct_with_rate("edge", success_rate=0.8)
        _max_plan_pools[self._USER] = [hi, edge]
        result = await select_max_plan_account_balanced(
            self._USER, [("anthropic", None)]
        )
        assert result is not None
        # edge (rate=0.8) >= 1.0 - 0.20 = 0.80 → kept; winner is hi (also at 0 active_tasks)
        # both kept means result can be either; just assert no crash and winner is valid
        assert result[0]["id"] in ("hi", "edge")


# ── _check_quarantine_trigger + disable_account_model quarantine integration ──


class TestQuarantineTrigger:
    """Tests for the confidence-gated quarantine trip-wire."""

    def _set_records(
        self, account_id: str, records: list[tuple[str, float | None]]
    ) -> None:
        from app.failure_classification import _account_failure_log

        now_ts = time.time()
        _account_failure_log[account_id] = [
            (cls, conf, now_ts - (len(records) - i) * 100)
            for i, (cls, conf) in enumerate(records)
        ]

    def setup_method(self):
        from app.failure_classification import _account_failure_log

        _account_failure_log.clear()
        _account_by_id.clear()
        _max_plan_pools.clear()

    def teardown_method(self):
        from app.failure_classification import _account_failure_log

        _account_failure_log.clear()
        _account_by_id.clear()
        _max_plan_pools.clear()

    def test_triggers_when_last_n_are_rate_limit_high_confidence(self):
        self._set_records(
            "a",
            [
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
            ],
        )
        assert _check_quarantine_trigger("a") is True

    def test_triggers_when_last_n_are_provision_failure_high_confidence(self):
        self._set_records(
            "a",
            [
                ("PROVISION_FAILURE", 0.85),
                ("RATE_LIMIT", 0.9),
                ("PROVISION_FAILURE", 0.9),
            ],
        )
        assert _check_quarantine_trigger("a") is True

    def test_no_trigger_when_confidence_below_threshold(self):
        self._set_records(
            "a",
            [
                ("RATE_LIMIT", 0.5),
                ("RATE_LIMIT", 0.5),
                ("RATE_LIMIT", 0.5),
            ],
        )
        assert _check_quarantine_trigger("a") is False

    def test_no_trigger_when_mixed_classes_break_streak(self):
        self._set_records(
            "a",
            [
                ("RATE_LIMIT", 0.9),
                ("CODE_ERROR", 0.9),
                ("RATE_LIMIT", 0.9),
            ],
        )
        assert _check_quarantine_trigger("a") is False

    def test_no_trigger_when_fewer_than_n_records(self):
        self._set_records(
            "a",
            [
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
            ],
        )
        assert _check_quarantine_trigger("a") is False

    def test_no_trigger_for_code_error_regardless_of_confidence(self):
        self._set_records(
            "a",
            [
                ("CODE_ERROR", 0.95),
                ("CODE_ERROR", 0.95),
                ("CODE_ERROR", 0.95),
            ],
        )
        assert _check_quarantine_trigger("a") is False

    def test_no_trigger_when_earlier_records_disqualify_last_n(self):
        # 5 records: last 3 must all qualify; position [2] is CODE_ERROR
        self._set_records(
            "a",
            [
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
                ("CODE_ERROR", 0.9),
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
            ],
        )
        assert _check_quarantine_trigger("a") is False

    async def test_disable_account_model_sets_quarantine_kind(self):
        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        self._set_records(
            acct_id,
            [
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
            ],
        )
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(acct_id, OPUS, kind="rate_limit")
        assert acct["disabled_models"][OPUS]["kind"] == "quarantine"

    async def test_disable_account_model_no_quarantine_below_confidence(self):
        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        self._set_records(
            acct_id,
            [
                ("RATE_LIMIT", 0.5),
                ("RATE_LIMIT", 0.5),
                ("RATE_LIMIT", 0.5),
            ],
        )
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(acct_id, OPUS, kind="rate_limit")
        assert acct["disabled_models"][OPUS].get("kind") != "quarantine"

    async def test_quarantine_cooldown_is_short_flat(self):
        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        self._set_records(
            acct_id,
            [
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
            ],
        )
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(acct_id, OPUS, kind="rate_limit")
        assert acct["disabled_models"][OPUS]["kind"] == "quarantine"
        assert (
            _cooldown_seconds_for_info(acct["disabled_models"][OPUS])
            == _QUARANTINE_COOLDOWN_SECONDS
        )

    async def test_quota_exhausted_not_overridden_by_quarantine(self):
        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        self._set_records(
            acct_id,
            [
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
                ("RATE_LIMIT", 0.9),
            ],
        )
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(acct_id, OPUS, kind="quota_exhausted")
        assert acct["disabled_models"][OPUS].get("kind") == "quota_exhausted"

    async def test_disable_account_model_queues_retry_on_raw_persist_exception(self):
        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        _pending_rl_persists.clear()

        async def _raise_connection_error(*args, **kwargs):
            raise ConnectionRefusedError("db offline")

        with patch(
            "app.max_plan_pool._persist_rate_limit_state",
            side_effect=_raise_connection_error,
        ):
            await disable_account_model(acct_id, OPUS, kind="rate_limit")

        assert OPUS in acct["disabled_models"]
        assert acct_id in _pending_rl_persists
        _pending_rl_persists.clear()


# ── disable_account_for_auth (non-recoverable auth hard-disable) ──────────────


class TestDisableAccountForAuth:
    def setup_method(self):
        _account_by_id.clear()

    def teardown_method(self):
        _account_by_id.clear()

    async def test_disables_account_and_sets_reason(self):
        acct = _acct(id="a", enabled=True)
        _account_by_id["a"] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            result = await disable_account_for_auth("a", "oauth_org_not_allowed")
        assert result is True
        assert acct["enabled"] is False
        assert acct["disabled_reason"] == "oauth_org_not_allowed"

    async def test_leaves_disabled_at_unset_to_exempt_cooldown_reenable(self):
        # recheck_disabled_accounts() re-enables disabled accounts after a
        # cooldown keyed on disabled_at; a non-recoverable auth disable must
        # leave it None so that path skips the account indefinitely.
        acct = _acct(id="a", enabled=True)
        _account_by_id["a"] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_for_auth("a", "bad credentials")
        assert acct.get("disabled_at") is None

    async def test_idempotent_when_already_disabled(self):
        acct = _acct(id="a", enabled=False)
        _account_by_id["a"] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            result = await disable_account_for_auth("a", "oauth_org_not_allowed")
        assert result is False

    async def test_unknown_account_returns_false(self):
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            assert await disable_account_for_auth("missing", "reason") is False

    async def test_empty_acct_id_returns_false(self):
        assert await disable_account_for_auth("", "reason") is False


# ── _compute_success_rate_7d_for_type ────────────────────────────────────────


class TestComputeSuccessRate7dForType:
    def test_returns_none_when_no_typed_events(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {
                    "at": now - timedelta(hours=i + 1),
                    "success": True,
                    "task_type": "review",
                }
                for i in range(5)
            ]
        )
        assert _compute_success_rate_7d_for_type(acct, "create", now) is None

    def test_returns_none_below_min_sample_threshold(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {
                    "at": now - timedelta(hours=1),
                    "success": True,
                    "task_type": "create",
                },
                {
                    "at": now - timedelta(hours=2),
                    "success": False,
                    "task_type": "create",
                },
            ]
        )
        assert _compute_success_rate_7d_for_type(acct, "create", now) is None

    def test_returns_type_scoped_rate_ignoring_other_types(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {
                    "at": now - timedelta(hours=1),
                    "success": True,
                    "task_type": "create",
                },
                {
                    "at": now - timedelta(hours=2),
                    "success": True,
                    "task_type": "create",
                },
                {
                    "at": now - timedelta(hours=3),
                    "success": True,
                    "task_type": "create",
                },
                # These should NOT count toward "create" rate.
                {
                    "at": now - timedelta(hours=4),
                    "success": False,
                    "task_type": "review",
                },
                {
                    "at": now - timedelta(hours=5),
                    "success": False,
                    "task_type": "review",
                },
                {
                    "at": now - timedelta(hours=6),
                    "success": False,
                    "task_type": "review",
                },
            ]
        )
        assert _compute_success_rate_7d_for_type(acct, "create", now) == 1.0

    def test_excludes_events_outside_7d_window(self):
        now = datetime.utcnow()
        old = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS + 60)
        acct = _acct(
            outcome_events=[
                {"at": old, "success": True, "task_type": "create"},
                {"at": old, "success": True, "task_type": "create"},
                {"at": old, "success": True, "task_type": "create"},
            ]
        )
        assert _compute_success_rate_7d_for_type(acct, "create", now) is None

    def test_events_without_task_type_excluded(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=1), "success": True},
                {"at": now - timedelta(hours=2), "success": True},
                {"at": now - timedelta(hours=3), "success": True},
                {
                    "at": now - timedelta(hours=4),
                    "success": True,
                    "task_type": "create",
                },
            ]
        )
        assert _compute_success_rate_7d_for_type(acct, "create", now) is None


# ── _effective_success_rate ──────────────────────────────────────────────────


class TestEffectiveSuccessRate:
    def test_returns_typed_rate_when_sufficient_typed_samples(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {
                    "at": now - timedelta(hours=i + 1),
                    "success": True,
                    "task_type": "create",
                }
                for i in range(3)
            ]
            + [
                {
                    "at": now - timedelta(hours=i + 4),
                    "success": False,
                    "task_type": "review",
                }
                for i in range(5)
            ]
        )
        rate = _effective_success_rate(acct, "create", now)
        assert rate == 1.0

    def test_falls_back_to_aggregate_when_typed_sample_sparse(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {
                    "at": now - timedelta(hours=1),
                    "success": True,
                    "task_type": "create",
                },
                # Only 1 "create" event — sparse; falls back to aggregate of 4 events.
                {"at": now - timedelta(hours=2), "success": False},
                {"at": now - timedelta(hours=3), "success": False},
                {"at": now - timedelta(hours=4), "success": False},
            ]
        )
        # Aggregate: 1 success / 4 total = 0.25
        rate = _effective_success_rate(acct, "create", now)
        assert rate == 0.25

    def test_falls_back_to_aggregate_when_task_type_is_none(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": i < 3} for i in range(5)
            ]
        )
        rate = _effective_success_rate(acct, None, now)
        assert rate == _compute_success_rate_7d(acct, now)

    def test_returns_none_when_both_typed_and_aggregate_sparse(self):
        now = datetime.utcnow()
        acct = _acct(
            outcome_events=[
                {
                    "at": now - timedelta(hours=1),
                    "success": True,
                    "task_type": "create",
                },
            ]
        )
        assert _effective_success_rate(acct, "create", now) is None


# ── _compute_avg_duration_24h ────────────────────────────────────────────────


class TestComputeAvgDuration24h:
    def test_returns_none_when_no_completions(self):
        assert _compute_avg_duration_24h(_acct()) is None

    def test_returns_none_below_min_samples(self):
        now = datetime.utcnow()
        acct = _acct(
            completion_events=[
                {"at": now - timedelta(hours=1), "duration_seconds": 60.0}
                for _ in range(_LATENCY_MIN_SAMPLES - 1)
            ]
        )
        assert _compute_avg_duration_24h(acct, now) is None

    def test_returns_average_at_min_samples(self):
        now = datetime.utcnow()
        acct = _acct(
            completion_events=[
                {
                    "at": now - timedelta(hours=i + 1),
                    "duration_seconds": float((i + 1) * 60),
                }
                for i in range(_LATENCY_MIN_SAMPLES)
            ]
        )
        result = _compute_avg_duration_24h(acct, now)
        assert result is not None
        expected = (
            sum((i + 1) * 60.0 for i in range(_LATENCY_MIN_SAMPLES))
            / _LATENCY_MIN_SAMPLES
        )
        assert abs(result - expected) < 0.01

    def test_excludes_events_outside_24h_window(self):
        now = datetime.utcnow()
        old = now - timedelta(seconds=_HEALTH_WINDOW_SECONDS + 60)
        acct = _acct(
            completion_events=[
                {"at": old, "duration_seconds": 10.0},
                {"at": old, "duration_seconds": 10.0},
                {"at": old, "duration_seconds": 10.0},
            ]
        )
        assert _compute_avg_duration_24h(acct, now) is None

    def test_events_missing_duration_are_skipped(self):
        now = datetime.utcnow()
        acct = _acct(
            completion_events=[
                {"at": now - timedelta(hours=1)},  # no duration_seconds
                {"at": now - timedelta(hours=2)},
                {"at": now - timedelta(hours=3)},
            ]
        )
        assert _compute_avg_duration_24h(acct, now) is None


# ── latency-aware selection ───────────────────────────────────────────────────


class TestLatencyAwareSelection:
    """Faster accounts are preferred when health/success rate are comparable."""

    _USER = "test-latency-user"

    def _acct_with_duration(
        self, id_, avg_seconds=None, active_tasks=0, provider="anthropic"
    ):
        now = datetime.utcnow()
        acct = {
            "id": id_,
            "name": id_,
            "enabled": True,
            "active_tasks": active_tasks,
            "oauth_token": "sk-ant-test",
            "provider": provider,
        }
        if avg_seconds is not None:
            acct["completion_events"] = [
                {"at": now - timedelta(hours=i + 1), "duration_seconds": avg_seconds}
                for i in range(_LATENCY_MIN_SAMPLES)
            ]
        return acct

    def setup_method(self):
        _max_plan_pools.pop(self._USER, None)

    def teardown_method(self):
        _max_plan_pools.pop(self._USER, None)

    async def test_faster_account_preferred_balanced(self):
        """Balanced picker selects faster account when active_tasks and success rate are equal."""
        fast = self._acct_with_duration("fast", avg_seconds=60.0)
        slow = self._acct_with_duration("slow", avg_seconds=300.0)
        _max_plan_pools[self._USER] = [fast, slow]
        result = await select_max_plan_account_balanced(
            self._USER, [("anthropic", None)]
        )
        assert result is not None
        assert result[0]["id"] == "fast"

    async def test_sample_floor_prevents_whip(self):
        """Account with fewer than _LATENCY_MIN_SAMPLES completions does not lose to a fast one."""
        now = datetime.utcnow()
        sparse = {
            "id": "sparse",
            "name": "sparse",
            "enabled": True,
            "active_tasks": 0,
            "oauth_token": "sk-ant-test",
            "provider": "anthropic",
            "completion_events": [
                {"at": now - timedelta(hours=1), "duration_seconds": 999.0}
                for _ in range(_LATENCY_MIN_SAMPLES - 1)
            ],
        }
        fast = self._acct_with_duration("fast", avg_seconds=60.0, active_tasks=1)
        _max_plan_pools[self._USER] = [sparse, fast]
        result = await select_max_plan_account_balanced(
            self._USER, [("anthropic", None)]
        )
        assert result is not None
        # sparse wins on load (0 < 1); latency signal is None so doesn't penalise it
        assert result[0]["id"] == "sparse"

    async def test_slow_account_still_gets_traffic_as_only_option(self):
        """Very slow account is selected when it is the only healthy candidate."""
        slow = self._acct_with_duration("slow", avg_seconds=600.0)
        _max_plan_pools[self._USER] = [slow]
        result = await select_max_plan_account_balanced(
            self._USER, [("anthropic", None)]
        )
        assert result is not None
        assert result[0]["id"] == "slow"


# ── disable_account_model(kind="regression") + clear_account_model_regression ─


class TestRegressionQuarantine:
    """Unit tests for the regression-kind disabled_models entry shape."""

    def setup_method(self):
        from app.failure_classification import _account_failure_log

        _account_failure_log.clear()
        _account_by_id.clear()
        _max_plan_pools.clear()

    def teardown_method(self):
        from app.failure_classification import _account_failure_log

        _account_failure_log.clear()
        _account_by_id.clear()
        _max_plan_pools.clear()

    async def test_regression_kind_does_not_cascade(self):
        """Regression on Sonnet must NOT also disable Opus / Haiku."""
        from app.max_plan_pool import disable_account_model

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                "claude-sonnet-4-5",
                kind="regression",
                regression_stats={
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
                provider="anthropic",
            )
        dm = acct["disabled_models"]
        assert "claude-sonnet-4-5" in dm
        # Opus and Haiku must be untouched — regression does NOT cascade.
        assert OPUS not in dm
        assert HAIKU not in dm

    async def test_regression_persists_stats_and_provider(self):
        from app.max_plan_pool import disable_account_model

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        stats = {
            "account_rate": 0.2,
            "project_median": 0.8,
            "sample_size": 6,
            "distinct_accounts": 3,
            "started_at": "2026-04-29T00:00:00+00:00",
        }
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                OPUS,
                kind="regression",
                regression_stats=stats,
                provider="anthropic",
            )
        info = acct["disabled_models"][OPUS]
        assert info["kind"] == "regression"
        assert info["provider"] == "anthropic"
        assert info["regression_stats"] == stats

    async def test_regression_cooldown_is_six_hours(self):
        from app.max_plan_pool import (
            _REGRESSION_COOLDOWN_SECONDS,
            disable_account_model,
        )

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                OPUS,
                kind="regression",
                regression_stats={
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
                provider="anthropic",
            )
        info = acct["disabled_models"][OPUS]
        assert _cooldown_seconds_for_info(info) == _REGRESSION_COOLDOWN_SECONDS
        assert _REGRESSION_COOLDOWN_SECONDS == 6 * 60 * 60

    async def test_regression_does_not_record_rl_event(self):
        """Regression bookkeeping skips rl_events / outcome_events writes —
        those are tied to per-event failure signals, not aggregate trends."""
        from app.max_plan_pool import disable_account_model

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                OPUS,
                kind="regression",
                regression_stats={
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
                provider="anthropic",
            )
        assert acct.get("rl_events") in (None, [])
        assert acct.get("outcome_events") in (None, [])

    async def test_regression_kind_serializes_stats_and_provider(self):
        from app.max_plan_pool import disable_account_model

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        stats = {
            "account_rate": 0.2,
            "project_median": 0.8,
            "sample_size": 6,
            "distinct_accounts": 3,
            "started_at": "2026-04-29T00:00:00+00:00",
        }
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                OPUS,
                kind="regression",
                regression_stats=stats,
                provider="anthropic",
            )
        serialized = _serialize_disabled_model_info(acct["disabled_models"][OPUS])
        assert serialized["kind"] == "regression"
        assert serialized["provider"] == "anthropic"
        assert serialized["regression_stats"] == stats

    async def test_clear_regression_only_clears_regression_kind(self):
        """clear_account_model_regression must NOT touch rate_limit / quarantine entries."""
        from app.max_plan_pool import (
            clear_account_model_regression,
            disable_account_model,
        )

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        # Seed a rate_limit-kind entry on Opus (cascade kept simple — no test fixtures for failure log).
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(acct_id, OPUS, kind="rate_limit")

        # clear_account_model_regression must refuse to touch a rate_limit entry.
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            cleared = await clear_account_model_regression(acct_id, OPUS)
        assert cleared is False
        assert OPUS in acct["disabled_models"]
        assert acct["disabled_models"][OPUS].get("kind") != "regression"

    async def test_clear_regression_clears_regression_kind(self):
        from app.max_plan_pool import (
            clear_account_model_regression,
            disable_account_model,
        )

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                OPUS,
                kind="regression",
                regression_stats={
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
                provider="anthropic",
            )
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            cleared = await clear_account_model_regression(acct_id, OPUS)
        assert cleared is True
        # disabled_models is removed entirely when emptied.
        assert "disabled_models" not in acct

    async def test_regression_quarantine_gates_haiku_dispatch(self):
        """Regression entry stored under Haiku must be found by
        _account_state_for_model when Haiku tasks are dispatched.

        disable_account_model(kind='regression') intentionally skips
        Haiku→Opus normalization (regression is per-model, not per-meter),
        so the key in disabled_models is the literal Haiku model name.
        _account_state_for_model normalizes Haiku→Opus before lookup, but
        must fall back to the un-normalized key to catch regression entries.
        """
        from app.max_plan_pool import _account_state_for_model, disable_account_model

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                HAIKU,
                kind="regression",
                regression_stats={
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
                provider="anthropic",
            )
        # Entry stored under the actual Haiku key (not Opus-normalized).
        assert HAIKU in acct["disabled_models"]
        assert OPUS not in acct["disabled_models"]
        # Gate must see the regression entry and block Haiku dispatch.
        assert _account_state_for_model(acct, HAIKU) == "unavailable"
        # Opus must remain healthy — Haiku regression must NOT bleed over.
        assert _account_state_for_model(acct, OPUS) == "healthy"

    async def test_clear_regression_rejects_provider_mismatch(self):
        """clear_account_model_regression must return False when the caller
        passes a provider that doesn't match the stored entry's provider."""
        from app.max_plan_pool import (
            clear_account_model_regression,
            disable_account_model,
        )

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                OPUS,
                kind="regression",
                regression_stats={
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
                provider="anthropic",
            )
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            # Wrong provider — must be rejected.
            cleared = await clear_account_model_regression(
                acct_id, OPUS, provider="google"
            )
        assert cleared is False
        # Entry must still be present.
        assert OPUS in acct["disabled_models"]

    async def test_clear_regression_on_success_clears_entry(self):
        """Successful probe removes the regression entry and returns metadata."""
        from app.max_plan_pool import clear_regression_on_success, disable_account_model

        acct_id = "a"
        acct = _acct(id=acct_id, user_id="u1", name="TestAcct")
        _account_by_id[acct_id] = acct
        stats = {
            "account_rate": 0.2,
            "project_median": 0.8,
            "sample_size": 5,
            "distinct_accounts": 2,
            "started_at": "2026-04-29T00:00:00+00:00",
        }
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                acct_id,
                OPUS,
                kind="regression",
                regression_stats=stats,
                provider="anthropic",
            )
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            result = await clear_regression_on_success(acct_id, OPUS)
        assert result is not None
        assert result["model"] == OPUS
        assert result["provider"] == "anthropic"
        assert result["account_rate"] == 0.2
        assert result["project_median"] == 0.8
        assert result["sample_size"] == 5
        assert result["user_id"] == "u1"
        assert result["account_label"] == "TestAcct"
        assert "disabled_models" not in acct

    async def test_clear_regression_on_success_returns_none_without_entry(self):
        """Returns None when no regression entry exists — also ensures second call is a no-op."""
        from app.max_plan_pool import clear_regression_on_success

        acct = _acct(id="a")
        _account_by_id["a"] = acct
        assert await clear_regression_on_success("a", OPUS) is None

    async def test_clear_regression_on_success_ignores_quota_exhausted(self):
        """Must not clear a quota_exhausted entry on the same model."""
        from app.max_plan_pool import clear_regression_on_success, disable_account_model

        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(acct_id, OPUS, kind="quota_exhausted")
        result = await clear_regression_on_success(acct_id, OPUS)
        assert result is None
        assert OPUS in acct["disabled_models"]
        assert acct["disabled_models"][OPUS]["kind"] == "quota_exhausted"

    async def test_clear_regression_on_success_leaves_quota_exhausted_sibling(self):
        """Clearing regression on one model must not touch quota_exhausted on another."""
        from app.max_plan_pool import clear_regression_on_success
        from datetime import datetime

        acct_id = "a"
        acct = _acct(id=acct_id, name="A")
        sonnet = "claude-sonnet-4-5"
        # Inject directly to avoid cascade interference between model tiers.
        acct["disabled_models"] = {
            sonnet: {
                "kind": "regression",
                "hits": 1,
                "at": datetime.utcnow(),
                "provider": "anthropic",
                "regression_stats": {
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
            },
            OPUS: {"kind": "quota_exhausted", "hits": 1, "at": datetime.utcnow()},
        }
        _account_by_id[acct_id] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            result = await clear_regression_on_success(acct_id, sonnet)
        assert result is not None
        assert result["model"] == sonnet
        dm = acct.get("disabled_models") or {}
        assert sonnet not in dm
        assert OPUS in dm and dm[OPUS]["kind"] == "quota_exhausted"


# ── scan_account_model_regressions dedupe ───────────────────────────────────


class TestScanRegressionsDedupe:
    """Tests for scanner idempotency: a steady-state regression must NOT
    re-fire on every tick."""

    def setup_method(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    def teardown_method(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    async def test_dedupes_within_cooldown(self):
        from app.max_plan_pool import (
            _REGRESSION_COOLDOWN_SECONDS,
            scan_account_model_regressions,
        )
        from app.account_model_regression import RegressionCandidate

        acct_id = "a"
        # Pre-seed a regression entry that's well within the 6h cooldown.
        acct = _acct(id=acct_id, user_id="u1", name="A")
        acct["disabled_models"] = {
            "claude-r": {
                "hits": 1,
                "at": datetime.utcnow() - timedelta(seconds=60),
                "kind": "regression",
                "provider": "anthropic",
            }
        }
        _account_by_id[acct_id] = acct

        # Even if find_account_model_regressions still flags the cell, scan
        # must dedupe and return an empty list (no new event to dispatch).
        candidate = RegressionCandidate(
            account_id=acct_id,
            provider="anthropic",
            model="claude-r",
            account_rate=0.0,
            project_median=0.5,
            sample_size=5,
            distinct_accounts=2,
        )
        with (
            patch(
                "app.account_model_regression.find_account_model_regressions",
                new=AsyncMock(return_value=[candidate]),
            ),
            patch("app.max_plan_pool._persist_rate_limit_state"),
        ):
            fired = await scan_account_model_regressions(session=None)
        assert fired == []
        # Within cooldown the entry must NOT have been bumped — at_dt unchanged.
        assert acct["disabled_models"]["claude-r"]["hits"] == 1
        # Sanity: the cooldown is ~6h, much longer than the 60s gap above.
        assert _REGRESSION_COOLDOWN_SECONDS > 60

    async def test_fires_when_no_prior_entry(self):
        from app.account_model_regression import RegressionCandidate
        from app.max_plan_pool import scan_account_model_regressions

        acct_id = "a"
        acct = _acct(id=acct_id, user_id="u1", name="A")
        _account_by_id[acct_id] = acct

        candidate = RegressionCandidate(
            account_id=acct_id,
            provider="anthropic",
            model="claude-r",
            account_rate=0.1,
            project_median=0.9,
            sample_size=5,
            distinct_accounts=2,
        )
        with (
            patch(
                "app.account_model_regression.find_account_model_regressions",
                new=AsyncMock(return_value=[candidate]),
            ),
            patch("app.max_plan_pool._persist_rate_limit_state"),
        ):
            fired = await scan_account_model_regressions(session=None)
        assert len(fired) == 1
        ev = fired[0]
        assert ev["account_id"] == acct_id
        assert ev["provider"] == "anthropic"
        assert ev["model"] == "claude-r"
        assert ev["account_rate"] == 0.1
        assert ev["project_median"] == 0.9
        assert ev["sample_size"] == 5
        # Quarantine state must be persisted in-memory.
        assert acct["disabled_models"]["claude-r"]["kind"] == "regression"


# ── pool_wide_rate_limit_cooldown: user/provider/model scoping ───────────────


class TestPoolWideRateLimitCooldownScoping:
    """pool_wide_rate_limit_cooldown must restrict its verdict to the candidate
    set the task could actually dispatch to, so unrelated users or providers
    cannot mask a full-pool blockage or cause false RATE_LIMIT verdicts."""

    def setup_method(self):
        _max_plan_pools.clear()
        _account_by_id.clear()

    def teardown_method(self):
        _max_plan_pools.clear()
        _account_by_id.clear()

    def _add(self, user_id: str, acct_id: str, provider: str = "anthropic", **kw):
        a = _acct(id=acct_id, user_id=user_id, provider=provider, **kw)
        _max_plan_pools.setdefault(user_id, []).append(a)
        _account_by_id[acct_id] = a
        return a

    def test_unrelated_user_healthy_account_does_not_prevent_true(self):
        """A healthy account belonging to a *different* user must not prevent
        pool_wide_rate_limit_cooldown from returning True for user A's pool."""
        from app.max_plan_pool import pool_wide_rate_limit_cooldown

        now = datetime.utcnow()
        # User A: single account rate-limited
        self._add(
            "user-a",
            "a1",
            disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}},
        )
        # User B: healthy account (different user)
        self._add("user-b", "b1")

        assert pool_wide_rate_limit_cooldown(user_id="user-a") is True

    def test_unrelated_provider_healthy_account_does_not_prevent_true(self):
        """A healthy account on a *different* provider must not prevent a True
        verdict when all anthropic accounts are rate-limited."""
        from app.max_plan_pool import pool_wide_rate_limit_cooldown

        now = datetime.utcnow()
        self._add(
            "user-a",
            "a-ant",
            provider="anthropic",
            disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}},
        )
        # Healthy gemini account for the same user
        self._add("user-a", "a-gem", provider="gemini")

        assert (
            pool_wide_rate_limit_cooldown(user_id="user-a", providers=["anthropic"])
            is True
        )

    def test_healthy_account_in_scope_returns_false(self):
        """If the scoped pool has at least one healthy account, return False."""
        from app.max_plan_pool import pool_wide_rate_limit_cooldown

        now = datetime.utcnow()
        self._add(
            "user-a",
            "a1",
            disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}},
        )
        self._add("user-a", "a2")  # healthy

        assert pool_wide_rate_limit_cooldown(user_id="user-a") is False

    def test_model_scoping_unblocked_model_returns_false(self):
        """When model is specified, an account disabled only for a *different*
        model is still healthy for the requested model, so return False."""
        from app.max_plan_pool import pool_wide_rate_limit_cooldown

        now = datetime.utcnow()
        # Account disabled for claude-sonnet only, not for Opus
        self._add(
            "user-a",
            "a1",
            disabled_models={
                "claude-sonnet-4-6": {"hits": 1, "at": now - timedelta(minutes=5)}
            },
        )
        assert pool_wide_rate_limit_cooldown(user_id="user-a", model=OPUS) is False

    def test_empty_scoped_pool_returns_false(self):
        """When the scoped pool is empty (user has no accounts), return False."""
        from app.max_plan_pool import pool_wide_rate_limit_cooldown

        self._add("user-b", "b1")  # different user
        assert pool_wide_rate_limit_cooldown(user_id="user-a") is False


# ── db_pool_wide_rate_limit_cooldown: user/provider/model scoping ─────────────


class TestDbPoolWideRateLimitCooldownScoping:
    """db_pool_wide_rate_limit_cooldown must restrict its verdict to the eligible
    pool, so unrelated-user or unrelated-provider accounts do not skew the result."""

    @pytest.mark.asyncio
    async def test_user_filter_isolates_verdict(self, monkeypatch):
        """When user_id is passed, only that user's accounts are considered.
        A healthy account for user-b must not prevent True for user-a's pool."""
        from datetime import datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock
        from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
        from app.models import MaxPlanAccount

        rl_at = datetime.utcnow() - timedelta(minutes=5)
        a_acct = MaxPlanAccount(
            user_id="user-a",
            name="a",
            oauth_token="tok",
            rate_limit_state={
                "disabled_models": {OPUS: {"at": rl_at.isoformat(), "hits": 1}}
            },
        )
        b_acct = MaxPlanAccount(
            user_id="user-b",
            name="b",
            oauth_token="tok-b",
            rate_limit_state=None,
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [a_acct]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal", MagicMock(return_value=mock_session)
        )
        _ = b_acct  # user-b account not returned by filtered query

        result = await db_pool_wide_rate_limit_cooldown(user_id="user-a")
        assert result is True

    @pytest.mark.asyncio
    async def test_provider_filter_excludes_healthy_unrelated_provider(
        self, monkeypatch
    ):
        """When providers filter is set, a healthy account on a different provider
        must not prevent a True verdict for the filtered provider pool."""
        from datetime import datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock
        from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
        from app.models import MaxPlanAccount, LLMProviderName

        rl_at = datetime.utcnow() - timedelta(minutes=5)
        ant_acct = MaxPlanAccount(
            user_id="u",
            name="ant",
            oauth_token="tok",
            provider=LLMProviderName.ANTHROPIC,
            rate_limit_state={
                "disabled_models": {OPUS: {"at": rl_at.isoformat(), "hits": 1}}
            },
        )
        # A healthy gemini account that should be excluded by the providers filter
        gem_acct = MaxPlanAccount(
            user_id="u",
            name="gem",
            oauth_token=None,
            provider=LLMProviderName.GEMINI
            if hasattr(LLMProviderName, "GEMINI")
            else LLMProviderName.ANTHROPIC,
            rate_limit_state=None,
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ant_acct, gem_acct]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal", MagicMock(return_value=mock_session)
        )

        result = await db_pool_wide_rate_limit_cooldown(providers=["anthropic"])
        assert result is True
