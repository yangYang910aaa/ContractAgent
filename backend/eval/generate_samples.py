"""合成合同生成器（v2：docx/pdf「合同感」排版）。

生成中文「采购合同」样本到 data/contracts/（md / docx / pdf 三格式）:
- sample_01 / sample_02:无缺陷（正常放行）
- sample_03:违约金比例过高 + 责任上限过低 + 分项加总与总额不一致
- sample_04:预付款 60%（超 30% 政策）+ 缺失保密条款
- sample_05:质保期 6 个月(不足 12)+ 保密期 60 个月(超 36)
- sample_06:校服式 gov_goods 正常合同（章节式一、二、…+明细表，质保 2 年/违约金日 0.05%）
- sample_07:校服式缺陷合同（质保 6 个月 < 12 + 违约金日 1.5% 畸高）
- sample_08:技术开发式 tech_service 正常合同（第X条式，IP 归甲方/保密 24 个月/责任上限 100%）
- sample_09:技术开发式缺陷合同（保密期 60 个月超 36 + 成果 IP 归乙方）

写法:企业样本按「第X条」成块、校服样本按「一、二、…」章节成块
(parser 双模式都支持)、技术开发样本按科技部示范文本的第X条骨架成块，
内容由参数化 spec 渲染，同一种缺陷形态可复现。
md 正文与 docx/pdf 正文同源（都来自 render_contract）；docx/pdf 的排版
外壳（宋体/首行缩进/双方信息表/签署区/页码）在 format_render.py 实现。
全部为合成数据，不含任何真实公司/个人信息；docx/pdf 只是"格式真实"，
用于验证 PDF/Word 上传链路，内容仍是合成的（合规红线）。

用法：
    python -m backend.eval.generate_samples
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "contracts"


@dataclass
class SampleSpec:
    """一份合成合同的参数化规格。"""

    sample_id: str  # 样本编号（如 sample_01，评测对齐用）
    filename: str  # 输出文件名（含缺陷特征，便于人眼区分）
    contract_no: str  # 合同编号（渲染进正文）
    title: str  # 合同标题
    buyer: str  # 甲方（采购方）名称
    supplier: str  # 乙方（供应商）名称
    signature_date: str  # 签署日期（中文文本，如 2026年3月10日）
    effective_date: str  # 生效日期
    expiry_date: str  # 到期日
    currency: str = "人民币"  # 币种
    total_amount: str = "1,000,000"  # 合同总额（元，字符串保留千分位，测抽取鲁棒性）
    # 分项：[(名称, 金额, 比例)]，用于金额一致性核验
    payment_terms: list = field(default_factory=list)  # 付款期次（名称/金额/占总额百分比）
    # 首期（预付款）比例，百分比数值
    prepayment_percent: float = 20.0  # 预付款占总额比例（%）
    warranty_months: int = 24  # 质保期（月）
    # 违约金：每日比例（百分比数值），如 0.05 表示日 0.05%
    penalty_daily_percent: float = 0.05
    # 责任上限（占合同总额百分比），None 表示未单独约定上限
    liability_cap_percent: float | None = 100.0
    confidentiality_months: int = 24  # 保密期（月）
    confidentiality_clause: bool = True  # True=渲染保密条款；False=整节缺失（构造缺陷）
    termination_notice_days: int = 30  # 提前解约通知期（天）
    ip_ownership: str = "定制成果知识产权归甲方（采购方）所有"  # IP 权属表述
    governing_law: str = "中华人民共和国法律"  # 适用法律
    note: str = ""  # 缺陷说明（写进生成清单）


SPECS: list[SampleSpec] = [
    # sample_01：正常合同（对照基线：字段齐全、金额一致）
    SampleSpec(
        sample_id="sample_01",
        filename="sample_01_电子元件采购合同_正常.md",
        contract_no="HT-2026-0101",
        title="电子元件采购合同",
        buyer="星辰智造科技有限公司",
        supplier="华芯电子有限公司",
        signature_date="2026年3月10日",
        effective_date="2026年3月10日",
        expiry_date="2027年3月9日",
        payment_terms=[
            ("预付款", "200,000", 20),
            ("验收合格后支付", "800,000", 80),
        ],
        note="正常合同：字段齐全、金额一致、条款合规。",
    ),
    # sample_02：正常合同（质保恰为下限 12 个月，验证边界合规）
    SampleSpec(
        sample_id="sample_02",
        filename="sample_02_办公设备采购合同_正常.md",
        contract_no="HT-2026-0102",
        title="办公设备采购合同",
        buyer="晨光数据服务有限公司",
        supplier="联创办公设备有限公司",
        signature_date="2026年4月2日",
        effective_date="2026年4月2日",
        expiry_date="2026年10月1日",
        total_amount="560,000",
        payment_terms=[
            ("预付款", "112,000", 20),
            ("到货验收后 30 日内", "448,000", 80),
        ],
        prepayment_percent=20.0,
        warranty_months=12,
        penalty_daily_percent=0.05,
        liability_cap_percent=100.0,
        confidentiality_months=24,
        termination_notice_days=45,
        note="正常合同：质保恰为 12 个月（政策下限，合规）。",
    ),
    # sample_03：缺陷 违约金日1.5% / 责任上限5% / 分项加总≠总额
    SampleSpec(
        sample_id="sample_03",
        filename="sample_03_服务器采购合同_违约金超限_金额不一致.md",
        contract_no="HT-2026-0103",
        title="服务器及配套软件采购合同",
        buyer="星辰智造科技有限公司",
        supplier="云启信息技术有限公司",
        signature_date="2026年5月15日",
        effective_date="2026年5月20日",
        expiry_date="2027年5月19日",
        total_amount="1,000,000",
        payment_terms=[
            ("预付款", "200,000", 20),
            ("第二批（到货后）", "500,000", 50),
            ("第三批（验收后）", "400,000", 40),
        ],
        prepayment_percent=20.0,
        warranty_months=24,
        penalty_daily_percent=1.5,  # 缺陷①：违约金日 1.5%，畸高
        liability_cap_percent=5.0,  # 缺陷②：责任上限仅 5%，过低
        confidentiality_months=24,
        note="缺陷：违约金率过高 + 责任上限过低；且分项金额 20+50+40=110 万 ≠ 总额 100 万（金额不一致）。",
    ),
    # sample_04：缺陷 预付款60%（超30%上限）/ 全篇缺保密条款
    SampleSpec(
        sample_id="sample_04",
        filename="sample_04_原材料采购合同_预付款超限_缺保密条款.md",
        contract_no="HT-2026-0104",
        title="电子原材料采购合同",
        buyer="晨光数据服务有限公司",
        supplier="宏远材料科技有限公司",
        signature_date="2026年6月1日",
        effective_date="2026年6月1日",
        expiry_date="2026年12月31日",
        total_amount="800,000",
        payment_terms=[
            ("预付款", "480,000", 60),  # 缺陷①：预付款 60%
            ("验收合格后支付", "320,000", 40),
        ],
        prepayment_percent=60.0,  # 缺陷①：超过政策上限 30%
        warranty_months=12,
        confidentiality_clause=False,  # 缺陷②：缺失保密条款
        note="缺陷：预付款比例 60% 超政策上限 30%；全篇无保密条款。",
    ),
    # sample_05：缺陷 质保6个月（<12）/ 保密期60个月（>36）
    SampleSpec(
        sample_id="sample_05",
        filename="sample_05_软件采购合同_质保过短_保密期过长.md",
        contract_no="HT-2026-0105",
        title="企业管理软件采购合同",
        buyer="星辰智造科技有限公司",
        supplier="智联软件股份有限公司",
        signature_date="2026年7月8日",
        effective_date="2026年7月8日",
        expiry_date="2027年7月7日",
        total_amount="2,000,000",
        payment_terms=[
            ("预付款", "400,000", 20),
            ("上线验收后支付", "1,600,000", 80),
        ],
        prepayment_percent=20.0,
        warranty_months=6,  # 缺陷①：质保 6 个月 < 政策下限 12
        confidentiality_months=60,  # 缺陷②：保密期 60 个月 > 政策上限 36
        note="缺陷：质保期 6 个月不足 12 个月；保密期 60 个月超过 36 个月上限。",
    ),
]


# ---- 校服式（gov_goods）样本：章节式固定骨架 + 缺陷载荷参数化 ----


@dataclass
class UniformSampleSpec:
    """校服式（gov_goods）合成合同规格，骨架仿 55.广州市校服采购合同（2021 版）。

    与 SampleSpec 的关系：企业样本逐条字段驱动「第X条」模板；校服正文是
    章节式固定骨架（一、二、…章节 + 1、2、3 子条 + 明细表 + 附件清单），
    只有缺陷载荷字段参数化，保证同骨架可复现 normal / defect 两版对比。
    骨架内容与填充口径见 docs/合同模板观察笔记.md 第一、二节（本地）。
    """

    sample_id: str  # 样本编号（sample_06/07，评测对齐用）
    filename: str  # 输出文件名（含缺陷特征，便于人眼区分）
    contract_no: str  # 合同编号（渲染进正文）
    title: str  # 合同标题
    buyer: str  # 甲方（采购方）名称
    supplier: str  # 乙方（供应商）名称
    signature_date: str  # 签署日期（中文文本，如 2026年3月10日）
    expiry_date: str  # 合同到期日（中文文本）
    warranty_months: int  # 质保期（月）：24=正常写「2 年」；6=缺陷（不足政策下限 12）
    penalty_daily_percent: float  # 违约金每日比例（百分比数值，0.05=日 0.05%）
    note: str = ""  # 缺陷说明（写进生成清单）


# 校服明细表（合成数据）：数量×单价加总须等于全校总价 198,400（金额一致锚点）
_DETAIL_HEADERS = ("序号", "品名", "面料/规格", "单价（元）", "数量（套）", "金额（元）")
_DETAIL_ROWS = (
    # (序号, 品名, 面料/规格, 单价, 数量, 金额) —— 640×150+320×200+320×120=198,400
    ("1", "夏季运动服套装", "涤棉混纺（短袖上衣、长裤各一件）", "150", "640", "96,000"),
    ("2", "冬季外套", "涤纶面料、抓绒内胆", "200", "320", "64,000"),
    ("3", "冬季长裤", "涤棉卡其布", "120", "320", "38,400"),
)
# 附件清单（学年汇总表）：数量与单人总价须与正文一致
_ATTACH_HEADERS = ("学年", "在校学生数（人）", "夏季运动服（套）", "冬季外套（件）", "冬季长裤（条）", "单人学年总价（元）")
_ATTACH_ROWS = (
    ("2026-2027 学年", "320", "640", "320", "320", "620"),
)


UNIFORM_SPECS: list[UniformSampleSpec] = [
    # sample_06：校服式正常合同（gov_goods 基线不要求责任上限/保密/IP/适用法律）
    UniformSampleSpec(
        sample_id="sample_06",
        filename="sample_06_学生校服采购合同_正常.md",
        contract_no="HT-2026-XF-0118",
        title="广州市晨光实验中学学生校服采购合同",
        buyer="广州市晨光实验中学",
        supplier="广州星海校服服饰有限公司",
        signature_date="2026年3月10日",
        expiry_date="2027年9月30日",
        warranty_months=24,  # 质保写「2 年」= 24 个月，合规（正文按年写，考验抽取折算）
        penalty_daily_percent=0.05,  # 违约金日 0.05%（参考模板十一.4 的 0.5‰）
        note="校服式正常合同：质保 2 年 / 违约金日 0.05% / 清单总价一致，应零风险 pass。",
    ),
    # sample_07：校服式缺陷合同（两条"写出来的数字"，抽取稳、规则必命中）
    UniformSampleSpec(
        sample_id="sample_07",
        filename="sample_07_学生校服采购合同_质保过短_违约金畸高.md",
        contract_no="HT-2026-XF-0119",
        title="广州市晨光实验中学学生校服采购合同",
        buyer="广州市晨光实验中学",
        supplier="广州星海校服服饰有限公司",
        signature_date="2026年3月10日",
        expiry_date="2027年9月30日",
        warranty_months=6,  # 缺陷①：质保 6 个月 < 政策下限 12 个月（P-02）
        penalty_daily_percent=1.5,  # 缺陷②：违约金日 1.5% 畸高（行业惯例上限 1%）
        note="缺陷：质保 6 个月不足 12 个月；违约金日 1.5% 明显偏高，应 fail 命中 P-02/违约金。",
    ),
]


# ---- 技术开发式（tech_service）样本：科技部示范文本第X条骨架 + 缺陷载荷 ----


@dataclass
class TechServiceSampleSpec:
    """技术开发（委托）合同合成规格，骨架仿科技部监制示范文本（22 条压缩为 14 条）。

    与 SampleSpec/UniformSampleSpec 的关系：第三条样本形态——企业「第X条」字段
    模板、校服章节式固定骨架、本类=科技部示范文本的第X条固定骨架。tech_service
    的 KIND_BASELINE 要求全量应含条款（责任上限/保密/IP/适用法律），正常样本
    必须写全才零风险；缺陷载荷只参数化保密期与 IP 权属方向。
    """

    sample_id: str  # 样本编号（sample_08/09，评测对齐用）
    filename: str  # 输出文件名（含缺陷特征）
    contract_no: str  # 合同编号（渲染进正文）
    buyer: str  # 甲方（委托方）名称
    supplier: str  # 乙方（受托方/开发方）名称
    signature_date: str  # 签署/生效日期（中文文本，如 2026年4月15日）
    expiry_date: str  # 合同到期日（中文文本）
    title: str = "技术开发（委托）合同"  # 合同标题（科技部示范文本式）
    warranty_months: int = 12  # 免费维护期（月）：验收合格后起算，12=政策下限边界合规
    penalty_daily_percent: float = 0.05  # 逾期违约金每日比例（0.05=日 0.05%）
    confidentiality_months: int = 24  # 保密期（月）：24=正常；60=缺陷（超 36 上限）
    liability_cap_percent: float = 100.0  # 责任上限（占开发费总额 %）：100=合规
    ip_to_supplier: bool = False  # True=成果 IP 归乙方（构造缺陷）；False=归甲方（正常）
    note: str = ""  # 缺陷说明（写进生成清单）


TECH_SPECS: list[TechServiceSampleSpec] = [
    # sample_08：技术开发式正常合同（tech_service 基线要求的责任上限/保密/IP/法律写全）
    TechServiceSampleSpec(
        sample_id="sample_08",
        filename="sample_08_技术开发委托合同_正常.md",
        contract_no="HT-2026-TD-0118",
        buyer="星辰智造科技有限公司",
        supplier="云启信息技术有限公司",
        signature_date="2026年4月15日",
        expiry_date="2027年10月31日",
        warranty_months=12,  # 免费维护 12 个月（恰为下限，边界合规）
        penalty_daily_percent=0.05,
        confidentiality_months=24,  # 保密期 24 个月，正常
        liability_cap_percent=100.0,  # 责任上限 100%，正常
        ip_to_supplier=False,  # 定制成果 IP 归甲方
        note="技术开发式正常合同：IP 归甲方 / 保密 24 个月 / 责任上限 100%，应零风险 pass。",
    ),
    # sample_09：缺陷（保密期 60 个月超 36 + 成果 IP 归乙方），应 fail 命中 P-04/P-05
    TechServiceSampleSpec(
        sample_id="sample_09",
        filename="sample_09_技术开发委托合同_保密期过长_IP归乙方.md",
        contract_no="HT-2026-TD-0119",
        buyer="星辰智造科技有限公司",
        supplier="云启信息技术有限公司",
        signature_date="2026年4月15日",
        expiry_date="2027年10月31日",
        warranty_months=12,
        penalty_daily_percent=0.05,
        confidentiality_months=60,  # 缺陷①：保密期 60 个月 > 政策上限 36（P-04）
        liability_cap_percent=100.0,
        ip_to_supplier=True,  # 缺陷②：成果 IP 归乙方，权属不在甲方（P-05 medium）
        note="缺陷：保密期 60 个月超过 36 个月上限；定制成果知识产权归乙方，应 fail 命中 P-04/P-05。",
    ),
]


def _warranty_text(months: int) -> str:
    """质保月数 → 正文写法：整年按「N 年」写（贴真实合同），其余按「N 个月」。"""
    # 分支：12 的整数倍（且非 0）→ 按年写；其余（含 6 个月缺陷）按月写
    return f"{months // 12} 年" if months and months % 12 == 0 else f"{months} 个月"


def _cn_ordinal(i: int) -> str:
    """1 起序号 → 中文第X条（tech 样本条款号，支持到二十）。"""
    units = "一二三四五六七八九十"
    return units[i - 1] if i <= 10 else "十" + units[i - 11]


def render_tech_service_contract(spec: TechServiceSampleSpec) -> str:
    """渲染技术开发（委托）式正文（第一条~第十四条），返回 md 全文。

    骨架仿科技部示范文本（签约方/项目内容/计划/转委托/保密/风险/成果归属/验收/
    维护/费用与支付/违约/变更解除/争议/生效），关键字段只以正文句出现；
    defect 载荷 = 保密期月数 + 成果权属方向，其余字段两版一致便于对比。
    """
    # 权属表述：正常=定制成果归甲方；缺陷=归乙方（句中不含「甲方」，规则才能命中 unclear）
    if spec.ip_to_supplier:
        ip_lines = (
            "本项目研究开发成果及其知识产权归乙方（受托方）所有，双方另有书面约定的除外。",
        )
    else:
        ip_lines = (
            "乙方根据甲方需求定制开发的软件、源代码及相关文档的知识产权归甲方所有。",
            "乙方授权甲方使用的既有基础平台软件的知识产权归乙方，甲方享有在本项目范围内"
            "永久、不可撤销的使用许可；乙方保证甲方使用时不受第三方权利主张影响。",
        )
    parts: list[str] = [
        f"# {spec.title}",
        "",
        f"合同编号：{spec.contract_no}",
        "",
        f"甲方（委托方）：{spec.buyer}",
        f"乙方（受托方）：{spec.supplier}",
        "签约地点：深圳市南山区",
        f"签约时间：{spec.signature_date}",
        "",
        "乙方具备相应的技术开发能力，甲方委托乙方研究开发「企业数据资产管理平台」"
        "（以下简称本项目）。双方依据《中华人民共和国民法典》《中华人民共和国科学"
        "技术进步法》及有关规定，本着平等自愿、诚实信用的原则，经协商一致订立本合同。",
        "",
    ]
    # 条款 = (标题, 正文行列表)；子条用 1、2、3，与科技部示范文本写法一致
    clauses: list[tuple[str, list[str]]] = [
        (
            "项目名称与技术内容",
            [
                "1、项目名称：企业数据资产管理平台技术开发（委托）项目。",
                "2、开发范围：数据接入与清洗、主数据管理、报表分析与可视化、权限与审计"
                "四大功能模块，具体以双方确认的《技术需求说明书》为准。",
                "3、开发手段：采用 B/S 架构与主流开源技术栈实现，交付物包括可部署的软件"
                "系统、源代码与设计文档。",
            ],
        ),
        (
            "研究开发计划",
            [
                "1、乙方应于2026年7月31日前完成需求评审与概要设计。",
                "2、乙方应于2026年9月30日前完成开发与内部测试，并提交甲方试运行。",
                "3、试运行1个月无重大缺陷后，甲方组织整体验收，验收应于2026年11月15日前完成。",
            ],
        ),
        (
            "转委托",
            [
                "乙方未经甲方书面同意，不得将本项目关键开发工作转委托给第三方；"
                "经甲方同意的，乙方仍应对第三方的工作成果向甲方承担责任。",
            ],
        ),
        (
            "保密要求",
            [
                "1、双方对因履行本合同而知悉的对方技术资料、经营信息与本项目成果负有"
                "保密义务，未经对方书面同意不得向第三方披露。",
                f"2、保密期限自本合同终止之日起 {spec.confidentiality_months} 个月。",
            ],
        ),
        (
            "双方权利义务",
            [
                "1、甲方应及时提供开发所需的数据样本、业务口径与配合条件，并按期组织评审与验收。",
                "2、乙方应投入足够的开发力量按期交付，并对交付成果的质量与安全负责。",
            ],
        ),
        (
            "风险承担",
            [
                "因现有技术水平与客观条件限制而在开发中难以克服的技术风险，由双方按本合同"
                "约定分担；风险出现后，乙方应在10日内书面通知甲方并提交风险分析报告。",
            ],
        ),
        ("技术成果权益的归属和分享", list(ip_lines)),
        (
            "成果验收",
            [
                "1、甲方按《技术需求说明书》及验收标准组织验收，验收合格的，双方签署《验收报告》。",
                "2、验收发现缺陷的，乙方应在15日内修复并重新提请验收。",
            ],
        ),
        (
            "相关技术服务",
            [
                f"1、乙方自本项目验收合格之日起，免费提供 {_warranty_text(spec.warranty_months)}"
                "的技术支持与维护服务。",
                "2、免费维护期内，乙方对系统缺陷应在48小时内响应、7日内修复。",
            ],
        ),
        (
            "费用及支付方式",
            [
                "1、本项目技术开发费总额为人民币（大写）壹佰贰拾万元整（小写：1,200,000 元），"
                "币种为人民币，实行费用包干。",
                "2、分期支付：",
                "（一）合同签订生效后10日内支付360,000元，占总额30%；",
                "（二）中期评审通过后支付480,000元，占总额40%；",
                "（三）项目验收合格后支付360,000元，占总额30%。",
            ],
        ),
        (
            "违约责任",
            [
                f"1、乙方逾期交付或逾期完成计划节点的，每逾期一日按技术开发费总额的 "
                f"{spec.penalty_daily_percent:g}% 向甲方支付违约金。",
                "2、交付成果存在重大缺陷经两次整改仍无法通过验收的，甲方有权解除合同并"
                "要求乙方返还已收款项。",
                f"3、除违约金外，乙方对甲方承担的赔偿责任总额以技术开发费总额的 "
                f"{spec.liability_cap_percent:g}% 为上限。",
            ],
        ),
        (
            "合同的变更与解除",
            [
                "1、经双方协商一致，可以变更或解除本合同。",
                "2、任何一方需提前解除合同的，应提前30日书面通知对方，并结清已发生的费用。",
            ],
        ),
        (
            "争议解决与适用法律",
            [
                "1、双方因本合同发生争议的，应友好协商解决；协商不成的，任何一方均可向"
                "甲方所在地人民法院提起诉讼。",
                "2、本合同的订立、效力、解释与履行适用中华人民共和国法律。",
            ],
        ),
        (
            "其他约定与合同生效",
            [
                "1、本合同一式六份，甲方执三份、乙方执三份，具有同等法律效力。",
                f"2、本合同自{spec.signature_date}双方签字盖章之日起生效，有效期至"
                f"{spec.expiry_date}。",
                "3、本合同未尽事宜，由双方另行协商并签订补充协议。",
            ],
        ),
    ]
    for idx, (title, lines) in enumerate(clauses, start=1):
        parts.append(f"第{_cn_ordinal(idx)}条 {title}")
        parts.extend(lines)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _md_table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> list[str]:
    """markdown 明细表块（表头 + 分隔行 + 数据行）。

    约定：行首以 | 开头、连续成块，docx/pdf 渲染器据此识别为真表格，
    parser 对 md 原样读取不受影响。表内金额不进 rules 抽取锚点（见 D2 取舍）。
    """
    parts = ["| " + " | ".join(headers) + " |"]
    parts.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        parts.append("| " + " | ".join(row) + " |")
    return parts


def render_uniform_contract(spec: UniformSampleSpec) -> str:
    """渲染校服式章节正文（一、二、…十四 + 附件清单），返回 md 全文。

    输出结构：合同头/双方/鉴于段（parser 归入「前言」）→ 十四个章节
    （章节头顶格、子条 1、2、3；八/十一章嵌入质保与违约金缺陷载荷）。
    关键金额只以正文句出现（二 单人价、一-2 全校总价），表内金额仅作明细展示。
    """
    total_line = (
        "上表各品类金额合计即本合同校服采购总价款：人民币（大写）"
        f"{_cn_upper_amount('198,400')}（小写：198,400 元），币种为人民币。"
        "如实际发放学生人数发生增减，按实际数量结算、多退少补。"
    )
    parts: list[str] = [
        f"# {spec.title}",
        "",
        f"合同编号：{spec.contract_no}",
        "",
        f"甲方（采购方）：{spec.buyer}",
        f"乙方（供应商）：{spec.supplier}",
        "签约地点：广州市天河区",
        f"签约时间：{spec.signature_date}",
        "",
        "为明确双方权利义务，依据《中华人民共和国民法典》《中华人民共和国产品质量法》"
        "及广州市中小学生校服管理相关规定，甲乙双方本着平等自愿、诚实信用的原则，"
        "就学生校服采购事宜协商一致，订立本合同。",
        "",
    ]
    # 章节 = (章节头, 正文行列表)；行以「1、」子条或普通句呈现，紧贴模板写法
    chapters: list[tuple[str, list[str]]] = [
        (
            "一、校服材质、数量、单价等明细",
            ["1、校服品类、面料、单价与数量如下表所示："]
            + _md_table(_DETAIL_HEADERS, _DETAIL_ROWS)
            + ["2、" + total_line],
        ),
        (
            "二、单个学生校服的总价",
            [
                "单个学生每学年校服总价为人民币（大写）陆佰贰拾元整（小写：620 元），"
                "其中夏季运动服套装两套计 300 元、冬季外套 200 元、冬季长裤 120 元。",
            ],
        ),
        (
            "三、质量要求",
            [
                "1、校服质量与安全指标应符合 GB/T 31888《中小学生校服》及国家相关"
                "强制性标准，甲醛含量、pH 值、可分解致癌芳香胺染料等安全项目应符合要求。",
                "2、面料应耐穿耐洗，经多次洗涤不褪色、不变形、不起球。",
                "3、交付的校服应全部为合格品，产品合格率 100%。",
            ],
        ),
        (
            "四、校服的样式与封样",
            [
                "1、校服样式由甲方确定，乙方据此制作样衣并送甲方书面确认。",
                "2、经甲方确认的样衣由双方共同封存，作为生产加工与验收的依据。",
            ],
        ),
        (
            "五、校服的生产加工与送检",
            [
                "1、乙方应严格按照封样组织生产，未经甲方书面同意不得变更面料、工艺与规格。",
                "2、每批次产品出厂前，乙方应送具有资质的检验机构检测，并向甲方提供检测报告。",
            ],
        ),
        (
            "六、交货时间、地点及货物包装",
            [
                "1、乙方应于2026年8月10日前将全部校服运送至甲方指定地点（甲方校内指定地点）。",
                "2、货物应按班级与规格分类包装并附清单，包装应防潮、防污、防损，包装费用由乙方承担。",
            ],
        ),
        (
            "七、校服验收",
            [
                "1、甲方收到校服后，应在10个工作日内完成数量与外观查验。",
                "2、数量异议应在收货后3个工作日内以书面形式提出，质量异议应在收货后"
                "7个工作日内提出；逾期未提出的，视为该批校服验收合格，但隐蔽的质量问题除外。",
            ],
        ),
        (
            "八、售后服务与附加服务",
            [
                f"1、质保期：校服自验收合格之日起质保 {_warranty_text(spec.warranty_months)}。"
                "质保期内出现起球、褪色、开线、拉链损坏等质量问题的，"
                "乙方应在接到甲方通知后5日内免费维修或更换。",
                "2、甲方提出补货或增订需求后，乙方应在2日内回复并安排生产，不得无故拒绝。",
                "3、甲方可要求乙方按学生身材提供上门量身定做服务，相关约定以补充协议为准。",
            ],
        ),
        (
            "九、付款日期与方式",
            [
                "1、校服款项由甲方统一代收，乙方不直接向学生收取。",
                "2、甲方应在2026年9月30日前将本合同总价款一次性支付给乙方，付款币种为人民币。",
            ],
        ),
        (
            "十、履约保证金",
            [
                "乙方应在本合同签订后10日内，向甲方提供金额为合同总价款20%的银行保函"
                "作为履约担保，保函有效期至2027年3月31日。",
            ],
        ),
        (
            "十一、违约责任",
            [
                f"1、乙方逾期交付校服的，每逾期一日按本合同总价款的 "
                f"{spec.penalty_daily_percent:g}% 向甲方支付违约金。",
                "2、乙方交付的校服与封样在质量、规格上不符的，甲方有权拒收，并可要求乙方"
                "在5日内调换；逾期调换的，按前款约定标准支付违约金。",
                "3、违约金不足以弥补实际损失的，守约方有权另行主张赔偿。",
            ],
        ),
        (
            "十二、合同的解除",
            [
                "1、经双方协商一致，可以解除本合同。",
                "2、一方迟延履行主要义务，经催告后15日内仍未履行的，另一方有权书面通知解除合同。",
            ],
        ),
        (
            "十三、争议解决",
            [
                "本合同履行过程中发生争议的，双方应友好协商解决；协商不成的，"
                "任何一方均可向甲方所在地人民法院提起诉讼。",
            ],
        ),
        (
            "十四、其他事项与附则",
            [
                "1、本合同未尽事宜，由双方协商后签订补充协议，补充协议与本合同具有同等效力。",
                f"2、本合同自{spec.signature_date}双方签字盖章之日起生效，"
                f"有效期至{spec.expiry_date}。",
                "3、本合同一式五份，甲方执两份、乙方执两份、一份报送教育主管部门备案。",
            ],
        ),
    ]
    for title, lines in chapters:
        parts.append(title)
        parts.extend(lines)
        parts.append("")
    # 附件清单：行首非章节头，parser 归入末章正文；数量与总价以正文为准
    parts.append("附件：校服采购清单")
    parts.extend(_md_table(_ATTACH_HEADERS, _ATTACH_ROWS))
    parts.append("本清单所列数量与单人总价同正文约定一致，采购总价款以正文约定为准。")
    return "\n".join(parts).rstrip() + "\n"


def _money(amount_str: str) -> str:
    """金额字符串后补单位，正文统一为「xxx 元」格式。"""
    return f"{amount_str} 元"


# 中文大写金额用字与位（银行票据写法：壹佰万元整）
_CN_UPPER_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_CN_UPPER_UNITS = ("", "拾", "佰", "仟")


def _cn_upper_four(n: int) -> str:
    """0~9999 → 中文大写（不含位组名）；中间零按「零」连接（如 1001 → 壹仟零壹）。"""
    out = ""
    zero = False  # 是否刚跳过零位（决定下个非零位前要不要补「零」）
    for idx in (3, 2, 1):
        unit = 10**idx
        digit = n // unit % 10
        # 分支：该位为 0 → 记 zero 标记，等后面非零位补零
        if digit == 0:
            if out:
                zero = True
            continue
        # 分支：该位非 0 → 前面跳了零则先写「零」再写数字+位名
        if zero and out:
            out += "零"
        out += _CN_UPPER_DIGITS[digit] + _CN_UPPER_UNITS[idx]
        zero = False
    digit = n % 10
    if digit:
        out += _CN_UPPER_DIGITS[digit]
    return out


def _cn_upper_amount(amount: str) -> str:
    """阿拉伯金额串 → 人民币中文大写（如 1,000,000 → 壹佰万元整）。

    只支持整数元（样本总额均为整万元）；输入先剥离千分位/单位再转换。
    """
    n = int(re.sub(r"[^\d]", "", amount))
    if n == 0:
        return "零元整"
    # 按 万/亿 位组由高到低拼；组间零桥接（如 1,001,000 → 壹佰万零壹仟元整）
    groups: list[int] = []
    while n:
        groups.append(n % 10000)
        n //= 10000
    big_units = ("", "万", "亿")
    out = ""
    zero_bridge = False  # 高位组结尾有零、低位组又非空时需要补「零」
    for idx in range(len(groups) - 1, -1, -1):
        group = groups[idx]
        if group == 0:
            if out:
                zero_bridge = True
            continue
        if zero_bridge and out:
            out += "零"
        out += _cn_upper_four(group) + big_units[idx]
        zero_bridge = group % 10 == 0
    return out + "元整"


def _payment_lines(spec: SampleSpec) -> list[str]:
    """付款方式正文行：期次自带（一）（二）中文序号，正文段落不再加工程编号。"""
    lines = [f"双方约定按如下期次支付合同价款（币种：{spec.currency}）："]
    cn_ordinals = ["一", "二", "三", "四", "五"]
    for idx, (name, amount, _) in enumerate(spec.payment_terms):
        lines.append(f"（{cn_ordinals[idx]}）{name}：{_money(amount)}；")
    lines.append(f"合同总价款为 {_money(spec.total_amount)}。")
    return lines


def _penalty_lines(spec: SampleSpec) -> list[str]:
    """生成违约责任正文（违约金率 + 责任上限两句）。

    分支：配置了责任上限 → 渲染上限句；未配置 → 写「按法律规定承担」兜底句，
    避免正文出现空条款。
    """
    lines = [
        f"乙方逾期交付的，每逾期一日按合同总价款的 "
        f"{spec.penalty_daily_percent:g}% 向甲方支付违约金。"
    ]
    # 分支 1：有责任上限配置 → 正文写明上限比例
    if spec.liability_cap_percent is not None:
        lines.append(
            f"除违约金外，乙方对甲方承担的赔偿责任总额以合同总价款的 "
            f"{spec.liability_cap_percent:g}% 为上限。"
        )
    # 分支 2：未配置上限 → 写法定兜底句（正文不出现空条款）
    else:
        lines.append("双方按法律规定承担违约责任。")
    return lines


def _confidentiality_lines(spec: SampleSpec) -> list[str]:
    """生成保密条款正文；返回空列表表示该节不渲染（缺保密条款缺陷）。"""
    # 分支：confidentiality_clause=False（如 sample_04）→ 返回空，整节不渲染
    if not spec.confidentiality_clause:
        return []
    return (
        "双方对因履行本合同而知悉的对方商业秘密负有保密义务。",
        f"保密期限自本合同终止之日起 {spec.confidentiality_months} 个月。",
    )


def render_contract(spec: SampleSpec) -> str:
    """按条款动态编号渲染一份合同正文（缺保密条款时整节不渲染、条款顺延）。"""
    cn_numbers = [
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
        "十一", "十二", "十三", "十四", "十五",
    ]
    # (条款名, 正文行列表) —— 顺序即合同条款顺序
    sections: list[tuple[str, list[str]]] = [
        (
            "合同标的与总价款",
            [
                # 大小写并用贴近真实合同；小写金额保留千分位作抽取锚点
                f"乙方向甲方供应本合同项下货物/服务。合同总价款为人民币（大写）"
                f"{_cn_upper_amount(spec.total_amount)}"
                f"（小写：{_money(spec.total_amount)}；币种：{spec.currency}）。"
            ],
        ),
        ("付款方式", _payment_lines(spec)),
        (
            "交付与验收",
            [
                "乙方应于本合同生效后 45 日内完成交付。",
                "甲方应在收到货物后 10 个工作日内组织验收，验收合格标准以双方确认的技术规范为准。",
            ],
        ),
        (
            "质量保证",
            [
                f"乙方对所供货物提供自验收合格之日起 {spec.warranty_months} 个月的质保期。",
                "质保期内出现质量问题，乙方应在 7 日内免费维修或更换。",
            ],
        ),
        ("违约责任", _penalty_lines(spec)),
    ]
    # 分支：需要保密条款才把该节加入，否则条款顺延（贴近真实"缺失"合同）
    if spec.confidentiality_clause:
        sections.append(("保密条款", _confidentiality_lines(spec)))
    sections.extend(
        [
            (
                "知识产权",
                [
                    f"{spec.ip_ownership}。",
                    "乙方保证交付物不侵犯任何第三方知识产权，因此产生的索赔由乙方承担。",
                ],
            ),
            (
                "合同期限与终止",
                [
                    f"本合同自 {spec.signature_date} 签署，自 {spec.effective_date} 生效，"
                    f"有效期至 {spec.expiry_date}。",
                    f"任何一方提前终止本合同的，应提前 "
                    f"{spec.termination_notice_days} 日书面通知对方。",
                ],
            ),
            (
                "争议解决与适用法律",
                [
                    f"本合同适用{spec.governing_law}。",
                    "因本合同产生的争议，双方应友好协商；协商不成的，"
                    "提交甲方所在地人民法院诉讼解决。",
                ],
            ),
            (
                "其他",
                ["本合同一式两份，双方各执一份，自双方盖章之日起生效。"],
            ),
        ]
    )

    parts: list[str] = [
        f"# {spec.title}",
        "",
        f"合同编号：{spec.contract_no}",
        "",
        "甲方（采购方）：" + spec.buyer,
        "乙方（供应商）：" + spec.supplier,
        "",
    ]
    for idx, (title, lines) in enumerate(sections):
        parts.append(f"第{cn_numbers[idx]}条 {title}")
        # 正文行直接落段：不再加「1.1/2.1」工程编号（贴近中文合同写法）
        for line in lines:
            parts.append(line)
        parts.append("")
    return "\n".join(parts)


# docx/pdf 排版渲染在 format_render.py（v2）；此处仅 re-export，保持调用方/测试兼容
from backend.eval.format_render import _cjk_font_path, render_docx, render_pdf  # noqa: F401


ALL_SPECS: list = [*SPECS, *UNIFORM_SPECS, *TECH_SPECS]  # 企业(01~05) + 校服(06/07) + 技术开发(08/09)


def _body_for(spec) -> str:
    """按 spec 类型返回正文 md：企业/技术开发「第X条」式 vs 校服章节式（docx/pdf 同源共用）。"""
    # 分支 1：技术开发式 spec → 科技部示范文本骨架（第X条式正文）
    if isinstance(spec, TechServiceSampleSpec):
        return render_tech_service_contract(spec)
    # 分支 2：校服式 spec → 章节式正文（一、二、… 章节头）
    if isinstance(spec, UniformSampleSpec):
        return render_uniform_contract(spec)
    # 分支 3：企业式 spec → 原有「第X条」正文
    return render_contract(spec)


def main() -> None:
    """把全部 spec（企业 01~05 + 校服 06/07 + 技术开发 08/09）渲染成 md/docx/pdf 落盘。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []
    for spec in ALL_SPECS:
        stem = Path(spec.filename).stem
        # 三格式同源输出：正文来自同一份 _body_for(spec)，保证内容一致
        body = _body_for(spec)
        (OUTPUT_DIR / spec.filename).write_text(body, encoding="utf-8")
        render_docx(spec, OUTPUT_DIR / "docx" / f"{stem}.docx", body=body)
        render_pdf(spec, OUTPUT_DIR / "pdf" / f"{stem}.pdf", body=body)
        manifest.append(f"{spec.sample_id}\t{spec.filename}\t{spec.note}")
    print(f"已生成 {len(ALL_SPECS)} 份合成合同（md/docx/pdf）-> {OUTPUT_DIR}")
    for line in manifest:
        print(" -", line)


if __name__ == "__main__":
    main()
