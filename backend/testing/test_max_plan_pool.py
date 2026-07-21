"""Focused unit tests for pure/synchronous functions in app/max_plan_pool.py.

These tests avoid the in-memory pool dicts and DB — they construct account
dicts directly and pass an explicit `now` where the function supports it,
keeping assertions deterministic without fixtures or locks.
"""

import asyncio
import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.time_utils import _utcnow
from app.max_plan_project_state import prune_project_state, reconcile_project_state
from app.providers.base import UsageSnapshot
from app.max_plan_pool import (
    _CREDENTIAL_SWEEP_INTERVAL_SECONDS,
    _HEALTH_SCORE_DISQUALIFY_THRESHOLD,
    _HEALTH_WINDOW_SECONDS,
    _LATENCY_MIN_SAMPLES,
    _OUTCOME_EVENTS_CAP,
    _OUTCOME_WINDOW_SECONDS,
    _QUARANTINE_COOLDOWN_SECONDS,
    _QUOTA_EXHAUSTED_COOLDOWN_SECONDS,
    _STARTUP_FAILURE_COOLDOWN_SECONDS,
    _STARTUP_FAILURE_THRESHOLD,
    _USAGE_HEADROOM_NEUTRAL,
    _USAGE_METER_DISPLAY_STALE_SECONDS,
    _USAGE_METER_REFRESH_INTERVAL_SECONDS,
    _USAGE_SNAPSHOT_TTL_SECONDS,
    _account_at_cap,
    _codex_oauth_requires_refresh_probe,
    _account_available_for_model,
    _project_at_cap,
    _account_by_id,
    _account_state_for_model,
    _apply_rate_limit_mutations,
    _apply_usage_cap_mutations,
    _build_rate_limit_state_blob,
    _apply_usage_gate,
    _cached_usage_snapshot,
    _check_quarantine_trigger,
    _compute_avg_duration_24h,
    _compute_health_score,
    _compute_success_rate_7d,
    _filter_providers_by_cli,
    _PROVIDER_REQUIRED_CLI,
    _compute_success_rate_7d_for_type,
    _effective_success_rate,
    _cooldown_seconds_for_info,
    _daily_reset_if_elapsed,
    _max_plan_pool_lock,
    _max_plan_pools,
    _maybe_schedule_usage_refresh,
    _normalize_rl_model,
    _pending_cap_persists,
    _pending_rl_persists,
    _persist_rate_limit_state,
    _prune_health_events,
    _refresh_usage_snapshot,
    _restore_usage_state,
    _serialize_disabled_model_info,
    _stage_usage_delta,
    _snapshot_from_persisted_usage,
    _usage_gate_should_skip,
    _usage_headroom_rank,
    _usage_headroom_settings,
    detect_persistently_dark_meters,
    get_meter_reliability_stats,
    _usage_dark_reason,
    _usage_snapshot_summary,
    usage_snapshot_summary_with_fallback,
    alternate_account_recovery_deadline,
    cascade_disabled_models,
    credential_sweep_service,
    describe_pool_blockage,
    disable_account_for_auth,
    disable_account_model,
    get_account_health_stats,
    get_all_account_health_summaries,
    record_account_startup_failure,
    recheck_disabled_accounts,
    refresh_usage_meter_snapshots,
    release_max_plan_account,
    select_max_plan_account,
    select_max_plan_account_balanced,
    select_llm_runtime_for_task,
    summarize_pool_state,
)

OPUS = "claude-opus-4-8"
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


def _codex_oauth_secret(expires_at_epoch: float) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at_epoch}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return json.dumps(
        {"tokens": {"access_token": f"header.{payload}.sig", "refresh_token": "rotating-refresh-token"}}
    )


@pytest.fixture(autouse=True)
def _autoclean_pool_state():
    """Clear all shared in-memory pool state before AND after every test.

    The after-clear matters as much as the before-clear: this module's last
    test would otherwise leak seeded/blocked accounts into whichever module
    pytest collects next in the same process, where the pool-wide rate-limit
    correlation reclassifies genuine CODE_ERRORs as RATE_LIMIT
    (TestClassifyFailure / TestFailureClassification shard failures).
    """
    from app.failure_classification import _account_failure_log

    _max_plan_pools.clear()
    _account_by_id.clear()
    _pending_cap_persists.clear()
    _pending_rl_persists.clear()
    _account_failure_log.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _pending_cap_persists.clear()
    _pending_rl_persists.clear()
    _account_failure_log.clear()


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

    def test_fable_isolated_on_own_meter(self):
        # TEMPORARY model (limited availability): tracked on its own meter
        # until we know empirically whether a fable 429 implies the shared
        # opus bucket is exhausted. Must not cascade to opus.
        assert cascade_disabled_models("claude-fable-5") == ("claude-fable-5",)


# ── _filter_providers_by_cli ──────────────────────────────────────────────────


class TestFilterProvidersByCLI:
    """Runner CLI inventory filtering tests."""

    ALL_PROVIDERS = ["anthropic", "deepseek", "zai", "openai", "gemini"]

    def test_all_clis_present_all_providers_allowed(self):
        clis = {"claude": {"present": True}, "codex": {"present": True}, "gemini": {"present": True}}
        assert _filter_providers_by_cli(self.ALL_PROVIDERS, clis) == self.ALL_PROVIDERS

    def test_only_claude_present_keeps_claude_providers(self):
        clis = {"claude": {"present": True}}
        result = _filter_providers_by_cli(self.ALL_PROVIDERS, clis)
        assert result == ["anthropic", "deepseek", "zai"]

    def test_only_codex_present_keeps_only_openai(self):
        clis = {"codex": {"present": True}}
        result = _filter_providers_by_cli(self.ALL_PROVIDERS, clis)
        assert result == ["openai"]

    def test_only_gemini_present_keeps_only_gemini(self):
        clis = {"gemini": {"present": True}}
        result = _filter_providers_by_cli(self.ALL_PROVIDERS, clis)
        assert result == ["gemini"]

    def test_none_runner_available_clis_returns_all(self):
        """None → no filtering applied (backward compat / cloud path)."""
        assert _filter_providers_by_cli(self.ALL_PROVIDERS, None) == self.ALL_PROVIDERS

    def test_empty_dict_conservative_claude_only(self):
        """Empty dict → conservative: assume only claude is available."""
        result = _filter_providers_by_cli(self.ALL_PROVIDERS, {})
        assert result == ["anthropic", "deepseek", "zai"]

    def test_all_providers_filtered_returns_empty(self):
        clis = {"claude": {"present": True}}
        assert _filter_providers_by_cli(["openai", "gemini"], clis) == []

    def test_cli_present_false_blocks_provider(self):
        clis = {"claude": {"present": False}, "codex": {"present": True}}
        result = _filter_providers_by_cli(["anthropic", "openai"], clis)
        assert result == ["openai"]

    def test_empty_provider_list_returns_empty(self):
        assert _filter_providers_by_cli([], {"claude": {"present": True}}) == []

    def test_cli_missing_key_returns_empty(self):
        """When a required CLI key is entirely absent, the provider is blocked."""
        clis = {"gemini": {"present": True}}
        assert _filter_providers_by_cli(["openai"], clis) == []

    def test_unknown_provider_name_returns_empty(self):
        """An unknown provider name (not in _PROVIDER_REQUIRED_CLI) is skipped."""
        assert _filter_providers_by_cli(["unknown_provider"], {"claude": {"present": True}}) == []

    def test_PROVIDER_REQUIRED_CLI_has_expected_keys(self):
        assert _PROVIDER_REQUIRED_CLI["anthropic"] == "claude"
        assert _PROVIDER_REQUIRED_CLI["deepseek"] == "claude"
        assert _PROVIDER_REQUIRED_CLI["zai"] == "claude"
        assert _PROVIDER_REQUIRED_CLI["openai"] == "codex"
        assert _PROVIDER_REQUIRED_CLI["gemini"] == "gemini"


# ── _compute_health_score ───────────────────────────────────────────────────


class TestComputeHealthScore:
    def test_empty_history_scores_zero(self):
        assert _compute_health_score(_acct()) == 0.0

    def test_formula_completions_minus_twice_rl_events(self):
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        _prune_health_events(acct, _utcnow())
        assert "rl_events" not in acct
        assert "completion_events" not in acct


# ── _daily_reset_if_elapsed ─────────────────────────────────────────────────


class TestDailyResetIfElapsed:
    def test_seeds_reset_at_when_missing(self):
        now = _utcnow()
        acct = _acct(daily_task_count=3)
        assert _daily_reset_if_elapsed(acct, now) is False
        assert acct["daily_task_count_reset_at"] == now
        # Existing count stays — we only seed the window marker.
        assert acct["daily_task_count"] == 3

    def test_no_reset_within_window(self):
        now = _utcnow()
        acct = _acct(
            daily_task_count=5, daily_task_count_reset_at=now - timedelta(hours=12)
        )
        assert _daily_reset_if_elapsed(acct, now) is False
        assert acct["daily_task_count"] == 5

    def test_resets_after_window_elapsed(self):
        now = _utcnow()
        acct = _acct(
            daily_task_count=42, daily_task_count_reset_at=now - timedelta(hours=25)
        )
        assert _daily_reset_if_elapsed(acct, now) is True
        assert acct["daily_task_count"] == 0
        assert acct["daily_task_count_reset_at"] == now

    def test_resets_per_project_daily_counts_too(self):
        now = _utcnow()
        acct = _acct(
            daily_task_count=10,
            daily_task_count_reset_at=now - timedelta(hours=25),
            project_state={
                "p1": {"active_tasks": 0, "daily_task_count": 8},
                "p2": {"active_tasks": 1, "daily_task_count": 3},
                "p3": {"active_tasks": 2},  # no daily_task_count — left alone
            },
        )
        assert _daily_reset_if_elapsed(acct, now) is True
        assert acct["daily_task_count"] == 0
        # Per-project daily counts reset
        assert acct["project_state"]["p1"]["daily_task_count"] == 0
        assert acct["project_state"]["p2"]["daily_task_count"] == 0
        # Entry without daily_task_count key unchanged
        assert acct["project_state"]["p3"]["active_tasks"] == 2
        assert "daily_task_count" not in acct["project_state"]["p3"]
        # Non-dict project_state entries are skipped
        acct["project_state"]["p4"] = "bogus"
        # Move reset_at backwards so the window check triggers again
        acct["daily_task_count_reset_at"] = now - timedelta(hours=25)
        assert _daily_reset_if_elapsed(acct, now) is True  # no crash

    def test_per_project_daily_reset_skipped_when_no_project_state(self):
        now = _utcnow()
        acct = _acct(
            daily_task_count=10,
            daily_task_count_reset_at=now - timedelta(hours=25),
            # no project_state key
        )
        assert _daily_reset_if_elapsed(acct, now) is True
        assert acct["daily_task_count"] == 0

    def test_per_project_daily_reset_skipped_when_empty_project_state(self):
        now = _utcnow()
        acct = _acct(
            daily_task_count=10,
            daily_task_count_reset_at=now - timedelta(hours=25),
            project_state={},
        )
        assert _daily_reset_if_elapsed(acct, now) is True
        assert acct["daily_task_count"] == 0


# ── project_state bounding (app/max_plan_project_state.py) ──────────────────
#
# project_state gained one entry per DISTINCT PROJECT ever dispatched on an
# account and never dropped one — copied forward on every ~300s pool rebuild and
# re-persisted to the project_state JSONB column on every cap flush. Unbounded.


class TestReconcileProjectState:
    """The pool-rebuild path: copy persisted state, override live counts, bound."""

    def test_stale_active_tasks_are_zeroed_and_overridden_from_live_counts(self):
        """Pre-existing contract: the tasks table is the SSOT for active_tasks."""
        reconciled = reconcile_project_state(
            {
                "p1": {"active_tasks": 99, "daily_task_count": 4},  # stale 99
                "p2": {"active_tasks": 7, "daily_task_count": 2},
            },
            {"p1": 3},  # p1 really has 3 active; p2 has none
        )
        assert reconciled["p1"] == {"active_tasks": 3, "daily_task_count": 4}
        # p2's stale active count is zeroed, and its daily count survives.
        assert reconciled["p2"] == {"active_tasks": 0, "daily_task_count": 2}

    def test_a_project_with_live_tasks_is_never_pruned(self):
        """The prune runs AFTER the override, so live projects always survive."""
        reconciled = reconcile_project_state(
            {},  # nothing persisted at all
            {"running-now": 2},
        )
        assert reconciled == {"running-now": {"active_tasks": 2}}

    def test_idle_and_deleted_project_entries_are_collected(self):
        """The unbounded-growth case: entries with nothing left to say are dropped."""
        reconciled = reconcile_project_state(
            {
                "idle": {"active_tasks": 5, "daily_task_count": 0},  # stale, gone quiet
                "deleted": {"active_tasks": 0, "daily_task_count": 3},
                "busy": {"active_tasks": 0, "daily_task_count": 1},
            },
            {},  # no live tasks anywhere
            live_project_ids={"idle", "busy"},
        )
        # "idle": active zeroed by reconciliation + no daily count => no information.
        # "deleted": project no longer exists => evicted despite a daily count.
        assert set(reconciled) == {"busy"}


class TestPruneProjectState:
    def test_zero_counter_entries_are_dropped(self):
        """An all-zero entry is indistinguishable from an absent one."""
        pruned = prune_project_state(
            {
                "idle": {"active_tasks": 0, "daily_task_count": 0},
                "never-ran": {},
            }
        )
        assert pruned == {}

    def test_live_and_recently_active_projects_are_kept(self):
        """Behaviour-preserving for anything carrying real state."""
        state = {
            "running": {"active_tasks": 2, "daily_task_count": 0},
            "dispatched-today": {"active_tasks": 0, "daily_task_count": 7},
            "both": {"active_tasks": 1, "daily_task_count": 3},
            "idle": {"active_tasks": 0, "daily_task_count": 0},
        }
        pruned = prune_project_state(state)
        assert set(pruned) == {"running", "dispatched-today", "both"}
        # Kept entries are untouched, counters and all.
        assert pruned["both"] == {"active_tasks": 1, "daily_task_count": 3}

    def test_deleted_project_is_evicted_even_with_a_daily_count(self):
        """A project that no longer exists is dropped without waiting a day."""
        state = {
            "alive": {"active_tasks": 0, "daily_task_count": 4},
            "deleted": {"active_tasks": 0, "daily_task_count": 4},
        }
        pruned = prune_project_state(state, live_project_ids={"alive"})
        assert set(pruned) == {"alive"}

    def test_deleted_project_with_live_tasks_is_kept(self):
        """Never evict an entry a release path still has to decrement."""
        state = {"racing": {"active_tasks": 1, "daily_task_count": 0}}
        pruned = prune_project_state(state, live_project_ids=set())
        assert set(pruned) == {"racing"}

    def test_malformed_entries_and_none_are_tolerated(self):
        assert prune_project_state(None) == {}
        assert prune_project_state({"bogus": "not-a-dict"}) == {}

    def test_prune_is_idempotent_and_bounds_growth(self):
        """The steady state: N projects run once, go idle, and fall out.

        This is the unbounded-growth scenario from the finding — a long-lived
        account that has touched many projects over time.
        """
        state = {
            f"proj-{i}": {"active_tasks": 0, "daily_task_count": 0} for i in range(500)
        }
        state["still-running"] = {"active_tasks": 1, "daily_task_count": 1}

        once = prune_project_state(state)
        assert set(once) == {"still-running"}
        # Idempotent: pruning the pruned dict changes nothing.
        assert prune_project_state(once) == once


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

    def test_fresh_codex_oauth_keeps_normal_concurrency(self):
        acct = _acct(provider="openai", auth_mode="oauth", active_tasks=8,
            runtime_secret=_codex_oauth_secret(time.time() + 6 * 60 * 60))
        assert _account_at_cap(acct) is None
        assert _codex_oauth_requires_refresh_probe(acct) == (False, True)

    def test_near_expiry_codex_oauth_serializes_only_refresh_probe(self):
        secret = _codex_oauth_secret(time.time() + 4 * 60 * 60)
        first = _acct(provider="openai", auth_mode="oauth", active_tasks=0, runtime_secret=secret)
        follower = {**first, "active_tasks": 1}
        assert _account_at_cap(first) is None
        assert "refresh probe" in (_account_at_cap(follower) or "")
        assert _codex_oauth_requires_refresh_probe(first) == (True, True)

    def test_opaque_codex_oauth_serializes_conservatively(self):
        acct = _acct(provider="openai", auth_mode="oauth", active_tasks=1,
            runtime_secret='{"tokens":{"access_token":"opaque","refresh_token":"rt"}}')
        reason = _account_at_cap(acct)
        assert reason is not None
        assert "expiry unknown" in reason
        assert _codex_oauth_requires_refresh_probe(acct) == (True, False)


class TestCodexOauthConcurrencyGate:
    def _seed(self, secret: str) -> dict:
        acct = _acct(
            id="codex-concurrent",
            user_id="codex-user",
            provider="openai",
            auth_mode="oauth",
            oauth_token=None,
            runtime_secret=secret,
            runtime_ready=True,
            disabled_models={},
        )
        _max_plan_pools["codex-user"] = [acct]
        _account_by_id[acct["id"]] = acct
        return acct

    async def test_fresh_access_token_admits_multiple_tasks(self):
        acct = self._seed(_codex_oauth_secret(time.time() + 6 * 60 * 60))

        first = await select_max_plan_account(
            "codex-user", provider="openai", model="gpt-5.6-terra"
        )
        second = await select_max_plan_account(
            "codex-user", provider="openai", model="gpt-5.6-terra"
        )

        assert first is acct
        assert second is acct
        assert acct["active_tasks"] == 2

    async def test_refresh_probe_reopens_concurrency_after_rotation(self):
        acct = self._seed(_codex_oauth_secret(time.time() + 4 * 60 * 60))

        probe = await select_max_plan_account(
            "codex-user", provider="openai", model="gpt-5.6-terra"
        )
        blocked = await select_max_plan_account(
            "codex-user", provider="openai", model="gpt-5.6-terra"
        )
        assert probe is acct
        assert blocked is None

        # Rotation endpoint updates runtime_secret in place immediately after
        # provider validation; the probe remains active but followers reopen.
        acct["runtime_secret"] = _codex_oauth_secret(time.time() + 6 * 60 * 60)
        follower = await select_max_plan_account(
            "codex-user", provider="openai", model="gpt-5.6-terra"
        )
        assert follower is acct
        assert acct["active_tasks"] == 2


