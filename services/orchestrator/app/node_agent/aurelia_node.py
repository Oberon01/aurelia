#!/usr/bin/env python3
"""Aurelia's node agent: runs on each of Kentrell's computers (Linux or Windows) and
lets her act there. Standard library only: nothing to install but Python 3.9+.

    python3 aurelia_node.py pair https://atlas-ai.<tailnet>.ts.net:8443 --name "Living room TV"
    python3 aurelia_node.py run        # what the service runs
    python3 aurelia_node.py shell on   # this computer's screen is Aurelia (off to stop)
    python3 aurelia_node.py status
    python3 aurelia_node.py forget     # delete this machine's key

How it works: the node never listens on a port. It calls out to Aurelia over the
tailnet with its own key (approved in her mailbox), says what it is, then waits for
commands (long polling) and reports how each went.

What it will do is fixed here, not decided by the server: open an http(s) page,
launch an installed app by name, list installed apps, describe this machine, and
bring its screen back to Aurelia. Anything else is refused. Nothing is ever run
through a shell, and only paths it found itself (installed apps) are launched.

The shell: with `shell on`, the agent keeps a kiosk browser (Chrome, Chromium or
Edge) filling the screen with Aurelia's view, signed in as this computer's screen.
Pages she opens here open inside it; "home" closes them. It talks to that browser
only through its control port on 127.0.0.1. On Linux the shell runs in the
"Aurelia" login session (the installer's --shell sets it up); a normal desktop
session is left alone, so GNOME stays a way back in.

Updates (his standing decision): every hello says which agent Aurelia serves. When
it differs, the agent downloads it from her, checks its checksum and that it
compiles, keeps the old one, swaps and restarts. The guard (aurelia_node_guard.py,
which never updates itself) puts the old one back if the new one can't reconnect.
After max_attempts failures it stops and tells her, once.
"""
from __future__ import annotations

import argparse
import configparser
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.3.0"
IS_WINDOWS = platform.system() == "Windows"
EXIT_REVOKED = 3                                   # the service manager doesn't restart after this
EXIT_UPDATED = 75                                  # restart me: a new version is in place
DEBUG_PORT = int(os.environ.get("AURELIA_DEBUG_PORT", "9333"))
HELLO_SECONDS = float(os.environ.get("AURELIA_HELLO_SECONDS", "600"))   # re-introduce itself (and check for updates)
POLL_WAIT = int(os.environ.get("AURELIA_POLL_WAIT", "25"))              # how long each wait for work lasts


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
    info = {"shell": SHELL.state() if SHELL else "off",
            "os": "Windows" if IS_WINDOWS else platform.system(),
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
    if SHELL and SHELL.running():
        SHELL.open(url)                                      # inside her shell, on top of her view
        return {"url": url, "in": "shell"}
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


def do_home(args: dict) -> dict:
    if not (SHELL and SHELL.running()):
        raise ValueError("her shell isn't running on this computer")
    return {"closed": SHELL.home()}


COMMANDS = {"open_url": do_open_url, "launch_app": do_launch_app, "list_apps": do_list_apps, "info": do_info,
            "home": do_home}


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


# ---- the shell: a kiosk browser that is her ---------------------------------------------------

def find_browser() -> str | None:
    forced = os.environ.get("AURELIA_BROWSER")
    if forced:
        return forced
    if IS_WINDOWS:
        roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for root in roots:
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                p = Path(root) / rel
                if root and p.exists():
                    return str(p)
        return None
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
                 "microsoft-edge-stable", "microsoft-edge"):
        if shutil.which(name):
            return shutil.which(name)
    return None


