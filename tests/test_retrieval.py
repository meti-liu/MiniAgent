"""测试 C1 检索：分词、分块、BM25 排序和沙箱、符号表，以及它们在工具定义、概览和 agent 里的开关。不联网。"""

import pytest
from test_agent import FakeLLM, answer_reply

from mini_agent import retrieval, trace
from mini_agent.agent import Settings, run
from mini_agent.tools import Tools


def test_tokenize_splits_identifiers_and_chinese():
    tokens = retrieval.tokenize("recordObservation parse_reply HTTPError 上下文压缩")
    for expected in ["recordobservation", "record", "observation", "parse_reply", "parse", "reply",
                     "httperror", "http", "error", "上下", "下文", "文压", "压缩"]:
        assert expected in tokens, expected


def test_chunks_overlap_and_cover_the_whole_file():
    assert retrieval.chunk_starts(10) == [0]  # 短文件只有一块
    starts = retrieval.chunk_starts(100)  # 40 行一块、重叠 10 行
    assert starts == [0, 30, 60]
    assert starts[-1] + retrieval.CHUNK_LINES >= 100


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "net.py").write_text(
        "def send(request):\n    for attempt in range(3):\n        retry_with_backoff(request, attempt)\n",
        encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("# 设计\n\n上下文太长时先压缩旧的工具结果。\n", encoding="utf-8")
    (tmp_path / "util.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / ".env").write_text("RETRY_TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "retry.js").write_text("retry retry retry\n", encoding="utf-8")
    return Tools(tmp_path)


def test_retrieve_ranks_relevant_chunks_first(repo):
    by_code = repo.run("retrieve", {"query": "how does retry with backoff work"})
    assert by_code.splitlines()[0] == "net.py:1-3"
    by_chinese = repo.run("retrieve", {"query": "上下文怎么压缩"})
    assert by_chinese.splitlines()[0] == "docs/notes.md:1-3"
    assert "3:" in by_chinese  # 带行号的开头几行


def test_retrieve_respects_the_sandbox(repo):
    out = repo.run("retrieve", {"query": "retry token secret"})
    assert ".env" not in out and "node_modules" not in out and "secret" not in out


def test_retrieve_has_no_result_message_and_builds_the_index_once(repo):
    assert repo.run("retrieve", {"query": "zzzz"}) == "(没有相关片段)"
    index = repo._index
    repo.run("retrieve", {"query": "add"})
    assert repo._index is index  # 第一次调用时建好，之后复用


def test_repo_map_lists_top_level_symbols_with_line_numbers(tmp_path):
    (tmp_path / "a.py").write_text("import os\n\ndef hello():\n    def inner():\n        pass\n\nclass Foo:\n    pass\n",
                                   encoding="utf-8")
    (tmp_path / "b.ts").write_text("const hidden = 1;\nexport function run() {}\nexport interface Pack {}\n"
                                   "export default class Kernel {}\nexport const LIMIT = 3;\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("# 总览\n正文\n## 细节\n#### 太深的不要\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")  # 语法错误的文件跳过，不报错
    shown = Tools(tmp_path).overview("tree", repomap=True)

    assert "【符号表" in shown
    assert "a.py: hello:3, Foo:7" in shown  # 只要顶层定义，inner 不算
    assert "b.ts: run:2, Pack:3, Kernel:4, LIMIT:5" in shown  # 只要 export 的
    assert "c.md: # 总览:1, ## 细节:3" in shown
    assert "broken.py" not in shown.split("【符号表")[1]
    assert "【符号表" not in Tools(tmp_path).overview("tree")  # 默认不加


def test_repo_map_is_capped():
    files = [(f"m{i:03}.py", "".join(f"def function_number_{j}():\n    pass\n" for j in range(30)))
             for i in range(200)]
    shown = retrieval.repo_map(files)
    assert len(shown) <= retrieval.REPOMAP_MAX_CHARS + 100
    assert "符号表已截断" in shown


def test_retrieve_tool_only_appears_when_enabled(repo):
    names = lambda specs: [s["function"]["name"] for s in specs]
    assert "retrieve" not in names(repo.specs())
    assert names(repo.specs(retrieve=True))[-1] == "retrieve"


def test_agent_wires_retrieval_settings(repo):
    bm25 = FakeLLM([answer_reply("a")])
    run("q", bm25, repo, "demo", on_step=lambda s: None, settings=Settings(retrieval="bm25"))
    assert "retrieve" in [s["function"]["name"] for s in bm25.calls[0]["tools"]]

    repomap = FakeLLM([answer_reply("a")])
    result = run("q", repomap, repo, "demo", on_step=lambda s: None, settings=Settings(retrieval="repomap"))
    assert "【符号表" in repomap.calls[0]["messages"][1]["content"]

    from test_trace import scripted_client, api_reply
    hashes = []
    for mode in ("none", "bm25"):
        llm = scripted_client(api_reply("a"))
        settings = Settings(retrieval=mode)
        result = run("q", llm, repo, "demo", on_step=lambda s: None, settings=settings)
        hashes.append(trace.build("q", "demo", llm, repo, settings, 10, result.transcript, result)["tools_hash"])
    assert hashes[0] != hashes[1]  # 工具定义变了，轨迹里的哈希能分清配置


def test_repo_map_lists_code_before_markdown_headings():
    files = [(f"docs/doc{i:02}.md", "".join(f"## 很长的文档标题第 {j} 节\n" for j in range(12))) for i in range(40)]
    files.append(("src/workflow.ts", "export class MinimalWorkflow {}\n"))
    shown = retrieval.repo_map(files)
    assert shown.splitlines()[0] == "src/workflow.ts: MinimalWorkflow:1"  # 文档排在后面，代码不会被截掉
