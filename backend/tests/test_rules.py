"""rules 单测：确定性规则引擎对正常/缺陷样本的命中与评级(TDD)。"""

from datetime import date
from decimal import Decimal

from backend.app.rules import evaluate, grade_report
from backend.app.schemas import ContractModel, Grade, PaymentTerm, Severity


def _term(name: str, amount: str, percent: float | None) -> PaymentTerm:
    return PaymentTerm(name=name, amount=Decimal(amount), percent=percent)


def _normal() -> ContractModel:
    """等价 sample_01: 无缺陷(预付款 20%、质保 24、责任上限 100%、保密 24 个月)"""
    return ContractModel(
        buyer="星辰智造科技有限公司",
        supplier="华芯电子有限公司",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 3, 9),
        total_amount=Decimal("1000000"),
        currency="人民币",
        payment_schedule=[
            _term("预付款", "200000", 20.0),
            _term("验收合格后支付", "800000", 80.0),
        ],
        penalty_rate=0.05,
        liability_cap=100.0,
        warranty_months=24,
        termination_notice_days=30,
        ip_ownership="定制成果知识产权归甲方（采购方）所有",
        confidentiality_months=24,
        governing_law="中华人民共和国法律",
    )


def _with(**overrides) -> ContractModel:
    """基于正常样本覆盖指定字段，构造缺陷样本。"""
    return _normal().model_copy(update=overrides)


def _risk_types(model: ContractModel) -> set[str]:
    return {r.risk_type for r in evaluate(model)}


def test_normal_contract_no_risk_and_pass() -> None:
    risks = evaluate(_normal())
    assert risks == []
    assert grade_report(risks) == Grade.pass_


def test_sample03_penalty_liability_and_amount_high() -> None:
    # 违约金日 1.5%、责任上限 5%、分项 20+50+40=110 万 ≠ 总额 100 万
    model = _with(
        penalty_rate=1.5,
        liability_cap=5.0,
        payment_schedule=[
            _term("预付款", "200000", 20.0),
            _term("第二批（到货后）", "500000", 50.0),
            _term("第三批（验收后）", "400000", 40.0),
        ],
    )
    types = _risk_types(model)
    assert "penalty_rate_too_high" in types
    assert "liability_cap_too_low" in types
    assert "amount_inconsistency" in types
    # 三个命中都应是 high，评级不通过
    severities = {r.severity for r in evaluate(model)}
    assert severities == {Severity.high}
    assert grade_report(evaluate(model)) == Grade.fail


def test_sample04_prepayment_over_limit_and_missing_confidentiality() -> None:
    model = _with(
        payment_schedule=[_term("预付款", "480000", 60.0), _term("验收合格后支付", "320000", 40.0)],
        confidentiality_months=None,
    )
    types = _risk_types(model)
    assert "prepayment_ratio_high" in types
    assert "confidentiality_missing" in types
    assert any(r.severity == Severity.high for r in evaluate(model))


def test_sample05_warranty_too_short_and_confidentiality_too_long() -> None:
    model = _with(warranty_months=6, confidentiality_months=60)
    types = _risk_types(model)
    assert "warranty_too_short" in types
    assert "confidentiality_too_long" in types
    assert grade_report(evaluate(model)) == Grade.fail


def test_date_logic_effective_before_signature() -> None:
    model = _with(effective_date=date(2026, 2, 1), signature_date=date(2026, 3, 10))
    types = _risk_types(model)
    assert "date_logic_effective_before_signature" in types
    assert grade_report(evaluate(model)) == Grade.conditional_pass


def test_expiry_not_after_effective() -> None:
    model = _with(effective_date=date(2026, 3, 10), expiry_date=date(2026, 3, 10))
    assert "date_logic_expiry_not_after_effective" in _risk_types(model)


def test_liability_cap_unset_is_medium_not_high() -> None:
    risks = evaluate(_with(liability_cap=None))
    caps = [r for r in risks if r.risk_type == "liability_cap_unclear"]
    assert caps and caps[0].severity == Severity.medium


def test_missing_governing_law_and_ip() -> None:
    model = _with(governing_law=None, ip_ownership=None)
    types = _risk_types(model)
    assert "governing_law_missing" in types
    assert "ip_ownership_missing" in types


def test_gov_goods_genre_missing_fields_not_flagged() -> None:
    """政采校服类：天然不含责任上限/保密/IP/适用法律条款 → 不应误报 medium。"""
    model = _with(
        contract_kind="gov_goods",
        liability_cap=None,
        confidentiality_months=None,
        ip_ownership=None,
        governing_law=None,
    )
    assert _risk_types(model) == set()


def test_agri_goods_requires_conf_and_law_only() -> None:
    """农副类基线：要求保密与适用法律；不要求责任上限/IP。"""
    model = _with(
        contract_kind="agri_goods",
        liability_cap=None,
        confidentiality_months=None,
        ip_ownership=None,
        governing_law=None,
    )
    types = _risk_types(model)
    assert types == {"confidentiality_missing", "governing_law_missing"}


def test_enterprise_and_tech_require_all_genre_fields() -> None:
    """企业/技术类基线：责任上限、保密、IP、适用法律都属应有条款。"""
    for kind in ("enterprise_goods", "tech_service"):
        model = _with(
            contract_kind=kind,
            liability_cap=None,
            confidentiality_months=None,
            ip_ownership=None,
            governing_law=None,
        )
        types = _risk_types(model)
        assert {
            "liability_cap_unclear",
            "confidentiality_missing",
            "ip_ownership_missing",
            "governing_law_missing",
        } <= types


def test_gov_goods_present_defects_still_flagged() -> None:
    """品类不豁免"写出来的缺陷"：质保过短/违约金畸高在 gov_goods 下同样命中。"""
    model = _with(contract_kind="gov_goods", warranty_months=6, penalty_rate=1.5)
    types = _risk_types(model)
    assert "warranty_too_short" in types
    assert "penalty_rate_too_high" in types


def test_empty_model_does_not_crash() -> None:
    risks = evaluate(ContractModel())
    assert risks  # 全空合同应至少报出必填缺失类风险
    assert grade_report(risks) in {Grade.fail, Grade.conditional_pass}
