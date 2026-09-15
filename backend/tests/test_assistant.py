"""对话助手工具层单测：上下文拼装 + 三个工具（假检索器，离线）+ 引用汇总与接地校验。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from backend.app import assistant
from backend.app.assistant import graph as assistant_graph
from backend.app.assistant import (
    MAX_CITATIONS,
    PolicyRetriever,
    answer_from_state,
    ask,
    build_context,
    build_chat_agent,
    build_tools,
    chat_config,
    chat_session_key,
    collect_citations,
    context_brief,
    final_answer,
    history_turns,
    mentioned_policy_refs,
    policy_directory,
    summarize_citations,
)
from backend.app.policy_rag import PolicyHit
from backend.app.store import TaskRecord

# 两份合同文本：一份有「第X条」结构，一份按 PDF 习惯硬换行且汉字间有空格
CONTRACT_TEXT = """采购合同

第一条 合同标的
采购服务器 10 台。

第二条 付款方式
合同签订后 30 日内支付预付款 60%。
"""

WRAPPED_TEXT = """第一条 标的
乙方不
得将本合同项下义务转包。
第二条 发票
乙方开具增值税专用发 票，甲方收到后付 款。
"""


def _fake_retriever(hits: list[PolicyHit]):
    """假政策检索：固定返回给定命中（离线测试不连向量库与 embedding）。"""

    def _search(query: str) -> list[PolicyHit]:
        return hits

    return _search


def _hit(policy_ref: str = "P-01", text: str = "## 第二条 预付款比例上限\n不得超过总额的 30%。") -> PolicyHit:
    """一条最小政策命中。"""
    return PolicyHit(policy_ref=policy_ref, source=f"{policy_ref}_x.md", text=text, score=0.87)


def _record(report: dict | None = None, gate_payload: dict | None = None, text: str = CONTRACT_TEXT) -> TaskRecord:
    """最小任务记录（详情页上下文就是从它拼出来的）。"""
    return TaskRecord(
        thread_id="t-1",
        source="data/uploads/t-1.pdf",
        name="采购合同.pdf",
        source_text=text,
        status="done" if report else "gate",
        report=report,
        gate_payload=gate_payload,
    )


def _tools_by_name(context, retriever=None) -> dict:
    """按工具名索引（工具顺序不属于对模型的约定，测试只认名字）。"""
    return {tool.name: tool for tool in build_tools(context, retriever=retriever)}


def _call(tool, call_id: str = "call-1", **args) -> ToolMessage:
    """按模型调用工具的形态跑工具，拿回 ToolMessage（content + artifact 都在上面）。"""
    return tool.invoke({"name": tool.name, "args": args, "id": call_id, "type": "tool_call"})


def _report() -> dict:
    """一份最小报告：一条 high 风险（带政策依据）+ 两个抽取字段。"""
    return {
        "grade": "fail",
        "risks": [
            {
                "risk_type": "prepayment_ratio_high",
                "label": "预付款比例过高",
                "severity": "high",
                "clause_ref": "第二条",
                "evidence": "预付款比例 60%",
                "evidence_quote": "合同签订后 30 日内支付预付款 60%。",
                "policy_ref": "P-01",
                "suggestion": "建议降至 30% 以内。",
            }
        ],
        "policy_hits": [{"policy_ref": "P-01", "score": 0.9, "snippet": "预付款不超过 30%"}],
        "extracted": {
            "total_amount": "930000",
            "currency": "CNY",
            "payment_schedule": [{"name": "预付款", "amount": "558000", "percent": 60.0}],
        },
    }


# ---- 上下文：合同目录与政策目录 ----


def test_policy_directory_covers_all_policies_with_scope() -> None:
    """政策目录要列全 15 份，并带上"这份政策管什么"的适用范围。"""
    directory = policy_directory()
    refs = [item["ref"] for item in directory]
    assert refs == [f"P-{i:02d}" for i in range(1, 16)]
    p01 = next(item for item in directory if item["ref"] == "P-01")
    assert p01["title"] == "预付款管理"
    assert "适用范围" not in p01["scope"] and "政府采购示范文本" in p01["scope"]


def test_context_brief_carries_risks_fields_and_directories() -> None:
    """上下文要带风险清单（判定口径）、抽取字段（含单位）、条款目录与政策目录。"""
    brief = context_brief(build_context(_record(report=_report())))
    assert "采购合同.pdf" in brief and "fail（不通过）" in brief
    assert "预付款比例过高" in brief and "高风险" in brief
    assert "条款：第二条" in brief and "依据：P-01" in brief
    assert "合同总额：930000元" in brief
    assert "预付款：558000 元（60.0%）" in brief
    assert "第一条 合同标的" in brief and "第二条 付款方式" in brief
    assert "P-01 预付款管理" in brief


def test_build_context_falls_back_to_gate_payload() -> None:
    """停闸口时还没有报告：风险清单取待审批载荷里的高风险项，别让助手没上下文可讲。"""
    payload = {"grade": "fail", "high_risks": [{"label": "预付款比例过高", "severity": "high", "policy_ref": "P-01"}]}
    context = build_context(_record(gate_payload=payload))
    assert context.risks[0]["label"] == "预付款比例过高"
    assert context.declared_refs == ["P-01"]


def test_build_context_without_clause_structure_still_lists_body() -> None:
    """合同没有「第X条」结构时，条款目录退成整篇一块，不报错也不给空目录。"""
    brief = context_brief(build_context(_record(text="甲方采购设备，乙方供货。")))
    assert "全文" in brief


# ---- 工具一：查政策库（框架检索工具 + 我们的混合检索适配器）----


def test_search_policies_tool_returns_text_with_policy_ref() -> None:
    """检索工具给模型的文本要带政策编号，回答才引得到号。"""
    tools = _tools_by_name(build_context(_record()), retriever=_fake_retriever([_hit()]))
    message = _call(tools["search_policies"], query="预付款上限是多少")
    assert message.content.startswith("[P-01] 第二条 预付款比例上限")
    assert "30%" in message.content
    assert isinstance(message.artifact, list) and isinstance(message.artifact[0], Document)
    assert message.artifact[0].metadata["policy_ref"] == "P-01"


def test_policy_retriever_async_path_works() -> None:
    """图里走异步检索：检索实现是同步的，框架会丢线程池执行，接口要能 await。"""
    retriever = PolicyRetriever(retriever=_fake_retriever([_hit(policy_ref="P-02")]))
    docs = asyncio.run(retriever.ainvoke("质保期多久"))
    assert [doc.metadata["policy_ref"] for doc in docs] == ["P-02"]


def test_search_policies_tool_without_hit_returns_empty() -> None:
    """一条没检索到 → 工具文本为空（模型据此说"政策库没查到"，不编条文）。"""
    tools = _tools_by_name(build_context(_record()), retriever=_fake_retriever([]))
    message = _call(tools["search_policies"], query="无关问题")
    assert message.content == "" and message.artifact == []


# ---- 工具二：读政策全文 ----


def test_read_policy_returns_whole_text_and_citation() -> None:
    """按编号读政策：返回整份全文，并把这份政策记成一条引用。"""
    tools = _tools_by_name(build_context(_record()))
    message = _call(tools["read_policy"], policy_ref="p-02")
    assert message.content.startswith("[P-02]") and "质量保证期" in message.content
    assert message.artifact[0]["kind"] == "policy" and message.artifact[0]["ref"] == "P-02"
    assert message.artifact[0]["source"].startswith("P-02")


def test_read_policy_unknown_ref_reports_missing() -> None:
    """编号不存在或形态不对 → 明确回报且不收进引用（防模型照着目录编政策）。"""
    tools = _tools_by_name(build_context(_record()))
    message = _call(tools["read_policy"], policy_ref="制度汇编")
    assert "没有找到政策" in message.content and message.artifact == []


# ---- 工具三：在合同里找条款 ----


def test_find_clauses_returns_clause_ref_title_and_quote() -> None:
    """找条款要回条款号 + 原句，且原句取自正文（前端据此在原文抽屉里高亮）。"""
    tools = _tools_by_name(build_context(_record()))
    message = _call(tools["find_clauses"], keyword="预付款")
    assert "第二条" in message.content
    assert "合同签订后30日内支付预付款60%。" in message.content
    assert message.artifact == [
        {
            "kind": "clause",
            "ref": "第二条",
            "title": "第二条 付款方式",
            "text": "合同签订后30日内支付预付款60%。",
            "source": "",
            "origin": "tool",
        }
    ]


def test_find_clauses_matches_across_hard_wrap_and_inner_spaces() -> None:
    """PDF/OCR 形态的正文：关键词被换行切开、汉字间多空格，仍要命中。"""
    tools = _tools_by_name(build_context(_record(text=WRAPPED_TEXT)))
    assert "乙方不得将本合同项下义务转包。" in _call(tools["find_clauses"], keyword="转包").content
    assert "增值税专用发票" in _call(tools["find_clauses"], keyword="发票").content


def test_find_clauses_reports_absent_keyword_and_empty_input() -> None:
    """没命中就照实说没有；空关键词直接要一个词，不拿空串在整篇里乱命中。"""
    tools = _tools_by_name(build_context(_record()))
    missed = _call(tools["find_clauses"], keyword="仲裁机构")
    assert "没有找到包含「仲裁机构」的表述" in missed.content and missed.artifact == []
    empty = _call(tools["find_clauses"], keyword="  ")
    assert "请给出要查找的关键词" in empty.content and empty.artifact == []


def test_find_clauses_without_clause_structure_searches_whole_text() -> None:
    """没有条款结构（如简短补充件）时按整篇一块找，条款号退成"全文"。"""
    tools = _tools_by_name(build_context(_record(text="双方约定：货到验收合格后付款。")))
    message = _call(tools["find_clauses"], keyword="验收")
    assert "货到验收合格后付款" in message.content
    assert message.artifact[0]["ref"] == "全文"


def test_build_tools_exposes_three_readonly_tools() -> None:
    """工具就三个，名字是模型要认的接口名。"""
    names = [tool.name for tool in build_tools(build_context(_record()))]
    assert names == ["search_policies", "read_policy", "find_clauses"]


# ---- 引用汇总与接地校验 ----


def _tool_message(content: str, artifact=None, tool_call_id: str = "call-1") -> ToolMessage:
    """伪造一条工具返回消息（artifact 就是工具返回记录）。"""
    return ToolMessage(content=content, artifact=artifact, tool_call_id=tool_call_id, name="find_clauses")


def test_tools_to_citations_end_to_end() -> None:
    """三个工具真正跑一遍 → 引用汇总只出现它们返回过的条目（政策条文 + 合同条款）。"""
    context = build_context(_record(report=_report()))
    tools = _tools_by_name(context, retriever=_fake_retriever([_hit()]))
    messages = [
        HumanMessage("预付款这条为什么判高风险？"),
        _call(tools["search_policies"], call_id="c1", query="预付款上限"),
        _call(tools["find_clauses"], call_id="c2", keyword="预付款"),
        AIMessage("预付款 60% 超过 P-01 的 30% 上限，另见 P-77。"),
    ]
    summary = summarize_citations(messages, messages[-1].content, declared_refs=context.declared_refs)
    assert [(c["kind"], c["ref"]) for c in summary["citations"]] == [("policy", "P-01"), ("clause", "第二条")]
    assert summary["citations"][1]["title"] == "第二条 付款方式"
    # P-01 有出处（工具返回过）、P-77 谁都没给过 → 只有它标"无法核实"
    assert summary["unverified"] == ["P-77"]


def test_collect_citations_only_counts_what_tools_returned() -> None:
    """引用只认工具返回记录：模型自己在回答里写的编号不算，重复返回只留一条。"""
    quote = {"kind": "clause", "ref": "第二条", "title": "第二条 付款方式", "text": "预付款 60%"}
    policy = Document(page_content="## 第二条 上限\n30%", metadata={"policy_ref": "P-01", "title": "第二条 上限", "source": "P-01_x.md"})
    messages = [
        HumanMessage("这份合同为什么判高风险"),
        _tool_message("第二条…", artifact=[quote]),
        _tool_message("[P-01]…", artifact=[policy]),
        _tool_message("第二条…", artifact=[quote]),  # 同一引用重复返回 → 只留一条
        AIMessage("依据 P-01，第二条写了 60% 的预付款。"),
    ]
    citations = collect_citations(messages)
    assert [c.ref for c in citations] == ["第二条", "P-01"]
    assert citations[1].source == "P-01_x.md"


def test_collect_citations_skips_unknown_artifact_shape_and_clips_text() -> None:
    """认不出的返回记录（没有编号）不进引用；超长条文按上限截断。"""
    messages = [
        _tool_message("x", artifact=[{"kind": "clause", "text": "没有编号"}]),
        _tool_message("y", artifact=[Document(page_content="长" * 3000, metadata={"policy_ref": "P-14"})]),
    ]
    citations = collect_citations(messages)
    assert len(citations) == 1 and citations[0].ref == "P-14"
    assert len(citations[0].text) < 3000 and citations[0].text.endswith("…")


def test_collect_citations_accepts_serialized_document() -> None:
    """从检查点读回来的检索命中是普通 dict（page_content + metadata），照样要成芯片。"""
    serialized = {
        "id": "doc-1",
        "type": "Document",
        "page_content": "## 第三条 按日计罚的累计上限\n日费率明显偏高的应加累计上限。",
        "metadata": {"policy_ref": "p-14", "title": "第三条 按日计罚的累计上限", "source": "P-14_x.md"},
    }
    citations = collect_citations([_tool_message("[P-14]…", artifact=[serialized])])
    assert [(c.kind, c.ref) for c in citations] == [("policy", "p-14")]
    assert citations[0].title == "第三条 按日计罚的累计上限"
    assert citations[0].source == "P-14_x.md"


def test_history_turns_keeps_tool_and_report_chips_after_reload() -> None:
    """回读历史要与现场同口径：检索来的芯片、报告补的芯片都在，依据不被判成"未检索到"。

    消息在检查点里是序列化过的（检索命中已成普通 dict、报告依据根本不在消息里），
    按现场口径重算才不会出现"回答写着依据、引用栏却空着"。
    """
    context = build_context(_record(report=_report()))
    serialized = {
        "id": "doc-1",
        "type": "Document",
        "page_content": "违约金累计上限与赔偿责任上限不应相互倒挂。",
        "metadata": {"policy_ref": "P-14", "title": "第四条 与其他责任条款的衔接", "source": "P-14_x.md"},
    }
    messages = [
        HumanMessage("这份合同为什么判不通过？"),
        _tool_message("[P-14]…", artifact=[serialized]),
        AIMessage("依据 P-14 与 P-01，两条都判高风险。"),
    ]
    turns = history_turns(messages, context.declared_refs, context.declared_hits)
    assert turns[0]["question"] == "这份合同为什么判不通过？"
    assert [(c["ref"], c["origin"]) for c in turns[0]["citations"]] == [("P-14", "tool"), ("P-01", "report")]
    assert turns[0]["unverified"] == []


def test_collect_citations_caps_the_chip_list() -> None:
    """引用条数按上限收口（芯片栏放不下更多，超出的仍留在回答正文里）。"""
    artifact = [
        {"kind": "clause", "ref": f"第{i}条", "text": f"原句 {i}"} for i in range(MAX_CITATIONS + 3)
    ]
    assert len(collect_citations([_tool_message("x", artifact=artifact)])) == MAX_CITATIONS


def test_collect_citations_merges_same_ref_instead_of_repeating() -> None:
    """同一编号命中多段条文 → 合成一条芯片（正文拼起来），否则"找条款"命中同一条两次会出两个同名芯片。"""
    first = {"kind": "clause", "ref": "第二条", "title": "第二条 付款方式", "text": "预付款 60%。"}
    second = {"kind": "clause", "ref": "第二条", "title": "", "text": "尾款 40%。"}
    citations = collect_citations([_tool_message("x", artifact=[first, second])])
    assert len(citations) == 1
    assert citations[0].title == "第二条 付款方式"
    assert "预付款 60%。" in citations[0].text and "尾款 40%。" in citations[0].text


def test_summarize_citations_backs_mentioned_ref_with_report_text() -> None:
    """回答提到报告里本来就有的依据、但这轮没检索 → 用报告原文补一条芯片，且不算"未检索到"。

    真跑里最常出现的情形：模型直接拿上下文回答（不调工具），正文写着"依据 P-01"，
    引用栏却空着——用户想点开依据点不了。补这条芯片后，依据可点、内容与报告页一致。
    """
    context = build_context(_record(report=_report()))
    summary = summarize_citations(
        [HumanMessage("这条为什么是高风险？"), AIMessage("依据 P-01，预付款不得超过 30%。")],
        "依据 P-01，预付款不得超过 30%。",
        context.declared_refs,
        context.declared_hits,
    )
    assert [c["ref"] for c in summary["citations"]] == ["P-01"]
    assert summary["citations"][0]["origin"] == "report"
    assert "30%" in summary["citations"][0]["text"]
    assert summary["unverified"] == []


def test_summarize_citations_keeps_tool_citation_when_both_present() -> None:
    """这轮检索到了同一条 → 保留检索到的那条（origin=tool），不再拿报告里的重复补一条。"""
    context = build_context(_record(report=_report()))
    tool_citation = {"kind": "policy", "ref": "P-01", "title": "第二条 上限", "text": "30% 上限"}
    summary = summarize_citations(
        [_tool_message("x", artifact=[tool_citation])],
        "依据 P-01。",
        context.declared_refs,
        context.declared_hits,
    )
    assert len(summary["citations"]) == 1
    assert summary["citations"][0]["origin"] == "tool"
    assert summary["citations"][0]["text"] == "30% 上限"


def test_summarize_citations_flags_only_unbacked_policy_refs() -> None:
    """回答里提到的政策编号：工具返回过或报告声明过的不算编造，其余标"无法核实"。"""
    messages = [_tool_message("[P-01]…", artifact=[Document(page_content="30%", metadata={"policy_ref": "P-01"})])]
    answer = "依据 P-01 上限 30%；另可参考 p-09 与 P-88。"
    summary = summarize_citations(messages, answer, declared_refs=["P-09"])
    assert [c["ref"] for c in summary["citations"]] == ["P-01"]
    assert summary["unverified"] == ["P-88"]


def test_mentioned_policy_refs_dedupes_and_ignores_other_text() -> None:
    """政策编号按形态取，去重保序、大小写归一；普通文字里的 P 字不误判。"""
    assert mentioned_policy_refs("见 P-14 第三条，另见 p-14、P-02。") == ["P-14", "P-02"]
    assert mentioned_policy_refs("本合同 P 系列无编号") == []


# ---- 对话图：假模型 + 假检索器，全程离线 ----


def _tool_call(name: str, call_id: str = "c1", **args) -> AIMessage:
    """假模型的一轮工具调用（框架按 tool_calls 驱动工具）。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


