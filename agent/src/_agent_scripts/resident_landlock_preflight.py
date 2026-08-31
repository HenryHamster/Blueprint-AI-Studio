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
GREETING_PROMPT = (
    "Initialize this long-lived Resident session. Do not call any tool. "
    "Reply with the exact greeting acknowledgement requested by the harness."
)
GREETING_RESULT = "Blueprint Resident greeting continuity preflight ready."
PRIVATE_PACKET_SENTINEL = "BLUEPRINT_PRIVATE_WORKDAY_PACKET_PREFLIGHT_V1"
PRIVATE_REPLY_CONTENT = "Blueprint Resident private packet native reply preflight passed."
PRIVATE_FINAL_RESULT = "Blueprint preflight private packet tool dispatch complete."
CODEX_PREFLIGHT_MODEL = "gpt-5.5"
CODEX_DIRECT_MCP_EXPOSURE = 'mcp_servers.workday.omit_tools_from=["deferred"]'
CODEX_RESIDENT_MCP_BOUNDARY = (
    "The only permitted MCP calls are reply_to_operator, strategic_operation, "
    "and project_read; they are already directly available. Never call "
    "list_mcp_resources, list_mcp_resource_templates, read_mcp_resource, "
    "tools/list, resources/list, resources/templates/list, resources/read, "
    "or any MCP discovery path."
)
FORBIDDEN_MCP_DISCOVERY_METHODS = {
    "resources/list", "resources/templates/list", "resources/read",
}
FORBIDDEN_MCP_DISCOVERY_TOOLS = {
    "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
    "mcp_server_discovery", "server_discovery",
}
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


def private_packet_call_specs() -> list[dict]:
    """Return the exact multi-step Workday packet exercised after greeting."""
    return [
        {
            "tool": "project_read",
            "arguments": {"resource": "running-update"},
        },
        {
            "tool": "strategic_operation",
            "arguments": {
                "operation": "set-running-update",
                "data": {"markdown": "# Private packet phase 1\nCanonical read completed."},
                "idempotency_key": "resident-private-packet-phase-1",
            },
        },
        {
            "tool": "project_read",
            "arguments": {"resource": "running-update"},
        },
        {
            "tool": "strategic_operation",
            "arguments": {
                "operation": "set-running-update",
                "data": {"markdown": "# Private packet phase 2\nPost-write read reconciled."},
                "idempotency_key": "resident-private-packet-phase-2",
            },
        },
        {
            "tool": "project_read",
            "arguments": {"resource": "running-update"},
        },
        {
            "tool": "reply_to_operator",
            "arguments": {
                "content": PRIVATE_REPLY_CONTENT,
                "message_type": "NOTE",
            },
        },
    ]


