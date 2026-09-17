"""政策起草（模型重档）：按现有体例把需求或半成品写成条文，并配解释与判定要点。

边界：模型只产出草稿——不写规则代码、不自动入库（入库仍走起稿页的预览 + 人工确认）。
成文里的数字只能来自输入，模型自己冒出来的数字由确定性护栏挑出来给人确认；
阈值口径、风险类型与判据最终由人定。
"""

from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel, Field

from backend.app.policy import drafts
from backend.app.llm import get_chat_model
from backend.app.review.rules import RISK_LABELS
from backend.app.usage import STAGE_DRAFT, llm_call

# 阈值数字：与冲突检测同一口径（百分比、月数），用来比对"模型有没有自己造数"
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_MONTH_RE = re.compile(r"(\d+)\s*个月")
# 中文条号：政策体例用「第一条」这种写法（1~30 够用）
_CN_NUM = "一二三四五六七八九十"
# 起草单次调用的超时（秒）：实测一次约一分钟，给足余量但必须有上限
DRAFT_TIMEOUT_SECONDS = 180.0
# 配套建议能接受的取值：品类四类、评级三档（模型给别的值一律归位并留提示）
_KINDS = {"enterprise_goods", "gov_goods", "agri_goods", "tech_service"}
_GRADES = {"pass", "conditional_pass", "fail"}


class DraftedArticle(BaseModel):
    """模型起草的一条条文：正文 + 解释 + 判定要点 + 误报护栏。"""

    heading: str = Field(description="条文标题，如「预付款比例上限」")
    body: str = Field(description="条文正文：用应当/不得/不得超过这类规范表述，一条一个判据")
    explanation: str = Field(description="1~2 句语义解释：这条在管什么")
    checkpoints: list[str] = Field(description="2~4 条判定要点：看合同的哪个位置、比什么数值")
    guards: list[str] = Field(description="1~2 条误报护栏：哪些情况属正当、不该报")


class RiskTypeSuggestion(BaseModel):
    """这条政策该挂到哪个风险类型上（既有编码，或确实没有对应时写 new）。"""

    risk_type: str = Field(description="优先取既有风险类型编码；确无对应时写 new")
    label: str = Field(description="风险的中文短名，如「预付款比例过高」")
    why: str = Field(description="为什么归到这一类（一句话）")
    evidence_hint: str = Field(description="审查时从合同原文怎么认出来：看哪个条款、抓什么词或数值")


class SampleSuggestion(BaseModel):
    """建议造的验证样本：用哪类合同、注入什么缺陷、期望什么结论。"""

    goal: str = Field(description="这条样本要验证本细则的哪一点")
    kind: str = Field(description="合同品类：enterprise_goods / gov_goods / agri_goods / tech_service")
    defect: str = Field(description="注入的缺陷，写成能照着造出来的一句话")
    expected_grade: str = Field(description="期望评级：pass / conditional_pass / fail")


class RetrievalSuggestion(BaseModel):
    """建议的检索标准答案：一句提问 + 该命中的政策条文。"""

    query: str = Field(description="像合同审查员那样提的一个问题")
    policy_ref: str = Field(description="该命中的政策编号（本细则用需求里给的编号）")
    expect: str = Field(description="期望命中的条文要点关键词，供人工比对")


class DraftedPolicy(BaseModel):
    """一次起草的全部产出。"""

    title: str = Field(description="政策标题，形如「采购合同审核制度 · 细则 P-XX：主题」")
    scope: str = Field(description="适用范围：一句话说清管哪些合同、不管哪些")
    articles: list[DraftedArticle] = Field(description="条文，按「制度目的 → 实体要求 → 审查提示」排序")
    notes: list[str] = Field(description="需要人确认的地方：缺哪些元信息、哪些阈值待定、哪里没把握")
    risk_types: list[RiskTypeSuggestion] = Field(description="配套建议一：这条政策该挂哪些风险类型")
    samples: list[SampleSuggestion] = Field(description="配套建议二：建议造的验证样本")
    retrievals: list[RetrievalSuggestion] = Field(description="配套建议三：建议的检索标准答案")


