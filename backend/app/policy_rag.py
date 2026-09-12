"""政策库检索

两层设计: Milvus 可达则用，否则用内存存储。
- MemoryStore:进程内余弦相似度。
- MilvusStore:pymilvus 3.0.1 MilvusClient 写法，插入后必须 flush()
  HNSW + COSINE,用于正式入库检索；

数据:data/policies/*.md(P-01~P-05)。纵向分条后
每个政策文件拆成"文件头(适用范围) + 各 第X条"多个检索单元——检索粒度从
"整份政策"细化到"具体条文"，引用编号仍 policy_ref=P-0X(评测/报告锚点不变)。
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

# 政策文件内条文头：行首"## 第X条 …"（标题行即条文边界，拆条时作单元标题保留）
_ARTICLE_RE = re.compile(r"(?m)^##\s*(第[一二三四五六七八九十百\d]+条.*)$")


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


def _split_doc_articles(
    full_text: str, source: str, policy_ref: str
) -> list[IndexDoc]:
    """把一份政策全文拆成检索单元：文件头一条 + 每个「第X条」一条。

    整文件入库只能命中整份政策，分条后查询能落到具体条文（"预付款上限"命中第二条、
    "无需预付情形"命中第三条）；文件头单列一条，保证适用范围类判定仍可召回。
    正文没有条文结构时整文件一条。
    """
    matches = list(_ARTICLE_RE.finditer(full_text))
    # 这种情况是：无条文结构 → 整文件单条（兼容历史/异常文件）
    if not matches:
        return [IndexDoc(text=full_text.strip(), source=source, policy_ref=policy_ref)]
    docs: list[IndexDoc] = []
    # 第一个条文前的文件头（标题/编号/版本/归口/适用范围）独立成检索单元
    header = full_text[: matches[0].start()].strip()
    if header:
        docs.append(IndexDoc(text=header, source=source, policy_ref=policy_ref))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        article = full_text[m.start() : end].strip()
        if article:
            docs.append(IndexDoc(text=article, source=source, policy_ref=policy_ref))
    return docs


def load_policies() -> list[IndexDoc]:
    """读 data/policies/*.md 并纵向分条为待入库条目; 编号从文件名前缀解析(P-0X)。"""
    docs: list[IndexDoc] = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        match = re.match(r"(P-\d+)", path.name)
        # 分支：文件名不带 P-编号 → 跳过
        if not match:
            continue
        docs.extend(
            _split_doc_articles(
                full_text=path.read_text(encoding="utf-8"),
                source=path.name,
                policy_ref=match.group(1),
            )
        )
    return docs


# 整份政策全文缓存：同一文件被多条命中时只读一次盘
_FULL_TEXT_CACHE: dict[str, str] = {}


def load_policy_full(source: str) -> str:
    """按 source 文件名取某政策的整份全文（报告"查看完整条文"展开用）。

    分条后检索命中是"具体条文"（IndexDoc.text），但报告仍应能展开整份政策；
    source 即入库时的文件名（如 P-01_预付款比例.md），文件不存在返回空串。
    """
    if source in _FULL_TEXT_CACHE:
        return _FULL_TEXT_CACHE[source]
    path = POLICY_DIR / Path(source).name
    try:
        text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        text = ""
    _FULL_TEXT_CACHE[source] = text
    return text


class MemoryStore:
    """进程内向量检索：文本 → 向量后存内存，查询按余弦相似度取 top-k。"""

    def __init__(self, embedding_model=None):
        # embedding_model 可注入，默认用 DashScope 模型
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
            #入库时归一化向量   
            self._vectors.append(_normalize(vec))

    def similarity_search(self, query: str, k: int = 2) -> list[PolicyHit]:
        """把 query 向量化，与库内全部向量算余弦相似度取前 k。"""
        q = _normalize(self._embedding.embed_query(query)) #query 归一化
        scored = [
            (doc, _cosine(q, vec)) # 计算余弦相似度
            for doc, vec in zip(self._docs, self._vectors)
        ]
        scored.sort(key=lambda item: item[1], reverse=True) # 按相似度降序排序
        # 取top-k
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
        """用一句话向量化探测维度(DashScope qwen3.7 = 1024)。"""
        return len(self._embedding.embed_query("测试"))

    def _ensure_collection(self) -> None:
        """建集合 + 索引并加载；已存在则跳过（幂等）。"""
        from pymilvus import DataType

        # 分支：集合已存在 → 无需重建 schema
        if self.client.has_collection(self.collection_name):
            return
        dim = self._probe_dim()

        #幂等建表：若集合已存在则跳过
        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("pk", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("policy_ref", DataType.VARCHAR, max_length=16)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        self.client.create_collection(self.collection_name, schema=schema)

        # 建 HNSW + COSINE 索引并加载，检索才能命中。语义上的相似度索引。
        index = self.client.prepare_index_params()
        index.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",

            #M=16:每个节点的最大连接数,越大召回越好，但内存越高
            #efConstruction=128:构建时的搜索宽度,越大索引质量越好但建索引越慢
            params={"M": 16, "efConstruction": 128},
        )
        self.client.create_index(self.collection_name, index_params=index)
        self.client.load_collection(self.collection_name)

    def insert(self, docs: list[IndexDoc]) -> None:
        """向量化 + 入库 + flush; 集合已有数据时跳过(防重复累积)。"""
        self._ensure_collection()
        count = self.client.get_collection_stats(self.collection_name).get("row_count", 0)
        # 分支：已有数据 → 不再重复灌入（可手动清集合后重灌）
        if count > 0:
            print(f"{self.collection_name} 已有 {count} 条，跳过导入")
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
        """query 向量化后在 Milvus 检索 top-k, 返回带政策编号的命中。"""
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


def _tokenize(text: str) -> list[str]:
    """中文分词（jieba）→ BM25 词元：丢弃空白，统一小写。

    jieba 首次导入会构建词典缓存（约 0.6s）且带 SyntaxWarning，故惰性导入并静音。
    易错点：中文单字虚词（的/了/和…）几乎每篇都有，不过滤会让"无关查询"也拿到
    非零 BM25 分并挤进 RRF 候选池，故按停用词表剔除。
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba

    return [
        tok.strip().lower()
        for tok in jieba.lcut(text or "")
        if tok.strip() and tok.strip() not in _BM25_STOPWORDS
    ]


# BM25 停用词：中文虚词/标点类单字（保留"不得/超过"这类有判定意义的词）
_BM25_STOPWORDS = {
    "的", "了", "和", "与", "或", "等", "是", "在", "为", "及", "以", "对", "就",
    "也", "都", "而", "并", "着", "被", "把", "之", "其", "该", "本", "上", "下",
    "，", "。", "、", "；", "：", "（", "）", "%", "％",
}


class BM25Index:
    """政策条文的 BM25 索引（与向量库共用同一批 IndexDoc，检索单元一致）。"""

    def __init__(self, docs: list[IndexDoc]):
        from rank_bm25 import BM25Okapi  # 惰性导入：只有走混合检索才需要

        self.docs = [d for d in docs if d.text.strip()]
        self._bm25 = BM25Okapi([_tokenize(d.text) for d in self.docs]) if self.docs else None

    def search(self, query: str, k: int = 10) -> list[PolicyHit]:
        """BM25 检索：按词元重叠打分排序，取前 k（无索引时返回空）。"""
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        order = sorted(range(len(self.docs)), key=lambda i: scores[i], reverse=True)
        return [
            PolicyHit(
                policy_ref=self.docs[i].policy_ref,
                source=self.docs[i].source,
                text=self.docs[i].text,
                score=float(scores[i]),
            )
            for i in order[:k]
            # 这种情况是：0 分（无任何词元重叠）→ 不进入候选，避免污染 RRF 融合
            if scores[i] > 0
        ]


def rrf_fuse(hit_lists: list[list[PolicyHit]], k: int = 60, top_k: int = 3) -> list[PolicyHit]:
    """RRF（Reciprocal Rank Fusion）融合多路检索结果。

    公式：score(d) = Σ 1/(k + rank_i(d))；k 默认 60（业界常用，弱化头部单路主导）。
    同一检索单元（source+text）在多路命中只累加分数一次每路；返回 top_k，
    score 写 RRF 分（越大越靠前；与余弦分不同量纲，仅用于排序）。
    """
    fused: dict[tuple[str, str], float] = {}
    meta: dict[tuple[str, str], PolicyHit] = {}
    for hits in hit_lists:
        for rank, hit in enumerate(hits, start=1):
            key = (hit.source, hit.text)
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
            meta.setdefault(key, hit)
    order = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    return [
        PolicyHit(
            policy_ref=meta[key].policy_ref,
            source=meta[key].source,
            text=meta[key].text,
            score=round(score, 6),
        )
        for key, score in order[:top_k]
    ]


class HybridRetriever:
    """混合检索：向量召回 + BM25 召回，经 RRF 融合（政策规模变大后提升引用准确率）。"""

    def __init__(self, store, docs: list[IndexDoc], pool: int = 10, rrf_k: int = 60):
        self.store = store  # MemoryStore / MilvusStore（提供 similarity_search）
        self.bm25 = BM25Index(docs)
        self.pool = pool  # 每路召回条数（融合前候选池）
        self.rrf_k = rrf_k

    def search(self, query: str, k: int = 3) -> list[PolicyHit]:
        """两路召回（各 pool 条）→ RRF 融合取前 k。"""
        vector_hits = self.store.similarity_search(query, k=self.pool)
        bm25_hits = self.bm25.search(query, k=self.pool)
        return rrf_fuse([vector_hits, bm25_hits], k=self.rrf_k, top_k=k)


# 混合检索器进程内缓存：向量库与 BM25 索引构建成本高，避免每次查询重灌
_HYBRID_CACHE: dict[str, HybridRetriever] = {}


def _get_hybrid(backend: str | None, embedding_model) -> HybridRetriever:
    """按后端取（并缓存）混合检索器；检索单元固定为 load_policies() 的全量条文。"""
    # 缓存键带上"是否注入自定义 embedding"，避免测试假向量与真实向量互相污染
    key = f"{backend or 'auto'}|{'custom' if embedding_model is not None else 'default'}"
    if key not in _HYBRID_CACHE:
        store = get_store(backend=backend, embedding_model=embedding_model)
        docs = load_policies()
        if isinstance(store, MemoryStore) and store.doc_count == 0:
            store.insert(docs)
        elif isinstance(store, MilvusStore):
            store.insert(docs)
        _HYBRID_CACHE[key] = HybridRetriever(store, docs)
    return _HYBRID_CACHE[key]


# ---- 向量工具（纯函数）----


def _normalize(vec: list[float]) -> list[float]:
    """向量归一化：让点积=余弦相似度"""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    """两个已归一化向量的余弦相似度（点积）。"""
    return sum(x * y for x, y in zip(a, b))


def _milvus_reachable(uri: str, timeout: float = 2.0) -> bool:
    """快速探测 Milvus 端口可达性(socket 级，不触发 pymilvus 长超时)。

    分支依据:uri 形如 http://host:port; 解析失败或连不上都按不可达处理。
    """
    parsed = urlparse(uri)   # 解析 http://host:port
    host, port = parsed.hostname, parsed.port or 19530
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_store(backend: str | None = None, embedding_model=None) -> MemoryStore | MilvusStore:
    """store 工厂: 按配置选实现, Milvus 不可达自动退回内存。

    backend 取值:memory / milvus / auto(默认读 .env RETRIEVAL_BACKEND)。
    """
    backend = backend or settings.retrieval_backend
    # 分支：显式 memory → 直接用内存，不探测外部依赖
    if backend == "memory":
        return MemoryStore(embedding_model=embedding_model)
    # 分支：milvus 或 auto 且端口可达 → 用真库
    if backend == "milvus" or (backend == "auto" and _milvus_reachable(settings.milvus_uri)):
        return MilvusStore(uri=settings.milvus_uri, embedding_model=embedding_model)
    # 分支：auto 但 Milvus 不可达 → 退回内存
    print(f"⚠️ Milvus({settings.milvus_uri}) 不可达，退回内存检索")
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
    mode: str | None = None,
) -> list[PolicyHit]:
    """按问题检索政策条目，返回带 policy_ref 的命中（供 rules/LLM 引用）。

    mode：hybrid=向量+BM25 经 RRF 融合（默认，读 settings.retrieval_mode）/
    vector=仅向量（回退与对比用）。
    """
    mode = mode or settings.retrieval_mode
    # 分支：混合检索 → 复用进程内缓存的检索器（向量库+BM25 索引只建一次）
    if mode == "hybrid":
        return _get_hybrid(backend, embedding_model).search(query, k=k)
    # 分支：纯向量（历史行为，保留作对比基线）
    store = get_store(backend=backend, embedding_model=embedding_model)
    # 分支：内存库为空 → 先灌政策再检索；Milvus 的 insert 自带幂等跳过
    if isinstance(store, MemoryStore) and store.doc_count == 0:
        store.insert(load_policies())
    elif isinstance(store, MilvusStore):
        store.insert(load_policies())
    return store.similarity_search(query, k=k)
