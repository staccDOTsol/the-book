#!/usr/bin/env python3
"""Authenticate and submit gross mark-to-market X/Q fee-arm feedback.

The score is an ETH-equivalent *estimate*, not realized profit. It values the
newly minted Q deposited at open and all Q burned at each tranche exit or
interim harvest with full-size, historical Q->ETH executable quotes from the
completed block before each transaction. It includes actual ETH/WETH recipient payouts. Attributable gas
is recorded when known but excluded because shared/reverted cranks cannot be
proven complete from these contracts. A missing quote defers reporting and
becomes a censored completed exit after Q's seven-day evidence window.

Read-only is the default. --live uses the existing Q price-configurator signer
and lock, archives the exact evidence before signing, and journals signed
transactions before broadcast for safe recovery.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak
import rlp

import pons_launch_watcher as watch
import pons_price_keeper as price
import pons_fee_reporter as report


LOCAL = price.LOCAL
DEFAULT_STATE = LOCAL / "pons-fee-feedback.json"
EVIDENCE_DIR = LOCAL / "pons-fee-evidence"
REPORT_SELECTOR = price.selector("reportExitedOutcome(address,int32,bytes32)")
CENSOR_SELECTOR = price.selector("censorExpiredOutcome(address)")
MAX_SCORE_BPS = 10_000
MAX_QUOTE_Q = (1 << 128) - 1


class FeedbackError(ValueError):
    pass


def signed_bps(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise FeedbackError("entry Q liquidation quote must be positive")
    absolute = (abs(numerator) * 20_000 + denominator) // (2 * denominator)
    return absolute if numerator >= 0 else -absolute


def gross_mark_score(observation: dict[str, Any], entry_eth: int,
                     burn_eth: int) -> dict[str, Any]:
    deposited = int(observation["qSpentWei"])
    burned = int(observation["qBurnedWei"])
    payout = int(observation["recipientCashEthWei"])
    if min(deposited, burned, entry_eth, burn_eth) <= 0 or payout < 0:
        raise FeedbackError("gross mark score requires positive Q amounts and executable quotes")
    surplus = payout + burn_eth - entry_eth
    raw_bps = signed_bps(surplus, entry_eth)
    return {
        "metric": "gross_mark_to_market_eth_equivalent_excluding_gas",
        "entryMintedQDepositedWei": str(deposited),
        "exitQBurnedWei": str(burned),
        "recipientCashEthWei": str(payout),
        "entryQSaleQuoteEthWei": str(entry_eth),
        "exitBurnQSaleQuoteEthWei": str(burn_eth),
        "grossEstimatedSurplusEthWei": str(surplus),
        "grossReturnBpsUnclipped": raw_bps,
        "grossReturnBpsForFeePolicy": max(-MAX_SCORE_BPS, min(MAX_SCORE_BPS, raw_bps)),
        "gasTreatment": "known receipts disclosed in observation; gas excluded from fee score",
        "profitClaim": False,
    }


def quote_q_to_eth(rpc: watch.Rpc, bindings: price.Bindings, q_amount: int,
                   block: int) -> dict[str, Any]:
    if not 0 < q_amount <= MAX_QUOTE_Q or block < 1:
        raise FeedbackError("Q quote amount or historical block is invalid")
    before = watch.block_hash(rpc, block)
    pinned = price.PinnedRpc(rpc, block)
    key = (price.ZERO, bindings.q, bindings.quote_fee, bindings.quote_spacing, price.ZERO)
    try:
        eth = price.ExecutableQuoteProvider(pinned, bindings).quote_single(key, False, q_amount)
    except (price.KeeperError, watch.WatcherError) as exc:
        raise FeedbackError("historical Q/ETH executable quote is unavailable") from exc
    if watch.block_hash(rpc, block) != before:
        raise FeedbackError("Q/ETH quote block was reorged")
    return {"route": "zero-hook Q/ETH exact-input Q sale",
            "sourceBlock": block, "sourceBlockHash": before,
            "poolId": bindings.quote_pool_id,
            "qInputWei": str(q_amount), "ethOutputWei": str(eth)}


def build_evidence(rpc: watch.Rpc, bindings: price.Bindings,
                   observation: dict[str, Any]) -> dict[str, Any]:
    open_block = int(observation["openBlock"])
    exit_block = int(observation["exitBlock"])
    if exit_block < open_block or open_block < 2:
        raise FeedbackError("position chronology cannot be quoted before open")
    entry = quote_q_to_eth(rpc, bindings, int(observation["qSpentWei"]), open_block - 1)
    burn_quotes: list[dict[str, Any]] = []
    settlements = observation.get("settlementBurns", [])
    if not isinstance(settlements, list) or len(settlements) < 3 or \
            sum(item.get("kind") == "position_exit" for item in settlements) != 3:
        raise FeedbackError("evidence must cover three exits and all interim burns")
    if int(settlements[-1]["block"]) != exit_block or \
            settlements[-1].get("kind") != "position_exit":
        raise FeedbackError("final settlement does not match final closure")
    for settlement in settlements:
        burn_quotes.append(quote_q_to_eth(
            rpc, bindings, int(settlement["qBurnedWei"]), int(settlement["block"]) - 1))
    if sum(int(item["qInputWei"]) for item in burn_quotes) != int(observation["qBurnedWei"]):
        raise FeedbackError("lifetime Q burns do not reconcile with per-settlement quotes")
    if watch.block_hash(rpc, exit_block) != observation.get("evidenceBlockHash"):
        raise FeedbackError("authenticated final exit block changed during valuation")
    score = gross_mark_score(observation, int(entry["ethOutputWei"]),
                             sum(int(item["ethOutputWei"]) for item in burn_quotes))
    return {"schemaVersion": 1, "chainId": 4663, "q": bindings.q,
            "token": observation["token"], "observation": observation,
            "entryQuote": entry, "burnQuotes": burn_quotes, "score": score,
            "limitations": [
                "Historical full-size Q sale quotes are hypothetical, not realized proceeds.",
                "Previous-block snapshots omit within-block price changes and own market impact.",
                "The score excludes gas because complete per-position attribution is unavailable.",
                "Fee assignment and closed-position selection do not prove causal profitability.",
            ]}


def canonical_evidence(evidence: dict[str, Any]) -> bytes:
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def archive_evidence(evidence: dict[str, Any]) -> str:
    if EVIDENCE_DIR.is_symlink():
        raise FeedbackError("fee evidence directory must not be a symlink")
    EVIDENCE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(EVIDENCE_DIR, 0o700)
    payload = canonical_evidence(evidence)
    digest = hashlib.sha256(payload).hexdigest()
    token = watch.address(evidence["token"])
    path = EVIDENCE_DIR / f"{token[2:]}-{digest}.json"
    if path.is_symlink():
        raise FeedbackError("fee evidence archive must not be a symlink")
    if path.exists():
        if path.read_bytes() != payload + b"\n":
            raise FeedbackError("archived evidence digest collision or file changed")
        return "0x" + digest
    staging = path.with_suffix(f".{os.getpid()}.new")
    try:
        with open(staging, "xb") as file:
            os.chmod(staging, 0o600)
            file.write(payload + b"\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(staging, path)
        directory = os.open(EVIDENCE_DIR, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staging.unlink(missing_ok=True)
    return "0x" + digest


def report_data(token: str, score_bps: int, evidence_hash: str) -> str:
    if not -MAX_SCORE_BPS <= score_bps <= MAX_SCORE_BPS:
        raise FeedbackError("fee score lies outside policy bounds")
    digest = report._hex(evidence_hash, 32, "evidence hash")
    return REPORT_SELECTOR + encode(["address", "int32", "bytes32"],
                                    [watch.address(token), score_bps,
                                     bytes.fromhex(digest[2:])]).hex()


def censor_data(token: str) -> str:
    return CENSOR_SELECTOR + encode(["address"], [watch.address(token)]).hex()


def pending_status(rpc: watch.Rpc, binding: report.Bindings, token: str,
                   block: int) -> tuple[int, int, int]:
    deadline, = report._call(rpc, binding.q, "outcomeDeadline(address)",
                             ["address"], [token], ["uint64"], block)
    stage = int(report._call(rpc, binding.q, "launches(address)", ["address"], [token],
                             ["uint8", "bool", "uint64", "uint64", "uint64", "uint64",
                              "uint32", "bool", "bool", "uint64", "uint32", "bool"], block)[0])
    _fee, _arm, status = report._call(rpc, binding.fee_policy, "assignments(address)",
                                     ["address"], [token], ["uint24", "uint8", "uint8"], block)
    return int(deadline), stage, int(status)


class FeedbackSigner:
    def __init__(self, rpc: watch.Rpc, q: str, private_key: str,
                 confirmations: int, max_gas: int, max_fee_wei: int,
                 max_priority_wei: int, receipt_timeout: int, poll_seconds: float):
        self.rpc, self.q = rpc, watch.address(q)
        self.account = Account.from_key(private_key)
        self.confirmations, self.max_gas = confirmations, max_gas
        self.max_fee_wei, self.max_priority_wei = max_fee_wei, max_priority_wei
        self.receipt_timeout, self.poll_seconds = receipt_timeout, poll_seconds

    @property
    def address(self) -> str:
        return self.account.address.lower()

    def _confirmed(self, tx_hash: str) -> bool:
        return price.confirmed_receipt(self.rpc, tx_hash, self.confirmations)

    def _wait(self, tx_hash: str) -> None:
        until = time.monotonic() + self.receipt_timeout
        while time.monotonic() < until:
            if self._confirmed(tx_hash):
                return
            time.sleep(self.poll_seconds)
        raise FeedbackError("fee feedback transaction is still pending in durable state")

    def _validate_payload(self, kind: str, token: str, data: str,
                          evidence_hash: str | None) -> None:
        if kind == "report":
            if not data.startswith(REPORT_SELECTOR) or len(data) != 10 + 64 * 3:
                raise FeedbackError("invalid fee report calldata")
            encoded_token, score, digest = decode(["address", "int32", "bytes32"],
                                                   bytes.fromhex(data[10:]))
            if (watch.address(encoded_token) != token or
                    not -MAX_SCORE_BPS <= score <= MAX_SCORE_BPS or
                    "0x" + digest.hex() != evidence_hash):
                raise FeedbackError("fee report calldata differs from evidence")
        elif kind == "censor":
            if data != censor_data(token) or evidence_hash is not None:
                raise FeedbackError("invalid fee censor calldata")
        else:
            raise FeedbackError("invalid fee feedback action")

    def submit(self, kind: str, token: str, data: str,
               evidence_hash: str | None, state: dict[str, Any], *,
               replacement_nonce: int | None = None,
               minimum_priority_wei: int = 0,
               minimum_fee_wei: int = 0) -> None:
        token = watch.address(token)
        self._validate_payload(kind, token, data, evidence_hash)
        estimate = watch.quantity(self.rpc.call("eth_estimateGas", [{"from": self.address,
                                        "to": self.q, "data": data}]), "feedback gas estimate")
        gas = price.ceil_div(estimate * 120, 100)
        if gas > self.max_gas:
            raise FeedbackError("feedback gas estimate exceeds configured cap")
        nonce = watch.quantity(self.rpc.call("eth_getTransactionCount", [self.address, "pending"]),
                               "feedback nonce")
        if replacement_nonce is not None:
            if replacement_nonce < 0 or replacement_nonce > nonce:
                raise FeedbackError("replacement feedback nonce is invalid")
            nonce = replacement_nonce
        latest = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(latest, dict) or "baseFeePerGas" not in latest:
            raise FeedbackError("latest block lacks EIP-1559 base fee")
        base_fee = watch.quantity(latest["baseFeePerGas"], "base fee")
        priority = max(min(watch.quantity(self.rpc.call("eth_maxPriorityFeePerGas", []),
                                          "priority fee"), self.max_priority_wei),
                       minimum_priority_wei)
        max_fee = max(base_fee * 2 + priority, minimum_fee_wei)
        if priority > self.max_priority_wei or max_fee > self.max_fee_wei:
            raise FeedbackError("feedback fee exceeds configured cap")
        tx = {"chainId": 4663, "nonce": nonce,
              "to": watch.signing_address(self.q), "value": 0,
              "data": data, "gas": gas, "type": 2,
              "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}
        signed = self.account.sign_transaction(tx)
        tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
        raw = "0x" + signed.raw_transaction.hex().removeprefix("0x")
        state["pendingTx"] = {"kind": kind, "token": token, "data": data,
                              "evidenceHash": evidence_hash, "nonce": nonce,
                              "txHash": tx_hash, "rawTx": raw}
        report._save_state(Path(state["statePath"]), state)
        sent = self.rpc.call("eth_sendRawTransaction", [raw])
        if not isinstance(sent, str) or sent.lower() != tx_hash:
            raise FeedbackError("RPC returned another fee feedback transaction hash")
        try:
            self._wait(tx_hash)
        except price.TransactionReverted:
            state["pendingTx"] = None
            report._save_state(Path(state["statePath"]), state)
            raise FeedbackError("fee feedback transaction reverted") from None
        state["pendingTx"] = None
        report._save_state(Path(state["statePath"]), state)

    def recover(self, state: dict[str, Any], binding: report.Bindings) -> None:
        pending = state.get("pendingTx")
        if pending is None:
            return
        if not isinstance(pending, dict):
            raise FeedbackError("pending fee transaction is malformed")
        kind = pending.get("kind")
        token = watch.address(pending.get("token"))
        data, digest = pending.get("data"), pending.get("evidenceHash")
        raw, tx_hash, nonce = pending.get("rawTx"), pending.get("txHash"), pending.get("nonce")
        if (not isinstance(data, str) or not isinstance(raw, str) or
                not isinstance(tx_hash, str) or not watch.HASH.fullmatch(tx_hash) or
                not isinstance(nonce, int) or nonce < 0 or
                not raw.startswith("0x") or len(raw) > 4098):
            raise FeedbackError("pending fee transaction fields are malformed")
        self._validate_payload(kind, token, data, digest)
        try:
            payload = bytes.fromhex(raw[2:])
            fields = rlp.decode(payload[1:])
            recovered = Account.recover_transaction(raw).lower()
        except Exception as exc:
            raise FeedbackError("pending fee transaction cannot be decoded") from exc
        n = lambda item: int.from_bytes(item, "big")
        if (payload[:1] != b"\x02" or not isinstance(fields, list) or len(fields) != 12 or
                "0x" + keccak(payload).hex() != tx_hash or recovered != self.address or
                n(fields[0]) != 4663 or n(fields[1]) != nonce or
                n(fields[2]) > self.max_priority_wei or n(fields[3]) > self.max_fee_wei or
                not 21_000 <= n(fields[4]) <= self.max_gas or
                fields[5] != bytes.fromhex(self.q[2:]) or n(fields[6]) != 0 or
                fields[7] != bytes.fromhex(data[2:]) or fields[8] != []):
            raise FeedbackError("pending signed fee transaction exceeds permissions or caps")
        try:
            confirmed = self._confirmed(tx_hash)
        except price.TransactionReverted:
            state["pendingTx"] = None
            report._save_state(Path(state["statePath"]), state)
            return
        except price.KeeperError as exc:
            if "receipt is not canonical" not in str(exc):
                raise
            # A previously mined transaction can be orphaned. Keep the
            # journal and treat it as absent until the canonical nonce and
            # outcome state prove what may safely be rebroadcast.
            confirmed = False
            orphaned = True
        else:
            orphaned = False
        if not confirmed:
            receipt = None if orphaned else self.rpc.call("eth_getTransactionReceipt", [tx_hash])
            known = (None if orphaned else receipt)
            if known is None and not orphaned:
                known = self.rpc.call("eth_getTransactionByHash", [tx_hash])
            confirmed_nonce = watch.quantity(self.rpc.call(
                "eth_getTransactionCount", [self.address, "latest"]), "confirmed feedback nonce")
            if confirmed_nonce > nonce:
                raise FeedbackError("feedback nonce was consumed without matching receipt")
            safe_head = watch.chain_head(self.rpc) - self.confirmations
            if safe_head < 1:
                raise FeedbackError("no confirmed block for fee transaction recovery")
            deadline, stage, status = pending_status(self.rpc, binding, token, safe_head)
            safe_time = watch.quantity(self.rpc.call(
                "eth_getBlockByNumber", [hex(safe_head), False])["timestamp"],
                "confirmed block time")
            if stage != 3 or status != 1 or deadline == 0:
                raise FeedbackError("fee outcome changed while signed transaction was pending")
            if kind == "report" and safe_time >= deadline:
                # Replace the unconfirmed report with a censor at the same
                # nonce. Both fee fields exceed the original by >10%, so a
                # node retaining the old report can accept the replacement.
                self.submit("censor", token, censor_data(token), None, state,
                            replacement_nonce=nonce,
                            minimum_priority_wei=(n(fields[2]) * 110 + 99) // 100 + 1,
                            minimum_fee_wei=(n(fields[3]) * 110 + 99) // 100 + 1)
                return
            if kind == "censor" and safe_time < deadline:
                raise FeedbackError("signed censor is not yet valid at confirmed head")
            if known is None:
                sent = self.rpc.call("eth_sendRawTransaction", [raw])
                if not isinstance(sent, str) or sent.lower() != tx_hash:
                    raise FeedbackError("pending fee rebroadcast returned another hash")
            try:
                self._wait(tx_hash)
            except price.TransactionReverted:
                state["pendingTx"] = None
                report._save_state(Path(state["statePath"]), state)
                return
        state["pendingTx"] = None
        report._save_state(Path(state["statePath"]), state)


def _load_state(rpc: watch.Rpc, path: Path, q: str, guard: str,
                start_block: int | None, safe_head: int) -> dict[str, Any]:
    if path.resolve().parent != LOCAL.resolve() or path.is_symlink():
        raise FeedbackError("fee feedback state must be a regular file directly in .local")
    if path.exists():
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise FeedbackError("fee feedback state is unreadable") from exc
        if (not isinstance(state, dict) or state.get("version") != 2 or
                state.get("q") != q or state.get("guard") != guard or
                state.get("chainId") != 4663 or not isinstance(state.get("pending"), dict) or
                not isinstance(state.get("lastBlock"), int) or
                not isinstance(state.get("lastHash"), str) or
                not watch.HASH.fullmatch(state["lastHash"]) or
                not isinstance(state.get("startBlock"), int) or
                state["lastBlock"] < state["startBlock"] - 1):
            raise FeedbackError("fee feedback state binding or cursor is malformed")
        if start_block is not None and start_block != state["startBlock"]:
            raise FeedbackError("start block conflicts with existing feedback state")
    else:
        if start_block is None or not 1 <= start_block <= safe_head:
            raise FeedbackError("first feedback run needs a confirmed Q deployment start block")
        if rpc.call("eth_getCode", [q, hex(start_block - 1)]) != "0x":
            raise FeedbackError("start block follows Q deployment")
        state = {"version": 2, "chainId": 4663, "q": q, "guard": guard,
                 "buyer": report.ZERO, "buyerCodeHash": "0x" + "00" * 32,
                 "startBlock": start_block, "lastBlock": start_block - 1,
                 "lastHash": watch.block_hash(rpc, start_block - 1),
                 "pending": {}, "pendingTx": None}
    state["statePath"] = str(path)
    return state


def cycle(rpc: watch.Rpc, binding: report.Bindings,
          quote_bindings: price.Bindings, state: dict[str, Any],
          signer: FeedbackSigner | None, confirmations: int,
          blocks_per_cycle: int, max_evidence_blocks: int,
          max_pending: int) -> dict[str, Any]:
    if signer and state.get("pendingTx") is not None:
        signer.recover(state, binding)
    snapshot = report.scan_once(rpc, binding, state, blocks_per_cycle,
                                max_evidence_blocks, max_pending, confirmations)
    report._save_state(Path(state["statePath"]), state)
    actions: list[dict[str, Any]] = []
    safe_head = int(snapshot["safeHead"])
    safe_time = watch.quantity(rpc.call("eth_getBlockByNumber", [hex(safe_head), False])["timestamp"],
                               "confirmed block time")
    for observation in snapshot["observations"]:
        token = observation["token"]
        pending = state["pending"].get(token)
        if pending is None or observation["status"] == "await_confirmed_finalization":
            continue
        deadline, stage, status = pending_status(rpc, binding, token, safe_head)
        if stage != 3 or status != 1 or deadline != pending["deadline"]:
            continue
        if safe_time >= deadline:
            actions.append({"token": token, "action": "censor_expired_completed_exit"})
            if signer:
                signer.submit("censor", token, censor_data(token), None, state)
            break
        if not observation.get("grossMarkEvidenceComplete"):
            actions.append({"token": token, "action": "await_receipt_evidence"})
            continue
        try:
            evidence = build_evidence(rpc, quote_bindings, observation)
        except (FeedbackError, watch.WatcherError, price.KeeperError):
            actions.append({"token": token, "action": "await_historical_quote"})
            continue
        digest = archive_evidence(evidence)
        score = int(evidence["score"]["grossReturnBpsForFeePolicy"])
        actions.append({"token": token, "action": "report_gross_mark", "scoreBps": score,
                        "evidenceHash": digest})
        if signer:
            if watch.block_hash(rpc, int(observation["exitBlock"])) != observation["evidenceBlockHash"]:
                raise FeedbackError("final exit block changed before signing")
            signer.submit("report", token, report_data(token, score, digest), digest, state)
        break
    return {"status": "live" if signer else "read_only", "safeHead": safe_head,
            "scannedThrough": snapshot["scannedThrough"],
            "pending": snapshot["pending"], "actions": actions,
            "writesSent": 1 if signer and actions and actions[-1]["action"] in
                          ("report_gross_mark", "censor_expired_completed_exit") else 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default=os.environ.get("PONS_HTTP_RPC_URL"))
    parser.add_argument("--q", default=os.environ.get("PONS_Q_ADDRESS"))
    parser.add_argument("--price-guard", default=os.environ.get("PONS_PRICE_GUARD_ADDRESS"))
    parser.add_argument("--quoter", default=price.QUOTER)
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--confirmations", type=int, default=3)
    parser.add_argument("--blocks-per-cycle", type=int, default=1000)
    parser.add_argument("--max-evidence-blocks", type=int, default=100000)
    parser.add_argument("--max-pending", type=int, default=2)
    parser.add_argument("--max-report-gas", type=int, default=300000)
    parser.add_argument("--max-fee-wei", type=int, default=10**11)
    parser.add_argument("--max-priority-wei", type=int, default=10**10)
    parser.add_argument("--receipt-timeout", type=int, default=60)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not args.http_url or not args.q or not args.price_guard:
        parser.error("HTTP RPC, Q, and price guard are required")
    if (args.confirmations < 1 or not 1 <= args.blocks_per_cycle <= 10000 or
            not 1 <= args.max_evidence_blocks <= 500000 or not 1 <= args.max_pending <= 20 or
            args.max_report_gas < 21000 or args.max_fee_wei <= 0 or
            args.max_priority_wei < 0 or args.receipt_timeout <= 0 or args.poll_seconds <= 0):
        parser.error("invalid feedback keeper bounds")
    key = os.environ.get("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY") if args.live else None
    if args.live and not key:
        parser.error("--live requires PONS_PRICE_CONFIGURATOR_PRIVATE_KEY")
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    if LOCAL.is_symlink() or args.state.resolve().parent != LOCAL.resolve():
        raise FeedbackError("fee feedback state path is unsafe")
    rpc = watch.HttpRpc(args.http_url)
    safe_head = watch.chain_head(rpc) - args.confirmations
    if safe_head < 1:
        raise FeedbackError("chain has no confirmed head")
    q, guard = watch.address(args.q), watch.address(args.price_guard)
    reporter_binding = report.verify_bindings(rpc, q, None, None, safe_head)
    signer_address = Account.from_key(key).address.lower() if key else None
    quote_binding = price.verify_bindings(rpc, 4663, q, guard, args.quoter,
                                          signer_address)
    if reporter_binding.executor != quote_binding.executor:
        raise FeedbackError("reporter and quote executor bindings differ")
    state = _load_state(rpc, args.state, q, guard, args.start_block, safe_head)
    signer = (FeedbackSigner(rpc, q, key, args.confirmations, args.max_report_gas,
                             args.max_fee_wei, args.max_priority_wei,
                             args.receipt_timeout, args.poll_seconds) if key else None)
    lock_path = args.state.with_suffix(args.state.suffix + ".lock")
    if lock_path.is_symlink():
        raise FeedbackError("feedback state lock must not be a symlink")
    with open(lock_path, "a+") as own_lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(own_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            with (price.ConfiguratorLock(q, args.state) if args.live else nullcontext()):
                output = cycle(rpc, reporter_binding, quote_binding, state, signer,
                               args.confirmations, args.blocks_per_cycle,
                               args.max_evidence_blocks, args.max_pending)
            print(json.dumps(output, sort_keys=True, separators=(",", ":")), flush=True)
            if args.once:
                return 0
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FeedbackError, report.ReportError, watch.WatcherError,
            price.KeeperError, BlockingIOError) as exc:
        raise SystemExit(f"fee feedback keeper stopped: {exc}") from exc
