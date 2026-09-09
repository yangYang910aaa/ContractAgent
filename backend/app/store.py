"""任务/线程登记。
ThreadStore: 进程内任务登记簿 (thread_id → TaskRecord),
记录来源文件、状态(pending/processing/gate/done/error)、闸口载荷、最终报告、解析出的原文全文。线程安全（加锁）。
这是路由层读任务状态的唯一数据源
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_thread_id() -> str:
    """生成短 thread_id (uuid4 前 12 位)"""
    #这个 ID 同时作为 MemorySaver checkpointer 的 thread key，AI 图的对话历史按此 ID 隔离
    return uuid.uuid4().hex[:12]


@dataclass
class TaskRecord:
    """一个审核任务在登记簿里的记录。"""

    thread_id: str  # LangGraph checkpointer 线程键
    source: str  #worker/文件下载时用。 来源文件路径/标签。上传场景先占位后补全
    name: str = ""  #前端列表/详情页时。  展示名（原始文件名；上传后与 source 落盘路径分离）
    review_mode: str = "single"  # 审查模式（single/double）；worker 起跑时读它决定是否盲审
    source_text: str = ""  #get_task_source()路由。  解析出的合同全文
    status: str = "pending"  #前端+_resume_or_409。  pending=抽取中 / gate=待人工审批 / done=完成 / error=失败
    gate_payload: dict | None = None  #审批页前端。 待审风险摘要(仅gate状态有值)
    report: dict | None = None  #详情页前端。 最终报告（JSON 可序列化）
    error: str = ""  #前端错误提示。 失败原因（抽取/图执行异常）
    created_at: float = field(default_factory=time.time)  #list_records排序。 创建时间戳（秒）


class ThreadStore:
    """进程内任务登记簿: thread_id → TaskRecord (线程安全)。"""

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    def create(self, source: str) -> TaskRecord:
        """生成id+创建记录+写入字典"""
        record = TaskRecord(thread_id=new_thread_id(), source=source, name=source)
        # 用with self._lock包裹,确保读-改-写整个序列是原子的
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

    def delete(self, thread_id: str) -> bool:
        """删除一条任务记录; 不存在返回 False。"""
        with self._lock:
            return self._records.pop(thread_id, None) is not None

    def clear(self) -> None:
        """清空登记簿 """
        with self._lock:
            self._records.clear()

    def mark_interrupted(self, reason: str = "") -> int:
        """启动恢复：内存登记簿进程重启即空，无历史任务可处理（接口与 Pg 实现对齐）。"""
        return 0

    def ping(self) -> bool:
        """连通性探针（health 用）：内存实现恒可用。"""
        return True
