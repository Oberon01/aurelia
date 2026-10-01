"""Picks the backend for each call from hot-reloaded config.

llm.routes maps a purpose to an ordered list of providers; the first one that
answers wins. Example: chat: [claude, ollama] → Claude, falling back to the
local model if Claude is unreachable or has no key.
"""
import logging

from .base import Completion, Message, ProviderError, ToolDef

log = logging.getLogger("aurelia.llm")


def build(name: str, cfg: dict):
    if name == "claude" or name.startswith("claude_"):         # e.g. claude_reason: another Claude model
        from .claude import ClaudeProvider
        return ClaudeProvider(model=cfg.get("model", "claude-haiku-4-5-20251001"),
                              max_tokens=int(cfg.get("max_tokens", 1024)))
    if name == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider(base_url=cfg.get("base_url", "http://host.docker.internal:11434"),
                              model=cfg.get("model", "qwen2.5:7b-instruct"),
                              timeout=float(cfg.get("timeout_seconds", 60)))
    if name == "scripted":
        from .scripted import ScriptedProvider
        return ScriptedProvider()
    raise ProviderError(f"unknown provider '{name}'")


class Router:
    def __init__(self, config_store):
        self._config = config_store
        self._cache: dict[str, tuple[str, object]] = {}   # name → (config fingerprint, provider)

    def _provider(self, name: str):
        cfg = self._config.current.llm.providers.get(name, {})
        fingerprint = repr(sorted(cfg.items()))
        cached = self._cache.get(name)
        if cached is None or cached[0] != fingerprint:   # rebuild when its config changes
            self._cache[name] = (fingerprint, build(name, cfg))
        return self._cache[name][1]

    async def complete(self, purpose: str, system: str, messages: list[Message],
                       tools: list[ToolDef]) -> Completion:
        llm = self._config.current.llm
        order = llm.routes.get(purpose) or [llm.default_provider]
        errors = []
        for name in order:
            try:
                return await self._provider(name).complete(system, messages, tools)
            except ProviderError as exc:
                log.warning("provider %s failed: %s", name, exc)
                errors.append(str(exc))
        raise ProviderError("; ".join(errors) or "no providers configured")
