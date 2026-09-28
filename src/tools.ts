/**
 * 工具箱：agent 的“手”（对应 M1 的 AgentToolPool + 只读 Executor）。
 *
 * 三个只读工具 list_dir / search / read_file，结果都是字符串，直接作为 tool 消息喂回模型。
 * 出错时返回 "ERROR: ..." 而不是抛异常，让模型看到错误后自己调整；所有路径都被限制在仓库目录内。
 * overview() 不是工具，运行开始时放进第一条用户消息（对应 M1 的 ORIENT）。
 */

import { readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import path from "node:path";

const SKIP_NAMES = new Set([".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", "dist"]);
const MAX_FILE_BYTES = 1_000_000;
export const MAX_OUTPUT_CHARS = 12_000;
const MAX_ENTRIES = 200;
const MAX_HITS = 50;
const MAX_LINES = 200;
const OVERVIEW_DEPTH = 3;
const OVERVIEW_PER_DIR = 20;
const OVERVIEW_MAX_CHARS = 4_000;
const OVERVIEW_SECTION_CHARS = 3_000;
// 概览里附上的根目录文件及行数；AGENTS.md 和 CLAUDE.md 只取其一
const OVERVIEW_FILES: ReadonlyArray<readonly [string, number]> = [
  ["AGENTS.md", 40], ["CLAUDE.md", 40], ["README.md", 60], ["pyproject.toml", 40],
  ["package.json", 40], ["go.mod", 40], ["Cargo.toml", 40],
];

/** 工具内部的可预期错误，run() 会把它变成 "ERROR: ..." 字符串。 */
export class ToolError extends Error {}

export function isSkipped(name: string): boolean {
  // 按小写比较：macOS 默认不区分大小写，.GIT 和 .git 是同一个目录
  const lower = name.toLowerCase();
  return SKIP_NAMES.has(lower) || lower.startsWith(".env");
}

export function truncate(text: string): string {
  if (text.length <= MAX_OUTPUT_CHARS) return text;
  return `${text.slice(0, MAX_OUTPUT_CHARS)}\n[输出已截断，共 ${text.length} 字符]`;
}

/** 和 Python 的 str.splitlines() 一样：末尾的换行不产生空行。 */
export function splitLines(text: string): string[] {
  const lines = text.split(/\r\n|\n|\r/);
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

/** 生成一个 OpenAI tools 格式的工具定义。 */
function fn(name: string, description: string, properties: object, required: string[]): object {
  return { type: "function", function: { name, description, parameters: { type: "object", properties, required } } };
}

type Args = Record<string, unknown>;

// 每个工具允许的参数和必填参数，和 specs() 保持一致
const PARAMS: Record<string, readonly [string[], string[]]> = {
  list_dir: [["path"], []],
  search: [["pattern", "path"], ["pattern"]],
  read_file: [["path", "start", "end"], ["path"]],
};

export class Tools {
  readonly root: string;

  constructor(root: string) {
    this.root = realpathSync(path.resolve(root));
  }

  specs(): object[] {
    const p = { type: "string", description: "相对仓库根目录的路径，默认 ." };
    return [
      fn("list_dir", "列出目录下的文件和子目录（目录以 / 结尾）。用来了解仓库结构。", { path: p }, []),
      fn("search", "在文件内容里按正则搜索，返回 路径:行号: 行内容。定位代码时优先用它。",
        { pattern: { type: "string", description: "JavaScript 正则表达式" }, path: p }, ["pattern"]),
      fn("read_file", `读取文件的指定行，带行号，一次最多 ${MAX_LINES} 行。先搜索定位，再读需要的几行。`,
        {
          path: { type: "string", description: "相对仓库根目录的文件路径" },
          start: { type: "integer", description: "起始行，从 1 开始，默认 1" },
          end: { type: "integer", description: "结束行（包含），默认 start+199" },
        }, ["path"]),
    ];
  }

  run(name: string, args: Args): string {
    const handlers: Record<string, (a: Args) => string> = {
      list_dir: (a) => this.listDir(a),
      search: (a) => this.search(a),
      read_file: (a) => this.readFile(a),
    };
    const handler = handlers[name];
    if (!handler) return `ERROR: unknown tool ${JSON.stringify(name)}`;
    if ("_raw" in args) return `ERROR: arguments are not valid JSON: ${JSON.stringify(args._raw)}`;
    // Python 版靠 **arguments 调用时的 TypeError 发现参数名写错；JS 不会报错，所以这里显式检查
    const [allowed, required] = PARAMS[name]!;
    const unknown = Object.keys(args).filter((key) => !allowed.includes(key));
    const missing = required.filter((key) => !(key in args));
    if (unknown.length || missing.length) {
      return `ERROR: bad arguments: unknown ${JSON.stringify(unknown)}, missing ${JSON.stringify(missing)}`;
    }
    try {
      return truncate(handler(args));
    } catch (error) {
      if (error instanceof ToolError) return `ERROR: ${error.message}`;
      throw error;
    }
  }

  // ---- 安全检查 ----

  /** 把相对路径变成真实的绝对路径；realpath 会展开 .. 和符号链接，所以一次检查就够。 */
  resolve(relative: string): string {
    const target = this.real(path.resolve(this.root, relative)); // 绝对路径会直接替换掉 root
    this.check(target);
    return target;
  }

  /** 文件不存在时 realpath 会抛错，这时退回到只做字面规范化的路径（反正也读不到）。 */
  private real(p: string): string {
    try {
      return realpathSync(p);
    } catch {
      return p;
    }
  }

  /** target 必须已经是真实路径：要在仓库内，且路径里没有要跳过的名字。 */
  private check(target: string): void {
    // 用 root + 分隔符比较，避免把隔壁的 mini-agent-backup 误判成在仓库里
    if (target !== this.root && !target.startsWith(this.root + path.sep)) {
      throw new ToolError("outside repository");
    }
    if (path.relative(this.root, target).split(path.sep).some(isSkipped)) {
      throw new ToolError("path is skipped");
    }
  }

  private display(file: string): string {
    return path.relative(this.root, file).split(path.sep).join("/");
  }

  readText(file: string): string {
    if (statSync(file).size > MAX_FILE_BYTES) throw new ToolError(`file larger than ${MAX_FILE_BYTES} bytes`);
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(readFileSync(file));
    } catch {
      throw new ToolError("file is not UTF-8 text");
    }
  }

  /** 遍历 target 下所有允许读的文件；符号链接按真实位置再检查一遍，不进入指向目录的链接。 */
  private *filesUnder(target: string): Generator<string> {
    if (statSync(target).isFile()) {
      yield target;
      return;
    }
    const entries = readdirSync(target, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1));
    const dirs: string[] = [];
    for (const entry of entries) {
      const full = path.join(target, entry.name);
      if (entry.isDirectory()) {
        if (!isSkipped(entry.name)) dirs.push(full); // 和 os.walk 一样：先文件，后子目录
        continue;
      }
      try {
        const real = realpathSync(full);
        this.check(real); // 和 resolve() 用同一套规则
        if (statSync(real).isFile()) yield full;
      } catch {
        continue; // 越界、被跳过、坏链接
      }
    }
    for (const dir of dirs) yield* this.filesUnder(dir);
  }

  // ---- 三个工具 ----

  private listDir(args: Args): string {
    const rel = String(args.path ?? ".");
    const target = this.resolve(rel);
    if (!this.isDir(target)) throw new ToolError(`not a directory: ${rel}`);
    const names = readdirSync(target)
      .sort()
      .filter((name) => !isSkipped(name))
      .map((name) => name + (this.isDir(path.join(target, name)) ? "/" : ""));
    const lines = names.slice(0, MAX_ENTRIES);
    if (names.length > MAX_ENTRIES) lines.push(`[还有 ${names.length - MAX_ENTRIES} 条]`);
    return lines.join("\n") || "(空目录)";
  }

  private search(args: Args): string {
    let pattern = String(args.pattern ?? "");
    let flags = "";
    if (pattern.startsWith("(?i)")) {
      // 模型常写 Python 风格的 (?i)，JS 正则不支持，这里转成 i 标志
      pattern = pattern.slice(4);
      flags = "i";
    }
    let regex: RegExp;
    try {
      regex = new RegExp(pattern, flags);
    } catch (error) {
      throw new ToolError(`invalid regex: ${(error as Error).message}`);
    }
    const hits: string[] = [];
    for (const file of this.filesUnder(this.resolve(String(args.path ?? ".")))) {
      let text: string;
      try {
        text = this.readText(file);
      } catch {
        continue; // 搜索时安静地跳过大文件和二进制文件
      }
      splitLines(text).forEach((line, i) => {
        if (regex.test(line)) hits.push(`${this.display(file)}:${i + 1}: ${line.trim()}`);
      });
    }
    const lines = hits.slice(0, MAX_HITS);
    if (hits.length > MAX_HITS) lines.push(`[还有 ${hits.length - MAX_HITS} 条]`);
    return lines.join("\n") || "(没有匹配)";
  }

  private readFile(args: Args): string {
    const rel = String(args.path ?? "");
    const target = this.resolve(rel);
    if (!this.isFile(target)) throw new ToolError(`not a file: ${rel}`);
    const start = Math.max(1, Math.trunc(Number(args.start ?? 1)) || 1);
    const end = args.end == null
      ? start + MAX_LINES - 1
      : Math.min(Math.trunc(Number(args.end)), start + MAX_LINES - 1);
    const lines = splitLines(this.readText(target));
    const last = Math.min(end, lines.length);
    const body: string[] = [];
    for (let n = start; n <= last; n++) body.push(`${n}: ${lines[n - 1]}`);
    return [`${this.display(target)}（第 ${start}-${last} 行，共 ${lines.length} 行）`, ...body].join("\n");
  }

  private isDir(p: string): boolean {
    try {
      return statSync(p).isDirectory();
    } catch {
      return false;
    }
  }

  private isFile(p: string): boolean {
    try {
      return statSync(p).isFile();
    } catch {
      return false;
    }
  }

  // ---- 仓库概览：不在 specs() 里，模型不能调用 ----

  /** 对应 M1 的 ORIENT。full = 目录树 + 根目录说明和清单文件开头；tree = 只有目录树；none = 空。 */
  overview(mode: "full" | "tree" | "none" = "full"): string {
    if (mode === "none") return "";
    const sections = [`【目录树，深度 ≤${OVERVIEW_DEPTH}，每个目录最多 ${OVERVIEW_PER_DIR} 项】\n${this.tree()}`];
    if (mode === "full") sections.push(...this.headSections());
    return sections.join("\n\n");
  }

  private tree(): string {
    const lines: string[] = [];
    const walk = (folder: string, depth: number): void => {
      const entries = readdirSync(folder).sort().filter((name) => !isSkipped(name));
      for (const name of entries.slice(0, OVERVIEW_PER_DIR)) {
        const full = path.join(folder, name);
        try {
          this.check(realpathSync(full)); // 指向仓库外或被跳过的位置，就不列出
        } catch {
          continue;
        }
        const dir = this.isDir(full);
        lines.push("  ".repeat(depth) + name + (dir ? "/" : ""));
        if (dir && depth + 1 < OVERVIEW_DEPTH) walk(full, depth + 1);
      }
      if (entries.length > OVERVIEW_PER_DIR) lines.push(`${"  ".repeat(depth)}…(+${entries.length - OVERVIEW_PER_DIR})`);
    };
    walk(this.root, 0);
    const text = lines.join("\n");
    return text.length > OVERVIEW_MAX_CHARS
      ? `${text.slice(0, OVERVIEW_MAX_CHARS)}\n[概览已截断，其余部分请用 list_dir 查看]`
      : text;
  }

  private headSections(): string[] {
    const sections: string[] = [];
    for (const [name, maxLines] of OVERVIEW_FILES) {
      if (name === "CLAUDE.md" && this.isFile(path.join(this.root, "AGENTS.md"))) continue;
      let lines: string[];
      try {
        const target = this.resolve(name);
        if (!this.isFile(target)) continue;
        lines = splitLines(this.readText(target));
      } catch {
        continue; // 被跳过、太大或不是文本
      }
      const body = lines.slice(0, maxLines).join("\n").slice(0, OVERVIEW_SECTION_CHARS);
      const shown = Math.min(maxLines, lines.length);
      // 用【】而不是 ##，避免和 README 里自己的标题混在一起
      sections.push(`【${name}，前 ${shown} 行，共 ${lines.length} 行】\n${body}`);
    }
    return sections;
  }
}
