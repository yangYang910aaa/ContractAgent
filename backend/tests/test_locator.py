"""定位模块单测：硬换行接回、条款范围划定、条款号回推（离线纯函数）。"""

from __future__ import annotations

from backend.app.rules.locator import (
    _clause_ref_at,
    _clean_rule_text,
    _clause_spans,
    _locate_missing_field,
    _unwrap_hard_wraps,
)


def test_unwrap_hard_wraps_joins_split_words() -> None:
    """PDF 在词中间硬换行 → 接回；换行两侧是西文时补空格，不粘连成新词。"""
    text = "乙方不\n得将本合同项下义务转包。\nParty B shall not\nsubcontract its rights."
    joined = _unwrap_hard_wraps(text)
    assert "乙方不得将本合同项下义务转包。" in joined
    assert "not subcontract" in joined


def test_unwrap_hard_wraps_keeps_clause_headers_on_own_line() -> None:
    """条款标题必须留在行首：条款号回推与摘录定位都靠"行首是标题"这一条。"""
    text = "　　第五条   货款的结算\n　　在签订本合同之日付款。\n　　第六条 验收"
    joined = _unwrap_hard_wraps(text)
    lines = [line for line in joined.split("\n") if line.strip()]
    assert lines[0].strip().startswith("第五条")
    assert any(line.strip().startswith("第六条") for line in lines)


def test_clean_rule_text_drops_page_marks_before_joining() -> None:
    """页标记先删再接折行：标记夹在词中间时，接回后词要复原。"""
    text = "合同价款--- 第 3 页 ---为人民\n币 1000 元。"
    cleaned = _clean_rule_text(text)
    assert "人民币 1000 元" in cleaned
    assert "第 3 页" not in cleaned


def test_locate_missing_field_stays_inside_indented_clause() -> None:
    """后一条款标题带前导空白时，范围仍要收在它前面（真实购销合同形态）。

    起点判定容忍行首空白、终点判定不容忍，范围会一路划到文末，
    泛词锚点就会抓到后面毫不相干的条款。
    """
    text = (
        "　　第五条   货款的结算\n"
        "　　在签订本合同之日，甲方应当支付货款 30 万元。\n"
        "　  第六条 验收时间、方法及异议期限\n"
        "　　乙方逾期交货的，应比照中国人民银行有关延期付款的规定偿付违约金。\n"
    )
    spans = _clause_spans(text, "第五条")
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:].startswith("第五条")
    # 范围收在下一条款行首之前（不是划到文末），条文内容里不该混进第六条
    assert text[end:].lstrip().startswith("第六条")
    assert "逾期交货" not in text[start:end]
    ref, quote = _locate_missing_field(text, "payment_schedule", "第五条")
    assert "货款" in quote
    assert "逾期交货" not in quote
    assert ref.startswith("第五条")


def test_clause_ref_at_returns_full_header_when_anchor_inside_header() -> None:
    """锚点词落在条款标题行里时，回指完整标题而不是被截断的半行。"""
    text = "第五条   货款的结算\n在签订本合同之日付款。"
    assert _clause_ref_at(text, text.index("结算")) == "第五条   货款的结算"
