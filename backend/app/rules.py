"""确定性规则引擎。

输入 ContractModel → 输出 RiskItem[] + 评级。纯函数、无 LLM 调用，可离线单测。
政策阈值集中定义在本文件顶部

风险等级约定：存在 high → gate 人工审批；只有 medium/low → 有条件通过。

"""

from __future__ import annotations

import re
from decimal import Decimal

from backend.app.schemas import ContractModel, Grade, PaymentTerm, RiskItem, Severity

# ---- 政策阈值（百分比数值；金额单位：元）----
PREPAY_MAX_PERCENT = 30.0  # P-01：预付款不超过总额 30%
WARRANTY_MIN_MONTHS = 12  # P-02：质保期不少于 12 个月
LIABILITY_CAP_MIN_PERCENT = 50.0  # P-03：责任上限不低于总额 50%
CONFIDENTIALITY_MAX_MONTHS = 36  # P-04：保密期不超过 36 个月
PENALTY_DAILY_MAX_PERCENT = 1.0  # 违约金日利率上限
AMOUNT_TOLERANCE_RATIO = Decimal("0.01")  # 分项加总 vs 总额允许偏差 1%

# 风险类型机器码 → 中文展示名（risk_type 是评测/接口对齐的编码，展示永远走
# 中文 label；新增风险类型时必须在此登记，否则界面会裸显机器码）
RISK_LABELS: dict[str, str] = {
    "missing_required_field": "缺失必填字段",
    "date_logic_effective_before_signature": "生效日早于签署日",
    "date_logic_expiry_not_after_effective": "到期日不晚于生效日",
    "amount_inconsistency": "付款金额不一致",
    "prepayment_ratio_high": "预付款比例过高",
    "warranty_too_short": "质保期不足",
    "liability_cap_unclear": "责任上限未明确",
    "liability_cap_too_low": "责任上限过低",
    "confidentiality_missing": "缺少保密条款",
    "confidentiality_too_long": "保密期过长",
    "penalty_rate_too_high": "违约金比例畸高",
    "ip_ownership_missing": "未约定知识产权归属",
    "ip_ownership_unclear": "知识产权归属不清",
    "governing_law_missing": "缺少适用法律约定",
    "blank_template_suspected": "疑似空白模板",
}

# ContractModel 字段 key → 中文名：用于建议文案/UI 展示（与前端 labels 对齐；
# 别让 effective_date 这类英文 key 出现在给人看的句子里）
FIELD_LABELS: dict[str, str] = {
    "contract_kind": "合同品类",
    "buyer": "甲方（采购方）",
    "supplier": "乙方（供应商）",
    "signature_date": "签署日期",
    "effective_date": "生效日期",
    "expiry_date": "到期日",
    "total_amount": "合同总额",
    "currency": "币种",
    "payment_schedule": "付款计划",
    "penalty_rate": "违约金日利率",
    "liability_cap": "责任上限",
    "warranty_months": "质保期",
    "termination_notice_days": "解约通知期",
    "ip_ownership": "知识产权归属",
    "confidentiality_months": "保密期",
    "governing_law": "适用法律",
}

# 必填核心字段：缺失会削弱整份审查的可信度
CORE_REQUIRED = ("buyer", "supplier", "effective_date", "expiry_date", "total_amount", "currency")
# 金额/日期缺失视为 high（审查无法继续）；主体信息缺失降为 medium
HIGH_IF_MISSING = ("total_amount", "effective_date", "expiry_date")

# 品类应含条款基线：某字段只在基线内才做"缺失 → medium"检查。
# 背景：政采校服类天然不写责任上限/保密/IP/适用法律；农副类有保密与适用法律但无责任
# 上限/IP——没有品类感知会把"品类正常的省略"误报成风险（易错点）。
# None（历史数据/未分类）按 enterprise_goods 全量处理，向后兼容。
KIND_BASELINE: dict[str, set[str]] = {
    "enterprise_goods": {"liability_cap", "confidentiality_months", "ip_ownership", "governing_law"},
    "gov_goods": set(),
    "agri_goods": {"confidentiality_months", "governing_law"},
    "tech_service": {"liability_cap", "confidentiality_months", "ip_ownership", "governing_law"},
}

# IP 权属合规判断用关键词（合同以甲方/采购方视角表述）
_BUYER_KEYWORDS = ("甲方", "采购方")


def _quote(model: ContractModel, field: str) -> str:
    """取某字段的原文证据（来自 extraction_meta）。

    作用：风险项 evidence 默认引用抽取阶段留存的原文摘录；
    没抽到就返回空串，规则不因证据缺失而中断。
    """
    meta = model.extraction_meta.get(field)
    return meta.quote if meta else ""


