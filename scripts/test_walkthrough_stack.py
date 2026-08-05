"""Tests for scripts/walkthrough_stack.py — the runner-side candidate stack.

PURE unit tests: docker is never invoked. A recording fake stands in for
``_run_docker`` so the tests assert the exact fixed-shape argv sequence (the
security property: the candidate's compose file is never executed and nothing
in the request can steer WHAT runs), the file protocol, and teardown
guarantees.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import walkthrough_stack as wts


def _request(**overrides):
    base = {
        "version": 1,
        "candidate_sha": "a" * 40,
        "admin_token": "wt-admin-0123456789abcdef",
        "pg_password": "wt-pg-0123456789abcdef",
        "agent_jwt_secret": "b" * 24,
        "ws_ticket_secret": "c" * 24,
    }
    base.update(overrides)
    return base


class RecordingRunner:
    """Stands in for _run_docker; records argv and returns success."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, check=True, env=None):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class FakeProc:
    """poll() returns None for the first *alive* calls, then 0."""

    def __init__(self, alive: int):
        self._alive = alive

    def poll(self):
        if self._alive > 0:
            self._alive -= 1
            return None
        return 0


class TestValidateRequest(unittest.TestCase):
    def test_valid_request_passes_and_drops_unknown_fields(self):
        clean, err = wts.validate_request(_request(extra_field="ignored"))
        self.assertEqual(err, "")
        self.assertNotIn("extra_field", clean)
        self.assertEqual(clean["candidate_sha"], "a" * 40)

    def test_rejects_bad_candidate_sha(self):
        for sha in ("", "xyz", "A A", "deadbeef; rm -rf /"):
            clean, err = wts.validate_request(_request(candidate_sha=sha))
            self.assertIsNone(clean, sha)

    def test_rejects_short_or_shelly_secrets(self):
        for key in ("pg_password", "agent_jwt_secret", "ws_ticket_secret"):
            clean, _ = wts.validate_request(_request(**{key: "short"}))
            self.assertIsNone(clean, key)
            clean, _ = wts.validate_request(_request(**{key: "$(touch /tmp/pwn)aaaa"}))
            self.assertIsNone(clean, key)

    def test_admin_token_accepts_opaque_but_not_whitespace(self):
        clean, err = wts.validate_request(_request(admin_token="tok+with/odd=chars"))
        self.assertIsNotNone(clean, err)
        clean, _ = wts.validate_request(_request(admin_token="has space"))
        self.assertIsNone(clean)
        clean, _ = wts.validate_request(_request(admin_token=""))
        self.assertIsNone(clean)

    def test_anthropic_key_is_optional_and_validated(self):
        clean, err = wts.validate_request(_request())
        self.assertIsNotNone(clean, err)
        self.assertNotIn("anthropic_api_key", clean)
        clean, err = wts.validate_request(_request(anthropic_api_key="sk-ant-abc123"))
        self.assertEqual(clean["anthropic_api_key"], "sk-ant-abc123")
        clean, _ = wts.validate_request(_request(anthropic_api_key="bad key\n"))
        self.assertIsNone(clean)

    def test_non_dict_request_rejected(self):
        clean, _ = wts.validate_request(["not", "a", "dict"])
        self.assertIsNone(clean)


