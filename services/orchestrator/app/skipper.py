"""Skipper (Phase 12.5): his phone as the command center.

The phone opens on my functions; a function opens its commands; a command opens its details (if it needs
any). Tapping one sends its sentence exactly as if he'd typed it (/ask: the same fast path, rules,
confirmations and record), so there's nothing here that runs anything: this only says what's on the menu.

The menu is config/menu.yaml (read when it changes). A function shows while it's running, a command while
its tool is there; services connected through Lagrange add themselves with their commands that need
nothing more. Pick lists come from what I already know: his computers and displays (paired with me), and
his lights, locks and TVs (from what Orbit last reported).
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request

from . import db, registry

log = logging.getLogger("aurelia.skipper")
router = APIRouter(prefix="/skipper", tags=["skipper"])

PICKS = ("computers", "displays", "lights", "locks", "tvs", "thermostats")
FIELD = re.compile(r"\{(\w+)\}")
OPTIONAL = re.compile(r"\[([^\[\]]*)\]")
_cache: dict[str, Any] = {"mtime": None, "menu": None, "path": None}


def menu_path(app=None) -> Path:
    from .settings import settings
    return Path(settings.config_path).parent / "menu.yaml"


def fill(say: str, values: dict[str, Any]) -> str:
    """The sentence with his details in it; an optional [part] goes when its details are empty."""
    def opt(m):
        part = m.group(1)
        names = FIELD.findall(part)
        if any(not str(values.get(n) or "").strip() for n in names):
            return ""
        return part
    text = OPTIONAL.sub(opt, say)
    text = FIELD.sub(lambda m: " ".join(str(values.get(m.group(1)) or "").split()), text)
    return " ".join(text.split())


def check_entry(c: dict) -> str | None:
    """What's wrong with one command, or None."""
    say, tool = c.get("say"), c.get("tool")
    if not isinstance(c.get("label"), str) or not c["label"].strip():
        return "a command needs a label"
    if not isinstance(say, str) or not say.strip():
        return f"{c['label']}: needs a sentence (say)"
    if not isinstance(tool, str) or "." not in tool:
        return f"{c['label']}: needs the tool it reaches (tool: function.tool)"
    fields = c.get("fields") or {}
    if not isinstance(fields, dict):
        return f"{c['label']}: fields must be a mapping"
    named = set(FIELD.findall(say))
    if named != set(fields):
        return f"{c['label']}: the sentence's details ({', '.join(sorted(named)) or 'none'}) and fields don't match"
    inside = {n for part in OPTIONAL.findall(say) for n in FIELD.findall(part)}
    for name, f in fields.items():
        if not isinstance(f, dict):
            return f"{c['label']}: {name} must be a mapping"
        if f.get("pick") and f["pick"] not in PICKS:
            return f"{c['label']}: {name} picks from {f['pick']}, which isn't one of {', '.join(PICKS)}"
        if f.get("optional") and name not in inside:
            return f"{c['label']}: {name} is optional but isn't inside [brackets]"
        if not f.get("optional") and name in inside:
            return f"{c['label']}: {name} is inside [brackets] but not optional"
    return None


SAMPLE = {"computers": "TV PC", "displays": "phone", "lights": "kitchen light", "locks": "front door",
          "tvs": "living room tv", "thermostats": "thermostat"}


def problems(raw: Any, patterns: list | None) -> list[str]:
    """What's wrong with a menu file: each command well formed, and (given today's fast paths) each sentence,
    filled with a sample, with and without its optional parts, reaching its tool by a fast path."""
    return [msg for _, _, msg in _problems(raw, patterns)]


def _problems(raw: Any, patterns: list | None) -> list[tuple[str, str, str]]:
    from . import fastpath
    if not isinstance(raw, dict) or not isinstance(raw.get("functions"), list):
        return [("", "", "menu.yaml is functions: a list, each with its commands")]
    out = []
    for fn in raw["functions"]:
        if not isinstance(fn, dict) or not fn.get("name"):
            out.append(("", "", "a function needs a name"))
            continue
        for c in fn.get("commands") or []:
            label = str(c.get("label") or "") if isinstance(c, dict) else ""
            problem = check_entry(c) if isinstance(c, dict) else "a command must be a mapping"
            if problem:
                out.append((fn["name"], label, f"{fn['name']}: {problem}"))
                continue
            if patterns is None:                         # only how it's written
                continue
            fields = c.get("fields") or {}
            for with_optional in (True, False):
                vals = {}
                for name, f in fields.items():
                    if f.get("optional") and not with_optional:
                        continue
                    vals[name] = str(f.get("sample") or SAMPLE.get(f.get("pick")) or (f["choices"][0] if f.get("choices") else "")
                                     or ("42" if f.get("number") else f.get("placeholder") or "something"))
                text = fill(c["say"], vals)
                m = fastpath.match(text, patterns)
                if m is None:
                    out.append((fn["name"], label, f"{fn['name']} · {label}: “{text}” isn't one of my fast paths (it would go to the model)"))
                elif m.pattern.tool != c["tool"]:
                    out.append((fn["name"], label, f"{fn['name']} · {label}: “{text}” reaches {m.pattern.tool}, not {c['tool']}"))
                if not any(f.get("optional") for f in fields.values()):
                    break
    return out


