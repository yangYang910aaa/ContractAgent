/**
 * 风险/字段的中文展示名（与 backend/app/rules.py 的 RISK_LABELS / FIELD_LABELS
 * 对齐；改后端时同步这里）。risk_type 是机器码（评测/接口用），界面永远显示
 * 中文 label——后端已随报告带 label，本表只兜底旧内存任务与漏网情况。
 */

export const RISK_LABELS: Record<string, string> = {
  missing_required_field: '缺失必填字段',
  date_logic_effective_before_signature: '生效日早于签署日',
  date_logic_expiry_not_after_effective: '到期日不晚于生效日',
  amount_inconsistency: '付款金额不一致',
  prepayment_ratio_high: '预付款比例过高',
  warranty_too_short: '质保期不足',
  liability_cap_unclear: '责任上限未明确',
  liability_cap_too_low: '责任上限过低',
  confidentiality_missing: '缺少保密条款',
  confidentiality_too_long: '保密期过长',
  penalty_rate_too_high: '违约金比例畸高',
  ip_ownership_missing: '未约定知识产权归属',
  ip_ownership_unclear: '知识产权归属不清',
  governing_law_missing: '缺少适用法律约定',
  blank_template_suspected: '疑似空白模板',
}

/** ContractModel 字段 key → 中文名（建议文案里出现字段名时用中文）。 */
export const FIELD_LABELS: Record<string, string> = {
  contract_kind: '合同品类',
  buyer: '甲方（采购方）',
  supplier: '乙方（供应商）',
  signature_date: '签署日期',
  effective_date: '生效日期',
  expiry_date: '到期日',
  total_amount: '合同总额',
  currency: '币种',
  payment_schedule: '付款计划',
  penalty_rate: '违约金日利率',
  liability_cap: '责任上限',
  warranty_months: '质保期',
  termination_notice_days: '解约通知期',
  ip_ownership: '知识产权归属',
  confidentiality_months: '保密期',
  governing_law: '适用法律',
}

/** 合同品类机器码 → 中文（抽取字段里的 contract_kind 展示用）。 */
export const KIND_LABELS: Record<string, string> = {
  enterprise_goods: '企业货物采购',
  gov_goods: '政府采购 / 校服',
  agri_goods: '农副产品买卖',
  tech_service: '技术开发 / 软件 / 服务',
}

/** 品类展示：未知/null 回退原文（别把 agri_goods 这类机器码直接给用户）。 */
export function kindLabel(kind: string | null | undefined): string {
  return (kind && KIND_LABELS[kind]) || kind || '未识别'
}

/**
 * 政策条文重排：把源文件里"手工折行"的半句续行合并成逻辑行，返回每行一条。
 * 背景：政策细则源文档每行约 40~50 字就换行，直接按行渲染会出现"一页纸只有
 * 左边有内容、右半空白"（用户反馈，2026-09-05）。规则：结构行（细则标题/
 * 第X条/「标签：」前缀行）另起一行；普通续行接续到上一逻辑行，直到句末
 * （。；！？）才断——渲染时每行按整行宽度自然换行，右侧不再空。
 */
export function policyReflow(text: string): string[] {
  const logical: string[] = []
  for (const raw of text.split('\n')) {
    const line = raw.trim().replace(/^#{1,6}\s*/, '')
    if (!line) continue
    // 同行多段元信息（文件编号／版本／生效日期 等全角空格分隔）先拆成独立段
    const segments = line.split(/\s{2,}|\u3000{2,}/).map((s) => s.trim()).filter(Boolean)
    for (const seg of segments) {
      const isHead = /^(采购合同审核制度|细则|第[一二三四五六七八九十\d]+条|附则)/.test(seg)
      const isLabel = /^[^：:，。！？\n]{1,10}[：:]/.test(seg)
      const prev = logical[logical.length - 1]
      const prevEndsSentence = prev ? /[。；！？]$/.test(prev) : true
      // 上一行若是标题/条文头（第X条等），正文不能并进标题行
      const prevIsHead = prev ? /^(采购合同审核制度|细则|第[一二三四五六七八九十\d]+条|附则)/.test(prev) : true
      // 续行合并：非结构行、上一行不是标题、且上一行没到句末 → 接上去
      if (prev && !prevIsHead && !isHead && !isLabel && !prevEndsSentence) {
        logical[logical.length - 1] += seg
      } else {
        logical.push(seg)
      }
    }
  }
  return logical
}

/** 带机器码与可选 label 的风险形状（闸口摘要与报告风险通用）。 */
export interface LabeledRisk {
  risk_type?: string
  label?: string | null
}

/** 取风险中文展示名：后端 label 优先，本地映射兜底，最后回退机器码。 */
export function riskLabel(r: LabeledRisk): string {
  return r.label || RISK_LABELS[r.risk_type ?? ''] || r.risk_type || '风险'
}

/** 把建议文案里「英文field」替换成中文（兜底旧任务里已固化的文案）。 */
export function prettyField(text: string): string {
  let out = text
  for (const [k, v] of Object.entries(FIELD_LABELS)) {
    out = out.split(`「${k}」`).join(`「${v}」`)
  }
  return out
}
