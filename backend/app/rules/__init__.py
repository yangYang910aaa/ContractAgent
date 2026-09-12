"""确定性规则引擎（包门面）。

输入合同模型与原文 → 输出风险清单 + 评级。纯函数、不调大模型，可离线单测。
风险等级约定：存在 high → 人工审批闸口；只有 medium/low → 有条件通过。

规则按类别拆成子模块，本文件只做门面，对外仍可 `from backend.app.rules import evaluate, ...`：

| 模块 | 职责 |
| --- | --- |
| constants.py | 政策阈值、字段与风险中文标签、必填口径、跨模块共用的口径正则 |
| locator.py | 摘录与定位：整句摘录、OCR 页标记、条款号回推、缺必填锚点 |
| fields.py | 字段级规则（金额/日期/政策阈值）、风险汇总入口、生效日推断 |
| template.py | 文档形态识别：空白模板、补充协议 |
| text_checks.py | 条款该不该写（验收/发票/担保/转包）与条款级检查入口 |
| text_data.py | 数据与个人信息合规 |
| text_penalty.py | 保密例外、违约金基数与上限 |
| annotate.py | 语境降级、文案修正、定位编排 |

依赖方向单向；新增规则按类别落到对应模块，不要写在本文件里。
"""

from __future__ import annotations

from backend.app.rules.annotate import annotate_open_ended_risks
from backend.app.rules.constants import (
    AMOUNT_TOLERANCE_RATIO,
    CONFIDENTIALITY_MAX_MONTHS,
    CORE_REQUIRED,
    FIELD_LABELS,
    HIGH_IF_MISSING,
    KIND_BASELINE,
    LIABILITY_CAP_MIN_PERCENT,
    PENALTY_DAILY_MAX_PERCENT,
    PERFORMANCE_BOND_MIN_TOTAL,
    PREPAY_MAX_PERCENT,
    RISK_LABELS,
    SUBCONTRACT_KINDS,
    TEXT_RULE_KINDS,
    WARRANTY_MIN_MONTHS,
)
from backend.app.rules.fields import evaluate, infer_effective_from_signature
from backend.app.rules.template import (
    annotate_template_risks,
    is_blank_template_suspect,
    is_supplementary_agreement,
)
from backend.app.rules.text_checks import text_rules
from backend.app.schemas import Grade, RiskItem, Severity

__all__ = [
    # 入口
    "evaluate",
    "text_rules",
    "grade_report",
    "annotate_template_risks",
    "annotate_open_ended_risks",
    "infer_effective_from_signature",
    "is_blank_template_suspect",
    "is_supplementary_agreement",
    # 口径与标签（前端/复核/评测共用）
    "RISK_LABELS",
    "FIELD_LABELS",
    "CORE_REQUIRED",
    "HIGH_IF_MISSING",
    "KIND_BASELINE",
    "TEXT_RULE_KINDS",
    "SUBCONTRACT_KINDS",
    "PREPAY_MAX_PERCENT",
    "WARRANTY_MIN_MONTHS",
    "LIABILITY_CAP_MIN_PERCENT",
    "CONFIDENTIALITY_MAX_MONTHS",
    "PENALTY_DAILY_MAX_PERCENT",
    "AMOUNT_TOLERANCE_RATIO",
    "PERFORMANCE_BOND_MIN_TOTAL",
    # 兼容：schema 枚举曾从本模块导出
    "Severity",
]


def grade_report(risks: list[RiskItem]) -> Grade:
    """按风险清单评级。

    映射：任一 high → fail（Phase 2 将据此触发 gate 人工审批）；
    只有 medium/low → conditional_pass；空清单 → pass。
    """
    if any(r.severity == Severity.high for r in risks):
        return Grade.fail
    return Grade.conditional_pass if risks else Grade.pass_
