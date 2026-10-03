#!/usr/bin/env python3
"""Aurelia's node agent: runs on each of Kentrell's computers (Linux or Windows) and
lets her act there. Standard library only: nothing to install but Python 3.9+.

    python3 aurelia_node.py pair https://atlas-ai.<tailnet>.ts.net:8443 --name "Living room TV"
    python3 aurelia_node.py run        # what the service runs
    python3 aurelia_node.py shell on   # this computer's screen is Aurelia (off to stop)
    python3 aurelia_node.py status
    python3 aurelia_node.py forget     # delete this machine's key
    python3 aurelia_node.py login add youtube   # a login she may use here (she never sees it)

How it works: the node never listens on a port. It calls out to Aurelia over the
tailnet with its own key (approved in her mailbox), says what it is, then waits for
commands (long polling) and reports how each went.

What it will do is fixed here, not decided by the server: open an http(s) page,
launch an installed app by name, list installed apps, describe this machine (its
hardware, how it's doing, its processes), close a process he confirmed (only that exact
process, only his, never the system's or her own), bring its screen back to Aurelia, and see and use the page on her screen (number what can
be clicked, click, type, press a key, scroll, go back, control a video). Anything
else is refused. Nothing is ever run through a shell, and only paths it found itself
(installed apps) are launched. The page script is fixed here too; it won't type into
password or payment boxes, and a click (or Enter) that looks like it buys, pays,
sends, posts or deletes comes back to Aurelia for his confirmation instead.

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
import base64
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

VERSION = "0.7.1"
IS_WINDOWS = platform.system() == "Windows"
EXIT_REVOKED = 3                                   # the service manager doesn't restart after this
EXIT_UPDATED = 75                                  # restart me: a new version is in place
EXIT_PAUSED = 4                                    # exam mode: stay stopped (a timer or he brings me back)
DEBUG_PORT = int(os.environ.get("AURELIA_DEBUG_PORT", "9333"))
HELLO_SECONDS = float(os.environ.get("AURELIA_HELLO_SECONDS", "600"))   # re-introduce itself (and check for updates)
POLL_WAIT = int(os.environ.get("AURELIA_POLL_WAIT", "25"))              # how long each wait for work lasts


def config_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "aurelia-node"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "aurelia-node"


def log(*parts) -> None:
    if sys.stderr is not None:
        print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)
        return
    try:                                                     # Windows, started without a console (pythonw): a file
        f = config_dir() / "agent.log"
        if f.exists() and f.stat().st_size > 1_000_000:
            f.replace(f.with_suffix(".log.old"))
        with f.open("a", encoding="utf-8") as out:
            out.write(" ".join([time.strftime("%Y-%m-%d %H:%M:%S"), *map(str, parts)]) + "\n")
    except OSError:
        pass


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
    try:
        info["booted_at"] = int(_boot_time()) if not IS_WINDOWS else None    # a restart shows up as a new value
    except Exception:
        pass
    if role_hint():
        info["role"] = role_hint()          # his choice at install; the devices panel has the last word
    m = read_mode()
    info["mode"] = m.get("mode", "normal")
    if m.get("until"):
        info["mode_until"] = int(m["until"])
    if not IS_WINDOWS:
        env = session_env()
        info.update(distro=rel.get("ID"), session=env.get("XDG_SESSION_TYPE") or None,
                    desktop=env.get("XDG_CURRENT_DESKTOP") or None)
    return {k: v for k, v in info.items() if v is not None}


NODE_ROLES = ("presentation", "workstation", "server", "room")


def role_hint() -> str | None:
    try:
        r = (config_dir() / "role").read_text().strip()
    except OSError:
        return None
    return r if r in NODE_ROLES else None


def cmd_role(role: str) -> int:
    config_dir().mkdir(parents=True, exist_ok=True)
    if role == "auto":
        try:
            (config_dir() / "role").unlink()
        except OSError:
            pass
        print("Role: guessed from this computer (shell on = presentation, a screen = workstation, none = server).")
        return 0
    (config_dir() / "role").write_text(role + "\n")
    print(f"Role: {role}. It's a suggestion: the devices panel on Aurelia's display has the last word.")
    return 0


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


# ---- what this computer can report (node health) ----------------------------------------------
#
# Hardware, live state and processes, read straight from the system: /proc and /sys on Linux,
# system calls on Windows. Nothing is run through a shell; the only programs started are
# nvidia-smi and lspci, by full argument list, when they exist. Command lines are never
# reported (they can hold tokens): a process is its name, its user and what it uses.

PROC, SYS = Path("/proc"), Path("/sys")
DISK_TYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "ntfs", "ntfs3", "fuseblk", "vfat", "exfat"}
PROTECTED_LINUX = {"systemd", "init", "gnome-shell", "gnome-session-b", "gdm", "gdm-session-wor", "gdm-x-session",
                   "gdm-wayland-ses", "Xorg", "Xwayland", "dbus-daemon", "dbus-broker", "pipewire", "wireplumber",
                   "pipewire-pulse", "pulseaudio", "sshd", "matchbox-window", "aurelia-session", "tailscaled"}
PROTECTED_WINDOWS = {"system", "idle", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
                     "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
                     "memory compression", "secure system", "lsaiso.exe", "tailscaled.exe", "tailscale-ipn.exe"}


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return default


def _run(argv: list[str], timeout: float = 4) -> str:
    """A fixed helper program, by argument list; its output or ''."""
    exe = shutil.which(argv[0])
    if not exe:
        return ""
    try:
        return subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gpus(live: bool = True) -> list[dict]:
    if current_mode() == "gaming":                           # never touch the GPU while he plays
        return []
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits"])
    found = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            num = lambda s: int(float(s)) if re.fullmatch(r"[\d.]+", s) else None
            g = {"name": parts[0], "memory_mb": num(parts[1])}
            if live:
                g.update(memory_used_mb=num(parts[2]), load=num(parts[3]), temp_c=num(parts[4]))
            found.append(g)
    if found or IS_WINDOWS:
        return found or _windows_gpus()
    for line in _run(["lspci", "-mm"]).splitlines():                  # no NVIDIA tools: at least the name
        if re.search(r'"(VGA compatible controller|3D controller|Display controller)"', line):
            fields = re.findall(r'"([^"]*)"', line)
            if len(fields) >= 3:
                found.append({"name": f"{fields[1]} {fields[2]}".strip()})
    return found


# Linux ---------------------------------------------------------------------------------------

def _meminfo() -> dict:
    out = {}
    for line in _read(PROC / "meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip().split()[:1]:
            out[k] = int(v.split()[0]) * 1024
    return out


def _cpu_times() -> tuple[int, int]:
    """(busy, total) jiffies for the whole machine."""
    first = _read(PROC / "stat").splitlines()[:1]
    if not first:
        return 0, 0
    f = [int(x) for x in first[0].split()[1:9]]
    idle = f[3] + f[4]
    return sum(f) - idle, sum(f)


def _linux_disks() -> list[dict]:
    seen, out = set(), []
    for line in _read(PROC / "mounts").splitlines():
        dev, mount, fstype = (line.split() + ["", "", ""])[:3]
        mount = mount.replace("\\040", " ")
        if fstype not in DISK_TYPES or dev in seen or mount.startswith(("/snap", "/boot/efi", "/var/snap")):
            continue
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            continue
        if u.total < 1 << 30:
            continue
        seen.add(dev)
        out.append({"mount": mount, "total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1),
                    "used_percent": round(100 * (u.total - u.free) / u.total)})
    return out


def _linux_temps() -> list[dict]:
    temps = []
    for hw in sorted((SYS / "class" / "hwmon").glob("hwmon*")):
        name = _read(hw / "name")
        for t in sorted(hw.glob("temp*_input")):
            raw = _read(t)
            if raw.lstrip("-").isdigit():
                label = _read(t.with_name(t.name.replace("_input", "_label"))) or name
                c = int(raw) / 1000
                if -20 < c < 150:
                    temps.append({"sensor": name, "label": label, "c": round(c)})
    if not temps:
        for z in sorted((SYS / "class" / "thermal").glob("thermal_zone*")):
            raw = _read(z / "temp")
            if raw.lstrip("-").isdigit() and -20 < int(raw) / 1000 < 150:
                temps.append({"sensor": _read(z / "type") or z.name, "label": _read(z / "type"), "c": round(int(raw) / 1000)})
    return temps


def _linux_speed() -> int | None:
    """How fast the CPU runs right now, as a % of its top speed (low under load = throttling)."""
    cur, top = [], []
    for c in (SYS / "devices" / "system" / "cpu").glob("cpu[0-9]*"):
        a, b = _read(c / "cpufreq" / "scaling_cur_freq"), _read(c / "cpufreq" / "cpuinfo_max_freq")
        if a.isdigit() and b.isdigit() and int(b):
            cur.append(int(a))
            top.append(int(b))
    return round(100 * sum(cur) / sum(top)) if top else None


def _linux_battery() -> dict | None:
    for p in (SYS / "class" / "power_supply").glob("*"):
        if _read(p / "type") == "Battery" and _read(p / "capacity").isdigit():
            return {"percent": int(_read(p / "capacity")), "charging": _read(p / "status").lower() in ("charging", "full")}
    return None


def _net_bytes() -> dict:
    out = {}
    for line in _read(PROC / "net" / "dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if name.strip() != "lo" and len(f) >= 9:
            out[name.strip()] = (int(f[0]), int(f[8]))
    return out


def _linux_procs() -> dict[int, dict]:
    """pid → {name, uid, ticks (cpu), rss, start} for every process we can read."""
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    procs = {}
    for d in PROC.iterdir():
        if not d.name.isdigit():
            continue
        stat = _read(d / "stat")
        if ")" not in stat:
            continue
        name = stat[stat.find("(") + 1:stat.rfind(")")]
        f = stat[stat.rfind(")") + 2:].split()
        uid = None
        for line in _read(d / "status").splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                break
        try:
            if f[0] in ("Z", "X"):                              # exited, just not collected by its parent yet
                continue
            procs[int(d.name)] = {"name": name, "uid": uid, "ticks": int(f[11]) + int(f[12]), "start": int(f[19]),
                                  "rss": int(f[21]) * page, "ppid": int(f[1])}
        except (IndexError, ValueError):
            continue
    return procs


def _boot_time() -> float:
    for line in _read(PROC / "stat").splitlines():
        if line.startswith("btime"):
            return float(line.split()[1])
    return time.time() - float(_read(PROC / "uptime", "0").split()[0] or 0)


def _user(uid) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, TypeError):
        return str(uid)


def linux_specs() -> dict:
    cpu = ""
    for line in _read(PROC / "cpuinfo").splitlines():
        k, _, v = line.partition(":")
        if k.strip() in ("model name", "Model", "Hardware") and v.strip():
            cpu = " ".join(v.split())
            break
    dmi = SYS / "class" / "dmi" / "id"
    model = " ".join(x for x in (_read(dmi / "sys_vendor"), _read(dmi / "product_name")) if x and "o.e.m" not in x.lower())
    return {"cpu": cpu, "cores": os.cpu_count(), "memory_gb": round(_meminfo().get("MemTotal", 0) / 1e9, 1),
            "model": model or None, "gpus": gpus(live=False),
            "disks": [{"mount": d["mount"], "total_gb": d["total_gb"]} for d in _linux_disks()]}


def linux_sample(seconds: float = 1.0, with_procs: bool = True) -> dict:
    busy0, total0 = _cpu_times()
    net0 = _net_bytes()
    procs0 = _linux_procs() if with_procs else {}
    time.sleep(seconds)
    busy1, total1 = _cpu_times()
    net1 = _net_bytes()
    procs1 = _linux_procs() if with_procs else {}
    span = max(1, total1 - total0)
    mem = _meminfo()
    total_mem = mem.get("MemTotal", 0) or 1
    out = {"cpu_percent": round(100 * (busy1 - busy0) / span),
           "load": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
           "memory": {"total_gb": round(total_mem / 1e9, 1), "used_percent": round(100 * (total_mem - mem.get("MemAvailable", 0)) / total_mem),
                      "available_gb": round(mem.get("MemAvailable", 0) / 1e9, 1)},
           "swap": {"total_gb": round(mem.get("SwapTotal", 0) / 1e9, 1),
                    "used_percent": round(100 * (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / mem["SwapTotal"])
                    if mem.get("SwapTotal") else 0},
           "disks": _linux_disks(), "temps": _linux_temps(), "speed_percent": _linux_speed(),
           "battery": _linux_battery(), "gpus": gpus(),
           "uptime_hours": round(float(_read(PROC / "uptime", "0").split()[0] or 0) / 3600, 1),
           "network": {k: {"down_kbps": round((net1[k][0] - v[0]) * 8 / seconds / 1000),
                           "up_kbps": round((net1[k][1] - v[1]) * 8 / seconds / 1000)}
                       for k, v in net0.items() if k in net1 and (net1[k][0] - v[0] or net1[k][1] - v[1])}}
    if with_procs:
        tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        boot, me = _boot_time(), os.getuid() if hasattr(os, "getuid") else None
        rows = []
        for pid, p in procs1.items():
            before = procs0.get(pid)
            used = p["ticks"] - before["ticks"] if before and before["start"] == p["start"] else 0
            rows.append({"pid": pid, "name": p["name"], "user": _user(p["uid"]), "mine": p["uid"] == me,
                         "cpu_percent": round(100 * used / span, 1), "memory_mb": round(p["rss"] / 1e6),
                         "started": round(boot + p["start"] / tick), "start_key": str(p["start"])})
        out["processes"] = rows
    return out


# Windows (system calls through ctypes) --------------------------------------------------------

def _win():
    from ctypes import wintypes
    return ctypes, wintypes, ctypes.windll.kernel32


def _filetime(ft) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _windows_gpus() -> list[dict]:
    if not IS_WINDOWS:
        return []
    import winreg
    found = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    for i in range(10):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{i:04d}") as k:
                name = winreg.QueryValueEx(k, "DriverDesc")[0]
                if name and "basic" not in name.lower():
                    found.append({"name": name})
        except OSError:
            continue
    return found


def _windows_memory() -> dict:
    c, w, k32 = _win()

    class MEMORYSTATUSEX(c.Structure):
        _fields_ = [("dwLength", w.DWORD), ("dwMemoryLoad", w.DWORD), ("ullTotalPhys", c.c_ulonglong),
                    ("ullAvailPhys", c.c_ulonglong), ("ullTotalPageFile", c.c_ulonglong), ("ullAvailPageFile", c.c_ulonglong),
                    ("ullTotalVirtual", c.c_ulonglong), ("ullAvailVirtual", c.c_ulonglong), ("ullAvailExtendedVirtual", c.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = c.sizeof(MEMORYSTATUSEX)
    k32.GlobalMemoryStatusEx(c.byref(m))
    return {"total": m.ullTotalPhys, "available": m.ullAvailPhys, "load": m.dwMemoryLoad,
            "page_total": m.ullTotalPageFile, "page_free": m.ullAvailPageFile}


def _windows_cpu_times() -> tuple[int, int]:
    c, w, k32 = _win()
    idle, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME()
    k32.GetSystemTimes(c.byref(idle), c.byref(kernel), c.byref(user))
    total = _filetime(kernel) + _filetime(user)              # kernel time includes idle
    return total - _filetime(idle), total


def _windows_disks() -> list[dict]:
    c, w, k32 = _win()
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if k32.GetDriveTypeW(c.c_wchar_p(root)) != 3:        # DRIVE_FIXED
            continue
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        out.append({"mount": f"{letter}:", "total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1),
                    "used_percent": round(100 * (u.total - u.free) / u.total) if u.total else 0})
    return out


def _windows_battery() -> dict | None:
    c, w, k32 = _win()

    class SYSTEM_POWER_STATUS(c.Structure):
        _fields_ = [("ACLineStatus", c.c_ubyte), ("BatteryFlag", c.c_ubyte), ("BatteryLifePercent", c.c_ubyte),
                    ("SystemStatusFlag", c.c_ubyte), ("BatteryLifeTime", w.DWORD), ("BatteryFullLifeTime", w.DWORD)]
    s = SYSTEM_POWER_STATUS()
    if not k32.GetSystemPowerStatus(c.byref(s)) or s.BatteryFlag == 128 or s.BatteryLifePercent == 255:
        return None                                          # no battery
    return {"percent": int(s.BatteryLifePercent), "charging": s.ACLineStatus == 1}


def _windows_procs() -> dict[int, dict]:
    c, w, k32 = _win()

    class PROCESSENTRY32W(c.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", c.c_void_p),
                    ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", c.c_long), ("dwFlags", w.DWORD), ("szExeFile", c.c_wchar * 260)]

    class PMC(c.Structure):
        _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD), ("PeakWorkingSetSize", c.c_size_t),
                    ("WorkingSetSize", c.c_size_t), ("QuotaPeakPagedPoolUsage", c.c_size_t),
                    ("QuotaPagedPoolUsage", c.c_size_t), ("QuotaPeakNonPagedPoolUsage", c.c_size_t),
                    ("QuotaNonPagedPoolUsage", c.c_size_t), ("PagefileUsage", c.c_size_t), ("PeakPagefileUsage", c.c_size_t)]
    k32.CreateToolhelp32Snapshot.restype = w.HANDLE
    k32.OpenProcess.restype = w.HANDLE
    snap = k32.CreateToolhelp32Snapshot(2, 0)                # TH32CS_SNAPPROCESS
    entry = PROCESSENTRY32W()
    entry.dwSize = c.sizeof(PROCESSENTRY32W)
    mine = w.DWORD()
    k32.ProcessIdToSessionId(os.getpid(), c.byref(mine))
    procs = {}
    ok = k32.Process32FirstW(snap, c.byref(entry))
    while ok:
        pid = int(entry.th32ProcessID)
        rec = {"name": entry.szExeFile, "uid": None, "ticks": 0, "start": 0, "rss": 0, "ppid": int(entry.th32ParentProcessID)}
        sess = w.DWORD()
        if k32.ProcessIdToSessionId(pid, c.byref(sess)):
            rec["uid"] = int(sess.value)                     # "same user" on Windows = his session
        h = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            cr, ex, ke, us = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
            if k32.GetProcessTimes(h, c.byref(cr), c.byref(ex), c.byref(ke), c.byref(us)):
                rec["ticks"], rec["start"] = _filetime(ke) + _filetime(us), _filetime(cr)
            pmc = PMC()
            pmc.cb = c.sizeof(PMC)
            if k32.K32GetProcessMemoryInfo(h, c.byref(pmc), pmc.cb):
                rec["rss"] = int(pmc.WorkingSetSize)
            k32.CloseHandle(h)
        procs[pid] = rec
        ok = k32.Process32NextW(snap, c.byref(entry))
    k32.CloseHandle(snap)
    procs["_session"] = int(mine.value)
    return procs


def windows_specs() -> dict:
    import winreg
    def reg(path, value):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                return str(winreg.QueryValueEx(k, value)[0]).strip()
        except OSError:
            return ""
    cpu = " ".join(reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString").split())
    model = " ".join(x for x in (reg(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer"),
                                 reg(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")) if x)
    return {"cpu": cpu, "cores": os.cpu_count(), "memory_gb": round(_windows_memory()["total"] / 1e9, 1),
            "model": model or None, "gpus": gpus(live=False),
            "disks": [{"mount": d["mount"], "total_gb": d["total_gb"]} for d in _windows_disks()]}


def windows_sample(seconds: float = 1.0, with_procs: bool = True) -> dict:
    busy0, total0 = _windows_cpu_times()
    procs0 = _windows_procs() if with_procs else {}
    t0 = time.time()
    time.sleep(seconds)
    busy1, total1 = _windows_cpu_times()
    procs1 = _windows_procs() if with_procs else {}
    wall = max(1e-3, time.time() - t0) * 1e7 * (os.cpu_count() or 1)       # 100 ns units, all cores
    m = _windows_memory()
    out = {"cpu_percent": round(100 * (busy1 - busy0) / max(1, total1 - total0)), "load": None,
           "memory": {"total_gb": round(m["total"] / 1e9, 1), "used_percent": int(m["load"]),
                      "available_gb": round(m["available"] / 1e9, 1)},
           "swap": {"total_gb": round(max(0, m["page_total"] - m["total"]) / 1e9, 1),
                    "used_percent": round(100 * (1 - m["page_free"] / m["page_total"])) if m["page_total"] else 0},
           "disks": _windows_disks(), "temps": [], "speed_percent": None, "battery": _windows_battery(),
           "gpus": gpus(), "uptime_hours": round(ctypes.windll.kernel32.GetTickCount64() / 3.6e6, 1), "network": {}}
    if with_procs:
        session = procs1.pop("_session", None)
        procs0.pop("_session", None)
        rows = []
        for pid, p in procs1.items():
            before = procs0.get(pid)
            used = p["ticks"] - before["ticks"] if before and before["start"] == p["start"] else 0
            rows.append({"pid": pid, "name": p["name"], "user": "you" if p["uid"] == session else "system",
                         "mine": p["uid"] == session and p["uid"] not in (None, 0), "cpu_percent": round(100 * used / wall, 1),
                         "memory_mb": round(p["rss"] / 1e6),
                         "started": round(p["start"] / 1e7 - 11644473600) if p["start"] else None,
                         "start_key": str(p["start"])})
        out["processes"] = rows
    return out


# Both ------------------------------------------------------------------------------------------

_SPECS: dict | None = None


def specs() -> dict:
    global _SPECS
    if _SPECS is None:
        try:
            _SPECS = windows_specs() if IS_WINDOWS else linux_specs()
        except Exception as exc:
            log("couldn't read the hardware:", repr(exc))
            _SPECS = {}
        try:
            _SPECS.update({k: v for k, v in wake_address().items() if v})
        except Exception:
            pass
    return _SPECS


def sample(seconds: float = 1.0, with_procs: bool = True) -> dict:
    return (windows_sample if IS_WINDOWS else linux_sample)(seconds, with_procs)


def vitals() -> dict:
    """The small reading that goes with every check-in (for alerts and the devices panel)."""
    try:
        s = sample(0.5, with_procs=False)
    except Exception as exc:
        log("couldn't read vitals:", repr(exc))
        return {}
    return {"cpu": s["cpu_percent"], "memory": s["memory"]["used_percent"], "swap": s["swap"]["used_percent"],
            "disks": [{"mount": d["mount"], "used": d["used_percent"], "free_gb": d["free_gb"]} for d in s["disks"]],
            "temp": max((t["c"] for t in s["temps"]), default=None),
            "gpu_temp": max((g.get("temp_c") or 0 for g in s["gpus"]), default=None) or None,
            "battery": s["battery"]}


def _groups(rows: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for r in rows:
        g = by.setdefault(r["name"], {"name": r["name"], "count": 0, "cpu_percent": 0.0, "memory_mb": 0, "mine": True})
        g["count"] += 1
        g["cpu_percent"] = round(g["cpu_percent"] + r["cpu_percent"], 1)
        g["memory_mb"] += r["memory_mb"]
        g["mine"] = g["mine"] and r["mine"]
    return sorted(by.values(), key=lambda g: (g["cpu_percent"], g["memory_mb"]), reverse=True)


def _public(r: dict) -> dict:
    out = {k: r[k] for k in ("pid", "name", "user", "mine", "cpu_percent", "memory_mb", "started", "start_key")}
    if _protected(r["pid"], r["name"]):
        out["protected"] = True                               # never offered for closing
    return out


def do_health(args: dict) -> dict:
    s = sample(1.0)
    rows = s.pop("processes", [])
    s["top_cpu"] = [_public(r) for r in sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:8]]
    s["top_memory"] = [_public(r) for r in sorted(rows, key=lambda r: r["memory_mb"], reverse=True)[:8]]
    s["groups"] = _groups(rows)[:10]
    s["process_count"] = len(rows)
    return {"specs": specs(), **s}


def do_processes(args: dict) -> dict:
    s = sample(1.0)
    rows = s.get("processes", [])
    name = " ".join(str(args.get("name") or "").lower().split())
    if name:
        exe = name if name.endswith(".exe") or not IS_WINDOWS else name + ".exe"
        rows = [r for r in rows if r["name"].lower() in (name, exe)] or \
               [r for r in rows if r["name"].lower().startswith(name)]
    key = "memory_mb" if args.get("sort") == "memory" else "cpu_percent"
    rows.sort(key=lambda r: (r[key], r["memory_mb"]), reverse=True)
    limit = max(1, min(int(args.get("limit") or 25), 200))
    return {"processes": [_public(r) for r in rows[:limit]], "groups": _groups(rows)[:15], "total": len(rows),
            "cpu_percent": s["cpu_percent"], "memory_percent": s["memory"]["used_percent"]}


def _protected(pid: int, name: str) -> bool:
    ours = {os.getpid(), os.getppid()}
    if SHELL and SHELL.proc:
        ours.add(SHELL.proc.pid)                              # her own screen
    if pid in ours or pid <= 4:
        return True
    return name.lower() in PROTECTED_WINDOWS if IS_WINDOWS else name in PROTECTED_LINUX


def _alive(pid: int, start_key: str) -> bool:
    if IS_WINDOWS:
        p = _windows_procs().get(pid)
        return bool(p) and str(p["start"]) == start_key
    p = _linux_procs().get(pid)
    return bool(p) and str(p["start"]) == start_key


def _windows_close(pid: int, force: bool) -> bool:
    """Gently: ask its windows to close (like clicking X). Force: end it. True if something was sent."""
    c, w, k32 = _win()
    if force:
        h = k32.OpenProcess(0x0001, False, pid)              # PROCESS_TERMINATE
        if not h:
            return False
        done = bool(k32.TerminateProcess(h, 1))
        k32.CloseHandle(h)
        return done
    user32, sent = c.windll.user32, []
    proto = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def each(hwnd, _):
        owner = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, c.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)            # WM_CLOSE
            sent.append(hwnd)
        return True
    user32.EnumWindows(proto(each), 0)
    return bool(sent)


def do_kill(args: dict) -> dict:
    """Close exactly the processes Aurelia showed him (number + start time), his own only,
    never protected ones. Gently unless force; waits up to 5 s to see them go."""
    targets = [t for t in (args.get("targets") or []) if isinstance(t, dict)][:100]
    if not targets:
        raise ValueError("which process? (its number and start time from the list)")
    force = bool(args.get("force"))
    procs = _windows_procs() if IS_WINDOWS else _linux_procs()
    session = procs.pop("_session", None) if IS_WINDOWS else None
    me = session if IS_WINDOWS else (os.getuid() if hasattr(os, "getuid") else None)
    sent, refused = [], []
    for t in targets:
        try:
            pid, key = int(t.get("pid")), str(t.get("start_key"))
        except (TypeError, ValueError):
            continue
        p = procs.get(pid)
        if not p or str(p["start"]) != key:
            refused.append({"pid": pid, "why": "it isn't running anymore (or a different program has that number now)"})
            continue
        if _protected(pid, p["name"]):
            refused.append({"pid": pid, "name": p["name"], "why": "it's part of the system or of me, so I don't close it"})
            continue
        if p["uid"] != me or (IS_WINDOWS and not me):
            refused.append({"pid": pid, "name": p["name"], "why": "it isn't yours"})
            continue
        if IS_WINDOWS:
            if not _windows_close(pid, force):
                refused.append({"pid": pid, "name": p["name"], "why": "it has no window to close; it needs a force close"})
                continue
        else:
            import signal
            try:
                os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                refused.append({"pid": pid, "name": p["name"], "why": "it isn't yours"})
                continue
        sent.append({"pid": pid, "name": p["name"], "key": key})
    deadline = time.time() + 5
    while time.time() < deadline and any(_alive(s["pid"], s["key"]) for s in sent):
        time.sleep(0.25)
    closed = [s for s in sent if not _alive(s["pid"], s["key"])]
    still = [{"pid": s["pid"], "name": s["name"]} for s in sent if s not in closed]
    return {"closed": [{"pid": s["pid"], "name": s["name"]} for s in closed], "still_running": still,
            "refused": refused, "force": force}


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
            exe = os.path.basename((e.get("Exec") or "").split()[0]) if e.get("Exec") else ""
            apps.append({"name": e.get("Name"), "id": p.stem, "path": str(p), **({"exe": exe} if exe else {})})
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

# ---- checking it happened -------------------------------------------------------------------------
#
# What she tells him is what the computer can see happened, not what was asked for: a page she
# opened is checked among the browser's pages, an app among the running programs, going home by the
# pages left. "verified": False means it was asked for but couldn't be seen (yet), and she says so.

VERIFY_SECONDS = 8.0


def proc_names() -> dict[int, str]:
    """Running programs by pid: their names, lower case, without .exe."""
    if IS_WINDOWS:
        return {pid: str(r["name"]).lower().removesuffix(".exe") for pid, r in _windows_procs().items() if isinstance(pid, int)}
    out = {}
    for d in PROC.iterdir() if PROC.exists() else []:
        if d.name.isdigit():
            try:
                out[int(d.name)] = (d / "comm").read_text().strip().lower()
            except OSError:
                pass
    return out


def _started(app: dict, before: dict[int, str], wait: float = VERIFY_SECONDS) -> dict | None:
    """A program that wasn't running before and looks like this app (by its name or its executable):
    {"pid", "name"}. Linux keeps only 15 letters of a program's name, so the executable's are compared."""
    words = {w for w in re.findall(r"[a-z0-9]+", f"{app.get('name', '')} {app.get('id', '')}".lower()) if len(w) > 2}
    exe = str(app.get("exe") or "").lower().removesuffix(".exe")
    end = time.monotonic() + wait
    while True:
        for pid, name in proc_names().items():
            if pid in before and before[pid] == name:
                continue
            if exe and name in (exe, exe[:15]) or name in words or any(name.startswith(w) for w in words if len(w) > 3):
                return {"pid": pid, "name": name}
        if time.monotonic() > end:
            return None
        time.sleep(0.4)


def _shell_has(url: str, wait: float = VERIFY_SECONDS) -> dict | None:
    """The page among her shell's pages (same site), once the browser has it."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    end = time.monotonic() + wait
    while True:
        try:
            for t in SHELL.pages():
                h = (urllib.parse.urlsplit(t.get("url") or "").hostname or "").lower().removeprefix("www.")
                if h and (h == host or h.endswith("." + host) or host.endswith("." + h)):
                    return t
        except Exception:
            pass
        if time.monotonic() > end:
            return None
        time.sleep(0.4)


