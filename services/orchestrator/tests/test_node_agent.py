"""The node agent on its own: what it refuses, how it finds apps, and that it never uses a shell."""
import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("aurelia_node", Path(__file__).resolve().parents[1] / "app/node_agent/aurelia_node.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    d = tmp_path / "applications"
    d.mkdir()
    (d / "org.videolan.vlc.desktop").write_text("[Desktop Entry]\nType=Application\nName=VLC media player\nExec=/usr/bin/vlc %U\n")
    (d / "firefox.desktop").write_text("[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox %u\n")
    (d / "helper.desktop").write_text("[Desktop Entry]\nType=Application\nName=Helper\nNoDisplay=true\nExec=h\n")
    (d / "broken.desktop").write_text("not a desktop file")
    monkeypatch.setattr(agent, "LINUX_APP_DIRS", [str(d)])
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    return d


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "spawn", lambda argv: calls.append(argv))
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_only_known_commands_run():
    assert agent.execute({"kind": "run_shell", "args": {"cmd": "rm -rf ~"}}) == \
        {"ok": False, "error": "this node doesn't do 'run_shell'"}


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ssh://host", "https://", "not a url"])
def test_only_web_pages_open(url, spawned):
    out = agent.execute({"kind": "open_url", "args": {"url": url}})
    assert out == {"ok": False, "error": "only http and https pages"} and spawned == []


def test_a_page_opens_in_the_browser_without_a_shell(spawned, monkeypatch):
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    out = agent.execute({"kind": "open_url", "args": {"url": "https://www.youtube.com/watch?v=x; rm -rf ~"}})
    assert out["ok"] and spawned == [["xdg-open", "https://www.youtube.com/watch?v=x; rm -rf ~"]]   # one argument, never parsed


def test_installed_apps_skip_hidden_and_broken(apps_dir):
    names = sorted(a["name"] for a in agent.installed_apps())
    assert names == ["Firefox", "VLC media player"]


def test_apps_are_found_by_what_he_calls_them(apps_dir, spawned, monkeypatch):
    monkeypatch.setattr(agent, "_started", lambda app, before, wait=0: {"pid": 77, "name": "vlc"})
    assert agent.find_app("vlc")["id"] == "org.videolan.vlc"
    assert agent.find_app("VLC Media Player")["name"] == "VLC media player"
    assert agent.find_app("firefox")["name"] == "Firefox" and agent.find_app("steam") is None
    out = agent.execute({"kind": "launch_app", "args": {"name": "vlc"}})
    assert out == {"ok": True, "data": {"app": "VLC media player", "verified": True, "process": "vlc"}} and spawned == [["gtk-launch", "org.videolan.vlc"]]
    assert agent.execute({"kind": "launch_app", "args": {"name": "steam"}}) == {"ok": False, "error": "no app called steam is installed"}


def test_listing_apps_shares_names_not_command_lines(apps_dir):
    out = agent.execute({"kind": "list_apps", "args": {}})["data"]["apps"]
    assert {"name": "Firefox", "id": "firefox"} in out and all("path" not in a and "Exec" not in str(a) for a in out)


def test_the_key_is_readable_only_by_its_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    agent.save({"server": "https://a", "device_id": 3, "name": "TV"}, "aur_secret")
    key = tmp_path / "aurelia-node" / "key"
    assert key.read_text().strip() == "aur_secret" and oct(key.stat().st_mode & 0o777) == "0o600"
    assert agent.load() == ({"server": "https://a", "device_id": 3, "name": "TV"}, "aur_secret")


def test_what_it_says_about_this_machine_is_plain():
    info = agent.machine_info()
    assert info["os"] and info["hostname"] and info["agent_version"] == agent.VERSION
    assert all(isinstance(v, (str, int, float, bool)) for v in info.values())


def test_the_installers_parse():
    import subprocess
    here = Path(__file__).resolve().parents[1]
    repo = here.parents[1] if len(here.parents) > 1 else here          # the repo's scripts aren't in the test image
    scripts = [here / "app/node_agent/install-linux.sh"] + [p for p in (repo / "scripts/pair.sh", repo / "scripts/ask.sh") if p.exists()]
    for script in scripts:
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, (script.name, r.stderr)


# ---- the shell and updating itself (step 3) -----------------------------------------------------

def test_the_shell_only_takes_her_session(monkeypatch):
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    sh = agent.Shell("https://aurelia.example:8443/", "aur_k")
    monkeypatch.setattr(agent, "session_env", lambda: {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})
    assert not sh.wanted()                                           # GNOME stays GNOME
    monkeypatch.setattr(agent, "session_env", lambda: {"XDG_CURRENT_DESKTOP": "Aurelia"})
    assert sh.wanted()


def test_the_kiosk_is_hers_only_on_this_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    argv = agent.Shell("https://a:8443", "k").argv("/usr/bin/chromium", "https://a:8443/pair/screen?t=x")
    assert argv[0] == "/usr/bin/chromium" and "--kiosk" in argv and argv[-1] == "https://a:8443/pair/screen?t=x"
    assert "--remote-debugging-address=127.0.0.1" in argv and f"--remote-debugging-port={agent.DEBUG_PORT}" in argv
    assert any(a.startswith("--user-data-dir=") and "aurelia-node" in a for a in argv)          # its own profile
    snap = agent.Shell("https://a", "k").profile("/snap/bin/chromium")
    assert "snap/chromium/common" in str(snap)                                                # where a snap may write


