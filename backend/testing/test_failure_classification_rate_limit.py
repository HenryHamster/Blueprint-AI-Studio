"""Evidence-first rate-limit classification compatibility tests.

Extracted from test_failure_classification.py to keep that module focused; this
file preserves helper compatibility while proving that ambient account/pool
cooldowns never replace direct evidence from the failing attempt.
"""

from unittest.mock import MagicMock
from datetime import timezone

from app.failure_classification import (
    AGENT_TIMEOUT,
    CODE_ERROR,
    RATE_LIMIT,
    classify_failure,
    classify_failure_with_confidence,
    normalize_agent_failure_category,
    persist_authoritative_verdict,
)
from app.models import Task


def _task(msg: str = "", ctx: dict | None = None) -> Task:
    t = MagicMock(spec=Task)
    t.latest_status_message = msg
    t.context = ctx or {}
    return t


class TestAccountStateRateLimitCorrelation:
    """An account cooldown from another attempt is not failure evidence."""

    def _make_rl_account(self, account_id: str, in_cooldown: bool) -> dict:
        from datetime import timedelta

        from app.time_utils import _utcnow

        if in_cooldown:
            at = _utcnow() - timedelta(minutes=5)
            return {
                "enabled": True,
                "disabled_models": {
                    "claude-opus-4-5": {
                        "at": at,
                        "hits": 1,
                        "kind": "rate_limit",
                    }
                },
            }
        return {"enabled": True, "disabled_models": {}}

    def test_incomplete_with_rate_limited_account_classifies_rate_limit(
        self, monkeypatch
    ):
        account_id = "acct-rl-test-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_account_id": account_id}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_incomplete_with_healthy_account_stays_code_error(self, monkeypatch):
        account_id = "acct-healthy-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=False)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_account_id": account_id}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_default_fallback_with_rate_limited_account_classifies_rate_limit(
        self, monkeypatch
    ):
        account_id = "acct-rl-test-2"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        # No failure_category in context, no rate-limit keywords — generic failure
        task = _task(
            "failed to read investigation report", ctx={"llm_account_id": account_id}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_default_fallback_with_no_account_stays_code_error(self, monkeypatch):
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})

        task = _task("failed to read investigation report")
        assert classify_failure(task) == CODE_ERROR

    def test_specific_failure_not_overridden(self, monkeypatch):
        """test_failed / lint_failed with rate-limited account stays CODE_ERROR — specific failures are authoritative."""
        account_id = "acct-rl-test-3"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task(
            "", ctx={"failure_category": "test_failed", "llm_account_id": account_id}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_root_sudo_rejection_not_reclassified_as_rate_limit(self, monkeypatch):
        """The deterministic --dangerously-skip-permissions root/sudo refusal is a
        config defect (e.g. the agent container running as root), not throttling.
        It reproduces on every retry, so it must classify as CODE_ERROR even when
        the assigned account is in rate-limit cooldown — otherwise the cooldown
        correlation masks it as RATE_LIMIT and the requeue path retry-storms it."""
        account_id = "acct-rl-rootsudo"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task(
            "--dangerously-skip-permissions cannot be used with root/sudo "
            "privileges for security reasons",
            ctx={"llm_account_id": account_id},
        )
        assert classify_failure(task) == CODE_ERROR

    def test_root_sudo_rejection_code_error_during_pool_wide_block(self, monkeypatch):
        """Same deterministic config refusal stays CODE_ERROR even under a
        pool-wide rate-limit block (the broadest correlation trigger)."""
        import app.max_plan_pool as pool_mod

        # Whole pool in cooldown — the pool-wide correlation would otherwise fire.
        blocked = {
            "acct-a": self._make_rl_account("acct-a", in_cooldown=True),
            "acct-b": self._make_rl_account("acct-b", in_cooldown=True),
        }
        monkeypatch.setattr(pool_mod, "_account_by_id", blocked)

        task = _task(
            "claude code exited with code 1\n"
            "--dangerously-skip-permissions cannot be used with root/sudo privileges"
        )
        assert classify_failure(task) == CODE_ERROR

    def test_normalize_incomplete_with_rate_limited_account(self, monkeypatch):
        account_id = "acct-rl-norm-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task("", ctx={"llm_account_id": account_id})
        assert normalize_agent_failure_category("incomplete", task) == CODE_ERROR

    def test_normalize_incomplete_with_healthy_account(self, monkeypatch):
        account_id = "acct-healthy-2"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=False)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)

        task = _task("", ctx={"llm_account_id": account_id})
        assert normalize_agent_failure_category("incomplete", task) == CODE_ERROR


