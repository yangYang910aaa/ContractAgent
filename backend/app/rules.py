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
# 责任上限底线(占总额 %)，按品类区分：货物/服务采购 50%；技术开发(委托/合作/服务)类行业惯例普遍把赔偿上限压到 30%，底线放宽到 30%，
LIABILITY_CAP_MIN_PERCENT: dict[str, float] = {
    "enterprise_goods": 50.0,
    "tech_service": 30.0,
}
CONFIDENTIALITY_MAX_MONTHS = 36  # P-04：保密期不超过 36 个月
PENALTY_DAILY_MAX_PERCENT = 1.0  # 违约金日利率上限
AMOUNT_TOLERANCE_RATIO = Decimal("0.01")  # 分项加总 vs 总额允许偏差 1%
# 预付款期次的名称关键词：P-01 第二条明文把"以'首付款'名义在交付或验收前支付的部分"
# 计入预付款，备料款/启动款同理（真实合同走查 2026-09-10：70% "首付款"曾被漏判）
_PREPAY_NAME_KEYWORDS = ("预付", "首付", "备料款", "启动款")

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
    # 批3 修正（D36）：判据是"保密期字段没抽到"，正文可能已写保密义务——
    # 展示名放宽为"条款或期限"，具体是哪种由 _refine_confidentiality_wording 按正文改写建议
    "confidentiality_missing": "缺少保密条款或未约定期限",
    "confidentiality_too_long": "保密期过长",
    "penalty_rate_too_high": "违约金比例畸高",
    "ip_ownership_missing": "未约定知识产权归属",
    "ip_ownership_unclear": "知识产权归属不清",
    "governing_law_missing": "缺少适用法律约定",
    "blank_template_suspected": "疑似空白模板",
    "acceptance_unclear": "验收标准或期限不明确",
    "invoice_unclear": "发票开具约定缺失",
    "performance_bond_missing": "履约担保缺失",
    "subcontract_unrestricted": "转包/分包未作限制",
    "personal_info_clause_missing": "未约定个人信息保护义务",
    "data_processing_terms_missing": "委托处理要件不完整",
    "data_cross_border_unclear": "数据出境缺少合规路径",
    "data_deletion_missing": "未约定数据删除与泄露通知",
    # 批3（P-13/P-14）：保密例外与违约金基数/上限
    "confidentiality_no_exception": "保密条款缺少例外",
    "penalty_basis_unclear": "违约金基数不明",
    "penalty_cap_missing": "违约金无上限",
}

