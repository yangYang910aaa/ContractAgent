"""流式：逐 token 推 SSE，工具过程与引用都挂同一条流上。

事件固定六类（status / tool / token / citations / usage / error+done），前端按事件名分发；
引用取自图状态里工具返回过的记录，不是模型复述的内容。非流式那一份（chat_once）供前端
读流失败时降级，两条路走同一份回答与引用口径。
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Iterable

from langchain_core.callbacks import BaseCallbackHandler

from backend.app.assistant.citations import artifact_items, citation_from
from backend.app.assistant.graph import answer_from_state, message_text
from backend.app.assistant.tools import hit_summary

# 工具 → 状态行文案（前端在回答气泡上方显示"正在做什么"）
_TOOL_STATUS = {
    "search_policies": "正在查政策库…",
    "read_policy": "正在读政策原文…",
    "find_clauses": "正在合同里找条款…",
}


class ChatUsage(BaseCallbackHandler):
    """一次问答的模型调用计数（token 由框架的用量回调单独提供）。"""

    calls: int = 0  # 到目前发起的模型调用次数

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        """模型调用开始 → 计数；失败的那次请求也已发出，照数。"""
        self.calls += 1


def sse_frame(event: str, data: dict) -> str:
    """SSE 帧：事件名 + JSON 数据（前端按事件名分发）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _usage_payload(calls: int, started: float) -> dict:
    """成本行数据：模型调用次数 + 耗时秒数。

    只报这两项：厂商回调给的 token 数在真跑里出现过明显不合理的量级（单次问答报出上百万），
    与其显示一个不能信的数字，不如不显示（调用次数与秒数是实测口径）。
    """
    return {"calls": calls, "seconds": round(time.perf_counter() - started, 1)}


def _tool_status(name: str) -> str:
    """工具名 → 状态行文案。"""
    return _TOOL_STATUS.get(name or "", "正在检索…")


def _tool_event(event: dict) -> dict:
    """工具结束事件 → 命中摘要 + 检索到的条目（前端状态行与展开看条文用）。"""
    output = (event.get("data") or {}).get("output")
    # 分支：框架给 ToolMessage（带 artifact）；直接拿元组（content, artifact）时取后一项
    artifact = getattr(output, "artifact", None)
    if artifact is None and isinstance(output, tuple) and len(output) == 2:
        artifact = output[1]
    items = [c.to_dict() for c in (citation_from(i) for i in artifact_items(artifact)) if c is not None]
    name = event.get("name") or ""
    return {"name": name, "summary": hit_summary(name, items), "items": items}


async def stream_chat(
    agent: Any,
    question: str,
    config: dict,
    declared_refs: Iterable[str] = (),
    declared_hits: dict[str, dict] | None = None,
) -> AsyncIterator[str]:
    """逐 token 跑一轮问答，按固定六类事件推 SSE 帧。

    事件：status（阶段）/ tool（工具与命中）/ token（答案增量）/ citations（本轮引用）/
    usage（次数与耗时）/ error 与 done（失败与结束）。引用取自图状态里工具返回过的记录，
    不是模型复述的内容；`done` 还带一份完整回答，供前端在校对丢字时兜底。
    """
    started = time.perf_counter()
    usage = ChatUsage()
    run_config = {**config, "callbacks": [usage]}
    try:
        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": question}]}, run_config, version="v2"
        ):
            kind = event.get("event")
            # 分支：模型吐正文 → 逐字推给前端（工具轮通常没有正文，空片段不推）
            if kind == "on_chat_model_stream":
                text = message_text(getattr((event.get("data") or {}).get("chunk"), "content", ""))
                if text:
                    yield sse_frame("token", {"text": text})
                continue
            # 分支：工具开始 → 状态行（"正在查政策库…"）
            if kind == "on_tool_start":
                yield sse_frame("status", {"text": _tool_status(event.get("name") or "")})
                continue
            # 分支：工具结束 → 状态行更新成命中摘要，并带上检索到的条目
            if kind == "on_tool_end":
                yield sse_frame("tool", _tool_event(event))
        state = await agent.aget_state(config)
        result = answer_from_state(getattr(state, "values", {}) or {}, declared_refs, declared_hits)
        yield sse_frame("citations", {"citations": result["citations"], "unverified": result["unverified"]})
        yield sse_frame("usage", _usage_payload(usage.calls, started))
        yield sse_frame("done", {"status": "done", "answer": result["answer"]})
    except Exception as exc:  # 流中途失败：把原因推给前端，由它给"重试"按钮
        yield sse_frame("error", {"message": f"回答失败：{exc}"})
        yield sse_frame("done", {"status": "error", "answer": ""})


async def chat_once(
    agent: Any,
    question: str,
    config: dict,
    declared_refs: Iterable[str] = (),
    declared_hits: dict[str, dict] | None = None,
) -> dict:
    """非流式跑一轮：回答 + 引用 + 用量（前端读流失败时降级到这一个）。"""
    started = time.perf_counter()
    usage = ChatUsage()
    state = await agent.ainvoke(
        {"messages": [{"role": "user", "content": question}]},
        {**config, "callbacks": [usage]},
    )
    return {**answer_from_state(state, declared_refs, declared_hits), "usage": _usage_payload(usage.calls, started)}
