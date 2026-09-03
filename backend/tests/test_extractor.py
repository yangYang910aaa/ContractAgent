"""extractor 单测：归一化解析 + LLM 原始输出 → ContractModel（纯函数部分，不调 LLM）。"""

from datetime import date
from decimal import Decimal

from backend.app.extractor import (
    _parse_amount,
    _parse_cn_date,
    _parse_int,
    _parse_percent,
    build_contract_model,
)


# ---- 归一化解析 ----


def test_parse_amount_variants() -> None:
    assert _parse_amount("1,000,000") == Decimal("1000000")
    assert _parse_amount("1,000,000 元") == Decimal("1000000")
    assert _parse_amount("1,000,000元") == Decimal("1000000")
    assert _parse_amount("") is None
    assert _parse_amount(None) is None


def test_parse_cn_date_variants() -> None:
    expect = date(2026, 3, 10)
    assert _parse_cn_date("2026年3月10日") == expect
    assert _parse_cn_date("2026-03-10") == expect
    assert _parse_cn_date("2026/3/10") == expect
    assert _parse_cn_date("未约定") is None
    assert _parse_cn_date("") is None


def test_parse_percent_variants() -> None:
    assert _parse_percent("1.5%") == 1.5
    assert _parse_percent("每日 1.5%") == 1.5
    assert _parse_percent("20") == 20.0
    assert _parse_percent("60%") == 60.0
    assert _parse_percent("") is None


def test_parse_int_variants() -> None:
    assert _parse_int("24 个月") == 24
    assert _parse_int("30") == 30
    assert _parse_int("6个月") == 6
    assert _parse_int("") is None


# ---- LLM 原始输出 → ContractModel ----


def _sample01_raw() -> dict:
    """模拟 LLM 对 sample_01 的输出（金额/日期保留原文格式，未做计算）。"""
    return {
        "buyer": "星辰智造科技有限公司",
        "supplier": "华芯电子有限公司",
        "signature_date": "2026年3月10日",
        "effective_date": "2026年3月10日",
        "expiry_date": "2027年3月9日",
        "total_amount": "1,000,000",
        "currency": "人民币",
        "payment_schedule": [
            {"name": "预付款", "amount": "200,000", "percent": "20"},
            {"name": "验收合格后支付", "amount": "800,000", "percent": "80"},
        ],
        "penalty_rate": "每日 0.05%",
        "liability_cap": "100%",
        "warranty_months": "24 个月",
        "termination_notice_days": "30 日",
        "ip_ownership": "定制成果知识产权归甲方（采购方）所有",
        "confidentiality_months": "24 个月",
        "governing_law": "中华人民共和国法律",
        "evidence": [
            {
                "field": "warranty_months",
                "quote": "乙方对所供货物提供自验收合格之日起 24 个月的质保期。",
                "clause_ref": "第四条",
                "confidence": 0.95,
            },
            {
                "field": "total_amount",
                "quote": "合同总价款为 1,000,000 元（币种：人民币）。",
                "clause_ref": "第一条",
                "confidence": 0.9,
            },
            {
                "field": "penalty_rate",
                "quote": "每逾期一日按合同总价款的 0.05% 向甲方支付违约金。",
                "clause_ref": "第五条",
                "confidence": 0.4,  # 低置信度 → needs_human_review
            },
        ],
    }


def test_build_normal_sample01() -> None:
    cm = build_contract_model(_sample01_raw())
    assert cm.total_amount == Decimal("1000000")
    assert cm.effective_date == date(2026, 3, 10)
    assert cm.expiry_date == date(2027, 3, 9)
    assert cm.warranty_months == 24
    assert cm.confidentiality_months == 24
    assert abs(cm.penalty_rate - 0.05) < 1e-9
    assert cm.payment_schedule[0].amount == Decimal("200000")
    assert cm.payment_schedule[0].percent == 20.0
    # 证据回填：quote 与 clause_ref 一一对应
    assert cm.extraction_meta["warranty_months"].quote.startswith("乙方对所供货物")
    assert cm.extraction_meta["warranty_months"].clause_ref == "第四条"
    # 低置信度标黄
    assert cm.extraction_meta["penalty_rate"].needs_human_review is True
    assert cm.extraction_meta["total_amount"].needs_human_review is False


def test_build_sample03_percent_values() -> None:
    """违约金日 1.5%、责任上限 5% 应归一化为 1.5 / 5.0（百分比数值口径）。"""
    raw = _sample01_raw()
    raw["penalty_rate"] = "每日 1.5%"
    raw["liability_cap"] = "5%"
    cm = build_contract_model(raw)
    assert cm.penalty_rate == 1.5
    assert cm.liability_cap == 5.0


def test_build_missing_and_messy_input() -> None:
    cm = build_contract_model({})
    assert cm.total_amount is None
    assert cm.extraction_meta == {}
    # 无法解析的日期/金额不崩溃，置 None
    messy = build_contract_model({"expiry_date": "无期限", "total_amount": "以实际结算为准", "evidence": [{"field": "不存在的字段", "confidence": 1.5}]})
    assert messy.expiry_date is None
    assert messy.total_amount is None
    assert messy.extraction_meta == {}  # 未知证据字段被忽略


def test_build_real_model_shaped_output() -> None:
    """镜像真实模型输出：percent 给数字、evidence 给 {字段名: 证据} 对象。"""
    raw = {
        "total_amount": "800,000 元",
        "payment_schedule": [
            {"name": "预付款", "amount": "480,000 元", "percent": 60},  # int 而非 str
            {"name": "验收合格后支付", "amount": "320,000 元", "percent": 40},
        ],
        "evidence": {
            "total_amount": {"quote": "合同总价款为 800,000 元", "clause_ref": "第一条", "confidence": 1.0},
            "penalty_rate": {"quote": "每逾期一日按总价款的 1.5%", "clause_ref": "第五条", "confidence": 0.6},
            "不存在的字段": {"quote": "x", "clause_ref": "", "confidence": 0.9},
        },
    }
    cm = build_contract_model(raw)
    assert cm.total_amount == Decimal("800000")
    assert cm.payment_schedule[0].percent == 60.0  # int 60 → 归一化成 60.0
    assert cm.extraction_meta["total_amount"].clause_ref == "第一条"
    assert cm.extraction_meta["penalty_rate"].needs_human_review is True  # 0.6 < 0.7
    assert "不存在的字段" not in cm.extraction_meta
