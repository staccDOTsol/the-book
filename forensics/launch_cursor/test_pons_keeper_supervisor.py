"""Offline supervisor ordering and bounded child failure tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_keeper_supervisor as supervisor


class Completed:
    returncode = 0


class SupervisorTests(unittest.TestCase):
    def test_serial_exit_then_price_and_pending_owner_first(self):
        local = supervisor.LOCAL / f"supervisor-test-{uuid4().hex}"
        local.mkdir(parents=True)
        exit_state, price_state = local / "exit.json", local / "price.json"
        exit_state.write_text(json.dumps({"pendingTx": None}))
        price_state.write_text(json.dumps({"pendingTx": None}))
        exit_spec = supervisor.KeeperSpec("exit", Path("exit.py"), exit_state, None)
        price_spec = supervisor.KeeperSpec("price", Path("price.py"), price_state, None)
        calls = []
        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return Completed()
        try:
            with patch.object(supervisor.subprocess, "run", side_effect=run):
                done = supervisor.run_keeper_cycle(price_spec, exit_spec, 30)
                self.assertEqual(done, ["exit", "price"])
                self.assertEqual([Path(call[0][1]).name for call in calls],
                                 ["exit.py", "price.py"])
                self.assertTrue(all("--once" in cmd and "--live" in cmd for cmd, _ in calls))
                self.assertTrue(all(kwargs["timeout"] == 30 for _, kwargs in calls))
                self.assertTrue(all("PONS_OWNER_PRIVATE_KEY" not in kwargs["env"]
                                    for _, kwargs in calls))
                calls.clear()
                price_state.write_text(json.dumps({"pendingTx": {"txHash": "0x1"}}))
                self.assertEqual(supervisor.run_keeper_cycle(price_spec, exit_spec, 30),
                                 ["price"])
                self.assertEqual(Path(calls[0][0][1]).name, "price.py")
                exit_state.write_text(json.dumps({"pendingTx": {"txHash": "0x2"}}))
                with self.assertRaisesRegex(supervisor.SupervisorError, "both configurator"):
                    supervisor.run_keeper_cycle(price_spec, exit_spec, 30)
        finally:
            shutil.rmtree(local)

    def test_first_run_requires_start_block_and_passes_it_only_once(self):
        state = supervisor.LOCAL / f"supervisor-first-{uuid4().hex}.json"
        spec = supervisor.KeeperSpec("exit", Path("exit.py"), state, None)
        with self.assertRaisesRegex(supervisor.SupervisorError, "start block"):
            supervisor.child_command(spec, once=True)
        spec = supervisor.KeeperSpec("exit", Path("exit.py"), state, 123)
        self.assertEqual(supervisor.child_command(spec, once=True)[-2:],
                         ["--start-block", "123"])

    def test_child_processes_receive_only_their_signer_role(self):
        watcher = supervisor.KeeperSpec("watcher", Path("watcher.py"),
                                        Path("watcher.json"), 1)
        price = supervisor.KeeperSpec("price", Path("price.py"),
                                      Path("price.json"), 1)
        exit_keeper = supervisor.KeeperSpec("exit", Path("exit.py"),
                                            Path("exit.json"), 1)
        with patch.dict(supervisor.os.environ, {
                "PONS_OWNER_PRIVATE_KEY": "owner-fixture",
                "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY": "config-fixture",
                "PONS_HTTP_RPC_URL": "https://rpc.example"}):
            watcher_env = supervisor.child_env(watcher)
            price_env = supervisor.child_env(price)
            exit_env = supervisor.child_env(exit_keeper)
        self.assertEqual(watcher_env["PONS_OWNER_PRIVATE_KEY"], "owner-fixture")
        self.assertNotIn("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", watcher_env)
        for env in (price_env, exit_env):
            self.assertEqual(env["PONS_PRICE_CONFIGURATOR_PRIVATE_KEY"], "config-fixture")
            self.assertNotIn("PONS_OWNER_PRIVATE_KEY", env)
            self.assertEqual(env["PONS_HTTP_RPC_URL"], "https://rpc.example")

    def test_immediately_failed_watcher_does_not_block_exit_cycle(self):
        class FailedWatcher:
            returncode = 1
            def poll(self): return 1
        env = {name: "fixture" for name in supervisor.REQUIRED_ENV}
        with patch.dict(supervisor.os.environ, env), \
             patch.object(supervisor.signal, "signal"), \
             patch.object(supervisor.subprocess, "Popen", return_value=FailedWatcher()), \
             patch.object(supervisor, "run_keeper_cycle", side_effect=KeyboardInterrupt) as cycle:
            result = supervisor.main(["--live", "--watcher-start-block", "1",
                                      "--price-start-block", "1", "--exit-start-block", "1"])
        self.assertEqual(result, 0)
        cycle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