def do_open_url(args: dict) -> dict:
    url = str(args.get("url") or "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https pages")
    if SHELL and SHELL.running():
        SHELL.open(url)                                      # inside her shell, on top of her view
        page = _shell_has(url)
        if page is None:
            raise ValueError("the page didn't open on my screen")
        return {"url": url, "in": "shell", "verified": True, "title": str(page.get("title") or "")[:120]}
    if IS_WINDOWS:
        os.startfile(url)                                    # the default browser
    elif shutil.which("xdg-open"):
        spawn(["xdg-open", url])
    else:
        raise ValueError("there's no way to open a browser here")
    return {"url": url, "verified": False}                   # his own browser: I can't see its pages


def do_launch_app(args: dict) -> dict:
    name = " ".join(str(args.get("name") or "").split())
    if not name:
        raise ValueError("which app?")
    app = find_app(name)
    if app is None:
        raise ValueError(f"no app called {name} is installed")
    before = proc_names()
    if IS_WINDOWS:
        os.startfile(app["path"])
    elif shutil.which("gtk-launch"):
        spawn(["gtk-launch", app["id"]])
    elif shutil.which("gio"):
        spawn(["gio", "launch", app["path"]])
    else:
        raise ValueError("there's no way to launch apps here")
    running = _started(app, before)
    if running and SHELL and SHELL.running():
        remember_launched(running["pid"], running["name"])    # on her screen: "go home" closes it again
    return {"app": app["name"], "verified": bool(running), **({"process": running["name"]} if running else {})}


# Apps she opened on her shell's screen (a TV): pid and start time, so "go home" closes exactly those and
# nothing else (any other program there is closed only with his confirmation, like anywhere).

def launched_file() -> Path:
    return config_dir() / "launched.json"


def _start_key(pid: int) -> str | None:
    p = (_windows_procs() if IS_WINDOWS else _linux_procs()).get(pid)
    return str(p["start"]) if p else None


def read_launched() -> list[dict]:
    try:
        v = json.loads(launched_file().read_text())
    except (OSError, ValueError):
        return []
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def write_launched(items: list[dict]) -> None:
    try:
        f = launched_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(items[-20:]))
    except OSError:
        pass


def remember_launched(pid: int, name: str) -> None:
    key = _start_key(pid)
    if key:
        write_launched([x for x in read_launched() if _alive(int(x["pid"]), str(x["key"]))] +
                       [{"pid": pid, "key": key, "name": name}])


def close_launched(wait: float = 4.0) -> tuple[list[str], list[str]]:
    """Closes the apps she opened here that are still running: (closed, still running)."""
    import signal
    mine = [x for x in read_launched() if _alive(int(x["pid"]), str(x["key"]))]
    for x in mine:
        try:
            os.kill(int(x["pid"]), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    end = time.monotonic() + wait
    while time.monotonic() < end and any(_alive(int(x["pid"]), str(x["key"])) for x in mine):
        time.sleep(0.2)
    still = [x for x in mine if _alive(int(x["pid"]), str(x["key"]))]
    write_launched(still)
    return [x["name"] for x in mine if x not in still], [x["name"] for x in still]


def do_list_apps(args: dict) -> dict:
    return {"apps": [{"name": a["name"], "id": a["id"]} for a in installed_apps()]}


def do_info(args: dict) -> dict:
    return machine_info()


def do_home(args: dict) -> dict:
    """Back to her: her player stops (it's on top of everything) and the pages she opened close."""
    stopped = None
    if player_busy():
        stopped = (player().control("stop", {}).get("last") or {}).get("title") or "the video"
    if not (SHELL and SHELL.running()):
        if stopped:
            return {"closed": 0, "stopped": stopped, "verified": not player_busy()}
        raise ValueError("my shell isn't running on this computer")
    closed = SHELL.home()
    apps_closed, apps_left = close_launched() if not IS_WINDOWS else ([], [])
    end = time.monotonic() + 3
    while True:                                               # the browser takes a moment to close them
        left = [t for t in SHELL.pages() if not SHELL.is_hers(t)]
        if not left or time.monotonic() > end:
            break
        time.sleep(0.25)
    if left or player_busy() or apps_left:
        raise ValueError("something is still on top of me there (" + (str(left[0].get("title") or "a page") if left else
                         apps_left[0] if apps_left else "my player") + ")")
    return {"closed": closed, "verified": True, **({"stopped": stopped} if stopped else {}),
            **({"apps": apps_closed} if apps_closed else {})}


# ---- power and presence ---------------------------------------------------------------------
#
# Restart, shut down, sleep, lock, screen off/on, idle time, and waking another computer. Fixed
# programs by argument list (no shell). Restart and shutdown are always confirmed on the server
# (change tier) and happen a few seconds after the answer, so the answer gets out first. On Linux
# they go through logind ("-i": a desktop that's holding the shutdown back doesn't hang it); the
# installer's --system step lets this account do that without a password, keeps a stuck service
# from holding a shutdown for minutes, and arms the hardware watchdog in case it still hangs.

MAC = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.I)
POWER_ACTIONS = ("restart", "shutdown", "sleep", "lock", "screen_off", "screen_on", "idle")


def _default_iface() -> str | None:
    for x in _read(PROC / "net" / "route").splitlines()[1:]:
        f = x.split()
        if len(f) > 1 and f[1] == "00000000":
            return f[0]
    return None


def wake_address() -> dict:
    """This computer's MAC address and LAN broadcast address, so another can wake it."""
    if IS_WINDOWS:
        out = _run(["getmac", "/fo", "csv", "/nh", "/v"])
        for line in out.splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) >= 3 and MAC.match(cols[2].replace("-", ":")) and "disconnected" not in line.lower():
                return {"mac": cols[2].replace("-", ":").lower()}
        return {}
    iface = _default_iface()
    if not iface:
        return {}
    mac = _read(SYS / "class" / "net" / iface / "address").strip().lower()
    brd = None
    m = re.search(r"brd (\d+\.\d+\.\d+\.\d+)", _run(["ip", "-o", "-4", "addr", "show", "dev", iface]))
    if m:
        brd = m.group(1)
    return {"mac": mac if MAC.match(mac) and mac != "00:00:00:00:00:00" else None, "broadcast": brd, "iface": iface}


