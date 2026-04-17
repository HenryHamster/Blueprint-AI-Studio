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


# ── Schema Revision Sync ───────────────────────────────────────────────────

class TestSchemaRevisionSync:
    def test_expected_revision_matches_latest_migration(self):
        """EXPECTED_SCHEMA_REVISION must match the latest alembic migration.

        If this fails, you added a migration but forgot to bump
        EXPECTED_SCHEMA_REVISION in main.py. The orchestrator will crash
        on startup with a schema version mismatch.
        """
        import glob as globmod
        from app.main import EXPECTED_SCHEMA_REVISION

        migrations_dir = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))
        assert files, "No migration files found"

        latest = max(os.path.basename(f).split("_")[0] for f in files)
        assert EXPECTED_SCHEMA_REVISION == latest, (
            f"EXPECTED_SCHEMA_REVISION is '{EXPECTED_SCHEMA_REVISION}' but latest "
            f"migration is '{latest}'. Bump EXPECTED_SCHEMA_REVISION in main.py."
        )

    def test_migration_filenames_match_internal_revision(self):
        """Each migration file's internal `revision = '...'` must match its
        filename prefix. Alembic orders migrations by the internal revision,
        so a mismatch means the filename is misleading about ordering.
        """
        import glob as globmod
        import re

        migrations_dir = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))
        rev_re = re.compile(r"^revision\s*=\s*['\"](\w+)['\"]", re.MULTILINE)
        mismatches: list[str] = []
        for path in files:
            filename_rev = os.path.basename(path).split("_")[0]
            with open(path) as f:
                content = f.read()
            match = rev_re.search(content)
            if not match:
                mismatches.append(f"{os.path.basename(path)}: no revision field found")
                continue
            internal_rev = match.group(1)
            if internal_rev != filename_rev:
                mismatches.append(
                    f"{os.path.basename(path)}: filename says {filename_rev}, "
                    f"internal revision says {internal_rev}"
                )
        assert not mismatches, "Migration revision mismatches:\n  " + "\n  ".join(mismatches)

    def test_migration_chain_is_linear(self):
        """The alembic migration chain must be a single linear sequence —
        no gaps, no branches, no dangling down_revision references.
        """
        import glob as globmod
        import re

        migrations_dir = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))
        rev_re = re.compile(r"^revision\s*=\s*['\"](\w+)['\"]", re.MULTILINE)
        # Match either down_revision = '0033' or down_revision = None
        down_re = re.compile(r"^down_revision\s*=\s*(?:['\"](\w+)['\"]|None)", re.MULTILINE)

        chain: dict[str, str | None] = {}  # revision -> down_revision
        for path in files:
            with open(path) as f:
                content = f.read()
            rev_match = rev_re.search(content)
            down_match = down_re.search(content)
            if rev_match:
                # group(1) is the quoted revision string or None if matched 'None' literal
                down = down_match.group(1) if down_match and down_match.group(1) else None
                chain[rev_match.group(1)] = down

        # Every down_revision must point to a revision that exists (or None for the root)
        missing_parents = [
            f"{rev} → {down} (parent doesn't exist)"
            for rev, down in chain.items()
            if down is not None and down not in chain
        ]
        assert not missing_parents, "Broken migration chain:\n  " + "\n  ".join(missing_parents)

        # Exactly one root (down_revision=None) and one head (no one points to it)
        roots = [r for r, d in chain.items() if d is None]
        assert len(roots) == 1, f"Expected exactly 1 root migration, found {len(roots)}: {roots}"

        pointed_to = {d for d in chain.values() if d is not None}
        heads = [r for r in chain if r not in pointed_to]
        assert len(heads) == 1, f"Expected exactly 1 head migration, found {len(heads)}: {heads}"

    def test_model_columns_covered_by_migrations(self):
        """Every column defined on a SQLModel table must exist in the alembic
        migration chain. If an agent adds a column to models.py but forgets
        the migration, Cloud Run startup will crash with UndefinedColumnError.

        Scans all migration files for add_column calls and column definitions
        in create_table, and checks each model's columns against them.
        """
        import glob as globmod
        import re
        from sqlmodel import SQLModel

        # Import models so SQLModel registers all tables
        from app import models  # noqa: F401

        migrations_dir = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))

        # Collect all (table, column) pairs present in migrations
        migration_columns: set[tuple[str, str]] = set()
        add_col_re = re.compile(r"add_column\(\s*['\"](\w+)['\"]\s*,\s*sa\.Column\(\s*['\"](\w+)['\"]")
        create_table_re = re.compile(r"create_table\(\s*['\"](\w+)['\"]", re.MULTILINE)
        col_in_create_re = re.compile(r"sa\.Column\(\s*['\"](\w+)['\"]")
        # Renames: alter_column('table', 'old', new_column_name='new') → new column exists
        rename_re = re.compile(
            r"alter_column\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"].*?new_column_name\s*=\s*['\"](\w+)['\"]",
            re.DOTALL,
        )

        for path in files:
            with open(path) as f:
                content = f.read()

            # add_column('table', sa.Column('name', ...))
            for m in add_col_re.finditer(content):
                migration_columns.add((m.group(1), m.group(2)))

            # alter_column('table', 'old_name', new_column_name='new_name', ...)
            for m in rename_re.finditer(content):
                migration_columns.add((m.group(1), m.group(3)))

            # create_table('table', sa.Column('name', ...), ...)
            for ct in create_table_re.finditer(content):
                table = ct.group(1)
                # Find the matching closing paren for this create_table call
                start = ct.end()
                depth = 1
                end = start
                while end < len(content) and depth > 0:
                    if content[end] == '(':
                        depth += 1
                    elif content[end] == ')':
                        depth -= 1
                    end += 1
                block = content[start:end]
                for col_m in col_in_create_re.finditer(block):
                    migration_columns.add((table, col_m.group(1)))

        # Check each SQLModel table's columns against migrations
        missing: list[tuple[str, str]] = []
        for table_name, table in SQLModel.metadata.tables.items():
            for col in table.columns:
                if (table_name, col.name) not in migration_columns:
                    missing.append((table_name, col.name))

        assert not missing, (
            f"Columns defined on models but missing from alembic migrations:\n"
            + "\n".join(f"  - {t}.{c}" for t, c in missing)
            + "\n\nAdd a migration for each missing column, or the backend will "
            "crash on startup with UndefinedColumnError."
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
    "task_lifecycle.py": 2050,
    "orchestrator.py": 3000,
    "admin.py": 3000,
    "compute.py": 2500,
    "github.py": 2000,
    "routers/admin_settings.py": 3000,
    "routers/admin_analytics.py": 3200,
    "routers/agent.py": 2000,
    "services.py": 1500,
    "routers/agent_status.py": 1500,
    "autonomous_research.py": 1500,
    "research_completion.py": 2000,
    "strategic_completion.py": 1500,
    "rollup_management.py": 1550,
    "reaper.py": 1020,
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


# ── Failure Classification Taxonomy ────────────────────────────────────────


class TestFailureClassificationInvariants:
    """Structural invariants over failure_classification canonical classes.

    INFRA_CLASSES governs which failures are treated as infrastructure (not
    penalizing direction health). direction_outcomes and research_completion
    both import it, so accidental additions/removals silently change rollup
    and dedup behavior across the pipeline.
    """

    def test_infra_classes_membership_exact(self):
        from app.failure_classification import (
            AGENT_TIMEOUT,
            EXTERNAL_API,
            INFRA_CLASSES,
            PROVISION_FAILURE,
            RATE_LIMIT,
            SCOPE_OVERFLOW,
        )
        assert INFRA_CLASSES == frozenset({
            PROVISION_FAILURE,
            RATE_LIMIT,
            AGENT_TIMEOUT,
            EXTERNAL_API,
            SCOPE_OVERFLOW,
        }), (
            "INFRA_CLASSES membership changed — direction_outcomes and "
            "research_completion rely on this exact set to distinguish infra "
            "failures from code failures."
        )

    def test_canonical_classes_partitioned_by_infra(self):
        from app.failure_classification import (
            AGENT_TIMEOUT,
            CODE_ERROR,
            EXTERNAL_API,
            INFRA_CLASSES,
            PROVISION_FAILURE,
            RATE_LIMIT,
            SCOPE_OVERFLOW,
        )
        canonical = [
            CODE_ERROR,
            PROVISION_FAILURE,
            AGENT_TIMEOUT,
            EXTERNAL_API,
            RATE_LIMIT,
            SCOPE_OVERFLOW,
        ]
        for cls in canonical:
            assert isinstance(cls, str) and cls, f"{cls!r} must be a non-empty string"
            assert cls in INFRA_CLASSES or cls == CODE_ERROR, (
                f"{cls!r} must be either CODE_ERROR or a member of INFRA_CLASSES"
            )
