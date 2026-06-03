"""Backend-relative shim for the co-change analyzer script.

The repo's changed-file lint helper runs from ``backend/`` and feeds diff paths
directly to Ruff after stripping only a leading ``backend/`` prefix. When a
root-level Python script such as ``scripts/analyze_cochange.py`` is in the diff,
that helper checks ``backend/scripts/analyze_cochange.py``. Keep this shim so
the path exists and delegates to the canonical root-level entrypoint.
"""

import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[2] / "scripts" / "analyze_cochange.py"
    sys.path.insert(0, str(target.parent))
    try:
        runpy.run_path(target, run_name="__main__")
    finally:
        sys.path.pop(0)
