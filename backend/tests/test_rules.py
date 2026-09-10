"""rules 单测：确定性规则引擎对正常/缺陷样本的命中与评级(TDD)。"""

from datetime import date
from decimal import Decimal

from backend.app.rules import annotate_template_risks, evaluate, grade_report
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


def test_risks_carry_chinese_label_and_field_name() -> None:
    """展示层友好：风险带中文 label，缺必填建议文案里的字段名不再露英文 key。"""
    model = _with(effective_date=None, expiry_date=None, total_amount=None)
    missing = [r for r in evaluate(model) if r.risk_type == "missing_required_field"]
    assert len(missing) == 3
    # label 是中文展示名，不是机器码
    assert all(r.label == "缺失必填字段" for r in missing)
    # 建议文案字段名已本地化（这句是给人看的）
    sug = next(r for r in missing if r.field == "effective_date").suggestion
    assert "生效日期" in sug
    assert "effective_date" not in sug


def test_blank_template_text_downgrades_missing_and_adds_notice() -> None:
    """空白模板（多类占位）→ 缺必填 high 降 medium + 追加"疑似空白模板"风险。"""
    from backend.app.rules import is_blank_template_suspect

    blank_text = (
        "甲方（采购方）：＿＿＿＿＿＿\n"
        "乙方（供应商）：＿＿＿＿＿＿\n"
        "签订时间： 年 月 日\n"
        "货款金额为： 元（大写：＿＿＿）"
    )
    assert is_blank_template_suspect(blank_text) is True
    model = _with(effective_date=None, expiry_date=None, total_amount=None)
    risks = annotate_template_risks(evaluate(model), blank_text)
    missing = [r for r in risks if r.risk_type == "missing_required_field"]
    # 三条缺必填全部降为 medium（不再触发闸口）
    assert missing and all(r.severity == Severity.medium for r in missing)
    # 追加模板提示：medium、无政策引用、建议说清"疑似模板"
    notice = [r for r in risks if r.risk_type == "blank_template_suspected"]
    assert len(notice) == 1
    assert notice[0].severity == Severity.medium
    assert notice[0].label == "疑似空白模板"
    assert "空白模板" in notice[0].suggestion
    assert grade_report(risks) == Grade.conditional_pass  # 无 high → 不再 fail/gate
    # 官方示范文本的另两类占位写法也要命中：GF 式"点线 + □ 选框"、
    # 科技部/校服式"冒号后纯空格填空栏"（不一定画下划线，2026-09-07 修复）
    gf_style = (
        "甲方（出卖人）:………… … …\n"
        "联系电话 :……………………………… … …\n"
        "证件类型 : 身份证□\u3000居住证□\u3000护照□\n"
        "出生年月日（注册登记日期）:\n"
    )
    assert is_blank_template_suspect(gf_style) is True
    space_fill_style = (
        "甲方（采购方）：              \n"
        "项目名称：                            \n"
        "有效期限：    年  月  日至    年  月   日\n"
    )
    assert is_blank_template_suspect(space_fill_style) is True
    gf_risks = annotate_template_risks(evaluate(model), gf_style)
    gf_missing = [r for r in gf_risks if r.risk_type == "missing_required_field"]
    assert gf_missing and all(r.severity == Severity.medium for r in gf_missing)
    assert any(r.risk_type == "blank_template_suspected" for r in gf_risks)
    # 填空式条款模板（霸王花式）：占位是"空标点/空单位"而非下划线/长空白
    fill_blank_style = (
        "2、农药残留不超标，标准是 ；\n"
        "3、不含沙石、草根、杂草等杂质；\n"
        "4、其它要求 。\n"
        "（二）验收标准、方法 。\n"
        "（一）包装方式和要求： 。\n"
        "（二）包装物由 方提供，费用由 方承担。\n"
        "（一）交付方式按下列第 项办理：\n"
        "甲方应于每批交付之日起 日内结清该批货款。\n"
        "甲方支付乙方定金 元；交付后按约定价格的 % 计算违约金。\n"
    )
    assert is_blank_template_suspect(fill_blank_style) is True
    fill_risks = annotate_template_risks(evaluate(model), fill_blank_style)
    fill_missing = [r for r in fill_risks if r.risk_type == "missing_required_field"]
    assert fill_missing and all(r.severity == Severity.medium for r in fill_missing)
    assert any(r.risk_type == "blank_template_suspected" for r in fill_risks)


