"""MCP 服务端测试：四个工具的契约与参数校验。

用 SDK 的内存 transport 走真协议（initialize / list_tools / call_tool），
不起真客户端、不联网、不调模型——抽取与检索都注入假实现。
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from backend.app.config import BASE_DIR
from backend.app.mcp_server import create_server
from backend.app.policy.rag import PolicyHit
from backend.app.review.graph import ReviewRunner
from backend.app.schemas import ContractModel, PaymentTerm
from backend.app.tasks.manager import TaskManager

TOOL_NAMES = {"submit_contract", "get_report", "ask_policy", "ask_contract"}


def _clean_model() -> ContractModel:
    """干净合同：必填齐全、无缺陷，跑完是 pass、不进闸口。"""
    return ContractModel(
        contract_kind="enterprise_goods",
        buyer="晨光实验中学",
        supplier="星海办公设备有限公司",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 3, 9),
        total_amount=Decimal("120000"),
        currency="人民币",
        payment_schedule=[PaymentTerm(name="验收款", amount=Decimal("120000"), percent=100.0)],
        warranty_months=24,
        confidentiality_months=24,
    )


@pytest.fixture()
def manager() -> TaskManager:
    """worker=False + 假抽取：任务由测试自己 run_one，不存在并发时序问题。"""
    runner = ReviewRunner(
        extractor=lambda text: _clean_model(),
        retriever=lambda query: [PolicyHit(policy_ref="P-02", source="P-02.md", text="质保条文", score=0.9)],
    )
    return TaskManager(runner=runner, worker=False)


@asynccontextmanager
async def _client(manager: TaskManager):
    """内存 transport 上的客户端会话：走真协议（initialize → list_tools → call_tool）。"""
    async with InMemoryTransport(create_server(manager=manager)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _call(session: ClientSession, name: str, arguments: dict) -> dict[str, Any]:
    """调一个工具并解出结果：优先结构化内容，退回正文里的 JSON。"""
    result = await session.call_tool(name, arguments)
    if result.structured_content:
        return result.structured_content
    return json.loads(getattr(result.content[0], "text", "{}"))


def test_tools_are_declared(manager: TaskManager) -> None:
    """四个工具都在，且都带描述（客户端靠描述决定什么时候调）。"""
    async def run() -> None:
        async with _client(manager) as session:
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == TOOL_NAMES
            assert all(tool.description for tool in tools.tools)

    asyncio.run(run())


def test_submit_then_report_round_trip(manager: TaskManager) -> None:
    """提交本机合同 → 拿到任务号；跑完后取报告，字段/评级都在。"""
    sample = BASE_DIR / "data" / "contracts" / "sample_01_电子元件采购合同_正常.md"

    async def run() -> None:
        async with _client(manager) as session:
            submitted = await _call(session, "submit_contract", {"path": str(sample)})
            assert submitted["status"] == "pending"
            thread_id = submitted["thread_id"]
            # 提交只登记入队：worker 关着，此刻还是 pending
            assert (await _call(session, "get_report", {"thread_id": thread_id}))["status"] == "pending"
            await asyncio.to_thread(manager.run_one, thread_id)
            report = await _call(session, "get_report", {"thread_id": thread_id})
            assert report["status"] == "done"
            assert report["file"] == sample.name
            # 评级与风险清单都随报告回传（具体判几条由规则决定，这里只钉契约）
            assert report["grade"] in {"pass", "conditional_pass", "fail"}
            assert report["fields"]["buyer"] == "晨光实验中学"
            assert isinstance(report["risks"], list)

    asyncio.run(run())


def test_submit_rejects_bad_input(manager: TaskManager, tmp_path: Path) -> None:
    """路径不存在 / 格式不支持 / 模式不认识 → 回错误对象，不登记任务。"""
    bad_format = tmp_path / "合同.docx.exe"
    bad_format.write_bytes(b"x")

    async def run() -> None:
        async with _client(manager) as session:
            assert "不存在" in (await _call(session, "submit_contract", {"path": str(tmp_path / "没有.md")}))["error"]
            assert "不支持" in (await _call(session, "submit_contract", {"path": str(bad_format)}))["error"]
            assert "模式" in (
                await _call(
                    session,
                    "submit_contract",
                    {"path": str(BASE_DIR / "data" / "contracts" / "sample_01_电子元件采购合同_正常.md"), "review_mode": "parallel"},
                )
            )["error"]
            assert manager.runner.store.list_records() == []

    asyncio.run(run())


def test_get_report_unknown_thread(manager: TaskManager) -> None:
    """任务号不存在 → 回错误对象（不抛异常，免得打断客户端整轮对话）。"""
    async def run() -> None:
        async with _client(manager) as session:
            assert "不存在" in (await _call(session, "get_report", {"thread_id": "nope"}))["error"]

    asyncio.run(run())


def test_get_report_gate_branch() -> None:
    """停在闸口：报告还没生成，但待审高风险照样用 risks 回给调用方（不必按状态换字段名）。"""
    def _defect() -> ContractModel:
        """缺陷合同：质保 6 个月 + 违约金日 1.5% → 两条 high，必停闸口。"""
        return ContractModel(
            contract_kind="enterprise_goods",
            buyer="晨光实验中学",
            supplier="星海软件有限公司",
            effective_date=date(2026, 3, 10),
            expiry_date=date(2027, 3, 9),
            total_amount=Decimal("300000"),
            currency="人民币",
            payment_schedule=[PaymentTerm(name="验收款", amount=Decimal("300000"), percent=100.0)],
            warranty_months=6,
            penalty_rate=1.5,
        )

    runner = ReviewRunner(extractor=lambda text: _defect(), retriever=lambda query: [])
    gate_manager = TaskManager(runner=runner, worker=False)
    sample = BASE_DIR / "data" / "contracts" / "sample_07_学生校服采购合同_质保过短_违约金畸高.md"

    async def run() -> None:
        async with _client(gate_manager) as session:
            submitted = await _call(session, "submit_contract", {"path": str(sample)})
            await asyncio.to_thread(gate_manager.run_one, submitted["thread_id"])
            out = await _call(session, "get_report", {"thread_id": submitted["thread_id"]})
            assert out["status"] == "gate"
            assert out["risks"] and all(risk["severity"] == "high" for risk in out["risks"])
            assert "审批" in out["awaiting"]

    asyncio.run(run())


def test_ask_policy_returns_hits(manager: TaskManager, monkeypatch) -> None:
    """问政策库：走检索并把命中整形成 编号/出处/原文/分值。"""
    import backend.app.mcp_server as mcp_server

    seen: dict = {}

    def fake_retrieve(question: str, k: int = 3):
        seen["question"] = question
        seen["k"] = k
        return [PolicyHit(policy_ref="P-02", source="P-02_质量保证期.md", text="质保不低于 12 个月", score=0.87654)]

    monkeypatch.setattr(mcp_server, "retrieve_policies", fake_retrieve)

    async def run() -> None:
        async with _client(manager) as session:
            out = await _call(session, "ask_policy", {"question": "质保期要求", "k": 2})
            assert seen == {"question": "质保期要求", "k": 2}
            assert out["hits"][0]["policy_ref"] == "P-02"
            # 排序分只用于排序：混合检索下是 RRF 融合分，字段名不叫 score 免得被当相似度读
            assert out["hits"][0]["rank_score"] == 0.8765
            assert (await _call(session, "ask_policy", {"question": "  "}))["error"]

    asyncio.run(run())


def test_ask_contract_answers_with_context(manager: TaskManager, monkeypatch) -> None:
    """问合同：把问题交给对话图，答案与引用原样带回；解析未完成时提前拒绝。"""
    import backend.app.assistant as assistant

    class _FakeAgent:
        def __init__(self) -> None:
            self.seen: dict = {}

        async def ainvoke(self, payload: dict, config: dict) -> dict:
            self.seen = {"payload": payload, "config": config}
            return {"messages": [HumanMessage("为什么判通过？"), AIMessage("因为必填字段齐全、无高风险。")]}

    agent = _FakeAgent()
    monkeypatch.setattr(assistant, "build_chat_agent", lambda context, checkpointer=None: agent)

    sample = BASE_DIR / "data" / "contracts" / "sample_01_电子元件采购合同_正常.md"

    async def run() -> None:
        async with _client(manager) as session:
            submitted = await _call(session, "submit_contract", {"path": str(sample)})
            thread_id = submitted["thread_id"]
            # 还没跑 → 助手没有可依据的内容，提前拒绝（省一次调用）
            early = await _call(session, "ask_contract", {"thread_id": thread_id, "question": "为啥判通过？"})
            assert "解析" in early["error"]
            await asyncio.to_thread(manager.run_one, thread_id)
            out = await _call(session, "ask_contract", {"thread_id": thread_id, "question": "为啥判通过？"})
            assert out["answer"] == "因为必填字段齐全、无高风险。"
            assert agent.seen["payload"]["messages"][0]["content"] == "为啥判通过？"
            assert agent.seen["config"]["configurable"]["thread_id"] == f"chat:{thread_id}:mcp"

    asyncio.run(run())
