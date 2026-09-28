# mini-agent（TypeScript 版）与 experiment/M0 的横向对比

> 对应 `docs/Plan.md` 第 7 节第 12 步。2026-09-29。
> 对比对象：mini-agent `ts-port` 分支（`653a74f`）；MultiAgentOS `experiment/M0` 分支（`c337224`，Cary）的 `experiments/minimal-agent-loop/`。
> M0 的代码只通过 `git show` 只读查看，没有检出、修改或运行 MultiAgentOS 里的任何东西。

## 1. 一句话结论

两者想解决同一个问题——“最小的 agent 闭环长什么样”——但走的是相反的两条路：
**M0 先定边界和协议，再填实现**（契约完整，但目前所有组件都是空实现，还跑不起来）；
**mini-agent 先跑通循环，再用试跑和实验决定加什么**（能用、有数据，但模块边界很松）。
两边最值得互相借的，是 M0 的**类型化契约和版本固定**，以及 mini-agent 的**上下文/缓存设计和评测方法**。

## 2. 基本情况

| | mini-agent（ts-port） | experiment/M0 |
|---|---|---|
| 任务 | 本地代码仓库问答，回答带 `路径:行号` | 联网搜索后写报告（示例：“武汉大学 150 字报告”） |
| 状态 | 可运行；已在 MultiAgentOS 上真实运行约 35 次（Python 版） | 可编译的骨架；所有方法 `Promise.reject(NotImplementedError)` |
| 代码量 | 源码 1,120 行（其中 agent 本身约 750 行，另有评测和消融脚本） | 源码约 315 行，其中 `contracts.ts` 168 行 |
| 组件 | llm、tools、context、agent、main（+ ablate、evaluate） | Kernel、Workflow、ContextEngine、AgentToolPool、ApiCallExecutor、WebSearchExecutor、runtime |
| 依赖 | 运行时零依赖；开发只用 typescript、@types/node | 沿用仓库根目录的 tsc / vitest / eslint 配置 |
| 测试 | 34 个 `node:test` 用例 + 10 题评测集 + 消融脚本 | `test/.gitkeep`，还没有测试；README 里有一个手工验收场景 |
| 运行方式 | `node src/main.ts "问题"`（不编译） | `createExperimentRuntime().kernel.run({prompt})`（需编译） |

## 3. 模块对照

| M0 | mini-agent | 差异 |
|---|---|---|
| `MinimalKernel.run()` | `main.ts` | M0 把“最简 UserInteraction”放进 Kernel；mini-agent 由 CLI 直接调用循环 |
| `MinimalKernel.dispatch()` + `ModuleRequest` | 无（`agent.ts` 直接调用 `llm.chat` 和 `tools.run`） | M0 的模型调用和外部工具**都经过 Kernel**，这是 M1“Kernel 唯一准入”的雏形 |
| `MinimalWorkflow`（内含 Agent Loop） | `agent.run` | 同一个职责；M0 还没实现 |
| `MinimalContextEngine.build()` → `ContextPack` | `context.initial/trim` + `Tools.overview` | 设计差异最大，见 4.3 |
| `AgentToolPool`（`AgentDefinitionReader`、`PromptTemplateReader`） | `Tools.specs()` + `context.ts` 里的常量 | M0 有版本化定义，mini-agent 没有，见 4.4 |
| `ApiCallExecutor` | `LLMClient` | M0 要把模型输出转成 `AgentAction`；mini-agent 直接用 OpenAI 的 `tool_calls` |
| `WebSearchExecutor` | `Tools`（list_dir / search / read_file） | 工具领域不同：外部网络 vs 本地只读文件 |

## 4. 关键设计差异

### 4.1 调用链：谁来调模型

M0：`Kernel.run → Workflow → Kernel.dispatch(API_CALL_EXECUTOR) → ApiCallExecutor`，模型调用和搜索都绕回 Kernel。
mini-agent：`agent.run` 直接 `await llm.chat(...)`、`tools.run(...)`。

