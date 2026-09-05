"""Phase 0 冒烟测试：目录/政策文档/合成合同可读且生成可复现。"""

from pathlib import Path

from backend.app.config import BASE_DIR
from backend.app.parser import split_clauses
from backend.eval.generate_samples import OUTPUT_DIR, SPECS, render_contract


def test_policy_docs_exist_and_readable() -> None:
    policy_dir = BASE_DIR / "data" / "policies"
    files = sorted(policy_dir.glob("*.md"))
    assert len(files) >= 5, "政策文档应至少 5 条"
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "P-0" in text, f"{f.name} 缺少政策编号"
        assert len(text) > 80, f"{f.name} 内容过短"


def test_sample_contracts_generated() -> None:
    files = sorted(OUTPUT_DIR.glob("sample_*.md"))
    assert len(files) == 7, "应生成 7 份合成合同（企业 01~05 + 校服 06/07）"
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "甲方" in text and "乙方" in text
        # 正文结构：企业「第X条」式 / 校服章节式，两者都必须能切出条文块
        assert split_clauses(text), f"{f.name} 缺少可切分的条款/章节结构"


def test_generator_reproducible() -> None:
    """同一 spec 渲染两次结果一致（可复现，供评测基线用）。"""
    for spec in SPECS:
        assert render_contract(spec) == render_contract(spec)


def test_defect_samples_contain_expected_markers() -> None:
    """缺陷样本应包含对应的可见异常标记（后续 Phase 1 规则命中点）。"""
    texts = {s.sample_id: render_contract(s) for s in SPECS}
    assert "1.5%" in texts["sample_03"]  # 违约金率
    assert "5% 为上限" in texts["sample_03"]  # 责任上限
    assert "480,000" in texts["sample_04"]  # 预付款金额（60% 比例由 spec 数值化断言）
    assert "保密" not in texts["sample_04"]  # 缺保密条款
    assert "6 个月" in texts["sample_05"]  # 质保过短
    assert "60 个月" in texts["sample_05"]  # 保密期过长


def test_defect_specs_ratio_sanity() -> None:
    """从规格层断言缺陷比例数值（Phase 1 规则将用这些数值化阈值命中）。"""
    specs = {s.sample_id: s for s in SPECS}
    assert specs["sample_04"].prepayment_percent == 60.0  # > 政策上限 30
    assert specs["sample_05"].warranty_months == 6  # < 政策下限 12
    assert specs["sample_05"].confidentiality_months == 60  # > 政策上限 36
    assert specs["sample_03"].penalty_daily_percent == 1.5
    assert specs["sample_03"].liability_cap_percent == 5.0