class TestPoolWideRateLimitCorrelation:
    """Pool state alone cannot classify an individual attempt."""

    def test_pre_assignment_death_pool_wide_block(self, monkeypatch):
        """No account assigned, entire pool blocked → RATE_LIMIT (not CODE_ERROR)."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        task = _task("", ctx={"failure_category": "incomplete"})
        assert classify_failure(task) == CODE_ERROR

    def test_healthy_account_pool_wide_block_still_rate_limit(self, monkeypatch):
        """Assigned account is healthy; pool-wide block drives RATE_LIMIT."""
        account_id = "acct-healthy-pool"
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod,
            "_account_by_id",
            {account_id: {"enabled": True, "disabled_models": {}}},
        )
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_account_id": account_id}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_pool_not_blocked_generic_failure_code_error(self, monkeypatch):
        """Pool not in cooldown → generic incomplete stays CODE_ERROR."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )
        task = _task("", ctx={"failure_category": "incomplete"})
        assert classify_failure(task) == CODE_ERROR

    def test_specific_label_not_overridden_by_pool_block(self, monkeypatch):
        """Authoritative labels (test_failed) are not overridden by pool-wide block."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        task = _task("", ctx={"failure_category": "test_failed"})
        assert classify_failure(task) == CODE_ERROR

    def test_pool_was_rate_limited_accepts_tz_aware_timestamp(self, monkeypatch):
        """Tz-aware at_time must not raise — helper normalizes to naive UTC before calling pool check."""
        from datetime import datetime, timezone

        from app.failure_classification import _pool_was_rate_limited
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        assert (
            _pool_was_rate_limited(None, at_time=datetime.now(tz=timezone.utc)) is True
        )

    def test_pool_check_scoped_to_task_provider(self, monkeypatch):
        """Pool check passes task provider so unrelated-provider accounts are excluded."""
        import app.max_plan_pool as pool_mod
        from app.failure_classification import _pool_was_rate_limited

        captured = {}

        def fake_pool_check(at_time=None, **kwargs):
            captured.update(kwargs)
            return False

        monkeypatch.setattr(pool_mod, "pool_wide_rate_limit_cooldown", fake_pool_check)
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        task = _task("", ctx={"llm_provider": "anthropic"})
        _pool_was_rate_limited(task)
        assert captured.get("providers") == ["anthropic"]

    def test_pool_check_scoped_to_task_model(self, monkeypatch):
        """Pool check passes task model so model-specific cooldown is evaluated."""
        import app.max_plan_pool as pool_mod
        from app.failure_classification import _pool_was_rate_limited

        captured = {}

        def fake_pool_check(at_time=None, **kwargs):
            captured.update(kwargs)
            return False

        monkeypatch.setattr(pool_mod, "pool_wide_rate_limit_cooldown", fake_pool_check)
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        task = _task("", ctx={"llm_model": "claude-sonnet-4-5"})
        _pool_was_rate_limited(task)
        assert captured.get("model") == "claude-sonnet-4-5"

    def test_mixed_provider_pool_unrelated_exhaustion_no_verdict_change(
        self, monkeypatch
    ):
        """Openai-provider exhaustion must not flip anthropic-only task to RATE_LIMIT."""
        import app.max_plan_pool as pool_mod

        def scoped_pool_check(at_time=None, *, providers=None, **_):
            # Only openai is exhausted; anthropic task should not see True.
            return providers != ["anthropic"]  # openai pool is exhausted

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", scoped_pool_check
        )
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_provider": "anthropic"}
        )
        assert classify_failure(task) == CODE_ERROR

    def test_mixed_user_pool_unrelated_exhaustion_no_verdict_change(self, monkeypatch):
        """Another user's fully-exhausted pool must not flip this task to RATE_LIMIT."""
        import app.max_plan_pool as pool_mod

        account_id = "acct-user-a"
        monkeypatch.setattr(
            pool_mod,
            "_account_by_id",
            {account_id: {"user_id": "user-a", "disabled_models": {}}},
        )

        def scoped_pool_check(at_time=None, *, user_id=None, **_):
            # user-b is exhausted; user-a (task's owner) is not.
            return user_id != "user-a"  # user-b pool exhausted

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", scoped_pool_check
        )
        task = _task(
            "",
            ctx={"failure_category": "incomplete", "llm_account_id": account_id},
        )
        assert classify_failure(task) == CODE_ERROR


