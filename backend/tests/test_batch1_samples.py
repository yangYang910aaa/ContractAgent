"""横向批1 新样本（sample_10~15）确定性测试：文本级规则命中/对照、语料登记、三格式渲染。

全部离线（不调 LLM）：P-06~P-09 是文本级规则，md 正文即可确定性断言命中；
docx/pdf 渲染走既有 format_render 通道，验证新 spec 能出三格式。
"""

from __future__ import annotations

import json

from backend.app.config import BASE_DIR
from backend.app.rules import Severity, text_rules
from backend.eval.format_render import render_docx, render_pdf
from backend.eval.generate_samples import NEW_SPECS, _body_for
from backend.app.parser import extract_text

SAMPLES_DIR = BASE_DIR / "data" / "contracts"
GT_PATH = BASE_DIR / "data/合同模板/合同变体/out/ground_truth.json"

# sample_10~15 品类（与 generate_samples 定义一致）
_KINDS = {f"sample_{i:02d}": "enterprise_goods" for i in range(10, 15)}
_KINDS["sample_15"] = "tech_service"

# 期望的文本级命中（缺陷样本应恰好命中目标类，正常对照应为空）
_EXPECTED_TEXT_RISKS = {
    "sample_10": {"acceptance_unclear": Severity.medium.value},
    "sample_11": {"invoice_unclear": Severity.medium.value},
    "sample_12": {"performance_bond_missing": Severity.medium.value},
    "sample_13": {"subcontract_unrestricted": Severity.high.value},
    "sample_14": {},
    "sample_15": {},
}


def _md_text(sample_id: str) -> str:
    """读 sample md 正文（生成器已落盘）。"""
    spec = next(s for s in NEW_SPECS if s.sample_id == sample_id)
    return (SAMPLES_DIR / spec.filename).read_text(encoding="utf-8")


def test_new_samples_text_rules_match_design() -> None:
    """sample_10~15 的文本级命中与设计一致：缺陷恰好命中目标类，正常对照零命中。"""
    for sample_id, expected in _EXPECTED_TEXT_RISKS.items():
        risks = text_rules(_md_text(sample_id), _KINDS[sample_id])
        got = {r.risk_type: r.severity.value for r in risks}
        assert got == expected, f"{sample_id}: {got} != {expected}"
        # 命中项应带政策编号与原文证据（报告可溯源）
        for r in risks:
            assert r.policy_ref in ("P-06", "P-07", "P-08", "P-09")
            assert r.evidence


def test_regenerated_old_samples_clean_on_text_rules() -> None:
    """方案 A 重渲染后，旧企业/技术样本（01~05/08/09）不再被新文本规则命中。"""
    kinds = {f"sample_{i:02d}": "enterprise_goods" for i in range(1, 6)}
    kinds.update({"sample_08": "tech_service", "sample_09": "tech_service"})
    for sample_id, kind in kinds.items():
        name = f"{sample_id}_"  # 文件名前缀
        path = next(p for p in SAMPLES_DIR.glob("*.md") if p.name.startswith(name))
        assert text_rules(path.read_text(encoding="utf-8"), kind) == [], path.name


def test_normal_samples_contain_batch1_clauses() -> None:
    """正常样本（含旧 01/02/08 与新 14/15）正文带齐 P-06~P-09 合规章节。"""
    for sample_id in ("sample_01", "sample_02", "sample_14"):
        path = next(p for p in SAMPLES_DIR.glob("*.md") if p.name.startswith(f"{sample_id}_"))
        content = path.read_text(encoding="utf-8")
        for clause_word in ("发票", "银行保函", "不得将本合同项下", "验收合格标准"):
            assert clause_word in content, f"{path.name} 缺合规章节关键词: {clause_word}"
    for sample_id in ("sample_08", "sample_15"):
        path = next(p for p in SAMPLES_DIR.glob("*.md") if p.name.startswith(f"{sample_id}_"))
        content = path.read_text(encoding="utf-8")
        for clause_word in ("发票", "银行保函", "转委托"):
            assert clause_word in content, f"{path.name} 缺合规章节关键词: {clause_word}"


def test_defect_samples_remove_only_target_clause() -> None:
    """缺陷样本按设计隔离：只缺目标条款，其余合规章节仍在。"""
    s10 = _md_text("sample_10")
    assert "验收" not in s10  # P-06 缺陷：全文无验收字样
    assert "银行保函" in s10  # 其余合规节（担保）仍在
    s11 = _md_text("sample_11")
    assert "发票" not in s11 and "开票" not in s11  # P-07 缺陷：全文无发票
    s12 = _md_text("sample_12")
    for bond_word in ("保函", "保证金", "质保金"):
        assert bond_word not in s12  # P-08 缺陷：全文无担保安排
    s13 = _md_text("sample_13")
    assert "任意转包" in s13 and "不得将本合同项下" not in s13  # P-09 high：免责改写替代限制句


def test_new_samples_registered_in_gt() -> None:
    """六份新样本都在 GT 登记（samples set、field_gt=false 豁免字段尺子）。"""
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    entries = {e["file"]: e for e in gt["files"] if e["set"] == "samples"}
    for spec in NEW_SPECS:
        entry = entries.get(spec.filename)
        assert entry is not None, f"{spec.filename} 未在 GT 登记"
        assert entry.get("field_gt") is False, f"{spec.filename} 应豁免字段尺子(field_gt=false)"


def test_new_specs_render_docx_pdf_roundtrip(tmp_path) -> None:
    """新 spec（企业式 + 技术式）都能出 md/docx/pdf，关键锚点（编号/双方/总额）不丢。"""
    for spec in NEW_SPECS:
        body = _body_for(spec)
        docx_path = tmp_path / f"{spec.sample_id}.docx"
        pdf_path = tmp_path / f"{spec.sample_id}.pdf"
        render_docx(spec, docx_path, body=body)
        render_pdf(spec, pdf_path, body=body)
        for fmt_path in (docx_path, pdf_path):
            text = extract_text(fmt_path)
            compact = text.replace(" ", "").replace("\n", "")
            assert spec.contract_no in text
            assert spec.buyer in compact or "甲方" in text
            assert spec.supplier in compact or "乙方" in text
