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
