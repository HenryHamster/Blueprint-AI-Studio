"""Cloud Run log viewer.

Fetches logs from the Blueprint orchestrator via the Cloud Logging REST API.
Handles auth token retrieval and filter construction.

Usage:
    python scripts/logs.py errors [--minutes N] [--limit N]
    python scripts/logs.py tail [--minutes N] [--limit N] [--severity LEVEL]
    python scripts/logs.py search <pattern> [--minutes N] [--limit N]
    python scripts/logs.py task <task_id> [--minutes N]
"""
import json
import os
import sys
import urllib.request
import urllib.error
import subprocess
from datetime import datetime, timezone, timedelta

GCP_PROJECT = "friction-logging-489102"
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
        env = {**os.environ, "CLOUDSDK_PYTHON": "python3"}
        subprocess.run([path, "--version"], capture_output=True, timeout=5, env=env)
        GCLOUD_BIN = path
        break
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        continue


def _get_access_token():
    """Get a GCP access token via gcloud."""
    if not GCLOUD_BIN:
        print("ERROR: gcloud not found. Install Google Cloud SDK.")
        sys.exit(1)
    import os
    env = {**os.environ, "CLOUDSDK_PYTHON": "python3"}
    result = subprocess.run(
        [GCLOUD_BIN, "auth", "print-access-token"],
        capture_output=True, text=True, timeout=15, env=env
    )
    token = result.stdout.strip().split("\n")[-1]  # Last line, skip Windows noise
    if not token or len(token) < 20:
        print(f"ERROR: Could not get access token. Run: gcloud auth login")
        print(f"  stdout length: {len(result.stdout)} chars")
        print(f"  stderr: {result.stderr[:200]}")
        sys.exit(1)
    return token


def _fetch_logs(filter_str, limit=20):
    """Fetch logs from Cloud Logging REST API."""
    token = _get_access_token()
    body = {
        "resourceNames": [f"projects/{GCP_PROJECT}"],
        "filter": filter_str,
        "orderBy": "timestamp desc",
        "pageSize": min(limit, 100),
    }
    req = urllib.request.Request(
        "https://logging.googleapis.com/v2/entries:list",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("entries", [])
    except urllib.error.HTTPError as e:
        print(f"API error {e.code}: {e.read().decode()[:500]}")
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
            error_lines = [l for l in lines if l.strip() and not l.startswith("  File")]
            last_file = [l for l in lines if l.strip().startswith("File ")]
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
