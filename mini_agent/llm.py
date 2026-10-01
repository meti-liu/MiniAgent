"""模型客户端：agent 里负责“问大脑”的那一段（对应 M1 的 Kernel 模型适配器）。

把 messages 和工具定义 POST 给 DeepSeek，把回复解析成 Reply，并累计 token 用量和费用。
每次调用的耗时、用量和费用另记一条到 call_log，写进运行轨迹（trace.py）。
只用标准库 urllib，这样能直接看到 HTTP 请求和响应长什么样。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

# 美元 / 百万 token，非高峰价；高峰时段 ×2
PRICE_CACHE_HIT = 0.003
PRICE_CACHE_MISS = 0.15
PRICE_OUTPUT = 0.60
PEAK_HOURS_UTC = [(1, 4), (6, 10)]  # 周一至周五，[开始, 结束) 小时

MAX_RETRIES = 2
RETRY_WAIT_SECONDS = 2


class LLMError(Exception):
    """调用模型失败：HTTP 错误或网络错误。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict  # 已从 JSON 字符串解析；解析失败时为 {"_raw": 原文}


@dataclass
class Reply:
    message: dict  # 原样追加回 messages 的 assistant 消息
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str


def is_peak(now: datetime) -> bool:
    """now 必须是 UTC 时间。"""
    if now.weekday() >= 5:  # 周六、周日
        return False
    return any(start <= now.hour < end for start, end in PEAK_HOURS_UTC)


@dataclass
class Usage:
    cache_hit: int = 0
    cache_miss: int = 0
    output: int = 0
    calls: int = 0
    cost_usd: float = 0.0  # 每次调用后按当时是否高峰累加

    def add(self, raw: dict, now: datetime) -> float:
        """累加一次调用的用量，返回这次的费用。"""
        hit = raw.get("prompt_cache_hit_tokens", 0)
        miss = raw.get("prompt_cache_miss_tokens", 0)
        out = raw.get("completion_tokens", 0)
        self.cache_hit += hit
        self.cache_miss += miss
        self.output += out
        self.calls += 1
        cost = (hit * PRICE_CACHE_HIT + miss * PRICE_CACHE_MISS + out * PRICE_OUTPUT) / 1_000_000
        if is_peak(now):
            cost *= 2
        self.cost_usd += cost
        return cost


def parse_reply(data: dict) -> Reply:
    """把 API 响应的 JSON 变成 Reply。"""
    choice = data["choices"][0]
    message = choice["message"]
    tool_calls = []
    for call in message.get("tool_calls") or []:
        raw = call["function"]["arguments"]
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            arguments = {"_raw": raw}
        tool_calls.append(ToolCall(call["id"], call["function"]["name"], arguments))
    return Reply(
        message=message,
        text=message.get("content"),
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason", ""),
    )


class LLMClient:
    def __init__(self, api_key: str, model: str = "deepseek-flash",
                 base_url: str = "https://api.deepseek.com", timeout: float = 60):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.usage = Usage()
        self.call_log: list[dict] = []  # 每次调用一条：耗时、用量、费用

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto") -> Reply:
        body = {
            "model": self.model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "max_tokens": 2000,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        started = time.monotonic()
        data = self._post(body)
        raw = data.get("usage", {})
        cost = self.usage.add(raw, datetime.now(timezone.utc))
        reply = parse_reply(data)
        self.call_log.append({
            "latency_ms": round((time.monotonic() - started) * 1000),
            "cache_hit": raw.get("prompt_cache_hit_tokens", 0),
            "cache_miss": raw.get("prompt_cache_miss_tokens", 0),
            "output": raw.get("completion_tokens", 0),
            "cost_usd": round(cost, 8),
            "tool_choice": tool_choice if tools else None,
            "finish_reason": reply.finish_reason,
        })
        return reply

    def _post(self, body: dict) -> dict:
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        for attempt in range(MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:  # 服务器回了错误状态码
                text = error.read().decode("utf-8", "replace")
                retryable = error.code == 429 or error.code >= 500
                if retryable and attempt < MAX_RETRIES:
                    time.sleep(RETRY_WAIT_SECONDS)
                    continue
                raise LLMError(f"HTTP {error.code}: {text}") from error
            except (urllib.error.URLError, TimeoutError) as error:  # 断网、超时：不重试
                raise LLMError(f"network error: {error}") from error
        raise AssertionError("unreachable")
