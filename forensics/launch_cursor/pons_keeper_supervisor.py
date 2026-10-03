#!/usr/bin/env python3
"""Run the owner watcher beside independent Q price and exit signer cycles."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Any

from eth_account import Account


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOCAL = ROOT / ".local"
STATUS_PATH = LOCAL / "pons-keeper-supervisor-status.json"
RPC_ENV_PATH = LOCAL / "pons-rpc.env"
RPC_ENV_KEYS = {"PONS_HTTP_RPC_URL", "PONS_WS_RPC_URL", "PONS_CHAIN_ID"}
SAFE_STATUS_ERRORS = {
    "A keeper cycle exceeded its timeout.",
    "A keeper cycle exited unsuccessfully.",
    "A keeper state journal is unreadable or malformed.",
    "Supervisor check failed; inspect local logs.",
    "A local process or file operation failed; inspect local logs.",
}
REQUIRED_ENV = ("PONS_HTTP_RPC_URL", "PONS_WS_RPC_URL", "PONS_CHAIN_ID",
                "PONS_Q_ADDRESS", "PONS_PRICE_GUARD_ADDRESS")
SIGNER_ENV = ("PONS_OWNER_PRIVATE_KEY", "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY",
              "PONS_EXIT_CONFIGURATOR_PRIVATE_KEY")


class SupervisorError(Exception):
    pass


def secure_local_directory() -> None:
    """Restrict the existing repo-local runtime directory before any writes."""
    if LOCAL.is_symlink():
        raise SupervisorError(".local must not be a symlink")
    if LOCAL.exists():
        info = LOCAL.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SupervisorError(".local must be an owned directory")
        os.chmod(LOCAL, 0o700)
    else:
        LOCAL.mkdir(mode=0o700)


def load_local_rpc_env(path: Path = RPC_ENV_PATH) -> None:
    """Read the one local RPC config without evaluating shell syntax.

    Process environment wins. The file may contain only three known keys and
    must be a private regular file owned by this user. Values are never logged.
    """
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077 or info.st_size > 8192):
        raise SupervisorError("local RPC env file is not a private regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SupervisorError("local RPC env file is unreadable") from exc
    parsed: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SupervisorError("local RPC env file has invalid syntax")
        key, value = line.split("=", 1)
        if (key not in RPC_ENV_KEYS or key in parsed or not value or
                any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)):
            raise SupervisorError("local RPC env file has invalid key or value")
        parsed[key] = value
    if "PONS_CHAIN_ID" in parsed and parsed["PONS_CHAIN_ID"] != "4663":
        raise SupervisorError("local RPC env file has the wrong chain ID")
    for key, value in parsed.items():
        os.environ.setdefault(key, value)


def load_key_file(path: Path) -> str:
    """Read one raw hex key without putting it in argv or diagnostic output."""
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size > 128):
            raise SupervisorError("signer key file must be a private regular file")
        key = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise SupervisorError("signer key file is unavailable or unreadable") from exc
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", key):
        raise SupervisorError("signer key file is invalid")
    return "0x" + key.removeprefix("0x")


def load_signer_files(owner: Path, price: Path, exit_signer: Path) -> None:
    for name, path in zip(SIGNER_ENV, (owner, price, exit_signer)):
        os.environ[name] = load_key_file(path)
    verify_signer_isolation()


def gwei_wei(value: str) -> int:
    try:
        scaled = Decimal(value) * Decimal(10**9)
    except (InvalidOperation, ValueError) as exc:
        raise SupervisorError("invalid keeper fee cap") from exc
    if not scaled.is_finite() or scaled <= 0 or scaled != scaled.to_integral_value():
        raise SupervisorError("invalid keeper fee cap")
    return int(scaled)


def sanitized_error(error: Exception) -> dict[str, str]:
    """Return only fixed diagnostic text; exception strings can hold secrets."""
    if isinstance(error, SupervisorError):
        detail = str(error)
        if "cycle exceeded timeout" in detail:
            message = "A keeper cycle exceeded its timeout."
        elif re.fullmatch(r"(?:price|exit|feedback|harvest) cycle exited -?\d+", detail):
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
                 completed_by_keeper: dict[str, str | None] | None = None,
                 mode: str = "live", trader_paid: bool = False) -> None:
    """Publish a credential-free, atomic operational heartbeat in .local."""
    if status not in ("starting", "running", "retrying", "stopped"):
        raise SupervisorError("invalid supervisor status")
    if watcher_status not in ("not_started", "process_running", "restarting", "retrying", "stopped"):
        raise SupervisorError("invalid watcher status")
    if mode not in ("live", "standby"):
        raise SupervisorError("invalid supervisor mode")
    if trader_paid and mode != "live":
        raise SupervisorError("trader-paid dequeue requires live mode")
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
    keepers = ("exit", "price", "feedback", "harvest")
    if any(name not in keepers for name in completed_by_keeper):
        raise SupervisorError("invalid completed keeper name")
    keeper_times = {name: safe_time(completed_by_keeper.get(name)) for name in keepers}
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
        "mode": mode,
        "writesEnabled": mode == "live" and status in ("running", "retrying"),
        "dequeueMode": "trader_transfer" if trader_paid else "keeper" if mode == "live" else "disabled",
        "status": status,
        "watcherStatus": watcher_status,
        "lastCompletedKeepers": [name for name in completed if name in keepers],
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
class BudgetSpec:
    signer: str
    daily_wei: int
    total_wei: int
    tx_wei: int
    floor_wei: int
    max_fee_wei: int

    def validate(self) -> None:
        if (not re.fullmatch(r"0x[0-9a-f]{40}", self.signer) or
                min(self.daily_wei, self.total_wei, self.tx_wei, self.max_fee_wei) <= 0 or
                self.floor_wei < 0 or self.tx_wei > self.total_wei or
                self.tx_wei > self.daily_wei):
            raise SupervisorError("invalid keeper spend budget")


@dataclass(frozen=True)
class KeeperSpec:
    name: str
    script: Path
    state: Path
    start_block: int | None
    budget: BudgetSpec | None = None
    max_fee_gwei: str = "0.1"
    max_priority_gwei: str = "0.01"
    max_enqueues_per_cycle: int = 1
    trader_paid: bool = False


def child_command(spec: KeeperSpec, *, once: bool, live: bool = True) -> list[str]:
    if not spec.state.exists() and (spec.start_block is None or spec.start_block < 1):
        raise SupervisorError(f"first {spec.name} run needs its inclusive start block")
    cmd = [sys.executable, str(spec.script), "--state", str(spec.state)]
    if live:
        cmd.append("--live")
    if once:
        cmd.append("--once")
    if live and spec.trader_paid and spec.name in ("price", "exit", "harvest"):
        cmd.append("--no-process-next")
    if live:
        if spec.name == "feedback":
            cmd += ["--max-fee-wei", str(gwei_wei(spec.max_fee_gwei)),
                    "--max-priority-wei", str(gwei_wei(spec.max_priority_gwei))]
        else:
            cmd += ["--max-fee-gwei", spec.max_fee_gwei,
                    "--max-priority-gwei", spec.max_priority_gwei]
        if spec.name == "watcher":
            cmd += ["--max-enqueues-per-cycle", str(spec.max_enqueues_per_cycle),
                    "--enqueue-attempts", "1"]
    if not spec.state.exists():
        cmd += ["--start-block", str(spec.start_block)]
    return cmd


def child_env(spec: KeeperSpec, *, live: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("PONS_BUDGET_"):
            env.pop(key)
    if not live:
        for key in SIGNER_ENV:
            env.pop(key, None)
        return env
    if spec.name == "watcher":
        env.pop("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", None)
        env.pop("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY", None)
    elif spec.name in ("price", "feedback"):
        env.pop("PONS_OWNER_PRIVATE_KEY", None)
        env.pop("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY", None)
    elif spec.name in ("exit", "harvest"):
        env.pop("PONS_OWNER_PRIVATE_KEY", None)
        env.pop("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY", None)
    else:
        raise SupervisorError("unknown keeper child role")
    if spec.budget is None:
        raise SupervisorError("live keeper child needs a spend budget")
    budget = spec.budget
    budget.validate()
    env.update({
        "PONS_BUDGET_REQUIRED": "1",
        "PONS_BUDGET_SIGNER": budget.signer,
        "PONS_BUDGET_DAILY_WEI": str(budget.daily_wei),
        "PONS_BUDGET_TOTAL_WEI": str(budget.total_wei),
        "PONS_BUDGET_TX_WEI": str(budget.tx_wei),
        "PONS_BUDGET_FLOOR_WEI": str(budget.floor_wei),
        "PONS_BUDGET_MAX_FEE_WEI": str(budget.max_fee_wei),
        "PONS_BUDGET_JOURNAL": str(LOCAL / "pons-keeper-budget.json"),
    })
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
    live: bool = True
    process: subprocess.Popen[Any] | None = None
    started_at: float = 0.0
    retry_after: float = 0.0
    failures: int = 0


def has_pending_transaction(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_symlink():
        raise SupervisorError("keeper state journal is unreadable or malformed")
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SupervisorError("keeper state journal is unreadable or malformed") from exc
    if not isinstance(state, dict) or "pendingTx" not in state:
        raise SupervisorError("keeper state journal is unreadable or malformed")
    return state["pendingTx"] is not None


def reject_pending_dequeue(path: Path) -> None:
    """Do not recover a keeper-paid process transaction after mode switch."""
    if not path.exists():
        return
    if path.is_symlink():
        raise SupervisorError("keeper state journal is unreadable or malformed")
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SupervisorError("keeper state journal is unreadable or malformed") from exc
    if not isinstance(state, dict) or "pendingTx" not in state:
        raise SupervisorError("keeper state journal is unreadable or malformed")
    pending = state["pendingTx"]
    if pending is not None and not isinstance(pending, dict):
        raise SupervisorError("keeper state journal is unreadable or malformed")
    if pending is not None and pending.get("kind") == "process":
        raise SupervisorError("trader-paid mode has a pending keeper processNext transaction")


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
        if job.live and job.spec.trader_paid:
            reject_pending_dequeue(job.spec.state)
        job.process = subprocess.Popen(child_command(job.spec, once=True, live=job.live),
                                       env=child_env(job.spec, live=job.live))
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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="enable bounded onchain writes")
    mode.add_argument("--standby", action="store_true", help="run read-only child checks and heartbeat")
    parser.add_argument("--trader-paid", action="store_true",
                        help="in live mode, leave Q.processNext to Q/USDG v3 trader transfers")
    parser.add_argument("--owner-key-file", type=Path)
    parser.add_argument("--price-key-file", type=Path)
    parser.add_argument("--exit-key-file", type=Path)
    parser.add_argument("--expected-owner")
    parser.add_argument("--expected-price")
    parser.add_argument("--expected-exit")
    for role in ("owner", "price", "exit"):
        parser.add_argument(f"--{role}-daily-cap-wei", type=int)
        parser.add_argument(f"--{role}-total-cap-wei", type=int)
        parser.add_argument(f"--{role}-tx-cap-wei", type=int)
        parser.add_argument(f"--{role}-balance-floor-wei", type=int)
    parser.add_argument("--max-fee-gwei", default="0.1")
    parser.add_argument("--max-priority-gwei", default="0.01")
    parser.add_argument("--max-enqueues-per-cycle", type=int, default=1)
    parser.add_argument("--watcher-start-block", type=int)
    parser.add_argument("--price-start-block", type=int)
    parser.add_argument("--exit-start-block", type=int)
    parser.add_argument("--feedback-start-block", type=int)
    parser.add_argument("--watcher-state", type=Path, default=LOCAL / "pons-launch-watcher.json")
    parser.add_argument("--price-state", type=Path, default=LOCAL / "pons-price-keeper.json")
    parser.add_argument("--exit-state", type=Path, default=LOCAL / "pons-exit-keeper.json")
    parser.add_argument("--feedback-state", type=Path, default=LOCAL / "pons-fee-feedback.json")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--child-timeout-seconds", type=float, default=600)
    parser.add_argument("--retry-cap-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    if args.trader_paid and not args.live:
        parser.error("--trader-paid requires --live")
    if (args.poll_seconds <= 0 or args.child_timeout_seconds <= 0 or
            args.retry_cap_seconds <= 0):
        parser.error("invalid interval, timeout, or retry setting")
    max_fee_wei = gwei_wei(args.max_fee_gwei)
    priority_wei = gwei_wei(args.max_priority_gwei)
    if priority_wei > max_fee_wei or args.max_enqueues_per_cycle < 1:
        parser.error("invalid fee or enqueue cap")
    secure_local_directory()
    load_local_rpc_env()
    for key in REQUIRED_ENV:
        if not os.environ.get(key):
            raise SupervisorError(f"missing process environment variable {key}")
    if os.environ["PONS_CHAIN_ID"] != "4663":
        raise SupervisorError("keeper supervisor requires Robinhood chain 4663")
    budgets: dict[str, BudgetSpec] = {}
    if args.live:
        if not all((args.owner_key_file, args.price_key_file, args.exit_key_file,
                    args.expected_owner, args.expected_price, args.expected_exit)):
            parser.error("live mode requires three key files and expected signer addresses")
        load_signer_files(args.owner_key_file, args.price_key_file, args.exit_key_file)
        expected = (args.expected_owner, args.expected_price, args.expected_exit)
        for role, env_key, supplied in zip(("owner", "price", "exit"), SIGNER_ENV, expected):
            derived = Account.from_key(os.environ[env_key]).address.lower()
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", supplied) or derived != supplied.lower():
                raise SupervisorError("keeper signer does not match expected public address")
            settings = [getattr(args, f"{role}_{field}") for field in (
                "daily_cap_wei", "total_cap_wei", "tx_cap_wei", "balance_floor_wei")]
            if any(value is None for value in settings):
                parser.error("live mode requires daily, total, transaction, and balance-floor caps per signer")
            budget = BudgetSpec(derived, *settings, max_fee_wei)
            budget.validate()
            budgets[role] = budget
    else:
        # The read-only rehearsal uses separate cursors and never loads keys.
        for key in SIGNER_ENV:
            os.environ.pop(key, None)
    states = [args.watcher_state, args.price_state, args.exit_state, args.feedback_state]
    if args.standby:
        states = [path.with_name(path.stem + ".standby" + path.suffix) for path in states]
    for path in states:
        if path.resolve().parent != LOCAL.resolve():
            raise SupervisorError("all keeper states must be directly inside repository .local")
    if len({path.resolve() for path in states}) != 4:
        raise SupervisorError("keepers need distinct state files")
    def spec(name: str, script: str, state: Path, start: int | None,
             role: str) -> KeeperSpec:
        return KeeperSpec(name, HERE / script, state, start, budgets.get(role),
                          args.max_fee_gwei, args.max_priority_gwei,
                          args.max_enqueues_per_cycle,
                          args.trader_paid and name in ("price", "exit", "harvest"))
    watcher = spec("watcher", "pons_launch_watcher.py", states[0],
                   args.watcher_start_block, "owner")
    price = spec("price", "pons_price_keeper.py", states[1],
                 args.price_start_block, "price")
    exit_keeper = spec("exit", "pons_exit_keeper.py", states[2],
                       args.exit_start_block, "exit")
    harvest = spec("harvest", "pons_harvest_keeper.py", states[2],
                   args.exit_start_block, "exit")
    feedback = spec("feedback", "pons_fee_feedback_keeper.py", states[3],
                    args.feedback_start_block or args.price_start_block, "price")
    # Validate first-run cursors before launching any writer.
    for spec in (watcher, price, exit_keeper, harvest, feedback):
        child_command(spec, once=True, live=args.live)
    watcher_job = KeeperJob(watcher, live=args.live)
    exit_job, harvest_job = KeeperJob(exit_keeper, live=args.live), KeeperJob(harvest, live=args.live)
    price_job, feedback_job = KeeperJob(price, live=args.live), KeeperJob(feedback, live=args.live)
    signal.signal(signal.SIGTERM, _stop)
    failures = 0
    watcher_status = "not_started"
    completed: list[str] = []
    completed_at: str | None = None
    completed_by_keeper: dict[str, str | None] = {
        "exit": None, "harvest": None, "price": None, "feedback": None}
    last_error: dict[str, str] | None = None
    write_status(status="starting", watcher_status=watcher_status,
                 completed=completed, completed_at=completed_at, failures=failures,
                 watcher_failures=0, last_error=last_error,
                 completed_by_keeper=completed_by_keeper,
                 mode="live" if args.live else "standby", trader_paid=args.trader_paid)
    try:
        while True:
            _, watcher_error = service_keeper(watcher_job, time.monotonic(),
                args.child_timeout_seconds, args.poll_seconds, args.retry_cap_seconds)
            if watcher_error:
                last_error = sanitized_error(watcher_error)
                print(json.dumps({"status": "watcher_retry",
                                  "reason": str(watcher_error),
                                  "consecutiveFailures": watcher_job.failures}),
                      file=sys.stderr, flush=True)
            watcher_status = ("process_running" if watcher_job.process is not None else
                              "retrying" if watcher_job.failures else "not_started")
            # Exit and harvest share one signer journal. Price and feedback
            # share the other signer key but keep separate durable journals.
            # Service each lane serially, while the two lanes run in parallel.
            completed_now: list[str] = []
            def service(job: KeeperJob) -> None:
                nonlocal last_error
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
            if harvest_job.process is not None and harvest_job.process.poll() is None:
                service(harvest_job)
            else:
                service(exit_job)
                if exit_job.process is None and not has_pending_transaction(states[2]):
                    service(harvest_job)
            if feedback_job.process is not None and feedback_job.process.poll() is None:
                service(feedback_job)
            else:
                service(price_job)
                if price_job.process is None and not has_pending_transaction(states[1]):
                    service(feedback_job)
            failures = max(exit_job.failures, harvest_job.failures,
                           price_job.failures, feedback_job.failures)
            if completed_now:
                completed = completed_now
                completed_at = datetime.now(timezone.utc).isoformat()
                for name in completed_now:
                    completed_by_keeper[name] = completed_at
                print(json.dumps({"status": "cycle_complete", "keepers": completed_now}),
                      flush=True)
            write_status(status="retrying" if failures or watcher_job.failures else "running",
                         watcher_status=watcher_status, completed=completed,
                         completed_at=completed_at, failures=failures,
                         watcher_failures=watcher_job.failures, last_error=last_error,
                         completed_by_keeper=completed_by_keeper,
                         mode="live" if args.live else "standby", trader_paid=args.trader_paid)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        for job in (watcher_job, exit_job, harvest_job, price_job, feedback_job):
            _stop_job(job)
        write_status(status="stopped", watcher_status="stopped",
                     completed=completed, completed_at=completed_at,
                     failures=failures, watcher_failures=watcher_job.failures,
                     last_error=last_error,
                     completed_by_keeper=completed_by_keeper,
                     mode="live" if args.live else "standby", trader_paid=args.trader_paid)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SupervisorError as exc:
        print(f"keeper supervisor stopped: {exc}", file=sys.stderr)
        sys.exit(1)
