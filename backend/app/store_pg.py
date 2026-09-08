"""Postgres 持久化: 任务登记簿(PgThreadStore) + 图检查点(PostgresSaver)。

让服务重启后任务列表/报告/待审批闸口仍在, 审批可继续(决策 D24)。
PgThreadStore 与内存 ThreadStore 接口一致, 路由与队列层无感切换。
连接串取 settings.database_url; 易错点见 docs/问题与踩坑记录.md(Postgres 持久化落地)。
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from backend.app.config import settings
from backend.app.store import TaskRecord, new_thread_id

# 业务表 DDL(登记簿的库形态; 与 TaskRecord 字段一一对应, jsonb 存闸口载荷/报告)
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS contract_tasks (
    thread_id    text PRIMARY KEY,
    source       text NOT NULL,
    name         text NOT NULL DEFAULT '',
    source_text  text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'pending',
    gate_payload jsonb,
    report       jsonb,
    error        text NOT NULL DEFAULT '',
    created_at   double precision NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
)
"""

# TaskRecord 字段 → 列名(update 白名单; 加字段必须同步这里, 防止任意列名注入)
_FIELD_COLUMNS: dict[str, str] = {
    "source": "source",
    "name": "name",
    "source_text": "source_text",
    "status": "status",
    "gate_payload": "gate_payload",
    "report": "report",
    "error": "error",
}
# 需要 json 序列化后入 jsonb 列的字段(其余按标量直接写)
_JSON_COLUMNS = {"gate_payload", "report"}


class PgThreadStore:
    """Postgres 版任务登记簿: 接口与内存 ThreadStore 一致(见 store.py)。

    每个方法从共享连接池取一条短连接执行; 行读成 dict 后直接还原 TaskRecord。
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._ensure_table()

    def _ensure_table(self) -> None:
        """幂等建表(启动/构造时执行, 重复建不报错)。"""
        with self._pool.connection() as conn:
            conn.execute(_TABLE_DDL)

    def _row_to_record(self, row: dict) -> TaskRecord:
        """DB 行(dict) → TaskRecord; jsonb 列 psycopg 已自动解成 dict。"""
        return TaskRecord(**row)

    def create(self, source: str) -> TaskRecord:
        """生成 thread_id 并插入一行, 返回登记记录(状态默认 pending)。"""
        record = TaskRecord(thread_id=new_thread_id(), source=source, name=source)
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO contract_tasks (thread_id, source, name, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (record.thread_id, record.source, record.name, record.status, record.created_at),
            )
        return record

    def get(self, thread_id: str) -> TaskRecord | None:
        """按 thread_id 取任务记录; 不存在返回 None。"""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT thread_id, source, name, source_text, status, gate_payload, "
                "report, error, created_at FROM contract_tasks WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def update(self, thread_id: str, **changes: Any) -> TaskRecord | None:
        """更新白名单字段(status/gate_payload/report/error/source...), 返回最新记录。"""
        # 这种情况是: 传了白名单外的字段 → 说明代码漏登记, 直接报错防静默丢字段
        unknown = set(changes) - set(_FIELD_COLUMNS)
        if unknown:
            raise KeyError(f"PgThreadStore 不支持的字段: {sorted(unknown)}")
        if not changes:
            return self.get(thread_id)
        assignments = []
        values: list[Any] = []
        for field, value in changes.items():
            column = _FIELD_COLUMNS[field]
            assignments.append(f"{column} = %s")
            # 分支: dict/list → json 序列化入 jsonb; None → NULL; 其余标量直写
            values.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
        values.append(thread_id)
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE contract_tasks SET {', '.join(assignments)} WHERE thread_id = %s",
                values,
            )
        return self.get(thread_id)

    def list_records(self) -> list[TaskRecord]:
        """全部任务(按创建时间倒序, 供队列/列表页展示)。"""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT thread_id, source, name, source_text, status, gate_payload, "
                "report, error, created_at FROM contract_tasks ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def delete(self, thread_id: str) -> bool:
        """删除任务行; 返回是否真的删到(不存在返回 False)。"""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM contract_tasks WHERE thread_id = %s", (thread_id,)
            )
        return cur.rowcount > 0

    def clear(self) -> None:
        """清空任务表(测试收尾/清库用)。"""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM contract_tasks")

    def mark_interrupted(self, reason: str = "") -> int:
        """启动恢复: 服务中断残留的 pending/processing 任务统一标 error。

        不做自动重跑, 提示用户重新上传。
        gate/done/error 原样保留(列表可见、gate 可继续审批)。
        """
        reason = reason or "服务重启中断, 请重新上传"
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE contract_tasks SET status = 'error', error = %s "
                "WHERE status IN ('pending', 'processing')",
                (reason,),
            )
        return cur.rowcount

    def ping(self) -> bool:
        """连通性探针(health 用): 能执行 SELECT 1 即认为可用。"""
        try:
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False


class PgPersistence:
    """Postgres 持久化运行时: 共享连接池上的登记簿 + 图检查点。

    服务入口(main.create_app)在 settings.database_url 配置时创建, 把
    store/checkpointer 注入 ReviewRunner; 进程退出前 close() 释放连接池。
    连接池 autocommit + dict_row(PostgresSaver 运行所需)。
    """

    def __init__(self, database_url: str | None = None) -> None:
        self.url = database_url or settings.database_url
        # 这种情况是: 没配连接串却要走持久化 → 明确报错, 不静默退回内存
        if not self.url:
            raise ValueError("缺少 DATABASE_URL(.env 未配置 Postgres 连接串)")
        self.pool = ConnectionPool(
            conninfo=self.url,
            min_size=1,
            max_size=8,
            open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        self.store = PgThreadStore(self.pool)
        self.checkpointer = PostgresSaver(self.pool)
        self.checkpointer.setup()  # 幂等: 建 checkpoints/checkpoint_writes 等表

    def close(self) -> None:
        """释放连接池(服务退出/测试收尾)。"""
        self.pool.close()
