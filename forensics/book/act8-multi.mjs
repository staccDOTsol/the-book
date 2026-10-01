import { Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram, PublicKey } from "@solana/web3.js";
import BN from "bn.js";
import { OnlinePumpAmmSdk, PumpAmmSdk } from "@pump-fun/pump-swap-sdk";
import { load } from "./wallets.mjs";
const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const POOL = new PublicKey("49PEdr3Jbbm5544rtsiwS7y3LGXhk7uLRJi9FpRamkPA");
const BUY_SOL = Number(process.env.BUY_SOL || 0.05);
async function buy(wallet, label) {
  const online = new OnlinePumpAmmSdk(conn);
  const off = new PumpAmmSdk();
  const st = await online.swapSolanaState(POOL, wallet.publicKey);
  const ixs = await off.buyQuoteInput(st, new BN(Math.round(BUY_SOL * 1e9)), 5000);
  const bh = await conn.getLatestBlockhash();
  const msg = new TransactionMessage({ payerKey: wallet.publicKey, recentBlockhash: bh.blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 500_000 }), ...ixs] }).compileToV0Message();
  const tx = new VersionedTransaction(msg); tx.sign([wallet]);
  const b64 = Buffer.from(tx.serialize()).toString("base64");
  const sim = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "simulateTransaction", params: [b64, { encoding: "base64", sigVerify: false, replaceRecentBlockhash: true }] }) }).then((x) => x.json());
  if (sim.result?.value?.err) { console.log(label, "SIM FAIL:", JSON.stringify(sim.result.value.err).slice(0, 100)); return; }
  const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sendTransaction", params: [b64, { encoding: "base64", skipPreflight: true }] }) }).then((x) => x.json());
  if (r.result) console.log(label, "Bought", BUY_SOL, "SOL ✓");
  else console.log(label, "ERR:", JSON.stringify(r.error || r).slice(0, 80));
}
async function main() {
  const wallets = [load("dev"), load("engine"), load("sniper1"), load("sniper2"), load("sniper3")];
  await Promise.all(wallets.map((w, i) => buy(w, ["dev", "engine", "s1", "s2", "s3"][i])));
}
main().catch(e => { console.error("MULTI FAILED:", e); process.exit(1); });
