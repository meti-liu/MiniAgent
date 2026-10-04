"""检索：给 agent 的两种“找代码”的辅助（对应 M1 ContextEngine 规范第 12 节的混合检索，规划 12.6）。

BM25：把仓库切成 40 行一块建关键词索引，retrieve 工具按自然语言查询返回最相关的几块（用 BM25 代替 embedding，只用标准库）。
符号表（repo map）：列出每个文件的顶层定义和行号，放进仓库概览，模型不用一层层 list_dir 也知道东西大概在哪。
"""

from __future__ import annotations

import ast
import math
import re
from collections import Counter

CHUNK_LINES = 40
CHUNK_OVERLAP = 10
TOP_K = 8
PREVIEW_LINES = 6
K1, B = 1.2, 0.75  # BM25 的两个常用参数：词频饱和速度、按块长度归一化的程度
REPOMAP_MAX_CHARS = 8_000
MD_MAX_HEADINGS = 12  # 一个 Markdown 文件最多列几个标题

WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[一-鿿]+")
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
TS_SYMBOL = re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(?:abstract\s+)?"
                       r"(?:function|class|interface|type|const|enum)\s+([A-Za-z_$][\w$]*)")
MD_HEADING = re.compile(r"^(#{1,3})\s+(.+)")


def tokenize(text: str) -> list[str]:
    """英文标识符保留整体，再按 snake_case 和 camelCase 拆开（都转小写）；中文按相邻两个字切。"""
    tokens = []
    for word in WORD.findall(text):
        if "一" <= word[0] <= "鿿":
            tokens += [word[i:i + 2] for i in range(len(word) - 1)] or [word]
            continue
        tokens.append(word.lower())
        parts = [p.lower() for piece in word.split("_") for p in CAMEL.findall(piece)]
        if len(parts) > 1:
            tokens += parts
    return tokens


def chunk_starts(line_count: int) -> list[int]:
    """每块的起始下标（从 0 开始）：40 行一块，相邻两块重叠 10 行，最后一块覆盖到文件末尾。"""
    if line_count <= CHUNK_LINES:
        return [0]
    return list(range(0, line_count - CHUNK_OVERLAP, CHUNK_LINES - CHUNK_OVERLAP))


class BM25Index:
    def __init__(self, files):
        """files 是 (相对路径, 文本) 的序列。"""
        self.chunks: list[tuple[str, int, int, list[str]]] = []  # 路径、起始行、结束行、这几行
        self.freqs: list[Counter] = []
        doc_freq: Counter = Counter()
        for path, text in files:
            lines = text.splitlines()
            for start in chunk_starts(len(lines)):
                body = lines[start:start + CHUNK_LINES]
                # 路径也算进这一块：文件名常常就是最好的线索
                freq = Counter(tokenize(path + "\n" + "\n".join(body)))
                self.chunks.append((path, start + 1, start + len(body), body))
                self.freqs.append(freq)
                doc_freq.update(freq.keys())
        n = len(self.chunks)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in doc_freq.items()}
        self.lengths = [sum(f.values()) for f in self.freqs]
        self.avg_length = sum(self.lengths) / n if n else 0.0

    def search(self, query: str, k: int = TOP_K) -> list[tuple[str, int, int, list[str]]]:
        terms = set(tokenize(query))
        scored = []
        for i, freq in enumerate(self.freqs):
            norm = K1 * (1 - B + B * self.lengths[i] / self.avg_length)
            score = sum(self.idf[t] * freq[t] * (K1 + 1) / (freq[t] + norm) for t in terms if freq[t])
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda item: -item[0])
        return [self.chunks[i] for _, i in scored[:k]]


def format_hits(hits) -> str:
    """每块一行“路径:起始行-结束行”，下面是带行号的开头几行。"""
    lines = []
    for path, start, end, body in hits:
        lines.append(f"{path}:{start}-{end}")
        lines += [f"  {start + i}: {text.strip()}" for i, text in enumerate(body[:PREVIEW_LINES])]
    return "\n".join(lines) or "(没有相关片段)"


def symbols(path: str, text: str) -> list[tuple[str, int]]:
    """一个文件的顶层定义和行号：Python 用 ast，TS/JS 只取 export，Markdown 取一到三级标题。其他文件为空。"""
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        return [(node.name, node.lineno) for node in tree.body if isinstance(node, kinds)]
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if path.endswith((".ts", ".tsx", ".js", ".mjs")):
            match = TS_SYMBOL.match(line)
            if match:
                found.append((match.group(1), number))
        elif path.endswith(".md"):
            match = MD_HEADING.match(line)
            if match:
                found.append((f"{match.group(1)} {match.group(2).strip()}", number))
    return found[:MD_MAX_HEADINGS] if path.endswith(".md") else found


def repo_map(files) -> str:
    """每个有定义的文件一行：“路径: 名字:行号, ...”，总长度不超过 REPOMAP_MAX_CHARS。
    代码文件排在前面、Markdown 标题排在后面：按路径字母序时 docs/ 会先把篇幅占满，代码反而被截掉。"""
    lines, used, listed = [], 0, 0
    entries = [(path, symbols(path, text)) for path, text in files]
    entries = sorted(((p, s) for p, s in entries if s), key=lambda e: (e[0].endswith(".md"), e[0]))
    for path, found in entries:
        line = f"{path}: " + ", ".join(f"{name}:{number}" for name, number in found)
        if used + len(line) > REPOMAP_MAX_CHARS:
            lines.append(f"[符号表已截断：只列了 {listed}/{len(entries)} 个文件，其余用 search 或 list_dir 查找]")
            break
        lines.append(line)
        used += len(line) + 1
        listed += 1
    return "\n".join(lines)
