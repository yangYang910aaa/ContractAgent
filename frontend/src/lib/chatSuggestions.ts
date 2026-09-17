/**
 * 对话助手空态的三个建议问题：按这份合同的实际结论生成。
 *
 * 写死文案会在没有高风险的合同上问"为什么判高风险"——三类问题（判定解释 / 怎么处置 /
 * 依据）都从结论与风险清单里取，闸口态传待审高风险（那一屏全是 high，不带 severity）。
 */

import { riskLabel } from '../labels'
import type { RiskCardItem } from '../types'

/** 没有风险清单时（抽取中、抽取失败）的兜底三问。 */
const GENERIC = [
  '这份合同审查了哪些方面？',
  '把付款条款的原文找出来',
  '政策库里对预付款是怎么规定的？',
]

/** 结论不同，问法不同：不能拿"为什么判高风险"去问一份有条件通过的合同。 */
const GRADE_QUESTION: Record<string, string> = {
  conditional_pass: '这份合同为什么是「有条件通过」？',
  pass: '这份合同为什么判通过？',
  fail: '这份合同为什么判不通过？',
}

export interface ChatSuggestionInput {
  grade?: string | null
  risks?: RiskCardItem[]
}

export function buildChatSuggestions(input: ChatSuggestionInput): string[] {
  const risks = orderBySeverity(input.risks ?? [])
  const top = risks[0]
  // 情况：一份风险都没有 → 通过合同问"为什么判通过"，其余（抽取中、抽取失败）给通用三问
  if (!top) {
    return input.grade === 'pass'
      ? ['这份合同为什么判通过？', '审查都看了哪些方面？', '把付款条款的原文找出来']
      : [...GENERIC]
  }

  // 闸口待审项不带 severity（那一屏都是高风险），按 high 处理
  const isHigh = (top.severity ?? 'high') === 'high'
  const name = riskLabel(top)
  // 政策口径优先问最该看的那条引用的政策；它没引用就往后面找一条有引用的
  const withPolicy = risks.find((risk) => risk.policy_ref)

  return [
    // ① 判定解释
    isHigh
      ? `${name}这条为什么算高风险？`
      : (GRADE_QUESTION[input.grade ?? ''] ?? '这份合同的结论是怎么得出来的？'),
    // ② 怎么处置
    `${name}要怎么补？`,
    // ③ 依据：有政策编号问口径，否则定位原文
    withPolicy?.policy_ref
      ? `${withPolicy.policy_ref} 是怎么规定的？`
      : top.clause_ref
        ? `把${top.clause_ref}的原文找出来`
        : '把付款条款的原文找出来',
  ]
}

/** 高风险排前面，同级保持报告里的原顺序（看的人先关心最严重的那条）。 */
function orderBySeverity(risks: RiskCardItem[]): RiskCardItem[] {
  return [...risks].sort((a, b) => severityRank(a) - severityRank(b))
}

function severityRank(risk: RiskCardItem): number {
  return (risk.severity ?? 'high') === 'high' ? 0 : 1
}
