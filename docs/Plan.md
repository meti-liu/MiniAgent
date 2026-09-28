# mini-agent 实现规划（M0.5 最小仓库问答 Agent）

> 2026-09-28。本文是 mini-agent 仓库的规划文档（`docs/Plan.md`），新对话直接按它实现。
> 目的：让 meti 亲手看懂一个 agent 是怎么跑起来的。它是学习样机，不是 M1 交付物。

## 1. 目标

在命令行输入一个关于某个代码仓库的问题，agent 自己决定要列哪些目录、搜什么、读哪几行，
最后给出回答，并附上它引用的文件和行号。

```text
$ python -m mini_agent "ContextEngine 的 ORIENT 做了什么？" --repo ../MultiAgentOS
[step 1] search {"pattern": "ORIENT"}                 -> 12 hits
[step 2] read_file {"path": "...", "start": 40, "end": 90}
[step 3] answer
ORIENT 在每次运行开始时……（引用 docs/…/ContextEngineSpec.md:88-94）

steps=3  input=5,210 tokens (cache hit 3,072)  output=412 tokens  cost≈$0.0009
```

### 1.1 验收标准

1. 对自己的仓库问 3 个不同类型的问题（解释已知函数、找未知实现、跨两个文件解释），都能得到带文件和行号的回答。
2. 每一步都打印出来：模型调用了哪个工具、参数是什么、结果多长。
3. 最多 10 步必然停止；工具不能读到仓库目录以外的任何文件，也不能写文件。
4. `pytest` 通过：不联网，用假模型把循环跑一遍。
5. 除测试外，代码总量约 500 行（软上限，为了可读性可以适当超出），每个文件都能一次读完。

### 1.2 不做

不做 Schema 框架、错误码体系、协议注册表、fake/contract 测试体系、多 agent、持久化、断点续跑、
写文件或执行命令、Web 界面、向量检索。遇到“以后可能需要”的东西，一律写进第 9 节，不写进代码。

## 2. 仓库与目录

- 独立的本地 git 仓库 `mini-agent`，与团队的 MultiAgentOS 平级（`Agent开发/mini-agent`），不嵌套、不共享历史。
- 两个仓库目标不同：MultiAgentOS 追求严谨（review、兼容性、提交规范）；mini-agent 追求理解和学习，可以随时推倒重来。
- 本仓库只保留两条规矩：commit 信息写清楚改了什么、为什么改；用假模型的循环测试始终保持通过。
- 本文就是 `docs/Plan.md`，规划有变化时先改本文，再改代码。

```text
.
├── README.md             # 怎么安装、怎么运行，10 行以内
├── pyproject.toml        # 只声明 Python 版本和 pytest
├── .gitignore            # .env、__pycache__、.venv
├── .env.example          # DEEPSEEK_API_KEY=
├── docs/Plan.md          # 本文
├── docs/Runs.md          # 真实模型试跑记录（第 7 节第 5 步）
├── mini_agent/
│   ├── __main__.py       # 让 python -m mini_agent 可用，只调用 main.run()
│   ├── llm.py
│   ├── tools.py
│   ├── context.py
│   ├── agent.py
│   ├── main.py
│   └── ablate.py         # 消融实验脚本（第 7 节第 9 步），不属于 agent 本身
└── tests/
    ├── test_tools.py
    └── test_agent.py
```

## 3. 技术选择

| 项目 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.10+ | meti 最熟；类型标注、`dataclasses` 都够用 |
| 运行依赖 | 只用标准库（`urllib.request`、`json`、`pathlib`、`re`、`dataclasses`） | 直接看到 HTTP 请求和响应长什么样，没有 SDK 隐藏细节 |
| 开发依赖 | `pytest` | 只有这一个 |
| 模型 | DeepSeek `deepseek-flash`，OpenAI 兼容接口，`thinking` 关闭 | 便宜；关闭 thinking 可以省 token、降延迟，也不用回传 `reasoning_content` |
| 配置 | 环境变量 `DEEPSEEK_API_KEY`（可用 `.env` 手动 export） | 不引入 dotenv；`.env` 不进仓库 |
| 格式检查 | 可选 `ruff format` | 不作为硬性要求 |

## 4. 整体流程

