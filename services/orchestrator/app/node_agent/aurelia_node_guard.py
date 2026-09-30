#!/usr/bin/env python3
"""The guard: what the service starts, before the agent. It never updates itself (only
the installer replaces it), so a broken agent update can always be undone.

After an update, the agent records it (update.json: from, to, boots). Each start here
counts a boot; the agent clears the record once it reconnects to Aurelia. If a new
version has started three times without reconnecting, the guard puts the previous
agent back and notes it, so the old one can tell Aurelia what happened.
"""
import json
import os
import platform
import shutil
import sys
from pathlib import Path

MAX_BOOTS = 3
HERE = Path(__file__).resolve().parent
AGENT = HERE / "aurelia_node.py"


def config_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "aurelia-node"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "aurelia-node"


def main() -> None:
    state = config_dir() / "update.json"
    try:
        st = json.loads(state.read_text())
    except (OSError, ValueError):
        st = {}
    if st.get("to"):
        st["boots"] = int(st.get("boots", 0)) + 1
        prev = AGENT.with_suffix(".py.prev")
        if st["boots"] > MAX_BOOTS and prev.exists():
            shutil.copy2(prev, AGENT)
            print(f"guard: version {st['to']} never reconnected; put {st.get('from')} back", file=sys.stderr, flush=True)
            st = {**{k: v for k, v in st.items() if k not in ("to", "from", "boots")}, "rolled_back": st["to"]}
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(st))
    os.execv(sys.executable, [sys.executable, str(AGENT), *(sys.argv[1:] or ["run"])])


if __name__ == "__main__":
    main()
