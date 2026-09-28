"""Fast path: predictable commands skip the LLM entirely.

Patterns live in config/aurelia.yaml (hot-reloaded). A match yields the tool,
its arguments and a templated reply. Arguments are then coerced to the types
the tool's input schema declares, so "3" becomes 3 for an integer field.
"""
import re
from dataclasses import dataclass
from typing import Any

from .config import FastPathPattern

_PLACEHOLDER = re.compile(r"\{(\w+)(?:\|([^}]*))?\}")


@dataclass
class FastPathMatch:
    pattern: FastPathPattern
    args: dict[str, Any]
    groups: dict[str, str]


def _fill(template: str, groups: dict[str, str | None]) -> str:
    return _PLACEHOLDER.sub(lambda m: groups.get(m.group(1)) or (m.group(2) or ""), template)


_POLITE = re.compile(r"^(?:(?:hey\s+)?(?:aura|aurelia)[,:]?\s+)?(?:(?:please|can you|could you|would you|will you)\s+)?"
                     r"(?P<rest>.*?)(?:[,\s]+please)?$", re.IGNORECASE)


def match(text: str, patterns: list[FastPathPattern]) -> FastPathMatch | None:
    cleaned = " ".join(text.strip().rstrip(".!?").split())
    cleaned = _POLITE.match(cleaned).group("rest") or cleaned
    for p in patterns:
        try:
            m = re.fullmatch(p.match, cleaned, flags=re.IGNORECASE)
        except re.error:
            continue  # a bad pattern in config never breaks the rest
        if m:
            groups = {k: v for k, v in m.groupdict().items()}
            args = {k: _fill(v, groups) for k, v in p.args.items()}
            filled = {k: (v if v is not None else "") for k, v in groups.items()}
            filled.update({k: str(v) for k, v in args.items()})
            return FastPathMatch(pattern=p, args=args, groups=filled)
    return None


def coerce(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Cast string arguments to the types in the tool's JSON schema; drop empty optionals."""
    props = schema.get("properties", {})
    out: dict[str, Any] = {}
    for key, value in args.items():
        kind = props.get(key, {}).get("type")
        if value == "" and key not in schema.get("required", []):
            continue
        try:
            if kind == "integer":
                value = int(value)
            elif kind == "number":
                value = float(value)
            elif kind == "boolean":
                value = str(value).lower() in ("1", "true", "yes", "on")
        except (TypeError, ValueError):
            pass  # leave as-is; the tool validates
        out[key] = value
    return out


def render_reply(pattern: FastPathPattern, groups: dict[str, str]) -> str:
    return _fill(pattern.reply, groups)
