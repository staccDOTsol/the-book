"""Fail-closed per-signer ETH spend reservations for supervised keeper writes.

Every supervised keeper uses the shared HttpRpc transport. Before it forwards a
signed transaction, this module reserves its *maximum* ETH cost (gas limit x
max fee plus value) in a private, durable journal. Reservations are intentionally
not refunded after mining: a failed or underpriced transaction still consumes
its budget until an operator explicitly resets the journal. This is conservative
and avoids relying on provider-specific pending balance semantics.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterator

from eth_account import Account
from eth_utils import keccak
import rlp


class BudgetError(Exception):
    pass


def _positive_env(name: str, *, zero_ok: bool = False) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.isdecimal():
        raise BudgetError("keeper budget configuration is missing or invalid")
    value = int(raw)
    if value < 0 or (not zero_ok and value == 0):
        raise BudgetError("keeper budget configuration is missing or invalid")
    return value


def _private_regular(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BudgetError("keeper budget journal must be a private regular file")
    return True


@contextmanager
def _locked_journal(path: Path) -> Iterator[dict[str, Any]]:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise BudgetError("keeper budget directory must be private")
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BudgetError("keeper budget lock must be private")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _private_regular(path):
            if path.stat().st_size > 2_000_000:
                raise BudgetError("keeper budget journal exceeds size limit")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise BudgetError("keeper budget journal is malformed") from exc
            if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("signers"), dict):
                raise BudgetError("keeper budget journal is malformed")
        else:
            data = {"schemaVersion": 1, "signers": {}}
        yield data
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _save_journal(path: Path, data: dict[str, Any]) -> None:
    staging = path.with_suffix(path.suffix + f".{os.getpid()}.new")
    try:
        with open(staging, "x", encoding="utf-8") as file:
            os.chmod(staging, 0o600)
            json.dump(data, file, sort_keys=True, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(staging, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staging.unlink(missing_ok=True)


def _int_field(value: Any) -> int:
    if not isinstance(value, bytes):
        raise BudgetError("keeper signed transaction is malformed")
    return int.from_bytes(value, "big")


def _transaction(raw: str) -> tuple[str, str, int, int, int, int]:
    """Return sender, hash, chain, nonce, max gas cost and max fee per gas."""
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) > 300_000:
        raise BudgetError("keeper signed transaction is malformed")
    try:
        encoded = bytes.fromhex(raw[2:])
        if not encoded or encoded[0] != 2:
            raise BudgetError("keeper only permits EIP-1559 transactions")
        fields = rlp.decode(encoded[1:])
        if not isinstance(fields, list) or len(fields) != 12:
            raise BudgetError("keeper signed transaction is malformed")
        sender = Account.recover_transaction(raw).lower()
        chain_id = _int_field(fields[0])
        nonce = _int_field(fields[1])
        max_priority = _int_field(fields[2])
        max_fee = _int_field(fields[3])
        gas = _int_field(fields[4])
        value = _int_field(fields[6])
    except BudgetError:
        raise
    except Exception as exc:
        raise BudgetError("keeper signed transaction is malformed") from exc
    if gas < 21_000 or max_fee < max_priority:
        raise BudgetError("keeper signed transaction has invalid gas or fee")
    return sender, "0x" + keccak(encoded).hex(), chain_id, nonce, gas * max_fee + value, max_fee


def budgeted_send_raw(rpc: Any, params: list[Any], send: Callable[[], Any]) -> Any:
    """Reserve worst-case spend, then submit under the same cross-process lock."""
    if not isinstance(params, list) or len(params) != 1:
        raise BudgetError("keeper signed transaction is malformed")
    sender, tx_hash, chain_id, nonce, max_cost, max_fee = _transaction(params[0])
    expected_sender = os.environ.get("PONS_BUDGET_SIGNER", "").lower()
    if sender != expected_sender or chain_id != 4663:
        raise BudgetError("keeper signer or chain does not match budget configuration")
    max_daily = _positive_env("PONS_BUDGET_DAILY_WEI")
    max_total = _positive_env("PONS_BUDGET_TOTAL_WEI")
    max_tx = _positive_env("PONS_BUDGET_TX_WEI")
    floor = _positive_env("PONS_BUDGET_FLOOR_WEI", zero_ok=True)
    max_fee_allowed = _positive_env("PONS_BUDGET_MAX_FEE_WEI")
    if max_cost > max_tx or max_fee > max_fee_allowed:
        raise BudgetError("keeper transaction exceeds per-transaction budget")
    path_text = os.environ.get("PONS_BUDGET_JOURNAL")
    if not path_text:
        raise BudgetError("keeper budget journal is not configured")
    path = Path(path_text)
    if not path.is_absolute() or path.name != "pons-keeper-budget.json":
        raise BudgetError("keeper budget journal path is invalid")
    today = datetime.now(timezone.utc).date().isoformat()
    with _locked_journal(path) as journal:
        signers = journal["signers"]
        record = signers.get(sender)
        if record is None:
            balance_hex = rpc.call("eth_getBalance", [sender, "pending"])
            if not isinstance(balance_hex, str) or not balance_hex.startswith("0x"):
                raise BudgetError("keeper signer balance is unavailable")
            initial_balance = int(balance_hex, 16)
            record = {"initialBalanceWei": initial_balance, "txs": {}}
            signers[sender] = record
        if (not isinstance(record, dict) or not isinstance(record.get("initialBalanceWei"), int) or
                not isinstance(record.get("txs"), dict)):
            raise BudgetError("keeper budget journal is malformed")
        txs = record["txs"]
        if tx_hash in txs:
            saved = txs[tx_hash]
            if (not isinstance(saved, dict) or saved.get("nonce") != nonce or
                    saved.get("maxCostWei") != max_cost):
                raise BudgetError("keeper budget journal is malformed")
            return send()  # exact raw rebroadcast; never reserve it twice
        total = 0
        daily = 0
        for item in txs.values():
            if (not isinstance(item, dict) or not isinstance(item.get("maxCostWei"), int) or
                    item["maxCostWei"] < 0 or not isinstance(item.get("day"), str) or
                    not isinstance(item.get("nonce"), int)):
                raise BudgetError("keeper budget journal is malformed")
            total += item["maxCostWei"]
            if item["day"] == today:
                daily += item["maxCostWei"]
        if (daily + max_cost > max_daily or total + max_cost > max_total or
                record["initialBalanceWei"] - total - max_cost < floor):
            raise BudgetError("keeper transaction exceeds signer daily, total, or reserve budget")
        # Check live balance too; an external spend must never bypass the floor.
        balance_hex = rpc.call("eth_getBalance", [sender, "pending"])
        if not isinstance(balance_hex, str) or not balance_hex.startswith("0x"):
            raise BudgetError("keeper signer balance is unavailable")
        if int(balance_hex, 16) < floor + max_cost:
            raise BudgetError("keeper signer balance is below reserve requirement")
        txs[tx_hash] = {"day": today, "nonce": nonce, "maxCostWei": max_cost}
        _save_journal(path, journal)
        return send()
