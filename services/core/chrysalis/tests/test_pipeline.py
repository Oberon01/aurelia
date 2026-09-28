"""Pipeline decisions end to end: real git repositories, fake Docker/verifier/store."""
import asyncio
import subprocess
from pathlib import Path

import pytest

from app.docker import DockerError
from app.git import Git
from app.pipeline import Chrysalis
from app.settings import ChrysalisConfig, Settings, VerifyConfig
from app.verify import VerifyError


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeDocker:
    def __init__(self):
        self.calls: list[tuple] = []
        self.images: set[tuple[str, str]] = set()
        self.fail_test = False
        self.test_stages = {"services/orchestrator", "services/functions/probe"}
        self.unhealthy: set[str] = set()
        self.no_access: str | None = None

    async def access(self): return self.no_access

    async def compose_services(self):
        return {"proxy", "postgres", "redis", "migrate", "orchestrator", "probe", "chrysalis"}

    async def has_test_stage(self, d): return d in self.test_stages

    async def test(self, d):
        self.calls.append(("test", d))
        if self.fail_test:
            raise DockerError("tests failed")

    async def build(self, services):
        self.calls.append(("build", tuple(services)))
        for s in services:
            self.images.add((s, "latest"))

    async def tag(self, svc, src, dst, check=True):
        self.calls.append(("tag", svc, src[:8], dst[:8]))
        if (svc, src) in self.images:
            self.images.add((svc, dst))
            return True
        return False

    async def has_image(self, svc, tag): return (svc, tag) in self.images
    async def migrate(self): self.calls.append(("migrate",))
    async def swap(self, svc): self.calls.append(("swap", svc))
    async def apply_stack(self, services): self.calls.append(("apply_stack", tuple(services)))
    async def health(self, svc): return ["unhealthy"] if svc in self.unhealthy else ["healthy"]
    async def tags(self, svc): return [t for s, t in self.images if s == svc]
    async def remove_tag(self, svc, tag): self.images.discard((svc, tag))


class FakeVerifier:
    def __init__(self): self.fail = False
    async def functions(self): return {"probe"}
    async def run(self, cfg, expected):
        if self.fail:
            raise VerifyError("smoke test task ended as failed")
        return ["orchestrator ready"]


class MemoryStore:
    def __init__(self): self.rows: list[dict] = []

    async def create(self, sha, prev, author, message, paths, plan, status):
        self.rows.append({"id": len(self.rows) + 1, "sha": sha, "prev_sha": prev, "author": author,
                          "message": message, "services": plan.get("services", []), "status": status,
                          "detail": None, "plan": plan})
        return len(self.rows)

    async def update(self, rid, status, detail=None, finished=False):
        self.rows[rid - 1]["status"] = status
        if detail:
            self.rows[rid - 1]["detail"] = detail

    async def decided(self, sha, dry_run):
        skip = {"failed", "rolled_back", "held"} | ({"dry_run"} if dry_run else set())
        hits = [r for r in self.rows if r["sha"] == sha and r["status"] in skip]
        return hits[-1]["status"] if hits else None

    async def approved(self, sha): return any(r["sha"] == sha and r["status"] == "approved" for r in self.rows)
    async def live(self, n): return [r for r in reversed(self.rows) if r["status"] == "live"][:n]
    async def get(self, rid): return self.rows[rid - 1] if 0 < rid <= len(self.rows) else None
    async def recent(self, n=10): return list(reversed(self.rows))[:n]


