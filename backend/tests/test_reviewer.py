"""reviewer 单测：盲审归一化 + 主审/复核 diff 合并（纯函数，不调 LLM）+ 图双审接线。"""

from datetime import date
from decimal import Decimal

from backend.app.graph import ReviewRunner
from backend.app.reviewer import (
    BlindReviewOutput,
    ReviewFinding,
    merge_review,
    normalize_findings,
)
from backend.app.policy_rag import PolicyHit
from backend.app.schemas import ContractModel, PaymentTerm, RiskItem, Severity


def _fake_retriever(query: str) -> list[PolicyHit]:
    """假政策检索：任意 query 回 P-03 命中（离线图测试用，避免真 embedding 调用）。"""
    return [PolicyHit(policy_ref="P-03", source="P-03.md", text="P-03 制度条文", score=0.9)]


def _risk(risk_type: str, severity: Severity, field: str | None = None) -> RiskItem:
    """最小主审 RiskItem 工厂（离线构造用）。"""
    return RiskItem(
        risk_type=risk_type,
        severity=severity,
        clause_ref="第四条",
        evidence="原文证据",
        suggestion="建议整改",
        field=field,
    )


def _finding(
    risk_type: str,
    severity: Severity,
    clause_ref: str = "第四条",
    evidence: str = "复核原文证据",
) -> ReviewFinding:
    """最小复核发现工厂。"""
    return ReviewFinding(
        risk_type=risk_type,
        severity=severity,
        clause_ref=clause_ref,
        evidence=evidence,
        policy_ref="P-03",
        suggestion="复核建议",
    )


# ---- normalize_findings：漂移收敛 ----


def test_normalize_findings_valid_and_drops() -> None:
    """合法条目保留（severity 别名收敛）；非 high/未登记类型丢弃并给原因（盲审只收 high）。"""
    raw = [
        {"risk_type": "penalty_rate_too_high", "severity": "HIGH", "clause_ref": "第八条"},
        {"risk_type": "warranty_too_short", "severity": "高"},
        {"risk_type": "not_a_risk_type", "severity": "high"},  # 未登记 → 丢
        {"risk_type": "penalty_rate_too_high", "severity": "maybe"},  # severity 无法识别 → 丢
        {"risk_type": "", "severity": "high"},  # 空类型 → 丢
        {"risk_type": "confidentiality_missing", "severity": "medium"},  # 提示级 → 丢
        {"risk_type": "confidentiality_too_long", "severity": "low"},  # low 纯噪音 → 丢
        "not-a-dict",  # 脏元素 → 丢
    ]
    findings, dropped = normalize_findings(raw)
    assert [f.risk_type for f in findings] == ["penalty_rate_too_high", "warranty_too_short"]
    assert findings[0].severity == Severity.high
    assert len(dropped) == 6


def test_normalize_findings_dedupe_and_cap() -> None:
    """同 (type, severity, clause) 去重；超过上限截断并给原因。"""
    a = {"risk_type": "prepayment_ratio_high", "severity": "high", "clause_ref": "第六条"}
    b = {"risk_type": "warranty_too_short", "severity": "high", "clause_ref": "第九条"}
    # 上限截断：两个不同发现只留第一个
    findings, dropped = normalize_findings([a, b], max_findings=1)
    assert len(findings) == 1
    assert any("截断" in d for d in dropped)
    # 去重：同一条重复输出只留一条
    findings, dropped = normalize_findings([a, dict(a)])
    assert len(findings) == 1
    assert any("重复" in d for d in dropped)


# ---- merge_review：D26 四类 diff ----


def test_merge_case_agree_keeps_single() -> None:
    """一致：主审与复核同 type 同 severity → 不重复并入，记 agreed。"""
    main = [_risk("penalty_rate_too_high", Severity.high)]
    outcome = merge_review(main, [_finding("penalty_rate_too_high", Severity.high)])
    assert len(outcome.risks) == 1
    assert outcome.review["stats"]["agreed"] == 1
    assert outcome.review["details"][0]["outcome"] == "agreed"


def test_merge_case_added_high_goes_into_risks() -> None:
    """复核新增：主审没报、复核报 high → 并入 risks（origin=review，走既有 gate）。"""
    f = _finding(
        "confidentiality_too_long",
        Severity.high,
        evidence="保密期限自合同终止之日起 60 个月。",
    )
    outcome = merge_review([], [f])
    assert len(outcome.risks) == 1
    item = outcome.risks[0]
    assert item.origin == "review"
    assert item.severity == Severity.high
    assert outcome.review["stats"]["added"] == 1


def test_merge_case_severity_upgrade_takes_high() -> None:
    """severity 分歧：都报但复核更高且通过复核门 → 主审项升级为 high 并标注。"""
    main = [_risk("prepayment_ratio_high", Severity.medium)]
    f = _finding(
        "prepayment_ratio_high",
        Severity.high,
        evidence="预付款为合同总额的 60%。",
    )
    outcome = merge_review(main, [f])
    assert outcome.risks[0].severity == Severity.high
    assert outcome.review["stats"]["upgraded"] == 1
    assert outcome.review["details"][0]["outcome"] == "upgraded"


