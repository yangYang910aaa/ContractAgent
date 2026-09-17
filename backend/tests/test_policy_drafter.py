"""政策起草测试：体例归一、条号中文、数字护栏、落盘与来源标注，全部离线（注入假起草器）。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.app.policy import drafts, drafter

_BRIEF = "预付款要限制比例，未提供履约担保的应更严；比例按合同总额计算。"

_DRAFTED = {
    "title": "采购合同审核制度 · 细则 P-16：预付款与担保",
    "scope": "本集团对外签署的采购合同；预付款与履约担保的审查。",
    "articles": [
        {
            "heading": "制度目的",
            "body": "控制预付款造成的资金占用与供应商履约风险。",
            "explanation": "把预付款与担保绑在一起管。",
            "checkpoints": ["看付款计划里交付/验收前支付的金额", "与合同总额相除得比例"],
            "guards": ["合同未约定预付款的不报"],
        },
        {
            "heading": "预付款比例上限",
            "body": "预付款合计不得超过合同总额的 40%；未要求履约担保的按公司制度确定。",
            "explanation": "超过比例说明资金与履约风险都偏高。",
            "checkpoints": ["比例超过上限时核对是否有等额担保"],
            "guards": ["对方违约在先的免责不属本细则范围"],
        },
    ],
    "notes": ["生效日期与归口部门待填", "未提供担保时的具体比例阈值待定"],
    "risk_types": [
        {
            "risk_type": "prepayment_ratio_high",
            "label": "预付款比例过高",
            "why": "管的就是交付前付款比例",
            "evidence_hint": "看付款计划里含「预付/首付」的期次，与合同总额相除",
        },
        {
            "risk_type": "prepayment_cap_missing",
            "label": "预付款上限缺失",
            "why": "本条还要求写清上限",
            "evidence_hint": "看付款条款有没有写「不超过」",
        },
        {
            "risk_type": "prepayment_ratio_high",
            "label": "重复的一条",
            "why": "模型常把同一类型列两遍",
            "evidence_hint": "",
        },
    ],
    "samples": [
        {
            "goal": "验证超比例判 fail",
            "kind": "enterprise_goods",
            "defect": "付款计划写「合同生效后 10 日内预付 50%」",
            "expected_grade": "fail",
        },
        {
            "goal": "验证品类取值兜底",
            "kind": "政采货物",
            "defect": "首付款 60%",
            "expected_grade": "严重",
        },
    ],
    "retrievals": [
        {"query": "预付款最多能给多少", "policy_ref": "P-16", "expect": "预付款比例上限"},
    ],
}


def _drafter(brief: str, meta_line: str) -> dict:
    """假起草器：不改需求、只回一份固定产出，用来验证归一与落盘。"""
    return _DRAFTED


def test_render_matches_corpus_style() -> None:
    """归一成政策文本：中文条号、文件头元信息、文末一条「审查提示」含要点与护栏。"""
    text = drafter.render_ai_draft(_DRAFTED, ref="P-16", group="集团采购管理中心")
    assert text.startswith("# 采购合同审核制度 · 细则 P-16：预付款与担保")
    assert "文件编号：P-16　　版本：V1.0　　生效日期：（待填）" in text
    assert "归口部门：集团采购管理中心" in text
    assert "## 第一条 制度目的" in text and "## 第二条 预付款比例上限" in text
    assert "## 第三条 审查提示" in text
    assert "- 判定要点：比例超过上限时核对是否有等额担保" in text
    assert "- 护栏：对方违约在先的免责不属本细则范围" in text
    # 归一后的文本要能被起稿的解析器认出来（条数与元信息）
    parsed = drafts.parse_policy(text, source="P-16_AI起草.md")
    assert parsed["ref"] == "P-16" and len(parsed["articles"]) == 3
    # 归口部门这次给了、生效日期没给 → 只报缺生效日期
    assert parsed["missing"] == ["effective_date"]


def test_new_numbers_flags_only_invented_values() -> None:
    """数字护栏：只挑需求里没有的百分比/月数；需求给过的数字不算新引入。"""
    text = drafter.render_ai_draft(_DRAFTED, ref="P-16")
    findings = drafter.new_numbers("预付款不得超过合同总额的 30%。", text)
    assert findings and findings[0]["article"].startswith("第")
    assert "40" in findings[0]["percent"] and "30" not in findings[0]["percent"]
    # 需求里给过 40% 时就不再提示
    assert drafter.new_numbers("比例 40%，按合同总额计算。", text) == []


def test_draft_policy_writes_draft_with_ai_meta(tmp_path: Path, monkeypatch) -> None:
    """起草产物落盘：草稿文本 + meta 里的来源标注与模型产出，回读拿得到。"""
    monkeypatch.setattr(drafts, "DRAFTS_DIR", tmp_path / "drafts")
    result = drafter.draft_policy(
        _BRIEF,
        ref="P-16",
        group="集团采购管理中心",
        effective_date="2026年10月1日",
        drafter=_drafter,
        retriever=lambda query, k: [],
    )
    draft_id = result["draft_id"]
    assert (result["draft"]).is_file()

    detail = drafts.load_draft(draft_id)
    assert detail is not None
    assert detail["origin"] == "ai"
    assert detail["suggested_file"] == "P-16_预付款与担保.md"
    assert detail["ai"]["brief"].startswith("预付款要限制比例")
    assert [item["heading"] for item in detail["ai"]["articles"]][:1] == ["制度目的"]
    assert detail["ai"]["notes"] and detail["ai"]["new_numbers"]
    assert detail["ai"]["suggestions"]["risk_types"][0]["risk_type"] == "prepayment_ratio_high"
    assert "## 第二条 预付款比例上限" in detail["draft"]


def test_draft_policy_leaves_unknown_meta_blank(tmp_path: Path, monkeypatch) -> None:
    """没给的元信息写「（待填）」而不是编：起稿后缺项列表能如实报出来。"""
    monkeypatch.setattr(drafts, "DRAFTS_DIR", tmp_path / "drafts")
    result = drafter.draft_policy(_BRIEF, drafter=_drafter, retriever=lambda query, k: [])
    parsed = result["parsed"]
    assert parsed["missing"] == ["ref", "effective_date", "owner"]
    assert json.loads((result["out_dir"] / "meta.json").read_text(encoding="utf-8"))["origin"] == "ai"


def test_cn_number() -> None:
    """条号转中文：一~十、十一、二十一都要对（政策体例用中文条号）。"""
    assert [drafter.cn_number(n) for n in (1, 9, 10, 11, 20, 21, 30)] == [
        "一", "九", "十", "十一", "二十", "二十一", "三十",
    ]


def test_normalize_suggestions_flags_unknown_code_and_bad_values() -> None:
    """配套建议的护栏：自造的风险类型编码标成待新增，非法品类与评级归位并留提示。"""
    result = drafter.normalize_suggestions(_DRAFTED)

    # 既有编码标 known、按出现顺序去重（重复那条丢掉）
    assert [item["risk_type"] for item in result["risk_types"]] == [
        "prepayment_ratio_high",
        "prepayment_cap_missing",
    ]
    assert result["risk_types"][0]["known"] is True
    assert result["risk_types"][0]["label"] == "预付款比例过高"
    assert result["risk_types"][0]["evidence_hint"].startswith("看付款计划")
    # 库里没有这个编码 → 标成待新增（编码不能由模型发明）
    assert result["risk_types"][1]["known"] is False
    # 展示名以登记表为准：模型把机器码原样填进 label 时不许透到页面
    assert result["risk_types"][0]["label"] != result["risk_types"][0]["risk_type"]

    # 非法品类归到企业货物品类、非法评级留空，两条改动都在提示里说明
    assert result["samples"][0]["kind"] == "enterprise_goods"
    assert result["samples"][0]["expected_grade"] == "fail"
    assert result["samples"][1]["kind"] == "enterprise_goods"
    assert result["samples"][1]["expected_grade"] == ""
    assert len(result["notes"]) == 2
    assert result["notes"][0].startswith("样本建议里的品类")
    assert result["retrievals"][0]["policy_ref"] == "P-16"

    # 模型把同一个非法品类写三遍 → 提示只说一次，页面别刷同一句话
    repeated = drafter.normalize_suggestions(
        {"samples": [{"kind": "supplies", "expected_grade": "fail"}] * 3}
    )
    assert len(repeated["notes"]) == 1
    # 已知编码但 label 也写成机器码 → 用登记表里的中文名
    coded = drafter.normalize_suggestions(
        {"risk_types": [{"risk_type": "warranty_too_short", "label": "warranty_too_short"}]}
    )
    assert coded["risk_types"][0]["label"] == "质保期不足"
    # 模型把品类编码串进 goal → 那不是"要验证什么"，留空不展示
    stray = drafter.normalize_suggestions({"samples": [{"goal": "tech_service", "kind": "tech_service"}]})
    assert stray["samples"][0]["goal"] == "" and stray["samples"][0]["kind"] == "tech_service"


def test_normalize_suggestions_tolerates_missing_sections() -> None:
    """模型没给某一组（或整段为空）时当空列表处理，不炸也不塞占位内容。"""
    result = drafter.normalize_suggestions({})
    assert result == {"risk_types": [], "samples": [], "retrievals": [], "notes": []}
    # 编码写成 new（自己承认库里没有）→ 也按待新增展示，标签用模型给的中文名
    result = drafter.normalize_suggestions(
        {"risk_types": [{"risk_type": "new", "label": "担保缺失", "why": "无对应编码", "evidence_hint": "看担保条款"}]}
    )
    assert result["risk_types"][0]["known"] is False
    assert result["risk_types"][0]["label"] == "担保缺失"
