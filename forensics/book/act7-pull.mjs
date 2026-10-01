// act7-pull.mjs — THE EXTRACTION. dev sells tranches on the PumpSwap AMM.
import {
  Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram, PublicKey,
} from "@solana/web3.js";
import BN from "bn.js";
import { OnlinePumpAmmSdk, PumpAmmSdk } from "@pump-fun/pump-swap-sdk";
import { load } from "./wallets.mjs";

const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const MINT = new PublicKey("8YxaREY5nejPRPztoskVPkWQAAAXxviZvNeTMETHf2X9");
const POOL = new PublicKey("49PEdr3Jbbm5544rtsiwS7y3LGXhk7uLRJi9FpRamkPA");
const PULL_M = Number(process.env.PULL_M || 5); // millions of tokens per tranche
const N_TRANCHES = Number(process.env.N || 1);
const DELAY_S = Number(process.env.DELAY || 0);
const log = (...a) => console.log(...a);

async function sendV0(ixs, signers, payer, label) {
  const bh = await conn.getLatestBlockhash();
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh.blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 500_000 }), ComputeBudgetProgram.setComputeUnitLimit({ units: 600_000 }), ...ixs] }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  tx.sign(signers);
  const b64 = Buffer.from(tx.serialize()).toString("base64");
  const sim = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "simulateTransaction", params: [b64, { encoding: "base64", sigVerify: false, replaceRecentBlockhash: true }] }) }).then((x) => x.json());
  if (sim.result?.value?.err) {
    const logs = (sim.result.value.logs || []).slice(-8).join("\n     ");
    throw new Error(label + " SIM FAILED: " + JSON.stringify(sim.result.value.err).slice(0, 200) + "\n     " + logs);
  }
  const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sendTransaction", params: [b64, { encoding: "base64", skipPreflight: true, maxRetries: 2 }] }) }).then((x) => x.json());
  if (r.error) throw new Error(label + " send: " + JSON.stringify(r.error).slice(0, 200));
  const sig = r.result;
  for (let t = 0; t < 50; t++) {
    await new Promise((res) => setTimeout(res, 500));
    const st = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getSignatureStatuses", params: [[sig]] }) }).then((x) => x.json());
    const v = st.result?.value?.[0];
    if (v?.err) throw new Error(label + " on-chain: " + JSON.stringify(v.err).slice(0, 200));
    if (v && ["confirmed", "finalized"].includes(v.confirmationStatus)) { log("   " + label + ":", sig); return sig; }
  }
  throw new Error(label + " never confirmed");
}

async function main() {
  const dev = load("dev");
  const online = new OnlinePumpAmmSdk(conn);
  const off = new PumpAmmSdk();
  const before = await conn.getBalance(dev.publicKey);
  log("dev before:", before / 1e9, "SOL | pulling", PULL_M, "M ×", N_TRANCHES, "tranches");

  for (let i = 0; i < N_TRANCHES; i++) {
    const st = await online.swapSolanaState(POOL, dev.publicKey);
    const baseIn = new BN(Math.round(PULL_M * 1e6 * 1e6)); // raw
    const minOut = new BN(1); // take whatever the AMM gives
    const ixs = await off.sellInstructions(st, baseIn, minOut);
    await sendV0(ixs, [dev], dev.publicKey, `PULL tranche ${i + 1} (${PULL_M}M → AMM)`);
    if (i + 1 < N_TRANCHES && DELAY_S > 0) await new Promise((r) => setTimeout(r, DELAY_S * 1000));
  }

  const after = await conn.getBalance(dev.publicKey);
  log("dev after:", after / 1e9, "SOL | Δ:", (after - before) / 1e9, "SOL extracted");
}

main().catch((e) => { console.error("PULL FAILED:", e); process.exit(1); });
