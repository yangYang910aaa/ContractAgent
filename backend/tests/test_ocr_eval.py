"""扫描件字段核对单测（离线，0 接口调用）：归一化口径 + 用假真值/输出来算准确率。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.eval.run_ocr_eval import _norm_amount, _norm_date, _norm_name, _outcome, evaluate


def test_norm_name_strips_space_and_punct() -> None:
    """机构名归一：OCR 在名称里插的空格/换行/括号差异不应算错。"""
    assert _norm_name("北京 北咨信息工程咨询有限公司\n") == _norm_name("北京北咨信息工程咨询有限公司")
    assert _norm_name(None) == ""


def test_norm_amount_tolerates_separators() -> None:
    """金额归一：千分位、全角逗号、￥、元 都要容忍（OCR 常把逗号读成句点也一并看清）。"""
    assert _norm_amount("￥2,175,000.00") == _norm_amount("2175000.00")
    assert _norm_amount("155，000.00元") == _norm_amount("155000")
    assert _norm_amount("不是数字") is None


def test_norm_date_accepts_cn_and_iso() -> None:
    """日期归一：2025年11月18日 / 2025-11-18 / date 对象 视为同一值。"""
    assert _norm_date("2025年11月18日") == "2025-11-18"
    assert _norm_date("2025-9-7") == "2025-09-07"


def test_outcome_branches() -> None:
    """四类判定：命中 / 缺抽 / 数值不符 / 名称不符。"""
    assert _outcome("buyer", "某医院", "某医院") == "ok"
    assert _outcome("supplier", "某公司", None) == "missing"
    assert _outcome("total_amount", "1000", "2000") == "wrong"
    assert _outcome("signature_date", "2025-09-07", "2025-09-08") == "wrong"


def test_evaluate_scores_only_declared_fields(tmp_path: Path) -> None:
    """只对 GT 写了的字段评分：没写的字段（如原文无日期栏）跳过，不算错。"""
    gt = {
        "files": [
            {"file": "a.pdf", "buyer": "某医院", "total_amount": "1,000.00"},
            {"file": "b.pdf", "supplier": "某公司"},
        ]
    }
    run = {
        "files": [
            {"file": "a.pdf", "grade": "pass", "extracted": {"buyer": "某医院", "total_amount": "1000"}},
            {"file": "b.pdf", "grade": "fail", "extracted": {"supplier": "别的公司"}},
        ]
    }
    gt_path = tmp_path / "gt.json"
    run_path = tmp_path / "run.json"
    gt_path.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    run_path.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")

    result = evaluate(gt_path, run_path)
    assert result["counters"] == {"ok": 2, "wrong": 1, "missing": 0}
    assert result["field_accuracy"] == round(2 / 3, 4)


def test_evaluate_reports_missing_file(tmp_path: Path) -> None:
    """输出里没有该文件（未跑/改名）→ 明确报错而不是静默算 0 分。"""
    gt_path = tmp_path / "gt.json"
    run_path = tmp_path / "run.json"
    gt_path.write_text(json.dumps({"files": [{"file": "x.pdf", "buyer": "某"}]}, ensure_ascii=False), encoding="utf-8")
    run_path.write_text(json.dumps({"files": []}, ensure_ascii=False), encoding="utf-8")
    result = evaluate(gt_path, run_path)
    assert result["files"][0]["error"]
    assert result["field_accuracy"] is None
