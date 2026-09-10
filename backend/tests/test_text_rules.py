"""text_rules（P-06~P-09 文本级条款基线）单测：纯函数、离线可跑。

覆盖四类规则各自的命中/放行分支、品类豁免（gov/agri 转包）、空白模板护栏、
新风险类型在 RISK_LABELS 的登记（评测/界面依赖该字典）。
"""

from __future__ import annotations

from backend.app.rules import RISK_LABELS, Severity, text_rules


def _types(text: str, kind: str | None = "enterprise_goods") -> dict[str, str]:
    """跑 text_rules 并折叠成 {risk_type: severity}，便于断言。"""
    return {r.risk_type: r.severity.value for r in text_rules(text, kind)}


def test_new_risk_types_registered_in_labels() -> None:
    """四类新风险类型必须在 RISK_LABELS 登记，否则界面裸显机器码。"""
    for risk_type in (
        "acceptance_unclear",
        "invoice_unclear",
        "performance_bond_missing",
        "subcontract_unrestricted",
    ):
        assert risk_type in RISK_LABELS


# ---- P-06 交付与验收 ----


def test_acceptance_absent_fires_medium() -> None:
    """正文完全没有验收安排 → acceptance_unclear medium（企业基线要求验收条款）。"""
    assert _types("乙方向甲方供应货物，货款 100,000 元。") == {
        "acceptance_unclear": Severity.medium.value,
        "invoice_unclear": Severity.medium.value,
        "subcontract_unrestricted": Severity.medium.value,
    }


def test_acceptance_with_std_and_deadline_clean() -> None:
    """验收条款带标准与期限（10 个工作日内 + 技术规范为准）→ 不报。"""
    text = "甲方应在收到货物后 10 个工作日内组织验收，验收合格标准以双方确认的技术规范为准。"
    assert "acceptance_unclear" not in _types(text)


def test_acceptance_mentioned_but_no_std_or_deadline_fires() -> None:
    """只把验收当付款触发点（验收合格后支付）、无标准/期限 → medium（P-06 口径）。"""
    text = "乙方交货后，甲方验收合格后支付全部货款。"
    assert _types(text)["acceptance_unclear"] == Severity.medium.value


# ---- P-07 付款与发票 ----


def test_invoice_absent_with_payment_fires() -> None:
    """有付款安排但全文无发票/开票字样 → invoice_unclear medium。"""
    text = "双方约定按如下期次支付合同价款：交付后 30 日内支付 100,000 元。"
    assert _types(text)["invoice_unclear"] == Severity.medium.value


def test_invoice_mentioned_clean() -> None:
    """已约定先票后款/开具发票 → 不报。"""
    text = "乙方应在甲方付款前开具等额增值税专用发票。"
    assert "invoice_unclear" not in _types(text)
    assert "invoice_unclear" not in _types("货款按先票后款结算。")


def test_invoice_without_payment_context_skips() -> None:
    """正文没有付款/结算安排（如纯服务说明）→ 发票义务无从依附，不报。"""
    text = "乙方向甲方提供咨询服务，服务范围以附件为准。"
    assert "invoice_unclear" not in _types(text)


# ---- P-08 履约担保 ----


def test_bond_missing_with_prepay_fires() -> None:
    """含预付款期次但无任何担保字样 → performance_bond_missing medium。"""
    text = "合同总价款为 500,000 元。甲方支付预付款 100,000 元，余款验收后付清。"
    assert _types(text)["performance_bond_missing"] == Severity.medium.value


def test_bond_gate_covers_first_payment_wording() -> None:
    """P-08 的预付触发与 P-01 口径一致："首付款"同样计入（真实合同走查修复）。"""
    text = "合同总价款为 500,000 元。甲方支付首付款 100,000 元，余款验收后付清。"
    assert _types(text)["performance_bond_missing"] == Severity.medium.value


def test_bond_missing_with_big_total_fires() -> None:
    """总额 100 万以上（无预付）也无担保 → medium（金额门槛生效）。"""
    assert _types("合同总价款为人民币（大写）壹佰万元整（小写：1,000,000 元）。")[
        "performance_bond_missing"
    ] == Severity.medium.value
    assert _types("本合同开发费总额为 120 万元。")["performance_bond_missing"] == Severity.medium.value


