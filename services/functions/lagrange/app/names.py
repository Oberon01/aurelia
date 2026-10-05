"""Names, tool specs and scrubbing: the small rules every connection shares.

- A connection's name is its function name ("gitea"): lower-case letters, digits and single underscores,
  never one of my own functions' names.
- A connected service's tools keep their own names for calling, but are registered under a safe name
  (letters, digits, underscores) so "gitea.list_my_repos" always means one tool.
- What a service says about its tools (descriptions, schemas) is text I didn't write: it's capped, kept as
  data, and pinned. A tool whose description or schema changes is held until he approves it again.
- Any stored secret that shows up in what a service sends back is cut out before it leaves Lagrange.
"""
import hashlib
import json
import re
from typing import Any

# My own functions and core parts, and words that would read badly as a connection's name.
RESERVED = {"probe", "orbit", "nebula", "kepler", "parallax", "constellation", "quasar", "pulsar", "admiral",
            "morpho", "mnemosyne", "chrysalis", "swallowtail", "lagrange", "iris", "beacon", "kuiper", "libra",
            "nova", "voice", "aurelia", "aura", "system", "owner", "all", "everything", "connections", "connection",
            "service", "services", "hook", "hooks", "webhook", "webhooks", "a", "an", "the", "my", "new"}
MAX_NAME = 24
MAX_TOOL_NAME = 40
MAX_DESCRIPTION = 600
MAX_SCHEMA_BYTES = 8000
MAX_TOOLS = 80


def slug(label: str) -> str:
    """"Home Assistant" -> "home_assistant"; "Gitea" -> "gitea". ValueError when nothing usable is left."""
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    s = re.sub(r"_+", "_", s)[:MAX_NAME].strip("_")
    if not s or not s[0].isalpha():
        raise ValueError("Give it a name that starts with a letter, like “gitea” or “home assistant”.")
    if s in RESERVED:
        raise ValueError(f"“{label}” is already one of my own names. Call it something else.")
    return s


def tool_name(raw: str, taken: set[str]) -> str:
    """A service's tool name -> the name I register it under (unique within the connection)."""
    s = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    s = re.sub(r"_+", "_", s)[:MAX_TOOL_NAME].strip("_") or "tool"
    if not s[0].isalpha():
        s = "t_" + s
    base, n = s, 2
    while s in taken:
        s = f"{base[:MAX_TOOL_NAME - 3]}_{n}"
        n += 1
    return s


def clean_schema(schema: Any) -> dict:
    """A tool's input schema as the model may see it: an object schema, size-capped, without the service's
    own $-keys. A schema I can't use becomes "no arguments" (the tool still runs, the model just can't
    pass anything), never an error."""
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return {"type": "object", "properties": {}}
    out = {k: v for k, v in schema.items() if not str(k).startswith("$")}
    out["type"] = "object"
    out.setdefault("properties", {})
    if not isinstance(out["properties"], dict):
        out["properties"] = {}
    if len(json.dumps(out, default=str)) > MAX_SCHEMA_BYTES:
        out = {"type": "object", "properties": {k: {"description": str((v or {}).get("description", ""))[:200]}
                                                 if isinstance(v, dict) else {} for k, v in list(out["properties"].items())[:30]},
               **({"required": out["required"]} if isinstance(out.get("required"), list) else {})}
    return out


def fingerprint(tool: dict) -> str:
    """What I pin for a tool: everything the service says about it (name, title, description, hints, schema)."""
    raw = json.dumps({"n": tool.get("name"), "d": tool.get("description") or "", "t": tool.get("title") or "",
                      "a": tool.get("annotations") or {},
                      "s": tool.get("inputSchema") or tool.get("input_schema") or {}}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def shape_tools(listed: list[dict]) -> list[dict]:
    """tools/list -> the specs I keep: {name, remote, description, input_schema, fp, read_only}."""
    out, taken = [], set()
    for t in listed[:MAX_TOOLS]:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        name = tool_name(str(t["name"]), taken)
        taken.add(name)
        ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
        desc = " ".join(str(t.get("description") or t.get("title") or "").split())[:MAX_DESCRIPTION]
        out.append({"name": name, "remote": str(t["name"]), "description": desc,
                    "input_schema": clean_schema(t.get("inputSchema")), "fp": fingerprint(t),
                    "read_only": bool(ann.get("readOnlyHint"))})
    return out


def compare(pinned: list[dict], listed: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Pinned tools vs what the service lists now -> (still the same, new or changed, gone)."""
    by_remote = {t["remote"]: t for t in pinned}
    same, held, seen = [], [], set()
    for t in listed:
        seen.add(t["remote"])
        old = by_remote.get(t["remote"])
        if old is not None and old["fp"] == t["fp"]:
            same.append({**t, "name": old["name"]})                    # the name he knows it by never moves
        else:
            held.append({**t, "changed": old is not None, **({"name": old["name"]} if old else {})})
    gone = [t["remote"] for t in pinned if t["remote"] not in seen]
    return same, held, gone


def scrub(value: Any, secrets: list[str]) -> Any:
    """Every stored secret (6+ characters) cut out of whatever a service sends back."""
    secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True)
    if not secrets:
        return value

    def go(v: Any) -> Any:
        if isinstance(v, str):
            for s in secrets:
                if s in v:
                    v = v.replace(s, "[secret]")
            return v
        if isinstance(v, list):
            return [go(x) for x in v]
        if isinstance(v, dict):
            return {go(k) if isinstance(k, str) else k: go(x) for k, x in v.items()}
        return v
    return go(value)


PACKAGE = re.compile(r"^(?P<eco>npm|pypi):(?P<name>(?:@[a-z0-9][\w.-]*/)?[A-Za-z0-9][\w.-]*)"
                     r"(?P<sep>@|==)(?P<ver>\d+(?:\.\d+)*(?:[-.+][\w.]+)?)$")


def package(spec: str) -> dict:
    """"npm:@scope/name@1.2.3" or "pypi:name==1.2.3" -> what to run. Always an exact version: a local
    server never updates itself behind his back."""
    m = PACKAGE.match((spec or "").strip())
    if not m or (m["eco"] == "npm" and m["sep"] != "@") or (m["eco"] == "pypi" and m["sep"] != "=="):
        raise ValueError("Give the package with its exact version: npm:@scope/name@1.2.3 or pypi:name==1.2.3.")
    return {"ecosystem": m["eco"], "name": m["name"], "version": m["ver"],
            "spec": f"{m['name']}@{m['ver']}" if m["eco"] == "npm" else f"{m['name']}=={m['ver']}"}
