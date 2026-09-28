# mini-agent

最小的仓库问答 agent，用来学习 agent 怎么运转。规划见 `docs/Plan.md`。

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install pytest
export DEEPSEEK_API_KEY=...        # 自己在终端设置，不要写进仓库
python -m mini_agent "问题" --repo 路径   # 提问
python -m pytest                   # 不联网的测试
```