def test_filled_text_keeps_missing_as_high() -> None:
    """填写完整的正文（即使字段仍缺）不该被当成模板降级——防止误放行。"""
    filled_text = (
        "甲方（采购方）：晨光实验中学\n"
        "乙方（供应商）：星海校服服饰有限公司\n"
        "签订时间：2026年3月10日\n"
        "本合同总价为人民币（大写）壹拾玖万捌仟肆佰元整（小写：198,400 元）"
    )
    model = _with(effective_date=None, expiry_date=None, total_amount=None)
    risks = annotate_template_risks(evaluate(model), filled_text)
    missing = [r for r in risks if r.risk_type == "missing_required_field"]
    assert missing and all(r.severity == Severity.high for r in missing)
    assert not any(r.risk_type == "blank_template_suspected" for r in risks)


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


# ---- 生效日兜底推断(签字盖章生效句式) ----


def _with_sig(effective=None, signature=None, text=""):
    """构造只关注生效/签署日的 ContractModel（其余字段默认空）。"""
    return ContractModel(
        signature_date=signature,
        effective_date=effective,
    ), text


def test_infer_effective_from_signature_fills_when_wording_matches() -> None:
    """正文写"自双方签字盖章之日起生效"且签署日已有 → 生效日回填为签署日。"""
    from datetime import date
    from backend.app.rules import infer_effective_from_signature
    model, text = _with_sig(
        effective=None,
        signature=date(2026, 3, 10),
        text="甲方（采购方）：某校。……合同自双方签字盖章之日起生效。",
    )
    out = infer_effective_from_signature(model, text)
    assert out.effective_date == date(2026, 3, 10)


def test_infer_effective_covers_signature_and_party_variants() -> None:
    """"签名（盖章）之日"与"经签约各方签字盖章后生效"两种真实措辞同样回填。"""
    from datetime import date
    from backend.app.rules import infer_effective_from_signature
    variants = [
        "本合同自甲、乙双方签名（盖章）之日起成立并生效。",
        "本合同经签约各方签字盖章后生效。",
        "合同自双方签字盖章之日起生效。",
    ]
    for sentence in variants:
        model = ContractModel(signature_date=date(2026, 9, 1), effective_date=None)
        out = infer_effective_from_signature(model, sentence)
        assert out.effective_date == date(2026, 9, 1), sentence


def test_infer_effective_keeps_explicit_effective_date() -> None:
    """生效日已显式抽到 → 不被覆盖（即使正文同时有"签字盖章生效"句）。"""
    from datetime import date
    from backend.app.rules import infer_effective_from_signature
    model, text = _with_sig(
        effective=date(2026, 4, 1),
        signature=date(2026, 3, 10),
        text="本合同自双方签字盖章之日起生效。",
    )
    out = infer_effective_from_signature(model, text)
    assert out.effective_date == date(2026, 4, 1)


def test_infer_effective_noop_without_signature_or_wording() -> None:
    """签署日缺失，或正文没有"签字盖章生效"句式 → 不推断（宁缺毋滥）。"""
    from datetime import date
    from backend.app.rules import infer_effective_from_signature
    # 情况 1：没有签署日
    model, text = _with_sig(effective=None, signature=None,
                            text="本合同自双方签字盖章之日起生效。")
    assert infer_effective_from_signature(model, text).effective_date is None
    # 情况 2：句式不匹配（如"经批准之日起生效"）
    model2, text2 = _with_sig(effective=None, signature=date(2026, 3, 10),
                              text="本合同自上级批准之日起生效。")
    assert infer_effective_from_signature(model2, text2).effective_date is None