def test_merge_case_reviewer_lower_keeps_main_high() -> None:
    """分歧反向：主审 high、复核 medium → 维持主审，记一致不降级。"""
    main = [_risk("warranty_too_short", Severity.high)]
    outcome = merge_review(main, [_finding("warranty_too_short", Severity.medium)])
    assert outcome.risks[0].severity == Severity.high
    assert outcome.review["stats"]["agreed"] == 1


def test_merge_medium_only_finding_not_merged() -> None:
    """复核只报 medium/low（主审没有）→ 不并入 risks（防污染评级/零误报），只记提示。"""
    outcome = merge_review([], [_finding("confidentiality_missing", Severity.medium)])
    assert outcome.risks == []
    assert outcome.review["stats"]["noted"] == 1
    assert outcome.review["details"][0]["outcome"] == "noted"


def test_merge_blank_template_missing_not_upgraded() -> None:
    """护栏：主审已判空白模板（缺必填降级）→ 复核 missing high 只提示不升级。"""
    main = [
        _risk("missing_required_field", Severity.medium, field="effective_date"),
        _risk("blank_template_suspected", Severity.medium),
    ]
    outcome = merge_review(main, [_finding("missing_required_field", Severity.high)])
    # 两条主审风险原样保留，没有升级成 high（否则空白模板会被复核顶回闸口）
    assert [r.severity for r in outcome.risks] == [Severity.medium, Severity.medium]
    assert outcome.review["stats"]["noted"] == 1


def test_merge_does_not_mutate_input() -> None:
    """merge 不改入参主审列表（复制防副作用，重审循环可复用原清单）。"""
    main = [_risk("prepayment_ratio_high", Severity.medium)]
    f = _finding(
        "prepayment_ratio_high",
        Severity.high,
        evidence="预付款为合同总额的 60%。",
    )
    outcome = merge_review(main, [f])
    assert outcome.risks[0].severity == Severity.high
    assert main[0].severity == Severity.medium


# ---- 复核门：数值型 high 必须由 evidence 原句支撑（防 LLM 算术误报）----


def test_merge_gate_rejects_wrong_prepay_math() -> None:
    """预付款 40万/200万=20% 合规 → 复核 high 被复核门拦下，只记提示不并入。"""
    f = _finding(
        "prepayment_ratio_high",
        Severity.high,
        evidence="预付款：400,000 元；合同总价款为 2,000,000 元。",
    )
    outcome = merge_review([], [f])
    assert outcome.risks == []
    assert outcome.review["stats"]["noted"] == 1
    assert "无法解析" in outcome.review["details"][0]["note"] or "20.0%" in outcome.review["details"][0]["note"]


def test_merge_gate_rejects_milestone_payment_as_prepay() -> None:
    """里程碑首期款（无"预付"字样）不适用 P-01 → 复核 prepayment high 被拦（tech_03 回归）。"""
    f = _finding(
        "prepayment_ratio_high",
        Severity.high,
        evidence="自合同生效后 10 个工作日内支付合同金额的 50%。",
    )
    outcome = merge_review([], [f])
    assert outcome.risks == []
    assert "普通分期" in outcome.review["details"][0]["note"]


def test_merge_gate_rejects_compliant_warranty_upgrade() -> None:
    """质保 24 个月合规 → 复核 warranty_too_short high 被拦（evidence 算不出违规）。"""
    f = _finding(
        "warranty_too_short",
        Severity.high,
        evidence="质保期为验收合格之日起 24 个月。",
    )
    outcome = merge_review([], [f])
    assert outcome.risks == []
    assert outcome.review["stats"]["noted"] == 1


def test_merge_gate_blocks_medium_type_upgrade() -> None:
    """提示级类型（ip_ownership_unclear）复核 high → 不在白名单，不升级（锁 sample_09 回归）。"""
    main = [_risk("ip_ownership_unclear", Severity.medium, field="ip_ownership")]
    f = _finding(
        "ip_ownership_unclear",
        Severity.high,
        evidence="本项目知识产权归乙方所有。",
    )
    outcome = merge_review(main, [f])
    assert outcome.risks[0].severity == Severity.medium  # 维持主审 medium
    assert outcome.review["stats"]["upgraded"] == 0
    assert outcome.review["details"][0]["outcome"] == "noted"


def test_merge_gate_accepts_plain_violation_text() -> None:
    """原文直接写明违规比例（预付款 60%）→ 复核门放行并入。"""
    f = _finding(
        "prepayment_ratio_high",
        Severity.high,
        evidence="预付款为合同总价款的 60%，验收合格后支付剩余 40%。",
    )
    outcome = merge_review([], [f])
    assert len(outcome.risks) == 1
    assert outcome.risks[0].origin == "review"


