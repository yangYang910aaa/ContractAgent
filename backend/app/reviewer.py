"""双审盲审：独立复核 LLM + 与主审风险合并。

分工（与 extractor 同思路——LLM 调用做薄、确定性逻辑做厚）：

- blind_review: 唯一调 LLM 的地方。复核 LLM 只拿「合同分条原文 + 政策条文」，
  不拿主审的抽取字段与风险清单,独立列风险清单；

  - normalize_findings: 纯函数，把模型原始输出（可能漂移/超发/编造类型）
  收敛成 ReviewFinding 列表；

  - merge_review: 把主审的结果和复核的结果diff:
    同类项同等级->一致（不重复并入）
    复核报、主审未报且 high → 复核新增,
    都报但 等级不同 → 取高并标注(复核更高则升级主审项)
    复核报 medium/low 或主审已按空白模板降级 → 只记提示不并入（防噪音/防误停闸）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from backend.app.llm import get_chat_model
from backend.app.parser import Clause, split_clauses
from backend.app.policy_rag import IndexDoc, load_policies
from backend.app.rules import RISK_LABELS
from backend.app.schemas import RiskItem, Severity
from backend.app.usage import STAGE_REVIEW, llm_call

# 复核清单上限：防 LLM 发散把整份合同逐条报一遍
REVIEW_MAX_FINDINGS = 8
# 复核 high 允许并入/升级的类型白名单（其余类型 rules 已按 medium 提示或自身口径
# 处理，复核再报 high 只会制造误报——如 sample_09 的 ip_ownership_unclear 跨次升级）
_REVIEW_HIGH_TYPES = {
    "missing_required_field",  # 定性可核：正文确实没写核心字段（另有空白模板护栏）
    "prepayment_ratio_high",  # 数值型：须 evidence 原句可解析出超限数字（复核门校验）
    "warranty_too_short",
    "confidentiality_too_long",
    "penalty_rate_too_high",
    "liability_cap_too_low",
        "penalty_cap_missing",
}
# 政策条文的严重级口径说明（写进盲审 prompt，指导模型与 rules 一致地分级）
_SEVERITY_GUIDE = (
    "severity 只允许 high（复核只列高风险清单；提示级问题由规则引擎负责，不要输出）：\n"
    "- high：确凿的缺陷——政策阈值违超（预付款超 30%、质保不足 12 个月、责任上限低于品类底线、"
    "保密期超 36 个月、违约金日费率畸高）、付款金额与总额明显不一致、核心字段（总额/生效日/到期日）缺失；\n"
    "- 违约金按日计罚却没有累计上限（且日费率 ≥0.1%）：同样按 high 报，类型用 penalty_cap_missing，"
    "evidence 要抄含日费率、且看不出上限的原句；\n"
    "- 提示级（规则引擎已按 medium 提示，复核不得输出、更不得升级成 high）："
    "liability_cap_unclear / confidentiality_missing / ip_ownership_missing / "
    "ip_ownership_unclear / governing_law_missing / date_logic_* / "
    "amount_inconsistency（金额空缺待人工核对的情形）。\n"
    "拿不准的不报：宁缺毋滥，干净合同应输出空数组 []。"
)


class ReviewFindingRaw(BaseModel):
    """LLM 原始输出的一条复核发现（severity/risk_type 用字符串，交给归一化校验）。"""

    risk_type: str | None = None  # 风险类型机器码（必须取自给定编码表）
    severity: str | None = None  # high / medium / low
    clause_ref: str = ""  # 合同条款引用（如"第四条"，须与给定分条一致）
    evidence: str = ""  # 原文摘录/说明（证据回指，不许改写原文数字）
    policy_ref: str | None = None  # 依据政策编号（P-01~P-05）
    suggestion: str = ""  # 整改建议


class BlindReviewSchema(BaseModel):
    """盲审 LLM with_structured_output 的输出结构：风险清单。"""

    findings: list[ReviewFindingRaw] = Field(default_factory=list)  # 复核发现（上限 8 条）


class ReviewFinding(BaseModel):
    """一条规范化后的复核发现（合并阶段的输入，由纯函数收敛而来）。"""

    risk_type: str  # 风险类型机器码（已校验 ∈ RISK_LABELS）
    severity: Severity  # 风险等级（已归一化为枚举）
    clause_ref: str = ""  # 合同条款引用
    evidence: str = ""  # 证据说明（原文摘录）
    policy_ref: str | None = None  # 依据政策编号
    suggestion: str = ""  # 整改建议


class BlindReviewOutput(BaseModel):
    """blind_review 的结果：规范化发现 + 可选错误说明（失败不阻断审查）。"""

    findings: list[ReviewFinding] = Field(default_factory=list)  # 规范化后的复核发现
    error: str | None = None  # 检索/LLM 失败说明（best-effort，空则正常）


_SEVERITY_ALIASES = {
    "high": Severity.high,
    "medium": Severity.medium,
    "low": Severity.low,
    "高": Severity.high,
    "中": Severity.medium,
    "低": Severity.low,
}
# 严重级数值序：取高/升级判断用（字符串字典序 high<medium 会判错，必须用数值）
_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _severity_of(value: Any) -> Severity | None:
    """把模型原始 severity 映射成枚举（大小写/中文别名容错）；不认识返回 None。"""
    if not isinstance(value, str):
        return None
    return _SEVERITY_ALIASES.get(value.strip().lower())


def _money_numbers(text: str) -> list[Decimal]:
    """从 evidence 里抓金额数字（千分位/小数容忍），供复核门算比例。"""
    out: list[Decimal] = []
    for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            out.append(Decimal(m.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def _first_percent(text: str) -> float | None:
    """抓 evidence 里第一个百分比数值（如 "60%" → 60.0）；没有返回 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def _months_in(text: str) -> int | None:
    """把 evidence 里按年/月写的期限折算成月数（"6 个月"→6、"0.5 年"→6）；没有返回 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*年", text)
    if m:
        return round(float(m.group(1)) * 12)
    m = re.search(r"(\d+(?:\.\d+)?)\s*个?月", text)
    return int(m.group(1)) if m else None


def _verify_high(f: ReviewFinding) -> tuple[bool, str]:
    """复核门：复核新增/升级的 high 是否可信（确定性校验，不信任 LLM 算术）。

    背景：盲审模型对"需要计算的比例/月数"极易判错（实测把 400,000/2,000,000=20%
    的合规预付款报成 high）。凡数值型 high，必须能从其 evidence 原句解析出支持
    违规结论的数字；解析不出或结论与数字矛盾 → 不并入，只记提示。
    """
    text = f.evidence or ""
    # 分支：类型不在白名单 → 一律不并入（rules 已按自身口径处理这些类型）
    if f.risk_type not in _REVIEW_HIGH_TYPES:
        return False, "该类型不属可并入的高风险类型"
    # 分支：缺核心必填 → 定性判断，正文没有数字可核，放行（空白模板另有护栏）
    if f.risk_type == "missing_required_field":
        return True, ""
    # ---- 数值型：逐类按 evidence 原句复核 ----
    # 预付款比例：有白纸黑字百分比 → 直接比 30%；否则用"预付款金额/总价"两数反推
    if f.risk_type == "prepayment_ratio_high":
        # 口径闸：P-01 只约束明确写"预付/首付/备料"的期次，普通分期/里程碑付款
        # 不算（与 rules._prepay_ratio 只认名称含"预付"一致，防 tech_03 里程碑误报）
        if not re.search(r"预付|首付|备料", text):
            return False, "原文无预付款/首付款字样（普通分期不适用 P-01）"
        pct = _first_percent(text)
        if pct is not None:
            ok = pct > 30.0
            return ok, f"原文写 {pct:g}%（需 >30% 才支持）" if not ok else ""
        nums = _money_numbers(text)
        # 这种情况是：evidence 里同时有预付款与总额两个金额 → 前除后算比例
        if len(nums) >= 2 and nums[0] < nums[1] and nums[0] > 0:
            ratio = float(nums[0] / nums[1] * 100)
            return ratio > 30.0, f"按原文金额算出 {ratio:.1f}%（需 >30%）"
        return False, "原文无法解析预付款/总额比例"
    # 质保不足 / 保密过长：evidence 里按年/月写的期限折算后与阈值比
    if f.risk_type == "warranty_too_short":
        months = _months_in(text)
        if months is None:
            return False, "原文无质保月数"
        return months < 12, f"原文质保 {months} 个月（需 <12 个月）"
    if f.risk_type == "confidentiality_too_long":
        months = _months_in(text)
        if months is None:
            return False, "原文无保密期月数"
        return months > 36, f"原文保密 {months} 个月（需 >36 个月）"
    # 违约金畸高：须"按日"口径且日率 >1%（"每次按 10%"非日费率，规则不适用）
    if f.risk_type == "penalty_rate_too_high":
        pct = _first_percent(text)
        if pct is None:
            return False, "原文无违约金比例"
        is_daily = bool(re.search(r"日|按天", text))
        not_occurrence = not bool(re.search(r"每次|按次|每笔", text))
        ok = is_daily and not_occurrence and pct > 1.0
        return ok, f"原文日费率 {pct:g}%（需按日且 >1%）" if not ok else ""
    # 违约金无累计上限：证据要能看出"按日计罚"，且原句附近没有上限表述。
    # 口径与规则引擎一致：日费率 ≥0.1% 才算失控敞口。
    if f.risk_type == "penalty_cap_missing":
        pct = _first_percent(text)
        is_daily = bool(re.search(r"每(?:日|天)|按日|每逾期一[日天]|每延期一[日天]|每延迟一[日天]", text))
        has_cap = bool(re.search(r"不超过|最高不超过|累计不超过|上限|封顶", text))
        if pct is None:
            return False, "原文无违约金比例"
        if not is_daily:
            return False, "原文看不出按日计罚"
        if has_cap:
            return False, "原文已写累计上限"
        ok = pct >= 0.1
        return ok, f"原文日费率 {pct:g}%（无上限需 ≥0.1%/日）" if not ok else ""
    # 责任上限过低：品类底线 50/30 视品类而定，evidence 无品类信息 → 只认 <30 的铁证
    if f.risk_type == "liability_cap_too_low":
        # 语义闸：责任上限指"赔偿/责任以总额 X% 为限"；违约金总额上限不算
        # （与 extractor 提示/rules 口径一致，防 tech_01"每次 10%+总额 30%"误报）
        if not re.search(r"赔偿|责任", text):
            return False, "原文是违约金类表述，非赔偿责任上限（P-03 口径）"
        pct = _first_percent(text)
        if pct is None:
            return False, "evidence 无责任上限比例"
        return pct < 30.0, f"evidence 上限 {pct:g}%（需 <30 的铁证）" if pct >= 30.0 else ""
    return False, "类型不支持复核新增"


def normalize_findings(
    raw: Any,
    max_findings: int = REVIEW_MAX_FINDINGS,
) -> tuple[list[ReviewFinding], list[str]]:
    """模型原始输出 → 规范化发现列表 + 丢弃原因清单。

    只收高风险且类型已知的条目（中低风险属提示级噪音，由规则引擎负责）；级别别名收敛、
    同类型同级别同条款去重、超上限截断。丢弃原因一并返回，不静默。
    """
    items = raw if isinstance(raw, list) else []
    out: list[ReviewFinding] = []
    dropped: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for idx, item in enumerate(items):
        # 这种情况是：单条不是 dict（模型输出脏元素）→ 丢弃
        if not isinstance(item, dict):
            dropped.append(f"#{idx}: 非对象条目 {item!r}")
            continue
        risk_type = str(item.get("risk_type") or "").strip()
        # 这种情况是：模型编造了编码表外的类型 → 丢弃（无法与 rules/GT 对齐比较）
        if risk_type not in RISK_LABELS:
            dropped.append(f"#{idx}: 未登记类型 {risk_type!r}")
            continue
        severity = _severity_of(item.get("severity"))
        # 这种情况是：severity 缺失/不认识或非 high → 丢弃（盲审只收高风险清单）
        if severity != Severity.high:
            raw_sev = item.get("severity") or "缺失"
            dropped.append(f"#{idx}: 非 high 级丢弃 {risk_type!r} severity={raw_sev!r}")
            continue
        clause_ref = str(item.get("clause_ref") or "").strip()
        key = (risk_type, severity.value, clause_ref)
        # 这种情况是：同一条被重复输出 → 只留第一条
        if key in seen:
            dropped.append(f"#{idx}: 重复 {key}")
            continue
        seen.add(key)
        # 这种情况是：超出上限 → 截断（模型偶尔无视"最多 N 条"）
        if len(out) >= max_findings:
            dropped.append(f"#{idx}: 超过上限 {max_findings} 条，截断")
            break
        out.append(
            ReviewFinding(
                risk_type=risk_type,
                severity=severity,
                clause_ref=clause_ref,
                evidence=str(item.get("evidence") or "").strip(),
                policy_ref=(
                    str(item["policy_ref"]).strip()
                    if item.get("policy_ref") not in (None, "")
                    else None
                ),
                suggestion=str(item.get("suggestion") or "").strip(),
            )
        )
    return out, dropped


_SYSTEM_PROMPT = """你是中文采购合同的独立复核员（盲审）。
你只拿到合同分条原文与采购政策条文——看不到主审的抽取结果与风险清单，请完全独立判断。

