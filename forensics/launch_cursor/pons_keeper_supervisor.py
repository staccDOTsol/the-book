#!/usr/bin/env python3
"""Run the owner watcher beside independent Q price and exit signer cycles."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any

from eth_account import Account


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOCAL = ROOT / ".local"
STATUS_PATH = LOCAL / "pons-keeper-supervisor-status.json"
SAFE_STATUS_ERRORS = {
    "A keeper cycle exceeded its timeout.",
    "A keeper cycle exited unsuccessfully.",
    "A keeper state journal is unreadable or malformed.",
    "Supervisor check failed; inspect local logs.",
    "A local process or file operation failed; inspect local logs.",
}
REQUIRED_ENV = ("PONS_HTTP_RPC_URL", "PONS_WS_RPC_URL", "PONS_CHAIN_ID",
                "PONS_Q_ADDRESS", "PONS_PRICE_GUARD_ADDRESS",
                "PONS_OWNER_PRIVATE_KEY", "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY",
                "PONS_EXIT_CONFIGURATOR_PRIVATE_KEY")


class SupervisorError(Exception):
    pass


def sanitized_error(error: Exception) -> dict[str, str]:
    """Return only fixed diagnostic text; exception strings can hold secrets."""
    if isinstance(error, SupervisorError):
        detail = str(error)
        if "cycle exceeded timeout" in detail:
            message = "A keeper cycle exceeded its timeout."
        elif re.fullmatch(r"(?:price|exit) cycle exited -?\d+", detail):
            message = "A keeper cycle exited unsuccessfully."
        elif "unreadable" in detail or "malformed" in detail:
            message = "A keeper state journal is unreadable or malformed."
        else:
            message = "Supervisor check failed; inspect local logs."
        kind = "SupervisorError"
    else:
        message = "A local process or file operation failed; inspect local logs."
        kind = "OSError" if isinstance(error, OSError) else "Error"
    return {"type": kind, "message": message}


def write_status(*, status: str, watcher_status: str, completed: list[str],
                 completed_at: str | None, failures: int, watcher_failures: int,
                 last_error: dict[str, str] | None,
                 completed_by_keeper: dict[str, str | None] | None = None) -> None:
    """Publish a credential-free, atomic operational heartbeat in .local."""
    if status not in ("starting", "running", "retrying", "stopped"):
        raise SupervisorError("invalid supervisor status")
    if watcher_status not in ("not_started", "process_running", "restarting", "retrying", "stopped"):
        raise SupervisorError("invalid watcher status")
    def safe_time(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 40:
            raise SupervisorError("invalid completion timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise SupervisorError("invalid completion timestamp") from exc
        if parsed.tzinfo is None:
            raise SupervisorError("completion timestamp has no time zone")
        return parsed.astimezone(timezone.utc).isoformat()
    completed_at = safe_time(completed_at)
    completed_by_keeper = completed_by_keeper or {}
    if any(name not in ("exit", "price") for name in completed_by_keeper):
        raise SupervisorError("invalid completed keeper name")
    keeper_times = {name: safe_time(completed_by_keeper.get(name)) for name in ("exit", "price")}
    if last_error is not None:
        if (not isinstance(last_error, dict) or
                not isinstance(last_error.get("type"), str) or
                last_error.get("type") not in ("SupervisorError", "OSError", "Error") or
                not isinstance(last_error.get("message"), str) or
                last_error.get("message") not in SAFE_STATUS_ERRORS):
            last_error = {"type": "Error", "message": "Supervisor check failed; inspect local logs."}
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    if LOCAL.is_symlink():
        raise SupervisorError(".local must not be a symlink")
    payload = {
        "schemaVersion": 1,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "watcherStatus": watcher_status,
        "lastCompletedKeepers": [name for name in completed if name in ("price", "exit")],
        "lastCompletedAt": completed_at,
        "lastCompletedAtByKeeper": keeper_times,
        "consecutiveFailures": failures,
        "watcherConsecutiveFailures": watcher_failures,
        "lastError": last_error,
    }
    staging = STATUS_PATH.with_suffix(STATUS_PATH.suffix + f".{os.getpid()}.new")
    try:
        with open(staging, "x", encoding="utf-8") as file:
            os.chmod(staging, 0o600)
            json.dump(payload, file, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(staging, STATUS_PATH)
        directory = os.open(LOCAL, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staging.unlink(missing_ok=True)


@dataclass(frozen=True)
class KeeperSpec:
    name: str
    script: Path
    state: Path
    start_block: int | None


def child_command(spec: KeeperSpec, *, once: bool) -> list[str]:
    if not spec.state.exists() and (spec.start_block is None or spec.start_block < 1):
        raise SupervisorError(f"first {spec.name} run needs its inclusive start block")
    cmd = [sys.executable, str(spec.script), "--live", "--state", str(spec.state)]
    if once:
        cmd.append("--once")
    if not spec.state.exists():
        cmd += ["--start-block", str(spec.start_block)]
    return cmd


def child_env(spec: KeeperSpec) -> dict[str, str]:
    env = os.environ.copy()
    if spec.name == "watcher":
        env.pop("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", None)
        env.pop("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY", None)
    elif spec.name == "price":
        env.pop("PONS_OWNER_PRIVATE_KEY", None)
        env.pop("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY", None)
    elif spec.name == "exit":
        env.pop("PONS_OWNER_PRIVATE_KEY", None)
        env.pop("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", None)
    else:
        raise SupervisorError("unknown keeper child role")
    return env


def verify_signer_isolation() -> None:
    try:
        addresses = [Account.from_key(os.environ[name]).address.lower() for name in (
            "PONS_OWNER_PRIVATE_KEY", "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY",
            "PONS_EXIT_CONFIGURATOR_PRIVATE_KEY")]
    except Exception as exc:
        raise SupervisorError("invalid keeper signing key") from exc
    if len(set(addresses)) != 3:
        raise SupervisorError("owner, price, and exit signing accounts must differ")


@dataclass
class KeeperJob:
    spec: KeeperSpec
    process: subprocess.Popen[Any] | None = None
    started_at: float = 0.0
    retry_after: float = 0.0
    failures: int = 0


def _stop_job(job: KeeperJob) -> None:
    if job.process is None or job.process.poll() is not None:
        return
    job.process.terminate()
    try:
        job.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        job.process.kill()
        job.process.wait(timeout=5)


def service_keeper(job: KeeperJob, now: float, timeout_seconds: float,
                   poll_seconds: float, retry_cap_seconds: float) -> tuple[str | None, Exception | None]:
    """Service one signer without waiting for another signer's subprocess."""
    if job.process is not None:
        returncode = job.process.poll()
        if returncode is None and now - job.started_at > timeout_seconds:
            _stop_job(job)
            returncode = job.process.returncode
            error: Exception | None = SupervisorError(f"{job.spec.name} cycle exceeded timeout")
        else:
            error = (SupervisorError(f"{job.spec.name} cycle exited {returncode}")
                     if returncode not in (None, 0) else None)
        if returncode is None:
            return None, None
        job.process = None
        if error is not None:
            job.failures += 1
            job.retry_after = now + min(retry_cap_seconds,
                poll_seconds * (2 ** min(job.failures - 1, 8)))
            return None, error
        job.failures = 0
        job.retry_after = now + poll_seconds
        return job.spec.name, None
    if now < job.retry_after:
        return None, None
    try:
        job.process = subprocess.Popen(child_command(job.spec, once=True),
                                       env=child_env(job.spec))
    except (OSError, SupervisorError) as exc:
        job.failures += 1
        job.retry_after = now + min(retry_cap_seconds,
            poll_seconds * (2 ** min(job.failures - 1, 8)))
        return None, exc
    job.started_at = now
    return None, None


