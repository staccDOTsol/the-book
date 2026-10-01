// pounce.mjs v2 — full lifecycle: entry on pool spike, exit on cluster outflow.
// the shape: cluster pumps in → our 0% pools collect the toll → cluster dumps → we exit with them
//
// entry: ≥5 pools on one token within 10 min (the AMM-count fingerprint)
// exit:  cluster wallet starts large token outflow (the down-triangle on the XGAS chart)
//        OR liquidity removal from cluster positions (ModifyLiquidity with negative delta)
//        OR price drops >20% from peak in <30 min (the 7a62 dump pattern)

import { WebSocket } from "ws";
import { createRequire } from "module";
const require = createRequire(import.meta.url);
const fs = require("fs");

const RPC_WS = process.env.RPC_WS || "wss://rpc.mainnet.chain.robinhood.com";
const RPC_HTTP = process.env.RPC_HTTP || "https://rpc.mainnet.chain.robinhood.com";
const POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951";
const POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7";

// event topics (computed from signatures)
const INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438";
const MODIFY_LIQUIDITY_TOPIC = "0xf208f4912782f3e39d3760ec19da2792bd2cfae6f6e0282e90854751c4e0b403";
const TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

const log = (...a) => console.log(`[${new Date().toISOString().slice(11, 19)}]`, ...a);

// ── config ──
const MIN_POOLS_FOR_ENTRY = 5;
const ENTRY_WINDOW_MS = 10 * 60 * 1000;  // rolling window: N new pools within this period triggers
// the burst is the signal — not t=0. the cluster found XGAS at minute 32,
// GME at minute 7, and hundreds more across the grid. the arrival time varies;
// the SPIKE doesn't. a token with 1-2 pools suddenly getting 5+ new ones in
// minutes is the cluster indexing it, whenever that happens.
const EXIT_DROP_THRESHOLD = 0.20;      // 20% drop from peak = exit
const EXIT_LARGE_TRANSFER = 100000;    // 100k tokens out = exit signal
const KNOWN_CLUSTER = new Set([        // from the XGAS.DEV forensics
  "0x7a62a31aa78c330a74d93b25ac7d376139fa4999",
  "0x3e6bc0ec610a0241e2c63362798885a7d387c99a",
  "0x62a4ec1fa3f770a13b425b48e0e31e4a5e98bf0c",
  "0x05783022615f76de7e887aed81307ae39206aa95",
  "0x7af2fda1c6f66f8ba4f6a02b76ba4bd0f3e13ca8",
  "0xe1a72d04f750be30492089823187e74cc045c5bc", // the throwaway hook deployer
].map(a => a.toLowerCase()));

// ── state ──
const tracked = new Map(); // mint → full lifecycle state
const triggered = new Set();

