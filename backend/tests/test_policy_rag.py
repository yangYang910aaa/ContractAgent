"""policy_rag 单测：内存检索 + 政策入库 + store 工厂（用确定性假向量，离线可跑）。"""

from backend.app import policy_rag as pr
from backend.app.policy_rag import IndexDoc, MemoryStore, get_store, index_policies, retrieve_policies


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
    assert store.doc_count == 5  # data/policies 共 5 条政策
    refs = {d.policy_ref for d in store.docs}
    assert refs == {f"P-0{i}" for i in range(1, 6)}
    # 政策引用随检索结果带回（防"凭空判断"的依据）
    hits = store.similarity_search("保密期超过 36 个月属于高风险", k=1)
    assert hits[0].policy_ref == "P-04"


def test_retrieve_policies_helper_with_memory_backend() -> None:
    hits = retrieve_policies("质保期不到 12 个月", k=1, backend="memory", embedding_model=FakeEmbeddings())
    assert hits[0].policy_ref == "P-02"


def test_store_factory_memory_backend() -> None:
    assert isinstance(get_store(backend="memory"), MemoryStore)
