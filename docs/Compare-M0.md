# mini-agent（TypeScript 版）与 experiment/M0 的横向对比

> 对应 `docs/Plan.md` 第 7 节第 12 步。2026-09-29。
> 对比对象：mini-agent `ts-port` 分支（`653a74f`）；MultiAgentOS `experiment/M0` 分支（`c337224`，Cary）的 `experiments/minimal-agent-loop/`。
> M0 的代码只通过 `git show` 只读查看，没有检出、修改或运行 MultiAgentOS 里的任何东西。
>
> **2026-10-01 更新**：M0 之后又有 3 个 commit（目标改成 C 仓库静态审查，回路已实现）。第 1–6 节是对骨架版 `c337224` 的对比，保留作历史；
> 对最新版 `62966c1` 的评审见第 7 节。

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

---

## 7. 第二次评审：experiment/M0 @ `62966c1`（2026-10-01）

> 范围：`experiments/minimal-agent-loop/`，源码约 1,840 行（`workflow.ts` 454、`contracts.ts` 357、`model-executor.ts` 318、`kernel.ts` 215、
> `agent-tool-pool.ts` 199、`file-read-executor.ts` 185 等），测试 351 行（vitest，10 个用例）。
> 方法：用 `git show` 逐个文件通读，没有运行（不在 MultiAgentOS 里执行任何东西，也没有调用模型）。下文行号都指 `62966c1` 的这个目录。
> 最新 commit 的说明是“实现了状态机和多agent定义（**仍不可正常使用**）”。

### 7.1 它现在是什么

一个**双 Agent 的只读 C 代码审查**：PlannerAgent 判断是直接回答（比如 “hi”）还是把任务交给 CRepositoryReviewAgent；
后者先拿到 `.c/.h` 文件清单，再一次读一段文件（≤120 行），最后提交带引用的审查报告。测试用的仓库是一个故意埋了内存错误的 200 行 C 项目。

调用链：`Kernel.run` 循环 → `Workflow.start/resume` 返回下一个 `UnitIntent` → `Kernel` 准入检查 → 分发给 ContextEngine / ModelExecutor / FileReadExecutor
→ `UnitCompletion` 交回 Workflow。每读一次文件要走 `FILE_READ → CONTEXT_BUILD → MODEL_CALL` 三个 Unit。

### 7.2 架构：方向对，但结构比能力走得快

**做得好的地方（这些是成熟系统的做法，值得保留）**

1. **职责边界清楚**：Workflow 只管业务状态、不碰 IO；Kernel 是所有执行的唯一入口；ContextEngine 只组装、不取数据；Executor 不做判断。
   这和 M1 规范一致，也是以后加权限、审计、预算时只改一处的前提。
2. **Workflow 是“纯”的状态转换**：`start/resume` 接收旧状态、返回新状态和下一个动作（`workflow.ts:83-231`），自己不执行任何东西。
   这样它天然可测、可重放，以后做持久化和断点恢复也只要把 `WorkflowState` 存下来。这是整个设计里最有远见的一点。
3. **逻辑执行和物理执行分开**：Workflow 的 `UnitRun` 和 Kernel 的 `UnitAttempt` 分开（README“双层状态机”），给以后的重试、租约、fencing 留好了位置。
4. **错误是类型的一部分**：`Result<T>` 带稳定错误码和 `retryable`，没有到处抛异常。
5. **文件访问的安全边界比 mini-agent 更严**（`file-read-executor.ts:22-125`）：拒绝绝对路径、`..`、路径上任何一级符号链接、非普通文件，
   `realpath` 前后各检查一次是否在仓库内。mini-agent 允许仓库内部的符号链接，只检查最终位置。
6. **引用按“读过的范围”核对**（`workflow.ts:70-78, 380-386`）：报告里的每条引用必须落在某次 FILE_READ 实际返回的行范围内。
   这比 mini-agent 的核对强——mini-agent 只查“文件和行号存在”，M0 查的是“模型真的看过这几行”，正好对着 mini-agent 第 1 轮发现的“没读到就下结论”问题。

**主要问题**

