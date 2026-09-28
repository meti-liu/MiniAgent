"""上下文：决定每次放进模型“眼里”的是什么（对应 M1 的 ContextEngine）。

SYSTEM_PROMPT 一字不变，保证每次调用的开头相同、能命中缓存；会变的仓库名和问题放进第一条用户消息。
trim 在总长度超限时，把最早的工具结果换成占位符，控制上下文长度。
"""

from __future__ import annotations

import json

SYSTEM_PROMPT = """你是一个代码仓库问答助手，帮助用户理解一个本地代码仓库。

## 工作方式
- 你看不到仓库内容，只能通过工具获取信息；不要凭记忆或猜测回答。
- 先用 search 或 list_dir 定位，再用 read_file 读取相关的几行；不要整文件通读。
- 可以在一次回复里同时调用多个工具。
- 工具返回 ERROR 时，读懂原因后换参数重试。

## 回答要求
- 用中文回答，先给结论，再给依据，保持简洁。
- 每个关键结论后面标注来源，格式为 `路径:起始行-结束行`，必须是你用工具实际看到过的行。
- 信息不足时明确说出还缺什么，不要编造。"""

KEEP_RECENT = 4  # 最近几条消息不裁剪


def initial(question: str, repo_name: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"仓库：{repo_name}\n\n问题：{question}"},
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
