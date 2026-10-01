"""命令行入口：agent 和人打交道的那一段（对应 M1 的 CLI / UserInteraction）。

解析参数、检查 key 和仓库路径，创建模型客户端和工具箱，运行 agent，
最后打印回答和一行用量汇总（模型调用次数、输入 token 及缓存命中、输出 token、估算费用）。
加 --json 时只输出一行 JSON，给评测脚本（evaluate.py）读取，Python 和 TypeScript 两个实现的格式相同。
每次运行（包括中途出错的）都把轨迹存到 --trace-dir，用 python -m mini_agent.trace 回放；--no-trace 关闭。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mini_agent import agent, trace
from mini_agent.llm import LLMClient, LLMError
from mini_agent.tools import Tools


def run() -> None:
    parser = argparse.ArgumentParser(prog="python -m mini_agent", description="对本地代码仓库提问")
    parser.add_argument("question")
    parser.add_argument("--repo", default=".", help="仓库路径，默认当前目录")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--json", action="store_true", help="只输出一行 JSON，供评测脚本读取")
    parser.add_argument("--trace-dir", default="runs/traces", help="运行轨迹的保存目录")
    parser.add_argument("--compaction", choices=["trim", "clear", "summary"], default="trim",
                        help="上下文超限时怎么压缩（规划 12.5）")
    parser.add_argument("--max-context", type=int, default=60_000, help="上下文超过这么多字符就压缩")
    parser.add_argument("--no-trace", action="store_true", help="不保存运行轨迹")
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
    tools = Tools(root)
    settings = agent.Settings(compaction=args.compaction, max_context_chars=args.max_context)
    transcript: list[dict] = []  # 自己持有这个列表，模型调用出错时也能存下已经发生的部分
    try:
        on_step = (lambda step: None) if args.json else agent.print_step
        result = agent.run(args.question, llm, tools, root.name, max_steps=args.max_steps,
                           on_step=on_step, settings=settings, transcript=transcript)
    except LLMError as error:
        record = trace.build(args.question, root.name, llm, tools, settings, args.max_steps,
                             transcript, error=str(error))
        path = _save(record, args)
        print(f"调用模型失败：{error}" + (f"（轨迹：{path}）" if path else ""), file=sys.stderr)
        sys.exit(1)

    record = trace.build(args.question, root.name, llm, tools, settings, args.max_steps, transcript, result)
    path = _save(record, args)
    u = llm.usage
    if args.json:
        print(json.dumps({
            "answer": result.answer,
            "stopped_by": result.stopped_by,
            "steps": [vars(step) for step in result.steps],
            "usage": vars(u),
            "prompt_hash": record["prompt_hash"],
            "tools_hash": record["tools_hash"],
            "trace": path,
        }, ensure_ascii=False))
        return
    print()
    print(result.answer)
    print()
    note = "（达到步数上限）" if result.stopped_by == "max_steps" else ""
    print(f"steps={u.calls}{note}  input={u.cache_hit + u.cache_miss:,} tokens "
          f"(cache hit {u.cache_hit:,})  output={u.output:,} tokens  cost≈${u.cost_usd:.4f}")
    if path:
        print(f"trace: {path}")


def _save(record: dict, args) -> str | None:
    if args.no_trace:
        return None
    return str(trace.save(record, Path(args.trace_dir)))
