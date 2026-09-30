#!/usr/bin/env python3
"""Aurelia's node agent: runs on each of Kentrell's computers (Linux or Windows) and
lets her act there. Standard library only: nothing to install but Python 3.9+.

    python3 aurelia_node.py pair https://atlas-ai.<tailnet>.ts.net:8443 --name "Living room TV"
    python3 aurelia_node.py run        # what the service runs
    python3 aurelia_node.py status
    python3 aurelia_node.py forget     # delete this machine's key

How it works: the node never listens on a port. It calls out to Aurelia over the
tailnet with its own key (approved in her mailbox), says what it is, then waits for
commands (long polling) and reports how each went.

What it will do is fixed here, not decided by the server: open an http(s) page,
launch an installed app by name, list installed apps, and describe this machine.
Anything else is refused. Nothing is ever run through a shell, and only paths it
found itself (installed apps) are launched.
"""
from __future__ import annotations

import argparse
import configparser
import ctypes
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.2.0"
IS_WINDOWS = platform.system() == "Windows"
EXIT_REVOKED = 3                                   # the service manager doesn't restart after this


def config_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "aurelia-node"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "aurelia-node"


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


# ---- talking to Aurelia ------------------------------------------------------------------

class Revoked(Exception):
    pass


def call(server: str, path: str, body=None, key: str | None = None, timeout: float = 30):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "User-Agent": f"aurelia-node/{VERSION}"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and key:
            raise Revoked() from e
        detail = e.read().decode(errors="replace")[:300]
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise RuntimeError(f"{e.code}: {detail}") from e


# ---- this machine ------------------------------------------------------------------------------

def os_release() -> dict:
    out = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v.strip().strip('"')
    except OSError:
        pass
    return out


def screens() -> str | None:
    try:
        if IS_WINDOWS:
            u = ctypes.windll.user32
            return f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}"
        if shutil.which("xrandr"):
            r = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=5, env=session_env())
            found = re.findall(r"(\d+)/\d+x(\d+)/\d+", r.stdout)
            return ", ".join(f"{w}x{h}" for w, h in found) or None
    except Exception:
        return None
    return None


def machine_info() -> dict:
    rel = os_release()
    info = {"os": "Windows" if IS_WINDOWS else platform.system(),
            "os_version": platform.release() if IS_WINDOWS else (rel.get("PRETTY_NAME") or platform.release()),
            "hostname": socket.gethostname(), "arch": platform.machine(), "agent_version": VERSION,
            "screens": screens()}
    if not IS_WINDOWS:
        env = session_env()
        info.update(distro=rel.get("ID"), session=env.get("XDG_SESSION_TYPE") or None,
                    desktop=env.get("XDG_CURRENT_DESKTOP") or None)
    return {k: v for k, v in info.items() if v is not None}


SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY",
                "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE")


def session_env() -> dict:
    """The graphical session's variables, even if this service started before the login
    (Linux: systemd's user environment, which the desktop fills in when it starts)."""
    env = dict(os.environ)
    if IS_WINDOWS or not shutil.which("systemctl"):
        return env
    try:
        r = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            if k in SESSION_KEYS and v:
                env[k] = v
    except Exception:
        pass
    return env


