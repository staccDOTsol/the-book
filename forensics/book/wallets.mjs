// wallets.mjs — spawn and persist the cast. everyone is us. theatrical.
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { Keypair } from "@solana/web3.js";

const DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "wallets");
fs.mkdirSync(DIR, { recursive: true });

export function load(name) {
  const p = path.join(DIR, name + ".json");
  if (fs.existsSync(p)) return Keypair.fromSecretKey(Buffer.from(JSON.parse(fs.readFileSync(p, "utf8"))));
  const kp = Keypair.generate();
  fs.writeFileSync(p, JSON.stringify(Array.from(kp.secretKey)));
  return kp;
}

export function loadFunder() {
  const home = process.env.HOME || require("os").homedir();
  return Keypair.fromSecretKey(Buffer.from(JSON.parse(fs.readFileSync(path.join(home, ".config/solana/id.json"), "utf8"))));
}
