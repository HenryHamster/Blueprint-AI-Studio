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
        from app.task_lifecycle import MAX_REVIEW_FIX_ITERATIONS
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


# ── VM Self-Destruct Timer ──────────────────────────────────────────────────

class TestVMSafety:
    def test_vm_self_destruct_timer(self):
        """GCP VMs must have a 1-hour self-destruct timer (TTL_SECONDS=3600)."""
        compute_path = os.path.join(os.path.dirname(__file__), "..", "app", "compute.py")
        with open(compute_path) as f:
            content = f.read()
        assert "TTL_SECONDS=3600" in content, (
            "VM self-destruct timer (TTL_SECONDS=3600) missing from compute.py startup script. "
            "This prevents runaway VMs from accumulating costs."
        )

    def test_vm_poweroff_present(self):
        """Startup script must end with poweroff."""
        compute_path = os.path.join(os.path.dirname(__file__), "..", "app", "compute.py")
        with open(compute_path) as f:
            content = f.read()
        assert "poweroff" in content, "VM startup script must include poweroff for self-termination"


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
        """max_active_directions must remain at 5 — controls direction sprawl."""
        from app.shared_state import runtime_config
        assert runtime_config["max_active_directions"] == 5, (
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


# ── main.py Size and Symbol Count Regression Guard ──────────────────────────
# Direction d-3d050b18: no new functions added to main.py; all new functionality
# goes in extracted modules.

MAIN_PY_MAX_LINES = 2500      # Ratchet down as extractions land
MAIN_PY_MAX_FUNCTIONS = 40    # Ratchet down as extractions land
MAIN_PY_MAX_EXTERNAL_IMPORTS = 15  # Ratchet down as import redirects land


def _get_main_py_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "app", "main.py")


class TestMainPyFreeze:
    """Enforce the main.py freeze: no new functions, no line growth."""

    def test_main_py_line_count(self):
        """main.py must not grow beyond the ceiling.

        If this fails: new functionality was added to main.py.
        New code must go in an extracted module (e.g. backend/app/<domain>.py).
        Once a line-reducing extraction lands, ratchet MAIN_PY_MAX_LINES down.
        """
        main_path = _get_main_py_path()
        with open(main_path) as f:
            lines = f.readlines()
        count = len(lines)
        assert count <= MAIN_PY_MAX_LINES, (
            f"main.py has {count} lines, exceeding the ceiling of {MAIN_PY_MAX_LINES}. "
            "New functionality must go in extracted modules, not main.py. "
            "See direction d-3d050b18."
        )

    def test_main_py_function_count(self):
        """main.py must not gain new functions.

        If this fails: a new function was added to main.py.
        New functions must be added to an appropriate extracted module instead.
        Once a function-removing extraction lands, ratchet MAIN_PY_MAX_FUNCTIONS down.
        """
        import ast
        main_path = _get_main_py_path()
        with open(main_path) as f:
            source = f.read()
        tree = ast.parse(source)
        func_count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        assert func_count <= MAIN_PY_MAX_FUNCTIONS, (
            f"main.py has {func_count} functions, exceeding the ceiling of {MAIN_PY_MAX_FUNCTIONS}. "
            "New functions must go in extracted modules, not main.py. "
            "See direction d-3d050b18."
        )

    def test_main_py_external_import_count(self):
        """Other backend modules must not import more symbols from app.main than the ceiling.

        If this fails: a new import from app.main was added elsewhere.
        As extractions re-export symbols from their new modules, update imports at
        the call sites to point to the extracted module and ratchet
        MAIN_PY_MAX_EXTERNAL_IMPORTS down.
        """
        import ast
        import glob

        app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
        main_path = os.path.realpath(_get_main_py_path())

        imported_names: set = set()
        for path in glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True):
            if os.path.realpath(path) == main_path:
                continue
            try:
                with open(path) as f:
                    tree = ast.parse(f.read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in ("app.main", "main"):
                    for alias in node.names:
                        imported_names.add(alias.name)

        count = len(imported_names)
        assert count <= MAIN_PY_MAX_EXTERNAL_IMPORTS, (
            f"{count} unique symbols are imported from app.main by other modules, "
            f"exceeding the ceiling of {MAIN_PY_MAX_EXTERNAL_IMPORTS}. "
            "Update import sites to point to the extracted module instead of app.main. "
            "See direction d-3d050b18."
        )


# ── Orchestrator Module Size Guards ─────────────────────────────────────────
# Direction d-51edc526: proactive orchestrator decomposition ratchet

TASK_COMPLETION_MAX_LINES = 950      # Ratcheted after anomaly/research/strategic extraction
TASK_COMPLETION_MAX_FUNCTIONS = 15   # Ratcheted after anomaly/research/strategic extraction

TASK_LIFECYCLE_MAX_LINES = 2100      # Ratchet down as extractions land
TASK_LIFECYCLE_MAX_FUNCTIONS = 12    # Ratchet down as extractions land

ORCHESTRATOR_MAX_LINES = 1450        # Current 1,373 + ~5% headroom
ORCHESTRATOR_MAX_FUNCTIONS = 10      # Current 6 + 4 slots


def _get_task_completion_py_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "app", "task_completion.py")


