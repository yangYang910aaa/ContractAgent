"""pipeline 纯逻辑单测（不调 LLM / 不联网）：报告组装 + 政策检索去重。"""

from backend.app.pipeline import _policy_snippet, build_report, enrich_policy_hits
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
    # 完整条文随报告带回（前端"查看完整条文"用），片段不裸存 markdown 标题
    assert hits[0]["text"] == "条文"
    # 分条后 text=命中条文，full_text=整份政策（假检索无真实文件 → 空串但键必须存在）
    assert "full_text" in hits[0]
    assert "##" not in hits[0]["snippet"]


def test_enrich_policy_hits_batch_path_matches_loop(monkeypatch) -> None:
    """默认路径走批量检索，结果必须与逐条检索完全一致（加速不改引用内容）。"""
    from backend.app import pipeline

    risks = [_risk("P-01", evidence="预付款比例 60%"), _risk("P-04", evidence="保密期 60 个月")]
    calls: list[list[str]] = []

    def fake_many(queries, k=1, **kwargs):
        calls.append(list(queries))
        return [[PolicyHit(policy_ref="P-01" if "预付" in q else "P-04", source="", text="条文 " + q, score=0.9)] for q in queries]

    monkeypatch.setattr(pipeline, "retrieve_policies_many", fake_many)
    batch_hits = pipeline.enrich_policy_hits(risks)

    # 逐条路径用等价的检索器 → 两者的引用条目应逐字段相同
    loop_hits = pipeline.enrich_policy_hits(
        risks, retriever=lambda q: [PolicyHit(policy_ref="P-01" if "预付" in q else "P-04", source="", text="条文 " + q, score=0.9)]
    )
    assert calls == [["预付款比例 60%", "保密期 60 个月"]]  # 一次批量、两条 query
    assert batch_hits == loop_hits


def test_enrich_policy_hits_calls_retriever_once_per_policy() -> None:
    """每个政策引用只检索一次：检索内部要调一次向量化接口，多调一次就是白等一轮往返。"""
    calls: list[str] = []

    def counting(query: str) -> list[PolicyHit]:
        calls.append(query)
        return [PolicyHit(policy_ref="P-01", source="x.md", text="条文", score=0.9)]

    hits = enrich_policy_hits([_risk("P-01"), _risk("P-01")], retriever=counting)
    assert len(calls) == 1
    assert hits[0]["policy_ref"] == "P-01"


def test_policy_snippet_cuts_at_sentence_and_strips_markdown() -> None:
    """片段生成：去 # 标题、保留行结构、在句末截断而不是硬切半句。"""
    long_doc = (
        "## 第一条 保密条款与期限\n"
        "涉及保密信息的采购合同应约定保密条款。保密期限自合同终止之日起不超过三十六个月。"
        "超出部分按双方另行约定执行，且不得违反前款上限。"
    )
    snip = _policy_snippet(long_doc, limit=60)
    assert "##" not in snip
    lines = snip.split("\n")
    assert lines[0] == "第一条 保密条款与期限"
    assert "…" in snip  # 超长 → 省略号
    assert len(lines) >= 2  # 标题与正文各占一行


def test_policy_snippet_short_doc_kept_whole() -> None:
    assert _policy_snippet("## 第一条\n质保期不少于 12 个月。", limit=200) == "第一条\n质保期不少于 12 个月。"


def test_policy_snippet_splits_inline_meta_onto_own_lines() -> None:
    """同行多段信息（文件编号/版本/生效日期）应各占一行，方便逐行阅读。"""
    doc = "## 细则\n文件编号：P-04　　版本：V2.0　　生效日期：2026年9月5日\n正文一句话。"
    snip = _policy_snippet(doc, limit=200)
    assert snip.split("\n")[1] == "文件编号：P-04"
    assert snip.split("\n")[2] == "版本：V2.0"
    assert snip.split("\n")[3] == "生效日期：2026年9月5日"


def test_enrich_policy_hits_retriever_failure_tolerated() -> None:
    def boom(query: str) -> list[PolicyHit]:
        raise RuntimeError("milvus down")

    hits = enrich_policy_hits([_risk()], retriever=boom)
    assert hits == [{"policy_ref": "P-01", "score": None, "snippet": ""}]
