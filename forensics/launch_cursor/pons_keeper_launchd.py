#!/usr/bin/env python3
"""Prepare or run a macOS launchd keeper job without putting keys in argv.

The local JSON config contains only public addresses, key *paths*, and spend
caps. `prepare` writes a reviewable plist into .local; it does not install or
start a LaunchAgent. `run` validates the private config, then execs the
supervisor. launchd can restart it after crashes or reboot.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOCAL = ROOT / ".local"
DEFAULT_CONFIG = LOCAL / "pons-keeper-runtime.json"
DEFAULT_PLIST = LOCAL / "pons-keeper.launchd.plist"
LABEL = "org.thebook.pons-keeper"


class LaunchdError(Exception):
    pass


def read_config(path: Path) -> tuple[list[str], dict[str, str]]:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size > 16_384):
            raise LaunchdError("keeper runtime config must be a private regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise LaunchdError("keeper runtime config is unavailable or malformed") from exc
    if (not isinstance(value, dict) or value.get("schemaVersion") != 1 or
            not isinstance(value.get("arguments"), list) or
            not value["arguments"] or
            any(not isinstance(item, str) or not item or len(item) > 512 or
                "\x00" in item for item in value["arguments"])):
        raise LaunchdError("keeper runtime config is malformed")
    args = value["arguments"]
    if ("--live" in args) == ("--standby" in args):
        raise LaunchdError("keeper runtime config needs exactly one mode")
    if any("PRIVATE_KEY" in item or item.startswith("0x") and len(item) == 66 for item in args):
        raise LaunchdError("keeper runtime config may not contain raw keys")
    public_env = {}
    for field, key in (("qAddress", "PONS_Q_ADDRESS"),
                       ("priceGuardAddress", "PONS_PRICE_GUARD_ADDRESS")):
        address = value.get(field)
        if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            raise LaunchdError("keeper runtime config needs public Q and guard addresses")
        public_env[key] = address
    return args, public_env


def prepare(config: Path, output: Path) -> None:
    read_config(config)
    if LOCAL.is_symlink() or not LOCAL.is_dir() or LOCAL.stat().st_uid != os.getuid():
        raise LaunchdError(".local must be an owned directory")
    os.chmod(LOCAL, 0o700)
    if output.parent.resolve() != LOCAL.resolve() or output.is_symlink():
        raise LaunchdError("launchd plist must be written directly in .local")
    payload = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(HERE / "pons_keeper_launchd.py"),
                             "run", "--config", str(config)],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 15,
        "StandardOutPath": str(LOCAL / "pons-keeper.stdout.log"),
        "StandardErrorPath": str(LOCAL / "pons-keeper.stderr.log"),
    }
    staging = output.with_suffix(output.suffix + f".{os.getpid()}.new")
    try:
        with open(staging, "xb") as file:
            os.chmod(staging, 0o600)
            plistlib.dump(payload, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(staging, output)
    finally:
        staging.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_PLIST)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        prepare(args.config, args.output)
        print(f"prepared {args.output}; not installed or started")
        return 0
    configured_args, public_env = read_config(args.config)
    os.environ.update(public_env)
    command = [sys.executable, str(HERE / "pons_keeper_supervisor.py")]
    command.extend(configured_args)
    os.execv(sys.executable, command)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LaunchdError as exc:
        raise SystemExit(f"keeper launchd starter stopped: {exc}") from exc
