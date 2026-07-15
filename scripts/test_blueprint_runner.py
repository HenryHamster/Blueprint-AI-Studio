"""Tests for scripts/blueprint_runner.py (the blueprint-runner launcher)."""

import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add scripts dir to path so we can import blueprint_runner directly.
sys.path.insert(0, str(Path(__file__).parent))
import blueprint_runner as br


class ResolveUrlTests(unittest.TestCase):
    def test_defaults_to_baked_in_prod_url(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(br.resolve_url(None), br.DEFAULT_ORCHESTRATOR_URL)

    def test_cli_flag_wins_over_env_and_default(self):
        with patch.dict("os.environ", {"BLUEPRINT_ORCHESTRATOR_URL": "https://env.example"}):
            self.assertEqual(br.resolve_url("https://flag.example"), "https://flag.example")

    def test_env_used_when_no_flag(self):
        with patch.dict("os.environ", {"BLUEPRINT_ORCHESTRATOR_URL": "https://env.example/"}):
            # Trailing slash is stripped so f-string path joins never double-slash.
            self.assertEqual(br.resolve_url(None), "https://env.example")

    def test_trailing_slash_stripped_from_flag(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(br.resolve_url("https://x.example///"), "https://x.example")


class ResolveTokenTests(unittest.TestCase):
    def test_cli_flag_wins(self):
        with patch.dict("os.environ", {"BLUEPRINT_RUNNER_TOKEN": "env-token"}):
            self.assertEqual(br.resolve_token("flag-token"), "flag-token")

    def test_env_used_when_no_flag(self):
        with patch.dict("os.environ", {"BLUEPRINT_RUNNER_TOKEN": "env-token"}):
            self.assertEqual(br.resolve_token(None), "env-token")

    def test_none_when_absent(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(br.resolve_token(None))
            self.assertIsNone(br.resolve_token("   "))

    def test_resolve_token_falls_back_to_persisted_credential_file(self):
        # When both CLI and env are absent, read from persisted credential file.
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_load_persisted_token", return_value="persisted-token"):
            self.assertEqual(br.resolve_token(None), "persisted-token")

    def test_cli_flag_wins_over_persisted_credential(self):
        # CLI flag takes priority over persisted file.
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_load_persisted_token", return_value="persisted-token"):
            self.assertEqual(br.resolve_token("flag-token"), "flag-token")

    def test_env_wins_over_persisted_credential(self):
        # Env var takes priority over persisted file.
        with patch.dict("os.environ", {"BLUEPRINT_RUNNER_TOKEN": "env-token"}, clear=True), \
             patch.object(br, "_load_persisted_token", return_value="persisted-token"):
            self.assertEqual(br.resolve_token(None), "env-token")

    def test_credential_dir_override_is_honored(self):
        # BLUEPRINT_CREDENTIAL_DIR must be respected by the real _load_persisted_token
        # (not just the mock) so non-standard installs find their token.
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "runner_token")
            with open(token_path, "w") as f:
                f.write("override-token\n")
            with patch.dict("os.environ", {"BLUEPRINT_CREDENTIAL_DIR": tmpdir,
                                           "BLUEPRINT_RUNNER_TOKEN": ""}, clear=True):
                self.assertEqual(br.resolve_token(None), "override-token")


class PrerequisiteTests(unittest.TestCase):
    def test_all_ok_when_node_new_enough_and_claude_present(self):
        with patch.object(br, "_node_major", return_value=22), \
             patch.object(br.shutil, "which", return_value="/usr/bin/claude"), \
             patch.object(br, "_codex_prerequisite", return_value=br.Prereq("codex", True, "0.144.1")):
            results = {p.name: p for p in br.check_prerequisites()}
        self.assertTrue(results["node"].ok)
        self.assertTrue(results["claude"].ok)
        self.assertTrue(results["codex"].ok)

    def test_node_too_old_flagged_with_actionable_fix(self):
        with patch.object(br, "_node_major", return_value=18), \
             patch.object(br.shutil, "which", return_value="/usr/bin/claude"), \
             patch.object(br, "_codex_prerequisite", return_value=br.Prereq("codex", True, "0.144.1")):
            results = {p.name: p for p in br.check_prerequisites()}
        self.assertFalse(results["node"].ok)
        self.assertIn("22", results["node"].fix)

    def test_node_missing_flagged(self):
        with patch.object(br, "_node_major", return_value=None), \
             patch.object(br.shutil, "which", return_value="/usr/bin/claude"), \
             patch.object(br, "_codex_prerequisite", return_value=br.Prereq("codex", True, "0.144.1")):
            results = {p.name: p for p in br.check_prerequisites()}
        self.assertFalse(results["node"].ok)
        self.assertIn("nodejs.org", results["node"].fix)

    def test_claude_missing_flagged_with_install_hint(self):
        with patch.object(br, "_node_major", return_value=22), \
             patch.object(br.shutil, "which", return_value=None), \
             patch.object(br, "_codex_prerequisite", return_value=br.Prereq("codex", True, "0.144.1")):
            results = {p.name: p for p in br.check_prerequisites()}
        self.assertFalse(results["claude"].ok)
        self.assertIn("claude-code", results["claude"].fix)

    def test_node_major_parses_version_string(self):
        fake = MagicMock(returncode=0, stdout="v22.11.0\n")
        with patch.object(br.subprocess, "run", return_value=fake):
            self.assertEqual(br._node_major(), 22)

    def test_node_major_none_on_missing_binary(self):
        with patch.object(br.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(br._node_major())

    def test_codex_path_shim_with_missing_native_package_is_rejected(self):
        failed = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: Missing optional dependency @openai/codex-linux-x64",
        )
        with patch.object(br.shutil, "which", return_value="/usr/bin/codex"), \
             patch.object(br.subprocess, "run", return_value=failed):
            result = br._codex_prerequisite()
        self.assertFalse(result.ok)
        self.assertIn("native Codex package is missing", result.detail)
        self.assertIn("--include=optional", result.fix)
        self.assertIn("0.144.1", result.fix)

    def test_codex_below_gpt_5_6_minimum_is_rejected(self):
        old = MagicMock(returncode=0, stdout="codex-cli 0.143.0\n", stderr="")
        with patch.object(br.shutil, "which", return_value="/usr/bin/codex"), \
             patch.object(br.subprocess, "run", return_value=old):
            result = br._codex_prerequisite()
        self.assertFalse(result.ok)
        self.assertIn("older than required", result.detail)

    def test_codex_version_probe_is_quota_free_and_healthy(self):
        current = MagicMock(returncode=0, stdout="codex-cli 0.144.1\n", stderr="")
        with patch.object(br.shutil, "which", return_value="/usr/bin/codex"), \
             patch.object(br.subprocess, "run", return_value=current) as run:
            result = br._codex_prerequisite()
        self.assertTrue(result.ok)
        run.assert_called_once_with(
            ["/usr/bin/codex", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )


class RunLocalRunnerTests(unittest.TestCase):
    def test_injects_token_and_url_into_child_env(self):
        captured = {}

        def fake_run(argv, env=None):
            captured["argv"] = argv
            captured["env"] = env
            return MagicMock(returncode=0)

        rc = br.run_local_runner("my-token", "https://orch.example", check=False, _run=fake_run)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["env"]["BLUEPRINT_RUNNER_TOKEN"], "my-token")
        self.assertEqual(captured["env"]["BLUEPRINT_ORCHESTRATOR_URL"], "https://orch.example")
        # The token/URL never appear on argv (would leak via /proc/cmdline).
        self.assertNotIn("my-token", captured["argv"])
        self.assertTrue(captured["argv"][1].endswith("local_runner.py"))
        self.assertNotIn("--check", captured["argv"])

    def test_check_mode_appends_check_flag(self):
        captured = {}

        def fake_run(argv, env=None):
            captured["argv"] = argv
            return MagicMock(returncode=3)

        rc = br.run_local_runner("t", "https://o", check=True, _run=fake_run)
        self.assertEqual(rc, 3)
        self.assertIn("--check", captured["argv"])


class MainTests(unittest.TestCase):
    def test_no_command_prints_help_and_returns_2(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(br.main([]), 2)

    def test_start_without_token_returns_2(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(br.main(["start"]), 2)

    def test_start_blocked_when_prereq_missing(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[
                 br.Prereq("node", False, "not found", "install node"),
             ]), \
             patch.object(br, "run_local_runner") as run_mock:
            rc = br.main(["start", "--token", "tok"])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()  # never launches the runner with a missing prereq

    def test_start_delegates_when_prereqs_ok(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[
                 br.Prereq("node", True, "v22"),
                 br.Prereq("claude", True, "/usr/bin/claude"),
             ]), \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["start", "--token", "tok", "--url", "https://o.example"])
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()
        _, kwargs = run_mock.call_args
        self.assertFalse(kwargs["check"])
        # URL flag propagated to the delegated runner.
        self.assertEqual(run_mock.call_args.args[1], "https://o.example")

    def test_start_with_broken_codex_keeps_other_capacity_online(self):
        """Codex is provider-specific: its failure must not stop the runner."""
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[
                 br.Prereq("node", True, "v22"),
                 br.Prereq("claude", True, "/usr/bin/claude"),
                 br.Prereq(
                     "codex", False,
                     "PATH shim found, but the native Codex package is missing",
                     br._CODEX_REPAIR,
                 ),
             ]), \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["start", "--token", "tok"])
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()

    def test_start_with_missing_claude_reaches_background_maintainer(self):
        """Missing Claude is repairable and must not keep compute/Codex offline."""
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[
                 br.Prereq("node", True, "v22"),
                 br.Prereq("claude", False, "not found", "install claude"),
                 br.Prereq("codex", True, "codex-cli 0.144.1"),
             ]), \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["start", "--token", "tok"])
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()

    def test_check_with_broken_codex_is_nonzero(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[
                 br.Prereq("node", True, "v22"),
                 br.Prereq("claude", True, "/usr/bin/claude"),
                 br.Prereq("codex", False, "missing native package", br._CODEX_REPAIR),
             ]), \
             patch.object(br, "run_local_runner", return_value=0):
            rc = br.main(["check", "--token", "tok"])
        self.assertEqual(rc, 1)

    def test_check_returns_nonzero_when_prereq_missing_even_if_runner_ok(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[
                 br.Prereq("claude", False, "not found", "install claude"),
             ]), \
             patch.object(br, "run_local_runner", return_value=0):
            rc = br.main(["check", "--token", "tok"])
        self.assertEqual(rc, 1)

    def test_check_returns_zero_when_all_ok(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[
                 br.Prereq("node", True, "v22"),
                 br.Prereq("claude", True, "/usr/bin/claude"),
             ]), \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["check", "--token", "tok"])
        self.assertEqual(rc, 0)
        _, kwargs = run_mock.call_args
        self.assertTrue(kwargs["check"])

    def test_token_from_env_is_accepted(self):
        with patch.dict("os.environ", {"BLUEPRINT_RUNNER_TOKEN": "env-tok"}, clear=True), \
             patch.object(br, "_docker_usable", return_value=False), \
             patch.object(br, "check_prerequisites", return_value=[br.Prereq("node", True, "v22")]), \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["start"])
        self.assertEqual(rc, 0)
        # Baked-in prod URL used when none supplied.
        self.assertEqual(run_mock.call_args.args[1], br.DEFAULT_ORCHESTRATOR_URL)


