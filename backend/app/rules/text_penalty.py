"""
新规则:保密例外 / 违约金基数 / 违约金上限
"""

from __future__ import annotations

import re

from backend.app.schemas import RiskItem, Severity
from backend.app.rules.constants import RISK_LABELS
from backend.app.rules.locator import _clause_ref_at, _text_excerpt


# ---- 保密例外与违约金基数/上限 ----

# 保密义务信号：正文写了保密安排，才谈"例外缺不缺"
_CONF_OBLIGATION_RE = re.compile(r"保密(?:义务|责任|条款|信息|内容)|负有保密|商业秘密|技术秘密|保密资料")


# 绝对禁止式披露（素材清单描述的目标缺陷形态："只写不得向任何第三方披露"）。
# 易错点：不能把普通"负有保密义务"也当缺陷——存量 32 份语料大多只有义务句，
# 那样会大面积新增 medium；实测绝对式措辞在存量语料上零命中，可以放心收紧。
_CONF_ABSOLUTE_RE = re.compile(
    r"不得(?:向|对)?(?:任何)?(?:第三方|第三人|他人|其他单位|任何单位)(?:披露|泄露|提供|公开)"
    r"|一律不得披露|严禁(?:向|对外)?披露"
)


# 保密例外信号：法定/监管/司法披露、已公开、独立开发、经对方书面同意、履约所必需、除外条款
_CONF_EXCEPTION_RE = re.compile(
    r"法律规定|法律法规|依法(?:披露|提供)|监管(?:机关|部门|机构)|司法(?:机关)?要求|法院"
    r"|仲裁.{0,6}要求|已(?:进入)?公开|公开(?:信息|领域)|公共领域|独立(?:开发|研发)|书面同意"
    r"|为履行.{0,8}(?:所必需|必要)|除外"
)


# 例外判定的"紧邻短句"上限（字符）：例外也可能写成紧随其后的独立短句
# （"……不得披露。法律法规另有规定的除外。"）。易错点——按固定字符窗口（±120/±60）
# 判定会把隔壁条款的"未经甲方书面同意"（转包）误当保密例外，
# 故改为"同一句 + 仅当紧邻句以除外类引导词开头才并入"
_CONF_EXCEPTION_TAIL_CHARS = 60


_CONF_EXCEPTION_TAIL_RE = re.compile(r"\s*(?:除|但|法律|法规|监管|司法)")


# 比例型违约金数值：万分之X / 千分之X / N% / N‰
_PENALTY_PCT_RE = re.compile(
    r"万分之[\d一二三四五六七八九十]+|千分之[\d一二三四五六七八九十]+"
    r"|[0-9]+(?:\.[0-9]+)?\s*%|[0-9]+(?:\.[0-9]+)?‰"
)


# 违约金基数词：句内出现任何一个"金额/数量类名词"即视为基数已明确。
# 教训：起初按品类逐个枚举（合同总价/技术开发费/订单金额…），结果真实合同
# 的写法永远多一种——"延期货款""订单总金额""当批货物总额"接连漏判，反而制造误报；
# 改为宽口径识别名词类别（判"有没有说清按什么算"），判不准时宁可不报。
_PENALTY_BASIS_RE = re.compile(
    r"金额|价款|货款|总价|总额|费用|价格|单价|造价|结算价|数量|基数|标准|部分|订单|批次|合同价"
)


# 按日计罚信号（"每逾期一日/按日/每拖延一天"）
_PENALTY_DAILY_RE = re.compile(r"每(?:日|天)|按日|每逾期一[日天]|每延迟一[日天]|每拖延一[日天]|每推迟一[日天]")


# 违约金上限信号
_PENALTY_CAP_RE = re.compile(r"不超过|最高不超过|累计不超过|上限|封顶|以.{0,8}为限")


# 上限回溯窗口（字符）：只看命中处之后这么远，避免把别处的上限借过来当本条封顶
# （真实钢结构合同：0.5%/日 那句之后 271 字才是另一条款的"不超过 8%"）
_PENALTY_CAP_WINDOW = 150