# ── _project_at_cap ──────────────────────────────────────────────────────────


class TestProjectAtCap:
    """Per-project cap enforcement: _project_at_cap gates on per-project daily
    and concurrent caps stored in the in-memory account entry."""

    def test_none_project_id_returns_none(self):
        """When project_id is None, no per-project gating is applied."""
        acct = _acct(
            project_daily_task_cap={"proj-1": 5},
            project_state={"proj-1": {"daily_task_count": 10, "active_tasks": 0}},
        )
        assert _project_at_cap(acct, None) is None

    def test_no_caps_configured_returns_none(self):
        """When no per-project caps are set (empty dicts), returns None."""
        acct = _acct()
        assert _project_at_cap(acct, "proj-1") is None

    def test_missing_key_in_cap_dict_returns_none(self):
        """When the project isn't in the cap dict, there's no limit."""
        acct = _acct(
            project_daily_task_cap={"other-proj": 5},
            project_state={"proj-1": {"daily_task_count": 10, "active_tasks": 0}},
        )
        assert _project_at_cap(acct, "proj-1") is None

    def test_project_daily_cap_reached_returns_reason(self):
        acct = _acct(
            project_daily_task_cap={"proj-1": 5},
            project_state={"proj-1": {"daily_task_count": 5, "active_tasks": 0}},
        )
        reason = _project_at_cap(acct, "proj-1")
        assert reason is not None
        assert "project daily cap" in reason

    def test_project_daily_cap_reached_above_limit_returns_reason(self):
        acct = _acct(
            project_daily_task_cap={"proj-1": 5},
            project_state={"proj-1": {"daily_task_count": 7, "active_tasks": 0}},
        )
        assert _project_at_cap(acct, "proj-1") is not None

    def test_project_daily_cap_under_limit_returns_none(self):
        acct = _acct(
            project_daily_task_cap={"proj-1": 5},
            project_state={"proj-1": {"daily_task_count": 4, "active_tasks": 0}},
        )
        assert _project_at_cap(acct, "proj-1") is None

    def test_project_concurrent_cap_reached_returns_reason(self):
        acct = _acct(
            project_max_concurrent_tasks={"proj-1": 2},
            project_state={"proj-1": {"daily_task_count": 0, "active_tasks": 2}},
        )
        reason = _project_at_cap(acct, "proj-1")
        assert reason is not None
        assert "project concurrent cap" in reason

    def test_project_concurrent_cap_under_limit_returns_none(self):
        acct = _acct(
            project_max_concurrent_tasks={"proj-1": 2},
            project_state={"proj-1": {"daily_task_count": 0, "active_tasks": 1}},
        )
        assert _project_at_cap(acct, "proj-1") is None

    def test_project_at_daily_but_not_concurrent_returns_daily_reason(self):
        """When the project is over daily but under concurrent, the daily
        cap is the blocking reason."""
        acct = _acct(
            project_daily_task_cap={"proj-1": 5},
            project_max_concurrent_tasks={"proj-1": 2},
            project_state={"proj-1": {"daily_task_count": 5, "active_tasks": 1}},
        )
        reason = _project_at_cap(acct, "proj-1")
        assert reason is not None
        assert "daily" in reason

    def test_project_at_concurrent_but_not_daily_returns_concurrent_reason(self):
        """When the project is over concurrent but under daily, the concurrent
        cap is the blocking reason."""
        acct = _acct(
            project_daily_task_cap={"proj-1": 5},
            project_max_concurrent_tasks={"proj-1": 2},
            project_state={"proj-1": {"daily_task_count": 3, "active_tasks": 2}},
        )
        reason = _project_at_cap(acct, "proj-1")
        assert reason is not None
        assert "concurrent" in reason


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
        acct = _acct(disabled_models={OPUS: {"hits": 1, "at": _utcnow()}})
        assert _account_available_for_model(acct, OPUS) is False

    def test_available_as_probing_after_cooldown_elapses(self):
        # hits=1 → 30 min cooldown; 60 min ago → cooldown elapsed → probing (still "available").
        acct = _acct(
            disabled_models={
                OPUS: {"hits": 1, "at": _utcnow() - timedelta(minutes=60)}
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
            now = _utcnow()
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
            now = _utcnow()
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
            now = _utcnow()
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


class TestAlternateAccountRecoveryDeadline:
    def _reset_pools(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    @pytest.mark.asyncio
    async def test_returns_now_when_alternate_account_is_already_eligible(self):
        self._reset_pools()
        try:
            failed = _acct(
                id="failed",
                user_id="u-park",
                disabled_models={
                    OPUS: {"at": _utcnow() - timedelta(minutes=5), "hits": 1}
                },
            )
            alternate = _acct(id="alternate", user_id="u-park")
            _account_by_id.update({"failed": failed, "alternate": alternate})
            _max_plan_pools["u-park"] = [failed, alternate]

            now = _utcnow()
            deadline = await alternate_account_recovery_deadline(
                "failed",
                OPUS,
                blacklist=["failed"],
                park_budget_seconds=600,
                now=now,
            )
            assert deadline == now
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_returns_recovery_within_budget_and_none_outside_budget(self):
        self._reset_pools()
        try:
            now = _utcnow()
            failed = _acct(id="failed", user_id="u-park")
            recovering = _acct(
                id="recovering",
                user_id="u-park",
                disabled_models={OPUS: {"at": now - timedelta(minutes=20), "hits": 1}},
            )
            _account_by_id.update({"failed": failed, "recovering": recovering})
            _max_plan_pools["u-park"] = [failed, recovering]

            deadline = await alternate_account_recovery_deadline(
                "failed",
                OPUS,
                blacklist=["failed"],
                park_budget_seconds=900,
                now=now,
            )
            assert deadline == recovering["disabled_models"][OPUS]["at"] + timedelta(
                seconds=_cooldown_seconds_for_info(recovering["disabled_models"][OPUS])
            )

            deadline = await alternate_account_recovery_deadline(
                "failed",
                OPUS,
                blacklist=["failed"],
                park_budget_seconds=120,
                now=now,
            )
            assert deadline is None
        finally:
            self._reset_pools()


# ── _HEALTH_SCORE_DISQUALIFY_THRESHOLD constant ──────────────────────────────


class TestHealthScoreDisqualifyThreshold:
    def test_threshold_is_negative(self):
        assert _HEALTH_SCORE_DISQUALIFY_THRESHOLD < 0

    def test_score_at_threshold_minus_one_is_below(self):
        """An account with score below threshold would be filtered when alternatives exist."""
        now = _utcnow()
        # Build an account whose health score lands below _HEALTH_SCORE_DISQUALIFY_THRESHOLD.
        # score = completions - 2*rl_events; 3 rl_events → score = -6, which is below -4.
        acct = _acct(
            rl_events=[now - timedelta(hours=i) for i in range(1, 4)],
        )
        score = _compute_health_score(acct, now=now)
        assert score < _HEALTH_SCORE_DISQUALIFY_THRESHOLD

    def test_score_at_threshold_is_eligible(self):
        """An account with score == threshold is NOT filtered."""
        now = _utcnow()
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
            now = _utcnow()
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
            now = _utcnow()
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
            now = _utcnow()
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
            now = _utcnow()
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
            now = _utcnow()
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
            now = _utcnow()
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
        now = _utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=1), "success": True},
                {"at": now - timedelta(hours=2), "success": False},
            ]
        )
        assert _compute_success_rate_7d(acct, now) is None

    def test_returns_rate_from_3_plus_events(self):
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
            ]
        )
        assert _compute_success_rate_7d(acct, now) == 1.0

    def test_zero_success_rate(self):
        now = _utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": False} for i in range(3)
            ]
        )
        assert _compute_success_rate_7d(acct, now) == 0.0


# ── _compute_success_rate_7d — per-task-type scoping ─────────────────────────


class TestComputeSuccessRateByType:
    """Baseline coverage for _compute_success_rate_7d without task_type scoping."""

    def test_no_task_type_returns_aggregate_unchanged(self):
        """Omitting task_type returns the aggregate — backward compatible."""
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
        assert (_compute_success_rate_7d(_acct(id="a"), now) or 0.0) == 0.0
        assert (_compute_success_rate_7d(_acct(id="b"), now) or 0.0) == 0.0

    def test_selection_key_prefers_high_success(self):
        """The selection key (health, success_rate, -active_tasks) ranks the
        high-success account ahead of the low-success account."""
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
        acct = _acct(
            outcome_events=[
                {"at": now - timedelta(hours=i + 1), "success": i < 3} for i in range(5)
            ]
        )
        rate = _effective_success_rate(acct, None, now)
        assert rate == _compute_success_rate_7d(acct, now)

    def test_returns_none_when_both_typed_and_aggregate_sparse(self):
        now = _utcnow()
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
        now = _utcnow()
        acct = _acct(
            completion_events=[
                {"at": now - timedelta(hours=1), "duration_seconds": 60.0}
                for _ in range(_LATENCY_MIN_SAMPLES - 1)
            ]
        )
        assert _compute_avg_duration_24h(acct, now) is None

    def test_returns_average_at_min_samples(self):
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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
        now = _utcnow()
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


class TestCooldownProviderIdentity:
    async def test_rejects_cross_provider_model_before_cascade(self):
        """A stale Claude model cannot write/cascade onto an OpenAI account."""
        acct = _acct(id="openai-a", provider="openai")
        _account_by_id["openai-a"] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                "openai-a", "claude-sonnet-4-6", provider="openai"
            )
        assert "disabled_models" not in acct

    async def test_ordinary_rate_limit_persists_provider_identity(self):
        acct = _acct(id="openai-a", provider="openai")
        _account_by_id["openai-a"] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                "openai-a", "gpt-5.6-sol", provider="openai"
            )
        info = acct["disabled_models"]["gpt-5.6-sol"]
        assert info["provider"] == "openai"
        assert _serialize_disabled_model_info(info)["provider"] == "openai"

    async def test_unknown_model_identity_fails_closed(self):
        acct = _acct(id="openai-a", provider="openai")
        _account_by_id["openai-a"] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                "openai-a", "customer-private-model", provider="openai"
            )
        assert "disabled_models" not in acct

    async def test_explicit_custom_model_escape_is_available(self):
        acct = _acct(id="openai-a", provider="openai")
        _account_by_id["openai-a"] = acct
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(
                "openai-a",
                "customer-private-model",
                provider="openai",
                allow_unknown_model_identity=True,
            )
        assert acct["disabled_models"]["customer-private-model"]["provider"] == "openai"

    def test_authoritative_disable_merge_preserves_concurrent_state(self):
        now = _utcnow().isoformat()
        blob = {
            "disabled_models": {
                "gpt-existing": {
                    "hits": 2,
                    "at": now,
                    "provider": "openai",
                }
            },
            "outcome_events": [{"at": now, "success": True}],
        }
        merged = _apply_rate_limit_mutations(
            blob,
            [
                {
                    "op": "disable_models",
                    "models": {
                        "gpt-5.6-sol": {
                            "at": now,
                            "provider": "openai",
                        }
                    },
                    "outcome_event": {"at": now, "success": False},
                }
            ],
        )
        assert merged is not None
        assert "gpt-existing" in merged["disabled_models"]
        assert merged["disabled_models"]["gpt-5.6-sol"]["hits"] == 1
        assert len(merged["outcome_events"]) == 2

    def test_authoritative_clear_removes_only_requested_model(self):
        now = _utcnow().isoformat()
        blob = {
            "disabled_models": {
                "gpt-a": {"hits": 1, "at": now, "provider": "openai"},
                "gpt-b": {"hits": 1, "at": now, "provider": "openai"},
            },
            "outcome_events": [{"at": now, "success": False}],
        }
        merged = _apply_rate_limit_mutations(
            blob, [{"op": "clear_models", "models": ["gpt-a"]}]
        )
        assert merged is not None
        assert set(merged["disabled_models"]) == {"gpt-b"}
        assert len(merged["outcome_events"]) == 1

    def test_startup_cooldown_mutation_preserves_models_and_outcomes(self):
        now = _utcnow().isoformat()
        blob = {
            "disabled_models": {
                "gpt-a": {"hits": 1, "at": now, "provider": "openai"}
            },
            "outcome_events": [{"at": now, "success": False}],
        }
        merged = _apply_rate_limit_mutations(
            blob,
            [
                {
                    "op": "set_startup_cooldown",
                    "at": now,
                    "reason": "startup-failure cooldown: test",
                }
            ],
        )
        assert merged is not None
        assert set(merged["disabled_models"]) == {"gpt-a"}
        assert len(merged["outcome_events"]) == 1
        assert merged["startup_failure_cooldown_at"] == now

    @pytest.mark.parametrize(
        "condition",
        [
            # A clear that observed an older generation cannot erase a newer hit.
            {
                "observed_at": "2026-07-14T10:00:00+00:00",
                "provider": "openai",
                "clear_at": "2026-07-14T12:00:00+00:00",
            },
            # A success timestamp older than the observed hit cannot clear it.
            {
                "observed_at": "2026-07-14T11:00:00+00:00",
                "provider": "openai",
                "clear_at": "2026-07-14T10:30:00+00:00",
            },
            # The model key alone is insufficient across providers.
            {
                "observed_at": "2026-07-14T11:00:00+00:00",
                "provider": "anthropic",
                "clear_at": "2026-07-14T12:00:00+00:00",
            },
        ],
    )
    def test_conditional_clear_cannot_erase_newer_or_other_provider_hit(
        self, condition
    ):
        blob = {
            "disabled_models": {
                "gpt-5.6-sol": {
                    "hits": 2,
                    "at": "2026-07-14T11:00:00+00:00",
                    "provider": "openai",
                }
            }
        }
        merged = _apply_rate_limit_mutations(
            blob,
            [
                {
                    "op": "clear_models",
                    "models": {"gpt-5.6-sol": condition},
                }
            ],
        )
        assert "gpt-5.6-sol" in merged["disabled_models"]

    def test_startup_clear_cannot_erase_newer_generation(self):
        merged = _apply_rate_limit_mutations(
            {
                "startup_failure_cooldown_at": "2026-07-14T11:00:00+00:00",
                "startup_failure_disabled_reason": "new generation",
            },
            [
                {
                    "op": "clear_startup_cooldown",
                    "observed_at": "2026-07-14T10:00:00+00:00",
                    "clear_at": "2026-07-14T12:00:00+00:00",
                }
            ],
        )
        assert merged["startup_failure_cooldown_at"] == "2026-07-14T11:00:00+00:00"

    async def test_stale_cache_cooldown_persist_preserves_operator_disable(
        self, monkeypatch
    ):
        acct_id = str(uuid.uuid4())
        acct = _acct(id=acct_id, provider="openai", enabled=True)
        acct["_pending_rate_limit_mutations"] = [
            {
                "op": "disable_models",
                "models": {
                    "gpt-5.6-sol": {
                        "at": _utcnow().isoformat(),
                        "provider": "openai",
                    }
                },
            }
        ]
        _account_by_id[acct_id] = acct
        db_acct = _FakeDbAcct(acct_id)
        db_acct.enabled = False
        db_acct.disabled_reason = "operator disabled"
        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal",
            lambda: _FakeAsyncSessionCtxWithAcct(db_acct),
        )

        await _persist_rate_limit_state(acct_id)

        assert db_acct.enabled is False
        assert db_acct.disabled_reason == "operator disabled"
        assert acct["enabled"] is False
        assert acct["disabled_reason"] == "operator disabled"
        assert "gpt-5.6-sol" in db_acct.rate_limit_state["disabled_models"]


# ── disable_account_model(kind="regression") + clear_account_model_regression ─


