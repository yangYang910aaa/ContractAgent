<!--
  原文抽屉：任务页「查看原合同」的侧滑面板。
  视图按文件类型给：pdf 提供「原文件」（浏览器内嵌预览，inline 而非下载）、
  「条文视图」（按条款整理、Markdown 表格转文本、证据定位锚点）与「纯文本」
  （模型解析出的原始全文快照，含 Markdown 标记）；docx/md/txt 提供
  「条文视图 / 纯文本」；docx 另有「原文件」页签——浏览器不原生支持 Word，
  用 docx-preview 把原文件渲染成近似 Word 的网页（下载仍弹确认框）。
  条文视图会按任务风险（props.risks）把命中条款整块标色 + 证据句高亮。
  审查中（processing）打开时 text 为空：内部轮询直到原文出现再展示。
-->
<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { renderAsync } from 'docx-preview'
import { getSource, taskFileUrl } from '../api'
import { riskLabel } from '../labels'
import type { SourceAnchor, SourceBlock, SourceDoc } from '../types'

/** 参与原文高亮的风险摘要（来自详情页：闸口 high_risks 或报告 risks）。 */
interface DrawerRisk {
  risk_type?: string
  label?: string | null
  severity?: string | null
  clause_ref?: string
  evidence?: string
  // 原文摘录（后端定位环节补）：说明句在正文里搜不到，定位与句内高亮优先用它
  evidence_quote?: string
}

/** 一个条款块的命中信息：命中的风险 + 可高亮的证据摘录。 */
interface BlockHit {
  risks: DrawerRisk[]
  markers: { text: string; sev: string }[]
}

const props = defineProps<{
  threadId: string
  anchor: SourceAnchor | null
  risks: DrawerRisk[]
}>()

const emit = defineEmits<{ close: [] }>()

/** 面板宽度（px）：0 = 用 CSS 默认宽度。用户拖过之后记住，下次打开还是这个宽度
 *  （右栏默认 60vw，不缩窄会把左边的报告内容遮住）。 */
const panelWidth = ref(Number(localStorage.getItem('src-panel-width')) || 0)
const resizing = ref(false)
// 最小宽度：再窄就看不清条文了（拖过头没意义）
const PANEL_MIN_W = 520