_SYSTEM = """你在为一家集团的采购合规部门起草内部审核细则。你的产出会被人审、然后入库，
之后会作为合同审查的政策依据被检索和引用。

【体例】与现有细则保持一致：
1. 标题写成「采购合同审核制度 · 细则 P-XX：主题」。
2. 条文按「制度目的 → 实体要求 → 审查提示」排序，每条一个判据，标题用「预付款比例上限」这类短语。
3. 正文用规范表述（应当 / 不得 / 不得超过 / 超过某比例的应要求……），要能落到合同文本上核对，
   不写"加强管理""提高意识"这类空泛要求。

【数字纪律】比例、月数、天数只能用我在需求里给出的数字。需求没给的，
正文里写「按公司制度确定」，并把"这条阈值待定"写进 notes——不许自己编一个数。
需求只说"更严/从严"而没给数时同样如此：**不要自行挑一个更小的数**，写「按公司制度确定」
并在 notes 里写明"从严档阈值待定"。

【每条要配三样】
- 解释：这条在管什么（1~2 句）。
- 判定要点：审查时看合同的哪个位置、与什么比。
- 误报护栏：哪些情况属正当、不该报这条；要具体，例如"对方违约在先的免责不属本细则范围"。

【notes】写清缺哪些元信息、哪些阈值待定、你对哪条没把握。不要编造法律法规名称与条号；
确实需要依据时写"需人工补充依据"。

【配套三样】条文之外还要给三样落地配套，都要具体到能照着做：
- risk_types（该挂的风险类型）：先从下面的既有编码里挑，**risk_type 写编码本身**；
  这条政策管的缺陷确实不在其中时才写 "new" 并给一个短名。每项写清为什么归这一类，
  以及审查时怎么从合同原文认出来（看哪个条款、抓什么词或数值）。
  既有风险类型：
{risk_labels}
- samples（建议造的验证样本）：用哪类合同的（品类用上面那四个编码）、注入什么缺陷、
  期望什么结论（pass / conditional_pass / fail）。**品类只能写 enterprise_goods /
  gov_goods / agri_goods / tech_service 这四个编码之一**，不要写中文或别的英文词。
  每条一句话，照着就能造出合同。
- retrievals（检索标准答案建议）：像审查员那样提一句问题、该命中本细则的哪个编号、
  期望命中的条文要点关键词。

【体例样例】（只对齐写法与颗粒度：样例的主题、数字都只属于它自己，**一律不要沿用**；
新政策的数字只能来自我给的需求。样例刻意选了别的主题，就是免得你顺手抄它的数）

# 采购合同审核制度 · 细则 P-06：交付与验收管理

文件编号：P-06　　版本：V1.0　　生效日期：2026年9月9日
归口部门：集团采购管理中心
适用范围：本集团及下属单位对外签署的采购合同交付与验收环节。

## 第一条 制度目的

统一交付期限、验收标准与验收期限的审查口径，避免验收久拖不决导致付款条件悬空。

## 第二条 验收期限

合同应约定验收期限；未约定的应要求补充，且期限不得超过到货后 10 个工作日。

## 第三条 审查提示

验收期限按"到货日或供方提交验收申请之日的次日起算"核对；约定期限过长的应要求缩短。
"""


