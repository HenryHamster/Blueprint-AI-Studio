#!/usr/bin/env python3
"""Production-shape, quota-free preflight for the Resident Landlock boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid


MARKER_KIND = "blueprint.resident_landlock_preflight.v1"


def abort(reason: str) -> "NoReturn":
    print(f"resident_landlock_preflight: {reason}", file=sys.stderr)
    raise SystemExit(126)


def expect_denied(action, label: str) -> None:
    try:
        action()
    except PermissionError:
        return
    except OSError as error:
        if error.errno in {1, 13, 30}:
            return
        abort(f"{label} returned unexpected errno={error.errno}")
    abort(f"{label} unexpectedly succeeded")


def stop_process_group(process: subprocess.Popen, grace: float = 0.5) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def serve_broker(
    socket_path: Path,
    upstream: list[str],
    upstream_env: dict[str, str],
    ready: threading.Event,
    stop: threading.Event,
    errors: list[str],
    metrics: dict[str, int],
) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        server.listen(1)
        server.settimeout(0.2)
        ready.set()
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            metrics["connections"] += 1
            metrics["active"] += 1
            process = subprocess.Popen(
                upstream,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=upstream_env,
                start_new_session=True,
            )

            def input_relay() -> None:
                try:
                    while data := connection.recv(65_536):
                        process.stdin.write(data)
                        process.stdin.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass

            def output_relay() -> None:
                try:
                    while data := os.read(process.stdout.fileno(), 65_536):
                        connection.sendall(data)
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    try:
                        connection.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            input_thread = threading.Thread(target=input_relay, daemon=True)
            output_thread = threading.Thread(target=output_relay, daemon=True)
            input_thread.start()
            output_thread.start()
            deadline = time.monotonic() + 10
            while process.poll() is None and not stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            timed_out = process.poll() is None and not stop.is_set()
            if process.poll() is None:
                stop_process_group(process)
            if timed_out:
                errors.append("broker upstream exceeded its bounded lifetime")
            returncode = process.wait(timeout=2)
            stderr = process.stderr.read().decode("utf-8", errors="replace")[-1000:]
            input_thread.join(timeout=1)
            output_thread.join(timeout=1)
            process.stdout.close()
            process.stderr.close()
            connection.close()
            metrics["active"] -= 1
            if returncode != 0 and not stop.is_set():
                errors.append(f"broker upstream rc={returncode}: {stderr}")
    except Exception as error:  # fail closed into the controller result
        errors.append(f"broker failed: {error}")
        ready.set()
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def run_restricted_child(options: argparse.Namespace) -> int:
    scratch = Path(options.scratch)
    repo = Path(options.repo)
    credential = Path(options.credential)
    authority_state = Path(options.authority_state)
    mcp_dir = Path(options.mcp_dir)
    receipt = Path(options.receipt)
    staged = Path(options.staged)
    repository_root = Path(options.repository_root) if options.repository_root else None
    repository_sentinel = Path(options.repository_sentinel) if options.repository_sentinel else None
    socket_path = Path(options.socket)
    socket_client = Path(options.socket_client)

    if not os.environ.get("BLUEPRINT_RESIDENT_LANDLOCK", "").startswith("v1:abi"):
        abort("Landlock receipt is absent")
    with open("/dev/null", "r+b", buffering=0) as null_device:
        if null_device.write(b"resident-landlock") != len(b"resident-landlock"):
            abort("exact /dev/null write contract failed")
        if null_device.read(1) != b"":
            abort("exact /dev/null read contract failed")
    expect_denied(lambda: (repo / "sentinel.txt").read_bytes(), "repository read")
    expect_denied(lambda: list(repo.iterdir()), "repository list")
    expect_denied(
        lambda: os.open(repo / "sentinel.txt", os.O_WRONLY | os.O_TRUNC),
        "repository truncate",
    )
    expect_denied(lambda: credential.read_bytes(), "credential read")
    expect_denied(
        lambda: os.open(credential, os.O_WRONLY | os.O_TRUNC),
        "credential truncate",
    )
    expect_denied(lambda: list(authority_state.iterdir()), "authority state list")
    expect_denied(lambda: receipt.read_bytes(), "authority receipt read")
    expect_denied(lambda: os.open(staged, os.O_WRONLY | os.O_CREAT), "authority staged write")
    if repository_root and repository_sentinel:
        expect_denied(lambda: list(repository_root.iterdir()), "outer repository list")
        expect_denied(lambda: repository_sentinel.read_bytes(), "outer repository read")
        expect_denied(
            lambda: os.open(repository_sentinel, os.O_WRONLY),
            "outer repository write-open",
        )
        expect_denied(
            lambda: (repository_root / ".resident-landlock-forbidden").write_text("forbidden"),
            "outer repository create",
        )
    immutable = mcp_dir / "resident_reply_mcp.mjs"
    if not immutable.read_bytes().startswith(b"#!/usr/bin/env node"):
        abort("immutable MCP script is unreadable")
    expect_denied(lambda: os.open(immutable, os.O_WRONLY), "immutable MCP write")

    probe = scratch / "restricted-child-write"
    probe.write_text("one", encoding="utf-8")
    with probe.open("r+b") as handle:
        handle.truncate(1)
    renamed = scratch / "restricted-child-renamed"
    probe.rename(renamed)
    renamed.unlink()
    outside = Path("/tmp") / f"resident-landlock-outside-{os.getpid()}"
    try:
        outside.write_text("forbidden", encoding="utf-8")
    except PermissionError:
        pass
    else:
        outside.unlink(missing_ok=True)
        abort("non-designated /tmp write unexpectedly succeeded")

    node = os.path.realpath(options.node)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "project_read", "arguments": {"resource": "running_update"},
        }},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "strategic_operation", "arguments": {
                "operation": "set-running-update",
                "data": {"markdown": "preflight update"},
                "idempotency_key": "resident-landlock-preflight",
            },
        }},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "reply_to_operator", "arguments": {"content": "preflight reply"},
        }},
    ]
    mcp = subprocess.run(
        [node, str(socket_client), str(socket_path)],
        input="\n".join(json.dumps(item) for item in requests) + "\n",
        capture_output=True,
        text=True,
        timeout=8,
        env={
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
        },
    )
    if mcp.returncode != 0:
        abort(f"MCP subprocess failed rc={mcp.returncode}: {mcp.stderr[-500:]}")
    try:
        responses = {item["id"]: item for item in (
            json.loads(line) for line in mcp.stdout.splitlines() if line
        )}
        tools = responses[2]["result"]["tools"]
        project_text = responses[3]["result"]["content"][0]["text"]
        project_read = responses[3]["result"]["structuredContent"]
        strategic = responses[4]["result"]["structuredContent"]
        accepted = responses[5]["result"]["structuredContent"]["accepted"]
    except (KeyError, TypeError, json.JSONDecodeError):
        abort("MCP response shape is invalid")
    if [tool.get("name") for tool in tools] != [
        "reply_to_operator", "strategic_operation", "project_read",
    ]:
        abort("MCP three-tool contract was not exposed")
    expected_project = json.dumps({"markdown": "preflight running update"})
    if (
        project_read.get("resource") != "running_update"
        or project_read.get("bytes") != len(expected_project.encode())
        or project_text != expected_project
    ):
        abort("MCP project-read helper did not complete")
    try:
        uuid.UUID(strategic["operation_id"])
        valid_fingerprint = len(bytes.fromhex(strategic["operation_fingerprint"])) == 32
    except (KeyError, TypeError, ValueError):
        valid_fingerprint = False
    if (
        strategic.get("outcome") != "applied"
        or strategic.get("applied") is not True
        or strategic.get("commit_acknowledged") is not True
        or strategic.get("receipt_confirmed") is not True
        or strategic.get("idempotency_key") != "resident-landlock-preflight"
        or not valid_fingerprint
    ):
        abort("MCP strategic helper did not complete")
    if accepted is not True:
        abort("MCP tool contract was not completed")
    if "credential sentinel" in mcp.stdout or "credential sentinel" in mcp.stderr:
        abort("MCP broker exposed credential bytes to the restricted child")

    codex_started = False
    if options.codex:
        command = [
            os.path.realpath(options.codex),
            "app-server", "--strict-config", "--listen", "stdio://",
            "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"',
            "-c", "tools.web_search=false",
            "-c", "mcp_servers={}",
            "-c", f"mcp_servers.workday.command={json.dumps(node)}",
            "-c", "mcp_servers.workday.args=" + json.dumps([str(socket_client), str(socket_path)], separators=(",", ":")),
            "-c", "mcp_servers.workday.enabled=true",
            "-c", "mcp_servers.workday.required=true",
            "-c", 'mcp_servers.workday.enabled_tools=["reply_to_operator","strategic_operation","project_read"]',
            "-c", 'mcp_servers.workday.default_tools_approval_mode="approve"',
        ]
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(scratch),
                "TMPDIR": str(scratch),
                "CODEX_HOME": str(scratch / "codex-home"),
            },
        )
        try:
            child.stdin.write(json.dumps({
                "method": "initialize", "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "blueprint_agent", "title": "Blueprint Agent", "version": "2.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }) + "\n")
            child.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
            child.stdin.flush()
            deadline = time.monotonic() + 3
            initialized = False
            while time.monotonic() < deadline:
                readable, _, _ = select.select([child.stdout], [], [], deadline - time.monotonic())
                if not readable:
                    break
                line = child.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if message.get("id") == 1:
                    if message.get("error") is not None or "result" not in message:
                        abort("Codex app-server initialize returned an error")
                    initialized = True
                    break
            if not initialized:
                abort("Codex app-server did not complete initialization")
        finally:
            stop_process_group(child)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()
        codex_started = True

    abi = int(os.environ["BLUEPRINT_RESIDENT_LANDLOCK"].rsplit("abi", 1)[1])
    print(json.dumps({
        "kind": MARKER_KIND,
        "abi": abi,
        "filesystem": True,
        "mcp": True,
        "codex_config": codex_started,
        "broker_transport": True,
        "credential_isolated": True,
        "three_tool_mcp": True,
        "codex_proxy_config": codex_started,
        "nested_namespace": False,
    }, separators=(",", ":")))
    return 0


def controller(options: argparse.Namespace) -> int:
    root = Path(options.runtime_root).resolve()
    if str(root) in {"/", "/tmp", "/workspace", "/home"}:
        abort("runtime root is too broad")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    scratch = root / "scratch"
    authority_state = root / "authority-state"
    repo = root / "denied-repository"
    mcp_dir = root / "mcp"
    socket_root = Path(tempfile.mkdtemp(prefix="blueprint-rmcp-", dir="/tmp"))
    socket_root.chmod(0o700)
    for directory in (scratch, authority_state, repo, mcp_dir):
        directory.mkdir(mode=0o700)
    credential = authority_state / "agent-token.jwt"
    (repo / "sentinel.txt").write_text("repository sentinel", encoding="utf-8")
    credential.write_text("credential sentinel", encoding="utf-8")
    receipt = authority_state / "reply-receipt.json"
    staged = authority_state / "staged-reply.json"
    receipt.write_text(json.dumps({
        "idempotency_key": "resident-landlock-preflight",
        "covered_message_ids": ["preflight-message"],
    }), encoding="utf-8")
    source_dir = Path(__file__).resolve().parent
    mcp_script = mcp_dir / "resident_reply_mcp.mjs"
    shutil.copyfile(source_dir / "resident_reply_mcp.mjs", mcp_script)
    mcp_script.chmod(0o500)
    socket_client = mcp_dir / "resident_mcp_socket_client.mjs"
    shutil.copyfile(source_dir / "resident_mcp_socket_client.mjs", socket_client)
    socket_client.chmod(0o500)
    helper_source = source_dir / "resident_landlock_preflight_helper.py"
    strategic_helper = mcp_dir / "resident_preflight_strategic_helper.py"
    project_read_helper = mcp_dir / "resident_preflight_project_read_helper.py"
    for helper in (strategic_helper, project_read_helper):
        shutil.copyfile(helper_source, helper)
        helper.chmod(0o500)
    strategic_ops = authority_state / "strategic-ops.json"
    strategic_ops.write_text(json.dumps({
        "set-running-update": {
            "backend_op": "set-running-update",
            "required": ["markdown"],
            "fields": ["markdown"],
        },
    }), encoding="utf-8")
    project_reads = authority_state / "project-reads.json"
    project_reads.write_text(json.dumps(["running_update"]), encoding="utf-8")
    private_marker = authority_state / "private-turn.json"
    private_marker.write_text(json.dumps({
        "kind": "typed-consultation",
        "incarnation_id": "resident-landlock-preflight",
        "packet_id": "preflight-packet",
        "workday_claim_id": "preflight-claim",
    }, separators=(",", ":")), encoding="utf-8")
    (scratch / "codex-home").mkdir(mode=0o700)

    python = os.path.realpath(sys.executable)
    node = shutil.which("node")
    if not node:
        abort("node is unavailable")
    socket_path = socket_root / "blueprint-resident-mcp-preflight.sock"
    broker_ready = threading.Event()
    broker_stop = threading.Event()
    broker_errors: list[str] = []
    broker_metrics = {"connections": 0, "active": 0}
    broker_env = {
        "PATH": f"{Path(node).resolve().parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "RESIDENT_REPLY_RECEIPT_PATH": str(receipt),
        "RESIDENT_STAGED_REPLY_PATH": str(staged),
        "RESIDENT_STRATEGIC_HELPER_PATH": str(strategic_helper),
        "RESIDENT_STRATEGIC_OPS_PATH": str(strategic_ops),
        "RESIDENT_PROJECT_READ_HELPER_PATH": str(project_read_helper),
        "RESIDENT_PROJECT_READS_PATH": str(project_reads),
        "AGENT_TOKEN_FILE": str(credential),
        "BLUEPRINT_RESIDENT_INCARNATION_ID": "resident-landlock-preflight",
        "BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH": str(private_marker),
    }
    broker_thread = threading.Thread(
        target=serve_broker,
        args=(socket_path, [os.path.realpath(node), str(mcp_script)], broker_env,
              broker_ready, broker_stop, broker_errors, broker_metrics),
        daemon=True,
    )
    broker_thread.start()
    if not broker_ready.wait(timeout=2) or broker_errors or not socket_path.exists():
        abort("private MCP broker failed to become ready")
    readonly = [
        path for path in (
            "/usr", "/usr/local", "/etc", str(source_dir), str(mcp_dir),
            str(socket_root),
            os.path.realpath(node),
        )
        if Path(path).exists()
    ]
    codex = options.codex
    if codex and not os.path.isabs(codex):
        codex = shutil.which(codex) or abort("Codex executable is unavailable")
    codex = os.path.realpath(codex) if codex else ""
    if codex and "/node_modules/" in codex:
        prefix, package_tail = codex.split("/node_modules/", 1)
        package_parts = package_tail.split("/")
        package_length = 2 if package_parts[0].startswith("@") else 1
        readonly.append(prefix + "/node_modules/" + "/".join(package_parts[:package_length]))
    elif codex:
        readonly.append(codex)
    policy = {
        "version": 1,
        "readonly_paths": readonly,
        "readwrite_paths": [str(scratch)],
        "deny_read_paths": [str(repo), str(authority_state), str(credential)],
        "deny_write_paths": [str(repo), str(authority_state), str(credential), str(mcp_dir)],
    }
    repository_root = Path(options.repository_root).resolve() if options.repository_root else None
    repository_sentinel = None
    if repository_root:
        if not repository_root.is_dir() or repository_root == source_dir or source_dir.is_relative_to(repository_root):
            abort("repository root is absent or overlaps the trusted preflight source")
        repository_sentinel = next(
            (entry for entry in repository_root.iterdir() if entry.is_file()), None,
        )
        if repository_sentinel is None:
            abort("repository root has no regular sentinel file")
        policy["deny_read_paths"].append(str(repository_root))
        policy["deny_write_paths"].append(str(repository_root))
    child_args = [
        python, "-I", str(Path(__file__).resolve()), "--restricted-child",
        "--scratch", str(scratch), "--repo", str(repo),
        "--credential", str(credential), "--mcp-dir", str(mcp_dir),
        "--authority-state", str(authority_state),
        "--receipt", str(receipt), "--staged", str(staged),
        "--node", os.path.realpath(node),
        "--socket", str(socket_path), "--socket-client", str(socket_client),
    ]
    if codex:
        child_args.extend(("--codex", codex))
    if repository_root and repository_sentinel:
        child_args.extend((
            "--repository-root", str(repository_root),
            "--repository-sentinel", str(repository_sentinel),
        ))
    path_entries = [str(Path(node).resolve().parent)]
    if codex:
        path_entries.append(str(Path(codex).resolve().parent))
    path_entries.extend(("/usr/local/bin", "/usr/bin", "/bin"))
    runtime_path = os.pathsep.join(dict.fromkeys(path_entries))
    try:
        result = subprocess.run(
            [
                python, "-I", str(source_dir / "resident_landlock_exec.py"),
                "--policy-json", json.dumps(policy, separators=(",", ":")),
                "--", *child_args,
            ],
            capture_output=True,
            text=True,
            timeout=options.timeout,
            env={
                "PATH": runtime_path,
                "HOME": str(scratch), "TMPDIR": str(scratch),
                "CODEX_HOME": str(scratch / "codex-home"),
            },
        )
    finally:
        broker_stop.set()
        broker_thread.join(timeout=3)
    if broker_thread.is_alive():
        abort("private MCP broker did not stop")
    shutil.rmtree(socket_root, ignore_errors=True)
    if broker_errors:
        abort(broker_errors[-1])
    # app-server parses the exact proxy config during initialize but opens MCP
    # lazily on a model turn; the quota-free direct client owns this connection.
    if broker_metrics != {"connections": 1, "active": 0}:
        abort(f"private MCP broker teardown receipt is invalid: {broker_metrics}")
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-2000:])
        return result.returncode
    staged_reply = json.loads(staged.read_text(encoding="utf-8"))
    if staged_reply != {
        "type": "NOTE",
        "content": "preflight reply",
        "idempotency_key": "resident-landlock-preflight",
        "covered_message_ids": ["preflight-message"],
    }:
        abort("MCP staged reply does not match the receipt-bound call")
    if "credential sentinel" in result.stdout or "credential sentinel" in result.stderr:
        abort("restricted preflight output exposed credential bytes")
    marker = json.loads(result.stdout.strip().splitlines()[-1])
    marker["broker_teardown"] = True
    print(json.dumps(marker, separators=(",", ":")))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root")
    parser.add_argument("--codex", default="")
    parser.add_argument("--repository-root", default="")
    parser.add_argument("--repository-sentinel", default="")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--restricted-child", action="store_true")
    parser.add_argument("--scratch")
    parser.add_argument("--repo")
    parser.add_argument("--credential")
    parser.add_argument("--authority-state")
    parser.add_argument("--mcp-dir")
    parser.add_argument("--receipt")
    parser.add_argument("--staged")
    parser.add_argument("--node")
    parser.add_argument("--socket")
    parser.add_argument("--socket-client")
    options = parser.parse_args()
    required = (
        "scratch", "repo", "credential", "authority_state", "mcp_dir",
        "receipt", "staged", "node", "socket", "socket_client",
    ) \
        if options.restricted_child else ("runtime_root",)
    for name in required:
        if not getattr(options, name):
            parser.error(f"--{name.replace('_', '-')} is required")
    return options


if __name__ == "__main__":
    opts = parse_args()
    raise SystemExit(run_restricted_child(opts) if opts.restricted_child else controller(opts))
