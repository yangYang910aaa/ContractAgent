import { describe, expect, it } from 'vitest'

import { mdBlocks } from './markdown'

describe('mdBlocks', () => {
  it('识别标题层级、列表项与段落', () => {
    const blocks = mdBlocks('# 标题\n\n## 二级\n\n- 第一条要点\n\n普通段落')
    expect(blocks).toEqual([
      { kind: 'h', level: 1, text: '标题' },
      { kind: 'h', level: 2, text: '二级' },
      { kind: 'li', level: 0, text: '第一条要点' },
      { kind: 'p', level: 0, text: '普通段落' },
    ])
  })

  it('去掉加粗标记、跳过空行（列表里常带**强调**）', () => {
    const blocks = mdBlocks('-  **待定**：阈值\n\n\n   缩进段落')
    expect(blocks.map((block) => block.text)).toEqual(['待定：阈值', '缩进段落'])
    // 星号列表与短横列表同等对待
    expect(mdBlocks('* 另一条')[0]).toEqual({ kind: 'li', level: 0, text: '另一条' })
  })

  it('空文本与纯空白不产出块（起稿产物可能为空）', () => {
    expect(mdBlocks('')).toEqual([])
    expect(mdBlocks('   \n\n  ')).toEqual([])
  })

  it('井号与文字之间没空格也当标题（起稿产物里两种写法都有）', () => {
    expect(mdBlocks('#标题')).toEqual([{ kind: 'h', level: 1, text: '标题' }])
    // 没有井号、也不是列表项的行一律当段落
    expect(mdBlocks('1. 编号列表不特殊处理')).toEqual([
      { kind: 'p', level: 0, text: '1. 编号列表不特殊处理' },
    ])
  })
})
