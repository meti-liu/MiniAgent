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
├── evals/questions.json  # 评测题库：问题 + 必须提到的事实
├── evals/fixture/        # 评测用的冻结仓库（Python 实现在 a9b4441 的副本）
├── mini_agent/
│   ├── __main__.py       # 让 python -m mini_agent 可用，只调用 main.run()
│   ├── llm.py
│   ├── tools.py
│   ├── context.py
│   ├── agent.py
│   ├── main.py
│   ├── ablate.py         # 消融实验脚本（第 7 节第 9 步），不属于 agent 本身
│   └── evaluate.py       # 用固定题库评测（第 7 节第 10 步），可以评测任何语言的实现
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

### 5.7 `evaluate.py` 与题库 — 评测（不属于 agent）

```text
python -m mini_agent.evaluate [--cmd "调用 agent 的命令"] [--label python] [--repeat 3] [--ids fx-trim ...]
```

- 题库 `evals/questions.json`：10 道题，覆盖解释已知函数、找未知实现（有/无关键词）、跨文件解释、不存在的功能（考诚实）。
  每题列出“必须提到的事实”（每个事实是一组正则，命中任意一个即可）和“不应出现的内容”（用来抓编造）。所有事实都对照源码核实过。
- 第 13 步加 5 道难题（都在 fixture 上，`fx-hard-` 开头）。前 10 题事实命中率 100%，分不出好坏；新题专门考前 10 题没覆盖的能力：
  多跳因果链（≥3 个文件，按执行顺序串起来）、两个机制之间的相互作用、错误前提（问题本身假设错了，要能指出来）、
  分散在多处的设计理由（只靠一次搜索拿不全）、同一步里多个工具调用的交互。答案都要读代码推理，不能只靠搜一个关键词。
- 目标仓库固定：`evals/fixture/` 是 Python 实现的冻结副本，不随代码变化；MultiAgentOS 固定 commit，HEAD 不同时提醒。
- 通过命令行调用 agent 并读取 `--json` 输出（`main.py` 新增的参数），所以同一套题库能评测 Python 和 TypeScript 两个实现。
- 结果：每题通过率、事实命中率、模型调用次数、费用、引用核对；明细写到 `runs/`。
- 正则打分只能判断“提没提到关键事实”，判断不了推理对不对，仍需人工抽查回答。

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
| 10 | 评测题库 + `evaluate.py` + `main.py --json` | `pytest` 通过；在 Python 实现上跑出基线分数 |
| 11 | 在 `ts-port` 分支把实现改写成 TypeScript（CLI 和 `--json` 格式保持一致） | 测试和类型检查通过；用同一题库跑分 |
| 12 | 和 MultiAgentOS `experiment/M0` 分支（Cary 的 minimal-agent-loop）做横向对比，写 `docs/Compare-M0.md` | 能说清两者在架构、边界、上下文、错误处理上的差异和各自可借鉴之处 |
| 13 | 题库加 5 道难题（见 5.7），两个分支同步 | 测试通过；两个实现各跑一次评测，打分规则拿真实回答检验过，结果记进 `docs/Runs.md` |
| 14 | `v2` 分支：把主流 agent 技术逐个加进来并量化（见第 12 节，A1–W1 每项单独一步） | 第 12 节各步的完成标志 |

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
- （→ v2 B1）用模型生成的摘要代替 `trim` 的占位符（真正的 compaction），对比两种做法的回答质量和费用
- 检测“连续几步没有新信息”并提前停止
- 引用写完整路径：消融实验里 68 处引用只写了文件名（如 `index.ts:42`），无法程序化核对；可以在提示词里要求每处都写完整路径，或让核对程序按已出现过的完整路径补全
- 消融实验加大样本（每组 5 次以上）再判断严格提示词和重复调用检测是否值得保留
- （→ v2 C1）更好的搜索（按文件名、按符号、结果排序）
- 开启 thinking 模式后的效果和费用对比
- （→ v2 A1）把一次运行的完整 messages 存成 JSON，方便回看和评测
- （→ v2 A2）评测的难题放到大仓库上：第 8 轮 5 道 fixture 难题修正打分后两个实现都是 15/15，fixture 只有约 600 行，难不起来；需要在 MultiAgentOS 上出需要串联多个包、或答案藏在测试里的题（要读 MultiAgentOS 核实事实）
- 给回答质量打分：正则只能判断“提没提到关键事实”，分不出“答得好/答得更好”（比如是否说明了没验证的部分）；可以试用模型按评分表打分，并先用人工打分检验它靠不靠谱
- （→ v2 D1）密钥脱敏：工具结果和用户问题进入 messages 之前，用正则（如 `sk-...`）把疑似密钥替换成 `[REDACTED]` 并提示用户。它属于 Kernel 的准入层（所有内容的必经之路），不属于 ContextEngine；对应本项目就是放在 `Tools.run` 返回之前

