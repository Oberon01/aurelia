"""Post-deploy checks: is Aurelia actually working on the new version?"""
import asyncio

import httpx

from .settings import VerifyConfig


class VerifyError(Exception):
    pass


class Verifier:
    def __init__(self, orchestrator_url: str):
        self.url = orchestrator_url.rstrip("/")

    async def functions(self) -> set[str]:
        return set((await self._registered()).keys())

    async def _registered(self) -> dict[str, str]:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.url}/registry/functions")
                r.raise_for_status()
                return {f["name"]: f["url"] for f in r.json()}
        except httpx.HTTPError:
            return {}

    async def reachable_functions(self) -> set[str]:
        """Registered functions whose own /health answers — a registration left over
        from a container that's being replaced doesn't count."""
        ok = set()
        async with httpx.AsyncClient(timeout=3) as c:
            for name, url in (await self._registered()).items():
                try:
                    if (await c.get(f"{url.rstrip('/')}/health")).status_code == 200:
                        ok.add(name)
                except httpx.HTTPError:
                    pass
        return ok

    async def validate_config(self, path: str, content: str) -> str | None:
        """Ask the running orchestrator whether a config file matches its schema.
        Returns an error message, or None if valid (or if the orchestrator can't be asked)."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{self.url}/config/validate", json={"path": path, "content": content})
                if r.status_code == 404:        # an older orchestrator without the endpoint
                    return None
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPError:
            return None
        if body.get("valid"):
            return None
        err = body.get("error")
        return " ".join(err) if isinstance(err, list) else str(err)

    async def run(self, cfg: VerifyConfig, expected_functions: set[str]) -> list[str]:
        """Returns what passed; raises VerifyError on the first failure."""
        passed = []
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(timeout=5) as c:
            deadline = loop.time() + cfg.ready_timeout_seconds
            while True:
                try:
                    if (await c.get(f"{self.url}/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if loop.time() > deadline:
                    raise VerifyError("orchestrator did not become ready")
                await asyncio.sleep(2)
            passed.append("orchestrator ready")

            if cfg.proxy_url:                       # the way his devices reach her, not just from inside
                deadline, last = loop.time() + cfg.proxy_timeout_seconds, "no answer"
                async with httpx.AsyncClient(timeout=10) as px:
                    while True:
                        try:
                            r = await px.get(f"{cfg.proxy_url.rstrip('/')}/health")
                            if r.status_code == 200:
                                break
                            last = f"HTTP {r.status_code}"
                        except httpx.HTTPError as exc:
                            last = exc.__class__.__name__
                        if loop.time() > deadline:
                            raise VerifyError(f"Aurelia isn't reachable through the proxy ({last}): the new "
                                              "orchestrator is up, but his devices can't reach it")
                        await asyncio.sleep(2)
                passed.append("reachable through the proxy")

            deadline = loop.time() + cfg.functions_timeout_seconds
            while True:
                missing = expected_functions - await self.reachable_functions()
                if not missing:
                    break
                if loop.time() > deadline:
                    raise VerifyError(f"functions not reachable after deploy: {', '.join(sorted(missing))}")
                await asyncio.sleep(2)
            passed.append(f"{len(expected_functions)} function(s) registered and reachable")

            if cfg.smoke_test and "probe" in expected_functions:
                deadline = loop.time() + 30
                while True:      # a function that just started may need a moment
                    r = await c.post(f"{self.url}/tasks", json={
                        "tool": "probe.echo", "args": {"message": "chrysalis smoke test", "steps": 0},
                        "user": "chrysalis", "trigger": "chrysalis"})
                    if r.status_code == 202:
                        break
                    if r.status_code not in (404, 502) or loop.time() > deadline:
                        raise VerifyError(f"smoke test was refused ({r.status_code}): {r.text[:200]}")
                    await asyncio.sleep(2)
                task_id = r.json()["task_id"]
                deadline = loop.time() + 20
                while True:
                    status = (await c.get(f"{self.url}/tasks/{task_id}")).json().get("status")
                    if status == "completed":
                        break
                    if status in ("failed", "cancelled") or loop.time() > deadline:
                        raise VerifyError(f"smoke test task ended as {status}")
                    await asyncio.sleep(0.5)
                passed.append("smoke test (probe.echo) completed")
        return passed
