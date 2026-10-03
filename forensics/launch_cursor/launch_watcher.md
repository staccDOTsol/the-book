# Pons launch watcher

`pons_launch_watcher.py` watches the Robinhood Pons V2 factory's
`TokenLaunched(address indexed token, address indexed curve, address indexed
deployer, address pairToken, uint256 launchConfigId, uint256
graduationThreshold)` event. The filter topic is the Keccak hash of that
signature; topic 1 contains the launch token. The factory address is fixed in
the watcher. Its sole onchain write is owner-signed `Q.enqueue(token)`.

The watcher verifies the configured chain ID, Q contract code,
`Q.ponsFactory()`, and (in live mode) `Q.owner()` before any transaction. It
reads `Q.launches(token)` to skip tokens already enqueued. Q itself verifies
the token against the Pons launch record and rejects duplicates.

## Runtime

Install the repository's pinned Python dependencies in its local virtual
environment; the manifest includes `eth-account`, `eth-utils`, `rlp`, and
`websockets`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r forensics/requirements-cranker.txt
```

Set these process environment variables at runtime:

| Variable | Purpose |
| --- | --- |
| `PONS_HTTP_RPC_URL` | Robinhood JSON-RPC HTTP endpoint with `eth_getLogs` and transaction support |
| `PONS_WS_RPC_URL` | Same chain's websocket endpoint with `eth_subscribe` support |
| `PONS_CHAIN_ID` | Expected chain ID; the watcher rejects a mismatch |
| `PONS_Q_ADDRESS` | Deployed hookless Q contract address |
| `PONS_OWNER_PRIVATE_KEY` | Q owner's key, required only with `--live`; supplied in the process environment, never read from a file |

The shared HTTP transport sends an explicit client User-Agent. Robinhood's
public RPC returned HTTP 403 to Python-urllib's default User-Agent in a
read-only smoke check; `eth_chainId` succeeded with the explicit identifier.

For the first run, choose `--start-block` at or before the earliest Pons launch
that Q should receive. It is inclusive. The flag is mandatory when the cursor
does not yet exist; subsequent runs use the saved cursor and omit it. A
read-only preview is:

```sh
.venv/bin/python forensics/launch_cursor/pons_launch_watcher.py --start-block BLOCK --once
```

The preview prints `would_enqueue` or `already_enqueued` records. It does not
advance the live cursor or read the owner key. Once Q is deployed and the
owner has deliberately configured the runtime, start continuous submission:

```sh
.venv/bin/python forensics/launch_cursor/pons_launch_watcher.py --start-block BLOCK --live
```

On later starts omit `--start-block`. `--once --live` is available for a
bounded catch-up run. Defaults are three confirmation blocks, 2,000 blocks
per `eth_getLogs` request, a ten-second polling interval, three attempts for
a confirmed reverted enqueue, 500,000 gas, 5 gwei max fee, and 1 gwei max
priority fee. Adjust with the matching CLI flags for the actual RPC and
network conditions. The fee and gas caps stop the watcher before signing a
transaction that exceeds them.

The JSON cursor and lock are under this repository's ignored `.local`
directory, with the cursor written by atomic replace and fsync. The cursor
records chain ID, Q, the last fully processed block and its hash, plus any
pending signed transaction. The pending record includes a **signed raw
transaction** so a crash between saving it and broadcasting it can recover
without a different nonce or call. The file is mode `0600`; protect and back
up `.local` as wallet operational state. Do not move the cursor to `/tmp`.
Run only one watcher against a given cursor; a file lock enforces this on one
host. If moving hosts, copy the cursor securely before starting the new
instance.

## Recovery and limits

Websocket logs only wake the watcher. Every wake-up, reconnect, and periodic
poll scans confirmed ranges through HTTP `eth_getLogs` from the durable
cursor. A range is committed only after every event is handled. Failed
enqueue attempts leave the block cursor in place; replay checks Q's stage and
skips tokens already enqueued. A pending signed transaction is validated for
owner, chain, Q target, `enqueue(address)` payload, zero ETH value, nonce,
hash, and current gas and fee caps before rebroadcast. If its nonce was consumed by
another transaction and no matching receipt exists, the watcher stops for
operator review.

The cursor's block hash and each returned log's block hash are compared with
canonical RPC blocks. A reorg of a committed block stops the watcher; choose
a new start block and reconcile Q's onchain stages before resetting the
cursor. Increasing `--confirmations` reduces this risk but delays launches.
The watcher cannot undo an enqueue that was already finalized if the Pons
launch itself later disappears in a deeper reorg.

The watcher does not configure a price, mint an LP, monitor range boundaries,
exit positions, or fund Q. The separate price and exit keepers and Q/executor
paths described in [README.md](README.md) remain necessary. Q has not been
launched or funded, and this watcher alone does not make live trading ready.

Offline verification:

```sh
.venv/bin/python -m unittest -v forensics/launch_cursor/test_pons_launch_watcher.py
```
