"""Tests for scripts/runner_isolation.py — the fail-closed isolation core.

These are PURE unit tests: they assert the decision table, GPU-index assignment,
env allowlist, and the bwrap / docker / apptainer argv builders WITHOUT running a
sandbox (bwrap/apptainer are not installed on the dev box or CI). On-box runtime
validation of a real bwrap/apptainer launch on the DGX is pending an operator
install of bubblewrap.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import runner_isolation as iso


def _caps(**kw):
    return iso.Capabilities(**kw)


# ── Decision table ────────────────────────────────────────────────────────────

class TestDecisionTable(unittest.TestCase):
    def test_agent_needs_bwrap_and_binary(self):
        bwrap = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(iso.decide_agent_backend(bwrap, True), iso.BACKEND_BWRAP)
        # No binary → decline.
        self.assertEqual(iso.decide_agent_backend(bwrap, False), iso.DECLINE)
        # No userns → bwrap unusable → decline even with binary.
        no_ns = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=False)
        self.assertEqual(iso.decide_agent_backend(no_ns, True), iso.DECLINE)
        # No bwrap at all → decline (docker present must NOT enable agent).
        docker_only = _caps(docker_usable=True)
        self.assertEqual(iso.decide_agent_backend(docker_only, True), iso.DECLINE)

    def test_agent_decline_reasons(self):
        bwrap = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertIsNone(iso.agent_decline_reason(bwrap, True))
        self.assertIn("not built", iso.agent_decline_reason(bwrap, False))
        self.assertIn("bwrap", iso.agent_decline_reason(_caps(), True))

    def test_agent_docker_when_image_docker_and_readonly_type(self):
        # agent_image + usable docker + a read-only-eligible type → docker.
        caps = _caps(docker_usable=True)
        self.assertEqual(
            iso.decide_agent_backend(
                caps, True, task_type="research", agent_image="reg/agent:latest"
            ),
            iso.BACKEND_DOCKER,
        )
        # task_type=None (unclassified) is eligible too.
        self.assertEqual(
            iso.decide_agent_backend(caps, True, agent_image="reg/agent:latest"),
            iso.BACKEND_DOCKER,
        )
        # The docker path does NOT need the host agent binary (image ships its own).
        self.assertEqual(
            iso.decide_agent_backend(
                caps, False, task_type="plan", agent_image="reg/agent:latest"
            ),
            iso.BACKEND_DOCKER,
        )

    def test_agent_docker_needs_usable_docker(self):
        # Image + eligible type but docker NOT usable → falls back to bwrap table.
        no_docker = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)  # docker_usable=False
        self.assertEqual(
            iso.decide_agent_backend(
                no_docker, True, task_type="research", agent_image="reg/agent:latest"
            ),
            iso.BACKEND_BWRAP,
        )

    def test_decide_agent_backend_prefers_bwrap_when_not_opted_in(self):
        """bwrap is the DEFAULT — but an explicit opt-in still beats it.

        This test previously asserted that bwrap wins even WITH an agent_image,
        which directly contradicted test_bwrap_first_policy_across_device_shapes
        below (same inputs, opposite expectation). The repo carried both for as
        long as this suite went unrun in CI. Resolved in favour of the opt-in:
        blueprint_runner only resolves an agent_image when the operator explicitly
        asks (--docker-agent / BLUEPRINT_AGENT_BACKEND=docker / --pull), so
        letting bwrap win anyway made that flag build or pull a whole image and
        then silently ignore it.

        What "bwrap-first" actually means: docker is never auto-enabled merely
        because docker is usable. It does NOT mean the opt-in is inert.
        """
        both = _caps(docker_usable=True, bwrap_path="/usr/bin/bwrap", userns_ok=True)
        # No opt-in → bwrap, even though docker is perfectly usable.
        self.assertEqual(
            iso.decide_agent_backend(both, True, task_type="research"),
            iso.BACKEND_BWRAP,
        )
        self.assertEqual(iso.decide_agent_backend(both, True), iso.BACKEND_BWRAP)
        # Opted in → docker actually happens (the flag is not a no-op).
        self.assertEqual(
            iso.decide_agent_backend(
                both, True, task_type="research", agent_image="reg/agent:latest"
            ),
            iso.BACKEND_DOCKER,
        )
        # Docker reserved when bwrap unavailable (no binary either — image ships its own).
        docker_only = _caps(docker_usable=True)
        self.assertEqual(
            iso.decide_agent_backend(
                docker_only, False, task_type="research", agent_image="reg/agent:latest"
            ),
            iso.BACKEND_DOCKER,
        )
        # bwrap_ok but no binary + eligible image + usable docker → docker fallback.
        self.assertEqual(
            iso.decide_agent_backend(
                both, False, task_type="research", agent_image="reg/agent:latest"
            ),
            iso.BACKEND_DOCKER,
        )

    def test_agent_docker_declined_for_write_task_type(self):
        # A write/heavy type is NOT eligible for the read-only image → it falls
        # through to the bwrap/decline table unchanged.
        both = _caps(docker_usable=True, bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(
            iso.decide_agent_backend(
                both, True, task_type="fix", agent_image="reg/agent:latest"
            ),
            iso.BACKEND_BWRAP,
        )
        # Docker-only box + write type → decline (no bwrap fallback, no docker route).
        docker_only = _caps(docker_usable=True)
        self.assertEqual(
            iso.decide_agent_backend(
                docker_only, True, task_type="fix", agent_image="reg/agent:latest"
            ),
            iso.DECLINE,
        )

    def test_agent_image_none_is_byte_identical_bwrap_only(self):
        # CRITICAL no-regression: with agent_image falsy the result is identical to
        # the historical bwrap-or-decline table — docker must NEVER sneak in.
        bwrap = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(iso.decide_agent_backend(bwrap, True), iso.BACKEND_BWRAP)
        self.assertEqual(iso.decide_agent_backend(bwrap, False), iso.DECLINE)
        # Docker usable + eligible type but NO image → still bwrap-gated.
        both = _caps(docker_usable=True, bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(
            iso.decide_agent_backend(both, True, task_type="research"),
            iso.BACKEND_BWRAP,
        )
        docker_only = _caps(docker_usable=True)
        self.assertEqual(
            iso.decide_agent_backend(docker_only, True, task_type="research"),
            iso.DECLINE,
        )

    def test_agent_docker_types_membership(self):
        # The MUST-include read-only slice is present.
        for t in (
            "strategic_review", "research", "web_research", "plan", "design",
            "memo", "investigate", "design_doc_update",
        ):
            self.assertIn(t, iso.AGENT_DOCKER_TYPES)
        # Write/heavy types are excluded (no postgres/pytest/docker-in-docker).
        # ``docs`` is a WRITE/PR type (agent_mode="write", output_kind="pr") and
        # must NOT run in the read-only image.
        for t in (
            "strategic_resident_turn", "create", "fix", "refactor",
            "verify_tests", "smoketest", "compute_experiment", "qa", "migrate", "docs",
        ):
            self.assertNotIn(t, iso.AGENT_DOCKER_TYPES)

    def test_agent_docker_types_excludes_all_write_types(self):
        # Machine-checkable guard: NO write/PR task type may ever appear in the
        # read-only docker slice. Hardcoded because runner_isolation is stdlib-only
        # and cannot import the backend registry (app.task_capabilities) — mirror
        # its agent_mode=="write" set here.
        write_types = {"create", "fix", "refactor", "migrate", "qa", "deps", "docs"}
        self.assertEqual(iso.AGENT_DOCKER_TYPES & write_types, set())

    def test_compute_image_with_docker_uses_docker(self):
        caps = _caps(docker_usable=True, bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(iso.decide_compute_backend(caps, has_image=True), iso.BACKEND_DOCKER)

    def test_compute_image_prefers_docker_over_apptainer(self):
        caps = _caps(docker_usable=True, apptainer_path="/usr/bin/apptainer")
        self.assertEqual(iso.decide_compute_backend(caps, True), iso.BACKEND_DOCKER)

    def test_compute_image_apptainer_when_no_docker(self):
        caps = _caps(apptainer_path="/usr/bin/apptainer")
        self.assertEqual(iso.decide_compute_backend(caps, True), iso.BACKEND_APPTAINER)

    def test_image_declines_without_runtime_when_fallback_disabled(self):
        # Default (allow_host_fallback=False): image job with bwrap but no container
        # runtime is DECLINED — honest refusal, not a silent downgrade.
        caps = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(iso.decide_compute_backend(caps, has_image=True), iso.DECLINE)
        # Decline reason must mention the flag so operators know how to opt in.
        reason = iso.compute_decline_reason(caps, has_image=True)
        self.assertIsNotNone(reason)
        self.assertIn("allow_host_fallback", reason)

    def test_compute_image_no_runtime_allows_bwrap_with_host_fallback(self):
        # Explicit opt-in: allow_host_fallback=True permits the imageless bwrap path.
        caps = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(
            iso.decide_compute_backend(caps, has_image=True, allow_host_fallback=True),
            iso.BACKEND_BWRAP,
        )
        # And compute_decline_reason returns None (not declined).
        self.assertIsNone(
            iso.compute_decline_reason(caps, has_image=True, allow_host_fallback=True)
        )

    def test_compute_imageless_uses_bwrap_even_with_docker(self):
        caps = _caps(docker_usable=True, bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(iso.decide_compute_backend(caps, has_image=False), iso.BACKEND_BWRAP)

    def test_compute_imageless_no_bwrap_declines_even_with_docker(self):
        # A container needs an image; nothing else is sandboxable without bwrap.
        caps = _caps(docker_usable=True)
        self.assertEqual(iso.decide_compute_backend(caps, has_image=False), iso.DECLINE)

    def test_compute_nothing_declines(self):
        caps = _caps()
        self.assertEqual(iso.decide_compute_backend(caps, True), iso.DECLINE)
        self.assertEqual(iso.decide_compute_backend(caps, False), iso.DECLINE)
        self.assertIn("bubblewrap", iso.compute_decline_reason(caps, False))
        self.assertIn("container", iso.compute_decline_reason(caps, True))

    def test_claimable_classes(self):
        # DGX after installing bwrap: both claimable.
        bwrap = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(
            iso.claimable_classes(bwrap, True),
            {"agent": True, "compute": True},
        )
        # Container-only box (no bwrap): compute claimable (image jobs), agent NOT.
        docker = _caps(docker_usable=True)
        self.assertEqual(
            iso.claimable_classes(docker, True),
            {"agent": False, "compute": True},
        )
        # Bare box (the DGX before install): nothing claimable.
        self.assertEqual(
            iso.claimable_classes(_caps(), True),
            {"agent": False, "compute": False},
        )

    def test_docker_present_but_denied_reads_unusable(self):
        # Capabilities models the probe RESULT: docker_usable=False means the
        # decision table never routes an image job to a denied docker.
        denied = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True)  # docker_usable defaults False
        self.assertFalse(denied.docker_usable)
        # Default: image job with no runtime → DECLINE (not a silent bwrap downgrade).
        self.assertEqual(iso.decide_compute_backend(denied, has_image=True), iso.DECLINE)
        # Explicit opt-in: allow_host_fallback=True → bwrap is permitted as fallback.
        self.assertEqual(
            iso.decide_compute_backend(denied, has_image=True, allow_host_fallback=True),
            iso.BACKEND_BWRAP,
        )

    def test_bwrap_first_policy_across_device_shapes(self):
        """Bwrap-first policy: docker chosen only when agent_image explicitly supplied.

        Covers all four target device shapes from the design doc.  Without an
        agent_image the result must always be BACKEND_BWRAP (or DECLINE on a
        box where bwrap is unbuildable) — docker must NEVER win by default.
        """
        # WSL PC / second PC: bwrap + docker both present, no image → bwrap wins.
        wsl_pc = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=True)
        self.assertEqual(iso.decide_agent_backend(wsl_pc, True), iso.BACKEND_BWRAP)

        # Lab server: same — bwrap preferred even though docker is fully usable.
        lab_server = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=True)
        self.assertEqual(iso.decide_agent_backend(lab_server, True), iso.BACKEND_BWRAP)

        # DGX (userns on, docker socket denied): bwrap wins; denied docker is skipped.
        dgx_userns_on = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=False)
        self.assertEqual(iso.decide_agent_backend(dgx_userns_on, True), iso.BACKEND_BWRAP)

        # DGX (userns off, docker denied): no viable boundary → DECLINE.
        dgx_userns_off = _caps(docker_usable=False)
        self.assertEqual(iso.decide_agent_backend(dgx_userns_off, True), iso.DECLINE)

        # Opt-in: explicit agent_image + docker usable → BACKEND_DOCKER (documented necessity #1).
        self.assertEqual(
            iso.decide_agent_backend(
                lab_server, True, agent_image="reg/agent:v1", task_type="research"
            ),
            iso.BACKEND_DOCKER,
        )
        # Same lab_server + image but docker NOT usable → falls back to bwrap.
        lab_no_docker = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=False)
        self.assertEqual(
            iso.decide_agent_backend(
                lab_no_docker, True, agent_image="reg/agent:v1", task_type="research"
            ),
            iso.BACKEND_BWRAP,
        )


# ── GPU assignment ────────────────────────────────────────────────────────────

class TestGpuAssignment(unittest.TestCase):
    def test_allowlist_default_all(self):
        self.assertEqual(iso.parse_gpu_allowlist(None, (0, 1, 2)), [0, 1, 2])
        self.assertEqual(iso.parse_gpu_allowlist("", (0, 1)), [0, 1])

    def test_allowlist_intersects_detected(self):
        self.assertEqual(iso.parse_gpu_allowlist("1,3", (0, 1, 2, 3)), [1, 3])
        # A value outside detected is dropped.
        self.assertEqual(iso.parse_gpu_allowlist("1,9", (0, 1, 2)), [1])
        # Typo intersecting to empty → empty (never silently "all").
        self.assertEqual(iso.parse_gpu_allowlist("9", (0, 1)), [])

    def test_assign_disjoint(self):
        allowed = [0, 1, 2, 3]
        in_use = {1}
        self.assertEqual(iso.assign_gpu_indices(allowed, in_use, 1), [0])
        self.assertEqual(iso.assign_gpu_indices(allowed, {0, 1}, 2), [2, 3])

    def test_assign_none_when_insufficient(self):
        self.assertIsNone(iso.assign_gpu_indices([0, 1], {0, 1}, 1))
        self.assertIsNone(iso.assign_gpu_indices([0], set(), 2))

    def test_assign_zero_per_job(self):
        self.assertEqual(iso.assign_gpu_indices([0, 1], set(), 0), [])

    def test_concurrent_jobs_get_disjoint_sets(self):
        allowed = [0, 1, 2, 3]
        in_use: set[int] = set()
        a = iso.assign_gpu_indices(allowed, in_use, 1)
        in_use.update(a)
        b = iso.assign_gpu_indices(allowed, in_use, 1)
        in_use.update(b)
        self.assertEqual(set(a) & set(b), set())


# ── Env allowlist ─────────────────────────────────────────────────────────────

class TestEnvAllowlist(unittest.TestCase):
    def test_managed_npm_cli_release_root_is_discovered_for_read_only_bind(self):
        paths = [
            "/home/runner/.blueprint/managed-clis/codex/releases/"
            "release-1/node_modules/@openai/codex/bin/codex.js",
            "/home/runner/.blueprint/managed-clis/codex/releases/"
            "release-1/node_modules/.bin/codex",
            "/usr/bin/git",
        ]
        self.assertEqual(
            iso.npm_tool_roots(paths),
            [
                "/home/runner/.blueprint/managed-clis/codex/releases/"
                "release-1"
            ],
        )

    def test_global_npm_scope_root_is_discovered_for_optional_native_bind(self):
        paths = [
            "/home/henry/node22/lib/node_modules/@openai/codex/bin/codex.js",
            "/home/henry/node22/bin/codex",
            "/usr/bin/git",
        ]
        self.assertEqual(
            iso.npm_tool_roots(paths),
            [
                "/home/henry/node22/lib/node_modules/@openai",
                "/home/henry/node22/bin",
            ],
        )

    def test_unscoped_global_npm_package_keeps_narrow_package_bind(self):
        paths = [
            "/home/henry/node22/lib/node_modules/some-cli/bin/cli.js",
            "/home/henry/node22/bin/some-cli",
        ]
        self.assertEqual(
            iso.npm_tool_roots(paths),
            [
                "/home/henry/node22/lib/node_modules/some-cli",
                "/home/henry/node22/bin",
            ],
        )

    def test_agent_env_never_leaks_runner_secrets(self):
        source = {
            "BLUEPRINT_RUNNER_TOKEN": "SECRET-RUNNER-TOKEN",
            "BLUEPRINT_ORCHESTRATOR_URL": "https://api.example.com",
            "ANTHROPIC_API_KEY": "sk-ant-123",
            "PATH": "/usr/bin:/bin",
            "SOME_RANDOM_HOST_VAR": "leak-me",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
        }
        env = iso.build_agent_env(
            source,
            {"AGENT_SECRET_TOKEN": "jwt", "ORCHESTRATOR_URL": "https://api.example.com",
             "TASK_ID": "t1", "AGENT_WORKSPACE": "/workspace", "HOME": "/home"},
            ["/opt/agent/bin"],
        )
        # Runner secrets NEVER present.
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", env)
        self.assertNotIn("BLUEPRINT_ORCHESTRATOR_URL", env)
        # Arbitrary host vars dropped.
        self.assertNotIn("SOME_RANDOM_HOST_VAR", env)
        # Allowlisted provider creds pass through.
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-123")
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "aws-secret")
        # Injected identity vars present.
        self.assertEqual(env["AGENT_SECRET_TOKEN"], "jwt")
        self.assertEqual(env["TASK_ID"], "t1")
        # PATH rebuilt from tool dirs + system dirs (host $HOME nvm dirs gone).
        self.assertIn("/opt/agent/bin", env["PATH"])
        self.assertIn("/usr/bin", env["PATH"])
        self.assertEqual(env["HOME"], "/home")

    def test_agent_env_no_full_environ_source_key_present(self):
        # Even a value LOOKING like a token under a non-allowlisted name is dropped.
        source = {"MY_CUSTOM_TOKEN": "x", "PROVIDER": "anthropic"}
        env = iso.build_agent_env(source, {}, [])
        self.assertNotIn("MY_CUSTOM_TOKEN", env)
        self.assertEqual(env["PROVIDER"], "anthropic")

    def test_compute_env_excludes_provider_creds(self):
        source = {
            "ANTHROPIC_API_KEY": "sk-ant",
            "BLUEPRINT_RUNNER_TOKEN": "SECRET",
            "LD_LIBRARY_PATH": "/usr/local/cuda/lib64",
            "PATH": "/usr/bin",
        }
        env = iso.build_compute_env(
            {"MY_HP": "0.1"},
            {"BLUEPRINT_RESULT_FILE": "/workspace/result.json", "HOME": "/home"},
            source,
        )
        # Untrusted compute NEVER sees LLM keys or the runner token.
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", env)
        # CUDA discovery passes through; declared + injected present.
        self.assertEqual(env["LD_LIBRARY_PATH"], "/usr/local/cuda/lib64")
        self.assertEqual(env["MY_HP"], "0.1")
        self.assertEqual(env["BLUEPRINT_RESULT_FILE"], "/workspace/result.json")

    def test_forbidden_keys_stripped_even_if_injected(self):
        env = iso.build_agent_env(
            {}, {"BLUEPRINT_RUNNER_TOKEN": "oops", "AGENT_SECRET_TOKEN": "jwt"}, []
        )
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", env)
        self.assertEqual(env["AGENT_SECRET_TOKEN"], "jwt")

    # ── per_task_creds (single credential code path) ────────────────────────

    def test_build_agent_env_per_task_creds_replaces_source_creds(self):
        """per_task_creds replaces matching credential keys from source_env."""
        source = {
            "ANTHROPIC_API_KEY": "sk-ant-host",
            "AWS_SECRET_ACCESS_KEY": "aws-host",
            "LANG": "en_US.UTF-8",
            "PATH": "/usr/bin",
        }
        per_task = {
            "ANTHROPIC_API_KEY": "sk-ant-per-task",
            "AWS_SECRET_ACCESS_KEY": "aws-per-task",
        }
        env = iso.build_agent_env(
            source,
            {"AGENT_SECRET_TOKEN": "jwt"},
            [],
            per_task_creds=per_task,
        )
        # Credential keys REPLACED by per-task values (host value NOT used).
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-per-task")
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "aws-per-task")
        # Non-credential keys still flow from source_env.
        self.assertEqual(env["LANG"], "en_US.UTF-8")
        self.assertIn("/usr/bin", env["PATH"])
        # Injected identity vars present.
        self.assertEqual(env["AGENT_SECRET_TOKEN"], "jwt")

    def test_build_agent_env_per_task_creds_single_code_path(self):
        """When per_task_creds is provided, source_env credential keys for which
        per_task_creds has a matching entry use the per-task value, not the host
        value (single code path — no fallback to static host env)."""
        source = {
            "ANTHROPIC_API_KEY": "sk-ant-host",
            "LANG": "en_US.UTF-8",
        }
        per_task = {
            "ANTHROPIC_API_KEY": "sk-ant-per-task",
        }
        env = iso.build_agent_env(
            source,
            {"AGENT_SECRET_TOKEN": "jwt"},
            [],
            per_task_creds=per_task,
        )
        # ANTHROPIC_API_KEY comes from per_task_creds, NOT source_env.
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-per-task")
        self.assertNotEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-host")
        # Non-credential key still from source_env.
        self.assertEqual(env["LANG"], "en_US.UTF-8")

    def test_build_agent_env_per_task_creds_filtered_through_allowlist(self):
        """per_task_creds keys NOT in AGENT_ENV_ALLOWLIST are dropped."""
        source = {"LANG": "en_US.UTF-8"}
        per_task = {
            "ANTHROPIC_API_KEY": "sk-ant-valid",
            "MALICIOUS_INJECTION": "danger",
        }
        env = iso.build_agent_env(
            source,
            {"AGENT_SECRET_TOKEN": "jwt"},
            [],
            per_task_creds=per_task,
        )
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-valid")
        self.assertNotIn("MALICIOUS_INJECTION", env)

    def test_build_agent_env_empty_per_task_creds_is_noop(self):
        """Empty/None per_task_creds does not change behavior (source creds pass)."""
        source = {
            "ANTHROPIC_API_KEY": "sk-ant-host",
            "LANG": "en_US.UTF-8",
        }
        env = iso.build_agent_env(
            source,
            {"AGENT_SECRET_TOKEN": "jwt"},
            [],
            per_task_creds=None,
        )
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-host")
        self.assertEqual(env["LANG"], "en_US.UTF-8")


# ── bwrap argv builder ────────────────────────────────────────────────────────

class TestBwrapArgv(unittest.TestCase):
    def _argv(self, **kw):
        base = dict(
            bwrap_path="/usr/bin/bwrap",
            command_argv=["node", "/opt/agent/dist/index.js"],
            workspace_host="/tmp/ws/task1",
        )
        base.update(kw)
        return iso.build_bwrap_argv(**base)

    def test_namespaces_and_lifecycle_flags(self):
        argv = self._argv()
        for flag in (
            "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
            "--unshare-cgroup-try", "--die-with-parent", "--new-session",
        ):
            self.assertIn(flag, argv)

    def test_network_not_unshared(self):
        # Agent + compute need egress; net namespace must NOT be unshared.
        self.assertNotIn("--unshare-net", self._argv())

    def test_global_npm_directory_binds_preserve_cli_symlink(self):
        package_scope = "/home/henry/node22/lib/node_modules/@openai"
        npm_bin = "/home/henry/node22/bin"
        real_entry = f"{package_scope}/codex/bin/codex.js"
        shim = f"{npm_bin}/codex"
        argv = self._argv(
            ro_binds=[package_scope, npm_bin],
            tool_paths=[real_entry, shim],
        )
        joined = " ".join(argv)
        self.assertIn(f"--ro-bind-try {package_scope} {package_scope}", joined)
        self.assertIn(f"--ro-bind-try {npm_bin} {npm_bin}", joined)
        self.assertNotIn(f"--ro-bind-try {real_entry} {real_entry}", joined)
        self.assertNotIn(f"--ro-bind-try {shim} {shim}", joined)

    def test_managed_release_bind_preserves_dot_bin_symlink(self):
        release = "/home/runner/.blueprint/managed-clis/codex/releases/release-1"
        real_entry = f"{release}/node_modules/@openai/codex/bin/codex.js"
        shim = f"{release}/node_modules/.bin/codex"
        argv = self._argv(ro_binds=[release], tool_paths=[real_entry, shim])
        joined = " ".join(argv)
        self.assertIn(f"--ro-bind-try {release} {release}", joined)
        self.assertNotIn(f"--ro-bind-try {real_entry} {real_entry}", joined)
        self.assertNotIn(f"--ro-bind-try {shim} {shim}", joined)

    def test_fresh_tmpfs_home_and_no_host_home(self):
        argv = self._argv()
        # tmpfs /home present.
        joined = " ".join(argv)
        self.assertIn("--tmpfs /home", joined)
        self.assertIn("--tmpfs /tmp", joined)
        # NO host $HOME is bound: no bind whose target is a real user home path,
        # and HOME/.ssh/.claude style host dirs never appear as bind sources.
        host_home = os.path.expanduser("~")
        for i, tok in enumerate(argv):
            if tok in ("--bind", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try"):
                src = argv[i + 1]
                self.assertNotEqual(os.path.normpath(src), os.path.normpath(host_home))

    def test_no_docker_socket_bound(self):
        argv = self._argv()
        self.assertNotIn("/var/run/docker.sock", argv)
        self.assertNotIn("/run/docker.sock", argv)

    def test_workspace_bound_rw_at_stable_path(self):
        argv = self._argv(workspace_host="/tmp/ws/task1")
        # The RW workspace bind maps host dir → /workspace.
        self.assertIn("--bind", argv)
        idx = argv.index("--bind")
        self.assertEqual(argv[idx + 1], "/tmp/ws/task1")
        self.assertEqual(argv[idx + 2], "/workspace")

    def test_gpu_nodes_dev_bound_after_dev(self):
        argv = self._argv(gpu_device_nodes_list=["/dev/nvidia0", "/dev/nvidiactl"])
        joined = " ".join(argv)
        self.assertIn("--dev /dev", joined)
        self.assertIn("--dev-bind-try /dev/nvidia0 /dev/nvidia0", joined)
        # --dev appears before the gpu --dev-bind-try.
        self.assertLess(argv.index("--dev"), argv.index("/dev/nvidia0"))

    def test_command_is_terminal(self):
        argv = self._argv(command_argv=["bash", "-lc", "python train.py"])
        self.assertEqual(argv[-3:], ["bash", "-lc", "python train.py"])
        # Separated from bwrap flags by --.
        self.assertIn("--", argv)
        self.assertLess(argv.index("--"), len(argv) - 3)

    def test_token_never_in_argv(self):
        # A secret only ever lives in env, never on the sandbox argv.
        argv = self._argv(command_argv=["node", "/opt/agent/dist/index.js"])
        self.assertNotIn("SECRET-TOKEN", " ".join(argv))


# ── docker / apptainer argv builders ──────────────────────────────────────────

class TestDockerArgv(unittest.TestCase):
    def test_gpu_scoped_and_env_off_argv(self):
        argv = iso.build_docker_argv(
            image="nvcr.io/nvidia/pytorch:24.01",
            command="python train.py",
            container_name="bp-job1",
            gpu_indices=[2, 3],
            binds=[("/tmp/out", "/tmp/blueprint_out")],
            env_keys=["ANTHROPIC_API_KEY", "MY_HP"],
            cwd="/workspace",
        )
        joined = " ".join(argv)
        self.assertIn("--rm", argv)
        self.assertIn("--name", argv)
        self.assertIn("bp-job1", argv)
        self.assertIn("--gpus", argv)
        self.assertIn("device=2,3", argv)
        # Env is `-e KEY` (value read from the subprocess env), never inlined.
        self.assertIn("-e ANTHROPIC_API_KEY", joined)
        self.assertNotIn("-e ANTHROPIC_API_KEY=", joined)
        # Image precedes the command.
        self.assertLess(argv.index("nvcr.io/nvidia/pytorch:24.01"), argv.index("python train.py"))

    def test_no_gpus_flag_when_none(self):
        argv = iso.build_docker_argv(
            image="img", command="c", container_name="bp-x",
            gpu_indices=None, binds=[], env_keys=[], cwd=None,
        )
        self.assertNotIn("--gpus", argv)

    def test_gpu_cdi_uses_device_flag_not_gpus(self):
        # CDI daemons (rootless + /etc/cdi spec) reject --gpus; one --device per GPU.
        argv = iso.build_docker_argv(
            image="img", command="c", container_name="bp-x",
            gpu_indices=[2, 3], binds=[], env_keys=[], cwd=None,
            gpu_cdi=True,
        )
        joined = " ".join(argv)
        self.assertNotIn("--gpus", argv)
        self.assertIn("--device nvidia.com/gpu=2", joined)
        self.assertIn("--device nvidia.com/gpu=3", joined)

    def test_gpu_cdi_no_devices_when_no_gpus(self):
        argv = iso.build_docker_argv(
            image="img", command="c", container_name="bp-x",
            gpu_indices=None, binds=[], env_keys=[], cwd=None,
            gpu_cdi=True,
        )
        self.assertNotIn("--device", argv)

    def test_resource_limits(self):
        argv = iso.build_docker_argv(
            image="img", command="c", container_name="bp-x",
            gpu_indices=None, binds=[], env_keys=[], cwd=None,
            cpu_limit="4", mem_limit="8g",
        )
        self.assertIn("--cpus", argv)
        self.assertIn("--memory", argv)

    def test_command_none_runs_image_cmd(self):
        # command=None → the image is the terminal token; NO bash -lc suffix, so
        # the container runs its own ENTRYPOINT/CMD.
        argv = iso.build_docker_argv(
            image="reg/agent:latest", command=None, container_name="bp-agent-x",
            gpu_indices=None, binds=[], env_keys=["AGENT_SECRET_TOKEN"], cwd=None,
        )
        self.assertEqual(argv[-1], "reg/agent:latest")
        self.assertNotIn("bash", argv)
        self.assertNotIn("-lc", argv)
        # Env still forwarded as `-e KEY` (value read from the subprocess env).
        self.assertIn("-e AGENT_SECRET_TOKEN", " ".join(argv))

    def test_command_string_keeps_bash_lc_suffix(self):
        # Back-compat: a string command still appends the bash -lc override.
        argv = iso.build_docker_argv(
            image="img", command="python train.py", container_name="bp-x",
            gpu_indices=None, binds=[], env_keys=[], cwd=None,
        )
        self.assertEqual(argv[-3:], ["bash", "-lc", "python train.py"])


class TestApptainerArgv(unittest.TestCase):
    def test_containall_and_no_home_mandatory(self):
        argv = iso.build_apptainer_argv(
            apptainer_path="/usr/bin/apptainer",
            image="nvcr.io/nvidia/pytorch:24.01",
            command="python train.py",
            gpu=True,
            binds=[("/tmp/out", "/workspace")],
            cwd="/workspace",
        )
        # Without --containall --no-home apptainer mounts host $HOME/$PWD → leak.
        self.assertIn("--containall", argv)
        self.assertIn("--no-home", argv)
        self.assertIn("--nv", argv)
        # OCI ref rewritten to a docker:// URI.
        self.assertIn("docker://nvcr.io/nvidia/pytorch:24.01", argv)

    def test_no_nv_when_cpu(self):
        argv = iso.build_apptainer_argv(
            apptainer_path="/usr/bin/apptainer", image="img", command="c",
            gpu=False, binds=[], cwd=None,
        )
        self.assertNotIn("--nv", argv)

    def test_env_via_apptainerenv_not_argv(self):
        env = iso.apptainer_env(
            {"ANTHROPIC_API_KEY": "sk", "CUDA_VISIBLE_DEVICES": "0"},
            {"username": "$oauthtoken", "password": "ngc-key"},
        )
        self.assertEqual(env["APPTAINERENV_ANTHROPIC_API_KEY"], "sk")
        self.assertEqual(env["APPTAINERENV_CUDA_VISIBLE_DEVICES"], "0")
        # Registry secret via env, off argv.
        self.assertEqual(env["APPTAINER_DOCKER_PASSWORD"], "ngc-key")
        self.assertEqual(env["APPTAINER_DOCKER_USERNAME"], "$oauthtoken")

    def test_normalize_image_keeps_existing_uri(self):
        self.assertEqual(iso.normalize_apptainer_image("docker://x"), "docker://x")
        self.assertEqual(iso.normalize_apptainer_image("library://y"), "library://y")
        self.assertEqual(iso.normalize_apptainer_image("ubuntu:22.04"), "docker://ubuntu:22.04")


# ── GPU device / driver helpers ───────────────────────────────────────────────

class TestGpuExposure(unittest.TestCase):
    def test_device_nodes(self):
        nodes = iso.gpu_device_nodes([0, 2])
        self.assertIn("/dev/nvidia0", nodes)
        self.assertIn("/dev/nvidia2", nodes)
        self.assertNotIn("/dev/nvidia1", nodes)  # unassigned device NOT exposed
        self.assertIn("/dev/nvidiactl", nodes)
        self.assertIn("/dev/nvidia-uvm", nodes)

    def test_driver_libs_parse(self):
        ldconfig = (
            "\tlibcuda.so.1 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libcuda.so.1\n"
            "\tlibnvidia-ml.so.1 (libc6,x86-64) => /opt/nvidia/libnvidia-ml.so.1\n"
            "\tlibc.so.6 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libc.so.6\n"
        )
        libs = iso.nvidia_driver_libs(ldconfig)
        self.assertIn("/usr/lib/x86_64-linux-gnu/libcuda.so.1", libs)
        self.assertIn("/opt/nvidia/libnvidia-ml.so.1", libs)
        self.assertNotIn("/usr/lib/x86_64-linux-gnu/libc.so.6", libs)  # not a driver lib

    def test_covered_by(self):
        self.assertTrue(iso._covered_by("/usr/lib/x/libcuda.so", ("/usr", "/lib")))
        self.assertFalse(iso._covered_by("/opt/nvidia/libcuda.so", ("/usr", "/lib")))


# ── Resource caps ─────────────────────────────────────────────────────────────

class TestResourceCaps(unittest.TestCase):
    def test_systemd_scope_prefix(self):
        caps = _caps(systemd_run_path="/usr/bin/systemd-run")
        argv = iso.build_resource_scope_prefix(caps, cpu_limit="2", mem_limit="4g", scope_name="bp-x")
        self.assertEqual(argv[:4], ["/usr/bin/systemd-run", "--user", "--scope", "--collect"])
        joined = " ".join(argv)
        self.assertIn("CPUQuota=200%", joined)
        self.assertIn("MemoryMax=4g", joined)

    def test_no_prefix_without_systemd(self):
        caps = _caps()  # no systemd-run
        self.assertEqual(iso.build_resource_scope_prefix(caps, cpu_limit="2", mem_limit="4g"), [])

    def test_no_prefix_without_limits(self):
        caps = _caps(systemd_run_path="/usr/bin/systemd-run")
        self.assertEqual(iso.build_resource_scope_prefix(caps, cpu_limit=None, mem_limit=None), [])

    def test_parse_mem_bytes(self):
        self.assertEqual(iso._parse_mem_bytes("8g"), 8 * 1024**3)
        self.assertEqual(iso._parse_mem_bytes("512m"), 512 * 1024**2)
        self.assertEqual(iso._parse_mem_bytes("1024"), 1024)
        self.assertIsNone(iso._parse_mem_bytes(None))
        self.assertIsNone(iso._parse_mem_bytes("abc"))


# ── Plan orchestrators ────────────────────────────────────────────────────────

class TestAgentPlan(unittest.TestCase):
    def test_decline_without_bwrap(self):
        plan = iso.plan_agent_execution(
            caps=_caps(docker_usable=True),
            agent_binary=["node", "/opt/agent/dist/index.js"],
            workspace_host="/tmp/ws/t1",
            injected_env={"AGENT_SECRET_TOKEN": "jwt"},
            source_env={"PATH": "/usr/bin"},
        )
        self.assertTrue(plan.declined)
        self.assertIn("bwrap", plan.decline_reason)

    def test_decline_without_binary(self):
        plan = iso.plan_agent_execution(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True),
            agent_binary=None,
            workspace_host="/tmp/ws/t1",
            injected_env={},
            source_env={},
        )
        self.assertTrue(plan.declined)

    def test_agent_plan_bwrap_no_token_on_argv(self):
        plan = iso.plan_agent_execution(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True),
            agent_binary=["node", "/opt/agent/dist/index.js"],
            workspace_host="/tmp/ws/t1",
            injected_env={
                "AGENT_SECRET_TOKEN": "SUPER-SECRET-JWT",
                "ORCHESTRATOR_URL": "https://api.example.com",
                "TASK_ID": "t1",
                "VM_INSTANCE_ID": "runner:local",
            },
            source_env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant"},
        )
        self.assertFalse(plan.declined)
        self.assertEqual(plan.backend, iso.BACKEND_BWRAP)
        self.assertEqual(plan.argv[0], "/usr/bin/bwrap")
        # Secret token lives ONLY in env, never on argv.
        self.assertNotIn("SUPER-SECRET-JWT", " ".join(plan.argv))
        self.assertEqual(plan.env["AGENT_SECRET_TOKEN"], "SUPER-SECRET-JWT")
        self.assertEqual(plan.env["ANTHROPIC_API_KEY"], "sk-ant")
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", plan.env)
        # Workspace bound; AGENT_WORKSPACE points at a subdirectory (not the
        # bind mountpoint itself) so the agent's rm -rf avoids EBUSY.
        self.assertEqual(plan.env["AGENT_WORKSPACE"], "/workspace/agent")
        self.assertIn("/tmp/ws/t1", plan.argv)
        self.assertIn("--chdir", plan.argv)
        self.assertEqual(plan.argv[plan.argv.index("--chdir") + 1], "/workspace")

    def test_agent_plan_docker_backend(self):
        plan = iso.plan_agent_execution(
            caps=_caps(docker_usable=True),
            agent_binary=None,  # image ships its own build; host binary irrelevant
            workspace_host="/tmp/ws/t1",
            injected_env={
                "AGENT_SECRET_TOKEN": "SUPER-SECRET-JWT",
                "ORCHESTRATOR_URL": "https://api.example.com",
                "TASK_ID": "abcdef0123456789",
                "VM_INSTANCE_ID": "runner:local",
            },
            source_env={
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "sk-ant",
                "BLUEPRINT_RUNNER_TOKEN": "SECRET",
            },
            task_type="research",
            agent_image="reg/blueprint-agent-job:latest",
        )
        self.assertFalse(plan.declined)
        self.assertEqual(plan.backend, iso.BACKEND_DOCKER)
        # container_name = bp-agent-<task_id[:12]> (bp- prefix convention).
        self.assertEqual(plan.container_name, "bp-agent-abcdef012345")
        joined = " ".join(plan.argv)
        # docker run of the prod image; image is terminal; NO bash -lc override.
        self.assertEqual(plan.argv[:2], ["docker", "run"])
        self.assertEqual(plan.argv[-1], "reg/blueprint-agent-job:latest")
        self.assertNotIn("bash", plan.argv)
        self.assertNotIn("-lc", plan.argv)
        # NO host /workspace bind — the container owns /workspace.
        self.assertNotIn("-v", plan.argv)
        self.assertNotIn("/tmp/ws/t1", plan.argv)
        # Identity + cred keys forwarded via `-e KEY` (value read from client env).
        self.assertIn("-e AGENT_SECRET_TOKEN", joined)
        self.assertIn("-e ANTHROPIC_API_KEY", joined)
        self.assertIn("-e TASK_ID", joined)
        self.assertIn("-e VM_INSTANCE_ID", joined)
        # Secret VALUES never on argv.
        self.assertNotIn("SUPER-SECRET-JWT", joined)
        self.assertNotIn("sk-ant", joined)
        # Values live in the docker CLIENT env for `-e` to read; token stripped.
        self.assertEqual(plan.env["AGENT_SECRET_TOKEN"], "SUPER-SECRET-JWT")
        self.assertEqual(plan.env["ANTHROPIC_API_KEY"], "sk-ant")
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", plan.env)
        # Host PATH is NOT forwarded into the container (`-e PATH` absent).
        self.assertNotIn("-e PATH", joined)

    def test_agent_plan_no_image_stays_bwrap(self):
        # An eligible task_type but NO image → the existing bwrap plan; backend
        # and container_name unchanged (container_name stays None on bwrap).
        plan = iso.plan_agent_execution(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True),
            agent_binary=["node", "/opt/agent/dist/index.js"],
            workspace_host="/tmp/ws/t1",
            injected_env={"AGENT_SECRET_TOKEN": "jwt", "TASK_ID": "t1"},
            source_env={"PATH": "/usr/bin"},
            task_type="research",
        )
        self.assertFalse(plan.declined)
        self.assertEqual(plan.backend, iso.BACKEND_BWRAP)
        self.assertIsNone(plan.container_name)

    # ── per_task_creds (single credential code path) ────────────────────────

    def test_agent_plan_bwrap_with_per_task_creds(self):
        """plan_agent_execution forwards per_task_creds on bwrap path."""
        plan = iso.plan_agent_execution(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True),
            agent_binary=["node", "/opt/agent/dist/index.js"],
            workspace_host="/tmp/ws/t1",
            injected_env={
                "AGENT_SECRET_TOKEN": "jwt",
                "ORCHESTRATOR_URL": "https://api.example.com",
                "TASK_ID": "t1",
                "VM_INSTANCE_ID": "runner:local",
            },
            source_env={
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "sk-ant-host",
                "LANG": "en_US.UTF-8",
            },
            per_task_creds={
                "ANTHROPIC_API_KEY": "sk-ant-per-task",
            },
        )
        self.assertFalse(plan.declined)
        self.assertEqual(plan.backend, iso.BACKEND_BWRAP)
        # per_task_creds value wins over source_env value.
        self.assertEqual(plan.env["ANTHROPIC_API_KEY"], "sk-ant-per-task")
        self.assertNotEqual(plan.env["ANTHROPIC_API_KEY"], "sk-ant-host")
        # Non-cred key from source_env preserved.
        self.assertEqual(plan.env["LANG"], "en_US.UTF-8")

    def test_agent_plan_docker_with_per_task_creds(self):
        """plan_agent_execution forwards per_task_creds on docker path."""
        plan = iso.plan_agent_execution(
            caps=_caps(docker_usable=True),
            agent_binary=None,
            workspace_host="/tmp/ws/t1",
            injected_env={
                "AGENT_SECRET_TOKEN": "SUPER-SECRET-JWT",
                "ORCHESTRATOR_URL": "https://api.example.com",
                "TASK_ID": "abcdef0123456789",
                "VM_INSTANCE_ID": "runner:local",
            },
            source_env={
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "sk-ant-host",
                "BLUEPRINT_RUNNER_TOKEN": "SECRET",
            },
            task_type="research",
            agent_image="reg/blueprint-agent-job:latest",
            per_task_creds={
                "ANTHROPIC_API_KEY": "sk-ant-per-task",
            },
        )
        self.assertFalse(plan.declined)
        self.assertEqual(plan.backend, iso.BACKEND_DOCKER)
        # per_task_creds value overrides source_env value in docker client env.
        self.assertEqual(plan.env["ANTHROPIC_API_KEY"], "sk-ant-per-task")
        self.assertNotEqual(plan.env["ANTHROPIC_API_KEY"], "sk-ant-host")
        # Host PATH is NOT forwarded into the container.
        joined = " ".join(plan.argv)
        self.assertNotIn("-e PATH", joined)
        # Runner token still stripped.
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", plan.env)


class TestComputePlan(unittest.TestCase):
    def _common(self, **over):
        base = dict(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True),
            command="python train.py",
            image=None,
            registry_auth=None,
            workspace_host="/tmp/bp_x",
            result_file_host="/tmp/bp_x/result.json",
            artifact_dir_host="/tmp/bp_x/artifacts",
            clone_dir_host=None,
            cwd_host=None,
            job_env={"MY_HP": "0.1"},
            gpu_indices=[],
            container_name="bp-job1",
            source_env={"PATH": "/usr/bin", "BLUEPRINT_RUNNER_TOKEN": "SECRET"},
        )
        base.update(over)
        return iso.plan_compute_execution(**base)

    def test_bwrap_imageless_no_token_leak(self):
        plan = self._common()
        self.assertEqual(plan.backend, iso.BACKEND_BWRAP)
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", plan.env)
        self.assertNotIn("SECRET", " ".join(plan.argv))
        # Result file translated to the in-sandbox workspace path.
        self.assertEqual(plan.env["BLUEPRINT_RESULT_FILE"], "/workspace/result.json")
        self.assertEqual(plan.env["MY_HP"], "0.1")
        # /sys is GPU-only: non-GPU sandboxes must stay /sys-free.
        self.assertNotIn(" /sys", " ".join(plan.argv))

    def test_bwrap_gpu_exposes_only_assigned_nodes(self):
        plan = self._common(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, gpu_indices=(0, 1, 2, 3)),
            gpu_indices=[2],
        )
        joined = " ".join(plan.argv)
        self.assertIn("/dev/nvidia2", joined)
        self.assertNotIn("/dev/nvidia0", joined)  # unassigned NOT exposed
        self.assertIn("/dev/nvidiactl", joined)
        # CUDA/NVML walk /sys/bus/pci at init — without this ro bind cuInit
        # fails with CUDA_ERROR_OPERATING_SYSTEM even with all /dev nodes bound.
        self.assertIn("--ro-bind-try /sys /sys", joined)
        # Only 1 device exposed → CUDA re-enumerates from 0.
        self.assertEqual(plan.env["CUDA_VISIBLE_DEVICES"], "0")
        self.assertEqual(plan.env["NVIDIA_VISIBLE_DEVICES"], "2")

    def test_docker_image_backend(self):
        plan = self._common(
            caps=_caps(docker_usable=True, gpu_indices=(0,)),
            image="nvcr.io/nvidia/pytorch:24.01",
            gpu_indices=[0],
        )
        self.assertEqual(plan.backend, iso.BACKEND_DOCKER)
        self.assertEqual(plan.container_name, "bp-job1")
        self.assertIn("device=0", plan.argv)
        # Client env carries the inner vars for `-e` forwarding but NOT the token.
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", plan.env)

    def test_docker_clone_honors_nested_cwd(self):
        plan = self._common(
            caps=_caps(docker_usable=True),
            image="nvcr.io/nvidia/pytorch:24.01",
            clone_dir_host="/tmp/bp_x/repo",
            cwd_host="/tmp/bp_x/repo/DEN",
        )

        self.assertEqual(plan.argv[plan.argv.index("-w") + 1], "/workspace/DEN")

    def test_apptainer_clone_honors_nested_cwd(self):
        plan = self._common(
            caps=_caps(apptainer_path="/usr/bin/apptainer"),
            image="nvcr.io/nvidia/pytorch:24.01",
            clone_dir_host="/tmp/bp_x/repo",
            cwd_host="/tmp/bp_x/repo/DEN",
        )

        self.assertEqual(
            plan.argv[plan.argv.index("--pwd") + 1],
            "/workspace/repo/DEN",
        )

    def test_bwrap_clone_honors_nested_cwd(self):
        plan = self._common(
            clone_dir_host="/tmp/bp_x/repo",
            cwd_host="/tmp/bp_x/repo/DEN",
        )

        self.assertEqual(
            plan.argv[plan.argv.index("--chdir") + 1],
            "/workspace/repo/DEN",
        )

    def test_docker_cdi_daemon_gets_cdi_device_flags(self):
        plan = self._common(
            caps=_caps(docker_usable=True, docker_cdi_gpu=True, gpu_indices=(0, 1)),
            image="nvcr.io/nvidia/pytorch:24.01",
            gpu_indices=[1],
        )
        self.assertEqual(plan.backend, iso.BACKEND_DOCKER)
        joined = " ".join(plan.argv)
        self.assertIn("--device nvidia.com/gpu=1", joined)
        self.assertNotIn("--gpus", plan.argv)

    def test_apptainer_image_backend_no_home(self):
        plan = self._common(
            caps=_caps(apptainer_path="/usr/bin/apptainer", gpu_indices=(0,)),
            image="nvcr.io/nvidia/pytorch:24.01",
            registry_auth={"username": "$oauthtoken", "password": "ngc"},
            gpu_indices=[0],
        )
        self.assertEqual(plan.backend, iso.BACKEND_APPTAINER)
        self.assertIn("--no-home", plan.argv)
        self.assertIn("--containall", plan.argv)
        # Registry secret via env, off argv.
        self.assertEqual(plan.env["APPTAINER_DOCKER_PASSWORD"], "ngc")
        self.assertNotIn("ngc", " ".join(plan.argv))

    def test_decline_when_no_boundary(self):
        plan = self._common(caps=_caps())
        self.assertTrue(plan.declined)

    def test_plan_image_no_runtime_declines_by_default(self):
        # image declared, bwrap available, no container runtime, no flag → DECLINE.
        plan = self._common(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True),
            image="nvcr.io/nvidia/pytorch:24.01",
        )
        self.assertTrue(plan.declined)
        self.assertIn("allow_host_fallback", plan.decline_reason or "")

    def test_plan_image_no_runtime_bwrap_with_allow_host_fallback(self):
        # Explicit opt-in: allow_host_fallback=True → imageless bwrap fallback.
        plan = self._common(
            caps=_caps(bwrap_path="/usr/bin/bwrap", userns_ok=True),
            image="nvcr.io/nvidia/pytorch:24.01",
            allow_host_fallback=True,
        )
        self.assertFalse(plan.declined)
        self.assertEqual(plan.backend, iso.BACKEND_BWRAP)


# ── CDI GPU probe ──────────────────────────────────────────────────────────────

class TestProbeDockerCdiGpu(unittest.TestCase):
    """probe_docker_cdi_gpu: daemon reports CDISpecDirs AND a readable spec
    mentions nvidia.com/gpu → True; every failure mode reads as False (legacy
    --gpus behavior preserved on hook-based hosts)."""

    @staticmethod
    def _info(stdout, rc=0):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")

    def test_true_when_spec_dir_has_nvidia_gpu_spec(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "nvidia.yaml"), "w", encoding="utf-8") as fh:
                fh.write("cdiVersion: 0.5.0\nkind: nvidia.com/gpu\ndevices:\n- name: '0'\n")
            with mock.patch.object(iso.subprocess, "run", return_value=self._info(d + "\n")):
                self.assertTrue(iso.probe_docker_cdi_gpu())

    def test_false_when_no_spec_dirs(self):
        # Pre-CDI daemon or cdi feature disabled: empty CDISpecDirs output.
        with mock.patch.object(iso.subprocess, "run", return_value=self._info("")):
            self.assertFalse(iso.probe_docker_cdi_gpu())


class TestProbeContainerGpuPassthrough(unittest.TestCase):
    @staticmethod
    def _info(stdout, rc=0):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")

    def test_docker_cdi_probe_uses_job_argv_and_requires_gpu_output(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="GPU 0: Test GPU (UUID: GPU-1)\n", stderr=""
        )
        caps = _caps(docker_usable=True, docker_cdi_gpu=True, gpu_indices=(0,))
        with mock.patch.dict(os.environ, {"BLUEPRINT_RUNNER_TOKEN": "secret"}), \
             mock.patch.object(iso.subprocess, "run", return_value=completed) as run:
            proof = iso.probe_container_gpu_passthrough(caps, image="probe:image")
        self.assertEqual(proof["docker"]["verified"], True)
        self.assertEqual(proof["docker"]["mode"], "cdi")
        self.assertIn("--device", run.call_args.args[0])
        self.assertNotIn("--gpus", run.call_args.args[0])
        self.assertNotIn("BLUEPRINT_RUNNER_TOKEN", run.call_args.kwargs["env"])

    def test_configured_private_image_is_used_for_retryable_gpu_proof(self):
        """A Docker-Hub-blocked runner can prove its configured project image."""
        caps = _caps(docker_usable=True, gpu_indices=(0,))
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="GPU 0: Project GPU\n", stderr=""
        )
        with mock.patch.dict(
            os.environ,
            {"BLUEPRINT_RUNNER_GPU_PROBE_IMAGE": "registry.example/project/cuda:stable"},
            clear=False,
        ), mock.patch.object(iso.subprocess, "run", return_value=completed) as run:
            proof = iso.refresh_container_gpu_passthrough(caps)

        self.assertTrue(proof.container_gpu_passthrough["docker"]["verified"])
        self.assertIn("registry.example/project/cuda:stable", run.call_args.args[0])
        self.assertNotIn(iso._GPU_PROBE_IMAGE, run.call_args.args[0])

    def test_runtime_presence_without_host_gpu_is_negative_evidence(self):
        proof = iso.probe_container_gpu_passthrough(_caps(podman_usable=True))
        self.assertEqual(
            proof,
            {"podman": {"verified": False, "mode": "hook", "reason": "no_host_gpu"}},
        )

    def test_timeout_force_removes_probe_container(self):
        caps = _caps(docker_usable=True, gpu_indices=(0,))
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[1:3] == ["rm", "-f"]:
                return subprocess.CompletedProcess(args=argv, returncode=0)
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=45)

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            proof = iso.probe_container_gpu_passthrough(caps)

        self.assertEqual(proof["docker"]["reason"], "probe_timeout")
        cleanup = next(call for call in calls if call[1:3] == ["rm", "-f"])
        self.assertRegex(cleanup[3], r"^bp-gpu-passthrough-probe-\d+-[0-9a-f]{8}$")

    def test_probe_names_do_not_collide_after_runner_restart(self):
        """A stale predecessor probe name must not poison the next daemon probe."""
        caps = _caps(docker_usable=True, gpu_indices=(0,))
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="GPU 0: Test GPU\n", stderr=""
        )
        with mock.patch.object(iso.subprocess, "run", return_value=completed) as run:
            iso.probe_container_gpu_passthrough(caps)
            first_name = run.call_args_list[0].args[0][
                run.call_args_list[0].args[0].index("--name") + 1
            ]
            iso.probe_container_gpu_passthrough(caps)
            second_name = run.call_args_list[1].args[0][
                run.call_args_list[1].args[0].index("--name") + 1
            ]

        self.assertNotEqual(first_name, second_name)
        self.assertTrue(first_name.startswith("bp-gpu-passthrough-probe-"))

    def test_zero_exit_without_gpu_is_no_gpu_visible(self):
        caps = _caps(podman_usable=True, gpu_indices=(0,))
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with mock.patch.object(iso.subprocess, "run", return_value=completed):
            proof = iso.probe_container_gpu_passthrough(caps)
        self.assertEqual(
            proof["podman"],
            {"verified": False, "mode": "hook", "reason": "no_gpu_visible"},
        )

    def test_nonzero_exit_retains_exit_reason(self):
        caps = _caps(podman_usable=True, gpu_indices=(0,))
        completed = subprocess.CompletedProcess(
            args=[], returncode=125, stdout="", stderr="hook unavailable"
        )
        with mock.patch.object(iso.subprocess, "run", return_value=completed):
            proof = iso.probe_container_gpu_passthrough(caps)
        self.assertEqual(proof["podman"]["reason"], "probe_exit_125")

    def test_only_recoverable_proof_failures_are_retried(self):
        transient = _caps(docker_usable=True, gpu_indices=(0,))
        transient = iso.replace(
            transient,
            container_gpu_passthrough={
                "docker": {"verified": False, "mode": "hook", "reason": "probe_timeout"}
            },
        )
        no_host_gpu = iso.replace(
            _caps(docker_usable=True),
            container_gpu_passthrough={
                "docker": {"verified": False, "mode": "hook", "reason": "no_host_gpu"}
            },
        )
        self.assertTrue(iso.container_gpu_passthrough_needs_retry(transient))
        self.assertFalse(iso.container_gpu_passthrough_needs_retry(no_host_gpu))

    def test_false_when_spec_dir_missing_or_empty(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nonexistent")
            with mock.patch.object(iso.subprocess, "run", return_value=self._info(missing + "\n")):
                self.assertFalse(iso.probe_docker_cdi_gpu())
            with mock.patch.object(iso.subprocess, "run", return_value=self._info(d + "\n")):
                self.assertFalse(iso.probe_docker_cdi_gpu())

    def test_false_when_specs_lack_nvidia_gpu(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "other.yaml"), "w", encoding="utf-8") as fh:
                fh.write("cdiVersion: 0.5.0\nkind: vendor.example/dev\n")
            with mock.patch.object(iso.subprocess, "run", return_value=self._info(d + "\n")):
                self.assertFalse(iso.probe_docker_cdi_gpu())

    def test_false_on_docker_info_failure(self):
        with mock.patch.object(iso.subprocess, "run", return_value=self._info("", rc=1)):
            self.assertFalse(iso.probe_docker_cdi_gpu())
        with mock.patch.object(iso.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(iso.probe_docker_cdi_gpu())


# ── Project verification boundary probe ──────────────────────────────────────

class TestProjectVerificationProbe(unittest.TestCase):
    def _plan(self):
        return iso.plan_agent_execution(
            caps=_caps(
                bwrap_path="/usr/bin/bwrap",
                userns_ok=True,
                docker_usable=True,
            ),
            agent_binary=["node", "/repo/agent/dist/index.js"],
            workspace_host="/tmp/verification-workspace",
            injected_env={"TASK_ID": "verification"},
            source_env={"PATH": "/usr/bin:/bin"},
            task_type="fix",
            agent_image="registry/agent:latest",
        )

    def test_all_zero_exit_probes_earn_readiness(self):
        plan = self._plan()
        completed = subprocess.CompletedProcess([], 0, "PROJECT_VERIFICATION_V1_OK\n", "")
        with mock.patch.object(iso.subprocess, "run", return_value=completed) as run:
            report = iso.probe_project_verification(plan)

        self.assertTrue(report["ready"])
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["boundary"], {"class": "agent", "backend": "bwrap"})
        self.assertEqual(report["probes"]["ruff"]["exit_code"], 0)
        self.assertEqual(report["probes"]["docker_postgres"]["exit_code"], 0)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertIn(iso.PROJECT_VERIFICATION_VENV, argv)
            self.assertNotIn("/var/run/docker.sock", argv)
            self.assertNotIn("/run/docker.sock", argv)

    def test_real_probe_runs_live_sandbox_postgres_when_installed(self):
        pg_config = shutil.which("pg_config")
        ruff_available = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            capture_output=True,
        ).returncode == 0
        if not pg_config or not ruff_available:
            self.skipTest("functional probe requires pg_config and Ruff")
        bindir = subprocess.run(
            [pg_config, "--bindir"], capture_output=True, text=True, check=True
        ).stdout.strip()
        if not os.path.isfile(os.path.join(bindir, "initdb")):
            self.skipTest("functional probe requires the PostgreSQL server tools")

        completed = subprocess.run(
            [iso.PROJECT_VERIFICATION_PROBE_PATH, "all"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=(completed.stderr or "") + (completed.stdout or ""),
        )
        self.assertIn("PROJECT_VERIFICATION_V1_OK", completed.stdout)

    def test_host_docker_does_not_make_daemonless_bwrap_ready(self):
        plan = self._plan()
        results = [
            subprocess.CompletedProcess([], 0, "PROJECT_VERIFICATION_V1_OK\n", ""),
            subprocess.CompletedProcess([], 127, "", "docker: command not found"),
        ]
        with mock.patch.object(iso.subprocess, "run", side_effect=results) as run:
            report = iso.probe_project_verification(plan)

        self.assertFalse(report["ready"])
        self.assertTrue(report["probes"]["ruff"]["ok"])
        self.assertFalse(report["probes"]["docker_postgres"]["ok"])
        self.assertEqual(report["probes"]["docker_postgres"]["exit_code"], 127)
        self.assertIn("docker: command not found", report["reason"])
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[0], "/usr/bin/bwrap")
            self.assertNotIn("/var/run/docker.sock", argv)
            self.assertNotIn("/run/docker.sock", argv)
            self.assertIn(iso.PROJECT_VERIFICATION_SANDBOX_PATH, argv)

    def test_timeout_is_structured_failure(self):
        plan = self._plan()
        with mock.patch.object(
            iso.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["bwrap"], 3),
        ):
            report = iso.probe_project_verification(
                plan, ruff_timeout=3, docker_postgres_timeout=4
            )

        self.assertFalse(report["ready"])
        self.assertIsNone(report["probes"]["ruff"]["exit_code"])
        self.assertEqual(
            report["probes"]["ruff"]["reason"], "ruff probe timed out after 3s"
        )
        self.assertEqual(
            report["probes"]["docker_postgres"]["reason"],
            "docker-postgres probe timed out after 4s",
        )

    def test_signal_terminated_probe_is_nonzero_failure_evidence(self):
        plan = self._plan()
        results = [
            subprocess.CompletedProcess([], -9, "", "killed"),
            subprocess.CompletedProcess([], 0, "PROJECT_VERIFICATION_V1_OK\n", ""),
        ]
        with mock.patch.object(iso.subprocess, "run", side_effect=results):
            report = iso.probe_project_verification(plan)

        self.assertFalse(report["ready"])
        self.assertFalse(report["probes"]["ruff"]["ok"])
        self.assertEqual(report["probes"]["ruff"]["exit_code"], 137)
        self.assertIn("killed", report["probes"]["ruff"]["reason"])


# ── Capabilities inventory (heartbeat backend-availability map) ────────────────

class TestCapabilitiesInventory(unittest.TestCase):
    _EXPECTED_KEYS = {
        "bwrap_ok",
        "userns_ok",
        "docker_usable",
        "docker_cdi_gpu",
        "podman_usable",
        "apptainer_ok",
        "enroot_ok",
        "systemd_run_ok",
    }

    def test_all_flags_true(self):
        caps = _caps(
            bwrap_path="/usr/bin/bwrap",
            userns_ok=True,
            docker_usable=True,
            docker_cdi_gpu=True,
            podman_usable=True,
            apptainer_path="/usr/bin/apptainer",
            enroot_path="/usr/bin/enroot",
            systemd_run_path="/usr/bin/systemd-run",
        )
        inv = iso.capabilities_inventory(caps)
        self.assertTrue(all(value for key, value in inv.items() if key != "enroot_ok"))
        self.assertFalse(inv["enroot_ok"])
        self.assertEqual(set(inv), self._EXPECTED_KEYS)

    def test_bwrap_ok_reflects_userns_property_semantics(self):
        # bwrap binary present but userns unavailable → bwrap_ok is False (the
        # Capabilities.bwrap_ok property couples the binary AND userns), while
        # userns_ok is independently reported False.
        caps = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=False)
        inv = iso.capabilities_inventory(caps)
        self.assertFalse(inv["bwrap_ok"])
        self.assertFalse(inv["userns_ok"])

    def test_path_only_backends_use_bool_of_path(self):
        caps = _caps(
            apptainer_path="/usr/bin/apptainer",
            enroot_path="/usr/bin/enroot",
            systemd_run_path="/usr/bin/systemd-run",
        )
        inv = iso.capabilities_inventory(caps)
        self.assertTrue(inv["apptainer_ok"])
        self.assertFalse(inv["enroot_ok"])
        self.assertTrue(inv["systemd_run_ok"])

    def test_empty_caps_all_false(self):
        inv = iso.capabilities_inventory(_caps())
        self.assertEqual(set(inv), self._EXPECTED_KEYS)
        self.assertFalse(any(inv.values()))

    def test_json_serializable_with_exact_keys(self):
        import json

        caps = _caps(docker_usable=True)
        inv = iso.capabilities_inventory(caps)
        # Round-trips through JSON and every value is a real bool.
        reloaded = json.loads(json.dumps(inv))
        self.assertEqual(set(reloaded), self._EXPECTED_KEYS)
        for value in reloaded.values():
            self.assertIsInstance(value, bool)


# ── LLM CLI probe ────────────────────────────────────────────────────────────

class TestProbeLlmClis(unittest.TestCase):
    """probe_llm_clis: checks shutil.which + subprocess.run for each LLM CLI."""

    @staticmethod
    def _which_side_effect(name):
        mapping = {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex", "gemini": "/usr/bin/gemini"}
        return mapping.get(name)

    def test_all_present_with_versions(self):
        with mock.patch.object(iso, "_which", side_effect=self._which_side_effect):
            with mock.patch.object(
                iso.subprocess, "run",
                side_effect=lambda args, **kw: subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=(
                        "codex-cli 0.144.1\n"
                        if args[0].endswith("codex")
                        else f"{args[0]} 0.2.0\n"
                    ),
                    stderr="",
                ),
            ):
                result = iso.probe_llm_clis()
        self.assertEqual(set(result), {"claude", "codex", "gemini"})
        for name in ("claude", "codex", "gemini"):
            self.assertTrue(result[name]["present"])
            self.assertTrue(result[name]["executable_found"])
            self.assertTrue(result[name]["healthy"])
            self.assertIsNone(result[name]["reason"])
        self.assertIn("0.2.0", result["claude"]["version"])
        self.assertIn("0.144.1", result["codex"]["version"])
        self.assertIn("0.2.0", result["gemini"]["version"])

    def test_explicit_probe_environment_is_used_for_every_version_process(self):
        safe_env = {"PATH": "/safe/bin", "HOME": "/safe/home"}
        with (
            mock.patch.object(
                iso, "_which", side_effect=self._which_side_effect
            ),
            mock.patch.object(
                iso.subprocess,
                "run",
                side_effect=lambda args, **kw: subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=(
                        "codex-cli 0.144.1"
                        if args[0].endswith("codex")
                        else "cli 1.0.0"
                    ),
                    stderr="",
                ),
            ) as run,
        ):
            iso.probe_llm_clis(env=safe_env)
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"], safe_env)

    def test_all_absent(self):
        with mock.patch.object(iso, "_which", return_value=None):
            result = iso.probe_llm_clis()
        self.assertEqual(set(result), {"claude", "codex", "gemini"})
        for name in ("claude", "codex", "gemini"):
            self.assertFalse(result[name]["present"])
            self.assertFalse(result[name]["executable_found"])
            self.assertFalse(result[name]["healthy"])
            self.assertEqual(result[name]["reason"], "not_found")
            self.assertEqual(result[name]["version"], "")

    def test_mixed_present_absent(self):
        def which_mixed(name):
            return "/usr/bin/claude" if name == "claude" else None

        with mock.patch.object(iso, "_which", side_effect=which_mixed):
            with mock.patch.object(
                iso.subprocess, "run",
                return_value=subprocess.CompletedProcess(
                    args=["/usr/bin/claude", "--version"], returncode=0,
                    stdout="claude 0.3.0\n", stderr=""
                ),
            ):
                result = iso.probe_llm_clis()
        self.assertTrue(result["claude"]["present"])
        self.assertEqual(result["claude"]["version"], "claude 0.3.0")
        self.assertFalse(result["codex"]["present"])
        self.assertEqual(result["codex"]["version"], "")
        self.assertFalse(result["gemini"]["present"])
        self.assertEqual(result["gemini"]["version"], "")

    def test_present_but_version_subprocess_fails(self):
        with mock.patch.object(iso, "_which", side_effect=self._which_side_effect):
            with mock.patch.object(
                iso.subprocess, "run",
                return_value=subprocess.CompletedProcess(
                    args=["/usr/bin/codex", "--version"], returncode=1,
                    stdout="", stderr="error"
                ),
            ):
                result = iso.probe_llm_clis()
        self.assertFalse(result["codex"]["present"])
        self.assertTrue(result["codex"]["executable_found"])
        self.assertFalse(result["codex"]["healthy"])
        self.assertEqual(result["codex"]["reason"], "version_probe_failed")
        self.assertEqual(result["codex"]["version"], "")

    def test_present_but_version_subprocess_oserror(self):
        def which_all(name):
            return "/usr/bin/claude"

        with mock.patch.object(iso, "_which", side_effect=which_all):
            with mock.patch.object(
                iso.subprocess, "run",
                side_effect=OSError("exec format error"),
            ):
                result = iso.probe_llm_clis()
        self.assertFalse(result["claude"]["present"])
        self.assertTrue(result["claude"]["executable_found"])
        self.assertFalse(result["claude"]["healthy"])
        self.assertEqual(result["claude"]["reason"], "version_probe_error")
        self.assertEqual(result["claude"]["version"], "")

    def test_codex_missing_native_optional_package_is_explicit_and_ineligible(self):
        with mock.patch.object(iso, "_which", side_effect=self._which_side_effect):
            with mock.patch.object(
                iso.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["/usr/bin/codex", "--version"], returncode=1,
                    stdout="",
                    stderr=(
                        "Error: Missing optional dependency "
                        "@openai/codex-linux-x64. Reinstall Codex."
                    ),
                ),
            ):
                result = iso.probe_llm_clis()
        self.assertTrue(result["codex"]["executable_found"])
        self.assertFalse(result["codex"]["healthy"])
        self.assertFalse(result["codex"]["present"])
        self.assertEqual(result["codex"]["reason"], "missing_native_package")

    def test_codex_below_minimum_is_ineligible_even_when_runnable(self):
        with mock.patch.object(iso, "_which", side_effect=self._which_side_effect):
            with mock.patch.object(
                iso.subprocess,
                "run",
                side_effect=lambda args, **kw: subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=(
                        "codex-cli 0.143.0\n"
                        if args[0].endswith("codex")
                        else f"{args[0]} 1.0.0\n"
                    ),
                    stderr="",
                ),
            ):
                result = iso.probe_llm_clis()
        self.assertTrue(result["codex"]["executable_found"])
        self.assertFalse(result["codex"]["healthy"])
        self.assertFalse(result["codex"]["present"])
        self.assertEqual(result["codex"]["version"], "codex-cli 0.143.0")
        self.assertEqual(result["codex"]["reason"], "unsupported_version")

    def test_container_probe_uses_image_entrypoint_not_host_path(self):
        def fake_run(args, **kwargs):
            cli = args[args.index("--entrypoint") + 1]
            version = "codex-cli 0.144.1" if cli == "codex" else f"{cli} 1.0.0"
            return subprocess.CompletedProcess(args, 0, stdout=version, stderr="")

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run) as run:
            result = iso.probe_container_llm_clis("agent:test")
        self.assertTrue(result["codex"]["healthy"])
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0:2], ["docker", "run"])
            self.assertIn("agent:test", call.args[0])

    def test_container_probe_broken_codex_is_ineligible_despite_host(self):
        def fake_run(args, **kwargs):
            cli = args[args.index("--entrypoint") + 1]
            if cli == "codex":
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr=(
                        "Missing optional dependency @openai/codex-linux-x64"
                    ),
                )
            return subprocess.CompletedProcess(args, 0, stdout=f"{cli} 1.0.0", stderr="")

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            result = iso.probe_container_llm_clis("agent:broken")
        self.assertFalse(result["codex"]["present"])
        self.assertEqual(result["codex"]["reason"], "missing_native_package")

    def test_empty_caps_on_capabilities_dataclass(self):
        """llm_clis defaults to {} on a freshly-constructed Capabilities."""
        caps = iso.Capabilities()
        self.assertEqual(caps.llm_clis, {})
        # Verify the dataclass still accepts its existing positional args.
        caps2 = iso.Capabilities(bwrap_path="/usr/bin/bwrap", userns_ok=True)
        self.assertEqual(caps2.llm_clis, {})


# ── GPU sandbox probe ─────────────────────────────────────────────────────────

class TestProbeGpuInSandbox(unittest.TestCase):
    """probe_gpu_in_sandbox: runs nvidia-smi -L inside the plan's sandbox argv.

    Tests use bwrap plans with a real-shaped argv so the probe logic can
    substitute the command correctly without actually launching bwrap.
    """

    def _bwrap_plan(self, gpu_indices=None):
        """Build a minimal bwrap ExecutionPlan with GPU assignment."""
        indices = gpu_indices if gpu_indices is not None else [1]
        caps = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, gpu_indices=tuple(indices))
        return iso.plan_compute_execution(
            caps=caps,
            command="python train.py",
            image=None,
            registry_auth=None,
            workspace_host="/tmp/bp_x",
            result_file_host="/tmp/bp_x/result.json",
            artifact_dir_host="/tmp/bp_x/artifacts",
            clone_dir_host=None,
            cwd_host=None,
            job_env={},
            gpu_indices=indices,
            container_name="bp-test",
            source_env={"PATH": "/usr/bin"},
        )

    def test_probe_success_returns_true_with_output(self):
        """Probe returns (True, output) when nvidia-smi -L succeeds with GPU list."""
        plan = self._bwrap_plan([0])
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="GPU 0: NVIDIA A100 (UUID: GPU-abc123)\n",
            stderr="",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertTrue(ok)
        self.assertIn("GPU 0", detail)

    def test_probe_nonzero_exit_returns_false(self):
        """Probe returns (False, reason) when nvidia-smi exits non-zero."""
        plan = self._bwrap_plan([1])
        fake = subprocess.CompletedProcess(
            args=[], returncode=6,
            stdout="",
            stderr="GPU access blocked by the operating system",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIn("6", detail)

    def test_probe_timeout_returns_false(self):
        """Probe returns (False, reason) on subprocess timeout."""
        plan = self._bwrap_plan([0])
        with mock.patch.object(
            iso.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=30)
        ):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_probe_oserror_returns_false(self):
        """Probe returns (False, reason) when the sandbox binary cannot be launched."""
        plan = self._bwrap_plan([0])
        with mock.patch.object(iso.subprocess, "run", side_effect=OSError("bwrap not found")):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIn("launch", detail)

    def test_probe_uses_same_bwrap_argv_prefix(self):
        """Probe argv shares the same bwrap flags as the real plan (same sandbox)."""
        plan = self._bwrap_plan([2])
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="GPU 0: NVIDIA V100\n",
                stderr="",
            )

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            iso.probe_gpu_in_sandbox(plan)

        probe_argv = captured["argv"]
        # Same bwrap binary starts the probe.
        self.assertEqual(probe_argv[0], "/usr/bin/bwrap")
        # Terminal command is nvidia-smi -L, not the original python train.py.
        self.assertEqual(probe_argv[-2:], ["-lc", "nvidia-smi -L"])
        # Original plan's bash invocation preserved.
        self.assertIn("bash", probe_argv)

    def test_probe_replaces_only_command_not_env(self):
        """Probe env is the same as the real plan's env (same CUDA_VISIBLE_DEVICES etc.)."""
        plan = self._bwrap_plan([1])
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="GPU 0: NVIDIA T4\n",
                stderr="",
            )

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            iso.probe_gpu_in_sandbox(plan)

        # CUDA_VISIBLE_DEVICES must be set (1 device → renumbered from 0).
        self.assertEqual(captured["env"].get("CUDA_VISIBLE_DEVICES"), "0")
        # Host GPU index preserved for driver hooks.
        self.assertEqual(captured["env"].get("NVIDIA_VISIBLE_DEVICES"), "1")

    def test_probe_declined_plan_shape_error(self):
        """Probe returns (False, reason) for a declined plan without crashing."""
        plan = iso.ExecutionPlan.decline("no bwrap")
        ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)

    def test_probe_non_zero_gpu_index_cuda_visible_devices_renumbered(self):
        """Host GPU index 2 → CUDA_VISIBLE_DEVICES=0 inside sandbox (renumbering)."""
        plan = self._bwrap_plan([2])
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="GPU 0: NVIDIA A100-SXM4\n",
                stderr="",
            )

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            ok, _ = iso.probe_gpu_in_sandbox(plan)

        self.assertTrue(ok)
        # Single exposed device → always renumbered to 0 inside sandbox.
        self.assertEqual(captured["env"].get("CUDA_VISIBLE_DEVICES"), "0")
        # Host index preserved so driver hooks can find the physical device.
        self.assertEqual(captured["env"].get("NVIDIA_VISIBLE_DEVICES"), "2")

    def test_probe_empty_output_returns_false(self):
        """Probe returns (False, reason) when nvidia-smi exits 0 but prints nothing."""
        plan = self._bwrap_plan([0])
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIn("no output", detail)