1. **韧性缺失：任何一步出错，整个运行就失败。** 这是目前最严重的问题，很可能也是“仍不可正常使用”的直接原因。
   `Workflow.resume` 收到任何非成功的 Unit 结果都直接返回 `FAILED`（`workflow.ts:165-183`），包括这些**模型完全可以自己改正的情况**：
   - 模型请求的文件路径写错、行号越界（`FILE_READ` 失败）；
   - 模型回复不是合法 JSON、或者在 JSON 前面多写了一句话（`INVALID_MODEL_ACTION`，`model-executor.ts:261-268`）；
   - 报告引用了没读过的范围（`UNSUPPORTED_REVIEW_CITATION`）；
   - 还没读文件就交报告（`REVIEW_EVIDENCE_REQUIRED`）；
   - 用完模型调用次数（`MODEL_CALL_LIMIT`，`workflow.ts:275-277`）——前面花的钱全部作废，用户什么都拿不到。

   mini-agent 的数据说明这些情况真的会发生：试跑里模型写错正则后看到 `ERROR: invalid regex` 会自己改；TS 版评测里模型去搜一个猜出来的、不存在的目录。
   **对模型来说，“出错 → 看到错误 → 改正”是正常工作的一部分，不是异常。**

2. **“重试”只有字段，没有行为。** `retryable` 在 ModelExecutor 里算得很细（408/429/5xx、超时、断网），但没有任何代码读它；
   `attemptNumber` 写死为 1（`kernel.ts:191, 210`）。一次 429 限流就会让整个审查失败。

3. **“多 Agent”和“状态机”目前是写死的流水线。**
   - Workflow 里直接判断是不是 Planner、是不是 Reviewer（`workflow.ts:258, 272, 296, 357`），`listRoutingAgents` 也写死只返回 Reviewer（`agent-tool-pool.ts:189-191`）。
     加第三个 Agent 要改 Workflow 本身，`AgentDefinition` 这套“定义驱动”的结构目前还没有承重。
   - 状态只是被赋值的标签，没有“哪些转换合法”的检查：Planner 从 `READY` 直接到 `WAITING_UNIT`，从来没进过 README 画的 `RUNNING`；
     Reviewer 在同一个函数里先设 `READY` 再立刻设 `RUNNING`（`workflow.ts:329-340`）。文档里的状态机和代码里的状态机已经不一致。
   - 路由本身是一次模型调用：对“有没有给仓库”这种几乎是确定性的判断，每次运行都多花一次调用、多一个失败点。在只有一个专家 Agent 时，规则路由更便宜也更可靠。

4. **模型协议偏弱。**
   - 没用模型 API 自带的工具调用，而是要求模型在正文里输出 JSON，再自己解析（`model-executor.ts:66-126`）；也没开 JSON 模式、没设 `max_tokens`。格式稍有偏差就是第 1 条里的整体失败。
   - **一次回复只能有一个动作**（`contracts.ts:175-178` 的 `action` 是单数）。上次对比给过数据：mini-agent 平均每次模型调用发出 2.3 个工具调用；
     一次一个，同样的审查大约要多一倍的模型调用，而 Reviewer 只有 12 次额度。

5. **上下文组装：能用，但浪费。** ContextEngine 每次把整个状态 `JSON.stringify(…, null, 2)` 成一条用户消息（`context-engine.ts:16-32`），
   读到的源码也在 JSON 字符串里，换行和引号都被转义成 `\n`、`\"`——token 更多，模型读带行号的代码也更吃力。
   好在可变的 `status` 放在最后、观察结果只往后加，前缀大部分稳定，缓存还能部分命中。没有长度预算，目前靠 16 次 × 120 行的上限兜住。

6. **没有记账。** `ModelResponse.usage` 被解析出来，但没人累加，也不区分缓存命中（`model-executor.ts:270-278`）。跑一次花了多少钱、多少 token，目前回答不了。

7. **看起来可复现，实际没有校验。** `RepositoryRef.revision` 只是一个原样透传的标签（`file-read-executor.ts:113`），没有对照 git 检查文件是不是这个版本。
   它让结果看起来“锁定了版本”，其实没有。要么接上 git，要么改名叫 `label`。

8. **小的代码质量问题**：`referencesMatch`、`failure` 在 4 个文件里各写了一遍；准入检查在 Workflow（`workflow.ts:403-413`）和 Kernel（`kernel.ts:116-133`）各做一次，
   两处逻辑一样，改一处很容易忘另一处；Kernel 的 `attempts` 只增不减。都不急，但适合趁早统一。

