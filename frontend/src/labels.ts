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
