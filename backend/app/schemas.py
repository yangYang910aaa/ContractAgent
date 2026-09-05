"""核心数据结构。

约定:extract / rules / policy_rag / graph / report 全部只依赖本模块，
字段增删必须同步更新评测 ground truth 与前端展示。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """风险等级。存在 high 时触发 gate(人工审批)。"""

    high = "high"#  高风险
    medium = "medium"#  中风险
    low = "low"#  低风险

class Grade(str, Enum):
    """合同整体评级。"""

    pass_ = "pass"#  合格
    conditional_pass = "conditional_pass" #  部分条件合格
    fail = "fail"#  不合格


class PaymentTerm(BaseModel):
    """付款期次：名称 + 金额 + 占总额比例（用于金额一致性/预付款比例规则）。"""

    name: str = ""  # 付款期次名称
    amount: Decimal | None = None  # 付款金额（元）
    percent: float | None = None  # 占总额比例（如 0.05 = 0.05% 每日）
    evidence: str = ""  # 证据说明


class Evidence(BaseModel):
    """字段抽取证据：原文摘录 + 条款引用 + 置信度。"""

    quote: str = ""  # 原文摘录
    clause_ref: str = ""  # 条款引用
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # 置信度（0-1）
    needs_human_review: bool = False  # 低置信度标黄，归入"需人工确认


class ContractModel(BaseModel):
    """从合同中抽取的结构化字段。

    extraction_meta 与字段一一对应(key=字段名)，保证"每个字段带证据"。
    金额统一为 Decimal(元)；比例统一为百分比数值（如 0.05 = 0.05% 每日）。
    """

    buyer: str | None = None  # 采购方名称
    # 合同品类：规则按品类判断"应含条款"基线（如校服类天然不含责任上限/保密条款，
    # 企业采购默认含全）；None 按 enterprise_goods 处理（向后兼容）
    contract_kind: Literal["enterprise_goods", "gov_goods", "agri_goods", "tech_service"] | None = None
    supplier: str | None = None  # 供应商名称
    signature_date: date | None = None  # 合同签订日期
    effective_date: date | None = None  # 合同生效日期
    expiry_date: date | None = None  # 合同过期日期
    total_amount: Decimal | None = None  # 合同总额（元）
    currency: str | None = None  # 合同货币（如 CNY）
    payment_schedule: list[PaymentTerm] = Field(default_factory=list)
    penalty_rate: float | None = None  # 逾期违约金：每日百分比（1.5 = 日 1.5%）
    liability_cap: float | None = None  # 责任上限：占合同总额百分比（100 = 全额）
    warranty_months: int | None = None  # 保修期（月）
    termination_notice_days: int | None = None  # 终止通知期（天）
    ip_ownership: str | None = None  # IP 权属（如 "采购方"）
    confidentiality_months: int | None = None  # 保密期（月）
    governing_law: str | None = None  # 适用法律（如 "中国法律"）

    # 字段级证据（extractor 填写，rules/报告引用）
    extraction_meta: dict[str, Evidence] = Field(default_factory=dict)


class RiskItem(BaseModel):
    """一条风险：类型/等级/条款引用/证据/政策引用/建议。

    field 关联 ContractModel 字段名，供前端高亮与评测对齐。
    """

    risk_type: str  # 风险类型机器码（如 missing_required_field；评测 ground truth 按此对齐，勿改）
    label: str = ""  # 风险中文展示名（rules 填，UI 直接展示；空时前端回退 risk_type）
    severity: Severity  # 风险等级（如 high）
    clause_ref: str = ""  # 条款引用
    evidence: str = ""  # 证据说明
    policy_ref: str | None = None  # 政策引用（如 "政策库检索结果）
    suggestion: str = ""  # 建议
    field: str | None = None  # 关联的 ContractModel 字段名


class ApprovalRecord(BaseModel):
    """HITL 审批结果 """

    action: Literal["approved", "rejected", "edited"] = "approved"  # 审批结果（默认 approved）
    reviewer_note: str = ""  # 审批备注
    created_at: datetime = Field(default_factory=datetime.now)  # 审批时间（默认当前时间）


class Report(BaseModel):
    """审核报告：抽取结果 + 风险清单 + 评级 + 审批记录。

    review_mode 是多智能体决策钩子：
    single=单审（默认）/ double=主审+盲审复核 / parallel=条款并行专家。
    """

    contract_id: str = ""  # 任务/thread 标识
    source_file: str = ""  # 合同文件路径
    contract_title: str = ""  # 合同标题
    review_mode: Literal["single", "double", "parallel"] = "single"  # 多智能体决策钩子（默认 single）
    extracted: ContractModel = Field(default_factory=ContractModel)  # 抽取结果
    risks: list[RiskItem] = Field(default_factory=list)  # 风险清单
    grade: Grade | None = None  # 合同整体评级
    approval: ApprovalRecord | None = None  # 审批记录
    created_at: datetime = Field(default_factory=datetime.now)  # 创建时间（默认当前时间）
