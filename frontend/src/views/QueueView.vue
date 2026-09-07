<!--
  队列视图：统计条（按状态计数）+ 搜索框（按文件名/任务号）+ 筛选标签 +
  任务列表，2.5s 轮询；「刷新」会立即重拉并显示更新时间（演示按钮已按
  用户要求移除，2026-09-05）。
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { listTasks } from '../api'
import type { TaskSummary, TaskStatus } from '../types'

// 行内「查看」跳详情
const emit = defineEmits<{ open: [threadId: string] }>()

const tasks = ref<TaskSummary[]>([])
const concurrency = ref(1) // 服务端并发上限（/api/tasks 返回；缺省按 1 处理）
const error = ref('')
const filter = ref<'all' | TaskStatus>('all')
const query = ref('') // 按文件名/任务号搜索
const refreshing = ref(false) // 手动刷新进行中（按钮反馈）
const lastUpdated = ref('') // 最近一次成功拉取时间（手动刷新时可见变化）
let timer: number | undefined

const statusText: Record<TaskStatus, string> = {
  pending: '排队中',
  processing: '审查中',
  gate: '待审批',
  done: '已完成',
  error: '失败',
}

const statusClass: Record<TaskStatus, string> = {
  pending: 'stamp-mute',
  processing: 'stamp-info', // 审查中=信息蓝（过程态，区别于风险语义）
  gate: 'stamp-seal',
  done: 'stamp-ok',
  error: 'stamp-seal',
}

const gradeText: Record<string, string> = {
  pass: '通过',
  conditional_pass: '有条件通过',
  fail: '不通过',
}

/** 行内评级配色：疑似空白模板的"待确认"与 fail 走琥珀/红，pass 走绿。 */
function gradeClass(t: TaskSummary): string {
  if (t.template && t.grade === 'conditional_pass') return 'g-uncertain'
  if (t.grade === 'pass') return 'g-ok'
  if (t.grade === 'fail') return 'g-bad'
  if (t.grade === 'conditional_pass') return 'g-warn'
  return ''
}

/** 行内评级文案：疑似空白模板的 conditional_pass 显示"待确认"更直白。 */
function gradeShow(t: TaskSummary): string {
  if (t.template && t.grade === 'conditional_pass') return '待确认'
  return gradeText[t.grade ?? ''] ?? t.grade ?? ''
}

/** 行内状态展示：疑似空白模板任务显示"待确认"（琥珀），不写"已完成"，避免
 *  "已完成 + 待确认"的矛盾观感（后端仍是 done，仅展示层区分）。 */
function dispStatus(t: TaskSummary): { text: string; cls: string } {
  if (t.template && t.status === 'done') return { text: '待确认', cls: 'stamp-warn' }
  return { text: statusText[t.status], cls: statusClass[t.status] }
}

/** 行内评级展示：疑似空白模板给"疑似空白模板"，比泛泛的"待确认"更直白。 */
function dispGrade(t: TaskSummary): string {
  if (t.template && t.grade === 'conditional_pass') return '疑似空白模板'
  return gradeShow(t)
}

/** 各状态数量：统计条与空态文案都用它。 */
const counts = computed(() => {
  const c: Record<TaskStatus, number> = { pending: 0, processing: 0, gate: 0, done: 0, error: 0 }
  for (const t of tasks.value) c[t.status] += 1
  return c
})

/** 同名次数：重复上传同一份文档时在行内标"同名 ×n"，方便找重复。 */
const nameCounts = computed(() => {
  const m = new Map<string, number>()
  for (const t of tasks.value) {
    const k = t.source.trim().toLowerCase()
    m.set(k, (m.get(k) ?? 0) + 1)
  }
  return m
})

/** 行内文件类型小标：按后缀给短标签与色类（docx/pdf/md/txt/其他）。 */
function fileChip(t: TaskSummary): { label: string; cls: string } {
  const s = t.source.toLowerCase()
  if (s.endsWith('.docx')) return { label: 'DOC', cls: 'docx' }
  if (s.endsWith('.pdf')) return { label: 'PDF', cls: 'pdf' }
  if (s.endsWith('.md')) return { label: 'MD', cls: 'md' }
  if (s.endsWith('.txt')) return { label: 'TXT', cls: 'txt' }
  return { label: 'FILE', cls: 'file' }
}

