import { describe, expect, it } from 'vitest'

import {
  citationCheckClass,
  citationCheckText,
  gradeDisplay,
  kindLabel,
  missingMetaText,
  outcomeChips,
  riskLabel,
} from './labels'

describe('展示文案映射', () => {
  it('品类：已知编码给中文，未知或空回退', () => {
    expect(kindLabel('enterprise_goods')).toBe('企业货物采购')
    expect(kindLabel('gov_goods')).toBe('政府采购 / 校服')
    // 未知编码回退原文，避免把机器码当中文显示；空值给"未识别"
    expect(kindLabel('something_else')).toBe('something_else')
    expect(kindLabel(null)).toBe('未识别')
  })

  it('评级：疑似空白模板的"有条件通过"显示成"待确认"', () => {
    expect(gradeDisplay('pass')).toBe('通过')
    expect(gradeDisplay('conditional_pass')).toBe('有条件通过')
    expect(gradeDisplay('conditional_pass', true)).toBe('待确认')
    // 没评级的错误报告给破折号，别显示成"通过"
    expect(gradeDisplay(null)).toBe('—')
  })

  it('风险名：优先用后端给的 label，其次查表，最后回退机器码', () => {
    expect(riskLabel({ risk_type: 'prepayment_ratio_high', label: '预付款比例过高' })).toBe(
      '预付款比例过高',
    )
    expect(riskLabel({ risk_type: 'prepayment_ratio_high' })).toBe('预付款比例过高')
    expect(riskLabel({ risk_type: 'brand_new_type' })).toBe('brand_new_type')
    expect(riskLabel({})).toBe('风险')
  })

  it('起稿缺项：已知元信息键给中文，未知键原样保留', () => {
    expect(missingMetaText(['ref', 'effective_date'])).toBe('文件编号、生效日期')
    expect(missingMetaText(['unknown_key'])).toBe('unknown_key')
    expect(missingMetaText(undefined)).toBe('')
  })
})

describe('引用核对结论', () => {
  it('没有引用时说清楚"没有引用"，而不是给"通过"的错觉', () => {
    expect(citationCheckText(null)).toBe('本次报告没有政策引用')
    expect(citationCheckText({ cited: 0, grounded: 0, noted: 0 })).toBe('本次报告没有政策引用')
    expect(citationCheckClass({ cited: 0, grounded: 0, noted: 0 })).toBe('ck-mute')
  })

  it('全部对得上：报条数并给通过色调', () => {
    expect(citationCheckText({ cited: 3, grounded: 3, noted: 0 })).toContain('3 条政策引用逐条核对通过')
    expect(citationCheckClass({ cited: 3, grounded: 3, noted: 0 })).toBe('ck-ok')
  })

  it('有对不上的：点明几条需人工核对并给告警色调', () => {
    expect(citationCheckText({ cited: 5, grounded: 4, noted: 0 })).toBe(
      '5 条政策引用中 1 条需人工核对',
    )
    expect(citationCheckClass({ cited: 5, grounded: 4, noted: 0 })).toBe('ck-warn')
  })
})

describe('复核统计 chips', () => {
  it('只保留非零项，顺序固定（并入 / 升级 / 一致 / 仅记录）', () => {
    expect(outcomeChips({ added: 1, upgraded: 0, agreed: 2, noted: 0 })).toEqual([
      { key: 'added', label: '复核新增', count: 1 },
      { key: 'agreed', label: '与主审一致', count: 2 },
    ])
  })

  it('没有统计或全为零时给空数组（单审报告没有复核段）', () => {
    expect(outcomeChips(null)).toEqual([])
    expect(outcomeChips({ added: 0, upgraded: 0, agreed: 0, noted: 0 })).toEqual([])
  })
})
