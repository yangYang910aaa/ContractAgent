"""
开放式条款降级 + 定位编排 + 文案修正
"""



from __future__ import annotations

import re

from backend.app.rules.constants import EFFECTIVE_FROM_SIGN_RE, SIGNING_EFFECT_RE
from backend.app.schemas import RiskItem, Severity
from backend.app.rules.locator import (
    _annotate_missing_locators,
    _clean_rule_text,
    _normalize_clause_refs,
)
from backend.app.rules.template import is_supplementary_agreement
from backend.app.rules.text_penalty import _CONF_OBLIGATION_RE


# 补充/变更协议里"由原合同继承"的必填字段：在补充件里缺席是常态，不是缺陷。
# 易错点：只剔除"继承型"字段（期限/币种），金额与甲乙方仍要查——补充协议改的往往
# 正是金额，写错了才是真问题。
_SUPPLEMENTARY_INHERITED_FIELDS = {"effective_date", "expiry_date", "currency"}

# 同理，"该不该有"类型的字段风险（缺保密期限/缺适用法律/缺成果归属/责任上限不明）
# 对补充协议也是继承型：原合同的对应条款继续有效。
# 注意别把"写得对不对"类一起剔掉——预付款超限、违约金过高、质保过短、金额不一致
# 都只在补充协议自己写了这些内容时才触发，是真缺陷，必须保留。
_SUPPLEMENTARY_INHERITED_TYPES = {
    "confidentiality_missing",  # 未约定保密期限
    "governing_law_missing",  # 缺适用法律/争议解决
    "ip_ownership_missing",  # 缺知识产权归属
    "liability_cap_unclear",  # 责任上限不明
}


# ---- 开放式条款语境标注 ----

# "按实/按月结算"类语境：合同不写固定总额是常态（月结、账期、按订单、框架协议）
_OPEN_AMOUNT_RE = re.compile(
    r"按实结算|据实结算|实报实销|按订单|按月结算|按月结|月结|每月结算|月度结算|结算周期|账期"
    r"|按实际发生|按批次结算|框架(?:协议|合同)|按需下单|对账后付款"
    r"|每月|每个月|按季|按季度|对账|对帐|结算单|结算上月|按供货批次"
    r"|订单要求|以订单为准|订单结算|订单方式|按批下单"
    # 农副类常用随行就市、过磅计量、批次收购定价，本就没有固定总额；
    # 不认这些口径就会把缺总额判成 high，误停闸口
    r"|随行就市|随市定价|保底价|浮动价|按质论价|按质计价|计量过磅|过磅|按等级|等级差价"
)


# "签字/盖章…生效"与"签订之日起生效"两个句式放在 constants，与生效日推断共用一份：
# 各写一份会漂移（窗口一处 20 一处 40），扫描件上就会漏判、把缺生效日判成 high。
# 具体的结束日期写法（"至 2025 年 12 月 31 日"）：有却抽不到才提示人工核对，
# 没有具体结束日期（以验收/履行完毕为界）→ 属开放式期限，降提示级
_CONCRETE_END_RE = re.compile(r"(?:至|到|截止)\s*\d{4}\s*年")


# 日期栏空白：出现"年 月 日"三连但中间没有数字（签署栏/期限栏未填）；
# 打码占位日期（"202*年*月*日"）同属"日期未定"。这两类判 high 会误停闸，只作提示级
_DATE_BLANK_RE = re.compile(
    r"(?<!\d)[\s*＊xX×·【】\[\]〔〕]{0,6}年"
    r"[\s*＊xX×·＿_\u3000【】\[\]〔〕]{0,6}月[\s*＊xX×·＿_\u3000【】\[\]〔〕]{0,6}日"
)