/** 拖左缘改宽度：往左拖变宽、往右拖变窄，夹在 [520, 视口宽-48] 之间。 */
function startResize(e: MouseEvent) {
  e.preventDefault()
  const panel = (e.currentTarget as HTMLElement).closest('.src-panel') as HTMLElement | null
  const startX = e.clientX
  // 没有显式宽度时以实际渲染宽度为起点（dock 模式是 min(860, 60vw)，不能按 0 算）
  const startW = panelWidth.value || panel?.getBoundingClientRect().width || window.innerWidth * 0.6
  const maxW = Math.max(PANEL_MIN_W, window.innerWidth - 48)
  resizing.value = true
  const onMove = (ev: MouseEvent) => {
    panelWidth.value = Math.min(Math.max(startW - (ev.clientX - startX), PANEL_MIN_W), maxW)
  }
  // 收尾：解绑监听 + 记住宽度（存 localStorage，关掉抽屉再开还是这个宽度）
  const onUp = () => {
    resizing.value = false
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    localStorage.setItem('src-panel-width', String(Math.round(panelWidth.value)))
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

type TabId = 'file' | 'blocks' | 'text'

// doc=原文数据；tab=当前视图；askDownload=下载确认弹窗开关
const doc = ref<SourceDoc | null>(null)
const error = ref('')
const loading = ref(true)
const tab = ref<TabId>('blocks')
const askDownload = ref(false)
const docxBox = ref<HTMLElement | null>(null) // docx 原文件渲染挂载点
const docxBusy = ref(false) // docx 渲染中（loading 文案）
const docxError = ref('') // docx 拉取/渲染失败原因
const docxClamped = ref(0) // 因源文件缩进异常而被夹回的段落数（>0 时给用户一句说明）
let timer: number | undefined

// 缩进容错阈值（pt）：超过 2 英寸的缩进在 A4 页面里没有正常用途，判为源文件坏样式。
// 真实素材里出现过缩进达 22 英寸的 docx，原样渲染会把行首字符顶出可视区，看着像"掉字"。
const INDENT_MAX_PT = 144

/** 把渲染结果里离谱的缩进夹回 0，返回被修正的属性个数（0=无需修正）。 */
function clampAbsurdIndents(root: HTMLElement): number {
  let fixed = 0
  const nodes = root.querySelectorAll<HTMLElement>('p, div, li, td, th')
  for (const el of Array.from(nodes)) {
    for (const prop of ['text-indent', 'margin-left', 'padding-left'] as const) {
      const raw = el.style.getPropertyValue(prop).trim()
      if (!raw) continue
      // 只处理带单位的长度值；auto / % 等交给浏览器自己算
      const m = /^(-?[\d.]+)(pt|px|in|cm)$/.exec(raw)
      if (!m) continue
      const value = Number(m[1])
      // 统一折算成 pt 再比较，避免单位不同导致漏判
      const pt =
        m[2] === 'pt' ? value : m[2] === 'px' ? value * 0.75 : m[2] === 'in' ? value * 72 : value * 28.35
      if (pt > INDENT_MAX_PT) {
        el.style.setProperty(prop, '0')
        fixed += 1
      }
    }
  }
  return fixed
}

const isPdf = computed(() => doc.value?.suffix === '.pdf')
const isDocx = computed(() => doc.value?.suffix === '.docx')
const isMd = computed(() => doc.value?.suffix === '.md' || doc.value?.suffix === '.txt')
// 没解析出原文（任务还在审查中 / 解析失败）时给空态提示
const hasText = computed(() => Boolean(doc.value?.text))

/** 每个条款块命中了哪些风险：按 clause_ref 精确/包含匹配，空 ref 按证据句兜底。 */
const blockHits = computed<BlockHit[]>(() => {
  const d = doc.value
  if (!d) return []
  const risks = props.risks ?? []
  return d.blocks.map((b) => {
    const hits = risks.filter((r) => {
      const clause = (r.clause_ref ?? '').trim()
      // 优先用原文摘录匹配（说明句在正文里搜不到，会让命中块与句内高亮都失效）
      const ev = (r.evidence_quote || r.evidence || '').trim()
      if (clause) {
        if (b.ref === clause) return true
        if (b.title.includes(clause) || clause.includes(b.title)) return true
      }
      if (ev && b.text.includes(ev)) return true
      // 容忍 PDF 抽取的空格/换行差异：折叠空白再比一次
      return Boolean(ev && b.text.replace(/\s+/g, '').includes(ev.replace(/\s+/g, '')))
    })
    // 证据句去重、超长截断（只标引用核心片段）；gate 载荷无 severity → 按 high 处理
    const markers: { text: string; sev: string }[] = []
    for (const r of hits) {
      // 句内高亮同样优先用原文摘录（说明句在正文里定位不到，会白标）
      const ev = (r.evidence_quote || r.evidence || '').trim()
      const sev = r.severity === 'medium' ? 'medium' : 'high'
      if (ev.length >= 8) {
        const text = ev.length > 200 ? ev.slice(0, 200) : ev
        if (!markers.some((m) => m.text === text)) markers.push({ text, sev })
      }
    }
    return { risks: hits, markers }
  })
})

/** 顶部标签：pdf 且文件在盘 → 原文件(内嵌预览)/条文/纯文本；其余 条文/纯文本。 */
const tabs = computed<{ id: TabId; label: string }[]>(() => {
  const d = doc.value
  if (!d) return []
  // pdf 原生预览；docx 由 docx-preview 渲染——两者都叫「原文件」
  const fileViewable = d.file_available && (d.suffix === '.pdf' || d.suffix === '.docx')
  if (fileViewable) {
    return [
      { id: 'file', label: '原文件' },
      { id: 'blocks', label: '条文视图' },
      { id: 'text', label: '纯文本' },
    ]
  }
  return [
    { id: 'blocks', label: '条文视图' },
    { id: 'text', label: '纯文本' },
  ]
})

/** Markdown 原文 → 可读文本：剥标题 #、表格行转普通文本（md 样本的条文更好读）。 */
function mdClean(text: string): string {
  return text
    .split('\n')
    .map((line) => {
      const t = line.trim()
      // 表头分隔行（|---|）直接去掉
      if (/^\|[\s\-:|]+\|$/.test(t)) return ''
      // 标题 # → 去井号；表格行 → 去管道，单元格用空格拼接
      if (/^#{1,6}\s/.test(t)) return t.replace(/^#{1,6}\s*/, '')
      if (t.startsWith('|') && t.endsWith('|')) {
        return t
          .slice(1, -1)
          .split('|')
          .map((c) => c.trim())
          .filter(Boolean)
          .join('   ')
      }
      return line
    })
    .filter(Boolean)
    .join('\n')
}

/** 条款块正文按来源加工：md/txt 走 Markdown 清洗，docx/pdf 文本直接用。 */
function blockText(b: SourceBlock): string {
  let text = isMd.value ? mdClean(b.text) : b.text
  // 块正文首行常与标题重复（split_clauses 的 text 含条款头），剥掉一次
  if (b.title && text.startsWith(b.title)) {
    text = text.slice(b.title.length).replace(/^\s*\n+/, '')
  }
  return text
}

/** HTML 转义（正文要进 v-html 做高亮，必须先转义防注入）。 */
function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** 给已转义的 HTML 片段包证据 <mark>（norm=true 时证据与正文都去空白再比，
 *  用于 PDF——pypdf 提取常把同一句拆断/加空格，命中率会更高）。 */
function applyMarks(html: string, index: number, norm: boolean): string {
  const markers = blockHits.value[index]?.markers ?? []
  for (const m of [...markers].sort((x, y) => y.text.length - x.text.length)) {
    const raw = norm ? m.text.replace(/\s+/g, '') : m.text
    const esc = escHtml(raw)
    html = html.split(esc).join(`<mark class="mk-${m.sev === 'medium' ? 'med' : 'high'}">${esc}</mark>`)
  }
  return html
}

/** docx 表格行（"a | b | c"）→ 单元格数组；非表格行返回 null。 */
function tableCells(line: string): string[] | null {
  if (!isDocx.value || !line.includes('|')) return null
  const cells = line.split('|').map((c) => c.trim())
  return cells.length >= 3 ? cells : null
}

/** PDF 行清理：去页码行、去掉行内多余空格（pypdf 常在汉字/数字间留空格）。 */
function pdfCleanLine(line: string): string {
  const t = line.trim()
  if (/^第\s*[0-9]+\s*页$/.test(t)) return ''
  return t.replace(/[\u3000 ]/g, '')
}

/** PDF 短碎片行：表格被 pypdf 逐格抽成"每格一行"时，每行都短且无句末标点。 */
function isPdfShard(line: string): boolean {
  const t = pdfCleanLine(line)
  if (!t || t.length > 14) return false
  return !/[。！？；]$/.test(t)
}

const _PDF_SEG_START = /^[0-9]+、|^（[一二三四五六七八九十0-9]+）|^第[0-9一二三四五六七八九十百千]+条/
// PDF 前言里的"标签：值"行（合同编号/甲方/乙方等），应独立成行而非并进段落
const _PDF_HEADER = /^[^，。！？；：\n]{1,14}[：:]/

/** docx/pdf 条文块的结构化排版 HTML：
 *  docx：表格行（| 分隔）重组为真表格，其余按行成段；
 *  pdf：页码行剔除、折行按句拼接、表格碎片收敛成带分隔的近似行。
 *  目标是把 pypdf/python-docx 的"机器文本"恢复成可读的条款排版。 */
function layoutHtml(index: number, b: SourceBlock): string {
  let body = b.text
  // 剥掉块正文首行与标题的重复（docx/pdf 首行常带前导空格）
  const head = b.title.trim()
  if (head && body.trimStart().startsWith(head)) {
    body = body.trimStart().slice(head.length)
  }
  const lines = body.split(/\r?\n/)
  const out: string[] = []

  // 分支 1：docx —— 段落行 + | 表格行重组
  if (isDocx.value) {
    let i = 0
    while (i < lines.length) {
      const line = lines[i].trim()
      if (!line) { i++; continue }
      const cells = tableCells(line)
      // 这种情况是：连续表格行 → 收集成一个 <table>（首行当表头）
      if (cells) {
        const rows: string[][] = [cells]
        i++
        while (i < lines.length) {
          const c = tableCells(lines[i].trim())
          if (!c) break
          rows.push(c)
          i++
        }
        const headHtml = rows[0].map((c) => `<th>${applyMarks(escHtml(c), index, false)}</th>`).join('')
        const bodyHtml = rows
          .slice(1)
          .map((r) => `<tr>${r.map((c) => `<td>${applyMarks(escHtml(c), index, false)}</td>`).join('')}</tr>`)
          .join('')
        out.push(`<div class="mini-tbl"><table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`)
        continue
      }
      out.push(`<p class="pl">${applyMarks(escHtml(line), index, false)}</p>`)
      i++
    }
    return out.join('')
  }

  // 分支 2：pdf —— 折行拼接 + 表格碎片收敛
  let i = 0
  let buf = ''
  const flush = () => {
    if (buf) {
      out.push(`<p class="pl">${applyMarks(escHtml(buf), index, true)}</p>`)
      buf = ''
    }
  }
  const pushNew = (s: string) => {
    flush()
    buf = s
  }
  while (i < lines.length) {
    const raw = lines[i]
    const clean = pdfCleanLine(raw)
    // 这种情况是：OCR 页标记行（"--- 第 3 页 ---"）→ 不进正文、也**不打断段落**。
    // 页标记常把一句话从中间切开（"…各自单｜--- 第 3 页 ---｜位公章…"），落进正文会读成
    // "页码混进条款"；跳过后两截自然拼回一句。纯文本页签仍保留原样。
    if (PAGE_MARK_AT.test(clean)) { i++; continue }
    // 表格碎片簇：连续 ≥3 个短行且都不是段落/编号头 → 合并成一行近似文本
    if (clean && isPdfShard(raw)) {
      let run = 1
      while (i + run < lines.length && isPdfShard(lines[i + run])) run++
      if (run >= 3) {
        const joined: string[] = []
        for (let k = 0; k < run; k++) {
          const c = pdfCleanLine(lines[i + k])
          if (c) joined.push(c)
        }
        flush()
        out.push(
          `<p class="tbl-flat">${applyMarks(escHtml(joined.join(' · ')), index, true)}</p>`,
        )
        i += run
        continue
      }
    }
    if (!clean) { i++; continue }
    // 段落/编号头与"标签：值"行：另起一段
    if (_PDF_SEG_START.test(clean) || _PDF_HEADER.test(clean)) {
      pushNew(clean)
      i++
      continue
    }
    if (!buf) {
      buf = clean
    } else {
      // 跨行拼接：中文字符间不加空格（pypdf 折行打断的句子直接接回）
      const prev = buf[buf.length - 1]
      const next = clean[0]
      const bothWord = /[\u4e00-\u9fff0-9A-Za-z]/.test(prev) && /[\u4e00-\u9fff0-9A-Za-z]/.test(next)
      buf += bothWord ? clean : ` ${clean}`
    }
    // 一句话结束（且下一行不是"，…"续句）→ 落一段
    const nx = i + 1 < lines.length ? pdfCleanLine(lines[i + 1]) : ''
    if (/[。！？；]$/.test(buf) && !/^[，；、]/.test(nx)) flush()
    i++
  }
  flush()
  return out.join('')
}

/** 某块的正文 HTML：md/txt 走原 mdClean 路径；docx/pdf 走结构化排版。 */
function blockHtml(index: number): string {
  const b = doc.value?.blocks[index]
  if (!b) return ''
  if (isMd.value) {
    const html = escHtml(blockText(b)).replace(/\r?\n/g, '<br>')
    return applyMarks(html, index, false)
  }
  return layoutHtml(index, b)
}

/** 任一条款块命中风险的标记（空态不显示提示条）。 */
const anyHit = computed(() => blockHits.value.some((h) => h.risks.length > 0))

/** 按风险项 clause_ref 找条款块下标：先精确比 ref，再按标题包含兜底。 */
function findBlock(clause: string, blocks: SourceBlock[]): number {
  const exact = blocks.findIndex((b) => b.ref === clause)
  if (exact >= 0) return exact
  const c = clause.trim()
  const byTitle = blocks.findIndex((b) => b.title.includes(c) || c.includes(b.title))
  if (byTitle >= 0) return byTitle
  // 条款号是子条（如 "5.2"）时标题里没有它，但块正文里通常写着（"5.2付款方式"）
  // → 再按块正文找一次（否则会落到"纯文本"兜底且不高亮）
  return blocks.findIndex((b) => b.text.includes(c))
}

/** 按证据原文找所在条款块：中风险项无 clause_ref 时用摘录回指（先精确比，
 *  再忽略空白比一次，容忍 pdf 提取的空格/换行差异）。 */
function findBlockByEvidence(evidence: string, blocks: SourceBlock[]): number {
  const ev = evidence.trim()
  if (!ev) return -1
  const exact = blocks.findIndex((b) => b.text.includes(ev))
  if (exact >= 0) return exact
  const flat = ev.replace(/\s+/g, '')
  return blocks.findIndex((b) => b.text.replace(/\s+/g, '').includes(flat))
}

// OCR 页标记（parser 逐页拼接时插入）：与后端 rules._PAGE_MARK_RE 同口径——比对原句时
// 连同空白一起丢掉，否则"正文有页标记、摘录没有"会让句级定位失败（真实扫描件实测）
const PAGE_MARK_SRC = String.raw`-{2,}\s*第\s*\d+\s*页\s*-{2,}`
const PAGE_MARK_ALL = new RegExp(PAGE_MARK_SRC, 'g') // 整串删除（处理摘录）
const PAGE_MARK_AT = new RegExp(`^${PAGE_MARK_SRC}`) // 锚定匹配（逐字符扫描正文）

/** 把块内文本压成"紧凑串"（去掉空白与页标记），并记录每个字符落在哪个文本节点上，
 *  这样摘录（连续句）才能精确映射回 DOM 位置——PDF/OCR 正文里满是硬换行与页标记。 */
function compactBlock(el: HTMLElement): { flat: string; owner: { node: Text; offset: number }[] } {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  let n = walker.nextNode() as Text | null
  while (n) {
    nodes.push(n)
    n = walker.nextNode() as Text | null
  }
  const chars: string[] = []
  const owner: { node: Text; offset: number }[] = []
  for (const node of nodes) {
    const s = node.data
    let i = 0
    while (i < s.length) {
      // 这种情况是：页标记（内部还可能有空格）→ 整段跳过，不参与比对
      const mark = s.slice(i).match(PAGE_MARK_AT)
      if (mark) {
        i += mark[0].length
        continue
      }
      if (!/\s/.test(s[i])) {
        chars.push(s[i])
        owner.push({ node, offset: i })
      }
      i += 1
    }
  }
  return { flat: chars.join(''), owner }
}

/** 摘录 → 紧凑候选片段（长的优先）。
 *  为什么不止一个候选：规则的摘录是"命中点左右取窗口"，会横跨两个条款块
 *  （实测"…以资共同遵守执行。一、原合同变更内容：…"跨"前言"与"一、"），
 *  整段在一个块里找不到时，要退到"按句拆开、取落在本块的那句"。 */
function quoteCandidates(quote: string): string[] {
  const norm = (s: string) => s.replace(PAGE_MARK_ALL, '').replace(/\s+/g, '')
  const whole = norm(quote)
  const parts = quote
    .split(/[。；;！？]/)
    .map(norm)
    // 太短的碎片（"1.1"这种）既没有定位价值，又容易在正文里撞到别处
    .filter((s) => s.length >= 12)
    .sort((x, y) => y.length - x.length)
  return [whole, ...parts].filter((s) => s.length > 0)
}

/** 摘录与块正文的"最长公共片段"（短于 minLen 视为对不上）。
 *  兜底用：摘录可能带着条款标题、跨块拼接、或与正文有零星差异，
 *  前两种候选都对不上时，至少把真正落在本块的那一段圈出来。 */
function longestOverlap(quote: string, flat: string, minLen = 12): string | null {
  const q = quote.replace(PAGE_MARK_ALL, '').replace(/\s+/g, '')
  let best = ''
  for (let i = 0; i < q.length; i++) {
    // 以 i 为起点二分最长可命中长度（前缀命中单调 → 二分成立）
    let lo = minLen
    let hi = q.length - i
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      if (flat.includes(q.slice(i, i + mid))) {
        if (mid > best.length) best = q.slice(i, i + mid)
        lo = mid + 1
      } else {
        hi = mid - 1
      }
    }
    if (best.length >= q.length) break
  }
  return best.length >= minLen ? best : null
}

/** 在块正文里把摘录那一句包成 <mark class="mk-locate">；命中返回该元素，找不到返回 null。 */
function wrapQuote(el: HTMLElement, quote: string): HTMLElement | null {
  // 只在**正文**里找：块标题与"命中 N"徽标夹在标题和正文之间，
  // 连它们一起算会让"摘录以条款标题开头"的情况永远匹配失败（实测发票那条）
  const body = (el.querySelector('.block-text') as HTMLElement | null) ?? el
  const { flat, owner } = compactBlock(body)
  // 先按整段 / 整句候选找，都对不上再退到"最长公共片段"
  const target = quoteCandidates(quote).find((c) => flat.includes(c)) ?? longestOverlap(quote, flat)
  if (!target) return null
  const at = flat.indexOf(target)
  const head = owner[at]
  const tail = owner[at + target.length - 1]
  if (!head || !tail) return null
  const range = document.createRange()
  range.setStart(head.node, head.offset)
  range.setEnd(tail.node, tail.offset + 1)
  const mark = document.createElement('mark')
  mark.className = 'mk-locate'
  mark.appendChild(range.extractContents())
  range.insertNode(mark)
  return mark
}

/** 清掉上一次定位留下的句内高亮，并合并被拆开的文本节点（下次定位仍能精确回查）。 */
function clearLocateMarks() {
  document.querySelectorAll('.mk-locate').forEach((node) => {
    const parent = node.parentNode
    if (!parent) return
    while (node.firstChild) parent.insertBefore(node.firstChild, node)
    parent.removeChild(node)
    ;(parent as HTMLElement).normalize()
  })
}

/** 滚动到定位目标并保持高亮（下次定位时清掉上一处）：高亮闪一下就没会让人以为没定位成功。
 *  优先只标"命中那一句"，拿不到原句时才退回整块高亮；返回 true 表示已做句级高亮。 */
function flashTo(index: number, quote = ''): boolean {
  const el = document.getElementById(`src-block-${index}`)
  if (!el) return false
  clearLocateMarks()
  document.querySelectorAll('.block.flash').forEach((node) => node.classList.remove('flash'))
  const mark = quote ? wrapQuote(el, quote) : null
  // 这种情况是：标不到原句 → 整块高亮兜底，至少让用户看到定位到了哪一块
  if (!mark) el.classList.add('flash')
  ;(mark ?? el).scrollIntoView({ behavior: 'smooth', block: mark ? 'center' : 'start' })
  return Boolean(mark)
}

/** 纯文本兜底定位：按片段在全文中的位置估滚（无条文结构时仍能跳个大概）。 */
async function scrollTextTo(clause: string) {
  tab.value = 'text'
  await nextTick()
  const text = doc.value?.text ?? ''
  const i = text.indexOf(clause)
  if (i < 0) return
  const el = document.querySelector<HTMLElement>('.raw')
  if (!el) return
  const ratio = i / Math.max(text.length, 1)
  el.scrollTop = ratio * (el.scrollHeight - el.clientHeight)
}

/** 定位指令（anchor.seq 变化）→ 有条文结构就切条文视图滚动，否则纯文本估位。
 *  支持两类目标：clause（条款号）与 evidence（无条款号的中风险摘录）。 */
async function locate() {
  if (!props.anchor) return
  const blocks = doc.value?.blocks ?? []
  const clause = (props.anchor.clause ?? '').trim()
  const evidence = (props.anchor.evidence ?? '').trim()
  if (clause && blocks.length) {
    const i = findBlock(clause, blocks)
    if (i >= 0) {
      tab.value = 'blocks'
      await nextTick()
      // 这种情况是：条款号能对上块，但摘录原句不在该块（条款号常来自模型自述、
      // 可能指偏）→ 改按摘录所在块定位，摘录是原文，落点更可信
      if (flashTo(i, evidence) || !evidence) return
      const byQuote = findBlockByEvidence(evidence, blocks)
      if (byQuote >= 0 && byQuote !== i) {
        flashTo(byQuote, evidence)
        return
      }
      return
    }
  }
  if (evidence) {
    // 这种情况是：风险项没有条款号 → 按证据摘录定位到命中条款块
    const j = blocks.length ? findBlockByEvidence(evidence, blocks) : -1
    if (j >= 0) {
      tab.value = 'blocks'
      await nextTick()
      flashTo(j, evidence)
      return
    }
    await scrollTextTo(evidence)
    return
  }
  await scrollTextTo(clause || evidence)
}

async function load() {
  try {
    const d = await getSource(props.threadId)
    doc.value = d
    error.value = ''
    // 默认 tab：带着定位指令 → 条文视图；否则 pdf/docx 看原文件，其余看条文视图
    if (!d.text) {
      tab.value = 'blocks'
    } else if (!props.anchor) {
      tab.value = d.file_available && (d.suffix === '.pdf' || d.suffix === '.docx') ? 'file' : 'blocks'
    }
    // 这种情况是：打开时带着定位指令（从风险项点进来）→ 数据到齐后滚动
    await locate()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '原文加载失败'
  } finally {
    loading.value = false
  }
}

/** 拉取原 docx 并渲染进 .docx-frame（docx-preview 纯前端渲染，离线可用）。 */
async function renderDocx() {
  const el = docxBox.value
  if (!el || docxBusy.value) return
  // 已渲染过（切走再切回容器被清空，重新渲染一次）
  if (el.firstChild) return
  docxBusy.value = true
  docxError.value = ''
  try {
    const resp = await fetch(taskFileUrl(props.threadId))
    if (!resp.ok) throw new Error(`原文件拉取失败（HTTP ${resp.status}）`)
    const blob = await resp.blob()
    // styleContainer 传同一容器：样式随内容一起注入，作用域不冲突
    await renderAsync(blob, el, el, { className: 'docx' })
    // 渲染后再做一次缩进容错（源文件坏样式会让行首被顶出可视区）
    docxClamped.value = clampAbsurdIndents(el)
  } catch (err) {
    docxError.value = err instanceof Error ? err.message : 'Word 渲染失败'
  } finally {
    docxBusy.value = false
  }
}

// 切到 docx「原文件」页签且数据就绪时渲染；离开再回来也会重触发（容器已重建）
watch(
  () => [doc.value, tab.value, props.threadId] as const,
  async ([d, t]) => {
    if (!d || !isDocx.value || t !== 'file' || !d.file_available) return
    await nextTick()
    await renderDocx()
  },
)

/** 确认下载：临时 <a download> 触发浏览器下载（不经弹窗不会直接下）。 */
function confirmDownload() {
  const name = doc.value?.name
  if (!name) return
  const a = document.createElement('a')
  a.href = taskFileUrl(props.threadId)
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  askDownload.value = false
}

watch(() => props.anchor?.seq, locate)
watch(
  () => props.threadId,
  () => {
    loading.value = true
    doc.value = null
    load()
  },
)

onMounted(() => {
  load()
  // 审查中打开抽屉：原文在 parse 完成后才有，轮询直到出现（2s 一跳）
  timer = window.setInterval(() => {
    if (!loading.value && !hasText.value && !error.value) load()
  }, 2000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <!-- Teleport 到 body：弹窗须相对视口 fixed；放在带动画的容器里会把包含块锁在卡片上，
       导致弹窗变小/偏右 -->
  <Teleport to="body">
    <div class="src-overlay" @click.self="emit('close')">
      <aside
        class="src-panel rise"
        :class="{ resizing }"
        :style="panelWidth ? { width: `${panelWidth}px` } : undefined"
        role="dialog"
        aria-label="原合同查看"
      >
      <!-- 左缘拖拽手柄：右栏默认占 60vw，能拖窄才方便边看报告边核对原文 -->
      <div class="src-resize" title="拖动调整宽度" @mousedown="startResize"></div>
      <header class="src-head">
        <div class="title-wrap">
          <span class="file serif" :title="doc?.name">{{ doc?.name || '…' }}</span>
          <span class="muted small">
            <span v-if="doc" :class="['kind', doc.kind === 'sample' ? 'kind-sample' : 'kind-upload']">
              {{ doc.kind === 'sample' ? '内置样本' : '上传合同' }}
            </span>
            <span v-if="doc?.suffix" class="mono-num">{{ doc.suffix }}</span>
            <span v-if="isDocx">· 网页渲染预览，排版细节可能与 Word 略有差异</span>
          </span>
        </div>
        <div class="acts">
          <button
            v-if="doc?.file_available"
            class="btn btn-ghost sm"
            @click="askDownload = true"
          >下载原文件</button>
          <button class="btn btn-ghost sm" @click="emit('close')">关闭</button>
        </div>
      </header>

      <div class="tabs">
        <button
          v-for="t in tabs"
          :key="t.id"
          :class="{ on: tab === t.id }"
          @click="tab = t.id"
        >{{ t.label }}</button>
      </div>
      <p v-if="tab === 'text' && hasText" class="pane-note">
        模型读取的原始全文快照（md 含 Markdown 标记；如需按条款阅读请切「条文视图」）
      </p>
      <p v-if="tab === 'blocks' && hasText && !isMd" class="pane-note">
        docx 表格已按行列重排；PDF 由逐格提取，折行已拼接、表格以分隔行近似（原版版式见「原文件」页签）
      </p>

      <p v-if="error" class="err pad">{{ error }}</p>

      <!-- 空态：任务还在审查中（原文未解析完）或解析失败 -->
      <div v-else-if="!hasText" class="empty pad">
        <p class="pulse">原文解析中…</p>
        <p class="muted small">任务完成 parse 后自动显示；可稍等片刻（本面板会自动刷新）</p>
      </div>

      <!-- 原文件：pdf 走浏览器内嵌（inline）；docx 由 docx-preview 渲染 -->
      <iframe
        v-else-if="tab === 'file' && doc && doc.file_available && isPdf"
        class="pdf-frame"
        :src="taskFileUrl(threadId)"
        title="原文件预览"
      ></iframe>
      <div
        v-else-if="tab === 'file' && doc && doc.file_available && isDocx"
        class="docx-wrap"
      >
        <p v-if="docxBusy" class="docx-state pulse">正在渲染 Word 原文件…</p>
        <p v-else-if="docxError" class="docx-state docx-err">{{ docxError }}</p>
        <p v-else-if="docxClamped > 0" class="docx-state">
          源文件缩进异常（超 2 英寸），已按容错方式排版，行首不再被顶出可视区。
        </p>
        <div ref="docxBox" class="docx-frame"></div>
      </div>

      <!-- 条文视图：按条款块渲染（md 表格已转文本），块标题即证据定位锚点 -->
      <div v-else-if="tab === 'blocks' && doc" class="src-body">
        <p v-if="anyHit" class="hit-hint">
          命中条款以底色标出（红=高风险 / 琥珀=中风险），句内亮色为风险证据原文
        </p>
        <div
          v-for="(b, i) in doc.blocks"
          :id="`src-block-${i}`"
          :key="i"
          class="block"
          :class="{ hit: blockHits[i]?.risks.length > 0 }"
        >
          <div class="b-top">
            <h4 v-if="b.title" class="block-title serif">{{ b.title }}</h4>
            <span v-if="blockHits[i]?.risks.length" class="hit-badge">
              命中 {{ blockHits[i].risks.length }}
            </span>
          </div>
          <div v-if="blockHits[i]?.risks.length" class="hit-tags">
            <span
              v-for="(r, ri) in blockHits[i].risks"
              :key="ri"
              class="hit-tag"
              :class="r.severity === 'medium' ? 'tag-med' : 'tag-high'"
            >{{ riskLabel(r) }}</span>
          </div>
          <p class="block-text" v-html="blockHtml(i)"></p>
        </div>
        <p v-if="!doc.blocks.length" class="muted small">无条文结构，可切「纯文本」查看全文</p>
      </div>

      <!-- 纯文本全文：等宽快照，与条文视图明显区分 -->
      <pre v-else-if="tab === 'text' && doc" class="raw">{{ doc.text }}</pre>
      </aside>

      <!-- 下载确认弹窗：不静默下载 -->
      <div v-if="askDownload" class="dl-overlay" @click.self="askDownload = false">
        <div class="dl-card rise" role="dialog" aria-label="下载确认">
          <h4 class="serif">下载原文件</h4>
          <p class="dl-name">{{ doc?.name }}</p>
          <p v-if="isPdf" class="muted small">PDF 可在「原文件」页直接预览；确认要下载到本地吗？</p>
          <p v-else-if="isDocx" class="muted small">Word 无法在浏览器预览，下载后用 Word/WPS 打开。</p>
          <p v-else class="muted small">将原文件保存到本地（建议先预览确认内容）。</p>
          <div class="dl-acts">
            <button class="btn btn-ghost sm" @click="askDownload = false">取消</button>
            <button class="btn btn-primary sm" @click="confirmDownload">下载</button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 原文抽屉：宽屏 = 右侧滑出 dock（overlay 不拦截左半，页面仍可读），
   窄屏 = 居中近全屏浮层（下面 media 回退）。功能与定位逻辑不变。 */
.src-overlay {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  justify-content: flex-end;
  pointer-events: none;
}

.src-panel {
  position: relative; /* 左缘拖拽手柄的定位基准 */
  width: min(860px, 60vw);
  height: 100%;
  margin: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid var(--line);
  border-right: 0;
  border-radius: 14px 0 0 14px;
  box-shadow: -22px 0 60px rgba(16, 24, 40, 0.24);
  overflow: hidden;
  pointer-events: auto;
  animation: src-in 0.22s ease both;
}

/* 拖动中禁掉选中文字：否则拖手柄会顺手把原文选中一片 */
.src-panel.resizing {
  user-select: none;
}

/* 左缘拖拽手柄：细条常驻，hover 变主色提醒"这里能拖" */
.src-resize {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 10px;
  z-index: 5;
  cursor: col-resize;
}

.src-resize::after {
  content: "";
  position: absolute;
  left: 3px;
  top: 50%;
  transform: translateY(-50%);
  width: 4px;
  height: 46px;
  border-radius: 3px;
  background: var(--line-strong);
  transition: background 0.15s ease;
}

.src-resize:hover::after {
  background: var(--pri);
}

@keyframes src-in {
  from {
    transform: translateX(46px);
    opacity: 0.4;
  }
  to {
    transform: none;
    opacity: 1;
  }
}

/* 中窄屏：回到居中近全屏浮层（点遮罩空白关闭） */
@media (max-width: 1100px) {
  /* 居中浮层里"拖左缘"没有直观的参照，隐藏手柄，避免误触 */
  .src-resize {
    display: none;
  }

  .src-overlay {
    justify-content: center;
    align-items: center;
    padding: 24px;
    background: rgba(28, 36, 51, 0.45);
    backdrop-filter: blur(3px);
    pointer-events: auto;
  }

  .src-panel {
    width: min(calc(100vw - 48px), 1680px);
    height: calc(100vh - 48px);
    border: 1px solid var(--line);
    border-radius: 12px;
    box-shadow: 0 24px 60px rgba(16, 24, 40, 0.3);
  }
}

/* 窄窗口/手机直接铺满，不留白边 */
@media (max-width: 760px) {
  .src-overlay {
    padding: 0;
  }

  .src-panel {
    width: calc(100vw - 8px);
    height: calc(100vh - 8px);
    border-radius: 0;
    border: 0;
    margin: 4px;
  }
}

.src-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--line);
  background: #fff;
}

.title-wrap {
  min-width: 0;
}

.file {
  display: block;
  font-size: 15px;
  font-weight: 700;
  word-break: break-all;
  letter-spacing: 0.01em;
}

.small {
  font-size: 12px;
}

.kind {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  margin-right: 8px;
  font-weight: 600;
}

.kind-sample {
  color: var(--ok);
  background: var(--ok-soft);
}

.kind-upload {
  color: var(--warn);
  background: var(--warn-soft);
}

.acts {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.btn.sm {
  padding: 5px 12px;
  font-size: 13px;
}

.tabs {
  display: flex;
  gap: 4px;
  padding: 8px 18px 0;
  border-bottom: 1px solid var(--line);
}

.tabs button {
  border: 0;
  background: transparent;
  padding: 6px 14px;
  border-bottom: 2px solid transparent;
  color: var(--muted);
  font-weight: 600;
  font-size: 13.5px;
  letter-spacing: 0.02em;
}

.tabs button.on {
  color: var(--pri);
  border-bottom-color: var(--pri);
}

.pane-note {
  margin: 8px 18px 0;
  font-size: 12px;
  color: var(--muted);
  letter-spacing: 0.03em;
}

.src-body,
.raw {
  flex: 1;
  overflow-y: auto;
  padding: 20px clamp(30px, 6vw, 120px) 56px;
}

/* 条文视图 = 合同排版：条头醒目，正文疏朗仿纸面 */
.block {
  border-bottom: 1px solid var(--line);
  padding: 10px 0 18px;
}

.b-top {
  display: flex;
  align-items: center;
  gap: 10px;
}

.block-title {
  display: inline-block;
  margin: 0;
  padding: 0 0 2px 10px;
  border-left: 3px solid var(--pri);
  background: transparent;
  font-size: 15px;
  letter-spacing: 0.02em;
  color: var(--ink);
}

.hit-badge {
  margin-left: auto;
  flex: none;
  font-size: 12.5px;
  color: #fff;
  background: var(--pri);
  border-radius: 6px;
  padding: 2px 10px;
  font-weight: 600;
}

.hit-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 9px 0 8px;
}

.hit-tag {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 6px;
  letter-spacing: 0.02em;
}

.tag-high {
  color: #fff;
  background: var(--seal);
}

.tag-med {
  color: #fff;
  background: var(--warn);
}

/* 命中条款整块标色：红=高风险 / 琥珀=中风险（换 C 语义色） */
.block.hit {
  background: rgba(224, 69, 79, 0.05);
  border: 1px solid rgba(224, 69, 79, 0.18);
  border-left: 3px solid var(--seal);
  border-radius: 8px;
  padding: 12px 14px 16px;
  margin: 4px 0 8px;
}

.hit-hint {
  font-size: 12.5px;
  color: var(--muted);
  letter-spacing: 0.04em;
  margin: 0 0 14px;
}

.block-text {
  margin: 0;
  white-space: pre-line;
  color: var(--ink-2);
  font-size: 15px;
  line-height: 1.9;
  text-align: justify;
}

.block-text :deep(mark) {
  padding: 0 2px;
  border-radius: 2px;
  font-weight: 600;
}

.block-text :deep(mark.mk-high) {
  background: #ffd9d4;
  color: #a02c33;
  box-shadow: inset 0 -2px 0 rgba(224, 69, 79, 0.3);
}

.block-text :deep(mark.mk-med) {
  background: #f7e7bd;
  color: #7a5510;
}

/* 定位命中句：句级高亮（蓝底 + 下划线）——比整块框选精确得多，
   扫描件那种"一块里塞十几条"的文本尤其需要 */
.block-text :deep(mark.mk-locate) {
  background: rgba(52, 86, 209, 0.16);
  color: var(--ink);
  box-shadow: inset 0 -2px 0 rgba(52, 86, 209, 0.45);
  border-radius: 2px;
  padding: 0 2px;
}

/* docx/pdf 条文块的结构化排版：段落、近似表格行、真表格 */
.block-text p.pl {
  margin: 0 0 0.6em;
  white-space: normal;
  text-align: justify;
}

.block-text p.pl:last-child {
  margin-bottom: 0;
}

.block-text .tbl-flat {
  margin: 4px 0 12px;
  padding: 8px 10px;
  border: 1px dashed var(--line-strong);
  border-radius: 6px;
  background: var(--card2);
  font-size: 12.5px;
  line-height: 1.8;
  color: var(--ink-2);
}

.mini-tbl {
  margin: 6px 0 12px;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
}

.mini-tbl table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  line-height: 1.7;
}

.mini-tbl th,
.mini-tbl td {
  padding: 5px 10px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}

.mini-tbl th {
  background: var(--card2);
  font-weight: 600;
  color: var(--ink-2);
  white-space: nowrap;
}

.mini-tbl tbody tr:last-child td {
  border-bottom: 0;
}

.block.flash {
  background: rgba(52, 86, 209, 0.08);
  border-radius: 3px;
  padding-left: 8px;
  border-left: 3px solid var(--pri);
  transition: background 0.5s ease;
}

/* 纯文本 = 机器快照：等宽、浅底虚线框，与条文视图一眼可分 */
.raw {
  margin: 14px clamp(18px, 4vw, 90px) 24px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--card-2);
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.9;
  white-space: pre-wrap;
  color: var(--ink-2);
  box-shadow: inset 0 1px 3px rgba(16, 24, 40, 0.05);
}

.pdf-frame {
  flex: 1;
  border: 0;
  width: 100%;
  background: #4b5563;
  min-height: 0;
}

/* docx 渲染区：深底 + 白纸页面由 docx-preview 生成，横向可滚 */
.docx-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #4b5563;
}

.docx-state {
  color: #dbe2ee;
  font-size: 13.5px;
  padding: 10px 18px;
  margin: 0;
}

.docx-err {
  color: #ffc9c9;
}

.docx-frame {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 18px 24px 32px;
  background: #4b5563;
}

/* docx-preview 生成的页面在浅色容器上保持白纸外观 */
.docx-frame :deep(.docx-wrapper) {
  background: transparent;
  padding: 0;
}

.empty {
  text-align: center;
  padding-top: 60px;
}

.err {
  color: var(--seal);
}

.pad {
  padding: 18px 22px;
}

/* 下载确认弹窗：居中纸片 */
.dl-overlay {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(28, 36, 51, 0.42);
  backdrop-filter: blur(2px);
  display: grid;
  place-items: center;
}

.dl-card {
  width: min(400px, 90vw);
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: 0 16px 48px rgba(16, 24, 40, 0.22);
  padding: 20px 22px 16px;
}

.dl-card h4 {
  margin: 0 0 10px;
  letter-spacing: 0.02em;
}

.dl-name {
  margin: 0 0 10px;
  font-weight: 700;
  word-break: break-all;
  font-size: 14px;
}

.dl-acts {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}
</style>
