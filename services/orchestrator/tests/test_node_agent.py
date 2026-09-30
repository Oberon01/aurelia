"""The node agent on its own: what it refuses, how it finds apps, and that it never uses a shell."""
import importlib.util
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
