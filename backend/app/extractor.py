"""结构化抽取。

分工（刻意把 LLM 调用做薄、把归一化做厚）：
- extract_contract：唯一调 LLM 的地方。让模型把金额/日期/比例"原样抄回"
  （不做计算、不改格式），再交给下方确定性归一化，避免模型格式化漂移；
- _parse_* / build_contract_model：纯函数，可离线单测，把原文串解析成
  ContractModel 类型化字段，并把模型返回的 evidence 回填到 extraction_meta。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field

from backend.app.llm import get_chat_model
from backend.app.schemas import ContractModel, Evidence, PaymentTerm

# 置信度低于该值 → needs_human_review=True（前端标黄，归入"需人工确认"）
CONFIDENCE_REVIEW_THRESHOLD = 0.7

# ContractModel 全部可抽取字段（不含 extraction_meta 本身），供证据字段校验
CONTRACT_FIELD_NAMES = {k for k in ContractModel.model_fields if k != "extraction_meta"}

# 抽取字段 → 中文含义（写进系统提示，指导模型逐项抽取）
EXTRACT_LABELS: dict[str, str] = {
    "buyer": "甲方（采购方）名称",
    "supplier": "乙方（供应商）名称",
    "signature_date": "合同签署日期",
    "effective_date": "合同生效日期",
    "expiry_date": "合同到期日",
    "total_amount": "合同总金额（元，保留千分位原样）",
    "currency": "币种",
    "penalty_rate": "逾期违约金比例（% 数值，如 1.5% 就写 1.5%）",
    "liability_cap": "责任上限（占合同总额 %）",
    "warranty_months": "质保期（月数）",
    "termination_notice_days": "解约提前通知期（天数）",
    "ip_ownership": "知识产权归属表述（原句）",
    "confidentiality_months": "保密期（月数）",
    "governing_law": "适用法律",
}


class ExtractionEvidence(BaseModel):
    """单条字段证据：原文摘录 + 条款引用 + 置信度。

    模型实测把 evidence 输出成 {字段名: 证据} 对象而非列表，故 schema 直接按
    dict 声明（键即 ContractModel 字段名），build 阶段再回填 extraction_meta。
    """

    quote: str = ""  # 原文摘录（模型必须抄原文，不允许改写）
    clause_ref: str = ""  # 条款引用（第X条 / 前言）
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # 置信度 0~1


class PaymentRaw(BaseModel):
    """付款期次原始输出（金额/比例保留字符串，由 build 归一化）。"""

    name: str = ""  # 期次名称
    amount: str | None = None  # 金额（原文，如 "200,000"）
    # 比例模型可能给数字或字符串（实测返回 int），归一化统一兜底
    percent: int | float | str | None = None  # 占总额比例（如 20 = 20%）


class ExtractionSchema(BaseModel):
    """with_structured_output 用的输出结构：普通字段 + 证据列表。"""

    buyer: str | None = None  # 采购方名称
    supplier: str | None = None  # 供应商名称
    signature_date: str | None = None  # 合同签订日期
    effective_date: str | None = None  # 合同生效日期
    expiry_date: str | None = None  # 合同过期日期
    total_amount: str | None = None  # 合同总额（元）
    currency: str | None = None  # 合同货币（如 CNY）
    payment_schedule: list[PaymentRaw] = Field(default_factory=list)
    penalty_rate: str | None = None  # 逾期违约金比例（% 数值，如 1.5% 就写 1.5%）
    liability_cap: str | None = None  # 责任上限（占合同总额 %）
    warranty_months: str | None = None  # 质保期（月数）
    termination_notice_days: str | None = None  # 解约提前通知期（天数）
    ip_ownership: str | None = None  # 知识产权归属表述（原句）
    confidentiality_months: str | None = None  # 保密期（月数）
    governing_law: str | None = None  # 适用法律
    evidence: dict[str, ExtractionEvidence] = Field(default_factory=dict)  # 字段名 → 证据


# ---- 确定性归一化（纯函数，核心测试面）----


def _parse_amount(value: str | int | float | None) -> Decimal | None:
    """金额串 → Decimal（元）。容忍千分位/单位/空格；解析不到返回 None。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    # 只取数字主体（含千分位与小数），丢弃"元/人民币"等字样
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def _parse_cn_date(value: str | None) -> date | None:
    """中文/ISO 日期串 → date；解析不到（如"无期限"）返回 None。"""
    if not value:
        return None
    text = value.strip()
    # 依次尝试：中文年月日 / ISO 短横线 / 斜杠
    for pattern in (r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"(\d{4})-(\d{1,2})-(\d{1,2})", r"(\d{4})/(\d{1,2})/(\d{1,2})"):
        match = re.search(pattern, text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None  # 日期越界（如 2月30日）
    return None


def _parse_percent(value: str | int | float | None) -> float | None:
    """百分比文本 → 数值口径（1.5% / 每日 1.5% / 20 → 1.5 / 1.5 / 20.0）。

    注意：口径与 rules 一致——存百分比数值而非小数（30 表示 30%）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _parse_int(value: str | int | float | None) -> int | None:
    """月数/天数文本 → int（"24 个月"→24）；解析不到返回 None。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


# ---- LLM 原始输出 → ContractModel ----


def _clamp_confidence(value: float) -> float:
    """置信度夹到 0~1，防模型给出越界值导致 pydantic 校验失败。"""
    return max(0.0, min(1.0, value))


def build_contract_model(raw: dict) -> ContractModel:
    """把 LLM 输出 dict 归一化成类型化 ContractModel，并回填字段证据。

    规则：
    - 每个字段独立容错——单个字段解析失败只置 None，不影响其他字段；
    - evidence 里 field 不在 ContractModel 字段集合内的条目直接丢弃；
    - 低置信度（< CONFIDENCE_REVIEW_THRESHOLD）自动标 needs_human_review。
    """
    # 分支：字段缺值/空串统一归一成 None，避免类型混入空字符串
    def _s(key: str) -> str | None:
        value = raw.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    terms: list[PaymentTerm] = []
    for item in raw.get("payment_schedule") or []:
        # 分支：期次名缺失的脏数据跳过，保留其余期次
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        terms.append(
            PaymentTerm(
                name=str(item.get("name")).strip(),
                amount=_parse_amount(item.get("amount")),
                percent=_parse_percent(item.get("percent")),
            )
        )

    meta: dict[str, Evidence] = {}
    evidence = raw.get("evidence")
    # 兼容两种形态：{字段名: 证据} 对象（模型实测）或 [{field, quote,...}] 列表
    if isinstance(evidence, dict):
        items = list(evidence.items())
    elif isinstance(evidence, list):
        items = [(ev.get("field"), ev) for ev in evidence if isinstance(ev, dict)]
    else:
        items = []
    for field, item in items:
        # 分支：字段不在可抽取集合 → 忽略（防模型编造字段名）
        if not field or field not in CONTRACT_FIELD_NAMES:
            continue
        confidence = _clamp_confidence(float(_get(item, "confidence", 0.0) or 0.0))
        meta[field] = Evidence(
            quote=str(_get(item, "quote", "") or ""),
            clause_ref=str(_get(item, "clause_ref", "") or ""),
            confidence=confidence,
            needs_human_review=confidence < CONFIDENCE_REVIEW_THRESHOLD,
        )

    return ContractModel(
        buyer=_s("buyer"),
        supplier=_s("supplier"),
        signature_date=_parse_cn_date(_s("signature_date")),
        effective_date=_parse_cn_date(_s("effective_date")),
        expiry_date=_parse_cn_date(_s("expiry_date")),
        total_amount=_parse_amount(_s("total_amount")),
        currency=_s("currency"),
        payment_schedule=terms,
        penalty_rate=_parse_percent(_s("penalty_rate")),
        liability_cap=_parse_percent(_s("liability_cap")),
        warranty_months=_parse_int(_s("warranty_months")),
        termination_notice_days=_parse_int(_s("termination_notice_days")),
        ip_ownership=_s("ip_ownership"),
        confidentiality_months=_parse_int(_s("confidentiality_months")),
        governing_law=_s("governing_law"),
        extraction_meta=meta,
    )


def _get(item, key: str, default=""):
    """从 dict 或 pydantic 模型取值（兼容模型输出的两种形态）。"""
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


# ---- LLM 调用----

_SYSTEM_PROMPT = """你是中文采购合同的结构化抽取器。请从合同正文中逐项抽取以下字段：
{labels}

输出要求：
1. 金额、日期、比例一律【原样抄写正文】，不要换算、不要改格式（如 1,000,000、2026年3月10日、每日 1.5%）；
2. 正文里找不到的字段填 null，且不要在 evidence 里编造；
3. payment_schedule 逐期输出：name（期次名）、amount（金额原文）、percent（占总额比例数值，如 20 表示 20%）；
4. evidence 输出为一个 JSON 对象：key 是字段名，value 是 {quote, clause_ref, confidence}。
   quote 必须是正文原句；clause_ref 填条款号（如"第四条"，无条款结构填"前言"）；
   confidence：原文明确命中给 0.9+，有推断或表述含糊给 0.6~0.85，找不到的字段不写 key；
5. 只输出 JSON。"""


def _system_message() -> str:
    """拼系统提示：把抽取字段清单（中文含义）写进去。"""
    labels = "\n".join(f"- {name}：{meaning}" for name, meaning in EXTRACT_LABELS.items())
    # 用 replace 而非 format：prompt 里含 {quote, ...} 字面花括号，format 会误当占位符
    return _SYSTEM_PROMPT.replace("{labels}", labels)


def extract_contract(llm=None, text: str = "") -> ContractModel:
    """对合同全文做一次结构化抽取：LLM 抄原文 → build 归一化回填证据。

    llm 可注入（测试用假模型）；不传则用默认 chat 模型（低温、关 thinking，
    抽取任务不需要深度推理，能显著降时延与成本）。
    """
    model = llm or get_chat_model(temperature=0.0, enable_thinking=False)
    structured = model.with_structured_output(ExtractionSchema, method="json_mode")
    result = structured.invoke([("system", _system_message()), ("human", text)])
    raw = result.model_dump() if hasattr(result, "model_dump") else result
    return build_contract_model(raw)
