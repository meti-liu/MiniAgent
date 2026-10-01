"""测试工具箱：在临时目录里建一个小仓库，验证三个工具的正常用法和安全规则。不联网。"""

import pytest

from mini_agent.tools import MAX_OUTPUT_CHARS, Tools


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("import os\ndef hello():\n    return 'hi'\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\nhello world\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("hello from git\n", encoding="utf-8")
    (root / ".env").write_text("hello=secret\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("hello outside\n", encoding="utf-8")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    return root


def test_list_dir_marks_dirs_and_skips_hidden(repo):
    out = Tools(repo).run("list_dir", {})
    assert out.splitlines() == ["README.md", "link.txt", "src/"]


def test_search_finds_lines_and_skips_git_env_and_outside(repo):
    out = Tools(repo).run("search", {"pattern": "hello"})
    assert "src/app.py:2: def hello():" in out
    assert "README.md:2: hello world" in out
    assert "git" not in out and "secret" not in out and "outside" not in out


def test_read_file_returns_numbered_range(repo):
    out = Tools(repo).run("read_file", {"path": "src/app.py", "start": 2, "end": 3})
    assert out.splitlines()[1:] == ["2: def hello():", "3:     return 'hi'"]
    assert "共 3 行" in out


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "link.txt", "src/../../outside.txt"])
def test_paths_outside_repo_are_rejected(repo, path):
    assert Tools(repo).run("read_file", {"path": path}) == "ERROR: outside repository"


def test_skipped_paths_are_rejected(repo):
    tools = Tools(repo)
    assert tools.run("read_file", {"path": ".env"}).startswith("ERROR")
    assert tools.run("list_dir", {"path": ".git"}).startswith("ERROR")


def test_search_does_not_follow_links_into_skipped_files(repo):
    (repo / "notes.txt").symlink_to(repo / ".env")
    assert "secret" not in Tools(repo).run("search", {"pattern": "secret"})


def test_skipped_names_ignore_case(repo):
    # 在 macOS 上 .GIT/config 就是 .git/config；这里直接建大写目录来模拟
    (repo / ".GIT").mkdir(exist_ok=True)  # macOS 不区分大小写时 .GIT 就是已存在的 .git
    (repo / ".GIT" / "config").write_text("x\n", encoding="utf-8")
    (repo / ".ENV.local").write_text("x\n", encoding="utf-8")
    tools = Tools(repo)
    assert tools.run("read_file", {"path": ".GIT/config"}) == "ERROR: path is skipped"
    assert tools.run("read_file", {"path": ".ENV.local"}) == "ERROR: path is skipped"


def test_long_output_is_truncated(repo):
    (repo / "big.txt").write_text(("x" * 100 + "\n") * 200, encoding="utf-8")
    out = Tools(repo).run("read_file", {"path": "big.txt"})
    assert len(out) < MAX_OUTPUT_CHARS + 100
    assert out.endswith("字符]")


def test_search_caps_hits(repo):
    (repo / "many.txt").write_text("match\n" * 60, encoding="utf-8")
    out = Tools(repo).run("search", {"pattern": "match", "path": "many.txt"})
    assert out.splitlines()[-1] == "[还有 10 条]"


def test_bad_calls_return_errors(repo):
    tools = Tools(repo)
    assert tools.run("delete_file", {}).startswith("ERROR: unknown tool")
    assert tools.run("search", {"_raw": "{oops"}).startswith("ERROR: arguments are not valid JSON")
    assert tools.run("search", {"pattern": "("}).startswith("ERROR: invalid regex")
    assert tools.run("read_file", {"file": "x"}).startswith("ERROR: bad arguments")


def test_specs_describe_three_tools(repo):
    names = [spec["function"]["name"] for spec in Tools(repo).specs()]
    assert names == ["list_dir", "search", "read_file"]


def test_overview_lists_tree_and_skips_hidden(repo):
    (repo / "src" / "deep" / "deeper" / "deepest").mkdir(parents=True)
    out = Tools(repo).overview("tree")
    assert out.splitlines()[1:] == [
        "README.md",
        "src/",
        "  app.py",
        "  deep/",
        "    deeper/",  # 第 3 层还列出，但不再往下
    ]  # link.txt 指向仓库外，.git 和 .env 被跳过


def test_overview_caps_entries_per_dir(repo):
    for i in range(25):
        (repo / "src" / f"m{i:02d}.py").write_text("", encoding="utf-8")
    lines = Tools(repo).overview("tree").splitlines()
    assert "  …(+6)" in lines  # src 里共 26 项，只列 20 项


def test_overview_none_is_empty(repo):
    assert Tools(repo).overview("none") == ""


def test_full_overview_adds_readme_and_manifest_heads(repo):
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("claude notes\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("agent notes\n", encoding="utf-8")
    out = Tools(repo).overview()
    assert "【README.md，前 2 行，共 2 行】\n# demo\nhello world" in out
    assert "【pyproject.toml，前 2 行，共 2 行】\n[project]" in out
    assert "agent notes" in out and "claude notes" not in out  # 只取 AGENTS.md
    assert "【目录树" not in Tools(repo).overview("none")
    assert "【README.md" not in Tools(repo).overview("tree")

