"""规则链入口：一次跑完整套规则。

图链路（在线）与离线链路共用这一个入口——规则顺序只在这里写一次，改链不用改两处。
"""

from __future__ import annotations

from backend.app.review.rules.annotate import annotate_open_ended_risks
from backend.app.review.rules.fields import evaluate
from backend.app.review.rules.template import annotate_template_risks
from backend.app.review.rules.text_checks import text_rules
from backend.app.schemas import ContractModel, RiskItem


def run_rules(model: ContractModel, text: str) -> list[RiskItem]:
    """抽取结果 + 原文 → 风险清单。

    字段级（evaluate，按品类挑基线）与文本级（text_rules）合并后，再过两道标注：
    开放式条款、空白模板与补充协议。后两道只做降级与补提示，不改判定口径。
    """
    return annotate_template_risks(
        annotate_open_ended_risks(evaluate(model) + text_rules(text, model.contract_kind), text),
        text,
    )