def load(path: Path) -> dict:
    """The menu file, checked: a bad command is left out (and logged), never the whole menu."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("menu.yaml couldn't be read: %s", exc)
        return {"functions": []}
    out = []
    for fn in raw.get("functions") or []:
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        cmds = []
        for c in fn.get("commands") or []:
            if not isinstance(c, dict):
                continue
            problem = check_entry(c)
            if problem:
                log.warning("menu.yaml: %s", problem)
                continue
            fields = []
            for name, f in (c.get("fields") or {}).items():      # in the order they're written
                fields.append({"name": name, "label": str(f.get("label") or name.title()),
                               **({"placeholder": str(f["placeholder"])} if f.get("placeholder") else {}),
                               **({"pick": f["pick"]} if f.get("pick") else {}),
                               **({"choices": [str(x) for x in f["choices"]]} if f.get("choices") else {}),
                               **({"number": True} if f.get("number") else {}),
                               **({"optional": True} if f.get("optional") else {}),
                               **({"sample": str(f["sample"])} if f.get("sample") else {})})
            cmds.append({"label": c["label"], "say": c["say"], "tool": c["tool"], "fields": fields,
                         **({"about": str(c["about"])} if c.get("about") else {})})
        out.append({"name": str(fn["name"]), "label": str(fn.get("label") or fn["name"].title()),
                    "about": str(fn.get("about") or ""), "commands": cmds})
    return {"functions": out}


def current() -> dict:
    p = menu_path()
    try:
        mtime = os.stat(p).st_mtime
    except OSError:
        return {"functions": []}
    if _cache["menu"] is None or _cache["mtime"] != mtime or _cache["path"] != p:
        _cache.update(menu=load(p), mtime=mtime, path=p)
    return _cache["menu"]


async def _connections(app) -> list[dict]:
    """Each connected service, with its commands that need nothing more (its name and the tool's words)."""
    from .assistant import _FILLER
    out = []
    for fn in await registry.list_all(app.state.redis):
        if fn.via != "lagrange":
            continue
        name = fn.name.replace("_", " ")
        cmds = []
        for t in fn.tools:
            if (t.input_schema or {}).get("required"):
                continue
            words = [w for w in t.name.split("_") if w]
            if not set(words) - _FILLER:
                continue
            cmds.append({"label": " ".join(words).capitalize(), "say": f"{name} {' '.join(words)}",
                         "tool": f"{fn.name}.{t.name}", "fields": [],
                         **({"about": t.description[:140]} if t.description else {})})
        if cmds:
            out.append({"name": fn.name, "label": name.title(), "about": "Connected through Lagrange",
                        "commands": cmds, "connection": True})
    return out


_routes: dict[str, Any] = {"key": None, "ok": set()}


def reachable(app) -> set[tuple[str, str]]:
    """(function, label) of each command whose sentence reaches its tool by today's fast paths. One that doesn't
    (a fast path changed, or isn't there yet) isn't offered: tapping it would go to the model, or somewhere else."""
    pats = app.state.config.current.fastpath.patterns
    key = (_cache.get("mtime"), id(pats))
    if _routes["key"] != key:
        raw = {"functions": [{"name": f["name"], "commands": [
            {"label": c["label"], "say": c["say"], "tool": c["tool"], "fields": {
                x["name"]: {k: v for k, v in x.items() if k not in ("name", "label")} | ({"sample": x["sample"]} if x.get("sample") else {})
                for x in c["fields"]}} for c in f["commands"]]} for f in current()["functions"]]}
        bad = set()
        for name, label, msg in _problems(raw, pats):
            bad.add((name, label))
            log.warning("menu.yaml: %s", msg)
        _routes.update(key=key, ok={(f["name"], c["label"]) for f in current()["functions"] for c in f["commands"]} - bad)
    return _routes["ok"]


@router.get("/menu")
async def menu(request: Request):
    app = request.app
    have = {f"{fn.name}.{t.name}" for fn in await registry.list_all(app.state.redis) for t in fn.tools}
    ok = reachable(app)
    funcs = []
    for fn in current()["functions"]:
        cmds = [c for c in fn["commands"] if c["tool"] in have and (fn["name"], c["label"]) in ok]
        if cmds:
            funcs.append({**fn, "commands": cmds})
    try:
        funcs += await _connections(app)
    except Exception:
        log.exception("connections for the menu")
    return {"functions": funcs}


async def _home(user: str, kinds: set[str]) -> list[str]:
    """Names of his home devices of these kinds, from what Orbit last reported (the past two weeks)."""
    rows = await db.pool().fetch(
        "SELECT panel FROM panel_snapshots WHERE tool LIKE 'orbit.%' AND user_id=$1 AND panel IS NOT NULL "
        "AND captured_at > now() - interval '14 days' ORDER BY captured_at DESC LIMIT 60", user)
    names: dict[str, None] = {}
    for r in rows:
        p = r["panel"]
        p = json.loads(p) if isinstance(p, str) else (p or {})
        for d in [*(p.get("devices") or []), *(p.get("tvs") or []), p]:
            if isinstance(d, dict) and d.get("kind") in kinds and isinstance(d.get("name"), str) and d["name"].strip():
                names.setdefault(d["name"].strip(), None)
    return sorted(names, key=str.lower)


@router.get("/pick/{source}")
async def pick(source: str, request: Request, user: str = "owner"):
    if source not in PICKS:
        raise HTTPException(404, "no such list")
    if source in ("computers", "displays"):
        from .events import DISPLAYS
        kinds = ("node",) if source == "computers" else DISPLAYS
        rows = await db.pool().fetch("SELECT name FROM devices WHERE status='approved' AND kind = ANY($1::text[]) "
                                     "ORDER BY lower(name)", list(kinds))
        return {"options": [r["name"] for r in rows if r["name"]]}
    kind = {"lights": "light", "locks": "lock", "tvs": "tv", "thermostats": "thermostat"}[source]
    return {"options": await _home(user, {kind})}
