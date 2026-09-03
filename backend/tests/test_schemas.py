"""schemas 单测：数据结构定死后的序列化与默认值契约。"""

from datetime import date
from decimal import Decimal

from backend.app.schemas import (
    ContractModel,
    Evidence,
    Grade,
    Report,
    RiskItem,
    Severity,
)


def test_contract_model_typed_defaults() -> None:
    cm = ContractModel(
        buyer="甲方公司",
        total_amount=Decimal("1000000"),
        effective_date="2026-03-10",  # pydantic 自动转 date
        warranty_months=24,
        extraction_meta={"warranty_months": Evidence(quote="24 个月", confidence=0.95)},
    )
    assert cm.effective_date == date(2026, 3, 10)
    assert cm.expiry_date is None
    assert cm.extraction_meta["warranty_months"].confidence == 0.95


def test_risk_item_and_report_json_roundtrip() -> None:
    risk = RiskItem(
        risk_type="prepayment_ratio_high",
        severity=Severity.high,
        clause_ref="第二条",
        evidence="预付款：480,000 元",
        policy_ref="P-01",
        suggestion="将预付款降至 30% 以内",
        field="payment_schedule",
    )
    report = Report(contract_title="测试合同", risks=[risk], grade=Grade.conditional_pass)
    data = report.model_dump()
    assert data["grade"] == "conditional_pass"
    assert data["risks"][0]["severity"] == "high"
    again = Report.model_validate(data)
    assert again.risks[0].policy_ref == "P-01"
