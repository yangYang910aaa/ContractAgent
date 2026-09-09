"""任务队列管理器

TaskManager: FIFO 队列 + 单 worker 守护线程。submit()登记入队,worker 循环取任务 → 置 processing → 调 `ReviewRunner.start()`。
单 worker 是刻意的 —— 防并发打爆 LLM 配额。worker=False 供离线测试

用户上传合同 → [TaskManager] → N个Worker线程抢任务 → 调AI审查 → 写回状态
                    ↑
              FIFO队列排队
              限流自动重试
              有界并发控制
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

    判定口径: langchain/OpenAI 兼容异常常带 status_code(429/5xx)；其余按
    报错文案里的关键词兜底。合同本身的解析/校验失败不含这些词，不会误重试。
    """
    status = getattr(exc, "status_code", None)
    # 这种情况是：显式带 429/5xx 状态码 → 直接判瞬时
    if isinstance(status, int) and status in (429, 500, 502, 503, 504):
        return True
    #把异常文案转小写，再判断是否包含_TRANSIENT_MARKERS中的关键词
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


class TaskManager:
    """上传任务 FIFO 队列:submit 入队,N 路 worker 有界并发处理。"""

    def __init__(
        self,
        runner: ReviewRunner | None = None, #AI审查引擎实例
        worker: bool = True, #是否启动后台worker线程
        workers: int | None = None, #并发数
    ) -> None:
        self.runner = runner or ReviewRunner() 
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker_threads: list[threading.Thread] = []
        self._stop = threading.Event()
        # 并发度：显式 workers 优先（测试传小值），否则取 .env 的 REVIEW_WORKERS
        self.worker_count = workers if workers is not None else settings.review_workers
        # 启动恢复：服务上次中断残留的 pending/processing 统一标 error，提示重新上传
        # （持久化 store 生效；内存 store 每次进程为空，mark_interrupted 是 no-op）
        self.runner.store.mark_interrupted()
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

        任务状态由 runner.start 内部收尾(gate/done/error)；这里只在最终失败
        时兜底标 error, 并把"瞬时错误重试过几次"写进错误文案方便排查。
        """
        while not self._stop.is_set():
            try:
                thread_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue  # 队列空 → 继续等（stop 未触发）
            record = self.runner.store.get(thread_id)
            if record is None:
                continue
            #立刻改状态为processing,这样前端轮询时能看到"处理中"
            self.runner.store.update(thread_id, status="processing")
            attempt = 0
            last_exc: Exception | None = None
            # 循环：瞬时错误（限流/超时/5xx）指数退避重试，其余/超次数直接失败
            while attempt < MAX_ATTEMPTS:
                attempt += 1
                try:
                    # 按任务登记的审查模式起跑：double 会在 rules 后多跑一路盲审
                    self.runner.start(
                        record.source,
                        thread_id=thread_id,
                        review_mode=record.review_mode,
                    )
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

 

    def register(self, source: str, review_mode: str = "single") -> str:
        """只创建任务记录，不入队，返回 thread_id；登记审查模式供 worker 起跑用。"""
        thread_id = self.runner.store.create(source).thread_id
        # 模式不是登记必填信息，用 update 补写（内存/PG 白名单都支持该字段）
        self.runner.store.update(thread_id, review_mode=review_mode)
        return thread_id

    def enqueue(self, thread_id: str) -> None:
        """把已登记任务放进队列 (register 与 enqueue 之间可更新 source)"""
        if self.runner.store.get(thread_id) is None:
            raise ValueError(f"任务不存在: {thread_id}")
        self._queue.put(thread_id)

    def submit(self, source: str, review_mode: str = "single") -> str:
        """登记任务并入队，返回 thread_id (worker 会按序处理)"""
        thread_id = self.register(source, review_mode=review_mode)
        self.enqueue(thread_id)
        return thread_id
    
    def run_one(self, thread_id: str) -> dict:
        """同步跑完一个任务 (离线测试/单发调试用; worker=False 时调用)"""
        record = self.runner.store.get(thread_id)
        if record is None:
            raise ValueError(f"任务不存在：{thread_id}")
        self.runner.store.update(thread_id, status="processing")
        try:
            state = self.runner.start(
                record.source,
                thread_id=thread_id,
                review_mode=record.review_mode,
            )
        except Exception as exc:
            self.runner.store.update(thread_id, status="error", error=f"审查失败：{exc}")
            state = {}
        return state

    def shutdown(self) -> None:
        """停全部 worker (测试/应用退出用)。join 只等 2s:正在跑的 LLM 调用
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
