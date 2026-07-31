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

try:
    # When imported as a package member (e.g. via pytest), absolute import works.
    from golden_tasks.agent_runner import AgentBinaryNotFound, run_single_task
    _AGENT_RUNNER_AVAILABLE = True
except ImportError:
    try:
        # When run as a script (python3 golden_tasks/runner.py), the script
        # directory is on sys.path[0] and the package prefix won't resolve.
        from agent_runner import AgentBinaryNotFound, run_single_task  # type: ignore[no-redef]
        _AGENT_RUNNER_AVAILABLE = True
    except ImportError:
        _AGENT_RUNNER_AVAILABLE = False
        AgentBinaryNotFound = Exception  # type: ignore[assignment,misc]

        def run_single_task(*args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[misc,no-redef]
            """Stub — agent_runner module is not importable."""
            raise AgentBinaryNotFound("agent_runner module not available")

# ---------------------------------------------------------------------------
# Valid task types — keep in sync with backend/app/task_capabilities.py
# ---------------------------------------------------------------------------
VALID_TASK_TYPES: set[str] = {
    "create",
    "review",
    "fix",
    "plan",
    "research",
    "strategic_review",
    "discovery_review",
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
    "salvage_bundle",
    "capability_proof_trial",
    "criterion_coverage_trial",
    "walkthrough_e2e",
    "memory_consolidation",
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
        # Extended telemetry (populated by real agent execution)
        self.cache_read_input_tokens: int = 0
        self.cache_creation_input_tokens: int = 0
        self.llm_call_count: int = 0
        self.num_turns: int = 0
        # Simulated metrics are useful for dry runs, but must never be
        # mistaken for a provider-backed cost baseline.
        self.simulated: bool = False

    @property
    def wall_time_s(self) -> float | None:
        if self.end_time is not None:
            return self.end_time - self.start_time
        return None

    @property
    def cache_hit_rate(self) -> float | None:
        """Cache read as a fraction of total input tokens (0.0–1.0)."""
        if self.prompt_tokens > 0:
            return self.cache_read_input_tokens / self.prompt_tokens
        return None

    def stop(self, success: bool = True) -> None:
        self.end_time = time.monotonic()
        self.success = success

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "wall_time_s": self.wall_time_s,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "success": self.success,
            "error": self.error,
            "llm_provider": self.llm_provider,
            "model_id": self.model_id,
            "simulated": self.simulated,
        }
        if self.cache_read_input_tokens or self.cache_creation_input_tokens:
            d["cache_read_input_tokens"] = self.cache_read_input_tokens
            d["cache_creation_input_tokens"] = self.cache_creation_input_tokens
        if self.llm_call_count:
            d["llm_call_count"] = self.llm_call_count
        if self.num_turns:
            d["num_turns"] = self.num_turns
        if self.cache_hit_rate is not None:
            d["cache_hit_rate"] = round(self.cache_hit_rate, 4)
        return d

    @staticmethod
    def aggregate(metrics_list: list[RunMetrics]) -> dict[str, Any]:
        """Aggregate multiple RunMetrics into summary statistics."""
        totals: dict[str, Any] = {
            "count": len(metrics_list),
            "total_wall_time_s": 0.0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "success_count": 0,
            "fail_count": 0,
            "simulated_count": 0,
            "real_count": 0,
            "total_cache_read_input_tokens": 0,
            "total_cache_creation_input_tokens": 0,
            "total_llm_call_count": 0,
            "total_num_turns": 0,
        }
        providers: dict[str, int] = {}
        models: dict[str, int] = {}
        failure_categories: dict[str, int] = {}

        for m in metrics_list:
            if m.simulated:
                totals["simulated_count"] += 1
            else:
                totals["real_count"] += 1
            if m.wall_time_s is not None:
                totals["total_wall_time_s"] += m.wall_time_s
            totals["total_prompt_tokens"] += m.prompt_tokens
            totals["total_completion_tokens"] += m.completion_tokens
            totals["total_tokens"] += m.total_tokens
            totals["total_cost_usd"] += m.cost_usd
            totals["total_cache_read_input_tokens"] += m.cache_read_input_tokens
            totals["total_cache_creation_input_tokens"] += m.cache_creation_input_tokens
            totals["total_llm_call_count"] += m.llm_call_count
            totals["total_num_turns"] += m.num_turns
            # A fallback simulation is neither a real success nor a real
            # failure, so it cannot distort a provider-run success rate.
            if not m.simulated and m.success is True:
                totals["success_count"] += 1
            elif not m.simulated and m.success is False:
                totals["fail_count"] += 1
                # Extract failure category from error string
                if m.error and "failure_category=" in m.error:
                    cat = m.error.split("failure_category=")[1].split(" ")[0].split(")")[0]
                    failure_categories[cat] = failure_categories.get(cat, 0) + 1
            if m.llm_provider:
                providers[m.llm_provider] = providers.get(m.llm_provider, 0) + 1
            if m.model_id:
                models[m.model_id] = models.get(m.model_id, 0) + 1

        if totals["count"] > 0:
            totals["avg_wall_time_s"] = totals["total_wall_time_s"] / totals["count"]
            totals["avg_tokens"] = totals["total_tokens"] / totals["count"]
            totals["avg_cost_usd"] = totals["total_cost_usd"] / totals["count"]
            if totals["real_count"] > 0:
                totals["success_rate"] = totals["success_count"] / totals["real_count"]
            # Cache hit rate: cache_read / total_input across all specs
            total_input = totals["total_prompt_tokens"]
            if total_input > 0:
                totals["cache_hit_rate"] = round(
                    totals["total_cache_read_input_tokens"] / total_input, 4
                )
        if providers:
            totals["providers"] = providers
        if models:
            totals["models"] = models
        if failure_categories:
            totals["failure_categories"] = failure_categories

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
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="Per-spec timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--agent-path",
        default=None,
        metavar="PATH",
        help="Override agent binary path (sets BLUEPRINT_AGENT_BINARY)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip real execution, use simulated metrics (preserves existing behavior)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream agent stdout/stderr to the terminal",
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
    """Run one or all specs against the configured agent.

    In ``--run`` mode (the default without ``--dry-run``), dispatches each spec
    through the real agent via ``golden_tasks.agent_runner.run_single_task()``.
    When the agent binary or LLM credentials are unavailable, falls back
    gracefully to simulated metrics with a warning.

    ``--dry-run`` preserves the existing simulated behavior explicitly.
    """
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

        # ── Dispatch: real agent execution or simulated fallback ──
        if args.dry_run:
            _run_simulated(metrics, args)
        else:
            _run_real(metrics, payload, args)

        all_metrics.append(metrics)
        if metrics.simulated:
            print(
                f"  SIMULATED: {metrics.total_tokens} placeholder tokens, "
                f"${metrics.cost_usd:.4f} (excluded from real success rate)"
            )
        elif metrics.success is True:
            cache_str = ""
            if metrics.cache_hit_rate is not None:
                cache_str = f", cache_hit={metrics.cache_hit_rate:.1%}"
            print(
                f"  DONE: {metrics.total_tokens} tokens, "
                f"${metrics.cost_usd:.4f}, "
                f"{metrics.wall_time_s:.2f}s"
                f"{cache_str}"
            )
        elif metrics.success is False:
            print(f"  FAIL: {metrics.error or 'unknown error'}")
        else:
            print(f"  DONE (status unknown)")

    # Report
    summary = RunMetrics.aggregate(all_metrics)
    print(f"\nSummary ({summary['count']} specs):")
    print(f"  Success rate: {summary['success_count']}/{summary['real_count']} real specs")
    if summary["simulated_count"]:
        print(f"  Simulated specs: {summary['simulated_count']}")
    print(f"  Total tokens: {summary['total_tokens']}")
    print(f"  Total cost: ${summary['total_cost_usd']:.4f}")
    print(f"  Avg wall time: {summary['avg_wall_time_s']:.2f}s")
    if summary.get("cache_hit_rate") is not None:
        print(f"  Cache hit rate: {summary['cache_hit_rate']:.1%}")
    if summary.get("providers"):
        print(f"  Providers: {summary['providers']}")
    if summary.get("models"):
        print(f"  Models: {summary['models']}")
    if summary.get("failure_categories"):
        print(f"  Failure categories: {summary['failure_categories']}")

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


def _run_simulated(metrics: RunMetrics, args: argparse.Namespace) -> None:
    """Populate *metrics* with placeholder values (existing ``--dry-run`` behavior)."""
    metrics.simulated = True
    time.sleep(0.05)  # simulate brief work
    metrics.prompt_tokens = 150
    metrics.completion_tokens = 450
    metrics.total_tokens = 600
    metrics.cost_usd = 0.015
    metrics.llm_provider = "anthropic"
    metrics.model_id = args.model or "claude-sonnet-4-20250514"
    metrics.cache_read_input_tokens = 0
    metrics.cache_creation_input_tokens = 0
    metrics.llm_call_count = 1
    metrics.num_turns = 1
    metrics.stop(success=True)


def _run_real(
    metrics: RunMetrics,
    payload: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Dispatch *payload* through the real agent and populate *metrics*.

    Falls back to simulated metrics when the agent binary or LLM credentials
    are unavailable (prints a clear warning).
    """
    if not _AGENT_RUNNER_AVAILABLE:
        print(
            "  WARNING: agent_runner module not importable — "
            "falling back to simulated metrics. "
            "Ensure golden_tasks/agent_runner.py exists and is importable.",
        )
        _run_simulated(metrics, args)
        return

    try:
        result = run_single_task(
            payload,
            timeout_seconds=args.timeout,
            agent_path=args.agent_path,
            verbose=args.verbose,
        )
    except AgentBinaryNotFound as exc:
        msg = str(exc)
        if "LLM credentials" in msg:
            print(f"  WARNING: {msg}")
        else:
            print(
                f"  WARNING: Agent binary not found — falling back to simulated metrics.\n"
                f"    {msg}\n"
                f"    Install agent deps: cd agent && npm install\n"
                f"    Or set: export BLUEPRINT_AGENT_BINARY=/path/to/agent/entry"
            )
        _run_simulated(metrics, args)
        return
    except Exception as exc:
        print(f"  WARNING: Agent execution failed: {exc} — falling back to simulated metrics.")
        _run_simulated(metrics, args)
        return

    # Populate metrics from the result dict
    metrics.prompt_tokens = result.get("prompt_tokens", 0)
    metrics.completion_tokens = result.get("completion_tokens", 0)
    metrics.total_tokens = result.get("total_tokens", 0)
    metrics.cost_usd = result.get("cost_usd", 0.0)
    metrics.llm_provider = result.get("llm_provider")
    metrics.model_id = result.get("model_id")
    metrics.cache_read_input_tokens = result.get("cache_read_input_tokens", 0)
    metrics.cache_creation_input_tokens = result.get("cache_creation_input_tokens", 0)
    metrics.llm_call_count = result.get("llm_call_count", 0)
    metrics.num_turns = result.get("num_turns", 0)

    success = result.get("success", False)
    error = result.get("error")
    metrics.stop(success=bool(success))
    if error:
        metrics.error = str(error)
    if metrics.wall_time_s is not None:
        # Override with the real wall time from agent_runner if it differs
        reported_wall = result.get("wall_time_s")
        if reported_wall is not None and isinstance(reported_wall, (int, float)):
            # run_single_task returns wall_time_s; RunMetrics also computes it.
            # Use the reported value as canonical.
            metrics.end_time = metrics.start_time + float(reported_wall)


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
