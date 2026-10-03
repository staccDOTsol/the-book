#!/usr/bin/env python3
"""Fee-only Pons/independent-pool arbitrage hurdle and creator cashflow.

This is algebra, not an executable route quote or forecast. It assumes one
Pons V2 hop, one separate static-fee pool hop, fixed prices before the route,
and zero price impact. Pons fees can accrue in memecoin and need conversion
and a sweep before the creator receives ETH.
"""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, localcontext
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "forensics/results/pons-arb-fee-hurdles.csv"
JSON = ROOT / "forensics/results/pons-arb-fee-hurdles.json"
BPS = Decimal(10_000)
POOL_FEES_BPS = (100, 700, 2000, 5000, 7000)


def d(value: object) -> Decimal:
    return Decimal(str(value))


def pct_bps(bps: int | Decimal) -> Decimal:
    return d(bps) / BPS


def fee_hurdle(pons_fee_bps: int, independent_pool_fee_bps: int,
               external_venue_fee_bps: int) -> Decimal:
    """Minimum pre-fee price premium for a two-hop loop to break even."""
    with localcontext() as context:
        context.prec = 45
        retention = ((1 - pct_bps(pons_fee_bps))
                     * (1 - pct_bps(independent_pool_fee_bps))
                     * (1 - pct_bps(external_venue_fee_bps)))
        if retention <= 0:
            raise ValueError("Combined fee retention must be positive")
        return 1 / retention - 1


def required_volume(cost_usd: Decimal, revenue_rate: Decimal) -> Decimal | None:
    return cost_usd / revenue_rate if revenue_rate > 0 else None


def build(*, external_venue_fee_bps: int = 0, base_pons_fee_bps: int = 100,
          protocol_share_bps: int = 3000, buyback_from_creator_bps: int = 0,
          volume_usd: Decimal = Decimal("100"), gas_usd: Decimal = Decimal("0.0359"),
          observed_fork_gross_loss_usd: Decimal = Decimal("1.153373"),
          observed_fork_gas_usd: Decimal = Decimal("0.0358852")) -> dict:
    for name, value in (("external_venue_fee_bps", external_venue_fee_bps),
                        ("base_pons_fee_bps", base_pons_fee_bps),
                        ("protocol_share_bps", protocol_share_bps),
                        ("buyback_from_creator_bps", buyback_from_creator_bps)):
        if not 0 <= value < 10_000:
            raise ValueError(f"{name} must be 0..9999 bps")
    if any(x < 0 for x in (volume_usd, gas_usd, observed_fork_gross_loss_usd,
                         observed_fork_gas_usd)):
        raise ValueError("Dollar inputs must be nonnegative")
    illustrative_fork_total = observed_fork_gross_loss_usd + observed_fork_gas_usd
    rows = []
    for tax_bps in range(0, 1001, 100):
        # Creator tax is entirely allocated to the creator. The creator's
        # cash share of the base fee is after protocol and optional buyback.
        base_creator_cash_rate = (pct_bps(base_pons_fee_bps)
                                  * (1 - pct_bps(protocol_share_bps))
                                  * (1 - pct_bps(buyback_from_creator_bps)))
        tax_rate = pct_bps(tax_bps)
        creator_cash_rate = tax_rate + base_creator_cash_rate
        for pool_fee_bps in POOL_FEES_BPS:
            hurdle = fee_hurdle(base_pons_fee_bps + tax_bps,
                                pool_fee_bps, external_venue_fee_bps)
            rows.append({
                "creatorTaxBps": tax_bps,
                "ponsBaseFeeBps": base_pons_fee_bps,
                "ponsTotalFeeBps": base_pons_fee_bps + tax_bps,
                "independentPoolFeeBps": pool_fee_bps,
                "externalVenueFeeBps": external_venue_fee_bps,
                "minimumPreFeePriceGapPct": str(hurdle * 100),
                "taxOn100UsdEquivalent": str(volume_usd * tax_rate),
                "creatorBaseFeeCashOn100UsdEquivalent": str(volume_usd * base_creator_cash_rate),
                "creatorTotalCashOn100UsdEquivalent": str(volume_usd * creator_cash_rate),
                "genuineVolumeForGasTaxOnlyUsd":
                    str(required_volume(gas_usd, tax_rate)) if tax_rate else None,
                "genuineVolumeForGasCreatorCashUsd":
                    str(required_volume(gas_usd, creator_cash_rate)) if creator_cash_rate else None,
                "genuineVolumeForIllustrativeForkLossPlusGasTaxOnlyUsd":
                    str(required_volume(illustrative_fork_total, tax_rate))
                    if tax_rate else None,
                "genuineVolumeForIllustrativeForkLossPlusGasCreatorCashUsd":
                    str(required_volume(illustrative_fork_total, creator_cash_rate))
                    if creator_cash_rate else None,
            })
    return {"schemaVersion": 1,
            "model": "(1+preFeeGap)*(1-ponsFee)*(1-independentPoolFee)*(1-externalVenueFee)=1; no size impact, no future-volume forecast",
            "cashflowScope": "The $100 volume is a quote-value-equivalent fee basis; exact-input Pons buys can accrue token-denominated fees, later converted at sweep. Values are pre-gas and pre-conversion impact. The illustrative fork loss is from a separate USDG LP strategy, not a NOTHINGBURGER outcome.",
            "assumptions": {"basePonsFeeBps": base_pons_fee_bps,
                            "protocolShareBps": protocol_share_bps,
                            "buybackFromCreatorBps": buyback_from_creator_bps,
                            "externalVenueFeeBps": external_venue_fee_bps,
                            "genuinePonsVolumeUsd": str(volume_usd),
                            "gasCostUsd": str(gas_usd),
                            "observedSeparateForkGrossLossUsd": str(observed_fork_gross_loss_usd),
                            "observedSeparateForkGasUsd": str(observed_fork_gas_usd),
                            "illustrativeSeparateForkLossPlusGasUsd": str(illustrative_fork_total),
                            "independentPoolFeesBps": list(POOL_FEES_BPS)},
            "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external-venue-fee-bps", type=int, default=0)
    parser.add_argument("--base-pons-fee-bps", type=int, default=100)
    parser.add_argument("--protocol-share-bps", type=int, default=3000)
    parser.add_argument("--buyback-from-creator-bps", type=int, default=0)
    parser.add_argument("--volume-usd", type=d, default=d("100"))
    parser.add_argument("--gas-usd", type=d, default=d("0.0359"))
    parser.add_argument("--fork-gross-loss-usd", type=d, default=d("1.153373"))
    parser.add_argument("--fork-gas-usd", type=d, default=d("0.0358852"))
    parser.add_argument("--csv", type=Path, default=CSV)
    parser.add_argument("--json", type=Path, default=JSON)
    args = parser.parse_args()
    result = build(external_venue_fee_bps=args.external_venue_fee_bps,
                   base_pons_fee_bps=args.base_pons_fee_bps,
                   protocol_share_bps=args.protocol_share_bps,
                   buyback_from_creator_bps=args.buyback_from_creator_bps,
                   volume_usd=args.volume_usd, gas_usd=args.gas_usd,
                   observed_fork_gross_loss_usd=args.fork_gross_loss_usd,
                   observed_fork_gas_usd=args.fork_gas_usd)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result["rows"][0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["rows"])
    print(json.dumps({"json": str(args.json), "csv": str(args.csv),
                      "rows": len(result["rows"]), "assumptions": result["assumptions"]}))


if __name__ == "__main__":
    main()
