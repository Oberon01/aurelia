"""The real config/aurelia.yaml must load: a bad fast path makes the whole file invalid
(Chrysalis would refuse the release, and a running orchestrator would keep the old one)."""
import re
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve()
CONFIG = HERE.parents[3] / "config" / "aurelia.yaml" if len(HERE.parents) > 3 else HERE.parent / "_none"


@pytest.mark.skipif(not CONFIG.exists(), reason="the repo's config isn't in the test image")
def test_the_config_file_loads_and_every_fast_path_compiles():
    from app.config import RuntimeConfig
    cfg = RuntimeConfig.model_validate(yaml.safe_load(CONFIG.read_text()))
    names = [p.name for p in cfg.fastpath.patterns]
    assert len(names) == len(set(names)), "two fast paths share a name"
    for p in cfg.fastpath.patterns:
        re.compile(p.match)
