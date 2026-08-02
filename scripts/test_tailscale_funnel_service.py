import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import tailscale_funnel_service as funnel


class FakeRun:
    def __init__(self, statuses, *, mutation_returncode=0):
        self.statuses = list(statuses)
        self.mutation_returncode = mutation_returncode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[1:] == ["funnel", "status", "--json"]:
            value = self.statuses.pop(0)
            if isinstance(value, subprocess.CompletedProcess):
                return value
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        return subprocess.CompletedProcess(
            argv,
            self.mutation_returncode,
            "",
            "mutation failed" if self.mutation_returncode else "",
        )


def exact_status(target="http://127.0.0.1:8080"):
    host = "hamsterwheel.example.ts.net:443"
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {host: {"Handlers": {"/": {"Proxy": target}}}},
        "AllowFunnel": {host: True},
    }


class FunnelServiceTests(unittest.TestCase):
    def setUp(self):
        # Command execution is injected, so a known real executable is the
        # safest path fixture and leaves no temporary executable/ACL behind.
        self.binary = Path(sys.executable).resolve()
        self.config = funnel.Config(self.binary, 8080)

    def test_start_uses_exact_command_and_verifies_exact_status(self):
        run = FakeRun([{}, exact_status()])
        funnel.start(self.config, run=run)
        self.assertEqual(
            run.calls,
            [
                [str(self.binary), "funnel", "status", "--json"],
                [
                    str(self.binary),
                    "funnel",
                    "--yes",
                    "--bg",
                    "--https=443",
                    "http://127.0.0.1:8080",
                ],
                [str(self.binary), "funnel", "status", "--json"],
            ],
        )

    def test_start_is_idempotent_for_exact_owned_route(self):
        run = FakeRun([exact_status()])
        funnel.start(self.config, run=run)
        self.assertEqual(
            run.calls, [[str(self.binary), "funnel", "status", "--json"]]
        )

    def test_start_refuses_foreign_target_without_mutating(self):
        run = FakeRun([exact_status("http://127.0.0.1:9999")])
        with self.assertRaisesRegex(ValueError, "foreign"):
            funnel.start(self.config, run=run)
        self.assertEqual(len(run.calls), 1)

    def test_start_refuses_additional_foreign_route_without_mutating(self):
        status = exact_status()
        status["Web"]["other.example.ts.net:443"] = {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
        }
        run = FakeRun([status])
        with self.assertRaisesRegex(ValueError, "foreign"):
            funnel.start(self.config, run=run)
        self.assertEqual(len(run.calls), 1)

    def test_start_fails_when_tailscale_rejects_mutation(self):
        run = FakeRun([{}], mutation_returncode=1)
        with self.assertRaisesRegex(RuntimeError, "mutation failed"):
            funnel.start(self.config, run=run)
        self.assertEqual(len(run.calls), 2)

    def test_start_fails_when_postcondition_is_not_exact(self):
        run = FakeRun([{}, {}])
        with self.assertRaisesRegex(RuntimeError, "route is absent"):
            funnel.start(self.config, run=run)

    def test_stop_turns_off_only_the_exact_route_and_verifies_empty(self):
        run = FakeRun([exact_status(), {}])
        funnel.stop(self.config, run=run)
        self.assertEqual(
            run.calls[1],
            [
                str(self.binary),
                "funnel",
                "--yes",
                "--https=443",
                "http://127.0.0.1:8080",
                "off",
            ],
        )
        self.assertNotIn("reset", run.calls[1])

    def test_stop_refuses_foreign_state_instead_of_global_reset(self):
        run = FakeRun(
            [
                {
                    "TCP": {"8443": {"HTTPS": True}},
                    "Web": {},
                    "AllowFunnel": {},
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "foreign"):
            funnel.stop(self.config, run=run)
        self.assertEqual(len(run.calls), 1)

    def test_stop_fails_when_exact_off_command_is_rejected(self):
        run = FakeRun([exact_status()], mutation_returncode=1)
        with self.assertRaisesRegex(RuntimeError, "mutation failed"):
            funnel.stop(self.config, run=run)
        self.assertEqual(len(run.calls), 2)

    def test_nonzero_status_is_fatal(self):
        failed = subprocess.CompletedProcess(
            ["tailscale"], 1, "", "access denied"
        )
        with self.assertRaisesRegex(RuntimeError, "set --operator"):
            funnel.check(self.config, run=FakeRun([failed]))

    def test_invalid_json_status_is_fatal(self):
        invalid = subprocess.CompletedProcess(["tailscale"], 0, "not json", "")
        with self.assertRaisesRegex(RuntimeError, "valid JSON"):
            funnel.check(self.config, run=FakeRun([invalid]))

    def test_config_rejects_relative_binary_and_invalid_port(self):
        with self.assertRaisesRegex(ValueError, "absolute path"):
            funnel.load_config(
                {
                    "BLUEPRINT_TAILSCALE_BINARY": "tailscale",
                    "BLUEPRINT_EDGE_API_PORT": "8080",
                }
            )
        with self.assertRaisesRegex(ValueError, "control characters"):
            funnel.load_config(
                {
                    "BLUEPRINT_TAILSCALE_BINARY": str(self.binary),
                    "BLUEPRINT_EDGE_API_PORT": "8080\nExecStart=/bad",
                }
            )

    @unittest.skipIf(os.name == "nt", "symlink permissions are platform-specific")
    def test_config_rejects_symlinked_binary(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            symlink = Path(temp) / "tailscale-link"
            symlink.symlink_to(self.binary)
            with self.assertRaisesRegex(ValueError, "symlink"):
                funnel.load_config(
                    {
                        "BLUEPRINT_TAILSCALE_BINARY": str(symlink),
                        "BLUEPRINT_EDGE_API_PORT": "8080",
                    }
                )


if __name__ == "__main__":
    unittest.main()
