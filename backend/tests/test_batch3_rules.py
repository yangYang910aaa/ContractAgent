"""保密例外 / 违约金口径 / 文档形态判定等文本规则单测（离线，无 API）。

覆盖：保密例外 / 违约金基数 / 违约金上限三条新规则，以及三处真实合同修正
（打码误伤空白模板、保密文案错位、农副随行就市金额口径）。
"""

from __future__ import annotations

from datetime import date

from backend.app.extractor import build_contract_model
from backend.app.schemas import ContractModel
from backend.app.rules import (
    annotate_open_ended_risks,
    infer_effective_from_signature,
    is_blank_template_suspect,
    text_rules,
)
# 口径正则统一放在 rules.constants（拆分后跨模块共用，避免两处漂移）：
# 用例要证明"页标记会吃掉窗口"
from backend.app.rules.constants import SIGNING_EFFECT_RE
from backend.app.schemas import RiskItem, Severity

# 企业合同底稿：验收/发票/担保/转包/法律条款齐全，只让保密与违约金两句可替换，
# 这样断言不会被其它规则干扰（底稿本身应零风险）
_BASE = (
    "甲方：某采购方　乙方：某供应商\n"
    "第一条 合同总价款为人民币 1,000,000 元。\n"
    "第二条 乙方应于生效后 45 日内交付，甲方应在收货后 10 个工作日内组织验收，"
    "验收标准以双方确认的技术规范为准。\n"
    "第三条 乙方应在本合同签订后 10 日内向甲方提供合同总价款 10% 的银行保函作为履约担保。\n"
    "第四条 付款方式：合同签订后 10 日内支付预付款 20%，验收合格后支付剩余 80%；"
    "乙方应在收款前向甲方开具增值税专用发票。\n"
    "第五条 {penalty}\n"
    "第六条 {confidentiality}\n"
    "第七条 乙方未经甲方书面同意不得将本项目转包或转委托给第三方。\n"
    "第八条 本合同适用中华人民共和国法律，争议提交甲方所在地人民法院诉讼解决。\n"
)

_CLEAN_PENALTY = (
    "乙方逾期交付的，每逾期一日按合同总价款的 0.05% 向甲方支付违约金；"
    "除违约金外，乙方对甲方承担的赔偿责任总额以合同总价款的 100% 为上限。"
)
_CLEAN_CONF = (
    "双方对因履行本合同而知悉的对方商业秘密负有保密义务；除法律法规要求、监管或"
    "司法机关要求披露，以及已公开信息、经对方书面同意外，不得向第三方披露。"
    "保密期限自本合同终止之日起 24 个月。"
)
# 断言只关心这三类，其它规则命中不参与
_BATCH3_TYPES = {"confidentiality_no_exception", "penalty_basis_unclear", "penalty_cap_missing"}


def _types(text: str, kind: str = "enterprise_goods") -> dict[str, str]:
    """跑文本规则 → {risk_type: severity}（便于按类型断言严重级）。"""
    return {r.risk_type: r.severity.value for r in text_rules(text, kind)}


def _text(penalty: str = _CLEAN_PENALTY, confidentiality: str = _CLEAN_CONF) -> str:
    """按底稿拼一份合同，只替换违约金句与保密句。"""
    return _BASE.format(penalty=penalty, confidentiality=confidentiality)


def test_clean_baseline_has_no_batch3_hits() -> None:
    """底稿（保密含例外、违约金基数与上限齐全）不应命中这三条中的任何一条。"""
    assert _BATCH3_TYPES.isdisjoint(_types(_text()))


def test_absolute_confidentiality_without_exception_is_medium() -> None:
    """绝对禁止式披露、无任何例外 → P-13 medium（素材清单描述的目标缺陷形态）。"""
    text = _text(confidentiality=(
        "双方对因履行本合同而知悉的对方商业秘密负有保密义务，不得向任何第三方披露、提供或公开。"
        "保密期限自本合同终止之日起 24 个月。"
    ))
    assert _types(text)["confidentiality_no_exception"] == "medium"