任务：逐条核对政策与合同条款，找出【确有依据、确属违规】的缺陷风险。这是缺陷审核，
不是条款点评——条款符合政策即正常，不得输出任何条目。
规则：
1. 风险类型 risk_type 只能取下列编码（未列出的问题不报，宁缺毋滥）：
{risk_labels}
2. 不要做数值计算：比例（预付款/责任上限/违约金日率）、金额加总一致性、月数（质保/
   保密）、日期先后都由规则引擎精确判定，复核对这些不做算术、不下 high 结论——只有
   原文直接写明违规数值（如"预付款为合同总额的 60%""每日按 3% 计"）时，才可引用该句
   原样报出；"预付款：400,000 元"这类需要自己算比例的一律不算违规。P-01 预付款上限
   只约束条款明确写为"预付款/首付款/备料款"的支付；普通分期/里程碑付款（如技术开发
   按节点付款、货到验收后付款）不适用，不得按预付款报。P-03 责任上限指"赔偿/责任以
   总额 X% 为限"的赔偿责任上限，违约金总额上限不算，别把违约金率当责任上限。
3. 只报告你能从原文找到证据的问题，evidence 必须引用合同原句，禁止脑补数字、禁止把
   空缺/含糊处脑补成确定违规；金额空缺时不得报"不一致"类 high。
