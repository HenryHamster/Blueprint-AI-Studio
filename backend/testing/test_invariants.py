"""
Behavioral invariant tests ("tripwires").

These tests assert that critical safety constants and patterns have not been
modified by agents. If an agent changes a safety value, CI catches it.

Run with: pytest testing/test_invariants.py -v --noconftest
"""
import os


# ── Safety Constants ────────────────────────────────────────────────────────

class TestSafetyConstants:
    def test_max_review_fix_iterations(self):
        """MAX_REVIEW_FIX_ITERATIONS must stay at 16."""
        from app.review_pipeline import MAX_REVIEW_FIX_ITERATIONS
        assert MAX_REVIEW_FIX_ITERATIONS == 16, (
            f"MAX_REVIEW_FIX_ITERATIONS changed to {MAX_REVIEW_FIX_ITERATIONS} — "
            "this controls review-fix loop runaway. Do not change without approval."
        )

    def test_max_retries_default(self):
        """Default max_retries must be 3."""
        from app.shared_state import runtime_config
        # The default is set from env var with fallback to 3
        assert runtime_config["max_retries"] == int(os.getenv("MAX_RETRIES", "3"))

    def test_max_autonomous_tasks_per_hour(self):
        """Rate limit for autonomous task spawning."""
        from app.shared_state import runtime_config
        assert runtime_config["max_autonomous_tasks_per_hour"] == 5

    def test_max_rollup_retries_default(self):
        """Default max_rollup_retries must be 5 — controls rollup PR creation retry ceiling."""
        import os
        from app.shared_state import runtime_config
        assert runtime_config["max_rollup_retries"] == int(os.getenv("MAX_ROLLUP_RETRIES", "5"))

    def test_provision_slots_geq_concurrent_vms(self):
        """MAX_PROVISION_SLOTS must be >= MAX_CONCURRENT_VMS default.

        If this fails: the provision slot limit (MAX_PROVISION_SLOTS env var) is
        lower than the concurrent VM limit (MAX_CONCURRENT_VMS env var), which
        creates a dispatch deadlock — slots would be reserved by active VMs before
        provisioning can complete.
        """
        from app.compute import _MAX_GLOBAL_PROVISIONS
        from app.shared_state import runtime_config
        max_concurrent_vms = int(os.getenv("MAX_CONCURRENT_VMS", "8"))
        assert _MAX_GLOBAL_PROVISIONS >= max_concurrent_vms, (
            f"MAX_PROVISION_SLOTS ({_MAX_GLOBAL_PROVISIONS}) < MAX_CONCURRENT_VMS ({max_concurrent_vms}) — "
            "provision slot limit must be >= concurrent VM limit to avoid dispatch deadlock."
        )


# ── Security Scanner Integrity ──────────────────────────────────────────────

class TestSecurityIntegrity:
    def test_injection_pattern_count(self):
        """Security scanner must have at least 20 injection patterns.
        If this count drops, an agent may have silently removed patterns."""
        from app.security import INJECTION_PATTERNS
        assert len(INJECTION_PATTERNS) >= 20, (
            f"Only {len(INJECTION_PATTERNS)} injection patterns compiled — "
            "expected at least 20. Patterns may have been removed."
        )

    def test_raw_pattern_categories(self):
        """All critical categories must be present."""
        from app.security import INJECTION_PATTERNS
        categories = {p["category"] for p in INJECTION_PATTERNS}
        required = {"instruction_override", "role_injection", "boundary_marker", "prompt_leak"}
        missing = required - categories
        assert not missing, f"Missing security pattern categories: {missing}"

    def test_high_severity_patterns_exist(self):
        """Must have high-severity patterns for critical injection types."""
        from app.security import INJECTION_PATTERNS
        high_patterns = [p for p in INJECTION_PATTERNS if p["severity"] == "high"]
        assert len(high_patterns) >= 10, (
            f"Only {len(high_patterns)} high-severity patterns — expected at least 10"
        )


# ── Deployment State Model ──────────────────────────────────────────────────

class TestDeploymentStateModel:
    def test_deployment_status_values(self):
        """DeploymentStatus enum must have all expected states."""
        from app.models import DeploymentStatus
        expected = {"MONITORING", "STABLE", "ROLLED_BACK", "PAUSED", "HALTED"}
        actual = {s.value for s in DeploymentStatus}
        assert expected == actual, f"DeploymentStatus values changed: expected {expected}, got {actual}"


# ── Agent Model Forward Compatibility ───────────────────────────────────────

class TestAgentModels:
    def test_agent_models_ignore_extra(self):
        """Agent-facing Pydantic models must accept unknown fields (forward compat)."""
        from app.schemas import AgentStatusPayload, HeartbeatRequest, SpawnTaskRequest
        for model_cls in [AgentStatusPayload, HeartbeatRequest, SpawnTaskRequest]:
            config = model_cls.model_config
            assert config.get("extra") == "ignore", (
                f"{model_cls.__name__} must have extra='ignore' for forward compatibility"
            )


# ── CLAUDE.md Agent Restrictions ────────────────────────────────────────────

