"""政策入库测试：预览不动库、落盘同步、幂等、撞号拦截、缺项拦截、失败回滚，全部离线。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.policy import publish
from backend.app.policy.corpus import corpus_fingerprint, corpus_units
from backend.app.policy.rag import MemoryStore

_EXISTING = """# 采购合同审核制度 · 细则 P-01：预付款比例

文件编号：P-01　　版本：V1.0　　生效日期：2026年9月1日
归口部门：集团采购管理中心
适用范围：本集团对外签署的采购合同。

## 第一条 预付款上限

预付款合计不得超过合同总额的 30%。
"""

_NEW = """# 采购合同审核制度 · 细则 P-16：解除与善后

文件编号：P-16　　版本：V1.0　　生效日期：2026年10月1日
归口部门：集团采购管理中心
适用范围：本集团对外签署的采购合同。

## 第一条 制度目的

规范解除权的设置与解除后的善后安排。

## 第二条 解除后的结算与返还

解除生效后三十日内完成结算、返还与款项清退。
"""


class _FakeEmbeddings:
    """离线假向量：只要求确定性与非零，不需要像真语义。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        vec = [0.0] * 8
        for char in text or "空":
            vec[ord(char) % 8] += 1.0
        return vec


class _FlakyStore(MemoryStore):
    """写入指定来源时报错的库：用来验证"同步失败要把文件与库退回去"。"""

    def __init__(self, boom_source: str) -> None:
        super().__init__(embedding_model=_FakeEmbeddings())
        self.boom_source = boom_source

    def add_docs(self, docs) -> int:
        # 分支：这批里含"注定失败"的来源 → 模拟写库报错
        if any(doc.source == self.boom_source for doc in docs):
            raise RuntimeError("模拟写库失败")
        return super().add_docs(docs)


@pytest.fixture()
def library(tmp_path: Path) -> tuple[Path, MemoryStore]:
    """临时语料目录（含一份既有政策）+ 已按该目录灌好的内存库。"""
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "P-01_预付款比例.md").write_text(_EXISTING, encoding="utf-8")
    store = MemoryStore(embedding_model=_FakeEmbeddings())
    store.add_docs(corpus_units(policy_dir=policy_dir))
    return policy_dir, store


def test_plan_reports_new_file_without_touching_anything(library) -> None:
    """预览：给出去向与要写/要删的条数，磁盘与库都不动。"""
    policy_dir, store = library
    before = corpus_fingerprint(policy_dir=policy_dir)["version"]
    plan = publish.plan_publish(_NEW, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    assert plan["blockers"] == []
    assert plan["exists"] is False
    assert plan["write_units"] == 3  # 文件头 + 两条条文
    assert plan["delete_units"] == 0
    assert plan["version"] == before and plan["next_version"] != before
    assert not (policy_dir / "P-16_解除与善后.md").exists()
    assert {row["source"] for row in store.iter_rows()} == {"P-01_预付款比例.md"}


def test_publish_writes_file_and_syncs_index(library) -> None:
    """入库：文件落盘、单元写进索引、执行后核对一致、版本号与预览的预估一致。"""
    policy_dir, store = library
    plan = publish.plan_publish(_NEW, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    result = publish.publish(_NEW, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    assert result["applied"] is True and result["updated"] is False
    assert result["written"] == 3 and result["removed"] == 0 and result["check_ok"] is True
    assert result["version"] == plan["next_version"]
    assert (policy_dir / "P-16_解除与善后.md").read_text(encoding="utf-8") == _NEW
    assert "P-16_解除与善后.md" in {row["source"] for row in store.iter_rows()}


def test_publish_is_idempotent(library) -> None:
    """再入一次同样的内容：算出无需同步，不重复写单元。"""
    policy_dir, store = library
    publish.publish(_NEW, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    again = publish.publish(_NEW, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    assert again["written"] == 0 and again["removed"] == 0 and again["check_ok"] is True


def test_publish_updates_existing_file_by_unit(library) -> None:
    """同名文件 = 更新那份政策：只重写变化的那一条，删掉消失的那一条。"""
    policy_dir, store = library
    publish.publish(_NEW, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    edited = _NEW.replace("解除生效后三十日内完成结算、返还与款项清退。", "解除生效后十五日内完成结算、返还与款项清退。")
    plan = publish.plan_publish(edited, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    assert plan["exists"] is True
    assert plan["write_units"] == 1 and plan["delete_units"] == 1
    result = publish.publish(edited, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    assert result["updated"] is True and result["written"] == 1 and result["removed"] == 1
    texts = [row["text"] for row in store.iter_rows() if row["source"] == "P-16_解除与善后.md"]
    assert any("十五日内" in text for text in texts) and not any("三十日内" in text for text in texts)


def test_publish_blocks_duplicate_ref(library) -> None:
    """编号撞号：另一份文件已用 P-01 → 硬阻止（不然检索引用分不清是哪份）。"""
    policy_dir, store = library
    with pytest.raises(publish.PublishBlocked) as caught:
        publish.publish(_EXISTING, "P-01_另一份.md", policy_dir=policy_dir, store=store)
    assert caught.value.reasons[0]["kind"] == "编号重复"
    assert not (policy_dir / "P-01_另一份.md").exists()


def test_publish_blocks_missing_meta_unless_allowed(library) -> None:
    """缺元信息默认拦下；显式放行时才入库。"""
    policy_dir, store = library
    thin = "# 采购合同审核制度 · 细则 P-17：无元信息\n\n## 第一条 只有正文\n\n一句话。\n"
    with pytest.raises(publish.PublishBlocked) as caught:
        publish.publish(thin, "P-17_无元信息.md", policy_dir=policy_dir, store=store)
    assert caught.value.reasons[0]["kind"] == "元信息缺失"
    result = publish.publish(
        thin, "P-17_无元信息.md", policy_dir=policy_dir, store=store, allow_missing_meta=True
    )
    assert result["applied"] is True and (policy_dir / "P-17_无元信息.md").is_file()


def test_publish_rejects_bad_file_name(library) -> None:
    """文件名不合法（带目录、形态不对）时直接拦下，不落盘。"""
    policy_dir, store = library
    for bad in ("../P-16_逃逸.md", "P-16.md", "新政策.md"):
        plan = publish.plan_publish(_NEW, bad, policy_dir=policy_dir, store=store)
        assert plan["blockers"] and plan["blockers"][0]["kind"] == "文件名不合法"
    assert [path.name for path in policy_dir.glob("*")] == ["P-01_预付款比例.md"]


def test_publish_rolls_back_when_sync_fails(tmp_path: Path) -> None:
    """同步中途报错：刚落的文件删掉、库保持原样，错误如实抛出。"""
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "P-01_预付款比例.md").write_text(_EXISTING, encoding="utf-8")
    store = _FlakyStore(boom_source="P-16_解除与善后.md")
    store.add_docs(corpus_units(policy_dir=policy_dir))
    before = corpus_fingerprint(policy_dir=policy_dir)["version"]
    with pytest.raises(RuntimeError):
        publish.publish(_NEW, "P-16_解除与善后.md", policy_dir=policy_dir, store=store)
    assert not (policy_dir / "P-16_解除与善后.md").exists()
    assert corpus_fingerprint(policy_dir=policy_dir)["version"] == before
    assert {row["source"] for row in store.iter_rows()} == {"P-01_预付款比例.md"}
