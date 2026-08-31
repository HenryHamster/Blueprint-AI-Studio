"""Focused regressions for the quota-free real-Codex Resident preflight."""

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
    def test_loopback_responses_fixture_serves_exactly_two_streamed_turns(self):
        server, thread, requests = PREFLIGHT.start_responses_fixture()
        url = f"http://127.0.0.1:{server.server_port}/v1/responses"
        try:
            for turn in range(2):
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
        self.assertEqual(len(requests), 2)

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
            {"type": "turn.completed"},
        ))
        completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
            "CODEX_HOME": "/tmp/preflight-home/codex-home",
        }, clear=False):
            with mock.patch.object(PREFLIGHT.subprocess, "run", return_value=completed):
                thread_id, stderr = PREFLIGHT.run_codex_turn(["codex"], "prompt", 1)
        self.assertEqual(thread_id, "thread-preflight")
        self.assertEqual(stderr, "")

    def test_jsonl_preflight_rejects_forbidden_local_items(self):
        stdout = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": "thread-forbidden"},
            {"type": "item.started", "item": {
                "id": "command", "type": "command_execution", "command": "pwd",
            }},
            {"type": "turn.completed"},
        ))
        completed = subprocess.CompletedProcess(["codex"], 0, stdout, "")
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/preflight-home", "TMPDIR": "/tmp/preflight-home",
            "CODEX_HOME": "/tmp/preflight-home/codex-home",
        }, clear=False):
            with mock.patch.object(PREFLIGHT.subprocess, "run", return_value=completed):
                with self.assertRaises(SystemExit) as raised:
                    PREFLIGHT.run_codex_turn(["codex"], "prompt", 1)
        self.assertEqual(raised.exception.code, 126)


if __name__ == "__main__":
    unittest.main()
