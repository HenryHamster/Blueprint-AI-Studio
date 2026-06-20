"""Compute-experiment measurement: structured-output self-repair success rate.

Measures repair-implementation quality by running the agent test suite for
``handlers/structured-output-repair.test.ts`` and reporting the test pass rate
as ``overall_success_rate``.

Compute-experiment command (run from repo root on the compute VM):
    python3 backend/scripts/measure_repair_success.py

Environment variables used:
    BLUEPRINT_RESULT_FILE — path where the metrics JSON is written (required by
        the compute-experiment substrate to capture the declared metric)

Root cause this script was written to address
--------------------------------------------
A previous compute experiment tried to import ``backend.app.db`` (a module that
does not exist; the correct module is ``backend.app.database``).  Even with the
correct import, that approach required a live database connection, which compute
VMs do not have.  This script avoids the database entirely: it runs the
already-installed agent unit-test suite (which IS available on the compute VM
once the repo is cloned) and reports the test pass fraction as the metric.
"""
import json
import os
import subprocess
import sys


def _agent_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "agent")
    )


def run_vitest(test_file: str, cwd: str) -> tuple[int, dict]:
    """Run vitest for *test_file* inside *cwd*.

    Returns ``(exit_code, vitest_json_data)``.  On a JSON-parse failure a
    synthetic summary is derived from the exit code so the caller always gets a
    numeric ``numPassedTests`` / ``numFailedTests`` regardless of vitest version
    differences in reporter output.
    """
    result = subprocess.run(
        ["node_modules/.bin/vitest", "run", test_file, "--reporter=json"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    try:
        return result.returncode, json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        if result.returncode == 0:
            return result.returncode, {"numPassedTests": 1, "numFailedTests": 0}
        return result.returncode, {"numPassedTests": 0, "numFailedTests": 1}


def compute_success_rate(vitest_data: dict) -> float:
    """Compute pass fraction from vitest JSON reporter output.

    Returns a value in ``[0.0, 1.0]``.  Returns 0.0 when no tests ran.
    """
    passed = int(vitest_data.get("numPassedTests", 0))
    failed = int(vitest_data.get("numFailedTests", 0))
    total = passed + failed
    if total == 0:
        return 0.0
    return round(passed / total, 4)


def main() -> int:
    agent_dir = _agent_dir()
    _exit_code, data = run_vitest(
        "handlers/structured-output-repair.test.ts", cwd=agent_dir
    )
    rate = compute_success_rate(data)
    metric = {"overall_success_rate": rate}

    rf = os.environ.get("BLUEPRINT_RESULT_FILE")
    if rf:
        with open(rf, "w") as f:
            json.dump(metric, f)

    print(json.dumps(metric))
    return 0


if __name__ == "__main__":
    sys.exit(main())
