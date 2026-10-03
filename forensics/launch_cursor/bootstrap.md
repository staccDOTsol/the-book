# Hookless Pons X/Q bootstrap on Robinhood

Status: **corrected Q, Q/ETH Pools.xyz launch, Q/USDG v3 market, and trader-paid Fly keeper are live**. The corrected Q address, mined receipts, queue status, and prior deployments are in the [live deployment record](live-deployment.md). This document describes the bootstrap procedure and its earlier fork evidence; use the deployment record for current state. `PonsBootstrap.s.sol` reads public settings and checks live contract bindings. Its `preflightLaunch()` only returns calldata; the recorded existing-token launch was sent as one atomic launcher multicall.

## Verified route and pins

At Robinhood block **78,939,587** on 2026-10-03, chain ID was 4663 and the contracts below had code. Read calls confirmed that both current Uniswap Instant Launch variants point to the pinned launcher, PoolManager, PositionManager, and respective FeeSplitters, and expose `TOTAL_SUPPLY=1e27`, `LP_FEE=2500`, `TICK_SPACING=25`, and `initialTick=198050`. PoolManager bindings for PositionManager, StateView, Quoter, Pons factory and hook were also checked. The pinned launcher reported canonical Permit2.

| Role | Address |
| --- | --- |
| Uniswap v4 PoolManager | `0x8366a39CC670B4001A1121B8F6A443A643e40951` |
| PositionManager | `0x58daec3116aae6D93017bAAea7749052E8a04fA7` |
| StateView | `0xF3334192D15450CdD385c8B70e03f9A6bD9E673b` |
| v4 Quoter | `0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` |
| Pons V2 factory | `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` |
| LiquidityLauncher v3.2.0 | `0x0000FffFBE8efE702c8703aE3477FF5dE3d319C0` |
| Instant Launch v3.3.0, creator fees on | `0x7c48DDe3B447381F4d986334679b3Afc7F2D35C2` |
| Its FeeSplitter / beneficiary vault | `0x9411fa7F956f64aa7981AA27cB3bC6eC0415449C` / `0x26d2F7AcB07707034406a0dC458351Bb63C02553` |
| Instant Launch v3.3.0, creator fees off | `0xC9566675b1Ea42861546f3c5B74Ace2c79c49572` |
| Its FeeSplitter | `0x882Ae5e2095435A62Fd1BBDEfcb637f5CeAFc0ee` |
| Robinhood WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` |
| Squarefun wizard fanout candidate | `0x1b88A6c6516FD2918905186F21Bb9F5CaA1a15c8` |

The [Uniswap deployment registry](https://github.com/Uniswap/liquidity-launcher/blob/main/README.md) identifies the launcher and current Robinhood strategies; the [Uniswap SDK registry](https://github.com/Uniswap/sdks/blob/main/sdks/liquidity-launcher-sdk/src/addresses.ts) marks the v3.3.0 pair current and records its 25-tick pool shape. The [official Robinhood deployment list](https://github.com/Uniswap/contracts/blob/main/deployments/4663.md) supplies v4, Permit2, and WETH addresses. The [current InstantLaunchStrategy source](https://github.com/Uniswap/liquidity-launcher/blob/main/src/strategies/InstantLaunchStrategy.sol) is the source of the 1 billion supply, exact transfer, fee-beneficiary, pool, and locked NFT behavior. An older paragraph in Uniswap's Technical Reference says spacing 60; the current strategy source, SDK deployment entry, and live getters agree on **25** for this v3.3.0 route. Older launch generations can still have spacing 60.

The fanout address comes from the [strategy record](../record/160-pons-every-launch-pool-plan.md) and its [verified Sourcify contract](https://sourcify.dev/server/v2/contract/4663/0x1b88A6c6516FD2918905186F21Bb9F5CaA1a15c8?fields=abi,sources). The script pins the observed runtime code hash `0x384c9220050083b0efd1cac6ac47ea6901e68a0a10a9ff1ad3a06ddade6d21ae`. Confirm this is still the intended **Squarefun wizard** recipient in the live Squarefun product before binding; code and a historical record cannot establish current recipient intent. Supply the intended developer address explicitly; the script can check its relationship to the deployed router but cannot choose it.

## Stage order

1. **Core:** deploy `HooklessLPExecutor`, `PositionInspector`, then the custom 1 billion/18-decimal `LaunchCursorToken` Q. Bind Q as executor controller and inspector cursor. The script puts PoolManager, PositionManager, Permit2, launcher, selected launch strategy and FeeSplitter, Pons factory, and Quoter on Q's internal-endpoint list and turns off automatic cursor work. The initial Q supply remains with the deployer.
2. **Q launch:** choose one current Instant Launch variant. The fees-on variant sends 40% of native LP fees to its beneficiary vault; the rest of native fees and all token fees go to its compounding recipient. The fees-off variant has a zero beneficiary vault and all LP fees go to compounding. `configData` is always `abi.encode(feeBeneficiary)`: a nonzero address other than the launcher is required even in fees-off mode, where it is ignored. `preflightLaunch()` returns exact calldata for Q's `approve(Permit2)`, Permit2's `approve(Q, launcher)`, and one launcher `multicall([depositToken(Q, full supply), distributeToken(Q, selected strategy, full supply, configData, salt=0)])`. The first two approvals are separate owner transactions; deposit and distribute **must remain in one atomic multicall**. Never leave Q in the launcher between transactions: another caller could distribute it. See the [Uniswap Deployment Guide](https://github.com/Uniswap/liquidity-launcher/blob/main/docs/DeploymentGuide.md) and [LiquidityLauncher source](https://github.com/Uniswap/liquidity-launcher/blob/main/src/LiquidityLauncher.sol).
3. **Make Q/ETH executable:** inspect the `TokenLaunched` receipt for Q, selected strategy, expected FeeSplitter, and the minted NFT owner. The launch NFT can have positive position liquidity while StateView's *active pool liquidity* is zero at its initial single-sided upper boundary. `deployVaultBuyer()` and `preflightVaultBuy()` remain available for an optional first ETH→Q market buy to move the pool into range. The preflight quotes an exact ETH input, applies a bounded Q minimum, and returns short-lived calldata for an owner-only adapter. A first buy moved the pool into its LP range in the earlier fork exercise. The owner must review any ETH spend separately. This acquired Q is **not** the per-position X/Q funding source: each X/Q open mints three Q tranches in its one atomic transaction, each 0.1% of the then-current supply. A tenfold initial-supply ceiling can pause new opens until exits burn Q.
4. **Post-launch bindings:** after active Q/ETH liquidity exists, deploy `OpenPriceGuard` for offchain keeper verification, then deploy and bind `ExitSettlementRouter` with the pinned WETH/fanout and explicit developer. The executor has no price/depth guard binding. Add the router and its three child adapters to Q's internal endpoints. Assign distinct price and exit configurators. Automatic cursor work remains off.
5. **Activation:** verify the owner/watcher, price keeper, and exit/harvest keeper signers and intended recipients, then enable Q's automatic flag. The script checks the live Q/ETH pool, router recipients, and endpoint exclusions. The corrected Q activation has mined, and the Fly watcher and workers have completed live cycles. The bound Q/USDG v3 pool is the trader-paid transfer trigger; Q/ETH v4 trades do not run a queue step. The exit keeper remains necessary to wind down X/Q positions after 120 minutes, including untouched Q-only positions.

The stage functions comprise multiple onchain transactions if later broadcast. A failed later transaction can leave earlier deployments or one-time bindings in place. Reconcile every mined receipt and address before any continuation; `deployAfterLaunch()` refuses a partially bound executor.

## Read-only commands and required inputs

These examples are **simulations**. None contains `--broadcast`, a private key, or an account selector. Run from the repository root. Use a Robinhood RPC; a provider URL can be supplied privately through `RH_RPC_URL` without printing it. The public endpoint below is rate limited.

```sh
export RH_RPC_URL=https://rpc.mainnet.chain.robinhood.com
export PONS_DEPLOYER=0xYOUR_OWNER_EOA
export PONS_INSTANT_STRATEGY=0x7c48DDe3B447381F4d986334679b3Afc7F2D35C2  # or fees-off address above
export PONS_Q_NAME='🧙‍♂️'
export PONS_Q_SYMBOL='🧙‍♂️'
export PONS_Q_DESCRIPTION='🧙‍♂️'
export PONS_Q_IMAGE_URI='https://raw.githubusercontent.com/staccDOTsol/the-book/450d558b169408de1483c0540faa1aae72889a57/assets/wizard-token.png'
export PONS_RETRY_DELAY_SECONDS=30
export PONS_TRANSFER_STEP_GAS_LIMIT=3000000
export PONS_HARVEST_GAS_PRICE_CEILING_WEI=1000000000