class TestBootArgvShape(unittest.TestCase):
    """The fixed-shape guarantee: request values appear ONLY as env values."""

    def setUp(self):
        self.runner = RecordingRunner()
        self.tmp = tempfile.mkdtemp()
        self.sup = wts.WalkthroughStackSupervisor(
            workspace=self.tmp, task_id="Task-123ABC", run=self.runner, log=lambda m: None
        )

    def test_boot_sequence_and_loopback_publish(self):
        clean, _ = wts.validate_request(_request())
        backend_port, db_port = self.sup._boot(clean)

        joined = [" ".join(c) for c in self.runner.calls]
        # Order: network create → db run → image build → pg_isready → alembic → app run.
        self.assertIn("docker network create", joined[0])
        self.assertIn("docker run -d --name", joined[1])
        self.assertIn(wts._DB_IMAGE, joined[1])
        self.assertIn(f"127.0.0.1:{db_port}:5432", joined[1])
        self.assertIn("docker build -t", joined[2])
        # The runner's workspace is the bind ROOT; the candidate checkout (and
        # its backend/) lives in the agent subdirectory. Asserting the true
        # layout catches a regression to the non-existent <root>/backend.
        self.assertTrue(joined[2].endswith(os.path.join(self.tmp, "agent", "backend")))
        self.assertFalse(joined[2].endswith(os.path.join(self.tmp, "backend")))
        self.assertIn("pg_isready", joined[3])
        self.assertIn("alembic upgrade head", joined[4])
        self.assertIn(f"127.0.0.1:{backend_port}:8080", joined[5])
        # The candidate compose file is NEVER executed by the runner.
        self.assertNotIn("compose", " ".join(joined))
        # DEPLOY_SHA labels the stack with the candidate SHA (integrity seam).
        self.assertIn(f"DEPLOY_SHA={'a' * 40}", joined[5])
        # No shell is involved anywhere: every element is a plain token.
        for call in self.runner.calls:
            self.assertNotIn(";", " ".join(call[:2]))

    def test_pick_free_ports_returns_distinct_ports(self):
        # Regression: backend and db must never share a port. The helper holds
        # every probe socket until ALL ports are allocated, so two back-to-back
        # allocations cannot observe the same ephemeral port (the old
        # single-port helper could return the same port twice).
        ports = wts.pick_free_ports(2)
        self.assertEqual(len(ports), 2)
        self.assertNotEqual(ports[0], ports[1])
        ports = wts.pick_free_ports(5)
        self.assertEqual(len(set(ports)), 5)

    def test_boot_publishes_distinct_backend_and_db_ports(self):
        clean, _ = wts.validate_request(_request())
        backend_port, db_port = self.sup._boot(clean)
        self.assertNotEqual(backend_port, db_port)

    def test_container_names_derive_from_task_id_only(self):
        self.assertTrue(self.sup.db_name.startswith("bp-wt-task123a"))
        self.assertTrue(self.sup.app_name.startswith("bp-wt-task123a"))

    def test_teardown_removes_everything_and_is_idempotent(self):
        clean, _ = wts.validate_request(_request())
        self.sup._boot(clean)
        self.runner.calls.clear()
        self.sup.teardown()
        joined = [" ".join(c) for c in self.runner.calls]
        self.assertTrue(any("docker rm -f" in j for j in joined))
        self.assertTrue(any("docker network rm" in j for j in joined))
        self.assertTrue(any("docker rmi -f" in j for j in joined))
        self.runner.calls.clear()
        self.sup.teardown()  # idempotent — no further docker calls
        self.assertEqual(self.runner.calls, [])

    def test_teardown_without_boot_is_a_noop(self):
        self.sup.teardown()
        self.assertEqual(self.runner.calls, [])