- M0 的好处：所有外部副作用有一个统一出口，将来加审计、权限、预算、重试都只改一处；这正是 M1 要的。
- 代价：多一层 `ModuleRequest/ModuleResponse` 的打包拆包。mini-agent 的经验是，在只有一个模型、三个只读工具时，这一层暂时没有承重。
  可以先让 `dispatch` 只做“记日志 + 转发”，等真的有第二种执行器或权限需求时再加内容。

### 4.2 模型输出协议：一次一个动作，还是一次多个

M0 定义 `AgentAction = FinalAction | ToolCallAction`，`ApiCallResponse` 里只有**一个** `action`。
mini-agent 用 OpenAI 格式，一次回复可以有多个 `tool_calls`。

这是这次对比里最值得带回 M0 的一条**数据**：消融实验（`docs/Runs.md` 第 5 轮）里，mini-agent 平均每次模型调用发出 **2.3 个**工具调用
（14.5 次工具调用 / 6.2 次模型调用）。如果协议限定一次一个动作，同样的任务大约要多一倍的模型调用，费用和延迟也跟着翻倍。
建议把 `ApiCallResponse.action` 改成 `actions: AgentAction[]`（或加一个 `TOOL_CALLS` 批量类型），Workflow 依次执行、一次性写回 Observation。

另外 `ToolCallAction.toolName` 写死为 `'web_search'`，`ToolObservation.input` 写死为 `WebSearchRequest`。
类型很安全，但每加一个工具要改 `ToolName`、`ModuleRequest`、`ToolObservation` 等好几处联合类型。
可以考虑让契约层只保留 `{ toolName: string; input: unknown }`，具体的输入校验交给各工具自己的 schema（mini-agent 的 `PARAMS` 表就是最简单的版本）。

### 4.3 上下文：每轮重建，还是只追加

M0 的 `ContextRequest = { objective, promptRef, observations }`，ContextEngine 每轮根据全部 Observation **重新组装**一个 `ContextPack`。
mini-agent 的 `messages` 是**只追加**的数组：固定的系统提示词和工具定义在最前，仓库概览和问题在第一条用户消息，之后只往后加。

- mini-agent 这样做是为了前缀缓存：DeepSeek 按前缀命中缓存，消融实验里缓存命中率是 **73%–84%**，命中部分只收 1/50 的价格。
  M0 目前的 `ContextPack` 没有规定各部分的顺序和稳定性；如果每轮组装时顺序或格式稍有变化，缓存就全部失效。
  建议在 `ContextPack` 里明确“前缀段”（指令、工具、目标）和“追加段”（Observation），前缀段要求字节级不变——这和 M1 ContextEngine 规范里的 PREFIX 规则是同一件事，M0 正好可以当试验场。
- M0 的 Observation 只有工具输入和输出，**模型自己上一轮说了什么没有保留**。mini-agent 把 assistant 消息原样追加回去，模型能看到自己之前的推理和调用。
  重建式上下文要决定是否保留这部分，否则模型容易重复同样的调用。
- M0 还没有长度预算或裁剪；Observation 会无限增长。mini-agent 的 `trim` 很粗糙，但保证了“只替换内容、不删消息”，工具调用和结果始终成对。
- M0 没有仓库概览（ORIENT）。在 mini-agent 上它是唯一被消融实验证实有效的组件：去掉后模型调用多 1.3 次、费用高 35%、6 次里 2 次撞上步数上限。
  M0 做的是网络搜索，对应物可能是“任务开始时先给一份搜索计划或已知信息摘要”，值得同样用实验验证。

### 4.4 版本化定义

M0 用 `DefinitionRef { id, version }` 固定 Agent、Prompt 和工具的版本，ContextEngine 只能按 `promptRef` 取**精确版本**，不能取“最新”。
mini-agent 的提示词和工具定义是源码里的常量，版本只能靠 git commit 追溯。