def test_merge_gate_accepts_liability_cap_plain_violation() -> None:
    """evidence 明确写赔偿责任上限 20%（<30）→ 复核门放行。"""
    f = _finding(
        "liability_cap_too_low",
        Severity.high,
        evidence="乙方赔偿责任以合同总价的 20% 为限。",
    )
    outcome = merge_review([], [f])
    assert len(outcome.risks) == 1


def test_merge_gate_rejects_penalty_cap_as_liability() -> None:
    """违约金总额上限/违约金率不算赔偿责任上限 → 复核 liability high 被拦（tech_01 回归）。"""
    f = _finding(
        "liability_cap_too_low",
        Severity.high,
        evidence="每次违约，违约方需支付合同总价的 10%，违约金总额不超过合同金额 30%。",
    )
    outcome = merge_review([], [f])
    assert outcome.risks == []
    assert "非赔偿责任上限" in outcome.review["details"][0]["note"]


# ---- 图接线：double 全链路（离线假抽取/假复核）----


def _normal_model() -> ContractModel:
    """等价无缺陷企业合同（rules 零风险，主审 pass）。"""
    return ContractModel(
        buyer="星辰智造科技有限公司",
        supplier="华芯电子有限公司",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 3, 9),
        total_amount=Decimal("1000000"),
        currency="人民币",
        payment_schedule=[PaymentTerm(name="验收后支付", amount=Decimal("1000000"), percent=100.0)],
        penalty_rate=0.05,
        liability_cap=100.0,
        warranty_months=24,
        confidentiality_months=24,
        ip_ownership="定制成果知识产权归甲方所有",
        governing_law="中华人民共和国法律",
    )


def test_double_reviewer_addition_gates_and_survives_resume() -> None:
    """double：复核新增 high → 与主审合并后停闸口；approved 恢复后报告含 review 段。"""
    fake_reviewer = lambda text: BlindReviewOutput(
        findings=[
            _finding(
                "penalty_rate_too_high",
                Severity.high,
                clause_ref="第八条",
                evidence="每逾期一日按合同总价款的 3% 支付违约金。",
            )
        ]
    )
    runner = ReviewRunner(
        extractor=lambda text: _normal_model(),
        retriever=_fake_retriever,
        reviewer=fake_reviewer,
        review_mode="double",
    )
    state = runner.start("clean.md", text="第一条 正常合同正文")
    tid = runner.last_thread_id
    # 主审 pass 但复核新增 high → 图应停在 gate 等人工
    assert runner.store.get(tid).status == "gate"
    payload = runner.pending(tid)
    assert any(r["risk_type"] == "penalty_rate_too_high" for r in payload["high_risks"])
    state = runner.resume(tid, action="approved", note="复核项人工确认")
    report = state["report"]
    assert report["grade"] == "fail"
    added = [r for r in report["risks"] if r.get("origin") == "review"]
    assert [r["risk_type"] for r in added] == ["penalty_rate_too_high"]
    assert report["review"]["stats"]["added"] == 1


def test_double_agreed_high_no_duplicate() -> None:
    """double：主审与复核同报一个 high → 合并后只留一条，报告 review 段记 agreed。"""

    def defect_model() -> ContractModel:
        return _normal_model().model_copy(update={"warranty_months": 6})

    fake_reviewer = lambda text: BlindReviewOutput(
        findings=[_finding("warranty_too_short", Severity.high, clause_ref="第九条")]
    )
    runner = ReviewRunner(
        extractor=lambda text: defect_model(),
        retriever=_fake_retriever,
        reviewer=fake_reviewer,
        review_mode="double",
    )
    state = runner.start("defect.md", text="第一条 质保期六个月的合同正文")
    tid = runner.last_thread_id
    state = runner.resume(tid, action="approved", note="同意放行")
    report = state["report"]
    same_type = [r for r in report["risks"] if r["risk_type"] == "warranty_too_short"]
    assert len(same_type) == 1  # 一致不重复并入
    assert report["review"]["stats"]["agreed"] == 1


def test_single_mode_report_has_no_review_section() -> None:
    """single：图不经过 review 节点，报告无 review 段（向后兼容）。"""
    runner = ReviewRunner(
        extractor=lambda text: _normal_model(),
        retriever=_fake_retriever,
        review_mode="single",
    )
    state = runner.start("clean.md", text="第一条 正常合同正文")
    assert state["report"]["grade"] == "pass"
    assert state["report"].get("review") is None


def test_double_reviewer_failure_falls_back_to_main() -> None:
    """double：盲审器抛异常 → 回退主审结果，report review 段附 error 不阻断。"""

    def boom(text: str) -> BlindReviewOutput:
        raise RuntimeError("LLM 超时")

    runner = ReviewRunner(
        extractor=lambda text: _normal_model(),
        retriever=_fake_retriever,
        reviewer=boom,
        review_mode="double",
    )
    state = runner.start("clean.md", text="第一条 正常合同正文")
    assert state["report"]["grade"] == "pass"
    assert "盲审失败" in state["report"]["review"]["error"]
