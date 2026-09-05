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
from backend.app.routes_tasks import router as tasks_router
from backend.app.tasks import TaskManager


def create_app(manager: TaskManager | None = None) -> FastAPI:
    """建 FastAPI 应用：任务管理器挂在 app.state，路由经 request 取用。"""
    app = FastAPI(
        title="供应商合同智能审核 Agent",
        description="上传采购合同 → Agent 结构化抽取 → 规则+政策库审查 → 风险报告(HITL)。",
        version="0.3.0",
    )
    app.state.manager = manager or TaskManager()
    app.include_router(tasks_router)

    @app.get("/")
    def root() -> dict:
        """入口页：返回应用名与文档/健康检查地址，便于快速调试。"""
        return {"app": "ContractAgent", "docs": "/docs", "health": "/api/health"}

    @app.get("/api/health")
    def health(request: Request) -> dict:
        """健康检查：报告配置就绪状态与外部依赖可达性。"""
        return {
            "status": "ok",
            "config": llm.check_env_ready(),
            "queued_tasks": len(request.app.state.manager.runner.store.list_records()),
        }

    return app


app = create_app()


if __name__ == "__main__":
    # 直接运行：本文件即服务入口（--reload 不开：加载 .env/模型工厂较重，
    # 开发时可用 -m uvicorn --reload 代替，见模块 docstring）
    import uvicorn

    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000)
