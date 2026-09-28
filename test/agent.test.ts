/** 测试 agent 循环和上下文：用按剧本回复的 FakeLLM 代替真实模型。不联网。 */

import assert from "node:assert/strict";
import path from "node:path";
import { beforeEach, test } from "node:test";
import { DEFAULT_SETTINGS, FINAL_PROMPT, REPEAT_NOTE, run, runTool } from "../src/agent.ts";
import * as context from "../src/context.ts";
import type { Message } from "../src/llm.ts";
import { Tools } from "../src/tools.ts";
import { answerReply, FakeLLM, tempDir, toolReply, write } from "./helpers.ts";

let tools: Tools;
const quiet = { onStep: () => {} };

beforeEach(() => {
  const dir = tempDir();
  write(path.join(dir, "app.py"), "def hello():\n    return 'hi'\n");
  tools = new Tools(dir);
});

test("search then read then answer", async () => {
  const llm = new FakeLLM([
    toolReply(["c1", "search", { pattern: "hello" }]),
    toolReply(["c2", "read_file", { path: "app.py" }]),
    answerReply("hello 返回 'hi'（app.py:1-2）"),
  ]);
  const printed: unknown[] = [];
  const result = await run("hello 做什么？", llm, tools, "demo", { onStep: (s) => printed.push(s) });

  assert.equal(result.answer, "hello 返回 'hi'（app.py:1-2）");
  assert.equal(result.stoppedBy, "answer");
  assert.deepEqual(result.steps.map((s) => s.tool), ["search", "read_file", null]);
  assert.deepEqual(printed, result.steps);
  // 第 3 次调用时，模型能看到前两次的工具结果，且 tool_call_id 对得上
  const toolMessages = llm.calls[2]!.messages.filter((m) => m.role === "tool");
  assert.deepEqual(toolMessages.map((m) => m.tool_call_id), ["c1", "c2"]);
  assert.ok(toolMessages[0]!.content!.includes("app.py:1: def hello():"));
  assert.ok(toolMessages[1]!.content!.includes("2:     return 'hi'"));
  // 第一条用户消息里有仓库概览，而且在问题前面
  const firstUser = llm.calls[0]!.messages[1]!.content!;
  assert.ok(firstUser.indexOf("app.py") < firstUser.indexOf("问题："));
});

test("stops after max steps with tool_choice none", async () => {
  const llm = new FakeLLM([0, 1, 2].map((i) => toolReply([`c${i}`, "list_dir", { path: `p${i}` }])).concat(answerReply("尽力回答")));
  const result = await run("q", llm, tools, "demo", { maxSteps: 3, ...quiet });

  assert.equal(result.stoppedBy, "max_steps");
  assert.equal(result.answer, "尽力回答");
  assert.equal(llm.calls.length, 4);
  const last = llm.calls.at(-1)!;
  assert.equal(last.toolChoice, "none");
  assert.deepEqual(last.tools, llm.calls[0]!.tools); // 工具集没变，前缀不变
  assert.deepEqual(last.messages.at(-1), { role: "user", content: FINAL_PROMPT });
});

test("errors are fed back and calls in one reply share a step", async () => {
  const llm = new FakeLLM([toolReply(["c1", "delete_file", {}], ["c2", "list_dir", {}]), answerReply("done")]);
  const result = await run("q", llm, tools, "demo", quiet);

  assert.deepEqual(result.steps.map((s) => [s.number, s.tool]), [[1, "delete_file"], [1, "list_dir"], [2, null]]);
  const toolMessages = llm.calls[1]!.messages.filter((m) => m.role === "tool");
  assert.ok(toolMessages[0]!.content!.startsWith("ERROR: unknown tool"));
  assert.equal(toolMessages[1]!.content, "app.py");
});

test("identical calls are not run twice unless dedup is off", async () => {
  const replies = () => [toolReply(["c1", "list_dir", {}]), toolReply(["c2", "list_dir", { path: "." }], ["c3", "list_dir", {}]), answerReply("done")];
  const result = await run("q", new FakeLLM(replies()), tools, "demo", quiet);
  assert.deepEqual(result.steps.map((s) => s.repeated), [false, false, true, false]);

  const llm = new FakeLLM(replies());
  await run("q", llm, tools, "demo", { ...quiet, settings: { ...DEFAULT_SETTINGS, dedup: false } });
  const contents = llm.calls.at(-1)!.messages.filter((m) => m.role === "tool").map((m) => m.content);
  assert.ok(!contents.includes(REPEAT_NOTE));
});

test("a repeat runs again after trim replaced the earlier result", () => {
  const call = { id: "c1", name: "list_dir", arguments: {} };
  const seen = new Map<string, number>();
  const messages: Message[] = [{ role: "tool", content: runTool(call, tools, [], seen, true)[0] }];
  assert.deepEqual(runTool(call, tools, messages, seen, true), [REPEAT_NOTE, true]);
  messages[0]!.content = "[已省略，共 6 字符]"; // 模拟被 trim 省略
  assert.deepEqual(runTool(call, tools, messages, seen, true), ["app.py", false]);
});

test("loose prompt drops the strict rules", async () => {
  const strict = new FakeLLM([answerReply("a")]);
  const loose = new FakeLLM([answerReply("a")]);
  await run("q", strict, tools, "demo", quiet);
  await run("q", loose, tools, "demo", { ...quiet, settings: { ...DEFAULT_SETTINGS, strictPrompt: false } });
  assert.ok(strict.calls[0]!.messages[0]!.content!.includes(context.STRICT_RULES));
  assert.ok(!loose.calls[0]!.messages[0]!.content!.includes(context.STRICT_RULES));
});

test("trim replaces only the oldest tool results", () => {
  const messages = context.initial("q", "demo");
  for (let i = 0; i < 6; i++) {
    messages.push(toolReply([`c${i}`, "search", { pattern: "x" }]).message);
    messages.push({ role: "tool", tool_call_id: `c${i}`, content: "y".repeat(1000) });
  }
  const trimmed = context.trim(messages, 4000);

  assert.equal(trimmed.length, messages.length); // 只替换内容，不删消息
  assert.deepEqual(trimmed.slice(0, 2), messages.slice(0, 2)); // system 和问题不动
  assert.deepEqual(trimmed.slice(-4), messages.slice(-4)); // 最近 4 条不动
  assert.ok(trimmed[3]!.content!.startsWith("[已省略，共 1000 字符"));
  assert.equal(trimmed[3]!.tool_call_id, "c0"); // 配对信息保留
  assert.equal(messages[3]!.content, "y".repeat(1000)); // 原数组没被修改
  assert.equal(context.trim(messages, 10 ** 6), messages); // 不超限时原样返回
});