def _stop(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required opt-in for onchain writes")
    parser.add_argument("--watcher-start-block", type=int)
    parser.add_argument("--price-start-block", type=int)
    parser.add_argument("--exit-start-block", type=int)
    parser.add_argument("--watcher-state", type=Path, default=LOCAL / "pons-launch-watcher.json")
    parser.add_argument("--price-state", type=Path, default=LOCAL / "pons-price-keeper.json")
    parser.add_argument("--exit-state", type=Path, default=LOCAL / "pons-exit-keeper.json")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--child-timeout-seconds", type=float, default=600)
    parser.add_argument("--retry-cap-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("supervisor requires explicit --live; use individual keepers for read-only planning")
    if (args.poll_seconds <= 0 or args.child_timeout_seconds <= 0 or
            args.retry_cap_seconds <= 0):
        parser.error("invalid interval, timeout, or retry setting")
    for key in REQUIRED_ENV:
        if not os.environ.get(key):
            raise SupervisorError(f"missing process environment variable {key}")
    verify_signer_isolation()
    for path in (args.watcher_state, args.price_state, args.exit_state):
        if path.resolve().parent != LOCAL.resolve():
            raise SupervisorError("all keeper states must be directly inside repository .local")
    if len({args.watcher_state.resolve(), args.price_state.resolve(), args.exit_state.resolve()}) != 3:
        raise SupervisorError("keepers need distinct state files")
    watcher = KeeperSpec("watcher", HERE / "pons_launch_watcher.py",
                         args.watcher_state, args.watcher_start_block)
    price = KeeperSpec("price", HERE / "pons_price_keeper.py",
                       args.price_state, args.price_start_block)
    exit_keeper = KeeperSpec("exit", HERE / "pons_exit_keeper.py",
                             args.exit_state, args.exit_start_block)
    # Validate first-run cursors before launching any writer.
    for spec in (watcher, price, exit_keeper):
        child_command(spec, once=spec.name != "watcher")
    exit_job, price_job = KeeperJob(exit_keeper), KeeperJob(price)
    signal.signal(signal.SIGTERM, _stop)
    watcher_process: subprocess.Popen[Any] | None = None
    watcher_started_at = 0.0
    watcher_failures = 0
    next_watcher_restart_at = 0.0
    failures = 0
    watcher_status = "not_started"
    completed: list[str] = []
    completed_at: str | None = None
    completed_by_keeper: dict[str, str | None] = {"exit": None, "price": None}
    last_error: dict[str, str] | None = None
    write_status(status="starting", watcher_status=watcher_status,
                 completed=completed, completed_at=completed_at, failures=failures,
                 watcher_failures=watcher_failures, last_error=last_error,
                 completed_by_keeper=completed_by_keeper)
    try:
        while True:
            if (watcher_process is not None and watcher_process.poll() is not None and
                    time.monotonic() < next_watcher_restart_at):
                watcher_status = "retrying"
            if ((watcher_process is None or watcher_process.poll() is not None) and
                    time.monotonic() >= next_watcher_restart_at):
                if watcher_process is not None:
                    if time.monotonic() - watcher_started_at > 60:
                        watcher_failures = 0
                    watcher_failures += 1
                    watcher_status = "restarting"
                    print(json.dumps({"status": "watcher_restart",
                                      "exitCode": watcher_process.returncode,
                                      "consecutiveFailures": watcher_failures}), flush=True)
                backoff = min(args.retry_cap_seconds,
                              args.poll_seconds * (2 ** min(max(watcher_failures - 1, 0), 8)))
                next_watcher_restart_at = time.monotonic() + backoff
                try:
                    watcher_process = subprocess.Popen(child_command(watcher, once=False),
                                                       env=child_env(watcher))
                except OSError as exc:
                    watcher_process = None
                    watcher_failures += 1
                    watcher_status = "retrying"
                    last_error = sanitized_error(exc)
                    print(json.dumps({"status": "watcher_retry", "reason": str(exc),
                                      "consecutiveFailures": watcher_failures}),
                          file=sys.stderr, flush=True)
                else:
                    watcher_started_at = time.monotonic()
                    watcher_status = "process_running"
                write_status(status="running", watcher_status=watcher_status,
                             completed=completed, completed_at=completed_at,
                             failures=failures, watcher_failures=watcher_failures,
                             last_error=last_error,
                             completed_by_keeper=completed_by_keeper)
            # Always service the exit slot first. Neither an owner watcher
            # outage nor a slow/pending price transaction delays the next
            # exit inspection; each signer recovers only its own journal.
            completed_now: list[str] = []
            for job in (exit_job, price_job):
                name, error = service_keeper(job, time.monotonic(),
                    args.child_timeout_seconds, args.poll_seconds, args.retry_cap_seconds)
                if name:
                    completed_now.append(name)
                if error:
                    last_error = sanitized_error(error)
                    print(json.dumps({"status": "cycle_retry", "keeper": job.spec.name,
                                      "reason": str(error),
                                      "consecutiveFailures": job.failures}),
                          file=sys.stderr, flush=True)
            failures = max(exit_job.failures, price_job.failures)
            if completed_now:
                completed = completed_now
                completed_at = datetime.now(timezone.utc).isoformat()
                for name in completed_now:
                    completed_by_keeper[name] = completed_at
                print(json.dumps({"status": "cycle_complete", "keepers": completed_now}),
                      flush=True)
            if watcher_process is not None and watcher_process.poll() is not None:
                watcher_status = "retrying"
            write_status(status="retrying" if failures else "running",
                         watcher_status=watcher_status, completed=completed,
                         completed_at=completed_at, failures=failures,
                         watcher_failures=watcher_failures, last_error=last_error,
                         completed_by_keeper=completed_by_keeper)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        _stop_job(exit_job)
        _stop_job(price_job)
        if watcher_process is not None and watcher_process.poll() is None:
            watcher_process.terminate()
            try:
                watcher_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watcher_process.kill()
                watcher_process.wait(timeout=5)
        write_status(status="stopped", watcher_status="stopped",
                     completed=completed, completed_at=completed_at,
                     failures=failures, watcher_failures=watcher_failures,
                     last_error=last_error,
                     completed_by_keeper=completed_by_keeper)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SupervisorError as exc:
        print(f"keeper supervisor stopped: {exc}", file=sys.stderr)
        sys.exit(1)
