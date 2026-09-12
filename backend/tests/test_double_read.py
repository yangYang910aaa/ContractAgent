"""关键字段双读单测：二次抽取比对、不一致标人工、第二读失败不阻断。"""

from backend.app.extractor import (
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
