"""运行轨迹：把一次运行的全过程存成 JSON，事后逐步回看（对应 M1 ContextEngine 规范第 16 节的可观测性）。

轨迹里有完整 messages（工具结果是 trim 之前的原文）、每次模型调用的耗时和用量、Settings，
以及系统提示词和工具定义的哈希，评测结果靠它说清用的是哪一版。轨迹包含仓库内容，只存在本地 runs/，不进仓库。
回放：python -m mini_agent.trace runs/traces/xxx.json [--full]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from mini_agent.context import SUMMARY_HEADER

PREVIEW_CHARS = 160


def fingerprint(value) -> str:
    """内容哈希的前 12 位；dict 先按键排序再序列化，同样的内容总是得到同样的哈希。"""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def build(question: str, repo_name: str, llm, tools, settings, max_steps: int,
          transcript: list[dict], result=None, error: str | None = None) -> dict:
    """result 为 None 表示运行中途出错，这时只有 transcript 和已完成的模型调用。
    只从 llm 取 model、usage、call_log，不碰 api_key。"""
    system_prompt = transcript[0]["content"] if transcript else ""
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": question,
        "repo": repo_name,
        "model": llm.model,
        "settings": asdict(settings),
        "max_steps": max_steps,
        "prompt_hash": fingerprint(system_prompt),
        "tools_hash": fingerprint(tools.specs()),
        "stopped_by": result.stopped_by if result else "error",
        "error": error,
        "answer": result.answer if result else None,
        "usage": dict(vars(llm.usage)),
        "model_calls": llm.call_log,
        "steps": [vars(step) for step in result.steps] if result else [],
        "trimmed": result.trimmed if result else [],
        "compactions": result.compactions if result else [],
        "messages": transcript,
    }


def save(trace: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    # 精确到微秒，再加问题的哈希：评测并发运行时文件名也不会撞
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"trace-{stamp}-{fingerprint(trace['question'])[:6]}.json"
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def stats(trace: dict) -> dict:
    """评测用的三个数：压缩次数、压缩后重读（和之前完全相同、但因为原结果被换掉而真正重新执行的调用）、
    单次调用的最大输入 token（上下文峰值）。"""
    seen, rereads = set(), 0
    for step in trace["steps"]:
        if step["tool"] is None:
            continue
        key = f"{step['tool']} {json.dumps(step['arguments'], sort_keys=True, ensure_ascii=False)}"
        if key in seen and not step["repeated"]:
            rereads += 1
        seen.add(key)
    peak = max((c["cache_hit"] + c["cache_miss"] for c in trace["model_calls"]), default=0)
    return {"compactions": len(trace.get("compactions", [])), "rereads": rereads, "peak_input_tokens": peak}


def replay(trace: dict, full: bool = False) -> str:
    """按模型调用逐步列出：模型说了什么、调了哪些工具、每个结果多长；最后是回答和用量。"""
    lines = [f"问题：{trace['question']}",
             f"仓库：{trace['repo']}  模型：{trace['model']}  "
             f"提示词 {trace['prompt_hash']}  工具 {trace['tools_hash']}",
             f"设置：{json.dumps(trace['settings'], ensure_ascii=False)}  max_steps={trace['max_steps']}"]
    trimmed = set(trace["trimmed"])
    number = 0
    for index, message in enumerate(trace["messages"]):
        if message["role"] == "assistant":
            number += 1
            lines += ["", _call_header(number, trace["model_calls"])]
            calls = message.get("tool_calls") or []
            if calls and message.get("content"):  # 没有工具调用的那条就是回答，放到最后完整打印
                lines.append("  说：" + _preview(message["content"], full))
            for call in calls:
                lines.append(f"  调用 {call['function']['name']} {call['function']['arguments']}")
        elif message["role"] == "tool":
            note = "（后来被 trim 替换）" if index in trimmed else ""
            lines.append(f"  结果 {len(message['content'])} 字符{note}：{_preview(message['content'], full)}")
        elif message["role"] == "user" and message["content"].startswith(SUMMARY_HEADER):
            number += 1  # 摘要也是一次模型调用，占一个编号
            lines += ["", "[压缩] " + _call_header(number, trace["model_calls"]),
                      "  摘要：" + _preview(message["content"][len(SUMMARY_HEADER):].strip(), full)]
        elif message["role"] == "user" and index > 1:  # 前两条是 system 和问题；其余 user 是 FINAL_PROMPT
            lines += ["", "追加：" + message["content"]]
    if trace["error"]:
        lines += ["", f"出错：{trace['error']}"]
    if trace["answer"] is not None:
        lines += ["", "回答：", trace["answer"]]
    u = trace["usage"]
    lines += ["", f"结束：{trace['stopped_by']}  调用 {u['calls']} 次  "
                  f"输入 {u['cache_hit'] + u['cache_miss']:,}（命中 {u['cache_hit']:,}）  "
                  f"输出 {u['output']:,}  ≈${u['cost_usd']:.4f}"]
    return "\n".join(lines)


def _call_header(number: int, calls: list[dict]) -> str:
    if number > len(calls):  # 这次调用失败了，没有用量记录
        return f"[调用 {number}]"
    c = calls[number - 1]
    forced = "  tool_choice=none" if c["tool_choice"] == "none" else ""
    return (f"[调用 {number}] {c['latency_ms'] / 1000:.1f}s  命中 {c['cache_hit']:,}  "
            f"未命中 {c['cache_miss']:,}  输出 {c['output']:,}  ${c['cost_usd']:.4f}{forced}")


def _preview(text: str, full: bool) -> str:
    if full:
        return text
    flat = text.replace("\n", " ↵ ")
    return flat if len(flat) <= PREVIEW_CHARS else flat[:PREVIEW_CHARS] + "…"


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mini_agent.trace", description="逐步回看一次运行")
    parser.add_argument("file", help="runs/traces/ 下的轨迹文件")
    parser.add_argument("--full", action="store_true", help="完整打印每条内容，不截断")
    args = parser.parse_args()
    print(replay(json.loads(Path(args.file).read_text(encoding="utf-8")), full=args.full))


if __name__ == "__main__":
    main()
