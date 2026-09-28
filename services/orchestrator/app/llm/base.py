"""Provider interface: one neutral message format, several backends.

The orchestrator only ever calls `complete(messages, tools)`. Which model
answers is decided by config (llm.routes), so moving work between Claude and
a local model is a config edit, never a code change.

Neutral format
  Message(role="user"|"assistant"|"tool", text, tool_calls, tool_call_id)
  ToolDef(name, description, input_schema)
  Completion(text, tool_calls, usage, model, provider)
"""
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Message:
    role: str                                   # "user" | "assistant" | "tool"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)   # assistant only
    tool_call_id: str | None = None             # tool only
    tool_name: str | None = None                # tool only
    is_error: bool = False                      # tool only


@dataclass
class ToolDef:
    name: str                                   # API-safe name, e.g. "probe__echo"
    description: str
    input_schema: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    provider: str


class ProviderError(Exception):
    """Raised when a backend is unreachable or rejects the call; the router tries the next one."""


class Provider(Protocol):
    name: str

    async def complete(self, system: str, messages: list[Message], tools: list[ToolDef]) -> Completion: ...
