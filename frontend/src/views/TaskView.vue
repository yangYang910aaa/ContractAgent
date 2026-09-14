<!--
  任务详情视图（页面骨架）：轮询任务状态，按状态渲染 审查中 / 闸口待审 / 报告 三块之一，
  并管住页面级交互——审批动作、原文抽屉、对话助手。
  面板本身拆在 components/task/ 下：GatePane（待审）/ ReportPane（报告）/ TaskRail（右栏）/
  RiskCard（风险卡）/ ReviewBlock（复核段）；这里只留状态、轮询与接线。
  轮询策略见 tick()：停在闸口等人工时暂停轮询，操作后恢复轮询等结果。
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { approve, editFields, getTask, reject } from '../api'
import ChatPanel from '../components/ChatPanel.vue'
import SourceDrawer from '../components/SourceDrawer.vue'
import GatePane from '../components/task/GatePane.vue'
import ReportPane from '../components/task/ReportPane.vue'
import TaskRail from '../components/task/TaskRail.vue'
import { riskLabel, SEVERITY_TEXT } from '../labels'
import type { LabeledRisk } from '../labels'
import type { ReviewSection, SourceAnchor, TaskDetail, TaskStatus } from '../types'

const props = defineProps<{ threadId: string }>()
const emit = defineEmits<{ back: [] }>()

// detail=当前任务快照（轮询更新）；error=加载失败；acting=审批请求进行中
const detail = ref<TaskDetail | null>(null)
const error = ref('')
const acting = ref(false)
const actionError = ref('') // 审批/编辑的表单级错误（如打回缺原因、JSON 非法）
const note = ref('') // 审批意见（打回必填）
const showEdit = ref(false) // 是否展开「编辑字段重审」面板
const patchText = ref('{\n  "warranty_months": 24\n}') // 字段补丁 JSON（编辑重审用）
// U2 原文抽屉：showSource=开关；anchor=定位指令（风险项 clause_ref 点进来）
const showSource = ref(false)
const sourceAnchor = ref<SourceAnchor | null>(null)
// 对话助手面板：浮动入口在面板组件里，这里只留一个引用（风险卡"问这条"要用它提问）
const chatRef = ref<InstanceType<typeof ChatPanel> | null>(null)
let timer: number | undefined

// 演示/验收直达：?source=1 打开原文抽屉（配合 App 的 ?view=task&thread= 使用）
if (new URLSearchParams(location.search).get('source') === '1') {
  showSource.value = true
}

/** 给原文抽屉的风险高亮：闸口看待审 high，报告看最终 risks（无 severity 按 high）。 */
const drawerRisks = computed(() => {
  const d = detail.value
  if (!d) return []
  const src =
    d.status === 'gate'
      ? (d.gate_payload?.high_risks ?? [])
      : d.status === 'done'
        ? (d.report?.risks ?? [])
        : []
  return src.map((r) => ({
    risk_type: r.risk_type,
    label: r.label ?? null,
    severity: (r as { severity?: string }).severity ?? 'high',
    clause_ref: r.clause_ref ?? '',
    evidence: r.evidence ?? '',
    // 原文摘录要一并带上：说明句（"预付款比例 70%"）在正文里搜不到，
    // 少了它抽屉里就只能整块标色、标不到那一句
    evidence_quote: (r as { evidence_quote?: string | null }).evidence_quote ?? '',
  }))
})

/** 右栏"原文核对"清单：把风险项的 clause_ref 去重聚合，取最高 severity 并计数，
 *  同时留一句摘录——条款号来自模型自述，可能对应不到条款块（实测有"三、其他 3"
 *  这类正文里不存在的号），点定位时靠摘录兜底才找得到位置。 */
const hitClauses = computed(() => {
  const seen = new Map<string, { clause: string; sev: string; count: number; quote: string }>()
  for (const r of drawerRisks.value) {
    const clause = (r.clause_ref ?? '').trim()
    if (!clause) continue
    const sev = r.severity === 'medium' ? 'medium' : 'high'
    const quote = (r.evidence_quote || r.evidence || '').trim()
    const cur = seen.get(clause)
    if (cur) {
      cur.count += 1
      if (cur.sev === 'medium' && sev === 'high') cur.sev = sev
      if (!cur.quote) cur.quote = quote
    } else {
      seen.set(clause, { clause, sev, count: 1, quote })
    }
  }
  return [...seen.values()]
})

