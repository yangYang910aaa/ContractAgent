"""引用接地校验的离线用例：编号、正文、对应政策、阈值四类判定，外加评测口径。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app import policy_grounding
from backend.eval.run_eval import _citation_metrics
from backend.app.pipeline import build_report
from backend.app.schemas import ContractModel, RiskItem, Severity

_POLICY_01 = """# 细则 P-01：预付款管理

文件编号：P-01　　版本：V2.0　　生效日期：2026年9月5日
适用范围：货物类、农副产品、技术开发与服务类采购/买卖合同。

## 第二条 预付款比例上限

预付款合计不得超过合同总额的 30%。
"""

_POLICY_09 = """# 细则 P-09：转包与分包限制

文件编号：P-09　　版本：V1.0　　生效日期：2026年9月9日
适用范围：定制交付与技术服务形态的合同；农副产品等现货买卖与政府采购示范文本项目
不适用本细则。

## 第二条 分包限制

未经采购方书面同意不得转包或分包。
"""


@pytest.fixture()
def corpus_dir(tmp_path: Path) -> Path:
    """两文件的小语料：一份够用（P-01），一份带"不适用"排除句（P-09）。"""
    directory = tmp_path / "policies"
    directory.mkdir()
    (directory / "P-01_预付款比例.md").write_text(_POLICY_01, encoding="utf-8")
    (directory / "P-09_转包与分包限制.md").write_text(_POLICY_09, encoding="utf-8")
    return directory


def _risk(**kwargs) -> RiskItem:
    """一条最小风险：默认是规则侧产出的预付款高风险，测试按需覆盖字段。"""
    base = {
        "risk_type": "prepayment_ratio_high",
        "label": "预付款比例过高",
        "severity": Severity.high,
        "policy_ref": "P-01",
    }
    base.update(kwargs)
    return RiskItem(**base)


def test_correct_citation_passes(corpus_dir: Path) -> None:
    result = policy_grounding.check_citations([_risk()], policy_dir=corpus_dir)
    assert result["summary"] == {"cited": 1, "grounded": 1, "rate": 1.0, "noted": 0}
    assert result["items"][0]["issues"] == []


def test_unknown_policy_ref_is_flagged(corpus_dir: Path) -> None:
    item = policy_grounding.check_citations(
        [_risk(policy_ref="P-99")], policy_dir=corpus_dir
    )["items"][0]
    assert item["ok"] is False
    assert any("没有 P-99" in issue for issue in item["issues"])


def test_wrong_policy_for_risk_type_is_flagged(corpus_dir: Path) -> None:
    # 预付款比例的风险引到转包政策 → 编号真实存在，但与结论对不上
    item = policy_grounding.check_citations(
        [_risk(policy_ref="P-09")], policy_dir=corpus_dir
    )["items"][0]
    assert item["ok"] is False
    assert any("对应 P-01" in issue for issue in item["issues"])


def test_threshold_missing_from_policy_text_is_flagged(tmp_path: Path) -> None:
    directory = tmp_path / "policies"
    directory.mkdir()
    # 政策正文没写 30% 这个阈值 → 无法支撑"超过上限"的结论
    (directory / "P-01_预付款比例.md").write_text(
        _POLICY_01.replace("不得超过合同总额的 30%", "应从严掌握"), encoding="utf-8"
    )
    item = policy_grounding.check_citations([_risk()], policy_dir=directory)["items"][0]
    assert item["ok"] is False
    assert any("阈值" in issue for issue in item["issues"])


def test_explicit_scope_exclusion_only_warns(corpus_dir: Path) -> None:
    # 政采合同引 P-09：政策自己写明这类合同不适用 → 出提示，但不算硬性不通过
    result = policy_grounding.check_citations(
        [_risk(risk_type="subcontract_unrestricted", policy_ref="P-09")],
        contract_kind="gov_goods",
        policy_dir=corpus_dir,
    )
    item = result["items"][0]
    assert item["ok"] is True
    assert item["notes"] and "不适用" in item["notes"][0]
    assert result["summary"]["noted"] == 1


def test_risks_without_policy_ref_are_not_counted(corpus_dir: Path) -> None:
    risks = [_risk(), _risk(risk_type="missing_required_field", policy_ref=None)]
    result = policy_grounding.check_citations(risks, policy_dir=corpus_dir)
    assert result["summary"]["cited"] == 1
    assert len(result["items"]) == 1


def test_review_origin_citation_is_checked_too(corpus_dir: Path) -> None:
    """复核（盲审）填的编号同样过这套检查——这正是"模型乱引"要拦的地方。"""
    item = policy_grounding.check_citations(
        [_risk(origin="review", policy_ref="P-09")], policy_dir=corpus_dir
    )["items"][0]
    assert item["origin"] == "review"
    assert item["ok"] is False


def test_every_rule_policy_ref_is_known() -> None:
    """规则侧写进报告的政策编号都要在校验表里——新增规则忘了登记就会在这里挂掉。"""
    rules_dir = Path(__file__).resolve().parents[1] / "app" / "rules"
    refs = set()
    for path in rules_dir.glob("*.py"):
        refs |= set(re.findall(r'policy_ref="(P-\d+)"', path.read_text(encoding="utf-8")))
    assert refs, "规则包里没有找到政策编号，检查正则"
    known = set(policy_grounding._EXPECTED_POLICY.values())
    assert refs <= known, f"这些编号没进校验表: {sorted(refs - known)}"


def test_every_expected_policy_exists_in_corpus() -> None:
    """校验表里的编号都要在真实语料里有对应文件（防写错编号）。"""
    corpus = policy_grounding.load_corpus()
    assert set(policy_grounding._EXPECTED_POLICY.values()) <= set(corpus)


def test_report_carries_citation_checks() -> None:
    report = build_report("demo.md", ContractModel(), [_risk()], [])
    block = report["citation_checks"]
    assert block["summary"]["cited"] == 1
    assert block["items"][0]["policy_ref"] == "P-01"


def test_eval_citation_metric_counts_all_with_high_breakdown() -> None:
    """评测口径：全部引用进主指标，high 单独给一份分项。"""
    observed = [
        {
            "citations": {
                "items": [
                    {"severity": "high", "ok": True},
                    {"severity": "high", "ok": False},
                    {"severity": "medium", "ok": False},
                ]
            }
        },
        {"citations": {"items": [{"severity": "high", "ok": True}]}},
    ]
    metrics = _citation_metrics(observed)
    assert metrics["citation_cited"] == 4
    assert metrics["citation_grounded"] == 2
    assert metrics["citation_accuracy"] == 0.5
    assert metrics["citation_high_cited"] == 3
    assert metrics["citation_high_grounded"] == 2
    assert metrics["citation_high_accuracy"] == 0.6667


def test_eval_citation_metric_without_citations() -> None:
    """整批都没有引用时给出 None，而不是 0（避免把"没得比"当成"全不对"）。"""
    assert _citation_metrics([{}, {"citations": None}])["citation_accuracy"] is None
