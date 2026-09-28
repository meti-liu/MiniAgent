/**
 * 评测：用固定题库给 agent 打分（规划第 5.7 节）。
 *
 * 通过命令行调用 agent（--cmd，默认就是本仓库的 TypeScript 实现），读取 --json 输出，
 * 所以同一套题库、同一份打分规则可以评测 TypeScript 和 Python 两个实现。它不属于 agent 本身。
 * 用法：node src/evaluate.ts [--cmd "python3 -m mini_agent"] [--repeat 3] [--label ts]
 */

import { execFile, execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { checkCitations, ROOT, runPool, RUNS_DIR, timestamp } from "./ablate.ts";
import { Tools } from "./tools.ts";

export const QUESTIONS_FILE = path.join(ROOT, "evals", "questions.json");

export interface Question {
  id: string;
  repo: string;
  type: string;
  question: string;
  must: string[][];
  must_not?: string[];
}

export interface Bank {
  repos: Record<string, { path: string; commit?: string }>;
  questions: Question[];
}

export interface Grade {
  facts: number;
  facts_total: number;
  missed: string[];
  must_not_hits: string[];
  passed: boolean;
}

export interface EvalRecord extends Partial<Grade> {
  id: string;
  repeat: number;
  error?: string;
  stopped_by?: string;
  model_calls?: number;
  tool_calls?: number;
  input_tokens?: number;
  cache_hit?: number;
  cost_usd?: number;
  citations?: number;
  bad_citations?: number;
  seconds?: number;
  answer?: string;
}

/** must 里每一组是一个事实，组内任一正则命中即可；must_not 命中任何一个就算错。都不区分大小写。 */
export function grade(answer: string, question: Question): Grade {
  const hit = (pattern: string): boolean => new RegExp(pattern, "i").test(answer);
  const missed = question.must.filter((group) => !group.some(hit));
  const wrong = (question.must_not ?? []).filter(hit);
  return {
    facts: question.must.length - missed.length, facts_total: question.must.length,
    missed: missed.map((group) => group[0]!), must_not_hits: wrong, passed: missed.length === 0 && wrong.length === 0,
  };
}

export function repoPath(bank: Bank, name: string): string {
  return path.resolve(ROOT, bank.repos[name]!.path);
}

/** 题库里固定了 commit 的仓库，如果当前 HEAD 不同就提醒（答案里的事实可能已经变了）。 */
function checkPins(bank: Bank): void {
  for (const [name, repo] of Object.entries(bank.repos)) {
    if (!repo.commit) continue;
    let head = "";
    try {
      head = execFileSync("git", ["-C", repoPath(bank, name), "rev-parse", "HEAD"],
        { encoding: "utf8", env: { ...process.env, GIT_OPTIONAL_LOCKS: "0" } }).trim(); // 只读，不留锁文件
    } catch {
      head = "";
    }
    if (head !== repo.commit) {
      console.error(`提醒：${name} 的 HEAD 是 ${head.slice(0, 7) || "未知"}，题库按 ${repo.commit.slice(0, 7)} 编写，部分事实可能对不上`);
    }
  }
}

/** 把 --cmd 字符串拆成参数，支持用引号包住带空格的路径。 */
export function splitCommand(cmd: string): string[] {
  return [...cmd.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map((m) => m[1] ?? m[2] ?? m[3]!);
}

function runCommand(cmd: string[], timeoutMs: number): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    execFile(cmd[0]!, cmd.slice(1), { cwd: ROOT, timeout: timeoutMs, maxBuffer: 16 * 1024 * 1024 },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === "number" ? error.code : -1) : 0;
        resolve({ code, stdout, stderr: error?.killed ? `超过 ${timeoutMs / 1000} 秒` : stderr });
      });
  });
}

export async function runOne(cmd: string[], question: Question, repeat: number, bank: Bank,
  timeoutMs: number): Promise<EvalRecord> {
  const repo = repoPath(bank, question.repo);
  const record = { id: question.id, repeat };
  const started = Date.now();
  const proc = await runCommand([...cmd, question.question, "--repo", repo, "--json"], timeoutMs);
  let out: any;
  try {
    out = JSON.parse(proc.stdout.trim().split("\n").at(-1) ?? "");
  } catch {
    return { ...record, error: `退出码 ${proc.code}：${proc.stderr.trim().slice(-200)}` };
  }
  const [citations, badCitations] = checkCitations(out.answer, new Tools(repo));
  return {
    ...record, ...grade(out.answer, question), stopped_by: out.stopped_by, model_calls: out.usage.calls,
    tool_calls: out.steps.filter((s: { tool: string | null }) => s.tool !== null).length,
    input_tokens: out.usage.cache_hit + out.usage.cache_miss, cache_hit: out.usage.cache_hit,
    cost_usd: out.usage.cost_usd, citations, bad_citations: badCitations,
    seconds: Number(((Date.now() - started) / 1000).toFixed(1)), answer: out.answer,
  };
}

