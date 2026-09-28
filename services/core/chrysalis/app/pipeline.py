"""The release pipeline.

   new commit on main
     → classify changed files → (held? invalid config? dirty server? dry run?)
     → fast-forward working copy (config hot-reloads here)
     → run each changed service's test stage
     → build images, tag them with the commit
     → migrate (if the orchestrator changed) → zero-downtime swap, orchestrator first
     → verify (ready, functions re-registered, smoke test) → watch for crash loops
     → live, or roll back: previous images + previous commit
"""
import asyncio
import logging
from datetime import datetime, timezone

import yaml

from .docker import Docker, DockerError
from .git import Git, GitError
from .plan import NEVER_RECREATE, Plan, classify
from .settings import ChrysalisConfig, Settings, load_config
from .store import Store
from .verify import Verifier, VerifyError

log = logging.getLogger("chrysalis")

# Services built from this repo whose images are versioned per release.
def built_services(known: set[str]) -> list[str]:
    infra = {"proxy", "postgres", "redis"}
    return sorted(s for s in known if s not in infra and s not in NEVER_RECREATE)


def service_dir(service: str) -> str:
    if service in ("orchestrator", "migrate"):
        return "services/orchestrator"
    return f"services/functions/{service}"


class Chrysalis:
    def __init__(self, settings: Settings, git: Git, docker: Docker, verifier: Verifier, store: Store):
        self.settings, self.git, self.docker, self.verifier, self.store = settings, git, docker, verifier, store
        self.lock = asyncio.Lock()          # one release or rollback at a time
        self.state = {"last_check": None, "last_error": None, "busy": None, "docker": None}

    def config(self) -> ChrysalisConfig:
        return load_config(self.settings.repo_path)

    # ---- startup ---------------------------------------------------------------

    async def ensure_baseline(self) -> None:
        """Record what's running as the first good release (first run only), and make
        sure the current live release has images saved to roll back to."""
        problem = await self.docker.access()
        self.state["docker"] = problem or "ok"
        if problem:
            log.error("Docker access: %s", problem)
            return
        live = await self.store.live(1)
        known = await self.docker.compose_services()
        if not live:
            sha = await self.git.head()
            rid = await self.store.create(sha, None, "", "baseline adopted by Chrysalis", [],
                                          {"services": [], "summary": "baseline"}, "live")
            await self.store.update(rid, "live", "Baseline at startup.", True)
            log.info("baseline adopted at %s", sha[:8])
            live = await self.store.live(1)
        sha = live[0]["sha"]
        saved = []
        for svc in built_services(known):
            if not await self.docker.has_image(svc, sha) and await self.docker.has_image(svc, "latest"):
                await self.docker.tag(svc, "latest", sha)
            if await self.docker.has_image(svc, sha):
                saved.append(svc)
        log.info("live release %s: rollback images saved for %s", sha[:8], ", ".join(saved) or "nothing")

    # Kept for callers of the old name.
    adopt_baseline = ensure_baseline

    # ---- the watch loop ----------------------------------------------------------

    async def loop(self) -> None:
        while True:
            cfg = self.config()
            try:
                if cfg.enabled:
                    await self.cycle(cfg)
                self.state["last_error"] = None
            except Exception as exc:     # never let the watcher die
                log.exception("cycle failed")
                self.state["last_error"] = str(exc)
            self.state["last_check"] = datetime.now(timezone.utc).isoformat()
            await asyncio.sleep(max(15, cfg.poll_seconds))

    async def cycle(self, cfg: ChrysalisConfig) -> str:
        """One check for new commits. Returns a short description of what happened."""
        async with self.lock:
            await self.git.fetch(cfg.branch)
            local, remote = await self.git.head(), await self.git.remote_head(cfg.branch)
            if local == remote:
                return "up to date"
            if await self.store.decided(remote, cfg.dry_run):
                return f"{remote[:8]} already decided"

            # An environment problem is not the commit's fault: don't record anything,
            # so the same commit is tried again once the problem is fixed.
            problem = await self.docker.access()
            self.state["docker"] = problem or "ok"
            if problem:
                raise RuntimeError(problem)

            commits = await self.git.commits(local, remote)
            newest = commits[0] if commits else None
            author = newest.author if newest else ""
            message = newest.message if newest else ""

            if not await self.git.is_ancestor(local, remote):
                return await self._record(remote, local, author, message, [], Plan(), "held",
                                          "History diverged (force-push?). Resolve on the server manually.")

            paths = await self.git.changed_paths(local, remote)
            plan = classify(paths, await self.docker.compose_services())
            approved = await self.store.approved(remote)

            held_by = sorted({c.author for c in commits if c.author in cfg.hold_authors})
            if held_by and not approved:
                return await self._record(remote, local, author, message, paths, plan, "held",
                                          f"Proposed by {', '.join(held_by)}: waiting for your approval.")

            bad = await self._invalid_config(remote, plan)
            if bad:
                return await self._record(remote, local, author, message, paths, plan, "failed", bad)

            dirty = await self.git.dirty()
            if dirty:
                return await self._record(remote, local, author, message, paths, plan, "held",
                                          "The server's copy has local edits: " + ", ".join(dirty[:5]))

            if cfg.dry_run:
                return await self._record(remote, local, author, message, paths, plan, "dry_run",
                                          "Dry run: nothing changed. Plan: " + plan.summary())

            rid = await self.store.create(remote, local, author, message, paths, plan.as_dict(), "planned")
            return await self._release(rid, local, remote, plan, cfg)

    async def _record(self, sha, prev, author, message, paths, plan: Plan, status, detail) -> str:
        rid = await self.store.create(sha, prev, author, message, paths, plan.as_dict(), status)
        await self.store.update(rid, status, detail, finished=True)
        log.info("%s %s: %s", status, sha[:8], detail)
        return f"{status}: {detail}"

    async def _invalid_config(self, sha: str, plan: Plan) -> str | None:
        for path in plan.config:
            if path.endswith((".yaml", ".yml")):
                try:
                    yaml.safe_load(await self.git.show(sha, path))
                except GitError:
                    continue            # file deleted in this commit
                except yaml.YAMLError as exc:
                    return f"Invalid YAML in {path}; nothing applied. {str(exc).splitlines()[0]}"
        return None

    # ---- a release -------------------------------------------------------------

    async def _release(self, rid: int, prev: str, sha: str, plan: Plan, cfg: ChrysalisConfig) -> str:
        self.state["busy"] = f"release {sha[:8]}"
        deployed: list[str] = []
        stage = "testing"
        try:
            expected = await self.verifier.functions()
            await self.git.fast_forward(sha)          # config hot-reloads from here

            await self.store.update(rid, "testing")
            for svc in plan.services:
                if await self.docker.has_test_stage(service_dir(svc)):
                    await self.docker.test(service_dir(svc))

            known = await self.docker.compose_services()
            targets = built_services(known) if plan.compose else plan.services + (["migrate"] if plan.migrate else [])
            if targets:
                # Save what's running now under the current commit before building over it,
                # so a rollback always has the exact previous images to return to.
                for svc in targets:
                    if not await self.docker.has_image(svc, prev) and await self.docker.has_image(svc, "latest"):
                        await self.docker.tag(svc, "latest", prev)
                await self.docker.build(targets)
                for svc in targets:
                    await self.docker.tag(svc, "latest", sha, check=False)

            stage = "deploying"
            await self.store.update(rid, "deploying")
            if plan.migrate or plan.compose:
                await self.docker.migrate()
            if plan.compose:
                stack = sorted(known - NEVER_RECREATE - {"migrate"})
                deployed = [s for s in built_services(known) if s != "migrate"]
                await self.docker.apply_stack(stack)
            else:
                for svc in plan.services:
                    deployed.append(svc)
                    await self.docker.swap(svc)

            if plan.needs_deploy:
                stage = "verifying"
                await self.store.update(rid, "verifying")
                passed = await self.verifier.run(cfg.verify, expected)
                await self._watch(deployed, cfg.verify.watch_seconds)
                passed.append(f"no crash loops for {cfg.verify.watch_seconds:.0f}s")
            else:
                passed = ["no services to rebuild"]

            await self.store.update(rid, "live", f"{plan.summary()}. Checks: {'; '.join(passed)}."
                                    + (" " + " ".join(plan.notes) if plan.notes else ""), finished=True)
            await self._prune(cfg.keep_releases)
            log.info("release %s live", sha[:8])
            return f"live: {plan.summary()}"
        except Exception as exc:   # DockerError, GitError, VerifyError or anything unexpected
            detail = f"Failed while {stage}: {exc}"
            if deployed:
                restored = await self._restore(deployed, prev)
                detail += f" Rolled back {', '.join(restored) or 'nothing (no previous images)'}."
                status = "rolled_back"
            else:
                status = "failed"
            try:
                await self.git.reset_to(prev)          # config back to the last good version
            except GitError as git_exc:
                detail += f" Could not reset the working copy: {git_exc}"
            await self.store.update(rid, status, detail, finished=True)
            log.error("release %s %s: %s", sha[:8], status, detail)
            return f"{status}: {detail}"
        finally:
            self.state["busy"] = None

    async def _watch(self, services: list[str], seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            for svc in services:
                states = await self.docker.health(svc)
                if not states or any(s in ("unhealthy", "exited") for s in states):
                    raise VerifyError(f"{svc} became unhealthy after deploying ({', '.join(states) or 'not running'})")
            await asyncio.sleep(min(5, max(0.05, deadline - loop.time())))

    async def _restore(self, services: list[str], sha: str) -> list[str]:
        """Point :latest back at the previous release's images and swap to them."""
        restored = []
        for svc in sorted(services, key=lambda s: (s != "orchestrator", s)):
            if await self.docker.has_image(svc, sha):
                await self.docker.tag(svc, sha, "latest")
                if svc == "migrate":        # one-shot job: retagging is enough
                    continue
                try:
                    await self.docker.swap(svc)
                    restored.append(svc)
                except DockerError as exc:
                    log.error("restore of %s failed: %s", svc, exc)
        return restored

    # ---- actions Aurelia can request (via the chrysalis function) ---------------

    async def rollback(self) -> str:
        async with self.lock:
            live = await self.store.live(2)
            if len(live) < 2:
                return "There's no earlier good release to roll back to."
            current, target = live
            known = await self.docker.compose_services()
            # Only what the current release changed; everything else is already at the target version.
            services = [s for s in (current.get("services") or []) if s in known] or built_services(known)
            if "orchestrator" in services and "migrate" not in services:
                services.append("migrate")
            self.state["busy"] = f"rollback to {target['sha'][:8]}"
            try:
                restored = await self._restore(services, target["sha"])
                await self.git.reset_to(target["sha"])
            finally:
                self.state["busy"] = None
            await self.store.update(current["id"], "rolled_back",
                                    f"Rolled back on request to {target['sha'][:8]} ({', '.join(restored)}).",
                                    finished=True)
            return f"Rolled back to {target['sha'][:8]}: {target.get('message') or 'previous release'}."

    async def approve(self, release_id: int) -> str:
        rel = await self.store.get(release_id)
        if rel is None or rel["status"] != "held":
            return "That release isn't waiting for approval."
        await self.store.update(release_id, "approved", "Approved; deploying on the next check.", finished=True)
        return f"Approved {rel['sha'][:8]}. It deploys on the next check."

    async def _prune(self, keep: int) -> None:
        keep_shas = {r["sha"] for r in await self.store.live(keep)}
        for svc in built_services(await self.docker.compose_services()):
            for tag in await self.docker.tags(svc):
                if tag != "latest" and len(tag) == 40 and tag not in keep_shas:
                    await self.docker.remove_tag(svc, tag)