def test_plain_confidentiality_obligation_not_flagged() -> None:
    """普通保密义务句（存量语料写法）不算缺陷——否则会有大面积误报。"""
    text = _text(confidentiality=(
        "双方对因履行本合同而知悉的对方商业秘密负有保密义务。"
        "保密期限自本合同终止之日起 24 个月。"
    ))
    assert "confidentiality_no_exception" not in _types(text)


def test_neighbor_clause_wording_is_not_a_confidentiality_exception() -> None:
    """隔壁条款的"未经甲方书面同意"（转包）不能被当成保密例外。"""
    text = _text(confidentiality=(
        "双方对因履行本合同而知悉的对方商业秘密负有保密义务，不得向任何第三方披露。"
        "保密期限自本合同终止之日起 24 个月。"
    ))
    # 底稿第七条就有"未经甲方书面同意"，且距离 confidential 句 60 字开外
    assert _types(text)["confidentiality_no_exception"] == "medium"


def test_penalty_basis_unclear_is_medium() -> None:
    """只写费率不写基数 → P-14 medium。"""
    text = _text(penalty=(
        "乙方逾期交付的，每逾期一日按 0.05% 向甲方支付违约金；"
        "除违约金外，乙方对甲方承担的赔偿责任总额以合同总价款的 100% 为上限。"
    ))
    assert _types(text)["penalty_basis_unclear"] == "medium"


def test_penalty_with_basis_not_flagged() -> None:
    """句内写了基数（含"逾期部分货款"这类写法）→ 不报基数问题。"""
    text = _text(penalty=(
        "乙方逾期交付部分货款的，每逾期一日按逾期交付部分货款的 0.05% 向甲方支付违约金；"
        "赔偿责任总额以合同总价款的 100% 为上限。"
    ))
    assert "penalty_basis_unclear" not in _types(text)


def test_penalty_cap_missing_high_for_high_daily_rate() -> None:
    """按日 0.5% 且无任何上限 → 违约金失控，判高风险。"""
    text = _text(penalty="乙方逾期交付的，每逾期一日按合同总价款的 0.5% 向甲方支付违约金。")
    assert _types(text)["penalty_cap_missing"] == "high"


def test_penalty_low_daily_rate_without_cap_not_flagged() -> None:
    """日费率低（0.05%）时无上限属行业常见写法 → 不报（防噪音）。"""
    text = _text(penalty="乙方逾期交付的，每逾期一日按合同总价款的 0.05% 向甲方支付违约金。")
    assert "penalty_cap_missing" not in _types(text)


def test_cap_in_another_clause_does_not_count() -> None:
    """其它条款写了上限，不能算本条违约金的封顶（真实钢结构合同的形态）。"""
    filler = "乙方应妥善保管甲方提供的全部技术资料与备件，并在项目结束后按清单归还。" * 3
    text = _text(penalty=(
        "乙方逾期交付的，每逾期一日按合同总价款的 0.5% 向甲方支付违约金。"
        + filler
        + "本工程结算价款的支付以竣工验收为前提，尾款最多不超过结算价款的 8%。"
    ))
    assert _types(text)["penalty_cap_missing"] == "high"


def test_gov_kind_skips_batch3_rules() -> None:
    """政采/校服类按示范文本执行 → 本组规则整组不跑（5‰/日 无上限不误报）。"""
    text = _text(penalty="乙方逾期交付的，每逾期一日按合同总价款的 0.5% 向甲方支付违约金。")
    assert _types(text, kind="gov_goods") == {}


def test_pdf_linebreak_artifacts_not_blank_template() -> None:
    """PDF 折行造出的"值\\n，"与" % ≤3.0%"不是空白模板证据。"""
    text = (
        "热值、全硫、全水、供应量都应为预测单一数值\n，\n"
        "全硫 干燥基St,d % ≤3.0%\n"
        "2025年9月1日 签订\n甲方见票后 30 天内支付货款 1,000,000 元。\n"
    )
    assert is_blank_template_suspect(text) is False


