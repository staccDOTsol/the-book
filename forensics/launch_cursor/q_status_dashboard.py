#!/usr/bin/env python3
"""Loopback-only, read-only status page for the hookless Q prototype.

Reads fixed local journals and exposes only selected operational fields. It
never loads signer keys, imports the keepers, calls an RPC, or starts a child.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


HERE = Path(__file__).resolve().parent
LOCAL = HERE.parents[1] / ".local"
PAGE = HERE / "q_status_dashboard.html"
MAX_JOURNAL_BYTES = 512_000
SUPERVISOR_FRESH_SECONDS = 90
CYCLE_FRESH_SECONDS = 120
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
JOURNALS = {
    "watcher": ("pons-launch-watcher.json", "pending"),
    "price": ("pons-price-keeper.json", "pendingTx"),
    "exit": ("pons-exit-keeper.json", "pendingTx"),
}
SUPERVISOR_FILE = "pons-keeper-supervisor-status.json"
SUPERVISOR_STATUSES = {"starting", "running", "retrying", "stopped"}
WATCHER_STATUSES = {"not_started", "process_running", "restarting", "retrying", "stopped"}
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
        "updatedAt": None, "fresh": False, "lastCompletedKeepers": [],
        "lastCompletedAt": None, "cycleFresh": False,
        "lastCompletedAtByKeeper": {"exit": None, "price": None},
        "keeperFresh": {"exit": False, "price": False},
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
        raw_keeper_times = {"exit": None, "price": None}
    if (not isinstance(raw_keeper_times, dict) or
            set(raw_keeper_times) != {"exit", "price"}):
        result["state"] = "invalid"
        return result
    keeper_times = {name: parse_time(raw_keeper_times[name]) for name in ("exit", "price")}
    if (not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or
            data.get("schemaVersion") != 1 or
            not isinstance(data.get("status"), str) or
            data.get("status") not in SUPERVISOR_STATUSES or
            not isinstance(data.get("watcherStatus"), str) or
            data.get("watcherStatus") not in WATCHER_STATUSES or updated is None or
            (data.get("lastCompletedAt") is not None and completed_at is None) or
            not isinstance(completed, list) or
            any(name not in ("price", "exit") for name in completed) or
            len(set(completed)) != len(completed) or
            any(raw_keeper_times[name] is not None and keeper_times[name] is None
                for name in ("exit", "price")) or
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
        updatedAt=iso(updated), fresh=fresh(updated, current, SUPERVISOR_FRESH_SECONDS),
        lastCompletedKeepers=completed,
        lastCompletedAt=iso(completed_at) if completed_at else None,
        cycleFresh=fresh(completed_at, current, CYCLE_FRESH_SECONDS),
        lastCompletedAtByKeeper={name: iso(keeper_times[name]) if keeper_times[name] else None
                                 for name in ("exit", "price")},
        keeperFresh={name: fresh(keeper_times[name], current, CYCLE_FRESH_SECONDS)
                     for name in ("exit", "price")},
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
    guards = {components[name]["guard"] for name in ("price", "exit")
              if components[name]["state"] == "valid"}
    bindings_match = len(q_values) <= 1 and len(chains) <= 1 and len(guards) <= 1
    if "invalid" in states or supervisor["state"] == "invalid" or not bindings_match:
        runtime_state = "attention"
        detail = "A journal is invalid or the component bindings disagree. Inspect local state."
    elif all(state == "missing" for state in states):
        runtime_state = "unconfigured"
        detail = "No Q runtime journals found. Deployment is not verified by this local page."
    elif "missing" in states:
        runtime_state = "partial"
        detail = "Some local runtime journals exist; the strategy is not confirmed live."
    elif (supervisor["state"] == "valid" and supervisor["status"] == "running" and
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
                    "deploymentVerified": False},
        "supervisor": supervisor,
        "components": components,
    }


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
    server = HTTPServer(("127.0.0.1", args.port), Handler)
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