### 7.3 测试和验证

- 10 个用例，用假的 `fetch` 跑通了“问候直接回答”和“handoff → 读文件 → 报告”两条完整路径，还检查了每次 ContextBuild 都经过 Kernel。
  **上次对比建议的“先写假模型的闭环测试”已经做到了**，这是很好的基础。
- 但完整回路的 3 个测试都只走**成功路径**。上面 7.2 第 1 条的 5 种失败，没有一个有测试；最可能出问题的地方正好没被覆盖。
- 没有 CLI、没有真实运行的记录。README 说故意在 C 仓库里埋了问题，“不保存预期答案，避免 Agent 读到”——出发点对，
  但结果是**没有任何地方记录答案**，审查报告好不好无法衡量。答案应该放在仓库根目录之外（比如 `test/` 下），Agent 读不到，评测脚本读得到。

### 7.4 能力对比

| | mini-agent | M0 @ 62966c1 |
|---|---|---|
| 任务 | 仓库问答 | C 仓库只读审查（+ 简单问题直接回答） |
| 能否真实运行 | 能；Python/TS 各跑过几十次 | 作者自述“仍不可正常使用”；没有运行记录 |
| Agent 数 | 1 | 2（路由 + 专家），写死 |
| 工具 | list_dir / search / read_file | file_read（+ 开头自动给文件清单）；**没有搜索** |
| 一次回复的工具调用 | 多个 | 1 个 |
| 工具出错 | 变成文本交给模型，模型自己改 | 整个运行失败 |
| 步数用完 | 强制作答，给出已有结论 | 整个运行失败 |
| 引用核对 | 查文件和行号存在 | **查是否落在已读范围内**（更强） |
| 路径安全 | 规范化 + 归属检查 | 更严：禁止任何符号链接 |
| 版本固定 | 无（靠 git） | `DefinitionRef` 固定 Agent/Prompt/Unit 版本 |
| 状态可重放 | 无 | Workflow 纯函数，有基础 |
| 成本 / 缓存 | 每次调用记账，区分缓存命中 | 无 |
| 评测 | 15 题题库、消融、跨实现对比 | 无 |
| 代码量 | 核心约 600 行（Python） | 约 1,840 行 |

一句话：**M0 的“骨架”已经比 mini-agent 专业，但“肌肉”还没长出来。** 它在边界、类型、可重放性上更好；
mini-agent 在“出了错怎么继续跑下去”和“怎么知道跑得好不好”上更好。对一个还跑不稳定的系统来说，后者更紧迫。

### 7.5 上次的建议落实了多少

| 上次建议（第 5 节） | 现状 |
|---|---|
| 先写假模型的闭环测试 | ✅ 已做 |
| 运行开始时给概览（ORIENT） | ✅ `REPOSITORY_VIEW`，只列 `.c/.h` 文件 |
| 稳定前缀 / 追加段 | ◑ 部分：系统提示词固定、观察结果只追加，但都在一条 JSON 里 |
| 工具失败作为 Observation 交还给模型 | ❌ 仍然整体失败（7.2 第 1 条） |
| 一次回复允许多个工具调用 | ❌ 仍是单个 `action` |

### 7.6 建议的后续规划（按优先级）

**P0 —— 先让它稳定地跑起来**（对应“仍不可正常使用”）

1. **把错误分成三类，分别处理**：
   - *模型能改的*（路径错、行号越界、格式不对、引用没读过、没读文件就交报告）→ 变成一条 Observation 交还给模型，设一个小的改正次数上限；
   - *可以重试的基础设施错误*（429、5xx、超时）→ Kernel 按 `retryable` 重试（`attemptNumber` 的位置已经留好了）；
   - *致命错误*（没有 key、定义找不到、协议不匹配）→ 结束运行。
2. **额度用完时强制作答**，不要整体失败：再调一次模型，要求它基于已读内容交报告，并说明还缺什么。
3. **给 5 种失败路径各写一个假模型测试**：比如模型先请求不存在的文件，看到错误后改正，最后成功交报告。
4. **建立度量**：加一个薄 CLI，输出和 mini-agent 一样的 `--json`（`answer / stopped_by / steps / usage`），就能直接用 mini-agent 的 `evaluate` 跑；
   把 C 仓库里埋的每个问题写成答案清单，放在仓库根目录之外。没有这一步，后面所有改动都只能凭感觉。