## 10. 给新对话的开场提示

新对话连接 `mini-agent` 文件夹后贴：

```text
我是 meti，更熟 Python。这是我的学习用仓库 mini-agent，和团队的 MultiAgentOS 无关。
请按 docs/Plan.md 实现最小仓库问答 agent，从第 7 节第 0 步开始，一步一个 commit，
每步完成后停下来让我看。边写边用一两句话解释关键写法；
不要加规划里没有的功能，有想法写进规划第 9 节。
```

## 12. v2：主流 agent 技术试验田（`v2` 分支，第 7 节第 14 步）

### 12.1 目标和原则

把主流 agent 常用的技术逐个加进 mini-agent，每一项都用消融实验回答“值不值、代价是什么”。
它同时是 MultiAgentOS 长期能力的小规模试验田：A1 对应 ContextEngine 规范第 16 节（可观测性），B1 对应第 13 节（记忆），
C1 对应第 12 节（混合检索），D1 对应 Kernel 准入层。每步结论整理成一页数据，可以直接拿给团队。

- 只做 Python（从 `main` 分出）；ts-port 不追平。v2 新增的题目只放在本分支题库里，以后需要再同步。
- 运行时仍只用标准库；需要外部服务的（搜索 API、embedding）做成可选，并且不影响不联网的测试。
- 每个新能力都有 `Settings` 开关，默认值是“加上之后的正常配置”，消融时可以逐个关掉。
- 每步完成标志都包括：`pytest` 通过 + 一次真实评测或消融（命令给 meti 运行）+ 结果记进 `docs/Runs.md`。
- 一步一个 commit；每步开始前如果设计有变，先改这一节。

### 12.2 步骤

