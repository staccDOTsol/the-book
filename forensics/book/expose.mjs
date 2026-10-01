// ─────────────────────────────────────────────────────────────
// CRIMIN' TESTS — the book, run on a surfpool (offline snapshot)
//   crimin' test 1: the float        (mint supply, insider holds all)
//   crimin' test 2: the real pool     (init price 1.0, protocol fee 0)
//   crimin' test 3: the ladder        (zero-liq pools quoting 5x / 25x)
//   crimin' test 4: the walk           (atomic [+dL, swap, −dL], ONE tx)
//   crimin' test 5: the extraction     (float holder withdraws at the top)
// ─────────────────────────────────────────────────────────────
const RPC_URL = "http://127.0.0.1:8899";
const TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";

const log = (...a) => console.log(...a);
let PASS = 0, FAIL = 0, RESULTS = [];

async function main() {
  const { generateKeyPairSigner, createSolanaRpc, address, getBase64EncodedWireTransaction, assertIsFullySignedTransaction } = await import("@solana/kit");
  const { setRpc, orderMints, createConcentratedLiquidityPoolInstructions, openPositionInstructions,
          increaseLiquidityInstructions, decreaseLiquidityInstructions, swapInstructions,
          WhirlpoolDeployment } = await import("@orca-so/whirlpools");
  const { buildTransaction } = await import("@orca-so/tx-sender");
  const { getCreateAccountInstruction } = await import("@solana-program/system");
  const { getInitializeMintInstruction, getMintToInstruction, findAssociatedTokenPda,
          getCreateAssociatedTokenIdempotentInstruction } = await import("@solana-program/token");
  const { increaseLiquidityQuote, increaseLiquidityQuoteA, increaseLiquidityQuoteB, sqrtPriceToPrice } = await import("@orca-so/whirlpools-core");
  const { fetchWhirlpool } = await import("@orca-so/whirlpools-client");
  const { getSignatureFromTransaction } = await import("@solana/transactions");

  const rpc = createSolanaRpc(RPC_URL);
  await setRpc(RPC_URL);
  const DEP = WhirlpoolDeployment.mainnet;

  async function post(m, p) {
    const r = await fetch(RPC_URL, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: m, params: p }) }).then((r) => r.json());
    if (r.error) throw new Error(m + ": " + JSON.stringify(r.error).slice(0, 180));
    return r.result;
  }
  async function send(ixs, payer) {
    const tx = await buildTransaction(ixs, payer);
    assertIsFullySignedTransaction(tx);
    const b64 = getBase64EncodedWireTransaction(tx);
    const hash = getSignatureFromTransaction(tx);
    for (let attempt = 0; ; attempt++) {
      try { await post("sendTransaction", [b64, { encoding: "base64", skipPreflight: true, maxRetries: 0 }]); break; }
      catch (e) {
        if (attempt >= 6 || !String(e).includes("Failed to fetch accounts")) throw e;
        await new Promise((r) => setTimeout(r, 1500 + attempt * 1000));
      }
    }
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 250));
      const st = (await post("getSignatureStatuses", [[hash]]))?.value?.[0];
      if (st && st.err) {
        try {
          const sim = await post("simulateTransaction", [b64, { encoding: "base64", sigVerify: false, replaceRecentBlockhash: true }]);
          const logs = sim?.value?.logs || [];
          console.log("   [fail-sim] " + (logs.filter((l) => /failed|Custom|invoke/.test(l)).slice(-12).join("\n            ")));
        } catch {}
        throw new Error("tx failed: " + JSON.stringify(st.err).slice(0, 180));
      }
      if (st && ["processed", "confirmed", "finalized"].includes(st.confirmationStatus)) return hash;
    }
    throw new Error("tx not confirmed in 30s: " + hash);
  }
  const t = (name, n, fn) => async () => {
    log(`\n── crimin' test ${n}: ${name} ──`);
    try { await fn(); log(`   crimin' test ${n}: PASS ✓`); RESULTS.push([n, name, "PASS"]); PASS++; }
    catch (e) { log(`   crimin' test ${n}: FAIL ✗ — ${String(e).slice(0, 160)}`); RESULTS.push([n, name, "FAIL"]); FAIL++; throw e; }
  };

  const insider = await generateKeyPairSigner();
  const engine = await generateKeyPairSigner();
  log("insider:", insider.address);
  log("engine :", engine.address);
  for (const w of [insider, engine]) {
    await rpc.requestAirdrop(w.address, 500_000_000_000n).send();
    for (let i = 0; i < 20; i++) {
      const b = await rpc.getBalance(w.address).send();
      if (Number(b.value) > 400_000_000_000) break;
      await new Promise((r) => setTimeout(r, 300));
    }
  }

  // ── crimin' test 1: the float ──
  let EXPOSE, LIQ;
  await t("the float", 1, async () => {
    async function mk(dec) {
      const m = await generateKeyPairSigner();
      await send([
        getCreateAccountInstruction({ payer: insider, newAccount: m, space: 82, lamports: 1461600, owner: address(TOKEN), programAddress: address(TOKEN) }),
        getInitializeMintInstruction({ mint: m.address, decimals: dec, mintAuthority: insider.address }),
      ], insider);
      return m;
    }
    EXPOSE = await mk(6); LIQ = await mk(6);
    const [iE, iL, eE, eL] = await Promise.all([
      findAssociatedTokenPda({ owner: insider.address, mint: EXPOSE.address, tokenProgram: address(TOKEN) }),
      findAssociatedTokenPda({ owner: insider.address, mint: LIQ.address, tokenProgram: address(TOKEN) }),
      findAssociatedTokenPda({ owner: engine.address, mint: EXPOSE.address, tokenProgram: address(TOKEN) }),
      findAssociatedTokenPda({ owner: engine.address, mint: LIQ.address, tokenProgram: address(TOKEN) }),
    ]).then((xs) => xs.map((x) => x[0]));
    await send([
      getCreateAssociatedTokenIdempotentInstruction({ payer: insider, owner: insider.address, mint: EXPOSE.address, tokenProgram: address(TOKEN), ata: iE }),
      getCreateAssociatedTokenIdempotentInstruction({ payer: insider, owner: insider.address, mint: LIQ.address, tokenProgram: address(TOKEN), ata: iL }),
      getCreateAssociatedTokenIdempotentInstruction({ payer: insider, owner: engine.address, mint: EXPOSE.address, tokenProgram: address(TOKEN), ata: eE }),
      getCreateAssociatedTokenIdempotentInstruction({ payer: insider, owner: engine.address, mint: LIQ.address, tokenProgram: address(TOKEN), ata: eL }),
      getMintToInstruction({ mint: EXPOSE.address, token: iE, mintAuthority: insider, amount: 1_000_000_000_000_000n }),
      getMintToInstruction({ mint: LIQ.address, token: iL, mintAuthority: insider, amount: 200_000_000_000n }),
      getMintToInstruction({ mint: EXPOSE.address, token: eE, mintAuthority: insider, amount: 50_000_000_000n }),
      getMintToInstruction({ mint: LIQ.address, token: eL, mintAuthority: insider, amount: 200_000_000_000n }),
    ], insider);
    const sup = await rpc.getTokenSupply(EXPOSE.address).send();
    log("   EXPOSE:", EXPOSE.address, " supply:", sup.value.uiAmountString, "(100% held by insider)");
    log("   LIQ   :", LIQ.address);
  })();

  const [mintA, mintB] = orderMints(EXPOSE.address, LIQ.address);
  const A_IS_EXPOSE = mintA === EXPOSE.address;
  const pAB = (e) => (A_IS_EXPOSE ? e : 1 / e);
  const toE = (p) => (A_IS_EXPOSE ? p : 1 / p);
  const sorted2 = (x, y) => (x < y ? [x, y] : [y, x]);
  const poolPrice = async (pa) => toE(Number(sqrtPriceToPrice((await fetchWhirlpool(rpc, address(pa))).data.sqrtPrice, 6, 6)));
  const tokenBalance = async (owner, mint) => {
    const [a] = await findAssociatedTokenPda({ owner, mint, tokenProgram: address(TOKEN) });
    return Number(((await rpc.getTokenAccountBalance(a).send()).value.uiAmountString) ?? 0);
  };

  // ── crimin' test 2: the real pool ──
  let real, ip, ep;
  await t("the real pool", 2, async () => {
    real = await createConcentratedLiquidityPoolInstructions(rpc, mintA, mintB, 64,
      { initialPrice: pAB(1), funder: insider, whirlpoolDeployment: DEP });
    await send(real.instructions, insider);
    const wp = await fetchWhirlpool(rpc, address(real.poolAddress.toString()));
    log("   pool:", real.poolAddress.toString());
    log("   quoted price at birth (zero trades):", await poolPrice(real.poolAddress.toString()), "EXPOSE/LIQ");
    log("   protocol_fee_rate:", wp.data.protocolFeeRate, "(config default zeroed)");
    // insider seeds thin range [0.97, 1.03]
    ip = await openPositionInstructions(rpc, address(real.poolAddress.toString()),
      { tokenMaxA: 20_000_000_000n, tokenMaxB: 20_000_000_000n }, ...sorted2(pAB(0.97), pAB(1.03)),
      { funder: insider, whirlpoolDeployment: DEP });
    await send(ip.instructions, insider);
    // engine opens empty position same range
    ep = await openPositionInstructions(rpc, address(real.poolAddress.toString()),
      { tokenMaxA: 1000n, tokenMaxB: 1000n }, ...sorted2(pAB(0.97), pAB(1.03)),
      { funder: engine, whirlpoolDeployment: DEP });
    await send(ep.instructions, engine);
    log("   insider position:", ip.positionMint.toString(), "[0.97, 1.03]");
    log("   engine position :", ep.positionMint.toString(), "(dust, standing by)");
  })();

  // ── crimin' test 3: the ladder ──
  let ladders = [];
  await t("the ladder", 3, async () => {
    for (const ts of [128, 220, 96, 32, 8, 1]) {
      if (ladders.length >= 2) break;
      try {
        const mult = ladders.length === 0 ? 5 : 25;
        const L = await createConcentratedLiquidityPoolInstructions(rpc, mintA, mintB, ts,
          { initialPrice: pAB(mult), funder: insider, whirlpoolDeployment: DEP });
        await send(L.instructions, insider);
        ladders.push({ ts, mult, pool: L.poolAddress.toString() });
      } catch (e) { log("   ts", ts, "skipped:", String(e).slice(0, 60)); }
    }
    log("   real pool quotes:", await poolPrice(real.poolAddress.toString()));
    for (const L of ladders)
      log(`   ladder ts=${L.ts} quotes ${await poolPrice(L.pool)} (target ${L.mult}x, liquidity $0, capital $0)`);
    if (ladders.length === 0) throw new Error("no ladder pools created");
  })();

  // ── crimin' test 4: the walk ──
  await t("the walk", 4, async () => {
    const eLiq0 = await tokenBalance(engine.address, LIQ.address);
    const eExp0 = await tokenBalance(engine.address, EXPOSE.address);
    const p0 = await poolPrice(real.poolAddress.toString());
    const wp = await fetchWhirlpool(rpc, address(real.poolAddress.toString()));
    const JIT = 5_000n * 1_000_000n;
    const q = A_IS_EXPOSE
      ? increaseLiquidityQuoteB(JIT, 0, wp.data.sqrtPrice, -305, 295)
      : increaseLiquidityQuoteA(JIT, 0, wp.data.sqrtPrice, -305, 295);
    log("   jit quote: liquidity", q.liquidityDelta.toString(), "tokenMaxA", q.tokenMaxA.toString(), "tokenMaxB", q.tokenMaxB.toString());
    // bind the quote to the LIQ side exactly; dec removes exactly what was added
    const HUGE = 10n ** 15n;
    const maxes = A_IS_EXPOSE ? { tokenMaxA: HUGE, tokenMaxB: JIT } : { tokenMaxA: JIT, tokenMaxB: HUGE };
    const inc = await increaseLiquidityInstructions(rpc, address(ep.positionMint.toString()),
      maxes,
      { authority: engine, funder: engine, slippageToleranceBps: 10_000, whirlpoolDeployment: DEP });
    await send(inc.instructions, engine);
    // read the ACTUAL liquidity the program added
    const { fetchPosition, getPositionAddress } = await import("@orca-so/whirlpools-client");
    const [posAddr] = await getPositionAddress(address(ep.positionMint.toString()), DEP.programId);
    const pos = await fetchPosition(rpc, posAddr);
    const L_ACT = pos.data.liquidity;
    log("   actual liquidity on-chain:", L_ACT.toString(), "(quote said", q.liquidityDelta.toString() + ")");
    const swp = await swapInstructions(rpc, { inputAmount: 3_000_000_000n, mint: LIQ.address },
      address(real.poolAddress.toString()),
      { signer: engine, slippageToleranceBps: 10_000, whirlpoolDeployment: DEP });
    const dec = await decreaseLiquidityInstructions(rpc, address(ep.positionMint.toString()),
      { liquidity: L_ACT },
      { authority: engine, funder: engine, slippageToleranceBps: 10_000, whirlpoolDeployment: DEP });
    const sig = await send([...swp.instructions, ...dec.instructions], engine);
    const p1 = await poolPrice(real.poolAddress.toString());
    const eLiq1 = await tokenBalance(engine.address, LIQ.address);
    const eExp1 = await tokenBalance(engine.address, EXPOSE.address);
    log("   one tx: [swap → −dL] sig:", sig, "(+dL pre-staged, pull is atomic with the fill)");
    log("   price:", p0, "→", p1, ` (x${(p1 / p0).toFixed(2)} violent uppy)`);
    log("   engine LIQ Δ:", (eLiq1 - eLiq0).toFixed(3), " EXPOSE Δ:", (eExp1 - eExp0).toFixed(3), "— liquidity in+out same tx, no overnight inventory");
  })();

  // ── crimin' test 5: the extraction ──
  await t("the extraction", 5, async () => {
    const iLiq0 = await tokenBalance(insider.address, LIQ.address);
    const { fetchPosition, getPositionAddress } = await import("@orca-so/whirlpools-client");
    const [ipAddr] = await getPositionAddress(address(ip.positionMint.toString()), DEP.programId);
    const ipAct = await fetchPosition(rpc, ipAddr);
    log("   insider position liquidity:", ipAct.data.liquidity.toString());
    const di = await decreaseLiquidityInstructions(rpc, address(ip.positionMint.toString()),
      { liquidity: ipAct.data.liquidity },
      { authority: insider, funder: insider, slippageToleranceBps: 10_000, whirlpoolDeployment: DEP });
    const sig = await send(di.instructions, insider);
    const iLiq1 = await tokenBalance(insider.address, LIQ.address);
    log("   withdrawal sig:", sig);
    log("   insider LIQ:", iLiq0, "→", iLiq1);
    log("   captured above seed: ≈", (iLiq1 - iLiq0 - 200_000 + 20_000).toFixed(3), "LIQ (the walk's flow, sold above 1.0)");
  })();

  log("\n════════ CRIMIN' TEST RESULTS ════════");
  for (const [n, name, r] of RESULTS) log(`  crimin' test ${n}: ${name} — ${r}`);
  log(`  ${PASS} pass / ${FAIL} fail`);
}

main().catch((e) => { console.error("EXPOSE ABORTED:", e); process.exit(1); });
