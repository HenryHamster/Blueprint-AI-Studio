#!/usr/bin/env python3
"""Fixed no-network helper used only by the Resident Landlock preflight."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def main() -> int:
    token_path = Path(os.environ.get("AGENT_TOKEN_FILE", ""))
    if not token_path.is_absolute() or token_path.read_text(encoding="utf-8") != "credential sentinel":
        raise SystemExit("preflight helper credential boundary failed")
    if "strategic" in Path(sys.argv[0]).name:
        if len(sys.argv) != 4 or sys.argv[2] != "--json":
            raise SystemExit("invalid strategic helper arguments")
        payload = json.loads(sys.argv[3])
        print(json.dumps({"status": "ok", "applied": True, "payload": payload}))
        return 0
    if not sys.argv[1:] or sys.argv[1] != "running-update":
        raise SystemExit("invalid project-read helper arguments")
    print(json.dumps({"markdown": "preflight running update"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