# ContractModel 字段 key → 中文名：用于建议文案/UI 展示,与前端 labels 对齐；
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
# 金额/日期缺失视为；主体信息缺失降为 medium
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
    """取某字段的原文证据(来自 extraction_meta)。

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


def _liability_cap_floor(kind: str | None) -> float:
    """按品类取 P-03 责任上限底线(占总额 %)。"""
    # 未登记品类（gov/agri 显式写了上限时也照查）兜底 50，保持历史口径
    return LIABILITY_CAP_MIN_PERCENT.get(kind or "enterprise_goods", 50.0)


def _penalty_quote_is_daily(quote: str) -> bool:
    """违约金 quote 是否"按日计收"。

    """
    # 这种情况是：没有证据原文（离线测试/旧路径）→ 不拦，交给抽取口径约束
    if not quote:
        return True
    occurrence = bool(re.search(r"每次|按次|每笔", quote))
    daily = bool(re.search(r"日|按天", quote))
    return not (occurrence and not daily)


def _total_amount_reliable(model: ContractModel) -> bool:
    """总额抽取是否可信（金额一致性 high 的前置门槛）。
    """
    meta = model.extraction_meta.get("total_amount")
    # 这种情况是：没有字段证据 → 默认可信（保持既有判定与测试）
    if meta is None:
        return True
    return meta.confidence >= 0.7 and bool(re.search(r"\d", meta.quote or ""))


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


def _term_amount_implausible(term: PaymentTerm, total: Decimal) -> bool:
    """期次金额是否疑似"把百分比抽进了金额字段"（真实合同走查暴露，2026-09-10）。

    背景：合同只写比例（如"预付款（70%）"）时，抽取会把 70 填进 amount；
    金额一致性照算就得到"期次加总 70 元 ≠ 总额 480 万"的假 high 误停闸。
    判定（宁缺毋滥，只拦明显不合常理的形态）：
    - amount 与 percent 数值相同 → 直接判为百分比串位；
    - 总额 ≥1 万时 amount ≤100 → 任何真实付款期次都不可能是这个量级。
    """
    amount = term.amount
    # 分支：没抽到金额 → 不算"疑似串位"（由其它分支处理）
    if amount is None:
        return False
    if term.percent is not None and Decimal(str(term.percent)) == amount:
        return True
    return total >= Decimal("10000") and amount <= 100


def _check_amount(model: ContractModel) -> list[RiskItem]:
    """金额一致性校验：付款期次加总应 ≈ 合同总额。

    作用：抓「分项加总对不上总额」的手误或故意不一致。
    判定口径：偏差 ≤ 1%(AMOUNT_TOLERANCE_RATIO)视为一致；超过报 high。
    """
    total = model.total_amount
    terms = [t for t in model.payment_schedule if t.amount is not None]
    # 分支 1：总额缺失/非正，或没有任何带金额的期次 → 无从校验，跳过
    if total is None or total <= 0 or not terms:
        return []
    # 分支 1-1：存在"疑似百分比串位"的期次金额 → 加总口径不可信，不参与一致性判定；
    #   降级为 medium 提示人工核对（不静默、不误停闸口，呼应 D23 低置信度处理口径）
    doubtful = [t for t in terms if _term_amount_implausible(t, total)]
    if doubtful:
        shown = "、".join(f"{t.name or '期次'} {t.amount}" for t in doubtful[:3])
        return [
            _mk(
                model,
                risk_type="amount_inconsistency",
                severity=Severity.medium,
                field="total_amount",
                policy_ref=None,
                evidence=f"付款期次金额疑似抽取为百分比（{shown}），无法校验金额一致性。",
                suggestion="期次金额疑似被抽成比例数字，金额一致性无法自动判定，请人工核对付款计划后再审。",
            )
        ]
    summed = sum((t.amount for t in terms), Decimal("0"))
    deviation = abs(summed - total) / total
    # 分支 2：偏差在容忍范围内 → 视为一致，不产生风险
    if deviation <= AMOUNT_TOLERANCE_RATIO:
        return []
    # 分支 3-1：偏差超容忍但总额不可信（低置信度/证据无数字）→ medium 待人工核对。
    #   背景：半填合同金额空白被 LLM 脑补出金额，不一致属幻觉不是真缺陷；
    #   降级不静默（medium 仍在报告里提示人工核对），也不误停闸口。
    if not _total_amount_reliable(model):
        return [
            _mk(
                model,
                risk_type="amount_inconsistency",
                severity=Severity.medium,
                field="total_amount",
                policy_ref=None,
                evidence=f"付款期次加总 {summed} 元 ≠ 合同总额 {total} 元（偏差 {deviation:.1%}）。",
                suggestion="总额抽取置信度低或原文金额栏为空，金额一致性无法自动判定，请人工核对后再审。",
            )
        ]
    # 分支 3-2：偏差超容忍且总额可信 → high；证据写计算式与具体数字，方便人工核对
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

    优先用期次自带的 percent; 只有金额时用 金额/总额 反推。
    返回 None 表示全文没有预付约定（规则按合规处理，不判风险）。
    """
    total = model.total_amount
    for term in model.payment_schedule:
        # 分支 1：只认名称含预付类关键词的期次（预付/首付/备料款/启动款，见 P-01 第二条），
        #   避免误把验收款/进度款当预付款
        if not any(kw in term.name for kw in _PREPAY_NAME_KEYWORDS):
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

    # ---- P-03 责任上限：未明确 → medium；明确但低于品类底线 → high ----
    cap = model.liability_cap
    floor = _liability_cap_floor(model.contract_kind)
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
                    suggestion=(
                        f"未明确责任上限，建议按 P-03 约定合理上限"
                        f"（该品类底线不低于总额 {floor:g}%）。"
                    ),
                )
            )
    # 分支 2：有约定但低于该品类底线 → high（供应商赔偿被压得过低）
    elif cap < floor:
        out.append(
            _mk(
                model,
                risk_type="liability_cap_too_low",
                severity=Severity.high,
                field="liability_cap",
                policy_ref="P-03",
                suggestion=f"责任上限 {cap:g}% 低于政策底线 {floor:g}%，建议提高。",
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
    # 分支：违约金率有值、超过阈值且属"按日计收" → 罚则畸高。P-03 细则第三条已
    # 写明"日费率超过每日 1% 属畸高"，故 policy_ref 挂 P-03 不算凭空引用；
    # 非按日（"每次违约按总额 X%"）不适用日费率阈值（2026-09-07 真实合同校准）。
    if (
        model.penalty_rate is not None
        and model.penalty_rate > PENALTY_DAILY_MAX_PERCENT
        and _penalty_quote_is_daily(_quote(model, "penalty_rate"))
    ):
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
    """知识产权归属与适用法律检查 。

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


# ---- 空白模板占位检测----

# 各类占位标记的正则（每类只要命中一次即计数）：
# - date：年/月/日之间只有空格/下划线/全角空格（真实日期中间是数字或中文数字）
# - amount：金额词空值或"￥　　元"形态（填写后会有 数字/大写中文 等实义字符）
# - amount_cap：金额大写栏空白（大写：＿＿＿）
# - party：冒号后跟下划线（甲方（采购方）：＿＿＿）
# - fill：成串下划线/全角下划线（模板填空位）
# - blank：冒号后只剩空白直到行尾（真空白值；易错点：不得用"冒号+空格"判断，
#   PDF 抽取把"甲方：    乙方："这类排版间距也带出空格，会误伤已签合同）
# - dot：点线/省略号填充栏（GF 示范文本用 "……………" 引出待填内容）
# - box：□ 勾选/未选框（示范文本"选项处打 √/×"结构）
# 说明：原 void_punct/void_unit（空格+标点/单位）在 2026-09-10 真实合同走查中
# 被证实是 PDF 排版空格的产物（已签合同也命中），会误报"疑似空白模板"，故移除
_BLANK_PATTERN_RE: dict[str, re.Pattern] = {
    "date": re.compile(r"年[ ＿_\u3000]*月[ ＿_\u3000]*日"),
    "amount": re.compile(
        r"(?:金额|价款|总价|货款)[为是：:]\s*[＿_ \u3000]*元(?!\s*[）)])"
        r"|[￥¥][\s＿_\u3000]{2,}元"
    ),
    "amount_cap": re.compile(r"大写[：:]\s*[＿_ \u3000]*[）)]"),
    "party": re.compile(r"[：:]\s*[＿_]{2,}"),
    "fill": re.compile(r"[＿_]{3,}"),
    "blank": re.compile(r"[：:][ \u3000]{3,}(?=\n|$)"),
    "dot": re.compile(r"[.．…]{3,}"),
    "box": re.compile(r"□"),
    # 填空式条款的空标点/空单位（霸王花式模板："标准是 ；""定金 元"）：
    # 易错点——PDF 排版抽取也会在正常句子里带出"空格+标点/单位"，故仅在"未填写文本"
    # 场景参与判定（见 _looks_filled），已填写合同里这两类一律忽略
    # 批3 修正（D36）：① 只认空格/全角空格，不认换行——PDF 折行会造出"值\n，"这种
    # 假空标点（电煤合同即因此被判空白模板）；② 单位前后紧邻数字说明该值已填
    # （"见票后 30 天内支付"不是空白位），故加数字邻接限制
    "void_punct": re.compile(r"[\u4e00-\u9fff%][：:]?[ \u3000]{1,3}[。；,，．]"),
    "void_unit": re.compile(r"(?<![\d])[ \u3000](?:%|元|日内|天内|项|种方式|方)(?![\s\u3000]*[\d≤≥<])"),
}

# 已填写合同的形态特征：有带数字的年份/年月 + 数字化金额（真实已签合同/正常样本都满足；
# 真实 PDF 合同常只写"2025 年"（项目名/期限），故年份单独出现也算已填写）
_FILLED_DATE_RE = re.compile(r"\d{4}\s*年|年\s*\d{1,2}\s*月")
_FILLED_AMOUNT_RE = re.compile(r"\d[\d,]{2,}(?:\.\d+)?\s*(?:元|万元)")
# 仅在"未填写文本"里算证据的类别（PDF 排版空格产物，见上）
_ARTIFACT_CATEGORIES = {"void_punct", "void_unit"}

# 签名/签署栏上下文：这些栏位的日期空白只说明"未写签署日期"，不能据此把整份
# 合同判为空白模板（真实合同走查 2026-09-10：已签合同的署名页日期栏曾误报）
_SIGNATURE_CONTEXT_RE = re.compile(r"签订(?:时间|地点|日期)|盖章|（章）|\(章\)|签约|双方签字")

# 判定为"疑似空白模板"：① 占位类别 ≥2（防单处偶发）且 ② 至少一类属"强证据"。
# 强证据 = 真空白值域（amount / amount_cap / party / fill / blank）；
# 易错点：date 空白在已签合同的署名页/页脚也常见（"年 月 日"处），只能计数、不能定罪；
# box、dot（选项框/点线）在真实合同里同样常见，也只能作旁证。
_BLANK_SUSPECT_MIN_CATEGORIES = 2
_BLANK_STRONG_CATEGORIES = {"amount", "amount_cap", "party", "fill", "blank"}
# 出 evidence 摘录时优先"看得出是哪个栏位"的类别；纯空白/点线/选框摘出来
# 不像话，只参与计数、不抢摘录位
_SNIPPET_CATEGORIES = ("date", "amount", "amount_cap", "party", "fill")


def _blank_markers(text: str) -> tuple[set[str], str]:
    """扫原文找占位痕迹，返回 (命中的类别集合, 首段占位原文摘录)。"""
    filled = len(_FILLED_DATE_RE.findall(text)) > 0 and bool(_FILLED_AMOUNT_RE.search(text))
    found: set[str] = set()
    snippet = ""
    for category, pattern in _BLANK_PATTERN_RE.items():
        # 这种情况是：文本看起来已填写完整 → 忽略排版空格类假信号
        if filled and category in _ARTIFACT_CATEGORIES:
            continue
        for match in pattern.finditer(text):
            # 这种情况是：日期空白出现在署名/签订栏 → 只是未写签署日期，不算整份空白
            if category == "date" and _SIGNATURE_CONTEXT_RE.search(
                text[max(match.start() - 24, 0) : match.end() + 8]
            ):
                continue
            found.add(category)
            if not snippet and category in _SNIPPET_CATEGORIES:
                snippet = match.group(0).strip()[:80]
            break
    return found, snippet


def is_blank_template_suspect(text: str) -> bool:
    """原文是否像空白/未定稿模板。

    分流口径（真实合同走查 2026-09-10）：
    - 已填写文本（有数字化年月+金额）：占位类别 ≥2 且含"真空白值域"强证据才算，
      防"署名栏日期空白 + 排版空格"把已签合同误判成模板；
    - 未填写文本（模板/半填）：沿用占位类别 ≥2 的原口径，保证 GF 填空式、
      下划线式、点线式各类官方模板仍能识别。
    """
    found, _ = _blank_markers(text or "")
    if len(found) < _BLANK_SUSPECT_MIN_CATEGORIES:
        return False
    filled = len(_FILLED_DATE_RE.findall(text or "")) > 0 and bool(_FILLED_AMOUNT_RE.search(text or ""))
    # 分支：已填写文本 → 必须命中"真空白值域"强证据；未填写文本 → 原口径放行
    return bool(found & _BLANK_STRONG_CATEGORIES) if filled else True


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


# ---- 开放式条款语境标注（真实合同走查 2026-09-10）----

# "按实/按月结算"类语境：合同不写固定总额是常态（月结、账期、按订单、框架协议）
_OPEN_AMOUNT_RE = re.compile(
    r"按实结算|据实结算|实报实销|按订单|按月结算|按月结|月结|每月结算|月度结算|结算周期|账期"
    r"|按实际发生|按批次结算|框架(?:协议|合同)|按需下单|对账后付款"
    r"|每月|每个月|按季|按季度|对账|对帐|结算单|结算上月|按供货批次"
    r"|订单要求|以订单为准|订单结算|订单方式|按批下单"
    # 批3 补农副定价口径（D36 泛化集：江苏小麦"随行就市 + 过磅计量 + 批次收购"
    # 本就没有固定总额，缺总额被判 high 误停闸）
    r"|随行就市|随市定价|保底价|浮动价|按质论价|按质计价|计量过磅|过磅|按等级|等级差价"
)
# "签字/盖章之日起生效"句式：生效规则明确，但正文未写具体签署日期
_SIGNING_EFFECT_RE = re.compile(r"(?:签字|盖章|签名)[^。\n]{0,12}生效")
# 生效日以"签订之日"为准的写法（真实合同走查 2026-09-10：服务/开发类合同常只写
# "自合同签订之日起"，不写具体日期，抽取拿不到生效日）
_EFFECTIVE_FROM_SIGN_RE = re.compile(
    r"签订之日起|合同签订之日|自.{0,12}(?:签署|签订|签字|盖章).{0,10}(?:之日|当日起)"
    r"|(?:签署|签字|盖章).{0,10}生效|(?:签署|签字|盖章).{0,10}成立"
)
# 具体的结束日期写法（"至 2025 年 12 月 31 日"）：有却抽不到才提示人工核对，
# 没有具体结束日期（以验收/履行完毕为界）→ 属开放式期限，降提示级
_CONCRETE_END_RE = re.compile(r"(?:至|到|截止)\s*\d{4}\s*年")
# 日期栏空白：出现"年 月 日"三连但中间没有数字（签署栏/期限栏未填），
# 真实合同走查 2026-09-10：已签合同正文只留空白签署日期栏，抽取拿不到日期就判 high
# 批3 补：打码占位日期（"202*年*月*日""202X年"）同属"日期未定"，真实电煤竞价件即此形态
_DATE_BLANK_RE = re.compile(
    r"(?<!\d)[\s*＊xX×·【】\[\]〔〕]{0,6}年"
    r"[\s*＊xX×·＿_\u3000【】\[\]〔〕]{0,6}月[\s*＊xX×·＿_\u3000【】\[\]〔〕]{0,6}日"
)


def annotate_open_ended_risks(risks: list[RiskItem], text: str) -> list[RiskItem]:
    """把"开放式条款"语境下的缺必填 high 降为 medium（保留风险并写明提示）。

    背景：真实合同常见"按实结算/月结/账期"（无固定总额）与"有效期 N 年/长期"
    （无具体到期日）、以及"签字盖章之日起生效"但未写签署日期——抽取拿不到对应
    字段就判 high 会误停闸口。这里按文本语境降级为提示级（不阻断审批），
    并在 suggestion 里显式写明"已降为提示级"，避免静默降级造成误导（呼应 D23 不静默）。
    返回新列表，不修改入参。
    """
    if not text:
        return risks
    # 批3 修正（D36）：保密期没抽到 ≠ 缺保密条款——先按正文语境把文案改准。
    # 放在开放式降级之前、且不受下方早退分支影响（三类开放式语境都没有时也要修）
    risks = _refine_confidentiality_wording(risks, text)
    amount_open = _OPEN_AMOUNT_RE.search(text) is not None
    signing_effect = _SIGNING_EFFECT_RE.search(text) is not None
    date_blank = _DATE_BLANK_RE.search(text) is not None
    # 生效日口径：正文写了"签订之日起生效/自签署生效"或日期栏空白 → 无具体签署日期
    effective_open = signing_effect or date_blank or _EFFECTIVE_FROM_SIGN_RE.search(text) is not None
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
    """把保密期未抽取的提示文案改准：正文有保密义务时不说"缺少保密条款"。

    背景（D36 泛化集）：字段规则 `confidentiality_missing` 的判据只是
    `confidentiality_months is None`，文案却写"缺少保密条款"——真实 10 份合同里
    8 份命中、其中 6 份正文明明写了保密义务（有的还成章节），客户视角就是误报。
    判定口径：正文有保密义务信号 → 文案改"未约定保密期限"；完全没有 → 保留原文案。
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


def infer_effective_from_signature(model: ContractModel, text: str) -> ContractModel:
    """生效日兜底推断: 条款写"签字盖章之日起生效"时, 生效日=正文签署日.

    背景: 校服/政采等官方示范文本把生效方式写成"自双方签字盖章之日起生效",
    不重复写具体日期; 抽取器照抄该句抽不出 date 类型值, 填写完整的正常合同
    会误报"缺生效日期" high 停闸口.

    判定口径(宁缺毋滥, 防误推):
    - 生效日已抽到 -> 不动
    - 签署日没抽到 -> 无从推断, 不动
    - 正文没有"签字/盖章...生效"句式 -> 不推断
    其余情况用签署日回填生效日.
    """
    if model.effective_date is not None or model.signature_date is None:
        return model
    # 这种情况是: 正文确实写了生效方式与签字/盖章绑定 -> 允许推断.
    # 覆盖三种真实措辞: 校服"双方签字盖章之日起生效", 农副"双方签名(盖章)
    # 之日起成立并生效", 科技部"经签约各方签字盖章后生效".
    if not re.search(
        r"(?:签字|签名)\s*[（(]?盖章?[）)]?\s*之?日?起?(?:成立并)?生效"
        r"|经?(?:签约各方|双方).{0,6}(?:签字|签名).{0,4}后生效",
        text or "",
    ):
        return model
    return model.model_copy(update={"effective_date": model.signature_date})


# ---- 文本级条款基线检查（P-06~P-09，横向政策扩类批1）----

# 触发品类：企业/农副/技术全查；gov（政采/校服）按示范文本执行豁免——示范文本自带
# 验收/履约保函章节，且"正常省略"本就受 KIND_BASELINE 保护（易错点：无品类感知会把
# 校服/政采的正常省略误报成风险）
TEXT_RULE_KINDS: set[str] = {"enterprise_goods", "tech_service", "agri_goods"}
# 转包/分包基线只约束"定制/工程交付"形态（企业采购、技术开发/服务）；
# 农副产品买卖无转包概念，不套用（范围卡判定草案第四行口径）
SUBCONTRACT_KINDS: set[str] = {"enterprise_goods", "tech_service"}

# P-08 履约担保金额门槛：合同总额 ≥ 100 万元才进入"大额需担保"检查（元）
PERFORMANCE_BOND_MIN_TOTAL = Decimal("1000000")

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

# P-08 履约担保信号词：出现任意一个即视为有担保安排（范围卡口径：保证/保函/保证金/质保金）
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

# 条款标题行（回指 evidence 所在条款用）：第X条 / 章节式"七、"两种头
_CLAUSE_HEADER_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百\d]+条|(?:[一二三四五六七八九十]+)、)[^\n]{0,24}$"
)


