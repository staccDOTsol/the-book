#!/bin/zsh
cd "$(dirname "$0")"
CYCLE=0
while true; do
  CYCLE=$((CYCLE+1))
  BUY_SOL=0.3 node act8-buy-amm.mjs 2>&1 | grep -E "Bought|FAILED" | tail -1
  if [ $((CYCLE % 10)) -eq 0 ]; then
    node amm-state.mjs 2>&1 | tail -1
    node act6-claim.mjs 2>&1 | grep "Δ" | tail -1
    # only pull if dev is running low (self-refuel)
    DEV_SOL=$(node -e "
(async()=>{const {Connection,Keypair}=require('@solana/web3.js');
const fs=require('fs');
const c=new Connection('https://mainnet.helius-rpc.com/?api-key='+process.env.HELIUS_API_KEY,'confirmed');
const d=Keypair.fromSecretKey(Buffer.from(JSON.parse(fs.readFileSync('wallets/dev.json','utf8'))));
console.log((await c.getBalance(d.publicKey))/1e9)})()" 2>/dev/null)
    if (( $(echo "$DEV_SOL < 1.5" | bc -l 2>/dev/null || echo 0) )); then
      PULL_M=5 N=1 node act7-pull.mjs 2>&1 | grep "Δ" | tail -1
      echo "[$(date -u +%H:%M:%S)] REFUEL: pulled 5M for more buys"
    fi
  fi
  sleep 2
done
