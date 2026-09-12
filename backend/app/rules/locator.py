from __future__ import annotations

import re

from backend.app.rules.constants import PAGE_MARK_RE
from backend.app.schemas import RiskItem


def _sentence_quote(text: str, pos: int, max_chars: int = 120) -> str:
    """取 pos 所在**整句**的摘录（前后切到句读边界），供原文定位/高亮用。

    按最近的分句标点取整句，再折叠空白、限长——原来看固定宽度的窗口，摘录常从半句中间
    开始（"…求与标准的与本服务项目有关的所有费用…"），前端高亮看着像"一大块"。
    """
    # 只用句读做边界，**不含换行**——PDF/OCR 文本每行硬换行，含换行会把句子切碎
    # （实测 "5.2付款方式" 后面紧跟换行，摘录就只剩标题两个字）
    separators = "。；;"
    start = max((text.rfind(ch, 0, pos) for ch in separators), default=-1) + 1
    ends = [text.find(ch, pos) for ch in separators]
    ends = [e for e in ends if e != -1]
    end = (min(ends) + 1) if ends else len(text)
    quote = re.sub(r"\s+", "", text[start:end])
    return quote[:max_chars]


def _locate_missing_field(text: str, field: str | None) -> tuple[str, str]:
    """缺必填字段的"原文该去哪找"：返回 (clause_ref, evidence 摘录)。

    字段没抽到时证据天然为空、风险卡上就没有"原文定位"，而缺必填恰恰最需要指路。
    这里按字段类型在正文里找最可能写该字段的句子（总额→"合同总价款"、币种→"人民币"、
    到期日→"有效期"）当定位锚点；找不到锚点返回空，不硬编造位置。
    """
    anchors = _MISSING_FIELD_ANCHORS.get(field or "")
    if not anchors:
        return "", ""
    # 分支：本字段的关键词一个都没出现（如合同通篇只写"价格条款"没写"人民币/币种"）
    # → 退回到"同类字段"的锚点（币种缺失该补在金额条款，不是没地方可指）
    fallback = _MISSING_FIELD_FALLBACK.get(field or "")
    if fallback and not any(re.search(k, text) for k in anchors):
        anchors = _MISSING_FIELD_ANCHORS.get(fallback, anchors)
    for keyword in anchors:
        match = re.search(keyword, text)
        if match:
            return _clause_ref_at(text, match.start()), _sentence_quote(text, match.start())
    return "", ""


# 缺必填字段 → 正文锚点关键词（按"先具体后笼统"排序，避免"金额"把无关句子捞出来）
_MISSING_FIELD_ANCHORS: dict[str, tuple[str, ...]] = {
    "total_amount": ("合同总价款", "合同总价", "合同金额", "合同价款", "总金额", "金额为", "货款"),
    "currency": ("币种", "人民币", "合同总价款", "合同金额"),
    "signature_date": ("签订时间", "签署日期", "签订日期", "签字盖章"),
    "effective_date": ("之日起生效", "生效条件", "签署并生效", "生效"),
    # 易错点：别用裸"合同期"——"履行合同期间"这类表述会误命中（实测指到了权利义务条款）
    "expiry_date": ("有效期", "合同期限", "服务期限", "合作期限", "保修期", "工期"),
    "buyer": ("甲方", "需方", "买方", "采购人", "委托人", "发包人"),
    "supplier": ("乙方", "供方", "卖方", "承包人", "供应商", "监理人"),
    "payment_schedule": ("付款", "支付方式", "结算", "价款支付"),
    "penalty_rate": ("违约金", "违约责任", "逾期"),
    "liability_cap": ("赔偿责任", "责任限额", "为上限"),
    "warranty_months": ("质保", "保修", "质量保证期"),
    "confidentiality_months": ("保密",),
    "termination_notice_days": ("解除", "终止", "提前通知"),
    "ip_ownership": ("知识产权", "成果归属"),
    "governing_law": ("适用法律", "中华人民共和国法律", "争议解决"),
    "contract_kind": ("合同",),
}


# 同类字段回落：本字段关键词全无时，指向"该字段本该写在哪儿"的同类锚点
_MISSING_FIELD_FALLBACK: dict[str, str] = {
    "currency": "total_amount",
    "payment_schedule": "total_amount",
}


