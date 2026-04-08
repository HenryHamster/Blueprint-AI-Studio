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
        """MAX_REVIEW_FIX_ITERATIONS must stay at 8."""
        from app.main import MAX_REVIEW_FIX_ITERATIONS
        assert MAX_REVIEW_FIX_ITERATIONS == 8, (
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