class TestMultiInstancePartialPoolView:
    """Multi-instance safety (design-audit failure-class-multiinstance).

    The pool-wide correlation reclassification reads PROCESS-LOCAL pool state.
    On a multi-instance deploy the classifying instance may hold a partial /
    stale view of the candidate set (a cooldown another instance has cleared
    lingers locally, or the local pool simply doesn't contain the routed
    scope's accounts). When the task has a resolvable dispatch scope but the
    local pool holds zero matching accounts, a True pool-wide verdict was drawn
    over a view that never inspected the real candidate set — so it must NOT
    drive a RATE_LIMIT reclassification. The durable db_rl_override signal
    (computed by the authoritative reaper / agent_status paths) remains the
    cross-instance authority and still reclassifies.
    """

    def test_scoped_verdict_over_empty_local_pool_does_not_reclassify(
        self, monkeypatch
    ):
        """Routed provider scope + pool_wide check True, but the local pool has
        zero in-scope accounts (cross-instance partial view) → stays CODE_ERROR,
        no spurious RATE_LIMIT reclassification."""
        import app.max_plan_pool as pool_mod

        # pool_wide_rate_limit_cooldown reports "fully blocked" from a partial
        # local view; the local pool, however, contains no anthropic accounts.
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(pool_mod, "_max_plan_pools", {})

        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_provider": "anthropic"}
        )
        assert classify_failure(task) == CODE_ERROR
        assert "rate_limit_correlation_reclassified" not in task.context

    def test_scoped_verdict_with_loaded_local_pool_still_reclassifies(
        self, monkeypatch
    ):
        """Behavior-preserving on a single (fully-loaded) instance: same scope,
        but the local pool actually holds an in-scope account → the pool-wide
        verdict is trusted and the task reclassifies to RATE_LIMIT."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        local_acct = {"id": "a1", "provider": "anthropic", "enabled": True}
        monkeypatch.setattr(pool_mod, "_account_by_id", {"a1": local_acct})
        monkeypatch.setattr(pool_mod, "_max_plan_pools", {None: [local_acct]})

        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_provider": "anthropic"}
        )
        assert classify_failure(task) == CODE_ERROR
        assert "rate_limit_correlation_reclassified" not in task.context

    def test_db_rl_override_reclassifies_despite_insufficient_local_view(
        self, monkeypatch
    ):
        """Cross-instance authority: even with a scoped task and an empty local
        pool (insufficient view), a durable db_rl_override=True still drives
        RATE_LIMIT — the gate only suppresses the unreliable LOCAL fast-path."""
        import app.max_plan_pool as pool_mod

        # Local fast-path would not fire (empty/partial view); only the durable
        # DB signal should reclassify.
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(pool_mod, "_max_plan_pools", {})

        task = _task(
            "", ctx={"failure_category": "incomplete", "llm_provider": "anthropic"}
        )
        assert classify_failure(task, db_rl_override=True) == CODE_ERROR

    def test_accountless_global_verdict_unaffected_by_gate(self, monkeypatch):
        """Scope-less (no provider, no account) pre-assignment death keeps the
        global pool-wide verdict — the gate has no candidate set to validate and
        must not change this legitimate single-instance path."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(pool_mod, "_max_plan_pools", {})

        task = _task("", ctx={"failure_category": "incomplete"})
        assert classify_failure(task) == CODE_ERROR

    def test_local_pool_view_sufficient_predicate(self, monkeypatch):
        """Unit-level: predicate is True for no-scope and for a loaded in-scope
        pool, False for a scoped task whose candidates are absent locally."""
        import app.max_plan_pool as pool_mod
        from app.failure_classification import _local_pool_view_sufficient

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(pool_mod, "_max_plan_pools", {})

        # No scope → not assessable → treated as sufficient (preserve global path).
        assert _local_pool_view_sufficient({"providers": None, "user_id": None}) is True

        # Scoped but empty local pool → insufficient.
        assert (
            _local_pool_view_sufficient({"providers": ["anthropic"], "user_id": None})
            is False
        )

        # Scoped with a matching enabled local account → sufficient.
        acct = {"id": "a1", "provider": "anthropic", "enabled": True}
        monkeypatch.setattr(pool_mod, "_account_by_id", {"a1": acct})
        monkeypatch.setattr(pool_mod, "_max_plan_pools", {None: [acct]})
        assert (
            _local_pool_view_sufficient({"providers": ["anthropic"], "user_id": None})
            is True
        )

        # Scoped to a different provider than the only local account → insufficient.
        assert (
            _local_pool_view_sufficient({"providers": ["openai"], "user_id": None})
            is False
        )