**P1 —— 降低成本、提高成功率**

5. 改用模型 API 自带的工具调用（或者至少开 JSON 模式），并允许一次回复多个动作。
6. 累加 `usage`，区分缓存命中，在结果里输出每次运行的 token 和费用。
7. 观察结果用纯文本块拼接（`path:start-end` 加带行号的代码），不要放进 JSON 字符串；固定内容在前、追加内容在后，给出长度预算。
8. 引用改为必填，并且也核对报告正文里出现的 `路径:行号`。

**P2 —— 让结构名副其实**

9. 二选一：要么让 Workflow 真的按 `AgentDefinition` 驱动（去掉写死的 Planner/Reviewer 判断），要么承认现在是固定流水线、先删掉不承重的抽象。
   在有第二个专家 Agent 之前，规则路由比模型路由更合适。
10. 状态机用一张“合法转换表”来强制执行，并加测试；README 里的图要和代码一致。
11. 准入检查只在 Kernel 做一次；公共小函数放到一个文件里。
12. `revision` 接上 git 校验，或者改名。
13. 换到更大的仓库之前加上搜索工具：只有文件清单和按行读取，在几百个文件的仓库里模型只能盲读（mini-agent 第 3 轮“找入口”撞上步数上限就是这个原因）。

### 7.7 mini-agent 可以反过来学的

1. **引用按“已读范围”核对**：把 `checkCitations` 从“行号存在”升级为“落在某次 read_file 返回的范围内”，正好对着第 1 轮的错误类型。
2. **结构化的最终回答**：引用作为单独字段，而不是从正文里用正则抠（规划第 9 节已有这一条）。
3. **更严的路径策略**：考虑完全禁止符号链接。
4. **可重放的状态**：把每次运行的完整 messages 存成 JSON（规划第 9 节已有），向 M0 的“状态进、状态出”靠拢。

### 7.8 给团队的一句话

**先让一条最简单的路径稳定地跑通，并且能量化“跑得好不好”，再往上加结构。**
M0 的边界设计是对的，以后也用得上；但现在每增加一层抽象，都在一个还没跑通的回路上增加失败点。
建议下一个 milestone 的完成标准写成“C 审查任务在 N 次运行里成功率 ≥ X%，找到埋设问题的比例 ≥ Y%，平均费用 ≤ Z”，而不是“实现了某某状态机”。

---

## 8. 其他分支：M0/ContextEngine 和 feat/M1（2026-10-01）

> 同样只用 `git show` / `git log` / `git diff` 只读查看；`M0/ContextEngine` 的运行结果读的是本地 `experiments/minimal-agent-loop/runs/`（不进仓库）。
> 背景：MultiAgentOS 的 `main` 只有 `docs:initial` 一个 commit，所有工作都在分支上。正式集成分支是 `feat/M1`（PR #1–#3），
> `experiment/M0` 和 `M0/ContextEngine` 都从它分出、没有合回去。

### 8.1 M0/ContextEngine（meti，在 `62966c1` 之上 5 个 commit；最新的 `e52714c` 还没 push）

这条分支用 mini-agent 的方法改 M0：**一次只改一个变量，用答案清单量化**。

| commit | 做了什么 | 对应第 7 节的哪条 |
|---|---|---|
| `2d7f49c` | 去掉 37 个文件的 UTF-8 BOM（pnpm 11 在 macOS 上拒绝读） | 新发现：之前在 macOS 上根本跑不起来 |
| `f7e6cdb` | 真实调用的基线脚本：每次调用记录缓存命中、输出、延迟、原文，按 mini-agent 的价格算费用 | P0-4 度量、P1-6 记账 |
| `5ebefe7` | 读取被截断时如实报告 `truncated`；**不再把本机绝对路径发给模型**（Planner 还会把它抄进 handoff） | 新发现：隐私泄露 |
| `0a50eb1` | 改用原生工具调用；`actions[]` 允许一次多个动作，Workflow 排队执行完再组装上下文；记录缓存 token | P1-5，上次的“多工具调用”建议 |
| `e52714c` | 18 个埋设缺陷（9 个核心、9 个隐蔽）的答案清单，放在 fixture 之外；打分脚本和单测 | P0-4，7.3 的“没有答案清单” |

