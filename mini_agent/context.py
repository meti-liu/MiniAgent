"""上下文：决定每次放进模型“眼里”的是什么（对应 M1 的 ContextEngine）。

SYSTEM_PROMPT 一字不变，保证每次调用的开头相同、能命中缓存；会变的仓库名、仓库概览和问题放进第一条用户消息。
超限时用 compact 压缩（规划 12.5）：trim 把最早的工具结果换成占位符、刚好降到上限以下；
clear 同样换占位符但一次清到上限的一半；summary 让模型把较早的轮次写成摘要。
"""

from __future__ import annotations

import json

BASE_PROMPT = """你是一个代码仓库问答助手，帮助用户理解一个本地代码仓库。

## 工作方式
- 你看不到仓库内容，只能通过工具获取信息；不要凭记忆或猜测回答。
- 第一条消息如果附有仓库概览，先用它判断该去哪里找，不必从根目录逐层 list_dir。
- 先用 search 或 list_dir 定位，再用 read_file 读取相关的几行；不要整文件通读。
- 可以在一次回复里同时调用多个工具。
- 工具返回 ERROR 时，读懂原因后换参数重试。

## 回答要求
- 用中文回答，先给结论，再给依据，保持简洁。
- 每个关键结论后面标注来源，格式为 `路径:起始行-结束行`，必须是你用工具实际看到过的行。
- 信息不足时明确说出还缺什么，不要编造。"""

# 第 5 步根据试跑加上的两条规则；消融实验可以关掉它们（Settings.strict_prompt=False）
STRICT_RULES = """
- 只对实际读到的内容下结论；如果某个文件只看过搜索命中的几行，不要推断它其余部分写了什么或没写什么。
- 直接输出最终回答，不要描述你的检查过程。"""

SYSTEM_PROMPT = BASE_PROMPT + STRICT_RULES


SUMMARY_HEADER = "【之前步骤的摘要】"
SUMMARY_PROMPT = """上下文太长了。请把到目前为止的工作写成摘要，用来替换之前的对话，之后你只能看到这份摘要和最近的几步。
按下面四个小标题写，只写你实际读到过的内容，不要推测：
1. 目标：用户要回答的问题
2. 已确认的结论：每条后面标注来源，格式为 `完整路径:起始行-结束行`（从仓库根目录写起）
3. 已经读过的文件和行范围：需要细节时可以重新读取
4. 还缺什么：还没确认的问题和打算去看的位置
只输出摘要本身，不要调用工具。"""


def initial(question: str, repo_name: str, overview: str = "", strict: bool = True) -> list[dict]:
    # 顺序：仓库名 → 目录概览 → 问题。同一仓库的概览相同，放在问题前面，换问题时这段也能命中缓存
    parts = [f"仓库：{repo_name}"]
    if overview:
        parts.append(f"仓库概览：\n\n{overview}")
    parts.append(f"问题：{question}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT if strict else BASE_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _size(message: dict) -> int:
    """用字符数近似 token 数；assistant 消息的 tool_calls 也算进去。"""
    size = len(message.get("content") or "")
    if message.get("tool_calls"):
        size += len(json.dumps(message["tool_calls"], ensure_ascii=False))
    return size


def trim(messages: list[dict], max_chars: int = 60_000, target_chars: int | None = None) -> list[dict]:
    """超过 max_chars 时，从最早的 tool 结果开始换成占位符，直到降到 target_chars（默认等于 max_chars）。

    只替换 tool 消息的内容、不删除消息，这样 tool_calls 和 tool 结果依然成对。
    system、用户问题（前两条）和受保护的最近部分（见 _protected_start）不动。
    """
    total = sum(_size(m) for m in messages)
    if total <= max_chars:
        return messages
    target = max_chars if target_chars is None else target_chars
    trimmed = list(messages)  # 不修改调用方传进来的列表
    for i in range(2, _protected_start(messages, max_chars)):
        if total <= target:
            break
        message = trimmed[i]
        content = message.get("content") or ""
        if message["role"] != "tool" or content.startswith("[已省略"):
            continue
        placeholder = f"[已省略，共 {len(content)} 字符；需要时请重新调用工具]"
        trimmed[i] = {**message, "content": placeholder}
        total -= len(content) - len(placeholder)
    return trimmed


def compact(messages: list[dict], strategy: str, max_chars: int,
            llm=None, specs: list[dict] | None = None) -> tuple[list[dict], dict | None]:
    """不超限时原样返回 (messages, None)；否则按 strategy 压缩，返回新的消息列表和这次压缩的记录。"""
    before = sum(_size(m) for m in messages)
    if before <= max_chars:
        return messages, None
    if strategy == "summary":
        return _summarize(messages, llm, specs, before, max_chars)
    target = max_chars // 2 if strategy == "clear" else max_chars  # clear 一次清得多，之后很多步都不用再改前缀
    compacted = trim(messages, max_chars, target)
    replaced = sum(1 for old, new in zip(messages, compacted) if old is not new)
    if replaced == 0:  # 能换的都换过了（剩下的都在受保护部分里）
        return compacted, None
    return compacted, {"kind": strategy, "replaced": replaced, "chars_before": before,
                       "chars_after": sum(_size(m) for m in compacted)}


def _protected_start(messages: list[dict], max_chars: int) -> int:
    """受保护部分从哪个下标开始。最新一轮（最后一条 assistant 和它的全部工具结果）模型还没看过，无条件保护；
    更早的消息从后往前累加，总共不超过 max_chars 的一半。按大小而不是条数算，否则几次大的读文件就会让每一步都要压缩。"""
    start = len(messages)
    for i in range(len(messages) - 1, 1, -1):
        if messages[i]["role"] == "assistant":
            start = i
            break
    kept = sum(_size(m) for m in messages[start:])
    while start > 2 and kept + _size(messages[start - 1]) <= max_chars // 2:
        start -= 1
        kept += _size(messages[start])
    return start


def _summarize(messages: list[dict], llm, specs, before: int, max_chars: int) -> tuple[list[dict], dict | None]:
    """把 system、问题之后、受保护部分之前的消息换成一条摘要。"""
    cut = _protected_start(messages, max_chars)
    while messages[cut]["role"] == "tool":  # 不能把 tool_calls 和它的结果拆开：往后挪到下一条 assistant，宁可少保护一点
        cut += 1
    if cut <= 2:
        return messages, None
    if sum(_size(m) for m in messages[2:cut]) < max_chars // 4:  # 换掉的太少，一份摘要（实测平均 3.7k 字符）省不下什么
        return messages, None
    # 前缀和主对话完全相同、工具照传但禁止调用，所以这次请求的大部分输入都能命中缓存
    reply = llm.chat(messages[:cut] + [{"role": "user", "content": SUMMARY_PROMPT}], specs, tool_choice="none")
    summary = (reply.text or "").strip()
    if not summary:  # 空摘要会把这段证据整个丢掉；退回 trim，至少还留着“读过什么、多长”
        compacted, event = compact(messages, "trim", max_chars)
        return compacted, event and {**event, "fallback": "summary_empty"}
    note = {"role": "user", "content": f"{SUMMARY_HEADER}\n{summary}"}
    compacted = messages[:2] + [note] + messages[cut:]
    return compacted, {"kind": "summary", "replaced": cut - 2, "chars_before": before,
                       "chars_after": sum(_size(m) for m in compacted), "summary": summary}
