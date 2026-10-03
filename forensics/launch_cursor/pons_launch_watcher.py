#!/usr/bin/env python3
"""Owner-only Pons TokenLaunched watcher for the hookless Q prototype.

Websocket logs are wake-ups, not the source of truth. Every wake-up and every
poll scans confirmed blocks with HTTP eth_getLogs from a durable .local cursor.
The only transaction this program can sign is Q.enqueue(address).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Protocol
from urllib import request


ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
DEFAULT_STATE = LOCAL / "pons-launch-watcher.json"
PONS_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
TOKEN_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
ENQUEUE = "0x8f807f6b"  # enqueue(address)
OWNER = "0x8da5cb5b"  # owner()
PONS_FACTORY_GETTER = "0x1e344ad1"  # ponsFactory()
LAUNCHES = "0x1f2d8550"  # launches(address), first word is Stage
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
ZERO_ADDRESS = "0x" + "00" * 20


class WatcherError(Exception):
    """A fail-closed condition requiring operator attention."""


class EnqueueReverted(WatcherError):
    """A mined, confirmed enqueue transaction reverted and can be retried."""


class Rpc(Protocol):
    def call(self, method: str, params: list[Any]) -> Any: ...


def address(value: str) -> str:
    if not ADDRESS.fullmatch(value):
        raise WatcherError("expected a 20-byte hex address")
    return value.lower()


def signing_address(value: str) -> str:
    """Return a validated EIP-55 destination accepted by eth_account."""
    from eth_utils import to_checksum_address
    return to_checksum_address(address(value))


def quantity(value: Any, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise WatcherError(f"invalid {field} quantity")
    try:
        number = int(value, 16)
    except ValueError as exc:
        raise WatcherError(f"invalid {field} quantity") from exc
    if number < 0:
        raise WatcherError(f"negative {field} quantity")
    return number


def result_address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise WatcherError(f"invalid {field} result")
    word = value[2:].lower()
    if word[:24] != "0" * 24:
        raise WatcherError(f"invalid {field} address padding")
    return "0x" + word[24:]


def token_from_log(log: dict[str, Any], factory: str) -> str:
    if not isinstance(log, dict) or str(log.get("address", "")).lower() != factory:
        raise WatcherError("unexpected log address")
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 4 or str(topics[0]).lower() != TOKEN_LAUNCHED:
        raise WatcherError("unexpected TokenLaunched topics")
    if log.get("removed") is True:
        raise WatcherError("removed log in confirmed eth_getLogs range")
    for topic in topics:
        if not isinstance(topic, str) or not HASH.fullmatch(topic):
            raise WatcherError("malformed TokenLaunched topic")
    return result_address(topics[1], "launched token")


def pair_token_from_log(log: dict[str, Any]) -> str:
    """Decode the first non-indexed TokenLaunched field, failing closed on bad ABI data."""
    data = log.get("data")
    if not isinstance(data, str) or not data.startswith("0x") or len(data) != 2 + 3 * 64:
        raise WatcherError("malformed TokenLaunched event data")
    try:
        bytes.fromhex(data[2:])
    except ValueError as exc:
        raise WatcherError("malformed TokenLaunched event data") from exc
    return result_address("0x" + data[2:66], "Pons pair token")


def block_hash(rpc: Rpc, block_number: int) -> str:
    block = rpc.call("eth_getBlockByNumber", [hex(block_number), False])
    if not isinstance(block, dict) or not isinstance(block.get("hash"), str) or not HASH.fullmatch(block["hash"]):
        raise WatcherError(f"missing block hash at {block_number}")
    return block["hash"].lower()


def chain_head(rpc: Rpc) -> int:
    return quantity(rpc.call("eth_blockNumber", []), "head")


def stage(rpc: Rpc, q: str, token: str, block: int | str = "latest") -> int:
    raw = rpc.call("eth_call", [{"to": q, "data": LAUNCHES + token[2:].rjust(64, "0")},
                                hex(block) if isinstance(block, int) else block])
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 66:
        raise WatcherError("Q.launches returned malformed data")
    try:
        value = int(raw[2:66], 16)
    except ValueError as exc:
        raise WatcherError("Q.launches returned malformed data") from exc
    if value > 5:
        raise WatcherError("Q.launches returned an unknown stage")
    return value


def verify_contract(rpc: Rpc, chain_id: int, q: str, owner: str | None) -> None:
    if quantity(rpc.call("eth_chainId", []), "chain ID") != chain_id:
        raise WatcherError("RPC chain ID differs from configured chain ID")
    code = rpc.call("eth_getCode", [q, "latest"])
    if not isinstance(code, str) or code == "0x":
        raise WatcherError("Q has no contract code")
    factory = result_address(rpc.call("eth_call", [{"to": q, "data": PONS_FACTORY_GETTER}, "latest"]), "Q.ponsFactory")
    if factory != PONS_FACTORY:
        raise WatcherError("Q.ponsFactory does not match the Pons V2 factory")
    if owner is not None:
        actual_owner = result_address(rpc.call("eth_call", [{"to": q, "data": OWNER}, "latest"]), "Q.owner")
        if actual_owner != owner:
            raise WatcherError("signing account is not Q.owner")


class HttpRpc:
    def __init__(self, url: str, timeout: float = 20):
        if not url.startswith(("https://", "http://")):
            raise WatcherError("HTTP RPC URL must use http or https")
        self.url, self.timeout, self.request_id = url, timeout, 0

    def call(self, method: str, params: list[Any]) -> Any:
        if method == "eth_sendRawTransaction" and os.environ.get("PONS_BUDGET_REQUIRED") == "1":
            from pons_tx_budget import budgeted_send_raw
            return budgeted_send_raw(self, params, lambda: self._call_http(method, params))
        return self._call_http(method, params)

    def _call_http(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}).encode()
        # Robinhood's public RPC rejects Python-urllib's default User-Agent
        # with HTTP 403. Use an explicit client identifier for all keepers,
        # which share this transport.
        req = request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "the-book-pons-cranker/1.0"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                result = json.load(response)
        except Exception as exc:
            raise WatcherError(f"HTTP RPC transport failed during {method}") from exc
        if not isinstance(result, dict) or result.get("id") != self.request_id:
            raise WatcherError(f"invalid HTTP RPC response for {method}")
        if "error" in result:
            code = result["error"].get("code") if isinstance(result["error"], dict) else "unknown"
            raise WatcherError(f"HTTP RPC {method} failed with code {code}")
        if "result" not in result:
            raise WatcherError(f"missing HTTP RPC result for {method}")
        return result["result"]


@dataclass
class Cursor:
    chain_id: int
    q: str
    last_block: int
    last_hash: str
    pending: dict[str, Any] | None = None
    last_log_index: int | None = None

    def json(self) -> dict[str, Any]:
        return {"version": 1, "chainId": self.chain_id, "factory": PONS_FACTORY,
                "q": self.q, "lastBlock": self.last_block, "lastHash": self.last_hash,
                "lastLogIndex": self.last_log_index, "pending": self.pending}


class CursorStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = None
        resolved_local = LOCAL.resolve()
        if path.resolve().parent != resolved_local:
            raise WatcherError("cursor path must be directly inside repository .local")

    def __enter__(self) -> "CursorStore":
        LOCAL.mkdir(mode=0o700, exist_ok=True)
        if LOCAL.is_symlink():
            raise WatcherError(".local must not be a symlink")
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = open(lock_path, "a+")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise WatcherError("another launch watcher holds the cursor lock") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()

    def load(self, rpc: Rpc, chain_id: int, q: str, start_block: int | None) -> Cursor:
        if not self.path.exists():
            if start_block is None or start_block < 1:
                raise WatcherError("first run requires --start-block >= 1")
            cursor = Cursor(chain_id, q, start_block - 1, block_hash(rpc, start_block - 1))
            self.save(cursor)
            return cursor
        if self.path.is_symlink():
            raise WatcherError("cursor must not be a symlink")
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise WatcherError("cursor file is unreadable or corrupt") from exc
        if (not isinstance(data, dict) or data.get("version") != 1 or data.get("chainId") != chain_id
                or data.get("factory") != PONS_FACTORY or data.get("q") != q):
            raise WatcherError("cursor is bound to another Q, chain, or factory")
        last_block, last_hash, pending = data.get("lastBlock"), data.get("lastHash"), data.get("pending")
        last_log_index = data.get("lastLogIndex")
        if (not isinstance(last_block, int) or last_block < 0 or not isinstance(last_hash, str)
                or not HASH.fullmatch(last_hash) or (pending is not None and not isinstance(pending, dict))
                or (last_log_index is not None and
                    (type(last_log_index) is not int or last_log_index < 0 or last_block == 0))):
            raise WatcherError("cursor fields are malformed")
        next_block = last_block if last_log_index is not None else last_block + 1
        if start_block is not None and start_block != next_block:
            raise WatcherError("--start-block conflicts with saved cursor")
        return Cursor(chain_id, q, last_block, last_hash.lower(), pending, last_log_index)

    def save(self, cursor: Cursor) -> None:
        # Same-directory replace, file fsync, then directory fsync. Never /tmp.
        staging = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.new")
        try:
            with open(staging, "x", encoding="utf-8") as file:
                os.chmod(staging, 0o600)
                json.dump(cursor.json(), file, separators=(",", ":"))
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(staging, self.path)
            directory = os.open(LOCAL, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if staging.exists():
                staging.unlink()


def log_order(log: dict[str, Any]) -> tuple[int, int, int]:
    return (quantity(log.get("blockNumber"), "log block"),
            quantity(log.get("transactionIndex"), "transaction index"),
            quantity(log.get("logIndex"), "log index"))


class Enqueuer(Protocol):
    def enqueue(self, token: str, source_block: int, cursor: Cursor, store: CursorStore) -> bool: ...


def backfill_once(rpc: Rpc, sender: Enqueuer, cursor: Cursor, store: CursorStore,
                  confirmations: int, block_span: int,
                  max_enqueues: int | None = None) -> int:
    if max_enqueues is not None and max_enqueues < 1:
        raise WatcherError("max enqueues per cycle must be positive")
    if block_hash(rpc, cursor.last_block) != cursor.last_hash:
        raise WatcherError("confirmed cursor block hash changed; inspect reorg before resuming")
    safe_head = chain_head(rpc) - confirmations
    processed = 0
    enqueued = 0
    while cursor.last_block < safe_head or (cursor.last_log_index is not None and
                                            cursor.last_block <= safe_head):
        first = cursor.last_block if cursor.last_log_index is not None else cursor.last_block + 1
        last = min(safe_head, first + block_span - 1)
        expected_hash = block_hash(rpc, last)
        logs = rpc.call("eth_getLogs", [{"address": PONS_FACTORY, "topics": [TOKEN_LAUNCHED],
                                         "fromBlock": hex(first), "toBlock": hex(last)}])
        if not isinstance(logs, list):
            raise WatcherError("eth_getLogs returned malformed data")
        if block_hash(rpc, last) != expected_hash:
            raise WatcherError("confirmed range changed during eth_getLogs; cursor unchanged")
        for log in sorted(logs, key=log_order):
            number = quantity(log.get("blockNumber"), "log block")
            if not first <= number <= last:
                raise WatcherError("eth_getLogs returned an out-of-range log")
            if str(log.get("blockHash", "")).lower() != block_hash(rpc, number):
                raise WatcherError("eth_getLogs returned a noncanonical log")
            log_index = quantity(log.get("logIndex"), "log index")
            if (cursor.last_log_index is not None and number == cursor.last_block and
                    log_index <= cursor.last_log_index):
                continue
            token = token_from_log(log, PONS_FACTORY)
            pair_token = pair_token_from_log(log)
            if pair_token == ZERO_ADDRESS:
                if sender.enqueue(token, number, cursor, store):
                    enqueued += 1
            else:
                print(json.dumps({"block": number, "token": token,
                                  "pairToken": pair_token,
                                  "action": "skipped_unsupported_pair"}), flush=True)
            processed += 1
            if max_enqueues is not None and enqueued >= max_enqueues:
                if block_hash(rpc, last) != expected_hash:
                    raise WatcherError("confirmed range changed during enqueue; cursor unchanged")
                cursor.last_block = number
                cursor.last_hash = block_hash(rpc, number)
                cursor.last_log_index = log_index
                store.save(cursor)
                return processed
        if block_hash(rpc, last) != expected_hash:
            raise WatcherError("confirmed range changed during enqueue; cursor unchanged")
        cursor.last_hash = expected_hash
        cursor.last_block = last
        cursor.last_log_index = None
        store.save(cursor)
    return processed


def receipt_confirmed(rpc: Rpc, tx_hash: str, confirmations: int) -> bool:
    receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        return False
    if not isinstance(receipt, dict):
        raise WatcherError("pending enqueue receipt is malformed")
    number = quantity(receipt.get("blockNumber"), "receipt block")
    if str(receipt.get("blockHash", "")).lower() != block_hash(rpc, number):
        raise WatcherError("pending enqueue receipt is not in the canonical chain")
    if chain_head(rpc) < number + confirmations:
        return False
    if quantity(receipt.get("status"), "receipt status") == 0:
        raise EnqueueReverted("confirmed enqueue transaction reverted")
    if quantity(receipt.get("status"), "receipt status") != 1:
        raise WatcherError("pending enqueue receipt has unknown status")
    return True


def validate_pending(pending: dict[str, Any], cursor: Cursor, owner: str,
                     max_gas: int, max_fee_wei: int, max_priority_wei: int) -> tuple[str, str, int]:
    """Reject any persisted signed transaction other than bounded Q.enqueue(token)."""
    from eth_account import Account
    from eth_utils import keccak
    import rlp

    token, tx_hash, raw_hex, nonce = (pending.get("token"), pending.get("txHash"),
                                      pending.get("rawTx"), pending.get("nonce"))
    if (not isinstance(token, str) or not ADDRESS.fullmatch(token) or
            not isinstance(tx_hash, str) or not HASH.fullmatch(tx_hash) or
            not isinstance(raw_hex, str) or not raw_hex.startswith("0x") or
            len(raw_hex) > 4098 or len(raw_hex) % 2 != 0 or
            not isinstance(nonce, int) or nonce < 0):
        raise WatcherError("pending signed transaction record is malformed")
    try:
        raw = bytes.fromhex(raw_hex[2:])
        fields = rlp.decode(raw[1:])
        sender = Account.recover_transaction(raw_hex).lower()
    except Exception as exc:
        raise WatcherError("pending signed transaction cannot be decoded") from exc
    if raw[:1] != b"\x02" or not isinstance(fields, list) or len(fields) != 12:
        raise WatcherError("pending transaction is not signed EIP-1559")
    number = lambda item: int.from_bytes(item, "big")
    if ("0x" + keccak(raw).hex()) != tx_hash.lower():
        raise WatcherError("pending transaction hash mismatch")
    expected_data = bytes.fromhex((ENQUEUE + token[2:].rjust(64, "0"))[2:])
    if (number(fields[0]) != cursor.chain_id or number(fields[1]) != nonce or
            number(fields[2]) > max_priority_wei or number(fields[3]) > max_fee_wei or
            not 21000 <= number(fields[4]) <= max_gas or
            fields[5] != bytes.fromhex(cursor.q[2:]) or number(fields[6]) != 0 or
            fields[7] != expected_data or fields[8] != [] or sender != owner):
        raise WatcherError("pending transaction is not the bounded owner Q.enqueue call")
    return token.lower(), tx_hash.lower(), nonce


def recover_pending(rpc: Rpc, cursor: Cursor, store: CursorStore, confirmations: int,
                    owner: str, max_gas: int, max_fee_wei: int, max_priority_wei: int,
                    receipt_timeout: int, poll_seconds: float) -> None:
    if cursor.pending is None:
        return
    pending = cursor.pending
    token, tx_hash, nonce = validate_pending(pending, cursor, owner, max_gas, max_fee_wei, max_priority_wei)
    try:
        confirmed = receipt_confirmed(rpc, tx_hash, confirmations)
    except EnqueueReverted:
        cursor.pending = None
        store.save(cursor)
        return  # The unchanged block cursor causes the launch to be retried.
    if not confirmed:
        receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
        known = rpc.call("eth_getTransactionByHash", [tx_hash]) if receipt is None else receipt
        if known is None:
            confirmed_nonce = quantity(rpc.call("eth_getTransactionCount", [owner, "latest"]), "confirmed nonce")
            if confirmed_nonce > nonce:
                raise WatcherError("pending nonce was consumed without this receipt; inspect owner account")
            try:
                sent = rpc.call("eth_sendRawTransaction", [pending["rawTx"]])
            except WatcherError:
                # A provider can accept the exact raw transaction and then
                # return an error (for example, already known). Check its hash.
                if (rpc.call("eth_getTransactionByHash", [tx_hash]) is None and
                        rpc.call("eth_getTransactionReceipt", [tx_hash]) is None):
                    raise
                sent = tx_hash
            if not isinstance(sent, str) or sent.lower() != tx_hash:
                raise WatcherError("rebroadcast returned a different transaction hash")
        deadline = time.monotonic() + receipt_timeout
        while time.monotonic() < deadline:
            try:
                if receipt_confirmed(rpc, tx_hash, confirmations):
                    confirmed = True
                    break
            except EnqueueReverted:
                cursor.pending = None
                store.save(cursor)
                return
            time.sleep(poll_seconds)
        if not confirmed:
            raise WatcherError("pending enqueue remains unconfirmed; signed transaction retained in .local cursor")
    if stage(rpc, cursor.q, token, max(0, chain_head(rpc) - confirmations)) == 0:
        raise WatcherError("confirmed enqueue receipt did not advance Q launch stage")
    cursor.pending = None
    store.save(cursor)


class LiveEnqueuer:
    def __init__(self, rpc: Rpc, q: str, chain_id: int, private_key: str,
                 confirmations: int, max_gas: int, max_fee_wei: int, max_priority_wei: int,
                 receipt_timeout: int, poll_seconds: float, enqueue_attempts: int):
        from eth_account import Account
        self.account = Account.from_key(private_key)
        self.rpc, self.q, self.chain_id = rpc, q, chain_id
        self.confirmations, self.max_gas = confirmations, max_gas
        self.max_fee_wei, self.max_priority_wei = max_fee_wei, max_priority_wei
        self.receipt_timeout, self.poll_seconds = receipt_timeout, poll_seconds
        self.enqueue_attempts = enqueue_attempts

    @property
    def owner(self) -> str:
        return self.account.address.lower()

    def enqueue(self, token: str, source_block: int, cursor: Cursor, store: CursorStore) -> bool:
        if stage(self.rpc, self.q, token, max(0, chain_head(self.rpc) - self.confirmations)) != 0:
            return False
        for attempt in range(self.enqueue_attempts):
            if stage(self.rpc, self.q, token, max(0, chain_head(self.rpc) - self.confirmations)) != 0:
                return False
            try:
                self._submit_once(token, source_block, cursor, store)
                return True
            except EnqueueReverted:
                cursor.pending = None
                store.save(cursor)
                if attempt + 1 == self.enqueue_attempts:
                    raise WatcherError("enqueue reverted after configured retries; block cursor unchanged")
                time.sleep(self.poll_seconds)

    def _submit_once(self, token: str, source_block: int, cursor: Cursor, store: CursorStore) -> None:
        data = ENQUEUE + token[2:].rjust(64, "0")
        nonce = quantity(self.rpc.call("eth_getTransactionCount", [self.owner, "pending"]), "nonce")
        estimate = quantity(self.rpc.call("eth_estimateGas", [{"from": self.owner, "to": self.q, "data": data}]), "gas estimate")
        gas = (estimate * 120 + 99) // 100
        if gas > self.max_gas:
            raise WatcherError("enqueue gas estimate exceeds configured cap")
        latest = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(latest, dict) or "baseFeePerGas" not in latest:
            raise WatcherError("RPC did not return an EIP-1559 base fee")
        base_fee = quantity(latest["baseFeePerGas"], "base fee")
        priority = min(quantity(self.rpc.call("eth_maxPriorityFeePerGas", []), "priority fee"), self.max_priority_wei)
        if 2 * base_fee + priority > self.max_fee_wei:
            raise WatcherError("required enqueue max fee exceeds configured cap")
        tx = {"chainId": self.chain_id, "nonce": nonce, "to": signing_address(self.q), "value": 0,
              "data": data, "gas": gas, "type": 2, "maxFeePerGas": 2 * base_fee + priority,
              "maxPriorityFeePerGas": priority}
        signed = self.account.sign_transaction(tx)
        tx_hash = "0x" + signed.hash.hex().lower().removeprefix("0x")
        # Persist before broadcast. A crash in this small interval fails closed
        # on restart; it cannot silently submit a second transaction.
        cursor.pending = {"token": token, "sourceBlock": source_block, "txHash": tx_hash,
                          "nonce": nonce, "rawTx": "0x" + signed.raw_transaction.hex().removeprefix("0x")}
        store.save(cursor)
        sent = self.rpc.call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        if not isinstance(sent, str) or sent.lower() != tx_hash:
            raise WatcherError("RPC returned an unexpected enqueue transaction hash")
        deadline = time.monotonic() + self.receipt_timeout
        while time.monotonic() < deadline:
            if receipt_confirmed(self.rpc, tx_hash, self.confirmations):
                if stage(self.rpc, self.q, token) == 0:
                    raise WatcherError("confirmed enqueue did not advance Q launch stage")
                cursor.pending = None
                store.save(cursor)
                return
            time.sleep(self.poll_seconds)
        raise WatcherError("enqueue receipt timeout; pending transaction retained in .local cursor")


class DryRunEnqueuer:
    def __init__(self, rpc: Rpc, q: str):
        self.rpc, self.q = rpc, q

    def enqueue(self, token: str, source_block: int, cursor: Cursor, store: CursorStore) -> bool:
        would_enqueue = stage(self.rpc, self.q, token) == 0
        print(json.dumps({"block": source_block, "token": token,
                          "action": "would_enqueue" if would_enqueue else "already_enqueued"}), flush=True)
        return would_enqueue


def gwei(value: str) -> int:
    try:
        number = Decimal(value) * Decimal(10**9)
    except InvalidOperation as exc:
        raise WatcherError("invalid gwei cap") from exc
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise WatcherError("gwei cap must be positive and have at most 9 decimal places")
    return int(number)


def watch_ws(url: str, rpc: Rpc, sender: Enqueuer, cursor: Cursor, store: CursorStore,
             confirmations: int, block_span: int, poll_seconds: float,
             max_enqueues: int | None = None) -> None:
    if not url.startswith(("wss://", "ws://")):
        raise WatcherError("websocket URL must use ws or wss")
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidHandshake

    while True:
        try:
            with connect(url, open_timeout=15, ping_interval=20, ping_timeout=20) as ws:
                ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                    "params": ["logs", {"address": PONS_FACTORY, "topics": [TOKEN_LAUNCHED]}]}))
                response = json.loads(ws.recv(timeout=15))
                if not isinstance(response, dict) or not isinstance(response.get("result"), str):
                    raise WatcherError("websocket eth_subscribe failed")
                print(json.dumps({"status": "subscribed", "lastBlock": cursor.last_block}), flush=True)
                backfill_once(rpc, sender, cursor, store, confirmations, block_span, max_enqueues)
                while True:
                    try:
                        message = json.loads(ws.recv(timeout=poll_seconds))
                    except TimeoutError:
                        message = None
                    if isinstance(message, dict) and message.get("method") == "eth_subscription":
                        result = message.get("params", {}).get("result", {})
                        if isinstance(result, dict) and result.get("removed") is True:
                            continue  # HTTP confirmed-log backfill remains authoritative.
                    backfill_once(rpc, sender, cursor, store, confirmations, block_span, max_enqueues)
        except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError, json.JSONDecodeError):
            print(json.dumps({"status": "websocket_disconnected", "lastBlock": cursor.last_block}), flush=True)
            # HTTP backfill continues while WS is down. The next connection
            # again scans from the saved cursor, so missed notifications are fine.
            backfill_once(rpc, sender, cursor, store, confirmations, block_span, max_enqueues)
            time.sleep(min(poll_seconds, 10))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default=os.environ.get("PONS_HTTP_RPC_URL"))
    parser.add_argument("--ws-url", default=os.environ.get("PONS_WS_RPC_URL"))
    parser.add_argument("--chain-id", type=int, default=os.environ.get("PONS_CHAIN_ID"))
    parser.add_argument("--q", default=os.environ.get("PONS_Q_ADDRESS"))
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--confirmations", type=int, default=3)
    parser.add_argument("--block-span", type=int, default=2000)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--receipt-timeout", type=int, default=180)
    parser.add_argument("--enqueue-attempts", type=int, default=1)
    parser.add_argument("--max-enqueues-per-cycle", type=int,
                        help="bound new enqueues per backfill; live default is one")
    parser.add_argument("--max-gas", type=int, default=500000)
    parser.add_argument("--max-fee-gwei", default="5")
    parser.add_argument("--max-priority-gwei", default="1")
    parser.add_argument("--live", action="store_true", help="enable owner-signed Q.enqueue transactions")
    parser.add_argument("--once", action="store_true", help="scan confirmed blocks once, without websocket")
    args = parser.parse_args(argv)
    if not args.http_url or not args.q or not args.chain_id:
        parser.error("--http-url, --chain-id, and --q are required (or use PONS_* environment variables)")
    if not args.once and not args.ws_url:
        parser.error("--ws-url is required unless --once is used")
    if (args.confirmations < 1 or args.block_span < 1 or args.poll_seconds <= 0 or
            args.receipt_timeout < 1 or args.max_gas < 21000 or args.enqueue_attempts < 1 or
            args.max_enqueues_per_cycle is not None and args.max_enqueues_per_cycle < 1):
        parser.error("invalid confirmation, block span, poll, receipt, or gas setting")
    if args.live and args.enqueue_attempts != 1:
        parser.error("live bounded cycles require --enqueue-attempts 1")
    max_enqueues = (args.max_enqueues_per_cycle if args.max_enqueues_per_cycle is not None
                    else (1 if args.live else None))
    q = address(args.q)
    rpc = HttpRpc(args.http_url)
    max_fee_wei, max_priority_wei = gwei(args.max_fee_gwei), gwei(args.max_priority_gwei)
    if max_priority_wei > max_fee_wei:
        raise WatcherError("priority fee cap exceeds max fee cap")
    if args.live:
        key = os.environ.get("PONS_OWNER_PRIVATE_KEY")
        if not key:
            raise WatcherError("--live requires PONS_OWNER_PRIVATE_KEY in the process environment")
        sender: Enqueuer = LiveEnqueuer(rpc, q, args.chain_id, key, args.confirmations,
                                       args.max_gas, max_fee_wei, max_priority_wei,
                                       args.receipt_timeout, args.poll_seconds, args.enqueue_attempts)
        owner = sender.owner
    else:
        sender = DryRunEnqueuer(rpc, q)
        owner = None
    verify_contract(rpc, args.chain_id, q, owner)
    with CursorStore(args.state) as store:
        cursor = store.load(rpc, args.chain_id, q, args.start_block)
        if cursor.pending is not None and args.live:
            recover_pending(rpc, cursor, store, args.confirmations, owner, args.max_gas, max_fee_wei,
                            max_priority_wei, args.receipt_timeout, args.poll_seconds)
        elif cursor.pending is not None:
            raise WatcherError("dry run cannot inspect an unresolved signed transaction; use --live recovery")
        if args.live:
            if args.once:
                backfill_once(rpc, sender, cursor, store, args.confirmations,
                              args.block_span, max_enqueues)
            else:
                watch_ws(args.ws_url, rpc, sender, cursor, store, args.confirmations,
                         args.block_span, args.poll_seconds, max_enqueues)
        else:
            # Dry runs use a temporary in-memory cursor. They must never move
            # the durable live cursor past tokens that still need enqueueing.
            class NoSave:
                def save(self, _cursor: Cursor) -> None: pass
            probe = Cursor(cursor.chain_id, cursor.q, cursor.last_block, cursor.last_hash,
                           last_log_index=cursor.last_log_index)
            if args.once:
                backfill_once(rpc, sender, probe, NoSave(), args.confirmations,
                              args.block_span, max_enqueues)
            else:
                watch_ws(args.ws_url, rpc, sender, probe, NoSave(), args.confirmations,
                         args.block_span, args.poll_seconds, max_enqueues)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except WatcherError as error:
        print(f"launch watcher stopped: {error}", file=sys.stderr)
        sys.exit(1)
