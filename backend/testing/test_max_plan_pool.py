"""Focused unit tests for pure/synchronous functions in app/max_plan_pool.py.

These tests avoid the in-memory pool dicts and DB — they construct account
dicts directly and pass an explicit `now` where the function supports it,
keeping assertions deterministic without fixtures or locks.
"""
from datetime import datetime, timedelta, timezone

from app.max_plan_pool import (
    _HEALTH_SCORE_DISQUALIFY_THRESHOLD,
    _HEALTH_WINDOW_SECONDS,
    _OUTCOME_WINDOW_SECONDS,
    _QUOTA_EXHAUSTED_COOLDOWN_SECONDS,
    _account_at_cap,
    _account_available_for_model,
    _compute_health_score,
    _compute_success_rate_7d,
    _cooldown_seconds_for_info,
    _daily_reset_if_elapsed,
    _normalize_rl_model,
    _prune_health_events,
    _serialize_disabled_model_info,
    cascade_disabled_models,
    describe_pool_blockage,
    get_account_health_stats,
    get_all_account_health_summaries,
    summarize_pool_state,
    _account_by_id,
    _max_plan_pools,
)

OPUS = "claude-opus-4-7"
HAIKU = "claude-haiku-4-5-20251001"


def _acct(**kw) -> dict:
    base = {"id": "a", "name": "a", "enabled": True, "active_tasks": 0,
            "oauth_token": "sk-ant-test"}
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
            "rl_events_24h", "completions_24h", "avg_duration_seconds", "health_score",
            "success_rate_7d",
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
        acct = _acct(daily_task_count=5, daily_task_count_reset_at=now - timedelta(hours=12))
        assert _daily_reset_if_elapsed(acct, now) is False
        assert acct["daily_task_count"] == 5

    def test_resets_after_window_elapsed(self):
        now = datetime.utcnow()
        acct = _acct(daily_task_count=42, daily_task_count_reset_at=now - timedelta(hours=25))
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
        acct = _acct(disabled_models={
            OPUS: {"hits": 1, "at": datetime.utcnow() - timedelta(minutes=60)}
        })
        assert _account_available_for_model(acct, OPUS) is True


# ── describe_pool_blockage ──────────────────────────────────────────────────

class TestDescribePoolBlockage:
    def _reset_pools(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    def test_no_accounts_configured(self):
        self._reset_pools()
        try:
            assert describe_pool_blockage(OPUS, user_id="nobody") == "no accounts configured"
        finally:
            self._reset_pools()

    def test_all_rate_limited_reports_earliest_recovery(self):
        self._reset_pools()
        try:
            now = datetime.utcnow()
            acct = _acct(id="r1", user_id="u",
                         disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}})
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
            rl = _acct(id="r", user_id="u",
                       disabled_models={OPUS: {"hits": 1, "at": now}})
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
            assert describe_pool_blockage(OPUS, user_id="u") == "no eligible accounts or slot contention"
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
            rl = _acct(id="r", user_id="u",
                       disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}})
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
        assert _cooldown_seconds_for_info(
            {"hits": 1, "kind": "quota_exhausted"}
        ) == _QUOTA_EXHAUSTED_COOLDOWN_SECONDS
        # Even high hit counts stay flat at 24h (not the 4h exponential cap).
        assert _cooldown_seconds_for_info(
            {"hits": 99, "kind": "quota_exhausted"}
        ) == _QUOTA_EXHAUSTED_COOLDOWN_SECONDS

    def test_retry_after_seconds_is_floor(self):
        """Server-provided retry hint lifts the exponential cooldown when larger."""
        # hits=1 → exponential=1800s. Hint of 3600s should win.
        assert _cooldown_seconds_for_info(
            {"hits": 1, "retry_after_seconds": 3600}
        ) == 3600
        # Hint smaller than exponential is ignored.
        assert _cooldown_seconds_for_info(
            {"hits": 1, "retry_after_seconds": 60}
        ) == 30 * 60
        # Non-numeric hint is ignored.
        assert _cooldown_seconds_for_info(
            {"hits": 1, "retry_after_seconds": "unknown"}
        ) == 30 * 60


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
        out = _serialize_disabled_model_info({"hits": 1, "at": at, "kind": "rate_limit"})
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
            acct = _acct(id="r1", user_id="u",
                         disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5)}})
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
            acct = _acct(id="d1", user_id="u", enabled=False,
                         disabled_reason="auto-disabled after 5 rate-limit hits")
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
            acct = _acct(id="q1", user_id="u",
                         rl_events=[now - timedelta(hours=i) for i in range(1, 4)])
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
            acct = _acct(id="qe1", user_id="u",
                         disabled_models={OPUS: {"hits": 1, "at": now - timedelta(minutes=5),
                                                  "kind": "quota_exhausted"}})
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
            acct = _acct(id="s1", user_id="u", outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": i < 4}
                for i in range(5)
            ])
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
        acct = _acct(outcome_events=[
            {"at": now - timedelta(hours=1), "success": True},
            {"at": now - timedelta(hours=2), "success": False},
        ])
        assert _compute_success_rate_7d(acct, now) is None

    def test_returns_rate_from_3_plus_events(self):
        now = datetime.utcnow()
        acct = _acct(outcome_events=[
            {"at": now - timedelta(hours=1), "success": True},
            {"at": now - timedelta(hours=2), "success": True},
            {"at": now - timedelta(hours=3), "success": False},
        ])
        rate = _compute_success_rate_7d(acct, now)
        assert rate == 2 / 3

    def test_excludes_events_older_than_7d(self):
        now = datetime.utcnow()
        old = now - timedelta(seconds=_OUTCOME_WINDOW_SECONDS + 60)
        acct = _acct(outcome_events=[
            {"at": old, "success": False},
            {"at": old, "success": False},
            {"at": old, "success": False},
        ])
        assert _compute_success_rate_7d(acct, now) is None

    def test_perfect_success_rate(self):
        now = datetime.utcnow()
        acct = _acct(outcome_events=[
            {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
        ])
        assert _compute_success_rate_7d(acct, now) == 1.0

    def test_zero_success_rate(self):
        now = datetime.utcnow()
        acct = _acct(outcome_events=[
            {"at": now - timedelta(hours=i + 1), "success": False} for i in range(3)
        ])
        assert _compute_success_rate_7d(acct, now) == 0.0


# ── selection tiebreaking by success rate ─────────────────────────────────────

class TestSuccessRateTiebreaking:
    def test_higher_success_rate_scores_higher(self):
        """_compute_success_rate_7d should rank high-success above low-success accounts."""
        now = datetime.utcnow()
        good = _acct(id="good", outcome_events=[
            {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
        ])
        bad = _acct(id="bad", outcome_events=[
            {"at": now - timedelta(hours=i + 1), "success": i < 1}  # 1/5 success
            for i in range(5)
        ])
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
        hi = _acct(id="hi", active_tasks=1, outcome_events=[
            {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
        ])
        lo = _acct(id="lo", active_tasks=0, outcome_events=[
            {"at": now - timedelta(hours=i + 1), "success": False} for i in range(5)
        ])
        key = lambda a: (  # noqa: E731
            _compute_health_score(a, now),
            _compute_success_rate_7d(a, now) or 0.0,
            -a.get("active_tasks", 0),
        )
        assert key(hi) > key(lo)
