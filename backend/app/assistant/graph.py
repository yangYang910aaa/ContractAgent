"""对话图：编排、循环控制、重试、摘要都走框架件。

`create_agent` 建图（模型、工具体、检查点都可注入），中间件统一管调用上限、工具重试
与长对话摘要；本模块只负责"按这份合同组装图"和"从图状态里取出回答 + 引用"。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import AIMessage

from backend.app.assistant.citations import summarize_citations
from backend.app.assistant.context import ContractContext
from backend.app.assistant.prompts import system_prompt
from backend.app.assistant.tools import build_tools
from backend.app.llm import get_chat_model

# 一轮问答的模型调用上限：检索用满也得留着配额写回答，所以给到 4 而不是 3
MODEL_CALL_LIMIT = 4
# 一轮问答的工具调用上限：查政策、读全文、找条款各来一两轮都够
TOOL_CALL_LIMIT = 6
# 工具失败重试：检索抖动重试两次后把错误交给模型，不让一次抖动掀翻整轮回答
TOOL_RETRY_MAX = 2
TOOL_RETRY_INITIAL_DELAY = 1.0  # 重试退避起点（秒），指数增长
# 长对话摘要阈值：定高——触发一次就多一次模型调用，正常一问一答到不了
SUMMARY_TRIGGER_TOKENS = 20000


def build_chat_agent(
    context: ContractContext,
    *,
    model: Any = None,
    retriever: Callable[..., list] | None = None,
    checkpointer: Any = None,
    summarizer_model: Any = None,
) -> Any:
    """构造对话图：create_agent + 调用上限 / 工具重试 / 长对话摘要三个中间件。

    模型、检索器、检查点都可注入（离线测试给假模型 + 内存检查点；服务端给真模型 +
    Postgres 检查点）。工具与系统提示都绑这份合同的上下文，故图按合同构造；对话历史
    按会话键存在检查点里，刷新页面与后端重启都不丢。
    """
    tools = build_tools(context, retriever=retriever)
    chat_model = model or get_chat_model()
    middleware = [
        # 上限交给中间件：超了优雅收尾（exit_behavior="end"），不抛异常打断前端流
        ModelCallLimitMiddleware(run_limit=MODEL_CALL_LIMIT, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=TOOL_CALL_LIMIT),
        # 工具重试与错误回传：重试用尽后把错误文本交给模型，让它照实说"检索失败"
        ToolRetryMiddleware(
            max_retries=TOOL_RETRY_MAX,
            on_failure="continue",
            initial_delay=TOOL_RETRY_INITIAL_DELAY,
        ),
        SummarizationMiddleware(
            model=summarizer_model or chat_model,
            trigger=("tokens", SUMMARY_TRIGGER_TOKENS),
        ),
    ]
    return create_agent(
        chat_model,
        tools,
        system_prompt=system_prompt(context),
        middleware=middleware,
        checkpointer=checkpointer,
    )


def message_text(content: Any) -> str:
    """消息正文 → 纯文本：模型可能回内容块列表（含非文本块），只取文本块。"""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        # 分支：内容块 dict → 只收文本类型，推理块之类不进回答
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def final_answer(state: dict) -> str:
    """图状态 → 最终回答文本（最后一条有内容的模型消息；没有就回空串）。"""
    for message in reversed(state.get("messages") or []):
        if not isinstance(message, AIMessage):
            continue
        text = message_text(message.content)
        if text.strip():
            return text
    return ""


def answer_from_state(state: dict, declared_refs: Iterable[str] = ()) -> dict:
    """图状态 → 本轮结果：回答文本 + 引用汇总 + 无法核实的政策编号。"""
    answer = final_answer(state)
    return {"answer": answer, **summarize_citations(state.get("messages"), answer, declared_refs)}


def ask(agent: Any, question: str, config: dict, declared_refs: Iterable[str] = ()) -> dict:
    """同步问一句（离线测试与脚本用）：跑图并汇总回答与引用。"""
    state = agent.invoke({"messages": [{"role": "user", "content": question}]}, config)
    return answer_from_state(state, declared_refs)


async def ask_async(agent: Any, question: str, config: dict, declared_refs: Iterable[str] = ()) -> dict:
    """异步问一句：流式失败时的降级路径用（同一路由按 Accept 决定走流还是走这里）。"""
    state = await agent.ainvoke({"messages": [{"role": "user", "content": question}]}, config)
    return answer_from_state(state, declared_refs)
