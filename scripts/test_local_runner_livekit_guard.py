"""Focused test for the reciprocal LiveKit gateway host-role guard."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent))
import local_runner


class LocalRunnerLiveKitGuardTests(unittest.TestCase):
    def test_direct_runner_invocation_rejects_gateway_host(self) -> None:
        with patch.object(local_runner.sys, "argv", ["local_runner.py"]), \
             patch.object(local_runner.br, "livekit_gateway_role_is_selected", return_value=True), \
             self.assertRaisesRegex(SystemExit, "1"):
            local_runner.main()


if __name__ == "__main__":
    unittest.main()
