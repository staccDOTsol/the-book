// jito.mjs — simulate via Helius (b64), send via Jito engine (b58). tips required.
import fs from "fs";
import os from "os";

const HELIUS_KEY = process.env.HELIUS_API_KEY;
const HELIUS = `https://mainnet.helius-rpc.com/?api-key=${HELIUS_KEY}`;
export const AUTH = process.env.JITO_AUTH_UUID || (() => {
  try {
    const env = fs.readFileSync(os.homedir() + "/perps/jit-metrics/.env", "utf8");
    return env.match(/JITO_AUTH_UUID=(\S+)/)?.[1];
  } catch { return undefined; }
})();
const REGION = process.env.JITO_REGION || "ny";
const ENGINE = `https://${REGION}.mainnet.block-engine.jito.wtf/api/v1/bundles`;

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
export function b58e(buf) {
  const d = [0];
  for (let i = 0; i < buf.length; i++) {
    let c = buf[i];
    for (let j = 0; j < d.length; j++) { c += d[j] << 8; d[j] = c % 58; c = (c / 58) | 0; }
    while (c) { d.push(c % 58); c = (c / 58) | 0; }
  }
  let s = "";
  for (let i = 0; i < buf.length && buf[i] === 0; i++) s += "1";
  return s + d.reverse().map((x) => B58[x]).join("");
}

async function post(url, method, params, auth) {
  const r = await fetch(url, { method: "POST",
    headers: { "content-type": "application/json", ...(auth ? { "x-jito-auth": auth } : {}) },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) }).then((x) => x.json());
  return r;
}

// txs: array of serialized V0 packets (Uint8Array/Buffer)
export async function sendBundle(txs) {
  const b64s = txs.map((t) => Buffer.from(t).toString("base64"));
  // 1) simulate the WHOLE bundle via Helius
  const sim = await post(HELIUS, "simulateBundle", [{ encodedTransactions: b64s }]);
  if (sim.error) throw new Error("simulateBundle: " + JSON.stringify(sim.error).slice(0, 200));
  const results = sim.result?.value?.transactionResults || [];
  const bad = results.findIndex((r) => r.err);
  if (bad >= 0 || sim.result?.value?.summary !== "succeeded") {
    const logs = (results[bad]?.logs || []).slice(-10).join("\n     ");
    throw new Error(`simulateBundle: tx[${bad}] failed ${JSON.stringify(results[bad]?.err || sim.result?.value?.summary).slice(0, 200)}\n     ${logs}`);
  }
  console.log("   [simulateBundle] succeeded —", results.length, "txs clean");
  // 2) send via the engine, base58
  const b58s = txs.map((t) => b58e(Buffer.from(t)));
  const sent = await post(ENGINE, "sendBundle", [b58s], AUTH);
  if (sent.error) throw new Error("sendBundle: " + JSON.stringify(sent.error).slice(0, 200));
  const bundleId = sent.result;
  console.log("   [sendBundle] id:", bundleId);
  // 3) poll inflight
  for (let i = 0; i < 80; i++) {
    await new Promise((res) => setTimeout(res, 500));
    const st = await post(ENGINE, "getInflightBundleStatuses", [[bundleId]], AUTH);
    const s = st.result?.value?.[0]?.status;
    if (s === "Landed") return { bundleId, status: "Landed" };
    if (s === "Failed" || s === "Invalid") return { bundleId, status: s };
  }
  return { bundleId, status: "Pending/Unknown" };
}
