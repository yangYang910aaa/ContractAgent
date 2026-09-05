<!--
  U2 原文抽屉：任务页「查看原合同」的侧滑面板（2026-09-05 体验修订）。
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
let timer: number | undefined

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
      const ev = (r.evidence ?? '').trim()
      if (clause) {
        if (b.ref === clause) return true
        if (b.title.includes(clause) || clause.includes(b.title)) return true
      }
      return Boolean(ev && b.text.includes(ev))
    })
    // 证据句去重、超长截断（只标引用核心片段）；gate 载荷无 severity → 按 high 处理
    const markers: { text: string; sev: string }[] = []
    for (const r of hits) {
      const ev = (r.evidence ?? '').trim()
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

/** 某块的正文 HTML：换行转 <br>，命中证据句包 <mark>（长句先替换防拆碎）。 */
function blockHtml(index: number): string {
  const b = doc.value?.blocks[index]
  if (!b) return ''
  let html = escHtml(blockText(b)).replace(/\r?\n/g, '<br>')
  const markers = blockHits.value[index]?.markers ?? []
  for (const m of [...markers].sort((x, y) => y.text.length - x.text.length)) {
    const esc = escHtml(m.text)
    html = html.split(esc).join(`<mark class="mk-${m.sev === 'medium' ? 'med' : 'high'}">${esc}</mark>`)
  }
  return html
}

/** 任一条款块命中风险的标记（空态不显示提示条）。 */
const anyHit = computed(() => blockHits.value.some((h) => h.risks.length > 0))

/** 按风险项 clause_ref 找条款块下标：先精确比 ref，再按标题包含兜底。 */
function findBlock(clause: string, blocks: SourceBlock[]): number {
  const exact = blocks.findIndex((b) => b.ref === clause)
  if (exact >= 0) return exact
  const c = clause.trim()
  return blocks.findIndex((b) => b.title.includes(c) || c.includes(b.title))
}

/** 滚动到目标块并闪一下背景（证据定位的轻量反馈，不打断阅读）。 */
function flashTo(index: number) {
  const el = document.getElementById(`src-block-${index}`)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  el.classList.add('flash')
  window.setTimeout(() => el.classList.remove('flash'), 1400)
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

/** 定位指令（anchor.seq 变化）→ 有条文结构就切条文视图滚动，否则纯文本估位。 */
async function locate() {
  if (!props.anchor) return
  const blocks = doc.value?.blocks ?? []
  const i = blocks.length ? findBlock(props.anchor.clause, blocks) : -1
  if (i >= 0) {
    tab.value = 'blocks'
    await nextTick()
    flashTo(i)
    return
  }
  await scrollTextTo(props.anchor.clause)
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
  <!-- Teleport 到 body：弹窗须相对视口 fixed；放在动画容器（.rise 带
       transform）内会把包含块锁在卡片上，导致弹窗变小/偏右（2026-09-05 实测） -->
  <Teleport to="body">
    <div class="src-overlay" @click.self="emit('close')">
      <aside class="src-panel rise" role="dialog" aria-label="原合同查看">
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
/* 遮罩 + 近全屏居中弹窗：点弹层空白关闭；弹窗占几乎整个可视区 */
.src-overlay {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: rgba(64, 52, 30, 0.42);
  backdrop-filter: blur(3px);
  display: flex;
  padding: 24px;
}

.src-panel {
  /* 确定尺寸 + margin:auto 居中：不依赖网格轨道百分比，也不会随内容塌缩 */
  width: min(calc(100vw - 48px), 1680px);
  height: calc(100vh - 48px);
  margin: auto;
  display: flex;
  flex-direction: column;
  background:
    repeating-linear-gradient(-45deg, rgba(110, 92, 52, 0.012) 0 1px, transparent 1px 7px),
    linear-gradient(180deg, #fdf9ee, #f6efdc);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  box-shadow: 0 24px 70px rgba(44, 35, 20, 0.35);
  overflow: hidden;
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
  background: rgba(249, 244, 232, 0.6);
}

.title-wrap {
  min-width: 0;
}

.file {
  display: block;
  font-size: 17px;
  font-weight: 700;
  word-break: break-all;
  letter-spacing: 0.03em;
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
  font-family: var(--kai);
  font-size: 14.5px;
  letter-spacing: 0.1em;
}

.tabs button.on {
  color: var(--seal);
  border-bottom-color: var(--seal);
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
  border-bottom: 1px dashed var(--line);
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
  padding: 2px 12px 2px 10px;
  border-left: 4px solid var(--seal);
  background: linear-gradient(90deg, var(--seal-soft), rgba(243, 223, 215, 0));
  font-size: 16px;
  letter-spacing: 0.08em;
  color: var(--ink);
}

.hit-badge {
  margin-left: auto;
  flex: none;
  font-family: var(--kai);
  font-size: 12.5px;
  color: #fff;
  background: linear-gradient(#c94a41, #b23a32);
  border-radius: 999px;
  padding: 1px 11px;
  box-shadow: 0 1px 2px rgba(127, 33, 28, 0.35);
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
  padding: 1px 9px;
  border-radius: 999px;
  letter-spacing: 0.05em;
}

.tag-high {
  color: #fff;
  background: #c94a41;
}

.tag-med {
  color: #fff;
  background: #c78f24;
}

/* 命中条款整块标色：红=高风险 / 琥珀=中风险 */
.block.hit {
  background: rgba(165, 49, 44, 0.06);
  border: 1px solid rgba(165, 49, 44, 0.18);
  border-left: 3px solid var(--seal);
  border-radius: 4px;
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
  font-family: var(--serif);
  font-size: 15.5px;
  line-height: 2;
  text-align: justify;
}

.block-text :deep(mark) {
  padding: 0 2px;
  border-radius: 2px;
  font-weight: 600;
}

.block-text :deep(mark.mk-high) {
  background: #ffd7cc;
  color: #8c221a;
  box-shadow: inset 0 -2px 0 rgba(165, 49, 44, 0.35);
}

.block-text :deep(mark.mk-med) {
  background: #f6e3ac;
  color: #6d4b0c;
}

.block.flash {
  background: rgba(165, 49, 44, 0.09);
  border-radius: 3px;
  padding-left: 8px;
  border-left: 3px solid var(--seal);
  transition: background 0.5s ease;
}

/* 纯文本 = 机器快照：等宽、浅底虚线框，与条文视图一眼可分 */
.raw {
  margin: 14px clamp(18px, 4vw, 90px) 24px;
  border: 1px dashed var(--line-strong);
  border-radius: 3px;
  background: var(--card-2);
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.9;
  white-space: pre-wrap;
  color: var(--ink-2);
  box-shadow: inset 0 1px 3px rgba(90, 76, 45, 0.06);
}

.pdf-frame {
  flex: 1;
  border: 0;
  width: 100%;
  background: #4c4436;
  min-height: 0;
}

/* docx 渲染区：深底 + 白纸页面由 docx-preview 生成，横向可滚 */
.docx-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #4c4436;
}

.docx-state {
  color: #efe6d2;
  font-size: 13.5px;
  padding: 10px 18px;
  margin: 0;
}

.docx-err {
  color: #ffd9cc;
}

.docx-frame {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 18px 24px 32px;
  background: #525659;
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
  background: rgba(64, 52, 30, 0.34);
  backdrop-filter: blur(2px);
  display: grid;
  place-items: center;
}

.dl-card {
  width: min(400px, 90vw);
  background: linear-gradient(180deg, #fdf9ee, #f5edda);
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  box-shadow: 0 12px 40px rgba(64, 52, 30, 0.28);
  padding: 20px 22px 16px;
}

.dl-card h4 {
  margin: 0 0 10px;
  letter-spacing: 0.12em;
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
