"""
数据合规 P-10~P-12
"""

from __future__ import annotations

import re

from backend.app.schemas import RiskItem, Severity
from backend.app.rules.constants import RISK_LABELS
from backend.app.rules.locator import _clause_ref_at, _text_excerpt


# ---- 数据与个人信息合规文本规则 ----

# 触发前置门：只有正文确实涉及个人信息/用户数据处理的合同才启用本组规则，
# 纯货物买卖等整批跳过。
# 易错点：不要用裸"数据"做门——技术开发类合同常出现"数据平台/数据资产"，
# 但并不处理个人信息，套用会误报。
_DATA_INVOLVED_RE = re.compile(
    r"个人信息|个人数据|隐私|用户信息|用户数据|用户资料|顾客信息|客户信息|员工信息|学生信息"
)


# 个人信息/数据保护义务条款信号（有其一即视为已约定保护义务）
_DATA_PROTECT_RE = re.compile(r"个人信息保护|数据保护|隐私保护|数据安全|信息安全|数据合规")


# 委托处理要件信号（P-10 要求：目的/期限/方式/种类/保护措施/删除返还）
_DATA_TERM_SIGNALS = {
    "目的": re.compile(r"处理目的|使用目的|服务目的"),
    "期限": re.compile(r"处理期限|保存期限|存储期限|服务期限"),
    "方式": re.compile(r"处理方式|使用方式"),
    "种类": re.compile(r"信息种类|数据类型|信息类型|数据范围|信息范围"),
    "措施": re.compile(r"保护措施|安全措施|加密|脱敏|去标识|权限管理"),
    "删除": re.compile(r"删除|销毁|返还|匿名化"),
}


# 数据出境/境外处理信号 + 三条合规路径（安全评估/标准合同/保护认证）
_CROSS_BORDER_RE = re.compile(r"数据出境|出境|境外|跨境|海外|境外服务器|境外机构")


_CROSS_BORDER_PATH_RE = re.compile(r"安全评估|标准合同|保护认证|个人信息保护认证|出境评估")


# 删除/返还义务与安全事件通知义务（P-12）
_DELETION_RE = re.compile(r"删除|销毁|返还|匿名化")


_BREACH_NOTICE_RE = re.compile(r"泄露|安全事件|事件通知|告知义务|应急")


def _check_personal_info_missing(text: str) -> RiskItem | None:
    """P-10：涉及个人信息处理却无个人信息/数据保护义务条款 → medium。"""
    if _DATA_PROTECT_RE.search(text):
        return None
    m = _DATA_INVOLVED_RE.search(text)
    return RiskItem(
        risk_type="personal_info_clause_missing",
        label=RISK_LABELS["personal_info_clause_missing"],
        severity=Severity.medium,
        clause_ref=_clause_ref_at(text, m.start()),
        evidence=_text_excerpt(text, m.start()),
        policy_ref="P-10",
        suggestion=(
            "合同涉及个人信息/用户数据处理，但未约定个人信息保护与数据安全义务"
            "（保密条款不能替代，建议按 P-10 补充）。"
        ),
        field=None,
    )


def _check_data_processing_terms(text: str) -> RiskItem | None:
    """P-10：委托处理要件不全（目的/期限/方式/种类/措施/删除返还 命中 <3 项）→ medium。"""
    hit = [name for name, rx in _DATA_TERM_SIGNALS.items() if rx.search(text)]
    # 分支：要件命中 ≥3 项 → 视为要件基本完整，不提示
    if len(hit) >= 3:
        return None
    m = _DATA_INVOLVED_RE.search(text)
    missing = "、".join(name for name in _DATA_TERM_SIGNALS if name not in hit)
    return RiskItem(
        risk_type="data_processing_terms_missing",
        label=RISK_LABELS["data_processing_terms_missing"],
        severity=Severity.medium,
        clause_ref=_clause_ref_at(text, m.start()),
        evidence=_text_excerpt(text, m.start()),
        policy_ref="P-10",
        suggestion=(
            f"数据处理条款要件不完整（缺：{missing}），建议按 P-10 约定处理目的、期限、"
            "方式、信息种类、保护措施与删除/返还义务。"
        ),
        field=None,
    )


def _check_cross_border(text: str) -> RiskItem | None:
    """P-11：约定数据出境/境外处理却无合规路径（评估/标准合同/认证）→ high。"""
    m = None
    for candidate in _CROSS_BORDER_RE.finditer(text):
        # 这种情况是：出现"不涉及出境/无境外访问"等否定句 → 不是出境安排，跳过
        # （易错点：正文常写"本项目全部数据在境内处理，不涉及出境"来声明合规）
        if re.search(r"[不无未非]", text[max(candidate.start() - 6, 0) : candidate.start() + 2]):
            continue
        m = candidate
        break
    # 分支：没有（肯定的）出境/境外信号 → 不适用本规则
    if m is None:
        return None
    # 分支：已写明任一合规路径 → 合规
    if _CROSS_BORDER_PATH_RE.search(text):
        return None
    return RiskItem(
        risk_type="data_cross_border_unclear",
        label=RISK_LABELS["data_cross_border_unclear"],
        severity=Severity.high,
        clause_ref=_clause_ref_at(text, m.start()),
        evidence=_text_excerpt(text, m.start()),
        policy_ref="P-11",
        suggestion=(
            "合同涉及数据出境/境外处理，却未约定安全评估、标准合同或保护认证等合规路径，"
            "建议按 P-11 补充后再签署。"
        ),
        field=None,
    )


def _check_data_deletion(text: str) -> RiskItem | None:
    """P-12：既无数据删除/返还义务、也无泄露等安全事件通知义务 → medium。"""
    if _DELETION_RE.search(text) or _BREACH_NOTICE_RE.search(text):
        return None
    m = _DATA_INVOLVED_RE.search(text)
    return RiskItem(
        risk_type="data_deletion_missing",
        label=RISK_LABELS["data_deletion_missing"],
        severity=Severity.medium,
        clause_ref=_clause_ref_at(text, m.start()),
        evidence=_text_excerpt(text, m.start()),
        policy_ref="P-12",
        suggestion=(
            "合同未约定数据删除/返还义务，也未约定数据泄露等安全事件的告知与补救义务，"
            "建议按 P-12 补充数据善后条款。"
        ),
        field=None,
    )