# 无上限判定的日费率门槛（%/日）：0.05%/日是行业常见写法，无上限的实际敞口有限
# （100 天累计 5%），报出来只是噪音；0.5%/日 这类高费率无封顶才会失控（真实钢结构件）
_PENALTY_UNCAPPED_MIN_DAILY_PERCENT = 0.1


# 基数判定的回看窗口（字符）：PDF 抽取常在"按合同价款的"与"1‰"之间插换行，
# 只看命中所在"句"会把基数词切到上一行（真实钢结构合同即如此）→ 往前多看 80 字。
# 方向仍以"宁可不报"为准：窗口内出现金额类名词就认为基数已写明。
_PENALTY_BASIS_LOOKBACK = 25


# 中文数字 → 数值（万分之X/千分之X 的 X 可能是中文，模板与真实合同都常见）
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _daily_penalty_percent(sentence: str) -> float | None:
    """从违约金句里取日费率（%/日）；取不到返回 None（宁缺毋滥，不据此定罪）。

    覆盖真实写法：0.5% / 5‰ / 万分之三 / 千分之五；多个数值取最大（同句常同时写
    "万分之三"与"0.03%"两种等价表述）。
    """
    values: list[float] = []
    for num in re.finditer(r"(\d+(?:\.\d+)?)\s*%", sentence):
        values.append(float(num.group(1)))
    for num in re.finditer(r"(\d+(?:\.\d+)?)\s*‰", sentence):
        values.append(float(num.group(1)) / 10)  # 1‰ = 0.1%
    for cn in re.finditer(r"万分之([\d一二三四五六七八九十]+)", sentence):
        raw = cn.group(1)
        values.append((float(raw) if raw.isdigit() else _CN_DIGITS.get(raw, 0)) / 100)
    for cn in re.finditer(r"千分之([\d一二三四五六七八九十]+)", sentence):
        raw = cn.group(1)
        values.append((float(raw) if raw.isdigit() else _CN_DIGITS.get(raw, 0)) / 10)
    return max(values) if values else None


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    """取 pos 所在句子的起止下标（句读按 。；;\\n 切），供"句内口径"判定用。

    易错点：基数这类判定必须在句内看——跨句会把责任上限句里的"合同总价款"
    借来当违约金基数，把真实缺陷判成合规。
    """
    start = max((text.rfind(ch, 0, pos) for ch in "。；;\n"), default=-1) + 1
    ends = [text.find(ch, pos) for ch in "。；;\n"]
    ends = [e for e in ends if e != -1]
    return start, (min(ends) if ends else len(text))


def _check_confidentiality_no_exception(text: str) -> RiskItem | None:
    """P-13 保密例外检查：保密条款写成"绝对不得披露"且无任何例外 → medium。

    口径（买方视角）：我方常有法定/监管披露义务（审计、监管报送、诉讼举证），
    条款若一律禁止披露，履行法定义务反而违约 → 提示补例外。
    触发刻意收紧为"绝对禁止式"措辞，普通保密义务句不报（防存量语料大面积误报）。
    """
    for m in _CONF_ABSOLUTE_RE.finditer(text):
        # 例外信号要在同一句内出现才算"有例外"（远处争议解决条款的"法院"不算）
        start, end = _sentence_span(text, m.start())
        scope = text[start:end]
        # 例外也可能写成紧随其后的独立短句，但只认以除外类引导词开头的下一句
        tail = text[end + 1 : end + 1 + _CONF_EXCEPTION_TAIL_CHARS]
        if _CONF_EXCEPTION_TAIL_RE.match(tail):
            scope += tail
        if _CONF_EXCEPTION_RE.search(scope):
            continue
        return RiskItem(
            risk_type="confidentiality_no_exception",
            label=RISK_LABELS["confidentiality_no_exception"],
            severity=Severity.medium,
            clause_ref=_clause_ref_at(text, m.start()),
            evidence=_text_excerpt(text, m.start()),
            policy_ref="P-13",
            suggestion=(
                "保密条款只写“不得向第三方披露”、未留任何例外，建议按 P-13 补充：法律法规或"
                "监管/司法机关要求披露、已公开信息、独立开发、经对方书面同意等情形不属于违约。"
            ),
            field=None,
        )
    return None


