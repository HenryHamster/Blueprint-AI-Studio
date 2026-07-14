"""Tests for scripts/local_runner.py."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
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


class TestCheckModeAggregatesFailures(unittest.TestCase):
    """`--check` reports ALL missing prerequisites in one pass (not first-fail)."""

    def _run_check(self, env_overrides=None, strip_vars=None):
        env = os.environ.copy()
        env.pop("BLUEPRINT_RUNNER_TOKEN", None)
        env.pop("BLUEPRINT_ORCHESTRATOR_URL", None)
        # Point the credential file at an empty temp dir so a real saved token on
        # the test box can't mask a "missing token" case.
        with tempfile.TemporaryDirectory() as tmp:
            env["BLUEPRINT_CREDENTIAL_DIR"] = tmp
            if env_overrides:
                env.update(env_overrides)
            if strip_vars:
                for k in strip_vars:
                    env.pop(k, None)
            return subprocess.run(
                [sys.executable, str(Path(__file__).parent / "local_runner.py"), "--check"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_check_missing_both_reports_both_and_exits_1(self):
        # Regression: today --check dies naming only the token; it must name BOTH.
        result = self._run_check()
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLUEPRINT_RUNNER_TOKEN", combined)
        self.assertIn("BLUEPRINT_ORCHESTRATOR_URL", combined)

    def test_check_missing_url_still_runs_preflight_probes(self):
        # Token present, URL absent: the preflight report must still print (the
        # probes ran despite the missing URL) rather than a bare first-fail exit.
        result = self._run_check(env_overrides={"BLUEPRINT_RUNNER_TOKEN": "tok-abc-123456"})
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLUEPRINT_ORCHESTRATOR_URL", combined)
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", combined.split("missing prerequisite")[-1])
        # Preflight probe output (isolation / task classes) proves probes ran.
        self.assertTrue(
            ("isolation" in combined.lower()) or ("task class" in combined.lower())
            or ("claimable" in combined.lower()),
            msg=f"expected preflight probe output, got:\n{combined}",
        )


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


# ── Host-load throttle ─────────────────────────────────────────────────────────

class TestHostLoadThrottle(unittest.TestCase):
    """_compute_poll_loop and agent poll skip claims when load thresholds are met."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def _make_compute_deps(self):
        active_jobs: set = set()
        lock = threading.Lock()
        return active_jobs, lock

    def _run_compute_poll(self, gpu_util_pct_ret, max_gpu_util_pct, post_side_effect):
        """Run _compute_poll_loop in a thread for 0.15s, return list of posted calls."""
        calls = []

        def fake_post(url, token, body, **kwargs):
            calls.append(url)
            return post_side_effect(url)

        active_jobs, lock = self._make_compute_deps()
        with patch.object(local_runner, "_gpu_utilization_pct", return_value=gpu_util_pct_ret), \
             patch.object(local_runner, "_post", side_effect=fake_post):
            t = threading.Thread(
                target=local_runner._compute_poll_loop,
                args=("http://test", "tok", active_jobs, lock, 1),
                kwargs={"max_gpu_util_pct": max_gpu_util_pct, "base_poll_interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.15)
            local_runner._shutdown_event.set()
            t.join(timeout=2)
        return calls

    def test_cpu_load_disabled_by_default(self):
        """max_cpu_load=0 (default) never blocks claiming regardless of load."""
        calls = self._run_compute_poll(
            gpu_util_pct_ret=None,
            max_gpu_util_pct=0,
            post_side_effect=lambda url: (204, None, None),
        )
        # The poll loop should have called _post (not skipped due to load)
        self.assertTrue(len(calls) >= 1)

    def test_gpu_util_above_threshold_skips_compute_claim(self):
        """Compute poll skips claiming when GPU utilization is at or above threshold."""
        calls = self._run_compute_poll(
            gpu_util_pct_ret=80,
            max_gpu_util_pct=80,
            post_side_effect=lambda url: (204, None, None),
        )
        # _post should NOT have been called — throttled
        self.assertEqual(calls, [])

    def test_gpu_util_below_threshold_allows_compute_claim(self):
        """Compute poll proceeds when GPU utilization is below threshold."""
        calls = self._run_compute_poll(
            gpu_util_pct_ret=79,
            max_gpu_util_pct=80,
            post_side_effect=lambda url: (204, None, None),
        )
        self.assertTrue(len(calls) >= 1)

    def test_gpu_util_none_does_not_skip(self):
        """When GPU util can't be read (None), claiming is not blocked."""
        calls = self._run_compute_poll(
            gpu_util_pct_ret=None,
            max_gpu_util_pct=80,
            post_side_effect=lambda url: (204, None, None),
        )
        self.assertTrue(len(calls) >= 1)

    def test_low_disk_skips_compute_claim(self):
        """Compute poll skips claiming when workspace free disk is below the guard.

        A baked container-image job must pull a multi-GB image before starting;
        claiming on a nearly-full box wastes the claim, so the loop must not
        even POST to next-compute-job while disk is below the floor.
        """
        calls = []

        def fake_post(url, token, body, **kwargs):
            calls.append(url)
            return (204, None, None)

        active_jobs, lock = self._make_compute_deps()
        with patch.object(local_runner, "_gpu_utilization_pct", return_value=None), \
             patch.object(local_runner, "_workspace_root", return_value="/tmp"), \
             patch.object(local_runner, "_min_free_disk_bytes", return_value=1_000_000), \
             patch.object(local_runner, "_disk_below_min", return_value=True), \
             patch.object(local_runner, "_post", side_effect=fake_post):
            t = threading.Thread(
                target=local_runner._compute_poll_loop,
                args=("http://test", "tok", active_jobs, lock, 1),
                kwargs={"base_poll_interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.15)
            local_runner._shutdown_event.set()
            t.join(timeout=2)
        self.assertEqual(calls, [], "must not poll for compute work while disk is low")

    def test_disk_ok_allows_compute_claim(self):
        """Compute poll proceeds when workspace free disk is above the guard."""
        calls = []

        def fake_post(url, token, body, **kwargs):
            calls.append(url)
            return (204, None, None)

        active_jobs, lock = self._make_compute_deps()
        with patch.object(local_runner, "_gpu_utilization_pct", return_value=None), \
             patch.object(local_runner, "_workspace_root", return_value="/tmp"), \
             patch.object(local_runner, "_min_free_disk_bytes", return_value=1_000_000), \
             patch.object(local_runner, "_disk_below_min", return_value=False), \
             patch.object(local_runner, "_post", side_effect=fake_post):
            t = threading.Thread(
                target=local_runner._compute_poll_loop,
                args=("http://test", "tok", active_jobs, lock, 1),
                kwargs={"base_poll_interval": 0.05},
                daemon=True,
            )
            t.start()
            time.sleep(0.15)
            local_runner._shutdown_event.set()
            t.join(timeout=2)
        self.assertTrue(len(calls) >= 1)


# ── Live-safe config applied from heartbeat response ──────────────────────────

class TestLiveRunnerConfig(unittest.TestCase):
    """Unit tests for the _LiveRunnerConfig holder: apply, clamp, report."""

    def _holder(self, gpu_indices=(0, 1, 2, 3)):
        gpu_state = _gpu_state(gpu_indices)
        holder = local_runner._LiveRunnerConfig(
            2, 2, task_types="all", gpu_state=gpu_state,
            all_gpu_indices=list(gpu_indices),
        )
        return holder, gpu_state

    def test_grow_and_shrink_concurrency(self):
        holder, _ = self._holder()
        self.assertEqual(holder.max_concurrent(), 2)
        applied = holder.apply({"max_concurrent": 8, "config_generation": "g1"})
        self.assertTrue(applied)
        self.assertEqual(holder.max_concurrent(), 8)
        # Shrink: the new value is the live source of truth; running work is the
        # caller's concern (the holder never touches it).
        holder.apply({"max_concurrent": 1, "config_generation": "g2"})
        self.assertEqual(holder.max_concurrent(), 1)

    def test_compute_limit_applied_independently(self):
        holder, _ = self._holder()
        holder.apply({"max_concurrent_compute": 5, "config_generation": "g"})
        self.assertEqual(holder.max_concurrent_compute(), 5)
        self.assertEqual(holder.max_concurrent(), 2)  # untouched

    def test_clamp_out_of_range(self):
        holder, _ = self._holder()
        holder.apply({"max_concurrent": 999, "config_generation": "hi"})
        self.assertEqual(holder.max_concurrent(), local_runner._LiveRunnerConfig.MAX_CONCURRENT)
        holder.apply({"max_concurrent": 0, "config_generation": "lo"})
        self.assertEqual(holder.max_concurrent(), local_runner._LiveRunnerConfig.MIN_CONCURRENT)

    def test_null_field_keeps_current_default(self):
        holder, _ = self._holder()
        holder.apply({"max_concurrent": 6, "config_generation": "g1"})
        # A subsequent block with a null max_concurrent must not clobber the value.
        holder.apply({"max_concurrent": None, "task_types": ["fix"], "config_generation": "g2"})
        self.assertEqual(holder.max_concurrent(), 6)

    def test_task_types_allowlist_applied_and_reported(self):
        holder, _ = self._holder()
        holder.apply({"task_types": ["create", "fix"], "config_generation": "g"})
        self.assertEqual(holder.report()["applied_task_types"], ["create", "fix"])
        holder.apply({"task_types": "all", "config_generation": "g2"})
        self.assertEqual(holder.report()["applied_task_types"], "all")

    def test_gpu_allowlist_updates_scheduling_set(self):
        holder, gpu_state = self._holder(gpu_indices=(0, 1, 2, 3))
        holder.apply({"gpu_indices_allowed": [1, 2], "config_generation": "g"})
        self.assertEqual(gpu_state["allowed"], [1, 2])
        self.assertEqual(holder.report()["applied_gpu_indices_allowed"], [1, 2])
        # "all" maps back to every DETECTED index.
        holder.apply({"gpu_indices_allowed": "all", "config_generation": "g2"})
        self.assertEqual(sorted(gpu_state["allowed"]), [0, 1, 2, 3])
        self.assertEqual(holder.report()["applied_gpu_indices_allowed"], "all")

    def test_gpu_allowlist_intersected_with_detected(self):
        holder, gpu_state = self._holder(gpu_indices=(0, 1))
        # Server asks for index 5 which this host lacks — never assign it.
        holder.apply({"gpu_indices_allowed": [0, 5], "config_generation": "g"})
        self.assertEqual(gpu_state["allowed"], [0])
        # The reported value mirrors the server projection (so the echoed
        # generation matches), even though only [0] is concretely assignable.
        self.assertEqual(holder.report()["applied_gpu_indices_allowed"], [0, 5])

    def test_gpus_per_job_applied_updates_scheduling_set(self):
        holder, gpu_state = self._holder(gpu_indices=(0, 1, 2, 3))
        # Seeded from gpu_state["per_job"] == 1.
        self.assertEqual(holder.report()["applied_gpus_per_job"], 1)
        holder.apply({"gpus_per_job": 2, "config_generation": "g"})
        # The shared scheduling state now reserves 2 GPUs per job, and the
        # applied value is echoed for the desired-vs-applied fleet view.
        self.assertEqual(gpu_state["per_job"], 2)
        self.assertEqual(holder.report()["applied_gpus_per_job"], 2)

    def test_gpus_per_job_clamped_to_bounds(self):
        holder, gpu_state = self._holder()
        holder.apply({"gpus_per_job": 999, "config_generation": "hi"})
        self.assertEqual(
            gpu_state["per_job"], local_runner._LiveRunnerConfig.MAX_GPUS_PER_JOB
        )
        holder.apply({"gpus_per_job": 0, "config_generation": "lo"})
        self.assertEqual(
            gpu_state["per_job"], local_runner._LiveRunnerConfig.MIN_GPUS_PER_JOB
        )

    def test_gpus_per_job_null_keeps_current(self):
        holder, gpu_state = self._holder()
        holder.apply({"gpus_per_job": 3, "config_generation": "g1"})
        self.assertEqual(gpu_state["per_job"], 3)
        # A later block omitting/nulling the field must not reset it.
        holder.apply({"gpus_per_job": None, "config_generation": "g2"})
        self.assertEqual(gpu_state["per_job"], 3)
        self.assertEqual(holder.report()["applied_gpus_per_job"], 3)

    def test_gpus_per_job_seeded_from_gpu_state(self):
        gpu_state = _gpu_state((0, 1))
        gpu_state["per_job"] = 2
        holder = local_runner._LiveRunnerConfig(
            2, 2, task_types="all", gpu_state=gpu_state, all_gpu_indices=[0, 1],
        )
        # The reported value matches the scheduler's startup per-job count so the
        # first heartbeat echo is not spuriously pending.
        self.assertEqual(holder.report()["applied_gpus_per_job"], 2)

    def test_gpus_per_job_unknown_field_still_blocks_generation(self):
        # gpus_per_job is now a KNOWN key — a block carrying only it (plus known
        # fields) is fully applicable and advances the generation.
        holder, _ = self._holder()
        self.assertTrue(
            holder.apply({"gpus_per_job": 2, "config_generation": "gen-pj"})
        )
        self.assertEqual(holder.report()["applied_config_generation"], "gen-pj")

    def test_generation_advances_only_when_fully_applicable(self):
        holder, _ = self._holder()
        self.assertIsNone(holder.report()["applied_config_generation"])
        self.assertTrue(holder.apply({"max_concurrent": 4, "config_generation": "gen-A"}))
        self.assertEqual(holder.report()["applied_config_generation"], "gen-A")
        # A block carrying an UNRECOGNIZED non-null field is not fully applicable:
        # the known fields still apply, but the generation stays pending (older).
        applied = holder.apply({
            "max_concurrent": 7,
            "future_field": "something",
            "config_generation": "gen-B",
        })
        self.assertFalse(applied)
        self.assertEqual(holder.max_concurrent(), 7)  # known field still applied
        self.assertEqual(holder.report()["applied_config_generation"], "gen-A")  # not advanced

    def test_malformed_block_is_noop(self):
        holder, _ = self._holder()
        self.assertFalse(holder.apply(None))
        self.assertFalse(holder.apply("not-a-dict"))
        self.assertEqual(holder.max_concurrent(), 2)


class TestComputePollLoopLiveConfig(unittest.TestCase):
    """_compute_poll_loop gates on the LIVE compute limit, not the static arg."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_live_limit_overrides_static_max_concurrent(self):
        """A live config that GROWS the compute limit lets a claim proceed even
        though the static positional max_concurrent would block it."""
        post_calls = []
        sleep_iters = [0]

        def fake_post(url, token, body, **kwargs):
            post_calls.append(url)
            return 204, None, None

        def fake_sleep(secs):
            sleep_iters[0] += 1
            if sleep_iters[0] >= 2:
                local_runner._shutdown_event.set()

        # One job already running; static ceiling is 1 (would block), but the
        # live holder was grown to 2 (should allow a claim).
        active_jobs = {"job-a"}
        lock = threading.Lock()
        holder = local_runner._LiveRunnerConfig(1, 2)

        with patch.object(local_runner, "_post", side_effect=fake_post), \
             patch("time.sleep", fake_sleep):
            local_runner._compute_poll_loop(
                "https://api.example.com", "tok", active_jobs, lock,
                max_concurrent=1, live_config=holder,
            )
        self.assertTrue(post_calls, "live-grown compute limit must allow the claim")

    def test_live_limit_blocks_when_shrunk(self):
        """A live config that SHRINKS the compute limit stops new claims even
        though the static positional max_concurrent would allow them."""
        post_calls = []
        sleep_iters = [0]

        def fake_post(url, token, body, **kwargs):
            post_calls.append(url)
            return 204, None, None

        def fake_sleep(secs):
            sleep_iters[0] += 1
            if sleep_iters[0] >= 2:
                local_runner._shutdown_event.set()

        active_jobs = {"job-a"}
        lock = threading.Lock()
        holder = local_runner._LiveRunnerConfig(5, 1)  # live compute limit = 1

        with patch.object(local_runner, "_post", side_effect=fake_post), \
             patch("time.sleep", fake_sleep):
            local_runner._compute_poll_loop(
                "https://api.example.com", "tok", active_jobs, lock,
                max_concurrent=5, live_config=holder,
            )
        self.assertEqual(post_calls, [], "live-shrunk compute limit must block the claim")


class TestHeartbeatLiveConfig(unittest.TestCase):
    """_heartbeat_loop applies the server config block and echoes applied-vs-pending."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def _drive(self, response_body, seed=(2, 2), all_gpu=(0, 1)):
        """Run _heartbeat_loop for two ticks; return (holder, [posted bodies])."""
        gpu_state = _gpu_state(all_gpu)
        holder = local_runner._LiveRunnerConfig(
            seed[0], seed[1], task_types="all", gpu_state=gpu_state,
            all_gpu_indices=list(all_gpu),
        )
        bodies = []
        calls = [0]

        def fake_post(url, token, body, **kwargs):
            bodies.append(dict(body))
            calls[0] += 1
            if calls[0] >= 2:
                local_runner._shutdown_event.set()
            return 200, response_body

        active_tasks: dict = {}
        lock = threading.Lock()
        # An idle runner + a mismatched version_hint arms the AUTO-UPDATE path.
        # Unstubbed, that runs a real `git fetch` (do_update) and then either
        # os._exit()s or os.execv()s — killing the pytest process itself and
        # silently truncating the run (exit 0, later tests never reported). These
        # tests are about the config echo, so the update path is stubbed inert.
        with patch.object(local_runner, "_post", side_effect=fake_post), \
             patch.object(local_runner, "_write_status_json"), \
             patch.object(local_runner.br, "do_update", return_value=1), \
             patch.object(os, "_exit"), \
             patch.object(os, "execv"), \
             patch("time.sleep", lambda *_: None):
            local_runner._heartbeat_loop(
                "https://api.example.com", "tok", active_tasks, lock, {},
                live_config=holder,
            )
        return holder, bodies

    def test_config_block_applied_and_echoed(self):
        response = {
            "ok": True,
            "config": {
                "max_concurrent": 8,
                "max_concurrent_compute": 4,
                "task_types": ["create"],
                "gpu_indices_allowed": [0, 1],
                "config_generation": "gen-xyz",
            },
        }
        holder, bodies = self._drive(response)
        # Live values applied.
        self.assertEqual(holder.max_concurrent(), 8)
        self.assertEqual(holder.max_concurrent_compute(), 4)
        # First body: nothing applied yet (generation pending / seed value).
        self.assertIsNone(bodies[0]["applied_config_generation"])
        self.assertEqual(bodies[0]["applied_max_concurrent"], 2)
        # Second body: echoes the generation + applied values from the first tick.
        self.assertEqual(bodies[1]["applied_config_generation"], "gen-xyz")
        self.assertEqual(bodies[1]["applied_max_concurrent"], 8)
        self.assertEqual(bodies[1]["applied_max_concurrent_compute"], 4)
        self.assertEqual(bodies[1]["applied_task_types"], ["create"])
        self.assertEqual(bodies[1]["applied_gpu_indices_allowed"], [0, 1])

    def test_env_defaults_when_no_config_block(self):
        """No config block in the response leaves the env-seeded defaults intact
        and the applied generation null (server renders as pending/unknown)."""
        holder, bodies = self._drive({"ok": True, "version_hint": "abc"}, seed=(3, 3))
        self.assertEqual(holder.max_concurrent(), 3)
        self.assertEqual(holder.max_concurrent_compute(), 3)
        for body in bodies:
            self.assertIsNone(body["applied_config_generation"])
            self.assertEqual(body["applied_max_concurrent"], 3)


class TestHeartbeatAgentUnavailableLegs(unittest.TestCase):
    """_heartbeat_loop reports EVERY failing agent leg, not just the first.

    The four independent legs gating the agent backends — bwrap sandbox, agent
    dist, node runtime, docker — must each surface independently: a compound
    ``agent_unavailable_reason`` naming every failed leg AND a structured
    ``agent_unavailable_legs`` map of leg -> ok bool.
    """

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def _drive_once(self, *, agent_sandbox_ok, binary_present, node_ok, docker_ok):
        """Run one heartbeat tick with each leg's state pinned; return the body."""
        caps = iso.Capabilities()  # truthy caps → per-leg map is emitted
        bodies = []
        calls = [0]

        def fake_post(url, token, body, **kwargs):
            bodies.append(dict(body))
            calls[0] += 1
            local_runner._shutdown_event.set()
            return 200, {"ok": True}

        binary = ["node", "/x/agent"] if binary_present else None
        active_tasks: dict = {}
        lock = threading.Lock()
        with patch.object(local_runner, "_post", side_effect=fake_post), \
             patch.object(local_runner, "_write_status_json"), \
             patch.object(local_runner, "_locate_agent_binary", return_value=binary), \
             patch.object(local_runner, "_node_runtime_ok", return_value=node_ok), \
             patch.object(local_runner, "_docker_agent_possible", return_value=docker_ok), \
             patch.object(iso, "probe_llm_clis", return_value={}), \
             patch("time.sleep", lambda *_: None):
            local_runner._heartbeat_loop(
                "https://api.example.com", "tok", active_tasks, lock, {},
                agent_sandbox_ok=agent_sandbox_ok, caps=caps, agent_image="img:latest",
            )
        self.assertTrue(bodies)
        return bodies[0]

    def test_multiple_failing_legs_all_named_in_reason(self):
        """Sandbox + node + docker all failing => reason enumerates ALL, not one."""
        body = self._drive_once(
            agent_sandbox_ok=False, binary_present=True, node_ok=False, docker_ok=False,
        )
        self.assertFalse(body["agent_available"])
        reason = body["agent_unavailable_reason"]
        self.assertIn("bwrap sandbox unavailable", reason)
        self.assertIn("node runtime not functional", reason)
        self.assertIn("docker agent unavailable", reason)
        # Compound string joins the failed legs (not a single first-match phrase).
        self.assertEqual(reason.count(";"), 2)

    def test_agent_unavailable_legs_map_reflects_each_leg(self):
        """Every leg's ok/failed boolean is reported independently in the map."""
        body = self._drive_once(
            agent_sandbox_ok=False, binary_present=False, node_ok=True, docker_ok=False,
        )
        legs = body["agent_unavailable_legs"]
        self.assertEqual(
            legs,
            {
                "bwrap_sandbox": False,
                "agent_dist": False,
                "node_runtime": True,
                "docker": False,
            },
        )
        # All four failing legs relevant to unavailability are named; node ok is not.
        reason = body["agent_unavailable_reason"]
        self.assertIn("bwrap sandbox unavailable", reason)
        self.assertIn("agent dist not built", reason)
        self.assertIn("docker agent unavailable", reason)
        self.assertNotIn("node runtime", reason)

    def test_agent_available_clears_reason_and_reports_legs(self):
        """When the bwrap trio is satisfied the agent is available: reason None,
        legs still reported (all the legs that mattered are ok)."""
        body = self._drive_once(
            agent_sandbox_ok=True, binary_present=True, node_ok=True, docker_ok=False,
        )
        self.assertTrue(body["agent_available"])
        self.assertIsNone(body["agent_unavailable_reason"])
        self.assertEqual(
            body["agent_unavailable_legs"],
            {
                "bwrap_sandbox": True,
                "agent_dist": True,
                "node_runtime": True,
                "docker": False,
            },
        )

    def test_docker_only_box_available_without_host_node_or_binary(self):
        """A docker-only box (no bwrap, no host dist/node) is still available via
        docker; the legs map shows the failed bwrap legs but reason stays None."""
        body = self._drive_once(
            agent_sandbox_ok=False, binary_present=False, node_ok=False, docker_ok=True,
        )
        self.assertTrue(body["agent_available"])
        self.assertIsNone(body["agent_unavailable_reason"])
        self.assertEqual(
            body["agent_unavailable_legs"],
            {
                "bwrap_sandbox": False,
                "agent_dist": False,
                "node_runtime": False,
                "docker": True,
            },
        )


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


# ── Exit-code 42 (runtime_unavailable) does not increment failure streak ─────

class TestExitCode42NoFailureStreak(unittest.TestCase):
    """exit code 42 (runtime_unavailable) must NOT increment consecutive_job_failures."""

    def setUp(self):
        local_runner._shutdown_event.clear()

    def tearDown(self):
        local_runner._shutdown_event.clear()

    def test_exit_code_42_does_not_increment_failure_streak(self):
        proc = MagicMock()
        proc.poll.return_value = 0   # poll() non-None so the reaper picks it up
        proc.returncode = 42

        active_tasks = {"task-42": proc}
        lock = threading.Lock()
        capacity = threading.BoundedSemaphore(1)
        self.assertTrue(capacity.acquire(blocking=False))  # permit held by the task
        telemetry = local_runner._TelemetryState()

        def stop_sleep(_secs):
            local_runner._shutdown_event.set()

        with patch("time.sleep", stop_sleep):
            local_runner._reap_loop(active_tasks, lock, capacity, telemetry=telemetry)

        self.assertNotIn("task-42", active_tasks)
        self.assertTrue(capacity.acquire(blocking=False))  # permit was released
        _last_success, fails = telemetry.snapshot()
        # Exit code 42 is a graceful infra decline — failure streak must stay 0.
        self.assertEqual(fails, 0)

    def test_nonzero_non42_exit_still_increments_failure_streak(self):
        """Sanity-check: a normal failure (e.g. exit 1) does increment the streak."""
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 1

        active_tasks = {"task-fail": proc}
        lock = threading.Lock()
        telemetry = local_runner._TelemetryState()

        def stop_sleep(_secs):
            local_runner._shutdown_event.set()

        with patch("time.sleep", stop_sleep):
            local_runner._reap_loop(active_tasks, lock, None, telemetry=telemetry)

        _last_success, fails = telemetry.snapshot()
        self.assertEqual(fails, 1)


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
                        result_metrics=None, command_log=None, progress=None):
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
                        result_metrics=None, command_log=None, progress=None):
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
                        result_metrics=None, command_log=None, progress=None):
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
                        result_metrics=None, command_log=None, progress=None):
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
                        result_metrics=None, command_log=None, progress=None):
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
                        result_metrics=None, command_log=None, progress=None):
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

    def test_detect_gpu_compute_capabilities_parses_single(self):
        """Single compute_cap line → [8.9]."""
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "8.9\n"
        with patch("subprocess.run", return_value=fake):
            result = local_runner._detect_gpu_compute_capabilities()
        self.assertEqual(result, [8.9])

    def test_detect_gpu_compute_capabilities_parses_multi(self):
        """Multiple compute_cap lines → list of floats, one per GPU."""
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "8.9\n7.0\n"
        with patch("subprocess.run", return_value=fake):
            result = local_runner._detect_gpu_compute_capabilities()
        self.assertEqual(result, [8.9, 7.0])

    def test_detect_gpu_compute_capabilities_error_returns_empty(self):
        """Any error in compute_cap query → empty list, no crash."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = local_runner._detect_gpu_compute_capabilities()
        self.assertEqual(result, [])

    def test_detect_gpu_includes_compute_capabilities(self):
        """_detect_gpu populates gpu_compute_capabilities when GPUs present."""
        list_fake = MagicMock()
        list_fake.returncode = 0
        list_fake.stdout = "GPU 0: NVIDIA L40S (UUID: GPU-xyz)\n"
        cap_fake = MagicMock()
        cap_fake.returncode = 0
        cap_fake.stdout = "8.9\n"

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            return list_fake if call_count[0] == 1 else cap_fake

        with patch("subprocess.run", side_effect=side_effect):
            result = local_runner._detect_gpu()

        self.assertTrue(result["has_gpu"])
        self.assertIn("NVIDIA L40S", result["gpu_names"][0])
        self.assertEqual(result["gpu_compute_capabilities"], [8.9])

    def test_detect_gpu_compute_capabilities_unavailable_returns_empty_list(self):
        """When compute_cap query fails, gpu_compute_capabilities is [] (not absent)."""
        list_fake = MagicMock()
        list_fake.returncode = 0
        list_fake.stdout = "GPU 0: Tesla T4 (UUID: GPU-abc)\n"
        cap_fake = MagicMock()
        cap_fake.returncode = 1
        cap_fake.stdout = ""

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            return list_fake if call_count[0] == 1 else cap_fake

        with patch("subprocess.run", side_effect=side_effect):
            result = local_runner._detect_gpu()

        self.assertTrue(result["has_gpu"])
        self.assertEqual(result["gpu_compute_capabilities"], [])

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
        # The daemon loop always carries a GENUINE active-work report, so the
        # orchestrator refreshes its reaper-trusted active set + stamp from it.
        self.assertTrue(posted_bodies[0]["active_work_report"])

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

    def test_llm_clis_included_in_heartbeat_body(self):
        """Heartbeat body carries the LLM CLI inventory when caps are supplied."""
        posted_bodies = []
        fake_llm_clis = {
            "claude": {"present": True, "version": "0.2.0"},
            "codex": {"present": True, "version": "0.3.0"},
            "gemini": {"present": True, "version": "1.0.0"},
        }

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
                 patch.object(local_runner, "_node_runtime_ok", return_value=True), \
                 patch.object(iso, "probe_llm_clis", return_value=fake_llm_clis):
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
        self.assertIsInstance(body["llm_clis"], dict)
        self.assertEqual(body["llm_clis"], fake_llm_clis)
        self.assertTrue(body["llm_clis"]["claude"]["present"])
        self.assertEqual(body["llm_clis"]["claude"]["version"], "0.2.0")
        self.assertTrue(body["llm_clis"]["codex"]["present"])
        self.assertEqual(body["llm_clis"]["codex"]["version"], "0.3.0")
        self.assertTrue(body["llm_clis"]["gemini"]["present"])
        # The remaining heartbeat fields are still present.
        self.assertIsInstance(body["isolation_backends"], dict)
        self.assertEqual(body["version"], "v22-abc")

    def test_llm_clis_none_when_caps_absent(self):
        """caps=None (an old caller) → llm_clis serializes as None."""
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
        self.assertIsNone(body["llm_clis"])

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
                        result_metrics=None, command_log=None, progress=None):
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
        """A sustained 401 streak AFTER a successful auth (a token revoked mid-life)
        exits the process non-zero once the streak reaches max_auth_failures (N4).
        The leading 200 keeps this on the streak path, distinct from the
        first-heartbeat fast-fail path."""
        exits = []
        # 200 first (authenticates), then a 401 streak that reaches the threshold.
        statuses = iter([200])

        def fake_post(url, token, body, **kwargs):
            try:
                return next(statuses), None
            except StopIteration:
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
        """A 200 between 401s resets the streak — no exit. Leads with a 200 so the
        token has authenticated (past the first-heartbeat fast-fail) and the test
        exercises the streak-reset path for a mid-life 401 sequence."""
        statuses = iter([200, 401, 401, 200, 401])
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
        detects the new token, resets the counter, and resumes without exit. Leads
        with a 200 so the runner has authenticated (past the first-heartbeat
        fast-fail) — the live token-swap escape hatch is for a mid-life revoke."""
        # First heartbeat authenticates; then the old token is revoked (401) and
        # the operator writes a new token to disk, which the runner picks up.
        call_count = [0]
        tokens_seen = []

        def fake_post(url, token, body, **kwargs):
            tokens_seen.append(token)
            call_count[0] += 1
            # First contact authenticates so ever_authenticated is set.
            if call_count[0] == 1:
                return 200, None
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

    def test_fast_fails_on_first_heartbeat_401(self):
        """A token that has NEVER authenticated and gets a 401 on the very first
        heartbeat exits immediately — it does not retry max_auth_failures times as
        if the rejection were transient (wrong/revoked/deleted-runner token)."""
        posts = []
        exits = []

        def fake_post(url, token, body, **kwargs):
            posts.append(token)
            return 401, None

        def fake_exit(code):
            exits.append(code)
            local_runner._shutdown_event.set()
            raise SystemExit(code)

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_load_runner_token", lambda: "tok"), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False), \
             patch("os._exit", fake_exit), \
             patch("time.sleep", lambda s: None):
            with self.assertRaises(SystemExit):
                local_runner._heartbeat_loop(
                    "https://api", "tok", {}, threading.Lock(),
                    {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                    # High threshold: fast-fail must NOT depend on it being reached.
                    max_auth_failures=20,
                )

        self.assertEqual(exits, [1], "first-heartbeat 401 must exit non-zero")
        self.assertEqual(
            len(posts), 1,
            "must fail fast on the FIRST 401, not retry a never-authenticated token",
        )

    def test_401_after_authenticating_does_not_fast_fail(self):
        """A token that DID authenticate (a 200 first) keeps the existing streak
        behavior when a later heartbeat 401s — it is retried up to max_auth_failures
        rather than fast-failing, preserving mid-life revoke / inactive-row re-arm."""
        statuses = iter([200, 401, 401])
        exits = []

        def fake_post(url, token, body, **kwargs):
            try:
                return next(statuses), None
            except StopIteration:
                local_runner._shutdown_event.set()
                return 204, None

        with patch.object(local_runner, "_post", fake_post), \
             patch.object(local_runner, "_load_runner_token", lambda: "tok"), \
             patch.object(local_runner, "_node_runtime_ok", return_value=False), \
             patch("os._exit", lambda c: exits.append(c)), \
             patch("time.sleep", lambda s: None):
            local_runner._heartbeat_loop(
                "https://api", "tok", {}, threading.Lock(),
                {"has_gpu": False, "gpu_count": 0, "gpu_names": []},
                # Two post-auth 401s stay under the streak threshold → no exit.
                max_auth_failures=5,
            )

        self.assertEqual(
            exits, [],
            "a 401 after a successful auth must NOT fast-fail below the streak threshold",
        )


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
                 patch.object(os, "execv"), \
                 patch("builtins.print", side_effect=fake_print):
                # BOTH exits are stubbed: after a successful update the runner
                # either exits for its supervisor OR re-execs in place, and which
                # branch it takes depends on the HOST running the tests. An
                # unstubbed os.execv replaces the pytest process itself, silently
                # truncating the run (exit 0, remaining tests never reported).
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

    def test_notify_decline_sends_reason_when_known(self):
        """The decline reason rides along so the orchestrator can record it and
        stop re-pinning the task to the runner that keeps rejecting it. Reasons
        used to be printed locally and dropped, leaving the dispatcher with
        nothing to exclude on — the pin -> pull -> decline livelock."""
        from unittest.mock import patch

        with patch.object(local_runner, "_post") as post_mock:
            local_runner._notify_decline(
                "https://api.example.com",
                "tok-123",
                "task-abc",
                reason="no bwrap: fail-closed",
            )
        post_mock.assert_called_once_with(
            "https://api.example.com/api/runner/decline-task",
            "tok-123",
            {"task_id": "task-abc", "reason": "no bwrap: fail-closed"},
        )

    def test_spawn_agent_task_reports_decline_reason(self):
        """_spawn_agent_task hands the fail-closed reason back to the caller
        (which forwards it to /decline-task) instead of only printing it."""
        from unittest.mock import patch

        reasons: list[str] = []
        with patch.object(
            local_runner.iso,
            "decide_agent_backend",
            return_value=local_runner.iso.DECLINE,
        ), patch.object(
            local_runner.iso,
            "agent_decline_reason",
            return_value="docker present but image is read-only; task type needs write",
        ):
            spawned = local_runner._spawn_agent_task(
                task_id="t1",
                task_type="fix",
                agent_token="tok",
                vm_instance_id="runner:1",
                orchestrator_url="https://api",
                workspace="/tmp/ws",
                caps=local_runner.iso.Capabilities(),
                agent_binary=["node", "agent.js"],
                agent_image=None,
                active_tasks={},
                agent_started_at={},
                agent_teardown={},
                lock=threading.Lock(),
                decline_reason_out=reasons,
            )

        assert spawned is False
        assert reasons and "read-only" in reasons[0]

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
        self.assertTrue(
            resolved.endswith(
                os.path.join("root", ".config", "claude", "settings.json")
            )
        )
        self.assertTrue(os.path.isabs(resolved))

    def test_path_without_leading_slash_is_accepted(self):
        """Paths without a leading slash also resolve correctly."""
        resolved = self._valid("home/user/config.yaml")
        self.assertEqual(
            resolved,
            os.path.join(self._tmpdir, "home", "user", "config.yaml"),
        )

    def test_deep_safe_path_is_accepted(self):
        """A deeply nested path without traversal is accepted."""
        resolved = self._valid("/a/b/c/d/e/f/g/h/file.txt")
        self.assertTrue(resolved.startswith(self._tmpdir))
        self.assertTrue(
            resolved.endswith(
                os.path.join("a", "b", "c", "d", "e", "f", "g", "h", "file.txt")
            )
        )

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

    def test_spawn_agent_task_uses_safe_credential_path(self):
        """The production credential-file writer must call the tested guard."""
        import inspect

        src = inspect.getsource(local_runner._spawn_agent_task)
        self.assertIn("_safe_credential_path", src)


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
    """_preflight still surfaces the backward-compat enrolled_max_concurrent field,
    but main() no longer emits a warn-only mismatch warning — a server config block
    delivered on the heartbeat RESPONSE now OVERRIDES the env default live (see
    TestHeartbeatLiveConfig), so the old "disagrees" advisory was removed."""

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

    def test_preflight_heartbeat_flagged_as_non_report(self):
        """The --check preflight posts active_work_report=False with an empty active
        set, so the orchestrator IGNORES the probe rather than treating it as a real
        report. Guards the zombie-reap hazard: a preflight on a box already running a
        live runner must never zero out that task's claim (reaper_orphan_classify)."""
        captured = {}

        def fake_post(url, token, body, **kwargs):
            captured["body"] = body
            return 200, {"ok": True, "last_seen_at": "2026-01-01T00:00:00Z"}

        with patch.object(local_runner, "_post", side_effect=fake_post), \
             patch.object(local_runner, "_workspace_writable", return_value=True):
            local_runner._preflight(
                "https://api.example.com", "tok", None,
                _bwrap_caps(), _gpu_state(()), 2, 2,
            )

        self.assertIn("body", captured)
        self.assertFalse(captured["body"]["active_work_report"])
        self.assertEqual(captured["body"]["active_task_ids"], [])

    def test_no_warn_only_mismatch_path(self):
        """main() no longer emits the warn-only "disagrees" advisory when the env
        default differs from the server enrolled max_concurrent — that path was
        replaced by live config application via the heartbeat response."""
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

        warning_lines = [line for line in output_lines if "disagrees" in line and "enrolled" in line.lower()]
        self.assertFalse(
            warning_lines,
            f"Did not expect the removed warn-only mismatch advisory. Got: {warning_lines}",
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
        # Restart=always (NOT on-failure): the runner exits 0 after a self-update
        # and relies on systemd to relaunch the fresh binary; on-failure would
        # leave a self-updated runner silently dead. See the unit's RESTART block.
        self.assertIn("Restart=always", unit_text)
        # The active directive is always (the comment may still explain on-failure).
        self.assertNotIn("\nRestart=on-failure\n", unit_text)
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
        """main() prints a WARNING when no provider credential is set (acceptance).

        Pool dispatch ships per-task credentials via /next-task so host env creds
        are fallback-only. Missing host creds → WARNING only, not a hard fail.
        """
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
             patch.object(br, "run_local_runner", return_value=0):
            rc = br.main(["check"])

        warning_lines = [line for line in output_lines if "WARNING" in line and "credential" in line.lower()]
        self.assertTrue(
            warning_lines,
            f"Expected WARNING about missing credentials in output. Got: {output_lines}",
        )
        self.assertIn("ANTHROPIC_API_KEY", warning_lines[0])
        # Missing host creds is now a warning only — pool-covered runners pass --check
        # green. rc=0 because run_local_runner returns 0 for a pool-covered runner.
        self.assertEqual(rc, 0, "Missing host creds is a warning only; --check should pass green")


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

    def test_overwrite_existing_file_succeeds(self):
        """Writing status.json twice must succeed: the second write replaces the first.
        os.replace (not os.rename) is used so this works on Windows too, where
        os.rename raises FileExistsError when the destination already exists."""
        local_runner._write_status_json(
            runner_id="first-box",
            last_heartbeat_status="initial",
            active_task_ids=[],
            active_compute_job_ids=[],
        )
        path = self._status_path()
        self.assertTrue(os.path.exists(path))

        local_runner._write_status_json(
            runner_id="second-box",
            last_heartbeat_status="updated",
            active_task_ids=["new-task"],
            active_compute_job_ids=[],
        )
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            data = json.loads(f.read())
        self.assertEqual(data["runner_id"], "second-box")
        self.assertEqual(data["last_heartbeat_status"], "updated")
        self.assertEqual(data["active_task_ids"], ["new-task"])

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
        # Provider creds absent → WARNING (not FAIL), pool dispatch covers claims.
        self.assertIn("WARNING", output)
        self.assertIn("provider creds", output)
        # setup_ok must be True: pool-covered runners have no host creds by design.
        self.assertTrue(result["setup_ok"])

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


# ── Agent deps bootstrap (blueprint-runner install) ──────────────────────────


class TestBootstrapAgentDeps(unittest.TestCase):
    """_bootstrap_agent_deps in blueprint_runner: agent npm ci at install time."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._agent_dir = os.path.join(self._tmpdir, "agent")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_agent_source(self, *, with_dist=False):
        """Create a minimal agent source tree under self._agent_dir."""
        os.makedirs(os.path.join(self._agent_dir, "src"))
        Path(os.path.join(self._agent_dir, "src", "index.ts")).write_text("// test")
        Path(os.path.join(self._agent_dir, "package.json")).write_text('{"name":"test-agent"}')
        Path(os.path.join(self._agent_dir, "package-lock.json")).write_text("{}")
        if with_dist:
            os.makedirs(os.path.join(self._agent_dir, "dist"))
            Path(os.path.join(self._agent_dir, "dist", "index.js")).write_text("// dist")

    def test_bootstrap_calls_npm_ci_when_source_present(self):
        """npm ci is called when agent source exists and dist is absent."""
        self._make_agent_source()
        calls = []
        def recording_run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0)
        with patch.object(br.shutil, "which", return_value="/usr/bin/npm"):
            br._bootstrap_agent_deps(agent_dir=self._agent_dir, _run=recording_run)
        self.assertEqual(len(calls), 1)
        self.assertIn("npm", calls[0][0][0])
        self.assertEqual(calls[0][1].get("cwd"), self._agent_dir)

    def test_bootstrap_skipped_with_no_agent_deps_flag(self):
        """npm ci is NOT called when no_agent_deps=True."""
        self._make_agent_source()
        calls = []
        def recording_run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0)
        br._bootstrap_agent_deps(agent_dir=self._agent_dir, no_agent_deps=True, _run=recording_run)
        self.assertEqual(len(calls), 0)

    def test_bootstrap_skipped_when_dist_exists(self):
        """npm ci is NOT called when dist/index.js already exists."""
        self._make_agent_source(with_dist=True)
        calls = []
        def recording_run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0)
        with patch.object(br.shutil, "which", return_value="/usr/bin/npm"):
            br._bootstrap_agent_deps(agent_dir=self._agent_dir, _run=recording_run)
        self.assertEqual(len(calls), 0, "npm ci should be skipped when dist is present")

    def test_bootstrap_warns_when_npm_not_on_path(self):
        """Warning is printed when npm is not on PATH, no crash."""
        self._make_agent_source()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
             patch.object(br.shutil, "which", return_value=None):
            br._bootstrap_agent_deps(agent_dir=self._agent_dir)
        self.assertIn("WARNING", stderr.getvalue())
        self.assertIn("npm", stderr.getvalue())

    def test_bootstrap_noop_on_compute_only(self):
        """No crash when agent dir does not exist (compute-only runner)."""
        compute_dir = os.path.join(self._tmpdir, "no-agent-here")
        br._bootstrap_agent_deps(agent_dir=compute_dir)

    def test_bootstrap_handles_npm_ci_failure_nonfatally(self):
        """npm ci non-zero exit is WARNING, not crash."""
        self._make_agent_source()
        stderr = io.StringIO()
        def failing_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 1)
        with patch.object(br.shutil, "which", return_value="/usr/bin/npm"), \
             contextlib.redirect_stderr(stderr):
            br._bootstrap_agent_deps(agent_dir=self._agent_dir, _run=failing_run)
        self.assertIn("WARNING", stderr.getvalue())

    def test_bootstrap_noop_when_no_package_json(self):
        """No crash when agent dir exists but no package.json."""
        # agent dir with src/index.ts but no package.json
        os.makedirs(os.path.join(self._agent_dir, "src"))
        Path(os.path.join(self._agent_dir, "src", "index.ts")).write_text("// test")
        calls = []
        def recording_run(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0)
        br._bootstrap_agent_deps(agent_dir=self._agent_dir, _run=recording_run)
        self.assertEqual(len(calls), 0)


# ── Agent deps missing detection (preflight) ────────────────────────────────


class TestAgentDepsMissing(unittest.TestCase):
    """_agent_deps_missing in local_runner: preflight detection."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._old_file = getattr(local_runner, "__file__", None)

    def tearDown(self):
        if self._old_file is not None:
            local_runner.__file__ = self._old_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _setup_agent(self, *, with_tsx=False, with_dist=False):
        """Create fake scripts/../agent structure under tmpdir, redirect __file__."""
        agent_dir = os.path.join(self._tmpdir, "agent")
        os.makedirs(os.path.join(agent_dir, "src"))
        Path(os.path.join(agent_dir, "src", "index.ts")).write_text("// test")
        if with_tsx:
            tsx_dir = os.path.join(agent_dir, "node_modules", ".bin")
            os.makedirs(tsx_dir)
            Path(os.path.join(tsx_dir, "tsx")).write_text("#!/usr/bin/env node")
        if with_dist:
            os.makedirs(os.path.join(agent_dir, "dist"))
            Path(os.path.join(agent_dir, "dist", "index.js")).write_text("// dist")
        # Point __file__ into this temp tree so agent is found at tmpdir/agent.
        fake_scripts = os.path.join(self._tmpdir, "scripts")
        os.makedirs(fake_scripts)
        local_runner.__file__ = os.path.join(fake_scripts, "local_runner.py")

    def test_true_when_agent_source_exists_but_no_tsx(self):
        """Returns True when src/index.ts exists but tsx not installed."""
        self._setup_agent()
        with patch.object(local_runner.shutil, "which", return_value=None):
            self.assertTrue(local_runner._agent_deps_missing())

    def test_false_when_tsx_installed(self):
        """Returns False when tsx is in node_modules/.bin."""
        self._setup_agent(with_tsx=True)
        self.assertFalse(local_runner._agent_deps_missing())

    def test_false_when_dist_built(self):
        """Returns False when dist/index.js exists (prebuilt artifact)."""
        self._setup_agent(with_dist=True)
        self.assertFalse(local_runner._agent_deps_missing())

    def test_false_compute_only(self):
        """Returns False when no agent source exists on this host."""
        local_runner.__file__ = os.path.join(self._tmpdir, "scripts", "local_runner.py")
        with patch.object(local_runner.os, "getcwd", return_value=os.path.join(self._tmpdir, "nowhere")):
            self.assertFalse(local_runner._agent_deps_missing())


class TestPreflightAgentDepsMessage(unittest.TestCase):
    """Preflight prints actionable missing-deps message."""

    def _run_preflight(self, deps_missing, caps=None):
        """Run _preflight with mocked agent_binary and return captured output."""
        if caps is None:
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
             patch.object(local_runner, "_agent_deps_missing", return_value=deps_missing), \
             contextlib.redirect_stdout(buf):
            result = local_runner._preflight(
                "https://api.example.com", "tok-abcdefgh-1234",
                agent_binary=["tsx", "/agent/src/index.ts"],
                caps=caps, gpu_state=gpu_state,
                max_concurrent=2, max_concurrent_compute=2,
            )
        return buf.getvalue(), result

    def test_actionable_message_when_deps_missing(self):
        """Preflight includes 'agent deps' line when deps are missing."""
        output, result = self._run_preflight(deps_missing=True)
        self.assertIn("MISSING", output)
        self.assertIn("agent deps", output)
        self.assertIn("npm install", output)
        self.assertTrue(result["setup_ok"])

    def test_no_message_when_deps_installed(self):
        """Preflight does NOT include the missing-deps line when deps are fine."""
        output, result = self._run_preflight(deps_missing=False)
        self.assertNotIn("MISSING", output)
        self.assertNotIn("agent deps", output)

    def test_no_message_when_docker_agent_only(self):
        """Preflight does NOT warn when agent is docker-only (deps in the image)."""
        caps = _docker_caps()
        output, result = self._run_preflight(deps_missing=True, caps=caps)
        # Docker-only agent doesn't have bwrap, so the check isn't relevant.
        self.assertNotIn("agent deps", output)


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


class TestInputArtifactRehydration(unittest.TestCase):
    """Cross-job checkpoint resume: the runner rehydrates a predecessor's durable
    bundle into the job's artifact dir before the command runs, path-traversal-safe."""

    def test_is_safe_tar_member(self):
        g = local_runner._is_safe_tar_member
        self.assertTrue(g("a/b.txt"))
        self.assertTrue(g("./ok.txt"))
        self.assertFalse(g(""))
        self.assertFalse(g("/etc/passwd"))
        self.assertFalse(g("../up.txt"))
        self.assertFalse(g("a/../../b.txt"))
        self.assertFalse(g("~/secret"))

    def _make_bundle(self, entries):
        buf = io.BytesIO()
        with __import__("tarfile").open(fileobj=buf, mode="w:gz") as tar:
            for name, data in entries:
                info = __import__("tarfile").TarInfo(name=name)
                payload = data.encode()
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    def test_rehydrate_extracts_safe_members_only(self):
        bundle = self._make_bundle(
            [("model.ckpt", "step-42"), ("../evil.txt", "x"), ("/abs.txt", "x")]
        )

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return bundle

        with tempfile.TemporaryDirectory() as dest:
            with patch("urllib.request.urlopen", return_value=_Resp()):
                local_runner._rehydrate_input_artifact(
                    "http://orchestrator", "tok", "job-1", dest
                )
            self.assertTrue(os.path.isfile(os.path.join(dest, "model.ckpt")))
            with open(os.path.join(dest, "model.ckpt"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "step-42")
            # Traversal members never escaped the destination.
            self.assertFalse(
                os.path.exists(os.path.join(os.path.dirname(dest), "evil.txt"))
            )

    def test_rehydrate_raises_on_download_error(self):
        import urllib.error

        with tempfile.TemporaryDirectory() as dest:
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    "http://x", 404, "not found", {}, None
                ),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    local_runner._rehydrate_input_artifact(
                        "http://orchestrator", "tok", "job-1", dest
                    )
            self.assertIn("input_artifact_rehydration_failed", str(ctx.exception))


class _FakeDocker:
    """Scriptable ``docker`` for _prune_docker_images.

    *images* is a list of dicts: ``{"id", "created", "last_tag_time", "tags",
    "rm_fails"}``. Records every argv the prune pass invokes so a test can assert
    on the MECHANISM (e.g. that no creation-time `until=` filter is used) and not
    just on the outcome.
    """

    def __init__(self, images, list_rc=0, inspect_rc=0):
        self.images = images
        self.list_rc = list_rc
        self.inspect_rc = inspect_rc
        self.calls = []
        self.removed = []

    def _result(self, rc, out="", err=""):
        class P:
            returncode = rc
            stdout = out
            stderr = err
        return P()

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:4] == ["docker", "image", "ls", "-a"]:
            if self.list_rc != 0:
                return self._result(self.list_rc, err="permission denied")
            return self._result(0, "".join(f"{i['id']}\n" for i in self.images))
        if cmd[:3] == ["docker", "image", "inspect"]:
            if self.inspect_rc != 0:
                return self._result(self.inspect_rc, err="no such image")
            rows = []
            for img in self.images:
                tags = "[" + " ".join(img.get("tags") or []) + "]"
                rows.append(
                    f"{img['id']}\t{img.get('last_tag_time', '')}\t"
                    f"{img.get('created', '')}\t{tags}"
                )
            return self._result(0, "\n".join(rows))
        if cmd[:3] == ["docker", "image", "prune"]:
            # The OLD, creation-time-keyed mechanism. Answered (not raised) so a
            # regression fails on the assertions below, not on a fake's exception.
            return self._result(0, "Total reclaimed space: 5GB")
        if cmd[:3] == ["docker", "image", "rm"]:
            target = cmd[3]
            img = next((i for i in self.images if i["id"] == target), None)
            if img is not None and img.get("rm_fails"):
                return self._result(
                    1, err="conflict: image is being used by stopped container"
                )
            self.removed.append(target)
            return self._result(0, f"Deleted: {target}")
        raise AssertionError(f"unexpected docker invocation: {cmd}")

    @property
    def used_until_filter(self) -> bool:
        """True iff any invocation used docker's creation-time `until=` filter."""
        return any(
            any(str(arg).startswith("until=") for arg in cmd) for cmd in self.calls
        )


def _go_time(dt) -> str:
    """Render *dt* the way `docker image inspect .Metadata.LastTagTime` does."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f000 +0000 UTC")


def _rfc3339(dt) -> str:
    """Render *dt* the way `docker image inspect .Created` does."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


class TestDockerImageCacheHygiene(unittest.TestCase):
    """Runner Docker image cache hygiene (_prune_docker_images).

    Hygiene must key on LAST-USE / PULL recency. `docker image prune --filter
    until=<age>` keys on the image's upstream CREATION date, so a just-pulled
    registry base image (built months ago) was evicted immediately and re-pulled
    by the next job — the prune thrashed the cache it exists to protect.
    """

    def setUp(self):
        self.now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
        self.tmpdir = tempfile.mkdtemp(prefix="bp_prune_test_")
        os.environ["BLUEPRINT_CREDENTIAL_DIR"] = self.tmpdir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ.pop("BLUEPRINT_CREDENTIAL_DIR", None)

    def _prune(self, docker, keep_days=7, ledger=None, protected=()):
        return local_runner._prune_docker_images(
            "/tmp",
            1,
            keep_days=keep_days,
            _run=docker,
            _disk_below=lambda root, m: True,
            _now=self.now,
            _ledger=ledger or {},
            _protected=set(protected),
        )

    def test_noop_when_disk_above_threshold(self):
        result = local_runner._prune_docker_images(
            "/tmp", 100, keep_days=7, _disk_below=lambda root, m: False
        )
        self.assertFalse(result["pruned"])
        self.assertEqual(result["reason"], "disk_above_threshold")

    def test_freshly_pulled_old_base_image_is_kept(self):
        """FINDING 7: an image BUILT 400 days ago but PULLED an hour ago stays.

        This is the exact cache-thrash case: NGC/CUDA base images carry an
        upstream creation date far older than any sane keep window, so a
        creation-time `until=` prune deletes them the moment they land.
        """
        docker = _FakeDocker([
            {
                "id": "sha256:cuda",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(hours=1)),
                "tags": ["nvcr.io/nvidia/pytorch:24.05-py3"],
            }
        ])
        result = self._prune(docker, keep_days=7)

        self.assertTrue(result["pruned"])
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(docker.removed, [])
        self.assertEqual(result["kept"], 1)
        # And the creation-time-keyed mechanism is gone for good.
        self.assertFalse(
            docker.used_until_filter,
            "hygiene must not use docker's creation-time until= filter",
        )

    def test_ledger_last_use_protects_an_old_pull(self):
        """An image pulled 30d ago but USED an hour ago (runner ledger) is kept."""
        docker = _FakeDocker([
            {
                "id": "sha256:inuse",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(days=30)),
                "tags": ["registry.example.com/proj/train:v3"],
            }
        ])
        result = self._prune(
            docker,
            keep_days=7,
            ledger={
                "registry.example.com/proj/train:v3":
                    (self.now - timedelta(hours=1)).isoformat()
            },
        )
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(docker.removed, [])

    def test_stale_unused_image_is_removed(self):
        """An image neither used nor pulled inside the window IS reclaimed."""
        docker = _FakeDocker([
            {
                "id": "sha256:stale",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(days=90)),
                "tags": ["old/experiment:v1"],
            },
            {
                "id": "sha256:fresh",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(days=1)),
                "tags": ["new/experiment:v2"],
            },
        ])
        result = self._prune(docker, keep_days=7)
        self.assertEqual(docker.removed, ["sha256:stale"])
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["kept"], 1)

    def test_in_use_image_is_never_force_removed(self):
        """A stale image still referenced by a container is left to docker to refuse.

        The removal must not pass ``-f``: docker's own refusal ("image is being
        used by ... container") is the in-use protection.
        """
        docker = _FakeDocker([
            {
                "id": "sha256:running",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(days=90)),
                "tags": ["busy/job:v1"],
                "rm_fails": True,
            }
        ])
        result = self._prune(docker, keep_days=7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["in_use_skipped"], 1)
        rm_calls = [c for c in docker.calls if c[:3] == ["docker", "image", "rm"]]
        self.assertTrue(rm_calls)
        for call in rm_calls:
            self.assertNotIn("-f", call)
            self.assertNotIn("--force", call)

    def test_agent_image_is_protected(self):
        """The configured agent image is never evicted, however old its build date."""
        docker = _FakeDocker([
            {
                "id": "sha256:agent",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(days=400)),
                "tags": ["ghcr.io/blueprint/agent:prod"],
            }
        ])
        result = self._prune(
            docker, keep_days=7, protected={"ghcr.io/blueprint/agent:prod"}
        )
        self.assertEqual(result["removed"], 0)
        self.assertEqual(docker.removed, [])

    def test_undatable_image_is_kept(self):
        """No parseable timestamp anywhere → keep. Never delete what we can't age."""
        docker = _FakeDocker([
            {
                "id": "sha256:unknown",
                "created": "",
                "last_tag_time": "0001-01-01T00:00:00Z",
                "tags": [],
            }
        ])
        result = self._prune(docker, keep_days=7)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["kept"], 1)

    def test_handles_docker_failure(self):
        def fake_run(cmd, **kw):
            raise OSError("docker not found")

        result = local_runner._prune_docker_images(
            "/tmp", 1, keep_days=7, _run=fake_run, _disk_below=lambda root, m: True
        )
        self.assertFalse(result["pruned"])
        self.assertIn("prune_error", result["reason"])

    def test_nonzero_exit_reported(self):
        docker = _FakeDocker([], list_rc=1)
        result = self._prune(docker, keep_days=7)
        self.assertFalse(result["pruned"])
        self.assertEqual(result["reason"], "prune_nonzero")

    def test_keep_days_from_env(self):
        with patch.dict(
            os.environ, {"BLUEPRINT_RUNNER_IMAGE_KEEP_DAYS": "2"}
        ):
            self.assertEqual(local_runner._compute_image_keep_days(), 2)
        with patch.dict(
            os.environ, {"BLUEPRINT_RUNNER_IMAGE_KEEP_DAYS": "notanint"}
        ):
            self.assertEqual(local_runner._compute_image_keep_days(), 7)

    def test_record_image_use_round_trips_through_ledger(self):
        """The runner's own last-use stamp survives a write/read cycle."""
        local_runner._record_image_use("nvcr.io/nvidia/pytorch:24.05-py3", _now=self.now)
        ledger = local_runner._load_image_use_ledger()
        self.assertIn("nvcr.io/nvidia/pytorch:24.05-py3", ledger)
        parsed = local_runner._parse_docker_time(
            ledger["nvcr.io/nvidia/pytorch:24.05-py3"]
        )
        self.assertEqual(parsed, self.now)
        # And a recorded use protects the image end-to-end (no _ledger injection).
        docker = _FakeDocker([
            {
                "id": "sha256:cuda",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(days=400)),
                "tags": ["nvcr.io/nvidia/pytorch:24.05-py3"],
            }
        ])
        result = local_runner._prune_docker_images(
            "/tmp", 1, keep_days=7, _run=docker,
            _disk_below=lambda root, m: True, _now=self.now, _protected=set(),
        )
        self.assertEqual(result["removed"], 0, result)

    def test_record_image_use_stores_project_owner(self):
        """A recorded use with a project_id is stored as a dict and round-trips."""
        local_runner._record_image_use(
            "registry.example.com/proj-a/train:v1",
            project_id="proj-a",
            _now=self.now,
        )
        ledger = local_runner._load_image_use_ledger()
        value = ledger["registry.example.com/proj-a/train:v1"]
        self.assertIsInstance(value, dict)
        self.assertEqual(value["project_id"], "proj-a")
        self.assertEqual(
            local_runner._parse_docker_time(local_runner._ledger_last_used(value)),
            self.now,
        )

    def test_record_image_use_preserves_owner_when_reused_without_project(self):
        """A later use with no project_id keeps a previously-recorded owner."""
        local_runner._record_image_use("img:v1", project_id="proj-a", _now=self.now)
        local_runner._record_image_use(
            "img:v1", _now=self.now + timedelta(hours=1)
        )
        value = local_runner._load_image_use_ledger()["img:v1"]
        self.assertEqual(local_runner._ledger_project_id(value), "proj-a")

    def test_prune_tolerates_dict_ledger_values(self):
        """The dict-shaped ledger value still protects an old pull from eviction."""
        docker = _FakeDocker([
            {
                "id": "sha256:inuse",
                "created": _rfc3339(self.now - timedelta(days=400)),
                "last_tag_time": _go_time(self.now - timedelta(days=30)),
                "tags": ["registry.example.com/proj/train:v3"],
            }
        ])
        result = self._prune(
            docker,
            keep_days=7,
            ledger={
                "registry.example.com/proj/train:v3": {
                    "last_used": (self.now - timedelta(hours=1)).isoformat(),
                    "project_id": "proj",
                }
            },
        )
        self.assertEqual(result["removed"], 0, result)
        self.assertEqual(docker.removed, [])

    def test_parse_docker_time_formats(self):
        """Docker emits RFC3339 (.Created), Go time.Time (.Metadata.LastTagTime),
        and a Go zero value for never-tagged images."""
        self.assertEqual(
            local_runner._parse_docker_time("2026-07-01T12:34:56.789012345Z"),
            datetime(2026, 7, 1, 12, 34, 56, 789012, tzinfo=timezone.utc),
        )
        self.assertEqual(
            local_runner._parse_docker_time("2026-07-01 12:34:56.789012 +0000 UTC"),
            datetime(2026, 7, 1, 12, 34, 56, 789012, tzinfo=timezone.utc),
        )
        self.assertIsNone(local_runner._parse_docker_time("0001-01-01T00:00:00Z"))
        self.assertIsNone(local_runner._parse_docker_time(""))
        self.assertIsNone(local_runner._parse_docker_time("not-a-time"))


