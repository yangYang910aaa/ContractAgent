"""结构化抽取。

分工（刻意把 LLM 调用做薄、把归一化做厚）：
- extract_contract: 唯一调 LLM 的地方。让模型把金额/日期/比例"原样抄回"
  （不做计算、不改格式），再交给下方确定性归一化，避免模型格式化漂移；
- _parse_* / build_contract_model: 纯函数，可离线单测，把原文串解析成
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
    "contract_kind": "合同品类（从标题/正文判断）:enterprise_goods=企业货物采购, gov_goods=政府采购/校服类, "
    "agri_goods=农副产品买卖, tech_service=技术开发/技术服务/软件开发/系统集成等（服务形态）；"
    "单纯购买成品软件/货物属 enterprise_goods；无法判断填 null",
    "buyer": "甲方（采购方）名称",
    "supplier": "乙方（供应商）名称",
    "signature_date": "合同签署日期",
    "effective_date": "合同生效日期",
    "expiry_date": "合同到期日",
    "total_amount": "合同总金额（元，保留千分位原样）",
    "currency": "币种",
    # 口径提醒: penalty_rate 只取乙方(供应商)逾期交付/履约的违约金比例.
    # 甲方逾期付款的违约金是甲方义务, 不属于对供应商的审查对象;
    # 混填会把正常合同误判成 high. 只填"按日计收"口径(正文含 每逾期一日/每日 X%);
    # "每次/每笔违约按总额 X%" 等非按日违约金不适用日费率畸高阈值, 填 null(2026-09-07).
    "penalty_rate": "乙方（供应商）逾期交付/逾期履约的违约金比例（% 数值，如 1.5% 就写 1.5%；若同时有甲方逾期付款违约金，取乙方违约那一项，不要取甲方的；只在按日计收时填：正文写'每逾期一日按…X%'或'每日 X%'才填数值，'每次违约按合同总价 X%'这类非按日口径填 null）",
    # 口径提醒: liability_cap 指赔偿责任上限(如"责任/赔偿总额以合同价款的
    # X% 为限"); 仅写"违约金总额不超过 X%"不算赔偿责任上限, 填 null.
    "liability_cap": "赔偿责任上限（占合同总额 %，如'赔偿总额以合同总价的X%为限'；仅违约金总额上限不要填）",
    "warranty_months": "质保期（月数）",
    # 口径提醒: termination_notice_days 只取"提前 N 日书面通知解除合同"的 N;
    # 催告期（"经催告 N 日内未履行可解除"）与异议期不是解约通知期, 填 null
    # （校服等示范文本常见催告表述, 2026-09-09 字段尺子实测被误抽）
    "termination_notice_days": "解约提前通知期（天数，只填正文明确写'提前 N 日书面通知解除合同'的 N；催告期不算）",
    "ip_ownership": "知识产权归属表述（原句）",
    "confidentiality_months": "保密期（月数）",
    "governing_law": "适用法律",
}


class ExtractionEvidence(BaseModel):
    """单条字段证据：原文摘录 + 条款引用 + 置信度。

    模型实测把 evidence 输出成 {字段名: 证据} 对象而非列表，故 schema 直接按
    dict 声明(键即 ContractModel 字段名), build 阶段再回填 extraction_meta。
    """

    quote: str = ""  # 原文摘录（模型必须抄原文，不允许改写）
    clause_ref: str = ""  # 条款引用（第X条 / 前言）
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # 置信度 0~1


class PaymentRaw(BaseModel):
    """付款期次原始输出（金额/比例保留字符串，由 build 归一化）。"""

    name: str = ""  # 期次名称
    amount: str | None = None  # 金额（原文，如 "200,000"）
    # 比例模型可能给数字或字符串，归一化统一兜底
    percent: int | float | str | None = None  # 占总额比例（如 20 = 20%）


class ExtractionSchema(BaseModel):
    """LLM with_structured_output 用的输出结构：普通字段 + 证据列表。"""

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


# ---- 确定性归一化:把 LLM 输出的不规范的、格式多变的原始值，通过纯函数 转换成类型安全、格式统一的目标值----


def _parse_kind(value: str | None) -> str | None:
    """LLM 品类输出 :把LLM输出的品类描述映射到四个枚举值之一。
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
    # 这种情况是：tech 只认"服务/开发形态"——技术开发/技术服务/软件开发/系统集成等；
    # 单纯"软件/货物采购"是 enterprise（sample_05 企业管理软件采购被裸"软件"误判过）
    if any(
        kw in text
        for kw in ("技术开发", "技术服务", "软件开发", "软件服务", "软件定制", "委托开发", "系统集成")
    ):
        return "tech_service"
    return None


