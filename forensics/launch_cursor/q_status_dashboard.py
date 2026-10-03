#!/usr/bin/env python3
"""Loopback-only Q status page with separate chain, Fly, and local evidence.

The live probes use a public Robinhood RPC and read-only Fly commands. Local
journals are filtered; signer keys, RPC credentials, and transaction bodies
are never read or returned.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
from typing import Any
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
LOCAL = HERE.parents[1] / ".local"
PAGE = HERE / "q_status_dashboard.html"
MAX_JOURNAL_BYTES = 512_000
SUPERVISOR_FRESH_SECONDS = 90
# Exit/harvest and price/feedback share signer lanes. Each child may use the
# supervisor's 600-second timeout, so a successful exit or price cycle can be
# nearly two child runtimes old while its lane is still working normally.
CYCLE_FRESH_SECONDS = 2 * 600 + 30
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
ZERO_ADDRESS = "0x" + "0" * 40
JOURNALS = {
    "watcher": ("pons-launch-watcher.json", "pending"),
    "price": ("pons-price-keeper.json", "pendingTx"),
    "exit": ("pons-exit-keeper.json", "pendingTx"),
}
SUPERVISOR_FILE = "pons-keeper-supervisor-status.json"
SUPERVISOR_STATUSES = {"starting", "running", "retrying", "stopped"}
WATCHER_STATUSES = {"not_started", "process_running", "restarting", "retrying", "stopped"}
KEEPER_NAMES = ("exit", "price", "feedback", "harvest")
LIVE_Q = "0x623B5374c4CB838DA24EE9F48F08664337936a06"
LAUNCH_TX = "0xf51753626d831774984de6114a09f84381226084526ea865e57ea9a0f27c35aa"
POOLS_INSTANT_STRATEGY = "0x0000ffffbe8efe702c8703ae3477ff5de3d319c0"
V3_POOL = "0xBe8DC1ef3F78B00050247267619517a1D22D1589"
V3_FACTORY = "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
CHAIN_ID = 4663
PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com/"
FLY_APP = "the-book-q-keeper"
PROBE_TTL_SECONDS = 20
_probe_cache: tuple[float, dict[str, Any]] | None = None
_probe_lock = threading.Lock()
SAFE_ERROR_MESSAGES = {
    "Both configurator keepers have pending signed transactions.",
    "A keeper cycle exceeded its timeout.",
    "A keeper cycle exited unsuccessfully.",
    "The bounded keeper retry limit was reached.",
    "A keeper state journal is unreadable or malformed.",
    "Supervisor check failed; inspect local logs.",
    "A local process or file operation failed; inspect local logs.",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def fresh(value: datetime | None, current: datetime, seconds: int) -> bool:
    return value is not None and 0 <= (current - value).total_seconds() <= seconds


def is_number(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def is_address(value: Any) -> bool:
    return isinstance(value, str) and ADDRESS.fullmatch(value) is not None


def _read_fixed_json(local: Path, filename: str) -> tuple[Any, datetime | None, str]:
    """Open a fixed basename without following a symlink; bound the read size."""
    if local.is_symlink():
        return None, None, "invalid"
    if not local.exists():
        return None, None, "missing"
    directory = None
    file = None
    try:
        directory = os.open(local, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            file = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None, None, "missing"
        metadata = os.fstat(file)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_JOURNAL_BYTES:
            return None, None, "invalid"
        raw = os.read(file, MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            return None, None, "invalid"
        return json.loads(raw), datetime.fromtimestamp(metadata.st_mtime, timezone.utc), "present"
    except (OSError, ValueError, UnicodeError):
        return None, None, "invalid"
    finally:
        if file is not None:
            os.close(file)
        if directory is not None:
            os.close(directory)


def component_status(local: Path, name: str) -> dict[str, Any]:
    filename, pending_field = JOURNALS[name]
    data, written_at, presence = _read_fixed_json(local, filename)
    result: dict[str, Any] = {
        "state": presence, "writtenAt": iso(written_at) if written_at else None,
        "chainId": None, "q": None, "lastBlock": None,
        "pendingTransaction": None, "tokenCount": None, "activeCount": None,
        "guard": None,
    }
    if presence != "present":
        return result
    if (not isinstance(data, dict) or type(data.get("version")) is not int or
            data.get("version") != 1 or pending_field not in data or
            data.get("chainId") != 4663 or
            not is_address(data.get("q")) or
            not is_number(data.get("lastBlock")) or
            not isinstance(data.get("lastHash"), str) or
            HASH.fullmatch(data["lastHash"]) is None or
            data.get(pending_field) is not None and not isinstance(data[pending_field], dict)):
        result["state"] = "invalid"
        return result
    if name in ("price", "exit"):
        tokens = data.get("tokens")
        if (not is_address(data.get("guard")) or not isinstance(tokens, list) or
                any(not is_address(token) for token in tokens)):
            result["state"] = "invalid"
            return result
        result["guard"] = data["guard"].lower()
        # The opening keeper now has no spot/depth gate and binds its journal
        # to zero. The exit keeper still uses a nonzero guard for cash quotes.
        if ((name == "price" and result["guard"] != ZERO_ADDRESS) or
                (name == "exit" and result["guard"] == ZERO_ADDRESS)):
            result["state"] = "invalid"
            return result
        result["tokenCount" if name == "price" else "activeCount"] = len(tokens)
    result.update(chainId=data["chainId"], q=data["q"].lower(),
                  lastBlock=data["lastBlock"],
                  pendingTransaction=data[pending_field] is not None,
                  state="valid")
    return result


def supervisor_status(local: Path, current: datetime) -> dict[str, Any]:
    data, written_at, presence = _read_fixed_json(local, SUPERVISOR_FILE)
    result: dict[str, Any] = {
        "state": presence, "status": None, "watcherStatus": None,
        "mode": None, "writesEnabled": False,
        "updatedAt": None, "fresh": False, "lastCompletedKeepers": [],
        "lastCompletedAt": None, "cycleFresh": False,
        "lastCompletedAtByKeeper": {name: None for name in KEEPER_NAMES},
        "keeperFresh": {name: False for name in KEEPER_NAMES},
        "consecutiveFailures": None, "watcherConsecutiveFailures": None,
        "lastError": None,
    }
    if presence != "present":
        return result
    updated = parse_time(data.get("updatedAt")) if isinstance(data, dict) else None
    completed_at = parse_time(data.get("lastCompletedAt")) if isinstance(data, dict) else None
    completed = data.get("lastCompletedKeepers") if isinstance(data, dict) else None
    raw_keeper_times = data.get("lastCompletedAtByKeeper") if isinstance(data, dict) else None
    if raw_keeper_times is None:
        # Older heartbeat format remains readable but cannot prove both
        # independent signer loops are currently making progress.
        raw_keeper_times = {name: None for name in KEEPER_NAMES}
    if (not isinstance(raw_keeper_times, dict) or
            set(raw_keeper_times) not in ({"exit", "price"}, set(KEEPER_NAMES))):
        result["state"] = "invalid"
        return result
    keeper_times = {name: parse_time(raw_keeper_times.get(name)) for name in KEEPER_NAMES}
    mode = data.get("mode") if isinstance(data, dict) else None
    writes_enabled = data.get("writesEnabled") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or
            data.get("schemaVersion") != 1 or
            (mode is not None and mode not in ("live", "standby")) or
            (writes_enabled is not None and type(writes_enabled) is not bool) or
            (mode == "standby" and writes_enabled is True) or
            not isinstance(data.get("status"), str) or
            data.get("status") not in SUPERVISOR_STATUSES or
            not isinstance(data.get("watcherStatus"), str) or
            data.get("watcherStatus") not in WATCHER_STATUSES or updated is None or
            (data.get("lastCompletedAt") is not None and completed_at is None) or
            not isinstance(completed, list) or
            any(name not in KEEPER_NAMES for name in completed) or
            len(set(completed)) != len(completed) or
            any(raw_keeper_times.get(name) is not None and keeper_times[name] is None
                for name in KEEPER_NAMES) or
            not is_number(data.get("consecutiveFailures")) or
            not is_number(data.get("watcherConsecutiveFailures"))):
        result["state"] = "invalid"
        return result
    error = data.get("lastError")
    if error is not None:
        if not isinstance(error, dict):
            result["state"] = "invalid"
            return result
        kind = error.get("type")
        message = error.get("message")
        if (not isinstance(kind, str) or kind not in ("SupervisorError", "OSError", "Error") or
                not isinstance(message, str) or message not in SAFE_ERROR_MESSAGES):
            error = {"type": "Error", "message": "Supervisor check failed; inspect local logs."}
    result.update(
        state="valid", status=data["status"], watcherStatus=data["watcherStatus"],
        mode=mode, writesEnabled=writes_enabled is True,
        updatedAt=iso(updated), fresh=fresh(updated, current, SUPERVISOR_FRESH_SECONDS),
        lastCompletedKeepers=completed,
        lastCompletedAt=iso(completed_at) if completed_at else None,
        cycleFresh=fresh(completed_at, current, CYCLE_FRESH_SECONDS),
        lastCompletedAtByKeeper={name: iso(keeper_times[name]) if keeper_times[name] else None
                                 for name in KEEPER_NAMES},
        keeperFresh={name: fresh(keeper_times[name], current, CYCLE_FRESH_SECONDS)
                     for name in KEEPER_NAMES},
        consecutiveFailures=data["consecutiveFailures"],
        watcherConsecutiveFailures=data["watcherConsecutiveFailures"],
        lastError=error,
    )
    return result


def snapshot(local: Path = LOCAL, current: datetime | None = None) -> dict[str, Any]:
    current = current or utc_now()
    components = {name: component_status(local, name) for name in JOURNALS}
    supervisor = supervisor_status(local, current)
    states = [item["state"] for item in components.values()]
    q_values = {item["q"] for item in components.values() if item["state"] == "valid"}
    chains = {item["chainId"] for item in components.values() if item["state"] == "valid"}
    bindings_match = len(q_values) <= 1 and len(chains) <= 1
    expected_binding = q_values == {LIVE_Q.lower()}
    if "invalid" in states or supervisor["state"] == "invalid" or not bindings_match:
        runtime_state = "attention"
        detail = "A journal is invalid or the component bindings disagree. Inspect local state."
    elif q_values and not expected_binding:
        runtime_state = "legacy"
        detail = "Local journals refer to a different Q. They do not describe the corrected live Q."
    elif (supervisor["state"] == "valid" and supervisor["mode"] == "standby" and
          supervisor["fresh"] and supervisor["status"] == "running"):
        runtime_state = "standby"
        detail = "Read-only keeper supervisor is running. Onchain writes are disabled."
    elif all(state == "missing" for state in states):
        runtime_state = "unconfigured"
        detail = "No Q runtime journals found. Deployment is not verified by this local page."
    elif "missing" in states:
        runtime_state = "partial"
        detail = "Some local runtime journals exist; the strategy is not confirmed live."
    elif (supervisor["state"] == "valid" and supervisor["mode"] == "live" and
          supervisor["writesEnabled"] and supervisor["status"] == "running" and
          supervisor["watcherStatus"] == "process_running" and
          supervisor["fresh"] and supervisor["cycleFresh"] and
          supervisor["keeperFresh"]["exit"] and supervisor["keeperFresh"]["price"]):
        runtime_state = "live_reported"
        detail = "Fresh local supervisor heartbeat and recent successful exit and price cycles. Onchain deployment is not independently checked."
    else:
        runtime_state = "not_live_verified"
        detail = "Q journals exist, but there is no fresh complete live supervisor report."
    return {
        "generatedAt": iso(current),
        "runtime": {"state": runtime_state, "detail": detail,
                    "q": next(iter(q_values)) if bindings_match and len(q_values) == 1 else None,
                    "chainId": next(iter(chains)) if bindings_match and len(chains) == 1 else None,
                    "deploymentVerified": False, "matchesLiveQ": expected_binding},
        "supervisor": supervisor,
        "components": components,
    }


def _rpc_batch() -> dict[int, Any]:
    def rpc(method: str, params: list[Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": len(calls) + 1, "method": method, "params": params}

    calls: list[dict[str, Any]] = []
    for method, params in (
        ("eth_chainId", []),
        ("eth_blockNumber", []),
        ("eth_getCode", [LIVE_Q, "latest"]),
        ("eth_getTransactionReceipt", [LAUNCH_TX]),
        ("eth_getCode", [V3_POOL, "latest"]),
        ("eth_call", [{"to": V3_FACTORY, "data": "0x1698ee82" +
                       USDG[2:].lower().rjust(64, "0") + LIVE_Q[2:].lower().rjust(64, "0") +
                       hex(3000)[2:].rjust(64, "0")}, "latest"]),
        ("eth_call", [{"to": V3_POOL, "data": "0x0dfe1681"}, "latest"]),
        ("eth_call", [{"to": V3_POOL, "data": "0xd21220a7"}, "latest"]),
        ("eth_call", [{"to": V3_POOL, "data": "0x1a686502"}, "latest"]),
        ("eth_call", [{"to": LIVE_Q, "data": "0x97f2f1a8"}, "latest"]),
        ("eth_call", [{"to": LIVE_Q, "data": "0x68e40ef4"}, "latest"]),
        ("eth_call", [{"to": LIVE_Q, "data": "0x1b6601b8"}, "latest"]),
        ("eth_call", [{"to": LIVE_Q, "data": "0x225d9741"}, "latest"]),
    ):
        calls.append(rpc(method, params))
    request = Request(PUBLIC_RPC, json.dumps(calls).encode(),
                      {"Content-Type": "application/json", "User-Agent": "curl/8.7.1"})
    with urlopen(request, timeout=6) as response:
        payload = response.read(256_001)
    if len(payload) > 256_000:
        raise ValueError("oversized RPC response")
    rows = json.loads(payload)
    if not isinstance(rows, list) or len(rows) != len(calls):
        raise ValueError("incomplete RPC batch")
    values = {row.get("id"): row.get("result") for row in rows if isinstance(row, dict)
              and "error" not in row}
    if len(values) != len(calls):
        raise ValueError("RPC batch failed")
    return values


def _rpc_address(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        return None
    return "0x" + value[-40:].lower()


def _hex_int(value: Any) -> int | None:
    try:
        return int(value, 16) if isinstance(value, str) and value.startswith("0x") else None
    except ValueError:
        return None


def chain_status(current: datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": "Robinhood public JSON-RPC", "checkedAt": None, "state": "unavailable",
        "chainId": None, "latestBlock": None, "qDeployed": None,
        "launchConfirmed": None, "launchBlock": None, "poolVerified": None,
        "poolLiquidity": None, "transferTriggerBound": None, "automaticEnabled": None,
        "pendingEntryCount": None, "successfulExecutorSteps": None,
    }
    try:
        rows = _rpc_batch()
        result["checkedAt"] = iso(current or utc_now())
        result["chainId"] = _hex_int(rows[1])
        result["latestBlock"] = _hex_int(rows[2])
        if result["chainId"] != CHAIN_ID:
            result["state"] = "wrong_chain"
            return result
        result["qDeployed"] = isinstance(rows[3], str) and rows[3] not in ("0x", "0x0")
        receipt = rows[4]
        if isinstance(receipt, dict):
            logs = receipt.get("logs")
            q_in_logs = isinstance(logs, list) and any(
                isinstance(item, dict) and str(item.get("address", "")).lower() == LIVE_Q.lower()
                for item in logs)
            result["launchConfirmed"] = (
                receipt.get("status") == "0x1" and
                str(receipt.get("to", "")).lower() == POOLS_INSTANT_STRATEGY.lower() and
                q_in_logs)
            result["launchBlock"] = _hex_int(receipt.get("blockNumber"))
        else:
            result["launchConfirmed"] = False
        pool_code = isinstance(rows[5], str) and rows[5] not in ("0x", "0x0")
        liquidity = _hex_int(rows[9])
        result["poolLiquidity"] = liquidity
        result["poolVerified"] = (pool_code and
            _rpc_address(rows[6]) == V3_POOL.lower() and
            _rpc_address(rows[7]) == USDG.lower() and
            _rpc_address(rows[8]) == LIVE_Q.lower() and
            liquidity is not None and liquidity > 0)
        result["transferTriggerBound"] = _rpc_address(rows[10]) == V3_POOL.lower()
        result["automaticEnabled"] = _hex_int(rows[11]) == 1
        result["pendingEntryCount"] = _hex_int(rows[12])
        result["successfulExecutorSteps"] = _hex_int(rows[13])
        result["state"] = "verified" if all(result[k] for k in (
            "qDeployed", "launchConfirmed", "poolVerified", "transferTriggerBound",
            "automaticEnabled")) else "partial"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return result


def _fly_command(*args: str) -> str:
    executable = shutil.which("fly") or str(Path.home() / ".fly" / "bin" / "fly")
    output = subprocess.run([executable, *args], capture_output=True, text=True,
                            timeout=8, check=True).stdout
    if len(output) > 512_000:
        raise ValueError("oversized Fly response")
    return output


def fly_status(current: datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": "Fly Machine list and remote supervisor heartbeat", "checkedAt": None,
        "state": "unavailable", "machineCount": None, "runningCount": None,
        "deploymentMode": None, "status": None, "mode": None, "dequeueMode": None,
        "writesEnabled": None, "updatedAt": None, "heartbeatFresh": False,
        "watcherStatus": None, "keeperFresh": {name: False for name in KEEPER_NAMES},
        "consecutiveFailures": None, "lastError": None,
    }
    try:
        machines = json.loads(_fly_command("machines", "list", "--app", FLY_APP, "--json"))
        if not isinstance(machines, list):
            raise ValueError("invalid Machine list")
        result["checkedAt"] = iso(current or utc_now())
        result["machineCount"] = len(machines)
        result["runningCount"] = sum(m.get("state") == "started" for m in machines
                                     if isinstance(m, dict))
        if len(machines) != 1 or not isinstance(machines[0], dict):
            result["state"] = "attention"
            return result
        environment = machines[0].get("config", {}).get("env", {})
        result["deploymentMode"] = environment.get("PONS_KEEPER_MODE") if isinstance(environment, dict) else None
        raw = _fly_command("ssh", "console", "--app", FLY_APP, "--command",
                           "cat /app/.local/pons-keeper-supervisor-status.json")
        start = raw.find("{")
        if start < 0:
            raise ValueError("remote heartbeat missing")
        heartbeat, _ = json.JSONDecoder().raw_decode(raw[start:])
        if not isinstance(heartbeat, dict) or heartbeat.get("schemaVersion") != 1:
            raise ValueError("invalid remote heartbeat")
        observed = current or utc_now()
        result["checkedAt"] = iso(observed)
        updated = parse_time(heartbeat.get("updatedAt"))
        times = heartbeat.get("lastCompletedAtByKeeper")
        if not isinstance(times, dict):
            times = {}
        error = heartbeat.get("lastError")
        if isinstance(error, dict) and error.get("message") in SAFE_ERROR_MESSAGES:
            result["lastError"] = error["message"]
        result.update(
            status=heartbeat.get("status") if heartbeat.get("status") in SUPERVISOR_STATUSES else None,
            mode=heartbeat.get("mode") if heartbeat.get("mode") in ("live", "standby") else None,
            dequeueMode=heartbeat.get("dequeueMode") if heartbeat.get("dequeueMode") in
                        ("keeper", "trader_transfer") else None,
            writesEnabled=heartbeat.get("writesEnabled") is True,
            updatedAt=iso(updated) if updated else None,
            heartbeatFresh=fresh(updated, observed, SUPERVISOR_FRESH_SECONDS),
            watcherStatus=heartbeat.get("watcherStatus") if
                heartbeat.get("watcherStatus") in WATCHER_STATUSES else None,
            keeperFresh={name: fresh(parse_time(times.get(name)), observed, CYCLE_FRESH_SECONDS)
                         for name in KEEPER_NAMES},
            consecutiveFailures=heartbeat.get("consecutiveFailures") if
                is_number(heartbeat.get("consecutiveFailures")) else None,
        )
        result["state"] = "running_reported" if (
            result["runningCount"] == 1 and result["deploymentMode"] == "trader-paid" and
            result["mode"] == "live" and result["dequeueMode"] == "trader_transfer" and
            result["writesEnabled"] and result["status"] == "running" and
            result["watcherStatus"] == "process_running" and result["heartbeatFresh"] and
            result["keeperFresh"]["exit"] and result["keeperFresh"]["price"] and
            result["consecutiveFailures"] == 0) else "attention"
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return result


def live_status() -> dict[str, Any]:
    global _probe_cache
    with _probe_lock:
        now = time.monotonic()
        if _probe_cache and now - _probe_cache[0] < PROBE_TTL_SECONDS:
            return _probe_cache[1]
        current = utc_now()
        report = {"generatedAt": iso(current), "q": LIVE_Q, "launchTx": LAUNCH_TX,
                  "v3Pool": V3_POOL, "usdg": USDG,
                  "chain": chain_status(current), "fly": fly_status()}
        _probe_cache = (time.monotonic(), report)
        return report


class Handler(BaseHTTPRequestHandler):
    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()

    def do_GET(self) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            body = b"Local host only\n"
            self._headers(403, "text/plain; charset=utf-8", len(body))
        elif self.path == "/":
            body = PAGE.read_bytes()
            self._headers(200, "text/html; charset=utf-8", len(body))
        elif self.path == "/api/status":
            body = json.dumps(snapshot(getattr(self.server, "journal_dir", LOCAL)),
                              separators=(",", ":")).encode()
            self._headers(200, "application/json; charset=utf-8", len(body))
        elif self.path == "/api/live":
            body = json.dumps(live_status(), separators=(",", ":")).encode()
            self._headers(200, "application/json; charset=utf-8", len(body))
        else:
            body = b"Not found\n"
            self._headers(404, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = b"Read-only dashboard\n"
        self._headers(405, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    if not 0 < args.port < 65536:
        parser.error("port must be between 1 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Q status dashboard: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