def test_fill_in_blank_template_still_detected() -> None:
    """真正的填空式模板仍要识别（根因修复不能把该拦的也放过去）。"""
    text = (
        "（二）验收标准、方法 。\n"
        "（二）包装物由 方提供，费用由 方承担。\n"
        "甲方支付乙方定金 元；交付后按约定价格的 % 计算违约金。\n"
    )
    assert is_blank_template_suspect(text) is True


def _missing_total() -> list[RiskItem]:
    """造一条"缺合同总额 high"，用于验证开放式金额降级。"""
    return [
        RiskItem(
            risk_type="missing_required_field",
            label="缺失必填字段",
            severity=Severity.high,
            field="total_amount",
            evidence="",
            suggestion="缺失必填字段「合同总额」，请人工确认或补全后再审。",
        )
    ]


def test_rush_market_pricing_downgrades_missing_total() -> None:
    """农副随行就市/过磅计量合同本无固定总额 → 缺总额降为提示级，不停闸口。"""
    text = "本合同按随行就市定价，以过磅计量结果作为结算依据，按批次结算。"
    out = annotate_open_ended_risks(_missing_total(), text)
    assert out[0].severity == Severity.medium


def test_confidentiality_wording_refined_when_obligation_exists() -> None:
    """正文已写保密义务时，文案改"未约定保密期限"，不说"缺少保密条款"。"""
    risks = [
        RiskItem(
            risk_type="confidentiality_missing",
            label="缺少保密条款或未约定期限",
            severity=Severity.medium,
            field="confidentiality_months",
            evidence="",
            suggestion="缺少保密条款，建议补充（保密期宜 24 个月以上、不超过 36 个月）。",
        )
    ]
    out = annotate_open_ended_risks(risks, "双方对因履行本合同而知悉的商业秘密负有保密义务。")
    assert "未明确保密期限" in out[0].suggestion
    # 正文完全没有保密义务时保留原文案（确实是缺条款）
    out2 = annotate_open_ended_risks(risks, "本合同就货物交付与付款作出如下约定。")
    assert out2[0].suggestion == risks[0].suggestion


def test_party_alias_fallback_fills_buyer_and_supplier() -> None:
    """真实合同用"需方/供方"称谓时兜底补齐买卖双方。"""
    text = "电煤购销合同\n需方：某电力有限公司\n供方：某煤业有限公司\n一、供应煤种与数量如下。"
    model = build_contract_model({}, text)
    assert model.buyer == "某电力有限公司"
    assert model.supplier == "某煤业有限公司"


def test_party_alias_fallback_skips_masked_name() -> None:
    """打码供方（*******）不是名称 → 宁缺毋滥，保持 None（真实电煤合同形态）。"""
    text = "需方：某电力有限公司\n供方：*******（中标供应商）\n一、供应煤种与数量如下。"
    model = build_contract_model({}, text)
    assert model.buyer == "某电力有限公司"
    assert model.supplier is None


def test_label_value_is_not_taken_as_party_name() -> None:
    """模型把栏位标签当人名抄回来（"甲方（需方）"）→ 视为未填，不能当主体名展示。"""
    model = build_contract_model(
        {"buyer": "甲方（需方）", "supplier": "乙方（供方）"},
        "甲方（需方）：\n乙方（供方）：\n一、合作方式如下。",
    )
    assert model.buyer is None
    assert model.supplier is None
    # 真实名称不受影响
    model2 = build_contract_model({"buyer": "某医院"}, "甲方：某医院\n乙方：某公司\n")
    assert model2.buyer == "某医院"


def test_missing_field_gets_text_locator() -> None:
    """缺必填字段没抽到证据时，也要按字段在正文里补一个"该去哪找"的定位。"""
    text = (
        "第二条 合同总价款为人民币 1,000,000 元（币种：人民币）。\n"
        "第七条 本合同有效期至 2027 年 3 月 9 日。\n"
    )
    # 期望值是"定位到的原句里应当出现的关键词"（锚点是先具体后笼统，命中的是"合同总价款"）
    for field, expect_in in (("total_amount", "总价款"), ("expiry_date", "有效期"), ("currency", "币种")):
        risks = [
            RiskItem(
                risk_type="missing_required_field",
                label="缺失必填字段",
                severity=Severity.medium,
                field=field,
                evidence="",
                suggestion="缺失必填字段，请人工确认或补全后再审。",
            )
        ]
        out = annotate_open_ended_risks(risks, text)[0]
        assert out.evidence, f"{field} 应有原文定位"
        assert expect_in in out.evidence


