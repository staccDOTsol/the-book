#!/usr/bin/env python3
"""Read-only, block-pinned Plan for the live Q/USDG atomic v3 bootstrap.

Runs LiveV3UsdGQuote.s.sol locally with forge script. No signer, key, broadcast,
approval, deployment, or token mutation is used. Re-run just before review.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from decimal import Decimal, ROUND_FLOOR, getcontext
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORGE = ROOT / ".local/foundry/forge"
SCRIPT = "forensics/launch_cursor/LiveV3UsdGQuote.s.sol:LiveV3UsdGQuote"
RPC = os.environ.get("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
Q = "0x623B5374c4CB838DA24EE9F48F08664337936a06"
OWNER = "0x26E8134eCC3af5cCE32f34B03E7BD2f318B25158"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
Q96 = 1 << 96


def rpc(method: str, params: list[object]) -> object:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        RPC, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if "error" in data:
        raise RuntimeError(f"RPC {method}: {data['error']}")
    return data["result"]


def quote_probe(block: int) -> dict[str, int | str]:
    command = [
        str(FORGE), "script", SCRIPT, "--sig", "run()",
        "--rpc-url", RPC, "--fork-block-number", str(block), "--root", str(ROOT),
        "--use", "0.8.26", "--via-ir", "--optimizer-runs", "1", "-vv",
    ]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=150)
    if process.returncode:
        raise RuntimeError(process.stdout[-5000:] + process.stderr[-5000:])
    values: dict[str, int | str] = {}
    for key, value in re.findall(r"^  ([a-z][a-z0-9_]*): (\S+)$", process.stdout, re.MULTILINE):
        values[key] = value if value.startswith("0x") else int(value)
    required = {
        "wallet_eth", "eth_cap", "wallet_usdg", "lp_usdg", "buy_usdg",
        "eth_in", "quote_usdg", "quote_q", "q_for_lp", "initial_sqrt",
        "v4_sqrt", "v4_fee", "v4_liquidity", "existing_q_usdg_v3", "q_is_token0",
    }
    if missing := required - values.keys():
        raise RuntimeError(f"Forge quote logs missing {sorted(missing)}: {process.stdout[-3000:]}")
    return values


def main() -> None:
    if int(str(rpc("eth_chainId", [])), 16) != 4663:
        raise RuntimeError("RPC is not Robinhood chain 4663")
    block = int(str(rpc("eth_blockNumber", [])), 16)
    header = rpc("eth_getBlockByNumber", [hex(block), False])
    assert isinstance(header, dict)
    values = quote_probe(block)
    if values["existing_q_usdg_v3"] != "0x0000000000000000000000000000000000000000":
        raise RuntimeError("Q/USDG 0.30% v3 pool already exists")
    eth_in = int(values["eth_in"])
    eth_cap = int(values["eth_cap"])
    quote_usdg = int(values["quote_usdg"])
    quote_q = int(values["quote_q"])
    usdg_lp = int(values["lp_usdg"])
    usdg_buy = int(values["buy_usdg"])
    q_lp = int(values["q_for_lp"])
    q_is_token0 = bool(values["q_is_token0"])
    numerator, denominator = (quote_usdg, quote_q) if q_is_token0 else (quote_q, quote_usdg)
    initial_sqrt = math.isqrt((numerator * Q96 * Q96) // denominator)
    if initial_sqrt != values["initial_sqrt"]:
        raise RuntimeError("Solidity/Python sqrt derivations disagree")
    if not (0 < eth_in <= eth_cap and quote_q > 0 and quote_usdg > 0):
        raise RuntimeError("Invalid quote or ETH cap")

    getcontext().prec = 105
    d = Decimal
    sqrt_ratio = d(initial_sqrt) / d(Q96)
    tick = int((2 * sqrt_ratio.ln() / d("1.0001").ln()).to_integral_value(rounding=ROUND_FLOOR))
    center = ((tick + 30) // 60) * 60
    lower, upper = center - 600, center + 600
    sqrt_lower = int((d("1.0001") ** (d(lower) / 2) * d(Q96)).to_integral_value(rounding=ROUND_FLOOR))
    sqrt_upper = int((d("1.0001") ** (d(upper) / 2) * d(Q96)).to_integral_value(rounding=ROUND_FLOOR))
    if not (sqrt_lower < initial_sqrt < sqrt_upper):
        raise RuntimeError("Derived ticks do not straddle initialization")
    s, sl, su = d(initial_sqrt), d(sqrt_lower), d(sqrt_upper)
    amount0 = d(q_lp if q_is_token0 else usdg_lp)
    amount1 = d(usdg_lp if q_is_token0 else q_lp)
    liquidity0 = amount0 * s * su / (d(Q96) * (su - s))
    liquidity1 = amount1 * d(Q96) / (s - sl)
    liquidity = min(liquidity0, liquidity1)
    lp0_used = int(liquidity * d(Q96) * (su - s) / (su * s))
    lp1_used = int(liquidity * (s - sl) / d(Q96))
    q_lp_used = lp0_used if q_is_token0 else lp1_used
    usdg_lp_used = lp1_used if q_is_token0 else lp0_used
    min_q_lp = q_lp * 95 // 100
    min_usdg_lp = usdg_lp * 95 // 100
    if q_lp_used < min_q_lp or usdg_lp_used < min_usdg_lp:
        raise RuntimeError("LP amount model does not clear 95% minima")

    # A USDG->Q exact-input v3 swap should move sqrt toward lower prices
    # when USDG is token0, and toward higher prices when it is token1.
    fee_adjusted_in = d(usdg_buy) * d(997) / d(1000)
    if not q_is_token0:
        after_sqrt = liquidity * d(Q96) * s / (liquidity * d(Q96) + fee_adjusted_in * s)
        projected_buy_q = liquidity * (s - after_sqrt) / d(Q96)
        buy_limit = math.isqrt(initial_sqrt * initial_sqrt * 95 // 100)
        limit_ok = after_sqrt > d(buy_limit)
    else:
        after_sqrt = s + fee_adjusted_in * d(Q96) / liquidity
        projected_buy_q = liquidity * d(Q96) * (after_sqrt - s) / (after_sqrt * s)
        buy_limit = math.isqrt(initial_sqrt * initial_sqrt * 105 // 100)
        limit_ok = after_sqrt < d(buy_limit)
    min_v4_q = quote_q * 98 // 100
    min_buy_q = usdg_buy * quote_q * 997 * 95 // (quote_usdg * 1000 * 100)
    if min_v4_q < q_lp or projected_buy_q < d(min_buy_q) or not limit_ok:
        raise RuntimeError("First buy or v4 quote does not clear plan minima/price limit")

    deadline = int(str(header["timestamp"]), 16) + 600
    plan = [
        0, min_v4_q, q_lp, usdg_lp, min_q_lp, min_usdg_lp,
        usdg_buy, min_buy_q, initial_sqrt, buy_limit, lower, upper, deadline,
    ]
    result = {
        "snapshot": {"chain_id": 4663, "block": block, "hash": header["hash"],
                     "timestamp": int(str(header["timestamp"]), 16)},
        "owner": OWNER, "q": Q, "usdg": USDG,
        "wallet": {"eth_wei": values["wallet_eth"], "eth_cap_25pct_wei": eth_cap,
                   "usdg_raw": values["wallet_usdg"]},
        "quotes": {"eth_in_wei": eth_in, "weth_to_usdg_raw": quote_usdg,
                   "eth_to_q_raw": quote_q, "v4_liquidity": values["v4_liquidity"]},
        "plan_fields": ["qInventoryIn", "minQFromV4", "qForLp", "usdgForLp", "minQInLp",
                        "minUsdgInLp", "usdgForBuy", "minQFromV3Buy", "initialSqrtPriceX96",
                        "v3BuySqrtPriceLimitX96", "tickLower", "tickUpper", "deadline"],
        "plan_tuple": plan,
        "msg_value_wei": eth_in,
        "model_check": {"initial_tick": tick, "lp_q_used_approx": q_lp_used,
                        "lp_usdg_used_approx": usdg_lp_used,
                        "first_buy_q_approx": int(projected_buy_q),
                        "first_buy_sqrt_after_approx": int(after_sqrt),
                        "note": "Local concentrated-liquidity math; simulate full bootstrap at this block before sending."},
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Plan generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
