"""Launch an operator compute_experiment via POST /api/admin/tasks/compute-experiment.

Usage:
    # Substrate smoke test (NO repo reliance) — confirms a compute VM provisions,
    # runs the command, reports a metric, uploads its artifact, and gates SUCCESS:
    python scripts/launch_compute_experiment.py --dud --repo HenryHamster/stsbot2

    # Repo-reliant test (imports the project's package) — confirms the clone fix:
    python scripts/launch_compute_experiment.py --repo HenryHamster/stsbot2 \
        --command 'python3 -c "import json,os,stsbot; open(os.environ[\"BLUEPRINT_RESULT_FILE\"],\"w\").write(json.dumps({\"import_ok\":1}))"' \
        --metric import_ok --threshold 1

The dud command depends on NOTHING from the repo — it only writes a metric to
$BLUEPRINT_RESULT_FILE and a file to $BLUEPRINT_ARTIFACT_DIR. A SUCCESS verdict
proves the substrate itself is sound (so any FAILURE on a real experiment is the
missing-repo gap, not the substrate).
"""
import argparse
import json
import sys

from blueprint_api import api

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# No-repo substrate probe. Heredoc body uses ONLY double quotes so the whole
# string survives the backend's shlex.quote(command) -> bash -lc wrapping.
_DUD_COMMAND = """python3 - <<'PYEOF'
import json
import os
import sys

result = {"dud_ok": 1, "py_major": sys.version_info[0]}
rf = os.environ.get("BLUEPRINT_RESULT_FILE")
if rf:
    with open(rf, "w") as f:
        json.dump(result, f)
ad = os.environ.get("BLUEPRINT_ARTIFACT_DIR")
if ad:
    with open(os.path.join(ad, "dud.txt"), "w") as f:
        f.write("compute substrate reached runtime and wrote an artifact\\n")
print(json.dumps(result))
PYEOF"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/repo, e.g. HenryHamster/stsbot2")
    ap.add_argument("--dud", action="store_true",
                    help="use the no-repo substrate-probe command + dud_ok>=1 criterion")
    ap.add_argument("--command", help="explicit command (ignored when --dud)")
    ap.add_argument("--title", default="operator substrate probe")
    ap.add_argument("--prompt", default="Operator-launched compute_experiment to verify the compute substrate.")
    ap.add_argument("--base-branch", default="main")
    ap.add_argument("--spend-cap", type=float, default=0.50)
    ap.add_argument("--deadline", type=int, default=600)
    ap.add_argument("--metric", default="dud_ok")
    ap.add_argument("--operator", default=">=")
    ap.add_argument("--threshold", type=float, default=1.0)
    ap.add_argument("--machine-type", default=None)
    args = ap.parse_args()

    command = _DUD_COMMAND if args.dud else args.command
    if not command:
        ap.error("provide --dud or --command")

    body = {
        "github_repo": args.repo,
        "title": args.title,
        "prompt": args.prompt,
        "base_branch": args.base_branch,
        "command": command,
        "spend_cap_usd": args.spend_cap,
        "deadline_seconds": args.deadline,
        "success_criterion": {
            "metric": args.metric,
            "operator": args.operator,
            "threshold": args.threshold,
        },
    }
    if args.machine_type:
        body["machine_type"] = args.machine_type

    resp = api("POST", "/api/admin/tasks/compute-experiment", body)
    print(json.dumps(resp, indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