def test_missing_field_locator_keeps_existing_evidence() -> None:
    """已经有证据的条目不被覆盖（只补空）。"""
    risks = [
        RiskItem(
            risk_type="missing_required_field",
            label="缺失必填字段",
            severity=Severity.medium,
            field="total_amount",
            evidence="原有证据句",
            suggestion="",
        )
    ]
    out = annotate_open_ended_risks(risks, "第二条 合同总价款为人民币 1,000,000 元。")[0]
    assert out.evidence == "原有证据句"


def test_rule_risk_gets_original_quote_for_locating() -> None:
    """规则说明句在正文里搜不到时，补 evidence_quote（真原文）供前端定位/高亮。"""
    text = (
        "第五条 服务费用的支付\n"
        "5.2付款方式 第一笔-预付款（70%）：合同签订生效后10个工作日内支付预付款；"
        "第二笔-尾款（30%）：通过最终验收后支付。\n"
    )
    risks = [
        RiskItem(
            risk_type="prepayment_ratio_high",
            label="预付款比例过高",
            severity=Severity.high,
            field="payment_schedule",
            evidence="预付款比例 70%",  # 规则生成的说明句，正文里没有
            suggestion="降至 30% 以内",
        )
    ]
    out = annotate_open_ended_risks(risks, text)[0]
    assert out.evidence == "预付款比例 70%"  # 说明文案不动
    assert out.evidence_quote and "预付款" in out.evidence_quote
    # 摘录必须真的来自原文（前端靠它滚动/高亮）；摘录已折叠空白，故两边都去空白再比
    import re as _re

    assert _re.sub(r"\s+", "", out.evidence_quote) in _re.sub(r"\s+", "", text)


def test_attached_text_used_directly_as_quote() -> None:
    """条款类风险的 evidence 本来就是原文 → 直接当摘录，不需要另找。"""
    text = "第七条 保密条款 双方对因履行本合同而知悉的对方商业秘密负有保密义务。"
    risks = [
        RiskItem(
            risk_type="confidentiality_no_exception",
            label="保密条款缺少例外",
            severity=Severity.medium,
            clause_ref="第七条",
            evidence="不得向任何第三方披露",
            suggestion="补充例外",
        )
    ]
    out = annotate_open_ended_risks(risks, text)[0]
    assert out.evidence_quote == "" or out.evidence_quote in text


def test_party_alias_fallback_skips_field_label() -> None:
    """栏位标签（签订时间）不是公司名 → 保持 None（小麦合同实测踩过的坑）。"""
    text = "卖方：\n签订时间：2023年5月1日\n买方：某粮油管理所有限公司\n"
    model = build_contract_model({}, text)
    assert model.supplier is None
    assert model.buyer == "某粮油管理所有限公司"


def test_party_alias_fallback_requires_org_like_name() -> None:
    """形态不像组织名（授权代表/Address）不补——宁缺毋滥（小麦/商用车实测踩过的坑）。"""
    text = "卖方：\n授权代表：张三\nSupplier：Address：Room 101, Building A\n甲方：某装备制造有限公司\n"
    model = build_contract_model({}, text)
    assert model.supplier is None
    assert model.buyer == "某装备制造有限公司"


def test_masked_bracket_date_downgrades_expiry() -> None:
    """【】年【】月【】日 这类占位日期算"日期未定"，缺到期日降为 medium（商用车形态）。"""
    risks = [
        RiskItem(
            risk_type="missing_required_field",
            label="缺失必填字段",
            severity=Severity.high,
            field="expiry_date",
            evidence="",
            suggestion="缺失必填字段「到期日」，请人工确认或补全后再审。",
        )
    ]
    text = "14.1 本合同有效期为【】年【】月【】日至 【】年【】月【】日，双方签字盖章后生效。"
    out = annotate_open_ended_risks(risks, text)
    assert out[0].severity == Severity.medium


