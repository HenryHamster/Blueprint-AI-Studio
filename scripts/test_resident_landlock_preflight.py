"""Focused regressions for the quota-free real-Codex Resident preflight."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock
import urllib.error
import urllib.request


SOURCE = (
    Path(__file__).parents[1]
    / "agent/src/_agent_scripts/resident_landlock_preflight.py"
)
SPEC = importlib.util.spec_from_file_location("resident_landlock_preflight", SOURCE)
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


def workday_namespace():
    project_read = {
        "type": "function", "name": "project_read",
        "description": PREFLIGHT.PROJECT_READ_DESCRIPTION, "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "enum": ["running-update"]},
                "id": {"type": "string"},
            },
            "required": ["resource"], "additionalProperties": False,
        },
    }
    reply = {
        "type": "function", "name": "reply_to_operator",
        "description": PREFLIGHT.REPLY_TO_OPERATOR_DESCRIPTION,
        "strict": False, "parameters": PREFLIGHT.REPLY_TO_OPERATOR_MCP_INPUT_SCHEMA,
    }
    namespace = {
        "type": "namespace", "name": "mcp__workday", "description": "Workday",
        "tools": [
            reply, {"type": "function", "name": "strategic_operation"}, project_read,
        ],
    }
    discovery_helpers = [
        {"type": "function", "name": name, "strict": False, "parameters": {}}
        for name in (
            "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
        )
    ]
    return [*discovery_helpers, namespace]


def strategic_receipt(arguments, sequence):
    return {
        "outcome": "applied", "applied": True,
        "commit_acknowledged": True, "receipt_confirmed": True,
        "operation": arguments["operation"],
        "idempotency_key": arguments["idempotency_key"],
        "operation_id": f"00000000-0000-5000-8000-{sequence:012d}",
        "operation_fingerprint": "ab" * 32,
    }


def responses_packets():
    tools = workday_namespace()
    requests = [
        {"tools": copy.deepcopy(tools), "input": PREFLIGHT.GREETING_PROMPT},
        {
            "tools": copy.deepcopy(tools),
            "input": [
                {"role": "assistant", "content": PREFLIGHT.GREETING_RESULT},
                {"role": "user", "content": PREFLIGHT.PRIVATE_PACKET_PROMPT},
            ],
        },
    ]
    retained = copy.deepcopy(requests[1]["input"])
    for call_number, spec in enumerate(PREFLIGHT.private_packet_call_specs(), start=1):
        call_id = f"call_blueprint_private_{call_number}"
        call = {
            "id": f"fc_blueprint_private_{call_number}", "type": "function_call",
            "call_id": call_id, "namespace": "mcp__workday", "name": spec["tool"],
            "arguments": json.dumps(spec["arguments"], separators=(",", ":")),
        }
        if spec["tool"] == "project_read":
            structured = {"bytes": 40, "resource": "running-update"}
        elif spec["tool"] == "strategic_operation":
            structured = strategic_receipt(spec["arguments"], call_number)
        else:
            structured = {"accepted": True}
        output = {
            "type": "function_call_output", "call_id": call_id,
            "output": (
                "Wall time: 0.0001 seconds\nOutput:\n"
                + json.dumps(structured, separators=(",", ":"))
            ),
        }
        retained.extend((call, output))
        requests.append({
            "tools": copy.deepcopy(tools), "input": copy.deepcopy(retained),
        })
    return requests


def jsonl_private_events(*, rejected_reply=False):
    events = [{"type": "thread.started", "thread_id": "thread-private"}]
    for call_number, spec in enumerate(PREFLIGHT.private_packet_call_specs(), start=1):
        item_id = f"mcp-private-{call_number}"
        base = {
            "id": item_id, "type": "mcp_tool_call", "server": "workday",
            "tool": spec["tool"], "arguments": spec["arguments"],
        }
        events.append({"type": "item.started", "item": {
            **base, "status": "in_progress",
        }})
        if spec["tool"] == "project_read":
            result = {
                "content": [{
                    "type": "text", "text": '{"markdown": "preflight running update"}',
                }],
                "structured_content": {"resource": "running-update", "bytes": 40},
            }
        elif spec["tool"] == "strategic_operation":
            result = {
                "content": [{"type": "text", "text": "applied"}],
                "structured_content": strategic_receipt(spec["arguments"], call_number),
            }
        else:
            result = {
                "content": [{
                    "type": "text",
                    "text": (
                        "not accepted" if rejected_reply
                        else "Workday reply accepted for delivery."
                    ),
                }],
                "structured_content": {"accepted": not rejected_reply},
            }
        events.append({"type": "item.completed", "item": {
            **base, "status": "completed", "error": None, "result": result,
        }})
    events.extend((
        {"type": "item.completed", "item": {
            "id": "answer", "type": "agent_message",
            "text": PREFLIGHT.PRIVATE_FINAL_RESULT,
        }},
        {"type": "turn.completed"},
    ))
    return events


class TestResidentLandlockPreflight(unittest.TestCase):
    def test_loopback_fixture_serves_greeting_then_long_private_packet(self):
        server, thread, requests = PREFLIGHT.start_responses_fixture()
        url = f"http://127.0.0.1:{server.server_port}/v1/responses"
        try:
            response_bodies = []
            for turn in range(8):
                request = urllib.request.Request(
                    url,
                    data=json.dumps({
                        "model": "gpt-5.5", "stream": True, "input": f"turn {turn}",
                    }).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    response_bodies.append(response.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(urllib.request.Request(
                    url, data=b'{"model":"x","stream":true}', method="POST",
                ), timeout=2)
            self.assertEqual(rejected.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(requests), 8)
        self.assertIn('"type":"message"', response_bodies[0])
        expected_names = [spec["tool"] for spec in PREFLIGHT.private_packet_call_specs()]
        for offset, name in enumerate(expected_names, start=1):
            self.assertIn(f'"name":"{name}"', response_bodies[offset])
        self.assertIn('"type":"message"', response_bodies[7])

    def test_direct_tool_contract_and_long_packet_are_exact(self):
        self.assertEqual(PREFLIGHT.CODEX_PREFLIGHT_MODEL, "gpt-5.5")
        self.assertEqual(
            PREFLIGHT.CODEX_DIRECT_MCP_EXPOSURE,
            'mcp_servers.workday.omit_tools_from=["deferred"]',
        )
        for forbidden in (
            "list_mcp_resources", "list_mcp_resource_templates",
            "read_mcp_resource", "tools/list", "resources/list",
            "resources/templates/list", "resources/read",
        ):
            self.assertIn(forbidden, PREFLIGHT.CODEX_RESIDENT_MCP_BOUNDARY)
        self.assertIn(PREFLIGHT.PRIVATE_PACKET_SENTINEL, PREFLIGHT.PRIVATE_PACKET_PROMPT)
        self.assertGreater(len(PREFLIGHT.PRIVATE_PACKET_PROMPT), 1_000)
        calls = PREFLIGHT.private_packet_call_specs()
        self.assertEqual([call["tool"] for call in calls], [
            "project_read", "strategic_operation", "project_read",
            "strategic_operation", "project_read", "reply_to_operator",
        ])
        self.assertEqual(len({
            call["arguments"]["idempotency_key"]
            for call in calls if call["tool"] == "strategic_operation"
        }), 2)
        self.assertEqual(PREFLIGHT.PROJECT_READ_MCP_INPUT_SCHEMA["properties"]["id"], {
            "type": "string", "minLength": 1, "maxLength": 200,
        })

    def test_responses_roundtrip_preserves_greeting_and_private_tool_sequence(self):
        packets = responses_packets()
        PREFLIGHT.validate_responses_mcp_roundtrips(packets)
        rejected = copy.deepcopy(packets)
        rejected[-1]["input"][-1]["output"] = (
            'Wall time: 0.0001 seconds\nOutput:\n{"accepted":false}'
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            PREFLIGHT.validate_responses_mcp_roundtrips(rejected)
        self.assertEqual(raised.exception.code, 126)
        self.assertIn("native reply was not accepted", stderr.getvalue())

    def test_responses_reject_discovery_or_bounded_schema_drift(self):
        discovery = responses_packets()
        discovery[1]["input"].append({
            "type": "list_mcp_resources", "name": "list_mcp_resources",
        })
        extra_tool = responses_packets()
        extra_tool[0]["tools"][-1]["tools"].append({
            "type": "function", "name": "unexpected_local_tool",
        })
        schema_drift = responses_packets()
        schema_drift[0]["tools"][-1]["tools"][2]["description"] = "x" * 5_000
        cases = [
            (discovery, "attempted MCP resource/server discovery"),
            (extra_tool, "non-exact Workday namespace"),
            (schema_drift, "wrong project_read schema"),
        ]
        for packets, diagnostic in cases:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as raised:
                PREFLIGHT.validate_responses_mcp_roundtrips(packets)
            self.assertEqual(raised.exception.code, 126)
            self.assertIn(diagnostic, stderr.getvalue())
            self.assertLess(len(stderr.getvalue()), 2_500)

    def test_jsonl_private_packet_requires_all_native_receipts(self):
        stdout = "\n".join(json.dumps(event) for event in jsonl_private_events())
        completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
            "CODEX_HOME": "/tmp/preflight-home/codex-home",
        }, clear=False), mock.patch.object(
            PREFLIGHT.subprocess, "run", return_value=completed
        ):
            thread_id, stderr, dispatched = PREFLIGHT.run_codex_turn(
                ["codex"], "prompt", 1,
                expected_calls=PREFLIGHT.private_packet_call_specs(),
                expected_agent_message=PREFLIGHT.PRIVATE_FINAL_RESULT,
                turn_label="resumed private packet",
            )
        self.assertEqual(thread_id, "thread-private")
        self.assertEqual(stderr, "")
        self.assertTrue(dispatched)

    def test_jsonl_preflight_rejects_missing_rejected_or_substituted_reply(self):
        missing = jsonl_private_events()
        del missing[-3:-1]
        substituted = [
            {"type": "thread.started", "thread_id": "thread-substitute"},
            {"type": "item.completed", "item": {
                "id": "answer", "type": "agent_message",
                "text": PREFLIGHT.PRIVATE_REPLY_CONTENT,
            }},
            {"type": "turn.completed"},
        ]
        cases = [
            (missing, "wrong native MCP dispatch count"),
            (jsonl_private_events(rejected_reply=True), "native reply_to_operator was rejected"),
            (substituted, "substituted ordinary text"),
        ]
        for events, diagnostic in cases:
            completed = subprocess.CompletedProcess(
                ["codex"], 0, "\n".join(json.dumps(event) for event in events), "",
            )
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {
                "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
                "CODEX_HOME": "/tmp/preflight-home/codex-home",
            }, clear=False), mock.patch.object(
                PREFLIGHT.subprocess, "run", return_value=completed
            ), contextlib.redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as raised:
                PREFLIGHT.run_codex_turn(
                    ["codex"], "prompt", 1,
                    expected_calls=PREFLIGHT.private_packet_call_specs(),
                    expected_agent_message=PREFLIGHT.PRIVATE_FINAL_RESULT,
                    turn_label="resumed private packet",
                )
            self.assertEqual(raised.exception.code, 126)
            self.assertIn(diagnostic, stderr.getvalue())
            self.assertLess(len(stderr.getvalue()), 2_500)

    def test_jsonl_greeting_is_tool_free_and_local_tools_fail_closed(self):
        greeting = (
            {"type": "thread.started", "thread_id": "thread-greeting"},
            {"type": "item.completed", "item": {
                "id": "greeting", "type": "agent_message",
                "text": PREFLIGHT.GREETING_RESULT,
            }},
            {"type": "turn.completed"},
        )
        completed = subprocess.CompletedProcess(
            ["codex"], 0, "\n".join(json.dumps(event) for event in greeting), "",
        )
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
            "CODEX_HOME": "/tmp/preflight-home/codex-home",
        }, clear=False), mock.patch.object(
            PREFLIGHT.subprocess, "run", return_value=completed
        ):
            thread_id, _, complete = PREFLIGHT.run_codex_turn(
                ["codex"], "greeting", 1, expected_calls=[],
                expected_agent_message=PREFLIGHT.GREETING_RESULT,
                turn_label="initial greeting",
            )
        self.assertEqual(thread_id, "thread-greeting")
        self.assertTrue(complete)
        forbidden = (
            {"type": "thread.started", "thread_id": "thread-forbidden"},
            {"type": "item.started", "item": {
                "id": "forbidden", "type": "command_execution",
            }},
            {"type": "turn.completed"},
        )
        completed = subprocess.CompletedProcess(
            ["codex"], 0, "\n".join(json.dumps(event) for event in forbidden), "",
        )
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
            "CODEX_HOME": "/tmp/preflight-home/codex-home",
        }, clear=False), mock.patch.object(
            PREFLIGHT.subprocess, "run", return_value=completed
        ), self.assertRaises(SystemExit) as raised:
            PREFLIGHT.run_codex_turn(
                ["codex"], "greeting", 1, expected_calls=[],
                expected_agent_message=PREFLIGHT.GREETING_RESULT,
                turn_label="initial greeting",
            )
        self.assertEqual(raised.exception.code, 126)

    def test_broker_trace_rejects_resource_or_server_discovery(self):
        private_calls = [spec["tool"] for spec in PREFLIGHT.private_packet_call_specs()]
        valid = [
            {
                "connection": 1, "parse_errors": 0,
                "methods": ["initialize", "tools/list", *(["tools/call"] * 3)],
                "tool_calls": [
                    "project_read", "strategic_operation", "reply_to_operator",
                ],
            },
            {
                "connection": 2, "parse_errors": 0,
                "methods": ["initialize", "notifications/initialized", "tools/list"],
                "tool_calls": [],
            },
            {
                "connection": 3, "parse_errors": 0,
                "methods": [
                    "initialize", "notifications/initialized", "tools/list",
                    *(["tools/call"] * len(private_calls)),
                ],
                "tool_calls": private_calls,
            },
        ]
        PREFLIGHT.validate_broker_protocol_trace(valid)
        for mutate in ("method", "tool"):
            drift = copy.deepcopy(valid)
            if mutate == "method":
                drift[2]["methods"].append("resources/list")
            else:
                drift[2]["tool_calls"].append("list_mcp_resources")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as raised:
                PREFLIGHT.validate_broker_protocol_trace(drift)
            self.assertEqual(raised.exception.code, 126)
            self.assertIn("resource/server discovery", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
