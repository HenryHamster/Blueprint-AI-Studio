#!/usr/bin/env python3
"""Production-shape, quota-free preflight for the Resident Landlock boundary."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import NoReturn
import uuid


MARKER_KIND = "blueprint.resident_landlock_preflight.v1"
PROJECT_READ_DESCRIPTION = (
    "Read one bounded canonical Blueprint project resource for "
    "preconditions and post-mutation verification."
)
PROJECT_READ_MCP_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "resource": {"type": "string", "enum": ["running-update"]},
        "id": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["resource"],
    "additionalProperties": False,
}
REPLY_TO_OPERATOR_DESCRIPTION = (
    "Send the complete response to the active Workday operator message. "
    "This is the only action that delivers a Resident chat reply."
)
REPLY_TO_OPERATOR_MCP_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Complete user-facing reply for the operator.",
        },
        "message_type": {
            "type": "string",
            "enum": ["NOTE", "QUESTION", "FEEDBACK", "BUG_REPORT", "BLOCKER"],
            "description": "Optional Workday message type. Defaults to NOTE.",
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}
INITIAL_REPLY_CONTENT = "Blueprint Resident initial native reply preflight passed."
RESUME_REPLY_CONTENT = "Blueprint Resident resumed native reply preflight passed."
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


def bounded_json(value: object, limit: int = 1200) -> str:
    """Render non-secret protocol shapes without flooding runner diagnostics."""
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[:limit]}...[truncated {len(rendered) - limit} chars]"


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
            if self.path != "/v1/responses" or len(requests) >= 6:
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
            request_number = len(requests)
            response_id = f"resp_blueprint_preflight_{request_number}"
            model = str(payload.get("model") or "blueprint-preflight")
            logical_turn = (request_number + 2) // 3
            turn_stage = (request_number - 1) % 3
            if turn_stage == 0:
                output = [{
                    "id": f"fc_blueprint_project_read_{logical_turn}",
                    "type": "function_call", "status": "completed",
                    "call_id": f"call_blueprint_project_read_{logical_turn}",
                    "namespace": "mcp__workday", "name": "project_read",
                    "arguments": '{"resource":"running-update"}',
                }]
            elif turn_stage == 1:
                reply_content = (
                    INITIAL_REPLY_CONTENT if logical_turn == 1 else RESUME_REPLY_CONTENT
                )
                output = [{
                    "id": f"fc_blueprint_reply_{logical_turn}",
                    "type": "function_call", "status": "completed",
                    "call_id": f"call_blueprint_reply_{logical_turn}",
                    "namespace": "mcp__workday", "name": "reply_to_operator",
                    "arguments": json.dumps({
                        "content": reply_content,
                        "message_type": "NOTE",
                    }, separators=(",", ":")),
                }]
            else:
                output = [{
                    "id": f"msg_blueprint_preflight_{logical_turn}",
                    "type": "message", "status": "completed",
                    "role": "assistant", "content": [{
                        "type": "output_text",
                        "text": "Blueprint preflight native tool dispatch complete.",
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
            events = [{
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            }]
            if output[0]["type"] == "message":
                events.append({
                    "type": "response.output_item.added", "response_id": response_id,
                    "output_index": 0,
                    "item": {**output[0], "status": "in_progress", "content": []},
                })
            events.extend([
                {"type": "response.output_item.done", "response_id": response_id,
                 "output_index": 0, "item": output[0]},
                {"type": "response.completed", "response": response},
            ])
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


def validate_responses_mcp_roundtrips(requests: list[dict]) -> None:
    if len(requests) != 6:
        abort(f"Codex loopback provider received {len(requests)} requests, expected six")
    expected_project_read_child = {
        "type": "function",
        "name": "project_read",
        "description": PROJECT_READ_DESCRIPTION,
        "strict": False,
        # Codex's Responses namespace projection preserves the accepted field
        # names and resource enum, but deliberately removes minLength and
        # maxLength. The direct MCP discovery check below separately proves
        # that the upstream server still advertises the bounded id contract.
        "parameters": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "enum": ["running-update"]},
                "id": {"type": "string"},
            },
            "required": ["resource"],
            "additionalProperties": False,
        },
    }
    expected_reply_child = {
        "type": "function",
        "name": "reply_to_operator",
        "description": REPLY_TO_OPERATOR_DESCRIPTION,
        "strict": False,
        "parameters": REPLY_TO_OPERATOR_MCP_INPUT_SCHEMA,
    }
    expected_output_prefix = re.compile(
        r'^Wall time: [0-9]+\.[0-9]{4} seconds\nOutput:\n'
    )
    for logical_turn, offset in enumerate((0, 3), start=1):
        initial = requests[offset]
        project_continuation = requests[offset + 1]
        reply_continuation = requests[offset + 2]
        namespace_tools = [
            tool for tool in initial.get("tools", [])
            if isinstance(tool, dict)
            and tool.get("type") == "namespace"
            and tool.get("name") == "mcp__workday"
        ]
        if len(namespace_tools) != 1:
            abort(f"Codex turn {logical_turn} omitted the exact Workday namespace")
        namespace = namespace_tools[0]
        if not isinstance(namespace.get("description"), str):
            abort(f"Codex turn {logical_turn} Workday namespace has no description")
        children = namespace.get("tools")
        if not isinstance(children, list) or expected_project_read_child not in children:
            actual_children = [
                child for child in children or []
                if isinstance(child, dict) and child.get("name") == "project_read"
            ] if isinstance(children, list) else children
            abort(
                f"Codex turn {logical_turn} exposed the wrong project_read schema; "
                f"expected={bounded_json(expected_project_read_child)} "
                f"actual={bounded_json(actual_children)}"
            )
        if expected_reply_child not in children:
            actual_children = [
                child for child in children
                if isinstance(child, dict) and child.get("name") == "reply_to_operator"
            ]
            abort(
                f"Codex turn {logical_turn} exposed the wrong reply_to_operator schema; "
                f"expected={bounded_json(expected_reply_child)} "
                f"actual={bounded_json(actual_children)}"
            )
        child_names = [
            child.get("name") for child in children if isinstance(child, dict)
        ]
        if (
            len(child_names) != len(children)
            or len(set(child_names)) != len(child_names)
            or set(child_names)
            != {"reply_to_operator", "strategic_operation", "project_read"}
        ):
            abort(f"Codex turn {logical_turn} exposed an unbounded Workday namespace")

        project_items = project_continuation.get("input")
        reply_items = reply_continuation.get("input")
        if not isinstance(project_items, list) or not isinstance(reply_items, list):
            abort(f"Codex turn {logical_turn} continuation input is not structured")
        project_call_id = f"call_blueprint_project_read_{logical_turn}"
        project_calls = [
            (index, item) for index, item in enumerate(project_items)
            if isinstance(item, dict)
            and item.get("type") == "function_call"
            and item.get("call_id") == project_call_id
        ]
        project_outputs = [
            (index, item) for index, item in enumerate(project_items)
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == project_call_id
        ]
        if (
            len(project_calls) != 1
            or len(project_outputs) != 1
            or project_calls[0][0] >= project_outputs[0][0]
        ):
            abort(f"Codex turn {logical_turn} did not round-trip project_read")
        call, output = project_calls[0][1], project_outputs[0][1]
        if any((
            call.get("id") != f"fc_blueprint_project_read_{logical_turn}",
            call.get("namespace") != "mcp__workday",
            call.get("name") != "project_read",
            call.get("arguments") != '{"resource":"running-update"}',
        )):
            abort(f"Codex turn {logical_turn} changed the namespaced function call")
        raw_output = output.get("output")
        if not isinstance(raw_output, str):
            abort(f"Codex turn {logical_turn} returned an invalid MCP output receipt")
        prefix = expected_output_prefix.match(raw_output)
        try:
            structured_output = json.loads(raw_output[prefix.end():]) if prefix else None
        except json.JSONDecodeError:
            structured_output = None
        if structured_output != {"resource": "running-update", "bytes": 40}:
            abort(f"Codex turn {logical_turn} returned an invalid project_read receipt")

        reply_call_id = f"call_blueprint_reply_{logical_turn}"
        reply_calls = [
            (index, item) for index, item in enumerate(reply_items)
            if isinstance(item, dict)
            and item.get("type") == "function_call"
            and item.get("call_id") == reply_call_id
        ]
        reply_outputs = [
            (index, item) for index, item in enumerate(reply_items)
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == reply_call_id
        ]
        retained_project_outputs = [
            (index, item) for index, item in enumerate(reply_items)
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == project_call_id
        ]
        if (
            len(reply_calls) != 1
            or len(reply_outputs) != 1
            or len(retained_project_outputs) != 1
            or not (
                retained_project_outputs[0][0]
                < reply_calls[0][0]
                < reply_outputs[0][0]
            )
        ):
            abort(
                f"Codex turn {logical_turn} did not round-trip one ordered "
                "reply_to_operator result"
            )
        reply_call, reply_output = reply_calls[0][1], reply_outputs[0][1]
        expected_reply_content = (
            INITIAL_REPLY_CONTENT if logical_turn == 1 else RESUME_REPLY_CONTENT
        )
        if any((
            reply_call.get("id") != f"fc_blueprint_reply_{logical_turn}",
            reply_call.get("namespace") != "mcp__workday",
            reply_call.get("name") != "reply_to_operator",
            reply_call.get("arguments") != json.dumps({
                "content": expected_reply_content,
                "message_type": "NOTE",
            }, separators=(",", ":")),
        )):
            abort(f"Codex turn {logical_turn} changed the native reply function call")
        raw_reply_output = reply_output.get("output")
        if not isinstance(raw_reply_output, str):
            abort(f"Codex turn {logical_turn} omitted the native reply receipt")
        reply_prefix = expected_output_prefix.match(raw_reply_output)
        try:
            structured_reply = (
                json.loads(raw_reply_output[reply_prefix.end():])
                if reply_prefix else None
            )
        except json.JSONDecodeError:
            structured_reply = None
        if structured_reply != {"accepted": True}:
            abort(
                f"Codex turn {logical_turn} native reply was not accepted; "
                f"actual={bounded_json(structured_reply)}"
            )


def run_codex_turn(
    command: list[str],
    prompt: str,
    timeout: float,
    *,
    reply_content: str,
    turn_label: str,
) -> tuple[str, str, bool]:
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
    if any(item not in {"reasoning", "agent_message", "mcp_tool_call", "error"}
           for item in item_types):
        abort("Codex preflight attempted a forbidden local tool")
    mcp_started = [
        (index, event["item"]) for index, event in enumerate(events)
        if event.get("type") == "item.started"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "mcp_tool_call"
    ]
    mcp_completed = [
        (index, event["item"]) for index, event in enumerate(events)
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "mcp_tool_call"
    ]
    agent_messages = [
        event["item"].get("text") for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
        and isinstance(event["item"].get("text"), str)
    ]
    if len(mcp_started) != 2 or len(mcp_completed) != 2:
        if any(reply_content in message for message in agent_messages):
            abort(
                f"Codex {turn_label} substituted ordinary text for the native "
                "reply_to_operator call"
            )
        abort(
            f"Codex {turn_label} omitted an exact native MCP dispatch; "
            f"started={bounded_json([item.get('tool') for _, item in mcp_started])} "
            f"completed={bounded_json([item.get('tool') for _, item in mcp_completed])}"
        )
    started_by_tool = {item.get("tool"): (index, item) for index, item in mcp_started}
    completed_by_tool = {item.get("tool"): (index, item) for index, item in mcp_completed}
    if set(started_by_tool) != {"project_read", "reply_to_operator"} or set(
        completed_by_tool
    ) != {"project_read", "reply_to_operator"}:
        abort(
            f"Codex {turn_label} dispatched the wrong native MCP tools; "
            f"started={bounded_json(sorted(str(key) for key in started_by_tool))} "
            f"completed={bounded_json(sorted(str(key) for key in completed_by_tool))}"
        )
    project_started_index, project_started = started_by_tool["project_read"]
    project_completed_index, project_completed = completed_by_tool["project_read"]
    reply_started_index, reply_started = started_by_tool["reply_to_operator"]
    reply_completed_index, reply_completed = completed_by_tool["reply_to_operator"]
    thread_index = next(
        index for index, event in enumerate(events)
        if event.get("type") == "thread.started"
    )
    terminal_index = next(
        index for index, event in enumerate(events)
        if event.get("type") == "turn.completed"
    )
    project_dispatch_id = project_started.get("id")
    if (
        not isinstance(project_dispatch_id, str)
        or not project_dispatch_id
        or project_dispatch_id != project_completed.get("id")
        or not (
            thread_index
            < project_started_index
            < project_completed_index
            < reply_started_index
            < reply_completed_index
            < terminal_index
        )
        or project_started.get("server") != "workday"
        or project_started.get("arguments") != {"resource": "running-update"}
        or project_completed.get("server") != "workday"
        or project_completed.get("status") != "completed"
        or project_completed.get("error") is not None
    ):
        abort(f"Codex {turn_label} emitted the wrong project_read identity or terminal")
    result_payload = project_completed.get("result")
    if not isinstance(result_payload, dict):
        abort("Codex exec MCP receipt omitted its structured result")
    content = result_payload.get("content")
    if (
        content != [{
            "type": "text",
            "text": '{"markdown": "preflight running update"}',
        }]
        or result_payload.get("is_error") is True
        or result_payload.get("structured_content")
        != {"resource": "running-update", "bytes": 40}
    ):
        abort(f"Codex {turn_label} did not preserve the project_read result")
    reply_dispatch_id = reply_started.get("id")
    if (
        not isinstance(reply_dispatch_id, str)
        or not reply_dispatch_id
        or reply_dispatch_id != reply_completed.get("id")
        or reply_started.get("server") != "workday"
        or reply_started.get("arguments") != {
            "content": reply_content,
            "message_type": "NOTE",
        }
        or reply_completed.get("server") != "workday"
        or reply_completed.get("status") != "completed"
        or reply_completed.get("error") is not None
    ):
        abort(
            f"Codex {turn_label} emitted the wrong reply_to_operator identity or terminal; "
            f"started={bounded_json(reply_started)} completed={bounded_json(reply_completed)}"
        )
    reply_result = reply_completed.get("result")
    if not isinstance(reply_result, dict):
        abort(f"Codex {turn_label} native reply receipt omitted its structured result")
    if (
        reply_result.get("content") != [{
            "type": "text", "text": "Workday reply accepted for delivery.",
        }]
        or reply_result.get("is_error") is True
        or reply_result.get("structured_content") != {"accepted": True}
    ):
        abort(
            f"Codex {turn_label} native reply_to_operator call was rejected; "
            f"actual={bounded_json(reply_result)}"
        )
    if any(reply_content in message for message in agent_messages):
        abort(
            f"Codex {turn_label} duplicated the native reply as ordinary agent text"
        )
    unsafe_stderr = ("unshare", "namespace", "failed to setup sandbox", "resident_landlock:")
    if any(marker in result.stderr.lower() for marker in unsafe_stderr):
        abort(f"Codex exec attempted nested sandboxing: {result.stderr[-500:]}")
    return thread_ids[0], result.stderr, True


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
            connection_env = dict(upstream_env)
            staged_reply_path = connection_env.get("RESIDENT_STAGED_REPLY_PATH", "")
            if staged_reply_path:
                connection_env["RESIDENT_STAGED_REPLY_PATH"] = (
                    f"{staged_reply_path}.connection-{metrics['connections']}"
                )
            process = subprocess.Popen(
                upstream,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=connection_env,
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
            "name": "project_read", "arguments": {"resource": "running-update"},
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
    strategic_tool = next(
        (tool for tool in tools if tool.get("name") == "strategic_operation"),
        None,
    )
    project_read_tool = next(
        (tool for tool in tools if tool.get("name") == "project_read"),
        None,
    )
    reply_to_operator_tool = next(
        (tool for tool in tools if tool.get("name") == "reply_to_operator"),
        None,
    )
    expected_project_read_tool = {
        "name": "project_read",
        "description": PROJECT_READ_DESCRIPTION,
        "inputSchema": PROJECT_READ_MCP_INPUT_SCHEMA,
    }
    if project_read_tool != expected_project_read_tool:
        abort(
            "MCP project_read discovery schema is incomplete; "
            f"expected={bounded_json(expected_project_read_tool)} "
            f"actual={bounded_json(project_read_tool)}"
        )
    expected_reply_to_operator_tool = {
        "name": "reply_to_operator",
        "description": REPLY_TO_OPERATOR_DESCRIPTION,
        "inputSchema": REPLY_TO_OPERATOR_MCP_INPUT_SCHEMA,
    }
    if reply_to_operator_tool != expected_reply_to_operator_tool:
        abort(
            "MCP reply_to_operator discovery schema is incomplete; "
            f"expected={bounded_json(expected_reply_to_operator_tool)} "
            f"actual={bounded_json(reply_to_operator_tool)}"
        )
    contract_marker = "contract-json="
    contract_description = (
        strategic_tool.get("inputSchema", {})
        .get("properties", {})
        .get("data", {})
        .get("description", "")
    ) if isinstance(strategic_tool, dict) else ""
    try:
        contract_offset = contract_description.index(contract_marker)
        advertised_contracts = json.loads(
            contract_description[contract_offset + len(contract_marker):]
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        abort("MCP strategic discovery contract is invalid")
    if advertised_contracts.get("set-running-update") != {
        "required": ["markdown"],
        "fields": ["markdown"],
        "requirements": ["markdown must be a string."],
        "example": {"markdown": "# Current status\n..."},
    }:
        abort("MCP strategic discovery contract is incomplete")
    expected_project = json.dumps({"markdown": "preflight running update"})
    if (
        project_read.get("resource") != "running-update"
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
    codex_mcp_dispatch = False
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
            "-c", (
                'developer_instructions="Use no filesystem or shell tools. '
                'Call workday.project_read exactly once for running-update, '
                'then call workday.reply_to_operator exactly once with the '
                'message_type and content requested by the prompt. A normal '
                'assistant message never substitutes for reply_to_operator. '
                'After the reply is accepted, return only a neutral completion '
                'acknowledgement and do not repeat the reply content."'
            ),
        ]
        try:
            codex = os.path.realpath(options.codex)
            thread_id, _, exec_mcp = run_codex_turn(
                [codex, "exec", *common, "-"],
                "Read running-update once, then deliver this reply using the native "
                "reply_to_operator tool with message_type NOTE: "
                f"{INITIAL_REPLY_CONTENT}",
                10,
                reply_content=INITIAL_REPLY_CONTENT,
                turn_label="initial turn",
            )
            codex_exec_turn = True
            resumed_id, _, resume_mcp = run_codex_turn(
                [codex, "exec", "resume", *common, thread_id, "-"],
                "Read running-update once, then deliver this reply using the native "
                "reply_to_operator tool with message_type NOTE: "
                f"{RESUME_REPLY_CONTENT}",
                10,
                reply_content=RESUME_REPLY_CONTENT,
                turn_label="resumed turn",
            )
            codex_resume_turn = True
            codex_mcp_dispatch = exec_mcp and resume_mcp
            exact_thread_identity = resumed_id == thread_id
            if not exact_thread_identity:
                abort("Codex resume changed the exact thread identity")
        finally:
            responses.shutdown()
            responses.server_close()
            responses_thread.join(timeout=1)
        if responses_thread.is_alive():
            abort("Codex loopback provider did not stop")
        validate_responses_mcp_roundtrips(response_requests)

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
        "codex_mcp_dispatch": codex_mcp_dispatch,
        "codex_native_reply": codex_mcp_dispatch,
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
            "backend_op": "set_running_update",
            "required": ["markdown"],
            "fields": ["markdown"],
            "requirements": ["markdown must be a string."],
            "example": {"markdown": "# Current status\n..."},
        },
    }), encoding="utf-8")
    project_reads = authority_state / "project-reads.json"
    project_reads.write_text(json.dumps(["running-update"]), encoding="utf-8")
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
    expected_staged_replies = (
        "preflight reply",
        INITIAL_REPLY_CONTENT,
        RESUME_REPLY_CONTENT,
    )
    for connection_number, expected_content in enumerate(
        expected_staged_replies, start=1,
    ):
        staged_connection = Path(f"{staged}.connection-{connection_number}")
        try:
            staged_reply = json.loads(staged_connection.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            abort(
                f"MCP connection {connection_number} omitted its staged native reply: "
                f"{error}"
            )
        if staged_reply != {
            "type": "NOTE",
            "content": expected_content,
            "idempotency_key": "resident-landlock-preflight",
            "covered_message_ids": ["preflight-message"],
        }:
            abort(
                f"MCP connection {connection_number} staged the wrong receipt-bound "
                f"native reply; actual={bounded_json(staged_reply)}"
            )
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
