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
    action_panel_seconds: float = 12         # finished actions (lock, lights...) fade after this; look-ups stay
    reply_panel_sentences: int = 3           # a reply this long (or longer than reply_panel_chars) opens as a panel
    reply_panel_chars: int = 320


class AmbientConfig(BaseModel):
    """What a presentation display shows when nothing's on it: a dim clock, the date, the weather
    and anything worth a glance. Dark during quiet hours."""
    enabled: bool = True
    dim: float = 0.55                         # brightness of the ambient screen, 0–1
    quiet_hours: str = "23:00-07:00"          # dark then (HH:MM-HH:MM, local time; "" for never)
    latitude: float | None = None             # set both for the weather (Open-Meteo, no key needed)
    longitude: float | None = None
    units: str = "fahrenheit"                 # or "celsius"


class RetentionConfig(BaseModel):
    hot_days: int = 7
    warm_days: int = 90
    critical_hot_days: int = 30              # tools you approved as critical keep full panels this long


class EmbeddingConfig(BaseModel):
    enabled: bool = True
    provider: str = "ollama"                 # the only one for now; runs locally, free
    model: str = "nomic-embed-text"
    base_url: str | None = None              # None = llm.providers.ollama.base_url
    dimensions: int = 768                    # must match the vector(768) column
    timeout_seconds: float = 20


class DigestConfig(BaseModel):
    enabled: bool = True
    hour: int = 6                            # shown on your first interaction after this hour
    timezone: str = "America/Chicago"


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
    # Requests that need real reasoning use routes.reason (a stronger model) when it's set.
    reason_when: list[str] = [
        r"\bwhy\b", r"\bexplain", r"\bimprov", r"\brecommend", r"\bsuggest", r"\bshould (i|we|you)\b",
        r"\bcompare|\bversus\b|\bvs\.?\s", r"\bplan\b|\bplanning\b", r"\bdesign\b", r"\banaly[sz]",
        r"\btrade-?offs?\b|\bpros and cons\b", r"\bwhat do you think\b", r"\bwhat'?s wrong\b",
        r"\bhow (could|would|can|should) (you|we|i)\b", r"\breason\b", r"\bwhat did (the|your) (last )?(release|update)"]


class SessionConfig(BaseModel):
    ttl_minutes: int = 1440     # a day; Mnemosyne keeps what matters beyond that
    max_turns: int = 30


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
    ambient: AmbientConfig = AmbientConfig()
    retention: RetentionConfig = RetentionConfig()
    memory: MemoryConfig = MemoryConfig()
    digest: DigestConfig = DigestConfig()
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
