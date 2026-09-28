from app.plan import classify

KNOWN = {"proxy", "postgres", "redis", "migrate", "orchestrator", "probe", "chrysalis"}


def test_config_only_needs_no_deploy():
    p = classify(["config/aurelia.yaml", "config/identity.md"], KNOWN)
    assert not p.needs_deploy and p.config == ["config/aurelia.yaml", "config/identity.md"]


def test_orchestrator_change_migrates_and_goes_first():
    p = classify(["services/functions/probe/app/main.py", "services/orchestrator/app/tasks.py"], KNOWN)
    assert p.services == ["orchestrator", "probe"] and p.migrate


def test_function_change_rebuilds_only_that_function():
    p = classify(["services/functions/probe/app/main.py"], KNOWN)
    assert p.services == ["probe"] and not p.migrate and not p.compose


def test_protected_core_is_never_applied():
    p = classify(["services/core/chrysalis/app/pipeline.py", "scripts/deploy.sh"], KNOWN)
    assert not p.needs_deploy and len(p.protected) == 2 and "manually" in p.summary()


def test_new_function_without_compose_entry_is_skipped_with_note():
    p = classify(["services/functions/orbit/app/main.py"], KNOWN)
    assert p.services == [] and "orbit" in p.notes[0]


def test_new_function_with_compose_change_is_applied():
    p = classify(["services/functions/orbit/app/main.py", "docker-compose.yml"], KNOWN)
    assert p.compose and p.services == ["orbit"]


def test_docs_and_scripts_are_ignored():
    p = classify(["README.md", "scripts/ask.sh"], KNOWN)
    assert not p.needs_deploy and p.summary() == "no action needed"
