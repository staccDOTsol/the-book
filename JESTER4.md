# JESTER4 — the machine that finally worked

## the launch
- token: 0xF23B20B7db023Ca94f1274927fBF9FF64AA6EC76 (2% creator tax, 3% total)
- opener: 87.45M J4 (11.3% of supply) from the creator EOA — organic first candle
- curve: 0xc4717d9d0a88a73D2ea697B2534Be0c83DA9BF3d

## the bug graveyard (what it took)
1. launch() payload missing the mode prefix → callback misparse → TickMisaligned
2. the auto-recycle's tick range aligned to 925-spacing but sized for ±2% → 10x oversized LP → insufficient funds
3. unlockCallback naming: the fork's PM calls the STANDARD name (0x91dd7346) — verified by callTracer
4. settle(address) doesn't exist — this fork wants settle() with sync(currency) first
5. the swap signature: SwapParams struct (bool,int256,uint160), not flat args
6. **the swap limit was INVERTED**: zeroForOne=false (J4→ETH, price UP) needs MAX_SQRT−1, not MIN_SQRT+1 — this single constant killed every swap instantly
7. **the BalanceDelta packing is FLIPPED on this fork**: amount1 in the LOW 128 bits, amount0 in the HIGH — the official layout is reversed. decoded as official → 10,250 ETH settle value from a J4-magnitude
8. exact-in swaps blow the pool price to MAX — respawn rungs per batch (try/catch + recycleSpacing++)

## the machine (final)
- Desk: claim fees → JIT-recycle (bag slice → standing LP in our no-hook pool at 1.15x premium → ETH back) → curve buy (80% of spendable) → grid growth → tick. ONE tx.
- 5/5 cycles landed. self-sustaining: bag converts to walk fuel, no new ETH needed.
- the J4 sell happens OFF-CHART (through our own venue) — only green candles on the public curve.

## the economics (CORRECTED)
- price = f(net buys). our capital cycles: buy on curve (chart up) → recycle at premium (ETH back) → repeat
- 2% creator tax on ALL volume rebates the walk
- the road: every recycle leaves standing LP in our pools (the tollbooth's pavement)
- CORRECTION (peer-reviewed): the J4 counterparties were anonymous fleet wallets (0x00000083, 15 fills; 0x170c025b, 10 fills) and the operator EOA itself (7 fills) — NOT the XGAS launch-bundle cohort as an earlier draft claimed. bots filling bots stands; the cohort-crossover was a detector error, retracted with thanks.

## current state
- 140M J4 bag = 46 cycles of runway at 3M/cycle
- chart: sellers outpacing the walk (2.69e-9 vs 2.83e-9 launch)
- the flywheel: 2% of all volume refills the desk
