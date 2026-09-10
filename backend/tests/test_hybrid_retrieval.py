"""混合检索单测：分词 / BM25 / RRF 融合 / 混合检索命中 / 模式开关（全部离线）。"""

from __future__ import annotations

from backend.app.policy_rag import (
    BM25Index,
    IndexDoc,
    HybridRetriever,
    MemoryStore,
    PolicyHit,
    _tokenize,
    retrieve_policies,
    rrf_fuse,
)


class FakeEmbeddings:
    """确定性假向量：按字符桶累加（与 test_policy_rag 同思路，离线可跑）。"""

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * 64
        for ch in text:
            vec[ord(ch) % 64] += 1.0
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def _docs() -> list[IndexDoc]:
    """三份政策条文：预付款 / 质保 / 数据出境。"""
    return [
        IndexDoc(text="预付款不得超过合同总额的30%，超限应要求下调", source="P-01.md", policy_ref="P-01"),
        IndexDoc(text="质保期不得少于12个月", source="P-02.md", policy_ref="P-02"),
        IndexDoc(text="数据出境应通过安全评估或标准合同", source="P-11.md", policy_ref="P-11"),
    ]


def test_tokenize_splits_chinese_terms() -> None:
    """分词能切开中文术语（BM25 依赖词元重叠）。"""
    tokens = _tokenize("预付款不得超过合同总额")
    assert "预付款" in tokens and "合同" in tokens


def test_bm25_ranks_matching_doc_first() -> None:
    """BM25：查询"数据出境安全评估"应把 P-11 排第一。"""
    hits = BM25Index(_docs()).search("数据出境安全评估", k=3)
    assert hits and hits[0].policy_ref == "P-11"
    # 无词元重叠的查询 → 空结果（不污染 RRF）
    assert BM25Index(_docs()).search("完全无关的天气话题", k=3) == []


def test_rrf_fuse_hand_computed_order() -> None:
    """RRF 融合手算：两路都排第一的文档得分最高，仅一路命中的次之。"""
    a = PolicyHit(policy_ref="P-01", source="a.md", text="x", score=0.9)
    b = PolicyHit(policy_ref="P-02", source="b.md", text="y", score=0.8)
    c = PolicyHit(policy_ref="P-11", source="c.md", text="z", score=0.7)
    # 向量路：a > b；BM25 路：a > c → a 两路第一，b/c 各一路第二
    fused = rrf_fuse([[a, b], [a, c]], k=60, top_k=3)
    assert [h.policy_ref for h in fused] == ["P-01", "P-02", "P-11"]
    assert fused[0].score > fused[1].score


def test_hybrid_retriever_uses_both_paths() -> None:
    """混合检索：向量 + BM25 各自命中时仍能返回正确政策（离线假向量）。"""
    store = MemoryStore(embedding_model=FakeEmbeddings())
    docs = _docs()
    store.insert(docs)
    hits = HybridRetriever(store, docs).search("数据出境安全评估标准合同", k=2)
    assert hits and hits[0].policy_ref == "P-11"


def test_retrieve_policies_mode_switch() -> None:
    """模式开关：vector 与 hybrid 均可返回命中（默认 hybrid 走缓存检索器）。"""
    fake = FakeEmbeddings()
    vector_hits = retrieve_policies("质保期不得少于12个月", k=1, backend="memory", embedding_model=fake, mode="vector")
    hybrid_hits = retrieve_policies("质保期不得少于12个月", k=1, backend="memory", embedding_model=fake, mode="hybrid")
    assert vector_hits and vector_hits[0].policy_ref == "P-02"
    assert hybrid_hits and hybrid_hits[0].policy_ref == "P-02"
