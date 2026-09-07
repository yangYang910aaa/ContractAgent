"""Phase 4 评测 runner 离线单测: 指标口径 + GT 与 rules 一致性。

不调 LLM(纯函数 + 读本地 GT JSON); 与既有测试一致, 本地语料不入库。
"""

from pathlib import Path

from backend.app.config import BASE_DIR
from backend.eval.run_eval import (
    DEFAULT_GT,
    _collapse,
    _load_json,
    _resolve,
    _run_level_metrics,
    _validate,
)


def _entry(**kwargs):
    """最小 GtEntry 工厂(仅构造判分所需字段)。"""
    from backend.eval.run_eval import GtEntry

    defaults = dict(
        file="x.md",
        set_="samples",
        kind="enterprise_goods",
        expected_grade="fail",
        expected_types={},
        judge=True,
    )
    defaults.update(kwargs)
    return GtEntry(**defaults)


def _obs(grade: str, high_types: set[str]) -> dict:
    return {"grade": grade, "high_types": high_types, "error": None}


def test_gt_file_consistent_with_rules() -> None:
    """GT 全条目: 文件在位、类型都已在 rules 登记、评级/严重级取值合法。"""
    assert DEFAULT_GT.is_file()
    entries = _load_json(DEFAULT_GT)
    for entry in entries:
        entry.path = _resolve(entry)
    assert len(entries) >= 21, "评测语料至少 12 变体 + 9 sample"
    assert _validate(entries) == [], "GT 校验应零问题"
    # 每个 set 都有缺陷/正常/模板类, 防语料被误删只剩单类
    sets = {entry.set_ for entry in entries}
    assert sets == {"variants", "samples"}
    defect = [e for e in entries if e.expected_highs]
    clean = [e for e in entries if e.judge and not e.expected_highs]
    assert defect and clean, "GT 应同时含缺陷样本(检出)与干净样本(零误报)"


def test_metrics_perfect_run_all_ones() -> None:
    """全命中运行: 检出/零误报/macro-F1/评级准确率都应为 1。"""
    entries = [
        _entry(expected_types={"penalty_rate_too_high": "high", "warranty_too_short": "high"}),
        _entry(expected_types={"amount_inconsistency": "high"}),
        _entry(expected_grade="pass"),
    ]
    observed = [
        _obs("fail", {"penalty_rate_too_high", "warranty_too_short"}),
        _obs("fail", {"amount_inconsistency"}),
        _obs("pass", set()),
    ]
    m = _run_level_metrics(entries, observed)
    assert m["detection_rate"] == 1.0
    assert m["zero_fp_rate"] == 1.0
    assert m["macro_f1"] == 1.0
    assert m["grade_accuracy"] == 1.0


def test_metrics_count_miss_and_fp() -> None:
    """漏检/误报/评级错都要如实扣分(按手算期望值核对口径)。"""
    entries = [
        _entry(expected_types={"penalty_rate_too_high": "high", "warranty_too_short": "high"}),
        _entry(expected_types={"amount_inconsistency": "high"}),
        _entry(expected_grade="pass"),
    ]
    # 运行1: e1 漏 warranty(只判中 risk 也算漏), e3 干净文件误报 high, e3 评级漂移
    observed = [
        _obs("fail", {"penalty_rate_too_high"}),
        _obs("fail", {"amount_inconsistency"}),
        _obs("conditional_pass", {"missing_required_field"}),
    ]
    m = _run_level_metrics(entries, observed)
    assert abs(m["detection_rate"] - 2 / 3) < 1e-4  # 3 个期望 high 只命中 2(指标保留 4 位小数)
    assert m["zero_fp_rate"] == 0.0  # 干净文件出现了 high
    assert abs(m["grade_accuracy"] - 2 / 3) < 1e-4  # e3 pass→conditional 算错
    pt = m["per_type"]
    # macro-F1 = (penalty 1 + warranty 0 + amount 1 + missing_required_field 0) / 4
    assert abs(m["macro_f1"] - 0.5) < 1e-4
    assert pt["warranty_too_short"]["fn"] == 1
    assert pt["missing_required_field"]["fp"] == 1


def test_collapse_mean_and_range() -> None:
    """波动区间 = 各次运行指标取 mean + [min, max]。"""
    col = _collapse([0.9, 1.0, 0.8])
    assert col is not None
    assert col["mean"] == 0.9
    assert (col["min"], col["max"]) == (0.8, 1.0)
    assert _collapse([None, None]) is None