def _fake_docker(
    *,
    present=False,
    build_rc=0,
    pull_rc=0,
    tag_rc=0,
    info_rc=0,
    record=None,
):
    """Return a fake ``subprocess.run`` that dispatches on the docker subcommand.

    NO real docker is ever invoked. ``record`` (a list) captures every argv so a
    test can assert which docker commands ran (e.g. that build was NOT invoked when
    the image is already present).
    """

    def run(argv, **kwargs):
        if record is not None:
            record.append(list(argv))
        sub = argv[1] if len(argv) > 1 else ""
        rc = {
            "image": 0 if present else 1,  # docker image inspect
            "build": build_rc,
            "pull": pull_rc,
            "tag": tag_rc,
            "info": info_rc,
        }.get(sub, 0)
        return MagicMock(returncode=rc)

    return run


class ResolveAgentImageTagTests(unittest.TestCase):
    def test_flag_wins_over_env_and_default(self):
        with patch.dict("os.environ", {"BLUEPRINT_AGENT_IMAGE": "env:tag"}):
            self.assertEqual(br.resolve_agent_image_tag("flag:tag"), "flag:tag")

    def test_env_used_when_no_flag(self):
        with patch.dict("os.environ", {"BLUEPRINT_AGENT_IMAGE": "env:tag"}):
            self.assertEqual(br.resolve_agent_image_tag(None), "env:tag")

    def test_defaults_to_blueprint_agent_local(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(br.resolve_agent_image_tag(None), br.DEFAULT_AGENT_IMAGE_TAG)
            self.assertEqual(br.DEFAULT_AGENT_IMAGE_TAG, "blueprint-agent:local")


class DockerUsableTests(unittest.TestCase):
    def test_true_when_docker_info_succeeds(self):
        with patch.object(br.shutil, "which", return_value="/usr/bin/docker"):
            self.assertTrue(br._docker_usable(_run=_fake_docker(info_rc=0)))

    def test_false_when_docker_info_fails(self):
        # docker present on PATH but the daemon/socket is denied (info != 0).
        with patch.object(br.shutil, "which", return_value="/usr/bin/docker"):
            self.assertFalse(br._docker_usable(_run=_fake_docker(info_rc=1)))

    def test_false_when_docker_absent_from_path(self):
        with patch.object(br.shutil, "which", return_value=None):
            self.assertFalse(br._docker_usable())

    def test_false_on_subprocess_error(self):
        with patch.object(br.shutil, "which", return_value="/usr/bin/docker"):
            def boom(*a, **k):
                raise OSError("no exec")
            self.assertFalse(br._docker_usable(_run=boom))


class EnsureAgentImageTests(unittest.TestCase):
    def setUp(self):
        # A real dir so ensure_agent_image's os.path.isdir(agent_dir) build guard passes.
        import tempfile
        self.agent_dir = tempfile.mkdtemp(prefix="bp_agent_dir_")

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.agent_dir, ignore_errors=True)

    def test_docker_unusable_skips_and_sets_no_image(self):
        res = br.ensure_agent_image(
            tag="blueprint-agent:local", docker_usable=False, _run=_fake_docker()
        )
        self.assertIsNone(res.image)
        self.assertFalse(res.active)
        self.assertIn("docker unusable", res.message.lower())

    def test_image_present_skips_build(self):
        calls = []
        run = _fake_docker(present=True, record=calls)
        res = br.ensure_agent_image(
            tag="blueprint-agent:local", docker_usable=True,
            agent_dir=self.agent_dir, _run=run,
        )
        self.assertEqual(res.image, "blueprint-agent:local")
        self.assertTrue(res.active)
        self.assertFalse(any(c[1] == "build" for c in calls), "build must NOT run when present")

    def test_image_absent_triggers_build_with_right_args(self):
        calls = []
        run = _fake_docker(present=False, build_rc=0, record=calls)
        res = br.ensure_agent_image(
            tag="mytag:1", docker_usable=True, agent_dir=self.agent_dir, _run=run,
        )
        self.assertEqual(res.image, "mytag:1")
        self.assertTrue(res.active)
        build_calls = [c for c in calls if c[1] == "build"]
        self.assertEqual(build_calls, [["docker", "build", "-t", "mytag:1", self.agent_dir]])

    def test_build_failure_sets_no_image(self):
        run = _fake_docker(present=False, build_rc=1)
        res = br.ensure_agent_image(
            tag="mytag:1", docker_usable=True, agent_dir=self.agent_dir, _run=run,
        )
        self.assertIsNone(res.image)
        self.assertFalse(res.active)
        self.assertIn("FAILED", res.message)

    def test_no_build_image_absent_stays_off(self):
        calls = []
        run = _fake_docker(present=False, record=calls)
        res = br.ensure_agent_image(
            tag="t:1", docker_usable=True, build=False,
            agent_dir=self.agent_dir, _run=run,
        )
        self.assertIsNone(res.image)
        self.assertFalse(res.active)
        self.assertFalse(any(c[1] == "build" for c in calls))

    def test_agent_image_tag_honored_in_build_and_result(self):
        run = _fake_docker(present=False, build_rc=0)
        res = br.ensure_agent_image(
            tag="custom/agent:v9", docker_usable=True, agent_dir=self.agent_dir, _run=run,
        )
        self.assertEqual(res.image, "custom/agent:v9")

    def test_pull_ref_pulls_and_retags(self):
        calls = []
        run = _fake_docker(present=False, pull_rc=0, tag_rc=0, record=calls)
        res = br.ensure_agent_image(
            tag="local:tag", docker_usable=True, pull_ref="reg.io/agent:1",
            agent_dir=self.agent_dir, _run=run,
        )
        self.assertEqual(res.image, "local:tag")
        self.assertTrue(res.active)
        self.assertIn(["docker", "pull", "reg.io/agent:1"], calls)
        self.assertIn(["docker", "tag", "reg.io/agent:1", "local:tag"], calls)

    def test_pull_ref_equal_to_tag_skips_retag(self):
        calls = []
        run = _fake_docker(pull_rc=0, record=calls)
        res = br.ensure_agent_image(
            tag="reg.io/agent:1", docker_usable=True, pull_ref="reg.io/agent:1", _run=run,
        )
        self.assertEqual(res.image, "reg.io/agent:1")
        self.assertFalse(any(c[1] == "tag" for c in calls), "no retag when ref == tag")

    def test_pull_failure_sets_no_image(self):
        run = _fake_docker(pull_rc=1)
        res = br.ensure_agent_image(
            tag="t:1", docker_usable=True, pull_ref="reg.io/x:1", _run=run,
        )
        self.assertIsNone(res.image)
        self.assertFalse(res.active)

    def test_probe_only_present_reports_on_without_building(self):
        calls = []
        run = _fake_docker(present=True, record=calls)
        res = br.ensure_agent_image(
            tag="t:1", docker_usable=True, probe_only=True, _run=run,
        )
        self.assertEqual(res.image, "t:1")
        self.assertTrue(res.active)
        self.assertFalse(any(c[1] in ("build", "pull") for c in calls))

    def test_probe_only_absent_reports_off_without_building(self):
        calls = []
        run = _fake_docker(present=False, record=calls)
        res = br.ensure_agent_image(
            tag="t:1", docker_usable=True, probe_only=True,
            agent_dir=self.agent_dir, _run=run,
        )
        self.assertIsNone(res.image)
        self.assertFalse(res.active)
        self.assertIn("will build", res.message)
        self.assertFalse(any(c[1] in ("build", "pull") for c in calls))


