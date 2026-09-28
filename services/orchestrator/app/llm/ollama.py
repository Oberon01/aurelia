"""Local model backend (Ollama chat API with tool calling)."""
import uuid
from typing import Any

import httpx

from .base import Completion, Message, ProviderError, ToolCall, ToolDef, Usage


def to_ollama(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text}
            if m.tool_calls:
                msg["tool_calls"] = [{"function": {"name": c.name, "arguments": c.args}} for c in m.tool_calls]
            out.append(msg)
        elif m.role == "tool":
            out.append({"role": "tool", "content": m.text, "tool_name": m.tool_name or ""})
    return out


def tools_to_ollama(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.input_schema}} for t in tools]


def from_ollama(data: dict[str, Any], model: str) -> Completion:
    msg = data.get("message", {})
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function", {})
        args = fn.get("arguments") or {}
        calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=fn.get("name", ""),
                              args=args if isinstance(args, dict) else {}))
    usage = Usage(input_tokens=data.get("prompt_eval_count", 0) or 0,
                  output_tokens=data.get("eval_count", 0) or 0)
    return Completion(text=(msg.get("content") or "").strip(), tool_calls=calls, usage=usage,
                      model=model, provider="ollama")


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float = 60):
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout

    async def complete(self, system: str, messages: list[Message], tools: list[ToolDef]) -> Completion:
        body: dict[str, Any] = {"model": self.model, "messages": to_ollama(system, messages), "stream": False}
        if tools:
            body["tools"] = tools_to_ollama(tools)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.base_url}/api/chat", json=body)
                r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama: {exc}") from exc
        return from_ollama(r.json(), self.model)