// 状态 → 中文（含义见 style.css 的 .stamp-* 族）
const statusText: Record<TaskStatus, string> = {
  pending: '排队中',
  processing: '审查中',
  gate: '待人工审批',
  done: '已完成',
  error: '失败',
}

// 审查中 = pending/processing：显示进行中动画，不渲染闸口/报告
const extracting = computed(() => detail.value && ['pending', 'processing'].includes(detail.value.status))
/** 审查中提示的预计时长：双审要多跑一轮独立复核；扫描件/图片多一步本地 OCR
 *  （实测 3~17 秒/页），都比单审慢——原来固定写"约需 30~60 秒"，双审扫描件会像卡住。 */
const etaText = computed(() => {
  const doubleReview = detail.value?.review_mode === 'double'
  return doubleReview
    ? '双审（主审 + 独立复核）约需 1~2 分钟；扫描件/图片件还要加本地 OCR，更久'
    : '单审约需 30~60 秒；扫描件/图片件要加本地 OCR，约 1~2 分钟起'
})

// 报告风险：疑似空白模板单拎为顶部"结论条"，不进风险清单卡片与计数
const reportRisks = computed(() => detail.value?.report?.risks ?? [])
const templateNotice = computed(
  () => reportRisks.value.find((r) => r.risk_type === 'blank_template_suspected') ?? null,
)
const listRisks = computed(() => reportRisks.value.filter((r) => r.risk_type !== 'blank_template_suspected'))
// 双审复核段：review_mode=double 的报告才有（merge_review 产出；单审为 null）
const review = computed(() => detail.value?.report?.review ?? null)
// 闸口阶段报告还没生成，复核结论随 gate 载荷带出（否则审批人放行前看不到盲审结果）
const gateReview = computed<ReviewSection | null>(
  () => (detail.value?.gate_payload as { review?: ReviewSection | null } | null)?.review ?? null,
)

async function load() {
  try {
    const d = await getTask(props.threadId)
    detail.value = d
    error.value = ''
  } catch (err) {
    // 加载失败保留旧快照并提示，等待下个轮询周期自动重试
    error.value = err instanceof Error ? err.message : '加载失败'
  }
}

/** 轮询一跳：非闸口状态继续刷；闸口且无进行中审批 → 暂停等人工操作。 */
function tick() {
  if (!detail.value) return
  const st = detail.value.status
  // 停在闸口且没有正在提交的审批 → 停下轮询，等人操作
  if (st === 'gate' && !acting.value) return
  load()
}

/** 审批动作统一执行器：置 acting 锁 → 调后端 → 成功后清空意见输入。 */
async function runAction(fn: () => Promise<TaskDetail>) {
  acting.value = true
  actionError.value = ''
  try {
    detail.value = await fn()
    note.value = ''
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : '审批失败'
  } finally {
    acting.value = false
  }
}

/** 放行：意见可为空，动作本身即留痕。 */
function doApprove() {
  runAction(() => approve(props.threadId, note.value))
}

function doReject() {
  // 这种情况是：打回没填原因 → 前端拦截，避免无痕打回
  if (!note.value.trim()) {
    actionError.value = '打回请填写原因'
    return
  }
  runAction(() => reject(props.threadId, note.value))
}

function doEdit() {
  // 编辑重审：先本地校验 JSON，非法直接提示不请求后端
  let patches: Record<string, unknown>
  try {
    patches = JSON.parse(patchText.value)
  } catch {
    actionError.value = '补丁不是合法 JSON'
    return
  }
  runAction(() => editFields(props.threadId, patches, note.value || '修改字段后重审'))
}

/** 切换任务号时重置本地状态再拉新任务（watch props.threadId 触发）。 */
function resetFor() {
  detail.value = null
  error.value = ''
  acting.value = false
  note.value = ''
  showEdit.value = false
  load()
}

