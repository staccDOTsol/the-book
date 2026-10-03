#!/usr/bin/env python3
"""Offchain Q position inspector and size-aware exit planner.

Confirmed Q open/exit events feed a durable active-position queue. Live mode
may sign only PositionInspector.poke, Q.configureExit, and Q.processNext.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_account import Account
from eth_utils import keccak
import rlp

import pons_launch_watcher as watch
import pons_price_keeper as price


DEFAULT_STATE = price.LOCAL / "pons-exit-keeper.json"
STEP_SUCCEEDED = "0x" + keccak(text="StepSucceeded(address,uint8)").hex()
LAUNCH_ABORTED = "0x" + keccak(text="LaunchAborted(address,uint256,uint256)").hex()
CONFIGURE_EXIT = price.selector("configureExit(address,(uint128,uint128,uint256,uint256,uint64))")
POKE = price.selector("poke(address)")
PROCESS_NEXT = price.PROCESS_NEXT
INSPECT = price.selector("inspect(address)")
WITHDRAW = price.selector("withdrawPosition(address,uint128,uint128,uint64)")
HARVESTED = price.selector("harvestedAmounts(address)")
SETTLEMENT_ROUTER = price.selector("settlementRouter()")
ACTIVE_SALE = price.selector("activeSale()")
PREVIEW_ACTIVE_SALE = price.selector("previewNativeActiveSale(address,uint256)")
EXIT_NOTIFIER = price.selector("exitNotifier()")
CURSOR = price.selector("cursor()")
CONTROLLER = price.selector("controller()")
SOURCE = price.selector("source()")
NEXT_ACTION = price.selector("nextAction()")
EXIT_CONFIGS = price.selector("exitConfigs(address)")
EXIT_CONFIGURATOR = price.selector("exitConfigurator()")
OWNER = price.selector("owner()")
EXIT_CONFIG_TYPES = ["uint128", "uint128", "uint256", "uint256", "uint64"]
LAUNCH_STATE_TYPES = ["uint8", "bool", "uint64", "uint64", "uint64", "uint64",
                      "uint32", "bool", "bool", "uint64", "uint32", "bool"]


class ExitError(price.KeeperError):
    pass


class WaitForExit(ExitError):
    pass


class ExitConfiguratorLock(price.ConfiguratorLock):
    """One exit signer journal, independent of the price signer lock."""

    def __init__(self, q: str, state_path: Path):
        super().__init__(q, state_path)
        self.path = price.LOCAL / f"pons-exit-configurator-{watch.address(q)[2:]}.lock"

@dataclass(frozen=True)
class ExitBindings:
    price: price.Bindings
    inspector: str
    router: str
    active_sale: str

    @property
    def q(self) -> str:
        return self.price.q

    @property
    def executor(self) -> str:
        return self.price.executor


@dataclass(frozen=True)
class ExitSettings:
    lp_slippage_bps: int = 1000
    swap_slippage_bps: int = 1500
    ttl_seconds: int = 120
    max_snapshot_age_seconds: int = 20

    def validate(self) -> None:
        if (not 1 <= self.lp_slippage_bps < 10000 or
                not 1 <= self.swap_slippage_bps < 10000 or
                not 30 <= self.ttl_seconds <= 900 or
                not 1 <= self.max_snapshot_age_seconds <= 120):
            raise ExitError("invalid exit planner setting")


@dataclass(frozen=True)
class ExitPlan:
    token: str
    min_token_out: int
    min_quote_out: int
    min_eth_out: int
    min_q_out: int
    deadline: int
    simulated_new_x: int
    simulated_new_q: int
    harvested_x: int
    harvested_q: int
    total_x: int
    expected_eth: int
    expected_bought_q: int
    quote_boundary: bool

    def abi_config(self) -> tuple[int, int, int, int, int]:
        return (self.min_token_out, self.min_quote_out,
                self.min_eth_out, self.min_q_out, self.deadline)


def verify_bindings(rpc: watch.Rpc, chain_id: int, q: str, guard: str, quoter: str,
                    configurator: str | None) -> ExitBindings:
    bound = price.verify_bindings(rpc, chain_id, q, guard, quoter, None, None)
    if configurator is not None:
        owner = price.read_address(rpc, bound.q, OWNER)
        opening = price.read_address(rpc, bound.q, price.PRICE_CONFIGURATOR)
        exiting = price.read_address(rpc, bound.q, EXIT_CONFIGURATOR)
        if configurator != exiting or configurator in (owner, opening):
            raise ExitError("signing account is not an isolated Q.exitConfigurator")
    inspector = price.read_address(rpc, bound.q, EXIT_NOTIFIER)
    router = price.read_address(rpc, bound.executor, SETTLEMENT_ROUTER)
    active_sale = price.read_address(rpc, router, ACTIVE_SALE)
    for name, address in (("inspector", inspector), ("settlement router", router),
                          ("active sale adapter", active_sale)):
        code = rpc.call("eth_getCode", [address, "latest"])
        if not isinstance(code, str) or code == "0x":
            raise ExitError(f"{name} has no contract code")
    if (price.read_address(rpc, inspector, CURSOR) != bound.q or
            price.read_address(rpc, inspector, price.EXECUTOR) != bound.executor or
            price.read_address(rpc, bound.executor, CONTROLLER) != bound.q or
            price.read_address(rpc, router, SOURCE) != bound.executor or
            price.read_address(rpc, router, price.QUOTE_TOKEN) != bound.q):
        raise ExitError("exit inspector, executor, or router binding mismatch")
    return ExitBindings(bound, inspector, router, active_sale)


def q_launch(rpc: watch.Rpc, q: str, token: str) -> tuple[Any, ...]:
    result = price.call_abi(rpc, q, watch.LAUNCHES, ["address"], [token], LAUNCH_STATE_TYPES)
    if int(result[0]) > 5:
        raise ExitError("Q launch stage is invalid")
    return result


def q_next_action(rpc: watch.Rpc, q: str) -> tuple[str, int, int]:
    token, step, eligible_at = price.call_abi(rpc, q, NEXT_ACTION,
                                               outputs=["address", "uint8", "uint64"])
    if int(step) > 2:
        raise ExitError("Q nextAction returned an invalid step")
    return watch.address(token), int(step), int(eligible_at)


def confirmed_stale_exit(rpc: watch.Rpc, q: str, token: str,
                         confirmations: int, state: price.KeeperState | None = None) -> bool:
    """Only a finalized Exited/Aborted stage can authorize stale-root cleanup."""
    safe_head = watch.chain_head(rpc) - confirmations
    if safe_head < 0:
        return False
    anchored_hash = watch.block_hash(rpc, safe_head)
    if state is not None and (state.last_block != safe_head or state.last_hash != anchored_hash):
        raise ExitError("exit event cursor is not anchored at the confirmed head")
    confirmed = q_launch(price.PinnedRpc(rpc, safe_head), q, token)
    if watch.block_hash(rpc, safe_head) != anchored_hash:
        raise ExitError("confirmed Q stage changed during stale exit check")
    return int(confirmed[0]) in (3, 5)


def inspect(rpc: watch.Rpc, executor: str, token: str) -> tuple[int, bool, bool, bool, bool]:
    result = price.call_abi(rpc, executor, INSPECT, ["address"], [token],
                            ["uint160", "bool", "bool", "bool", "bool"])
    return (int(result[0]), bool(result[1]), bool(result[2]), bool(result[3]), bool(result[4]))


def needs_poke(launch: tuple[Any, ...], observed: tuple[int, bool, bool, bool, bool]) -> bool:
    if int(launch[0]) != 2:
        return False
    _, in_band, at_quote, at_token, was_entered = observed
    entered_reported = int(launch[4]) > 0
    if not entered_reported and (in_band or at_token):
        return True
    return (not bool(launch[7]) and (was_entered or entered_reported) and
            (at_quote or at_token))


def _simulate_withdraw(rpc: watch.Rpc, bindings: ExitBindings, token: str,
                       min_x: int, min_q: int, deadline: int) -> tuple[int, int]:
    data = WITHDRAW + encode(["address", "uint128", "uint128", "uint64"],
                             [token, min_x, min_q, deadline]).hex()
    raw = rpc.call("eth_call", [{"from": bindings.q, "to": bindings.executor,
                                 "data": data}, "latest"])
    try:
        new_x, new_q = decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    except (ValueError, TypeError, DecodingError, AttributeError) as exc:
        raise ExitError("withdrawPosition simulation returned malformed receipts") from exc
    return int(new_x), int(new_q)


def _min_receipt(amount: int, haircut_bps: int) -> int:
    return min(price.MAX_UINT128, max(1, amount * (10000 - haircut_bps) // 10000)) if amount else 0


def plan_exit(rpc: watch.Rpc, bindings: ExitBindings, token: str,
              settings: ExitSettings, quotes: price.ExecutableQuoteProvider) -> ExitPlan:
    settings.validate()
    token = watch.address(token)
    head = watch.chain_head(rpc)
    anchor_hash = watch.block_hash(rpc, head)
    snapshot = price.PinnedRpc(rpc, head)
    snapshot_quotes = quotes.at_block(head) if hasattr(quotes, "at_block") else quotes
    timestamp = price.latest_block_time(snapshot)
    launch_state = q_launch(snapshot, bindings.q, token)
    if int(launch_state[0]) != 2 or not launch_state[7] or int(launch_state[4]) == 0:
        raise WaitForExit("Q position is not active, entered, and exit-ready")
    _, _, at_quote, at_token, entered = inspect(snapshot, bindings.executor, token)
    if not entered or not (at_quote or at_token) or at_quote == at_token:
        raise WaitForExit("position is not at a live one-sided exit boundary")
    launch = price.read_launch(snapshot, token)
    if watch.address(launch[4]) != price.ZERO:
        raise WaitForExit("Pons launch is not paired with native ETH")
    phase = int(launch[10])
    deadline = timestamp + settings.ttl_seconds
    try:
        new_x, new_q = _simulate_withdraw(snapshot, bindings, token,
                                          0 if at_quote else 1,
                                          1 if at_quote else 0, deadline)
    except watch.WatcherError as exc:
        raise WaitForExit("withdrawPosition simulation failed") from exc
    harvested_x, harvested_q = (int(v) for v in price.call_abi(
        snapshot, bindings.executor, HARVESTED, ["address"], [token], ["uint256", "uint256"]))
    total_x = new_x + harvested_x
    if new_x == 0 and new_q == 0 or total_x == 0 and new_q + harvested_q == 0:
        raise WaitForExit("simulated exit has no settleable receipts")
    min_x = _min_receipt(new_x, settings.lp_slippage_bps)
    min_q = _min_receipt(new_q, settings.lp_slippage_bps)
    if at_quote and min_q == 0 or at_token and min_x == 0:
        raise WaitForExit("boundary asset has no simulated principal receipt")
    expected_eth = expected_bought_q = min_eth = min_bought_q = 0
    if total_x:
        if phase == 1:
            raise WaitForExit("Pons swept phase 1; retry after graduation")
        if phase not in (0, 2):
            raise WaitForExit("Pons launch has no supported sale phase")
        if total_x > price.MAX_UINT128:
            raise WaitForExit("total X receipt exceeds v4 quote input range")
        if phase == 0:
            curve, amount = price.call_abi(snapshot, bindings.active_sale, PREVIEW_ACTIVE_SALE,
                                           ["address", "uint256"], [token, total_x],
                                           ["address", "uint256"])
            if watch.address(curve) != watch.address(launch[1]):
                raise ExitError("active sale adapter returned another Pons curve")
            expected_eth = int(amount)
        else:
            pool_key = (price.ZERO, token, int(launch[6]), int(launch[7]),
                        bindings.price.pons_hook)
            expected_eth = snapshot_quotes.quote_single(pool_key, False, total_x)
        min_eth = expected_eth * (10000 - settings.swap_slippage_bps) // 10000
        if min_eth < 2 or min_eth > price.MAX_UINT128:
            raise WaitForExit("Pons sale has insufficient conservative ETH output")
        quote_key = (price.ZERO, bindings.q, bindings.price.quote_fee,
                     bindings.price.quote_spacing, price.ZERO)
        expected_bought_q = snapshot_quotes.quote_single(quote_key, True, min_eth // 2)
        min_bought_q = expected_bought_q * (10000 - settings.swap_slippage_bps) // 10000
        if min_bought_q == 0:
            raise WaitForExit("Q/ETH buy has insufficient conservative output")
    # The proposed LP minima must still pass the same pinned burn simulation.
    try:
        checked_x, checked_q = _simulate_withdraw(snapshot, bindings, token,
                                                   min_x, min_q, deadline)
    except watch.WatcherError as exc:
        raise WaitForExit("bounded withdrawPosition simulation failed") from exc
    if (checked_x, checked_q) != (new_x, new_q):
        raise ExitError("withdrawPosition receipts changed within pinned block")
    if watch.block_hash(rpc, head) != anchor_hash:
        raise WaitForExit("pinned exit block changed during planning")
    if price.latest_block_time(rpc) - timestamp > settings.max_snapshot_age_seconds:
        raise WaitForExit("pinned exit snapshot became stale")
    return ExitPlan(token, min_x, min_q, min_eth, min_bought_q, deadline,
                    new_x, new_q, harvested_x, harvested_q, total_x,
                    expected_eth, expected_bought_q, at_quote)


def configure_exit_data(plan: ExitPlan) -> str:
    return CONFIGURE_EXIT + encode(["address", "(uint128,uint128,uint256,uint256,uint64)"],
                                   [plan.token, plan.abi_config()]).hex()


def poke_data(token: str) -> str:
    return POKE + encode(["address"], [token]).hex()


def discover_active(rpc: watch.Rpc, state: price.KeeperState, store: price.KeeperStore,
                    confirmations: int, block_span: int) -> int:
    if watch.block_hash(rpc, state.last_block) != state.last_hash:
        raise ExitError("exit cursor block hash changed; reconcile reorg")
    safe_head = watch.chain_head(rpc) - confirmations
    found = 0
    active = set(state.tokens)
    while state.last_block < safe_head:
        first, last = state.last_block + 1, min(safe_head, state.last_block + block_span)
        expected_hash = watch.block_hash(rpc, last)
        logs = rpc.call("eth_getLogs", [{"address": state.q,
                                        "topics": [[STEP_SUCCEEDED, LAUNCH_ABORTED]],
                                        "fromBlock": hex(first), "toBlock": hex(last)}])
        if not isinstance(logs, list) or watch.block_hash(rpc, last) != expected_hash:
            raise ExitError("confirmed Q event range changed during backfill")
        for log in sorted(logs, key=watch.log_order):
            if (not isinstance(log, dict) or str(log.get("address", "")).lower() != state.q or
                    log.get("removed") or not isinstance(log.get("topics"), list) or
                    len(log["topics"]) != 2 or
                    watch.quantity(log.get("blockNumber"), "log block") < first or
                    watch.quantity(log.get("blockNumber"), "log block") > last):
                raise ExitError("malformed or out-of-range Q event")
            block = watch.quantity(log["blockNumber"], "log block")
            if str(log.get("blockHash", "")).lower() != watch.block_hash(rpc, block):
                raise ExitError("Q event block is not canonical")
            topic = str(log["topics"][0]).lower()
            if topic not in (STEP_SUCCEEDED, LAUNCH_ABORTED):
                raise ExitError("unexpected Q event topic")
            word = str(log["topics"][1])
            if not watch.HASH.fullmatch(word) or int(word[2:26], 16):
                raise ExitError("malformed indexed Q event token")
            token = watch.address("0x" + word[-40:])
            if topic == STEP_SUCCEEDED:
                try:
                    step = decode(["uint8"], bytes.fromhex(str(log.get("data", ""))[2:]))[0]
                except (ValueError, TypeError, DecodingError) as exc:
                    raise ExitError("malformed Q StepSucceeded data") from exc
                if step == 0 and token not in active:
                    state.tokens.append(token)
                    active.add(token)
                    found += 1
                elif step == 1 and token in active:
                    state.tokens.remove(token)
                    active.remove(token)
                elif step not in (0, 1, 2):
                    raise ExitError("unknown Q step in event")
            elif token in active:
                state.tokens.remove(token)
                active.remove(token)
        if watch.block_hash(rpc, last) != expected_hash:
            raise ExitError("confirmed Q event range reorged during processing")
        state.last_block, state.last_hash = last, expected_hash
        store.save(state)
    return found


class ExitSigner:
    """Persist, validate, and recover only the three exit-loop selectors."""

    def __init__(self, rpc: watch.Rpc, bindings: ExitBindings, chain_id: int,
                 private_key: str, confirmations: int, max_poke_gas: int,
                 max_config_gas: int, max_process_gas: int, max_fee_wei: int,
                 max_priority_wei: int, receipt_timeout: int, poll_seconds: float):
        self.account = Account.from_key(private_key)
        self.rpc, self.bindings, self.chain_id = rpc, bindings, chain_id
        self.confirmations = confirmations
        self.gas_caps = {"poke": max_poke_gas, "configure": max_config_gas,
                         "process": max_process_gas}
        self.max_fee_wei, self.max_priority_wei = max_fee_wei, max_priority_wei
        self.receipt_timeout, self.poll_seconds = receipt_timeout, poll_seconds

    @property
    def signer(self) -> str:
        return self.account.address.lower()

    def _target(self, kind: str) -> str:
        return self.bindings.inspector if kind == "poke" else self.bindings.q

    @staticmethod
    def _check_payload(kind: str, token: str, data: str) -> None:
        if kind == "process":
            if data != PROCESS_NEXT:
                raise ExitError("processNext payload must have no arguments")
            return
        if kind not in ("poke", "configure") or not isinstance(data, str) or not data.startswith(
                POKE if kind == "poke" else CONFIGURE_EXIT):
            raise ExitError("attempted non-exit transaction")
        payload_type = (["address"] if kind == "poke" else
                        ["address", "(uint128,uint128,uint256,uint256,uint64)"])
        if len(data) != 10 + 64 * (1 if kind == "poke" else 6):
            raise ExitError("exit transaction payload length is invalid")
        try:
            decoded = decode(payload_type, bytes.fromhex(data[10:]))
        except (ValueError, TypeError, DecodingError) as exc:
            raise ExitError("exit transaction payload is undecodable") from exc
        if watch.address(decoded[0]) != token:
            raise ExitError("exit transaction token mismatch")
        if kind == "configure":
            min_x, min_q, min_eth, min_bought_q, deadline = decoded[1]
            if (not (min_x or min_q) or (bool(min_eth) != bool(min_bought_q)) or
                    deadline == 0):
                raise ExitError("configureExit minima are invalid")

    def _wait_receipt(self, tx_hash: str) -> None:
        deadline = time.monotonic() + self.receipt_timeout
        while time.monotonic() < deadline:
            if price.confirmed_receipt(self.rpc, tx_hash, self.confirmations):
                return
            time.sleep(self.poll_seconds)
        raise ExitError("exit transaction remains unconfirmed in .local state")

    def submit(self, kind: str, token: str, data: str, state: price.KeeperState,
               store: price.KeeperStore, purpose: str | None = None) -> None:
        token = watch.address(token)
        self._check_payload(kind, token, data)
        if (kind == "process" and purpose not in ("configured_exit", "stale_cleanup")) or \
                (kind != "process" and purpose is not None):
            raise ExitError("exit transaction purpose is invalid")
        if purpose == "stale_cleanup":
            selected_token, step, eligible_at = q_next_action(self.rpc, self.bindings.q)
            if (selected_token != token or step != 1 or
                    eligible_at > price.latest_block_time(self.rpc) or
                    not confirmed_stale_exit(self.rpc, self.bindings.q, token,
                                             self.confirmations)):
                raise WaitForExit("stale exit is no longer the confirmed Q root")
        target = self._target(kind)
        try:
            estimate = watch.quantity(self.rpc.call("eth_estimateGas", [{"from": self.signer,
                                  "to": target, "data": data}]), "gas estimate")
        except watch.WatcherError as exc:
            raise WaitForExit(f"{kind} gas simulation failed") from exc
        gas = price.ceil_div(estimate * 120, 100)
        if gas > self.gas_caps[kind]:
            raise WaitForExit(f"{kind} gas estimate exceeds configured cap")
        nonce = watch.quantity(self.rpc.call("eth_getTransactionCount", [self.signer, "pending"]),
                               "nonce")
        latest = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(latest, dict) or "baseFeePerGas" not in latest:
            raise ExitError("RPC omitted EIP-1559 base fee")
        base_fee = watch.quantity(latest["baseFeePerGas"], "base fee")
        priority = min(watch.quantity(self.rpc.call("eth_maxPriorityFeePerGas", []),
                                      "priority fee"), self.max_priority_wei)
        max_fee = base_fee * 2 + priority
        if max_fee > self.max_fee_wei:
            raise WaitForExit("exit transaction fee exceeds configured cap")
        signed = self.account.sign_transaction({
            "chainId": self.chain_id, "nonce": nonce, "to": target, "value": 0,
            "data": data, "gas": gas, "type": 2, "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority})
        tx_hash = "0x" + signed.hash.hex().lower().removeprefix("0x")
        raw = "0x" + signed.raw_transaction.hex().removeprefix("0x")
        state.pending_tx = {"kind": kind, "token": token, "data": data,
                            "nonce": nonce, "txHash": tx_hash, "rawTx": raw,
                            "purpose": purpose}
        store.save(state)
        sent = self.rpc.call("eth_sendRawTransaction", [raw])
        if not isinstance(sent, str) or sent.lower() != tx_hash:
            raise ExitError("RPC returned an unexpected exit transaction hash")
        try:
            self._wait_receipt(tx_hash)
        except price.TransactionReverted:
            state.pending_tx = None
            store.save(state)
            raise WaitForExit(f"{kind} reverted; active position remains pending")
        state.pending_tx = None
        store.save(state)

    def _validated_pending(self, pending: dict[str, Any], state: price.KeeperState
                           ) -> tuple[str, str, str, int]:
        kind, token, data, nonce, tx_hash, raw_hex = (
            pending.get("kind"), pending.get("token"), pending.get("data"),
            pending.get("nonce"), pending.get("txHash"), pending.get("rawTx"))
        purpose = pending.get("purpose")
        if (kind not in self.gas_caps or not isinstance(token, str) or
                not watch.ADDRESS.fullmatch(token) or not isinstance(data, str) or
                not isinstance(nonce, int) or nonce < 0 or not isinstance(tx_hash, str) or
                not watch.HASH.fullmatch(tx_hash) or not isinstance(raw_hex, str) or
                not raw_hex.startswith("0x") or len(raw_hex) > 4098 or len(raw_hex) % 2):
            raise ExitError("pending exit transaction is malformed")
        if (kind == "process" and purpose not in ("configured_exit", "stale_cleanup")) or \
                (kind != "process" and purpose is not None):
            raise ExitError("pending exit transaction purpose is invalid")
        self._check_payload(kind, token, data)
        try:
            raw = bytes.fromhex(raw_hex[2:])
            fields = rlp.decode(raw[1:])
            sender = Account.recover_transaction(raw_hex).lower()
        except Exception as exc:
            raise ExitError("pending signed exit transaction cannot be decoded") from exc
        if raw[:1] != b"\x02" or not isinstance(fields, list) or len(fields) != 12:
            raise ExitError("pending exit transaction is not signed EIP-1559")
        number = lambda item: int.from_bytes(item, "big")
        if "0x" + keccak(raw).hex() != tx_hash.lower():
            raise ExitError("pending exit transaction hash mismatch")
        if (number(fields[0]) != state.chain_id or number(fields[1]) != nonce or
                number(fields[2]) > self.max_priority_wei or
                number(fields[3]) > self.max_fee_wei or
                not 21000 <= number(fields[4]) <= self.gas_caps[kind] or
                fields[5] != bytes.fromhex(self._target(kind)[2:]) or
                number(fields[6]) != 0 or fields[7] != bytes.fromhex(data[2:]) or
                fields[8] != [] or sender != self.signer):
            raise ExitError("pending signed transaction exceeds exit permissions or caps")
        return kind, token, tx_hash, nonce

    def _unknown_safe(self, kind: str, token: str, data: str, purpose: str | None,
                      settings: ExitSettings, quotes: price.ExecutableQuoteProvider) -> bool:
        launch = q_launch(self.rpc, self.bindings.q, token)
        if kind == "process":
            selected_token, step, eligible_at = q_next_action(self.rpc, self.bindings.q)
            if (selected_token != token or step != 1 or
                    eligible_at > price.latest_block_time(self.rpc)):
                return False
            if purpose == "stale_cleanup":
                return (int(launch[0]) in (3, 5) and
                        confirmed_stale_exit(self.rpc, self.bindings.q, token,
                                             self.confirmations))
            if purpose != "configured_exit" or int(launch[0]) != 2 or not launch[7]:
                return False
            try:
                config = price.call_abi(self.rpc, self.bindings.executor, EXIT_CONFIGS,
                                        ["address"], [token], EXIT_CONFIG_TYPES)
            except (watch.WatcherError, price.KeeperError):
                return False
            now = price.latest_block_time(self.rpc)
            return (bool(int(config[0]) or int(config[1])) and
                    bool(config[2]) == bool(config[3]) and
                    int(config[4]) >= now + 30)
        if int(launch[0]) != 2:
            return False
        if kind == "poke":
            return needs_poke(launch, inspect(self.rpc, self.bindings.executor, token))
        if not launch[7]:
            return False
        fresh = plan_exit(self.rpc, self.bindings, token, settings, quotes)
        _, old = decode(["address", "(uint128,uint128,uint256,uint256,uint64)"],
                        bytes.fromhex(data[10:]))
        old_x, old_q, old_eth, old_bought_q, deadline = (int(v) for v in old)
        now = price.latest_block_time(self.rpc)
        if not now + 30 <= deadline <= now + 900:
            return False
        if (old_x < fresh.min_token_out or old_q < fresh.min_quote_out or
                old_x > fresh.simulated_new_x or old_q > fresh.simulated_new_q):
            return False
        if fresh.total_x == 0:
            return old_eth == old_bought_q == 0
        return (fresh.min_eth_out <= old_eth <= fresh.expected_eth and
                fresh.min_q_out <= old_bought_q <= fresh.expected_bought_q)

    def recover(self, state: price.KeeperState, store: price.KeeperStore,
                settings: ExitSettings, quotes: price.ExecutableQuoteProvider) -> None:
        if state.pending_tx is None:
            return
        pending = state.pending_tx
        kind, token, tx_hash, nonce = self._validated_pending(pending, state)
        try:
            confirmed = price.confirmed_receipt(self.rpc, tx_hash, self.confirmations)
        except price.TransactionReverted:
            state.pending_tx = None
            store.save(state)
            return
        if not confirmed:
            receipt = self.rpc.call("eth_getTransactionReceipt", [tx_hash])
            known = self.rpc.call("eth_getTransactionByHash", [tx_hash]) if receipt is None else receipt
            if known is None:
                latest_nonce = watch.quantity(self.rpc.call("eth_getTransactionCount",
                                                            [self.signer, "latest"]), "nonce")
                if latest_nonce > nonce:
                    raise ExitError("exit signer nonce consumed without matching receipt")
                if not self._unknown_safe(kind, token, pending["data"],
                                          pending.get("purpose"), settings, quotes):
                    raise ExitError("unknown pending exit transaction is stale; inspect nonce")
                try:
                    sent = self.rpc.call("eth_sendRawTransaction", [pending["rawTx"]])
                except watch.WatcherError:
                    if (self.rpc.call("eth_getTransactionByHash", [tx_hash]) is None and
                            self.rpc.call("eth_getTransactionReceipt", [tx_hash]) is None):
                        raise
                    sent = tx_hash
                if not isinstance(sent, str) or sent.lower() != tx_hash:
                    raise ExitError("pending exit rebroadcast returned another hash")
            try:
                self._wait_receipt(tx_hash)
            except price.TransactionReverted:
                state.pending_tx = None
                store.save(state)
                return
        state.pending_tx = None
        store.save(state)


def run_cycle(rpc: watch.Rpc, bindings: ExitBindings, state: price.KeeperState,
              store: price.KeeperStore, settings: ExitSettings,
              quotes: price.ExecutableQuoteProvider, signer: ExitSigner | None,
              confirmations: int, block_span: int, process_next: bool) -> dict[str, Any]:
    found = discover_active(rpc, state, store, confirmations, block_span)
    waiting: dict[str, int] = {}
    selected: tuple[str, int, int] | None = None
    # Check every confirmed active position; a boundary can arrive long after
    # its open event, and a phase-1 sale becomes tradeable after graduation.
    for token in state.tokens[:]:
        launch = q_launch(rpc, bindings.q, token)
        stage = int(launch[0])
        if stage != 2:
            # Discovery alone may remove a token, after a confirmed Exit or
            # Abort event. A latest-stage close can still reorg away.
            waiting["await confirmed Q close"] = waiting.get("await confirmed Q close", 0) + 1
            continue
        try:
            observed = inspect(rpc, bindings.executor, token)
        except (watch.WatcherError, price.KeeperError) as exc:
            raise WaitForExit("active position inspection unavailable") from exc
        if needs_poke(launch, observed):
            if signer is None:
                return {"status": "poke_ready_read_only", "token": token,
                        "found": found, "active": len(state.tokens)}
            try:
                signer.submit("poke", token, poke_data(token), state, store)
            except WaitForExit as exc:
                waiting[str(exc)] = waiting.get(str(exc), 0) + 1
                continue
            return {"status": "poked", "token": token,
                    "found": found, "active": len(state.tokens)}
        if not launch[7]:
            continue
        if selected is None:
            selected = q_next_action(rpc, bindings.q)
        if selected[0] != token or selected[1] != 1:
            continue
        if selected[2] > price.latest_block_time(rpc):
            waiting["Q exit retry delay"] = waiting.get("Q exit retry delay", 0) + 1
            continue
        if int(launch[5]) > price.latest_block_time(rpc):
            waiting["Q exit retry delay"] = waiting.get("Q exit retry delay", 0) + 1
            continue
        try:
            plan = plan_exit(rpc, bindings, token, settings, quotes)
        except (WaitForExit, price.WaitForPrice) as exc:
            waiting[str(exc)] = waiting.get(str(exc), 0) + 1
            continue
        if signer is None:
            return {"status": "planned_read_only", "plan": asdict(plan),
                    "found": found, "active": len(state.tokens), "waiting": waiting}
        latest = q_launch(rpc, bindings.q, token)
        if int(latest[0]) != 2 or not latest[7]:
            waiting["Q exit state changed during plan"] = 1
            continue
        try:
            signer.submit("configure", token, configure_exit_data(plan), state, store)
        except WaitForExit as exc:
            waiting[str(exc)] = waiting.get(str(exc), 0) + 1
            continue
        processed = False
        next_token, next_step, eligible_at = q_next_action(rpc, bindings.q)
        if (process_next and int(q_launch(rpc, bindings.q, token)[0]) == 2 and
                next_token == token and next_step == 1 and
                eligible_at <= price.latest_block_time(rpc)):
            try:
                signer.submit("process", token, PROCESS_NEXT, state, store,
                              purpose="configured_exit")
                processed = True
            except WaitForExit as exc:
                waiting[str(exc)] = waiting.get(str(exc), 0) + 1
        return {"status": "configured_or_armed", "token": token,
                "processNextSent": processed, "found": found,
                "active": len(state.tokens), "waiting": waiting}
    # Owner abort leaves a stale exit heap root. Only clear it after the
    # closed/aborted stage is part of our confirmed event cursor.
    if process_next:
        action_token, action_step, eligible_at = q_next_action(rpc, bindings.q)
        if (action_token != price.ZERO and action_step == 1 and
                eligible_at <= price.latest_block_time(rpc) and
                confirmed_stale_exit(rpc, bindings.q, action_token,
                                     confirmations, state)):
            if signer is None:
                return {"status": "stale_exit_cleanup_read_only", "token": action_token,
                        "found": found, "active": len(state.tokens), "waiting": waiting}
            fresh_token, fresh_step, fresh_at = q_next_action(rpc, bindings.q)
            if (fresh_token, fresh_step) != (action_token, 1) or \
                    fresh_at > price.latest_block_time(rpc):
                waiting["stale exit selection changed"] = 1
            else:
                try:
                    signer.submit("process", action_token, PROCESS_NEXT, state, store,
                                  purpose="stale_cleanup")
                except WaitForExit as exc:
                    waiting[str(exc)] = waiting.get(str(exc), 0) + 1
                else:
                    return {"status": "stale_exit_cleared", "token": action_token,
                            "found": found, "active": len(state.tokens), "waiting": waiting}
    return {"status": "waiting", "found": found,
            "active": len(state.tokens), "waiting": waiting}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default=os.environ.get("PONS_HTTP_RPC_URL"))
    parser.add_argument("--chain-id", type=int, default=os.environ.get("PONS_CHAIN_ID"))
    parser.add_argument("--q", default=os.environ.get("PONS_Q_ADDRESS"))
    parser.add_argument("--guard", default=os.environ.get("PONS_PRICE_GUARD_ADDRESS"))
    parser.add_argument("--quoter", default=price.QUOTER)
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--confirmations", type=int, default=3)
    parser.add_argument("--block-span", type=int, default=2000)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--lp-slippage-bps", type=int, default=1000)
    parser.add_argument("--swap-slippage-bps", type=int, default=1500)
    parser.add_argument("--ttl-seconds", type=int, default=120)
    parser.add_argument("--max-snapshot-age-seconds", type=int, default=20)
    parser.add_argument("--max-poke-gas", type=int, default=500000)
    parser.add_argument("--max-config-gas", type=int, default=500000)
    parser.add_argument("--max-process-gas", type=int, default=3500000)
    parser.add_argument("--max-fee-gwei", default="5")
    parser.add_argument("--max-priority-gwei", default="1")
    parser.add_argument("--receipt-timeout", type=int, default=180)
    parser.add_argument("--no-process-next", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.live and not args.once:
        parser.error("--live requires --once; run via the keeper supervisor")
    if not args.http_url or not args.chain_id or not args.q or not args.guard:
        parser.error("HTTP URL, chain ID, Q, and guard are required")
    if (args.confirmations < 1 or args.block_span < 1 or args.poll_seconds <= 0 or
            args.max_poke_gas < 21000 or args.max_config_gas < 21000 or
            args.max_process_gas < 21000 or args.receipt_timeout < 1):
        parser.error("invalid confirmation, span, poll, gas, or receipt setting")
    settings = ExitSettings(args.lp_slippage_bps, args.swap_slippage_bps,
                            args.ttl_seconds, args.max_snapshot_age_seconds)
    settings.validate()
    q, guard = watch.address(args.q), watch.address(args.guard)
    rpc = watch.HttpRpc(args.http_url)
    private_key = os.environ.get("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY") if args.live else None
    if args.live and not private_key:
        raise ExitError("--live requires PONS_EXIT_CONFIGURATOR_PRIVATE_KEY")
    try:
        signer_address = Account.from_key(private_key).address.lower() if private_key else None
    except Exception as exc:
        raise ExitError("exitConfigurator private key is invalid") from exc
    bindings = verify_bindings(rpc, args.chain_id, q, guard, args.quoter, signer_address)
    quotes = price.ExecutableQuoteProvider(rpc, bindings.price)
    fee_cap, priority_cap = watch.gwei(args.max_fee_gwei), watch.gwei(args.max_priority_gwei)
    if priority_cap > fee_cap:
        raise ExitError("priority fee cap exceeds max fee cap")
    signer = (ExitSigner(rpc, bindings, args.chain_id, private_key, args.confirmations,
                         args.max_poke_gas, args.max_config_gas, args.max_process_gas,
                         fee_cap, priority_cap, args.receipt_timeout, args.poll_seconds)
              if private_key else None)
    with (ExitConfiguratorLock(q, args.state) if signer else nullcontext()), \
            price.KeeperStore(args.state) as store:
        state = store.load(rpc, args.chain_id, q, guard, args.start_block)
        if state.pending_tx is not None:
            if signer is None:
                raise ExitError("read-only mode cannot recover an outstanding signed transaction")
            signer.recover(state, store, settings, quotes)
        while True:
            try:
                result = run_cycle(rpc, bindings, state, store, settings, quotes,
                                   signer, args.confirmations, args.block_span,
                                   not args.no_process_next)
            except watch.WatcherError as exc:
                if args.once:
                    raise
                print(json.dumps({"status": "rpc_retry", "reason": str(exc)}), flush=True)
                time.sleep(args.poll_seconds)
                continue
            print(json.dumps(result, separators=(",", ":")), flush=True)
            if args.once:
                return 0
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ExitError, price.KeeperError, watch.WatcherError) as exc:
        print(f"exit keeper stopped: {exc}", file=sys.stderr)
        sys.exit(1)
