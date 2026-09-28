"""工具箱：agent 的“手”（对应 M1 的 AgentToolPool + 只读 Executor）。

三个只读工具 list_dir / search / read_file，结果都是字符串，直接作为 tool 消息喂回模型。
出错时返回 "ERROR: ..." 而不是抛异常，让模型看到错误后自己调整；所有路径都被限制在仓库目录内。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SKIP_NAMES = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", "dist"}
MAX_FILE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 12_000
MAX_ENTRIES = 200
MAX_HITS = 50
MAX_LINES = 200
OVERVIEW_DEPTH = 3
OVERVIEW_PER_DIR = 20
OVERVIEW_MAX_CHARS = 4_000


class ToolError(Exception):
    """工具内部的可预期错误，run() 会把它变成 "ERROR: ..." 字符串。"""


def is_skipped(name: str) -> bool:
    # 按小写比较：macOS 默认不区分大小写，.GIT 和 .git 是同一个目录
    name = name.lower()
    return name in SKIP_NAMES or name.startswith(".env")


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[输出已截断，共 {len(text)} 字符]"


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """生成一个 OpenAI tools 格式的工具定义。"""
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


class Tools:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def specs(self) -> list[dict]:
        path = {"type": "string", "description": "相对仓库根目录的路径，默认 ."}
        return [
            _function("list_dir", "列出目录下的文件和子目录（目录以 / 结尾）。用来了解仓库结构。",
                      {"path": path}, []),
            _function("search", "在文件内容里按正则搜索，返回 路径:行号: 行内容。定位代码时优先用它。",
                      {"pattern": {"type": "string", "description": "Python 正则表达式"}, "path": path},
                      ["pattern"]),
            _function("read_file", f"读取文件的指定行，带行号，一次最多 {MAX_LINES} 行。先搜索定位，再读需要的几行。",
                      {"path": {"type": "string", "description": "相对仓库根目录的文件路径"},
                       "start": {"type": "integer", "description": "起始行，从 1 开始，默认 1"},
                       "end": {"type": "integer", "description": "结束行（包含），默认 start+199"}},
                      ["path"]),
        ]

    def run(self, name: str, arguments: dict) -> str:
        handlers = {"list_dir": self.list_dir, "search": self.search, "read_file": self.read_file}
        if name not in handlers:
            return f"ERROR: unknown tool {name!r}"
        if "_raw" in arguments:
            return f"ERROR: arguments are not valid JSON: {arguments['_raw']!r}"
        try:
            return truncate(handlers[name](**arguments))
        except TypeError as error:  # 参数名不对，比如多传或漏传
            return f"ERROR: bad arguments: {error}"
        except ToolError as error:
            return f"ERROR: {error}"

    # ---- 安全检查 ----

    def _resolve(self, path: str) -> Path:
        """把相对路径变成绝对路径；resolve() 会展开 .. 和符号链接，所以一次检查就够。"""
        target = (self.root / path).resolve()
        self._check(target)
        return target

    def _check(self, target: Path) -> None:
        """target 必须已经 resolve 过：要在仓库内，且路径里没有要跳过的名字。"""
        if target != self.root and self.root not in target.parents:
            raise ToolError("outside repository")
        if any(is_skipped(part) for part in target.relative_to(self.root).parts):
            raise ToolError("path is skipped")

    def _read_text(self, file: Path) -> str:
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"file larger than {MAX_FILE_BYTES} bytes")
        try:
            return file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError("file is not UTF-8 text") from None

    def _files_under(self, target: Path):
        """遍历 target 下所有允许读的文件；符号链接按真实位置再检查一遍。"""
        if target.is_file():
            yield target
            return
        for folder, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if not is_skipped(d))  # 原地修改 = 不进入这些目录
            for filename in sorted(filenames):
                file = Path(folder) / filename
                try:
                    self._check(file.resolve())  # 和 _resolve 用同一套规则
                except ToolError:
                    continue
                yield file

    # ---- 三个工具 ----

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        names = [p.name + ("/" if p.is_dir() else "")
                 for p in sorted(target.iterdir()) if not is_skipped(p.name)]
        lines = names[:MAX_ENTRIES]
        if len(names) > MAX_ENTRIES:
            lines.append(f"[还有 {len(names) - MAX_ENTRIES} 条]")
        return "\n".join(lines) or "(空目录)"

    def search(self, pattern: str, path: str = ".") -> str:
        try:
            regex = re.compile(pattern)
        except re.error as error:
            raise ToolError(f"invalid regex: {error}") from None
        hits = []
        for file in self._files_under(self._resolve(path)):
            try:
                text = self._read_text(file)
            except ToolError:
                continue  # 搜索时安静地跳过大文件和二进制文件
            relative = file.relative_to(self.root).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{relative}:{number}: {line.strip()}")
        lines = hits[:MAX_HITS]
        if len(hits) > MAX_HITS:
            lines.append(f"[还有 {len(hits) - MAX_HITS} 条]")
        return "\n".join(lines) or "(没有匹配)"

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        start = max(1, int(start))
        end = start + MAX_LINES - 1 if end is None else min(int(end), start + MAX_LINES - 1)
        lines = self._read_text(target).splitlines()
        body = [f"{n}: {lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1)]
        header = f"{target.relative_to(self.root).as_posix()}（第 {start}-{min(end, len(lines))} 行，共 {len(lines)} 行）"
        return "\n".join([header, *body])

    # ---- 仓库概览：运行开始时放进第一条用户消息，不在 specs() 里，模型不能调用 ----

    def overview(self, mode: str = "tree") -> str:
        """对应 M1 的 ORIENT。mode="none" 返回空字符串，只用于消融实验。"""
        return "" if mode == "none" else self._tree()

    def _tree(self) -> str:
        """深度不超过 OVERVIEW_DEPTH 的目录树。"""
        lines: list[str] = []

        def walk(folder: Path, depth: int) -> None:
            entries = [p for p in sorted(folder.iterdir()) if not is_skipped(p.name)]
            for entry in entries[:OVERVIEW_PER_DIR]:
                try:
                    self._check(entry.resolve())  # 指向仓库外或被跳过的位置，就不列出
                except ToolError:
                    continue
                is_dir = entry.is_dir()
                lines.append("  " * depth + entry.name + ("/" if is_dir else ""))
                if is_dir and depth + 1 < OVERVIEW_DEPTH:
                    walk(entry, depth + 1)
            if len(entries) > OVERVIEW_PER_DIR:
                lines.append("  " * depth + f"…(+{len(entries) - OVERVIEW_PER_DIR})")

        walk(self.root, 0)
        text = "\n".join(lines)
        if len(text) > OVERVIEW_MAX_CHARS:
            text = text[:OVERVIEW_MAX_CHARS] + "\n[概览已截断，其余部分请用 list_dir 查看]"
        return text
