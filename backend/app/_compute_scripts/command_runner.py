"""Command runner invoked by the GCP compute-job VM startup script.

argv[1]: command string to run via bash -lc
argv[2]: path to write the bounded command log JSON
argv[3]: max log bytes (integer; defaults to 32768)
argv[4]: path to write the live output tail (base64 JSON; optional)
"""

import base64
import contextlib
import json
import pathlib
import re
import subprocess
import sys
import threading

# ---------------------------------------------------------------------------
# Credential redaction — stdlib-only, mirrors failure_classification._CRED_REDACT_SUBS
# and _CRED_FIELD_RE.  Applied before writing command.log.json so no downstream
# copy (polling_metadata, serial marker, terminal.json bundle) is ever raw.
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
    # Stripe API keys
    (re.compile(r"sk_live_[A-Za-z0-9_-]+"), "sk_live_***"),
    (re.compile(r"sk_test_[A-Za-z0-9_-]+"), "sk_test_***"),
    (re.compile(r"rk_live_[A-Za-z0-9_-]+"), "rk_live_***"),
    (re.compile(r"whsec_[A-Za-z0-9_-]+"), "whsec_***"),
    # Database connection strings with embedded user:password
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

_LIVE_TAIL_MAX_BYTES = 10240


def _redact_credentials(text: str) -> str:
    for pattern, replacement in _CRED_REDACT_SUBS:
        text = pattern.sub(replacement, text)
    text = _CRED_FIELD_RE.sub(r"\1***\3", text)
    return text


# ---------------------------------------------------------------------------

command = sys.argv[1]
log_path = pathlib.Path(sys.argv[2])
max_log_bytes = int(sys.argv[3]) if len(sys.argv) > 3 else 32768
live_tail_path = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else None
log_path.parent.mkdir(parents=True, exist_ok=True)
proc = subprocess.Popen(
    ["bash", "-lc", command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
captured = bytearray()
truncated = False

_stop_tail = threading.Event()


def _flush_live_tail() -> None:
    if live_tail_path is None:
        return
    snapshot = bytes(captured)
    tail = snapshot[-_LIVE_TAIL_MAX_BYTES:]
    is_truncated = len(snapshot) > _LIVE_TAIL_MAX_BYTES or truncated
    payload = json.dumps(
        {
            "text": _redact_credentials(tail.decode("utf-8", "replace")),
            "truncated": is_truncated,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    with contextlib.suppress(OSError):
        live_tail_path.write_bytes(base64.b64encode(payload))


def _tail_worker() -> None:
    while not _stop_tail.wait(timeout=30):
        _flush_live_tail()


_tail_thread = threading.Thread(target=_tail_worker, daemon=True)
_tail_thread.start()

assert proc.stdout is not None
while True:
    chunk = proc.stdout.read(8192)
    if not chunk:
        break
    if len(captured) < max_log_bytes:
        remaining = max_log_bytes - len(captured)
        if len(chunk) <= remaining:
            captured.extend(chunk)
        else:
            captured.extend(chunk[:remaining])
            truncated = True
    else:
        truncated = True
exit_code = proc.wait()
_stop_tail.set()
if proc.stdout is not None:
    proc.stdout.close()
if captured or truncated:
    text = _redact_credentials(captured.decode("utf-8", "replace"))
    payload = {
        "text": text,
        "truncated": truncated,
        "byte_count": len(captured),
    }
    log_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
sys.exit(exit_code)
