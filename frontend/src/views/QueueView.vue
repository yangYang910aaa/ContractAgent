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
  processing: 'stamp-warn',
  gate: 'stamp-seal',
  done: 'stamp-ok',
  error: 'stamp-seal',
}

const gradeText: Record<string, string> = {
  pass: '通过',
  conditional_pass: '有条件通过',
  fail: '不通过',
}

/** 行内评级文案：疑似空白模板的 conditional_pass 显示"待确认"更直白。 */
function gradeShow(t: TaskSummary): string {
  if (t.template && t.grade === 'conditional_pass') return '待确认'
  return gradeText[t.grade ?? ''] ?? t.grade ?? ''
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
      <input
        v-model="query"
        type="text"
        placeholder="按文件名 / 任务号搜索（同名重复也能筛出来）"
      />
      <button v-if="query" class="btn btn-plain sm" @click="query = ''">清空</button>
    </div>

    <!-- 统计条：一眼看到待审批/审查中/完成分布 -->
    <div class="stats">
      <div class="stat card">
        <b class="mono-num">{{ counts.gate }}</b><span>待审批</span>
      </div>
      <div class="stat card">
        <b class="mono-num">{{ counts.processing + counts.pending }}</b><span>进行中</span>
      </div>
      <div class="stat card">
        <b class="mono-num">{{ counts.done }}</b><span>已完成</span>
      </div>
      <div class="stat card">
        <b class="mono-num">{{ counts.error }}</b><span>失败</span>
      </div>
      <div class="stat card total">
        <b class="mono-num">{{ tasks.length }}</b><span>全部</span>
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

    <p v-if="!tasks.length" class="empty muted">
      还没有任务——上传几份合同后就会出现在这里
    </p>
    <p v-else-if="!visible.length" class="empty muted">
      {{ query.trim() ? `没有匹配「${query.trim()}」的任务，试试改一下名字或清空筛选` : '该筛选下暂无任务' }}
    </p>

    <!-- 任务列表 -->
    <div v-else class="card list">
      <div v-for="t in visible" :key="t.thread_id" class="row">
        <div class="name">
          <span class="file-line">
            <span class="file">{{ t.source }}</span>
            <span v-if="nameCounts.get(t.source.trim().toLowerCase())! > 1" class="dup-badge mono-num">
              同名 ×{{ nameCounts.get(t.source.trim().toLowerCase()) }}
            </span>
          </span>
          <span class="mono-num tid">{{ t.thread_id }}</span>
        </div>
        <div class="meta">
          <span v-if="t.risk_count != null" class="risk-badge mono-num"
                :class="t.status === 'done' ? 'ok' : 'seal'">
            {{ t.status === 'gate' ? `待审 ${t.risk_count}` : `${t.risk_count} 项` }}
          </span>
          <span class="stamp" :class="statusClass[t.status]">{{ statusText[t.status] }}</span>
          <span v-if="t.grade" class="grade serif">{{ gradeShow(t) }}</span>
        </div>
        <button class="btn btn-ghost" @click="emit('open', t.thread_id)">查看</button>
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
  font-family: var(--serif);
  font-size: 26px;
  letter-spacing: 0.12em;
  margin: 0 0 4px;
  padding-left: 15px;
  position: relative;
}

/* 页头左侧朱线：像文书篇题的小标记 */
.head h2::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0.2em;
  bottom: 0.2em;
  width: 4px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--seal), rgba(165, 49, 44, 0.3));
}

.head p {
  margin: 0;
  font-size: 13.5px;
}

.head-actions {
  display: flex;
  gap: 10px;
}

.sysline {
  margin: 10px 0 0;
  font-size: 13px;
  letter-spacing: 0.06em;
  font-family: var(--mono);
  color: var(--ink-2); /* 比 muted 更深：状态行要一眼能读 */
}

.searchbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0 2px;
}

.searchbar input {
  max-width: 420px;
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
  align-items: center;
  gap: 2px;
  padding: 14px 8px;
  border-top: 2px solid transparent;
  transition: transform 0.12s ease;
}

.stat:hover {
  transform: translateY(-2px);
}

.stat b {
  font-size: 26px;
  line-height: 1.1;
}

.stat span {
  color: var(--muted);
  font-size: 12.5px;
  letter-spacing: 0.12em;
}

/* 统计条按语义着色（朱=待批/失败，琥珀=进行中，石绿=完成） */
.stat:nth-child(1) b {
  color: var(--seal);
}

.stat:nth-child(1) {
  border-top-color: rgba(165, 49, 44, 0.5);
}

.stat:nth-child(2) b {
  color: var(--warn);
}

.stat:nth-child(2) {
  border-top-color: rgba(156, 107, 28, 0.5);
}

.stat:nth-child(3) b {
  color: var(--ok);
}

.stat:nth-child(3) {
  border-top-color: rgba(61, 106, 69, 0.5);
}

.stat:nth-child(4) b {
  color: var(--seal);
}

.stat:nth-child(4) {
  border-top-color: rgba(165, 49, 44, 0.35);
}

.stat.total b {
  color: var(--ink);
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
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 13px;
  color: var(--ink-2);
  display: inline-flex;
  gap: 6px;
  align-items: center;
}

.filters button.on {
  border-color: var(--seal);
  color: var(--seal);
  background: var(--seal-soft);
  font-weight: 700;
}

.filters .mono-num {
  font-size: 12px;
}

.empty {
  padding: 36px 0;
}

.list {
  overflow: hidden;
}

.row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 16px;
  padding: 13px 18px;
  border-bottom: 1px solid var(--line);
}

.row:last-child {
  border-bottom: 0;
}

.row:hover {
  background: var(--paper);
  box-shadow: inset 3px 0 0 var(--seal);
}

/* 列表行轻微错峰入场（最多 8 行封顶，避免延迟过长） */
.list .row {
  animation: rise 0.32s ease both;
}

.list .row:nth-of-type(1) { animation-delay: 0.02s; }
.list .row:nth-of-type(2) { animation-delay: 0.06s; }
.list .row:nth-of-type(3) { animation-delay: 0.1s; }
.list .row:nth-of-type(4) { animation-delay: 0.14s; }
.list .row:nth-of-type(5) { animation-delay: 0.18s; }
.list .row:nth-of-type(6) { animation-delay: 0.22s; }
.list .row:nth-of-type(7) { animation-delay: 0.26s; }
.list .row:nth-of-type(8) { animation-delay: 0.3s; }

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

/* 同名徽标：同一份文档被反复上传时提示重复 */
.dup-badge {
  flex: none;
  font-size: 11px;
  color: var(--warn);
  background: var(--warn-soft);
  border: 1px solid rgba(156, 107, 28, 0.3);
  border-radius: 999px;
  padding: 0 8px;
}

.tid {
  color: var(--muted);
  font-size: 12px;
}

.meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.risk-badge {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 999px;
}

.risk-badge.seal {
  color: var(--seal);
  background: var(--seal-soft);
}

.risk-badge.ok {
  color: var(--ok);
  background: var(--ok-soft);
}

.grade {
  font-size: 15px;
}

@media (max-width: 720px) {
  .stats {
    grid-template-columns: repeat(3, 1fr);
  }

  .row {
    grid-template-columns: 1fr auto;
  }
}
</style>
