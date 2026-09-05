"""任务队列管理器（2026-09-05 起支持有界并发）。

职责：上传的合同进入 FIFO 队列，由 N 路 worker 线程（默认 settings.review_workers
=2）同时送进 ReviewRunner（LangGraph 图 + MemorySaver）。N 是"有界"的——
真实 LLM 抽取并发过大会打爆配额/限流，N 按实测限流调（env REVIEW_WORKERS）。
429/超时等瞬时错误自动退避重试（与"合同本身审查失败"区分），仍失败才标
error；任务状态都记在 runner.store(ThreadStore)里，路由层只读写登记簿。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from backend.app.config import settings
from backend.app.graph import ReviewRunner

# 瞬时失败自动重试：最多尝试 3 次（2 次重试），退避按 2s 指数增长（2/4/…）
MAX_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 2.0

# 判"瞬时/限流类"错误的依据：HTTP 状态码（若带上）或报错文案关键词
_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "insufficient_quota",
    "quota exceeded",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "暂时不可用",
)


def _is_transient(exc: Exception) -> bool:
    """判断异常是否属"限流/超时/服务抖动"（可重试），而不是合同本身问题。

    判定口径：langchain/OpenAI 兼容异常常带 status_code（429/5xx）；其余按
    报错文案里的关键词兜底。合同本身的解析/校验失败不含这些词，不会误重试。
    """
    status = getattr(exc, "status_code", None)
    # 这种情况是：显式带 429/5xx 状态码 → 直接判瞬时
    if isinstance(status, int) and status in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


class TaskManager:
    """上传任务 FIFO 队列：submit 入队，N 路 worker 有界并发处理。"""

    def __init__(
        self,
        runner: ReviewRunner | None = None,
        worker: bool = True,
        workers: int | None = None,
    ) -> None:
        self.runner = runner or ReviewRunner()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker_threads: list[threading.Thread] = []
        self._stop = threading.Event()
        # 并发度：显式 workers 优先（测试传小值），否则取 .env 的 REVIEW_WORKERS
        self.worker_count = workers if workers is not None else settings.review_workers
        # 分支：worker=True（服务运行时）→ 立即起 N 路循环消费队列
        if worker:
            self._start_workers()

    def _start_workers(self) -> None:
        """启动 N 路 worker 守护线程（服务生命周期内常驻）。"""
        for i in range(max(1, self.worker_count)):
            thread = threading.Thread(target=self._loop, name=f"review-worker-{i}", daemon=True)
            thread.start()
            self._worker_threads.append(thread)

    def _loop(self) -> None:
        """worker 主循环：取 thread_id → 置 processing → 跑图（带限流重试）。

        任务状态由 runner.start 内部收尾（gate/done/error）；这里只在最终失败
        时兜底标 error，并把"瞬时错误重试过几次"写进错误文案方便排查。
        """
        while not self._stop.is_set():
            try:
                thread_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue  # 队列空 → 继续等（stop 未触发）
            record = self.runner.store.get(thread_id)
            if record is None:
                continue
            self.runner.store.update(thread_id, status="processing")
            attempt = 0
            last_exc: Exception | None = None
            # 循环：瞬时错误（限流/超时/5xx）指数退避重试，其余/超次数直接失败
            while attempt < MAX_ATTEMPTS:
                attempt += 1
                try:
                    self.runner.start(record.source, thread_id=thread_id)
                    last_exc = None
                    break
                except Exception as exc:  # 图/LLM 异常，先判断能否重试
                    last_exc = exc
                    # 这种情况是：非瞬时错误（合同本身/解析问题）→ 不再重试
                    if not _is_transient(exc):
                        break
                    # 这种情况是：瞬时错误且还有重试次数 → 退避后重试
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
            if last_exc is not None:
                # 重试过才在文案里注明，方便区分"限流"与"合同问题"
                retried = f"（重试 {attempt - 1} 次后仍失败）" if attempt > 1 else ""
                self.runner.store.update(thread_id, status="error", error=f"审查失败{retried}：{last_exc}")

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
        """停全部 worker (测试/应用退出用)。join 只等 2s：正在跑的 LLM 调用
        可能未结束，靠 daemon 保证进程退出不悬挂（任务状态在内存，重启即失）。"""
        self._stop.set()
        for thread in self._worker_threads:
            if thread.is_alive():
                thread.join(timeout=2.0)


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
