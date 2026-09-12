"""
文本级规则 P-06~P-09(验收 / 发票 / 担保 / 转包)
"""

from __future__ import annotations

from decimal import Decimal
import re

from backend.app.schemas import RiskItem, Severity
from backend.app.rules.constants import PERFORMANCE_BOND_MIN_TOTAL, RISK_LABELS, SUBCONTRACT_KINDS, TEXT_RULE_KINDS
from backend.app.rules.locator import _clause_ref_at, _clean_page_marks, _text_excerpt
from backend.app.rules.template import is_blank_template_suspect, is_supplementary_agreement
from backend.app.rules.text_data import _DATA_INVOLVED_RE, _check_cross_border, _check_data_deletion, _check_data_processing_terms, _check_personal_info_missing
from backend.app.rules.text_penalty import _check_confidentiality_no_exception, _check_penalty_basis_unclear, _check_penalty_cap_missing


# 判定"有验收安排"的信号：验收词出现后，其附近 ±_ACCEPT_WINDOW 内要有"标准/依据"类
# 或"期限/时间"类实义词才算有验收安排；否则只算"提了验收"（口径：不咬文嚼字，但也不
# 允许只有一句"验收合格后付款"就当作有验收标准——P-06 要的是可执行的标准与时限）
# 易错点：不能把"验收合格后"当标准信号（那是付款触发点，不是验收标准）；
# 也不能全文搜"标准/日内"（质保"7 日内维修"会误当验收期限）
_ACCEPT_STD_RE = re.compile(r"标准|规范|技术(?:要求|条件|参数)|说明书|依据|为准|验收报告")


_ACCEPT_TIME_RE = re.compile(r"期限|日(?:内|前)|天内|小时(?:内|前)|时间|日期|前完成|完成验收")


_ACCEPT_WINDOW = 200  # 验收词两侧的检索窗口（字符），覆盖整句验收条款


# 付款安排上下文词：发票义务依附于付款安排，正文根本没有付款/结算约定的合同不套 P-07
_PAYMENT_CONTEXT_RE = re.compile(r"付款|支付|结算|收款|款项|货款")


# 发票约定信号词：出现任意一个即视为已约定开票义务（含"先票后款"等实务写法）
_INVOICE_RE = re.compile(r"发票|开票|凭票|先票后款|票到")


# P-08 履约担保信号词：出现任意一个即视为有担保安排（保证/保函/保证金/质保金）
_BOND_RE = re.compile(r"履约保证|履约保函|履约担保|银行保函|保证金|质保金")


# 预付信号：P-08 的另一触发条件（含预付期次即查，哪怕总额不足 100 万）；
# 与 P-01 口径一致，"首付款"也计入预付款
_PREPAY_RE = re.compile(r"预付|首付|备料款|启动款")


# 转包限制句信号：命中即视为已限制转包/分包（覆盖"不得转包""转包须经甲方同意"
# "未经甲方书面同意不得转委托"三种真实写法）
_SUBCONTRACT_RESTRICT_RE = re.compile(
    r"不得.{0,16}(?:转包|分包|转委托)"
    r"|未经(?:甲方|采购方|委托方).{0,20}(?:同意|许可|批准).{0,16}(?:转包|分包|转委托)"
    r"|(?:转包|分包|转委托).{0,16}(?:须|需|应)经(?:甲方|采购方|委托方).{0,12}(?:书面)?(?:同意|批准|许可)"
    r"|禁止(?:转包|分包)"
    # 官方示范文本常见"勾选式作答"：（2）否 ☑ 表示不允许转委托（科技部/政采采购文本）
    # 易错点：PDF 抽取会把选项折行，必须用 [\s\S] 跨行匹配，不能用 [^。\n]
    r"|(?:转包|分包|转委托)[\s\S]{0,80}?[（(]?2[）)]?\s*否\s*[☑√✓×]"
    r"|是否[\s\S]{0,30}?(?:转包|分包|转委托)[\s\S]{0,80}?否\s*[☑√✓×]"
)


