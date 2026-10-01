// act2-sell.mjs — PULL FROM THE CURVE: dev sells tokens, recycles SOL to the engine.
import {
  Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram, PublicKey,
} from "@solana/web3.js";
import BN from "bn.js";
import { PumpSdk, OnlinePumpSdk, getSellSolAmountFromTokenAmount } from "@pump-fun/pump-sdk";
import { load } from "./wallets.mjs";

const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const MINT = new PublicKey(process.env.MINT || "8YxaREY5nejPRPztoskVPkWQAAAXxviZvNeTMETHf2X9");
const SELL_TOKENS_MILLIONS = Number(process.env.SELL_TOKENS || 12); // millions of tokens
const log = (...a) => console.log(...a);

async function sendV0(ixs, signers, payer, label) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const bh = await conn.getLatestBlockhash();
    const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh.blockhash,
      instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 500_000 }), ComputeBudgetProgram.setComputeUnitLimit({ units: 800_000 }), ...ixs] }).compileToV0Message();
    const tx = new VersionedTransaction(msg);
    tx.sign(signers);
    const b64 = Buffer.from(tx.serialize()).toString("base64");
    const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sendTransaction", params: [b64, { encoding: "base64", skipPreflight: true, maxRetries: 2 }] }) }).then((x) => x.json());
    if (r.error) {
      if (String(r.error.message).includes("Blockhash not found") && attempt < 3) continue;
      throw new Error(label + " send: " + JSON.stringify(r.error).slice(0, 250));
    }
    const sig = r.result;
    for (let t = 0; t < 50; t++) {
      await new Promise((res) => setTimeout(res, 500));
      const st = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getSignatureStatuses", params: [[sig]] }) }).then((x) => x.json());
      const v = st.result?.value?.[0];
      if (v?.err) throw new Error(label + " on-chain: " + JSON.stringify(v.err).slice(0, 250));
      if (v && ["confirmed", "finalized"].includes(v.confirmationStatus)) { log("   " + label + ": " + sig); return sig; }
    }
    throw new Error(label + " never confirmed");
  }
}

async function main() {
  const dev = load("dev");
  const engine = load("engine");
  const online = new OnlinePumpSdk(conn);
  const off = new PumpSdk();
  const global = await online.fetchGlobal();
  const feeConfig = await online.fetchFeeConfig();
  const st = await online.fetchSellState(MINT, dev.publicKey, new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"));

  const amount = new BN(Math.round(SELL_TOKENS_MILLIONS * 1e6));
  const solOut = getSellSolAmountFromTokenAmount({ global, feeConfig, mintSupply: null, bondingCurve: st.bondingCurve,
    amount, quoteMint: st.quoteMint, quoteTokenProgram: st.quoteTokenProgram, creatorFeeBps: st.bondingCurve.creatorFeeBps });
  log("selling", SELL_TOKENS_MILLIONS, "M tokens, est", Number(solOut) / 1e9, "SOL out");

  const ixs = await off.sellV2Instructions({ global, bondingCurveAccountInfo: st.bondingCurveAccountInfo, bondingCurve: st.bondingCurve,
    mint: MINT, user: dev.publicKey, amount, quoteAmount: solOut, slippage: 5000,
    tokenProgram: new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"), quoteTokenProgram: st.quoteTokenProgram });
  await sendV0(ixs, [dev], dev.publicKey, "dev curve pull");
  const devBal = await conn.getBalance(dev.publicKey);
  log("dev now:", devBal / 1e9, "SOL");

  // recycle: push (balance - 1 SOL) to the engine
  const { SystemProgram } = await import("@solana/web3.js");
  const push = BigInt(devBal) - 1_000_000_000n;
  if (push > 0) {
    const t2 = new TransactionMessage({ payerKey: dev.publicKey, recentBlockhash: (await conn.getLatestBlockhash()).blockhash,
      instructions: [SystemProgram.transfer({ fromPubkey: dev.publicKey, toPubkey: engine.publicKey, lamports: push })] }).compileToV0Message();
    const tx2 = new VersionedTransaction(t2);
    tx2.sign([dev]);
    const b64 = Buffer.from(tx2.serialize()).toString("base64");
    const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sendTransaction", params: [b64, { encoding: "base64", skipPreflight: true }] }) }).then((x) => x.json());
    await new Promise((res) => setTimeout(res, 2500));
  }
  log("engine now:", (await conn.getBalance(engine.publicKey)) / 1e9, "SOL — go walk");
}

main().catch((e) => { console.error("SELL FAILED:", e); process.exit(1); });