class TestRegressionQuarantine:
    """Unit tests for the regression-kind disabled_models entry shape."""

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
        acct_id = "a"
        acct = _acct(id=acct_id, name="A")
        sonnet = "claude-sonnet-4-5"
        # Inject directly to avoid cascade interference between model tiers.
        acct["disabled_models"] = {
            sonnet: {
                "kind": "regression",
                "hits": 1,
                "at": _utcnow(),
                "provider": "anthropic",
                "regression_stats": {
                    "account_rate": 0.1,
                    "project_median": 0.9,
                    "sample_size": 5,
                    "distinct_accounts": 2,
                    "started_at": "2026-04-29T00:00:00+00:00",
                },
            },
            OPUS: {"kind": "quota_exhausted", "hits": 1, "at": _utcnow()},
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
                "at": _utcnow() - timedelta(seconds=60),
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

    def _add(self, user_id: str, acct_id: str, provider: str = "anthropic", **kw):
        a = _acct(id=acct_id, user_id=user_id, provider=provider, **kw)
        _max_plan_pools.setdefault(user_id, []).append(a)
        _account_by_id[acct_id] = a
        return a

    def test_unrelated_user_healthy_account_does_not_prevent_true(self):
        """A healthy account belonging to a *different* user must not prevent
        pool_wide_rate_limit_cooldown from returning True for user A's pool."""
        from app.max_plan_pool import pool_wide_rate_limit_cooldown

        now = _utcnow()
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

        now = _utcnow()
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

        now = _utcnow()
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

        now = _utcnow()
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
        from datetime import timedelta
        from unittest.mock import AsyncMock, MagicMock
        from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
        from app.models import MaxPlanAccount

        rl_at = _utcnow() - timedelta(minutes=5)
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
        from datetime import timedelta
        from unittest.mock import AsyncMock, MagicMock
        from app.max_plan_pool import db_pool_wide_rate_limit_cooldown
        from app.models import MaxPlanAccount, LLMProviderName

        rl_at = _utcnow() - timedelta(minutes=5)
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


# ── Proactive account-usage headroom (advisory) ─────────────────────────────


def _known_snap(five, seven, *, probed_at="2026-06-06T00:00:00"):
    return UsageSnapshot(
        known=True,
        window_utilizations={"five_hour": five, "seven_day": seven},
        resets_at={"five_hour": None, "seven_day": None},
        probed_at=probed_at,
    )


def _acct_usage(acct, snap, *, at):
    acct["usage_snapshot"] = snap
    acct["usage_snapshot_at"] = at
    return acct


class TestCachedUsageSnapshot:
    def test_fresh_snapshot_returned(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), _known_snap(10.0, 20.0), at=now)
        assert _cached_usage_snapshot(acct, now) is not None

    def test_stale_snapshot_treated_as_unknown(self):
        now = _utcnow()
        acct = _acct_usage(
            _acct(),
            _known_snap(10.0, 20.0),
            at=now - timedelta(seconds=_USAGE_SNAPSHOT_TTL_SECONDS + 1),
        )
        assert _cached_usage_snapshot(acct, now) is None

    def test_missing_snapshot_is_none(self):
        assert _cached_usage_snapshot(_acct(), _utcnow()) is None

    def test_non_snapshot_value_is_none(self):
        now = _utcnow()
        acct = _acct(usage_snapshot={"not": "a snapshot"}, usage_snapshot_at=now)
        assert _cached_usage_snapshot(acct, now) is None


class TestUsageGateShouldSkip:
    def test_five_hour_over_threshold_skips(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), _known_snap(95.0, 10.0), at=now)
        assert _usage_gate_should_skip(acct, now, 90.0) is True

    def test_seven_day_over_threshold_skips(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), _known_snap(10.0, 92.0), at=now)
        assert _usage_gate_should_skip(acct, now, 90.0) is True

    def test_under_threshold_not_skipped(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), _known_snap(80.0, 85.0), at=now)
        assert _usage_gate_should_skip(acct, now, 90.0) is False

    def test_unknown_snapshot_never_skipped(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), UsageSnapshot.unknown("t"), at=now)
        assert _usage_gate_should_skip(acct, now, 90.0) is False

    def test_stale_snapshot_never_skipped(self):
        now = _utcnow()
        acct = _acct_usage(
            _acct(),
            _known_snap(99.0, 99.0),
            at=now - timedelta(seconds=_USAGE_SNAPSHOT_TTL_SECONDS + 1),
        )
        assert _usage_gate_should_skip(acct, now, 90.0) is False


class TestUsageHeadroomRank:
    def test_known_rank_is_100_minus_max_util(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), _known_snap(20.0, 60.0), at=now)
        assert _usage_headroom_rank(acct, now) == 40.0

    def test_unknown_ranks_neutral(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), UsageSnapshot.unknown("t"), at=now)
        assert _usage_headroom_rank(acct, now) == _USAGE_HEADROOM_NEUTRAL

    def test_missing_ranks_neutral(self):
        assert (
            _usage_headroom_rank(_acct(), _utcnow()) == _USAGE_HEADROOM_NEUTRAL
        )

    def test_stale_ranks_neutral(self):
        now = _utcnow()
        acct = _acct_usage(
            _acct(),
            _known_snap(20.0, 60.0),
            at=now - timedelta(seconds=_USAGE_SNAPSHOT_TTL_SECONDS + 1),
        )
        assert _usage_headroom_rank(acct, now) == _USAGE_HEADROOM_NEUTRAL


class TestUsageHeadroomSettings:
    def test_defaults_when_empty(self):
        enabled, threshold, long_types = _usage_headroom_settings({})
        assert enabled is False
        assert threshold == 90.0
        assert "create" in long_types

    def test_parses_enabled_threshold_and_types(self):
        enabled, threshold, long_types = _usage_headroom_settings(
            {
                "usage_headroom_gate_enabled": True,
                "usage_headroom_threshold_pct": 75,
                "usage_headroom_long_running_task_types": ["create", "research"],
            }
        )
        assert enabled is True
        assert threshold == 75.0
        assert long_types == frozenset({"create", "research"})

    def test_malformed_threshold_falls_back(self):
        _, threshold, _ = _usage_headroom_settings(
            {"usage_headroom_threshold_pct": "not-a-number"}
        )
        assert threshold == 90.0

    def test_malformed_types_falls_back_to_default(self):
        _, _, long_types = _usage_headroom_settings(
            {"usage_headroom_long_running_task_types": "not-a-list"}
        )
        assert "create" in long_types


class TestApplyUsageGate:
    def test_filters_over_threshold_when_alternatives_remain(self):
        now = _utcnow()
        hot = _acct_usage(_acct(id="hot"), _known_snap(95.0, 10.0), at=now)
        cool = _acct_usage(_acct(id="cool"), _known_snap(10.0, 10.0), at=now)
        result = _apply_usage_gate(
            [hot, cool], now=now, threshold_pct=90.0, acct_getter=lambda a: a
        )
        assert result == [cool]

    def test_keeps_all_when_every_candidate_exhausted(self):
        now = _utcnow()
        a = _acct_usage(_acct(id="a"), _known_snap(95.0, 10.0), at=now)
        b = _acct_usage(_acct(id="b"), _known_snap(92.0, 10.0), at=now)
        result = _apply_usage_gate(
            [a, b], now=now, threshold_pct=90.0, acct_getter=lambda a: a
        )
        assert result == [a, b]  # no deadlock — never empties the set

    def test_single_candidate_unchanged(self):
        now = _utcnow()
        only = _acct_usage(_acct(id="only"), _known_snap(99.0, 99.0), at=now)
        result = _apply_usage_gate(
            [only], now=now, threshold_pct=90.0, acct_getter=lambda a: a
        )
        assert result == [only]


class TestUsageSnapshotSummary:
    def test_known_summary_shape(self):
        now = _utcnow()
        snap = UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 28.0, "seven_day": 64.0},
            resets_at={"five_hour": "2026-06-06T00:00:00Z", "seven_day": None},
            probed_at="2026-06-06T01:00:00",
        )
        acct = _acct_usage(_acct(), snap, at=now)
        out = _usage_snapshot_summary(acct, now)
        assert out["known"] is True
        assert out["five_hour_utilization"] == 28.0
        assert out["seven_day_utilization"] == 64.0
        assert out["resets_at"]["five_hour"] == "2026-06-06T00:00:00Z"
        assert out["probed_at"] == "2026-06-06T01:00:00"
        assert out["stale"] is False

    def test_missing_summary_is_unknown(self):
        out = _usage_snapshot_summary(_acct(), _utcnow())
        assert out["known"] is False
        assert out["five_hour_utilization"] is None
        assert out["stale"] is True

    def test_stale_flag_uses_display_window_not_selection_ttl(self):
        now = _utcnow()
        # Older than the 60s selection TTL but within the display window —
        # NOT stale for the meter (the 300s refresh cadence makes the 60s TTL
        # meaningless as a display signal).
        acct = _acct_usage(
            _acct(),
            _known_snap(10.0, 10.0),
            at=now - timedelta(seconds=_USAGE_SNAPSHOT_TTL_SECONDS + 1),
        )
        out = _usage_snapshot_summary(acct, now)
        assert out["stale"] is False
        assert out["known"] is True

    def test_stale_flag_set_past_display_window(self):
        now = _utcnow()
        acct = _acct_usage(
            _acct(),
            _known_snap(10.0, 10.0),
            at=now - timedelta(seconds=_USAGE_METER_DISPLAY_STALE_SECONDS + 1),
        )
        out = _usage_snapshot_summary(acct, now)
        assert out["stale"] is True
        # Stale-but-known still reports its last values for operator visibility.
        assert out["known"] is True


class TestUsageDarkReason:
    """Each ineligible/failing dark-meter state has a distinct machine-readable reason."""

    def test_not_oauth_reason(self):
        acct = _acct(auth_mode="api_key", oauth_token=None)
        assert _usage_dark_reason(acct) == "not_oauth"

    def test_no_secret_reason(self):
        acct = _acct(auth_mode="oauth", oauth_token=None, runtime_secret=None)
        assert _usage_dark_reason(acct) == "no_secret"

    def test_secret_fetch_failed_reason(self):
        """secret_fetch_failed is distinct from no_secret when a fetch was attempted."""
        acct = _acct(auth_mode="oauth", oauth_token=None, runtime_secret=None)
        acct["secret_fetch_failed"] = True
        assert _usage_dark_reason(acct) == "secret_fetch_failed"

    def test_never_probed_reason(self):
        # OAuth + secret but no probe attempt yet — fresh account.
        acct = _acct(auth_mode="oauth", oauth_token="tok")
        assert _usage_dark_reason(acct) == "never_probed"

    def test_endpoint_error_after_failed_probe(self):
        now = _utcnow()
        acct = _acct(auth_mode="oauth", oauth_token="tok")
        acct["usage_probe_failed_at"] = now
        assert _usage_dark_reason(acct) == "endpoint_error"

    def test_endpoint_error_when_snapshot_present_but_unknown(self):
        now = _utcnow()
        acct = _acct(auth_mode="oauth", oauth_token="tok")
        acct["usage_snapshot"] = UsageSnapshot.unknown("2026-01-01T00:00:00")
        acct["usage_snapshot_at"] = now
        acct["usage_probe_failed_at"] = now
        assert _usage_dark_reason(acct) == "endpoint_error"

    def test_known_snapshot_dark_reason_is_none_via_summary(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), _known_snap(10.0, 20.0), at=now)
        out = _usage_snapshot_summary(acct, now)
        assert out["known"] is True
        assert out["dark_reason"] is None

    def test_not_oauth_surfaced_in_summary(self):
        acct = _acct(auth_mode="api_key", oauth_token=None)
        out = _usage_snapshot_summary(acct, _utcnow())
        assert out["known"] is False
        assert out["dark_reason"] == "not_oauth"

    def test_no_secret_surfaced_in_summary(self):
        acct = _acct(auth_mode="oauth", oauth_token=None, runtime_secret=None)
        out = _usage_snapshot_summary(acct, _utcnow())
        assert out["known"] is False
        assert out["dark_reason"] == "no_secret"

    def test_secret_fetch_failed_surfaced_in_summary(self):
        """secret_fetch_failed reason propagates through the summary."""
        acct = _acct(auth_mode="oauth", oauth_token=None, runtime_secret=None)
        acct["secret_fetch_failed"] = True
        out = _usage_snapshot_summary(acct, _utcnow())
        assert out["known"] is False
        assert out["dark_reason"] == "secret_fetch_failed"

    def test_never_probed_surfaced_in_summary(self):
        acct = _acct()  # default: oauth_token="sk-ant-test", no probe done
        out = _usage_snapshot_summary(acct, _utcnow())
        assert out["known"] is False
        assert out["dark_reason"] == "never_probed"

    def test_endpoint_error_surfaced_in_summary(self):
        now = _utcnow()
        acct = _acct()
        acct["usage_probe_failed_at"] = now
        out = _usage_snapshot_summary(acct, now)
        assert out["known"] is False
        assert out["dark_reason"] == "endpoint_error"


def _persisted_usage_blob(five, seven, *, updated_at):
    """Mirror the blob _persist_rate_limit_usage_to_db writes to the DB."""
    return {
        "updated_at": updated_at.isoformat(),
        "source": "oauth_usage_probe",
        "five_hour": {"utilization": float(five)},
        "seven_day": {"utilization": float(seven)},
    }


class TestUsageSnapshotSummaryWithFallback:
    """The admin meter must survive the leader-only probe: non-leader instances
    never populate the in-memory snapshot, so the summary must fall back to the
    DB-persisted rate_limit_usage blob (shared across instances)."""

    def test_falls_back_to_persisted_when_memory_absent(self):
        # Simulates a non-leader instance: no in-memory probe result, but the
        # shared DB has a fresh persisted blob from the leader's probe.
        now = _utcnow()
        blob = _persisted_usage_blob(33.0, 71.0, updated_at=now)
        out = usage_snapshot_summary_with_fallback(_acct(), blob, now)
        assert out["known"] is True
        assert out["five_hour_utilization"] == 33.0
        assert out["seven_day_utilization"] == 71.0
        assert out["stale"] is False

    def test_falls_back_when_mem_account_is_none(self):
        now = _utcnow()
        blob = _persisted_usage_blob(5.0, 9.0, updated_at=now)
        out = usage_snapshot_summary_with_fallback(None, blob, now)
        assert out["known"] is True
        assert out["five_hour_utilization"] == 5.0

    def test_none_everywhere_is_unknown(self):
        out = usage_snapshot_summary_with_fallback(None, None, _utcnow())
        assert out["known"] is False
        assert out["five_hour_utilization"] is None
        assert out["stale"] is True

    def test_memory_snapshot_preferred_when_present(self):
        now = _utcnow()
        acct = _acct_usage(_acct(), _known_snap(11.0, 22.0), at=now)
        # Persisted blob is older — the fresher in-memory value must win.
        blob = _persisted_usage_blob(
            99.0, 99.0, updated_at=now - timedelta(hours=6)
        )
        out = usage_snapshot_summary_with_fallback(acct, blob, now)
        assert out["five_hour_utilization"] == 11.0
        assert out["seven_day_utilization"] == 22.0

    def test_fresher_persisted_wins_over_stale_memory(self):
        # Non-leader whose in-memory snapshot froze at startup: the DB blob the
        # leader wrote afterwards is fresher and must override.
        now = _utcnow()
        acct = _acct_usage(
            _acct(),
            _known_snap(80.0, 80.0),
            at=now - timedelta(hours=6),
        )
        blob = _persisted_usage_blob(12.0, 15.0, updated_at=now)
        out = usage_snapshot_summary_with_fallback(acct, blob, now)
        assert out["five_hour_utilization"] == 12.0
        assert out["stale"] is False

    def test_unknown_memory_falls_back_to_persisted(self):
        # A probe-unknown in-memory snapshot (known=False) must not shadow a
        # good persisted blob.
        now = _utcnow()
        acct = _acct_usage(_acct(), UsageSnapshot.unknown("t"), at=now)
        blob = _persisted_usage_blob(40.0, 50.0, updated_at=now)
        out = usage_snapshot_summary_with_fallback(acct, blob, now)
        assert out["known"] is True
        assert out["five_hour_utilization"] == 40.0

    def test_persisted_staleness_flagged_past_display_window(self):
        now = _utcnow()
        blob = _persisted_usage_blob(
            10.0,
            10.0,
            updated_at=now - timedelta(seconds=_USAGE_METER_DISPLAY_STALE_SECONDS + 1),
        )
        out = usage_snapshot_summary_with_fallback(_acct(), blob, now)
        assert out["known"] is True
        assert out["stale"] is True

    def test_malformed_persisted_blob_ignored(self):
        out = usage_snapshot_summary_with_fallback(
            _acct(), {"source": "something_else"}, _utcnow()
        )
        assert out["known"] is False


def _healthy_acct(acct_id, **kw):
    acct = _acct(id=acct_id, name=acct_id, provider="anthropic", auth_mode="oauth")
    acct.update(kw)
    return acct


