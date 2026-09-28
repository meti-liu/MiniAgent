"""测试 agent 循环：用按剧本回复的 FakeLLM 代替真实模型，验证工具被执行、结果被喂回、会按时停止。不联网。"""

import json

import pytest

from mini_agent.agent import FINAL_PROMPT, run
from mini_agent.llm import Reply, ToolCall
from mini_agent.tools import Tools


class FakeLLM:
    """和 LLMClient 有同名的 chat 方法（鸭子类型），按预设顺序返回回复，并记下每次收到了什么。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls.append({"messages": list(messages), "tools": tools, "tool_choice": tool_choice})
        return self.replies.pop(0)


def tool_reply(*calls):
    """calls 是 (id, 工具名, 参数) 元组；构造一个“模型要调用这些工具”的回复。"""
    raw = [{"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
           for i, n, a in calls]
    return Reply({"role": "assistant", "content": None, "tool_calls": raw},
                 None, [ToolCall(i, n, a) for i, n, a in calls], "tool_calls")


def answer_reply(text):
    return Reply({"role": "assistant", "content": text}, text, [], "stop")


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    return Tools(tmp_path)


def test_search_then_read_then_answer(tools):
    llm = FakeLLM([
        tool_reply(("c1", "search", {"pattern": "hello"})),
        tool_reply(("c2", "read_file", {"path": "app.py"})),
        answer_reply("hello 返回 'hi'（app.py:1-2）"),
    ])
    printed = []
    result = run("hello 做什么？", llm, tools, "demo", on_step=printed.append)

    assert result.answer == "hello 返回 'hi'（app.py:1-2）"
    assert result.stopped_by == "answer"
    assert [s.tool for s in result.steps] == ["search", "read_file", None]
    assert printed == result.steps
    # 第 3 次调用时，模型能看到前两次的工具结果，且 tool_call_id 对得上
    tool_messages = [m for m in llm.calls[2]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]
    assert "app.py:1: def hello():" in tool_messages[0]["content"]
    assert "2:     return 'hi'" in tool_messages[1]["content"]


def test_stops_after_max_steps_with_tool_choice_none(tools):
    llm = FakeLLM([tool_reply((f"c{i}", "list_dir", {})) for i in range(3)] + [answer_reply("尽力回答")])
    result = run("q", llm, tools, "demo", max_steps=3, on_step=lambda s: None)

    assert result.stopped_by == "max_steps"
    assert result.answer == "尽力回答"
    assert len(llm.calls) == 4
    last = llm.calls[-1]
    assert last["tool_choice"] == "none"
    assert last["tools"] == llm.calls[0]["tools"]  # 工具集没变，前缀不变
    assert last["messages"][-1] == {"role": "user", "content": FINAL_PROMPT}


def test_errors_are_fed_back_and_multiple_calls_share_a_step(tools):
    llm = FakeLLM([
        tool_reply(("c1", "delete_file", {}), ("c2", "list_dir", {})),
        answer_reply("done"),
    ])
    result = run("q", llm, tools, "demo", on_step=lambda s: None)

    assert [(s.number, s.tool) for s in result.steps] == [(1, "delete_file"), (1, "list_dir"), (2, None)]
    tool_messages = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_messages[0]["content"].startswith("ERROR: unknown tool")
    assert tool_messages[1]["content"] == "app.py"
