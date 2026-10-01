"""Thin async wrapper around git in the server's working copy."""
import asyncio
from dataclasses import dataclass


class GitError(Exception):
    pass


@dataclass
class Commit:
    sha: str
    author: str
    message: str          # the subject line
    body: str = ""        # the rest of the message: what it does and why

    def as_dict(self) -> dict:
        return {"sha": self.sha, "author": self.author, "subject": self.message, "body": self.body}


class Git:
    def __init__(self, repo: str, remote: str = "origin"):
        self.repo, self.remote = repo, remote

    async def _run(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self.repo, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {err.decode().strip() or out.decode().strip()}")
        return out.decode().strip()

    async def fetch(self, branch: str) -> None:
        await self._run("fetch", "--quiet", self.remote, branch)

    async def head(self) -> str:
        return await self._run("rev-parse", "HEAD")

    async def remote_head(self, branch: str) -> str:
        return await self._run("rev-parse", f"{self.remote}/{branch}")

    async def is_ancestor(self, older: str, newer: str) -> bool:
        try:
            await self._run("merge-base", "--is-ancestor", older, newer)
            return True
        except GitError:
            return False

    async def changed_paths(self, old: str, new: str) -> list[str]:
        out = await self._run("diff", "--name-only", old, new)
        return [line for line in out.splitlines() if line]

    async def commits(self, old: str | None, new: str) -> list[Commit]:
        """Newest first, each with its whole message (subject and body)."""
        out = await self._run("log", "--format=%H%x1f%ae%x1f%s%x1f%b%x1e", f"{old}..{new}" if old else f"-1", *([] if old else [new]))
        commits = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record.strip():
                continue
            sha, author, message, body = (record.split("\x1f", 3) + ["", "", "", ""])[:4]
            commits.append(Commit(sha.strip(), author, message, body.strip()))
        return commits

    async def show(self, sha: str, path: str) -> str:
        return await self._run("show", f"{sha}:{path}")

    async def dirty(self) -> list[str]:
        """Tracked files changed on the server, including permission changes
        (ignored files like .env don't count)."""
        out = await self._run("diff", "--name-only", "HEAD")
        return [line for line in out.splitlines() if line]

    async def fast_forward(self, sha: str) -> None:
        await self._run("merge", "--ff-only", "--quiet", sha)

    async def reset_to(self, sha: str) -> None:
        await self._run("reset", "--hard", "--quiet", sha)