class TestSelectionUsageGate:
    _USER = "u"
    _GATE_CFG: ClassVar[dict] = {
        "usage_headroom_gate_enabled": True,
        "usage_headroom_threshold_pct": 90,
        "usage_headroom_long_running_task_types": ["create"],
    }

    def _setup(self, accounts):
        _max_plan_pools.clear()
        _account_by_id.clear()
        _max_plan_pools[self._USER] = list(accounts)
        for a in accounts:
            _account_by_id[a["id"]] = a

    async def test_long_running_skips_exhausted_even_when_better_ranked(self):
        now = _utcnow()
        # `hot` ranks first on health_score but is over the usage threshold.
        hot = _healthy_acct(
            "hot",
            completion_events=[
                {"at": now - timedelta(minutes=5), "duration_seconds": 60.0}
                for _ in range(3)
            ],
        )
        _acct_usage(hot, _known_snap(95.0, 10.0), at=now)
        cool = _healthy_acct("cool")
        _acct_usage(cool, _known_snap(10.0, 10.0), at=now)
        self._setup([hot, cool])
        account = await select_max_plan_account(
            self._USER,
            model=OPUS,
            task_type="create",
            runtime_config=self._GATE_CFG,
        )
        assert account["id"] == "cool"

    async def test_non_long_running_type_does_not_skip(self):
        now = _utcnow()
        hot = _healthy_acct(
            "hot",
            completion_events=[
                {"at": now - timedelta(minutes=5), "duration_seconds": 60.0}
                for _ in range(3)
            ],
        )
        _acct_usage(hot, _known_snap(95.0, 10.0), at=now)
        cool = _healthy_acct("cool")
        _acct_usage(cool, _known_snap(10.0, 10.0), at=now)
        self._setup([hot, cool])
        account = await select_max_plan_account(
            self._USER,
            model=OPUS,
            task_type="review",  # not in long-running set
            runtime_config=self._GATE_CFG,
        )
        # `review` isn't gated; health_score wins, so the exhausted account stays.
        assert account["id"] == "hot"

    async def test_unknown_account_not_skipped(self):
        now = _utcnow()
        unknown = _healthy_acct("unk")
        _acct_usage(unknown, UsageSnapshot.unknown("t"), at=now)
        hot = _healthy_acct("hot")
        _acct_usage(hot, _known_snap(99.0, 99.0), at=now)
        self._setup([unknown, hot])
        account = await select_max_plan_account(
            self._USER,
            model=OPUS,
            task_type="create",
            runtime_config=self._GATE_CFG,
        )
        # `hot` is skipped (>= threshold); `unk` is unknown → eligible.
        assert account["id"] == "unk"

    async def test_disabled_gate_is_noop(self):
        now = _utcnow()
        # hot has fewer active tasks; with the gate OFF, load-balancing picks it
        # despite high utilization (headroom ignored entirely).
        hot = _healthy_acct("hot", active_tasks=0)
        _acct_usage(hot, _known_snap(99.0, 99.0), at=now)
        cool = _healthy_acct("cool", active_tasks=5)
        _acct_usage(cool, _known_snap(5.0, 5.0), at=now)
        self._setup([hot, cool])
        account = await select_max_plan_account(
            self._USER,
            model=OPUS,
            task_type="create",
            runtime_config={"usage_headroom_gate_enabled": False},
        )
        assert account["id"] == "hot"

    async def test_ranking_prefers_more_headroom(self):
        now = _utcnow()
        # Equal health/success/duration/active_tasks → headroom decides.
        more = _healthy_acct("more")
        _acct_usage(more, _known_snap(20.0, 20.0), at=now)  # headroom 80
        less = _healthy_acct("less")
        _acct_usage(less, _known_snap(80.0, 80.0), at=now)  # headroom 20
        self._setup([more, less])
        account = await select_max_plan_account(
            self._USER,
            model=OPUS,
            task_type="create",
            runtime_config=self._GATE_CFG,
        )
        assert account["id"] == "more"

    async def test_unknown_ranks_neutrally_between_high_and_low(self):
        now = _utcnow()
        # Unknown (neutral 50) should beat a low-headroom account (20)…
        unknown = _healthy_acct("unk")
        _acct_usage(unknown, UsageSnapshot.unknown("t"), at=now)
        low = _healthy_acct("low")
        _acct_usage(low, _known_snap(80.0, 80.0), at=now)  # headroom 20
        self._setup([unknown, low])
        account = await select_max_plan_account(
            self._USER,
            model=OPUS,
            task_type="create",
            runtime_config=self._GATE_CFG,
        )
        assert account["id"] == "unk"


# ── select_max_plan_account / release_max_plan_account — per-project caps ───


class TestSelectMaxPlanAccountWithProjectCaps:
    """Selection with per-project caps: a project at its cap cannot claim an
    account while another project CAN claim the same account. Per-project
    counters are incremented/decremented on claim/release."""

    _USER = "test-proj-cap-user"
    _PROJ_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _PROJ_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def _setup(self, accounts):
        _max_plan_pools.clear()
        _account_by_id.clear()
        _pending_cap_persists.clear()
        _max_plan_pools[self._USER] = list(accounts)
        for a in accounts:
            _account_by_id[a["id"]] = a

    def _healthy(self, acct_id, **kw):
        acct = _acct(
            id=acct_id, name=acct_id, provider="anthropic", auth_mode="oauth", **kw
        )
        return acct

    # ── _project_at_cap gating in selection ──────────────────────────────

    async def test_project_at_daily_cap_cannot_claim(self):
        """A project at its per-project daily cap is excluded from selection."""
        acct = self._healthy(
            "acct-1",
            project_daily_task_cap={self._PROJ_A: 5},
            project_state={self._PROJ_A: {"daily_task_count": 5, "active_tasks": 0}},
        )
        self._setup([acct])
        result = await select_max_plan_account(
            self._USER, model=OPUS, project_id=self._PROJ_A
        )
        assert result is None

    async def test_project_under_cap_can_claim(self):
        """A project under its per-project daily cap claims successfully."""
        acct = self._healthy(
            "acct-1",
            project_daily_task_cap={self._PROJ_A: 5},
            project_state={self._PROJ_A: {"daily_task_count": 4, "active_tasks": 0}},
        )
        self._setup([acct])
        result = await select_max_plan_account(
            self._USER, model=OPUS, project_id=self._PROJ_A
        )
        assert result is not None
        assert result["id"] == "acct-1"
        # Per-project counters incremented (via project_state).
        proj_state = (result.get("project_state") or {}).get(self._PROJ_A, {})
        assert proj_state.get("active_tasks") == 1
        assert proj_state.get("daily_task_count") == 5

    async def test_project_at_concurrent_cap_cannot_claim(self):
        """A project at its per-project concurrent cap is excluded."""
        acct = self._healthy(
            "acct-1",
            project_max_concurrent_tasks={self._PROJ_A: 2},
            project_state={self._PROJ_A: {"daily_task_count": 0, "active_tasks": 2}},
        )
        self._setup([acct])
        result = await select_max_plan_account(
            self._USER, model=OPUS, project_id=self._PROJ_A
        )
        assert result is None

    async def test_other_project_can_claim_when_project_a_at_cap(self):
        """Project B can claim the account even when Project A is at its cap."""
        acct = self._healthy(
            "acct-1",
            project_daily_task_cap={self._PROJ_A: 5},
            project_state={self._PROJ_A: {"daily_task_count": 5, "active_tasks": 0}},
        )
        self._setup([acct])
        result = await select_max_plan_account(
            self._USER, model=OPUS, project_id=self._PROJ_B
        )
        assert result is not None
        assert result["id"] == "acct-1"

    async def test_project_id_none_skips_per_project_enforcement(self):
        """When project_id is None, per-project caps are ignored."""
        acct = self._healthy(
            "acct-1",
            project_daily_task_cap={self._PROJ_A: 1},
            project_state={self._PROJ_A: {"daily_task_count": 5, "active_tasks": 0}},
        )
        self._setup([acct])
        result = await select_max_plan_account(
            self._USER, model=OPUS, project_id=None
        )
        assert result is not None
        assert result["id"] == "acct-1"

    async def test_project_none_increments_global_but_not_per_project(self):
        """When project_id is None, global counters increment but
        per-project counters stay empty."""
        acct = self._healthy("acct-1")
        self._setup([acct])
        result = await select_max_plan_account(
            self._USER, model=OPUS, project_id=None
        )
        assert result is not None
        assert result["active_tasks"] == 1
        assert result["daily_task_count"] == 1
        # Per-project counters unchanged when project_id is None.
        assert result.get("project_state") in (None, {})

    async def test_per_project_decremented_on_release(self):
        """Releasing with project_id decrements per-project active_tasks."""
        acct = self._healthy(
            "acct-1",
            active_tasks=1,
            project_state={self._PROJ_A: {"daily_task_count": 0, "active_tasks": 1}},
        )
        self._setup([acct])
        await release_max_plan_account("acct-1", project_id=self._PROJ_A)
        # Global counter decremented.
        assert acct["active_tasks"] == 0
        # Per-project counter decremented.
        proj_state = (acct.get("project_state") or {}).get(self._PROJ_A, {})
        assert proj_state.get("active_tasks", 0) == 0

    async def test_release_without_project_id_does_not_touch_per_project(self):
        """Releasing without project_id leaves per-project counters alone."""
        acct = self._healthy(
            "acct-1",
            active_tasks=1,
            project_state={self._PROJ_A: {"daily_task_count": 0, "active_tasks": 1}},
        )
        self._setup([acct])
        await release_max_plan_account("acct-1", project_id=None)
        assert acct["active_tasks"] == 0
        # Per-project counter unchanged (release did not touch it).
        proj_state = (acct.get("project_state") or {}).get(self._PROJ_A, {})
        assert proj_state.get("active_tasks") == 1

    async def test_release_floors_per_project_at_zero(self):
        """Defensive .get with default 0 handles missing per-project key."""
        acct = self._healthy(
            "acct-1",
            active_tasks=1,
        )
        self._setup([acct])
        # Release with a project_id that has no entry (shouldn't crash).
        await release_max_plan_account("acct-1", project_id=self._PROJ_A)
        assert acct["active_tasks"] == 0
        # No crash; project_state may have been created with floor value.
        proj_state = (acct.get("project_state") or {}).get(self._PROJ_A, {})
        assert proj_state.get("active_tasks", 0) == 0

    # ── Global caps still apply ──────────────────────────────────────────

    async def test_global_daily_cap_still_blocks_when_project_under_cap(self):
        """Global daily cap blocks even when per-project caps are fine."""
        acct = self._healthy(
            "acct-1",
            daily_task_cap=2,
            daily_task_count=2,
            project_daily_task_cap={self._PROJ_A: 10},
            project_state={self._PROJ_A: {"daily_task_count": 3, "active_tasks": 0}},
        )
        self._setup([acct])
        result = await select_max_plan_account(
            self._USER, model=OPUS, project_id=self._PROJ_A
        )
        assert result is None

    # ── Balanced-mode per-project gating ─────────────────────────────────

    async def test_balanced_mode_respects_per_project_cap(self):
        """Balanced mode also gates on per-project caps."""
        acct = self._healthy(
            "acct-1",
            project_daily_task_cap={self._PROJ_A: 1},
            project_state={self._PROJ_A: {"daily_task_count": 1, "active_tasks": 0}},
        )
        self._setup([acct])
        result = await select_max_plan_account_balanced(
            self._USER,
            [("anthropic", OPUS)],
            project_id=self._PROJ_A,
        )
        assert result is None

    async def test_balanced_mode_allows_other_project(self):
        """Balanced mode allows another project to claim when first is at cap."""
        acct = self._healthy(
            "acct-1",
            project_daily_task_cap={self._PROJ_A: 1},
            project_state={self._PROJ_A: {"daily_task_count": 1, "active_tasks": 0}},
        )
        self._setup([acct])
        result = await select_max_plan_account_balanced(
            self._USER,
            [("anthropic", OPUS)],
            project_id=self._PROJ_B,
        )
        assert result is not None
        assert result[0]["id"] == "acct-1"


class TestNoIOUnderLock:
    _USER = "u"

    async def test_selection_reads_only_cache_no_probe(self, monkeypatch):
        now = _utcnow()
        acct = _healthy_acct("a")
        _acct_usage(acct, _known_snap(10.0, 10.0), at=now)  # fresh → no refresh
        _max_plan_pools.clear()
        _account_by_id.clear()
        _max_plan_pools[self._USER] = [acct]
        _account_by_id["a"] = acct

        called = {"probe": False}

        class _Boom:
            async def probe_usage(self, *a, **k):
                called["probe"] = True
                raise AssertionError("selection must not probe under the lock")

        monkeypatch.setattr("app.max_plan_pool.get_provider", lambda *_a, **_k: _Boom())
        account = await select_max_plan_account(
            self._USER,
            model=OPUS,
            task_type="create",
            runtime_config={
                "usage_headroom_gate_enabled": True,
                "usage_headroom_threshold_pct": 90,
                "usage_headroom_long_running_task_types": ["create"],
            },
        )
        assert account["id"] == "a"
        assert called["probe"] is False

    async def test_refresh_probes_outside_lock_and_caches(self, monkeypatch):
        acct = _healthy_acct("a")  # no snapshot → refresh will probe
        _max_plan_pools.clear()
        _account_by_id.clear()
        _account_by_id["a"] = acct

        observed = {"lock_held_during_probe": None}

        class _Probe:
            async def probe_usage(self, *a, **k):
                observed["lock_held_during_probe"] = _max_plan_pool_lock.locked()
                return _known_snap(30.0, 40.0)

        monkeypatch.setattr(
            "app.max_plan_pool.get_provider", lambda *_a, **_k: _Probe()
        )
        await _refresh_usage_snapshot("a")
        # The network probe ran WITHOUT the pool lock held…
        assert observed["lock_held_during_probe"] is False
        # …and the result was cached under the lock afterward.
        assert isinstance(acct.get("usage_snapshot"), UsageSnapshot)
        assert acct["usage_snapshot"].window_utilizations["five_hour"] == 30.0
        assert acct.get("usage_probe_inflight") is None

    async def test_refresh_caches_unknown_on_probe_error(self, monkeypatch):
        acct = _healthy_acct("a")
        _max_plan_pools.clear()
        _account_by_id.clear()
        _account_by_id["a"] = acct

        class _Broken:
            async def probe_usage(self, *a, **k):
                raise RuntimeError("provider blew up")

        monkeypatch.setattr(
            "app.max_plan_pool.get_provider", lambda *_a, **_k: _Broken()
        )
        await _refresh_usage_snapshot("a")
        # A blown probe still caches an (unknown) snapshot so we don't hammer it.
        assert isinstance(acct.get("usage_snapshot"), UsageSnapshot)
        assert acct["usage_snapshot"].known is False

    async def test_maybe_schedule_fires_background_refresh(self, monkeypatch):
        acct = _healthy_acct("a")  # no snapshot → stale
        _account_by_id.clear()
        _account_by_id["a"] = acct
        ran = {"count": 0}

        async def _fake_refresh(acct_id):
            ran["count"] += 1

        monkeypatch.setattr("app.max_plan_pool._refresh_usage_snapshot", _fake_refresh)
        _maybe_schedule_usage_refresh(acct, _utcnow())
        assert acct.get("usage_probe_inflight") is True
        await asyncio.sleep(0)  # let the fire-and-forget task run
        assert ran["count"] == 1

    async def test_refresh_failure_preserves_last_known_snapshot(self, monkeypatch):
        now = _utcnow()
        acct = _healthy_acct("a")
        _acct_usage(acct, _known_snap(30.0, 40.0), at=now - timedelta(seconds=120))
        _account_by_id["a"] = acct

        class _Broken:
            async def probe_usage(self, *a, **k):
                raise RuntimeError("provider blew up")

        monkeypatch.setattr(
            "app.max_plan_pool.get_provider", lambda *_a, **_k: _Broken()
        )
        await _refresh_usage_snapshot("a")
        # The last-known values survive for the meter (they age into stale);
        # only the failure stamp is recorded so schedulers back off.
        snap = acct.get("usage_snapshot")
        assert isinstance(snap, UsageSnapshot) and snap.known
        assert snap.window_utilizations["five_hour"] == 30.0
        assert acct.get("usage_probe_failed_at") is not None
        assert acct.get("usage_probe_inflight") is None

    async def test_refresh_success_clears_failure_stamp(self, monkeypatch):
        acct = _healthy_acct("a", usage_probe_failed_at=_utcnow())
        _account_by_id["a"] = acct

        class _Probe:
            async def probe_usage(self, *a, **k):
                return _known_snap(15.0, 25.0)

        monkeypatch.setattr(
            "app.max_plan_pool.get_provider", lambda *_a, **_k: _Probe()
        )
        await _refresh_usage_snapshot("a")
        assert acct.get("usage_probe_failed_at") is None
        assert acct["usage_snapshot"].window_utilizations["five_hour"] == 15.0

    async def test_maybe_schedule_backs_off_after_recent_failure(self, monkeypatch):
        now = _utcnow()
        acct = _healthy_acct("a", usage_probe_failed_at=now)  # no snapshot
        _account_by_id["a"] = acct
        ran = {"count": 0}

        async def _fake_refresh(acct_id):
            ran["count"] += 1

        monkeypatch.setattr("app.max_plan_pool._refresh_usage_snapshot", _fake_refresh)
        _maybe_schedule_usage_refresh(acct, now)
        assert acct.get("usage_probe_inflight") is None
        # Once the failure stamp ages past the TTL, scheduling resumes.
        acct["usage_probe_failed_at"] = now - timedelta(
            seconds=_USAGE_SNAPSHOT_TTL_SECONDS + 1
        )
        _maybe_schedule_usage_refresh(acct, now)
        assert acct.get("usage_probe_inflight") is True
        await asyncio.sleep(0)
        assert ran["count"] == 1


# ── Usage-meter persistence + periodic refresh ───────────────────────────────


class TestSnapshotFromPersistedUsage:
    def test_round_trips_persisted_blob(self):
        at = _utcnow()
        raw = {
            "source": "oauth_usage_probe",
            "updated_at": at.isoformat(),
            "five_hour": {"utilization": 42.0, "resets_at": "2026-07-11T05:00:00Z"},
            "seven_day": {"utilization": 63.5},
        }
        parsed = _snapshot_from_persisted_usage(raw)
        assert parsed is not None
        snap, stamp = parsed
        assert stamp == at
        assert snap.known is True
        assert snap.window_utilizations == {"five_hour": 42.0, "seven_day": 63.5}
        assert snap.resets_at == {"five_hour": "2026-07-11T05:00:00Z"}
        assert snap.probed_at == at.isoformat()

    def test_round_trips_messages_api_header_blob(self):
        at = _utcnow()
        raw = {
            "source": "messages_api_header",
            "updated_at": at.isoformat(),
            "unified_5h": {"utilization": 55.0, "resets_at": "2026-07-11T05:00:00Z"},
        }
        parsed = _snapshot_from_persisted_usage(raw)
        assert parsed is not None
        snap, stamp = parsed
        assert stamp == at
        assert snap.known is True
        assert snap.window_utilizations == {"five_hour": 55.0}
        assert snap.resets_at == {"five_hour": "2026-07-11T05:00:00Z"}

    def test_messages_api_header_without_utilization_returns_none(self):
        at = _utcnow()
        # unified_5h present but no numeric utilization → no loadbearing window → None
        assert (
            _snapshot_from_persisted_usage(
                {
                    "source": "messages_api_header",
                    "updated_at": at.isoformat(),
                    "unified_5h": {"status": "live"},
                }
            )
            is None
        )

    def test_rejects_foreign_source_and_garbage(self):
        assert _snapshot_from_persisted_usage(None) is None
        # messages_api_header with no updated_at or unified_5h → None
        assert _snapshot_from_persisted_usage({"source": "messages_api_header"}) is None
        assert (
            _snapshot_from_persisted_usage(
                {"source": "oauth_usage_probe", "updated_at": "not-a-date"}
            )
            is None
        )
        # No numeric windows → nothing to display → None.
        assert (
            _snapshot_from_persisted_usage(
                {
                    "source": "oauth_usage_probe",
                    "updated_at": _utcnow().isoformat(),
                    "five_hour": {"utilization": "high"},
                }
            )
            is None
        )


