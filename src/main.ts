/**
 * 命令行入口：agent 和人打交道的那一段（对应 M1 的 CLI / UserInteraction）。
 *
 * 解析参数、检查 key 和仓库路径，创建模型客户端和工具箱，运行 agent，
 * 最后打印回答和一行用量汇总。加 --json 时只输出一行 JSON，格式和 Python 版相同，给评测脚本读取。
 * 用法：node src/main.ts "问题" [--repo 路径] [--max-steps 10] [--model deepseek-flash] [--json]
 */

import { statSync } from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";
import * as agent from "./agent.ts";
import { LLMClient, LLMError } from "./llm.ts";
import { Tools } from "./tools.ts";

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      repo: { type: "string", default: "." },
      "max-steps": { type: "string", default: "10" },
      model: { type: "string", default: "deepseek-flash" },
      json: { type: "boolean", default: false },
    },
  });
  const question = positionals[0];
  if (!question) {
    console.error('用法：node src/main.ts "问题" [--repo 路径] [--max-steps 10] [--json]');
    process.exit(2);
  }

  const apiKey = process.env.DEEPSEEK_API_KEY;
  if (!apiKey) {
    console.error("缺少 DEEPSEEK_API_KEY，请先在终端运行：export DEEPSEEK_API_KEY=你的key");
    process.exit(2);
  }
  const root = path.resolve(values.repo);
  if (!isDir(root)) {
    console.error(`仓库路径不存在或不是目录：${values.repo}`);
    process.exit(2);
  }

  const llm = new LLMClient(apiKey, values.model);
  let result: agent.RunResult;
  try {
    result = await agent.run(question, llm, new Tools(root), path.basename(root), {
      maxSteps: Number(values["max-steps"]),
      onStep: values.json ? () => {} : agent.printStep,
    });
  } catch (error) {
    if (!(error instanceof LLMError)) throw error;
    console.error(`调用模型失败：${error.message}`);
    process.exit(1);
  }

  const u = llm.usage;
  if (values.json) {
    console.log(JSON.stringify({ answer: result.answer, stopped_by: result.stoppedBy, steps: result.steps, usage: u }));
    return;
  }
  console.log(`\n${result.answer}\n`);
  const note = result.stoppedBy === "max_steps" ? "（达到步数上限）" : "";
  console.log(
    `steps=${u.calls}${note}  input=${(u.cacheHit + u.cacheMiss).toLocaleString("en-US")} tokens ` +
      `(cache hit ${u.cacheHit.toLocaleString("en-US")})  output=${u.output.toLocaleString("en-US")} tokens  ` +
      `cost≈$${u.costUsd.toFixed(4)}`,
  );
}

function isDir(p: string): boolean {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

await main();