**实测数据**（本地 `runs/`，deepseek-flash，thinking 关闭）：

| 版本 | c-review 成功 | 核心缺陷 | 隐蔽缺陷 | 模型调用 | 输入 token | 缓存命中 | 费用 |
|---|---|---|---|---|---|---|---|
| 原 M0（正文里写 JSON） | **0/3** | – | – | 2–4 | – | – | – |
| 原生工具调用（×10） | **10/10** | 86/90（96%） | 67/90（74%） | 5.4 | 23,035 | 70% | $0.0039 |

1. **0/3 → 10/10 证实了“协议是第一瓶颈”**。原 M0 三次全部 `INVALID_MODEL_ACTION`：模型要么一次写出好几个 JSON（想并行读文件，但协议只许一个动作），
   要么把 DeepSeek 内部的工具调用标记（`<｜｜DSML｜｜ invoke name="file_read">`）漏进正文。模型被训练成用原生工具调用，硬让它在正文里写 JSON 是在跟它的习惯对着干。
2. **新的头号浪费：上下文“失忆”导致重复读取**。10 次里重复读取 0–12 次（最多一次运行 16 次读取里 12 次是重复的）。看 rep10 的模型输出，
   几乎每一步都说“四个文件我都读过了”，然后又“确认一下”读几段。原因是每次 ContextBuild 只有“系统提示词 + 一条状态 JSON”，
   **模型上一轮自己写下的分析不会带到下一轮**，只能每次从头分析、重新读来核实。这正是第 4.3 节预言的问题，现在有了数据：

   | | 次数 | 模型调用 | 输入 token | 费用 | 核心 | 隐蔽 |
   |---|---|---|---|---|---|---|
   | 重复读取 ≤2 | 5 | 3.6 | 9,126 | $0.0033 | 8.8 | 7.0 |
   | 重复读取 >2 | 5 | 7.2 | 36,943 | $0.0045 | 8.4 | 6.4 |

   多读的部分让 token 翻了 4 倍、费用高 36%，**找到的缺陷反而略少**。注意这些重复不是“完全相同的调用”（行范围各不相同），
   所以 mini-agent 那种精确去重挡不住；要治的是上下文：把 assistant 消息和工具结果按原生协议逐条追加（mini-agent 的做法），或者至少保留模型上一轮的分析。
   按这条分支的节奏，这正好是消融的第 2 步。
3. **`empty-value-accepted` 10/10 都没找到**。先按 mini-agent 第 6、8 轮的教训检查是打分规则太窄还是模型真漏了，再决定改规则还是改提示词。

**代码层面还要注意的两点**

- **纯文本回复绕过了引用核对**：Reviewer 不调工具、直接回一段文字时被当作 FINAL（`toActions`），但没有 `citations` 字段，
  而 Workflow 用的是 `action.citations ?? []`（`workflow.ts:389`），空数组的 `every` 永远为真。`submit_review` 要求引用，纯文本却能绕过去。
  Reviewer 的纯文本回复应该被拒绝，或者要求它改用 `submit_review`。
- **错误仍然是致命的**（commit 说明里写明是有意保留，为了隔离变量）：一个回复里只要有一个工具调用的参数不合法，整个回复作废（`toActions` 返回 undefined），整次运行失败；
  `file_read` 和 `submit_review` 混在同一个回复里也会整体失败；排队读取中途超过 16 次的上限，前面的读取全部白费。这些是第 3 步该处理的。

### 8.2 feat/M1（正式集成分支）

约 6,400 行代码（不含 lockfile）、13 个包，另有约 3,000 行文档。按包看：

| 包 | 源码 | 测试 | 状态 |
|---|---|---|---|
| agent-tool-pool | 1,261 | 714 | **完整**：YAML 定义、schema 校验、摘要封存、运行时固定版本、撤销清单 |
| contracts | 1,233 | 362 | 大量 schema 和端口定义 |
| testing | 324 | 101 | 假的目录服务 + 共享契约测试 |
| kernel | 176 | 0 | 只能建 run、查 run、执行 CONTEXT；其他执行类型一律“不支持” |
| context-engine | 126 | 47 | 本地实现，忽略 `BoundaryContext`（第 3 轮已指出） |
| workflow | 93 | 69 | 只有 `create` / `inspect`，**没有推进步骤的循环** |
| executor | 145 | 117 | 只读执行器 |

