"""extractor 单测：归一化解析 + LLM 原始输出 → ContractModel（纯函数部分，不调 LLM）。"""

from datetime import date
from decimal import Decimal
import json

from backend.app.extractor import (
    _parse_amount,
    _parse_cn_date,
    _parse_int,
    _parse_kind,
    _parse_months,
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


def test_parse_months_unit_aware() -> None:
    """月数归一化须单位感知：按年书写 ×12（校服合同「质保 2 年」→24 个月）。"""
    assert _parse_months("2 年") == 24
    assert _parse_months("自验收合格之日起质保 2 年") == 24
    assert _parse_months("6 个月") == 6
    assert _parse_months("24 个月") == 24
    assert _parse_months("60") == 60  # 裸数字按原值
    assert _parse_months("长期") is None
    assert _parse_months("") is None
    assert _parse_months(None) is None


def test_parse_kind_variants() -> None:
    """品类解析：直接枚举值 / 关键词文本 / 空值。"""
    assert _parse_kind("gov_goods") == "gov_goods"
    assert _parse_kind("校服采购") == "gov_goods"
    assert _parse_kind("政府采购（校服）") == "gov_goods"
    assert _parse_kind("农副产品买卖") == "agri_goods"
    assert _parse_kind("软件技术开发合同") == "tech_service"
    assert _parse_kind("enterprise_goods") == "enterprise_goods"
    assert _parse_kind("") is None
    assert _parse_kind(None) is None


def test_build_contract_model_keeps_parsed_kind() -> None:
    """LLM 原始输出里的 contract_kind 经解析后写入 ContractModel。"""
    model = build_contract_model({"contract_kind": "农副产品买卖合同"})
    assert model.contract_kind == "agri_goods"
    assert build_contract_model({}).contract_kind is None


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


def test_build_warranty_in_years_converts_to_months() -> None:
    """LLM 按原文抄「质保 2 年」时，归一化应折算成 24 个月而非 2。"""
    raw = _sample01_raw()
    raw["warranty_months"] = "2 年"
    raw["confidentiality_months"] = "3 年"
    cm = build_contract_model(raw)
    assert cm.warranty_months == 24
    assert cm.confidentiality_months == 36


def test_extraction_schema_tolerates_numeric_months() -> None:
    """json_mode 输出里月数可能是数字（质保 24/6）而非字符串，schema 应放行。"""
    from backend.app.extractor import ExtractionSchema

    raw = ExtractionSchema(
        contract_kind="gov_goods",
        warranty_months=24,
        confidentiality_months="3 年",
        penalty_rate=1.5,
        termination_notice_days=30,
    )
    cm = build_contract_model(raw.model_dump())
    assert cm.warranty_months == 24
    assert cm.confidentiality_months == 36
    assert cm.penalty_rate == 1.5
    assert cm.termination_notice_days == 30


def test_build_normalizes_evidence_wrapped_drift() -> None:
    """漂移形态归一化：字段值=evidence 对象 → 值取 quote，引用回填 evidence。"""
    from backend.app.extractor import _normalize_drifted

    raw = {
        "buyer": {"quote": "星辰智造科技有限公司", "clause_ref": "甲方（采购方）", "confidence": 1.0},
        "total_amount": {"quote": "1,000,000 元", "clause_ref": "第一条", "confidence": 1.0},
        "warranty_months": {"quote": "24个月", "clause_ref": "第四条", "confidence": 1.0},
        "penalty_rate": {"quote": "1.5%", "clause_ref": "第五条", "confidence": 0.9},
        "currency": "人民币",  # 半漂移：正常标量应原样保留
        "evidence": {"ignore": {"quote": "x"}},  # 漂移输出里的伪 evidence 键应被跳过
    }
    cm = build_contract_model(_normalize_drifted(raw))
    assert cm.buyer == "星辰智造科技有限公司"
    assert cm.total_amount == Decimal("1000000")
    assert cm.warranty_months == 24
    assert cm.penalty_rate == 1.5
    assert cm.currency == "人民币"
    assert cm.extraction_meta["buyer"].clause_ref == "甲方（采购方）"
    assert cm.extraction_meta["warranty_months"].quote == "24个月"
    assert "ignore" not in cm.extraction_meta


class _DriftLLM:
    """假模型：with_structured_output 后 invoke 直接抛"证据包裹型"解析错误。"""

    def __init__(self, completion: str) -> None:
        self._completion = completion

    def with_structured_output(self, schema, method=None):
        return self

    def invoke(self, messages):
        # 镜像 langchain 实测报错文案（completion 后跟原始 JSON，Got: 后是校验明细）
        raise RuntimeError(
            f"Failed to parse ExtractionSchema from completion {self._completion} "
            "Got: 25 validation errors for ExtractionSchema"
        )


class _DriftLLMDotSeparator(_DriftLLM):
    """镜像真实 langchain 报错：completion JSON 与 Got: 之间带句点（". Got:"）。

    早期正则 `completion (\{.*\}) Got:` 在此形态下漏匹配导致整份 error——
    2026-09-05 用户上传农副 GF 示范文本（GF—2025—0151）实测复现。
    """

    def invoke(self, messages):
        raise RuntimeError(
            f"Failed to parse ExtractionSchema from completion {self._completion}."
            " Got: 13 validation errors for ExtractionSchema"
        )


def test_extract_fallback_rescues_evidence_wrapped_drift() -> None:
    """parse 失败时能从报错还原 completion 并走漂移归一化，合同不再整体 error。"""
    from backend.app.extractor import extract_contract

    completion = json.dumps(
        {
            "buyer": {"quote": "星辰智造科技有限公司", "clause_ref": "甲方（采购方）", "confidence": 1.0},
            "total_amount": {"quote": "1,000,000 元", "clause_ref": "第一条", "confidence": 1.0},
            "expiry_date": {"quote": "2027年5月19日", "clause_ref": "第八条", "confidence": 1.0},
            "warranty_months": {"quote": "24个月", "clause_ref": "第四条", "confidence": 1.0},
        },
        ensure_ascii=False,
    )
    cm = extract_contract(llm=_DriftLLM(completion), text="正文")
    assert cm.buyer == "星辰智造科技有限公司"
    assert cm.total_amount == Decimal("1000000")
    assert cm.expiry_date == date(2027, 5, 19)
    assert cm.warranty_months == 24


def test_extract_fallback_rescues_dot_separator_drift() -> None:
    """报错形如 '}. Got:'（带句点）也能还原 completion，合同不再整体 error。"""
    from backend.app.extractor import extract_contract

    completion = json.dumps(
        {
            "contract_kind": "agri_goods",  # 半漂移：这个字段是正常标量
            "buyer": {"quote": "甲方（买受人）：", "clause_ref": "首部", "confidence": 0.9},
            "supplier": {"quote": "乙方（出卖人）：", "clause_ref": "首部", "confidence": 0.9},
            "signature_date": {"quote": "年 月 日", "clause_ref": "签署栏", "confidence": 0.6},
            "effective_date": {"quote": "本合同自甲、乙双方签名（盖章）之日起成立并生效。", "clause_ref": "第十七条", "confidence": 0.9},
            "total_amount": {"quote": "货款金额为： 元（大写: ）。", "clause_ref": "第四条", "confidence": 0.6},
            "currency": {"quote": "元", "clause_ref": "第四条", "confidence": 0.9},
            "governing_law": {"quote": "本合同之订立、生效、解释、变更、终止、执行与争议解决均适用中华人民共和国的法律法规。", "clause_ref": "第十六条", "confidence": 0.9},
        },
        ensure_ascii=False,
    )
    cm = extract_contract(llm=_DriftLLMDotSeparator(completion), text="正文")
    # 品类与文本字段照常归一化；金额空白模板解析不到 → None（规则会判缺必填）
    assert cm.contract_kind == "agri_goods"
    assert cm.buyer == "甲方（买受人）："
    assert cm.governing_law.startswith("本合同之订立")
    assert cm.total_amount is None
    assert cm.extraction_meta["buyer"].clause_ref == "首部"


def test_extract_raises_when_completion_not_recoverable() -> None:
    """报错里没有 completion JSON（如网络/接口异常）→ 原样抛出，走 error 报告。"""
    from backend.app.extractor import extract_contract

    class _PlainErrorLLM:
        def with_structured_output(self, schema, method=None):
            return self

        def invoke(self, messages):
            raise RuntimeError("connect timeout")

    try:
        extract_contract(llm=_PlainErrorLLM(), text="正文")
    except RuntimeError as exc:
        assert "connect timeout" in str(exc)
    else:
        raise AssertionError("预期抛 RuntimeError，实际未抛")


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