def _clause_ref(model: ContractModel, field: str) -> str:
    """取某字段的证据条款引用（如「第二条」）；没抽到返回空串。"""
    meta = model.extraction_meta.get(field)
    return meta.clause_ref if meta else ""


def _mk(
    model: ContractModel,
    risk_type: str,  # 风险类型编码（评测 ground truth 按此对齐）
    severity: Severity,  # 风险等级
    field: str,  # 关联 ContractModel 字段名
    suggestion: str,  # 整改建议文案
    policy_ref: str | None = None,  # 对应政策编号（P-01..P-05）
    evidence: str | None = None,  # 覆盖默认原文证据（金额类"计算型证据"用）
) -> RiskItem:
    """RiskItem 小工厂：统一拼证据与条款引用，避免每处规则重复写。

    默认证据取自 extraction_meta 的原文摘录；调用方可传 evidence 覆盖，
    用于金额不一致这类「证据是一句计算结果」而非原文摘录的情况。
    """
    return RiskItem(
        risk_type=risk_type,
        label=RISK_LABELS.get(risk_type, risk_type),  # 中文展示名；未登记类型回退机器码
        severity=severity,
        field=field,
        evidence=_quote(model, field) if evidence is None else evidence,
        clause_ref=_clause_ref(model, field),
        policy_ref=policy_ref,
        suggestion=suggestion,
    )


def _check_required(model: ContractModel) -> list[RiskItem]:
    """必填字段完整性检查。

    作用：核心字段（甲乙方、生效/到期日、总额、币种）缺失时给出风险——
    这些字段缺失会直接削弱后续金额、日期等规则的判定可信度。
    返回：每个缺失字段一条 RiskItem。
    """
    out: list[RiskItem] = []
    for field in CORE_REQUIRED:
        # 分支：该字段为空 → 缺失。金额与日期缺失会阻断审查，定 high；
        # 主体信息（甲乙方/币种）缺失影响较小，定 medium 提示人工补全。
        if getattr(model, field) is None:
            severity = Severity.high if field in HIGH_IF_MISSING else Severity.medium
            out.append(
                _mk(
                    model,
                    risk_type="missing_required_field",
                    severity=severity,
                    field=field,
                    # 字段名用中文（FIELD_LABELS），避免界面出现 effective_date 这类英文 key
                    suggestion=f"缺失必填字段「{FIELD_LABELS.get(field, field)}」，请人工确认或补全后再审。",
                )
            )
    return out


def _check_dates(model: ContractModel) -> list[RiskItem]:
    """日期逻辑检查（纯逻辑规则，不依赖政策库）。

    处理两种矛盾情况，各产出一条 medium 风险：
    1) 生效日早于签署日（日期填写矛盾）；
    2) 到期日不晚于生效日（合同没有有效存续期）。
    返回：风险列表（日期缺失时无法判定，直接为空）。
    """
    out: list[RiskItem] = []
    # 分支 1：签署日在生效日之后 → 生效日期填错，需人工核实
    if model.signature_date and model.effective_date and model.effective_date < model.signature_date:
        out.append(
            _mk(
                model,
                risk_type="date_logic_effective_before_signature",
                severity=Severity.medium,
                field="effective_date",
                suggestion="生效日期早于签署日期，请核实日期填写是否有误。",
            )
        )
    # 分支 2：到期日 ≤ 生效日 → 合同期限非法（到期日必须晚于生效日）
    if model.effective_date and model.expiry_date and model.expiry_date <= model.effective_date:
        out.append(
            _mk(
                model,
                risk_type="date_logic_expiry_not_after_effective",
                severity=Severity.medium,
                field="expiry_date",
                suggestion="到期日应晚于生效日，请核实合同期限。",
            )
        )
    return out