watch(() => props.threadId, resetFor)

onMounted(() => {
  load()
  timer = window.setInterval(tick, 1600)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})

// 阶段进度条：与后端状态一一映射（pending→排队、processing→抽取审查、
// gate→人工审批、done/error→报告）。error 时末段标红提示。
const stageNames = ['排队', '抽取审查', '人工审批', '报告']
const stageIndex = computed(() => {
  const st = detail.value?.status
  if (st === 'pending') return 0
  if (st === 'processing') return 1
  if (st === 'gate') return 2
  return 3
})
const stageFailed = computed(() => detail.value?.status === 'error')

/** 导出报告 JSON：前端侧生成下载（报告已全量在 detail.report 里）。 */
function downloadReport() {
  const report = detail.value?.report
  if (!report) return
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${detail.value?.source || 'report'}.report.json`
  a.click()
  URL.revokeObjectURL(url)
}

/** U2：打开原文抽屉。有 clause 时定位到对应条款块；无条款号但有 evidence 的
 *  中风险项（如"保密条款缺失"）按摘录定位到所在条款块——报告里所有风险都能
 *  "原文定位"，而不是只有带条款号的高风险。 */
function openSource(clause?: string, evidence?: string) {
  // seq 自增：同一目标反复点击也会触发抽屉内 watch 重新滚动
  if (clause || evidence) {
    sourceAnchor.value = {
      clause: clause ?? '',
      evidence: evidence ?? '',
      seq: (sourceAnchor.value?.seq ?? 0) + 1,
    }
  } else {
    sourceAnchor.value = null
  }
  showSource.value = true
}

/** 风险卡上的「问这条」：把"点风险 → 问为什么"做成一步（面板打开并直接提问）。 */
function askRisk(risk: LabeledRisk & { severity?: string }) {
  const name = riskLabel(risk)
  const severity = SEVERITY_TEXT[risk.severity ?? 'high'] ?? '高风险'
  chatRef.value?.openWith(`「${name}」这条为什么判${severity}？`)
}

/** 对话里点「查看这条风险」：滚到报告里同一条款的风险卡并闪一下（只做定位，不改数据）。 */
function focusRisk(clause: string) {
  const cards = Array.from(document.querySelectorAll<HTMLElement>('.risk[data-clause]'))
  // 分支：按条款号找不到对应风险卡（引用来自找条款、未必对应一条风险）→ 不猜、不跳
  const target = cards.find((el) => el.dataset.clause === clause)
  if (!target) return
  target.scrollIntoView({ behavior: 'smooth', block: 'center' })
  target.classList.add('flash-risk')
  window.setTimeout(() => target.classList.remove('flash-risk'), 1600)
}
</script>

<template>
  <section class="rise task">
    <div class="bar">
      <button class="btn btn-ghost" @click="emit('back')">← 返回</button>
      <!-- U2：gate/done/error 时点开抽屉核对原文；审查中也可开（自动等原文） -->
      <button
        v-if="detail && detail.status !== 'pending'"
        class="btn btn-ghost btn-source"
        @click="openSource()"
      >
        查看原合同
      </button>
    </div>

    <!-- 阶段进度条：当前步高亮；已完成步打勾点；error 末段标红 -->
    <div v-if="detail" class="steps">
      <div v-for="(s, i) in stageNames" :key="s" class="step"
           :class="{ on: i === stageIndex, done: i < stageIndex, fail: stageFailed && i === stageNames.length - 1 }">
        <span class="dot"></span>
        <span>{{ s }}</span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <!-- 无数据/加载中 -->
    <div v-else-if="!detail" class="card pad-center muted">加载中…</div>

    <!-- 审查中 -->
    <div v-else-if="extracting" class="card pad-center">
      <p class="serif big pulse">{{ statusText[detail.status] }}</p>
      <p class="muted">正在抽取字段 → 规则审查 → 政策比对，{{ etaText }}</p>
    </div>

    <!-- 待审批闸口（与报告页同款两栏：左=待审风险与审批，右=结论预览/原文核对） -->
    <div v-else-if="detail.status === 'gate' && detail.gate_payload" class="gate">
      <div class="rep-grid">
        <GatePane
          v-model:note="note"
          v-model:patch-text="patchText"
          class="rep-main"
          :detail="detail"
          :review="gateReview"
          :action-error="actionError"
          :acting="acting"
          :show-edit="showEdit"
          @approve="doApprove"
          @reject="doReject"
          @edit="doEdit"
          @toggle-edit="showEdit = !showEdit"
          @locate="openSource"
          @ask="askRisk"
        />
        <TaskRail
          class="rep-side"
          mode="gate"
          :detail="detail"
          :risks="[]"
          :hits="hitClauses"
          @locate="openSource"
          @open-source="openSource()"
        />
      </div>
    </div>

    <!-- 完成：报告（宽屏两栏：左=主流程，右=结论速览/关键字段/原文核对） -->
    <div v-else-if="detail.status === 'done' && detail.report" class="report">
      <div class="rep-grid">
        <ReportPane
          class="rep-main"
          :detail="detail"
          :risks="listRisks"
          :template-notice="templateNotice"
          :review="review"
          @export="downloadReport"
          @locate="openSource"
          @ask="askRisk"
        />
        <TaskRail
          class="rep-side"
          mode="report"
          :detail="detail"
          :risks="listRisks"
          :hits="hitClauses"
          @locate="openSource"
          @open-source="openSource()"
        />
      </div>
    </div>

    <!-- 失败 -->
    <div v-else-if="detail.status === 'error'" class="card pad err-box">
      <h3>审查失败</h3>
      <p>{{ detail.error || detail.report?.error || '未知错误' }}</p>
    </div>

    <!-- U2 原文抽屉：锚点与当前任务号绑定 -->
    <SourceDrawer
      v-if="showSource"
      :thread-id="props.threadId"
      :anchor="sourceAnchor"
      :risks="drawerRisks"
      @close="showSource = false"
    />

    <!-- 对话助手：只在有上下文的详情页出现（待审批/出报告后） -->
    <ChatPanel
      v-if="detail && (detail.status === 'gate' || detail.status === 'done')"
      :key="props.threadId"
      ref="chatRef"
      :thread-id="props.threadId"
      :file-name="detail.source"
      @open-clause="openSource"
      @focus-risk="focusRisk"
    />
  </section>
</template>

<style scoped>
.task {
  max-width: 1240px;
}

.bar {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 16px;
}

.btn-source {
  margin-left: auto;
}

.pad-center {
  padding: 48px 22px;
  text-align: center;
}

.big {
  font-size: 20px;
  letter-spacing: 0.04em;
  color: var(--info);
}

/* ---- 两栏布局：左主流程 + 右侧 sticky 速览（窄屏自动回落单栏）---- */
.rep-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 304px;
  gap: 18px;
  align-items: start;
}

.rep-main {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.rep-side {
  position: sticky;
  top: 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

@media (max-width: 1080px) {
  .rep-grid {
    grid-template-columns: 1fr;
  }

  .rep-side {
    position: static;
  }
}

.err-box h3 {
  margin-top: 0;
  color: var(--seal);
}

/* 阶段进度条：点 + 连线，当前步印章红、已完成步实心、失败末段标红 */
.steps {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 4px 0 18px;
}

.step {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 12.5px;
  letter-spacing: 0.02em;
  font-weight: 500;
}

.step + .step::before {
  content: '';
  width: 26px;
  height: 1px;
  background: var(--line);
  margin-right: 4px;
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  border: 2px solid var(--line-strong);
  background: #fff;
}

.step.on {
  color: var(--pri);
  font-weight: 700;
}

.step.on .dot {
  border-color: var(--pri);
  background: var(--pri);
  box-shadow: 0 0 0 3px var(--pri-soft);
  animation: pulse 1.6s ease-in-out infinite;
}

.step.done .dot {
  border-color: var(--ok);
  background: var(--ok);
}

.step.fail {
  color: var(--seal);
}

.step.fail .dot {
  border-color: var(--seal);
  background: var(--seal);
  box-shadow: 0 0 0 3px var(--seal-soft);
}
</style>
