#!/usr/bin/env python3
"""Plan and submit bounded interim X/Q fee claims using the exit signer lane.

This keeper shares the exit keeper's state journal and configurator lock. It
never sends a transaction without --live --once, and never reads the key in
read-only mode.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_account import Account

import pons_launch_watcher as watch
import pons_price_keeper as price
import pons_exit_keeper as exit_keeper


DEFAULT_STATE = exit_keeper.DEFAULT_STATE
CONFIGURE_HARVEST = price.selector(
    "configureHarvest(address,(uint128,uint128,uint256,uint256,uint256,uint32,uint64))")
POKE_HARVEST = price.selector("pokeHarvest(address)")
SIMULATE_HARVEST = price.selector("simulateHarvest(address)")
HARVEST_CONFIGS = price.selector("harvestConfigs(address)")
PREVIEW_HARVEST = price.selector("previewHarvest(address)")
HARVEST_TYPES = ["uint128", "uint128", "uint256", "uint256", "uint256", "uint32", "uint64"]
HARVEST_TUPLE = "(uint128,uint128,uint256,uint256,uint256,uint32,uint64)"


class HarvestError(exit_keeper.ExitError):
    pass


class WaitForHarvest(HarvestError):
    pass


@dataclass(frozen=True)
class HarvestSettings:
    lp_slippage_bps: int = 1000
    swap_slippage_bps: int = 1500
    ttl_seconds: int = 120
    max_snapshot_age_seconds: int = 20
    whole_cycle_gas_units: int = 5_000_000
    max_fee_wei: int = 5_000_000_000

    def validate(self) -> None:
        if (not 1 <= self.lp_slippage_bps < 10000 or
                not 1 <= self.swap_slippage_bps < 10000 or
                not 30 <= self.ttl_seconds <= 900 or
                not 1 <= self.max_snapshot_age_seconds <= 120 or
                not 21_000 <= self.whole_cycle_gas_units <= 0xffffffff or
                self.max_fee_wei <= 0):
            raise HarvestError("invalid harvest planner setting")


@dataclass(frozen=True)
class HarvestPlan:
    token: str
    min_token_fee: int
    min_quote_fee: int
    min_eth_out: int
    min_q_out: int
    gross_eth_value: int
    estimated_gas_units: int
    deadline: int
    simulated_x_fee: int
    simulated_q_fee: int
    expected_x_eth: int
    expected_q_eth: int
    expected_bought_q: int

    def abi_config(self) -> tuple[int, int, int, int, int, int, int]:
        return (self.min_token_fee, self.min_quote_fee, self.min_eth_out,
                self.min_q_out, self.gross_eth_value,
                self.estimated_gas_units, self.deadline)


def is_harvest_pending(pending: dict[str, Any]) -> bool:
    data = pending.get("data")
    return (pending.get("purpose") == "configured_harvest" or
            isinstance(data, str) and data[:10] in (CONFIGURE_HARVEST, POKE_HARVEST))


def simulate_harvest(rpc: watch.Rpc, bindings: exit_keeper.ExitBindings,
                     token: str) -> tuple[int, int]:
    data = SIMULATE_HARVEST + encode(["address"], [token]).hex()
    raw = rpc.call("eth_call", [{"from": bindings.q, "to": bindings.executor,
                                 "data": data}, "latest"])
    try:
        x_fee, q_fee = decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    except (ValueError, TypeError, DecodingError, AttributeError) as exc:
        raise HarvestError("simulateHarvest returned malformed fees") from exc
    return int(x_fee), int(q_fee)


def _min_fee(amount: int, slippage_bps: int) -> int:
    return max(1, amount * (10000 - slippage_bps) // 10000) if amount else 0


def plan_harvest(rpc: watch.Rpc, bindings: exit_keeper.ExitBindings,
                 token: str, settings: HarvestSettings,
                 quotes: price.ExecutableQuoteProvider) -> HarvestPlan:
    """Quote collectible fees and all three signer transactions at one block."""
    settings.validate()
    token = watch.address(token)
    head = watch.chain_head(rpc)
    anchor_hash = watch.block_hash(rpc, head)
    snapshot = price.PinnedRpc(rpc, head)
    snapshot_quotes = quotes.at_block(head) if hasattr(quotes, "at_block") else quotes
    timestamp = price.latest_block_time(snapshot)
    launch_state = exit_keeper.q_launch(snapshot, bindings.q, token)
    if int(launch_state[0]) != 2:
        raise WaitForHarvest("Q position is not active")
    x_fee, q_fee = simulate_harvest(snapshot, bindings, token)
    if x_fee == 0 and q_fee == 0:
        raise WaitForHarvest("no claimable X or Q fees")
    if x_fee > price.MAX_UINT128 or q_fee > price.MAX_UINT128:
        raise WaitForHarvest("fee amount exceeds executable quote range")
    launch = price.read_launch(snapshot, token)
    if watch.address(launch[4]) != price.ZERO:
        raise WaitForHarvest("Pons launch is not ETH paired")
    phase = int(launch[10])
    expected_x_eth = expected_q_eth = expected_bought_q = 0
    min_x = min_eth = min_bought_q = 0
    if x_fee and phase in (0, 2):
        try:
            if phase == 0:
                curve, quoted = price.call_abi(
                    snapshot, bindings.active_sale, exit_keeper.PREVIEW_ACTIVE_SALE,
                    ["address", "uint256"], [token, x_fee], ["address", "uint256"])
                if watch.address(curve) != watch.address(launch[1]):
                    raise HarvestError("active sale adapter returned another Pons curve")
                expected_x_eth = int(quoted)
            else:
                key = (price.ZERO, token, int(launch[6]), int(launch[7]),
                       bindings.price.pons_hook)
                expected_x_eth = snapshot_quotes.quote_single(key, False, x_fee)
        except (watch.WatcherError, price.WaitForPrice):
            expected_x_eth = 0
        if expected_x_eth:
            min_x = _min_fee(x_fee, settings.lp_slippage_bps)
            min_eth = expected_x_eth * (10000 - settings.swap_slippage_bps) // 10000
            if 2 <= min_eth <= price.MAX_UINT128:
                q_key = (price.ZERO, bindings.q, bindings.price.quote_fee,
                         bindings.price.quote_spacing, price.ZERO)
                expected_bought_q = snapshot_quotes.quote_single(q_key, True, min_eth // 2)
                min_bought_q = (expected_bought_q *
                                (10000 - settings.swap_slippage_bps) // 10000)
            if min_bought_q == 0:
                min_x = min_eth = 0
    if q_fee:
        q_key = (price.ZERO, bindings.q, bindings.price.quote_fee,
                 bindings.price.quote_spacing, price.ZERO)
        expected_q_eth = snapshot_quotes.quote_single(q_key, False, q_fee)
    min_q_fee = _min_fee(q_fee, settings.lp_slippage_bps)
    gas_cost_ceiling = settings.whole_cycle_gas_units * settings.max_fee_wei
    # An X sale pays ETH cash; a Q-only burn supplies market value but no
    # signer cash reimbursement. If the X route cannot cover the whole cycle,
    # retain collected X and claim Q only when its pinned executable quote
    # clears the same twofold gas margin.
    x_cash_positive = min_eth > 2 * gas_cost_ceiling
    if not x_cash_positive:
        min_x = min_eth = min_bought_q = 0
        if min_q_fee == 0 or expected_q_eth <= 2 * gas_cost_ceiling:
            raise WaitForHarvest("X cash and Q mark-to-market value do not cover whole-cycle gas")
    gross_eth_value = (expected_x_eth if x_cash_positive else 0) + expected_q_eth
    if gross_eth_value <= 2 * gas_cost_ceiling:
        raise WaitForHarvest("gross fee value does not clear Q gas gate")
    if watch.block_hash(rpc, head) != anchor_hash:
        raise WaitForHarvest("pinned harvest block changed during planning")
    if price.latest_block_time(rpc) - timestamp > settings.max_snapshot_age_seconds:
        raise WaitForHarvest("pinned harvest snapshot became stale")
    return HarvestPlan(token, min_x, min_q_fee, min_eth, min_bought_q,
                       gross_eth_value, settings.whole_cycle_gas_units,
                       timestamp + settings.ttl_seconds, x_fee, q_fee,
                       expected_x_eth, expected_q_eth, expected_bought_q)


def configure_harvest_data(plan: HarvestPlan) -> str:
    return CONFIGURE_HARVEST + encode(["address", HARVEST_TUPLE],
                                      [plan.token, plan.abi_config()]).hex()


def poke_harvest_data(token: str) -> str:
    return POKE_HARVEST + encode(["address"], [watch.address(token)]).hex()


def configured_harvest(rpc: watch.Rpc, bindings: exit_keeper.ExitBindings,
                       token: str, now: int) -> bool:
    config = harvest_config(rpc, bindings, token)
    return (int(config[4]) > 0 and int(config[5]) > 0 and
            int(config[6]) >= now + 30)


def harvest_config(rpc: watch.Rpc, bindings: exit_keeper.ExitBindings,
                   token: str) -> tuple[Any, ...]:
    return price.call_abi(rpc, bindings.executor, HARVEST_CONFIGS,
                          ["address"], [token], HARVEST_TYPES)


class HarvestSigner(exit_keeper.ExitSigner):
    """Use the exit signer journal, nonce lock, and fee caps for fee claims."""

    @staticmethod
    def _check_payload(kind: str, token: str, data: str) -> None:
        if kind == "process":
            if data != exit_keeper.PROCESS_NEXT:
                raise HarvestError("processNext payload must have no arguments")
            return
        selector = POKE_HARVEST if kind == "poke" else CONFIGURE_HARVEST
        if kind not in ("poke", "configure") or not isinstance(data, str) or \
                not data.startswith(selector):
            raise HarvestError("attempted non-harvest transaction")
        words = 1 if kind == "poke" else 8
        if len(data) != 10 + 64 * words:
            raise HarvestError("harvest transaction payload length is invalid")
        try:
            values = decode(["address"] if kind == "poke" else
                            ["address", HARVEST_TUPLE], bytes.fromhex(data[10:]))
        except (ValueError, TypeError, DecodingError) as exc:
            raise HarvestError("harvest transaction payload is undecodable") from exc
        if watch.address(values[0]) != token:
            raise HarvestError("harvest transaction token mismatch")
        if kind == "configure":
            min_x, min_q, min_eth, min_bought_q, gross, gas, deadline = values[1]
            if (not (min_x or min_q) or not gross or not gas or not deadline or
                    bool(min_x) != bool(min_eth) or bool(min_x) != bool(min_bought_q)):
                raise HarvestError("configureHarvest bounds are invalid")

    def _unknown_safe(self, kind: str, token: str, data: str, purpose: str | None,
                      settings: exit_keeper.ExitSettings,
                      quotes: price.ExecutableQuoteProvider) -> bool:
        launch = exit_keeper.q_launch(self.rpc, self.bindings.q, token)
        if int(launch[0]) != 2:
            return False
        now = price.latest_block_time(self.rpc)
        if kind == "process":
            selected, step, eligible = exit_keeper.q_next_action(self.rpc, self.bindings.q)
            return (purpose == "configured_harvest" and selected == token and step == 2 and
                    eligible <= now and configured_harvest(self.rpc, self.bindings, token, now))
        if kind == "poke":
            try:
                gross, gas = price.call_abi(self.rpc, self.bindings.executor, PREVIEW_HARVEST,
                                            ["address"], [token], ["uint256", "uint256"])
            except (watch.WatcherError, price.KeeperError):
                return False
            return int(gross) > 0 and int(gas) > 0
        try:
            _, config = decode(["address", HARVEST_TUPLE], bytes.fromhex(data[10:]))
            x_fee, q_fee = simulate_harvest(self.rpc, self.bindings, token)
        except (watch.WatcherError, HarvestError, ValueError, DecodingError):
            return False
        min_x, min_q, min_eth, min_bought_q, gross, gas, deadline = (int(v) for v in config)
        return (now + 30 <= deadline <= now + 900 and
                x_fee >= min_x and q_fee >= min_q and
                gross > 2 * gas * self.max_fee_wei and
                bool(min_x) == bool(min_eth) == bool(min_bought_q))


def run_cycle(rpc: watch.Rpc, bindings: exit_keeper.ExitBindings,
              state: price.KeeperState, store: price.KeeperStore,
              settings: HarvestSettings, quotes: price.ExecutableQuoteProvider,
              signer: HarvestSigner | None, confirmations: int,
              block_span: int, process_next: bool) -> dict[str, Any]:
    found = exit_keeper.discover_active(rpc, state, store, confirmations, block_span)
    waiting: dict[str, int] = {}
    for token in state.tokens[:]:
        launch = exit_keeper.q_launch(rpc, bindings.q, token)
        if int(launch[0]) != 2:
            continue
        now = price.latest_block_time(rpc)
        selected, step, eligible = exit_keeper.q_next_action(rpc, bindings.q)
        if bool(launch[11]) and configured_harvest(rpc, bindings, token, now):
            if selected == token and step == 2 and eligible <= now:
                # A Q-only burn has mark-to-market value but sends no ETH to
                # repay the signer. Requote immediately before processing.
                configured = harvest_config(rpc, bindings, token)
                if int(configured[0]) == 0:
                    try:
                        fresh = plan_harvest(rpc, bindings, token, settings, quotes)
                    except (WaitForHarvest, price.WaitForPrice, watch.WatcherError) as exc:
                        waiting[str(exc)] = waiting.get(str(exc), 0) + 1
                        continue
                    gas_ceiling = settings.whole_cycle_gas_units * settings.max_fee_wei
                    if (fresh.simulated_q_fee < int(configured[1]) or
                            fresh.expected_q_eth <= 2 * gas_ceiling):
                        waiting["Q-only market value fell below gas margin"] = waiting.get(
                            "Q-only market value fell below gas margin", 0) + 1
                        continue
                if signer is None or not process_next:
                    return {"status": "harvest_ready_read_only" if signer is None else "harvest_ready",
                            "token": token, "found": found, "active": len(state.tokens)}
                try:
                    signer.submit("process", token, exit_keeper.PROCESS_NEXT, state, store,
                                  purpose="configured_harvest")
                except exit_keeper.WaitForExit as exc:
                    waiting[str(exc)] = waiting.get(str(exc), 0) + 1
                    continue
                return {"status": "harvest_processed", "token": token,
                        "found": found, "active": len(state.tokens)}
            waiting["configured harvest awaits Q scheduler"] = waiting.get(
                "configured harvest awaits Q scheduler", 0) + 1
            continue
        if step == 1 and selected == token and eligible <= now:
            waiting["eligible exit has priority"] = waiting.get("eligible exit has priority", 0) + 1
            continue
        try:
            plan = plan_harvest(rpc, bindings, token, settings, quotes)
        except (WaitForHarvest, price.WaitForPrice, watch.WatcherError) as exc:
            waiting[str(exc)] = waiting.get(str(exc), 0) + 1
            continue
        if signer is None:
            return {"status": "planned_read_only", "plan": asdict(plan),
                    "found": found, "active": len(state.tokens), "waiting": waiting}
        if int(exit_keeper.q_launch(rpc, bindings.q, token)[0]) != 2:
            continue
        try:
            signer.submit("configure", token, configure_harvest_data(plan), state, store)
            signer.submit("poke", token, poke_harvest_data(token), state, store)
        except exit_keeper.WaitForExit as exc:
            waiting[str(exc)] = waiting.get(str(exc), 0) + 1
            continue
        processed = False
        next_token, next_step, eligible_at = exit_keeper.q_next_action(rpc, bindings.q)
        if (process_next and next_token == token and next_step == 2 and
                eligible_at <= price.latest_block_time(rpc)):
            try:
                signer.submit("process", token, exit_keeper.PROCESS_NEXT, state, store,
                              purpose="configured_harvest")
            except exit_keeper.WaitForExit as exc:
                waiting[str(exc)] = waiting.get(str(exc), 0) + 1
                return {"status": "harvest_configured", "token": token,
                        "processNextSent": False, "found": found,
                        "active": len(state.tokens), "waiting": waiting}
            processed = True
        return {"status": "harvest_configured", "token": token,
                "processNextSent": processed, "found": found,
                "active": len(state.tokens), "waiting": waiting}
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
    parser.add_argument("--lp-slippage-bps", type=int, default=1000)
    parser.add_argument("--swap-slippage-bps", type=int, default=1500)
    parser.add_argument("--ttl-seconds", type=int, default=120)
    parser.add_argument("--max-snapshot-age-seconds", type=int, default=20)
    parser.add_argument("--max-poke-gas", type=int, default=500_000)
    parser.add_argument("--max-config-gas", type=int, default=500_000)
    parser.add_argument("--max-process-gas", type=int, default=10_000_000)
    parser.add_argument("--max-fee-gwei", default="5")
    parser.add_argument("--max-priority-gwei", default="1")
    parser.add_argument("--receipt-timeout", type=int, default=180)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--no-process-next", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.live and not args.once:
        parser.error("--live requires --once; run via the keeper supervisor")
    if not args.http_url or not args.chain_id or not args.q or not args.guard:
        parser.error("HTTP URL, chain ID, Q, and guard are required")
    if (args.confirmations < 1 or args.block_span < 1 or args.poll_seconds <= 0 or
            min(args.max_poke_gas, args.max_config_gas, args.max_process_gas) < 21_000 or
            args.receipt_timeout < 1):
        parser.error("invalid confirmation, span, poll, gas, or receipt setting")
    fee_cap, priority_cap = watch.gwei(args.max_fee_gwei), watch.gwei(args.max_priority_gwei)
    if priority_cap > fee_cap:
        raise HarvestError("priority fee cap exceeds max fee cap")
    settings = HarvestSettings(args.lp_slippage_bps, args.swap_slippage_bps,
                               args.ttl_seconds, args.max_snapshot_age_seconds,
                               args.max_poke_gas + args.max_config_gas + args.max_process_gas,
                               fee_cap)
    settings.validate()
    q, guard = watch.address(args.q), watch.address(args.guard)
    rpc = watch.HttpRpc(args.http_url)
    private_key = os.environ.get("PONS_EXIT_CONFIGURATOR_PRIVATE_KEY") if args.live else None
    if args.live and not private_key:
        raise HarvestError("--live requires PONS_EXIT_CONFIGURATOR_PRIVATE_KEY")
    try:
        signer_address = Account.from_key(private_key).address.lower() if private_key else None
    except Exception as exc:
        raise HarvestError("exitConfigurator private key is invalid") from exc
    bindings = exit_keeper.verify_bindings(rpc, args.chain_id, q, guard,
                                           args.quoter, signer_address)
    quotes = price.ExecutableQuoteProvider(rpc, bindings.price)
    signer = (HarvestSigner(rpc, bindings, args.chain_id, private_key, args.confirmations,
                            args.max_poke_gas, args.max_config_gas, args.max_process_gas,
                            fee_cap, priority_cap, args.receipt_timeout, args.poll_seconds)
              if private_key else None)
    with (exit_keeper.ExitConfiguratorLock(q, args.state) if signer else nullcontext()), \
            price.KeeperStore(args.state) as store:
        state = store.load(rpc, args.chain_id, q, guard, args.start_block)
        if state.pending_tx is not None:
            if signer is None:
                raise HarvestError("read-only mode cannot recover an outstanding signed transaction")
            if is_harvest_pending(state.pending_tx):
                signer.recover(state, store, exit_keeper.ExitSettings(), quotes)
            else:
                exit_signer = exit_keeper.ExitSigner(
                    rpc, bindings, args.chain_id, private_key, args.confirmations,
                    args.max_poke_gas, args.max_config_gas, args.max_process_gas,
                    fee_cap, priority_cap, args.receipt_timeout, args.poll_seconds)
                exit_signer.recover(state, store, exit_keeper.ExitSettings(), quotes)
        result = run_cycle(rpc, bindings, state, store, settings, quotes, signer,
                           args.confirmations, args.block_span,
                           not args.no_process_next)
        print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HarvestError, exit_keeper.ExitError, price.KeeperError, watch.WatcherError) as exc:
        print(f"harvest keeper stopped: {exc}", file=sys.stderr)
        sys.exit(1)
