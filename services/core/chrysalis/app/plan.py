"""Turn a list of changed files into a release plan.

  config/**                     → validated, then hot-reloaded (no rebuild)
  services/orchestrator/**      → rebuild + swap orchestrator; run migrations first
  services/functions/<name>/**  → rebuild + swap that function
  docker-compose.yml            → apply the whole stack (brief restart of changed infra)
  services/core/**, deploy.sh   → protected core: never applied automatically
  anything else (docs, scripts) → no action
"""
from dataclasses import dataclass, field

PROTECTED_PREFIXES = ("services/core/",)
PROTECTED_FILES = ("scripts/deploy.sh",)
# Never touched by an automatic stack-wide apply.
NEVER_RECREATE = {"chrysalis"}


@dataclass
class Plan:
    services: list[str] = field(default_factory=list)   # rebuild + zero-downtime swap, in this order
    migrate: bool = False
    compose: bool = False                               # whole-stack apply needed
    config: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_deploy(self) -> bool:
        return bool(self.services or self.compose)

    def summary(self) -> str:
        parts = []
        if self.services:
            parts.append("rebuild " + ", ".join(self.services))
        if self.migrate:
            parts.append("migrate database")
        if self.compose:
            parts.append("apply docker-compose.yml")
        if self.config:
            parts.append(f"{len(self.config)} config file(s) hot-reload")
        if self.protected:
            parts.append("protected core changed: apply manually")
        return "; ".join(parts) or "no action needed"

    def as_dict(self) -> dict:
        return {"services": self.services, "migrate": self.migrate, "compose": self.compose,
                "config": self.config, "protected": self.protected, "ignored": self.ignored,
                "notes": self.notes, "summary": self.summary()}


def classify(paths: list[str], known_services: set[str]) -> Plan:
    plan = Plan()
    services: set[str] = set()
    for p in sorted(set(paths)):
        if p.startswith(PROTECTED_PREFIXES) or p in PROTECTED_FILES:
            plan.protected.append(p)
        elif p.startswith("services/orchestrator/"):
            services.add("orchestrator")
        elif p.startswith("services/functions/"):
            parts = p.split("/")
            if len(parts) > 2:
                services.add(parts[2])
        elif p == "docker-compose.yml":
            plan.compose = True
        elif p.startswith("config/"):
            plan.config.append(p)
        else:
            plan.ignored.append(p)

    for name in sorted(services):
        if name not in known_services and not plan.compose:
            plan.notes.append(f"'{name}' has code but no service in docker-compose.yml yet; skipped")
            continue
        plan.services.append(name)
    # Orchestrator first: functions re-register against the newest version.
    plan.services.sort(key=lambda s: (s != "orchestrator", s))
    plan.migrate = "orchestrator" in plan.services
    if plan.protected:
        plan.notes.append("Protected core changed (Chrysalis or deploy.sh). Apply on the server with: "
                          "docker compose --profile chrysalis up -d --build chrysalis")
    return plan
