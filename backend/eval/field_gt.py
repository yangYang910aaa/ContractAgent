"""字段级 ground truth：合成 sample 的抽取期望（金额/日期/比例等，D29 后下一步②）。

用途：给 run_eval 的"字段准确率"指标提供真值——逐字段比较抽取结果与生成 spec
的真实参数，量"抽取准不准"（金额/日期/比例），是"关键字段双读"的尺子。

真值来源：generate_samples.SPECS（sample_01~05 企业 / 06~07 校服 / 08~09 技术），
字段值与渲染正文一致（spec 参数化，直接是文本真值）。校服/技术合同的生效日正文
写"自{签署日}双方签字盖章之日起生效"，无独立日期——抽取靠 infer_effective_from_
signature 回填，故期望生效日 = 签署日（与 rules 兜底口径一致）。

口径约定（仅第一期，覆盖 9 份 sample；变体/真实合同无结构化 spec，后续再说）：
- expected_fields 只收录"正文必然出现、抽取器应当填对"的字段；值为 None 表示
  该文件正文没有此内容（如 sample_04 缺保密条款、校服无责任上限），抽到值反算
  wrong（量化 LLM 幻觉，呼应 D23 tech_03 脑补金额的教训）；
- 判分字段 = 金额(total_amount/payment_schedule) + 日期(signature/effective/expiry)
  + 比例与数值(penalty_rate/liability_cap/warranty_months/confidentiality_months/
  termination_notice_days) + 标识(buyer/supplier/contract_kind/currency)；
  ip_ownership/governing_law 是语义原句，不进精确匹配（规则按语义判断）。
"""

from __future__ import annotations

from decimal import Decimal

from backend.eval.generate_samples import SPECS, TECH_SPECS, UNIFORM_SPECS
from backend.eval.generate_samples import SampleSpec, TechServiceSampleSpec, UniformSampleSpec

# 判分字段顺序（输出/打印稳定用；只判 expected_fields 里出现的字段）
FIELD_GROUPS: dict[str, list[str]] = {
    "金额": ["total_amount", "payment_schedule"],
    "日期": ["signature_date", "effective_date", "expiry_date"],
    "比例数值": [
        "penalty_rate",
        "liability_cap",
        "warranty_months",
        "confidentiality_months",
        "termination_notice_days",
    ],
    "标识": ["contract_kind", "buyer", "supplier", "currency"],
}


def _amount(value: str) -> str:
    """金额字符串 → 去千分位的数字串（抽取 output 的 Decimal json 形态一致）。"""
    return str(Decimal(value.replace(",", "")))


def _date(value: str) -> str:
    """中文日期（2026年3月10日）→ ISO（2026-03-10）；复用 extractor 同一解析口径。"""
    from backend.app.extractor import _parse_cn_date

    return _parse_cn_date(value).isoformat()


def _pay(terms: list[tuple[str, str, int | float]]) -> list[dict]:
    """期次 (name, amount, percent) → 判分形态（金额去千分位、比例统一 float）。"""
    return [{"amount": _amount(amount), "percent": float(percent)} for _, amount, percent in terms]


def _enterprise_fields(spec: SampleSpec) -> dict:
    """企业 SampleSpec → 字段期望（该品类条款全渲染，按 spec 直取）。"""
    return {
        "contract_kind": "enterprise_goods",
        "buyer": spec.buyer,
        "supplier": spec.supplier,
        "signature_date": _date(spec.signature_date),
        "effective_date": _date(spec.effective_date),
        "expiry_date": _date(spec.expiry_date),
        "currency": "人民币",
        "total_amount": _amount(spec.total_amount),
        "payment_schedule": _pay(spec.payment_terms),
        "warranty_months": spec.warranty_months,
        "penalty_rate": float(spec.penalty_daily_percent),
        "liability_cap": (
            float(spec.liability_cap_percent) if spec.liability_cap_percent is not None else None
        ),
        "confidentiality_months": (
            spec.confidentiality_months if spec.confidentiality_clause else None
        ),
        "termination_notice_days": spec.termination_notice_days,
    }


def _uniform_fields(spec: UniformSampleSpec) -> dict:
    """校服 UniformSampleSpec → 字段期望。

    与企业的差异：总额/付款是固定骨架值（198,400、一次性付款无期次）；生效日无独立
    文本（回填签署日）；gov 基线不渲染 责任上限/保密/IP，故数值字段期望 None。
    """
    return {
        "contract_kind": "gov_goods",
        "buyer": spec.buyer,
        "supplier": spec.supplier,
        "signature_date": _date(spec.signature_date),
        "effective_date": _date(spec.signature_date),  # "签字盖章之日起生效"→回填签署日
        "expiry_date": _date(spec.expiry_date),
        "currency": "人民币",
        "total_amount": "198400",
        "payment_schedule": [],  # 一次性付款，无期次表
        "warranty_months": spec.warranty_months,
        "penalty_rate": float(spec.penalty_daily_percent),
        "liability_cap": None,  # 校服骨架无责任上限条款
        "confidentiality_months": None,  # 校服骨架无保密条款
        "termination_notice_days": None,  # 解除条款无"提前 N 日通知"约定
    }


def _tech_fields(spec: TechServiceSampleSpec) -> dict:
    """技术开发 TechServiceSampleSpec → 字段期望（费用 1,200,000/期次 30-40-30 固定骨架）。"""
    return {
        "contract_kind": "tech_service",
        "buyer": spec.buyer,
        "supplier": spec.supplier,
        "signature_date": _date(spec.signature_date),
        "effective_date": _date(spec.signature_date),  # 生效句同校服式（回填签署日）
        "expiry_date": _date(spec.expiry_date),
        "currency": "人民币",
        "total_amount": "1200000",
        "payment_schedule": [
            {"amount": "360000", "percent": 30.0},
            {"amount": "480000", "percent": 40.0},
            {"amount": "360000", "percent": 30.0},
        ],
        "warranty_months": spec.warranty_months,
        "penalty_rate": float(spec.penalty_daily_percent),
        "liability_cap": float(spec.liability_cap_percent),
        "confidentiality_months": spec.confidentiality_months,
        "termination_notice_days": 30,  # 解除条款固定"提前 30 日书面通知"
    }


def build_expected_fields() -> dict[str, dict]:
    """全部 9 份 sample 的 expected_fields：{文件名: {字段: 期望值}}。"""
    out: dict[str, dict] = {}
    for spec in SPECS:
        out[spec.filename] = _enterprise_fields(spec)
    for spec in UNIFORM_SPECS:
        out[spec.filename] = _uniform_fields(spec)
    for spec in TECH_SPECS:
        out[spec.filename] = _tech_fields(spec)
    return out


EXPECTED_FIELDS = build_expected_fields()
