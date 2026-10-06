"""A real calendar server for the tests (Radicale, the same version and the settings that matter of the one I run:
bcrypt passwords, each user only their own calendars), on a free port with its own folder and password file."""
import socket
import subprocess
import sys
import time

import bcrypt
import httpx
import pytest


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def radicale(tmp_path):
    """{url, users (the password file), user, password}; the server's own config, pointed at a temp folder."""
    try:
        import radicale as _  # noqa: F401
    except ImportError:
        pytest.skip("radicale isn't installed")
    port = _port()
    users = tmp_path / "auth" / "users"
    users.parent.mkdir()
    users.write_text(f"kentrell:{bcrypt.hashpw(b'calendar-pass-1', bcrypt.gensalt()).decode()}\n")
    text = (f"[server]\nhosts = 127.0.0.1:{port}\n[auth]\ntype = htpasswd\nhtpasswd_filename = {users}\n"
            f"htpasswd_encryption = bcrypt\ndelay = 0\n[rights]\ntype = owner_only\n[storage]\n"
            f"filesystem_folder = {tmp_path / 'collections'}\n[web]\ntype = none\n[logging]\nlevel = warning\n")
    cfg = tmp_path / "config"
    cfg.write_text(text)
    proc = subprocess.Popen([sys.executable, "-m", "radicale", "--config", str(cfg)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("radicale didn't start: " + (proc.stderr.read() or b"").decode()[:500])
    yield {"url": url, "users": users, "user": "kentrell", "password": "calendar-pass-1"}
    proc.terminate()
    proc.wait(5)