def _get_task_lifecycle_py_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "app", "task_lifecycle.py")


def _get_orchestrator_py_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "app", "orchestrator.py")


class TestTaskCompletionFreeze:
    """Enforce the task_completion.py freeze: no new functions, no line growth."""

    def test_task_completion_line_count(self):
        """task_completion.py must not grow beyond the ceiling.

        If this fails: new functionality was added to task_completion.py.
        New functionality must go in extracted modules (e.g., anomaly_detection.py,
        research_completion.py). Once a line-reducing extraction lands, ratchet
        the ceiling down.
        """
        path = _get_task_completion_py_path()
        with open(path) as f:
            lines = f.readlines()
        count = len(lines)
        assert count <= TASK_COMPLETION_MAX_LINES, (
            f"task_completion.py has {count} lines, exceeding the ceiling of "
            f"{TASK_COMPLETION_MAX_LINES}. "
            "New functionality must go in extracted modules (e.g., anomaly_detection.py, "
            "research_completion.py). Once a line-reducing extraction lands, ratchet the "
            "ceiling down."
        )

    def test_task_completion_function_count(self):
        """task_completion.py must not gain new functions.

        If this fails: a new function was added to task_completion.py.
        New functionality must go in extracted modules (e.g., anomaly_detection.py,
        research_completion.py). Once a line-reducing extraction lands, ratchet the
        ceiling down.
        """
        import ast
        path = _get_task_completion_py_path()
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        func_count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        assert func_count <= TASK_COMPLETION_MAX_FUNCTIONS, (
            f"task_completion.py has {func_count} functions, exceeding the ceiling of "
            f"{TASK_COMPLETION_MAX_FUNCTIONS}. "
            "New functionality must go in extracted modules (e.g., anomaly_detection.py, "
            "research_completion.py). Once a line-reducing extraction lands, ratchet the "
            "ceiling down."
        )