**最重要的事实：`feat/M1` 里没有任何地方调用模型**（全仓库搜不到 `fetch`、`chat/completions`；`maxModelCalls` 只是一个预算数字）。
一个 RUN 请求进来，Workflow 建一个 `CREATED` 状态的记录、固定好定义版本，然后就结束了。**agent 的“心跳”——模型决定、程序执行、结果喂回——在正式分支里还不存在。**

**做得好的**

- **AgentToolPool 的版本封存是真正的工程价值**：Agent 定义的摘要覆盖模型、提示词、Unit、工具的整个闭包，运行开始时固定，之后只读固定的那一份；
  定义文件封存后不可修改，出问题的版本进撤销清单。这正好解决 mini-agent 评测里“结果对应哪一版提示词说不清”的问题（第 4.4 节），而且 `modelSettings.thinking` 必须显式写出，便于评测时对照。
- **契约测试工装**（`packages/testing` 的共享契约测试 + 假实现）让每个端口的真假实现跑同一套测试，这是多人并行开发时最有用的基础设施之一。

**问题**

1. **投入和风险倒挂**：最重的两个包（定义目录、契约，合计约 2,500 行源码）解决的是“版本可追溯”，而最大的未知——循环能不能跑通、上下文怎么组、错误怎么恢复——在正式分支里是零行。
   M0 实验已经用数据回答了其中几个问题（必须用原生工具调用、要允许多个动作、上下文不能失忆），但这些结论**还没有回流到 M1 的契约里**：
   M1 的 `ContextPack`、`UnitIntent` 是在这些实验之前定的。
2. **Kernel 的几个具体问题**（`packages/kernel/src/index.ts`）：
   - CONTEXT 在 Kernel 里内联执行，其他类型才走执行适配器——同一个“唯一出口”里有两种路由方式；
   - `catch {}` 把 ContextEngine 的所有异常折叠成 `CONTEXT_EXECUTION_FAILED`，`message` 就是错误码本身，原始原因丢失（第 3 轮也看到了）；
   - `BoundaryContext` 和 `UnitIntent` 的校验在 `try` 之外，校验失败会直接抛异常，而不是返回 `Result`——同一个方法里两种失败方式；
   - `usage.outputTokens` 填的是 ContextPack 的 token 数，语义不对（组装上下文不产生模型输出），以后汇总费用会算错。
3. **评测没有合进来**：`feature/M1/eval-fixtures`（10 个 commit、约 2,800 行：任务 schema、按固定 commit 核对证据锚点、网页快照规则）没有合进 `feat/M1`。
   而 `M0/ContextEngine` 又各自做了一套答案清单。两套评测，正式分支一套都没有。

### 8.3 整体判断和建议

三条线现在是分开走的：
- **feat/M1**：平台骨架和定义目录，质量高，但没有 agent；
- **experiment/M0 → M0/ContextEngine**：真正能跑的 agent，有数据，但是“实验”，不在正式分支；
- **eval-fixtures**：评测基础设施，悬空。

建议把它们按“先跑通、再度量、再固化”的顺序汇合：

1. **把 M0/ContextEngine 的消融做完**（第 2 步：上下文按原生协议逐条追加、不再失忆；第 3 步：错误分三类处理），每步用答案清单跑 ×10。
   目标是有一组数字：成功率、核心/隐蔽缺陷命中率、平均费用。
2. **用这些结论修订 M1 的契约**：`ModelResponse` 是多个动作；`ContextPack` 区分稳定前缀和逐条追加的消息；`UnitResult` 区分“交还给模型”“可重试”“致命”三类错误。
   这一步是把实验的结论写进正式设计，而不是把实验代码搬过去。
3. **在 feat/M1 里实现最小循环**：Workflow 能推进 CONTEXT → MODEL → FILE_READ → … → 报告，用 AgentToolPool 固定的定义；先用假模型跑通，再接真模型。
4. **合并评测**：把 eval-fixtures 合进 feat/M1，C 审查的答案清单也按它的 schema 收进去；M1 的完成标准写成“在这套题上成功率 ≥ X%、命中率 ≥ Y%、费用 ≤ Z”。
5. **Kernel 的四个小问题**顺手修掉，尤其是错误原因丢失和 `usage` 语义，否则以后的评测数据会不准。
