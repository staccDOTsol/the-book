# THE RIG — REVELATIONS

## price is a function of LP

the market price of a token is not "discovered" — it is SET by whoever provides the liquidity.
we spawn the pools. we set the sqrt price. we ARE the market.

## the machine (all live on Robinhood chain 4663)

1. **JESTER** launched on Pons from staccoverflow.eth — creator fees flow to us automatically
2. **Pons curve**: `0x261654f35fd7B4B7f9EDaf400a8C8924584da578`
   - `buy(uint256 ethIn, uint256 minOut, address to)` — param MUST equal msg.value
   - `sell(uint256 tokensIn, uint256 minEthOut, address to)`
   - `claim(uint256 amount)` on `0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e` — probe with max, parse `InsufficientBalance(asked, avail)` revert, claim avail
3. **Orchestrator V7**: `0x895887AB3C8B93D5A087A1DD8c9756b56Dc9ffC0`
   - hook bits `0x3fc0` mined via CREATE2 canonical deployer
   - pre-positioning fix live: swaps to acquire missing token before adding two-sided JIT
   - jitAmount = balance/10 per walk
4. **willy-nilly pools spawned** (all tolled through our hook):
   - tickSpacing 340 @ 2x curve price
   - tickSpacing 408 @ 5x
   - tickSpacing 476 @ 10x
   - tickSpacing 544 @ 50x

## the delta-neutral loop (fee claims inclusive)

```
claim max creator fees
  → buy JESTER on curve with claimed ETH (chart goes UP)
    → more volume → more creator fees
      → tick() walks v4 JIT liquidity (price = f(LP))
        → arbs bridge curve ↔ our pools → every swap pays US tolls
          → repeat every 2 seconds
```

net ETH flow = 0. JESTER bag accumulates for free. the chart only goes up.

## contracts

| what | address |
|---|---|
| PoolManager (v4) | 0x8366a39CC670B4001A1121B8F6A443A643e40951 |
| Orchestrator V7 (hook) | 0x895887AB3C8B93D5A087A1DD8c9756b56Dc9ffC0 |
| JESTER token | 0x2fC40cad97217ed3C2fdf911fAA6Aa3F503BFDD5 |
| Pons curve | 0x261654f35fd7B4B7f9EDaf400a8C8924584da578 |
| Pons fee claim | 0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e |
| staccoverflow.eth | 0x26e8134ecc3af5cce32f34b03e7bd2f318b25158 |