class TestTaskLifecycleFreeze:
    """Enforce the task_lifecycle.py freeze: no new functions, no line growth."""

    def test_task_lifecycle_line_count(self):
        """task_lifecycle.py must not grow beyond the ceiling.

        If this fails: new functionality was added to task_lifecycle.py.
        New functionality must go in extracted modules (e.g., review_pipeline.py).
        Once a line-reducing extraction lands, ratchet the ceiling down.
        """
        path = _get_task_lifecycle_py_path()
        with open(path) as f:
            lines = f.readlines()
        count = len(lines)
        assert count <= TASK_LIFECYCLE_MAX_LINES, (
            f"task_lifecycle.py has {count} lines, exceeding the ceiling of "
            f"{TASK_LIFECYCLE_MAX_LINES}. "
            "New functionality must go in extracted modules (e.g., review_pipeline.py). "
            "Once a line-reducing extraction lands, ratchet the ceiling down."
        )

    def test_task_lifecycle_function_count(self):
        """task_lifecycle.py must not gain new functions.

        If this fails: a new function was added to task_lifecycle.py.
        New functionality must go in extracted modules (e.g., review_pipeline.py).
        Once a line-reducing extraction lands, ratchet the ceiling down.
        """
        import ast
        path = _get_task_lifecycle_py_path()
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        func_count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        assert func_count <= TASK_LIFECYCLE_MAX_FUNCTIONS, (
            f"task_lifecycle.py has {func_count} functions, exceeding the ceiling of "
            f"{TASK_LIFECYCLE_MAX_FUNCTIONS}. "
            "New functionality must go in extracted modules (e.g., review_pipeline.py). "
            "Once a line-reducing extraction lands, ratchet the ceiling down."
        )


# ── Autonomous Research Module Size Guards ───────────────────────────────────
# Direction d-3d050b18: autonomous research spawning extracted from main.py

AUTONOMOUS_RESEARCH_MAX_LINES = 1100   # Ratchet down as extractions land
AUTONOMOUS_RESEARCH_MAX_FUNCTIONS = 12  # Ratchet down as extractions land


def _get_autonomous_research_py_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "app", "autonomous_research.py")


class TestAutonomousResearchFreeze:
    """Enforce the autonomous_research.py freeze: no new functions, no line growth."""

    def test_autonomous_research_line_count(self):
        """autonomous_research.py must not grow beyond the ceiling.

        If this fails: new functionality was added to autonomous_research.py.
        New functionality must go in extracted modules.
        Once a line-reducing extraction lands, ratchet the ceiling down.
        """
        path = _get_autonomous_research_py_path()
        with open(path) as f:
            lines = f.readlines()
        count = len(lines)
        assert count <= AUTONOMOUS_RESEARCH_MAX_LINES, (
            f"autonomous_research.py has {count} lines, exceeding the ceiling of "
            f"{AUTONOMOUS_RESEARCH_MAX_LINES}. "
            "New functionality must go in extracted modules. "
            "Once a line-reducing extraction lands, ratchet the ceiling down."
        )

    def test_autonomous_research_function_count(self):
        """autonomous_research.py must not gain new functions.

        If this fails: a new function was added to autonomous_research.py.
        New functionality must go in an appropriate extracted module instead.
        Once a function-removing extraction lands, ratchet the ceiling down.
        """
        import ast
        path = _get_autonomous_research_py_path()
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        func_count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        assert func_count <= AUTONOMOUS_RESEARCH_MAX_FUNCTIONS, (
            f"autonomous_research.py has {func_count} functions, exceeding the ceiling of "
            f"{AUTONOMOUS_RESEARCH_MAX_FUNCTIONS}. "
            "New functionality must go in extracted modules. "
            "Once a function-removing extraction lands, ratchet the ceiling down."
        )


# ── Deploy Health Module Size Guards ────────────────────────────────────────
# Direction d-3d050b18: deploy health functions extracted from main.py

DEPLOY_HEALTH_MAX_LINES = 490
DEPLOY_HEALTH_MAX_FUNCTIONS = 10


def _get_deploy_health_py_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "app", "deploy_health.py")