@pytest.fixture
def world(tmp_path: Path):
    remote, server, dev = tmp_path / "remote.git", tmp_path / "server", tmp_path / "dev"
    sh(tmp_path, "git", "init", "--bare", "-q", "-b", "main", str(remote))
    sh(tmp_path, "git", "clone", "-q", str(remote), str(dev))
    for repo in (dev,):
        sh(repo, "git", "config", "user.email", "kentrell@example.com")
        sh(repo, "git", "config", "user.name", "Kentrell")
    (dev / "config").mkdir()
    (dev / "config" / "aurelia.yaml").write_text("llm: {}\n")
    (dev / "config" / "chrysalis.yaml").write_text("dry_run: false\n")
    (dev / "services" / "functions" / "probe").mkdir(parents=True)
    (dev / "services" / "functions" / "probe" / "main.py").write_text("v1\n")
    sh(dev, "git", "add", "-A"); sh(dev, "git", "commit", "-qm", "initial"); sh(dev, "git", "push", "-q", "origin", "main")
    sh(tmp_path, "git", "clone", "-q", str(remote), str(server))

    docker, verifier, store = FakeDocker(), FakeVerifier(), MemoryStore()
    settings = Settings(repo_path=str(server), database_url="unused")
    c = Chrysalis(settings, Git(str(server)), docker, verifier, store)
    docker.images |= {("orchestrator", "latest"), ("probe", "latest"), ("migrate", "latest")}
    asyncio.run(c.adopt_baseline())

    def commit(path, text, message, author="kentrell@example.com"):
        f = dev / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        sh(dev, "git", "add", "-A")
        sh(dev, "git", "-c", f"user.email={author}", "commit", "-qm", message)
        sh(dev, "git", "push", "-q", "origin", "main")
        return sh(dev, "git", "rev-parse", "HEAD")

    return {"c": c, "docker": docker, "verifier": verifier, "store": store, "server": server, "commit": commit}


CFG = ChrysalisConfig(dry_run=False, verify=VerifyConfig(watch_seconds=0.2))


def run(world, cfg=CFG):
    return asyncio.run(world["c"].cycle(cfg))


def server_head(world):
    return sh(world["server"], "git", "rev-parse", "HEAD")


def test_baseline_and_up_to_date(world):
    assert world["store"].rows[0]["status"] == "live"
    assert ("probe", server_head(world)) in world["docker"].images
    assert run(world) == "up to date"


def test_dry_run_changes_nothing_and_records_once(world):
    before = server_head(world)
    world["commit"]("services/functions/probe/main.py", "v2\n", "probe v2")
    dry = ChrysalisConfig(dry_run=True)
    assert run(world, dry).startswith("dry_run")
    assert server_head(world) == before and not [c for c in world["docker"].calls if c[0] == "swap"]
    assert "already decided" in run(world, dry)
    # Turning dry run off deploys the same commit.
    assert run(world).startswith("live")


def test_config_only_is_live_without_rebuild(world):
    sha = world["commit"]("config/aurelia.yaml", "llm: {max_tool_rounds: 3}\n", "tune config")
    assert run(world).startswith("live")
    assert server_head(world) == sha
    assert not [c for c in world["docker"].calls if c[0] in ("build", "swap")]


def test_function_change_tests_builds_tags_and_swaps(world):
    sha = world["commit"]("services/functions/probe/main.py", "v2\n", "probe v2")
    assert run(world).startswith("live")
    kinds = [c[0] for c in world["docker"].calls]
    assert kinds.index("test") < kinds.index("build") < kinds.index("swap")
    assert ("probe", sha) in world["docker"].images and ("swap", "probe") in world["docker"].calls


def test_failing_tests_leave_everything_untouched(world):
    before = server_head(world)
    world["docker"].fail_test = True
    world["commit"]("services/functions/probe/main.py", "broken\n", "broken probe")
    assert run(world).startswith("failed")
    assert server_head(world) == before
    assert not [c for c in world["docker"].calls if c[0] == "swap"]
    assert "already decided" in run(world)          # the bad commit is not retried


def test_failed_verification_rolls_back_images_and_code(world):
    before = server_head(world)
    world["verifier"].fail = True
    world["commit"]("services/functions/probe/main.py", "v2\n", "probe v2")
    result = run(world)
    assert result.startswith("rolled_back") and "Rolled back probe" in result
    assert server_head(world) == before
    swaps = [c for c in world["docker"].calls if c[0] == "swap"]
    assert len(swaps) == 2                           # new version, then the previous one
    assert ("tag", "probe", before[:8], "latest") in world["docker"].calls


def test_crash_loop_after_going_live_rolls_back(world):
    world["docker"].unhealthy.add("probe")
    world["commit"]("services/functions/probe/main.py", "v2\n", "probe v2")
    assert run(world).startswith("rolled_back")


