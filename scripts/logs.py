"""Cloud Run log viewer.

Fetches logs from the Blueprint orchestrator by shelling out to
`gcloud logging read`. gcloud handles auth, project, and quota internally —
unlike a hand-extracted `gcloud auth print-access-token`, which the Cloud
Logging REST API rejects as ACCESS_TOKEN_TYPE_UNSUPPORTED.

Usage:
    python scripts/logs.py errors [--minutes N] [--limit N]
    python scripts/logs.py tail [--minutes N] [--limit N] [--severity LEVEL]
    python scripts/logs.py search <pattern> [--minutes N] [--limit N]
    python scripts/logs.py task <task_id> [--minutes N]
"""
import json
import os
import sys
import subprocess
from datetime import datetime, timezone, timedelta

GCP_PROJECT = os.environ.get("GCP_PROJECT", "blueprint-orchestrator")
SERVICE_NAME = "blueprint-orchestrator"

# gcloud path — use the binary directly, avoid .cmd wrapper issues on Windows
GCLOUD_BIN = None
_candidates = [
    "/c/Users/Henry/google-cloud-sdk/bin/gcloud",
    os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
    "C:\\Users\\Henry\\google-cloud-sdk\\bin\\gcloud.cmd",
    "gcloud",
]
for path in _candidates:
    try:
        # No CLOUDSDK_PYTHON override — forcing it to "python3" fails on
        # Windows (no such executable; the Store alias is not a real python).
        # gcloud's own bundled-python detection works. Require returncode 0
        # so a candidate that launches but errors isn't accepted.
        probe = subprocess.run(
            [path, "--version"], capture_output=True, timeout=15
        )
        if probe.returncode == 0:
            GCLOUD_BIN = path
            break
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        continue


def _fetch_logs(filter_str, limit=20):
    """Fetch logs from Cloud Logging via `gcloud logging read`.

    gcloud's own auth path works where a raw `print-access-token` does not, and
    `--format=json` emits the same LogEntry schema the REST API returns, so
    `_format_entry` consumes the result unchanged. Entries come back newest-first
    (`--order=desc`), matching the previous `orderBy: "timestamp desc"`.
    """
    if not GCLOUD_BIN:
        print("ERROR: gcloud not found. Install Google Cloud SDK.")
        sys.exit(1)
    cmd = [
        GCLOUD_BIN, "logging", "read", filter_str,
        f"--project={GCP_PROJECT}",
        f"--limit={limit}",
        "--format=json",
        "--order=desc",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90
        )
    except subprocess.TimeoutExpired:
        print("ERROR: `gcloud logging read` timed out after 90s.")
        return []
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        print(f"ERROR: `gcloud logging read` failed (exit {result.returncode}):")
        print(f"  {stderr[:500]}")
        if "auth" in stderr.lower() or "credential" in stderr.lower():
            print("  Run: gcloud auth login")
        return []
    out = (result.stdout or "").strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # --format=json should emit pure JSON on stdout; if a banner leaked in,
        # salvage the array.
        start = out.find("[")
        if start != -1:
            try:
                return json.loads(out[start:])
            except json.JSONDecodeError:
                pass
        print(f"ERROR: could not parse gcloud output as JSON:\n  {out[:300]}")
        return []


def _format_entry(entry):
    """Format a single log entry for display. Returns (timestamp, severity, full_text)."""
    ts = entry.get("timestamp", "")[:19]
    severity = entry.get("severity", "?")[:5]

    # Get the actual log text — no truncation here, callers decide
    text = entry.get("textPayload", "")
    if not text:
        json_payload = entry.get("jsonPayload", {})
        text = json_payload.get("message", json.dumps(json_payload) if json_payload else "")
    if not text:
        http = entry.get("httpRequest", {})
        if http:
            text = f"{http.get('requestMethod', '?')} {http.get('requestUrl', '?')} -> {http.get('status', '?')}"

    return ts, severity, text


def _parse_common_args(args, defaults=None):
    """Parse common CLI args: --minutes, --limit, --full."""
    opts = {**(defaults or {})}
    for i, a in enumerate(args):
        if a == "--minutes" and i + 1 < len(args):
            opts["minutes"] = int(args[i + 1])
        if a == "--limit" and i + 1 < len(args):
            opts["limit"] = int(args[i + 1])
        if a == "--severity" and i + 1 < len(args):
            opts["severity"] = args[i + 1].upper()
        if a == "--full":
            opts["full"] = True
    return opts


