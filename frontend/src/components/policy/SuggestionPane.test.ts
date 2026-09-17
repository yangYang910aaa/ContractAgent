import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SuggestionPane from './SuggestionPane.vue'
import type { AiDraftSuggestions } from '../../types'

const SUGGESTIONS: AiDraftSuggestions = {
  risk_types: [
    {
      risk_type: 'prepayment_ratio_high',
      label: '预付款比例过高',
      why: '管的就是交付前付款比例',
      evidence_hint: '看付款计划里含预付的期次',
      known: true,
    },
    {
      risk_type: 'new',
      label: '担保缺失',
      why: '库里没有对应编码',
      evidence_hint: '看有没有担保条款',
      known: false,
    },
  ],
  samples: [{ goal: '验证超比例判 fail', kind: 'gov_goods', defect: '首付 60%', expected_grade: '' }],
  retrievals: [{ query: '预付款最多能给多少', policy_ref: 'P-16', expect: '预付款比例上限' }],
  notes: ['样本建议里的品类「supplies」不在四类里，先按企业货物品类处理'],
}

describe('SuggestionPane', () => {
  it('三组建议都渲染，编码用芯片、品类与评级走中文映射', () => {
    const wrapper = mount(SuggestionPane, { props: { suggestions: SUGGESTIONS } })
    const text = wrapper.text()
    expect(text).toContain('建议挂的风险类型')
    expect(text).toContain('建议造的验证样本')
    expect(text).toContain('检索标准答案建议')
    expect(text).toContain('prepayment_ratio_high')
    expect(text).toContain('政府采购 / 校服') // 品类机器码不直接展示
    expect(text).toContain('评级待定') // 取值被归位后留空
    expect(text).toContain('P-16')
  })

  it('编码不在册的单列红字提示，并展示被后端改过取值的说明', () => {
    const wrapper = mount(SuggestionPane, { props: { suggestions: SUGGESTIONS } })
    expect(wrapper.find('.err').text()).toContain('需人工新增')
    expect(wrapper.find('.warn').text()).toContain('不在四类里')
  })

  it('某一组没给内容时给一句说明，不留空块', () => {
    const wrapper = mount(SuggestionPane, {
      props: {
        suggestions: { risk_types: [], samples: [], retrievals: [], notes: [] },
      },
    })
    expect(wrapper.text()).toContain('模型没给风险类型建议')
    expect(wrapper.text()).toContain('模型没给样本建议')
    expect(wrapper.text()).toContain('模型没给检索标准答案建议')
    // notes 为空时不显示"取值被归位"那行
    expect(wrapper.find('.warn').exists()).toBe(false)
  })
})
