// SIMD-0385: 4096 bytes, 64 account keys, 64 instructions, at most 12 signatures.
import { PublicKey } from '@solana/web3.js';
import nacl from 'tweetnacl';

export const V1_LIMITS = Object.freeze({ bytes: 4096, accounts: 64, instructions: 64, signatures: 12 });

export function compileV1({ payer, instructions, blockhash, signers = [], priorityFee = 100_000n, computeUnits = 1_400_000 }) {
  if (!instructions.length || instructions.length > V1_LIMITS.instructions) throw new Error('V1 instruction count must be 1..64');
  if (!Number.isInteger(computeUnits) || computeUnits < 1 || computeUnits > 1_400_000) throw new Error('Invalid V1 compute-unit limit');
  const payerKey = payer.publicKey ?? payer;
  const metas = new Map();
  function merge(pubkey, isSigner, isWritable) {
    const key = pubkey.toBase58();
    const existing = metas.get(key);
    if (existing) {
      existing.isSigner ||= isSigner;
      existing.isWritable ||= isWritable;
    } else metas.set(key, { pubkey, isSigner, isWritable });
  }
  merge(payerKey, true, true);
  for (const ix of instructions) {
    if (!ix?.programId || !Array.isArray(ix.keys) || !Buffer.isBuffer(ix.data)) throw new Error('Invalid transaction instruction');
    if (ix.keys.length > 255 || ix.data.length > 65535) throw new Error('V1 instruction exceeds wire field limits');
    merge(ix.programId, false, false);
    for (const key of ix.keys) merge(key.pubkey, key.isSigner, key.isWritable);
  }
  const ordered = [...metas.values()].sort((a, b) => {
    const rank = x => x.pubkey.equals(payerKey) ? -1 : x.isSigner ? (x.isWritable ? 0 : 1) : (x.isWritable ? 2 : 3);
    return rank(a) - rank(b);
  });
  if (ordered.length > V1_LIMITS.accounts) throw new Error(`V1 has ${ordered.length} accounts; maximum is 64`);
  const signed = ordered.filter(meta => meta.isSigner);
  if (signed.length > V1_LIMITS.signatures) throw new Error('V1 has more than 12 signers');
  const readonlySigned = signed.filter(meta => !meta.isWritable).length;
  const readonlyUnsigned = ordered.filter(meta => !meta.isSigner && !meta.isWritable).length;
  const index = new Map(ordered.map((meta, i) => [meta.pubkey.toBase58(), i]));
  const config = Buffer.alloc(16);
  config.writeBigUInt64LE(BigInt(priorityFee));
  config.writeUInt32LE(computeUnits, 8);
  config.writeUInt32LE(64 * 1024 * 1024, 12);
  const prefix = Buffer.alloc(42);
  prefix.set([129, signed.length, readonlySigned, readonlyUnsigned]);
  prefix.writeUInt32LE(15, 4); // u64 fee + u32 CU + u32 loaded-data limit
  new PublicKey(blockhash).toBuffer().copy(prefix, 8);
  prefix[40] = instructions.length;
  prefix[41] = ordered.length;
  const headers = [];
  const payloads = [];
  for (const ix of instructions) {
    const indexes = ix.keys.map(meta => index.get(meta.pubkey.toBase58()));
    const header = Buffer.alloc(4);
    header[0] = index.get(ix.programId.toBase58());
    header[1] = indexes.length;
    header.writeUInt16LE(ix.data.length, 2);
    headers.push(header);
    payloads.push(Buffer.from(indexes), ix.data);
  }
  const message = Buffer.concat([prefix, ...ordered.map(meta => meta.pubkey.toBuffer()), config, ...headers, ...payloads]);
  const size = message.length + signed.length * 64;
  if (size > V1_LIMITS.bytes) throw new Error(`V1 is ${size} bytes; maximum is 4096`);
  const keypairs = new Map([payer, ...signers].filter(key => key?.secretKey).map(key => [key.publicKey.toBase58(), key]));
  const signatures = signed.map(meta => {
    const signer = keypairs.get(meta.pubkey.toBase58());
    if (!signer) throw new Error(`Missing signer ${meta.pubkey.toBase58()}`);
    return Buffer.from(nacl.sign.detached(message, signer.secretKey));
  });
  return { v1: Buffer.concat([message, ...signatures]), message, addresses: ordered.map(meta => meta.pubkey), signatures, signers: signed.map(meta => meta.pubkey) };
}
