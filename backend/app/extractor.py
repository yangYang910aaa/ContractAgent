"""结构化抽取。

分工（刻意把 LLM 调用做薄、把归一化做厚）：
- extract_contract：唯一调 LLM 的地方。让模型把金额/日期/比例"原样抄回"
  （不做计算、不改格式），再交给下方确定性归一化，避免模型格式化漂移；
- _parse_* / build_contract_model：纯函数，可离线单测，把原文串解析成
  ContractModel 类型化字段，并把模型返回的 evidence 回填到 extraction_meta。
"""

from __future__ import annotations

import re
import json
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
    "contract_kind": "合同品类（从标题/正文判断）：enterprise_goods=企业货物采购, gov_goods=政府采购/校服类, "
    "agri_goods=农副产品买卖, tech_service=技术开发/软件/技术服务；无法判断填 null",
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

    contract_kind: str | None = None  # 合同品类（enterprise_goods/gov_goods/agri_goods/tech_service）
    buyer: str | None = None  # 采购方名称
    supplier: str | None = None  # 供应商名称
    signature_date: str | None = None  # 合同签订日期
    effective_date: str | None = None  # 合同生效日期
    expiry_date: str | None = None  # 合同过期日期
    total_amount: str | None = None  # 合同总额（元）
    currency: str | None = None  # 合同货币（如 CNY）
    payment_schedule: list[PaymentRaw] = Field(default_factory=list)
    # 数值类字段容忍 str/int/float：模型对"质保 2 年"可能直接给折算好的数字 24，
    # 只声明 str 会让 json_mode 校验直接失败（2026-09-04 校服样本实测踩坑）；
    # 归一化阶段 _parse_* 本就兼容数字输入，故仅放宽声明不做逻辑改动。
    penalty_rate: str | int | float | None = None  # 逾期违约金比例（% 数值，如 1.5% 就写 1.5%）
    liability_cap: str | int | float | None = None  # 责任上限（占合同总额 %）
    warranty_months: str | int | float | None = None  # 质保期（月数，可给 24 或 "24 个月"）
    termination_notice_days: str | int | float | None = None  # 解约提前通知期（天数）
    ip_ownership: str | None = None  # 知识产权归属表述（原句）
    confidentiality_months: str | int | float | None = None  # 保密期（月数，可给数字或 "3 年"）
    governing_law: str | None = None  # 适用法律
    evidence: dict[str, ExtractionEvidence] = Field(default_factory=dict)  # 字段名 → 证据


# ---- 确定性归一化（纯函数，核心测试面）----


def _parse_kind(value: str | None) -> str | None:
    """LLM 品类输出 → 枚举值；识别不到返回 None（规则按企业采购默认处理）。

    兼容模型直接给枚举值或给中文描述/含关键词的文本两种形态。
    """
    if not value:
        return None
    text = value.strip()
    kinds = ("enterprise_goods", "gov_goods", "agri_goods", "tech_service")
    # 分支 1：直接命中枚举值 → 原样返回
    if text in kinds:
        return text
    # 分支 2：关键词判别（政府采购/校服 → gov；农副 → agri；技术/软件/服务 → tech）
    if any(kw in text for kw in ("校服", "政采", "政府采购")):
        return "gov_goods"
    if any(kw in text for kw in ("农副", "农产品")):
        return "agri_goods"
    if any(kw in text for kw in ("技术开发", "技术服务", "软件", "系统集成")):
        return "tech_service"
    return None


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