# 转包免责（high）信号：明确允许任意转包且甲方无权追责——比"未限制"更严重，直接 high。
# 易错点：禁止裸匹配"甲方无权/不得…"（正常合同也有"甲方不得泄露保密信息"类表述），
# 免责句必须与"转包/分包"同语境出现才算（如"可任意转包""转包无须经甲方同意"）。
_SUBCONTRACT_WAIVER_RE = re.compile(
    r"可(?:以)?任意转包|有权(?:自行|任意)?转包"
    r"|转包.{0,12}(?:无需|无须|不需)(?:征得|经)?(?:甲方|采购方|委托方).{0,6}(?:同意|许可)"
    r"|(?:转包|分包).{0,40}(?:甲方|采购方|委托方)(?:无权|不得).{0,12}(?:追责|要求承担)"
    r"|(?:转包|分包).{0,24}(?:与甲方|与采购方)无关|因转包.{0,12}(?:甲方|采购方)(?:不承担|概不负责)"
)


def _looks_large_total(text: str) -> bool:
    """原文是否写明了 ≥100 万的合同总额（P-08 金额门槛，纯文本判断）。

    判定策略：优先找"总额/总价/总金额/合同价款"等关键词后的首个阿拉伯金额，
    兼容"（大写）壹佰万元整（小写：1,000,000 元"的样本写法与"N 万元"写法；
    金额形态认不出（如纯大写/空白模板）→ 返回 False（宁缺毋滥，不误报）。
    """
    # 关键词后紧跟金额：允许中间隔着 人民币/（大写）…（小写）： 等前缀
    anchored = re.compile(
        r"(?:总额|总价|总金额|合同价款|合同金额|合同总价款|价款总额|采购总价|开发费总额)"
        r"[为是：:（(]{0,3}(?:人民币)?[（(]?大写[）)]?[^0-9]{0,24}?[（(]?小写[）)]?[：:]?\s*"
        r"([\d,]+(?:\.\d+)?)\s*(万元|万|元)?"
    )
    for m in anchored.finditer(text):
        value = _amount_to_yuan(m.group(1), m.group(2) or "")
        if value is not None and value >= PERFORMANCE_BOND_MIN_TOTAL:
            return True
    # 兜底：总额关键词附近（40 字内）出现 "N 万元" 独立金额（真实合同常只写万元）
    loose = re.compile(
        r"(?:总额|总价|总金额|合同价款|合同金额|合同总价款).{0,40}?([\d.]+)\s*万\s*元"
    )
    for m in loose.finditer(text):
        try:
            if Decimal(m.group(1)) * 10000 >= PERFORMANCE_BOND_MIN_TOTAL:
                return True
        except Exception:
            continue
    return False


def _amount_to_yuan(digits: str, unit: str) -> Decimal | None:
    """阿拉伯金额串 + 单位 → 元；单位是万/元时换算，解析失败返回 None。"""
    try:
        value = Decimal(digits.replace(",", ""))
    except Exception:
        return None
    # 这种情况是：写了"万元/万" → 翻万倍；其余（"元"或没写单位）按元
    return value * 10000 if "万" in unit else value