# OCR 文本形态：模型抄出的摘录没有页标记空格，正文里却有（"--- 第 3 页 ---"）
# 易错点：真实 OCR 会把"单位"这样的词从中间切开（"各自单" + 页标记 + "位公章"），
# 造用例时要照抄这个形态——写成"各自单位"再插页标记会多出一个"位"，反而不真实
_OCR_SECTION_TEXT = (
    "二、其他\n"
    "3、本协议自双方法定代表人或授权代表签字并分别加盖各自单\n"
    "--- 第 3 页 ---\n"
    "位公章之日起生效。\n"
)


def _bogus_ref_risk() -> RiskItem:
    """造一条"条款号在正文里根本不存在"的风险（模拟抽取模型自述的章节号）。"""
    return RiskItem(
        risk_type="liability_cap_unclear",
        label="责任上限不明",
        severity=Severity.medium,
        evidence="本协议自双方法定代表人或授权代表签字并分别加盖各自单位公章之日起生效。",
        clause_ref="三、其他 3",
        suggestion="建议写明赔偿责任上限。",
    )


def test_hallucinated_clause_ref_corrected_by_quote() -> None:
    """正文没有"三、其他"时，按摘录位置把条款号纠正为真实章节。"""
    out = annotate_open_ended_risks([_bogus_ref_risk()], _OCR_SECTION_TEXT)
    assert out[0].clause_ref == "二、其他"


def test_clause_ref_kept_when_present_in_text() -> None:
    """条款号能在正文里原样找到 → 一个字都不动（避免把正常定位改坏）。"""
    risk = _bogus_ref_risk().model_copy(update={"clause_ref": "二、其他"})
    out = annotate_open_ended_risks([risk], _OCR_SECTION_TEXT)
    assert out[0].clause_ref == "二、其他"


def test_clause_ref_kept_when_quote_not_locatable() -> None:
    """摘录在正文里也找不到 → 保留模型给的描述性定位，不武断清空（如"首部"）。"""
    risk = _bogus_ref_risk().model_copy(
        update={"clause_ref": "首部", "evidence": "正文里没有这句话"}
    )
    out = annotate_open_ended_risks([risk], _OCR_SECTION_TEXT)
    assert out[0].clause_ref == "首部"


def test_page_marker_no_longer_eats_rule_window() -> None:
    """"签字…生效"之间隔着 OCR 页标记会落出 40 字窗口 → 缺生效日误留高风险。

    规则/定位都先去掉页标记再判，这条断言的是"去掉之后能正确降级"。
    """
    raw = (
        "原合同其余条款继续有效。\n"
        "3、本协议自双方法定代表人或授权代表签字并分别加盖各自单位公章专用章并加盖骑缝章确认无误后\n"
        "--- 第 3 页 ---\n位公章之日起生效。\n"
    )
    # 先确认用例真的踩到坑：带页标记时窗口不够、匹配不到（不这样就等于没测到修复点）
    assert SIGNING_EFFECT_RE.search(raw) is None
    risks = [
        RiskItem(
            risk_type="missing_required_field",
            label="缺失必填字段",
            severity=Severity.high,
            field="effective_date",
            evidence="",
            suggestion="缺失必填字段「生效日期」，请人工确认或补全后再审。",
        )
    ]
    assert annotate_open_ended_risks(risks, raw)[0].severity == Severity.medium


def test_locator_quote_drops_page_marker() -> None:
    """定位摘录里不能再出现页标记，且被页标记切开的词要拼回来（"各自单"+"位公章"）。"""
    risks = [
        RiskItem(
            risk_type="missing_required_field",
            label="缺失必填字段",
            severity=Severity.medium,
            field="effective_date",
            evidence="",
            suggestion="",
        )
    ]
    out = annotate_open_ended_risks(risks, "原合同其余条款继续有效。\n" + _OCR_SECTION_TEXT)[0]
    assert "---" not in out.evidence
    assert "各自单位公章之日起生效" in out.evidence