def _check_amount(model: ContractModel) -> list[RiskItem]:
    """金额一致性校验：付款期次加总应 ≈ 合同总额。

    作用：抓「分项加总对不上总额」的手误或故意不一致。
    判定口径：偏差 ≤ 1%（AMOUNT_TOLERANCE_RATIO）视为一致；超过报 high。
    """
    total = model.total_amount
    terms = [t for t in model.payment_schedule if t.amount is not None]
    # 分支 1：总额缺失/非正，或没有任何带金额的期次 → 无从校验，跳过
    if total is None or total <= 0 or not terms:
        return []
    summed = sum((t.amount for t in terms), Decimal("0"))
    deviation = abs(summed - total) / total
    # 分支 2：偏差在容忍范围内 → 视为一致，不产生风险
    if deviation <= AMOUNT_TOLERANCE_RATIO:
        return []
    # 分支 3：偏差超容忍 → high；证据写计算式与具体数字，方便人工核对
    return [
        _mk(
            model,
            risk_type="amount_inconsistency",
            severity=Severity.high,
            field="total_amount",
            policy_ref=None,
            evidence=f"付款期次加总 {summed} 元 ≠ 合同总额 {total} 元（偏差 {deviation:.1%}）。",
            suggestion="核对付款计划与合同总额是否一致，修改错误金额。",
        )
    ]


def _prepay_ratio(model: ContractModel) -> tuple[PaymentTerm, float] | None:
    """定位「预付款」期次并返回其占合同总额的比例（百分比）。

    优先用期次自带的 percent；只有金额时用 金额/总额 反推。
    返回 None 表示全文没有预付约定（规则按合规处理，不判风险）。
    """
    total = model.total_amount
    for term in model.payment_schedule:
        # 分支 1：只认名称含「预付」的期次，避免误把验收款当预付款
        if "预付" not in term.name:
            continue
        # 分支 2：期次带显式比例 → 直接用（抽取/人工填写阶段应尽量带比例）
        if term.percent is not None:
            return term, term.percent
        # 分支 3：只有金额且总额可得 → 用 金额/总额*100 反推比例
        if term.amount is not None and total:
            return term, float(term.amount / total * 100)
    # 分支 4：扫完所有期次都没找到预付 → 无预付约定
    return None


def _check_policies(model: ContractModel, required: set[str]) -> list[RiskItem]:
    """政策类规则汇总（输出带 policy_ref，可回指政策库文档）。

    依次检查：预付款比例(P-01)、质保期(P-02)、责任上限(P-03)、
    保密期(P-04)、违约金日利率(P-03——细则第三条明文后从"惯例提示"升级为
    可回指制度，2026-09-05 语料 v2 起)。
    required=该品类"应含条款"字段集合（缺失才报 medium，见 KIND_BASELINE）。
    返回：风险列表（可能为空）。
    """
    out: list[RiskItem] = []

    # ---- P-01 预付款比例：超过 30% → high ----
    prepay = _prepay_ratio(model)
    # 分支：存在预付期次且比例超阈值 → 预付款过高，资金风险
    if prepay and prepay[1] > PREPAY_MAX_PERCENT:
        term, ratio = prepay
        out.append(
            _mk(
                model,
                risk_type="prepayment_ratio_high",
                severity=Severity.high,
                field="payment_schedule",
                policy_ref="P-01",
                evidence=term.evidence or f"预付款比例 {ratio:g}%",
                suggestion=f"预付款 {ratio:g}% 超过政策上限 {PREPAY_MAX_PERCENT:g}%，建议降至 30% 以内。",
            )
        )

    # ---- P-02 质保期：不足 12 个月 → high ----
    # 分支：质保月数有值且低于下限 → 交付后保障不足
    if model.warranty_months is not None and model.warranty_months < WARRANTY_MIN_MONTHS:
        out.append(
            _mk(
                model,
                risk_type="warranty_too_short",
                severity=Severity.high,
                field="warranty_months",
                policy_ref="P-02",
                suggestion=f"质保 {model.warranty_months} 个月低于政策下限 {WARRANTY_MIN_MONTHS} 个月。",
            )
        )

    # ---- P-03 责任上限：未明确 → medium；明确但低于 50% → high ----
    cap = model.liability_cap
    # 分支 1：完全没约定上限 → medium（建议按 P-03 明确，避免履约争议）
    if cap is None:
        # 这种情况是：品类要求责任上限但正文没写 → medium 提示补条款
        if "liability_cap" in required:
            out.append(
                _mk(
                    model,
                    risk_type="liability_cap_unclear",
                    severity=Severity.medium,
                    field="liability_cap",
                    policy_ref="P-03",
                    suggestion="未明确责任上限，建议按 P-03 约定合理上限（不低于总额 50%）。",
                )
            )
    # 分支 2：有约定但低于政策底线 → high（供应商赔偿被压得过低）
    elif cap < LIABILITY_CAP_MIN_PERCENT:
        out.append(
            _mk(
                model,
                risk_type="liability_cap_too_low",
                severity=Severity.high,
                field="liability_cap",
                policy_ref="P-03",
                suggestion=f"责任上限 {cap:g}% 低于政策底线 {LIABILITY_CAP_MIN_PERCENT:g}%，建议提高。",
            )
        )

    # ---- P-04 保密期：缺失 → medium；超过 36 个月 → high ----
    conf = model.confidentiality_months
    # 分支 1：未约定保密期 → medium（提示补条款，24~36 个月为宜）
    if conf is None:
        # 这种情况是：品类要求保密条款但正文没写 → medium 提示补条款
        if "confidentiality_months" in required:
            out.append(
                _mk(
                    model,
                    risk_type="confidentiality_missing",
                    severity=Severity.medium,
                    field="confidentiality_months",
                    policy_ref="P-04",
                    suggestion="缺少保密条款，建议补充（保密期宜 24 个月以上、不超过 36 个月）。",
                )
            )
    # 分支 2：保密期超上限 → high（约束过重，超出政策允许范围）
    elif conf > CONFIDENTIALITY_MAX_MONTHS:
        out.append(
            _mk(
                model,
                risk_type="confidentiality_too_long",
                severity=Severity.high,
                field="confidentiality_months",
                policy_ref="P-04",
                suggestion=f"保密期 {conf} 个月超过政策上限 {CONFIDENTIALITY_MAX_MONTHS} 个月。",
            )
        )

    # ---- 违约金日利率（P-03）：>1%/日 → high ----
    # 分支：违约金率有值且超过阈值 → 罚则畸高。P-03 细则第三条已写明
    # "日费率超过每日 1% 属畸高"，故 policy_ref 挂 P-03 不算凭空引用。
    if model.penalty_rate is not None and model.penalty_rate > PENALTY_DAILY_MAX_PERCENT:
        out.append(
            _mk(
                model,
                risk_type="penalty_rate_too_high",
                severity=Severity.high,
                field="penalty_rate",
                policy_ref="P-03",
                suggestion=f"逾期违约金日 {model.penalty_rate:g}% 超过 P-03 允许的 1% 上限（实务 0.05%~0.1%），建议协商下调。",
            )
        )
    return out