# 企业货物/服务形态的强信号词：出现在正文时，即便模型判了 tech_service 也纠正回来
# （真实合同走查 2026-09-10："汽车定点维修服务采购合同""软件代理销售协议"被判 tech，
# 导致按技术类基线误报 IP/保密/责任上限缺失）
_ENTERPRISE_FORM_KEYWORDS = ("代理销售", "购销", "买卖", "供货", "维修", "维保", "耗材", "租赁")
# tech 形态的强信号词：只认"开发/集成"类具体形态；"技术服务"四个字常出现在代理销售、
# 维修等非技术合同里，不能作为判 tech 的依据（故用"技术服务合同"而非裸"技术服务"）
_TECH_FORM_KEYWORDS = (
    "技术开发",
    "技术服务合同",
    "软件开发",
    "软件服务",
    "软件定制",
    "委托开发",
    "系统集成",
)


def _normalize_kind(kind: str | None, text: str) -> str | None:
    """按正文形态校正品类：企业货物/服务强信号（代理销售/供货/维修等）优先于 tech。

    判定口径：kind 非空、正文含企业形态强信号且不含 tech 强信号 → 改判 enterprise_goods；
    其余情况保持模型/关键词判据结果不变（只在"明显矛盾"时纠正，避免过度干预）。
    """
    if not kind or not text:
        return kind
    # 分支：正文含企业货物/服务强信号且无 tech 强信号 → 纠正为 enterprise_goods
    if any(kw in text for kw in _ENTERPRISE_FORM_KEYWORDS) and not any(
        kw in text for kw in _TECH_FORM_KEYWORDS
    ):
        return "enterprise_goods"
    return kind


def _parse_amount(value: str | int | float | None) -> Decimal | None:
    """把各种写法的金额字符串转为Decimal(元)。容忍千分位/单位/空格"""
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
    """把中文/ISO/斜杠三种日期字符串转为date对象。"""
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
    """百分比文本 → 数值口径(1.5% / 每日 1.5% / 20 → 1.5 / 1.5 / 20.0)。

    注意：口径与 rules 一致——存百分比数值而非小数(30 表示 30%)。
    千分号(‰)单独归一化: 真实示范文本常用 0.5‰(=0.05%), 若按 % 直读会偏大
    10 倍, 合规的 1.5‰ 会被误判成 1.5% 触发"违约金畸高".
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    # 分支 1：千分号写法（0.5‰）→ 数值 ÷10 折算成百分比
    permille = re.search(r"(\d+(?:\.\d+)?)\s*‰", text)
    if permille:
        return float(permille.group(1)) / 10.0
    # 分支 2：普通百分比/裸数（% 与"每日"等前缀由 LLM 原样抄回）
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _parse_int(value: str | int | float | None) -> int | None:
    """月数/天数文本 → int("24 个月"→24)；解析不到返回 None。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def _parse_months(value: str | int | float | None) -> int | None:
    """质保/保密期等「月数」字段 → int(2 年→24、6 个月→6、裸数→原值)。

    作用：真实合同常按「年」写期限(如校服合同「质保 2 年」),LLM 按"原样抄写"
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


def build_contract_model(raw: dict, text: str = "") -> ContractModel:
    """把 LLM 输出 dict 归一化成类型化 ContractModel,并回填字段证据。

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
        # 品类先按模型/关键词判据解析，再按正文形态校正（代理销售/维修等不会被判 tech）
        contract_kind=_normalize_kind(_parse_kind(raw.get("contract_kind")), text),
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
2. 字段值直接写内容本身（字符串或数字），不要把 {quote, clause_ref, confidence}
   对象当字段值；正文里找不到的字段填 null，且不要在 evidence 里编造；
3. payment_schedule 逐期输出：name（期次名）、amount（金额原文，只填金额数字，
   严禁把年份/日期等非金额数字当金额）、percent（占总额比例数值，如 20 表示 20%；
   正文没写比例就填 null）；一次性付清/整笔支付不是付款期次，此时输出空数组
   （只有正文明确列出多期/分期才逐期输出）；
4. evidence 输出为一个 JSON 对象：key 是字段名，value 是 {quote, clause_ref, confidence}。
   quote 必须是正文原句；clause_ref 填所在条款/章节号（如"第四条"，章节式文本填
   "一、质量要求"这类章节头，无条款结构填"前言"）；
   confidence：原文明确命中给 0.9+，有推断或表述含糊给 0.6~0.85，找不到的字段不写 key；
