// build-pools.mjs — kit-side: pre-flight config txs + Orca pool-init envelopes as JSON.
// Runs against MAINNET (pre-flight sends) with deterministic builders (no account fetches).
import {
  createSolanaRpc, address, generateKeyPairSigner, createKeyPairSignerFromPrivateKeyBytes,
} from "@solana/kit";
import {
  getWhirlpoolAddress, getFeeTierAddress, getTokenBadgeAddress,
  getInitializePoolV2Instruction, getInitializeDynamicTickArrayInstruction,
  getInitializeFeeTierInstruction, getSetDefaultProtocolFeeRateInstruction,
  WhirlpoolDeployment,
} from "@orca-so/whirlpools-client";
import { getFullRangeTickIndexes, getTickArrayStartTickIndex, priceToSqrtPrice, sqrtPriceToTickIndex } from "@orca-so/whirlpools-core";
import { buildTransaction } from "@orca-so/tx-sender";
import { getSignatureFromTransaction } from "@solana/transactions";
import fs from "fs";

const RPC = process.env.RPC_URL || `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
const OUR_CONFIG = address("19zArjWikZJL3eLS6ouvFrY4bwrX9KmrmQWCLHtSLU8");
const PID = WhirlpoolDeployment.mainnet.programId;
const TOKEN = address("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
const SYSTEM = address("11111111111111111111111111111111");
const RENT = address("SysvarRent111111111111111111111111111111111");

// pool plan: [tickSpacing, initialPrice(A-per-B), label]
const PLAN = process.env.POOL_PLAN
  ? JSON.parse(process.env.POOL_PLAN)
  : [
      { ts: 64, price: 1, label: "real" },
      { ts: 128, price: 5, label: "ladder5x" },
      { ts: 220, price: 25, label: "ladder25x" },
      { ts: 96, price: 100, label: "ladder100x" },
      { ts: 8, price: 250, label: "ladder250x" },
      { ts: 16, price: 500, label: "ladder500x" },
      { ts: 32, price: 1000, label: "ladder1000x" },
      { ts: 1, price: 2500, label: "ladder2500x" },
      { ts: 256, price: 5000, label: "ladder5000x" },
    ];

async function post(m, p) {
  const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: m, params: p }) }).then((x) => x.json());
  if (r.error) throw new Error(m + ": " + JSON.stringify(r.error).slice(0, 200));
  return r.result;
}

async function main() {
  const MINT = address(process.env.MINT);
  const MINT_B = address(process.env.MINT_B || "So11111111111111111111111111111111111111112"); // quote = WSOL
  const rpc = createSolanaRpc(RPC);

  // any decimals + any token program: read from the mint account when it exists,
  // else fall back to env (pre-creation: pump.fun mints are 6dp SPL Token)
  async function mintInfo(mint, fallbackDecimals) {
    try {
      const { fetchMaybeMint } = await import("@solana-program/token-2022");
      const m = await fetchMaybeMint(rpc, mint);
      if (m.exists)
        return { decimals: m.data.decimals, program: m.programAddress.toString() };
    } catch {}
    return {
      decimals: Number(process.env.TOKEN_DECIMALS ?? fallbackDecimals),
      program: process.env.TOKEN_PROGRAM ?? "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    };
  }
  const infoA_src = await mintInfo(MINT, 6);
  const infoB_src = await mintInfo(MINT_B, 9);
  const log2 = (...a) => console.log(...a);
  log2("token mint:", MINT.toString(), "decimals:", infoA_src.decimals, "program:", infoA_src.program);
  log2("quote mint:", MINT_B.toString(), "decimals:", infoB_src.decimals, "program:", infoB_src.program);

  // canonical order: A < B by bytes
  const ab = Buffer.from(MINT, "base64"); // Address is base58 in kit — do it via bytes
  // simpler: derive both orders and check which PDA has the right order via builder assertion —
  // kit's orderMints is exported from @orca-so/whirlpools; use it:
  const { orderMints } = await import("@orca-so/whirlpools");
  const [mintA, mintB] = orderMints(MINT, MINT_B);
  const A_IS_TOKEN = mintA === MINT;

  // ── pre-flight 1: zero the protocol fee on our config ──
  const funder = await (async () => {
    const { setPayerFromBytes } = await import("@orca-so/whirlpools");
    const home = process.env.HOME;
    const sk = new Uint8Array(JSON.parse(fs.readFileSync(home + "/.config/solana/id.json", "utf8")));
    // kit needs 64-byte secret key: id.json is already 64 bytes
    return await setPayerFromBytes(sk);
  })();

  const out = { config: OUR_CONFIG, mint: MINT, mintA, mintB, aIsToken: A_IS_TOKEN, pools: [] };

  // set_default_protocol_fee_rate → 0 (signed by id.json as fee authority)
  const setFee = getSetDefaultProtocolFeeRateInstruction(
    { whirlpoolsConfig: OUR_CONFIG, feeAuthority: funder, defaultProtocolFeeRate: 0 },
    { programAddress: PID },
  );
  // fee tiers for every spacing in the plan — hand-encoded for the DEPLOYED
  // program's account list: [config, feeTier, funder(sig,w), feeAuthority(sig), system]
  // (the kit builder emits the newer 5-account shape; deployed wants both signers)
  const tierIxs = [];
  const tierSpacings = [...new Set(PLAN.map((p) => p.ts))];
  for (const ts of tierSpacings) {
    const [feeTier] = await getFeeTierAddress(ts, { configAddress: OUR_CONFIG, programId: PID });
    // skip tiers that already exist (idempotent re-runs)
    try {
      const exists = await rpc.getAccountInfo(feeTier).send();
      if (exists.value) { console.log("tier ts", ts, "already exists — skip"); continue; }
    } catch {}
    const kitIx = getInitializeFeeTierInstruction(
      { config: OUR_CONFIG, feeTier, funder, tickSpacing: ts, defaultFeeRate: 3000 },
      { programAddress: PID },
    );
    const FEE_AUTH = funder.address.toString();
    tierIxs.push({
      programId: PID.toString(),
      keys: [
        { pubkey: OUR_CONFIG.toString(), isSigner: false, isWritable: false },
        { pubkey: feeTier.toString(), isSigner: false, isWritable: true },
        { pubkey: FEE_AUTH, isSigner: true, isWritable: true },
        { pubkey: FEE_AUTH, isSigner: true, isWritable: false },
        { pubkey: "11111111111111111111111111111111", isSigner: false, isWritable: false },
      ],
      data: Buffer.from(kitIx.data).toString("base64"),
    });
  }

  // kit AccountMeta: role 0=readonly, 1=writable, 2=readonly-signer, 3=writable-signer
  const dump = (ix) => ({
    programId: ix.programAddress.toString(),
    keys: ix.accounts.map((a) => ({ pubkey: a.address.toString(),
      isSigner: a.role === 2 || a.role === 3,
      isWritable: a.role === 1 || a.role === 3 })),
    data: Buffer.from(ix.data).toString("base64"),
  });

  out.preflight = [dump(setFee), ...tierIxs];

  // ── pool envelopes ──
  for (const spec of PLAN) {
    const initialPrice = A_IS_TOKEN ? spec.price : 1 / spec.price; // A-per-B
    const decA = A_IS_TOKEN ? infoA_src.decimals : infoB_src.decimals;
    const decB = A_IS_TOKEN ? infoB_src.decimals : infoA_src.decimals;
    const progA = A_IS_TOKEN ? infoA_src.program : infoB_src.program;
    const progB = A_IS_TOKEN ? infoB_src.program : infoA_src.program;
    const sqrtPrice = priceToSqrtPrice(initialPrice, decA, decB); // (decA, decB)
    const [whirlpool] = await getWhirlpoolAddress(mintA, mintB, spec.ts, { configAddress: OUR_CONFIG, programId: PID });
    const [feeTier] = await getFeeTierAddress(spec.ts, { configAddress: OUR_CONFIG, programId: PID });
    const [badgeA] = await getTokenBadgeAddress(mintA, { configAddress: OUR_CONFIG, programId: PID });
    const [badgeB] = await getTokenBadgeAddress(mintB, { configAddress: OUR_CONFIG, programId: PID });
    const mkVault = async () => {
      const { randomBytes } = await import("node:crypto");
      const seed = new Uint8Array(randomBytes(32));
      const signer = await createKeyPairSignerFromPrivateKeyBytes(seed);
      return { signer, seed: Array.from(seed) };
    };
    const vaultA = await mkVault();
    const vaultB = await mkVault();
    const ix = getInitializePoolV2Instruction(
      {
        whirlpoolsConfig: OUR_CONFIG, tokenMintA: mintA, tokenMintB: mintB,
        tokenBadgeA: badgeA, tokenBadgeB: badgeB, funder,
        whirlpool, tokenVaultA: vaultA.signer, tokenVaultB: vaultB.signer,
        tokenProgramA: address(progA), tokenProgramB: address(progB), feeTier,
        tickSpacing: spec.ts, initialSqrtPrice: sqrtPrice,
      },
      { programAddress: PID },
    );
    // tick arrays: full-range lower/upper + current
    const fr = getFullRangeTickIndexes(spec.ts);
    const initialTick = sqrtPriceToTickIndex(sqrtPrice);
    const starts = [...new Set([
      getTickArrayStartTickIndex(fr.tickLowerIndex, spec.ts),
      getTickArrayStartTickIndex(fr.tickUpperIndex, spec.ts),
      getTickArrayStartTickIndex(initialTick, spec.ts),
    ])];
    const tickArrays = [];
    for (const s of starts) {
      const { getTickArrayAddress } = await import("@orca-so/whirlpools-client");
      const [ta] = await getTickArrayAddress(whirlpool, s, PID);
      const tix = getInitializeDynamicTickArrayInstruction(
        { whirlpool, funder, tickArray: ta, startTickIndex: s, idempotent: true },
        { programAddress: PID },
      );
      tickArrays.push({ startTickIndex: s, addr: ta.toString(), ix: dump(tix) });
    }
    out.pools.push({
      label: spec.label, ts: spec.ts, initialPrice: spec.price,
      pool: whirlpool.toString(), feeTier: feeTier.toString(),
      init: dump(ix), tickArrays,
      vaultASeed: vaultA.seed, vaultBSeed: vaultB.seed,
    });
  }

  fs.writeFileSync(new URL("./pools.json", import.meta.url), JSON.stringify(out, null, 1));
  console.log("pools.json written:", out.pools.length, "pools; preflight ixs:", out.preflight.length);
  console.log("funder (fee authority):", funder.address.toString());
}

main().catch((e) => { console.error("BUILD FAILED:", e); process.exit(1); });
