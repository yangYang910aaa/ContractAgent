"""TaskManager 并发改造测试：N 路 worker 互不串状态 + 瞬时错误退避重试。"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backend.app import tasks as tasks_mod
from backend.app.graph import ReviewRunner
from backend.app.schemas import ContractModel
from backend.app.store import ThreadStore
from backend.app.tasks import TaskManager, wait_until_settled


def _zero_risk_model(text: str) -> ContractModel:
    """gov_goods 品类 + 必填齐全 → 规则零风险（pass 直通，便于并发断言）。"""
    return ContractModel(
        contract_kind="gov_goods",
        buyer=f"MARK:{text[:24]}",
        supplier="供应商",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 3, 9),
        total_amount=Decimal("1000"),
        currency="人民币",
    )


def test_parallel_workers_isolate_tasks(tmp_path: Path) -> None:
    """2 路 worker 同时跑 2 份不同文件：各自抽取结果不串号、都 done。"""
    (tmp_path / "a.md").write_text("MARK-A 电子产品采购合同正文", encoding="utf-8")
    (tmp_path / "b.md").write_text("MARK-B 办公耗材采购合同正文", encoding="utf-8")
    manager = TaskManager(
        runner=ReviewRunner(extractor=lambda text: _zero_risk_model(text)),
        worker=True,
        workers=2,
    )
    try:
        tid_a = manager.submit(str(tmp_path / "a.md"))
        tid_b = manager.submit(str(tmp_path / "b.md"))
        assert wait_until_settled(manager, tid_a, timeout=20) is not None
        assert wait_until_settled(manager, tid_b, timeout=20) is not None
        ra = manager.runner.store.get(tid_a)
        rb = manager.runner.store.get(tid_b)
        assert ra.status == "done" and rb.status == "done"
        buyer_a = ra.report["extracted"]["buyer"]
        buyer_b = rb.report["extracted"]["buyer"]
        # 各自只含自己的文件标记，绝不互相串
        assert "MARK-A" in buyer_a and "MARK-B" not in buyer_a
        assert "MARK-B" in buyer_b and "MARK-A" not in buyer_b
    finally:
        manager.shutdown()


class _FlakyRunner:
    """假 runner：store 用真 ThreadStore；start 前 N 次抛瞬时错误后成功。"""

    def __init__(self, fail_times: int = 2, exc: Exception | None = None) -> None:
        self.store = ThreadStore()
        self.calls = 0
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("429 rate limit: 触发限流")

    def start(self, source: str, thread_id: str | None = None, text: str | None = None) -> dict:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        self.store.update(thread_id, status="done", report={"grade": "pass"})
        return {}


def test_transient_failure_retried_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 瞬时错误：自动退避重试，最终成功（不把限流当合同失败）。"""
    monkeypatch.setattr(tasks_mod, "RETRY_BACKOFF_BASE", 0.01)
    manager = TaskManager(runner=_FlakyRunner(fail_times=2), worker=True, workers=1)
    try:
        tid = manager.submit("sample_x.md")
        assert wait_until_settled(manager, tid, timeout=20) is not None
        record = manager.runner.store.get(tid)
        # 前 2 次限流失败 + 第 3 次成功 = 3 次调用，任务最终 done
        assert manager.runner.calls == 3
        assert record.status == "done"
    finally:
        manager.shutdown()


def test_nontransient_failure_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """合同本身问题（非限流）：不重试，直接标 error。"""
    monkeypatch.setattr(tasks_mod, "RETRY_BACKOFF_BASE", 0.01)
    runner = _FlakyRunner(fail_times=99, exc=ValueError("抽取字段非法"))
    manager = TaskManager(runner=runner, worker=True, workers=1)
    try:
        tid = manager.submit("sample_y.md")
        assert wait_until_settled(manager, tid, timeout=20) is not None
        record = manager.runner.store.get(tid)
        assert manager.runner.calls == 1  # 只试了一次
        assert record.status == "error"
        assert "审查失败" in record.error
    finally:
        manager.shutdown()
