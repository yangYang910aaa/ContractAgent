"""FastAPI 入口

启动方式（二选一）：
    python backend/app/main.py            # 直接跑本文件即起服务（见 __main__）
    python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接运行本文件时（python backend/app/main.py），Python 把 backend/app 当
# sys.path[0]，找不到 backend 包——把仓库根插进 path（模块方式启动时跳过）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, Request

from backend.app import llm
from backend.app.config import settings
from backend.app.graph import ReviewRunner
from backend.app.routes_tasks import router as tasks_router
from backend.app.store_pg import PgPersistence
from backend.app.tasks import TaskManager

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


@asynccontextmanager
async def lifespan(app:FastAPI)->AsyncIterator[None]:
    """应用生命周期：服务停止时干净关闭 TaskManager 的 worker 线程。
    启动侧无需操作——manager 在 create_app() 中创建时已自动启动 worker
    此处只负责 uvicorn 收到退出信号时
    调用 shutdown()，避免 worker 守护线程残留。
    """
    yield
    #停止worker
    app.state.manager.shutdown()
    # Postgres 持久化连接池释放(存在时); 内存模式无此对象
    persistence = getattr(app.state, "persistence", None)
    if persistence is not None:
        persistence.close()


def _build_default_manager() -> tuple[TaskManager, PgPersistence | None]:
    """服务默认 manager: DATABASE_URL 配置则用 Postgres 持久化, 否则全内存兜底。

    Postgres 路径在此显式构造(PgPersistence 建表 + checkpointer setup);
    连不上数据库会直接抛错——持久化失败宁可起不来, 也不静默退回内存丢任务。
    """
    if not settings.database_url:
        return TaskManager(), None
    persistence = PgPersistence()
    runner = ReviewRunner(store=persistence.store, checkpointer=persistence.checkpointer)
    return TaskManager(runner=runner), persistence

def create_app(manager: TaskManager | None = None) -> FastAPI:
    """建 FastAPI 应用：任务管理器挂在 app.state, 路由经 request 取用。"""
    app = FastAPI(
        title="供应商合同智能审核 Agent",
        description="上传采购合同 → Agent 结构化抽取 → 规则+政策库审查 → 风险报告(HITL)。",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.state.persistence = None
    if manager is None:
        manager, app.state.persistence = _build_default_manager()
    app.state.manager = manager
    app.include_router(tasks_router)

    @app.get("/")
    def root() -> dict:
        """入口页：返回应用名与文档/健康检查地址，便于快速调试。"""
        return {"app": "ContractAgent", "docs": "/docs", "health": "/api/health"}

    @app.get("/api/health")
    def health(request: Request) -> dict:
        """健康检查：报告配置就绪状态与外部依赖可达性。"""
        store = request.app.state.manager.runner.store
        return {
            "status": "ok",
            "config": llm.check_env_ready(),
            "database": "postgres" if request.app.state.persistence else "memory",
            "database_ready": store.ping(),
            "queued_tasks": len(store.list_records()),
        }

    return app


app = create_app()


if __name__ == "__main__":
    # 直接运行：本文件即服务入口（--reload 不开：加载 .env/模型工厂较重，
    # 开发时可用 -m uvicorn --reload 代替，见模块 docstring）
    import uvicorn

    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000)
