/** 测试共用：按剧本回复的假模型，以及在临时目录里建小仓库。 */

import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { ChatModel, Message, Reply } from "../src/llm.ts";

/** 和 LLMClient 有同名的 chat 方法（结构类型），按预设顺序返回回复，并记下每次收到了什么。 */
export class FakeLLM implements ChatModel {
  readonly calls: Array<{ messages: Message[]; tools: object[] | null; toolChoice: string }> = [];
  private readonly replies: Reply[];

  constructor(replies: Reply[]) {
    this.replies = [...replies];
  }

  async chat(messages: Message[], tools: object[] | null = null, toolChoice = "auto"): Promise<Reply> {
    this.calls.push({ messages: [...messages], tools, toolChoice });
    const reply = this.replies.shift();
    if (!reply) throw new Error("FakeLLM 的剧本用完了");
    return reply;
  }
}

/** calls 是 [id, 工具名, 参数] 元组；构造一个“模型要调用这些工具”的回复。 */
export function toolReply(...calls: Array<[string, string, Record<string, unknown>]>): Reply {
  const raw = calls.map(([id, name, args]) => ({ id, type: "function", function: { name, arguments: JSON.stringify(args) } }));
  return {
    message: { role: "assistant", content: null, tool_calls: raw },
    text: null,
    toolCalls: calls.map(([id, name, args]) => ({ id, name, arguments: args })),
    finishReason: "tool_calls",
  };
}

export function answerReply(text: string): Reply {
  return { message: { role: "assistant", content: text }, text, toolCalls: [], finishReason: "stop" };
}

export function tempDir(): string {
  return mkdtempSync(path.join(tmpdir(), "mini-agent-"));
}

export function write(file: string, text: string): void {
  writeFileSync(file, text, "utf8");
}
