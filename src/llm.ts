/**
 * 模型客户端：agent 里负责“问大脑”的那一段（对应 M1 的 Kernel 模型适配器）。
 *
 * 把 messages 和工具定义 POST 给 DeepSeek，把回复解析成 Reply，并累计 token 用量和费用。
 * 只用 Node 自带的 fetch，这样能直接看到 HTTP 请求和响应长什么样。
 */

// 美元 / 百万 token，非高峰价；高峰时段 ×2
const PRICE_CACHE_HIT = 0.003;
const PRICE_CACHE_MISS = 0.15;
const PRICE_OUTPUT = 0.6;
const PEAK_HOURS_UTC: ReadonlyArray<readonly [number, number]> = [[1, 4], [6, 10]]; // 周一至周五，[开始, 结束) 小时

const MAX_RETRIES = 2;
let retryWaitMs = 2000; // 测试里会改小

export function setRetryWaitMs(ms: number): void {
  retryWaitMs = ms;
}

/** 调用模型失败：HTTP 错误或网络错误。 */
export class LLMError extends Error {
  override name = "LLMError";
}

/** OpenAI 格式的一条消息。字段比较多且因角色而异，这里只约束最常用的几个。 */
export interface Message {
  role: "system" | "user" | "assistant" | "tool";
  content?: string | null;
  tool_calls?: RawToolCall[];
  tool_call_id?: string;
  [key: string]: unknown;
}

interface RawToolCall {
  id: string;
  type: string;
  function: { name: string; arguments: string };
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>; // 已从 JSON 字符串解析；解析失败时为 {_raw: 原文}
}

export interface Reply {
  message: Message; // 原样追加回 messages 的 assistant 消息
  text: string | null;
  toolCalls: ToolCall[];
  finishReason: string;
}

/** agent.ts 只依赖这一个方法（结构类型），测试里的 FakeLLM 实现同名方法即可替换。 */
export interface ChatModel {
  chat(messages: Message[], tools?: object[] | null, toolChoice?: string): Promise<Reply>;
}

export function isPeak(now: Date): boolean {
  const day = now.getUTCDay(); // 0 = 周日, 6 = 周六
  if (day === 0 || day === 6) return false;
  const hour = now.getUTCHours();
  return PEAK_HOURS_UTC.some(([start, end]) => start <= hour && hour < end);
}

export class Usage {
  cacheHit = 0;
  cacheMiss = 0;
  output = 0;
  calls = 0;
  costUsd = 0; // 每次调用后按当时是否高峰累加

  add(raw: Record<string, number | undefined>, now: Date): void {
    const hit = raw.prompt_cache_hit_tokens ?? 0;
    const miss = raw.prompt_cache_miss_tokens ?? 0;
    const out = raw.completion_tokens ?? 0;
    this.cacheHit += hit;
    this.cacheMiss += miss;
    this.output += out;
    this.calls += 1;
    let cost = (hit * PRICE_CACHE_HIT + miss * PRICE_CACHE_MISS + out * PRICE_OUTPUT) / 1_000_000;
    if (isPeak(now)) cost *= 2;
    this.costUsd += cost;
  }

  /** 和 Python 版 --json 输出的字段名保持一致。 */
  toJSON(): Record<string, number> {
    return {
      cache_hit: this.cacheHit,
      cache_miss: this.cacheMiss,
      output: this.output,
      calls: this.calls,
      cost_usd: this.costUsd,
    };
  }
}

/** 把 API 响应的 JSON 变成 Reply。 */
export function parseReply(data: any): Reply {
  const choice = data.choices[0];
  const message: Message = choice.message;
  const toolCalls: ToolCall[] = [];
  for (const call of message.tool_calls ?? []) {
    const raw = call.function.arguments;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = null;
    }
    const isObject = typeof parsed === "object" && parsed !== null && !Array.isArray(parsed);
    toolCalls.push({
      id: call.id,
      name: call.function.name,
      arguments: isObject ? (parsed as Record<string, unknown>) : { _raw: raw },
    });
  }
  return {
    message,
    text: message.content ?? null,
    toolCalls,
    finishReason: choice.finish_reason ?? "",
  };
}

export class LLMClient implements ChatModel {
  readonly usage = new Usage();
  private readonly apiKey: string;
  private readonly model: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;

  // 不用 `constructor(private readonly apiKey: string)` 这种参数属性：它不是“可擦除”语法，Node 不能直接运行
  constructor(apiKey: string, model = "deepseek-flash", baseUrl = "https://api.deepseek.com", timeoutMs = 60_000) {
    this.apiKey = apiKey;
    this.model = model;
    this.baseUrl = baseUrl;
    this.timeoutMs = timeoutMs;
  }

  async chat(messages: Message[], tools: object[] | null = null, toolChoice = "auto"): Promise<Reply> {
    const body: Record<string, unknown> = {
      model: this.model,
      messages,
      thinking: { type: "disabled" },
      max_tokens: 2000,
    };
    if (tools && tools.length > 0) {
      body.tools = tools;
      body.tool_choice = toolChoice;
    }
    const data = await this.post(body);
    this.usage.add(data.usage ?? {}, new Date());
    return parseReply(data);
  }

  private async post(body: Record<string, unknown>): Promise<any> {
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      let response: Response;
      try {
        response = await fetch(`${this.baseUrl}/chat/completions`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.apiKey}` },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(this.timeoutMs),
        });
      } catch (error) {
        // 断网、超时：不重试
        throw new LLMError(`network error: ${(error as Error).message}`, { cause: error });
      }
      if (response.ok) return await response.json();
      const text = await response.text(); // 错误正文里通常有具体原因
      const retryable = response.status === 429 || response.status >= 500;
      if (retryable && attempt < MAX_RETRIES) {
        await new Promise((resolve) => setTimeout(resolve, retryWaitMs));
        continue;
      }
      throw new LLMError(`HTTP ${response.status}: ${text}`);
    }
    throw new Error("unreachable");
  }
}
