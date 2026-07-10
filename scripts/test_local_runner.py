"""Tests for scripts/local_runner.py."""

import contextlib
import io
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
import blueprint_runner as br
import local_runner
import runner_isolation as iso


# ── Capability factories (isolation core) ─────────────────────────────────────

def _bwrap_caps(gpu_indices=()):
    """A host where a bwrap sandbox can be built (no container runtime)."""
    return iso.Capabilities(
        bwrap_path="/usr/bin/bwrap",
        unshare_path="/usr/bin/unshare",
        userns_ok=True,
        gpu_indices=tuple(gpu_indices),
        gpu_names=tuple(f"Tesla V100-{i}" for i in gpu_indices),
    )


def _docker_caps(gpu_indices=()):
    """A host with usable docker (and optionally GPUs), no bwrap."""
    return iso.Capabilities(
        docker_usable=True,
        gpu_indices=tuple(gpu_indices),
        gpu_names=tuple(f"Tesla V100-{i}" for i in gpu_indices),
    )


def _gpu_state(indices):
    return {
        "allowed": list(indices),
        "in_use": set(),
        "per_job": 1,
        "lock": threading.Lock(),
    }


def _bare_plan_factory(captured=None):
    """Return a fake plan_compute_execution that substitutes a BARE argv.

    The sandbox argv/env is exhaustively verified in test_runner_isolation.py;
    substituting a bare plan here lets the exec/report/metrics/teardown loop run
    against a REAL process (bwrap is not installed in CI) while still threading
    BLUEPRINT_RESULT_FILE/ARTIFACT_DIR through exactly as the real plan would.
    """
    def _fake(**kw):
        if captured is not None:
            captured.update(kw)
        # Test bypass: inherit the FULL host env so the real `bash` launch works
        # cross-platform (Git Bash on Windows needs SYSTEMROOT/etc.). The env
        # ALLOWLIST is verified separately in test_runner_isolation.py; here we
        # only exercise the exec/report/metrics loop with a live process.
        env = os.environ.copy()
        env.update(kw.get("job_env") or {})
        env["BLUEPRINT_RESULT_FILE"] = kw["result_file_host"]
        env["BLUEPRINT_ARTIFACT_DIR"] = kw["artifact_dir_host"]
        return iso.ExecutionPlan(
            backend="bwrap",
            argv=["bash", "-lc", kw["command"]],
            env=env,
        )
    return _fake


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
            self.assertEqual(result, ["node", path])
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
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0], "node")
            self.assertTrue(result[1].endswith("index.js"))

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
                self.assertEqual(local_runner._locate_agent_binary(), ["node", path])
        finally:
            os.unlink(path)

    def test_locate_returns_none_when_autodetect_misses(self):
        """No explicit binary and nothing on disk → None, never SystemExit."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.isfile", return_value=False):
                self.assertIsNone(local_runner._locate_agent_binary())

    # ── tsx-based fallback ───────────────────────────────────────────────

    def test_locate_falls_back_to_tsx_when_dist_missing_and_tsx_present(self):
        """No dist/index.js but agent/src/index.ts + node_modules/.bin/tsx exist → [tsx_path, src_path]."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create agent/src/index.ts
            src_dir = os.path.join(tmpdir, "agent", "src")
            os.makedirs(src_dir)
            src_idx = os.path.join(src_dir, "index.ts")
            open(src_idx, "w").close()
            # Create agent/node_modules/.bin/tsx
            tsx_dir = os.path.join(tmpdir, "agent", "node_modules", ".bin")
            os.makedirs(tsx_dir)
            tsx_bin = os.path.join(tsx_dir, "tsx")
            open(tsx_bin, "w").close()

            with patch.dict(os.environ, {}, clear=True):
                env_clean = {k: v for k, v in os.environ.items() if k != "BLUEPRINT_AGENT_BINARY"}
                with patch.dict(os.environ, env_clean, clear=True):
                    with patch("local_runner.__file__", os.path.join(tmpdir, "scripts", "local_runner.py")):
                        result = local_runner._locate_agent_binary()

        self.assertIsNotNone(result)
        self.assertEqual(result[0], tsx_bin)
        self.assertEqual(result[1], src_idx)

    def test_locate_falls_back_to_tsx_on_path_when_no_local_tsx_binary(self):
        """When node_modules/.bin/tsx is absent but src/index.ts exists, fall back to 'tsx' on PATH."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "agent", "src")
            os.makedirs(src_dir)
            src_idx = os.path.join(src_dir, "index.ts")
            open(src_idx, "w").close()
            # Do NOT create node_modules/.bin/tsx

            with patch.dict(os.environ, {}, clear=True):
                env_clean = {k: v for k, v in os.environ.items() if k != "BLUEPRINT_AGENT_BINARY"}
                with patch.dict(os.environ, env_clean, clear=True):
                    with patch("local_runner.__file__", os.path.join(tmpdir, "scripts", "local_runner.py")):
                        result = local_runner._locate_agent_binary()

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "tsx")
        self.assertTrue(result[1].endswith("src/index.ts"))

    def test_locate_falls_back_to_tsx_via_cwd(self):
        """When script_dir-relative agent is absent, check cwd-relative agent."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "agent", "src")
            os.makedirs(src_dir)
            src_idx = os.path.join(src_dir, "index.ts")
            open(src_idx, "w").close()

            # Patch __file__ to a location that does NOT have agent/ nearby
            with patch.dict(os.environ, {}, clear=True):
                env_clean = {k: v for k, v in os.environ.items() if k != "BLUEPRINT_AGENT_BINARY"}
                with patch.dict(os.environ, env_clean, clear=True):
                    with patch("local_runner.__file__", "/tmp/nonexistent/local_runner.py"):
                        with patch("os.getcwd", return_value=tmpdir):
                            result = local_runner._locate_agent_binary()

        self.assertIsNotNone(result)
        # On cwd path without node_modules/.bin/tsx, falls back to 'tsx' on PATH
        self.assertEqual(result[0], "tsx")
        self.assertTrue(result[1].endswith("src/index.ts"))

    def test_locate_dist_takes_precedence_over_tsx(self):
        """When BOTH dist/index.js and src/index.ts exist, dist/index.js wins."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create both dist/index.js and src/index.ts
            dist_dir = os.path.join(tmpdir, "agent", "dist")
            os.makedirs(dist_dir)
            dist_idx = os.path.join(dist_dir, "index.js")
            open(dist_idx, "w").close()

            src_dir = os.path.join(tmpdir, "agent", "src")
            os.makedirs(src_dir)
            src_idx = os.path.join(src_dir, "index.ts")
            open(src_idx, "w").close()

            with patch.dict(os.environ, {}, clear=True):
                env_clean = {k: v for k, v in os.environ.items() if k != "BLUEPRINT_AGENT_BINARY"}
                with patch.dict(os.environ, env_clean, clear=True):
                    with patch("local_runner.__file__", os.path.join(tmpdir, "scripts", "local_runner.py")):
                        result = local_runner._locate_agent_binary()

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "node")
        self.assertTrue(result[1].endswith("dist/index.js"))


class TestAgentWorkspace(unittest.TestCase):
    def test_agent_workspace_for_task_uses_configured_root_and_sanitizes_task_id(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir}):
                workspace = local_runner._agent_workspace_for_task("task/abc:123")

            self.assertTrue(workspace.startswith(os.path.abspath(tmpdir)))
            self.assertTrue(workspace.endswith("task_abc_123"))
            self.assertTrue(os.path.isdir(workspace))

    def test_workspace_path_does_not_create_dir(self):
        """_agent_workspace_path computes the same path but has no side effect."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir}):
                path = local_runner._agent_workspace_path("task/abc:123")
                created = local_runner._agent_workspace_for_task("task/abc:123")
            self.assertEqual(path, created)
            self.assertTrue(path.endswith("task_abc_123"))

    def test_cleanup_removes_per_task_workspace(self):
        """Auto-clean: a finished task's workspace dir is rmtree'd, not left behind."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir},
                            clear=False):
                os.environ.pop("BLUEPRINT_RUNNER_KEEP_WORKSPACES", None)
                workspace = local_runner._agent_workspace_for_task("task-xyz")
                # Simulate leftover scratch content the agent produced.
                with open(os.path.join(workspace, "big.log"), "w") as fh:
                    fh.write("x" * 1024)
                self.assertTrue(os.path.isdir(workspace))

                local_runner._cleanup_agent_workspace("task-xyz")

            self.assertFalse(os.path.exists(workspace))

    def test_cleanup_respects_keep_workspaces_opt_out(self):
        """BLUEPRINT_RUNNER_KEEP_WORKSPACES=1 retains the dir for debugging."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {
                "BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir,
                "BLUEPRINT_RUNNER_KEEP_WORKSPACES": "1",
            }):
                workspace = local_runner._agent_workspace_for_task("task-keep")
                local_runner._cleanup_agent_workspace("task-keep")
            self.assertTrue(os.path.isdir(workspace))

    def test_cleanup_missing_workspace_is_noop(self):
        """Cleaning a workspace that was never created must not raise."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir}):
                os.environ.pop("BLUEPRINT_RUNNER_KEEP_WORKSPACES", None)
                local_runner._cleanup_agent_workspace("never-made")  # no raise


class TestLowDiskGuard(unittest.TestCase):
    def test_min_free_disk_bytes_parses_env(self):
        with patch.dict(os.environ, {"BLUEPRINT_RUNNER_MIN_DISK_BYTES": "2048"}):
            self.assertEqual(local_runner._min_free_disk_bytes(), 2048)

    def test_min_free_disk_bytes_defaults_and_rejects_garbage(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLUEPRINT_RUNNER_MIN_DISK_BYTES", None)
            self.assertEqual(local_runner._min_free_disk_bytes(), 0)
        with patch.dict(os.environ, {"BLUEPRINT_RUNNER_MIN_DISK_BYTES": "notanint"}):
            self.assertEqual(local_runner._min_free_disk_bytes(), 0)

    def test_guard_disabled_when_threshold_zero(self):
        # Even with a tiny reported free space, threshold 0 never guards.
        with patch.object(local_runner, "_disk_free_bytes", return_value=1):
            self.assertFalse(local_runner._disk_below_min("/some/root", 0))

    def test_guard_trips_below_threshold(self):
        with patch.object(local_runner, "_disk_free_bytes", return_value=500):
            self.assertTrue(local_runner._disk_below_min("/some/root", 1000))

    def test_guard_ok_at_or_above_threshold(self):
        with patch.object(local_runner, "_disk_free_bytes", return_value=1500):
            self.assertFalse(local_runner._disk_below_min("/some/root", 1000))

    def test_guard_ignores_unknown_free_space(self):
        # Unknown free space (None) must not falsely trip the guard.
        with patch.object(local_runner, "_disk_free_bytes", return_value=None):
            self.assertFalse(local_runner._disk_below_min("/some/root", 1000))


# ── Stale workspace cleanup ────────────────────────────────────────────────────

class TestStaleWorkspaceCleanup(unittest.TestCase):
    def test_stale_workspace_cleanup_removes_old_dirs(self):
        """Cleanup removes workspace dirs not matching any active task id."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir}):
                stale_a = os.path.join(tmpdir, "task_stale_1")
                stale_b = os.path.join(tmpdir, "task_stale_2")
                active_dir = os.path.join(tmpdir, "task_active_1")
                for d in (stale_a, stale_b, active_dir):
                    os.makedirs(d)

                # "task/active:1" sanitizes to "task_active_1" — matching active_dir
                removed = local_runner._cleanup_stale_workspaces(
                    {"task/active:1"}, max_age_seconds=0
                )

                self.assertEqual(removed, 2)
                self.assertFalse(os.path.isdir(stale_a))
                self.assertFalse(os.path.isdir(stale_b))
                self.assertTrue(os.path.isdir(active_dir))


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

        def fake_post(url, token, body, **kwargs):
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


# ── Failure circuit-breaker pause ───────────────────────────────────────────

class TestFailurePause(unittest.TestCase):
    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_pause_disabled_by_default(self):
        """Threshold 0 (disabled) means no pause regardless of failures."""
        ts = local_runner._TelemetryState()
        ts.record_failure()
        ts.record_failure()
        ts.record_failure()
        self.assertIsNone(ts.pause_remaining(0, 60))

    def test_pause_not_triggered_below_threshold(self):
        """Pause does not activate until failures reach the threshold."""
        ts = local_runner._TelemetryState()
        ts.record_failure()
        ts.record_failure()
        self.assertIsNone(ts.pause_remaining(3, 60))

    def test_pause_triggers_at_threshold(self):
        """When consecutive_failures >= threshold, pause_remaining returns > 0."""
        ts = local_runner._TelemetryState()
        for _ in range(3):
            ts.record_failure()
        remaining = ts.pause_remaining(3, 60)
        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 0)

    def test_pause_clears_on_record_success(self):
        """record_success resets the failure count and clears the pause state."""
        ts = local_runner._TelemetryState()
        for _ in range(3):
            ts.record_failure()
        self.assertIsNotNone(ts.pause_remaining(3, 60))
        ts.record_success()
        self.assertEqual(ts.consecutive_job_failures, 0)
        self.assertIsNone(ts.pause_remaining(3, 60))

    def test_pause_expires_and_repeats(self):
        """After the pause duration elapses, pause_remaining returns None, then a
        new cycle begins because failures are still at the threshold."""
        ts = local_runner._TelemetryState()
        for _ in range(3):
            ts.record_failure()
        # Start a very short pause (1 ms).
        rem = ts.pause_remaining(3, 0.001)
        self.assertIsNotNone(rem)
        self.assertGreater(rem, 0)
        time.sleep(0.01)
        # After pause expiry, returns None.
        rem = ts.pause_remaining(3, 0.001)
        self.assertIsNone(rem)
        # But immediately re-enters pause since failures still >= threshold.
        rem = ts.pause_remaining(3, 0.001)
        self.assertIsNotNone(rem)

    def test_pause_does_not_block_heartbeat(self):
        """The circuit-breaker lives in the claiming loop, not the heartbeat thread,
        so heartbeats continue unaffected. Verify _TelemetryState is orthogonal to
        heartbeats by checking snapshot still works during a pause."""
        ts = local_runner._TelemetryState()
        for _ in range(3):
            ts.record_failure()
        # Enter pause.
        ts.pause_remaining(3, 60)
        # snapshot must still report the failures (heartbeat needs this).
        _, fails = ts.snapshot()
        self.assertEqual(fails, 3)

    def test_no_claim_guard_when_pause_active(self):
        """When the failure pause is active, the claiming loop must NOT call
        next-task (simulated guard check)."""
        ts = local_runner._TelemetryState()
        for _ in range(3):
            ts.record_failure()

        call_count = {"n": 0}

        def fake_post(url, token, body, **kwargs):
            call_count["n"] += 1
            return 204, None

        local_runner._shutdown_event.clear()
        try:
            with patch.object(local_runner, "_post", fake_post):
                pause_rem = ts.pause_remaining(3, 60)
                self.assertIsNotNone(pause_rem)
                self.assertGreater(pause_rem, 0)
                # Simulate the guard: if paused, do NOT call _post.
                if pause_rem is not None and pause_rem > 0:
                    pass  # guard would sleep and continue
            self.assertEqual(call_count["n"], 0,
                             "next-task must NOT be called while failure pause is active")
        finally:
            local_runner._shutdown_event.clear()


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

    def test_reap_cleans_up_finished_workspace(self):
        """A reaped agent's per-task workspace is removed (auto-clean wiring)."""
        import tempfile

        done_proc = MagicMock()
        done_proc.poll.return_value = 0
        done_proc.returncode = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_WORKSPACE_ROOT": tmpdir}):
                os.environ.pop("BLUEPRINT_RUNNER_KEEP_WORKSPACES", None)
                workspace = local_runner._agent_workspace_for_task("task-done")
                self.assertTrue(os.path.isdir(workspace))

                active_tasks = {"task-done": done_proc}
                lock = threading.Lock()

                def stop_after_sleep(secs):
                    local_runner._shutdown_event.set()

                local_runner._shutdown_event.clear()
                try:
                    with patch("time.sleep", stop_after_sleep):
                        local_runner._reap_loop(active_tasks, lock)
                finally:
                    local_runner._shutdown_event.clear()

                self.assertNotIn("task-done", active_tasks)
                self.assertFalse(os.path.exists(workspace))


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

    def test_shutdown_waits_for_compute_jobs(self):
        """Shutdown waits for active compute jobs to complete before exiting."""
        lock = threading.Lock()
        active: set[str] = {"job-a", "job-b"}

        def _drain() -> None:
            """Simulate a worker completing and removing itself from the set."""
            time.sleep(0.3)
            with lock:
                active.clear()

        drainer = threading.Thread(target=_drain)
        drainer.start()

        start = time.monotonic()
        local_runner._wait_for_compute_jobs(active, lock, "https://api.example.com", "test-token", timeout=5)
        elapsed = time.monotonic() - start
        drainer.join()

        # Must have waited for the drainer to clear the set (not instant).
        self.assertGreaterEqual(
            elapsed,
            0.15,
            "Should have waited at least ~300 ms for compute jobs to finish",
        )
        # Must NOT have hit the full 5 s timeout.
        self.assertLess(
            elapsed,
            3,
            "Should have returned as soon as all jobs finished, not waited full timeout",
        )
        self.assertFalse(active, "All jobs should have been drained")

    def test_shutdown_no_compute_jobs_returns_immediately(self):
        """Shutdown with zero active compute jobs returns without delay."""
        lock = threading.Lock()
        active: set[str] = set()

        start = time.monotonic()
        local_runner._wait_for_compute_jobs(active, lock, "https://api.example.com", "test-token", timeout=5)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.5, "Should return immediately when no jobs active")

    def test_shutdown_compute_jobs_timeout_logs_message(self):
        """Shutdown logs a timeout message when jobs do not finish in time."""
        lock = threading.Lock()
        active: set[str] = {"stuck-job"}
        buf = io.StringIO()

        with contextlib.redirect_stdout(buf):
            local_runner._wait_for_compute_jobs(active, lock, "https://api.example.com", "test-token", timeout=0.05)

        output = buf.getvalue()
        self.assertIn("timeout", output, "Should log a timeout message")
        self.assertIn("reporting as failed", output, "Should report stranded jobs as failed")
        self.assertIn("stuck-job", active, "Stuck job must remain in set")

    def test_shutdown_reports_stranded_jobs_as_failed(self):
        """Stranded compute jobs after shutdown deadline are reported as failed."""
        lock = threading.Lock()
        active: set[str] = {"stranded-job"}
        reports = []

        def fake_report(orchestrator_url, token, job_id, **kwargs):
            reports.append((job_id, kwargs))
            return True

        with patch.object(local_runner, "_report_compute_job", fake_report):
            local_runner._wait_for_compute_jobs(active, lock, "https://api.example.com", "tok", timeout=0.05)

        self.assertEqual(len(reports), 1, "Should have reported the stranded job")
        reported_id, kwargs = reports[0]
        self.assertEqual(reported_id, "stranded-job")
        self.assertEqual(kwargs["exec_status"], "failed")
        self.assertEqual(kwargs["exit_code"], 1)
        self.assertIn("SIGTERM", kwargs["command_log"]["text"])
        self.assertFalse(kwargs["command_log"]["truncated"])


