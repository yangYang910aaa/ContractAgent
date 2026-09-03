"""pipeline 纯逻辑单测（不调 LLM / 不联网）：报告组装 + 政策检索去重。"""

from backend.app.pipeline import build_report, enrich_policy_hits
from backend.app.policy_rag import PolicyHit
from backend.app.schemas import ContractModel, RiskItem, Severity


def _risk(policy_ref: str | None = "P-01", evidence: str = "预付款比例 60%") -> RiskItem:
    return RiskItem(
        risk_type="prepayment_ratio_high",
        severity=Severity.high,
        policy_ref=policy_ref,
        evidence=evidence,
        suggestion="降至 30% 以内",
    )


def test_build_report_shape() -> None:
    report = build_report("demo.md", ContractModel(), [_risk()], [])
    assert report["contract_file"] == "demo.md"
    assert report["grade"] == "fail"  # 有 high → fail
    assert len(report["risks"]) == 1
    assert report["risks"][0]["severity"] == "high"


def test_enrich_policy_hits_dedup_and_skip_nonpolicy() -> None:
    risks = [
        _risk("P-01", evidence="预付款比例 60%"),
        _risk("P-01", evidence="预付款比例 60%"),
        _risk("P-04", evidence="保密期 60 个月"),
        _risk(None),
    ]
    fake = lambda q: [PolicyHit(policy_ref="P-01" if "预付" in q else "P-04", source="x.md", text="条文", score=0.9)]
    hits = enrich_policy_hits(risks, retriever=fake)
    # 同 policy_ref 只查一次；无政策编号的风险不检索
    assert [h["policy_ref"] for h in hits] == ["P-01", "P-04"]
    assert hits[0]["score"] == 0.9


def test_enrich_policy_hits_retriever_failure_tolerated() -> None:
    def boom(query: str) -> list[PolicyHit]:
        raise RuntimeError("milvus down")

    hits = enrich_policy_hits([_risk()], retriever=boom)
    assert hits == [{"policy_ref": "P-01", "score": None, "snippet": ""}]
