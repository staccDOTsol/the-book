"""Read-only, evidence-gated accounting for hookless Pons X/Q fee arms.

This tool never calls a contract or promotes a result to StaticNextPoolFee.
Its inputs are operator-supplied event, receipt, cost-lot, and executable-quote
evidence. It checks internal consistency, not chain authenticity.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
STAGES = {"queued", "active", "exited", "skipped", "aborted"}


class EvidenceError(ValueError):
    """An input is missing evidence or contradicts the contract's accounting."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be a list")
    return value


def _uint(value: Any, label: str) -> int:
    if isinstance(value, bool) or not (
        isinstance(value, int) or isinstance(value, str) and re.fullmatch(r"[0-9]+", value)
    ):
        raise EvidenceError(f"{label} must be an unsigned decimal integer")
    number = int(value)
    if number < 0:
        raise EvidenceError(f"{label} must be nonnegative")
    return number


def _field_uint(row: dict[str, Any], key: str, label: str) -> int:
    if key not in row:
        raise EvidenceError(f"{label}.{key} is required")
    return _uint(row[key], f"{label}.{key}")


def _hex(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EvidenceError(f"{label} must be a 0x-prefixed hex value of the expected length")
    return value.lower()


def _ref(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} needs an evidence reference")
    return value.strip()


def _wei(value: int) -> str:
    return str(value)


def _signed_bps(numerator: int, denominator: int) -> int:
    """Round to nearest basis point, ties away from zero."""
    magnitude = (abs(numerator) * 10_000 * 2 + denominator) // (2 * denominator)
    return magnitude if numerator >= 0 else -magnitude


def _quote(row: dict[str, Any], key: str, expected_q: int, expected_block: int,
           label: str) -> int | None:
    if key not in row or row[key] is None:
        return None
    quote = _object(row[key], f"{label}.{key}")
    if _field_uint(quote, "q_amount_wei", f"{label}.{key}") != expected_q:
        raise EvidenceError(f"{label}.{key} must quote the whole relevant Q amount")
    if _field_uint(quote, "block_number", f"{label}.{key}") != expected_block:
        raise EvidenceError(f"{label}.{key} must use the open/exit block")
    if quote.get("route") != "Q/ETH":
        raise EvidenceError(f"{label}.{key}.route must be Q/ETH")
    _ref(quote.get("source_ref"), f"{label}.{key}.source_ref")
    return _field_uint(quote, "eth_out_wei", f"{label}.{key}")


def _gas(row: dict[str, Any], label: str) -> tuple[int, int, dict[str, int], set[str], list[tuple[str, int, int]]]:
    total = 0
    network_total = 0
    by_payer: dict[str, int] = defaultdict(int)
    hashes: set[str] = set()
    allocations: list[tuple[str, int, int]] = []
    for i, item in enumerate(_list(row.get("gas_receipts", []), f"{label}.gas_receipts")):
        name = f"{label}.gas_receipts[{i}]"
        receipt = _object(item, name)
        tx = _hex(receipt.get("tx_hash"), TX_HASH, f"{name}.tx_hash")
        if tx in hashes:
            raise EvidenceError(f"{label} repeats gas receipt {tx}")
        hashes.add(tx)
        payer = _hex(receipt.get("payer"), ADDRESS, f"{name}.payer")
        _ref(receipt.get("role"), f"{name}.role")
        gas_used = _field_uint(receipt, "gas_used", name)
        gas_price = _field_uint(receipt, "effective_gas_price_wei", name)
        extra = _field_uint(receipt, "extra_fee_wei", name)
        full_fee = gas_used * gas_price + extra
        allocated = _uint(receipt.get("attributed_fee_wei", full_fee), f"{name}.attributed_fee_wei")
        if allocated > full_fee:
            raise EvidenceError(f"{name} attributes more than the receipt fee")
        if allocated < full_fee:
            _ref(receipt.get("allocation_ref"), f"{name}.allocation_ref")
        total += allocated
        network_total += full_fee
        by_payer[payer] += allocated
        allocations.append((tx, allocated, full_fee))
    return total, network_total, dict(sorted(by_payer.items())), hashes, allocations


def _cost_basis(row: dict[str, Any], q_spent: int, label: str) -> int | None:
    if "q_cost_lots" not in row or row["q_cost_lots"] is None:
        return None
    lots = _list(row["q_cost_lots"], f"{label}.q_cost_lots")
    if not lots:
        raise EvidenceError(f"{label}.q_cost_lots cannot be empty")
    q_total = cost_total = 0
    for i, item in enumerate(lots):
        name = f"{label}.q_cost_lots[{i}]"
        lot = _object(item, name)
        q_amount = _field_uint(lot, "q_amount_wei", name)
        if q_amount == 0:
            raise EvidenceError(f"{name}.q_amount_wei must be positive")
        q_total += q_amount
        cost_total += _field_uint(lot, "eth_paid_wei", name)
        _ref(lot.get("acquisition_ref"), f"{name}.acquisition_ref")
    if q_total != q_spent:
        raise EvidenceError(f"{label}.q_cost_lots must cover exactly the Q spent at open")
    return cost_total


def _volume(row: dict[str, Any], through: int, label: str) -> tuple[bool, int, int, int]:
    if "swap_volume" not in row or row["swap_volume"] is None:
        return False, 0, 0, 0
    item = _object(row["swap_volume"], f"{label}.swap_volume")
    name = f"{label}.swap_volume"
    count = _field_uint(item, "swap_count", name)
    x_in = _field_uint(item, "x_in_wei", name)
    q_in = _field_uint(item, "q_in_wei", name)
    if _field_uint(item, "through_block", name) < through:
        raise EvidenceError(f"{name} stops before the measurement block")
    _ref(item.get("source_ref"), f"{name}.source_ref")
    if (count == 0) != (x_in == 0 and q_in == 0):
        raise EvidenceError(f"{name} has inconsistent count and input volume")
    return True, count, x_in, q_in


def _observation(row: dict[str, Any], as_of_block: int, index: int,
                 strategy_gas_payers: set[str]) -> tuple[dict[str, Any], list[tuple[str, int, int]]]:
    label = f"observations[{index}]"
    token = _hex(row.get("token"), ADDRESS, f"{label}.token")
    fee = _field_uint(row, "fee_pips", label)
    if fee < 50_000 or fee > 500_000 or fee % 10_000:
        raise EvidenceError(f"{label}.fee_pips is not one of the 46 static fee arms")
    selection_tx = _hex(row.get("selection_tx_hash"), TX_HASH, f"{label}.selection_tx_hash")
    stage = row.get("stage")
    if stage not in STAGES:
        raise EvidenceError(f"{label}.stage must be one of {sorted(STAGES)}")

    opening = row.get("open")
    open_tx = None
    open_block = None
    q_spent = None
    if opening is not None:
        opening = _object(opening, f"{label}.open")
        open_tx = _hex(opening.get("tx_hash"), TX_HASH, f"{label}.open.tx_hash")
        open_block = _field_uint(opening, "block_number", f"{label}.open")
        q_spent = _field_uint(opening, "q_spent_wei", f"{label}.open")
        if q_spent == 0 or open_block > as_of_block:
            raise EvidenceError(f"{label}.open has no Q spend or lies after as_of_block")
    if (stage in {"active", "exited", "aborted"}) != (opening is not None):
        raise EvidenceError(f"{label}.stage and open evidence disagree")

    closed = row.get("exit")
    exit_tx = None
    exit_block = None
    payout = eth_out = q_bought = q_burned = q_settled = 0
    if stage == "exited":
        closed = _object(closed, f"{label}.exit")
        exit_tx = _hex(closed.get("tx_hash"), TX_HASH, f"{label}.exit.tx_hash")
        exit_block = _field_uint(closed, "block_number", f"{label}.exit")
        if exit_block < open_block or exit_block > as_of_block:
            raise EvidenceError(f"{label}.exit block is outside the open-to-measurement interval")
        x_settled = _field_uint(closed, "x_settled_wei", f"{label}.exit")
        x_sold = _field_uint(closed, "x_sold_wei", f"{label}.exit")
        eth_out = _field_uint(closed, "eth_out_wei", f"{label}.exit")
        q_bought = _field_uint(closed, "q_bought_wei", f"{label}.exit")
        q_settled = _field_uint(closed, "q_settled_wei", f"{label}.exit")
        q_burned = _field_uint(closed, "q_burned_wei", f"{label}.exit")
        wizard = _field_uint(closed, "wizard_weth_wei", f"{label}.exit")
        developer = _field_uint(closed, "developer_eth_wei", f"{label}.exit")
        payout = wizard + developer
        if x_sold != x_settled or q_burned != q_bought + q_settled:
            raise EvidenceError(f"{label}.exit disagrees with executor/router settlement")
        if (x_sold == 0 and (eth_out != 0 or q_bought != 0)) or (
            x_sold != 0 and (eth_out == 0 or q_bought == 0)
        ):
            raise EvidenceError(f"{label}.exit has inconsistent X sale and Q purchase")
        if payout != eth_out - eth_out // 2 or wizard != payout // 2 or developer != payout - wizard:
            raise EvidenceError(f"{label}.exit payouts do not match router's split")
        if q_burned == 0:
            raise EvidenceError(f"{label}.exit did not burn Q")
    elif closed is not None:
        raise EvidenceError(f"{label}.exit evidence requires exited stage")

    cost = _cost_basis(row, q_spent, label) if q_spent is not None else None
    if q_spent is None and row.get("q_cost_lots") is not None:
        raise EvidenceError(f"{label}.q_cost_lots requires an open")
    gas, linked_network_gas, by_payer, gas_hashes, allocations = _gas(row, label)
    strategy_gas = sum(amount for payer, amount in by_payer.items() if payer in strategy_gas_payers)
    external_gas = gas - strategy_gas
    gas_complete = row.get("gas_complete", False)
    if not isinstance(gas_complete, bool):
        raise EvidenceError(f"{label}.gas_complete must be boolean")
    if gas_complete:
        _ref(row.get("gas_scope_ref"), f"{label}.gas_scope_ref")
        required = {selection_tx}
        if open_tx is not None:
            required.add(open_tx)
        if exit_tx is not None:
            required.add(exit_tx)
        if not required.issubset(gas_hashes):
            raise EvidenceError(f"{label}.gas_receipts omit a selected/open/exit transaction")
    entry_quote = _quote(row, "entry_q_eth_quote", q_spent, open_block, label) if q_spent is not None else None
    exit_quote = _quote(row, "exit_burn_q_eth_quote", q_burned, exit_block, label) if exit_tx else None
    if q_spent is None and row.get("entry_q_eth_quote") is not None:
        raise EvidenceError(f"{label}.entry_q_eth_quote requires an open")
    if not exit_tx and row.get("exit_burn_q_eth_quote") is not None:
        raise EvidenceError(f"{label}.exit_burn_q_eth_quote requires an exit")
    volume_complete, swaps, x_volume, q_volume = _volume(row, exit_block or as_of_block, label)
    if stage == "exited" and volume_complete and swaps == 0:
        raise EvidenceError(f"{label} cannot close at a post-entry boundary with zero observed swaps")

    cash_net = (
        payout - cost - strategy_gas
        if stage == "exited" and cost is not None and gas_complete else None
    )
    system_cash_net = (
        payout - cost - gas
        if stage == "exited" and cost is not None and gas_complete else None
    )
    burn_value_estimate = (
        payout + exit_quote - entry_quote - gas
        if stage == "exited" and gas_complete and
        entry_quote is not None and exit_quote is not None else None
    )
    cash_candidate = (
        _signed_bps(cash_net, cost)
        if cash_net is not None and cost > 0 and volume_complete and swaps > 0 else None
    )
    missing_cash = []
    missing_burn_value = []
    if stage == "exited":
        if cost is None: missing_cash.append("Q acquisition cost lots")
        if cost == 0: missing_cash.append("positive actual Q acquisition cost")
        if not gas_complete:
            missing_cash.append("complete attributed gas")
            missing_burn_value.append("complete attributed gas")
        if not volume_complete: missing_cash.append("complete X/Q swap volume")
        if volume_complete and swaps == 0: missing_cash.append("nonzero X/Q swap volume")
        if entry_quote is None: missing_burn_value.append("size-aware entry Q/ETH liquidation quote")
        if exit_quote is None: missing_burn_value.append("size-aware exit burn Q/ETH liquidation quote")

    return ({
        "token": token,
        "fee_pips": fee,
        "stage": stage,
        "classification": (
            "cash_feedback_eligible_exit" if cash_candidate is not None else
            "cash_feedback_ineligible_exit" if stage == "exited" else
            "right_censored_active" if stage == "active" else
            "never_opened" if stage in {"queued", "skipped"} else "emergency_unwind"
        ),
        "q_spent_wei": _wei(q_spent) if q_spent is not None else None,
        "historical_q_cost_eth_wei": _wei(cost) if cost is not None else None,
        "entry_q_liquidation_eth_wei": _wei(entry_quote) if entry_quote is not None else None,
        "x_sale_eth_wei": _wei(eth_out) if exit_tx else None,
        "eth_spent_buying_q_wei": _wei(eth_out // 2) if exit_tx else None,
        "realized_recipient_cash_eth_wei": _wei(payout) if exit_tx else None,
        "q_bought_and_burned_wei": _wei(q_bought) if exit_tx else None,
        "q_recovered_and_burned_wei": _wei(q_settled) if exit_tx else None,
        "total_q_burned_wei": _wei(q_burned) if exit_tx else None,
        "exit_burn_q_liquidation_estimate_eth_wei": _wei(exit_quote) if exit_quote is not None else None,
        "known_attributed_gas_eth_wei": _wei(gas),
        "linked_receipts_full_network_gas_eth_wei": _wei(linked_network_gas),
        "strategy_paid_gas_eth_wei": _wei(strategy_gas),
        "external_payer_gas_eth_wei": _wei(external_gas),
        "gas_by_payer_eth_wei": {payer: _wei(amount) for payer, amount in by_payer.items()},
        "gas_operator_attested_complete": gas_complete,
        "swap_volume_complete": volume_complete,
        "swap_count": swaps if volume_complete else None,
        "x_input_volume_wei": _wei(x_volume) if volume_complete else None,
        "q_input_volume_wei": _wei(q_volume) if volume_complete else None,
        "recipient_cash_after_q_cost_and_strategy_gas_wei": _wei(cash_net) if cash_net is not None else None,
        "system_cash_after_q_cost_and_all_gas_wei": _wei(system_cash_net) if system_cash_net is not None else None,
        "system_cash_plus_burn_valuation_estimate_wei": (
            _wei(burn_value_estimate) if burn_value_estimate is not None else None
        ),
        "cash_return_on_actual_q_cost_bps_unclipped": cash_candidate,
        "cash_return_on_actual_q_cost_bps_clipped": (
            max(-10_000, min(10_000, cash_candidate)) if cash_candidate is not None else None
        ),
        "missing_for_cash_feedback": missing_cash,
        "missing_for_burn_valuation": missing_burn_value,
    }, allocations)


def analyze(document: dict[str, Any]) -> dict[str, Any]:
    doc = _object(document, "input")
    if doc.get("schema_version") == 2:
        return analyze_minted(doc)
    if doc.get("schema_version") != 1:
        raise EvidenceError("schema_version must be 1 or 2")
    chain_id = _field_uint(doc, "chain_id", "input")
    if chain_id != 4663:
        raise EvidenceError("chain_id must be Robinhood chain 4663")
    as_of_block = _field_uint(doc, "as_of_block", "input")
    payer_inputs = _list(doc.get("strategy_gas_payers"), "input.strategy_gas_payers")
    strategy_gas_payers = {
        _hex(payer, ADDRESS, f"input.strategy_gas_payers[{i}]")
        for i, payer in enumerate(payer_inputs)
    }
    if not strategy_gas_payers or len(strategy_gas_payers) != len(payer_inputs):
        raise EvidenceError("strategy_gas_payers needs distinct configured signer addresses")
    inputs = _list(doc.get("observations"), "input.observations")
    rows: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    receipt_allocations: dict[str, tuple[int, int]] = {}
    arms: dict[int, dict[str, Any]] = {}
    for i, item in enumerate(inputs):
        row, allocations = _observation(
            _object(item, f"observations[{i}]"), as_of_block, i, strategy_gas_payers
        )
        if row["token"] in seen_tokens:
            raise EvidenceError(f"duplicate token {row['token']}")
        seen_tokens.add(row["token"])
        for tx, allocated, full in allocations:
            prior, prior_full = receipt_allocations.get(tx, (0, full))
            if prior_full != full or prior + allocated > full:
                raise EvidenceError(f"receipt {tx} is inconsistently or multiply attributed")
            receipt_allocations[tx] = (prior + allocated, full)
        rows.append(row)
        arm = arms.setdefault(row["fee_pips"], {
            "fee_pips": row["fee_pips"], "assignments": 0,
            "stages": {stage: 0 for stage in sorted(STAGES)},
            "cash_feedback_ineligible_exits": 0, "cash_feedback_eligible_exits": 0,
            "idle_open_at_horizon": 0, "cash_feedback_bps_sum_clipped": 0,
            "known_recipient_cash_eth_wei": 0,
            "known_q_burned_wei": 0,
            "known_strategy_gas_eth_wei": 0,
            "known_external_payer_gas_eth_wei": 0,
            "complete_swap_windows": 0,
            "swap_count_from_complete_windows": 0,
            "x_input_volume_wei_from_complete_windows": 0,
            "q_input_volume_wei_from_complete_windows": 0,
            "eligible_cash_result_eth_wei": 0,
        })
        arm["assignments"] += 1
        arm["stages"][row["stage"]] += 1
        arm["known_strategy_gas_eth_wei"] += int(row["strategy_paid_gas_eth_wei"])
        arm["known_external_payer_gas_eth_wei"] += int(row["external_payer_gas_eth_wei"])
        if row["realized_recipient_cash_eth_wei"] is not None:
            arm["known_recipient_cash_eth_wei"] += int(row["realized_recipient_cash_eth_wei"])
            arm["known_q_burned_wei"] += int(row["total_q_burned_wei"])
        if row["swap_volume_complete"]:
            arm["complete_swap_windows"] += 1
            arm["swap_count_from_complete_windows"] += row["swap_count"]
            arm["x_input_volume_wei_from_complete_windows"] += int(row["x_input_volume_wei"])
            arm["q_input_volume_wei_from_complete_windows"] += int(row["q_input_volume_wei"])
        if row["classification"] == "cash_feedback_ineligible_exit":
            arm["cash_feedback_ineligible_exits"] += 1
        if row["classification"] == "cash_feedback_eligible_exit":
            arm["cash_feedback_eligible_exits"] += 1
            arm["cash_feedback_bps_sum_clipped"] += row["cash_return_on_actual_q_cost_bps_clipped"]
            arm["eligible_cash_result_eth_wei"] += int(row["recipient_cash_after_q_cost_and_strategy_gas_wei"])
        if row["stage"] == "active" and row["swap_count"] == 0:
            arm["idle_open_at_horizon"] += 1
    for arm in arms.values():
        n = arm["cash_feedback_eligible_exits"]
        arm["cash_feedback_bps_mean_clipped"] = (
            arm["cash_feedback_bps_sum_clipped"] / n if n else None
        )
        for key in (
            "known_recipient_cash_eth_wei", "known_q_burned_wei",
            "known_strategy_gas_eth_wei", "known_external_payer_gas_eth_wei",
            "x_input_volume_wei_from_complete_windows",
            "q_input_volume_wei_from_complete_windows", "eligible_cash_result_eth_wei",
        ):
            arm[key] = _wei(arm[key])
    return {
        "schema_version": 1,
        "chain_id": chain_id,
        "as_of_block": as_of_block,
        "strategy_gas_payers": sorted(strategy_gas_payers),
        "unique_linked_receipts_network_gas_eth_wei": _wei(
            sum(full for _, full in receipt_allocations.values())
        ),
        "evidence_scope": "operator-supplied, internally reconciled, not chain-authenticated",
        "onchain_fee_policy_action": "none; unvalued executor exits remain Selected until an authorized report or post-deadline censor",
        "accounting_note": "The candidate arm objective is actual recipient ETH/WETH less historical Q purchase cost and strategy-paid gas, divided by positive actual Q purchase cost. External Q-transfer caller gas is disclosed separately and included in system cost, not charged to strategy feedback. Burn and entry Q values are hypothetical executable-quote estimates, never realized proceeds.",
        "observations": rows,
        "arms": [arms[fee] for fee in sorted(arms)],
    }


def analyze_minted(document: dict[str, Any]) -> dict[str, Any]:
    """Reconcile the active minted-Q fee feedback schema without a cash-cost fiction.

    These local records are not themselves chain authentication. The live
    feedback keeper authenticates receipts and pins archive quotes first.
    """
    from pons_fee_feedback_keeper import canonical_evidence, gross_mark_score

    doc = _object(document, "input")
    if _field_uint(doc, "chain_id", "input") != 4663:
        raise EvidenceError("chain_id must be Robinhood chain 4663")
    as_of = _field_uint(doc, "as_of_block", "input")
    inputs = _list(doc.get("observations"), "input.observations")
    rows: list[dict[str, Any]] = []
    arms: dict[int, dict[str, Any]] = {}
    seen: set[str] = set()
    for i, raw in enumerate(inputs):
        label = f"observations[{i}]"
        item = _object(raw, label)
        token = _hex(item.get("token"), ADDRESS, f"{label}.token")
        if token in seen:
            raise EvidenceError(f"duplicate token {token}")
        seen.add(token)
        fee = _field_uint(item, "fee_pips", label)
        if fee < 50_000 or fee > 500_000 or fee % 10_000:
            raise EvidenceError(f"{label}.fee_pips is not a 5–50% fee arm")
        stage = item.get("stage")
        if stage not in STAGES:
            raise EvidenceError(f"{label}.stage is invalid")
        record = {"token": token, "fee_pips": fee, "stage": stage,
                  "gross_mark_score_bps": None, "gross_surplus_eth_wei": None,
                  "evidence_hash": None, "profit_claim": False}
        evidence = item.get("evidence")
        if evidence is not None:
            if stage != "exited":
                raise EvidenceError(f"{label}.evidence requires exited stage")
            evidence = _object(evidence, f"{label}.evidence")
            if evidence.get("schemaVersion") != 1 or evidence.get("chainId") != 4663 or \
                    evidence.get("token", "").lower() != token:
                raise EvidenceError(f"{label}.evidence identity disagrees")
            observed = _object(evidence.get("observation"), f"{label}.evidence.observation")
            if observed.get("token", "").lower() != token or \
                    _field_uint(observed, "feePips", f"{label}.evidence.observation") != fee or \
                    _field_uint(observed, "trancheCount", f"{label}.evidence.observation") != 3:
                raise EvidenceError(f"{label}.evidence opening disagrees with three-tranche fee assignment")
            q_spent = _field_uint(observed, "qSpentWei", f"{label}.evidence.observation")
            q_minted = _field_uint(observed, "qMintedWei", f"{label}.evidence.observation")
            unused = _field_uint(observed, "qUnusedBurnedAtOpenWei", f"{label}.evidence.observation")
            q_burned = _field_uint(observed, "qBurnedWei", f"{label}.evidence.observation")
            open_block = _field_uint(observed, "openBlock", f"{label}.evidence.observation")
            exit_block = _field_uint(observed, "exitBlock", f"{label}.evidence.observation")
            if q_spent == 0 or q_burned == 0 or q_minted != q_spent + unused or \
                    open_block < 2 or not open_block <= exit_block <= as_of:
                raise EvidenceError(f"{label}.evidence mint, burn, or chronology is invalid")
            entry = _object(evidence.get("entryQuote"), f"{label}.evidence.entryQuote")
            burns = _list(evidence.get("burnQuotes"), f"{label}.evidence.burnQuotes")
            settlements = _list(observed.get("settlementBurns"),
                                f"{label}.evidence.observation.settlementBurns")
            if len(settlements) < 3 or \
                    sum(_object(s, f"{label}.settlement").get("kind") == "position_exit"
                        for s in settlements) != 3 or \
                    _object(settlements[-1], f"{label}.settlement").get("kind") != "position_exit":
                raise EvidenceError(f"{label}.evidence must include all three position exits")
            expected_burns = []
            previous_block = open_block
            for j, raw_settlement in enumerate(settlements):
                settlement = _object(raw_settlement, f"{label}.settlement[{j}]")
                block = _field_uint(settlement, "block", f"{label}.settlement[{j}]")
                amount = _field_uint(settlement, "qBurnedWei", f"{label}.settlement[{j}]")
                if (settlement.get("kind") not in {"position_exit", "fee_harvest"} or
                        amount == 0 or not previous_block <= block <= exit_block or block < 2):
                    raise EvidenceError(f"{label}.evidence settlement chronology is invalid")
                expected_burns.append((amount, block - 1))
                previous_block = block
            if previous_block != exit_block:
                raise EvidenceError(f"{label}.evidence final settlement block differs from closure")
            if (_field_uint(entry, "qInputWei", f"{label}.evidence.entryQuote") != q_spent or
                    _field_uint(entry, "sourceBlock", f"{label}.evidence.entryQuote") != open_block - 1 or
                    entry.get("route") != "zero-hook Q/ETH exact-input Q sale" or
                    len(burns) != len(expected_burns)):
                raise EvidenceError(f"{label}.evidence quotes do not cover exact Q amounts at pinned blocks")
            _hex(entry.get("sourceBlockHash"), TX_HASH, f"{label}.evidence.entryQuote.sourceBlockHash")
            quoted_burn_eth = 0
            for j, (quote_raw, (amount, block)) in enumerate(zip(burns, expected_burns, strict=True)):
                quote = _object(quote_raw, f"{label}.evidence.burnQuotes[{j}]")
                if (_field_uint(quote, "qInputWei", f"{label}.evidence.burnQuotes[{j}]") != amount or
                        _field_uint(quote, "sourceBlock", f"{label}.evidence.burnQuotes[{j}]") != block or
                        quote.get("route") != "zero-hook Q/ETH exact-input Q sale"):
                    raise EvidenceError(f"{label}.evidence quotes do not cover exact Q amounts at pinned blocks")
                _hex(quote.get("sourceBlockHash"), TX_HASH,
                     f"{label}.evidence.burnQuotes[{j}].sourceBlockHash")
                quoted_burn_eth += _field_uint(quote, "ethOutputWei", f"{label}.evidence.burnQuotes[{j}]")
            if sum(amount for amount, _ in expected_burns) != q_burned:
                raise EvidenceError(f"{label}.evidence lifetime Q burns do not reconcile")
            computed = gross_mark_score(
                observed, _field_uint(entry, "ethOutputWei", f"{label}.evidence.entryQuote"),
                quoted_burn_eth)
            if evidence.get("score") != computed:
                raise EvidenceError(f"{label}.evidence score does not recompute")
            evidence_hash = "0x" + hashlib.sha256(canonical_evidence(evidence)).hexdigest()
            record.update({"gross_mark_score_bps": computed["grossReturnBpsForFeePolicy"],
                           "gross_mark_score_bps_unclipped": computed["grossReturnBpsUnclipped"],
                           "gross_surplus_eth_wei": computed["grossEstimatedSurplusEthWei"],
                           "evidence_hash": evidence_hash,
                           "recipient_cash_eth_wei": computed["recipientCashEthWei"],
                           "q_minted_deposited_wei": computed["entryMintedQDepositedWei"],
                           "q_burned_lifetime_wei": computed["exitQBurnedWei"]})
        elif stage == "exited":
            record["censor_reason"] = "missing authenticated historical Q/ETH quote evidence"
        if stage == "active":
            count = item.get("observed_swap_count")
            if count is not None:
                record["observed_swap_count"] = _uint(count, f"{label}.observed_swap_count")
        rows.append(record)
        arm = arms.setdefault(fee, {"fee_pips": fee, "assigned": 0,
                                     "completed_with_gross_mark": 0,
                                     "unvalued_or_censored": 0,
                                     "still_open": 0, "idle_open_at_horizon": 0,
                                     "selection_score_sum_bps": 0})
        arm["assigned"] += 1
        if record["gross_mark_score_bps"] is not None:
            arm["completed_with_gross_mark"] += 1
            arm["selection_score_sum_bps"] += record["gross_mark_score_bps"]
        else:
            arm["selection_score_sum_bps"] -= 5_000
            if stage == "active":
                arm["still_open"] += 1
                if record.get("observed_swap_count") == 0:
                    arm["idle_open_at_horizon"] += 1
            else:
                arm["unvalued_or_censored"] += 1
    for arm in arms.values():
        arm["exploratory_selection_mean_bps"] = (
            arm["selection_score_sum_bps"] // arm["assigned"])
    return {"schema_version": 2, "chain_id": 4663, "as_of_block": as_of,
            "metric": "gross_mark_to_market_eth_equivalent_excluding_gas",
            "profit_claim": False,
            "selection_penalty_bps_for_unresolved": -5_000,
            "evidence_scope": "locally reconciled; use the live keeper for canonical receipt and quote authentication",
            "observations": rows, "arms": [arms[fee] for fee in sorted(arms)]}


def draft_report_calls(document: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    """Encode unsigned calls only; the JSON evidence is not chain-authenticated."""
    doc = _object(document, "input")
    if doc.get("schema_version") == 2:
        from pons_fee_feedback_keeper import report_data
        cursor = doc.get("cursor_address")
        if cursor is not None:
            cursor = _hex(cursor, ADDRESS, "input.cursor_address")
        return [{"to": cursor, "token": row["token"],
                 "gross_mark_score_bps": row["gross_mark_score_bps"],
                 "evidence_hash": row["evidence_hash"],
                 "calldata": report_data(row["token"], row["gross_mark_score_bps"],
                                         row["evidence_hash"]),
                 "status": "unsigned gross-mark draft; not a profit report or chain authentication"}
                for row in report["observations"] if row["gross_mark_score_bps"] is not None]
    source_rows = _list(doc.get("observations"), "input.observations")
    cursor = doc.get("cursor_address")
    if cursor is not None:
        cursor = _hex(cursor, ADDRESS, "input.cursor_address")
    calls = []
    for source, outcome in zip(source_rows, report["observations"], strict=True):
        bps = outcome["cash_return_on_actual_q_cost_bps_clipped"]
        if bps is None:
            continue
        payload = {
            "schema_version": 1,
            "chain_id": report["chain_id"],
            "as_of_block": report["as_of_block"],
            "strategy_gas_payers": report["strategy_gas_payers"],
            "observation": source,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        evidence_hash = hashlib.sha256(encoded).hexdigest()
        token = outcome["token"]
        calldata = (
            "0x173510f8" + token[2:].rjust(64, "0") +
            (bps % (1 << 256)).to_bytes(32, "big").hex() + evidence_hash
        )
        calls.append({
            "to": cursor,
            "token": token,
            "cash_return_bps": bps,
            "evidence_hash": "0x" + evidence_hash,
            "evidence_hash_scheme": "sha256 of canonical JSON payload v1",
            "calldata": calldata,
            "status": "unsigned draft; chain receipts, Q lots, signer role, and pending deadline must be authenticated",
        })
    return calls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON evidence file; no network calls are made")
    parser.add_argument("--output", type=Path, help="write report JSON instead of stdout")
    parser.add_argument("--draft-report-calls", action="store_true",
                        help="include unsigned reportExitedOutcome calldata for eligible exits")
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.input.read_text())
        result = analyze(document)
        if args.draft_report_calls:
            result["unsigned_report_calls"] = draft_report_calls(document, result)
    except (OSError, json.JSONDecodeError, EvidenceError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