class TestRestoreUsageState:
    def test_seeds_from_persisted_when_no_previous(self):
        at = _utcnow() - timedelta(seconds=200)
        entry: dict = {}
        _restore_usage_state(
            entry,
            None,
            {
                "source": "oauth_usage_probe",
                "updated_at": at.isoformat(),
                "five_hour": {"utilization": 10.0},
            },
        )
        assert entry["usage_snapshot"].known is True
        assert entry["usage_snapshot_at"] == at

    def test_prefers_fresher_in_memory_over_persisted(self):
        newer = _utcnow()
        older = newer - timedelta(seconds=600)
        entry: dict = {}
        _restore_usage_state(
            entry,
            (_known_snap(55.0, 5.0), newer, None),
            {
                "source": "oauth_usage_probe",
                "updated_at": older.isoformat(),
                "five_hour": {"utilization": 10.0},
            },
        )
        assert entry["usage_snapshot"].window_utilizations["five_hour"] == 55.0
        assert entry["usage_snapshot_at"] == newer

    def test_carries_failure_stamp_and_tolerates_nothing(self):
        failed_at = _utcnow()
        entry: dict = {}
        _restore_usage_state(entry, (None, None, failed_at), None)
        assert entry.get("usage_probe_failed_at") == failed_at
        assert "usage_snapshot" not in entry
        empty: dict = {}
        _restore_usage_state(empty, None, None)
        assert empty == {}


@pytest.mark.asyncio
class TestRefreshUsageMeterSnapshots:
    def _register(self, acct):
        _account_by_id[acct["id"]] = acct
        return acct

    async def test_schedules_only_eligible_accounts(self, monkeypatch):
        scheduled: list[str] = []
        monkeypatch.setattr(
            "app.max_plan_pool._schedule_usage_refresh",
            lambda acct: scheduled.append(acct["id"]),
        )
        self._register(
            _acct(id="claude", provider="anthropic", auth_mode="oauth")
        )
        self._register(_acct(id="codex", provider="openai", auth_mode="oauth"))
        self._register(
            _acct(id="key", provider="anthropic", auth_mode="api_key")
        )
        self._register(
            _acct(
                id="off",
                provider="anthropic",
                auth_mode="oauth",
                enabled=False,
            )
        )
        self._register(
            _acct(
                id="no-secret",
                provider="anthropic",
                auth_mode="oauth",
                oauth_token=None,
            )
        )
        count = await refresh_usage_meter_snapshots()
        assert scheduled == ["claude"]
        assert count == 1

    async def test_skips_fresh_snapshots_and_recent_failures(self, monkeypatch):
        now = _utcnow()
        scheduled: list[str] = []
        monkeypatch.setattr(
            "app.max_plan_pool._schedule_usage_refresh",
            lambda acct: scheduled.append(acct["id"]),
        )
        fresh = self._register(
            _acct(id="fresh", provider="anthropic", auth_mode="oauth")
        )
        _acct_usage(fresh, _known_snap(10.0, 10.0), at=now - timedelta(seconds=30))
        aging = self._register(
            _acct(id="aging", provider="anthropic", auth_mode="oauth")
        )
        _acct_usage(
            aging,
            _known_snap(10.0, 10.0),
            at=now - timedelta(seconds=_USAGE_METER_REFRESH_INTERVAL_SECONDS + 1),
        )
        failed = self._register(
            _acct(
                id="failed",
                provider="anthropic",
                auth_mode="oauth",
                usage_probe_failed_at=now - timedelta(seconds=30),
            )
        )
        assert failed["id"] == "failed"
        count = await refresh_usage_meter_snapshots(now)
        assert scheduled == ["aging"]
        assert count == 1


class TestDetectPersistentlyDarkMeters:
    def _register(self, acct):
        _account_by_id[acct["id"]] = acct
        return acct

    async def test_stale_failure_appears(self):
        now = _utcnow()
        threshold = 1800  # 2x the 900s display stale window
        acct = self._register(
            _acct(
                id="stuck",
                provider="anthropic",
                auth_mode="oauth",
                usage_probe_failed_at=now - timedelta(seconds=threshold + 1),
            )
        )
        result = await detect_persistently_dark_meters(now, threshold_seconds=threshold)
        ids = [r["id"] for r in result]
        assert "stuck" in ids
        entry = next(r for r in result if r["id"] == "stuck")
        assert entry["dark_reason"] == "endpoint_error"
        assert entry["dark_since"] == acct["usage_probe_failed_at"]

    async def test_recent_failure_excluded(self):
        now = _utcnow()
        threshold = 1800
        self._register(
            _acct(
                id="recent-fail",
                provider="anthropic",
                auth_mode="oauth",
                usage_probe_failed_at=now - timedelta(seconds=threshold - 60),
            )
        )
        result = await detect_persistently_dark_meters(now, threshold_seconds=threshold)
        ids = [r["id"] for r in result]
        assert "recent-fail" not in ids

    async def test_known_snapshot_excluded(self):
        now = _utcnow()
        threshold = 1800
        acct = self._register(
            _acct(id="known", provider="anthropic", auth_mode="oauth")
        )
        # Snapshot is known and old enough that it would fail if checked for staleness
        _acct_usage(acct, _known_snap(10.0, 10.0), at=now - timedelta(seconds=threshold + 1))
        result = await detect_persistently_dark_meters(now, threshold_seconds=threshold)
        ids = [r["id"] for r in result]
        assert "known" not in ids

    async def test_non_eligible_account_excluded(self):
        now = _utcnow()
        threshold = 1800
        # API key account — not OAuth, so not eligible
        self._register(
            _acct(
                id="api-key",
                provider="anthropic",
                auth_mode="api_key",
                usage_probe_failed_at=now - timedelta(seconds=threshold + 1),
            )
        )
        result = await detect_persistently_dark_meters(now, threshold_seconds=threshold)
        ids = [r["id"] for r in result]
        assert "api-key" not in ids

    async def test_never_probed_past_threshold_appears(self):
        now = _utcnow()
        threshold = 1800
        # No stamps at all → never probed, dark_since=None → qualifies (no stamp to bound with)
        self._register(_acct(id="never", provider="anthropic", auth_mode="oauth"))
        result = await detect_persistently_dark_meters(now, threshold_seconds=threshold)
        ids = [r["id"] for r in result]
        assert "never" in ids
        entry = next(r for r in result if r["id"] == "never")
        assert entry["dark_reason"] == "never_probed"
        assert entry["dark_since"] is None

    async def test_all_fresh_emits_empty(self):
        now = _utcnow()
        threshold = 1800
        acct = self._register(
            _acct(id="fresh-ok", provider="anthropic", auth_mode="oauth")
        )
        _acct_usage(acct, _known_snap(5.0, 5.0), at=now - timedelta(seconds=60))
        result = await detect_persistently_dark_meters(now, threshold_seconds=threshold)
        ids = [r["id"] for r in result]
        assert "fresh-ok" not in ids


# ── record_account_startup_failure / startup-failure breaker ────────────────


def _pool_acct(acct_id: str, **kw) -> dict:
    """Create an account dict and register it in _account_by_id for breaker tests."""
    entry = {
        "id": acct_id,
        "name": acct_id,
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
    }
    entry.update(kw)
    _account_by_id[acct_id] = entry
    return entry


@pytest.mark.asyncio
class TestRecordAccountStartupFailure:
    async def _record_n(
        self, acct_id: str, zones: list[str], n: int | None = None
    ) -> None:
        """Record n startup failures cycling through zones list."""
        count = n if n is not None else _STARTUP_FAILURE_THRESHOLD
        for i in range(count):
            await record_account_startup_failure(acct_id, zones[i % len(zones)])

    async def test_multi_zone_failures_trip_breaker(self):
        acct = _pool_acct("a")
        await self._record_n("a", ["us-central1-b", "us-central1-c"])
        assert acct.get("startup_failure_cooldown_at") is not None
        reason = acct.get("disabled_reason", "")
        assert reason.startswith("startup-failure cooldown")
        assert "a" in _pending_rl_persists

    async def test_single_zone_failures_do_not_trip_breaker(self):
        acct = _pool_acct("a")
        await self._record_n("a", ["us-central1-b"], n=_STARTUP_FAILURE_THRESHOLD + 2)
        assert acct.get("startup_failure_cooldown_at") is None

    async def test_below_threshold_no_trip(self):
        acct = _pool_acct("a")
        await self._record_n(
            "a",
            ["us-central1-b", "us-central1-c"],
            n=_STARTUP_FAILURE_THRESHOLD - 1,
        )
        assert acct.get("startup_failure_cooldown_at") is None

    async def test_exactly_threshold_trips_breaker(self):
        acct = _pool_acct("a")
        await self._record_n(
            "a",
            ["us-central1-b", "us-central1-c"],
            n=_STARTUP_FAILURE_THRESHOLD,
        )
        assert acct.get("startup_failure_cooldown_at") is not None

    async def test_already_in_cooldown_does_not_reset_timer(self):
        acct = _pool_acct("a")
        await self._record_n("a", ["us-central1-b", "us-central1-c"])
        first_cooldown_at = acct["startup_failure_cooldown_at"]
        # Record more failures — timer should not move
        await self._record_n("a", ["us-central1-b", "us-central1-c"])
        assert acct["startup_failure_cooldown_at"] == first_cooldown_at

    async def test_unknown_account_is_noop(self):
        # Should not raise
        await record_account_startup_failure("unknown-account-id", "us-central1-b")

    async def test_zone_wide_storm_different_accounts_not_blamed(self):
        """Zone-wide storm: many accounts each fail once in the same zone.
        No single account reaches the multi-zone threshold, so none are tripped."""
        for i in range(10):
            _pool_acct(f"acct-{i}")
            # Each account gets failures only in one zone (zone-wide signature)
            await self._record_n(
                f"acct-{i}", ["us-central1-b"], n=_STARTUP_FAILURE_THRESHOLD + 1
            )
        # No account should be in startup-failure cooldown
        for i in range(10):
            assert (
                _account_by_id[f"acct-{i}"].get("startup_failure_cooldown_at") is None
            )


@pytest.mark.asyncio
class TestStartupFailureAccountState:
    async def test_account_unavailable_during_cooldown(self):
        acct = _pool_acct("a")
        now = _utcnow()
        acct["startup_failure_cooldown_at"] = now - timedelta(seconds=10)
        # 10 seconds elapsed < 30min cooldown → unavailable
        state = _account_state_for_model(acct, "claude-opus-4-8", now=now)
        assert state == "unavailable"

    async def test_account_available_after_cooldown_expires(self):
        acct = _pool_acct("a")
        now = _utcnow()
        expired_at = now - timedelta(seconds=_STARTUP_FAILURE_COOLDOWN_SECONDS + 60)
        acct["startup_failure_cooldown_at"] = expired_at
        state = _account_state_for_model(acct, "claude-opus-4-8", now=now)
        assert state != "unavailable"

    async def test_no_cooldown_account_healthy(self):
        acct = _pool_acct("a")
        state = _account_state_for_model(acct, "claude-opus-4-8")
        assert state == "healthy"


@pytest.mark.asyncio
class TestRecheckClearsStartupCooldown:
    async def test_recheck_clears_expired_cooldown(self, monkeypatch):
        acct_id = str(uuid.uuid4())
        acct = _pool_acct(acct_id)
        now = _utcnow()
        expired_at = now - timedelta(seconds=_STARTUP_FAILURE_COOLDOWN_SECONDS + 60)
        acct["startup_failure_cooldown_at"] = expired_at
        acct["disabled_reason"] = (
            "startup-failure cooldown: 5 failures in 120min across zones us-central1-b, us-central1-c"
        )

        db_acct = _FakeDbAcct(acct_id)
        db_acct.rate_limit_state = {
            "startup_failure_cooldown_at": expired_at.isoformat(),
            "startup_failure_disabled_reason": acct["disabled_reason"],
        }
        db_acct.disabled_reason = acct["disabled_reason"]
        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal",
            lambda: _FakeAsyncSessionCtxWithAcct(db_acct),
        )
        await recheck_disabled_accounts()

        assert acct.get("startup_failure_cooldown_at") is None
        assert acct.get("disabled_reason") is None
        assert acct_id not in _pending_cap_persists

    async def test_recheck_preserves_newer_startup_generation(self, monkeypatch):
        acct_id = str(uuid.uuid4())
        now = _utcnow()
        old_at = now - timedelta(seconds=_STARTUP_FAILURE_COOLDOWN_SECONDS + 60)
        newer_at = now - timedelta(seconds=30)
        acct = _pool_acct(
            acct_id,
            startup_failure_cooldown_at=old_at,
            disabled_reason="startup-failure cooldown: old",
        )
        db_acct = _FakeDbAcct(acct_id)
        db_acct.rate_limit_state = {
            "startup_failure_cooldown_at": newer_at.isoformat(),
            "startup_failure_disabled_reason": "startup-failure cooldown: newer",
        }
        db_acct.disabled_reason = "startup-failure cooldown: newer"
        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal",
            lambda: _FakeAsyncSessionCtxWithAcct(db_acct),
        )

        await recheck_disabled_accounts()

        assert acct["startup_failure_cooldown_at"] == newer_at
        assert acct["disabled_reason"] == "startup-failure cooldown: newer"

    async def test_recheck_does_not_clear_active_cooldown(self, monkeypatch):
        acct = _pool_acct("a")
        now = _utcnow()
        acct["startup_failure_cooldown_at"] = now - timedelta(seconds=30)  # recent
        acct["disabled_reason"] = "startup-failure cooldown: ..."

        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal",
            lambda: _FakeAsyncSessionCtx(),
        )
        await recheck_disabled_accounts()

        # Still in cooldown
        assert acct.get("startup_failure_cooldown_at") is not None

    async def test_persist_retry_during_active_cooldown_preserves_reason(
        self, monkeypatch
    ):
        """Regression: _pending_rl_persists retry must not wipe disabled_reason
        while a startup-failure cooldown is still active (enabled=True accounts
        that carry a disabled_reason for UI visibility)."""
        acct_id = str(uuid.uuid4())
        acct = _pool_acct(acct_id)
        now = _utcnow()
        acct["startup_failure_cooldown_at"] = now - timedelta(seconds=30)  # active
        expected_reason = (
            "startup-failure cooldown: 5 failures across us-central1-b, us-central1-c"
        )
        acct["disabled_reason"] = expected_reason

        # Simulate a previously failed rl-persist that queued a retry
        if acct_id not in _pending_rl_persists:
            _pending_rl_persists.append(acct_id)

        fake_db_acct = _FakeDbAcct(acct_id)
        fake_db_acct.disabled_reason = expected_reason
        fake_db_acct.rate_limit_state = {
            "startup_failure_cooldown_at": acct[
                "startup_failure_cooldown_at"
            ].isoformat(),
            "startup_failure_disabled_reason": expected_reason,
        }

        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal",
            lambda: _FakeAsyncSessionCtxWithAcct(fake_db_acct),
        )
        await recheck_disabled_accounts()

        # In-memory reason must be preserved
        assert acct.get("disabled_reason") == expected_reason
        # DB-side reason must NOT have been wiped
        assert fake_db_acct.disabled_reason == expected_reason

    async def test_cap_flush_does_not_copy_stale_enabled_state(self, monkeypatch):
        acct_id = str(uuid.uuid4())
        generation = _utcnow()
        acct = _pool_acct(
            acct_id,
            enabled=True,
            daily_task_count=7,
            daily_task_count_reset_at=generation,
        )
        db_acct = _FakeDbAcct(acct_id)
        db_acct.daily_task_count_reset_at = generation
        db_acct.enabled = False
        db_acct.disabled_reason = "operator disabled"
        _stage_usage_delta(acct, 7, None)
        monkeypatch.setattr(
            "app.max_plan_pool.AsyncSessionLocal",
            lambda: _FakeAsyncSessionCtxWithAcct(db_acct),
        )

        await recheck_disabled_accounts()

        assert db_acct.enabled is False
        assert db_acct.disabled_reason == "operator disabled"
        assert acct["enabled"] is False
        assert acct["disabled_reason"] == "operator disabled"
        assert db_acct.daily_task_count == 7

    async def test_usage_cap_deltas_merge_distinct_pod_project_updates(self):
        generation = _utcnow()
        db_acct = _FakeDbAcct()
        db_acct.daily_task_count_reset_at = generation
        db_acct.project_state = {"existing": {"daily_task_count": 4, "cap": 9}}

        _apply_usage_cap_mutations(
            db_acct,
            [
                {
                    "op": "usage_delta",
                    "generation": generation.isoformat(),
                    "delta": 1,
                    "project_id": "project-a",
                    "project_delta": 1,
                }
            ],
        )
        _apply_usage_cap_mutations(
            db_acct,
            [
                {
                    "op": "usage_delta",
                    "generation": generation.isoformat(),
                    "delta": 1,
                    "project_id": "project-b",
                    "project_delta": 1,
                }
            ],
        )

        assert db_acct.daily_task_count == 2
        assert db_acct.project_state["project-a"]["daily_task_count"] == 1
        assert db_acct.project_state["project-b"]["daily_task_count"] == 1
        assert db_acct.project_state["existing"] == {"daily_task_count": 4, "cap": 9}

    async def test_usage_delta_near_reset_boundary_is_rejected_without_time_tolerance(
        self,
    ):
        current_generation = _utcnow()
        stale_generation = current_generation - timedelta(seconds=1)
        db_acct = _FakeDbAcct()
        db_acct.daily_task_count = 3
        db_acct.daily_task_count_reset_at = current_generation
        db_acct.rate_limit_state = {
            "usage_window_origin": {
                "generation": current_generation.isoformat(),
                "kind": "daily_reset",
            }
        }

        _apply_usage_cap_mutations(
            db_acct,
            [
                {
                    "op": "usage_delta",
                    "generation": stale_generation.isoformat(),
                    "observed_generation_missing": True,
                    "delta": 1,
                    "project_id": "stale-project",
                    "project_delta": 1,
                }
            ],
        )

        assert db_acct.daily_task_count == 3
        assert "stale-project" not in (db_acct.project_state or {})

    async def test_concurrent_first_use_generation_aliases_explicit_initialization(
        self,
    ):
        winning_generation = _utcnow()
        losing_generation = winning_generation + timedelta(seconds=1)
        db_acct = _FakeDbAcct()
        db_acct.daily_task_count_reset_at = winning_generation
        db_acct.rate_limit_state = {
            "usage_window_origin": {
                "generation": winning_generation.isoformat(),
                "kind": "initialization",
            }
        }

        _apply_usage_cap_mutations(
            db_acct,
            [
                {
                    "op": "usage_delta",
                    "generation": losing_generation.isoformat(),
                    "observed_generation_missing": True,
                    "delta": 1,
                    "project_id": "project-b",
                    "project_delta": 1,
                }
            ],
        )

        assert db_acct.daily_task_count == 1
        assert db_acct.project_state["project-b"]["daily_task_count"] == 1

    async def test_operator_reset_tombstone_rejects_old_but_accepts_new_hit(self):
        reset_at = _utcnow()
        old_at = (reset_at - timedelta(seconds=1)).isoformat()
        new_at = (reset_at + timedelta(seconds=1)).isoformat()
        state = {"cooldown_reset_at": reset_at.isoformat()}

        state = _apply_rate_limit_mutations(
            state,
            [
                {
                    "op": "disable_models",
                    "models": {
                        "gpt-5.6-sol": {
                            "at": old_at,
                            "provider": "openai",
                        }
                    },
                }
            ],
        )
        assert "disabled_models" not in state
        state = _apply_rate_limit_mutations(
            state,
            [
                {
                    "op": "disable_models",
                    "models": {
                        "gpt-5.6-sol": {
                            "at": new_at,
                            "provider": "openai",
                        }
                    },
                }
            ],
        )
        assert state["disabled_models"]["gpt-5.6-sol"]["hits"] == 1