class Resp:
    def __init__(self, data): self.data = data
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A copy of the agent in its own folder, as a node would have it."""
    d = tmp_path / "app"
    d.mkdir()
    here = d / "aurelia_node.py"
    here.write_bytes(Path(agent.__file__).read_bytes())
    monkeypatch.setattr(agent, "__file__", str(here))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    return here


def release(version="9.9.9", tamper=False):
    import hashlib as h
    data = Path(agent.__file__).read_bytes().replace(f'VERSION = "{agent.VERSION}"'.encode(), f'VERSION = "{version}"'.encode())
    return data, {"version": version, "sha256": h.sha256(b"x" if tamper else data).hexdigest(), "max_attempts": 3}


def test_an_update_is_checked_then_swapped_with_the_old_one_kept(installed, monkeypatch):
    data, want = release()
    old = installed.read_bytes()
    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda req, timeout=30: Resp(data))
    with pytest.raises(SystemExit) as out:
        agent.update("https://a", "k", want)
    assert out.value.code == agent.EXIT_UPDATED
    assert installed.read_bytes() == data and installed.with_suffix(".py.prev").read_bytes() == old
    assert agent.read_state()["to"] == "9.9.9" and agent.read_state()["from"] == agent.VERSION


def test_a_bad_download_is_never_installed_and_three_failures_are_reported_once(installed, monkeypatch):
    data, want = release(tamper=True)
    old = installed.read_bytes()
    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda req, timeout=30: Resp(data))
    reports = []
    monkeypatch.setattr(agent, "call", lambda server, path, body=None, key=None, timeout=30: reports.append((path, body)))
    for _ in range(5):
        agent.update("https://a", "k", want)
    assert installed.read_bytes() == old and agent.read_state()["attempts"] == {"9.9.9": 3}
    assert reports == [("/nodes/update-failed", {"version": "9.9.9", "attempts": 3,
                                                  "error": "the download didn't match Aurelia's checksum"})]


def test_retry_starts_the_count_again(installed, monkeypatch):
    agent.write_state({"attempts": {"9.9.9": 3}, "reported": "9.9.9"})
    calls = []
    monkeypatch.setattr(agent, "update", lambda s, k, w: calls.append(w["version"]))
    agent.after_hello("https://a", "k", {"retry_update": True, "agent": {"version": "9.9.9", "sha256": "x"}})
    assert "attempts" not in agent.read_state() and calls == ["9.9.9"]


def test_the_guard_puts_the_old_agent_back_when_the_new_one_never_reconnects(tmp_path, monkeypatch):
    import subprocess
    d = tmp_path / "app"
    d.mkdir()
    guard_src = Path(agent.__file__).resolve().parent / "aurelia_node_guard.py"
    (d / "aurelia_node_guard.py").write_bytes(guard_src.read_bytes())
    (d / "aurelia_node.py").write_text("import sys; print('new'); sys.exit(1)\n")        # the broken new version
    (d / "aurelia_node.py.prev").write_text("import sys; print('old'); sys.exit(0)\n")
    cfg = tmp_path / "cfg" / "aurelia-node"
    cfg.mkdir(parents=True)
    (cfg / "update.json").write_text('{"from": "0.3.0", "to": "0.4.0", "boots": 0}')
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "PATH": "/usr/bin:/bin"}
    outs = [subprocess.run([sys.executable, str(d / "aurelia_node_guard.py"), "run"], env=env,
                           capture_output=True, text=True).stdout.strip() for _ in range(4)]
    assert outs == ["new", "new", "new", "old"]                      # three tries, then the old one is back
    import json as j
    assert j.loads((cfg / "update.json").read_text()) == {"rolled_back": "0.4.0"}


def test_a_rollback_counts_as_a_failed_try(installed, monkeypatch):
    agent.write_state({"rolled_back": "0.4.0"})
    monkeypatch.setattr(agent, "update", lambda s, k, w: None)
    agent.after_hello("https://a", "k", {"agent": {"version": "0.4.0", "sha256": "x"}})
    st = agent.read_state()
    assert st["attempts"] == {"0.4.0": 1} and "rolled_back" not in st and "reconnect" in st["last_error"]


# ---- step 4: the page on her screen ---------------------------------------------------------------

def test_page_commands_need_her_shell(monkeypatch):
    monkeypatch.setattr(agent, "SHELL", None)
    for kind in ("page_look", "page_click", "page_type", "page_key", "page_scroll", "page_back", "page_media"):
        out = agent.execute({"kind": kind, "args": {"n": 1, "text": "x", "key": "enter", "action": "play"}})
        assert out == {"ok": False, "error": "my shell isn't running on this computer, so I can't see its screen"}


def test_a_press_that_buys_or_sends_comes_back_undone(monkeypatch):
    def risky(args):
        raise agent.NeedsConfirmation("Place order", "click")
    monkeypatch.setitem(agent.COMMANDS, "page_click", risky)
    assert agent.execute({"kind": "page_click", "args": {"n": 6}}) == \
        {"ok": False, "error": "needs confirmation: Place order", "data": {"confirm": "Place order", "what": "click"}}


def test_the_page_script_is_fixed_and_gets_only_data(monkeypatch):
    """What runs in the page is PAGE_JS from this file; the server's args go in as a JSON value."""
    sent = []

    class FakeTab(agent.Tab):
        def __init__(self):
            self.ctx = 7

        def call(self, method, **params):
            sent.append((method, params))
            return {"result": {"value": {"ok": 1}}}
    tab = FakeTab()
    tab.run("click", {"text": "\"); fetch('https://evil'); (\""})
    method, params = sent[-1]
    assert method == "Runtime.evaluate" and params["contextId"] == 7 and params["returnByValue"]
    expr = params["expression"]
    assert expr.startswith(f"({agent.PAGE_JS})(") and expr.endswith(")")
    tail = expr[len(agent.PAGE_JS) + 3:-1]
    import json
    op, args = json.loads("[" + tail + "]")
    assert op == "click" and args == {"text": "\"); fetch('https://evil'); (\""}


def test_it_only_talks_to_a_local_control_port():
    with pytest.raises(agent.CDPError):
        agent.Tab({"id": "x", "webSocketDebuggerUrl": "ws://10.0.0.5:9333/devtools/page/x"})


def test_websocket_masking_round_trips():
    data, mask = bytes(range(256)) * 3 + b"xyz", b"\x01\x02\x03\x04"
    assert agent.Tab._mask(agent.Tab._mask(data, mask), mask) == data


def test_keys_and_media_actions_are_a_fixed_set(monkeypatch):
    class S:
        def running(self):
            return True
    monkeypatch.setattr(agent, "SHELL", S())
    assert "I can press" in agent.execute({"kind": "page_key", "args": {"key": "ctrl+w"}})["error"]
    assert agent.execute({"kind": "page_media", "args": {"action": "delete"}})["error"].startswith("I can play")


# ---- node reporting ----------------------------------------------------------------------------------

@pytest.fixture
def fake_linux(tmp_path, monkeypatch):
    proc, sys_ = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    (proc / "stat").write_text("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 100 0 50 800 50 0 0 0 0 0\nbtime 1790000000\n")
    (proc / "meminfo").write_text("MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\nSwapTotal:       2000000 kB\n"
                                  "SwapFree:        1000000 kB\n")
    (proc / "cpuinfo").write_text("processor\t: 0\nmodel name\t: Intel(R) N100\n")
    (proc / "uptime").write_text("7200.5 100.0\n")
    (proc / "mounts").write_text("")
    (proc / "net" / "dev").write_text("h1\nh2\n  lo: 1 0 0 0 0 0 0 0 1 0\n eth0: 1000 0 0 0 0 0 0 0 500 0\n")

    def pid(n, name, uid, state="S", start=500, ticks=(30, 20)):
        d = proc / str(n)
        d.mkdir()
        f = [state, "1"] + ["0"] * 9 + [str(ticks[0]), str(ticks[1])] + ["0"] * 6 + [str(start), "0", "25"]
        (d / "stat").write_text(f"{n} ({name}) " + " ".join(f) + "\n")
        (d / "status").write_text(f"Name:\t{name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    pid(1, "systemd", 0, start=1)
    pid(200, "Web Content (x)", 1000, start=900)                  # names can hold spaces and parens
    pid(201, "chrome", 1000, start=950)
    pid(202, "chrome", 1000, start=960)
    pid(203, "defunct", 1000, state="Z")
    hw = sys_ / "class" / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text("71000\n")
    (hw / "temp1_label").write_text("Package id 0\n")
    bat = sys_ / "class" / "power_supply" / "BAT0"
    bat.mkdir(parents=True)
    (bat / "type").write_text("Battery\n")
    (bat / "capacity").write_text("42\n")
    (bat / "status").write_text("Discharging\n")
    cpu = sys_ / "devices" / "system" / "cpu" / "cpu0" / "cpufreq"
    cpu.mkdir(parents=True)
    (cpu / "scaling_cur_freq").write_text("1200000\n")
    (cpu / "cpuinfo_max_freq").write_text("3400000\n")
    (sys_ / "class" / "dmi" / "id").mkdir(parents=True)
    (sys_ / "class" / "dmi" / "id" / "sys_vendor").write_text("AZW\n")
    (sys_ / "class" / "dmi" / "id" / "product_name").write_text("MINI S\n")
    monkeypatch.setattr(agent, "PROC", proc)
    monkeypatch.setattr(agent, "SYS", sys_)
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "_run", lambda argv, timeout=4: "")
    monkeypatch.setattr(agent, "SHELL", None)
    return proc


def test_it_reads_this_linux_machine(fake_linux):
    s = agent.linux_specs()
    assert s["cpu"] == "Intel(R) N100" and s["memory_gb"] == 16.4 and s["model"] == "AZW MINI S"
    procs = agent._linux_procs()
    assert procs[200]["name"] == "Web Content (x)" and procs[200]["ticks"] == 50 and procs[200]["uid"] == 1000
    assert 203 not in procs                                        # a zombie has already exited
    assert agent._linux_temps() == [{"sensor": "coretemp", "label": "Package id 0", "c": 71}]
    assert agent._linux_battery() == {"percent": 42, "charging": False}
    assert agent._linux_speed() == 35
    assert agent._net_bytes() == {"eth0": (1000, 500)}


