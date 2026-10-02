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
    assert total(cleared) < total(trimmed)  # 一次往上限的一半清，比 trim 清得多
    assert by_trim["kind"] == "trim" and by_clear["kind"] == "clear"
    assert by_clear["replaced"] > by_trim["replaced"]
    assert context.compact(messages, "trim", 10**6) == (messages, None)  # 不超限时什么都不做


def paired(messages):
    """真实 API 的规则：带 tool_calls 的 assistant 之后紧跟它的全部 tool 结果；没有孤立的 tool 消息。"""
    pending = []
    for m in messages:
        if m["role"] == "tool":
            if not pending or m["tool_call_id"] != pending.pop(0):
                return False
        elif pending:
            return False
        elif m["role"] == "assistant" and m.get("tool_calls"):
            pending = [c["id"] for c in m["tool_calls"]]
    return not pending


def test_the_latest_turn_is_never_trimmed_even_with_many_tool_calls():
    messages = history([[1000], [1000], [1000] * 5])  # 最后一步同时调了 5 个工具
    trimmed, _ = context.compact(messages, "trim", 3000)
    assert all(m["content"] == "y" * 1000 for m in trimmed[-5:])  # 模型还没看过这 5 个结果，一个都不能换
    assert trimmed[3]["content"].startswith("[已省略") and trimmed[5]["content"].startswith("[已省略")


def test_older_messages_are_protected_by_size_not_count():
    messages = history([[4000]] * 6)
    trimmed, _ = context.compact(messages, "trim", 8000)
    assert trimmed[-3]["content"].startswith("[已省略")  # 上一步的结果超过上限的一半，不再受保护
    assert trimmed[-1]["content"] == "y" * 4000  # 最新一轮仍然受保护


def test_summary_replaces_everything_before_the_protected_part():
    messages = history([[100], [100, 100], [100], [100]])  # 下标 9 是最新一轮的 assistant
    llm = FakeLLM([answer_reply("目标：……")])
    compacted, event = context.compact(messages, "summary", 100, llm, SPECS)

    assert [m["role"] for m in compacted] == ["system", "user", "user", "assistant", "tool"]
    assert compacted[2]["content"].startswith(context.SUMMARY_HEADER) and "目标：……" in compacted[2]["content"]
    assert compacted[3:] == messages[9:]  # 最新一轮原样保留
    assert (event["kind"], event["replaced"], event["summary"]) == ("summary", 7, "目标：……")

    request = llm.calls[0]  # 摘要请求复用同一个前缀，只在末尾加提示词；工具照传但禁止调用
    assert request["messages"][:-1] == messages[:9]
    assert request["messages"][-1]["content"] == context.SUMMARY_PROMPT
    assert (request["tools"], request["tool_choice"]) == (SPECS, "none")


def test_summary_cut_never_splits_a_tool_call_from_its_results():
    messages = history([[100], [100, 100, 100], [100], [100, 100]])
    for limit in range(200, 3000, 50):  # 不同上限下，受保护部分的开头会落在各种位置上
        compacted, _ = context.compact(messages, "summary", limit, FakeLLM([answer_reply("s")]), SPECS)
        assert paired(compacted), limit

    one_step = history([[100, 100, 100, 100]])  # 只有一步：全是最新一轮，没有可以压缩的
    llm = FakeLLM([])
    assert context.compact(one_step, "summary", 100, llm, SPECS) == (one_step, None)
    assert llm.calls == []


def test_summary_is_skipped_when_too_little_would_be_replaced():
    messages = history([[100], [100], [30000]])  # 能换掉的只有前两步，不到上限的四分之一
    llm = FakeLLM([])
    assert context.compact(messages, "summary", 20_000, llm, SPECS) == (messages, None)
    assert llm.calls == []


def test_after_an_ineffective_compaction_the_next_two_steps_skip_it(tmp_path):
    replies = [tool_reply((f"c{i}", "search", {"pattern": str(i)})) for i in range(1, 7)] + [answer_reply("done")]
    result = run("q", FakeLLM(replies), Tools(tmp_path), "demo", on_step=lambda s: None,
                 settings=Settings(max_context_chars=10))
    # 第 1 步只有最新一轮、没什么可换；第 2 步压缩后仍超限，于是第 3、4 步跳过，第 5 步再压缩
    assert [c["after_step"] for c in result.compactions] == [2, 5]


def summary_script():
    """5 步工具调用 + 2 次摘要：第 2 步之后摘要，冷却 2 步，第 5 步之后再摘要；第 4 步重复了第 1 步的 list_dir。"""
    return [tool_reply(("c1", "list_dir", {})), tool_reply(("c2", "search", {"pattern": "hello"})),
            answer_reply("摘要一：列过目录、搜过 hello"), tool_reply(("c3", "read_file", {"path": "app.py"})),
            tool_reply(("c4", "list_dir", {})), tool_reply(("c5", "search", {"pattern": "hi"})),
            answer_reply("摘要二"), answer_reply("done")]


def test_agent_summarizes_and_allows_rereading_after(tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    llm = FakeLLM(summary_script())
    settings = Settings(compaction="summary", max_context_chars=10)
    result = run("q", llm, Tools(tmp_path), "demo", on_step=lambda s: None, settings=settings)

    assert result.answer == "done" and len(result.compactions) == 2
    assert [c["after_step"] for c in result.compactions] == [2, 5]
    assert result.steps[3].tool == "list_dir" and not result.steps[3].repeated  # 摘要之后允许重新执行
    assert result.trimmed == []  # 下标已经变了，不再计算
    assert llm.calls[2]["tool_choice"] == "none"  # 第 3 次调用是摘要
    summaries = [m for m in result.transcript if m["content"] and str(m["content"]).startswith(context.SUMMARY_HEADER)]
    assert len(summaries) == 2


def test_trace_counts_compactions_and_rereads_and_replay_shows_them(tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    tools = Tools(tmp_path)
    script = [api_reply(tool_calls=[("c1", "list_dir", {})]),
              api_reply(tool_calls=[("c2", "search", {"pattern": "hello"})]), api_reply("摘要一"),
              api_reply(tool_calls=[("c3", "read_file", {"path": "app.py"})]),
              api_reply(tool_calls=[("c4", "list_dir", {})]),
              api_reply(tool_calls=[("c5", "search", {"pattern": "hi"})]), api_reply("摘要二"), api_reply("done")]
    llm = scripted_client(*script)
    settings = Settings(compaction="summary", max_context_chars=10)
    result = run("q", llm, tools, "demo", on_step=lambda s: None, settings=settings)
    record = trace.build("q", "demo", llm, tools, settings, 10, result.transcript, result)

    stats = trace.stats(record)
    assert stats == {"compactions": 2, "rereads": 1, "peak_input_tokens": 150}
    shown = trace.replay(record)
    assert shown.count("[压缩]") == 2 and "摘要一" in shown
    assert "[调用 8]" in shown  # 摘要调用也占调用编号，和 call_log 对得上


def test_an_empty_summary_falls_back_to_trim_instead_of_losing_evidence():
    messages = history([[3000]] * 6)
    empty = FakeLLM([tool_reply(("x", "search", {"pattern": "p"}))])  # 没有文字的回复
    compacted, event = context.compact(messages, "summary", 10_000, empty, SPECS)
    assert len(compacted) == len(messages)  # 没有删消息
    assert not any(str(m["content"]).startswith(context.SUMMARY_HEADER) for m in compacted)
    assert (event["kind"], event["fallback"]) == ("trim", "summary_empty")
    assert total(compacted) <= 10_000
