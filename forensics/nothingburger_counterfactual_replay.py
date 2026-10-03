#!/usr/bin/env python3
"""Counterfactual Pons quote-token / one-tick v4 LP replay.

The 19 X/ETH paths are observed. NOTHINGBURGER, each X/N pool, and every
arbitrage trade are synthetic. Completed external swap blocks provide a price
and a cap on hypothetical route notional, not a guaranteed executable quote.
No transactions are sent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import heapq
import json
import math
from pathlib import Path

try:
    from .pons_curve_scenario import (
        CurveConfig, CurveState, after_buy, after_sell, opening_state,
        quote_buy, quote_sell,
    )
except ImportError:
    from pons_curve_scenario import (
        CurveConfig, CurveState, after_buy, after_sell, opening_state,
        quote_buy, quote_sell,
    )


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "forensics/results/nothingburger-historical-x-eth-shocks-24h.json"
OUTPUT = ROOT / "forensics/results/nothingburger-counterfactual-replay-24h.json"
WEI = 10**18
INITIAL_ETH = .04                 # $104 at the $2,600/ETH scenario conversion.
ETH_USD = 2600
LAUNCH_FEE_ETH = .0005
GAS_RESERVE_ETH = .001
DEV_BUY_ETH = INITIAL_ETH - LAUNCH_FEE_ETH - GAS_RESERVE_ETH
MINT_BURN_GAS_ETH = .000013802     # Sampled historical fork, per LP entry/exit.
ARB_GAS_ETH = .00001              # Assumed outsider route cost, decision only.
CREATOR_BASE_SHARE = .70          # Live hook 30% protocol, buyback disabled.
FEE_BPS = (100, 700, 2000, 5000, 7000)
TAX_BPS = tuple(range(0, 1001, 100))
DEFAULT_SPACING = 200
CONFIG = CurveConfig(10**27, 100, 1_680_000_000_000_000_000,
                     4_200_000_000_000_000_000, 0, 200, True)


@dataclass
class Position:
    token: str
    entry_block: int
    cap_block: int
    x_price: float
    external_fee: float
    n_deposit: float
    lower: float
    upper: float
    sqrt_price: float
    liquidity: float
    external_sqrt: float
    external_liquidity: float
    external_lower: float
    external_upper: float
    fee_x: float = 0.0
    fee_n: float = 0.0
    arb_count: int = 0
    activated: bool = False
    last_external_notional: float = 0.0

    def balances(self) -> tuple[float, float]:
        s = max(self.lower, min(self.sqrt_price, self.upper))
        return (self.liquidity * (1 / s - 1 / self.upper),
                self.liquidity * (s - self.lower))


def n_spot(state: CurveState) -> float:
    return state.quote_reserve / state.token_reserve


def from_wei(value: int) -> float:
    return value / WEI


def to_wei(value: float) -> int:
    return max(0, math.floor(value * WEI))


def make_position(path: dict, n_deposit: float, curve: CurveState,
                  fee_bps: int, spacing: int) -> Position:
    px = float(path["initialPriceEthPerX"])
    if px <= 0 or n_deposit <= 0:
        raise ValueError("nonpositive entry X price or stake")
    parity_n_per_x = px / n_spot(curve)
    tick = math.floor(math.log(parity_n_per_x) / math.log(1.0001))
    upper_tick = (tick // spacing) * spacing
    lower_tick = upper_tick - spacing
    # N is token1; at the upper tick the entire position is N. Its first
    # conversion to X requires X to fall relative to N.
    lower = math.pow(1.0001, lower_tick / 2)
    upper = math.pow(1.0001, upper_tick / 2)
    liquidity = n_deposit / (upper - lower)
    e = (int(path.get("externalPonsBaseFeeBps", 100)) +
         int(path.get("externalPonsCreatorTaxBps", 0))) / 10000
    ext_s, ext_l, ext_a, ext_b = external_tick_state(
        path["initialSqrtPriceX96"], path["initialActiveLiquidityRaw"],
        path["initialTick"], int(path.get("tickSpacing", 200)))
    return Position(path["token"], int(path["entryBlock"]),
                    int(path["timeCapBlock"]), px, e, n_deposit,
                    lower, upper, upper, liquidity, ext_s, ext_l, ext_a, ext_b)


def external_tick_state(sqrt_raw: str | int, liquidity_raw: str | int,
                        tick: int, spacing: int) -> tuple[float, float, float, float]:
    s = int(sqrt_raw) / 2**96
    l = int(liquidity_raw) / WEI
    lower_tick = (int(tick) // spacing) * spacing
    a = math.pow(1.0001, lower_tick / 2)
    b = math.pow(1.0001, (lower_tick + spacing) / 2)
    return s, l, a, b


def external_buy_x_cost(position: Position, x_units: float,
                        mode: str) -> float | None:
    """ETH paid to buy X on its Pons pool, including X hook fee."""
    if mode == "marginal":
        return x_units * position.x_price / (1 - position.external_fee)
    s, l = position.external_sqrt, position.external_liquidity
    # Pons takes its quote-leg fee from the X output of an ETH->X exact-input
    # swap. The core pool must output more X to deliver x_units to the arb.
    core_x_out = x_units / (1 - position.external_fee)
    if l <= 0 or core_x_out >= l * (s - position.external_lower):
        return None
    next_s = s - core_x_out / l
    return l * (1 / next_s - 1 / s)


def external_sell_x_proceeds(position: Position, x_units: float,
                             mode: str) -> float | None:
    """ETH received for selling X on its Pons pool, including X hook fee."""
    if mode == "marginal":
        return x_units * position.x_price * (1 - position.external_fee)
    s, l = position.external_sqrt, position.external_liquidity
    if l <= 0 or x_units >= l * (position.external_upper - s):
        return None
    next_s = s + x_units / l
    return l * (1 / s - 1 / next_s) * (1 - position.external_fee)


def swap_x_in(position: Position, gross_x: float, fee: float) -> tuple[float, float, float]:
    net_x = gross_x * (1 - fee)
    next_s = max(position.lower,
                 1 / (1 / position.sqrt_price + net_x / position.liquidity))
    n_out = position.liquidity * (position.sqrt_price - next_s)
    return n_out, next_s, gross_x - net_x


def swap_n_in(position: Position, gross_n: float, fee: float) -> tuple[float, float, float]:
    net_n = gross_n * (1 - fee)
    next_s = min(position.upper, position.sqrt_price + net_n / position.liquidity)
    x_out = position.liquidity * (1 / position.sqrt_price - 1 / next_s)
    return x_out, next_s, gross_n - net_n


def best_trade(position: Position, curve: CurveState, pool_fee: float,
               tax_bps: int, external_notional: float,
               external_mode: str) -> dict | None:
    if external_notional <= 0:
        return None
    px = position.x_price
    pn = n_spot(curve)
    p_pool = position.sqrt_price**2
    pons_fee = (CONFIG.base_fee_bps + tax_bps) / 10000
    e = position.external_fee
    candidates: list[dict] = []

    # Buy X on the observed external market, sell X to our LP, sell N to
    # the Pons N/ETH curve. Outsider pays both our LP core fee and N tax.
    x_marginal = (1 - pool_fee) * p_pool * pn * (1 - pons_fee) - px / (1 - e)
    if x_marginal > 0 and position.sqrt_price > position.lower * (1 + 1e-13):
        max_pool = (position.liquidity *
                    (1 / position.lower - 1 / position.sqrt_price) /
                    (1 - pool_fee))
        max_depth = external_notional * (1 - e) / px
        if external_mode == "one_tick":
            max_depth = min(max_depth,
                            (1 - e) * position.external_liquidity *
                            (position.external_sqrt - position.external_lower) * .999999)
        cap = min(max_pool, max_depth)

        def trial_x(amount: float) -> dict | None:
            if amount <= 0:
                return None
            n_out, next_s, fee_x = swap_x_in(position, amount, pool_fee)
            raw_n = to_wei(n_out)
            if raw_n <= 0:
                return None
            sale = quote_sell(CONFIG, curve, raw_n, tax_bps)
            if sale.gross_quote > curve.real_quote_reserve:
                return None
            cost = external_buy_x_cost(position, amount, external_mode)
            if cost is None or cost > external_notional:
                return None
            profit = from_wei(sale.quote_out) - cost - ARB_GAS_ETH
            return {"direction": "X_into_pool", "amount": amount,
                    "profitEth": profit, "nRaw": raw_n, "ponsQuote": sale,
                    "nextS": next_s, "feeX": fee_x, "feeN": 0.0,
                    "thirdPartyPonsVolumeEth": from_wei(sale.gross_quote)}

        best = maximize_positive(trial_x, cap)
        if best:
            candidates.append(best)

    # Buy N on its Pons curve, sell N to our LP, sell X on the observed
    # external market. This is possible only after some X is held by our LP.
    n_marginal = ((1 - pool_fee) / p_pool * px * (1 - e) -
                  pn / (1 - pons_fee))
    if n_marginal > 0 and position.sqrt_price < position.upper * (1 - 1e-13):
        cap = position.liquidity * (position.upper - position.sqrt_price) / (1 - pool_fee)
        cap = min(cap, from_wei(curve.sellable_tokens) * .999)

        def trial_n(amount: float) -> dict | None:
            if amount <= 0:
                return None
            x_out, next_s, fee_n = swap_n_in(position, amount, pool_fee)
            proceeds = external_sell_x_proceeds(position, x_out, external_mode)
            if proceeds is None or proceeds > external_notional * (1 + 1e-9):
                return None
            raw_n = to_wei(amount)
            if raw_n <= 0 or raw_n >= curve.sellable_tokens:
                return None
            net_need = ((raw_n * curve.quote_reserve) //
                        (curve.token_reserve - raw_n) + 1)
            gross_quote = ((net_need * 10000 + 10000 - CONFIG.base_fee_bps - tax_bps - 1) //
                           (10000 - CONFIG.base_fee_bps - tax_bps))
            buy = quote_buy(CONFIG, curve, gross_quote, tax_bps)
            if buy.tokens_out < raw_n:
                return None
            cost = from_wei(buy.quote_spent)
            profit = proceeds - cost - ARB_GAS_ETH
            return {"direction": "N_into_pool", "amount": amount,
                    "profitEth": profit, "nRaw": raw_n, "ponsQuote": buy,
                    "nextS": next_s, "feeX": 0.0, "feeN": fee_n,
                    "thirdPartyPonsVolumeEth": cost}

        best = maximize_positive(trial_n, cap)
        if best:
            candidates.append(best)

    return max(candidates, key=lambda row: row["profitEth"]) if candidates else None


def maximize_positive(trial, cap: float) -> dict | None:
    if not math.isfinite(cap) or cap <= 0:
        return None
    left, right = 0.0, cap
    # Gross route profit is unimodal over one v4 tick and one Pons curve hop.
    # Each evaluation uses the verified curve's integer quote functions.
    for _ in range(16):
        x1 = left + (right - left) * .38196601125
        x2 = left + (right - left) * .61803398875
        r1, r2 = trial(x1), trial(x2)
        p1 = r1["profitEth"] if r1 else -math.inf
        p2 = r2["profitEth"] if r2 else -math.inf
        if p1 < p2:
            left = x1
        else:
            right = x2
    rows = [trial(x) for x in (cap, left, (left + right) / 2, right)]
    rows = [row for row in rows if row and row["profitEth"] > 0]
    return max(rows, key=lambda row: row["profitEth"]) if rows else None


def creator_claim(quote) -> float:
    # 30% protocol share of the 1% Pons base fee; buyback disabled.
    return from_wei(quote.creator_tax) + CREATOR_BASE_SHARE * from_wei(quote.base_fee)


def redeem_available_n(curve: CurveState, n_raw: int,
                       tax_bps: int) -> tuple[int, object | None]:
    """Largest immediately redeemable N amount under the real ETH reserve."""
    if n_raw <= 0 or curve.real_quote_reserve <= 0:
        return 0, None
    full = quote_sell(CONFIG, curve, n_raw, tax_bps)
    if full.gross_quote <= curve.real_quote_reserve:
        return n_raw, full
    lo, hi = 0, n_raw
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        q = quote_sell(CONFIG, curve, mid, tax_bps)
        if q.gross_quote <= curve.real_quote_reserve:
            lo = mid
        else:
            hi = mid
    return lo, quote_sell(CONFIG, curve, lo, tax_bps) if lo else None


def launch_hold_baseline(tax_bps: int) -> float:
    """Same single-wallet launch with no X/N LPs or outsider N swaps."""
    dev = quote_buy(CONFIG, opening_state(CONFIG), to_wei(DEV_BUY_ETH), tax_bps)
    state = after_buy(opening_state(CONFIG), dev)
    sale = quote_sell(CONFIG, state, dev.tokens_out, tax_bps)
    return GAS_RESERVE_ETH + from_wei(sale.quote_out) + creator_claim(dev) + creator_claim(sale)


def replay(paths: list[dict], fee_bps: int, tax_bps: int,
           spacing: int = DEFAULT_SPACING,
           capture_details: bool = False,
           external_mode: str = "marginal") -> dict:
    pool_fee = fee_bps / 10000
    dev = quote_buy(CONFIG, opening_state(CONFIG), to_wei(DEV_BUY_ETH), tax_bps)
    curve = after_buy(opening_state(CONFIG), dev)
    n_wallet = from_wei(dev.tokens_out)
    eth_wallet = GAS_RESERVE_ETH
    creator_eth = creator_claim(dev)
    creator_third_party_eth = 0.0
    creator_self_eth = creator_claim(dev)
    lp_core_fee_x = lp_core_fee_n = 0.0
    external_x_proceeds_eth = 0.0
    x_unquoted_beyond_first_tick = 0.0
    third_party_pons_volume_eth = 0.0
    total_arb_profit_eth = 0.0
    total_arb_count = 0
    gas_charged_eth = 0.0
    activated_count = 0
    entries = exits = 0
    exit_reasons: dict[str, int] = {}
    positions: dict[str, Position] = {}
    entry_details = []
    trade_details = []
    exit_details = []
    events = []
    for path in paths:
        token = path["token"]
        events.append((int(path["entryBlock"]) + 1, 1, token, "entry", path))
        for swap in path["swaps"]:
            events.append((int(swap["block"]) + 1, 0, token, "shock", swap))
        events.append((int(path["timeCapBlock"]) + 1, 2, token, "timeout", None))
    heapq.heapify(events)

    def close(position: Position, reason: str) -> None:
        nonlocal n_wallet, eth_wallet, gas_charged_eth, exits, activated_count
        nonlocal external_x_proceeds_eth, lp_core_fee_x, lp_core_fee_n
        nonlocal x_unquoted_beyond_first_tick
        x_principal, n_principal = position.balances()
        x_total = x_principal + position.fee_x
        n_total = n_principal + position.fee_n
        n_wallet += n_total
        x_eth = external_sell_x_proceeds(position, x_total, external_mode)
        if x_eth is None:
            # We know the current tick's L but not all following initialized
            # ticks. Cash value after crossing them is not identified.
            # Retain a marginal-price upper proxy and track the unquoted X.
            one_tick_capacity = max(0.0, position.external_liquidity *
                                    (position.external_upper - position.external_sqrt))
            x_unquoted_beyond_first_tick += max(0.0, x_total - one_tick_capacity)
            x_eth = x_total * position.x_price * (1 - position.external_fee)
        eth_wallet += x_eth
        external_x_proceeds_eth += x_eth
        lp_core_fee_x += position.fee_x
        lp_core_fee_n += position.fee_n
        gas_charged_eth += MINT_BURN_GAS_ETH
        eth_wallet -= MINT_BURN_GAS_ETH
        exits += 1
        activated_count += int(position.activated)
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        if capture_details:
            exit_details.append({
                "token": position.token, "reason": reason,
                "priceEthPerXAtExit": position.x_price,
                "xPrincipal": x_principal, "nPrincipal": n_principal,
                "feeX": position.fee_x, "feeN": position.fee_n,
                "xEthLiquidationAfterExternalFee": x_eth,
                "hypotheticalArbTrades": position.arb_count,
            })
        del positions[position.token]

    while events:
        _, _, token, kind, data = heapq.heappop(events)
        if kind == "entry":
            # 25% of remaining uncommitted N. No fresh ETH is assumed.
            n_deposit = n_wallet * .25
            if n_deposit <= 0:
                continue
            pos = make_position(data, n_deposit, curve, fee_bps, spacing)
            if capture_details:
                probe_eth = .001
                probe_buy = quote_buy(CONFIG, curve, to_wei(probe_eth), tax_bps)
                probe_n = probe_eth / n_spot(curve)
                probe_sell = quote_sell(CONFIG, curve, to_wei(probe_n), tax_bps)
                entry_details.append({
                    "token": token, "entryBlock": pos.entry_block,
                    "nDeposited": n_deposit,
                    "nSpotEthPerToken": n_spot(curve),
                    "xObservedSpotEthPerToken": pos.x_price,
                    "nPerXMarginalParity": pos.x_price / n_spot(curve),
                    "nPerXPoolInitialBoundary": pos.upper**2,
                    "nCurveBuyFor001EthTokens": from_wei(probe_buy.tokens_out),
                    "nCurveSellOf001EthSpotTokensNetEth": from_wei(probe_sell.quote_out),
                    "externalXSmallBuyQuote001Eth": data.get("signalSmallBuyQuote001Eth"),
                })
            n_wallet -= n_deposit
            positions[token] = pos
            entries += 1
        elif kind == "shock":
            pos = positions.get(token)
            if not pos:
                continue
            pos.x_price = float(data["postPriceEthPerX"])
            (pos.external_sqrt, pos.external_liquidity,
             pos.external_lower, pos.external_upper) = external_tick_state(
                data["postSqrtPriceX96"], data["postLiquidityRaw"],
                data["postTick"], 200)
            volume = float(data.get("ethNotional", 0))
            pos.last_external_notional = volume
            result = best_trade(pos, curve, pool_fee, tax_bps, volume,
                                external_mode)
            if result is None:
                continue
            if capture_details:
                trade_details.append({
                    "token": token, "externalCompletedBlock": int(data["block"]),
                    "direction": result["direction"],
                    "arbInputUnits": result["amount"],
                    "arbProfitAfterAssumedGasEth": result["profitEth"],
                    "ownerLpFeeXUnits": result["feeX"],
                    "ownerLpFeeNUnits": result["feeN"],
                    "ownerPonsCreatorFeeEth": creator_claim(result["ponsQuote"]),
                    "observedExternalEthNotionalCap": volume,
                    "externalXSpotEth": pos.x_price,
                    "nCurveSpotEthBefore": n_spot(curve),
                    "ponsQuoteGrossEth": result["thirdPartyPonsVolumeEth"],
                })
            quote = result["ponsQuote"]
            if result["direction"] == "X_into_pool":
                curve = after_sell(curve, result["nRaw"], quote)
            else:
                curve = after_buy(curve, quote)
            receipt = creator_claim(quote)
            creator_eth += receipt
            creator_third_party_eth += receipt
            third_party_pons_volume_eth += result["thirdPartyPonsVolumeEth"]
            total_arb_profit_eth += result["profitEth"]
            total_arb_count += 1
            pos.sqrt_price = result["nextS"]
            pos.fee_x += result["feeX"]
            pos.fee_n += result["feeN"]
            pos.arb_count += 1
            pos.activated = True
            if pos.sqrt_price <= pos.lower * (1 + 1e-11):
                close(pos, "full_X")
            elif pos.sqrt_price >= pos.upper * (1 - 1e-11):
                close(pos, "returned_N")
        else:
            pos = positions.get(token)
            if pos:
                close(pos, "30m")

    # Owner sells remaining N through the same curve after all candidate exits.
    # The creator tax paid on this self-trade is returned as a creator claim.
    ending_n_raw = to_wei(n_wallet)
    redeemed_raw, redeem = redeem_available_n(curve, ending_n_raw, tax_bps)
    redeemable = redeemed_raw == ending_n_raw
    if redeem:
        creator_self_eth += creator_claim(redeem)
        creator_eth += creator_claim(redeem)
        eth_wallet += from_wei(redeem.quote_out)
    eth_wallet += creator_eth
    final_usd = eth_wallet * ETH_USD
    row = {
        "poolFeeBps": fee_bps,
        "creatorTaxBps": tax_bps,
        "tickSpacing": spacing,
        "externalQuoteMode": external_mode,
        "entryCount": entries,
        "exitCount": exits,
        "activatedPositions": activated_count,
        "exitReasons": exit_reasons,
        "hypotheticalArbTrades": total_arb_count,
        "hypotheticalExternalPonsVolumeEth": third_party_pons_volume_eth,
        "hypotheticalArbProfitAfterAssumedGasEth": total_arb_profit_eth,
        "thirdPartyCreatorFeesEth": creator_third_party_eth,
        "selfPaidCreatorFeesEth": creator_self_eth,
        "totalCreatorFeesEth": creator_eth,
        "lpCoreFeeXUnits": lp_core_fee_x,
        "lpCoreFeeNUnits": lp_core_fee_n,
        "externalXLiquidationEth": external_x_proceeds_eth,
        "xUnitsBeyondKnownExternalExitTick": x_unquoted_beyond_first_tick,
        "remainingNBeforeRedemption": n_wallet,
        "remainingNEthRedeemable": redeemable,
        "redeemedNUnits": from_wei(redeemed_raw),
        "unredeemedNUnits": from_wei(ending_n_raw - redeemed_raw),
        "endingNRedeemNetEth": from_wei(redeem.quote_out) if redeem else None,
        "sampledMintBurnGasEth": gas_charged_eth,
        "walletEndEth": eth_wallet,
        "walletEndUsd": final_usd,
        "walletProfitUsd": final_usd - INITIAL_ETH * ETH_USD,
        "deltaVsLaunchHoldUsd": (eth_wallet - launch_hold_baseline(tax_bps)) * ETH_USD,
        "returnPct": (eth_wallet / INITIAL_ETH - 1) * 100,
    }
    if capture_details:
        row["entryDetails"] = entry_details
        row["tradeDetails"] = trade_details
        row["exitDetails"] = exit_details
    return row


def main() -> None:
    source = json.loads(SOURCE.read_text())
    paths = source["paths"]
    grid = [replay(paths, fee, tax) for fee in FEE_BPS for tax in TAX_BPS]
    spacing = [replay(paths, fee, tax, tick_spacing)
               for fee, tax in ((100, 0), (100, 500), (100, 1000),
                                (700, 0), (700, 500), (700, 1000))
               for tick_spacing in (60, 600)]
    best = max(grid, key=lambda r: r["walletProfitUsd"])
    best_detail = replay(paths, best["poolFeeBps"], best["creatorTaxBps"],
                         capture_details=True)
    exact_external_tick = [replay(paths, fee, tax, external_mode="one_tick")
                           for fee in FEE_BPS for tax in TAX_BPS]
    output = {
        "schemaVersion": 1,
        "scope": "19 historical Pons X/ETH completed-block price/volume paths; all NOTHINGBURGER positions and all X/N arbitrage trades are synthetic.",
        "source": str(SOURCE.relative_to(ROOT)),
        "initialWalletUsd": 104,
        "ethUsdAssumption": ETH_USD,
        "initialWalletEth": INITIAL_ETH,
        "launchFeeEth": LAUNCH_FEE_ETH,
        "initialGasReserveEth": GAS_RESERVE_ETH,
        "devBuyEth": DEV_BUY_ETH,
        "ponsConfig": asdict(CONFIG),
        "entry": "At completed signal block + 1, deposit 25% of uncommitted N inventory in the nearest legal one-tick quote-only [lower, upper] band below X/N marginal parity; no X and no fresh ETH are deposited.",
        "exit": "Burn at first full-X or returned-N boundary after activation, else at the 30-minute cap; sell X at observed end-block X/ETH price after X's actual Pons base+creator fees; keep N for reuse; redeem remaining N on its Pons curve at sample end.",
        "arb": "After each observed X/ETH swap block, outsider considers the more profitable of X->pool->N curve and N curve->pool->X, optimizes size inside one tick, caps the external ETH leg by that block's observed ETH notional, and trades only above assumed 0.00001 ETH gas. Main grid marks the external X leg at post-swap marginal price; exactExternalSingleTickSensitivity uses that block's observed active liquidity for an in-tick execution quote. Neither is a guaranteed executable route or forecast of order flow.",
        "ownerGas": "Sampled historical mint+burn 0.000013802 ETH per position charged. New pool initialization, token launch, creator fee sweep/claim, and N redemption gas are unmeasured and excluded; these make reported profits optimistic.",
        "creatorFee": "Pons N creator receives full tax plus 70% of its 1% base fee, using current hook 30% protocol share and buyback disabled. Self-paid launch/redemption creator fees are internal wallet transfers, included in both spending and receipts. Third-party creator fees shown separately.",
        "limitations": ["Counterfactual N has no historical price or actual liquidity; its curve starts from current verified factory config and a $104-wallet launch scenario.",
                        "Historical X/ETH swap notional is used as a cap, but it is not contemporaneous executable X quote depth. The model prices the X leg at post-swap marginal price with Pons fee but no price impact, an optimistic assumption.",
                        "The proposed X/N pool changes routing, so historical X market path may not survive this intervention. No on-chain fork reproduces all 19 invented pools or third-party arbs.",
                        "A new X/N v4 pool may need initialization gas, which is excluded. If activation cannot be routed, actual core and creator fees are zero.",
                        "Only the historical X signal venue is treated as the external market. Competing X/N pools, MEV, failed trades, and Pons N snipe-window tax are excluded."],
        "grid": grid,
        "tickSpacingSensitivity": spacing,
        "exactExternalSingleTickSensitivity": exact_external_tick,
        "bestRowDetails": best_detail,
    }
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({"output": str(OUTPUT), "rows": len(grid),
                      "best": max(grid, key=lambda r: r["walletProfitUsd"]),
                      "worst": min(grid, key=lambda r: r["walletProfitUsd"])},
                     indent=2))


if __name__ == "__main__":
    main()
