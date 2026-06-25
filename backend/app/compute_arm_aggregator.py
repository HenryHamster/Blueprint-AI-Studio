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


def aggregate(
    arms: list[tuple[str, str]],
    declared_metric: str,
    output_path: str,
) -> dict:
    """Read per-arm result files and write the combined arms block.

    Returns the written dict so callers can inspect without re-reading the file.
    """
    arms_block: dict = {}
    for name, path in arms:
        try:
            data = json.loads(Path(path).read_text())
            value = data.get(declared_metric)
            if value is None:
                arms_block[name] = {
                    "error": f"metric '{declared_metric}' not found in result"
                }
            else:
                arms_block[name] = {declared_metric: value}
        except FileNotFoundError:
            arms_block[name] = {
                "error": "arm result file not written (arm may have failed)"
            }
        except Exception as exc:
            arms_block[name] = {"error": str(exc)}

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
    args = parser.parse_args()

    arm_pairs: list[tuple[str, str]] = []
    for spec in args.arms:
        colon_idx = spec.index(":")
        arm_pairs.append((spec[:colon_idx], spec[colon_idx + 1 :]))

    aggregate(arm_pairs, args.declared_metric, args.output)


if __name__ == "__main__":
    main()
