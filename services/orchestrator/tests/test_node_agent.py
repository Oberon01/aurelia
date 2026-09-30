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


def test_apps_are_found_by_what_he_calls_them(apps_dir, spawned):
    assert agent.find_app("vlc")["id"] == "org.videolan.vlc"
    assert agent.find_app("VLC Media Player")["name"] == "VLC media player"
    assert agent.find_app("firefox")["name"] == "Firefox" and agent.find_app("steam") is None
    out = agent.execute({"kind": "launch_app", "args": {"name": "vlc"}})
    assert out == {"ok": True, "data": {"app": "VLC media player"}} and spawned == [["gtk-launch", "org.videolan.vlc"]]
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
        assert out == {"ok": False, "error": "her shell isn't running on this computer, so I can't see its screen"}


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