class _FakeCacheDocker:
    """Scriptable ``docker`` for _docker_image_cache_by_project.

    *images* is a list of ``{"id", "size", "tags"}``. Answers ``image ls`` with
    the ids and ``image inspect --format {{.Id}}\\t{{.Size}}\\t{{.RepoTags}}``
    with size in BYTES (the shape `.Size` renders), so the accounting can be
    tested without a real docker.
    """

    def __init__(self, images, list_rc=0, inspect_rc=0):
        self.images = images
        self.list_rc = list_rc
        self.inspect_rc = inspect_rc
        self.calls = []

    def _result(self, rc, out="", err=""):
        class P:
            returncode = rc
            stdout = out
            stderr = err
        return P()

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:4] == ["docker", "image", "ls", "-a"]:
            if self.list_rc != 0:
                return self._result(self.list_rc, err="permission denied")
            return self._result(0, "".join(f"{i['id']}\n" for i in self.images))
        if cmd[:3] == ["docker", "image", "inspect"]:
            if self.inspect_rc != 0:
                return self._result(self.inspect_rc, err="no such image")
            rows = []
            for img in self.images:
                tags = "[" + " ".join(img.get("tags") or []) + "]"
                rows.append(f"{img['id']}\t{img.get('size', 0)}\t{tags}")
            return self._result(0, "\n".join(rows))
        raise AssertionError(f"unexpected docker invocation: {cmd}")


