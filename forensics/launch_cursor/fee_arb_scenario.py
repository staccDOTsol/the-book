#!/usr/bin/env python3
"""Deterministic X-in/Q-out arbitrage accounting for the planned three bands.

The curve mode uses continuous concentrated-liquidity math, assumes the Q
issuer is the sole in-range LP, no protocol fee, a flat external X price in
Q/X, and no gas or external slippage. It is a conditional scenario, not a
profit forecast. Uniswap v4 applies integer rounding and may allocate fees
to other LPs; executable route quotes are needed for a real decision.

The route mode only reconciles a caller-supplied same-block quote bundle. It
does not authenticate RPC responses or execute any transaction.

For this repo's planned bands, R is
((Pons phantom quote reserve + graduation threshold) / phantom)^2. The
illustrative p0 is a fixed 0.01 Q/X band scale, independent of mint size and
not an external market price.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import sys
from typing import Any


PIPS = Decimal(1_000_000)
ILLUSTRATIVE_SUPPLY = Decimal(1_000_000_000)
ILLUSTRATIVE_MINT_SHARE = Decimal("0.001")
ILLUSTRATIVE_DEPOSIT_SHARE = Decimal("0.95")
ILLUSTRATIVE_P0 = Decimal("0.01")
ILLUSTRATIVE_R = Decimal("12.25")
ILLUSTRATIVE_EXTERNAL_Q_PER_X = Decimal("0.664713176195")


class ScenarioError(ValueError):
    pass


def positive_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ScenarioError(f"{label} is not a decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ScenarioError(f"{label} must be positive and finite")
    return number


def _string(number: Decimal) -> str:
    return "0" if number == 0 else format(number, ".18g")


def illustrative_mints_and_deposits() -> tuple[list[Decimal], list[Decimal]]:
    """Sequential 0.1%-of-current-supply mints, then 95% assumed deposits."""
    supply = ILLUSTRATIVE_SUPPLY
    mints: list[Decimal] = []
    for _ in range(3):
        mint = supply * ILLUSTRATIVE_MINT_SHARE
        mints.append(mint)
        supply += mint
    return mints, [mint * ILLUSTRATIVE_DEPOSIT_SHARE for mint in mints]


def illustrative_matrix_rows() -> list[dict[str, str]]:
    """All 46 fee arms at four external X-price factors, with p0 held fixed."""
    _, deposits = illustrative_mints_and_deposits()
    rows: list[dict[str, str]] = []
    for factor in (Decimal(1), Decimal("0.75"), Decimal("0.5"), Decimal("0.25")):
        external = ILLUSTRATIVE_EXTERNAL_Q_PER_X * factor
        for fee_pct in range(5, 51):
            result = analyze_pool(ILLUSTRATIVE_P0, ILLUSTRATIVE_R, external,
                                  fee_pct * 10_000, deposits)
            rows.append({
                "pons_price_factor": _string(factor),
                "fee_pct": str(fee_pct),
                "external_q_per_x": _string(external),
                "first_marginal_arb": "yes" if Decimal(result["qExtractedTotal"]) > 0 else "no",
                "optimal_stop_q_per_x": result["poolStopQPerX"],
                "q_extracted": result["qExtractedTotal"],
                "x_gross_bought": result["xGrossBoughtExternally"],
                "issuer_lp_mark_loss_q": result["issuerLPMarkLossQ"],
            })
    return rows


def analyze_pool(p0: Any, r: Any, external_q_per_x: Any,
                 fee_pips: int, q_deposits: list[Any]) -> dict[str, Any]:
    """Analyze the joint pool, including overlapping bands, at one flat X price."""
    with localcontext() as context:
        context.prec = 70
        p0 = positive_decimal(p0, "p0")
        r = positive_decimal(r, "R")
        external = positive_decimal(external_q_per_x, "external Q/X price")
        if r <= 1 or not 50_000 <= fee_pips <= 500_000 or \
                fee_pips % 10_000 or len(q_deposits) != 3:
            raise ScenarioError("R must exceed one; fee must be a 5–50% arm; supply three Q deposits")
        deposits = [positive_decimal(value, f"Q deposit {i}")
                    for i, value in enumerate(q_deposits)]
        fee = Decimal(fee_pips) / PIPS
        lower_sqrt = p0.sqrt()
        upper_prices = [p0 * r * multiple for multiple in (1, 2, 10)]
        highest = upper_prices[-1]
        unconstrained_stop = external / (1 - fee)
        stop_price = max(p0, min(highest, unconstrained_stop))
        stop_sqrt = stop_price.sqrt()
        bands: list[dict[str, str | int]] = []
        total_q_out = total_x_net = total_x_fee = Decimal(0)
        for index, (deposit, upper) in enumerate(zip(deposits, upper_prices, strict=True)):
            upper_sqrt = upper.sqrt()
            liquidity = deposit / (upper_sqrt - lower_sqrt)
            end_sqrt = min(upper_sqrt, max(lower_sqrt, stop_sqrt))
            q_out = liquidity * (upper_sqrt - end_sqrt)
            x_net = liquidity * (1 / end_sqrt - 1 / upper_sqrt)
            x_fee = x_net * fee / (1 - fee)
            total_q_out += q_out
            total_x_net += x_net
            total_x_fee += x_fee
            marginal_threshold = max(Decimal(0), 1 - external / upper)
            full_sweep_threshold = max(Decimal(0), 1 - external / (p0 * upper).sqrt())
            bands.append({
                "tranche": index,
                "lowerQPerX": _string(p0),
                "upperQPerX": _string(upper),
                "qDeposited": _string(deposit),
                "qExtractedAtOptimalStop": _string(q_out),
                "xPrincipalReceived": _string(x_net),
                "xFeesReceivedIfSoleLP": _string(x_fee),
                "feeRequiredToStopFirstMarginalArb": _string(marginal_threshold),
                "feeRequiredForFullSweepBreakEven": _string(full_sweep_threshold),
            })
        total_q = sum(deposits)
        x_gross = total_x_net + total_x_fee
        external_x_cost_q = external * x_gross
        gross_arb_gain_q = total_q_out - external_x_cost_q
        lp_end_mark_q = total_q - gross_arb_gain_q
        return {
            "model": "conditional_continuous_flat_external_price",
            "assumptions": [
                "issuer is sole in-range LP and receives all X input fees",
                "protocol fee, external slippage, gas, MEV, and v4 integer rounding omitted",
                "external Q/X price is flat at every trade size",
            ],
            "p0QPerX": _string(p0), "R": _string(r),
            "externalQPerX": _string(external), "feePips": fee_pips,
            "marginalOptimalStopQPerX": _string(unconstrained_stop),
            "poolStopQPerX": _string(stop_price),
            "bands": bands,
            "qDepositedTotal": _string(total_q),
            "qExtractedTotal": _string(total_q_out),
            "xNetIntoCurve": _string(total_x_net),
            "xInputFeeToIssuer": _string(total_x_fee),
            "xGrossBoughtExternally": _string(x_gross),
            "externalXCostQ": _string(external_x_cost_q),
            "arbitrageurGrossGainQBeforeGas": _string(gross_arb_gain_q),
            "issuerLPEndMarkQ": _string(lp_end_mark_q),
            "issuerLPMarkLossQ": _string(gross_arb_gain_q),
        }


def _uint(value: Any, label: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ScenarioError(f"{label} must be a decimal integer string")
    return int(value)


def check_executable_route(bundle: dict[str, Any]) -> dict[str, Any]:
    """Reconcile three same-block executable quotes; do not authenticate them."""
    if not isinstance(bundle, dict):
        raise ScenarioError("route bundle must be a JSON object")
    legs = []
    for name in ("xq", "ponsBuy", "qSale"):
        leg = bundle.get(name)
        if not isinstance(leg, dict):
            raise ScenarioError(f"{name} quote is missing")
        legs.append(leg)
    block_hash = legs[0].get("blockHash")
    block_number = legs[0].get("blockNumber")
    if (not isinstance(block_hash, str) or not block_hash.startswith("0x") or
            len(block_hash) != 66 or type(block_number) is not int or block_number < 0 or
            any(leg.get("blockHash") != block_hash or
                leg.get("blockNumber") != block_number for leg in legs)):
        raise ScenarioError("all three executable quotes must pin the same block")
    try:
        bytes.fromhex(block_hash[2:])
    except ValueError as exc:
        raise ScenarioError("quote block hash is invalid hex") from exc
    x_in = _uint(legs[0].get("xInputWei"), "X/Q X input")
    q_out = _uint(legs[0].get("qOutputWei"), "X/Q Q output")
    pons_x = _uint(legs[1].get("xOutputWei"), "Pons X output")
    pons_eth = _uint(legs[1].get("ethInputWei"), "Pons ETH input")
    q_sale_in = _uint(legs[2].get("qInputWei"), "Q sale input")
    q_sale_eth = _uint(legs[2].get("ethOutputWei"), "Q sale ETH output")
    gas = _uint(bundle.get("gasEthWei", "0"), "gas ETH cost")
    if min(x_in, q_out, pons_x, pons_eth, q_sale_in, q_sale_eth) <= 0 or \
            x_in != pons_x or q_out != q_sale_in:
        raise ScenarioError("Pons, X/Q, and Q/ETH quote sizes do not match")
    return {"model": "caller_supplied_executable_route_quotes",
            "authenticated": False, "blockNumber": block_number,
            "blockHash": block_hash, "xBoughtAndSoldWei": str(x_in),
            "qExtractedAndSoldWei": str(q_out),
            "ponsEthInputWei": str(pons_eth),
            "qSaleEthOutputWei": str(q_sale_eth),
            "gasEthWei": str(gas),
            "routeNetEthWei": str(q_sale_eth - pons_eth - gas),
            "profitableAtTheseExactSizes": q_sale_eth > pons_eth + gas}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    curve = sub.add_parser("curve", help="continuous illustrative three-band math")
    curve.add_argument("--p0", required=True)
    curve.add_argument("--r", required=True)
    curve.add_argument("--external-q-per-x", required=True)
    curve.add_argument("--fee-pips", required=True, type=int)
    curve.add_argument("--q-deposits", required=True, nargs=3)
    route = sub.add_parser("route", help="reconcile a caller-supplied quote JSON")
    route.add_argument("bundle", type=Path)
    sub.add_parser("matrix", help="print the fixed illustrative 0.1%% mint matrix as CSV")
    args = parser.parse_args()
    try:
        if args.mode == "matrix":
            rows = illustrative_matrix_rows()
            writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            return 0
        result = (analyze_pool(args.p0, args.r, args.external_q_per_x,
                               args.fee_pips, args.q_deposits)
                  if args.mode == "curve" else
                  check_executable_route(json.loads(args.bundle.read_text())))
    except (ScenarioError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
