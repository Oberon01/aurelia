"""How Aurelia speaks: her usual voice, or a style he switches on to try.

A style changes how she says things, never what she may do. Each style is a short prompt in
config/styles/<name>.md (edit it to tune her), added after her identity, plus the few fixed
lines she says without the model (fast-path replies, confirmations).

He switches by saying so ("use Raphael style", "normal style"); the choice is per person and
stays until he changes it.
"""
import re
from pathlib import Path

from .settings import settings

KEY = "aurelia:style:{}"
STYLES = ("raphael",)

_ON = re.compile(r"^(?:please\s+)?(?:use|try|switch to|turn on|go to|talk (?:like|in))\s+(?:the\s+)?"
                 r"(?P<name>raphael)(?:'s)?(?:\s+(?:style|mode|voice))?[.!]?$", re.I)
_OFF = re.compile(r"^(?:please\s+)?(?:(?:use|switch to|go back to|back to|turn on)\s+(?:your\s+)?"
                  r"(?:normal|regular|usual|default)(?:\s+(?:style|mode|voice|self))?"
                  r"|(?:normal|regular|usual|default)\s+(?:style|mode|voice)"
                  r"|(?:stop talking like|turn off|stop)\s+(?:the\s+)?raphael(?:\s+(?:style|mode|voice))?)[.!]?$", re.I)


def switch(text: str) -> str | None:
    """'raphael' to switch it on, '' to go back to normal, None if this isn't a style switch."""
    t = " ".join(text.split())
    if m := _ON.match(t):
        return m["name"].lower()
    if _OFF.match(t):
        return ""
    return None


async def current(redis, user: str) -> str:
    v = await redis.get(KEY.format(user))
    v = v.decode() if isinstance(v, bytes) else (v or "")
    return v if v in STYLES else ""


async def choose(redis, user: str, name: str) -> str:
    """Saves the choice; returns what she says about it (already in the new style)."""
    if name:
        await redis.set(KEY.format(user), name)
        return "Answer. Raphael style is active. Say \"normal style\" to return to my usual voice."
    await redis.delete(KEY.format(user))
    return "Okay, I'm back to my usual self."


def prompt(name: str) -> str:
    """The style's instructions, added after her identity (so the identity stays cached)."""
    if name not in STYLES:
        return ""
    path = Path(settings.config_path).parent / "styles" / f"{name}.md"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


# The fixed lines, in each style. A key missing here keeps her usual words.
_LINES = {
    "raphael": {
        "needs_confirmation": "Notice. This action requires your confirmation. Do you wish to proceed?",
        "declined": "Understood. The action has been cancelled.",
        "done": "Report. Complete.",
    },
}

_LABELS = ("Answer.", "Report.", "Notice.")


def line(name: str, key: str, usual: str) -> str:
    return _LINES.get(name, {}).get(key, usual)


def report(name: str, text: str) -> str:
    """A fixed reply after something she did ("Here's everything paired with me.")."""
    if name != "raphael" or not text or text.startswith(_LABELS):
        return text
    return f"Report. {text}"


def confirming(name: str, text: str, plain: bool) -> str:
    """Something is held for his OK: the generic line, or the tool's own reason, asked her way."""
    if name != "raphael":
        return text
    if plain or not text:
        return line(name, "needs_confirmation", text)
    return f"{notice(name, text.rstrip())} Do you wish to proceed?"


def notice(name: str, text: str) -> str:
    """Something went wrong or needs his attention."""
    if name != "raphael" or not text or text.startswith(_LABELS):
        return text
    return f"Notice. {text}"
