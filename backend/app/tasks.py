"""任务队列管理器。

职责：上传的合同进入 FIFO 队列，由单 worker 线程逐个送进 ReviewRunner
(LangGraph 图 + MemorySaver)，单 worker 是刻意的——真实 LLM 抽取并发
打爆配额/成本，先来先审；任务状态都记在 runner.store(ThreadStore)里，
路由层只读写登记簿，不做业务。

"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from backend.app.graph import ReviewRunner


class TaskManager:
    """上传任务 FIFO 队列: submit 入队，单 worker 顺序处理。"""

    def __init__(self, runner: ReviewRunner | None = None, worker: bool = True) -> None:
        self.runner = runner or ReviewRunner()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 分支：worker=True（服务运行时）→ 立即起单线程循环消费队列
        if worker:
            self._start_worker()

    def _start_worker(self) -> None:
        """启动单 worker 守护线程（服务生命周期内常驻）。"""
        self._worker_thread = threading.Thread(target=self._loop, name="review-worker", daemon=True)
        self._worker_thread.start()

    def _loop(self) -> None:
        """worker 主循环：取 thread_id → 置 processing → 跑图(runner 自己收尾状态)"""
        while not self._stop.is_set():
            try:
                thread_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue  # 队列空 → 继续等（stop 未触发）
            record = self.runner.store.get(thread_id)
            if record is None:
                continue
            self.runner.store.update(thread_id, status="processing")
            try:
                self.runner.start(record.source, thread_id=thread_id)
            except Exception as exc:  # 图/LLM 异常 → 任务标 error，不拖垮队列
                self.runner.store.update(thread_id, status="error", error=f"审查失败：{exc}")

    def submit(self, source: str) -> str:
        """登记任务并入队，返回 thread_id (worker 会按序处理)"""
        thread_id = self.register(source)
        self.enqueue(thread_id)
        return thread_id

    def register(self, source: str) -> str:
        """只登记不入队 (上传路径依赖 thread_id 时先建任务后补 source)"""
        return self.runner.store.create(source).thread_id

    def enqueue(self, thread_id: str) -> None:
        """把已登记任务放进队列 (register 与 enqueue 之间可更新 source)"""
        if self.runner.store.get(thread_id) is None:
            raise ValueError(f"任务不存在: {thread_id}")
        self._queue.put(thread_id)

    def run_one(self, thread_id: str) -> dict:
        """同步跑完一个任务 (离线测试/单发调试用; worker=False 时调用)"""
        record = self.runner.store.get(thread_id)
        if record is None:
            raise ValueError(f"任务不存在：{thread_id}")
        self.runner.store.update(thread_id, status="processing")
        try:
            state = self.runner.start(record.source, thread_id=thread_id)
        except Exception as exc:
            self.runner.store.update(thread_id, status="error", error=f"审查失败：{exc}")
            state = {}
        return state

    def shutdown(self) -> None:
        """停 worker (测试/应用退出用)"""
        self._stop.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)


def wait_until_settled(manager: TaskManager, thread_id: str, timeout: float = 120.0) -> dict | None:
    """轮询任务直到离开 processing/pending (测试与单发脚本复用)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = manager.runner.store.get(thread_id)
        if record and record.status not in ("pending", "processing"):
            return manager.runner.store.get(thread_id).report or {
                "status": record.status,
                "error": record.error,
            }
        time.sleep(0.5)
    return None
