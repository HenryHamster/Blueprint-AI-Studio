#!/usr/bin/env python3
"""Fail-closed lifecycle controller for Blueprint's single Tailscale Funnel.

The controller owns exactly one HTTPS listener and never uses ``funnel reset``.
It refuses to mutate a node whose Funnel/Serve state is not either empty or the
exact Blueprint route.  This prevents an install or stop from silently
overwriting an operator-managed route on the same Tailscale node.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_PORT_RE = re.compile(r"^[1-9][0-9]{0,4}$")
_HOST_PORT_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net:443$"
)
_HTTPS_PORT = "443"


@dataclass(frozen=True)
class Config:
    binary: Path
    api_port: int

    @property
    def target(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"


def _safe_text(value: str, *, field: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not be empty or contain control characters")
    if any(char.isspace() for char in value):
        raise ValueError(f"{field} must not contain whitespace")
    return value


def _executable_path(value: str) -> Path:
    path = Path(_safe_text(value, field="BLUEPRINT_TAILSCALE_BINARY"))
    if not path.is_absolute():
        raise ValueError("BLUEPRINT_TAILSCALE_BINARY must be an absolute path")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"Tailscale binary is not accessible: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Tailscale binary must be a real regular file, not a symlink")
    if not os.access(path, os.X_OK):
        raise ValueError(f"Tailscale binary is not executable: {path}")
    return path


def load_config(environ: dict[str, str] | None = None) -> Config:
    values = os.environ if environ is None else environ
    binary = _executable_path(
        values.get("BLUEPRINT_TAILSCALE_BINARY", "/usr/bin/tailscale")
    )
    raw_port = _safe_text(
        values.get("BLUEPRINT_EDGE_API_PORT", ""), field="BLUEPRINT_EDGE_API_PORT"
    )
    if not _PORT_RE.fullmatch(raw_port):
        raise ValueError("BLUEPRINT_EDGE_API_PORT must be a decimal port from 1 to 65535")
    port = int(raw_port)
    if port > 65535:
        raise ValueError("BLUEPRINT_EDGE_API_PORT must be a decimal port from 1 to 65535")
    return Config(binary=binary, api_port=port)


def _invoke(
    argv: list[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    try:
        return run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not execute Tailscale CLI: {exc}") from exc


def _read_status(
    config: Config,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    result = _invoke(
        [str(config.binary), "funnel", "status", "--json"],
        run=run,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(
            "could not read Tailscale Funnel status as the Blueprint service user; "
            "configure that account with `sudo tailscale set --operator=<user>`: "
            f"{detail}"
        )
    try:
        status_value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Tailscale Funnel status was not valid JSON") from exc
    if not isinstance(status_value, dict):
        raise RuntimeError("Tailscale Funnel status must be a JSON object")
    return status_value


def _exact_route_hostname(status_value: dict[str, Any], target: str) -> str | None:
    """Return the owned hostname for an exact config; None means empty.

    Any additional listener, handler, service, foreground route, or unknown
    top-level field is foreign state and is rejected by the caller.
    """

    if status_value == {}:
        return None
    if set(status_value) != {"TCP", "Web", "AllowFunnel"}:
        raise ValueError("foreign Tailscale Funnel state: unexpected configuration fields")
    if status_value["TCP"] != {_HTTPS_PORT: {"HTTPS": True}}:
        raise ValueError("foreign Tailscale Funnel state: HTTPS listener is not exact")
    web = status_value["Web"]
    allow = status_value["AllowFunnel"]
    if not isinstance(web, dict) or len(web) != 1:
        raise ValueError("foreign Tailscale Funnel state: expected exactly one web route")
    hostname, web_config = next(iter(web.items()))
    if not isinstance(hostname, str) or not _HOST_PORT_RE.fullmatch(hostname):
        raise ValueError("foreign Tailscale Funnel state: invalid Funnel hostname")
    expected_web = {"Handlers": {"/": {"Proxy": target}}}
    if web_config != expected_web:
        raise ValueError("foreign Tailscale Funnel state: proxy target is not exact")
    if allow != {hostname: True}:
        raise ValueError("foreign Tailscale Funnel state: public listener is not exact")
    return hostname


def check(
    config: Config,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    _exact_route_hostname(_read_status(config, run=run), config.target)


def start(
    config: Config,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    current = _read_status(config, run=run)
    if _exact_route_hostname(current, config.target) is not None:
        return
    result = _invoke(
        [
            str(config.binary),
            "funnel",
            "--yes",
            "--bg",
            "--https=443",
            config.target,
        ],
        run=run,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"could not enable Blueprint Funnel route: {detail}")
    if _exact_route_hostname(_read_status(config, run=run), config.target) is None:
        raise RuntimeError("Tailscale reported success but Blueprint Funnel route is absent")


def stop(
    config: Config,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    current = _read_status(config, run=run)
    if _exact_route_hostname(current, config.target) is None:
        return
    result = _invoke(
        [
            str(config.binary),
            "funnel",
            "--yes",
            "--https=443",
            config.target,
            "off",
        ],
        run=run,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"could not disable Blueprint Funnel route: {detail}")
    if _read_status(config, run=run) != {}:
        raise RuntimeError("Blueprint Funnel route was not removed exactly")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("start", "stop"), default="start")
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
        if args.check:
            check(config)
        elif args.action == "stop":
            stop(config)
        else:
            start(config)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
