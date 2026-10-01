"""测试运行轨迹：每次模型调用都有记录、trim 前的原文保留下来、出错时也有记录、哈希能区分提示词版本、能回放、不含密钥。不联网。"""

import json

import pytest

from mini_agent import trace
from mini_agent.agent import Settings, run
from mini_agent.llm import LLMClient, LLMError
from mini_agent.tools import Tools

SECRET = "sk-test-secret"


def api_reply(content=None, tool_calls=(), hit=100, miss=50, out=20):
    """构造一份 DeepSeek 风格的响应 JSON；tool_calls 是 (id, 工具名, 参数) 元组。"""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [{"id": i, "type": "function",
                                  "function": {"name": n, "arguments": json.dumps(a)}}
                                 for i, n, a in tool_calls]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss,
                      "completion_tokens": out}}


def scripted_client(*replies):
    """真正的 LLMClient，只把发 HTTP 的 _post 换成按剧本返回；剧本里放异常就抛出。"""
    llm = LLMClient(SECRET, model="test-model")
    queue = list(replies)

    def fake_post(body):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    llm._post = fake_post
    return llm


def search(i):
    return api_reply(tool_calls=[(f"c{i}", "search", {"pattern": "hello", "path": "app.py"})])


@pytest.fixture
def tools(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    return Tools(repo)


def test_chat_logs_latency_usage_and_cost_per_call():
    llm = scripted_client(api_reply("hi"))
    llm.chat([{"role": "user", "content": "hi"}])
    entry = llm.call_log[0]
    assert (entry["cache_hit"], entry["cache_miss"], entry["output"]) == (100, 50, 20)
    assert entry["cost_usd"] == pytest.approx(llm.usage.cost_usd)
    assert entry["latency_ms"] >= 0 and entry["finish_reason"] == "stop"


def test_transcript_keeps_original_tool_results_after_trim(tools):
    # 把上限调到 50 字符，第 3 步之后最早的工具结果（下标 3）就会被换成占位符
    llm = scripted_client(search(1), search(2), search(3), api_reply("done"))
    result = run("q", llm, tools, "repo", on_step=lambda step: None, settings=Settings(max_context_chars=50))
    assert result.trimmed == [3]
    assert result.transcript[3]["role"] == "tool"
    assert result.transcript[3]["content"].startswith("app.py:1:")  # 原文，不是占位符
    assert len(result.transcript) == 9  # system、问题、3 组（调用 + 结果）、回答


def test_transcript_survives_a_failed_model_call(tools):
    transcript = []
    llm = scripted_client(search(1), LLMError("HTTP 500"))
    with pytest.raises(LLMError):
        run("q", llm, tools, "repo", on_step=lambda step: None, transcript=transcript)
    assert [m["role"] for m in transcript] == ["system", "user", "assistant", "tool"]


def test_build_save_and_replay(tools, tmp_path):
    llm = scripted_client(search(1), api_reply("hello 在 app.py:1"))
    result = run("hello 在哪？", llm, tools, "repo", on_step=lambda step: None)
    record = trace.build("hello 在哪？", "repo", llm, tools, Settings(), 10, result.transcript, result)
    path = trace.save(record, tmp_path / "traces")

    text = path.read_text(encoding="utf-8")
    assert SECRET not in text  # 轨迹里不能有 API key
    loaded = json.loads(text)
    assert loaded["stopped_by"] == "answer" and len(loaded["model_calls"]) == 2
    assert loaded["messages"][0]["role"] == "system"

    shown = trace.replay(loaded)
    assert "[调用 1]" in shown and "search" in shown and "hello 在 app.py:1" in shown


def test_failed_run_is_still_traced(tools):
    transcript = []
    llm = scripted_client(search(1), LLMError("HTTP 500"))
    with pytest.raises(LLMError):
        run("q", llm, tools, "repo", on_step=lambda step: None, transcript=transcript)
    record = trace.build("q", "repo", llm, tools, Settings(), 10, transcript, error="HTTP 500")
    assert record["stopped_by"] == "error" and record["answer"] is None
    assert "出错：HTTP 500" in trace.replay(record)


def test_prompt_hash_tells_prompt_versions_apart(tools):
    hashes = []
    for settings in (Settings(), Settings(strict_prompt=False)):
        llm = scripted_client(api_reply("a"))
        result = run("q", llm, tools, "repo", on_step=lambda step: None, settings=settings)
        hashes.append(trace.build("q", "repo", llm, tools, settings, 10, result.transcript, result))
    assert hashes[0]["prompt_hash"] != hashes[1]["prompt_hash"]
    assert hashes[0]["tools_hash"] == hashes[1]["tools_hash"]


def test_replay_marks_trimmed_results_and_the_forced_final_call(tools):
    settings = Settings(max_context_chars=50)
    llm = scripted_client(search(1), search(2), search(3), api_reply("forced"))
    result = run("q", llm, tools, "repo", max_steps=3, on_step=lambda step: None, settings=settings)
    shown = trace.replay(trace.build("q", "repo", llm, tools, settings, 3, result.transcript, result))
    assert "后来被 trim 替换" in shown
    assert "tool_choice=none" in shown and "已达到步数上限" in shown
