"""报告引用的接地校验（确定性，不调模型）。

报告里的政策编号有两个来源：规则引擎按风险类型写死，与盲审模型自己填。这里对每条带
引用的风险执行同一套检查——编号是否存在、正文能否读到、编号与该风险类型是否对应、
该类型的判定阈值能否在原文里找到——结论写进报告的 `citation_checks` 段。
只做标注与统计，不改风险等级、不改是否并入，判定口径不受影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.app.policy_rag import POLICY_DIR
from backend.app.rules import (
    CONFIDENTIALITY_MAX_MONTHS,
    LIABILITY_CAP_MIN_PERCENT,
    PENALTY_CAP_MIN_DAILY_PERCENT,
    PENALTY_DAILY_MAX_PERCENT,
    PREPAY_MAX_PERCENT,
    RISK_LABELS,
    WARRANTY_MIN_MONTHS,
)

# 风险类型 → 规则侧写入的政策编号。规则按此写报告，复核新增的引用也要对得上；
# 未登记的类型（如缺必填字段、空白模板）本就不引政策，不参与这项核对。
_EXPECTED_POLICY: dict[str, str] = {
    "prepayment_ratio_high": "P-01",
    "warranty_too_short": "P-02",
    "liability_cap_unclear": "P-03",
    "liability_cap_too_low": "P-03",
    "penalty_rate_too_high": "P-03",
    "confidentiality_missing": "P-04",
    "confidentiality_too_long": "P-04",
    "ip_ownership_missing": "P-05",
    "ip_ownership_unclear": "P-05",
    "governing_law_missing": "P-05",
    "acceptance_unclear": "P-06",
    "invoice_unclear": "P-07",
    "performance_bond_missing": "P-08",
    "subcontract_unrestricted": "P-09",
    "personal_info_clause_missing": "P-10",
    "data_processing_terms_missing": "P-10",
    "data_cross_border_unclear": "P-11",
    "data_deletion_missing": "P-12",
    "confidentiality_no_exception": "P-13",
    "penalty_basis_unclear": "P-14",
    "penalty_cap_missing": "P-14",
}

# 数值型风险：判定依据的阈值必须能在被引政策原文里找到（阈值取自规则侧同一批常量）
_THRESHOLD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "prepayment_ratio_high": (f"{PREPAY_MAX_PERCENT:g}%",),
    "warranty_too_short": (f"{WARRANTY_MIN_MONTHS}个月",),
    "confidentiality_too_long": (f"{CONFIDENTIALITY_MAX_MONTHS}个月",),
    "liability_cap_too_low": tuple(
        f"{value:g}%" for value in sorted(set(LIABILITY_CAP_MIN_PERCENT.values()))
    ),
    "penalty_rate_too_high": (f"{PENALTY_DAILY_MAX_PERCENT:g}%",),
    "penalty_cap_missing": (f"{PENALTY_CAP_MIN_DAILY_PERCENT:g}%",),
}

# 品类 → 政策适用范围里的对应写法（只用于识别政策自己写了"不适用"的那类排除句）
_KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "enterprise_goods": ("货物",),
    "agri_goods": ("农副",),
    "tech_service": ("技术开发", "服务", "软件"),
    "gov_goods": ("政府采购", "示范文本", "政采"),
}


@dataclass
class PolicyEntry:
    """一份政策：编号、来源文件、整份正文、适用范围行。"""

    ref: str  # 政策编号（如 P-01）
    source: str  # 来源文件名
    text: str  # 整份正文
    scope: str  # 文件头"适用范围"那一行（取不到为空串）


def load_corpus(policy_dir: Path | None = None) -> dict[str, PolicyEntry]:
    """读语料目录，返回 编号 → 政策；编号从文件名前缀解析（与入库口径一致）。"""
    directory = policy_dir or POLICY_DIR
    corpus: dict[str, PolicyEntry] = {}
    for path in sorted(directory.glob("*.md")):
        matched = re.match(r"(P-\d+)", path.name)
        # 分支：文件名不带 P-编号 → 不入库也不参与核对
        if not matched:
            continue
        text = path.read_text(encoding="utf-8")
        corpus[matched.group(1)] = PolicyEntry(
            ref=matched.group(1), source=path.name, text=text, scope=_scope_of(text)
        )
    return corpus


def check_citations(
    risks: list, contract_kind: str | None = None, policy_dir: Path | None = None
) -> dict:
    """把报告里所有带引用的风险逐条核对，返回 {summary, items}；不带引用的不进统计。"""
    corpus = load_corpus(policy_dir=policy_dir)
    items = [
        check_citation(risk, corpus, contract_kind=contract_kind)
        for risk in risks
        if _value(risk, "policy_ref")
    ]
    cited = len(items)
    grounded = sum(1 for item in items if item["ok"])
    return {
        "summary": {
            "cited": cited,  # 报告里带政策引用的风险条数（分母）
            "grounded": grounded,  # 引用经得起核对（编号/正文/对应政策/阈值都过）
            "rate": round(grounded / cited, 4) if cited else None,
            "noted": sum(1 for item in items if item["notes"]),  # 只带提示的条数
        },
        "items": items,
    }


def check_citation(risk, corpus: dict[str, PolicyEntry], contract_kind: str | None = None) -> dict:
    """核对一条风险的引用：ok=硬性检查全过；notes=提示项，不影响结论。"""
    ref = _value(risk, "policy_ref") or ""
    risk_type = _value(risk, "risk_type") or ""
    issues: list[str] = []
    notes: list[str] = []
    entry = corpus.get(ref)
    # 分支：编号在语料里找不到 → 后面几项无从谈起，直接给结论
    if entry is None:
        issues.append(f"政策库没有 {ref} 这个编号")
    else:
        # 分支：文件在位但正文读不出来（空文件/被截断）→ 引用没有依据
        if not entry.text.strip():
            issues.append(f"{ref} 的政策正文读不到")
        expected = _EXPECTED_POLICY.get(risk_type)
        # 分支：该风险类型由别的政策管辖 → 引用与结论对不上
        if expected and ref != expected:
            issues.append(f"该风险类型对应 {expected}，实际引用 {ref}")
        candidates = _THRESHOLD_CANDIDATES.get(risk_type)
        # 分支：数值型风险 → 判定依据的阈值要能在被引政策原文里找到
        if candidates and not any(candidate in _norm(entry.text) for candidate in candidates):
            issues.append(f"{ref} 原文找不到该类型依据的阈值（{'、'.join(candidates)}）")
        notes.extend(_scope_notes(entry, contract_kind))
    return {
        "risk_type": risk_type,
        "label": _value(risk, "label") or RISK_LABELS.get(risk_type, ""),
        "severity": _value(risk, "severity") or "",
        "origin": _value(risk, "origin") or "rules",
        "policy_ref": ref,
        "ok": not issues,
        "issues": issues,
        "notes": notes,
    }


def _scope_notes(entry: PolicyEntry, contract_kind: str | None) -> list[str]:
    """政策适用范围里自己写了"此类合同不适用"时提醒一句，避免引到管不着的政策。

    只认政策明写的排除句（"…等不适用本细则"），不做品类推断——推断容易把正常引用误判。
    """
    keywords = _KIND_KEYWORDS.get(contract_kind or "")
    # 分支：合同品类未知，或该政策没写适用范围 → 无可提示
    if not keywords or not entry.scope:
        return []
    for sentence in re.split(r"[；;。]", entry.scope):
        # 分支：同一句里既有"不适用"又提到本类合同 → 值得人工看一眼
        if "不适用" in sentence and any(keyword in sentence for keyword in keywords):
            return [f"{entry.ref} 的适用范围写明这类合同不适用，需人工确认是否引错"]
    return []


def _scope_of(text: str) -> str:
    """取文件头的"适用范围"整段（换行折行接回一行），取不到返回空串。"""
    matched = re.search(r"适用范围[：:]\s*([\s\S]+?)(?:\n\s*\n|\n#)", text)
    return re.sub(r"\s+", " ", matched.group(1)).strip() if matched else ""


def _norm(text: str) -> str:
    """比对前归一：去掉空白、全角百分号按半角算（政策原文常用" 30%"这种写法）。"""
    return re.sub(r"\s+", "", text or "").replace("％", "%")


def _value(risk, name: str):
    """风险取字段：RiskItem 与 dict 两种形态都要支持（图状态里存的是 dict）。"""
    raw = risk.get(name) if isinstance(risk, dict) else getattr(risk, name, None)
    return getattr(raw, "value", raw)  # Severity 等枚举取字符串值