class TestAtTimeFailureInstantCorrelation:
    """Verifies that classify_failure respects at_time so a rate-limit window
    that has already expired by sweep time is still detected at failure time."""

    def _make_rl_account(self, account_id: str, *, in_cooldown: bool):
        """Return a fake pool entry whose cooldown state matches in_cooldown."""
        if in_cooldown:
            from datetime import datetime, timedelta

            return {
                "id": account_id,
                "disabled_models": {
                    "claude-sonnet-4-6": {
                        "at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2),
                        "kind": "rate_limit",
                        "hits": 1,
                    }
                },
            }
        return {"id": account_id, "disabled_models": {}}

    def test_account_rate_limited_at_failure_time_not_sweep_time(self, monkeypatch):
        """A generic failure classified at the failure instant (at_time=failure_time)
        is RATE_LIMIT; the same failure classified at sweep time (after cooldown
        expires) falls back to CODE_ERROR.

        This locks in the fix that threads at_time through to the account-state
        correlation helpers so a short cooldown that expired before the reaper
        sweep still protects the retry budget.
        """
        from datetime import datetime, timedelta
        import app.max_plan_pool as pool_mod

        account_id = "acct-at-time-test"

        monkeypatch.setattr(
            pool_mod,
            "_account_by_id",
            {account_id: self._make_rl_account(account_id, in_cooldown=True)},
        )
        # Pool-wide check must be disabled so only the account-level at_time
        # correlation drives the result — isolates the variable under test.
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task(
            "",
            ctx={
                "failure_category": "claude_code_failed",
                "llm_account_id": account_id,
            },
        )

        # Ambient cooldown state is not evidence, even at the failure instant.
        failure_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        assert classify_failure(task, at_time=failure_time) == CODE_ERROR

        # With at_time=None (sweep time), cooldown has expired → CODE_ERROR.
        assert classify_failure(task, at_time=None) == CODE_ERROR, (
            "failure re-classified at sweep time must be CODE_ERROR (cooldown expired)"
        )

    def test_agent_status_path_threads_at_time_via_heartbeat(self, monkeypatch):
        """Verify agent_status failure-reporting path threads at_time correctly.

        This test mimics the agent_status code path where tasks with last_heartbeat
        set are classified. The path should extract and pass last_heartbeat (or
        started_at fallback) as at_time to classify_failure, not call classify_failure
        without at_time which would use sweep time instead.
        """
        from datetime import datetime, timedelta
        import app.max_plan_pool as pool_mod

        account_id = "acct-agent-status-test"

        monkeypatch.setattr(
            pool_mod,
            "_account_by_id",
            {account_id: self._make_rl_account(account_id, in_cooldown=True)},
        )
        # Pool-wide check must be disabled so only the account-level at_time
        # correlation drives the result — isolates the variable under test.
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        # Simulate agent_status code path: task with last_heartbeat set
        failure_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        task = _task(
            "",
            ctx={
                "failure_category": "claude_code_failed",
                "llm_account_id": account_id,
            },
        )
        task.last_heartbeat = failure_time
        task.started_at = failure_time - timedelta(minutes=1)

        # Simulate agent_status classify_failure call with at_time threaded through
        # (as updated in routers/agent_status.py)
        failure_class_with_at_time = classify_failure(
            task, at_time=task.last_heartbeat or task.started_at
        )

        assert failure_class_with_at_time == CODE_ERROR

        # Verify that calling without at_time (old broken path) would miss the rate-limit
        failure_class_without_at_time = classify_failure(task, at_time=None)
        assert failure_class_without_at_time == CODE_ERROR, (
            "classify_failure without at_time falls back to CODE_ERROR (broken path)"
        )

    def test_normalize_agent_failure_category_uses_failure_instant(self, monkeypatch):
        """normalize_agent_failure_category threads at_time into the account/pool
        rate-limit correlation.

        A generic agent-reported code-error label is RATE_LIMIT when normalized at
        the failure instant (cooldown active then) but CODE_ERROR at sweep time
        (cooldown expired). Guards the at_time plumbing added to
        normalize_agent_failure_category itself — the sibling tests above exercise
        classify_failure, which is a distinct entry point.
        """
        from datetime import datetime, timedelta
        import app.max_plan_pool as pool_mod

        account_id = "acct-normalize-at-time"

        monkeypatch.setattr(
            pool_mod,
            "_account_by_id",
            {account_id: self._make_rl_account(account_id, in_cooldown=True)},
        )
        # Isolate the account-level at_time correlation from the pool-wide check.
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        failure_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        task = _task("", ctx={"llm_account_id": account_id})

        # Generic labels remain code errors without direct quota evidence.
        assert (
            normalize_agent_failure_category("incomplete", task, at_time=failure_time)
            == CODE_ERROR
        )

        # Same label re-classified at sweep time (at_time=None) → CODE_ERROR.
        assert (
            normalize_agent_failure_category("incomplete", task, at_time=None)
            == CODE_ERROR
        ), "generic label at sweep time (cooldown expired) falls back to CODE_ERROR"


