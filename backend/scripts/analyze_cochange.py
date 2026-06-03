"""Backend-relative shim for the co-change analyzer script.

The repo's changed-file lint helper runs from ``backend/`` and feeds diff paths
directly to Ruff after stripping only a leading ``backend/`` prefix. When a
root-level Python script such as ``scripts/analyze_cochange.py`` is in the diff,
that helper checks ``backend/scripts/analyze_cochange.py``. Keep this shim so
the path exists and delegates to the canonical root-level entrypoint.
"""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(
        Path(__file__).resolve().parents[2] / "scripts" / "analyze_cochange.py",
        run_name="__main__",
    )