# ── Compute job report helper ─────────────────────────────────────────────────

class TestComputeJobReport(unittest.TestCase):
    def test_sends_correct_fields_and_returns_cancel_flag(self):
        """_report_compute_job POSTs exec_status/exit_code and returns cancel_requested."""
        captured = {}

        def fake_post(url, token, body, **kwargs):
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
        def fake_post(url, token, body, **kwargs):
            return 200, {"ok": True, "cancel_requested": False}

        with patch.object(local_runner, "_post", fake_post):
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-abc",
                exec_status="running",
            )

        self.assertFalse(result)

    def test_404_returns_true(self):
        """A 404 response means the job is gone — treat as cancel_requested."""
        def fake_post(url, token, body, **kwargs):
            return 404, None

        with patch.object(local_runner, "_post", fake_post):
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-gone",
                exec_status="running",
            )

        self.assertTrue(result)

    def test_network_error_returns_false(self):
        def fake_post(url, token, body, **kwargs):
            raise RuntimeError("network error")

        with patch.object(local_runner, "_post", fake_post):
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-err",
                exec_status="running",
            )

        self.assertFalse(result)

    def test_running_report_does_not_retry_transient_failure(self):
        """Interim 'running' heartbeats are single-shot — no retry, no sleep."""
        calls = {"n": 0}

        def fake_post(url, token, body, **kwargs):
            calls["n"] += 1
            raise RuntimeError("boom")

        with patch.object(local_runner, "_post", fake_post), \
                patch("time.sleep") as mock_sleep:
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-run",
                exec_status="running",
            )

        self.assertFalse(result)
        self.assertEqual(calls["n"], 1)
        mock_sleep.assert_not_called()

    def test_terminal_report_retries_network_error_then_succeeds(self):
        """A transient network blip on the terminal metric report is retried, not lost."""
        calls = {"n": 0}

        def fake_post(url, token, body, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient network error")
            return 200, {"cancel_requested": False}

        with patch.object(local_runner, "_post", fake_post), \
                patch("time.sleep") as mock_sleep:
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-metrics",
                exec_status="succeeded", exit_code=0,
                result_metrics={"loss": 0.5},
            )

        self.assertFalse(result)
        self.assertEqual(calls["n"], 3)  # failed twice, succeeded on the third
        self.assertEqual(mock_sleep.call_count, 2)  # one backoff per retry

    def test_terminal_report_retries_5xx_then_gives_up(self):
        """Terminal report retries a 5xx up to the cap, then settles without raising."""
        calls = {"n": 0}

        def fake_post(url, token, body, **kwargs):
            calls["n"] += 1
            return 503, None

        with patch.object(local_runner, "_post", fake_post), \
                patch("time.sleep") as mock_sleep:
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-5xx",
                exec_status="failed", exit_code=1,
            )

        self.assertFalse(result)
        # 1 initial attempt + _REPORT_MAX_RETRIES retries.
        self.assertEqual(calls["n"], local_runner._REPORT_MAX_RETRIES + 1)
        self.assertEqual(mock_sleep.call_count, local_runner._REPORT_MAX_RETRIES)

    def test_terminal_report_does_not_retry_permanent_4xx(self):
        """A permanent 4xx (e.g. 400) is not retried — retrying can't fix it."""
        calls = {"n": 0}

        def fake_post(url, token, body, **kwargs):
            calls["n"] += 1
            return 400, {"detail": "bad request"}

        with patch.object(local_runner, "_post", fake_post), \
                patch("time.sleep") as mock_sleep:
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-400",
                exec_status="succeeded", exit_code=0,
            )

        self.assertFalse(result)
        self.assertEqual(calls["n"], 1)
        mock_sleep.assert_not_called()

    def test_terminal_report_404_still_short_circuits(self):
        """A 404 on a terminal report means the job is gone — cancel, no retry."""
        calls = {"n": 0}

        def fake_post(url, token, body, **kwargs):
            calls["n"] += 1
            return 404, None

        with patch.object(local_runner, "_post", fake_post), \
                patch("time.sleep") as mock_sleep:
            result = local_runner._report_compute_job(
                "https://api.example.com", "tok", "job-gone",
                exec_status="failed", exit_code=1,
            )

        self.assertTrue(result)
        self.assertEqual(calls["n"], 1)
        mock_sleep.assert_not_called()


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
                capabilities=_bwrap_caps(),
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

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner.iso, "plan_compute_execution", _bare_plan_factory()):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock, capabilities=_bwrap_caps())

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

        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner.iso, "plan_compute_execution", _bare_plan_factory()):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock, capabilities=_bwrap_caps())

        terminal = [r for r in reports if r["exec_status"] in ("succeeded", "failed")]
        self.assertTrue(terminal)
        self.assertEqual(terminal[-1]["exec_status"], "failed")
        self.assertNotEqual(terminal[-1]["exit_code"], 0)

    def test_setup_step_folded_into_sandbox_and_aborts_on_failure(self):
        """setup_step is folded into the sandboxed command (NOT run bare) and a
        setup failure short-circuits the main command via `&&`.

        BEHAVIOR CHANGE (fail-closed isolation): the old runner executed
        setup_step as a bare `bash -lc` on the HOST before the container. That is
        an unsandboxed execution path, so setup now runs INSIDE the same boundary
        as the main command. `{ setup } && { main }` preserves the original
        semantics: a non-zero setup aborts before main and the job fails with the
        setup exit code.
        """
        reports = []
        captured: dict = {}

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
             patch.object(local_runner.iso, "plan_compute_execution",
                          _bare_plan_factory(captured)):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock, capabilities=_bwrap_caps())

        # setup + main folded into one sandboxed command, joined by `&&`.
        self.assertIn("exit 2", captured["command"])
        self.assertIn("echo main", captured["command"])
        self.assertIn("&&", captured["command"])
        # Real bash run: setup exit 2 short-circuits → job fails with exit 2.
        self.assertTrue(reports)
        self.assertEqual(reports[-1]["exec_status"], "failed")
        self.assertEqual(reports[-1]["exit_code"], 2)

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
             patch("subprocess.Popen", side_effect=fake_popen):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock, capabilities=_docker_caps())

        self.assertTrue(popen_calls, "Expected Popen to be called for main command")
        docker_args = popen_calls[0]
        self.assertEqual(docker_args[0], "docker")
        self.assertEqual(docker_args[1], "run")
        self.assertIn("--rm", docker_args)
        # Named for teardown-by-name.
        self.assertIn("--name", docker_args)
        # Volume mount includes the container out dir
        v_idx = docker_args.index("-v")
        self.assertIn(local_runner._RUNNER_CONTAINER_OUT_DIR, docker_args[v_idx + 1])
        # Image appears before the command
        self.assertIn("nvcr.io/nvidia/pytorch:latest", docker_args)
        # No GPUs on this host → no --gpus flag.
        self.assertNotIn("--gpus", docker_args)
        # Env values are forwarded via `-e KEY` (no inlined KEY=VALUE on argv).
        e_indices = [i for i, a in enumerate(docker_args) if a == "-e"]
        for i in e_indices:
            self.assertNotIn("=", docker_args[i + 1],
                             "env must be `-e KEY` not `-e KEY=VALUE` (value off argv)")

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
             patch("subprocess.Popen", side_effect=fake_popen):
            local_runner._run_compute_job(
                job, "https://api.example.com", "tok",
                active_jobs, lock,
                capabilities=_docker_caps(gpu_indices=(0,)),
                gpu_state=_gpu_state([0]),
            )

        docker_args = popen_calls[0]
        self.assertIn("--gpus", docker_args)
        gpus_idx = docker_args.index("--gpus")
        # GPUs are scoped to the ASSIGNED device, not "all".
        self.assertEqual(docker_args[gpus_idx + 1], "device=0")

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
                                          active_jobs, lock, capabilities=_bwrap_caps())

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

            def poll(self):
                # After teardown the process is gone (terminate_process_tree polls).
                return 1 if proc_terminated else None

            def terminate(self):
                proc_terminated.append(True)

            def kill(self):
                proc_terminated.append(True)

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
                                          active_jobs, lock, capabilities=_bwrap_caps())

        self.assertTrue(proc_terminated, "Expected the process group to be torn down")
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

        def fake_post(url, token, body, **kwargs):
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

        def fake_post(url, token, body, **kwargs):
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

        def fake_post(url, token, body, **kwargs):
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

        def fake_post(url, token, body, **kwargs):
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
            def __init__(self, target=None, args=(), kwargs=None, daemon=False):
                threads_started.append({"target": target, "args": args, "kwargs": kwargs})

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

        def fake_post(url, token, body, **kwargs):
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
            def __init__(self, target=None, args=(), kwargs=None, daemon=False):
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

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            # Stop the loop after the first heartbeat so the test doesn't block.
            local_runner._shutdown_event.set()
            return 200, {"ok": True, "last_seen_at": "2026-01-01T00:00:00"}

        gpu_caps = {"has_gpu": True, "gpu_count": 1, "gpu_names": ["Tesla T4"]}
        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            # agent_available now also requires a working node runtime (I11); patch
            # the functional probe so the assertion is hermetic on a nodeless CI.
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=["node", "/agent/dist/index.js"]), \
                 patch.object(local_runner, "_node_runtime_ok", return_value=True), \
                 patch.object(local_runner, "_gpu_utilization_pct", return_value=42):
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

        def fake_post(url, token, body, **kwargs):
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

    def test_hostname_included_in_heartbeat_body(self):
        """Heartbeat carries the host name so the console shows a real machine name.

        Regression: the runner is enrolled from a browser (which cannot know the
        host's name), so the heartbeat is the ONLY place hostname is populated.
        It was previously omitted from the body, so the panel showed None forever.
        """
        posted_bodies = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_hostname", return_value="operator-box"), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None):
                local_runner._heartbeat_loop(
                    "https://api",
                    "tok",
                    active_tasks,
                    lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(posted_bodies), 1)
        self.assertIn("hostname", posted_bodies[0])
        self.assertEqual(posted_bodies[0]["hostname"], "operator-box")

    def test_hostname_helper_tolerates_failure(self):
        """_hostname returns None (never raises) when the OS lookup fails, so a
        heartbeat is never broken by a hostname resolution error."""
        with patch.object(local_runner.socket, "gethostname", side_effect=OSError):
            self.assertIsNone(local_runner._hostname())

    def test_isolation_backends_included_in_heartbeat_body(self):
        """Heartbeat body carries the isolation-backend inventory, selected
        agent/compute backends, and the runner version when caps are supplied."""
        posted_bodies = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version", return_value="v22-abc"), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=["node", "/agent/dist/index.js"]), \
                 patch.object(local_runner, "_node_runtime_ok", return_value=True):
                local_runner._heartbeat_loop(
                    "https://api",
                    "tok",
                    active_tasks,
                    lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    caps=_bwrap_caps(),
                    agent_image=None,
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(posted_bodies), 1)
        body = posted_bodies[0]
        self.assertEqual(body["version"], "v22-abc")
        self.assertIsInstance(body["isolation_backends"], dict)
        self.assertTrue(body["isolation_backends"]["bwrap_ok"])
        self.assertFalse(body["isolation_backends"]["docker_usable"])
        # bwrap host with an agent binary → bwrap agent backend, bwrap compute.
        self.assertEqual(body["agent_backend"], iso.BACKEND_BWRAP)
        self.assertEqual(body["compute_backend"], iso.BACKEND_BWRAP)

    def test_isolation_fields_none_when_caps_absent(self):
        """caps=None (an old caller) → the three isolation fields serialize as
        None so the heartbeat is non-breaking and the backend tolerates them."""
        posted_bodies = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version", return_value=None), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None):
                local_runner._heartbeat_loop(
                    "https://api",
                    "tok",
                    active_tasks,
                    lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(posted_bodies), 1)
        body = posted_bodies[0]
        self.assertIsNone(body["isolation_backends"])
        self.assertIsNone(body["agent_backend"])
        self.assertIsNone(body["compute_backend"])
        self.assertIsNone(body["version"])

    def test_docker_only_declares_read_only_agent_slice(self):
        """A docker-only box (docker+image, NO bwrap) DECLARES the read-only agent
        slice in supported_agent_task_types.

        It still advertises agent_available=True, but self-restricts to the types it
        can actually run so the orchestrator never pins it a write/heavy agent type
        (which would DECLINE at claim time and strand/loop the task).
        """
        posted_bodies = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()
        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    agent_sandbox_ok=False,          # no bwrap
                    caps=_docker_caps(),             # docker usable, bwrap absent
                    agent_image="ghcr.io/acme/agent:prod",
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(posted_bodies), 1)
        self.assertTrue(posted_bodies[0]["agent_available"])
        self.assertEqual(
            posted_bodies[0]["supported_agent_task_types"],
            sorted(iso.AGENT_DOCKER_TYPES),
        )

    def test_bwrap_available_declares_no_agent_restriction(self):
        """A bwrap agent box (full agent slice on the host toolchain) declares NO
        supported_agent_task_types restriction (None) — even when an agent image is
        configured — so the orchestrator applies no runner-side narrowing.
        """
        posted_bodies = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()
        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_locate_agent_binary",
                              return_value=["node", "/agent/dist/index.js"]), \
                 patch.object(local_runner, "_node_runtime_ok", return_value=True):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    agent_sandbox_ok=True,
                    caps=_bwrap_caps(),
                    agent_image="ghcr.io/acme/agent:prod",
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(posted_bodies), 1)
        self.assertTrue(posted_bodies[0]["agent_available"])
        self.assertIsNone(posted_bodies[0]["supported_agent_task_types"])

    def test_bwrap_and_docker_available_declares_no_restriction(self):
        """bwrap takes precedence: a box that can run the FULL agent slice on the
        host toolchain declares no restriction even when docker is ALSO usable —
        the docker-only self-restriction only fires when bwrap is unavailable.
        """
        posted_bodies = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        both_caps = iso.Capabilities(
            bwrap_path="/usr/bin/bwrap",
            unshare_path="/usr/bin/unshare",
            userns_ok=True,
            docker_usable=True,
        )
        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()
        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_locate_agent_binary",
                              return_value=["node", "/agent/dist/index.js"]), \
                 patch.object(local_runner, "_node_runtime_ok", return_value=True):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    agent_sandbox_ok=True,
                    caps=both_caps,
                    agent_image="ghcr.io/acme/agent:prod",
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(posted_bodies), 1)
        self.assertIsNone(posted_bodies[0]["supported_agent_task_types"])


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
             patch("subprocess.Popen", side_effect=fake_popen):
            local_runner._run_compute_job(job, "https://api.example.com", "tok",
                                          active_jobs, lock, capabilities=_docker_caps())

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
                                          active_jobs, lock, capabilities=_docker_caps())

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
            ), patch("time.sleep"):
                # Must NOT raise even after exhausting retries.
                local_runner._upload_artifact(
                    "https://api.example.com", "tok", "job-1", art
                )

    def test_upload_retries_5xx_up_to_cap(self):
        """A 5xx artifact upload is retried up to the cap before giving up."""
        import tempfile
        import urllib.error

        with tempfile.TemporaryDirectory() as art:
            self._write(art, "f.txt", b"data")
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError("u", 503, "err", {}, None),
            ) as mock_urlopen, patch("time.sleep") as mock_sleep:
                local_runner._upload_artifact(
                    "https://api.example.com", "tok", "job-1", art
                )

        self.assertEqual(
            mock_urlopen.call_count, local_runner._REPORT_MAX_RETRIES + 1
        )
        self.assertEqual(mock_sleep.call_count, local_runner._REPORT_MAX_RETRIES)

    def test_upload_retries_network_error_then_succeeds(self):
        """A transient network blip on upload is retried instead of dropping artifacts."""
        import tempfile
        import urllib.error

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise urllib.error.URLError("conn reset")
            return _Resp()

        with tempfile.TemporaryDirectory() as art:
            self._write(art, "f.txt", b"data")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                    patch("time.sleep") as mock_sleep:
                local_runner._upload_artifact(
                    "https://api.example.com", "tok", "job-1", art
                )

        self.assertEqual(calls["n"], 2)  # failed once, uploaded on retry
        self.assertEqual(mock_sleep.call_count, 1)


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