/Users/stacc/.foundry/bin/forge script forensics/launch_cursor/PonsBootstrap.s.sol:PonsBootstrap \
  --sig 'deployCore()' --rpc-url "$RH_RPC_URL" --root . --use 0.8.26 --via-ir --optimizer-runs 1
```

The returned core addresses in a dry run are simulated. For later stages, `PONS_EXECUTOR_ADDRESS`, `PONS_INSPECTOR_ADDRESS`, and `PONS_Q_ADDRESS` must come from **actual verified deployment receipts**, not those dry-run predictions. Keep the same `PONS_DEPLOYER` and `PONS_INSTANT_STRATEGY` throughout.
The four metadata variables are optional because the script defaults to these
pinned UTF-8 values; if supplied, each must match exactly. This catches stale
name, symbol, description, or image settings before deployment.

```sh
export PONS_EXECUTOR_ADDRESS=0xMINED_EXECUTOR
export PONS_INSPECTOR_ADDRESS=0xMINED_INSPECTOR
export PONS_Q_ADDRESS=0xMINED_Q
export PONS_FEE_BENEFICIARY=0xINTENDED_NONZERO_BENEFICIARY
export PONS_PERMIT2_EXPIRATION=2000000000  # replace with a reviewed future Unix time

/Users/stacc/.foundry/bin/forge script forensics/launch_cursor/PonsBootstrap.s.sol:PonsBootstrap \
  --sig 'preflightLaunch()' --rpc-url "$RH_RPC_URL" --root . --use 0.8.26 --via-ir --optimizer-runs 1