# ── Arch gate: GPU listed by the driver, unusable by the image ───────────────

# The real banner an NGC pytorch image prints on a Volta (sm_70) host when the
# image dropped Volta support. Note it goes to STDERR, the GPU is still listed
# on stdout by nvidia-smi (which reads the driver, not the image's CUDA
# runtime), and the container exits 0.
NGC_VOLTA_BANNER = (
    "ERROR: Detected NVIDIA Tesla V100-SXM2-16GB GPU, which is not supported "
    "by this container\n"
    "ERROR: No supported GPU(s) detected to run this container\n"
)


class TestProbeGpuArchGate(unittest.TestCase):
    """The probe must fail an image whose CUDA runtime cannot use the host GPU.

    Regression: task 9b8093fd declared a T4, landed on a V100 (sm_70) DGX, and
    requested nvcr.io/nvidia/pytorch:25.01-py3 — which dropped Volta. The probe
    passed (exit 0, GPU listed), the job burned a ~14-min image pull plus a full
    run, produced no metric, and failed the unmeasured-=-failure gate with a
    confusing reason. The signal was on STDERR the whole time.
    """

    def _docker_plan(self, gpu_indices=(0,), container_name="bp-job1"):
        caps = _caps(docker_usable=True, gpu_indices=tuple(gpu_indices))
        return iso.plan_compute_execution(
            caps=caps,
            command="python train.py",
            image="nvcr.io/nvidia/pytorch:25.01-py3",
            registry_auth=None,
            workspace_host="/tmp/bp_x",
            result_file_host="/tmp/bp_x/result.json",
            artifact_dir_host="/tmp/bp_x/artifacts",
            clone_dir_host=None,
            cwd_host=None,
            job_env={},
            gpu_indices=list(gpu_indices),
            container_name=container_name,
            source_env={"PATH": "/usr/bin"},
        )

    def test_marker_on_stderr_fails_probe_despite_exit0_and_gpu_listed(self):
        """THE regression: exit 0 + GPU on stdout + banner on stderr → FAIL.

        Every signal the old probe looked at says healthy. Only stderr says the
        image cannot use this GPU.
        """
        plan = self._docker_plan()
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU 0: Tesla V100-SXM2-16GB (UUID: GPU-abc123)\n",
            stderr=NGC_VOLTA_BANNER,
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, detail = iso.probe_gpu_in_sandbox(plan)

        self.assertFalse(ok, "arch-incompatible image must not pass the GPU probe")
        self.assertIn("no usable GPU", detail)
        # The reason names the mismatch, not a generic "no metric" downstream.
        self.assertIn("compatibility", detail)
        self.assertIn("V100", detail)

    def test_marker_on_stdout_also_fails_probe(self):
        """Entrypoints that print the banner to stdout are caught identically."""
        plan = self._docker_plan()
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU 0: Tesla V100\n" + NGC_VOLTA_BANNER,
            stderr="",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIn("no usable GPU", detail)

    def test_marker_match_is_case_insensitive(self):
        """Markers are matched against a lowercased haystack, not verbatim."""
        plan = self._docker_plan()
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU 0: Tesla V100\n",
            stderr="NO SUPPORTED GPU(S) DETECTED TO RUN THIS CONTAINER",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, _ = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)

    def test_clean_probe_with_benign_stderr_still_passes(self):
        """A healthy GPU still PASSES — stderr noise alone must not fail the gate.

        Guards against over-triggering: the gate keys on the markers, not on
        stderr being non-empty.
        """
        plan = self._docker_plan()
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-def456)\n",
            stderr="WARNING: The NVIDIA Driver was not detected as up to date.\n",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertTrue(ok, "a compatible GPU must still pass the probe")
        self.assertIn("A100", detail)

    def test_nvidia_smi_device_visibility_check_is_retained(self):
        """The original empty-GPU-list check still fails, marker or no marker."""
        plan = self._docker_plan()
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=fake):
            ok, detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIn("no output", detail)

    def test_no_usable_gpu_marker_helper(self):
        """The helper returns the matched marker, or None on clean text."""
        self.assertIsNotNone(iso.no_usable_gpu_marker(NGC_VOLTA_BANNER))
        self.assertIsNone(
            iso.no_usable_gpu_marker("GPU 0: NVIDIA A100-SXM4-40GB\n")
        )
        self.assertIsNone(iso.no_usable_gpu_marker(""))

    def test_probe_detail_distinguishes_conclusive_from_inconclusive_failure(self):
        """The caller's fatal/non-fatal split reads the DETAIL string — pin it.

        local_runner decides whether a probe failure is fatal by re-scanning the
        returned detail with no_usable_gpu_marker(). An arch mismatch is
        conclusive (the container said it cannot use this GPU) and must always
        be fatal; a timeout is INconclusive (the probe could not run) and must
        not kill a job that never asked for a GPU. If the detail strings stopped
        carrying that distinction, the split would silently invert.
        """
        plan = self._docker_plan()

        arch = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="GPU 0: Tesla V100\n", stderr=NGC_VOLTA_BANNER,
        )
        with mock.patch.object(iso.subprocess, "run", return_value=arch):
            ok, arch_detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIsNotNone(
            iso.no_usable_gpu_marker(arch_detail),
            "arch-mismatch detail must be recognisable as CONCLUSIVE — it is what "
            "makes the failure fatal even for a job that declared no accelerator",
        )

        def timeout_run(argv, **kwargs):
            if "rm" in argv:
                return subprocess.CompletedProcess(args=[], returncode=0)
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=30)

        with mock.patch.object(iso.subprocess, "run", side_effect=timeout_run):
            ok, timeout_detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIsNone(
            iso.no_usable_gpu_marker(timeout_detail),
            "a timeout must NOT read as an arch mismatch — otherwise a cold pull "
            "or a slow host would fatally fail a CPU-only containerized job",
        )

        missing = subprocess.CompletedProcess(
            args=[], returncode=127,
            stdout="", stderr="bash: nvidia-smi: command not found\n",
        )
        with mock.patch.object(iso.subprocess, "run", return_value=missing):
            ok, missing_detail = iso.probe_gpu_in_sandbox(plan)
        self.assertFalse(ok)
        self.assertIsNone(
            iso.no_usable_gpu_marker(missing_detail),
            "a missing nvidia-smi is inconclusive, not an arch mismatch",
        )


