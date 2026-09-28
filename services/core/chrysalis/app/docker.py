"""Docker operations, run against the host's Docker through the mounted socket.

The repo is mounted at the same path as on the host, so compose resolves build
contexts and bind mounts exactly as it would if you ran it yourself.
"""
import asyncio
import json
import logging
from dataclasses import dataclass

log = logging.getLogger("chrysalis.docker")


class DockerError(Exception):
    pass


@dataclass
class RunResult:
    code: int
    output: str


class Docker:
    def __init__(self, repo: str, project: str = "aurelia"):
        self.repo, self.project = repo, project

    async def _run(self, *args: str, env: dict | None = None, check: bool = True, timeout: float = 1800) -> RunResult:
        import os
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.repo, env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise DockerError(f"{' '.join(args[:4])}… timed out after {timeout:.0f}s")
        result = RunResult(proc.returncode, out.decode(errors="replace"))
        if check and result.code != 0:
            raise DockerError(f"{' '.join(args[:5])} failed:\n{_tail(result.output)}")
        return result

    async def access(self) -> str | None:
        """None if the Docker daemon is reachable; otherwise a plain explanation."""
        r = await self._run("docker", "version", "--format", "{{.Server.Version}}", check=False, timeout=20)
        if r.code == 0:
            return None
        if "permission denied" in r.output.lower():
            return ("No permission to use the Docker socket. Set DOCKER_GID in .env to the socket's group "
                    "(stat -c %g /var/run/docker.sock), then: docker compose up -d chrysalis")
        return "Docker is not reachable: " + _tail(r.output, 3)

    def image(self, service: str) -> str:
        return f"{self.project}-{service}"

    async def compose_services(self) -> set[str]:
        r = await self._run("docker", "compose", "config", "--services")
        return {s for s in r.output.split() if s}

    async def has_test_stage(self, service_dir: str) -> bool:
        import pathlib
        dockerfile = pathlib.Path(self.repo, service_dir, "Dockerfile")
        return dockerfile.exists() and " AS test" in dockerfile.read_text()

    async def test(self, service_dir: str) -> None:
        """Build the Dockerfile's `test` stage; it runs the unit tests and fails on any failure."""
        await self._run("docker", "build", "--target", "test", "--quiet", service_dir)

    async def build(self, services: list[str]) -> None:
        await self._run("docker", "compose", "build", *services)

    async def tag(self, service: str, src_tag: str, dst_tag: str, check: bool = True) -> bool:
        r = await self._run("docker", "tag", f"{self.image(service)}:{src_tag}",
                            f"{self.image(service)}:{dst_tag}", check=check)
        return r.code == 0

    async def has_image(self, service: str, tag: str) -> bool:
        r = await self._run("docker", "image", "inspect", f"{self.image(service)}:{tag}", check=False)
        return r.code == 0

    async def migrate(self) -> None:
        await self._run("docker", "compose", "run", "--rm", "migrate")

    async def swap(self, service: str) -> None:
        """Zero-downtime swap to the image tagged :latest (built beforehand)."""
        await self._run("./scripts/deploy.sh", service, env={"SKIP_BUILD": "1"})

    async def apply_stack(self, services: list[str]) -> None:
        await self._run("docker", "compose", "up", "-d", "--no-build", *services)

    async def health(self, service: str) -> list[str]:
        """Health of each running container of a service: healthy | unhealthy | starting | none | exited."""
        r = await self._run("docker", "compose", "ps", "--all", "--format", "json", service, check=False)
        states = []
        for line in r.output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            c = json.loads(line)
            if c.get("State") != "running":
                states.append("exited")
            else:
                states.append(c.get("Health") or "none")
        return states

    async def tags(self, service: str) -> list[str]:
        r = await self._run("docker", "image", "ls", self.image(service), "--format", "{{.Tag}}", check=False)
        return [t for t in r.output.split() if t and t != "<none>"]

    async def remove_tag(self, service: str, tag: str) -> None:
        await self._run("docker", "image", "rm", f"{self.image(service)}:{tag}", check=False)


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])
