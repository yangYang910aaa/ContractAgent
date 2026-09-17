"""政策库运维助手（轻档）单测：解析/体例重排/重叠分级/可核对冲突/产物落盘，全部离线。"""

from __future__ import annotations

import re
from pathlib import Path

from backend.app.policy import drafts
from backend.app.policy.rag import POLICY_DIR, PolicyHit

_DRAFT = """# 采购合同审核制度 · 细则 P-29：示例政策

文件编号：P-29　　版本：V1.0　　生效日期：2026年10月1日
归口部门：集团采购管理中心
适用范围：本集团对外签署的采购合同。

## 第一条 制度目的

示例。

## 第二条 预付款比例上限

预付款合计不得超过合同总额的 40%。
"""


def _hit(ref: str, text: str, score: float) -> PolicyHit:
    """构造一条检索命中（离线用，不连向量库）。"""
    return PolicyHit(policy_ref=ref, source=f"{ref}_x.md", text=text, score=score)


def _retriever(mapping):
    """按"查询里出现的条文标题"给不同命中，用来控制重叠等级。"""
    return lambda query, k: mapping.get("预付款" if "预付款" in query else "其他", [])


def test_parse_real_policy_file() -> None:
    """解析现有政策文件：编号/版本/生效日期/归口/适用范围与条文数都要认出来。"""
    path = POLICY_DIR / "P-15_格式条款与免责限制.md"
    parsed = drafts.parse_policy(path.read_text(encoding="utf-8"), source=path.name)
    assert parsed["ref"] == "P-15"
    assert parsed["version"] == "V1.0"
    assert parsed["effective_date"] == "2026年9月14日"
    assert parsed["owner"] and parsed["scope"]
    assert len(parsed["articles"]) == 5
    assert parsed["articles"][1]["heading"].startswith("第二条")


def test_render_draft_keeps_format_and_marks_missing() -> None:
    """缺元信息时草稿里写「待填」而不是替作者编；条文头按体例保留。"""
    parsed = drafts.parse_policy("## 第二条 预付款不得超过 30%\n\n正文一句。\n")
    draft = drafts.render_draft(parsed)
    assert "文件编号：（待填）" in draft
    assert "## 第二条 预付款不得超过 30%" in draft


def test_overlap_levels_from_injected_retriever() -> None:
    """重叠分级只看最高余弦分：≥0.8 高、≥0.7 中、其余低。"""
    parsed = drafts.parse_policy(_DRAFT)
    overlaps = drafts.find_overlaps(
        parsed,
        retriever=_retriever({
            "预付款": [_hit("P-01", "预付款不得超过合同总额的 30%。", 0.83)],
            "其他": [_hit("P-05", "成果归属。", 0.42)],
        }),
    )
    levels = {item["article"]: item["level"] for item in overlaps}
    assert levels["第二条 预付款比例上限"] == "high"
    assert levels["第一条 制度目的"] == "low"


def test_conflicts_flag_duplicate_ref_and_missing_meta() -> None:
    """可核对的冲突：编号撞号、元信息缺失。"""
    parsed = drafts.parse_policy(_DRAFT)
    parsed["ref"] = "P-15"  # 故意撞上现有政策编号
    parsed["version"] = ""
    parsed["missing"] = ["version"]
    conflicts = drafts.detect_conflicts(parsed, overlaps=[])
    kinds = {item["kind"] for item in conflicts}
    assert "编号重复" in kinds and "元信息缺失" in kinds


def test_conflicts_flag_threshold_mismatch() -> None:
    """同一主题数字对不上 → 阈值不一致（新政策 40% vs 既有 30%）。"""
    parsed = drafts.parse_policy(_DRAFT)
    overlaps = drafts.find_overlaps(
        parsed,
        retriever=_retriever({"预付款": [_hit("P-01", "不得超过合同总额的 30%。", 0.83)], "其他": []}),
    )
    conflicts = drafts.detect_conflicts(parsed, overlaps)
    assert any("阈值不一致" in item["kind"] and "40" in item["detail"] for item in conflicts)


def test_run_assist_writes_artifacts(tmp_path: Path) -> None:
    """跑一遍起稿：草稿、重叠记录、配套清单（另加回读用的 meta）落盘，清单里带政策库版本。"""
    source = tmp_path / "P-29_示例政策.md"
    source.write_text(_DRAFT, encoding="utf-8")
    result = drafts.run_assist(
        source, retriever=_retriever({"其他": []}), out_dir=tmp_path / "out"
    )
    out = result["out_dir"]
    assert (out / "overlaps.json").is_file()
    checklist = (out / "checklist.md").read_text(encoding="utf-8")
    assert "政策库当前版本：PL-" in checklist
    assert result["draft"].is_file()
    assert result["parsed"]["ref"] == "P-29"


def test_draft_dir_name_strips_draft_suffix(tmp_path: Path, monkeypatch) -> None:
    """来源名以 _draft 结尾（把上一次的产物再喂回来）时，目录名不再重复带 _draft。"""
    monkeypatch.setattr(drafts, "DRAFTS_DIR", tmp_path / "drafts")
    source = tmp_path / "P-29_draft.md"
    source.write_text(_DRAFT, encoding="utf-8")
    result = drafts.run_assist(source, retriever=_retriever({"其他": []}))
    assert re.fullmatch(r"P-29_\d{6}", result["draft_id"])


def test_placeholder_meta_counts_as_missing() -> None:
    """草稿里的「（待填）」不是真值：回读时按缺项处理，否则占位符会被当成填好的版本号。"""
    parsed = drafts.parse_policy("## 第一条 正文\n\n一句话。\n", source="P-29_示例政策.md")
    again = drafts.parse_policy(
        drafts.render_draft(parsed), source="P-29_示例政策.md"
    )
    assert again["ref"] == "P-29"  # 编号从来源名兜底
    assert set(again["missing"]) == {"version", "effective_date", "owner", "scope"}


def test_suggest_file_name_from_title() -> None:
    """建议入库文件名 = 编号 + 标题冒号后的短名（语料文件名就是这个形态）。"""
    parsed = drafts.parse_policy(_DRAFT)
    assert drafts.suggest_file_name(parsed) == "P-29_示例政策.md"
