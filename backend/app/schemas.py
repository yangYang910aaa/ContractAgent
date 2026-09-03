"""核心数据结构（Phase 1 定死，全流水线共用）。

约定：extract / rules / policy_rag / graph / report 全部只依赖本模块，
字段增删必须同步更新评测 ground truth 与前端展示。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """风险等级。存在 high 时触发 gate（人工审批）。"""

    high = "high"
    medium = "medium"
    low = "low"


class Grade(str, Enum):
    """合同整体评级。"""

    pass_ = "pass"
    conditional_pass = "conditional_pass"
    fail = "fail"


class PaymentTerm(BaseModel):
    """付款期次：名称 + 金额 + 占总额比例（用于金额一致性/预付款比例规则）。"""

    name: str = ""
    amount: Decimal | None = None
    percent: float | None = None
    evidence: str = ""


class Evidence(BaseModel):
    """字段抽取证据：原文摘录 + 条款引用 + 置信度。"""

    quote: str = ""
    clause_ref: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_human_review: bool = False  # 低置信度标黄，归入"需人工确认"


class ContractModel(BaseModel):
    """从合同中抽取的结构化字段。

    extraction_meta 与字段一一对应（key=字段名），保证"每个字段带证据"。
    金额统一为 Decimal（元）；比例统一为百分比数值（如 0.05 = 0.05% 每日）。
    """

    buyer: str | None = None
    supplier: str | None = None
    signature_date: date | None = None
    effective_date: date | None = None
    expiry_date: date | None = None
    total_amount: Decimal | None = None
    currency: str | None = None
    payment_schedule: list[PaymentTerm] = Field(default_factory=list)
    penalty_rate: float | None = None  # 逾期违约金：每日百分比（1.5 = 日 1.5%）
    liability_cap: float | None = None  # 责任上限：占合同总额百分比（100 = 全额）
    warranty_months: int | None = None
    termination_notice_days: int | None = None
    ip_ownership: str | None = None
    confidentiality_months: int | None = None
    governing_law: str | None = None

    # 字段级证据（extractor 填写，rules/报告引用）
    extraction_meta: dict[str, Evidence] = Field(default_factory=dict)


class RiskItem(BaseModel):
    """一条风险：类型/等级/条款引用/证据/政策引用/建议。

    field 关联 ContractModel 字段名，供前端高亮与评测对齐。
    """

    risk_type: str
    severity: Severity
    clause_ref: str = ""
    evidence: str = ""
    policy_ref: str | None = None  # P-01..P-05，来自政策库检索
    suggestion: str = ""
    field: str | None = None


class ApprovalRecord(BaseModel):
    """HITL 审批结果（Phase 2 使用，先定结构保持 Report 稳定）。"""

    action: Literal["approved", "rejected", "edited"] = "approved"
    reviewer_note: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class Report(BaseModel):
    """审核报告：抽取结果 + 风险清单 + 评级 + 审批记录。

    review_mode 是多智能体决策钩子（决策记录见执行计划.md）：
    single=单审（默认）/ double=主审+盲审复核 / parallel=条款并行专家。
    """

    contract_id: str = ""  # 任务/thread 标识（Phase 2 绑定 LangGraph thread_id）
    source_file: str = ""
    contract_title: str = ""
    review_mode: Literal["single", "double", "parallel"] = "single"
    extracted: ContractModel = Field(default_factory=ContractModel)
    risks: list[RiskItem] = Field(default_factory=list)
    grade: Grade | None = None
    approval: ApprovalRecord | None = None
    created_at: datetime = Field(default_factory=datetime.now)