def draft_policy(
    brief: str,
    ref: str = "",
    group: str = "",
    effective_date: str = "",
    source_name: str = "",
    drafter: Callable[[str, str], dict] | None = None,
    retriever=None,
) -> dict:
    """按需求起草一份政策：调模型 → 归一成政策文本 → 走同一套起稿管线（重叠分级/冲突/清单）。

    drafter 可注入（(需求, 已知元信息行) -> dict），离线测试不必真调模型。
    """
    meta_line = "；".join(
        item
        for item in (
            f"文件编号：{ref}" if ref else "",
            f"归口部门：{group}" if group else "",
            f"生效日期：{effective_date}" if effective_date else "",
        )
        if item
    )
    drafted = (drafter or _default_drafter())(brief, meta_line)
    text = render_ai_draft(drafted, ref=ref, group=group, effective_date=effective_date)
    # 来源与人问的原话一并留档：回看时要知道这份条文是怎么来的、当时提了什么要求
    return drafts.write_draft(
        text,
        source=source_name or f"{ref or '新政策'}_AI起草.md",
        retriever=retriever,
        extra={
            "origin": "ai",
            "ai": {
                "brief": brief[:2000],
                "notes": drafted.get("notes", []),
                "articles": drafted.get("articles", []),
                "new_numbers": new_numbers(brief, text),
                "suggestions": normalize_suggestions(drafted),
            },
        },
    )


