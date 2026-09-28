"""上下文：决定每次放进模型“眼里”的是什么（对应 M1 的 ContextEngine）。

SYSTEM_PROMPT 一字不变，保证每次调用的开头相同、能命中缓存；会变的仓库名、目录概览和问题放进第一条用户消息。
trim 在总长度超限时，把最早的工具结果换成占位符，控制上下文长度。
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

KEEP_RECENT = 4  # 最近几条消息不裁剪


def initial(question: str, repo_name: str, overview: str = "", strict: bool = True) -> list[dict]:
    # 顺序：仓库名 → 目录概览 → 问题。同一仓库的概览相同，放在问题前面，换问题时这段也能命中缓存
    parts = [f"仓库：{repo_name}"]
    if overview:
        parts.append(f"目录概览（深度 ≤3，每个目录最多 20 项）：\n{overview}")
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


def trim(messages: list[dict], max_chars: int = 60_000) -> list[dict]:
    """超过 max_chars 时，从最早的 tool 结果开始换成占位符，直到不超限。

    只替换 tool 消息的内容、不删除消息，这样 tool_calls 和 tool 结果依然成对。
    system、用户问题（前两条）和最近 KEEP_RECENT 条消息不动。
    """
    total = sum(_size(m) for m in messages)
    if total <= max_chars:
        return messages
    trimmed = list(messages)  # 不修改调用方传进来的列表
    for i in range(2, len(trimmed) - KEEP_RECENT):
        if total <= max_chars:
            break
        message = trimmed[i]
        content = message.get("content") or ""
        if message["role"] != "tool" or content.startswith("[已省略"):
            continue
        placeholder = f"[已省略，共 {len(content)} 字符；需要时请重新调用工具]"
        trimmed[i] = {**message, "content": placeholder}
        total -= len(content) - len(placeholder)
    return trimmed