class ScriptedChatModel(FakeMessagesListChatModel):
    """脚本化假模型：按顺序吐给定消息，工具绑定是空操作。

    离线测试用——脚本里已经写好这一轮要调哪个工具，绑不绑工具不影响行为；
    基类的 bind_tools 未实现（真实厂商模型才需要），这里补上让它能进 create_agent。
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """接收工具绑定但不改行为，返回自身。"""
        return self


def _agent(responses: list, retriever=None, checkpointer=None):
    """按脚本化消息序列造一个对话图（模型只吐给定消息，不连任何接口）。"""
    context = build_context(_record(report=_report()))
    agent = build_chat_agent(
        context,
        model=ScriptedChatModel(responses=responses),
        retriever=retriever if retriever is not None else _fake_retriever([_hit()]),
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
    )
    return context, agent


def test_agent_calls_clause_tool_then_answers_with_citation() -> None:
    """调工具 → 回答：引用来自工具返回记录，回答里提到的政策编号有报告依据不算编造。"""
    context, agent = _agent(
        [
            _tool_call("find_clauses", keyword="预付款"),
            AIMessage("第二条约定预付款 60%，超过 P-01 的 30% 上限。"),
        ]
    )
    result = ask(agent, "预付款这条为什么判高风险？", chat_config("t-1", "s1"), context.declared_refs)
    assert result["answer"].startswith("第二条约定预付款 60%")
    assert [(c["kind"], c["ref"]) for c in result["citations"]] == [("clause", "第二条")]
    assert result["unverified"] == []


def test_agent_policy_search_result_becomes_policy_citation() -> None:
    """查政策库 → 命中的条文进引用，回答引用的编号就是工具返回过的那个。"""
    context, agent = _agent(
        [
            _tool_call("search_policies", query="预付款上限"),
            AIMessage("按 P-01 第二条，预付款不得超过合同总额的 30%。"),
        ]
    )
    result = ask(agent, "预付款上限是多少？", chat_config("t-1", "s1"))
    assert [c["ref"] for c in result["citations"]] == ["P-01"]
    assert result["citations"][0]["source"] == "P-01_x.md"


def test_agent_answers_from_knowledge_without_citations() -> None:
    """不用工具也能答（如解释上下文里已有的判定）：引用为空，回答照常返回。"""
    context, agent = _agent([AIMessage("这条判高风险是因为预付款 60% 超过 P-01 的上限。")])
    result = ask(agent, "为什么判高风险？", chat_config("t-1", "s1"), context.declared_refs)
    assert result["citations"] == [] and result["unverified"] == []


def test_agent_flags_policy_ref_it_invented() -> None:
    """模型编了个没检索到的政策编号 → 标出来交给前端提示"无法核实"。"""
    _, agent = _agent([AIMessage("依据 P-88 的规定，这条应判高风险。")])
    result = ask(agent, "为什么判高风险？", chat_config("t-1", "s1"))
    assert result["unverified"] == ["P-88"]


def test_agent_retries_flaky_tool_and_still_answers(monkeypatch) -> None:
    """检索抖动：重试后拿到结果继续答（重试退避调小，测试不空等）。"""
    # 退避起点常量在 graph 模块里被 build_chat_agent 读取，要改就改那里
    monkeypatch.setattr(assistant_graph, "TOOL_RETRY_INITIAL_DELAY", 0.01)
    calls = {"n": 0}

    def flaky(query: str) -> list[PolicyHit]:
        calls["n"] += 1
        # 分支：第一次调用失败（模拟检索超时）→ 中间件重试后才成功
        if calls["n"] == 1:
            raise RuntimeError("检索超时")
        return [_hit()]

    _, agent = _agent(
        [_tool_call("search_policies", query="预付款上限"), AIMessage("按 P-01，上限 30%。")],
        retriever=flaky,
    )
    result = ask(agent, "预付款上限是多少？", chat_config("t-1", "s1"))
    assert calls["n"] == 2 and [c["ref"] for c in result["citations"]] == ["P-01"]


def test_agent_keeps_running_when_tool_fails_for_good(monkeypatch) -> None:
    """检索一直失败：不发散、不抛错——错误文本回给模型，这轮回答照样收尾。"""
    monkeypatch.setattr(assistant_graph, "TOOL_RETRY_INITIAL_DELAY", 0.01)

    def broken(query: str) -> list[PolicyHit]:
        raise RuntimeError("检索服务不可用")

    _, agent = _agent(
        [_tool_call("search_policies", query="预付款上限"), AIMessage("政策库暂时查不到，无法给出依据。")],
        retriever=broken,
    )
    result = ask(agent, "预付款上限是多少？", chat_config("t-1", "s1"))
    assert result["answer"].startswith("政策库暂时查不到")
    assert result["citations"] == []


def test_agent_stops_at_model_call_limit() -> None:
    """模型一直要求调工具：到上限就优雅收尾，不无限循环也不抛异常。"""
    _, agent = _agent([_tool_call("find_clauses", keyword="预付款")] * (assistant.MODEL_CALL_LIMIT + 2))
    agent_state = agent.invoke(
        {"messages": [{"role": "user", "content": "一直查"}]}, chat_config("t-1", "s1")
    )
    ai_messages = [m for m in agent_state["messages"] if isinstance(m, AIMessage)]
    assert len(ai_messages) <= assistant.MODEL_CALL_LIMIT
    assert isinstance(final_answer(agent_state), str)


def test_chat_history_is_kept_per_session() -> None:
    """历史按会话键存在检查点里：同一会话接着聊，换个会话从头开始。"""
    _, agent = _agent([AIMessage("第一答。"), AIMessage("第二答。")], checkpointer=MemorySaver())
    config = chat_config("t-1", "s1")
    ask(agent, "第一问", config)
    ask(agent, "第二问", config)
    other = chat_config("t-1", "s2")
    ask(agent, "别的会话", other)
    assert len(agent.get_state(config).values["messages"]) == 4
    assert len(agent.get_state(other).values["messages"]) == 2


def test_chat_session_key_is_prefixed_and_defaulted() -> None:
    """会话键带 chat 前缀且与审查图线程区分；会话号空时给默认值。"""
    assert chat_session_key("t-1", "s1") == "chat:t-1:s1"
    assert chat_session_key("t-1", "") == "chat:t-1:default"
    assert chat_session_key("t-1", "s1") != "t-1"


def test_answer_from_state_returns_empty_answer_when_model_said_nothing() -> None:
    """模型这轮没吐正文（如只剩工具调用）→ 回答为空串，引用仍从工具返回记录汇总。"""
    state = {"messages": [HumanMessage("问"), _tool_message("第二条…", artifact=[{"kind": "clause", "ref": "第二条", "text": "原句"}])]}
    result = answer_from_state(state)
    assert result["answer"] == ""
    assert [c["ref"] for c in result["citations"]] == ["第二条"]


def test_system_prompt_carries_context_and_boundaries() -> None:
    """系统提示要带这份合同的上下文，并把"不改判定/没依据说答不了"写死。"""
    prompt = assistant.system_prompt(build_context(_record(report=_report())))
    assert "采购合同.pdf" in prompt and "预付款比例过高" in prompt
    assert "不改风险清单、评级与审批结论" in prompt
    assert "没有依据的问题" in prompt
