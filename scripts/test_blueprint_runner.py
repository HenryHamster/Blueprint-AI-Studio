"""Tests for scripts/blueprint_runner.py (the blueprint-runner launcher)."""

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


class PrerequisiteTests(unittest.TestCase):
    def test_all_ok_when_node_new_enough_and_claude_present(self):
        with patch.object(br, "_node_major", return_value=22), \
             patch.object(br.shutil, "which", return_value="/usr/bin/claude"):
            results = {p.name: p for p in br.check_prerequisites()}
        self.assertTrue(results["node"].ok)
        self.assertTrue(results["claude"].ok)

    def test_node_too_old_flagged_with_actionable_fix(self):
        with patch.object(br, "_node_major", return_value=18), \
             patch.object(br.shutil, "which", return_value="/usr/bin/claude"):
            results = {p.name: p for p in br.check_prerequisites()}
        self.assertFalse(results["node"].ok)
        self.assertIn("22", results["node"].fix)

    def test_node_missing_flagged(self):
        with patch.object(br, "_node_major", return_value=None), \
             patch.object(br.shutil, "which", return_value="/usr/bin/claude"):
            results = {p.name: p for p in br.check_prerequisites()}
        self.assertFalse(results["node"].ok)
        self.assertIn("nodejs.org", results["node"].fix)

    def test_claude_missing_flagged_with_install_hint(self):
        with patch.object(br, "_node_major", return_value=22), \
             patch.object(br.shutil, "which", return_value=None):
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
            rc = br.main(["start", "--token", "tok"])
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
            rc = br.main(["check", "--token", "tok"])
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
            br.main(["start", "--token", "tok", "--agent-image", "custom:tag"])
        self.assertEqual(ens.call_args.kwargs["tag"], "custom:tag")

    def test_no_build_image_flag_reaches_ensure(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(br, "check_prerequisites", return_value=self._ok_prereqs()), \
             patch.object(br, "_docker_usable", return_value=True), \
             patch.object(br, "ensure_agent_image",
                          return_value=br.AgentImageResult(None, False, "OFF")) as ens, \
             patch.object(br, "run_local_runner", return_value=0):
            br.main(["start", "--token", "tok", "--no-build-image"])
        self.assertFalse(ens.call_args.kwargs["build"])


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


if __name__ == "__main__":
    unittest.main()
