/** 测试 LLM 解析、消融和评测脚本里不联网的部分。 */

import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { test } from "node:test";
import { checkCitations, summarize as summarizeAblation } from "../src/ablate.ts";
import { grade, QUESTIONS_FILE, runOne, splitCommand, summarize } from "../src/evaluate.ts";
import type { Bank, EvalRecord, Question } from "../src/evaluate.ts";
import { isPeak, LLMClient, LLMError, parseReply, setRetryWaitMs, Usage } from "../src/llm.ts";
import { Tools } from "../src/tools.ts";
import { tempDir, write } from "./helpers.ts";

const QUESTION: Question = { id: "q", repo: "demo", type: "t", question: "问",
  must: [["is_peak"], ["Usage\\.add", "def add"]], must_not: ["sqlite"] };

test("parseReply keeps bad JSON arguments as _raw", () => {
  const reply = parseReply({ choices: [{ finish_reason: "tool_calls", message: { role: "assistant", content: null,
    tool_calls: [{ id: "a", type: "function", function: { name: "search", arguments: '{"pattern":"x"}' } },
      { id: "b", type: "function", function: { name: "read_file", arguments: "not json" } }] } }] });
  assert.deepEqual(reply.toolCalls[0]!.arguments, { pattern: "x" });
  assert.deepEqual(reply.toolCalls[1]!.arguments, { _raw: "not json" });
});

test("usage doubles the cost in peak hours only", () => {
  const u = new Usage();
  u.add({ prompt_cache_hit_tokens: 1_000_000 }, new Date("2026-09-27T02:00:00Z")); // 周日
  assert.equal(u.costUsd, 0.003);
  assert.ok(isPeak(new Date("2026-09-28T02:00:00Z")));
  assert.ok(!isPeak(new Date("2026-09-28T05:00:00Z")) && !isPeak(new Date("2026-09-28T10:00:00Z")));
});

