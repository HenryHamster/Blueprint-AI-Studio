"""Tests for scripts/walkthrough_stack.py — the runner-side candidate stack.

PURE unit tests: docker is never invoked. A recording fake stands in for
``_run_docker`` so the tests assert the exact fixed-shape argv sequence (the
security property: the candidate's compose file is never executed and nothing
in the request can steer WHAT runs), the file protocol, and teardown
guarantees.
"""

import base64
import contextlib
import io
import json
import os
import signal
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

    def test_boot_provisions_ephemeral_encryption_key(self):
        """Migration 0609 refuses to stamp without GITHUB_PAT_ENCRYPTION_KEY,
        and a keyless app runs in the plaintext-credential failure class. The
        supervisor must mint a per-boot Fernet key and hand the SAME key to
        both the migration run and the app container."""
        clean, _ = wts.validate_request(_request())
        self.sup._boot(clean)
        joined = [" ".join(c) for c in self.runner.calls]

        def _key_from(call: str) -> str:
            for token in call.split():
                if token.startswith("GITHUB_PAT_ENCRYPTION_KEY="):
                    return token.split("=", 1)[1]
            return ""

        migrate_key = _key_from(joined[4])
        app_key = _key_from(joined[5])
        self.assertTrue(migrate_key, "migration run missing GITHUB_PAT_ENCRYPTION_KEY")
        self.assertTrue(app_key, "app container missing GITHUB_PAT_ENCRYPTION_KEY")
        self.assertEqual(migrate_key, app_key)
        # Fernet shape: 32 url-safe-base64 bytes (44 chars with padding).
        self.assertEqual(len(base64.urlsafe_b64decode(migrate_key)), 32)
        # Ephemeral: a second supervisor boot mints a DIFFERENT key.
        other_runner = RecordingRunner()
        other = wts.WalkthroughStackSupervisor(
            workspace=self.tmp, task_id="Task-456DEF", run=other_runner, log=lambda m: None
        )
        other._boot(clean)
        other_key = _key_from(" ".join(other_runner.calls[4]))
        self.assertNotEqual(migrate_key, other_key)

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