# ---- 补充协议前置门 / 缺失类不再拿文档开头凑定位 / 生效日推断口径统一 ----

# 补充协议形态：标题在开头 + 声明"原合同其余部分继续有效"
_SUPPLEMENT_TEXT = (
    "2025年城区绿地养护项目《园林绿化养护合同》之补充协议\n"
    "甲方（发包人）：某绿化服务中心　乙方（承包人）：某园林公司\n"
    "一、原合同变更内容：合同价款调整为 1,732,552.94 元。\n"
    "二、其他：本补充协议生效后即成为原合同不可分割的组成部分。"
    "除本补充协议中明确所作修改的内容之外，原合同的其余部分应完全继续有效。\n"
)


def test_supplementary_agreement_skips_completeness_rules() -> None:
    """补充/变更协议只查"写得对不对"，不再按完整合同逐条提示缺条款。"""
    # 底稿是完整合同该有的样子：故意抽掉验收/发票/担保/转包，触发条款完备性规则
    text = _SUPPLEMENT_TEXT + "三、本协议自双方签字盖章之日起生效。\n"
    assert text_rules(text, "enterprise_goods") == []


def test_full_contract_mentioning_supplement_still_checked() -> None:
    """对照组：完整合同正文里提一句"未尽事宜可另签补充协议"，不能因此跳过检查。"""
    text = (
        "采购合同\n甲方：某采购方　乙方：某供应商\n"
        "第一条 合同总价款为人民币 1,000,000 元，付款后 10 日内交付。\n"
        "第二条 本合同未尽事宜，双方可另行签订补充协议约定。\n"
    )
    types = {r.risk_type for r in text_rules(text, "enterprise_goods")}
    assert "acceptance_unclear" in types  # 缺验收安排照常提示


def test_supplementary_agreement_drops_inherited_missing_fields() -> None:
    """补充协议里"期限/币种"由原合同继承，缺了不是缺陷；金额仍要查。"""
    risks = [
        RiskItem(
            risk_type="missing_required_field",
            label="缺失必填字段",
            severity=Severity.medium,
            field="expiry_date",
            evidence="",
            suggestion="缺失必填字段「到期日」。",
        ),
        RiskItem(
            risk_type="missing_required_field",
            label="缺失必填字段",
            severity=Severity.medium,
            field="total_amount",
            evidence="",
            suggestion="缺失必填字段「合同总额」。",
        ),
    ]
    out = annotate_open_ended_risks(risks, _SUPPLEMENT_TEXT)
    assert [r.field for r in out] == ["total_amount"]


def test_missing_type_rules_do_not_fake_locator() -> None:
    """全篇找不到的规则（缺验收/未限制转包）不能拿文档开头当"原文定位"。"""
    text = "供货合同\n甲方：某采购方　乙方：某供应商\n第一条 合同总价款为人民币 1,000,000 元。\n"
    risks = {r.risk_type: r for r in text_rules(text, "enterprise_goods")}
    assert risks["acceptance_unclear"].evidence == ""
    assert risks["subcontract_unrestricted"].evidence == ""


def test_effective_date_inferred_across_page_marker() -> None:
    """"签字…生效"中间夹着 OCR 页标记时也要能推断生效日（此前窗口 20 字 + 不清页标记 → 漏）。"""
    model = ContractModel(signature_date=date(2025, 9, 7))
    text = (
        "二、其他\n"
        "3、本协议自双方法定代表人或授权代表签字并分别加盖各自单\n"
        "--- 第 3 页 ---\n"
        "位公章之日起生效。\n"
    )
    assert infer_effective_from_signature(model, text).effective_date == date(2025, 9, 7)


def test_effective_date_not_inferred_without_signing_clause() -> None:
    """对照组：正文没有"签字…生效"句式（如"合同经批准后生效"）→ 不推断，宁缺毋滥。"""
    model = ContractModel(signature_date=date(2025, 9, 7))
    text = "第五条 本合同经双方上级主管部门批准后生效。\n"
    assert infer_effective_from_signature(model, text).effective_date is None
