"""确定性规则引擎。

输入 ContractModel → 输出 RiskItem[] + 评级。纯函数、无 LLM 调用，可离线单测。
政策阈值集中定义在本文件顶部（来源 data/policies/P-01~05）。

风险等级约定：存在 high → gate 人工审批；只有 medium/low → 有条件通过。
evaluate（产风险）与 grade_report（评级）分离，方便 Phase 2 的 grade→gate 节点复用。
"""

from __future__ import annotations

from decimal import Decimal

from backend.app.schemas import ContractModel, Grade, PaymentTerm, RiskItem, Severity

# ---- 政策阈值（百分比数值；金额单位：元）----
PREPAY_MAX_PERCENT = 30.0  # P-01：预付款不超过总额 30%
WARRANTY_MIN_MONTHS = 12  # P-02：质保期不少于 12 个月
LIABILITY_CAP_MIN_PERCENT = 50.0  # P-03：责任上限不低于总额 50%
CONFIDENTIALITY_MAX_MONTHS = 36  # P-04：保密期不超过 36 个月
PENALTY_DAILY_MAX_PERCENT = 1.0  # 违约金日利率上限（行业惯例，非政策硬指标）
AMOUNT_TOLERANCE_RATIO = Decimal("0.01")  # 分项加总 vs 总额允许偏差 1%

# 必填核心字段：缺失会削弱整份审查的可信度
CORE_REQUIRED = ("buyer", "supplier", "effective_date", "expiry_date", "total_amount", "currency")
# 金额/日期缺失视为 high（审查无法继续）；主体信息缺失降为 medium
HIGH_IF_MISSING = ("total_amount", "effective_date", "expiry_date")

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
                    suggestion=f"缺失必填字段「{field}」，请人工确认或补全后再审。",
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


def _check_policies(model: ContractModel) -> list[RiskItem]:
    """政策类规则汇总（输出带 policy_ref，可回指政策库文档）。

    依次检查：预付款比例(P-01)、质保期(P-02)、责任上限(P-03)、
    保密期(P-04)、违约金日利率（行业惯例，无对应政策条目故 policy_ref 留空）。
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

    # ---- 违约金日利率（行业惯例）：>1%/日 → high ----
    # 分支：违约金率有值且超过阈值 → 罚则畸高；该阈值无政策条目，
    # policy_ref 留空，避免"凭空引用政策"，仅按惯例提示。
    if model.penalty_rate is not None and model.penalty_rate > PENALTY_DAILY_MAX_PERCENT:
        out.append(
            _mk(
                model,
                risk_type="penalty_rate_too_high",
                severity=Severity.high,
                field="penalty_rate",
                policy_ref=None,
                suggestion=f"逾期违约金日 {model.penalty_rate:g}% 明显偏高（行业惯例 0.05%~0.1%），建议协商下调。",
            )
        )
    return out


def _check_ip_and_law(model: ContractModel) -> list[RiskItem]:
    """知识产权归属与适用法律检查（P-05）。

    处理三种情况（均为 medium，需人工确认/补条款）：
    1) 完全没提 IP 归属；2) 写了归属但未归甲方/采购方；
    3) 缺适用法律约定。
    返回：风险列表（可能为空）。
    """
    out: list[RiskItem] = []
    ip = model.ip_ownership
    # 分支 1：完全未提 IP 归属 → medium，建议补充归属采购方
    if ip is None:
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
    elif not any(kw in ip for kw in _BUYER_KEYWORDS):
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


def evaluate(model: ContractModel) -> list[RiskItem]:
    """规则引擎入口：跑全部确定性规则，返回风险清单。

    执行顺序固定：必填 → 日期 → 金额 → 政策 → IP/法律
    （先基础后政策），保证输出稳定，便于测试与前端展示。
    """
    return (
        _check_required(model)
        + _check_dates(model)
        + _check_amount(model)
        + _check_policies(model)
        + _check_ip_and_law(model)
    )


def grade_report(risks: list[RiskItem]) -> Grade:
    """按风险清单评级。

    映射：任一 high → fail（Phase 2 将据此触发 gate 人工审批）；
    只有 medium/low → conditional_pass；空清单 → pass。
    """
    if any(r.severity == Severity.high for r in risks):
        return Grade.fail
    return Grade.conditional_pass if risks else Grade.pass_