def test_tech_service_liability_cap_30_is_ok_but_29_low() -> None:
    """P-03 技术类底线 30%（2026-09-07 B 口径）：cap=30 不判 too_low（科技部示范
    文本即 30%），低于 30% 仍判 high。"""
    model = _with(contract_kind="tech_service", liability_cap=30.0)
    assert "liability_cap_too_low" not in _risk_types(model)
    low = _with(contract_kind="tech_service", liability_cap=29.0)
    assert "liability_cap_too_low" in _risk_types(low)


def test_enterprise_liability_cap_30_still_low() -> None:
    """货物/服务采购底线仍是 50%：cap=30 对企业类仍判 high，不因技术类放宽误伤。"""
    model = _with(liability_cap=30.0)  # contract_kind=None → 按 enterprise 处理
    types = _risk_types(model)
    assert "liability_cap_too_low" in types
    assert all(r.severity == Severity.high
               for r in evaluate(model) if r.risk_type == "liability_cap_too_low")


def test_amount_mismatch_with_unreliable_total_is_medium() -> None:
    """金额不一致但总额低置信度/证据为空栏 → medium 待人工核对（tech_03 幻觉场景），
    不误停闸口；若总额可信则仍 high（test_sample03 覆盖可信路径）。"""
    from backend.app.schemas import Evidence

    meta = {
        "total_amount": Evidence(
            quote="合同金额为（大写）：人民币　　（￥　　　　元）。",
            clause_ref="第十四条",
            confidence=0.6,
        )
    }
    model = _with(
        extraction_meta=meta,
        payment_schedule=[
            # 期次名不含预付类关键词：本用例只验"金额不一致降级"，避免叠加 P-01
            _term("第一期", "1679900", 50.0),
            _term("尾款", "335980", 10.0),
        ],
    )
    # 上面分项加总 201.6 万 ≠ 总额 100 万，但总额证据为空栏 + 低置信度 → 只降 medium
    amounts = [r for r in evaluate(model) if r.risk_type == "amount_inconsistency"]
    assert amounts and amounts[0].severity == Severity.medium
    assert grade_report(evaluate(model)) == Grade.conditional_pass


def test_penalty_occurrence_based_not_daily_no_high() -> None:
    """"每次违约按合同总价 X%"（非按日）不适用日费率畸高阈值（tech_01 现象）。"""
    from backend.app.schemas import Evidence

    meta = {
        "penalty_rate": Evidence(
            quote="每次违约，违约方需向守约方支付合同总价的10%",
            clause_ref="第十六条",
            confidence=0.9,
        )
    }
    model = _with(penalty_rate=10.0, extraction_meta=meta)
    assert "penalty_rate_too_high" not in _risk_types(model)


def test_penalty_daily_quote_still_high() -> None:
    """按日计收（每逾期一日 X%）仍走畸高阈值，防豁免把真缺陷放掉。"""
    from backend.app.schemas import Evidence

    meta = {
        "penalty_rate": Evidence(
            quote="每逾期一日按合同总价款的2%向甲方支付违约金",
            clause_ref="第十一条",
            confidence=0.9,
        )
    }
    model = _with(penalty_rate=2.0, extraction_meta=meta)
    assert "penalty_rate_too_high" in _risk_types(model)


