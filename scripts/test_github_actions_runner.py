"""Focused unit tests for the GitHub Actions role planner (no GitHub access)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import github_actions_runner as gar


class RoleValidationTests(unittest.TestCase):
    def test_untrusted_requires_ephemeral(self):
        with self.assertRaisesRegex(ValueError, "must be ephemeral"):
            gar.build_role(
                name="forks",
                registration_url="https://github.com/example",
                safety_mode="untrusted",
            )

    def test_untrusted_rejects_shared_docker_socket(self):
        with self.assertRaisesRegex(ValueError, "shared Docker socket"):
            gar.build_role(
                name="forks",
                registration_url="https://github.com/example",
                safety_mode="untrusted",
                ephemeral=True,
                docker_socket="/var/run/docker.sock",
            )

    def test_trusted_rejects_shared_docker_socket_too(self):
        with self.assertRaisesRegex(ValueError, "shared Docker socket"):
            gar.build_role(
                name="ci",
                registration_url="https://github.com/example",
                safety_mode="trusted",
                docker_socket="/run/docker.sock",
            )

    def test_role_paths_and_user_are_safety_scoped(self):
        trusted = gar.build_role(
            name="ci", registration_url="https://github.com/example", safety_mode="trusted"
        )
        untrusted = gar.build_role(
            name="ci",
            registration_url="https://github.com/example",
            safety_mode="untrusted",
            ephemeral=True,
        )
        self.assertNotEqual(trusted.runner_user, untrusted.runner_user)
        self.assertNotEqual(trusted.state_root, untrusted.state_root)
        self.assertNotEqual(trusted.env_file, untrusted.env_file)
        self.assertNotEqual(trusted.unit_name, untrusted.unit_name)
        self.assertLessEqual(len(trusted.runner_user), 32)

    def test_url_requires_clean_https(self):
        with self.assertRaisesRegex(ValueError, "https"):
            gar.build_role(
                name="ci", registration_url="http://github.com/example", safety_mode="trusted"
            )


class RenderTests(unittest.TestCase):
    def test_untrusted_unit_is_ephemeral_and_denies_docker(self):
        role = gar.build_role(
            name="forks",
            registration_url="https://github.com/example",
            safety_mode="untrusted",
            ephemeral=True,
        )
        unit = gar.render_systemd_unit(role)
        self.assertIn(
            "ConditionPathExists=/var/lib/blueprint-actions/BOOTSTRAP_COMPLETE",
            unit,
        )
        self.assertIn(f"User={role.runner_user}", unit)
        self.assertIn("Restart=no", unit)
        self.assertIn("InaccessiblePaths=/var/run/docker.sock /run/docker.sock", unit)
        self.assertIn("IPAddressDeny=169.254.169.254/32", unit)
        self.assertIn("IPAddressDeny=fd00:ec2::254/128", unit)
        self.assertNotIn("BindReadOnlyPaths=", unit)
        self.assertIn("ProtectHome=tmpfs", unit)

    def test_trusted_dedicated_socket_is_rendered_without_shared_socket(self):
        role = gar.build_role(
            name="ci",
            registration_url="https://github.com/example",
            safety_mode="trusted",
            docker_socket="/run/blueprint-actions/ci.sock",
        )
        unit = gar.render_systemd_unit(role)
        self.assertIn(
            "ConditionPathExists=/var/lib/blueprint-actions/BOOTSTRAP_COMPLETE",
            unit,
        )
        self.assertIn("BindReadOnlyPaths=/run/blueprint-actions/ci.sock", unit)
        self.assertIn("Restart=always", unit)
        self.assertNotIn("/var/run/docker.sock", unit)

    def test_untrusted_environment_rejects_production_credentials(self):
        role = gar.build_role(
            name="forks",
            registration_url="https://github.com/example",
            safety_mode="untrusted",
            ephemeral=True,
        )
        with self.assertRaisesRegex(ValueError, "production credentials"):
            gar.render_environment(role, {"OPENAI_API_KEY": "secret"})

    def test_environment_rejects_control_and_systemd_syntax(self):
        role = gar.build_role(
            name="ci", registration_url="https://github.com/example", safety_mode="trusted"
        )
        with self.assertRaisesRegex(ValueError, "control characters"):
            gar.render_environment(role, {"CI_VALUE": "good\x00bad"})
        with self.assertRaisesRegex(ValueError, "EnvironmentFile"):
            gar.render_environment(role, {"CI_VALUE": "has space"})

    def test_plan_contains_no_registration_token_value(self):
        role = gar.build_role(
            name="ci", registration_url="https://github.com/example", safety_mode="trusted"
        )
        plan = gar.describe_role(role)
        serialized = str(plan)
        self.assertNotIn("<token>", serialized)
        self.assertIn("registration-token", serialized)
        self.assertIn(
            "test -f /var/lib/blueprint-actions/BOOTSTRAP_COMPLETE",
            "\n".join(plan["lifecycle"]["install"]),
        )
        self.assertIn("systemctl stop", "\n".join(plan["lifecycle"]["drain"]))
        self.assertIn("useradd --system", "\n".join(plan["lifecycle"]["install"]))


if __name__ == "__main__":
    unittest.main()
