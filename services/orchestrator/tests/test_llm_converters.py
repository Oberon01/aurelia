from types import SimpleNamespace

from app.llm.base import Message, ToolCall, ToolDef
from app.llm.claude import from_anthropic, to_anthropic, tools_to_anthropic
from app.llm.ollama import from_ollama, to_ollama


def conversation():
    return [
        Message("user", "echo hi"),
        Message("assistant", "On it.", tool_calls=[ToolCall("t1", "probe__echo", {"message": "hi"}),
                                                   ToolCall("t2", "probe__echo", {"message": "yo"})]),
        Message("tool", "<tool_output>ok</tool_output>", tool_call_id="t1", tool_name="probe__echo"),
        Message("tool", "<tool_output>bad</tool_output>", tool_call_id="t2", tool_name="probe__echo", is_error=True),
    ]


def test_claude_groups_tool_results_into_one_user_turn():
    out = to_anthropic(conversation())
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["type"] for b in out[1]["content"]] == ["text", "tool_use", "tool_use"]
    results = out[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert results[1]["is_error"] is True


def test_claude_caches_tool_definitions():
    defs = tools_to_anthropic([ToolDef("a__b", "x", {}), ToolDef("c__d", "y", {})])
    assert "cache_control" not in defs[0] and defs[1]["cache_control"] == {"type": "ephemeral"}


def test_claude_response_parsing():
    resp = SimpleNamespace(
        model="claude-test",
        content=[SimpleNamespace(type="text", text="Sure."),
                 SimpleNamespace(type="tool_use", id="toolu_1", name="probe__echo", input={"message": "hi"})],
        usage=SimpleNamespace(input_tokens=50, output_tokens=10, cache_read_input_tokens=40),
    )
    c = from_anthropic(resp)
    assert c.text == "Sure." and c.tool_calls[0].args == {"message": "hi"}
    assert c.usage.cache_read_tokens == 40 and c.provider == "claude"


def test_ollama_round_trip():
    out = to_ollama("sys", conversation())
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[2]["tool_calls"][0]["function"]["name"] == "probe__echo"
    assert out[3]["role"] == "tool"
    c = from_ollama({"message": {"content": "", "tool_calls": [
        {"function": {"name": "probe__echo", "arguments": {"message": "hi"}}}]},
        "prompt_eval_count": 30, "eval_count": 5}, "qwen")
    assert c.tool_calls[0].name == "probe__echo" and c.usage.output_tokens == 5
