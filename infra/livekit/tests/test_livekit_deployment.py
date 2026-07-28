"""Static guardrails for the checked-in LiveKit deployment contract."""

from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class LiveKitDeploymentContractTests(unittest.TestCase):
    def test_images_are_digest_pinned_and_networking_separates_turn_tls(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("livekit/livekit-server:v1.9.12@sha256:", compose)
        self.assertIn("caddy:2.10.2@sha256:", compose)
        self.assertEqual(compose.count("network_mode: host"), 1)
        self.assertIn('"5349:${LIVEKIT_TURN_TLS_PORT:-443}/tcp"', compose)
        self.assertIn('"50000-60000:50000-60000/udp"', compose)
        self.assertNotIn("privileged:", compose)

    def test_runtime_secrets_are_not_environment_variables(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        renderer = (ROOT / "bin" / "render-livekit-secrets").read_text(encoding="utf-8")
        self.assertNotIn("LIVEKIT_CONFIG_SECRET", compose)
        self.assertIn("versions/([1-9][0-9]*)", renderer)
        self.assertIn('"$GCLOUD_BIN" secrets versions access', renderer)
        self.assertIn("install -m 0400", renderer)

    def test_linux_runtime_files_are_lf_only(self) -> None:
        attributes = (ROOT.parents[1] / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("infra/livekit/bin/* text eol=lf", attributes)
        self.assertIn(
            "scripts/blueprint-livekit-system.service text eol=lf",
            attributes,
        )

    def test_turn_domain_is_checked_against_the_tcp_proxy_host(self) -> None:
        config = (ROOT / "livekit.yaml.example").read_text(encoding="utf-8")
        renderer = (ROOT / "bin" / "render-livekit-secrets").read_text(encoding="utf-8")
        runner = (ROOT.parents[1] / "scripts" / "blueprint_runner.py").read_text(encoding="utf-8")
        self.assertIn("domain: turn.example.com", config)
        self.assertIn("LIVEKIT_TURN_HOST", renderer)
        self.assertIn('LIVEKIT_TURN_TLS_PORT="${LIVEKIT_TURN_TLS_PORT:-443}"', renderer)
        self.assertIn('tls_port: ${LIVEKIT_TURN_TLS_PORT}', renderer)
        self.assertIn("TURN domain mismatch", renderer)
        self.assertIn('"--check"', renderer)
        self.assertIn('"render-livekit-secrets"), "--check"', runner)

    def test_supervision_and_tls_healthcheck_are_runner_managed(self) -> None:
        service = (ROOT.parents[1] / "scripts" / "blueprint-livekit-system.service").read_text(encoding="utf-8")
        healthcheck = (ROOT / "bin" / "livekit-healthcheck").read_text(encoding="utf-8")
        self.assertIn("ExecStartPost=@BASH@ @LIVEKIT_DIR@/bin/livekit-healthcheck --wait", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("User=root", service)
        self.assertIn("RuntimeDirectoryMode=0711", service)
        self.assertIn("CLOUDSDK_CONFIG=/var/cache/blueprint-livekit-gcloud", service)
        self.assertIn("EnvironmentFile=/etc/blueprint-livekit/public.env", service)
        self.assertNotIn("CLOUDSDK_CONFIG=/run/blueprint-livekit", service)
        self.assertIn("--resolve", healthcheck)
        self.assertIn("https://$LIVEKIT_PUBLIC_HOST/", healthcheck)
        self.assertIn("LIVEKIT_TURN_HOST", healthcheck)

    def test_docs_forbid_cohosting_with_untrusted_task_workloads(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("not\nan active Blueprint task daemon", readme)
        self.assertIn("169.254.169.254", readme)
        self.assertIn("secretAccessor", readme)

    def test_backend_contract_has_separate_pinned_secret_bindings(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        backend_env = (ROOT / "backend-call.env.example").read_text(encoding="utf-8")
        self.assertIn("BACKEND_SA", readme)
        self.assertIn("LIVEKIT_API_KEY", readme)
        self.assertIn("CALL_DEEPGRAM_API_KEY_SECRET_VERSION", readme)
        self.assertIn("Credential rotation", readme)
        self.assertIn("both the old\n   and new pairs", readme)
        self.assertIn("versions/1", backend_env)
        self.assertIn("LIVEKIT_URL=wss://", backend_env)

    def test_runner_cli_installs_an_independent_system_service(self) -> None:
        runner = (ROOT.parents[1] / "scripts" / "blueprint_runner.py").read_text(encoding="utf-8")
        unit = (ROOT.parents[1] / "scripts" / "blueprint-livekit-system.service").read_text(encoding="utf-8")
        self.assertIn('"livekit-preflight"', runner)
        self.assertIn('"livekit-install"', runner)
        self.assertIn('"livekit-reload"', runner)
        self.assertIn('"livekit-status"', runner)
        self.assertIn("WSL2 NAT", runner)
        self.assertIn("pass --system", runner)
        self.assertIn("BLUEPRINT_LIVEKIT_GCLOUD=@GCLOUD@", unit)
        self.assertIn("ConditionPathExists=/etc/blueprint-livekit-host-role", unit)
        self.assertIn("User=root", unit)
        self.assertNotIn("--user", unit)
        self.assertIn("livekit_gateway_role_is_selected", runner)
        local_runner = (ROOT.parents[1] / "scripts" / "local_runner.py").read_text(encoding="utf-8")
        self.assertIn("if br.livekit_gateway_role_is_selected():", local_runner)


if __name__ == "__main__":
    unittest.main()
