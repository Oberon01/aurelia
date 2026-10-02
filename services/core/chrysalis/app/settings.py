from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    repo_path: str                                   # the host path of the repo, mounted at the same path
    database_url: str
    orchestrator_url: str = "http://orchestrator:8000"
    function_url: str = "http://chrysalis:9100"
    project: str = "aurelia"                         # compose project name (image prefix)
    heartbeat_seconds: int = 10


class VerifyConfig(BaseModel):
    ready_timeout_seconds: float = 60
    functions_timeout_seconds: float = 45
    smoke_test: bool = True
    watch_seconds: float = 120
    # Also load Aurelia the way his devices do, through the proxy (Traefik): a new orchestrator can
    # be healthy inside its container yet unreachable from outside (e.g. Traefik picked an address on
    # a network it isn't on), and that's a failed release. "" turns the check off.
    proxy_url: str = "http://proxy:80"
    proxy_timeout_seconds: float = 45


class ChrysalisConfig(BaseModel):
    enabled: bool = True
    dry_run: bool = True
    branch: str = "main"
    poll_seconds: int = 120
    hold_authors: list[str] = []
    verify: VerifyConfig = VerifyConfig()
    keep_releases: int = 5


def load_config(repo_path: str) -> ChrysalisConfig:
    """Re-read every cycle. A broken file falls back to the safest config: dry run."""
    path = Path(repo_path) / "config" / "chrysalis.yaml"
    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
        return ChrysalisConfig.model_validate(raw)
    except Exception:
        return ChrysalisConfig(dry_run=True)
