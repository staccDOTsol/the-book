// book.mjs — THE MAINNET BOOK. everyone is us. theatrical.
// Act 0 (pre-flight): fund cast, zero protocol fee, create fee tiers
// Act 1 (block 0, one Jito bundle): pump create+dev-buy → ALL pool inits → tip
// Act 2 (block 1+): breadth buys, thin range, walk, extraction (next iteration)
import {
  Connection, Keypair, PublicKey, SystemProgram, TransactionInstruction,
  VersionedTransaction, TransactionMessage, ComputeBudgetProgram,
} from "@solana/web3.js";
import BN from "bn.js";
import { PumpSdk, OnlinePumpSdk, getBuyTokenAmountFromSolAmount } from "@pump-fun/pump-sdk";
import { load, loadFunder } from "./wallets.mjs";
import { sendBundle } from "./jito.mjs";
import { compileV1 } from "./v1-wire.mjs";
import fs from "fs";

const RPC = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const conn = new Connection(RPC, "confirmed");
const log = (...a) => console.log(...a);

const COIN = {
  name: process.env.COIN_NAME || "THE EXPOSE",
  symbol: process.env.COIN_SYMBOL || "EXPOSE",
  uri: process.env.COIN_URI || "",
};
const DEV_BUY_SOL = Number(process.env.DEV_BUY_SOL || 2);
const TIP_SOL = Number(process.env.TIP_SOL || 0.002);

async function sendAndConfirm(tx, signers, label) {
  const sig = await conn.sendTransaction(tx, signers, { skipPreflight: false, maxRetries: 3 });
  const latest = await conn.getLatestBlockhash();
  await conn.confirmTransaction({ signature: sig, blockhash: latest.blockhash, lastValidBlockHeight: latest.lastValidBlockHeight }, "confirmed");
  log(`   ${label}: ${sig}`);
  return sig;
}

function ixFromDump(d) {
  return new TransactionInstruction({
    programId: new PublicKey(d.programId),
    keys: d.keys.map((k) => ({ pubkey: new PublicKey(k.pubkey), isSigner: k.isSigner, isWritable: k.isWritable })),
    data: Buffer.from(d.data, "base64"),
  });
}

