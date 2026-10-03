#!/usr/bin/env python3
"""Read-only, integer-exact Pons V2 ETH curve launch and trade scenario.

The quote arithmetic follows the Pons V2 integration docs' quoteBuy and
quoteSell examples.  The factory config and launch fee are read from public
Robinhood Chain JSON-RPC when run; no key, signature, or transaction is used.

https://docs.ponsfamily.com/v2#getting-a-quote
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR, getcontext
import json
from urllib.request import Request, urlopen


getcontext().prec = 60
BPS = 10_000
WEI = 10**18
FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
RPC = "https://rpc.mainnet.chain.robinhood.com"
SELECTORS = {
    "launchConfigCount": "0xae72d871",
    "getLaunchConfig": "0x1cad862d",
    "launchFee": "0xcf3cf573",
    "maxCreatorTaxBps": "0xf325a5fb",
    "previewLaunchEconomics": "0xf718b78c",
    "snipeTaxStartBps": "0x50e25ac2",
    "snipeTaxSeconds": "0x6783774b",
}


@dataclass(frozen=True)
class CurveConfig:
    supply: int
    base_fee_bps: int
    phantom_quote: int
    graduation_threshold: int
    pool_fee_pips: int
    tick_spacing: int
    enabled: bool


@dataclass(frozen=True)
class CurveState:
    quote_reserve: int
    token_reserve: int
    reserved_tokens: int
    # The real reserve is the quote deposited into constant-product pricing;
    # accrued fees are not counted here.
    real_quote_reserve: int

    @property
    def sellable_tokens(self) -> int:
        return self.token_reserve - self.reserved_tokens


@dataclass(frozen=True)
class BuyQuote:
    tokens_out: int
    quote_spent: int
    quote_refund: int
    base_fee: int
    creator_tax: int
    snipe_tax: int
    quote_to_curve: int


@dataclass(frozen=True)
class SellQuote:
    quote_out: int
    gross_quote: int
    base_fee: int
    creator_tax: int


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    if amount_in < 0 or reserve_in <= 0 or reserve_out <= 0:
        raise ValueError("invalid constant-product inputs")
    return amount_in * reserve_out // (reserve_in + amount_in)


def amount_in(amount_out_wanted: int, reserve_in: int, reserve_out: int) -> int:
    if not 0 <= amount_out_wanted < reserve_out or reserve_in <= 0:
        raise ValueError("invalid constant-product output")
    return amount_out_wanted * reserve_in // (reserve_out - amount_out_wanted) + 1


def opening_state(config: CurveConfig) -> CurveState:
    if min(config.supply, config.phantom_quote, config.graduation_threshold) <= 0:
        raise ValueError("nonpositive curve economics")
    reserved = (config.supply * config.phantom_quote //
                (config.phantom_quote + config.graduation_threshold))
    return CurveState(config.phantom_quote, config.supply, reserved, 0)


def quote_buy(config: CurveConfig, state: CurveState, quote_in: int,
              creator_tax_bps: int, snipe_tax_bps: int = 0) -> BuyQuote:
    if quote_in < 0 or not 0 <= creator_tax_bps <= 1000 or snipe_tax_bps < 0:
        raise ValueError("invalid buy amount or tax")
    if state.sellable_tokens <= 0:
        raise ValueError("curve already ready to graduate")
    if snipe_tax_bps:
        snipe_tax_bps = min(snipe_tax_bps,
                            BPS - config.base_fee_bps - creator_tax_bps - 100)
    spent = quote_in
    fee = spent * config.base_fee_bps // BPS
    tax = spent * creator_tax_bps // BPS
    snipe = spent * snipe_tax_bps // BPS
    net = spent - fee - tax - snipe
    tokens = amount_out(net, state.quote_reserve, state.token_reserve)
    if tokens > state.sellable_tokens:
        tokens = state.sellable_tokens
        net_needed = amount_in(tokens, state.quote_reserve, state.token_reserve)
        gross = _ceil_div(net_needed * BPS,
                          BPS - config.base_fee_bps - creator_tax_bps - snipe_tax_bps)
        spent = min(gross, quote_in)
        fee = spent * config.base_fee_bps // BPS
        tax = spent * creator_tax_bps // BPS
        snipe = spent * snipe_tax_bps // BPS
        net = spent - fee - tax - snipe
    return BuyQuote(tokens, spent, quote_in - spent, fee, tax, snipe, net)


def after_buy(state: CurveState, quote: BuyQuote) -> CurveState:
    return CurveState(state.quote_reserve + quote.quote_to_curve,
                      state.token_reserve - quote.tokens_out,
                      state.reserved_tokens,
                      state.real_quote_reserve + quote.quote_to_curve)


def quote_sell(config: CurveConfig, state: CurveState, tokens_in: int,
               creator_tax_bps: int) -> SellQuote:
    if tokens_in < 0 or not 0 <= creator_tax_bps <= 1000:
        raise ValueError("invalid sell amount or tax")
    if state.sellable_tokens <= 0:
        raise ValueError("curve already ready to graduate")
    gross = amount_out(tokens_in, state.token_reserve, state.quote_reserve)
    fee = gross * config.base_fee_bps // BPS
    tax = gross * creator_tax_bps // BPS
    return SellQuote(gross - fee - tax, gross, fee, tax)


def after_sell(state: CurveState, tokens_in: int, quote: SellQuote) -> CurveState:
    if quote.gross_quote > state.real_quote_reserve:
        raise ValueError("sell exceeds real reserve")
    return CurveState(state.quote_reserve - quote.gross_quote,
                      state.token_reserve + tokens_in,
                      state.reserved_tokens,
                      state.real_quote_reserve - quote.gross_quote)


def rpc_call(selector: str, arguments: list[int] | None = None,
             rpc: str = RPC, block: str = "latest") -> str:
    data = selector + "".join(f"{x:064x}" for x in (arguments or []))
    request = Request(
        rpc,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                         "params": [{"to": FACTORY, "data": data}, block]}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "rig-monitor/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if "error" in result or "result" not in result:
        raise RuntimeError(f"eth_call failed: {result}")
    return result["result"]


def _words(data: str) -> list[int]:
    if not data.startswith("0x") or (len(data) - 2) % 64:
        raise ValueError("invalid eth_call ABI result")
    return [int(data[i:i + 64], 16) for i in range(2, len(data), 64)]


def read_factory(rpc: str = RPC, block: str = "latest") -> tuple[CurveConfig, int, int, str]:
    count = _words(rpc_call(SELECTORS["launchConfigCount"], rpc=rpc, block=block))[0]
    if count != 1:
        raise ValueError(f"expected only config 0; found {count} configs")
    values = _words(rpc_call(SELECTORS["getLaunchConfig"], [0], rpc, block))
    if len(values) != 7:
        raise ValueError("unexpected LaunchConfig ABI")
    config = CurveConfig(*values[:6], bool(values[6]))
    launch_fee = _words(rpc_call(SELECTORS["launchFee"], rpc=rpc, block=block))[0]
    tax_cap = _words(rpc_call(SELECTORS["maxCreatorTaxBps"], rpc=rpc, block=block))[0]
    economics_pin = rpc_call(SELECTORS["previewLaunchEconomics"], [0, 0], rpc, block)
    return config, launch_fee, tax_cap, economics_pin


def ether(raw: int) -> str:
    return format(Decimal(raw) / WEI, "f")


def tokens(raw: int) -> str:
    return format(Decimal(raw) / WEI, "f")


def marginal_price_eth_per_token(state: CurveState) -> Decimal:
    """Pre-fee curve spot. Quote an actual trade for impact and tax."""
    return Decimal(state.quote_reserve) / Decimal(state.token_reserve)


def x_per_n_price_from_eth_price(x_eth_per_token: Decimal,
                                 n_state: CurveState) -> Decimal:
    """X units for one N from an X price in ETH; a marginal reference only."""
    if x_eth_per_token <= 0:
        raise ValueError("X/ETH price must be positive")
    return marginal_price_eth_per_token(n_state) / x_eth_per_token


def n_per_x_price_from_eth_price(x_eth_per_token: Decimal,
                                 n_state: CurveState) -> Decimal:
    """N units for one X from an X price in ETH; a marginal reference only."""
    if x_eth_per_token <= 0:
        raise ValueError("X/ETH price must be positive")
    return x_eth_per_token / marginal_price_eth_per_token(n_state)


def fee_aware_n_per_x_quote(config: CurveConfig, n_state: CurveState,
                            x_eth_per_token: Decimal, x_token_amount: Decimal,
                            creator_tax_bps: int,
                            snipe_tax_bps: int = 0) -> dict:
    """Buy-side reference for X/N pool birth at a specified X trade size.

    The hypothetical trade first exchanges the ETH value of `x_token_amount`
    X on the N/ETH Pons curve. It includes N's base fee, creator tax, snipe tax
    if any, and curve impact. It does not model an actual X/N pool fill, or
    prove a profitable arb route.
    """
    if x_eth_per_token <= 0 or x_token_amount <= 0:
        raise ValueError("X price and amount must be positive")
    wei_in = int((x_eth_per_token * x_token_amount * WEI).to_integral_value(
        rounding=ROUND_FLOOR))
    if wei_in <= 0:
        raise ValueError("X notional rounds to zero wei")
    buy = quote_buy(config, n_state, wei_in, creator_tax_bps, snipe_tax_bps)
    return {
        "xTokenAmount": str(x_token_amount),
        "xEthPerToken": str(x_eth_per_token),
        "nCurveEthInput": ether(wei_in),
        "nReceived": tokens(buy.tokens_out),
        "nPerXAtThisSize": str((Decimal(buy.tokens_out) / WEI) / x_token_amount),
        "nCurveBaseFeeEth": ether(buy.base_fee),
        "nCurveCreatorTaxEth": ether(buy.creator_tax),
        "nCurveSnipeTaxEth": ether(buy.snipe_tax),
        "nCurveQuoteRefundEth": ether(buy.quote_refund),
    }


def scenario(config: CurveConfig, dev_buy_wei: int, taxes: list[int]) -> list[dict]:
    state = opening_state(config)
    rows = []
    for tax_bps in taxes:
        buy = quote_buy(config, state, dev_buy_wei, tax_bps)
        after = after_buy(state, buy)
        immediate_sell = quote_sell(config, after, buy.tokens_out, tax_bps)
        rows.append({
            "creatorTaxBps": tax_bps,
            "devBuyInputEth": ether(dev_buy_wei),
            "devTokens": tokens(buy.tokens_out),
            "devTokenSupplyPct": str(Decimal(buy.tokens_out) / config.supply * 100),
            "quoteToCurveEth": ether(buy.quote_to_curve),
            "baseFeeEth": ether(buy.base_fee),
            "creatorTaxEth": ether(buy.creator_tax),
            "realQuoteReserveEth": ether(after.real_quote_reserve),
            "pricingQuoteReserveEth": ether(after.quote_reserve),
            "tokenReserve": tokens(after.token_reserve),
            "sellableTokens": tokens(after.sellable_tokens),
            "graduationProgressPct": str(Decimal(after.real_quote_reserve) /
                                         config.graduation_threshold * 100),
            "marginalPriceEthPerToken": str(Decimal(after.quote_reserve) /
                                            Decimal(after.token_reserve)),
            "immediateSellGrossEth": ether(immediate_sell.gross_quote),
            "immediateSellNetEth": ether(immediate_sell.quote_out),
            "immediateSellLossBeforeCreatorFeeClaimEth":
                ether(dev_buy_wei - immediate_sell.quote_out),
        })
    return rows


def main() -> None:
    config, launch_fee, tax_cap, economics_pin = read_factory()
    snipe_start_bps = _words(rpc_call(SELECTORS["snipeTaxStartBps"]))[0]
    snipe_seconds = _words(rpc_call(SELECTORS["snipeTaxSeconds"]))[0]
    dev_buy = 44 * 10**15
    total_wei = 40 * 10**15  # $104 at the backtest's fixed $2600/ETH.
    # These are sensitivity reserves, not an estimate of all future LP gas.
    gas_reserves = [0, 3 * 10**14, 10**15, 2 * 10**15]
    affordable = []
    for gas_reserve in gas_reserves:
        affordable_buy = total_wei - launch_fee - gas_reserve
        if affordable_buy <= 0:
            continue
        affordable.append({
            "totalWalletEth": ether(total_wei),
            "launchFeeEth": ether(launch_fee),
            "gasReserveEth": ether(gas_reserve),
            "devBuyEth": ether(affordable_buy),
            "remainingEthAfterLaunchAndBuy": ether(gas_reserve),
            "rows": scenario(config, affordable_buy,
                             list(range(0, tax_cap + 1, 100))),
        })
    output = {
        "source": "RH public JSON-RPC; Pons V2 docs integer quote formulas",
        "factory": FACTORY,
        "launchConfigId": 0,
        "config": asdict(config),
        "launchFeeEth": ether(launch_fee),
        "maxCreatorTaxBps": tax_cap,
        "snipeTaxStartBps": snipe_start_bps,
        "snipeTaxSeconds": snipe_seconds,
        "expectedEconomicsPin": economics_pin,
        "initialReservedTokens": tokens(opening_state(config).reserved_tokens),
        "initialSellableTokens": tokens(opening_state(config).sellable_tokens),
        "affordableFrom104UsdAt2600PerEth": affordable,
        "draft044EthSensitivityInfeasibleFrom104Usd": True,
        "rows": scenario(config, dev_buy, list(range(0, tax_cap + 1, 100))),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
