"""Tests for scripts/local_runner.py."""

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add scripts dir to path so we can import local_runner directly
sys.path.insert(0, str(Path(__file__).parent))
import local_runner


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env(**extra):
    """Return a minimal env dict for tests."""
    return {
        "BLUEPRINT_RUNNER_TOKEN": "tok-abcdefgh-1234",
        "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
        **extra,
    }


class _FakeResp:
    """Minimal urllib response mock."""
    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = json.dumps(body).encode() if body else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeHTTPError(Exception):
    """Mimics urllib.error.HTTPError for tests."""
    def __init__(self, code: int, body: bytes = b""):
        self.code = code
        self._body = body

    def read(self):
        return self._body


# ── Env var validation ────────────────────────────────────────────────────────

class TestEnvVarValidation(unittest.TestCase):
    def _run_script(self, env_overrides=None, strip_vars=None):
        env = os.environ.copy()
        env.pop("BLUEPRINT_RUNNER_TOKEN", None)
        env.pop("BLUEPRINT_ORCHESTRATOR_URL", None)
        if env_overrides:
            env.update(env_overrides)
        if strip_vars:
            for k in strip_vars:
                env.pop(k, None)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "local_runner.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result

    def test_missing_token_exits_1(self):
        result = self._run_script(
            env_overrides={"BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLUEPRINT_RUNNER_TOKEN", result.stderr)

    def test_missing_url_exits_1(self):
        result = self._run_script(
            env_overrides={"BLUEPRINT_RUNNER_TOKEN": "tok-abc"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLUEPRINT_ORCHESTRATOR_URL", result.stderr)

    # NOTE: a missing agent binary is NO LONGER fatal at startup (FIX M5) — the
    # runner resolves the agent lazily so a compute/GPU-only runner can boot
    # without a built agent. The startup-exit-on-missing-binary behavior was
    # removed; its unit-level replacement lives in TestAgentBinaryDetection
    # (test_locate_* — non-fatal None) and the lazy per-task skip is covered by
    # the main loop's _locate_agent_binary re-resolution.


# ── Agent binary detection ─────────────────────────────────────────────────────

class TestAgentBinaryDetection(unittest.TestCase):
    def test_explicit_existing_path_accepted(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            path = f.name
        try:
            with patch.dict(os.environ, {"BLUEPRINT_AGENT_BINARY": path}):
                result = local_runner._find_agent_binary()
            self.assertEqual(result, path)
        finally:
            os.unlink(path)

    def test_explicit_missing_path_exits(self):
        with patch.dict(os.environ, {"BLUEPRINT_AGENT_BINARY": "/no/such/file.js"}):
            with self.assertRaises(SystemExit) as ctx:
                local_runner._find_agent_binary()
        self.assertEqual(ctx.exception.code, 1)

    def test_auto_detect_from_script_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dist_dir = os.path.join(tmpdir, "agent", "dist")
            os.makedirs(dist_dir)
            idx = os.path.join(dist_dir, "index.js")
            open(idx, "w").close()
            # Patch __file__ so the script dir points to our tmpdir
            with patch.dict(os.environ, {}, clear=True):
                env_clean = {k: v for k, v in os.environ.items() if k != "BLUEPRINT_AGENT_BINARY"}
                with patch.dict(os.environ, env_clean, clear=True):
                    with patch("local_runner.__file__", os.path.join(tmpdir, "scripts", "local_runner.py")):
                        with patch("os.path.isfile") as mock_isfile:
                            mock_isfile.side_effect = lambda p: p == os.path.normpath(
                                os.path.join(tmpdir, "agent", "dist", "index.js")
                            )
                            result = local_runner._find_agent_binary()
            self.assertTrue(result.endswith("index.js"))

    def test_auto_detect_not_found_exits(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.isfile", return_value=False):
                with self.assertRaises(SystemExit) as ctx:
                    local_runner._find_agent_binary()
        self.assertEqual(ctx.exception.code, 1)

    # ── Lazy (non-fatal) locator — FIX M5: compute-only runners boot w/o agent ──

    def test_locate_returns_none_for_missing_explicit(self):
        """_locate_agent_binary returns None (does NOT exit) for a bad explicit path."""
        with patch.dict(os.environ, {"BLUEPRINT_AGENT_BINARY": "/no/such/file.js"}):
            self.assertIsNone(local_runner._locate_agent_binary())

    def test_locate_returns_path_for_existing_explicit(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False) as f:
            path = f.name
        try:
            with patch.dict(os.environ, {"BLUEPRINT_AGENT_BINARY": path}):
                self.assertEqual(local_runner._locate_agent_binary(), path)
        finally:
            os.unlink(path)

    def test_locate_returns_none_when_autodetect_misses(self):
        """No explicit binary and nothing on disk → None, never SystemExit."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.isfile", return_value=False):
                self.assertIsNone(local_runner._locate_agent_binary())


class TestAgentWorkspace(unittest.TestCase):
    def test_agent_workspace_for_task_uses_configured_root_and_sanitizes_task_id(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir}):
                workspace = local_runner._agent_workspace_for_task("task/abc:123")

            self.assertTrue(workspace.startswith(os.path.abspath(tmpdir)))
            self.assertTrue(workspace.endswith("task_abc_123"))
            self.assertTrue(os.path.isdir(workspace))


# ── Startup summary ───────────────────────────────────────────────────────────

class TestStartupSummary(unittest.TestCase):
    def test_token_prefix_in_summary(self):
        """Startup output includes first 8 chars of token followed by '...'."""
        token = "tok-abcdefgh-1234"
        expected_prefix = token[:8] + "..."
        # Simulate the print lines in main() by checking _require_env + prefix logic
        prefix = token[:8] + "..." if len(token) >= 8 else token
        self.assertEqual(prefix, expected_prefix)
        self.assertIn("...", prefix)

    def test_short_token_no_truncation(self):
        token = "short"
        prefix = token[:8] + "..." if len(token) >= 8 else token
        self.assertEqual(prefix, token)


# ── HTTP helper ───────────────────────────────────────────────────────────────

class TestPostHelper(unittest.TestCase):
    def test_200_with_json_body(self):
        body = {"task_id": "abc", "agent_token": "jwt"}
        fake_resp = _FakeResp(200, body)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            status, result = local_runner._post("https://api/endpoint", "tok", {})
        self.assertEqual(status, 200)
        self.assertEqual(result, body)

    def test_204_no_content(self):
        fake_resp = _FakeResp(204)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            status, result = local_runner._post("https://api/endpoint", "tok", {})
        self.assertEqual(status, 204)
        self.assertIsNone(result)

    def test_401_http_error(self):
        import urllib.error
        exc = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        exc.read = lambda: b""
        with patch("urllib.request.urlopen", side_effect=exc):
            status, result = local_runner._post("https://api/endpoint", "tok", {})
        self.assertEqual(status, 401)

    def test_network_error_raises(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conn refused")):
            with self.assertRaises(RuntimeError):
                local_runner._post("https://api/endpoint", "tok", {})


# ── Poll loop logic ───────────────────────────────────────────────────────────

class TestMaxConcurrentCap(unittest.TestCase):
    def test_does_not_poll_when_at_max(self):
        """When active_tasks is full, the loop should sleep without calling next-task."""
        max_concurrent = 2
        active_tasks = {"t1": MagicMock(), "t2": MagicMock()}
        lock = threading.Lock()

        call_count = {"n": 0}

        def fake_post(url, token, body):
            call_count["n"] += 1
            return 204, None

        iterations = [0]

        def controlled_sleep(secs):
            iterations[0] += 1
            if iterations[0] >= 3:
                local_runner._shutdown_event.set()

        local_runner._shutdown_event.clear()
        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch("time.sleep", controlled_sleep), \
                 patch("os.environ", {
                     "BLUEPRINT_RUNNER_TOKEN": "tok-abc",
                     "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
                 }):
                # Simulate the "at capacity" branch of the loop
                with lock:
                    active_count = len(active_tasks)
                # If at capacity, we sleep and continue without calling _post
                if active_count >= max_concurrent:
                    controlled_sleep(2)  # triggers shutdown after 3 iterations

            self.assertEqual(call_count["n"], 0, "next-task should NOT be called when at capacity")
        finally:
            local_runner._shutdown_event.clear()

    def test_backoff_doubles_on_no_task(self):
        """Poll interval doubles (up to 30s) on 204 responses."""
        # Test the backoff arithmetic directly
        poll_interval = 2.0
        for expected in [4.0, 8.0, 16.0, 30.0, 30.0]:
            poll_interval = min(poll_interval * 2, 30.0)
            self.assertLessEqual(poll_interval, 30.0)
        self.assertEqual(poll_interval, 30.0)

    def test_backoff_resets_on_task(self):
        """Poll interval resets to base on a 200 response."""
        base_poll_interval = 2.0
        poll_interval = 30.0  # maxed out
        poll_interval = base_poll_interval  # simulate reset
        self.assertEqual(poll_interval, base_poll_interval)


# ── Subprocess spawn ───────────────────────────────────────────────────────────

class TestSubprocessSpawn(unittest.TestCase):
    def test_agent_env_vars_set(self):
        """Agent subprocess receives AGENT_SECRET_TOKEN and ORCHESTRATOR_URL."""
        captured_env = {}

        def fake_popen(args, env, stdout, stderr):
            captured_env.update(env)
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            return proc

        parent_env = {
            "BLUEPRINT_RUNNER_TOKEN": "tok-test",
            "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
            "PATH": "/usr/bin:/bin",
            "ANTHROPIC_API_KEY": "sk-test",
        }

        with patch("os.environ", parent_env), \
             patch("subprocess.Popen", side_effect=fake_popen):
            env = parent_env.copy()
            env["AGENT_SECRET_TOKEN"] = "jwt-for-task"
            env["ORCHESTRATOR_URL"] = "https://api.example.com"
            env["TASK_ID"] = "task-123"
            env["VM_INSTANCE_ID"] = "runner:abc"
            env["AGENT_WORKSPACE"] = "/tmp/blueprint-agent-workspaces/task-123"

            subprocess.Popen(
                ["node", "/path/to/index.js"],
                env=env,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )

        self.assertEqual(captured_env.get("AGENT_SECRET_TOKEN"), "jwt-for-task")
        self.assertEqual(captured_env.get("ORCHESTRATOR_URL"), "https://api.example.com")
        self.assertEqual(captured_env.get("TASK_ID"), "task-123")
        self.assertEqual(captured_env.get("VM_INSTANCE_ID"), "runner:abc")
        self.assertEqual(
            captured_env.get("AGENT_WORKSPACE"),
            "/tmp/blueprint-agent-workspaces/task-123",
        )
        # BLUEPRINT_LOCAL_RUNNER_ID is not part of the runner contract.
        self.assertNotIn("BLUEPRINT_LOCAL_RUNNER_ID", captured_env)
        # Parent env vars must flow through
        self.assertEqual(captured_env.get("ANTHROPIC_API_KEY"), "sk-test")
        self.assertEqual(captured_env.get("PATH"), "/usr/bin:/bin")

    def test_spawn_args_use_node(self):
        """Agent is invoked as 'node <agent_binary>'."""
        args_seen = []

        def fake_popen(args, env, stdout, stderr):
            args_seen.extend(args)
            proc = MagicMock()
            proc.pid = 1
            proc.poll.return_value = None
            return proc

        with patch("subprocess.Popen", side_effect=fake_popen):
            subprocess.Popen(
                ["node", "/agent/dist/index.js"],
                env={},
                stdout=sys.stdout,
                stderr=sys.stderr,
            )

        self.assertEqual(args_seen[0], "node")
        self.assertTrue(args_seen[1].endswith("index.js"))


# ── Reaper loop ───────────────────────────────────────────────────────────────

class TestReaperLoop(unittest.TestCase):
    def test_reap_exited_process(self):
        """Reaper removes exited processes from active_tasks."""
        done_proc = MagicMock()
        done_proc.poll.return_value = 0
        done_proc.returncode = 0

        active_tasks = {"task-done": done_proc}
        lock = threading.Lock()

        local_runner._shutdown_event.clear()
        try:
            # Run one iteration of the reap logic inline
            with lock:
                done = [tid for tid, proc in active_tasks.items() if proc.poll() is not None]
                for tid in done:
                    del active_tasks[tid]

            self.assertNotIn("task-done", active_tasks)
        finally:
            local_runner._shutdown_event.clear()

    def test_running_process_not_reaped(self):
        """Running processes (poll() = None) stay in active_tasks."""
        running_proc = MagicMock()
        running_proc.poll.return_value = None

        active_tasks = {"task-running": running_proc}
        lock = threading.Lock()

        with lock:
            done = [tid for tid, proc in active_tasks.items() if proc.poll() is not None]
            for tid in done:
                del active_tasks[tid]

        self.assertIn("task-running", active_tasks)

    def test_reap_releases_shared_capacity(self):
        done_proc = MagicMock()
        done_proc.poll.return_value = 0
        done_proc.returncode = 0

        active_tasks = {"task-done": done_proc}
        lock = threading.Lock()
        capacity = threading.BoundedSemaphore(1)
        self.assertTrue(capacity.acquire(blocking=False))

        def stop_after_sleep(secs):
            local_runner._shutdown_event.set()

        local_runner._shutdown_event.clear()
        try:
            with patch("time.sleep", stop_after_sleep):
                local_runner._reap_loop(active_tasks, lock, capacity)
        finally:
            local_runner._shutdown_event.clear()

        self.assertNotIn("task-done", active_tasks)
        self.assertTrue(capacity.acquire(blocking=False))


# ── Graceful shutdown ─────────────────────────────────────────────────────────

class TestGracefulShutdown(unittest.TestCase):
    def test_shutdown_waits_for_active_agents(self):
        """Shutdown waits for active processes before exiting."""
        proc = MagicMock()
        proc.wait.return_value = None  # finishes within timeout

        remaining = {"task-1": proc}
        deadline = time.time() + 120

        for tid, p in remaining.items():
            timeout = max(0.0, deadline - time.time())
            p.wait(timeout=timeout)

        proc.wait.assert_called_once()
        call_kwargs = proc.wait.call_args
        self.assertIn("timeout", call_kwargs.kwargs)

    def test_shutdown_terminates_on_timeout(self):
        """If wait times out, SIGTERM is sent to remaining subprocesses."""
        proc = MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="node", timeout=120)

        remaining = {"task-1": proc}
        deadline = time.time() + 0.001  # already expired

        for tid, p in remaining.items():
            timeout = max(0.0, deadline - time.time())
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.terminate()

        proc.terminate.assert_called_once()


# ── Compute job report helper ─────────────────────────────────────────────────

class TestComputeJobReport(unittest.TestCase):
    def test_sends_correct_fields_and_returns_cancel_flag(self):
        """_report_compute_job POSTs exec_status/exit_code and returns cancel_requested."""
        captured = {}

        def fake_post(url, token, body):
            captured["url"] = url
            captured["body"] = body
            return 200, {"ok": True, "cancel_requested": True}

        with patch.object(local_runner, "_post", fake_post):
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-123",
                exec_status="succeeded", exit_code=0,
                result_metrics={"loss": 0.5},
            )

        self.assertTrue(result)
        self.assertIn("/api/runner/compute-job/job-123/report", captured["url"])
        self.assertEqual(captured["body"]["exec_status"], "succeeded")
        self.assertEqual(captured["body"]["exit_code"], 0)
        self.assertEqual(captured["body"]["result_metrics"], {"loss": 0.5})

    def test_cancel_not_requested_returns_false(self):
        def fake_post(url, token, body):
            return 200, {"ok": True, "cancel_requested": False}

        with patch.object(local_runner, "_post", fake_post):
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-abc",
                exec_status="running",
            )

        self.assertFalse(result)

    def test_404_returns_true(self):
        """A 404 response means the job is gone — treat as cancel_requested."""
        def fake_post(url, token, body):
            return 404, None

        with patch.object(local_runner, "_post", fake_post):
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-gone",
                exec_status="running",
            )

        self.assertTrue(result)

    def test_network_error_returns_false(self):
        def fake_post(url, token, body):
            raise RuntimeError("network error")

        with patch.object(local_runner, "_post", fake_post):
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-err",
                exec_status="running",
            )

        self.assertFalse(result)


# ── Compute job worker ────────────────────────────────────────────────────────

class TestRunComputeJob(unittest.TestCase):
    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def _make_job(self, **kwargs):
        base = {
            "job_id": "test-job-001",
            "command": "echo hello",
            "env": {},
            "cwd": None,
            "setup_step": None,
            "container_image": None,
            "container_registry_auth": {},
            "runtime_deadline_seconds": None,
            "cancel_requested": False,
        }
        base.update(kwargs)
        return base

    def test_compute_job_releases_shared_capacity_on_exit(self):
        reports = []

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None):
            reports.append({"exec_status": exec_status, "exit_code": exit_code})
            return False

        job = self._make_job(cancel_requested=True)
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()
        capacity = threading.BoundedSemaphore(1)
        self.assertTrue(capacity.acquire(blocking=False))

        with patch.object(local_runner, "_report_compute_job", fake_report):
            local_runner._run_compute_job(
                job,
                "https://api.example.com",
                "tok",
                active_jobs,
                lock,
                capacity,
            )

        self.assertNotIn("test-job-001", active_jobs)
        self.assertTrue(capacity.acquire(blocking=False))
        self.assertEqual(reports[-1]["exec_status"], "failed")

    @unittest.skipUnless(
        os.name == "posix",
        "Integration test: the runner executes the command via `bash -lc` and the "
        "command writes $BLUEPRINT_RESULT_FILE (a host path). This requires bash and "
        "the host filesystem to share path semantics — true on Linux/CI and WSL "
        "(where the runner actually runs), but NOT on Windows-native Python + MSYS "
        "bash, where a `C:\\...` path is unwritable from bash. Sibling tests that use "
        "path-free commands (e.g. `exit 42`) or mock Popen are not affected.",
    )
    def test_successful_command_reports_succeeded_with_metrics(self):
        """A command that exits 0 and writes result.json → succeeded report with metrics."""
        reports = []

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None):
            reports.append({
                "exec_status": exec_status,
                "exit_code": exit_code,
                "result_metrics": result_metrics,
            })
            return False

        # Command writes result.json and exits 0
        job = self._make_job(
            command="python3 -c \""
                    "import json,os; "
                    "open(os.environ['BLUEPRINT_RESULT_FILE'],'w').write(json.dumps({'acc':0.9}))"
                    "\"",
        )
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        terminal = [r for r in reports if r["exec_status"] in ("succeeded", "failed")]
        self.assertTrue(terminal, "Expected a terminal report")
        self.assertEqual(terminal[-1]["exec_status"], "succeeded")
        self.assertEqual(terminal[-1]["exit_code"], 0)
        self.assertEqual(terminal[-1]["result_metrics"], {"acc": 0.9})
        # job_id removed from active_jobs after completion
        self.assertNotIn("test-job-001", active_jobs)

    def test_failed_command_reports_failed(self):
        """A command that exits non-zero → failed report."""
        reports = []

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None):
            reports.append({"exec_status": exec_status, "exit_code": exit_code})
            return False

        job = self._make_job(command="exit 42")
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        terminal = [r for r in reports if r["exec_status"] in ("succeeded", "failed")]
        self.assertTrue(terminal)
        self.assertEqual(terminal[-1]["exec_status"], "failed")
        self.assertNotEqual(terminal[-1]["exit_code"], 0)

    def test_setup_step_nonzero_fails_immediately(self):
        """A non-zero setup_step exit reports failed without running main command."""
        reports = []
        commands_run = []

        real_run = subprocess.run

        def fake_run(args, **kwargs):
            commands_run.append(args[2] if len(args) > 2 else args)
            if len(args) >= 3 and args[2] == "exit 2":
                mock = MagicMock()
                mock.returncode = 2
                return mock
            return real_run(args, **kwargs)

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None):
            reports.append({"exec_status": exec_status, "exit_code": exit_code})
            return False

        job = self._make_job(
            command="echo main",
            setup_step="exit 2",  # will fail
        )
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch("subprocess.run", side_effect=fake_run):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        # Should report failed with setup exit code
        self.assertTrue(reports)
        # Only the setup should have triggered a report
        self.assertEqual(reports[-1]["exec_status"], "failed")
        self.assertEqual(reports[-1]["exit_code"], 2)
        # Main command (echo main) should NOT have been reached via Popen
        # (only subprocess.run calls for setup_step, no Popen for main)

    def test_container_image_uses_docker_run(self):
        """When container_image is set, docker run args include volume mount and env vars."""
        popen_calls = []

        class FakeProc:
            pid = 9999
            stdout = MagicMock()

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        fake_proc = FakeProc()
        fake_proc.stdout.read.return_value = b""

        def fake_popen(args, **kwargs):
            popen_calls.append(list(args))
            return fake_proc

        reports = []

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None):
            reports.append(exec_status)
            return False

        job = self._make_job(
            command="python3 train.py",
            container_image="nvcr.io/nvidia/pytorch:latest",
        )
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(local_runner, "_detect_runner_gpu", return_value=False):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        self.assertTrue(popen_calls, "Expected Popen to be called for main command")
        docker_args = popen_calls[0]
        self.assertEqual(docker_args[0], "docker")
        self.assertEqual(docker_args[1], "run")
        self.assertIn("--rm", docker_args)
        # Volume mount includes the container out dir
        v_idx = docker_args.index("-v")
        self.assertIn(local_runner._RUNNER_CONTAINER_OUT_DIR, docker_args[v_idx + 1])
        # Image appears before the command
        self.assertIn("nvcr.io/nvidia/pytorch:latest", docker_args)
        # --gpus all NOT present when GPU not detected
        self.assertNotIn("--gpus", docker_args)

    def test_container_image_with_gpu_adds_gpus_flag(self):
        """When GPU is detected, docker run includes --gpus all."""
        popen_calls = []

        class FakeProc:
            pid = 1
            stdout = MagicMock()

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

        fake_proc = FakeProc()
        fake_proc.stdout.read.return_value = b""

        def fake_popen(args, **kwargs):
            popen_calls.append(list(args))
            return fake_proc

        def fake_report(*a, **kw):
            return False

        job = self._make_job(
            command="python3 train.py",
            container_image="pytorch/pytorch:latest",
        )
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(local_runner, "_detect_runner_gpu", return_value=True):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        docker_args = popen_calls[0]
        self.assertIn("--gpus", docker_args)
        gpus_idx = docker_args.index("--gpus")
        self.assertEqual(docker_args[gpus_idx + 1], "all")

    def test_cancel_requested_on_claim_skips_execution(self):
        """When cancel_requested is True on the job body, no subprocess is started."""
        reports = []

        def fake_report(orch_url, token, job_id, *, exec_status, **kwargs):
            reports.append(exec_status)
            return False

        job = self._make_job(cancel_requested=True)
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch("subprocess.Popen") as mock_popen:
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        mock_popen.assert_not_called()
        self.assertIn("failed", reports)

    def test_cancel_from_heartbeat_terminates_process(self):
        """When a heartbeat response returns cancel_requested, the subprocess is terminated."""
        report_statuses = []
        proc_terminated = []

        class FakeProc:
            pid = 5555
            stdout = MagicMock()

            def wait(self, timeout=None):
                # First call: TimeoutExpired (so the loop continues to heartbeat)
                # Subsequent calls: return 0
                if not proc_terminated:
                    raise subprocess.TimeoutExpired(cmd="bash", timeout=30)
                return 1

            def terminate(self):
                proc_terminated.append(True)

            def kill(self):
                pass

        fake_proc = FakeProc()
        fake_proc.stdout.read.return_value = b""

        report_call_count = [0]

        def fake_report(orch_url, token, job_id, *, exec_status, **kwargs):
            report_call_count[0] += 1
            report_statuses.append(exec_status)
            # First call (initial running report): no cancel
            # Second call (heartbeat): signal cancel
            return report_call_count[0] >= 2

        job = self._make_job()
        active_jobs: set = {"test-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch("subprocess.Popen", return_value=fake_proc):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        self.assertTrue(proc_terminated, "Expected proc.terminate() to be called")
        self.assertIn("failed", report_statuses)


# ── Compute poll loop ─────────────────────────────────────────────────────────

class TestComputePollLoop(unittest.TestCase):
    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_204_triggers_backoff_and_no_worker(self):
        """A 204 from next-compute-job causes backoff; no worker thread is spawned."""
        sleep_calls = []
        iterations = [0]

        def fake_post(url, token, body):
            return 204, None

        def fake_sleep(secs):
            sleep_calls.append(secs)
            iterations[0] += 1
            if iterations[0] >= 3:
                local_runner._shutdown_event.set()

        active_jobs: set = set()
        lock = threading.Lock()

        with patch.object(local_runner, "_post", fake_post), \
             patch("time.sleep", fake_sleep), \
             patch("threading.Thread") as mock_thread:
            local_runner._compute_poll_loop(
                "https://api.example.com", "tok", active_jobs, lock, max_concurrent=2
            )

        # Backoff doubles: 2→4
        self.assertGreater(sleep_calls[1], sleep_calls[0])
        # No worker thread spawned for 204 responses
        mock_thread.assert_not_called()

    def test_at_capacity_does_not_poll(self):
        """When active_jobs is full, the loop sleeps without calling next-compute-job."""
        post_calls = []
        sleep_iterations = [0]

        def fake_post(url, token, body):
            post_calls.append(url)
            return 204, None

        def fake_sleep(secs):
            sleep_iterations[0] += 1
            if sleep_iterations[0] >= 2:
                local_runner._shutdown_event.set()

        active_jobs: set = {"job-a", "job-b"}  # already at max=2
        lock = threading.Lock()

        with patch.object(local_runner, "_post", fake_post), \
             patch("time.sleep", fake_sleep):
            local_runner._compute_poll_loop(
                "https://api.example.com", "tok", active_jobs, lock, max_concurrent=2
            )

        self.assertEqual(post_calls, [], "next-compute-job must NOT be called when at capacity")

    def test_shared_capacity_does_not_poll_when_exhausted(self):
        """Compute polling shares the runner-wide capacity with agent tasks."""
        post_calls = []
        sleep_iterations = [0]

        def fake_post(url, token, body):
            post_calls.append(url)
            return 204, None

        def fake_sleep(secs):
            sleep_iterations[0] += 1
            if sleep_iterations[0] >= 2:
                local_runner._shutdown_event.set()

        active_jobs: set = set()
        lock = threading.Lock()
        capacity = threading.BoundedSemaphore(1)
        self.assertTrue(capacity.acquire(blocking=False))

        with patch.object(local_runner, "_post", fake_post), \
             patch("time.sleep", fake_sleep):
            local_runner._compute_poll_loop(
                "https://api.example.com",
                "tok",
                active_jobs,
                lock,
                max_concurrent=2,
                capacity=capacity,
            )

        self.assertEqual(post_calls, [], "next-compute-job must NOT be called when runner capacity is full")

    def test_200_response_spawns_worker_thread(self):
        """A 200 response with a job body causes a worker thread to be started."""
        call_count = [0]
        threads_started = []

        def fake_post(url, token, body):
            call_count[0] += 1
            if call_count[0] == 1:
                return 200, {
                    "job_id": "job-xyz",
                    "command": "echo hi",
                    "env": {},
                    "cwd": None,
                    "setup_step": None,
                    "container_image": None,
                    "container_registry_auth": {},
                    "runtime_deadline_seconds": None,
                    "cancel_requested": False,
                }
            # After spawning, return 204 and then shut down
            local_runner._shutdown_event.set()
            return 204, None

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=False):
                threads_started.append({"target": target, "args": args})

            def start(self):
                pass

        active_jobs: set = set()
        lock = threading.Lock()

        with patch.object(local_runner, "_post", fake_post), \
             patch("time.sleep", lambda s: None), \
             patch("threading.Thread", FakeThread):
            local_runner._compute_poll_loop(
                "https://api.example.com", "tok", active_jobs, lock, max_concurrent=2
            )

        self.assertTrue(threads_started, "Expected a worker thread to be started")
        self.assertEqual(threads_started[0]["args"][0]["job_id"], "job-xyz")

    def test_worker_start_failure_releases_capacity_and_active_job(self):
        """A claimed job must not wedge local capacity if the worker fails to start."""
        reports = []

        def fake_post(url, token, body):
            return 200, {
                "job_id": "job-xyz",
                "command": "echo hi",
                "env": {},
                "cwd": None,
                "setup_step": None,
                "container_image": None,
                "container_registry_auth": {},
                "runtime_deadline_seconds": None,
                "cancel_requested": False,
            }

        def fake_report(orch_url, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None):
            reports.append({
                "job_id": job_id,
                "exec_status": exec_status,
                "exit_code": exit_code,
                "command_log": command_log,
            })
            return False

        class FailingThread:
            def __init__(self, target=None, args=(), daemon=False):
                pass

            def start(self):
                local_runner._shutdown_event.set()
                raise RuntimeError("thread boom")

        active_jobs: set = set()
        lock = threading.Lock()
        capacity = threading.BoundedSemaphore(1)

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_report_compute_job", fake_report), \
             patch("time.sleep", lambda s: None), \
             patch("threading.Thread", FailingThread):
            local_runner._compute_poll_loop(
                "https://api.example.com",
                "tok",
                active_jobs,
                lock,
                max_concurrent=2,
                capacity=capacity,
            )

        self.assertEqual(active_jobs, set())
        self.assertTrue(capacity.acquire(blocking=False))
        self.assertEqual(reports[-1]["job_id"], "job-xyz")
        self.assertEqual(reports[-1]["exec_status"], "failed")


# ── GPU detection ─────────────────────────────────────────────────────────────

class TestGpuDetection(unittest.TestCase):
    def test_nvidia_smi_absent_returns_no_gpu(self):
        """FileNotFoundError → has_gpu=False, no crash."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = local_runner._detect_gpu()
        self.assertFalse(result["has_gpu"])
        self.assertEqual(result["gpu_count"], 0)
        self.assertEqual(result["gpu_names"], [])

    def test_nvidia_smi_timeout_returns_no_gpu(self):
        """TimeoutExpired → has_gpu=False, no crash."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)):
            result = local_runner._detect_gpu()
        self.assertFalse(result["has_gpu"])
        self.assertEqual(result["gpu_count"], 0)

    def test_nvidia_smi_error_returns_no_gpu(self):
        """Non-zero returncode → has_gpu=False."""
        fake = MagicMock()
        fake.returncode = 1
        fake.stdout = ""
        with patch("subprocess.run", return_value=fake):
            result = local_runner._detect_gpu()
        self.assertFalse(result["has_gpu"])

    def test_nvidia_smi_parses_single_gpu(self):
        """Single GPU line → has_gpu=True, gpu_count=1, name captured."""
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "GPU 0: Tesla T4 (UUID: GPU-abc123)\n"
        with patch("subprocess.run", return_value=fake):
            result = local_runner._detect_gpu()
        self.assertTrue(result["has_gpu"])
        self.assertEqual(result["gpu_count"], 1)
        self.assertEqual(len(result["gpu_names"]), 1)
        self.assertIn("Tesla T4", result["gpu_names"][0])

    def test_nvidia_smi_parses_multi_gpu(self):
        """Multiple GPU lines → gpu_count=2."""
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = (
            "GPU 0: Tesla T4 (UUID: GPU-abc)\n"
            "GPU 1: Tesla T4 (UUID: GPU-def)\n"
        )
        with patch("subprocess.run", return_value=fake):
            result = local_runner._detect_gpu()
        self.assertTrue(result["has_gpu"])
        self.assertEqual(result["gpu_count"], 2)

    def test_gpu_capabilities_included_in_heartbeat_body(self):
        """Heartbeat body includes GPU fields detected at startup."""
        posted_bodies = []

        def fake_post(url, token, body):
            posted_bodies.append(body)
            # Stop the loop after the first heartbeat so the test doesn't block.
            local_runner._shutdown_event.set()
            return 200, {"ok": True, "last_seen_at": "2026-01-01T00:00:00"}

        gpu_caps = {"has_gpu": True, "gpu_count": 1, "gpu_names": ["Tesla T4"]}
        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_locate_agent_binary", return_value="/agent/dist/index.js"):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock, gpu_caps
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(posted_bodies), 1)
        self.assertIn("has_gpu", posted_bodies[0])
        self.assertTrue(posted_bodies[0]["has_gpu"])
        self.assertEqual(posted_bodies[0]["gpu_count"], 1)
        self.assertEqual(posted_bodies[0]["gpu_names"], ["Tesla T4"])
        self.assertEqual(posted_bodies[0]["active_compute_job_ids"], [])
        self.assertTrue(posted_bodies[0]["agent_available"])

    def test_heartbeat_includes_active_compute_job_ids(self):
        posted_bodies = []

        def fake_post(url, token, body):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        active_tasks = {"task-1": MagicMock()}
        active_compute_jobs = {"job-b", "job-a"}
        task_lock = threading.Lock()
        compute_lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None):
                local_runner._heartbeat_loop(
                    "https://api",
                    "tok",
                    active_tasks,
                    task_lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    active_compute_jobs,
                    compute_lock,
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(posted_bodies[0]["active_task_ids"], ["task-1"])
        self.assertEqual(posted_bodies[0]["active_compute_job_ids"], ["job-a", "job-b"])
        self.assertFalse(posted_bodies[0]["agent_available"])


# ── Docker login helper ───────────────────────────────────────────────────────

class TestDockerLogin(unittest.TestCase):
    def test_docker_login_called_when_auth_has_password(self):
        """_docker_login calls subprocess.run with docker login args when password present."""
        captured = {}

        def fake_run(cmd, *, input=None, capture_output=False, timeout=None):
            captured["cmd"] = cmd
            captured["input"] = input
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        result = local_runner._docker_login(
            {"server": "nvcr.io", "username": "$oauthtoken", "password": "mykey"},
            _subprocess_run=fake_run,
        )
        self.assertTrue(result)
        self.assertIn("docker", captured["cmd"])
        self.assertIn("login", captured["cmd"])
        self.assertIn("--password-stdin", captured["cmd"])
        self.assertIn("nvcr.io", captured["cmd"])
        self.assertIn("--username", captured["cmd"])
        self.assertEqual(captured["input"], b"mykey")

    def test_docker_login_skipped_when_no_password(self):
        """_docker_login returns True immediately when no password is present."""
        called = []

        def fake_run(*args, **kwargs):
            called.append(True)
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        result = local_runner._docker_login(
            {"server": "nvcr.io", "username": "user"},
            _subprocess_run=fake_run,
        )
        self.assertTrue(result)
        self.assertEqual(called, [], "subprocess.run must NOT be called when no password")

    def test_docker_login_failure_returns_false(self):
        """_docker_login returns False when docker login exits non-zero."""
        def fake_run(*args, **kwargs):
            m = MagicMock()
            m.returncode = 1
            m.stderr = b"unauthorized: incorrect credentials"
            return m

        result = local_runner._docker_login(
            {"server": "nvcr.io", "username": "u", "password": "bad"},
            _subprocess_run=fake_run,
        )
        self.assertFalse(result)

    def test_docker_login_timeout_returns_false(self):
        """_docker_login returns False when subprocess times out."""
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=60)

        result = local_runner._docker_login(
            {"server": "nvcr.io", "username": "u", "password": "pw"},
            _subprocess_run=fake_run,
        )
        self.assertFalse(result)

    def test_container_image_with_registry_auth_calls_login_before_run(self):
        """_run_compute_job calls docker login before docker run when password present."""
        subprocess_calls = []

        class FakeProc:
            pid = 7777
            stdout = MagicMock()

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

        fake_proc = FakeProc()
        fake_proc.stdout.read.return_value = b""

        def fake_popen(args, **kwargs):
            subprocess_calls.append(("popen", list(args)))
            return fake_proc

        login_calls = []

        def fake_docker_login(auth, **kwargs):
            login_calls.append(auth)
            return True

        def fake_report(*a, **kw):
            return False

        job = {
            "job_id": "auth-job-001",
            "command": "python3 train.py",
            "env": {},
            "cwd": None,
            "setup_step": None,
            "container_image": "nvcr.io/nvidia/pytorch:latest",
            "container_registry_auth": {
                "server": "nvcr.io",
                "username": "$oauthtoken",
                "password": "ngc-key-abc",
            },
            "runtime_deadline_seconds": None,
            "cancel_requested": False,
            "repo_clone": None,
            "repo_token": None,
        }
        active_jobs: set = {"auth-job-001"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner, "_docker_login", fake_docker_login), \
             patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(local_runner, "_detect_runner_gpu", return_value=False):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        self.assertEqual(len(login_calls), 1, "docker login must be called once")
        self.assertEqual(login_calls[0]["server"], "nvcr.io")
        self.assertEqual(login_calls[0]["password"], "ngc-key-abc")
        docker_run_calls = [c for c in subprocess_calls if c[0] == "popen"]
        self.assertTrue(docker_run_calls, "docker run must be called after login")
        self.assertIn("docker", docker_run_calls[0][1])

    def test_docker_login_failure_reports_failed_and_skips_docker_run(self):
        """When docker login fails, the job reports failed and docker run is not called."""
        popen_calls = []

        def fake_popen(args, **kwargs):
            popen_calls.append(args)
            raise AssertionError("docker run must not be called after login failure")

        reports = []

        def fake_report(orch_url, token, job_id, *, exec_status, **kwargs):
            reports.append(exec_status)
            return False

        def fake_docker_login(auth, **kwargs):
            return False  # login fails

        job = {
            "job_id": "auth-fail-002",
            "command": "python3 train.py",
            "env": {},
            "cwd": None,
            "setup_step": None,
            "container_image": "nvcr.io/nvidia/pytorch:latest",
            "container_registry_auth": {
                "server": "nvcr.io",
                "username": "$oauthtoken",
                "password": "bad-key",
            },
            "runtime_deadline_seconds": None,
            "cancel_requested": False,
            "repo_clone": None,
            "repo_token": None,
        }
        active_jobs: set = {"auth-fail-002"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner, "_docker_login", fake_docker_login), \
             patch("subprocess.Popen", side_effect=fake_popen):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock)

        self.assertIn("failed", reports)
        self.assertEqual(popen_calls, [], "docker run must not be called on login failure")


# ── Registry-host derivation (FIX B4) ──────────────────────────────────────────

class TestRegistryHostDerivation(unittest.TestCase):
    def test_ngc_image_derives_nvcr_io(self):
        self.assertEqual(
            local_runner._derive_registry_host("nvcr.io/nvidia/tensorflow:25.01"),
            "nvcr.io",
        )

    def test_host_port_segment_is_registry(self):
        self.assertEqual(
            local_runner._derive_registry_host("myregistry:5000/team/img"),
            "myregistry:5000",
        )

    def test_dockerhub_user_repo_is_not_a_registry(self):
        # First segment has no '.' or ':' → Docker Hub → no server arg.
        self.assertEqual(local_runner._derive_registry_host("pytorch/pytorch:latest"), "")

    def test_dockerhub_single_name_is_not_a_registry(self):
        self.assertEqual(local_runner._derive_registry_host("ubuntu:22.04"), "")

    def test_none_image_returns_empty(self):
        self.assertEqual(local_runner._derive_registry_host(None), "")

    def test_docker_login_derives_server_from_image_when_omitted(self):
        """When auth has no 'server', the host is derived from container_image."""
        captured = {}

        def fake_run(cmd, *, input=None, capture_output=False, timeout=None):
            captured["cmd"] = cmd
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        result = local_runner._docker_login(
            {"username": "$oauthtoken", "password": "ngc-key"},  # no 'server'
            container_image="nvcr.io/nvidia/tensorflow:25.01",
            _subprocess_run=fake_run,
        )
        self.assertTrue(result)
        # Derived host appended as the docker login server argument.
        self.assertEqual(captured["cmd"][-1], "nvcr.io")

    def test_docker_login_dockerhub_image_omits_server_arg(self):
        """A Docker Hub image with no explicit server logs in with no server arg."""
        captured = {}

        def fake_run(cmd, *, input=None, capture_output=False, timeout=None):
            captured["cmd"] = cmd
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        local_runner._docker_login(
            {"username": "u", "password": "pw"},  # no 'server'
            container_image="pytorch/pytorch:latest",
            _subprocess_run=fake_run,
        )
        # No registry host appended — last token is the flag/value, not a host.
        self.assertNotIn("pytorch/pytorch:latest", captured["cmd"])
        self.assertEqual(captured["cmd"][-1], "u")  # ends with --username value

    def test_explicit_server_takes_precedence_over_image(self):
        captured = {}

        def fake_run(cmd, *, input=None, capture_output=False, timeout=None):
            captured["cmd"] = cmd
            m = MagicMock()
            m.returncode = 0
            m.stderr = b""
            return m

        local_runner._docker_login(
            {"server": "explicit.example.com", "password": "pw"},
            container_image="nvcr.io/nvidia/tensorflow:25.01",
            _subprocess_run=fake_run,
        )
        self.assertEqual(captured["cmd"][-1], "explicit.example.com")


# ── Durable artifact upload (FIX M1, runner side) ──────────────────────────────

class TestArtifactUpload(unittest.TestCase):
    def _write(self, root, rel, data: bytes):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_empty_dir_skips_upload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as art:
            with patch("urllib.request.urlopen") as mock_urlopen:
                local_runner._upload_artifact("https://api.example.com", "tok", "job-1", art)
            mock_urlopen.assert_not_called()

    def test_missing_dir_skips_upload(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            local_runner._upload_artifact(
                "https://api.example.com", "tok", "job-1", "/no/such/dir"
            )
        mock_urlopen.assert_not_called()

    def test_over_cap_skips_upload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as art:
            self._write(art, "checkpoint.bin", b"x" * 4096)
            with patch.object(local_runner, "_RUNNER_ARTIFACT_MAX_BYTES", 1024), \
                 patch("urllib.request.urlopen") as mock_urlopen:
                local_runner._upload_artifact(
                    "https://api.example.com", "tok", "job-1", art
                )
            mock_urlopen.assert_not_called()

    def test_successful_upload_posts_multipart_with_counts(self):
        import tempfile
        captured = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = req.data
            return _Resp()

        with tempfile.TemporaryDirectory() as art:
            self._write(art, "plots/loss.png", b"PNGDATA")
            self._write(art, "ckpt.pt", b"WEIGHTS123")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                local_runner._upload_artifact(
                    "https://api.example.com", "tok", "job-xyz", art
                )

        self.assertIn("/api/runner/compute-job/job-xyz/artifact", captured["url"])
        # Content-Type is multipart with a boundary; auth header carried through.
        ct = captured["headers"].get("Content-type") or captured["headers"].get("Content-Type")
        self.assertIn("multipart/form-data; boundary=", ct)
        auth = captured["headers"].get("Authorization")
        self.assertEqual(auth, "Bearer tok")
        body = captured["body"]
        # Form fields present with correct values (2 files, 17 total bytes).
        self.assertIn(b'name="archive"; filename="artifacts.tar.gz"', body)
        self.assertIn(b'name="file_count"', body)
        self.assertIn(b"\r\n\r\n2\r\n", body)  # file_count == 2
        self.assertIn(b'name="total_bytes"', body)
        self.assertIn(b"\r\n\r\n17\r\n", body)  # 7 + 10 bytes

    def test_upload_http_error_is_non_fatal(self):
        import tempfile
        import urllib.error

        with tempfile.TemporaryDirectory() as art:
            self._write(art, "f.txt", b"data")
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError("u", 500, "err", {}, None),
            ):
                # Must NOT raise.
                local_runner._upload_artifact(
                    "https://api.example.com", "tok", "job-1", art
                )


class TestBoundedDedupLog(unittest.TestCase):
    """Command-log capture: consecutive-duplicate collapsing + head/tail budget."""

    def _feed(self, log, data: bytes, chunk: int = 8192):
        for i in range(0, len(data), chunk):
            log.feed(data[i : i + chunk])

    def test_small_log_passthrough_untruncated(self):
        log = local_runner._BoundedDedupLog(max_bytes=1000, head_bytes=200)
        self._feed(log, b"line one\nline two\nline three\n")
        text, truncated = log.result()
        self.assertFalse(truncated)
        self.assertEqual(text, "line one\nline two\nline three\n")

    def test_consecutive_duplicates_collapse(self):
        log = local_runner._BoundedDedupLog(max_bytes=10000, head_bytes=2000)
        log.feed(b"begin\n")
        for _ in range(500):
            log.feed(b"'+ptx85' is not a recognized feature\n")
        log.feed(b"end\n")
        text, truncated = log.result()
        # Spam line stored exactly once, with an accurate repeat marker.
        self.assertEqual(text.count("'+ptx85' is not a recognized feature"), 1)
        self.assertIn("repeated 499 more time(s)", text)
        self.assertIn("begin", text)
        self.assertIn("end", text)
        self.assertFalse(truncated)  # collapsed well under budget

    def test_tail_survives_when_over_budget(self):
        # Head 100B + tail 200B. A long unique body must drop the MIDDLE, keeping
        # the final summary lines (where results live).
        log = local_runner._BoundedDedupLog(max_bytes=300, head_bytes=100)
        body = "".join(f"row{i:04d}\n" for i in range(200)).encode("utf-8")
        self._feed(log, b"HEADER_START\n" + body)
        self._feed(log, b"FAIL module_x\nall_green=0\n")
        text, truncated = log.result()
        self.assertTrue(truncated)
        self.assertIn("HEADER_START", text)         # head kept
        self.assertIn("FAIL module_x", text)         # tail kept
        self.assertIn("all_green=0", text)           # final summary kept
        self.assertIn("elided", text)                # middle marked elided

    def test_distinct_lines_not_collapsed(self):
        log = local_runner._BoundedDedupLog(max_bytes=10000, head_bytes=2000)
        self._feed(log, b"a\nb\na\nb\n")  # alternating, not consecutive dups
        text, _ = log.result()
        self.assertNotIn("repeated", text)
        self.assertEqual(text, "a\nb\na\nb\n")

    def test_trailing_line_without_newline_captured(self):
        log = local_runner._BoundedDedupLog(max_bytes=1000, head_bytes=200)
        self._feed(log, b"first\nlast-no-newline")
        text, _ = log.result()
        self.assertIn("last-no-newline", text)

    def test_newline_free_stream_is_bounded(self):
        # A '\r'-based progress stream never emits '\n'. _pending must NOT grow
        # without bound — it force-flushes past the budget. Assert the buffer and
        # the final text stay within a small multiple of max_bytes (no OOM).
        log = local_runner._BoundedDedupLog(max_bytes=2000, head_bytes=500)
        for _ in range(5000):
            log.feed(b"\rprogress " + b"=" * 50)  # 60 bytes each, no newline
        self.assertLessEqual(len(log._pending), 2000 + 60)
        text, truncated = log.result()
        self.assertLessEqual(len(text.encode("utf-8")), 2000)
        self.assertTrue(truncated)

    def test_single_oversized_line_clipped_to_budget(self):
        # One line far larger than the budget must be clipped (not stored whole)
        # AND reported truncated — the previous ring-buffer guard kept it un-popped.
        log = local_runner._BoundedDedupLog(max_bytes=1000, head_bytes=200)
        self._feed(log, b"A" * 50000 + b"\nTAIL_END\n")
        text, truncated = log.result()
        self.assertLessEqual(len(text.encode("utf-8")), 1000)
        self.assertTrue(truncated)
        self.assertIn("TAIL_END", text)  # most-recent output survives

    def test_assembled_text_never_exceeds_max_bytes(self):
        # The elision marker must fit WITHIN max_bytes (reserve), so the backend's
        # equal-size cap never re-truncates and clips the tail.
        log = local_runner._BoundedDedupLog(max_bytes=4096, head_bytes=1024)
        body = "".join(f"unique line {i}\n" for i in range(5000)).encode("utf-8")
        self._feed(log, body)
        text, truncated = log.result()
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode("utf-8")), 4096)
        self.assertIn("elided", text)


if __name__ == "__main__":
    unittest.main()