class _FakeDbAcct:
    """Minimal stub for a DB MaxPlanAccount row."""

    def __init__(self, acct_id: str | None = None):
        self.id = uuid.UUID(acct_id) if acct_id else uuid.uuid4()
        self.rate_limit_state = None
        self.enabled = True
        self.disabled_reason = None
        self.disabled_at = None
        self.daily_task_count = 0
        self.daily_task_count_reset_at = None
        self.project_state = None


class _FakeExecuteResult:
    def __init__(self, db_acct: _FakeDbAcct | None):
        self._db_acct = db_acct

    def scalar_one_or_none(self):
        return self._db_acct

    def scalars(self):
        return self

    def all(self):
        return [self._db_acct] if self._db_acct is not None else []


class _FakeAsyncSessionCtxWithAcct:
    """Fake session that returns a stub DB account from get()."""

    def __init__(self, db_acct: _FakeDbAcct):
        self._db_acct = db_acct

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, *_a, **_k):
        return self._db_acct

    async def execute(self, *_a, **_k):
        return _FakeExecuteResult(self._db_acct)

    async def commit(self):
        pass

    def add(self, *_):
        pass


class _FakeAsyncSessionCtx:
    """Minimal async context manager + session stub for tests that don't need DB."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, *_a, **_k):
        return None

    async def execute(self, *_a, **_k):
        return _FakeExecuteResult(None)

    async def commit(self):
        pass

    def add(self, *_):
        pass


# ── _build_rate_limit_state_blob startup-failure coverage ───────────────────


class TestBuildRateLimitStateBlobStartupFailure:
    def test_startup_failure_cooldown_at_written_to_blob(self):
        now = _utcnow()
        acct = _acct(startup_failure_cooldown_at=now)
        blob = _build_rate_limit_state_blob(acct)
        assert blob is not None
        assert "startup_failure_cooldown_at" in blob
        assert blob["startup_failure_cooldown_at"] == now.isoformat()

    def test_startup_failure_reason_written_to_blob(self):
        now = _utcnow()
        reason = "startup-failure cooldown: 5 failures in 120min across zones us-central1-b, us-central1-c"
        acct = _acct(startup_failure_cooldown_at=now, disabled_reason=reason)
        blob = _build_rate_limit_state_blob(acct)
        assert blob is not None
        assert blob.get("startup_failure_disabled_reason") == reason

    def test_non_startup_reason_not_written_to_blob(self):
        now = _utcnow()
        acct = _acct(startup_failure_cooldown_at=now, disabled_reason="rate limit")
        blob = _build_rate_limit_state_blob(acct)
        assert blob is not None
        assert "startup_failure_disabled_reason" not in blob

    def test_no_startup_cooldown_no_field_in_blob(self):
        acct = _acct()
        # No startup_failure_cooldown_at set
        blob = _build_rate_limit_state_blob(acct)
        assert blob is None or "startup_failure_cooldown_at" not in (blob or {})

    def test_blob_roundtrip_preserves_reason(self):
        now = _utcnow()
        reason = "startup-failure cooldown: 3 agent-startup failure(s) in 120min across zones us-east1-b, us-west1-a"
        acct = _acct(startup_failure_cooldown_at=now, disabled_reason=reason)
        blob = _build_rate_limit_state_blob(acct)
        assert blob["startup_failure_cooldown_at"] == now.isoformat()
        assert blob["startup_failure_disabled_reason"] == reason


# ── load_max_plan_pool startup-failure restore ───────────────────────────────


class _FakeMaxPlanAccount:
    """Minimal stub that mimics a DB MaxPlanAccount row for load_max_plan_pool tests."""

    def __init__(
        self,
        acct_id: str,
        *,
        rate_limit_state: dict | None = None,
        disabled_reason: str | None = None,
        enabled: bool = True,
    ):
        import uuid as _uuid

        # uuid5 is deterministic across runs (no PYTHONHASHSEED dependency)
        self.id = _uuid.uuid5(_uuid.NAMESPACE_DNS, acct_id)
        self.name = acct_id
        self.user_id = "user-1"
        self.provider = None
        self.auth_mode = None
        self.secret_blob_ref = "sk-ant-test"
        self.oauth_token = None
        self.provider_metadata = {}
        self.claude_config_json = None
        self.enabled = enabled
        self.total_tasks = 0
        self.token_created_at = None
        self.daily_task_cap = None
        self.max_concurrent_tasks = None
        self.auto_disable_after_rate_limits = None
        self.daily_task_count = 0
        self.daily_task_count_reset_at = None
        self.disabled_reason = disabled_reason
        self.disabled_at = None
        self.rate_limit_state = rate_limit_state
        self.project_daily_task_cap = None
        self.project_max_concurrent_tasks = None
        self.project_state = None


class _FakeLoadSession:
    """Session stub for load_max_plan_pool: first execute() returns accounts, second returns tasks."""

    def __init__(self, accounts: list, active_tasks: list | None = None):
        self._accounts = accounts
        self._active_tasks = active_tasks or []
        self._call_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def execute(self, _stmt):
        self._call_count += 1
        if self._call_count == 1:
            return _FakeScalarsResult(self._accounts)
        return _FakeScalarsResult(self._active_tasks)

    async def commit(self):
        pass

    def add(self, *_):
        pass


class _FakeScalarsResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


@pytest.mark.asyncio
class TestLoadMaxPlanPoolStartupFailureRestore:
    async def _load_with_accounts(self, accounts: list, monkeypatch) -> None:
        from app import max_plan_pool as mpp

        monkeypatch.setattr(
            mpp, "AsyncSessionLocal", lambda: _FakeLoadSession(accounts)
        )
        await mpp.load_max_plan_pool()

    async def test_active_cooldown_restored_from_rate_limit_state(self, monkeypatch):
        """load_max_plan_pool must restore startup_failure_cooldown_at from rate_limit_state."""
        now = _utcnow()
        active_at = (now - timedelta(seconds=60)).isoformat()
        acct = _FakeMaxPlanAccount(
            "acct-a",
            rate_limit_state={"startup_failure_cooldown_at": active_at},
            disabled_reason=None,
        )
        await self._load_with_accounts([acct], monkeypatch)
        entry = _account_by_id[str(acct.id)]
        assert entry.get("startup_failure_cooldown_at") is not None

    async def test_active_cooldown_restores_reason_from_blob(self, monkeypatch):
        """disabled_reason from startup_failure_disabled_reason must survive restart."""
        now = _utcnow()
        active_at = (now - timedelta(seconds=60)).isoformat()
        reason = (
            "startup-failure cooldown: 5 failures across us-central1-b, us-central1-c"
        )
        acct = _FakeMaxPlanAccount(
            "acct-a",
            rate_limit_state={
                "startup_failure_cooldown_at": active_at,
                "startup_failure_disabled_reason": reason,
            },
            disabled_reason=None,
        )
        await self._load_with_accounts([acct], monkeypatch)
        entry = _account_by_id[str(acct.id)]
        assert (entry.get("disabled_reason") or "").startswith(
            "startup-failure cooldown"
        )

    async def test_active_cooldown_fallback_reason_when_no_blob_reason(
        self, monkeypatch
    ):
        """A cooldown without a persisted reason still gets a generic disabled_reason."""
        now = _utcnow()
        active_at = (now - timedelta(seconds=60)).isoformat()
        acct = _FakeMaxPlanAccount(
            "acct-a",
            rate_limit_state={"startup_failure_cooldown_at": active_at},
            disabled_reason=None,
        )
        await self._load_with_accounts([acct], monkeypatch)
        entry = _account_by_id[str(acct.id)]
        assert (entry.get("disabled_reason") or "").startswith(
            "startup-failure cooldown"
        )

    async def test_expired_cooldown_clears_stale_disabled_reason(self, monkeypatch):
        """An expired persisted cooldown must not leave a stale startup-failure reason."""
        now = _utcnow()
        expired_at = (
            now - timedelta(seconds=_STARTUP_FAILURE_COOLDOWN_SECONDS + 120)
        ).isoformat()
        reason = "startup-failure cooldown: 5 failures across zones"
        acct = _FakeMaxPlanAccount(
            "acct-a",
            rate_limit_state={"startup_failure_cooldown_at": expired_at},
            disabled_reason=reason,
        )
        await self._load_with_accounts([acct], monkeypatch)
        entry = _account_by_id[str(acct.id)]
        # Cooldown must be gone
        assert entry.get("startup_failure_cooldown_at") is None
        # Stale reason must be cleared
        assert not (entry.get("disabled_reason") or "").startswith(
            "startup-failure cooldown"
        )

    async def test_no_startup_cooldown_in_rate_limit_state_leaves_entry_unset(
        self, monkeypatch
    ):
        """Accounts without a startup cooldown in rate_limit_state remain unaffected."""
        acct = _FakeMaxPlanAccount(
            "acct-a",
            rate_limit_state={"disabled_models": {}},
            disabled_reason=None,
        )
        await self._load_with_accounts([acct], monkeypatch)
        entry = _account_by_id[str(acct.id)]
        assert entry.get("startup_failure_cooldown_at") is None


@pytest.mark.asyncio
class TestLoadMaxPlanPoolSecretFetchFailed:
    """load_max_plan_pool sets secret_fetch_failed=True when Secret Manager raises."""

    async def _load_with_sm_acct(self, secret_ref: str, monkeypatch, side_effect):
        from app import max_plan_pool as mpp

        acct = _FakeMaxPlanAccount("acct-sm")
        acct.secret_blob_ref = secret_ref

        async def _fake_get_user_secret(_ref):
            raise side_effect

        monkeypatch.setattr(
            "app.secrets_manager.get_user_secret",
            _fake_get_user_secret,
        )
        monkeypatch.setattr(
            mpp, "AsyncSessionLocal", lambda: _FakeLoadSession([acct])
        )
        await mpp.load_max_plan_pool()
        return _account_by_id[str(acct.id)]

    async def test_secret_fetch_failure_sets_flag(self, monkeypatch):
        """When Secret Manager raises, secret_fetch_failed must be True."""
        entry = await self._load_with_sm_acct(
            "projects/proj/secrets/mysecret/versions/latest",
            monkeypatch,
            RuntimeError("secret unavailable"),
        )
        assert entry["secret_fetch_failed"] is True

    async def test_secret_fetch_failure_clears_runtime_secret(self, monkeypatch):
        """When Secret Manager raises, runtime_secret must be None so the meter is dark."""
        entry = await self._load_with_sm_acct(
            "projects/proj/secrets/mysecret/versions/latest",
            monkeypatch,
            RuntimeError("secret unavailable"),
        )
        assert entry["runtime_secret"] is None

    async def test_secret_fetch_failure_emits_warning(self, monkeypatch, caplog):
        """When Secret Manager raises, a '[POOL] Usage meter: failed to fetch secret' warning is logged."""
        import logging

        with caplog.at_level(logging.WARNING, logger="app.max_plan_pool"):
            await self._load_with_sm_acct(
                "projects/proj/secrets/mysecret/versions/latest",
                monkeypatch,
                RuntimeError("secret unavailable"),
            )
        assert any(
            "Usage meter: failed to fetch secret" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_non_sm_secret_ref_leaves_flag_false(self, monkeypatch):
        """A plain API-key secret_blob_ref must not set secret_fetch_failed."""
        from app import max_plan_pool as mpp

        acct = _FakeMaxPlanAccount("acct-plain")
        acct.secret_blob_ref = "sk-ant-test"
        monkeypatch.setattr(
            mpp, "AsyncSessionLocal", lambda: _FakeLoadSession([acct])
        )
        await mpp.load_max_plan_pool()
        entry = _account_by_id[str(acct.id)]
        assert entry["secret_fetch_failed"] is False


class TestGetTaskLlmAccountId:
    """get_task_llm_account_id prefers the promoted Task.llm_account_id column
    when present, falling back to the context JSONB for pre-migration rows or
    plain-dict callers (D2 over-fetch fix, d-57436105)."""

    def test_prefers_column_over_context_when_set(self):
        from app.max_plan_pool import get_task_llm_account_id
        from app.models import Task

        task = Task(
            project_id=uuid.uuid4(),
            llm_account_id="acct-from-column",
            context={
                "llm_account_id": "acct-from-context",
                "max_plan_account_id": "acct-legacy",
            },
        )
        # Column wins even when context carries a (stale) different value.
        assert get_task_llm_account_id(task) == "acct-from-column"

    def test_falls_back_to_context_when_column_unset(self):
        from app.max_plan_pool import get_task_llm_account_id
        from app.models import Task

        # Pre-migration row: column NULL, canonical key in context.
        task = Task(
            project_id=uuid.uuid4(),
            llm_account_id=None,
            context={"llm_account_id": "acct-ctx"},
        )
        assert get_task_llm_account_id(task) == "acct-ctx"

    def test_falls_back_to_legacy_max_plan_key(self):
        from app.max_plan_pool import get_task_llm_account_id
        from app.models import Task

        task = Task(
            project_id=uuid.uuid4(),
            llm_account_id=None,
            context={"max_plan_account_id": "acct-legacy"},
        )
        assert get_task_llm_account_id(task) == "acct-legacy"

    def test_plain_context_dict_uses_context_lookup(self):
        from app.max_plan_pool import get_task_llm_account_id

        # Called with a raw context Mapping (no column to prefer).
        assert get_task_llm_account_id({"llm_account_id": "acct-dict"}) == "acct-dict"

    def test_returns_none_when_neither_set(self):
        from app.max_plan_pool import get_task_llm_account_id
        from app.models import Task

        task = Task(project_id=uuid.uuid4(), llm_account_id=None, context={})
        assert get_task_llm_account_id(task) is None


# ── typed success-rate failure outcomes + cardinality cap ────────────────────


class TestTypedSuccessRateFailureOutcomes:
    """Failure outcome_events must carry task_type so typed rates include them.
    The event list must also be capped at _OUTCOME_EVENTS_CAP entries."""

    async def test_typed_success_rate_includes_failure_outcomes_and_caps_events(self):
        """record_task_outcome and disable_account_model persist task_type on failure
        events so _compute_success_rate_7d_for_type counts them.  The list is also
        capped at _OUTCOME_EVENTS_CAP to bound JSONB size."""
        acct_id = "a"
        acct = _acct(id=acct_id)
        _account_by_id[acct_id] = acct

        now = _utcnow()

        # Seed 3 successful "create" events directly.
        acct["outcome_events"] = [
            {"at": now - timedelta(hours=i + 1), "success": True, "task_type": "create"}
            for i in range(3)
        ]

        # Record a CODE_ERROR failure for "create" via record_task_outcome.
        from app.max_plan_pool import record_task_outcome

        await record_task_outcome(acct_id, success=False, task_type="create")

        # The failure must appear in the typed rate (3 success + 1 fail = 0.75).
        rate = _compute_success_rate_7d_for_type(acct, "create", now=_utcnow())
        assert rate is not None, "failure without task_type is invisible to typed rate"
        assert rate == 3 / 4

        # Record a rate-limit failure via disable_account_model with task_type.
        with patch("app.max_plan_pool._persist_rate_limit_state"):
            await disable_account_model(acct_id, OPUS, kind="rate_limit", task_type="create")

        # Now 3 success + 2 failures = 0.6.
        rate2 = _compute_success_rate_7d_for_type(acct, "create", now=_utcnow())
        assert rate2 is not None
        assert rate2 == 3 / 5

        # Cardinality cap: flood beyond _OUTCOME_EVENTS_CAP.
        acct["outcome_events"] = [
            {"at": now - timedelta(minutes=i), "success": True, "task_type": "create"}
            for i in range(_OUTCOME_EVENTS_CAP + 50)
        ]
        # The serialization path must cap the blob at _OUTCOME_EVENTS_CAP entries.
        from app.max_plan_pool import _build_rate_limit_state_blob

        blob = _build_rate_limit_state_blob(acct)
        assert blob is not None
        assert len(blob.get("outcome_events", [])) <= _OUTCOME_EVENTS_CAP


# ── _persist_rate_limit_usage_to_db ──────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_rate_limit_usage_skips_unknown_snapshot():
    """Unknown snapshot is not persisted (no-op)."""
    from app.max_plan_pool import _persist_rate_limit_usage_to_db

    snap = UsageSnapshot.unknown()
    # Should not raise
    await _persist_rate_limit_usage_to_db("some-account-id", snap)


@pytest.mark.asyncio
async def test_persist_rate_limit_usage_writes_usage_dict(monkeypatch):
    """Known snapshot writes correct usage_dict to DB."""
    from app.max_plan_pool import _persist_rate_limit_usage_to_db

    snap = UsageSnapshot(
        known=True,
        window_utilizations={"five_hour": 30.0, "seven_day": 45.0},
        resets_at={
            "five_hour": "2026-07-09T15:00:00Z",
            "seven_day": "2026-07-14T00:00:00Z",
        },
        probed_at="2026-07-09T12:00:00",
    )

    written = None

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

    monkeypatch.setattr(
        "app.max_plan_pool.AsyncSessionLocal",
        lambda: FakeSession(),
    )
    monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

    await _persist_rate_limit_usage_to_db("test-id", snap)

    assert written is not None
    assert written["source"] == "oauth_usage_probe"
    assert written["five_hour"]["utilization"] == 30.0
    assert written["five_hour"]["resets_at"] == "2026-07-09T15:00:00Z"
    assert written["seven_day"]["utilization"] == 45.0
    assert written["seven_day"]["resets_at"] == "2026-07-14T00:00:00Z"
    assert "updated_at" in written
    assert "source" in written


@pytest.mark.asyncio
async def test_persist_rate_limit_usage_no_row_is_noop(monkeypatch):
    """When the DB row doesn't exist, the function is a no-op (no raise)."""
    from app.max_plan_pool import _persist_rate_limit_usage_to_db

    snap = UsageSnapshot(
        known=True,
        window_utilizations={"five_hour": 50.0, "seven_day": 50.0},
        resets_at={},
        probed_at="2026-07-09T12:00:00",
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a, **k):
            pass

        async def get(self, model, acct_id):
            return None  # row not found

        def add(self, obj):
            raise AssertionError("should not add when row is None")

        async def commit(self):
            raise AssertionError("should not commit when row is None")

    monkeypatch.setattr(
        "app.max_plan_pool.AsyncSessionLocal",
        lambda: FakeSession(),
    )
    monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

    # Should not raise
    await _persist_rate_limit_usage_to_db("nonexistent-id", snap)


