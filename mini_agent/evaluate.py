"""评测：用固定题库给 agent 打分（规划第 5.7 节、第 7 节第 10 步）。

它通过命令行调用 agent（--cmd，默认就是本仓库的 Python 实现），读取 `--json` 输出，
所以同一套题库、同一份打分规则可以评测 Python 和 TypeScript 两个实现。它不属于 agent 本身。
用法：python -m mini_agent.evaluate [--cmd "node ../mini-agent-ts/src/main.ts"] [--repeat 3] [--label ts]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from mini_agent.ablate import RUNS_DIR, check_citations
from mini_agent.tools import Tools

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_FILE = ROOT / "evals" / "questions.json"


def grade(answer: str, question: dict) -> dict:
    """must 里每一组是一个事实，组内任一正则命中即可；must_not 命中任何一个就算错。"""
    missed = [group for group in question["must"]
              if not any(re.search(p, answer, re.IGNORECASE) for p in group)]
    wrong = [p for p in question.get("must_not", []) if re.search(p, answer, re.IGNORECASE)]
    return {"facts": len(question["must"]) - len(missed), "facts_total": len(question["must"]),
            "missed": [group[0] for group in missed], "must_not_hits": wrong,
            "passed": not missed and not wrong}


def repo_path(data: dict, name: str) -> Path:
    return (ROOT / data["repos"][name]["path"]).resolve()


def check_pins(data: dict) -> None:
    """题库里固定了 commit 的仓库，如果当前 HEAD 不同就提醒（答案里的事实可能已经变了）。"""
    for name, repo in data["repos"].items():
        if "commit" not in repo:
            continue
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}  # 只读，不在别人的仓库里留下锁文件
        head = subprocess.run(["git", "-C", str(repo_path(data, name)), "rev-parse", "HEAD"],
                              capture_output=True, text=True, env=env).stdout.strip()
        if head != repo["commit"]:
            print(f"提醒：{name} 的 HEAD 是 {head[:7] or '未知'}，题库按 {repo['commit'][:7]} 编写，"
                  f"部分事实可能对不上", file=sys.stderr)


def run_one(cmd: list[str], question: dict, repeat: int, data: dict, timeout: int) -> dict:
    repo = repo_path(data, question["repo"])
    record = {"id": question["id"], "repeat": repeat}
    started = time.time()
    try:
        proc = subprocess.run(cmd + [question["question"], "--repo", str(repo), "--json"],
                              capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {**record, "error": f"超过 {timeout} 秒"}
    lines = proc.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1])
    except (json.JSONDecodeError, IndexError):
        return {**record, "error": f"退出码 {proc.returncode}：{proc.stderr.strip()[-200:]}"}
    answer, usage, steps = out["answer"], out["usage"], out["steps"]
    cited, bad = check_citations(answer, Tools(repo))
    return {**record, **grade(answer, question), "stopped_by": out["stopped_by"],
            "model_calls": usage["calls"], "tool_calls": sum(1 for s in steps if s["tool"]),
            "input_tokens": usage["cache_hit"] + usage["cache_miss"], "cache_hit": usage["cache_hit"],
            "cost_usd": usage["cost_usd"], "citations": cited, "bad_citations": bad,
            "seconds": round(time.time() - started, 1), "answer": answer}


def summarize(records: list[dict], questions: list[dict]) -> str:
    ok = [r for r in records if "error" not in r]
    rows = ["| 题目 | 类型 | 通过 | 事实命中 | 模型调用 | 费用 | 漏掉的事实 |", "|---|---|---|---|---|---|---|"]
    for q in questions:
        rs = [r for r in ok if r["id"] == q["id"]]
        if not rs:
            continue
        missed = sorted({m for r in rs for m in r["missed"]} | {f"不应出现 {m}" for r in rs for m in r["must_not_hits"]})
        rows.append(f"| {q['id']} | {q['type']} | {sum(r['passed'] for r in rs)}/{len(rs)} | "
                    f"{sum(r['facts'] for r in rs)}/{sum(r['facts_total'] for r in rs)} | "
                    f"{sum(r['model_calls'] for r in rs) / len(rs):.1f} | "
                    f"${sum(r['cost_usd'] for r in rs) / len(rs):.4f} | {', '.join(missed) or '-'} |")
    if ok:
        facts = sum(r["facts"] for r in ok) / sum(r["facts_total"] for r in ok)
        rows.append("")
        rows.append(f"总计：通过 {sum(r['passed'] for r in ok)}/{len(ok)}，事实命中率 {facts:.0%}，"
                    f"撞上步数上限 {sum(r['stopped_by'] == 'max_steps' for r in ok)} 次，"
                    f"平均费用 ${sum(r['cost_usd'] for r in ok) / len(ok):.4f}，"
                    f"引用 {sum(r['citations'] for r in ok)} 处（无法核实 {sum(r['bad_citations'] for r in ok)}）")
    if len(ok) < len(records):
        rows.append(f"失败 {len(records) - len(ok)} 次，详见 JSON。")
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mini_agent.evaluate", description="用固定题库评测 agent")
    parser.add_argument("--cmd", default=f"{shlex.quote(sys.executable)} -m mini_agent",
                        help="调用 agent 的命令，会在后面追加：问题 --repo 路径 --json")
    parser.add_argument("--label", default="python", help="结果文件名里的标签，例如 python / ts")
    parser.add_argument("--ids", nargs="+", help="只跑这些题目")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=300, help="单次运行的超时秒数")
    args = parser.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("需要先 export DEEPSEEK_API_KEY", file=sys.stderr)
        sys.exit(2)
    data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    questions = [q for q in data["questions"] if not args.ids or q["id"] in args.ids]
    check_pins(data)
    cmd = shlex.split(args.cmd)
    jobs = [(q, i) for i in range(args.repeat) for q in questions]
    print(f"{args.label}：{len(questions)} 题 × {args.repeat} 次 = {len(jobs)} 次运行")

    records = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_one, cmd, q, i, data, args.timeout) for q, i in jobs]
        for n, future in enumerate(as_completed(futures), start=1):
            r = future.result()
            records.append(r)
            if "error" in r:
                print(f"[{n}/{len(jobs)}] {r['id']:<14} 失败：{r['error']}")
            else:
                mark = "通过" if r["passed"] else f"未通过（漏：{', '.join(r['missed'] + r['must_not_hits'])}）"
                print(f"[{n}/{len(jobs)}] {r['id']:<14} {mark}  calls={r['model_calls']} cost=${r['cost_usd']:.4f}")

    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"eval-{args.label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"label": args.label, "cmd": args.cmd, "records": records},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(summarize(records, questions))
    print(f"\n明细：{path}")


if __name__ == "__main__":
    main()
