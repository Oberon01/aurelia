"""Scripted backend — FOR TESTS ONLY.

Replays completions from a JSON file named by AURELIA_SCRIPTED_PROVIDER, so the
full ask → tool → result loop can be exercised without an API key or a GPU.
Each entry: {"text": "...", "tool_calls": [{"name": "probe__echo", "args": {...}}]}.
The script cycles when it runs out.
"""
import json
import os
import uuid

from .base import Completion, Message, ProviderError, ToolCall, ToolDef, Usage


class ScriptedProvider:
    name = "scripted"

    def __init__(self):
        path = os.environ.get("AURELIA_SCRIPTED_PROVIDER")
        if not path:
            raise ProviderError("scripted provider needs AURELIA_SCRIPTED_PROVIDER")
        with open(path) as f:
            self._script = json.load(f)
        self._i = 0

    async def complete(self, system: str, messages: list[Message], tools: list[ToolDef]) -> Completion:
        step = self._script[self._i % len(self._script)]
        self._i += 1
        calls = [ToolCall(id=f"toolu_{uuid.uuid4().hex[:12]}", name=c["name"], args=c.get("args", {}))
                 for c in step.get("tool_calls", [])]
        return Completion(text=step.get("text", ""), tool_calls=calls,
                          usage=Usage(input_tokens=100, output_tokens=20), model="scripted", provider="scripted")