def _text_excerpt(text: str, pos: int, width: int = 120) -> str:
    """截取命中位置附近的原文作证据：取 pos 前后 width/2 字符，折叠空白，防摘录过长。"""
    start = max(pos - width // 2, 0)
    return re.sub(r"\s+", "", text[start : pos + width // 2]).strip()


def _clause_ref_at(text: str, pos: int) -> str:
    """找 pos 前的最后一个条款/章节标题作 clause_ref（无标题返回空串）。"""
    for line in reversed(text[:pos].splitlines()):
        if _CLAUSE_HEADER_RE.match(line):
            return line.strip()
    return ""


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
            evidence=_text_excerpt(text, 0),
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
        evidence=_text_excerpt(text, 0),
        policy_ref="P-09",
        suggestion="合同未限制乙方转包/分包，建议按 P-09 约定转包须经甲方书面同意。",
        field=None,
    )


# ---- 数据与个人信息合规文本规则（P-10~P-12，横向批2）----

# 触发前置门（批2 防误伤核心）：只有正文确实涉及个人信息/用户数据处理的合同才启用
# 本组规则；纯货物买卖、校服/农副等不涉及数据处理的合同整批跳过。
# 易错点：不要用裸"数据"做门——技术开发/平台建设类合同常出现"数据平台/数据资产"
# 但不处理个人信息，套用会误报（批2 范围卡：以个人信息类信号为准）。
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


# ---- 批3 文本规则（P-13 保密例外 / P-14 违约金基数与上限）----

# 保密义务信号：正文写了保密安排，才谈"例外缺不缺"
_CONF_OBLIGATION_RE = re.compile(r"保密(?:义务|责任|条款|信息|内容)|负有保密|商业秘密|技术秘密|保密资料")
# 绝对禁止式披露（素材清单描述的目标缺陷形态："只写不得向任何第三方披露"）。
# 易错点：不能把普通"负有保密义务"也当缺陷——存量 32 份语料大多只有义务句，
# 那样会大面积新增 medium；批3 开工前离线验证：绝对式在旧语料上 0 命中。
_CONF_ABSOLUTE_RE = re.compile(
    r"不得(?:向|对)?(?:任何)?(?:第三方|第三人|他人|其他单位|任何单位)(?:披露|泄露|提供|公开)"
    r"|一律不得披露|严禁(?:向|对外)?披露"
)
# 保密例外信号：法定/监管/司法披露、已公开、独立开发、经对方书面同意、履约所必需、除外条款
_CONF_EXCEPTION_RE = re.compile(
    r"法律规定|法律法规|依法(?:披露|提供)|监管(?:机关|部门|机构)|司法(?:机关)?要求|法院"
    r"|仲裁.{0,6}要求|已(?:进入)?公开|公开(?:信息|领域)|公共领域|独立(?:开发|研发)|书面同意"
    r"|为履行.{0,8}(?:所必需|必要)|除外"
)
# 例外判定的"紧邻短句"上限（字符）：例外也可能写成紧随其后的独立短句
# （"……不得披露。法律法规另有规定的除外。"）。易错点——按固定字符窗口（±120/±60）
# 判定会把隔壁条款的"未经甲方书面同意"（转包）误当保密例外（批3 实测两次踩坑），
# 故改为"同一句 + 仅当紧邻句以除外类引导词开头才并入"
_CONF_EXCEPTION_TAIL_CHARS = 60
_CONF_EXCEPTION_TAIL_RE = re.compile(r"\s*(?:除|但|法律|法规|监管|司法)")

# 比例型违约金数值：万分之X / 千分之X / N% / N‰
_PENALTY_PCT_RE = re.compile(
    r"万分之[\d一二三四五六七八九十]+|千分之[\d一二三四五六七八九十]+"
    r"|[0-9]+(?:\.[0-9]+)?\s*%|[0-9]+(?:\.[0-9]+)?‰"
)
# 违约金基数词：句内出现任何一个"金额/数量类名词"即视为基数已明确。
# 教训（批3 实跑）：起初按品类逐个枚举（合同总价/技术开发费/订单金额…），结果真实合同
# 的写法永远多一种——"延期货款""订单总金额""当批货物总额"接连漏判，反而制造误报；
# 改为宽口径识别名词类别（判"有没有说清按什么算"），判不准时宁可不报。
_PENALTY_BASIS_RE = re.compile(
    r"金额|价款|货款|总价|总额|费用|价格|单价|造价|结算价|数量|基数|标准|部分|订单|批次|合同价"
)
# 按日计罚信号（"每逾期一日/按日/每拖延一天"）
_PENALTY_DAILY_RE = re.compile(r"每(?:日|天)|按日|每逾期一[日天]|每延迟一[日天]|每拖延一[日天]|每推迟一[日天]")
# 违约金上限信号
_PENALTY_CAP_RE = re.compile(r"不超过|最高不超过|累计不超过|上限|封顶|以.{0,8}为限")
# 上限回溯窗口（字符）：只看命中处之后这么远，避免把别处的上限借过来当本条封顶
# （真实钢结构合同：0.5%/日 那句之后 271 字才是另一条款的"不超过 8%"）
_PENALTY_CAP_WINDOW = 150
# 无上限判定的日费率门槛（%/日）：0.05%/日是行业常见写法，无上限的实际敞口有限
# （100 天累计 5%），报出来只是噪音；0.5%/日 这类高费率无封顶才会失控（真实钢结构件）
_PENALTY_UNCAPPED_MIN_DAILY_PERCENT = 0.1
# 基数判定的回看窗口（字符）：PDF 抽取常在"按合同价款的"与"1‰"之间插换行，
# 只看命中所在"句"会把基数词切到上一行（真实钢结构合同即如此）→ 往前多看 80 字。
# 方向仍以"宁可不报"为准：窗口内出现金额类名词就认为基数已写明。
_PENALTY_BASIS_LOOKBACK = 25
# 中文数字 → 数值（万分之X/千分之X 的 X 可能是中文，模板与真实合同都常见）
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _daily_penalty_percent(sentence: str) -> float | None:
    """从违约金句里取日费率（%/日）；取不到返回 None（宁缺毋滥，不据此定罪）。

    覆盖真实写法：0.5% / 5‰ / 万分之三 / 千分之五；多个数值取最大（同句常同时写
    "万分之三"与"0.03%"两种等价表述）。
    """
    values: list[float] = []
    for num in re.finditer(r"(\d+(?:\.\d+)?)\s*%", sentence):
        values.append(float(num.group(1)))
    for num in re.finditer(r"(\d+(?:\.\d+)?)\s*‰", sentence):
        values.append(float(num.group(1)) / 10)  # 1‰ = 0.1%
    for cn in re.finditer(r"万分之([\d一二三四五六七八九十]+)", sentence):
        raw = cn.group(1)
        values.append((float(raw) if raw.isdigit() else _CN_DIGITS.get(raw, 0)) / 100)
    for cn in re.finditer(r"千分之([\d一二三四五六七八九十]+)", sentence):
        raw = cn.group(1)
        values.append((float(raw) if raw.isdigit() else _CN_DIGITS.get(raw, 0)) / 10)
    return max(values) if values else None


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    """取 pos 所在句子的起止下标（句读按 。；;\\n 切），供"句内口径"判定用。

    易错点：基数这类判定必须在句内看——跨句会把责任上限句里的"合同总价款"
    借来当违约金基数，把真实缺陷判成合规。
    """
    start = max((text.rfind(ch, 0, pos) for ch in "。；;\n"), default=-1) + 1
    ends = [text.find(ch, pos) for ch in "。；;\n"]
    ends = [e for e in ends if e != -1]
    return start, (min(ends) if ends else len(text))


def _check_confidentiality_no_exception(text: str) -> RiskItem | None:
    """P-13 保密例外检查：保密条款写成"绝对不得披露"且无任何例外 → medium。

    口径（买方视角）：我方常有法定/监管披露义务（审计、监管报送、诉讼举证），
    条款若一律禁止披露，履行法定义务反而违约 → 提示补例外。
    触发刻意收紧为"绝对禁止式"措辞，普通保密义务句不报（防存量语料大面积误报）。
    """
    for m in _CONF_ABSOLUTE_RE.finditer(text):
        # 例外信号要在同一句内出现才算"有例外"（远处争议解决条款的"法院"不算）
        start, end = _sentence_span(text, m.start())
        scope = text[start:end]
        # 例外也可能写成紧随其后的独立短句，但只认以除外类引导词开头的下一句
        tail = text[end + 1 : end + 1 + _CONF_EXCEPTION_TAIL_CHARS]
        if _CONF_EXCEPTION_TAIL_RE.match(tail):
            scope += tail
        if _CONF_EXCEPTION_RE.search(scope):
            continue
        return RiskItem(
            risk_type="confidentiality_no_exception",
            label=RISK_LABELS["confidentiality_no_exception"],
            severity=Severity.medium,
            clause_ref=_clause_ref_at(text, m.start()),
            evidence=_text_excerpt(text, m.start()),
            policy_ref="P-13",
            suggestion=(
                "保密条款只写“不得向第三方披露”、未留任何例外，建议按 P-13 补充：法律法规或"
                "监管/司法机关要求披露、已公开信息、独立开发、经对方书面同意等情形不属于违约。"
            ),
            field=None,
        )
    return None


def _check_penalty_basis_unclear(text: str) -> RiskItem | None:
    """P-14 违约金基数检查：句内写了比例违约金却没写基数 → medium。

    基数不明（按总额还是未履行部分、是否含税）会让违约金无法计算、争议时各执一词。
    """
    for m in _PENALTY_PCT_RE.finditer(text):
        start, end = _sentence_span(text, m.start())
        sentence = text[start:end]
        # 分支 1：本句没提违约金（如责任上限句的百分比）→ 不属本规则
        if "违约金" not in sentence:
            continue
        # 分支 2：本句（含往前 80 字，PDF 换行会把基数词切到上一行）已写基数 → 合规
        window = text[max(start - _PENALTY_BASIS_LOOKBACK, 0) : end]
        if _PENALTY_BASIS_RE.search(window):
            continue
        # 分支 3：写了比例却没写基数 → medium
        return RiskItem(
            risk_type="penalty_basis_unclear",
            label=RISK_LABELS["penalty_basis_unclear"],
            severity=Severity.medium,
            clause_ref=_clause_ref_at(text, m.start()),
            evidence=_text_excerpt(text, m.start()),
            policy_ref="P-14",
            suggestion=(
                "违约金只写了比例、未写明计算基数（合同总价/未履行部分/逾期部分，是否含税），"
                "建议按 P-14 明确基数与计算方式。"
            ),
            field=None,
        )
    return None


def _check_penalty_cap_missing(text: str) -> RiskItem | None:
    """P-14 违约金上限检查：按日计罚且近旁无上限 → high（长期拖延可超本金）。

    口径：按日比例若无封顶，工期越长违约金越高、可能超过合同总额本身，对买方同样是
    失控敞口；上限句通常紧跟违约金句，故只看命中后的固定窗口，不取全文
    （真实合同里别的条款写了上限，不能算本条的封顶）。
    """
    hit = None
    rate: float | None = None
    for m in _PENALTY_DAILY_RE.finditer(text):
        start, end = _sentence_span(text, m.start())
        sentence = text[start:end]
        # 分支：本句没提违约金（如"每日巡检"）→ 继续找下一处
        if "违约金" not in sentence:
            continue
        hit, rate = m, _daily_penalty_percent(sentence)
        break
    # 分支 1：没有按日违约金 → 不套本规则（按次/一次性违约金走 P-03 口径）
    if hit is None:
        return None
    # 分支 2：日费率取不到、或低于门槛（0.05%/日 等常见写法）→ 不报（防噪音：低费率
    #    无上限的敞口有限，真实语料里这类写法很普遍）
    if rate is None or rate < _PENALTY_UNCAPPED_MIN_DAILY_PERCENT:
        return None
    # 分支 3：近旁写了上限（不超过/最高不超过/为限…）→ 视为已封顶
    if _PENALTY_CAP_RE.search(text[hit.start() : hit.start() + _PENALTY_CAP_WINDOW]):
        return None
    # 分支 4：日费率高且无上限 → high
    return RiskItem(
        risk_type="penalty_cap_missing",
        label=RISK_LABELS["penalty_cap_missing"],
        severity=Severity.high,
        clause_ref=_clause_ref_at(text, hit.start()),
        evidence=_text_excerpt(text, hit.start()),
        policy_ref="P-14",
        suggestion=(
            f"逾期违约金按日 {rate:g}% 计收却没有累计上限（长期拖延将超过合同总额），"
            "建议按 P-14 增加“违约金总额不超过合同总价款 X%”的上限。"
        ),
        field=None,
    )


def text_rules(text: str, kind: str | None) -> list[RiskItem]:
    """文本级条款基线检查：对原文做 P-06~P-14 的存在性/语义检查，输出 RiskItem 列表。

    与 evaluate()（字段级规则）互补：本函数不新增抽取字段，只看"条款该不该写、
    写了什么"；clause_ref/evidence 摘原文，policy_ref 挂 P-06~P-14。
    调用时机：evaluate() 之后、annotate_template_risks() 同层（pipeline/graph 接线）。
    kind 为 None 时按 enterprise_goods 处理（与 KIND_BASELINE 的 None 兜底口径一致）。
    空白模板（占位 ≥2 类）直接返回空：模板到处缺内容，补条款提示是噪音，
    缺必填降级 + 疑似空白模板已覆盖（呼应 D19 不误伤）。
    数据合规（批2）另有"触发前置门"：正文不涉及个人信息/用户数据处理时整组跳过。
    批3（P-13/P-14）查两条：保密条款缺例外、违约金基数不明/按日无上限——
    均为"写得对不对"类缺陷，不影响"有没有写"的既有判定。
    """
    # 这种情况是：原文疑似空白/未定稿模板 → 不谈条款完备性
    if is_blank_template_suspect(text or ""):
        return []
    effective_kind = kind or "enterprise_goods"
    # 这种情况是：品类不在触发集（gov 豁免）→ 整组规则不跑
    if effective_kind not in TEXT_RULE_KINDS:
        return []
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
    # 批3（P-13/P-14）：保密例外、违约金基数、违约金上限——三条都是"写得对不对"，
    # 与"有没有写"的批1/批2 规则互不重复（按日无封顶是批3 唯一新增闸口点）
    for check in (
        _check_confidentiality_no_exception,
        _check_penalty_basis_unclear,
        _check_penalty_cap_missing,
    ):
        risk = check(text)
        if risk:
            out.append(risk)
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
