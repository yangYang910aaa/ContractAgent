"""关键字段双读单测：二次抽取比对、不一致标人工、第二读失败不阻断。"""

from backend.app.extractor import (
    _second_read_scope,
    _field_equal,
    _merge_double_read,
    extract_contract,
)
from backend.app.schemas import ContractModel, Evidence, PaymentTerm


def _model_with_terms(amounts_percents: list[tuple[str, float | None]]) -> ContractModel:
    """按 (金额串, 比例) 构造带 payment_schedule 的 ContractModel（金额走 Decimal 归一）。"""
    from decimal import Decimal

    terms = [
        PaymentTerm(name=f"期{i + 1}", amount=Decimal(a), percent=p)
        for i, (a, p) in enumerate(amounts_percents)
    ]
    return ContractModel(payment_schedule=terms)


def test_terms_signature_ignores_name_but_not_order() -> None:
    """期次判同看 (金额,比例) 且顺序敏感（与金额规则同口径）。"""
    a = _model_with_terms([("200000", 20.0), ("800000", 80.0)])
    b = _model_with_terms([("200000", 20.0), ("800000", 80.0)])
    assert _field_equal("payment_schedule", a.payment_schedule, b.payment_schedule)
    # 比例漂移（20% 写成 0.2 之类）→ 不一致
    c = _model_with_terms([("200000", 0.2), ("800000", 80.0)])
    assert not _field_equal("payment_schedule", a.payment_schedule, c.payment_schedule)
    # 顺序颠倒 → 不一致
    d = _model_with_terms([("800000", 80.0), ("200000", 20.0)])
    assert not _field_equal("payment_schedule", a.payment_schedule, d.payment_schedule)


def test_merge_same_second_read_keeps_first_without_flag() -> None:
    """两读一致 → 用首读，不标人工。"""
    first = _model_with_terms([("200000", 20.0), ("800000", 80.0)])
    second = _model_with_terms([("200000", 20.0), ("800000", 80.0)])
    merged = _merge_double_read(first, second)
    assert merged.payment_schedule == first.payment_schedule
    assert "payment_schedule" not in merged.extraction_meta


def test_merge_conflict_flags_human_review_and_keeps_first() -> None:
    """两读不一致 → 保留首读并标 needs_human_review（不静默采用可能错的一读）。"""
    first = _model_with_terms([("200000", 20.0), ("800000", 80.0)])
    second = _model_with_terms([("200000", 20.0), ("800000", 85.0)])
    merged = _merge_double_read(first, second)
    assert merged.payment_schedule == first.payment_schedule  # 保留首读
    assert merged.extraction_meta["payment_schedule"].needs_human_review is True


def test_merge_both_empty_is_consistent() -> None:
    """两读都空期次（正文一次性付清/无分期）→ 一致，不脑补不标人工。"""
    first = _model_with_terms([])
    second = _model_with_terms([])
    merged = _merge_double_read(first, second)
    assert merged.payment_schedule == []
    assert "payment_schedule" not in merged.extraction_meta


def test_merge_keeps_existing_review_flag() -> None:
    """已有人工标记的证据在冲突时保留标记（不覆盖为 False）。"""
    first = _model_with_terms([("200000", 20.0)])
    first.extraction_meta["payment_schedule"] = Evidence(
        quote="原文", clause_ref="第二条", confidence=0.6, needs_human_review=True
    )
    second = _model_with_terms([("300000", 30.0)])
    merged = _merge_double_read(first, second)
    assert merged.extraction_meta["payment_schedule"].needs_human_review is True


class _FakeStructured:
    """假 with_structured_output：按预置结果序列依次返回（dict 形态，模拟两次调用）。"""

    def __init__(self, results, fail_on: int | None = None) -> None:
        self._results = list(results)
        self._fail_on = fail_on
        self.calls = 0

    def with_structured_output(self, schema, method: str = "json_mode"):
        return self

    def invoke(self, messages) -> dict:
        self.calls += 1
        # 这种情况是：预置在第 N 次抛异常（模拟第二读限流/超时）
        if self._fail_on == self.calls:
            raise RuntimeError("429 rate limit")
        return self._results[self.calls - 1]


def _raw_payment(terms: list[tuple[str, str, int]]) -> dict:
    """构造 ExtractionSchema 形态原始输出（payment_schedule 用 PaymentRaw 结构）。"""
    return {
        "contract_kind": "enterprise_goods",
        "payment_schedule": [
            {"name": name, "amount": amount, "percent": percent}
            for name, amount, percent in terms
        ],
    }