def _check_acceptance_unclear(text: str) -> RiskItem | None:
    """P-06 验收安排检查：正文无"验收"或只提验收、无标准/期限 → medium。"""
    # 分支 1：全文没有验收字样 → 连验收安排都没有，直接提示补条款
    if "验收" not in text:
        return RiskItem(
            risk_type="acceptance_unclear",
            label=RISK_LABELS["acceptance_unclear"],
            severity=Severity.medium,
            clause_ref="",
            # 这条是"全篇找不到"类：没有可指的原文，**不能拿文档开头凑数**——
    # 用户点"原文定位"会跳到抬头，反而更困惑。
            # 前端对"无条款号且无摘录"已有明说提示，比指错地方诚实。
            evidence="",
            policy_ref="P-06",
            suggestion="合同未约定交付验收安排（验收标准与验收期限），建议按 P-06 补充验收条款。",
            field=None,
        )
    # 分支 2：验收字样出现，但每个出现位置的近旁都找不到标准/期限信号 →
    #    只算"提了验收"（如付款触发句），不算有可执行的验收安排
    positions = [m.start() for m in re.finditer("验收", text)]
    for pos in positions:
        window = text[max(pos - _ACCEPT_WINDOW, 0) : pos + _ACCEPT_WINDOW]
        if _ACCEPT_STD_RE.search(window) or _ACCEPT_TIME_RE.search(window):
            return None
    return RiskItem(
        risk_type="acceptance_unclear",
        label=RISK_LABELS["acceptance_unclear"],
        severity=Severity.medium,
        clause_ref=_clause_ref_at(text, positions[0]),
        evidence=_text_excerpt(text, positions[0]),
        policy_ref="P-06",
        suggestion="正文仅笼统提及验收，未明确验收标准与验收期限，建议按 P-06 补充可执行条款。",
        field=None,
    )


def _check_invoice_unclear(text: str) -> RiskItem | None:
    """P-07 发票约定检查：有付款安排但全文无发票/开票字样 → medium。"""
    # 分支 1：正文没有付款/结算安排 → 发票义务无从依附，不套本规则
    if not _PAYMENT_CONTEXT_RE.search(text):
        return None
    # 分支 2：已有发票/开票约定 → 合规
    if _INVOICE_RE.search(text):
        return None
    # 分支 3：有付款安排却完全没提开票义务 → medium 提示约定增值税发票与先票后款
    m = _PAYMENT_CONTEXT_RE.search(text)
    return RiskItem(
        risk_type="invoice_unclear",
        label=RISK_LABELS["invoice_unclear"],
        severity=Severity.medium,
        clause_ref=_clause_ref_at(text, m.start()),
        evidence=_text_excerpt(text, m.start()),
        policy_ref="P-07",
        suggestion="合同约定了付款安排但未约定发票开具义务，建议按 P-07 补充增值税发票与先票后款约定。",
        field=None,
    )


def _check_performance_bond_missing(text: str) -> RiskItem | None:
    """P-08 履约担保检查：大额（≥100 万）或含预付的合同缺担保安排 → medium。"""
    has_prepay = _PREPAY_RE.search(text) is not None
    is_large = _looks_large_total(text)
    # 分支 1：既非大额也无预付 → 不在担保审查范围（小额现货采购不强制要保函）
    if not has_prepay and not is_large:
        return None
    # 分支 2：已有履约保证/保函/保证金/质保金安排 → 合规
    if _BOND_RE.search(text):
        return None
    # 分支 3：大额或含预付却无任何担保 → medium（供应商跑路风险敞口）
    anchor = _PREPAY_RE.search(text) or _BOND_RE.search(text)
    pos = anchor.start() if anchor else (text.find("总价") if "总价" in text else 0)
    return RiskItem(
        risk_type="performance_bond_missing",
        label=RISK_LABELS["performance_bond_missing"],
        severity=Severity.medium,
        clause_ref=_clause_ref_at(text, pos),
        evidence=_text_excerpt(text, pos),
        policy_ref="P-08",
        suggestion=(
            "合同金额较大或含预付款，却未约定履约担保（履约保函/保证金），"
            "建议按 P-08 要求供应商提供履约担保后再付款。"
        ),
        field=None,
    )


