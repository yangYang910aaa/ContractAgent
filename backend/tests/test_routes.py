"""FastAPI 任务路由（Phase 3）测试：上传/队列/详情/审批，全部离线（假抽取）。"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import BASE_DIR
from backend.app.graph import ReviewRunner
from backend.app.main import create_app
from backend.app.policy_rag import PolicyHit
from backend.app.schemas import ContractModel, PaymentTerm
from backend.app.tasks import TaskManager


def _defect_model() -> ContractModel:
    """缺陷模型：质保 6 个月 + 违约金 1.5% → 两条 high，必停闸口。"""
    return ContractModel(
        contract_kind="gov_goods",
        buyer="晨光实验中学",
        supplier="星海校服服饰有限公司",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 9, 30),
        total_amount=Decimal("198400"),
        currency="人民币",
        payment_schedule=[PaymentTerm(name="一次性付清", amount=Decimal("198400"), percent=100.0)],
        penalty_rate=1.5,
        warranty_months=6,
    )


def _fake_retriever(query: str) -> list[PolicyHit]:
    ref = "P-02" if "质保" in query else "P-03"
    return [PolicyHit(policy_ref=ref, source=f"{ref}.md", text=f"{ref} 条文", score=0.9)]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """app：worker=False + 假抽取；上传目录指到临时路径，测完不留文件。"""
    import backend.app.routes_tasks as routes

    monkeypatch.setattr(routes, "UPLOAD_DIR", tmp_path)
    runner = ReviewRunner(extractor=lambda text: _defect_model(), retriever=_fake_retriever)
    manager = TaskManager(runner=runner, worker=False)
    app = create_app(manager=manager)
    return TestClient(app)


def _sample_bytes() -> bytes:
    sample = BASE_DIR / "data" / "contracts" / "sample_07_学生校服采购合同_质保过短_违约金畸高.md"
    return sample.read_bytes()


def _sample_docx_bytes() -> bytes:
    """样本 docx 二进制：U2 上传 docx → 解析原文 → /source 返回全文。"""
    sample = BASE_DIR / "data" / "contracts" / "docx" / "sample_07_学生校服采购合同_质保过短_违约金畸高.docx"
    return sample.read_bytes()


def test_upload_then_processing_then_gate_and_approve(client: TestClient) -> None:
    """上传 → 入队 pending → run_one 到 gate → approve → done 报告带审批记录。"""
    resp = client.post("/api/tasks", files={"file": ("contract.md", _sample_bytes(), "text/markdown")})
    assert resp.status_code == 200
    tid = resp.json()["thread_id"]
    manager = client.app.state.manager
    assert manager.runner.store.get(tid).status == "pending"
    # 离线手动跑（worker=False 不自动处理）
    manager.run_one(tid)
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["status"] == "gate"
    assert len(detail["gate_payload"]["high_risks"]) == 2
    # 放行
    resp = client.post(f"/api/tasks/{tid}/approve", json={"note": "复核后放行"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["report"]["grade"] == "fail"
    assert body["report"]["approval"]["action"] == "approved"
    assert body["report"]["approval"]["reviewer_note"] == "复核后放行"


def test_upload_reject_flow(client: TestClient) -> None:
    """打回路径：reject + 原因写进报告。"""
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    client.app.state.manager.run_one(tid)
    resp = client.post(f"/api/tasks/{tid}/reject", json={"note": "质保期不足，打回重谈"})
    assert resp.status_code == 200
    approval = resp.json()["report"]["approval"]
    assert approval["action"] == "rejected"
    assert approval["reviewer_note"] == "质保期不足，打回重谈"


def test_edit_patch_reruns_and_second_gate(client: TestClient) -> None:
    """编辑重审：修质保 24 → 违约金仍 high → 二次闸口，再放行完成。"""
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    manager = client.app.state.manager
    manager.run_one(tid)
    resp = client.post(f"/api/tasks/{tid}/edit", json={"patches": {"warranty_months": 24}, "note": "质保改 24"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "gate"
    risk = resp.json()["gate_payload"]["high_risks"][0]
    assert risk["risk_type"] == "penalty_rate_too_high"
    resp = client.post(f"/api/tasks/{tid}/approve", json={"note": "违约金同意协商"})
    assert resp.json()["report"]["approval"]["action"] == "approved"


def test_upload_unsupported_suffix_400(client: TestClient) -> None:
    """非白名单格式应 400 并给出原因。"""
    resp = client.post("/api/tasks", files={"file": ("a.xyz", b"x", "application/octet-stream")})
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


def test_approve_non_gate_returns_409(client: TestClient) -> None:
    """对还没到闸口（pending）的任务审批应 409，防误操作。"""
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    resp = client.post(f"/api/tasks/{tid}/approve", json={"note": ""})
    assert resp.status_code == 409


def test_get_missing_task_404(client: TestClient) -> None:
    assert client.get("/api/tasks/not-exist").status_code == 404


def test_list_tasks_summary(client: TestClient) -> None:
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    client.app.state.manager.run_one(tid)
    body = client.get("/api/tasks").json()
    tasks = {t["thread_id"]: t for t in body["tasks"]}
    assert tasks[tid]["status"] == "gate"
    assert tasks[tid]["source"] == "c.md"
    assert tasks[tid]["risk_count"] == 2  # gate 时 = 待审 high 数


def test_demo_enqueues_internal_samples(client: TestClient) -> None:
    """demo 接口直接入队内置合成样本（免上传），worker=False 时停在 pending。"""
    resp = client.post("/api/tasks/demo", json={"count": 2})
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 2
    assert all(t["source"].startswith("sample_") for t in tasks)
    # 手动跑完第一份：假抽取器返回缺陷模型 → 停在闸口
    manager = client.app.state.manager
    manager.run_one(tasks[0]["thread_id"])
    detail = client.get(f"/api/tasks/{tasks[0]['thread_id']}").json()
    assert detail["status"] == "gate"
    assert detail["risk_count"] == 2


def test_demo_count_out_of_range_422(client: TestClient) -> None:
    assert client.post("/api/tasks/demo", json={"count": 99}).status_code == 422


# ---- U2：查看原合同（/source 全文+条款块、/file 原文件）----


def test_source_after_run_returns_text_and_blocks(client: TestClient) -> None:
    """上传 md → 跑完到闸口 → /source 返回解析全文与章节块（原文查看数据源）。"""
    tid = client.post("/api/tasks", files={"file": ("校服合同.md", _sample_bytes(), "text/markdown")}).json()[
        "thread_id"
    ]
    # 跑完前（pending）source_text 还没落库 → 空文本
    before = client.get(f"/api/tasks/{tid}/source").json()
    assert before["text"] == ""
    client.app.state.manager.run_one(tid)
    body = client.get(f"/api/tasks/{tid}/source").json()
    assert body["name"] == "校服合同.md"
    assert body["suffix"] == ".md"
    assert body["kind"] == "upload"
    assert body["file_available"] is True
    assert "学生校服" in body["text"]
    # 章节式样本：块 0 是前言，后面按「一、二、…」切
    assert body["blocks"][0]["title"] == "前言"
    assert any(b["title"].startswith("一、") for b in body["blocks"])


def test_source_demo_sample_kind_and_file_download(client: TestClient) -> None:
    """demo 样本 → kind=sample；/file 能取回原 md 二进制（下载/对照用）。"""
    resp = client.post("/api/tasks/demo", json={"count": 1})
    tid = resp.json()["tasks"][0]["thread_id"]
    client.app.state.manager.run_one(tid)
    src = client.get(f"/api/tasks/{tid}/source").json()
    assert src["kind"] == "sample"
    assert src["suffix"] == ".md"
    assert "电子元件" in src["text"] or "办公设备" in src["text"]
    file_resp = client.get(f"/api/tasks/{tid}/file")
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"].startswith("text/markdown")


def test_source_docx_upload_parses_full_text(client: TestClient) -> None:
    """docx 上传 → 解析出的章节全文能从 /source 取到（pdf/docx 预览的基础）。"""
    tid = client.post("/api/tasks", files={"file": ("校服合同.docx", _sample_docx_bytes(), "application/octet-stream")}).json()[
        "thread_id"
    ]
    client.app.state.manager.run_one(tid)
    body = client.get(f"/api/tasks/{tid}/source").json()
    assert body["suffix"] == ".docx"
    assert "校服" in body["text"]
    file_resp = client.get(f"/api/tasks/{tid}/file")
    assert file_resp.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_pdf_file_served_inline_for_preview(client: TestClient) -> None:
    """pdf 原文件必须是 inline（浏览器内嵌预览），不能是 attachment（否则变下载）。"""
    pdf = BASE_DIR / "data" / "contracts" / "pdf" / "sample_07_学生校服采购合同_质保过短_违约金畸高.pdf"
    tid = client.post("/api/tasks", files={"file": ("校服合同.pdf", pdf.read_bytes(), "application/pdf")}).json()[
        "thread_id"
    ]
    client.app.state.manager.run_one(tid)
    resp = client.get(f"/api/tasks/{tid}/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    # content-disposition 应为 inline；attachment 会让 iframe 预览变下载
    assert resp.headers["content-disposition"].startswith("inline")


def test_source_and_file_missing_task_404(client: TestClient) -> None:
    """不存在的任务：/source 与 /file 都回 404。"""
    assert client.get("/api/tasks/not-exist/source").status_code == 404
    assert client.get("/api/tasks/not-exist/file").status_code == 404
