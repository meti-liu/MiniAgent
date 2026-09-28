"""命令行入口：agent 和人打交道的那一段（对应 M1 的 CLI / UserInteraction）。

解析参数、检查 key 和仓库路径，创建模型客户端和工具箱，运行 agent，
最后打印回答和一行用量汇总（模型调用次数、输入 token 及缓存命中、输出 token、估算费用）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mini_agent import agent
from mini_agent.llm import LLMClient, LLMError
from mini_agent.tools import Tools


def run() -> None:
    parser = argparse.ArgumentParser(prog="python -m mini_agent", description="对本地代码仓库提问")
    parser.add_argument("question")
    parser.add_argument("--repo", default=".", help="仓库路径，默认当前目录")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--model", default="deepseek-flash")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY，请先在终端运行：export DEEPSEEK_API_KEY=你的key", file=sys.stderr)
        sys.exit(2)
    root = Path(args.repo).resolve()
    if not root.is_dir():
        print(f"仓库路径不存在或不是目录：{args.repo}", file=sys.stderr)
        sys.exit(2)

    llm = LLMClient(api_key, model=args.model)
    try:
        result = agent.run(args.question, llm, Tools(root), root.name, max_steps=args.max_steps)
    except LLMError as error:
        print(f"调用模型失败：{error}", file=sys.stderr)
        sys.exit(1)

    print()
    print(result.answer)
    print()
    u = llm.usage
    note = "（达到步数上限）" if result.stopped_by == "max_steps" else ""
    print(f"steps={u.calls}{note}  input={u.cache_hit + u.cache_miss:,} tokens "
          f"(cache hit {u.cache_hit:,})  output={u.output:,} tokens  cost≈${u.cost_usd:.4f}")
