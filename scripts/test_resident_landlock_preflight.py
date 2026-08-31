"""Focused regressions for the quota-free real-Codex Resident preflight."""

import copy
import importlib.util
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
    def test_loopback_responses_fixture_serves_two_tool_call_continuation_pairs(self):
        server, thread, requests = PREFLIGHT.start_responses_fixture()
        url = f"http://127.0.0.1:{server.server_port}/v1/responses"
        try:
            response_bodies = []
            for turn in range(4):
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
        self.assertEqual(len(requests), 4)
        self.assertIn('"type":"function_call"', response_bodies[0])
        self.assertIn('"type":"message"', response_bodies[1])
        self.assertIn('"type":"function_call"', response_bodies[2])
        self.assertIn('"type":"message"', response_bodies[3])

    def test_responses_roundtrip_requires_namespaced_schema_and_exact_mcp_output(self):
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
                    "id": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "required": ["resource"],
                "additionalProperties": False,
            },
        }
        requests = []
        retained = []
        for turn in (1, 2):
            call = {
                "id": f"fc_blueprint_preflight_{turn}",
                "type": "function_call",
                "call_id": f"call_blueprint_preflight_{turn}",
                "namespace": "mcp__workday", "name": "project_read",
                "arguments": '{"resource":"running-update"}',
            }
            output = {
                "type": "function_call_output",
                "call_id": f"call_blueprint_preflight_{turn}",
                "output": (
                    'Wall time: 0.0001 seconds\nOutput:\n'
                    '{"bytes":40,"resource":"running-update"}'
                ),
            }
            requests.extend((
                {"tools": [{
                    "type": "namespace", "name": "mcp__workday",
                    "description": "Workday", "tools": [
                        {"type": "function", "name": "reply_to_operator"},
                        {"type": "function", "name": "strategic_operation"},
                        project_read,
                    ],
                }]},
                {"input": [*retained, call, output]},
            ))
            retained.extend((call, output))
        PREFLIGHT.validate_responses_mcp_roundtrips(requests)
        bad_packets = []
        missing_namespace = copy.deepcopy(requests)
        missing_namespace[1]["input"][0].pop("namespace")
        bad_packets.append(missing_namespace)
        extra_child = copy.deepcopy(requests)
        extra_child[0]["tools"][0]["tools"].append(
            {"type": "function", "name": "unexpected_tool"}
        )
        bad_packets.append(extra_child)
        for packet in bad_packets:
            with self.assertRaises(SystemExit) as raised:
                PREFLIGHT.validate_responses_mcp_roundtrips(packet)
            self.assertEqual(raised.exception.code, 126)

    def test_jsonl_transport_diagnostics_do_not_replace_terminal_authority(self):
        stdout = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": "thread-preflight"},
            {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "item.completed", "item": {
                "id": "fallback", "type": "error", "message": "Falling back to HTTPS",
            }},
            {"type": "item.completed", "item": {
                "id": "answer", "type": "agent_message", "text": "complete",
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
            {"type": "turn.completed"},
        ))
        completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
            "CODEX_HOME": "/tmp/preflight-home/codex-home",
        }, clear=False):
            with mock.patch.object(PREFLIGHT.subprocess, "run", return_value=completed):
                thread_id, stderr, dispatched = PREFLIGHT.run_codex_turn(
                    ["codex"], "prompt", 1
                )
        self.assertEqual(thread_id, "thread-preflight")
        self.assertEqual(stderr, "")
        self.assertTrue(dispatched)

    def test_jsonl_preflight_rejects_missing_mcp_dispatch(self):
        result = {
            "content": [{"type": "text", "text": '{"markdown": "preflight running update"}'}],
            "structured_content": {"resource": "running-update", "bytes": 40},
        }
        start = {"type": "item.started", "item": {
            "type": "mcp_tool_call", "server": "workday", "tool": "project_read",
            "arguments": {"resource": "running-update"}, "status": "in_progress",
        }}
        done = {"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "workday", "tool": "project_read",
            "arguments": {"resource": "running-update"}, "status": "completed",
            "error": None, "result": result,
        }}
        cases = [
            [{"type": "item.completed", "item": {
                "id": "answer", "type": "agent_message", "text": "complete",
            }}],
            [start, done],
            [
                {**done, "item": {**done["item"], "id": "mcp-read"}},
                {**start, "item": {**start["item"], "id": "mcp-read"}},
            ],
        ]
        for items in cases:
            stdout = "\n".join(json.dumps(event) for event in (
                {"type": "thread.started", "thread_id": "thread-no-mcp"},
                *items,
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
                    PREFLIGHT.run_codex_turn(["codex"], "prompt", 1)
            self.assertEqual(raised.exception.code, 126)

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
                    PREFLIGHT.run_codex_turn(["codex"], "prompt", 1)
            self.assertEqual(raised.exception.code, 126)


if __name__ == "__main__":
    unittest.main()