def _parse_months(value: str | int | float | None) -> int | None:
    """质保/保密期等「月数」字段 → int（单位感知：2 年→24、6 个月→6、裸数→原值）。

    作用：真实合同常按「年」写期限（如校服合同「质保 2 年」），LLM 按"原样抄写"
    原则可能返回 "2 年"——直接取数字会得到 2，被规则误判成不足 12 个月（易错点）。
    判定口径：文本带「N 年」→ N×12；带「N 个月/月」→ N；只有裸数字 → 原值；
    解析不到返回 None。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    # 分支 1：按「年」书写（如 2 年 / 1.5 年）→ 折算成月
    match = re.search(r"(\d+(?:\.\d+)?)\s*年", text)
    if match:
        return round(float(match.group(1)) * 12)
    # 分支 2：按「月」书写（如 24 个月 / 6 个月）→ 原值
    match = re.search(r"(\d+(?:\.\d+)?)\s*个?月", text)
    if match:
        return int(match.group(1))
    # 分支 3：裸数字（历史/简化写法，如 36）→ 原值
    match = re.search(r"\d+", text)
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

    # 数字类字段（月数/比例/天数）：模型可能给 int/float（json_mode 实测给数字），
    # 不能走 _s 把数字折叠成 None，需原样交给下方 _parse_*（它们兼容数字输入）
    def _n(key: str):
        value = raw.get(key)
        return value.strip() or None if isinstance(value, str) else value

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
        contract_kind=_parse_kind(raw.get("contract_kind")),
        buyer=_s("buyer"),
        supplier=_s("supplier"),
        signature_date=_parse_cn_date(_s("signature_date")),
        effective_date=_parse_cn_date(_s("effective_date")),
        expiry_date=_parse_cn_date(_s("expiry_date")),
        total_amount=_parse_amount(_s("total_amount")),
        currency=_s("currency"),
        payment_schedule=terms,
        penalty_rate=_parse_percent(_n("penalty_rate")),
        liability_cap=_parse_percent(_n("liability_cap")),
        warranty_months=_parse_months(_n("warranty_months")),
        termination_notice_days=_parse_int(_n("termination_notice_days")),
        ip_ownership=_s("ip_ownership"),
        confidentiality_months=_parse_months(_n("confidentiality_months")),
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
   quote 必须是正文原句；clause_ref 填所在条款/章节号（如"第四条"，章节式文本填
   "一、质量要求"这类章节头，无条款结构填"前言"）；
   confidence：原文明确命中给 0.9+，有推断或表述含糊给 0.6~0.85，找不到的字段不写 key；
5. contract_kind 只从标题/首部/条款风格判断，不要凭正文金额猜；
6. 只输出 JSON。"""


def _system_message() -> str:
    """拼系统提示：把抽取字段清单（中文含义）写进去。"""
    labels = "\n".join(f"- {name}：{meaning}" for name, meaning in EXTRACT_LABELS.items())
    # 用 replace 而非 format：prompt 里含 {quote, ...} 字面花括号，format 会误当占位符
    return _SYSTEM_PROMPT.replace("{labels}", labels)


# ---- json_mode 解析失败兜底（模型漂移：把字段值包成 evidence 对象）----


def _unwrap_drifted(value):
    """把"证据包裹型"值还原：dict 且带 quote → 取 quote 当字段值；其余原样返回。

    递归处理列表（payment_schedule 逐项也可能是包裹型）。
    """
    if isinstance(value, dict) and "quote" in value:
        return value["quote"]
    if isinstance(value, list):
        return [_unwrap_drifted(item) for item in value]
    return value


def _normalize_drifted(raw: dict) -> dict:
    """把漂移输出整形成 build_contract_model 认识的形态 (字段标量 + 独立 evidence)。
    """
    out: dict = {}
    evidence: dict = {}
    for key, value in raw.items():
        # 分支：漂移输出里没有独立 evidence（都内嵌在字段里），跳过避免覆盖
        if key == "evidence":
            continue
        # 分支：字段值是证据对象 → 值取 quote，并把引用信息收集进 evidence
        if isinstance(value, dict) and "quote" in value:
            out[key] = value.get("quote", "")
            evidence[key] = {
                "quote": value.get("quote", ""),
                "clause_ref": value.get("clause_ref", ""),
                "confidence": value.get("confidence", 0.0) or 0.0,
            }
        # 分支：正常标量或半漂移（列表/其余 dict）→ 递归拆包
        else:
            out[key] = _unwrap_drifted(value)
    out["evidence"] = evidence
    return out


def _recover_completion(exc: Exception) -> dict | None:
    """从 with_structured_output 的解析报错里还原模型原始 JSON。

    langchain 报错形如 "Failed to parse X from completion {json} Got: …"；
    提取花括号正文后 json 解析。解析失败返回 None（上层按原异常抛给 error 报告）。
    """
    text = str(exc)
    match = re.search(r"completion (\{.*\}) Got:", text, re.S)
    if not match:
        return None
    try:
        raw = json.loads(match.group(1))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def extract_contract(llm=None, text: str = "") -> ContractModel:
    """对合同全文做一次结构化抽取: LLM 抄原文 → build 归一化回填证据。

    llm 可注入（测试用假模型）；不传则用默认 chat 模型（低温、关 thinking,
    抽取任务不需要深度推理，能显著降时延与成本）。
    """
    model = llm or get_chat_model(temperature=0.0, enable_thinking=False)
    structured = model.with_structured_output(ExtractionSchema, method="json_mode")
    try:
        result = structured.invoke([("system", _system_message()), ("human", text)])
    except Exception as exc:
        raw = _recover_completion(exc)
        # 这种情况是：解析失败但报错里带原始 completion → 归一化兜底后照常审核
        if raw is not None:
            return build_contract_model(_normalize_drifted(raw))
        # 这种情况是：还原失败（接口/超时/格式不支持）→ 原样抛出走 error 报告
        raise
    raw = result.model_dump() if hasattr(result, "model_dump") else result
    return build_contract_model(raw)