def _check_penalty_basis_unclear(text: str) -> RiskItem | None:
    """P-14 违约金基数检查：句内写了比例违约金却没写基数 → medium。

    基数不明（按总额还是未履行部分、是否含税）会让违约金无法计算、争议时各执一词。
    """
    for m in _PENALTY_PCT_RE.finditer(text):
        start, end = _sentence_span(text, m.start())
        sentence = text[start:end]
        # 分支 1：本句没提违约金（如责任上限句的百分比）→ 不属本规则
        if "违约金" not in sentence:
            continue
        # 分支 2：本句（含往前 80 字，PDF 换行会把基数词切到上一行）已写基数 → 合规
        window = text[max(start - _PENALTY_BASIS_LOOKBACK, 0) : end]
        if _PENALTY_BASIS_RE.search(window):
            continue
        # 分支 3：写了比例却没写基数 → medium
        return RiskItem(
            risk_type="penalty_basis_unclear",
            label=RISK_LABELS["penalty_basis_unclear"],
            severity=Severity.medium,
            clause_ref=_clause_ref_at(text, m.start()),
            evidence=_text_excerpt(text, m.start()),
            policy_ref="P-14",
            suggestion=(
                "违约金只写了比例、未写明计算基数（合同总价/未履行部分/逾期部分，是否含税），"
                "建议按 P-14 明确基数与计算方式。"
            ),
            field=None,
        )
    return None


def _check_penalty_cap_missing(text: str) -> RiskItem | None:
    """P-14 违约金上限检查：按日计罚且近旁无上限 → high（长期拖延可超本金）。

    口径：按日比例若无封顶，工期越长违约金越高、可能超过合同总额本身，对买方同样是
    失控敞口；上限句通常紧跟违约金句，故只看命中后的固定窗口，不取全文
    （真实合同里别的条款写了上限，不能算本条的封顶）。
    """
    hit = None
    rate: float | None = None
    for m in _PENALTY_DAILY_RE.finditer(text):
        start, end = _sentence_span(text, m.start())
        sentence = text[start:end]
        # 分支：本句没提违约金（如"每日巡检"）→ 继续找下一处
        if "违约金" not in sentence:
            continue
        hit, rate = m, _daily_penalty_percent(sentence)
        break
    # 分支 1：没有按日违约金 → 不套本规则（按次/一次性违约金走 P-03 口径）
    if hit is None:
        return None
    # 分支 2：日费率取不到、或低于门槛（0.05%/日 等常见写法）→ 不报（防噪音：低费率
    #    无上限的敞口有限，真实语料里这类写法很普遍）
    if rate is None or rate < _PENALTY_UNCAPPED_MIN_DAILY_PERCENT:
        return None
    # 分支 3：近旁写了上限（不超过/最高不超过/为限…）→ 视为已封顶
    if _PENALTY_CAP_RE.search(text[hit.start() : hit.start() + _PENALTY_CAP_WINDOW]):
        return None
    # 分支 4：日费率高且无上限 → high
    return RiskItem(
        risk_type="penalty_cap_missing",
        label=RISK_LABELS["penalty_cap_missing"],
        severity=Severity.high,
        clause_ref=_clause_ref_at(text, hit.start()),
        evidence=_text_excerpt(text, hit.start()),
        policy_ref="P-14",
        suggestion=(
            f"逾期违约金按日 {rate:g}% 计收却没有累计上限（长期拖延将超过合同总额），"
            "建议按 P-14 增加“违约金总额不超过合同总价款 X%”的上限。"
        ),
        field=None,
    )
