"""任务/线程登记。

职责：给每个审核任务分配 thread_id (LangGraph checkpointer 键），登记来源文件、
状态、闸口待审信息与最终报告—— FastAPI 路由据此做列表/详情/审批接口。
持久化说明: checkpointer 用 MemorySaver (进程内存), 服务重启即失; Postgres
checkpointer 见 docs/问题与踩坑记录.md,
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_thread_id() -> str:
    """生成短 thread_id (uuid4 前 12 位)"""
    return uuid.uuid4().hex[:12]


@dataclass
class TaskRecord:
    """一个审核任务在登记簿里的记录。"""

    thread_id: str  # LangGraph checkpointer 线程键
    source: str  # 来源文件路径/标签
    name: str = ""  # 展示名（原始文件名；上传后与 source 落盘路径分离）
    source_text: str = ""  # 解析出的合同全文（U2：任务页查看原合同用；图跑完 parse 后落库）
    status: str = "pending"  # pending=抽取中 / gate=待人工审批 / done=完成 / error=失败
    gate_payload: dict | None = None  # 闸口待审载荷（风险摘要），审批页展示用
    report: dict | None = None  # 最终报告（JSON 可序列化）
    error: str = ""  # 失败原因（抽取/图执行异常）
    created_at: float = field(default_factory=time.time)  # 创建时间戳（秒）


class ThreadStore:
    """进程内任务登记簿: thread_id → TaskRecord (线程安全)。"""

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    def create(self, source: str) -> TaskRecord:
        """登记一个新任务并返回记录 (thread_id 自动分配)。"""
        record = TaskRecord(thread_id=new_thread_id(), source=source, name=source)
        with self._lock:
            self._records[record.thread_id] = record
        return record

    def get(self, thread_id: str) -> TaskRecord | None:
        """按 thread_id 取任务记录；不存在返回 None。"""
        with self._lock:
            return self._records.get(thread_id)

    def update(self, thread_id: str, **changes: Any) -> TaskRecord | None:
        """就地更新记录字段 (status/gate_payload/report/error…), 返回更新后记录。"""
        with self._lock:
            record = self._records.get(thread_id)
            if record is None:
                return None
            for key, value in changes.items():
                setattr(record, key, value)
            return record

    def list_records(self) -> list[TaskRecord]:
        """全部任务（按创建时间倒序，供队列/列表页展示）。"""
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)

    def clear(self) -> None:
        """清空登记簿 """
        with self._lock:
            self._records.clear()
