"""命令行入口：agent 和人打交道的那一段（对应 M1 的 CLI / UserInteraction）。

第 1 步的临时版本：不带工具问模型一句话，打印回答、用量，并检查 thinking 是否真的关闭。
第 4 步会换成完整的 agent 循环。
"""

from __future__ import annotations

import argparse
import os
import sys

from mini_agent.llm import LLMClient, LLMError


def run() -> None:
    parser = argparse.ArgumentParser(prog="python -m mini_agent")
    parser.add_argument("question")
    parser.add_argument("--model", default="deepseek-flash")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY，请先在终端运行：export DEEPSEEK_API_KEY=你的key", file=sys.stderr)
        sys.exit(2)

    llm = LLMClient(api_key, model=args.model)
    try:
        reply = llm.chat([{"role": "user", "content": args.question}])
    except LLMError as error:
        print(f"调用失败：{error}", file=sys.stderr)
        sys.exit(1)

    print(reply.text)
    print()
    print(f"finish_reason={reply.finish_reason}")
    print(f"reasoning_content 存在: {bool(reply.message.get('reasoning_content'))}")
    u = llm.usage
    print(f"usage: cache_hit={u.cache_hit} cache_miss={u.cache_miss} "
          f"output={u.output} calls={u.calls} cost≈${u.cost_usd:.6f}")
