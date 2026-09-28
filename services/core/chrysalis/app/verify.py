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
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.url}/registry/functions")
                r.raise_for_status()
                return {f["name"] for f in r.json()}
        except httpx.HTTPError:
            return set()

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

            deadline = loop.time() + cfg.functions_timeout_seconds
            while True:
                missing = expected_functions - await self.functions()
                if not missing:
                    break
                if loop.time() > deadline:
                    raise VerifyError(f"functions did not re-register: {', '.join(sorted(missing))}")
                await asyncio.sleep(2)
            passed.append(f"{len(expected_functions)} function(s) registered")

            if cfg.smoke_test and "probe" in expected_functions:
                r = await c.post(f"{self.url}/tasks", json={
                    "tool": "probe.echo", "args": {"message": "chrysalis smoke test", "steps": 0},
                    "user": "chrysalis", "trigger": "chrysalis"})
                if r.status_code != 202:
                    raise VerifyError(f"smoke test was refused ({r.status_code}): {r.text[:200]}")
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
