"""Focused regressions for the quota-free real-Codex Resident preflight."""

import copy
import contextlib
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


class TestResidentLandlockPreflight(unittest.TestCase):
    def test_loopback_fixture_serves_project_read_then_native_reply_for_exec_and_resume(self):
        server, thread, requests = PREFLIGHT.start_responses_fixture()
        url = f"http://127.0.0.1:{server.server_port}/v1/responses"
        try:
            response_bodies = []
            for turn in range(6):
                request = urllib.request.Request(
                    url,
                    data=json.dumps({
                        "model": "blueprint-preflight", "stream": True,
                        "input": f"turn {turn}",
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    body = response.read().decode()
                response_bodies.append(body)
                self.assertIn('"type":"response.completed"', body)
                self.assertTrue(body.endswith("data: [DONE]\n\n"))
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
        self.assertEqual(len(requests), 6)
        self.assertIn('"type":"function_call"', response_bodies[0])
        self.assertIn('"name":"reply_to_operator"', response_bodies[1])
        self.assertIn('"type":"message"', response_bodies[2])
        self.assertIn('"type":"function_call"', response_bodies[3])
        self.assertIn('"name":"reply_to_operator"', response_bodies[4])
        self.assertIn('"type":"message"', response_bodies[5])

    def test_project_read_keeps_exact_mcp_bounds_before_codex_projection(self):
        self.assertEqual(PREFLIGHT.CODEX_PREFLIGHT_MODEL, "gpt-5.5")
        self.assertEqual(
            PREFLIGHT.CODEX_DIRECT_MCP_EXPOSURE,
            'mcp_servers.workday.omit_tools_from=["deferred"]',
        )
        self.assertEqual(
            PREFLIGHT.PROJECT_READ_DESCRIPTION,
            "Read one bounded canonical Blueprint project resource for "
            "preconditions and post-mutation verification.",
        )
        self.assertEqual(PREFLIGHT.PROJECT_READ_MCP_INPUT_SCHEMA, {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "enum": ["running-update"]},
                "id": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["resource"],
            "additionalProperties": False,
        })
        self.assertEqual(PREFLIGHT.REPLY_TO_OPERATOR_MCP_INPUT_SCHEMA, {
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
        })

    def test_responses_roundtrip_accepts_codex_0150_bounded_schema_projection(self):
        project_read = {
            "type": "function",
            "name": "project_read",
            "description": (
                "Read one bounded canonical Blueprint project resource for "
                "preconditions and post-mutation verification."
            ),
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {"type": "string", "enum": ["running-update"]},
                    # codex-cli 0.150.1 projects MCP tools into a Responses
                    # namespace after dropping string-length annotations. The
                    # direct MCP discovery contract above remains exact.
                    "id": {"type": "string"},
                },
                "required": ["resource"],
                "additionalProperties": False,
            },
        }
        reply_to_operator = {
            "type": "function",
            "name": "reply_to_operator",
            "description": PREFLIGHT.REPLY_TO_OPERATOR_DESCRIPTION,
            "strict": False,
            "parameters": PREFLIGHT.REPLY_TO_OPERATOR_MCP_INPUT_SCHEMA,
        }
        requests = []
        retained = []
        for turn in (1, 2):
            project_call = {
                "id": f"fc_blueprint_project_read_{turn}",
                "type": "function_call",
                "call_id": f"call_blueprint_project_read_{turn}",
                "namespace": "mcp__workday", "name": "project_read",
                "arguments": '{"resource":"running-update"}',
            }
            project_output = {
                "type": "function_call_output",
                "call_id": f"call_blueprint_project_read_{turn}",
                "output": (
                    'Wall time: 0.0001 seconds\nOutput:\n'
                    '{"bytes":40,"resource":"running-update"}'
                ),
            }
            reply_content = (
                PREFLIGHT.INITIAL_REPLY_CONTENT
                if turn == 1 else PREFLIGHT.RESUME_REPLY_CONTENT
            )
            reply_call = {
                "id": f"fc_blueprint_reply_{turn}",
                "type": "function_call",
                "call_id": f"call_blueprint_reply_{turn}",
                "namespace": "mcp__workday", "name": "reply_to_operator",
                "arguments": json.dumps({
                    "content": reply_content,
                    "message_type": "NOTE",
                }, separators=(",", ":")),
            }
            reply_output = {
                "type": "function_call_output",
                "call_id": f"call_blueprint_reply_{turn}",
                "output": 'Wall time: 0.0001 seconds\nOutput:\n{"accepted":true}',
            }
            tools = [{
                "type": "namespace", "name": "mcp__workday",
                "description": "Workday", "tools": [
                    reply_to_operator,
                    {"type": "function", "name": "strategic_operation"},
                    project_read,
                ],
            }]
            requests.append({"tools": tools})
            retained.extend((project_call, project_output))
            requests.append({"input": copy.deepcopy(retained)})
            retained.extend((reply_call, reply_output))
            requests.append({"input": copy.deepcopy(retained)})
        PREFLIGHT.validate_responses_mcp_roundtrips(requests)
        bad_packets = []
        missing_namespace = copy.deepcopy(requests)
        missing_namespace[1]["input"][0].pop("namespace")
        bad_packets.append((missing_namespace, "changed the namespaced function call"))
        unbounded_resource = copy.deepcopy(requests)
        del unbounded_resource[0]["tools"][0]["tools"][2]["parameters"][
            "properties"
        ]["resource"]["enum"]
        bad_packets.append((unbounded_resource, "wrong project_read schema"))
        permissive_arguments = copy.deepcopy(requests)
        permissive_arguments[0]["tools"][0]["tools"][2]["parameters"][
            "additionalProperties"
        ] = True
        bad_packets.append((permissive_arguments, "wrong project_read schema"))
        rejected_reply = copy.deepcopy(requests)
        rejected_reply[2]["input"][-1]["output"] = (
            'Wall time: 0.0001 seconds\nOutput:\n{"accepted":false}'
        )
        bad_packets.append((rejected_reply, "native reply was not accepted"))
        extra_child = copy.deepcopy(requests)
        extra_child[0]["tools"][0]["tools"].append(
            {"type": "function", "name": "unexpected_tool"}
        )
        bad_packets.append((extra_child, "unbounded Workday namespace"))
        for packet, diagnostic in bad_packets:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as raised:
                PREFLIGHT.validate_responses_mcp_roundtrips(packet)
            self.assertEqual(raised.exception.code, 126)
            self.assertIn(diagnostic, stderr.getvalue())

    def test_responses_schema_mismatch_diagnostic_is_useful_and_bounded(self):
        project_read = {
            "type": "function",
            "name": "project_read",
            "description": "x" * 5000,
            "strict": False,
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
        initial = {"tools": [{
            "type": "namespace", "name": "mcp__workday",
            "description": "Workday", "tools": [
                {"type": "function", "name": "reply_to_operator"},
                {"type": "function", "name": "strategic_operation"},
                project_read,
            ],
        }]}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(
            SystemExit
        ) as raised:
            PREFLIGHT.validate_responses_mcp_roundtrips([
                initial, {}, {}, initial, {}, {},
            ])
        self.assertEqual(raised.exception.code, 126)
        diagnostic = stderr.getvalue()
        self.assertIn("wrong project_read schema", diagnostic)
        self.assertIn("expected=", diagnostic)
        self.assertIn("actual=", diagnostic)
        self.assertIn("[truncated", diagnostic)
        self.assertLess(len(diagnostic), 2500)

    def test_jsonl_transport_diagnostics_do_not_replace_terminal_authority(self):
        stdout = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": "thread-preflight"},
            {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "item.completed", "item": {
                "id": "fallback", "type": "error", "message": "Falling back to HTTPS",
            }},
            {"type": "item.started", "item": {
                "id": "mcp-read", "type": "mcp_tool_call", "server": "workday",
                "tool": "project_read", "arguments": {"resource": "running-update"},
                "status": "in_progress",
            }},
            {"type": "item.completed", "item": {
                "id": "mcp-read", "type": "mcp_tool_call", "server": "workday",
                "tool": "project_read", "arguments": {"resource": "running-update"},
                "status": "completed", "error": None,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": '{"markdown": "preflight running update"}',
                    }],
                    "structured_content": {"resource": "running-update", "bytes": 40},
                },
            }},
            {"type": "item.started", "item": {
                "id": "mcp-reply", "type": "mcp_tool_call", "server": "workday",
                "tool": "reply_to_operator", "arguments": {
                    "content": PREFLIGHT.INITIAL_REPLY_CONTENT,
                    "message_type": "NOTE",
                }, "status": "in_progress",
            }},
            {"type": "item.completed", "item": {
                "id": "mcp-reply", "type": "mcp_tool_call", "server": "workday",
                "tool": "reply_to_operator", "arguments": {
                    "content": PREFLIGHT.INITIAL_REPLY_CONTENT,
                    "message_type": "NOTE",
                }, "status": "completed", "error": None,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": "Workday reply accepted for delivery.",
                    }],
                    "structured_content": {"accepted": True},
                },
            }},
            {"type": "item.completed", "item": {
                "id": "answer", "type": "agent_message", "text": "complete",
            }},
            {"type": "turn.completed"},
        ))
        completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
            "CODEX_HOME": "/tmp/preflight-home/codex-home",
        }, clear=False):
            with mock.patch.object(PREFLIGHT.subprocess, "run", return_value=completed):
                thread_id, stderr, dispatched = PREFLIGHT.run_codex_turn(
                    ["codex"], "prompt", 1,
                    reply_content=PREFLIGHT.INITIAL_REPLY_CONTENT,
                    turn_label="initial turn",
                )
        self.assertEqual(thread_id, "thread-preflight")
        self.assertEqual(stderr, "")
        self.assertTrue(dispatched)

    def test_jsonl_preflight_rejects_missing_mcp_dispatch(self):
        project_result = {
            "content": [{"type": "text", "text": '{"markdown": "preflight running update"}'}],
            "structured_content": {"resource": "running-update", "bytes": 40},
        }
        project_start = {"type": "item.started", "item": {
            "id": "mcp-read",
            "type": "mcp_tool_call", "server": "workday", "tool": "project_read",
            "arguments": {"resource": "running-update"}, "status": "in_progress",
        }}
        project_done = {"type": "item.completed", "item": {
            "id": "mcp-read",
            "type": "mcp_tool_call", "server": "workday", "tool": "project_read",
            "arguments": {"resource": "running-update"}, "status": "completed",
            "error": None, "result": project_result,
        }}
        reply_start = {"type": "item.started", "item": {
            "id": "mcp-reply",
            "type": "mcp_tool_call", "server": "workday",
            "tool": "reply_to_operator", "arguments": {
                "content": PREFLIGHT.INITIAL_REPLY_CONTENT,
                "message_type": "NOTE",
            }, "status": "in_progress",
        }}
        reply_done = {"type": "item.completed", "item": {
            "id": "mcp-reply",
            "type": "mcp_tool_call", "server": "workday",
            "tool": "reply_to_operator", "arguments": {
                "content": PREFLIGHT.INITIAL_REPLY_CONTENT,
                "message_type": "NOTE",
            }, "status": "completed", "error": None,
            "result": {
                "content": [{
                    "type": "text", "text": "Workday reply accepted for delivery.",
                }],
                "structured_content": {"accepted": True},
            },
        }}
        cases = [
            ([{"type": "item.completed", "item": {
                "id": "answer", "type": "agent_message",
                "text": PREFLIGHT.INITIAL_REPLY_CONTENT,
            }}], "substituted ordinary text"),
            ([project_start, project_done], "omitted an exact native MCP dispatch"),
            ([
                project_start, project_done, reply_start,
                {**reply_done, "item": {
                    **reply_done["item"],
                    "result": {
                        "content": [{"type": "text", "text": "not accepted"}],
                        "structured_content": {"accepted": False},
                    },
                }},
            ], "native reply_to_operator call was rejected"),
        ]
        for items, expected_diagnostic in cases:
            stdout = "\n".join(json.dumps(event) for event in (
                {"type": "thread.started", "thread_id": "thread-no-mcp"},
                *items,
                {"type": "turn.completed"},
            ))
            completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
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
                    reply_content=PREFLIGHT.INITIAL_REPLY_CONTENT,
                    turn_label="initial turn",
                )
            self.assertEqual(raised.exception.code, 126)
            self.assertIn(expected_diagnostic, stderr.getvalue())
            self.assertLess(len(stderr.getvalue()), 2500)

    def test_jsonl_preflight_rejects_forbidden_local_items(self):
        for item_type in ("command_execution", "image_generation"):
            stdout = "\n".join(json.dumps(event) for event in (
                {"type": "thread.started", "thread_id": "thread-forbidden"},
                {"type": "item.started", "item": {
                    "id": "forbidden", "type": item_type,
                }},
                {"type": "turn.completed"},
            ))
            completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
            with mock.patch.dict(os.environ, {
                "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
                "CODEX_HOME": "/tmp/preflight-home/codex-home",
            }, clear=False), mock.patch.object(
                PREFLIGHT.subprocess, "run", return_value=completed
            ):
                with self.assertRaises(SystemExit) as raised:
                    PREFLIGHT.run_codex_turn(
                        ["codex"], "prompt", 1,
                        reply_content=PREFLIGHT.INITIAL_REPLY_CONTENT,
                        turn_label="initial turn",
                    )
            self.assertEqual(raised.exception.code, 126)


if __name__ == "__main__":
    unittest.main()