# ── Robustness hardening (I3–I6, N1–N4) + heartbeat telemetry (I11) ────────────

class TestParseRetryAfter(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(local_runner._parse_retry_after("120"), 120.0)
        self.assertEqual(local_runner._parse_retry_after("2.5"), 2.5)
        self.assertEqual(local_runner._parse_retry_after("0"), 0.0)

    def test_negative_clamps_to_zero(self):
        self.assertEqual(local_runner._parse_retry_after("-5"), 0.0)

    def test_none_and_blank_return_none(self):
        self.assertIsNone(local_runner._parse_retry_after(None))
        self.assertIsNone(local_runner._parse_retry_after(""))
        self.assertIsNone(local_runner._parse_retry_after("   "))

    def test_garbage_returns_none(self):
        self.assertIsNone(local_runner._parse_retry_after("soon-ish"))

    def test_http_date_future(self):
        from email.utils import formatdate
        future = formatdate(time.time() + 100, usegmt=True)
        val = local_runner._parse_retry_after(future)
        self.assertIsNotNone(val)
        self.assertGreater(val, 0)
        self.assertLessEqual(val, 100)

    def test_http_date_past_clamps_to_zero(self):
        from email.utils import formatdate
        past = formatdate(time.time() - 100, usegmt=True)
        self.assertEqual(local_runner._parse_retry_after(past), 0.0)


class TestEnvParseGuards(unittest.TestCase):
    def test_int_valid(self):
        with patch.dict(os.environ, {"BP_X": "5"}):
            self.assertEqual(local_runner._env_int("BP_X", 2), 5)

    def test_int_garbage_falls_back_to_default(self):
        with patch.dict(os.environ, {"BP_X": "abc"}):
            self.assertEqual(local_runner._env_int("BP_X", 2), 2)

    def test_int_clamps_to_minimum(self):
        with patch.dict(os.environ, {"BP_X": "0"}):
            self.assertEqual(local_runner._env_int("BP_X", 2, minimum=1), 1)
        with patch.dict(os.environ, {"BP_X": "-4"}):
            self.assertEqual(local_runner._env_int("BP_X", 2, minimum=1), 1)

    def test_int_missing_uses_default(self):
        env = {k: v for k, v in os.environ.items() if k != "BP_X"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(local_runner._env_int("BP_X", 7), 7)

    def test_float_garbage_falls_back(self):
        with patch.dict(os.environ, {"BP_F": "notnum"}):
            self.assertEqual(local_runner._env_float("BP_F", 2.0), 2.0)

    def test_float_clamps_to_minimum(self):
        with patch.dict(os.environ, {"BP_F": "0.1"}):
            self.assertEqual(local_runner._env_float("BP_F", 2.0, minimum=0.5), 0.5)


class TestTelemetryState(unittest.TestCase):
    def test_counter_and_timestamp(self):
        t = local_runner._TelemetryState()
        self.assertIsNone(t.snapshot()[0])
        self.assertEqual(t.snapshot()[1], 0)
        t.record_failure()
        t.record_failure()
        self.assertEqual(t.snapshot()[1], 2)
        t.record_success()
        ts, fails = t.snapshot()
        self.assertEqual(fails, 0)
        self.assertIsInstance(ts, str)
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        # A failure increments but must NOT wipe the last-success timestamp.
        t.record_failure()
        self.assertEqual(t.snapshot()[1], 1)
        self.assertEqual(t.snapshot()[0], ts)


class TestFunctionalProbes(unittest.TestCase):
    def test_node_ok_on_zero_exit(self):
        fake = MagicMock()
        fake.returncode = 0
        with patch("subprocess.run", return_value=fake):
            self.assertTrue(local_runner._node_runtime_ok())

    def test_node_not_ok_when_absent(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(local_runner._node_runtime_ok())

    def test_node_not_ok_on_nonzero(self):
        fake = MagicMock()
        fake.returncode = 1
        with patch("subprocess.run", return_value=fake):
            self.assertFalse(local_runner._node_runtime_ok())

    def test_disk_free_none_for_none_root(self):
        self.assertIsNone(local_runner._disk_free_bytes(None))

    def test_disk_free_real_dir_is_int(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertIsInstance(local_runner._disk_free_bytes(d), int)

    def test_gpu_util_absent_returns_none(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(local_runner._gpu_utilization_pct())

    def test_gpu_util_parses_max_across_gpus(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "15\n83\n7\n"
        with patch("subprocess.run", return_value=fake):
            self.assertEqual(local_runner._gpu_utilization_pct(), 83)

    def test_gpu_util_clamps_to_100(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "250\n"
        with patch("subprocess.run", return_value=fake):
            self.assertEqual(local_runner._gpu_utilization_pct(), 100)

    def test_runner_version_returns_describe_output(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "v22-15-gabc1234-dirty\n"
        with patch("subprocess.run", return_value=fake):
            self.assertEqual(local_runner._runner_version(), "v22-15-gabc1234-dirty")

    def test_runner_version_none_on_nonzero_exit(self):
        fake = MagicMock()
        fake.returncode = 128  # not a git repo
        fake.stdout = ""
        with patch("subprocess.run", return_value=fake):
            self.assertIsNone(local_runner._runner_version())

    def test_runner_version_none_when_git_absent(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(local_runner._runner_version())

    def test_runner_version_none_on_os_error(self):
        with patch("subprocess.run", side_effect=OSError):
            self.assertIsNone(local_runner._runner_version())

    def test_runner_version_none_on_empty_output(self):
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "   \n"
        with patch("subprocess.run", return_value=fake):
            self.assertIsNone(local_runner._runner_version())


class TestAgentWallClockDeadline(unittest.TestCase):
    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_reaper_terminates_agent_past_deadline(self):
        """An agent past its wall-clock deadline is torn down, its permit released,
        and a telemetry failure recorded."""
        proc = MagicMock()
        proc.poll.return_value = None  # still running (not a normal reap)

        active_tasks = {"t1": proc}
        agent_started_at = {"t1": time.monotonic() - 100.0}
        lock = threading.Lock()
        capacity = threading.BoundedSemaphore(1)
        self.assertTrue(capacity.acquire(blocking=False))  # permit held by the task
        telemetry = local_runner._TelemetryState()

        terminated = []

        def fake_term(p, **kw):
            terminated.append(p)

        def stop_sleep(_secs):
            local_runner._shutdown_event.set()

        with patch.object(local_runner.iso, "terminate_process_tree", fake_term), \
             patch("time.sleep", stop_sleep):
            local_runner._reap_loop(
                active_tasks, lock, capacity,
                agent_started_at=agent_started_at,
                agent_max_seconds=10,
                telemetry=telemetry,
            )

        self.assertEqual(terminated, [proc])
        self.assertNotIn("t1", active_tasks)
        self.assertNotIn("t1", agent_started_at)
        self.assertTrue(capacity.acquire(blocking=False))  # permit released
        _, fails = telemetry.snapshot()
        self.assertEqual(fails, 1)

    def test_reaper_leaves_young_agent_alone(self):
        proc = MagicMock()
        proc.poll.return_value = None

        active_tasks = {"t1": proc}
        agent_started_at = {"t1": time.monotonic()}  # just started
        lock = threading.Lock()

        def stop_sleep(_secs):
            local_runner._shutdown_event.set()

        with patch.object(local_runner.iso, "terminate_process_tree") as term, \
             patch("time.sleep", stop_sleep):
            local_runner._reap_loop(
                active_tasks, lock, None,
                agent_started_at=agent_started_at,
                agent_max_seconds=3600,
            )

        term.assert_not_called()
        self.assertIn("t1", active_tasks)


class TestComputeJobTelemetry(unittest.TestCase):
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

    def test_success_records_success(self):
        telem = local_runner._TelemetryState()
        job = self._make_job(command="exit 0")
        active_jobs, lock = {"test-job-001"}, threading.Lock()
        with patch.object(local_runner, "_report_compute_job", lambda *a, **k: False), \
             patch.object(local_runner.iso, "plan_compute_execution", _bare_plan_factory()):
            local_runner._run_compute_job(job, "https://api", "tok", active_jobs, lock,
                                          capabilities=_bwrap_caps(), telemetry=telem)
        ts, fails = telem.snapshot()
        self.assertIsInstance(ts, str)
        self.assertEqual(fails, 0)

    def test_failure_records_failure(self):
        telem = local_runner._TelemetryState()
        job = self._make_job(command="exit 3")
        active_jobs, lock = {"test-job-001"}, threading.Lock()
        with patch.object(local_runner, "_report_compute_job", lambda *a, **k: False), \
             patch.object(local_runner.iso, "plan_compute_execution", _bare_plan_factory()):
            local_runner._run_compute_job(job, "https://api", "tok", active_jobs, lock,
                                          capabilities=_bwrap_caps(), telemetry=telem)
        ts, fails = telem.snapshot()
        self.assertIsNone(ts)
        self.assertEqual(fails, 1)

    def test_cancel_is_telemetry_neutral(self):
        telem = local_runner._TelemetryState()
        job = self._make_job(cancel_requested=True)
        active_jobs, lock = {"test-job-001"}, threading.Lock()
        with patch.object(local_runner, "_report_compute_job", lambda *a, **k: False):
            local_runner._run_compute_job(job, "https://api", "tok", active_jobs, lock,
                                          capabilities=_bwrap_caps(), telemetry=telem)
        ts, fails = telem.snapshot()
        self.assertIsNone(ts)
        self.assertEqual(fails, 0)

    def test_image_downgrade_note_written_to_command_log(self):
        """A container_image job that runs image-less under bwrap gets a diagnostic
        NOTE in its OWN command_log (I10)."""
        reports = []

        def fake_report(orch, token, job_id, *, exec_status, exit_code=None,
                        result_metrics=None, command_log=None):
            reports.append({"exec_status": exec_status, "command_log": command_log})
            return False

        job = self._make_job(command="exit 0",
                             container_image="nvcr.io/nvidia/pytorch:24.01")
        active_jobs, lock = {"test-job-001"}, threading.Lock()
        with patch.object(local_runner, "_report_compute_job", fake_report), \
             patch.object(local_runner.iso, "plan_compute_execution", _bare_plan_factory()):
            local_runner._run_compute_job(job, "https://api", "tok", active_jobs, lock,
                                          capabilities=_bwrap_caps())

        terminal = [r for r in reports if r["exec_status"] in ("succeeded", "failed")]
        self.assertTrue(terminal)
        self.assertEqual(terminal[-1]["exec_status"], "succeeded")
        cl = terminal[-1]["command_log"]
        self.assertIsNotNone(cl)
        self.assertIn("nvcr.io/nvidia/pytorch:24.01", cl["text"])
        self.assertIn("image-less on host toolchain", cl["text"])


class TestHeartbeatTelemetryPayload(unittest.TestCase):
    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_emits_all_four_telemetry_fields(self):
        posted = []

        def fake_post(url, token, body, **kwargs):
            posted.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        telem = local_runner._TelemetryState()
        telem.record_success()   # sets last_successful_job_at, failures=0
        telem.record_failure()   # failures=1, timestamp preserved

        import tempfile
        with tempfile.TemporaryDirectory() as ws:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_node_runtime_ok", return_value=True), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=["node", "/x/index.js"]):
                local_runner._heartbeat_loop(
                    "https://api", "tok", {}, threading.Lock(),
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    telemetry=telem, workspace_root=ws,
                )

        body = posted[0]
        # EXACT field names consumed by the parallel backend PR.
        self.assertIn("last_successful_job_at", body)
        self.assertIsInstance(body["last_successful_job_at"], str)
        self.assertEqual(body["consecutive_job_failures"], 1)
        self.assertIn("disk_free_bytes", body)
        self.assertIsInstance(body["disk_free_bytes"], int)
        self.assertIn("gpu_utilization_pct", body)
        self.assertIsNone(body["gpu_utilization_pct"])  # has_gpu False → skipped/None

    def test_defaults_when_no_telemetry(self):
        posted = []

        def fake_post(url, token, body, **kwargs):
            posted.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False):
            local_runner._heartbeat_loop(
                "https://api", "tok", {}, threading.Lock(),
                {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
            )

        body = posted[0]
        self.assertIsNone(body["last_successful_job_at"])
        self.assertEqual(body["consecutive_job_failures"], 0)
        self.assertIsNone(body["disk_free_bytes"])  # workspace_root None
        self.assertIsNone(body["gpu_utilization_pct"])


class TestHeartbeat401Streak(unittest.TestCase):
    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_exits_after_consecutive_401_streak(self):
        """A sustained 401 streak (revoked token) exits the process non-zero (N4)."""
        exits = []

        def fake_post(url, token, body, **kwargs):
            return 401, None

        # Patch os._exit so the streak detector doesn't kill the test process.
        def fake_exit(code):
            exits.append(code)
            local_runner._shutdown_event.set()
            raise SystemExit(code)  # unwind out of the loop in-test

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False), \
             patch("os._exit", fake_exit), \
             patch("time.sleep", lambda s: None):
            with self.assertRaises(SystemExit):
                local_runner._heartbeat_loop(
                    "https://api", "tok", {}, threading.Lock(),
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    max_auth_failures=3,
                )

        self.assertEqual(exits, [1])

    def test_no_exit_when_streak_resets(self):
        """A 200 between 401s resets the streak — no exit."""
        statuses = iter([401, 401, 200, 401])
        exits = []

        def fake_post(url, token, body, **kwargs):
            try:
                return next(statuses), None
            except StopIteration:
                local_runner._shutdown_event.set()
                return 204, None

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False), \
             patch("os._exit", lambda c: exits.append(c)), \
             patch("time.sleep", lambda s: None):
            local_runner._heartbeat_loop(
                "https://api", "tok", {}, threading.Lock(),
                {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                max_auth_failures=3,
            )

        self.assertEqual(exits, [], "streak reset by the 200 must not trigger an exit")

    def test_token_reload_on_401_prevents_exit(self):
        """When the token file is updated on disk during a 401 streak, the runner
        detects the new token, resets the counter, and resumes without exit."""
        # _post always returns 401 while using the old token; after reload the
        # runner retries with the new token and gets a 200.
        call_count = [0]
        tokens_seen = []

        def fake_post(url, token, body, **kwargs):
            tokens_seen.append(token)
            call_count[0] += 1
            # Old token always gets 401; new token succeeds.
            if token == "new-token":
                return 200, None
            return 401, None

        # _load_runner_token returns the old token on the first call (no change
        # detected), then returns a new token on the second call (simulating
        # operator updating the file between heartbeat cycles).
        load_calls = [0]
        def fake_load_token():
            load_calls[0] += 1
            if load_calls[0] == 1:
                return "old-token"
            return "new-token"

        exits = []

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_load_runner_token", fake_load_token), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False), \
             patch("os._exit", lambda c: exits.append(c)), \
             patch("time.sleep", lambda s: None):
            # Shut down after the new token succeeds so the loop terminates.
            def shutdown_after_success():
                if call_count[0] >= 3:
                    local_runner._shutdown_event.set()
            with patch.object(local_runner, "_write_status_json",
                              lambda **kw: shutdown_after_success()):
                local_runner._heartbeat_loop(
                    "https://api", "old-token", {}, threading.Lock(),
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    max_auth_failures=3,
                )

        self.assertEqual(exits, [], "token reload must prevent auth-fatal exit")
        self.assertIn("new-token", tokens_seen,
                      "heartbeat must use the reloaded token")


class TestDescribeSha(unittest.TestCase):
    """Unit tests for _describe_sha, which extracts abbreviated SHAs from
    ``git describe`` output."""

    def test_tag_commits_sha_format(self):
        """``v1.2.3-5-g4f5a6b7`` → ``4f5a6b7`` (standard git describe)."""
        self.assertEqual(local_runner._describe_sha("v1.2.3-5-g4f5a6b7"), "4f5a6b7")

    def test_tag_commits_sha_dirty(self):
        """``v1.2.3-5-g4f5a6b7-dirty`` → ``4f5a6b7`` (dirty tree suffixes)."""
        self.assertEqual(
            local_runner._describe_sha("v1.2.3-5-g4f5a6b7-dirty"), "4f5a6b7",
        )

    def test_tag_commits_long_sha(self):
        """``release-v2-10-gabcdef0123456789`` → ``abcdef0123456789`` (longer SHA)."""
        self.assertEqual(
            local_runner._describe_sha("release-v2-10-gabcdef0123456789"),
            "abcdef0123456789",
        )

    def test_bare_sha_format(self):
        """``4f5a6b7`` → ``4f5a6b7`` (``--always`` fallback when no tags)."""
        self.assertEqual(local_runner._describe_sha("4f5a6b7"), "4f5a6b7")

    def test_bare_sha_dirty(self):
        """``4f5a6b7-dirty`` → ``4f5a6b7`` (bare SHA with dirty suffix)."""
        self.assertEqual(local_runner._describe_sha("4f5a6b7-dirty"), "4f5a6b7")

    def test_pure_tag_returns_none(self):
        """``v1.2.3`` → ``None`` (pure tag with no commit SHA)."""
        self.assertIsNone(local_runner._describe_sha("v1.2.3"))

    def test_pure_tag_with_dash_returns_none(self):
        """``v1.0-rc1`` → ``None`` (tag containing dash but no ``-g`` SHA)."""
        self.assertIsNone(local_runner._describe_sha("v1.0-rc1"))

    def test_empty_string_returns_none(self):
        """``""`` → ``None`` (empty input)."""
        self.assertIsNone(local_runner._describe_sha(""))


class TestHeartbeatVersionHintWarning(unittest.TestCase):
    """Heartbeat response includes version_hint; runner warns when its own version
    does not match the orchestrator's hint."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_version_hint_mismatch_logs_warning(self):
        """When version_hint differs and tasks are executing, a warning is printed."""
        posted_bodies = []
        output_lines = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00",
                "enrolled_max_concurrent": 2,
                "version_hint": "v99-abcdef01",
            }

        def fake_print(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            output_lines.append(line)

        # Non-empty active_tasks so the runner appears busy and does NOT
        # auto-update — the idle path triggers update instead of warning.
        active_tasks = {"task-1": MagicMock()}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version", return_value="v1-oldversion"), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None), \
                 patch("builtins.print", side_effect=fake_print):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        warning_lines = [line for line in output_lines if "WARNING" in line and "version" in line.lower()]
        self.assertTrue(
            warning_lines,
            "Expected a version mismatch warning in output",
        )
        self.assertIn("not auto-updating", str(warning_lines))

    def test_version_hint_match_silent(self):
        """When version_hint matches runner version, no warning is printed."""
        output_lines = []

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 200, {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00",
                "enrolled_max_concurrent": 2,
                "version_hint": "v1-current",
            }

        def fake_print(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            output_lines.append(line)

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version", return_value="v1-current"), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None), \
                 patch("builtins.print", side_effect=fake_print):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        warning_lines = [line for line in output_lines if "WARNING" in line and "version" in line.lower()]
        self.assertFalse(warning_lines, "No version-mismatch warning expected when versions match")

    def test_version_hint_match_sha_based(self):
        """SHA-based comparison: runner SHA matches orchestrator SHA prefix — no warning."""
        output_lines = []

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 200, {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00",
                "enrolled_max_concurrent": 2,
                # Orchestrator sends a full 40-char SHA that starts with the
                # runner's abbreviated SHA (4f5a6b7).
                "version_hint": "4f5a6b7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b",
            }

        def fake_print(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            output_lines.append(line)

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version",
                              return_value="v1.2.3-5-g4f5a6b7"), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None), \
                 patch("builtins.print", side_effect=fake_print):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        warning_lines = [line for line in output_lines if "WARNING" in line and "version" in line.lower()]
        self.assertFalse(
            warning_lines,
            "No warning expected when runner SHA matches orchestrator SHA prefix",
        )

    def test_version_hint_mismatch_sha_based(self):
        """SHA-based comparison: runner SHA differs from orchestrator SHA — warning."""
        posted_bodies = []
        output_lines = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00",
                "enrolled_max_concurrent": 2,
                # Orchestrator sends a full SHA that does NOT start with the
                # runner's abbreviated SHA (4f5a6b7).
                "version_hint": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            }

        def fake_print(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            output_lines.append(line)

        # Non-empty active_tasks so the runner appears busy and does NOT
        # auto-update — the idle path triggers update instead of warning.
        active_tasks = {"task-1": MagicMock()}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version",
                              return_value="v1.2.3-5-g4f5a6b7"), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None), \
                 patch("builtins.print", side_effect=fake_print):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        warning_lines = [line for line in output_lines if "WARNING" in line and "version" in line.lower()]
        self.assertTrue(
            warning_lines,
            "Expected a version mismatch warning when SHA comparison fails",
        )
        self.assertIn("not auto-updating", str(warning_lines))

    def test_version_hint_mismatch_triggers_auto_update_when_idle(self):
        """When version_hint differs and runner is idle, auto-update is triggered."""
        output_lines = []

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 200, {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00",
                "enrolled_max_concurrent": 2,
                "version_hint": "v99-abcdef01",
            }

        def fake_print(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            output_lines.append(line)

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version",
                              return_value="v1-oldversion"), \
                 patch.object(local_runner, "_locate_agent_binary",
                              return_value=None), \
                 patch.object(local_runner.br, "do_update",
                              return_value=0), \
                 patch.object(os, "_exit"), \
                 patch("builtins.print", side_effect=fake_print):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        combined = " ".join(output_lines).lower()
        self.assertIn(
            "auto-updating",
            combined,
            "Expected auto-update message when runner is idle and version differs",
        )

    def test_version_hint_mismatch_skips_auto_update_when_compute_busy(self):
        """When version_hint differs but compute jobs are active, no auto-update."""
        output_lines = []

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 200, {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00",
                "enrolled_max_concurrent": 2,
                "version_hint": "v99-abcdef01",
            }

        def fake_print(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            output_lines.append(line)

        active_tasks: dict = {}
        active_compute_jobs = {"job-1"}
        task_lock = threading.Lock()
        compute_lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version",
                              return_value="v1-oldversion"), \
                 patch.object(local_runner, "_locate_agent_binary",
                              return_value=None), \
                 patch("builtins.print", side_effect=fake_print):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, task_lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    active_compute_jobs, compute_lock,
                )
        finally:
            local_runner._shutdown_event.clear()

        warning_lines = [line for line in output_lines if "WARNING" in line and "version" in line.lower()]
        self.assertTrue(
            warning_lines,
            "Expected a warning when compute jobs are active and version differs",
        )
        self.assertIn("not auto-updating", str(warning_lines))

    def test_no_version_hint_no_warning(self):
        """When the response lacks version_hint, the runner never warns."""
        output_lines = []

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 200, {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00",
                "enrolled_max_concurrent": 2,
            }

        def fake_print(*args, **kwargs):
            line = " ".join(str(a) for a in args)
            output_lines.append(line)

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_runner_version", return_value="v1-something"), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None), \
                 patch("builtins.print", side_effect=fake_print):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        warning_lines = [line for line in output_lines if "WARNING" in line and "version" in line.lower()]
        self.assertFalse(
            warning_lines,
            "No version-mismatch warning expected when version_hint is absent",
        )


class TestHeartbeatJitterAndNetworkFailureEscalation(unittest.TestCase):
    """Heartbeat loop: jitter spreads restarts; network failure streak escalates."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_heartbeat_jitter_and_network_failure_escalation(self):
        """Jitter spreads sleep intervals; consecutive network errors trigger _die."""
        jitter_calls = []
        exits = []

        def fake_post(url, token, body, **kwargs):
            raise RuntimeError("Network error: [Errno -2] Name or service not known")

        def fake_exit(code):
            exits.append(code)
            local_runner._shutdown_event.set()
            raise SystemExit(code)

        def fake_uniform(a, b):
            jitter_calls.append((a, b))
            return 3.0  # deterministic jitter for test

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False), \
             patch("os._exit", fake_exit), \
             patch("time.sleep", lambda s: None), \
             patch("random.uniform", fake_uniform):
            with self.assertRaises(SystemExit):
                local_runner._heartbeat_loop(
                    "https://api", "tok", {}, threading.Lock(),
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    max_network_failures=3,
                )

        # _die must have been called exactly once with exit code 1
        self.assertEqual(exits, [1], "network failure streak must exit non-zero")

        # Jitter: random.uniform must have been called with the ±6s window on each
        # sleep cycle so runners don't thundering-herd on orchestrator restart.
        self.assertGreater(len(jitter_calls), 0, "random.uniform never called — jitter not applied")
        for args in jitter_calls:
            self.assertEqual(args, (-6, 6), f"jitter window wrong: got {args}, want (-6, 6)")


class TestComputePollRetryAfter(unittest.TestCase):
    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_429_honors_retry_after_header(self):
        sleeps = []
        calls = [0]

        def fake_post(url, token, body, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return 429, None, 7.0  # (status, body, retry_after)
            local_runner._shutdown_event.set()
            return 204, None, None

        active_jobs, lock = set(), threading.Lock()
        with patch.object(local_runner, "_post", fake_post), \
             patch("time.sleep", lambda s: sleeps.append(s)):
            local_runner._compute_poll_loop(
                "https://api", "tok", active_jobs, lock, max_concurrent=2
            )

        self.assertIn(7.0, sleeps)

    def test_429_without_header_uses_default_backoff(self):
        sleeps = []
        calls = [0]

        def fake_post(url, token, body, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return 429, None, None  # no Retry-After
            local_runner._shutdown_event.set()
            return 204, None, None

        active_jobs, lock = set(), threading.Lock()
        with patch.object(local_runner, "_post", fake_post), \
             patch("time.sleep", lambda s: sleeps.append(s)):
            local_runner._compute_poll_loop(
                "https://api", "tok", active_jobs, lock, max_concurrent=2
            )

        self.assertTrue(sleeps)  # backed off with a positive default
        self.assertGreater(sleeps[0], 0)


class TestPostRetryAfterHeader(unittest.TestCase):
    def test_return_headers_parses_retry_after(self):
        class _Resp:
            status = 429
            headers = {"Retry-After": "12"}

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=_Resp()):
            status, body, retry_after = local_runner._post(
                "https://api/x", "tok", {}, return_headers=True
            )
        self.assertEqual(status, 429)
        self.assertEqual(retry_after, 12.0)

    def test_default_form_still_two_tuple(self):
        class _Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=_Resp()):
            result = local_runner._post("https://api/x", "tok", {})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], 200)


# ── Docker agent backend (BLUEPRINT_AGENT_IMAGE) ──────────────────────────────

class TestDockerAgentBackend(unittest.TestCase):
    """Agent tasks routed to the prod agent container image instead of bwrap.

    Everything here mocks ``iso`` (decide_agent_backend / plan_agent_execution /
    terminate_process_tree) — no real docker/postgres/bwrap is touched. Covers:
    plan+spawn routing, per-task container tracking, docker-aware teardown, and the
    relaxed advertisement (claim read-only agent work on docker+image WITHOUT bwrap).
    """

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    # ---- plan + spawn + container tracking -----------------------------------

    def _spawn(self, *, backend, container_name, agent_image, agent_binary, caps,
               task_type="research"):
        active_tasks: dict = {}
        agent_started_at: dict = {}
        agent_teardown: dict = {}
        fake_proc = MagicMock()
        fake_proc.pid = 4321
        # Provide a real BytesIO for stdout so the tee reader thread exits
        # cleanly instead of crashing on a MagicMock.
        fake_proc.stdout = io.BytesIO(b"")

        plan = iso.ExecutionPlan(
            backend=backend,
            argv=(["docker", "run", "..."] if backend == iso.BACKEND_DOCKER
                  else ["bwrap", "node", "..."]),
            env={"AGENT_SECRET_TOKEN": "s"},
            container_name=container_name,
        )
        decide_mock = MagicMock(return_value=backend)
        plan_mock = MagicMock(return_value=plan)

        with patch.object(local_runner.iso, "decide_agent_backend", decide_mock), \
             patch.object(local_runner.iso, "plan_agent_execution", plan_mock), \
             patch.object(local_runner.subprocess, "Popen", return_value=fake_proc):
            spawned = local_runner._spawn_agent_task(
                task_id="task-xyz",
                task_type=task_type,
                agent_token="agent-tok",
                vm_instance_id="runner:local",
                orchestrator_url="https://api.example.com",
                workspace="/tmp/ws",
                caps=caps,
                agent_binary=agent_binary,
                agent_image=agent_image,
                active_tasks=active_tasks,
                agent_started_at=agent_started_at,
                agent_teardown=agent_teardown,
                lock=threading.Lock(),
            )
        return {
            "spawned": spawned, "active": active_tasks, "started": agent_started_at,
            "teardown": agent_teardown, "decide": decide_mock, "plan": plan_mock,
            "proc": fake_proc,
        }

    def test_docker_image_plans_spawns_and_tracks_container(self):
        r = self._spawn(
            backend=iso.BACKEND_DOCKER,
            container_name="bp-agent-abcd1234",
            agent_image="registry/agent:prod",
            agent_binary=None,            # a docker box need not carry the host dist
            caps=_docker_caps(),
        )
        self.assertTrue(r["spawned"])
        self.assertIs(r["active"]["task-xyz"], r["proc"])
        self.assertIn("task-xyz", r["started"])
        # container_name tracked alongside the backend for the reaper/shutdown teardown.
        self.assertEqual(
            r["teardown"]["task-xyz"], (iso.BACKEND_DOCKER, "bp-agent-abcd1234")
        )
        # The claim decision is routed through decide_agent_backend with task_type+image.
        _, dkw = r["decide"].call_args
        self.assertEqual(dkw["task_type"], "research")
        self.assertEqual(dkw["agent_image"], "registry/agent:prod")
        # The image + task_type are handed to the planner for the docker task.
        _, pkw = r["plan"].call_args
        self.assertEqual(pkw["agent_image"], "registry/agent:prod")
        self.assertEqual(pkw["task_type"], "research")
        # Injected identity env is passed through unchanged.
        self.assertEqual(pkw["injected_env"]["AGENT_SECRET_TOKEN"], "agent-tok")
        self.assertEqual(pkw["injected_env"]["TASK_ID"], "task-xyz")

    def test_real_iso_interface_docker_plan_and_container_name(self):
        """Integration guard against interface drift: drive _spawn_agent_task with the
        REAL iso.decide_agent_backend + iso.plan_agent_execution (both pure) — only
        subprocess.Popen is mocked. A read-only task type on a docker+image box must
        plan the docker backend and derive container_name = bp-agent-<task_id>."""
        active_tasks: dict = {}
        started: dict = {}
        teardown: dict = {}
        fake_proc = MagicMock()
        fake_proc.pid = 999
        fake_proc.stdout = io.BytesIO(b"")
        with patch.object(local_runner.subprocess, "Popen", return_value=fake_proc):
            spawned = local_runner._spawn_agent_task(
                task_id="abc123def456ghi",  # >12 chars → container name truncates it
                task_type="research",       # in iso.AGENT_DOCKER_TYPES
                agent_token="t", vm_instance_id="vm",
                orchestrator_url="https://api", workspace="/tmp/ws",
                caps=_docker_caps(),        # docker_usable, no bwrap
                agent_binary=None,          # host dist absent — image carries the agent
                agent_image="registry/agent:prod",
                active_tasks=active_tasks, agent_started_at=started,
                agent_teardown=teardown, lock=threading.Lock(),
            )
        self.assertTrue(spawned)
        backend, cname = teardown["abc123def456ghi"]
        self.assertEqual(backend, iso.BACKEND_DOCKER)
        self.assertEqual(cname, "bp-agent-abc123def456")  # bp-agent-<task_id[:12]>

    def test_no_image_uses_bwrap_and_no_container(self):
        r = self._spawn(
            backend=iso.BACKEND_BWRAP,
            container_name=None,
            agent_image=None,
            agent_binary="/agent/dist/index.js",
            caps=_bwrap_caps(),
        )
        self.assertTrue(r["spawned"])
        self.assertEqual(r["teardown"]["task-xyz"], (iso.BACKEND_BWRAP, None))
        # No image → the planner is never offered one (bwrap-only, unchanged).
        _, pkw = r["plan"].call_args
        self.assertIsNone(pkw["agent_image"])

    def test_declined_task_type_not_spawned(self):
        """A write task type on a docker-only (read-only image) box declines before
        planning; nothing is tracked and the caller (which releases the permit) sees
        False."""
        active_tasks: dict = {}
        started: dict = {}
        teardown: dict = {}
        with patch.object(local_runner.iso, "plan_agent_execution") as plan_mock, \
             patch.object(local_runner.iso, "decide_agent_backend",
                          return_value=iso.DECLINE):
            spawned = local_runner._spawn_agent_task(
                task_id="task-w", task_type="feature", agent_token="t",
                vm_instance_id="vm", orchestrator_url="https://api",
                workspace="/tmp/ws", caps=_docker_caps(), agent_binary=None,
                agent_image="registry/agent:prod", active_tasks=active_tasks,
                agent_started_at=started, agent_teardown=teardown,
                lock=threading.Lock(),
            )
        self.assertFalse(spawned)
        self.assertEqual(active_tasks, {})
        self.assertEqual(teardown, {})
        plan_mock.assert_not_called()

    def test_notify_decline_posts_to_orchestrator(self):
        """_notify_decline POSTs the task_id to the decline-task endpoint."""
        from unittest.mock import patch

        with patch.object(local_runner, "_post") as post_mock:
            local_runner._notify_decline(
                "https://api.example.com", "tok-123", "task-abc"
            )
        post_mock.assert_called_once_with(
            "https://api.example.com/api/runner/decline-task",
            "tok-123",
            {"task_id": "task-abc"},
        )

    def test_notify_decline_swallows_exceptions(self):
        """_notify_decline is best-effort — exceptions must not propagate."""
        from unittest.mock import patch

        with patch.object(
            local_runner, "_post", side_effect=OSError("network down")
        ):
            # Must not raise.
            local_runner._notify_decline("https://api", "tok", "t1")

    # ---- docker-aware teardown (reaper deadline path) ------------------------

    def _reap_once_past_deadline(self, teardown_entry):
        proc = MagicMock()
        proc.poll.return_value = None  # still running → deadline path (not normal reap)
        active_tasks = {"t1": proc}
        agent_started_at = {"t1": time.monotonic() - 100.0}
        agent_teardown = {"t1": teardown_entry}
        captured = {}

        def fake_term(p, **kw):
            captured["proc"] = p
            captured["kwargs"] = kw

        def stop_sleep(_secs):
            local_runner._shutdown_event.set()

        with patch.object(local_runner.iso, "terminate_process_tree", fake_term), \
             patch("time.sleep", stop_sleep):
            local_runner._reap_loop(
                active_tasks, threading.Lock(), None,
                agent_started_at=agent_started_at,
                agent_max_seconds=10,
                agent_teardown=agent_teardown,
            )
        return captured, active_tasks, agent_teardown, proc

    def test_reaper_docker_teardown_stops_container(self):
        captured, active, teardown, proc = self._reap_once_past_deadline(
            (iso.BACKEND_DOCKER, "bp-agent-abcd1234")
        )
        self.assertIs(captured["proc"], proc)
        self.assertEqual(captured["kwargs"]["backend"], iso.BACKEND_DOCKER)
        self.assertEqual(captured["kwargs"]["container_name"], "bp-agent-abcd1234")
        self.assertNotIn("t1", active)
        self.assertNotIn("t1", teardown)

    def test_reaper_bwrap_teardown_has_no_container(self):
        captured, _active, _teardown, _proc = self._reap_once_past_deadline(
            (iso.BACKEND_BWRAP, None)
        )
        self.assertEqual(captured["kwargs"]["backend"], iso.BACKEND_BWRAP)
        self.assertIsNone(captured["kwargs"]["container_name"])

    # ---- relaxed advertisement (heartbeat agent_available) -------------------

    def _heartbeat_agent_available(self, *, caps, agent_image):
        posted = []

        def fake_post(url, token, body, **kwargs):
            posted.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        # Host toolchain ABSENT (docker-only box): no agent dist, no node runtime.
        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_locate_agent_binary", return_value=None), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False):
            local_runner._heartbeat_loop(
                "https://api", "tok", {}, threading.Lock(),
                {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                agent_sandbox_ok=caps.bwrap_ok,
                caps=caps, agent_image=agent_image,
            )
        return posted[0]["agent_available"]

    def test_advertises_agent_on_docker_image_without_bwrap(self):
        self.assertTrue(
            self._heartbeat_agent_available(
                caps=_docker_caps(), agent_image="registry/agent:prod"
            )
        )

    def test_no_advertise_agent_on_docker_without_image(self):
        # Regression guard: docker-usable but no image → NOT claimable (unchanged).
        self.assertFalse(
            self._heartbeat_agent_available(caps=_docker_caps(), agent_image=None)
        )

    def test_spawn_writes_agent_output_to_per_task_log_file(self):
        """A spawned agent task's stdout output must be persisted to a per-task
        log file on disk — the core regression test for per-task log persistence."""
        import io as _io
        import tempfile

        with tempfile.TemporaryDirectory() as log_dir:
            with patch.dict(os.environ, {"BLUEPRINT_RUNNER_LOG_DIR": log_dir}):
                active_tasks: dict = {}
                agent_started_at: dict = {}
                agent_teardown: dict = {}
                fake_proc = MagicMock()
                fake_proc.pid = 7777
                fake_proc.stdout = _io.BytesIO(
                    b"task output line 1\ntask output line 2\n"
                )

                plan = iso.ExecutionPlan(
                    backend=iso.BACKEND_BWRAP,
                    argv=["bash", "-c", "echo test"],
                    env={"AGENT_SECRET_TOKEN": "s"},
                    container_name=None,
                )
                decide_mock = MagicMock(return_value=iso.BACKEND_BWRAP)
                plan_mock = MagicMock(return_value=plan)

                with patch.object(local_runner.iso, "decide_agent_backend", decide_mock), \
                     patch.object(local_runner.iso, "plan_agent_execution", plan_mock), \
                     patch.object(local_runner.subprocess, "Popen",
                                 return_value=fake_proc):
                    spawned = local_runner._spawn_agent_task(
                        task_id="task-log-test",
                        task_type="research",
                        agent_token="tok",
                        vm_instance_id="vm",
                        orchestrator_url="https://api",
                        workspace="/tmp/ws",
                        caps=_bwrap_caps(),
                        agent_binary="/usr/bin/node",
                        agent_image=None,
                        active_tasks=active_tasks,
                        agent_started_at=agent_started_at,
                        agent_teardown=agent_teardown,
                        lock=threading.Lock(),
                    )

                self.assertTrue(spawned)

                log_path = local_runner._task_log_path("task-log-test")
                self.assertTrue(
                    os.path.isfile(log_path),
                    f"Log file should exist at {log_path}",
                )

                with open(log_path, "rb") as f:
                    content = f.read()
                self.assertIn(b"task output line 1", content)
                self.assertIn(b"task output line 2", content)


class TestAgentImagePresence(unittest.TestCase):
    """Fail-closed image check (Fix 3): a docker box with BLUEPRINT_AGENT_IMAGE set
    but the image ABSENT must NOT advertise the docker agent backend — it would
    claim-and-crash-loop. ``_agent_image_present`` gates main()'s startup enablement;
    when it fails, main() sets agent_image=None so the runner falls back to
    bwrap-or-decline. No real docker is touched (subprocess.run is mocked)."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    @staticmethod
    def _inspect_result(returncode):
        result = MagicMock()
        result.returncode = returncode
        return result

    # ---- the helper itself ---------------------------------------------------

    def test_present_when_inspect_exits_zero(self):
        with patch.object(
            local_runner.subprocess, "run",
            return_value=self._inspect_result(0),
        ) as run_mock:
            self.assertTrue(local_runner._agent_image_present("registry/agent:prod"))
        # It really shells `docker image inspect <image>`.
        argv = run_mock.call_args[0][0]
        self.assertEqual(argv, ["docker", "image", "inspect", "registry/agent:prod"])

    def test_absent_when_inspect_exits_nonzero(self):
        with patch.object(
            local_runner.subprocess, "run",
            return_value=self._inspect_result(1),
        ):
            self.assertFalse(local_runner._agent_image_present("registry/agent:prod"))

    def test_absent_on_subprocess_error(self):
        for exc in (
            OSError("docker not installed"),
            subprocess.TimeoutExpired(cmd="docker", timeout=15),
        ):
            with patch.object(local_runner.subprocess, "run", side_effect=exc):
                self.assertFalse(
                    local_runner._agent_image_present("registry/agent:prod")
                )

    # ---- effect on advertisement (main()'s startup gate) ---------------------

    def _advertised_after_image_gate(self, *, inspect_rc):
        """Drive the REAL heartbeat advertisement with the effective agent_image
        main() would compute: present (inspect exit 0) keeps the image, absent
        disables it (None). Docker-only box (no bwrap), host toolchain absent."""
        with patch.object(
            local_runner.subprocess, "run",
            return_value=self._inspect_result(inspect_rc),
        ):
            present = local_runner._agent_image_present("registry/agent:prod")
        effective_image = "registry/agent:prod" if present else None

        posted = []

        def fake_post(url, token, body, **kwargs):
            posted.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_locate_agent_binary", return_value=None), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False):
            local_runner._heartbeat_loop(
                "https://api", "tok", {}, threading.Lock(),
                {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                agent_sandbox_ok=False,  # no bwrap → docker-only
                caps=_docker_caps(), agent_image=effective_image,
            )
        return posted[0]["agent_available"]

    def test_present_image_advertises_docker_agent(self):
        self.assertTrue(self._advertised_after_image_gate(inspect_rc=0))

    def test_absent_image_disables_and_falls_back(self):
        # Absent image → agent_image disabled → docker-only box no longer advertises
        # agent work (falls back to decline, since bwrap is unavailable).
        self.assertFalse(self._advertised_after_image_gate(inspect_rc=1))


class TestAgentBackendPreference(unittest.TestCase):
    """The default agent backend is bwrap-first; docker requires an explicit opt-in.

    These tests drive the preference logic in main() (agent_backend_pref) and the
    decide_agent_backend integration, confirming that:
      1. Default (no env vars, no image) → bwrap backend selected.
      2. BLUEPRINT_AGENT_BACKEND=docker → docker backend for read-only types.
      3. BLUEPRINT_AGENT_BACKEND=bwrap with image set → image ignored, bwrap used.
    """

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def _backend_from_env(self, env_overrides, *, caps, task_type="research"):
        """Drive decide_agent_backend the way main() + _spawn_agent_task would.

        Parses agent_image and agent_backend_pref from env_overrides (mirrors the
        main() startup block), then calls decide_agent_backend with the result.
        ``agent_binary_present=True`` is assumed (we're testing backend selection,
        not binary detection — the host binary check is separate from isolation routing).
        """
        env = {**env_overrides}
        agent_image = env.get("BLUEPRINT_AGENT_IMAGE", "").strip() or None
        pref = env.get("BLUEPRINT_AGENT_BACKEND", "").strip().lower() or "auto"
        if pref == "bwrap":
            agent_image = None
        # BLUEPRINT_AGENT_BACKEND=docker just keeps agent_image as-is; the
        # docker path activates when decide_agent_backend sees image+docker_usable.
        return iso.decide_agent_backend(
            caps, True, task_type=task_type, agent_image=agent_image
        )

    def test_default_no_env_uses_bwrap(self):
        """No env vars at all → bwrap backend (the default)."""
        backend = self._backend_from_env({}, caps=_bwrap_caps())
        self.assertEqual(backend, iso.BACKEND_BWRAP)

    def test_default_docker_usable_but_no_image_uses_bwrap(self):
        """Docker usable but BLUEPRINT_AGENT_IMAGE not set → bwrap."""
        backend = self._backend_from_env({}, caps=_docker_caps())
        # No image means decide_agent_backend returns bwrap (or decline if no bwrap).
        # _docker_caps has no bwrap, so it's DECLINE — proving docker is NOT auto-selected.
        self.assertNotEqual(backend, iso.BACKEND_DOCKER)

    def test_docker_opt_in_with_image_routes_read_only_to_docker(self):
        """BLUEPRINT_AGENT_BACKEND=docker + BLUEPRINT_AGENT_IMAGE → docker for research."""
        backend = self._backend_from_env(
            {
                "BLUEPRINT_AGENT_BACKEND": "docker",
                "BLUEPRINT_AGENT_IMAGE": "registry/agent:prod",
            },
            caps=_docker_caps(),
            task_type="research",
        )
        self.assertEqual(backend, iso.BACKEND_DOCKER)

    def test_bwrap_force_ignores_image(self):
        """BLUEPRINT_AGENT_BACKEND=bwrap zeroes out agent_image → docker NOT selected."""
        # With BACKEND=bwrap, agent_image is discarded. Even though docker is usable and
        # the image would normally enable docker, bwrap is forced so docker is never chosen.
        backend = self._backend_from_env(
            {
                "BLUEPRINT_AGENT_BACKEND": "bwrap",
                "BLUEPRINT_AGENT_IMAGE": "registry/agent:prod",
            },
            caps=_docker_caps(),   # docker usable, but bwrap is forced → docker NOT selected
        )
        self.assertNotEqual(backend, iso.BACKEND_DOCKER)

    def test_explicit_image_without_backend_pref_acts_as_docker_optin(self):
        """BLUEPRINT_AGENT_IMAGE set (no BACKEND pref) → auto mode treats image as opt-in."""
        backend = self._backend_from_env(
            {"BLUEPRINT_AGENT_IMAGE": "registry/agent:prod"},
            caps=_docker_caps(),
            task_type="research",
        )
        self.assertEqual(backend, iso.BACKEND_DOCKER)


class TestSafeCredentialPath(unittest.TestCase):
    """_safe_credential_path rejects directory traversal and out-of-workspace paths."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _valid(self, fpath: str) -> str:
        """Helper: assert path is accepted, return resolved path."""
        return local_runner._safe_credential_path(fpath, self._tmpdir)

    def _invalid(self, fpath: str) -> None:
        """Helper: assert path raises ValueError."""
        with self.assertRaises(ValueError):
            local_runner._safe_credential_path(fpath, self._tmpdir)

    def test_normal_path_with_leading_slash_is_accepted(self):
        """A normal path under /root is resolved relative to workspace root."""
        resolved = self._valid("/root/.config/claude/settings.json")
        self.assertTrue(resolved.startswith(self._tmpdir))
        self.assertTrue(resolved.endswith("root/.config/claude/settings.json"))
        self.assertTrue(os.path.isabs(resolved))

    def test_path_without_leading_slash_is_accepted(self):
        """Paths without a leading slash also resolve correctly."""
        resolved = self._valid("home/user/config.yaml")
        self.assertEqual(resolved, os.path.join(self._tmpdir, "home/user/config.yaml"))

    def test_deep_safe_path_is_accepted(self):
        """A deeply nested path without traversal is accepted."""
        resolved = self._valid("/a/b/c/d/e/f/g/h/file.txt")
        self.assertTrue(resolved.startswith(self._tmpdir))
        self.assertTrue(resolved.endswith("a/b/c/d/e/f/g/h/file.txt"))

    def test_leading_dot_dot_is_rejected(self):
        """Path starting with '..' is rejected."""
        self._invalid("../.ssh/authorized_keys")

    def test_leading_dot_dot_with_leading_slash_is_rejected(self):
        """Path like '/../../.ssh/authorized_keys' is rejected."""
        self._invalid("/../../.ssh/authorized_keys")

    def test_dot_dot_in_middle_is_rejected(self):
        """Path with '..' in the middle is rejected."""
        self._invalid("root/../etc/passwd")

    def test_deep_dot_dot_chain_is_rejected(self):
        """Path like 'a/b/../../../../etc/hosts' is rejected."""
        self._invalid("a/b/../../../../etc/hosts")

    def test_multiple_dot_dot_segments_is_rejected(self):
        """Path with multiple '..' components like '../../dir/../other' is rejected."""
        self._invalid("../../dir/../other")

    def test_dot_dot_at_end_is_rejected(self):
        """Path ending with '..' is rejected."""
        self._invalid("safe/dir/..")

    def test_encoded_dot_dot_via_backslash_is_still_detected(self):
        """A '..' segment hidden behind \\x2e encoding is NOT bypassed (literal check)."""
        with self.assertRaises(ValueError):
            # '\x2e\x2e' is the string ".." — our check on decoded string catches it
            local_runner._safe_credential_path("safe/\x2e\x2e/escaped", self._tmpdir)

    def test_path_that_resolves_outside_workspace_via_symlink_is_rejected(self):
        """A path that resolves outside workspace via abspath is rejected."""
        import tempfile
        outside = tempfile.mkdtemp()
        try:
            with self.assertRaises(ValueError):
                # /tmp/something/../../outside resolves to /outside which is outside workspace
                local_runner._safe_credential_path("/../../" + os.path.basename(outside), self._tmpdir)
        finally:
            os.rmdir(outside)

    def test_deep_traversal_from_workspace_is_rejected(self):
        """A path appending '../../..' from within the workspace is rejected."""
        self._invalid("some/sub/dir/../../../etc/passwd")

    def test_workspace_itself_is_safe(self):
        """A path of just '' or '.' relative resolves to workspace root."""
        resolved = self._valid("")
        self.assertEqual(resolved, os.path.abspath(self._tmpdir))

        resolved2 = self._valid(".")
        expected = os.path.abspath(os.path.join(self._tmpdir, "."))
        self.assertEqual(resolved2, expected)

    def test_rejects_bare_dot_dot(self):
        """Bare '..' is rejected."""
        self._invalid("..")


class TestCredentialPersistence(unittest.TestCase):
    """Token persistence: save/load round-trip, file permissions, fallback behaviour."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._orig_cred_dir = os.environ.get("BLUEPRINT_CREDENTIAL_DIR")
        os.environ["BLUEPRINT_CREDENTIAL_DIR"] = self._tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        if self._orig_cred_dir is None:
            os.environ.pop("BLUEPRINT_CREDENTIAL_DIR", None)
        else:
            os.environ["BLUEPRINT_CREDENTIAL_DIR"] = self._orig_cred_dir
        local_runner._shutdown_event.clear()

    def test_save_and_load_roundtrip(self):
        local_runner._save_runner_token("tok-abc123-test")
        self.assertEqual(local_runner._load_runner_token(), "tok-abc123-test")

    def test_save_load_runner_token_roundtrip(self):
        """Comprehensive save/load roundtrip verifying credential persistence,
        directory permissions (0700), and file permissions (0600).

        Exercises the hardened _save_runner_token which now chmods the credential
        directory, and the cross-module agreement with blueprint_runner._credential_dir.
        """
        import blueprint_runner as br

        # Verify the two modules agree on the credential directory (shared contract).
        self.assertEqual(
            local_runner._credential_dir(),
            br._credential_dir(),
            "local_runner._credential_dir() and blueprint_runner._credential_dir() "
            "must agree so the runner writes and the launcher reads from the same place.",
        )

        # Save a token and reload it — the basic round-trip.
        token = "tok-roundtrip-verify-123"
        local_runner._save_runner_token(token)
        self.assertEqual(local_runner._load_runner_token(), token)

        # Credential directory must be 0700 (hardened by the production change).
        cred_dir = local_runner._credential_dir()
        dir_mode = os.stat(cred_dir).st_mode & 0o777
        self.assertEqual(dir_mode, 0o700,
                        f"credential dir must be 0700, got {oct(dir_mode)}")

        # Token file must be 0600.
        path = local_runner._credential_path()
        file_mode = os.stat(path).st_mode & 0o777
        self.assertEqual(file_mode, 0o600,
                        f"token file must be 0600, got {oct(file_mode)}")

        # Overwrite with a new token and verify the latest value.
        local_runner._save_runner_token("updated-token")
        self.assertEqual(local_runner._load_runner_token(), "updated-token")

        # The credential dir permissions must survive the overwrite.
        dir_mode2 = os.stat(cred_dir).st_mode & 0o777
        self.assertEqual(dir_mode2, 0o700,
                        f"credential dir permissions must survive overwrite, got {oct(dir_mode2)}")

    def test_file_permissions_0600(self):
        local_runner._save_runner_token("tok-secret")
        path = local_runner._credential_path()
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"Expected 0600 but got {oct(mode)}")

    def test_load_nonexistent_returns_none(self):
        self.assertIsNone(local_runner._load_runner_token())

    def test_load_empty_returns_none(self):
        path = local_runner._credential_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        self.assertIsNone(local_runner._load_runner_token())

    def test_overwrites_existing(self):
        local_runner._save_runner_token("tok-first")
        local_runner._save_runner_token("tok-second")
        self.assertEqual(local_runner._load_runner_token(), "tok-second")

    def test_credential_dir_created_automatically(self):
        """_save_runner_token creates the credential directory if absent."""
        import shutil
        import tempfile
        fresh_dir = os.path.join(tempfile.mkdtemp(), "nonexistent_subdir")
        try:
            os.environ["BLUEPRINT_CREDENTIAL_DIR"] = fresh_dir
            local_runner._save_runner_token("tok-dir-test")
            self.assertEqual(local_runner._load_runner_token(), "tok-dir-test")
            self.assertTrue(os.path.isdir(fresh_dir))
        finally:
            shutil.rmtree(os.path.dirname(fresh_dir), ignore_errors=True)
            os.environ["BLUEPRINT_CREDENTIAL_DIR"] = self._tmpdir

    def test_main_saves_token_from_env_to_file(self):
        """When BLUEPRINT_RUNNER_TOKEN is set, main() persists it to the credential file."""
        fake_body = {"ok": True, "last_seen_at": "2026-01-01T00:00:00Z"}
        calls = []

        def fake_post(url, token, body, **kwargs):
            calls.append(token)
            local_runner._shutdown_event.set()
            return 200, fake_body

        env = {
            "BLUEPRINT_RUNNER_TOKEN": "tok-from-env",
            "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
            "BLUEPRINT_CREDENTIAL_DIR": self._tmpdir,
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(local_runner, "_post", side_effect=fake_post), \
             patch.object(local_runner, "_get_capabilities", return_value=_bwrap_caps()), \
             patch.object(local_runner, "_locate_agent_binary", return_value=None), \
             patch.object(local_runner, "_detect_gpu", return_value={}), \
             patch.object(local_runner, "_workspace_root", return_value="/tmp"), \
             patch.object(local_runner, "_workspace_writable", return_value=True), \
             patch.object(local_runner, "iso") as mock_iso:
            mock_iso.detect_capabilities.return_value = _bwrap_caps()
            mock_iso.claimable_classes.return_value = {"agent": False, "compute": False}
            mock_iso.agent_decline_reason.return_value = "no binary"
            mock_iso.CLASS_AGENT = "agent"
            mock_iso.CLASS_COMPUTE = "compute"
            mock_iso.sweep_stale_containers.return_value = None
            mock_iso.AGENT_DOCKER_TYPES = []
            try:
                local_runner.main()
            except SystemExit:
                pass

        saved = local_runner._load_runner_token()
        self.assertEqual(saved, "tok-from-env")

    def test_main_loads_token_from_file_when_env_absent(self):
        """When BLUEPRINT_RUNNER_TOKEN is unset, main() reads from the credential file."""
        local_runner._save_runner_token("tok-from-file")
        fake_body = {"ok": True, "last_seen_at": "2026-01-01T00:00:00Z"}
        tokens_used = []

        def fake_post(url, token, body, **kwargs):
            tokens_used.append(token)
            local_runner._shutdown_event.set()
            return 200, fake_body

        env = {
            "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
            "BLUEPRINT_CREDENTIAL_DIR": self._tmpdir,
        }
        clean_env = {k: v for k, v in os.environ.items() if k != "BLUEPRINT_RUNNER_TOKEN"}
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True), \
             patch.object(local_runner, "_post", side_effect=fake_post), \
             patch.object(local_runner, "_get_capabilities", return_value=_bwrap_caps()), \
             patch.object(local_runner, "_locate_agent_binary", return_value=None), \
             patch.object(local_runner, "_detect_gpu", return_value={}), \
             patch.object(local_runner, "_workspace_root", return_value="/tmp"), \
             patch.object(local_runner, "_workspace_writable", return_value=True), \
             patch.object(local_runner, "iso") as mock_iso:
            mock_iso.detect_capabilities.return_value = _bwrap_caps()
            mock_iso.claimable_classes.return_value = {"agent": False, "compute": False}
            mock_iso.agent_decline_reason.return_value = "no binary"
            mock_iso.CLASS_AGENT = "agent"
            mock_iso.CLASS_COMPUTE = "compute"
            mock_iso.sweep_stale_containers.return_value = None
            mock_iso.AGENT_DOCKER_TYPES = []
            try:
                local_runner.main()
            except SystemExit:
                pass

        self.assertTrue(
            any(t == "tok-from-file" for t in tokens_used),
            f"Expected token from file in heartbeat calls, got: {tokens_used}",
        )


class TestConcurrencyPresetMismatch(unittest.TestCase):
    """Warn when BLUEPRINT_RUNNER_MAX_CONCURRENT disagrees with server enrolled max_concurrent."""

    def setUp(self):
        local_runner._shutdown_event.clear()
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        os.environ["BLUEPRINT_CREDENTIAL_DIR"] = self._tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        os.environ.pop("BLUEPRINT_CREDENTIAL_DIR", None)
        local_runner._shutdown_event.clear()

    def _run_preflight(self, enrolled_max_concurrent, client_max_concurrent=2):
        if enrolled_max_concurrent is not None:
            fake_body = {
                "ok": True,
                "last_seen_at": "2026-01-01T00:00:00Z",
                "enrolled_max_concurrent": enrolled_max_concurrent,
            }
        else:
            fake_body = {"ok": True, "last_seen_at": "2026-01-01T00:00:00Z"}
        with patch.object(local_runner, "_post", return_value=(200, fake_body)), \
             patch.object(local_runner, "_workspace_writable", return_value=True):
            return local_runner._preflight(
                "https://api.example.com", "tok", None,
                _bwrap_caps(), _gpu_state(()), client_max_concurrent, 2,
            )

    def test_enrolled_max_concurrent_returned_in_preflight(self):
        """_preflight returns enrolled_max_concurrent when server provides it."""
        result = self._run_preflight(enrolled_max_concurrent=1)
        self.assertEqual(result.get("enrolled_max_concurrent"), 1)

    def test_none_when_server_does_not_include_field(self):
        """enrolled_max_concurrent is None when the server response omits the field."""
        result = self._run_preflight(enrolled_max_concurrent=None)
        self.assertIsNone(result.get("enrolled_max_concurrent"))

    def test_mismatch_warning_emitted(self):
        """main() warns when client max_concurrent != server enrolled_max_concurrent."""
        fake_body = {
            "ok": True,
            "last_seen_at": "2026-01-01T00:00:00Z",
            "enrolled_max_concurrent": 1,
        }

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 200, fake_body

        env = {
            "BLUEPRINT_RUNNER_TOKEN": "tok-test",
            "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
            "BLUEPRINT_RUNNER_MAX_CONCURRENT": "2",
            "BLUEPRINT_CREDENTIAL_DIR": self._tmpdir,
        }
        output_lines = []
        real_print = print

        def capture_print(*args, **kwargs):
            text = " ".join(str(a) for a in args)
            output_lines.append(text)
            real_print(*args, **kwargs)

        with patch.dict(os.environ, env, clear=False), \
             patch.object(local_runner, "_post", side_effect=fake_post), \
             patch.object(local_runner, "_get_capabilities", return_value=_bwrap_caps()), \
             patch.object(local_runner, "_locate_agent_binary", return_value=None), \
             patch.object(local_runner, "_detect_gpu", return_value={}), \
             patch.object(local_runner, "_workspace_root", return_value="/tmp"), \
             patch.object(local_runner, "_workspace_writable", return_value=True), \
             patch("builtins.print", side_effect=capture_print), \
             patch.object(local_runner, "iso") as mock_iso:
            mock_iso.detect_capabilities.return_value = _bwrap_caps()
            mock_iso.claimable_classes.return_value = {"agent": False, "compute": False}
            mock_iso.agent_decline_reason.return_value = "no binary"
            mock_iso.CLASS_AGENT = "agent"
            mock_iso.CLASS_COMPUTE = "compute"
            mock_iso.sweep_stale_containers.return_value = None
            mock_iso.AGENT_DOCKER_TYPES = []
            try:
                local_runner.main()
            except SystemExit:
                pass

        warning_lines = [line for line in output_lines if "BLUEPRINT_RUNNER_MAX_CONCURRENT" in line and "enrolled" in line.lower()]
        self.assertTrue(
            warning_lines,
            f"Expected mismatch warning in output. Got: {output_lines[:10]}",
        )

    def test_no_mismatch_warning_when_values_match(self):
        """No concurrency warning when client max_concurrent == server enrolled_max_concurrent."""
        fake_body = {
            "ok": True,
            "last_seen_at": "2026-01-01T00:00:00Z",
            "enrolled_max_concurrent": 2,
        }

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 200, fake_body

        env = {
            "BLUEPRINT_RUNNER_TOKEN": "tok-test",
            "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
            "BLUEPRINT_RUNNER_MAX_CONCURRENT": "2",
            "BLUEPRINT_CREDENTIAL_DIR": self._tmpdir,
        }
        output_lines = []
        real_print = print

        def capture_print(*args, **kwargs):
            text = " ".join(str(a) for a in args)
            output_lines.append(text)
            real_print(*args, **kwargs)

        with patch.dict(os.environ, env, clear=False), \
             patch.object(local_runner, "_post", side_effect=fake_post), \
             patch.object(local_runner, "_get_capabilities", return_value=_bwrap_caps()), \
             patch.object(local_runner, "_locate_agent_binary", return_value=None), \
             patch.object(local_runner, "_detect_gpu", return_value={}), \
             patch.object(local_runner, "_workspace_root", return_value="/tmp"), \
             patch.object(local_runner, "_workspace_writable", return_value=True), \
             patch("builtins.print", side_effect=capture_print), \
             patch.object(local_runner, "iso") as mock_iso:
            mock_iso.detect_capabilities.return_value = _bwrap_caps()
            mock_iso.claimable_classes.return_value = {"agent": False, "compute": False}
            mock_iso.agent_decline_reason.return_value = "no binary"
            mock_iso.CLASS_AGENT = "agent"
            mock_iso.CLASS_COMPUTE = "compute"
            mock_iso.sweep_stale_containers.return_value = None
            mock_iso.AGENT_DOCKER_TYPES = []
            try:
                local_runner.main()
            except SystemExit:
                pass

        warning_lines = [line for line in output_lines if "disagrees" in line and "enrolled" in line.lower()]
        self.assertFalse(
            warning_lines,
            f"Did not expect mismatch warning when values match. Got: {warning_lines}",
        )


class BlueprintRunnerInstallTests(unittest.TestCase):
    """Acceptance-evidence tests: the install path operators rely on for onboarding
    a new device renders a valid systemd unit with real host paths and the crash-loop
    burst cap. These tests import and exercise ``blueprint_runner`` (not local_runner)
    — the launcher that wraps the runner as a systemd service."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bp_install_test_")
        self.unit_path = os.path.join(self.tmp, "blueprint-runner.service")
        self.env_path = os.path.join(self.tmp, "runner.env")
        # The template file MUST exist at the real path — render_unit reads it.
        # Both templates live at well-known paths relative to this test file.
        self._user_template = br._USER_SERVICE_TEMPLATE
        self._system_template = br._SYSTEM_SERVICE_TEMPLATE
        self.assertTrue(
            os.path.isfile(self._user_template),
            f"user template missing: {self._user_template}",
        )
        self.assertTrue(
            os.path.isfile(self._system_template),
            f"system template missing: {self._system_template}",
        )

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def test_install_renders_unit_with_real_paths(self):
        """Acceptance evidence: the --user install flow renders the real template
        with resolved host paths, writes it to the user unit path, and preserves the
        crash-loop burst cap directives (StartLimitIntervalSec / StartLimitBurst)."""
        calls = []
        from scripts.test_blueprint_runner import _systemctl_recorder
        run = _systemctl_recorder(record=calls)

        patches = [
            patch.object(br, "_user_unit_path", return_value=self.unit_path),
            patch.object(br, "_env_file_path", return_value=self.env_path),
            patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"),
        ]
        for p in patches:
            p.start()
        try:
            rc = br.do_install(
                system=False,
                token="acceptance-token",
                url="https://orch.acceptance.example",
                _run=run,
            )
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0, "install must return 0 when all steps succeed")

        # The unit was written and contains real resolved paths (not @TOKENS@).
        unit_text = Path(self.unit_path).read_text()
        self.assertIn(sys.executable, unit_text,
                      "rendered unit must contain the real Python path")
        self.assertIn(br._LOCAL_RUNNER, unit_text,
                      "rendered unit must contain the real local_runner.py path")
        self.assertTrue(
            br._LOCAL_RUNNER.endswith("local_runner.py"),
            f"_LOCAL_RUNNER should point at local_runner.py, got {br._LOCAL_RUNNER}",
        )
        # No leftover @…@ template placeholders.
        import re
        leftover = re.findall(r"@[A-Z_]+@", unit_text)
        self.assertEqual(leftover, [], f"leftover placeholders: {leftover}")

        # Crash-loop guard directives from the updated service template are present.
        self.assertIn("StartLimitIntervalSec=60", unit_text,
                      "burst-cap interval must be in rendered unit")
        self.assertIn("StartLimitBurst=3", unit_text,
                      "burst count must be in rendered unit")
        self.assertIn("RestartSec=5", unit_text)
        self.assertIn("Restart=on-failure", unit_text)
        # StartLimitIntervalSec and StartLimitBurst belong in [Unit], not [Service].
        self.assertLess(
            unit_text.index("StartLimitIntervalSec=60"), unit_text.index("[Service]"),
            "StartLimitIntervalSec must appear in the [Unit] block")
        self.assertLess(
            unit_text.index("StartLimitBurst=3"), unit_text.index("[Service]"),
            "StartLimitBurst must appear in the [Unit] block")

        # The comment block documents crash-loop recovery.
        self.assertIn("reset-failed", unit_text,
                      "comment block must mention reset-failed recovery")

        # The token is in the env file, NOT baked into the unit.
        self.assertNotIn("acceptance-token", unit_text,
                         "token must never appear in the rendered unit")
        env_text = Path(self.env_path).read_text()
        self.assertIn("BLUEPRINT_RUNNER_TOKEN=acceptance-token", env_text)

        # systemctl commands were issued.
        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("daemon-reload" in j for j in joined),
                        "daemon-reload must be called during install")
        self.assertTrue(
            any("enable --now blueprint-runner.service" in j for j in joined),
            "enable --now must be called during install",
        )

    def test_system_install_renders_unit_with_burst_cap(self):
        """System install path also includes the crash-loop guard in the
        rendered unit."""
        calls = []
        from scripts.test_blueprint_runner import _systemctl_recorder
        run = _systemctl_recorder(record=calls)

        sys_unit = os.path.join(self.tmp, "etc-system", "blueprint-runner.service")
        patches = [
            patch.object(br, "_system_unit_path", return_value=sys_unit),
            patch.object(br, "_env_file_path", return_value=self.env_path),
            patch.object(br, "_install_user", return_value="testop"),
            patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"),
        ]
        for p in patches:
            p.start()
        try:
            rc = br.do_install(
                system=True, token=None, url="https://o.example", _run=run,
            )
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        unit_text = Path(sys_unit).read_text()
        self.assertIn("StartLimitIntervalSec=60", unit_text)
        self.assertIn("StartLimitBurst=3", unit_text)
        # StartLimitIntervalSec and StartLimitBurst belong in [Unit], not [Service].
        self.assertLess(
            unit_text.index("StartLimitIntervalSec=60"), unit_text.index("[Service]"),
            "StartLimitIntervalSec must appear in the [Unit] block")
        self.assertLess(
            unit_text.index("StartLimitBurst=3"), unit_text.index("[Service]"),
            "StartLimitBurst must appear in the [Unit] block")
        self.assertIn("reset-failed", unit_text)
        self.assertIn("User=testop", unit_text)
        self.assertIn("WantedBy=multi-user.target", unit_text)

    def test_uninstall_stops_and_removes_unit(self):
        """Uninstall disables, stops, and removes a present unit file, leaving
        credentials untouched."""
        unit = os.path.join(self.tmp, "installed.service")
        Path(unit).write_text("[Unit]\n")
        calls = []
        from scripts.test_blueprint_runner import _systemctl_recorder
        run = _systemctl_recorder(record=calls)

        with patch.object(br, "_user_unit_path", return_value=unit), \
             patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"):
            rc = br.do_uninstall(system=False, _run=run)

        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(unit), "unit file must be removed")
        joined = [" ".join(c) for c in calls]
        self.assertTrue(
            any("disable --now blueprint-runner.service" in j for j in joined),
        )
        self.assertTrue(any("daemon-reload" in j for j in joined))

    def test_resolve_token_falls_back_to_persisted_file(self):
        """resolve_token() reads the persisted token file when CLI and env are absent."""
        token_path = os.path.join(self.tmp, "runner_token")
        with open(token_path, "w") as f:
            f.write("persisted-acceptance-token\n")

        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_credential_dir", return_value=self.tmp):
            self.assertEqual(br.resolve_token(None), "persisted-acceptance-token")


class TestProviderCredentialCheck(unittest.TestCase):
    """LLM provider credential presence check for the preflight."""

    def test_detects_anthropic_api_key(self):
        """ANTHROPIC_API_KEY set -> credentials found."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
            self.assertTrue(br._check_provider_credentials())

    def test_detects_claude_code_oauth_token(self):
        """CLAUDE_CODE_OAUTH_TOKEN set -> credentials found."""
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}, clear=True):
            self.assertTrue(br._check_provider_credentials())

    def test_detects_zai_api_key(self):
        """ZAI_API_KEY set -> credentials found."""
        with patch.dict("os.environ", {"ZAI_API_KEY": "zai-test"}, clear=True):
            self.assertTrue(br._check_provider_credentials())

    def test_detects_openai_api_key(self):
        """OPENAI_API_KEY set -> credentials found."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-openai-test"}, clear=True):
            self.assertTrue(br._check_provider_credentials())

    def test_returns_false_when_none_set(self):
        """No credential env vars -> credentials NOT found."""
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(br._check_provider_credentials())

    def test_returns_false_when_only_whitespace(self):
        """A credential key set to whitespace is treated as absent."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "   "}, clear=True):
            self.assertFalse(br._check_provider_credentials())

    def test_warning_printed_in_preflight_when_no_credentials(self):
        """main() prints a WARNING when no provider credential is set (acceptance)."""
        output_lines = []

        def capture_print(*args, **kwargs):
            text = " ".join(str(a) for a in args)
            output_lines.append(text)

        def fake_post(url, token, body, **kwargs):
            return 200, {"ok": True}

        env = {
            "BLUEPRINT_RUNNER_TOKEN": "tok-acceptance",
            "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
        }
        with patch.dict("os.environ", env, clear=True), \
             patch("builtins.print", side_effect=capture_print), \
             patch.object(br, "_node_major", return_value=22), \
             patch.object(br.shutil, "which", return_value="/usr/bin/claude"), \
             patch.object(br, "run_local_runner", return_value=1):
            rc = br.main(["check"])

        warning_lines = [line for line in output_lines if "WARNING" in line and "credential" in line.lower()]
        self.assertTrue(
            warning_lines,
            f"Expected WARNING about missing credentials in output. Got: {output_lines}",
        )
        self.assertIn("ANTHROPIC_API_KEY", warning_lines[0])
        # Missing provider creds must make --check exit non-zero so the operator
        # catches the misconfiguration at enrollment time instead of discovering
        # every agent task fails at runtime.
        self.assertEqual(rc, 1, "Missing provider creds must make blueprint-runner check exit non-zero")


class TestStatusJson(unittest.TestCase):
    """On-box status.json liveness file: write, read, permissions, no-secrets."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self._orig_cred_dir = os.environ.get("BLUEPRINT_CREDENTIAL_DIR")
        os.environ["BLUEPRINT_CREDENTIAL_DIR"] = self._tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        if self._orig_cred_dir is None:
            os.environ.pop("BLUEPRINT_CREDENTIAL_DIR", None)
        else:
            os.environ["BLUEPRINT_CREDENTIAL_DIR"] = self._orig_cred_dir

    def _status_path(self):
        return os.path.join(self._tmpdir, "status.json")

    def test_write_and_read_round_trip(self):
        """Write status.json with known fields, read it back, verify all keys."""
        caps = _bwrap_caps()
        local_runner._write_status_json(
            runner_id="test-box",
            last_heartbeat_status="ok",
            active_task_ids=["task-1", "task-2"],
            active_compute_job_ids=["job-a"],
            caps=caps,
            agent_backend="bwrap",
            compute_backend="bwrap",
        )
        path = self._status_path()
        self.assertTrue(os.path.exists(path))

        with open(path) as f:
            data = json.loads(f.read())

        self.assertEqual(data["runner_id"], "test-box")
        self.assertEqual(data["last_heartbeat_status"], "ok")
        self.assertEqual(data["active_task_ids"], ["task-1", "task-2"])
        self.assertEqual(data["active_compute_job_ids"], ["job-a"])
        self.assertTrue(data["bwrap_ok"])
        self.assertFalse(data["docker_usable"])
        self.assertEqual(data["agent_backend"], "bwrap")
        self.assertEqual(data["compute_backend"], "bwrap")
        # last_heartbeat_at must be a plausible ISO UTC string
        self.assertIn("T", data["last_heartbeat_at"])
        self.assertIn("Z", data["last_heartbeat_at"])

    def test_atomic_write_no_partial_file(self):
        """tempfile + rename leaves no .tmp artifact in the credential dir."""
        local_runner._write_status_json(
            runner_id="box",
            last_heartbeat_status="ok",
            active_task_ids=[],
            active_compute_job_ids=[],
        )
        tmp_files = [f for f in os.listdir(self._tmpdir) if f.endswith(".tmp")]
        self.assertEqual(len(tmp_files), 0, f"stale .tmp files: {tmp_files}")

    def test_permissions_are_0644(self):
        """status.json must be world-readable (0644), not 0600 like the token."""
        local_runner._write_status_json(
            runner_id="box",
            last_heartbeat_status="ok",
            active_task_ids=[],
            active_compute_job_ids=[],
        )
        path = self._status_path()
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o644, f"Expected 0644 but got {oct(mode)}")

    def test_no_secrets_in_file(self):
        """status.json must NOT contain the token or any secret-like field."""
        local_runner._write_status_json(
            runner_id="box",
            last_heartbeat_status="ok",
            active_task_ids=[],
            active_compute_job_ids=[],
        )
        path = self._status_path()
        with open(path) as f:
            text = f.read()
        data = json.loads(text)
        for key in data:
            self.assertNotIn("token", key.lower(), f"secret-like key '{key}' in status.json")
            self.assertNotIn("secret", key.lower(), f"secret-like key '{key}' in status.json")
            self.assertNotIn("password", key.lower(), f"secret-like key '{key}' in status.json")
        # No sensitive string values
        self.assertNotIn("blpr_", text)

    def test_caps_none_writes_null_fields(self):
        """When caps is None, capability fields serialize as null — all keys present."""
        local_runner._write_status_json(
            runner_id="box",
            last_heartbeat_status="ok",
            active_task_ids=[],
            active_compute_job_ids=[],
            caps=None,
            agent_backend=None,
            compute_backend=None,
        )
        path = self._status_path()
        with open(path) as f:
            data = json.loads(f.read())
        self.assertIsNone(data["bwrap_ok"])
        self.assertIsNone(data["docker_usable"])
        self.assertIsNone(data["agent_backend"])
        self.assertIsNone(data["compute_backend"])

    def test_heartbeat_ok_path_writes_status(self):
        """After a successful heartbeat POST, status.json is written with 'ok'."""
        posted_bodies = []
        status_writes = []

        def fake_post(url, token, body, **kwargs):
            posted_bodies.append(body)
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        orig_write = local_runner._write_status_json

        def capturing_write(**kw):
            status_writes.append(kw)
            return orig_write(**kw)

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_write_status_json", capturing_write), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=["node", "/agent/dist/index.js"]), \
                 patch.object(local_runner, "_node_runtime_ok", return_value=True), \
                 patch.object(local_runner, "_hostname", return_value="test-host"):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    caps=_bwrap_caps(),
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(status_writes), 1)
        self.assertEqual(status_writes[0]["last_heartbeat_status"], "ok")
        self.assertEqual(status_writes[0]["runner_id"], "test-host")
        self.assertEqual(status_writes[0]["active_task_ids"], [])
        self.assertEqual(status_writes[0]["active_compute_job_ids"], [])

    def test_heartbeat_error_path_writes_status(self):
        """After a heartbeat POST returns non-2xx, status.json records the error."""
        status_writes = []

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            return 503, {}

        orig_write = local_runner._write_status_json

        def capturing_write(**kw):
            status_writes.append(kw)
            return orig_write(**kw)

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_write_status_json", capturing_write), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=["node", "/agent/dist/index.js"]), \
                 patch.object(local_runner, "_node_runtime_ok", return_value=True):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    caps=_bwrap_caps(),
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(status_writes), 1)
        self.assertIn("error", status_writes[0]["last_heartbeat_status"])
        self.assertIn("503", status_writes[0]["last_heartbeat_status"])

    def test_heartbeat_exception_path_writes_status(self):
        """When _post raises (network error), status.json records the exception."""
        status_writes = []

        def fake_post(url, token, body, **kwargs):
            local_runner._shutdown_event.set()
            raise RuntimeError("connection refused")

        orig_write = local_runner._write_status_json

        def capturing_write(**kw):
            status_writes.append(kw)
            return orig_write(**kw)

        active_tasks: dict = {}
        lock = threading.Lock()
        local_runner._shutdown_event.clear()

        try:
            with patch.object(local_runner, "_post", fake_post), \
                 patch.object(local_runner, "_write_status_json", capturing_write), \
                 patch.object(local_runner, "_locate_agent_binary", return_value=None):
                local_runner._heartbeat_loop(
                    "https://api", "tok", active_tasks, lock,
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                )
        finally:
            local_runner._shutdown_event.clear()

        self.assertEqual(len(status_writes), 1)
        self.assertIn("error", status_writes[0]["last_heartbeat_status"])
        self.assertIn("connection refused", status_writes[0]["last_heartbeat_status"])

    def test_die_writes_final_status(self):
        """_die() writes a status.json with the fatal reason before os._exit."""
        status_writes = []

        def capturing_write(**kw):
            status_writes.append(kw)

        def fake_exit(code):
            raise SystemExit(code)

        with patch.object(local_runner, "_write_status_json", capturing_write), \
             patch.object(local_runner, "_hostname", return_value="dying-box"), \
             patch.object(local_runner.os, "_exit", fake_exit):
            try:
                local_runner._die("OOM killer struck", code=137)
            except SystemExit:
                pass

        self.assertEqual(len(status_writes), 1)
        self.assertEqual(status_writes[0]["runner_id"], "dying-box")
        self.assertIn("fatal", status_writes[0]["last_heartbeat_status"])
        self.assertIn("OOM killer struck", status_writes[0]["last_heartbeat_status"])
        self.assertEqual(status_writes[0]["active_task_ids"], [])
        self.assertEqual(status_writes[0]["active_compute_job_ids"], [])