class TestCli(unittest.TestCase):
    """Standalone CLI (``python3 scripts/walkthrough_stack.py --candidate-sha …``)."""

    def _tmp_checkout(self):
        ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(ws, "backend"))
        return ws

    # ── argument parsing ──
    def test_parse_args_defaults(self):
        args = wts.build_parser().parse_args(["--candidate-sha", "a" * 40])
        self.assertEqual(args.candidate_sha, "a" * 40)
        self.assertIsNone(args.workspace)
        self.assertIsNone(args.build_context)
        self.assertEqual(args.timeout, wts._CLI_DEFAULT_TIMEOUT_S)
        self.assertEqual(args.frontend_port, 3001)
        self.assertFalse(args.run_harness)
        self.assertFalse(args.check)
        self.assertFalse(args.keep)

    def test_main_requires_candidate_sha_unless_check(self):
        with self.assertRaises(SystemExit):
            wts.main([])
        args = wts.build_parser().parse_args(["--check"])
        self.assertTrue(args.check)

    def test_cli_build_request_mints_valid_secrets(self):
        req = wts._cli_build_request(
            wts.build_parser().parse_args(["--candidate-sha", "b" * 40])
        )
        clean, err = wts.validate_request(req)
        self.assertEqual(err, "")
        self.assertEqual(clean["candidate_sha"], "b" * 40)
        self.assertTrue(req["pg_password"].startswith("wt-pg-"))
        self.assertTrue(req["admin_token"].startswith("wt-admin-"))
        self.assertNotIn("anthropic_api_key", clean)

    # ── --check pre-flight ──
    def test_check_passes_with_docker_and_checkout(self):
        ws = self._tmp_checkout()
        args = wts.build_parser().parse_args(["--check", "--workspace", ws])
        with mock.patch.object(wts.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="true", stderr=""
            )
            self.assertEqual(wts._cli_check(args), 0)
        # docker info + git rev-parse both exercised; no containers created.
        self.assertEqual(mock_run.call_count, 2)

    def test_check_fails_when_backend_context_missing(self):
        ws = tempfile.mkdtemp()
        args = wts.build_parser().parse_args(["--check", "--workspace", ws])
        with mock.patch.object(wts.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="true", stderr=""
            )
            self.assertEqual(wts._cli_check(args), 1)

    def test_check_fails_when_docker_unreachable(self):
        ws = self._tmp_checkout()
        args = wts.build_parser().parse_args(["--check", "--workspace", ws])

        def fake_run(argv, **kwargs):
            if argv[:2] == ["docker", "info"]:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="unreachable"
                )
            return subprocess.CompletedProcess(argv, 0, stdout="true", stderr="")

        with mock.patch.object(wts.subprocess, "run", side_effect=fake_run):
            self.assertEqual(wts._cli_check(args), 1)

    # ── boot → health → SHA → teardown ──
    def test_main_boots_healths_reads_sha_and_tears_down(self):
        runner = RecordingRunner()
        ws = self._tmp_checkout()
        calls = []

        def fake_http(url, *, timeout, headers=None):
            calls.append(url)
            if url.endswith("/health"):
                return (200, {"status": "healthy"})
            return (200, {"sha": "a" * 40, "revision": "rev-1"})

        def fake_setup(sup, holder, *, keep):
            return lambda: sup.teardown()

        with mock.patch.object(wts, "_setup_teardown", fake_setup), mock.patch.object(
            wts, "_http_get_json", side_effect=fake_http
        ):
            exit_code = wts.main(
                ["--candidate-sha", "a" * 40, "--workspace", ws, "--timeout", "30"],
                run=runner,
            )
        self.assertEqual(exit_code, 0)
        joined = [" ".join(c) for c in runner.calls]
        # Fixed-shape boot: network → db → build → migrate → app.
        self.assertIn("docker network create", joined[0])
        self.assertIn("docker build -t", joined[2])
        # Bare-checkout layout detection: backend/ at the workspace root.
        self.assertTrue(joined[2].endswith(os.path.join(ws, "backend")))
        self.assertFalse(joined[2].endswith(os.path.join(ws, "agent", "backend")))
        self.assertTrue(any("/health" in u for u in calls))
        self.assertTrue(any("/api/version" in u for u in calls))
        # Teardown ran on the normal exit path (idempotent with atexit).
        self.assertTrue(any("docker rm -f" in j for j in joined))

    def test_main_invalid_sha_exits_2_before_docker(self):
        runner = RecordingRunner()
        ws = self._tmp_checkout()
        exit_code = wts.main(
            ["--candidate-sha", "not-a-sha!", "--workspace", ws], run=runner
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(runner.calls, [])

    def test_main_run_harness_invokes_harness(self):
        runner = RecordingRunner()
        ws = self._tmp_checkout()

        def fake_http(url, *, timeout, headers=None):
            if url.endswith("/health"):
                return (200, {"status": "healthy"})
            return (200, {"sha": "a" * 40, "revision": "rev-1"})

        def fake_setup(sup, holder, *, keep):
            return lambda: sup.teardown()

        with mock.patch.object(wts, "_setup_teardown", fake_setup), mock.patch.object(
            wts, "_http_get_json", side_effect=fake_http
        ), mock.patch.object(wts, "_run_harness") as mock_harness:
            exit_code = wts.main(
                [
                    "--candidate-sha", "a" * 40, "--workspace", ws,
                    "--timeout", "30", "--run-harness",
                ],
                run=runner,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_harness.call_count, 1)
        self.assertEqual(mock_harness.call_args.kwargs["frontend_port"], 3001)
        self.assertIn("backend_port", mock_harness.call_args.kwargs)

    # ── teardown guarantees ──
    def test_handle_signal_exits(self):
        with mock.patch.object(wts.sys, "exit") as mock_exit:
            wts._handle_signal(signal.SIGTERM, None)
        mock_exit.assert_called_once_with(128 + signal.SIGTERM)

    def test_setup_teardown_registers_handlers_and_cleans_up(self):
        runner = RecordingRunner()
        sup = wts.WalkthroughStackSupervisor(
            workspace=self._tmp_checkout(), task_id="t1", run=runner, log=lambda m: None
        )
        clean, _ = wts.validate_request(_request())
        sup._boot(clean)

        class FakeFrontend:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        frontend = FakeFrontend()
        holder = [frontend]
        with mock.patch.object(wts.atexit, "register") as mock_atexit, mock.patch.object(
            wts.signal, "signal"
        ) as mock_signal:
            cleanup = wts._setup_teardown(sup, holder, keep=False)
        mock_atexit.assert_called_once()
        self.assertEqual(mock_signal.call_count, 2)
        runner.calls.clear()
        cleanup()
        self.assertTrue(frontend.terminated)
        self.assertTrue(any("docker rm -f" in " ".join(c) for c in runner.calls))
        # Idempotent — a second cleanup (atexit + finally both fire) is a no-op.
        runner.calls.clear()
        cleanup()
        self.assertEqual(runner.calls, [])

    def test_setup_teardown_keep_skips_registration(self):
        runner = RecordingRunner()
        sup = wts.WalkthroughStackSupervisor(
            workspace=self._tmp_checkout(), task_id="t1", run=runner, log=lambda m: None
        )
        with mock.patch.object(wts.atexit, "register") as mock_atexit, mock.patch.object(
            wts.signal, "signal"
        ) as mock_signal:
            wts._setup_teardown(sup, [], keep=True)
        mock_atexit.assert_not_called()
        mock_signal.assert_not_called()

    # ── --run-harness with a fake harness script ──
    def test_run_harness_seeds_serves_and_runs_fake_harness(self):
        req = wts.validate_request(_request())[0]
        fake_root = tempfile.mkdtemp()
        seed_dir = os.path.join(fake_root, "scripts", "walkthrough")
        os.makedirs(seed_dir)
        with open(os.path.join(seed_dir, "seed_test_data.py"), "w") as fh:
            fh.write("print('seeded')\n")
        # The fake harness is interpreted by _HARNESS_BIN, which the test points
        # at the real python interpreter (node is not installed on task VMs).
        with open(os.path.join(seed_dir, "v26-live-walkthrough.cjs"), "w") as fh:
            fh.write(
                "import json, os\n"
                "with open(os.environ['WALKTHROUGH_RESULT_FILE'], 'w') as fh:\n"
                "    json.dump({'passed': True, 'sha': os.environ['GIT_SHA'], 'backend': os.environ['WALKTHROUGH_BACKEND_URL']}, fh)\n"
            )
        os.makedirs(os.path.join(fake_root, "admin-ui"))

        class FakeFrontend:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        fake_frontend = FakeFrontend()
        holder = []
        out = io.StringIO()
        with mock.patch.object(wts, "_REPO_ROOT", fake_root), mock.patch.object(
            wts, "_HARNESS_BIN", sys.executable
        ), mock.patch.object(wts, "_serve_frontend", return_value=fake_frontend):

            with contextlib.redirect_stdout(out):
                wts._run_harness(
                    req,
                    backend_port=18080,
                    db_port=15432,
                    frontend_port=13001,
                    workspace=tempfile.mkdtemp(),
                    frontend_holder=holder,
                )
        self.assertIs(holder[0], fake_frontend)
        result = json.loads(out.getvalue())
        self.assertTrue(result["passed"])
        self.assertEqual(result["sha"], "a" * 40)
        self.assertEqual(result["backend"], "http://127.0.0.1:18080")
        # The emitted result must carry the frontend-source caveat so a green
        # harness run is never misread as proof the candidate frontend works.
        self.assertEqual(result["frontend_source"], "local_repo_checkout")
        self.assertIn("frontend_caveat", result)


if __name__ == "__main__":
    unittest.main()
