from app import fastpath
from app.config import FastPathPattern

ECHO = FastPathPattern(
    name="echo",
    match=r"^(?:probe\s+)?echo\s+(?P<message>.+?)(?:\s+(?P<steps>\d+)\s+times)?$",
    tool="probe.echo",
    args={"message": "{message}", "steps": "{steps|3}"},
    reply='Echoing "{message}".',
)
SCHEMA = {"type": "object", "properties": {"message": {"type": "string"}, "steps": {"type": "integer"}},
          "required": ["message"]}


def test_matches_with_default():
    m = fastpath.match("Echo hello there", [ECHO])
    assert m and m.args == {"message": "hello there", "steps": "3"}
    assert fastpath.coerce(m.args, SCHEMA) == {"message": "hello there", "steps": 3}
    assert fastpath.render_reply(m.pattern, m.groups) == 'Echoing "hello there".'


def test_matches_optional_group_and_trailing_punctuation():
    m = fastpath.match("probe echo hi 7 times.", [ECHO])
    assert fastpath.coerce(m.args, SCHEMA) == {"message": "hi", "steps": 7}


def test_no_match_falls_through_to_llm():
    assert fastpath.match("what's the weather like?", [ECHO]) is None


def test_bad_regex_is_skipped():
    broken = FastPathPattern(name="bad", match="(", tool="x.y")
    assert fastpath.match("echo ok", [broken, ECHO]).pattern.name == "echo"


def test_polite_wrappers_are_ignored():
    for text in ["please echo hi", "Aura, echo hi", "can you echo hi please", "hey aurelia echo hi."]:
        m = fastpath.match(text, [ECHO])
        assert m and m.args["message"] == "hi", text
