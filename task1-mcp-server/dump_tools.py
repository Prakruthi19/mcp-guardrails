"""Prints the full tools/list response (schema + annotations) as a real MCP
client would see it. Run with: .venv/Scripts/python.exe dump_tools.py
"""

import json
import subprocess
import sys

requests = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "dump_tools", "version": "0.0.1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
]

proc = subprocess.Popen(
    [sys.executable, "server.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

for req in requests:
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    if "id" in req:
        line = proc.stdout.readline()
        obj = json.loads(line)
        if obj.get("id") == 2:
            print(json.dumps(obj["result"]["tools"], indent=2))

proc.stdin.close()
proc.terminate()
