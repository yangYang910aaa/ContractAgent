<!--
  队列视图：统计条（按状态计数）+ 筛选标签 + 任务列表，2.5s 轮询。
  提供「一键演示」：服务端直接把内置合成样本入队（剧本 3 批量演示）。
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { demoRun, listTasks } from '../api'
import type { TaskSummary, TaskStatus } from '../types'

// 行内「查看」跳详情；消息 flash 是轻量提示（无 toast 依赖）
const emit = defineEmits<{ open: [threadId: string] }>()

const tasks = ref<TaskSummary[]>([])
const error = ref('')
const flash = ref('') // 临时提示（如"已入队 5 份演示样本"）
const filter = ref<'all' | TaskStatus>('all')
const demoBusy = ref(false)
let timer: number | undefined
let flashTimer: number | undefined

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

/** 各状态数量：统计条与空态文案都用它。 */
const counts = computed(() => {
  const c: Record<TaskStatus, number> = { pending: 0, processing: 0, gate: 0, done: 0, error: 0 }
  for (const t of tasks.value) c[t.status] += 1
  return c
})

const visible = computed(() =>
  filter.value === 'all' ? tasks.value : tasks.value.filter((t) => t.status === filter.value),
)

function showFlash(msg: string) {
  flash.value = msg
  if (flashTimer) window.clearTimeout(flashTimer)
  flashTimer = window.setTimeout(() => (flash.value = ''), 3500)
}

/** 拉取列表；失败保留旧数据，下周期自动重试。 */
async function load() {
  try {
    const res = await listTasks()
    tasks.value = res.tasks
    error.value = ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  }
}

/** 一键演示：入队内置 sample_*.md 前 N 份，然后刷新队列。 */
async function runDemo() {
  if (demoBusy.value) return
  demoBusy.value = true
  error.value = ''
  try {
    const res = await demoRun(5)
    showFlash(`已入队 ${res.tasks.length} 份内置合成样本，正在顺序审查`)
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '演示入队失败'
  } finally {
    demoBusy.value = false
  }
}

onMounted(() => {
  load()
  // 轮询 2.5s：队列状态持续变化，页面开销可忽略
  timer = window.setInterval(load, 2500)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
  if (flashTimer) window.clearTimeout(flashTimer)
})
</script>

<template>
  <section class="rise">
    <!-- 页头：标题 + 手动刷新/一键演示 -->
    <div class="head">
      <div>
        <h2>任务队列</h2>
        <p class="muted">每 2.5s 自动刷新；高风险任务会停在「待审批」等你处理</p>
      </div>
      <div class="head-actions">
        <button class="btn btn-ghost" @click="load">刷新</button>
        <button class="btn btn-primary" :disabled="demoBusy" @click="runDemo">
          {{ demoBusy ? '入队中…' : '一键演示（5 份样本）' }}
        </button>
      </div>
    </div>

    <p v-if="flash" class="flash serif">※ {{ flash }}</p>
    <p v-if="error" class="err">{{ error }}</p>

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
      还没有任务——传几份合同，或点右上角「一键演示」用内置样本跑一遍
    </p>
    <p v-else-if="!visible.length" class="empty muted">该筛选下暂无任务</p>

    <!-- 任务列表 -->
    <div v-else class="card list">
      <div v-for="t in visible" :key="t.thread_id" class="row">
        <div class="name">
          <span class="file">{{ t.source }}</span>
          <span class="mono-num tid">{{ t.thread_id }}</span>
        </div>
        <div class="meta">
          <span v-if="t.risk_count != null" class="risk-badge mono-num"
                :class="t.status === 'done' ? 'ok' : 'seal'">
            {{ t.status === 'gate' ? `待审 ${t.risk_count}` : `${t.risk_count} 项` }}
          </span>
          <span class="stamp" :class="statusClass[t.status]">{{ statusText[t.status] }}</span>
          <span v-if="t.grade" class="grade serif">{{ gradeText[t.grade] ?? t.grade }}</span>
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
}

.head p {
  margin: 0;
  font-size: 13.5px;
}

.head-actions {
  display: flex;
  gap: 10px;
}

.flash {
  color: var(--ok);
  margin: 8px 0;
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
  background: #fff;
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