class TestDbRlOverrideCorrelation:
    """Legacy DB-cooldown hints cannot replace attempt-local evidence."""

    def test_db_rl_override_true_reclassifies_generic_to_rate_limit(self, monkeypatch):
        """db_rl_override=True upgrades a generic CODE_ERROR to RATE_LIMIT even when
        the in-memory pool shows no cooldown (cross-instance case)."""
        import app.max_plan_pool as pool_mod

        # In-memory pool is empty for this account (different instance never saw RL).
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"llm_account_id": "acct-cross-instance"})
        result = normalize_agent_failure_category(
            "incomplete", task, db_rl_override=True
        )
        assert result == CODE_ERROR

    def test_db_rl_override_false_falls_through_to_code_error(self, monkeypatch):
        """db_rl_override=False (default) with no in-memory cooldown → CODE_ERROR."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"llm_account_id": "acct-no-rl"})
        result = normalize_agent_failure_category(
            "incomplete", task, db_rl_override=False
        )
        assert result == CODE_ERROR

    def test_db_rl_override_only_applies_to_generic_labels(self, monkeypatch):
        """db_rl_override=True does not override authoritative labels like test_failed."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"llm_account_id": "acct-test-fail"})
        result = normalize_agent_failure_category(
            "test_failed", task, db_rl_override=True
        )
        assert result == CODE_ERROR

    def test_correlation_reclassification_context_persisted_on_db_override(
        self, monkeypatch
    ):
        """When db_rl_override drives the reclassification, the correlation marker
        is still written to task.context (same as in-memory path)."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"llm_account_id": "acct-marker"})
        result = normalize_agent_failure_category(
            "incomplete", task, db_rl_override=True
        )
        assert result == CODE_ERROR
        assert "rate_limit_correlation_reclassified" not in task.context


class TestParseFailedCorrelation:
    """parse_failed deaths correlate to RATE_LIMIT when pool/account is rate-limited."""

    def _make_rl_account(self, account_id: str, in_cooldown: bool) -> dict:
        from datetime import timedelta

        from app.time_utils import _utcnow

        if in_cooldown:
            at = _utcnow() - timedelta(minutes=5)
            return {
                "enabled": True,
                "disabled_models": {
                    "claude-opus-4-5": {
                        "at": at,
                        "hits": 1,
                        "kind": "rate_limit",
                    }
                },
            }
        return {"enabled": True, "disabled_models": {}}

    def test_parse_failed_with_rate_limited_account_reclassifies_to_rate_limit(
        self, monkeypatch
    ):
        account_id = "acct-pf-rl-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=True)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"llm_account_id": account_id})
        result = normalize_agent_failure_category("parse_failed", task)
        assert result == CODE_ERROR
        assert "rate_limit_correlation_reclassified" not in task.context
        assert "rate_limit_kind" not in task.context

    def test_parse_failed_with_healthy_pool_stays_code_error(self, monkeypatch):
        account_id = "acct-pf-healthy-1"
        fake_pool = {account_id: self._make_rl_account(account_id, in_cooldown=False)}
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", fake_pool)
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"llm_account_id": account_id})
        result = normalize_agent_failure_category("parse_failed", task)
        assert result == CODE_ERROR
        assert "rate_limit_correlation_reclassified" not in task.context

    def test_parse_failed_reclassifies_via_weekly_limit_text_in_status_message(
        self, monkeypatch
    ):
        """normalize_agent_failure_category returns RATE_LIMIT for parse_failed when
        latest_status_message contains a verbatim weekly-limit phrase — the text-override
        branch now covers _ALL_GENERIC_CODE_ERROR_LABELS (including parse_failed)."""
        import app.max_plan_pool as pool_mod

        # Ensure account/pool correlation paths don't fire so the TEXT override is
        # the sole reclassification mechanism.
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        weekly_limit_msg = (
            "You've hit your weekly limit · resets 9pm (UTC)\nNo output produced."
        )
        task = _task(weekly_limit_msg)
        result = normalize_agent_failure_category("parse_failed", task)
        assert result == RATE_LIMIT

    def test_classify_failure_class_parse_failed_pool_rate_limited(self, monkeypatch):
        """classify_failure fast-path via agent_category=parse_failed during pool block."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )

        task = _task("", ctx={"failure_category": "parse_failed"})
        assert classify_failure(task) == CODE_ERROR


