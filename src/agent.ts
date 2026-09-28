/**
 * 循环：agent 的“心跳”（对应 M1 的 Workflow）。
 *
 * 模型决定下一步 → 程序执行工具 → 结果喂回模型，直到模型不再调用工具（给出回答）或达到 maxSteps。
 * “一步”指一次模型调用；同一次调用里的多个工具调用共用一个步号，逐条打印。
 * 和之前完全相同的工具调用不再执行（见 runTool），Settings 集中放消融实验的开关。
 */

import * as context from "./context.ts";
import type { ChatModel, Message, ToolCall } from "./llm.ts";
import type { Tools } from "./tools.ts";

export const FINAL_PROMPT = "已达到步数上限。请根据已获得的信息直接回答，不要再调用工具。";
export const REPEAT_NOTE = "NOTE: 这个调用和前面某次完全相同，结果就在上文，没有重新执行。请换参数，或根据已有信息回答。";

/** 消融实验的开关；默认值就是正常运行的配置。 */
export interface Settings {
  readonly overview: "full" | "tree" | "none";
  readonly dedup: boolean; // 跳过和之前完全相同的工具调用
  readonly strictPrompt: boolean; // 系统提示词是否包含 STRICT_RULES
}

export const DEFAULT_SETTINGS: Settings = Object.freeze({ overview: "full", dedup: true, strictPrompt: true });

export interface Step {
  number: number;
  tool: string | null; // null 表示这一步是最终回答
  arguments: Record<string, unknown> | null;
  result_chars: number; // 字段名和 Python 版 --json 输出一致
  repeated: boolean; // 重复调用，没有真正执行
}

export interface RunResult {
  answer: string;
  steps: Step[];
  stoppedBy: "answer" | "max_steps";
}

export interface RunOptions {
  maxSteps?: number;
  onStep?: (step: Step) => void;
  settings?: Settings;
}

export function printStep(step: Step): void {
  if (step.tool === null) {
    console.log(`[step ${step.number}] answer`);
  } else {
    const note = step.repeated ? "（重复，未执行）" : `-> ${step.result_chars} chars`;
    console.log(`[step ${step.number}] ${step.tool} ${JSON.stringify(step.arguments)} ${note}`);
  }
}

export async function run(
  question: string, llm: ChatModel, tools: Tools, repoName: string, options: RunOptions = {},
): Promise<RunResult> {
  const { maxSteps = 10, onStep = printStep, settings = DEFAULT_SETTINGS } = options;
  const overview = tools.overview(settings.overview); // 开头就给出仓库概览
  let messages = context.initial(question, repoName, overview, settings.strictPrompt);
  const specs = tools.specs(); // 整个运行过程中工具集不变，前缀才能命中缓存
  const steps: Step[] = [];
  const seen = new Map<string, number>(); // 调用签名 -> 那次结果在 messages 里的下标

  for (let number = 1; number <= maxSteps; number++) {
    const reply = await llm.chat(messages, specs);
    messages.push(reply.message); // 让模型下一轮“记得”自己说过什么、调过什么

    if (reply.toolCalls.length === 0) {
      // 没有工具调用 = 模型认为可以回答了
      return finish(reply.text, number, steps, "answer", onStep);
    }

    for (const call of reply.toolCalls) {
      const [result, repeated] = runTool(call, tools, messages, seen, settings.dedup);
      messages.push({ role: "tool", tool_call_id: call.id, content: result });
      const step: Step = { number, tool: call.name, arguments: call.arguments, result_chars: result.length, repeated };
      steps.push(step);
      onStep(step);
    }
    messages = context.trim(messages); // 太长时把最早的工具结果换成占位符
  }

  // 步数用完：工具集照传，但禁止调用工具，逼模型基于已有信息作答
  messages.push({ role: "user", content: FINAL_PROMPT });
  const reply = await llm.chat(messages, specs, "none");
  return finish(reply.text, maxSteps + 1, steps, "max_steps", onStep);
}

/** 执行一个工具调用；和之前完全相同、且那次结果还在上下文里时，不再执行。 */
export function runTool(
  call: ToolCall, tools: Tools, messages: Message[], seen: Map<string, number>, dedup: boolean,
): [string, boolean] {
  const key = `${call.name} ${stableJson(call.arguments)}`;
  const earlier = seen.get(key);
  if (dedup && earlier !== undefined && !(messages[earlier]?.content ?? "").startsWith("[已省略")) {
    return [REPEAT_NOTE, true];
  }
  seen.set(key, messages.length); // 结果马上会 push 到这个位置；trim 只替换不删除，下标不会变
  return [tools.run(call.name, call.arguments), false]; // 出错也只是返回 "ERROR: ..." 字符串
}

/** 键按字母排序的 JSON，相当于 Python 的 json.dumps(sort_keys=True)。 */
function stableJson(value: Record<string, unknown>): string {
  return JSON.stringify(Object.fromEntries(Object.entries(value).sort(([a], [b]) => (a < b ? -1 : 1))));
}

function finish(
  text: string | null, number: number, steps: Step[], stoppedBy: RunResult["stoppedBy"], onStep: (s: Step) => void,
): RunResult {
  const answer = text || "(模型没有给出回答)";
  const step: Step = { number, tool: null, arguments: null, result_chars: answer.length, repeated: false };
  steps.push(step);
  onStep(step);
  return { answer, steps, stoppedBy };
}
