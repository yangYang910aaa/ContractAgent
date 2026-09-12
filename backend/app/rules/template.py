"""
空白模板检测 + 新增补充协议识别
"""

from __future__ import annotations

import re

from backend.app.schemas import RiskItem, Severity
from backend.app.rules.constants import RISK_LABELS


# ---- 空白模板占位检测----

# 各类占位标记的正则（每类只要命中一次即计数）：
# - date：年/月/日之间只有空格/下划线/全角空格（真实日期中间是数字或中文数字）
# - amount：金额词空值或"￥　　元"形态（填写后会有 数字/大写中文 等实义字符）
# - amount_cap：金额大写栏空白（大写：＿＿＿）
# - party：冒号后跟下划线（甲方（采购方）：＿＿＿）
# - fill：成串下划线/全角下划线（模板填空位）
# - blank：冒号后只剩空白直到行尾（真空白值；易错点：不得用"冒号+空格"判断，
#   PDF 抽取把"甲方：    乙方："这类排版间距也带出空格，会误伤已签合同）
# - dot：点线/省略号填充栏（GF 示范文本用 "……………" 引出待填内容）
# - box：□ 勾选/未选框（示范文本"选项处打 √/×"结构）
# 说明：空格+标点/单位这两类被证实是 PDF 排版的空格造成的（已签合同也命中），
# 会误报"疑似空白模板"，只在"未填写文本"场景参与判定
_BLANK_PATTERN_RE: dict[str, re.Pattern] = {
    "date": re.compile(r"年[ ＿_\u3000]*月[ ＿_\u3000]*日"),
    "amount": re.compile(
        r"(?:金额|价款|总价|货款)[为是：:]\s*[＿_ \u3000]*元(?!\s*[）)])"
        r"|[￥¥][\s＿_\u3000]{2,}元"
    ),
    "amount_cap": re.compile(r"大写[：:]\s*[＿_ \u3000]*[）)]"),
    "party": re.compile(r"[：:]\s*[＿_]{2,}"),
    "fill": re.compile(r"[＿_]{3,}"),
    "blank": re.compile(r"[：:][ \u3000]{3,}(?=\n|$)"),
    "dot": re.compile(r"[.．…]{3,}"),
    "box": re.compile(r"□"),
    # 填空式条款的空标点/空单位（霸王花式模板："标准是 ；""定金 元"）：
    # 易错点——PDF 排版抽取也会在正常句子里带出"空格+标点/单位"，故仅在"未填写文本"
    # 场景参与判定（见 _looks_filled），已填写合同里这两类一律忽略
    # 易错点：① 只认空格与全角空格、不认换行——PDF 折行会造出"值\n，"这种假空标点；
    # ② 单位前后紧邻数字说明该值已填，故加数字邻接限制
    "void_punct": re.compile(r"[\u4e00-\u9fff%][：:]?[ \u3000]{1,3}[。；,，．]"),
    "void_unit": re.compile(r"(?<![\d])[ \u3000](?:%|元|日内|天内|项|种方式|方)(?![\s\u3000]*[\d≤≥<])"),
}


# 已填写合同的形态特征：有带数字的年份/年月 + 数字化金额（真实已签合同/正常样本都满足；
# 真实 PDF 合同常只写"2025 年"（项目名/期限），故年份单独出现也算已填写）
_FILLED_DATE_RE = re.compile(r"\d{4}\s*年|年\s*\d{1,2}\s*月")


_FILLED_AMOUNT_RE = re.compile(r"\d[\d,]{2,}(?:\.\d+)?\s*(?:元|万元)")


# 仅在"未填写文本"里算证据的类别（PDF 排版空格所致，见上）
_ARTIFACT_CATEGORIES = {"void_punct", "void_unit"}


# 签名/签署栏上下文：这些栏位的日期空白只说明"未写签署日期"，
# 不能据此把整份合同判为空白模板
_SIGNATURE_CONTEXT_RE = re.compile(r"签订(?:时间|地点|日期)|盖章|（章）|\(章\)|签约|双方签字")


# 判定为"疑似空白模板"：① 占位类别 ≥2（防单处偶发）且 ② 至少一类属"强证据"。
# 强证据 = 真空白值域（amount / amount_cap / party / fill / blank）；
# 易错点：date 空白在已签合同的署名页/页脚也常见（"年 月 日"处），只能计数、不能定罪；
# box、dot（选项框/点线）在真实合同里同样常见，也只能作旁证。
_BLANK_SUSPECT_MIN_CATEGORIES = 2


_BLANK_STRONG_CATEGORIES = {"amount", "amount_cap", "party", "fill", "blank"}


# 出 evidence 摘录时优先"看得出是哪个栏位"的类别；纯空白/点线/选框摘出来
# 不像话，只参与计数、不抢摘录位
_SNIPPET_CATEGORIES = ("date", "amount", "amount_cap", "party", "fill")


