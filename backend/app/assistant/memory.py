"""会话记忆：会话键 + 从检查点回读的历史轮次。

对话历史不另存一份，就落在审查图共用的那个检查点库里，键是 `chat:{任务号}:{会话号}`——
前缀不能去掉，否则会与审查图的线程撞在一起。回读时按轮次重算引用，与当时页面上的芯片同源。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from backend.app.assistant.citations import summarize_citations
from backend.app.assistant.graph import message_text

# 对话会话键前缀：与审查图共用检查点库，带前缀才不会读写到审查状态
_SESSION_KEY_PREFIX = "chat"


def chat_session_key(thread_id: str, session_id: str) -> str:
    """对话会话键：带自己的前缀，免得与审查图在同一个检查点库里撞线程。"""
    return f"{_SESSION_KEY_PREFIX}:{thread_id}:{session_id or 'default'}"


def chat_config(thread_id: str, session_id: str) -> dict:
    """对话图的调用配置：会话键 + 递归上限（防失控循环）。"""
    return {"configurable": {"thread_id": chat_session_key(thread_id, session_id)}, "recursion_limit": 30}


def history_turns(
    messages: Sequence[BaseMessage] | None,
    declared_refs: Iterable[str] = (),
    declared_hits: dict[str, dict] | None = None,
) -> list[dict]:
    """检查点里的消息 → 面板气泡用的轮次列表（提问 + 回答 + 引用）。

    引用按这一轮里工具返回过的记录现算，与当时页面上的芯片一致——不另存一份引用，
    省掉"存的与显示的不一致"这种漂移；报告里本来就有的依据也要一起传进来，否则
    当时靠报告原文补的那条芯片（origin=report）回读时会不见。没有回答的轮次也保留提问。
    """
    turns: list[dict] = []
    tools: list[ToolMessage] = []  # 当前这一轮的工具返回记录
    for message in messages or []:
        # 分支：新的提问 → 先给上一轮结算引用，再开一轮
        if isinstance(message, HumanMessage):
            if turns:
                turns[-1].update(
                    summarize_citations(tools, turns[-1]["answer"], declared_refs, declared_hits)
                )
            turns.append({"question": message_text(message.content), "answer": "", "citations": [], "unverified": []})
            tools = []
            continue
        # 分支：还没有提问（异常历史）→ 忽略
        if not turns:
            continue
        if isinstance(message, ToolMessage):
            tools.append(message)
        # 分支：模型消息 → 取最后一条有正文的（工具轮的中间叙述会被最终回答盖掉）
        elif isinstance(message, AIMessage):
            text = message_text(message.content)
            if text.strip():
                turns[-1]["answer"] = text
    if turns:
        turns[-1].update(summarize_citations(tools, turns[-1]["answer"], declared_refs, declared_hits))
    return turns
