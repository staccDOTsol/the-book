#!/usr/bin/env python3
"""Select a read-only or live Fly keeper without logging secret values."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LOCAL = ROOT / ".local"
KEY_DIR = Path("/dev/shm/pons-keys")
KEYS = {
    "PONS_OWNER_PRIVATE_KEY": "owner.hex",
    "PONS_PRICE_CONFIGURATOR_PRIVATE_KEY": "price.hex",
    "PONS_EXIT_CONFIGURATOR_PRIVATE_KEY": "exit.hex",
}
LIVE_JOURNALS = (
    "pons-launch-watcher.json",
    "pons-price-keeper.json",
    "pons-exit-keeper.json",
    "pons-fee-feedback.json",
    "pons-keeper-budget.json",
)


def fail(reason: str) -> None:
    raise SystemExit(f"Fly keeper startup stopped: {reason}")


def check_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        fail("runtime directory is missing, linked, or has the wrong owner")
    os.chmod(path, 0o700)


def require_live_journals() -> None:
    for name in LIVE_JOURNALS:
        path = LOCAL / name
        try:
            info = path.lstat()
        except OSError:
            fail("live journal handoff is incomplete")
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077):
            fail("live journal handoff is incomplete or unsafe")


def write_key_files() -> None:
    try:
        KEY_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        fail("private signer directory could not be created")
    check_directory(KEY_DIR)
    for env_name, name in KEYS.items():
        key = os.environ.pop(env_name, "")
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", key):
            fail("a live signer secret is missing or malformed")
        path = KEY_DIR / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(key + "\n")
        except OSError:
            fail("a live signer key file could not be created")


def main() -> None:
    mode = os.environ.get("PONS_KEEPER_MODE", "standby")
    if mode not in ("standby", "live", "trader-paid"):
        fail("mode must be standby, live, or trader-paid")
    check_directory(LOCAL)
    if mode != "standby":
        require_live_journals()
        write_key_files()
    else:
        for env_name in KEYS:
            os.environ.pop(env_name, None)
    runtime = HERE / f"runtime-{mode}.json"
    starter = HERE.parent / "pons_keeper_launchd.py"
    os.execv(sys.executable, [sys.executable, str(starter), "run", "--config", str(runtime)])


if __name__ == "__main__":
    main()
