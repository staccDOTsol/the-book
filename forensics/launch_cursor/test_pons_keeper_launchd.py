"""Local launchd starter validation; never installs or starts a job."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_keeper_launchd as starter


class LaunchdTests(unittest.TestCase):
    def test_private_config_can_prepare_reviewable_restart_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.chmod(root, 0o700)
            config = root / "runtime.json"
            config.write_text(json.dumps({"schemaVersion": 1,
                                          "qAddress": "0x" + "11" * 20,
                                          "priceGuardAddress": "0x" + "22" * 20,
                                          "arguments": ["--standby", "--watcher-start-block", "123"]}))
            os.chmod(config, 0o600)
            output = root / "keeper.plist"
            with patch.object(starter, "LOCAL", root):
                starter.prepare(config, output)
            plist = plistlib.loads(output.read_bytes())
            self.assertTrue(plist["RunAtLoad"])
            self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(plist["ProgramArguments"][-1], str(config))
            self.assertEqual(output.stat().st_mode & 0o077, 0)

    def test_rejects_world_readable_config_and_embedded_raw_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "runtime.json"
            config.write_text(json.dumps({"schemaVersion": 1,
                                          "qAddress": "0x" + "11" * 20,
                                          "priceGuardAddress": "0x" + "22" * 20,
                                          "arguments": ["--standby"]}))
            os.chmod(config, 0o644)
            with self.assertRaisesRegex(starter.LaunchdError, "private regular"):
                starter.read_config(config)
            os.chmod(config, 0o600)
            config.write_text(json.dumps({"schemaVersion": 1,
                                          "qAddress": "0x" + "11" * 20,
                                          "priceGuardAddress": "0x" + "22" * 20,
                                          "arguments": ["--live", "0x" + "01" * 32]}))
            with self.assertRaisesRegex(starter.LaunchdError, "raw keys"):
                starter.read_config(config)

    def test_fresh_launchd_environment_gets_public_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "runtime.json"
            config.write_text(json.dumps({"schemaVersion": 1,
                                          "qAddress": "0x" + "11" * 20,
                                          "priceGuardAddress": "0x" + "22" * 20,
                                          "arguments": ["--standby", "--watcher-start-block", "123"]}))
            os.chmod(config, 0o600)
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(starter.os, "execv", side_effect=RuntimeError("stopped")) as execute:
                with self.assertRaisesRegex(RuntimeError, "stopped"):
                    starter.main(["run", "--config", str(config)])
                self.assertEqual(os.environ["PONS_Q_ADDRESS"], "0x" + "11" * 20)
                self.assertEqual(os.environ["PONS_PRICE_GUARD_ADDRESS"], "0x" + "22" * 20)
                self.assertIn("--standby", execute.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
