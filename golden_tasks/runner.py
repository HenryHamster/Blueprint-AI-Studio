"""Golden-task suite runner.

Loads a golden-task spec, builds the corresponding task payload, and (in
``--run`` mode) dispatches it against the configured model/provider, collecting
cost and timing metrics.

Usage::

    # Validate one spec
    python3 golden_tasks/runner.py --validate specs/golden-001-create-health-check.json

    # Validate all specs
    python3 golden_tasks/runner.py --validate --all

    # List available specs
    python3 golden_tasks/runner.py --list-specs

    # Run a spec (future — dispatches to orchestrator or local agent)
    python3 golden_tasks/runner.py --run specs/golden-001.json

    # Run all specs and collect metrics
    python3 golden_tasks/runner.py --run --all --report /tmp/golden_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Valid task types — keep in sync with backend/app/task_capabilities.py
# ---------------------------------------------------------------------------
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

# Where this runner's spec files live
_HERE = Path(__file__).resolve().parent
DEFAULT_SPEC_DIR = _HERE / "specs"


# ---------------------------------------------------------------------------
# Spec model
# ---------------------------------------------------------------------------


class GoldenSpec:
    """A loaded and validated golden-task specification."""

    def __init__(self, data: dict[str, Any], source: str = "<unknown>") -> None:
        self._data = data
        self.source = source

    @property
    def spec_id(self) -> str:
        return str(self._data.get("spec_id", ""))

    @property
    def name(self) -> str:
        return str(self._data.get("name", ""))

    @property
    def task_type(self) -> str:
        return str(self._data.get("task_type", ""))

    @property
    def prompt(self) -> str:
        return str(self._data.get("prompt", ""))

    @property
    def effort(self) -> str | None:
        return self._data.get("effort")

    @property
    def tags(self) -> list[str]:
        return list(self._data.get("tags", []))

    @property
    def expected(self) -> dict[str, Any]:
        return dict(self._data.get("expected", {}))

    @property
    def raw(self) -> dict[str, Any]:
        return dict(self._data)

    def to_task_payload(self) -> dict[str, Any]:
        """Convert this spec to a task payload dict suitable for the backend.

        The returned dict can be passed to ``payload_schemas.validate_task_payload``
        for full schema-level validation.
        """
        payload: dict[str, Any] = {
            "type": self.task_type,
            "prompt": self.prompt,
            "title": self.name,
        }
        # Propagate effort when set
        effort = self.effort
        if effort:
            payload["effort"] = effort
        # Add other optional fields from raw spec
        for key in ("base_branch", "model", "prompt_suffix"):
            val = self._data.get(key)
            if val is not None:
                payload[key] = val
        return payload


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_spec(spec: GoldenSpec) -> list[str]:
    """Validate a golden-task spec, returning a list of error messages.

    Returns an empty list when the spec is fully valid.
    """
    errors: list[str] = []

    # Required top-level fields
    for field in REQUIRED_SPEC_FIELDS:
        if field not in spec._data:
            errors.append(f"Missing required field: {field!r}")

    # Task type
    task_type = spec.task_type
    if not task_type:
        errors.append("Field 'task_type' is empty")
    elif task_type not in VALID_TASK_TYPES:
        errors.append(
            f"Unknown task_type {task_type!r}. Valid: {', '.join(sorted(VALID_TASK_TYPES))}"
        )

    # Prompt must not be empty
    if not spec.prompt.strip():
        errors.append("Field 'prompt' is empty")

    # Effort must be valid when present
    effort = spec.effort
    if effort is not None and effort not in VALID_EFFORTS:
        errors.append(
            f"Invalid effort {effort!r}. Valid: {', '.join(VALID_EFFORTS)}"
        )

    # Expected block
    expected = spec.expected
    if not expected:
        errors.append("Missing 'expected' block")
    else:
        for field in REQUIRED_EXPECTED_FIELDS:
            if field not in expected:
                errors.append(f"Missing expected field: {field!r}")
        outcome_kind = expected.get("outcome_kind")
        if outcome_kind and outcome_kind not in OUTCOME_KINDS:
            errors.append(
                f"Unknown outcome_kind {outcome_kind!r}. "
                f"Valid: {', '.join(sorted(OUTCOME_KINDS))}"
            )

    return errors


def validate_all_specs(spec_dir: str | Path | None = None) -> dict[str, list[str]]:
    """Validate every spec in *spec_dir*, returning ``{spec_id: [errors]}``.

    Specs with an empty error list are valid.
    """
    spec_dir = Path(spec_dir or DEFAULT_SPEC_DIR)
    results: dict[str, list[str]] = {}
    for path in sorted(spec_dir.glob("*.json")):
        spec = load_spec(path)
        errors = validate_spec(spec)
        results[spec.spec_id] = errors
    return results


# ---------------------------------------------------------------------------
# Load / list
# ---------------------------------------------------------------------------


def load_spec(path: str | Path) -> GoldenSpec:
    """Load a single golden-task spec from a JSON file."""
    path = Path(path)
    with open(path, "r") as f:
        data: dict[str, Any] = json.load(f)
    return GoldenSpec(data, source=str(path))


def list_specs(spec_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """List available specs with summary metadata."""
    spec_dir = Path(spec_dir or DEFAULT_SPEC_DIR)
    specs: list[dict[str, Any]] = []
    for path in sorted(spec_dir.glob("*.json")):
        spec = load_spec(path)
        tags = spec.tags
        effort = spec.effort or "not-set"
        specs.append(
            {
                "spec_id": spec.spec_id,
                "name": spec.name,
                "task_type": spec.task_type,
                "effort": effort,
                "tags": tags,
                "file": str(path),
            }
        )
    return specs


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class RunMetrics:
    """Aggregated metrics collected during a spec replay."""

    def __init__(self) -> None:
        self.start_time: float = time.monotonic()
        self.end_time: float | None = None
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0
        self.cost_usd: float = 0.0
        self.success: bool | None = None  # None = not yet completed
        self.error: str | None = None
        self.llm_provider: str | None = None
        self.model_id: str | None = None

    @property
    def wall_time_s(self) -> float | None:
        if self.end_time is not None:
            return self.end_time - self.start_time
        return None

    def stop(self, success: bool = True) -> None:
        self.end_time = time.monotonic()
        self.success = success

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_time_s": self.wall_time_s,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "success": self.success,
            "error": self.error,
            "llm_provider": self.llm_provider,
            "model_id": self.model_id,
        }

    @staticmethod
    def aggregate(metrics_list: list[RunMetrics]) -> dict[str, Any]:
        """Aggregate multiple RunMetrics into summary statistics."""
        totals = {
            "count": len(metrics_list),
            "total_wall_time_s": 0.0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "success_count": 0,
            "fail_count": 0,
        }
        for m in metrics_list:
            if m.wall_time_s is not None:
                totals["total_wall_time_s"] += m.wall_time_s
            totals["total_prompt_tokens"] += m.prompt_tokens
            totals["total_completion_tokens"] += m.completion_tokens
            totals["total_tokens"] += m.total_tokens
            totals["total_cost_usd"] += m.cost_usd
            if m.success is True:
                totals["success_count"] += 1
            elif m.success is False:
                totals["fail_count"] += 1
        if totals["count"] > 0:
            totals["avg_wall_time_s"] = totals["total_wall_time_s"] / totals["count"]
            totals["avg_tokens"] = totals["total_tokens"] / totals["count"]
            totals["avg_cost_usd"] = totals["total_cost_usd"] / totals["count"]
            totals["success_rate"] = totals["success_count"] / totals["count"]
        return totals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Golden-task suite runner")
    parser.add_argument(
        "--spec-dir",
        default=str(DEFAULT_SPEC_DIR),
        help=f"Directory containing spec JSON files (default: {DEFAULT_SPEC_DIR})",
    )
    parser.add_argument(
        "--validate",
        nargs="?",
        const="<all>",
        default=None,
        metavar="SPEC_PATH",
        help="Validate one or all specs (pass individual path or omit for --all)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Apply operation (--validate or --run) to all specs",
    )
    parser.add_argument(
        "--list-specs",
        action="store_true",
        help="List available specs with metadata",
    )
    parser.add_argument(
        "--run",
        nargs="?",
        const="<all>",
        default=None,
        metavar="SPEC_PATH",
        help="Run a spec (or all with --all) against the configured provider",
    )
    parser.add_argument(
        "--report",
        default=None,
        metavar="FILE",
        help="Write run report JSON to FILE",
    )
    parser.add_argument(
        "--effort",
        default=None,
        choices=sorted(VALID_EFFORTS),
        help="Override effort level for --run",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override model ID for --run",
    )
    return parser


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate one or all specs."""
    spec_dir = Path(args.spec_dir)

    if args.all or args.validate == "<all>":
        results = validate_all_specs(spec_dir)
        total = len(results)
        errors_found = sum(1 for errs in results.values() if errs)
        print(f"Validated {total} specs — {total - errors_found} valid, {errors_found} with errors")
        for spec_id in sorted(results):
            errs = results[spec_id]
            if errs:
                print(f"  FAIL {spec_id}:")
                for e in errs:
                    print(f"    - {e}")
            else:
                print(f"  PASS {spec_id}")
        return 1 if errors_found > 0 else 0

    # Validate a single spec
    spec_path = Path(args.validate)
    if not spec_path.exists():
        print(f"ERROR: spec file not found: {spec_path}")
        return 1
    spec = load_spec(spec_path)
    errors = validate_spec(spec)
    if errors:
        print(f"FAIL {spec.spec_id} ({spec.name}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PASS {spec.spec_id} ({spec.name})")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Run one or all specs (future — dispatches to orchestrator)."""
    # TODO: implement actual dispatch to orchestrator or local agent runner.
    # For the skeleton phase, --run simulates a successful run with placeholder
    # metrics so the infrastructure can be tested end-to-end.
    spec_dir = Path(args.spec_dir)
    all_metrics: list[RunMetrics] = []

    if args.all or args.run == "<all>":
        specs = [load_spec(p) for p in sorted(spec_dir.glob("*.json"))]
    else:
        spec_path = Path(args.run)
        if not spec_path.exists():
            print(f"ERROR: spec file not found: {spec_path}")
            return 1
        specs = [load_spec(spec_path)]

    for spec in specs:
        print(f"[run] {spec.spec_id}: {spec.name}")
        metrics = RunMetrics()

        errors = validate_spec(spec)
        if errors:
            metrics.error = "; ".join(errors)
            metrics.stop(success=False)
            print(f"  SKIP (validation failed): {metrics.error}")
            all_metrics.append(metrics)
            continue

        # Build the task payload
        payload = spec.to_task_payload()

        # Override effort/model from CLI when provided
        if args.effort:
            payload["effort"] = args.effort
        if args.model:
            payload["model"] = args.model

        # ── Skeleton: simulate metrics instead of dispatching ──
        # In the future, this section would:
        #   1. Submit the task via the orchestrator API
        #   2. Poll for completion
        #   3. Collect real token/cost/wall-time data
        #   4. Check outcome against spec.expected
        time.sleep(0.05)  # simulate brief work
        metrics.prompt_tokens = 150
        metrics.completion_tokens = 450
        metrics.total_tokens = 600
        metrics.cost_usd = 0.015
        metrics.llm_provider = "anthropic"
        metrics.model_id = args.model or "claude-sonnet-4-20250514"

        metrics.stop(success=True)
        all_metrics.append(metrics)
        print(
            f"  DONE: {metrics.total_tokens} tokens, "
            f"${metrics.cost_usd:.4f}, "
            f"{metrics.wall_time_s:.2f}s"
        )

    # Report
    summary = RunMetrics.aggregate(all_metrics)
    print(f"\nSummary ({summary['count']} specs):")
    print(f"  Success rate: {summary['success_count']}/{summary['count']}")
    print(f"  Total tokens: {summary['total_tokens']}")
    print(f"  Total cost: ${summary['total_cost_usd']:.4f}")
    print(f"  Avg wall time: {summary['avg_wall_time_s']:.2f}s")

    if args.report:
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "specs": {s.spec_id: m.to_dict() for s, m in zip(specs, all_metrics)},
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2))
        print(f"  Report written to {report_path}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _setup_parser()
    args = parser.parse_args(argv)

    # Ensure spec_dir exists
    spec_dir = Path(args.spec_dir)
    if not spec_dir.is_dir():
        print(f"ERROR: spec directory not found: {spec_dir}")
        return 1

    # Dispatch
    if args.list_specs:
        specs = list_specs(spec_dir)
        print(f"Available golden-task specs ({len(specs)}):")
        print(f"{'ID':<30} {'Type':<20} {'Effort':<10} {'Name'}")
        print("-" * 80)
        for s in specs:
            tags_str = ",".join(s["tags"][:3])
            print(f"{s['spec_id']:<30} {s['task_type']:<20} {s['effort']:<10} {s['name']}")
        return 0

    if args.validate is not None:
        return _cmd_validate(args)

    if args.run is not None:
        return _cmd_run(args)

    # Default: list + validate
    print("Golden-task suite runner\n")
    specs = list_specs(spec_dir)
    print(f"Loaded {len(specs)} task specs")
    results = validate_all_specs(spec_dir)
    errors = sum(1 for errs in results.values() if errs)
    if errors:
        print(f"\n{errors} spec(s) have validation errors — run --validate --all for details")
        return 1
    print("All specs valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
