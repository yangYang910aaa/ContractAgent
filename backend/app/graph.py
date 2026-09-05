"""
LangGraph 审核图：解析 → 抽取 → 规则 → 政策检索 → 评级 → HITL 闸口 → 报告。

"""

from __future__ import annotations

import argparse
import operator
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from backend.app.parser import extract_text
from backend.app.pipeline import enrich_policy_hits
from backend.app.rules import evaluate, grade_report
from backend.app.schemas import ContractModel, RiskItem
from backend.app.store import ThreadStore


class ReviewState(TypedDict, total=False):
    """图状态：全部字段可 JSON 序列化（MemorySaver 存储/checkpoint 需要）。

    约定：extracted/risks 存 json dict 而非 pydantic 对象，节点用前
    model_validate 还原——避免 pydantic 模型直接过序列化层的坑。
    """

    source: str  # 来源文件路径/标签
    text: str  # 抽取的合同全文
    extracted: dict  # ContractModel 的 json 形态（字段+证据）
    risks: list[dict]  # RiskItem json 列表
    policy_hits: list[dict]  # 政策引用（policy_ref/score/snippet）
    grade: str  # pass / conditional_pass / fail
    approval: dict | None  # 最近一次审批意见（approved/rejected/edited）
    approvals: Annotated[list[dict], operator.add]  # 审批历史（reducer 追加，编辑重审可多条）
    rerun: bool  # True=审批要求 edited，需回 rules 重审（edge 判定用）
    report: dict  # 最终报告（JSON 可序列化）
    error: str  # 抽取/图执行错误信息
    review_mode: str  # single / double / parallel（多智能体决策钩子，默认 single）


def _risks_from_dicts(risks: list[dict]) -> list[RiskItem]:
    """state 里的风险 json → RiskItem 列表( rules/评级/检索都用模型形态)"""
    return [RiskItem.model_validate(r) for r in risks]


def _build_gate_payload(state: ReviewState) -> dict:
    """gate 中断载荷：把高风险摘要交给人工审批页/CLI 展示。"""
    high = [r for r in state.get("risks", []) if r.get("severity") == "high"]
    return {
        "ask": "检测到高风险项，请审批：approve=放行 / reject=打回 / edited=修改字段后重审",
        "grade": state.get("grade"),
        "high_risks": [
            {
                "risk_type": r.get("risk_type"),
                "clause_ref": r.get("clause_ref", ""),
                "evidence": (r.get("evidence") or "")[:120],
                "policy_ref": r.get("policy_ref"),
                "suggestion": r.get("suggestion", ""),
            }
            for r in high
        ],
    }


def _parse_approval(answer: Any) -> dict:
    """人工审批原始回答 → ApprovalRecord 形状的 dict。

    兼容两种形态: CLI/路由传 {action,note,patches}；已序列化的
    ApprovalRecord dict (reviewer_note/created_at)也能解析。action 缺省
    视为 approved (安全默认：放行也留痕)。
    """
    if isinstance(answer, dict):
        action = answer.get("action") or answer.get("reviewer_action") or "approved"
        note = answer.get("note") or answer.get("reviewer_note") or ""
        patches = answer.get("patches")
    else:
        action, note, patches = "approved", str(answer or ""), None
    # 分支：edited 必须有 patches 才生效；没有就当 approved 处理（防空转）
    if action != "edited":
        patches = None
    return {
        "action": "approved" if action not in ("rejected", "edited") else action,
        "reviewer_note": note,
        "patches": patches,
    }