def spawn(argv: list[str]) -> None:
    """Start something detached from this agent. A list of arguments, never a shell."""
    subprocess.Popen(argv, env=session_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


# ---- installed apps -----------------------------------------------------------------------------

LINUX_APP_DIRS = ["/usr/share/applications", "/usr/local/share/applications", "~/.local/share/applications",
                  "/var/lib/flatpak/exports/share/applications", "~/.local/share/flatpak/exports/share/applications",
                  "/var/lib/snapd/desktop/applications"]


def installed_apps() -> list[dict]:
    """[{'name', 'id', 'path'}] of apps a person would launch (no hidden or helper entries)."""
    apps, seen = [], set()
    if IS_WINDOWS:
        roots = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
                 Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
        for root in roots:
            for p in sorted(root.rglob("*")) if root.is_dir() else []:
                if p.suffix.lower() in (".lnk", ".url") and not re.search(r"uninstall|readme|help|documentation", p.stem, re.I):
                    if p.stem.lower() not in seen:
                        seen.add(p.stem.lower())
                        apps.append({"name": p.stem, "id": p.stem, "path": str(p)})
        return apps
    for d in LINUX_APP_DIRS:
        root = Path(d).expanduser()
        for p in sorted(root.glob("*.desktop")) if root.is_dir() else []:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(p, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("Type", "Application") != "Application" or e.get("NoDisplay", "").lower() == "true" \
                    or e.get("Hidden", "").lower() == "true" or not e.get("Name"):
                continue
            if p.stem in seen:
                continue
            seen.add(p.stem)
            apps.append({"name": e.get("Name"), "id": p.stem, "path": str(p)})
    return apps


def find_app(name: str) -> dict | None:
    want = " ".join(name.lower().split())
    apps = installed_apps()
    for test in (lambda a: a["name"].lower() == want,
                 lambda a: a["id"].lower() == want or a["id"].lower().endswith("." + want),
                 lambda a: all(w in a["name"].lower() for w in want.split()),
                 lambda a: want in a["id"].lower()):
        hits = [a for a in apps if test(a)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return sorted(hits, key=lambda a: len(a["name"]))[0]
    return None


# ---- what it will do ----------------------------------------------------------------------------

def do_open_url(args: dict) -> dict:
    url = str(args.get("url") or "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https pages")
    if IS_WINDOWS:
        os.startfile(url)                                    # the default browser
    elif shutil.which("xdg-open"):
        spawn(["xdg-open", url])
    else:
        raise ValueError("there's no way to open a browser here")
    return {"url": url}


def do_launch_app(args: dict) -> dict:
    name = " ".join(str(args.get("name") or "").split())
    if not name:
        raise ValueError("which app?")
    app = find_app(name)
    if app is None:
        raise ValueError(f"no app called {name} is installed")
    if IS_WINDOWS:
        os.startfile(app["path"])
    elif shutil.which("gtk-launch"):
        spawn(["gtk-launch", app["id"]])
    elif shutil.which("gio"):
        spawn(["gio", "launch", app["path"]])
    else:
        raise ValueError("there's no way to launch apps here")
    return {"app": app["name"]}


def do_list_apps(args: dict) -> dict:
    return {"apps": [{"name": a["name"], "id": a["id"]} for a in installed_apps()]}


def do_info(args: dict) -> dict:
    return machine_info()


COMMANDS = {"open_url": do_open_url, "launch_app": do_launch_app, "list_apps": do_list_apps, "info": do_info}


def execute(cmd: dict) -> dict:
    fn = COMMANDS.get(cmd.get("kind"))
    if fn is None:
        return {"ok": False, "error": f"this node doesn't do '{cmd.get('kind')}'"}
    try:
        return {"ok": True, "data": fn(cmd.get("args") or {})}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log("command failed:", cmd.get("kind"), repr(exc))
        return {"ok": False, "error": f"it failed here ({exc.__class__.__name__})"}


# ---- config ----------------------------------------------------------------------------------

def load() -> tuple[dict, str]:
    d = config_dir()
    try:
        return json.loads((d / "config.json").read_text()), (d / "key").read_text().strip()
    except OSError:
        raise SystemExit("Not paired yet. Run: aurelia_node.py pair <Aurelia's address> --name <this computer>")


def save(cfg: dict, key: str) -> None:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, indent=2))
    kp = d / "key"
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    if not IS_WINDOWS:
        os.chmod(kp, 0o600)


# ---- commands ----------------------------------------------------------------------------------

def cmd_pair(server: str, name: str) -> int:
    server = server.rstrip("/")
    info = machine_info()
    p = call(server, "/pair/request", {"kind": "node", "name": name or info["hostname"], "info": info})
    code = p["code"]
    print(f"\n  Approve this code in Aurelia's mailbox:\n\n      {code}\n\n"
          f"  (or on the server: ./scripts/pair.sh {code})  Waiting...", flush=True)
    deadline = time.time() + p.get("expires_in", 900)
    while time.time() < deadline:
        time.sleep(3)
        try:
            s = call(server, "/pair/status", {"device_id": p["device_id"], "claim": p["claim"]})
        except RuntimeError as exc:
            print(f"  Pairing ended: {exc}")
            return 1
        except OSError:
            continue                                     # a network blip; keep waiting
        if s["status"] == "approved":
            save({"server": server, "device_id": s["device_id"], "name": s["name"]}, s["key"])
            print(f"  Paired as {s['name']}.")
            return 0
        if s["status"] in ("declined", "expired", "revoked"):
            print(f"  Not paired: the request was {s['status']}.")
            return 1
    print("  The code expired. Run pair again.")
    return 1


def cmd_run() -> int:
    cfg, key = load()
    server, backoff, said_hello_at = cfg["server"], 2.0, 0.0
    log(f"Aurelia node {VERSION} for {cfg['name']} -> {server}")
    while True:
        try:
            if time.time() - said_hello_at > 600:
                call(server, "/nodes/hello", {"info": machine_info()}, key)
                said_hello_at = time.time()
                log("connected")
            got = call(server, "/nodes/commands?wait=25", key=key, timeout=45)
            backoff = 2.0
            for cmd in got.get("commands", []):
                log("command:", cmd.get("kind"))
                result = execute(cmd)
                call(server, f"/nodes/commands/{cmd['id']}/result", result, key)
        except Revoked:
            log("this computer's key was revoked or isn't valid anymore; stopping. Pair it again to reconnect.")
            return EXIT_REVOKED
        except (OSError, RuntimeError, ValueError) as exc:
            log(f"can't reach Aurelia ({exc}); retrying in {int(backoff)}s")
            said_hello_at = 0.0
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def cmd_status() -> int:
    cfg, _ = load()
    print(json.dumps({**cfg, "this_machine": machine_info()}, indent=2))
    return 0


def cmd_forget() -> int:
    for f in ("key", "config.json"):
        try:
            (config_dir() / f).unlink()
        except OSError:
            pass
    print("This computer's key is deleted here. Revoke it on Aurelia's devices panel too.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aurelia_node", description="Aurelia's node agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="pair this computer with Aurelia")
    p.add_argument("server")
    p.add_argument("--name", default="")
    sub.add_parser("run", help="connect and wait for work (what the service runs)")
    sub.add_parser("status")
    sub.add_parser("forget")
    a = ap.parse_args(argv)
    if a.cmd == "pair":
        return cmd_pair(a.server, a.name)
    return {"run": cmd_run, "status": cmd_status, "forget": cmd_forget}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