5. contract_kind 只从标题/首部/条款风格判断，不要凭正文金额猜：
   成品货物/耗材/代理销售/供货/维修保养/租赁类合同判 enterprise_goods；
   只有标的为软件开发、技术开发/服务、系统集成等信息技术服务交付才判 tech_service；
   政府采购/校服类判 gov_goods，农副产品买卖判 agri_goods；
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

    """
    text = str(exc)
    # 分支 1：报错里没有 completion 字样（接口/超时类异常）→ 无法还原
    marker = text.find("completion ")
    if marker < 0:
        return None
    start = text.find("{", marker)
    if start < 0:
        return None
    try:
        raw, _ = json.JSONDecoder().raw_decode(text, start)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


# 双读试点字段：尺子(决策 D30)实证付款期次跨次漂移最大(payment_schedule 0.37)，
# 二次抽取多数一致可压漂移；后续可按同一机制扩展其它低置信度/金额日期字段。
DOUBLE_READ_FIELDS: tuple[str, ...] = ("payment_schedule",)


def _single_read(structured, text: str) -> ContractModel:
    """调一次结构化抽取并归一化（漂移输出兜底；异常上抛由调用方决定是否吞）。"""
    try:
        result = structured.invoke([("system", _system_message()), ("human", text)])
    except Exception as exc:
        raw = _recover_completion(exc)
        # 这种情况是：解析失败但报错里带原始 completion → 归一化兜底后照常返回
        if raw is not None:
            return build_contract_model(_normalize_drifted(raw), text)
        # 这种情况是：还原失败（接口/超时/格式不支持）→ 原样抛出
        raise
    raw = result.model_dump() if hasattr(result, "model_dump") else result
    return build_contract_model(raw, text)


def _terms_signature(terms: list[PaymentTerm]) -> list[tuple]:
    """付款期次判同签名：[(金额, 比例)]——与字段尺子同一口径，忽略期次名差异。"""
    return [(t.amount, t.percent) for t in terms]


def _field_equal(field: str, a, b) -> bool:
    """双读字段判同：付款期次按期次签名比；其余字段直接等值（None 与空都算一致）。"""
    # 这种情况是：付款期次 → 序列签名比较（顺序敏感，与尺子/金额规则口径一致）
    if field == "payment_schedule":
        return _terms_signature(a or []) == _terms_signature(b or [])
    return a == b


def _merge_double_read(
    first: ContractModel,
    second: ContractModel,
    fields: tuple[str, ...] = DOUBLE_READ_FIELDS,
) -> ContractModel:
    """双读合并：对指定字段两读比对——一致用首读；不一致保留首读并标需人工。

    作用：LLM 抽取跨次漂移时（如付款期次比例 20% 偶发写成 0.2），二次抽取能
    暴露不一致；宁标人工复核也不静默采用可能错的一读。半填/空缺不算问题：
    两读都空视为一致（不脑补，呼应 D23 低置信度口径）。
    """
    meta = dict(first.extraction_meta)
    changed = False
    for field in fields:
        # 这种情况是：两读一致（含都为空）→ 采用首读，不动证据
        if _field_equal(field, getattr(first, field), getattr(second, field)):
            continue
        # 这种情况是：两读不一致 → 保留首读，把该字段标 needs_human_review
        evidence = meta.get(field)
        if evidence is None:
            evidence = Evidence(quote="", clause_ref="", confidence=0.0, needs_human_review=True)
        else:
            evidence = evidence.model_copy(update={"needs_human_review": True})
        meta[field] = evidence
        changed = True
    if not changed:
        return first
    return first.model_copy(update={"extraction_meta": meta})


def extract_contract(
    llm=None,
    text: str = "",
    double_read_fields: tuple[str, ...] = DOUBLE_READ_FIELDS,
) -> ContractModel:
    """对合同全文做结构化抽取: LLM 抄原文 → build 归一化回填证据（关键字段双读）。

    llm 可注入；不传则用默认 chat 模型（低温、关 thinking,
    抽取任务不需要深度推理，能显著降时延与成本）。
    double_read_fields: 需要二次抽取比对的关键字段（默认付款期次）；传空元组关闭。
    成本：开启时每份多 1 次 LLM 抽取调用（第二读失败不阻断，以首读为准）。
    """
    model = llm or get_chat_model(temperature=0.0, enable_thinking=False)
    structured = model.with_structured_output(ExtractionSchema, method="json_mode")
    first = _single_read(structured, text)
    # 这种情况是：没开双读 → 一次抽取即返回（向后兼容/评测对照）
    if not double_read_fields:
        return first
    try:
        second = _single_read(structured, text)
    except Exception as exc:
        # 这种情况是：第二读失败（限流/超时）→ 以首读为准，不阻断审查
        return first
    return _merge_double_read(first, second, double_read_fields)