def build_review_graph(
    extractor: Callable[[str], ContractModel] | None = None,
    retriever: Callable[[str], list[Any]] | None = None,
    checkpointer: Any = None,
) -> Any:
    """构造 LangGraph 审核图 

    extractor: text -> ContractModel (默认 extract_contract 真 LLM)  
    retriever: query -> PolicyHit 列表 (默认 pipeline.enrich 的默认检索)。
    checkpointer: MemorySaver 等；不传也能跑，但 interrupt/HITL 必须配
    checkpointer (LangGraph 硬约束，见 docs/问题与踩坑记录.md)。
    返回 compiled graph。
    """
    from backend.app.extractor import extract_contract  # 延迟导入：防循环（extractor 不依赖 graph）

    extract = extractor or (lambda text: extract_contract(text=text))

    # ---- 节点（每个函数返回要写入 state 的字段子集）----

    def parse_node(state: ReviewState) -> dict:
        """读取来源文件为全文 (text 已在 state 则跳过，兼容路由预取)"""
        # 这种情况是：调用方已传 text（如测试直喂）→ 不再读盘
        if state.get("text"):
            return {}
        return {"text": extract_text(state["source"])}

    def extract_node(state: ReviewState) -> dict:
        """LLM 结构化抽取；失败不中断图，置 error 由条件边走错误出口。"""
        try:
            model = extract(state.get("text") or "")
        except Exception as exc:  # LLM/解析异常 → 整份走 error 报告（不拖垮队列）
            return {"error": f"抽取失败：{exc}"}
        return {"extracted": model.model_dump(mode="json")}

    def rules_node(state: ReviewState) -> dict:
        """确定性规则审查：抽取结果 → 风险清单；重审循环也回到这里。"""
        model = ContractModel.model_validate(state["extracted"])
        risks = evaluate(model)
        return {"risks": [r.model_dump(mode="json") for r in risks], "rerun": False}

    def policy_node(state: ReviewState) -> dict:
        """为带 policy_ref 的风险检索政策原文（引用依据，检索失败不阻断）。"""
        risks = _risks_from_dicts(state.get("risks", []))
        hits = enrich_policy_hits(risks, retriever=retriever)
        return {"policy_hits": hits}

    def grade_node(state: ReviewState) -> dict:
        """按风险清单评级 (pass/conditional_pass/fail)"""
        risks = _risks_from_dicts(state.get("risks", []))
        return {"grade": grade_report(risks).value}

    def gate_node(state: ReviewState) -> dict:
        """HITL 闸口：任一 high → interrupt() 暂停等人工审批。

        恢复后按 action 分流: approved/rejected → 直达报告, edited →
        应用字段补丁并 rerun=True 回 rules 重审 (重审后仍 high 会再次暂停，
        审批历史 approvals 持续累积，防无限循环靠人工闭环)。
        """
        payload = _build_gate_payload(state)
        answer = interrupt(payload)
        approval = _parse_approval(answer)
        # 分支 1：edited → 打补丁回重审（approval 暂置 None，等重审后新闸口再留痕）
        if approval["action"] == "edited":
            patches = approval.get("patches") or {}
            base = ContractModel.model_validate(state["extracted"])
            try:
                updated = base.model_copy(update=patches)
            except Exception as exc:  # 补丁类型不合法 → 保留原字段但注明，避免图崩溃
                updated = base
                approval["reviewer_note"] = (approval.get("reviewer_note") or "") + f"（补丁未生效：{exc}）"
            return {
                "extracted": updated.model_dump(mode="json"),
                "approvals": [approval],
                "approval": None,
                "rerun": True,
            }
        # 分支 2：approved/rejected → 直接进报告，意见留痕
        return {"approval": approval, "approvals": [approval]}

    def report_node(state: ReviewState) -> dict:
        """汇总最终报告（抽取/风险/政策引用/评级/审批意见），输出 JSON。"""
        return {
            "report": {
                "contract_file": state.get("source", ""),
                "grade": state.get("grade"),
                "risks": state.get("risks", []),
                "policy_hits": state.get("policy_hits", []),
                "extracted": state.get("extracted"),
                "approval": state.get("approval"),
                "review_mode": state.get("review_mode", "single"),
                "status": "done",
            }
        }

    def error_node(state: ReviewState) -> dict:
        """抽取/图执行失败的落点：报告带 error，批处理可定位坏文件。"""
        return {
            "report": {
                "contract_file": state.get("source", ""),
                "grade": None,
                "risks": [],
                "policy_hits": [],
                "extracted": state.get("extracted"),
                "error": state.get("error", "未知错误"),
                "status": "error",
            }
        }

    # ---- 组装：直线链路 + 两处条件分流 ----
    builder = StateGraph(state_schema=ReviewState)
    builder.add_node("parse", parse_node)
    builder.add_node("extract", extract_node)
    builder.add_node("rules", rules_node)
    builder.add_node("policy", policy_node)
    builder.add_node("grade", grade_node)
    builder.add_node("gate", gate_node)
    builder.add_node("report", report_node)
    builder.add_node("error", error_node)

    builder.add_edge(START, "parse")
    builder.add_edge("parse", "extract")
    # 这种情况是：抽取失败 → 走 error 出口；成功 → 继续规则审查
    builder.add_conditional_edges(
        "extract",
        lambda s: "error" if s.get("error") else "rules",
        {"error": "error", "rules": "rules"},
    )
    builder.add_edge("rules", "policy")
    builder.add_edge("policy", "grade")
    # 这种情况是：存在 high（评级 fail）→ 停闸口等人工；否则直达报告
    builder.add_conditional_edges(
        "grade",
        lambda s: "gate" if any(r.get("severity") == "high" for r in s.get("risks", [])) else "report",
        {"gate": "gate", "report": "report"},
    )
    # 这种情况是：审批要求 edited → 回 rules 重审, approved/rejected → 报告
    builder.add_conditional_edges(
        "gate",
        lambda s: "rules" if s.get("rerun") else "report",
        {"rules": "rules", "report": "report"},
    )
    builder.add_edge("report", END)
    builder.add_edge("error", END)
    return builder.compile(checkpointer=checkpointer)


