import { describe, expect, it } from 'vitest'

import { buildChatSuggestions } from './chatSuggestions'

describe('buildChatSuggestions', () => {
  it('有条件通过的合同：问结论，不问"为什么判高风险"', () => {
    const chips = buildChatSuggestions({
      grade: 'conditional_pass',
      risks: [
        {
          risk_type: 'missing_required_field',
          label: '缺失必填字段「乙方（供应商）」',
          severity: 'medium',
          policy_ref: null,
        },
      ],
    })
    expect(chips[0]).toBe('这份合同为什么是「有条件通过」？')
    expect(chips.some((q) => q.includes('高风险'))).toBe(false)
    expect(chips[1]).toBe('缺失必填字段「乙方（供应商）」要怎么补？')
  })

  it('高风险排前面，三条分别问判定、处置与政策口径', () => {
    const chips = buildChatSuggestions({
      grade: 'fail',
      risks: [
        { risk_type: 'confidentiality_missing', label: '缺少保密条款', severity: 'medium' },
        {
          risk_type: 'prepayment_ratio_high',
          label: '预付款比例过高',
          severity: 'high',
          policy_ref: 'P-01',
        },
      ],
    })
    expect(chips).toEqual([
      '预付款比例过高这条为什么算高风险？',
      '预付款比例过高要怎么补？',
      'P-01 是怎么规定的？',
    ])
  })

  it('闸口待审项不带 severity 也按高风险问', () => {
    const chips = buildChatSuggestions({
      risks: [{ risk_type: 'penalty_rate_too_high', label: '违约金比例畸高', clause_ref: '第五条' }],
    })
    expect(chips[0]).toBe('违约金比例畸高这条为什么算高风险？')
    expect(chips[2]).toBe('把第五条的原文找出来')
  })

  it('没有风险清单：通过的合同问结论，其余（抽取中）给通用三问', () => {
    expect(buildChatSuggestions({ grade: 'pass', risks: [] })[0]).toBe('这份合同为什么判通过？')
    expect(buildChatSuggestions({})).toEqual([
      '这份合同审查了哪些方面？',
      '把付款条款的原文找出来',
      '政策库里对预付款是怎么规定的？',
    ])
  })
})