def _find_claude_md() -> str:
    """Find CLAUDE.md — either copied into Docker image root or at repo root."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "CLAUDE.md"),       # /app/CLAUDE.md (Docker, copied by CI)
        os.path.join(os.path.dirname(__file__), "..", "..", "CLAUDE.md"), # repo root (local dev)
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""

_CLAUDE_MD = _find_claude_md()


class TestAgentRestrictions:
    def test_claude_md_exists(self):
        """CLAUDE.md must exist in the repo root."""
        assert _CLAUDE_MD, "CLAUDE.md not found — expected at repo root or copied into Docker image by CI"

    def test_claude_md_restricts_workflows(self):
        """CLAUDE.md must restrict agents from modifying CI workflows."""
        assert _CLAUDE_MD, "CLAUDE.md not found"
        with open(_CLAUDE_MD) as f:
            content = f.read()
        assert ".github/workflows" in content, (
            "CLAUDE.md must mention .github/workflows/ as a restricted path"
        )

    def test_claude_md_restricts_alembic(self):
        """CLAUDE.md must restrict agents from modifying migrations."""
        assert _CLAUDE_MD, "CLAUDE.md not found"
        with open(_CLAUDE_MD) as f:
            content = f.read()
        assert "alembic" in content.lower(), (
            "CLAUDE.md must mention alembic as a restricted path"
        )


# -- Direction Lifecycle Regression Guards --

class TestDirectionLifecycleInvariants:
    def test_max_active_directions_default(self):
        """max_active_directions must remain at 10 — controls direction sprawl."""
        from app.shared_state import runtime_config
        assert runtime_config["max_active_directions"] == 10, (
            f"max_active_directions changed to {runtime_config['max_active_directions']} — "
            "this controls direction sprawl and must not change without approval."
        )

    def test_direction_text_limits(self):
        """DIRECTION_TEXT_MAX and COMPLETION_CRITERIA_MAX must match spec."""
        from app.routers.directions import DIRECTION_TEXT_MAX, COMPLETION_CRITERIA_MAX
        assert DIRECTION_TEXT_MAX == 2000, (
            f"DIRECTION_TEXT_MAX changed to {DIRECTION_TEXT_MAX} — expected 2000."
        )
        assert COMPLETION_CRITERIA_MAX == 500, (
            f"COMPLETION_CRITERIA_MAX changed to {COMPLETION_CRITERIA_MAX} — expected 500."
        )

    def test_momentum_factor_contract(self):
        """_compute_momentum_factor must return canonical values for known inputs."""
        from app.research_completion import _compute_momentum_factor
        # Hot streak: last 2 cycles productive → 2.0
        hot = {"research_history": [{"tasks_created": 3}, {"tasks_created": 2}]}
        assert _compute_momentum_factor(hot) == 2.0, (
            f"Hot streak returned {_compute_momentum_factor(hot)}, expected 2.0"
        )
        # Stalled: last 3 cycles empty → 0.5
        stalled = {"research_history": [{"tasks_created": 0}, {"tasks_created": 0}, {"tasks_created": 0}]}
        assert _compute_momentum_factor(stalled) == 0.5, (
            f"Stalled returned {_compute_momentum_factor(stalled)}, expected 0.5"
        )
        # Neutral: no history → 1.0
        assert _compute_momentum_factor({}) == 1.0, (
            f"Neutral returned {_compute_momentum_factor({})}, expected 1.0"
        )

    def test_auto_completion_pattern_exists(self):
        """auto_completed flag must exist in research_completion.py — lost in PR #832."""
        rc_path = os.path.join(os.path.dirname(__file__), "..", "app", "research_completion.py")
        with open(rc_path) as f:
            content = f.read()
        assert 'auto_completed' in content, (
            "The 'auto_completed' string literal is missing from research_completion.py. "
            "Auto-completion logic was silently removed in PR #832 and must not be removed again."
        )

    def test_direction_cap_enforced_in_post(self):
        """directions.py POST endpoint must check max_active_directions cap — lost in PR #832."""
        directions_path = os.path.join(os.path.dirname(__file__), "..", "app", "routers", "directions.py")
        with open(directions_path) as f:
            content = f.read()
        assert 'max_active_directions' in content, (
            "'max_active_directions' cap check is missing from directions.py. "
            "Direction cap enforcement was lost in PR #832 and must not be removed again."
        )


# ── Module Size Guardrails ─────────────────────────────────────────────────
# These are loose upper bounds to catch runaway growth, not tight ceilings.
# If a file hits the limit, consider a refactor — but do NOT "ratchet down"
# the limit after every cleanup. These limits should stay stable.

MODULE_SIZE_LIMITS = {
    # (relative_path_from_app_dir, max_lines)
    "main.py": 3500,
    "task_completion.py": 2000,
    "task_lifecycle.py": 2000,
    "orchestrator.py": 3000,
    "admin.py": 3000,
    "compute.py": 2500,
    "github.py": 2000,
    "routers/admin_settings.py": 3000,
    "routers/admin_analytics.py": 2500,
    "routers/agent.py": 2000,
    "services.py": 1500,
    "routers/agent_status.py": 1500,
    "autonomous_research.py": 1500,
    "research_completion.py": 2000,
    "strategic_completion.py": 1500,
    "rollup_management.py": 1500,
    "reaper.py": 1000,
    "review_pipeline.py": 800,
    "deploy_health.py": 1000,
}


