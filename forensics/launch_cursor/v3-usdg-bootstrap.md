# Replacement Q: Pools.xyz first, Q/USDG v3 second

Status: **deployed and bootstrapped on Robinhood**. The corrected
`LaunchCursorTokenV2` is `0x623B5374c4CB838DA24EE9F48F08664337936a06`.
Its Pools.xyz Q/ETH launch and canonical Q/USDG v3 bootstrap both mined.
See [live-deployment.md](live-deployment.md) for the receipts, actual v3 LP
NFT, keeper mode, and current dequeue evidence. The earlier Q cannot be
upgraded to trigger its queue on v3 pool transfers.

`V3UsdGBootstrap.sol` is the second stage only. First, the Q owner deploys V2
with the same name, symbol, description, and image URI as live Q; fixes the
canonical Robinhood v3 factory, USDG, and 0.30% fee in V2's constructor; and
launches its entire initial supply through Pools.xyz Instant Launch. That
creates the original locked v4 Q/ETH position and its creator fee path. The
owner must verify the mined launch receipt and locked NFT ownership before
deploying the bootstrap contract. Its constructor checks the v4 pool key is
initialized, but that check alone cannot prove the pool was created by the
Instant strategy.

The bootstrap contract constructor takes `(replacementQ, QOwner)`. It checks
Q's immutable owner, v3 factory/USDG/fee, and exact live-Q `name`, `symbol`,
and `tokenURI`. It also checks the deployed Robinhood v3 position manager and
router point at the expected factory. `QOwner` remains Q's immutable owner;
the bootstrap contract does not take that role.

One `bootstrap(Plan)` call performed these operations atomically:

1. Check the canonical Q/USDG 0.30% v3 pool does not exist and the v4 Q/ETH
   0.25% pool is initialized.
2. Acquire Q with exact `msg.value` ETH through the existing hookless v4
   Q/ETH pool, with a minimum Q output; or pull explicitly approved Q
   inventory from the owner when `msg.value == 0`.
3. Pull the owner's approved USDG, create and initialize the fresh v3 pool at
   the supplied square root price, and mint the first two-token v3 LP NFT
   directly to the Q owner. Both Q and USDG amounts have positive minima.
4. Bind V2's transfer trigger to that now-liquid, factory-registered pool.
   The initial LP transfers happen before binding.
5. Spend the full specified USDG buy amount through the deployed Router02,
   with positive minimum Q output and a square root price limit. The bought
   Q goes directly to the owner. Return unused LP Q and USDG to the owner.

Every step reverts together if a guard fails. The owner must approve the
bootstrap contract for `usdgForLp + usdgForBuy` and, in inventory mode,
`qInventoryIn` before the call. The LP NFT, bought Q, and refunds belong to
the owner. The bootstrap contract does not mint Q or retain a v3 NFT. The
original Pools.xyz v4 NFT remains owned by its fee splitter.

## Price and queue policy

`initialSqrtPriceX96` is the v3 raw-token price, with token0 and token1 sorted
by address. The owner must derive it from a reviewed Q/USDG valuation and
token decimals; it is not the existing v4 pool's price encoding. The fork
fixture below uses a synthetic 1:1 **raw-unit** start only to prove contract
mechanics. It is not a live launch quote. `tickLower` and `tickUpper` must
straddle that start to make both sides of the LP active.

Once bound and automatic processing is enabled, a Q transfer involving the
v3 Q/USDG pool requires one successful queued executor action when an action
is ready. A failed or gas-starved action reverts the trader's swap. The owner
can use V2's emergency pause for an unexecutable plan. Trading through the
original v4 Q/ETH pool remains possible and does not inherit the v3 pool
transfer trigger; the existing v4 market remains the exit buyback route.

## Fork verification

At Robinhood block `79,297,729`, the fork test launched a fresh V2 Q through
the deployed Pools.xyz Instant strategy, then used the deployed v3 factory,
NFT position manager, and Router02. It passed the ETH→Q acquisition path,
the separately provided Q inventory path, and a forced first-buy failure
that rolled back pool creation, LP mint, binding, and USDG spending.

```sh
/Users/stacc/the-book/.local/foundry/forge test --root . \
  --contracts forensics/launch_cursor/V3UsdGBootstrapFork.t.sol \
  --match-contract V3UsdGBootstrapForkTest \
  --fork-url "$RH_RPC_URL" --fork-block-number 79297729 \
  --use 0.8.26 --via-ir --optimizer-runs 1 -vv
```

The test's successful bootstrap call used about 5.8 million gas at that fork
state. The production bootstrap later mined with **5,454,506 gas**. The fork
fixture is a mechanics and gas proof, not a production price or route quote;
the mined production receipt and current queue state are recorded separately.

Canonical deployment addresses are checked against [Uniswap's Robinhood
registry](https://github.com/Uniswap/contracts/blob/main/deployments/4663.md).
