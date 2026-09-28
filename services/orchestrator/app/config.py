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


class LLMConfig(BaseModel):
    default_provider: str = "claude"
    routes: dict[str, Any] = {}


class RegistryConfig(BaseModel):
    function_ttl_seconds: int = 30


class RuntimeConfig(BaseModel):
    display: DisplayConfig = DisplayConfig()
    retention: RetentionConfig = RetentionConfig()
    llm: LLMConfig = LLMConfig()
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
