"""Hot-reloaded runtime config.

The YAML file is watched; on change it is re-parsed and swapped in atomically.
A file that fails to parse is logged and ignored, so a typo never takes Aurelia down.
"""
import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from watchfiles import awatch

log = logging.getLogger("aurelia.config")


class DisplayConfig(BaseModel):
    live_mode_threshold_seconds: float = 30


class RetentionConfig(BaseModel):
    hot_days: int = 7
    warm_days: int = 90


class EmbeddingConfig(BaseModel):
    enabled: bool = True
    provider: str = "ollama"                 # the only one for now; runs locally, free
    model: str = "nomic-embed-text"
    base_url: str | None = None              # None = llm.providers.ollama.base_url
    dimensions: int = 768                    # must match the vector(768) column
    timeout_seconds: float = 20


class MemoryConfig(BaseModel):
    restore: bool = True                     # repeated look-ups show their last panel at once
    maintenance_minutes: int = 60            # tiering, rollups and embedding backfill
    embeddings: EmbeddingConfig = EmbeddingConfig()


class LLMConfig(BaseModel):
    default_provider: str = "claude"
    routes: dict[str, list[str]] = {"chat": ["claude"]}
    providers: dict[str, dict[str, Any]] = {}
    monthly_token_cap: int = 2_000_000       # input + output tokens across cloud providers
    max_tool_rounds: int = 5                 # tool calls the LLM may chain for one request
    wait_for_result_seconds: float = 8       # wait this long for a task result before replying


class SessionConfig(BaseModel):
    ttl_minutes: int = 60
    max_turns: int = 20


class FastPathPattern(BaseModel):
    name: str
    match: str                  # regex, matched case-insensitively against the whole request
    tool: str                   # "<function>.<tool>"
    args: dict[str, str] = {}   # values may use {group} or {group|default}
    reply: str = "Done."        # templated confirmation, same placeholders


class FastPathConfig(BaseModel):
    enabled: bool = True
    patterns: list[FastPathPattern] = []


class RegistryConfig(BaseModel):
    function_ttl_seconds: int = 30


class RuntimeConfig(BaseModel):
    display: DisplayConfig = DisplayConfig()
    retention: RetentionConfig = RetentionConfig()
    memory: MemoryConfig = MemoryConfig()
    llm: LLMConfig = LLMConfig()
    session: SessionConfig = SessionConfig()
    fastpath: FastPathConfig = FastPathConfig()
    registry: RegistryConfig = RegistryConfig()


class ConfigStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.current = RuntimeConfig()
        self.load()

    def load(self) -> bool:
        try:
            raw = yaml.safe_load(self.path.read_text()) or {}
            self.current = RuntimeConfig.model_validate(raw)
            log.info("config loaded from %s", self.path)
            return True
        except FileNotFoundError:
            log.warning("config %s not found; using defaults", self.path)
        except Exception:
            log.exception("config %s invalid; keeping previous config", self.path)
        return False

    async def watch(self) -> None:
        if not self.path.parent.exists():
            return
        # Watch the directory: editors often replace the file rather than write in place.
        async for changes in awatch(self.path.parent):
            if any(Path(p).name == self.path.name for _, p in changes):
                self.load()

    def start(self) -> asyncio.Task:
        return asyncio.create_task(self.watch())