4. policy_ref 只能引用给定政策条文里的编号；与该政策适用范围不符的正常省略不算缺陷
   （例如政府采购示范文本类合同按示范文本执行，无责任上限/保密条款不视为缺陷）。
5. 正文含大量填空占位（下划线/□/纯空格/点线）的疑似未填模板或未定稿，不要仅因
   "没填写"报缺失类风险。
6. clause_ref 必须能在合同分条中找到（如"第四条"或章节头）。
7. findings 最多 {max_findings} 条；全部合规时必须输出空数组 []，不许凑数。
{severity_guide}
只输出 JSON。"""


def _clause_blocks(text: str) -> list[Clause]:
    """合同全文 → 分条块；无条文结构时整段兜底成一块（盲审仍能看全文）。"""
    clauses = split_clauses(text)
    # 这种情况是：无「第X条/章节头」结构 → 兜底整段（ref 空，title=全文）
    if not clauses and text.strip():
        clauses = [Clause(ref="", title="全文", text=text.strip())]
    return clauses


def _policy_context(
    text: str,
    retriever=None,
) -> tuple[list[IndexDoc], str | None]:
    """取盲审用的政策条文（编号 + 正文）。

    默认本地直读全部条文，口径最稳且不产生检索调用；可注入检索函数改按条召回，
    等条文数量显著变多后再切 top-k 并控制提示长度。
    """
    # 这种情况是：调用方注入了检索器（图测试/未来语料规模化）→ 用检索命中
    if retriever is not None:
        try:
            hits = retriever((text or "")[:1500])
            docs = [
                IndexDoc(text=h.text, source=getattr(h, "source", ""), policy_ref=h.policy_ref)
                for h in hits
            ]
            return docs, None
        except Exception as exc:
            return [], f"政策检索失败：{exc}"
    # 这种情况是：默认路径 → 本地直读全量政策
    try:
        return load_policies(), None
    except Exception as exc:
        return [], f"政策读取失败：{exc}"


def _recover_completion(exc: Exception) -> dict | None:
    """从 with_structured_output 的解析报错里还原模型原始 JSON（与 extractor 同法）。

    extractor 里同名私有函数不便跨模块引用，此处保留最小副本（不动待审文件）。
    """
    text = str(exc)
    # 分支：报错里没有 completion 字样（接口/超时类异常）→ 无法还原
    marker = text.find("completion ")
    if marker < 0:
        return None
    start = text.find("{", marker)
    if start < 0:
        return None
    try:
        raw, _ = json.JSONDecoder().raw_decode(text, start)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def blind_review(
    text: str,
    llm=None,
    retriever=None,
    max_findings: int = REVIEW_MAX_FINDINGS,
) -> BlindReviewOutput:
    """对合同全文做一次独立盲审：政策条文 + 分条原文 → 规范化风险清单。

    llm/retriever 可注入（测试用假对象）；不传则默认 chat 模型 + 本地全量政策。
    LLM/解析失败不抛异常：返回 error 说明、空 findings（双审是增强，失败不阻断）。
    """
    model = llm or get_chat_model(temperature=0.0, enable_thinking=False)
    policy_docs, policy_error = _policy_context(text, retriever=retriever)
    clauses = _clause_blocks(text)

    risk_labels = "\n".join(f"- {k}：{v}" for k, v in RISK_LABELS.items())
    system = (
        _SYSTEM_PROMPT.replace("{risk_labels}", risk_labels)
        .replace("{max_findings}", str(max_findings))
        .replace("{severity_guide}", _SEVERITY_GUIDE)
    )
    policy_section = "\n\n".join(f"【{d.policy_ref}】\n{d.text}" for d in policy_docs)
    contract_section = "\n\n".join(
        f"【{c.ref or c.title}】\n{c.text}" for c in clauses
    )
    human = (
        "请只输出 JSON。\n\n===== 政策条文（可引用编号见【】）=====\n"
        + (policy_section or "（政策条文不可用，仅凭所列风险类型常识判断，宁缺毋滥）")
        + "\n\n===== 合同分条原文 =====\n"
        + contract_section
    )

    structured = model.with_structured_output(BlindReviewSchema, method="json_mode")
    try:
# 调用计数：包住模型调用；双审每份只这一次复核调用
        with llm_call(STAGE_REVIEW):
            result = structured.invoke([("system", system), ("human", human)])
    except Exception as exc:
        # 这种情况是：解析失败但报错里带原始 completion → 归一化兜底后照常复核
        raw = _recover_completion(exc)
        if raw is not None:
            findings, _ = normalize_findings(raw.get("findings"), max_findings)
            return BlindReviewOutput(findings=findings, error=policy_error)
        # 这种情况是：还原失败（接口/超时）→ 空发现 + 错误说明（不阻断主审）
        return BlindReviewOutput(findings=[], error=f"盲审调用失败：{exc}")

    raw = result.model_dump() if hasattr(result, "model_dump") else result
    findings, _ = normalize_findings(raw.get("findings"), max_findings)
    return BlindReviewOutput(findings=findings, error=policy_error)


@dataclass
class MergeOutcome:
    """merge_review 的结果：合并后的风险清单 + JSON 可序列化的 review 报告段。"""

    risks: list[RiskItem]  # 合并后的风险（主审 + 复核新增/升级）
    review: dict  # 报告 review 段（stats/details/summary；直接可落 JSON）


def _outcome_detail(f: ReviewFinding, outcome: str, note: str = "") -> dict:
    """一条复核发现 → review 段 details 里的记录（含处理结果）。"""
    return {
        "risk_type": f.risk_type,
        "severity": f.severity.value,
        "clause_ref": f.clause_ref,
        "evidence": f.evidence,
        "policy_ref": f.policy_ref,
        "suggestion": f.suggestion,
        "outcome": outcome,
        "note": note,
    }


def merge_review(
    main: list[RiskItem],
    findings: list[ReviewFinding],
) -> MergeOutcome:
    """主审风险与复核发现合并 → (最终风险清单, 复核报告段)。

    逐条处理：两边都报且同级 → 记一致；只有复核报 → 高风险并入清单、中低风险只记录；
    两边级别不同 → 取高并标注。护栏：主审已判空白模板时，复核报的缺必填只记录不升级，
    否则会把空白模板重新顶回闸口。
    """
    out = [r.model_copy() for r in main]  # 不修改入参（复制防副作用）
    details: list[dict] = []
    stats = {"findings": len(findings), "agreed": 0, "upgraded": 0, "added": 0, "noted": 0}
    blank_downgraded = any(r.risk_type == "blank_template_suspected" for r in main)

    for f in findings:
        # 收集主审同 type 的候选（risk_type 对齐是 diff 的比较键）
        candidates = [r for r in out if r.risk_type == f.risk_type]
        # 分支 1：主审没有该 type
        if not candidates:
            # 这种情况是：复核报 high 且通过复核门 → 并入 risks（可能补上主审漏检）
            ok, why = _verify_high(f)
            if f.severity == Severity.high and ok:
                out.append(
                    RiskItem(
                        risk_type=f.risk_type,
                        label=RISK_LABELS.get(f.risk_type, f.risk_type),
                        severity=Severity.high,
                        clause_ref=f.clause_ref,
                        evidence=f.evidence,
                        policy_ref=f.policy_ref,
                        suggestion=f.suggestion,
                        field=None,
                        origin="review",
                    )
                )
                stats["added"] += 1
                details.append(_outcome_detail(f, "added", "复核独立发现，并入风险清单"))
            # 这种情况是：复核报 medium/low 或未过复核门 → 只记提示（防噪音/防误报）
            else:
                stats["noted"] += 1
                note = why or f"非 high 提示（{f.severity.value}），仅记录不并入"
                details.append(_outcome_detail(f, "noted", note))
            continue

        # 分支 2：主审有同 type 且存在同 severity → 一致（不重复并入）
        if any(r.severity == f.severity for r in candidates):
            stats["agreed"] += 1
            details.append(_outcome_detail(f, "agreed", "主审与复核一致"))
            continue

        # 分支 3：都报但 severity 不同 → 取高
        # 这种情况是：空白模板下主审把缺必填主动降了级 → 复核 high 不升级（护栏）
        if f.risk_type == "missing_required_field" and blank_downgraded:
            stats["noted"] += 1
            details.append(
                _outcome_detail(f, "noted", "疑似空白模板已降级，复核 missing 不升级")
            )
            continue
        # 这种情况是：复核比主审更高 → 升级同 type 中 severity 最高的那个候选
        if _SEVERITY_RANK[f.severity.value] > max(
            _SEVERITY_RANK[r.severity.value] for r in candidates
        ):
            # 复核门：升级也要过确定性校验（防模型把提示级/算错数字顶成 high）
            ok, why = _verify_high(f)
            if ok:
                target = max(candidates, key=lambda r: _SEVERITY_RANK[r.severity.value])
                target.severity = f.severity
                stats["upgraded"] += 1
                details.append(
                    _outcome_detail(
                        f,
                        "upgraded",
                        f"主审判 {target.risk_type} 偏低，复核取高为 {f.severity.value}",
                    )
                )
            else:
                stats["noted"] += 1
                details.append(_outcome_detail(f, "noted", f"复核门未通过：{why}"))
            continue
        # 这种情况是：复核比主审低 → 维持主审高判定，记一致
        stats["agreed"] += 1
        details.append(_outcome_detail(f, "agreed", "主审判定更高，维持主审"))

    summary = _merge_summary(stats)
    return MergeOutcome(
        risks=out,
        review={
            "mode": "double",
            "summary": summary,
            "stats": stats,
            "details": details,
        },
    )


def _merge_summary(stats: dict) -> str:
    """按合并统计拼一句话摘要（供报告 review 段首行展示）。"""
    if stats["findings"] == 0:
        return "独立复核未发现主审遗漏的高风险项"
    parts = []
    if stats["agreed"]:
        parts.append(f"{stats['agreed']} 条与主审一致")
    if stats["upgraded"]:
        parts.append(f"{stats['upgraded']} 条取高升级")
    if stats["added"]:
        parts.append(f"{stats['added']} 条为复核新增")
    if stats["noted"]:
        # 措辞与前端徽标保持一致（"仅提示"太含糊，用户看不出是"只记录不并入"）
        parts.append(f"{stats['noted']} 条仅记录不并入")
    return "；".join(parts) + "（独立复核，未参考主审结论）"


def double_review(
    risks: list[RiskItem],
    text: str,
    llm=None,
    retriever=None,
    max_findings: int = REVIEW_MAX_FINDINGS,
) -> tuple[list[RiskItem], dict]:
    """双审编排：盲审 LLM → 与主审合并 → (最终风险, review 报告段)。

    pipeline/graph 的统一入口；LLM 失败时回退主审结果并附 error（不阻断）。
    """
    output = blind_review(text=text, llm=llm, retriever=retriever, max_findings=max_findings)
    outcome = merge_review(risks, output.findings)
    # 这种情况是：检索/LLM 失败 → 把错误挂到 review 段，方便排查
    if output.error:
        outcome.review["error"] = output.error
    return outcome.risks, outcome.review