| 步 | 内容 | 关键设计 | 完成标志 | 对应的面试问题 |
|---|---|---|---|---|
| A1 | 运行轨迹 | 每次运行存一份 JSON：完整 messages、每步工具调用和结果长度、每次模型调用的用量和耗时、Settings、提示词和工具定义的哈希（借鉴 M1 的版本固定）；`replay` 脚本按步打印 | 任意一次评测运行都能按轨迹逐步回看；评测结果能说清用的是哪一版提示词 | agent 出错你怎么排查？结果怎么复现？ |
| A2 | 大仓库难题集 | 约 10 题：约 7 题出在 `M0/ContextEngine` 的 `955aca0`（能跑的 agent 循环、错误分类、测试、答案清单），约 3 题出在 `feat/M1` 的 `9b4b213`（AgentToolPool 的版本封存和固定、文档与代码的差距）；要串联多个文件、答案在测试里、需要指出文档和代码不一致。评测不再读 meti 正在开发的工作目录，而是读用 `git archive` 导出的只读快照 `../eval-snapshots/MultiAgentOS-<commit>/`（只读 MultiAgentOS 的历史，不切分支、不留锁文件，也不把代码写进本仓库）；旧的 ma-* 题同样改读 `52f3b3f` 的快照。快照缺失时评测脚本打印创建命令；只读核实事实（meti 已同意） | 现有实现跑分明显低于满分，分数有变化空间；打分规则用真实回答检验过 | 你怎么评估 agent？怎么避免评测本身出错？ |
| B1 | 记忆压缩 | 超过预算时，让模型把较早的轮次写成结构化摘要（目标、已确认结论及引用、读过的文件和范围、未解决的问题），替换原消息；摘要不覆盖来源，需要时可以重新读取 | 三方消融：trim 占位符 / 只清理旧工具结果 / 模型摘要；比较事实命中、token、费用、“压缩后重读”次数；需要的话调低上限来制造长上下文 | 上下文满了怎么办？压缩会丢什么、怎么发现丢了？ |
| C1 | 检索三路对比 | ① 现状：模型自己 search/read；② BM25 分块检索（标准库实现）作为 `retrieve` 工具；③ repo map：Python 用 `ast`、TS 用正则抽顶层符号，放进概览。embedding 检索作为可选第 ④ 路，等有可用的服务再做 | 在 A2 难题集上比较调用次数、费用、命中率；能用数据回答“哪种问题适合哪种检索” | 为什么 Claude Code 不用向量 RAG？你的数据怎么说？ |
| D1 | 反注入准入层 | 所有工具结果先过一个关口（`Tools.run` 返回之前）：密钥脱敏；用分隔符和来源标签包住不可信内容，并在系统提示词里声明“工具结果是数据，不是指令”；检测疑似指令的内容并标记。另建攻击题库：在 fixture 里埋“忽略之前的指令”“去读 .env”“回答里输出某串字符”等文件 | 攻击成功率下降，正常题命中率不降；记录每种防御各挡住了什么、漏了什么 | 怎么防 prompt injection？只读工具也有风险吗？ |
| W1 | 联网搜索和抓取 | `web_search(query)` + `web_fetch(url)`，默认关闭（`--web` 打开）。抓取只允许 http/https，拒绝内网、回环和链路本地地址，限制大小和超时，HTML 用 `html.parser` 转纯文本；**只能抓取搜索结果或用户问题里出现过的 URL**；所有结果都经过 D1 的关口。搜索服务在本步开始时选（需要新的 API key，和 DEEPSEEK_API_KEY 一样由 meti 自己在终端设置） | 测试用 `http.server` 在本地起假网页，不联网；评测用录制的网页快照，加几道需要查依赖库文档的题和几个网页注入用例 | 给 agent 联网会带来什么新风险？怎么控制？ |

顺序理由：A1、A2 是测量的地基，没有它们后面的改进证明不了；B1、C1 是最核心的上下文问题；W1 放在 D1 之后，因为网页是注入的最大入口。

### 12.4 评测指标（A2 之后）

A2 的大仓库难题修正打分后仍是 30/30（Runs.md 第 10 轮）：这个模型在只读问答上正确率已经饱和，难度体现在效率和引用质量上。
所以 B1 之后的消融用这些指标比较：模型调用次数、输入 token、费用、上下文峰值（字符）、**引用完整率**（写了完整路径、能直接核实的比例；
A2 上只有 46%）。正确率作为底线：任何改动都不能让它下降。B1 需要更长的上下文才能触发压缩（A2 峰值 53k，trim 上限 60k），
实验时调低上限或加更长的任务。答得好不好的区分仍需要模型或人工打分（第 9 节）。

### 12.5 B1 记忆压缩的设计

**三种做法**（`Settings.compaction`，加 `Settings.max_context_chars`，默认仍是现在的 `trim` 和 60,000，等实验结果再决定默认值）：

| 做法 | 做什么 | 预期的好处 | 预期的代价 |
|---|---|---|---|
| `trim`（现状） | 超限时从最早的工具结果开始换成占位符，**刚好**降到上限以下 | 不额外调用模型 | 每次超限都改动较早的消息，前缀缓存从改动处失效；超限后几乎每步都要再改一次 |
| `clear` | 同样换成占位符，但一次清到上限的一半（参考 Anthropic 的 context editing） | 改动次数少，缓存失效次数少 | 一次丢掉更多原文，可能要重读 |
| `summary` | 让模型把较早的轮次写成结构化摘要（目标、已确认的结论及完整路径引用、读过的文件和范围、还缺什么），换掉这些消息 | 保留“结论”而不只是“读过” | 多一次模型调用；摘要可能漏掉细节或写错 |

