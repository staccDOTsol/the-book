// act6-claim.mjs — claim pump.fun creator fees for the dev. slow and sure.
import {
  Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram,
} from "@solana/web3.js";
import { OnlinePumpSdk } from "@pump-fun/pump-sdk";
import { load } from "./wallets.mjs";

const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const log = (...a) => console.log(...a);

async function sendV0(ixs, signers, payer, label) {
  const bh = await conn.getLatestBlockhash();
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh.blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 300_000 }), ComputeBudgetProgram.setComputeUnitLimit({ units: 400_000 }), ...ixs] }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  tx.sign(signers);
  const b64 = Buffer.from(tx.serialize()).toString("base64");
  const sim = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "simulateTransaction", params: [b64, { encoding: "base64", sigVerify: false, replaceRecentBlockhash: true }] }) }).then((x) => x.json());
  if (sim.result?.value?.err) {
    const logs = (sim.result.value.logs || []).slice(-8).join("\n     ");
    throw new Error(label + " SIM FAILED: " + JSON.stringify(sim.result.value.err).slice(0, 200) + "\n     " + logs);
  }
  log("   [sim ok]", label);
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
  const online = new OnlinePumpSdk(conn);

  const before = await conn.getBalance(dev.publicKey);
  log("dev balance before:", before / 1e9, "SOL");

  // curve creator fees (SOL-quoted curve)
  try {
    const ixs = await online.collectCoinCreatorFeeInstructions(dev.publicKey, dev.publicKey);
    if (ixs.length === 0) { log("curve creator fees: nothing to collect (or already claimed)"); }
    else await sendV0(ixs, [dev], dev.publicKey, "claim curve creator fees");
  } catch (e) { log("curve claim:", String(e).slice(0, 140)); }

  // all-quotes sweep (curve + pump-AMM creator vaults) — needs the quote ATAs to exist
  try {
    const { createAssociatedTokenAccountIdempotentInstruction, getAssociatedTokenAddressSync, TOKEN_PROGRAM_ID } = await import("@solana/spl-token");
    const QUOTES = ["EPjFWdd5AufqSSqeM2qN1xzyBapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYc"];
    const ataIxs = [];
    for (const q of QUOTES) {
      const mint = new (await import("@solana/web3.js")).PublicKey(q);
      const ata = getAssociatedTokenAddressSync(mint, dev.publicKey);
      if (!(await conn.getAccountInfo(ata)))
        ataIxs.push(createAssociatedTokenAccountIdempotentInstruction(dev.publicKey, ata, dev.publicKey, mint, TOKEN_PROGRAM_ID));
    }
    if (ataIxs.length) await sendV0(ataIxs, [dev], dev.publicKey, "create quote ATAs (pump-amm sweep prep)");
    const ixs = await online.collectCoinCreatorFeeAllQuotesInstructions(dev.publicKey, dev.publicKey);
    if (ixs.length === 0) log("all-quotes sweep: nothing additional");
    else await sendV0(ixs, [dev], dev.publicKey, "claim all-quotes creator fees");
  } catch (e) { log("all-quotes claim:", String(e).slice(0, 140)); }

  const after = await conn.getBalance(dev.publicKey);
  log("dev balance after:", after / 1e9, "SOL | Δ:", (after - before) / 1e9, "SOL claimed");
}

main().catch((e) => { console.error("CLAIM FAILED:", e); process.exit(1); });
