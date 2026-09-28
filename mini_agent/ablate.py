"""消融实验：一次去掉一个组件，在同一组问题上对比效果（规划第 5.6 节、第 7 节第 9 步）。

它不属于 agent 本身，只是用不同的 Settings 批量调用 agent.run，记录步数、token、费用和引用核对结果。
用法：python -m mini_agent.ablate --repo ../MultiAgentOS "问题1" "问题2" --repeat 2
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from mini_agent import agent
from mini_agent.agent import Settings
from mini_agent.llm import LLMClient, LLMError
from mini_agent.tools import ToolError, Tools

# 每个配置只比 full 少一个组件
CONFIGS = {
    "full": Settings(),
    "tree_only": Settings(overview="tree"),
    "no_overview": Settings(overview="none"),
    "no_dedup": Settings(dedup=False),
    "loose_prompt": Settings(strict_prompt=False),
}
# 只认 ASCII 路径，避免把紧挨着的中文算进路径里
CITATION = re.compile(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


def check_citations(answer: str, tools: Tools) -> tuple[int, int]:
    """返回 (引用数, 无法核实数)。只查文件在不在、行号在不在范围内，查不出“结论错误”。"""
    cited = set(CITATION.findall(answer))
    bad = 0
    for path, start, end in cited:
        try:
            target = tools._resolve(path)
            total = len(tools._read_text(target).splitlines()) if target.is_file() else 0
        except ToolError:
            total = 0
        if not 1 <= int(start) <= int(end or start) <= total:
            bad += 1
    return len(cited), bad


def run_one(config: str, question: str, repeat: int, root: Path, api_key: str, max_steps: int) -> dict:
    tools, llm = Tools(root), LLMClient(api_key)
    record = {"config": config, "question": question, "repeat": repeat}
    started = time.time()
    try:
        result = agent.run(question, llm, tools, root.name, max_steps=max_steps,
                           on_step=lambda step: None, settings=CONFIGS[config])
    except LLMError as error:
        return {**record, "error": str(error)}
    tool_steps = [s for s in result.steps if s.tool]
    cited, bad = check_citations(result.answer, tools)
    u = llm.usage
    return {**record, "stopped_by": result.stopped_by, "model_calls": u.calls,
            "tool_calls": len(tool_steps),
            "list_dir": sum(s.tool == "list_dir" for s in tool_steps),
            "repeated": sum(s.repeated for s in tool_steps),
            "input_tokens": u.cache_hit + u.cache_miss, "cache_hit": u.cache_hit,
            "output_tokens": u.output, "cost_usd": round(u.cost_usd, 6),
            "citations": cited, "bad_citations": bad,
            "seconds": round(time.time() - started, 1), "answer": result.answer}


def summarize(records: list[dict]) -> str:
    """按配置取平均，生成 Markdown 表格。"""
    rows = ["| 配置 | 次数 | 模型调用 | 工具调用 | list_dir | 重复 | 输入 token | 缓存命中率 | 费用 | 撞上限 | 引用（无法核实） |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for config in CONFIGS:
        rs = [r for r in records if r["config"] == config and "error" not in r]
        if not rs:
            continue

        def mean(key: str) -> float:
            return sum(r[key] for r in rs) / len(rs)

        hit_rate = sum(r["cache_hit"] for r in rs) / max(1, sum(r["input_tokens"] for r in rs))
        max_steps_hits = sum(r["stopped_by"] == "max_steps" for r in rs)
        rows.append(f"| {config} | {len(rs)} | {mean('model_calls'):.1f} | {mean('tool_calls'):.1f} | "
                    f"{mean('list_dir'):.1f} | {mean('repeated'):.1f} | {mean('input_tokens'):,.0f} | "
                    f"{hit_rate:.0%} | ${mean('cost_usd'):.4f} | {max_steps_hits} | "
                    f"{mean('citations'):.1f}（{mean('bad_citations'):.1f}） |")
    errors = sum("error" in r for r in records)
    return "\n".join(rows) + (f"\n\n失败 {errors} 次，详见 JSON。" if errors else "")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mini_agent.ablate", description="消融实验")
    parser.add_argument("questions", nargs="+")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--repeat", type=int, default=1, help="每个配置 × 问题跑几次（模型输出有随机性）")
    parser.add_argument("--workers", type=int, default=4, help="同时运行几个")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--configs", nargs="+", default=list(CONFIGS), choices=list(CONFIGS))
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    root = Path(args.repo).resolve()
    if not api_key or not root.is_dir():
        print("需要 DEEPSEEK_API_KEY，并且 --repo 必须是目录", file=sys.stderr)
        sys.exit(2)

    jobs = [(c, q, i) for i in range(args.repeat) for q in args.questions for c in args.configs]
    print(f"共 {len(jobs)} 次运行，预计费用约 ${len(jobs) * 0.002:.2f}–${len(jobs) * 0.005:.2f}")
    records = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:  # 各次运行互不相关，可以并行
        futures = [pool.submit(run_one, c, q, i, root, api_key, args.max_steps) for c, q, i in jobs]
        for n, future in enumerate(as_completed(futures), start=1):
            r = future.result()
            records.append(r)
            label = f"[{n}/{len(jobs)}] {r['config']:<12} q{args.questions.index(r['question']) + 1}"
            if "error" in r:
                print(f"{label} 失败：{r['error'][:100]}")
            else:
                print(f"{label} calls={r['model_calls']} cost=${r['cost_usd']:.4f} "
                      f"引用={r['citations']}（无法核实 {r['bad_citations']}）")

    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"ablation-{datetime.now():%Y%m%d-%H%M%S}.json"
    data = {"repo": str(root), "questions": args.questions, "records": records}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(summarize(records))
    print(f"\n明细：{path}")


if __name__ == "__main__":
    main()
