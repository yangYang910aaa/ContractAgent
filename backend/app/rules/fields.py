from __future__ import annotations

from decimal import Decimal
import re

from backend.app.schemas import ContractModel, PaymentTerm, RiskItem, Severity
from backend.app.rules.constants import AMOUNT_TOLERANCE_RATIO, CONFIDENTIALITY_MAX_MONTHS, CORE_REQUIRED, FIELD_LABELS, HIGH_IF_MISSING, KIND_BASELINE, LIABILITY_CAP_MIN_PERCENT, PENALTY_DAILY_MAX_PERCENT, PREPAY_MAX_PERCENT, RISK_LABELS, WARRANTY_MIN_MONTHS, _BUYER_KEYWORDS, _PREPAY_NAME_KEYWORDS
from backend.app.rules.locator import _clean_page_marks


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
    risk_type: str,  # 风险类型编码
    severity: Severity,  # 风险等级
    field: str,  # 关联 ContractModel 字段名
    suggestion: str,  # 整改建议文案
    policy_ref: str | None = None,  # 对应政策编号（P-01..）
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
    """日期逻辑检查：生效日早于签署日、或到期日不晚于生效日，各产出一条提示级风险。

    日期缺失时无从判定，返回空列表。
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
    """期次金额是否疑似"把百分比抽进了金额字段"。

    合同只写比例时会算出"期次加总 70 元 ≠ 总额 480 万"这种假不一致。
    只拦明显不合常理的形态：金额与比例数值相同，或总额上万而金额不到三位数。
    """
    amount = term.amount
    # 分支：没抽到金额 → 不算"疑似把比例抄进金额"（交给其它分支）
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
    # 分支 1-1：存在"疑似把比例抄进金额"的期次金额 → 加总不可信，不参与一致性判定；
    #   降级为 medium 提示人工核对（不静默、不误停闸口）
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
    # 非按日（"每次违约按总额 X%"）不适用日费率阈值。
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
    """知识产权归属与适用法律检查，都判提示级。

    三种情况：完全没提归属、写了但未归采购方、缺适用法律。只对品类基线要求这两项的
    合同检查，返回风险列表（可能为空）。
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


def infer_effective_from_signature(model: ContractModel, text: str) -> ContractModel:
    """生效日兜底推断：正文写"签字盖章之日起生效"且抽到了签署日 → 用签署日回填。

    示范文本常只写生效方式、不写具体日期，抽不到就误报"缺生效日期"高风险。
    宁缺毋滥：已抽到生效日、没抽到签署日、或正文没有该句式时一概不推断。
    """
    if model.effective_date is not None or model.signature_date is None:
        return model
    # 扫描件文本先去掉 OCR 页标记：页标记会把"签字…生效"从中间截开、白占十几个字，
    # 不清理就会漏判、误报"缺生效日期"
    text = _clean_page_marks(text or "")
    # 这种情况是: 正文确实写了生效方式与签字/盖章绑定 -> 允许推断.
    # 覆盖三种真实措辞: 校服"双方签字盖章之日起生效", 农副"双方签名(盖章)
    # 之日起成立并生效", 科技部"经签约各方签字盖章后生效".
    # 窗口 40 与 annotate 的 SIGNING_EFFECT_RE 同口径（此前这里是 20，两处漂移）
    if not re.search(
        r"(?:签字|签名)(?:(?!。)[\s\S]){0,40}(?:之日|当日起)?(?:成立并)?生效"
        r"|经?(?:签约各方|双方).{0,6}(?:签字|签名).{0,4}后生效",
        text or "",
    ):
        return model
    return model.model_copy(update={"effective_date": model.signature_date})


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