def test_percent_misparsed_into_term_amount_downgrades_to_medium() -> None:
    """真实合同走查（2026-09-10）：只写比例时抽取把 70/30 填进金额字段，
    不得当成"期次加总 ≠ 总额"的 high 误停闸 → 降 medium 提示人工核对。"""
    model = _with(
        total_amount=Decimal("1600000"),
        # 期次名不含"预付"，避免叠加 P-01 预付款畸高 high 干扰本用例
        payment_schedule=[_term("第一期", "70", 70.0), _term("第二期", "30", 30.0)],
    )
    amounts = [r for r in evaluate(model) if r.risk_type == "amount_inconsistency"]
    assert amounts and amounts[0].severity == Severity.medium
    assert "疑似" in amounts[0].evidence
    assert grade_report(evaluate(model)) == Grade.conditional_pass


def test_plain_small_amount_without_percent_still_doubtful() -> None:
    """金额与比例不同值但量级不合常理（总额 480 万、期次 70 元）同样按疑似处理。"""
    model = _with(
        total_amount=Decimal("4815000"),
        payment_schedule=[_term("首付款", "70", None), _term("尾款", "30", None)],
    )
    amounts = [r for r in evaluate(model) if r.risk_type == "amount_inconsistency"]
    assert amounts and amounts[0].severity == Severity.medium


def test_normal_and_real_defect_amount_consistency_unchanged() -> None:
    """护栏不吞真缺陷：正常样本零风险；分项 110 万 ≠ 总额 100 万 仍判 high。"""
    assert "amount_inconsistency" not in _risk_types(_normal())
    model = _with(
        total_amount=Decimal("1000000"),
        payment_schedule=[
            _term("预付款", "200000", 20.0),
            _term("第二批", "500000", 50.0),
            _term("第三批", "400000", 40.0),
        ],
    )
    amounts = [r for r in evaluate(model) if r.risk_type == "amount_inconsistency"]
    assert amounts and amounts[0].severity == Severity.high


def test_first_payment_named_shoufu_counts_as_prepayment() -> None:
    """"首付款"按 P-01 第二条计入预付款（真实合同走查 2026-09-10 漏判修复）。"""
    model = _with(
        total_amount=Decimal("4815000"),
        payment_schedule=[_term("首付款", "3370500", 70.0), _term("尾款", "1444500", 30.0)],
    )
    prepay = [r for r in evaluate(model) if r.risk_type == "prepayment_ratio_high"]
    assert prepay and prepay[0].severity == Severity.high
    # 名称只有"进度款/验收款"的期次不能被当预付款
    model2 = _with(
        payment_schedule=[_term("进度款", "200000", 20.0), _term("验收款", "800000", 80.0)],
    )
    assert "prepayment_ratio_high" not in _risk_types(model2)


# ---- 开放式条款语境（真实合同走查 2026-09-10）----


def test_open_ended_amount_downgrades_missing_total_with_explicit_notice() -> None:
    """月结/按实结算合同无总额：缺必填 high → medium，且建议里显式写明"已降为提示级"。"""
    from backend.app.rules import annotate_open_ended_risks

    model = _with(total_amount=None, effective_date=None, expiry_date=None)
    text = "双方每月结算一次，每月30日前结清当月货款。"
    risks = annotate_open_ended_risks(evaluate(model), text)
    total = next(r for r in risks if r.field == "total_amount")
    assert total.severity == Severity.medium
    assert "已降为提示级" in total.suggestion
    # 生效日：正文既无签署日期表述也无空白日期栏 → 仍是 high
    assert next(r for r in risks if r.field == "effective_date").severity == Severity.high
    # 到期日：正文无"至 YYYY 年"具体结束日期 → 按开放式期限降 medium
    assert next(r for r in risks if r.field == "expiry_date").severity == Severity.medium


def test_open_ended_term_and_signing_downgrade_with_notice() -> None:
    """有效期"N 年"/签字盖章生效无日期：对应缺必填降 medium 并附明确提示。"""
    from backend.app.rules import annotate_open_ended_risks

    model = _with(effective_date=None, expiry_date=None)
    text = "本合同自双方签字盖章之日起生效，有效期暂定为4年。"
    risks = annotate_open_ended_risks(evaluate(model), text)
    by_field = {r.field: r for r in risks if r.risk_type == "missing_required_field"}
    assert by_field["effective_date"].severity == Severity.medium
    assert by_field["expiry_date"].severity == Severity.medium
    assert "已降为提示级" in by_field["effective_date"].suggestion
    assert "已降为提示级" in by_field["expiry_date"].suggestion