```text
main.py      解析参数，创建 LLMClient 和 Tools，调用 agent.run(question)
  │
agent.py     messages = context.initial(question)
  │          for step in 1..max_steps:
  │              reply = llm.chat(messages, tool_specs)        ← 调模型
  │              messages.append(reply.message)
  │              if 没有 tool_calls: return 回答               ← 模型说完了
  │              for call in reply.tool_calls:
  │                  result = tools.run(call.name, call.args)  ← 执行工具
  │                  messages.append(tool 结果消息)
  │              messages = context.trim(messages)             ← 控制长度
  │          超过步数：让模型基于已有信息作答（tools 照传，tool_choice="none"）
  │
llm.py       POST https://api.deepseek.com/chat/completions，累计 usage 和费用
tools.py     list_dir / search / read_file，全部限制在 --repo 目录内
context.py   系统提示词、初始消息、历史裁剪
```

这就是一个 agent 的全部骨架：**“模型决定下一步 → 程序执行 → 结果喂回模型”的循环**。
M1 里的 Workflow、Kernel、ContextEngine、AgentToolPool，都是把这个循环里的某一段拆出来、加上约束。

## 5. 各文件设计

行数是上限预算，不是目标。

### 5.1 `llm.py`（约 100 行）— 对应 M1 的 Kernel 模型适配器

```python
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict            # 已从 JSON 字符串解析；解析失败时为 {"_raw": 原文}

@dataclass
class Reply:
    message: dict              # 原样追加回 messages 的 assistant 消息
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str

@dataclass
class Usage:
    cache_hit: int = 0
    cache_miss: int = 0
    output: int = 0
    calls: int = 0
    cost_usd: float = 0.0      # 每次调用后按当时是否高峰累加，事后无法再按总量算

class LLMClient:
    def __init__(self, api_key: str, model: str = "deepseek-flash",
                 base_url: str = "https://api.deepseek.com", timeout: float = 60): ...
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto") -> Reply: ...
    usage: Usage
```

要点：

- 请求体：`model`、`messages`、`tools`、`tool_choice`（默认 `"auto"`）、`thinking: {"type": "disabled"}`、`max_tokens: 2000`。
- 从响应的 `usage.prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`completion_tokens` 累计用量。
- 费用按非高峰价估算（美元 / 百万 token：缓存命中 0.003、未命中 0.15、输出 0.60）。
  UTC 周一至周五 01:00–04:00、06:00–10:00 为高峰，价格 ×2；每次调用结束时按当时时刻算出本次费用，累加到 `usage.cost_usd`，不处理节假日。
- HTTP 错误时抛出带状态码和响应正文的 `LLMError`；对 429 和 5xx 最多重试 2 次，每次等待 2 秒。
- 网络错误（超时、断网等 `URLError`）也包装成 `LLMError`，不重试。
- `LLMClient` 只需要一个 `chat` 方法，测试用的 `FakeLLM` 实现同名方法即可替换（鸭子类型，不需要抽象基类）。

### 5.2 `tools.py`（约 130 行）— 对应 M1 的 AgentToolPool + 只读 Executor

三个工具，都返回**字符串**（直接作为 tool 消息内容），出错时返回以 `ERROR:` 开头的字符串而不是抛异常，让模型自己看到并调整：

| 工具 | 参数 | 返回 |
|---|---|---|
| `list_dir` | `path`（默认 `.`） | 目录下的条目，目录名以 `/` 结尾，最多 200 条 |
| `search` | `pattern`（正则）、`path`（默认 `.`） | `相对路径:行号: 行内容`，最多 50 条，超出时注明“还有 N 条” |
| `read_file` | `path`、`start`（默认 1）、`end`（默认 start+199） | 带行号的内容，一次最多 200 行 |

```python
class Tools:
    def __init__(self, root: Path): ...
    def specs(self) -> list[dict]: ...          # OpenAI tools 格式的 JSON Schema
    def run(self, name: str, arguments: dict) -> str: ...