class TestDeployHealthFreeze:
    """Enforce the deploy_health.py freeze: no new functions, no line growth."""

    def test_deploy_health_line_count(self):
        """deploy_health.py must not grow beyond the ceiling.

        If this fails: new functionality was added to deploy_health.py.
        New functionality must go in extracted modules.
        Once a line-reducing extraction lands, ratchet the ceiling down.
        """
        path = _get_deploy_health_py_path()
        with open(path) as f:
            lines = f.readlines()
        count = len(lines)
        assert count <= DEPLOY_HEALTH_MAX_LINES, (
            f"deploy_health.py has {count} lines, exceeding the ceiling of "
            f"{DEPLOY_HEALTH_MAX_LINES}. "
            "New functionality must go in extracted modules. "
            "Once a line-reducing extraction lands, ratchet the ceiling down."
        )

    def test_deploy_health_function_count(self):
        """deploy_health.py must not gain new functions.

        If this fails: a new function was added to deploy_health.py.
        New functionality must go in an appropriate extracted module instead.
        Once a function-removing extraction lands, ratchet the ceiling down.
        """
        import ast
        path = _get_deploy_health_py_path()
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        func_count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        assert func_count <= DEPLOY_HEALTH_MAX_FUNCTIONS, (
            f"deploy_health.py has {func_count} functions, exceeding the ceiling of "
            f"{DEPLOY_HEALTH_MAX_FUNCTIONS}. "
            "New functionality must go in extracted modules. "
            "Once a function-removing extraction lands, ratchet the ceiling down."
        )


# ── Frontend File Size Guards ────────────────────────────────────────────────
# Enforces line count ceilings on the largest frontend files to prevent unbounded growth.

USEAPPSTATE_MAX_LINES = 1500
DIRECTIONS_PANEL_MAX_LINES = 1500
CATCHUP_PANEL_MAX_LINES = 1200
TASK_DETAIL_DRAWER_MAX_LINES = 1200
SETTINGS_DROPDOWN_MAX_LINES = 1200
KANBAN_BOARD_MAX_LINES = 1100
APP_TSX_MAX_LINES = 1000


def _get_frontend_path(relative: str) -> str:
    """Resolve a path relative to admin-ui/src/ from the test file location."""
    return os.path.join(os.path.dirname(__file__), "..", "..", "admin-ui", "src", relative)


