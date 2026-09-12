from __future__ import annotations

import re
from decimal import Decimal


# ---- 政策阈值（百分比数值；金额单位：元）----
PREPAY_MAX_PERCENT = 30.0  # P-01：预付款不超过总额 30%


WARRANTY_MIN_MONTHS = 12  # P-02：质保期不少于 12 个月


# 责任上限底线(占总额 %)，按品类区分：货物/服务采购 50%；技术开发(委托/合作/服务)类行业惯例普遍把赔偿上限压到 30%，底线放宽到 30%，
LIABILITY_CAP_MIN_PERCENT: dict[str, float] = {
    "enterprise_goods": 50.0,
    "tech_service": 30.0,
}


CONFIDENTIALITY_MAX_MONTHS = 36  # P-04：保密期不超过 36 个月


PENALTY_DAILY_MAX_PERCENT = 1.0  # 违约金日利率上限


AMOUNT_TOLERANCE_RATIO = Decimal("0.01")  # 分项加总 vs 总额允许偏差 1%


# 计入预付款，备料款/启动款同理
_PREPAY_NAME_KEYWORDS = ("预付", "首付", "备料款", "启动款")


# 风险类型机器码 → 中文展示名（risk_type 是评测/接口对齐的编码，展示永远走
# 中文 label；新增风险类型时必须在此登记，否则界面会裸显机器码）
RISK_LABELS: dict[str, str] = {
#字段级规则:evaluate()函数,依赖抽取出来的字段。这类规则先拿到ContractModel,再对字段进行逻辑判断。   
    "missing_required_field": "缺失必填字段", #high/medium
    "date_logic_effective_before_signature": "生效日早于签署日", #medium
    "date_logic_expiry_not_after_effective": "到期日不晚于生效日", #medium
    "amount_inconsistency": "付款金额不一致", #付款期次加总≠总额（偏差 > 1%）,high/medium
    "prepayment_ratio_high": "预付款比例过高", #>30%,high,P-01
    "warranty_too_short": "质保期不足", # <12个月, high,P-02
    "liability_cap_unclear": "责任上限未明确", #medium,P-03
    "liability_cap_too_low": "责任上限过低", #责任上限<品类底线(企业50%,技术开发30%),high,P-03
    "confidentiality_missing": "缺少保密条款或未约定期限", #medium,P-04
    "confidentiality_too_long": "保密期过长",  #>36个月,high,P-04
    "penalty_rate_too_high": "违约金比例畸高", #日利率>1%,high,P-03
    "ip_ownership_missing": "未约定知识产权归属", #medium,P-05
    "ip_ownership_unclear": "知识产权归属不清", #medium,P-05
    "governing_law_missing": "缺少适用法律约定", #medium,P-05

#文本级规则:text_rules()函数,直接扫原文,不依赖抽取字段。用正则直接在合同原文找关键词。
    "acceptance_unclear": "验收标准或期限不明确", #medium, P-06
    "invoice_unclear": "发票开具约定缺失", #medium, P-07
    "performance_bond_missing": "履约担保缺失", #大额(>=100w)或含预付的合同缺履约担保medium, P-08
    "subcontract_unrestricted": "转包/分包未作限制",#未限制转包=medium;"任意转包且甲方无权追责"=high, P-09
    "personal_info_clause_missing": "未约定个人信息保护义务", #medium, P-10
    "data_processing_terms_missing": "委托处理要件不完整", #(目的、期限、方式、种类、措施、删除)命中<3项。medium, P-10
    "data_cross_border_unclear": "数据出境缺少合规路径",#(安全评估/标准合同/认证) high P-11
    "data_deletion_missing": "未约定数据删除与泄露通知",#medium, P-12
    "confidentiality_no_exception": "保密条款缺少例外",
    "penalty_basis_unclear": "违约金基数不明",
    "penalty_cap_missing": "违约金无上限",

#特殊标注:预警提示，需要人工审核
    "blank_template_suspected": "疑似空白模板",  
}