def test_held_author_waits_for_approval(world):
    cfg = ChrysalisConfig(dry_run=False, hold_authors=["aurelia-bot@example.com"],
                          verify=VerifyConfig(watch_seconds=0.2))
    world["commit"]("services/functions/probe/main.py", "v2\n", "suggested by Aurelia",
                    author="aurelia-bot@example.com")
    assert run(world, cfg).startswith("held")
    assert "already decided" in run(world, cfg)
    held = [r for r in world["store"].rows if r["status"] == "held"][0]
    asyncio.run(world["c"].approve(held["id"]))
    assert run(world, cfg).startswith("live")


def test_invalid_yaml_is_rejected_before_anything_changes(world):
    before = server_head(world)
    world["commit"]("config/aurelia.yaml", "llm: [broken\n", "oops")
    result = run(world)
    assert result.startswith("failed") and "Invalid YAML" in result
    assert server_head(world) == before


def test_local_edits_on_server_hold_the_release(world):
    (world["server"] / "config" / "aurelia.yaml").write_text("edited on server\n")
    world["commit"]("services/functions/probe/main.py", "v2\n", "probe v2")
    result = run(world)
    assert "local edits" in result and "config/aurelia.yaml" in result


def test_permission_change_on_server_is_a_local_edit(world):
    import os
    probe = world["server"] / "services" / "functions" / "probe" / "main.py"
    os.chmod(probe, 0o755)
    world["commit"]("README.md", "docs\n", "docs")
    assert "services/functions/probe/main.py" in run(world)


def test_protected_core_goes_live_without_touching_chrysalis(world):
    world["commit"]("services/core/chrysalis/app/x.py", "change\n", "chrysalis change")
    result = run(world)
    assert result.startswith("live") and not [c for c in world["docker"].calls if c[0] in ("build", "swap")]
    assert "protected core" in world["store"].rows[-1]["detail"].lower()


def test_rollback_on_request_restores_previous_release(world):
    first = server_head(world)
    world["commit"]("services/functions/probe/main.py", "v2\n", "probe v2")
    assert run(world).startswith("live")
    message = asyncio.run(world["c"].rollback())
    assert message.startswith(f"Rolled back to {first[:8]}")
    assert server_head(world) == first
    assert world["store"].rows[-1]["status"] == "rolled_back"
    # Only the changed service is swapped back.
    last_swaps = [c for c in world["docker"].calls if c[0] == "swap"][-1:]
    assert last_swaps == [("swap", "probe")]


def test_rollback_of_orchestrator_retags_migrate_without_swapping_it(world):
    world["commit"]("services/orchestrator/app/x.py", "v2\n", "orchestrator v2")
    assert run(world).startswith("live")
    asyncio.run(world["c"].rollback())
    swapped = {c[1] for c in world["docker"].calls if c[0] == "swap"}
    assert "migrate" not in swapped and "orchestrator" in swapped
    assert any(c[:3] == ("tag", "migrate", c[2]) and c[3] == "latest" for c in world["docker"].calls if c[0] == "tag")


def test_no_docker_access_records_nothing_and_retries_later(world):
    world["docker"].no_access = "No permission to use the Docker socket."
    world["commit"]("services/functions/probe/main.py", "v2\n", "probe v2")
    with pytest.raises(RuntimeError, match="Docker socket"):
        run(world)
    assert [r["status"] for r in world["store"].rows] == ["live"]     # only the baseline
    world["docker"].no_access = None
    assert run(world).startswith("live")                              # same commit, now deployed


def test_baseline_images_are_saved_once_docker_works(world):
    sha = world["store"].rows[0]["sha"]
    world["docker"].images = {("probe", "latest"), ("orchestrator", "latest")}   # tags lost
    asyncio.run(world["c"].ensure_baseline())
    assert ("probe", sha) in world["docker"].images and ("orchestrator", sha) in world["docker"].images
    assert len([r for r in world["store"].rows if r["status"] == "live"]) == 1   # no duplicate baseline
