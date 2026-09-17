"""MCP(stdio) 服务端：把审查能力暴露成工具，供 AI 客户端（Claude Desktop / Cursor / Codex）调用。

启动（在客户端里配 stdio 命令，或直接命令行跑）：
    python backend/app/mcp_server.py
工作目录要是仓库根：data/policies 与 data/uploads 按它定位，.env 里的模型 key 也从根目录读。

四个工具：submit_contract（提交 → 任务号）/ get_report（状态 + 报告）/ ask_policy（问政策库）/
ask_contract（就某份合同提问）。一次审查 30~120 秒，同步等着返回必然撞客户端超时，
所以提交只登记入队、结果靠 get_report 轮询——服务端本来就是这套模式。

本文件是薄层：判定、抽取、检索、对话图都复用现成实现，这里只做参数校验与结果整形。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# 直接运行本文件时（python backend/app/mcp_server.py），Python 把 backend/app 当
# sys.path[0]，找不到 backend 包——把仓库根插进 path（模块方式启动时跳过）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.mcpserver import MCPServer

from backend.app import assistant
from backend.app.config import settings
from backend.app.policy.rag import retrieve_policies
from backend.app.review.graph import ReviewRunner
from backend.app.review.parser import SUPPORTED_SUFFIXES
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.store_pg import PgPersistence

# 与主服务的 HTTP 入口同一口径：都只认 single/double（parallel 未实现）
_REVIEW_MODES = ("single", "double")


def _build_manager() -> tuple[TaskManager, PgPersistence | None]:
    """建任务管理器：配了 DATABASE_URL 就用 Postgres 持久化，否则内存兜底。

    与 main.py 的 _build_default_manager 同一口径——持久化失败宁可起不来，
    也不要静默退回内存把任务丢掉。
    """
    if not settings.database_url:
        return TaskManager(), None
    persistence = PgPersistence()
    runner = ReviewRunner(store=persistence.store, checkpointer=persistence.checkpointer)
    return TaskManager(runner=runner), persistence


def _risk_brief(risk: dict, default_severity: str | None = None) -> dict[str, Any]:
    """一条风险的对外形态：留下判定所需的字段，去掉内部属性。

    闸口载荷里的风险不带 severity（那屏全是待审高风险），用 default_severity 兜底，
    调用方拿到的 severity 就不会是空。
    """
    return {
        "type": risk.get("risk_type"),
        "label": risk.get("label") or risk.get("risk_type"),
        "severity": risk.get("severity") or default_severity,
        "clause": risk.get("clause_ref") or "",
        "evidence": risk.get("evidence") or "",
        "quote": risk.get("evidence_quote") or "",
        "policy": risk.get("policy_ref"),
        "suggestion": risk.get("suggestion") or "",
        "origin": risk.get("origin", "rules"),
    }


def _report_brief(record, include_full: bool) -> dict[str, Any]:
    """报告摘要：状态、评级、风险清单、关键字段、审批留痕（-- 可选带完整报告）。"""
    report = getattr(record, "report", None) or {}
    payload = getattr(record, "gate_payload", None) or {}
    out: dict[str, Any] = {
        "thread_id": record.thread_id,
        "file": record.name or record.source,
        "status": record.status,
        "review_mode": record.review_mode,
    }
    # 分支：停在闸口 → 报告还没生成，给待审高风险与"该怎么处理"
    if record.status == "gate":
        out["grade"] = payload.get("grade")
        out["awaiting"] = "高风险待人工审批：请到工作台选择放行或打回，报告此时还没生成"
        # 闸口只送高风险进来，但对外仍叫 risks——调用方不必按状态换字段名
        out["risks"] = [_risk_brief(r, default_severity="high") for r in payload.get("high_risks") or []]
        out["ask"] = payload.get("ask", "")
        return out
    # 分支：审查失败 → 只回原因，别让调用方以为拿到了报告
    if record.status == "error":
        out["error"] = record.error or "审查失败"
        return out
    # 分支：还在排队/审查中 → 提示稍后再取（长任务不阻塞）
    if record.status in ("pending", "processing"):
        out["hint"] = "审查进行中，稍后重试 get_report；单份约 30~120 秒"
        return out
    out["grade"] = report.get("grade")
    out["risks"] = [_risk_brief(r) for r in report.get("risks") or []]
    out["fields"] = (report.get("extracted") or {}) and {
        key: value
        for key, value in (report.get("extracted") or {}).items()
        if key != "extraction_meta"
    }
    out["approval"] = report.get("approval")
    out["llm"] = report.get("llm")
    if include_full:
        out["report"] = report
    return out


def create_server(
    manager: TaskManager | None = None,
    persistence: PgPersistence | None = None,
) -> MCPServer:
    """建 MCP 服务端（可注入 manager，测试与 main.create_app 同一套路）。

    注入的 manager 由调用方负责关闭（lifespan 只收尾自己建的那个）。
    """
    owns_manager = manager is None
    if manager is None:
        manager, persistence = _build_manager()

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        """进程生命周期：退出时停 worker 池、释放持久化连接（与 main.py 同样两条收尾）。"""
        try:
            yield
        finally:
            if owns_manager:
                manager.shutdown()
                if persistence is not None:
                    persistence.close()

    server = MCPServer(
        name="contract-agent",
        instructions=(
            "供应商合同智能审核：提交合同文件 → 轮询取风险报告；也可直接问政策库或就某份合同提问。"
            "审查要跑模型，单份约 30~120 秒，请提交后轮询 get_report，不要期待立即拿到结果。"
        ),
        lifespan=lifespan,
    )

    @server.tool(
        structured_output=True,
        description=(
            "提交一份本机合同文件（pdf/docx/md/txt/图片）登记审查任务，立刻返回任务号。"
            "用 get_report 拿结果；review_mode=double 时多一路独立盲审复核，更慢但更稳。"
        )
    )
    def submit_contract(path: str, review_mode: str = "single") -> dict[str, Any]:
        """提交本机合同文件 → 任务号。不等待审查完成。"""
        file = Path(path).expanduser()
        # 分支：路径不存在 → 明确回报（客户端往往只拿到一段文本，错误要说清楚该怎么改）
        if not file.is_file():
            return {"error": f"文件不存在：{file}"}
        # 分支：格式不在可解析范围 → 提前拒绝，别让任务跑到 worker 里才失败
        if file.suffix.lower() not in SUPPORTED_SUFFIXES:
            return {"error": f"暂不支持 {file.suffix or '无后缀'} 格式，请给 PDF/Word/文本/图片"}
        # 分支：模式不认识 → 拒绝（静默退回 single 会让人以为选了双审）
        if review_mode not in _REVIEW_MODES:
            return {"error": f"审查模式只支持 {'/'.join(_REVIEW_MODES)}，收到 {review_mode!r}"}
        thread_id = manager.register(file.name, review_mode=review_mode)
        # 任务先按文件名登记（落盘名/展示名用它），再把 source 补成真实路径供 worker 读盘
        manager.runner.store.update(thread_id, source=str(file.resolve()))
        manager.enqueue(thread_id)
        return {
            "thread_id": thread_id,
            "status": "pending",
            "file": file.name,
            "review_mode": review_mode,
            "hint": "已入队；过一会儿用 get_report 取状态与报告（单份约 30~120 秒）",
        }

    @server.tool(
        structured_output=True,
        description=(
            "按任务号取审查状态与报告：进行中只回状态，完成回评级 / 风险清单 / 关键字段，"
            "停在闸口回待审高风险。include_full=true 连抽取证据一起给（内容较长）。"
        )
    )
    def get_report(thread_id: str, include_full: bool = False) -> dict[str, Any]:
        """取某个任务的状态与报告（报告结构见 _report_brief）。"""
        record = manager.runner.store.get(thread_id)
        # 分支：任务号不存在 → 回错误对象，别抛异常把客户端整轮对话打断
        if record is None:
            return {"error": f"任务不存在：{thread_id}"}
        return _report_brief(record, include_full)

    @server.tool(
        structured_output=True,
        description=(
            "按问题检索政策库（混合检索：向量 + BM25 经 RRF 融合），"
            "返回命中条文的编号、出处与原文；rank_score 是融合分，只用于排序，不是相似度。"
        )
    )
    def ask_policy(question: str, k: int = 3) -> dict[str, Any]:
        """问政策库：返回最相关的 k 条条文（编号 + 出处 + 原文 + 排序分）。"""
        # 分支：空问题 → 不发起检索（一次向量化调用也是钱）
        if not question.strip():
            return {"error": "问题不能为空"}
        hits = retrieve_policies(question, k=max(1, min(int(k), 10)))
        return {
            "question": question,
            "hits": [
                {
                    "policy_ref": hit.policy_ref,
                    "source": hit.source,
                    "text": hit.text,
                    # 混合检索下这是 RRF 融合分（越大越靠前），与余弦相似度不同量纲
                    "rank_score": round(float(hit.score), 4),
                }
                for hit in hits
            ],
        }

    @server.tool(
        structured_output=True,
        description=(
            "就某份已提交的合同提问（解释判定、查政策依据、找条款原文）。"
            "只解释、不改风险清单与评级；同一 session_id 的追问会带上上下文。"
        )
    )
    async def ask_contract(thread_id: str, question: str, session_id: str = "mcp") -> dict[str, Any]:
        """就某份合同提问：复用详情页那套对话图（上下文绑定这份合同）。"""
        record = manager.runner.store.get(thread_id)
        # 分支：任务不存在 → 回错误对象
        if record is None:
            return {"error": f"任务不存在：{thread_id}"}
        # 分支：原文还没解析出来 → 助手没有可依据的合同内容，先别花这次调用
        if not (getattr(record, "source_text", "") or getattr(record, "report", None)):
            return {"error": "这份合同还在解析/审查中，暂时没内容可问，稍后再试"}
        if not question.strip():
            return {"error": "问题不能为空"}
        context = assistant.build_context(record)
        agent = assistant.build_chat_agent(context, checkpointer=manager.runner.checkpointer)
        config = assistant.chat_config(thread_id, session_id)
        result = await assistant.chat_once(
            agent, question.strip(), config, context.declared_refs, context.declared_hits
        )
        return {
            "thread_id": thread_id,
            "question": question,
            "answer": result.get("answer", ""),
            "citations": result.get("citations", []),
            "unverified": result.get("unverified", []),
            "llm": result.get("usage"),
        }

    return server


def main() -> None:
    """命令行入口：以 stdio 起服务（客户端配的就是这条命令）。"""
    # stdout 是协议线：固定 UTF-8（客户端按 UTF-8 解析），并让串码不致命——
    # 万一路径上有非 UTF-8 输出漏进来，也只是那一行解析失败，不会把整条连接打断
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