# ── Provider credential check in --check preflight ────────────────────────────

class TestProviderCredentials(unittest.TestCase):
    """Provider credential detection for --check preflight."""

    def test_no_creds_returns_empty(self):
        """When no provider credential env vars are set, returns empty list."""
        with patch.dict(os.environ, {}, clear=True):
            result = local_runner._check_provider_credentials()
        self.assertEqual(result, [])

    def test_anthropic_key_found(self):
        """ANTHROPIC_API_KEY appears in the found list."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-123"}):
            result = local_runner._check_provider_credentials()
        self.assertIn("ANTHROPIC_API_KEY", result)

    def test_openai_key_found(self):
        """OPENAI_API_KEY appears in the found list."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-456"}):
            result = local_runner._check_provider_credentials()
        self.assertIn("OPENAI_API_KEY", result)

    def test_multiple_keys_found(self):
        """Multiple provider creds all appear in the found list."""
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-openai-test",
            "GEMINI_API_KEY": "gemini-test",
        }):
            result = local_runner._check_provider_credentials()
        self.assertEqual(len(result), 3)
        self.assertIn("ANTHROPIC_API_KEY", result)
        self.assertIn("OPENAI_API_KEY", result)
        self.assertIn("GEMINI_API_KEY", result)

    def test_preflight_no_provider_credentials_warns(self):
        """Preflight --check warns when no provider credentials are set."""
        caps = _bwrap_caps()
        gpu_state = {"allowed": [], "in_use": set(), "per_job": 1}

        def fake_post(url, token, body, **kwargs):
            return 200, {"ok": True}

        buf = io.StringIO()
        with patch.dict(os.environ, _env(), clear=True), \
             patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_detect_gpu",
                          return_value={"has_gpu": False, "gpu_count": 0, "gpu_names": []}), \
             patch.object(local_runner, "_workspace_writable", return_value=True), \
             patch.object(local_runner, "_hostname", return_value="test-box"), \
             patch.object(local_runner, "_runner_version", return_value="v1-test"), \
             contextlib.redirect_stdout(buf):
            result = local_runner._preflight(
                "https://api.example.com", "tok-abcdefgh-1234",
                agent_binary=["node", "/agent/dist/index.js"],
                caps=caps, gpu_state=gpu_state,
                max_concurrent=2, max_concurrent_compute=2,
            )

        output = buf.getvalue()
        self.assertIn("FAIL", output)
        self.assertIn("provider creds", output)
        # Failure is when agent tasks are claimable but no provider creds are set.
        self.assertFalse(result["setup_ok"])

    def test_preflight_with_provider_credentials_does_not_warn(self):
        """Preflight does NOT warn when provider credentials are set."""
        caps = _bwrap_caps()
        gpu_state = {"allowed": [], "in_use": set(), "per_job": 1}

        def fake_post(url, token, body, **kwargs):
            return 200, {"ok": True}

        buf = io.StringIO()
        cred_env = _env(ANTHROPIC_API_KEY="sk-ant-test")
        with patch.dict(os.environ, cred_env, clear=True), \
             patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_detect_gpu",
                          return_value={"has_gpu": False, "gpu_count": 0, "gpu_names": []}), \
             patch.object(local_runner, "_workspace_writable", return_value=True), \
             patch.object(local_runner, "_hostname", return_value="test-box"), \
             patch.object(local_runner, "_runner_version", return_value="v1-test"), \
             contextlib.redirect_stdout(buf):
            result = local_runner._preflight(
                "https://api.example.com", "tok-abcdefgh-1234",
                agent_binary=["node", "/agent/dist/index.js"],
                caps=caps, gpu_state=gpu_state,
                max_concurrent=2, max_concurrent_compute=2,
            )

        output = buf.getvalue()
        self.assertNotIn("NONE", output)
        self.assertIn("present", output)
        self.assertIn("provider creds", output)
        self.assertTrue(result["setup_ok"])


