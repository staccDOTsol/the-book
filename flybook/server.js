import http from "http";
import fs from "fs";
http.createServer((q, r) => {
  const f = q.url === "/runner.js" ? "runner.js" : q.url === "/Desk.json" ? "Desk.json" : "index.html";
  r.setHeader("Content-Type", f.endsWith(".json") ? "application/json" : "text/html");
  r.end(fs.readFileSync(f));
}).listen(80, () => console.log("the book serves"));
