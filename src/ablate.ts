/**
 * 消融实验：一次去掉一个组件，在同一组问题上对比效果（规划第 5.6 节）。
 *
 * 它不属于 agent 本身，只是用不同的 Settings 批量调用 agent.run，记录步数、token、费用和引用核对结果。
 * 用法：node src/ablate.ts --repo ../MultiAgentOS "问题1" "问题2" --repeat 2
 */

import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import * as agent from "./agent.ts";
import type { Settings } from "./agent.ts";
import { LLMClient, LLMError } from "./llm.ts";
import { splitLines, Tools } from "./tools.ts";

// 每个配置只比 full 少一个组件
export const CONFIGS: Record<string, Settings> = {
  full: agent.DEFAULT_SETTINGS,
  tree_only: { ...agent.DEFAULT_SETTINGS, overview: "tree" },
  no_overview: { ...agent.DEFAULT_SETTINGS, overview: "none" },
  no_dedup: { ...agent.DEFAULT_SETTINGS, dedup: false },
  loose_prompt: { ...agent.DEFAULT_SETTINGS, strictPrompt: false },
};
// 只认 ASCII 路径，避免把紧挨着的中文算进路径里
const CITATION = /([A-Za-z0-9_./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?/g;
export const ROOT = fileURLToPath(new URL("..", import.meta.url));
export const RUNS_DIR = path.join(ROOT, "runs");

/** 返回 [引用数, 无法核实数]。只查文件在不在、行号在不在范围内，查不出“结论错误”。 */
export function checkCitations(answer: string, tools: Tools): [number, number] {
  const cited = new Set([...answer.matchAll(CITATION)].map((m) => `${m[1]}:${m[2]}:${m[3] ?? ""}`));
  let bad = 0;
  for (const key of cited) {
    const [file, start, end] = key.split(":") as [string, string, string];
    let total = 0;
    try {
      total = splitLines(tools.readText(tools.resolve(file))).length;
    } catch {
      total = 0; // 越界、被跳过、不存在、不是文本
    }
    const first = Number(start);
    const last = Number(end || start);
    if (!(1 <= first && first <= last && last <= total)) bad += 1;
  }
  return [cited.size, bad];
}

/** 最多同时跑 workers 个任务，每完成一个就回调一次。 */
export async function runPool<T, R>(
  items: T[], workers: number, task: (item: T) => Promise<R>, onDone: (result: R, done: number) => void,
): Promise<R[]> {
  const results: R[] = [];
  let next = 0;
  const worker = async (): Promise<void> => {
    while (next < items.length) {
      const result = await task(items[next++]!);
      results.push(result);
      onDone(result, results.length);
    }
  };
  await Promise.all(Array.from({ length: Math.min(workers, items.length) }, worker));
  return results;
}

export interface AblationRecord {
  config: string;
  question: string;
  repeat: number;
  error?: string;
  stopped_by?: string;
  model_calls?: number;
  tool_calls?: number;
  list_dir?: number;
  repeated?: number;
  input_tokens?: number;
  cache_hit?: number;
  output_tokens?: number;
  cost_usd?: number;
  citations?: number;
  bad_citations?: number;
  seconds?: number;
  answer?: string;
}

async function runOne(config: string, question: string, repeat: number, root: string, apiKey: string,
  maxSteps: number): Promise<AblationRecord> {
  const tools = new Tools(root);
  const llm = new LLMClient(apiKey);
  const record = { config, question, repeat };
  const started = Date.now();
  let result: agent.RunResult;
  try {
    result = await agent.run(question, llm, tools, path.basename(root),
      { maxSteps, onStep: () => {}, settings: CONFIGS[config]! });
  } catch (error) {
    if (error instanceof LLMError) return { ...record, error: error.message };
    throw error;
  }
  const toolSteps = result.steps.filter((s) => s.tool !== null);
  const [citations, badCitations] = checkCitations(result.answer, tools);
  const u = llm.usage;
  return {
    ...record, stopped_by: result.stoppedBy, model_calls: u.calls, tool_calls: toolSteps.length,
    list_dir: toolSteps.filter((s) => s.tool === "list_dir").length,
    repeated: toolSteps.filter((s) => s.repeated).length,
    input_tokens: u.cacheHit + u.cacheMiss, cache_hit: u.cacheHit, output_tokens: u.output,
    cost_usd: Number(u.costUsd.toFixed(6)), citations, bad_citations: badCitations,
    seconds: Number(((Date.now() - started) / 1000).toFixed(1)), answer: result.answer,
  };
}

const n0 = (x: number): string => x.toLocaleString("en-US", { maximumFractionDigits: 0 });

/** 按配置取平均，生成 Markdown 表格。 */
export function summarize(records: AblationRecord[]): string {
  const rows = [
    "| 配置 | 次数 | 模型调用 | 工具调用 | list_dir | 重复 | 输入 token | 缓存命中率 | 费用 | 撞上限 | 引用（无法核实） |",
    "|---|---|---|---|---|---|---|---|---|---|---|",
  ];
  for (const config of Object.keys(CONFIGS)) {
    const rs = records.filter((r) => r.config === config && r.error === undefined);
    if (rs.length === 0) continue;
    const mean = (key: keyof AblationRecord): number => rs.reduce((sum, r) => sum + Number(r[key]), 0) / rs.length;
    const hitRate = rs.reduce((s, r) => s + r.cache_hit!, 0) / Math.max(1, rs.reduce((s, r) => s + r.input_tokens!, 0));
    const maxHits = rs.filter((r) => r.stopped_by === "max_steps").length;
    rows.push(`| ${config} | ${rs.length} | ${mean("model_calls").toFixed(1)} | ${mean("tool_calls").toFixed(1)} | ` +
      `${mean("list_dir").toFixed(1)} | ${mean("repeated").toFixed(1)} | ${n0(mean("input_tokens"))} | ` +
      `${Math.round(hitRate * 100)}% | $${mean("cost_usd").toFixed(4)} | ${maxHits} | ` +
      `${mean("citations").toFixed(1)}（${mean("bad_citations").toFixed(1)}） |`);
  }
  const errors = records.filter((r) => r.error !== undefined).length;
  return rows.join("\n") + (errors ? `\n\n失败 ${errors} 次，详见 JSON。` : "");
}

export function timestamp(): string {
  const d = new Date();
  const p = (x: number): string => String(x).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

async function main(): Promise<void> {
  const { values, positionals: questions } = parseArgs({
    allowPositionals: true,
    options: {
      repo: { type: "string", default: "." },
      repeat: { type: "string", default: "1" },
      workers: { type: "string", default: "4" },
      "max-steps": { type: "string", default: "10" },
    },
  });
  const apiKey = process.env.DEEPSEEK_API_KEY;
  const root = path.resolve(values.repo);
  if (!apiKey || questions.length === 0) {
    console.error('需要 DEEPSEEK_API_KEY 和至少一个问题：node src/ablate.ts --repo 路径 "问题"');
    process.exit(2);
  }
  const jobs: Array<[string, string, number]> = [];
  for (let i = 0; i < Number(values.repeat); i++)
    for (const q of questions) for (const c of Object.keys(CONFIGS)) jobs.push([c, q, i]);
  console.log(`共 ${jobs.length} 次运行，预计费用约 $${(jobs.length * 0.002).toFixed(2)}–$${(jobs.length * 0.005).toFixed(2)}`);

  const records = await runPool(jobs, Number(values.workers),
    ([c, q, i]) => runOne(c, q, i, root, apiKey, Number(values["max-steps"])),
    (r, n) => {
      const label = `[${n}/${jobs.length}] ${r.config.padEnd(12)} q${questions.indexOf(r.question) + 1}`;
      console.log(r.error !== undefined ? `${label} 失败：${r.error.slice(0, 100)}`
        : `${label} calls=${r.model_calls} cost=$${r.cost_usd!.toFixed(4)} 引用=${r.citations}（无法核实 ${r.bad_citations}）`);
    });

  mkdirSync(RUNS_DIR, { recursive: true });
  const file = path.join(RUNS_DIR, `ablation-ts-${timestamp()}.json`);
  writeFileSync(file, JSON.stringify({ repo: root, questions, records }, null, 2));
  console.log(`\n${summarize(records)}\n\n明细：${file}`);
}

// 只在直接运行这个文件时执行 main（被测试 import 时不执行）；路径含中文时 URL 会被编码，所以比较文件路径
if (fileURLToPath(import.meta.url) === path.resolve(process.argv[1] ?? "")) await main();
