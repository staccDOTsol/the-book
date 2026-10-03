# Interim X/Q fee harvest keeper

`pons_harvest_keeper.py` looks for claimable X and Q fees on active three-position X/Q pools, including pools whose price has not entered a band. It simulates `executor.simulateHarvest(X)` from Q at one pinned block, quotes X→ETH through the live Pons curve or graduated pool, and quotes Q fees through the executable Q/ETH pool. It requires either **conservative ETH cash from an X sale** or **Q burn mark-to-market ETH value** to exceed twice the configured gas ceiling for all three transactions: configure, poke, and process. Q-only burns send no ETH to reimburse the signer; that branch is an economic value estimate and keeps unsold X fees reserved for a later harvest or exit. The keeper requotes Q value immediately before processing a queued Q-only claim. An unavailable route, stale block, or claim below the gas margin yields a waiting result without signing.

The signed `HarvestConfig` contains minimum X/Q fees, minimum ETH and Q swap outputs, gross ETH value, estimated whole-cycle gas, and a short deadline. The keeper sends `Q.configureHarvest(X, config)`, `PositionInspector.pokeHarvest(X)`, and, if Q selects the harvest, `Q.processNext()`. An eligible exit has scheduler priority; a queued harvest waits through exit retry or another selected action without paying to configure it again. Gas estimates and EIP-1559 fee caps are checked before each submission.

## Run

Use the repository's Python environment and set `PONS_HTTP_RPC_URL`, `PONS_CHAIN_ID`, `PONS_Q_ADDRESS`, and `PONS_PRICE_GUARD_ADDRESS`. Run a read-only cycle first, with an inclusive Q deployment block if the shared exit cursor has not yet been initialized:

```sh
.venv/bin/python forensics/launch_cursor/pons_harvest_keeper.py --start-block Q_DEPLOY_BLOCK --once
```

For signed operation, set `PONS_EXIT_CONFIGURATOR_PRIVATE_KEY` and run `--live --once` under the supervisor. The harvest and exit keepers share `.local/pons-exit-keeper.json`, the exit configurator lock, and one nonce stream. Either keeper recovers a pending transaction from the other before signing a new one. `--no-process-next` leaves scheduler execution to another worker. Use `--help` to set confirmation depth, block span, quote haircuts, TTL, and per-transaction gas and fee caps.

The supervisor schedules exit first, then harvest in the same signer lane. A pending exit or harvest transaction blocks the other workflow until receipt recovery. The price configurator and owner watcher use separate accounts and journals.

No transaction was broadcast while building or testing this keeper. The mock tests cover pinned quotes, X cash and Q-only mark-to-market gas tests, block reorg rejection, payload scope, queued harvest repricing, and temporary process gas failure.
