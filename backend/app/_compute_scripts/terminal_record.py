"""Durable terminal-record writer for the GCP compute-job VM startup script.

Writes a small ``terminal.json`` into the artifact dir BEFORE poweroff so a
finished run can be reconciled from the durable GCS bundle even when its
serial-console marker is lost. A fast job can power off in the gap between
emitting ``BLUEPRINT_COMPUTE_STATUS=`` to the serial console and the
orchestrator's next serial poll — and GCP serial output goes empty once the VM
is TERMINATED — so a successful run with no captured marker would otherwise be
misclassified FAILURE. The durable record closes that race.

The record mirrors the serial markers exactly — status, exit code, reason, and
the same base64-encoded metrics / command-log / release-echo / resume-provenance
payloads — so the
backend reuses one decode path for both the serial fast-path and the durable
reconcile.

argv[1]: artifact dir (``terminal.json`` is written here; created if absent)
argv[2]: status — SUCCEEDED | FAILED
argv[3]: exit_code (integer string; a non-integer is recorded as null)
argv[4]: reason (e.g. completed, command_failed, runtime_ttl, repo_clone_failed)
argv[5]: result file path (optional; metrics JSON the command wrote) — may be absent
argv[6]: command-log path (optional; bounded combined stdout/stderr) — may be absent
argv[7]: resume provenance (optional; base64-encoded JSON) — may be absent
env BLUEPRINT_RELEASE_SHA: optional release SHA echoed back for live-proof jobs

Best-effort by contract: every failure mode is swallowed and the script still
exits 0 — writing the durable record must NEVER block the VM's poweroff.
"""

import base64
import json
import os
import pathlib
import re
import sys

TERMINAL_RECORD_FILENAME = "terminal.json"

# ---------------------------------------------------------------------------
# Credential redaction — stdlib-only, inlined here as defense-in-depth before
# base64-encoding the command-log file, in case command.log.json is ever
# populated by another writer.  command_runner.py carries an independent copy
# of the same patterns (both scripts are shipped standalone to VMs).
# Patterns mirror failure_classification._CRED_REDACT_SUBS / _CRED_FIELD_RE.
# ---------------------------------------------------------------------------

_CRED_REDACT_SUBS = (
    (re.compile(r"https://x-access-token:[^@]+@"), "https://x-access-token:***@"),
    (re.compile(r"https://[^:]+:[^@]+@"), "https://***:***@"),
    (re.compile(r"ghp_[A-Za-z0-9_]+"), "ghp_***"),
    (re.compile(r"ghs_[A-Za-z0-9_]+"), "ghs_***"),
    (re.compile(r"ghu_[A-Za-z0-9_]+"), "ghu_***"),
    (re.compile(r"gho_[A-Za-z0-9_]+"), "gho_***"),
    (re.compile(r"ghr_[A-Za-z0-9_]+"), "ghr_***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "github_pat_***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{10,}"), "sk-proj-***"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "sk-***"),
    # AWS IAM access keys (AKIA + exactly 16 uppercase alphanumeric)
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),
    # Stripe API keys (sk_live_, sk_test_, rk_live_) and webhook signing secrets (whsec_)
    (re.compile(r"sk_live_[A-Za-z0-9_-]+"), "sk_live_***"),
    (re.compile(r"sk_test_[A-Za-z0-9_-]+"), "sk_test_***"),
    (re.compile(r"rk_live_[A-Za-z0-9_-]+"), "rk_live_***"),
    (re.compile(r"whsec_[A-Za-z0-9_-]+"), "whsec_***"),
    # Database connection strings with embedded user:password (including driver-qualified forms)
    (re.compile(r"postgresql(?:\+\w+)?://[^:/@]+:[^@]+@"), "postgresql://***:***@"),
    (re.compile(r"postgres://[^:/@]+:[^@]+@"), "postgres://***:***@"),
    (re.compile(r"AIza[A-Za-z0-9_-]{20,}"), "AIza***"),
    (re.compile(r"ya29\.[A-Za-z0-9_.\-]+"), "ya29***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "eyJ***"),
    (re.compile(r"Bearer\s+[^\s,'\"]+", re.IGNORECASE), "Bearer ***"),
    (re.compile(r"oauth_token[=:]\s*[^\s&'\"]+", re.IGNORECASE), "oauth_token=***"),
    (
        re.compile(
            r"([\"']?\b(?:AGENT_SECRET_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY|CODEX_API_KEY|GEMINI_API_KEY|ZAI_API_KEY|DEEPSEEK_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|CALL_DEEPGRAM_API_KEY|CALL_CARTESIA_API_KEY|X-Agent-Token)\b[\"']?\s*[:=]\s*)(?!['\"]?AIza\*\*\*)(?:(['\"])(?:[^'\"\\]|\\.)+\2|[^\s,'\";&\])\n]{4,})",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    (
        re.compile(
            r"-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----"
        ),
        "***PRIVATE KEY***",
    ),
)

_CRED_FIELD_NAMES = (
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "client_secret",
    "private_key",
    "token",
)

_CRED_FIELD_RE = re.compile(
    r'(["\']?\b(?:'
    + "|".join(_CRED_FIELD_NAMES)
    + r")"
    + r'\b["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_.+=\-/@]{8,})(["\']?)',
    re.IGNORECASE,
)


def _redact_credentials(text: str) -> str:
    for pattern, replacement in _CRED_REDACT_SUBS:
        text = pattern.sub(replacement, text)
    text = _CRED_FIELD_RE.sub(r"\1***\3", text)
    return text


# ---------------------------------------------------------------------------


def _b64_file(path_str, *, redact_command_log: bool = False):
    """Base64 of a file's bytes, or "" when the path is empty/missing/unreadable.

    When redact_command_log=True the file is treated as a JSON command-log
    record ({text, truncated, byte_count}) and the text field is redacted
    before re-encoding — defense-in-depth for writers other than command_runner.
    """
    if not path_str:
        return ""
    try:
        data = pathlib.Path(path_str).read_bytes()
    except OSError:
        return ""
    if not data:
        return ""
    if redact_command_log:
        try:
            record = json.loads(data.decode("utf-8", "replace"))
            if isinstance(record, dict) and isinstance(record.get("text"), str):
                record["text"] = _redact_credentials(record["text"])
                data = json.dumps(record, ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                )
        except Exception:
            pass
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
    resume_provenance_b64 = sys.argv[7] if len(sys.argv) > 7 else ""

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
        "command_log_b64": _b64_file(command_log, redact_command_log=True),
        "release_echo_b64": release_echo_b64,
        "resume_provenance_b64": resume_provenance_b64,
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