def _check_ip_and_law(model: ContractModel, required: set[str]) -> list[RiskItem]:
    """知识产权归属与适用法律检查 (P-05)。

    处理三种情况（均为 medium, 需人工确认/补条款）：
    1) 完全没提 IP 归属;2) 写了归属但未归甲方/采购方；
    3) 缺适用法律约定。
    只有 required 含对应字段的品类才检查（品类本身不要求时可省略）。
    返回：风险列表（可能为空）。
    """
    out: list[RiskItem] = []
    ip = model.ip_ownership
    # 分支 1：完全未提 IP 归属 → medium，建议补充归属采购方
    if ip is None:
        # 这种情况是：品类要求 IP 归属条款但正文没写 → medium
        if "ip_ownership" in required:
            out.append(
                _mk(
                    model,
                    risk_type="ip_ownership_missing",
                    severity=Severity.medium,
                    field="ip_ownership",
                    policy_ref="P-05",
                    suggestion="未约定知识产权归属，建议明确定制成果归采购方。",
                )
            )
    # 分支 2：写了归属但没出现「甲方/采购方」关键词 → 权属可能不在我方，需人工核实
    elif ip is not None and "ip_ownership" in required and not any(kw in ip for kw in _BUYER_KEYWORDS):
        out.append(
            _mk(
                model,
                risk_type="ip_ownership_unclear",
                severity=Severity.medium,
                field="ip_ownership",
                policy_ref="P-05",
                suggestion="知识产权归属未归采购方，请核实权属表述。",
            )
        )
    # 分支 3：缺适用法律 → medium（争议解决无依据）
    if model.governing_law is None:
        # 这种情况是：品类要求适用法律条款但正文没写 → medium
        if "governing_law" in required:
            out.append(
                _mk(
                    model,
                    risk_type="governing_law_missing",
                    severity=Severity.medium,
                    field="governing_law",
                    policy_ref="P-05",
                    suggestion="缺少适用法律/争议解决约定，请补充。",
                )
            )
    return out


# ---- 空白模板占位检测（2026-09-05，用户上传真实示范文本模板后加的体验层）----
# 背景：空白模板（甲方/日期/金额都是占位）会如实触发三条 high"缺必填"停闸口，
# 演示观感像"系统把好合同审坏了"。这里检测文本里的占位痕迹，命中则把缺必填
# 降为 medium + 追加"疑似空白模板"提示——不误放行（仍是 conditional_pass），
# 也不会让模板整批卡在闸口。只作用于"缺必填"类风险，真实缺陷不受影响。

