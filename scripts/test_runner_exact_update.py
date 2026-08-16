"""Real-Git tests for the transactional runner exact-update path."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent))
import runner_exact_update as exact


def _git_executable() -> str:
    found = shutil.which("git")
    if found:
        return found
    windows_git = r"C:\Program Files\Git\cmd\git.exe"
    if os.path.isfile(windows_git):
        return windows_git
    raise RuntimeError("git is required for runner exact-update tests")


class ExactUpdateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.runner = root / "runner"
        self.state_dir = root / "state"
        self.state_dir.mkdir(mode=0o700)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        self.state = self.state_dir / "transition.json"
        self.git = _git_executable()

        self._git("init", "--bare", str(self.origin), cwd=root)
        self._git("init", str(self.seed), cwd=root)
        self._git("config", "user.email", "runner-test@example.com", cwd=self.seed)
        self._git("config", "user.name", "Runner Test", cwd=self.seed)
        (self.seed / "agent").mkdir()
        (self.seed / "agent" / "package-lock.json").write_text(
            '{"lockfileVersion":3,"name":"base"}\n', encoding="utf-8"
        )
        (self.seed / "payload.txt").write_text("base\n", encoding="utf-8")
        self._git("add", ".", cwd=self.seed)
        self._git("commit", "-m", "base", cwd=self.seed)
        self._git("branch", "-M", "main", cwd=self.seed)
        self.base = self._sha(self.seed)
        self._git("remote", "add", "origin", str(self.origin), cwd=self.seed)
        self._git("push", "-u", "origin", "main", cwd=self.seed)

        self._git("checkout", "-b", "recovery", cwd=self.seed)
        (self.seed / "payload.txt").write_text("recovery\n", encoding="utf-8")
        (self.seed / "agent" / "package-lock.json").write_text(
            '{"lockfileVersion":3,"name":"recovery"}\n', encoding="utf-8"
        )
        self._git("add", ".", cwd=self.seed)
        self._git("commit", "-m", "recovery", cwd=self.seed)
        self.recovery = self._sha(self.seed)
        self._git("push", "origin", "recovery", cwd=self.seed)

        self._git("checkout", "main", cwd=self.seed)
        (self.seed / "payload.txt").write_text("main\n", encoding="utf-8")
        self._git("add", ".", cwd=self.seed)
        self._git("commit", "-m", "main diverged", cwd=self.seed)
        self.main = self._sha(self.seed)
        self._git("push", "origin", "main", cwd=self.seed)
        self._git(
            "clone", "--single-branch", "--branch", "main",
            str(self.origin), str(self.runner), cwd=root,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.git, *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def _sha(self, repo: Path) -> str:
        return self._git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    def _branch(self) -> str:
        return self._git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=self.runner
        ).stdout.strip()

    def _run(self, argv, **kwargs):
        if argv[0] == "git":
            argv = [self.git, *argv[1:]]
        elif argv[0] == "npm":
            return MagicMock(returncode=0, stdout="", stderr="")
        return subprocess.run(argv, **kwargs)

    def _update(self, target: str, run=None) -> bool:
        return exact.update_to_exact_commit(
            repo_root=str(self.runner),
            target_commit=target,
            transition_path=str(self.state),
            run=run or self._run,
        )

    def test_divergent_and_rollback_targets_replace_detached_without_moving_branch(self):
        self.assertTrue(self._update(self.recovery))
        self.assertEqual(self._sha(self.runner), self.recovery)
        self.assertEqual(self._branch(), "HEAD")
        self.assertEqual(
            self._git("rev-parse", "refs/heads/main", cwd=self.runner).stdout.strip(),
            self.main,
        )

        # The authenticated served SHA may intentionally roll back across
        # ancestry. It remains detached and the saved main branch is untouched.
        self.assertTrue(self._update(self.base))
        self.assertEqual(self._sha(self.runner), self.base)
        self.assertEqual(self._branch(), "HEAD")
        self.assertEqual(
            self._git("rev-parse", "refs/heads/main", cwd=self.runner).stdout.strip(),
            self.main,
        )
        self.assertFalse(self.state.exists())

    def test_dependency_failure_restores_original_branch_and_retry_converges(self):
        npm_calls = 0

        def fail_target_then_restore(argv, **kwargs):
            nonlocal npm_calls
            if argv[0] == "npm":
                npm_calls += 1
                return MagicMock(returncode=1 if npm_calls == 1 else 0)
            return self._run(argv, **kwargs)

        self.assertFalse(self._update(self.recovery, fail_target_then_restore))
        self.assertEqual(npm_calls, 2)
        self.assertEqual(self._sha(self.runner), self.main)
        self.assertEqual(self._branch(), "main")
        self.assertFalse(self.state.exists())

        self.assertTrue(self._update(self.recovery))
        self.assertEqual(self._sha(self.runner), self.recovery)
        self.assertEqual(self._branch(), "HEAD")

    def test_crash_after_checkout_retries_dependencies_before_marker_clear(self):
        npm_calls = 0

        def crash_once(argv, **kwargs):
            nonlocal npm_calls
            if argv[0] == "npm":
                npm_calls += 1
                if npm_calls == 1:
                    raise KeyboardInterrupt("simulated process death")
                return MagicMock(returncode=0, stdout="", stderr="")
            return self._run(argv, **kwargs)

        with self.assertRaises(KeyboardInterrupt):
            self._update(self.recovery, crash_once)
        self.assertEqual(self._sha(self.runner), self.recovery)
        self.assertEqual(self._branch(), "HEAD")
        self.assertTrue(self.state.exists())

        self.assertTrue(self._update(self.recovery, crash_once))
        self.assertEqual(npm_calls, 2)
        self.assertEqual(self._sha(self.runner), self.recovery)
        self.assertEqual(self._branch(), "HEAD")
        self.assertFalse(self.state.exists())

    def test_prior_failed_target_is_recovered_before_different_target(self):
        def fail_all_npm(argv, **kwargs):
            if argv[0] == "npm":
                return MagicMock(returncode=1)
            return self._run(argv, **kwargs)

        self.assertFalse(self._update(self.recovery, fail_all_npm))
        self.assertTrue(self.state.exists())
        self.assertEqual(self._sha(self.runner), self.main)
        self.assertEqual(self._branch(), "main")

        self.assertTrue(self._update(self.base))
        self.assertEqual(self._sha(self.runner), self.base)
        self.assertEqual(self._branch(), "HEAD")
        self.assertFalse(self.state.exists())

    def test_unverified_or_non_exact_target_cannot_move_checkout(self):
        calls = []

        def record(argv, **kwargs):
            calls.append(list(argv))
            return self._run(argv, **kwargs)

        self.assertFalse(self._update("HEAD", record))
        self.assertEqual(calls, [])
        self.assertFalse(self._update("f" * 40))
        self.assertEqual(self._sha(self.runner), self.main)
        self.assertEqual(self._branch(), "main")

    def test_malformed_target_is_side_effect_free_before_default_journal(self):
        calls = []

        def record(argv, **kwargs):
            calls.append(list(argv))
            return self._run(argv, **kwargs)

        state_dir = self.runner / ".git" / "blueprint-runner-update"
        self.assertFalse(state_dir.exists())
        self.assertFalse(
            exact.update_to_exact_commit(
                repo_root=str(self.runner),
                target_commit="HEAD",
                run=record,
            )
        )
        self.assertEqual(calls, [])
        self.assertFalse(state_dir.exists())

    def test_corrupt_numeric_marker_fails_closed_without_type_error(self):
        self.state.write_text(
            json.dumps(
                {
                    "repo_root": os.path.realpath(self.runner),
                    "target_commit": int("1" * 40),
                    "old_head": int("2" * 40),
                    "old_branch": "main",
                    "phase": "target_pending",
                }
            ),
            encoding="utf-8",
        )
        try:
            self.state.chmod(0o600)
        except OSError:
            pass
        self.assertFalse(self._update(self.recovery))
        self.assertEqual(self._sha(self.runner), self.main)
        self.assertEqual(self._branch(), "main")

    def test_default_journal_is_shared_by_every_instance_on_the_worktree(self):
        self.assertTrue(
            exact.update_to_exact_commit(
                repo_root=str(self.runner),
                target_commit=self.recovery,
                run=self._run,
            )
        )
        state_dir = self.runner / ".git" / "blueprint-runner-update"
        self.assertTrue((state_dir / "transition.json.lock").is_file())
        self.assertFalse((state_dir / "transition.json").exists())


if __name__ == "__main__":
    unittest.main()