class Shell:
    """Keeps her view on the screen in a kiosk browser, restarting it if it closes."""

    def __init__(self, server: str, key: str):
        self.server, self.key = server.rstrip("/"), key
        self.proc: subprocess.Popen | None = None
        self.starts: list[float] = []
        self.note = "starting"
        self.stop_event = threading.Event()
        self.changed = threading.Event()                 # tell Aurelia soon (the devices panel shows it)

    def wanted(self) -> bool:
        if IS_WINDOWS:
            return True
        desktop = session_env().get("XDG_CURRENT_DESKTOP", "").lower()
        return "aurelia" in desktop                      # only in her session; GNOME stays GNOME

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def state(self) -> str:
        return "on" if self.running() else self.note

    def profile(self, browser: str) -> Path:
        if "/snap/" in browser or browser.startswith("/snap"):
            return Path.home() / "snap" / "chromium" / "common" / "aurelia-shell"    # what a snap may write
        return config_dir() / "shell-profile"

    def argv(self, browser: str, url: str) -> list[str]:
        prof = self.profile(browser)
        prof.mkdir(parents=True, exist_ok=True)
        args = [browser, "--kiosk", "--start-fullscreen", "--window-position=0,0", f"--user-data-dir={prof}",
                f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
                "--no-first-run", "--no-default-browser-check", "--noerrdialogs", "--disable-infobars",
                "--disable-session-crashed-bubble", "--disable-features=Translate",
                "--autoplay-policy=no-user-gesture-required", "--check-for-update-interval=31536000"]
        if IS_WINDOWS and "msedge" in browser.lower():
            args.append("--edge-kiosk-type=fullscreen")
        args += [a for a in os.environ.get("AURELIA_BROWSER_ARGS", "").split() if a]
        return args + [url]

    def start(self) -> None:
        browser = find_browser()
        if not browser:
            self.note = "no browser"
            return
        now = time.time()
        self.starts = [t for t in self.starts if now - t < 120]
        if len(self.starts) >= 5:
            self.note = "crashing"                      # five starts in two minutes: back off
            return
        login = call(self.server, "/nodes/screen", {}, self.key)["login"]
        self.proc = subprocess.Popen(self.argv(browser, self.server + login), env=session_env(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self.starts.append(now)
        self.changed.set()
        log("shell started:", Path(browser).name)

    def stop(self) -> None:
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.changed.set()

    def supervise(self) -> None:
        while not self.stop_event.wait(3):
            try:
                if self.wanted() and not self.running():
                    self.start()
                elif not self.wanted() and self.running():
                    self.stop()
                    self.note = "off (another desktop is in use)"
                elif not self.wanted():
                    self.note = "waiting for her session"
            except Revoked:
                self.stop()
                return
            except Exception as exc:
                self.note = f"can't start ({exc.__class__.__name__})"
                log("shell:", repr(exc))

    # the browser's control port (127.0.0.1 only)
    def cdp(self, path: str, method: str = "GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{DEBUG_PORT}{path}", method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode()
        try:
            return json.loads(body)
        except ValueError:
            return body

    def pages(self) -> list[dict]:
        return [t for t in self.cdp("/json/list") if t.get("type") == "page"]

    def is_hers(self, t: dict) -> bool:
        return t.get("url", "").startswith(self.server)

    def open(self, url: str) -> None:
        self.cdp("/json/new?" + urllib.parse.quote(url, safe=""), "PUT")    # quoted whole: keeps ?a=1&b=2#t

    def home(self) -> int:
        pages = self.pages()
        closed = 0
        for t in pages:
            if not self.is_hers(t):
                self.cdp(f"/json/close/{t['id']}")
                closed += 1
        hers = [t for t in pages if self.is_hers(t)]
        if hers:
            self.cdp(f"/json/activate/{hers[0]['id']}")
        return closed


SHELL: Shell | None = None


# ---- updating itself (Kentrell's standing decision) ---------------------------------------------

def state_file() -> Path:
    return config_dir() / "update.json"


def read_state() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def write_state(st: dict) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps(st))


def after_hello(server: str, key: str, hello: dict) -> None:
    """A successful hello: an update that got this far worked; failures get reported once."""
    st = read_state()
    if st.get("to") == VERSION:
        log(f"updated from {st.get('from')} to {VERSION}")
        st = {k: v for k, v in st.items() if k not in ("to", "from", "boots")}
        st.pop("attempts", None)
        write_state(st)
    rolled = st.get("rolled_back")
    if rolled:
        st["attempts"] = {**st.get("attempts", {}), rolled: st.get("attempts", {}).get(rolled, 0) + 1}
        st.pop("rolled_back")
        st["last_error"] = "the new version didn't reconnect, so the old one was put back"
        write_state(st)
    if hello.get("retry_update"):
        st = read_state()
        st.pop("attempts", None), st.pop("gave_up", None), st.pop("reported", None)
        write_state(st)
    want = hello.get("agent") or {}
    if want.get("version") and want["version"] != VERSION:
        update(server, key, want)


def update(server: str, key: str, want: dict) -> None:
    st = read_state()
    version, limit = want["version"], int(want.get("max_attempts", 3))
    tries = st.get("attempts", {}).get(version, 0)
    if tries >= limit:
        if st.get("reported") != version:
            try:
                call(server, "/nodes/update-failed", {"version": version, "attempts": tries,
                                                      "error": st.get("last_error", "")}, key)
                st["reported"] = version
                write_state(st)
            except (OSError, RuntimeError):
                pass
        return
    here = Path(__file__).resolve()
    new = here.with_suffix(".py.new")
    try:
        req = urllib.request.Request(server.rstrip("/") + "/node/agent.py",
                                     headers={"User-Agent": f"aurelia-node/{VERSION}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != want.get("sha256"):
            raise ValueError("the download didn't match Aurelia's checksum")
        if f'VERSION = "{version}"'.encode() not in data:
            raise ValueError("the download isn't the version Aurelia announced")
        new.write_bytes(data)
        import py_compile
        py_compile.compile(str(new), doraise=True)
    except Exception as exc:
        st.setdefault("attempts", {})[version] = tries + 1
        st["last_error"] = str(exc)[:300]
        write_state(st)
        log(f"update to {version} failed ({tries + 1}/{limit}): {exc}")
        try:
            new.unlink()
        except OSError:
            pass
        return
    shutil.copy2(here, here.with_suffix(".py.prev"))            # the guard's way back
    os.replace(new, here)
    st.update({"from": VERSION, "to": version, "boots": 0})
    st.setdefault("attempts", {})[version] = tries + 1
    write_state(st)
    log(f"updating from {VERSION} to {version}; restarting")
    if SHELL:
        SHELL.stop_event.set()
    sys.exit(EXIT_UPDATED)


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
    global SHELL
    cfg, key = load()
    server, backoff, said_hello_at = cfg["server"], 2.0, 0.0
    log(f"Aurelia node {VERSION} for {cfg['name']} -> {server}")
    if cfg.get("shell"):
        SHELL = Shell(server, key)
        threading.Thread(target=SHELL.supervise, daemon=True).start()
    while True:
        try:
            if time.time() - said_hello_at > HELLO_SECONDS or (SHELL and SHELL.changed.is_set()):
                if SHELL:
                    SHELL.changed.clear()
                hello = call(server, "/nodes/hello", {"info": machine_info()}, key)
                said_hello_at = time.time()
                log("connected")
                after_hello(server, key, hello)
            got = call(server, f"/nodes/commands?wait={POLL_WAIT}", key=key, timeout=POLL_WAIT + 20)
            backoff = 2.0
            for cmd in got.get("commands", []):
                log("command:", cmd.get("kind"))
                result = execute(cmd)
                call(server, f"/nodes/commands/{cmd['id']}/result", result, key)
        except Revoked:
            log("this computer's key was revoked or isn't valid anymore; stopping. Pair it again to reconnect.")
            if SHELL:
                SHELL.stop_event.set()
                SHELL.stop()
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


def cmd_shell(state: str) -> int:
    cfg, key = load()
    save({**cfg, "shell": state == "on"}, key)
    print(f"Her shell is {state} for this computer. It takes effect when the agent restarts"
          + (" (on Linux, in the Aurelia login session)." if state == "on" and not IS_WINDOWS else "."))
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
    sh = sub.add_parser("shell", help="make this computer's screen Aurelia (on) or not (off)")
    sh.add_argument("state", choices=["on", "off"])
    sub.add_parser("status")
    sub.add_parser("forget")
    a = ap.parse_args(argv)
    if a.cmd == "pair":
        return cmd_pair(a.server, a.name)
    if a.cmd == "shell":
        return cmd_shell(a.state)
    return {"run": cmd_run, "status": cmd_status, "forget": cmd_forget}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