def _check_subcontract_unrestricted(text: str) -> RiskItem | None:
    """P-09 转包/分包检查：未限制转包 → medium；明确"任意转包且甲方无权追责" → high。"""
    # 分支 1：明文允许任意转包且甲方无权追责 → high（示范 high 缺陷，评测闸口验证点）
    waiver = _SUBCONTRACT_WAIVER_RE.search(text)
    if waiver:
        return RiskItem(
            risk_type="subcontract_unrestricted",
            label=RISK_LABELS["subcontract_unrestricted"],
            severity=Severity.high,
            clause_ref=_clause_ref_at(text, waiver.start()),
            evidence=_text_excerpt(text, waiver.start()),
            policy_ref="P-09",
            suggestion=(
                "合同允许乙方任意转包且甲方无权追责，供应商履约主体与质量失去控制，"
                "建议按 P-09 删除该免责表述并约定转包须经甲方书面同意。"
            ),
            field=None,
        )
    # 分支 2：已有不得转包/分包限制句 → 合规
    if _SUBCONTRACT_RESTRICT_RE.search(text):
        return None
    # 分支 3：完全未限制转包 → medium（定制交付依赖乙方自身履约能力）
    return RiskItem(
        risk_type="subcontract_unrestricted",
        label=RISK_LABELS["subcontract_unrestricted"],
        severity=Severity.medium,
        clause_ref="",
        # 同上：全篇没有可指的原文，宁可不给定位（frontend 会明说"没有可直接指路的表述"）
        evidence="",
        policy_ref="P-09",
        suggestion="合同未限制乙方转包/分包，建议按 P-09 约定转包须经甲方书面同意。",
        field=None,
    )


def text_rules(text: str, kind: str | None) -> list[RiskItem]:
    """按条款清单查原文，返回条款级风险（不依赖抽取字段）。

    查两类问题：该写的条款有没有写（验收、发票、担保、转包、数据合规），写了的条款
    写得对不对（保密例外、违约金基数与上限）；风险带政策编号与原文摘录，便于溯源。
    三种情况整组不查：疑似空白模板、品类不在触发集、补充/变更协议（条款继承原合同）。
    """
    text = _clean_page_marks(text)
    # 这种情况是：原文疑似空白/未定稿模板 → 不谈条款完备性
    if is_blank_template_suspect(text or ""):
        return []
    effective_kind = kind or "enterprise_goods"
    # 这种情况是：品类不在触发集（gov 豁免）→ 整组规则不跑
    if effective_kind not in TEXT_RULE_KINDS:
        return []
    # 这种情况是：补充/变更协议 → 条款完备性不适用，只查"写得对不对"那几条
    if is_supplementary_agreement(text):
        supplement: list[RiskItem] = []
        for check in (
            _check_confidentiality_no_exception,
            _check_penalty_basis_unclear,
            _check_penalty_cap_missing,
        ):
            risk = check(text)
            if risk:
                supplement.append(risk)
        return supplement
    out: list[RiskItem] = []
    # 分支收集：每条规则独立判定，命中才追加（顺序固定便于测试/展示）
    acceptance = _check_acceptance_unclear(text)
    if acceptance:
        out.append(acceptance)
    invoice = _check_invoice_unclear(text)
    if invoice:
        out.append(invoice)
    bond = _check_performance_bond_missing(text)
    if bond:
        out.append(bond)
    # 转包/分包只约束定制/工程交付形态（农副/政采无此概念）
    if effective_kind in SUBCONTRACT_KINDS:
        subcontract = _check_subcontract_unrestricted(text)
        if subcontract:
            out.append(subcontract)
    # 数据合规（P-10~P-12）：仅当正文涉及个人信息/用户数据处理才跑（触发前置门）
    if _DATA_INVOLVED_RE.search(text):
        for check in (
            _check_personal_info_missing,
            _check_data_processing_terms,
            _check_cross_border,
            _check_data_deletion,
        ):
            risk = check(text)
            if risk:
                out.append(risk)
    # 保密例外、违约金基数、违约金上限：三条都属"写得对不对"，与上面"有没有写"互补
    for check in (
        _check_confidentiality_no_exception,
        _check_penalty_basis_unclear,
        _check_penalty_cap_missing,
    ):
        risk = check(text)
        if risk:
            out.append(risk)
    return out