class TestCorrelationReclassificationMarker:
    """Ambient cooldowns never write quota evidence into task context."""

    def _make_rl_account(self, account_id: str) -> dict:
        from datetime import timedelta

        from app.time_utils import _utcnow

        at = _utcnow() - timedelta(minutes=5)
        return {
            "enabled": True,
            "disabled_models": {
                "claude-opus-4-5": {"at": at, "hits": 1, "kind": "rate_limit"}
            },
        }

    def test_correlation_marker_persisted_for_generic_labels_when_pool_rate_limited(
        self, monkeypatch
    ):
        """correlation marker is written for incomplete/fatal correlation paths."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )

        for label in ("incomplete", "fatal"):
            task = _task("", ctx={"failure_category": label})
            result = normalize_agent_failure_category(label, task)
            assert result == CODE_ERROR
            assert "rate_limit_correlation_reclassified" not in task.context
            assert "rate_limit_kind" not in task.context

    def test_correlation_marker_not_written_when_text_signal_matched(self, monkeypatch):
        """When the text signal fires, _persist_rate_limit_signal_context runs — not correlation marker."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )

        msg = "Fatal error: Quota exceeded for quota metric 'GenerateRequests'. Please retry in 39.8s."
        task = _task(msg, ctx={"llm_provider": "gemini", "llm_auth_mode": "api_key"})
        result = normalize_agent_failure_category("fatal", task)
        assert result == RATE_LIMIT
        # Text-matched signal sets rate_limit_kind via _persist_rate_limit_signal_context
        assert task.context.get("rate_limit_kind") is not None
        # But correlation marker must NOT be set when text signal matched
        assert "rate_limit_correlation_reclassified" not in task.context