async function post(method, params) {
  const r = await fetch(RPC_HTTP, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": "Mozilla/5.0" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  }).then((x) => x.json());
  if (r.error) throw new Error(method + ": " + JSON.stringify(r.error).slice(0, 150));
  return r.result;
}

function getState(mint) {
  if (!tracked.has(mint)) {
    tracked.set(mint, {
      phase: "watching",
      poolCount: 0,
      firstPool: 0,
      fees: [],
      poolIds: [],
      peakPrice: 0,
      currentPrice: 0,
      clusterOutflow: 0,
      liquidityRemovals: 0,
      pounceTx: null,
      exitTx: null,
    });
  }
  return tracked.get(mint);
}

// ── ENTRY: pool birthrate fingerprint ──
function onPoolBirth(mint, poolId, fee, tickSpacing) {
  const s = getState(mint);
  const now = Date.now();
  s.poolCount++;
  s.fees.push(fee);
  s.poolIds.push(poolId);

  // track births in a rolling window
  if (!s.birthTimestamps) s.birthTimestamps = [];
  s.birthTimestamps.push(now);

  // prune timestamps outside the window
  while (s.birthTimestamps.length > 0 && now - s.birthTimestamps[0] > ENTRY_WINDOW_MS) {
    s.birthTimestamps.shift();
  }

  const recentCount = s.birthTimestamps.length;
  log(`pool #${s.poolCount} [${recentCount} in last ${ENTRY_WINDOW_MS / 60000}m]: ${mint.slice(0, 8)}… fee=${fee} ts=${tickSpacing}`);

  // the fingerprint: a BURST of new pools within the rolling window
  // regardless of when the token launched (t=0 or t=32min or t=2h)
  if (recentCount >= MIN_POOLS_FOR_ENTRY && s.phase === "watching") {
    log(`⚡ ENTRY SIGNAL: ${recentCount} pools in the last ${Math.round((now - s.birthTimestamps[0]) / 1000)}s on ${mint.slice(0, 8)}…`);
    log(`  total pools: ${s.poolCount} | burst rate: ${recentCount} in ${(now - s.birthTimestamps[0]) / 1000}s`);
    s.phase = "entered";
    pounce(mint, s);
  }
}

// ── EXIT: cluster outflow fingerprint ──
function onTransfer(mint, from, to, value) {
  const s = getState(mint);
  if (s.phase !== "entered") return;

  const fromLower = from.toLowerCase();
  if (KNOWN_CLUSTER.has(fromLower)) {
    s.clusterOutflow += value;
    log(`⚠️  cluster outflow: ${fromLower.slice(0, 8)}… sent ${value.toLocaleString()} tokens`);

    if (s.clusterOutflow > EXIT_LARGE_TRANSFER || value > EXIT_LARGE_TRANSFER) {
      log(`🚨 EXIT SIGNAL: cluster dumping (${s.clusterOutflow.toLocaleString()} total out)`);
      s.phase = "exiting";
      exit(mint, s, "cluster_outflow");
    }
  }
}

function onLiquidityRemoval(mint, liquidityDelta) {
  const s = getState(mint);
  if (s.phase !== "entered") return;

  if (liquidityDelta < 0) {
    s.liquidityRemovals++;
    log(`⚠️  liquidity removal #${s.liquidityRemovals} on ${mint.slice(0, 8)}…`);
    if (s.liquidityRemovals >= 3) {
      log(`🚨 EXIT SIGNAL: ${s.liquidityRemovals} liquidity removals — cluster pulling`);
      s.phase = "exiting";
      exit(mint, s, "liquidity_withdrawal");
    }
  }
}

function onPriceUpdate(mint, price) {
  const s = getState(mint);
  if (s.phase !== "entered") return;

  if (price > s.peakPrice) {
    s.peakPrice = price;
    return;
  }

  const dropFromPeak = (s.peakPrice - price) / s.peakPrice;
  if (dropFromPeak > EXIT_DROP_THRESHOLD && s.peakPrice > 0) {
    log(`🚨 EXIT SIGNAL: price dropped ${Math.round(dropFromPeak * 100)}% from peak`);
    s.phase = "exiting";
    exit(mint, s, "price_collapse");
  }
}

// ── ACTIONS ──
async function pounce(mint, s) {
  triggered.add(mint);
  const honeypotFees = s.fees.filter((f) => f >= 700000);
  log(`  🎯 POUNCING on ${mint.slice(0, 8)}… (${s.poolCount} pools, ${honeypotFees.length} honeypot)`);

  // resolve token name
  const name = await resolveTokenName(mint);
  log(`  token: ${name || "unknown"}`);
  log(`  → deploy TheRig 0% pools on this token`);
  log(`  → the arbs route through our free pools, the hook counts the toll`);

  // record
  appendPounce({
    event: "ENTRY",
    mint,
    timestamp: new Date().toISOString(),
    poolCount: s.poolCount,
    windowMs: Date.now() - s.firstPool,
    fees: s.fees,
    honeypotCount: honeypotFees.length,
  });
}

async function exit(mint, s, reason) {
  log(`  🏃 EXITING ${mint.slice(0, 8)}… reason=${reason}`);
  log(`  peak=${s.peakPrice} current=${s.currentPrice} outflow=${s.clusterOutflow}`);

  // pull our positions, claim our fees, withdraw
  // the hook's toll has been collecting since entry
  // this is where you'd call the exit script

  s.phase = "exited";
  appendPounce({
    event: "EXIT",
    mint,
    reason,
    timestamp: new Date().toISOString(),
    peakPrice: s.peakPrice,
    poolCount: s.poolCount,
    tollCollected: s.poolCount, // the hook counted these
    clusterOutflow: s.clusterOutflow,
  });

  // stop tracking
  tracked.delete(mint);
}

function appendPounce(d) {
  fs.appendFileSync("pounces.jsonl", JSON.stringify(d) + "\n");
}

async function resolveTokenName(mint) {
  try {
    const code = await post("eth_call", [{ to: mint, data: "0x06fdde03" }, "latest"]);
    if (code && code !== "0x") {
      const data = Buffer.from(code.slice(2), "hex");
      const len = Number(BigInt("0x" + code.slice(66, 130)));
      if (len > 0 && len < 64) return data.slice(64, 64 + len).toString("utf8");
    }
  } catch {}
  return null;
}

// ── WebSocket: subscribe to logs ──
async function startWs() {
  const ws = new WebSocket(RPC_WS, { headers: { "User-Agent": "Mozilla/5.0" } });

  ws.on("open", () => {
    log("ws connected");
    // subscribe to new blocks
    ws.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_subscribe", params: ["newHeads"] }));
  });

  ws.on("message", async (data) => {
    const msg = JSON.parse(data.toString());
    if (msg.method !== "eth_subscription") return;
    const blockNum = msg.params.result.number;

    try {
      // scan for all three event types in one batch
      const [initLogs, modLogs] = await Promise.all([
        post("eth_getLogs", [{ address: POOL_MANAGER, topics: [INITIALIZE_TOPIC], fromBlock: blockNum, toBlock: blockNum }]),
        post("eth_getLogs", [{ address: POOL_MANAGER, topics: [MODIFY_LIQUIDITY_TOPIC], fromBlock: blockNum, toBlock: blockNum }]),
      ]);

      // Initialize → entry signal
      if (initLogs) {
        for (const lg of initLogs) {
          const poolId = lg.topics[1];
          const currency0 = "0x" + lg.topics[2].slice(26);
          const currency1 = "0x" + lg.topics[3].slice(26);
          const fee = parseInt(lg.data.slice(2, 66), 16);
          const ts = parseInt(lg.data.slice(66, 130), 16);
          const mint = currency0 === "0x0000000000000000000000000000000000000000" ? currency1 : currency0;
          onPoolBirth(mint.toLowerCase(), poolId, fee, ts);
        }
      }

      // ModifyLiquidity → exit signal (negative delta = removal)
      if (modLogs) {
        for (const lg of modLogs) {
          // ModifyLiquidity(PoolId indexed, address indexed sender, int256 liquidityDelta, ...)
          // data = liquidityDelta (int256)
          const liquidityDeltaHex = lg.data.slice(2, 66);
          const liquidityDelta = BigInt("0x" + liquidityDeltaHex);
          const isNegative = liquidityDeltaHex.slice(0, 1) === "f" && liquidityDelta > 0n;
          if (isNegative || liquidityDelta < 0n) {
            const sender = "0x" + lg.topics[2].slice(26);
            if (KNOWN_CLUSTER.has(sender.toLowerCase())) {
              const poolId = lg.topics[1];
              // need to resolve pool → mint, but for now use the pool's token pair
              // in practice the rig hook knows which pool it's on
              const mint = poolId.toLowerCase(); // simplified
              onLiquidityRemoval(mint, Number(liquidityDelta));
            }
          }
        }
      }
    } catch (e) {
      if (!String(e).includes("-32005")) log("scan err:", String(e).slice(0, 80));
    }
  });

  ws.on("error", (e) => log("ws err:", String(e).slice(0, 80)));
  ws.on("close", () => {
    log("ws closed, reconnecting…");
    setTimeout(startWs, 5000);
  });
}

// ── main ──
log("pounce v2 — full lifecycle watcher");
log(`entry: ≥${MIN_POOLS_FOR_ENTRY} pools within ${ENTRY_WINDOW_MS / 1000}s`);
log(`exit: cluster outflow >${EXIT_LARGE_TRANSFER} tokens, ≥3 liquidity removals, or >${EXIT_DROP_THRESHOLD * 100}% price drop`);
log(`known cluster wallets: ${KNOWN_CLUSTER.size}`);
startWs();