def annotate_open_ended_risks(risks: list[RiskItem], text: str) -> list[RiskItem]:
    """把"开放式条款"语境下的缺必填高风险降为提示级（保留风险并写明原因）。

    合同写"按实结算/长期有效/签字盖章之日起生效"时，抽不到金额或日期是常态，
    判高风险会误停闸口。降级会在建议里写明，不静默。返回新列表，不修改入参。
    """
    # 扫描件/PDF 文本先做统一清洗：去掉页标记、接回硬换行——后面的锚点与窗口判定、
    # 以及给用户看的摘录都按干净正文来（页标记占窗口预算，硬换行会把关键词切开）
    text = _clean_rule_text(text)
    if not text:
        return risks
    # 这种情况是：补充/变更协议 → 期限、币种这些字段由原合同继承，缺了不是缺陷；
    # 金额与甲乙方仍照查，补充协议改的往往正是金额
    if is_supplementary_agreement(text):
        risks = [
            risk
            for risk in risks
            if not (
                (
                    risk.risk_type == "missing_required_field"
                    and risk.field in _SUPPLEMENTARY_INHERITED_FIELDS
                )
                or risk.risk_type in _SUPPLEMENTARY_INHERITED_TYPES
            )
        ]
    # 保密期没抽到 ≠ 缺保密条款：先按正文语境把文案改准。
    # 放在开放式降级之前，且不受下方早退分支影响（没有开放式语境时也要修）
    risks = _refine_confidentiality_wording(risks, text)
    # 缺必填的原文定位：字段没抽到 → 证据天然为空，风险卡上就没有"原文定位"。
    # 这里按字段类型补"该去哪找"的锚点
    risks = _annotate_missing_locators(risks, text)
    # 条款号幻觉纠正：放在定位补完之后，因为纠正要用到摘录，而摘录可能正是上一步补上的
    risks = _normalize_clause_refs(risks, text)
    amount_open = _OPEN_AMOUNT_RE.search(text) is not None
    signing_effect = SIGNING_EFFECT_RE.search(text) is not None
    date_blank = _DATE_BLANK_RE.search(text) is not None
    # 生效日口径：正文写了"签订之日起生效/自签署生效"或日期栏空白 → 无具体签署日期
    effective_open = signing_effect or date_blank or EFFECTIVE_FROM_SIGN_RE.search(text) is not None
    # 到期日口径：正文没有"至 YYYY 年"的具体结束日期（如只写"有效期 N 年/长期"、
    # 以验收或履行完毕为界、日期栏空白）→ 开放式期限；写了具体结束日期却抽不到 → 保留 high
    expiry_open = date_blank or _CONCRETE_END_RE.search(text) is None
    # 分支：三种语境都没有 → 原样返回（不是开放式合同，缺字段照常 high）
    if not (amount_open or effective_open or expiry_open):
        return risks
    out: list[RiskItem] = []
    for risk in risks:
        # 分支：仅处理"缺必填"的 high，其余风险（含已有 medium）原样保留
        if risk.risk_type == "missing_required_field" and risk.severity == Severity.high:
            if risk.field == "total_amount" and amount_open:
                out.append(
                    risk.model_copy(
                        update={
                            "severity": Severity.medium,
                            "suggestion": risk.suggestion
                            + " 正文按实/按月结算、未列明合同总额：本条已降为提示级（不阻断审批），"
                            "请人工确认结算上限或补充金额条款。",
                        }
                    )
                )
                continue
            if risk.field == "expiry_date" and expiry_open:
                out.append(
                    risk.model_copy(
                        update={
                            "severity": Severity.medium,
                            "suggestion": risk.suggestion
                            + " 正文未写具体到期日（空白日期栏或只写'有效期 N 年/长期'）："
                            "本条已降为提示级（不阻断审批），请人工确认起止日期。",
                        }
                    )
                )
                continue
            if risk.field == "effective_date" and effective_open:
                out.append(
                    risk.model_copy(
                        update={
                            "severity": Severity.medium,
                            "suggestion": risk.suggestion
                            + " 正文未写具体签署日期（签字盖章生效 / 空白日期栏）：本条已降为"
                            "提示级（不阻断审批），请人工确认签署/生效日期。",
                        }
                    )
                )
                continue
        out.append(risk)
    return out


def _refine_confidentiality_wording(risks: list[RiskItem], text: str) -> list[RiskItem]:
    """保密期没抽到时把文案改准：正文有保密义务就不说"缺少保密条款"。

    判据只是"期限没抽到"，而正文常常写了保密义务，照原文案会被读成误报。
    返回新列表，不修改入参。
    """
    if not (text or "") or not _CONF_OBLIGATION_RE.search(text):
        return risks
    out: list[RiskItem] = []
    for risk in risks:
        # 分支：保密期字段没抽到、但正文确有保密义务 → 只改文案，类型/严重级不动
        if risk.risk_type == "confidentiality_missing":
            out.append(
                risk.model_copy(
                    update={
                        "suggestion": (
                            "正文已约定保密义务，但未明确保密期限，建议按 P-04 补充"
                            "（保密期宜 24 个月以上、不超过 36 个月）。"
                        )
                    }
                )
            )
            continue
        out.append(risk)
    return out
