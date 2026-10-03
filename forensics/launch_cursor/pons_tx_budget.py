"""Fail-closed per-signer ETH spend reservations for supervised keeper writes.

Every supervised keeper uses the shared HttpRpc transport. Before it forwards a
signed transaction, this module reserves its *maximum* ETH cost (gas limit x
max fee plus value) in a private, durable journal. When a cap would block a new
write, canonical receipts with twelve confirmations can replace reservations
with actual gas cost. Unknown and unconfirmed transactions keep their full
reservation. The live balance floor is checked before every new write.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Iterator

from eth_account import Account
from eth_utils import keccak
import rlp


class BudgetError(Exception):
    pass


RECONCILE_CONFIRMATIONS = 12
HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _quantity(value: Any, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise BudgetError(f"keeper {field} is malformed")
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise BudgetError(f"keeper {field} is malformed") from exc
    if result < 0:
        raise BudgetError(f"keeper {field} is malformed")
    return result


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


def _charge(item: dict[str, Any]) -> int:
    maximum = item.get("maxCostWei")
    if (type(maximum) is not int or maximum < 0 or
            not isinstance(item.get("day"), str) or
            type(item.get("nonce")) is not int or item["nonce"] < 0):
        raise BudgetError("keeper budget journal is malformed")
    if "actualCostWei" not in item:
        return maximum
    actual = item["actualCostWei"]
    if (type(actual) is not int or not 0 <= actual <= maximum or
            type(item.get("settlementBlockNumber")) is not int or
            item["settlementBlockNumber"] < 0 or
            not isinstance(item.get("settlementBlockHash"), str) or
            not HASH.fullmatch(item["settlementBlockHash"])):
        raise BudgetError("keeper budget journal is malformed")
    return actual


def _settled_cost(rpc: Any, tx_hash: str, sender: str, nonce: int,
                  max_cost: int, head: int) -> tuple[int, int, str] | None:
    receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        return None
    if (not isinstance(receipt, dict) or
            str(receipt.get("transactionHash", "")).lower() != tx_hash or
            str(receipt.get("from", "")).lower() != sender or
            not isinstance(receipt.get("blockHash"), str) or
            not HASH.fullmatch(receipt["blockHash"])):
        raise BudgetError("keeper receipt is malformed")
    block_number = _quantity(receipt.get("blockNumber"), "receipt block number")
    if head < block_number + RECONCILE_CONFIRMATIONS:
        return None
    status = _quantity(receipt.get("status"), "receipt status")
    if status not in (0, 1):
        raise BudgetError("keeper receipt status is malformed")
    block = rpc.call("eth_getBlockByNumber", [hex(block_number), False])
    block_hash = receipt["blockHash"].lower()
    if (not isinstance(block, dict) or
            str(block.get("hash", "")).lower() != block_hash):
        raise BudgetError("keeper receipt is not in the canonical chain")
    tx = rpc.call("eth_getTransactionByHash", [tx_hash])
    if (not isinstance(tx, dict) or str(tx.get("hash", "")).lower() != tx_hash or
            str(tx.get("from", "")).lower() != sender or
            str(tx.get("blockHash", "")).lower() != block_hash or
            _quantity(tx.get("nonce"), "transaction nonce") != nonce or
            _quantity(tx.get("type"), "transaction type") != 2):
        raise BudgetError("keeper mined transaction is malformed")
    gas = _quantity(tx.get("gas"), "transaction gas")
    max_fee = _quantity(tx.get("maxFeePerGas"), "transaction max fee")
    value = _quantity(tx.get("value"), "transaction value")
    gas_used = _quantity(receipt.get("gasUsed"), "receipt gas used")
    effective_fee = _quantity(receipt.get("effectiveGasPrice"), "receipt effective fee")
    if (gas * max_fee + value != max_cost or gas_used > gas or
            effective_fee > max_fee):
        raise BudgetError("keeper receipt cost exceeds signed reservation")
    actual = gas_used * effective_fee + value
    return actual, block_number, block_hash


def _totals(txs: dict[str, Any], today: str) -> tuple[int, int]:
    total = daily = 0
    for tx_hash, item in txs.items():
        if not isinstance(tx_hash, str) or not HASH.fullmatch(tx_hash) or not isinstance(item, dict):
            raise BudgetError("keeper budget journal is malformed")
        charged = _charge(item)
        total += charged
        if item["day"] == today:
            daily += charged
    return total, daily


def never_reserved_after_prior_nonce(sender: str, tx_hash: str, nonce: int) -> bool:
    """Prove a saved signer transaction never passed the supervised send guard.

    The preceding nonce must be in the retained journal and no reservation may
    exist at or after this nonce. Missing or reset journals cannot prove this.
    """
    if (os.environ.get("PONS_BUDGET_REQUIRED") != "1" or
            not isinstance(sender, str) or not isinstance(tx_hash, str) or
            not HASH.fullmatch(tx_hash) or type(nonce) is not int or nonce < 1):
        return False
    path_text = os.environ.get("PONS_BUDGET_JOURNAL")
    if not path_text:
        return False
    path = Path(path_text)
    if not path.is_absolute() or path.name != "pons-keeper-budget.json" or not _private_regular(path):
        return False
    with _locked_journal(path) as journal:
        record = journal["signers"].get(sender.lower())
        if (not isinstance(record, dict) or
                type(record.get("initialBalanceWei")) is not int or
                not isinstance(record.get("txs"), dict)):
            return False
        txs = record["txs"]
        _totals(txs, datetime.now(timezone.utc).date().isoformat())
        nonces = [item["nonce"] for item in txs.values()]
        return (all(saved_hash.lower() != tx_hash.lower() for saved_hash in txs) and
                nonce - 1 in nonces and
                all(saved_nonce < nonce for saved_nonce in nonces))


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
        total, daily = _totals(txs, today)
        def fits() -> bool:
            return (daily + max_cost <= max_daily and
                    total + max_cost <= max_total and
                    record["initialBalanceWei"] - total - max_cost >= floor)
        if not fits():
            head = _quantity(rpc.call("eth_blockNumber", []), "chain head")
            changed = False
            candidates = sorted(txs.items(), key=lambda entry:
                                (entry[1]["day"] != today, -entry[1]["maxCostWei"]))
            for old_hash, item in candidates:
                if "actualCostWei" in item:
                    continue
                settled = _settled_cost(rpc, old_hash, sender, item["nonce"],
                                        item["maxCostWei"], head)
                if settled is None:
                    continue
                actual, block_number, block_hash = settled
                item.update(actualCostWei=actual,
                            settlementBlockNumber=block_number,
                            settlementBlockHash=block_hash)
                changed = True
                total, daily = _totals(txs, today)
                if fits():
                    break
            if changed:
                _save_journal(path, journal)
        if not fits():
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
