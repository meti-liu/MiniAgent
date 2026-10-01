# CLAUDE.md — 给 Claude Code 的交接说明

meti 的个人学习仓库：一个最小的仓库问答 agent，目的是看懂 agent 怎么运转。meti 更熟 Python。
和团队项目 MultiAgentOS 是两个独立仓库。

## 先读这些
- `docs/Plan.md`：唯一的需求来源；第 7 节是步骤表，第 9 节是以后再做的想法
- `docs/Runs.md`：每一轮试跑、消融、评测的数据和结论
- `git log`：每一步改了什么、为什么改

## 工作规矩
- 规划先改，代码后写；规划里没有的功能不加，新想法记进 Plan.md 第 9 节
- 一次只做一步，做完停下来，说明：改了哪些文件、怎么运行验证、关键概念（必要时用 Python 常见写法类比）
- 代码短而直白，宁可多几行也不炫技；每个文件开头用两三句话说明它对应 agent 的哪个部分
- 每步一个 commit：summary 用 `feat:` / `fix:` / `test:` / `docs:` 前缀加英文祈使句，正文用 `- ` 列出改了什么和为什么
- 不要 push，由 meti 自己决定
- 不读取、不打印 DEEPSEEK_API_KEY；需要真实调用模型时，把命令给 meti，让他在自己终端运行
- `.env` 不进仓库
- `../MultiAgentOS` 只读：只有 meti 同意时才读，只读被引用的那几行，不改任何东西，也不把它的代码写进这里；
  在那个仓库里只用不留锁文件的 git 命令（`GIT_OPTIONAL_LOCKS=0`），不要 `git status`
- 省 token：不要一次读很多无关文件，不要生成规划以外的长文档

## 分支
- `main`：Python 实现
- `ts-port`：TypeScript 实现（Plan.md 第 11 节），和 Python 版行为、CLI、`--json` 格式一致
- 两个分支共用 `evals/questions.json` 题库，改题库时两边要同步

## 当前进度（2026-10-01）
- Plan.md 第 7 节 0–12 步都已完成
- Python 版评测基线：10 题 30/30 通过，平均每题 $0.0012（Runs.md 第 6 轮）
- TS 版评测已和 Python 基线对比（Runs.md 第 7 轮）：行为一致；修了 search 遇到不存在路径时崩溃的 bug
- 比较费用时注意计价时段：本机 NZDT，UTC 06:00 起是高峰价 ×2，看 token 比看美元可靠
- 已知问题：题库偏容易（事实命中率 100%），需要加更难的题

## 本分支（ts-port）怎么运行
```bash
npm install                                  # 只有 typescript 和 @types/node；Node ≥ 22.18
node src/main.ts "问题" --repo 路径 [--json]   # Node 直接运行 .ts，不编译
npm test && npm run typecheck                # node:test + tsc --noEmit
node src/evaluate.ts --repeat 3              # 用题库评测，结果写到 runs/
node src/ablate.ts --repo 路径 "问题" ...      # 消融实验
```
只用可擦除的 TS 语法（不用 enum、不用构造函数参数属性），import 写 `.ts` 后缀。