class TestSupervisorLoop(unittest.TestCase):
    def setUp(self):
        self.runner = RecordingRunner()
        self.tmp = tempfile.mkdtemp()
        # GC is exercised in its own test; keep the loop tests deterministic.
        wts._gc_ran = True

    def _mk_supervisor(self):
        return wts.WalkthroughStackSupervisor(
            workspace=self.tmp, task_id="t1", run=self.runner, log=lambda m: None
        )

    def _proto(self, name):
        return os.path.join(self.tmp, wts.PROTOCOL_DIR, name)

    def test_marker_then_request_then_ok_response_then_teardown_on_exit(self):
        sup = self._mk_supervisor()
        sup.write_marker()
        self.assertTrue(os.path.exists(self._proto("supervisor.json")))
        with open(self._proto("request.json"), "w") as fh:
            json.dump(_request(), fh)
        with mock.patch.object(wts, "_POLL_INTERVAL_S", 0.01):
            sup.run(FakeProc(alive=3))
        with open(self._proto("response.json")) as fh:
            response = json.load(fh)
        self.assertEqual(response["status"], "ok")
        self.assertIsInstance(response["backend_port"], int)
        self.assertIsInstance(response["db_port"], int)
        # Teardown ran after the agent process exited.
        self.assertTrue(
            any("rm" in c[1] for c in self.runner.calls if c[0] == "docker")
        )

    def test_invalid_request_yields_error_response_and_no_docker(self):
        sup = self._mk_supervisor()
        sup.write_marker()
        with open(self._proto("request.json"), "w") as fh:
            json.dump(_request(candidate_sha="nope!"), fh)
        with mock.patch.object(wts, "_POLL_INTERVAL_S", 0.01):
            sup.run(FakeProc(alive=3))
        with open(self._proto("response.json")) as fh:
            response = json.load(fh)
        self.assertEqual(response["status"], "error")
        self.assertIn("candidate_sha", response["detail"])
        self.assertEqual(self.runner.calls, [])

    def test_boot_failure_writes_error_response_and_still_tears_down(self):
        def failing_runner(argv, *, timeout, check=True, env=None):
            self.runner.calls.append(list(argv))
            if argv[:3] == ["docker", "network", "create"]:
                raise RuntimeError("docker daemon unreachable")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        sup = wts.WalkthroughStackSupervisor(
            workspace=self.tmp, task_id="t1", run=failing_runner, log=lambda m: None
        )
        sup.write_marker()
        with open(self._proto("request.json"), "w") as fh:
            json.dump(_request(), fh)
        with mock.patch.object(wts, "_POLL_INTERVAL_S", 0.01):
            sup.run(FakeProc(alive=3))
        with open(self._proto("response.json")) as fh:
            response = json.load(fh)
        self.assertEqual(response["status"], "error")
        self.assertIn("daemon unreachable", response["detail"])
        # _booted was set before the first docker call → teardown still swept.
        self.assertTrue(any(c[:2] == ["docker", "rm"] for c in self.runner.calls))

    def test_teardown_file_requests_early_teardown(self):
        sup = self._mk_supervisor()
        sup.write_marker()
        os.makedirs(os.path.join(self.tmp, wts.PROTOCOL_DIR), exist_ok=True)
        with open(self._proto("teardown.json"), "w") as fh:
            fh.write("{}")
        with mock.patch.object(wts, "_POLL_INTERVAL_S", 0.01):
            start = time.monotonic()
            sup.run(FakeProc(alive=10_000))  # would spin ~100 s without the signal
        self.assertLess(time.monotonic() - start, 5)


class TestProtocolDirContract(unittest.TestCase):
    """The single shared probe contract: the write location (host side) and the
    probe location (sandbox side) derive from the SAME constant, so the two
    sides of the sandbox boundary can never disagree about where the marker is."""

    def test_write_location_is_proto_dir_under_the_host_workspace(self):
        ws = "/tmp/blueprint-agent-workspaces/task-abc"
        self.assertEqual(wts._proto_dir(ws), os.path.join(ws, wts.PROTOCOL_DIR))

    def test_sandbox_probe_location_derives_from_the_same_constant(self):
        # The bwrap workspace bind maps the host workspace to /workspace; the
        # marker sits at that bind root, one level ABOVE the agent's workspace.
        self.assertEqual(
            wts.sandbox_proto_dir("/workspace"),
            os.path.join("/workspace", wts.PROTOCOL_DIR),
        )

    def test_both_sides_share_the_same_relative_dir_name(self):
        # The runner writes at _proto_dir(<host workspace>) and hands the agent
        # sandbox_proto_dir(<sandbox mount>): the two paths differ only in the
        # bind root, never in the protocol dir name itself.
        self.assertEqual(
            os.path.basename(wts._proto_dir("/tmp/ws")),
            os.path.basename(wts.sandbox_proto_dir("/workspace")),
        )

    def test_write_marker_lands_exactly_at_proto_dir(self):
        tmp = tempfile.mkdtemp()
        sup = wts.WalkthroughStackSupervisor(
            workspace=tmp,
            task_id="t1",
            run=lambda argv, *, timeout, check=True, env=None: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
            log=lambda m: None,
        )
        sup.write_marker()
        self.assertTrue(
            os.path.exists(os.path.join(tmp, wts.PROTOCOL_DIR, "supervisor.json"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(tmp, "agent", wts.PROTOCOL_DIR, "supervisor.json"))
        )


class TestGcStaleStacks(unittest.TestCase):
    def test_gc_removes_listed_stale_containers_once(self):
        wts._gc_ran = False
        calls = []

        def fake_run(argv, *, timeout, check=True, env=None):
            calls.append(list(argv))
            if argv[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(argv, 0, stdout="bp-wt-old-db\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        wts.gc_stale_stacks(run=fake_run, log=lambda m: None)
        self.assertTrue(any(c[:3] == ["docker", "rm", "-f"] for c in calls))
        before = len(calls)
        wts.gc_stale_stacks(run=fake_run, log=lambda m: None)  # once per process
        self.assertEqual(len(calls), before)


if __name__ == "__main__":
    unittest.main()