# 补充/变更协议标题：出现在正文开头（前 300 字）才算标题，避免正文里一句
# "本合同未尽事宜可另签补充协议"把完整合同误判成补充件
_SUPPLEMENTARY_TITLE_RE = re.compile(r"补充协议|补充合同|变更协议|补充约定|之补充")
# 继承原合同的表述：补充协议的标准写法，说明未被修改的条款依然有效
_SUPPLEMENTARY_INHERIT_RE = re.compile(
    r"(?:原合同|本合同|主合同)[^。；\n]{0,16}(?:其余|其他|剩余)[^。；\n]{0,10}(?:继续有效|仍然有效|继续履行|保持不变)"
    r"|(?:除|除本)[^。；\n]{0,24}(?:修改|变更|调整|补充)[^。；\n]{0,10}(?:之外|以外)[^。；\n]{0,16}(?:继续有效|仍然有效|保持不变)"
    r"|(?:未(?:作)?修改|未(?:作)?变更|未涉及)[^。；\n]{0,10}(?:条款|部分|内容)[^。；\n]{0,10}(?:继续有效|仍然有效|保持不变)"
)


def is_supplementary_agreement(text: str) -> bool:
    """原文是不是补充/变更协议（只改原合同某几条、其余条款继承）。

    两个信号都要求：标题字样（开头 300 字内）与"其余部分继续有效"类继承句。
    只看标题会被完整合同里"未尽事宜可另签补充协议"带跑；只看继承句又会漏掉
    没写这句的补充件——漏判只是维持现状，误判会让整组条款检查失效。
    """
    body = text or ""
    if not _SUPPLEMENTARY_TITLE_RE.search(body[:300]):
        return False
    return _SUPPLEMENTARY_INHERIT_RE.search(body) is not None


def _blank_markers(text: str) -> tuple[set[str], str]:
    """扫原文找占位痕迹，返回 (命中的类别集合, 首段占位原文摘录)。"""
    filled = len(_FILLED_DATE_RE.findall(text)) > 0 and bool(_FILLED_AMOUNT_RE.search(text))
    found: set[str] = set()
    snippet = ""
    for category, pattern in _BLANK_PATTERN_RE.items():
        # 这种情况是：文本看起来已填写完整 → 忽略排版空格类假信号
        if filled and category in _ARTIFACT_CATEGORIES:
            continue
        for match in pattern.finditer(text):
            # 这种情况是：日期空白出现在署名/签订栏 → 只是未写签署日期，不算整份空白
            if category == "date" and _SIGNATURE_CONTEXT_RE.search(
                text[max(match.start() - 24, 0) : match.end() + 8]
            ):
                continue
            found.add(category)
            if not snippet and category in _SNIPPET_CATEGORIES:
                snippet = match.group(0).strip()[:80]
            break
    return found, snippet


def is_blank_template_suspect(text: str) -> bool:
    """原文是否像空白/未定稿模板。

    已填写文本（有数字化年月与金额）要求占位类别 ≥2 且含"真空白值域"强证据，
    防署名栏空日期被误判；未填写文本沿用占位类别 ≥2，保证各类官方模板仍能识别。
    """
    found, _ = _blank_markers(text or "")
    if len(found) < _BLANK_SUSPECT_MIN_CATEGORIES:
        return False
    filled = len(_FILLED_DATE_RE.findall(text or "")) > 0 and bool(_FILLED_AMOUNT_RE.search(text or ""))
    # 分支：已填写文本 → 必须命中"真空白值域"强证据；未填写文本 → 原口径放行
    return bool(found & _BLANK_STRONG_CATEGORIES) if filled else True


def annotate_template_risks(risks: list[RiskItem], text: str) -> list[RiskItem]:
    """模板场景下的风险标注：缺必填 high → medium，并附一条"疑似空白模板"。

    口径：仅当 ① 原文疑似空白模板 且 ② 风险里确有"缺必填"时生效；
    其他缺陷（质保/违约金等）severity 原样保留，仍可能触发闸口。
    返回新列表，不修改入参。
    """
    if not is_blank_template_suspect(text):
        return risks
    # 这种情况是：没有"缺必填"风险 → 模板检测不影响本份结果
    if not any(r.risk_type == "missing_required_field" for r in risks):
        return risks
    _, snippet = _blank_markers(text)
    out = [
        # 缺必填因模板占位而降级（medium）；其余风险原样
        r.model_copy(update={"severity": Severity.medium})
        if r.risk_type == "missing_required_field" and r.severity == Severity.high
        else r
        for r in risks
    ]
    out.append(
        RiskItem(
            risk_type="blank_template_suspected",
            label=RISK_LABELS["blank_template_suspected"],
            severity=Severity.medium,
            field="",
            evidence=snippet,
            suggestion=(
                "原文含多处空白占位（甲方/签署日期/金额未填写），疑似空白模板或未定稿版本。"
                "请确认是否上传了填写完整的最终签署版；若确为模板本身，无需逐条补全。"
            ),
            policy_ref=None,
        )
    )
    return out