class TestDockerAgentOptInIsNotASilentNoOp(unittest.TestCase):
    """`--docker-agent` must actually select docker, not quietly fall to bwrap.

    blueprint_runner only resolves an agent_image when the operator explicitly
    opts in (--docker-agent / BLUEPRINT_AGENT_BACKEND=docker / --pull), so a
    truthy agent_image IS the opt-in. Checking bwrap FIRST made the flag a no-op
    on every host that has bwrap — the operator asked for a prod-parity container
    and silently got bwrap. This class pins the decision ORDER, which is the only
    thing standing between the opt-in and a silent no-op.
    """

    def test_optin_wins_over_bwrap_when_both_are_available(self):
        both = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=True)
        self.assertEqual(
            iso.decide_agent_backend(
                both, True, agent_image="reg/agent:v1", task_type="research"
            ),
            iso.BACKEND_DOCKER,
            "an explicit agent_image opt-in must beat bwrap — otherwise "
            "--docker-agent does nothing at all on a bwrap-capable host",
        )

    def test_no_optin_is_byte_identical_to_the_historical_bwrap_table(self):
        """The CRITICAL invariant: agent_image falsy → docker branch inert."""
        both = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=True)
        self.assertEqual(iso.decide_agent_backend(both, True), iso.BACKEND_BWRAP)
        self.assertEqual(
            iso.decide_agent_backend(both, True, task_type="research"),
            iso.BACKEND_BWRAP,
            "docker must NEVER win merely because docker is usable",
        )
        no_bwrap_no_docker = _caps(docker_usable=False)
        self.assertEqual(iso.decide_agent_backend(no_bwrap_no_docker, True), iso.DECLINE)

    def test_optin_still_gated_by_task_type(self):
        """The write/heavy slice never rides the container path, flag or not."""
        both = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=True)
        self.assertEqual(
            iso.decide_agent_backend(
                both, True, agent_image="reg/agent:v1", task_type="code_change"
            ),
            iso.BACKEND_BWRAP,
            "an ineligible task type must fall back to bwrap even when opted in",
        )

    def test_optin_falls_back_to_bwrap_when_docker_is_unusable(self):
        no_docker = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, docker_usable=False)
        self.assertEqual(
            iso.decide_agent_backend(
                no_docker, True, agent_image="reg/agent:v1", task_type="research"
            ),
            iso.BACKEND_BWRAP,
        )