# 各类占位标记的正则（每类只要命中一次即计数）：
# - date：年/月/日之间只有空格/下划线/全角空格（真实日期中间是数字或中文数字）
# - amount：金额词与「元」之间只有冒号/空格等（填写后会有 数字/大写中文 等实义字符）
# - amount_cap：金额大写栏空白（大写：＿＿＿）
# - party：冒号后跟下划线（甲方（采购方）：＿＿＿）
# - fill：成串下划线/全角下划线（模板填空位）
_BLANK_PATTERN_RE: dict[str, re.Pattern] = {
    "date": re.compile(r"年[ ＿_\u3000]*月[ ＿_\u3000]*日"),
    "amount": re.compile(r"(?:货款|合同)?(?:金额|价款|总价)[为是：:（( ]{0,5}元"),
    "amount_cap": re.compile(r"大写[：:]\s*[＿_ \u3000]*[）)]"),
    "party": re.compile(r"[：:]\s*[＿_]{2,}"),
    "fill": re.compile(r"[＿_]{3,}"),
}

# 判定为"疑似空白模板"所需的最少占位类别数（≥2 防单处误报，如正文里偶尔
# 出现一处"年 月 日"或个别下划线不会触发降级）
_BLANK_SUSPECT_MIN_CATEGORIES = 2


def _blank_markers(text: str) -> tuple[set[str], str]:
    """扫原文找占位痕迹，返回 (命中的类别集合, 首段占位原文摘录)。"""
    found: set[str] = set()
    snippet = ""
    for category, pattern in _BLANK_PATTERN_RE.items():
        match = pattern.search(text)
        if match:
            found.add(category)
            if not snippet:
                snippet = match.group(0).strip()[:80]
    return found, snippet


def is_blank_template_suspect(text: str) -> bool:
    """原文是否像空白/未定稿模板：命中的占位类别 ≥ 2 视为疑似。"""
    found, _ = _blank_markers(text or "")
    return len(found) >= _BLANK_SUSPECT_MIN_CATEGORIES


def annotate_template_risks(risks: list[RiskItem], text: str) -> list[RiskItem]:
    """模板场景下的风险标注：缺必填 high → medium，并附一条"疑似空白模板"。

    口径：仅当 ① 原文疑似空白模板 且 ② 风险里确有"缺必填"时生效；
    其他缺陷（质保/违约金等）severity 原样保留，仍可能触发闸口。
    返回新列表，不修改入参。
    """
    if not is_blank_template_suspect(text):
        return risks
    # 这种情况是：没有"缺必填"风险 → 模板检测不影响本份结果
    if not any(r.risk_type == "missing_required_field" for r in risks):
        return risks
    _, snippet = _blank_markers(text)
    out = [
        # 缺必填因模板占位而降级（medium）；其余风险原样
        r.model_copy(update={"severity": Severity.medium})
        if r.risk_type == "missing_required_field" and r.severity == Severity.high
        else r
        for r in risks
    ]
    out.append(
        RiskItem(
            risk_type="blank_template_suspected",
            label=RISK_LABELS["blank_template_suspected"],
            severity=Severity.medium,
            field="",
            evidence=snippet,
            suggestion=(
                "原文含多处空白占位（甲方/签署日期/金额未填写），疑似空白模板或未定稿版本。"
                "请确认是否上传了填写完整的最终签署版；若确为模板本身，无需逐条补全。"
            ),
            policy_ref=None,
        )
    )
    return out


def evaluate(model: ContractModel) -> list[RiskItem]:
    """规则引擎入口：跑全部确定性规则，返回风险清单。

    执行顺序固定：必填 → 日期 → 金额 → 政策 → IP/法律
    （先基础后政策），保证输出稳定，便于测试与前端展示。
    品类基线：contract_kind 决定"应含条款"，缺失类检查只在基线内生效。
    """
    kind = model.contract_kind or "enterprise_goods"
    required = KIND_BASELINE.get(kind, KIND_BASELINE["enterprise_goods"])
    return (
        _check_required(model)
        + _check_dates(model)
        + _check_amount(model)
        + _check_policies(model, required)
        + _check_ip_and_law(model, required)
    )


def grade_report(risks: list[RiskItem]) -> Grade:
    """按风险清单评级。

    映射：任一 high → fail（Phase 2 将据此触发 gate 人工审批）；
    只有 medium/low → conditional_pass；空清单 → pass。
    """
    if any(r.severity == Severity.high for r in risks):
        return Grade.fail
    return Grade.conditional_pass if risks else Grade.pass_
