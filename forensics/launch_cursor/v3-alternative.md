# 🧙‍♂️ Q/USDG v3 trading market: design record

Status: **historical proposal, now deployed as the corrected Q route**. The
current deployment and runtime are recorded in [live-deployment.md](live-deployment.md)
and [Fly runtime](fly/README.md). The earlier Q at
`0x0E34d0792032Ffc54C058751cF048dA347193472` cannot use this route to
advance its queue because its deployed `_update` excludes routed transfers.

The corrected Q at `0x623B5374c4CB838DA24EE9F48F08664337936a06`
mirrors the earlier token metadata: name, symbol, and description `🧙‍♂️`,
plus the pinned [`wizard-token.png`](../../assets/wizard-token.png) image.
There was no holder balance migration.

## Deployed markets and launch

- Corrected Q launched into the native ETH/Q v4 pool through Pools.xyz Instant
  Launch. This market retains the Q/ETH price guard, exit buyback route,
  locked LP, and creator fee path.
- The canonical Q/USDG Uniswap v3 0.30% pool is
  `0xBe8DC1ef3F78B00050247267619517a1D22D1589`. Its ERC-20 Q transfers
  make a trader crossing the v3 side, including an arbitrageur trading
  between v3 and v4, pay gas for one ready queue action.
- The strategy's X/Q positions remain on Uniswap v4. Whether Pools.xyz's
  website will list or manage those externally created custom positions is
  unverified.
- The [atomic bootstrap](live-deployment.md#mined-route) created and
  initialized the v3 pool, minted its first LP position, bound Q's transfer
  trigger, and made a USDG→Q buy in one transaction at block 79,310,824.
  The NFT is #1378241 and belongs to the Q owner.

The second pool is a separate v3 LP NFT and accrues its configured v3 LP fee
to its owner. It does not replace the locked Pools.xyz v4 market. V4-only Q
trades bypass the v3 transfer trigger; the route makes traders who cross the
v3 side pay for dequeue work. Pools.xyz Instant Launch consumed the initial
Q supply. The atomic bootstrap acquired Q for the second pool from the v4
market.

## Queue behavior and limits

- A v3 Q buy sends Q from the pool before its swap callback. A sell pays Q to
  the pool during the callback. Both call corrected Q's transfer code while
  the v3 pool is locked. The exit buyback uses the separate v4 Q/ETH pool, so
  it does not reenter the locked v3 pool.
- The corrected Q applies a strict rule to transfers through its bound v3
  pool: if a ready action exists, the transfer must complete one actual
  executor action or revert the trade. An empty queue leaves ordinary v3
  transfers unaffected. A stale or unexecutable queue root can temporarily
  prevent v3 trades, so the price and exit configurators still matter. The
  [deployment record](live-deployment.md#keeper-and-dequeue-status) tracks
  observed mined dequeue progress.
- V4 X/Q swaps can net Q deltas or use ERC-6909 claims without an ERC-20 Q
  transfer. The v3 pool therefore supplies a gas-paying arbitrage surface,
  not a universal tax on every economic Q trade.

Sources: [Uniswap v3 pool swap](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol),
[v3 pool initializer](https://github.com/Uniswap/v3-periphery/blob/main/contracts/base/PoolInitializer.sol),
[Robinhood v3 deployments](https://developers.uniswap.org/docs/protocols/v3/deployments),
[Liquidity Launchpad deployments](https://developers.uniswap.org/docs/liquidity/liquidity-launchpad/deployments),
and [Uniswap v4 PoolManager](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol).
