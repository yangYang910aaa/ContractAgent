"""P-15 过度免责规则单测（离线，无 API）。

覆盖三类形态（概括免责 / 数据安全责任免除 / 买方自担使用风险）、正当免责护栏、
主体方向性判定、以及"现有语料零新增命中"这条批4 的硬口径。
"""

from __future__ import annotations

from pathlib import Path

from backend.eval.run_eval import SAMPLES_DIR
from backend.app.parser import extract_text
from backend.app.rules import text_rules
from backend.app.rules.text_terms import _check_unfair_exemption

# 企业合同底稿：验收/发票/担保/转包/保密/违约金/法律条款齐全，只替换免责句，
# 这样断言不会被其它规则干扰（底稿本身应零风险）
_BASE = (
    "甲方：某采购方　乙方：某供应商\n"
    "第一条 合同总价款为人民币 1,000,000 元。\n"
    "第二条 乙方应于生效后 45 日内交付，甲方应在收货后 10 个工作日内组织验收，"
    "验收标准以双方确认的技术规范为准。\n"
    "第三条 乙方应在本合同签订后 10 日内向甲方提供合同总价款 10% 的银行保函作为履约担保。\n"
    "第四条 付款方式：合同签订后 10 日内支付预付款 20%，验收合格后支付剩余 80%；"
    "乙方应在收款前向甲方开具增值税专用发票。\n"
    "第五条 乙方逾期交付的，每逾期一日按合同总价款的 0.05% 向甲方支付违约金；"
    "除违约金外，乙方对甲方承担的赔偿责任总额以合同总价款的 100% 为上限。\n"
    "第六条 双方对因履行本合同而知悉的对方商业秘密负有保密义务；除法律法规要求、"
    "监管或司法机关要求披露，以及已公开信息、经对方书面同意外，不得向第三方披露。"
    "保密期限自本合同终止之日起 24 个月。\n"
    "第七条 乙方未经甲方书面同意不得将本项目转包或转委托给第三方。\n"
    "第八条 {exemption}\n"
    "第九条 本合同适用中华人民共和国法律，争议提交甲方所在地人民法院诉讼解决。\n"
)


def _risks(exemption: str) -> list:
    """在底稿里替换免责句后跑过度免责规则，返回命中列表。"""
    return _check_unfair_exemption(_BASE.format(exemption=exemption))


def _types(text: str, kind: str = "enterprise_goods") -> dict[str, str]:
    """跑整套文本规则 → {risk_type: severity}，用于确认底稿本身零命中。"""
    return {r.risk_type: r.severity.value for r in text_rules(text, kind)}


def test_clean_contract_has_no_exemption_risk() -> None:
    """底稿（免责句为正当约定）不该出现 P-15 风险，其它规则也不该误报。"""
    text = _BASE.format(exemption="因不可抗力导致合同不能履行的，双方互不承担违约责任。")
    assert "unfair_exemption_clause" not in _types(text)


def test_supplier_blanket_exemption_is_medium() -> None:
    risks = _risks("乙方对因本合同产生的任何损失概不负责。")
    assert [(r.risk_type, r.severity.value, r.policy_ref) for r in risks] == [
        ("unfair_exemption_clause", "medium", "P-15")
    ]


def test_data_liability_exemption_is_high() -> None:
    """数据/个人信息安全责任免除是本批唯一闸口点。"""
    risks = _risks("乙方对数据泄露、个人信息被非法使用不承担赔偿责任。")
    assert [(r.risk_type, r.severity.value) for r in risks] == [
        ("unfair_exemption_clause", "high")
    ]


def test_buyer_bears_usage_risk_is_medium() -> None:
    risks = _risks("甲方自行承担使用期间的漏洞攻击风险。")
    assert [(r.risk_type, r.severity.value) for r in risks] == [
        ("unfair_exemption_clause", "medium")
    ]


def test_blanket_overlapping_data_exemption_reports_once() -> None:
    """同一句里既是概括免责又指向数据责任 → 只报更重的那条，不重复出卡。"""
    risks = _risks("乙方对数据泄露造成的任何损失不承担任何责任。")
    assert len(risks) == 1
    assert risks[0].severity.value == "high"


def test_justified_exemptions_are_not_reported() -> None:
    """正当免责不报：不可抗力、法定免除、对方违约在先、迟延履行不免除。"""
    for sentence in (
        "因不可抗力造成的设备损毁，乙方不承担赔偿责任。",
        "依照法律规定可以免除责任的，双方互不追究。",
        "由于甲方的责任而造成服务延误的，乙方不承担违约责任。",
        "当事人迟延履行后发生不可抗力的，不免除其违约责任。",
    ):
        assert _risks(sentence) == [], sentence


def test_buyer_side_exemptions_are_not_reported() -> None:
    """方向性：买方侧权利与买方主体免责不报（真实合同里最常见的形态）。"""
    for sentence in (
        "甲方有权全部或部分退货，并不承担相应费用。",
        "供方违规的，需方不承担任何责任。",
    ):
        assert _risks(sentence) == [], sentence


def test_corpus_has_no_new_hits_except_sample_25() -> None:
    """批4 硬口径：现有合成语料里除新造的 sample_25 外，零新增命中。"""
    hits: dict[str, list[str]] = {}
    for path in sorted(SAMPLES_DIR.glob("sample_*.md")):
        risks = [r for r in _check_unfair_exemption(extract_text(path))]
        if risks:
            hits[path.name] = [r.severity.value for r in risks]
    assert list(hits) == ["sample_25_运维数据服务合同_过度免责.md"]
    assert sorted(hits["sample_25_运维数据服务合同_过度免责.md"]) == ["high", "medium"]


def test_real_contracts_have_no_hits() -> None:
    """真实合同是防误报正例：扫描件用已有 OCR 文本，电子版走解析。"""
    material = Path(__file__).resolve().parents[2] / "data" / "素材"
    scanned = sorted((material / "ocr" / "text").glob("*.txt"))
    assert scanned, "缺少扫描件 OCR 文本，检查素材目录"
    for path in scanned:
        assert _check_unfair_exemption(path.read_text(encoding="utf-8")) == [], path.name