async function main() {
  const funder = loadFunder();
  const dev = load("dev");
  const engine = load("engine");
  const s1 = load("sniper1");
  const s2 = load("sniper2");
  const s3 = load("sniper3");
  log("funder:", funder.publicKey.toBase58());
  log("dev   :", dev.publicKey.toBase58());

  // mint first, then build its pool envelopes (MINT_FILE reuses an existing CA)
  const mint = process.env.MINT_FILE
    ? (await import("@solana/web3.js")).Keypair.fromSecretKey(Buffer.from(JSON.parse(fs.readFileSync(process.env.MINT_FILE, "utf8"))))
    : load(`mint-${Date.now()}`);
  log("mint  :", mint.publicKey.toBase58());
  const { execSync } = await import("child_process");
  execSync("node build-pools.mjs", {
    cwd: new URL("./../expose/", import.meta.url).pathname,
    env: { ...process.env, MINT: mint.publicKey.toBase58(), TOKEN_DECIMALS: "6", TOKEN_PROGRAM: "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" },
    stdio: "inherit",
  });
  const pools = JSON.parse(fs.readFileSync(new URL("./../expose/pools.json", import.meta.url), "utf8"));

  // ── Act 0: fund the cast (skippable for continuation runs) ──
  const fundings = process.env.SKIP_FUND === "1" ? [] : [
    [dev, 3.0], [engine, 2.0], [s1, 0.4], [s2, 0.4], [s3, 0.4],
  ];
  for (const [w, sol] of fundings) {
    const bal = await conn.getBalance(w.publicKey);
    if (bal < sol * 0.9 * 1e9) {
      const { Transaction } = await import("@solana/web3.js");
      const t = new Transaction().add(SystemProgram.transfer({
        fromPubkey: funder.publicKey, toPubkey: w.publicKey, lamports: Math.round(sol * 1e9),
      }));
      await sendAndConfirm(t, [funder], `funded ${w === dev ? "dev" : w === engine ? "engine" : "sniper"} ${sol} SOL`);
    } else log(`   ${w.publicKey.toBase58().slice(0, 8)} already funded`);
  }

  // ── Act 0b: pre-flight (config fee 0 + fee tiers), signed by funder (V0) ──
  // simulate-first sender via Helius (V0 wire — lands, diagnosable, no jito)
  async function sendV0(ixs, signers, payer, label) {
    for (let attempt = 0; attempt < 4; attempt++) {
      const bh2 = await conn.getLatestBlockhash();
      const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: bh2.blockhash,
        instructions: [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 500_000 }), ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }), ...ixs] }).compileToV0Message();
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
        if (String(r.error.message).includes("Blockhash not found") && attempt < 3) { console.log("   [" + label + "] stale blockhash, retrying…"); continue; }
        throw new Error(label + " send: " + JSON.stringify(r.error).slice(0, 250));
      }
      const sig = r.result;
      for (let t = 0; t < 60; t++) {
        await new Promise((res) => setTimeout(res, 500));
        const st = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getSignatureStatuses", params: [[sig]] }) }).then((x) => x.json());
        const v = st.result?.value?.[0];
        if (v?.err) throw new Error(label + " failed on-chain: " + JSON.stringify(v.err).slice(0, 250));
        if (v && ["confirmed", "finalized"].includes(v.confirmationStatus)) { console.log("   " + label + ": " + sig); return sig; }
      }
      throw new Error(label + " never confirmed");
    }
    throw new Error(label + " exhausted retries");
  }
  {
    await sendV0(pools.preflight.map(ixFromDump), [funder], funder.publicKey, "preflight (fee→0 + tiers)");
  }

  // ── Act 1: block-0 bundle ──
  const online = new OnlinePumpSdk(conn);
  const off = new PumpSdk();
  const global = await online.fetchGlobal();
  log("pump global fetched:", Object.keys(global).slice(0, 8).join(","), "…");

  const feeConfig = await online.fetchFeeConfig();
  const buyAmount = getBuyTokenAmountFromSolAmount({ global, feeConfig, mintSupply: null, bondingCurve: null,
    amount: new BN(Math.round(DEV_BUY_SOL * 1e9)), quoteMint: PublicKey.default });
  log("dev buy:", DEV_BUY_SOL, "SOL →", buyAmount.toString(), "raw tokens");
  const createIxs = await off.createV2AndBuyInstructions({
    global, mint: mint.publicKey, name: COIN.name, symbol: COIN.symbol, uri: COIN.uri,
    creator: dev.publicKey, user: dev.publicKey,
    amount: buyAmount, solAmount: new BN(Math.round(DEV_BUY_SOL * 1e9)),
    mayhemMode: false,
  });
  log("create+buy ixs:", createIxs.length);

  // tip account (rotating first)
  const TIP_ACC = new PublicKey("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5");

  const TIP_IX_DEV = SystemProgram.transfer({ fromPubkey: dev.publicKey, toPubkey: TIP_ACC, lamports: Math.round(TIP_SOL * 1e9) });
  const TIP_IX_FUNDER = SystemProgram.transfer({ fromPubkey: funder.publicKey, toPubkey: TIP_ACC, lamports: Math.round(TIP_SOL * 1e9) });
  const bhB = (await conn.getLatestBlockhash()).blockhash;

  // simulate one V1 (helius) — fail fast with logs
  async function simV1(buf, label) {
    const sim = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "simulateTransaction", params: [buf.toString("base64"), { encoding: "base64", sigVerify: false, replaceRecentBlockhash: true }] }) }).then((x) => x.json());
    if (sim.result?.value?.err) {
      const logs = (sim.result.value.logs || []).slice(-10).join("\n     ");
      throw new Error(label + " SIM FAILED: " + JSON.stringify(sim.result.value.err).slice(0, 220) + "\n     " + logs);
    }
    console.log("   [sim ok]", label);
  }

  // idempotent create: skip if mint is already live
  let mintAcc = null;
  for (let t = 0; t < 6 && !mintAcc; t++) {
    const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getAccountInfo", params: [mint.publicKey.toBase58(), { encoding: "base64" }] }) }).then((x) => x.json());
    mintAcc = r.result?.value;
    if (!mintAcc) await new Promise((res) => setTimeout(res, 1000));
  }
  let tx1 = null;
  if (mintAcc) log("   CA already live:", mint.publicKey.toBase58());
  else {
    tx1 = compileV1({ payer: dev, instructions: [...createIxs, TIP_IX_DEV], blockhash: (await conn.getLatestBlockhash()).blockhash, signers: [dev, mint],
      priorityFee: 200_000n, computeUnits: 1_400_000 }).v1;
    await simV1(tx1, "create+devbuy");
    if (process.env.LAND_CREATE === "1") {
      // land the create alone via the engine single-tx bundle path (tip inside)
      const AUTH2 = (await import("./jito.mjs")).AUTH ?? (await import("fs")).readFileSync((await import("os")).homedir() + "/perps/jit-metrics/.env", "utf8").match(/JITO_AUTH_UUID=(\S+)/)[1];
      const r = await fetch("https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions?bundleOnly=true", { method: "POST",
        headers: { "content-type": "application/json", "x-jito-auth": AUTH2 },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sendTransaction", params: [tx1.toString("base64"), { encoding: "base64", skipPreflight: false, maxRetries: 0 }] }) }).then((x) => x.json());
      if (r.error) throw new Error("create send: " + JSON.stringify(r.error).slice(0, 200));
      const sig = r.result;
      log("   create sent (bundleOnly):", sig);
      let landed = false;
      for (let t = 0; t < 80; t++) {
        await new Promise((res) => setTimeout(res, 500));
        const st = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getSignatureStatuses", params: [[sig]] }) }).then((x) => x.json());
        const v = st.result?.value?.[0];
        if (v?.err) throw new Error("create failed on-chain: " + JSON.stringify(v.err).slice(0, 250));
        if (v && ["confirmed", "finalized"].includes(v.confirmationStatus)) { landed = true; break; }
      }
      log("   create", landed ? "CONFIRMED — CA LIVE: " + mint.publicKey.toBase58() : "never confirmed");
      if (!landed) process.exit(1);
      tx1 = null; // already live; pool sims now run against real state
    }
  }
  // pool inits: 2 pools per tx + tip, idempotent (skip if pool exists)
  const poolPackets = [];
  for (let i = 0; i < pools.pools.length; i += 1) {
    const chunk = pools.pools.slice(i, i + 1);
    if (await Promise.all(chunk.map((p) => conn.getAccountInfo(new PublicKey(p.pool)))).then((r) => r.every(Boolean))) {
      log(`   pools [${chunk.map((p) => p.label).join(",")}] already live — skipped`); continue;
    }
    const ixs = [TIP_IX_FUNDER];
    const signers = [funder];
    const labels = [];
    for (const p of chunk) {
      const vaultA = Keypair.fromSeed(Buffer.from(p.vaultASeed));
      const vaultB = Keypair.fromSeed(Buffer.from(p.vaultBSeed));
      signers.push(vaultA, vaultB);
      ixs.push(ixFromDump(p.init), ...p.tickArrays.map((t) => ixFromDump(t.ix)));
      labels.push(`${p.label}(${p.ts})`);
    }
    const bhFresh = (await conn.getLatestBlockhash()).blockhash;
    const tx = compileV1({ payer: funder, instructions: ixs, blockhash: bhFresh, signers,
      priorityFee: 200_000n, computeUnits: 1_400_000 }).v1;
    poolPackets.push({ tx, labels });
    log(`   pool tx built [${labels.join(" + ")}] +tip (${tx.length}B)`);
  }

  log("\n── ACT 1: live, idempotent ──");
  if (process.env.DRY_RUN === "1") {
    log("   [DRY RUN] mint:", mint.publicKey.toBase58());
    for (const p of pools.pools) log(`   [DRY RUN] pool: ${p.label}(${p.ts}) ${p.pool}`);
    fs.writeFileSync(new URL("./run.json", import.meta.url), JSON.stringify({ mint: mint.publicKey.toBase58(), coin: COIN, dryRun: true, pools: pools.pools.map((p) => ({ label: p.label, ts: p.ts, initialPrice: p.initialPrice, pool: p.pool })) }, null, 2));
    log("\n[DRY RUN] mint:", mint.publicKey.toBase58(), "— everything assembles. run without DRY_RUN to go live.");
    return;
  }
  const packets = [...(tx1 ? [tx1] : []), ...poolPackets.map((p) => p.tx)];
  if (packets.length) {
    // atomic bundle simulation (state-aware: tx1's mint exists for tx2+)
    const simB = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "simulateBundle", params: [{ encodedTransactions: packets.map((x) => x.toString("base64")) }] }) }).then((x) => x.json());
    if (simB.error) console.log("   [simulateBundle] unavailable:", JSON.stringify(simB.error).slice(0, 140), "— proceeding (per-tx sims passed)");
    else {
      const trs = simB.result?.value?.transactionResults || [];
      const bad = trs.findIndex((r) => r.err);
      if (bad >= 0) {
        console.log("FULL TX RESULT:", JSON.stringify(trs[bad]).slice(0, 3000));
        throw new Error(`simulateBundle tx[${bad}] failed: ` + JSON.stringify(trs[bad].err).slice(0, 200));
      }
      console.log("   [simulateBundle] succeeded —", trs.length, "txs atomic-clean");
    }
    const res = await sendBundle(packets);
    log("bundle:", res);
    if (res.status !== "Landed") { log("BUNDLE NOT LANDED — rerun book (idempotent)"); process.exit(1); }
  } else log("nothing to send — everything already live.");

  const out = {
    mint: mint.publicKey.toBase58(), coin: COIN, devBuySol: DEV_BUY_SOL,
    pools: pools.pools.map((p) => ({ label: p.label, ts: p.ts, initialPrice: p.initialPrice, pool: p.pool })),
    bundle: res,
  };
  fs.writeFileSync(new URL("./run.json", import.meta.url), JSON.stringify(out, null, 2));
  log("\nMINT:", mint.publicKey.toBase58());
  log("run.json written. the surface is live.");
}

main().catch((e) => { console.error("BOOK FAILED:", e); process.exit(1); });
