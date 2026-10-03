#!/usr/bin/env python3
"""Run the owner watcher beside serial, bounded Q price and exit cycles."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOCAL = ROOT / ".local"
REQUIRED_ENV = ("PONS_HTTP_RPC_URL", "PONS_WS_RPC_URL", "PONS_CHAIN_ID",
                "PONS_Q_ADDRESS", "PONS_PRICE_GUARD_ADDRESS",
                "PONS_OWNER_PRIVATE_KEY", "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY")


class SupervisorError(Exception):
    pass


@dataclass(frozen=True)
class KeeperSpec:
    name: str
    script: Path
    state: Path
    start_block: int | None


def _pending(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    if state_path.is_symlink():
        raise SupervisorError(f"{state_path.name} must not be a symlink")
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"{state_path.name} is unreadable") from exc
    if not isinstance(state, dict) or "pendingTx" not in state:
        raise SupervisorError(f"{state_path.name} has no pending transaction field")
    pending = state["pendingTx"]
    if pending is not None and not isinstance(pending, dict):
        raise SupervisorError(f"{state_path.name} has malformed pending transaction")
    return pending is not None


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
    elif spec.name in ("price", "exit"):
        env.pop("PONS_OWNER_PRIVATE_KEY", None)
    else:
        raise SupervisorError("unknown keeper child role")
    return env


def keeper_order(price: KeeperSpec, exit_keeper: KeeperSpec) -> list[KeeperSpec]:
    price_pending, exit_pending = _pending(price.state), _pending(exit_keeper.state)
    if price_pending and exit_pending:
        raise SupervisorError("both configurator keepers have pending signed transactions")
    if price_pending:
        return [price]
    if exit_pending:
        return [exit_keeper]
    # Inspect and clear active risk before configuring another entry.
    return [exit_keeper, price]


def run_keeper_cycle(price: KeeperSpec, exit_keeper: KeeperSpec,
                     timeout_seconds: float) -> list[str]:
    completed: list[str] = []
    for spec in keeper_order(price, exit_keeper):
        cmd = child_command(spec, once=True)
        try:
            result = subprocess.run(cmd, timeout=timeout_seconds, check=False,
                                    env=child_env(spec))
        except subprocess.TimeoutExpired as exc:
            raise SupervisorError(f"{spec.name} cycle exceeded timeout") from exc
        if result.returncode != 0:
            raise SupervisorError(f"{spec.name} cycle exited {result.returncode}")
        completed.append(spec.name)
    return completed


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
    parser.add_argument("--max-consecutive-failures", type=int, default=10)
    parser.add_argument("--retry-cap-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("supervisor requires explicit --live; use individual keepers for read-only planning")
    if (args.poll_seconds <= 0 or args.child_timeout_seconds <= 0 or
            args.max_consecutive_failures < 1 or args.retry_cap_seconds <= 0):
        parser.error("invalid interval, timeout, or retry setting")
    for key in REQUIRED_ENV:
        if not os.environ.get(key):
            raise SupervisorError(f"missing process environment variable {key}")
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
    signal.signal(signal.SIGTERM, _stop)
    watcher_process: subprocess.Popen[Any] | None = None
    watcher_started_at = 0.0
    watcher_failures = 0
    next_watcher_restart_at = 0.0
    failures = 0
    try:
        while True:
            if ((watcher_process is None or watcher_process.poll() is not None) and
                    time.monotonic() >= next_watcher_restart_at):
                if watcher_process is not None:
                    if time.monotonic() - watcher_started_at > 60:
                        watcher_failures = 0
                    watcher_failures += 1
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
                    print(json.dumps({"status": "watcher_retry", "reason": str(exc),
                                      "consecutiveFailures": watcher_failures}),
                          file=sys.stderr, flush=True)
                else:
                    watcher_started_at = time.monotonic()
            # A watcher outage must not suspend inspection or exits of
            # positions that Q already opened.
            try:
                completed = run_keeper_cycle(price, exit_keeper,
                                             args.child_timeout_seconds)
                failures = 0
                print(json.dumps({"status": "cycle_complete", "keepers": completed}),
                      flush=True)
                time.sleep(args.poll_seconds)
            except (SupervisorError, OSError) as exc:
                failures += 1
                print(json.dumps({"status": "cycle_retry", "reason": str(exc),
                                  "consecutiveFailures": failures}), file=sys.stderr,
                      flush=True)
                if failures >= args.max_consecutive_failures:
                    raise SupervisorError("bounded keeper retries exhausted") from exc
                time.sleep(min(args.retry_cap_seconds,
                               args.poll_seconds * (2 ** min(failures - 1, 8))))
    except KeyboardInterrupt:
        return 0
    finally:
        if watcher_process is not None and watcher_process.poll() is None:
            watcher_process.terminate()
            try:
                watcher_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watcher_process.kill()
                watcher_process.wait(timeout=5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SupervisorError as exc:
        print(f"keeper supervisor stopped: {exc}", file=sys.stderr)
        sys.exit(1)