class RunLocalRunnerAgentImageTests(unittest.TestCase):
    def test_agent_image_exported_to_child_env(self):
        captured = {}

        def fake_run(argv, env=None):
            captured["env"] = env
            return MagicMock(returncode=0)

        br.run_local_runner(
            "tok", "https://o", check=False,
            agent_image="blueprint-agent:local", _run=fake_run,
        )
        self.assertEqual(captured["env"]["BLUEPRINT_AGENT_IMAGE"], "blueprint-agent:local")

    def test_no_image_drops_ambient_agent_image(self):
        captured = {}

        def fake_run(argv, env=None):
            captured["env"] = env
            return MagicMock(returncode=0)

        with patch.dict("os.environ", {"BLUEPRINT_AGENT_IMAGE": "stale:leftover"}):
            br.run_local_runner("tok", "https://o", check=False, agent_image=None, _run=fake_run)
        # The launcher is authoritative: a stale ambient image is NOT leaked.
        self.assertNotIn("BLUEPRINT_AGENT_IMAGE", captured["env"])


class MainDockerAgentWiringTests(unittest.TestCase):
    def _ok_prereqs(self):
        return [br.Prereq("node", True, "v22"), br.Prereq("claude", True, "/usr/bin/claude")]

    def test_start_ensures_image_and_passes_it_to_runner(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image",
                          return_value=br.AgentImageResult("blueprint-agent:local", True, "ON")) as ens, \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["start", "--token", "tok", "--docker-agent"])
        self.assertEqual(rc, 0)
        # start actually builds/pulls (probe_only False) and the ensured image flows through.
        self.assertFalse(ens.call_args.kwargs["probe_only"])
        self.assertEqual(run_mock.call_args.kwargs["agent_image"], "blueprint-agent:local")

    def test_check_probes_without_side_effects_and_forwards_none(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image",
                          return_value=br.AgentImageResult(None, False, "OFF")) as ens, \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["check", "--token", "tok", "--docker-agent"])
        self.assertEqual(rc, 0)
        self.assertTrue(ens.call_args.kwargs["probe_only"])  # no build/pull during check
        self.assertIsNone(run_mock.call_args.kwargs["agent_image"])

    def test_agent_image_flag_reaches_ensure(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image",
                          return_value=br.AgentImageResult(None, False, "OFF")) as ens, \
             patch.object(br, "run_local_runner", return_value=0):
            br.main(["start", "--token", "tok", "--docker-agent", "--agent-image", "custom:tag"])
        self.assertEqual(ens.call_args.kwargs["tag"], "custom:tag")

    def test_no_build_image_flag_reaches_ensure(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image",
                          return_value=br.AgentImageResult(None, False, "OFF")) as ens, \
             patch.object(br, "run_local_runner", return_value=0):
            br.main(["start", "--token", "tok", "--docker-agent", "--no-build-image"])
        self.assertFalse(ens.call_args.kwargs["build"])

    def test_docker_not_used_by_default_when_docker_usable(self):
        """Even when docker is usable, docker-agent path is OFF without explicit opt-in."""
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image") as ens, \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["start", "--token", "tok"])
        self.assertEqual(rc, 0)
        # ensure_agent_image must NOT be called — no build/pull without explicit opt-in.
        ens.assert_not_called()
        # agent_image must be None so BLUEPRINT_AGENT_IMAGE is NOT exported to the child.
        self.assertIsNone(run_mock.call_args.kwargs["agent_image"])

    def test_docker_agent_env_backend_acts_as_optin(self):
        """BLUEPRINT_AGENT_BACKEND=docker activates the docker path without --docker-agent."""
        with patch.dict("os.environ", {"BLUEPRINT_AGENT_BACKEND": "docker"}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image",
                          return_value=br.AgentImageResult("blueprint-agent:local", True, "ON")) as ens, \
             patch.object(br, "run_local_runner", return_value=0) as run_mock:
            rc = br.main(["start", "--token", "tok"])
        self.assertEqual(rc, 0)
        # ensure_agent_image IS called (env var is an explicit opt-in).
        ens.assert_called_once()
        self.assertEqual(run_mock.call_args.kwargs["agent_image"], "blueprint-agent:local")

    def test_pull_flag_acts_as_docker_optin(self):
        """--pull implies docker opt-in without needing --docker-agent."""
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image",
                          return_value=br.AgentImageResult("blueprint-agent:local", True, "ON")) as ens, \
             patch.object(br, "run_local_runner", return_value=0):
            rc = br.main(["start", "--token", "tok", "--pull", "registry.io/agent:1"])
        self.assertEqual(rc, 0)
        ens.assert_called_once()
        self.assertEqual(ens.call_args.kwargs["pull_ref"], "registry.io/agent:1")


def _systemctl_recorder(*, fail_on=None, record=None):
    """Fake ``_run`` for systemd/loginctl calls that dispatches on argv.

    NO real systemd is ever invoked. ``record`` (a list) captures every argv.
    ``fail_on`` maps a subcommand keyword (e.g. ``"is-active"``, ``"enable"``,
    ``"daemon-reload"``, ``"enable-linger"``) to the returncode it should yield;
    everything else returns rc 0. ``is-system-running`` returns rc 0 by default so
    _systemd_available() reads the manager as reachable.
    """
    fail_on = fail_on or {}

    def run(argv, **kwargs):
        if record is not None:
            record.append(list(argv))
        joined = " ".join(argv)
        rc = 0
        for needle, code in fail_on.items():
            if needle in joined:
                rc = code
                break
        return MagicMock(returncode=rc, stdout="", stderr="")

    return run


class RenderUnitTests(unittest.TestCase):
    def test_user_template_substitutes_all_tokens(self):
        text = br.render_unit(
            br._USER_SERVICE_TEMPLATE,
            {
                "@PYTHON@": "/opt/py/bin/python3",
                "@LOCAL_RUNNER@": "/home/me/repo/scripts/local_runner.py",
                "@ENV_FILE@": "/home/me/.blueprint/runner.env",
            },
        )
        self.assertIn("ExecStart=/opt/py/bin/python3 /home/me/repo/scripts/local_runner.py", text)
        self.assertIn("EnvironmentFile=-/home/me/.blueprint/runner.env", text)
        # No leftover @…@ placeholder survives.
        self.assertEqual(re.findall(r"@[A-Z_]+@", text), [])
        # Hardening directives are preserved from the template.
        for directive in ("Restart=on-failure", "RestartSec=5", "StartLimitIntervalSec=60",
                          "StartLimitBurst=3", "KillSignal=SIGTERM",
                          "TimeoutStopSec=30", "WantedBy=default.target"):
            self.assertIn(directive, text)
        # StartLimitIntervalSec and StartLimitBurst belong in [Unit], not [Service].
        self.assertLess(
            text.index("StartLimitIntervalSec=60"), text.index("[Service]"),
            "StartLimitIntervalSec must appear in the [Unit] block")
        self.assertLess(
            text.index("StartLimitBurst=3"), text.index("[Service]"),
            "StartLimitBurst must appear in the [Unit] block")

    def test_system_template_substitutes_user_and_wantedby(self):
        text = br.render_unit(
            br._SYSTEM_SERVICE_TEMPLATE,
            {
                "@PYTHON@": "/usr/bin/python3",
                "@LOCAL_RUNNER@": "/srv/repo/scripts/local_runner.py",
                "@ENV_FILE@": "/root/.blueprint/runner.env",
                "@USER@": "dgxops",
                "@WANTED_BY@": "multi-user.target",
            },
        )
        self.assertIn("User=dgxops", text)
        self.assertIn("WantedBy=multi-user.target", text)
        self.assertEqual(re.findall(r"@[A-Z_]+@", text), [])
        self.assertIn("KillSignal=SIGTERM", text)
        self.assertIn("StartLimitIntervalSec=60", text)
        self.assertIn("StartLimitBurst=3", text)
        # StartLimitIntervalSec and StartLimitBurst belong in [Unit], not [Service].
        self.assertLess(
            text.index("StartLimitIntervalSec=60"), text.index("[Service]"),
            "StartLimitIntervalSec must appear in the [Unit] block")
        self.assertLess(
            text.index("StartLimitBurst=3"), text.index("[Service]"),
            "StartLimitBurst must appear in the [Unit] block")
        # Comment block documents crash-loop guard recovery.
        self.assertIn("systemctl reset-failed", text)

    def test_leftover_placeholder_raises(self):
        with self.assertRaises(ValueError):
            br.render_unit(
                br._SYSTEM_SERVICE_TEMPLATE,
                {"@PYTHON@": "/usr/bin/python3"},  # @LOCAL_RUNNER@ etc. left unsubstituted
            )

    def test_missing_template_raises_filenotfounderror(self):
        """render_unit raises FileNotFoundError when the template path does not exist."""
        with self.assertRaises(FileNotFoundError):
            br.render_unit("/nonexistent/template.service", {"@TOKEN@": "value"})


class InstallTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bp_install_")
        self.unit_path = os.path.join(self.tmp, "systemd", "blueprint-runner.service")
        self.env_path = os.path.join(self.tmp, "cred", "runner.env")

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def _patch_paths(self):
        return [
            patch.object(br, "_user_unit_path", return_value=self.unit_path),
            patch.object(br, "_env_file_path", return_value=self.env_path),
            patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"),
        ]

    def test_install_renders_and_enables_systemd_user_unit(self):
        """REQUIRED acceptance test: --user install renders real paths, writes the
        user unit, and issues daemon-reload + enable --now + enable-linger."""
        calls = []
        run = _systemctl_recorder(record=calls)
        patches = self._patch_paths()
        for p in patches:
            p.start()
        try:
            rc = br.do_install(
                system=False, token="seed-token", url="https://orch.example", _run=run
            )
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        # Unit written to the user path with REAL substituted paths.
        self.assertTrue(os.path.isfile(self.unit_path))
        unit_text = Path(self.unit_path).read_text()
        self.assertIn(sys.executable, unit_text)
        self.assertIn(br._LOCAL_RUNNER, unit_text)
        self.assertTrue(br._LOCAL_RUNNER.endswith("local_runner.py"))
        # No leftover placeholder and no stale hardcoded workspace path.
        self.assertEqual(re.findall(r"@[A-Z_]+@", unit_text), [])
        self.assertNotIn("/path/to/workspace", unit_text)

        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("systemctl --user daemon-reload" in j for j in joined))
        self.assertTrue(any("systemctl --user enable --now blueprint-runner.service" in j for j in joined))
        self.assertTrue(any(
            j.startswith("loginctl enable-linger") for j in joined
        ), f"enable-linger must be issued; calls={joined}")

        # The token/url were seeded into the 0600 env file (not baked into the unit).
        env_text = Path(self.env_path).read_text()
        self.assertIn("BLUEPRINT_ORCHESTRATOR_URL=https://orch.example", env_text)
        self.assertIn("BLUEPRINT_RUNNER_TOKEN=seed-token", env_text)
        self.assertNotIn("seed-token", unit_text)
        self.assertEqual(os.stat(self.env_path).st_mode & 0o777, 0o600)

    def test_system_install_uses_system_path_and_skips_linger(self):
        calls = []
        run = _systemctl_recorder(record=calls)
        sys_unit = os.path.join(self.tmp, "etc", "blueprint-runner.service")
        patches = [
            patch.object(br, "_system_unit_path", return_value=sys_unit),
            patch.object(br, "_env_file_path", return_value=self.env_path),
            patch.object(br, "_install_user", return_value="dgxops"),
            patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"),
        ]
        for p in patches:
            p.start()
        try:
            rc = br.do_install(system=True, token=None, url="https://o.example", _run=run)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        unit_text = Path(sys_unit).read_text()
        self.assertIn("User=dgxops", unit_text)
        self.assertIn("WantedBy=multi-user.target", unit_text)
        joined = [" ".join(c) for c in calls]
        # System scope: no `--user`, and NO linger.
        self.assertTrue(any("systemctl daemon-reload" in j for j in joined))
        self.assertTrue(any("systemctl enable --now blueprint-runner.service" in j for j in joined))
        self.assertFalse(any("enable-linger" in j for j in joined),
                         "system install must NOT call enable-linger")
        # Token omitted → env file has URL but no token line.
        env_text = Path(self.env_path).read_text()
        self.assertIn("BLUEPRINT_ORCHESTRATOR_URL=https://o.example", env_text)
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", env_text)

    def test_enable_failure_returns_nonzero(self):
        run = _systemctl_recorder(fail_on={"enable --now": 1})
        patches = self._patch_paths()
        for p in patches:
            p.start()
        try:
            rc = br.do_install(system=False, token="t", url="https://o", _run=run)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc, 1)

    def test_verify_failure_returns_nonzero(self):
        # daemon-reload/enable succeed but the unit never becomes active.
        run = _systemctl_recorder(fail_on={"is-active": 3})
        patches = self._patch_paths()
        for p in patches:
            p.start()
        try:
            rc = br.do_install(system=False, token="t", url="https://o", _run=run)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc, 1)

    def test_missing_systemd_fails_actionably(self):
        run = _systemctl_recorder()
        with patch.object(br.shutil, "which", return_value=None):
            rc = br.do_install(system=False, token="t", url="https://o", _run=run)
        self.assertEqual(rc, 1)

    def test_dry_run_touches_nothing(self):
        calls = []
        run = _systemctl_recorder(record=calls)
        patches = self._patch_paths()
        for p in patches:
            p.start()
        try:
            rc = br.do_install(
                system=False, token="t", url="https://o", dry_run=True, _run=run,
            )
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc, 0)
        # No unit written, no env seeded, and only the availability probe ran.
        self.assertFalse(os.path.exists(self.unit_path))
        self.assertFalse(os.path.exists(self.env_path))
        self.assertFalse(any("daemon-reload" in " ".join(c) for c in calls))
        self.assertFalse(any("enable" in " ".join(c) for c in calls))

    def test_missing_template_returns_nonzero(self):
        """do_install returns rc=1 when the unit template file is absent."""
        run = _systemctl_recorder()
        patches = self._patch_paths()
        # Patch the module-level template constant to a nonexistent path so
        # do_install's os.path.isfile check returns False.
        patches.append(patch.object(br, "_USER_SERVICE_TEMPLATE",
                                     "/nonexistent/template.service"))
        for p in patches:
            p.start()
        try:
            rc = br.do_install(system=False, token=None, url="https://o", _run=run)
            self.assertEqual(rc, 1)
        finally:
            for p in patches:
                p.stop()

    def test_unit_write_oserror_returns_nonzero(self):
        """do_install returns rc=1 when writing the unit file raises OSError."""
        calls = []
        run = _systemctl_recorder(record=calls)
        patches = self._patch_paths()
        patches.append(patch.object(br, "_write_env_file", return_value=None))
        for p in patches:
            p.start()
        try:
            # Only fail on the unit_path write, allowing template and env writes.
            real_open = open
            def selective_open(path, *args, **kwargs):
                if isinstance(path, str) and path == self.unit_path:
                    raise OSError("disk full")
                return real_open(path, *args, **kwargs)

            with patch("builtins.open", side_effect=selective_open):
                rc = br.do_install(
                    system=False, token="t", url="https://o", _run=run
                )
            self.assertEqual(rc, 1)
        finally:
            for p in patches:
                p.stop()

    def test_write_env_file_chmod_failure_not_fatal(self):
        """_write_env_file survives os.chmod failures (both dir and file)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, "runner.env")
            # Cause both chmod calls to raise OSError — must not propagate.
            with patch("os.chmod", side_effect=OSError("permission denied")):
                br._write_env_file(env_path, url="https://o.example", token="my-tok")
            # File must still exist and contain the expected content.
            self.assertTrue(os.path.isfile(env_path))
            content = Path(env_path).read_text()
            self.assertIn("BLUEPRINT_ORCHESTRATOR_URL=https://o.example", content)
            self.assertIn("BLUEPRINT_RUNNER_TOKEN=my-tok", content)

    def test_install_user_honors_sudo_user(self):
        """_install_user returns SUDO_USER when set, taking precedence over USER."""
        with patch.dict("os.environ", {
            "SUDO_USER": "realop",
            "USER": "root",
        }):
            self.assertEqual(br._install_user(), "realop")

    def test_install_user_falls_back_to_user(self):
        """_install_user returns USER when SUDO_USER is not set."""
        with patch.dict("os.environ", {"USER": "agent"}, clear=True):
            self.assertEqual(br._install_user(), "agent")

    def test_systemd_available_rejects_dbus_failure_in_stderr(self):
        """_systemd_available returns False when stderr contains D-Bus failure."""
        def fake_run(argv, capture_output=True, text=True):
            return MagicMock(
                returncode=1,
                stdout="degraded",
                stderr="Failed to connect to bus: No such file or directory",
            )

        with patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"):
            self.assertFalse(br._systemd_available(system=False, _run=fake_run))

    def test_systemd_available_rejects_dbus_get_error_in_stderr(self):
        """_systemd_available returns False for 'Failed to get D-Bus' in stderr."""
        def fake_run(argv, capture_output=True, text=True):
            return MagicMock(
                returncode=1,
                stdout="",
                stderr="Failed to get D-Bus connection: Connection refused",
            )

        with patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"):
            self.assertFalse(br._systemd_available(system=True, _run=fake_run))

    def test_systemd_available_false_when_systemctl_absent(self):
        """_systemd_available returns False when systemctl is not on PATH."""
        with patch.object(br.shutil, "which", return_value=None):
            self.assertFalse(br._systemd_available(system=False))


class UninstallTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bp_uninstall_")
        self.unit_path = os.path.join(self.tmp, "blueprint-runner.service")

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def test_disables_and_removes_present_unit(self):
        Path(self.unit_path).write_text("[Unit]\n")
        calls = []
        run = _systemctl_recorder(record=calls)
        with patch.object(br, "_user_unit_path", return_value=self.unit_path), \
             patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"):
            rc = br.do_uninstall(system=False, _run=run)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.unit_path))  # unit file removed
        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("disable --now blueprint-runner.service" in j for j in joined))
        self.assertTrue(any("daemon-reload" in j for j in joined))

    def test_idempotent_when_nothing_installed(self):
        # No unit file on disk → clean report, rc 0, no daemon-reload needed.
        calls = []
        run = _systemctl_recorder(record=calls)
        with patch.object(br, "_user_unit_path", return_value=self.unit_path), \
             patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"):
            rc = br.do_uninstall(system=False, _run=run)
        self.assertEqual(rc, 0)
        self.assertFalse(any("daemon-reload" in " ".join(c) for c in calls))

    def test_uninstall_without_systemctl_removes_file_only(self):
        """When systemctl is absent, do_uninstall still removes the unit file
        but skips systemctl calls entirely."""
        Path(self.unit_path).write_text("[Unit]\n")
        with patch.object(br, "_user_unit_path", return_value=self.unit_path), \
             patch.object(br.shutil, "which", return_value=None):
            rc = br.do_uninstall(system=False)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.unit_path))

    def test_uninstall_remove_failure_returns_nonzero(self):
        """When os.remove raises OSError, do_uninstall returns rc=1."""
        Path(self.unit_path).write_text("[Unit]\n")
        with patch.object(br, "_user_unit_path", return_value=self.unit_path), \
             patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch("os.remove", side_effect=OSError("permission denied")):
            rc = br.do_uninstall(system=False)
        self.assertEqual(rc, 1)
        # The unit file must still exist (removal failed).
        self.assertTrue(os.path.isfile(self.unit_path))


class InstallUninstallMainRoutingTests(unittest.TestCase):
    def test_install_routed_to_do_install(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "do_install", return_value=0) as inst:
            rc = br.main(["install", "--token", "tok"])
        self.assertEqual(rc, 0)
        inst.assert_called_once()
        self.assertFalse(inst.call_args.kwargs["system"])
        self.assertEqual(inst.call_args.kwargs["token"], "tok")

    def test_install_system_flag_forwarded(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "do_install", return_value=0) as inst:
            br.main(["install", "--system"])
        self.assertTrue(inst.call_args.kwargs["system"])

    def test_install_without_token_still_routes(self):
        # Unlike start/check, install does not require a token (persisted-file path).
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "do_install", return_value=0) as inst:
            rc = br.main(["install"])
        self.assertEqual(rc, 0)
        self.assertIsNone(inst.call_args.kwargs["token"])

    def test_uninstall_routed_to_do_uninstall(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "do_uninstall", return_value=0) as uninst:
            rc = br.main(["uninstall", "--system"])
        self.assertEqual(rc, 0)
        uninst.assert_called_once()
        self.assertTrue(uninst.call_args.kwargs["system"])


def _resolve_admin_ui_src_root() -> str | None:
    """Locate ``admin-ui/src`` across local + Docker test environments.

    Mirrors backend/testing/test_invariants.py::_resolve_admin_ui_src_root so
    the cross-language guard resolves in the same places:
      1. ``ADMIN_UI_SRC`` env override.
      2. ``/admin-ui/src`` (Docker volume-mount target used by test-backend.sh).
      3. ``../admin-ui/src`` from this file (scripts/ -> repo root).
    """
    candidates = []
    env_override = os.getenv("ADMIN_UI_SRC")
    if env_override:
        candidates.append(env_override)
    candidates.append("/admin-ui/src")
    candidates.append(
        os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin-ui", "src")
        )
    )
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


# The TS declaration is: export const DEFAULT_ORCHESTRATOR_URL = 'https://...';
# (`url !== DEFAULT_ORCHESTRATOR_URL` comparisons have no `=`, so they don't match.)
_TS_DEFAULT_URL_RE = re.compile(
    r"""DEFAULT_ORCHESTRATOR_URL\s*=\s*['"]([^'"]+)['"]"""
)


class OrchestratorUrlCrossLanguageGuardTests(unittest.TestCase):
    """Fail on drift between the baked-in orchestrator URL in the Python launcher
    (``blueprint_runner.DEFAULT_ORCHESTRATOR_URL``) and the admin-UI constant
    (``FleetRunnersPanel.utils.ts``). If they diverge, the enroll dialog can omit
    ``--url`` while the launcher falls back to a stale baked URL, so an enrolled
    runner never connects and the console spins on "waiting...".
    """

    def _read_ts_default_url(self) -> str:
        root = _resolve_admin_ui_src_root()
        if root is None:
            # CI's backend-only Docker context (./backend build context) has no
            # admin-ui on disk; the guard fires on dev machines / agent VMs (full
            # checkout) and wherever test-backend.sh mounts admin-ui/src. This
            # mirrors the established skip in test_invariants.py's frontend guards.
            self.skipTest(
                "admin-ui/src not available in this test environment "
                "(run from a full checkout or mount via test-backend.sh)"
            )
        utils_path = os.path.join(root, "components", "FleetRunnersPanel.utils.ts")
        self.assertTrue(
            os.path.isfile(utils_path),
            f"expected FleetRunnersPanel.utils.ts at {utils_path}",
        )
        with open(utils_path, encoding="utf-8") as fh:
            source = fh.read()
        matches = _TS_DEFAULT_URL_RE.findall(source)
        self.assertEqual(
            len(matches),
            1,
            "expected exactly one DEFAULT_ORCHESTRATOR_URL declaration in "
            f"{utils_path}, found {len(matches)}: {matches}",
        )
        return matches[0]

    def test_python_and_ts_default_urls_are_identical(self):
        ts_url = self._read_ts_default_url()
        self.assertEqual(
            br.DEFAULT_ORCHESTRATOR_URL,
            ts_url,
            "DEFAULT_ORCHESTRATOR_URL drifted between "
            f"scripts/blueprint_runner.py ({br.DEFAULT_ORCHESTRATOR_URL!r}) and "
            f"admin-ui/src/components/FleetRunnersPanel.utils.ts ({ts_url!r}). "
            "Keep them identical: a mismatch makes the enroll dialog omit --url "
            "while the launcher falls back to a stale URL, so runners never connect.",
        )


class CredentialDirTests(unittest.TestCase):
    def test_credential_dir_honors_env_override(self):
        """_credential_dir returns BLUEPRINT_CREDENTIAL_DIR when set in env."""
        with patch.dict("os.environ", {"BLUEPRINT_CREDENTIAL_DIR": "/custom/cred/path"}):
            self.assertEqual(br._credential_dir(), "/custom/cred/path")

    def test_credential_dir_defaults_to_dot_blueprint(self):
        """_credential_dir returns ~/.blueprint when BLUEPRINT_CREDENTIAL_DIR is not set."""
        with patch.dict("os.environ", {}, clear=True):
            result = br._credential_dir()
            self.assertTrue(result.endswith(".blueprint"),
                           f"expected path ending with .blueprint, got {result!r}")

    def test_load_persisted_token_returns_none_on_oserror(self):
        """_load_persisted_token returns None when file open raises OSError."""
        with patch("builtins.open", side_effect=OSError("permission denied")):
            self.assertIsNone(br._load_persisted_token())

    def test_load_persisted_token_returns_none_on_empty_file(self):
        """_load_persisted_token returns None when the file is empty or whitespace-only."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "runner_token")
            Path(token_path).write_text("   \n")
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                self.assertIsNone(br._load_persisted_token())

    def test_load_persisted_token_reads_valid_token(self):
        """_load_persisted_token returns the stripped token content."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "runner_token")
            Path(token_path).write_text("my-persisted-token\n")
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                self.assertEqual(br._load_persisted_token(), "my-persisted-token")


def _git_recorder(*, upstream="upstream-sha", behind=True, fetch_rc=0, merge_rc=0, record=None):
    """Fake ``_run`` for git commands dispatched on argv.

    Simulates a repo that is behind origin: ``git rev-parse HEAD`` returns
    ``"local-sha"``, ``git rev-parse @{u}`` returns *upstream*, and
    ``git merge --ff-only @{u}`` succeeds when *behind* is True (or fails when
    behind is False, simulating nothing-to-pull or conflict).
    ``git diff --name-only`` returns changed files from *diff_files* (default
    ``"agent/package-lock.json\nadmin-ui/package-lock.json"``). ``git fetch``
    returns *fetch_rc*. ``record`` captures every argv.
    """

    LOCAL_SHA = "a" * 40

    def run(argv, **kwargs):
        if record is not None:
            record.append(list(argv))
        joined = " ".join(argv) if isinstance(argv, (list, tuple)) else ""

        # git fetch origin
        if "fetch" in joined and "origin" in joined:
            return MagicMock(returncode=fetch_rc, stdout="", stderr="")
        # git rev-parse HEAD
        if "rev-parse" in joined and "HEAD" in joined and "@{u}" not in joined and "--abbrev-ref" not in joined:
            return MagicMock(returncode=0, stdout=LOCAL_SHA, stderr="")
        # git rev-parse @{u}
        if "rev-parse" in joined and "@{u}" in joined:
            return MagicMock(
                returncode=0,
                stdout=upstream,
                stderr="",
            )
        # git rev-parse --abbrev-ref HEAD
        if "rev-parse" in joined and "--abbrev-ref" in joined and "HEAD" in joined:
            return MagicMock(returncode=0, stdout="main\n", stderr="")
        # git merge-base
        if "merge-base" in joined:
            # When local is behind, merge-base = local-sha (not upstream)
            if behind:
                return MagicMock(returncode=0, stdout=LOCAL_SHA, stderr="")
            return MagicMock(returncode=0, stdout=upstream, stderr="")
        # git merge --ff-only
        if "merge" in joined and "--ff-only" in joined:
            return MagicMock(returncode=merge_rc, stdout="Fast-forward\n", stderr="")
        # git diff --name-only
        if "diff" in joined and "--name-only" in joined:
            if behind and merge_rc == 0:
                return MagicMock(
                    returncode=0,
                    stdout="agent/package-lock.json\nadmin-ui/package-lock.json\n",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return run


class UpdateCommandTests(unittest.TestCase):
    """Tests for the ``blueprint-runner update`` command."""

    def test_update_routed_from_main(self):
        """``main(["update"])`` routes to ``_run_update_and_report``."""
        with patch.object(br, "_run_update_and_report", return_value=0) as routed:
            rc = br.main(["update"])
        self.assertEqual(rc, 0)
        routed.assert_called_once()

    def test_update_returns_1_when_git_unavailable(self):
        """When _git returns None (git absent), do_update prints an error and returns 1."""
        rc = br.do_update(_run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
        self.assertEqual(rc, 1)

    def test_update_already_up_to_date(self):
        """When local HEAD matches upstream, do_update reports up-to-date and returns 0."""
        # Both rev-parse HEAD and @{u} return the same sha → already up to date.
        SAME_SHA = "a" * 40
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            joined = " ".join(argv) if isinstance(argv, (list, tuple)) else ""
            if "fetch" in joined and "origin" in joined:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "rev-parse" in joined and "HEAD" in joined and "@{u}" not in joined and "--abbrev-ref" not in joined:
                return MagicMock(returncode=0, stdout=SAME_SHA, stderr="")
            if "rev-parse" in joined and "@{u}" in joined:
                return MagicMock(returncode=0, stdout=SAME_SHA, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        rc = br.do_update(_run=fake_run)
        self.assertEqual(rc, 0)
        # No merge attempted when already up to date.
        self.assertFalse(any("merge" in " ".join(c) for c in calls))

    def test_update_behind_fetches_and_fastforwards(self):
        """When behind origin, do_update fetches, merges --ff-only, and reinstalls deps."""
        calls = []
        run = _git_recorder(record=calls)
        rc = br.do_update(_run=run)
        self.assertEqual(rc, 0)
        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("fetch origin" in j for j in joined))
        self.assertTrue(any("merge --ff-only" in j for j in joined))
        self.assertTrue(any("diff --name-only" in j for j in joined))

    def test_update_ahead_returns_1(self):
        """When local HEAD is ahead of upstream, do_update returns 1."""
        # Simulate local ahead: merge-base matches upstream, not local
        LOCAL = "a" * 40
        UPSTREAM = "b" * 40  # older than local

        def fake_run(argv, **kwargs):
            joined = " ".join(argv) if isinstance(argv, (list, tuple)) else ""
            if "fetch" in joined and "origin" in joined:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "rev-parse" in joined and "HEAD" in joined and "@{u}" not in joined and "--abbrev-ref" not in joined:
                return MagicMock(returncode=0, stdout=LOCAL, stderr="")
            if "rev-parse" in joined and "@{u}" in joined:
                return MagicMock(returncode=0, stdout=UPSTREAM, stderr="")
            if "merge-base" in joined:
                # merge-base equals upstream → local is ahead
                return MagicMock(returncode=0, stdout=UPSTREAM, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        rc = br.do_update(_run=fake_run)
        self.assertEqual(rc, 1)

    def test_update_diverged_returns_1(self):
        """When local and upstream have diverged, do_update returns 1."""
        LOCAL = "a" * 40
        UPSTREAM = "b" * 40
        MERGE_BASE = "c" * 40  # neither HEAD nor upstream → diverged

        def fake_run(argv, **kwargs):
            joined = " ".join(argv) if isinstance(argv, (list, tuple)) else ""
            if "fetch" in joined and "origin" in joined:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "rev-parse" in joined and "HEAD" in joined and "@{u}" not in joined and "--abbrev-ref" not in joined:
                return MagicMock(returncode=0, stdout=LOCAL, stderr="")
            if "rev-parse" in joined and "@{u}" in joined:
                return MagicMock(returncode=0, stdout=UPSTREAM, stderr="")
            if "merge-base" in joined:
                return MagicMock(returncode=0, stdout=MERGE_BASE, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        rc = br.do_update(_run=fake_run)
        self.assertEqual(rc, 1)

    def test_update_no_upstream_returns_1(self):
        """When there is no upstream tracking branch, do_update returns 1."""
        def fake_run(argv, **kwargs):
            joined = " ".join(argv) if isinstance(argv, (list, tuple)) else ""
            if "fetch" in joined and "origin" in joined:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "rev-parse" in joined and "HEAD" in joined and "@{u}" not in joined:
                return MagicMock(returncode=0, stdout="a" * 40, stderr="")
            if "rev-parse" in joined and "@{u}" in joined:
                return MagicMock(returncode=1, stdout="", stderr="fatal: no upstream configured")
            return MagicMock(returncode=0, stdout="", stderr="")

        rc = br.do_update(_run=fake_run)
        self.assertEqual(rc, 1)

    def test_update_merge_failure_returns_1(self):
        """When git merge --ff-only fails, do_update returns 1."""
        calls = []
        run = _git_recorder(merge_rc=1, record=calls)
        rc = br.do_update(_run=run)
        self.assertEqual(rc, 1)
        joined = [" ".join(c) for c in calls]
        self.assertTrue(any("merge --ff-only" in j for j in joined))

    def test_update_fetch_failure_returns_1(self):
        """When git fetch fails, do_update returns 1."""
        run = _git_recorder(fetch_rc=1)
        rc = br.do_update(_run=run)
        self.assertEqual(rc, 1)

    def test_update_restarts_service(self):
        """After successful fast-forward, _restart_runner_service is called."""
        run = _git_recorder()
        with patch.object(br, "_restart_runner_service", return_value=True) as restart_mock:
            rc = br.do_update(_run=run)
        self.assertEqual(rc, 0)
        restart_mock.assert_called_once()

    def test_update_reports_success_to_orchestrator(self):
        """_run_update_and_report POSTs the update result on success."""
        with patch.object(br, "do_update", return_value=0) as update_mock, \
             patch.object(br, "_post_update_result") as post_mock, \
             patch.object(br, "_git", return_value=MagicMock(returncode=0, stdout="a" * 40)), \
             patch.object(br, "resolve_url", return_value="https://example.com"), \
             patch.object(br, "resolve_token", return_value="test-token"):
            rc = br._run_update_and_report()
        self.assertEqual(rc, 0)
        update_mock.assert_called_once()
        post_mock.assert_called_once()
        kwargs = post_mock.call_args[1]
        self.assertEqual(kwargs["status"], "success")
        self.assertEqual(kwargs["old_version"], "a" * 40)
        self.assertEqual(kwargs["new_version"], "a" * 40)

    def test_update_reports_failure_to_orchestrator(self):
        """_run_update_and_report POSTs the update result on failure."""
        with patch.object(br, "do_update", return_value=1) as update_mock, \
             patch.object(br, "_post_update_result") as post_mock, \
             patch.object(br, "_git", return_value=MagicMock(returncode=0, stdout="a" * 40)), \
             patch.object(br, "resolve_url", return_value="https://example.com"), \
             patch.object(br, "resolve_token", return_value="test-token"):
            rc = br._run_update_and_report()
        self.assertEqual(rc, 1)
        update_mock.assert_called_once()
        post_mock.assert_called_once()
        kwargs = post_mock.call_args[1]
        self.assertEqual(kwargs["status"], "failure")

    def test_update_report_does_not_block_on_post_failure(self):
        """_run_update_and_report returns the update exit code even if POST fails."""
        with patch.object(br, "do_update", return_value=0), \
             patch.object(br, "_post_update_result", side_effect=RuntimeError("network error")), \
             patch.object(br, "_git", return_value=MagicMock(returncode=0, stdout="a" * 40)), \
             patch.object(br, "resolve_url", return_value="https://example.com"), \
             patch.object(br, "resolve_token", return_value="test-token"):
            rc = br._run_update_and_report()
        self.assertEqual(rc, 0)

    def test_update_main_routes_to_run_update_and_report(self):
        """``main(["update"])`` routes to ``_run_update_and_report``, not ``do_update``."""
        with patch.object(br, "_run_update_and_report", return_value=0) as routed:
            rc = br.main(["update"])
        self.assertEqual(rc, 0)
        routed.assert_called_once()


class RestartServiceTests(unittest.TestCase):
    """Tests for :func:`_restart_runner_service`."""

    def test_restarts_user_service(self):
        """When user unit exists and is active, restarts via systemctl --user."""
        calls = []
        run = _systemctl_recorder(record=calls)
        with patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(br.os.path, "isfile", return_value=True):
            result = br._restart_runner_service(_run=run)
        self.assertTrue(result)
        joined = [" ".join(c) for c in calls]
        self.assertTrue(
            any("systemctl --user restart blueprint-runner.service" in j for j in joined)
        )

    def test_restarts_system_service_when_no_user_unit(self):
        """When only system unit exists, restarts via systemctl (no --user)."""
        calls = []
        run = _systemctl_recorder(record=calls)
        with patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(br.os.path, "isfile", side_effect=lambda p: "etc/systemd" in str(p)):
            result = br._restart_runner_service(_run=run)
        self.assertTrue(result)
        joined = [" ".join(c) for c in calls]
        self.assertTrue(
            any("systemctl restart blueprint-runner.service" in j for j in joined)
        )
        self.assertFalse(
            any("--user" in j and "restart" in j for j in joined)
        )

    def test_noop_when_not_installed(self):
        """Returns False when no unit file exists."""
        calls = []
        run = _systemctl_recorder(record=calls)
        with patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(br.os.path, "isfile", return_value=False):
            result = br._restart_runner_service(_run=run)
        self.assertFalse(result)
        self.assertFalse(any("restart" in " ".join(c) for c in calls))

    def test_noop_when_unit_inactive(self):
        """Returns False when unit exists but is-active fails."""
        calls = []
        run = _systemctl_recorder(fail_on={"is-active": 3}, record=calls)
        with patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(br.os.path, "isfile", return_value=True):
            result = br._restart_runner_service(_run=run)
        self.assertFalse(result)
        self.assertFalse(any("restart" in " ".join(c) for c in calls))

    def test_noop_when_systemctl_absent(self):
        """Returns False when systemctl is not on PATH."""
        with patch.object(br.shutil, "which", return_value=None):
            result = br._restart_runner_service()
        self.assertFalse(result)

    def test_user_scope_preferred_over_system(self):
        """When both user and system units exist, restarts user scope first."""
        calls = []
        run = _systemctl_recorder(record=calls)
        with patch.object(br.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(br.os.path, "isfile", return_value=True):
            result = br._restart_runner_service(_run=run)
        self.assertTrue(result)
        joined = [" ".join(c) for c in calls]
        self.assertTrue(
            any("systemctl --user restart" in j for j in joined),
            "user scope must be checked (and restarted) first when both exist",
        )


class PrintNoSystemdGuidanceTests(unittest.TestCase):
    def test_output_includes_actionable_guidance(self):
        """_print_no_systemd_guidance writes a nohup fallback to stderr."""
        import io
        fake_stderr = io.StringIO()
        with patch.object(br.sys, "stderr", fake_stderr):
            br._print_no_systemd_guidance(system=False)
        output = fake_stderr.getvalue()
        self.assertIn("no reachable systemd manager", output)
        self.assertIn("install --user", output)
        self.assertIn("nohup", output)

    def test_system_scope_mentions_system_flag(self):
        """For --system scope, the guidance references --system."""
        import io
        fake_stderr = io.StringIO()
        with patch.object(br.sys, "stderr", fake_stderr):
            br._print_no_systemd_guidance(system=True)
        output = fake_stderr.getvalue()
        self.assertIn("install --system", output)


class StatusCommandTests(unittest.TestCase):
    """Tests for the ``blueprint-runner status`` command."""

    def test_status_routed_from_main(self):
        """``main(["status"])`` routes to ``do_status``."""
        with patch.object(br, "do_status", return_value=0) as status_mock:
            rc = br.main(["status"])
        self.assertEqual(rc, 0)
        status_mock.assert_called_once()

    def test_status_command_reads_status_json(self):
        """do_status reads ~/.blueprint/status.json and prints a human-readable summary."""
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = os.path.join(tmpdir, "status.json")
            with open(status_path, "w") as f:
                json.dump({
                    "runner_id": "test-runner-1",
                    "last_heartbeat_at": "2026-07-09T12:00:00+00:00",
                    "last_heartbeat_status": "ok",
                    "active_task_ids": ["task-1", "task-2"],
                    "active_compute_job_ids": ["job-1"],
                    "bwrap_ok": True,
                    "docker_usable": False,
                    "agent_backend": "bwrap",
                    "compute_backend": "process",
                }, f)
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stdout = io.StringIO()
                with patch.object(br.sys, "stdout", fake_stdout):
                    rc = br.do_status()
            self.assertEqual(rc, 0)
            output = fake_stdout.getvalue()
            self.assertIn("runner_id", output)
            self.assertIn("test-runner-1", output)
            self.assertIn("last_heartbeat_at", output)
            self.assertIn("2026-07-09T12:00:00+00:00", output)
            self.assertIn("last_heartbeat_status", output)
            self.assertIn("ok", output)
            self.assertIn("task-1", output)
            self.assertIn("task-2", output)
            self.assertIn("job-1", output)
            self.assertIn("bwrap_ok", output)
            self.assertIn("True", output)

    def test_status_missing_file_returns_1(self):
        """do_status returns 1 when status.json does not exist."""
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stderr = io.StringIO()
                with patch.object(br.sys, "stderr", fake_stderr):
                    rc = br.do_status()
            self.assertEqual(rc, 1)
            self.assertIn("status file not found", fake_stderr.getvalue())

    def test_status_bad_json_returns_1(self):
        """do_status returns 1 when status.json contains invalid JSON."""
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = os.path.join(tmpdir, "status.json")
            with open(status_path, "w") as f:
                f.write("not valid json")
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stderr = io.StringIO()
                with patch.object(br.sys, "stderr", fake_stderr):
                    rc = br.do_status()
            self.assertEqual(rc, 1)
            self.assertIn("could not parse", fake_stderr.getvalue())

    def test_status_empty_active_ids(self):
        """do_status handles empty active_task_ids / active_compute_job_ids gracefully."""
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = os.path.join(tmpdir, "status.json")
            with open(status_path, "w") as f:
                json.dump({
                    "runner_id": "r1",
                    "last_heartbeat_at": "2026-07-09T12:00:00+00:00",
                    "last_heartbeat_status": "ok",
                    "active_task_ids": [],
                    "active_compute_job_ids": [],
                    "bwrap_ok": True,
                    "docker_usable": False,
                }, f)
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stdout = io.StringIO()
                with patch.object(br.sys, "stdout", fake_stdout):
                    rc = br.do_status()
            self.assertEqual(rc, 0)
            output = fake_stdout.getvalue()
            self.assertIn("none", output)

    def test_status_oserror_returns_1(self):
        """do_status returns 1 when the status file exists but cannot be read."""
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = os.path.join(tmpdir, "status.json")
            with open(status_path, "w") as f:
                f.write("{}")
            # Make file unreadable
            os.chmod(status_path, 0o000)
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stderr = io.StringIO()
                with patch.object(br.sys, "stderr", fake_stderr):
                    rc = br.do_status()
            self.assertEqual(rc, 1)
            self.assertIn("could not read", fake_stderr.getvalue())


class WriteEnvFileResourceLimitsTests(unittest.TestCase):
    """_write_env_file writes resource-limit env vars when supplied."""

    def test_resource_limits_written_to_env_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "runner.env")
            br._write_env_file(
                path,
                url="https://orch.example",
                token="tok",
                resource_limits={
                    "BLUEPRINT_RUNNER_MAX_CONCURRENT": "4",
                    "BLUEPRINT_RUNNER_GPU_INDICES": "0,1",
                },
            )
            with open(path) as f:
                content = f.read()
        self.assertIn("BLUEPRINT_RUNNER_MAX_CONCURRENT=4", content)
        self.assertIn("BLUEPRINT_RUNNER_GPU_INDICES=0,1", content)
        self.assertIn("BLUEPRINT_ORCHESTRATOR_URL=https://orch.example", content)
        self.assertIn("BLUEPRINT_RUNNER_TOKEN=tok", content)

    def test_no_resource_limits_omits_limit_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "runner.env")
            br._write_env_file(path, url="https://orch.example", token=None)
            with open(path) as f:
                content = f.read()
        self.assertNotIn("MAX_CONCURRENT", content)
        self.assertNotIn("GPU", content)

    def test_all_resource_limit_flags_written(self):
        import tempfile
        limits = {env_var: f"val_{i}" for i, (_, env_var, _, _) in enumerate(br._RESOURCE_LIMIT_VARS)}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "runner.env")
            br._write_env_file(path, url="https://orch.example", token=None, resource_limits=limits)
            with open(path) as f:
                content = f.read()
        for env_var, value in limits.items():
            self.assertIn(f"{env_var}={value}", content)

    def test_env_file_mode_is_0600(self):
        import tempfile
        import stat
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "runner.env")
            br._write_env_file(path, url="https://o.example", token=None)
            mode = oct(stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(mode, oct(0o600))


class ReadEnvFileTests(unittest.TestCase):
    """_read_env_file parses key=value pairs correctly."""

    def test_parses_key_value_pairs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "runner.env")
            with open(path, "w") as f:
                f.write("# comment\nFOO=bar\nBAZ=qux\n")
            result = br._read_env_file(path)
        self.assertEqual(result, {"FOO": "bar", "BAZ": "qux"})

    def test_missing_file_returns_empty_dict(self):
        result = br._read_env_file("/nonexistent/path/runner.env")
        self.assertEqual(result, {})

    def test_blank_lines_and_comments_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "runner.env")
            with open(path, "w") as f:
                f.write("# header\n\nKEY=value\n\n# another comment\n")
            result = br._read_env_file(path)
        self.assertEqual(result, {"KEY": "value"})


class InstallResourceLimitFlagsTests(unittest.TestCase):
    """install subcommand writes resource-limit flags into the env file."""

    def _fake_systemd(self, *, available=True, active=True, enabled=True):
        """Return fakes for _systemd_available and _systemctl."""
        def fake_systemd_avail(**kwargs):
            return available

        def fake_systemctl(args, **kwargs):
            sub = args[0] if args else ""
            rc = 0
            if sub == "is-active" and not active:
                rc = 1
            if sub == "is-enabled" and not enabled:
                rc = 1
            return MagicMock(returncode=rc)

        return fake_systemd_avail, fake_systemctl

    def test_max_concurrent_flag_written_to_env_file(self):
        import tempfile
        avail, systemctl = self._fake_systemd()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(br, "_credential_dir", return_value=tmpdir), \
                 patch.object(br, "_user_unit_path", return_value=os.path.join(tmpdir, "unit")), \
                 patch.object(br, "_systemd_available", avail), \
                 patch.object(br, "_systemctl", systemctl), \
                 patch.object(br, "render_unit", return_value="[Unit]"), \
                 patch.object(br, "_install_user", return_value="testuser"):
                rc = br.do_install(
                    url="https://o.example",
                    resource_limits={"BLUEPRINT_RUNNER_MAX_CONCURRENT": "6"},
                )
            env_path = os.path.join(tmpdir, "runner.env")
            with open(env_path) as f:
                content = f.read()
        self.assertEqual(rc, 0)
        self.assertIn("BLUEPRINT_RUNNER_MAX_CONCURRENT=6", content)

    def test_gpu_indices_flag_written_to_env_file(self):
        import tempfile
        avail, systemctl = self._fake_systemd()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(br, "_credential_dir", return_value=tmpdir), \
                 patch.object(br, "_user_unit_path", return_value=os.path.join(tmpdir, "unit")), \
                 patch.object(br, "_systemd_available", avail), \
                 patch.object(br, "_systemctl", systemctl), \
                 patch.object(br, "render_unit", return_value="[Unit]"), \
                 patch.object(br, "_install_user", return_value="testuser"):
                rc = br.do_install(
                    url="https://o.example",
                    resource_limits={"BLUEPRINT_RUNNER_GPU_INDICES": "2,3"},
                )
            env_path = os.path.join(tmpdir, "runner.env")
            with open(env_path) as f:
                content = f.read()
        self.assertEqual(rc, 0)
        self.assertIn("BLUEPRINT_RUNNER_GPU_INDICES=2,3", content)

    def test_no_resource_limits_installs_cleanly(self):
        import tempfile
        avail, systemctl = self._fake_systemd()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(br, "_credential_dir", return_value=tmpdir), \
                 patch.object(br, "_user_unit_path", return_value=os.path.join(tmpdir, "unit")), \
                 patch.object(br, "_systemd_available", avail), \
                 patch.object(br, "_systemctl", systemctl), \
                 patch.object(br, "render_unit", return_value="[Unit]"), \
                 patch.object(br, "_install_user", return_value="testuser"):
                rc = br.do_install(url="https://o.example")
        self.assertEqual(rc, 0)

    def test_install_parser_accepts_resource_limit_flags(self):
        """argparse install subparser accepts all resource-limit flags."""
        parser = br.build_parser()
        args = parser.parse_args([
            "install", "--token", "tok",
            "--max-concurrent", "4",
            "--max-concurrent-compute-jobs", "2",
            "--gpu-indices", "0,1",
            "--gpus-per-job", "2",
            "--cpu-limit", "8",
            "--mem-limit", "16g",
            "--min-disk-bytes", "10000000",
        ])
        self.assertEqual(args.max_concurrent, "4")
        self.assertEqual(args.gpu_indices, "0,1")
        self.assertEqual(args.mem_limit, "16g")
        self.assertEqual(args.min_disk_bytes, "10000000")


class StatusResourceLimitsTests(unittest.TestCase):
    """do_status displays resource-limit config from runner.env."""

    def _write_status(self, tmpdir: str) -> None:
        status_path = os.path.join(tmpdir, "status.json")
        with open(status_path, "w") as f:
            json.dump({
                "runner_id": "r1",
                "last_heartbeat_at": "2026-07-09T12:00:00+00:00",
                "last_heartbeat_status": "ok",
                "active_task_ids": [],
                "active_compute_job_ids": [],
            }, f)

    def test_status_shows_configured_resource_limits(self):
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_status(tmpdir)
            env_path = os.path.join(tmpdir, "runner.env")
            with open(env_path, "w") as f:
                f.write("BLUEPRINT_ORCHESTRATOR_URL=https://o.example\n")
                f.write("BLUEPRINT_RUNNER_MAX_CONCURRENT=8\n")
                f.write("BLUEPRINT_RUNNER_GPU_INDICES=0,1,2\n")
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stdout = io.StringIO()
                with patch.object(br.sys, "stdout", fake_stdout):
                    rc = br.do_status()
        self.assertEqual(rc, 0)
        output = fake_stdout.getvalue()
        self.assertIn("max-concurrent", output)
        self.assertIn("8", output)
        self.assertIn("gpu-indices", output)
        self.assertIn("0,1,2", output)

    def test_status_shows_not_set_for_unconfigured_limits(self):
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_status(tmpdir)
            env_path = os.path.join(tmpdir, "runner.env")
            with open(env_path, "w") as f:
                f.write("BLUEPRINT_ORCHESTRATOR_URL=https://o.example\n")
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stdout = io.StringIO()
                with patch.object(br.sys, "stdout", fake_stdout):
                    rc = br.do_status()
        self.assertEqual(rc, 0)
        output = fake_stdout.getvalue()
        self.assertIn("not set (using default)", output)

    def test_status_notes_when_env_file_absent(self):
        import tempfile
        import io
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_status(tmpdir)
            # No runner.env written — simulates uninstalled / direct-run scenario
            with patch.object(br, "_credential_dir", return_value=tmpdir):
                fake_stdout = io.StringIO()
                with patch.object(br.sys, "stdout", fake_stdout):
                    rc = br.do_status()
        self.assertEqual(rc, 0)
        output = fake_stdout.getvalue()
        self.assertIn("env file not found", output)


if __name__ == "__main__":
    unittest.main()
