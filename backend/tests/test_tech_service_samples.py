"""技术开发式（tech_service）样本测试：第X条正文/三格式渲染/规则期望（v3 扩品类）。"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backend.app.extractor import _parse_kind, build_contract_model
from backend.app.parser import extract_text, split_clauses
from backend.app.rules import evaluate, grade_report
from backend.app.schemas import ContractModel, Grade, PaymentTerm, Severity
from backend.eval.generate_samples import TECH_SPECS, _body_for, render_docx, render_pdf


def _tech_model(spec) -> ContractModel:
    """按 spec 期望值构造 ContractModel（等价真实 LLM 抽取 + 归一化结果）。"""
    return ContractModel(
        contract_kind="tech_service",
        buyer=spec.buyer,
        supplier=spec.supplier,
        signature_date=date(2026, 4, 15),
        effective_date=date(2026, 4, 15),
        expiry_date=date(2027, 10, 31),
        total_amount=Decimal("1200000"),
        currency="人民币",
        payment_schedule=[
            PaymentTerm(name="首期款", amount=Decimal("360000"), percent=30.0),
            PaymentTerm(name="中期款", amount=Decimal("480000"), percent=40.0),
            PaymentTerm(name="验收尾款", amount=Decimal("360000"), percent=30.0),
        ],
        penalty_rate=0.05,
        liability_cap=100.0,
        warranty_months=12,
        confidentiality_months=spec.confidentiality_months,
        ip_ownership=(
            "本项目研究开发成果及其知识产权归乙方（受托方）所有，双方另有书面约定的除外。"
            if spec.ip_to_supplier
            else "乙方根据甲方需求定制开发的软件、源代码及相关文档的知识产权归甲方所有。"
        ),
        governing_law="中华人民共和国法律",
    )


def _risk_types(model: ContractModel) -> set[str]:
    return {r.risk_type for r in evaluate(model)}


def test_tech_kind_detected() -> None:
    """技术开发（委托）式标题/正文应被识别为 tech_service（关键词兜底）。"""
    assert _parse_kind("技术开发（委托）合同") == "tech_service"
    assert _parse_kind("技术开发合同") == "tech_service"
    cm = build_contract_model({"contract_kind": "技术开发（委托）合同"})
    assert cm.contract_kind == "tech_service"


def test_tech_md_splits_into_14_tiao() -> None:
    """技术开发正文应按「第X条」切成 14 条 + 前言，子条不误切。"""
    body = _body_for(TECH_SPECS[0])
    clauses = split_clauses(body)
    refs = [c.ref for c in clauses]
    assert refs[0] == ""
    assert refs[1] == "第一条" and refs[-1] == "第十四条"
    assert len(refs) == 15
    # 保密/权属/金额留在对应条款内
    conf = next(c for c in clauses if c.title.startswith("第四条"))
    assert "24 个月" in conf.text
    first = next(c for c in clauses if c.title.startswith("第一条"))
    assert "企业数据资产管理平台" in first.text


def test_normal_sample08_zero_risk_pass() -> None:
    """sample_08（IP 归甲方/保密 24 个月/责任上限 100%）应零风险 pass。"""
    spec = TECH_SPECS[0]
    assert spec.sample_id == "sample_08"
    risks = evaluate(_tech_model(spec))
    assert risks == []
    assert grade_report(risks) == Grade.pass_


def test_defect_sample09_hits_confidentiality_and_ip() -> None:
    """sample_09（保密 60 个月 + 成果 IP 归乙方）应 fail，命中 P-04 high 与 P-05 medium。"""
    spec = TECH_SPECS[1]
    assert spec.sample_id == "sample_09"
    risks = evaluate(_tech_model(spec))
    types = {r.risk_type for r in risks}
    assert types == {"confidentiality_too_long", "ip_ownership_unclear"}
    by_type = {r.risk_type: r for r in risks}
    assert by_type["confidentiality_too_long"].severity == Severity.high
    assert by_type["confidentiality_too_long"].policy_ref == "P-04"
    assert by_type["ip_ownership_unclear"].severity == Severity.medium
    assert by_type["ip_ownership_unclear"].policy_ref == "P-05"
    assert grade_report(risks) == Grade.fail


@pytest.mark.parametrize("idx", range(len(TECH_SPECS)))
def test_tech_specs_render_docx_pdf_roundtrip(tmp_path: Path, idx: int) -> None:
    """两版技术开发样本都能出 md/docx/pdf，关键锚点（编号/总价/保密期/权属句）不丢。"""
    spec = TECH_SPECS[idx]
    body = _body_for(spec)
    docx_path = tmp_path / f"{spec.sample_id}.docx"
    pdf_path = tmp_path / f"{spec.sample_id}.pdf"
    render_docx(spec, docx_path, body=body)
    render_pdf(spec, pdf_path, body=body)
    for fmt_path in (docx_path, pdf_path):
        text = extract_text(fmt_path)
        compact = text.replace(" ", "").replace("\n", "")
        assert spec.contract_no in text
        assert "第一条 项目名称与技术内容" in text
        # PDF 段落两端对齐会把金额/期限折行，折叠空白后仍应完整可读
        assert "1,200,000" in compact
        assert f"{spec.confidentiality_months}个月" in compact
    # 正常版 IP 归甲方；缺陷版 IP 归乙方（权属句随版本切换）
    if spec.ip_to_supplier:
        assert "知识产权归乙方（受托方）所有" in body
    else:
        assert "知识产权归甲方" in body


def test_tech_docx_clause_header_bold(tmp_path: Path) -> None:
    """技术开发 docx 的「第X条」条款头仍黑体加粗（复用第X条渲染路径）。"""
    from docx import Document

    spec = TECH_SPECS[0]
    path = tmp_path / "tiao.docx"
    render_docx(spec, path, body=_body_for(spec))
    doc = Document(path)
    head = next(p for p in doc.paragraphs if p.text.startswith("第一条 项目名称"))
    assert head.runs[0].font.bold is True
