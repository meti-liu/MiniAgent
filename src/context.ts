/**
 * 上下文：决定每次放进模型“眼里”的是什么（对应 M1 的 ContextEngine）。
 *
 * SYSTEM_PROMPT 一字不变，保证每次调用的开头相同、能命中缓存；会变的仓库名、仓库概览和问题放进第一条用户消息。
 * trim 在总长度超限时，把最早的工具结果换成占位符，控制上下文长度。
 */

import type { Message } from "./llm.ts";

export const BASE_PROMPT = `你是一个代码仓库问答助手，帮助用户理解一个本地代码仓库。

## 工作方式
- 你看不到仓库内容，只能通过工具获取信息；不要凭记忆或猜测回答。
- 第一条消息如果附有仓库概览，先用它判断该去哪里找，不必从根目录逐层 list_dir。
- 先用 search 或 list_dir 定位，再用 read_file 读取相关的几行；不要整文件通读。
- 可以在一次回复里同时调用多个工具。
- 工具返回 ERROR 时，读懂原因后换参数重试。

## 回答要求
- 用中文回答，先给结论，再给依据，保持简洁。
- 每个关键结论后面标注来源，格式为 \`路径:起始行-结束行\`，必须是你用工具实际看到过的行。
- 信息不足时明确说出还缺什么，不要编造。`;

// 第 5 步根据试跑加上的两条规则；消融实验可以关掉它们（Settings.strictPrompt = false）
export const STRICT_RULES = `
- 只对实际读到的内容下结论；如果某个文件只看过搜索命中的几行，不要推断它其余部分写了什么或没写什么。
- 直接输出最终回答，不要描述你的检查过程。`;

export const SYSTEM_PROMPT = BASE_PROMPT + STRICT_RULES;

const KEEP_RECENT = 4; // 最近几条消息不裁剪

export function initial(question: string, repoName: string, overview = "", strict = true): Message[] {
  // 顺序：仓库名 → 仓库概览 → 问题。同一仓库的概览相同，放在问题前面，换问题时这段也能命中缓存
  const parts = [`仓库：${repoName}`];
  if (overview) parts.push(`仓库概览：\n\n${overview}`);
  parts.push(`问题：${question}`);
  return [
    { role: "system", content: strict ? SYSTEM_PROMPT : BASE_PROMPT },
    { role: "user", content: parts.join("\n\n") },
  ];
}

/** 用字符数近似 token 数；assistant 消息的 tool_calls 也算进去。 */
function size(message: Message): number {
  let n = (message.content ?? "").length;
  if (message.tool_calls) n += JSON.stringify(message.tool_calls).length;
  return n;
}

/**
 * 超过 maxChars 时，从最早的 tool 结果开始换成占位符，直到不超限。
 *
 * 只替换 tool 消息的内容、不删除消息，这样 tool_calls 和 tool 结果依然成对。
 * system、用户问题（前两条）和最近 KEEP_RECENT 条消息不动。
 */
export function trim(messages: Message[], maxChars = 60_000): Message[] {
  let total = messages.reduce((sum, m) => sum + size(m), 0);
  if (total <= maxChars) return messages;
  const trimmed = [...messages]; // 不修改调用方传进来的数组
  for (let i = 2; i < trimmed.length - KEEP_RECENT; i++) {
    if (total <= maxChars) break;
    const message = trimmed[i]!;
    const content = message.content ?? "";
    if (message.role !== "tool" || content.startsWith("[已省略")) continue;
    const placeholder = `[已省略，共 ${content.length} 字符；需要时请重新调用工具]`;
    trimmed[i] = { ...message, content: placeholder };
    total -= content.length - placeholder.length;
  }
  return trimmed;
}
