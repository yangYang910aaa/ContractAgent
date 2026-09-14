"""政策库语料指纹与"文件 ↔ 索引"一致性核对。

政策语料的事实来源是 data/policies/*.md（不进版本库、没有外部台账），所以用内容哈希
当版本：每份文件算一个 sha256，再把「文件名 + 哈希」排序后汇总成一个政策库版本号。
检索侧只读比对（不写库、不改任何检索口径），供命令行核对与报告标注"依据哪一版政策库"。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from backend.app.policy_rag import POLICY_DIR, _split_doc_articles, get_store

# 文件头元信息形如「文件编号：P-01　　版本：V2.0　　生效日期：2026年9月5日」
_META_RE = re.compile(r"(版本|生效日期)\s*[：:]\s*([^\s　]+)")


def _file_sha256(path: Path) -> str:
    """整份文件的字节级 sha256（改一个字符即变，换行/空白的写法差异同样算一次变更）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unit_sha256(text: str) -> str:
    """单个检索单元的正文哈希：比对时只认正文，不认入库顺序与相似度分。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _meta(header: str, key: str) -> str:
    """从文件头取一项元信息（版本/生效日期），取不到返回空串。"""
    found = {name: value for name, value in _META_RE.findall(header)}
    return found.get(key, "")


def _scan(policy_dir: Path | None = None) -> tuple[list[dict], list]:
    """扫一遍语料目录，返回（文件清单, 分条后的检索单元）。

    分条用与入库同一个实现数出来，避免"核对按一种口径、入库按另一种口径"。
    """
    directory = policy_dir or POLICY_DIR
    documents: list[dict] = []
    units: list = []
    for path in sorted(directory.glob("*.md")):
        matched = re.match(r"(P-\d+)", path.name)
        # 分支：文件名不带 P-编号 → 与入库口径一致地跳过（这类文件不入库，也不算进版本）
        if not matched:
            continue
        text = path.read_text(encoding="utf-8")
        articles = _split_doc_articles(
            full_text=text, source=path.name, policy_ref=matched.group(1)
        )
        documents.append(
            {
                "ref": matched.group(1),
                "source": path.name,
                "title": text.splitlines()[0].lstrip("# ").strip() if text.strip() else "",
                "version": _meta(text[:400], "版本"),
                "effective_date": _meta(text[:400], "生效日期"),
                "sha256": _file_sha256(path),
                "units": len(articles),
            }
        )
        units.extend(articles)
    return documents, units


def policy_documents(policy_dir: Path | None = None) -> list[dict]:
    """语料文件清单：编号/标题/版本/生效日期/内容哈希/检索单元数（按文件名排序）。"""
    return _scan(policy_dir)[0]


def corpus_units(policy_dir: Path | None = None) -> list:
    """磁盘语料按入库口径分条后的全部单元（按份重插时取该文件的单元用）。"""
    return _scan(policy_dir)[1]


def corpus_fingerprint(policy_dir: Path | None = None) -> dict:
    """政策库版本号 + 语料清单：任意一份文件增删改，版本号都会变。"""
    documents = policy_documents(policy_dir=policy_dir)
    joined = "\n".join(f"{doc['source']}:{doc['sha256']}" for doc in documents)
    return {
        "version": "PL-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12],
        "files": len(documents),
        "units": sum(doc["units"] for doc in documents),
        "documents": documents,
    }


def report_policy_library(policy_dir: Path | None = None) -> dict:
    """报告里的政策库段：版本 + 份数/条数 + 各份政策的编号与版本号。

    纯读盘、不访问向量库，所以每份报告都能带上，成本可忽略。
    """
    fingerprint = corpus_fingerprint(policy_dir=policy_dir)
    return {
        "version": fingerprint["version"],
        "files": fingerprint["files"],
        "units": fingerprint["units"],
        "revisions": [
            f"{doc['ref']} {doc['version'] or '未标版本'}" for doc in fingerprint["documents"]
        ],
    }


def compare_corpus_and_index(store=None, policy_dir: Path | None = None) -> dict:
    """按「文件名 + 正文哈希」逐单元比对磁盘语料与索引，返回差异清单。

    多重集比差的好处是重复条文、调序都不会被误判成变更；只比正文，不比向量。
    ok=True 表示该版本语料与库内单元一一对应，报告标出的政策库版本才名副其实。
    """
    store = get_store() if store is None else store
    disk = Counter((doc.source, _unit_sha256(doc.text)) for doc in _scan(policy_dir)[1])
    index = Counter(
        (row.get("source", ""), _unit_sha256(row.get("text", "")))
        for row in store.iter_rows()
    )
    missing = disk - index
    extra = index - disk
    disk_by_source = Counter(source for source, _ in disk.elements())
    index_by_source = Counter(source for source, _ in index.elements())
    # 差异按来源文件归并：同步时的最小操作单位就是"一份文件"，逐单元改没有意义
    files: list[dict] = []
    for source in sorted(set(disk_by_source) | set(index_by_source)):
        lost = sum(count for (src, _), count in missing.items() if src == source)
        surplus = sum(count for (src, _), count in extra.items() if src == source)
        files.append(
            {
                "source": source,
                "disk_units": disk_by_source.get(source, 0),
                "index_units": index_by_source.get(source, 0),
                "missing": lost,
                "extra": surplus,
                "same": lost == 0 and surplus == 0,
            }
        )
    return {
        "version": corpus_fingerprint(policy_dir=policy_dir)["version"],
        "files": len(disk_by_source),
        "disk_units": sum(disk.values()),
        "index_units": sum(index.values()),
        "ok": not missing and not extra,
        # 只存在于索引、磁盘上已没有的文件（孤儿行：删掉文件后留下的旧单元）
        "orphan_sources": sorted(set(index_by_source) - set(disk_by_source)),
        "files_detail": files,
    }
