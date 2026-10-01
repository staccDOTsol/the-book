// the book, self-serve — for an EXISTING token (CA). you arrive with ETH and/or bags.
// your key never leaves localStorage. the machine runs from your browser.
import { ethers } from "https://cdn.jsdelivr.net/npm/ethers@6.17.0/+esm";

const $ = id => document.getElementById(id);
const log = (m) => { $("log").textContent += "\n" + m; $("log").scrollTop = 1e9; };
const setStatus = s => $("status").textContent = s;

$("key").value = localStorage.getItem("bookkey") || "";
$("rpc").value = localStorage.getItem("bookrpc") || $("rpc").value;
$("token").value = localStorage.getItem("bookca") || "";
$("key").onchange = () => localStorage.setItem("bookkey", $("key").value.trim());
$("rpc").onchange = () => localStorage.setItem("bookrpc", $("rpc").value.trim());
$("token").onchange = () => localStorage.setItem("bookca", $("token").value.trim());

let desk = localStorage.getItem("bookdesk") || "";
let loopTimer = null;

const RPC = () => $("rpc").value.trim();
const wallet = () => new ethers.Wallet($("key").value.trim(), new ethers.JsonRpcProvider(RPC()));
const chainId = () => parseInt($("chain").value);

const DESK_ABI = ["function setUpOnly(bool)","function setPoolPrice(uint160,int24)","function setRecycleSpacing(uint16)",
  "function cycle()","function withdraw()","function stats() view returns(uint256,uint256,uint256,uint256,uint256)"];

// detect what the visitor brought: ETH + token balance
async function scan() {
  const w = wallet();
  const ca = $("token").value.trim();
  const eth = await w.provider.getBalance(w.address);
  log("wallet: " + w.address);
  log("ETH: " + ethers.formatEther(eth));
  if (ca.startsWith("0x")) {
    const bag = await new ethers.Contract(ca, ["function balanceOf(address) view returns(uint256)"], w).balanceOf(w.address);
    log("bag: " + ethers.formatEther(bag) + " tokens");
    const curve = await new ethers.Contract(ca, ["function curve() view returns(address)"], w).curve().catch(()=>null);
    if (curve) log("curve: " + curve);
  }
  setStatus("scanned");
}
window.scan = scan;

// THE MACHINE: deploy the desk for the CA, arm everything the visitor brought, start walking
async function deployMachine() {
  const w = wallet();
  const ca = $("token").value.trim();
  if (!ca.startsWith("0x")) { log("paste the CA first"); return; }

  // read the curve + live price
  const curve = await new ethers.Contract(ca, ["function curve() view returns(address)"], w).curve();
  const res = await new ethers.Contract(curve, ["function getReserves() view returns(uint256,uint256)"], w).getReserves();
  const tokR = BigInt(res[1]);
  const sp = BigInt(Math.round(Math.sqrt(Number(tokR) / Number(res[0])) * 2 ** 96));
  const tick = Math.floor(Math.log(Number(tokR) / Number(res[0])) / Math.log(1.0001));
  log("curve " + curve + " | live tick " + Math.round(tick));

  // deploy the desk (bytecode from the repo build)
  const build = await (await fetch("/Desk.json")).json();
  const code = "0x" + build.bytecode.object + ethers.AbiCoder.defaultAbiCoder()
    .encode(["address","address","address","address","address"],
      [curve, "0xd3AFEB2a57f70eF218Aa82451c51B2fb0416Ac9e", ca,
       "0x895887AB3C8B93D5A087A1DD8c9756b56Dc9ffC0", "0x8366a39CC670B4001A1121B8F6A443A643e40951"]).slice(2);
  const dep = await w.sendTransaction({ data: code, gasLimit: 6000000, gasPrice: 50000000, type: 0, chainId: chainId() });
  const rc = await dep.wait();
  desk = rc.contractAddress;
  localStorage.setItem("bookdesk", desk);
  log("DESK " + desk);

  const d = new ethers.Contract(desk, DESK_ABI, w);
  await (await d.setUpOnly(true)).wait();
  await (await d.setPoolPrice(sp, Math.round(tick))).wait();
  log("armed — pooling everything you brought…");

  // fund: all ETH minus gas | bags: transfer the whole token balance to the desk
  const eth = await w.provider.getBalance(w.address);
  const fund = eth - 2000000000000000n;
  if (fund > 0n) { await (await w.sendTransaction({to: desk, value: fund, gasPrice: 50000000, type: 0, chainId: chainId()})).wait(); log("funded " + ethers.formatEther(fund) + " ETH"); }
  const bag = await new ethers.Contract(ca, ["function balanceOf(address) view returns(uint256)","function transfer(address,uint256)"], w).balanceOf(w.address);
  if (bag > 0n) { await (await new ethers.Contract(ca, ["function transfer(address,uint256) returns(bool)"], w).transfer.staticCall) ; await (await new ethers.Contract(ca, ["function transfer(address,uint256) returns(bool)"], w).connect(w).transfer(desk, bag)).wait(); log("armed with " + ethers.formatEther(bag) + " tokens"); }
  setStatus("machine live — hit WALK");
}
window.deployMachine = deployMachine;

// the walk — cycles every 5s from the browser
async function walk() {
  if (!desk) { log("deploy the machine first"); return; }
  const w = wallet();
  const d = new ethers.Contract(desk, DESK_ABI, w);
  setStatus("WALKING");
  log("loop on");
  loopTimer = setInterval(async () => {
    try {
      await d.cycle();
      const s = await d.stats();
      log("c" + s[0] + " buys:" + s[1] + " eth:" + ethers.formatEther(s[4]).slice(0,7));
    } catch (e) { log("·"); }
  }, 5000);
}
window.walk = walk;
function stop() { clearInterval(loopTimer); loopTimer = null; setStatus("idle"); log("stopped"); }
window.stop = stop;

// the exit — withdraw everything back
async function exitAll() {
  if (!desk) return;
  const w = wallet();
  await (await new ethers.Contract(desk, DESK_ABI, w).withdraw()).wait();
  log("swept — everything back to your wallet");
  setStatus("exited");
}
window.exitAll = exitAll;

if (desk) { log("restored desk " + desk); setStatus("ready — " + desk.slice(0,10)); }
