"""Focused contract tests for the independent LiveKit host-role commands."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).parent))
import blueprint_runner as br


class LiveKitCliRoutingTests(unittest.TestCase):
    def test_install_requires_system_semantics_at_dispatch(self) -> None:
        with patch.object(br, "do_livekit_install", return_value=17) as install:
            self.assertEqual(br.main(["livekit-install"]), 17)
        install.assert_called_once_with(system=False, dry_run=False)

    def test_preflight_and_status_receive_system_flag(self) -> None:
        with patch.object(br, "do_livekit_preflight", return_value=0) as preflight:
            self.assertEqual(br.main(["livekit-preflight", "--system"]), 0)
        preflight.assert_called_once_with(system=True)

        with patch.object(br, "do_livekit_status", return_value=0) as status:
            self.assertEqual(br.main(["livekit-status", "--system"]), 0)
        status.assert_called_once_with(system=True)

    def test_wsl_is_unconditionally_rejected(self) -> None:
        with patch.object(br.sys, "platform", "linux"), patch.object(br, "_is_wsl_host", return_value=True):
            self.assertFalse(br._livekit_host_is_supported())

    def test_rendered_system_unit_has_root_config_and_absolute_gcloud(self) -> None:
        with patch.object(br, "_livekit_requirements", return_value=("/bin/bash", "/usr/bin/docker", "/usr/bin/gcloud", "/usr/bin/curl", "/usr/bin/openssl")):
            unit = br._render_livekit_unit()
        self.assertIn("User=root", unit)
        self.assertIn("BLUEPRINT_LIVEKIT_CONFIG_DIR=/etc/blueprint-livekit", unit)
        self.assertIn("BLUEPRINT_LIVEKIT_GCLOUD=/usr/bin/gcloud", unit)
        self.assertIn("CLOUDSDK_CONFIG=/var/cache/blueprint-livekit-gcloud", unit)
        self.assertNotIn("@", unit)

    def test_gateway_role_detects_marker_or_active_unit(self) -> None:
        inactive = type("Result", (), {"returncode": 3})()
        active = type("Result", (), {"returncode": 0})()
        with patch.object(br.os.path, "isfile", return_value=True):
            self.assertTrue(br.livekit_gateway_role_is_selected())
        with patch.object(br.os.path, "isfile", return_value=False), \
             patch.object(br.sys, "platform", "linux"):
            self.assertFalse(br.livekit_gateway_role_is_selected(_run=lambda *args, **kwargs: inactive))
            self.assertTrue(br.livekit_gateway_role_is_selected(_run=lambda *args, **kwargs: active))

    def test_preflight_rejects_non_system_before_host_checks(self) -> None:
        self.assertEqual(br.do_livekit_preflight(system=False), 1)

    def test_preflight_rejects_an_active_system_runner(self) -> None:
        completed = type("Result", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})()
        with patch.object(br.sys, "platform", "linux"), \
             patch.object(br, "_is_wsl_host", return_value=False), \
             patch.object(br, "_systemd_available", return_value=True), \
             patch.object(br, "_systemctl", return_value=completed):
            self.assertEqual(br.do_livekit_preflight(system=True), 1)

    def test_preflight_rejects_any_local_runner_process(self) -> None:
        inactive = type("Result", (), {"returncode": 3, "stdout": "inactive\n", "stderr": ""})()
        with patch.object(br.sys, "platform", "linux"), \
             patch.object(br, "_is_wsl_host", return_value=False), \
             patch.object(br, "_systemd_available", return_value=True), \
             patch.object(br, "_systemctl", return_value=inactive), \
             patch.object(br, "_active_local_runner_pids", return_value=["1234"]):
            self.assertEqual(br.do_livekit_preflight(system=True), 1)

    def test_preflight_checks_rendered_turn_contract_before_docker(self) -> None:
        inactive = type("Result", (), {"returncode": 3, "stdout": "inactive\n", "stderr": ""})()
        success = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        run = Mock(return_value=success)
        with patch.object(br.sys, "platform", "linux"), \
             patch.object(br, "_is_wsl_host", return_value=False), \
             patch.object(br, "_systemd_available", return_value=True), \
             patch.object(br, "_systemctl", return_value=inactive), \
             patch.object(br, "_active_local_runner_pids", return_value=[]), \
             patch.object(br, "_livekit_requirements", return_value=("/bin/bash", "/usr/bin/docker", "/usr/bin/gcloud", "/usr/bin/curl", "/usr/bin/openssl")), \
             patch.object(br, "_livekit_config_is_ready", return_value=True):
            self.assertEqual(br.do_livekit_preflight(system=True, _run=run), 0)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["/bin/bash", br.os.path.join(br._LIVEKIT_DIR, "bin", "render-livekit-secrets"), "--check"],
        )
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/docker", "info"])


if __name__ == "__main__":
    unittest.main()
