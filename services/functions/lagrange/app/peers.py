"""Who is calling: only my own stack (its own Docker network) may use Lagrange's API.

Lagrange also joins your home-lab network (to reach Gitea or Home Assistant by name), so a request from one of
those containers, or from a local MCP server running inside this container, must not be able to run a
connection's tools: those requests come from another network, or from loopback, and are refused. My stack's
network is the one "postgres" lives on (only my stack is on it). Where that can't be told (a dev machine, the
tests), nothing is refused here and the other checks still apply: but where it must be told (STACK_GUARD=1,
set for the running stack) a name that won't resolve refuses everything, never lets everything in.
"""
import ipaddress
import os
import socket
import time

_cache: dict = {"at": 0.0, "net": None}
ANCHOR = "postgres"


def _strict() -> bool:
    return os.environ.get("STACK_GUARD", "") == "1"


def _routes() -> list[ipaddress.IPv4Network]:
    nets = []
    try:
        with open("/proc/net/route") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) < 8:
                    continue
                dest, mask = int(parts[1], 16), int(parts[7], 16)
                if mask == 0:
                    continue
                to_ip = lambda v: ipaddress.IPv4Address(int.from_bytes(v.to_bytes(4, "little"), "big"))
                nets.append(ipaddress.IPv4Network(f"{to_ip(dest)}/{to_ip(mask)}", strict=False))
    except OSError:
        pass
    return nets


def stack_network(anchor: str = ANCHOR) -> ipaddress.IPv4Network | None:
    """The network my stack shares with `anchor` (looked up at most once a minute; a failed look-up keeps the
    last one found)."""
    if time.monotonic() - _cache["at"] < 60:
        return _cache["net"]
    net = None
    try:
        ip = ipaddress.IPv4Address(socket.gethostbyname(anchor))
        net = next((n for n in _routes() if ip in n), None)
    except (OSError, ValueError):
        net = None
    _cache.update(at=time.monotonic(), net=net or _cache["net"])     # a failed look-up keeps the last one found
    return _cache["net"]


def from_stack(host: str | None, anchor: str = ANCHOR) -> bool:
    net = stack_network(anchor)
    if net is None:
        return not _strict()                         # can't tell: only a dev machine or the tests let it through
    try:
        return ipaddress.ip_address(host or "") in net
    except ValueError:
        return False
