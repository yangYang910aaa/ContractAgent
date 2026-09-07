"""Postgres 持久化集成测试(需本地 PG + .env 配置 DATABASE_URL, 未配置则跳过)。

覆盖: PgThreadStore CRUD/jsonb/排序/启动恢复(mark_interrupted), 以及
核心价值点——闸口任务在"换一个连接/进程"后仍能恢复审批(LangGraph 检查点落库)。
内存路径(默认)由既有 test_tasks/test_routes/test_graph 覆盖, 不依赖数据库。
"""

from datetime import date
from decimal import Decimal

import pytest

from backend.app.config import settings
from backend.app.graph import ReviewRunner, build_review_graph
from backend.app.schemas import ContractModel, PaymentTerm
from backend.app.store_pg import PgPersistence
from langgraph.types import Command

pytestmark = pytest.mark.skipif(
    not settings.database_url,
    reason=".env 未配置 DATABASE_URL, 跳过 Postgres 集成测试",
)


@pytest.fixture()
def pg() -> PgPersistence:
    """每个测试独立建连(顺带幂等建表), 收尾释放连接池。"""
    persistence = PgPersistence()
    persistence.store.clear()  # 共享同一张表: 每用例前清空, 防跨用例数据残留
    yield persistence
    persistence.close()


def _gate_model() -> ContractModel:
    """全部核心字段齐全、仅违约金畸高(5%>1%)的模型 → 规则必出 high 停闸口。"""
    return ContractModel(
        contract_kind="enterprise_goods",
        buyer="甲科技有限公司",
        supplier="乙供应商",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 3, 9),
        total_amount=Decimal("1000000"),
        currency="人民币",
        payment_schedule=[PaymentTerm(name="一次付清", amount=Decimal("1000000"), percent=100.0)],
        penalty_rate=5.0,
        liability_cap=100.0,
        warranty_months=24,
        confidentiality_months=24,
        ip_ownership="定制成果知识产权归甲方所有",
        governing_law="中华人民共和国法律",
    )


def test_create_get_update_jsonb_roundtrip(pg: PgPersistence) -> None:
    """create → update(report/gate_payload/source_text) → get 还原一致。"""
    store = pg.store
    record = store.create("data/x.md")
    assert record.status == "pending" and record.name == "data/x.md"
    report = {"grade": "fail", "risks": [{"risk_type": "penalty_rate_too_high"}]}
    payload = {"ask": "请审批", "high_risks": []}
    updated = store.update(
        record.thread_id,
        status="gate",
        gate_payload=payload,
        report=report,
        source_text="合同全文……",
    )
    assert updated is not None and updated.status == "gate"
    assert updated.gate_payload == payload  # jsonb 回读应与写入相等
    assert updated.report == report
    assert updated.source_text == "合同全文……"


def test_list_records_sorted_desc(pg: PgPersistence) -> None:
    """列表按创建时间倒序(与内存 ThreadStore 口径一致)。"""
    import time

    store = pg.store
    a = store.create("a.md").thread_id
    time.sleep(0.01)
    b = store.create("b.md").thread_id
    ids = [r.thread_id for r in store.list_records()]
    assert ids == [b, a]
    assert store.get("不存在的id") is None


def test_mark_interrupted_flags_stale_only(pg: PgPersistence) -> None:
    """启动恢复: 只把 pending/processing 标 error, gate/done/error 原样保留。"""
    store = pg.store
    stale = store.create("stale.md")
    store.update(stale.thread_id, status="processing")
    waiting = store.create("wait.md")
    store.update(waiting.thread_id, status="gate", gate_payload={"ask": "审"})
    done = store.create("done.md")
    store.update(done.thread_id, status="done", report={"grade": "pass"})
    n = store.mark_interrupted("服务重启中断, 请重新上传")
    assert n == 1
    assert store.get(stale.thread_id).status == "error"
    assert "服务重启中断" in store.get(stale.thread_id).error
    assert store.get(waiting.thread_id).status == "gate"  # 可继续审批
    assert store.get(done.thread_id).status == "done"


def test_clear_and_unknown_field(pg: PgPersistence) -> None:
    """clear 清空; update 白名单外字段直接报错(防字段漏登记静默丢失)。"""
    store = pg.store
    store.create("a.md")
    store.clear()
    assert store.list_records() == []
    with pytest.raises(KeyError):
        store.update("whatever", not_a_field=True)


def test_gate_survives_new_connection_and_resume(pg: PgPersistence) -> None:
    """核心价值: 闸口任务在全新连接/进程后仍可恢复审批并出报告。"""
    graph1 = build_review_graph(
        extractor=lambda text: _gate_model(),
        retriever=lambda q: [],
        checkpointer=pg.checkpointer,
    )
    config = {"configurable": {"thread_id": "pg-e2e-gate"}, "recursion_limit": 30}
    state = graph1.invoke(
        {"source": "sample.md", "text": "完整已填正文", "review_mode": "single"},
        config,
    )
    assert state.get("__interrupt__"), "应停在人工审批闸口"

    # 模拟重启: 新建连接池 + 新 graph, 同一 thread_id 从数据库恢复检查点
    pg2 = PgPersistence()
    try:
        graph2 = build_review_graph(
            extractor=lambda text: _gate_model(),
            retriever=lambda q: [],
            checkpointer=pg2.checkpointer,
        )
        resumed = graph2.invoke(
            Command(resume={"action": "approved", "note": "重启后人工放行"}),
            config,
        )
    finally:
        pg2.close()
    report = resumed["report"]
    assert report["grade"] == "fail"
    assert report["approval"]["action"] == "approved"
    assert resumed["approvals"][-1]["reviewer_note"] == "重启后人工放行"


def test_runner_with_pg_store_reports_status(pg: PgPersistence) -> None:
    """ReviewRunner 注入 Pg store/saver 后, start→gate 状态落在登记簿(jsonb 可读)。"""
    runner = ReviewRunner(
        extractor=lambda text: _gate_model(),
        retriever=lambda q: [],
        store=pg.store,
        checkpointer=pg.checkpointer,
    )
    state = runner.start("sample.md", text="完整已填正文")
    tid = runner.last_thread_id
    record = pg.store.get(tid)
    assert record is not None and record.status == "gate"
    assert state.get("__interrupt__")
    # 清理本用例数据, 不影响其他用例计数
    pg.store.update(tid, status="done", report={"grade": "fail"})