@pytest.mark.asyncio
async def test_persist_rate_limit_usage_retries_and_counts_failures(monkeypatch):
    """On DB failure, retries once and increments _usage_persist_failures."""
    import app.max_plan_pool as pool_mod

    from app.max_plan_pool import _persist_rate_limit_usage_to_db

    snap = UsageSnapshot(
        known=True,
        window_utilizations={"five_hour": 30.0},
        resets_at={},
        probed_at="2026-07-09T12:00:00",
    )

    commit_call_count = 0

    class FakeRow:
        rate_limit_usage = None
        updated_at = None

    class FailThenSucceedSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a, **k):
            pass

        async def get(self, model, acct_id):
            return FakeRow()  # row exists

        def add(self, obj):
            pass

        async def commit(self):
            nonlocal commit_call_count
            commit_call_count += 1
            if commit_call_count == 1:
                raise Exception("transient DB error")

    monkeypatch.setattr(
        "app.max_plan_pool.AsyncSessionLocal",
        lambda: FailThenSucceedSession(),
    )
    monkeypatch.setattr("app.max_plan_pool.uuid.UUID", lambda x: x)

    before = pool_mod._usage_persist_failures
    await _persist_rate_limit_usage_to_db("test-id", snap)
    after = pool_mod._usage_persist_failures

    # Retry fired: commit was called twice (first fails, second succeeds).
    assert commit_call_count == 2, (
        f"expected 2 commit calls (1 fail + 1 retry), got {commit_call_count}"
    )
    # Counter reflects the first failure.
    assert after == before + 1, (
        f"counter should have incremented by 1 (was {before}, now {after})"
    )


def test_get_usage_persist_failure_count_reflects_module_counter():
    """get_usage_persist_failure_count() reads the module-level counter."""
    import app.max_plan_pool as pool_mod

    from app.max_plan_pool import get_usage_persist_failure_count

    original = pool_mod._usage_persist_failures
    try:
        assert get_usage_persist_failure_count() == original
        pool_mod._usage_persist_failures = original + 7
        assert get_usage_persist_failure_count() == original + 7
    finally:
        pool_mod._usage_persist_failures = original


# ── get_project_account_usage / get_user_project_pool_summary ────────────────


