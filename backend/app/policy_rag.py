"""政策库检索

两层设计(对应执行计划风险预案"Milvus 可达则用，否则用内存")
- MemoryStore:进程内余弦相似度，离线可测、零外部依赖；
- MilvusStore:pymilvus 3.0.1 MilvusClient 写法，插入后必须 flush()
  HNSW + COSINE,用于正式入库检索；

数据:data/policies/*.md(P-01~P-05)，每条政策一个检索单元，
metadata 带 policy_ref,检索结果可回指政策编号（防"凭空判断"）。
"""

from __future__ import annotations

import math
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from backend.app.config import BASE_DIR, settings
from backend.app.llm import get_embedding_model

POLICY_DIR = BASE_DIR / "data" / "policies"


@dataclass
class IndexDoc:
    """一条待入库的政策片段：正文 + 来源 + 政策编号。"""

    text: str  # 政策正文（含条文与判定要点）
    source: str  # 来源文件名（如 P-01_预付款比例.md）
    policy_ref: str  # 政策编号（如 P-01），检索结果的引用依据


@dataclass
class PolicyHit:
    """一次检索命中：政策编号 + 正文 + 相似度分。"""

    policy_ref: str
    source: str
    text: str
    score: float  # 余弦相似度（0~1，越高越相关）


def load_policies() -> list[IndexDoc]:
    """读 data/policies/*.md 为待入库条目；编号从文件名前缀解析（P-0X）。"""
    docs: list[IndexDoc] = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        match = re.match(r"(P-\d+)", path.name)
        # 分支：文件名不带 P-编号 → 跳过（防止脏文件混入政策库）
        if not match:
            continue
        docs.append(
            IndexDoc(
                text=path.read_text(encoding="utf-8").strip(),
                source=path.name,
                policy_ref=match.group(1),
            )
        )
    return docs


class MemoryStore:
    """进程内向量检索：文本 → 向量后存内存，查询按余弦相似度取 top-k。"""

    def __init__(self, embedding_model=None):
        # embedding_model 可注入（测试用假向量），默认用 DashScope 模型
        self._embedding = embedding_model or get_embedding_model()
        self._docs: list[IndexDoc] = []  # 正文与元数据（与向量一一对应）
        self._vectors: list[list[float]] = []  # 已归一化向量

    @property
    def docs(self) -> list[IndexDoc]:
        """已入库条目"""
        return list(self._docs)

    @property
    def doc_count(self) -> int:
        """已入库条数。"""
        return len(self._docs)

    def insert(self, docs: list[IndexDoc]) -> None:
        """向量化并入库存量；文本为空的行跳过。"""
        texts = [d.text for d in docs if d.text.strip()]
        vectors = self._embedding.embed_documents(texts)
        for doc, vec in zip(docs, vectors):
            if not doc.text.strip():
                continue
            self._docs.append(doc)
            self._vectors.append(_normalize(vec))

    def similarity_search(self, query: str, k: int = 2) -> list[PolicyHit]:
        """把 query 向量化，与库内全部向量算余弦相似度取前 k。"""
        q = _normalize(self._embedding.embed_query(query))
        scored = [
            (doc, _cosine(q, vec))
            for doc, vec in zip(self._docs, self._vectors)
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [
            PolicyHit(policy_ref=doc.policy_ref, source=doc.source, text=doc.text, score=score)
            for doc, score in scored[:k]
        ]


class MilvusStore:
    """Milvus 政策库封装(pymilvus 3.0.1,MilvusClient 写法)。

    集合字段:pk(auto) / text / source / policy_ref / vector。
    注意坑:insert 后必须 flush() 再建索引，否则 row_count 恒为 0、检索不到。
    """

    def __init__(self, uri: str = "", collection_name: str = "", embedding_model=None):
        self.uri = uri or settings.milvus_uri
        self.collection_name = collection_name or settings.milvus_collection
        self._embedding = embedding_model or get_embedding_model()
        # 延迟导入：只有真用 Milvus 才拉 pymilvus，避免拖慢内存路径
        from pymilvus import MilvusClient

        self.client = MilvusClient(uri=self.uri)

    def _probe_dim(self) -> int:
        """用一句话向量化探测维度（DashScope qwen3.7 = 1024）。"""
        return len(self._embedding.embed_query("测试"))

    def _ensure_collection(self) -> None:
        """建集合 + 索引并加载；已存在则跳过（幂等）。"""
        from pymilvus import DataType

        # 分支：集合已存在 → 无需重建 schema
        if self.client.has_collection(self.collection_name):
            return
        dim = self._probe_dim()
        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("pk", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("policy_ref", DataType.VARCHAR, max_length=16)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        self.client.create_collection(self.collection_name, schema=schema)
        # 建 HNSW + COSINE 索引并加载，检索才能命中
        index = self.client.prepare_index_params()
        index.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 128},
        )
        self.client.create_index(self.collection_name, index_params=index)
        self.client.load_collection(self.collection_name)

    def insert(self, docs: list[IndexDoc]) -> None:
        """向量化 + 入库 + flush；集合已有数据时跳过（防重复累积）。"""
        self._ensure_collection()
        count = self.client.get_collection_stats(self.collection_name).get("row_count", 0)
        # 分支：已有数据 → 不再重复灌入（可手动清集合后重灌）
        if count > 0:
            print(f"ℹ️  {self.collection_name} 已有 {count} 条，跳过导入")
            return
        rows = [d for d in docs if d.text.strip()]
        vectors = self._embedding.embed_documents([d.text for d in rows])
        data = [
            {"text": d.text, "source": d.source, "policy_ref": d.policy_ref, "vector": v}
            for d, v in zip(rows, vectors)
        ]
        self.client.insert(self.collection_name, data)
        self.client.flush(self.collection_name)  # 关键：不 flush 检索不到

    def similarity_search(self, query: str, k: int = 2) -> list[PolicyHit]:
        """query 向量化后在 Milvus 检索 top-k，返回带政策编号的命中。"""
        vec = self._embedding.embed_query(query)
        results = self.client.search(
            collection_name=self.collection_name,
            data=[vec],
            limit=k,
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
            output_fields=["text", "source", "policy_ref"],
        )
        hits: list[PolicyHit] = []
        for hit in results[0]:
            entity = hit["entity"]
            hits.append(
                PolicyHit(
                    policy_ref=entity.get("policy_ref", ""),
                    source=entity.get("source", ""),
                    text=entity.get("text", ""),
                    score=hit.get("distance", 0.0),
                )
            )
        return hits


