"""循环：agent 的“心跳”（对应 M1 的 Workflow）。

模型决定下一步 → 程序执行工具 → 结果喂回模型，直到模型不再调用工具（给出回答）或达到 max_steps。
“一步”指一次模型调用；同一次调用里的多个工具调用共用一个步号，逐条打印。
和之前完全相同的工具调用不再执行（见 _run_tool），Settings 集中放消融实验的开关。
transcript 是只追加的完整记录（工具结果是压缩之前的原文，摘要也记在里面），用来写运行轨迹（trace.py）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mini_agent import context
from mini_agent.tools import Tools

FINAL_PROMPT = "已达到步数上限。请根据已获得的信息直接回答，不要再调用工具。"
COMPACTION_COOLDOWN = 2  # 一次压缩后仍超限，接下来几步不再压缩，免得每一步都改动前缀（规划 12.5 的 B1.1）
REPEAT_NOTE = "NOTE: 这个调用和前面某次完全相同，结果就在上文，没有重新执行。请换参数，或根据已有信息回答。"


@dataclass(frozen=True)
class Settings:
    """消融实验的开关；默认值就是正常运行的配置。frozen=True 让它可以安全地当默认参数。"""
    overview: str = "full"  # "full" | "tree" | "none"
    dedup: bool = True  # 跳过和之前完全相同的工具调用
    strict_prompt: bool = True  # 系统提示词是否包含 STRICT_RULES
    compaction: str = "clear"  # 超限时怎么压缩："clear" | "trim" | "summary"（规划 12.5；第 12 轮之后默认 clear）
    max_context_chars: int = 60_000  # 上下文超过这么多字符就压缩


@dataclass
class Step:
    number: int
    tool: str | None  # None 表示这一步是最终回答
    arguments: dict | None
    result_chars: int
    repeated: bool = False  # 重复调用，没有真正执行


@dataclass
class RunResult:
    answer: str
    steps: list[Step]
    stopped_by: str  # "answer" | "max_steps"
    transcript: list[dict]  # 所有消息的原文，顺序和下标与模型看到的 messages 一致
    trimmed: list[int]  # 运行结束时已被 trim 换成占位符的消息下标（做过 summary 后下标对不上，为空）
    compactions: list[dict]  # 每次压缩一条：第几步之后、换掉几条、前后字符数（summary 还有摘要原文）


def print_step(step: Step) -> None:
    if step.tool is None:
        print(f"[step {step.number}] answer")
    else:
        arguments = json.dumps(step.arguments, ensure_ascii=False)
        note = "（重复，未执行）" if step.repeated else f"-> {step.result_chars} chars"
        print(f"[step {step.number}] {step.tool} {arguments} {note}")


def run(question: str, llm, tools: Tools, repo_name: str,
        max_steps: int = 10, on_step=print_step, settings: Settings = Settings(),
        transcript: list[dict] | None = None) -> RunResult:
    overview = tools.overview(settings.overview)  # 开头就给出仓库概览
    messages = context.initial(question, repo_name, overview, strict=settings.strict_prompt)
    # 调用方传入列表时，模型调用中途出错也能从这个列表拿到已经发生的部分
    transcript = [] if transcript is None else transcript
    transcript.extend(messages)
    specs = tools.specs()  # 整个运行过程中工具集不变，前缀才能命中缓存
    steps: list[Step] = []
    seen: dict[str, int] = {}  # 调用签名 -> 那次结果在 messages 里的下标
    compactions: list[dict] = []
    cooldown = 0  # 还要跳过几步压缩

    for number in range(1, max_steps + 1):
        reply = llm.chat(messages, specs)
        messages.append(reply.message)  # 让模型下一轮“记得”自己说过什么、调过什么
        transcript.append(reply.message)

        if not reply.tool_calls:  # 没有工具调用 = 模型认为可以回答了
            return _finish(reply.text, number, steps, "answer", on_step, messages, transcript, compactions)

        for call in reply.tool_calls:
            result, repeated = _run_tool(call, tools, messages, seen, settings.dedup)
            tool_message = {"role": "tool", "tool_call_id": call.id, "content": result}
            messages.append(tool_message)
            transcript.append(tool_message)
            step = Step(number, call.name, call.arguments, len(result), repeated)
            steps.append(step)
            on_step(step)
        if cooldown > 0:
            cooldown -= 1
            continue
        messages, event = context.compact(messages, settings.compaction, settings.max_context_chars, llm, specs)
        if event:
            compactions.append({"after_step": number, **event})
            if event["chars_after"] > settings.max_context_chars:  # 压缩了也降不下来：先停几步
                cooldown = COMPACTION_COOLDOWN
            if event["kind"] == "summary":
                transcript.append(messages[2])  # 摘要那条消息也记进原始记录，回放时能看到
                seen.clear()  # 消息下标变了；被摘要掉的调用允许重新执行

    # 步数用完：工具集照传，但禁止调用工具，逼模型基于已有信息作答
    final_prompt = {"role": "user", "content": FINAL_PROMPT}
    messages.append(final_prompt)
    transcript.append(final_prompt)
    reply = llm.chat(messages, specs, tool_choice="none")
    messages.append(reply.message)
    transcript.append(reply.message)
    return _finish(reply.text, max_steps + 1, steps, "max_steps", on_step, messages, transcript, compactions)


def _run_tool(call, tools: Tools, messages: list[dict], seen: dict[str, int],
              dedup: bool) -> tuple[str, bool]:
    """执行一个工具调用；和之前完全相同、且那次结果还在上下文里时，不再执行。"""
    key = f"{call.name} {json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
    earlier = seen.get(key)
    if dedup and earlier is not None and not messages[earlier]["content"].startswith("[已省略"):
        return REPEAT_NOTE, True
    seen[key] = len(messages)  # 结果马上会 append 到这个位置；trim 只替换不删除，下标不会变
    return tools.run(call.name, call.arguments), False  # 出错也只是返回 "ERROR: ..." 字符串


def _finish(text: str | None, number: int, steps: list[Step], stopped_by: str, on_step,
            messages: list[dict], transcript: list[dict], compactions: list[dict]) -> RunResult:
    answer = text or "(模型没有给出回答)"
    step = Step(number, None, None, len(answer))
    steps.append(step)
    on_step(step)
    # trim / clear 只替换不删除，所以 messages 和 transcript 的下标一一对应；做过 summary 就不再对应
    summarized = any(c["kind"] == "summary" for c in compactions)
    trimmed = [] if summarized else [i for i, m in enumerate(messages)
                                     if m["role"] == "tool" and m["content"].startswith("[已省略")]
    return RunResult(answer, steps, stopped_by, transcript, trimmed, compactions)
