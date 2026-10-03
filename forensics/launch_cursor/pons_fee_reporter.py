#!/usr/bin/env python3
"""Bounded, canonical receipt collector for pending hookless fee outcomes.

This module authenticates minted Q, position, settlement, and swap events.
The companion feedback keeper adds historical executable Q/ETH quotes and
may submit a clearly labeled gross mark-to-market fee score. This CLI remains
read-only and never signs or broadcasts.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any

from eth_abi import decode, encode
from eth_utils import keccak

import pons_launch_watcher as watch
import pons_price_keeper as price


HERE = Path(__file__).resolve().parent
LOCAL = HERE.parents[1] / ".local"
DEFAULT_STATE = LOCAL / "pons-fee-reporter.json"
ROBINHOOD_CHAIN_ID = 4663
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


PENDING = topic("FeeOutcomePending(address,uint64)")
ALL_POSITIONS_EXITED = topic("AllPositionsExited(address)")
REPORTED = topic("FeeOutcomeReported(address,int32,bytes32)")
CENSORED = topic("FeeOutcomeCensored(address)")
FEE_SELECTED = topic("FeeSelected(address,uint24,uint8,bool)")
POSITION_OPENED = topic("PositionOpened(address,bytes32,uint256,uint24,uint256)")
OPEN_MINTED = topic("OpenMinted(address,uint256,uint256)")
OPEN_TRANCHE_MINTED = topic("OpenTrancheMinted(address,uint8,uint256,uint256)")
POSITION_SETTLED = topic("PositionSettled(address,uint256,uint256,uint256,uint256)")
FEES_SETTLED = topic("FeesSettled(address,uint256,uint256,uint256,uint256)")
EXIT_SETTLED = topic("ExitSettled(address,uint256,uint256,uint256,uint256,uint256,uint256)")
QUOTE_BOUGHT = topic("QuoteBought(address,bytes32,uint256,uint256)")
TRANSFER = topic("Transfer(address,address,uint256)")
SWAP = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
STEP_SUCCEEDED = topic("StepSucceeded(address,uint8)")
ZERO = "0x" + "00" * 20
SELECTOR_BUY_Q = price.selector("buyQ(uint256,address,uint64)")
Q_EXECUTOR = price.selector("executor()")
Q_OWNER = price.selector("owner()")
Q_FEE_POLICY = price.selector("feePolicy()")
Q_DEADLINE = price.selector("outcomeDeadline(address)")
Q_LAUNCHES = price.selector("launches(address)")
FEE_ASSIGNMENTS = price.selector("assignments(address)")
BUYER_SOURCE = price.selector("source()")
BUYER_QUOTE = price.selector("quoteToken()")
BUYER_POOL = price.selector("poolManager()")
BUYER_POOL_ID = price.selector("poolId()")
EXEC_ROUTER = price.selector("settlementRouter()")
EXEC_POSITIONS = price.selector("positions(address)")
ROUTER_QUOTE_BUY = price.selector("quoteBuy()")
MAX_LOGS_PER_QUERY = 256


class ReportError(ValueError):
    """Evidence is unavailable, malformed, or contradictory. Never report."""


def _hex(value: Any, width: int, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 2 + width * 2:
        raise ReportError(f"{label} is not {width}-byte hex")
    try:
        bytes.fromhex(value[2:])
    except ValueError as exc:
        raise ReportError(f"{label} is invalid hex") from exc
    return value.lower()


def _word_address(address: str) -> str:
    return "0x" + watch.address(address)[2:].rjust(64, "0")


def _indexed_address(value: Any, label: str) -> str:
    word = _hex(value, 32, label)
    if word[2:26] != "0" * 24:
        raise ReportError(f"{label} has invalid address padding")
    return "0x" + word[-40:]


def _receipt(rpc: watch.Rpc, tx_hash: str, safe_head: int,
             cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tx_hash = _hex(tx_hash, 32, "transaction hash")
    if tx_hash in cache:
        return cache[tx_hash]
    row = rpc.call("eth_getTransactionReceipt", [tx_hash])
    if not isinstance(row, dict) or _hex(row.get("transactionHash"), 32, "receipt transaction hash") != tx_hash:
        raise ReportError("receipt is missing or belongs to another transaction")
    block = watch.quantity(row.get("blockNumber"), "receipt block")
    if block > safe_head or _hex(row.get("blockHash"), 32, "receipt block hash") != watch.block_hash(rpc, block):
        raise ReportError("receipt is unconfirmed or noncanonical")
    if watch.quantity(row.get("status"), "receipt status") != 1:
        raise ReportError("receipt did not succeed")
    if not isinstance(row.get("logs"), list):
        raise ReportError("receipt omitted logs")
    cache[tx_hash] = row
    return row


def _authenticated_log(rpc: watch.Rpc, log: dict[str, Any], safe_head: int,
                       cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(log, dict) or log.get("removed") is True:
        raise ReportError("removed or malformed confirmed log")
    tx_hash = _hex(log.get("transactionHash"), 32, "log transaction hash")
    receipt = _receipt(rpc, tx_hash, safe_head, cache)
    if _hex(log.get("blockHash"), 32, "log block hash") != receipt["blockHash"].lower():
        raise ReportError("log block disagrees with receipt")
    if watch.quantity(log.get("blockNumber"), "log block") != watch.quantity(receipt["blockNumber"], "receipt block"):
        raise ReportError("log block number disagrees with receipt")
    log_index = watch.quantity(log.get("logIndex"), "log index")
    matched = [item for item in receipt["logs"] if
               watch.quantity(item.get("logIndex"), "receipt log index") == log_index]
    if len(matched) != 1:
        raise ReportError("log is absent from canonical receipt")
    item = matched[0]
    for key in ("address", "data", "transactionIndex"):
        if str(item.get(key, "")).lower() != str(log.get(key, "")).lower():
            raise ReportError("queried log differs from canonical receipt")
    if [str(x).lower() for x in item.get("topics", [])] != [str(x).lower() for x in log.get("topics", [])]:
        raise ReportError("queried log topics differ from canonical receipt")
    return receipt


def _logs(rpc: watch.Rpc, address: str, topics: list[Any], first: int, last: int,
          safe_head: int, cache: dict[str, dict[str, Any]], span: int = 1000) -> list[dict[str, Any]]:
    if first < 0 or last > safe_head or span < 1:
        raise ReportError("log query lies outside confirmed range")
    found: list[dict[str, Any]] = []
    for begin in range(first, last + 1, span):
        end = min(last, begin + span - 1)
        anchor = watch.block_hash(rpc, end)
        result = rpc.call("eth_getLogs", [{"address": address, "topics": topics,
                                           "fromBlock": hex(begin), "toBlock": hex(end)}])
        if not isinstance(result, list) or len(result) > MAX_LOGS_PER_QUERY:
            raise ReportError("bounded log query overflow or malformed response")
        if watch.block_hash(rpc, end) != anchor:
            raise ReportError("canonical log range changed during query")
        for log in result:
            if not isinstance(log, dict) or str(log.get("address", "")).lower() != address:
                raise ReportError("RPC returned log for another contract")
            block = watch.quantity(log.get("blockNumber"), "log block")
            if not begin <= block <= end:
                raise ReportError("RPC returned out-of-range log")
            _authenticated_log(rpc, log, safe_head, cache)
            found.append(log)
        if watch.block_hash(rpc, end) != anchor:
            raise ReportError("canonical log range changed after receipt checks")
    return sorted(found, key=watch.log_order)


def _call(rpc: watch.Rpc, contract: str, signature: str, types: list[str],
          values: list[Any], results: list[str], block: int) -> tuple[Any, ...]:
    return price.call_abi(rpc, contract, price.selector(signature), types, values, results, hex(block))


def _read_address(rpc: watch.Rpc, contract: str, signature: str, block: int) -> str:
    return watch.address(_call(rpc, contract, signature, [], [], ["address"], block)[0])


@dataclass(frozen=True)
class Bindings:
    q: str
    executor: str
    router: str
    fee_policy: str
    buyer: str
    owner: str
    buyer_code_hash: str
    buyer_pool_id: str


def verify_bindings(rpc: watch.Rpc, q: str, buyer: str | None, buyer_code_hash: str | None,
                    safe_head: int) -> Bindings:
    q = watch.address(q)
    buyer = watch.address(buyer) if buyer else ZERO
    code_hash = _hex(buyer_code_hash, 32, "buyer code hash") if buyer_code_hash else "0x" + "00" * 32
    if watch.quantity(rpc.call("eth_chainId", []), "chain ID") != ROBINHOOD_CHAIN_ID:
        raise ReportError("RPC is not Robinhood mainnet")
    owner = _read_address(rpc, q, "owner()", safe_head)
    executor = _read_address(rpc, q, "executor()", safe_head)
    fee_policy = _read_address(rpc, q, "feePolicy()", safe_head)
    router = _read_address(rpc, executor, "settlementRouter()", safe_head)
    contracts = [("Q", q), ("executor", executor), ("router", router),
                 ("fee policy", fee_policy)]
    if buyer != ZERO:
        contracts.append(("buyer", buyer))
    for name, address in contracts:
        code = rpc.call("eth_getCode", [address, hex(safe_head)])
        if not isinstance(code, str) or code == "0x":
            raise ReportError(f"{name} code unavailable")
        if name == "buyer" and "0x" + keccak(bytes.fromhex(code[2:])).hex() != code_hash:
            raise ReportError("buyer runtime code hash changed")
    if (_read_address(rpc, router, "quoteToken()", safe_head) != q or
            _read_address(rpc, router, "source()", safe_head) != executor):
        raise ReportError("Q, executor, and router bindings disagree")
    buyer_pool_id = b"\x00" * 32
    if buyer != ZERO:
        if (_read_address(rpc, buyer, "source()", safe_head) != owner or
                _read_address(rpc, buyer, "quoteToken()", safe_head) != q or
                _read_address(rpc, buyer, "poolManager()", safe_head) != POOL_MANAGER):
            raise ReportError("owner buyer bindings disagree")
        buyer_pool_id, = _call(rpc, buyer, "poolId()", [], [], ["bytes32"], safe_head)
        expected_pool_id = keccak(encode(
            ["address", "address", "uint24", "int24", "address"],
            [ZERO, q, *_call(rpc, buyer, "fee()", [], [], ["uint24"], safe_head),
             *_call(rpc, buyer, "tickSpacing()", [], [], ["int24"], safe_head), ZERO]
        ))
        if buyer_pool_id != expected_pool_id:
            raise ReportError("owner buyer is not bound to its declared zero-hook Q/ETH pool")
    return Bindings(q, executor, router, fee_policy, buyer, owner, code_hash,
                    "0x" + buyer_pool_id.hex())


def _event_data(log: dict[str, Any], types: list[str], label: str) -> tuple[Any, ...]:
    raw = log.get("data")
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise ReportError(f"{label} has malformed data")
    try:
        return decode(types, bytes.fromhex(raw[2:]))
    except Exception as exc:
        raise ReportError(f"{label} data does not match ABI") from exc


def _tx(rpc: watch.Rpc, receipt: dict[str, Any]) -> dict[str, Any]:
    tx_hash = _hex(receipt["transactionHash"], 32, "receipt transaction hash")
    row = rpc.call("eth_getTransactionByHash", [tx_hash])
    if not isinstance(row, dict) or _hex(row.get("hash"), 32, "transaction hash") != tx_hash or \
            _hex(row.get("blockHash"), 32, "transaction block hash") != receipt["blockHash"].lower():
        raise ReportError("transaction is not in its canonical receipt block")
    return row


def _buyer_lot(rpc: watch.Rpc, log: dict[str, Any], binding: Bindings,
               safe_head: int, cache: dict[str, dict[str, Any]]) -> tuple[int | None, str]:
    """Accept only a direct owner buy into the vault from the pinned buyer."""
    receipt = _authenticated_log(rpc, log, safe_head, cache)
    amount, = _event_data(log, ["uint256"], "Q Transfer")
    tx = _tx(rpc, receipt)
    if (str(tx.get("from", "")).lower() != binding.owner or
            str(tx.get("to", "")).lower() != binding.buyer):
        return None, "Q arrived without direct owner buyer transaction"
    buys = [item for item in receipt["logs"] if
            str(item.get("address", "")).lower() == binding.buyer and
            item.get("topics", [None])[0].lower() == QUOTE_BOUGHT]
    if len(buys) != 1 or len(buys[0].get("topics", [])) != 3:
        return None, "Q arrived without unique owner buyer QuoteBought"
    bought = buys[0]
    if _indexed_address(bought["topics"][1], "QuoteBought recipient") != binding.executor:
        return None, "QuoteBought recipient differs from vault"
    if _hex(bought["topics"][2], 32, "QuoteBought pool ID") != binding.buyer_pool_id:
        return None, "QuoteBought came from unexpected Q/ETH pool"
    eth_in, q_out = _event_data(bought, ["uint256", "uint256"], "QuoteBought")
    if q_out != amount or eth_in != watch.quantity(tx.get("value"), "buy transaction value") or eth_in == 0:
        return None, "QuoteBought does not reconcile Q and ETH"
    calldata = str(tx.get("input", "")).lower()
    if not calldata.startswith(SELECTOR_BUY_Q):
        return None, "owner transaction is not buyQ"
    try:
        min_q, recipient, deadline = decode(["uint256", "address", "uint64"], bytes.fromhex(calldata[10:]))
    except Exception as exc:
        raise ReportError("owner buyQ calldata is malformed") from exc
    if watch.address(recipient) != binding.executor or min_q == 0 or min_q > q_out or deadline == 0:
        return None, "owner buyQ calldata does not reconcile with vault receipt"
    transfers = [item for item in receipt["logs"] if
                 str(item.get("address", "")).lower() == binding.q and
                 item.get("topics", [None])[0].lower() == TRANSFER and
                 len(item.get("topics", [])) == 3 and
                 _indexed_address(item["topics"][2], "Q transfer recipient") == binding.executor]
    if len(transfers) != 1 or transfers[0]["logIndex"] != log["logIndex"] or \
            _indexed_address(log["topics"][1], "Q transfer sender") != binding.buyer:
        return None, "buyer purchase lacks unique direct Q Transfer to vault"
    return eth_in, "confirmed owner buyer receipt " + receipt["transactionHash"].lower()


def cost_lots(rpc: watch.Rpc, binding: Bindings, start: int, open_block: int,
              target_open: dict[str, Any], safe_head: int,
              cache: dict[str, dict[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
    vault = _word_address(binding.executor)
    inbound = _logs(rpc, binding.q, [TRANSFER, None, vault], start, open_block, safe_head, cache)
    outbound = _logs(rpc, binding.q, [TRANSFER, vault], start, open_block, safe_head, cache)
    # An unrelated vault outflow still consumes FIFO inventory; only the
    # target position's exact spend receives a cost assignment.
    flows = sorted([(row, 1) for row in inbound] + [(row, -1) for row in outbound],
                   key=lambda item: watch.log_order(item[0]))
    # A self-transfer is not a purchase or position spend.
    if len({(row["transactionHash"].lower(), watch.quantity(row["logIndex"], "log index"))
            for row, _ in flows}) != len(flows):
        raise ReportError("vault self-transfer or duplicate Q log confuses lot accounting")
    queue: list[list[Any]] = []  # remaining Q, remaining ETH cost or None, source receipt
    used: list[dict[str, str]] = []
    reasons: list[str] = []
    target_hash = target_open["transactionHash"].lower()
    target_spent = int(_event_data(target_open, ["uint24", "uint256"], "PositionOpened")[1])
    actual_target_out = 0
    for row, direction in flows:
        tx_hash = row["transactionHash"].lower()
        amount, = _event_data(row, ["uint256"], "Q Transfer")
        if amount == 0:
            continue
        if direction == 1:
            paid, source = _buyer_lot(rpc, row, binding, safe_head, cache)
            queue.append([amount, paid, source])
            continue
        if tx_hash == target_hash:
            actual_target_out += amount
        # Every withdrawal consumes FIFO Q, including earlier positions and
        # nonposition transfers. Unknown inflows stay unknown until consumed.
        remaining = amount
        while remaining:
            if not queue:
                reasons.append("vault Q outflow exceeds all observed inflows")
                queue.append([remaining, None, "unexplained opening balance"])
            lot = queue[0]
            part = min(remaining, lot[0])
            attributed = None if lot[1] is None else lot[1] * part // lot[0]
            if tx_hash == target_hash:
                if attributed is None:
                    reasons.append("entry Q spent from unpriced or unexplained inflow")
                else:
                    used.append({"qAmountWei": str(part), "ethCostWei": str(attributed),
                                 "source": lot[2]})
            lot[0] -= part
            if attributed is not None:
                lot[1] -= attributed
            remaining -= part
            if lot[0] == 0:
                queue.pop(0)
    if actual_target_out != target_spent:
        reasons.append("vault Q Transfer outflow differs from PositionOpened.quoteSpent")
    if sum(int(row["qAmountWei"]) for row in used) != target_spent:
        reasons.append("authenticated cost lots do not cover exact entry Q")
    return used, sorted(set(reasons))


def collect_exit(rpc: watch.Rpc, binding: Bindings, token: str,
                 pending: dict[str, Any], start: int, safe_head: int,
                 max_evidence_blocks: int) -> dict[str, Any]:
    token = watch.address(token)
    exit_block = int(pending["block"])
    if exit_block < start or exit_block - start + 1 > max_evidence_blocks:
        raise ReportError("exit exceeds bounded evidence lookback")
    cache: dict[str, dict[str, Any]] = {}
    indexed = _word_address(token)
    opens = _logs(rpc, binding.executor, [POSITION_OPENED, indexed], start, exit_block, safe_head, cache)
    selections = _logs(rpc, binding.fee_policy, [FEE_SELECTED, indexed], start, exit_block, safe_head, cache)
    if len(opens) != 3 or len(selections) != 1:
        raise ReportError("expected three authenticated PositionOpened tranches and one FeeSelected")
    opened, selected = opens[0], selections[0]
    if len({row["transactionHash"].lower() for row in opens}) != 1:
        raise ReportError("three position tranches were not opened atomically")
    open_block = watch.quantity(opened["blockNumber"], "open block")
    if open_block > exit_block:
        raise ReportError("position opened after exit")
    chosen_fee, _arm, _explore = _event_data(selected, ["uint24", "uint8", "bool"], "FeeSelected")
    pool_ids: set[str] = set()
    token_ids: set[int] = set()
    spent_by_tranche: list[int] = []
    for row in opens:
        if len(row.get("topics", [])) != 4:
            raise ReportError("PositionOpened has malformed indexed fields")
        fee, spent = _event_data(row, ["uint24", "uint256"], "PositionOpened")
        if fee != chosen_fee or spent == 0:
            raise ReportError("opened tranche disagrees with selected fee or spent zero Q")
        pool_ids.add(_hex(row["topics"][2], 32, "X/Q pool ID"))
        token_ids.add(int(_hex(row["topics"][3], 32, "position NFT"), 16))
        spent_by_tranche.append(int(spent))
    if len(pool_ids) != 1 or len(token_ids) != 3 or 0 in token_ids:
        raise ReportError("opened tranches lack one pool and three distinct position NFTs")
    quote_spent = sum(spent_by_tranche)
    open_receipt = _receipt(rpc, opened["transactionHash"], safe_head, cache)
    aggregates = [row for row in open_receipt["logs"] if
                  str(row.get("address", "")).lower() == binding.q and
                  row.get("topics", [None])[0].lower() == OPEN_MINTED and
                  len(row.get("topics", [])) == 2 and
                  _indexed_address(row["topics"][1], "minted token") == token]
    tranches = [row for row in open_receipt["logs"] if
                str(row.get("address", "")).lower() == binding.q and
                row.get("topics", [None])[0].lower() == OPEN_TRANCHE_MINTED and
                len(row.get("topics", [])) == 3 and
                _indexed_address(row["topics"][1], "tranche token") == token]
    if len(aggregates) != 1 or len(tranches) != 3:
        raise ReportError("open receipt lacks aggregate and three tranche mint events")
    minted, aggregate_spent = _event_data(aggregates[0], ["uint256", "uint256"], "OpenMinted")
    tranche_amounts: list[tuple[int, int]] = []
    for index, row in enumerate(sorted(tranches, key=lambda item: int(item["topics"][2], 16))):
        if int(_hex(row["topics"][2], 32, "tranche index"), 16) != index:
            raise ReportError("open receipt has duplicate or missing tranche index")
        tranche_amounts.append(tuple(map(int, _event_data(
            row, ["uint256", "uint256"], "OpenTrancheMinted"))))
    if (minted < quote_spent or aggregate_spent != quote_spent or
            sum(item[0] for item in tranche_amounts) != minted or
            [item[1] for item in tranche_amounts] != spent_by_tranche):
        raise ReportError("minted Q does not reconcile with three position spends")
    minted_transfers = [row for row in open_receipt["logs"] if
                        str(row.get("address", "")).lower() == binding.q and
                        row.get("topics", [None])[0].lower() == TRANSFER and
                        len(row.get("topics", [])) == 3 and
                        _indexed_address(row["topics"][1], "Q mint source") == ZERO and
                        _indexed_address(row["topics"][2], "Q mint recipient") == binding.executor]
    unused_burns = [row for row in open_receipt["logs"] if
                    str(row.get("address", "")).lower() == binding.q and
                    row.get("topics", [None])[0].lower() == TRANSFER and
                    len(row.get("topics", [])) == 3 and
                    _indexed_address(row["topics"][1], "unused Q burn source") == binding.executor and
                    _indexed_address(row["topics"][2], "unused Q burn recipient") == ZERO]
    if (sum(int(_event_data(row, ["uint256"], "Q mint")[0]) for row in minted_transfers) != minted or
            sum(int(_event_data(row, ["uint256"], "unused Q burn")[0]) for row in unused_burns)
            != minted - quote_spent):
        raise ReportError("open Q mint and unused burn Transfers do not reconcile")
    exit_receipt = _receipt(rpc, pending["txHash"], safe_head, cache)
    if watch.quantity(exit_receipt["blockNumber"], "exit block") != exit_block:
        raise ReportError("pending event exit block disagrees with receipt")
    final_markers = [row for row in exit_receipt["logs"] if
                     str(row.get("address", "")).lower() == binding.q and
                     row.get("topics", [None])[0].lower() == ALL_POSITIONS_EXITED and
                     len(row.get("topics", [])) == 2 and
                     _indexed_address(row["topics"][1], "final closure token") == token]
    if len(final_markers) != 1:
        raise ReportError("pending receipt lacks unique AllPositionsExited final marker")
    pending_logs = [row for row in exit_receipt["logs"] if
                    str(row.get("address", "")).lower() == binding.q and
                    row.get("topics", [None])[0].lower() == PENDING and
                    len(row.get("topics", [])) == 2 and
                    _indexed_address(row["topics"][1], "pending token") == token]
    if len(pending_logs) != 1 or int(_event_data(pending_logs[0], ["uint64"], "FeeOutcomePending")[0]) != int(pending["deadline"]):
        raise ReportError("exit receipt lacks matching FeeOutcomePending")
    exit_logs = _logs(rpc, binding.executor, [POSITION_SETTLED, indexed],
                      open_block, exit_block, safe_head, cache)
    interim_logs = _logs(rpc, binding.executor, [FEES_SETTLED, indexed],
                         open_block, exit_block, safe_head, cache)
    router_logs = _logs(rpc, binding.router, [EXIT_SETTLED, indexed],
                        open_block, exit_block, safe_head, cache)
    if len(exit_logs) != 3:
        raise ReportError("expected exactly three authenticated position exits")
    if exit_logs[-1]["transactionHash"].lower() != exit_receipt["transactionHash"].lower():
        raise ReportError("final marker is not attached to the third position exit")
    all_settlements = sorted([*exit_logs, *interim_logs], key=watch.log_order)
    settlement_txs = [row["transactionHash"].lower() for row in all_settlements]
    router_txs = [row["transactionHash"].lower() for row in router_logs]
    if (len(set(settlement_txs)) != len(all_settlements) or
            len(set(router_txs)) != len(router_logs) or
            set(settlement_txs) != set(router_txs)):
        raise ReportError("settlement transactions and router payouts do not match one-to-one")
    router_by_tx = {row["transactionHash"].lower(): row for row in router_logs}
    interim: list[dict[str, Any]] = []
    position_exits: list[dict[str, Any]] = []
    settlement_burns: list[dict[str, Any]] = []
    for settlement in all_settlements:
        tx_hash = settlement["transactionHash"].lower()
        receipt = _receipt(rpc, tx_hash, safe_head, cache)
        router_event = router_by_tx[tx_hash]
        burns = [item for item in receipt["logs"] if
                 str(item.get("address", "")).lower() == binding.q and
                 item.get("topics", [None])[0].lower() == TRANSFER and
                 len(item.get("topics", [])) == 3 and
                 _indexed_address(item["topics"][1], "Q burn source") == binding.router and
                 _indexed_address(item["topics"][2], "Q burn recipient") == ZERO]
        if len(burns) != 1:
            raise ReportError("settlement receipt lacks unique Q burn")
        sx, sq, eth_out, burned = _event_data(settlement, ["uint256"] * 4,
                                               "executor settlement")
        rx, reth, q_bought, rburned, wizard, developer = _event_data(
            router_event, ["uint256"] * 6, "ExitSettled")
        if (sx != rx or eth_out != reth or burned != rburned or
                burned != sq + q_bought or
                wizard + developer != eth_out - eth_out // 2 or
                wizard != (eth_out - eth_out // 2) // 2 or burned == 0 or
                int(_event_data(burns[0], ["uint256"], "Q burn")[0]) != burned):
            raise ReportError("executor/router cash and burn values do not reconcile")
        if (sx == 0) != (eth_out == 0 and q_bought == 0):
            raise ReportError("X sale and Q purchase disagree")
        kind = "position_exit" if settlement["topics"][0].lower() == POSITION_SETTLED else "fee_harvest"
        record = {"txHash": tx_hash,
                  "block": watch.quantity(settlement["blockNumber"], "settlement block"),
                  "xSoldWei": str(sx), "ethOutWei": str(eth_out),
                  "recipientCashEthWei": str(wizard + developer),
                  "qBurnedWei": str(burned)}
        if kind == "position_exit":
            record["qPrincipalAndFeeSettledWei"] = str(sq)
            position_exits.append(record)
        else:
            record["qFeeSettledWei"] = str(sq)
            interim.append(record)
        settlement_burns.append({"kind": kind, **record})
    total_cash = sum(int(item["recipientCashEthWei"]) for item in settlement_burns)
    total_burned = sum(int(item["qBurnedWei"]) for item in settlement_burns)
    final_exit = position_exits[-1]
    # The current contracts emit no per-position gas allocation for reverted
    # cranks or shared transfers. These receipts are known, not a full audit.
    known_receipts = []
    for tx_hash in {opened["transactionHash"].lower(), selected["transactionHash"].lower(),
                    *settlement_txs}:
        receipt = _receipt(rpc, tx_hash, safe_head, cache)
        tx = _tx(rpc, receipt)
        known_receipts.append({"txHash": tx_hash, "payer": str(tx["from"]).lower(),
                               "gasUsed": str(watch.quantity(receipt["gasUsed"], "gas used")),
                               "effectiveGasPriceWei": str(watch.quantity(receipt["effectiveGasPrice"], "gas price"))})
    known_receipts.sort(key=lambda row: row["txHash"])
    # Full Swap window on the *custom* pool ID, from actual open through exit.
    pool_id = next(iter(pool_ids))
    swaps = _logs(rpc, POOL_MANAGER, [SWAP, pool_id], open_block, exit_block, safe_head, cache)
    q_is_0 = int(binding.q, 16) < int(token, 16)
    x_in = q_in = x_out = q_out = 0
    for swap in swaps:
        if len(swap.get("topics", [])) != 3:
            raise ReportError("X/Q Swap has malformed indexed fields")
        amount0, amount1, _sqrt, _liquidity, _tick, swap_fee = _event_data(
            swap, ["int128", "int128", "uint160", "uint128", "int24", "uint24"], "X/Q Swap")
        if amount0 * amount1 >= 0 or swap_fee != chosen_fee:
            raise ReportError("X/Q Swap deltas or static fee are inconsistent")
        q_delta, x_delta = (amount0, amount1) if q_is_0 else (amount1, amount0)
        q_in += max(q_delta, 0)
        x_in += max(x_delta, 0)
        q_out += max(-q_delta, 0)
        x_out += max(-x_delta, 0)
    return {"token": token, "feePips": int(chosen_fee), "poolId": pool_id,
            "openTxHash": opened["transactionHash"].lower(), "exitTxHash": exit_receipt["transactionHash"].lower(),
            "openBlock": open_block, "exitBlock": exit_block,
            "positionTokenIds": sorted(token_ids), "trancheCount": 3,
            "qMintedWei": str(minted), "qUnusedBurnedAtOpenWei": str(minted - quote_spent),
            "qSpentWei": str(quote_spent),
            "exitEthOutWei": final_exit["ethOutWei"],
            "exitRecipientCashEthWei": final_exit["recipientCashEthWei"],
            "exitQBurnedWei": final_exit["qBurnedWei"],
            "recipientCashEthWei": str(total_cash),
            "qBurnedWei": str(total_burned),
            "xSoldWei": str(sum(int(item["xSoldWei"]) for item in settlement_burns)),
            "positionExits": position_exits, "interimHarvests": interim,
            "settlementBurns": settlement_burns,
            "customPoolSwapCount": len(swaps),
            "customPoolQInWei": str(q_in), "customPoolXInWei": str(x_in),
            "customPoolQOutWei": str(q_out), "customPoolXOutWei": str(x_out),
            "knownReceipts": known_receipts,
            "evidenceBlock": exit_block, "evidenceBlockHash": watch.block_hash(rpc, exit_block),
            "gasComplete": False, "grossMarkEvidenceComplete": True,
            "reportable": False,
            "reportBlockedBy": ["historical size-aware Q/ETH quotes not yet attached"]}


def _save_state(path: Path, data: dict[str, Any]) -> None:
    if path.resolve().parent != LOCAL.resolve() or LOCAL.is_symlink() or path.is_symlink():
        raise ReportError("reporter state must be a regular file directly in .local")
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    staging = path.with_suffix(path.suffix + f".{os.getpid()}.new")
    try:
        with open(staging, "x", encoding="utf-8") as file:
            os.chmod(staging, 0o600)
            json.dump(data, file, separators=(",", ":"), sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(staging, path)
        fd = os.open(LOCAL, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        staging.unlink(missing_ok=True)


def scan_once(rpc: watch.Rpc, binding: Bindings, state: dict[str, Any],
              blocks_per_cycle: int, max_evidence_blocks: int,
              max_pending: int, confirmations: int) -> dict[str, Any]:
    safe_head = watch.chain_head(rpc) - confirmations
    if safe_head < int(state["lastBlock"]):
        raise ReportError("confirmed head moved behind reporter cursor")
    if watch.block_hash(rpc, int(state["lastBlock"])) != state["lastHash"]:
        raise ReportError("reporter cursor was reorged; reconcile before continuing")
    last = min(safe_head, int(state["lastBlock"]) + blocks_per_cycle)
    cache: dict[str, dict[str, Any]] = {}
    if last > state["lastBlock"]:
        events = _logs(rpc, binding.q, [[PENDING, REPORTED, CENSORED]],
                       state["lastBlock"] + 1, last, safe_head, cache)
        for row in events:
            topics = row["topics"]
            if len(topics) != 2:
                raise ReportError("outcome event has unexpected indexed fields")
            token = _indexed_address(topics[1], "outcome token")
            kind = topics[0].lower()
            if kind == PENDING:
                deadline, = _event_data(row, ["uint64"], "FeeOutcomePending")
                if token in state["pending"]:
                    raise ReportError("same token has two pending outcomes")
                state["pending"][token] = {"txHash": row["transactionHash"].lower(),
                                           "block": watch.quantity(row["blockNumber"], "pending block"),
                                           "deadline": deadline}
            else:
                state["pending"].pop(token, None)
        state["lastBlock"], state["lastHash"] = last, watch.block_hash(rpc, last)
    observations: list[dict[str, Any]] = []
    for token, pending in list(state["pending"].items())[:max_pending]:
        try:
            deadline, = _call(rpc, binding.q, "outcomeDeadline(address)", ["address"], [token], ["uint64"], safe_head)
            stage = int(_call(rpc, binding.q, "launches(address)", ["address"], [token],
                              ["uint8", "bool", "uint64", "uint64", "uint64", "uint64",
                               "uint32", "bool", "bool", "uint64", "uint32", "bool"], safe_head)[0])
            _fee, _arm, status = _call(rpc, binding.fee_policy, "assignments(address)",
                                       ["address"], [token], ["uint24", "uint8", "uint8"], safe_head)
            if deadline == 0 or status != 1 or stage != 3:
                observations.append({"token": token, "status": "await_confirmed_finalization"})
                continue
            if deadline != pending["deadline"]:
                raise ReportError("onchain deadline differs from authenticated pending event")
            expired = watch.quantity(
                rpc.call("eth_getBlockByNumber", [hex(safe_head), False])["timestamp"], "safe timestamp"
            ) >= deadline
            try:
                row = collect_exit(rpc, binding, token, pending, state["startBlock"],
                                   safe_head, max_evidence_blocks)
            except (ReportError, watch.WatcherError, price.KeeperError) as exc:
                observations.append({"token": token,
                                     "status": "censor_due" if expired else "evidence_unavailable",
                                     "reason": str(exc), "reportable": False})
                continue
            row["status"] = "censor_due" if expired else "pending_incomplete_evidence"
            observations.append(row)
        except (ReportError, watch.WatcherError, price.KeeperError) as exc:
            observations.append({"token": token, "status": "evidence_unavailable",
                                 "reason": str(exc), "reportable": False})
    return {"status": "read_only", "scannedThrough": state["lastBlock"],
            "safeHead": safe_head, "pending": len(state["pending"]),
            "observations": observations, "writesSent": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default=os.environ.get("PONS_HTTP_RPC_URL"))
    parser.add_argument("--q", default=os.environ.get("PONS_Q_ADDRESS"))
    parser.add_argument("--buyer", default=os.environ.get("PONS_VAULT_BUY_ADAPTER_ADDRESS"))
    parser.add_argument("--buyer-code-hash", default=os.environ.get("PONS_VAULT_BUY_CODE_HASH"))
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--confirmations", type=int, default=3)
    parser.add_argument("--blocks-per-cycle", type=int, default=1000)
    parser.add_argument("--max-evidence-blocks", type=int, default=100000)
    parser.add_argument("--max-pending", type=int, default=2)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not args.http_url or not args.q:
        parser.error("HTTP RPC and Q address are required")
    if bool(args.buyer) != bool(args.buyer_code_hash):
        parser.error("buyer and buyer-code-hash must be supplied together")
    if (args.confirmations < 1 or not 1 <= args.blocks_per_cycle <= 10000 or
            not 1 <= args.max_evidence_blocks <= 500000 or not 1 <= args.max_pending <= 20 or
            args.poll_seconds <= 0):
        parser.error("invalid bounded scan settings")
    if args.state.resolve().parent != LOCAL.resolve():
        parser.error("reporter state must be directly inside repository .local")
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    lock_path = args.state.with_suffix(args.state.suffix + ".lock")
    if lock_path.is_symlink():
        raise ReportError("reporter lock must not be a symlink")
    with open(lock_path, "a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReportError("another fee reporter holds this cursor") from exc
        rpc = watch.HttpRpc(args.http_url)
        safe_head = watch.chain_head(rpc) - args.confirmations
        if safe_head < 1:
            raise ReportError("chain has no confirmed head")
        binding = verify_bindings(rpc, args.q, args.buyer, args.buyer_code_hash, safe_head)
        if args.state.exists():
            if args.state.is_symlink():
                raise ReportError("reporter state must not be a symlink")
            try:
                state = json.loads(args.state.read_text())
            except (OSError, ValueError) as exc:
                raise ReportError("reporter state is unreadable") from exc
            if (not isinstance(state, dict) or state.get("version") != 1 or
                    state.get("chainId") != ROBINHOOD_CHAIN_ID or
                    state.get("q") != binding.q or state.get("buyer") != binding.buyer or
                    state.get("buyerCodeHash") != binding.buyer_code_hash or
                    not isinstance(state.get("pending"), dict)):
                raise ReportError("reporter state binding or schema changed")
            if (not isinstance(state.get("startBlock"), int) or state["startBlock"] < 1 or
                    not isinstance(state.get("lastBlock"), int) or
                    state["lastBlock"] < state["startBlock"] - 1 or
                    not isinstance(state.get("lastHash"), str) or
                    not watch.HASH.fullmatch(state["lastHash"])):
                raise ReportError("reporter cursor fields are malformed")
            for token, pending in state["pending"].items():
                if (not isinstance(token, str) or not watch.ADDRESS.fullmatch(token) or
                        not isinstance(pending, dict) or
                        not isinstance(pending.get("block"), int) or
                        not isinstance(pending.get("deadline"), int) or
                        not isinstance(pending.get("txHash"), str) or
                        not watch.HASH.fullmatch(pending["txHash"])):
                    raise ReportError("reporter pending outcome is malformed")
            if args.start_block is not None and args.start_block != state.get("startBlock"):
                raise ReportError("start block conflicts with saved reporter cursor")
        else:
            if args.start_block is None or not 1 <= args.start_block <= safe_head:
                parser.error("first run requires a confirmed Q deployment --start-block")
            # Completeness proof: no Q code before the claimed deployment block.
            if rpc.call("eth_getCode", [binding.q, hex(args.start_block - 1)]) != "0x":
                raise ReportError("start block is after Q deployment; earlier mint evidence may be missing")
            state = {"version": 1, "chainId": ROBINHOOD_CHAIN_ID, "q": binding.q,
                     "buyer": binding.buyer, "buyerCodeHash": binding.buyer_code_hash,
                     "startBlock": args.start_block,
                     "lastBlock": args.start_block - 1,
                     "lastHash": watch.block_hash(rpc, args.start_block - 1), "pending": {}}
        while True:
            result = scan_once(rpc, binding, state, args.blocks_per_cycle,
                               args.max_evidence_blocks, args.max_pending, args.confirmations)
            _save_state(args.state, state)
            print(json.dumps(result, separators=(",", ":"), sort_keys=True), flush=True)
            if args.once:
                return 0
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReportError, watch.WatcherError, price.KeeperError) as exc:
        raise SystemExit(f"fee reporter stopped: {exc}") from exc
