#!/bin/zsh
export PATH="$PATH:/Users/stacc/.foundry/bin"
cd "$(dirname "$0")"
RPC=${DRPC_URL:-https://rpc.mainnet.chain.robinhood.com}
KEY=0x$(cat ~/staccoverflow.eth)
STACC=0x26e8134ecc3af5cce32f34b03e7bd2f318b25158
DESK=0xBA85ec82cD97300312d95e430d2c6eE729CEa0BB
CLAIM=0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e
CYCLE=0

while true; do
  CYCLE=$((CYCLE+1))

  # THE ATOMIC CYCLE — claim + EMA + dip-buy/spike-sell + grid growth + tick, ONE TX
  cast send $DESK "cycle()" --rpc-url $RPC --private-key $KEY >/dev/null 2>&1

  # fee relay (every 10 cycles): stacc claims Pons fees, forwards to desk
  if [ $((CYCLE % 2)) -eq 0 ]; then
    ERR=$(cast send $CLAIM "claim(uint256)" 0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff --rpc-url $RPC --private-key $KEY 2>&1)
    if [[ "$ERR" == *"InsufficientBalance"* ]]; then
      AVAIL=$(echo "$ERR" | grep -oE '\], ([0-9]+)' | grep -oE '[0-9]+$')
      if [ -n "$AVAIL" ] && [ "$AVAIL" -gt 50000000000000 ] 2>/dev/null; then
        cast send $CLAIM "claim(uint256)" $AVAIL --rpc-url $RPC --private-key $KEY >/dev/null 2>&1
        cast send $DESK --value $(printf '%d' $((AVAIL * 90 / 100))) --rpc-url $RPC --private-key $KEY >/dev/null 2>&1
        echo "[$(date -u +%H:%M:%S)] relayed $(printf '%.4f' $(echo "scale=4; $AVAIL/10^18" | bc)) ETH to desk"
      fi
    fi
    # report
    cast call $DESK "stats()" --rpc-url $RPC 2>/dev/null | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{const w=d.trim().slice(2).match(/.{64}/g);if(w&&w.length>=5)console.log('['+new Date().toISOString().slice(11,19)+'] c$CYCLE | cycles:',Number(BigInt('0x'+w[0])),'| buys:',Number(BigInt('0x'+w[1])),'| sells:',Number(BigInt('0x'+w[2])),'| claimed:',(Number(BigInt('0x'+w[3]))/1e18).toFixed(5),'| desk ETH:',(Number(BigInt('0x'+w[4]))/1e18).toFixed(4))})"
  fi

  sleep 1
done
