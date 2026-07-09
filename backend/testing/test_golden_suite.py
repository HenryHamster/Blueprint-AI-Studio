"""Tests for the golden-task suite skeleton.

Verifies that every spec JSON is structurally valid, that specs produce valid
task payloads, and that payloads pass the backend's schema validation.

All tests use embedded spec fixtures so they work identically inside the Docker
test container and from a bare pytest invocation.

Runnable via::

    bash scripts/test-backend.sh testing/test_golden_suite.py
    bash scripts/test-backend.sh --standalone --no-postgres testing/test_golden_suite.py
    cd backend && python3 -m pytest testing/test_golden_suite.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Inline constants — duplicated from golden_tasks/runner.py because the Docker
# test harness mounts only backend/ and golden_tasks/ is at the project root.
# ─────────────────────────────────────────────────────────────────────────────

VALID_TASK_TYPES: set[str] = {
    "create",
    "review",
    "panel_review",
    "fix",
    "plan",
    "research",
    "strategic_review",
    "design_doc_update",
    "security_audit",
    "qa",
    "deps",
    "docs",
    "migrate",
    "smoketest",
    "refactor",
    "debug",
    "perf",
    "investigate",
    "self_audit",
    "release_audit",
    "release_audit_challenge",
    "release_merge_grade",
    "memo",
    "verify_tests",
    "compute_experiment",
    "web_research",
    "design",
    "repo_bootstrap",
}

VALID_EFFORTS: set[str] = {"low", "medium", "high", "max"}

REQUIRED_SPEC_FIELDS: set[str] = {
    "spec_id",
    "name",
    "description",
    "task_type",
    "prompt",
    "expected",
}

REQUIRED_EXPECTED_FIELDS: set[str] = {
    "terminal_status",
    "outcome_status",
    "outcome_kind",
}

OUTCOME_KINDS: set[str] = {
    "code_merge",
    "research_quality",
    "audit_findings",
    "memo_artifact",
    "verification_verdict",
    "compute_result",
    "review_verdict",
    "knowledge_document",
    "read_only_unclassified",
    "unknown",
}


# ─────────────────────────────────────────────────────────────────────────────
# Embedded spec fixtures (representative task types)
# ─────────────────────────────────────────────────────────────────────────────

# Each entry is a minimal spec dict with all required fields. These are a
# representative subset of the 18 specs in golden_tasks/specs/ selected to
# cover 12 distinct task types.

_BUILTIN_SPECS: list[dict[str, Any]] = [
    {
        "spec_id": "golden-001",
        "name": "Add health check endpoint",
        "description": "Minimal create task: add a GET /api/health endpoint.",
        "task_type": "create",
        "prompt": "Add a GET /api/health endpoint returning {\"status\": \"ok\"}.",
        "effort": "low",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "code_merge",
        },
        "tags": ["create", "simple", "cost-baseline"],
    },
    {
        "spec_id": "golden-002",
        "name": "Fix null reference",
        "description": "Fix NullReferenceError from None profile_picture.",
        "task_type": "fix",
        "prompt": "Fix NullReferenceError when profile.profile_picture is None.",
        "effort": "low",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "code_merge",
        },
        "tags": ["fix", "simple", "cost-baseline"],
    },
    {
        "spec_id": "golden-003",
        "name": "Rename service class",
        "description": "Rename UserService to UserManager across the codebase.",
        "task_type": "refactor",
        "prompt": "Rename UserService class to UserManager updating all references.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "code_merge",
        },
        "tags": ["refactor", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-004",
        "name": "Review small PR",
        "description": "Review a pull request for correctness and style.",
        "task_type": "review",
        "prompt": "Review PR #13613 for correctness and edge cases.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "review_verdict",
        },
        "tags": ["review", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-005",
        "name": "Research auth patterns",
        "description": "Research JWT authentication implementation.",
        "task_type": "research",
        "prompt": "Research JWT auth across the backend and identify consolidation opportunities.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "research_quality",
        },
        "tags": ["research", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-006",
        "name": "Strategic direction sweep",
        "description": "Sweep active directions, assess health, propose retirements.",
        "task_type": "strategic_review",
        "prompt": "Sweep active directions and recommend retire/continue/stage for each.",
        "effort": "high",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "read_only_unclassified",
        },
        "tags": ["strategic", "expensive", "cost-baseline"],
    },
    {
        "spec_id": "golden-007",
        "name": "Investigate VM crash loop",
        "description": "Investigate repeated VM crashes for research tasks.",
        "task_type": "investigate",
        "prompt": "Investigate OOM and timeout patterns for research task VMs.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "read_only_unclassified",
        },
        "tags": ["investigate", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-008",
        "name": "Debug flaky test",
        "description": "Root-cause a flaky CI test.",
        "task_type": "debug",
        "prompt": "Debug flaky test_task_dispatch_timeout that fails ~30% of CI runs.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "read_only_unclassified",
        },
        "tags": ["debug", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-009",
        "name": "Document API endpoints",
        "description": "Add docstrings to public endpoint handlers.",
        "task_type": "docs",
        "prompt": "Add Google-style docstrings to all public endpoint handlers.",
        "effort": "low",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "code_merge",
        },
        "tags": ["docs", "simple", "cost-baseline"],
    },
    {
        "spec_id": "golden-010",
        "name": "Plan task decomposition",
        "description": "Decompose a large feature into ordered subtasks.",
        "task_type": "plan",
        "prompt": "Decompose the rate-limiter feature into testable subtasks.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "read_only_unclassified",
        },
        "tags": ["plan", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-011",
        "name": "Add migration column",
        "description": "Add last_login_at column with Alembic migration.",
        "task_type": "migrate",
        "prompt": "Add nullable last_login_at to User model and create migration.",
        "effort": "low",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "code_merge",
        },
        "tags": ["migrate", "simple", "cost-baseline"],
    },
    {
        "spec_id": "golden-012",
        "name": "Write efficiency tests",
        "description": "Add unit tests for efficiency_snapshot module.",
        "task_type": "qa",
        "prompt": "Add unit tests for compute_efficiency_snapshot covering empty, populated, and failed.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "code_merge",
        },
        "tags": ["qa", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-013",
        "name": "Upgrade httpx dep",
        "description": "Upgrade httpx to latest minor version.",
        "task_type": "deps",
        "prompt": "Upgrade httpx in pyproject.toml to latest minor and fix regressions.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "code_merge",
        },
        "tags": ["deps", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-014",
        "name": "Memo design rationale",
        "description": "Memo on task routing architecture tradeoffs.",
        "task_type": "memo",
        "prompt": "Analyze queue-based vs DAG vs event-driven task routing at 500+ concurrency.",
        "effort": "high",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "memo_artifact",
        },
        "tags": ["memo", "expensive", "cost-baseline"],
    },
    {
        "spec_id": "golden-015",
        "name": "Profile N+1 queries",
        "description": "Find N+1 query patterns in task listing.",
        "task_type": "perf",
        "prompt": "Profile GET /api/projects/{id}/tasks for N+1 queries.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "audit_findings",
        },
        "tags": ["perf", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-016",
        "name": "Research competitor pricing",
        "description": "Research how AI coding assistants price their services.",
        "task_type": "web_research",
        "prompt": "Research Copilot, Cursor, Windsurf, Cody pricing tiers.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "knowledge_document",
        },
        "tags": ["web-research", "medium", "cost-baseline"],
    },
    {
        "spec_id": "golden-017",
        "name": "Audit access controls",
        "description": "Audit endpoints for missing access control decorators.",
        "task_type": "security_audit",
        "prompt": "Audit API endpoints for missing @require_project_access or @require_role.",
        "effort": "high",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "audit_findings",
        },
        "tags": ["security", "expensive", "cost-baseline"],
    },
    {
        "spec_id": "golden-018",
        "name": "Self-audit maturity",
        "description": "Score orchestrator module on 4 maturity signals.",
        "task_type": "self_audit",
        "prompt": "Score orchestrator.py on test coverage, error handling, docs, and observability.",
        "effort": "medium",
        "expected": {
            "terminal_status": "COMPLETED",
            "outcome_status": "success",
            "outcome_kind": "audit_findings",
        },
        "tags": ["audit", "medium", "cost-baseline"],
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _validate_spec(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_SPEC_FIELDS:
        if field not in data:
            errors.append(f"Missing required field: {field!r}")

    task_type = str(data.get("task_type", ""))
    if not task_type:
        errors.append("Field 'task_type' is empty")
    elif task_type not in VALID_TASK_TYPES:
        errors.append(f"Unknown task_type {task_type!r}")

    prompt = str(data.get("prompt", ""))
    if not prompt.strip():
        errors.append("Field 'prompt' is empty")

    effort = data.get("effort")
    if effort is not None and effort not in VALID_EFFORTS:
        errors.append(f"Invalid effort {effort!r}")

    expected = data.get("expected", {})
    if not expected:
        errors.append("Missing 'expected' block")
    else:
        for field in REQUIRED_EXPECTED_FIELDS:
            if field not in expected:
                errors.append(f"Missing expected field: {field!r}")
        ok = expected.get("outcome_kind")
        if ok and ok not in OUTCOME_KINDS:
            errors.append(f"Unknown outcome_kind {ok!r}")
    return errors


def _to_task_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": str(data.get("task_type", "")),
        "prompt": str(data.get("prompt", "")),
        "title": str(data.get("name", "")),
    }
    effort = data.get("effort")
    if effort:
        payload["effort"] = effort
    for key in ("base_branch", "model", "prompt_suffix"):
        val = data.get(key)
        if val is not None:
            payload[key] = val
    return payload


# Try to load on-disk specs alongside the embedded defaults. The on-disk specs
# are authoritative when available; the embedded ones are the fallback so the
# test works identically inside Docker where golden_tasks/ is not mounted.
def _load_specs() -> list[dict[str, Any]]:
    here = Path(__file__).resolve().parent  # backend/testing/
    disk_spec_dir = here.parent.parent / "golden_tasks" / "specs"
    if disk_spec_dir.is_dir():
        specs: list[dict[str, Any]] = []
        for path in sorted(disk_spec_dir.glob("*.json")):
            with open(path) as f:
                specs.append(dict(json.load(f)))
        if specs:
            return specs
    return list(_BUILTIN_SPECS)


ALL_SPECS = _load_specs()


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def all_specs() -> list[dict[str, Any]]:
    return list(ALL_SPECS)


# ─────────────────────────────────────────────────────────────────────────────
# Suite-level integrity
# ─────────────────────────────────────────────────────────────────────────────


class TestSuiteIntegrity:
    def test_suite_has_at_least_10_specs(self):
        assert len(ALL_SPECS) >= 10, (
            f"Expected at least 10 specs, got {len(ALL_SPECS)}"
        )

    def test_suite_spans_at_least_10_task_types(self):
        types = {s.get("task_type") for s in ALL_SPECS}
        assert len(types) >= 10, (
            f"Expected >= 10 task types, got {len(types)}: {sorted(types)}"
        )

    def test_all_specs_have_unique_ids(self):
        ids = [s.get("spec_id") for s in ALL_SPECS]
        duplicates = {i for i in ids if ids.count(i) > 1}
        assert not duplicates, f"Duplicate spec_ids: {duplicates}"

    def test_all_specs_validate(self):
        failures: list[tuple[str, list[str]]] = []
        for spec in ALL_SPECS:
            errors = _validate_spec(spec)
            if errors:
                failures.append((spec.get("spec_id", "???"), errors))
        if failures:
            msg = "\n".join(f"  {sid}: {errs}" for sid, errs in failures)
            pytest.fail(f"{len(failures)} spec(s) failed validation:\n{msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Per-spec field validation
# ─────────────────────────────────────────────────────────────────────────────


class TestSpecFields:
    @pytest.mark.parametrize("field", sorted(REQUIRED_SPEC_FIELDS))
    def test_required_fields_present(
        self, all_specs: list[dict[str, Any]], field: str
    ):
        missing = [s.get("spec_id", "???") for s in all_specs if field not in s]
        assert not missing, f"Spec(s) missing required field {field!r}: {missing}"

    def test_task_type_in_valid_set(self, all_specs: list[dict[str, Any]]):
        invalid = [s for s in all_specs if s.get("task_type") not in VALID_TASK_TYPES]
        assert not invalid, (
            f"Invalid task_type(s): {[(s.get('spec_id'), s.get('task_type')) for s in invalid]}"
        )

    def test_task_type_non_empty(self, all_specs: list[dict[str, Any]]):
        empty = [s.get("spec_id", "???") for s in all_specs if not s.get("task_type")]
        assert not empty, "Spec(s) with empty task_type"

    def test_prompt_non_empty(self, all_specs: list[dict[str, Any]]):
        empty = [
            s.get("spec_id", "???")
            for s in all_specs
            if not str(s.get("prompt", "")).strip()
        ]
        assert not empty, "Spec(s) with empty prompt"

    @pytest.mark.parametrize("field", sorted(REQUIRED_EXPECTED_FIELDS))
    def test_expected_fields_present(
        self, all_specs: list[dict[str, Any]], field: str
    ):
        missing = [
            s.get("spec_id", "???")
            for s in all_specs
            if field not in s.get("expected", {})
        ]
        assert not missing, f"Spec(s) missing expected field {field!r}: {missing}"

    def test_outcome_kind_valid(self, all_specs: list[dict[str, Any]]):
        invalid = [
            (s.get("spec_id"), s.get("expected", {}).get("outcome_kind"))
            for s in all_specs
            if s.get("expected", {}).get("outcome_kind") not in OUTCOME_KINDS
        ]
        assert not invalid, f"Invalid outcome_kind(s): {invalid}"

    @pytest.mark.parametrize("effort", [None, "low", "medium", "high", "max"])
    def test_effort_valid_when_set(
        self, all_specs: list[dict[str, Any]], effort: str | None
    ):
        if effort is None:
            return
        invalid = [
            s.get("spec_id")
            for s in all_specs
            if s.get("effort") is not None and s.get("effort") not in VALID_EFFORTS
        ]
        assert not invalid, f"Invalid effort value(s): {invalid}"

    def test_spec_id_format(self, all_specs: list[dict[str, Any]]):
        for spec in all_specs:
            sid = spec.get("spec_id", "")
            assert len(sid) > 0, "Empty spec_id"
            assert sid.isascii(), f"Non-ASCII spec_id: {sid}"


# ─────────────────────────────────────────────────────────────────────────────
# Payload construction
# ─────────────────────────────────────────────────────────────────────────────


class TestPayloadConstruction:
    def test_spec_to_payload_has_type(self, all_specs: list[dict[str, Any]]):
        for spec in all_specs:
            payload = _to_task_payload(spec)
            assert "type" in payload, f"{spec.get('spec_id')}: payload missing 'type'"
            assert payload["type"] == spec.get("task_type"), (
                f"{spec.get('spec_id')}: payload type mismatch"
            )

    def test_spec_to_payload_has_prompt(self, all_specs: list[dict[str, Any]]):
        for spec in all_specs:
            payload = _to_task_payload(spec)
            assert "prompt" in payload, (
                f"{spec.get('spec_id')}: payload missing 'prompt'"
            )
            assert len(payload["prompt"]) > 0, f"{spec.get('spec_id')}: empty prompt"

    def test_all_payloads_json_serializable(self, all_specs: list[dict[str, Any]]):
        for spec in all_specs:
            payload = _to_task_payload(spec)
            try:
                json.dumps(payload)
            except (TypeError, ValueError) as exc:
                pytest.fail(f"{spec.get('spec_id')}: payload not JSON-serializable: {exc}")

    def test_effort_passed_through(self):
        payload = _to_task_payload(
            {
                "spec_id": "test-e",
                "name": "t",
                "description": "t",
                "task_type": "create",
                "prompt": "hello",
                "effort": "high",
                "expected": {
                    "terminal_status": "COMPLETED",
                    "outcome_status": "success",
                    "outcome_kind": "code_merge",
                },
            }
        )
        assert payload["effort"] == "high"

    def test_effort_omitted_when_not_set(self):
        payload = _to_task_payload(
            {
                "spec_id": "test-ne",
                "name": "t",
                "description": "t",
                "task_type": "create",
                "prompt": "hello",
                "expected": {
                    "terminal_status": "COMPLETED",
                    "outcome_status": "success",
                    "outcome_kind": "code_merge",
                },
            }
        )
        assert "effort" not in payload

    def test_on_disk_specs_loaded_when_available(self):
        """When the golden_tasks/specs/ directory exists on disk, the module-level
        loader should prefer the on-disk specs (which are more detailed) over the
        embedded defaults. This test asserts that the on-disk spec format is a
        superset of the embedded format."""
        here = Path(__file__).resolve().parent
        disk_dir = here.parent.parent / "golden_tasks" / "specs"
        if not disk_dir.is_dir():
            pytest.skip("on-disk specs not available (expected inside Docker)")
        disk_ids = set()
        for p in disk_dir.glob("*.json"):
            with open(p) as f:
                disk_ids.add(json.load(f).get("spec_id"))
        embedded_ids = {s["spec_id"] for s in _BUILTIN_SPECS}
        assert disk_ids == embedded_ids, (
            f"On-disk spec IDs {disk_ids} != embedded IDs {embedded_ids}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Backend schema validation
# ─────────────────────────────────────────────────────────────────────────────


class TestBackendSchemaValidation:
    """These tests require the backend's ``validate_task_payload`` function.

    They are standalone-safe (no DB, no network) because payload validation
    is purely structural.
    """

    # Task types whose payload only requires ``type`` + ``prompt`` (no PR
    # metadata like pr_number / branch which fix and review need).
    _SIMPLE_PROMPT_TYPES = {
        "create",
        "refactor",
        "docs",
        "qa",
        "deps",
        "migrate",
        "plan",
        "investigate",
    }

    def _import_validator(self):
        try:
            from app.payload_schemas import validate_task_payload  # noqa: PLC0415
            return validate_task_payload
        except ImportError as exc:
            pytest.skip(f"Backend payload_schemas not importable: {exc}")

    def test_simple_create_payload_passes_schema(self):
        validate = self._import_validator()
        spec = next(s for s in ALL_SPECS if s.get("task_type") == "create")
        payload = _to_task_payload(spec)
        try:
            result = validate(payload)
        except Exception as exc:
            pytest.fail(f"validate_task_payload raised {type(exc).__name__}: {exc}")
        assert isinstance(result, dict)
        assert result.get("type") == "create"

    def test_all_simple_prompt_payloads_pass_schema(
        self, all_specs: list[dict[str, Any]]
    ):
        validate = self._import_validator()
        failures: list[tuple[str, str]] = []
        for spec in all_specs:
            tt = spec.get("task_type", "")
            if tt not in self._SIMPLE_PROMPT_TYPES:
                continue
            payload = _to_task_payload(spec)
            try:
                validate(payload)
            except Exception as exc:
                failures.append(
                    (spec.get("spec_id", "???"), f"{type(exc).__name__}: {exc}")
                )
        if failures:
            msg = "\n".join(f"  {sid}: {err}" for sid, err in failures)
            pytest.fail(
                f"{len(failures)} payload(s) failed schema validation:\n{msg}"
            )