class _RecordingStructured(_FakeStructured):
    """假模型：额外记录每次调用收到的正文，用于验证"第二读只喂付款片段"。"""

    def __init__(self, results) -> None:
        super().__init__(results)
        self.human_texts: list[str] = []

    def invoke(self, messages) -> dict:
        self.human_texts.append(messages[-1][1])
        return super().invoke(messages)


# 一份有条款结构、且付款条款与其它条款混在一起的小合同
_PAYMENT_TEXT = (
    "第一条 甲方为某采购方，乙方为某供应商。\n"
    "第二条 交货地点为北京市通州区。\n"
    "第三条 质保期为 24 个月。\n"
    "第九条 付款方式：合同签订后 30 个工作日内支付合同总价的 60%。\n"
)


def test_second_read_only_gets_payment_excerpt() -> None:
    """第二读只喂写付款安排的条款：它的作用是复核期次，重喂整份合同纯属重复 prefill。"""
    fake = _RecordingStructured(
        [_raw_payment([("预付款", None, 60)]), _raw_payment([("预付款", None, 60)])]
    )
    extract_contract(llm=fake, text=_PAYMENT_TEXT, double_read_fields=("payment_schedule",))
    assert len(fake.human_texts) == 2
    assert fake.human_texts[0] == _PAYMENT_TEXT  # 首读仍然吃全文
    assert "付款方式" in fake.human_texts[1]
    assert "质保期" not in fake.human_texts[1]  # 与付款无关的条款不进第二读


def test_second_read_keeps_full_text_without_clause_structure() -> None:
    """正文没有条款结构（挑不出付款片段）→ 退回全文，与改动前行为一致。"""
    fake = _RecordingStructured([_raw_payment([("预付款", "93000", 60)])] * 2)
    extract_contract(llm=fake, text="合同正文", double_read_fields=("payment_schedule",))
    assert fake.human_texts[1] == "合同正文"


def test_second_read_scope_falls_back_when_values_are_outside_excerpt() -> None:
    """首读抽到的比例在片段里找不到 → 退回全文，不拿残缺上下文去比对期次。"""
    first = _model_with_terms([("123456", 80.0)])  # 80% 与 123456 都不在付款条款里
    assert _second_read_scope(first, _PAYMENT_TEXT) == _PAYMENT_TEXT


def test_second_read_scope_uses_excerpt_when_evidence_is_covered() -> None:
    """首读的期次数值落在片段里 → 第二读只喂该片段。"""
    first = ContractModel(payment_schedule=[PaymentTerm(name="预付款", percent=60.0)])
    scope = _second_read_scope(first, _PAYMENT_TEXT)
    assert "付款方式" in scope
    assert "质保期" not in scope


def test_extract_contract_double_read_takes_majority_shape() -> None:
    """集成：两次抽取返回不同期次比例 → 保留首读并标人工。"""
    fake = _FakeStructured(
        [
            _raw_payment([("预付款", "200,000", 20), ("验收后", "800,000", 80)]),
            _raw_payment([("预付款", "200,000", 20), ("验收后", "800,000", 85)]),
        ]
    )
    model = extract_contract(llm=fake, text="合同正文", double_read_fields=("payment_schedule",))
    assert fake.calls == 2
    assert model.payment_schedule[0].amount == 200000
    assert model.payment_schedule[1].percent == 80.0  # 首读保留
    assert model.extraction_meta["payment_schedule"].needs_human_review is True


def test_extract_contract_double_read_off_single_call() -> None:
    """double_read_fields=() → 只抽一次（向后兼容/评测对照用）。"""
    fake = _FakeStructured([_raw_payment([("预付款", "200,000", 20)])])
    model = extract_contract(llm=fake, text="合同正文", double_read_fields=())
    assert fake.calls == 1
    assert model.payment_schedule[0].percent == 20.0


def test_extract_contract_second_read_failure_falls_back() -> None:
    """第二读抛异常 → 以首读为准返回，不整份失败。"""
    fake = _FakeStructured(
        [_raw_payment([("预付款", "200,000", 20)])], fail_on=2
    )
    model = extract_contract(llm=fake, text="合同正文", double_read_fields=("payment_schedule",))
    assert model.payment_schedule[0].amount == 200000
