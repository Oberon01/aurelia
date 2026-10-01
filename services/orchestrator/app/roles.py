"""What each device is for. Her behavior follows from it.

  presentation  shows things; nobody sits at it (the TV PC, a wall display). Only what's sent to it
                appears, over an ambient screen; she may run its screen on his word without asking.
  workstation   a computer he uses (desktop, laptop). Panels he asks for open there; she never takes
                over its screen without asking. Exam and gaming modes live here.
  server        no screen (the home server, the GPU box, a NAS). Health, services, fixes; no panels.
  companion     his phone: personal, goes with him; what he asks there stays there.
  room          a room unit (microphone and speaker, later): no screen.

A role is chosen on the devices panel (stored in devices.role). Until then it's guessed: a node
running her shell is presentation, one with a screen is a workstation, one without is a server (a
node's --role at install is its own suggestion); a phone's browser is a companion, other browsers
workstations; a node's screen takes its node's role. Aurelia can read roles, never set them.
"""
import json
from typing import Any

ROLES = {
    "presentation": "Shows things; only what's sent to it, over an ambient screen",
    "workstation": "A computer you use; she never takes over its screen without asking",
    "server": "No screen: health, services and fixes",
    "companion": "Your phone: what you ask there stays there",
    "room": "A room unit: listens and speaks, no screen",
}
FOR_KIND = {"node": ("presentation", "workstation", "server", "room"),
            "browser": ("workstation", "presentation", "companion")}
PHONES = ("Android", "iPhone", "iPad", "iOS")
# How people say a group of them ("send this to all presentation devices").
GROUPS = {
    "presentation": ("presentation", "presentations", "presentation devices", "presentation screens",
                     "presentation displays", "presentation nodes", "tvs", "televisions", "tv screens"),
    "workstation": ("workstation", "workstations", "computers i use", "desks"),
    "companion": ("phone", "phones", "companion", "companions", "mobile", "mobiles"),
}
EVERYWHERE = ("everywhere", "all screens", "all displays", "all devices", "every screen", "every display",
              "every device", "all my screens", "all my devices", "all of them")


def info_of(row: Any) -> dict:
    v = row.get("info") if hasattr(row, "get") else None
    if isinstance(v, str):
        try:
            v = json.loads(v or "{}")
        except ValueError:
            v = {}
    return v if isinstance(v, dict) else {}


def guess(kind: str, info: dict) -> str | None:
    if kind == "node":
        hint = info.get("role")
        if hint in FOR_KIND["node"]:
            return hint
        if info.get("shell") == "on":
            return "presentation"
        if "screens" in info and not info.get("screens"):
            return "server"
        return "workstation"
    if kind == "browser":
        return "companion" if any(p in str(info.get("os") or "") for p in PHONES) else "workstation"
    if kind == "screen":
        return "presentation"
    return None                                   # a terminal is for typing, not a role


def effective(kind: str, chosen: str | None, info: dict, node_role: str | None = None) -> str | None:
    """The role in force: the one he chose, else the guess. A node's screen follows its node."""
    if kind == "screen":
        return node_role or "presentation"
    if chosen in FOR_KIND.get(kind, ()):
        return chosen
    return guess(kind, info)


def _one(phrase: str) -> str:
    """'presentation displays' and 'presentation display' read the same."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in phrase.split())


def group(words: str) -> str | None:
    """'all presentation devices' → 'presentation'; 'everywhere' → '*'; anything else → None."""
    import re
    w = " ".join(re.sub(r"\bthe\b", " ", words.lower()).split())
    if w in EVERYWHERE:
        return "*"
    m = re.match(r"^(?:all|every|each)(?: one)?(?: of)?(?: my)? (.+)$", w)
    if not m:
        return None
    rest = _one(m.group(1))
    if rest in ("screen", "display", "device", "them", "of them"):
        return "*"
    for role, names in GROUPS.items():
        if rest in {_one(n) for n in names}:
            return role
    return None