```

安全规则（这是 M1 里 Kernel 准入和 Executor 的最小版本）：

1. 所有路径先 `(root / path).resolve()`，结果必须在 `root.resolve()` 之内，否则返回 `ERROR: outside repository`。这一条同时挡住 `..`、绝对路径和指向外部的符号链接。
2. 跳过 `.git`、`node_modules`、`.venv`、`__pycache__`、`.pytest_cache`、`dist` 和 `.env*`（按小写比较）。
3. 单个文件超过 1 MB 或无法按 UTF-8 解码时不读取，返回说明。
4. 每个工具输出最多 12,000 个字符，超出截断并注明。
5. 没有任何写文件、删文件或执行命令的代码。

另有一个**不给模型调用**的方法 `overview(mode)`，对应 M1 ContextEngine 的 ORIENT，运行开始时放进第一条用户消息：
- `"tree"`：深度不超过 3 层的目录树（目录以 `/` 结尾，每个目录最多 20 项，超出写 `…(+N)`，最多 4,000 字符）。
- `"full"`（默认）：目录树，加上根目录下存在的 `AGENTS.md`（没有则 `CLAUDE.md`）前 40 行、`README.md` 前 60 行、
  清单文件（`pyproject.toml`、`package.json`、`go.mod`、`Cargo.toml`）前 40 行；每段最多 3,000 字符。
- `"none"`：空字符串，只用于消融实验。
遵守和工具相同的安全规则。M1 ORIENT 里的符号大纲和测试布局暂不做。
加它的原因见 `docs/Runs.md` 第 3 轮：没有概览时，模型问“入口在哪”只能逐层 `list_dir`，撞上步数上限。

### 5.3 `context.py`（约 60 行）— 对应 M1 的 ContextEngine

```python
SYSTEM_PROMPT: str
def initial(question: str, repo_name: str, overview: str = "") -> list[dict]: ...
def trim(messages: list[dict], max_chars: int = 60_000) -> list[dict]: ...
```

- 第一条用户消息的顺序是“仓库名 → 目录概览 → 问题”：同一个仓库的概览相同，放在问题前面，换问题时这段前缀仍能命中缓存。
- 系统提示词写清楚：只能用工具获取信息；先搜索再读；回答必须引用 `路径:起始行-结束行`；信息不足时明确说出来。
  提示词保持固定不变，这样每次调用的开头相同，可以命中 DeepSeek 的缓存（这就是 M1 里“稳定前缀”的由来）。
  仓库名这类会变的信息不写进系统提示词，而是和问题一起放进第一条用户消息，这样换仓库、换问题时前缀依然相同。
- `trim`：总字符数超过上限时，从最早的 tool 结果开始，把内容替换成 `[已省略，共 N 字符]`。
  system 消息、用户问题和最近 4 条消息不动。用字符数近似 token 数，不引入分词器。
- 注意：assistant 消息里的 `tool_calls` 和对应的 tool 消息必须成对保留，只能替换 tool 消息的内容，不能删除消息。

### 5.4 `agent.py`（约 80 行）— 对应 M1 的 Workflow

```python
@dataclass
class Step:
    number: int
    tool: str | None
    arguments: dict | None
    result_chars: int

@dataclass
class RunResult:
    answer: str
    steps: list[Step]
    stopped_by: str            # "answer" | "max_steps"

def run(question: str, llm, tools: Tools, repo_name: str,
        max_steps: int = 10, on_step=print) -> RunResult: ...
