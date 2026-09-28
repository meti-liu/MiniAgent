"""测试评测脚本里不联网的部分：打分规则、调用子进程读 JSON、汇总表格、题库格式。"""

import json
import re
import sys

from mini_agent.evaluate import QUESTIONS_FILE, grade, run_one, summarize

QUESTION = {"id": "q", "repo": "demo", "type": "t", "question": "问",
            "must": [["is_peak"], ["Usage\\.add", "def add"]], "must_not": ["sqlite"]}


def test_grade_needs_every_group_and_no_forbidden_words():
    assert grade("在 Usage.add 里调用 IS_PEAK", QUESTION)["passed"]  # 不区分大小写，组内任一即可
    result = grade("在 is_peak 里，用 sqlite 保存", QUESTION)
    assert result["facts"] == 1 and result["missed"] == ["Usage\\.add"]
    assert result["must_not_hits"] == ["sqlite"] and not result["passed"]


def test_run_one_reads_json_from_any_command(tmp_path):
    (tmp_path / "llm.py").write_text("a\nb\n", encoding="utf-8")
    out = {"answer": "is_peak 在 Usage.add（llm.py:1-2）", "stopped_by": "answer",
           "steps": [{"tool": "search"}, {"tool": None}],
           "usage": {"calls": 2, "cache_hit": 10, "cache_miss": 90, "cost_usd": 0.001}}
    fake_agent = [sys.executable, "-c", f"print({json.dumps(json.dumps(out))})"]  # 假装是另一种语言的 agent
    data = {"repos": {"demo": {"path": str(tmp_path)}}}
    r = run_one(fake_agent, QUESTION, 0, data, timeout=30)
    assert r["passed"] and r["model_calls"] == 2 and r["tool_calls"] == 1
    assert (r["citations"], r["bad_citations"]) == (1, 0)

    broken = [sys.executable, "-c", "import sys; sys.exit('boom')"]
    assert "error" in run_one(broken, QUESTION, 0, data, timeout=30)


def test_summarize_reports_pass_rate():
    ok = {"id": "q", "passed": True, "facts": 2, "facts_total": 2, "missed": [], "must_not_hits": [],
          "model_calls": 3, "cost_usd": 0.002, "stopped_by": "answer", "citations": 4, "bad_citations": 1}
    bad = {**ok, "passed": False, "facts": 1, "missed": ["is_peak"]}
    table = summarize([ok, bad, {"id": "q", "error": "x"}], [QUESTION])
    assert "| q | t | 1/2 | 3/4 | 3.0 | $0.0020 | is_peak |" in table
    assert "通过 1/2" in table and "失败 1 次" in table


def test_question_bank_is_well_formed():
    data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    ids = [q["id"] for q in data["questions"]]
    assert len(ids) == len(set(ids))
    for q in data["questions"]:
        assert q["repo"] in data["repos"] and q["must"]
        for pattern in [p for group in q["must"] for p in group] + q.get("must_not", []):
            re.compile(pattern)
