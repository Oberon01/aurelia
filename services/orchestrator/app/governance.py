"""Governance: the shared charter plus one policy per function.

The orchestrator enforces the generic rules before any tool runs:
  - the function is enabled
  - the tool's tier comes from POLICY, never from the function's own registration
  - tier "never" is blocked; tiers requiring confirmation need confirmed=true
  - per-function rate limits
  - the requesting user exists (per-user overrides may only narrow a policy)

Function-specific rules (Nebula's domain lists, Constellation's sharing rules)
are served to each function at GET /governance/functions/<name> and enforced
inside the function too — defence in depth.

Files are hot-reloaded. A file that fails to parse is ignored and the last good
version stays in force, so a typo can never loosen the rules.
"""
import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from watchfiles import awatch

log = logging.getLogger("aurelia.governance")

TIER_ORDER = ["reference", "read", "act", "change", "never"]


class TierRule(BaseModel):
    requires_confirmation: bool = False
    blocked: bool = False


class Enforced(BaseModel):
    default_tier: str = "change"
    tiers: dict[str, TierRule] = {
        "reference": TierRule(), "read": TierRule(), "act": TierRule(),
        "change": TierRule(requires_confirmation=True), "never": TierRule(blocked=True),
    }
    rate_limit_per_minute: int = 60
    audit: bool = True


class UserEntry(BaseModel):
    display_name: str = ""
    role: str = "member"
    # Per-user narrowing: tools this user may not run, e.g. {"nebula": ["purchase"]}
    deny: dict[str, list[str]] = {}


class Charter(BaseModel):
    version: int = 1
    enforced: Enforced = Enforced()
    principles: list[str] = []
    users: dict[str, UserEntry] = {"owner": UserEntry(display_name="Owner", role="owner")}


class ToolPolicy(BaseModel):
    tier: str


class FunctionPolicy(BaseModel):
    function: str
    enabled: bool = True
    tools: dict[str, ToolPolicy] = {}
    rate_limit_per_minute: int | None = None
    rules: dict[str, Any] = {}


class Decision(BaseModel):
    allowed: bool
    tier: str
    needs_confirmation: bool = False
    reason: str = ""


class GovernanceStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.charter = Charter()
        self.functions: dict[str, FunctionPolicy] = {}
        self.load_all()

    # ---- loading ---------------------------------------------------------
    def load_all(self) -> None:
        self._load_charter()
        for path in sorted((self.root / "functions").glob("*.yaml")):
            self._load_function(path)

    def _load_charter(self) -> None:
        path = self.root / "charter.yaml"
        try:
            self.charter = Charter.model_validate(yaml.safe_load(path.read_text()) or {})
            log.info("charter v%s loaded", self.charter.version)
        except FileNotFoundError:
            log.warning("no charter at %s; using strict defaults", path)
        except Exception:
            log.exception("charter invalid; keeping last good version")

    def _load_function(self, path: Path) -> None:
        try:
            policy = FunctionPolicy.model_validate(yaml.safe_load(path.read_text()) or {})
            self.functions[policy.function] = policy
            log.info("policy loaded for %s", policy.function)
        except Exception:
            log.exception("policy %s invalid; keeping last good version", path.name)

    async def watch(self) -> None:
        if not self.root.exists():
            return
        async for changes in awatch(self.root):
            for _, p in changes:
                path = Path(p)
                if path.name == "charter.yaml":
                    self._load_charter()
                elif path.suffix == ".yaml" and path.parent.name == "functions":
                    if path.exists():
                        self._load_function(path)
                    else:  # policy deleted → the function is no longer allowed to run
                        self.functions.pop(path.stem, None)
                        log.warning("policy for %s removed; function disabled", path.stem)

    def start(self) -> asyncio.Task:
        return asyncio.create_task(self.watch())

    # ---- decisions -------------------------------------------------------
    def decide(self, function: str, tool: str, user: str, confirmed: bool) -> Decision:
        enforced = self.charter.enforced
        if user not in self.charter.users:
            return Decision(allowed=False, tier="never", reason=f"unknown user '{user}'")
        policy = self.functions.get(function)
        if policy is None:
            return Decision(allowed=False, tier="never", reason=f"no policy for function '{function}'")
        if not policy.enabled:
            return Decision(allowed=False, tier="never", reason=f"function '{function}' is disabled")
        if tool in self.charter.users[user].deny.get(function, []):
            return Decision(allowed=False, tier="never", reason=f"'{function}.{tool}' is denied for {user}")

        tier = policy.tools[tool].tier if tool in policy.tools else enforced.default_tier
        rule = enforced.tiers.get(tier) or enforced.tiers.get("change") or TierRule(requires_confirmation=True)
        if rule.blocked:
            return Decision(allowed=False, tier=tier, reason=f"'{function}.{tool}' is in tier '{tier}'")
        if rule.requires_confirmation and not confirmed:
            return Decision(allowed=False, tier=tier, needs_confirmation=True,
                            reason=f"'{function}.{tool}' is a '{tier}' action and needs your confirmation")
        return Decision(allowed=True, tier=tier)

    def rate_limit(self, function: str) -> int:
        policy = self.functions.get(function)
        if policy and policy.rate_limit_per_minute:
            return policy.rate_limit_per_minute
        return self.charter.enforced.rate_limit_per_minute

    def for_function(self, function: str) -> dict[str, Any] | None:
        """What a function fetches: the principles plus its own policy."""
        policy = self.functions.get(function)
        if policy is None:
            return None
        return {"charter_version": self.charter.version, "principles": self.charter.principles,
                "policy": policy.model_dump()}