def test_open_ended_annotation_keeps_normal_missing_high() -> None:
    """普通合同（无按实结算/无固定期限/无签字盖章生效句式）的缺必填保持 high。"""
    from backend.app.rules import annotate_open_ended_risks

    model = _with(total_amount=None)
    risks = annotate_open_ended_risks(evaluate(model), "甲方应于2026年12月31日前交付货物。")
    assert next(r for r in risks if r.field == "total_amount").severity == Severity.high


def test_open_ended_order_based_total_medium() -> None:
    """按订单结算（无固定总额）→ 缺总额降 medium 并附提示。"""
    from backend.app.rules import annotate_open_ended_risks

    model = _with(total_amount=None)
    text = "二、订单要求：买方订单均以书面传真/邮件形式通知卖方，卖方收到订单后两个工作日内回复。"
    risks = annotate_open_ended_risks(evaluate(model), text)
    total = next(r for r in risks if r.field == "total_amount")
    assert total.severity == Severity.medium and "已降为提示级" in total.suggestion


def test_open_ended_effective_and_expiry_without_concrete_date() -> None:
    """生效日"自合同签订之日起"、到期日以验收为界（无"至 YYYY 年"）→ 均降提示级。"""
    from backend.app.rules import annotate_open_ended_risks

    model = _with(effective_date=None, expiry_date=None)
    text = "2.3 服务期限：自合同签订之日起至本项目验收合格之日止。"
    risks = annotate_open_ended_risks(evaluate(model), text)
    by_field = {r.field: r for r in risks if r.risk_type == "missing_required_field"}
    assert by_field["effective_date"].severity == Severity.medium
    assert by_field["expiry_date"].severity == Severity.medium


def test_missing_expiry_with_concrete_end_date_stays_high() -> None:
    """正文写了"至 2027 年"却抽不到到期日 → 保留 high（提示人工核对抽取）。"""
    from backend.app.rules import annotate_open_ended_risks

    model = _with(expiry_date=None)
    risks = annotate_open_ended_risks(evaluate(model), "本合同有效期至 2027 年 3 月 9 日。")
    assert next(r for r in risks if r.field == "expiry_date").severity == Severity.high


# ---- 空白模板检测精化（真实合同走查 2026-09-10）----


def test_filled_contract_with_signature_date_blank_not_template() -> None:
    """已签合同仅署名页/页脚日期空白 + 排版空格 → 不得判"疑似空白模板"。"""
    from backend.app.rules import is_blank_template_suspect

    signed = (
        "软件开发合同（2025 年升级改造）\n"
        "合同金额：人民币壹佰陆拾万元整（￥1600000 元整）。\n"
        "甲方：              乙方：\n"
        "权利义务由双方承担 。\n"
        "合同签订地点：\n   年  月   日\n第 14 页 共 14\n"
    )
    assert is_blank_template_suspect(signed) is False


def test_unfilled_gf_style_templates_still_detected() -> None:
    """未填写文本仍按原口径识别：点线+选框式、纯空格填空式（防误伤官方模板）。"""
    from backend.app.rules import is_blank_template_suspect

    gf_style = (
        "甲方（出卖人）:………… … …\n"
        "联系电话 :……………………………… … …\n"
        "证件类型 : 身份证□\u3000居住证□\u3000护照□\n"
    )
    space_fill_style = (
        "甲方（采购方）：              \n"
        "项目名称：                            \n"
        "有效期限：    年  月  日至    年  月   日\n"
    )
    assert is_blank_template_suspect(gf_style) is True
    assert is_blank_template_suspect(space_fill_style) is True
