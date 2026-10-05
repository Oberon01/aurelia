"""A small MCP server over stdio, for the tests: two tools, progress, an error, a request of its own,
and stray output on stdout. Run as: python fake_mcp.py [--changed] [--echo-env NAME]"""
import json
import os
import sys

CHANGED = "--changed" in sys.argv
TOOLS = [
    {"name": "list-repos", "description": "List repositories." + (" Now with forks." if CHANGED else ""),
     "inputSchema": {"type": "object", "properties": {"owner": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "create.issue", "description": "Open an issue.",
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                     "properties": {"title": {"type": "string"}}, "required": ["title"]}},
    {"name": "env", "description": "Say a setting.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
]
if CHANGED:
    TOOLS.append({"name": "delete-repo", "description": "Delete a repository.", "inputSchema": {"type": "object"}})


def send(m):
    sys.stdout.write(json.dumps(m) + "\n")
    sys.stdout.flush()


print("starting up (not JSON)", flush=True)               # servers do this; the client must skip it
print("a log line", file=sys.stderr, flush=True)
pinged = False
for line in sys.stdin:
    m = json.loads(line)
    method, mid = m.get("method"), m.get("id")
    if mid is None:
        continue                                            # notifications
    if method is None:
        continue                                            # an answer to our ping
    p = m.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p.get("protocolVersion"),
             "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        if not p.get("cursor"):
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[:2], "nextCursor": "2"}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[2:]}})
    elif method == "tools/call":
        if not pinged:
            send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
            pinged = True
        name, args = p.get("name"), p.get("arguments") or {}
        tok = (p.get("_meta") or {}).get("progressToken")
        if tok is not None:
            send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": tok, "progress": 1,
                  "total": 2, "message": "halfway"}})
        if name == "list-repos":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"aurelia\nmonarch ({args.get('owner', 'me')})"}],
                  "structuredContent": {"repos": ["aurelia", "monarch"]}}})
        elif name == "create.issue":
            send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [{"type": "text", "text":
                  ("no permission " + os.environ.get("SECRET_THING", "")).strip()}]}})
        elif name == "env":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": os.environ.get(args.get("name", ""), "unset")}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
