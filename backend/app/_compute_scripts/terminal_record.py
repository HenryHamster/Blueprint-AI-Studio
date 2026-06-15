"""Durable terminal-record writer for the GCP compute-job VM startup script.

Writes a small ``terminal.json`` into the artifact dir BEFORE poweroff so a
finished run can be reconciled from the durable GCS bundle even when its
serial-console marker is lost. A fast job can power off in the gap between
emitting ``BLUEPRINT_COMPUTE_STATUS=`` to the serial console and the
orchestrator's next serial poll — and GCP serial output goes empty once the VM
is TERMINATED — so a successful run with no captured marker would otherwise be
misclassified FAILURE. The durable record closes that race.

The record mirrors the serial markers exactly — status, exit code, reason, and
the same base64-encoded metrics / command-log / release-echo payloads — so the
backend reuses one decode path for both the serial fast-path and the durable
reconcile.

argv[1]: artifact dir (``terminal.json`` is written here; created if absent)
argv[2]: status — SUCCEEDED | FAILED
argv[3]: exit_code (integer string; a non-integer is recorded as null)
argv[4]: reason (e.g. completed, command_failed, runtime_ttl, repo_clone_failed)
argv[5]: result file path (optional; metrics JSON the command wrote) — may be absent
argv[6]: command-log path (optional; bounded combined stdout/stderr) — may be absent
env BLUEPRINT_RELEASE_SHA: optional release SHA echoed back for live-proof jobs

Best-effort by contract: every failure mode is swallowed and the script still
exits 0 — writing the durable record must NEVER block the VM's poweroff.
"""

import base64
import json
import os
import pathlib
import sys

TERMINAL_RECORD_FILENAME = "terminal.json"


def _b64_file(path_str):
    """Base64 of a file's bytes, or "" when the path is empty/missing/unreadable."""
    if not path_str:
        return ""
    try:
        data = pathlib.Path(path_str).read_bytes()
    except OSError:
        return ""
    if not data:
        return ""
    return base64.b64encode(data).decode("ascii")


def main():
    if len(sys.argv) < 5:
        return
    artifact_dir = pathlib.Path(sys.argv[1])
    status = sys.argv[2]
    exit_code_raw = sys.argv[3]
    reason = sys.argv[4]
    result_file = sys.argv[5] if len(sys.argv) > 5 else ""
    command_log = sys.argv[6] if len(sys.argv) > 6 else ""

    try:
        exit_code = int(exit_code_raw)
    except (TypeError, ValueError):
        exit_code = None

    release_sha = os.environ.get("BLUEPRINT_RELEASE_SHA", "")
    release_echo_b64 = (
        base64.b64encode(release_sha.encode("utf-8")).decode("ascii")
        if release_sha
        else ""
    )

    record = {
        "schema_version": 1,
        "status": status,
        "exit_code": exit_code,
        "reason": reason,
        "metrics_b64": _b64_file(result_file),
        "command_log_b64": _b64_file(command_log),
        "release_echo_b64": release_echo_b64,
    }

    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / TERMINAL_RECORD_FILENAME).write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return


if __name__ == "__main__":
    # The on-VM caller invokes this as `python3 ... || true` with stdout/stderr
    # discarded, so a non-zero exit can never block poweroff. main() guards its
    # only fallible I/O (file reads + the terminal.json write) with narrow
    # OSError handling, so the durable record is best-effort either way.
    main()