class TestPullContainerImage(unittest.TestCase):
    """The probe must not have to win a race against a cold image pull.

    docker's default pull policy is `missing`, so the probe's own `docker run`
    IS the pull. A 14-minute NGC pull against a 30s probe timeout means the job
    dies reporting "GPU probe timed out" — never reaching the arch check the
    probe exists to perform. Pulling explicitly first makes the probe's run warm.
    """

    def _docker_plan(self):
        caps = _caps(docker_usable=True, gpu_indices=(0,))
        return iso.plan_compute_execution(
            caps=caps,
            command="python train.py",
            image="nvcr.io/nvidia/pytorch:25.01-py3",
            registry_auth=None,
            workspace_host="/tmp/bp_x",
            result_file_host="/tmp/bp_x/result.json",
            artifact_dir_host="/tmp/bp_x/artifacts",
            clone_dir_host=None,
            cwd_host=None,
            job_env={},
            gpu_indices=[0],
            container_name="bp-job1",
            source_env={"PATH": "/usr/bin"},
        )

    def test_pull_invokes_docker_pull_with_the_image(self):
        plan = self._docker_plan()
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            ok, detail = iso.pull_container_image(plan, "nvcr.io/nvidia/pytorch:25.01-py3")

        self.assertTrue(ok)
        self.assertEqual(
            calls[0], ["docker", "pull", "nvcr.io/nvidia/pytorch:25.01-py3"]
        )

    def test_pull_timeout_is_not_fatal(self):
        """A failed pull is reported, never raised — the run surfaces the error."""
        plan = self._docker_plan()

        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=1800)

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            ok, detail = iso.pull_container_image(plan, "img:tag")

        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_pull_is_a_noop_without_an_image_or_on_bwrap(self):
        plan = self._docker_plan()
        ok, detail = iso.pull_container_image(plan, None)
        self.assertTrue(ok)
        self.assertIn("no image", detail)

        caps = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, gpu_indices=(0,))
        bwrap_plan = iso.plan_compute_execution(
            caps=caps,
            command="python train.py",
            image=None,
            registry_auth=None,
            workspace_host="/tmp/bp_x",
            result_file_host="/tmp/bp_x/result.json",
            artifact_dir_host="/tmp/bp_x/artifacts",
            clone_dir_host=None,
            cwd_host=None,
            job_env={},
            gpu_indices=[0],
            container_name="bp-job1",
            source_env={"PATH": "/usr/bin"},
        )
        with mock.patch.object(iso.subprocess, "run") as run_mock:
            ok, _ = iso.pull_container_image(bwrap_plan, "img:tag")
        self.assertTrue(ok)
        run_mock.assert_not_called()


