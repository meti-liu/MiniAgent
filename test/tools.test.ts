/** 测试工具箱：在临时目录里建一个小仓库，验证三个工具的正常用法和安全规则。不联网。 */

import assert from "node:assert/strict";
import { mkdirSync, symlinkSync } from "node:fs";
import path from "node:path";
import { beforeEach, test } from "node:test";
import { MAX_OUTPUT_CHARS, Tools } from "../src/tools.ts";
import { tempDir, write } from "./helpers.ts";

let repo: string;

beforeEach(() => {
  const tmp = tempDir();
  repo = path.join(tmp, "repo");
  mkdirSync(path.join(repo, "src"), { recursive: true });
  write(path.join(repo, "src", "app.py"), "import os\ndef hello():\n    return 'hi'\n");
  write(path.join(repo, "README.md"), "# demo\nhello world\n");
  mkdirSync(path.join(repo, ".git"));
  write(path.join(repo, ".git", "config"), "hello from git\n");
  write(path.join(repo, ".env"), "hello=secret\n");
  write(path.join(tmp, "outside.txt"), "hello outside\n");
  symlinkSync(path.join(tmp, "outside.txt"), path.join(repo, "link.txt"));
});

test("list_dir marks dirs and skips hidden", () => {
  assert.deepEqual(new Tools(repo).run("list_dir", {}).split("\n"), ["README.md", "link.txt", "src/"]);
});

test("search finds lines and skips .git, .env and outside", () => {
  const out = new Tools(repo).run("search", { pattern: "hello" });
  assert.ok(out.includes("src/app.py:2: def hello():"));
  assert.ok(out.includes("README.md:2: hello world"));
  assert.ok(!out.includes("git") && !out.includes("secret") && !out.includes("outside"));
});

test("search accepts a Python-style (?i) prefix", () => {
  assert.ok(new Tools(repo).run("search", { pattern: "(?i)HELLO WORLD" }).includes("README.md:2"));
});

test("read_file returns a numbered range", () => {
  const out = new Tools(repo).run("read_file", { path: "src/app.py", start: 2, end: 3 });
  assert.deepEqual(out.split("\n").slice(1), ["2: def hello():", "3:     return 'hi'"]);
  assert.ok(out.includes("共 3 行"));
});

for (const p of ["../outside.txt", "/etc/passwd", "link.txt", "src/../../outside.txt"]) {
  test(`path outside repo is rejected: ${p}`, () => {
    assert.equal(new Tools(repo).run("read_file", { path: p }), "ERROR: outside repository");
  });
}

test("skipped paths are rejected, ignoring case", () => {
  // macOS 默认不区分大小写，.GIT 就是已经存在的 .git，所以允许目录已存在
  mkdirSync(path.join(repo, ".GIT"), { recursive: true });
  write(path.join(repo, ".GIT", "config"), "x\n");
  write(path.join(repo, ".ENV.local"), "x\n");
  const tools = new Tools(repo);
  assert.ok(tools.run("read_file", { path: ".env" }).startsWith("ERROR"));
  assert.ok(tools.run("list_dir", { path: ".git" }).startsWith("ERROR"));
  assert.equal(tools.run("read_file", { path: ".GIT/config" }), "ERROR: path is skipped");
  assert.equal(tools.run("read_file", { path: ".ENV.local" }), "ERROR: path is skipped");
});

test("search does not follow links into skipped files", () => {
  symlinkSync(path.join(repo, ".env"), path.join(repo, "notes.txt"));
  assert.ok(!new Tools(repo).run("search", { pattern: "secret" }).includes("secret"));
});

test("long output is truncated", () => {
  write(path.join(repo, "big.txt"), `${"x".repeat(100)}\n`.repeat(200));
  const out = new Tools(repo).run("read_file", { path: "big.txt" });
  assert.ok(out.length < MAX_OUTPUT_CHARS + 100);
  assert.ok(out.endsWith("字符]"));
});

test("search caps hits", () => {
  write(path.join(repo, "many.txt"), "match\n".repeat(60));
  const out = new Tools(repo).run("search", { pattern: "match", path: "many.txt" });
  assert.equal(out.split("\n").at(-1), "[还有 10 条]");
});

test("bad calls return errors", () => {
  const tools = new Tools(repo);
  assert.ok(tools.run("delete_file", {}).startsWith("ERROR: unknown tool"));
  assert.ok(tools.run("search", { _raw: "{oops" }).startsWith("ERROR: arguments are not valid JSON"));
  assert.ok(tools.run("search", { pattern: "(" }).startsWith("ERROR: invalid regex"));
  assert.ok(tools.run("read_file", { file: "x" }).startsWith("ERROR: bad arguments"));
});

test("specs describe three tools", () => {
  const names = new Tools(repo).specs().map((s: any) => s.function.name);
  assert.deepEqual(names, ["list_dir", "search", "read_file"]);
});

test("overview tree lists entries up to depth 3", () => {
  mkdirSync(path.join(repo, "src", "deep", "deeper", "deepest"), { recursive: true });
  const lines = new Tools(repo).overview("tree").split("\n").slice(1);
  assert.deepEqual(lines, ["README.md", "src/", "  app.py", "  deep/", "    deeper/"]); // link.txt 指向仓库外
});

test("overview caps entries per directory", () => {
  for (let i = 0; i < 25; i++) write(path.join(repo, "src", `m${String(i).padStart(2, "0")}.py`), "");
  assert.ok(new Tools(repo).overview("tree").split("\n").includes("  …(+6)"));
});

test("full overview adds README, agent notes and manifest heads", () => {
  write(path.join(repo, "pyproject.toml"), "[project]\nname = 'demo'\n");
  write(path.join(repo, "CLAUDE.md"), "claude notes\n");
  write(path.join(repo, "AGENTS.md"), "agent notes\n");
  const tools = new Tools(repo);
  const out = tools.overview();
  assert.ok(out.includes("【README.md，前 2 行，共 2 行】\n# demo\nhello world"));
  assert.ok(out.includes("【pyproject.toml，前 2 行，共 2 行】\n[project]"));
  assert.ok(out.includes("agent notes") && !out.includes("claude notes"));
  assert.equal(tools.overview("none"), "");
  assert.ok(!tools.overview("tree").includes("【README.md"));
});