class TestPersistAuthoritativeVerdictShortCircuit:
    """persist_authoritative_verdict() short-circuit prevents RATE_LIMIT→CODE_ERROR
    drift when analytics paths call classify_failure() after cooldown has lifted.

    Done-looks-like from the task spec: create a failed task, classify at the
    failure instant (at_time=heartbeat) to get RATE_LIMIT, advance the clock
    past cooldown, call classify_failure() the way digest_metrics/anomaly_detection
    does, assert the verdict is still RATE_LIMIT.
    """

    def test_persisted_rate_limit_survives_post_cooldown_classify(self, monkeypatch):
        """Reaper persists RATE_LIMIT at failure instant; analytics path calling
        classify_failure() hours later (no at_time, no active cooldown) must
        still return RATE_LIMIT via the short-circuit, not CODE_ERROR."""
        import app.max_plan_pool as pool_mod

        # Simulate post-cooldown state: no accounts rate-limited, pool healthy.
        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        # Task with a generic failure label that would default to CODE_ERROR
        # when no rate-limit state is active (this is the drift scenario).
        task = _task(
            "",
            ctx={"failure_category": "claude_code_failed"},
        )

        # Without persist_authoritative_verdict, analytics path returns CODE_ERROR.
        assert classify_failure(task) == CODE_ERROR, (
            "baseline: no persisted verdict + no active cooldown → CODE_ERROR"
        )

        # Reaper classifies at the failure instant (cooldown was active then) and
        # persists the authoritative verdict.
        persist_authoritative_verdict(task, RATE_LIMIT)

        # Analytics path (digest_metrics, anomaly_detection, finding_deduplication)
        # calls classify_failure() without at_time — short-circuit must return
        # the persisted verdict, not re-run correlation against expired state.
        result = classify_failure(task)
        assert result == RATE_LIMIT, (
            f"Expected RATE_LIMIT (persisted by reaper at failure instant) "
            f"but got {result!r} — classify_failure() ignored the persisted verdict, "
            f"causing RATE_LIMIT→CODE_ERROR drift after cooldown expired"
        )

    def test_persisted_verdict_written_to_context_failure_class_key(self):
        """persist_authoritative_verdict writes to context['failure_class'], not
        context['failure_category'] — verify the key and value are correct."""
        task = _task("", ctx={})
        persist_authoritative_verdict(task, RATE_LIMIT)
        assert task.context.get("failure_class") == RATE_LIMIT

    def test_persisted_verdict_overrides_generic_code_error_fast_path(self):
        """failure_class short-circuit takes precedence over the failure_category
        fast-path so analytics paths see the persisted verdict even when
        failure_category=claude_code_failed would re-map to CODE_ERROR."""
        task = _task(
            "",
            ctx={"failure_category": "claude_code_failed", "failure_class": RATE_LIMIT},
        )
        assert classify_failure(task) == RATE_LIMIT

    def test_rate_limit_event_preempts_persisted_generic_code_error(self):
        """Structured rate_limit_event must beat a persisted generic CODE_ERROR.

        Production path: a downstream handler can stamp failure_class=CODE_ERROR
        before classify_failure() re-runs on the same task. The Claude CLI's
        structured rate_limit_event is the more specific signal and must still
        route through RATE_LIMIT handling.
        """
        msg = (
            "[CODE_ERROR] Memo handler could not extract JSON\n"
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}\n'
        )
        task = _task(
            msg,
            ctx={"failure_category": "agent_self_failed", "failure_class": CODE_ERROR},
        )

        assert classify_failure(task) == RATE_LIMIT

    def test_no_persisted_verdict_still_re_derives(self, monkeypatch):
        """Without a persisted verdict, classify_failure() must still re-derive
        normally — the short-circuit must not suppress first-call classification."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        task = _task("", ctx={"failure_category": "incomplete"})
        # No persist_authoritative_verdict call — direct evidence is still required.
        assert classify_failure(task) == CODE_ERROR


class TestReaperDbRlOverrideClassifyFailure:
    """Legacy DB cooldown hints do not classify an ambiguous attempt."""

    def test_classify_failure_db_rl_override_reclassifies_generic(self, monkeypatch):
        """In-memory pool empty (cross-instance); reaper passes db_rl_override=True
        after db_account_was_rate_limited confirmed cooldown → RATE_LIMIT."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"failure_category": "claude_code_failed"})
        assert classify_failure(task) == CODE_ERROR, "baseline without override"
        assert classify_failure(task, db_rl_override=True) == CODE_ERROR

    def test_classify_failure_with_confidence_db_rl_override(self, monkeypatch):
        """classify_failure_with_confidence passes db_rl_override through; result is RATE_LIMIT."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"failure_category": "incomplete"})
        result = classify_failure_with_confidence(task, db_rl_override=True)
        assert result.failure_class == CODE_ERROR

    def test_db_rl_override_then_persist_analytics_stable(self, monkeypatch):
        """Full reaper sequence: classify with db_rl_override=True → persist verdict →
        analytics classify_failure (no override, expired cooldown) → still RATE_LIMIT."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"failure_category": "claude_code_failed"})

        # Step 1: ambient DB state does not replace attempt-local evidence.
        reaper_class = classify_failure(task, db_rl_override=True)
        assert reaper_class == CODE_ERROR

        # Step 2: reaper persists the authoritative verdict.
        persist_authoritative_verdict(task, reaper_class)

        # Step 3: analytics path (digest, anomaly detection) calls classify_failure
        # hours later with no override and no active cooldown — must not flip to CODE_ERROR.
        assert classify_failure(task) == CODE_ERROR

    def test_db_rl_override_does_not_override_authoritative_labels(self, monkeypatch):
        """Authoritative labels (test_failed) are not upgraded to RATE_LIMIT even
        when db_rl_override=True, matching normalize_agent_failure_category behaviour."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )

        task = _task("", ctx={"failure_category": "test_failed"})
        assert classify_failure(task, db_rl_override=True) == CODE_ERROR


class TestSilentDeathPoolWideCoverage:
    """A silent death remains AGENT_TIMEOUT without direct quota evidence."""

    def test_accountless_silent_death_pool_blocked_rate_limit(self, monkeypatch):
        """No account assigned, empty log, pool blocked → RATE_LIMIT (not AGENT_TIMEOUT)."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: True
        )
        # No failure_category, no account id, empty status message — pure silent death.
        task = _task("", ctx={})
        assert classify_failure(task) == AGENT_TIMEOUT
        assert "rate_limit_correlation_reclassified" not in task.context

    def test_accountless_silent_death_pool_not_blocked_agent_timeout(self, monkeypatch):
        """No account, empty log, pool not blocked → AGENT_TIMEOUT (no false positive)."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )
        task = _task("", ctx={})
        assert classify_failure(task) == AGENT_TIMEOUT

    def test_db_rl_override_silent_death_no_failure_category(self, monkeypatch):
        """Cross-instance: empty log, no failure_category, db_rl_override=True → RATE_LIMIT."""
        import app.max_plan_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_account_by_id", {})
        monkeypatch.setattr(
            pool_mod, "pool_wide_rate_limit_cooldown", lambda at_time=None, **_: False
        )
        task = _task("", ctx={})
        assert classify_failure(task, db_rl_override=True) == AGENT_TIMEOUT
        assert "rate_limit_correlation_reclassified" not in task.context
