"""Claude backend (Anthropic Messages API).

System prompt and tool definitions are marked for prompt caching: they are
resent on every call, so caching them is most of the cost saving.
"""
import os
from typing import Any

import anthropic

from .base import Completion, Message, ProviderError, ToolCall, ToolDef, Usage


def to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            for c in m.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
            out.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text}
            if m.is_error:
                block["is_error"] = True
            # Consecutive tool results belong in one user turn.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def _cache_tail(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation so far is cached too (its last block marks the end): the next step of the same request,
    and the next turn of the conversation, read it from the cache instead of paying for it again."""
    if not msgs:
        return msgs
    last = msgs[-1]
    content = last["content"]
    if isinstance(content, str):
        if not content:
            return msgs
        content = [{"type": "text", "text": content}]
    else:
        content = [dict(b) for b in content]
    if content:
        content[-1]["cache_control"] = {"type": "ephemeral"}
    return [*msgs[:-1], {**last, "content": content}]


def tools_to_anthropic(tools: list[ToolDef]) -> list[dict[str, Any]]:
    defs = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
    if defs:
        defs[-1]["cache_control"] = {"type": "ephemeral"}
    return defs


def from_anthropic(resp: Any, provider: str = "claude") -> Completion:
    text_parts, calls = [], []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input or {})))
    u = resp.usage
    usage = Usage(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                  cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                  cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
    return Completion(text="".join(text_parts).strip(), tool_calls=calls, usage=usage,
                      model=resp.model, provider=provider)


class ClaudeProvider:
    name = "claude"

    def __init__(self, model: str, max_tokens: int = 1024):
        self.model, self.max_tokens = model, max_tokens
        key = os.environ.get("ANTHROPIC_API_KEY")
        self._client = anthropic.AsyncAnthropic(api_key=key) if key else None

    async def complete(self, system: str, messages: list[Message], tools: list[ToolDef]) -> Completion:
        if self._client is None:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": _cache_tail(to_anthropic(messages)),
        }
        if tools:
            kwargs["tools"] = tools_to_anthropic(tools)
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise ProviderError(f"claude: {exc}") from exc
        return from_anthropic(resp)