class TestFrontendFreeze:
    """Enforce line count ceilings on the largest frontend files."""

    @staticmethod
    def _skip_if_missing(path: str) -> None:
        import pytest
        if not os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "..", "admin-ui")):
            pytest.skip("admin-ui directory not present — skipping frontend size guards")

    def test_useappstate_line_count(self):
        """useAppState.ts must not grow beyond the ceiling.

        If this fails: new state was added to useAppState.ts.
        Extract into a standalone hook under admin-ui/src/hooks/ instead.
        """
        path = _get_frontend_path("useAppState.ts")
        self._skip_if_missing(path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= USEAPPSTATE_MAX_LINES, (
            f"useAppState.ts has {count} lines, exceeding the ceiling of {USEAPPSTATE_MAX_LINES}. "
            "Extract into a standalone hook under admin-ui/src/hooks/ instead."
        )

    def test_directions_panel_line_count(self):
        """DirectionsPanel.tsx must not grow beyond the ceiling.

        If this fails: new UI was added to DirectionsPanel.tsx.
        Extract into a sub-component under admin-ui/src/components/ instead.
        """
        path = _get_frontend_path("components/DirectionsPanel.tsx")
        self._skip_if_missing(path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= DIRECTIONS_PANEL_MAX_LINES, (
            f"DirectionsPanel.tsx has {count} lines, exceeding the ceiling of {DIRECTIONS_PANEL_MAX_LINES}. "
            "Extract into a sub-component or hook instead."
        )

    def test_catchup_panel_line_count(self):
        """CatchUpPanel.tsx must not grow beyond the ceiling.

        If this fails: new UI was added to CatchUpPanel.tsx.
        Extract into a sub-component or hook instead.
        """
        path = _get_frontend_path("components/CatchUpPanel.tsx")
        self._skip_if_missing(path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= CATCHUP_PANEL_MAX_LINES, (
            f"CatchUpPanel.tsx has {count} lines, exceeding the ceiling of {CATCHUP_PANEL_MAX_LINES}. "
            "Extract into a sub-component or hook instead."
        )

    def test_task_detail_drawer_line_count(self):
        """TaskDetailDrawer.tsx must not grow beyond the ceiling.

        If this fails: new UI was added to TaskDetailDrawer.tsx.
        Extract into a sub-component or hook instead.
        """
        path = _get_frontend_path("components/TaskDetailDrawer.tsx")
        self._skip_if_missing(path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= TASK_DETAIL_DRAWER_MAX_LINES, (
            f"TaskDetailDrawer.tsx has {count} lines, exceeding the ceiling of {TASK_DETAIL_DRAWER_MAX_LINES}. "
            "Extract into a sub-component or hook instead."
        )

    def test_settings_dropdown_line_count(self):
        """SettingsDropdown.tsx must not grow beyond the ceiling.

        If this fails: new UI was added to SettingsDropdown.tsx.
        Extract into a sub-component or hook instead.
        """
        path = _get_frontend_path("components/SettingsDropdown.tsx")
        self._skip_if_missing(path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= SETTINGS_DROPDOWN_MAX_LINES, (
            f"SettingsDropdown.tsx has {count} lines, exceeding the ceiling of {SETTINGS_DROPDOWN_MAX_LINES}. "
            "Extract into a sub-component or hook instead."
        )

    def test_kanban_board_line_count(self):
        """KanbanBoard.tsx must not grow beyond the ceiling.

        If this fails: new UI was added to KanbanBoard.tsx.
        Extract into a sub-component or hook instead.
        """
        path = _get_frontend_path("components/KanbanBoard.tsx")
        self._skip_if_missing(path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= KANBAN_BOARD_MAX_LINES, (
            f"KanbanBoard.tsx has {count} lines, exceeding the ceiling of {KANBAN_BOARD_MAX_LINES}. "
            "Extract into a sub-component or hook instead."
        )

    def test_app_tsx_line_count(self):
        """App.tsx must not grow beyond the ceiling.

        If this fails: new UI was added to App.tsx.
        Extract into a sub-component or hook instead.
        """
        path = _get_frontend_path("App.tsx")
        self._skip_if_missing(path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= APP_TSX_MAX_LINES, (
            f"App.tsx has {count} lines, exceeding the ceiling of {APP_TSX_MAX_LINES}. "
            "Extract into a sub-component or hook instead."
        )


class TestOrchestratorFreeze:
    """Enforce the orchestrator.py freeze: no new functions, no line growth."""

    def test_orchestrator_line_count(self):
        """orchestrator.py must not grow beyond the ceiling.

        If this fails: new functionality was added to orchestrator.py.
        New functionality must go in extracted modules. The orchestrator_worker
        function (1,257 lines) is the primary extraction candidate.
        Once a line-reducing extraction lands, ratchet the ceiling down.
        """
        path = _get_orchestrator_py_path()
        with open(path) as f:
            lines = f.readlines()
        count = len(lines)
        assert count <= ORCHESTRATOR_MAX_LINES, (
            f"orchestrator.py has {count} lines, exceeding the ceiling of "
            f"{ORCHESTRATOR_MAX_LINES}. "
            "New functionality must go in extracted modules. "
            "Once a line-reducing extraction lands, ratchet the ceiling down."
        )

    def test_orchestrator_function_count(self):
        """orchestrator.py must not gain new functions.

        If this fails: a new function was added to orchestrator.py.
        New functionality must go in extracted modules.
        Once a line-reducing extraction lands, ratchet the ceiling down.
        """
        import ast
        path = _get_orchestrator_py_path()
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        func_count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        assert func_count <= ORCHESTRATOR_MAX_FUNCTIONS, (
            f"orchestrator.py has {func_count} functions, exceeding the ceiling of "
            f"{ORCHESTRATOR_MAX_FUNCTIONS}. "
            "New functionality must go in extracted modules. "
            "Once a line-reducing extraction lands, ratchet the ceiling down."
        )
