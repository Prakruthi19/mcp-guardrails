// Drives server_broken.mjs over raw stdin/stdout, request-by-request, and
// checks whether every stdout line is valid JSON-RPC.
import { spawn } from "node:child_process";
import readline from "node:readline";

const requests = [
  {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "verify", version: "0.0.1" },
    },
  },
  { jsonrpc: "2.0", method: "notifications/initialized" },
  {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: "echo", arguments: { text: "hello" } },
  },
  {
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "echo", arguments: { text: "world" } },
  },
];

const proc = spawn(process.execPath, ["server_broken.mjs"], { stdio: ["pipe", "pipe", "pipe"] });

const rl = readline.createInterface({ input: proc.stdout });
const lines = [];
rl.on("line", (line) => lines.push(line));

let stderrBuf = "";
proc.stderr.on("data", (d) => (stderrBuf += d.toString()));

await new Promise((r) => setTimeout(r, 500)); // let the child finish starting up

for (const req of requests) {
  proc.stdin.write(JSON.stringify(req) + "\n");
  if (req.id !== undefined) {
    // wait a bit for the response to arrive
    await new Promise((r) => setTimeout(r, 300));
  }
}
await new Promise((r) => setTimeout(r, 300));
proc.stdin.end();
proc.kill();

console.log(`stdout: ${lines.length} line(s)`);
let bad = 0;
lines.forEach((line, i) => {
  try {
    const obj = JSON.parse(line);
    if (obj.jsonrpc !== "2.0") throw new Error("missing jsonrpc field");
    console.log(`  [${i}] OK id=${obj.id} kind=${"error" in obj ? "error" : "result"}`);
  } catch (e) {
    bad++;
    console.log(`  [${i}] NOT VALID JSON-RPC: ${e.message} -- raw: ${JSON.stringify(line)}`);
  }
});

console.log(`\nstderr contents:\n${stderrBuf}`);
console.log(bad ? `\nFAIL: ${bad} corrupted stdout line(s)` : "\nPASS: stdout stayed clean");
