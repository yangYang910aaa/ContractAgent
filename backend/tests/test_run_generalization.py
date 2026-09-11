"""泛化集观察跑批单测（离线，0 API）：文件筛选 / 观察行 / 汇总口径 / dry-run。

这批发起的真实调用只在人工跑 CLI 时发生，测试一律注入假模型 + 空检索器。
"""

from __future__ import annotations

from pathlib import Path

from backend.eval.run_generalization import _row, _summarize, discover, main

_MINI_CONTRACT = (
    "货物采购合同\n"
    "甲方：某采购方　乙方：某供应商\n"
    "第一条 合同总价 100000 元。\n"
    "第二条 乙方应于签订后交付。\n"
)


class _FakeStructured:
    """假结构化输出：只统计调用次数（计数逻辑已在 test_llm_usage_pipeline 详测）。"""

    def __init__(self, model: "_FakeModel", payload) -> None:
        self._model = model
        self._payload = payload

    def invoke(self, messages):
        """模拟一次 LLM 往返并计数。"""
        self._model.calls += 1
        return self._payload


class _FakeModel:
    """假 chat 模型：抽取/复核都给最小合法 payload。"""

    def __init__(self) -> None:
        self.calls = 0

    def with_structured_output(self, schema, method=None):
        """按 schema 分派：抽取给空 dict，复核给空 findings。"""
        if schema.__name__ == "BlindReviewSchema":
            return _FakeStructured(self, {"findings": []})
        return _FakeStructured(self, {})


def _touch(dir_path: Path, names: list[str]) -> None:
    """在临时目录里造占位文件（内容不参与筛选测试）。"""
    for name in names:
        (dir_path / name).write_text(_MINI_CONTRACT, encoding="utf-8")


def test_discover_skips_scans_by_default(tmp_path: Path) -> None:
    """默认排除扫描件；--only 只留命中子串的文件。"""
    _touch(tmp_path, ["扫描件_甲_盖章版.md", "电煤购销.md", "农副购销.md"])
    names = [p.name for p in discover(tmp_path, only=[], exclude=["扫描件"], include_scans=False)]
    assert names == ["农副购销.md", "电煤购销.md"]
    only = [p.name for p in discover(tmp_path, only=["电煤"], exclude=["扫描件"], include_scans=False)]
    assert only == ["电煤购销.md"]


def test_discover_include_scans_keeps_them(tmp_path: Path) -> None:
    """--include-scans → 扫描件也进清单（留给 OCR 步骤用）。"""
    _touch(tmp_path, ["扫描件_甲_盖章版.md", "电煤购销.md"])
    names = [p.name for p in discover(tmp_path, only=[], exclude=["扫描件"], include_scans=True)]
    assert names == ["扫描件_甲_盖章版.md", "电煤购销.md"]


def test_row_captures_observation_fields(tmp_path: Path) -> None:
    """观察行包含评级/类型/调用次数/文本统计，供人工核对与落盘。"""
    path = tmp_path / "mini.md"
    path.write_text(_MINI_CONTRACT, encoding="utf-8")
    row = _row(path, "single", True, llm=_FakeModel(), retriever=lambda query: [])
    assert row["file"] == "mini.md"
    assert row["chars"] > 0
    assert row["clauses"] >= 1
    assert row["llm_calls"] == 2  # 默认双读：抽取两次
    assert isinstance(row["types"], list)


def test_summarize_counts_high_medium_and_clause_anomaly() -> None:
    """汇总口径：评级分布、high 清单、medium 频率、结构异常（有正文但切不出条款）。"""
    rows = [
        {
            "file": "a.md", "grade": "fail", "error": None,
            "medium_types": ["invoice_unclear", "invoice_unclear"], "high_types": ["penalty_rate_too_high"],
            "llm_calls": 2, "seconds": 3.0, "chars": 500, "clauses": 10,
            "risks": [
                {
                    "risk_type": "penalty_rate_too_high", "severity": "high",
                    "clause_ref": "第七条", "evidence": "违约金按合同总价 1%/日计算",
                }
            ],
        },
        {
            "file": "b.md", "grade": "pass", "error": None,
            "medium_types": ["invoice_unclear"], "high_types": [],
            "llm_calls": 2, "seconds": 2.0, "chars": 2000, "clauses": 0,
        },
        {
            "file": "c.md", "grade": None, "error": "抽取失败：超时",
            "medium_types": [], "high_types": [],
            "llm_calls": 1, "seconds": 1.0, "chars": -1, "clauses": -1,
        },
    ]
    summary = _summarize(rows)
    assert summary["grade_counts"] == {"fail": 1, "pass": 1, "error": 1}
    assert summary["errors"] == ["c.md"]
    assert summary["high_files"] == {"a.md": ["penalty_rate_too_high"]}
    # high 明细带原文证据（人工核误报就看这一条）
    assert summary["high_details"]["a.md"][0]["evidence"] == "违约金按合同总价 1%/日计算"
    assert summary["high_details"]["a.md"][0]["clause_ref"] == "第七条"
    assert summary["medium_freq"] == {"invoice_unclear": 3}
    assert summary["total_calls"] == 5
    assert summary["clause_anomalies"] == {"b.md": {"chars": 2000, "clauses": 0}}


def test_dry_run_lists_files_without_llm(tmp_path: Path, monkeypatch) -> None:
    """--dry-run 只做清单与预算：把 run_review 换成会炸的桩，验证一次调用都没发。"""
    _touch(tmp_path, ["电煤购销.md"])

    def _boom(*args, **kwargs):  # 真被调到就说明 dry-run 失效
        raise AssertionError("dry-run 不应发起任何 run_review 调用")

    monkeypatch.setattr("backend.eval.run_generalization.run_review", _boom)
    assert main(["--dir", str(tmp_path), "--dry-run"]) == 0
