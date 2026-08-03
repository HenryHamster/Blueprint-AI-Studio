from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))

import bootstrap_local_app_release as bootstrap


class BootstrapLocalAppReleaseTests(unittest.TestCase):
    def test_edge_reset_loads_after_installed_funnel_drop_in(self):
        self.assertGreater(
            bootstrap._EDGE_DROP_IN.name,
            "10-noninteractive-funnel.conf",
        )

    def test_bundle_digest_is_order_independent(self):
        first = bootstrap._bundle_digest({"a.py": b"a", "b.py": b"b"})
        second = bootstrap._bundle_digest({"b.py": b"b", "a.py": b"a"})
        self.assertEqual(first, second)

    def test_api_drop_in_resets_commands_to_reviewed_wrapper(self):
        payload = bootstrap._api_drop_in(PurePosixPath("/trusted/bundle")).decode()
        self.assertIn("ExecStartPre=\n", payload)
        self.assertIn(
            "ExecStartPre=/usr/bin/python3.12 /trusted/bundle/local_api_service.py --check",
            payload,
        )
        self.assertIn("ExecStart=\n", payload)
        self.assertTrue(payload.endswith("/trusted/bundle/local_api_service.py\n"))

    def test_systemctl_properties_allow_reordering(self):
        payload = "MainPID=\nActiveState=active\nActiveEnterTimestampMonotonic=688094\n"
        self.assertEqual(
            bootstrap._parse_systemctl_properties(
                payload,
                {"ActiveState", "MainPID", "ActiveEnterTimestampMonotonic"},
            ),
            {
                "ActiveState": "active",
                "MainPID": "",
                "ActiveEnterTimestampMonotonic": "688094",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            bootstrap._parse_systemctl_properties(
                "ActiveState=active\n", {"ActiveState", "MainPID"}
            )

    def test_timer_marker_does_not_request_or_require_main_pid(self):
        result = bootstrap.subprocess.CompletedProcess(
            [],
            0,
            "ActiveState=active\nActiveEnterTimestampMonotonic=406788235451\n",
            "",
        )
        with unittest.mock.patch.object(
            bootstrap, "_run", return_value=result
        ) as run_mock:
            marker = bootstrap._marker("blueprint-database-backup.timer")
        self.assertEqual(marker, ("active", "", "406788235451"))
        self.assertNotIn("--property=MainPID", run_mock.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