PRIVATE_PACKET_PROMPT = "\n".join((
    "This is a bounded private Workday packet on the already-open Resident thread.",
    f"Packet contract: {PRIVATE_PACKET_SENTINEL}",
    "Use only the three directly visible workday tools; do not search for MCP servers or resources.",
    "Execute this exact causal sequence without parallel calls:",
    "1. project_read running-update.",
    "2. strategic_operation set-running-update with phase-1 data and stable key.",
    "3. project_read running-update to reconcile the first receipt.",
    "4. strategic_operation set-running-update with phase-2 data and a distinct stable key.",
    "5. project_read running-update to reconcile the second receipt.",
    "6. reply_to_operator once with NOTE and the exact requested acceptance text.",
    "Never substitute ordinary assistant text for the native reply.",
    "The packet intentionally carries enough context to exercise long-turn continuation:",
    *(f"Context checkpoint {index:02d}: preserve exact thread and receipt ordering."
      for index in range(1, 25)),
))


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
            if self.path != "/v1/responses" or len(requests) >= 8:
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
            if request_number == 1:
                output = [{
                    "id": "msg_blueprint_greeting",
                    "type": "message", "status": "completed",
                    "role": "assistant", "content": [{
                        "type": "output_text",
                        "text": GREETING_RESULT,
                        "annotations": [],
                    }],
                }]
            elif request_number <= 1 + len(private_packet_call_specs()):
                call_index = request_number - 2
                spec = private_packet_call_specs()[call_index]
                tool = spec["tool"]
                output = [{
                    "id": f"fc_blueprint_private_{call_index + 1}",
                    "type": "function_call", "status": "completed",
                    "call_id": f"call_blueprint_private_{call_index + 1}",
                    "namespace": "mcp__workday", "name": tool,
                    "arguments": json.dumps(spec["arguments"], separators=(",", ":")),
                }]
            else:
                output = [{
                    "id": "msg_blueprint_private_complete",
                    "type": "message", "status": "completed",
                    "role": "assistant", "content": [{
                        "type": "output_text",
                        "text": PRIVATE_FINAL_RESULT,
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


def validate_strategic_receipt(value: object, arguments: dict, label: str) -> None:
    valid_fingerprint = False
    valid_operation_id = False
    if isinstance(value, dict):
        try:
            uuid.UUID(str(value.get("operation_id")))
            valid_operation_id = True
            valid_fingerprint = len(
                bytes.fromhex(str(value.get("operation_fingerprint")))
            ) == 32
        except ValueError:
            pass
    if (
        not isinstance(value, dict)
        or value.get("outcome") != "applied"
        or value.get("applied") is not True
        or value.get("commit_acknowledged") is not True
        or value.get("receipt_confirmed") is not True
        or value.get("operation") != arguments["operation"]
        or value.get("idempotency_key") != arguments["idempotency_key"]
        or not valid_operation_id
        or not valid_fingerprint
    ):
        abort(f"{label} returned an invalid strategic receipt; actual={bounded_json(value)}")


def parse_responses_tool_output(raw_output: object, label: str) -> object:
    if not isinstance(raw_output, str):
        abort(f"{label} omitted its structured MCP output")
    prefix = re.match(r'^Wall time: [0-9]+\.[0-9]{4} seconds\nOutput:\n', raw_output)
    try:
        return json.loads(raw_output[prefix.end():]) if prefix else None
    except json.JSONDecodeError:
        abort(f"{label} emitted invalid structured MCP output")


def validate_direct_workday_namespace(request: dict, request_number: int) -> None:
    tools = request.get("tools")
    namespace_tools = [
        tool for tool in tools or []
        if isinstance(tool, dict)
        and tool.get("type") == "namespace"
        and tool.get("name") == "mcp__workday"
    ] if isinstance(tools, list) else []
    if len(namespace_tools) != 1:
        abort(
            f"Codex request {request_number} omitted the direct Workday "
            f"namespace; actual={bounded_json(tools)}"
        )
    namespace = namespace_tools[0]
    children = namespace.get("tools")
    if not isinstance(namespace.get("description"), str) or not isinstance(children, list):
        abort(f"Codex request {request_number} Workday namespace is incomplete")
    child_names = [
        child.get("name") for child in children if isinstance(child, dict)
    ]
    if (
        len(child_names) != len(children)
        or len(set(child_names)) != len(child_names)
        or set(child_names)
        != {"reply_to_operator", "strategic_operation", "project_read"}
    ):
        abort(
            f"Codex request {request_number} exposed a non-exact Workday namespace; "
            f"actual={bounded_json(child_names)}"
        )
    expected_project_read_child = {
        "type": "function",
        "name": "project_read",
        "description": PROJECT_READ_DESCRIPTION,
        "strict": False,
        # Codex's Responses projection deliberately drops string length bounds;
        # direct MCP discovery below separately proves the upstream contract.
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
    for expected_child in (expected_project_read_child, expected_reply_child):
        if expected_child not in children:
            actual = [
                child for child in children
                if isinstance(child, dict) and child.get("name") == expected_child["name"]
            ]
            abort(
                f"Codex request {request_number} exposed the wrong "
                f"{expected_child['name']} schema; expected={bounded_json(expected_child)} "
                f"actual={bounded_json(actual)}"
            )
    input_items = request.get("input")
    if isinstance(input_items, list):
        for item in input_items:
            if not isinstance(item, dict):
                continue
            method = item.get("method")
            tool_name = item.get("name") or item.get("tool")
            if (
                method in FORBIDDEN_MCP_DISCOVERY_METHODS
                or tool_name in FORBIDDEN_MCP_DISCOVERY_TOOLS
                or item.get("type") in FORBIDDEN_MCP_DISCOVERY_TOOLS
            ):
                abort(
                    f"Codex request {request_number} attempted MCP resource/server "
                    f"discovery; item={bounded_json(item)}"
                )


def validate_responses_mcp_roundtrips(requests: list[dict]) -> None:
    expected_calls = private_packet_call_specs()
    expected_requests = len(expected_calls) + 2
    if len(requests) != expected_requests:
        abort(
            f"Codex loopback provider received {len(requests)} requests, "
            f"expected {expected_requests}"
        )
    for request_number, request in enumerate(requests, start=1):
        validate_direct_workday_namespace(request, request_number)
    greeting_wire = json.dumps(requests[0].get("input"), sort_keys=True)
    if GREETING_PROMPT not in greeting_wire:
        abort("Codex initial request omitted the exact greeting prompt")
    private_wire = json.dumps(requests[1].get("input"), sort_keys=True)
    if PRIVATE_PACKET_SENTINEL not in private_wire:
        abort("Codex resume request omitted the long private Workday packet")
    if GREETING_RESULT not in private_wire:
        abort("Codex resume request omitted the greeting continuity context")

    previous_output_index = -1
    for call_index, spec in enumerate(expected_calls):
        continuation = requests[call_index + 2]
        items = continuation.get("input")
        if not isinstance(items, list):
            abort(f"Codex private call {call_index + 1} continuation is not structured")
        call_id = f"call_blueprint_private_{call_index + 1}"
        calls = [
            (index, item) for index, item in enumerate(items)
            if isinstance(item, dict)
            and item.get("type") == "function_call"
            and item.get("call_id") == call_id
        ]
        outputs = [
            (index, item) for index, item in enumerate(items)
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == call_id
        ]
        if (
            len(calls) != 1
            or len(outputs) != 1
            or not (previous_output_index < calls[0][0] < outputs[0][0])
        ):
            abort(
                f"Codex private call {call_index + 1} did not round-trip in exact order"
            )
        call = calls[0][1]
        if any((
            call.get("id") != f"fc_blueprint_private_{call_index + 1}",
            call.get("namespace") != "mcp__workday",
            call.get("name") != spec["tool"],
            call.get("arguments")
            != json.dumps(spec["arguments"], separators=(",", ":")),
        )):
            abort(
                f"Codex private call {call_index + 1} changed the namespaced "
                f"{spec['tool']} call"
            )
        structured = parse_responses_tool_output(
            outputs[0][1].get("output"),
            f"Codex private call {call_index + 1} {spec['tool']}",
        )
        if spec["tool"] == "project_read":
            if structured != {"resource": "running-update", "bytes": 40}:
                abort(
                    f"Codex private call {call_index + 1} returned an invalid "
                    "project_read receipt"
                )
        elif spec["tool"] == "strategic_operation":
            validate_strategic_receipt(
                structured, spec["arguments"],
                f"Codex private call {call_index + 1}",
            )
        elif structured != {"accepted": True}:
            abort(
                f"Codex private call {call_index + 1} native reply was not accepted; "
                f"actual={bounded_json(structured)}"
            )
        previous_output_index = outputs[0][0]


def run_codex_turn(
    command: list[str],
    prompt: str,
    timeout: float,
    *,
    expected_calls: list[dict],
    expected_agent_message: str,
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
    expected_reply = next(
        (spec["arguments"]["content"] for spec in expected_calls
         if spec["tool"] == "reply_to_operator"),
        None,
    )
    if len(mcp_started) != len(expected_calls) or len(mcp_completed) != len(
        expected_calls
    ):
        if expected_reply and any(expected_reply in message for message in agent_messages):
            abort(
                f"Codex {turn_label} substituted ordinary text for the native "
                "reply_to_operator call"
            )
        abort(
            f"Codex {turn_label} emitted the wrong native MCP dispatch count; "
            f"expected={bounded_json([spec['tool'] for spec in expected_calls])} "
            f"started={bounded_json([item.get('tool') for _, item in mcp_started])} "
            f"completed={bounded_json([item.get('tool') for _, item in mcp_completed])}"
        )
    thread_index = next(
        index for index, event in enumerate(events)
        if event.get("type") == "thread.started"
    )
    terminal_index = next(
        index for index, event in enumerate(events)
        if event.get("type") == "turn.completed"
    )
    previous_completed_index = thread_index
    for call_number, (spec, started_receipt, completed_receipt) in enumerate(
        zip(expected_calls, mcp_started, mcp_completed, strict=True),
        start=1,
    ):
        started_index, started = started_receipt
        completed_index, completed = completed_receipt
        dispatch_id = started.get("id")
        if (
            not isinstance(dispatch_id, str)
            or not dispatch_id
            or dispatch_id != completed.get("id")
            or not (
                previous_completed_index < started_index < completed_index < terminal_index
            )
            or started.get("server") != "workday"
            or started.get("tool") != spec["tool"]
            or started.get("arguments") != spec["arguments"]
            or completed.get("server") != "workday"
            or completed.get("tool") != spec["tool"]
            or completed.get("status") != "completed"
            or completed.get("error") is not None
        ):
            abort(
                f"Codex {turn_label} call {call_number} emitted the wrong "
                f"{spec['tool']} identity or terminal; started={bounded_json(started)} "
                f"completed={bounded_json(completed)}"
            )
        result_payload = completed.get("result")
        if not isinstance(result_payload, dict) or result_payload.get("is_error") is True:
            abort(
                f"Codex {turn_label} call {call_number} omitted a successful "
                f"{spec['tool']} receipt; actual={bounded_json(result_payload)}"
            )
        structured = result_payload.get("structured_content")
        if spec["tool"] == "project_read":
            if (
                result_payload.get("content") != [{
                    "type": "text",
                    "text": '{"markdown": "preflight running update"}',
                }]
                or structured != {"resource": "running-update", "bytes": 40}
            ):
                abort(
                    f"Codex {turn_label} call {call_number} did not preserve the "
                    "project_read result"
                )
        elif spec["tool"] == "strategic_operation":
            validate_strategic_receipt(
                structured, spec["arguments"],
                f"Codex {turn_label} call {call_number}",
            )
        elif (
            result_payload.get("content") != [{
                "type": "text", "text": "Workday reply accepted for delivery.",
            }]
            or structured != {"accepted": True}
        ):
            abort(
                f"Codex {turn_label} call {call_number} native reply_to_operator "
                f"was rejected; actual={bounded_json(result_payload)}"
            )
        previous_completed_index = completed_index
    if agent_messages != [expected_agent_message]:
        abort(
            f"Codex {turn_label} emitted the wrong bounded agent acknowledgement; "
            f"actual={bounded_json(agent_messages)}"
        )
    if expected_reply and any(expected_reply in message for message in agent_messages):
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
    protocol_trace: list[dict],
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
            connection_number = metrics["connections"]
            connection_trace = {
                "connection": connection_number,
                "methods": [],
                "tool_calls": [],
                "parse_errors": 0,
            }
            protocol_trace.append(connection_trace)
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

            def input_relay(
                client: socket.socket = connection,
                child: subprocess.Popen = process,
                trace: dict = connection_trace,
            ) -> None:
                pending = b""
                try:
                    while data := client.recv(65_536):
                        child.stdin.write(data)
                        child.stdin.flush()
                        pending += data
                        while b"\n" in pending:
                            raw_line, pending = pending.split(b"\n", 1)
                            if not raw_line.strip():
                                continue
                            try:
                                request = json.loads(raw_line)
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                trace["parse_errors"] += 1
                                continue
                            method = request.get("method") if isinstance(request, dict) else None
                            if isinstance(method, str):
                                trace["methods"].append(method)
                            params = request.get("params") if isinstance(request, dict) else None
                            tool_name = params.get("name") if isinstance(params, dict) else None
                            if method == "tools/call" and isinstance(tool_name, str):
                                trace["tool_calls"].append(tool_name)
                    if pending.strip():
                        trace["parse_errors"] += 1
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    try:
                        child.stdin.close()
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
            # The resumed quota-free probe performs six sequential native
            # calls. Keep its broker lifetime below the controller's 24s gate
            # while allowing the exact long-packet sequence to drain.
            deadline = time.monotonic() + 15
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


def validate_broker_protocol_trace(protocol_trace: list[dict]) -> None:
    if len(protocol_trace) != 3:
        abort(
            f"private MCP broker recorded {len(protocol_trace)} protocol traces, "
            "expected three"
        )
    expected_tool_calls = (
        ["project_read", "strategic_operation", "reply_to_operator"],
        [],
        [spec["tool"] for spec in private_packet_call_specs()],
    )
    allowed_methods = {
        "initialize", "notifications/initialized", "tools/list", "tools/call", "ping",
    }
    for connection_number, (trace, expected_calls) in enumerate(
        zip(protocol_trace, expected_tool_calls, strict=True),
        start=1,
    ):
        methods = trace.get("methods")
        tool_calls = trace.get("tool_calls")
        if (
            trace.get("connection") != connection_number
            or trace.get("parse_errors") != 0
            or not isinstance(methods, list)
            or not isinstance(tool_calls, list)
        ):
            abort(
                f"private MCP connection {connection_number} trace is invalid; "
                f"actual={bounded_json(trace)}"
            )
        forbidden_methods = [
            method for method in methods
            if method in FORBIDDEN_MCP_DISCOVERY_METHODS or method not in allowed_methods
        ]
        forbidden_tools = [
            tool for tool in tool_calls if tool in FORBIDDEN_MCP_DISCOVERY_TOOLS
        ]
        if forbidden_methods or forbidden_tools:
            abort(
                f"private MCP connection {connection_number} attempted resource/server "
                f"discovery; methods={bounded_json(forbidden_methods)} "
                f"tools={bounded_json(forbidden_tools)}"
            )
        if methods.count("initialize") != 1 or methods.count("tools/list") != 1:
            abort(
                f"private MCP connection {connection_number} omitted its one bounded "
                f"tool handshake; methods={bounded_json(methods)}"
            )
        if tool_calls != expected_calls or methods.count("tools/call") != len(
            expected_calls
        ):
            abort(
                f"private MCP connection {connection_number} used the wrong direct "
                f"Workday calls; expected={bounded_json(expected_calls)} "
                f"actual={bounded_json(tool_calls)}"
            )


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
            # Use the production Resident model profile, whose native tool-search
            # capability is what defers external MCP tools unless explicitly
            # excluded below. A made-up model silently misses that failure mode.
            "-m", CODEX_PREFLIGHT_MODEL,
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
            "-c", CODEX_DIRECT_MCP_EXPOSURE,
            "-c", 'mcp_servers.workday.default_tools_approval_mode="approve"',
            "-c", 'shell_environment_policy.inherit="none"',
            "-c", "shell_environment_policy.ignore_default_excludes=false",
            "-c", f"shell_environment_policy.set.HOME={json.dumps(str(scratch))}",
            "-c", f"shell_environment_policy.set.TMPDIR={json.dumps(str(scratch))}",
            "-c", (
                'developer_instructions="Use no filesystem or shell tools. '
                'For the initialization greeting, call no tool and return only '
                'the requested greeting acknowledgement. For a private Workday '
                'packet, use only the three directly visible workday tools and '
                'execute its exact ordered read, strategic operation, reconciliation, '
                'and reply sequence. '
                f'{CODEX_RESIDENT_MCP_BOUNDARY} '
                'A normal assistant message never substitutes for reply_to_operator. '
                'After the native reply is accepted, return only the requested '
                'neutral completion acknowledgement."'
            ),
        ]
        try:
            codex = os.path.realpath(options.codex)
            thread_id, _, greeting_complete = run_codex_turn(
                [codex, "exec", *common, "-"],
                GREETING_PROMPT,
                7,
                expected_calls=[],
                expected_agent_message=GREETING_RESULT,
                turn_label="initial greeting",
            )
            codex_exec_turn = True
            resumed_id, _, private_packet_complete = run_codex_turn(
                [codex, "exec", "resume", *common, thread_id, "-"],
                PRIVATE_PACKET_PROMPT,
                14,
                expected_calls=private_packet_call_specs(),
                expected_agent_message=PRIVATE_FINAL_RESULT,
                turn_label="resumed private packet",
            )
            codex_resume_turn = True
            codex_mcp_dispatch = greeting_complete and private_packet_complete
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
        "codex_private_packet": codex_mcp_dispatch,
        "codex_no_mcp_discovery": codex_mcp_dispatch,
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
    broker_protocol_trace: list[dict] = []
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
              broker_ready, broker_stop, broker_errors, broker_metrics,
              broker_protocol_trace),
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
    validate_broker_protocol_trace(broker_protocol_trace)
    expected_staged_replies = {
        1: "preflight reply",
        3: PRIVATE_REPLY_CONTENT,
    }
    for connection_number, expected_content in expected_staged_replies.items():
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
    greeting_staged = Path(f"{staged}.connection-2")
    if greeting_staged.exists():
        abort("greeting connection unexpectedly staged an operator reply")
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
