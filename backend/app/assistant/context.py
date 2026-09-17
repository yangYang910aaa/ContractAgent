"""这份合同的上下文：风险清单、抽取字段、条款目录、政策库目录。

助手只谈用户正在看的那一份合同，所以上下文全部从详情页已有的数据里拼——
报告或待审批载荷里的风险、抽取字段、解析后的正文，不新增取数接口、也不重跑规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.review.parser import split_clauses
from backend.app.policy.rag import POLICY_DIR
from backend.app.review.rules.constants import FIELD_LABELS

# 政策文件名的编号前缀（与入库时的 policy_ref 同一出处）；忽略大小写——模型会写 p-02
POLICY_FILE_RE = re.compile(r"(P-\d+)", re.IGNORECASE)

# 风险等级中文名（给模型看的上下文用中文，与前端展示一致）
_SEVERITY_LABELS = {"high": "高风险", "medium": "中风险", "low": "低风险"}
# 评级中文名：模型解释"为什么判不通过"时要能说对档位
_GRADE_LABELS = {"pass": "通过", "conditional_pass": "有条件通过", "fail": "不通过"}
# 字段单位：只补容易被读错的量纲（金额/比例/月/天），其余字段名已自明
_FIELD_UNITS = {
    "total_amount": "元",
    "penalty_rate": "%（每日）",
    "liability_cap": "%（占合同总额）",
    "warranty_months": "个月",
    "confidentiality_months": "个月",
    "termination_notice_days": "天",
}


@dataclass
class ContractContext:
    """一次提问所属的合同上下文（全部取详情页已有的数据，不新增取数接口）。"""

    thread_id: str = ""  # 任务号（会话键与前端面板用）
    file_name: str = ""  # 展示用文件名
    grade: str | None = None  # 报告评级（pass/conditional_pass/fail）
    risks: list[dict] = field(default_factory=list)  # 风险清单（报告 risks 或闸口 high_risks）
    extracted: dict = field(default_factory=dict)  # 抽取字段（报告 extracted）
    text: str = ""  # 解析后的合同正文（找条款用）
    declared_refs: list[str] = field(default_factory=list)  # 报告声明过的政策编号（确定性依据）
    # 报告里每条政策引用的原文（编号 → 标题/正文）：回答提到这些编号时补一条"报告依据"芯片
    declared_hits: dict[str, dict] = field(default_factory=dict)


def build_context(record: Any) -> ContractContext:
    """从任务登记记录拼出上下文：风险清单、抽取字段、正文、已声明的政策编号。

    gate 状态还没有报告，用待审批载荷里的高风险项当清单——用户恰恰是停闸口时最想问
    "为什么判高风险"。文件名与正文取登记簿里的展示名与解析全文。
    """
    report = getattr(record, "report", None) or {}
    payload = getattr(record, "gate_payload", None) or {}
    source = getattr(record, "source", "") or ""
    return ContractContext(
        thread_id=getattr(record, "thread_id", "") or "",
        file_name=getattr(record, "name", "") or Path(source).name,
        grade=report.get("grade"),
        risks=list(report.get("risks") or payload.get("high_risks") or []),
        extracted=dict(report.get("extracted") or {}),
        text=getattr(record, "source_text", "") or "",
        declared_refs=declared_policy_refs(report, payload),
        declared_hits=declared_policy_hits(report),
    )


def declared_policy_refs(*sources: dict | None) -> list[str]:
    """已声明过的政策编号，去重保序返回。

    数据源是报告（风险依据 + 政策引用清单）与待审批载荷（闸口阶段还没有报告）。
    这些编号都是规则引擎写的确定性数据；回答照抄它们不算编造，不该被标"无法核实"。
    """
    refs: list[str] = []
    for source in sources:
        data = source or {}
        for risk in (data.get("risks") or []) + (data.get("high_risks") or []):
            ref = risk.get("policy_ref")
            if ref:
                refs.append(str(ref))
        for hit in data.get("policy_hits") or []:
            ref = hit.get("policy_ref")
            if ref:
                refs.append(str(ref))
    return list(dict.fromkeys(refs))


def declared_policy_hits(report: dict | None) -> dict[str, dict]:
    """报告里的政策引用 → {编号: {title, text}}，去重后返回（同一编号只留第一条）。

    用来给"回答提到、但这轮没检索"的编号补一条可点的芯片：正文用报告当时检索到的原文，
    所以点开看到的依据与报告页一致，不依赖模型配合。
    """
    out: dict[str, dict] = {}
    for hit in (report or {}).get("policy_hits") or []:
        ref = str(hit.get("policy_ref") or "")
        # 分支：没有编号或这个编号已经收过 → 跳过
        if not ref or ref in out:
            continue
        text = str(hit.get("text") or hit.get("snippet") or "").strip()
        out[ref] = {"title": first_line_title(text), "text": text}
    return out


# ---- 条款与政策的小工具（拼上下文、查条款共用）----


def clause_blocks(text: str) -> list[dict]:
    """全文 → 条款块（ref/title/text）；没有条款结构时整篇一块，找条款仍能工作。"""
    blocks = [{"ref": c.ref, "title": c.title, "text": c.text} for c in split_clauses(text)]
    # 分支：合同没有「第X条/章节」结构 → 整篇当一块（与原文抽屉的兜底一致）
    if not blocks:
        return [{"ref": "", "title": "全文", "text": text.strip()}] if text.strip() else []
    return blocks


def first_line_title(text: str) -> str:
    """取正文首行当标题（去掉 Markdown 标记），芯片上显示"这是哪一条/哪份政策"。"""
    for line in (text or "").splitlines():
        cleaned = re.sub(r"^(?:#{1,6}|>|-|\*)\s*", "", line.strip())
        if cleaned:
            return cleaned
    return ""


def policy_title(text: str) -> str:
    """政策名称：标题行里"…细则 P-01：预付款管理"的冒号后半截；没有冒号用整行。"""
    title = first_line_title(text)
    return title.split("：")[-1].strip() if "：" in title else title


def _policy_scope(text: str) -> str:
    """政策适用范围：取"适用范围"起头那一段（给模型看各政策管什么）。"""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("适用范围"):
            continue
        collected: list[str] = []
        for follow in lines[i:]:
            # 分支：空行 → 适用范围这一段结束
            if not follow.strip():
                break
            collected.append(follow.strip())
        return re.sub(r"\s+", "", "".join(collected)).removeprefix("适用范围：")
    return ""


def policy_directory() -> list[dict]:
    """政策库目录：编号 + 名称 + 适用范围。

    只给目录（各政策管什么），条文与全文由工具按问题取——目录进上下文，全文不进。
    """
    out: list[dict] = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        match = POLICY_FILE_RE.match(path.name)
        # 分支：文件名不带编号 → 不是入库的政策条目，跳过
        if not match:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        out.append({"ref": match.group(1), "title": policy_title(text), "scope": _policy_scope(text)})
    return out


# ---- 上下文正文：给模型看的那几段 ----


def _risk_lines(risks: list[dict]) -> list[str]:
    """风险清单 → 给模型看的行：等级 + 名称 + 条款号 + 政策依据 + 证据 + 建议。"""
    lines: list[str] = []
    for index, risk in enumerate(risks, start=1):
        severity = _SEVERITY_LABELS.get(str(risk.get("severity")), str(risk.get("severity") or ""))
        parts = [f"{index}. {risk.get('label') or risk.get('risk_type')}", severity]
        # 分支：有条款号/政策依据才拼进去（缺必填这类没有）
        if risk.get("clause_ref"):
            parts.append(f"条款：{risk['clause_ref']}")
        if risk.get("policy_ref"):
            parts.append(f"依据：{risk['policy_ref']}")
        # 分支：复核新增的风险标出来，模型解释时不该说成主审结论
        if risk.get("origin") == "review":
            parts.append("来源：独立复核")
        line = "｜".join(parts)
        if risk.get("evidence"):
            line += f"｜证据：{risk['evidence']}"
        if risk.get("suggestion"):
            line += f"｜建议：{risk['suggestion']}"
        lines.append(line)
    return lines


def _payment_line(term: dict) -> str:
    """一期付款 → "名称：金额 元（比例%）"；金额或比例缺哪项就不显示哪项。"""
    pieces: list[str] = []
    if term.get("amount") not in (None, ""):
        pieces.append(f"{term['amount']} 元")
    if term.get("percent") not in (None, ""):
        pieces.append(f"{term['percent']}%")
    name = term.get("name") or "未命名期次"
    return f"{name}：{'（'.join(pieces) + '）' if pieces else '金额与比例未抽到'}"


def _field_lines(extracted: dict) -> list[str]:
    """抽取字段 → "中文名：值（单位）"行；空字段不列（缺什么由风险清单说）。"""
    lines: list[str] = []
    for key, label in FIELD_LABELS.items():
        value = (extracted or {}).get(key)
        # 分支：没抽到的字段不列，免得模型把空值当"合同里写着 0"
        if value in (None, "", [], {}):
            continue
        # 分支：付款期次是列表 → 逐期展开成一行
        if key == "payment_schedule":
            lines.append(f"{label}：" + "；".join(_payment_line(t) for t in value))
            continue
        unit = _FIELD_UNITS.get(key, "")
        lines.append(f"{label}：{value}{unit}")
    return lines


def context_brief(context: ContractContext) -> str:
    """拼给模型的合同上下文：风险清单 + 抽取字段 + 条款目录 + 政策库目录。

    条款只给目录不喂全文（正文用 find_clauses 按关键词取），政策只给目录不喂全文
    （依据用 search_policies/read_policy 取）——上下文短，检索才落到具体条文上。
    """
    blocks = clause_blocks(context.text)
    grade = context.grade
    grade_text = f"{grade}（{_GRADE_LABELS[grade]}）" if grade in _GRADE_LABELS else "尚无评级"
    sections: list[str] = [f"【这份合同】{context.file_name or '（文件名未知）'}｜评级：{grade_text}"]
    risks = _risk_lines(context.risks)
    sections.append(
        f"【风险清单】{len(risks)} 条（判定由规则引擎给出，助手只解释不改动）\n" + ("\n".join(risks) or "（无）")
    )
    fields = _field_lines(context.extracted)
    sections.append("【抽取字段】\n" + ("\n".join(fields) or "（未抽到字段）"))
    directory = [f"{b['ref']}{' ' if b['ref'] else ''}{b['title']}" for b in blocks]
    sections.append("【条款目录】" + ("；".join(directory) or "（这份合同没有条款结构）"))
    policies = [f"{p['ref']} {p['title']}：{p['scope']}" for p in policy_directory()]
    sections.append("【政策库目录】\n" + ("\n".join(policies) or "（政策库为空）"))
    return "\n\n".join(sections)