def _run_env(argv: list[str], timeout: float = 6) -> tuple[int, str, str]:
    """Like _run_full, inside the graphical session (for locking and the screen)."""
    exe = shutil.which(argv[0])
    if not exe:
        return 127, "", ""
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, env=session_env())
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def _logind_can(what: str) -> str:
    """'yes', 'challenge' (needs a password), 'no' or 'na', from logind (CanReboot, CanPowerOff, CanSuspend)."""
    code, out, _ = _run_full(["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                              "org.freedesktop.login1.Manager", f"Can{what}"])
    m = re.search(r'"(\w+)"', out)
    return m.group(1) if code == 0 and m else "unknown"


def _later(seconds: float, argv: list[str]) -> None:
    """Run it a moment from now, so this answer reaches Aurelia first."""
    def go():
        time.sleep(seconds)
        try:
            subprocess.run(argv, timeout=30, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception as exc:
            log("power command failed:", repr(exc))
    threading.Thread(target=go, daemon=True).start()


def idle_seconds() -> int | None:
    if IS_WINDOWS:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return int((ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000)
        return None
    code, out, _ = _run_env(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor", "--object-path",
                             "/org/gnome/Mutter/IdleMonitor/Core", "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
    m = re.search(r"uint64 (\d+)", out)
    if code == 0 and m:
        return int(m.group(1)) // 1000
    return None


def _lock() -> str:
    if SHELL and SHELL.state() == "on":
        return "Nothing to lock: this screen shows only me."
    if IS_WINDOWS:
        ctypes.windll.user32.LockWorkStation()
        return "Locked."
    code, _, _ = _run_env(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
                           "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.Lock"])
    if code == 0:
        return "Locked."
    code, out, _ = _run_full(["loginctl", "list-sessions", "--no-legend"])
    me = getpass_user()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[2] == me and _run_full(["loginctl", "lock-session", f[0]])[0] == 0:
            return "Locked."
    raise ValueError("I couldn't lock this computer's screen.")


def getpass_user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return ""


def _screen(on: bool) -> str:
    if IS_WINDOWS:
        if on:
            ctypes.windll.user32.mouse_event(0x0001, 0, 1, 0, 0)          # a tiny nudge wakes the display
        else:
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)  # monitor power off
        return "Screen on." if on else "Screen off."
    env = session_env()
    tries = []
    if env.get("WAYLAND_DISPLAY"):
        tries.append(["wlopm", "--on" if on else "--off", "*"])                       # Cage and wlroots
        tries.append(["busctl", "--user", "set-property", "org.gnome.Mutter.DisplayConfig",
                      "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode",
                      "i", "0" if on else "3"])                                        # GNOME on Wayland
    if env.get("DISPLAY"):
        tries.append(["xset", "dpms", "force", "on" if on else "off"])
    for argv in tries:
        if _run_env(argv)[0] == 0:
            return "Screen on." if on else "Screen off."
    raise ValueError("I couldn't turn this screen " + ("on" if on else "off") +
                     " (on my shell this needs wlopm: run the installer again with --system).")


def do_power(args: dict) -> dict:
    action = str(args.get("action") or "")
    if action not in POWER_ACTIONS:
        raise ValueError("power actions: " + ", ".join(POWER_ACTIONS))
    if action == "idle":
        s = idle_seconds()
        return {"idle_s": s, "said": "Idle time isn't available here." if s is None else None}
    if action == "lock":
        return {"said": _lock()}
    if action in ("screen_off", "screen_on"):
        return {"said": _screen(action == "screen_on")}
    if action in ("restart", "shutdown") and args.get("confirmed") is not True:
        raise ValueError("a restart or shutdown needs his confirmation")
    if IS_WINDOWS:
        argv = {"restart": ["shutdown", "/r", "/t", "5", "/c", "Aurelia: restarting"],
                "shutdown": ["shutdown", "/s", "/t", "5", "/c", "Aurelia: shutting down"],
                "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}[action]
        _later(1, argv)
        return {"said": {"restart": "Restarting in a few seconds.", "shutdown": "Shutting down in a few seconds.",
                         "sleep": "Going to sleep."}[action], "in_s": 5}
    can = _logind_can({"restart": "Reboot", "shutdown": "PowerOff", "sleep": "Suspend"}[action])
    if can == "challenge":
        raise ValueError("this computer wants a password for that; run the installer again with --system "
                         "(one-time sudo) so I can " + {"restart": "restart", "shutdown": "shut down",
                                                         "sleep": "suspend"}[action] + " it")
    if can != "yes":                                    # can't tell, or it says no: never try blind
        raise ValueError(f"this computer says it can't {action.replace('shutdown', 'shut down')} right now ({can})"
                         if can in ("no", "na") else
                         f"I couldn't check whether I'm allowed to {action.replace('shutdown', 'shut down')} this computer")
    verb = {"restart": "reboot", "shutdown": "poweroff", "sleep": "suspend"}[action]
    _later(3, ["systemctl", verb, "-i"])
    return {"said": {"restart": "Restarting in a few seconds.", "shutdown": "Shutting down in a few seconds.",
                     "sleep": "Going to sleep in a few seconds."}[action], "in_s": 3}


def do_wake(args: dict) -> dict:
    """Send a wake-on-LAN packet for another computer on this network."""
    mac = str(args.get("mac") or "").strip().lower().replace("-", ":")
    if not MAC.match(mac):
        raise ValueError("that isn't a MAC address")
    packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16
    targets = ["255.255.255.255"]
    brd = str(args.get("broadcast") or "")
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", brd):
        targets.insert(0, brd)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in targets:
            for port in (9, 7):
                try:
                    sock.sendto(packet, (addr, port))
                except OSError:
                    pass
    return {"sent": True, "to": targets}


# ---- sound and media players ------------------------------------------------------------------
#
# The computer's own volume, mute and audio output, and whatever media player is running (Spotify,
# a browser tab, VLC): play, pause, next, previous, what's playing. Linux: PipeWire's wpctl (or
# PulseAudio's pactl) and MPRIS through playerctl; Windows: the media and volume keys. By argument
# list, inside the user's session; nothing else.

SOUND_ACTIONS = ("get", "set", "up", "down", "mute", "unmute", "toggle", "outputs", "output")
PLAYER_ACTIONS = ("play", "pause", "toggle", "next", "previous", "status")
VK = {"up": 0xAF, "down": 0xAE, "mute": 0xAD, "next": 0xB0, "previous": 0xB1, "toggle": 0xB3}


def _key(vk: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


def parse_wpctl(out: str) -> list[dict]:
    """Audio outputs from `wpctl status` (the Sinks list under Audio), with the default marked."""
    outs, in_audio, in_sinks = [], False, False
    for line in out.splitlines():
        if line and not line[0].isspace():                  # a top-level section: PipeWire, Audio, Video, Settings
            in_audio, in_sinks = line.startswith("Audio"), False
            continue
        if not in_audio:
            continue
        bare = line.strip(" │├└─\t")
        if re.match(r"^[A-Za-z][\w ]*:$", bare):            # a list heading: Devices:, Sinks:, Sink endpoints:…
            in_sinks = bare == "Sinks:"
            continue
        m = re.match(r"^(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[vol:.*\])?$", bare) if in_sinks else None
        if m:
            outs.append({"id": m.group(2), "name": m.group(3).strip(), "default": bool(m.group(1))})
    return outs


def _wpctl_outputs() -> list[dict]:
    return parse_wpctl(_run_env(["wpctl", "status"])[1])


def _pactl_outputs() -> list[dict]:
    _, out, _ = _run_env(["pactl", "list", "short", "sinks"])
    _, default, _ = _run_env(["pactl", "get-default-sink"])
    _, long, _ = _run_env(["pactl", "list", "sinks"])
    names = dict(re.findall(r"Name: (\S+)\s+Description: (.+)", long))
    return [{"id": x.split()[1], "name": names.get(x.split()[1], x.split()[1]), "default": x.split()[1] == default.strip()}
            for x in out.splitlines() if len(x.split()) > 1]


def _sound_state() -> dict:
    if shutil.which("wpctl"):
        _, out, _ = _run_env(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        m = re.search(r"Volume:\s*([\d.]+)", out)
        outs = _wpctl_outputs()
        return {"volume": round(float(m.group(1)) * 100) if m else None, "muted": "MUTED" in out,
                "output": next((o["name"] for o in outs if o["default"]), None), "outputs": outs}
    if shutil.which("pactl"):
        _, vol, _ = _run_env(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        _, mute, _ = _run_env(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        m = re.search(r"(\d+)%", vol)
        outs = _pactl_outputs()
        return {"volume": int(m.group(1)) if m else None, "muted": "yes" in mute,
                "output": next((o["name"] for o in outs if o["default"]), None), "outputs": outs}
    raise ValueError("I can't reach this computer's sound (no PipeWire or PulseAudio tools here)")


def do_sound(args: dict) -> dict:
    action = str(args.get("action") or "get")
    if action not in SOUND_ACTIONS:
        raise ValueError("sound actions: " + ", ".join(SOUND_ACTIONS))
    level = args.get("level")
    if IS_WINDOWS:
        if action in ("up", "down"):
            _key(VK[action], 5)                             # about 10%
        elif action in ("mute", "unmute", "toggle"):
            _key(VK["mute"])                                # Windows only toggles
        elif action == "set":
            lv = max(0, min(100, int(float(level or 0))))
            _key(VK["down"], 50)
            _key(VK["up"], round(lv / 2))
        else:
            raise ValueError("on Windows I can turn it up, down, to a level, or mute it, but not read it yet")
        return {"windows": True}
    wp = shutil.which("wpctl")
    sink = "@DEFAULT_AUDIO_SINK@" if wp else "@DEFAULT_SINK@"
    if action == "set":
        try:
            lv = max(0, min(100, int(float(level))))
        except (TypeError, ValueError):
            raise ValueError("set the volume to what? (0-100)")
        argv = ["wpctl", "set-volume", sink, f"{lv / 100:.2f}"] if wp else ["pactl", "set-sink-volume", sink, f"{lv}%"]
    elif action in ("up", "down"):
        step = int(float(level or 10))
        sign = "+" if action == "up" else "-"
        argv = ["wpctl", "set-volume", "-l", "1.0", sink, f"{step}%{sign}"] if wp else \
            ["pactl", "set-sink-volume", sink, f"{sign}{step}%"]
    elif action in ("mute", "unmute", "toggle"):
        v = {"mute": "1", "unmute": "0", "toggle": "toggle"}[action]
        argv = ["wpctl", "set-mute", sink, v] if wp else ["pactl", "set-sink-mute", sink, v]
    elif action == "output":
        want = " ".join(str(args.get("device") or "").lower().split())
        if not want:
            raise ValueError("which output? (say part of its name, e.g. 'HDMI' or 'headphones')")
        outs = _sound_state()["outputs"]
        hits = [o for o in outs if all(w in o["name"].lower() for w in want.split())]
        if len(hits) != 1:
            raise ValueError(("Which one: " if hits else "No output like that. Outputs: ") +
                             ", ".join(o["name"] for o in (hits or outs)))
        argv = ["wpctl", "set-default", hits[0]["id"]] if wp else ["pactl", "set-default-sink", hits[0]["id"]]
    else:
        argv = None
    if argv:
        code, _, err = _run_env(argv)
        if code != 0:
            raise ValueError(f"the sound system said no ({_clean(err) or code})")
    return _sound_state()


PLAYER_FORMAT = ("{{playerName}}\t{{status}}\t{{artist}}\t{{title}}\t{{album}}\t{{duration(position)}}\t"
                 "{{duration(mpris:length)}}")


def _players() -> list[dict]:
    return parse_players(_run_env(["playerctl", "-a", "metadata", "--format", PLAYER_FORMAT])[1])


def parse_players(out: str) -> list[dict]:
    """playerctl's lines, playing first, then paused."""
    found = []
    for line in out.splitlines():
        f = (line.split("\t") + [""] * 7)[:7]
        if f[0]:
            found.append({"player": f[0], "status": f[1], "artist": f[2], "title": f[3], "album": f[4],
                          "position": f[5], "length": f[6]})
    return sorted(found, key=lambda p: {"Playing": 0, "Paused": 1}.get(p["status"], 2))


def do_player(args: dict) -> dict:
    action = str(args.get("action") or "status")
    if player_busy() and action in PLAYER_STEP_ACTIONS:      # her own player, when it's playing
        p = player()
        return {"player": p.status() if action == "status" else p.control(action, args)}
    if action in PLAYER_STEP_ACTIONS and action not in PLAYER_ACTIONS:
        raise ValueError("nothing of mine is playing here")
    if action not in PLAYER_ACTIONS:
        raise ValueError("player actions: " + ", ".join(PLAYER_ACTIONS))
    if IS_WINDOWS:
        if action == "status":
            raise ValueError("I can't see what's playing on Windows yet")
        _key(VK["toggle" if action in ("play", "pause", "toggle") else action])
        return {"windows": True}
    if not shutil.which("playerctl"):
        if action == "status":                                  # mine isn't playing, and I can't see the others
            return {"player": {"active": False}}
        raise ValueError("I can't reach media players here (playerctl isn't installed: run the installer with --system)")
    players = _players()
    if not players:
        return {"players": [], "said": "Nothing is playing."}
    if action != "status":
        verb = {"toggle": "play-pause"}.get(action, action)
        code, _, err = _run_env(["playerctl", "-p", players[0]["player"], verb])
        if code != 0:
            raise ValueError(f"{players[0]['player']} said no ({_clean(err) or code})")
        time.sleep(0.4)
        players = _players()
    return {"players": players[:5]}


# ---- her player ----------------------------------------------------------------------------------
#
# Video and music that Aurelia plays here go to her own player (mpv), not a web page: she hands it
# links to her own server (Jellyfin through Aurelia, never anything else), and it tells her exactly
# what it's doing (title, position, paused, volume, subtitles) over its control socket. Full
# screen on top of her shell while it plays; her shell is back when it stops. It reports its state
# to Aurelia every few seconds while it plays (so "what's playing" and resume points are exact).

SERVER_URL: str = ""
NODE_KEY: str = ""
PLAYER_STEP_ACTIONS = ("play", "pause", "toggle", "stop", "forward", "back", "seek", "restart", "volume", "mute",
                       "unmute", "next", "previous", "subtitles", "audio", "speed", "status")


def mpv_socket() -> str:
    if IS_WINDOWS:
        return r"\\.\pipe\aurelia-mpv"
    run = os.environ.get("XDG_RUNTIME_DIR") or session_env().get("XDG_RUNTIME_DIR")
    base = Path(run) if run and Path(run).is_dir() else config_dir()
    path = str(base / "aurelia-mpv.sock")
    if len(path) > 100:                                   # a socket's address is limited to about 108 bytes
        d = Path("/tmp") / f"aurelia-{os.getuid()}"
        d.mkdir(mode=0o700, exist_ok=True)
        path = str(d / "mpv.sock")
    return path


def find_mpv() -> str | None:
    forced = os.environ.get("AURELIA_MPV")                     # tests: a path, or "none"
    if forced:
        return None if forced == "none" else forced
    exe = shutil.which("mpv")
    if exe or not IS_WINDOWS:
        return exe
    for root in (os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")):
        for sub in ("mpv\\mpv.exe", "Programs\\mpv\\mpv.exe"):
            if root and Path(root, sub).exists():
                return str(Path(root, sub))
    return None


class Mpv:
    """Her player: one mpv process, idle (no window) until it has something to play."""

    def __init__(self, path: str | None = None):
        self.path = path or mpv_socket()
        self.proc: subprocess.Popen | None = None
        self.items: list[dict] = []
        self.last: dict = {}
        self.next_id = 0
        self.lock = threading.Lock()
        self.reporting = False
        self.ended: dict | None = None                                  # where the one it just left got to

    def argv(self, exe: str) -> list[str]:
        extra = os.environ.get("AURELIA_MPV_ARGS", "").split()          # tests: --vo=null --ao=null
        return [exe, "--idle=yes", "--force-window=no", "--fs", "--no-terminal", "--really-quiet", "--no-config",
                "--load-scripts=no", "--ytdl=no", "--hwdec=auto-safe", "--keep-open=no", "--audio-display=no",
                "--title=Aurelia", f"--input-ipc-server={self.path}", *extra]

    def running(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        try:                                                  # one left playing by the agent before an update
            self.command("get_property", "idle-active")
            return True
        except (OSError, ValueError):
            return False

    def start(self) -> None:
        if self.running():
            return
        exe = find_mpv()
        if not exe:
            raise ValueError("I can't play here yet: my player (mpv) isn't installed (run the installer with --system)")
        if not IS_WINDOWS:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        kw = {} if IS_WINDOWS else {"start_new_session": True}
        self.proc = subprocess.Popen(self.argv(exe), env=session_env(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                self.command("get_property", "idle-active")
                return
            except (OSError, ValueError):
                if not self.running():
                    break
                time.sleep(0.15)
        raise ValueError("my player didn't start here")

    def _open(self):
        if IS_WINDOWS:
            return open(self.path, "r+b", buffering=0)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(self.path)
        return s.makefile("rwb", buffering=0), s

    def command(self, *args):
        """A command by position, or one dict with named arguments (loadfile with per-file options:
        the same on old and new mpv, whose positional loadfile arguments differ)."""
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            opened = self._open()
            f, sock = opened if isinstance(opened, tuple) else (opened, None)
            try:
                cmd = args[0] if len(args) == 1 and isinstance(args[0], dict) else list(args)
                f.write((json.dumps({"command": cmd, "request_id": rid}) + "\n").encode())
                while True:
                    line = f.readline()
                    if not line:
                        raise OSError("the player closed its control socket")
                    msg = json.loads(line)
                    if msg.get("request_id") == rid:
                        if msg.get("error") not in (None, "success"):
                            raise ValueError(str(msg["error"]))
                        return msg.get("data")
            finally:
                f.close()
                if sock:
                    sock.close()

    def get(self, prop: str, default=None):
        try:
            v = self.command("get_property", prop)
            return default if v is None else v
        except (OSError, ValueError):
            return default

    def active(self) -> bool:
        return self.running() and not self.get("idle-active", True)

    def status(self) -> dict:
        if not self.active():
            return {"active": False, **({"last": self.last} if self.last else {})}
        pos = int(self.get("playlist-pos", 0) or 0)
        it = self.items[pos] if 0 <= pos < len(self.items) else {}
        subs = [t for t in (self.get("track-list", []) or []) if isinstance(t, dict) and t.get("type") == "sub"]
        on = next((t for t in subs if t.get("selected")), None)
        dur = self.get("duration")
        st = {"active": True, "title": it.get("title") or self.get("media-title", ""), "item": it.get("item"),
              "kind": it.get("kind") or "video", "position": round(float(self.get("time-pos", 0) or 0), 1),
              "duration": round(float(dur), 1) if dur else None, "paused": bool(self.get("pause", False)),
              "volume": round(float(self.get("volume", 0) or 0)), "muted": bool(self.get("mute", False)),
              "index": pos, "count": len(self.items),
              "subtitles": ((on.get("lang") or on.get("title") or "on") if on else "off") if subs else None,
              "next": self.items[pos + 1].get("title") if pos + 1 < len(self.items) else None}
        self.last = {k: st[k] for k in ("title", "item", "kind", "position", "duration")}
        return st

    def _load(self, it: dict, flags: str) -> None:
        if it.get("opts"):
            self.command({"name": "loadfile", "url": it["url"], "flags": flags, "options": it["opts"]})
        else:
            self.command("loadfile", it["url"], flags)

    def play(self, items: list[dict], start: float = 0.0, header: str | None = None) -> dict:
        self.start()
        # Always set: my key goes only with streams from my own server, never to anyone else's.
        self.command("set_property", "http-header-fields", [header] if header else [])
        self.items, self.last = items, {}
        self.command("set_property", "pause", False)
        self._load(items[0], "replace")
        for it in items[1:]:
            self._load(it, "append")
        deadline, seen_busy = time.monotonic() + 20, False
        while time.monotonic() < deadline:                    # confirmed from the player itself
            idle = self.get("idle-active", True)
            seen_busy = seen_busy or not idle
            if not idle and (self.get("duration") or self.get("time-pos") is not None):
                break
            if seen_busy and idle:
                raise ValueError("my player couldn't open it (the stream may be unavailable)")
            time.sleep(0.2)
        else:
            raise ValueError("my player didn't start playing it within 20 seconds")
        if start and start > 5:
            self.command("seek", float(start), "absolute+exact")
        time.sleep(0.2)
        return self.status()

    def control(self, action: str, args: dict) -> dict:
        secs = float(args.get("seconds") or 0)
        if action == "play":
            self.command("set_property", "pause", False)
        elif action == "pause":
            self.command("set_property", "pause", True)
        elif action == "toggle":
            self.command("cycle", "pause")
        elif action == "stop":
            last = self.status()
            self.command("stop")
            self.items = []
            return {"active": False, "stopped": True, "last": {k: last.get(k) for k in ("title", "item", "kind", "position", "duration")}}
        elif action in ("forward", "back"):
            self.command("seek", (secs or 10) * (1 if action == "forward" else -1), "relative+exact")
        elif action == "seek":
            self.command("seek", secs, "absolute+exact")
        elif action == "restart":
            self.command("seek", 0, "absolute")
        elif action == "volume":
            self.command("set_property", "volume", max(0, min(130, float(args.get("level") or 0))))
        elif action in ("mute", "unmute"):
            self.command("set_property", "mute", action == "mute")
        elif action in ("next", "previous"):
            cur = self.status()
            self.ended = {k: cur.get(k) for k in ("title", "item", "kind", "position", "duration")}
            try:
                self.command("playlist-next" if action == "next" else "playlist-prev", "weak")
            except ValueError:
                raise ValueError("that's the last one" if action == "next" else "that's the first one")
        elif action == "speed":
            self.command("set_property", "speed", max(0.25, min(4.0, float(args.get("rate") or 1))))
        elif action in ("subtitles", "audio"):
            self._track("sub" if action == "subtitles" else "audio", str(args.get("which") or "next").lower().strip())
        elif action != "status":
            raise ValueError("my player can " + ", ".join(PLAYER_STEP_ACTIONS))
        time.sleep(0.35)
        return self.status()

    def _track(self, kind: str, which: str) -> None:
        prop = "sid" if kind == "sub" else "aid"
        if which in ("off", "none", "no") and kind == "sub":
            self.command("set_property", prop, "no")
            return
        tracks = [t for t in (self.get("track-list", []) or []) if isinstance(t, dict) and t.get("type") == kind]
        if not tracks:
            raise ValueError("this has no subtitles" if kind == "sub" else "this has only one audio track")
        if which in ("on", "next", ""):
            if which == "on" and any(t.get("selected") for t in tracks):
                return
            self.command("cycle", prop)
            return
        want = [t for t in tracks if which in str(t.get("lang") or "").lower() or which in str(t.get("title") or "").lower()
                or {"english": "en", "japanese": "ja", "spanish": "es"}.get(which, "\x00") == str(t.get("lang") or "")[:2]]
        if not want:
            raise ValueError("no track like that; it has " + ", ".join(str(t.get("lang") or t.get("title") or t.get("id")) for t in tracks))
        self.command("set_property", prop, want[0]["id"])

    def report(self) -> None:
        """While something plays: its state to Aurelia every few seconds, and once more when it stops."""
        if self.reporting or not SERVER_URL:
            return
        self.reporting = True

        def go():
            try:
                while True:
                    try:
                        st = self.status()
                    except Exception:
                        st = {"active": False}
                    ended, self.ended = self.ended, None
                    if ended and st.get("active") and ended.get("item") != st.get("item"):
                        st = {**st, "last": ended}
                    try:
                        call(SERVER_URL, "/nodes/player/state", st, NODE_KEY, timeout=10)
                    except Revoked:
                        return
                    except (OSError, RuntimeError, ValueError):
                        pass
                    if not st.get("active"):
                        return
                    for _ in range(20):                       # every 10 s; sooner when it moves on
                        time.sleep(0.5)
                        if not self.active() or int(self.get("playlist-pos", 0) or 0) != st.get("index"):
                            break
            finally:
                self.reporting = False
        threading.Thread(target=go, daemon=True).start()


PLAYER: Mpv | None = None


def player() -> Mpv:
    global PLAYER
    if PLAYER is None:
        PLAYER = Mpv()
    return PLAYER


def player_busy() -> bool:
    return PLAYER is not None and PLAYER.active()


def do_play(args: dict) -> dict:
    """Play streams from Aurelia's own server (/nodes/media/<ticket>), in order, full screen; start:
    seconds into the first. Or one YouTube video by its id, resolved here (with this computer's own
    YouTube sign-in) into YouTube's own streams. Nothing else: no other address, no files, no scripts."""
    raw = args.get("items")
    if not isinstance(raw, list) or not raw:
        raise ValueError("nothing to play")
    if raw[0].get("youtube"):
        it = youtube_item(str(raw[0]["youtube"]), str(raw[0].get("title") or ""))
        p = player()
        st = p.play([it], float(args.get("start") or 0), None)        # no header: my key never goes to Google
        p.report()
        return {"player": st}
    items = []
    for it in raw[:500]:
        path = str((it or {}).get("path") or "")
        if not SERVER_URL or not re.fullmatch(r"/nodes/media/[0-9a-f]{32}", path):
            raise ValueError("I only play what Aurelia sends from her own server")
        items.append({"url": SERVER_URL.rstrip("/") + path, "title": str(it.get("title") or "")[:200], "item": str(it.get("item") or "")[:64],
                      "kind": "audio" if it.get("kind") == "audio" else "video"})
    p = player()
    st = p.play(items, float(args.get("start") or 0), f"Authorization: Bearer {NODE_KEY}" if NODE_KEY else None)
    p.report()
    return {"player": st}


# ---- YouTube (Quasar step 3) -------------------------------------------------------------------------
#
# yt-dlp reads YouTube for her: his feeds (home, subscriptions, Watch Later) and search, with this
# computer's own YouTube sign-in (the cookies in her shell's browser profile, read here and never sent
# anywhere), and the streams for a video, which her player plays. yt-dlp itself comes from Aurelia's
# release, pinned and tested there, fetched with this computer's key and checked against her checksum
# before it's used; it runs as a Python package from that one file, never as a downloaded program.
# YouTube needs a JavaScript runtime beside it: Deno, or Node 20 or newer (Ubuntu's nodejs package).

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_FEEDS = {"home": ":ytrec", "subscriptions": ":ytsubs", "watch_later": ":ytwatchlater", "history": ":ythistory"}
YT_FORMAT = "bv*[height<=1080][vcodec!^=av01]+ba/b[height<=1080]/bv*+ba/b"


def ytdlp_bundle() -> Path:
    """Aurelia's yt-dlp, here: the one she announces, checked against her checksum."""
    if not SERVER_URL:
        raise ValueError("I'm not connected to Aurelia")
    meta = call(SERVER_URL, "/nodes/ytdlp", key=NODE_KEY, timeout=15)
    sha, version = str(meta.get("sha256") or ""), str(meta.get("version") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("Aurelia didn't say which yt-dlp to use")
    d = config_dir() / "ytdlp"
    f = d / f"yt-dlp-{sha[:16]}.zip"
    if f.exists() and hashlib.sha256(f.read_bytes()).hexdigest() == sha:
        return f
    req = urllib.request.Request(SERVER_URL.rstrip("/") + "/nodes/ytdlp.zip",
                                 headers={"User-Agent": f"aurelia-node/{VERSION}", "Authorization": f"Bearer {NODE_KEY}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("the yt-dlp download didn't match Aurelia's checksum, so I didn't use it")
    d.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, f)
    for old in d.glob("yt-dlp-*.zip"):                        # only the one in use is kept
        if old != f:
            try:
                old.unlink()
            except OSError:
                pass
    log(f"yt-dlp {version} ready")
    return f


def js_runtime() -> list[str]:
    """yt-dlp's JavaScript runtime for YouTube: Deno, else Node 20+."""
    if shutil.which("deno"):
        return []                                              # yt-dlp's default
    node = shutil.which("node") or shutil.which("nodejs")
    if node:
        m = re.match(r"v?(\d+)", _run([node, "--version"]).strip())
        if m and int(m.group(1)) >= 20:
            return ["--js-runtimes", f"node:{node}"]
    raise ValueError("YouTube needs Node.js here (Ubuntu's nodejs package, version 20 or newer)")


def youtube_cookies() -> list[str]:
    """This computer's YouTube sign-in: the cookies in her shell's browser profile (read in place)."""
    if IS_WINDOWS:
        return []
    browser = find_browser() or ""
    prof = (Path.home() / "snap" / "chromium" / "common" / "aurelia-shell") if "/snap/" in browser \
        else config_dir() / "shell-profile"
    if not (prof / "Default" / "Cookies").exists():
        return []
    name = "chrome" if "google-chrome" in browser else "edge" if "edge" in browser else "chromium"
    return ["--cookies-from-browser", f"{name}:{prof}"]


def ytdlp(args: list[str], timeout: float = 90) -> str:
    bundle = ytdlp_bundle()
    env = {**session_env(), "PYTHONPATH": str(bundle)}
    argv = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--no-progress",
            *youtube_cookies(), *args]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise ValueError("YouTube took too long to answer")
    if r.returncode != 0:
        err = " ".join((r.stderr or "").split())
        if re.search(r"sign in|log ?in|cookies|account", err, re.I):
            raise ValueError("YouTube wants a sign-in for that, and this computer isn't signed in to YouTube "
                             "(sign in once in my shell's browser here)")
        raise ValueError("YouTube said no: " + (err.removeprefix("ERROR: ")[:200] or f"exit {r.returncode}"))
    return r.stdout


def _yt_entry(e: dict) -> dict | None:
    vid = str(e.get("id") or "")
    if not YT_ID.match(vid):
        return None                                            # channels, playlists, shorts shelves: not a video
    out = {"id": vid, "title": str(e.get("title") or "")[:200], "channel": str(e.get("channel") or e.get("uploader") or "")[:100]}
    if e.get("duration"):
        out["seconds"] = int(float(e["duration"]))
    if e.get("view_count"):
        out["views"] = int(e["view_count"])
    if e.get("upload_date"):
        out["date"] = str(e["upload_date"])[:8]
    if e.get("live_status") == "is_live":
        out["live"] = True
    return out


def do_youtube_feed(args: dict) -> dict:
    """His YouTube: home (recommended), subscriptions, watch_later, history, or a search."""
    feed = str(args.get("feed") or "home")
    n = max(1, min(60, int(args.get("limit") or 30)))
    if feed == "search":
        q = " ".join(str(args.get("query") or "").split())[:200]
        if not q:
            raise ValueError("search YouTube for what?")
        url = f"ytsearch{n}:{q}"
    elif feed in YT_FEEDS:
        url = YT_FEEDS[feed]
    else:
        raise ValueError("I can read your YouTube home, subscriptions, Watch Later, history, or a search")
    out = json.loads(ytdlp(["--flat-playlist", "-J", "--playlist-end", str(n), url], timeout=120) or "{}")
    items = [x for x in (_yt_entry(e) for e in out.get("entries") or [] if isinstance(e, dict)) if x]
    return {"feed": feed, "items": items[:n], "signed_in": bool(youtube_cookies())}


def youtube_item(vid: str, title: str = "") -> dict:
    """One video's streams for her player (best up to 1080p), from YouTube itself."""
    if not YT_ID.match(vid):
        raise ValueError("that isn't a YouTube video id")
    info = json.loads(ytdlp([*js_runtime(), "-f", YT_FORMAT, "-J", "--no-playlist",
                             f"https://www.youtube.com/watch?v={vid}"], timeout=120) or "{}")
    fmts = info.get("requested_formats") or ([info] if info.get("url") else [])
    if not fmts or not fmts[0].get("url"):
        raise ValueError("YouTube didn't give me a stream for that video")
    ua = str((fmts[0].get("http_headers") or {}).get("User-Agent") or "")
    opts = {"force-media-title": (title or str(info.get("title") or ""))[:200]}
    if ua:
        opts["user-agent"] = ua
    if len(fmts) > 1 and fmts[1].get("url"):
        opts["audio-files-append"] = fmts[1]["url"]             # YouTube sends picture and sound separately
    return {"url": fmts[0]["url"], "opts": opts, "title": opts["force-media-title"], "item": f"yt:{vid}",
            "kind": "video"}


# ---- a Spotify speaker (Quasar step 4) ---------------------------------------------------------------
#
# This computer as a Spotify Connect speaker named after it (librespot, from the system's own
# packages). Aurelia doesn't play Spotify herself: she tells Spotify what to play on this speaker
# through Spotify's own API and reads back what Spotify says. The first time, he picks this speaker
# once in the Spotify app on his phone (it's found on the home network); librespot then keeps that
# sign-in in its own cache here (only this account can read it) and comes back signed in after a
# restart, so Aurelia can start music on it from then on. Nothing about it leaves this computer.

class Speaker:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.name = ""
        self.starts: list[float] = []
        self.lock = threading.Lock()

    def cache(self) -> Path:
        d = config_dir() / "spotify"
        d.mkdir(parents=True, exist_ok=True)
        if not IS_WINDOWS:
            os.chmod(d, 0o700)
        return d

    def argv(self, exe: str, name: str) -> list[str]:
        return [exe, "--name", name, "--cache", str(self.cache()), "--disable-audio-cache", "--bitrate", "320",
                "--initial-volume", "70", "--enable-volume-normalisation",
                "--device-type", "tv" if (SHELL and SHELL.running()) else "computer"]

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, name: str) -> bool:
        with self.lock:
            if self.running() and self.name == name:
                return True
            if IS_WINDOWS:
                raise ValueError("on Windows, the Spotify app itself is the speaker: open it once on this computer")
            exe = shutil.which("librespot")
            if not exe:
                raise ValueError("librespot isn't installed here (it makes this computer a Spotify speaker)")
            now = time.time()
            self.starts = [t for t in self.starts if now - t < 120]
            if len(self.starts) >= 5:
                raise ValueError("my Spotify speaker keeps stopping here; check its log")
            if self.running():
                self.proc.terminate()
            self.starts.append(now)
            kw = {} if IS_WINDOWS else {"start_new_session": True}
            self.proc = subprocess.Popen(self.argv(exe, name), env=session_env(), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)
            self.name = name
            time.sleep(1.0)
            if not self.running():
                raise ValueError("my Spotify speaker didn't start here")
            log(f"Spotify speaker on as {name}")
            return True

    def supervise(self, name: str, stop: threading.Event) -> None:
        """Keeps it running (it's how Spotify finds this computer), quietly backing off if it can't."""
        while not stop.wait(15):
            if not self.running() and shutil.which("librespot") and current_mode() != "exam":
                try:
                    self.start(name)
                except ValueError as exc:
                    log("spotify speaker:", exc)


SPEAKER = Speaker()


def do_spotify_speaker(args: dict) -> dict:
    name = " ".join(str(args.get("name") or "").split())[:60] or socket.gethostname()
    SPEAKER.start(name)
    return {"speaker": name, "running": SPEAKER.running()}


# ---- her window on a workstation --------------------------------------------------------------------
#
# On his own computers, Aurelia is a window of her own, not a browser tab: an app window (Edge,
# Chrome or Chromium) with its own profile, paired once like any display (named after this computer,
# approved in the mailbox), so "send this to the desk" lands in it. The installer puts it in the
# Start menu / app list with a key: Ctrl+Alt+A opens it, or brings it to the front if it's open.

def _win_find_window(title: str) -> int | None:
    c, w, _ = _win()
    user32 = c.windll.user32
    found = []

    @c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def each(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = c.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            cls = c.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if buf.value == title and cls.value.startswith("Chrome_WidgetWin"):
                found.append(hwnd)
        return True
    user32.EnumWindows(each, 0)
    return found[0] if found else None


def window_token_file() -> Path:
    return config_dir() / "window"


def save_window_token(token: str) -> None:
    f = window_token_file()
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(token + "\n")
    if not IS_WINDOWS:
        os.chmod(f, 0o600)


def take_window_token() -> str | None:
    """The one-time link that signs her window in, approved with this computer; used once, then gone."""
    f = window_token_file()
    try:
        token = f.read_text().strip()
        f.unlink()
    except OSError:
        return None
    return token or None


def app_argv(browser: str, server: str, name: str, token: str | None = None) -> list[str]:
    q = urllib.parse.quote(name)
    first = f"/pair/window?t={urllib.parse.quote(token)}&app={q}" if token else f"/view?app={q}"
    return [browser, f"--app={server.rstrip('/')}{first}",
            f"--user-data-dir={config_dir() / 'app-profile'}", "--window-size=520,900", "--no-first-run",
            "--no-default-browser-check"]


def cmd_app() -> int:
    cfg, _ = load()
    if IS_WINDOWS:
        hwnd = _win_find_window("Aurelia")
        if hwnd:                                              # already open: to the front
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)                        # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return 0
    browser = find_browser()
    if not browser:
        print("Her window needs Edge, Chrome or Chromium.")
        return 1
    argv = app_argv(browser, cfg["server"], cfg.get("name") or socket.gethostname(), take_window_token())
    if IS_WINDOWS:
        subprocess.Popen(argv, close_fds=True, creationflags=0x00000008)   # DETACHED_PROCESS
    else:
        spawn(argv)
    return 0


# ---- modes: normal, gaming, exam ----------------------------------------------------------------
#
# Gaming: the agent stays connected but does nothing heavy (no screen work, no installs, no player,
# no GPU queries); only quick things (lock, sound, the clipboard, its mode) are answered. It turns on
# by itself while a game runs (from Steam, Epic, GOG, Riot, EA, Ubisoft or Battle.net folders) and off
# when the game closes, unless he set it himself.
# Exam: nothing of Aurelia runs on this computer at all. The agent stops, its autostart is switched
# off, and a one-time system timer brings it back when the exam is over. He can end it early on the
# computer itself (Start menu: "Aurelia - end exam mode", or `aurelia_node.py mode normal`).

MODES = ("normal", "gaming", "exam")
GAMING_OK = {"mode", "power", "sound", "player", "clipboard_get", "clipboard_set", "health", "spotify_speaker"}
GAME_DIRS = ("steamapps/common/", "epic games/", "gog galaxy/games/", "riot games/", "ea games/",
             "ubisoft game launcher/games/", "battle.net/", "xboxgames/")
MODE_CHANGED = threading.Event()
TASK_NAME = "Aurelia node"
EXAM_TASK = "Aurelia exam end"


def mode_file() -> Path:
    return config_dir() / "mode.json"


def read_mode() -> dict:
    try:
        m = json.loads(mode_file().read_text())
    except (OSError, ValueError):
        return {"mode": "normal"}
    if m.get("mode") == "exam" and m.get("until") and time.time() >= float(m["until"]):
        return {"mode": "normal", "ended": "exam"}                 # the exam is over
    return m if m.get("mode") in MODES else {"mode": "normal"}


def write_mode(mode: str, until: float | None = None, auto: bool = False) -> dict:
    m = {"mode": mode, **({"until": int(until)} if until else {}), **({"auto": True} if auto else {})}
    mode_file().parent.mkdir(parents=True, exist_ok=True)
    mode_file().write_text(json.dumps(m))
    MODE_CHANGED.set()
    return m


def current_mode() -> str:
    return read_mode().get("mode", "normal")


def game_paths() -> list[str]:
    """Executable paths of what's running (lower case, forward slashes)."""
    paths = []
    if IS_WINDOWS:
        c, w, k32 = _win()
        procs = _windows_procs()
        k32.OpenProcess.restype = w.HANDLE
        for pid in [p for p in procs if isinstance(p, int)]:
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                continue
            buf, size = c.create_unicode_buffer(1024), w.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, c.byref(size)):
                paths.append(buf.value.replace("\\", "/").lower())
            k32.CloseHandle(h)
        return paths
    for d in PROC.iterdir() if PROC.exists() else []:
        if d.name.isdigit():
            try:
                paths.append(os.readlink(d / "exe").lower())
            except OSError:
                pass
    return paths


def game_running(paths: list[str] | None = None) -> str | None:
    for p in game_paths() if paths is None else paths:
        for marker in GAME_DIRS:
            if marker in p:
                rest = p.split(marker, 1)[1]
                return rest.split("/", 1)[0] or marker.rstrip("/")
    return None


def auto_gaming_loop(stop: threading.Event, every: float = 30) -> None:
    while not stop.wait(every):
        try:
            m, game = read_mode(), game_running()
            if game and m.get("mode") == "normal":
                write_mode("gaming", auto=True)
                log(f"gaming mode on ({game})")
            elif not game and m.get("mode") == "gaming" and m.get("auto"):
                write_mode("normal")
                log("gaming mode off (the game closed)")
        except Exception as exc:
            log("couldn't check for games:", repr(exc))


def _win_tasks(script: str, env: dict | None = None) -> tuple[int, str, str]:
    """A fixed PowerShell script for the scheduled tasks; values only through environment variables."""
    exe = shutil.which("powershell") or "powershell"
    try:
        r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                           timeout=60, env={**os.environ, **(env or {})})
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError):
        return 1, "", "powershell didn't run"


def _pythonw() -> str:
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else sys.executable)


def schedule_return(seconds: float) -> str:
    """The one-time timer that ends exam mode, and switching autostart off meanwhile."""
    guard = Path(__file__).resolve().with_name("aurelia_node_guard.py")
    if IS_WINDOWS:
        code, _, err = _win_tasks(
            "$a = New-ScheduledTaskAction -Execute $env:AUR_PY -Argument ('\"' + $env:AUR_GUARD + '\" exam-end');"
            "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds([int]$env:AUR_SECONDS);"
            "Register-ScheduledTask -TaskName $env:AUR_EXAM -Action $a -Trigger $t -Force | Out-Null;"
            "Disable-ScheduledTask -TaskName $env:AUR_TASK -ErrorAction SilentlyContinue | Out-Null",
            {"AUR_PY": _pythonw(), "AUR_GUARD": str(guard), "AUR_SECONDS": str(int(seconds)),
             "AUR_EXAM": EXAM_TASK, "AUR_TASK": TASK_NAME})
        if code != 0:
            raise ValueError(f"I couldn't set the timer that brings me back ({_clean(err) or code}), so I stayed on")
        return "task"
    if shutil.which("systemd-run"):
        code, _, err = _run_full(["systemd-run", "--user", f"--on-active={int(seconds)}", "--unit=aurelia-exam-end",
                                  "--collect", "systemctl", "--user", "start", "aurelia-node.service"], 15)
        if code != 0:
            raise ValueError(f"I couldn't set the timer that brings me back ({_clean(err) or code}), so I stayed on")
        return "systemd"
    raise ValueError("this computer has no way to bring me back on a timer, so I stayed on")


def end_exam() -> None:
    """Exam mode is over (the timer, or him on this computer): autostart back on, and start."""
    write_mode("normal")
    if IS_WINDOWS:
        _win_tasks("Enable-ScheduledTask -TaskName $env:AUR_TASK -ErrorAction SilentlyContinue | Out-Null;"
                   "Unregister-ScheduledTask -TaskName $env:AUR_EXAM -Confirm:$false -ErrorAction SilentlyContinue;"
                   "Start-ScheduledTask -TaskName $env:AUR_TASK -ErrorAction SilentlyContinue",
                   {"AUR_TASK": TASK_NAME, "AUR_EXAM": EXAM_TASK})
    elif shutil.which("systemctl"):
        _run_full(["systemctl", "--user", "stop", "aurelia-exam-end.timer"], 10)
        _run_full(["systemctl", "--user", "start", "aurelia-node.service"], 10)


STOP_AFTER_REPLY = threading.Event()


def do_mode(args: dict) -> dict:
    mode = str(args.get("mode") or "")
    if mode not in MODES:
        raise ValueError("modes: normal, gaming, exam")
    if mode == "exam":
        hours = max(0.25, min(12.0, float(args.get("hours") or 3)))
        until = time.time() + hours * 3600
        how = schedule_return(hours * 3600)
        m = write_mode("exam", until)
        STOP_AFTER_REPLY.set()                                 # answer first, then stop completely
        return {**m, "how": how}
    if mode == "gaming":
        return write_mode("gaming")
    return write_mode("normal")


# ---- the clipboard (only when he asks) ----------------------------------------------------------
#
# Text only. Read when he asks ("copy what's on the desk"), written when he asks ("put it on the TV
# PC"). Never anything a password manager marked as secret, and never what looks like a password,
# key or token: Aurelia doesn't carry credentials between computers. Linux: wl-clipboard (Wayland)
# or xclip (X11), by argument list; Windows: PowerShell's own clipboard commands (fixed text; what's
# copied goes through stdin, never into a command line).

CLIP_MAX = 20000
SECRET_MIME = ("x-kde-passwordmanagerhint", "excludeclipboardcontentfrommonitorprocessing",
               "canincludeinclipboardhistory")
SECRET_TEXT = re.compile(r"^(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
                         r"aur_[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----.*|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,})$",
                         re.S)


def looks_secret(text: str) -> bool:
    """A key, token or password-looking string (one word, long, mixing letters, digits and symbols)."""
    t = text.strip()
    if SECRET_TEXT.match(t):
        return True
    if " " in t or "\n" in t or not (12 <= len(t) <= 128) or re.match(r"^[a-z]+://", t, re.I):
        return False
    kinds = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return kinds >= 3 and not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", t)          # an email address is fine


def _run_in(argv: list[str], data: str, timeout: float = 10) -> tuple[int, str, str]:
    exe = shutil.which(argv[0])
    if not exe:
        return 127, "", ""
    try:
        r = subprocess.run([exe, *argv[1:]], input=data, capture_output=True, text=True, timeout=timeout, env=session_env())
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def _clip_tools() -> str:
    if IS_WINDOWS:
        return "windows"
    if wayland() and shutil.which("wl-paste") and shutil.which("wl-copy"):
        return "wayland"
    if session_env().get("DISPLAY") and shutil.which("xclip"):
        return "x11"
    raise ValueError("I can't reach this computer's clipboard (wl-clipboard or xclip isn't installed: run the "
                     "installer with --system)")


def do_clipboard_get(args: dict) -> dict:
    how = _clip_tools()
    if how == "windows":
        code, out, err = _run_in(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"], "", 15)
        types = ""
    elif how == "wayland":
        _, types, _ = _run_env(["wl-paste", "--list-types"])
        code, out, err = _run_env(["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"])
        if code != 0:
            code, out, err = _run_env(["wl-paste", "--no-newline"])
    else:
        _, types, _ = _run_env(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"])
        code, out, err = _run_env(["xclip", "-selection", "clipboard", "-o"])
    if any(m in types.lower() for m in SECRET_MIME):
        raise ValueError("what's copied there came from a password manager, so I won't take it")
    text = out if how != "windows" else out.rstrip("\r\n")
    if code != 0 or not text.strip():
        return {"text": "", "empty": True}
    if looks_secret(text):
        raise ValueError("what's copied there looks like a password or a key, so I won't take it")
    return {"text": text[:CLIP_MAX], "cut": len(text) > CLIP_MAX}


def do_clipboard_set(args: dict) -> dict:
    text = str(args.get("text") or "")
    if not text.strip():
        raise ValueError("nothing to put on the clipboard")
    if len(text) > CLIP_MAX:
        raise ValueError("that's too long for the clipboard")
    if looks_secret(text):
        raise ValueError("that looks like a password or a key, so I won't put it on a clipboard")
    how = _clip_tools()
    if how == "windows":
        code, _, err = _run_in(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                               text, 15)
    elif how == "wayland":
        code, _, err = _run_in(["wl-copy"], text)
    else:
        code, _, err = _run_in(["xclip", "-selection", "clipboard", "-i"], text)
    if code != 0:
        raise ValueError(f"the clipboard said no ({_clean(err) or code})")
    back = None
    for _ in range(3):                                        # read it back: it's really there
        try:
            back = do_clipboard_get({}).get("text", "")
        except ValueError:
            back = None
            break
        if back.rstrip("\r\n") == text[:CLIP_MAX].rstrip("\r\n"):
            break
        time.sleep(0.2)                                       # the clipboard can take a moment to settle
    if back is not None and back.rstrip("\r\n") != text[:CLIP_MAX].rstrip("\r\n"):
        raise ValueError("I put it on the clipboard, but it didn't stay there")
    return {"set": True, "chars": len(text), "verified": back is not None}


# ---- installing and removing apps (always his confirmation) -----------------------------------------
#
# Only from the system's own signed sources: Ubuntu's packages through PackageKit (pkgcli, or pkcon on
# older releases), and snaps.
# Never a file, a script or a web download. Aurelia finds what would be installed or removed first
# and puts it in front of him; these commands run only with confirmed=True, which the orchestrator
# sends only after he said yes. A removal that would take other packages with it is refused, and
# the system's core (and its security software, and what Aurelia herself runs on) is never removed.
# Permission comes from the installer's --system step (a polkit rule for this account and only
# these actions: install trusted packages, remove packages, manage snaps).

PKG_NAME = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,62}$")
PKG_TERM = re.compile(r"^[A-Za-z0-9 +.\-]{1,60}$")
PROTECTED_PKG = re.compile(
    r"^(linux-|systemd|libc|libsystemd|python3|openssh|sudo|polkit|policykit|packagekit|apt|dpkg|snapd|core\d*$|bare$|"
    r"tailscale|network-manager|netplan|gdm3|ubuntu-|cage$|grub|shim|initramfs|base-|coreutils|bash$|dash$|login$|passwd$|"
    r"util-linux|dbus|udev|firmware|xorg|xwayland|wayland|gnome-shell|mutter|pipewire|wireplumber|pulseaudio|"
    r"ufw|apparmor|clamav|fail2ban|unattended-upgrades|ca-certificates|gnupg|openssl|chromium|google-chrome|firefox$)")


def _run_c(argv: list[str], timeout: float = 30) -> tuple[int, str, str]:
    """A fixed program by argument list, in the C locale (its output is parsed)."""
    exe = shutil.which(argv[0])
    if not exe:
        return 127, "", ""
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C", "LANG": "C"})
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "it took too long"
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def parse_apt_search(out: str) -> list[dict]:
    found = []
    for line in out.splitlines():
        name, sep, summary = line.partition(" - ")
        if sep and PKG_NAME.match(name.strip()):
            found.append({"source": "apt", "name": name.strip(), "summary": summary.strip()[:120]})
    return found


def parse_snap_find(out: str) -> list[dict]:
    found = []
    for line in out.splitlines()[1:]:
        f = line.split(None, 4)
        if len(f) >= 4 and PKG_NAME.match(f[0]):
            found.append({"source": "snap", "name": f[0], "summary": (f[4] if len(f) > 4 else "").strip()[:120],
                          "publisher": f[2].rstrip("✓*✪")})
    return found


def parse_apt_removal(out: str) -> list[str]:
    """What `apt-get -s remove` says would go: one name per 'Remv' line."""
    return [m.group(1) for m in re.finditer(r"^Remv (\S+)", out, re.M)]


def _apt_installed(names: list[str]) -> set[str]:
    if not names:
        return set()
    _, out, _ = _run_c(["dpkg-query", "-W", "-f", "${Package} ${db:Status-Status}\n", *names])
    return {l.split()[0] for l in out.splitlines() if l.endswith(" installed")}


def _snap_installed() -> set[str]:
    _, out, _ = _run_c(["snap", "list"])
    return {l.split()[0] for l in out.splitlines()[1:] if l.strip()}


def _essential(name: str) -> bool:
    _, out, _ = _run_c(["dpkg-query", "-W", "-f", "${Essential} ${Priority}", name])
    return out.startswith("yes") or " required" in out or " important" in out


def do_packages_find(args: dict) -> dict:
    """What could be installed for his words (and whether it already is). Changes nothing."""
    if IS_WINDOWS:
        raise ValueError("installing apps on Windows comes with the Windows installer")
    term = " ".join(str(args.get("name") or "").split())
    if not PKG_TERM.match(term):
        raise ValueError("say the app's name (letters, numbers, spaces)")
    want = term.lower().replace(" ", "-")
    apt = []
    if shutil.which("apt-cache"):
        _, out, _ = _run_c(["apt-cache", "search", "--names-only", term])
        apt = parse_apt_search(out)
    snaps = []
    if shutil.which("snap"):
        _, out, _ = _run_c(["snap", "find", term], 40)
        snaps = parse_snap_find(out)
    rank = lambda c: (c["name"] != want, not c["name"].startswith(want), len(c["name"]))
    apt, snaps = sorted(apt, key=rank)[:6], sorted(snaps, key=rank)[:4]
    have, have_snap = _apt_installed([c["name"] for c in apt]), (_snap_installed() if snaps else set())
    for c in apt:
        c["installed"] = c["name"] in have
    for c in snaps:
        c["installed"] = c["name"] in have_snap
    cands = sorted(apt + snaps, key=rank)
    return {"candidates": cands, "can_install": bool(_pk("install", "x")) or bool(shutil.which("snap"))}


def _removal(name: str) -> list[str]:
    code, out, err = _run_c(["apt-get", "-s", "remove", name], 60)
    if code != 0:
        raise ValueError(f"I can't tell what removing {name} would take with it ({_clean(err) or code})")
    return parse_apt_removal(out)


def do_package_check_remove(args: dict) -> dict:
    """What removing it would take away, before he's asked. Changes nothing."""
    name, source = _pkg_args(args)
    if source == "snap":
        if name not in _snap_installed():
            raise ValueError(f"{name} isn't installed (as a snap)")
        return {"name": name, "source": "snap", "removes": [name]}
    if PROTECTED_PKG.match(name) or _essential(name):
        raise ValueError(f"{name} is part of the system (or keeps me or this computer safe), so I won't remove it")
    if name not in _apt_installed([name]):
        raise ValueError(f"{name} isn't installed here")
    removes = _removal(name)
    extra = [x for x in removes if x != name]
    if extra:
        raise ValueError(f"removing {name} would also remove {', '.join(extra[:8])}" + ("…" if len(extra) > 8 else "")
                         + ", so I won't")
    return {"name": name, "source": "apt", "removes": removes or [name]}


def _pkg_args(args: dict) -> tuple[str, str]:
    if IS_WINDOWS:
        raise ValueError("installing apps on Windows comes with the Windows installer")
    name, source = str(args.get("name") or "").strip(), str(args.get("source") or "apt")
    if not PKG_NAME.match(name) or source not in ("apt", "snap"):
        raise ValueError("which package? (its exact name, from apt or snap)")
    return name, source


def _pk(action: str, name: str = "") -> list[str] | None:
    """PackageKit's command line: pkgcli (PackageKit 1.3+, Ubuntu 26.04) or pkcon (older). Never
    removes unused dependencies on the side, never untrusted packages."""
    if shutil.which("pkgcli"):
        return {"install": ["pkgcli", "-y", "install", "--no-autoremove", name],
                "remove": ["pkgcli", "-y", "remove", "--no-autoremove", name],
                "refresh": ["pkgcli", "refresh"]}[action]
    if shutil.which("pkcon"):
        return {"install": ["pkcon", "-y", "-p", "install", name], "remove": ["pkcon", "-y", "-p", "remove", name],
                "refresh": ["pkcon", "-p", "refresh"]}[action]
    return None


def _need_permission(err: str) -> bool:
    low = err.lower()
    return "not authorized" in low or "authorization" in low or "permission denied" in low or "access denied" in low


def do_package_install(args: dict) -> dict:
    if args.get("confirmed") is not True:
        raise ValueError("installing an app needs his confirmation")
    name, source = _pkg_args(args)
    if source == "snap":
        if not shutil.which("snap"):
            raise ValueError("snaps aren't available here")
        code, out, err = _run_c(["snap", "install", name], 900)
        installed = name in _snap_installed()
    else:
        if not _pk("install", name):
            raise ValueError("I can't install here yet: run the installer with --system once (it lets me install with your OK)")
        code, out, err = _run_c(_pk("install", name), 900)
        if name not in _apt_installed([name]) and "not found" in (out + err).lower():
            _run_c(_pk("refresh"), 300)                           # an old package list: refresh once and try again
            code, out, err = _run_c(_pk("install", name), 900)
        installed = name in _apt_installed([name])
    if not installed:
        why = err or out
        if _need_permission(why):
            raise ValueError("this computer didn't let me install (run the installer with --system once)")
        raise ValueError(f"{name} didn't install ({_clean(why.strip().splitlines()[-1] if why.strip() else str(code))})")
    return {"name": name, "source": source, "installed": True}


def do_package_remove(args: dict) -> dict:
    if args.get("confirmed") is not True:
        raise ValueError("removing an app needs his confirmation")
    expected = [str(x) for x in args.get("removes") or []]
    checked = do_package_check_remove(args)                       # again, now: nothing may have changed
    if expected and sorted(checked["removes"]) != sorted(expected):
        raise ValueError("what removing it would take away has changed since you were asked, so I didn't")
    name, source = checked["name"], checked["source"]
    if source == "snap":
        code, out, err = _run_c(["snap", "remove", name], 600)
        gone = name not in _snap_installed()
    else:
        if not _pk("remove", name):
            raise ValueError("I can't remove apps here yet: run the installer with --system once")
        code, out, err = _run_c(_pk("remove", name), 600)
        gone = name not in _apt_installed([name])
    if not gone:
        why = err or out
        if _need_permission(why):
            raise ValueError("this computer didn't let me remove it (run the installer with --system once)")
        raise ValueError(f"{name} wasn't removed ({_clean(why.strip().splitlines()[-1] if why.strip() else str(code))})")
    return {"name": name, "source": source, "removed": True}


# ---- diagnostics (stage 1 of node fixes: read-only) ---------------------------------------------
#
# "What's wrong with this computer?" answered with evidence. Every check here only looks: a fixed
# list of programs run by argument list (no shell), files read, names resolved. Nothing is changed,
# restarted or written. Log lines are trimmed, and anything that looks like a secret is masked
# before it leaves this computer. What comes back is data for Aurelia, never instructions.

UNIT = re.compile(r"^[A-Za-z0-9@._:-]{1,80}$")
SECRETISH = re.compile(r"(?i)\b(pass(word)?|passwd|token|secret|api[_-]?key|auth|cookie|session)(\s*[=:]\s*)\S+")
LONG_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
DIAG_CHECKS = ("services", "her", "errors", "kernel", "network", "tailscale", "time", "storage", "memory", "updates",
               "shutdown")


def _clean(line: str, limit: int = 220) -> str:
    line = "".join(ch if ch.isprintable() or ch == "\t" else " " for ch in line).strip()
    line = SECRETISH.sub(lambda m: m.group(1) + m.group(3) + "…", line)
    line = LONG_BLOB.sub("[…]", line)
    return line[:limit]


def _lines(text: str, n: int = 25) -> list[str]:
    return [_clean(x) for x in text.splitlines() if x.strip()][-n:]


def _run_full(argv: list[str], timeout: float = 6) -> tuple[int, str, str]:
    exe = shutil.which(argv[0])
    if not exe:
        return 127, "", ""
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def _check(cid: str, name: str, status: str, summary: str, lines: list[str] | None = None) -> dict:
    return {"id": cid, "name": name, "status": status, "summary": summary, "lines": lines or []}


def _resolves(host: str, seconds: float = 3) -> bool:
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        try:
            return bool(ex.submit(socket.getaddrinfo, host, 443).result(timeout=seconds))
        except Exception:
            return False


def _diag_services() -> dict:
    if not shutil.which("systemctl"):
        return _check("services", "Services", "info", "Couldn't check (no systemd here)")
    code, sysout, _ = _run_full(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain"])
    if code != 0:
        return _check("services", "Services", "info", "Couldn't check (systemd isn't answering)")
    _, userout, _ = _run_full(["systemctl", "--user", "list-units", "--state=failed", "--no-legend", "--plain"])
    failed = [x.split()[0] for x in sysout.splitlines() if x.strip()] + \
             [x.split()[0] + " (yours)" for x in userout.splitlines() if x.strip()]
    if failed:
        return _check("services", "Services", "bad", f"{len(failed)} failed: {', '.join(failed[:5])}", failed[:20])
    return _check("services", "Services", "ok", "No failed services")


def _diag_her() -> dict:
    if not SHELL or SHELL.state() != "on":
        return _check("her", "My screen", "ok", "My agent is running (no shell on this computer)")
    if not SHELL.running():
        return _check("her", "My screen", "bad", "My shell is on, but its browser isn't running")
    try:
        pages = SHELL.pages()
    except Exception as exc:
        return _check("her", "My screen", "warn", f"My browser is running but not answering ({exc.__class__.__name__})")
    mine = [t for t in pages if SHELL.is_hers(t)]
    if not mine:
        return _check("her", "My screen", "warn", "My browser is running but my view isn't open in it")
    return _check("her", "My screen", "ok", f"My view is up ({len(pages)} page{'s' if len(pages) != 1 else ''} open)")


def _journal(args: list[str], n: int = 30) -> tuple[list[str], bool]:
    if not shutil.which("journalctl") or not any(Path(d).is_dir() and any(Path(d).iterdir())
                                                 for d in ("/run/log/journal", "/var/log/journal")):
        raise LookupError("no journal here")
    code, out, err = _run_full(["journalctl", "--no-pager", "-o", "short-iso", "-n", str(n), *args], timeout=8)
    if code != 0 or "No journal files were found" in err:
        raise LookupError("the journal isn't readable")
    limited = "not seeing messages" in err or "No journal files were opened" in err
    return _lines(out, n), limited


def _diag_errors() -> dict:
    try:
        lines, limited = _journal(["-b", "-p", "err", "--quiet"])
    except LookupError:
        return _check("errors", "Errors this boot", "info", "Couldn't read the logs (no journal here)")
    note = " (only your own logs: add this account to the adm group to see the system's)" if limited else ""
    if not lines:
        return _check("errors", "Errors this boot", "ok", "None logged" + note)
    sources: dict[str, int] = {}
    for x in lines:
        parts = x.split()
        src = parts[2].split("[")[0].rstrip(":") if len(parts) > 2 else "?"
        sources[src] = sources.get(src, 0) + 1
    top = ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in sorted(sources.items(), key=lambda kv: -kv[1])[:4])
    return _check("errors", "Errors this boot", "warn" if len(lines) >= 10 else "info",
                  f"{len(lines)}{'+' if len(lines) >= 30 else ''} logged, mostly from {top}" + note, lines)


def _diag_kernel() -> dict:
    try:
        lines, _ = _journal(["-k", "-b", "-p", "warning", "--quiet"], 40)
    except LookupError:
        return _check("kernel", "Kernel", "info", "Couldn't read the kernel log")
    oom = [x for x in lines if "out of memory" in x.lower() or "oom-kill" in x.lower() or "killed process" in x.lower()]
    io = [x for x in lines if any(w in x.lower() for w in ("i/o error", "ext4-fs error", "nvme", "ata", "smart"))
          and "error" in x.lower()]
    if oom:
        return _check("kernel", "Kernel", "bad", f"Ran out of memory this boot and killed {len(oom)} program(s)", oom[-10:])
    if io:
        return _check("kernel", "Kernel", "bad", "Disk errors in the kernel log", io[-10:])
    if lines:
        return _check("kernel", "Kernel", "info", f"{len(lines)} warning(s), nothing serious", lines[-10:])
    return _check("kernel", "Kernel", "ok", "No warnings")


def _diag_network(server: str) -> dict:
    lines = _lines(_run(["ip", "-br", "addr"]), 12)
    route = _run(["ip", "route", "show", "default"]).strip()
    if not route:                                       # straight from the kernel, if `ip` isn't there
        for x in _read(PROC / "net" / "route").splitlines()[1:]:
            f = x.split()
            if len(f) > 2 and f[1] == "00000000":
                gw = ".".join(str(int(f[2][i:i + 2], 16)) for i in (6, 4, 2, 0))
                route = f"default via {gw} dev {f[0]}"
                break
    host = urllib.parse.urlsplit(server).hostname or ""
    problems = []
    if not route:
        problems.append("no default route (not connected to a network)")
    if not _resolves("example.com"):
        problems.append("can't look up internet names (DNS)")
    if host and not _resolves(host):
        problems.append(f"can't look up {host}")
    wifi = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"])
    signal = next((x.split(":") for x in wifi.splitlines() if x.startswith("yes:")), None)
    extra = ""
    if signal and len(signal) >= 3 and signal[2].isdigit():
        extra = f"; Wi-Fi {signal[1]} at {signal[2]}%"
        if int(signal[2]) < 40:
            problems.append(f"weak Wi-Fi ({signal[2]}%)")
    lines += [_clean("default route: " + (route or "none"))]
    if problems:
        return _check("network", "Network", "bad" if not route else "warn", "; ".join(problems).capitalize(), lines)
    return _check("network", "Network", "ok", "Connected; names resolve" + extra, lines)


def _diag_tailscale() -> dict:
    code, out, _ = _run_full(["tailscale", "status", "--json"])
    if code == 127:
        return _check("tailscale", "Tailscale", "info", "Not installed here (I reach this computer another way)")
    try:
        j = json.loads(out)
    except ValueError:
        return _check("tailscale", "Tailscale", "warn", "Installed, but it didn't answer")
    state, me = j.get("BackendState"), (j.get("Self") or {})
    if state != "Running":
        return _check("tailscale", "Tailscale", "bad", f"Not running ({state or 'unknown'})")
    peers = j.get("Peer") or {}
    online = sum(1 for p in peers.values() if p.get("Online"))
    return _check("tailscale", "Tailscale", "ok" if me.get("Online", True) else "warn",
                  f"Running; {online} of {len(peers)} other devices online")


def _diag_time() -> dict:
    v = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip()
    if v == "no":
        return _check("time", "Clock", "warn", "Not synced to network time (sign-ins and certificates can fail)")
    return _check("time", "Clock", "ok", "Synced") if v == "yes" else _check("time", "Clock", "info", "Couldn't tell")


def _diag_storage() -> dict:
    disks, lines, worst = _linux_disks(), [], None
    for d in disks:
        lines.append(f"{d['mount']}: {d['used_percent']}% used, {d['free_gb']} GB free of {d['total_gb']} GB")
        if worst is None or d["used_percent"] > worst["used_percent"]:
            worst = d
    root_ro = any(x.split()[1] == "/" and "ro" in x.split()[3].split(",") for x in _read(PROC / "mounts").splitlines()
                  if len(x.split()) > 3)
    try:
        st = os.statvfs("/")
        inodes = 100 - round(100 * st.f_favail / st.f_files) if st.f_files else 0
    except OSError:
        inodes = 0
    if root_ro:
        return _check("storage", "Storage", "bad", "The system disk is read-only (usually a disk error)", lines)
    if worst and worst["used_percent"] >= 95 or inodes >= 95:
        return _check("storage", "Storage", "bad", f"{worst['mount']} is {worst['used_percent']}% full" if worst else
                      f"Out of file slots ({inodes}% of inodes used)", lines)
    if worst and worst["used_percent"] >= 85:
        return _check("storage", "Storage", "warn", f"{worst['mount']} is {worst['used_percent']}% full", lines)
    return _check("storage", "Storage", "ok", "Plenty of space", lines)


def _diag_memory() -> dict:
    m = _meminfo()
    total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    swap_used = m.get("SwapTotal", 0) - m.get("SwapFree", 0)
    if not total:
        return _check("memory", "Memory", "info", "Couldn't read memory")
    pct = round(100 * avail / total)
    line = f"{round(avail / 1e9, 1)} GB free of {round(total / 1e9, 1)} GB; swap used {round(swap_used / 1e9, 1)} GB"
    if pct < 8:
        return _check("memory", "Memory", "bad", f"Almost out of memory ({pct}% free)", [line])
    if pct < 15 or swap_used > total * 0.5:
        return _check("memory", "Memory", "warn", f"Memory is tight ({pct}% free)", [line])
    return _check("memory", "Memory", "ok", f"{pct}% free", [line])


def _diag_updates() -> dict:
    lines, recent = [], []
    pending = Path("/var/run/reboot-required").exists()
    pkgs = _read(Path("/var/run/reboot-required.pkgs")).split()
    cutoff = time.time() - 7 * 86400
    for x in _read(Path("/var/log/dpkg.log")).splitlines()[-3000:]:
        parts = x.split()
        if len(parts) >= 4 and parts[2] in ("install", "upgrade", "remove"):
            try:
                when = time.mktime(time.strptime(parts[0] + " " + parts[1], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if when >= cutoff:
                recent.append(_clean(f"{parts[0]} {parts[2]} {parts[3].split(':')[0]}"))
    if recent:
        lines = recent[-15:]
    if pending:
        return _check("updates", "Updates", "warn",
                      "A restart is pending to finish updates" + (f" ({', '.join(pkgs[:3])})" if pkgs else ""), lines)
    if recent:
        return _check("updates", "Updates", "info", f"{len(recent)} package change(s) in the last week", lines)
    return _check("updates", "Updates", "ok", "No restart pending; no package changes this week")


CLEAN_END = re.compile(r"systemd-shutdown|Reached target .*(Reboot|Power-?Off|Shutdown)|System is (rebooting|powering down)"
                       r"|reboot: (Restarting|Power down)|Shutting down\.", re.I)
STUCK = re.compile(r"(?:A stop job is running for|State 'stop-sigterm' timed out|Stopping timed out|Failed to stop|"
                   r"Processes still around after|Killing process)\s*(.*)", re.I)


def _diag_shutdown() -> dict:
    """Did the last shutdown or restart finish, or did it hang (or the power go)?"""
    try:
        lines, _ = _journal(["-b", "-1"], 200)
    except LookupError:
        return _check("shutdown", "Last shutdown", "info", "Couldn't read the previous boot's log")
    if not lines:
        return _check("shutdown", "Last shutdown", "info", "No log from before this boot (it may not be kept)")
    stuck = []
    for x in lines:
        m = STUCK.search(x)
        if m:
            stuck.append(x)
    clean = any(CLEAN_END.search(x) for x in lines[-60:])
    if stuck:
        names = []
        for x in stuck:
            m = re.search(r"([\w@.-]+\.(?:service|scope|mount|socket|slice))", x)
            if m and m.group(1) not in names:
                names.append(m.group(1))
        what = f" waiting on {', '.join(names[:4])}" if names else ""
        return _check("shutdown", "Last shutdown", "warn" if clean else "bad",
                      ("It finished, but slowly," if clean else "It hung") + what, stuck[-12:])
    if clean:
        return _check("shutdown", "Last shutdown", "ok", "Finished cleanly")
    return _check("shutdown", "Last shutdown", "warn",
                  "The last boot ended without a clean shutdown (forced off, a power cut, or a hang)", lines[-10:])


def _diag_unit(unit: str) -> dict:
    if not UNIT.match(unit):
        raise ValueError("that isn't a service name")
    if not shutil.which("systemctl"):
        return _check("service:" + unit, unit, "info", "Couldn't check services here (no systemd)")
    name = unit if "." in unit else unit + ".service"
    for scope in ([], ["--user"]):
        code, out, _ = _run_full(["systemctl", *scope, "show", name, "-p", "LoadState,ActiveState,SubState,Result,"
                                  "NRestarts,ExecMainStatus,Description"])
        props = dict(x.split("=", 1) for x in out.splitlines() if "=" in x)
        if props.get("LoadState") not in (None, "not-found"):
            try:
                jl, _ = _journal((["--user-unit", name] if scope else ["-u", name]) + ["-b"], 30)
            except LookupError:
                jl = []
            state = f"{props.get('ActiveState')} ({props.get('SubState')})"
            restarts = int(props.get("NRestarts") or 0)
            bad = props.get("ActiveState") in ("failed",) or props.get("Result") not in (None, "", "success")
            summary = f"{props.get('Description') or name}: {state}" + \
                      (f", restarted {restarts} time(s)" if restarts else "") + \
                      (f", last result {props.get('Result')}" if props.get("Result") not in (None, "", "success") else "")
            return _check("service:" + name, name + (" (yours)" if scope else ""),
                          "bad" if bad else "warn" if restarts >= 3 else "ok", summary, jl)
    return _check("service:" + name, name, "info", "No service by that name here")


def windows_diagnose() -> list[dict]:
    out = []
    code, text, _ = _run_full(["wevtutil", "qe", "System", "/c:25", "/rd:true", "/f:text",
                               "/q:*[System[(Level=1 or Level=2)]]"], timeout=10)
    errs = [x for x in text.splitlines() if x.strip().startswith(("Date:", "Source:", "Description:"))]
    out.append(_check("errors", "Errors (System log)", "info" if errs else "ok",
                      f"{len(errs) // 3} recent error(s)" if errs else "None recent", _lines("\n".join(errs), 30)))
    out.append(_check("network", "Network", "ok" if _resolves("example.com") else "bad",
                      "Names resolve" if _resolves("example.com") else "Can't look up internet names (DNS)"))
    out.append(_check("storage", "Storage", "info", "See health for disk space"))
    return out


def do_diagnose(args: dict) -> dict:
    """The read-only checks above; one named service in detail when asked."""
    try:
        server = load()[0].get("server", "")
    except SystemExit:
        server = ""
    if IS_WINDOWS:
        checks = windows_diagnose()
    else:
        want = [c for c in (args.get("checks") or DIAG_CHECKS) if c in DIAG_CHECKS]
        run = {"services": _diag_services, "her": _diag_her, "errors": _diag_errors, "kernel": _diag_kernel,
               "network": lambda: _diag_network(server), "tailscale": _diag_tailscale, "time": _diag_time,
               "storage": _diag_storage, "memory": _diag_memory, "updates": _diag_updates, "shutdown": _diag_shutdown}
        checks = []
        for c in want:
            try:
                checks.append(run[c]())
            except Exception as exc:                    # one check failing never hides the rest
                checks.append(_check(c, c.capitalize(), "info", f"Couldn't check ({exc.__class__.__name__})"))
    service = str(args.get("service") or "").strip()
    if service and not IS_WINDOWS:
        checks.insert(0, _diag_unit(service))
    return {"checks": checks, "uptime_s": int(time.time() - _boot_time()) if not IS_WINDOWS else None}


def do_showing(args: dict) -> dict:
    """What's in front on her screen: her own view, or something she opened (its title)."""
    if not (SHELL and SHELL.running()):
        return {"shell": False}
    others = [t for t in SHELL.pages() if not SHELL.is_hers(t)]
    if not others:
        return {"shell": True, "content": False}
    t = next((x for x in others if x["id"] == SHELL.current), others[0])
    return {"shell": True, "content": True, "title": str(t.get("title") or "")[:120]}


COMMANDS = {"open_url": do_open_url, "launch_app": do_launch_app, "list_apps": do_list_apps, "info": do_info,
            "home": do_home, "showing": do_showing, "diagnose": do_diagnose, "power": do_power, "wake": do_wake,
            "sound": do_sound, "player": do_player, "health": do_health, "processes": do_processes, "kill": do_kill}


def execute(cmd: dict) -> dict:
    fn = COMMANDS.get(cmd.get("kind"))
    if fn is not None and cmd.get("kind") not in GAMING_OK and current_mode() == "gaming":
        return {"ok": False, "error": "this computer is in gaming mode, so I'm keeping out of the way (say \"end "
                                      "gaming mode\" first)", "data": {"mode": "gaming"}}
    if fn is None:
        return {"ok": False, "error": f"this node doesn't do '{cmd.get('kind')}'"}
    try:
        return {"ok": True, "data": fn(cmd.get("args") or {})}
    except NeedsConfirmation as exc:                         # nothing was pressed; he decides
        return {"ok": False, "error": str(exc), "data": {"confirm": exc.label, "what": exc.what}}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log("command failed:", cmd.get("kind"), repr(exc))
        return {"ok": False, "error": f"it failed here ({exc.__class__.__name__})"}


# ---- the shell: a kiosk browser that is her ---------------------------------------------------

BROWSER_DIRS = ["/snap/bin", "/usr/local/bin", "/usr/bin", "/var/lib/flatpak/exports/bin"]


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
    names = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge-stable",
             "microsoft-edge")
    for name in names:
        if shutil.which(name):
            return shutil.which(name)
    # A service's PATH often leaves out snap and flatpak (Ubuntu's Chromium is a snap): look there too.
    for d in BROWSER_DIRS + [str(Path.home() / ".local/share/flatpak/exports/bin")]:
        for name in names + ("org.chromium.Chromium", "com.google.Chrome"):
            p = Path(d) / name
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return None


def wayland() -> bool:
    """Her session is Wayland (Cage) when the session says so; Chromium must then draw with Wayland."""
    env = session_env()
    return bool(env.get("WAYLAND_DISPLAY")) and (env.get("XDG_SESSION_TYPE") == "wayland" or not env.get("DISPLAY"))


class Shell:
    """Keeps her view on the screen in a kiosk browser, restarting it if it closes."""

    def __init__(self, server: str, key: str):
        self.server, self.key = server.rstrip("/"), key
        self.proc: subprocess.Popen | None = None
        self.starts: list[float] = []
        self.note = "starting"
        self.stop_event = threading.Event()
        self.changed = threading.Event()                 # tell Aurelia soon (the devices panel shows it)
        self.current: str | None = None                  # the page in front (the last one she opened)
        self.token = ""                                  # this look's numbers (see page_look)

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
        if not IS_WINDOWS and wayland():
            args += ["--ozone-platform=wayland", "--enable-features=UseOzonePlatform"]
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
        self.close_leftover()
        login = call(self.server, "/nodes/screen", {}, self.key)["login"]
        self.proc = subprocess.Popen(self.argv(browser, self.server + login), env=session_env(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self.starts.append(now)
        self.changed.set()
        log("shell started:", Path(browser).name)

    def close_leftover(self) -> None:
        """A kiosk from before this agent started (it restarted) still holds the control port and the
        profile, so a new one couldn't start: close it first."""
        try:
            info = self.cdp("/json/version")
        except OSError:
            return
        ws = info.get("webSocketDebuggerUrl") if isinstance(info, dict) else None
        if not ws:
            return
        try:
            with Tab({"id": "browser", "webSocketDebuggerUrl": ws}) as t:
                t.call("Browser.close")
        except (OSError, CDPError):
            pass
        for _ in range(20):
            try:
                self.cdp("/json/version")
            except OSError:
                log("closed the kiosk left from before")
                return
            time.sleep(0.25)

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
        t = self.cdp("/json/new?" + urllib.parse.quote(url, safe=""), "PUT")    # quoted whole: keeps ?a=1&b=2#t
        self.current = t.get("id") if isinstance(t, dict) else None

    def page(self) -> dict:
        """The page on the screen now: the last one she opened, or the newest that isn't her."""
        pages = [t for t in self.pages() if not self.is_hers(t)]
        if not pages:
            raise ValueError("nothing is open on this screen (it's showing me)")
        return next((t for t in pages if t["id"] == self.current), pages[0])

    def home(self) -> int:
        pages = self.pages()
        closed = 0
        for t in pages:
            if not self.is_hers(t):
                self.cdp(f"/json/close/{t['id']}")
                closed += 1
        self.current = None
        hers = [t for t in pages if self.is_hers(t)]
        if hers:
            self.cdp(f"/json/activate/{hers[0]['id']}")
        return closed


SHELL: Shell | None = None


# ---- seeing and using the page on her screen ---------------------------------------------------
#
# Through the kiosk browser's control port (DevTools protocol, 127.0.0.1 only). Everything the
# page script can do is below, fixed in this file; the server only picks an operation and gives
# it data (a number, some text). The script runs in an isolated world: it shares the page's
# elements but not its JavaScript, so the page can't change what the script sees or does.
# Page content that goes back to Aurelia is data, never instructions (she treats it that way).

class NeedsConfirmation(ValueError):
    """The thing to press looks like it buys, pays, sends, posts or deletes: he decides."""

    def __init__(self, label: str, what: str = "click"):
        super().__init__(f"needs confirmation: {label}")
        self.label, self.what = label, what


DEFAULT_CONFIRM = ["buy", "buy now", "purchase", "pay", "pay now", "checkout", "check out", "place order", "order now",
                   "rent", "subscribe", "donate", "tip", "transfer", "send", "post", "reply", "comment", "submit",
                   "delete", "confirm"]

PAGE_JS = r"""
async (op, a) => {
  const D = document, W = window, ATTR = "data-aurelia-n";
  const all = (root, sel, out = []) => {                        // querySelectorAll, into open shadow roots
    root.querySelectorAll(sel).forEach(e => out.push(e));
    root.querySelectorAll("*").forEach(e => { if (e.shadowRoot) all(e.shadowRoot, sel, out); });
    return out;
  };
  const clean = s => String(s || "").replace(/\s+/g, " ").trim();
  const norm = s => clean(s).toLowerCase();
  const deepActive = () => { let e = D.activeElement; while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement; return e; };
  const contains = (outer, inner) => { for (let n = inner; n; n = n.parentNode || n.host) if (n === outer) return true; return false; };
  const deepAt = (x, y) => { let e = D.elementFromPoint(x, y);
    while (e && e.shadowRoot) { const i = e.shadowRoot.elementFromPoint(x, y); if (!i || i === e) break; e = i; } return e; };
  const tag = e => e.tagName.toLowerCase();
  const isField = e => !!e && (e.isContentEditable || tag(e) === "textarea" ||
    (tag(e) === "input" && ["", "text", "search", "email", "url", "tel", "number"].includes((e.getAttribute("type") || "").toLowerCase())) ||
    ["textbox", "searchbox", "combobox"].includes(e.getAttribute("role")));
  const labelOf = e => {
    let s = e.getAttribute("aria-label") || "";
    const by = e.getAttribute("aria-labelledby");
    if (!s && by) s = by.split(/\s+/).map(id => { const r = e.getRootNode(); const t = (r.getElementById && r.getElementById(id)) || D.getElementById(id); return t ? t.textContent : ""; }).join(" ");
    if (!s && ["input", "textarea", "select"].includes(tag(e))) {
      if (e.labels && e.labels.length) s = e.labels[0].textContent;
      s = s || e.getAttribute("placeholder") || (["submit", "button", "reset"].includes(e.type) ? e.value : "") || e.getAttribute("title") || e.name || "";
    }
    if (!clean(s)) s = e.innerText || e.textContent || "";
    if (!clean(s)) { const img = e.querySelector && e.querySelector("img[alt]"); s = (img && img.alt) || e.getAttribute("title") || ""; }
    if (!clean(s) && tag(e) === "video") s = "video";
    return clean(s).slice(0, 100);
  };
  const roleOf = e => {
    const r = e.getAttribute("role"); if (r) return r;
    const t = tag(e);
    if (t === "a") return "link";
    if (t === "input") { const ty = (e.type || "").toLowerCase(); return ["submit", "button", "reset", "image"].includes(ty) ? "button" : ["checkbox", "radio"].includes(ty) ? ty : "text box"; }
    if (t === "textarea" || e.isContentEditable) return "text box";
    if (t === "select") return "list"; if (t === "video") return "video";
    return (t === "button" || t === "summary") ? "button" : "item";
  };
  const shown = e => { const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return null;
    const cs = getComputedStyle(e); if (cs.visibility === "hidden" || cs.display === "none" || parseFloat(cs.opacity) === 0) return null; return r; };
  const reach = (e, r) => {                                      // a point where a click lands on it
    for (const [fx, fy] of [[.5, .5], [.3, .5], [.7, .5], [.5, .3], [.5, .7], [.15, .5]]) {
      const x = r.left + r.width * fx, y = r.top + r.height * fy;
      if (x < 1 || y < 1 || x >= innerWidth - 1 || y >= innerHeight - 1) continue;
      const hit = deepAt(x, y); if (hit && (contains(e, hit) || contains(hit, e))) return [Math.round(x), Math.round(y)];
    } return null; };
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,summary,video,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=option],[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=searchbox],[role=textbox],' +
    '[contenteditable=""],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const OUTER = e => ["a", "button", "summary"].includes(tag(e)) || ["button", "link", "menuitem", "tab", "option"].includes(e.getAttribute("role"));
  const candidates = () => {
    const seen = new Set(), out = [], area = innerWidth * innerHeight;
    for (const e of all(D, SEL)) {
      if (seen.has(e) || e.id === "__aurelia_marks") continue; seen.add(e);
      if (e.disabled || e.getAttribute("aria-disabled") === "true" || e.getAttribute("aria-hidden") === "true") continue;
      const r = shown(e); if (!r) continue;
      const role = roleOf(e), label = labelOf(e);
      if (role === "item" && (!label || r.width * r.height > area * 0.5)) continue;     // a big focusable wrapper
      const p = reach(e, r); if (!p) continue;
      out.push({ e, r, p, role, label });
    }
    const kept = out.filter(c => !out.some(o => o !== c && OUTER(o.e) && contains(o.e, c.e)));
    kept.sort((x, y) => (Math.round(x.r.top / 24) - Math.round(y.r.top / 24)) || (x.r.left - y.r.left));
    return kept;
  };
  const words = new RegExp("(?:^|[^a-z])(?:" + ((a.words && a.words.length) ? a.words : ["buy"]).map(w =>
    String(w).toLowerCase().trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+")).join("|") + ")(?![a-z])", "i");
  const risky = e => { const t = [labelOf(e), e.value, e.getAttribute && e.getAttribute("title")].filter(Boolean).join(" ");
    return words.test(t) ? (labelOf(e) || clean(e.value) || "that button") : null; };
  // What a box is for. password and payment boxes are never typed into; a one-time code box (the
  // code a site emails or texts him) takes only a code he says. "Security code" means a card's
  // only on a page that is about paying (its address or its card boxes say so).
  const payPage = () => /checkout|payment|billing|\/pay(?:\b|\/)|cart|purchase|order|subscribe|upgrade|premium\/.*plan/i.test(location.pathname + location.search) ||
    !!D.querySelector('input[autocomplete^="cc-"],input[name*="cardnumber" i],input[name*="card_number" i]');
  const fieldKind = e => { if (!e || !e.getAttribute) return null;
    const auto = (e.getAttribute("autocomplete") || "").toLowerCase(), type = (e.getAttribute("type") || "").toLowerCase();
    const said = [e.name, e.id, e.getAttribute("aria-label"), e.getAttribute("placeholder"), labelOf(e)].join(" ").toLowerCase();
    const short = (+e.getAttribute("maxlength") || 99) <= 8, numeric = /numeric|decimal/.test(e.getAttribute("inputmode") || "") || type === "tel" || type === "number";
    if (type === "password" || /current-password|new-password/.test(auto)) return "password";
    if (/^cc-|\bcc-/.test(auto)) return "payment";
    if (/one-time-code/.test(auto)) return "code";
    if (/card ?number|credit card|debit card|\bcvv|\bcvc|\bcvn|expir(?:y|ation)|name on card|\biban\b|routing number|account number/.test(said)) return "payment";
    if (/security code/.test(said)) return payPage() ? "payment" : "code";
    if (/\bssn\b|social security|pass(?:word|phrase)|passwd/.test(said)) return "password";
    if (/zip|postal|post ?code|promo|coupon|discount|voucher|gift|referral|invite|country|area code|source code|\bcolou?r\b/.test(said)) return null;
    if (/(?:verification|confirmation|login|sign[- ]?in|one[- ]time|6[- ]digit|security|access) code|\botp\b|\bcode\b|\bdigit/.test(said) || (short && numeric)) return "code";
    if (/passcode|\bpin\b/.test(said)) return short && numeric ? "code" : "password";
    return null; };
  const secret = e => { const k = fieldKind(e); return k === "password" || k === "payment" ? k : null; };
  const enterRisk = e => {                                      // would Enter here send something?
    if (!e) return null;
    if (!isField(e)) return risky(e);                            // Enter on a focused button presses it
    const ty = (e.getAttribute("type") || "").toLowerCase(), role = e.getAttribute("role");
    if (ty === "search" || role === "searchbox" || role === "combobox") return null;
    if (e.form) { for (const b of e.form.querySelectorAll('button,input[type=submit]')) { if ((b.type || "submit") === "submit" && risky(b)) return labelOf(e) || "that box"; } return null; }
    if (tag(e) === "textarea" || e.isContentEditable) return labelOf(e) || "that box";   // a message box: Enter sends
    return null; };
  const clearMarks = () => { all(D, "[" + ATTR + "]").forEach(e => e.removeAttribute(ATTR)); const b = D.getElementById("__aurelia_marks"); if (b) b.remove(); };
  const draw = items => {
    const old = D.getElementById("__aurelia_marks"); if (old) old.remove();
    const box = D.createElement("div"); box.id = "__aurelia_marks";
    box.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647";
    for (const it of items) {
      const b = D.createElement("div"); b.textContent = it.n;
      const edge = it.r.left < 18 || it.r.top < 14;                // on the corner, mostly outside, so it hides little
      b.style.cssText = "position:fixed;left:" + Math.max(0, it.r.left) + "px;top:" + Math.max(0, it.r.top) + "px;" +
        (edge ? "" : "transform:translate(-65%,-60%);") +
        "min-width:18px;padding:0 5px;border-radius:5px;background:#04121a;color:#e9fbfe;border:2px solid #5fd4e6;" +
        "font:700 15px/1.25 ui-monospace,Menlo,Consolas,monospace;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.6)";
      box.append(b);
    }
    (D.fullscreenElement || D.documentElement).append(box);
    const gone = () => box.remove();
    setTimeout(() => { W.addEventListener("scroll", gone, { once: true }); W.addEventListener("wheel", gone, { once: true }); }, 400);
    setTimeout(gone, 120000);
  };
  const videos = () => {
    const vs = all(D, "video").map(v => ({ v, r: v.getBoundingClientRect() })).filter(x => x.r.width > 40 && x.r.height > 40);
    vs.sort((x, y) => ((!y.v.paused) - (!x.v.paused)) || (y.r.width * y.r.height - x.r.width * x.r.height));
    return vs.length ? vs[0].v : null; };
  const vstate = v => v ? { paused: v.paused, time: Math.round(v.currentTime || 0), live: v.duration === Infinity,
    duration: isFinite(v.duration) ? Math.round(v.duration) : null, muted: v.muted, volume: Math.round(v.volume * 100),
    speed: v.playbackRate, fullscreen: !!D.fullscreenElement } : null;
  const find = n => all(D, "[" + ATTR + '="' + a.token + ":" + n + '"]')[0] || null;
  const byText = t => { const want = norm(t); const cs = candidates();
    for (const test of [c => norm(c.label) === want, c => norm(c.label).startsWith(want), c => norm(c.label).includes(want)]) {
      const hits = cs.filter(test); if (hits.length) return { e: hits[0].e, others: hits.length - 1 }; } return null; };
  const point = e => { let r = shown(e);
    if (!r || r.top < 0 || r.bottom > innerHeight) { e.scrollIntoView({ block: "center", inline: "center" }); r = shown(e); }
    return r ? reach(e, r) : null; };

  if (op === "look") {
    clearMarks();
    const cs = candidates(), items = cs.slice(0, 90);
    items.forEach((c, i) => { c.n = i + 1; c.e.setAttribute(ATTR, a.token + ":" + c.n); });
    const text = clean(D.body ? D.body.innerText : "").slice(0, 2500);
    draw(items);
    return { title: clean(D.title), url: location.href, items: items.map(c => ({ n: c.n, role: c.role, label: c.label })),
             more: cs.length - items.length, text, video: vstate(videos()) };
  }
  if (op === "clear") { clearMarks(); return {}; }
  if (op === "hide") { const b = D.getElementById("__aurelia_marks"); if (b) b.remove(); return {}; }   // numbers stay usable
  if (op === "login_fields" || op === "focus_login") {                // sign-in: find the boxes, never read them
    const inputs = all(D, "input").filter(e => { const r = e.getBoundingClientRect();
      return r.width > 2 && r.height > 2 && !e.disabled && e.getAttribute("aria-hidden") !== "true" &&
             e.getAttribute("tabindex") !== "-1" && getComputedStyle(e).visibility !== "hidden"; });
    const pw = inputs.find(e => (e.type || "").toLowerCase() === "password");
    const user = inputs.find(e => { const t = (e.type || "").toLowerCase(), au = (e.getAttribute("autocomplete") || "").toLowerCase();
      const said = [e.name, e.id, e.getAttribute("aria-label"), e.getAttribute("placeholder")].join(" ").toLowerCase();
      return ["text", "email", ""].includes(t) && (t === "email" || au.includes("username") || au === "email" ||
             /e-?mail|user|login|identifier|account/.test(said)); });
    const code = inputs.find(e => fieldKind(e) === "code");
    const usePw = !pw && all(D, "button,a,[role=button]").find(e => { const r = e.getBoundingClientRect();
      return r.width > 2 && r.height > 2 && /(?:log ?in|sign ?in|continue) with (?:a |your )?password|use (?:a |your )?password|enter (?:a |your )?password instead/i.test(labelOf(e)); });
    if (op === "login_fields") return { user: !!user, password: !!pw, code: !!code, use_password: usePw ? labelOf(usePw) : null };
    const e = a.kind === "password" ? pw : user;
    if (!e) return { error: "nofield" };
    e.scrollIntoView({ block: "center" }); e.focus(); if (e.select) e.select();
    return { ok: true };
  }
  if (op === "state") return { title: clean(D.title), url: location.href, video: vstate(videos()) };
  if (op === "viewport") return { w: innerWidth, h: innerHeight, x: scrollX, y: scrollY };
  if (op === "click" || op === "focus") {
    let e = null, others = 0;
    if (a.n) { e = find(a.n); if (!e) return { error: "stale" }; }
    else if (a.text) { const f = byText(a.text); if (f) { e = f.e; others = f.others; } }
    if (!e) return { error: "notfound" };
    const label = labelOf(e);
    if (a.expect && norm(label) !== norm(a.expect)) return { error: "changed", label };
    if (op === "focus" && tag(e) === "input" && secret(e)) return { error: "secret", what: secret(e), label };
    if (op === "focus" && !isField(e)) return { error: "notfield", label };
    if (op === "click" && !a.allow) { const r = risky(e); if (r) return { risky: r }; }
    const p = point(e);
    if (!p) { e.focus && e.focus(); if (op === "click") e.click(); return { label, others, done: true }; }
    return { label, others, x: p[0], y: p[1] };
  }
  if (op === "field") {
    const e = deepActive();
    if (!isField(e)) return { error: "nofield" };
    if (secret(e)) return { error: "secret", what: secret(e) };
    const kind = fieldKind(e);
    if (kind === "code" && e.form) {                                  // six boxes, one digit each: start at the first
      const boxes = all(e.form, "input").filter(x => fieldKind(x) === "code" && x.getBoundingClientRect().width > 2);
      if (boxes.length > 1 && (+boxes[0].getAttribute("maxlength") || 99) <= 2) { boxes.forEach(x => { x.value = ""; }); boxes[0].focus(); }
    }
    if (a.clear) { if (e.isContentEditable) D.execCommand("selectAll"); else if (deepActive().select) deepActive().select(); }
    return { label: labelOf(deepActive()), enter_risk: enterRisk(deepActive()), kind };
  }
  if (op === "enter") {
    const e = deepActive(), label = e ? labelOf(e) : "";
    if (a.expect && norm(label) !== norm(a.expect)) return { error: "changed", label };
    const r = a.allow ? null : enterRisk(e);
    return r ? { risky: r } : { label };
  }
  if (op === "scroll_to") {
    let s = D.scrollingElement;
    if (!s || s.scrollHeight <= s.clientHeight + 10) {
      let best = null; for (const e of all(D, "*")) { if (e.scrollHeight > e.clientHeight + 10 && /(auto|scroll)/.test(getComputedStyle(e).overflowY))
        if (!best || e.clientHeight * e.clientWidth > best.clientHeight * best.clientWidth) best = e; }
      s = best || s; }
    if (s) s.scrollTo({ top: a.to === "top" ? 0 : s.scrollHeight, behavior: "instant" });
    return {};
  }
  if (op === "media") {
    const v = videos(); if (!v) return { error: "novideo" };
    const act = a.action, s = Number(a.seconds || 0);
    const play = () => Promise.race([v.play().then(() => null, e => e.name), new Promise(r => setTimeout(() => r(null), 3000))]);
    let problem = null;
    if (act === "play") problem = await play();
    else if (act === "pause") v.pause();
    else if (act === "toggle") { if (v.paused) problem = await play(); else v.pause(); }
    else if (act === "forward" || act === "back") {
      const to = v.currentTime + (act === "forward" ? s : -s);
      v.currentTime = Math.max(0, isFinite(v.duration) ? Math.min(to, v.duration - 1) : to); }
    else if (act === "seek") v.currentTime = Math.max(0, isFinite(v.duration) ? Math.min(s, v.duration - 1) : s);
    else if (act === "restart") v.currentTime = 0;
    else if (act === "mute") v.muted = true;
    else if (act === "unmute") v.muted = false;
    else if (act === "volume") { v.volume = Math.min(1, Math.max(0, Number(a.level) / 100)); if (v.volume > 0) v.muted = false; }
    else if (act === "speed") v.playbackRate = Math.min(4, Math.max(0.25, Number(a.rate) || 1));
    else if (act === "fullscreen") {
      if (!D.fullscreenElement) {                                 // the player (video plus its controls), not the bare video
        const r = v.getBoundingClientRect(); let t = v;
        for (let p = v.parentElement; p && p !== D.body; p = p.parentElement) {
          const q = p.getBoundingClientRect();
          if (Math.abs(q.width - r.width) <= 4 && Math.abs(q.height - r.height) <= 4) t = p; else break; }
        try { await t.requestFullscreen(); } catch (e) { problem = e.name; } } }
    else if (act === "exit_fullscreen") { if (D.fullscreenElement) try { await D.exitFullscreen(); } catch (e) { problem = e.name; } }
    else return { error: "action" };
    await new Promise(r => setTimeout(r, 300));
    return { title: clean(D.title), video: vstate(v), problem };
  }
  return { error: "op" };
}
"""

CDP_KEYS = {"enter": ("Enter", "Enter", 13, "\r"), "escape": ("Escape", "Escape", 27, ""), "tab": ("Tab", "Tab", 9, ""),
            "space": (" ", "Space", 32, " "), "backspace": ("Backspace", "Backspace", 8, ""),
            "up": ("ArrowUp", "ArrowUp", 38, ""), "down": ("ArrowDown", "ArrowDown", 40, ""),
            "left": ("ArrowLeft", "ArrowLeft", 37, ""), "right": ("ArrowRight", "ArrowRight", 39, ""),
            "page up": ("PageUp", "PageUp", 33, ""), "page down": ("PageDown", "PageDown", 34, ""),
            "home": ("Home", "Home", 36, ""), "end": ("End", "End", 35, "")}


class CDPError(ValueError):
    pass


class Tab:
    """One page in her shell, over the browser's control port: a minimal WebSocket client
    (standard library only) speaking the DevTools protocol."""
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, target: dict, timeout: float = 15):
        u = urllib.parse.urlsplit(target["webSocketDebuggerUrl"])
        if u.hostname not in ("127.0.0.1", "localhost"):
            raise CDPError("the browser's control port isn't local")
        self.id, self.buf, self.next_id = target["id"], b"", 0
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._until(b"\r\n\r\n")
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest())
        if b" 101 " not in head.split(b"\r\n", 1)[0] or accept not in head:
            self.sock.close()
            raise CDPError("the browser refused the control connection")
        self.ctx: int | None = None
        self.events: list[dict] = []                                  # kept while waiting for an answer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("the page closed its connection")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(marker)
        return head

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise CDPError("the page closed its connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    @staticmethod
    def _mask(data: bytes, mask: bytes) -> bytes:
        n = len(data)
        return (int.from_bytes(data, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")

    def _send(self, opcode: int, payload: bytes) -> None:
        n, mask = len(payload), os.urandom(4)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self.sock.sendall(head + mask + (self._mask(payload, mask) if n else b""))

    def _recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), "big")
            elif n == 127:
                n = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n) if n else b""
            if mask and data:
                data = self._mask(data, mask)
            if op == 8:
                raise CDPError("the page closed")
            if op == 9:
                self._send(10, data)
                continue
            if op == 10:
                continue
            parts.append(data)
            if b1 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def call(self, method: str, **params) -> dict:
        self.next_id += 1
        mid = self.next_id
        self._send(1, json.dumps({"id": mid, "method": method, "params": params}).encode())
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(str(msg["error"].get("message") or "the browser said no"))
                return msg.get("result") or {}
            if msg.get("method"):
                self.events = (self.events + [msg])[-20:]

    def notify(self, method: str, **params) -> None:
        """A message that needs no answer (its answer is skipped when it comes)."""
        self.next_id += 1
        self._send(1, json.dumps({"id": self.next_id, "method": method, "params": params}).encode())

    def event(self, method: str, timeout: float) -> dict | None:
        """The next event of this kind, or None after `timeout` seconds."""
        import select
        end = time.monotonic() + timeout
        while True:
            for i, e in enumerate(self.events):
                if e.get("method") == method:
                    return self.events.pop(i).get("params") or {}
            left = end - time.monotonic()
            if left <= 0:
                return None
            if not self.buf and not select.select([self.sock], [], [], left)[0]:
                return None
            msg = json.loads(self._recv())
            if msg.get("method"):
                self.events = (self.events + [msg])[-20:]

    def close(self) -> None:
        try:
            self._send(8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def run(self, op: str, args: dict | None = None):
        """The page script, in an isolated world (fresh after each navigation)."""
        for attempt in (1, 2):
            if self.ctx is None:
                frame = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
                self.ctx = self.call("Page.createIsolatedWorld", frameId=frame, worldName="aurelia")["executionContextId"]
            try:
                res = self.call("Runtime.evaluate", expression=f"({PAGE_JS})({json.dumps(op)}, {json.dumps(args or {})})",
                                contextId=self.ctx, returnByValue=True, awaitPromise=True, userGesture=True)
            except CDPError as exc:
                if attempt == 1 and "context" in str(exc).lower():
                    self.ctx = None                                  # the page navigated; make a new world
                    continue
                raise
            if res.get("exceptionDetails"):
                d = res["exceptionDetails"]
                raise CDPError(f"the page script failed ({(d.get('exception') or {}).get('description', d.get('text', ''))[:200]})")
            return (res.get("result") or {}).get("value")

    def click(self, x: int, y: int) -> None:
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)

    def key(self, name: str) -> None:
        key, code, vk, text = CDP_KEYS[name]
        down = {"type": "keyDown", "text": text} if text else {"type": "rawKeyDown"}
        self.call("Input.dispatchKeyEvent", key=key, code=code, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, **down)
        self.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=code, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)

    def wheel(self, dy: int) -> None:
        vp = self.run("viewport")
        self.call("Input.dispatchMouseEvent", type="mouseWheel", x=vp["w"] // 2, y=vp["h"] // 2, deltaX=0, deltaY=dy)

    def shot(self, width: int = 1280, quality: int = 60) -> str:
        """What the screen shows (with her numbers on it), as a JPEG in base64. Taken from the
        browser's screencast, which never resizes the page (a scaled screenshot briefly does, and
        on a TV that shows as the picture jumping)."""
        self.call("Page.startScreencast", format="jpeg", quality=quality, maxWidth=width, maxHeight=width, everyNthFrame=1)
        try:
            f = self.event("Page.screencastFrame", 2.5)
            if f:
                self.notify("Page.screencastFrameAck", sessionId=f.get("sessionId"))
                return f["data"]
        finally:
            try:
                self.call("Page.stopScreencast")
            except CDPError:
                pass
        return self.call("Page.captureScreenshot", format="jpeg", quality=quality)["data"]   # unscaled: no resize


# ---- the live view -------------------------------------------------------------------------------
#
# While Kentrell has this screen's live panel open somewhere, a few frames a second go to Aurelia
# (and from there only to that panel; each lives ten seconds). Its own channel and its own browser
# connection, so it keeps going while a command (a sign-in) is running. Only the page on the
# screen is captured, never the rest of the desktop; password boxes show as dots, as on screen.

LIVE_WIDTH = int(os.environ.get("AURELIA_LIVE_WIDTH", "1280"))
LIVE_FPS = float(os.environ.get("AURELIA_LIVE_FPS", "3"))
LIVE_QUALITY = 55


def live_target() -> dict:
    """What the screen shows: the page she opened, or her own view when nothing else is open."""
    try:
        return SHELL.page()
    except ValueError:
        pages = SHELL.pages()
        if not pages:
            raise ValueError("my screen has nothing open")
        return next((t for t in pages if SHELL.is_hers(t)), pages[0])


def live_frame(tab: "Tab", wait: float) -> tuple[str, int, int] | None:
    """The newest frame of the browser's screencast (started by live_start), or None if the page
    hasn't changed. The screencast never resizes the page, unlike a scaled screenshot."""
    newest = None
    f = tab.event("Page.screencastFrame", wait)
    while f:
        tab.notify("Page.screencastFrameAck", sessionId=f.get("sessionId"))
        newest = f
        f = tab.event("Page.screencastFrame", 0)
    if not newest:
        return None
    m = newest.get("metadata") or {}
    return newest["data"], int(m.get("deviceWidth") or 0), int(m.get("deviceHeight") or 0)


def live_start(tab: "Tab") -> None:
    tab.call("Page.startScreencast", format="jpeg", quality=LIVE_QUALITY, maxWidth=LIVE_WIDTH, maxHeight=LIVE_WIDTH,
             everyNthFrame=1)


def live_stream(server: str, key: str, stop: threading.Event) -> None:
    tab, tab_id = None, None
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                if not (SHELL and SHELL.running()):
                    raise ValueError("my shell isn't running on this computer")
                t = live_target()
                if tab is None or t["id"] != tab_id:              # it switched pages: follow it
                    if tab:
                        tab.close()
                    tab, tab_id = Tab(t, timeout=8), t["id"]
                    live_start(tab)
                got = live_frame(tab, max(0.05, 1 / LIVE_FPS - (time.monotonic() - started)))
                u = urllib.parse.urlsplit(t.get("url") or "")
                body = {"title": str(t.get("title") or "")[:120], "site": "" if SHELL.is_hers(t) else (u.hostname or "")}
                if got:                                           # nothing new: the last frame stays
                    body.update(jpeg=got[0], w=got[1], h=got[2])
                if player_busy():
                    body["problem"] = "my player is on the screen (video isn't shown here)"
            except (CDPError, OSError, ValueError, KeyError) as exc:
                if tab:
                    tab.close()
                tab, tab_id = None, None
                body = {"problem": str(exc)[:160]}
            try:
                r = call(server, "/nodes/live/frame", body, key, timeout=10)
            except (Revoked, OSError, RuntimeError, ValueError):
                return
            if not r.get("keep"):                                 # nobody is watching any more
                return
            stop.wait(max(0.0, 1 / LIVE_FPS - (time.monotonic() - started)))
    finally:
        if tab:
            tab.close()


def live_loop(server: str, key: str, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            got = call(server, f"/nodes/live/wait?wait={POLL_WAIT}", key=key, timeout=POLL_WAIT + 20)
        except Revoked:
            return
        except (OSError, RuntimeError, ValueError):
            stop.wait(15)
            continue
        if got.get("want"):
            live_stream(server, key, stop)


def _tab() -> Tab:
    if not (SHELL and SHELL.running()):
        raise ValueError("my shell isn't running on this computer, so I can't see its screen")
    return Tab(SHELL.page())


def _clicked(tab: Tab, r: dict) -> None:
    if "x" in r:
        tab.click(r["x"], r["y"])


def _page_error(r: dict, n=None, text=None) -> None:
    err = r.get("error")
    if err == "stale":
        raise ValueError(f"there's no number {n} on the screen now (the page changed); look again for new numbers")
    if err == "notfound":
        raise ValueError(f"I don't see \"{text}\" on the screen" if text else "say which one, by its number")
    if err == "changed":
        raise ValueError(f"the page changed (that's \"{r.get('label')}\" now), so I didn't press anything")
    if err == "secret":
        raise ValueError(SECRET_SAY.get(r.get("what"), SECRET_SAY["password"]))
    if err == "notfield":
        raise ValueError(f"number {n} (\"{r.get('label')}\") isn't a text box")
    if err:
        raise ValueError(f"that didn't work on this page ({err})")


SECRET_SAY = {"password": "that's a password box; I don't type passwords (a login you saved on this computer can sign in "
                         "instead)",
              "payment": "that's a payment box (card details); I don't type into those"}
CODE = re.compile(r"^[A-Za-z0-9]{4,10}$")


def _words(args: dict) -> list:
    w = args.get("confirm_words")
    return [str(x) for x in w][:100] if isinstance(w, list) and w else DEFAULT_CONFIRM


def _settle(tab: Tab, wait: float = 0.9) -> dict:
    time.sleep(wait)
    try:
        return tab.run("state") or {}
    except CDPError:                                               # mid-navigation
        time.sleep(1)
        tab.ctx = None
        try:
            return tab.run("state") or {}
        except CDPError:
            return {}


def do_page_look(args: dict) -> dict:
    with _tab() as tab:
        SHELL.token = os.urandom(4).hex()
        out = tab.run("look", {"token": SHELL.token})
        time.sleep(0.15)
        try:
            out["shot"] = tab.shot()
        except CDPError:
            pass
        return out


def do_page_click(args: dict) -> dict:
    n = int(args["n"]) if str(args.get("n") or "").strip().isdigit() else None
    text = " ".join(str(args.get("text") or "").split())[:200] or None
    if not (n or text):
        raise ValueError("say which one: its number, or the words on it")
    with _tab() as tab:
        r = tab.run("click", {"token": SHELL.token, "n": n, "text": text, "words": _words(args),
                              "allow": bool(args.get("allow")), "expect": args.get("expect")})
        if r.get("risky"):
            raise NeedsConfirmation(r["risky"], "click")
        _page_error(r, n, text)
        _clicked(tab, r)
        return {"clicked": r["label"], "others": r.get("others", 0), **_settle(tab)}


def do_page_type(args: dict) -> dict:
    text = str(args.get("text") or "")[:2000]
    if not text:
        raise ValueError("what should I type?")
    n = int(args["n"]) if str(args.get("n") or "").strip().isdigit() else None
    with _tab() as tab:
        if n:
            r = tab.run("focus", {"token": SHELL.token, "n": n})
            _page_error(r, n)
            _clicked(tab, r)
            time.sleep(0.2)
        f = tab.run("field", {"clear": args.get("clear", True) is not False, "words": _words(args)})
        if f.get("error") == "nofield":
            raise ValueError("no text box is selected; say which one (by its number)")
        _page_error(f)
        if f.get("kind") == "code":                           # the code a site sent him, as he said it
            code = re.sub(r"[\s-]", "", text)
            if not CODE.match(code):
                raise ValueError("that's a box for the code the site sent you; tell me just the code (like 123456)")
            text = code
            for ch in code:                                   # one at a time: split boxes move to the next
                tab.call("Input.insertText", text=ch)
                time.sleep(0.08)
        else:
            tab.call("Input.insertText", text=text)
        out = {"typed": text, "into": f.get("label") or "the text box"}
        if args.get("enter"):
            if f.get("enter_risk"):
                raise NeedsConfirmation(f["enter_risk"], "enter")
            tab.key("enter")
            out.update(_settle(tab))
        return out


def do_page_key(args: dict) -> dict:
    name = " ".join(str(args.get("key") or "").lower().replace("pageup", "page up").replace("pagedown", "page down").split())
    name = {"return": "enter", "esc": "escape", "spacebar": "space", "delete": "backspace"}.get(name, name)
    if name not in CDP_KEYS:
        raise ValueError(f"I can press {', '.join(CDP_KEYS)}")
    with _tab() as tab:
        if name == "enter":
            r = tab.run("enter", {"words": _words(args), "allow": bool(args.get("allow")), "expect": args.get("expect")})
            if r.get("risky"):
                raise NeedsConfirmation(r["risky"], "enter")
            _page_error(r)
        tab.key(name)
        return {"pressed": name, **_settle(tab, 0.5)}


def do_page_scroll(args: dict) -> dict:
    where = str(args.get("direction") or "down").lower()
    where = {"to the top": "top", "to the bottom": "bottom", "top": "top", "bottom": "bottom", "up": "up"}.get(where, "down")
    with _tab() as tab:
        if where in ("up", "down"):
            vp = tab.run("viewport")
            tab.wheel(int(vp["h"] * 0.8) * (-1 if where == "up" else 1))
        else:
            tab.run("scroll_to", {"to": where})
        time.sleep(0.8)
        SHELL.token = os.urandom(4).hex()
        return {"scrolled": where, **tab.run("look", {"token": SHELL.token})}


def do_page_back(args: dict) -> dict:
    with _tab() as tab:
        h = tab.call("Page.getNavigationHistory")
        i = int(h.get("currentIndex", 0))
        if i <= 0:
            raise ValueError("there's no earlier page here; say \"go home\" to close it")
        entry = h["entries"][i - 1]
        tab.call("Page.navigateToHistoryEntry", entryId=entry["id"])
        time.sleep(1.2)
        return {"title": entry.get("title") or "", "url": entry.get("url") or ""}


MEDIA = ("play", "pause", "toggle", "forward", "back", "seek", "restart", "mute", "unmute", "volume", "speed",
         "fullscreen", "exit_fullscreen")


def do_page_media(args: dict) -> dict:
    action = str(args.get("action") or "")
    if player_busy() and action in PLAYER_STEP_ACTIONS + ("fullscreen", "exit_fullscreen"):   # her player is on top
        if action in ("fullscreen", "exit_fullscreen"):
            return {"player": player().status()}                       # it's always full screen
        return {"player": player().control(action, args)}
    if action not in MEDIA:
        raise ValueError(f"I can {', '.join(MEDIA)}")
    with _tab() as tab:
        tab.run("hide")
        r = tab.run("media", {"action": action, "seconds": float(args.get("seconds") or 0),
                              "level": args.get("level"), "rate": args.get("rate")})
        if r.get("error") == "novideo":
            raise ValueError("there's no video on this page")
        _page_error(r)
        return r


# ---- sign-ins he saved on this computer (strict) ----------------------------------------------------
#
# His decision (Sep 30): Aurelia may sign in to a site for him, with a login he saved HERE, on this
# computer, himself (`aurelia_node.py login add youtube`). The file is readable only by this account
# and is refused if anyone else could read it. The values never leave this agent: Aurelia (the server
# and the model) only ever asks "sign in to youtube" and hears back how it went. The agent fills a
# login only into its own site's sign-in page (by exact origin), never a lookalike.

LOGIN_SITES = {
    "youtube": {"label": "YouTube (Google)", "origins": ["https://accounts.google.com"],
                "start": "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"},
    "google": {"label": "Google", "origins": ["https://accounts.google.com"], "start": "https://accounts.google.com/"},
    "crunchyroll": {"label": "Crunchyroll", "origins": ["https://sso.crunchyroll.com"],
                    "start": "https://sso.crunchyroll.com/login"},
    "netflix": {"label": "Netflix", "origins": ["https://www.netflix.com"], "start": "https://www.netflix.com/login"},
}


def logins_file() -> Path:
    return config_dir() / "logins.json"


def read_logins() -> dict:
    f = logins_file()
    if not f.exists():
        return {}
    if not IS_WINDOWS and f.stat().st_mode & 0o077:
        raise ValueError("the saved logins file can be read by other accounts, so I won't use it "
                         f"(fix: chmod 600 {f})")
    try:
        data = json.loads(f.read_text())
    except ValueError:
        raise ValueError("the saved logins file is damaged; add the logins again")
    return data if isinstance(data, dict) else {}


def save_logins(data: dict) -> None:
    f = logins_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out)
    if not IS_WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, f)


def _origin(url: str) -> str:
    u = urllib.parse.urlsplit(url or "")
    return f"{u.scheme}://{u.netloc}".lower() if u.scheme and u.netloc else ""


def do_logins(args: dict) -> dict:
    """Which sites have a saved login here: names and sign-in pages only, never the values."""
    return {"logins": [{"site": k, "label": v.get("label") or k, "origins": v.get("origins") or []}
                       for k, v in sorted(read_logins().items()) if isinstance(v, dict)]}


def do_sign_in(args: dict) -> dict:
    site = " ".join(str(args.get("site") or "").lower().split())
    entry = read_logins().get(site)
    if not isinstance(entry, dict) or not entry.get("user") or not entry.get("password"):
        raise ValueError(f"there's no saved login for {site or 'that site'} on this computer "
                         f"(he adds it here: aurelia_node.py login add {site or '<site>'})")
    origins = {_origin(o) for o in entry.get("origins") or []} - {""}
    if not origins:
        raise ValueError(f"the saved login for {site} doesn't say which sign-in page it's for")
    if not (SHELL and SHELL.running()):
        raise ValueError("my shell isn't running on this computer, so I can't see its screen")
    try:
        on_screen = SHELL.page()
    except ValueError:
        on_screen = None
    if (on_screen is None or _origin(on_screen.get("url", "")) not in origins) and entry.get("start"):
        SHELL.open(entry["start"])                            # its own sign-in page
        time.sleep(4)
    filled, label = [], entry.get("label") or site
    with _tab() as tab:
        for _ in range(5):                                    # email, then password (one page or two)
            url = (tab.run("state") or {}).get("url", "")
            if _origin(url) not in origins:
                if filled:
                    break                                     # it left the sign-in page: signed in
                raise ValueError(f"the page on screen isn't {label}'s sign-in page, so I didn't fill anything")
            f = tab.run("login_fields") or {}
            kind = "password" if f.get("password") and "password" not in filled else \
                   "user" if f.get("user") and "user" not in filled else None
            if kind is None and f.get("use_password") and "password" not in filled and "use_password" not in filled:
                r = tab.run("click", {"token": "", "text": f["use_password"], "allow": True})   # "Log in with a password"
                if r.get("error"):
                    break
                _clicked(tab, r)
                filled.append("use_password")
                time.sleep(2.5)
                continue
            if kind is None:
                break
            if (tab.run("focus_login", {"kind": kind}) or {}).get("error"):
                break
            tab.call("Input.insertText", text=str(entry["password" if kind == "password" else "user"]))
            tab.key("enter")
            filled.append(kind)
            time.sleep(3.5)
        still = _origin((tab.run("state") or {}).get("url", "")) in origins
        boxes = (tab.run("login_fields") or {}) if still else {}
    filled = [k for k in filled if k != "use_password"]
    if not filled:
        raise ValueError(f"I couldn't find the sign-in boxes on {label}'s page")
    if not still:
        status = "signed in"                                  # the site took him back to where he was going
    elif boxes.get("code"):
        status = "it's asking for the code it sent you (check your email or texts); tell me the code and I'll type it in"
    elif "password" not in filled:
        status = "the email went in, but the password box never came"
    elif boxes.get("password"):
        status = "the password didn't go through (the saved one may be out of date)"
    else:
        status = "waiting for you: it may want your phone (2-step) or a code"
    return {"site": site, "label": label, "status": status, "filled": filled}      # never the values


def do_page_clear(args: dict) -> dict:
    with _tab() as tab:
        tab.run("clear")
        return {}


COMMANDS.update({"logins": do_logins, "sign_in": do_sign_in, "play": do_play, "packages_find": do_packages_find,
                 "package_check_remove": do_package_check_remove, "package_install": do_package_install,
                 "package_remove": do_package_remove, "clipboard_get": do_clipboard_get,
                 "clipboard_set": do_clipboard_set, "mode": do_mode, "youtube_feed": do_youtube_feed,
                 "spotify_speaker": do_spotify_speaker})
COMMANDS.update({"page_look": do_page_look, "page_click": do_page_click, "page_type": do_page_type,
                 "page_key": do_page_key, "page_scroll": do_page_scroll, "page_back": do_page_back,
                 "page_media": do_page_media, "page_clear": do_page_clear})


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
    maybe_update(server, key, hello.get("agent") or {})


def maybe_update(server: str, key: str, want: dict) -> None:
    """A newer agent is out: take it now, unless something is playing (then at the next chance)."""
    if want.get("version") and want["version"] != VERSION and not player_busy():
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

def cmd_pair(server: str, name: str, window: bool = False) -> int:
    server = server.rstrip("/")
    info = machine_info()
    if window:
        info["window"] = True                                # her window here is approved with it: one approval
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
            if s.get("window_token"):
                save_window_token(s["window_token"])
            print(f"  Paired as {s['name']}." + (" My window here is paired too." if s.get("window_token") else ""))
            return 0
        if s["status"] in ("declined", "expired", "revoked"):
            print(f"  Not paired: the request was {s['status']}.")
            return 1
    print("  The code expired. Run pair again.")
    return 1


def cmd_run() -> int:
    global SHELL, SERVER_URL, NODE_KEY
    m = read_mode()
    if m.get("ended") == "exam":
        write_mode("normal")
    elif m.get("mode") == "exam":
        log("exam mode: staying stopped until it ends")      # nothing of Aurelia runs meanwhile
        return EXIT_PAUSED
    cfg, key = load()
    server, backoff, said_hello_at = cfg["server"], 2.0, 0.0
    SERVER_URL, NODE_KEY = server, key
    log(f"Aurelia node {VERSION} for {cfg['name']} -> {server}")
    if cfg.get("shell"):
        SHELL = Shell(server, key)
        threading.Thread(target=SHELL.supervise, daemon=True).start()
        threading.Thread(target=live_loop, args=(server, key, SHELL.stop_event), daemon=True).start()
    if not cfg.get("shell"):                                  # a desktop someone games on (her shell isn't a desktop)
        threading.Thread(target=auto_gaming_loop, args=(threading.Event(),), daemon=True).start()
    if not IS_WINDOWS and shutil.which("librespot"):          # this computer as a Spotify speaker, named after it
        try:
            SPEAKER.start(cfg.get("name") or socket.gethostname())
        except ValueError as exc:
            log("spotify speaker:", exc)
        threading.Thread(target=SPEAKER.supervise, args=(cfg.get("name") or socket.gethostname(), threading.Event()),
                         daemon=True).start()
    while True:
        try:
            if time.time() - said_hello_at > HELLO_SECONDS or (SHELL and SHELL.changed.is_set()) or MODE_CHANGED.is_set():
                if SHELL:
                    SHELL.changed.clear()
                MODE_CHANGED.clear()
                hello = call(server, "/nodes/hello", {"info": machine_info(), "specs": specs(), "vitals": vitals()}, key)
                said_hello_at = time.time()
                log("connected")
                after_hello(server, key, hello)
            got = call(server, f"/nodes/commands?wait={POLL_WAIT}", key=key, timeout=POLL_WAIT + 20)
            backoff = 2.0
            for cmd in got.get("commands", []):
                log("command:", cmd.get("kind"))
                result = execute(cmd)
                call(server, f"/nodes/commands/{cmd['id']}/result", result, key)
                if STOP_AFTER_REPLY.is_set():                        # exam mode: he has the answer; now stop
                    log("exam mode: stopping")
                    if SHELL:
                        SHELL.stop_event.set()
                        SHELL.stop()
                    if player_busy():
                        player().control("stop", {})
                    return EXIT_PAUSED
            maybe_update(server, key, got.get("agent") or {})        # seen within a poll, not ten minutes
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


def cmd_mode(mode: str, hours: float) -> int:
    """On this computer itself: the Start-menu shortcuts and the terminal."""
    if mode == "normal":
        was = read_mode().get("mode")
        end_exam() if was == "exam" else write_mode("normal")
        print("Back to normal." + (" Starting again." if was == "exam" else ""))
        return 0
    if mode == "gaming":
        write_mode("gaming")
        print("Gaming mode: I keep out of the way until you set it back (mode normal).")
        return 0
    try:
        schedule_return(hours * 3600)
    except ValueError as exc:
        print(f"Not in exam mode: {exc}.")
        return 1
    write_mode("exam", time.time() + hours * 3600)
    if IS_WINDOWS:
        _win_tasks("Stop-ScheduledTask -TaskName $env:AUR_TASK -ErrorAction SilentlyContinue", {"AUR_TASK": TASK_NAME})
    elif shutil.which("systemctl"):
        _run_full(["systemctl", "--user", "stop", "aurelia-node.service"], 15)
    print(f"Exam mode for {hours:g} h: nothing of mine runs here until then (mode normal ends it early).")
    return 0


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


def cmd_login(action: str, site: str, origin: str, start: str) -> int:
    """Logins live only here. Added by him, at this computer; the password is never shown or sent."""
    import getpass
    site = " ".join((site or "").lower().split())
    try:
        data = read_logins()
    except ValueError as exc:
        print(f"Not using the saved logins: {exc}.")
        return 1
    if action == "list":
        if not data:
            print("No saved logins on this computer.")
        for k, v in sorted(data.items()):
            print(f"  {k}: {v.get('label') or k} — fills only on {', '.join(v.get('origins') or [])}")
        return 0
    if not site:
        print("Which site? e.g. login add youtube")
        return 1
    if action == "remove":
        if data.pop(site, None) is None:
            print(f"No saved login for {site}.")
            return 1
        save_logins(data)
        print(f"Removed the login for {site}.")
        return 0
    known = LOGIN_SITES.get(site, {})
    origins = [origin] if origin else known.get("origins")
    if not origins or not all(_origin(o) for o in origins):
        print(f"{site} isn't a site I know; give its sign-in page: login add {site} --origin https://login.example.com "
              "[--start https://login.example.com/signin]")
        return 1
    user = input(f"{known.get('label') or site} email or username: ").strip()
    password = getpass.getpass("Password (not shown): ")
    if not user or not password:
        print("Nothing saved.")
        return 1
    data[site] = {"label": known.get("label") or site, "origins": [_origin(o) for o in origins],
                  "start": start or known.get("start") or origins[0], "user": user, "password": password}
    save_logins(data)
    print(f"Saved. Aurelia can sign in to {data[site]['label']} on this computer (she never sees it); "
          f"it fills only on {', '.join(data[site]['origins'])}.")
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
    p.add_argument("--window", action="store_true", help="also pair my window on this computer (one approval)")
    sub.add_parser("run", help="connect and wait for work (what the service runs)")
    sh = sub.add_parser("shell", help="make this computer's screen Aurelia (on) or not (off)")
    sh.add_argument("state", choices=["on", "off"])
    rl = sub.add_parser("role", help="what this computer is for (a suggestion; the devices panel decides)")
    rl.add_argument("role", choices=[*NODE_ROLES, "auto"])
    sub.add_parser("status")
    sub.add_parser("forget")
    lg = sub.add_parser("login", help="logins Aurelia may use on this computer (add, list, remove)")
    lg.add_argument("action", choices=["add", "list", "remove"])
    lg.add_argument("site", nargs="?", default="")
    lg.add_argument("--origin", default="", help="the sign-in page's address (for sites she doesn't know)")
    lg.add_argument("--start", default="", help="where to open to sign in, if not the origin itself")
    md = sub.add_parser("mode", help="normal, gaming, or exam (stops me completely; --hours, default 3)")
    md.add_argument("mode", choices=list(MODES))
    md.add_argument("--hours", type=float, default=3)
    sub.add_parser("exam-end", help="(the timer's) end exam mode and start again")
    sub.add_parser("app", help="open her window on this computer (or bring it to the front)")
    a = ap.parse_args(argv)
    if a.cmd == "mode":
        return cmd_mode(a.mode, a.hours)
    if a.cmd == "exam-end":
        end_exam()
        return 0
    if a.cmd == "app":
        return cmd_app()
    if a.cmd == "login":
        return cmd_login(a.action, a.site, a.origin, a.start)
    if a.cmd == "pair":
        return cmd_pair(a.server, a.name, a.window)
    if a.cmd == "shell":
        return cmd_shell(a.state)
    if a.cmd == "role":
        return cmd_role(a.role)
    return {"run": cmd_run, "status": cmd_status, "forget": cmd_forget}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