# ---- 向量工具（纯函数）----


def _normalize(vec: list[float]) -> list[float]:
    """向量归一化：让点积即余弦相似度，方便内存检索。"""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    """两个已归一化向量的余弦相似度（点积）。"""
    return sum(x * y for x, y in zip(a, b))


def _milvus_reachable(uri: str, timeout: float = 2.0) -> bool:
    """快速探测 Milvus 端口可达性（socket 级，不触发 pymilvus 长超时）。

    分支依据：uri 形如 http://host:port；解析失败或连不上都按不可达处理。
    """
    parsed = urlparse(uri)
    host, port = parsed.hostname, parsed.port or 19530
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_store(backend: str | None = None, embedding_model=None) -> MemoryStore | MilvusStore:
    """store 工厂：按配置选实现，Milvus 不可达自动退回内存。

    backend 取值：memory / milvus / auto（默认读 .env RETRIEVAL_BACKEND）。
    """
    backend = backend or settings.retrieval_backend
    # 分支：显式 memory → 直接用内存，不探测外部依赖
    if backend == "memory":
        return MemoryStore(embedding_model=embedding_model)
    # 分支：milvus 或 auto 且端口可达 → 用真库
    if backend == "milvus" or (backend == "auto" and _milvus_reachable(settings.milvus_uri)):
        return MilvusStore(uri=settings.milvus_uri, embedding_model=embedding_model)
    # 分支：auto 但 Milvus 不可达 → 退回内存（计划风险预案）
    print(f"⚠️  Milvus({settings.milvus_uri}) 不可达，退回内存检索")
    return MemoryStore(embedding_model=embedding_model)


def index_policies(store: MemoryStore | MilvusStore | None = None) -> MemoryStore | MilvusStore:
    """把 data/policies 全部入库（幂等：库里已有数据则跳过）。"""
    store = store or get_store()
    store.insert(load_policies())
    return store


def retrieve_policies(
    query: str,
    k: int = 2,
    backend: str | None = None,
    embedding_model=None,
) -> list[PolicyHit]:
    """按问题检索政策条目，返回带 policy_ref 的命中（供 rules/LLM 引用）。"""
    store = get_store(backend=backend, embedding_model=embedding_model)
    # 分支：内存库为空 → 先灌政策再检索；Milvus 的 insert 自带幂等跳过
    if isinstance(store, MemoryStore) and store.doc_count == 0:
        store.insert(load_policies())
    elif isinstance(store, MilvusStore):
        store.insert(load_policies())
    return store.similarity_search(query, k=k)
