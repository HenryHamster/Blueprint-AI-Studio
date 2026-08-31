#!/usr/bin/env python3
"""Production-shape, quota-free preflight for the Resident Landlock boundary."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
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
SYSTEM_READONLY_ROOTS = (
    "/usr", "/usr/local", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/etc",
)
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apply_patch_freeform", "apps", "browser_use",
    "browser_use_external", "browser_use_full_cdp_access", "computer_use", "goals",
    "hooks", "image_generation", "in_app_browser", "multi_agent", "plugins",
    "remote_plugin", "skill_mcp_dependency_install", "skill_search", "tool_suggest",
    "view_image", "auth_elicitation", "tool_call_mcp_elicitation", "code_mode",
    "code_mode_host",
)


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


def start_responses_fixture() -> tuple[ThreadingHTTPServer, threading.Thread, list[dict]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            if self.path != "/v1/responses" or len(requests) >= 2:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            if not isinstance(payload, dict) or payload.get("stream") is not True:
                self.send_error(400)
                return
            requests.append(payload)
            response_id = f"resp_blueprint_preflight_{len(requests)}"
            item_id = f"msg_blueprint_preflight_{len(requests)}"
            model = str(payload.get("model") or "blueprint-preflight")
            output = [{
                "id": item_id, "type": "message", "status": "completed",
                "role": "assistant", "content": [{
                    "type": "output_text", "text": "Blueprint preflight turn complete.",
                    "annotations": [],
                }],
            }]
            response = {
                "id": response_id, "object": "response", "created_at": int(time.time()),
                "status": "completed", "error": None, "incomplete_details": None,
                "instructions": None, "model": model, "output": output,
                "parallel_tool_calls": False, "temperature": None,
                "tool_choice": "auto", "tools": [], "top_p": None,
                "usage": {
                    "input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 2,
                },
            }
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "response_id": response_id,
                 "output_index": 0, "item": {**output[0], "status": "in_progress", "content": []}},
                {"type": "response.output_item.done", "response_id": response_id,
                 "output_index": 0, "item": output[0]},
                {"type": "response.completed", "response": response},
            ]
            body = "".join(f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events)
            body += "data: [DONE]\n\n"
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests


def run_codex_turn(command: list[str], prompt: str, timeout: float) -> tuple[str, str]:
    try:
        result = subprocess.run(
            command, input=prompt, capture_output=True, text=True, timeout=timeout,
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": os.environ["HOME"], "TMPDIR": os.environ["TMPDIR"],
                "CODEX_HOME": os.environ["CODEX_HOME"],
                "BLUEPRINT_CODEX_PREFLIGHT_TOKEN": "quota-free-loopback-sentinel",
            },
        )
    except subprocess.TimeoutExpired as error:
        stderr = (error.stderr or b"") if isinstance(error.stderr, bytes) else (error.stderr or "")
        abort(f"Codex exec exceeded its bounded timeout: {str(stderr)[-500:]}")
    if result.returncode != 0:
        abort(f"Codex exec failed rc={result.returncode}: {result.stderr[-500:]}")
    try:
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    except json.JSONDecodeError:
        abort("Codex exec emitted invalid JSONL")
    thread_ids = [event.get("thread_id") for event in events if event.get("type") == "thread.started"]
    item_types = [
        event.get("item", {}).get("type") for event in events
        if event.get("type") in {"item.started", "item.updated", "item.completed"}
        and isinstance(event.get("item"), dict)
    ]
    if len(thread_ids) != 1 or not isinstance(thread_ids[0], str) or not thread_ids[0]:
        abort("Codex exec did not emit one exact thread receipt")
    if sum(event.get("type") == "turn.completed" for event in events) != 1:
        abort("Codex exec did not emit one terminal completion")
    if any(event.get("type") == "turn.failed" for event in events):
        abort("Codex exec emitted a failed terminal")
    if any(item in {"command_execution", "file_change", "web_search", "todo_list", "computer_use"}
           for item in item_types):
        abort("Codex preflight attempted a forbidden local tool")
    unsafe_stderr = ("unshare", "namespace", "failed to setup sandbox", "resident_landlock:")
    if any(marker in result.stderr.lower() for marker in unsafe_stderr):
        abort(f"Codex exec attempted nested sandboxing: {result.stderr[-500:]}")
    return thread_ids[0], result.stderr


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

    codex_exec_turn = False
    codex_resume_turn = False
    exact_thread_identity = False
    if options.codex:
        responses, responses_thread, response_requests = start_responses_fixture()
        provider_url = f"http://127.0.0.1:{responses.server_port}/v1"
        common = [
            "--json", "--skip-git-repo-check", "--ignore-user-config", "--strict-config",
            "--dangerously-bypass-approvals-and-sandbox",
            *(part for feature in CODEX_DISABLED_FEATURES for part in ("--disable", feature)),
            "-m", "blueprint-preflight",
            "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"',
            "-c", "tools.web_search=false",
            "-c", 'model_provider="blueprint_preflight"',
            "-c", 'model_providers.blueprint_preflight.name="Blueprint preflight"',
            "-c", f"model_providers.blueprint_preflight.base_url={json.dumps(provider_url)}",
            "-c", 'model_providers.blueprint_preflight.env_key="BLUEPRINT_CODEX_PREFLIGHT_TOKEN"',
            "-c", 'model_providers.blueprint_preflight.wire_api="responses"',
            "-c", "model_providers.blueprint_preflight.requires_openai_auth=false",
            "-c", "model_providers.blueprint_preflight.supports_websockets=false",
            "-c", "model_providers.blueprint_preflight.request_max_retries=0",
            "-c", "model_providers.blueprint_preflight.stream_max_retries=0",
            "-c", "mcp_servers={}",
            "-c", f"mcp_servers.workday.command={json.dumps(node)}",
            "-c", "mcp_servers.workday.args=" + json.dumps([str(socket_client), str(socket_path)], separators=(",", ":")),
            "-c", "mcp_servers.workday.enabled=true",
            "-c", "mcp_servers.workday.required=true",
            "-c", 'mcp_servers.workday.enabled_tools=["reply_to_operator","strategic_operation","project_read"]',
            "-c", 'mcp_servers.workday.default_tools_approval_mode="approve"',
            "-c", 'shell_environment_policy.inherit="none"',
            "-c", "shell_environment_policy.ignore_default_excludes=false",
            "-c", f"shell_environment_policy.set.HOME={json.dumps(str(scratch))}",
            "-c", f"shell_environment_policy.set.TMPDIR={json.dumps(str(scratch))}",
            "-c", 'developer_instructions="Use no local tools. Return the requested preflight text only."',
        ]
        try:
            codex = os.path.realpath(options.codex)
            thread_id, _ = run_codex_turn(
                [codex, "exec", *common, "-"], "Return the bounded preflight text.", 10,
            )
            codex_exec_turn = True
            resumed_id, _ = run_codex_turn(
                [codex, "exec", "resume", *common, thread_id, "-"],
                "Return the bounded resume preflight text.", 10,
            )
            codex_resume_turn = True
            exact_thread_identity = resumed_id == thread_id
            if not exact_thread_identity:
                abort("Codex resume changed the exact thread identity")
        finally:
            responses.shutdown()
            responses.server_close()
            responses_thread.join(timeout=1)
        if responses_thread.is_alive() or len(response_requests) != 2:
            abort("Codex loopback provider did not receive two bounded turns")
        for request in response_requests:
            tool_names = [
                str(tool.get("name") or tool.get("function", {}).get("name") or "")
                for tool in request.get("tools", []) if isinstance(tool, dict)
            ]
            if "mcp__workday" not in tool_names:
                abort(f"Codex exec omitted required MCP tools: {tool_names}")

    abi = int(os.environ["BLUEPRINT_RESIDENT_LANDLOCK"].rsplit("abi", 1)[1])
    print(json.dumps({
        "kind": MARKER_KIND,
        "abi": abi,
        "filesystem": True,
        "mcp": True,
        "codex_config": codex_exec_turn,
        "broker_transport": True,
        "credential_isolated": True,
        "three_tool_mcp": True,
        "codex_proxy_config": codex_exec_turn,
        "codex_exec_turn": codex_exec_turn,
        "codex_resume_turn": codex_resume_turn,
        "thread_identity": exact_thread_identity,
        "jsonl_terminal": codex_exec_turn and codex_resume_turn,
        "loopback_provider": codex_exec_turn and codex_resume_turn,
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
            *SYSTEM_READONLY_ROOTS, str(source_dir), str(mcp_dir),
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
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-2000:])
        return result.returncode
    # One direct tool-contract client plus each real Codex exec process must
    # initialize the required MCP server, and every connection must drain.
    if broker_metrics != {"connections": 3, "active": 0}:
        abort(f"private MCP broker teardown receipt is invalid: {broker_metrics}")
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
