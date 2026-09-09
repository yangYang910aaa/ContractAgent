"""字段级 GT 与字段准确率指标单测（不调 LLM，纯离线）。"""

from backend.eval.field_gt import EXPECTED_FIELDS, FIELD_GROUPS
from backend.eval.run_eval import (
    _FIELD_FLAT,
    _field_level_metrics,
    _field_outcome,
)


def _entry(**kwargs):
    """最小 GtEntry 工厂（字段准确率判分只需 expected_fields）。"""
    from backend.eval.run_eval import GtEntry

    defaults = dict(file="x.md", set_="samples", kind="", expected_grade=None, judge=True)
    defaults.update(kwargs)
    return GtEntry(**defaults)


def test_field_gt_covers_all_nine_samples_and_matches_corpus() -> None:
    """字段 GT 恰好覆盖 9 份 sample（与语料一一对应，防新增样本漏登记）。"""
    assert len(EXPECTED_FIELDS) == 9
    from backend.app.config import BASE_DIR

    corpus = sorted((BASE_DIR / "data" / "contracts").glob("sample_*.md"))
    corpus_names = {p.name for p in corpus}
    assert set(EXPECTED_FIELDS) == corpus_names, "field_gt 与 data/contracts sample 文件必须一一对应"


def test_field_gt_keys_match_judged_field_set() -> None:
    """每份的 expected_fields 键 ⊆ 判分字段集合（防拼错字段名静默不判）。"""
    judged = {f for group in FIELD_GROUPS.values() for f in group}
    assert judged == set(_FIELD_FLAT)
    for filename, fields in EXPECTED_FIELDS.items():
        unknown = set(fields) - judged
        assert not unknown, f"{filename} 含未登记判分字段: {unknown}"


def test_field_gt_value_shapes() -> None:
    """值形态 sanity：金额是数字串、日期 ISO、比例 float/int；None 表示正文无此内容。"""
    import re

    for filename, fields in EXPECTED_FIELDS.items():
        assert re.fullmatch(r"\d+", fields["total_amount"]), filename
        for key in ("signature_date", "effective_date", "expiry_date"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[key]), (filename, key)
        for term in fields["payment_schedule"]:
            assert re.fullmatch(r"\d+", term["amount"])
            assert isinstance(term["percent"], (int, float))
        assert isinstance(fields["contract_kind"], str)


def _outcome_cases() -> list[tuple]:
    """(字段, 期望, 实际, 结果) 覆盖 ok/wrong/missing/None 期望/期次表。"""
    return [
        ("total_amount", "1000000", "1000000", "ok"),
        ("total_amount", "1000000", "1,000,000", "ok"),  # 抽取归一后带千分位也认（防御）
        ("total_amount", "1000000", "1100000", "wrong"),
        ("total_amount", "1000000", None, "missing"),
        ("signature_date", "2026-03-10", "2026-03-10", "ok"),
        ("signature_date", "2026-03-10", "2026-03-11", "wrong"),
        ("warranty_months", 24, 24, "ok"),
        ("warranty_months", 24, 6, "wrong"),
        ("confidentiality_months", None, None, "ok"),  # 正文无保密期 → 没抽到=对
        ("confidentiality_months", None, 60, "wrong"),  # 幻觉值 → wrong（tech_03 教训）
        ("payment_schedule", [{"amount": "200000", "percent": 20.0}], None, "missing"),
        (
            "payment_schedule",
            [
                {"amount": "200000", "percent": 20.0},
                {"amount": "800000", "percent": 80.0},
            ],
            [
                {"amount": "200000", "percent": 20.0},
                {"amount": "800000", "percent": 80.0},
            ],
            "ok",
        ),
        (
            "payment_schedule",
            [
                {"amount": "200000", "percent": 20.0},
                {"amount": "800000", "percent": 80.0},
            ],
            [{"amount": "800000", "percent": 80.0}],  # 漏一期 → wrong
            "wrong",
        ),
    ]


def test_field_outcome_ok_wrong_missing() -> None:
    """单字段判定口径逐例核对（含 None 期望的幻觉检测与期次表比对）。"""
    for field, expected, actual, want in _outcome_cases():
        got = _field_outcome(field, expected, actual)
        assert got == want, f"{field} expected={expected!r} actual={actual!r} -> {got} != {want}"


def test_field_level_metrics_hand_computed() -> None:
    """字段级汇总按手算值核对：ok/wrong/missing 与 overall 准确率。"""
    entries = [
        _entry(
            expected_fields={
                "total_amount": "1000000",
                "warranty_months": 24,
                "confidentiality_months": None,
            }
        ),
        _entry(expected_fields={"total_amount": "800000", "signature_date": "2026-06-01"}),
    ]
    observed = [
        {
            "fields": {
                "total_amount": "ok",
                "warranty_months": "wrong",
                "confidentiality_months": "ok",
            }
        },
        {
            "fields": {
                "total_amount": "missing",
                "signature_date": "ok",
            }
        },
    ]
    m = _field_level_metrics(entries, observed)
    # 全量 5 判分：ok=3 / wrong=1 / missing=1 → overall = 3/5 = 0.6
    assert m["overall_accuracy"] == 0.6
    assert m["per_field"]["total_amount"]["ok"] == 1
    assert m["per_field"]["total_amount"]["missing"] == 1
    assert m["per_field"]["warranty_months"]["wrong"] == 1
