"""Offline, read-only Q dashboard and safe supervisor status tests."""

from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_keeper_supervisor as supervisor
import q_status_dashboard as dashboard


NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
Q = dashboard.LIVE_Q.lower()
GUARD = "0x" + "b" * 40
ZERO = "0x" + "0" * 40
TOKEN = "0x" + "c" * 40
HASH = "0x" + "d" * 64


def component(name, *, q=Q, secret="signed-raw-tx-secret"):
    data = {"version": 1, "chainId": 4663, "q": q,
            "lastBlock": 123, "lastHash": HASH}
    if name == "watcher":
        data["pending"] = {"rawTransaction": secret}
    else:
        data.update(guard=ZERO if name == "price" else GUARD, tokens=[TOKEN],
                    pendingTx={"rawTx": secret} if name == "price" else None)
    return data


def supervisor_heartbeat(*, at=NOW, status="running", exit_at=None, price_at=None,
                         mode="live"):
    exit_at = exit_at or at
    price_at = price_at or at
    return {"schemaVersion": 1, "updatedAt": at.isoformat(),
            "mode": mode, "writesEnabled": mode == "live",
            "status": status, "watcherStatus": "process_running",
            "lastCompletedKeepers": ["exit", "price", "feedback", "harvest"],
            "lastCompletedAt": at.isoformat(), "consecutiveFailures": 0,
            "lastCompletedAtByKeeper": {"exit": exit_at.isoformat(),
                                        "price": price_at.isoformat(),
                                        "feedback": at.isoformat(),
                                        "harvest": at.isoformat()},
            "watcherConsecutiveFailures": 0, "lastError": None}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.local = Path(self.temporary.name) / ".local"
        self.local.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def save_components(self):
        for name, (filename, _) in dashboard.JOURNALS.items():
            (self.local / filename).write_text(json.dumps(component(name)))

    def test_empty_local_state_is_unconfigured_and_does_not_claim_deployment(self):
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["runtime"]["state"], "unconfigured")
        self.assertFalse(report["runtime"]["deploymentVerified"])
        self.assertIsNone(report["runtime"]["q"])
        self.assertEqual(report["supervisor"]["state"], "missing")

    def test_fresh_complete_supervisor_cycle_reports_live_without_secret_fields(self):
        self.save_components()
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(supervisor_heartbeat()))
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["runtime"]["state"], "live_reported")
        self.assertEqual(report["runtime"]["q"], Q)
        self.assertTrue(report["components"]["watcher"]["pendingTransaction"])
        self.assertEqual(report["components"]["price"]["tokenCount"], 1)
        self.assertEqual(report["components"]["exit"]["activeCount"], 1)
        serialized = json.dumps(report)
        self.assertNotIn("signed-raw-tx-secret", serialized)
        self.assertNotIn("rawTx", serialized)
        self.assertNotIn("rawTransaction", serialized)

    def test_stale_or_partial_reports_do_not_claim_live(self):
        self.save_components()
        old = NOW - timedelta(minutes=5)
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(supervisor_heartbeat(at=old)))
        self.assertEqual(dashboard.snapshot(self.local, NOW)["runtime"]["state"],
                         "not_live_verified")
        (self.local / dashboard.JOURNALS["exit"][0]).unlink()
        self.assertEqual(dashboard.snapshot(self.local, NOW)["runtime"]["state"], "partial")

    def test_standby_heartbeat_never_claims_live(self):
        (self.local / dashboard.SUPERVISOR_FILE).write_text(
            json.dumps(supervisor_heartbeat(mode="standby")))
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["runtime"]["state"], "standby")
        self.assertFalse(report["supervisor"]["writesEnabled"])

    def test_old_heartbeat_without_mode_cannot_claim_live(self):
        self.save_components()
        heartbeat = supervisor_heartbeat()
        del heartbeat["mode"]
        del heartbeat["writesEnabled"]
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(heartbeat))
        self.assertEqual(dashboard.snapshot(self.local, NOW)["runtime"]["state"],
                         "not_live_verified")

    def test_fresh_price_cannot_mask_stale_exit_cycle(self):
        self.save_components()
        heartbeat = supervisor_heartbeat(exit_at=NOW - timedelta(minutes=5))
        heartbeat["lastCompletedKeepers"] = ["price"]
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(heartbeat))
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["runtime"]["state"], "not_live_verified")
        self.assertFalse(report["supervisor"]["keeperFresh"]["exit"])
        self.assertTrue(report["supervisor"]["keeperFresh"]["price"])
        self.assertEqual(report["supervisor"]["lastCompletedAtByKeeper"]["exit"],
                         (NOW - timedelta(minutes=5)).isoformat())

    def test_legacy_heartbeat_remains_readable_but_cannot_claim_both_keepers(self):
        self.save_components()
        heartbeat = supervisor_heartbeat()
        del heartbeat["lastCompletedAtByKeeper"]
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(heartbeat))
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["supervisor"]["state"], "valid")
        self.assertEqual(report["runtime"]["state"], "not_live_verified")

    def test_mismatched_binding_and_symlinked_journal_require_attention(self):
        self.save_components()
        price_path = self.local / dashboard.JOURNALS["price"][0]
        price_path.write_text(json.dumps(component("price", q="0x" + "e" * 40)))
        self.assertEqual(dashboard.snapshot(self.local, NOW)["runtime"]["state"], "attention")
        price_path.unlink()
        price_path.symlink_to(self.local / dashboard.JOURNALS["watcher"][0])
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["components"]["price"]["state"], "invalid")
        self.assertEqual(report["runtime"]["state"], "attention")

    def test_price_and_exit_guard_roles_are_checked_independently(self):
        self.save_components()
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(supervisor_heartbeat()))
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["runtime"]["state"], "live_reported")
        self.assertEqual(report["components"]["price"]["guard"], ZERO)
        self.assertEqual(report["components"]["exit"]["guard"], GUARD)
        price_path = self.local / dashboard.JOURNALS["price"][0]
        invalid_price = component("price")
        invalid_price["guard"] = GUARD
        price_path.write_text(json.dumps(invalid_price))
        self.assertEqual(dashboard.snapshot(self.local, NOW)["runtime"]["state"], "attention")
        price_path.write_text(json.dumps(component("price")))
        exit_path = self.local / dashboard.JOURNALS["exit"][0]
        invalid_exit = component("exit")
        invalid_exit["guard"] = ZERO
        exit_path.write_text(json.dumps(invalid_exit))
        self.assertEqual(dashboard.snapshot(self.local, NOW)["runtime"]["state"], "attention")

    def test_wrong_chain_journal_cannot_report_live(self):
        self.save_components()
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(supervisor_heartbeat()))
        watcher_path = self.local / dashboard.JOURNALS["watcher"][0]
        wrong = component("watcher")
        wrong["chainId"] = 1
        watcher_path.write_text(json.dumps(wrong))
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["components"]["watcher"]["state"], "invalid")
        self.assertEqual(report["runtime"]["state"], "attention")

    def test_old_q_journals_are_labeled_legacy(self):
        for name, (filename, _) in dashboard.JOURNALS.items():
            (self.local / filename).write_text(json.dumps(component(name, q="0x" + "a" * 40)))
        report = dashboard.snapshot(self.local, NOW)
        self.assertEqual(report["runtime"]["state"], "legacy")
        self.assertFalse(report["runtime"]["matchesLiveQ"])

    def test_live_chain_probe_requires_actual_pool_and_launch_evidence(self):
        q = dashboard.LIVE_Q.lower()
        pool = dashboard.V3_POOL.lower()
        usdg = dashboard.USDG.lower()
        address_word = lambda value: "0x" + value[2:].rjust(64, "0")
        rows = {1: hex(dashboard.CHAIN_ID), 2: hex(79_316_000), 3: "0x6000",
                4: {"status": "0x1", "to": dashboard.POOLS_INSTANT_STRATEGY,
                    "blockNumber": hex(79_307_686), "logs": [{"address": q}]},
                5: "0x6000", 6: address_word(pool), 7: address_word(usdg),
                8: address_word(q), 9: hex(1_000), 10: address_word(pool),
                11: "0x1", 12: "0x4", 13: "0x0"}
        with patch.object(dashboard, "_rpc_batch", return_value=rows):
            report = dashboard.chain_status(NOW)
        self.assertEqual(report["state"], "verified")
        self.assertEqual(report["pendingEntryCount"], 4)
        self.assertEqual(report["successfulExecutorSteps"], 0)
        rows[6] = address_word("0x" + "a" * 40)
        with patch.object(dashboard, "_rpc_batch", return_value=rows):
            self.assertEqual(dashboard.chain_status(NOW)["state"], "partial")

    def test_fly_probe_reports_trader_paid_without_echoing_remote_error(self):
        machines = [{"state": "started", "config": {"env": {"PONS_KEEPER_MODE": "trader-paid"}}}]
        heartbeat = supervisor_heartbeat()
        heartbeat["dequeueMode"] = "trader_transfer"
        heartbeat["lastError"] = {"type": "Error", "message": "secret https://rpc.example/key"}
        with patch.object(dashboard, "_fly_command", side_effect=[
            json.dumps(machines), json.dumps(heartbeat)]):
            report = dashboard.fly_status(NOW)
        self.assertEqual(report["state"], "running_reported")
        self.assertEqual(report["deploymentMode"], "trader-paid")
        self.assertNotIn("rpc.example", json.dumps(report))

    def test_untrusted_supervisor_error_is_replaced_without_echoing_secret(self):
        self.save_components()
        heartbeat = supervisor_heartbeat()
        heartbeat["lastError"] = {"type": "OSError", "message": "secret https://rpc.example/key"}
        (self.local / dashboard.SUPERVISOR_FILE).write_text(json.dumps(heartbeat))
        report = dashboard.snapshot(self.local, NOW)
        self.assertNotIn("https://rpc.example", json.dumps(report))
        self.assertEqual(report["supervisor"]["lastError"]["type"], "Error")

    def test_http_is_get_only_and_serves_filtered_status(self):
        self.save_components()
        server = HTTPServer(("127.0.0.1", 0), dashboard.Handler)
        server.journal_dir = self.local
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with urlopen(url + "/api/status") as response:
                body = response.read().decode()
                self.assertEqual(response.status, 200)
            self.assertIn(Q, body)
            self.assertNotIn("signed-raw-tx-secret", body)
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(url + "/api/status", method="POST", data=b"{}"))
            self.assertEqual(raised.exception.code, 405)
            raised.exception.close()
            with self.assertRaises(HTTPError) as forbidden:
                urlopen(Request(url + "/api/status", headers={"Host": "outside.example"}))
            self.assertEqual(forbidden.exception.code, 403)
            forbidden.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


