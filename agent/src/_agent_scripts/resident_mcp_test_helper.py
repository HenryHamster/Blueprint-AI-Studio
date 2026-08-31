#!/usr/bin/python3
"""Fixed executable fixture for the Workday MCP helper boundary tests."""

import json
import os
import subprocess
import sys
import time


if len(sys.argv) >= 4 and sys.argv[2] == "--json":
    payload = json.loads(sys.argv[3])
    markdown = payload.get("markdown") if isinstance(payload, dict) else None
    response_prefix = "fixture-response:"
    if isinstance(markdown, str) and markdown.startswith(response_prefix):
        sys.stdout.write(markdown.removeprefix(response_prefix))
        raise SystemExit(0)
    prefix = "fixture-overflow-child:"
    if isinstance(markdown, str) and markdown.startswith(prefix):
        marker = markdown.removeprefix(prefix)
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib,signal,sys,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "sys.stdout.close(); sys.stderr.close(); time.sleep(0.4); "
                    "pathlib.Path(sys.argv[1]).write_text('orphan')"
                ),
                marker,
            ]
        )
        time.sleep(0.1)
        sys.stdout.write("x" * (513 * 1024))
        sys.stdout.flush()
        time.sleep(30)
        raise SystemExit(0)


print(
    json.dumps(
        {
            "status": "ok",
            "args": sys.argv[1:],
            "env": {
                "PATH": os.environ.get("PATH"),
                "TMPDIR": os.environ.get("TMPDIR"),
                "ORCHESTRATOR_URL": os.environ.get("ORCHESTRATOR_URL"),
                "AGENT_TOKEN_FILE": os.environ.get("AGENT_TOKEN_FILE"),
                "BLUEPRINT_STRATEGIC_OPERATION_ID": os.environ.get(
                    "BLUEPRINT_STRATEGIC_OPERATION_ID"
                ),
                "BLUEPRINT_STRATEGIC_MAX_ATTEMPTS": os.environ.get(
                    "BLUEPRINT_STRATEGIC_MAX_ATTEMPTS"
                ),
            },
            "leaked": [
                key
                for key in (
                    "HOME",
                    "CODEX_HOME",
                    "OPENAI_API_KEY",
                    "CODEX_API_KEY",
                    "ANTHROPIC_API_KEY",
                )
                if key in os.environ
            ],
        },
        separators=(",", ":"),
    ),
    end="",
)
