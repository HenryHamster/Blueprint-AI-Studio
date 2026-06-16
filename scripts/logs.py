"""DEPRECATED — use ``python scripts/blueprint.py logs`` instead.

This script is fully superseded by the ``logs`` subcommand in scripts/blueprint.py:

    python scripts/blueprint.py logs errors  [--minutes N] [--limit N]
    python scripts/blueprint.py logs tail    [--minutes N] [--limit N] [--severity LEVEL]
    python scripts/blueprint.py logs search <pattern> [--minutes N] [--limit N]
    python scripts/blueprint.py logs task   <task_id>  [--minutes N]
"""
import sys

COMMANDS: dict = {}

if __name__ == "__main__":
    print(
        "DEPRECATED: scripts/logs.py is superseded by scripts/blueprint.py.\n"
        "Use: python scripts/blueprint.py logs {errors|tail|search|task} ..."
    )
    sys.exit(1)
