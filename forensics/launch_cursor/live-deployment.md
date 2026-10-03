# 🧙‍♂️ Robinhood live deployment

Chain ID: **4663**. This record contains public onchain identifiers and observed runtime state. The corrected Q below is the active deployment. Private signer material and RPC credentials stay in ignored local files and Fly secrets.

| Component | Address |
| --- | --- |
| Corrected Q token (`🧙‍♂️`) | `0x623B5374c4CB838DA24EE9F48F08664337936a06` |
| LP executor | `0x42287032A1994A77CAf22C2A3e7eAa98fd05A315` |
| Position inspector | `0xffbC7a0BC356d85e5196de34D00E3555C24f1124` |
| Price guard | `0x9755b28b7f69b105599691e220da7a7f582faf07` |
| Settlement router | `0x94af2c81c7b2520146652d69171bbbf4892d8116` |
| Canonical Q/USDG Uniswap v3 pool, 0.30% | `0xBe8DC1ef3F78B00050247267619517a1D22D1589` |
| Q/USDG bootstrap orchestrator | `0x88c20e286e5b12fa4e7fFe71559bDE03140d7f98` |
| Owner, fee beneficiary, developer | `0x26E8134eCC3af5cCE32f34B03E7BD2f318B25158` |
| Price configurator | `0xA52dA6820d7289AB4673d86B82f89f9DB7c0957f` |
| Exit configurator | `0x4F9543Ea44Fa2F925d7C74F3367711B5894708D6` |
| Wizards fanout | `0x1b88A6c6516FD2918905186F21Bb9F5CaA1a15c8` |

## Mined route

The corrected Q core was deployed at block **79,306,622**. Its entire initial 1 billion supply went into the locked Pools.xyz Instant Launch Q/ETH v4 position in transaction `0xf51753626d831774984de6114a09f84381226084526ea865e57ea9a0f27c35aa`, block **79,307,686**. The locked position began at a single-sided tick boundary, so active Q/ETH liquidity required the first ETH-to-Q trade.

The owner deployed the price guard in transaction `0x1baefd82a72a0b2c9a2bfe3e8b1b2fc6dec5d5af1a550e6beb66b8c33bdc4598`, block **79,310,240**, and the settlement router in transaction `0x3b28990b99f0466638f698e86ec4d4f0b1d2c2f2e58210d3fdb80272c9708095`, block **79,310,246**.

The atomic Q/USDG v3 bootstrap transaction `0xf18baa05403242003277ccaa463af25f228438d0b03ed18e3a676fea22370860` succeeded at block **79,310,824**. It acquired Q from the Pools.xyz Q/ETH pool with about **0.00077356 ETH**, created the canonical v3 Q/USDG pool, minted a two-sided LP, bound that pool as Q's transfer trigger, and performed a first USDG-to-Q buy. The minted v3 position NFT is **#1378241**; its onchain owner is the owner address in the table. The bootstrap spent **2.662119 USDG** in total: 1.996589 USDG for LP and 0.665530 USDG for the first buy. The receipt used **5,454,506 gas**.

Q activation transaction `0x0b29800b81faa47ef6be3b6663eefc8c1e59f08fbd6dda3dd911140a7e15a138` succeeded at block **79,311,964**. Onchain reads confirmed `automaticEnabled() == true` and `canonicalV3Pool()` equal to the pool above.

## Keeper and dequeue status

The Fly app `the-book-q-keeper` runs one Machine in `yyz` with `PONS_KEEPER_MODE=trader-paid`. Its supervisor reported `mode=live`, `writesEnabled=true`, `dequeueMode=trader_transfer`, successful price, exit, feedback, and harvest cycles, and zero consecutive failures at the **2026-10-03 18:59 UTC** check. The owner watcher has submitted enqueues; the pending entry stack rose from **4 at 18:59 UTC** to **8 at 19:04 UTC**. The currently ready action was an `Open` for a queued Pons launch. These are snapshots, not a guarantee of current health; inspect the latest Fly heartbeat and Q state before relying on the system.

**No trader-paid dequeue has mined yet:** `successfulExecutorSteps()` remained **0** at 19:04 UTC. A read-only `eth_call` simulated a direct Q transfer through the bound v3 pool and completed one ready step, but that simulation did not change chain state. A real Q/USDG v3 buy or sell that transfers Q through this pool is needed to test a mined dequeue. Q/ETH v4 trades do not trigger this route. If a ready executor action cannot complete within the trader's transaction, the v3 trade reverts; the owner can pause automatic processing if needed.

The local live writer was stopped before the Fly cutover. The Fly volume received four fresh corrected-Q journals and the retained budget journal; the unresolved signed `processNext()` transaction in the earlier Q price journal was not migrated. Fly signs enqueues and configuration work, while `--no-process-next` leaves dequeue gas to the v3 trader. See [Fly runtime](fly/README.md) and [v3 bootstrap](v3-usdg-bootstrap.md).

At 19:01 UTC the watcher hit its original **0.01 ETH** owner balance floor while the owner held about **0.00658 ETH**. The Fly supervisor retried the watcher. The floor was lowered to **0.001 ETH** in the trader-paid runtime and redeployed successfully; a fresh watcher cycle enqueued more launches. The other gas budget limits were unchanged.

At 19:14 UTC a separate owner-account transaction consumed nonce **56259** before the watcher's saved enqueue could mine. The saved enqueue had no receipt and its token remained unqueued. The watcher now checks the owner nonce and token stage at the same confirmed block before discarding that stale signed transaction; it replays the unchanged launch cursor with a fresh nonce. This recovery was deployed at 19:19 UTC, logged `pending_nonce_recovered`, and the missed token advanced to `Queued`. At 19:19:49 UTC the Fly watcher and worker loops were running with zero consecutive failures; Q had **20 pending entries** and **0 successful executor steps**. Shared owner-account activity can cause further nonce collisions, so watch for repeated recovery events.

## Superseded deployments

`0x0E34d0792032Ffc54C058751cF048dA347193472` is the earlier Q deployment. It has its own launch history and is **not** the corrected trader-paid Q. `0xa4054a5bfd747fcdac0aecb1380c4edbfccc912b` is a subsequently deployed, **unlaunched** replacement with 1 billion tokens still in the owner wallet; it was superseded after a transfer-trigger bug was found before Pools launch. Neither address should be used for the corrected Q dashboard, keeper, or v3 pool. No holder balances were migrated or burned.