class SupervisorJournalTests(unittest.TestCase):
    def test_writer_is_atomic_private_and_redacts_exception_content(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / ".local"
            path = local / "pons-keeper-supervisor-status.json"
            secret = "https://rpc.example/api-key/private-key/raw-tx"
            error = supervisor.sanitized_error(OSError(secret))
            with patch.object(supervisor, "LOCAL", local), patch.object(supervisor, "STATUS_PATH", path):
                supervisor.write_status(status="retrying", watcher_status="retrying",
                                        completed=["exit", "unknown"], completed_at=None,
                                        failures=2, watcher_failures=1, last_error=error,
                                        completed_by_keeper={"exit": NOW.isoformat()})
            payload = json.loads(path.read_text())
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["lastCompletedKeepers"], ["exit"])
            self.assertEqual(payload["lastCompletedAtByKeeper"],
                             {"exit": NOW.isoformat(), "price": None,
                              "feedback": None, "harvest": None})
            self.assertEqual(payload["lastError"]["type"], "OSError")
            self.assertNotIn(secret, path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(local.glob("*.new")), [])
            with patch.object(supervisor, "LOCAL", local), patch.object(supervisor, "STATUS_PATH", path):
                supervisor.write_status(status="retrying", watcher_status="retrying",
                                        completed=[], completed_at=None, failures=2,
                                        watcher_failures=1,
                                        last_error={"type": "OSError", "message": secret})
            self.assertNotIn(secret, path.read_text())

    def test_writer_rejects_untrusted_completion_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / ".local"
            path = local / "pons-keeper-supervisor-status.json"
            with patch.object(supervisor, "LOCAL", local), patch.object(supervisor, "STATUS_PATH", path):
                with self.assertRaisesRegex(supervisor.SupervisorError, "completion timestamp"):
                    supervisor.write_status(status="running", watcher_status="process_running",
                                            completed=["exit"], completed_at=None,
                                            failures=0, watcher_failures=0, last_error=None,
                                            completed_by_keeper={"exit": "private-key-secret"})
            self.assertFalse(path.exists())

    def test_supervisor_error_text_is_not_copied_to_journal(self):
        secret = "https://rpc.example/api-key/private-key/raw-tx"
        error = supervisor.sanitized_error(supervisor.SupervisorError("price cycle exited 1 " + secret))
        self.assertEqual(error["type"], "SupervisorError")
        self.assertNotIn(secret, json.dumps(error))


if __name__ == "__main__":
    unittest.main()