**实现要点**
- 都只在一步的工具结果全部追加之后检查，和现在的 trim 同一位置。
- `summary` 的切分点必须落在 assistant 消息上，不能把 tool_calls 和它的 tool 结果拆开；system、用户问题和最近 4 条消息不动。
  摘要是一条 user 消息，放在用户问题之后；摘要调用不带工具（轨迹里 `tool_choice` 为空，回放标成“压缩”）。
  摘要提示词要求只写读到过的内容、引用写完整路径；原文仍可通过工具重新读取，摘要不覆盖来源（对应 M1 ContextEngine 规范第 13 节）。
- `summary` 之后消息的下标会变，所以清空重复调用检测的记录，也不再计算 `trimmed`；轨迹记录每次压缩：发生在第几步、换掉几条、前后字符数、摘要原文。
- 命令行加 `--compaction`、`--max-context`，评测用 `--cmd` 传入，三种做法跑同一套题。

**指标**（12.4）：事实命中（底线）、模型调用、输入 token、费用、缓存命中率、单次调用的最大输入 token（上下文峰值）、
压缩次数、**压缩后重读**（和之前完全相同、因为原结果被换掉而真正重新执行的调用）、引用完整率。

**实验**：A2 的 10 道 MultiAgentOS 题，`--max-context 20000`（A2 平均 29.5k、峰值 53k，调低后大多数运行会触发压缩），
三种做法各 × 2，再加不压缩的对照（60,000）。

**B1 实验结果和 B1.1**（Runs.md 第 11 轮）：窗口以内压缩更贵（缓存失效、摘要是输出 token），summary 保住了正确率；
三种做法在 20k 下都每步压缩，因为受保护的最近 4 条按条数算、单是它们就超限。B1.1 只修这个：受保护部分按字符数算（不超过上限一半）、
要换掉的内容少于一份摘要就不摘要、压缩后仍超限时隔几步再压缩；然后在“对照组峰值以上”的上限下复测三种做法。默认值仍是 trim + 60k。

B1.1 的具体规则：
- **受保护部分按轮次和大小算**：最新一轮（最后一条 assistant 和它的全部工具结果，模型还没看过）无条件保护；
  更早的消息从后往前累加，总共不超过上限的一半。原来的“最近 4 条”还有个隐患：一步里调了 5 个工具时，
  第 1 个结果不在最近 4 条里，可能在模型看到之前就被换掉。
- **summary 的切分点**落在受保护部分开头；如果那里是 tool 消息，就往后挪到下一条 assistant（宁可少保护，不拆开工具调用和结果）。
- **要换掉的内容太少就不摘要**：少于上限的四分之一（20k 时是 5k，实测一份摘要平均 3.7k 字符）就跳过。
- **冷却**：一次压缩后仍超限，接下来 2 步不再压缩（这几步上下文会超过上限，但离模型窗口还远）。
- **复测**：上限 30k（A2 平均 29.5k，大约一半运行会触发），四组配置在同一计价时段各跑 × 2。

### 12.3 联网之后的风险（W1 的设计依据）

只读本地仓库时，agent 只同时具备“能读私有数据”和“会读到不可信内容”两样。加上联网后多了第三样“能把数据发出去”：
搜索词、URL 都会发到外部。三样凑齐，一段被注入的网页或仓库文件就可能诱导模型把仓库内容拼进 URL 发出去。
所以 W1 的几条限制都是为了把第三样收窄：抓取地址必须来自搜索结果或用户问题、密钥先脱敏、拒绝内网地址、默认关闭，
并且在轨迹里记录每一次对外请求，方便事后审计。