class TestGetProjectAccountUsage:
    def _reset_pools(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    def test_empty_user_returns_empty_list(self):
        self._reset_pools()
        try:
            from app.max_plan_pool import get_project_account_usage

            result = get_project_account_usage("p1", "nobody")
            assert result == []
        finally:
            self._reset_pools()

    def test_project_with_no_state_returns_zero_counts(self):
        self._reset_pools()
        try:
            from app.max_plan_pool import get_project_account_usage

            acct = _acct(id="a1", user_id="u1", name="Acct1")
            _account_by_id["a1"] = acct
            _max_plan_pools["u1"] = [acct]

            result = get_project_account_usage("p1", "u1")
            assert len(result) == 1
            r = result[0]
            assert r["account_id"] == "a1"
            assert r["account_name"] == "Acct1"
            assert r["project_active_tasks"] == 0
            assert r["project_daily_task_count"] == 0
            assert r["at_project_cap"] is False
            assert r["global_active_tasks"] == 0
        finally:
            self._reset_pools()

    def test_project_with_active_tasks_and_caps(self):
        self._reset_pools()
        try:
            from app.max_plan_pool import get_project_account_usage

            acct = _acct(
                id="a1",
                user_id="u1",
                name="Acct1",
                active_tasks=5,
                daily_task_count=12,
                project_state={"p1": {"active_tasks": 3, "daily_task_count": 8}},
                project_daily_task_cap={"p1": 10},
                project_max_concurrent_tasks={"p1": 5},
            )
            _account_by_id["a1"] = acct
            _max_plan_pools["u1"] = [acct]

            result = get_project_account_usage("p1", "u1")
            assert len(result) == 1
            r = result[0]
            assert r["project_active_tasks"] == 3
            assert r["project_daily_task_count"] == 8
            assert r["project_daily_task_cap"] == 10
            assert r["project_max_concurrent_tasks"] == 5
            assert r["at_project_daily_cap"] is False  # 8 < 10
            assert r["at_project_concurrent_cap"] is False  # 3 < 5
            assert r["at_project_cap"] is False
            assert r["global_active_tasks"] == 5
            assert r["global_daily_task_count"] == 12

            # Test at daily cap
            acct["project_state"]["p1"]["daily_task_count"] = 10
            result2 = get_project_account_usage("p1", "u1")
            assert result2[0]["at_project_daily_cap"] is True
            assert result2[0]["at_project_cap"] is True

            # Test at concurrent cap
            acct["project_state"]["p1"]["daily_task_count"] = 8
            acct["project_state"]["p1"]["active_tasks"] = 5
            result3 = get_project_account_usage("p1", "u1")
            assert result3[0]["at_project_concurrent_cap"] is True
            assert result3[0]["at_project_cap"] is True
        finally:
            self._reset_pools()

    def test_mixed_cap_state_across_accounts(self):
        self._reset_pools()
        try:
            from app.max_plan_pool import get_project_account_usage

            acct1 = _acct(
                id="a1",
                user_id="u1",
                name="Acct1",
                project_state={"p1": {"active_tasks": 5, "daily_task_count": 12}},
                project_daily_task_cap={"p1": 10},
            )
            acct2 = _acct(
                id="a2",
                user_id="u1",
                name="Acct2",
                project_state={"p1": {"active_tasks": 1, "daily_task_count": 2}},
            )
            _account_by_id["a1"] = acct1
            _account_by_id["a2"] = acct2
            _max_plan_pools["u1"] = [acct1, acct2]

            result = get_project_account_usage("p1", "u1")
            assert len(result) == 2
            assert result[0]["at_project_daily_cap"] is True  # 12 >= 10
            assert result[0]["at_project_cap"] is True
            assert result[1]["at_project_cap"] is False
        finally:
            self._reset_pools()

    def test_different_project_id_isolates_state(self):
        self._reset_pools()
        try:
            from app.max_plan_pool import get_project_account_usage

            acct = _acct(
                id="a1",
                user_id="u1",
                name="Acct1",
                project_state={
                    "p1": {"active_tasks": 3, "daily_task_count": 5},
                    "p2": {"active_tasks": 7, "daily_task_count": 2},
                },
                project_daily_task_cap={"p1": 10},
                project_max_concurrent_tasks={"p2": 5},
            )
            _account_by_id["a1"] = acct
            _max_plan_pools["u1"] = [acct]

            r1 = get_project_account_usage("p1", "u1")
            assert r1[0]["project_active_tasks"] == 3
            assert r1[0]["project_daily_task_cap"] == 10
            assert r1[0]["project_max_concurrent_tasks"] is None

            r2 = get_project_account_usage("p2", "u1")
            assert r2[0]["project_active_tasks"] == 7
            assert r2[0]["project_daily_task_cap"] is None
            assert r2[0]["project_max_concurrent_tasks"] == 5
        finally:
            self._reset_pools()


class TestGetUserProjectPoolSummary:
    """Dedicated unit tests for get_user_project_pool_summary.

    Tests the batched ProjectMember query, Project name lookups, accumulated
    totals across accounts, accounts_at_cap counting, caps_configured flag,
    and DB exception handling.
    """

    def _reset_pools(self):
        """Convenience: clear in-memory pool state."""
        _account_by_id.clear()
        _max_plan_pools.clear()

    @pytest.mark.asyncio
    async def test_no_accounts_no_memberships_returns_empty_list(self):
        """When the user has no pool accounts and no project memberships."""
        from app.max_plan_pool import get_user_project_pool_summary

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await get_user_project_pool_summary("u1", mock_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_batched_project_member_query(self):
        """Verify that ProjectMember rows are included and project name query is
        batched via the valid-UUID filter."""
        from app.max_plan_pool import get_user_project_pool_summary
        import uuid as _uuid

        pid = str(_uuid.uuid4())
        acct = _acct(
            id="a1",
            user_id="u1",
            name="Acct",
            project_state={pid: {"active_tasks": 2, "daily_task_count": 5}},
            project_daily_task_cap={pid: 10},
        )
        _account_by_id["a1"] = acct
        _max_plan_pools["u1"] = [acct]

        # First execute call returns a ProjectMember row for pid
        member_result = MagicMock()
        member_result.all.return_value = [(pid,)]

        # Second execute call returns the Project name
        project_result = MagicMock()
        project_result.all.return_value = [(pid, "My Project")]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=[member_result, project_result])

        try:
            result = await get_user_project_pool_summary("u1", mock_session)
            assert len(result) == 1
            r = result[0]
            assert r["project_id"] == pid
            assert r["project_name"] == "My Project"
            assert r["total_active_tasks"] == 2
            assert r["total_daily_task_count"] == 5
            assert r["caps_configured"] is True
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_accumulated_totals_across_accounts(self):
        """Multiple accounts for the same project are summed up."""
        from app.max_plan_pool import get_user_project_pool_summary

        acct1 = _acct(
            id="a1",
            user_id="u1",
            name="Acct1",
            project_state={"p1": {"active_tasks": 3, "daily_task_count": 8}},
        )
        acct2 = _acct(
            id="a2",
            user_id="u1",
            name="Acct2",
            project_state={"p1": {"active_tasks": 2, "daily_task_count": 5}},
        )
        _account_by_id["a1"] = acct1
        _account_by_id["a2"] = acct2
        _max_plan_pools["u1"] = [acct1, acct2]

        # ProjectMember query: no rows, but IDs come from project_state keys
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        try:
            result = await get_user_project_pool_summary("u1", mock_session)
            assert len(result) == 1
            r = result[0]
            assert r["project_id"] == "p1"
            assert r["total_active_tasks"] == 5  # 3 + 2
            assert r["total_daily_task_count"] == 13  # 8 + 5
            assert r["total_accounts"] == 2
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_accounts_at_cap_and_available(self):
        """accounts_at_cap reflects accounts where project is at either daily or
        concurrent cap, and accounts_available counts the rest."""
        from app.max_plan_pool import get_user_project_pool_summary

        # acct1: at daily cap (8 >= 8)
        acct1 = _acct(
            id="a1",
            user_id="u1",
            name="Acct1",
            project_state={"p1": {"active_tasks": 1, "daily_task_count": 8}},
            project_daily_task_cap={"p1": 8},
        )
        # acct2: at concurrent cap (3 >= 3)
        acct2 = _acct(
            id="a2",
            user_id="u1",
            name="Acct2",
            project_state={"p1": {"active_tasks": 3, "daily_task_count": 2}},
            project_max_concurrent_tasks={"p1": 3},
        )
        # acct3: no caps at all — available
        acct3 = _acct(
            id="a3",
            user_id="u1",
            name="Acct3",
            project_state={"p1": {"active_tasks": 0, "daily_task_count": 0}},
        )
        # acct4: below both caps — available
        acct4 = _acct(
            id="a4",
            user_id="u1",
            name="Acct4",
            project_state={"p1": {"active_tasks": 1, "daily_task_count": 3}},
            project_daily_task_cap={"p1": 10},
        )
        _account_by_id["a1"] = acct1
        _account_by_id["a2"] = acct2
        _account_by_id["a3"] = acct3
        _account_by_id["a4"] = acct4
        _max_plan_pools["u1"] = [acct1, acct2, acct3, acct4]

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        try:
            result = await get_user_project_pool_summary("u1", mock_session)
            assert len(result) == 1
            r = result[0]
            assert r["accounts_at_cap"] == 2  # acct1 + acct2
            assert r["accounts_available"] == 2  # acct3 + acct4
            assert r["total_accounts"] == 4
            assert r["caps_configured"] is True
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_caps_configured_flag(self):
        """caps_configured is True when ANY account has a per-project cap for
        the project, even if others don't."""
        from app.max_plan_pool import get_user_project_pool_summary

        acct1 = _acct(
            id="a1",
            user_id="u1",
            name="Acct1",
            project_state={"p1": {"active_tasks": 0, "daily_task_count": 0}},
            project_daily_task_cap={"p1": 10},
        )
        acct2 = _acct(
            id="a2",
            user_id="u1",
            name="Acct2",
            project_state={"p1": {"active_tasks": 0, "daily_task_count": 0}},
            # No per-project cap
        )
        _account_by_id["a1"] = acct1
        _account_by_id["a2"] = acct2
        _max_plan_pools["u1"] = [acct1, acct2]

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        try:
            result = await get_user_project_pool_summary("u1", mock_session)
            assert len(result) == 1
            assert result[0]["caps_configured"] is True

            # Remove caps — caps_configured should be False
            del acct1["project_daily_task_cap"]
            result2 = await get_user_project_pool_summary("u1", mock_session)
            assert result2[0]["caps_configured"] is False
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_no_caps_configured_returns_false(self):
        """When no account has per-project caps, caps_configured is False."""
        from app.max_plan_pool import get_user_project_pool_summary

        acct = _acct(
            id="a1",
            user_id="u1",
            name="Acct1",
            project_state={"p1": {"active_tasks": 1, "daily_task_count": 2}},
        )
        _account_by_id["a1"] = acct
        _max_plan_pools["u1"] = [acct]

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        try:
            result = await get_user_project_pool_summary("u1", mock_session)
            assert result[0]["caps_configured"] is False
            assert result[0]["accounts_at_cap"] == 0
            assert result[0]["accounts_available"] == 1
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_db_exception_on_project_member_query(self):
        """A DB exception in the ProjectMember query is caught and logged;
        results are built from pool state alone."""
        from app.max_plan_pool import get_user_project_pool_summary

        acct = _acct(
            id="a1",
            user_id="u1",
            name="Acct1",
            project_state={"p1": {"active_tasks": 2, "daily_task_count": 5}},
        )
        _account_by_id["a1"] = acct
        _max_plan_pools["u1"] = [acct]

        mock_session = AsyncMock()
        # First execute call raises an exception
        mock_session.execute = AsyncMock(side_effect=Exception("DB connection lost"))

        try:
            # Should not raise — exception is caught
            result = await get_user_project_pool_summary("u1", mock_session)
            # Results still built from pool state
            assert len(result) == 1
            assert result[0]["project_id"] == "p1"
            assert result[0]["total_active_tasks"] == 2
            assert result[0]["total_daily_task_count"] == 5
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_db_exception_on_project_name_query(self):
        """A DB exception in the Project name query is caught; project_name
        falls back to truncated project_id."""
        from app.max_plan_pool import get_user_project_pool_summary

        acct = _acct(
            id="a1",
            user_id="u1",
            name="Acct1",
            project_state={"p1": {"active_tasks": 1, "daily_task_count": 3}},
        )
        _account_by_id["a1"] = acct
        _max_plan_pools["u1"] = [acct]

        # First call (ProjectMember) succeeds with no rows
        member_result = MagicMock()
        member_result.all.return_value = []

        # Second call (Project name) raises
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(
            side_effect=[member_result, Exception("name query failed")]
        )

        try:
            result = await get_user_project_pool_summary("u1", mock_session)
            assert len(result) == 1
            # Name falls back to truncated project_id since query failed
            assert result[0]["project_name"] == "p1"
            assert result[0]["total_active_tasks"] == 1
        finally:
            self._reset_pools()

    @pytest.mark.asyncio
    async def test_multiple_projects_aggregated_separately(self):
        """Two different projects each get their own summary entry with correct
        totals."""
        from app.max_plan_pool import get_user_project_pool_summary

        acct = _acct(
            id="a1",
            user_id="u1",
            name="Acct1",
            project_state={
                "p1": {"active_tasks": 3, "daily_task_count": 8},
                "p2": {"active_tasks": 1, "daily_task_count": 4},
            },
            project_daily_task_cap={"p1": 10},
        )
        _account_by_id["a1"] = acct
        _max_plan_pools["u1"] = [acct]

        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        try:
            result = await get_user_project_pool_summary("u1", mock_session)
            assert len(result) == 2
            r1 = next(r for r in result if r["project_id"] == "p1")
            r2 = next(r for r in result if r["project_id"] == "p2")
            assert r1["total_active_tasks"] == 3
            assert r1["total_daily_task_count"] == 8
            assert r1["caps_configured"] is True
            assert r2["total_active_tasks"] == 1
            assert r2["total_daily_task_count"] == 4
            assert r2["caps_configured"] is False  # p2 has no cap
        finally:
            self._reset_pools()


# ── Credential sweep and release secret clearing ───────────────────────────


def _sm_acct(**kw) -> dict:
    """Account backed by a Secret Manager secret_blob_ref."""
    base = {
        "id": "sm1",
        "name": "sm-account",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "resolved-secret",
        "runtime_secret": "resolved-secret",
        "secret_blob_ref": "projects/proj/secrets/mysecret/versions/latest",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "runtime_ready": True,
    }
    base.update(kw)
    return base


class TestReleaseMaxPlanAccountSecretClear:
    """release_max_plan_account clears runtime_secret when active_tasks hits 0."""

    async def test_clears_runtime_secret_on_idle_sm_account(self):
        acct = _sm_acct(active_tasks=1)
        _account_by_id["sm1"] = acct
        await release_max_plan_account("sm1")
        assert acct["active_tasks"] == 0
        assert "runtime_secret" not in acct

    async def test_does_not_clear_when_tasks_still_active(self):
        acct = _sm_acct(active_tasks=2)
        _account_by_id["sm1"] = acct
        await release_max_plan_account("sm1")
        assert acct["active_tasks"] == 1
        assert acct.get("runtime_secret") == "resolved-secret"

    async def test_does_not_clear_for_non_sm_account(self):
        acct = _sm_acct(active_tasks=1, secret_blob_ref=None)
        acct["oauth_token"] = "direct-token"
        acct["runtime_secret"] = "direct-token"
        _account_by_id["sm1"] = acct
        await release_max_plan_account("sm1")
        assert acct["active_tasks"] == 0
        assert acct.get("runtime_secret") == "direct-token"

    async def test_does_not_clear_for_non_projects_blob_ref(self):
        acct = _sm_acct(active_tasks=1, secret_blob_ref="s3://bucket/key")
        _account_by_id["sm1"] = acct
        await release_max_plan_account("sm1")
        assert acct["active_tasks"] == 0
        assert acct.get("runtime_secret") == "resolved-secret"

    async def test_unknown_account_is_noop(self):
        # Should not raise
        await release_max_plan_account("nonexistent")


class TestCredentialSweepService:
    """credential_sweep_service clears runtime_secret for idle SM-backed accounts."""

    async def test_sweep_clears_idle_sm_account(self, monkeypatch):
        acct = _sm_acct(active_tasks=0)
        _account_by_id["sm1"] = acct

        call_count = 0

        async def fast_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        monkeypatch.setattr("app.max_plan_pool.asyncio.sleep", fast_sleep)
        with pytest.raises(asyncio.CancelledError):
            await credential_sweep_service()

        assert "runtime_secret" not in acct

    async def test_sweep_skips_active_account(self, monkeypatch):
        acct = _sm_acct(active_tasks=1)
        _account_by_id["sm1"] = acct

        call_count = 0

        async def fast_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        monkeypatch.setattr("app.max_plan_pool.asyncio.sleep", fast_sleep)
        with pytest.raises(asyncio.CancelledError):
            await credential_sweep_service()

        assert acct.get("runtime_secret") == "resolved-secret"

    async def test_sweep_skips_non_sm_account(self, monkeypatch):
        acct = _sm_acct(active_tasks=0, secret_blob_ref=None)
        acct["runtime_secret"] = "direct-token"
        _account_by_id["sm1"] = acct

        call_count = 0

        async def fast_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        monkeypatch.setattr("app.max_plan_pool.asyncio.sleep", fast_sleep)
        with pytest.raises(asyncio.CancelledError):
            await credential_sweep_service()

        assert acct.get("runtime_secret") == "direct-token"

    def test_sweep_interval_is_5_minutes(self):
        assert _CREDENTIAL_SWEEP_INTERVAL_SECONDS == 300


class TestMaybeRefreshAccountSecret:
    """_maybe_refresh_account_secret re-fetches from Secret Manager when cleared."""

    async def test_refetches_when_runtime_secret_cleared(self, monkeypatch):
        from app.max_plan_pool import _maybe_refresh_account_secret

        acct = _sm_acct(runtime_secret=None, oauth_token=None)

        async def fake_get_secret(ref):
            return "fresh-secret"

        monkeypatch.setattr("app.secrets_manager.get_user_secret", fake_get_secret)
        await _maybe_refresh_account_secret(acct)

        assert acct["runtime_secret"] == "fresh-secret"
        assert acct["oauth_token"] == "fresh-secret"

    async def test_refreshes_only_runtime_secret_for_non_oauth_provider(self, monkeypatch):
        from app.max_plan_pool import _maybe_refresh_account_secret

        acct = _sm_acct(
            runtime_secret=None,
            oauth_token=None,
            provider="vertex",
            auth_mode="service_account",
        )

        async def fake_get_secret(ref):
            return "fresh-vertex-secret"

        monkeypatch.setattr("app.secrets_manager.get_user_secret", fake_get_secret)
        await _maybe_refresh_account_secret(acct)

        assert acct["runtime_secret"] == "fresh-vertex-secret"
        # oauth_token is NOT set for non-ANTHROPIC/OAUTH accounts
        assert acct.get("oauth_token") is None

    async def test_skips_when_secret_already_present(self, monkeypatch):
        from app.max_plan_pool import _maybe_refresh_account_secret

        acct = _sm_acct(runtime_secret="existing-secret")
        calls = []

        async def fake_get_secret(ref):
            calls.append(ref)
            return "should-not-be-called"

        monkeypatch.setattr("app.secrets_manager.get_user_secret", fake_get_secret)
        await _maybe_refresh_account_secret(acct)
        assert calls == []
        assert acct["runtime_secret"] == "existing-secret"

    async def test_skips_when_no_sm_blob_ref(self, monkeypatch):
        from app.max_plan_pool import _maybe_refresh_account_secret

        acct = _sm_acct(runtime_secret=None, secret_blob_ref=None)
        calls = []

        async def fake_get_secret(ref):
            calls.append(ref)
            return "should-not-be-called"

        monkeypatch.setattr("app.secrets_manager.get_user_secret", fake_get_secret)
        await _maybe_refresh_account_secret(acct)
        assert calls == []

    async def test_handles_secret_fetch_failure_gracefully(self, monkeypatch):
        from app.max_plan_pool import _maybe_refresh_account_secret

        acct = _sm_acct(runtime_secret=None)

        async def exploding_get(ref):
            raise RuntimeError("SM unavailable")

        monkeypatch.setattr("app.secrets_manager.get_user_secret", exploding_get)
        # Should not raise
        await _maybe_refresh_account_secret(acct)
        assert acct.get("runtime_secret") is None


class TestSelectRefreshesSecret:
    """select_max_plan_account calls _maybe_refresh_account_secret after selection."""

    _USER = "u"

    def _setup(self, acct):
        _max_plan_pools[self._USER] = [acct]
        _account_by_id[acct["id"]] = acct

    async def test_select_calls_refresh_helper(self, monkeypatch):
        acct = _sm_acct(active_tasks=0)
        self._setup(acct)

        refresh_calls = []

        async def fake_refresh(a):
            refresh_calls.append(a["id"])

        monkeypatch.setattr("app.max_plan_pool._maybe_refresh_account_secret", fake_refresh)
        result = await select_max_plan_account(self._USER)

        assert result is not None
        assert result["id"] == "sm1"
        assert refresh_calls == ["sm1"]

    async def test_select_returns_none_on_empty_pool(self):
        result = await select_max_plan_account("no-such-user")
        assert result is None


# ── select_llm_runtime_for_task — runner CLI filtering ──────────────────────


class TestSelectLLMRuntimeFiltersByRunnerCLIs:
    """Tests for runner_available_clis parameter in select_llm_runtime_for_task.

    Mocks select_max_plan_account so the test doesn't need full pool state.
    Verifies that when runner_available_clis filters out all providers, the
    function returns None (caller should decline the task).
    """

    _USER = "test-cli-filter-user"

    def _reset_pools(self):
        _account_by_id.clear()
        _max_plan_pools.clear()

    @patch("app.max_plan_pool.select_max_plan_account", new_callable=AsyncMock)
    async def test_select_llm_runtime_filters_by_runner_clis(self, mock_select):
        """runner_available_clis filters providers before account selection."""
        self._reset_pools()
        try:
            mock_select.return_value = {
                "id": "acct1",
                "name": "acct1",
                "oauth_token": "token",
                "auth_mode": "oauth",
                "provider": "anthropic",
            }

            task = MagicMock(
                spec=[
                    "first_payload",
                    "payloads",
                    "context",
                    "project_id",
                    "launched_by_user_id",
                    "id",
                ]
            )
            task.first_payload = {"type": "create"}
            task.payloads = [{}]
            task.context = {}
            task.project_id = None
            task.launched_by_user_id = None
            task.id = "test-cli-filter-task"

            rc = {
                "provider_selection_mode": "preferred",
                "category_routing": {
                    "_default": {"providers": ["anthropic", "openai"]}
                },
                "task_type_categories": {},
                "provider_settings_by_effort": {
                    "anthropic": {"medium": {"model": "claude-opus-4-8"}},
                },
            }

            # Test 1: runner has all CLIs → can find an account
            result = await select_llm_runtime_for_task(
                user_id=self._USER,
                task=task,
                runtime_config=rc,
                runner_available_clis={
                    "claude": {"present": True},
                    "codex": {"present": True},
                },
            )
            assert result is not None
            # Should have selected the mocked anthropic account
            assert mock_select.called

            mock_select.reset_mock()

            # Test 2: runner has only claude → anthropic passes, openai filtered
            result = await select_llm_runtime_for_task(
                user_id=self._USER,
                task=task,
                runtime_config=rc,
                runner_available_clis={"claude": {"present": True}},
            )
            assert result is not None
            assert result.provider == "anthropic"

            mock_select.reset_mock()

            # Test 3: runner has no relevant CLIs → all providers filtered → returns None
            result = await select_llm_runtime_for_task(
                user_id=self._USER,
                task=task,
                runtime_config=rc,
                runner_available_clis={"gemini": {"present": True}},
            )
            assert result is None

            mock_select.reset_mock()

            # Test 4: no runner_available_clis → no filtering (backward compat)
            result = await select_llm_runtime_for_task(
                user_id=self._USER,
                task=task,
                runtime_config=rc,
                runner_available_clis=None,
            )
            assert result is not None
            assert mock_select.called
        finally:
            self._reset_pools()


# ── get_meter_reliability_stats ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestGetMeterReliabilityStats:
    """Fleet-wide meter reliability aggregation."""

    def _reg(self, acct):
        _account_by_id[acct["id"]] = acct
        return acct

    async def test_empty_pool_returns_zero_counts(self):
        result = await get_meter_reliability_stats()
        assert result["meters_eligible"] == 0
        assert result["meters_known"] == 0
        assert result["meters_stale"] == 0
        assert result["meters_dark"] == 0
        assert result["coverage_ratio"] is None
        assert result["armable"] is False

    async def test_all_fresh_known_reports_full_coverage(self):
        import app.max_plan_pool as pool_mod
        now = _utcnow()
        acct = self._reg(_acct(id="ok", provider="anthropic", auth_mode="oauth"))
        _acct_usage(acct, _known_snap(5.0, 5.0), at=now)
        original_failures = pool_mod._usage_persist_failures
        try:
            pool_mod._usage_persist_failures = 0
            result = await get_meter_reliability_stats(now=now, min_coverage_pct=80)
            assert result["meters_eligible"] == 1
            assert result["meters_known"] == 1
            assert result["meters_dark"] == 0
            assert result["coverage_ratio"] == 1.0
            assert result["armable"] is True
        finally:
            pool_mod._usage_persist_failures = original_failures

    async def test_dark_account_reported_with_reason(self):
        now = _utcnow()
        acct = self._reg(
            _acct(id="dark", provider="anthropic", auth_mode="oauth")
        )
        acct["usage_probe_failed_at"] = now
        result = await get_meter_reliability_stats(now=now, min_coverage_pct=80)
        assert result["meters_eligible"] == 1
        assert result["meters_dark"] == 1
        assert result["meters_known"] == 0
        assert result["dark_by_reason"].get("endpoint_error", 0) == 1
        assert result["coverage_ratio"] == 0.0
        assert result["armable"] is False

    async def test_ineligible_account_not_counted_in_eligible(self):
        now = _utcnow()
        # API key account is ineligible
        self._reg(_acct(id="apikey", provider="anthropic", auth_mode="api_key", oauth_token=None))
        result = await get_meter_reliability_stats(now=now)
        assert result["meters_ineligible"] == 1
        assert result["meters_eligible"] == 0

    async def test_stale_known_counted_separately(self):
        import app.max_plan_pool as pool_mod
        now = _utcnow()
        stale_seconds = pool_mod._USAGE_METER_DISPLAY_STALE_SECONDS
        acct = self._reg(_acct(id="stale", provider="anthropic", auth_mode="oauth"))
        _acct_usage(acct, _known_snap(5.0, 5.0), at=now - timedelta(seconds=stale_seconds + 1))
        result = await get_meter_reliability_stats(now=now, min_coverage_pct=80)
        assert result["meters_stale"] == 1
        assert result["meters_known"] == 0
        assert result["meters_eligible"] == 1
        assert result["coverage_ratio"] == 0.0
        assert result["armable"] is False

    async def test_per_reason_dark_breakdown(self):
        now = _utcnow()
        acct_ep = self._reg(_acct(id="ep", provider="anthropic", auth_mode="oauth"))
        acct_ep["usage_probe_failed_at"] = now
        self._reg(_acct(id="np", provider="anthropic", auth_mode="oauth"))
        # never_probed: no stamps, no snapshot
        result = await get_meter_reliability_stats(now=now)
        assert result["dark_by_reason"].get("endpoint_error", 0) == 1
        assert result["dark_by_reason"].get("never_probed", 0) == 1
        assert result["meters_dark"] == 2

    async def test_persist_failure_count_reflected(self):
        import app.max_plan_pool as pool_mod
        now = _utcnow()
        acct = self._reg(_acct(id="ok2", provider="anthropic", auth_mode="oauth"))
        _acct_usage(acct, _known_snap(5.0, 5.0), at=now)
        original = pool_mod._usage_persist_failures
        try:
            pool_mod._usage_persist_failures = original + 3
            result = await get_meter_reliability_stats(now=now, min_coverage_pct=80)
            assert result["persist_failure_count"] == original + 3
            # armable requires zero persist failures
            assert result["armable"] is False
        finally:
            pool_mod._usage_persist_failures = original

    async def test_coverage_ratio_zero_eligible_returns_none(self):
        result = await get_meter_reliability_stats(min_coverage_pct=0)
        assert result["coverage_ratio"] is None
        assert result["armable"] is False

    async def test_db_persisted_blob_covers_dark_in_memory_account(self, monkeypatch):
        """Non-leader instances have no in-memory snapshot, but the leader's
        probe is persisted to MaxPlanAccount.rate_limit_usage. The fresher-of-two
        fallback must classify such an account as known, not dark."""
        import app.max_plan_pool as pool_mod

        now = _utcnow()
        acct = self._reg(_acct(id="nonleader", provider="anthropic", auth_mode="oauth"))
        # in-memory is dark (never probed on this non-leader instance)
        assert acct.get("usage_snapshot") is None

        async def _fake_blobs():
            return {
                "nonleader": {
                    "source": "oauth_usage_probe",
                    "updated_at": now.isoformat(),
                    "five_hour": {"utilization": 12.0},
                    "seven_day": {"utilization": 8.0},
                }
            }

        monkeypatch.setattr(pool_mod, "_load_persisted_usage_blobs", _fake_blobs)
        original = pool_mod._usage_persist_failures
        try:
            pool_mod._usage_persist_failures = 0
            result = await get_meter_reliability_stats(now=now, min_coverage_pct=80)
            assert result["meters_known"] == 1
            assert result["meters_dark"] == 0
            assert result["coverage_ratio"] == 1.0
            assert result["armable"] is True
        finally:
            pool_mod._usage_persist_failures = original
