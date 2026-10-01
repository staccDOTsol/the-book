// act3-walk.mjs — THE MEGA WALK. all remaining cast SOL into the curve, atomically.
import {
  Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram, PublicKey, SystemProgram,
} from "@solana/web3.js";
import BN from "bn.js";
import { PumpSdk, OnlinePumpSdk, getBuyTokenAmountFromSolAmount } from "@pump-fun/pump-sdk";
import { load } from "./wallets.mjs";
import { sendBundle } from "./jito.mjs";

const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const MINT = new PublicKey(process.env.MINT || "8YxaREY5nejPRPztoskVPkWQAAAXxviZvNeTMETHf2X9");
const T2022 = new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
const TIP_ACC = new PublicKey("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5");
const log = (...a) => console.log(...a);

async function mkV0(payer, ixs, signers) {
  const bh = await conn.getLatestBlockhash();
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh.blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1_000_000 }), ComputeBudgetProgram.setComputeUnitLimit({ units: 1_000_000 }), ...ixs] }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  tx.sign(signers);
  return tx.serialize();
}

async function main() {
  const dev = load("dev"), engine = load("engine");
  const s1 = load("sniper1"), s2 = load("sniper2"), s3 = load("sniper3");
  const online = new OnlinePumpSdk(conn);
  const off = new PumpSdk();
  const global = await online.fetchGlobal();
  const feeConfig = await online.fetchFeeConfig();

  async function buyIx(wallet, solBasis, label) {
    const st = await online.fetchBuyState(MINT, wallet.publicKey);
    const amt = getBuyTokenAmountFromSolAmount({ global, feeConfig, mintSupply: null, bondingCurve: st.bondingCurve,
      amount: new BN(Math.round(solBasis * 1e9)), quoteMint: st.quoteMint, quoteTokenProgram: st.quoteTokenProgram, creatorFeeBps: st.bondingCurve.creatorFeeBps });
    const ixs = await off.buyV2Instructions({ global, bondingCurveAccountInfo: st.bondingCurveAccountInfo, bondingCurve: st.bondingCurve,
      associatedUserAccountInfo: st.associatedUserAccountInfo, mint: MINT, user: wallet.publicKey,
      amount: amt, quoteAmount: new BN(Math.round(solBasis * 1e9)), slippage: 10_000,
      tokenProgram: T2022, quoteTokenProgram: st.quoteTokenProgram });
    log("   " + label + ": " + Number(amt) / 1e6 + "M tokens (basis " + solBasis + " SOL)");
    return ixs;
  }

  const devBal = await conn.getBalance(dev.publicKey);
  const push = BigInt(devBal) - 800_000_000n;
  log("dev recycles", Number(push) / 1e9, "SOL to engine");

  // ONE bundle: [dev→engine transfer, engine MEGA buy, sniper buys] — atomic, up hard.
  const txs = [];
  txs.push(await mkV0(dev.publicKey, [
    SystemProgram.transfer({ fromPubkey: dev.publicKey, toPubkey: engine.publicKey, lamports: push }),
    SystemProgram.transfer({ fromPubkey: dev.publicKey, toPubkey: TIP_ACC, lamports: 30_000_000 }),
  ], [dev]));

  const engineSol = Number(push) / 1e9 + (await conn.getBalance(engine.publicKey)) / 1e9;
  const basis = Math.max(0.4, (engineSol - 1.2) * 0.13); // on-chain transfer ≈ 6x basis; keep 1.2 SOL headroom
  const eIxs = await buyIx(engine, basis, "ENGINE MEGA");
  txs.push(await mkV0(engine.publicKey, [...eIxs, SystemProgram.transfer({ fromPubkey: engine.publicKey, toPubkey: TIP_ACC, lamports: 30_000_000 })], [engine]));

  for (const [w, n] of [[s1, "sniper1"], [s2, "sniper2"], [s3, "sniper3"]]) {
    const bal = (await conn.getBalance(w.publicKey)) / 1e9;
    const basis = Math.max(0.05, (bal - 0.35) * 0.13);
    const ixs = await buyIx(w, basis, n);
    txs.push(await mkV0(w.publicKey, [...ixs, SystemProgram.transfer({ fromPubkey: w.publicKey, toPubkey: TIP_ACC, lamports: 20_000_000 })], [w]));
  }

  log("\n── THE MEGA WALK: " + txs.length + " txs, one bundle ──");
  const res = await sendBundle(txs);
  log("bundle:", res.status, res.bundleId);
  // verify by state, not by status
  await new Promise((r) => setTimeout(r, 6000));
  const [bc] = await PublicKey.findProgramAddress([Buffer.from("bonding-curve"), MINT.toBuffer()], new PublicKey("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"));
  const acc = await conn.getAccountInfo(bc);
  const vtr = acc.data.readBigUInt64LE(8), vsr = acc.data.readBigUInt64LE(16);
  const price = (Number(vsr) / 1e9) / (Number(vtr) / 1e6);
  log("curve now:", price.toExponential(3), "SOL/token | mcap ≈ $" + (price * 1e9 * 180).toFixed(0));
}

main().catch((e) => { console.error("WALK FAILED:", e); process.exit(1); });
