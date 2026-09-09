"""policy_rag 单测：内存检索 + 政策入库 + store 工厂（用确定性假向量，离线可跑）。"""

from backend.app import policy_rag as pr
from backend.app.policy_rag import (
    IndexDoc,
    MemoryStore,
    _split_doc_articles,
    get_store,
    index_policies,
    load_policy_full,
    retrieve_policies,
)


class FakeEmbeddings:
    """确定性假向量：按字符 ord 累加进定长桶并归一化，同句相似度可预期。"""

    DIM = 128

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.DIM
        for ch in text:
            v[ord(ch) % self.DIM] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def test_memory_store_insert_and_search() -> None:
    store = MemoryStore(embedding_model=FakeEmbeddings())
    store.insert(
        [
            IndexDoc(text="预付款不得超过合同总额的 30%", source="P-01_预付款比例.md", policy_ref="P-01"),
            IndexDoc(text="质保期不得少于 12 个月", source="P-02_质量保证期.md", policy_ref="P-02"),
            IndexDoc(text="保密期限不超过 36 个月", source="P-04_保密期限.md", policy_ref="P-04"),
        ]
    )
    hits = store.similarity_search("预付款比例过高，达到 30% 上限", k=1)
    assert hits and hits[0].policy_ref == "P-01"
    assert hits[0].score > 0.5  # 字符桶假向量余弦分偏低，只验相对排序


def test_ingest_policies_into_memory() -> None:
    store = MemoryStore(embedding_model=FakeEmbeddings())
    index_policies(store=store)
    # 纵向分条后：5 个文件各含"文件头 + N 个第X条"检索单元（总数远大于 5）
    assert store.doc_count > 5
    refs = {d.policy_ref for d in store.docs}
    assert refs == {f"P-0{i}" for i in range(1, 6)}
    # 每个政策编号都应同时有"文件头/适用范围"与"第X条"两类单元（细粒度检索的前提）
    for ref in sorted(refs):
        same = [d for d in store.docs if d.policy_ref == ref]
        assert len(same) >= 2, f"{ref} 应拆成头+条文多个检索单元"
        assert any("第" in d.text and "条" in d.text for d in same)
    # 政策引用随检索结果带回（防"凭空判断"的依据）
    hits = store.similarity_search("保密期超过 36 个月属于高风险", k=1)
    assert hits[0].policy_ref == "P-04"
    # 分条后命中应落在含阈值句的具体条文（而不是整份/文件头）
    assert "36 个月" in hits[0].text or "保密" in hits[0].text


def test_retrieve_policies_helper_with_memory_backend() -> None:
    hits = retrieve_policies("质保期不到 12 个月", k=1, backend="memory", embedding_model=FakeEmbeddings())
    assert hits[0].policy_ref == "P-02"


def test_store_factory_memory_backend() -> None:
    assert isinstance(get_store(backend="memory"), MemoryStore)


def test_split_doc_articles_units() -> None:
    """政策全文拆条：文件头单列 + 每个 ## 第X条 独立成单元（含标题行）。"""
    text = (
        "# 采购合同审核制度 细则 P-09\n"
        "文件编号：P-09\n适用范围：企业采购初审\n"
        "## 第一条 预付款上限\n"
        "预付款合计不得超过总额 30%。\n"
        "## 第二条 无需预付情形\n"
        "校服等按示范文本执行，不要求预付安排。\n"
    )
    docs = _split_doc_articles(text, "P-09.md", "P-09")
    assert [d.policy_ref for d in docs] == ["P-09"] * 3
    assert "适用范围" in docs[0].text  # 文件头（适用范围）独立可召回
    assert "第一条 预付款上限" in docs[1].text and "30%" in docs[1].text
    assert "第二条 无需预付情形" in docs[2].text


def test_split_doc_articles_without_headers_falls_back_to_single() -> None:
    """没有条文结构 → 整文件单条（兼容旧版/异常文件）。"""
    docs = _split_doc_articles("只有一句话的政策说明", "P-09.md", "P-09")
    assert len(docs) == 1
    assert docs[0].text == "只有一句话的政策说明"


def test_load_policy_full_returns_whole_file() -> None:
    """整份政策全文按 source 取回（报告"查看完整条文"展开用）；文件不存在回空串。"""
    full = load_policy_full("P-01_预付款比例.md")
    assert "P-01" in full and "预付款" in full
    assert load_policy_full("not-exist.md") == ""
