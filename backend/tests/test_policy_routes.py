"""政策库起稿接口（只读起稿）测试：上传/粘贴 → 摘要 → 详情，全部离线（假检索器）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import policy_assistant, routes_policy
from backend.app.main import create_app
from backend.app.policy_corpus import corpus_units
from backend.app.policy_rag import POLICY_DIR, MemoryStore, PolicyHit
from backend.app.tasks import TaskManager

_DRAFT_TEXT = """# 采购合同审核制度 · 细则 P-29：示例政策

文件编号：P-29　　版本：V1.0　　生效日期：2026年10月1日
归口部门：集团采购管理中心
适用范围：本集团对外签署的采购合同。

## 第一条 制度目的

示例。

## 第二条 预付款比例上限

预付款合计不得超过合同总额的 40%。
"""

_EXISTING_POLICY = """# 采购合同审核制度 · 细则 P-01：预付款比例

文件编号：P-01　　版本：V1.0　　生效日期：2026年9月1日
归口部门：集团采购管理中心
适用范围：本集团对外签署的采购合同。

## 第一条 预付款上限

预付款合计不得超过合同总额的 30%。
"""


class _FakeEmbeddings:
    """离线假向量：只要确定性与非零，够内存库算相似度即可。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        vec = [0.0] * 8
        for char in text or "空":
            vec[ord(char) % 8] += 1.0
        return vec


def _retriever(query: str, k: int) -> list[PolicyHit]:
    """假检索器：预付款那条给高分（触发高度重叠），其余给低分。"""
    # 这种情况是：命中的既有条文阈值与新政策不一样 → 顺带触发"阈值不一致"
    if "预付款" in query:
        return [PolicyHit(policy_ref="P-01", source="P-01_预付款比例.md", text="预付款不得超过合同总额的 30%。", score=0.83)]
    return [PolicyHit(policy_ref="P-05", source="P-05_知识产权与争议解决.md", text="成果归属。", score=0.42)]


@pytest.fixture()
def library(tmp_path: Path) -> tuple[Path, MemoryStore]:
    """临时语料目录（含一份既有政策）+ 已按该目录灌好的内存库：入库接口的离线依赖。"""
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "P-01_预付款比例.md").write_text(_EXISTING_POLICY, encoding="utf-8")
    store = MemoryStore(embedding_model=_FakeEmbeddings())
    store.add_docs(corpus_units(policy_dir=policy_dir))
    return policy_dir, store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch, library) -> TestClient:
    """app：草稿目录/语料目录/检索库全指到临时与内存对象——不连向量库、不花调用、不写真语料。"""
    drafts = tmp_path / "_drafts"
    policy_dir, store = library
    monkeypatch.setattr(policy_assistant, "DRAFTS_DIR", drafts)
    monkeypatch.setattr(policy_assistant, "_default_retriever", lambda: _retriever)
    monkeypatch.setattr(routes_policy, "INCOMING_DIR", drafts / "_incoming")
    monkeypatch.setattr(routes_policy, "_policy_dir", lambda: policy_dir)
    monkeypatch.setattr(routes_policy, "_publish_store", lambda: store)
    return TestClient(create_app(manager=TaskManager(worker=False)))


