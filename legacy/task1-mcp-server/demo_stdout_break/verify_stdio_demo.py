"""DEMO OF A BROKEN SERVER: same test as ../verify_stdio.py, but pointed at
server_broken.py, which has one stray `print()` left in a tool handler.
Shows exactly how a stray stdout write corrupts the JSON-RPC stream.
"""

import json
import pathlib
import subprocess
import sys

SERVER_PATH = pathlib.Path(__file__).parent / "server_broken.py"

requests = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "verify_stdio", "version": "0.0.1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-00001"}},
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "get_customer_record", "arguments": {"customer_id": "not-valid"}},
    },
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-00002"}},
    },
]

proc = subprocess.Popen(
    [sys.executable, str(SERVER_PATH)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

assert proc.stdin and proc.stdout

lines: list[str] = []
try:
    for req in requests:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        if "id" in req:  # notifications get no response
            line = proc.stdout.readline()
            lines.append(line.rstrip("\n"))
finally:
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

stderr_text = proc.stderr.read()
proc.stderr.close()
proc.stdout.close()

print(f"stdout: {len(lines)} response line(s) (expected 5)")
bad = 0
for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
        assert obj.get("jsonrpc") == "2.0", "missing/invalid jsonrpc field"
        tag = "error" if "error" in obj else "result"
        print(f"  [{i}] OK id={obj.get('id')} kind={tag}")
    except Exception as exc:
        bad += 1
        print(f"  [{i}] NOT VALID JSON-RPC: {exc!r} -- raw: {line!r}")

print(f"\nstderr had {len([l for l in stderr_text.splitlines() if l.strip()])} line(s) (expected: all logging output)")
print("stderr contents:")
print(stderr_text)

if bad or len(lines) != 5:
    print(f"\nFAIL: bad={bad} responses={len(lines)}")
    sys.exit(1)
else:
    print("\nPASS: stdout is pure JSON-RPC, all 4 responses received")