```

Review the returned targets/calldata and execute them from the Q owner only after the recipient and launch choice are approved. The atomic launch call uses **zero native ETH**. The script does not submit those calls. The initial supply fits both `uint160` Permit2 and `uint128` strategy amount fields. Do not use the ordinary Pools.xyz creation UI for this custom token path: its support and listing behavior for externally supplied Q are unverified.

After a confirmed Q launch and its NFT/receipt review, simulate deployment of the restricted buyer. Record its actual mined address if later deployed:

```sh
/Users/stacc/.foundry/bin/forge script forensics/launch_cursor/PonsBootstrap.s.sol:PonsBootstrap \
  --sig 'deployVaultBuyer()' --rpc-url "$RH_RPC_URL" --root . --use 0.8.26 --via-ir --optimizer-runs 1

export PONS_VAULT_BUY_ADAPTER_ADDRESS=0xMINED_OWNER_ONLY_BUYER
export PONS_VAULT_BUY_ETH_WEI=10000000000000000  # example: 0.01 ETH; choose an approved spend
export PONS_VAULT_BUY_SLIPPAGE_BPS=500      # example: 5% haircut, maximum allowed is 10%
export PONS_VAULT_BUY_TTL_SECONDS=120       # maximum allowed is 300

/Users/stacc/.foundry/bin/forge script forensics/launch_cursor/PonsBootstrap.s.sol:PonsBootstrap \
  --sig 'preflightVaultBuy()' --rpc-url "$RH_RPC_URL" --root . --use 0.8.26 --via-ir --optimizer-runs 1
```

The preflight returns the verified adapter target, exact `ethIn` transaction value, Quoter output, `minQOut`, deadline, and `buyQ` calldata. This is a **read-only plan**; the Quoter's method is non-view internally but runs only in the local simulation. Review the output at a fresh block immediately before any owner-wallet execution. The adapter rejects another caller, a zero minimum, a partial ETH fill, a Q output below the minimum, or an expired deadline. Check the mined receipt and positive active Q/ETH liquidity before proceeding. The first buy is a market-activation step; idle executor Q from it is never required for X/Q opens.

After that Q/ETH buy produces active liquidity:

```sh
export PONS_DEVELOPER=0xINTENDED_ETH_RECIPIENT
export PONS_PRICE_CONFIGURATOR=0xINTENDED_PRICE_KEEPER_SIGNER
export PONS_EXIT_CONFIGURATOR=0xDISTINCT_EXIT_KEEPER_SIGNER
export PONS_SPOT_MAX_DEVIATION_BPS=1000

/Users/stacc/.foundry/bin/forge script forensics/launch_cursor/PonsBootstrap.s.sol:PonsBootstrap \
  --sig 'deployAfterLaunch()' --rpc-url "$RH_RPC_URL" --root . --use 0.8.26 --via-ir --optimizer-runs 1
```

The return values are `(priceGuard, settlementRouter)`. Record their mined
addresses and provide the guard as `PONS_PRICE_GUARD_ADDRESS` to the keepers.

The deployed Q activation is recorded in [live-deployment.md](live-deployment.md). To review the script's activation checks for this deployment or a later deployment, simulate:

The owner, price, and exit signers must be three distinct accounts. Fund the
exit signer with ETH for gas; its nonce stream must remain independent of
owner enqueue transactions and price configuration transactions.

```sh
/Users/stacc/.foundry/bin/forge script forensics/launch_cursor/PonsBootstrap.s.sol:PonsBootstrap \
  --sig 'activate()' --rpc-url "$RH_RPC_URL" --root . --use 0.8.26 --via-ir --optimizer-runs 1
```

The script has no `envBytes32`, key file, keystore, or private-key handling. A future real broadcast must be run with an approved external Forge signer and reviewed transaction simulation. Private credentials belong outside this repository and outside these examples.

## Verification and remaining limits

```sh
/Users/stacc/.foundry/bin/forge build forensics/launch_cursor/PonsBootstrap.s.sol \
  --root . --use 0.8.26 --via-ir --optimizer-runs 1

/Users/stacc/.foundry/bin/forge test --root . --contracts forensics/launch_cursor \
  --match-contract PonsBootstrapForkTest --fork-url "$RH_RPC_URL" \
  --use 0.8.26 --via-ir --optimizer-runs 1
```

The sequential fork test exercises **both** fees-on and fees-off variants with a fresh custom Q per variant, real Permit2/launcher, locked v4 launch NFT, Quoter-bounded first Q/ETH buy, post-launch price guard and router, and final activation. Without a Robinhood fork it skips cleanly. The current two-address post-launch script compiles, but its revised fork path still needs a live fork run. The test does not establish Pools.xyz frontend listing, Q demand or value, Pons profitability, keeper operation, or production recipient intent. The bootstrap script does not parse `TokenLaunched` receipts or inspect Squarefun's current frontend, so those remain operator checks.