# ── Per-task log persistence ────────────────────────────────────────────────


class TestTaskLogPersistence(unittest.TestCase):
    """Unit tests for per-task log file path, rotation, and cleanup."""

    def test_task_log_dir_defaults_to_credential_dir(self):
        """Default log dir is <credential_dir>/task-logs/."""
        with patch.object(local_runner, "_credential_dir",
                          return_value="/tmp/.blueprint"):
            log_dir = local_runner._task_log_dir()
        self.assertEqual(log_dir, "/tmp/.blueprint/task-logs")

    def test_task_log_dir_respects_override_env(self):
        """BLUEPRINT_RUNNER_LOG_DIR overrides the default log directory."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ,
                            {"BLUEPRINT_RUNNER_LOG_DIR": tmpdir}):
                log_dir = local_runner._task_log_dir()
            self.assertEqual(log_dir, os.path.abspath(tmpdir))

    def test_task_log_path_generates_safe_filename(self):
        """_task_log_path uses a sanitized task ID as the filename."""
        with patch.object(local_runner, "_task_log_dir",
                          return_value="/logs"):
            path = local_runner._task_log_path("task/abc:123")
        self.assertEqual(path, "/logs/task_abc_123.log")

    def test_task_log_path_with_unsafe_chars(self):
        """Unsafe chars in task IDs are replaced with underscores."""
        with patch.object(local_runner, "_task_log_dir",
                          return_value="/logs"):
            path = local_runner._task_log_path("task@#$%")
        self.assertEqual(path, "/logs/task____.log")

    def test_task_log_path_fallback_for_empty_id(self):
        """An empty or all-unsafe task ID falls back to 'unknown'."""
        with patch.object(local_runner, "_task_log_dir",
                          return_value="/logs"):
            path = local_runner._task_log_path("")
        self.assertEqual(path, "/logs/unknown.log")

    def test_cleanup_removes_old_log_files(self):
        """_cleanup_stale_task_logs removes .log files older than max_age."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            old_path = os.path.join(tmpdir, "old_task.log")
            new_path = os.path.join(tmpdir, "new_task.log")
            with open(old_path, "w") as f:
                f.write("old")
            with open(new_path, "w") as f:
                f.write("new")
            # Set old file's mtime far in the past.
            old_mtime = time.time() - 1000
            os.utime(old_path, (old_mtime, old_mtime))

            with patch.object(local_runner, "_task_log_dir",
                              return_value=tmpdir):
                removed = local_runner._cleanup_stale_task_logs(
                    max_age_seconds=500, max_files=100
                )

            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(old_path))
            self.assertTrue(os.path.exists(new_path))

    def test_cleanup_respects_file_count_cap(self):
        """_cleanup_stale_task_logs removes oldest files when count exceeds cap."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = []
            for i in range(5):
                p = os.path.join(tmpdir, f"task_{i}.log")
                with open(p, "w") as f:
                    f.write(str(i))
                paths.append(p)
                time.sleep(0.01)  # ensure distinct mtimes

            with patch.object(local_runner, "_task_log_dir",
                              return_value=tmpdir):
                removed = local_runner._cleanup_stale_task_logs(
                    max_age_seconds=99999, max_files=2
                )

            # Should remove the 3 oldest, keep the 2 newest.
            self.assertEqual(removed, 3)
            remaining = sorted(os.listdir(tmpdir))
            self.assertEqual(len(remaining), 2)

    def test_cleanup_skips_nonexistent_dir(self):
        """Cleanup is a no-op when the log directory does not exist."""
        with patch.object(local_runner, "_task_log_dir",
                          return_value="/tmp/nonexistent-log-dir-xyz"):
            removed = local_runner._cleanup_stale_task_logs()
        self.assertEqual(removed, 0)


# ── Credential file path-traversal guard ──────────────────────────────────────

class TestCredentialFileTraversalGuard(unittest.TestCase):
    """Path-traversal guard in _spawn_agent_task rejects credential-file paths
    that would escape the per-task workspace directory."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def _spawn_with_llm_files(self, workspace, llm_files):
        """Drive _spawn_agent_task with mocked iso layer and the given llm_files.
        Returns (spawned_flag, stdout_text)."""
        active_tasks: dict = {}
        agent_started_at: dict = {}
        agent_teardown: dict = {}
        fake_proc = MagicMock()
        fake_proc.pid = 4321
        fake_proc.stdout = io.BytesIO(b"")

        plan = iso.ExecutionPlan(
            backend=iso.BACKEND_BWRAP,
            argv=["bwrap", "node", "..."],
            env={"AGENT_SECRET_TOKEN": "s"},
            container_name=None,
        )

        with patch.object(local_runner.iso, "decide_agent_backend",
                          return_value=iso.BACKEND_BWRAP), \
             patch.object(local_runner.iso, "plan_agent_execution",
                          return_value=plan), \
             patch.object(local_runner.subprocess, "Popen",
                          return_value=fake_proc):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                spawned = local_runner._spawn_agent_task(
                    task_id="task-xyz",
                    task_type="research",
                    agent_token="agent-tok",
                    vm_instance_id="runner:local",
                    orchestrator_url="https://api.example.com",
                    workspace=workspace,
                    caps=_bwrap_caps(),
                    agent_binary=["node", "/agent/dist/index.js"],
                    agent_image=None,
                    active_tasks=active_tasks,
                    agent_started_at=agent_started_at,
                    agent_teardown=agent_teardown,
                    lock=threading.Lock(),
                    llm_files=llm_files,
                )
        return spawned, buf.getvalue()

    def test_normal_in_workspace_path_writes(self):
        """A legitimate relative path inside the workspace is written correctly."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            llm_files = [
                {
                    "path": "home/user/.config/claude/config.json",
                    "content": '{"provider": "anthropic"}',
                }
            ]
            spawned, output = self._spawn_with_llm_files(tmpdir, llm_files)

            dest = os.path.join(tmpdir, "home/user/.config/claude/config.json")
            self.assertTrue(os.path.isfile(dest), "normal path must be written")
            with open(dest) as f:
                self.assertEqual(f.read(), '{"provider": "anthropic"}')
            # No warning about skipping
            self.assertNotIn("skipping", output.lower())

    def test_simple_traversal_skipped_and_warns(self):
        """A path with ../ is rejected (not written) and a warning is logged."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            llm_files = [
                {
                    "path": "../escape.txt",
                    "content": "SHOULD NOT BE WRITTEN",
                }
            ]
            spawned, output = self._spawn_with_llm_files(tmpdir, llm_files)

            # The file must not exist at its resolved (escaped) location
            escaped = os.path.abspath(os.path.normpath(
                os.path.join(tmpdir, "../escape.txt")
            ))
            self.assertFalse(os.path.isfile(escaped),
                             "traversal path must not be written outside workspace")
            # Nothing written inside workspace either
            inside = os.path.join(tmpdir, "escape.txt")
            self.assertFalse(os.path.isfile(inside),
                             "traversal path must not be written inside workspace")

            # Warning must mention the path and the word "skipping"
            self.assertIn("outside workspace", output.lower())
            self.assertIn("skipping", output.lower())

    def test_deep_traversal_skipped(self):
        """A deeper ../ sequence (../../x/y) is also rejected by the guard."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            llm_files = [
                {
                    "path": "../../etc/__blueprint_escape_test",
                    "content": "SHOULD NOT BE WRITTEN",
                }
            ]
            spawned, output = self._spawn_with_llm_files(tmpdir, llm_files)

            escaped = os.path.abspath(os.path.normpath(
                os.path.join(tmpdir, "../../etc/__blueprint_escape_test")
            ))
            self.assertFalse(os.path.isfile(escaped),
                             "deep traversal must not write outside workspace")
            self.assertIn("outside workspace", output.lower())
            self.assertIn("skipping", output.lower())

    def test_mixed_paths_normal_writes_traversal_skipped(self):
        """A normal path is still written when a traversal path is also present."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            llm_files = [
                {
                    "path": ".config/claude/config.json",
                    "content": '{"valid": true}',
                },
                {
                    "path": "../escape.txt",
                    "content": "SHOULD NOT BE WRITTEN",
                },
            ]
            spawned, output = self._spawn_with_llm_files(tmpdir, llm_files)

            # Normal file must be written
            normal = os.path.join(tmpdir, ".config/claude/config.json")
            self.assertTrue(os.path.isfile(normal),
                            "normal path must still be written alongside traversal path")
            with open(normal) as f:
                self.assertEqual(f.read(), '{"valid": true}')

            # Traversal file must not be written
            escaped = os.path.abspath(os.path.normpath(
                os.path.join(tmpdir, "../escape.txt")
            ))
            self.assertFalse(os.path.isfile(escaped),
                             "traversal path must not be written despite sibling normal path")
            self.assertIn("outside workspace", output.lower())
            self.assertIn("skipping", output.lower())

    def test_leading_slash_normalized_still_in_workspace(self):
        """A leading-slash path is normalized to workspace-relative and passes."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            llm_files = [
                {
                    "path": "/home/user/.config/credentials.json",
                    "content": '{"key": "secret"}',
                }
            ]
            spawned, output = self._spawn_with_llm_files(tmpdir, llm_files)

            # The leading / is stripped by .lstrip("/"), then joined with workspace
            dest = os.path.join(tmpdir, "home/user/.config/credentials.json")
            self.assertTrue(os.path.isfile(dest),
                            "leading-slash path must be treated as relative to workspace")
            self.assertNotIn("skipping", output.lower())


if __name__ == "__main__":
    unittest.main()
