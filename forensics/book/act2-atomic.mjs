// act2-atomic.mjs — ONE atomic Jito bundle: dev pulls from curve → recycles SOL → engine walks HARD.
import {
  Connection, Keypair, VersionedTransaction, TransactionMessage, ComputeBudgetProgram, PublicKey, SystemProgram,
} from "@solana/web3.js";
import BN from "bn.js";
import { PumpSdk, OnlinePumpSdk, getBuyTokenAmountFromSolAmount, getSellSolAmountFromTokenAmount } from "@pump-fun/pump-sdk";
import { load } from "./wallets.mjs";
import { sendBundle } from "./jito.mjs";

const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const MINT = new PublicKey(process.env.MINT || "8YxaREY5nejPRPztoskVPkWQAAAXxviZvNeTMETHf2X9");
const T2022 = new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
const TIP_ACC = new PublicKey("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5");
const SELL_M = Number(process.env.SELL_M || 18);   // millions of tokens dev sells
const ENGINE_BUY_SOL = Number(process.env.ENGINE_SOL || 0.5); // basis for the walk buy
const log = (...a) => console.log(...a);

async function mkV0(payer, ixs, signers) {
  const bh = await conn.getLatestBlockhash();
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh.blockhash,
    instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 500_000 }), ComputeBudgetProgram.setComputeUnitLimit({ units: 1_000_000 }), ...ixs] }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  tx.sign(signers);
  return tx.serialize();
}

async function main() {
  const dev = load("dev");
  const engine = load("engine");
  const online = new OnlinePumpSdk(conn);
  const off = new PumpSdk();
  const global = await online.fetchGlobal();
  const feeConfig = await online.fetchFeeConfig();

  // ── tx1: dev sells SELL_M million tokens back into the curve ──
  const st = await online.fetchSellState(MINT, dev.publicKey, T2022);
  const amount = new BN(Math.round(SELL_M * 1e6 * 1e6)); // millions → raw
  const solOut = getSellSolAmountFromTokenAmount({ global, feeConfig, mintSupply: null, bondingCurve: st.bondingCurve,
    amount, quoteMint: st.quoteMint, quoteTokenProgram: st.quoteTokenProgram, creatorFeeBps: st.bondingCurve.creatorFeeBps });
  log("pull:", SELL_M, "M tokens, est", Number(solOut) / 1e9, "SOL out");
  const sellIxs = await off.sellV2Instructions({ global, bondingCurveAccountInfo: st.bondingCurveAccountInfo, bondingCurve: st.bondingCurve,
    mint: MINT, user: dev.publicKey, amount, quoteAmount: new BN(1), slippage: 10_000, // zero floor — take whatever the curve gives
    tokenProgram: T2022, quoteTokenProgram: st.quoteTokenProgram });
  const tx1 = await mkV0(dev.publicKey, [...sellIxs, SystemProgram.transfer({ fromPubkey: dev.publicKey, toPubkey: TIP_ACC, lamports: 15_000_000 })], [dev]);

  // ── tx2: dev recycles everything minus 1 SOL to the engine ──
  const devBal = await conn.getBalance(dev.publicKey);
  const push = BigInt(devBal) - 1_200_000_000n; // current balance - keep (pull stays in dev as buffer)
  log("recycle to engine ≈", Number(push) / 1e9, "SOL");
  const tx2 = await mkV0(dev.publicKey, [
    SystemProgram.transfer({ fromPubkey: dev.publicKey, toPubkey: engine.publicKey, lamports: push > 0n ? push : 100_000_000n }),
    SystemProgram.transfer({ fromPubkey: dev.publicKey, toPubkey: TIP_ACC, lamports: 15_000_000 }),
  ], [dev]);

  // ── tx3: engine walks HARD into the curve ──
  const stE = await online.fetchBuyState(MINT, engine.publicKey);
  const walkAmt = getBuyTokenAmountFromSolAmount({ global, feeConfig, mintSupply: null, bondingCurve: stE.bondingCurve,
    amount: new BN(Math.round(ENGINE_BUY_SOL * 1e9)), quoteMint: stE.quoteMint, quoteTokenProgram: stE.quoteTokenProgram, creatorFeeBps: stE.bondingCurve.creatorFeeBps });
  log("walk: engine buys", Number(walkAmt) / 1e6, "M tokens (basis", ENGINE_BUY_SOL, "SOL)");
  const buyIxs = await off.buyV2Instructions({ global, bondingCurveAccountInfo: stE.bondingCurveAccountInfo, bondingCurve: stE.bondingCurve,
    associatedUserAccountInfo: stE.associatedUserAccountInfo, mint: MINT, user: engine.publicKey,
    amount: walkAmt, quoteAmount: new BN(Math.round(ENGINE_BUY_SOL * 1e9)), slippage: 10_000,
    tokenProgram: T2022, quoteTokenProgram: stE.quoteTokenProgram });
  const tx3 = await mkV0(engine.publicKey, [...buyIxs, SystemProgram.transfer({ fromPubkey: engine.publicKey, toPubkey: TIP_ACC, lamports: 15_000_000 })], [engine]);

  log("\n── atomic: pull + recycle + walk, ONE bundle ──");
  const res = await sendBundle([tx1, tx2, tx3]);
  log("bundle:", res);
  log("dev:", (await conn.getBalance(dev.publicKey)) / 1e9, "SOL | engine:", (await conn.getBalance(engine.publicKey)) / 1e9, "SOL");
}

main().catch((e) => { console.error("ATOMIC FAILED:", e); process.exit(1); });
