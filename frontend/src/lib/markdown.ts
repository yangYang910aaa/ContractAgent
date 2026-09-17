/**
 * 极简 Markdown → 展示块：政策草稿与配套清单共用。
 *
 * 只认起稿输出文件里真实出现的三样——标题行、`- ` 列表项、普通段落；
 * 渲染成 Vue 模板里的元素而不是 innerHTML，所以不引 HTML 注入面。
 */

/** 一个展示块：h=标题（带层级）、li=列表项、p=段落。 */
export interface MdBlock {
  kind: 'h' | 'li' | 'p'
  level: number // 标题层级 1~6；非标题为 0
  text: string
}

/** Markdown 文本 → 展示块列表；空行跳过，加粗标记直接去掉（列表里常带强调）。 */
export function mdBlocks(text: string): MdBlock[] {
  const blocks: MdBlock[] = []
  for (const raw of (text || '').split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const clean = line.replace(/\*\*/g, '')
    const heading = /^(#{1,6})\s*(.+)$/.exec(clean)
    // 这种情况是：标题行 → 记层级，由组件按层级给字号
    if (heading) {
      blocks.push({ kind: 'h', level: heading[1].length, text: heading[2] })
      continue
    }
    const bullet = /^[-*]\s+(.+)$/.exec(clean)
    // 这种情况是：列表项 → 交给列表容器渲染
    if (bullet) {
      blocks.push({ kind: 'li', level: 0, text: bullet[1] })
      continue
    }
    blocks.push({ kind: 'p', level: 0, text: clean })
  }
  return blocks
}