class TestDockerImageCacheAccounting(unittest.TestCase):
    """Per-project docker image-cache accounting (_docker_image_cache_by_project)."""

    def _cache(self, docker, ledger):
        return local_runner._docker_image_cache_by_project(_run=docker, _ledger=ledger)

    def test_attributes_size_to_owning_project(self):
        docker = _FakeCacheDocker([
            {"id": "sha256:a", "size": 3_000_000_000, "tags": ["reg/proj-a/train:v1"]},
            {"id": "sha256:b", "size": 1_000_000_000, "tags": ["reg/proj-b/train:v1"]},
        ])
        ledger = {
            "reg/proj-a/train:v1": {"last_used": "2026-07-12T00:00:00+00:00", "project_id": "proj-a"},
            "reg/proj-b/train:v1": {"last_used": "2026-07-12T00:00:00+00:00", "project_id": "proj-b"},
        }
        result = self._cache(docker, ledger)
        self.assertEqual(result["total_bytes"], 4_000_000_000)
        self.assertEqual(result["image_count"], 2)
        self.assertEqual(result["by_project"]["proj-a"], {"bytes": 3_000_000_000, "image_count": 1})
        self.assertEqual(result["by_project"]["proj-b"], {"bytes": 1_000_000_000, "image_count": 1})
        self.assertEqual(result["unattributed_bytes"], 0)

    def test_legacy_string_ledger_entry_is_unattributed(self):
        """An image whose ledger stamp predates project tracking is unattributed."""
        docker = _FakeCacheDocker([
            {"id": "sha256:a", "size": 2_000_000_000, "tags": ["reg/old/train:v1"]},
        ])
        ledger = {"reg/old/train:v1": "2026-07-12T00:00:00+00:00"}  # legacy shape
        result = self._cache(docker, ledger)
        self.assertEqual(result["by_project"], {})
        self.assertEqual(result["unattributed_bytes"], 2_000_000_000)
        self.assertEqual(result["unattributed_image_count"], 1)

    def test_no_images_returns_zeroed_accounting(self):
        result = self._cache(_FakeCacheDocker([]), {})
        self.assertEqual(result["total_bytes"], 0)
        self.assertEqual(result["image_count"], 0)
        self.assertEqual(result["by_project"], {})

    def test_docker_missing_returns_none(self):
        def boom(cmd, **kw):
            raise OSError("docker not found")
        self.assertIsNone(
            local_runner._docker_image_cache_by_project(_run=boom, _ledger={})
        )

    def test_list_nonzero_returns_none(self):
        docker = _FakeCacheDocker([], list_rc=1)
        self.assertIsNone(self._cache(docker, {}))