/** 可见任务 = 状态筛选 ∩ 名称/任务号搜索（忽略大小写）。 */
const visible = computed(() => {
  const q = query.value.trim().toLowerCase()
  return tasks.value.filter((t) => {
    if (filter.value !== 'all' && t.status !== filter.value) return false
    if (!q) return true
    return t.source.toLowerCase().includes(q) || t.thread_id.toLowerCase().includes(q)
  })
})

/** 拉取列表；失败保留旧数据，下周期自动重试。 */
async function load() {
  try {
    const res = await listTasks()
    tasks.value = res.tasks
    concurrency.value = res.concurrency ?? 1
    lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    error.value = ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  }
}

/** 手动刷新：置 refreshing 给按钮即时反馈（自动轮询不走这里）。 */
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  await load()
  refreshing.value = false
}

onMounted(() => {
  load()
  // 轮询 2.5s：队列状态持续变化，页面开销可忽略
  timer = window.setInterval(load, 2500)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <section class="rise">
    <!-- 页头：标题 + 手动刷新（带即时反馈） -->
    <div class="head">
      <div>
        <h2>任务队列</h2>
        <p class="muted">每 2.5s 自动刷新 · 高风险任务会停在「待审批」等你处理</p>
      </div>
      <div class="head-actions">
        <button class="btn btn-ghost" :disabled="refreshing" @click="refresh">
          {{ refreshing ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p class="sysline muted">
      并发上限 {{ concurrency }} · 排队 {{ counts.pending }} · 审查中 {{ counts.processing }}
      <template v-if="lastUpdated"> · 更新于 {{ lastUpdated }}</template>
    </p>

    <!-- 按文件名/任务号搜索：重复上传多份时快速定位 -->
    <div class="searchbar">
      <span class="search-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <circle cx="11" cy="11" r="7"></circle>
          <path d="M20 20l-3.5-3.5"></path>
        </svg>
        <input
          v-model="query"
          type="text"
          placeholder="按文件名 / 任务号搜索（同名重复也能筛出来）"
        />
      </span>
      <button v-if="query" class="btn btn-plain sm" @click="query = ''">清空</button>
    </div>

    <!-- 统计条：一眼看到待审批/审查中/完成分布 -->
    <div class="stats">
      <div class="stat card">
        <span class="lab">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"></path><path d="M12 8v4M12 15.5v.5"></path></svg>
          待审批
        </span>
        <b class="mono-num">{{ counts.gate }}</b>
      </div>
      <div class="stat card">
        <span class="lab">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>
          进行中
        </span>
        <b class="mono-num">{{ counts.processing + counts.pending }}</b>
      </div>
      <div class="stat card">
        <span class="lab">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M8 12.5l2.5 2.5L16 9.5"></path></svg>
          已完成
        </span>
        <b class="mono-num">{{ counts.done }}</b>
      </div>
      <div class="stat card">
        <span class="lab">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 8v4M12 15.5v.5"></path></svg>
          失败
        </span>
        <b class="mono-num">{{ counts.error }}</b>
      </div>
      <div class="stat card total">
        <span class="lab">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"></path></svg>
          全部
        </span>
        <b class="mono-num">{{ tasks.length }}</b>
      </div>
    </div>

    <!-- 筛选标签：全部/待审批/审查中/已完成/失败 -->
    <div class="filters">
      <button v-for="f in (['all', 'gate', 'processing', 'pending', 'done', 'error'] as const)" :key="f"
              :class="{ on: filter === f }" @click="filter = f">
        {{ f === 'all' ? '全部' : statusText[f] }}
        <span class="mono-num">{{ f === 'all' ? tasks.length : counts[f] }}</span>
      </button>
    </div>

    <div v-if="!tasks.length" class="empty-card">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 7l2-3h12l2 3v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"></path><path d="M4 7h16M9 12h6"></path></svg>
      <p>还没有任务——上传几份合同后就会出现在这里</p>
    </div>
    <div v-else-if="!visible.length" class="empty-card">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg>
      <p>{{ query.trim() ? `没有匹配「${query.trim()}」的任务，试试改一下名字或清空筛选` : '该筛选下暂无任务' }}</p>
    </div>

    <!-- 任务列表 -->
    <div v-else class="card list">
      <div class="th">
        <span>任务（文件 / 任务号）</span>
        <span>状态</span>
        <span>风险 / 评级</span>
        <span class="op-hd">操作</span>
      </div>
      <div v-for="t in visible" :key="t.thread_id" class="row">
        <div class="name">
          <span class="file-line">
            <span class="ficon" :class="fileChip(t).cls">{{ fileChip(t).label }}</span>
            <span class="file">{{ t.source }}</span>
            <span v-if="nameCounts.get(t.source.trim().toLowerCase())! > 1" class="dup-badge mono-num">
              同名 ×{{ nameCounts.get(t.source.trim().toLowerCase()) }}
            </span>
          </span>
          <span class="mono-num tid">{{ t.thread_id }}</span>
        </div>
        <span class="stamp" :class="dispStatus(t).cls">{{ dispStatus(t).text }}</span>
        <span class="rk">
          <span v-if="t.risk_count != null" class="risk-badge mono-num"
                :class="t.template ? 'warn' : t.status === 'done' ? 'ok' : 'seal'">
            {{ t.status === 'gate' ? `待审 ${t.risk_count}` : `${t.risk_count} 项` }}
          </span>
          <span v-if="t.grade" class="grade" :class="gradeClass(t)">{{ dispGrade(t) }}</span>
        </span>
        <button class="op" @click="emit('open', t.thread_id)">查看 →</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.head h2 {
  font-size: 21px;
  letter-spacing: 0.02em;
  margin: 0 0 4px;
  font-weight: 700;
}

.head p {
  margin: 0;
  font-size: 13px;
}

.head-actions {
  display: flex;
  gap: 10px;
}

.sysline {
  margin: 10px 0 0;
  font-size: 13px;
  letter-spacing: 0.02em;
  font-family: var(--mono);
  color: var(--ink-2);
}

.searchbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0 2px;
}

.search-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
  width: min(380px, 100%);
}

.search-wrap svg {
  position: absolute;
  left: 11px;
  width: 14px;
  height: 14px;
  color: var(--muted);
  pointer-events: none;
}

.searchbar input {
  max-width: none;
  padding-left: 34px;
}

.btn.sm {
  padding: 5px 12px;
  font-size: 13px;
}

.err {
  color: var(--seal);
}

.stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
  margin: 18px 0;
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 16px;
  overflow: hidden;
  transition: transform 0.12s ease;
}

.stat:hover {
  transform: translateY(-1px);
}

.stat b {
  font-size: 22px;
  line-height: 1.2;
}

.stat .lab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.stat .lab svg {
  width: 13px;
  height: 13px;
}

/* KPI 带按语义着色（红=待批/失败，蓝=进行中，绿=完成；全部=靛蓝） */
.stat:nth-child(1) b {
  color: var(--seal);
}

.stat:nth-child(1) {
  border-top: 2px solid rgba(224, 69, 79, 0.55);
}

.stat:nth-child(2) b {
  color: var(--info);
}

.stat:nth-child(2) {
  border-top: 2px solid rgba(47, 128, 216, 0.55);
}

.stat:nth-child(3) b {
  color: var(--ok);
}

.stat:nth-child(3) {
  border-top: 2px solid rgba(47, 158, 99, 0.55);
}

.stat:nth-child(4) b {
  color: var(--seal);
}

.stat:nth-child(4) {
  border-top: 2px solid rgba(224, 69, 79, 0.35);
}

.stat.total b {
  color: var(--pri);
}

.stat.total {
  border-top: 2px solid rgba(52, 86, 209, 0.5);
}

.filters {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.filters button {
  border: 1px solid var(--line);
  background: transparent;
  border-radius: 8px;
  padding: 4px 12px;
  font-size: 13px;
  color: var(--ink-2);
  display: inline-flex;
  gap: 6px;
  align-items: center;
}

.filters button.on {
  border-color: var(--pri);
  color: var(--pri);
  background: var(--pri-soft);
  font-weight: 700;
}

.filters .mono-num {
  font-size: 12px;
}

.empty-card {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 26px 16px;
  margin-top: 12px;
  border: 1px dashed var(--line-strong);
  border-radius: 10px;
  background: #fff;
  color: var(--muted);
  font-size: 13px;
}

.empty-card svg {
  width: 17px;
  height: 17px;
  flex: none;
  color: var(--line2);
}

.empty-card p {
  margin: 0;
}

.list {
  overflow: hidden;
}

.th {
  display: grid;
  /* 四列表格：任务弹性占主，状态/风险按比例铺满整行，操作固定右对齐，
     避免"1fr auto auto"把所有内容推到最右挤成一簇 */
  grid-template-columns: minmax(240px, 1.3fr) minmax(108px, 0.55fr) minmax(220px, 1fr) 84px;
  align-items: center;
  gap: 16px;
  padding: 8px 18px;
  background: var(--card-2);
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.03em;
}

.th .op-hd {
  justify-self: end;
}

.row {
  display: grid;
  grid-template-columns: minmax(240px, 1.3fr) minmax(108px, 0.55fr) minmax(220px, 1fr) 84px;
  align-items: center;
  gap: 16px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--line);
}

.row:last-child {
  border-bottom: 0;
}

.row:hover {
  background: #f8faff;
  box-shadow: inset 3px 0 0 var(--pri);
}

/* 列表行轻量入场（一次性淡入上移，不逐行错峰，避免"弹跳"感） */
.list .row {
  animation: rise 0.2s ease both;
}

.name {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.file {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.file-line .file {
  min-width: 0;
}

/* 文件类型小图标：按后缀着色（DOC/PDF/MD/TXT） */
.ficon {
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  display: inline-grid;
  place-items: center;
  font-size: 8.5px;
  font-weight: 800;
  color: #fff;
  letter-spacing: 0.02em;
}

.ficon.docx {
  background: #2b579a;
}

.ficon.pdf {
  background: #c4302b;
}

.ficon.md {
  background: #4b5a77;
}

.ficon.txt {
  background: #6b7f9e;
}

.ficon.file {
  background: var(--muted);
}

/* 行内"查看"：靛蓝文字操作（对齐表头操作列右侧） */
.op {
  border: 0;
  background: none;
  color: var(--pri);
  font-size: 12.5px;
  font-weight: 600;
  padding: 4px 8px;
  border-radius: 6px;
  justify-self: end;
  cursor: pointer;
  transition: background 0.12s ease;
}

.op:hover {
  background: var(--pri-soft);
}

/* 同名徽标：同一份文档被反复上传时提示重复 */
.dup-badge {
  flex: none;
  font-size: 11px;
  color: var(--warn);
  background: var(--warn-soft);
  border: 1px solid rgba(192, 127, 18, 0.3);
  border-radius: 6px;
  padding: 0 8px;
}

.tid {
  color: var(--muted);
  font-size: 12px;
}

.rk {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  white-space: nowrap;
}

.risk-badge {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 999px;
}

.risk-badge.seal {
  color: var(--seal-deep);
  background: var(--seal-soft);
  border: 1px solid rgba(224, 69, 79, 0.2);
}

.risk-badge.ok {
  color: var(--ok);
  background: var(--ok-soft);
  border: 1px solid rgba(47, 158, 99, 0.2);
}

/* 疑似空白模板的"待确认"风险数用琥珀，和状态语义一致 */
.risk-badge.warn {
  color: var(--warn);
  background: var(--warn-soft);
  border: 1px solid rgba(192, 127, 18, 0.25);
}

.grade {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

/* 行内评级按语义分色：pass 绿 / 待确认·有条件琥珀 / fail 红 */
.grade.g-ok {
  color: var(--ok);
}

.grade.g-warn,
.grade.g-uncertain {
  color: var(--warn);
  font-weight: 700;
}

.grade.g-bad {
  color: var(--seal-deep);
}

@media (max-width: 720px) {
  .stats {
    grid-template-columns: repeat(3, 1fr);
  }

  .th {
    display: none;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: center;
  }

  .row .name {
    flex: 1 1 100%;
  }

  .row .op {
    margin-left: auto;
  }
}
</style>
