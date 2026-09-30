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

VERSION = "0.5.1"
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
            "home": do_home, "health": do_health, "processes": do_processes, "kill": do_kill}


def execute(cmd: dict) -> dict:
    fn = COMMANDS.get(cmd.get("kind"))
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
  const secret = e => { const auto = (e.getAttribute("autocomplete") || "").toLowerCase();
    const said = [e.name, e.id, e.getAttribute("aria-label"), e.getAttribute("placeholder"), labelOf(e)].join(" ");
    return (e.getAttribute("type") || "").toLowerCase() === "password" || /password|cc-|one-time-code/.test(auto) ||
      /pass(?:word|code)?\b|\bpin\b|cvv|cvc|security code|card ?number|\bssn\b|social security/i.test(said); };
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
  if (op === "state") return { title: clean(D.title), url: location.href, video: vstate(videos()) };
  if (op === "viewport") return { w: innerWidth, h: innerHeight, x: scrollX, y: scrollY };
  if (op === "click" || op === "focus") {
    let e = null, others = 0;
    if (a.n) { e = find(a.n); if (!e) return { error: "stale" }; }
    else if (a.text) { const f = byText(a.text); if (f) { e = f.e; others = f.others; } }
    if (!e) return { error: "notfound" };
    const label = labelOf(e);
    if (a.expect && norm(label) !== norm(a.expect)) return { error: "changed", label };
    if (op === "focus" && tag(e) === "input" && secret(e)) return { error: "secret", label };
    if (op === "focus" && !isField(e)) return { error: "notfield", label };
    if (op === "click" && !a.allow) { const r = risky(e); if (r) return { risky: r }; }
    const p = point(e);
    if (!p) { e.focus && e.focus(); if (op === "click") e.click(); return { label, others, done: true }; }
    return { label, others, x: p[0], y: p[1] };
  }
  if (op === "field") {
    const e = deepActive();
    if (!isField(e)) return { error: "nofield" };
    if (secret(e)) return { error: "secret" };
    if (a.clear) { if (e.isContentEditable) D.execCommand("selectAll"); else if (e.select) e.select(); }
    return { label: labelOf(e), enter_risk: enterRisk(e) };
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

    def shot(self, width: int = 1280) -> str:
        """What the screen shows (with her numbers on it), as a JPEG in base64."""
        vp = self.run("viewport")
        scale = min(1.0, width / max(1, vp["w"]))
        return self.call("Page.captureScreenshot", format="jpeg", quality=60,
                         clip={"x": vp["x"], "y": vp["y"], "width": vp["w"], "height": vp["h"], "scale": scale})["data"]


def _tab() -> Tab:
    if not (SHELL and SHELL.running()):
        raise ValueError("her shell isn't running on this computer, so I can't see its screen")
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
        raise ValueError("that's a password or payment box; I don't type into those")
    if err == "notfield":
        raise ValueError(f"number {n} (\"{r.get('label')}\") isn't a text box")
    if err:
        raise ValueError(f"that didn't work on this page ({err})")


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
        if f.get("error") == "secret":
            raise ValueError("that's a password or payment box; I don't type into those")
        _page_error(f)
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


def do_page_clear(args: dict) -> dict:
    with _tab() as tab:
        tab.run("clear")
        return {}


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
