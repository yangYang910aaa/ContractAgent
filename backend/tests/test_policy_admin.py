"""政策库语料指纹、文件 ↔ 索引一致性核对与按份同步的离线用例（假向量，不连向量库）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app import policy_admin, policy_corpus
from backend.app.pipeline import build_report
from backend.app.policy_rag import IndexDoc, MemoryStore
from backend.app.schemas import ContractModel

_POLICY_A = """# 采购合同审核制度 · 细则 P-01：预付款管理

文件编号：P-01　　版本：V2.0　　生效日期：2026年9月5日
适用范围：货物类采购合同。

## 第二条 预付款比例上限

预付款合计不得超过合同总额的 30%。
"""

_POLICY_B = """# 采购合同审核制度 · 细则 P-02：质量保证期

文件编号：P-02　　版本：V1.0　　生效日期：2026年9月5日

## 第二条 质保期限

质保期不得少于 12 个月。
"""


class FakeEmbeddings:
    """确定性假向量：按字符 ord 累加进定长桶后归一化，只用于把条目塞进内存库。"""

    DIM = 32

    def _vec(self, text: str) -> list[float]:
        vector = [0.0] * self.DIM
        for char in text:
            vector[ord(char) % self.DIM] += 1.0
        norm = sum(x * x for x in vector) ** 0.5 or 1.0
        return [x / norm for x in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture()
def corpus_dir(tmp_path: Path) -> Path:
    """两文件的小语料：改一份/删一份/加一份都能直接构造出核对差异。"""
    directory = tmp_path / "policies"
    directory.mkdir()
    (directory / "P-01_预付款比例.md").write_text(_POLICY_A, encoding="utf-8")
    (directory / "P-02_质量保证期.md").write_text(_POLICY_B, encoding="utf-8")
    return directory


def _store_with_corpus(corpus_dir: Path) -> MemoryStore:
    """把磁盘语料灌进内存库，得到一个与文件一致的索引。"""
    store = MemoryStore(embedding_model=FakeEmbeddings())
    store.add_docs(policy_corpus.corpus_units(policy_dir=corpus_dir))
    return store


def test_fingerprint_stable_and_version_changes_with_content(corpus_dir: Path) -> None:
    first = policy_corpus.corpus_fingerprint(policy_dir=corpus_dir)
    assert first["version"] == policy_corpus.corpus_fingerprint(policy_dir=corpus_dir)["version"]
    assert first["files"] == 2
    assert {doc["ref"] for doc in first["documents"]} == {"P-01", "P-02"}

    # 改一个字 → 文件哈希与总版本号都要变，且只影响这一份文件
    target = corpus_dir / "P-01_预付款比例.md"
    target.write_text(_POLICY_A.replace("30%", "20%"), encoding="utf-8")
    after = policy_corpus.corpus_fingerprint(policy_dir=corpus_dir)
    assert after["version"] != first["version"]
    hashes = {doc["ref"]: doc["sha256"] for doc in after["documents"]}
    assert hashes["P-01"] != {doc["ref"]: doc["sha256"] for doc in first["documents"]}["P-01"]
    assert hashes["P-02"] == {doc["ref"]: doc["sha256"] for doc in first["documents"]}["P-02"]


def test_compare_ok_then_reports_drift(corpus_dir: Path) -> None:
    store = _store_with_corpus(corpus_dir)
    state = policy_corpus.compare_corpus_and_index(store=store, policy_dir=corpus_dir)
    assert state["ok"] is True
    assert state["disk_units"] == state["index_units"]
    assert all(item["same"] for item in state["files_detail"])

    # 磁盘改一份 → 该文件标为不一致（旧条文在库里、新条文不在），其余文件仍一致
    (corpus_dir / "P-01_预付款比例.md").write_text(
        _POLICY_A.replace("30%", "20%"), encoding="utf-8"
    )
    drifted = policy_corpus.compare_corpus_and_index(store=store, policy_dir=corpus_dir)
    assert drifted["ok"] is False
    detail = {item["source"]: item for item in drifted["files_detail"]}
    assert detail["P-01_预付款比例.md"]["same"] is False
    assert detail["P-01_预付款比例.md"]["missing"] >= 1
    assert detail["P-01_预付款比例.md"]["extra"] >= 1
    assert detail["P-02_质量保证期.md"]["same"] is True


def test_compare_reports_missing_and_orphan(corpus_dir: Path) -> None:
    store = MemoryStore(embedding_model=FakeEmbeddings())
    store.add_docs(policy_corpus.corpus_units(policy_dir=corpus_dir))
    # 索引里多出一份磁盘上没有的文件 → 孤儿行（删掉语料文件后库里残留的旧单元）
    store.add_docs([IndexDoc(text="旧条文", source="P-09_已删除.md", policy_ref="P-09")])
    state = policy_corpus.compare_corpus_and_index(store=store, policy_dir=corpus_dir)
    assert state["ok"] is False
    assert state["orphan_sources"] == ["P-09_已删除.md"]


def test_unit_sync_plan_only_touches_changed_units(corpus_dir: Path) -> None:
    """增量口径精确到单元：改一条条文只重写那一条，同文件里没变的条文不动。"""
    store = _store_with_corpus(corpus_dir)
    units = policy_corpus.corpus_units(policy_dir=corpus_dir)
    plan = policy_admin.plan_unit_sync(units, store.iter_rows())
    assert plan["write"] == [] and plan["delete"] == []

    (corpus_dir / "P-02_质量保证期.md").write_text(
        _POLICY_B.replace("12 个月", "24 个月"), encoding="utf-8"
    )
    plan = policy_admin.plan_unit_sync(
        policy_corpus.corpus_units(policy_dir=corpus_dir), store.iter_rows()
    )
    # 只写"改过的那一条"：P-02 的文件头单元正文没变，不该被重写
    assert {unit.source for unit in plan["write"]} == {"P-02_质量保证期.md"}
    assert len(plan["write"]) == 1
    assert "24 个月" in plan["write"][0].text
    # 旧条文一条，按业务键精确删除
    assert len(plan["delete"]) == 1
    assert plan["index_source"][plan["delete"][0]] == "P-02_质量保证期.md"


def test_unit_sync_restores_consistency_and_touches_only_changed(corpus_dir: Path) -> None:
    store = _store_with_corpus(corpus_dir)
    # 动两份：改 P-01 正文，另伪造一份磁盘上已无的孤儿行；P-02 保持不变做对照
    store.add_docs([IndexDoc(text="旧条文", source="P-07_已删除.md", policy_ref="P-07")])
    (corpus_dir / "P-01_预付款比例.md").write_text(
        _POLICY_A.replace("30%", "20%"), encoding="utf-8"
    )
    before_untouched = [row for row in store.iter_rows() if row["source"] == "P-02_质量保证期.md"]

    plan = policy_admin.plan_unit_sync(
        policy_corpus.corpus_units(policy_dir=corpus_dir), store.iter_rows()
    )
    assert store.add_docs(plan["write"]) == 1  # 只有改过的那一条要重写
    assert store.delete_units(plan["delete"]) == 2  # 旧条文 + 孤儿行

    after = policy_corpus.compare_corpus_and_index(store=store, policy_dir=corpus_dir)
    assert after["ok"] is True
    assert after["orphan_sources"] == []
    # 替换后的 P-01 是新正文，旧正文一条不剩（业务键变了：新写一条 + 删掉旧的一条）
    rows = [row for row in store.iter_rows() if row["source"] == "P-01_预付款比例.md"]
    assert "20%" in "".join(row["text"] for row in rows)
    assert "30%" not in "".join(row["text"] for row in rows)
    # 没变化的文件一个单元都没被碰过
    assert [row for row in store.iter_rows() if row["source"] == "P-02_质量保证期.md"] == before_untouched


def test_unit_id_stable_and_rewrite_is_idempotent(corpus_dir: Path) -> None:
    """业务键由内容决定：同内容同主键，重复写入不累积（upsert 语义）。"""
    store = _store_with_corpus(corpus_dir)
    before = store.doc_count
    units = policy_corpus.corpus_units(policy_dir=corpus_dir)
    assert policy_admin.unit_id(units[0].source, units[0].text) == policy_admin.unit_id(
        units[0].source, units[0].text
    )
    store.add_docs(units)  # 再灌一遍完全相同的内容
    assert store.doc_count == before


def test_cli_check_and_sync_exit_codes(corpus_dir: Path, monkeypatch, capsys) -> None:
    store = _store_with_corpus(corpus_dir)
    monkeypatch.setattr(policy_admin, "get_store", lambda backend=None: store)

    assert policy_admin.main(["--check", "--policy-dir", str(corpus_dir)]) == 0
    assert "一致" in capsys.readouterr().out

    (corpus_dir / "P-01_预付款比例.md").write_text(
        _POLICY_A.replace("30%", "20%"), encoding="utf-8"
    )
    assert policy_admin.main(["--check", "--policy-dir", str(corpus_dir)]) == 1

    # 不带 --yes 只出计划、不改库 → 仍然不一致
    assert policy_admin.main(["--sync", "--policy-dir", str(corpus_dir)]) == 0
    assert "未执行" in capsys.readouterr().out
    assert policy_corpus.compare_corpus_and_index(store=store, policy_dir=corpus_dir)["ok"] is False

    assert policy_admin.main(["--sync", "--yes", "--policy-dir", str(corpus_dir)]) == 0
    assert policy_corpus.compare_corpus_and_index(store=store, policy_dir=corpus_dir)["ok"] is True


def test_fingerprint_cli_prints_version_only(corpus_dir: Path, capsys) -> None:
    assert policy_admin.main(["--fingerprint", "--policy-dir", str(corpus_dir)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == policy_corpus.corpus_fingerprint(policy_dir=corpus_dir)["version"]


def test_report_carries_policy_library() -> None:
    library = policy_corpus.report_policy_library()
    assert library["version"].startswith("PL-")
    assert library["files"] >= 1
    assert all(" " in revision for revision in library["revisions"])
    assert build_report("demo.md", ContractModel(), [], [])["policy_library"] == library