export function summarize(records: EvalRecord[], questions: Question[]): string {
  const ok = records.filter((r) => r.error === undefined);
  const sum = (rs: EvalRecord[], key: keyof EvalRecord): number => rs.reduce((s, r) => s + Number(r[key] ?? 0), 0);
  const rows = ["| 题目 | 类型 | 通过 | 事实命中 | 模型调用 | 费用 | 漏掉的事实 |", "|---|---|---|---|---|---|---|"];
  for (const q of questions) {
    const rs = ok.filter((r) => r.id === q.id);
    if (rs.length === 0) continue;
    const missed = [...new Set([...rs.flatMap((r) => r.missed ?? []), ...rs.flatMap((r) => (r.must_not_hits ?? []).map((m) => `不应出现 ${m}`))])].sort();
    rows.push(`| ${q.id} | ${q.type} | ${rs.filter((r) => r.passed).length}/${rs.length} | ` +
      `${sum(rs, "facts")}/${sum(rs, "facts_total")} | ${(sum(rs, "model_calls") / rs.length).toFixed(1)} | ` +
      `$${(sum(rs, "cost_usd") / rs.length).toFixed(4)} | ${missed.join(", ") || "-"} |`);
  }
  if (ok.length) {
    rows.push("", `总计：通过 ${ok.filter((r) => r.passed).length}/${ok.length}，` +
      `事实命中率 ${Math.round((sum(ok, "facts") / sum(ok, "facts_total")) * 100)}%，` +
      `撞上步数上限 ${ok.filter((r) => r.stopped_by === "max_steps").length} 次，` +
      `平均费用 $${(sum(ok, "cost_usd") / ok.length).toFixed(4)}，` +
      `引用 ${sum(ok, "citations")} 处（无法核实 ${sum(ok, "bad_citations")}）`);
  }
  if (ok.length < records.length) rows.push(`失败 ${records.length - ok.length} 次，详见 JSON。`);
  return rows.join("\n");
}

async function main(): Promise<void> {
  const { values } = parseArgs({
    options: {
      cmd: { type: "string", default: `"${process.execPath}" "${path.join(ROOT, "src", "main.ts")}"` },
      label: { type: "string", default: "ts" },
      ids: { type: "string", multiple: true },
      repeat: { type: "string", default: "3" },
      workers: { type: "string", default: "4" },
      timeout: { type: "string", default: "300" },
    },
  });
  if (!process.env.DEEPSEEK_API_KEY) {
    console.error("需要先 export DEEPSEEK_API_KEY");
    process.exit(2);
  }
  const bank: Bank = JSON.parse(readFileSync(QUESTIONS_FILE, "utf8"));
  const questions = bank.questions.filter((q) => !values.ids || values.ids.includes(q.id));
  checkPins(bank);
  const cmd = splitCommand(values.cmd);
  const jobs: Array<[Question, number]> = [];
  for (let i = 0; i < Number(values.repeat); i++) for (const q of questions) jobs.push([q, i]);
  console.log(`${values.label}：${questions.length} 题 × ${values.repeat} 次 = ${jobs.length} 次运行`);

  const records = await runPool(jobs, Number(values.workers),
    ([q, i]) => runOne(cmd, q, i, bank, Number(values.timeout) * 1000),
    (r, n) => {
      const label = `[${n}/${jobs.length}] ${r.id.padEnd(14)}`;
      if (r.error !== undefined) console.log(`${label} 失败：${r.error}`);
      else {
        const mark = r.passed ? "通过" : `未通过（漏：${[...(r.missed ?? []), ...(r.must_not_hits ?? [])].join(", ")}）`;
        console.log(`${label} ${mark}  calls=${r.model_calls} cost=$${r.cost_usd!.toFixed(4)}`);
      }
    });

  mkdirSync(RUNS_DIR, { recursive: true });
  const file = path.join(RUNS_DIR, `eval-${values.label}-${timestamp()}.json`);
  writeFileSync(file, JSON.stringify({ label: values.label, cmd: values.cmd, records }, null, 2));
  console.log(`\n${summarize(records, questions)}\n\n明细：${file}`);
}

// 只在直接运行这个文件时执行 main（被测试 import 时不执行）；路径含中文时 URL 会被编码，所以比较文件路径
if (fileURLToPath(import.meta.url) === path.resolve(process.argv[1] ?? "")) await main();
