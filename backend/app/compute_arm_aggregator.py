#!/usr/bin/env python3
"""Per-arm result aggregator for Blueprint compute experiments.

Called by the synthesised arm-isolation bash wrapper after all arms complete.
Reads each arm's per-arm result file, extracts the declared_metric value, and
writes {"arms": {arm_name: {declared_metric: value}}} to the main result file.

One arm failing does not suppress other arms: a missing or unreadable result
file is recorded as {"error": "..."} for that arm, while successfully captured
arms still appear with their metric value.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_STDERR_MAX_BYTES = 2048


def _read_exit_code(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return None


def _read_elapsed_seconds(path: str) -> float | None:
    try:
        val = int(Path(path).read_text().strip())
        return float(val) if val >= 0 else None
    except Exception:  # noqa: BLE001
        return None


def _read_stderr_excerpt(path: str) -> str | None:
    try:
        data = Path(path).read_bytes()
        if not data:
            return None
        tail = data[-_STDERR_MAX_BYTES:]
        return tail.decode(errors="replace").strip() or None
    except Exception:
        return None


def _failure_reason(
    exit_code: int | None,
    declared_metric: str,
    *,
    result_missing: bool,
    metric_missing: bool,
) -> str:
    if exit_code == 124:
        return "arm timed out (exit 124)"
    if result_missing:
        if exit_code is not None and exit_code != 0:
            return f"arm crashed (exit {exit_code}): result file not written"
        return "arm result file not written (arm may have failed)"
    if metric_missing:
        if exit_code is not None and exit_code != 0:
            return (
                f"arm crashed (exit {exit_code}): "
                f"metric '{declared_metric}' not found in result"
            )
        return f"unmeasured criterion: metric '{declared_metric}' not found in result"
    return "unknown failure"


def aggregate(
    arms: list[tuple[str, str]],
    declared_metric: str,
    output_path: str,
    *,
    exit_codes: dict[str, str] | None = None,
    stderr_files: dict[str, str] | None = None,
    durations: dict[str, str] | None = None,
) -> dict:
    """Read per-arm result files and write the combined arms block.

    Returns the written dict so callers can inspect without re-reading the file.

    Optional exit_codes and stderr_files map arm name to the path of the
    corresponding capture file.  When provided, each arm entry gains an
    exit_code field and, on failure, a stderr_excerpt and human-readable
    reason that distinguishes crash / timeout / unmeasured-criterion.

    Optional durations maps arm name to the path of the elapsed-seconds file
    written by the wrapper.  When provided, each arm entry gains an
    elapsed_seconds field for both successful and failed arms so that the
    backend can apportion total cost by wall-clock fraction.
    """
    arms_block: dict = {}
    for name, path in arms:
        ec_path = (exit_codes or {}).get(name)
        se_path = (stderr_files or {}).get(name)
        dur_path = (durations or {}).get(name)
        exit_code = _read_exit_code(ec_path) if ec_path else None
        elapsed = _read_elapsed_seconds(dur_path) if dur_path else None

        result_missing = False
        metric_missing = False
        data: dict = {}

        try:
            data = json.loads(Path(path).read_text())
            value = data.get(declared_metric)
            if value is None:
                metric_missing = True
            else:
                entry: dict = {declared_metric: value}
                if exit_code is not None:
                    entry["exit_code"] = exit_code
                if elapsed is not None:
                    entry["elapsed_seconds"] = elapsed
                arms_block[name] = entry
                continue
        except FileNotFoundError:
            result_missing = True
        except Exception as exc:
            arms_block[name] = {"error": str(exc)}
            if exit_code is not None:
                arms_block[name]["exit_code"] = exit_code
            if elapsed is not None:
                arms_block[name]["elapsed_seconds"] = elapsed
            continue

        reason = _failure_reason(
            exit_code,
            declared_metric,
            result_missing=result_missing,
            metric_missing=metric_missing,
        )
        entry = {"error": reason}
        if exit_code is not None:
            entry["exit_code"] = exit_code
        if se_path:
            excerpt = _read_stderr_excerpt(se_path)
            if excerpt:
                entry["stderr_excerpt"] = excerpt
        if elapsed is not None:
            entry["elapsed_seconds"] = elapsed
        arms_block[name] = entry

    result = {"arms": arms_block}
    Path(output_path).write_text(json.dumps(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate Blueprint compute experiment per-arm results."
    )
    parser.add_argument(
        "--arms",
        nargs="+",
        required=True,
        metavar="NAME:PATH",
        help="Colon-separated arm name and result file path pairs.",
    )
    parser.add_argument(
        "--declared-metric",
        required=True,
        help="The metric key each arm writes to its result file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the aggregated result JSON.",
    )
    parser.add_argument(
        "--exit-codes",
        nargs="+",
        metavar="NAME:PATH",
        default=None,
        help="Colon-separated arm name and exit code file path pairs.",
    )
    parser.add_argument(
        "--stderr",
        nargs="+",
        metavar="NAME:PATH",
        default=None,
        help="Colon-separated arm name and stderr capture file path pairs.",
    )
    parser.add_argument(
        "--durations",
        nargs="+",
        metavar="NAME:PATH",
        default=None,
        help="Colon-separated arm name and elapsed-seconds file path pairs.",
    )
    args = parser.parse_args()

    def _parse_specs(specs: list[str] | None) -> dict[str, str] | None:
        if not specs:
            return None
        result: dict[str, str] = {}
        for spec in specs:
            colon_idx = spec.index(":")
            result[spec[:colon_idx]] = spec[colon_idx + 1 :]
        return result

    arm_pairs: list[tuple[str, str]] = []
    for spec in args.arms:
        colon_idx = spec.index(":")
        arm_pairs.append((spec[:colon_idx], spec[colon_idx + 1 :]))

    aggregate(
        arm_pairs,
        args.declared_metric,
        args.output,
        exit_codes=_parse_specs(args.exit_codes),
        stderr_files=_parse_specs(args.stderr),
        durations=_parse_specs(args.durations),
    )


if __name__ == "__main__":
    main()
