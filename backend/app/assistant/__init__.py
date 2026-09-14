"""对话助手（包门面）。

助手只做三件事：解释判定、查政策库、在合同里找条款；风险清单、评级与审批状态一律不动。
对外只从这里取用（`from backend.app.assistant import build_chat_agent, ...`），模块按职责拆开：

| 模块 | 职责 |
| --- | --- |
| context.py | 这份合同的上下文：风险清单 / 抽取字段 / 条款目录 / 政策库目录 |
| citations.py | 引用：工具返回记录 → 本轮引用 + "无法核实"接地校验（纯函数） |
| tools.py | 三个工具：查政策库（检索器适配）/ 读政策全文 / 合同找条款 |
| prompts.py | 系统提示：回答要求、不做的事、拼上合同上下文 |
| graph.py | 对话图：create_agent + 调用上限 / 工具重试 / 长对话摘要 |
| memory.py | 会话记忆：会话键与调用配置 + 从检查点回读历史轮次 |
| streaming.py | 流式：SSE 帧与事件映射、非流式降级、用量计数 |

依赖方向单向：tools 用 context/citations，graph 用 tools/prompts/context，
streaming 与 memory 用 graph/citations——不会绕回来。新增能力按职责落到对应模块，
不要把实现写进本文件。
"""

from __future__ import annotations

from backend.app.assistant.citations import (
    MAX_CITATIONS,
    Citation,
    collect_citations,
    mentioned_policy_refs,
    summarize_citations,
)
from backend.app.assistant.context import (
    ContractContext,
    build_context,
    context_brief,
    declared_policy_refs,
    policy_directory,
)
from backend.app.assistant.graph import (
    MODEL_CALL_LIMIT,
    SUMMARY_TRIGGER_TOKENS,
    TOOL_CALL_LIMIT,
    TOOL_RETRY_INITIAL_DELAY,
    TOOL_RETRY_MAX,
    answer_from_state,
    ask,
    ask_async,
    build_chat_agent,
    final_answer,
)
from backend.app.assistant.memory import chat_config, chat_session_key, history_turns
from backend.app.assistant.prompts import system_prompt
from backend.app.assistant.streaming import ChatUsage, chat_once, sse_frame, stream_chat
from backend.app.assistant.tools import (
    FIND_CLAUSES_LIMIT,
    POLICY_SEARCH_K,
    PolicyRetriever,
    build_tools,
)

__all__ = [
    # 上下文
    "ContractContext",
    "build_context",
    "context_brief",
    "declared_policy_refs",
    "policy_directory",
    # 引用
    "Citation",
    "MAX_CITATIONS",
    "collect_citations",
    "mentioned_policy_refs",
    "summarize_citations",
    # 工具
    "PolicyRetriever",
    "build_tools",
    "POLICY_SEARCH_K",
    "FIND_CLAUSES_LIMIT",
    # 提示词与图
    "system_prompt",
    "build_chat_agent",
    "MODEL_CALL_LIMIT",
    "TOOL_CALL_LIMIT",
    "TOOL_RETRY_MAX",
    "TOOL_RETRY_INITIAL_DELAY",
    "SUMMARY_TRIGGER_TOKENS",
    "final_answer",
    "answer_from_state",
    "ask",
    "ask_async",
    # 会话记忆
    "chat_session_key",
    "chat_config",
    "history_turns",
    # 流式
    "stream_chat",
    "chat_once",
    "sse_frame",
    "ChatUsage",
]