这是 M0 明显更好的地方。mini-agent 的评测结果（`runs/*.json`）目前没有记录用的是哪一版提示词；一旦提示词改了，旧结果就无法严格对比。
mini-agent 可以借鉴的最小做法：给 `SYSTEM_PROMPT` 和 `specs()` 算一个哈希，写进 `--json` 输出和评测结果。

### 4.5 错误处理

M0 的契约是 `Result<T> = { ok: true, value } | { ok: false, error: { code, message, retryable } }`，错误是类型的一部分，带稳定错误码和是否可重试。
mini-agent 分两类：工具错误变成 `"ERROR: ..."` 字符串交给模型自己调整；模型调用失败抛 `LLMError`，整个运行结束。

- M0 的 `Result` 更规范，也和 M1 规范里“保留稳定错误码”的要求一致。
- 但有两点值得注意：
  1. 骨架里的方法签名返回 `Promise<Result<...>>`，实现却是 `Promise.reject(...)`，调用方要同时处理 `ok: false` 和异常两种失败方式。实现时最好统一成只返回 `Result`。
  2. 工具失败（比如搜索没结果、参数错）不应该让整个运行失败，而应该作为一条 Observation 交还给模型。mini-agent 的试跑里，模型看到 `ERROR: invalid regex` 后会自己改写正则，这正是这条设计的价值。
     建议 Workflow 把 `WebSearchExecutor` 返回的 `ok: false` 转成 Observation，只有模型调用本身失败才结束运行。

### 4.6 停止条件和验收

两边都有最大步数的想法。mini-agent 的做法是步数用完后再调用一次模型、工具集不变但 `tool_choice: "none"`，逼它基于已有信息作答（保持前缀不变，缓存仍能命中）；实测撞上上限时这次调用给出了完整回答。
M0 的 README 提出了更进一步的设想：把“必须用过 web_search”“字数约 150”写成结构化约束，由 Workflow 检查、不满足就再来一轮——这就是一个内置的 Evaluator，mini-agent 还没有。

### 4.7 测试与评测

M0 目前只有一个自然语言的验收场景。mini-agent 的经验是，**假模型测试**（`FakeLLM` 按剧本返回回复）能在完全不联网的情况下把整个循环跑通，而且 M0 的结构非常适合这么做：
只要实现一个假的 `ApiCallExecutorPort` 和 `WebSearchExecutorPort`，就能测试 Kernel → Workflow → ContextEngine 的整条链路。建议作为 M0 实现的第一步。

## 5. 各自可以借鉴什么

**mini-agent 向 M0 学**
1. 契约先行：把 `Reply`、`Step`、`--json` 输出这些隐含格式写成明确的类型，放进一个 `contracts.ts`。
2. 版本固定：给提示词和工具定义加版本或哈希，写进评测结果。
3. `Result` 风格的错误：至少让 `LLMError` 带上稳定错误码和“是否可重试”。
4. 统一出口：如果以后加写文件或执行命令的工具，照 M0 那样让它们经过一个 `dispatch`，方便加权限检查。

**M0 可以参考 mini-agent**
1. 一次回复允许多个工具调用（实测平均 2.3 个）。
2. 上下文区分“稳定前缀”和“追加段”，为缓存设计；保留模型自己之前的消息。
3. 工具失败作为 Observation 交还给模型，不结束运行。
4. 先写假模型的闭环测试，再接真实 API。
5. 用固定题库 + 消融实验决定组件去留，而不是先把所有边界都建好。

## 6. 为什么这次没有“跑分对比”

M0 目前所有组件都是空实现，无法运行；而且它的任务是联网搜索，和 mini-agent 的仓库问答不是同一个领域，题库不能直接通用。
如果之后要做运行层面的对比，有两种办法：
- M0 实现后加一个薄 CLI，按 mini-agent 的 `--json` 格式输出（`answer`、`stopped_by`、`steps`、`usage`），就能直接接入 `evaluate` 脚本；题库需要另出一套搜索类的题。
- 或者反过来，给 M0 加上本地只读文件工具，用现有题库对比两者在同一任务上的步数、费用和事实命中率。