def test_bond_small_no_prepay_skips() -> None:
    """小额且无预付 → 不在担保审查范围，不报。"""
    text = "合同总价款为 50,000 元，验收合格后一次性付清。"
    assert "performance_bond_missing" not in _types(text)


def test_bond_present_clean() -> None:
    """已有履约保函/质保金/保证金 → 不报（含质保金口径，范围卡列明）。"""
    for text in (
        "合同总价款为 2,000,000 元，乙方应向甲方提供合同总价款 10% 的银行保函。",
        "合同总价款为 2,000,000 元，甲方按合同总价款的 5% 预留质保金。",
        "乙方应缴纳履约保证金后方可签订合同。",
    ):
        assert "performance_bond_missing" not in _types(text)


# ---- P-09 转包/分包限制 ----


def test_subcontract_unrestricted_fires_medium() -> None:
    """企业合同未限制转包/分包 → medium。"""
    text = "乙方向甲方供应定制设备，交货期为 2026 年 12 月 31 日前。"
    assert _types(text)["subcontract_unrestricted"] == Severity.medium.value


def test_subcontract_restricted_clean() -> None:
    """已有不得转包/转包须经同意限制句 → 不报（三种真实写法各验一句）。"""
    for text in (
        "乙方不得将本合同项下义务转包或分包。",
        "未经甲方书面同意，乙方不得将本项目关键开发工作转委托给第三方。",
        "乙方转包须经甲方书面批准。",
    ):
        assert "subcontract_unrestricted" not in _types(text)


def test_subcontract_waiver_fires_high() -> None:
    """明文允许任意转包且甲方无权追责 → subcontract_unrestricted high（批1 唯一 high）。"""
    text = (
        "乙方可将本合同项下全部或部分工作任意转包给第三方，无需征得甲方同意；"
        "因转包产生的责任与甲方无关。"
    )
    assert _types(text)["subcontract_unrestricted"] == Severity.high.value


def test_agri_kind_no_subcontract_baseline() -> None:
    """农副产品买卖无转包概念：agri 品类不套转包基线（范围卡判定草案口径）。"""
    text = "甲方采购农副产品，货到验收后结算货款。"
    kinds = _types(text, "agri_goods")
    assert "subcontract_unrestricted" not in kinds


# ---- 品类豁免与护栏 ----


def test_gov_kind_exempt_all_text_rules() -> None:
    """政采/校服（gov_goods）按示范文本豁免，四类文本规则都不跑。"""
    text = "校服采购，按教育部门示范文本执行。"  # 无验收/发票/担保/转包限制也不报
    assert _types(text, "gov_goods") == {}


def test_blank_template_text_skips_all_rules() -> None:
    """空白模板占位 ≥2 类 → 条款完备性提示是噪音，直接返回空（缺必填已降级处理）。"""
    blank = (
        "甲方（采购方）：＿＿＿＿\n乙方（供应商）：＿＿＿＿\n"
        "签订时间： 年 月 日\n货款金额为： 元（大写：）"
    )
    assert text_rules(blank, "enterprise_goods") == []


def test_kind_none_defaults_to_enterprise() -> None:
    """kind=None 按 enterprise_goods 兜底（与 evaluate 的 KIND_BASELINE 口径一致）。"""
    text = "乙方不得转包，货款验收后支付。"
    kinds = _types(text, None)
    # enterprise 基线要求转包限制与验收；正文只满足其一 → 另一条 medium 照报
    assert "acceptance_unclear" in kinds


def test_all_four_fire_on_bare_big_prepay_contract() -> None:
    """最简触发文本（大额+预付+无任何条款）→ 四条 medium 齐发（顺序固定）。"""
    text = "合同总价款为 2,000,000 元。甲方支付预付款 500,000 元，余款验收合格后付清。"
    risks = text_rules(text, "enterprise_goods")
    assert [r.risk_type for r in risks] == [
        "acceptance_unclear",
        "invoice_unclear",
        "performance_bond_missing",
        "subcontract_unrestricted",
    ]
    assert all(r.policy_ref in ("P-06", "P-07", "P-08", "P-09") for r in risks)
    assert all(r.label for r in risks)  # 每条都有中文展示名
    assert all(r.evidence for r in risks)  # 每条都带原文证据
