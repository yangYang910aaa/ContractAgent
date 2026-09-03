"""FastAPI 入口（Phase 0 空壳）。

后续 Phase 3 在此挂 upload / queue / report / approval 路由与 SSE。
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.app import llm

app = FastAPI(
    title="供应商合同智能审核 Agent",
    description="上传采购合同 → Agent 结构化抽取 → 规则+政策库审查 → 风险报告（HITL）。",
    version="0.1.0",
)


@app.get("/")
def root() -> dict:
    return {"app": "ContractAgent", "docs": "/docs", "health": "/api/health"}


@app.get("/api/health")
def health() -> dict:
    """健康检查：报告配置就绪状态与外部依赖可达性。"""
    return {
        "status": "ok",
        "config": llm.check_env_ready(),
    }
