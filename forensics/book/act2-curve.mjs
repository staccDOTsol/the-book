// act2-curve.mjs — UP HARD. breadth + engine buys on the bonding curve.
import {
  Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram, PublicKey,
} from "@solana/web3.js";
import BN from "bn.js";
import { PumpSdk, OnlinePumpSdk, getBuyTokenAmountFromSolAmount, getBuySolAmountFromTokenAmount } from "@pump-fun/pump-sdk";
import { load, loadFunder } from "./wallets.mjs";
import fs from "fs";

const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const MINT = new PublicKey(process.env.MINT || "8YxaREY5nejPRPztoskVPkWQAAAXxviZvNeTMETHf2X9");
const log = (...a) => console.log(...a);

async function sendV0(ixs, signers, payer, label) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const bh = await conn.getLatestBlockhash();
    const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh.blockhash,
      instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 500_000 }), ComputeBudgetProgram.setComputeUnitLimit({ units: 800_000 }), ...ixs] }).compileToV0Message();
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
    if (r.error) {
      if (String(r.error.message).includes("Blockhash not found") && attempt < 3) { log("   [" + label + "] stale bh, retry"); continue; }
      throw new Error(label + " send: " + JSON.stringify(r.error).slice(0, 200));
    }
    const sig = r.result;
    for (let t = 0; t < 50; t++) {
      await new Promise((res) => setTimeout(res, 500));
      const st = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getSignatureStatuses", params: [[sig]] }) }).then((x) => x.json());
      const v = st.result?.value?.[0];
      if (v?.err) throw new Error(label + " on-chain fail: " + JSON.stringify(v.err).slice(0, 200));
      if (v && ["confirmed", "finalized"].includes(v.confirmationStatus)) { log("   " + label + ": " + sig); return sig; }
    }
    throw new Error(label + " never confirmed");
  }
}

async function main() {
  const funder = loadFunder();
  const dev = load("dev");
  const engine = load("engine");
  const s1 = load("sniper1");
  const s2 = load("sniper2");
  const s3 = load("sniper3");
  const online = new OnlinePumpSdk(conn);
  const off = new PumpSdk();
  const global = await online.fetchGlobal();
  const feeConfig = await online.fetchFeeConfig();

  async function buy(wallet, sol, label) {
    const st = await online.fetchBuyState(MINT, wallet.publicKey);
    const amt = getBuyTokenAmountFromSolAmount({ global, feeConfig, mintSupply: null, bondingCurve: st.bondingCurve,
      amount: new BN(Math.round(sol * 1e9)), quoteMint: st.quoteMint, quoteTokenProgram: st.quoteTokenProgram, creatorFeeBps: st.bondingCurve.creatorFeeBps });
    const solCost = getBuySolAmountFromTokenAmount({ global, feeConfig, mintSupply: null, bondingCurve: st.bondingCurve,
      amount: amt, quoteMint: st.quoteMint, quoteTokenProgram: st.quoteTokenProgram, creatorFeeBps: st.bondingCurve.creatorFeeBps });
    log("   [" + label + "] cost: " + Number(solCost) / 1e9 + " SOL for " + Number(amt) / 1e6 + "M tok");
    const ixs = await off.buyV2Instructions({ global, bondingCurveAccountInfo: st.bondingCurveAccountInfo, bondingCurve: st.bondingCurve,
      associatedUserAccountInfo: st.associatedUserAccountInfo, mint: MINT, user: wallet.publicKey,
      amount: amt, quoteAmount: solCost, slippage: 5000,
      tokenProgram: new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"), quoteTokenProgram: st.quoteTokenProgram });
    await sendV0(ixs, [wallet], wallet.publicKey, label + " (" + sol + " SOL → " + Number(amt) / 1e6 + "M tok)");
    return amt;
  }

  log("── ACT 2a: breadth + walk on the curve ──");
  // idempotent breadth: skip wallets that already hold the coin
  async function holds(wallet) {
    const bal = await conn.getParsedTokenAccountsByOwner(wallet.publicKey, { mint: MINT });
    return bal.value.length > 0;
  }
  for (const [w, name] of [[s1, "sniper1"], [s2, "sniper2"], [s3, "sniper3"]]) {
    if (await holds(w)) { log("   " + name + " already holds — skipped"); continue; }
    await buy(w, 0.2, name);
  }
  const ENGINE_SOL = Number(process.env.ENGINE_SOL || 0.7);
  await buy(engine, ENGINE_SOL, "ENGINE walk buy");
  log("curve walked hard. CA:", MINT.toBase58());
}

main().catch((e) => { console.error("ACT2 FAILED:", e); process.exit(1); });
