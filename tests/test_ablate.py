"""测试消融实验里不联网的部分：引用核对和汇总表格。"""

from mini_agent.ablate import check_citations, summarize
from mini_agent.tools import Tools


def test_check_citations_counts_missing_files_and_out_of_range_lines(tmp_path):
    (tmp_path / "a.py").write_text("1\n2\n3\n", encoding="utf-8")
    answer = "见 `a.py:1-2`、`a.py:5`、`missing.py:1`，以及中文紧挨着a.py:2"
    assert check_citations(answer, Tools(tmp_path)) == (4, 2)  # a.py:5 越界，missing.py 不存在
    # 简写按同一回答里的完整路径补全；对不上唯一路径的简写仍算无法核实
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("1\n2\n", encoding="utf-8")
    assert check_citations("见 src/b.py:1，后面又说 b.py:2 和 c.py:1", Tools(tmp_path)) == (3, 1)


def test_summarize_averages_per_config():
    base = {"stopped_by": "answer", "model_calls": 3, "tool_calls": 4, "list_dir": 1, "repeated": 0,
            "input_tokens": 1000, "cache_hit": 500, "cost_usd": 0.001, "citations": 5, "bad_citations": 0}
    records = [{**base, "config": "full"}, {**base, "config": "full", "model_calls": 5},
               {"config": "no_dedup", "error": "HTTP 500"}]
    table = summarize(records)
    assert "| full | 2 | 4.0 |" in table and "50%" in table
    assert "no_dedup" not in table and "失败 1 次" in table