def _annotate_missing_locators(risks: list[RiskItem], text: str) -> list[RiskItem]:
    """给风险补原文定位：缺必填补摘录，字段类规则补一句真原文引用。

    缺必填的证据本就是空的，定位到的原句直接当证据；字段类规则的证据是规则拼的说明句
    （正文里搜不到），另外存一句真原文供前端定位与高亮，说明文案不动。
    """
    if not text:
        return risks
    out: list[RiskItem] = []
    for risk in risks:
        # 分支：只处理"缺必填"且当前没有定位信息的条目
        if risk.risk_type == "missing_required_field" and not risk.evidence:
            ref, evidence = _locate_missing_field(text, risk.field)
            if evidence:
                out.append(risk.model_copy(update={"clause_ref": ref, "evidence": evidence}))
                continue
            out.append(risk)
            continue
        # 分支：说明句本身就在正文里（条款类规则常直接抄原文）→ 直接当摘录用
        if not risk.evidence_quote and risk.evidence and risk.evidence in text:
            out.append(risk.model_copy(update={"evidence_quote": risk.evidence}))
            continue
        # 分支：说明句不在正文里 → 按字段锚点找一句真正的原文当摘录
        if not risk.evidence_quote:
            _, quote = _locate_missing_field(text, risk.field)
            if quote:
                out.append(risk.model_copy(update={"evidence_quote": quote}))
                continue
        out.append(risk)
    return out


def _clean_page_marks(text: str) -> str:
    """去掉 OCR 页标记，保留其余字符与换行（规则/摘录都应按"干净正文"工作）。

    页标记是解析时为人工逐页核对插的（"--- 第 N 页 ---"），但会占掉正则窗口预算，
    还会混进给用户看的摘录（"…各自单---第3页---位公章…"）。
    纯文本页签仍保留原样，那里本来就是机器快照。
    """
    return PAGE_MARK_RE.sub("", text or "")


def _find_quote_pos(text: str, quote: str) -> int:
    """找摘录在原文里的起始下标，找不到返回 -1。

    先精确匹配，不中再丢掉空白与 OCR 页标记匹配一次：页标记会把同一句从中间截开，
    而模型抄出的摘录是连续句，不做宽松比对就会漏、定位退化成整块高亮。
    """
    pos = text.find(quote)
    if pos >= 0:
        return pos
    flat_quote = PAGE_MARK_RE.sub("", re.sub(r"\s+", "", quote))
    if not flat_quote:
        return -1
    # 宽松比对：边扫原文边记录"保留下来的字符"各自在原文里的下标，命中后回查原下标
    keep_idx: list[int] = []
    skip_re = re.compile(rf"(?:{PAGE_MARK_RE.pattern})|\s+")
    i = 0
    while i < len(text):
        skip = skip_re.match(text, i)
        if skip:
            i = skip.end()
            continue
        keep_idx.append(i)
        i += 1
    flat_text = "".join(text[i] for i in keep_idx)
    at = flat_text.find(flat_quote)
    return keep_idx[at] if at >= 0 else -1


def _normalize_clause_refs(risks: list[RiskItem], text: str) -> list[RiskItem]:
    """把正文里不存在的条款号，按摘录位置纠正为所属章节。

    条款号来自模型自述，会写出原文没有的章节号，前端据此列出假条款、点定位也滚不动。
    只在"原号不在正文"且"能按摘录回推"时改写，回推不出就保留原值。返回新列表。
    """
    if not text:
        return risks
    out: list[RiskItem] = []
    for risk in risks:
        ref = (risk.clause_ref or "").strip()
        # 分支：条款号为空、或能在正文里原样找到 → 无需纠正
        if not ref or ref in text:
            out.append(risk)
            continue
        # 分支：正文找不到 → 用摘录位置回推所在章节；回推不出就原样保留
        quote = (risk.evidence_quote or risk.evidence or "").strip()
        pos = _find_quote_pos(text, quote) if quote else -1
        derived = _clause_ref_at(text, pos) if pos >= 0 else ""
        # 回推出的章节空/与摘录同段但无章节头 → 保留原值，不做无依据的清空
        out.append(risk.model_copy(update={"clause_ref": derived}) if derived else risk)
    return out


# 条款标题行（回指 evidence 所在条款用）：第X条 / 章节式"七、"两种头
_CLAUSE_HEADER_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百\d]+条|(?:[一二三四五六七八九十]+)、)[^\n]{0,24}$"
)


def _text_excerpt(text: str, pos: int, width: int = 120) -> str:
    """截取命中位置附近的原文作证据：取 pos 前后 width/2 字符，折叠空白，防摘录过长。"""
    start = max(pos - width // 2, 0)
    return re.sub(r"\s+", "", text[start : pos + width // 2]).strip()


def _clause_ref_at(text: str, pos: int) -> str:
    """找 pos 前的最后一个条款/章节标题作 clause_ref（无标题返回空串）。"""
    for line in reversed(text[:pos].splitlines()):
        if _CLAUSE_HEADER_RE.match(line):
            return line.strip()
    return ""