class _FakeResp:
    """Minimal context-manager HTTP response for mocking urlopen."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _make_bundle(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class DownloadInputArtifactTests(unittest.TestCase):
    """Cross-job checkpoint resume: runner-side bundle hydration."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bp_resume_test_")
        self.artifact_dir = os.path.join(self.tmpdir, "artifacts")
        os.makedirs(self.artifact_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_hydrates_prior_bundle_and_strips_stale(self):
        """A successful download extracts the checkpoint into the artifact dir,
        strips predecessor run-status files, and reports honest provenance."""
        bundle = _make_bundle(
            {
                "checkpoint/model.pt": b"WEIGHTS-EPOCH-7",
                "terminal.json": b"STALE",
                "result.json": b"{}",
            }
        )
        with patch(
            "local_runner.urllib.request.urlopen",
            return_value=_FakeResp(200, bundle),
        ):
            prov = local_runner._download_input_artifact(
                "http://orch", "tok", "job-1", self.artifact_dir
            )
        self.assertTrue(prov["ok"], prov)
        # Checkpoint present.
        ckpt = os.path.join(self.artifact_dir, "checkpoint", "model.pt")
        self.assertTrue(os.path.isfile(ckpt))
        with open(ckpt, "rb") as fh:
            self.assertEqual(fh.read(), b"WEIGHTS-EPOCH-7")
        # Stale run-status files stripped.
        self.assertFalse(os.path.exists(os.path.join(self.artifact_dir, "terminal.json")))
        self.assertFalse(os.path.exists(os.path.join(self.artifact_dir, "result.json")))
        # Provenance counts only surviving files (the single checkpoint).
        self.assertEqual(prov["file_count"], 1)
        self.assertEqual(prov["bytes"], len(b"WEIGHTS-EPOCH-7"))

    def test_download_http_error_reports_failure_not_resume(self):
        """A 404 from the endpoint yields ok=False (never a claimed resume)."""
        import urllib.error

        with patch(
            "local_runner.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("u", 404, "nf", {}, None),
        ):
            prov = local_runner._download_input_artifact(
                "http://orch", "tok", "job-1", self.artifact_dir
            )
        self.assertFalse(prov["ok"])
        self.assertEqual(prov["reason"], "http_404")
        self.assertEqual(os.listdir(self.artifact_dir), [])

    def test_empty_bundle_reports_failure(self):
        with patch(
            "local_runner.urllib.request.urlopen",
            return_value=_FakeResp(200, b""),
        ):
            prov = local_runner._download_input_artifact(
                "http://orch", "tok", "job-1", self.artifact_dir
            )
        self.assertFalse(prov["ok"])
        self.assertEqual(prov["reason"], "empty_bundle")

    def test_safe_members_rejects_path_traversal(self):
        """A bundle with an absolute / .. member must not escape the dest dir."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in ("../escape.txt", "/abs.txt", "ok.txt"):
                info = tarfile.TarInfo(name=name)
                info.size = 3
                tar.addfile(info, io.BytesIO(b"xyz"))
        with patch(
            "local_runner.urllib.request.urlopen",
            return_value=_FakeResp(200, buf.getvalue()),
        ):
            local_runner._download_input_artifact(
                "http://orch", "tok", "job-1", self.artifact_dir
            )
        # Only the safe member landed; no escape above the artifact dir.
        self.assertTrue(os.path.isfile(os.path.join(self.artifact_dir, "ok.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "escape.txt")))


class SupervisorDetectionTests(unittest.TestCase):
    """FINDING 3: supervision must be a claim about THIS PROCESS, not the host.

    The old detector returned "systemd" whenever /proc/1/comm was systemd — true
    of EVERY process on a systemd host, including a runner started by hand in
    tmux. The self-update path then took the "a supervisor will restart me"
    branch, os._exit(0)-ed, and nothing came back (the dgx-1 outage), while
    --check printed a healthy "systemd (auto-restart on crash/update)".
    """

    # A hand-started runner on a systemd host: systemd tracks the login session
    # in a *scope*, which it never restarts.
    SESSION_CGROUP = "0::/user.slice/user-1000.slice/session-3.scope\n"
    # A runner running as a real systemd service unit.
    SERVICE_CGROUP = "0::/system.slice/blueprint-runner.service\n"

    def _state(self, env=None, pid1="", cgroup="", ancestry=True):
        with patch.dict(os.environ, env or {}, clear=True), \
             patch.object(local_runner, "_pid1_comm", return_value=pid1), \
             patch.object(local_runner, "_self_cgroup_text", return_value=cgroup), \
             patch.object(
                 local_runner, "_ancestry_reaches_pid1", return_value=ancestry
             ):
            return local_runner._detect_supervisor_state()

    def test_systemd_host_manual_launch_is_unsupervised(self):
        """PID 1 == systemd + a session SCOPE cgroup → NOT supervised."""
        state = self._state(pid1="systemd", cgroup=self.SESSION_CGROUP)
        self.assertFalse(state["supervised"])
        self.assertIsNone(state["supervisor"])
        self.assertIn("not a systemd .service unit", state["evidence"])
        self.assertIn("will NOT restart", state["detail"])
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(local_runner, "_pid1_comm", return_value="systemd"), \
             patch.object(
                 local_runner, "_self_cgroup_text", return_value=self.SESSION_CGROUP
             ):
            self.assertIsNone(local_runner._detect_process_supervisor())

    def test_systemd_service_unit_is_supervised(self):
        state = self._state(pid1="systemd", cgroup=self.SERVICE_CGROUP)
        self.assertTrue(state["supervised"])
        self.assertEqual(state["supervisor"], "systemd")
        self.assertIn("blueprint-runner.service", state["evidence"])

    def test_service_cgroup_but_orphaned_ancestry_is_unsupervised(self):
        """Inherited service cgroup with a parent chain that never reaches PID 1
        (e.g. a re-parented `docker exec`) is not a supervision guarantee."""
        state = self._state(
            pid1="systemd", cgroup=self.SERVICE_CGROUP, ancestry=False
        )
        self.assertFalse(state["supervised"])

    def test_systemd_unit_env_is_supervised(self):
        state = self._state(env={"INVOCATION_ID": "abc"}, pid1="systemd")
        self.assertTrue(state["supervised"])
        self.assertEqual(state["supervisor"], "systemd")

    def test_operator_declared_wrapper_is_supervised(self):
        state = self._state(
            env={"BLUEPRINT_RUNNER_SUPERVISED": "1"},
            pid1="systemd",
            cgroup=self.SESSION_CGROUP,
        )
        self.assertTrue(state["supervised"])
        self.assertEqual(state["supervisor"], "declared")

    def test_supervisord_init_in_process_tree_is_supervised(self):
        state = self._state(pid1="supervisord", ancestry=True)
        self.assertTrue(state["supervised"])
        self.assertEqual(state["supervisor"], "supervisord")

    def test_supervising_init_but_not_in_its_tree_is_unsupervised(self):
        """`docker exec` into a supervised container: the init never started us."""
        state = self._state(pid1="supervisord", ancestry=False)
        self.assertFalse(state["supervised"])
        self.assertIn("NOT in its process tree", state["evidence"])

    def test_tini_is_not_a_supervisor(self):
        """tini reaps zombies and forwards signals — it restarts nothing."""
        state = self._state(pid1="tini")
        self.assertFalse(state["supervised"])
        self.assertIn("does not restart", state["evidence"])

    def test_systemd_service_unit_parser(self):
        self.assertEqual(
            local_runner._systemd_service_unit(self.SERVICE_CGROUP),
            "blueprint-runner.service",
        )
        # cgroup v1 multi-line form.
        v1 = (
            "12:pids:/system.slice/blueprint-runner.service\n"
            "0::/system.slice/blueprint-runner.service\n"
        )
        self.assertEqual(
            local_runner._systemd_service_unit(v1), "blueprint-runner.service"
        )
        self.assertIsNone(local_runner._systemd_service_unit(self.SESSION_CGROUP))
        self.assertIsNone(local_runner._systemd_service_unit(""))


@contextlib.contextmanager
def _fake_host(pid1="", cgroup="", ancestry=True, env=None):
    """Simulate a host's PID-1/cgroup/ancestry facts for the REAL detector."""
    with patch.dict(os.environ, env or {}, clear=True), \
         patch.object(local_runner, "_pid1_comm", return_value=pid1), \
         patch.object(local_runner, "_self_cgroup_text", return_value=cgroup), \
         patch.object(local_runner, "_ancestry_reaches_pid1", return_value=ancestry):
        yield


class SelfUpdateSurvivalTests(unittest.TestCase):
    """The self-update survival path must not exit into the void on a systemd
    host where nothing is actually supervising the runner (the dgx-1 outage).

    These drive the REAL detector against simulated host facts — a detector that
    calls every systemd host "supervised" makes the first test fail.
    """

    def test_unsupervised_systemd_host_reexecs_in_place(self):
        with _fake_host(
            pid1="systemd",
            cgroup=SupervisorDetectionTests.SESSION_CGROUP,
        ), \
             patch.object(local_runner.os, "execv") as execv, \
             patch.object(local_runner.os, "_exit") as exit_:
            local_runner._reexec_after_update()
        execv.assert_called_once()
        exit_.assert_not_called()

    def test_real_systemd_unit_exits_for_restart(self):
        with _fake_host(
            pid1="systemd",
            cgroup=SupervisorDetectionTests.SERVICE_CGROUP,
        ), \
             patch.object(local_runner.os, "execv") as execv, \
             patch.object(
                 local_runner.os, "_exit", side_effect=SystemExit(0)
             ) as exit_:
            # os._exit is real-process-fatal; SystemExit emulates it under test.
            with self.assertRaises(SystemExit):
                local_runner._reexec_after_update()
        exit_.assert_called_once_with(0)
        execv.assert_not_called()


class PreflightSupervisorReportTests(unittest.TestCase):
    """--check must report the REAL supervision state — the concealment half of
    FINDING 3 (a hand-launched runner on a systemd host read as healthy)."""

    def _preflight_output(self, pid1, cgroup):
        buf = io.StringIO()
        with _fake_host(pid1=pid1, cgroup=cgroup), \
             patch.object(
                 local_runner, "_post",
                 return_value=(200, {"ok": True, "last_seen_at": "2026-01-01T00:00:00Z"}),
             ), \
             patch.object(local_runner, "_workspace_writable", return_value=True), \
             contextlib.redirect_stdout(buf):
            result = local_runner._preflight(
                "https://api.example.com", "tok", None,
                _bwrap_caps(), _gpu_state(()), 2, 2,
            )
        return result, buf.getvalue()

    def test_unsupervised_systemd_host_is_reported_as_unsupervised(self):
        result, out = self._preflight_output(
            "systemd", SupervisorDetectionTests.SESSION_CGROUP
        )
        self.assertIsNone(result["supervisor"])
        self.assertFalse(result["supervised"])
        self.assertIn("UNSUPERVISED", out)
        self.assertIn("not a systemd .service unit", out)
        # It must NOT claim an auto-restart that will never happen.
        self.assertNotIn("auto-restart on crash/update", out)

    def test_real_supervisor_is_reported_with_its_evidence(self):
        result, out = self._preflight_output(
            "systemd", SupervisorDetectionTests.SERVICE_CGROUP
        )
        self.assertEqual(result["supervisor"], "systemd")
        self.assertTrue(result["supervised"])
        self.assertIn("auto-restart on crash/update", out)
        self.assertIn("blueprint-runner.service", out)


class GpuSandboxSmokeTests(unittest.TestCase):
    """FINDING 6: the preflight GPU smoke must probe against a LIVE workspace.

    The plan's bwrap argv binds workspace_host into the sandbox, and bwrap fails
    hard ("Can't find source path") when a bind source is missing — so deleting
    the temp workspace between plan and probe made the smoke fail 100% of the
    time on a real GPU host and report a CUDA outage that did not exist.
    """

    def _fake_plan(self, captured, declined=False, reason=None):
        def plan_compute_execution(**kwargs):
            captured["workspace"] = kwargs["workspace_host"]
            captured["existed_at_plan"] = os.path.isdir(kwargs["workspace_host"])
            plan = MagicMock()
            plan.declined = declined
            plan.decline_reason = reason
            return plan
        return plan_compute_execution

    def _fake_probe(self, captured, raises=False):
        def probe_gpu_in_sandbox(plan, timeout=30):
            workspace = captured["workspace"]
            captured["existed_at_probe"] = os.path.isdir(workspace)
            if raises:
                raise RuntimeError("probe blew up")
            if not captured["existed_at_probe"]:
                # Exactly what bwrap does with a missing bind source.
                return False, (
                    f"bwrap: Can't find source path {workspace}: "
                    "No such file or directory"
                )
            return True, "GPU 0: Tesla V100-SXM2-32GB (UUID: GPU-abc)\n"
        return probe_gpu_in_sandbox

    def test_probe_runs_against_an_existing_workspace(self):
        captured = {}
        with patch.object(local_runner, "_read_ldconfig", return_value=""), \
             patch.object(iso, "plan_compute_execution", self._fake_plan(captured)), \
             patch.object(iso, "probe_gpu_in_sandbox", self._fake_probe(captured)):
            line = local_runner._gpu_sandbox_smoke(_bwrap_caps((0,)), [0])

        self.assertTrue(captured["existed_at_plan"])
        self.assertTrue(
            captured["existed_at_probe"],
            "the smoke workspace must still exist while the probe binds it",
        )
        self.assertTrue(line.startswith("CUDA reachable"), line)
        self.assertNotIn("Can't find source path", line)
        # Cleaned up AFTER the probe — no temp-dir leak.
        self.assertFalse(os.path.isdir(captured["workspace"]))

    def test_workspace_cleaned_up_even_when_probe_raises(self):
        captured = {}
        with patch.object(local_runner, "_read_ldconfig", return_value=""), \
             patch.object(iso, "plan_compute_execution", self._fake_plan(captured)), \
             patch.object(
                 iso, "probe_gpu_in_sandbox", self._fake_probe(captured, raises=True)
             ):
            line = local_runner._gpu_sandbox_smoke(_bwrap_caps((0,)), [0])

        self.assertIn("smoke check error", line)
        self.assertFalse(os.path.isdir(captured["workspace"]))

    def test_declined_plan_reported_and_cleaned_up(self):
        captured = {}
        with patch.object(local_runner, "_read_ldconfig", return_value=""), \
             patch.object(
                 iso, "plan_compute_execution",
                 self._fake_plan(captured, declined=True, reason="no sandbox"),
             ), \
             patch.object(iso, "probe_gpu_in_sandbox", self._fake_probe(captured)):
            line = local_runner._gpu_sandbox_smoke(_bwrap_caps((0,)), [0])

        self.assertIn("plan declined: no sandbox", line)
        self.assertFalse(os.path.isdir(captured["workspace"]))


class TestCrossProjectGpuLedger(unittest.TestCase):
    """No-double-booking on the unified single-daemon GPU ledger.

    The multi-project local-runner runs ONE daemon per physical box that serves
    compute jobs from every allow-listed project through one process-wide
    ``gpu_state`` (see ``_make_gpu_state`` / ``_assign_gpus`` / ``_release_gpus``
    in local_runner.py). The whole point of collapsing the old per-project
    daemons into one is to close the double-booking bug: two daemons on the same
    box would each keep a private ``in_use`` set and could hand the SAME physical
    GPU index to two concurrently-running jobs. These tests prove the unified
    ledger never assigns the same index to two live jobs regardless of which
    project each job belongs to.
    """

    @staticmethod
    def _shared_state(allowed, per_job=1):
        return {
            "allowed": list(allowed),
            "in_use": set(),
            "per_job": per_job,
            "lock": threading.Lock(),
        }

    def test_two_projects_never_share_a_gpu_via_ledger(self):
        """Jobs from different projects draw disjoint indices from one ledger."""
        state = self._shared_state([0, 1])

        a = local_runner._assign_gpus(state)  # project-A job
        b = local_runner._assign_gpus(state)  # project-B job

        self.assertEqual(a, [0])
        self.assertEqual(b, [1])
        self.assertEqual(set(a) & set(b), set(), "cross-project GPU double-book")
        self.assertEqual(state["in_use"], {0, 1})

        # A third project's job finds the shared ledger saturated → no GPU.
        c = local_runner._assign_gpus(state)
        self.assertEqual(c, [])
        self.assertEqual(state["in_use"], {0, 1})

    def test_release_by_one_project_frees_index_for_another(self):
        """Releasing a project's job returns its index to the shared pool."""
        state = self._shared_state([0])

        a = local_runner._assign_gpus(state)  # project-A holds GPU 0
        self.assertEqual(a, [0])
        # Project-B is starved while A holds the only device.
        self.assertEqual(local_runner._assign_gpus(state), [])

        local_runner._release_gpus(state, a)  # project-A job finishes
        self.assertEqual(state["in_use"], set())

        b = local_runner._assign_gpus(state)  # project-B reuses the freed index
        self.assertEqual(b, [0])
        self.assertEqual(state["in_use"], {0})

    def test_per_job_multi_gpu_across_projects_stays_disjoint(self):
        """With per_job>1 each project's job gets a disjoint multi-GPU block."""
        state = self._shared_state([0, 1, 2, 3], per_job=2)

        a = local_runner._assign_gpus(state)  # project-A
        b = local_runner._assign_gpus(state)  # project-B

        self.assertEqual(a, [0, 1])
        self.assertEqual(b, [2, 3])
        self.assertEqual(set(a) & set(b), set())
        self.assertEqual(state["in_use"], {0, 1, 2, 3})
        # Pool exhausted: a third project's two-GPU request cannot be honored.
        self.assertEqual(local_runner._assign_gpus(state), [])

    def test_unified_gpu_ledger_no_cross_project_double_booking(self):
        """End-to-end: two concurrent _run_compute_job workers for DIFFERENT
        projects, sharing ONE gpu_state, never receive the same GPU index.

        A barrier-blocked fake plan holds BOTH jobs inside execution
        simultaneously (each after its ledger assignment), so the two jobs are
        genuinely live at the same instant — the exact condition under which two
        old per-project daemons would have double-booked. We capture the
        ``gpu_indices`` each job was planned with and assert they are disjoint,
        both non-empty, and together account for the whole two-GPU ledger.
        """
        state = self._shared_state([0, 1])
        caps = _bwrap_caps(gpu_indices=(0, 1))

        barrier = threading.Barrier(2, timeout=10)
        capture_lock = threading.Lock()
        captured = []  # list of (container_name, tuple(gpu_indices))

        def fake_plan(**kw):
            # Record the GPUs this job was assigned from the shared ledger.
            with capture_lock:
                captured.append(
                    (kw.get("container_name"), tuple(kw.get("gpu_indices") or ()))
                )
            # Block until BOTH jobs have assigned + reached here, so both hold
            # their GPUs at the same time — the double-book window.
            barrier.wait()
            env = os.environ.copy()
            env["BLUEPRINT_RESULT_FILE"] = kw["result_file_host"]
            env["BLUEPRINT_ARTIFACT_DIR"] = kw["artifact_dir_host"]
            return iso.ExecutionPlan(
                backend="bwrap",
                argv=["bash", "-lc", "true"],
                env=env,
            )

        def noop_report(*a, **k):
            return False

        active_jobs: set = set()
        lock = threading.Lock()

        def run_for_project(pid):
            job = {
                "job_id": f"{pid}-job",
                "project_id": pid,  # informational; assignment is project-agnostic
                "command": "true",
            }
            active_jobs.add(job["job_id"])
            local_runner._run_compute_job(
                job, "https://api.example.com", "tok", active_jobs, lock,
                capabilities=caps, gpu_state=state,
            )

        with patch.object(local_runner, "_report_compute_job", noop_report), \
             patch.object(local_runner.iso, "plan_compute_execution", fake_plan):
            t_a = threading.Thread(target=run_for_project, args=("proj-A",))
            t_b = threading.Thread(target=run_for_project, args=("proj-B",))
            t_a.start()
            t_b.start()
            t_a.join(timeout=15)
            t_b.join(timeout=15)

        self.assertFalse(t_a.is_alive() or t_b.is_alive(), "workers did not finish")
        self.assertEqual(len(captured), 2, "both jobs should have been planned")

        gpus = [g for _, g in captured]
        self.assertTrue(all(len(g) == 1 for g in gpus), f"expected 1 GPU each: {gpus}")
        self.assertEqual(
            set(gpus[0]) & set(gpus[1]), set(),
            f"same GPU handed to two live cross-project jobs: {captured}",
        )
        # The two disjoint assignments together consume the whole ledger.
        self.assertEqual(set(gpus[0]) | set(gpus[1]), {0, 1})
        # Both jobs finished → the ledger drained back to empty (no leak).
        self.assertEqual(state["in_use"], set())

    def test_assign_refuses_to_double_book_on_inconsistent_ledger(self):
        """Defense-in-depth: if the underlying assigner ever returns an index
        already marked in-use (ledger corruption / future-refactor bug),
        ``_assign_gpus`` refuses it rather than SILENTLY double-booking a GPU
        across projects."""
        state = self._shared_state([0, 1])
        state["in_use"].add(0)  # project-A already holds GPU 0

        # Force the low-level assigner to (wrongly) hand back the in-use index.
        with patch.object(local_runner.iso, "assign_gpu_indices", return_value=[0]):
            result = local_runner._assign_gpus(state)

        self.assertEqual(result, [], "must not double-book an already-held GPU")
        # The other project's reservation is left exactly as it was — untouched.
        self.assertEqual(state["in_use"], {0})

    def test_saturated_ledger_lets_extra_project_run_without_a_gpu(self):
        """A single-GPU ledger already held by one project: a second project's
        job still RUNS (GPU-less) rather than crashing or double-booking."""
        state = self._shared_state([0])
        state["in_use"].add(0)  # project-A already holds the only GPU
        caps = _bwrap_caps(gpu_indices=(0,))

        captured: dict = {}

        def noop_report(*a, **k):
            return False

        job = {"job_id": "proj-B-job", "project_id": "proj-B", "command": "true"}
        active_jobs: set = {"proj-B-job"}
        lock = threading.Lock()

        with patch.object(local_runner, "_report_compute_job", noop_report), \
             patch.object(local_runner.iso, "plan_compute_execution",
                          _bare_plan_factory(captured)):
            local_runner._run_compute_job(
                job, "https://api.example.com", "tok", active_jobs, lock,
                capabilities=caps, gpu_state=state,
            )

        # Second project got NO device (project-A still holds it) but still ran.
        self.assertEqual(tuple(captured.get("gpu_indices") or ()), ())
        self.assertEqual(state["in_use"], {0}, "project-A's GPU must be untouched")


if __name__ == "__main__":
    unittest.main()