def test_paste_text_creates_draft_and_detail(client: TestClient) -> None:
    """粘贴正文起稿：摘要给出编号/条文数/重叠分级/冲突，详情回读草稿与清单。"""
    resp = client.post("/api/policy/drafts", data={"text": _DRAFT_TEXT, "name": "P-29_示例政策.md"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ref"] == "P-29"
    assert body["articles"] == 2
    assert body["missing"] == []
    assert body["overlap"] == {"high": 1, "medium": 0, "low": 1}
    assert any(item["kind"] == "阈值不一致" for item in body["conflicts"])

    detail = client.get(f"/api/policy/drafts/{body['draft_id']}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["source"] == "P-29_示例政策.md"
    assert data["parsed"]["articles"][1]["heading"].startswith("第二条")
    assert "文件编号：P-29" in data["draft"]
    assert "政策库当前版本：PL-" in data["checklist"]
    levels = {item["article"]: item["level"] for item in data["overlaps"]}
    assert levels["第二条 预付款比例上限"] == "high"
    assert data["overlaps"][1]["hits"][0]["policy_ref"] == "P-01"
    assert set(data["files"]) == {"meta.json", "overlaps.json", "checklist.md", "P-29_draft.md"}


def test_upload_file_creates_draft(client: TestClient, tmp_path: Path) -> None:
    """上传文件起稿：走解析器取正文，原件不留库（临时文件用完即删）。"""
    resp = client.post(
        "/api/policy/drafts",
        files={"file": ("P-29_示例政策.md", _DRAFT_TEXT.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ref"] == "P-29"
    detail = client.get(f"/api/policy/drafts/{body['draft_id']}").json()
    assert "预付款合计不得超过合同总额的 40%" in detail["draft"]
    # 暂存目录只作中转：起稿完成后不留任何原件
    incoming = routes_policy.INCOMING_DIR
    assert not incoming.exists() or not any(incoming.iterdir())


def test_draft_rejects_bad_input(client: TestClient) -> None:
    """输入不合法：不支持的格式、空文件、两个入参都空都要 400 而不是落空草稿。"""
    bad_suffix = client.post(
        "/api/policy/drafts", files={"file": ("P-29.jpg", b"\xff\xd8\xff", "image/jpeg")}
    )
    assert bad_suffix.status_code == 400 and "不支持" in bad_suffix.json()["detail"]

    empty_file = client.post(
        "/api/policy/drafts", files={"file": ("P-29.md", b"   \n", "text/markdown")}
    )
    assert empty_file.status_code == 400

    nothing = client.post("/api/policy/drafts", data={"text": "  "})
    assert nothing.status_code == 400


def test_draft_detail_rejects_unknown_and_traversal(client: TestClient) -> None:
    """详情接口：查不到的编号与想跳出草稿目录的编号都回 404。"""
    assert client.get("/api/policy/drafts/不存在的草稿").status_code == 404
    assert client.get("/api/policy/drafts/..").status_code == 404
    assert client.get("/api/policy/drafts/..%2F..%2Fbackend").status_code == 404


def test_draft_does_not_touch_policy_library(client: TestClient) -> None:
    """只读边界：起稿只在草稿目录留产物，语料目录一个文件都不动。"""
    before = sorted(path.name for path in POLICY_DIR.glob("*.md"))
    assert client.post("/api/policy/drafts", data={"text": _DRAFT_TEXT}).status_code == 200
    assert sorted(path.name for path in POLICY_DIR.glob("*.md")) == before


def test_apply_previews_then_publishes(client: TestClient, library) -> None:
    """入库两步：预览只算计划，确认后才落盘同步，页面随后能回看到已入库记录。"""
    policy_dir, _store = library
    draft_id = client.post(
        "/api/policy/drafts", data={"text": _DRAFT_TEXT, "name": "P-29_示例政策.md"}
    ).json()["draft_id"]
    assert client.get(f"/api/policy/drafts/{draft_id}").json()["suggested_file"] == "P-29_示例政策.md"

    preview = client.post(f"/api/policy/drafts/{draft_id}/apply", json={"confirm": False}).json()
    plan = preview["plan"]
    assert preview["applied"] is False
    assert plan["file_name"] == "P-29_示例政策.md" and plan["blockers"] == [] and plan["missing"] == []
    assert plan["write_units"] == 3 and plan["delete_units"] == 0 and plan["exists"] is False
    assert not (policy_dir / plan["file_name"]).exists()  # 预览不落盘

    done = client.post(f"/api/policy/drafts/{draft_id}/apply", json={"confirm": True}).json()
    assert done["applied"] is True and done["written"] == 3 and done["check_ok"] is True
    assert done["version"] == plan["next_version"] and done["version"] != done["previous_version"]
    assert (policy_dir / plan["file_name"]).is_file()

    # 回看：草稿记上了入库结果，政策库现状里多出这一份
    assert client.get(f"/api/policy/drafts/{draft_id}").json()["applied"]["file_name"] == plan["file_name"]
    status = client.get("/api/policy/library").json()
    assert status["version"] == done["version"]
    assert {doc["ref"] for doc in status["documents"]} == {"P-01", "P-29"}


def test_apply_blocks_duplicate_ref_and_missing_meta(client: TestClient) -> None:
    """撞号硬阻止；缺元信息默认阻止、显式放行才入库。"""
    # 拿现有政策的正文起稿、却想用另一个文件名入库 → 编号撞号
    dup = client.post("/api/policy/drafts", data={"text": _EXISTING_POLICY}).json()["draft_id"]
    dup_resp = client.post(
        f"/api/policy/drafts/{dup}/apply", json={"confirm": True, "file_name": "P-01_另一份.md"}
    )
    assert dup_resp.status_code == 400 and "编号重复" in dup_resp.json()["detail"]

    thin = client.post(
        "/api/policy/drafts",
        data={
            "text": "# 采购合同审核制度 · 细则 P-31：无元信息\n\n## 第一条 正文\n\n一句话。\n",
            # 正文里没写「文件编号」→ 靠来源名认编号，元信息缺项仍应被拦下
            "name": "P-31_无元信息.md",
        },
    ).json()["draft_id"]
    blocked = client.post(f"/api/policy/drafts/{thin}/apply", json={"confirm": True})
    assert blocked.status_code == 400 and "元信息缺失" in blocked.json()["detail"]
    allowed = client.post(
        f"/api/policy/drafts/{thin}/apply", json={"confirm": True, "allow_missing_meta": True}
    )
    assert allowed.status_code == 200 and allowed.json()["applied"] is True


def test_apply_rejects_bad_file_name_and_unknown_draft(client: TestClient) -> None:
    """文件名形态不对拦下；草稿不存在回 404。"""
    draft_id = client.post("/api/policy/drafts", data={"text": _DRAFT_TEXT}).json()["draft_id"]
    bad = client.post(f"/api/policy/drafts/{draft_id}/apply", json={"confirm": True, "file_name": "../x.md"})
    assert bad.status_code == 400 and "文件名不合法" in bad.json()["detail"]
    assert client.post("/api/policy/drafts/没有这份/apply", json={"confirm": True}).status_code == 404
