# 0.1% mint: executable route counterfactual

This is a **read-only Robinhood fork test**, not a historical return series or
live deployment. The test is [`PointOnePercentRouteFork.t.sol`](PointOnePercentRouteFork.t.sol).
It pins chain 4663 at block **79,257,966**, hash
`0x9e7395e9ec4985ea0665239c2652cbba0123ddbc458d7ec9f7c3e0fe0644dc64`,
timestamp 2026-10-03 17:22:45 UTC, and base fee **23,412,000 wei/gas**.
The sampled active Pons token is
`0xeB765696eE5905ce1D06D72280dEFB2cE426115d` with curve
`0x9d4bcCd80332ba9CcCC75657B560Bb89265462aD`.

The fork creates a local custom Q, launches its 1-billion initial supply
through the canonical fees-on Pools.xyz existing-token route, and buys Q with
0.01 ETH to activate the **real v4 Q/ETH pool**. The Q/ETH LP NFT is locked in
the Pools strategy fee splitter, not owned by the temporary test trader.
One X/Q pool then receives three newly minted, Q-only 0.1%-of-current-supply
positions, using the strategy's `p0=0.01 Q/X`, `R=12.25`, 60-tick bands and
95% target utilization. Net newly issued Q entering LP is **2,852,850.95 Q**.
The test trader buys X on the real Pons curve, sells exact-input X into the
new X/Q pool, and sells exact-input Q through the newly launched Q/ETH pool.
Both v4 legs require complete fills. The trader and Q contracts are local to
the fork; no transaction is broadcast.

## Two accounting views

| One forced 5%-fee route, then 120-minute exit | Amount |
| --- | ---: |
| Pons X buy | 0.000300000000000000 ETH |
| X obtained | 158,903.317151381295775779 X |
| Q received from X/Q | 157,288.310637433286116130 Q |
| ETH received from Q/ETH sale | 0.000396316557570271 ETH |
| **Trader route margin before gas** | **+0.000096316557570271 ETH** |
| Developer cash from three routed exits | 0.000059407500000001 ETH |
| Wizards fanout from three routed exits | 0.000059407500000000 WETH |
| **Both recipients' combined cash before strategy gas** | **0.000118815000000001 ETH-equivalent** |
| Q minted into X/Q, executable Q/ETH liquidation at entry | 0.007168935904928608 ETH |
| Q burned at exit, executable Q/ETH liquidation at exit | 0.006890748318334749 ETH |
| **Partial issuer mark before gas**: recipients + burn mark − entry mint mark | **−0.000159372586593858 ETH** |

The developer receives only the first cash line. The other payout goes to
the Wizards fanout as WETH; it is not developer wallet cash. Giving the
developer responsibility for *all* per-launch strategy gas makes that wallet
negative even when the two recipients' aggregate cash is positive. At this
fork's base fee, the three enqueue/configure/open calls use **1,738,788**
execution gas and the winddown plus three configure/exit pairs use
**2,383,962**. Adding 21,000 base gas for each of those ten transactions
gives **4,332,750 gas**, or **0.000101438343 ETH**. Thus combined recipient
cash after this gas is **+0.000017376657000001 ETH**, while the developer's
own cash less all that gas is **−0.000042030842999999 ETH**. The combined
cash break-even gas price is **0.0274225 Gwei**; at 0.028 Gwei it is already
negative. At the configured 1 Gwei gas-price ceiling it is materially
negative. This excludes the one-time Q launch, first Q buy, deployment,
signer funding, priority fees, and any failed/retried transactions.

The **partial issuer mark** asks a different question: what ETH could the
newly issued Q deposited at opening buy if sold on the executable Q/ETH route,
versus the executable value of the Q burned on exit plus recipient cash? It
is **−0.000260810929593858 ETH after the measured per-launch gas**. The
valuation sells an equal Q amount in a reverted fork snapshot, so it does
not count hypothetical sale proceeds as cash actually received. It omits the
change in the locked Q/ETH LP NFT's principal value, unclaimed Q/ETH fees,
any beneficiary fee claim, and other Q holders' mark. It is therefore a
*partial consolidated mark*, not a complete tokenholder P&L.

## No-trade baseline and fee sensitivity

With no X/Q swap, the 120-minute winddown withdraws and burns the issued
2,852,850.95 Q except for **3 wei of Q rounding dust**. Its entry and exit
executable liquidation values are both **0.007168935904928608 ETH**, so the
Q mark is zero before gas. Recipient cash is zero. The opening and winddown
use 1,738,789 + 1,684,030 execution gas; adding ten base transaction costs
gives 3,632,819 gas, or **−0.000085051558428 ETH** at the pinned base fee.
An untouched launch is therefore a gas expense, not a profit.

At the same fork state, five separate trade-size trials were each reverted
to the same opening snapshot. The table shows trader ETH margin **before gas**
after the complete Pons buy → X/Q sell → Q/ETH sell route:

| Pons ETH in | X/Q fee 5% | X/Q fee 50% |
| ---: | ---: | ---: |
| 0.00001 | +0.000005461475 | −0.000001839691 |
| 0.00003 | +0.000015845696 | −0.000005670087 |
| 0.00010 | +0.000046849478 | −0.000020614252 |
| 0.00030 | +0.000096316558 | −0.000075404469 |
| 0.00100 | −0.000022459022 | −0.000375762144 |

For the 0.0003 ETH, 5% trial, the three trader calls used roughly 384,055
execution gas in the isolated sweep. With three transaction base charges at
the pinned base fee, the route remains positive by about **0.00008585 ETH**.
The full-cycle test measured 473,974 execution gas for the same three calls
in its different access/warmth context; even that higher figure stays
positive. At 50%, these **five sampled sizes** are negative before gas, so a
rational trader would not take these specific routes at this state. This
does not rule out every size, future X price, another external route, or an
arbitrageur that receives fees elsewhere.

## Limits of the inference

- The test forces one trader sequence at one Pons launch and one historical
  block. It contains no organic volume, no observed probability of entering a
  band, and no cadence estimate.
- The position starts Q-only; after the trade, the test advances time by 120
  minutes with no further organic transactions. Timed exits use real
  PositionManager burns and real Pons X sale/Q buy routes, but only **1-wei
  output minima**. They prove liveness here, not safe slippage limits.
- The trade uses a local counterfactual trader contract and exact full-fill
  checks, with the real forked PoolManager and Pons curve. Q/ETH is a real v4
  pool initialized by the canonical launcher in this fork; its locally
  deployed Q and first 0.01 ETH buy did not exist at the historical block.
- The settlement test uses local WETH and Wizards fanout stand-ins to measure
  the recipient split. It does not verify the real fanout's receipt or
  distribution, nor claim the locked Q/ETH position's fees. The test
  beneficiary is the test contract.
- Gas units are measured around the named calls, with 21,000 added per
  strategy transaction. A real block's effective gas price and access-list
  warmth may differ. Offchain fork setup, quote simulations, and test helper
  deployment are excluded, as are one-time live deployment costs.

Reproduce without broadcasting:

```sh
set -a
source .local/pons-rpc.env
set +a
forge test --contracts forensics/launch_cursor/PointOnePercentRouteFork.t.sol \
  --match-contract PointOnePercentRouteForkTest --via-ir --optimize \
  --optimizer-runs 1 -vv
```
