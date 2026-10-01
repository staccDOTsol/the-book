// act8-buy-amm.mjs — SLOW SURE WALK on the PumpSwap AMM. small buys, lock each step.
import { Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram, PublicKey } from "@solana/web3.js";
import BN from "bn.js";
import { OnlinePumpAmmSdk, PumpAmmSdk } from "@pump-fun/pump-swap-sdk";
import { load } from "./wallets.mjs";
const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const POOL = new PublicKey("49PEdr3Jbbm5544rtsiwS7y3LGXhk7uLRJi9FpRamkPA");
const BUY_SOL = Number(process.env.BUY_SOL || 0.05);
const log = (...a) => console.log(...a);
async function main() {
  const dev = load("dev");
  const online = new OnlinePumpAmmSdk(conn);
  const off = new PumpAmmSdk();
  const st = await online.swapSolanaState(POOL, dev.publicKey);
  const quoteIn = new BN(Math.round(BUY_SOL * 1e9));
  // buyQuoteInput: exact WSOL in, slippage bps
  const ixs = await off.buyQuoteInput(st, quoteIn, 500);
  const bh = await conn.getLatestBlockhash();
  const msg = new TransactionMessage({ payerKey: dev.publicKey, recentBlockhash: bh.blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 500_000 }), ...ixs] }).compileToV0Message();
  const tx = new VersionedTransaction(msg); tx.sign([dev]);
  const b64 = Buffer.from(tx.serialize()).toString("base64");
  const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sendTransaction", params: [b64, { encoding: "base64", skipPreflight: true }] }) }).then((x) => x.json());
  if (r.result) {
    for (let i = 0; i < 30; i++) {
      await new Promise((res) => setTimeout(res, 400));
      const v = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getSignatureStatuses", params: [[r.result]] }) }).then((x) => x.json());
      const s = v.result?.value?.[0];
      if (s?.err) { log("FAILED:", JSON.stringify(s.err).slice(0, 120)); return; }
      if (s && s.confirmationStatus) { log("Bought", BUY_SOL, "SOL → sig", r.result.slice(0, 12) + "…"); return; }
    }
  } else log("SEND ERR:", JSON.stringify(r.error || r).slice(0, 120));
}
main().catch((e) => { console.error("BUY FAILED:", e); process.exit(1); });
