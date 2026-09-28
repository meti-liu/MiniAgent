# mini-agent（TypeScript 版，`ts-port` 分支）

最小的仓库问答 agent，用来学习 agent 怎么运转。规划见 `docs/Plan.md`（第 11 节是 TypeScript 版说明）。

```bash
npm install                                   # 只装 typescript 和 @types/node，运行时零依赖；需要 Node ≥ 22.18
export DEEPSEEK_API_KEY=...                   # 自己在终端设置，不要写进仓库
node src/main.ts "问题" --repo 路径            # 提问（Node 直接运行 .ts，不需要编译）
npm test && npm run typecheck                 # 不联网的测试 + 类型检查
node src/evaluate.ts --repeat 3               # 用 evals/questions.json 评测
```
