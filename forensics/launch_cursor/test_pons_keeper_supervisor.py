"""Offline supervisor signer isolation and independent child recovery tests."""

from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_keeper_supervisor as supervisor


class Child:
    def __init__(self):
        self.returncode = None
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = -15
    def kill(self):
        self.returncode = -9
    def wait(self, timeout=None):
        return self.returncode


class SupervisorTests(unittest.TestCase):
    def test_price_pending_cannot_block_new_exit_cycle(self):
        exit_spec = supervisor.KeeperSpec("exit", Path("exit.py"), Path("exit.json"), 1)
        price_spec = supervisor.KeeperSpec("price", Path("price.py"), Path("price.json"), 1)
        exit_job, price_job = supervisor.KeeperJob(exit_spec), supervisor.KeeperJob(price_spec)
        exit_child, price_child, next_exit = Child(), Child(), Child()
        calls = []
        def start(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return [exit_child, price_child, next_exit][len(calls) - 1]
        with patch.object(supervisor.subprocess, "Popen", side_effect=start):
            self.assertEqual(supervisor.service_keeper(exit_job, 10, 600, 1, 60), (None, None))
            self.assertEqual(supervisor.service_keeper(price_job, 10, 600, 1, 60), (None, None))
            self.assertEqual([Path(call[0][1]).name for call in calls], ["exit.py", "price.py"])
            exit_child.returncode = 0
            self.assertEqual(supervisor.service_keeper(exit_job, 11, 600, 1, 60), ("exit", None))
            self.assertEqual(supervisor.service_keeper(price_job, 11, 600, 1, 60), (None, None))
            self.assertEqual(supervisor.service_keeper(exit_job, 12, 600, 1, 60), (None, None))
        self.assertIs(exit_job.process, next_exit)
        self.assertIs(price_job.process, price_child)

    def test_timeout_restarts_only_own_signer_after_backoff(self):
        spec = supervisor.KeeperSpec("price", Path("price.py"), Path("price.json"), 1)
        job = supervisor.KeeperJob(spec)
        child = Child()
        with patch.object(supervisor.subprocess, "Popen", return_value=child):
            supervisor.service_keeper(job, 10, 20, 1, 10)
            done, error = supervisor.service_keeper(job, 31, 20, 1, 10)
        self.assertIsNone(done)
        self.assertIn("timeout", str(error))
        self.assertEqual(job.retry_after, 32)
        self.assertIsNone(job.process)
        self.assertEqual(child.returncode, -15)

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
        feedback = supervisor.KeeperSpec("feedback", Path("feedback.py"),
                                         Path("feedback.json"), 1)
        harvest = supervisor.KeeperSpec("harvest", Path("harvest.py"),
                                        Path("exit.json"), 1)
        with patch.dict(supervisor.os.environ, {
                "PONS_OWNER_PRIVATE_KEY": "owner-fixture",
                "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY": "config-fixture",
                "PONS_EXIT_CONFIGURATOR_PRIVATE_KEY": "exit-fixture",
                "PONS_HTTP_RPC_URL": "https://rpc.example"}):
            watcher_env = supervisor.child_env(watcher)
            price_env = supervisor.child_env(price)
            exit_env = supervisor.child_env(exit_keeper)
            feedback_env = supervisor.child_env(feedback)
            harvest_env = supervisor.child_env(harvest)
        self.assertEqual(watcher_env["PONS_OWNER_PRIVATE_KEY"], "owner-fixture")
        self.assertNotIn("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", watcher_env)
        self.assertNotIn("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY", watcher_env)
        self.assertEqual(price_env["PONS_PRICE_CONFIGURATOR_PRIVATE_KEY"], "config-fixture")
        self.assertNotIn("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY", price_env)
        self.assertEqual(exit_env["PONS_EXIT_CONFIGURATOR_PRIVATE_KEY"], "exit-fixture")
        self.assertNotIn("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", exit_env)
        self.assertEqual(feedback_env["PONS_PRICE_CONFIGURATOR_PRIVATE_KEY"], "config-fixture")
        self.assertNotIn("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY", feedback_env)
        self.assertEqual(harvest_env["PONS_EXIT_CONFIGURATOR_PRIVATE_KEY"], "exit-fixture")
        self.assertNotIn("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", harvest_env)
        for env in (price_env, exit_env, feedback_env, harvest_env):
            self.assertNotIn("PONS_OWNER_PRIVATE_KEY", env)
            self.assertEqual(env["PONS_HTTP_RPC_URL"], "https://rpc.example")

    def test_private_local_rpc_env_loads_without_overriding_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pons-rpc.env"
            path.write_text("PONS_HTTP_RPC_URL=https://local.example\n"
                            "PONS_WS_RPC_URL=wss://local.example\nPONS_CHAIN_ID=4663\n")
            os.chmod(path, 0o600)
            with patch.dict(supervisor.os.environ,
                            {"PONS_HTTP_RPC_URL": "https://process.example"}, clear=True):
                supervisor.load_local_rpc_env(path)
                self.assertEqual(supervisor.os.environ["PONS_HTTP_RPC_URL"],
                                 "https://process.example")
                self.assertEqual(supervisor.os.environ["PONS_WS_RPC_URL"],
                                 "wss://local.example")
                self.assertEqual(supervisor.os.environ["PONS_CHAIN_ID"], "4663")

    def test_loose_or_symlinked_rpc_env_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pons-rpc.env"
            path.write_text("PONS_CHAIN_ID=4663\n")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(supervisor.SupervisorError, "private regular file"):
                supervisor.load_local_rpc_env(path)
            os.chmod(path, 0o600)
            link = Path(tmp) / "linked.env"
            link.symlink_to(path)
            with self.assertRaisesRegex(supervisor.SupervisorError, "private regular file"):
                supervisor.load_local_rpc_env(link)

    def test_live_signers_must_be_three_distinct_accounts(self):
        keys = {
            "PONS_OWNER_PRIVATE_KEY": "0x" + "01" * 32,
            "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY": "0x" + "02" * 32,
            "PONS_EXIT_CONFIGURATOR_PRIVATE_KEY": "0x" + "03" * 32,
        }
        with patch.dict(supervisor.os.environ, keys):
            supervisor.verify_signer_isolation()
        keys["PONS_EXIT_CONFIGURATOR_PRIVATE_KEY"] = keys["PONS_OWNER_PRIVATE_KEY"]
        with patch.dict(supervisor.os.environ, keys):
            with self.assertRaisesRegex(supervisor.SupervisorError, "must differ"):
                supervisor.verify_signer_isolation()

    def test_immediately_failed_watcher_does_not_block_exit_cycle(self):
        class FailedWatcher:
            returncode = 1
            def poll(self): return 1
        env = {name: "fixture" for name in supervisor.REQUIRED_ENV}
        with patch.dict(supervisor.os.environ, env), \
             patch.object(supervisor, "verify_signer_isolation"), \
             patch.object(supervisor.signal, "signal"), \
             patch.object(supervisor.subprocess, "Popen", return_value=FailedWatcher()), \
             patch.object(supervisor, "write_status") as status, \
             patch.object(supervisor, "service_keeper", side_effect=KeyboardInterrupt) as cycle:
            result = supervisor.main(["--live", "--watcher-start-block", "1",
                                      "--price-start-block", "1", "--exit-start-block", "1"])
        self.assertEqual(result, 0)
        cycle.assert_called_once()
        self.assertEqual([call.kwargs["status"] for call in status.call_args_list],
                         ["starting", "running", "stopped"])


if __name__ == "__main__":
    unittest.main()
