"""测试 B1 记忆压缩：trim / clear / summary 各自怎么改消息，summary 的切分点和摘要请求，
agent 压缩之后的行为，以及轨迹里的压缩统计和回放。不联网。"""

from test_agent import FakeLLM, answer_reply, tool_reply
from test_trace import api_reply, scripted_client

from mini_agent import context, trace
from mini_agent.agent import Settings, run
from mini_agent.tools import Tools

SPECS = [{"type": "function", "function": {"name": "search"}}]


def history(steps):
    """steps 里每个元素是一步中各个工具结果的长度；返回 system、问题和这些步骤的消息。"""
    messages = context.initial("q", "demo")
    n = 0
    for sizes in steps:
        calls = []
        for _ in sizes:
            n += 1
            calls.append((f"c{n}", "search", {"pattern": str(n)}))
        messages.append(tool_reply(*calls).message)
        for (call_id, _, _), size in zip(calls, sizes):
            messages.append({"role": "tool", "tool_call_id": call_id, "content": "y" * size})
    return messages


def total(messages):
    return sum(context._size(m) for m in messages)


def test_trim_stops_just_under_the_limit_and_clear_goes_to_half():
    messages = history([[3000]] * 8)
    trimmed, by_trim = context.compact(messages, "trim", 20_000)
    cleared, by_clear = context.compact(messages, "clear", 20_000)
    assert total(trimmed) <= 20_000 < total(trimmed) + 3000  # 刚好降到上限以下
    assert total(cleared) <= 10_000  # 一次清到上限的一半
    assert by_trim["kind"] == "trim" and by_clear["kind"] == "clear"
    assert by_clear["replaced"] > by_trim["replaced"]
    assert context.compact(messages, "trim", 10**6) == (messages, None)  # 不超限时什么都不做


def test_summary_replaces_older_turns_with_one_message():
    messages = history([[100], [100, 100], [100], [100]])  # 下标 7 是第 3 步的 assistant
    llm = FakeLLM([answer_reply("目标：……")])
    compacted, event = context.compact(messages, "summary", 100, llm, SPECS)

    assert [m["role"] for m in compacted] == ["system", "user", "user", "assistant", "tool", "assistant", "tool"]
    assert compacted[2]["content"].startswith(context.SUMMARY_HEADER) and "目标：……" in compacted[2]["content"]
    assert compacted[3:] == messages[7:]  # 最近两步原样保留
    assert (event["kind"], event["replaced"], event["summary"]) == ("summary", 5, "目标：……")

    request = llm.calls[0]  # 摘要请求复用同一个前缀，只在末尾加提示词；工具照传但禁止调用
    assert request["messages"][:-1] == messages[:7]
    assert request["messages"][-1]["content"] == context.SUMMARY_PROMPT
    assert (request["tools"], request["tool_choice"]) == (SPECS, "none")


def test_summary_never_splits_a_tool_call_from_its_results():
    messages = history([[100], [100, 100, 100], [100]])  # 默认切分点（下标 6）落在 tool 上
    compacted, event = context.compact(messages, "summary", 100, FakeLLM([answer_reply("s")]), SPECS)
    assert event["replaced"] == 2  # 往前退到第 2 步的 assistant，只压缩第 1 步
    assert [m["role"] for m in compacted[3:7]] == ["assistant", "tool", "tool", "tool"]

    one_step = history([[100, 100, 100, 100]])  # 只有一步：没有可以压缩的完整轮次
    llm = FakeLLM([])
    assert context.compact(one_step, "summary", 100, llm, SPECS) == (one_step, None)
    assert llm.calls == []


def summary_script():
    """四步工具调用 + 两次压缩：第 3 步之后和第 4 步之后各压缩一次；第 4 步重复了第 1 步的 list_dir。"""
    return [tool_reply(("c1", "list_dir", {})), tool_reply(("c2", "search", {"pattern": "hello"})),
            tool_reply(("c3", "read_file", {"path": "app.py"})), answer_reply("摘要一：读过 app.py"),
            tool_reply(("c4", "list_dir", {})), answer_reply("摘要二"), answer_reply("done")]


def test_agent_summarizes_and_allows_rereading_after(tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    llm = FakeLLM(summary_script())
    settings = Settings(compaction="summary", max_context_chars=10)
    result = run("q", llm, Tools(tmp_path), "demo", on_step=lambda s: None, settings=settings)

    assert result.answer == "done" and len(result.compactions) == 2
    assert [c["after_step"] for c in result.compactions] == [3, 4]
    assert result.steps[3].tool == "list_dir" and not result.steps[3].repeated  # 摘要之后允许重新执行
    assert result.trimmed == []  # 下标已经变了，不再计算
    assert llm.calls[3]["tool_choice"] == "none"  # 第 4 次调用是摘要
    summaries = [m for m in result.transcript if m["content"] and str(m["content"]).startswith(context.SUMMARY_HEADER)]
    assert len(summaries) == 2


def test_trace_counts_compactions_and_rereads_and_replay_shows_them(tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    tools = Tools(tmp_path)
    script = [api_reply(tool_calls=[("c1", "list_dir", {})]),
              api_reply(tool_calls=[("c2", "search", {"pattern": "hello"})]),
              api_reply(tool_calls=[("c3", "read_file", {"path": "app.py"})]), api_reply("摘要一"),
              api_reply(tool_calls=[("c4", "list_dir", {})]), api_reply("摘要二"), api_reply("done")]
    llm = scripted_client(*script)
    settings = Settings(compaction="summary", max_context_chars=10)
    result = run("q", llm, tools, "demo", on_step=lambda s: None, settings=settings)
    record = trace.build("q", "demo", llm, tools, settings, 10, result.transcript, result)

    stats = trace.stats(record)
    assert stats == {"compactions": 2, "rereads": 1, "peak_input_tokens": 150}
    shown = trace.replay(record)
    assert shown.count("[压缩]") == 2 and "摘要一" in shown
    assert "[调用 7]" in shown  # 摘要调用也占调用编号，和 call_log 对得上


def test_an_empty_summary_falls_back_to_trim_instead_of_losing_evidence():
    messages = history([[3000]] * 6)
    empty = FakeLLM([tool_reply(("x", "search", {"pattern": "p"}))])  # 没有文字的回复
    compacted, event = context.compact(messages, "summary", 10_000, empty, SPECS)
    assert len(compacted) == len(messages)  # 没有删消息
    assert not any(str(m["content"]).startswith(context.SUMMARY_HEADER) for m in compacted)
    assert (event["kind"], event["fallback"]) == ("trim", "summary_empty")
    assert total(compacted) <= 10_000