class TestBackendModuleSizeGuardrails:
    """Loose upper bounds to catch runaway file growth.

    These are NOT tight ceilings to ratchet down. They are generous limits
    that should only fire if a file has genuinely gotten out of hand.
    If a test fails, consider a refactor — but do NOT lower the limit afterward.
    """

    def _check_module(self, relative_path: str) -> None:
        max_lines = MODULE_SIZE_LIMITS[relative_path]
        path = os.path.join(os.path.dirname(__file__), "..", "app", relative_path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= max_lines, (
            f"{relative_path} has {count} lines (limit: {max_lines}). "
            "This file has grown large — consider refactoring into smaller modules."
        )

    def test_main_py(self):
        self._check_module("main.py")

    def test_task_completion(self):
        self._check_module("task_completion.py")

    def test_task_lifecycle(self):
        self._check_module("task_lifecycle.py")

    def test_orchestrator(self):
        self._check_module("orchestrator.py")

    def test_admin(self):
        self._check_module("admin.py")

    def test_compute(self):
        self._check_module("compute.py")

    def test_github(self):
        self._check_module("github.py")

    def test_admin_settings(self):
        self._check_module("routers/admin_settings.py")

    def test_admin_analytics(self):
        self._check_module("routers/admin_analytics.py")

    def test_agent_router(self):
        self._check_module("routers/agent.py")

    def test_services(self):
        self._check_module("services.py")

    def test_agent_status(self):
        self._check_module("routers/agent_status.py")

    def test_autonomous_research(self):
        self._check_module("autonomous_research.py")

    def test_research_completion(self):
        self._check_module("research_completion.py")

    def test_strategic_completion(self):
        self._check_module("strategic_completion.py")

    def test_rollup_management(self):
        self._check_module("rollup_management.py")

    def test_reaper(self):
        self._check_module("reaper.py")

    def test_review_pipeline(self):
        self._check_module("review_pipeline.py")

    def test_deploy_health(self):
        self._check_module("deploy_health.py")


# ── Frontend File Size Guardrails ─────────────────────────────────────────────

FRONTEND_SIZE_LIMITS = {
    "useAppState.ts": 3000,
    "components/DirectionsPanel.tsx": 3000,
    "components/CatchUpPanel.tsx": 2500,
    "components/TaskDetailDrawer.tsx": 2500,
    "components/SettingsDropdown.tsx": 2500,
    "components/KanbanBoard.tsx": 1500,
    "components/KanbanColumn.tsx": 1500,
    "App.tsx": 2000,
    "components/TaskCard.tsx": 1500,
    "components/DirectionCard.tsx": 1200,
    "components/PipelineView.tsx": 1200,
    "components/DashboardHome.tsx": 1200,
    "components/MessagesPanel.tsx": 1000,
}


def _get_frontend_path(relative: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "admin-ui", "src", relative)


class TestFrontendSizeGuardrails:
    """Loose upper bounds to catch runaway frontend file growth."""

    @staticmethod
    def _skip_if_missing() -> None:
        import pytest
        if not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "..", "admin-ui")):
            pytest.skip("admin-ui directory not present")

    def _check_frontend(self, relative_path: str) -> None:
        self._skip_if_missing()
        max_lines = FRONTEND_SIZE_LIMITS[relative_path]
        path = _get_frontend_path(relative_path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= max_lines, (
            f"{relative_path} has {count} lines (limit: {max_lines}). "
            "This file has grown large — consider extracting sub-components or hooks."
        )

    def test_useappstate(self):
        self._check_frontend("useAppState.ts")

    def test_directions_panel(self):
        self._check_frontend("components/DirectionsPanel.tsx")

    def test_catchup_panel(self):
        self._check_frontend("components/CatchUpPanel.tsx")

    def test_task_detail_drawer(self):
        self._check_frontend("components/TaskDetailDrawer.tsx")

    def test_settings_dropdown(self):
        self._check_frontend("components/SettingsDropdown.tsx")

    def test_kanban_board(self):
        self._check_frontend("components/KanbanBoard.tsx")

    def test_kanban_column(self):
        self._check_frontend("components/KanbanColumn.tsx")

    def test_app_tsx(self):
        self._check_frontend("App.tsx")

    def test_task_card(self):
        self._check_frontend("components/TaskCard.tsx")

    def test_direction_card(self):
        self._check_frontend("components/DirectionCard.tsx")

    def test_pipeline_view(self):
        self._check_frontend("components/PipelineView.tsx")

    def test_dashboard_home(self):
        self._check_frontend("components/DashboardHome.tsx")

    def test_messages_panel(self):
        self._check_frontend("components/MessagesPanel.tsx")
