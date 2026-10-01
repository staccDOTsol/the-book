// pounce v3 — Birdeye WebSocket detection + chain polling backup
// watches for pool birthrate bursts, fires the Orchestrator when the fingerprint matches

import fs from "fs";
import https from "https";

const BIRDEYE_KEY = "3fa616d53b45415c8bd550e7820b3160";
const BIRDEYE_WS = `wss://streams.birdeye.so/robinhood?apiKey=${BIRDEYE_KEY}`;
const RPC_HTTP = "https://rpc.mainnet.chain.robinhood.com";
const POOL_MANAGER = "0x8366a39CC670B4001A1121B8F6A443A643e40951";
const INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438";
const MIN_POOLS = 5;
const WINDOW_MS = 10 * 60 * 1000;
const ORCHESTRATOR = "0x2b860de6be694a3db9680efb7c0f839ea27fffc0";

const log = (...a) => console.log(`[${new Date().toISOString().slice(11,19)}]`, ...a);
const births = new Map(); // mint → timestamps[]
const triggered = new Set();

async function post(url, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = https.request({ hostname: u.hostname, path: u.pathname + u.search, method: "POST",
      headers: { "Content-Type": "application/json", "User-Agent": "Mozilla/5.0" } }, (res) => {
      let d = ""; res.on("data", c => d += c); res.on("end", () => resolve(JSON.parse(d)));
    });
    req.on("error", reject);
    req.write(JSON.stringify(body)); req.end();
  });
}

function onPoolBirth(mint, poolId, fee) {
  if (!births.has(mint)) births.set(mint, []);
  const times = births.get(mint);
  times.push(Date.now());
  // prune outside window
  const cutoff = Date.now() - WINDOW_MS;
  while (times.length > 0 && times[0] < cutoff) times.shift();

  log(`pool #${times.length}: ${mint.slice(0,10)}… fee=${fee}`);

  if (times.length >= MIN_POOLS && !triggered.has(mint)) {
    triggered.add(mint);
    log(`⚡ BURST: ${times.length} pools in ${Math.round((Date.now() - times[0])/1000)}s on ${mint.slice(0,10)}…`);
    fs.appendFileSync("pounces.jsonl", JSON.stringify({
      event: "ENTRY", mint, timestamp: new Date().toISOString(),
      poolCount: times.length, windowMs: Date.now() - times[0],
    }) + "\n");
    fireAndTick(mint);
  }
}


const KEY = "0xb5eed452e305712dc08bd9c4482bb967a21e5c41e944c72326e51c7e86a678ad";
const RPC = "https://rpc.mainnet.chain.robinhood.com";

async function fireAndTick(mint) {
  log("  🔥 FIRING spawn + starting ticks…");
  const { execSync } = await import("child_process");
  // fire spawn
  try {
    const r = execSync(
      `cast send ${ORCHESTRATOR} "spawn(address,uint160,uint256[],int24)" ${mint} 79228162514264337593543950336 "[5,25,100]" 60 --rpc-url ${RPC} --private-key ${KEY} 2>&1`,
      { timeout: 15000 }
    ).toString();
    log("  🔥 spawn:", r.includes("transactionHash") ? "SENT ✓" : "reverted — trying pounce()");
    if (!r.includes("transactionHash")) {
      const r2 = execSync(
        `cast send ${ORCHESTRATOR} "pounce(address,uint160,uint256[],int24,uint256,int24,int24)" ${mint} 79228162514264337593543950336 "[5,25,100]" 60 100000000000000000 -100 100 --rpc-url ${RPC} --private-key ${KEY} 2>&1`,
        { timeout: 15000 }
      ).toString();
      log("  🔥 pounce:", r2.includes("transactionHash") ? "SENT ✓" : "FAILED");
    }
  } catch (e) { log("  spawn err:", String(e).slice(0, 100)); }

  // start ticking
  log("  ⏱  ticks every 3s");
  setInterval(() => {
    try {
      execSync(`cast send ${ORCHESTRATOR} "tick()" --rpc-url ${RPC} --private-key ${KEY} 2>&1`, { timeout: 10000 });
    } catch (e) { /* silent — ticks retry */ }
  }, 3000);
}

async function chainPoll() {
  try {
    const blockR = await post(RPC_HTTP, { jsonrpc: "2.0", id: 1, method: "eth_blockNumber", params: [] });
    const current = parseInt(blockR.result, 16);
    if (current <= chainPoll.last) return;
    const from = chainPoll.last === 0 ? "0x" + (current - 1).toString(16) : "0x" + (chainPoll.last + 1).toString(16);
    const logs = await post(RPC_HTTP, { jsonrpc: "2.0", id: 1, method: "eth_getLogs", params: [{
      address: POOL_MANAGER, topics: [INITIALIZE_TOPIC], fromBlock: from, toBlock: "0x" + current.toString(16) }] });
    if (logs.result) {
      const QUOTES = new Set([
        "0x0000000000000000000000000000000000000000", // native ETH
        "0x5fc5360d0400a0fd4f2af552add042d716f1d168", // USDG
      ]);
      for (const lg of logs.result) {
        const c0 = ("0x" + lg.topics[2].slice(26)).toLowerCase();
        const c1 = ("0x" + lg.topics[3].slice(26)).toLowerCase();
        const fee = parseInt(lg.data.slice(2, 66), 16);
        // pick the NON-quote currency (the subject token)
        const mint = QUOTES.has(c0) ? c1 : QUOTES.has(c1) ? c0 : null;
        if (mint && !QUOTES.has(mint)) {
          onPoolBirth(mint, lg.topics[1], fee);
        }
      }
    }
    chainPoll.last = current;
  } catch (e) { if (!String(e).includes("-32005")) log("poll err:", String(e).slice(0, 60)); }
}
chainPoll.last = 0;

log("pounce v4 — chain polling + Birdeye REST enrichment");
log(`Orchestrator: ${ORCHESTRATOR}`);
log(`entry: ≥${MIN_POOLS} pools in ${WINDOW_MS/1000}s rolling window`);
log(`known cluster wallets: 6`);

setInterval(chainPoll, 3000);
chainPoll();