def render_ai_draft(drafted: dict, ref: str = "", group: str = "", effective_date: str = "") -> str:
    """把模型产出归一成政策文本：文件头 + 逐条正文 + 文末一条「审查提示」。

    解释与判定要点并进文末的审查提示条——现有细则就是这么写的（如 P-01 第四条），
    这样入库后检索与引用都能用到，而不是只留在页面上。
    """
    articles = drafted.get("articles") or []
    lines = [
        f"# {drafted.get('title') or '（待填标题）'}",
        "",
        f"文件编号：{ref or '（待填）'}　　版本：V1.0　　生效日期：{effective_date or '（待填）'}",
        f"归口部门：{group or '（待填）'}",
        f"适用范围：{drafted.get('scope') or '（待填）'}",
        "",
    ]
    for index, article in enumerate(articles, start=1):
        lines.extend(
            [f"## 第{cn_number(index)}条 {article['heading']}", "", article["body"].strip(), ""]
        )
    lines.extend([f"## 第{cn_number(len(articles) + 1)}条 审查提示", ""])
    for article in articles:
        lines.append(f"{article['heading']}：{article['explanation'].strip()}")
        for point in article.get("checkpoints") or []:
            lines.append(f"- 判定要点：{point}")
        for guard in article.get("guards") or []:
            lines.append(f"- 护栏：{guard}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def new_numbers(brief: str, text: str) -> list[dict]:
    """挑出模型自己引入的数字：成文里有、需求里没有的百分比与月数，逐条列出等人确认。"""
    allowed_percent = set(_PERCENT_RE.findall(brief or ""))
    allowed_months = set(_MONTH_RE.findall(brief or ""))
    findings: list[dict] = []
    for heading, body in _articles_of(text):
        percent = sorted(set(_PERCENT_RE.findall(body)) - allowed_percent)
        months = sorted(set(_MONTH_RE.findall(body)) - allowed_months)
        # 分支：这条没有新引入的数字 → 不占篇幅
        if percent or months:
            findings.append({"article": heading, "percent": percent, "months": months})
    return findings


def normalize_suggestions(drafted: dict) -> dict:
    """把模型给的配套建议收敛成能直接展示的三组：风险类型 / 样本 / 检索标准答案。

    判定能落到确定性地方的就别交给模型——风险类型编码必须对得上规则里登记的那份，
    自造编码单独标出来等人定；品类与评级取值不合法时归位，并在提示里说明改了哪里。
    返回 {risk_types, samples, retrievals, notes}，notes 只记"模型的取值被改过"这类提示。
    """
    risk_types: list[dict] = []
    seen: set[str] = set()
    for item in drafted.get("risk_types") or []:
        code = (item.get("risk_type") or "").strip()
        raw_label = (item.get("label") or "").strip()
        # 情况：同编码重复出现 → 只留第一条（模型常把一类拆成两行）
        if code in seen:
            continue
        seen.add(code)
        # 展示名以登记表为准：模型会把机器码原样填进 label（实测出现过），
        # 库里没有的编码才用它给的中文名，且要挡掉"new"这种没信息量的值
        label = RISK_LABELS.get(code, "") or (raw_label if raw_label.lower() != code.lower() else "")
        risk_types.append(
            {
                "risk_type": code or "new",
                "label": label,
                "why": item.get("why", ""),
                "evidence_hint": item.get("evidence_hint", ""),
                "known": code in RISK_LABELS,  # False=库里没这个编码，需人工定
            }
        )

    notes: list[str] = []  # 同一句提示只留一次（模型常把同一类错误重复三遍）
    samples: list[dict] = []
    for item in drafted.get("samples") or []:
        kind = (item.get("kind") or "").strip()
        grade = (item.get("expected_grade") or "").strip()
        # 情况：品类不在四类里 → 按企业货物品类处理并留提示，别把非法值原样展示
        if kind not in _KINDS:
            note = f"样本建议里的品类「{kind or '空'}」不在四类里，先按企业货物品类处理"
            if note not in notes:
                notes.append(note)
            kind = "enterprise_goods"
        # 情况：期望评级不是三档之一 → 留空（页面显示"待定"），不替模型猜结论
        if grade not in _GRADES:
            note = f"样本建议里的期望评级「{grade or '空'}」不是三档之一，先按待定处理"
            if note not in notes:
                notes.append(note)
            grade = ""
        # 情况：模型把品类编码串进了 goal（实测发生过）→ 那不是"要验证什么"，留空不展示
        goal = (item.get("goal") or "").strip()
        samples.append(
            {
                "goal": "" if goal in _KINDS else goal,
                "kind": kind,
                "defect": item.get("defect", ""),
                "expected_grade": grade,
            }
        )

    retrievals = [
        {
            "query": item.get("query", ""),
            "policy_ref": item.get("policy_ref", ""),
            "expect": item.get("expect", ""),
        }
        for item in drafted.get("retrievals") or []
    ]
    return {"risk_types": risk_types, "samples": samples, "retrievals": retrievals, "notes": notes}


def cn_number(value: int) -> str:
    """1~99 的阿拉伯数字转中文（1→一、11→十一、21→二十一）：条号按政策体例写中文。"""
    if value <= 10:
        return _CN_NUM[value - 1]
    if value < 20:
        return "十" + _CN_NUM[value - 11]
    tens, ones = divmod(value, 10)
    return _CN_NUM[tens - 1] + "十" + (_CN_NUM[ones - 1] if ones else "")


def _articles_of(text: str) -> list[tuple[str, str]]:
    """把政策文本按「## 第X条 标题」切成 (标题, 正文) 对，供数字核对逐条定位。"""
    matches = list(re.finditer(r"(?m)^##\s*(第[一二三四五六七八九十百\d]+条.*)$", text or ""))
    return [
        (
            match.group(1).strip(),
            text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)],
        )
        for index, match in enumerate(matches)
    ]


def _default_drafter() -> Callable[[str, str], dict]:
    """默认起草器：真调模型（结构化输出），把结果转成 dict。"""
    # 起草要跑一整篇条文，比抽取慢；给个上限，卡住的连接宁可报错也不要把请求吊死
    structured = get_chat_model(timeout=DRAFT_TIMEOUT_SECONDS).with_structured_output(DraftedPolicy)
    # 既有风险类型编码交给模型挑：让它知道库里已有什么，别自己发明编码
    system = _SYSTEM.replace("{risk_labels}", "\n".join(f"- {k}：{v}" for k, v in RISK_LABELS.items()))

    def run(brief: str, meta_line: str) -> dict:
        question = f"【需求/要点】\n{brief}"
        # 分支：调用方给了编号/归口/生效日期 → 一并交给模型，别让它去猜
        if meta_line:
            question += f"\n\n【已知元信息】\n{meta_line}"
        with llm_call(STAGE_DRAFT):
            drafted = structured.invoke([("system", system), ("human", question)])
        return drafted.model_dump()

    return run