```

- 循环见第 4 节。模型一次返回多个 tool call 时依次全部执行。
- “步”按模型调用计数（`max_steps` 限制的是模型调用次数）；打印按工具调用逐条打，同一次模型调用里的多个工具调用共用一个步号。
- 重复调用：同一次运行里，工具名和参数都和之前某次完全相同、而且那次结果还没被 `trim` 省略时，不再执行，
  直接回一条 `NOTE:` 提示“结果就在上文”，这一步照样计数、打印时标注“重复”。结果已被省略时照常执行。
  原因见 `docs/Runs.md` 第 4 轮：同一个空目录被 `list_dir` 了 3 次。
- `Settings`（冻结的 dataclass）集中放消融实验的开关：`overview`（full/tree/none）、`dedup`、`strict_prompt`
  （是否加第 5 步那两条“只对读到的内容下结论、不描述检查过程”的规则）。默认值就是正常配置。
- 未知工具名或参数不是合法 JSON：把 `ERROR:` 结果回给模型，算一步，不中断。
- 达到 `max_steps` 后，追加一条用户消息“请根据已获得的信息直接回答”，再调用一次模型，这次 `tools` 照传但 `tool_choice="none"`（不改工具集，前缀不变，缓存仍能命中），结果作为最终回答，`stopped_by = "max_steps"`。
- `on_step` 回调负责打印，便于测试时替换成收集列表。

### 5.5 `main.py`（约 50 行）— 对应 M1 的 CLI / UserInteraction

```text
python -m mini_agent "问题" [--repo 路径，默认当前目录] [--max-steps 10] [--model deepseek-flash]
```

- 读取 `DEEPSEEK_API_KEY`，缺失时打印如何设置并以退出码 2 结束。
- 打印每一步、最终回答，以及最后一行用量汇总（步数、输入 token 及缓存命中、输出 token、估算费用）。

### 5.6 `ablate.py` — 消融实验（不属于 agent）

```text
python -m mini_agent.ablate --repo 路径 "问题1" "问题2" ... [--repeat 2] [--workers 4]
```

- 配置：`full`（全部开启）、`tree_only`、`no_overview`、`no_dedup`、`loose_prompt`，每次只去掉一个组件。
- 每个“配置 × 问题 × 重复”跑一次 `agent.run`，记录模型调用次数、工具调用次数（其中 list_dir、重复调用）、
  token、缓存命中、费用、是否撞上 max_steps，以及回答里 `路径:行号` 引用的程序化核对结果（文件是否存在、行号是否在范围内）。
- 明细写到 `runs/ablation-时间.json`（`runs/` 不进仓库），终端打印按配置汇总的表格；分析写进 `docs/Runs.md`。
- 程序化核对只能发现“引用不存在”，发现不了“结论错误”（见 `docs/Runs.md` 第 1 轮），所以回答质量仍要人工抽查。

## 6. 测试

只测不联网的部分：

| 文件 | 用例 |
|---|---|
| `tests/test_tools.py` | 在 `tmp_path` 里建一个小仓库：正常列目录、搜索、读指定行；`../` 和绝对路径被拒绝；指向外部的符号链接被拒绝；`.git` 被跳过；超长输出被截断 |
| `tests/test_agent.py` | `FakeLLM` 按预设顺序返回“调用 search → 调用 read_file → 回答”，断言工具被执行、tool 消息被追加、最终答案正确；再测一个一直调用工具的假模型，断言在 `max_steps` 后停止，并且最后一次调用的 `tool_choice` 是 `"none"` |

`FakeLLM` 就写在 `test_agent.py` 里，大约 20 行。

真实模型的效果不写成自动化测试，而是在第 7 节第 5 步手动跑，把问题、步数、费用和回答质量记到 `docs/Runs.md`。

## 7. 实现步骤

每一步结束时都能运行，并单独 commit（summary 用 `feat: ...` 这类前缀，正文用 `- ` 写要点和原因）。

| 步 | 内容 | 完成标志 |
|---|---|---|
| 0 | `git init`，提交 `docs/Plan.md`（本文）、`pyproject.toml`、`.gitignore`、`.env.example`、`README.md` | `git log` 只有 1 个 commit |
| 1 | `llm.py` + 临时 `main.py`：不带工具问一句话，打印回答和用量 | 真实调用成功，看到 usage 字段 |
| 2 | `tools.py` + `test_tools.py` | `pytest` 通过 |
| 3 | `agent.py` + `test_agent.py`（假模型） | `pytest` 通过 |
| 4 | `context.py`，`main.py` 接上完整循环和用量汇总 | 对 mini-agent 自己问一个问题并得到带行号的回答 |
| 5 | 用第 1.1 节的 3 类问题试跑（可用 `--repo ../MultiAgentOS` 作为只读提问对象），记录到 `docs/Runs.md`，根据结果调整提示词 | 3 个问题都有带引用的回答 |
| 6 | 仓库概览：`Tools.overview()` 生成浅层目录树，`context.initial` 把它放进第一条用户消息（见 5.2、5.3） | 在 MultiAgentOS 上重问“入口在哪”，对比第 3 轮的步数和费用，记录到 `docs/Runs.md` |
| 7 | 重复调用检测 + `Settings` 开关 | `pytest` 通过 |
| 8 | 完善 ORIENT：概览加入 AGENTS.md/CLAUDE.md、README、清单文件 | `pytest` 通过 |
| 9 | `ablate.py` 消融实验：在 MultiAgentOS 上跑 5 种配置，分析写进 `docs/Runs.md` | 能说出每个组件对步数、费用、引用质量的影响 |

预计总量：代码约 420 行，测试约 150 行。

## 8. 与 M1 的对照（读代码时用）

| M0.5 里的一段代码 | M1 里对应的模块 | M1 为什么要做得更复杂 |
|---|---|---|
| `agent.run` 的循环 | Workflow | 需要预算、多种终止条件、来源验收、多 Task |
| `Tools.run` 前的路径检查 | Kernel 准入 + Executor | 需要审计记录、权限、以后还要支持写文件和执行命令 |
| `Tools.specs` 和系统提示词 | AgentToolPool | 需要版本化，保证评测和复现时知道用的是哪一版 |
| `context.initial` / `trim` | ContextEngine | 需要检索排序、稳定前缀、按 token 预算装配、来源记录 |
| `LLMClient` | Kernel 模型适配器 | 需要切换供应商、结构化输出校验、用量统计 |
| `main.py` | CLI / UserInteraction | 需要会话、查看运行状态、取消 |

建议的阅读方式：先把 M0.5 跑通并读懂，再逐行对照上表，问自己“M1 这一块多出来的每个东西，解决的是 M0.5 里哪个真实问题”。
答不上来的，就是 M1 可以先砍掉的。

### 8.1 用 Harness 视角读本项目

Harness 指包在模型外面、让它能可靠干活的那层程序。模型本身是无状态函数，循环、工具、上下文管理和停止条件都属于 Harness。
**Harness 的每个组件都对应一个“模型自己做不到”的假设**；读代码时可以对每一段问：它在防什么？

| 本项目的组件 | 它背后的假设 | 对应的通用做法 |
|---|---|---|
| 三个只读工具，按需列目录、搜索、按行读 | 把整个仓库塞进上下文，模型会失焦（context rot） | 即时检索、渐进式披露：先看结构，再定位，最后只读需要的几行 |
| 工具输出上限、`read_file` 每次 200 行 | 上下文是有限资源，塞得越多，每个 token 的价值越低 | 把 token 当预算来花 |
| 出错时返回 `ERROR:` 字符串而不是抛异常 | 模型看到错误后能自己换参数重试 | 工具对错误鲁棒，错误本身就是反馈 |
| `context.trim` 用占位符替换旧的工具结果 | 历史越长，旧的原始结果越没用 | 压缩（compaction）的最简版：丢掉冗余的工具结果，需要时让模型重新查。代价是改写了历史，从被改处往后缓存失效，所以只在超限时才裁剪 |
| 固定的系统提示词和工具集，会变的信息放进用户消息 | 缓存按前缀匹配，动了底层，上面全部按全价重算 | 稳定前缀：用消息改变状态，而不是改工具集或系统提示词 |
| `max_steps` 和最后一次 `tool_choice="none"` 的调用 | 模型不一定会自己停下 | 硬性终止条件 |
| 回答必须引用 `路径:行号` | 模型会自信地编造，自己评估自己时会偏乐观 | 让输出可以被外部核对（真正的核对见第 9 节） |

我们做这个仓库的方式本身也是一个 Harness：本规划是需求说明，第 7 节是逐项的功能清单；每步只做一件事、一个 commit、停下来让 meti 验收。
写代码的 agent 对应 Coder，meti 对应独立的 Evaluator，git 历史和本文对应跨会话的笔记（progress 文件）。
这样做正是为了避开两种常见失败：一次写太多、过早宣布完成。

本项目刻意不做子 agent、持久记忆和多轮压缩：问一个仓库问题通常不到 10 步，用不上。
等第 5 步的试跑记录真的显示出问题，再从第 9 节里挑来做。

## 9. 以后再说（现在不写）

- 结构化的最终回答（JSON 格式、引用列表校验）
- 程序化核对回答里引用的 `路径:行号` 确实存在、内容相关（最便宜的独立 Evaluator，代替让模型自评）
- 用模型生成的摘要代替 `trim` 的占位符（真正的 compaction），对比两种做法的回答质量和费用
- 检测“连续几步没有新信息”并提前停止
- 引用写完整路径：消融实验里 68 处引用只写了文件名（如 `index.ts:42`），无法程序化核对；可以在提示词里要求每处都写完整路径，或让核对程序按已出现过的完整路径补全
- 消融实验加大样本（每组 5 次以上）再判断严格提示词和重复调用检测是否值得保留
- 更好的搜索（按文件名、按符号、结果排序）
- 开启 thinking 模式后的效果和费用对比
- 把一次运行的完整 messages 存成 JSON，方便回看和评测
- 密钥脱敏：工具结果和用户问题进入 messages 之前，用正则（如 `sk-...`）把疑似密钥替换成 `[REDACTED]` 并提示用户。它属于 Kernel 的准入层（所有内容的必经之路），不属于 ContextEngine；对应本项目就是放在 `Tools.run` 返回之前

## 10. 给新对话的开场提示

新对话连接 `mini-agent` 文件夹后贴：

```text
我是 meti，更熟 Python。这是我的学习用仓库 mini-agent，和团队的 MultiAgentOS 无关。
请按 docs/Plan.md 实现最小仓库问答 agent，从第 7 节第 0 步开始，一步一个 commit，
每步完成后停下来让我看。边写边用一两句话解释关键写法；
不要加规划里没有的功能，有想法写进规划第 9 节。
```