test("client retries 5xx, sends the right body and wraps errors", async () => {
  setRetryWaitMs(0);
  const bodies: any[] = [];
  let status = 503;
  const server = createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => (raw += chunk));
    req.on("end", () => {
      bodies.push(JSON.parse(raw));
      if (bodies.length < 3 || status === 401) {
        res.writeHead(status).end(status === 401 ? '{"error":"bad key"}' : "busy");
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({
        choices: [{ finish_reason: "stop", message: { role: "assistant", content: "ok" } }],
        usage: { prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 10, completion_tokens: 2 } }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  try {
    const client = new LLMClient("k", "deepseek-flash", url);
    const reply = await client.chat([{ role: "user", content: "hi" }]);
    assert.equal(reply.text, "ok");
    assert.equal(bodies.length, 3); // 两次 503 后第三次成功
    assert.ok(!("tools" in bodies[0]) && bodies[0].thinking.type === "disabled");
    assert.equal(client.usage.calls, 1);

    await client.chat([], [{ type: "function" }], "none");
    assert.equal(bodies.at(-1).tool_choice, "none");

    status = 401;
    await assert.rejects(client.chat([]), (e) => e instanceof LLMError && e.message.startsWith("HTTP 401"));
  } finally {
    server.close();
  }
  await assert.rejects(new LLMClient("k", "m", "http://127.0.0.1:1").chat([]), /network error/);
});

test("checkCitations counts missing files and out-of-range lines", () => {
  const dir = tempDir();
  write(path.join(dir, "a.py"), "1\n2\n3\n");
  const answer = "见 `a.py:1-2`、`a.py:5`、`missing.py:1`，以及中文紧挨着a.py:2";
  assert.deepEqual(checkCitations(answer, new Tools(dir)), [4, 2]);
  // 简写按同一回答里的完整路径补全；对不上唯一路径的简写仍算无法核实
  mkdirSync(path.join(dir, "src"));
  write(path.join(dir, "src", "b.py"), "1\n2\n");
  assert.deepEqual(checkCitations("见 src/b.py:1，后面又说 b.py:2 和 c.py:1", new Tools(dir)), [3, 1]);
});

test("ablation summary averages per config", () => {
  const base = { question: "q", repeat: 0, stopped_by: "answer", model_calls: 3, tool_calls: 4, list_dir: 1, repeated: 0,
    input_tokens: 1000, cache_hit: 500, cost_usd: 0.001, citations: 5, bad_citations: 0 };
  const table = summarizeAblation([{ ...base, config: "full" }, { ...base, config: "full", model_calls: 5 },
    { config: "no_dedup", question: "q", repeat: 0, error: "HTTP 500" }]);
  assert.ok(table.includes("| full | 2 | 4.0 |") && table.includes("50%"));
  assert.ok(!table.includes("no_dedup") && table.includes("失败 1 次"));
});

test("grade needs every fact group and no forbidden words", () => {
  assert.ok(grade("在 Usage.add 里调用 IS_PEAK", QUESTION).passed);
  const result = grade("在 is_peak 里，用 sqlite 保存", QUESTION);
  assert.equal(result.facts, 1);
  assert.deepEqual(result.missed, ["Usage\\.add"]);
  assert.deepEqual(result.must_not_hits, ["sqlite"]);
  assert.ok(!result.passed);
});

test("runOne reads JSON from any command", async () => {
  const dir = tempDir();
  write(path.join(dir, "llm.py"), "a\nb\n");
  const out = { answer: "is_peak 在 Usage.add（llm.py:1-2）", stopped_by: "answer",
    steps: [{ tool: "search" }, { tool: null }], usage: { calls: 2, cache_hit: 10, cache_miss: 90, cost_usd: 0.001 } };
  // 假装是另一种语言写的 agent：只要按约定打印一行 JSON
  const fakeAgent = [process.execPath, "-e", `console.log(${JSON.stringify(JSON.stringify(out))})`, "--"];
  const bank: Bank = { repos: { demo: { path: dir } }, questions: [] };
  const r = await runOne(fakeAgent, QUESTION, 0, bank, 30_000);
  assert.ok(r.passed);
  assert.equal(r.model_calls, 2);
  assert.equal(r.tool_calls, 1);
  assert.deepEqual([r.citations, r.bad_citations], [1, 0]);

  const broken = await runOne([process.execPath, "-e", "process.exit(3)", "--"], QUESTION, 0, bank, 30_000);
  assert.ok(broken.error?.startsWith("退出码 3"));
});

test("evaluation summary reports the pass rate", () => {
  const ok: EvalRecord = { id: "q", repeat: 0, passed: true, facts: 2, facts_total: 2, missed: [], must_not_hits: [],
    model_calls: 3, cost_usd: 0.002, stopped_by: "answer", citations: 4, bad_citations: 1 };
  const bad: EvalRecord = { ...ok, passed: false, facts: 1, missed: ["is_peak"] };
  const table = summarize([ok, bad, { id: "q", repeat: 0, error: "x" }], [QUESTION]);
  assert.ok(table.includes("| q | t | 1/2 | 3/4 | 3.0 | $0.0020 | is_peak |"));
  assert.ok(table.includes("通过 1/2") && table.includes("失败 1 次"));
});

test("splitCommand keeps quoted paths together", () => {
  assert.deepEqual(splitCommand(`"/a b/node" src/main.ts 'x y'`), ["/a b/node", "src/main.ts", "x y"]);
});

test("question bank is well formed", () => {
  const bank: Bank = JSON.parse(readFileSync(QUESTIONS_FILE, "utf8"));
  const ids = bank.questions.map((q) => q.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const q of bank.questions) {
    assert.ok(q.repo in bank.repos && q.must.length > 0);
    for (const pattern of [...q.must.flat(), ...(q.must_not ?? [])]) new RegExp(pattern, "i");
  }
});