# ContractModel 字段 key → 中文名：用于建议文案/UI 展示,与前端 labels 对齐；
FIELD_LABELS: dict[str, str] = {
    "contract_kind": "合同品类",
    "buyer": "甲方（采购方）",
    "supplier": "乙方（供应商）",
    "signature_date": "签署日期",
    "effective_date": "生效日期",
    "expiry_date": "到期日",
    "total_amount": "合同总额",
    "currency": "币种",
    "payment_schedule": "付款计划",
    "penalty_rate": "违约金日利率",
    "liability_cap": "责任上限",
    "warranty_months": "质保期",
    "termination_notice_days": "解约通知期",
    "ip_ownership": "知识产权归属",
    "confidentiality_months": "保密期",
    "governing_law": "适用法律",
}


# 必填核心字段：缺失会削弱整份审查的可信度
CORE_REQUIRED = ("buyer", "supplier", "effective_date", "expiry_date", "total_amount", "currency")


# 金额/日期缺失视为；主体信息缺失降为 medium
HIGH_IF_MISSING = ("total_amount", "effective_date", "expiry_date")


# 品类应含条款基线：某字段只在基线内才做"缺失 → medium"检查。
# 背景：政采校服类天然不写责任上限/保密/IP/适用法律；农副类有保密与适用法律但无责任
# 上限/IP——没有品类感知会把"品类正常的省略"误报成风险。
# None（历史数据/未分类）按 enterprise_goods 全量处理，向后兼容。
KIND_BASELINE: dict[str, set[str]] = {
    "enterprise_goods": {"liability_cap", "confidentiality_months", "ip_ownership", "governing_law"},
    "gov_goods": set(),
    "agri_goods": {"confidentiality_months", "governing_law"},
    "tech_service": {"liability_cap", "confidentiality_months", "ip_ownership", "governing_law"},
}


# IP 权属合规判断用关键词（合同以甲方/采购方视角表述）
_BUYER_KEYWORDS = ("甲方", "采购方")


# ---- 文本级条款基线检查（验收/发票/担保/转包）----

# 触发品类：企业/农副/技术全查；gov（政采/校服）按示范文本执行豁免——示范文本自带
# 验收/履约保函章节，且"正常省略"本就受 KIND_BASELINE 保护（易错点：无品类感知会把
# 校服/政采的正常省略误报成风险）
TEXT_RULE_KINDS: set[str] = {"enterprise_goods", "tech_service", "agri_goods"}


# 转包/分包基线只约束"定制/工程交付"形态（企业采购、技术开发/服务）；
# 农副产品买卖无转包概念，不套用
SUBCONTRACT_KINDS: set[str] = {"enterprise_goods", "tech_service"}


# P-08 履约担保金额门槛：合同总额 ≥ 100 万元才进入"大额需担保"检查（元）
PERFORMANCE_BOND_MIN_TOTAL = Decimal("1000000")


# ---- 跨模块共用的文本口径正则（多个规则要用同一口径，集中在这里防漂移）----

# OCR 页标记（parser 逐页拼接时插入）：定位比对、规则判定前都要去掉它
PAGE_MARK_RE = re.compile(r"-{2,}\s*第\s*\d+\s*页\s*-{2,}")

# 易错点：字符类不能排除 \n——OCR/PDF 文本每行硬换行，排除换行会让长句永远匹配不到；
# 改用"允许换行但不许跨句"的写法（(?!。) 逐字否定）
_NO_PERIOD = r"(?:(?!。)[\s\S])"

# "签字/盖章…生效"句式：生效规则明确、但正文未写具体签署日期。
# 窗口 40：真实扫描件常见长修饰（"…签字并分别加盖各自单位公章之日起生效"），
# 曾经两处口径不一致（一处 20 一处 40）导致"缺生效日"在扫描件上误留 high——
# 统一放到这里，规则判定（annotate）与生效日推断（fields）共用同一口径。
SIGNING_EFFECT_RE = re.compile(rf"(?:签字|盖章|签名){_NO_PERIOD}{{0,40}}生效")

# 生效日以"签订之日"为准的写法（服务/开发类合同常只写"自合同签订之日起"，不写具体日期）
EFFECTIVE_FROM_SIGN_RE = re.compile(
    r"签订之日起|合同签订之日|自.{0,12}(?:签署|签订|签字|盖章).{0,10}(?:之日|当日起)"
    rf"|(?:签署|签字|盖章){_NO_PERIOD}{{0,40}}生效|(?:签署|签字|盖章){_NO_PERIOD}{{0,40}}成立"
)
