"""FastAPI 任务路由（Phase 3）测试：上传/队列/详情/审批，全部离线（假抽取）。"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from backend.app.config import BASE_DIR
from backend.app.review.graph import ReviewRunner
from backend.app.main import create_app
from backend.app.policy.rag import PolicyHit
from backend.app.schemas import ContractModel, PaymentTerm
from backend.app.tasks.manager import TaskManager


def _defect_model() -> ContractModel:
    """缺陷模型：质保 6 个月 + 违约金 1.5% → 两条 high，必停闸口。"""
    return ContractModel(
        contract_kind="gov_goods",
        buyer="晨光实验中学",
        supplier="星海校服服饰有限公司",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 9, 30),
        total_amount=Decimal("198400"),
        currency="人民币",
        payment_schedule=[PaymentTerm(name="一次性付清", amount=Decimal("198400"), percent=100.0)],
        penalty_rate=1.5,
        warranty_months=6,
    )


def _fake_retriever(query: str) -> list[PolicyHit]:
    ref = "P-02" if "质保" in query else "P-03"
    return [PolicyHit(policy_ref=ref, source=f"{ref}.md", text=f"{ref} 条文", score=0.9)]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """app：worker=False + 假抽取；上传目录指到临时路径，测完不留文件。"""
    import backend.app.api.routes_tasks as routes

    monkeypatch.setattr(routes, "UPLOAD_DIR", tmp_path)
    runner = ReviewRunner(extractor=lambda text: _defect_model(), retriever=_fake_retriever)
    manager = TaskManager(runner=runner, worker=False)
    app = create_app(manager=manager)
    return TestClient(app)


def _sample_bytes() -> bytes:
    sample = BASE_DIR / "data" / "contracts" / "sample_07_学生校服采购合同_质保过短_违约金畸高.md"
    return sample.read_bytes()


def _sample_docx_bytes() -> bytes:
    """样本 docx 二进制：U2 上传 docx → 解析原文 → /source 返回全文。"""
    sample = BASE_DIR / "data" / "contracts" / "docx" / "sample_07_学生校服采购合同_质保过短_违约金畸高.docx"
    return sample.read_bytes()


def test_upload_then_processing_then_gate_and_approve(client: TestClient) -> None:
    """上传 → 入队 pending → run_one 到 gate → approve → done 报告带审批记录。"""
    resp = client.post("/api/tasks", files={"file": ("contract.md", _sample_bytes(), "text/markdown")})
    assert resp.status_code == 200
    tid = resp.json()["thread_id"]
    manager = client.app.state.manager
    assert manager.runner.store.get(tid).status == "pending"
    # 离线手动跑（worker=False 不自动处理）
    manager.run_one(tid)
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["status"] == "gate"
    assert len(detail["gate_payload"]["high_risks"]) == 2
    # 放行
    resp = client.post(f"/api/tasks/{tid}/approve", json={"note": "复核后放行"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["report"]["grade"] == "fail"
    assert body["report"]["approval"]["action"] == "approved"
    assert body["report"]["approval"]["reviewer_note"] == "复核后放行"


def test_upload_reject_flow(client: TestClient) -> None:
    """打回路径：reject + 原因写进报告。"""
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    client.app.state.manager.run_one(tid)
    resp = client.post(f"/api/tasks/{tid}/reject", json={"note": "质保期不足，打回重谈"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["report"]["approval"]["action"] == "rejected"
    assert body["report"]["approval"]["reviewer_note"] == "质保期不足，打回重谈"


def test_delete_done_task_removes_record_and_file(client: TestClient) -> None:
    """删除任务: 登记簿移除 + 上传落盘文件一并删 + 再查/再删都 404。"""
    tid = client.post(
        "/api/tasks", files={"file": ("删除用.md", _sample_bytes(), "text/markdown")}
    ).json()["thread_id"]
    manager = client.app.state.manager
    manager.run_one(tid)  # 缺陷模型 → gate（删除允许放弃待审批）
    file_path = Path(manager.runner.store.get(tid).source)
    assert file_path.exists()

    resp = client.delete(f"/api/tasks/{tid}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": tid}
    assert manager.runner.store.get(tid) is None
    assert not file_path.exists()  # 上传文件随任务删除
    assert client.get(f"/api/tasks/{tid}").status_code == 404
    assert client.delete(f"/api/tasks/{tid}").status_code == 404


def test_delete_processing_task_conflict(client: TestClient) -> None:
    """处理中的任务不可删(409)，避免删到一半的状态/文件。"""
    tid = client.post(
        "/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}
    ).json()["thread_id"]
    manager = client.app.state.manager
    manager.runner.store.update(tid, status="processing")
    resp = client.delete(f"/api/tasks/{tid}")
    assert resp.status_code == 409
    assert manager.runner.store.get(tid) is not None


def test_batch_delete_mixed_results(client: TestClient) -> None:
    """批量删除: 可删的删掉, 处理中/重复 id 跳过, 结果汇总返回。"""
    manager = client.app.state.manager
    deletable = []
    for _ in range(2):
        tid = client.post(
            "/api/tasks", files={"file": ("b.md", _sample_bytes(), "text/markdown")}
        ).json()["thread_id"]
        manager.run_one(tid)  # 缺陷模型 → gate（可删）
        deletable.append(tid)
    busy = client.post(
        "/api/tasks", files={"file": ("p.md", _sample_bytes(), "text/markdown")}
    ).json()["thread_id"]
    manager.runner.store.update(busy, status="processing")

    resp = client.post(
        "/api/tasks/batch-delete",
        json={"thread_ids": [*deletable, busy, deletable[0]]},  # 含重复 id
    )
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["deleted"]) == sorted(deletable)
    assert body["skipped"] == [{"thread_id": busy, "reason": "任务正在处理"}]
    for tid in deletable:
        assert manager.runner.store.get(tid) is None
    assert manager.runner.store.get(busy) is not None


def test_batch_delete_empty_list_422(client: TestClient) -> None:
    """空列表批量删除应 422(入参校验兜底, 防误清空)。"""
    resp = client.post("/api/tasks/batch-delete", json={"thread_ids": []})
    assert resp.status_code == 422


def test_edit_patch_reruns_and_second_gate(client: TestClient) -> None:
    """编辑重审：修质保 24 → 违约金仍 high → 二次闸口，再放行完成。"""
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    manager = client.app.state.manager
    manager.run_one(tid)
    resp = client.post(f"/api/tasks/{tid}/edit", json={"patches": {"warranty_months": 24}, "note": "质保改 24"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "gate"
    risk = resp.json()["gate_payload"]["high_risks"][0]
    assert risk["risk_type"] == "penalty_rate_too_high"
    resp = client.post(f"/api/tasks/{tid}/approve", json={"note": "违约金同意协商"})
    assert resp.json()["report"]["approval"]["action"] == "approved"


def test_upload_saves_readable_file_name(client: TestClient) -> None:
    """上传落盘文件名 = 任务号 + 原文件名：出问题时对着 uploads 目录能认出是哪份。"""
    import backend.app.api.routes_tasks as routes

    tid = client.post(
        "/api/tasks", files={"file": ("学生校服采购合同.md", _sample_bytes(), "text/markdown")}
    ).json()["thread_id"]
    source = Path(client.app.state.manager.runner.store.get(tid).source)
    assert source.name == f"{tid}_学生校服采购合同.md"
    assert source.is_file()
    # 原文件名里只剩符号时退化成只用任务号，也不该报错
    assert routes._upload_name("abc123", "///", ".md") == "abc123.md"
    # 路径分隔符与控制字符一并收敛，不能借文件名跳出上传目录
    assert "/" not in routes._upload_name("abc123", "..\\..\\坏`名", ".md")


def test_upload_unsupported_suffix_400(client: TestClient) -> None:
    """非白名单格式应 400 并给出原因。"""
    resp = client.post("/api/tasks", files={"file": ("a.xyz", b"x", "application/octet-stream")})
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


def test_approve_non_gate_returns_409(client: TestClient) -> None:
    """对还没到闸口（pending）的任务审批应 409，防误操作。"""
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    resp = client.post(f"/api/tasks/{tid}/approve", json={"note": ""})
    assert resp.status_code == 409


def test_get_missing_task_404(client: TestClient) -> None:
    assert client.get("/api/tasks/not-exist").status_code == 404


def test_list_tasks_summary(client: TestClient) -> None:
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    client.app.state.manager.run_one(tid)
    body = client.get("/api/tasks").json()
    tasks = {t["thread_id"]: t for t in body["tasks"]}
    assert tasks[tid]["status"] == "gate"
    assert tasks[tid]["source"] == "c.md"
    assert tasks[tid]["risk_count"] == 2  # gate 时 = 待审 high 数
    # 并发上限随列表返回（前端"并发 n"展示用）
    assert body["concurrency"] >= 1


def test_demo_enqueues_internal_samples(client: TestClient) -> None:
    """samples 接口直接入队内置合成样本（免上传，worker=False 时停在 pending）。"""
    resp = client.post("/api/tasks/samples", json={"count": 2})
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 2
    assert all(t["source"].startswith("sample_") for t in tasks)
    # 手动跑完第一份：假抽取器返回缺陷模型 → 停在闸口
    manager = client.app.state.manager
    manager.run_one(tasks[0]["thread_id"])
    detail = client.get(f"/api/tasks/{tasks[0]['thread_id']}").json()
    assert detail["status"] == "gate"
    assert detail["risk_count"] == 2


def test_demo_count_out_of_range_422(client: TestClient) -> None:
    assert client.post("/api/tasks/samples", json={"count": 99}).status_code == 422


# ---- U2：查看原合同（/source 全文+条款块、/file 原文件）----


def test_source_after_run_returns_text_and_blocks(client: TestClient) -> None:
    """上传 md → 跑完到闸口 → /source 返回解析全文与章节块（原文查看数据源）。"""
    tid = client.post("/api/tasks", files={"file": ("校服合同.md", _sample_bytes(), "text/markdown")}).json()[
        "thread_id"
    ]
    # 跑完前（pending）source_text 还没落库 → 空文本
    before = client.get(f"/api/tasks/{tid}/source").json()
    assert before["text"] == ""
    client.app.state.manager.run_one(tid)
    body = client.get(f"/api/tasks/{tid}/source").json()
    assert body["name"] == "校服合同.md"
    assert body["suffix"] == ".md"
    assert body["kind"] == "upload"
    assert body["file_available"] is True
    assert "学生校服" in body["text"]
    # 章节式样本：块 0 是前言，后面按「一、二、…」切
    assert body["blocks"][0]["title"] == "前言"
    assert any(b["title"].startswith("一、") for b in body["blocks"])


def test_source_demo_sample_kind_and_file_download(client: TestClient) -> None:
    """samples 样本 → kind=sample；/file 能取回原 md 二进制（下载/对照用）。"""
    resp = client.post("/api/tasks/samples", json={"count": 1})
    tid = resp.json()["tasks"][0]["thread_id"]
    client.app.state.manager.run_one(tid)
    src = client.get(f"/api/tasks/{tid}/source").json()
    assert src["kind"] == "sample"
    assert src["suffix"] == ".md"
    assert "电子元件" in src["text"] or "办公设备" in src["text"]
    file_resp = client.get(f"/api/tasks/{tid}/file")
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"].startswith("text/markdown")


def test_source_docx_upload_parses_full_text(client: TestClient) -> None:
    """docx 上传 → 解析出的章节全文能从 /source 取到（pdf/docx 预览的基础）。"""
    tid = client.post("/api/tasks", files={"file": ("校服合同.docx", _sample_docx_bytes(), "application/octet-stream")}).json()[
        "thread_id"
    ]
    client.app.state.manager.run_one(tid)
    body = client.get(f"/api/tasks/{tid}/source").json()
    assert body["suffix"] == ".docx"
    assert "校服" in body["text"]
    file_resp = client.get(f"/api/tasks/{tid}/file")
    assert file_resp.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_pdf_file_served_inline_for_preview(client: TestClient) -> None:
    """pdf 原文件必须是 inline（浏览器内嵌预览），不能是 attachment（否则变下载）。"""
    pdf = BASE_DIR / "data" / "contracts" / "pdf" / "sample_07_学生校服采购合同_质保过短_违约金畸高.pdf"
    tid = client.post("/api/tasks", files={"file": ("校服合同.pdf", pdf.read_bytes(), "application/pdf")}).json()[
        "thread_id"
    ]
    client.app.state.manager.run_one(tid)
    resp = client.get(f"/api/tasks/{tid}/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    # content-disposition 应为 inline；attachment 会让 iframe 预览变下载
    assert resp.headers["content-disposition"].startswith("inline")


def test_source_and_file_missing_task_404(client: TestClient) -> None:
    """不存在的任务：/source 与 /file 都回 404。"""
    assert client.get("/api/tasks/not-exist/source").status_code == 404
    assert client.get("/api/tasks/not-exist/file").status_code == 404


def test_upload_double_mode_registers_and_shows_in_list(client: TestClient) -> None:
    """上传带 review_mode=double：任务登记与列表/详情都回带该模式（服务双审入口）。"""
    resp = client.post(
        "/api/tasks",
        files={"file": ("c.md", _sample_bytes(), "text/markdown")},
        data={"review_mode": "double"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_mode"] == "double"
    manager = client.app.state.manager
    assert manager.runner.store.get(body["thread_id"]).review_mode == "double"
    # 列表与详情摘要统一带模式（前端队列行/详情页徽标数据源）
    listed = client.get("/api/tasks").json()["tasks"]
    assert any(t["thread_id"] == body["thread_id"] and t["review_mode"] == "double" for t in listed)
    detail = client.get(f"/api/tasks/{body['thread_id']}").json()
    assert detail["review_mode"] == "double"


def test_upload_invalid_review_mode_400(client: TestClient) -> None:
    """parallel 等未实现模式：上传直接 400，不落任务。"""
    resp = client.post(
        "/api/tasks",
        files={"file": ("c.md", _sample_bytes(), "text/markdown")},
        data={"review_mode": "parallel"},
    )
    assert resp.status_code == 400
    assert "review_mode" in resp.json()["detail"] or "审查模式" in resp.json()["detail"]


def test_samples_enqueue_with_double_mode(client: TestClient) -> None:
    """样本批量入队也支持 review_mode：登记簿落 double（回归/评测按需选双审）。"""
    resp = client.post("/api/tasks/samples", json={"count": 1, "review_mode": "double"})
    assert resp.status_code == 200
    tid = resp.json()["tasks"][0]["thread_id"]
    assert client.app.state.manager.runner.store.get(tid).review_mode == "double"


# ---- 对话助手路由：假模型 + 假检索，全程离线 ----


class _ScriptedChatModel(FakeMessagesListChatModel):
    """脚本化假模型（可流式）：按顺序吐给定消息，工具绑定是空操作。

    对话链路要逐 token 才有 on_chat_model_stream 事件，所以这里自己实现 _stream：
    正文按小块吐、工具轮只吐一块 tool_call_chunks（工具轮没有正文，不吐这块框架会
    报"流里没有内容"）。测试全程离线，不连模型接口。
    """

    def bind_tools(self, tools, **kwargs):
        """接收工具绑定但不改行为，返回自身。"""
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """按脚本吐片段：正文逐块（逐字效果），工具轮吐一块工具调用。"""
        message = self.responses[self.i]
        # 与基类 _generate 同口径推进下标：最后一条重复吐，脚本用尽不越界
        if self.i < len(self.responses) - 1:
            self.i += 1
        # 分支：工具轮 → 只吐一块 tool_call_chunks，框架据此发起工具调用
        if getattr(message, "tool_calls", None):
            call = message.tool_calls[0]
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call["args"], ensure_ascii=False),
                            "id": call["id"],
                            "index": 0,
                            "type": "tool_call_chunk",
                        }
                    ],
                )
            )
            return
        text = message.content if isinstance(message.content, str) else ""
        for start in range(0, len(text), 4):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[start : start + 4]))


def _install_chat_stub(monkeypatch, responses: list) -> None:
    """把对话图的模型与政策检索换成离线的：路由测试不发任何外部请求。

    脚本共用一个模型实例，按调用顺序往后走，多轮问答的应答才排得出先后。
    替身要打在真正用到这两个名字的模块上（模型在 graph、检索在 tools）。
    """
    from backend.app.assistant import graph as assistant_graph
    from backend.app.assistant import tools as assistant_tools

    model = _ScriptedChatModel(responses=responses)
    monkeypatch.setattr(assistant_graph, "get_chat_model", lambda *a, **k: model)
    monkeypatch.setattr(
        assistant_tools,
        "retrieve_policies",
        lambda query, k=3: [PolicyHit(policy_ref="P-01", source="P-01_预付款比例.md", text="## 第二条 上限\n30%", score=0.9)],
    )


def _chat_tool_call(keyword: str = "质保") -> AIMessage:
    """假模型的一轮工具调用（框架按 tool_calls 驱动工具）。"""
    return AIMessage(content="", tool_calls=[{"name": "find_clauses", "args": {"keyword": keyword}, "id": "c1"}])


def _gate_task(client: TestClient) -> str:
    """上传样本并跑到闸口（停闸口时也该能问"为什么判高风险"）。"""
    tid = client.post("/api/tasks", files={"file": ("c.md", _sample_bytes(), "text/markdown")}).json()["thread_id"]
    client.app.state.manager.run_one(tid)
    return tid


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """SSE 文本 → [(事件名, 数据)]，用来核对事件序列与载荷。"""
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        name, data = "", None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            # 分支：数据行 → 解析 JSON（事件载荷）
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name:
            events.append((name, data))
    return events


def test_chat_stream_emits_tokens_citations_and_usage(client: TestClient, monkeypatch) -> None:
    """流式问答：过程状态 → 逐 token → 引用 → 用量 → done，引用来自工具返回记录。"""
    _install_chat_stub(monkeypatch, [_chat_tool_call(), AIMessage("质保只有 6 个月，低于 P-02 的 12 个月下限。")])
    tid = _gate_task(client)
    resp = client.post(
        f"/api/tasks/{tid}/chat",
        json={"message": "为什么判高风险？", "session_id": "s1"},
        headers={"accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "status" and names[-1] == "done"
    assert "tool" in names and "citations" in names and "usage" in names
    assert names.index("citations") < names.index("usage") < names.index("done")
    # 逐 token：拼起来的正文就是最终答案
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert answer == events[-1][1]["answer"]
    assert answer.startswith("质保只有 6 个月")
    citations = next(data for name, data in events if name == "citations")
    assert [c["kind"] for c in citations["citations"]] and citations["citations"][0]["ref"]
    usage = next(data for name, data in events if name == "usage")
    assert usage["calls"] >= 1 and usage["seconds"] >= 0


def test_chat_returns_whole_answer_as_json_without_stream_accept(client: TestClient, monkeypatch) -> None:
    """不要事件流（前端读流失败降级）→ 同一路由回整段 JSON：回答 + 引用 + 用量。"""
    _install_chat_stub(monkeypatch, [_chat_tool_call(), AIMessage("质保只有 6 个月。")])
    tid = _gate_task(client)
    resp = client.post(f"/api/tasks/{tid}/chat", json={"message": "为什么判高风险？", "session_id": "s1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"].startswith("质保只有 6 个月")
    assert body["citations"] and body["unverified"] == []
    assert body["usage"]["calls"] >= 1


def test_chat_history_reads_back_turns(client: TestClient, monkeypatch) -> None:
    """历史回读：刷新页面后再打开面板，问答与引用都在（不用重新问一遍）。"""
    _install_chat_stub(monkeypatch, [_chat_tool_call(), AIMessage("质保只有 6 个月。")])
    tid = _gate_task(client)
    client.post(f"/api/tasks/{tid}/chat", json={"message": "为什么判高风险？", "session_id": "s1"})
    body = client.get(f"/api/tasks/{tid}/chat/s1").json()
    assert [turn["question"] for turn in body["turns"]] == ["为什么判高风险？"]
    assert body["turns"][0]["answer"] == "质保只有 6 个月。"
    assert body["turns"][0]["citations"][0]["kind"] == "clause"


def test_chat_history_is_per_session_and_cleared(client: TestClient, monkeypatch) -> None:
    """会话互相隔离；清空会话后回到空态（换个角度重新问/演示前重置）。"""
    _install_chat_stub(monkeypatch, [AIMessage("第一答。"), AIMessage("第二答。")])
    tid = _gate_task(client)
    client.post(f"/api/tasks/{tid}/chat", json={"message": "第一问", "session_id": "s1"})
    client.post(f"/api/tasks/{tid}/chat", json={"message": "第二问", "session_id": "s2"})
    assert client.get(f"/api/tasks/{tid}/chat/s1").json()["turns"][0]["answer"] == "第一答。"
    assert client.get(f"/api/tasks/{tid}/chat/s2").json()["turns"][0]["answer"] == "第二答。"
    assert client.delete(f"/api/tasks/{tid}/chat/s1").json()["cleared"] is True
    assert client.get(f"/api/tasks/{tid}/chat/s1").json()["turns"] == []


def test_chat_rejects_empty_question_and_unknown_task(client: TestClient, monkeypatch) -> None:
    """边界：空问题 400（不白花调用）、任务不存在 404。"""
    _install_chat_stub(monkeypatch, [AIMessage("答。")])
    tid = _gate_task(client)
    assert client.post(f"/api/tasks/{tid}/chat", json={"message": "   "}).status_code == 400
    assert client.post("/api/tasks/not-exist/chat", json={"message": "问"}).status_code == 404
    assert client.get("/api/tasks/not-exist/chat/s1").status_code == 404
    assert client.delete("/api/tasks/not-exist/chat/s1").status_code == 404
