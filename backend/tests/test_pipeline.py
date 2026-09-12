"""pipeline 纯逻辑单测（不调 LLM / 不联网）：报告组装 + 政策检索去重 + 空正文护栏。"""

from backend.app.pipeline import _policy_snippet, build_report, enrich_policy_hits, run_review
from backend.app.policy_rag import PolicyHit
from backend.app.schemas import ContractModel, RiskItem, Severity


def _risk(
    policy_ref: str | None = "P-01",
    evidence: str = "预付款比例 60%",
    suggestion: str = "预付款降至 30% 以内",
) -> RiskItem:
    return RiskItem(
        risk_type="prepayment_ratio_high",
        severity=Severity.high,
        policy_ref=policy_ref,
        evidence=evidence,
        suggestion=suggestion,
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
        _risk("P-04", evidence="保密期 60 个月", suggestion="保密期宜压到 24 个月以内"),
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

    risks = [
        _risk("P-01", evidence="预付款比例 60%"),
        _risk("P-04", evidence="保密期 60 个月", suggestion="保密期宜压到 24 个月以内"),
    ]
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
    # 一次批量、两条 query；query 取建议文本（讲的是"要什么样的条款"，与所引政策对得上）
    assert calls == [["预付款降至 30% 以内", "保密期宜压到 24 个月以内"]]
    assert batch_hits == loop_hits


def test_policy_query_prefers_suggestion_over_evidence() -> None:
    """检索 query 取建议文本：证据原文是合同自己的话，话题常与所引政策两回事。

    真实运维服务合同的证据段讲的是"权利瑕疵担保"，按证据检索会命中知识产权政策，
    报告里就查不到声明的数据合规政策原文。
    """
    queries: list[str] = []

    def spy(query: str) -> list[PolicyHit]:
        queries.append(query)
        return [PolicyHit(policy_ref="P-10", source="p.md", text="条文", score=0.9)]

    enrich_policy_hits(
        [_risk("P-10", evidence="乙方保证交付成果不侵犯第三方专利权、著作权", suggestion="建议按 P-10 补充委托处理要件")],
        retriever=spy,
    )
    assert queries == ["建议按 P-10 补充委托处理要件"]

    # 建议为空时才退回证据
    enrich_policy_hits([_risk("P-10", evidence="乙方保证交付成果不侵犯第三方专利权", suggestion="")], retriever=spy)
    assert queries[-1] == "乙方保证交付成果不侵犯第三方专利权"


def test_policy_hit_prefers_declared_policy() -> None:
    """候选里出现别条政策的条文时，取属于声明政策号的那条；都没有才退回首条。"""
    foreign = PolicyHit(policy_ref="P-05", source="p05.md", text="知识产权条文", score=0.9)
    declared = PolicyHit(policy_ref="P-13", source="p13.md", text="保密例外条文", score=0.8)
    hits = enrich_policy_hits([_risk("P-13", suggestion="保密条款宜写明例外情形")],
                              retriever=lambda q: [foreign, declared])
    assert hits[0]["policy_ref"] == "P-13"
    assert hits[0]["text"] == "保密例外条文"

    fallback = enrich_policy_hits([_risk("P-13", suggestion="保密条款宜写明例外情形")],
                                  retriever=lambda q: [foreign])
    assert fallback[0]["policy_ref"] == "P-05"


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


def test_run_review_empty_file_returns_error_without_llm(tmp_path) -> None:
    """读不出正文的文件（空文件/加密损坏 PDF）→ 直接给错误报告。

    不调模型、不进规则：否则会在空正文上凭空报"缺必填"并停到人工审批闸口。
    """
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    report = run_review(empty)
    assert report["grade"] is None
    assert report["risks"] == []
    assert "无法解析" in report["error"]
    assert report["llm"]["calls"] == 0