def _time_filter(minutes):
    """Build a timestamp filter for the last N minutes."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return f'timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"'


def cmd_errors(args):
    """Show recent error logs from Cloud Run. Use --full for complete tracebacks."""
    opts = _parse_common_args(args, {"minutes": 60, "limit": 15, "full": False})

    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{SERVICE_NAME}" '
        f'severity>=ERROR '
        f'{_time_filter(opts["minutes"])}'
    )
    entries = _fetch_logs(filter_str, opts["limit"])
    if not entries:
        print(f"No errors in the last {opts['minutes']} minutes.")
        return

    print(f"=== ERRORS (last {opts['minutes']}min, showing {len(entries)}) ===\n")
    for entry in entries:
        ts, severity, text = _format_entry(entry)
        if opts["full"]:
            print(f"  {ts}  [{severity}]")
            for line in text.split("\n"):
                print(f"    {line}")
            print()
        elif "Traceback" in text:
            lines = text.split("\n")
            error_lines = [ln for ln in lines if ln.strip() and not ln.startswith("  File")]
            last_file = [ln for ln in lines if ln.strip().startswith("File ")]
            summary = error_lines[-1] if error_lines else text[:300]
            location = last_file[-1].strip() if last_file else ""
            print(f"  {ts}  {location}")
            print(f"    {summary}")
            print()
        else:
            print(f"  {ts}  [{severity}] {text[:300]}")
            print()


def cmd_tail(args):
    """Show recent logs from Cloud Run."""
    opts = _parse_common_args(args, {"minutes": 10, "limit": 30, "severity": "INFO", "full": False})

    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{SERVICE_NAME}" '
        f'severity>={opts["severity"]} '
        f'{_time_filter(opts["minutes"])}'
    )
    entries = _fetch_logs(filter_str, opts["limit"])
    if not entries:
        print(f"No logs (severity>={opts['severity']}) in the last {opts['minutes']} minutes.")
        return

    for entry in reversed(entries):
        ts, severity_str, text = _format_entry(entry)
        if opts["full"]:
            print(f"  {ts}  [{severity_str:5}] {text}")
        else:
            first_line = text.split("\n")[0]
            print(f"  {ts}  [{severity_str:5}] {first_line}")


def cmd_search(args):
    """Search logs for a pattern."""
    if not args or args[0].startswith("-"):
        print("Usage: logs.py search <pattern> [--minutes N] [--limit N] [--full]")
        return
    pattern = args[0]
    opts = _parse_common_args(args, {"minutes": 60, "limit": 20, "full": False})

    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{SERVICE_NAME}" '
        f'textPayload:"{pattern}" '
        f'{_time_filter(opts["minutes"])}'
    )
    entries = _fetch_logs(filter_str, opts["limit"])
    if not entries:
        print(f"No logs matching '{pattern}' in the last {opts['minutes']} minutes.")
        return

    print(f"=== SEARCH '{pattern}' (last {opts['minutes']}min, {len(entries)} results) ===\n")
    for entry in reversed(entries):
        ts, severity, text = _format_entry(entry)
        if opts["full"]:
            print(f"  {ts}  [{severity:5}] {text}")
        else:
            first_line = text.split("\n")[0]
            print(f"  {ts}  [{severity:5}] {first_line}")


def cmd_task(args):
    """Search logs for a specific task ID."""
    if not args or args[0].startswith("-"):
        print("Usage: logs.py task <task_id> [--minutes N] [--full]")
        return
    task_id = args[0]
    opts = _parse_common_args(args, {"minutes": 120, "limit": 50, "full": False})

    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{SERVICE_NAME}" '
        f'textPayload:"{task_id}" '
        f'{_time_filter(opts["minutes"])}'
    )
    entries = _fetch_logs(filter_str, opts["limit"])
    if not entries:
        print(f"No logs for task {task_id[:8]} in the last {opts['minutes']} minutes.")
        return

    print(f"=== TASK {task_id[:8]} (last {opts['minutes']}min, {len(entries)} entries) ===\n")
    for entry in reversed(entries):
        ts, severity, text = _format_entry(entry)
        if opts["full"]:
            print(f"  {ts}  [{severity:5}] {text}")
        else:
            first_line = text.split("\n")[0]
            print(f"  {ts}  [{severity:5}] {first_line}")


COMMANDS = {
    "errors": cmd_errors,
    "tail": cmd_tail,
    "search": cmd_search,
    "task": cmd_task,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Usage: logs.py <command> [args...]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