class TestProbeContainerNameIsolation(unittest.TestCase):
    """The probe container must never hold the real run's container name.

    `--rm` does not protect us: on TimeoutExpired Python kills the docker CLI
    *client*, not the container. The container keeps running, `--rm` never
    fires, the name stays held, and the REAL run then dies with "name already
    in use".
    """

    def _docker_plan(self, container_name="bp-job1"):
        caps = _caps(docker_usable=True, gpu_indices=(0,))
        return iso.plan_compute_execution(
            caps=caps,
            command="python train.py",
            image="nvcr.io/nvidia/pytorch:25.01-py3",
            registry_auth=None,
            workspace_host="/tmp/bp_x",
            result_file_host="/tmp/bp_x/result.json",
            artifact_dir_host="/tmp/bp_x/artifacts",
            clone_dir_host=None,
            cwd_host=None,
            job_env={},
            gpu_indices=[0],
            container_name=container_name,
            source_env={"PATH": "/usr/bin"},
        )

    def test_probe_container_name_differs_from_real_run(self):
        plan = self._docker_plan(container_name="bp-9b8093fd0000")
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="GPU 0: Tesla V100\n", stderr="",
            )

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            iso.probe_gpu_in_sandbox(plan)

        probe_argv = captured["argv"]
        name_idx = probe_argv.index("--name")
        probe_name = probe_argv[name_idx + 1]

        # The real run's name is still what the plan carries...
        self.assertEqual(plan.container_name, "bp-9b8093fd0000")
        # ...and the probe must NOT reuse it.
        self.assertNotEqual(probe_name, plan.container_name)
        self.assertEqual(probe_name, "bp-9b8093fd0000" + iso.PROBE_NAME_SUFFIX)
        # The rest of the sandbox is still identical (same image, same GPU flags).
        self.assertIn("nvcr.io/nvidia/pytorch:25.01-py3", probe_argv)
        self.assertIn("device=0", probe_argv)

    def test_probe_timeout_force_removes_the_probe_container(self):
        """A timed-out probe force-removes ITS OWN container, never the real name."""
        plan = self._docker_plan(container_name="bp-job7")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "rm" in argv:
                return subprocess.CompletedProcess(args=[], returncode=0)
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=30)

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            ok, detail = iso.probe_gpu_in_sandbox(plan)

        self.assertFalse(ok)
        self.assertIn("timed out", detail)
        rm_calls = [c for c in calls if "rm" in c]
        self.assertEqual(len(rm_calls), 1, "timed-out probe must force-remove its container")
        self.assertEqual(rm_calls[0], ["docker", "rm", "-f", "bp-job7" + iso.PROBE_NAME_SUFFIX])
        # Never force-remove the real run's container.
        self.assertNotIn("bp-job7", rm_calls[0])

    def test_probe_cleanup_failure_is_swallowed(self):
        """A failing `docker rm` must not turn into a raised exception."""
        plan = self._docker_plan()

        def fake_run(argv, **kwargs):
            if "rm" in argv:
                raise OSError("docker daemon gone")
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=30)

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            ok, detail = iso.probe_gpu_in_sandbox(plan)

        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_bwrap_timeout_attempts_no_container_removal(self):
        """bwrap has no container to remove — no stray `rm` subprocess."""
        caps = _caps(bwrap_path="/usr/bin/bwrap", userns_ok=True, gpu_indices=(0,))
        plan = iso.plan_compute_execution(
            caps=caps,
            command="python train.py",
            image=None,
            registry_auth=None,
            workspace_host="/tmp/bp_x",
            result_file_host="/tmp/bp_x/result.json",
            artifact_dir_host="/tmp/bp_x/artifacts",
            clone_dir_host=None,
            cwd_host=None,
            job_env={},
            gpu_indices=[0],
            container_name="bp-job1",
            source_env={"PATH": "/usr/bin"},
        )
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=30)

        with mock.patch.object(iso.subprocess, "run", side_effect=fake_run):
            ok, _ = iso.probe_gpu_in_sandbox(plan)

        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "bwrap probe must not attempt a container rm")


if __name__ == "__main__":
    unittest.main()