def test_programs_are_grouped_with_their_processes():
    rows = [{"name": "chrome", "cpu_percent": 30.0, "memory_mb": 500, "mine": True},
            {"name": "chrome", "cpu_percent": 12.5, "memory_mb": 700, "mine": True},
            {"name": "sshd", "cpu_percent": 0.1, "memory_mb": 5, "mine": False}]
    g = agent._groups(rows)
    assert g[0] == {"name": "chrome", "count": 2, "cpu_percent": 42.5, "memory_mb": 1200, "mine": True}
    assert g[1]["mine"] is False


def test_closing_takes_only_the_exact_process_and_never_protected_ones(fake_linux, monkeypatch):
    killed = []
    monkeypatch.setattr(agent.os, "getuid", lambda: 1000)
    monkeypatch.setattr(agent.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(agent, "_alive", lambda pid, key: False)
    out = agent.do_kill({"targets": [{"pid": 1, "start_key": "1"},            # the init system
                                     {"pid": 201, "start_key": "123"},        # restarted since: not the one he saw
                                     {"pid": 999, "start_key": "5"},          # gone
                                     {"pid": 202, "start_key": "960"}]})      # this one
    assert killed == [(202, agent_signal("SIGTERM"))]
    assert out["closed"] == [{"pid": 202, "name": "chrome"}]
    why = {r["pid"]: r["why"] for r in out["refused"]}
    assert "part of the system" in why[1] and "isn't running anymore" in why[201] and "isn't running anymore" in why[999]
    monkeypatch.setattr(agent.os, "getuid", lambda: 1001)                    # someone else's
    killed.clear()
    out = agent.do_kill({"targets": [{"pid": 202, "start_key": "960"}], "force": True})
    assert killed == [] and out["refused"][0]["why"] == "it isn't yours"


def test_her_own_processes_are_protected(fake_linux, monkeypatch):
    monkeypatch.setattr(agent.os, "getpid", lambda: 201)
    assert agent._protected(201, "chrome") and agent._protected(1, "systemd") and not agent._protected(202, "chrome")
    assert agent._public({"pid": 201, "name": "chrome", "user": "k", "mine": True, "cpu_percent": 1, "memory_mb": 1,
                          "started": 0, "start_key": "950"})["protected"] is True


def agent_signal(name):
    import signal
    return getattr(signal, name)


def test_a_snap_browser_is_found_even_off_the_path(tmp_path, monkeypatch):
    """Ubuntu's Chromium is a snap in /snap/bin, which a user service's PATH usually leaves out."""
    monkeypatch.delenv("AURELIA_BROWSER", raising=False)
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    snap = tmp_path / "snap-bin"
    snap.mkdir()
    (snap / "chromium").write_text("#!/bin/sh\n")
    (snap / "chromium").chmod(0o755)
    monkeypatch.setattr(agent, "BROWSER_DIRS", [str(snap)])
    assert agent.find_browser() == str(snap / "chromium")


def test_in_her_wayland_session_chromium_draws_with_wayland(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "session_env", lambda: {"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland",
                                                        "XDG_CURRENT_DESKTOP": "Aurelia"})
    sh = agent.Shell("https://aurelia", "k")
    assert sh.wanted() and "--ozone-platform=wayland" in sh.argv("/usr/bin/chromium", "https://x")
    monkeypatch.setattr(agent, "session_env", lambda: {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "Aurelia"})
    assert "--ozone-platform=wayland" not in sh.argv("/usr/bin/chromium", "https://x")


def test_the_installer_makes_a_wayland_session_and_sets_it_through_the_account_service():
    text = (agent.Path(__file__).resolve().parents[1] / "app/node_agent/install-linux.sh").read_text()
    assert "apt-get install -y cage" in text and "/usr/share/wayland-sessions/aurelia.desktop" in text
    assert "exec cage -d -s --" in text and "SetSession s" in text and "matchbox" not in text
    assert ">/dev/null\n" not in text.split("apt-get install -y cage")[1][:5]          # its errors are shown


# ---- sign-ins saved on this computer -------------------------------------------------------------

def test_saved_logins_stay_private_and_are_never_listed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    answers = iter(["me@example.com"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hunter2-secret")
    assert agent.cmd_login("add", "YouTube", "", "") == 0
    f = tmp_path / "logins.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    saved = agent.read_logins()["youtube"]
    assert saved["origins"] == ["https://accounts.google.com"] and saved["password"] == "hunter2-secret"
    capsys.readouterr()
    agent.cmd_login("list", "", "", "")
    out = capsys.readouterr().out
    assert "youtube" in out and "hunter2" not in out and "me@example.com" not in out
    assert "hunter2" not in json.dumps(agent.do_logins({})) and "me@example" not in json.dumps(agent.do_logins({}))
    assert agent.cmd_login("add", "somesite", "", "") == 1          # an unknown site needs its sign-in page named
    f.chmod(0o644)                                                   # readable by others: refused outright
    with pytest.raises(ValueError, match="can be read by other accounts"):
        agent.read_logins()
    assert agent.execute({"kind": "sign_in", "args": {"site": "youtube"}})["ok"] is False


def test_signing_in_needs_a_saved_login(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    out = agent.execute({"kind": "sign_in", "args": {"site": "crunchyroll"}})
    assert out["ok"] is False and "no saved login for crunchyroll" in out["error"] and "login add crunchyroll" in out["error"]


import json  # noqa: E402


# ---- diagnostics: read-only checks, judged from what real Ubuntu prints -------------------------

class FakeSystem:
    """Answers the agent's fixed programs the way Ubuntu does; records every call (no shell, no writes)."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, timeout=6):
        self.calls.append(argv)
        for prefix, reply in self.answers.items():
            if " ".join(argv).startswith(prefix):
                return reply
        return 0, "", ""


@pytest.fixture
def ubuntu(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    (journal / "abc").mkdir(parents=True)
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    real_isdir = agent.Path.is_dir
    monkeypatch.setattr(agent.Path, "is_dir", lambda self: True if str(self) == "/var/log/journal" else real_isdir(self))
    monkeypatch.setattr(agent.Path, "iterdir", lambda self: iter([journal / "abc"]))
    def install(answers):
        fake = FakeSystem(answers)
        monkeypatch.setattr(agent, "_run_full", fake)
        monkeypatch.setattr(agent, "_run", lambda argv, timeout=4: fake(argv, timeout)[1])
        return fake
    return install


def test_failed_services_and_errors(ubuntu):
    fake = ubuntu({
        "systemctl list-units": (0, "snapd.service loaded failed failed Snap Daemon\n", ""),
        "systemctl --user list-units": (0, "aurelia-node.service loaded failed failed Aurelia\n", ""),
        "journalctl --no-pager -o short-iso -n 30 -b -p err": (0, "\n".join(
            f"2026-10-01T10:0{i}:00-0500 node1 gnome-shell[2106]: JS ERROR: thing {i}" for i in range(6)) +
            "\n2026-10-01T10:09:00-0500 node1 kernel: usb 1-2: device descriptor read/64, error -71\n", ""),
    })
    s = agent._diag_services()
    assert s["status"] == "bad" and "snapd.service" in s["summary"] and "aurelia-node.service (yours)" in s["lines"]
    e = agent._diag_errors()
    assert e["status"] == "info" and "gnome-shell ×6" in e["summary"] and len(e["lines"]) == 7
    assert all(c[0] in ("systemctl", "journalctl") for c in fake.calls)
    assert not any(w in " ".join(c) for c in fake.calls for w in ("restart", "start", "stop", "enable", "reset-failed"))


def test_out_of_memory_is_serious(ubuntu):
    ubuntu({"journalctl --no-pager -o short-iso -n 40 -k": (0, (
        "2026-10-01T09:00:00 node1 kernel: Out of memory: Killed process 4242 (chrome) total-vm:9000000kB\n"
        "2026-10-01T09:00:01 node1 kernel: oom-kill:constraint=CONSTRAINT_NONE\n"), "")})
    k = agent._diag_kernel()
    assert k["status"] == "bad" and "out of memory" in k["summary"].lower()


def test_tailscale_and_clock(ubuntu):
    ubuntu({"tailscale status --json": (0, '{"BackendState": "Running", "Self": {"Online": true}, '
                                           '"Peer": {"a": {"Online": true}, "b": {"Online": false}}}', ""),
            "timedatectl show": (0, "no\n", "")})
    t = agent._diag_tailscale()
    assert t["status"] == "ok" and "1 of 2" in t["summary"]
    assert agent._diag_time()["status"] == "warn"
    ubuntu({"tailscale status --json": (0, '{"BackendState": "NeedsLogin"}', "")})
    assert agent._diag_tailscale()["status"] == "bad"


def test_one_service_in_detail(ubuntu):
    ubuntu({"systemctl show NetworkManager.service": (0, "LoadState=loaded\nActiveState=active\nSubState=running\n"
                                                         "Result=success\nNRestarts=4\nDescription=Network Manager\n", ""),
            "journalctl --no-pager -o short-iso -n 30 -u NetworkManager.service": (0, "2026-10-01 node1 NetworkManager[812]: dhcp4 timeout\n", "")})
    c = agent._diag_unit("NetworkManager")
    assert c["status"] == "warn" and "restarted 4 time(s)" in c["summary"] and c["lines"]
    with pytest.raises(ValueError):
        agent._diag_unit("x; rm -rf /")
    ubuntu({"systemctl show": (0, "LoadState=not-found\n", ""), "systemctl --user show": (0, "LoadState=not-found\n", "")})
    assert agent._diag_unit("nothere")["summary"] == "No service by that name here"


def test_secrets_never_leave(ubuntu):
    line = agent._clean("sshd: password=hunter2 api_key: sk-abc Authorization Bearer eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop")
    assert "hunter2" not in line and "sk-abc" not in line and "eyJhbGci" not in line


def test_a_failing_check_never_hides_the_rest(ubuntu, monkeypatch):
    ubuntu({})
    monkeypatch.setattr(agent, "_diag_network", lambda server: 1 / 0)
    out = agent.do_diagnose({})
    ids = [c["id"] for c in out["checks"]]
    assert ids == list(agent.DIAG_CHECKS) and next(c for c in out["checks"] if c["id"] == "network")["status"] == "info"


# ---- power: restart only when confirmed, after the answer; did the last shutdown hang? ----------

def test_restart_needs_confirmation_and_happens_after_the_answer(monkeypatch):
    later = []
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "_later", lambda s, argv: later.append((s, argv)))
    with pytest.raises(ValueError, match="confirmation"):
        agent.do_power({"action": "restart"})
    monkeypatch.setattr(agent, "_logind_can", lambda what: "challenge")
    with pytest.raises(ValueError, match="--system"):
        agent.do_power({"action": "restart", "confirmed": True})
    monkeypatch.setattr(agent, "_logind_can", lambda what: "yes")
    out = agent.do_power({"action": "restart", "confirmed": True})
    assert later == [(3, ["systemctl", "reboot", "-i"])] and out["in_s"] == 3
    agent.do_power({"action": "sleep"})                                  # sleeping doesn't need it
    assert later[-1] == (3, ["systemctl", "suspend", "-i"])
    with pytest.raises(ValueError):
        agent.do_power({"action": "format the disk"})


def test_wake_packet(monkeypatch):
    sent = []

    class Sock:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def setsockopt(self, *a): pass
        def sendto(self, data, addr): sent.append((data, addr))
    monkeypatch.setattr(agent.socket, "socket", Sock)
    out = agent.do_wake({"mac": "AA-BB-CC-DD-EE-FF", "broadcast": "192.168.1.255"})
    assert out["to"] == ["192.168.1.255", "255.255.255.255"]
    data, addr = sent[0]
    assert addr == ("192.168.1.255", 9) and data == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16
    with pytest.raises(ValueError):
        agent.do_wake({"mac": "not a mac"})


@pytest.mark.parametrize("tail,status,words", [
    (["Oct 01 node1 systemd[1]: Stopping aurelia-node.service...",
      "Oct 01 node1 systemd[1]: A stop job is running for snapd.service (1min 30s / 1min 30s)",
      "Oct 01 node1 systemd[1]: snapd.service: State 'stop-sigterm' timed out. Killing."], "bad", "It hung waiting on snapd.service"),
    (["Oct 01 node1 systemd[1]: A stop job is running for cage.scope (20s)",
      "Oct 01 node1 systemd[1]: Reached target System Reboot.", "Oct 01 node1 systemd-shutdown[1]: Syncing filesystems"],
     "warn", "finished, but slowly, waiting on cage.scope"),
    (["Oct 01 node1 systemd[1]: Reached target System Reboot.", "Oct 01 node1 kernel: reboot: Restarting system"], "ok", "Finished cleanly"),
    (["Oct 01 node1 gnome-shell[2106]: JS ERROR: thing", "Oct 01 node1 kernel: usb 1-2: reset"], "warn", "without a clean shutdown"),
])
def test_last_shutdown(ubuntu, tail, status, words):
    ubuntu({"journalctl --no-pager -o short-iso -n 200 -b -1": (0, "\n".join(tail) + "\n", "")})
    c = agent._diag_shutdown()
    assert c["status"] == status and words in c["summary"]


# ---- sound and media players ---------------------------------------------------------------------

WPCTL_STATUS = """PipeWire 'pipewire-0' [1.0.5, kentrell@tv-pc, cookie:123]
 └─ Clients:
        33. pipewire                            [1.0.5, kentrell@tv-pc, pid:901]

Audio
 ├─ Devices:
 │      42. Built-in Audio                      [alsa]
 │      43. GA104 High Definition Audio         [alsa]
 │
 ├─ Sinks:
 │      49. Built-in Audio Analog Stereo        [vol: 0.40]
 │  *   55. GA104 HDMI / DisplayPort 1 Output   [vol: 0.65 MUTED]
 │
 ├─ Sink endpoints:
 │      70. Not a real output
 │
 ├─ Sources:
 │  *   50. Built-in Audio Analog Stereo        [vol: 1.00]
 │
 └─ Streams:
        88. Chromium

Video
 ├─ Devices:
 │      61. Integrated Camera                   [v4l2]
 │
 ├─ Sinks:
 │      62. A video sink, not sound
"""


def test_wpctl_outputs_are_only_audio_sinks():
    outs = agent.parse_wpctl(WPCTL_STATUS)
    assert outs == [{"id": "49", "name": "Built-in Audio Analog Stereo", "default": False},
                    {"id": "55", "name": "GA104 HDMI / DisplayPort 1 Output", "default": True}]


class Sound:
    """A fake PipeWire: records each command and answers like wpctl."""

    def __init__(self):
        self.calls, self.volume, self.muted, self.default = [], 0.65, True, "55"

    def __call__(self, argv, *a, **k):
        self.calls.append(argv)
        if argv[:2] == ["wpctl", "status"]:
            return 0, WPCTL_STATUS.replace(" *   55.", "     55.").replace("│      49.", "│      49.").replace(
                f"     {self.default}.", f" *   {self.default}."), ""
        if argv[:2] == ["wpctl", "get-volume"]:
            return 0, f"Volume: {self.volume:.2f}" + (" [MUTED]" if self.muted else ""), ""
        if argv[:2] == ["wpctl", "set-volume"]:
            v = argv[-1]
            self.volume = float(v) if "%" not in v else self.volume + (1 if v.endswith("+") else -1) * int(v[:-2]) / 100
            return 0, "", ""
        if argv[:2] == ["wpctl", "set-mute"]:
            self.muted = {"1": True, "0": False, "toggle": not self.muted}[argv[-1]]
            return 0, "", ""
        if argv[:2] == ["wpctl", "set-default"]:
            self.default = argv[-1]
            return 0, "", ""
        return 1, "", "unknown"


@pytest.fixture
def pipewire(monkeypatch):
    fake = Sound()
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "_run_env", fake)
    monkeypatch.setattr(agent.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("wpctl", "playerctl") else None)
    return fake


def test_sound_reads_and_sets(pipewire):
    got = agent.do_sound({"action": "get"})
    assert got["volume"] == 65 and got["muted"] and got["output"] == "GA104 HDMI / DisplayPort 1 Output"
    assert agent.do_sound({"action": "set", "level": 30})["volume"] == 30
    assert ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.30"] in pipewire.calls
    agent.do_sound({"action": "up"})
    assert ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "10%+"] in pipewire.calls   # never past 100%
    assert agent.do_sound({"action": "unmute"})["muted"] is False
    assert agent.do_sound({"action": "set", "level": 400})["volume"] == 100                        # clamped


def test_sound_switches_output_by_part_of_its_name(pipewire):
    got = agent.do_sound({"action": "output", "device": "built-in"})
    assert got["output"] == "Built-in Audio Analog Stereo" and ["wpctl", "set-default", "49"] in pipewire.calls
    with pytest.raises(ValueError, match="No output like that"):
        agent.do_sound({"action": "output", "device": "bluetooth"})
    with pytest.raises(ValueError, match="Which one"):
        agent.do_sound({"action": "output", "device": "o"})                     # too vague: both match


def test_sound_only_runs_its_own_programs(pipewire):
    with pytest.raises(ValueError):
        agent.do_sound({"action": "rm -rf /"})
    assert all(c[0] == "wpctl" for c in pipewire.calls)


def test_no_sound_tools_says_so(monkeypatch):
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent.shutil, "which", lambda n: None)
    with pytest.raises(ValueError, match="can't reach this computer's sound"):
        agent.do_sound({"action": "get"})
    with pytest.raises(ValueError, match="playerctl isn't installed"):
        agent.do_player({"action": "next"})


def test_players_playing_first():
    out = ("vlc\tPaused\t\tHoliday video\t\t0:12\t3:40\n"
           "spotify\tPlaying\tDaft Punk\tOne More Time\tDiscovery\t1:02\t5:20\n"
           "chromium.instance42\tStopped\t\t\t\t\t\n")
    ps = agent.parse_players(out)
    assert [p["player"] for p in ps] == ["spotify", "vlc", "chromium.instance42"]
    assert ps[0]["artist"] == "Daft Punk" and ps[0]["length"] == "5:20"


def test_player_controls_the_active_one(monkeypatch):
    calls = []
    lines = {"now": "spotify\tPlaying\tDaft Punk\tOne More Time\tDiscovery\t1:02\t5:20\nvlc\tPaused\t\tClip\t\t0:01\t1:00\n"}

    def run(argv, *a, **k):
        calls.append(argv)
        if "metadata" in argv:
            return 0, lines["now"], ""
        if argv[-1] == "next":
            lines["now"] = lines["now"].replace("One More Time", "Aerodynamic")
        return 0, "", ""
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "_run_env", run)
    monkeypatch.setattr(agent.shutil, "which", lambda n: "/usr/bin/playerctl")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    got = agent.do_player({"action": "next"})
    assert ["playerctl", "-p", "spotify", "next"] in calls and got["players"][0]["title"] == "Aerodynamic"
    agent.do_player({"action": "toggle"})
    assert ["playerctl", "-p", "spotify", "play-pause"] in calls
    assert agent.do_player({"action": "status"})["players"][1]["player"] == "vlc"
    with pytest.raises(ValueError):
        agent.do_player({"action": "open"})


def test_windows_uses_the_media_keys(monkeypatch):
    keys = []
    monkeypatch.setattr(agent, "IS_WINDOWS", True)
    monkeypatch.setattr(agent, "_key", lambda vk, times=1: keys.append((vk, times)))
    assert agent.do_sound({"action": "up"}) == {"windows": True} and keys[-1] == (0xAF, 5)
    agent.do_sound({"action": "set", "level": 40})
    assert keys[-2:] == [(0xAE, 50), (0xAF, 20)]
    agent.do_player({"action": "next"})
    assert keys[-1] == (0xB0, 1)
    with pytest.raises(ValueError):
        agent.do_player({"action": "status"})


# ---- the live view -------------------------------------------------------------------------------

class FakeTab:
    """A page's control connection: a screencast that has one new frame each time it's asked."""

    def __init__(self, target, timeout=8):
        self.target, self.calls, self.closed, self.queued = target, [], False, []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "Page.startScreencast":
            self.queued.append({"data": "/9j/frame", "sessionId": 1, "metadata": {"deviceWidth": 1280, "deviceHeight": 720}})
        return {"data": "/9j/still"}

    def notify(self, method, **params):
        self.calls.append((method, params))

    def event(self, method, timeout):
        return self.queued.pop(0) if self.queued else None

    def close(self):
        self.closed = True


class FakeShell:
    def __init__(self, pages, current=None, running=True):
        self._pages, self.current, self._running, self.server = pages, current, running, "https://aurelia"

    def running(self):
        return self._running

    def pages(self):
        return self._pages

    def is_hers(self, t):
        return t["url"].startswith(self.server)

    def page(self):
        mine = [t for t in self._pages if not self.is_hers(t)]
        if not mine:
            raise ValueError("nothing is open on this screen (it's showing me)")
        return mine[0]


def test_live_uses_the_screencast_which_never_resizes_the_page():
    tab = FakeTab({"id": "a"})
    agent.live_start(tab)
    data, w, h = agent.live_frame(tab, 0.1)
    names = [c[0] for c in tab.calls]
    assert data == "/9j/frame" and (w, h) == (1280, 720)
    assert "Page.captureScreenshot" not in names and "Page.screencastFrameAck" in names    # acked, or it stops sending
    assert dict(tab.calls)["Page.startScreencast"]["maxWidth"] == agent.LIVE_WIDTH
    assert agent.live_frame(tab, 0.01) is None                                            # nothing new: keep the last


def test_a_look_screenshot_comes_from_the_screencast_too():
    class T(FakeTab):
        pass
    t = T({"id": "a"})
    assert agent.Tab.shot(t) == "/9j/frame"
    assert [c[0] for c in t.calls] == ["Page.startScreencast", "Page.screencastFrameAck", "Page.stopScreencast"]


def test_live_follows_the_screen_and_stops_when_nobody_watches(monkeypatch):
    sent, keeps = [], iter([True, True, False])
    shell = FakeShell([{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"},
                       {"id": "s", "url": "https://accounts.spotify.com/login?continue=secret-token", "title": "Log in - Spotify"}])
    monkeypatch.setattr(agent, "SHELL", shell)
    monkeypatch.setattr(agent, "Tab", FakeTab)
    monkeypatch.setattr(agent, "call", lambda server, path, body, key, timeout=10: (sent.append((path, body)), {"keep": next(keeps)})[1])
    agent.live_stream("https://aurelia", "k", agent.threading.Event())
    assert len(sent) == 3 and all(p == "/nodes/live/frame" for p, _ in sent)
    assert "jpeg" not in sent[1][1]                                           # the page didn't change: no new frame
    body = sent[0][1]
    assert body["site"] == "accounts.spotify.com" and body["title"] == "Log in - Spotify" and body["jpeg"] == "/9j/frame"
    assert "secret-token" not in str(sent)                                   # the address's query never leaves


def test_live_shows_her_own_view_when_nothing_else_is_open_and_says_why_it_cant(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "Tab", FakeTab)
    monkeypatch.setattr(agent, "call", lambda server, path, body, key, timeout=10: (sent.append(body), {"keep": False})[1])
    monkeypatch.setattr(agent, "SHELL", FakeShell([{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"}]))
    agent.live_stream("https://aurelia", "k", agent.threading.Event())
    assert sent[-1]["jpeg"] and sent[-1]["site"] == ""                      # her own view: no site to name
    monkeypatch.setattr(agent, "SHELL", FakeShell([], running=False))
    agent.live_stream("https://aurelia", "k", agent.threading.Event())
    assert sent[-1] == {"problem": "my shell isn't running on this computer"}


def test_live_ends_when_the_server_cant_be_reached(monkeypatch):
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise OSError("down")
    monkeypatch.setattr(agent, "Tab", FakeTab)
    monkeypatch.setattr(agent, "SHELL", FakeShell([{"id": "s", "url": "https://x.example/", "title": "x"}]))
    monkeypatch.setattr(agent, "call", down)
    agent.live_stream("https://aurelia", "k", agent.threading.Event())
    assert calls == [1]                                                     # back to waiting, not a busy loop


# ---- her player ----------------------------------------------------------------------------------

def test_she_plays_only_her_own_streams(monkeypatch):
    monkeypatch.setattr(agent, "SERVER_URL", "https://aurelia.example:8443")
    for bad in ([{"path": "/../etc/passwd"}], [{"path": "https://evil.example/x.mp4"}], [{"url": "file:///etc/passwd"}],
                [{"path": "/nodes/media/" + "a" * 32 + "/../x"}], []):
        with pytest.raises(ValueError):
            agent.do_play({"items": bad})


def test_a_long_home_still_gets_a_player_socket(monkeypatch, tmp_path):
    deep = tmp_path / ("x" * 90)
    deep.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(deep))
    path = agent.mpv_socket()
    assert len(path) <= 100 and path.endswith(".sock")


def test_without_mpv_she_says_how_to_get_it(monkeypatch):
    monkeypatch.setattr(agent, "find_mpv", lambda: None)
    with pytest.raises(ValueError, match="--system"):
        agent.Mpv(path="/tmp/nowhere.sock").start()


# ---- installing and removing apps -------------------------------------------------------------------

APT_SEARCH = "vlc - multimedia player and streamer\nvlc-bin - binaries from VLC\nbad name - nope\n"
SNAP_FIND = ("Name        Version  Publisher     Notes  Summary\n"
             "vlc         3.0.20   videolan✓     -      The ultimate media player\n"
             "vlc-remote  1.0      someone       -      Control VLC\n")


def test_package_listings_are_read_safely():
    assert agent.parse_apt_search(APT_SEARCH) == [
        {"source": "apt", "name": "vlc", "summary": "multimedia player and streamer"},
        {"source": "apt", "name": "vlc-bin", "summary": "binaries from VLC"}]
    snaps = agent.parse_snap_find(SNAP_FIND)
    assert snaps[0] == {"source": "snap", "name": "vlc", "summary": "The ultimate media player", "publisher": "videolan"}
    assert agent.parse_apt_removal("Remv vlc [3.0]\nRemv vlc-bin [3.0]\nInst x\n") == ["vlc", "vlc-bin"]


class Pkgs:
    """A fake package system: apt-cache, dpkg-query, apt-get -s, pkcon, snap."""

    def __init__(self, installed=(), deps=None, allowed=True):
        self.installed, self.deps, self.allowed, self.calls = set(installed), deps or {}, allowed, []

    def __call__(self, argv, timeout=30):
        self.calls.append(argv)
        if argv[0] == "apt-cache":
            return 0, APT_SEARCH, ""
        if argv[0] == "snap" and argv[1] == "find":
            return 0, SNAP_FIND, ""
        if argv[0] == "snap" and argv[1] == "list":
            return 0, "Name Version\n", ""
        if argv[0] == "dpkg-query" and "${Essential} ${Priority}" in argv:
            return 0, "yes required" if argv[-1] == "bash-ish" else "no optional", ""
        if argv[0] == "dpkg-query":
            return 0, "".join(f"{n} {'installed' if n in self.installed else 'not-installed'}\n" for n in argv[4:]), ""
        if argv[0] == "apt-get":
            return 0, "".join(f"Remv {x} [1]\n" for x in [argv[-1], *self.deps.get(argv[-1], [])]), ""
        if argv[0] in ("pkcon", "pkgcli") and "install" in argv:
            if not self.allowed:
                return 1, "", "Fatal error: Not authorized"
            self.installed.add(argv[-1])
            return 0, "Finished", ""
        if argv[0] in ("pkcon", "pkgcli") and "remove" in argv:
            self.installed.discard(argv[-1])
            return 0, "Finished", ""
        return 127, "", ""


@pytest.fixture
def pkgs(monkeypatch):
    fake = Pkgs(installed={"vlc-bin"})
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "_run_c", fake)
    monkeypatch.setattr(agent.shutil, "which", lambda n: f"/usr/bin/{n}")
    return fake


def test_finding_an_app_changes_nothing(pkgs):
    got = agent.do_packages_find({"name": "VLC"})["candidates"]
    assert got[0]["name"] == "vlc" and {c["source"] for c in got} == {"apt", "snap"}
    assert next(c for c in got if c["name"] == "vlc-bin")["installed"] is True
    assert not [c for c in pkgs.calls if c[0] in ("pkcon",) or c[:2] == ["snap", "install"]]
    with pytest.raises(ValueError):
        agent.do_packages_find({"name": "vlc; rm -rf /"})


def test_installing_needs_his_yes_and_a_real_package_name(pkgs):
    with pytest.raises(ValueError, match="confirmation"):
        agent.do_package_install({"name": "vlc"})
    for bad in ("../x.deb", "/tmp/evil.deb", "https://x/y.deb", "VLC Player"):
        with pytest.raises(ValueError):
            agent.do_package_install({"name": bad, "confirmed": True})
    assert agent.do_package_install({"name": "vlc", "confirmed": True}) == {"name": "vlc", "source": "apt", "installed": True}
    assert ["pkgcli", "-y", "install", "--no-autoremove", "vlc"] in pkgs.calls          # PackageKit 1.3 (Ubuntu 26.04)


def test_older_releases_use_pkcon(pkgs, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda n: None if n == "pkgcli" else f"/usr/bin/{n}")
    agent.do_package_install({"name": "vlc", "confirmed": True})
    assert ["pkcon", "-y", "-p", "install", "vlc"] in pkgs.calls and not [c for c in pkgs.calls if c[0] == "pkgcli"]


def test_without_permission_it_says_how_to_fix_it(pkgs):
    pkgs.allowed = False
    with pytest.raises(ValueError, match="--system"):
        agent.do_package_install({"name": "vlc", "confirmed": True})


def test_removing_never_takes_the_system_or_other_packages(pkgs):
    pkgs.installed |= {"vlc", "systemd", "libvlc5", "steam"}
    pkgs.deps = {"libvlc5": ["vlc", "vlc-bin"]}
    assert agent.do_package_check_remove({"name": "vlc"}) == {"name": "vlc", "source": "apt", "removes": ["vlc"]}
    with pytest.raises(ValueError, match="part of the system"):
        agent.do_package_check_remove({"name": "systemd"})
    with pytest.raises(ValueError, match="would also remove vlc, vlc-bin"):
        agent.do_package_check_remove({"name": "libvlc5"})
    with pytest.raises(ValueError, match="confirmation"):
        agent.do_package_remove({"name": "steam"})
    pkgs.deps["steam"] = ["steam-libs"]                                       # changed after he was asked
    with pytest.raises(ValueError):
        agent.do_package_remove({"name": "steam", "confirmed": True, "removes": ["steam"]})
    assert agent.do_package_remove({"name": "vlc", "confirmed": True, "removes": ["vlc"]})["removed"] is True
    assert "vlc" not in pkgs.installed


def test_updates_wait_while_something_plays(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "update", lambda server, key, want: seen.append(want["version"]))
    monkeypatch.setattr(agent, "player_busy", lambda: True)
    agent.maybe_update("s", "k", {"version": "9.9.9"})
    assert seen == []
    monkeypatch.setattr(agent, "player_busy", lambda: False)
    agent.maybe_update("s", "k", {"version": agent.VERSION})
    agent.maybe_update("s", "k", {"version": "9.9.9"})
    assert seen == ["9.9.9"]


# ---- the clipboard -------------------------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("hello world", False), ("https://www.youtube.com/watch?v=abc123XYZ", False), ("kentrell@solaces.me", False),
    ("Frieren S1E3", False), ("sk-abcdefghijklmnop1234", True), ("Tr0ub4dor&3xyz", True),
    ("ghp_abcdefghijklmnopqrstuvwxyz12", True), ("aur_" + "x" * 30, True),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", True)])
def test_what_counts_as_a_secret(text, secret):
    assert agent.looks_secret(text) is secret


@pytest.fixture
def wayland_clip(monkeypatch):
    state = {"text": "copied on the TV", "types": "text/plain;charset=utf-8\nTEXT\n", "calls": []}
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "wayland", lambda: True)
    monkeypatch.setattr(agent.shutil, "which", lambda n: f"/usr/bin/{n}")

    def run_env(argv, timeout=6):
        state["calls"].append(argv)
        if "--list-types" in argv:
            return 0, state["types"], ""
        return 0, state["text"], ""

    def run_in(argv, data, timeout=10):
        state["calls"].append(argv)
        state["text"] = data
        return 0, "", ""
    monkeypatch.setattr(agent, "_run_env", run_env)
    monkeypatch.setattr(agent, "_run_in", run_in)
    return state


def test_clipboard_round_trip(wayland_clip):
    assert agent.do_clipboard_get({}) == {"text": "copied on the TV", "cut": False}
    assert agent.do_clipboard_set({"text": "from my phone"}) == {"set": True, "chars": 13, "verified": True}
    assert wayland_clip["text"] == "from my phone" and ["wl-copy"] in wayland_clip["calls"]


def test_clipboard_never_carries_secrets(wayland_clip):
    wayland_clip["types"] += "x-kde-passwordManagerHint\n"                     # KeePassXC marks what it copies
    with pytest.raises(ValueError, match="password manager"):
        agent.do_clipboard_get({})
    wayland_clip["types"], wayland_clip["text"] = "text/plain\n", "sk-abcdefghijklmnop1234"
    with pytest.raises(ValueError, match="password or a key"):
        agent.do_clipboard_get({})
    with pytest.raises(ValueError, match="password or a key"):
        agent.do_clipboard_set({"text": "ghp_abcdefghijklmnopqrstuvwxyz12"})
    assert ["wl-copy"] not in wayland_clip["calls"]


def test_no_clipboard_tools_says_how_to_get_them(monkeypatch):
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "wayland", lambda: True)
    monkeypatch.setattr(agent.shutil, "which", lambda n: None)
    monkeypatch.setattr(agent, "session_env", lambda: {})
    with pytest.raises(ValueError, match="--system"):
        agent.do_clipboard_get({})


# ---- modes: gaming and exam ---------------------------------------------------------------------------

@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    return tmp_path


def test_a_running_game_is_noticed():
    assert agent.game_running(["c:/program files (x86)/steam/steamapps/common/elden ring/game/eldenring.exe"]) == "elden ring"
    assert agent.game_running(["/home/k/.local/share/steam/steamapps/common/hades/hades"]) == "hades"
    assert agent.game_running(["c:/program files/epic games/fortnite/fortniteclient.exe"]) == "fortnite"
    assert agent.game_running(["c:/windows/explorer.exe", "c:/program files/steam/steam.exe"]) is None   # the launcher isn't a game


def test_gaming_mode_keeps_out_of_the_way(cfgdir, monkeypatch):
    agent.write_mode("gaming")
    refused = agent.execute({"kind": "open_url", "args": {"url": "https://x.example"}})
    assert refused["ok"] is False and "gaming mode" in refused["error"]
    monkeypatch.setattr(agent, "_run", lambda argv, timeout=5: "should not be called")
    assert agent.gpus() == []                                                     # never the GPU while he plays
    assert agent.execute({"kind": "mode", "args": {"mode": "normal"}})["data"]["mode"] == "normal"
    assert agent.current_mode() == "normal"


def test_auto_gaming_turns_off_only_what_it_turned_on(cfgdir, monkeypatch):
    games = iter(["hades", None, None])
    monkeypatch.setattr(agent, "game_running", lambda paths=None: next(games))
    stop = agent.threading.Event()
    calls = []
    real_wait = stop.wait
    monkeypatch.setattr(stop, "wait", lambda s: (calls.append(1), len(calls) > 2)[1])
    agent.write_mode("normal")
    agent.auto_gaming_loop(stop, 0)
    assert agent.current_mode() == "normal" and len(calls) == 3                 # on with the game, off after it
    agent.write_mode("gaming")                                                  # he set it himself: it stays
    games = iter([None])
    monkeypatch.setattr(agent, "game_running", lambda paths=None: next(games))
    calls.clear()
    monkeypatch.setattr(stop, "wait", lambda s: (calls.append(1), len(calls) > 1)[1])
    agent.auto_gaming_loop(stop, 0)
    assert agent.current_mode() == "gaming"


def test_exam_mode_stops_everything_until_the_time_is_up(cfgdir, monkeypatch):
    timers = []
    monkeypatch.setattr(agent, "schedule_return", lambda secs: (timers.append(secs), "systemd")[1])
    agent.STOP_AFTER_REPLY.clear()
    got = agent.do_mode({"mode": "exam", "hours": 2})
    assert got["mode"] == "exam" and timers == [7200] and agent.STOP_AFTER_REPLY.is_set()
    agent.STOP_AFTER_REPLY.clear()
    monkeypatch.setattr(agent, "load", lambda: (_ for _ in ()).throw(AssertionError("must not even connect")))
    assert agent.cmd_run() == agent.EXIT_PAUSED                                 # started during the exam: stays stopped
    assert agent.do_mode({"mode": "exam", "hours": 50})["until"] <= agent.time.time() + 12 * 3600 + 1   # at most 12 h
    (cfgdir / "mode.json").write_text('{"mode": "exam", "until": 1}')           # the time is up
    assert agent.read_mode() == {"mode": "normal", "ended": "exam"}


def test_exam_mode_needs_a_way_back(cfgdir, monkeypatch):
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent.shutil, "which", lambda n: None)
    with pytest.raises(ValueError, match="stayed on"):
        agent.do_mode({"mode": "exam"})
    assert agent.current_mode() == "normal"


def test_on_windows_the_guard_restarts_the_agent_but_not_in_exam_mode(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("guard", Path(__file__).resolve().parents[1] / "app/node_agent/aurelia_node_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    codes = iter([75, 1, 4])                                                     # updated, crashed, exam mode
    runs = []
    import subprocess as sp, time as tm
    monkeypatch.setattr(guard.platform, "system", lambda: "Windows")
    monkeypatch.setattr(guard, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sp, "call", lambda argv: (runs.append(argv), next(codes))[1])
    monkeypatch.setattr(tm, "sleep", lambda s: None)
    monkeypatch.setattr(guard.sys, "argv", ["guard", "run"])
    with pytest.raises(SystemExit) as stop:
        guard.main()
    assert stop.value.code == 4 and len(runs) == 3


def test_a_copy_that_doesnt_stay_on_the_clipboard_is_an_error_not_a_success(wayland_clip, monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    monkeypatch.setattr(agent, "_run_in", lambda argv, data, timeout=10: (0, "", ""))     # wl-copy "worked"
    with pytest.raises(ValueError, match="didn't stay there"):
        agent.do_clipboard_set({"text": "from my phone"})                       # but the old text is still there


# ---- checking it happened (no ghost confirmations) -------------------------------------------------

class HomeShell(FakeShell):
    def __init__(self, pages, closes=True):
        super().__init__(pages)
        self.closes = closes

    def home(self):
        n = len([t for t in self._pages if not self.is_hers(t)])
        if self.closes:
            self._pages = [t for t in self._pages if self.is_hers(t)]
        return n


def test_open_url_in_her_shell_is_checked_among_its_pages(monkeypatch):
    shell = HomeShell([{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"}])
    opened = []
    shell.open = lambda url: (opened.append(url), shell._pages.append({"id": "n", "url": "https://www.youtube.com/watch?v=1", "title": "A video"}))
    monkeypatch.setattr(agent, "SHELL", shell)
    out = agent.do_open_url({"url": "https://youtube.com/watch?v=1"})
    assert out == {"url": "https://youtube.com/watch?v=1", "in": "shell", "verified": True, "title": "A video"}


_REAL = agent._shell_has


def test_open_url_that_never_appears_is_an_error(monkeypatch):
    shell = HomeShell([{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"}])
    shell.open = lambda url: None                                             # the browser ignored it
    monkeypatch.setattr(agent, "SHELL", shell)
    monkeypatch.setattr(agent, "_shell_has", lambda url: _REAL(url, wait=0))
    with pytest.raises(ValueError, match="didn't open on my screen"):
        agent.do_open_url({"url": "https://example.com/"})


def test_the_default_browser_is_not_claimed_as_seen(spawned, monkeypatch):
    monkeypatch.setattr(agent, "SHELL", None)
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    assert agent.do_open_url({"url": "https://example.com/"})["verified"] is False


def test_an_app_counts_as_started_only_when_a_new_process_shows_up(apps_dir, spawned, monkeypatch):
    procs = {1: "systemd", 50: "firefox"}
    monkeypatch.setattr(agent, "proc_names", lambda: dict(procs))
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    out = agent._started({"name": "Firefox", "id": "firefox", "exe": "firefox"}, dict(procs), wait=0)
    assert out is None                                                        # it was already running: not proof
    procs[77] = "vlc"
    assert agent._started({"name": "VLC media player", "id": "org.videolan.vlc", "exe": "vlc"}, {1: "systemd"}, wait=0) == \
        {"pid": 77, "name": "vlc"}
    procs[78] = "gnome-text-edit"                                             # Linux keeps 15 letters of a name
    assert agent._started({"name": "Text Editor", "id": "org.gnome.TextEditor", "exe": "gnome-text-editor"},
                          {1: "systemd", 77: "vlc"}, wait=0) == {"pid": 78, "name": "gnome-text-edit"}
    monkeypatch.setattr(agent, "_started", lambda app, before, wait=0: None)
    out = agent.do_launch_app({"name": "vlc"})
    assert out == {"app": "VLC media player", "verified": False}


def test_installed_apps_know_their_program(apps_dir):
    vlc = next(a for a in agent.installed_apps() if a["id"] == "org.videolan.vlc")
    assert vlc["exe"] == "vlc"


def test_going_home_is_checked(monkeypatch):
    monkeypatch.setattr(agent, "player_busy", lambda: False)
    monkeypatch.setattr(agent, "close_launched", lambda: ([], []))
    pages = [{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"},
             {"id": "y", "url": "https://youtube.com/", "title": "YouTube"}]
    monkeypatch.setattr(agent, "SHELL", HomeShell(list(pages)))
    assert agent.do_home({}) == {"closed": 1, "verified": True}
    monkeypatch.setattr(agent, "SHELL", HomeShell(list(pages), closes=False))
    t = iter([0, 0, 10, 10])
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(t, 10))
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    with pytest.raises(ValueError, match=r"still on top of me there \(YouTube\)"):
        agent.do_home({})


def test_going_home_says_so_when_the_player_wont_stop(monkeypatch):
    monkeypatch.setattr(agent, "close_launched", lambda: ([], []))
    class P:
        def control(self, action, args):
            return {"last": {"title": "Dune"}}
    monkeypatch.setattr(agent, "player_busy", lambda: True)
    monkeypatch.setattr(agent, "player", lambda: P())
    monkeypatch.setattr(agent, "SHELL", HomeShell([{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"}]))
    with pytest.raises(ValueError, match="my player"):
        agent.do_home({})


def test_her_window_is_a_separate_app_window_named_after_this_computer(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    argv = agent.app_argv("/usr/bin/chromium", "https://aura.example:8443/", "Kentrell's laptop")
    assert argv[0] == "/usr/bin/chromium"
    assert argv[1] == "--app=https://aura.example:8443/view?app=Kentrell%27s%20laptop"
    assert f"--user-data-dir={tmp_path / 'app-profile'}" in argv                 # its own profile, not his browser's


def test_her_window_signs_in_once_with_the_link_approved_with_this_computer(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    agent.save_window_token("tok-123")
    t = agent.take_window_token()
    assert t == "tok-123" and agent.take_window_token() is None                     # used once, then gone
    argv = agent.app_argv("/usr/bin/chromium", "https://aura.example:8443", "Desk", t)
    assert argv[1] == "--app=https://aura.example:8443/pair/window?t=tok-123&app=Desk"


def test_pairing_with_the_window_asks_for_it_and_keeps_its_link(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "machine_info", lambda: {"hostname": "desk", "os": "Windows"})
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    sent = []

    def call(server, path, body=None, key=None, timeout=10):
        sent.append((path, body))
        if path == "/pair/request":
            return {"code": "ABC-DEF", "device_id": 7, "claim": "c", "expires_in": 60}
        return {"status": "approved", "device_id": 7, "name": "Desk", "key": "aur_k", "window_token": "w1"}
    monkeypatch.setattr(agent, "call", call)
    assert agent.cmd_pair("https://a", "Desk", window=True) == 0
    assert sent[0][1]["info"]["window"] is True
    assert (tmp_path / "window").read_text().strip() == "w1" and (tmp_path / "key").read_text().strip() == "aur_k"


def test_going_home_closes_the_apps_she_opened_there_and_only_those(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "player_busy", lambda: False)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    running = {501: "100", 502: "200"}                                        # pid: start time
    monkeypatch.setattr(agent, "_linux_procs", lambda: {p: {"start": k, "name": "x"} for p, k in running.items()})
    killed = []
    monkeypatch.setattr(agent.os, "kill", lambda pid, sig: (killed.append(pid), running.pop(pid, None)))
    agent.remember_launched(501, "gnome-text-edit")
    agent.write_launched(agent.read_launched() + [{"pid": 503, "key": "9", "name": "gone"}])     # already closed
    monkeypatch.setattr(agent, "SHELL", HomeShell([{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"}]))
    out = agent.do_home({})
    assert killed == [501] and out["apps"] == ["gnome-text-edit"] and out["verified"] is True   # never 502: not hers
    assert agent.read_launched() == []


def test_going_home_says_which_app_wont_close(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "IS_WINDOWS", False)
    monkeypatch.setattr(agent, "player_busy", lambda: False)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    t = iter(range(0, 100, 5))
    monkeypatch.setattr(agent.time, "monotonic", lambda: next(t))
    monkeypatch.setattr(agent, "_linux_procs", lambda: {601: {"start": "1", "name": "x"}})
    monkeypatch.setattr(agent.os, "kill", lambda pid, sig: None)                          # it ignores the request
    agent.remember_launched(601, "stubborn")
    monkeypatch.setattr(agent, "SHELL", HomeShell([{"id": "v", "url": "https://aurelia/view", "title": "Aurelia"}]))
    with pytest.raises(ValueError, match=r"still on top of me there \(stubborn\)"):
        agent.do_home({})
