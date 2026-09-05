"""graph单测: LangGraph 图 + HITL 闸口，全部离线（假抽取/假检索）。"""

from datetime import date
from decimal import Decimal

import pytest

from backend.app.config import BASE_DIR
from backend.app.graph import ReviewRunner
from backend.app.policy_rag import PolicyHit
from backend.app.schemas import ContractModel, PaymentTerm


def _normal_model() -> ContractModel:
    """等价无缺陷企业合同：质保 24/违约金 0.05/保密 24/IP 归甲方——零风险。"""
    return ContractModel(
        buyer="星辰智造科技有限公司",
        supplier="华芯电子有限公司",
        signature_date=date(2026, 3, 10),
        effective_date=date(2026, 3, 10),
        expiry_date=date(2027, 3, 9),
        total_amount=Decimal("1000000"),
        currency="人民币",
        payment_schedule=[PaymentTerm(name="验收后支付", amount=Decimal("1000000"), percent=100.0)],
        penalty_rate=0.05,
        liability_cap=100.0,
        warranty_months=24,
        confidentiality_months=24,
        ip_ownership="定制成果知识产权归甲方所有",
        governing_law="中华人民共和国法律",
    )


def _defect_model() -> ContractModel:
    """等价缺陷合同（gov 品类）：质保 6 个月 + 违约金日 1.5% → 两条 high。"""
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


class FakeRetriever:
    """假政策检索：记录 query，按关键词回 P-02/P-03 命中（离线可跑）。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[PolicyHit]:
        self.queries.append(query)
        ref = "P-02" if "质保" in query else "P-03"
        return [PolicyHit(policy_ref=ref, source=f"{ref}.md", text=f"{ref} 制度条文", score=0.9)]


def _runner(model: ContractModel) -> tuple[ReviewRunner, FakeRetriever]:
    retriever = FakeRetriever()
    runner = ReviewRunner(extractor=lambda text: model, retriever=retriever)
    return runner, retriever


def test_normal_contract_passes_without_gate() -> None:
    """无缺陷合同应一路到底：done + pass + 无审批记录、无闸口。"""
    runner, retriever = _runner(_normal_model())
    state = runner.start("sample_01.md", text="第一条 无缺陷正文")
    assert runner.store.get(runner.last_thread_id).status == "done"
    assert state["report"]["grade"] == "pass"
    assert state["report"]["approval"] is None
    assert runner.pending(runner.last_thread_id) is None
    assert retriever.queries == []  # 零风险 → 无需政策检索


def test_defect_stops_at_gate_then_approved() -> None:
    """缺陷合同应停在闸口；approve 恢复后报告保留风险、评级 fail、审批留痕。"""
    runner, retriever = _runner(_defect_model())
    state = runner.start("sample_07.md", text="质保 6 个月，违约金日 1.5%")
    tid = runner.last_thread_id
    assert runner.store.get(tid).status == "gate"
    payload = runner.pending(tid)
    assert payload and len(payload["high_risks"]) == 2
    assert state.get("report") is None  # 还没到报告节点
    # 审批放行
    state = runner.resume(tid, action="approved", note="人工复核后放行")
    assert runner.store.get(tid).status == "done"
    assert state["report"]["grade"] == "fail"
    assert state["report"]["approval"]["action"] == "approved"
    assert state["report"]["approval"]["reviewer_note"] == "人工复核后放行"
    # 政策检索针对两条带 policy_ref 的风险各查一次
    assert sorted(h["policy_ref"] for h in state["report"]["policy_hits"]) == ["P-02", "P-03"]


def test_gate_rejected_records_note() -> None:
    """打回（rejected）也应写入审批记录并可追溯。"""
    runner, _ = _runner(_defect_model())
    runner.start("sample_07.md", text="质保 6 个月，违约金日 1.5%")
    tid = runner.last_thread_id
    state = runner.resume(tid, action="rejected", note="质保期不足，打回重谈")
    approval = state["report"]["approval"]
    assert approval["action"] == "rejected"
    assert approval["reviewer_note"] == "质保期不足，打回重谈"


def test_edited_patch_reruns_and_gates_again() -> None:
    """edited 应打补丁回 rules 重审：质保改 24 后该条消失，违约金仍 high → 二次闸口。"""
    runner, _ = _runner(_defect_model())
    runner.start("sample_07.md", text="质保 6 个月，违约金日 1.5%")
    tid = runner.last_thread_id
    # 第一次审批：只修质保（24 个月），违约金不管
    state = runner.resume(tid, action="edited", note="质保改为 24 个月", patches={"warranty_months": 24})
    assert runner.store.get(tid).status == "gate"  # 违约金仍 high → 再次停闸口
    assert runner.store.get(tid).gate_payload["high_risks"][0]["risk_type"] == "penalty_rate_too_high"
    # 第二次审批：放行 → 报告只剩违约金一条风险，审批历史两条
    state = runner.resume(tid, action="approved", note="违约金同意协商")
    report = state["report"]
    assert [r["risk_type"] for r in report["risks"]] == ["penalty_rate_too_high"]
    assert report["approval"]["action"] == "approved"
    assert len(state.get("approvals", [])) == 2
    assert state["approvals"][0]["action"] == "edited"


def test_extract_error_goes_error_report() -> None:
    """抽取抛异常不应停在闸口，应产出带 error 的报告（批处理可定位坏文件）。"""
    def _boom(text: str) -> ContractModel:
        raise RuntimeError("LLM 超时")

    runner = ReviewRunner(extractor=_boom, retriever=lambda q: [])
    state = runner.start("bad.md", text="正文")
    tid = runner.last_thread_id
    assert runner.store.get(tid).status == "error"
    assert "抽取失败" in state["report"]["error"]
    assert runner.pending(tid) is None


def test_start_reads_file_when_no_text_given() -> None:
    """start 不给 text 时应按 source 读盘（真实上传链路形态）。"""
    sample_md = BASE_DIR / "data" / "contracts" / "sample_06_学生校服采购合同_正常.md"
    runner, _ = _runner(_normal_model())
    state = runner.start(str(sample_md))
    assert state["report"]["grade"] == "pass"
    assert runner.store.get(runner.last_thread_id).status == "done"


def test_resume_on_non_gate_raises() -> None:
    """对未停在闸口（已完成）的任务续跑应明确报错，防误操作。"""
    runner, _ = _runner(_normal_model())
    runner.start("ok.md", text="正常正文")
    with pytest.raises(ValueError):
        runner.resume(runner.last_thread_id, action="approved")
