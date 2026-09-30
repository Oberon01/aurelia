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
