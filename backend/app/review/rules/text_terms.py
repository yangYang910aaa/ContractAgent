"""文本级规则 P-15(格式条款与免责限制)。"""

from __future__ import annotations

import re

from backend.app.review.rules.constants import RISK_LABELS
from backend.app.review.rules.locator import _clause_ref_at
from backend.app.review.rules.text_penalty import _sentence_span
from backend.app.schemas import RiskItem, Severity

# 概括免责：只认绝对式（"概不负责/不承担任何责任"），"不承担违约责任"这类有限免责不在此列
_BLANKET_RE = re.compile(r"概不负责|不承担任何(?:责任|损失|赔偿)|不承担一切责任|不负任何责任")

# 数据/个人信息安全责任免除：两种语序都认（免责词在前 / 泄露事件在后）
_DATA_EXEMPTION_RE = re.compile(
    r"(?:不承担|不负责|概不负责|免除|不予赔偿)[^。；]{0,20}(?:数据|个人信息|网络安全)[^。；]{0,12}(?:责任|赔偿|损失)"
    r"|(?:数据|个人信息|网络安全)[^。；]{0,20}(?:泄露|丢失|被非法使用|安全事件)[^。；]{0,12}"
    r"(?:不承担|不负责|概不负责|免除)"
)

# 买方自担使用风险：把供方交付物的后果整体推给买方
_BUYER_RISK_RE = re.compile(
    r"(?:甲方|买方|需方)[^。；]{0,10}(?:自行承担|自担)[^。；]{0,12}(?:使用风险|全部风险|风险|后果)"
)

# 正当免责护栏：不可抗力、法定免除、对方违约在先、迟延履行不免除、买方自身权利
_GUARD_RE = re.compile(
    r"不可抗力|法律规定|依法(?:可以)?免除|另有规定|不免除"
    r"|甲方(?:的)?(?:原因|责任)|对方(?:的)?(?:原因|责任)|买方有权|甲方有权"
)

# 主体词：免责句的主语是供方侧才报；认不出主体不报（宁缺毋滥）
_ACTOR_RE = re.compile(r"乙方|供方|卖方|供应商|承包方|承接方|甲方|买方|需方|出租方|承租方")
_SUPPLIER_ACTORS = {"乙方", "供方", "卖方", "供应商", "承包方", "承接方"}


def _check_unfair_exemption(text: str) -> list[RiskItem]:
    """P-15 过度免责检查：供方概括免责/数据安全责任免除/买方自担使用风险。

    口径（买方视角，方向性）：只报对买方不利的供方免责——概括式免责 medium、
    免责覆盖数据与个人信息安全 high、交付物风险整体推给买方 medium。
    正当免责不报（不可抗力、法定免除、对方违约在先、买方自身权利）；
    免责句主语认不出是供方侧也不报。每类形态最多报一条，命中位置重叠时取更重的那条。
    """
    items: list[RiskItem] = []
    # 数据责任免除与概括免责可能命中同一句：先收重的那条，概括免责跳过重叠位置
    data_span = _first_hit(text, _DATA_EXEMPTION_RE)
    if data_span is not None:
        risk = _build_risk(text, data_span, kind="data", severity=Severity.high)
        if risk is not None:
            items.append(risk)
    blanket_span = _first_hit(text, _BLANKET_RE)
    # 分支：概括免责与数据责任免除命中同一处 → 已按数据责任报过，不重复出卡
    if blanket_span is not None and not (data_span is not None and _overlaps(blanket_span, data_span)):
        risk = _build_risk(text, blanket_span, kind="blanket", severity=Severity.medium)
        if risk is not None:
            items.append(risk)
    # 买方自担风险的句式本身就把主体写成买方，不再套"主语须是供方"那道闸
    buyer_span = _first_hit(text, _BUYER_RISK_RE, require_supplier=False)
    if buyer_span is not None:
        risk = _build_risk(text, buyer_span, kind="buyer_risk", severity=Severity.medium)
        if risk is not None:
            items.append(risk)
    return items


def _first_hit(
    text: str, pattern: re.Pattern, require_supplier: bool = True
) -> tuple[int, int] | None:
    """取正则在正文里第一处"通得过护栏与主体判定"的位置（起止下标），没有返回 None。"""
    for matched in pattern.finditer(text):
        start, end = _sentence_span(text, matched.start())
        sentence = text[start:end]
        # 分支：该句属正当免责（不可抗力/法定/对方违约/买方权利）→ 换个位置继续找
        if _GUARD_RE.search(sentence):
            continue
        # 分支：免责句主语不是供方侧（如"需方不承担任何责任"）→ 不报
        if require_supplier and not _supplier_subject(text, matched.start()):
            continue
        return matched.start(), matched.end()
    return None


def _supplier_subject(text: str, pos: int) -> bool:
    """免责短语的主语是不是供方侧：取所在小句（按句/分号/换行/逗号切）的首个主体词。"""
    clause = re.split(r"[。；\n，]", text[max(pos - 60, 0) : pos])[-1]
    actors = _ACTOR_RE.findall(clause)
    return bool(actors) and actors[0] in _SUPPLIER_ACTORS


def _overlaps(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """两处命中是否落在同一段（起止区间相交）。"""
    return left[0] <= right[1] and right[0] <= left[1]


def _build_risk(text: str, span: tuple[int, int], kind: str, severity: Severity) -> RiskItem | None:
    """按命中位置组装风险卡；`kind` 决定建议文案（同一风险类型下三种免责形态）。"""
    pos = span[0]
    suggestion = {
        "blanket": (
            "供方对任何损失概括免责，建议按 P-15 改为与过错程度相称、双方对等的责任限制，"
            "并写明故意与重大过失不得预先免除。"
        ),
        "data": (
            "免责条款覆盖了数据与个人信息安全责任，建议按 P-15 删除该免责，明确供方对数据泄露、"
            "个人信息被非法使用承担赔偿责任。"
        ),
        "buyer_risk": (
            "交付物使用风险整体推给买方，建议按 P-15 明确供方对其交付物质量与安全负责，"
            "把风险分配与责任边界写清。"
        ),
    }[kind]
    # 证据只取命中的那一句（用 ±60 字窗口会把上一条的末句一起带进来，展示与定位都变脏）
    start, end = _sentence_span(text, pos)
    evidence = re.sub(r"\s+", "", text[start:end]).strip()[:120]
    return RiskItem(
        risk_type="unfair_exemption_clause",
        label=RISK_LABELS["unfair_exemption_clause"],
        severity=severity,
        clause_ref=_clause_ref_at(text, pos),
        evidence=evidence,
        policy_ref="P-15",
        suggestion=suggestion,
        field=None,
    )