class ReviewRunner:
    """审核运行器: graph + MemorySaver checkpointer + 任务登记簿，封装开始/续跑。"""

    def __init__(
        self,
        extractor: Callable[[str], ContractModel] | None = None,
        retriever: Callable[[str], list[Any]] | None = None,
        review_mode: str = "single",
    ) -> None:
        self.review_mode = review_mode
        self.checkpointer = MemorySaver()
        # checkpointer 在建图时传入：interrupt/恢复依赖它保存线程状态
        self.graph = build_review_graph(
            extractor=extractor, retriever=retriever, checkpointer=self.checkpointer
        )
        self.store = ThreadStore()
        self.last_thread_id: str = ""  # 最近一次 start 的 thread_id 

    def _config(self, thread_id: str) -> dict:
        """LangGraph 配置: thread_id 是 checkpointer 持久化键; recursion_limit 防失控循环。"""
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}

    def _finish(self, thread_id: str, state: dict) -> dict:
        """start/resume 后统一收尾：按状态更新任务登记并返回最新 state。"""
        interrupted = state.get("__interrupt__")
        if interrupted:
            payload = interrupted[0].value if isinstance(interrupted, list) else interrupted
            self.store.update(thread_id, status="gate", gate_payload=payload)
        elif state.get("error") or (state.get("report") or {}).get("status") == "error":
            self.store.update(thread_id, status="error", error=state.get("error", ""))
        elif state.get("report"):
            self.store.update(thread_id, status="done", report=state["report"])
        return state

    def start(self, source: str, text: str | None = None, thread_id: str | None = None) -> dict:
        """发起一份合同的审核: 登记任务 → 跑图（可能停在 gate 等审批）。

        thread_id 缺省时新建任务; 队列/路由先登记的场景传入既有 thread_id，
        避免同一任务被登记两次（登记簿与 checkpointer 必须同键）。
        """
        if thread_id is None:
            record = self.store.create(source)
            tid = record.thread_id
        else:
            record = self.store.get(thread_id)
            # 这种情况是: thread_id 不存在（非法调用）→ 明确报错
            if record is None:
                raise ValueError(f"任务不存在: {thread_id}")
            tid = thread_id
            # 登记时可能还没存 source（先建任务后补路径），为空则补上
            if not record.source:
                self.store.update(tid, source=source)
        self.last_thread_id = tid
        init: dict = {"source": source, "review_mode": self.review_mode}
        if text is not None:
            init["text"] = text
        state = self.graph.invoke(init, self._config(tid))
        return self._finish(tid, state)

    def pending(self, thread_id: str) -> dict | None:
        """取待审批载荷 (gate_payload)  非 gate 状态返回 None。"""
        record = self.store.get(thread_id)
        return record.gate_payload if record and record.status == "gate" else None

    def resume(
        self,
        thread_id: str,
        action: str = "approved",
        note: str = "",
        patches: dict | None = None,
    ) -> dict:
        """对停在 gate 的任务恢复审批: approved/rejected/edited(带 patches)。"""
        record = self.store.get(thread_id)
        # 这种情况是：任务不在 gate（未开始/已完成/已失败）→ 不允许续跑
        if record is None or record.status != "gate":
            raise ValueError(f"任务 {thread_id} 不在待审批状态（当前 {record.status if record else '未知'}) ")
        answer = {"action": action, "note": note, "patches": patches}
        state = self.graph.invoke(Command(resume=answer), self._config(thread_id))
        return self._finish(thread_id, state)


def _print_summary(state: dict) -> None:
    """CLI 剧本 2: 打印最终报告摘要（文件/评级/风险/审批）。"""
    report = state.get("report") or {}
    print(f"文件: {report.get('contract_file', '')}")
    print(f"评级: {report.get('grade')}")
    for risk in report.get("risks", []):
        print(f"  风险: {risk.get('risk_type')} | {risk.get('severity')} | 条款: {risk.get('clause_ref')} | 政策: {risk.get('policy_ref')}")
    approval = report.get("approval")
    if approval:
        print(f"审批: {approval.get('action')} | 意见: {approval.get('reviewer_note')}")
    if report.get("error"):
        print(f"错误: {report['error']}")


def main(argv: list[str] | None = None) -> int:
    """CLI 剧本 2: 跑一份缺陷样本 → 停在闸口 → 等审批输入 → 输出带审批记录的报告。"""
    parser = argparse.ArgumentParser(description="LangGraph 审核图 + HITL 演示")
    parser.add_argument("path", help="合同文件路径 (默认 sample_03)", nargs="?")
    parser.add_argument("--action", choices=["approved", "rejected"], default="approved", help="自动审批动作（默认 approved) ")
    parser.add_argument("--note", default="人工复核后放行 (CLI 演示)", help="审批意见")
    args = parser.parse_args(argv)

    path = args.path or str(
        sorted((Path(__file__).resolve().parents[2] / "data" / "contracts").glob("sample_03_*.md"))[0]
    )
    runner = ReviewRunner()
    state = runner.start(path)
    tid = runner.last_thread_id
    # 这种情况是：图停在闸口 → 展示载荷并恢复；未停（正常/错误）→ 直接出结果
    payload = runner.pending(tid)
    if payload:
        print(f"[gate] 任务 {tid} 待审批，载荷：{payload.get('ask')}")
        for item in payload.get("high_risks", []):
            print(f"   - {item.get('risk_type')} | {item.get('clause_ref')} | {item.get('policy_ref')}")
        state = runner.resume(tid, action=args.action, note=args.note)
    print("---- 报告 ----")
    _print_summary(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
