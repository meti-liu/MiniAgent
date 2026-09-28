"""循环：agent 的“心跳”（对应 M1 的 Workflow）。

模型决定下一步 → 程序执行工具 → 结果喂回模型，直到模型不再调用工具（给出回答）或达到 max_steps。
“一步”指一次模型调用；同一次调用里的多个工具调用共用一个步号，逐条打印。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mini_agent import context
from mini_agent.tools import Tools

FINAL_PROMPT = "已达到步数上限。请根据已获得的信息直接回答，不要再调用工具。"


@dataclass
class Step:
    number: int
    tool: str | None  # None 表示这一步是最终回答
    arguments: dict | None
    result_chars: int


@dataclass
class RunResult:
    answer: str
    steps: list[Step]
    stopped_by: str  # "answer" | "max_steps"


def print_step(step: Step) -> None:
    if step.tool is None:
        print(f"[step {step.number}] answer")
    else:
        arguments = json.dumps(step.arguments, ensure_ascii=False)
        print(f"[step {step.number}] {step.tool} {arguments} -> {step.result_chars} chars")


def run(question: str, llm, tools: Tools, repo_name: str,
        max_steps: int = 10, on_step=print_step) -> RunResult:
    messages = context.initial(question, repo_name)
    specs = tools.specs()  # 整个运行过程中工具集不变，前缀才能命中缓存
    steps: list[Step] = []

    for number in range(1, max_steps + 1):
        reply = llm.chat(messages, specs)
        messages.append(reply.message)  # 让模型下一轮“记得”自己说过什么、调过什么

        if not reply.tool_calls:  # 没有工具调用 = 模型认为可以回答了
            return _finish(reply.text, number, steps, "answer", on_step)

        for call in reply.tool_calls:
            result = tools.run(call.name, call.arguments)  # 出错也只是返回 "ERROR: ..." 字符串
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            step = Step(number, call.name, call.arguments, len(result))
            steps.append(step)
            on_step(step)
        messages = context.trim(messages)  # 太长时把最早的工具结果换成占位符

    # 步数用完：工具集照传，但禁止调用工具，逼模型基于已有信息作答
    messages.append({"role": "user", "content": FINAL_PROMPT})
    reply = llm.chat(messages, specs, tool_choice="none")
    return _finish(reply.text, max_steps + 1, steps, "max_steps", on_step)


def _finish(text: str | None, number: int, steps: list[Step],
            stopped_by: str, on_step) -> RunResult:
    answer = text or "(模型没有给出回答)"
    step = Step(number, None, None, len(answer))
    steps.append(step)
    on_step(step)
    return RunResult(answer, steps, stopped_by)
